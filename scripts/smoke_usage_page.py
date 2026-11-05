#!/usr/bin/env python3
"""Browser live-verification script for the stage 5 `/usage` page (not a unit test, hence the smoke_ prefix to keep pytest from collecting it).

Read-only: opens the page, clicks a few things, takes screenshots. Writes nothing to the DB and changes no state.
Run it once per language — the page is fully i18n-driven, so verifying only one language is the same as not verifying the other.

Usage: python3 scripts/smoke_usage_page.py [base_url]
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8010"
SHOTS = Path(__file__).resolve().parent.parent / "test-screenshots"
SHOTS.mkdir(exist_ok=True)

MUST_APPEAR = {
    "zh": [
        "Coverage Matrix",
        "Unattributed Drill-down",
        "Attributed Detail",
        "Single Measurement",
        "Metric Definitions",
        "usage_sum",
        "ctx_last",
        "Not collected by design",
        "No total row",
        "Four layers, split; no total field",
        "Single measurement, not a full ledger",
        "Leader main session",
        "no_transcript_path",
        "not_yet_measured",
        "by_design",
    ],
    "en": [
        "Coverage Matrix",
        "Unattributed Drill-down",
        "Attributed Detail",
        "Single Measurement",
        "Metric Definitions",
        "usage_sum",
        "ctx_last",
        "No total row",
        "Four layers, split; no total field",
        "Single measurement, not a full ledger",
        "Leader main session",
    ],
}


def run(lang: str) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1200})
        page.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        page.goto(f"{BASE}/usage")
        page.evaluate(f"localStorage.setItem('lang', '{lang}')")
        page.goto(f"{BASE}/usage", wait_until="networkidle")
        page.wait_for_timeout(7000)

        page.screenshot(path=str(SHOTS / f"usage-{lang}-01-firstscreen.png"))
        page.screenshot(path=str(SHOTS / f"usage-{lang}-02-full.png"), full_page=True)

        body = page.locator("body").inner_text()
        missing = [probe for probe in MUST_APPEAR[lang] if probe not in body]
        for probe in MUST_APPEAR[lang]:
            print(f"  [{lang}] {probe}: {'YES' if probe in body else '*** MISSING ***'}")

        # Mobile breakpoint (375px) — tables and side-by-side panels must not overflow horizontally
        if lang == "zh":
            page.set_viewport_size({"width": 375, "height": 900})
            page.wait_for_timeout(1500)
            page.screenshot(path=str(SHOTS / "usage-zh-03-mobile375.png"), full_page=True)
            overflow = page.evaluate(
                "document.documentElement.scrollWidth - document.documentElement.clientWidth"
            )
            print(f"  [zh] 375px horizontal overflow: {overflow}px")

        browser.close()
    return missing, errors


def main() -> int:
    bad = False
    for lang in ("zh", "en"):
        missing, errors = run(lang)
        print(f"[{lang}] CONSOLE ERRORS:", errors[:8] if errors else "none")
        if missing or errors:
            bad = True
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
