from __future__ import annotations

import asyncio
import random
from typing import Any, Awaitable, Callable, Optional

from human_behavior import HumanBehavior


LINKEDIN_HOME = "https://www.linkedin.com/feed/"
SEARCH_INPUT_SELECTOR = "input.search-global-typeahead__input"

LINKEDIN_MODAL_SELECTORS = (
    ".artdeco-modal",
    ".artdeco-modal__content",
    ".upsell-animated-fullpage-takeover-modal__content",
    "div[class*='upsell-animated-fullpage-takeover-modal__content']",
    "[role='dialog']",
    "div[role='dialog']",
    "[data-test-modal]",
    ".artdeco-dialog",
    ".msg-overlay-conversation-bubble",
)
LINKEDIN_MODAL_CLOSE_SELECTORS = (
    "button[aria-label='Dismiss']",
    "button[aria-label='Close']",
    "button[aria-label='Dismiss dialog']",
    "button[aria-label='Close dialog']",
    "button[aria-label*='dismiss' i]",
    "button[aria-label*='close' i]",
    "button[data-control-name*='dismiss' i]",
    "button[data-control-name*='close' i]",
    ".artdeco-modal__dismiss",
    "[data-test-modal-close-btn]",
    "[role='dialog'] button.artdeco-button__icon",
    "button:has(use[href='#close-medium'])",
    "[role='dialog'] button:has(use[href='#close-medium'])",
    ".artdeco-modal button:has(use[href='#close-medium'])",
    ".artdeco-modal__content button:has(use[href='#close-medium'])",
)
LINKEDIN_MODAL_ACTION_SELECTORS = (
    "button:has-text('Not now')",
    "button:has-text('No thanks')",
    "button:has-text('Maybe later')",
    "button:has-text('Dismiss')",
    "button:has-text('Close')",
)


async def human_delay(lo: float = 2, hi: float = 5) -> None:
    await asyncio.sleep(random.uniform(lo, hi))


async def random_scroll(
    page,
    scrolls_min: int = 4,
    scrolls_max: int = 7,
    scroll_pause_min: float = 0.8,
    scroll_pause_max: float = 2.5,
) -> None:
    await HumanBehavior.natural_mouse_move(page, random.randint(400, 700), random.randint(350, 500))
    await asyncio.sleep(random.uniform(0.2, 0.4))

    num_scrolls = random.randint(scrolls_min, scrolls_max)
    print(f"    📜 Scrolling {num_scrolls} times...")
    for i in range(num_scrolls):
        direction = random.choices(["down", "up"], weights=[0.8, 0.2])[0]
        await HumanBehavior.smooth_scroll(page, direction)
        await asyncio.sleep(random.uniform(scroll_pause_min, scroll_pause_max))

        if i % 4 == 0 and random.random() > 0.5:
            await HumanBehavior.random_idle_movement(page)

    if random.random() > 0.4:
        await HumanBehavior.hover_and_interact(page, "a, button, [role='button']")
    print("    📜 Scrolling complete")


async def dismiss_blocking_linkedin_popup(page) -> bool:
    dismissed_any = False

    for _ in range(3):
        visible_modal = None
        for modal_selector in LINKEDIN_MODAL_SELECTORS:
            modal = page.locator(modal_selector).first
            try:
                if await modal.is_visible():
                    visible_modal = modal
                    break
            except Exception:
                continue

        if visible_modal is None:
            if dismissed_any:
                return True
            break

        clicked_close = False

        for close_selector in LINKEDIN_MODAL_CLOSE_SELECTORS:
            close_button = visible_modal.locator(close_selector).first
            try:
                if not await close_button.is_visible():
                    continue

                box = await close_button.bounding_box()
                if box:
                    target_x = int(box["x"] + box["width"] / 2 + random.randint(-4, 4))
                    target_y = int(box["y"] + box["height"] / 2 + random.randint(-4, 4))
                    await HumanBehavior.natural_mouse_move(page, target_x, target_y)
                    await asyncio.sleep(random.uniform(0.1, 0.3))

                await close_button.click(timeout=2_500)
                await asyncio.sleep(random.uniform(0.4, 0.9))
                dismissed_any = True
                clicked_close = True
                print("    ✕ Dismissed blocking LinkedIn popup")
                break
            except Exception:
                continue

        if clicked_close:
            continue

        for action_selector in LINKEDIN_MODAL_ACTION_SELECTORS:
            action_button = visible_modal.locator(action_selector).first
            try:
                if not await action_button.is_visible():
                    continue
                await action_button.click(timeout=2_500)
                await asyncio.sleep(random.uniform(0.4, 0.9))
                dismissed_any = True
                clicked_close = True
                print("    ✕ Dismissed blocking LinkedIn popup via action button")
                break
            except Exception:
                continue

        if clicked_close:
            continue

        # Some LinkedIn dialogs only expose an icon-only close button with
        # <use href="#close-medium"> inside an SVG.
        try:
            icon_close = page.locator("button:has(use[href='#close-medium'])").first
            if await icon_close.is_visible(timeout=1_500):
                await icon_close.click(timeout=2_500)
                await asyncio.sleep(random.uniform(0.4, 0.9))
                dismissed_any = True
                clicked_close = True
                print("    ✕ Dismissed blocking LinkedIn popup via close-medium icon button")
        except Exception:
            pass

        if clicked_close:
            continue

        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(random.uniform(0.4, 0.9))
            dismissed_any = True
            print("    ✕ Attempted popup dismissal via Escape")
        except Exception:
            break

    return dismissed_any


async def click_with_popup_recovery(page, locator, description: str) -> None:
    await dismiss_blocking_linkedin_popup(page)

    last_exc = None
    for attempt in range(2):
        try:
            if attempt == 1:
                dismissed = await dismiss_blocking_linkedin_popup(page)
                if dismissed:
                    await human_delay(0.2, 0.5)
            await locator.click()
            return
        except Exception as exc:
            last_exc = exc

    raise RuntimeError(f"Failed to click {description}: {last_exc}")


async def go_home_and_simulate_reading(page, linkedin_home: str = LINKEDIN_HOME) -> None:
    await page.goto(linkedin_home, wait_until="domcontentloaded")
    await dismiss_blocking_linkedin_popup(page)
    await human_delay(2, 4)
    print("    🏠 On home feed — simulating reading behavior…")
    await HumanBehavior.simulate_reading(page)
    await human_delay(1, 2)


async def use_search_bar(page, query: str) -> None:
    try:
        await page.wait_for_selector(SEARCH_INPUT_SELECTOR, timeout=10_000)
    except Exception:
        await go_home_and_simulate_reading(page)
        await page.wait_for_selector(SEARCH_INPUT_SELECTOR, timeout=10_000)

    await dismiss_blocking_linkedin_popup(page)

    search_box = page.locator(SEARCH_INPUT_SELECTOR)

    box = await search_box.bounding_box()
    if box:
        target_x = int(box["x"] + box["width"] / 2 + random.randint(-10, 10))
        target_y = int(box["y"] + box["height"] / 2 + random.randint(-3, 3))
        await HumanBehavior.natural_mouse_move(page, target_x, target_y)
        await asyncio.sleep(random.uniform(0.1, 0.3))

    await click_with_popup_recovery(page, search_box, "LinkedIn search box")
    await human_delay(0.3, 0.7)

    await page.keyboard.press("Meta+a")
    await asyncio.sleep(random.uniform(0.1, 0.3))
    await page.keyboard.press("Backspace")
    await asyncio.sleep(random.uniform(0.2, 0.5))

    for ch in query:
        await page.keyboard.type(ch, delay=random.randint(40, 170))
    await human_delay(0.5, 1.2)

    await page.keyboard.press("Enter")
    await page.wait_for_load_state("domcontentloaded")
    await dismiss_blocking_linkedin_popup(page)
    await human_delay(3, 6)

    try:
        people_btn = page.locator('button:has-text("People")').first
        await people_btn.wait_for(timeout=6_000)
        await human_delay(0.3, 0.8)

        box = await people_btn.bounding_box()
        if box:
            target_x = int(box["x"] + box["width"] / 2 + random.randint(-5, 5))
            target_y = int(box["y"] + box["height"] / 2 + random.randint(-3, 3))
            await HumanBehavior.natural_mouse_move(page, target_x, target_y)
            await asyncio.sleep(random.uniform(0.1, 0.3))
        await click_with_popup_recovery(page, people_btn, "People filter button")

        await page.wait_for_load_state("domcontentloaded")
        await dismiss_blocking_linkedin_popup(page)
        await human_delay(2, 4)
    except Exception:
        pass


async def click_matching_profile(
    page,
    slug: Optional[str],
    debug_cursor: bool = False,
    show_cursor_fn: Callable[[Any], Awaitable[None]] | None = None,
) -> bool:
    try:
        await page.wait_for_selector('a[data-view-name="search-result-lockup-title"]', timeout=10_000)
    except Exception:
        return False

    links = page.locator('a[data-view-name="search-result-lockup-title"]')
    count = await links.count()

    if slug:
        slug_token = f"/in/{slug.lower()}"

        for idx in range(count):
            href = (await links.nth(idx).get_attribute("href") or "").lower()
            if (slug_token + "/") in href or (slug_token + "?") in href or href.rstrip("/").endswith(slug_token):
                await human_delay(0.5, 1.5)
                box = await links.nth(idx).bounding_box()
                if box:
                    target_x = int(box["x"] + box["width"] / 2 + random.randint(-10, 10))
                    target_y = int(box["y"] + box["height"] / 2 + random.randint(-10, 10))
                    await HumanBehavior.natural_mouse_move(page, target_x, target_y)
                    await asyncio.sleep(random.uniform(0.3, 0.7))
                await dismiss_blocking_linkedin_popup(page)
                await click_with_popup_recovery(page, links.nth(idx), "matching search result")
                await page.wait_for_load_state("domcontentloaded")
                await dismiss_blocking_linkedin_popup(page)
                if debug_cursor and show_cursor_fn:
                    await show_cursor_fn(page)
                return True

        for idx in range(count):
            href = (await links.nth(idx).get_attribute("href") or "").lower()
            if slug in href:
                await human_delay(0.5, 1.5)
                box = await links.nth(idx).bounding_box()
                if box:
                    target_x = int(box["x"] + box["width"] / 2 + random.randint(-10, 10))
                    target_y = int(box["y"] + box["height"] / 2 + random.randint(-10, 10))
                    await HumanBehavior.natural_mouse_move(page, target_x, target_y)
                    await asyncio.sleep(random.uniform(0.3, 0.7))
                await dismiss_blocking_linkedin_popup(page)
                await click_with_popup_recovery(page, links.nth(idx), "partial-match search result")
                await page.wait_for_load_state("domcontentloaded")
                await dismiss_blocking_linkedin_popup(page)
                if debug_cursor and show_cursor_fn:
                    await show_cursor_fn(page)
                return True

    if count > 0:
        await human_delay(0.5, 1.5)
        box = await links.first.bounding_box()
        if box:
            target_x = int(box["x"] + box["width"] / 2 + random.randint(-10, 10))
            target_y = int(box["y"] + box["height"] / 2 + random.randint(-10, 10))
            await HumanBehavior.natural_mouse_move(page, target_x, target_y)
            await asyncio.sleep(random.uniform(0.3, 0.7))
        await dismiss_blocking_linkedin_popup(page)
        await click_with_popup_recovery(page, links.first, "first search result")
        await page.wait_for_load_state("domcontentloaded")
        await dismiss_blocking_linkedin_popup(page)
        if debug_cursor and show_cursor_fn:
            await show_cursor_fn(page)
        return True

    return False
