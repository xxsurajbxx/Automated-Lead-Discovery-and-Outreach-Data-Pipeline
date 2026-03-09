## Plan: LLM Profile Scoring + Outreach Draft

Build a new standalone CLI script that uses [leads.db](leads.db) as the source of truth: select all leads where `rating IS NULL`, load each lead’s cleaned profile from [user_data/{slug}/{slug}_cleaned_data.json](user_data), score it with OpenAI using your goals text, and write results back to the database. Each result includes a single 0–10 score, pass/fail (`score >= 5`), rationale, and a 4-sentence-max outreach message. The design follows existing project conventions (root-level script, argparse, stdout/stderr logging), with strict response validation and per-lead DB updates.

**Steps**
1. Add a new root script [score_and_message_profiles.py](score_and_message_profiles.py) with `argparse` options for `--db`, `--user-data-dir`, `--goals-file`, `--model`, `--threshold` (default `5`), `--max-retries`, and optional `--limit`.
2. Add DB selector logic that queries [leads.db](leads.db) for leads where `rating IS NULL` (and optionally `information_extracted = 1` to ensure cleaned data exists first).
3. For each selected row, resolve cleaned profile path as `user_data/{slug}/{slug}_cleaned_data.json` (from `--user-data-dir` root), load JSON safely, and skip/log if missing or invalid.
4. Define request builder that merges cleaned profile fields (`name`, `headline`, `experience`, `education`, `profile_url`) with goals-file text into one prompt and requires strict JSON response keys.
5. Integrate OpenAI client via env vars (`OPENAI_API_KEY`, optional `OPENAI_MODEL`) with timeout, exponential backoff, and per-lead error capture.
6. Add response validator: enforce numeric score range 0–10, enforce message sentence count `<= 4`, normalize pass/fail using threshold, and attach parse/validation warnings if repaired.
7. On success, update the corresponding lead row in [leads.db](leads.db): set `rating` and `connection_message` (and optional decision metadata if you choose an extra column later).
8. On failure, leave `rating` as `NULL` so the row remains eligible for retry, and log the failure reason clearly.
9. Add run instructions and env setup to [readme.md](readme.md), including an example invocation with goals text file and DB path.

**Verification**
- Create a tiny fixture in [leads.db](leads.db) with a few `rating = NULL` rows and matching cleaned files under [user_data](user_data).
- Confirm success path updates `rating` and `connection_message` in DB for processed rows.
- Confirm failure path sets `rating = -1`.
- Validate random successful samples: score in bounds, `decision` matches threshold, message is 4 sentences or fewer.
- Validate path resolution for cleaned files matches `user_data/{slug}/{slug}_cleaned_data.json` exactly.

**Decisions**
- Score model: single overall score (0–10).
- Data source/sink: DB-driven (`leads.db`), no queue popping from JSONL.
- Personal context input: text file path.
- LLM provider: OpenAI API.
