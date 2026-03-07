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
DEBUG_EXTRACT = os.getenv("DEBUG_EXTRACT", "1") == "1"
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

# ── profile extraction ───────────────────────────────────────────────────
async def extract_profile_info(page) -> dict:
    """Extract key profile fields from the current LinkedIn profile page.

    Returns a dict with: name, headline, education, connections,
    mutual_connections, recent_activity, experience.
    """
    info = {
        "name": "",
        "headline": "",
        "education": [],
        "connections": "",
        "mutual_connections": "",
        "recent_activity": "",
        "experience": [],
    }

    def clean(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "")).strip()

    def is_noise_line(text: str) -> bool:
        return bool(re.match(r"^(Connect|Message|Follow|More|Open to|Save to PDF|Contact info|\d+(?:st|nd|rd)?)$", text, re.I))

    async def smooth_scroll_to_top(max_attempts: int = 14) -> None:
        """Use simulate_reading-style wheel scrolling with robust top detection."""
        if DEBUG_EXTRACT:
            print("    🔎 Extract debug: scrolling to top with HumanBehavior.smooth_scroll(up)")

        # Move mouse to center first so wheel events are routed to main content.
        viewport = page.viewport_size
        if not viewport:
            try:
                viewport = await page.evaluate("() => ({ width: window.innerWidth, height: window.innerHeight })")
            except Exception:
                viewport = {"width": 1280, "height": 720}

        center_x = int(viewport["width"] / 2 + random.randint(-80, 80))
        center_y = int(viewport["height"] / 2 + random.randint(-40, 40))
        await HumanBehavior.natural_mouse_move(page, center_x, center_y)
        await asyncio.sleep(random.uniform(0.15, 0.35))
        if DEBUG_EXTRACT:
            print(f"    🔎 Extract debug: mouse centered at ({center_x}, {center_y})")

        consecutive_top_checks = 0
        stagnant_checks = 0
        previous_doc_top = None
        previous_active_top = None
        min_attempts_before_break = 6

        for attempt in range(max_attempts):
            await HumanBehavior.smooth_scroll(page, "up")
            await asyncio.sleep(random.uniform(0.45, 0.9))

            try:
                top_probe = await page.evaluate(
                    """() => {
                        const doc = document.scrollingElement || document.documentElement || document.body;
                        const docTop = doc ? (doc.scrollTop || 0) : 0;
                        const cx = Math.floor(window.innerWidth / 2);
                        const cy = Math.floor(window.innerHeight / 2);

                        function findScrollable(el) {
                            while (el && el !== document.body && el !== document.documentElement) {
                                const s = getComputedStyle(el);
                                const canScroll = el.scrollHeight > el.clientHeight + 4;
                                const overflowY = s.overflowY;
                                if (canScroll && (overflowY === 'auto' || overflowY === 'scroll')) {
                                    return el;
                                }
                                el = el.parentElement;
                            }
                            return null;
                        }

                        const hit = document.elementFromPoint(cx, cy);
                        const active = findScrollable(hit);
                        const activeTop = active ? (active.scrollTop || 0) : null;
                        const h1 = document.querySelector('h1');
                        const h1Top = h1 ? h1.getBoundingClientRect().top : null;

                        const bodyLen = (document.body && document.body.innerText)
                            ? document.body.innerText.trim().length
                            : 0;

                        return {
                            docTop,
                            activeTop,
                            h1Top,
                            activeTag: active ? active.tagName : null,
                            bodyLen
                        };
                    }"""
                )
            except Exception:
                top_probe = {"docTop": None, "activeTop": None, "h1Top": None, "activeTag": None, "bodyLen": 0}

            doc_top = top_probe.get("docTop")
            active_top = top_probe.get("activeTop")
            h1_top = top_probe.get("h1Top")
            active_tag = top_probe.get("activeTag")
            body_len = top_probe.get("bodyLen") or 0

            # At top when document is top and active container (if any) is also top,
            # OR when h1 is visibly near the upper viewport.
            doc_is_top = (doc_top is not None and doc_top <= 5)
            active_is_top = (active_top is None or active_top <= 5)
            h1_near_top = (h1_top is not None and -10 <= h1_top <= 420)

            # IMPORTANT: reaching top is a scroll-state check, not a content check.
            # Content visibility (e.g., only "0 notifications") is handled separately.
            has_profile_signal = (h1_top is not None) or (body_len >= 200)
            is_at_top = (doc_is_top and active_is_top) or h1_near_top

            if previous_doc_top == doc_top and previous_active_top == active_top:
                stagnant_checks += 1
            else:
                stagnant_checks = 0
            previous_doc_top = doc_top
            previous_active_top = active_top

            if DEBUG_EXTRACT:
                print(
                    "    🔎 Extract debug: "
                    f"top-check attempt={attempt + 1} docTop={doc_top} activeTop={active_top} "
                    f"h1Top={h1_top} activeTag={active_tag} bodyLen={body_len} "
                    f"profileSignal={has_profile_signal} at_top={is_at_top} stagnant={stagnant_checks}"
                )

            if DEBUG_EXTRACT and is_at_top and not has_profile_signal:
                print("    🔎 Extract debug: at top but profile content signal is weak (likely overlay/toast-only DOM state)")

            if is_at_top:
                consecutive_top_checks += 1
            else:
                consecutive_top_checks = 0

            # Need minimum attempts + two consecutive confirmations to avoid false positives.
            if attempt + 1 >= min_attempts_before_break and consecutive_top_checks >= 2:
                break

            # If values are stuck, re-center cursor to avoid scrolling an overlay/sidebar.
            if stagnant_checks >= 3:
                center_x = int(viewport["width"] / 2 + random.randint(-120, 120))
                center_y = int(viewport["height"] / 2 + random.randint(-80, 80))
                await HumanBehavior.natural_mouse_move(page, center_x, center_y)
                await asyncio.sleep(random.uniform(0.2, 0.35))
                stagnant_checks = 0
                if DEBUG_EXTRACT:
                    print(f"    🔎 Extract debug: re-centered mouse at ({center_x}, {center_y}) due to stagnant scroll")

        # Deterministic fallback: force all known scroll containers to top.
        try:
            await page.evaluate(
                """() => {
                    const doc = document.scrollingElement || document.documentElement || document.body;
                    if (doc) doc.scrollTop = 0;
                    window.scrollTo(0, 0);

                    const all = document.querySelectorAll('*');
                    for (const el of all) {
                        if (el.scrollHeight > el.clientHeight + 4 && el.scrollTop > 0) {
                            el.scrollTop = 0;
                        }
                    }
                }"""
            )
            await asyncio.sleep(0.2)
            if DEBUG_EXTRACT:
                print("    🔎 Extract debug: applied force-scroll-top fallback")
        except Exception:
            pass

    async def extract_headline_at_top(name: str) -> str:
        # Force top-of-profile position first (user requested this behavior)
        try:
            await page.evaluate("""() => { window.scrollTo({ top: 0, behavior: 'instant' }); }""")
            await asyncio.sleep(0.8)
        except Exception:
            pass

        if DEBUG_EXTRACT:
            print("    🔎 Extract debug: attempting headline extraction at top of profile")

        selector_candidates = [
            "main .text-body-medium.break-words",
            "main .pv-text-details__left-panel .text-body-medium",
            "main .mt2 .text-body-medium",
            "section:has(h1) .text-body-medium",
            "xpath=//h1/ancestor::*[self::section or self::div][1]//*[contains(@class,'text-body-medium')]",
        ]

        for selector in selector_candidates:
            locator = page.locator(selector)
            try:
                if await locator.count() == 0:
                    continue

                texts = await locator.all_inner_texts()
                for text in texts:
                    candidate = clean(text)
                    if len(candidate) < 8 or len(candidate) > 180:
                        continue
                    if candidate == name:
                        continue
                    if is_noise_line(candidate):
                        continue
                    if re.search(r"\b(connections?|followers?|mutual|message|connect|contact info)\b", candidate, re.I):
                        continue
                    if DEBUG_EXTRACT:
                        print(f"    🔎 Extract debug: headline from selector -> {selector}")
                    return candidate
            except Exception:
                continue

        # Fallback: derive from lines around h1 in visible text
        try:
            top_text = await page.evaluate(
                """() => {
                    const h1 = document.querySelector('h1');
                    if (!h1) return '';
                    const card = h1.closest('section') || h1.parentElement;
                    return (card && card.innerText) ? card.innerText : (document.body?.innerText || '');
                }"""
            )
        except Exception:
            top_text = ""

        lines = [clean(line) for line in (top_text or "").splitlines() if clean(line)]
        if name and name in lines:
            idx = lines.index(name)
            for candidate in lines[idx + 1: idx + 10]:
                if len(candidate) < 8 or len(candidate) > 180:
                    continue
                if is_noise_line(candidate):
                    continue
                if re.search(r"\b(connections?|followers?|mutual|message|connect|contact info)\b", candidate, re.I):
                    continue
                if DEBUG_EXTRACT:
                    print("    🔎 Extract debug: headline from lines-near-h1 fallback")
                return candidate

        return ""

    def extract_section_lines(lines: list[str], heading: str, max_lines: int = 40) -> list[str]:
        heading_lower = heading.lower()
        stop_headings = {
            "about", "activity", "education", "experience", "skills", "interests",
            "licenses & certifications", "recommendations", "projects", "publications",
            "honors & awards", "volunteer experience", "languages", "organizations", "courses"
        }

        start_index = -1
        for i, line in enumerate(lines):
            normalized = line.lower().strip(" :")
            if normalized == heading_lower or normalized.startswith(f"{heading_lower} "):
                start_index = i
                break

        if start_index == -1:
            return []

        collected: list[str] = []
        for line in lines[start_index + 1:start_index + 1 + max_lines]:
            normalized = line.lower().strip(" :")
            if normalized in stop_headings and normalized != heading_lower:
                break
            if len(line) < 3:
                continue
            if line.lower().startswith("show all"):
                continue
            if is_noise_line(line):
                continue
            collected.append(line)

        # de-duplicate while preserving order
        deduped: list[str] = []
        seen = set()
        for item in collected:
            if item not in seen:
                seen.add(item)
                deduped.append(item)

        return deduped[:8]

    async def selector_items(selectors: list[str], limit: int = 8) -> list[str]:
        items: list[str] = []
        seen = set()
        for selector in selectors:
            locator = page.locator(selector)
            try:
                if await locator.count() == 0:
                    continue
                texts = await locator.all_inner_texts()
            except Exception:
                continue

            for text in texts:
                cleaned = clean(text)
                if len(cleaned) < 6:
                    continue
                if cleaned.lower().startswith("show all"):
                    continue
                if is_noise_line(cleaned):
                    continue
                if cleaned not in seen:
                    seen.add(cleaned)
                    items.append(cleaned)
                if len(items) >= limit:
                    return items

        return items

    # User-requested behavior: always scroll to top first via established smooth scroll
    await smooth_scroll_to_top()

    try:
        await page.wait_for_selector("h1", timeout=7000)
    except Exception:
        pass

    if DEBUG_EXTRACT:
        print(f"    🔎 Extract debug: URL={page.url}")
        try:
            print(f"    🔎 Extract debug: Title={await page.title()}")
        except Exception:
            print("    🔎 Extract debug: Title=<unavailable>")

    # Name from h1 is the most stable signal
    try:
        name_text = clean(await page.locator("h1").first.inner_text(timeout=1200))
        info["name"] = name_text
    except Exception:
        info["name"] = ""

    body_raw = ""
    try:
        body_raw = await page.locator("body").inner_text(timeout=3000)
    except Exception:
        body_raw = ""

    body_text = clean(body_raw)
    lines = [clean(line) for line in body_raw.splitlines() if clean(line)]

    if DEBUG_EXTRACT:
        print(f"    🔎 Extract debug: body_text_chars={len(body_text)} lines={len(lines)}")
        if lines:
            preview = " | ".join(lines[:10])
            print(f"    🔎 Extract debug: first_lines={preview[:400]}")

    # Headline extraction first: always scroll to top and extract there.
    info["headline"] = await extract_headline_at_top(info["name"])

    if not info["headline"]:
        # JSON-LD / meta fallback for headline
        try:
            ld_blocks = await page.evaluate(
                """() => Array.from(document.querySelectorAll('script[type="application/ld+json"]')).map(s => s.textContent || '')"""
            )
        except Exception:
            ld_blocks = []

        def parse_ld_objects(block: str):
            try:
                parsed = json.loads(block)
            except Exception:
                return []
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                if isinstance(parsed.get("@graph"), list):
                    return parsed["@graph"]
                return [parsed]
            return []

        for block in ld_blocks:
            for obj in parse_ld_objects(block):
                if not isinstance(obj, dict):
                    continue
                headline = clean(obj.get("headline", ""))
                description = clean(obj.get("description", ""))
                if headline and len(headline) > 6:
                    info["headline"] = headline
                    if DEBUG_EXTRACT:
                        print("    🔎 Extract debug: headline from JSON-LD headline")
                    break
                if description and len(description) > 6 and description != info.get("name"):
                    info["headline"] = description[:140]
                    if DEBUG_EXTRACT:
                        print("    🔎 Extract debug: headline from JSON-LD description")
                    break
            if info["headline"]:
                break

    if not info["headline"]:
        try:
            meta_description = await page.locator('meta[property="og:description"]').first.get_attribute("content")
            meta_description = clean(meta_description or "")
            if meta_description and len(meta_description) > 6:
                info["headline"] = meta_description[:140]
                if DEBUG_EXTRACT:
                    print("    🔎 Extract debug: headline from og:description")
        except Exception:
            pass

    # Connections / followers / mutuals
    connections_match = re.search(r"(\d[\d,]*\+?)\s+connections?\b", body_text, re.I)
    if connections_match:
        info["connections"] = connections_match.group(1)
    else:
        followers_match = re.search(r"(\d[\d,]*\+?)\s+followers?\b", body_text, re.I)
        if followers_match:
            info["connections"] = f"{followers_match.group(1)} followers"

    mutual_match = re.search(r"(\d[\d,]*)\s+mutual\s+connections?\b", body_text, re.I)
    if mutual_match:
        info["mutual_connections"] = mutual_match.group(1)

    if not info["connections"]:
        try:
            conn_candidates = await page.locator("text=/\\d[\\d,]*\\+?\\s+connections?/i").all_inner_texts()
            for candidate in conn_candidates:
                matched = re.search(r"(\d[\d,]*\+?)\s+connections?\b", clean(candidate), re.I)
                if matched:
                    info["connections"] = matched.group(1)
                    if DEBUG_EXTRACT:
                        print("    🔎 Extract debug: connections from text locator")
                    break
        except Exception:
            pass

    if not info["mutual_connections"]:
        try:
            mutual_candidates = await page.locator("text=/\\d[\\d,]*\\s+mutual\\s+connections?/i").all_inner_texts()
            for candidate in mutual_candidates:
                matched = re.search(r"(\d[\d,]*)\s+mutual\s+connections?\b", clean(candidate), re.I)
                if matched:
                    info["mutual_connections"] = matched.group(1)
                    if DEBUG_EXTRACT:
                        print("    🔎 Extract debug: mutual connections from text locator")
                    break
        except Exception:
            pass

    # Experience / education from heading blocks in visible text
    info["experience"] = await selector_items([
        "section:has(h2:has-text('Experience')) li",
        "section:has(h3:has-text('Experience')) li",
        "section[id*='experience' i] li",
    ])
    if not info["experience"]:
        info["experience"] = extract_section_lines(lines, "Experience")

    info["education"] = await selector_items([
        "section:has(h2:has-text('Education')) li",
        "section:has(h3:has-text('Education')) li",
        "section[id*='education' i] li",
    ])
    if not info["education"]:
        info["education"] = extract_section_lines(lines, "Education")

    # Recent activity date: prefer Activity block; fallback to whole page scan
    activity_lines = extract_section_lines(lines, "Activity", max_lines=25)
    activity_text = " ".join(activity_lines) if activity_lines else body_text
    activity_patterns = [
        r"(\d+\s*[mhdw]\s*ago)",
        r"(\d+\s*(?:minute|hour|day|week|month|year)s?\s*ago)",
        r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2}(?:,?\s*\d{4})?)",
    ]
    for pattern in activity_patterns:
        matched = re.search(pattern, activity_text, re.I)
        if matched:
            info["recent_activity"] = clean(matched.group(1))
            break

    if DEBUG_EXTRACT:
        print(
            "    🔎 Extract debug: "
            f"headline={'yes' if bool(info['headline']) else 'no'}, "
            f"connections={'yes' if bool(info['connections']) else 'no'}, "
            f"mutual={'yes' if bool(info['mutual_connections']) else 'no'}, "
            f"experience_items={len(info['experience'])}, "
            f"education_items={len(info['education'])}, "
            f"recent_activity={'yes' if bool(info['recent_activity']) else 'no'}"
        )

    return info


def print_profile_info(search_name: str, info: dict) -> None:
    """Pretty-print extracted profile data to the console."""
    sep = "═" * 60
    name = info.get('name') or search_name
    headline = info.get('headline', '')

    print(f"\n{sep}")
    print(f"  {name}")
    if headline:
        print(f"  {headline}")
    print(sep)

    conns = info.get('connections', '')
    mutual = info.get('mutual_connections', '')
    print(f"  Connections:       {conns or 'N/A'}")
    print(f"  Mutual conns:      {mutual or 'N/A'}")

    # Experience
    exp = info.get('experience', [])
    if exp:
        print(f"  Experience:")
        for entry in exp:
            for j, line in enumerate(entry.splitlines()):
                line = line.strip()
                if not line:
                    continue
                prefix = "    • " if j == 0 else "      "
                print(f"{prefix}{line}")
    else:
        print("  Experience:        N/A")

    # Education
    edu = info.get('education', [])
    if edu:
        print(f"  Education:")
        for entry in edu:
            for j, line in enumerate(entry.splitlines()):
                line = line.strip()
                if not line:
                    continue
                prefix = "    • " if j == 0 else "      "
                print(f"{prefix}{line}")
    else:
        print("  Education:         N/A")

    # Recent Activity
    activity = info.get('recent_activity', '')
    print(f"  Recent Activity:   {activity or 'N/A'}")

    print(sep)


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
    scroll to load lazy sections, and print key profile info."""
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

        # Simulate natural reading / scrolling so lazy sections load
        print("    📖 Scrolling through profile to load all sections…")
        await HumanBehavior.simulate_reading(page)
        await human_delay(1, 2)

        # ── extract & print profile data ──
        try:
            info = await extract_profile_info(page)
            print_profile_info(name, info)
        except Exception as exc:
            print(f"    ⚠ Extraction failed for '{name}': {exc}")
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
