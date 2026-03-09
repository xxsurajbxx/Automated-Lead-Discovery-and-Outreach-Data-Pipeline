"""
Human-like behavior simulation for Playwright browser automation.
Uses smooth scrolling, natural mouse movements with Bézier curves,
and hovering patterns to avoid bot detection.

Environment variable to toggle visible cursor debugging:
  SHOW_CURSOR=1 python3 enrichment.py --input people.csv
"""

import os
import random
import asyncio
import time
from playwright.async_api import Page

# Seed RNG with current time so every run has different behavior
random.seed(time.time())

# Toggle for visible cursor debugging
DEBUG_CURSOR = os.getenv("SHOW_CURSOR", "0") == "1"


async def show_cursor(page: Page) -> None:
    """Make mouse cursor visible during automation for debugging.

    Creates a red circle that follows the actual mouse position.
    Uses multiple event listeners (mousemove, pointermove, mouseover)
    on window, document, and document.documentElement to ensure
    Playwright CDP mouse events are captured on macOS.
    """
    await page.evaluate("""
        () => {
            // Remove existing cursor if any
            const existing = document.getElementById('automation-cursor');
            if (existing) existing.remove();

            const cursor = document.createElement('div');
            cursor.id = 'automation-cursor';
            cursor.style.cssText = `
                position: fixed;
                width: 20px;
                height: 20px;
                border: 3px solid red;
                border-radius: 50%;
                pointer-events: none;
                z-index: 2147483647;
                background: rgba(255, 0, 0, 0.3);
                transition: left 0.05s linear, top 0.05s linear;
                left: -100px;
                top: -100px;
            `;
            document.documentElement.appendChild(cursor);

            const moveCursor = (e) => {
                cursor.style.left = (e.clientX - 10) + 'px';
                cursor.style.top = (e.clientY - 10) + 'px';
            };

            // Listen on every target Playwright might dispatch to
            for (const target of [window, document, document.documentElement, document.body]) {
                if (!target) continue;
                target.addEventListener('mousemove',   moveCursor, true);
                target.addEventListener('pointermove', moveCursor, true);
                target.addEventListener('mouseover',   moveCursor, true);
                target.addEventListener('pointerover', moveCursor, true);
            }
        }
    """)


class HumanBehavior:
    """Simulate natural human browsing behavior."""

    # Track last known mouse position to avoid jumping to 0,0
    _last_mouse_x = 500
    _last_mouse_y = 400

    @staticmethod
    async def smooth_scroll(page: Page, direction: str = "down") -> None:
        """Scroll smoothly like a human using native mouse wheel events.

        Uses Playwright's mouse.wheel() to dispatch real wheel events
        at the current cursor position. The browser handles routing
        these to the correct scrollable container automatically,
        which works reliably on LinkedIn profile pages.

        Args:
            page: Playwright page object
            direction: "down" or "up"
        """
        total_distance = random.randint(300, 700)
        if direction == "up":
            total_distance = -total_distance

        steps = random.randint(8, 15)
        per_step = total_distance / steps

        # First, make sure the mouse is over the main content area
        # so wheel events hit the right scrollable container
        viewport = page.viewport_size
        if not viewport:
            viewport = await page.evaluate("() => ({ width: window.innerWidth, height: window.innerHeight })")
        center_x = viewport['width'] // 2
        center_y = viewport['height'] // 2

        # Move mouse to center of viewport if not already there
        current_x = HumanBehavior._last_mouse_x
        current_y = HumanBehavior._last_mouse_y
        if abs(current_x - center_x) > 200 or abs(current_y - center_y) > 200:
            await HumanBehavior.natural_mouse_move(
                page,
                center_x + random.randint(-100, 100),
                center_y + random.randint(-50, 50)
            )
            await asyncio.sleep(random.uniform(0.1, 0.3))

        # Use native mouse wheel events for scrolling
        # The browser will route these to the correct scrollable element
        for step_i in range(steps):
            delta = int(per_step + random.uniform(-10, 10))
            await page.mouse.wheel(0, delta)

            # Variable delay between wheel ticks for natural feel
            if step_i < steps // 3:
                delay = random.uniform(0.02, 0.06)  # Fast start
            elif step_i > 2 * steps // 3:
                delay = random.uniform(0.02, 0.06)  # Fast end
            else:
                delay = random.uniform(0.04, 0.10)  # Slower middle
            await asyncio.sleep(delay)

        # Small settling pause after scroll
        await asyncio.sleep(random.uniform(0.1, 0.3))

    @staticmethod
    async def natural_mouse_move(page: Page, target_x: int, target_y: int) -> None:
        """Move mouse naturally with curved Bézier path and acceleration/deceleration.

        Args:
            page: Playwright page object
            target_x: Target X coordinate
            target_y: Target Y coordinate
        """
        # Use last known position instead of 0,0
        current_x = HumanBehavior._last_mouse_x
        current_y = HumanBehavior._last_mouse_y

        # Calculate distance for step calculation
        distance = ((target_x - current_x) ** 2 + (target_y - current_y) ** 2) ** 0.5

        # Adjust steps based on distance
        if distance < 100:
            steps = random.randint(10, 20)
        elif distance < 300:
            steps = random.randint(20, 35)
        else:
            steps = random.randint(35, 50)

        # Generate curved path using quadratic Bézier curve
        control_x = random.randint(min(current_x, target_x) - 50, max(current_x, target_x) + 50)
        control_y = random.randint(min(current_y, target_y) - 50, max(current_y, target_y) + 50)

        for i in range(steps):
            t = i / steps

            # Ease-in-out curve for acceleration/deceleration
            # Fast at start and end, slower in middle
            if t < 0.5:
                eased_t = 2 * t * t  # Ease in (accelerate)
            else:
                eased_t = 1 - 2 * (1 - t) * (1 - t)  # Ease out (decelerate)

            # Apply easing to position
            x = (1 - eased_t) ** 2 * current_x + 2 * (1 - eased_t) * eased_t * control_x + eased_t ** 2 * target_x
            y = (1 - eased_t) ** 2 * current_y + 2 * (1 - eased_t) * eased_t * control_y + eased_t ** 2 * target_y

            await page.mouse.move(int(x), int(y))

            # Variable speed - faster during acceleration/deceleration phases
            if t < 0.3 or t > 0.7:
                delay = random.uniform(0.01, 0.03)  # Fast
            else:
                delay = random.uniform(0.03, 0.06)  # Slower middle

            await asyncio.sleep(delay)

        # Update tracked position
        HumanBehavior._last_mouse_x = target_x
        HumanBehavior._last_mouse_y = target_y

        # Brief pause at destination
        await asyncio.sleep(random.uniform(0.1, 0.3))

    @staticmethod
    async def hover_and_interact(page: Page, selector: str) -> None:
        """Hover naturally over elements, pause, then move away.

        Args:
            page: Playwright page object
            selector: CSS selector for elements to hover
        """
        try:
            element = await page.query_selector(selector)
            if element:
                # Get element bounding box
                box = await element.bounding_box()
                if box:
                    # Target position with slight randomness
                    target_x = int(box['x'] + box['width'] / 2 + random.randint(-20, 20))
                    target_y = int(box['y'] + box['height'] / 2 + random.randint(-20, 20))

                    # Move mouse naturally to element
                    await HumanBehavior.natural_mouse_move(page, target_x, target_y)

                    # Hover and "read" for a moment
                    await asyncio.sleep(random.uniform(0.5, 1.5))

                    # Move away to random location
                    away_x = random.randint(100, page.viewport_size['width'] - 100)
                    away_y = random.randint(100, page.viewport_size['height'] - 100)
                    await HumanBehavior.natural_mouse_move(page, away_x, away_y)
        except Exception:
            pass  # Element not found or interaction failed

    @staticmethod
    async def random_idle_movement(page: Page) -> None:
        """Move mouse to center/random area and pause (simulating reading/thinking)."""
        viewport = page.viewport_size
        if not viewport:
            viewport = await page.evaluate("() => ({ width: window.innerWidth, height: window.innerHeight })")

        # Move to a random reading position
        target_x = random.randint(viewport['width'] // 4, 3 * viewport['width'] // 4)
        target_y = random.randint(viewport['height'] // 4, 3 * viewport['height'] // 4)

        await HumanBehavior.natural_mouse_move(page, target_x, target_y)
        await asyncio.sleep(random.uniform(0.5, 1.5))

    @staticmethod
    async def simulate_reading(page: Page) -> None:
        """Simulate natural reading behavior with scrolling and hovering.

        Moves the mouse to the content area first so wheel events work,
        then combines smooth scrolling with occasional pauses and hovers.
        Scrolls aggressively enough to trigger LinkedIn's lazy-loaded
        profile sections (experience, education, skills, etc.).

        Args:
            page: Playwright page object
        """
        # Move mouse to the center content area so wheel events route correctly
        viewport = page.viewport_size
        if not viewport:
            viewport = await page.evaluate("() => ({ width: window.innerWidth, height: window.innerHeight })")
        await HumanBehavior.natural_mouse_move(
            page,
            viewport['width'] // 2 + random.randint(-100, 100),
            viewport['height'] // 2 + random.randint(-50, 50)
        )
        await asyncio.sleep(random.uniform(0.3, 0.6))

        # Do 5-8 reading actions to ensure enough scrolling to load
        # all lazy-loaded profile sections
        num_actions = random.randint(5, 8)
        print(f"      📖 Reading: {num_actions} actions")

        for i in range(num_actions):
            action_type = random.random()

            if action_type < 0.65:  # 65% chance: scroll down
                direction = "down"
                await HumanBehavior.smooth_scroll(page, direction)
                await asyncio.sleep(random.uniform(0.8, 2.0))

            elif action_type < 0.75:  # 10% chance: scroll up slightly
                direction = "up"
                await HumanBehavior.smooth_scroll(page, direction)
                await asyncio.sleep(random.uniform(0.5, 1.2))

            elif action_type < 0.9:  # 15% chance: idle movement
                await HumanBehavior.random_idle_movement(page)

            else:  # 10% chance: hover over element
                await HumanBehavior.hover_and_interact(page, "a, button, [role='button']")

            # Occasional longer pause (like reading a paragraph)
            if random.random() > 0.7:
                await asyncio.sleep(random.uniform(1.5, 3.0))

        # Final scroll back up partially so the page state looks natural
        if random.random() > 0.5:
            await HumanBehavior.smooth_scroll(page, "up")
            await asyncio.sleep(random.uniform(0.5, 1.0))

        if DEBUG_CURSOR:
            print("  ✓ Reading simulation complete")

    @staticmethod
    async def human_pause(min_sec: float = 1, max_sec: float = 4) -> None:
        """Variable pause between actions to simulate thinking/reading.

        Args:
            min_sec: Minimum pause duration
            max_sec: Maximum pause duration
        """
        await asyncio.sleep(random.uniform(min_sec, max_sec))
