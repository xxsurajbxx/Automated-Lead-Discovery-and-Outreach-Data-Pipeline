# LinkedIn Referral Farmer — Current Runbook

This README is updated for the **current file names** in this repo.

Pipeline order (run in this exact sequence):
1. `discover_leads.py`
2. `scrape_linkedin.py`
3. `clean_linkedin_data.py`
4. `score_profiles.py`
5. `send_connections.py`


## 0) One-time setup

Create and activate a virtual environment:

```bash
python3 -m venv venv
source venv/bin/activate
```

Install dependencies:

```bash
python3 -m pip install --upgrade pip
python3 -m pip install playwright playwright-stealth
python3 -m playwright install chromium
```

Create/update `.env` with at least:
- `OPENAI_API_KEY` (required for `score_profiles.py`)
- `CDP_ENDPOINT` (default `http://127.0.0.1:9222`)
- search term banks used by `discover_leads.py`


## 1) Start Chrome in remote-debug mode

You must run a real logged-in Chrome session for LinkedIn automation.

macOS:

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --user-data-dir="$HOME/chrome-debug-profile"
```

Linux:

```bash
google-chrome --remote-debugging-port=9222 --user-data-dir="$HOME/chrome-debug-profile"
```

Then log in to LinkedIn in that Chrome profile.


## 2) Discover leads (`discover_leads.py`)

What it does:
- Uses randomized Google queries to find LinkedIn profile URLs.
- Inserts unique leads into `leads.db` (creates table if missing).
- Sets baseline fields like `linkedin_url`, `name`, `slug`, `scraped=0`.

Run:

```bash
source venv/bin/activate
python3 discover_leads.py
```

Common flags:

```bash
python3 discover_leads.py --limit 25
python3 discover_leads.py --db leads.db
python3 discover_leads.py --max-pages-per-query 8
python3 discover_leads.py --cdp-endpoint http://127.0.0.1:9222
```


## 3) Scrape LinkedIn profile payloads (`scrape_linkedin.py`)

What it does:
- Reads leads where `scraped = 0`.
- Searches each person on LinkedIn and opens the profile.
- Intercepts/saves raw profile JSON into `user_data/{slug}/raw_data/*.json`.
- Marks attempted leads as `scraped = 1`.

Run:

```bash
source venv/bin/activate
python3 scrape_linkedin.py --db leads.db
```

Optional cursor debug:

```bash
SHOW_CURSOR=1 python3 scrape_linkedin.py --db leads.db
```


## 4) Clean extracted data (`clean_linkedin_data.py`)

What it does:
- Reads leads where `scraped = 1` and `information_extracted = 0`.
- Parses and merges raw JSON for each person.
- Writes cleaned output to `user_data/{slug}/{slug}_cleaned_data.json`.
- Marks processed leads as `information_extracted = 1`.

Run:

```bash
source venv/bin/activate
python3 clean_linkedin_data.py --db leads.db --input-dir user_data
```

Optional:

```bash
python3 clean_linkedin_data.py --include-empty
```


## 5) Score profiles + generate connection messages (`score_profiles.py`)

What it does:
- Reads leads where `rating IS NULL` (optionally only extracted rows).
- Loads cleaned profile JSON.
- Calls OpenAI with `LLMPrompt.txt`.
- Writes `rating` and `connection_message` back to `leads.db`.

Run:

```bash
source venv/bin/activate
python3 score_profiles.py --db leads.db --user-data-dir user_data --prompt-file LLMPrompt.txt
```

Common flags:

```bash
python3 score_profiles.py --model gpt-5-mini --threshold 5.0
python3 score_profiles.py --limit 50
python3 score_profiles.py --allow-unextracted
python3 score_profiles.py --max-retries 3
```


## 6) Send connection requests (`send_connections.py`)

What it does:
- Reads leads where `rating >= threshold` and `connection_requested = 0`.
- Searches each person via LinkedIn search bar, opens profile, and tries to connect.
- Sends the saved `connection_message` as note when possible.
- Updates status:
  - `connection_requested = 1` → request sent successfully
  - `connection_requested = -1` → already connected/pending/no connect path

Run:

```bash
source venv/bin/activate
python3 send_connections.py --db leads.db --threshold 5.0
```

Common flags:

```bash
python3 send_connections.py --limit 30 --daily-limit 20
python3 send_connections.py --disable-temp-single-run
python3 send_connections.py --cdp-endpoint http://127.0.0.1:9222
```


## Database fields used in pipeline

- `scraped`: set by `scrape_linkedin.py`
- `information_extracted`: set by `clean_linkedin_data.py`
- `rating`, `connection_message`: set by `score_profiles.py`
- `connection_requested`: set by `send_connections.py` (`1`, `0`, `-1`)


## Typical full run (copy/paste)

```bash
source venv/bin/activate
python3 discover_leads.py --limit 25
python3 scrape_linkedin.py --db leads.db
python3 clean_linkedin_data.py --db leads.db --input-dir user_data
python3 score_profiles.py --db leads.db --user-data-dir user_data --prompt-file LLMPrompt.txt --threshold 5.0
python3 send_connections.py --db leads.db --threshold 5.0 --disable-temp-single-run
```