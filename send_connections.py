r"""
LinkedIn Connection Sender
===========================
Connects to your real Chrome browser via CDP (Chrome DevTools Protocol),
searches LinkedIn for each high-scoring lead (same search-bar flow as
scrape_linkedin.py), navigates to their profile, and sends a personalised
connection request using the message stored in leads.db.

Usage:
  1. Launch Chrome with remote debugging (macOS):
       /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222

  2. Make sure you're logged into LinkedIn in that browser.

  3. Run:
       python3 send_connections.py --db leads.db --threshold 5.0

  Leads with rating >= threshold and connection_requested = 0 are processed.
    connection_requested = 1  → invitation sent successfully
    connection_requested = -1 → already connected / pending / no connect path found
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import re
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

from env_utils import load_env_file
from linkedin_common import (
    click_matching_profile,
    dismiss_blocking_linkedin_popup,
    go_home_and_simulate_reading,
    human_delay,
    random_scroll,
    use_search_bar,
)

load_env_file()

from playwright.async_api import async_playwright
from human_behavior import HumanBehavior, show_cursor, DEBUG_CURSOR


# ── constants ─────────────────────────────────────────────────────────────────
CDP_ENDPOINT = os.getenv("CDP_ENDPOINT", os.getenv("CHROME_CDP_ENDPOINT", "http://127.0.0.1:9222"))

MIN_WAIT, MAX_WAIT = 2, 5

RANDOM_RUN_MIN_PROFILES = 2
RANDOM_RUN_MAX_PROFILES = 4

CONNECTION_SUCCESS = 1
CONNECTION_SKIP = -1


# ── helpers (mirrored from scrape_linkedin.py) ────────────────────────────────


# ── connection-specific logic ─────────────────────────────────────────────────

async def _click_naturally(page, locator) -> None:
    """Move mouse naturally to a locator's center, then click. Falls back to force click."""
    try:
        box = await locator.bounding_box()
    except Exception:
        box = None
    if box:
        tx = int(box["x"] + box["width"] / 2 + random.randint(-5, 5))
        ty = int(box["y"] + box["height"] / 2 + random.randint(-3, 3))
        await HumanBehavior.natural_mouse_move(page, tx, ty)
        await asyncio.sleep(random.uniform(0.2, 0.4))

    # Always try normal click first; if intercepted, fall back to force/JS click.
    try:
        await locator.click()
    except Exception:
        try:
            await locator.click(force=True)
        except Exception:
            try:
                await locator.evaluate("el => el.click()")
            except Exception:
                pass


async def _collect_visible_primary_action_labels(page) -> list[str]:
    labels: list[str] = []
    selectors = (
        "main section button",
        "main section a[role='button']",
        "main button",
        "main a[role='button']",
    )

    for selector in selectors:
        try:
            candidates = page.locator(selector)
            count = await candidates.count()
            for idx in range(min(count, 20)):
                node = candidates.nth(idx)
                if not await node.is_visible():
                    continue
                text = (await node.inner_text() or "").strip()
                aria = (await node.get_attribute("aria-label") or "").strip()
                label = text or aria
                if label and label not in labels:
                    labels.append(label)
            if labels:
                break
        except Exception:
            continue

    return labels


async def _debug_profile_action_snapshot(page) -> None:
    try:
        title = await page.title()
    except Exception:
        title = "<unavailable>"

    try:
        ready_state = await page.evaluate("document.readyState")
    except Exception:
        ready_state = "<unknown>"

    print(f"    🧪 Snapshot: url={page.url}")
    print(f"    🧪 Snapshot: title={title!r} readyState={ready_state}")

    selector_checks = (
        "main",
        "main button",
        "main [role='button']",
        "button",
        "[role='button']",
        "button[aria-label*='Invite' i][aria-label*='to connect' i]",
        "button:has(use[href='#connect-small'])",
        "[role='button'][aria-label*='Invite' i][aria-label*='connect' i]",
        "button[aria-label*='More actions' i]",
        "button[id*='profile-overflow-action']",
        "button:has(use[href*='overflow'])",
        "[role='button']:has(use[href*='overflow'])",
        "button:has(use[xlink\\:href*='overflow'])",
        "[role='button']:has(use[xlink\\:href*='overflow'])",
    )

    for selector in selector_checks:
        try:
            count = await page.locator(selector).count()
            print(f"    🧪 Count({selector})={count}")
        except Exception:
            print(f"    🧪 Count({selector})=<error>")

    visible_labels: list[str] = []
    seen: set[str] = set()
    candidates = page.locator("button, [role='button']")
    try:
        total = await candidates.count()
    except Exception:
        total = 0

    for idx in range(min(total, 80)):
        try:
            node = candidates.nth(idx)
            if not await node.is_visible(timeout=250):
                continue
            text = (await node.inner_text() or "").strip()
            aria = (await node.get_attribute("aria-label") or "").strip()
            label = text or aria
            if not label:
                continue
            normalized = " ".join(label.split())
            if normalized in seen:
                continue
            seen.add(normalized)
            visible_labels.append(normalized)
            if len(visible_labels) >= 15:
                break
        except Exception:
            continue

    print(f"    🧪 Visible button labels sample: {visible_labels if visible_labels else 'none_detected'}")

    # Dump ALL button labels regardless of visibility — to see what buttons are in the DOM.
    try:
        all_labels = await page.evaluate("""() => {
            const els = [...document.querySelectorAll('button, [role="button"]')];
            const seen = new Set();
            const out = [];
            for (const el of els) {
                const label = (el.getAttribute('aria-label') || el.innerText || '').replace(/\\s+/g, ' ').trim();
                if (!label || seen.has(label)) continue;
                seen.add(label);
                out.push(label);
                if (out.length >= 30) break;
            }
            return out;
        }""")
        print(f"    🧪 ALL button labels (DOM, no visibility filter): {all_labels if all_labels else 'none_found'}")
    except Exception as e:
        print(f"    🧪 ALL button labels eval error: {e}")


async def _scroll_profile_to_top(page) -> None:
    try:
        await page.evaluate("""() => {
            window.scrollTo(0, 0);
            const selectors = [
              '.scaffold-layout__main',
              'main',
              '.application-outlet',
              '.scaffold-layout',
              '.scaffold-layout-container'
            ];
            for (const sel of selectors) {
              for (const el of document.querySelectorAll(sel)) {
                try { el.scrollTop = 0; } catch {}
              }
            }
            for (const el of document.querySelectorAll('*')) {
              try {
                if (el.scrollHeight > el.clientHeight + 20) el.scrollTop = 0;
              } catch {}
            }
        }""")
        await page.keyboard.press("Home")
        await asyncio.sleep(0.15)
        await page.keyboard.press("Home")
    except Exception:
        pass
    await asyncio.sleep(random.uniform(0.3, 0.7))


async def _profile_header_cta_count(page) -> int:
    selectors = (
        "button[aria-label='More actions'][id$='-profile-overflow-action']",
        "button[id*='profile-overflow-action']",
        "button[aria-label*='Invite' i][aria-label*='to connect' i]",
        "button:has(use[href='#connect-small'])",
    )
    total = 0
    for sel in selectors:
        try:
            total += await page.locator(sel).count()
        except Exception:
            continue
    return total


async def _stabilize_profile_page(page) -> None:
    # LinkedIn SPA sometimes leaves search/feed overlays active after profile navigation.
    for attempt in range(2):
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.25)
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.25)
        await _scroll_profile_to_top(page)

        cta_count = await _profile_header_cta_count(page)
        if cta_count > 0:
            return

        if attempt == 0:
            print("    🧪 Profile header CTAs missing; reloading current profile once...")
            try:
                await page.reload(wait_until="domcontentloaded")
                await asyncio.sleep(0.6)
            except Exception:
                pass


async def _find_connect_after_opening_more(page) -> Any | None:
    open_menu = page.locator(".artdeco-dropdown__content[aria-hidden='false']").first
    try:
        menu_items = open_menu.locator("[role='menuitem'], [role='button'], button, li")
        menu_count = await menu_items.count()
        menu_labels: list[str] = []
        for idx in range(min(menu_count, 10)):
            item = menu_items.nth(idx)
            text = (await item.inner_text() or "").strip()
            if text:
                menu_labels.append(" ".join(text.split()))
        if menu_labels:
            print(f"    🧪 More menu items: {menu_labels[:8]}")
    except Exception:
        pass

    try:
        connect_role_loc = open_menu.get_by_role("menuitem", name="Connect")
        if await connect_role_loc.count() > 0:
            print("    🧪 Found Connect via role=menuitem")
            return connect_role_loc.first
    except Exception:
        pass

    connect_selectors = (
        "div.artdeco-dropdown__item[role='button'][aria-label^='Invite ' i][aria-label$=' to connect' i]",
        "div.artdeco-dropdown__item[role='button'][aria-label*='Invite' i][aria-label*='connect' i]",
        "div.artdeco-dropdown__item[role='button']:has(use[href='#connect-medium'])",
        "div.artdeco-dropdown__item[role='button']:has(span:has-text('Connect'))",
        "[role='menuitem']:has-text('Connect')",
        "[role='button'][aria-label*=' to connect' i]",
    )

    for selector in connect_selectors:
        try:
            candidates = open_menu.locator(selector)
            count = await candidates.count()
            for idx in range(min(count, 10)):
                locator = candidates.nth(idx)
                try:
                    label = (await locator.inner_text() or "").strip() or (
                        await locator.get_attribute("aria-label") or ""
                    ).strip()
                except Exception:
                    label = ""
                print(f"    🧪 Found Connect via selector: {selector} idx={idx} label={label!r}")
                return locator
        except Exception:
            continue

    return None


async def _try_global_more_menu_for_connect(page) -> Any | None:
    more_selectors = (
        "main button[aria-label='More actions'][id$='-profile-overflow-action']",
        "main button[aria-label='More actions']",
        "main button.artdeco-dropdown__trigger[aria-label='More actions']",
        "main button[id$='-profile-overflow-action']",
        "main button[id*='profile-overflow-action']",
        "main button:has(use[href='#overflow-web-ios-small'])",
        "main button:has(use[href*='overflow'])",
        "main button:has(use[xlink\\:href*='overflow'])",
        "main button:has([data-test-icon='overflow-web-ios-small'])",
    )

    for more_selector in more_selectors:
        try:
            candidates = page.locator(more_selector)
            count = await candidates.count()
        except Exception:
            continue

        for idx in range(min(count, 8)):
            try:
                more_button = candidates.nth(idx)
                label = (await more_button.inner_text() or "").strip() or (
                    await more_button.get_attribute("aria-label") or ""
                ).strip()
                button_id = ((await more_button.get_attribute("id")) or "").strip().lower()
                try:
                    in_main = await more_button.evaluate("el => !!el.closest('main')")
                except Exception:
                    in_main = False
                lower_label = label.lower()
                if not in_main:
                    continue
                if "more actions" not in lower_label and "profile-overflow-action" not in button_id:
                    continue
                print(f"    🧪 Trying More control: selector={more_selector} idx={idx} label={label!r}")

                await _click_naturally(page, more_button)
                print(f"    🧪 Clicked More control: selector={more_selector} idx={idx} label={label!r}")
                await asyncio.sleep(random.uniform(0.5, 1.0))

                try:
                    expanded = await more_button.get_attribute("aria-expanded")
                    if expanded:
                        print(f"    🧪 More control aria-expanded={expanded}")
                except Exception:
                    pass

                connect_locator = await _find_connect_after_opening_more(page)
                if connect_locator is not None:
                    return connect_locator

                await page.keyboard.press("Escape")
                await asyncio.sleep(random.uniform(0.3, 0.6))
            except Exception:
                continue

    # Final fallback: click a likely overflow icon control directly from the DOM.
    try:
        clicked_meta = await page.evaluate("""() => {
            const candidates = [...document.querySelectorAll(`
                main button, main .artdeco-dropdown__trigger
            `)];
            const scored = [];
            for (const el of candidates) {
                const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                const text = (el.innerText || '').replace(/\s+/g, ' ').trim().toLowerCase();
                const id = (el.id || '').toLowerCase();
                const hasOverflowUse = !!el.querySelector("use[href*='overflow'], use[*|href*='overflow']");
                const looksProfileMore = aria.includes('more actions') || id.includes('profile-overflow-action') || hasOverflowUse;
                if (text.includes('load more')) continue;
                if (el.closest('header, .global-nav, .application-outlet__header, .feed-shared-update-v2, .scaffold-finite-scroll, .search-results-container, .notification-badge, .notifications-menu')) continue;
                if (!looksProfileMore) continue;
                const rect = el.getBoundingClientRect();
                const nearTop = rect.top >= 0 && rect.top < 500;
                let score = 0;
                if (nearTop) score += 3;
                if (aria.includes('more')) score += 4;
                if (text === 'more') score += 3;
                if (id.includes('profile-overflow-action')) score += 6;
                if (id.includes('overflow')) score += 2;
                if (hasOverflowUse) score += 4;
                if (el.className && String(el.className).includes('artdeco-dropdown__trigger')) score += 2;
                const nearbyLabels = [...(el.parentElement?.querySelectorAll('button, a[role="button"], [role="button"]') || [])]
                    .map(node => ((node.getAttribute('aria-label') || node.innerText || '').replace(/\s+/g, ' ').trim().toLowerCase()))
                    .filter(Boolean);
                if (nearbyLabels.some(label => label.includes('message'))) score += 3;
                if (nearbyLabels.some(label => label === 'follow' || label.includes('follow '))) score += 3;
                scored.push({ el, score, aria, text, id });
            }
            scored.sort((a, b) => b.score - a.score);
            const winner = scored[0];
            if (!winner) return null;
            winner.el.click();
            return { score: winner.score, aria: winner.aria, text: winner.text, id: winner.id };
        }""")
        if clicked_meta:
            print(f"    🧪 More fallback DOM-click: {clicked_meta}")
            await asyncio.sleep(random.uniform(0.5, 1.0))
            connect_locator = await _find_connect_after_opening_more(page)
            if connect_locator is not None:
                return connect_locator
            await page.keyboard.press("Escape")
            await asyncio.sleep(random.uniform(0.3, 0.6))
    except Exception:
        pass

    return None


async def get_connection_action(page) -> tuple[str, Any]:
    """
    Inspect the current profile page and return the available connection action.

    Returns (status, locator_or_none):
      'pending'   – connection request already pending; should skip
      'connected' – already a 1st-degree connection; should skip
      'connect'   – Connect locator found and returned
      'none'      – no actionable path found; should skip
    """
    # 1. Already pending?
    for sel in ("button[aria-label*='Pending' i]", "button:has-text('Pending')"):
        try:
            if await page.locator(sel).count() > 0:
                return "pending", None
        except Exception:
            pass

    # 2. Already connected? LinkedIn shows "1st" degree near the headline.
    for sel in (
        "span.dist-value:has-text('1st')",
        "span:has-text('• 1st')",
        "[data-anonymize='connection-degree']:has-text('1st')",
    ):
        try:
            if await page.locator(sel).count() > 0:
                return "connected", None
        except Exception:
            pass

    debug_labels = await _collect_visible_primary_action_labels(page)
    print(f"    🧪 Visible profile actions: {debug_labels[:8] if debug_labels else 'none_detected'}")

    if not debug_labels:
        await _debug_profile_action_snapshot(page)

    normalized_debug_labels = [label.strip().lower() for label in debug_labels]
    header_has_more = any(label == "more" or "more actions" in label for label in normalized_debug_labels)
    header_has_connect = any(label == "connect" or "invite" in label for label in normalized_debug_labels)

    # If the visible top action row shows More but not Connect, the real Connect
    # path is inside More. Ignore any other Connect buttons elsewhere on the page.
    if header_has_more and not header_has_connect:
        print("    🧪 Header actions show More but no Connect; forcing More menu path")
        connect_from_more = await _try_global_more_menu_for_connect(page)
        if connect_from_more is not None:
            return "connect", connect_from_more
        return "none", None

    # Prefer profile-header "More actions" path first when present.
    # This avoids false positives from non-header "Invite ... to connect" buttons
    # in side modules/recommendations.
    header_more_selectors = (
        "button[aria-label='More actions'][id$='-profile-overflow-action']",
        "button[aria-label='More actions']",
        "button.artdeco-dropdown__trigger[aria-label='More actions']",
        "button[id*='profile-overflow-action']",
    )
    header_more_count = 0
    for sel in header_more_selectors:
        try:
            header_more_count += await page.locator(sel).count()
        except Exception:
            continue

    if header_more_count > 0 and not header_has_connect:
        print(f"    🧪 Header More actions detected (count={header_more_count}); prioritizing More menu path")
        connect_from_more = await _try_global_more_menu_for_connect(page)
        if connect_from_more is not None:
            return "connect", connect_from_more
        # If header More exists but connect wasn't found in that menu, avoid
        # matching unrelated Connect buttons elsewhere on the page.
        return "none", None

    # 3. Direct "Connect" button on the profile action bar.
    # Note: visibility checks are intentionally skipped — the LinkedIn SPA keeps the
    # previous search-results DOM alive as an overlay during navigation, causing
    # is_visible() to return False for all profile buttons. We detect by count and label,
    # then force-click via _click_naturally which falls back to force=True.
    direct_connect_selectors = (
        "main button[aria-label*='Invite' i][aria-label*='to connect' i]",
        "button[aria-label*='Invite' i][aria-label*='to connect' i]",
        "main [role='button'][aria-label*='Invite' i][aria-label*='connect' i]",
        "[role='button'][aria-label*='Invite' i][aria-label*='connect' i]",
        "main button:has(use[href='#connect-small'])",
        "button:has(use[href='#connect-small'])",
        "main [role='button']:has(use[href*='connect'])",
        "[role='button']:has(use[href*='connect'])",
        "main button[aria-label^='Connect with' i]",
        "main button[aria-label='Connect' i]",
    )
    for sel in direct_connect_selectors:
        try:
            candidates = page.locator(sel)
            count = await candidates.count()
            for idx in range(min(count, 6)):
                btn = candidates.nth(idx)
                try:
                    label = (await btn.inner_text() or "").strip() or (await btn.get_attribute("aria-label") or "").strip()
                except Exception:
                    label = ""
                if "pending" in label.lower():
                    continue
                print(f"    🧪 Found direct Connect via selector: {sel} idx={idx} label={label!r}")
                return "connect", btn
        except Exception:
            continue

    # 4. Inspect the "More" dropdown for Connect across UI variants.
    connect_from_more = await _try_global_more_menu_for_connect(page)
    if connect_from_more is not None:
        return "connect", connect_from_more

    return "none", None


async def send_connection_with_message(page, message: str) -> bool:
    """
    After the Connect button has been clicked, handle the invitation modal:
    click 'Add a note', type the personalised message, and send.
    Returns True on success.
    """
    # Wait for the invitation dialog to appear.
    try:
        await page.wait_for_selector(
            "button[aria-label='Add a note'], "
            "button[aria-label='Send without a note'], "
            ".artdeco-modal__actionbar, [data-test-modal], [role='dialog']",
            timeout=12_000,
        )
    except Exception:
        return False
    await asyncio.sleep(random.uniform(0.3, 0.6))

    try:
        add_note_count = await page.locator("button[aria-label='Add a note']").count()
        send_wo_count = await page.locator("button[aria-label='Send without a note']").count()
        print(f"    🧪 Invite modal buttons: add_note={add_note_count} send_without_note={send_wo_count}")
    except Exception:
        pass

    # Click "Add a note" to reveal the message textarea.
    note_clicked = False
    for sel in (
        "button[aria-label='Add a note']",
        "[role='dialog'] button[aria-label='Add a note']",
        ".artdeco-modal__actionbar button[aria-label='Add a note']",
        "button[aria-label*='Add a note' i]",
    ):
        try:
            candidates = page.locator(sel)
            count = await candidates.count()
            if count == 0:
                continue
            add_note_btn = candidates.first
            await _click_naturally(page, add_note_btn)
            print(f"    🧪 Clicked Add a note via selector: {sel}")
            await asyncio.sleep(random.uniform(0.5, 1.0))
            note_clicked = True
            break
        except Exception:
            pass

    if not note_clicked:
        print("    ⚠ 'Add a note' button not found; attempting to continue without note.")
        return False

    if note_clicked:
        try:
            textarea = page.locator(
                "textarea#custom-message, textarea[name='message'], "
                "[role='dialog'] textarea, [data-test-modal] textarea, textarea"
            ).first
            await textarea.wait_for(timeout=8_000)
            await _click_naturally(page, textarea)
            await asyncio.sleep(random.uniform(0.2, 0.4))
            for ch in message:
                await page.keyboard.type(ch, delay=random.randint(40, 150))
            await asyncio.sleep(random.uniform(0.5, 1.0))

        except Exception as exc:
            print(f"    ⚠ Failed to type message: {exc}")
            await page.keyboard.press("Escape")
            return False

    # Click Send / Send invitation.
    for sel in (
        "button[aria-label*='Send invitation' i]",
        "button[aria-label*='Send now' i]",
        "button:has-text('Send without a note')",
        "[data-test-modal] button:has-text('Send')",
        "[data-test-modal] button:has-text('Send without a note')",
        "[role='dialog'] button:has-text('Send')",
        "[role='dialog'] button:has-text('Send without a note')",
    ):
        try:
            send_btn = page.locator(sel).first
            if await send_btn.is_visible(timeout=3_000):
                await _click_naturally(page, send_btn)
                await asyncio.sleep(random.uniform(1.0, 2.0))
                return True
        except Exception:
            pass

    return False


# ── per-lead orchestration ────────────────────────────────────────────────────

async def connect_to_lead(page, lead: dict[str, Any]) -> tuple[int, str]:
    """
    Search for, navigate to, and connect with a single lead.

    Returns (db_value, log_message):
      CONNECTION_SUCCESS ( 1) – invitation sent
      CONNECTION_SKIP   (-1) – already connected / pending / no connect path
      0                      – transient error; DB not updated
    """
    name = lead.get("name") or ""
    slug = (lead.get("slug") or "").strip().lower() or None
    message = (lead.get("connection_message") or "").strip()
    linkedin_url = lead.get("linkedin_url") or ""

    print(f"  → Searching for: {name}" + (f"  (slug: {slug})" if slug else ""))

    # ── search via LinkedIn search bar (same flow as scrape_linkedin.py) ──
    await use_search_bar(page, name)

    if DEBUG_CURSOR:
        await show_cursor(page)

    try:
        await page.wait_for_selector('[data-view-name="people-search-result"]', timeout=15_000)
    except Exception:
        print(f"    ⚠ Search results not found for '{name}'.")

    await random_scroll(page)
    await human_delay(1, 3)

    # ── click into the matching profile ──
    clicked = await click_matching_profile(
        page,
        slug,
        debug_cursor=DEBUG_CURSOR,
        show_cursor_fn=show_cursor,
    )
    if not clicked:
        return 0, f"{linkedin_url}: could_not_click_profile"

    try:
        await page.wait_for_url(re.compile(r"linkedin\.com/in/"), timeout=15_000)
    except Exception:
        return 0, f"{linkedin_url}: did_not_land_on_profile (url={page.url})"

    await _stabilize_profile_page(page)

    await dismiss_blocking_linkedin_popup(page)
    await human_delay(1, 2)

    # Brief simulate_reading so the profile fully loads and looks natural.
    print("    📖 Scrolling through profile…")
    await HumanBehavior.simulate_reading(page)
    await dismiss_blocking_linkedin_popup(page)

    # Return to top where primary CTA buttons (Connect / More / Follow) live.
    await _scroll_profile_to_top(page)
    await _stabilize_profile_page(page)
    await dismiss_blocking_linkedin_popup(page)
    await human_delay(0.3, 0.8)

    # ── detect profile status and find Connect path ──
    status, connect_locator = await get_connection_action(page)
    print(f"    🔍 Profile status: {status}")

    if status == "pending":
        return CONNECTION_SKIP, f"{linkedin_url}: already_pending"

    if status == "connected":
        return CONNECTION_SKIP, f"{linkedin_url}: already_connected"

    if status == "none":
        return CONNECTION_SKIP, f"{linkedin_url}: no_connect_path_found"

    # ── status == "connect" ── click the button ──
    await _click_naturally(page, connect_locator)
    await asyncio.sleep(random.uniform(0.8, 1.5))

    if not message:
        # No personalised message stored — dismiss "Add a note" and send directly.
        for send_sel in (
            "button[aria-label*='Send invitation' i]",
            "button[aria-label*='Send now' i]",
            "button:has-text('Send without a note')",
            "[data-test-modal] button:has-text('Send')",
            "[data-test-modal] button:has-text('Send without a note')",
            "[role='dialog'] button:has-text('Send')",
            "[role='dialog'] button:has-text('Send without a note')",
        ):
            try:
                send_btn = page.locator(send_sel).first
                if await send_btn.is_visible(timeout=3_000):
                    await _click_naturally(page, send_btn)
                    await asyncio.sleep(random.uniform(1.0, 2.0))
                    return CONNECTION_SUCCESS, f"{linkedin_url}: sent_without_note"
            except Exception:
                pass
        return CONNECTION_SUCCESS, f"{linkedin_url}: sent (no_note, no_dialog_appeared)"

    sent = await send_connection_with_message(page, message)
    if sent:
        return CONNECTION_SUCCESS, f"{linkedin_url}: sent_with_note"

    return 0, f"{linkedin_url}: send_failed"


# ── database helpers ──────────────────────────────────────────────────────────

def fetch_leads_to_connect(
    conn: sqlite3.Connection,
    threshold: float,
    limit: int,
) -> list[dict[str, Any]]:
    sql = """
    SELECT linkedin_url, slug, name, connection_message, rating
    FROM leads
    WHERE rating >= ?
      AND connection_requested = 0
    ORDER BY rating DESC
    """
    params: list[Any] = [threshold]

    if limit > 0:
        sql += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(sql, tuple(params)).fetchall()
    return [
        {
            "linkedin_url": (row[0] or "").strip() or None,
            "slug": (row[1] or "").strip() or None,
            "name": (row[2] or "").strip() or None,
            "connection_message": (row[3] or "").strip() or None,
            "rating": row[4],
        }
        for row in rows
    ]


def mark_connection_result(conn: sqlite3.Connection, linkedin_url: str, value: int) -> None:
    conn.execute(
        "UPDATE leads SET connection_requested = ? WHERE linkedin_url = ?",
        (value, linkedin_url),
    )
    conn.commit()


def delete_user_folder_for_slug(slug: str | None) -> tuple[bool, str]:
    normalized_slug = (slug or "").strip().strip("/")
    if not normalized_slug:
        return False, "slug_missing"

    target_dir = Path("user_data") / normalized_slug
    if not target_dir.exists():
        return False, f"not_found:{target_dir}"

    if not target_dir.is_dir():
        return False, f"not_a_directory:{target_dir}"

    try:
        shutil.rmtree(target_dir)
        return True, f"deleted:{target_dir}"
    except Exception as exc:
        return False, f"delete_failed:{target_dir}: {exc}"


def choose_profiles_for_run(leads: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    if not leads:
        return [], 0

    target_count = random.randint(RANDOM_RUN_MIN_PROFILES, RANDOM_RUN_MAX_PROFILES)
    selected_count = min(target_count, len(leads))
    return leads[:selected_count], target_count


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send LinkedIn connection requests to high-scoring leads from leads.db"
    )
    parser.add_argument("--db", type=Path, default=Path("leads.db"), help="Path to SQLite database (default: leads.db)")
    parser.add_argument("--threshold", type=float, default=5.0, help="Minimum rating to process (default: 5.0)")
    parser.add_argument("--limit", type=int, default=0, help="Max leads to process per run; 0 = all (default: 0)")
    parser.add_argument(
        "--daily-limit",
        type=int,
        default=20,
        help="Hard cap per run to respect LinkedIn limits (default: 20)",
    )
    parser.add_argument(
        "--cdp-endpoint",
        default=CDP_ENDPOINT,
        help="Chrome CDP endpoint (default: from env or http://127.0.0.1:9222)",
    )
    return parser.parse_args()


# ── main ──────────────────────────────────────────────────────────────────────

async def main() -> int:
    args = parse_args()

    if not args.db.exists():
        print(f"Database not found: {args.db}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(args.db))

    # Respect both --limit and --daily-limit; whichever is tighter wins,
    # but still fetch enough candidates for the random 2-4 selection.
    effective_limit = args.daily_limit
    if args.limit > 0:
        effective_limit = min(args.limit, args.daily_limit)
    effective_limit = max(effective_limit, RANDOM_RUN_MAX_PROFILES)

    try:
        leads = fetch_leads_to_connect(conn, threshold=args.threshold, limit=effective_limit)
    except sqlite3.Error as exc:
        conn.close()
        print(f"Failed to query leads: {exc}", file=sys.stderr)
        return 1

    if not leads:
        conn.close()
        print(f"No qualifying leads found (rating >= {args.threshold:.0f}, connection_requested = 0).")
        return 0

    print(f"Qualifying leads: {len(leads)}")

    leads, target_count = choose_profiles_for_run(leads)
    print(
        f"Selected for this run: {len(leads)} "
        f"(random target={target_count}, min={RANDOM_RUN_MIN_PROFILES}, max={RANDOM_RUN_MAX_PROFILES})"
    )
    for idx, lead in enumerate(leads, start=1):
        print(
            f"  [{idx}] slug={lead.get('slug') or 'N/A'} | "
            f"name={lead.get('name') or 'N/A'} | "
            f"rating={lead.get('rating') or 'N/A'} | "
            f"url={lead.get('linkedin_url') or 'N/A'}"
        )

    success_count = 0
    skip_count = 0
    failure_count = 0

    async with async_playwright() as pw:
        print(f"Connecting to Chrome on {args.cdp_endpoint} …")
        browser = await pw.chromium.connect_over_cdp(args.cdp_endpoint)
        context = browser.contexts[0]
        page = await context.new_page()

        await go_home_and_simulate_reading(page)

        if DEBUG_CURSOR:
            await show_cursor(page)

        for idx, lead in enumerate(leads, start=1):
            linkedin_url = lead.get("linkedin_url")
            print(f"\n[{idx}/{len(leads)}] {lead.get('name') or 'N/A'} (rating={lead.get('rating') or 'N/A'})")

            try:
                db_value, detail = await connect_to_lead(page, lead)
            except Exception as exc:
                db_value, detail = 0, f"{linkedin_url}: exception: {exc}"

            if db_value == CONNECTION_SUCCESS:
                success_count += 1
                if linkedin_url:
                    mark_connection_result(conn, linkedin_url, CONNECTION_SUCCESS)
                deleted, cleanup_detail = delete_user_folder_for_slug(lead.get("slug"))
                if deleted:
                    print(f"  🗑 Cleanup: {cleanup_detail}")
                else:
                    print(f"  ⚠ Cleanup skipped: {cleanup_detail}")
                print(f"  ✅ {detail}")
            elif db_value == CONNECTION_SKIP:
                skip_count += 1
                if linkedin_url:
                    mark_connection_result(conn, linkedin_url, CONNECTION_SKIP)
                print(f"  ⏭ {detail}")
            else:
                failure_count += 1
                print(f"  ❌ {detail}", file=sys.stderr)

            if idx < len(leads):
                await human_delay(MIN_WAIT, MAX_WAIT)
                # Every 5 profiles return to the feed to break up the pattern.
                if idx % 5 == 0:
                    await go_home_and_simulate_reading(page)

        await page.close()

    conn.close()
    print(f"\nDone. Sent={success_count}, Skipped={skip_count}, Failed={failure_count}")
    return 0 if failure_count == 0 or success_count > 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
