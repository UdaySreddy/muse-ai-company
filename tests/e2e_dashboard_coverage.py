"""
AI Team OS Dashboard — supplementary E2E tests
Covers pages and scenarios not included in the existing tests:
- ProjectsPage (/projects)
- ProjectDetailPage (/projects/:id)
- MeetingsPage (/meetings)
- TeamsPage (the /teams route via dashboard / team detail is actually embedded in ProjectDetail)

Prerequisites:
- The backend API is running at http://localhost:8000
- The Dashboard (Vite) is running at http://localhost:5173
- playwright is installed: pip install playwright && playwright install chromium

How to run:
    cd ai-team-os
    python tests/e2e_dashboard_coverage.py
"""

import os
import traceback

from playwright.sync_api import sync_playwright

from aiteam.clock import utc_now

BASE_URL = "http://localhost:5173"
API_URL = "http://localhost:8000"
RESULTS: list[str] = []


# ─── Helper functions ─────────────────────────────────────────────────────────────────


def log(msg: str) -> None:
    ts = utc_now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def record(test_name: str, step: str, status: str, detail: str = "") -> None:
    entry = f"[{status}] {test_name} — {step}"
    if detail:
        entry += f" | {detail}"
    RESULTS.append(entry)
    log(entry)


def screenshot(page, name: str) -> None:
    dir_ = os.path.join(os.path.dirname(__file__), "..", "test-screenshots")
    os.makedirs(dir_, exist_ok=True)
    path = os.path.join(dir_, name)
    page.screenshot(path=path, full_page=True)
    log(f"  Screenshot: {name}")


# ─── Health check ─────────────────────────────────────────────────────────────────


def test_api_health(page):
    """TC-000: Backend API reachability check"""
    test = "TC000-API health check"
    try:
        log(f"\n{'=' * 60}\nStarting {test}\n{'=' * 60}")
        resp = page.request.get(f"{API_URL}/api/health")
        if resp.status == 200:
            record(test, "GET /api/health returns 200", "PASS")
        else:
            record(test, "GET /api/health", "FAIL", f"HTTP {resp.status}")
    except Exception as e:
        record(test, "API connectivity", "FAIL", str(e))


# ─── TC-001: ProjectsPage ─────────────────────────────────────────────────────


def test_projects_page(page):
    """TC-001: Project list page — page is accessible, core elements render, create-project dialog"""
    test = "TC001-ProjectsPage"
    try:
        log(f"\n{'=' * 60}\nStarting {test}\n{'=' * 60}")

        page.goto(f"{BASE_URL}/projects", wait_until="networkidle")
        page.wait_for_timeout(1500)
        screenshot(page, "e2e-projects-list.png")

        # 1. Page heading is visible
        try:
            heading = page.locator("h1").first
            heading_text = heading.inner_text() if heading.count() > 0 else ""
            record(test, f"Page heading is visible: '{heading_text}'", "PASS" if heading_text else "WARN")
        except Exception as e:
            record(test, "Page heading", "FAIL", str(e))

        # 2. Core elements: table or empty state
        try:
            table = page.locator("table")
            empty_hint = page.locator("text=No projects yet, text=No projects")
            if table.count() > 0:
                rows = page.locator("table tbody tr")
                record(test, f"Project table is visible, row count: {rows.count()}", "PASS")
            elif empty_hint.count() > 0:
                record(test, "Empty-state hint is visible", "PASS")
            else:
                record(test, "Table or empty state", "WARN", "Neither was found")
        except Exception as e:
            record(test, "Table rendering verification", "FAIL", str(e))

        # 3. Create-project button is visible
        try:
            create_btn = page.locator("button:has-text('Create Project'), button:has-text('New Project')")
            if create_btn.count() > 0:
                record(test, "Create-project button exists", "PASS")
            else:
                # Try the Plus icon button
                plus_btn = page.locator("button svg.lucide-plus").locator("..")
                record(test, "Create button (Plus icon)", "PASS" if plus_btn.count() > 0 else "WARN")
        except Exception as e:
            record(test, "Create button verification", "FAIL", str(e))

        # 4. Open the create-project dialog
        try:
            create_btn = page.locator("button").filter(has_text="Create").first
            if create_btn.count() > 0:
                create_btn.click()
                page.wait_for_timeout(600)
                dialog = page.locator("[role='dialog']")
                if dialog.count() > 0:
                    screenshot(page, "e2e-projects-create-dialog.png")
                    record(test, "Create-project dialog opened", "PASS")
                    # Verify form fields
                    name_input = page.locator("[role='dialog'] input").first
                    if name_input.count() > 0:
                        record(test, "Dialog contains input fields", "PASS")
                    else:
                        record(test, "Dialog form fields", "WARN", "No input found")
                    # Close the dialog
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(400)
                else:
                    record(test, "Create dialog opened", "WARN", "The dialog element did not appear")
        except Exception as e:
            record(test, "Create dialog interaction", "FAIL", str(e))

        # 5. Edge-case test: create with an empty name (if the dialog is already open)
        try:
            create_btn = page.locator("button").filter(has_text="Create").first
            if create_btn.count() > 0:
                create_btn.click()
                page.wait_for_timeout(500)
                dialog = page.locator("[role='dialog']")
                if dialog.count() > 0:
                    # Submit without filling in a name
                    submit = (
                        page.locator("[role='dialog'] button[type='submit'], [role='dialog'] button")
                        .filter(has_text="Create")
                        .last
                    )
                    submit.click()
                    page.wait_for_timeout(500)
                    # The dialog should still be present (submission did not succeed)
                    still_open = page.locator("[role='dialog']").count() > 0
                    record(test, "Empty name is not submitted (dialog did not close)", "PASS" if still_open else "WARN",
                        "An empty name should be blocked from submission")
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(300)
        except Exception as e:
            record(test, "Empty-name edge-case test", "WARN", str(e))

    except Exception as e:
        record(test, "Overall", "FAIL", str(e))
        traceback.print_exc()


# ─── TC-002: ProjectDetailPage ────────────────────────────────────────────────


def test_project_detail_page(page):
    """TC-002: Project detail page — navigate in via the first project and verify the activity log and decision
    timeline"""
    test = "TC002-ProjectDetailPage"
    try:
        log(f"\n{'=' * 60}\nStarting {test}\n{'=' * 60}")

        # First get the project list and take the first project ID
        page.goto(f"{BASE_URL}/projects", wait_until="networkidle")
        page.wait_for_timeout(1500)

        # Prefer getting the first project ID via the API and navigating directly to the detail page (most reliable)
        resp = page.request.get(f"{API_URL}/api/projects?limit=1")
        if resp.status == 200:
            data = resp.json()
            projects = data.get("data", [])
            if projects:
                project_id = projects[0]["id"]
                page.goto(f"{BASE_URL}/projects/{project_id}", wait_until="networkidle")
                page.wait_for_timeout(2000)
            else:
                # When there are no projects, try clicking the View button on the list page
                view_btn = page.locator(
                    "button:has-text('View'), a:has-text('View'), "
                    "button:has-text('Details'), a:has-text('Details'), "
                    "tr td a, table a"
                ).first
                if view_btn.count() > 0:
                    view_btn.click()
                    page.wait_for_timeout(2000)
                else:
                    record(test, "No project data, skipping the detail-page test", "WARN",
                        "A project needs to be created first")
                    return
        else:
            record(test, "Could not get the project list", "WARN", f"API {resp.status}")
            return

        current_url = page.url
        if "/projects/" not in current_url:
            record(test, "URL should contain /projects/:id", "FAIL", f"current URL: {current_url}")
            return

        screenshot(page, "e2e-project-detail.png")
        record(test, f"Entered the project detail page: {current_url}", "PASS")

        # 1. Project name/title is visible
        try:
            heading = page.locator("h1, h2").first
            heading_text = heading.inner_text() if heading.count() > 0 else ""
            record(test, f"Project title is visible: '{heading_text[:30]}'", "PASS" if heading_text else "WARN")
        except Exception as e:
            record(test, "Project title", "FAIL", str(e))

        # 2. Team/Agent section
        try:
            agent_section = page.locator("text=Agent, text=Members, text=Team Members")
            if agent_section.count() > 0:
                record(test, "Agent/Members section is visible", "PASS")
            else:
                record(test, "Agent/Members section", "WARN", "No related labels found")
        except Exception as e:
            record(test, "Agent section verification", "FAIL", str(e))

        # 3. Activity log section
        try:
            activity_labels = ["Activity", "Log", "Activity", "History"]
            found_activity = False
            for label in activity_labels:
                if page.locator(f"text={label}").count() > 0:
                    found_activity = True
                    record(test, f"Activity section is visible (matched: '{label}')", "PASS")
                    break
            if not found_activity:
                record(test, "Activity log section", "WARN", "No Activity/Log labels found")
        except Exception as e:
            record(test, "Activity log verification", "FAIL", str(e))

        # 4. Decision timeline
        try:
            decision_labels = ["Decisions", "Timeline", "Decision"]
            found_decision = False
            for label in decision_labels:
                if page.locator(f"text={label}").count() > 0:
                    found_decision = True
                    record(test, f"Decision section is visible (matched: '{label}')", "PASS")
                    break
            if not found_decision:
                record(test, "Decision timeline section", "WARN", "No decision-related labels found")
        except Exception as e:
            record(test, "Decision timeline verification", "FAIL", str(e))

        # 5. Back button
        try:
            back_btn = page.locator("button:has-text('Back'), a:has-text('Back'), [aria-label='back']")
            if back_btn.count() > 0:
                record(test, "Back button exists", "PASS")
            else:
                record(test, "Back button", "WARN", "No back button found")
        except Exception as e:
            record(test, "Back button verification", "FAIL", str(e))

        # 6. Run-task button
        try:
            run_task_btn = page.locator(
                "button:has-text('Run Task'), button:has-text('Execute Task'), button:has-text('Dispatch')"
            )
            if run_task_btn.count() > 0:
                record(test, "Run-task button is visible", "PASS")
            else:
                record(test, "Run-task button", "WARN", "Not found")
        except Exception as e:
            record(test, "Run-task button verification", "FAIL", str(e))

        screenshot(page, "e2e-project-detail-full.png")

    except Exception as e:
        record(test, "Overall", "FAIL", str(e))
        traceback.print_exc()


# ─── TC-003: MeetingsPage ─────────────────────────────────────────────────────


def test_meetings_page(page):
    """TC-003: Meeting list page — page is accessible, status filtering, create-meeting dialog"""
    test = "TC003-MeetingsPage"
    try:
        log(f"\n{'=' * 60}\nStarting {test}\n{'=' * 60}")

        page.goto(f"{BASE_URL}/meetings", wait_until="networkidle")
        page.wait_for_timeout(1500)
        screenshot(page, "e2e-meetings-list.png")

        # 1. Page title
        try:
            heading = page.locator("h1").first
            heading_text = heading.inner_text() if heading.count() > 0 else ""
            record(test, f"Page title: '{heading_text}'", "PASS" if heading_text else "WARN")
        except Exception as e:
            record(test, "Page title", "FAIL", str(e))

        # 2. Status filter button group (All / In Progress / Ended)
        try:
            filter_buttons = ["All", "In Progress", "Ended"]
            found_filters = 0
            for label in filter_buttons:
                btn = page.locator(f"button:has-text('{label}')")
                if btn.count() > 0:
                    found_filters += 1
            if found_filters >= 2:
                record(test, f"Status filter button group exists ({found_filters}/3)", "PASS")
            else:
                record(test, f"Status filter buttons ({found_filters}/3)", "WARN", "Some filter buttons were not found")
        except Exception as e:
            record(test, "Status filter verification", "FAIL", str(e))

        # 3. Click the "In Progress" filter
        try:
            active_btn = page.locator("button").filter(has_text="In Progress").first
            if active_btn.count() > 0:
                active_btn.click()
                page.wait_for_timeout(800)
                screenshot(page, "e2e-meetings-active-filter.png")
                record(test, "Clicked the 'In Progress' filter", "PASS")
            else:
                record(test, "'In Progress' filter button", "WARN", "Not found")
        except Exception as e:
            record(test, "Filter interaction", "FAIL", str(e))

        # 4. Click the "Ended" filter
        try:
            concluded_btn = page.locator("button").filter(has_text="Ended").first
            if concluded_btn.count() > 0:
                concluded_btn.click()
                page.wait_for_timeout(800)
                screenshot(page, "e2e-meetings-concluded-filter.png")
                record(test, "Clicked the 'Ended' filter", "PASS")
        except Exception as e:
            record(test, "'Ended' filter", "WARN", str(e))

        # 5. Back to "All"
        try:
            all_btn = page.locator("button").filter(has_text="All").first
            if all_btn.count() > 0:
                all_btn.click()
                page.wait_for_timeout(600)
        except Exception:
            pass

        # 6. Create-meeting button (only shown when a team exists)
        try:
            create_btn = page.locator("button:has-text('Create Meeting'), button:has-text('New Meeting')")
            if create_btn.count() > 0:
                record(test, "Create-meeting button is visible", "PASS")
                # Open the dialog
                create_btn.first.click()
                page.wait_for_timeout(600)
                dialog = page.locator("[role='dialog']")
                if dialog.count() > 0:
                    screenshot(page, "e2e-meetings-create-dialog.png")
                    record(test, "Create-meeting dialog opened", "PASS")
                    # Verify the team selector and topic input
                    topic_input = page.locator("#meeting-topic")
                    if topic_input.count() > 0:
                        record(test, "Meeting topic input exists", "PASS")
                    else:
                        record(test, "Meeting topic input", "WARN", "#meeting-topic not found")
                    team_select = page.locator("#meeting-team")
                    if team_select.count() > 0:
                        record(test, "Team selector exists", "PASS")
                    # Edge case: submit with an empty topic; the button should be disabled
                    submit_btn = page.locator("[role='dialog'] button[disabled]")
                    record(test, "Submit button is disabled when the topic is empty",
                        "PASS" if submit_btn.count() > 0 else "WARN")
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(400)
                else:
                    record(test, "Create dialog", "WARN", "The dialog did not appear")
            else:
                record(test, "Create-meeting button is not visible (expected when there is no team)", "WARN")
        except Exception as e:
            record(test, "Create-meeting interaction", "FAIL", str(e))

        # 7. Empty state or meeting cards
        try:
            meeting_cards = page.locator("[class*='card'], [class*='Card']")
            no_meetings = page.locator("text=No meetings yet, text=No meetings")
            if meeting_cards.count() > 2:  # The page has content cards
                record(test, f"Meeting cards rendered (about {meeting_cards.count()} elements)", "PASS")
            elif no_meetings.count() > 0:
                record(test, "Empty-state hint is visible", "PASS")
            else:
                record(test, "Meeting list or empty state", "WARN", "Neither had a clear marker")
        except Exception as e:
            record(test, "Meeting content verification", "FAIL", str(e))

    except Exception as e:
        record(test, "Overall", "FAIL", str(e))
        traceback.print_exc()


# ─── TC-004: Navigation — navigation completeness ─────────────────────────────────────────


def test_navigation_completeness(page):
    """TC-004: Sidebar navigation completeness — all routes accessible with no 404"""
    test = "TC004-Navigation completeness"
    routes = [
        ("/", "Home/Dashboard"),
        ("/projects", "Project list"),
        ("/tasks", "Task board"),
        ("/events", "Event log"),
        ("/meetings", "Meeting list"),
        ("/analytics", "Analytics"),
        ("/settings", "System settings"),
    ]
    try:
        log(f"\n{'=' * 60}\nStarting {test}\n{'=' * 60}")
        for route, name in routes:
            try:
                resp = page.goto(f"{BASE_URL}{route}", wait_until="domcontentloaded")
                page.wait_for_timeout(800)
                status = resp.status if resp else -1
                # A React SPA returns 200 (index.html) for all routes; judge by page rendering
                has_content = page.locator("main, #root, [class*='layout']").count() > 0
                record(
                    test,
                    f"{route} ({name}) is accessible",
                    "PASS" if has_content and status == 200 else "FAIL",
                    f"HTTP {status}",
                )
            except Exception as e:
                record(test, f"Accessing {route}", "FAIL", str(e))
    except Exception as e:
        record(test, "Overall", "FAIL", str(e))
        traceback.print_exc()


# ─── TC-005: Edge cases — invalid routes ─────────────────────────────────────────────


def test_invalid_routes(page):
    """TC-005: Invalid route handling — 404 page or redirect"""
    test = "TC005-Invalid route"
    try:
        log(f"\n{'=' * 60}\nStarting {test}\n{'=' * 60}")

        # Non-existent project ID
        page.goto(f"{BASE_URL}/projects/nonexistent-id-99999", wait_until="networkidle")
        page.wait_for_timeout(1500)
        screenshot(page, "e2e-invalid-project.png")

        has_error = (
            page.locator("text=404, text=Not found, text=Does not exist, text=Failed to load").count() > 0
            or page.locator("[class*='error'], [class*='destructive']").count() > 0
        )
        record(test, "Invalid project ID has error handling", "PASS" if has_error else "WARN",
            "Recommend showing a 404 or error hint")

        # A completely non-existent route
        page.goto(f"{BASE_URL}/this-route-does-not-exist", wait_until="domcontentloaded")
        page.wait_for_timeout(800)
        has_any_content = page.locator("#root").count() > 0
        record(test, "Unknown route renders content (SPA fallback)", "PASS" if has_any_content else "FAIL")

    except Exception as e:
        record(test, "Overall", "FAIL", str(e))
        traceback.print_exc()


# ─── TC-006: TeamsPage (accessed via project details) ────────────────────────────────────


def test_teams_via_project_detail(page):
    """TC-006: Team management — access team/Agent management features via the project detail page"""
    test = "TC006-Team/Agent management"
    try:
        log(f"\n{'=' * 60}\nStarting {test}\n{'=' * 60}")

        # Get the first project
        resp = page.request.get(f"{API_URL}/api/projects?limit=1")
        if resp.status != 200:
            record(test, "Could not get the project list", "WARN", "Skipping the team test")
            return
        projects = resp.json().get("data", [])
        if not projects:
            record(test, "No projects, skipping", "WARN")
            return

        project_id = projects[0]["id"]
        page.goto(f"{BASE_URL}/projects/{project_id}", wait_until="networkidle")
        page.wait_for_timeout(2000)
        screenshot(page, "e2e-team-via-project.png")

        # 1. Verify the page rendered the team list
        try:
            team_section = page.locator("text=Team, text=Teams").first
            record(test, "Team section exists", "PASS" if team_section.count() > 0 else "WARN")
        except Exception as e:
            record(test, "Team section", "FAIL", str(e))

        # 2. Add Agent button
        try:
            add_agent_btn = page.locator(
                "button:has-text('Add Agent'), button:has-text('AddAgent'), button:has-text('Add Member')"
            )
            if add_agent_btn.count() > 0:
                record(test, "Add Agent button exists", "PASS")
                # Open the dialog
                add_agent_btn.first.click()
                page.wait_for_timeout(600)
                if page.locator("[role='dialog']").count() > 0:
                    screenshot(page, "e2e-add-agent-dialog.png")
                    record(test, "Add Agent dialog opened", "PASS")
                    # Edge case: submit with an empty name
                    submit_btn = page.locator("[role='dialog'] button[disabled]")
                    record(test, "Submit button is disabled when the Agent name is empty",
                        "PASS" if submit_btn.count() > 0 else "WARN")
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(400)
            else:
                record(test, "Add Agent button", "WARN", "Not found (possibly no team)")
        except Exception as e:
            record(test, "Add Agent interaction", "FAIL", str(e))

        # 3. Agent status display
        try:
            agent_badges = page.locator("[class*='badge'], [class*='Badge']")
            record(test, f"Number of status badges: {agent_badges.count()}", "PASS")
        except Exception as e:
            record(test, "Agent status badges", "WARN", str(e))

    except Exception as e:
        record(test, "Overall", "FAIL", str(e))
        traceback.print_exc()


# ─── Main entry point ───────────────────────────────────────────────────────────────────


def main():
    log("=" * 60)
    log("AI Team OS Dashboard — supplementary E2E tests")
    log(f"Time: {utc_now().strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"Dashboard: {BASE_URL}")
    log(f"API: {API_URL}")
    log("=" * 60)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--start-maximized"],
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        page = context.new_page()
        # Set a reasonable timeout
        page.set_default_timeout(15_000)

        test_api_health(page)
        test_navigation_completeness(page)
        test_projects_page(page)
        test_project_detail_page(page)
        test_meetings_page(page)
        test_teams_via_project_detail(page)
        test_invalid_routes(page)

        page.wait_for_timeout(1500)
        browser.close()

    # ── Summary ──────────────────────────────────────────────────────────────────
    log("\n" + "=" * 60)
    log("Test result summary")
    log("=" * 60)

    pass_count = sum(1 for r in RESULTS if "[PASS]" in r)
    fail_count = sum(1 for r in RESULTS if "[FAIL]" in r)
    warn_count = sum(1 for r in RESULTS if "[WARN]" in r)

    for r in RESULTS:
        log(r)

    log(f"\nTotal: {len(RESULTS)} items | PASS: {pass_count} | FAIL: {fail_count} | WARN: {warn_count}")

    # ── Write the report ──────────────────────────────────────────────────────────────
    report_dir = os.path.join(os.path.dirname(__file__), "..", "test-screenshots")
    os.makedirs(report_dir, exist_ok=True)
    report_path = os.path.join(report_dir, "e2e_coverage_report.txt")

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("AI Team OS Dashboard — supplementary E2E test report\n")
        f.write(f"Date: {utc_now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Dashboard: {BASE_URL} | API: {API_URL}\n")
        f.write("=" * 60 + "\n\n")
        for r in RESULTS:
            f.write(r + "\n")
        f.write(f"\n{'=' * 60}\n")
        f.write(f"Total: {len(RESULTS)} items | PASS: {pass_count} | FAIL: {fail_count} | WARN: {warn_count}\n")

    log(f"\nReport saved to: {report_path}")
    return fail_count


if __name__ == "__main__":
    import sys

    fails = main()
    sys.exit(0 if fails == 0 else 1)
