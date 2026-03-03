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
from playwright.async_api import Page

# Toggle for visible cursor debugging
DEBUG_CURSOR = os.getenv("SHOW_CURSOR", "0") == "1"


async def show_cursor(page: Page) -> None:
    """Make mouse cursor visible during automation for debugging.
    
    Creates a red circle that follows the actual mouse position.
    Only affects what's displayed; doesn't change automation behavior.
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
                z-index: 999999;
                background: rgba(255, 0, 0, 0.2);
                transition: all 0.05s linear;
                transform: translate(-10px, -10px);
            `;
            document.body.appendChild(cursor);
            
            let mouseX = 0;
            let mouseY = 0;
            
            const updateCursor = () => {
                cursor.style.left = mouseX + 'px';
                cursor.style.top = mouseY + 'px';
                requestAnimationFrame(updateCursor);
            };
            
            document.addEventListener('mousemove', (e) => {
                mouseX = e.clientX;
                mouseY = e.clientY;
            }, true);
            
            updateCursor();
        }
    """)
    print("\n🔴 Cursor visibility ENABLED - red circle will follow mouse movements\n")


class HumanBehavior:
    """Simulate natural human browsing behavior."""
    
    # Track last known mouse position to avoid jumping to 0,0
    _last_mouse_x = 500
    _last_mouse_y = 400

    @staticmethod
    async def smooth_scroll(page: Page, direction: str = "down") -> None:
        """Scroll smoothly like a human, not instant jumps.
        
        Args:
            page: Playwright page object
            direction: "down" or "up"
        """
        current_scroll = await page.evaluate("window.scrollY")
        
        # Determine target scroll position
        if direction == "down":
            target_scroll = current_scroll + random.randint(300, 800)
        else:
            target_scroll = max(0, current_scroll - random.randint(200, 500))
        
        # Scroll in small increments for smoothness
        steps = random.randint(10, 20)
        increment = (target_scroll - current_scroll) / steps
        
        for _ in range(steps):
            await page.evaluate(f"window.scrollBy(0, {increment})")
            await asyncio.sleep(random.uniform(0.05, 0.15))

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
            if DEBUG_CURSOR:
                print(f"  → {int(x)}, {int(y)}", end="\r")
            
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
        
        if DEBUG_CURSOR:
            print(f"  ✓ Hovered over element: {selector}")
    
    @staticmethod
    async def random_idle_movement(page: Page) -> None:
        """Move mouse to center/random area and pause (simulating reading/thinking)."""
        viewport = page.viewport_size
        if not viewport:
            viewport = await page.evaluate("() => ({ width: window.innerWidth, height: window.innerHeight })")
        
        # Move to a random reading position
        target_x = random.randint(viewport['width'] // 4, 3 * viewport['width'] // 4)
        target_y = random.randint(viewport['height'] // 4, 3 * viewport['height'] // 4)
        
        if DEBUG_CURSOR:
            print(f"  💭 Idle movement to ({target_x}, {target_y})")
        
        await HumanBehavior.natural_mouse_move(page, target_x, target_y)
        await asyncio.sleep(random.uniform(0.5, 1.5))

    @staticmethod
    async def simulate_reading(page: Page) -> None:
        """Simulate natural reading behavior with lots of scrolling and hovering.
        
        Combines smooth scrolling with occasional pauses and element hovers
        to mimic a real person reading a page.
        
        Args:
            page: Playwright page object
        """
        # Do 5-10 reading actions (scrolls + interactions)
        num_actions = random.randint(5, 10)
        
        for i in range(num_actions):
            action_type = random.random()
            
            if action_type < 0.6:  # 60% chance: scroll
                direction = random.choice(["down", "down", "down", "up"])  # Mostly down
                await HumanBehavior.smooth_scroll(page, direction)
                await asyncio.sleep(random.uniform(0.8, 2.0))
            
            elif action_type < 0.85:  # 25% chance: idle movement
                await HumanBehavior.random_idle_movement(page)
            
            else:  # 15% chance: hover over element
                await HumanBehavior.hover_and_interact(page, "a, button, [role='button']")
            
            # Occasional longer pause (like reading a paragraph)
            if random.random() > 0.7:
                await asyncio.sleep(random.uniform(1.5, 3.0))
        
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
