import argparse
import asyncio
import os
import random
import re
import sqlite3
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from env_utils import load_env_file

load_env_file()

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

from human_behavior import HumanBehavior, show_cursor, DEBUG_CURSOR


GOOGLE_SEARCH_URL = "https://www.google.com/search"
GOOGLE_HOME_URL = "https://www.google.com/"
LINKEDIN_PROFILE_PATH_PREFIX = "/in/"
CDP_ENDPOINT = os.getenv("CDP_ENDPOINT", os.getenv("CHROME_CDP_ENDPOINT", "http://127.0.0.1:9222"))


def _parse_env_list(var_name: str) -> list[str]:
    raw = (os.getenv(var_name, "") or "").strip()
    if not raw:
        return []

    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1].strip()

    normalized = raw.replace("|", ",").replace(";", ",")
    parts = [part.strip() for part in re.split(r"[\n,]", normalized)]
    cleaned: list[str] = []
    for part in parts:
        token = part.strip().strip("[]").strip()
        if len(token) >= 2 and ((token[0] == '"' and token[-1] == '"') or (token[0] == "'" and token[-1] == "'")):
            token = token[1:-1].strip()
        if token:
            cleaned.append(token)
    return cleaned

SITE_SEARCH_TERMS = _parse_env_list("SITE_SEARCH_TERMS") or ["site:linkedin.com/in/"]

TECHNICAL_TERM_BANK = _parse_env_list("TECHNICAL_TERM_BANK")

COMPANY_BANK = _parse_env_list("COMPANY_BANK")
LANGUAGE_BANK = _parse_env_list("LANGUAGE_BANK")
CONSTANT_NEGATIVE_SEARCH_TERMS = _parse_env_list("CONSTANT_NEGATIVE_SEARCH_TERMS")
VARIABLE_NEGATIVE_SEARCH_TERMS = _parse_env_list("VARIABLE_NEGATIVE_SEARCH_TERMS")
LEGACY_NEGATIVE_SEARCH_TERMS = _parse_env_list("NEGATIVE_SEARCH_TERMS")


class CaptchaDetectedError(RuntimeError):
    pass


def init_db(db_path: str) -> sqlite3.Connection:
    db_file = Path(db_path)
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS leads (
            linkedin_url TEXT PRIMARY KEY,
            name TEXT,
            slug TEXT,
            scraped INTEGER NOT NULL DEFAULT 0,
            information_extracted INTEGER NOT NULL DEFAULT 0,
            rating INTEGER,
            connection_message TEXT,
            connection_requested INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    conn.commit()
    return conn


def build_random_query() -> str:
    if not COMPANY_BANK:
        raise ValueError("COMPANY_BANK is empty. Set COMPANY_BANK in .env.")
    if not TECHNICAL_TERM_BANK:
        raise ValueError("TECHNICAL_TERM_BANK is empty. Set TECHNICAL_TERM_BANK in .env.")
    if not LANGUAGE_BANK:
        raise ValueError("LANGUAGE_BANK is empty. Set LANGUAGE_BANK in .env.")

    site_term = random.choice(SITE_SEARCH_TERMS)
    company_term = random.choice(COMPANY_BANK)

    max_terms = min(5, len(TECHNICAL_TERM_BANK))
    min_terms = min(3, max_terms)
    num_terms = random.randint(min_terms, max_terms)
    selected_terms = random.sample(TECHNICAL_TERM_BANK, k=num_terms)

    max_languages = min(5, len(LANGUAGE_BANK))
    min_languages = min(3, max_languages)
    num_languages = random.randint(min_languages, max_languages)
    selected_languages = random.sample(LANGUAGE_BANK, k=num_languages)

    technical_or_group = "(" + " OR ".join(f'"{term}"' for term in selected_terms) + ")"
    language_or_group = "(" + " OR ".join(f'"{lang}"' for lang in selected_languages) + ")"

    variable_negative_terms: list[str] = []
    if VARIABLE_NEGATIVE_SEARCH_TERMS:
        count_negatives = 1
        variable_negative_terms = random.sample(VARIABLE_NEGATIVE_SEARCH_TERMS, k=count_negatives)

    all_negative_terms = [
        *CONSTANT_NEGATIVE_SEARCH_TERMS,
        *variable_negative_terms,
    ]

    if not all_negative_terms and LEGACY_NEGATIVE_SEARCH_TERMS:
        all_negative_terms = LEGACY_NEGATIVE_SEARCH_TERMS

    negative_terms_clause = " ".join(f'-"{term}"' for term in all_negative_terms)
    query = f"{site_term} \"{company_term}\" {technical_or_group} {language_or_group}"
    if negative_terms_clause:
        query = f"{query} {negative_terms_clause}"
    return query


def build_google_search_url(query: str, start: int) -> str:
    encoded_query = quote_plus(query)
    return f"{GOOGLE_SEARCH_URL}?q={encoded_query}&start={start}&num=10&hl=en"


def decode_google_result_href(href: str) -> Optional[str]:
    if not href:
        return None

    if href.startswith("/url?"):
        parsed = urlparse(href)
        q_param = parse_qs(parsed.query).get("q", [None])[0]
        return unquote(q_param) if q_param else None

    if href.startswith("http://") or href.startswith("https://"):
        return href

    return None


def canonicalize_linkedin_profile_url(raw_url: str) -> Optional[str]:
    try:
        parsed = urlparse(raw_url)
    except Exception:
        return None

    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host != "linkedin.com":
        return None

    path = parsed.path or ""
    if not path.startswith(LINKEDIN_PROFILE_PATH_PREFIX):
        return None

    path_parts = [segment for segment in path.split("/") if segment]
    if len(path_parts) < 2 or path_parts[0] != "in":
        return None

    slug = path_parts[1].strip().lower()
    if not slug:
        return None

    return f"https://www.linkedin.com/in/{slug}/"


def extract_slug(linkedin_url: str) -> Optional[str]:
    parsed = urlparse(linkedin_url)
    path_parts = [segment for segment in parsed.path.split("/") if segment]
    if len(path_parts) >= 2 and path_parts[0] == "in":
        return path_parts[1].strip().lower() or None
    return None


def derive_name_from_slug(slug: Optional[str]) -> Optional[str]:
    if not slug:
        return None
    words = [part for part in slug.replace("_", "-").split("-") if part]
    if not words:
        return None
    return " ".join(word.capitalize() for word in words)


def extract_name_from_google_headline(headline: Optional[str]) -> Optional[str]:
    if not headline:
        return None

    text = re.sub(r"\s+", " ", headline).strip()
    if not text:
        return None

    text = re.sub(r"\s*[|–—]\s*linkedin\b.*$", "", text, flags=re.IGNORECASE).strip()

    split_patterns = [r"\s+-\s+", r"\s+\|\s+", r"\s+–\s+", r"\s+—\s+"]
    for pattern in split_patterns:
        parts = re.split(pattern, text, maxsplit=1)
        if parts and parts[0].strip():
            text = parts[0].strip()
            break

    text = re.sub(r"\blinkedin\b", "", text, flags=re.IGNORECASE).strip(" -|–—")
    text = re.sub(r"\s+", " ", text).strip()

    if len(text) < 2:
        return None
    if sum(ch.isalpha() for ch in text) < 2:
        return None
    return text


def insert_lead_if_new(conn: sqlite3.Connection, linkedin_url: str, extracted_name: Optional[str] = None) -> bool:
    slug = extract_slug(linkedin_url)
    name = extract_name_from_google_headline(extracted_name) or derive_name_from_slug(slug)

    cur = conn.execute(
        """
        INSERT OR IGNORE INTO leads (
            linkedin_url,
            name,
            slug,
            scraped,
            rating,
            connection_message,
            connection_requested
        ) VALUES (?, ?, ?, 0, NULL, NULL, 0)
        """,
        (linkedin_url, name, slug),
    )
    conn.commit()
    return cur.rowcount > 0


async def human_pause(low: float = 2.0, high: float = 5.0) -> None:
    await asyncio.sleep(random.uniform(low, high))


async def go_google_home(page) -> None:
    await page.goto(GOOGLE_HOME_URL, wait_until="domcontentloaded")
    await human_pause(1.5, 3.0)
    await human_pause(1.0, 2.0)


async def safe_simulate_reading(page, stage: str) -> None:
    """Run simulate_reading with a retry when navigation destroys JS context."""
    try:
        await HumanBehavior.simulate_reading(page)
        return
    except Exception as exc:
        message = str(exc).lower()
        is_context_destroyed = "execution context was destroyed" in message
        if not is_context_destroyed:
            raise

        print(f"  ⚠ Reading interrupted during {stage}; waiting for page to stabilize and retrying once")

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=10_000)
    except Exception:
        pass

    await human_pause(0.8, 1.6)
    try:
        await HumanBehavior.simulate_reading(page)
    except Exception as retry_exc:
        retry_msg = str(retry_exc).lower()
        if "execution context was destroyed" in retry_msg:
            print(f"  ⚠ Reading skipped during {stage} after retry due to ongoing navigation")
            return
        raise


async def extract_profile_candidates_from_page(page) -> list[dict]:
    candidates: list[dict] = []
    seen_urls: set[str] = set()

    anchors = await page.eval_on_selector_all(
        "a[href]",
        """
        elements => elements.map(a => {
            const href = a.getAttribute('href') || '';
            const h3InAnchor = a.querySelector('h3');
            const h3Near = a.closest('div') ? a.closest('div').querySelector('h3') : null;
            const title = ((h3InAnchor && h3InAnchor.innerText) || (h3Near && h3Near.innerText) || '').trim();
            const text = (a.innerText || '').trim();
            return { href, title, text };
        })
        """,
    )

    for anchor in anchors:
        href = (anchor or {}).get("href") or ""
        decoded = decode_google_result_href(href)
        if not decoded:
            continue

        canonical = canonicalize_linkedin_profile_url(decoded)
        if not canonical or canonical in seen_urls:
            continue

        headline = (anchor or {}).get("title") or (anchor or {}).get("text") or ""
        extracted_name = extract_name_from_google_headline(headline)

        seen_urls.add(canonical)
        candidates.append({"linkedin_url": canonical, "name": extracted_name})

    return candidates


async def google_results_has_next_page(page) -> bool:
    selectors = (
        'a[aria-label="Next"]',
        'a[aria-label="Next page"]',
        'a#pnnext',
    )

    for selector in selectors:
        try:
            next_link = page.locator(selector).first
            if await next_link.is_visible(timeout=1_500):
                return True
        except Exception:
            continue

    return False


async def ensure_no_google_captcha(page, stage: str) -> None:
    current_url = (page.url or "").lower()
    if "google.com/sorry" in current_url or "recaptcha" in current_url:
        raise CaptchaDetectedError(
            f"Google CAPTCHA/challenge detected during {stage} (url: {page.url}). "
            "Stop the run, solve challenge manually, then retry later."
        )

    body_text = ""
    title_text = ""
    try:
        title_text = (await page.title() or "").lower()
    except Exception:
        pass

    try:
        body_text = (
            await page.evaluate("() => (document.body && document.body.innerText) ? document.body.innerText : ''")
            or ""
        ).lower()
    except Exception:
        pass

    signals = [
        "unusual traffic",
        "our systems have detected unusual traffic",
        "i'm not a robot",
        "not a robot",
        "solve this challenge",
        "recaptcha",
    ]
    joined = f"{title_text}\n{body_text}"
    if any(signal in joined for signal in signals):
        raise CaptchaDetectedError(
            f"Google CAPTCHA/challenge detected during {stage}. "
            "Stop the run, solve challenge manually, then retry later."
        )


async def collect_leads(limit: int, db_path: str, max_pages_per_query: int, cdp_endpoint: str) -> None:
    conn = init_db(db_path)
    remaining = limit
    total_inserted = 0

    print(f"Target new leads: {limit}")
    print(f"Using DB: {Path(db_path).resolve()}")

    async with async_playwright() as pw:
        print(f"Connecting to Chrome on {cdp_endpoint} …")
        browser = await pw.chromium.connect_over_cdp(cdp_endpoint)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await context.new_page()
        stealth = Stealth()
        await stealth.apply_stealth_async(page)

        if DEBUG_CURSOR:
            await show_cursor(page)

        await go_google_home(page)

        try:
            while remaining > 0:
                query = build_random_query()
                print(f"\n🔎 Query: {query}")
                start = 0
                pages_checked = 0

                while remaining > 0 and pages_checked < max_pages_per_query:
                    search_url = build_google_search_url(query=query, start=start)
                    print(f"  ↳ Opening results page (start={start})")

                    await page.goto(search_url, wait_until="domcontentloaded")
                    await ensure_no_google_captcha(page, stage=f"results navigation start={start}")
                    await human_pause(2.0, 4.0)
                    await safe_simulate_reading(page, stage=f"search-results start={start}")
                    await ensure_no_google_captcha(page, stage=f"results processing start={start}")
                    await human_pause(1.0, 2.5)

                    profile_candidates = await extract_profile_candidates_from_page(page)
                    print(f"  ↳ Extracted {len(profile_candidates)} LinkedIn profile candidate(s)")

                    new_on_page = 0
                    for candidate in profile_candidates:
                        if remaining <= 0:
                            break

                        link = candidate["linkedin_url"]
                        inserted = insert_lead_if_new(conn, link, extracted_name=candidate.get("name"))
                        if inserted:
                            remaining -= 1
                            total_inserted += 1
                            new_on_page += 1
                            display_name = candidate.get("name") or "(name fallback from slug)"
                            print(f"    ✅ Added: {display_name} | {link} (remaining target: {remaining})")
                        else:
                            print(f"    ⏭ Already exists: {link}")

                    pages_checked += 1
                    if remaining <= 0:
                        break

                    has_next_page = await google_results_has_next_page(page)
                    if not has_next_page:
                        print(
                            "  ↳ Fewer than the target number of Google result pages are available for this query; "
                            "switching to a new randomized query"
                        )
                        break

                    start += 10
                    print("  ↳ Page exhausted, moving pagination forward via &start=")
                    await human_pause(2.0, 5.0)

                if remaining > 0:
                    print("  ↳ Switching to a new randomized query")
                    await human_pause(3.0, 6.0)
        finally:
            await page.close()
            conn.close()

    print(f"\nDone. Inserted {total_inserted} new lead(s).")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover LinkedIn leads from Google and store unique URLs in leads.db"
    )
    parser.add_argument(
        "--limit",
        "-l",
        type=int,
        default=10,
        help="Number of new leads to collect (default: 10)",
    )
    parser.add_argument(
        "--db",
        "-d",
        default="leads.db",
        help="Path to SQLite database file (default: leads.db)",
    )
    parser.add_argument(
        "--cdp-endpoint",
        default=CDP_ENDPOINT,
        help="Chrome CDP endpoint (default: http://127.0.0.1:9222)",
    )
    parser.add_argument(
        "--max-pages-per-query",
        type=int,
        default=5,
        help="Maximum Google pages to scan per randomized query (default: 5)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        asyncio.run(
            collect_leads(
                limit=max(1, args.limit),
                db_path=args.db,
                max_pages_per_query=max(1, args.max_pages_per_query),
                cdp_endpoint=args.cdp_endpoint,
            )
        )
    except CaptchaDetectedError as exc:
        print(f"❌ {exc}")
        raise SystemExit(1)
