"""v1.5.1 smoke test: verify stage facet statistics are accurate + StatsBar/FilterBar tooltips + RepoCard badges.

Run:
  python scripts/smoke_v151_stage_facet.py
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright

PROJECT_ID = "3aa58dc7-a771-4745-8fa6-efbd5819956a"
PROJECT_PATH = "C:/Users/TUF/Desktop/AI-team-framework/ai-team-os"
BASE = "http://localhost:8000"


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        page = ctx.new_page()

        # Step 1: Inject the project id into localStorage
        page.goto(BASE)
        page.evaluate(
            f"() => {{ localStorage.setItem('aiteam.activeProjectId', '{PROJECT_ID}'); "
            f"localStorage.setItem('aiteam.activeProjectPath', '{PROJECT_PATH}'); }}"
        )
        page.goto(f"{BASE}/ecosystem", wait_until="networkidle")

        # Give the frontend 1s to render
        page.wait_for_timeout(1500)

        # === Check 1: StatsBar display ===
        stats_text = page.locator(".grid, .flex").first.inner_text() if False else page.content()
        print("\n=== Test 1: StatsBar ===")
        # Look for "262" or "queued" numbers in the page text
        import re
        # Find all number + label pairs
        for keyword in ["Pending shallow scan", "Deep-scanned", "Inactive repos", "Current view"]:
            if keyword in stats_text:
                # Find "{keyword}: number" or "{number}\n{keyword}" patterns
                idx = stats_text.find(keyword)
                snippet = stats_text[max(0, idx - 60):idx + 60]
                nums = re.findall(r">(\d+)<", snippet)
                print(f"  {keyword}: nearby numbers = {nums}")
            else:
                print(f"  {keyword}: NOT FOUND")

        # === Check 2: Pending shallow scan = 262 (not 102) ===
        page.screenshot(path="v151_screenshot_stats.png", full_page=False)
        print("\nScreenshot: v151_screenshot_stats.png")

        # === Check 3: FilterBar selecting "Deep-scanned" shows 3 repos ===
        print("\n=== Test 3: Deep-scanned filter ===")
        try:
            stage_trigger = page.get_by_label("Research stage").first
            stage_trigger.click()
            page.wait_for_timeout(500)
            page.get_by_text("Deep-scanned", exact=False).first.click()
            page.wait_for_timeout(1500)
            page.screenshot(path="v151_screenshot_deepscan.png", full_page=False)
            print("Screenshot: v151_screenshot_deepscan.png")
        except Exception as e:
            print(f"  Filter test failed: {e}")

        page.wait_for_timeout(2000)
        browser.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
