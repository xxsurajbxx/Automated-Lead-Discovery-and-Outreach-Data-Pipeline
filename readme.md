in order to run i need to run

to install necessary modules:
    pip install playwright
    playwright install chromium

Google in debug mode with port 9222:
    /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --user-data-dir="$HOME/chrome-debug-profile"

run the scraper:
    source venv/bin/activate
    python3 enrichment.py --input people.csv