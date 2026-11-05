"""Direction-layer memory isolation for unregistered directories — REST end-to-end tests (root-cause fix for the
2026-07-21 cross-talk incident).

Covers:
- A scope=project write from an unregistered directory lands in a 'directory-fingerprint temporary bucket', with zero
pollution of the system global bucket;
- The read path is symmetric: this directory can read back its own temporary bucket, other directories cannot;
- Reads from unregistered directories still include the true global global/user entries;
- A scope=project bare write with no cwd (no X-Project-Dir header) is rejected with 422 and never lands in system;
- Registered project paths (X-Project-Id) are unaffected and still land in the project bucket (regression).

Uses TestClient + in-memory SQLite (conftest.integration_client).
"""

from __future__ import annotations

from aiteam.memory.scoping import dir_bucket_scope_id

_DIR_A = "/tmp/aiteam-unreg-a"
_DIR_B = "/tmp/aiteam-unreg-b"


def _project_ids(listed: dict) -> list[str]:
    return [m["id"] for m in listed["data"]]


def test_unregistered_write_lands_in_dir_bucket_not_system(integration_client) -> None:
    """A scope=project write from an unregistered directory → lands in the dir:<sha1> temporary bucket, with zero
    growth in the system bucket."""
    bucket = dir_bucket_scope_id(_DIR_A)
    assert bucket.startswith("dir:")

    resp = integration_client.post(
        "/api/memories",
        json={"content": "A constraint specific to this directory", "kind": "constraint", "scope": "project"},
        headers={"X-Project-Dir": _DIR_A},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    mem_id = body["data"]["id"]
    assert body["data"]["scope"] == "project"
    assert body["data"]["scope_id"] == bucket

    # Lands in the directory-fingerprint bucket
    in_bucket = integration_client.get(
        f"/api/memory?scope=project&scope_id={bucket}"
    ).json()
    assert mem_id in _project_ids(in_bucket)

    # Zero pollution of the system global bucket (the old bug silently landed here and broadcast to all sessions)
    in_system = integration_client.get(
        "/api/memory?scope=global&scope_id=system"
    ).json()
    assert mem_id not in _project_ids(in_system)


def test_read_symmetry_same_dir_reads_own_bucket(integration_client) -> None:
    """Writing a project memory in directory A → reading /api/memories with X-Project-Dir=A reads it back."""
    mem_id = integration_client.post(
        "/api/memories",
        json={"content": "Directory A's design intent", "kind": "design", "scope": "project"},
        headers={"X-Project-Dir": _DIR_A},
    ).json()["data"]["id"]

    listed_a = integration_client.get(
        "/api/memories", headers={"X-Project-Dir": _DIR_A}
    ).json()
    assert mem_id in _project_ids(listed_a)


def test_read_isolation_other_dir_cannot_see(integration_client) -> None:
    """Directory A's project memories are not visible to directory B (isolation is a directory's default right)."""
    mem_id = integration_client.post(
        "/api/memories",
        json={"content": "A's private constraint", "kind": "constraint", "scope": "project"},
        headers={"X-Project-Dir": _DIR_A},
    ).json()["data"]["id"]

    listed_b = integration_client.get(
        "/api/memories", headers={"X-Project-Dir": _DIR_B}
    ).json()
    assert mem_id not in _project_ids(listed_b)


def test_unregistered_read_still_includes_global(integration_client) -> None:
    """Reads from unregistered directories still include true global entries (legitimate global discipline is
    inherited as usual)."""
    global_id = integration_client.post(
        "/api/memories",
        json={"content": "All output in Chinese", "kind": "constraint", "scope": "global"},
    ).json()["data"]["id"]
    dir_id = integration_client.post(
        "/api/memories",
        json={"content": "Directory A preference", "kind": "preference", "scope": "project"},
        headers={"X-Project-Dir": _DIR_A},
    ).json()["data"]["id"]

    listed_a = integration_client.get(
        "/api/memories", headers={"X-Project-Dir": _DIR_A}
    ).json()
    ids_a = _project_ids(listed_a)
    assert global_id in ids_a  # Global inheritance
    assert dir_id in ids_a  # This directory's temporary bucket

    # Another directory B: sees the global entries, not A's temporary bucket
    listed_b = integration_client.get(
        "/api/memories", headers={"X-Project-Dir": _DIR_B}
    ).json()
    ids_b = _project_ids(listed_b)
    assert global_id in ids_b
    assert dir_id not in ids_b


def test_scope_project_without_cwd_rejected(integration_client) -> None:
    """A bare scope=project write with no X-Project-Dir → rejected with 422; it must never silently land in system."""
    resp = integration_client.post(
        "/api/memories",
        json={"content": "No directory context", "kind": "constraint", "scope": "project"},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "X-Project-Dir" in detail or "scope=global" in detail

    # Confirm no entry silently landed in the system bucket
    in_system = integration_client.get(
        "/api/memory?scope=global&scope_id=system"
    ).json()
    assert all(m["content"] != "No directory context" for m in in_system["data"])


def test_search_unregistered_dir_finds_own_bucket(integration_client) -> None:
    """GET /api/memory (BM25 search) for an unregistered directory with scope=project and a default scope_id → hits this
    directory's temporary bucket.

    Corresponding to a leader follow-up: the search endpoint originally went through a non-scoped repo, so unregistered
    directories could not find their own bucket.
    """
    integration_client.post(
        "/api/memories",
        json={"content": "probe-token dedicated entry", "kind": "preference", "scope": "project"},
        headers={"X-Project-Dir": _DIR_A},
    )

    # With a query (BM25 path)
    hit = integration_client.get(
        "/api/memory?scope=project&query=probe-token",
        headers={"X-Project-Dir": _DIR_A},
    ).json()
    assert any("probe-token" in m["content"] for m in hit["data"])

    # Without a query (list path) it should also hit
    listed = integration_client.get(
        "/api/memory?scope=project", headers={"X-Project-Dir": _DIR_A}
    ).json()
    assert any("probe-token" in m["content"] for m in listed["data"])


def test_search_cross_dir_isolation(integration_client) -> None:
    """search endpoint: entries in directory A's temporary bucket are not visible to directory B's project search."""
    integration_client.post(
        "/api/memories",
        json={"content": "onlyA-marker", "kind": "preference", "scope": "project"},
        headers={"X-Project-Dir": _DIR_A},
    )
    from_b = integration_client.get(
        "/api/memory?scope=project&query=onlyA-marker",
        headers={"X-Project-Dir": _DIR_B},
    ).json()
    assert all("onlyA-marker" not in m["content"] for m in from_b["data"])


def test_search_project_scope_without_cwd_rejected(integration_client) -> None:
    """search with scope=project, a default scope_id and no cwd → 422 (does not silently search system)."""
    resp = integration_client.get("/api/memory?scope=project&query=x")
    assert resp.status_code == 422


def test_search_explicit_scope_id_respected(integration_client) -> None:
    """An explicitly passed scope_id is always respected (regression for explicit paths such as the M4 inspection)."""
    # Explicit global/system
    integration_client.post(
        "/api/memories",
        json={"content": "global-explicit", "kind": "constraint", "scope": "global"},
    )
    got = integration_client.get(
        "/api/memory?scope=global&scope_id=system&query=global-explicit"
    ).json()
    assert any("global-explicit" in m["content"] for m in got["data"])

    # An explicit project scope_id (a real project bucket, not system) — respected directly, not overridden by context
    proj_id = "proj-explicit-search"
    integration_client.post(
        "/api/memories",
        json={"content": "proj-explicit", "kind": "constraint", "scope": "project"},
        headers={"X-Project-Id": proj_id},
    )
    got2 = integration_client.get(
        f"/api/memory?scope=project&scope_id={proj_id}&query=proj-explicit"
    ).json()
    assert any("proj-explicit" in m["content"] for m in got2["data"])


def test_registered_project_path_unaffected(integration_client) -> None:
    """A registered project (X-Project-Id) still reads and writes into the project bucket and does not take the
    dir-fingerprint branch (regression)."""
    proj_id = "proj-registered-xyz"
    mem_id = integration_client.post(
        "/api/memories",
        json={"content": "Registered project constraint", "kind": "constraint", "scope": "project"},
        headers={"X-Project-Id": proj_id},
    ).json()["data"]["id"]

    # Lands in the real project bucket (not a dir: prefix)
    in_proj = integration_client.get(
        f"/api/memory?scope=project&scope_id={proj_id}"
    ).json()
    assert mem_id in _project_ids(in_proj)

    # Reading /api/memories back with the same project id returns it
    listed = integration_client.get(
        "/api/memories", headers={"X-Project-Id": proj_id}
    ).json()
    assert mem_id in _project_ids(listed)
