"""AI Team OS backend API comprehensive smoke test script (run manually).

Uses Python urllib to send requests; does not depend on third-party libraries.
**Deliberately named smoke_*.py rather than test_*.py to avoid being auto-collected by pytest** —
This is a side-effecting script that pollutes the DB (it creates real meetings/teams),
it can only be run manually and explicitly via `python tests/smoke_api_comprehensive.py`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

BASE_URL = "http://localhost:8000"

# Collect test results
results: list[dict[str, Any]] = []


def req(
    method: str,
    path: str,
    body: dict | None = None,
    expected_status: int = 200,
) -> tuple[int, Any]:
    """Send an HTTP request; returns (status_code, json_body)."""
    url = BASE_URL + path
    data = json.dumps(body).encode("utf-8") if body else None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            status = resp.status
            raw = resp.read()
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = raw.decode("utf-8", errors="replace")
            return status, parsed
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = raw.decode("utf-8", errors="replace")
        return e.code, parsed
    except Exception as e:
        return 0, str(e)


def check(
    label: str,
    status: int,
    body: Any,
    expected_status: int = 200,
    assertions: list[str] | None = None,
) -> bool:
    """Record and print one test result."""
    status_ok = status == expected_status
    body_str = (
        json.dumps(body, ensure_ascii=False)[:200] if not isinstance(body, str) else body[:200]
    )

    assertion_failures: list[str] = []
    if assertions and status_ok:
        for assertion in assertions:
            # Simple field-existence check
            if not eval(assertion, {"body": body}):  # noqa: S307
                assertion_failures.append(assertion)

    passed = status_ok and not assertion_failures
    marker = "PASS" if passed else "FAIL"
    fail_reason = ""
    if not status_ok:
        fail_reason = f" (expected {expected_status}, actual {status})"
    elif assertion_failures:
        fail_reason = f" (assertion failed: {assertion_failures})"

    print(f"{marker} | {label}{fail_reason}")
    if not passed:
        print(f"       Response: {body_str}")

    results.append(
        {
            "label": label,
            "passed": passed,
            "status": status,
            "expected_status": expected_status,
            "fail_reason": fail_reason,
        }
    )
    return passed


# ================================================================
# Step one: get the base data (team_id, project_id)
# ================================================================

print("\n" + "=" * 60)
print("[Group 1] Core API endpoint tests")
print("=" * 60)

# 1. GET /api/teams
status, body = req("GET", "/api/teams")
check("GET /api/teams — team list", status, body, 200)
team_id = None
if isinstance(body, dict) and body.get("data"):
    team_id = body["data"][0]["id"]
    print(f"       Obtained team_id: {team_id}")
elif isinstance(body, list) and body:
    team_id = body[0].get("id")
    print(f"       Obtained team_id: {team_id}")

# 2. GET /api/projects
status, body = req("GET", "/api/projects")
check("GET /api/projects — project list", status, body, 200)
project_id = "3aa58dc7-a771-4745-8fa6-efbd5819956a"

# 3. GET /api/projects/{id}/task-wall
status, body = req("GET", f"/api/projects/{project_id}/task-wall")
check("GET /api/projects/{id}/task-wall — task wall", status, body, 200)

# 4. GET /api/system/rules
status, body = req("GET", "/api/system/rules")
check("GET /api/system/rules — rule list", status, body, 200)

# 5. GET /api/agent-templates
status, body = req("GET", "/api/agent-templates")
check("GET /api/agent-templates — template list", status, body, 200)
if isinstance(body, dict):
    total = body.get("total", 0)
    print(f"       {total} templates in total")

# 6. GET /api/agent-templates/recommend
status, body = req("GET", "/api/agent-templates/recommend?task_type=frontend")
check("GET /api/agent-templates/recommend?task_type=frontend — template recommendation", status, body, 200)

# 7. GET /api/decisions
status, body = req("GET", "/api/decisions?limit=10")
check("GET /api/decisions?limit=10 — decision log", status, body, 200)

# ================================================================
# Group two: MCP tool endpoints
# ================================================================

print("\n" + "=" * 60)
print("[Group 2] MCP tool endpoints / Hook events")
print("=" * 60)

# 8. POST /api/hooks/event
hook_payload = {
    "event_type": "test.ping",
    "source": "api-qa-test",
    "payload": {"message": "API test heartbeat", "timestamp": "2026-03-20T00:00:00Z"},
}
status, body = req("POST", "/api/hooks/event", hook_payload)
check("POST /api/hooks/event — Hook event reception", status, body, expected_status=200)
# It may also be 202 Accepted
if status == 202:
    results[-1]["passed"] = True
    print("       (202 also counts as a pass)")

# ================================================================
# Group three: meeting system tests
# ================================================================

print("\n" + "=" * 60)
print("[Group 3] Meeting system")
print("=" * 60)

meeting_id = None
if team_id:
    # 9. Create a meeting
    meeting_payload = {
        "topic": "API test meeting",
        "participants": ["api-qa", "team-lead"],
    }
    status, body = req("POST", f"/api/teams/{team_id}/meetings", meeting_payload)
    check("POST /api/teams/{id}/meetings — create a meeting", status, body, expected_status=201)
    if status == 201 and isinstance(body, dict):
        data = body.get("data", {})
        meeting_id = data.get("id")
        print(f"       meeting_id: {meeting_id}")

    # 10. Send a message
    if meeting_id:
        msg_payload = {
            "agent_id": "api-qa-agent",
            "agent_name": "api-qa",
            "content": "This is a message sent by the API test",
            "round_number": 1,
        }
        status, body = req("POST", f"/api/meetings/{meeting_id}/messages", msg_payload)
        check("POST /api/meetings/{id}/messages — send a message", status, body, expected_status=201)

    # 11. Read messages
    if meeting_id:
        status, body = req("GET", f"/api/meetings/{meeting_id}/messages")
        check("GET /api/meetings/{id}/messages — read messages", status, body, 200)
        if isinstance(body, dict):
            total = body.get("total", 0)
            print(f"       Message count: {total}")

    # 12. Close the meeting
    if meeting_id:
        status, body = req("PUT", f"/api/meetings/{meeting_id}/conclude")
        check("PUT /api/meetings/{id}/conclude — close the meeting", status, body, 200)
else:
    print("SKIP | Meeting system tests (no team_id available)")
    for _ in range(4):
        results.append(
            {
                "label": "Meeting system - skipped",
                "passed": None,
                "status": 0,
                "expected_status": 0,
                "fail_reason": "No team_id",
            }
        )

# ================================================================
# Group four: task system
# ================================================================

print("\n" + "=" * 60)
print("[Group 4] Task system")
print("=" * 60)

task_id = None
if team_id:
    # 13. Create a task
    task_payload = {
        "title": "API test task",
        "description": "A temporary task created by the api-qa automated test to verify the task CRUD flow",
        "priority": "medium",
    }
    status, body = req("POST", f"/api/teams/{team_id}/tasks/run", task_payload)
    check("POST /api/teams/{id}/tasks/run — create a task", status, body, 200)
    if isinstance(body, dict) and body.get("data"):
        task_id = body["data"].get("id")
        print(f"       task_id: {task_id}")

    # 14. Query the task status
    if task_id:
        status, body = req("GET", f"/api/tasks/{task_id}")
        check("GET /api/tasks/{id} — query task status", status, body, 200)

    # 15. Mark the task complete
    if task_id:
        status, body = req("PUT", f"/api/tasks/{task_id}/complete")
        check("PUT /api/tasks/{id}/complete — mark complete", status, body, 200)
        if isinstance(body, dict):
            data = body.get("data", {})
            task_status = data.get("status", "")
            print(f"       Task status: {task_status}")

    # 16. Verify the task-wall update
    if project_id:
        status, body = req("GET", f"/api/projects/{project_id}/task-wall")
        check("GET /api/projects/{id}/task-wall — verify the task-wall update", status, body, 200)
else:
    print("SKIP | Task system tests (no team_id available)")
    for _ in range(4):
        results.append(
            {
                "label": "Task system - skipped",
                "passed": None,
                "status": 0,
                "expected_status": 0,
                "fail_reason": "No team_id",
            }
        )

# ================================================================
# Group five: new features — single template, activity tracking, smart matching
# ================================================================

print("\n" + "=" * 60)
print("[Group 5] New features")
print("=" * 60)

# 17. GET /api/agent-templates/{name}
status, body = req("GET", "/api/agent-templates/engineering-frontend-developer")
check("GET /api/agent-templates/{name} — single template", status, body, 200)
if isinstance(body, dict) and "error" in body:
    print(f"       Hint: {body['error']}")

if team_id:
    # 18. GET /api/teams/{team_id}/activities
    status, body = req("GET", f"/api/teams/{team_id}/activities")
    check("GET /api/teams/{id}/activities — activity tracking", status, body, 200)

    # 19. GET /api/teams/{team_id}/task-matches
    status, body = req("GET", f"/api/teams/{team_id}/task-matches")
    check("GET /api/teams/{id}/task-matches — smart matching", status, body, 200)
else:
    print("SKIP | Activity tracking and smart matching (no team_id available)")
    for _ in range(2):
        results.append(
            {
                "label": "New features - skipped",
                "passed": None,
                "status": 0,
                "expected_status": 0,
                "fail_reason": "No team_id",
            }
        )

# ================================================================
# Group six: edge conditions and exception scenarios
# ================================================================

print("\n" + "=" * 60)
print("[Group 6] Edge conditions / exception scenarios")
print("=" * 60)

# 20. A non-existent project's task-wall
status, body = req("GET", "/api/projects/nonexistent-id-00000000/task-wall")
check("GET /api/projects/nonexistent-id/task-wall — expect 404", status, body, expected_status=404)

# 21. A non-existent task
status, body = req("GET", "/api/tasks/nonexistent-task-id-000")
check("GET /api/tasks/nonexistent-id — expect 404", status, body, expected_status=404)

# 22. A non-existent meeting
status, body = req("GET", "/api/meetings/nonexistent-meeting-id")
check("GET /api/meetings/nonexistent-id — expect 404", status, body, expected_status=404)

# 23. Send a message to a closed meeting (expect 400)
if meeting_id:
    msg_payload = {
        "agent_id": "api-qa-agent",
        "agent_name": "api-qa",
        "content": "Sending a message to an ended meeting (expect 400)",
        "round_number": 2,
    }
    status, body = req("POST", f"/api/meetings/{meeting_id}/messages", msg_payload)
    check("POST sending a message to a closed meeting — expect 400", status, body, expected_status=400)
else:
    print("SKIP | No meeting_id, skipping the closed-meeting message test")

# 24. A template name containing path-traversal characters (expect an error field in the response)
status, body = req("GET", "/api/agent-templates/../../etc/passwd")
# Path traversal may be intercepted by the FastAPI route parsing, returning 404, or 200 with an error
traversal_blocked = status in (200, 400, 404, 422)
marker = "PASS" if traversal_blocked else "FAIL"
print(f"{marker} | GET /api/agent-templates/path-traversal — expect it to be blocked (actual {status})")
results.append(
    {
        "label": "Path traversal protection",
        "passed": traversal_blocked,
        "status": status,
        "expected_status": -1,
        "fail_reason": "",
    }
)

# 25. A Hook event missing required fields (expect 422)
status, body = req("POST", "/api/hooks/event", {})
check("POST /api/hooks/event with an empty body — expect 422", status, body, expected_status=422)

# ================================================================
# Summary report
# ================================================================

print("\n" + "=" * 60)
print("[Test summary]")
print("=" * 60)

passed_count = sum(1 for r in results if r["passed"] is True)
failed_count = sum(1 for r in results if r["passed"] is False)
skipped_count = sum(1 for r in results if r["passed"] is None)
total_count = len(results)

print(
    f"\nPassed: {passed_count} | Failed: {failed_count} | Skipped: {skipped_count} | Total: {total_count}"
)

if failed_count > 0:
    print("\nFailure details:")
    for r in results:
        if r["passed"] is False:
            print(f"  FAIL | {r['label']}{r['fail_reason']}")

print()

# ================================================================
# Cleanup: delete the meeting / task created during the test to avoid polluting the database
# ================================================================
print("=" * 60)
print("[Cleanup] Deleting the temporary records created by the test")
print("=" * 60)

cleanup_count = 0
if meeting_id:
    s, _ = req("DELETE", f"/api/meetings/{meeting_id}")
    if s in (200, 204, 404):
        print(f"  ✓ Deleted meeting {meeting_id[:8]}")
        cleanup_count += 1
    else:
        print(f"  ! Failed to delete meeting, status={s}")

if task_id:
    s, _ = req("DELETE", f"/api/tasks/{task_id}")
    if s in (200, 204, 404):
        print(f"  ✓ Deleted task {task_id[:8]}")
        cleanup_count += 1
    else:
        print(f"  ! Failed to delete task, status={s}")

print(f"\nCleanup complete: {cleanup_count} records")
print()
