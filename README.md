# Carrefour Kenya price tracker

Checks Carrefour Kenya once a day for fridges, air fryers, blenders, water dispensers,
pressure cookers, microwaves and coffee makers. It keeps a price history and builds a report.

## What it does
- Opens Carrefour's search pages in a headless browser (the listings need JavaScript) and scrolls to load every result.
- Keeps real products only: accessories are filtered out, and Nunix and Roch are dropped.
- Saves every price to `data/history.csv` (one row per product per day) and `data/latest.json`.
- Writes `docs/index.html`: one tab per category, preferred brands first, items over the cap hidden
  (fridge KES 70,000, everything else KES 15,000), with price drops, rises and "lowest seen" marked.

## Run it on GitHub (free)
1. Create a repository and push these files.
2. In the repo: Settings > Actions > General > Workflow permissions > "Read and write permissions".
3. Actions tab > "Carrefour price check" > "Run workflow" to test. After that it runs daily at 07:15 Nairobi time.
4. Optional: Settings > Pages > deploy from branch `main`, folder `/docs`, to view the report as a web page.

## Run it on your computer
    pip install -r requirements.txt
    playwright install chromium
    python scraper.py            # add --headed to watch the browser

## Change what it tracks
Edit `config.json`: categories, search keywords, price caps, include/exclude words, brand order.

## If a run fails
Exit code 2 means no products were found on any search (blocked, or the page layout changed).
The history is left untouched and the pages it saw are saved in `debug/` (uploaded as a workflow artifact).

## Fair use
About 10 page loads a day, a few seconds apart, no login. Carrefour's robots.txt does not block these
search pages, but check their terms of use if you plan to do more than personal price tracking.
