r"""
LinkedIn Profile Scraper
========================
Connects to your real Chrome browser via CDP (Chrome DevTools Protocol),
searches LinkedIn for each person from the leads database, emulates human scrolling,
and captures profile-related API JSON responses.

Usage:
  1. Launch Chrome with remote debugging (macOS):
       /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222

  2. Or on Linux:
       google-chrome --remote-debugging-port=9222 --user-data-dir="$HOME/chrome-debug-profile"

  3. Make sure you're logged into LinkedIn in that browser.

    4. Run the scraper with cursor debug:
             SHOW_CURSOR=1 python3 enrichment.py --db leads.db

    The SQLite DB should contain a leads table with fields including
    name, linkedin_url, slug, and scraped.
    The scraper processes rows where scraped = 0 and marks each attempted
    row as scraped = 1 after the attempt completes.
"""

import argparse
import asyncio
import json
import os
import random
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from typing import Optional

from env_utils import load_env_file

load_env_file()

from playwright.async_api import async_playwright
from human_behavior import HumanBehavior, show_cursor, DEBUG_CURSOR


# ── constants ────────────────────────────────────────────────────────────
LINKEDIN_HOME = "https://www.linkedin.com/feed/"
CDP_ENDPOINT = os.getenv("CDP_ENDPOINT", os.getenv("CHROME_CDP_ENDPOINT", "http://127.0.0.1:9222"))

# Timing knobs (seconds) – tweak to taste
MIN_WAIT, MAX_WAIT = 2, 5          # between major actions
SCROLL_PAUSE_MIN, SCROLL_PAUSE_MAX = 0.8, 2.5  # increased pause time
SCROLLS_MIN, SCROLLS_MAX = 4, 7    # scroll movements per page
DEBUG_INTERCEPT = os.getenv("DEBUG_INTERCEPT", "0") == "1"
INTERCEPT_OUTPUT_ROOT = Path(os.getenv("INTERCEPT_OUTPUT_DIR", "user_data"))


# ── helpers ──────────────────────────────────────────────────────────────


async def human_delay(lo: float = MIN_WAIT, hi: float = MAX_WAIT) -> None:
    """Sleep a random amount to look human."""
    await asyncio.sleep(random.uniform(lo, hi))


async def random_scroll(page) -> None:
    """Scroll up and down randomly with natural behavior."""
    # Drift mouse naturally to the center of the page content so wheel events hit the scrollable area
    await HumanBehavior.natural_mouse_move(page, random.randint(400, 700), random.randint(350, 500))
    await asyncio.sleep(random.uniform(0.2, 0.4))

    num_scrolls = random.randint(SCROLLS_MIN, SCROLLS_MAX)
    print(f"    📜 Scrolling {num_scrolls} times...")
    for i in range(num_scrolls):
        direction = random.choices(["down", "up"], weights=[0.8, 0.2])[0]  # Mostly scroll down
        await HumanBehavior.smooth_scroll(page, direction)
        await asyncio.sleep(random.uniform(SCROLL_PAUSE_MIN, SCROLL_PAUSE_MAX))

        # Occasionally do idle mouse movement
        if i % 4 == 0 and random.random() > 0.5:
            await HumanBehavior.random_idle_movement(page)

    # Sometimes hover over elements at the end
    if random.random() > 0.4:
        await HumanBehavior.hover_and_interact(page, "a, button, [role='button']")
    print("    📜 Scrolling complete")


async def go_home_and_simulate_reading(page) -> None:
    """Navigate to LinkedIn home and simulate brief feed reading behavior."""
    await page.goto(LINKEDIN_HOME, wait_until="domcontentloaded")
    await human_delay(2, 4)
    print("    🏠 On home feed — simulating reading behavior…")
    await HumanBehavior.simulate_reading(page)
    await human_delay(1, 2)


def fallback_slug_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    m = re.search(r"linkedin\.com/in/([^/?#]+)", url, flags=re.IGNORECASE)
    return m.group(1).rstrip("/").lower() if m else None


def sanitize_for_filename(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", (value or "").strip())
    return cleaned.strip("_.")[:80] or "unknown"


# ── core logic ───────────────────────────────────────────────────────────

SEARCH_INPUT_SELECTOR = "input.search-global-typeahead__input"


async def use_search_bar(page, query: str) -> None:
    """Type a query into LinkedIn's own search bar and press Enter."""
    # Click the search input to focus it
    try:
        await page.wait_for_selector(SEARCH_INPUT_SELECTOR, timeout=10_000)
    except Exception:
        # Fallback: go to the feed so the search bar is present
        await go_home_and_simulate_reading(page)
        await page.wait_for_selector(SEARCH_INPUT_SELECTOR, timeout=10_000)

    search_box = page.locator(SEARCH_INPUT_SELECTOR)

    # Move mouse naturally to the search box before clicking
    box = await search_box.bounding_box()
    if box:
        target_x = int(box['x'] + box['width'] / 2 + random.randint(-10, 10))
        target_y = int(box['y'] + box['height'] / 2 + random.randint(-3, 3))
        await HumanBehavior.natural_mouse_move(page, target_x, target_y)
        await asyncio.sleep(random.uniform(0.1, 0.3))

    await search_box.click()
    await human_delay(0.3, 0.7)

    # Clear any existing text
    await page.keyboard.press("Meta+a")
    await asyncio.sleep(random.uniform(0.1, 0.3))
    await page.keyboard.press("Backspace")
    await asyncio.sleep(random.uniform(0.2, 0.5))

    # Type the query character by character
    for ch in query:
        await page.keyboard.type(ch, delay=random.randint(40, 170))
    await human_delay(0.5, 1.2)

    # Press Enter to search
    await page.keyboard.press("Enter")
    await page.wait_for_load_state("domcontentloaded")
    await human_delay(3, 6)

    # Click the "People" filter so results are scoped to profiles
    try:
        people_btn_selector = 'button:has-text("People")'
        people_btn = page.locator(people_btn_selector).first
        await people_btn.wait_for(timeout=6_000)
        await human_delay(0.3, 0.8)

        # Move mouse naturally to the button, then click
        box = await people_btn.bounding_box()
        if box:
            target_x = int(box['x'] + box['width'] / 2 + random.randint(-5, 5))
            target_y = int(box['y'] + box['height'] / 2 + random.randint(-3, 3))
            await HumanBehavior.natural_mouse_move(page, target_x, target_y)
            await asyncio.sleep(random.uniform(0.1, 0.3))
        await people_btn.click()

        await page.wait_for_load_state("domcontentloaded")
        await human_delay(2, 4)
    except Exception:
        pass  # might already be on People tab


async def click_matching_profile(page, slug: Optional[str]) -> bool:
    """Find and click a profile link in search results.

    If *slug* is given, look for the link whose href contains that slug.
    Otherwise fall back to clicking the first profile link.
    Returns True if a profile was successfully clicked.
    """
    try:
        await page.wait_for_selector('a[data-view-name="search-result-lockup-title"]', timeout=10_000)
    except Exception:
        return False

    links = page.locator('a[data-view-name="search-result-lockup-title"]')
    count = await links.count()

    if slug:
        slug_token = f"/in/{slug.lower()}"

        # Try to find the exact matching profile
        for idx in range(count):
            href = (await links.nth(idx).get_attribute("href") or "").lower()
            if (slug_token + "/") in href or (slug_token + "?") in href or href.rstrip("/").endswith(slug_token):
                await human_delay(0.5, 1.5)
                # Use natural click with mouse movement
                box = await links.nth(idx).bounding_box()
                if box:
                    target_x = int(box['x'] + box['width'] / 2 + random.randint(-10, 10))
                    target_y = int(box['y'] + box['height'] / 2 + random.randint(-10, 10))
                    await HumanBehavior.natural_mouse_move(page, target_x, target_y)
                    await asyncio.sleep(random.uniform(0.3, 0.7))
                await links.nth(idx).click()
                await page.wait_for_load_state("domcontentloaded")
                # Re-enable cursor after navigation
                if DEBUG_CURSOR:
                    await show_cursor(page)
                return True

        # Partial match (slug contained in href)
        for idx in range(count):
            href = (await links.nth(idx).get_attribute("href") or "").lower()
            if slug in href:
                await human_delay(0.5, 1.5)
                # Use natural click with mouse movement
                box = await links.nth(idx).bounding_box()
                if box:
                    target_x = int(box['x'] + box['width'] / 2 + random.randint(-10, 10))
                    target_y = int(box['y'] + box['height'] / 2 + random.randint(-10, 10))
                    await HumanBehavior.natural_mouse_move(page, target_x, target_y)
                    await asyncio.sleep(random.uniform(0.3, 0.7))
                await links.nth(idx).click()
                await page.wait_for_load_state("domcontentloaded")
                # Re-enable cursor after navigation
                if DEBUG_CURSOR:
                    await show_cursor(page)
                return True

    # Fallback: click the first result
    if count > 0:
        await human_delay(0.5, 1.5)
        # Use natural click with mouse movement
        box = await links.first.bounding_box()
        if box:
            target_x = int(box['x'] + box['width'] / 2 + random.randint(-10, 10))
            target_y = int(box['y'] + box['height'] / 2 + random.randint(-10, 10))
            await HumanBehavior.natural_mouse_move(page, target_x, target_y)
            await asyncio.sleep(random.uniform(0.3, 0.7))
        await links.first.click()
        await page.wait_for_load_state("domcontentloaded")
        # Re-enable cursor after navigation
        if DEBUG_CURSOR:
            await show_cursor(page)
        return True

    return False


def is_profile_api_response(url: str) -> bool:
    """Return True only for voyagerIdentityDashProfileCards GraphQL responses."""
    url_lower = unquote(url).lower()
    if "/voyager/api/graphql" not in url_lower:
        return False

    target_query = "voyageridentitydashprofilecards"

    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
    except Exception:
        params = {}

    query_id_values = params.get("queryId", []) + params.get("queryid", [])
    has_target_query_id = any(target_query in (value or "").lower() for value in query_id_values)

    # Fallback for unusual encoded formats where parse_qs misses the queryId signal.
    if not has_target_query_id and "queryid=" in url_lower:
        has_target_query_id = target_query in url_lower

    if not has_target_query_id:
        return False

    return True


def payload_looks_like_profile_data(payload: object, expected_slug: Optional[str] = None) -> bool:
    """Heuristic validation that a JSON payload contains LinkedIn profile data."""
    try:
        blob = json.dumps(payload, ensure_ascii=False).lower()
    except Exception:
        return False

    profile_markers = [
        "profileview",
        "voyager.identity.profile.profile",
        "voyager.identity.shared.miniprofile",
        "firstName".lower(),
        "lastName".lower(),
    ]

    if not any(marker in blob for marker in profile_markers):
        return False

    if expected_slug:
        return expected_slug.lower() in blob

    return True


async def intercept_profile_data(response, output_dir: Path, expected_slug: Optional[str] = None, capture_state: Optional[dict] = None):
    # LinkedIn's internal API endpoints use 'voyager'
    # We filter for profile data and ensure it's a successful response
    if response.status != 200:
        return

    if not is_profile_api_response(response.url):
        if DEBUG_INTERCEPT:
            print(f"    🧪 Intercept skip (url filter): {response.url}")
        return

    try:
        headers = await response.all_headers()
        content_type = (headers.get("content-type") or "").lower()
        if "json" not in content_type:
            if DEBUG_INTERCEPT:
                print(f"    🧪 Intercept skip (content-type): {response.url} [{content_type or 'unknown'}]")
            return
    except Exception:
        pass

    base_url = response.url.split("?")[0]

    try:
        # Extract the raw JSON payload
        raw_data = await response.json()

        looks_like_profile = payload_looks_like_profile_data(raw_data, expected_slug=expected_slug)
        if capture_state is not None:
            capture_state["count"] = int(capture_state.get("count", 0)) + 1
            sequence = capture_state["count"]
        else:
            sequence = int(time.time() * 1000)

        output_dir.mkdir(parents=True, exist_ok=True)
        file_name = f"{sequence:04d}_{'profile' if looks_like_profile else 'other'}_{int(time.time() * 1000)}.json"
        out_file = output_dir / file_name

        payload = {
            "meta": {
                "url": response.url,
                "base_url": base_url,
                "status": response.status,
                "captured_at": int(time.time()),
                "looks_like_profile": looks_like_profile,
                "expected_slug": expected_slug,
            },
            "data": raw_data,
        }

        out_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"    💾 Saved JSON: {out_file}")

    except Exception:
        # Handle cases where the response body isn't valid JSON (e.g., preflight requests)
        if DEBUG_INTERCEPT:
            print(f"    🧪 Intercept skip (json parse): {base_url}")

async def scrape_person(page, name: str, slug: Optional[str] = None) -> None:
    """Search LinkedIn for *name*, click the matching profile,
    then capture intercepted profile JSON responses."""
    slug = (slug or "").strip().lower() or None
    print(f"  → Searching for: {name}" + (f"  (slug: {slug})" if slug else ""))

    # ── use the in‑page search bar ──
    await use_search_bar(page, name)

    # Re-enable cursor after search page loads
    if DEBUG_CURSOR:
        await show_cursor(page)

    # Wait for the results container
    try:
        await page.wait_for_selector('[data-view-name="people-search-result"]', timeout=15_000)
    except Exception:
        print(f"    ⚠ Search results not found for '{name}'.")

    # ── human‑like scrolling on results ──
    await random_scroll(page)
    await human_delay(1, 3)

    # Only intercept during this person's profile navigation/extraction window
    person_key = sanitize_for_filename(slug or name)
    person_output_dir = INTERCEPT_OUTPUT_ROOT / person_key / "raw_data"
    capture_state = {"count": 0}

    profile_listener = lambda response: asyncio.create_task(
        intercept_profile_data(
            response,
            output_dir=person_output_dir,
            expected_slug=slug,
            capture_state=capture_state,
        )
    )
    page.on("response", profile_listener)

    try:
        # ── click into the matching profile ──
        clicked = await click_matching_profile(page, slug)
        if not clicked:
            print(f"    ⚠ Could not click into a profile for '{name}'. Skipping.")
            return

        # Guard: only extract on an actual LinkedIn profile URL
        try:
            await page.wait_for_url(re.compile(r"linkedin\.com/in/"), timeout=15_000)
        except Exception:
            print(f"    ⚠ Landed on non-profile page: {page.url}")
            return

        # Emergency fallback only when slug is unavailable from DB.
        if not slug:
            slug = fallback_slug_from_url(page.url)

        await human_delay(3, 6)

        # Simulate natural reading / scrolling so lazy sections and API calls load
        print("    📖 Scrolling through profile to load all sections…")
        await HumanBehavior.simulate_reading(page)
        await human_delay(1, 2)
    finally:
        page.remove_listener("response", profile_listener)
        print(f"    💾 JSON saved for this profile: {capture_state['count']} file(s) in {person_output_dir}")


def load_people_from_db(conn: sqlite3.Connection) -> list[dict]:
    people: list[dict] = []
    rows = conn.execute(
        """
        SELECT linkedin_url, name, slug
        FROM leads
        WHERE scraped = 0
        """
    ).fetchall()

    for row in rows:
        linkedin_url_val = (row[0] or "").strip()
        name_val = (row[1] or "").strip()
        slug_val = (row[2] or "").strip().lower()

        if not name_val:
            print(f"  ⚠ Skipping lead with missing name: {linkedin_url_val or '(no linkedin_url)'}")
            continue

        people.append({
            "name": name_val,
            "linkedin_url": linkedin_url_val or None,
            "slug": slug_val or None,
        })

    return people


def mark_scraped(conn: sqlite3.Connection, linkedin_url: str) -> None:
    conn.execute(
        "UPDATE leads SET scraped = 1 WHERE linkedin_url = ?",
        (linkedin_url,),
    )
    conn.commit()


async def main(db_path: str) -> None:
    conn = sqlite3.connect(db_path)

    try:
        people = load_people_from_db(conn)
    except sqlite3.Error as exc:
        conn.close()
        raise SystemExit(f"Failed to read leads from database: {exc}")

    if not people:
        conn.close()
        raise SystemExit("No unscraped leads found in the database.")

    print(f"Found {len(people)} person(s) to look up.")
    print(f"  (Source DB: {Path(db_path).resolve()})")
    print(f"  (JSON output directory: {INTERCEPT_OUTPUT_ROOT.resolve()})")
    print()

    # Connect to the already‑running Chrome instance
    async with async_playwright() as pw:
        print(f"Connecting to Chrome on {CDP_ENDPOINT} …")
        browser = await pw.chromium.connect_over_cdp(CDP_ENDPOINT)
        # Use the default (first) browser context – this is your real session
        context = browser.contexts[0]
        page = await context.new_page()

        # Start on LinkedIn feed so the search bar is available
        await go_home_and_simulate_reading(page)

        # Enable cursor visibility for debugging if requested
        if DEBUG_CURSOR:
            await show_cursor(page)

        for i, person in enumerate(people, 1):
            print(f"\n[{i}/{len(people)}]")
            attempted = False
            try:
                attempted = True
                await scrape_person(
                    page,
                    person["name"],
                    slug=person.get("slug"),
                )
            except Exception as exc:
                print(f"    ⚠ Scrape attempt failed for '{person['name']}': {exc}")
            finally:
                linkedin_url = person.get("linkedin_url")
                if attempted and linkedin_url:
                    try:
                        mark_scraped(conn, linkedin_url)
                        print(f"    ✅ Marked scraped in DB: {linkedin_url}")
                    except sqlite3.Error as exc:
                        print(f"    ⚠ Failed to mark scraped for '{person['name']}': {exc}")

            # Random pause between people
            if i < len(people):
                pause = random.uniform(5, 12)
                print(f"  … waiting {pause:.1f}s before next search")
                await asyncio.sleep(pause)

        await page.close()
        print("\nDone! Processed all profiles.")
        conn.close()


# ── entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape LinkedIn profiles from leads.db rows where scraped = 0.")
    parser.add_argument(
        "--db", "-d",
        default="leads.db",
        help="Path to SQLite DB file (default: leads.db).",
    )
    args = parser.parse_args()
    asyncio.run(main(args.db))
