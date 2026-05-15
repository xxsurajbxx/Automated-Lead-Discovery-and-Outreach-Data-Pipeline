# LinkedIn Lead Generation & Outreach Pipeline

An end-to-end, fully automated pipeline for discovering, profiling, scoring, and reaching out to targeted LinkedIn contacts — built on top of a real Chrome session via CDP, with deep anti-detection engineering and LLM-driven lead qualification.

---

## Overview

This system implements a five-stage pipeline that takes a set of targeting criteria and produces personalized, sent LinkedIn connection requests — with zero manual intervention between steps. Each stage is independently operable, idempotent, and backed by a shared SQLite state store.

```
discover_leads.py
      ↓
scrape_linkedin.py
      ↓
clean_linkedin_data.py
      ↓
score_profiles.py
      ↓
send_connections.py
```

---

## Architecture

### State Management: SQLite with Stage Flags

All pipeline state is tracked in a single SQLite database (`leads.db`). Each row represents a lead, and boolean/integer columns act as stage gates:

| Column | Set by | Meaning |
|---|---|---|
| `linkedin_url` | `discover_leads.py` | Canonical profile URL (primary key) |
| `name`, `slug` | `discover_leads.py` | Display name and URL slug |
| `scraped` | `scrape_linkedin.py` | Raw API intercept completed |
| `information_extracted` | `clean_linkedin_data.py` | Structured data parsed from raw JSON |
| `rating` | `score_profiles.py` | LLM-assigned score (0–10) |
| `connection_message` | `score_profiles.py` | LLM-generated personalized note |
| `connection_requested` | `send_connections.py` | `1` = sent, `-1` = skipped, `0` = pending |

This schema allows each stage to be re-run independently, pick up where it left off, and never double-process a record.

### Browser Automation: CDP over Real Chrome

Rather than spawning a sandboxed browser, all automation connects to a **live, logged-in Chrome instance** via the Chrome DevTools Protocol (CDP) using Playwright's `connect_over_cdp`. This means:

- LinkedIn sees your real session cookies, fingerprint, and browser history
- No new browser profiles to fingerprint or flag
- The automation is indistinguishable from a real user's session

### Anti-Detection: Human Behavior Simulation Layer

`human_behavior.py` implements a comprehensive behavioral simulation engine designed to defeat heuristic bot detection:

**Mouse Movement**: All clicks and navigations route through `HumanBehavior.natural_mouse_move()`, which generates **quadratic Bézier curves** between the current and target coordinates. Movement follows an ease-in/ease-out acceleration profile (fast at start and end, slower through the middle), and uses a persistent `_last_mouse_x/_last_mouse_y` tracker so the cursor never teleports from `(0, 0)`.

**Scrolling**: `smooth_scroll()` dispatches real `mouse.wheel()` events (not `window.scrollTo()` JS calls) at the current viewport center in randomized micro-steps, with variable inter-step timing mimicking human deceleration. This reliably triggers LinkedIn's lazy-loaded profile sections (experience, education, skills).

**Reading Simulation**: `simulate_reading()` orchestrates 5–8 stochastic actions per profile visit: 65% probability of downward scroll, 10% upward scroll, 15% idle mouse drift, and 10% element hover-and-release. Random long pauses (1.5–3s) are injected at 30% probability to simulate paragraph reading.

**Keystroke Dynamics**: All search queries and connection messages are typed character-by-character with `keyboard.type(ch, delay=random.randint(40, 170))` — variable inter-key delay that produces realistic WPM variance.

**Timing Randomization**: Every inter-action pause is drawn from uniform distributions rather than fixed sleeps. Run sizes are also randomized (2–4 or 3–5 profiles per session) to prevent detectable periodic request patterns.

**Playwright Stealth**: The lead discovery module applies `playwright_stealth.Stealth()` on the Google search page to suppress common headless browser signals (navigator properties, WebGL fingerprinting, etc.).

---

## Pipeline Stages

### Stage 1: Lead Discovery (`discover_leads.py`)

Drives the real Chrome session to Google and extracts LinkedIn profile URLs from search results.

**Query Construction**: Queries are assembled programmatically from environment-configured term banks:
- A `COMPANY_BANK` (target employers)
- A `TECHNICAL_TERM_BANK` (randomly samples 3–5 terms, joined as OR groups)
- A `LANGUAGE_BANK` (randomly samples 3–5 programming languages as OR groups)
- `CONSTANT_NEGATIVE_SEARCH_TERMS` and `VARIABLE_NEGATIVE_SEARCH_TERMS` (exclusion clauses)

Each run generates a distinct randomized query like:
```
site:linkedin.com/in/ "Citadel" ("Rust" OR "Go" OR "C++") ("distributed" OR "systems") -"recruiter"
```

**URL Canonicalization**: All extracted hrefs are decoded (handling Google's `/url?q=` redirect wrapping), parsed, and normalized to `https://www.linkedin.com/in/{slug}/` — deduplicated by canonical URL before insertion.

**Name Extraction**: Profile names are parsed from Google result headline text using regex-based splitting on ` - `, ` | `, ` – `, and ` — ` delimiters, with LinkedIn brand text stripped.

**CAPTCHA Detection**: Before and after each page load, the module inspects URL patterns, page titles, and body text for known CAPTCHA/challenge signals (`"unusual traffic"`, `"not a robot"`, `recaptcha`), raising a hard stop if detected.

**Pagination**: Automatically follows Google's `&start=` pagination up to a configurable `--max-pages-per-query` limit, then switches to a new randomized query if the target lead count hasn't been met.

### Stage 2: Profile Scraping (`scrape_linkedin.py`)

Navigates to each unscraped lead's LinkedIn profile and intercepts the internal API responses.

**Network Interception**: A Playwright `response` event listener is attached narrowly around each profile visit. It filters for LinkedIn's internal `voyagerIdentityDashProfileCards` GraphQL endpoint by inspecting the `queryId` parameter (with a fallback URL-contains check for unusual encodings) and validates `Content-Type: application/json`.

**Payload Validation**: A heuristic check (`payload_looks_like_profile_data`) scans the serialized JSON for profile-specific markers (`profileview`, `voyager.identity.profile.profile`, `firstName`, `lastName`) and optionally validates that the expected slug appears in the payload before writing it to disk.

**File Naming**: Captured payloads are written to `user_data/{slug}/raw_data/{sequence}_{type}_{timestamp}.json` with metadata envelope containing the source URL, capture timestamp, and a `looks_like_profile` flag.

**Run Sizing**: Each invocation randomly selects 3–5 leads from the unscraped queue, reducing the per-session footprint.

### Stage 3: Data Extraction (`clean_linkedin_data.py` + `profile_parser.py`)

Parses the raw intercepted JSON payloads into a clean, normalized profile record.

**Profile Identification**: `profile_parser.py` implements a scored candidate selection algorithm across all dict nodes in the payload tree. Each node is scored: +100 for matching `publicIdentifier`, +60 for matching the profile token extracted from the request URL's `profileUrn` parameter, +20 for a non-null full name, +10 for a valid headline. The highest-scoring node wins.

**Entity Card Disambiguation**: Experience and education entries are extracted by walking the full JSON tree and checking ancestor nodes for `fsd_profileCard:(token,SECTION_TYPE)` URN patterns. Only nodes whose ancestors contain a card URN matching both the target profile token and the relevant section (`EXPERIENCE` or `EDUCATION`) are extracted — preventing cross-profile contamination in responses that contain multiple people's data.

**Multi-File Merging**: Multiple raw JSON files per profile are parsed independently and merged via `merge_profile_records`. Scalar fields prefer longer values; headline fields use a scoring function that rewards role-title patterns (`" at "`, `"incoming"`, `"@"`) and penalizes skill-list formats; URL fields prefer values matching the LinkedIn profile regex.

**Noise Filtering**: A battery of heuristic filters reject low-quality records:
- `_is_noise_experience`: rejects entries with invalid date ranges, blocked generic titles (`"experience"`, `"education"`, `"github"`), employment type strings as company names, and `.pdf` suffixes
- `_is_noise_education`: rejects entries without school/degree signals
- `_is_generic_heading`: exact-match blocklist for section headers that appear as standalone nodes
- `_is_skill_list_headline`: detects bullet-separated skill lists masquerading as headlines

**Deduplication**: All extracted lists are deduplicated via a normalized dict-key fingerprint.

### Stage 4: LLM Scoring (`score_profiles.py`)

Sends each cleaned profile to an OpenAI model for qualification scoring and personalized message generation.

**Prompt Architecture**: The prompt combines a static system prompt from `LLMPrompt.txt` (which encodes the target persona, scoring rubric, and message style constraints) with a dynamic profile payload serialized as compact JSON (name, headline, profile URL, experience, education). Scoring constraints (threshold, message length limit) are injected at the boundary between the static and dynamic sections.

**Structured Output Enforcement**: The API is called with `"format": {"type": "json_object"}` to guarantee JSON output. The response is parsed from the `output_text` field with a fallback extractor that walks the output array for `output_text`-typed content parts.

**Response Validation**: `validate_result()` enforces hard constraints before writing to the database:
- Score coerced to float, clamped to [0, 10]
- Rationale must be a non-empty string
- If score >= threshold: message must be non-empty and ≤ 4 sentences (split on `(?<=[.!?])\s+`)
- If score < threshold: message must be empty string

**Retry with Backoff**: Each lead gets up to `--max-retries` attempts with exponential backoff (`min(10.0, 1.5^attempt)`), handling transient API/parse failures without abandoning the run.

### Stage 5: Connection Sending (`send_connections.py`)

Navigates to each qualifying lead's profile and sends a personalized connection request.

**Profile Navigation**: Uses the same LinkedIn search bar flow as the scraper, matching on the stored slug with a two-pass strategy: exact `/in/{slug}/` suffix match first, then partial-URL fallback, then first result.

**Connection Path Resolution**: `get_connection_action()` implements a multi-branch detection strategy for the Connect button:

1. **Already-connected detection**: inspects for `"Pending"` buttons and `"1st"` degree indicators
2. **Header CTA analysis**: collects visible button labels from the profile action bar; if `"More"` is present but `"Connect"` is not, routes directly to the More menu path
3. **Direct button selectors**: tries a prioritized list of CSS selectors (`aria-label*='Invite'`, SVG `use[href='#connect-small']`, etc.) with explicit skip of `"Pending"` labels
4. **More menu exploration**: `_try_global_more_menu_for_connect()` tries ~9 overflow button selectors, clicks each, inspects the open dropdown for `Connect` menu items via role and selector matching, then falls back to a scored DOM evaluation that ranks candidate elements by proximity to "Message"/"Follow" CTAs and presence of overflow icon SVGs
5. **SPA stability fixes**: `_stabilize_profile_page()` presses Escape twice, scrolls to top, checks for header CTAs, and reloads the page once if they're missing — addressing LinkedIn's SPA overlay behavior where previous search results remain in the DOM during profile navigation

**Message Typing**: The personalized message is typed into the invitation modal character-by-character with randomized keystroke delay, preceded by a natural mouse move to the textarea.

**Post-Send Cleanup**: On successful connection, the lead's `user_data/{slug}/` folder is deleted with `shutil.rmtree` to reclaim disk space.

---

## Setup

### Prerequisites

- Python 3.10+
- Google Chrome installed
- A LinkedIn account logged in to Chrome

### Installation

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install playwright playwright-stealth
playwright install chromium
```

### Configuration (`.env`)

```bash
# API Keys
OPENAI_API_KEY=sk-...

# Chrome Remote Debugging
CDP_ENDPOINT=http://127.0.0.1:9222

# Lead Discovery Term Banks
COMPANY_BANK=["Jane Street", "Citadel", "Hudson River Trading", "Two Sigma", "Google"]
TECHNICAL_TERM_BANK=["distributed systems", "systems programming", "low latency", "kernel", "firmware"]
LANGUAGE_BANK=["Rust", "Go", "C++", "Assembly"]

# Negative Filters (constant)
CONSTANT_NEGATIVE_SEARCH_TERMS=["recruiter", "talent acquisition"]

# Negative Filters (one randomly sampled per query)
VARIABLE_NEGATIVE_SEARCH_TERMS=["frontend", "product manager", "marketing"]
```

Term banks support array syntax (`["a", "b"]`), CSV, or newline-delimited values.

### Start Chrome with Remote Debugging

**macOS:**
```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/chrome-debug-profile"
```

**Linux:**
```bash
google-chrome --remote-debugging-port=9222 --user-data-dir="$HOME/chrome-debug-profile"
```

Log in to LinkedIn in this browser window before running any pipeline step.

---

## Running the Pipeline

### Full Run

```bash
source venv/bin/activate
python3 discover_leads.py --limit 25
python3 scrape_linkedin.py --db leads.db
python3 clean_linkedin_data.py --db leads.db --input-dir user_data
python3 score_profiles.py --db leads.db --prompt-file LLMPrompt.txt --threshold 5.0
python3 send_connections.py --db leads.db --threshold 5.0
```

### Per-Stage Options

**`discover_leads.py`**
```
--limit N              Number of new leads to collect (default: 25)
--db PATH              SQLite database path (default: leads.db)
--max-pages-per-query  Max Google result pages per query (default: 5)
--cdp-endpoint URL     Chrome CDP endpoint (default: http://127.0.0.1:9222)
```

**`scrape_linkedin.py`**
```
--db PATH              SQLite database path (default: leads.db)
SHOW_CURSOR=1          Enable visible red cursor overlay for debugging
DEBUG_INTERCEPT=1      Log every intercepted network response
```

**`clean_linkedin_data.py`**
```
--db PATH              SQLite database path (default: leads.db)
--input-dir PATH       user_data directory (default: user_data)
--include-empty        Write cleaned files even when no fields extracted
```

**`score_profiles.py`**
```
--db PATH              SQLite database path (default: leads.db)
--user-data-dir PATH   Root directory for cleaned JSON (default: user_data)
--prompt-file PATH     LLM system prompt file (default: LLMPrompt.txt)
--model NAME           OpenAI model (default: gpt-5-mini)
--threshold FLOAT      Minimum score to qualify (default: 5.0)
--max-retries N        Retry attempts per lead (default: 3)
--limit N              Max leads to process per run (default: all)
--allow-unextracted    Process leads regardless of information_extracted flag
```

**`send_connections.py`**
```
--db PATH              SQLite database path (default: leads.db)
--threshold FLOAT      Minimum rating to process (default: 5.0)
--limit N              Max leads per run; 0 = all (default: 0)
--daily-limit N        Hard cap per run for rate limiting (default: 20)
--cdp-endpoint URL     Chrome CDP endpoint
```

---

## File Structure

```
├── discover_leads.py         Stage 1: Google → LinkedIn URL extraction
├── scrape_linkedin.py        Stage 2: LinkedIn profile API interception
├── clean_linkedin_data.py    Stage 3: Raw JSON → structured profile record
├── profile_parser.py         Parsing engine (entity resolution, noise filters)
├── score_profiles.py         Stage 4: LLM scoring and message generation
├── send_connections.py       Stage 5: Automated connection request delivery
├── human_behavior.py         Anti-detection behavioral simulation engine
├── linkedin_common.py        Shared LinkedIn navigation primitives
├── env_utils.py              .env parser with multiline array support
├── LLMPrompt.txt             Scoring rubric and persona definition
├── leads.db                  SQLite pipeline state store
└── user_data/
    └── {slug}/
        ├── raw_data/         Intercepted API JSON payloads
        └── {slug}_cleaned_data.json
```

---

## Debugging

**Visible cursor overlay** — renders a red circle that tracks the automated mouse in real time:
```bash
SHOW_CURSOR=1 python3 scrape_linkedin.py
```

**Network intercept logging** — prints every response URL evaluated during scraping:
```bash
DEBUG_INTERCEPT=1 python3 scrape_linkedin.py
```

The connection sender includes inline diagnostic snapshots (`_debug_profile_action_snapshot`) that log page title, `readyState`, element counts for all relevant selectors, and visible button label samples when the Connect path cannot be resolved.

---

## Design Notes

**Why CDP over a headless browser**: A sandboxed headless Chromium has a distinctive TLS fingerprint, missing browser APIs, and no session history — all signals that platforms use for bot detection. Attaching to a live session via CDP inherits the full browser environment and avoids these tells entirely.

**Why raw API interception instead of DOM scraping**: LinkedIn's frontend renders profile data from Voyager GraphQL responses. Intercepting these payloads at the network layer gives access to structured data (typed fields, URNs, nested objects) that is difficult to reliably extract from rendered HTML, which is obfuscated, lazy-loaded, and subject to frequent DOM structure changes.

**Why per-run randomized batch sizes**: Fixed batch sizes create a detectable periodic signal. Drawing from a uniform distribution each run ensures the activity pattern looks like variable human usage rather than an automated schedule.

**Why a shared SQLite store**: A single-file relational store provides ACID semantics for stage flag updates, is trivially portable, requires no server process, and supports ad-hoc queries for pipeline inspection without any additional tooling.
