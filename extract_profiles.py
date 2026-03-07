from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from profile_parser import (
    extract_payload,
    extract_profile_from_payload,
    is_effectively_empty,
    load_json_safe,
    merge_profile_records,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract merged profile records from intercepted LinkedIn JSON payloads."
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("intercepted_json"),
        help="Directory containing person subfolders with JSON files.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("profiles.jsonl"),
        help="Path for output JSONL file.",
    )
    parser.add_argument(
        "--include-empty",
        action="store_true",
        help="Include folders even if no profile fields were extracted.",
    )
    return parser.parse_args()


def iter_person_directories(root: Path) -> list[Path]:
    if not root.exists() or not root.is_dir():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir())


def iter_json_files(person_dir: Path) -> list[Path]:
    return sorted(path for path in person_dir.iterdir() if path.is_file() and path.suffix.lower() == ".json")


def build_record_for_person(person_dir: Path) -> tuple[dict[str, object], list[str]]:
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

    for json_file in iter_json_files(person_dir):
        document, error = load_json_safe(json_file)
        if error:
            warnings.append(f"{person_dir.name}/{json_file.name}: {error}")
            continue

        meta, payload = extract_payload(document)

        candidate = extract_profile_from_payload(payload, expected_slug=person_dir.name, meta=meta)
        merge_profile_records(aggregate, candidate)

    record = {
        "person_key": person_dir.name,
        "name": aggregate["name"],
        "headline": aggregate["headline"],
        "profile_url": aggregate["profile_url"],
        "public_identifier": aggregate["public_identifier"],
        "experience": aggregate["experience"],
        "education": aggregate["education"],
    }

    confidence_issues = _evaluate_record_confidence(record)
    for issue in confidence_issues:
        warnings.append(f"{person_dir.name}: {issue}")

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


def write_jsonl(output_path: Path, records: list[dict[str, object]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as file_handle:
        for record in records:
            file_handle.write(json.dumps(record, ensure_ascii=False, indent=2) + "\n\n")


def main() -> int:
    args = parse_args()
    person_dirs = iter_person_directories(args.input_dir)

    if not person_dirs:
        print(f"No person folders found under: {args.input_dir}", file=sys.stderr)
        return 1

    records: list[dict[str, object]] = []
    all_warnings: list[str] = []

    for person_dir in person_dirs:
        record, warnings = build_record_for_person(person_dir)
        all_warnings.extend(warnings)

        if not args.include_empty and is_effectively_empty(record):
            continue

        records.append(record)

    write_jsonl(args.output, records)

    print(f"Processed folders: {len(person_dirs)}")
    print(f"Records written: {len(records)}")
    print(f"Output: {args.output}")

    if all_warnings:
        print(f"Warnings: {len(all_warnings)}", file=sys.stderr)
        for warning in all_warnings[:25]:
            print(f"  - {warning}", file=sys.stderr)
        if len(all_warnings) > 25:
            print(f"  ... and {len(all_warnings) - 25} more", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
