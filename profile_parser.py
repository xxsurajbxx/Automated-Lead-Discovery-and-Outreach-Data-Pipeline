from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


LINKEDIN_PROFILE_RE = re.compile(r"https?://(?:www\.)?linkedin\.com/in/[A-Za-z0-9\-_%]+")
PROFILE_TOKEN_RE = re.compile(r"fsd_profile:([A-Za-z0-9\-_]+)")
PROFILE_CARD_RE = re.compile(r"fsd_profileCard:\(([^,]+),([A-Z_]+),")
NOISE_PREFIXES = ("com.linkedin.", "urn:li:")
NOISE_EXACT = {"string", "null", "none"}


def load_json_safe(file_path: Path) -> tuple[Any | None, str | None]:
    try:
        with file_path.open("r", encoding="utf-8") as file_handle:
            return json.load(file_handle), None
    except Exception as error:  # noqa: BLE001
        return None, f"invalid_json: {error}"


def extract_payload(document: Any) -> tuple[dict[str, Any], Any]:
    if isinstance(document, dict):
        meta = document.get("meta") if isinstance(document.get("meta"), dict) else {}
        payload = document.get("data", document)
        return meta, payload
    return {}, document


def merge_profile_records(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    for key in ("name", "headline", "location", "current_company", "profile_url", "public_identifier"):
        base[key] = _merge_scalar(base.get(key), incoming.get(key), key)

    base["experience"] = _merge_object_lists(base.get("experience", []), incoming.get("experience", []))
    base["education"] = _merge_object_lists(base.get("education", []), incoming.get("education", []))

    return base


def extract_profile_from_payload(
    payload: Any,
    expected_slug: str | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": None,
        "headline": None,
        "location": None,
        "current_company": None,
        "profile_url": None,
        "public_identifier": None,
        "experience": [],
        "education": [],
    }

    normalized_slug = (expected_slug or "").strip().lower() or None
    meta_token = _extract_profile_token_from_meta(meta or {})
    context = _find_target_context(payload, normalized_slug, meta_token)

    result["name"] = context.get("name")
    result["headline"] = context.get("headline")
    result["location"] = context.get("location")
    result["current_company"] = context.get("current_company")
    result["public_identifier"] = context.get("public_identifier")

    if context.get("public_identifier"):
        result["profile_url"] = f"https://www.linkedin.com/in/{context['public_identifier']}"
    elif normalized_slug:
        result["profile_url"] = f"https://www.linkedin.com/in/{normalized_slug}"

    target_token = context.get("profile_token")
    target_slug = context.get("public_identifier") or normalized_slug
    company_map = _build_company_name_map(payload)

    if not result["headline"]:
        result["headline"] = _extract_headline_fallback(payload, target_slug, target_token)

    result["experience"] = _extract_experience(payload, target_slug, target_token, company_map)
    result["education"] = _extract_education(payload, target_slug, target_token)

    current_from_present = _pick_present_company(result["experience"])
    result["current_company"] = _merge_scalar(result.get("current_company"), current_from_present, "current_company")

    role_company_headline = _headline_from_present_role(result.get("experience", []), result.get("headline"))
    if role_company_headline:
        result["headline"] = role_company_headline

    return result


def is_effectively_empty(record: dict[str, Any]) -> bool:
    scalar_keys = ["name", "headline", "location", "current_company", "profile_url", "public_identifier"]
    if any(record.get(key) for key in scalar_keys):
        return False
    if record.get("experience"):
        return False
    if record.get("education"):
        return False
    return True


def _find_target_context(payload: Any, expected_slug: str | None, meta_token: str | None) -> dict[str, str | None]:
    best: dict[str, str | None] = {
        "name": None,
        "headline": None,
        "location": None,
        "current_company": None,
        "public_identifier": expected_slug,
        "profile_token": meta_token,
    }
    best_score = -1

    for node in _iter_dict_nodes(payload):
        public_identifier = _clean_text(node.get("publicIdentifier"))
        if public_identifier:
            public_identifier = public_identifier.lower()

        profile_token = _extract_profile_token(node.get("entityUrn"))
        first_name = _clean_text(node.get("firstName"))
        last_name = _clean_text(node.get("lastName"))
        full_name = _normalize_profile_name(_clean_text(node.get("fullName")))
        if not full_name and (first_name or last_name):
            full_name = _normalize_profile_name(" ".join(part for part in [first_name, last_name] if part))

        headline = _extract_profile_headline(node)
        if headline and not _looks_like_identity_headline(headline):
            headline = None
        location = _clean_text(node.get("locationName"))

        score = 0
        if expected_slug and public_identifier == expected_slug:
            score += 100
        if meta_token and profile_token == meta_token:
            score += 60
        if full_name:
            score += 20
        if headline:
            score += 10

        if score <= best_score:
            continue

        best_score = score
        next_public_identifier = best.get("public_identifier")
        if public_identifier:
            if not expected_slug:
                next_public_identifier = public_identifier
            elif public_identifier == expected_slug:
                next_public_identifier = public_identifier
            elif not next_public_identifier:
                next_public_identifier = public_identifier

        best.update(
            {
                "name": full_name,
                "headline": headline,
                "location": location,
                "public_identifier": next_public_identifier,
                "profile_token": profile_token or best.get("profile_token"),
            }
        )

    return best


def _normalize_profile_name(value: str | None) -> str | None:
    if not value:
        return None

    normalized = re.sub(r"\s+", " ", value).strip()
    normalized = re.sub(r",\s*[A-Z](?:[A-Z]|\.){1,10}$", "", normalized).strip()
    normalized = re.sub(r"\s+", " ", normalized).strip(" ,")

    return normalized or None


def _extract_profile_headline(node: dict[str, Any]) -> str | None:
    direct_headline = _canonicalize_headline_candidate(_clean_text(node.get("headline")))
    if direct_headline:
        return direct_headline

    occupation = _canonicalize_headline_candidate(_clean_text(node.get("occupation")))
    if occupation:
        return occupation

    nested_headline = _canonicalize_headline_candidate(_clean_text(_extract_nested_text(node.get("headlineV2"))))
    if nested_headline:
        return nested_headline

    return None


def _canonicalize_headline_candidate(value: str | None) -> str | None:
    cleaned = _clean_text(value)
    if not cleaned:
        return None

    normalized = re.sub(r"\s+", " ", cleaned).strip()

    currently_match = re.search(
        r"\bcurrently\s+(?:an?|the)\s+([^.,;]+?)\s+at\s+([^.,;]+)",
        normalized,
        flags=re.IGNORECASE,
    )
    if currently_match:
        role = currently_match.group(1).strip()
        company = currently_match.group(2).strip()
        normalized = f"{role} at {company}"

    normalized = normalized.replace(" & ", " and ")
    normalized = re.sub(r"\s+", " ", normalized).strip(" .")

    return normalized or None


def _looks_like_identity_headline(value: str) -> bool:
    lowered = value.lower().strip()
    if lowered in {"technology, information and internet"}:
        return False
    if _is_skill_list_headline(value):
        return False
    return _looks_like_plausible_headline(value)


def _extract_experience(
    payload: Any,
    target_slug: str | None,
    target_token: str | None,
    company_map: dict[str, str],
) -> list[dict[str, str | None]]:
    entries: list[dict[str, str | None]] = []

    for node, ancestors in _iter_dict_nodes_with_ancestors(payload):
        if not _looks_like_entity_component(node):
            continue

        in_target_profile_card = _ancestors_in_target_card_sections(
            ancestors,
            target_token,
            {"EXPERIENCE"},
        )
        if not in_target_profile_card:
            continue

        title = _extract_nested_text(node.get("titleV2")) or _extract_nested_text(node.get("title"))
        subtitle = _extract_nested_text(node.get("subtitle"))
        caption = _extract_nested_text(node.get("caption"))

        parent_title = _clean_text(title)
        ancestor_company = _find_ancestor_company_title(ancestors)
        ancestor_date_range = _find_ancestor_date_range(ancestors)
        nested_role_title = _extract_nested_role_title(node)
        if nested_role_title and parent_title and nested_role_title != parent_title:
            title = nested_role_title
            if not subtitle:
                subtitle = parent_title

        text_action_target = _normalize_url(node.get("textActionTarget"))

        company = None
        if subtitle and " · " in subtitle:
            company = subtitle.split(" · ", 1)[0].strip()
        if not company:
            company = _extract_company_from_text(subtitle)
        if not company and parent_title and title and parent_title != title:
            company = parent_title
        if not company and ancestor_company and ancestor_company != _clean_text(title):
            company = ancestor_company
        if not company and text_action_target:
            company = company_map.get(text_action_target.lower())
        if not company:
            company = _extract_company_from_text(title)

        cleaned_title = _clean_text(title)
        if company and cleaned_title and company.lower() == cleaned_title.lower() and ancestor_company:
            if ancestor_company.lower() != cleaned_title.lower():
                company = ancestor_company

        description = _extract_nested_text(node.get("description"))

        record = {
            "title": _clean_text(title),
            "company": _clean_text(company),
            "summary": _clean_text(subtitle),
            "date_range": _clean_text(caption),
            "description": _clean_text(description),
        }

        if not record["date_range"] and _looks_like_real_date_range(record["summary"]):
            record["date_range"] = record["summary"]
        if not record["date_range"] and ancestor_date_range:
            record["date_range"] = ancestor_date_range

        if record["title"] and _is_generic_heading(record["title"]):
            continue

        if _is_low_quality_experience(record):
            continue

        if _is_noise_experience(record):
            continue

        if record["title"] or record["company"]:
            entries.append(record)

    return _dedupe_object_list(entries)


def _extract_education(payload: Any, target_slug: str | None, target_token: str | None) -> list[dict[str, str | None]]:
    entries: list[dict[str, str | None]] = []

    for node, ancestors in _iter_dict_nodes_with_ancestors(payload):
        if not _looks_like_entity_component(node):
            continue

        in_target_profile_card = _ancestors_in_target_card_sections(
            ancestors,
            target_token,
            {"EDUCATION"},
        )
        if not in_target_profile_card:
            continue

        school = _extract_nested_text(node.get("titleV2")) or _extract_nested_text(node.get("title"))
        subtitle = _extract_nested_text(node.get("subtitle"))
        caption = _extract_nested_text(node.get("caption"))

        record = {
            "school": _clean_text(school),
            "summary": _clean_text(subtitle),
            "date_range": _clean_text(caption),
        }

        if not record["school"] or _is_generic_heading(record["school"]):
            continue

        if _is_noise_education(record):
            continue

        has_school_signal = _looks_like_school(record["school"])
        has_degree_signal = _looks_like_degree(record["summary"])
        if not (has_school_signal or has_degree_signal):
            continue

        if record["school"]:
            entries.append(record)

    return _dedupe_object_list(entries)


def _pick_first_string(node: dict[str, Any], keys: list[str]) -> str | None:
    for key in keys:
        value = node.get(key)
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned:
                return cleaned
    return None


def _iter_dict_nodes(payload: Any):
    stack = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            yield current
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def _iter_dict_nodes_with_ancestors(payload: Any):
    stack: list[tuple[Any, list[dict[str, Any]]]] = [(payload, [])]
    while stack:
        current, ancestors = stack.pop()
        if isinstance(current, dict):
            yield current, ancestors
            child_ancestors = ancestors + [current]
            stack.extend((value, child_ancestors) for value in current.values())
        elif isinstance(current, list):
            stack.extend((item, ancestors) for item in current)


def _extract_date_string(raw_value: Any) -> str | None:
    if isinstance(raw_value, str):
        stripped = raw_value.strip()
        return stripped or None

    if isinstance(raw_value, dict):
        year = raw_value.get("year")
        month = raw_value.get("month")
        day = raw_value.get("day")
        date_parts = [str(value) for value in [year, month, day] if value is not None]
        if date_parts:
            return "-".join(date_parts)

    return None


def _merge_scalar(existing: str | None, incoming: str | None, key: str) -> str | None:
    if not incoming:
        return existing
    if not existing:
        return incoming

    if key == "headline":
        existing_score = _score_headline_value(existing)
        incoming_score = _score_headline_value(incoming)
        if incoming_score != existing_score:
            return incoming if incoming_score > existing_score else existing
        return incoming if len(incoming) > len(existing) else existing

    if key == "profile_url":
        if LINKEDIN_PROFILE_RE.search(incoming) and not LINKEDIN_PROFILE_RE.search(existing):
            return incoming

    return incoming if len(incoming) > len(existing) else existing


def _merge_object_lists(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _dedupe_object_list(existing + incoming)


def _extract_profile_token_from_meta(meta: dict[str, Any]) -> str | None:
    url = meta.get("url")
    if not isinstance(url, str):
        return None

    encoded_marker = "profileUrn:urn%3Ali%3Afsd_profile%3A"
    if encoded_marker in url:
        return url.split(encoded_marker, 1)[1].split(")", 1)[0].split("&", 1)[0]

    plain_match = re.search(r"profileUrn:urn:li:fsd_profile:([A-Za-z0-9\-_]+)", url)
    if plain_match:
        return plain_match.group(1)

    return None


def _extract_profile_token(raw_value: Any) -> str | None:
    if not isinstance(raw_value, str):
        return None
    match = PROFILE_TOKEN_RE.search(raw_value)
    return match.group(1) if match else None


def _extract_profile_card_info(raw_value: Any) -> tuple[str, str] | None:
    if not isinstance(raw_value, str):
        return None
    match = PROFILE_CARD_RE.search(raw_value)
    if not match:
        return None
    return match.group(1), match.group(2)


def _ancestors_in_target_card_sections(
    ancestors: list[dict[str, Any]],
    target_token: str | None,
    allowed_sections: set[str],
) -> bool:
    if not target_token:
        return False

    for ancestor in ancestors:
        entity_urn = ancestor.get("entityUrn")
        card_info = _extract_profile_card_info(entity_urn)
        if not card_info:
            continue
        profile_token, section = card_info
        if profile_token == target_token and section in allowed_sections:
            return True

    return False


def _containers_link_to_target(
    containers: list[dict[str, Any]],
    target_slug: str | None,
    target_token: str | None,
    section: str,
) -> bool:
    markers = {
        "position": (
            "fsd_profileposition:(",
            "add-edit/position",
            "entityurn=urn%3ali%3afsd_profileposition",
            "position_contextual_skills_see_details",
            ",experience,en_us)",
        ),
        "education": (
            "fsd_profileeducation:(",
            "add-edit/education",
            "entityurn=urn%3ali%3afsd_profileeducation",
            "education_contextual_skills_see_details",
            ",education,en_us)",
        ),
    }
    section_markers = markers[section]

    string_values: list[str] = []
    for container in containers[-24:]:
        string_values.extend(_collect_strings(container))

    if not string_values:
        return False

    has_section_marker = any(any(marker in value for marker in section_markers) for value in string_values)
    if not has_section_marker:
        return False

    if target_token and any(target_token.lower() in value for value in string_values):
        return True

    if target_slug and any(f"/in/{target_slug}" in value for value in string_values):
        return True

    return False


def _containers_reference_target(
    containers: list[dict[str, Any]],
    target_slug: str | None,
    target_token: str | None,
) -> bool:
    string_values: list[str] = []
    for container in containers[-24:]:
        string_values.extend(_collect_strings(container))

    if target_token and any(target_token.lower() in value for value in string_values):
        return True
    if target_slug and any(f"/in/{target_slug}" in value for value in string_values):
        return True
    return False


def _is_target_linked_org_role_node(
    node: dict[str, Any],
    ancestors: list[dict[str, Any]],
    target_slug: str | None,
    target_token: str | None,
) -> bool:
    text_action_target = _normalize_url(node.get("textActionTarget"))
    if not text_action_target or "/company/" not in text_action_target.lower():
        return False

    subtitle = _extract_nested_text(node.get("subtitle"))
    caption = _extract_nested_text(node.get("caption"))
    title = _extract_nested_text(node.get("titleV2")) or _extract_nested_text(node.get("title"))

    has_duration = _looks_like_real_date_range(subtitle) or _looks_like_real_date_range(caption)
    has_title = bool(_clean_text(title))
    if not (has_duration and has_title):
        return False

    if _containers_reference_target(ancestors + [node], target_slug, target_token):
        return True

    nested_role_title = _extract_nested_role_title(node)
    return bool(nested_role_title and _looks_like_role_title(nested_role_title))


def _collect_strings(value: Any) -> list[str]:
    found: list[str] = []
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            found.append(current.lower())
        elif isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return found


def _looks_like_entity_component(node: dict[str, Any]) -> bool:
    return any(key in node for key in ("titleV2", "title", "subtitle", "caption"))


def _extract_nested_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        text_value = value.get("text")
        if isinstance(text_value, str):
            return text_value
        if isinstance(text_value, dict):
            inner_text = text_value.get("text")
            if isinstance(inner_text, str):
                return inner_text
        accessibility = value.get("accessibilityText")
        if isinstance(accessibility, str):
            return accessibility
    return None


def _extract_headline_fallback(payload: Any, target_slug: str | None, target_token: str | None) -> str | None:
    candidates: list[tuple[int, str]] = []

    for node in _iter_dict_nodes(payload):
        node_public_identifier = _clean_text(node.get("publicIdentifier"))
        if node_public_identifier:
            node_public_identifier = node_public_identifier.lower()

        node_profile_token = _extract_profile_token(node.get("entityUrn"))

        if target_slug and node_public_identifier and node_public_identifier != target_slug:
            continue
        if target_token and node_profile_token and node_profile_token != target_token:
            continue

        link_score = 0
        if target_slug and node_public_identifier == target_slug:
            link_score += 12
        if target_token and node_profile_token == target_token:
            link_score += 12

        node_strings = _collect_strings(node)
        if target_slug and any(f"/in/{target_slug}" in value for value in node_strings):
            link_score += 6
        if target_token and any(target_token.lower() in value for value in node_strings):
            link_score += 6

        if link_score == 0:
            continue

        possible_headlines = [
            _clean_text(node.get("headline")),
            _clean_text(node.get("occupation")),
            _clean_text(_extract_nested_text(node.get("headlineV2"))),
            _clean_text(_extract_nested_text(node.get("subHeadline"))),
            _clean_text(_extract_nested_text(node.get("description"))),
            _clean_text(_extract_nested_text(node.get("text"))),
            _clean_text(_extract_nested_text(node.get("subtitle"))),
            _clean_text(_extract_nested_text(node.get("titleV2"))),
            _clean_text(_extract_nested_text(node.get("title"))),
        ]

        node_type = _clean_text(node.get("$type")) or ""
        is_actor_component = "actorcomponent" in node_type.lower()

        for raw_candidate in possible_headlines:
            was_currently_pattern = bool(
                raw_candidate
                and re.search(
                    r"\bcurrently\s+(?:an?|the)\s+[^.,;]+?\s+at\s+[^.,;]+",
                    raw_candidate,
                    flags=re.IGNORECASE,
                )
            )

            candidate = _canonicalize_headline_candidate(raw_candidate)
            if not _looks_like_plausible_headline(candidate):
                continue

            score = link_score
            lowered = candidate.lower()
            if "@" in candidate:
                score += 3
            if "|" in candidate:
                score += 2
            if " at " in lowered:
                score += 5
            if "prev" in lowered:
                score += 2
            if any(term in lowered for term in ("intern", "engineer", "student", "research", "software", "data", "cs")):
                score += 2

            if "intern" in lowered and " at " not in lowered and "@" not in candidate:
                score -= 8

            if was_currently_pattern:
                score += 12

            if is_actor_component and _clean_text(_extract_nested_text(node.get("description"))) == candidate:
                score += 14

            if _is_skill_list_headline(candidate):
                score -= 25

            candidates.append((score, candidate))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], len(item[1])), reverse=True)
    return candidates[0][1]


def _looks_like_plausible_headline(value: str | None) -> bool:
    if not value:
        return False

    cleaned = value.strip()
    if len(cleaned) < 8 or len(cleaned) > 180:
        return False

    lowered = cleaned.lower()
    if lowered.startswith(("http://", "https://")):
        return False
    if _is_noise_phrase(cleaned) or _is_generic_heading(cleaned):
        return False
    if _looks_like_real_date_range(cleaned):
        return False
    if re.search(r"\s·\s(?:full-time|part-time|internship|contract|temporary)\b", lowered):
        return False
    if any(token in lowered for token in ("people also viewed", "view profile", "message", "connect", "mutual connection", "endorsed by")):
        return False

    return bool(re.search(r"[a-z]", lowered))


def _is_skill_list_headline(value: str) -> bool:
    if " • " not in value:
        return False

    parts = [part.strip().lower() for part in value.split("•") if part.strip()]
    if len(parts) < 3:
        return False

    role_markers = (
        "@",
        "incoming",
        "ex-",
        "engineer",
        "intern",
        "student",
        "consultant",
        "manager",
    )
    if any(any(marker in part for marker in role_markers) for part in parts):
        return False

    skill_markers = (
        "python",
        "javascript",
        "typescript",
        "react",
        "java",
        "spring",
        "sql",
        "aws",
        "web",
        "database",
        "programming language",
    )
    matched_skills = sum(1 for part in parts if any(marker in part for marker in skill_markers))
    return matched_skills >= 2


def _extract_company_from_text(value: str | None) -> str | None:
    if not value:
        return None
    lowered = value.lower()
    if _looks_like_real_date_range(value):
        return None
    for bad_prefix in ("internship", "full-time", "part-time", "contract"):
        if lowered.startswith(bad_prefix):
            return None
    return value.split(" · ", 1)[0].strip()


def _find_ancestor_company_title(ancestors: list[dict[str, Any]]) -> str | None:
    for ancestor in reversed(ancestors):
        title = _extract_nested_text(ancestor.get("titleV2")) or _extract_nested_text(ancestor.get("title"))
        cleaned_title = _clean_text(title)
        if not cleaned_title:
            continue
        if _is_noise_phrase(cleaned_title) or _is_generic_heading(cleaned_title):
            continue
        if _looks_like_role_title(cleaned_title):
            continue
        if _looks_like_real_date_range(cleaned_title):
            continue
        return cleaned_title
    return None


def _find_ancestor_date_range(ancestors: list[dict[str, Any]]) -> str | None:
    for ancestor in reversed(ancestors):
        subtitle = _extract_nested_text(ancestor.get("subtitle"))
        caption = _extract_nested_text(ancestor.get("caption"))

        subtitle_clean = _clean_text(subtitle)
        caption_clean = _clean_text(caption)

        if subtitle_clean and _looks_like_real_date_range(subtitle_clean):
            return subtitle_clean
        if caption_clean and _looks_like_real_date_range(caption_clean):
            return caption_clean

    return None


def _extract_nested_role_title(node: dict[str, Any]) -> str | None:
    subcomponents = node.get("subComponents")
    if not isinstance(subcomponents, dict):
        return None

    components = subcomponents.get("components")
    if not isinstance(components, list):
        return None

    candidates: list[str] = []
    for component in components:
        if not isinstance(component, dict):
            continue
        nested_components = component.get("components")
        if not isinstance(nested_components, dict):
            continue

        nested_entity = nested_components.get("entityComponent")
        if isinstance(nested_entity, dict):
            nested_title = _extract_nested_text(nested_entity.get("titleV2")) or _extract_nested_text(nested_entity.get("title"))
            cleaned_nested_title = _clean_text(nested_title)
            if cleaned_nested_title and not _is_generic_heading(cleaned_nested_title):
                candidates.append(cleaned_nested_title)

    if not candidates:
        return None

    candidates.sort(key=len, reverse=True)
    return candidates[0]


def _build_company_name_map(payload: Any) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for node in _iter_dict_nodes(payload):
        if not isinstance(node, dict):
            continue
        text_action_target = node.get("textActionTarget")
        if not isinstance(text_action_target, str):
            continue

        cleaned_url = _normalize_url(text_action_target)
        if not cleaned_url:
            continue

        title = _extract_nested_text(node.get("titleV2")) or _extract_nested_text(node.get("title"))
        cleaned_title = _clean_text(title)
        if not cleaned_title:
            continue
        if _is_noise_phrase(cleaned_title) or _is_generic_heading(cleaned_title):
            continue

        lowered_url = cleaned_url.lower()
        if "/company/" in lowered_url:
            mapping[lowered_url] = cleaned_title

    return mapping


def _normalize_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned


def _pick_present_company(experience: list[dict[str, str | None]]) -> str | None:
    for entry in experience:
        date_range = (entry.get("date_range") or "").lower()
        if "present" in date_range:
            return entry.get("company")
    return None


def _headline_from_present_role(experience: list[dict[str, str | None]], current_headline: str | None) -> str | None:
    if not current_headline:
        return None

    cleaned_headline = _canonicalize_headline_candidate(current_headline)
    if not cleaned_headline:
        return None

    for entry in experience:
        date_range = (entry.get("date_range") or "").lower()
        if "present" not in date_range:
            continue

        title = _canonicalize_headline_candidate(entry.get("title"))
        company = _canonicalize_headline_candidate(entry.get("company"))
        if not title or not company:
            continue

        if cleaned_headline.lower() == title.lower() and " at " not in cleaned_headline.lower() and "@" not in cleaned_headline:
            combined = f"{title} at {company}"
            return _canonicalize_headline_candidate(combined)

    return None


def _looks_like_school(value: str | None) -> bool:
    if not value:
        return False
    lowered = value.lower()
    school_terms = ("university", "college", "institute", "school", "academy", "polytechnic")
    return any(term in lowered for term in school_terms)


def _looks_like_degree(value: str | None) -> bool:
    if not value:
        return False
    lowered = value.lower()

    degree_terms = (
        "bachelor",
        "master",
        "doctor",
        "phd",
        "mba",
        "associate",
        "diploma",
        "major",
        "minor",
    )
    if any(term in lowered for term in degree_terms):
        return True

    short_degree_pattern = re.compile(
        r"\b(b\.?\s?[asce]|m\.?\s?[asce]|ph\.?d\.?|m\.?b\.?a\.?)\b",
        re.IGNORECASE,
    )
    return bool(short_degree_pattern.search(value))


def _is_generic_heading(value: str) -> bool:
    lowered = value.strip().lower()
    generic = {
        "experience",
        "education",
        "projects",
        "featured",
        "highlights",
        "courses",
        "test scores",
        "certificate",
        "code",
        "volunteering",
        "honors & awards",
        "licenses & certifications",
    }
    return lowered in generic


def _is_low_quality_experience(record: dict[str, str | None]) -> bool:
    title = (record.get("title") or "").strip()
    company = (record.get("company") or "").strip()
    summary = (record.get("summary") or "").strip()

    employment_types = {"part-time", "full-time", "internship", "contract", "temporary"}
    duration_like = re.compile(r"^\d+\s+(?:yr|yrs|year|years|mo|mos|month|months)\b", re.IGNORECASE)

    if company.lower() in employment_types:
        return True
    if summary.lower() in employment_types:
        return True
    if duration_like.match(company) or duration_like.match(summary):
        return True

    if _looks_like_school(title) and (company.lower() in employment_types or duration_like.match(summary or "")):
        return True

    return False


def _is_noise_phrase(value: str) -> bool:
    lowered = value.lower().strip()
    noisy_prefixes = (
        "you both ",
        "birthdays are ",
        "associated with ",
        "view profile",
        "top skills",
    )
    if lowered.startswith(noisy_prefixes):
        return True

    noisy_contains = (
        "before you started",
        "after you started",
        "great opportunity",
        "native or bilingual proficiency",
        "full professional proficiency",
    )
    return any(token in lowered for token in noisy_contains)


def _looks_like_real_date_range(value: str | None) -> bool:
    if not value:
        return False
    lowered = value.lower()
    if "native" in lowered or "proficiency" in lowered or "score:" in lowered:
        return False
    date_signal = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|\b\d{4}\b)", lowered)
    range_signal = ("-" in lowered) or (" to " in lowered) or ("present" in lowered)
    duration_signal = re.search(r"\b\d+\s*(yr|yrs|year|years|mo|mos|month|months)\b", lowered)
    if duration_signal and not ("native" in lowered or "proficiency" in lowered):
        return True
    return bool(date_signal and (range_signal or duration_signal))


def _is_noise_experience(record: dict[str, str | None]) -> bool:
    title = (record.get("title") or "").strip()
    company = (record.get("company") or "").strip()
    summary = (record.get("summary") or "").strip()
    date_range = (record.get("date_range") or "").strip()

    if not title:
        return True
    if _is_noise_phrase(title) or _is_noise_phrase(company) or _is_noise_phrase(summary):
        return True

    if not _looks_like_real_date_range(date_range):
        return True

    blocked_titles = {
        "about",
        "languages",
        "organizations",
        "courses",
        "projects",
        "featured",
        "highlights",
        "top skills",
        "test scores",
        "certificate",
        "honors & awards",
        "licenses & certifications",
        "volunteer",
    }
    if title.lower() in blocked_titles:
        return True

    if title.lower() in {"commit post", "summer recap"}:
        return True

    if company and title.lower() == company.lower() and any(token in title.lower() for token in (" post", "recap", " recap")):
        return True

    lowered_title = title.lower()
    if any(token in lowered_title for token in ("github", "project", "proposal", "term project", "dean", "award")):
        return True

    lowered_company = company.lower()
    if any(token in lowered_company for token in ("issued by", "score:", "native or bilingual", "full professional")):
        return True

    if title.lower().endswith(".pdf"):
        return True

    if _looks_like_school(title) and (_looks_like_degree(summary) or _looks_like_degree(company)):
        return True

    return False


def _is_noise_education(record: dict[str, str | None]) -> bool:
    school = (record.get("school") or "").strip()
    summary = (record.get("summary") or "").strip()
    date_range = (record.get("date_range") or "").strip()

    if not school:
        return True
    if _is_noise_phrase(school) or _is_noise_phrase(summary):
        return True

    lowered_school = school.lower()
    if any(token in lowered_school for token in ("dean", "award", "certificate", "issued by", "honors & awards")):
        return True

    if summary and re.fullmatch(r"\d+\s*(yr|yrs|year|years|mo|mos|month|months)", summary.lower()):
        return True

    if summary and re.fullmatch(r"\d+\s*(yr|yrs|year|years|mo|mos|month|months)(\s+\d+\s*(mo|mos|month|months))?", summary.lower()):
        return True

    has_signal = _looks_like_school(school) or _looks_like_degree(summary) or _looks_like_real_date_range(date_range)
    return not has_signal


def _looks_like_role_title(value: str | None) -> bool:
    if not value:
        return False
    lowered = value.lower()
    role_terms = (
        "engineer",
        "intern",
        "researcher",
        "tutor",
        "grader",
        "analyst",
        "developer",
        "manager",
        "assistant",
        "chair",
        "teacher",
        "instructor",
        "consultant",
        "lead",
        "specialist",
        "co-op",
    )
    return any(term in lowered for term in role_terms)


def _score_headline_value(value: str | None) -> int:
    if not value:
        return -100

    cleaned = value.strip()
    lowered = cleaned.lower()

    score = 0
    if _looks_like_identity_headline(cleaned):
        score += 25
    if _is_skill_list_headline(cleaned):
        score -= 30

    if "@" in cleaned:
        score += 8
    if "|" in cleaned:
        score += 6
    if "incoming" in lowered:
        score += 8
    if "ex-" in lowered or "prev" in lowered:
        score += 5

    if lowered in {"technology, information and internet"}:
        score -= 15

    if any(term in lowered for term in ("engineer", "intern", "student", "research", "developer", "software")):
        score += 3

    return score


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None

    cleaned = value.strip()
    if not cleaned:
        return None

    lowered = cleaned.lower()
    if lowered in NOISE_EXACT:
        return None
    if lowered.startswith(NOISE_PREFIXES):
        return None
    if lowered.startswith("http") and "linkedin.com/in/" not in lowered:
        return None

    return cleaned


def _dedupe_object_list(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique_entries: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    for entry in entries:
        normalized_entry = {key: value for key, value in entry.items() if value not in (None, "", [])}
        if not normalized_entry:
            continue

        dedupe_key = "|".join(f"{key}:{normalized_entry[key]}" for key in sorted(normalized_entry.keys()))
        dedupe_key = dedupe_key.lower()

        if dedupe_key in seen_keys:
            continue

        seen_keys.add(dedupe_key)
        unique_entries.append(normalized_entry)

    return unique_entries
