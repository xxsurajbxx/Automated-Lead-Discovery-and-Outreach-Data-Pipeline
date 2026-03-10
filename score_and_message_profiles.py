from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from env_utils import load_env_file


load_env_file()


TEMPORARY_TEST_SINGLE_RUN_DEFAULT = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Score leads with OpenAI from cleaned user_data files and write rating/message back to leads.db"
    )
    parser.add_argument("--db", type=Path, default=Path("leads.db"), help="Path to SQLite database file")
    parser.add_argument(
        "--user-data-dir",
        type=Path,
        default=Path("user_data"),
        help="Root directory containing {slug}/{slug}_cleaned_data.json",
    )
    parser.add_argument(
        "--prompt-file",
        type=Path,
        default=Path("LLMPrompt.txt"),
        help="Path to text file containing your prompt (default: LLMPrompt.txt)",
    )
    parser.add_argument(
        "--model",
        default="gpt-5-mini",
        help="OpenAI model name (default: gpt-5-mini)",
    )
    parser.add_argument("--threshold", type=float, default=5.0, help="Pass threshold for score (default: 5.0)")
    parser.add_argument("--max-retries", type=int, default=3, help="Max retries per lead on API/parse failure")
    parser.add_argument("--limit", type=int, default=0, help="Max number of leads to process (0 means all)")
    parser.add_argument(
        "--require-extracted",
        action="store_true",
        help="Only process rows with information_extracted=1 (default: true)",
    )
    parser.add_argument(
        "--allow-unextracted",
        action="store_true",
        help="Process rows regardless of information_extracted flag",
    )
    parser.add_argument(
        "--disable-temp-single-run",
        action="store_true",
        help="Disable TEMPORARY test behavior and process all pending leads.",
    )
    parser.set_defaults(require_extracted=True)
    return parser.parse_args()


def load_prompt(prompt_path: Path) -> str:
    if not prompt_path.exists():
        raise FileNotFoundError(f"prompt file not found: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8").strip()


def fetch_pending_leads(conn: sqlite3.Connection, require_extracted: bool, limit: int) -> list[dict[str, Any]]:
    sql = """
    SELECT linkedin_url, slug, name
    FROM leads
    WHERE rating IS NULL
    """
    params: list[Any] = []

    if require_extracted:
        sql += " AND information_extracted = 1"

    sql += " ORDER BY rowid ASC"

    if limit > 0:
        sql += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(sql, tuple(params)).fetchall()
    return [
        {
            "linkedin_url": (row[0] or "").strip() or None,
            "slug": (row[1] or "").strip() or None,
            "name": (row[2] or "").strip() or None,
        }
        for row in rows
    ]


def load_cleaned_profile(user_data_dir: Path, slug: str) -> dict[str, Any]:
    path = user_data_dir / slug / f"{slug}_cleaned_data.json"
    if not path.exists():
        raise FileNotFoundError(f"Cleaned profile not found: {path}")

    with path.open("r", encoding="utf-8") as file_handle:
        data = json.load(file_handle)

    if not isinstance(data, dict):
        raise ValueError(f"Invalid cleaned profile JSON (not object): {path}")

    return data


def build_llm_prompt(profile: dict[str, Any], prompt_text: str, threshold: float) -> str:
    profile_payload = {
        "name": profile.get("name"),
        "headline": profile.get("headline"),
        "profile_url": profile.get("profile_url"),
        "experience": profile.get("experience", []),
        "education": profile.get("education", []),
    }

    return (
        f"{prompt_text}\n"
        f"""SCORING CONSTRAINTS
- 'score' MUST be an integer between 0 and 10.
- Score high-value leads >= {threshold}.
- Ensure messages are <= 4 sentences and personalized.
- If the score is >= {threshold}, the message MUST be high-effort and technical.
- If the score is < {threshold}, set 'message' to an empty string.\n"""
        "### CANDIDATE DATA\n"
        f"{json.dumps(profile_payload, separators=(',', ':'), ensure_ascii=False)}"
    )


def call_openai_json(model: str, prompt: str, timeout_seconds: int = 60) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    body = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            }
        ],
        "text": {"format": {"type": "json_object"}},
    }

    req = urlrequest.Request(
        url="https://api.openai.com/v1/responses",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urlrequest.urlopen(req, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8")
            envelope = json.loads(raw)
    except urlerror.HTTPError as err:
        details = err.read().decode("utf-8", errors="ignore") if hasattr(err, "read") else str(err)
        raise RuntimeError(f"OpenAI HTTP error {err.code}: {details}") from err
    except Exception as err:
        raise RuntimeError(f"OpenAI request failed: {err}") from err

    text = envelope.get("output_text")
    if not text:
        text = _extract_text_from_response_envelope(envelope)

    if not text:
        raise RuntimeError("OpenAI response missing text output")

    return _parse_json_from_text(text)


def _extract_text_from_response_envelope(envelope: dict[str, Any]) -> str | None:
    outputs = envelope.get("output")
    if not isinstance(outputs, list):
        return None

    chunks: list[str] = []
    for item in outputs:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                chunks.append(part["text"])
    return "\n".join(chunks).strip() or None


def _parse_json_from_text(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("No JSON object found in model output")

    parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("Model output JSON is not an object")
    return parsed


def validate_result(payload: dict[str, Any], threshold: float) -> dict[str, Any]:
    warnings: list[str] = []

    score = payload.get("score")
    if isinstance(score, str):
        try:
            score = float(score.strip())
            warnings.append("coerced_score_string_to_float")
        except Exception as err:
            raise ValueError(f"Invalid score string: {err}") from err

    if not isinstance(score, (int, float)):
        raise ValueError("score must be numeric")

    score = max(0.0, min(10.0, float(score)))

    rationale = payload.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ValueError("rationale must be non-empty string")
    rationale = rationale.strip()

    decision = "pass" if score >= threshold else "fail"

    raw_message = payload.get("message")
    if not isinstance(raw_message, str):
        raise ValueError("message must be a string")
    message = raw_message.strip()

    if decision == "pass":
        if not message:
            raise ValueError("message must be non-empty when score is passing")
        sentence_count = _sentence_count(message)
        if sentence_count > 4:
            raise ValueError(f"message has {sentence_count} sentences; must be <= 4")
    else:
        if message:
            warnings.append("non_empty_message_for_low_score")

    return {
        "score": score,
        "decision": decision,
        "rationale": rationale,
        "message": message,
        "warnings": warnings,
    }


def _sentence_count(text: str) -> int:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return len([part for part in parts if part.strip()])


def update_lead_success(conn: sqlite3.Connection, linkedin_url: str, score: float, message: str) -> None:
    conn.execute(
        """
        UPDATE leads
        SET rating = ?, connection_message = ?
        WHERE linkedin_url = ?
        """,
        (int(round(score)), message, linkedin_url),
    )
    conn.commit()


def score_single_lead(
    conn: sqlite3.Connection,
    lead: dict[str, Any],
    user_data_dir: Path,
    prompt_text: str,
    model: str,
    threshold: float,
    max_retries: int,
) -> tuple[bool, str]:
    linkedin_url = lead.get("linkedin_url")
    slug = lead.get("slug")

    if not linkedin_url:
        return False, "missing_linkedin_url"
    if not slug:
        return False, f"{linkedin_url}: missing_slug"

    try:
        profile = load_cleaned_profile(user_data_dir=user_data_dir, slug=slug)
    except Exception as err:
        return False, f"{linkedin_url}: cleaned_profile_error: {err}"

    prompt = build_llm_prompt(profile=profile, prompt_text=prompt_text, threshold=threshold)

    last_error: str | None = None
    for attempt in range(1, max_retries + 1):
        try:
            raw = call_openai_json(model=model, prompt=prompt)
            validated = validate_result(payload=raw, threshold=threshold)
            update_lead_success(
                conn=conn,
                linkedin_url=linkedin_url,
                score=validated["score"],
                message=validated["message"],
            )
            return True, (
                f"{linkedin_url}: score={validated['score']:.1f}, "
                f"decision={validated['decision']}, warnings={validated['warnings']}"
            )
        except Exception as err:
            last_error = str(err)
            if attempt < max_retries:
                backoff = min(10.0, 1.5 ** attempt)
                time.sleep(backoff)

    return False, f"{linkedin_url}: failed_after_retries: {last_error}"


def main() -> int:
    args = parse_args()

    require_extracted = args.require_extracted and not args.allow_unextracted
    temp_single_run_enabled = TEMPORARY_TEST_SINGLE_RUN_DEFAULT and not args.disable_temp_single_run

    if not args.db.exists():
        print(f"Database not found: {args.db}", file=sys.stderr)
        return 1

    try:
        prompt_text = load_prompt(args.prompt_file)
    except Exception as err:
        print(f"Failed to load prompt: {err}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)

    try:
        leads = fetch_pending_leads(conn, require_extracted=require_extracted, limit=max(0, args.limit))
    except sqlite3.Error as err:
        conn.close()
        print(f"Failed to query pending leads: {err}", file=sys.stderr)
        return 1

    if not leads:
        conn.close()
        print("No pending leads found (rating IS NULL).")
        return 0

    print(f"Pending leads: {len(leads)}")

    if temp_single_run_enabled:
        print("TEMPORARY TEST MODE ENABLED: listing all pending leads, then processing one lead and exiting.")
        for idx, lead in enumerate(leads, start=1):
            print(
                f"  [{idx}] slug={lead.get('slug') or 'N/A'} | "
                f"name={lead.get('name') or 'N/A'} | url={lead.get('linkedin_url') or 'N/A'}"
            )
        leads = leads[:1]
        selected = leads[0]
        print(
            "TEMPORARY TEST MODE SELECTED: "
            f"slug={selected.get('slug') or 'N/A'} | "
            f"name={selected.get('name') or 'N/A'} | "
            f"url={selected.get('linkedin_url') or 'N/A'}"
        )
        print("TEMPORARY TEST MODE: running LLM for selected lead only, then quitting.")

    success_count = 0
    failure_count = 0

    for idx, lead in enumerate(leads, start=1):
        ok, message = score_single_lead(
            conn=conn,
            lead=lead,
            user_data_dir=args.user_data_dir,
            prompt_text=prompt_text,
            model=args.model,
            threshold=args.threshold,
            max_retries=max(1, args.max_retries),
        )

        if ok:
            success_count += 1
            print(f"[{idx}/{len(leads)}] ✅ {message}")
        else:
            failure_count += 1
            print(f"[{idx}/{len(leads)}] ❌ {message}", file=sys.stderr)

    conn.close()

    print(f"Done. Success={success_count}, Failed={failure_count}")
    return 0 if success_count > 0 or failure_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
