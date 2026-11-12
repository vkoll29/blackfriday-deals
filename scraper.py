#!/usr/bin/env python3
"""Carrefour Kenya price tracker.

Loads Carrefour's search pages in a headless browser (the listings need
JavaScript), keeps a daily price history in data/history.csv and writes a
light-mode report to docs/index.html.

    python scraper.py                 # live run
    python scraper.py --headed        # watch the browser
    python scraper.py --fixture tests/fixture   # offline test, no network
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
FIELDS = ["date", "category", "id", "name", "brand", "price", "was", "stock", "url"]

# Runs inside the page. Finds every product link (/p/<id>), climbs to its card,
# and reads name, price, struck-through "was" price, discount badge and stock.
EXTRACT_JS = r"""
(base) => {
  const pid = h => { const m = (h || '').match(/\/p\/([A-Za-z0-9_-]+)/); return m ? m[1] : null; };
  const num = s => {
    const m = (s || '').match(/\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?/);
    return m ? parseFloat(m[0].replace(/,/g, '')) : null;
  };
  const isPrice = s => /^\d{1,3}(,\d{3})*(\.\d{1,2})?$|^\d+(\.\d{1,2})?$/.test(s);
  const out = {};
  for (const a of document.querySelectorAll('a[href*="/p/"]')) {
    const href = a.getAttribute('href');
    const id = pid(href);
    const name = (a.innerText || '').trim();
    if (!id || !name) continue;
    let card = a.parentElement, found = false;
    for (let i = 0; i < 6 && card; i++, card = card.parentElement) {
      const ids = new Set([...card.querySelectorAll('a[href*="/p/"]')].map(x => pid(x.getAttribute('href'))));
      if (ids.size > 1) break;
      if (/KES/.test(card.innerText || '')) { found = true; break; }
    }
    if (!found) continue;
    const lines = card.innerText.split('\n').map(s => s.trim()).filter(Boolean);
    const k = lines.findIndex(l => /^KES\s*[\d,.]*$/.test(l));
    let price = null, was = null;
    if (k >= 0) {
      const rest = lines[k].replace(/^KES/, '').trim();
      const seq = rest ? [rest, ...lines.slice(k + 1, k + 3)] : lines.slice(k + 1, k + 3);
      if (seq[0] && isPrice(seq[0])) price = num(seq[0]);
      if (seq[1] && isPrice(seq[1])) was = num(seq[1]);
    }
    const strike = card.querySelector('.line-through, s, del');
    if (strike && num(strike.innerText)) was = num(strike.innerText);
    const stock = lines.find(l => /only \d+ left|out of stock|sold out|unavailable/i.test(l)) || '';
    if (price === null) continue;
    out[id] = { id, name, price, was, stock, url: new URL(href, base).href.split('?')[0] };
  }
  return Object.values(out);
}
"""


def load_products(page, url: str, base: str, max_scrolls: int) -> list[dict]:
    """Open a listing page, scroll until no more products load, return the cards."""
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_selector('a[href*="/p/"]', state="attached", timeout=30_000)
    except PWTimeout:
        return []
    last, stable = -1, 0
    for _ in range(max_scrolls):
        n = page.locator('a[href*="/p/"]').count()
        stable = stable + 1 if n == last else 0
        if stable >= 2:
            break
        last = n
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1500)
    return page.evaluate(EXTRACT_JS, base)


def brand_of(name: str, brands: list[str]) -> str:
    low = name.lower()
    for b in brands:
        if re.search(r"(?<![a-z])" + re.escape(b.lower()) + r"(?![a-z])", low):
            return b
    return name.split()[0].title() if name.split() else ""


def matches(name: str, cat: dict) -> bool:
    low = " " + name.lower() + " "
    return any(w in low for w in cat["include"]) and not any(w in low for w in cat["exclude"])


def read_history(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["price"] = float(r["price"])
        r["was"] = float(r["was"]) if r["was"] else None
    return rows


def write_history(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in sorted(rows, key=lambda r: (r["date"], r["category"], r["id"])):
            w.writerow({k: ("" if r.get(k) is None else r[k]) for k in FIELDS})


def kes(v: float | None) -> str:
    return "" if v is None else f"{v:,.0f}"


def build_report(cfg: dict, today: str, todays: list[dict], history: list[dict], warnings: list[str]) -> str:
    pref = [b.lower() for b in cfg["preferred_brands"]]
    past: dict[str, list[dict]] = {}
    for r in history:
        if r["date"] < today:
            past.setdefault(r["id"], []).append(r)
    has_past = bool(past)
    tabs, panels = [], []
    for i, cat in enumerate(cfg["categories"]):
        items = [r for r in todays if r["category"] == cat["key"] and r["price"] <= cat["cap"]]
        over = sum(1 for r in todays if r["category"] == cat["key"] and r["price"] > cat["cap"])
        items.sort(key=lambda r: (pref.index(r["brand"].lower()) if r["brand"].lower() in pref else 99, r["price"]))
        rows = []
        for r in items:
            tags = []
            off = ""
            if r["was"] and r["was"] > r["price"]:
                pct = round(100 * (1 - r["price"] / r["was"]))
                off = f"{pct}%"
                if pct >= 25:
                    tags.append(("warn", "big discount, verify"))
            prev = sorted(past.get(r["id"], []), key=lambda x: x["date"])
            if prev:
                d = r["price"] - prev[-1]["price"]
                if d < 0:
                    tags.append(("good", f"▼ {kes(-d)} since {prev[-1]['date']}"))
                elif d > 0:
                    tags.append(("bad", f"▲ {kes(d)} since {prev[-1]['date']}"))
                if r["price"] < min(x["price"] for x in prev):
                    tags.append(("good", "lowest seen"))
            elif has_past:
                tags.append(("note", "new"))
            if cat["key"] == "fridge":
                if "inverter" in r["name"].lower() and r["price"] <= 55000:
                    tags.append(("star", "★ inverter ≤55k"))
                if r["price"] > 60000:
                    tags.append(("note", "stretch"))
            if r["brand"].lower() in pref:
                tags.append(("note", "preferred brand"))
            tag_html = "".join(f'<span class="tag t-{k}">{html.escape(v)}</span>' for k, v in tags)
            rows.append(
                f'<tr><td><a href="{html.escape(r["url"])}" target="_blank" rel="noopener">{html.escape(r["name"])}</a></td>'
                f'<td>{html.escape(r["brand"])}</td><td class="n price">{kes(r["price"])}</td>'
                f'<td class="n was">{kes(r["was"])}</td><td class="n">{off}</td>'
                f'<td>{html.escape(r["stock"] or "not shown")}</td><td>{tag_html}</td></tr>'
            )
        body = "".join(rows) or '<tr><td colspan="7">Nothing found under the cap today.</td></tr>'
        note = f"{len(items)} items at or under KES {kes(cat['cap'])}" + (f" · {over} more above the cap are hidden" if over else "")
        tabs.append(f'<button class="tab" type="button" data-t="{cat["key"]}" aria-selected="{"true" if i == 0 else "false"}">{html.escape(cat["name"])} ({len(items)})</button>')
        panels.append(
            f'<section id="p-{cat["key"]}"{"" if i == 0 else " hidden"}><p class="note">{note}</p><div class="tbl"><table>'
            "<thead><tr><th>Product</th><th>Brand</th><th>Price</th><th>Was</th><th>Off</th><th>Stock</th><th>Notes</th></tr></thead>"
            f"<tbody>{body}</tbody></table></div></section>"
        )
    warn_html = "".join(f"<li>{html.escape(w)}</li>" for w in warnings)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Carrefour Price Tracker</title>
<style>
:root{{color-scheme:light;--bg:#f6f7f5;--surface:#fff;--fg:#1b2420;--muted:#5c6b64;--line:#dde3df;--accent:#0f6b55;--soft:#e2f1ec;--star:#b86e00;--star-soft:#fff2db;--bad:#b23a2e;--bad-soft:#fbe7e4;--good:#1d7a3e}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}
.wrap{{max-width:1100px;margin:0 auto;padding:20px 16px 40px}}
h1{{font-size:1.6rem;margin:0}} .sub,.note{{color:var(--muted);margin:4px 0 0}}
.tabs{{display:flex;gap:6px;overflow-x:auto;padding:14px 0 8px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg)}}
.tab{{flex:none;font:600 .85rem inherit;padding:7px 12px;border-radius:999px;border:1px solid var(--line);background:var(--surface);color:var(--fg);cursor:pointer}}
.tab[aria-selected="true"]{{background:var(--accent);border-color:var(--accent);color:#fff}}
.tbl{{overflow-x:auto;margin-top:10px;border:1px solid var(--line);border-radius:8px;background:var(--surface)}}
table{{border-collapse:collapse;width:100%;min-width:720px;font-size:.86rem}}
th,td{{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}}
th{{font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);background:var(--bg)}}
tr:last-child td{{border-bottom:0}} td.n{{font-variant-numeric:tabular-nums;white-space:nowrap}} td.price{{font-weight:700}}
td.was{{color:var(--muted);text-decoration:line-through}} a{{color:var(--accent)}}
.tag{{display:inline-block;font-size:.68rem;font-weight:700;padding:1px 6px;border-radius:4px;margin:2px 4px 0 0;white-space:nowrap}}
.t-star,.t-warn{{background:var(--star-soft);color:var(--star)}} .t-good{{background:var(--soft);color:var(--good)}}
.t-bad{{background:var(--bad-soft);color:var(--bad)}} .t-note{{background:var(--bg);color:var(--muted);border:1px solid var(--line)}}
.warn{{color:var(--bad);font-size:.85rem}}
</style></head><body><div class="wrap">
<h1>Carrefour Price Tracker</h1>
<p class="sub">Carrefour Kenya · prices in KES · checked {today} · preferred brands listed first, then by price</p>
{f'<ul class="warn">{warn_html}</ul>' if warnings else ''}
<div class="tabs" role="tablist">{''.join(tabs)}</div>
{''.join(panels)}
<p class="note">Prices come from Carrefour's search listings. Confirm the price and stock in the cart before paying.</p>
</div><script>
document.querySelectorAll('.tab').forEach(function(t){{t.onclick=function(){{
  document.querySelectorAll('.tab').forEach(function(x){{x.setAttribute('aria-selected',x===t)}});
  document.querySelectorAll('section').forEach(function(s){{s.hidden=s.id!=='p-'+t.dataset.t}});
}}}});
</script></body></html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--out", default=str(ROOT), help="folder that holds data/ and docs/")
    ap.add_argument("--fixture", help="folder of saved HTML pages to read instead of the live site")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    out = Path(args.out)
    today = datetime.now(ZoneInfo(cfg["timezone"])).strftime("%Y-%m-%d")
    base = cfg["base_url"]
    brands = cfg["preferred_brands"] + cfg["excluded_brands"] + cfg["other_brands"]
    excluded = {b.lower() for b in cfg["excluded_brands"]}
    debug = out / "debug"

    found: dict[str, dict] = {}
    warnings: list[str] = []
    searches = cards_total = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_context(locale="en-KE", viewport={"width": 1366, "height": 900}).new_page()
        for cat in cfg["categories"]:
            for kw in cat["keywords"]:
                if args.fixture:
                    f = Path(args.fixture) / f"{cat['key']}.html"
                    f = f if f.exists() else Path(args.fixture) / "default.html"
                    url = f.resolve().as_uri()
                else:
                    url = base + cfg["search_path"].format(kw=quote(kw))
                    if searches:
                        time.sleep(cfg["delay_seconds"])
                searches += 1
                try:
                    cards = load_products(page, url, base, cfg["max_scrolls"])
                except Exception as e:  # network error, navigation failure
                    cards = []
                    warnings.append(f'Search "{kw}" failed: {type(e).__name__}')
                if not cards:
                    warnings.append(f'Search "{kw}" returned no products.')
                    debug.mkdir(parents=True, exist_ok=True)
                    slug = re.sub(r"\W+", "-", kw)
                    try:
                        page.screenshot(path=str(debug / f"{slug}.png"), full_page=True)
                        (debug / f"{slug}.html").write_text(page.content(), encoding="utf-8")
                    except Exception:
                        pass
                    continue
                cards_total += len(cards)
                kept = 0
                for c in cards:
                    if not matches(c["name"], cat):
                        continue
                    brand = brand_of(c["name"], brands)
                    if brand.lower() in excluded:
                        continue
                    kept += 1
                    found.setdefault(c["id"], {"date": today, "category": cat["key"], "brand": brand, **c})
                print(f'{cat["name"]:<16} "{kw}": {len(cards)} listed, {kept} kept')
        browser.close()

    if cards_total == 0:
        for w in warnings:
            print("WARNING:", w, file=sys.stderr)
        print("No products found on any search. The site may be blocking this run or its layout changed. "
              "See debug/ for the pages. History and report left unchanged.", file=sys.stderr)
        return 2

    hist_path = out / "data" / "history.csv"
    history = [r for r in read_history(hist_path) if r["date"] != today] + list(found.values())
    write_history(hist_path, history)
    (out / "data" / "latest.json").write_text(
        json.dumps({"date": today, "warnings": warnings, "items": list(found.values())}, indent=1, ensure_ascii=False),
        encoding="utf-8")
    (out / "docs").mkdir(parents=True, exist_ok=True)
    (out / "docs" / "index.html").write_text(build_report(cfg, today, list(found.values()), history, warnings), encoding="utf-8")
    print(f"{today}: {len(found)} products saved. Report: {out / 'docs' / 'index.html'}")
    for w in warnings:
        print("WARNING:", w, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
