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

headline file 1
components: text component in file 8 (about me section)