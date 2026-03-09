from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

from env_utils import load_env_file
from profile_parser import (
    extract_payload,
    extract_profile_from_payload,
    is_effectively_empty,
    load_json_safe,
    merge_profile_records,
)


load_env_file()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract merged profile records for scraped, unextracted leads from leads.db."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("leads.db"),
        help="Path to SQLite database file.",
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("user_data"),
        help="Directory containing person subfolders with raw_data JSON files.",
    )
    parser.add_argument(
        "--include-empty",
        action="store_true",
        help="Include folders even if no profile fields were extracted.",
    )
    return parser.parse_args()


def fallback_slug_from_url(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(r"linkedin\.com/in/([^/?#]+)", url, flags=re.IGNORECASE)
    return match.group(1).rstrip("/").lower() if match else None


def iter_json_files(raw_data_dir: Path) -> list[Path]:
    return sorted(path for path in raw_data_dir.iterdir() if path.is_file() and path.suffix.lower() == ".json")


def build_record_for_person(person_key: str, raw_data_dir: Path, expected_slug: str | None = None) -> tuple[dict[str, object], list[str]]:
    aggregate = {
        "name": None,
        "headline": None,
        "location": None,
        "current_company": None,
        "profile_url": None,
        "public_identifier": None,
        "experience": [],
        "education": [],
    }

    warnings: list[str] = []

    for json_file in iter_json_files(raw_data_dir):
        document, error = load_json_safe(json_file)
        if error:
            warnings.append(f"{person_key}/{json_file.name}: {error}")
            continue

        meta, payload = extract_payload(document)

        candidate = extract_profile_from_payload(payload, expected_slug=expected_slug or person_key, meta=meta)
        merge_profile_records(aggregate, candidate)

    record = {
        "person_key": person_key,
        "name": aggregate["name"],
        "headline": aggregate["headline"],
        "profile_url": aggregate["profile_url"],
        "public_identifier": aggregate["public_identifier"],
        "experience": aggregate["experience"],
        "education": aggregate["education"],
    }

    confidence_issues = _evaluate_record_confidence(record)
    for issue in confidence_issues:
        warnings.append(f"{person_key}: {issue}")

    return record, warnings


def _evaluate_record_confidence(record: dict[str, object]) -> list[str]:
    issues: list[str] = []

    if not record.get("public_identifier"):
        issues.append("missing_public_identifier")
    if not record.get("name"):
        issues.append("missing_name")
    if not record.get("headline"):
        issues.append("missing_headline")

    experience = record.get("experience")
    education = record.get("education")
    if not experience and not education:
        issues.append("missing_experience_and_education")

    return issues


def write_cleaned_json(person_dir: Path, person_key: str, record: dict[str, object]) -> Path:
    person_dir.mkdir(parents=True, exist_ok=True)
    output_path = person_dir / f"{person_key}_cleaned_data.json"
    output_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


def iter_pending_leads(conn: sqlite3.Connection) -> list[dict[str, str | None]]:
    rows = conn.execute(
        """
        SELECT linkedin_url, name, slug
        FROM leads
        WHERE scraped = 1 AND information_extracted = 0
        """
    ).fetchall()

    leads: list[dict[str, str | None]] = []
    for linkedin_url, name, slug in rows:
        leads.append(
            {
                "linkedin_url": (linkedin_url or "").strip() or None,
                "name": (name or "").strip() or None,
                "slug": (slug or "").strip().lower() or None,
            }
        )
    return leads


def mark_information_extracted(conn: sqlite3.Connection, linkedin_url: str) -> None:
    conn.execute(
        "UPDATE leads SET information_extracted = 1 WHERE linkedin_url = ?",
        (linkedin_url,),
    )
    conn.commit()


def main() -> int:
    args = parse_args()
    if not args.db.exists():
        print(f"Database not found: {args.db}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)

    try:
        pending_leads = iter_pending_leads(conn)
    except sqlite3.Error as error:
        conn.close()
        print(f"Failed to query leads: {error}", file=sys.stderr)
        return 1

    if not pending_leads:
        conn.close()
        print("No scraped + unextracted leads found in database.")
        return 0

    all_warnings: list[str] = []
    processed_count = 0
    records_written = 0

    for lead in pending_leads:
        linkedin_url = lead.get("linkedin_url")
        slug = lead.get("slug") or fallback_slug_from_url(lead.get("linkedin_url"))
        person_key = slug or (lead.get("name") or "unknown")
        person_dir = args.input_dir / person_key
        raw_data_dir = person_dir / "raw_data"

        if not raw_data_dir.exists() or not raw_data_dir.is_dir():
            all_warnings.append(f"{person_key}: missing_raw_data_folder")
            continue

        record, warnings = build_record_for_person(
            person_key=person_key,
            raw_data_dir=raw_data_dir,
            expected_slug=slug,
        )
        all_warnings.extend(warnings)

        if not args.include_empty and is_effectively_empty(record):
            if linkedin_url:
                try:
                    mark_information_extracted(conn, linkedin_url)
                    processed_count += 1
                except sqlite3.Error as error:
                    all_warnings.append(f"{person_key}: failed_mark_information_extracted ({error})")
            continue

        output_path = write_cleaned_json(person_dir=person_dir, person_key=person_key, record=record)
        records_written += 1
        print(f"Wrote cleaned data: {output_path}")

        if linkedin_url:
            try:
                mark_information_extracted(conn, linkedin_url)
                processed_count += 1
            except sqlite3.Error as error:
                all_warnings.append(f"{person_key}: failed_mark_information_extracted ({error})")

    conn.close()

    print(f"Pending leads scanned: {len(pending_leads)}")
    print(f"Leads marked extracted: {processed_count}")
    print(f"Cleaned files written: {records_written}")

    if all_warnings:
        print(f"Warnings: {len(all_warnings)}", file=sys.stderr)
        for warning in all_warnings[:25]:
            print(f"  - {warning}", file=sys.stderr)
        if len(all_warnings) > 25:
            print(f"  ... and {len(all_warnings) - 25} more", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
