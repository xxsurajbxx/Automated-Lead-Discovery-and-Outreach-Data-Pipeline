in order to run i need to run

to install necessary modules:
    python3 -m pip install playwright playwright-stealth
    python3 -m playwright install chromium

Google in debug mode with port 9222:
    macOS: /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --user-data-dir="$HOME/chrome-debug-profile"
    linux: google-chrome --remote-debugging-port=9222 --user-data-dir="$HOME/chrome-debug-profile"


run the lead discovery collector:
    source venv/bin/activate
    python3 discover_leads.py

collector defaults:
    - collects up to 10 new leads per run
    - uses leads.db in the project root (creates it if missing)
    - launches Playwright Chromium in headful mode

common runtime flags:
    - set custom lead target:
        python3 discover_leads.py --limit 25
    - set custom DB path:
        python3 discover_leads.py --db leads.db
    - run headless:
        python3 discover_leads.py --headless
    - control Google pages per query:
        python3 discover_leads.py --max-pages-per-query 8

notes:
    - installs unique LinkedIn profile URLs only (deduplicated by primary key)
    - scans Google using randomized queries built from term banks in discover_leads.py
    - paginates by increasing the &start= parameter when needed


run the linkedin scraper:
    source venv/bin/activate
    python3 enrichment.py --input people.csv


run the post-processing extractor:
    source venv/bin/activate
    python3 extract_profiles.py --input-dir intercepted_json --output profiles.jsonl

extractor behavior:
    - reads each person folder under intercepted_json one by one
    - reads each json file inside each person folder
    - extracts profile identity, experience summary, and education summary
    - merges overlapping values across files for the same person
    - skips invalid/noisy files with warnings
    - writes one normalized json object per person to profiles.jsonl