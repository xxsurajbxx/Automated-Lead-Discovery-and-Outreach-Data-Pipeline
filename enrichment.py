r"""
LinkedIn Profile Scraper
========================
Connects to your real Chrome browser via CDP (Chrome DevTools Protocol),
searches LinkedIn for each person in a CSV, emulates human scrolling,
and saves the full HTML of the profile page.

Usage:
  1. Launch Chrome with remote debugging (macOS):
       /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222

  2. Or on Linux:
       google-chrome --remote-debugging-port=9222 --user-data-dir="$HOME/chrome-debug-profile"

  3. Make sure you're logged into LinkedIn in that browser.

  4. Run the scraper with cursor debug:
       SHOW_CURSOR=1 python3 enrichment.py --input people.csv

  The CSV must have a column called "name" (case-insensitive).
  It may optionally have a "url" column with the LinkedIn profile URL.
  When a URL is provided the scraper uses LinkedIn's own search bar
  to find the person and then clicks the matching profile link —
  it never navigates directly to the URL.
"""

import argparse
import asyncio
import csv
import json
import os
import random
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from typing import Optional

from playwright.async_api import async_playwright
from human_behavior import HumanBehavior, show_cursor, DEBUG_CURSOR


# ── constants ────────────────────────────────────────────────────────────
LINKEDIN_HOME = "https://www.linkedin.com/feed/"
CDP_ENDPOINT = "http://127.0.0.1:9222"

# Timing knobs (seconds) – tweak to taste
MIN_WAIT, MAX_WAIT = 2, 5          # between major actions
SCROLL_PAUSE_MIN, SCROLL_PAUSE_MAX = 0.8, 2.5  # increased pause time
SCROLLS_MIN, SCROLLS_MAX = 4, 7    # scroll movements per page
DEBUG_INTERCEPT = os.getenv("DEBUG_INTERCEPT", "0") == "1"
INTERCEPT_OUTPUT_ROOT = Path(os.getenv("INTERCEPT_OUTPUT_DIR", "intercepted_json"))


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
        print(f"    📜 Scroll {i+1}/{num_scrolls}: {direction}")
        await HumanBehavior.smooth_scroll(page, direction)
        await asyncio.sleep(random.uniform(SCROLL_PAUSE_MIN, SCROLL_PAUSE_MAX))

        # Occasionally do idle mouse movement
        if i % 4 == 0 and random.random() > 0.5:
            await HumanBehavior.random_idle_movement(page)

    # Sometimes hover over elements at the end
    if random.random() > 0.4:
        await HumanBehavior.hover_and_interact(page, "a, button, [role='button']")
    print("    📜 Scrolling complete")


async def smooth_type(page, selector: str, text: str) -> None:
    """Type text character-by-character at a human pace."""
    await page.click(selector)
    await asyncio.sleep(random.uniform(0.2, 0.5))
    for ch in text:
        await page.keyboard.type(ch, delay=random.randint(40, 170))
    await asyncio.sleep(random.uniform(0.3, 0.8))


def extract_slug(url: str) -> Optional[str]:
    """Extract the vanity slug from a LinkedIn profile URL.

    e.g. 'https://www.linkedin.com/in/johndoe/' → 'johndoe'
    """
    m = re.search(r"linkedin\.com/in/([^/?#]+)", url)
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
        await page.goto(LINKEDIN_HOME, wait_until="domcontentloaded")
        await human_delay(2, 4)
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
        # Try to find the exact matching profile
        for idx in range(count):
            href = await links.nth(idx).get_attribute("href") or ""
            link_slug = extract_slug(href)
            if link_slug and link_slug == slug:
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

async def scrape_person(page, name: str, url: Optional[str] = None) -> None:
    """Search LinkedIn for *name*, click the matching profile,
    then capture intercepted profile JSON responses."""
    slug = extract_slug(url) if url else None
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
    person_output_dir = INTERCEPT_OUTPUT_ROOT / person_key
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

        # When slug wasn't provided by CSV URL, bind to the landed profile slug.
        if not slug:
            slug = extract_slug(page.url)

        await human_delay(3, 6)

        # Simulate natural reading / scrolling so lazy sections and API calls load
        print("    📖 Scrolling through profile to load all sections…")
        await HumanBehavior.simulate_reading(page)
        await human_delay(1, 2)
    finally:
        page.remove_listener("response", profile_listener)
        print(f"    💾 JSON saved for this profile: {capture_state['count']} file(s) in {person_output_dir}")


async def main(csv_path: str) -> None:
    # Read names (and optional URLs) from CSV
    people: list[dict] = []  # [{"name": ..., "url": ... | None}]
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        headers = {col.strip().lower(): col for col in (reader.fieldnames or [])}

        name_col = headers.get("name")
        if name_col is None:
            raise SystemExit("CSV must contain a column called 'name'.")

        # Accept "url", "linkedin_url", "profile_url", "link", etc.
        url_col = None
        for key in ("url", "linkedin_url", "profile_url", "link", "linkedin"):
            if key in headers:
                url_col = headers[key]
                break

        for row in reader:
            name_val = row[name_col].strip()
            url_val = row.get(url_col, "").strip() if url_col else None
            if name_val:
                people.append({"name": name_val, "url": url_val or None})

    if not people:
        raise SystemExit("No names found in the CSV.")

    print(f"Found {len(people)} person(s) to look up.")
    if url_col:
        print(f"  (URL column detected: '{url_col}')")
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
        await page.goto(LINKEDIN_HOME, wait_until="domcontentloaded")
        await human_delay(2, 4)

        # Enable cursor visibility for debugging if requested
        if DEBUG_CURSOR:
            await show_cursor(page)

        for i, person in enumerate(people, 1):
            print(f"\n[{i}/{len(people)}]")
            await scrape_person(page, person["name"], person["url"])
            # Random pause between people
            if i < len(people):
                pause = random.uniform(5, 12)
                print(f"  … waiting {pause:.1f}s before next search")
                await asyncio.sleep(pause)

        await page.close()
        print("\nDone! Processed all profiles.")


# ── entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape LinkedIn profiles from a CSV of names.")
    parser.add_argument(
        "--input", "-i",
        required=True,
        help="Path to CSV file with a 'name' column (and optional 'url' column).",
    )
    args = parser.parse_args()
    asyncio.run(main(args.input))
