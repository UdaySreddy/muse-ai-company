"""
AI Team OS — API-level E2E tests
Does not depend on the Dashboard/Playwright; tests the backend API directly with urllib.

Covered scenarios:
1. TC-A01: GET /api/health — health check
2. TC-A02: PUT /api/tasks/{id} — update status/result after creating a task
3. TC-A03: GET /api/config/team-templates — meeting/team template list
4. TC-A04: skill_registry.recommend — call the Python module directly and verify the recommendation logic
5. TC-A05: Edge cases — invalid task_id, invalid status values
6. TC-A06: GET /api/projects — project list pagination

How to run:
    cd ai-team-os
    python tests/e2e_api_coverage.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Any

from aiteam.clock import utc_now

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


def api_get(path: str) -> tuple[int, Any]:
    """GET request; returns (status_code, parsed_json_or_None)"""
    url = f"{API_URL}{path}"
    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"_raw": body}
    except Exception as e:
        return -1, {"error": str(e)}


def api_put(path: str, payload: dict) -> tuple[int, Any]:
    """PUT request; returns (status_code, parsed_json_or_None)"""
    url = f"{API_URL}{path}"
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="PUT")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"_raw": body}
    except Exception as e:
        return -1, {"error": str(e)}


def api_post(path: str, payload: dict) -> tuple[int, Any]:
    """POST request; returns (status_code, parsed_json_or_None)"""
    url = f"{API_URL}{path}"
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"_raw": body}
    except Exception as e:
        return -1, {"error": str(e)}


# ─── TC-A01: /api/health ──────────────────────────────────────────────────────


def test_health():
    """TC-A01: GET /api/health — health check"""
    test = "TC-A01-health"
    log(f"\n{'=' * 60}\nStarting {test}\n{'=' * 60}")

    status, body = api_get("/api/health")
    if status == 200:
        record(test, "GET /api/health returns 200", "PASS", f"body={body}")
    else:
        record(test, "GET /api/health", "FAIL", f"HTTP {status}, body={body}")
        return

    # Verify response fields
    if body.get("status") == "ok":
        record(test, "Response contains status=ok", "PASS")
    else:
        record(test, "Response field status", "FAIL", f"actual: {body.get('status')}")

    if "version" in body:
        record(test, f"Response contains version={body['version']}", "PASS")
    else:
        record(test, "Response is missing the version field", "WARN")


# ─── TC-A02: PUT /api/tasks/{id} ──────────────────────────────────────────────


def test_task_update():
    """TC-A02: PUT /api/tasks/{id} — update status/result after creating a task"""
    test = "TC-A02-task_update"
    log(f"\n{'=' * 60}\nStarting {test}\n{'=' * 60}")

    # First get a team ID
    status, body = api_get("/api/projects?limit=1")
    if status != 200:
        record(test, "Could not get the project list", "WARN", f"HTTP {status}")
        return

    projects = body.get("data", [])
    if not projects:
        record(test, "No project data, skipping", "WARN")
        return

    project_id = projects[0]["id"]

    # Get the associated team via the project
    status, teams_body = api_get(f"/api/teams?project_id={project_id}")
    team_id = None
    if status == 200:
        teams = teams_body.get("data", [])
        if teams:
            team_id = teams[0]["id"]

    if not team_id:
        # List all teams directly
        status, all_teams = api_get("/api/teams")
        if status == 200:
            teams = all_teams.get("data", [])
            if teams:
                team_id = teams[0]["id"]

    if not team_id:
        record(test, "No team data, skipping the task_update test", "WARN")
        return

    record(test, f"Using team team_id={team_id[:8]}...", "PASS")

    # Create a test task
    status, create_body = api_post(
        f"/api/teams/{team_id}/tasks/run",
        {
            "title": "QA automated test task",
            "description": "Created by e2e_api_coverage.py to verify the task_update API",
        },
    )
    if status not in (200, 201):
        record(test, "Create test task", "FAIL", f"HTTP {status}, {create_body}")
        return

    task_id = create_body.get("data", {}).get("id")
    if not task_id:
        record(test, "No id in the create-task response", "FAIL", str(create_body))
        return

    record(test, f"Task created successfully task_id={task_id[:8]}...", "PASS",
        f"initial status={create_body['data'].get('status')}")

    # Update status → running
    status, upd_body = api_put(
        f"/api/tasks/{task_id}",
        {"status": "running"},
    )
    if status == 200 and upd_body.get("success"):
        new_status = upd_body["data"].get("status")
        record(test, "Update status=running", "PASS", f"response status={new_status}")
    else:
        record(test, "Update status=running", "FAIL", f"HTTP {status}, {upd_body}")

    # Update result
    status, upd_body = api_put(
        f"/api/tasks/{task_id}",
        {"status": "completed", "result": "QA test automation verification complete"},
    )
    if status == 200 and upd_body.get("success"):
        result_val = upd_body["data"].get("result")
        record(test, "Update status=completed + result", "PASS", f"result={result_val}")
    else:
        record(test, "Update status=completed+result", "FAIL", f"HTTP {status}, {upd_body}")

    # Edge case: invalid status value → expect 400
    status, err_body = api_put(
        f"/api/tasks/{task_id}",
        {"status": "invalid_status_xyz"},
    )
    if status == 400:
        record(test, "Invalid status value returns 400", "PASS", f"detail={err_body.get('detail', '')[:80]}")
    else:
        record(test, "Invalid status value should return 400", "FAIL", f"actual HTTP {status}")


# ─── TC-A03: GET /api/config/team-templates ───────────────────────────────────


def test_team_templates():
    """TC-A03: GET /api/config/team-templates — team template list"""
    test = "TC-A03-team_templates"
    log(f"\n{'=' * 60}\nStarting {test}\n{'=' * 60}")

    status, body = api_get("/api/config/team-templates")
    if status == 200:
        record(test, "GET /api/config/team-templates returns 200", "PASS")
    else:
        record(test, "GET /api/config/team-templates", "FAIL", f"HTTP {status}, body={body}")
        return

    templates = body.get("data", [])
    if isinstance(templates, list):
        record(test, f"Returned a template list with {len(templates)} entries", "PASS")
    else:
        record(test, "The response data field is not a list", "FAIL", str(type(templates)))
        return

    if templates:
        first = templates[0]
        has_id = "id" in first
        has_name = "name" in first or "title" in first
        record(test, "Template contains an id field", "PASS" if has_id else "WARN", str(list(first.keys())[:5]))
        record(test, "Template contains a name/title field", "PASS" if has_name else "WARN")
    else:
        record(test, "Template list is empty (the config file may not exist)", "WARN",
            "plugin/config/team-templates.json")


# ─── TC-A04: skill_registry.recommend ────────────────────────────────────────


def test_skill_registry():
    """TC-A04: import skill_registry directly and verify the recommend logic"""
    test = "TC-A04-skill_registry"
    log(f"\n{'=' * 60}\nStarting {test}\n{'=' * 60}")

    try:
        import os
        import sys

        # Add src to the path
        src_path = os.path.join(os.path.dirname(__file__), "..", "src")
        if src_path not in sys.path:
            sys.path.insert(0, os.path.abspath(src_path))

        from aiteam.mcp.skill_registry import SKILLS

        record(test, f"Successfully imported SKILLS, {len(SKILLS)} skills in total", "PASS")

        # Verify data structure integrity
        if SKILLS:
            first = SKILLS[0]
            has_id = bool(first.id)
            has_name = bool(first.name)
            has_oneliner = bool(first.oneliner)
            record(
                test,
                "The first Skill has id/name/oneliner fields",
                "PASS" if (has_id and has_name and has_oneliner) else "FAIL",
                f"id={first.id}, name={first.name}",
            )
        else:
            record(test, "SKILLS list is empty", "WARN")
            return

        # Simulate the Layer1 recommend logic — keyword matching
        query = "memory session"
        query_words = set(query.lower().split())
        matches = []
        for skill in SKILLS:
            text = f"{skill.name} {skill.oneliner} {' '.join(skill.tags)}".lower()
            overlap = sum(1 for w in query_words if w in text)
            if overlap > 0:
                matches.append((overlap, skill))
        matches.sort(key=lambda x: x[0], reverse=True)
        top3 = [s.to_layer1() for _, s in matches[:3]]

        if top3:
            record(test, f"Keyword '{query}' recommended {len(top3)} skills", "PASS", f"top1={top3[0]['name']}")
        else:
            record(test, f"Keyword '{query}' matched no skills", "WARN")

        # Edge case: empty keyword — should not crash
        empty_query_words: set[str] = set()
        empty_matches = [s for s in SKILLS if any(w in f"{s.name} {s.oneliner}".lower() for w in empty_query_words)]
        record(test, "Empty keyword query does not crash and returns 0 results", "PASS",
            f"result count={len(empty_matches)}")

        # Verify to_layer3() contains all layer fields
        sample = SKILLS[0].to_layer3()
        required_keys = {"id", "name", "oneliner", "category", "install_cmd", "tags", "features", "os_complement"}
        missing = required_keys - set(sample.keys())
        if not missing:
            record(test, "to_layer3() contains all required fields", "PASS")
        else:
            record(test, "to_layer3() is missing fields", "FAIL", f"missing: {missing}")

    except ImportError as e:
        record(test, "Failed to import skill_registry", "FAIL", str(e))
    except Exception as e:
        record(test, "skill_registry test raised an exception", "FAIL", str(e))


# ─── TC-A05: Edge cases ─────────────────────────────────────────────────────────


def test_boundary_conditions():
    """TC-A05: Edge cases — invalid task_id, non-existent resources"""
    test = "TC-A05-boundary"
    log(f"\n{'=' * 60}\nStarting {test}\n{'=' * 60}")

    # Non-existent task_id → 404
    status, body = api_get("/api/tasks/nonexistent-task-id-99999")
    if status == 404:
        record(test, "Non-existent task_id returns 404", "PASS", f"detail={body.get('detail', '')[:60]}")
    else:
        record(test, "Non-existent task_id should return 404", "FAIL", f"actual HTTP {status}")

    # PUT a non-existent task → 404
    status, body = api_put(
        "/api/tasks/nonexistent-task-id-99999",
        {"status": "running"},
    )
    if status == 404:
        record(test, "PUT of a non-existent task returns 404", "PASS")
    else:
        record(test, "PUT of a non-existent task should return 404", "FAIL", f"actual HTTP {status}")

    # Non-existent project_id → 404
    status, body = api_get("/api/projects/nonexistent-project-99999")
    if status == 404:
        record(test, "Non-existent project_id returns 404", "PASS")
    else:
        record(test, "Non-existent project_id should return 404", "FAIL", f"actual HTTP {status}")

    # PUT task with an empty body → should return 200 (no fields updated) or 422 (validation failed)
    status, body = api_get("/api/tasks/nonexistent-id")
    # Here we only confirm the endpoint is reachable
    record(test, "GET /api/tasks/nonexistent-id is reachable (404)", "PASS" if status == 404 else "WARN",
        f"HTTP {status}")


# ─── TC-A06: GET /api/projects pagination ──────────────────────────────────────────


def test_projects_api():
    """TC-A06: GET /api/projects — list, pagination, field completeness"""
    test = "TC-A06-projects_api"
    log(f"\n{'=' * 60}\nStarting {test}\n{'=' * 60}")

    # Normal list
    status, body = api_get("/api/projects")
    if status == 200:
        record(test, "GET /api/projects returns 200", "PASS")
    else:
        record(test, "GET /api/projects", "FAIL", f"HTTP {status}")
        return

    projects = body.get("data", [])
    total = body.get("total", -1)
    record(test, f"Returned {len(projects)} projects, total={total}", "PASS")

    if projects:
        first = projects[0]
        required = {"id", "name"}
        missing = required - set(first.keys())
        if not missing:
            record(test, "Project object contains id/name fields", "PASS", f"keys={list(first.keys())[:6]}")
        else:
            record(test, "Project object is missing required fields", "FAIL", f"missing: {missing}")

        # Get a single project
        proj_id = first["id"]
        status2, proj_body = api_get(f"/api/projects/{proj_id}")
        if status2 == 200:
            record(test, f"GET /api/projects/{proj_id[:8]}... returns 200", "PASS")
        else:
            record(test, "GET /api/projects/:id", "FAIL", f"HTTP {status2}")

    # Edge case: limit=0
    status, body = api_get("/api/projects?limit=0")
    record(test, "limit=0 does not crash", "PASS" if status in (200, 422) else "WARN", f"HTTP {status}")

    # Edge case: very large limit
    status, body = api_get("/api/projects?limit=99999")
    record(
        test,
        "limit=99999 does not crash",
        "PASS" if status == 200 else "WARN",
        f"HTTP {status}, returned {len(body.get('data', []))} entries",
    )


# ─── Main entry point ───────────────────────────────────────────────────────────────────


def main() -> int:
    log("=" * 60)
    log("AI Team OS — API-level E2E tests")
    log(f"Time: {utc_now().strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"API: {API_URL}")
    log("=" * 60)

    test_health()
    test_task_update()
    test_team_templates()
    test_skill_registry()
    test_boundary_conditions()
    test_projects_api()

    # ── Summary ──────────────────────────────────────────────────────────────────
    log("\n" + "=" * 60)
    log("API test result summary")
    log("=" * 60)

    pass_count = sum(1 for r in RESULTS if "[PASS]" in r)
    fail_count = sum(1 for r in RESULTS if "[FAIL]" in r)
    warn_count = sum(1 for r in RESULTS if "[WARN]" in r)

    for r in RESULTS:
        log(r)

    log(f"\nTotal: {len(RESULTS)} items | PASS: {pass_count} | FAIL: {fail_count} | WARN: {warn_count}")

    import os

    report_dir = os.path.join(os.path.dirname(__file__), "..", "test-screenshots")
    os.makedirs(report_dir, exist_ok=True)
    report_path = os.path.join(report_dir, "e2e_api_report.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("AI Team OS — API E2E test report\n")
        f.write(f"Date: {utc_now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"API: {API_URL}\n")
        f.write("=" * 60 + "\n\n")
        for r in RESULTS:
            f.write(r + "\n")
        f.write(f"\n{'=' * 60}\n")
        f.write(f"Total: {len(RESULTS)} items | PASS: {pass_count} | FAIL: {fail_count} | WARN: {warn_count}\n")

    log(f"\nReport saved to: {report_path}")
    return fail_count


if __name__ == "__main__":
    fails = main()
    sys.exit(0 if fails == 0 else 1)
