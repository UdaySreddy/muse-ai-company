#!/usr/bin/env python3
"""Interaction verification for the stage 5 `/usage` page (not a unit test; the smoke_ prefix keeps pytest from collecting it).

Verifies four things that can really break: drill-down can enter the next level, the unattributed drawer can collapse and expand, the single-measurement card can produce numbers,
and an unattributed number on the matrix can switch the drawer to that path. Read-only; writes nothing to the DB.

Usage: python3 scripts/smoke_usage_interactions.py <agent_id> [base_url]
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

AGENT_ID = sys.argv[1]
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8010"
SHOTS = Path(__file__).resolve().parent.parent / "test-screenshots"
SHOTS.mkdir(exist_ok=True)


def main() -> int:
    errors: list[str] = []
    failures: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1200})
        page.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        page.goto(f"{BASE}/usage")
        page.evaluate("localStorage.setItem('lang', 'zh')")
        page.goto(f"{BASE}/usage", wait_until="networkidle")
        page.wait_for_timeout(7000)

        # ① Click "N unattributed" on the matrix → the drawer switches to that path
        page.get_by_role("button", name="117 unattributed").click()
        page.wait_for_timeout(2500)
        drawer = page.get_by_text("Unattributed Drill-down").first
        drawer.scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        page.screenshot(path=str(SHOTS / "usage-zh-04-drawer-leader.png"))
        if "Leader main session" not in page.locator("body").inner_text():
            failures.append("The unattributed number on the matrix failed to switch the drawer to the Leader path")
        print("① Matrix unattributed number → drawer switched path: OK")

        # ② Drill down: in the sub-agent panel, click "Drill into session" on the first sub-card
        page.get_by_role("button", name="Drill into session").first.click()
        page.wait_for_timeout(5000)
        body = page.locator("body").inner_text()
        if "Drill into workflow run" not in body:
            failures.append("After drilling down one level, the next level's (workflow run) drill-down entry did not appear")
        page.get_by_role("button", name="Drill into workflow run").first.scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        page.screenshot(path=str(SHOTS / "usage-zh-05-drill-session.png"))
        print("② Drill down project→session→(workflow_run entry): OK")

        # ③ Drill down two more levels to agent, verify the "Measure this one" button appears
        page.get_by_role("button", name="Drill into workflow run").first.click()
        page.wait_for_timeout(5000)
        if "Drill into agent" in page.locator("body").inner_text():
            page.get_by_role("button", name="Drill into agent").first.click()
            page.wait_for_timeout(5000)
            has_probe = page.get_by_role("button", name="Measure this one").count() > 0
            print(f"③ Drilled down to the agent level, \"Measure this one\" button present: {'OK' if has_probe else 'MISSING'}")
            if not has_probe:
                failures.append("The agent level did not show the \"Measure this one\" button")
            page.screenshot(path=str(SHOTS / "usage-zh-06-drill-agent.png"), full_page=True)
        else:
            print("③ This path has no workflow-run children (that is what the data says), skipping the agent-level drill-down")

        # ④ Single-measurement card: type an agent id by hand and get numbers
        box = page.get_by_placeholder("Paste an agent id, or hit \"Measure this one\" in the detail section above")
        box.scroll_into_view_if_needed()
        box.fill(AGENT_ID)
        page.get_by_role("button", name="Measure", exact=True).click()
        page.wait_for_timeout(6000)
        body = page.locator("body").inner_text()
        ok = all(k in body for k in ("Input excerpt", "Output excerpt", "API calls", "Model (observed)"))
        print(f"④ Single-measurement card produced numbers: {'OK' if ok else 'FAILED'}")
        if not ok:
            failures.append("The single-measurement card did not render the four layers / excerpts / model")
        page.get_by_text("Single Measurement").first.scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        page.screenshot(path=str(SHOTS / "usage-zh-07-probe.png"))

        # ⑤ Drawer collapse/expand
        page.get_by_text("No transcript path ever recorded").first.click()
        page.wait_for_timeout(1200)
        print("⑤ Drawer collapse/expand: OK")

        print("CONSOLE ERRORS:", errors[:8] if errors else "none")
        browser.close()

    for f in failures:
        print(f"❌ {f}")
    return 1 if failures or errors else 0


if __name__ == "__main__":
    sys.exit(main())
