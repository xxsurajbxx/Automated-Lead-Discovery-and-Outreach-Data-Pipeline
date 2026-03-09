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
    - control Google pages per query:
        python3 discover_leads.py --max-pages-per-query 8

notes:
    - installs unique LinkedIn profile URLs only (deduplicated by primary key)
    - scans Google using randomized queries built from term banks in discover_leads.py
    - paginates by increasing the &start= parameter when needed


run the linkedin scraper:
    source venv/bin/activate
    python3 enrichment.py --db leads.db


run the post-processing extractor:
    source venv/bin/activate
    python3 extract_profiles.py

extractor behavior:
    - reads pending leads from leads.db where scraped = 1 and information_extracted = 0
    - reads each user's raw json files from user_data/{slug}/raw_data
    - extracts profile identity, experience summary, and education summary
    - merges overlapping values across files for the same person
    - skips invalid/noisy files with warnings
    - writes one cleaned file per user to user_data/{slug}/{slug}_cleaned_data.json


run the llm scorer + outreach draft generator:
    source venv/bin/activate
    python3 score_and_message_profiles.py

llm scorer inputs:
    - reads pending leads from leads.db where rating IS NULL
    - expects cleaned profile files at user_data/{slug}/{slug}_cleaned_data.json
    - requires OPENAI_API_KEY in env

common runtime flags:
    - custom db path:
        python3 score_and_message_profiles.py --db leads.db
    - custom user_data root:
        python3 score_and_message_profiles.py --user-data-dir user_data
    - custom goals file:
        python3 score_and_message_profiles.py --goals-file goals.txt
    - custom model and threshold:
        python3 score_and_message_profiles.py --model gpt-5-mini --threshold 6

temporary testing behavior (marked temporary in code):
    - by default, the script lists all eligible leads, picks one lead, runs the llm for that one lead, updates DB, and exits
    - to disable temporary mode and process all pending leads:
        python3 score_and_message_profiles.py --disable-temp-single-run