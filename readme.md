in order to run i need to run

to install necessary modules:
    pip install playwright
    playwright install chromium

Google in debug mode with port 9222:
    macOS: /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --user-data-dir="$HOME/chrome-debug-profile"
    linux: google-chrome --remote-debugging-port=9222 --user-data-dir="$HOME/chrome-debug-profile"

run the scraper:
    source venv/bin/activate
    python3 enrichment.py --input people.csv

run the post-processing extractor (separate from scraper):
    source venv/bin/activate
    python3 extract_profiles.py --input-dir intercepted_json --output profiles.jsonl

extractor behavior:
    - reads each person folder under intercepted_json one by one
    - reads each json file inside each person folder
    - extracts profile identity, experience summary, and education summary
    - merges overlapping values across files for the same person
    - skips invalid/noisy files with warnings
    - writes one normalized json object per person to profiles.jsonl