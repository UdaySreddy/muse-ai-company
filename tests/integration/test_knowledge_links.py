"""Knowledge layer P1a — cross-domain reference graph integration tests (docs/knowledge-layer-design.md).

Covered: extractor pure functions → automatic edge creation on memo/report writes → forward and reverse queries →
recursive fan-out backtracking.
"""

from __future__ import annotations

from aiteam.api.link_extract import extract_refs

# ── Extractor unit tests (pure functions, mixed Chinese and English) ──


def test_extract_wf_and_commit_with_fix_cue():
    text = "Fixed the team-creation broken link in wf_cbad7348-934, commit 3a31d8b has been committed and verified"
    refs = {(r.to_kind, r.to_id): r for r in extract_refs(text)}
    assert ("run", "wf_cbad7348-934") in refs
    assert refs[("run", "wf_cbad7348-934")].link_type == "fixes"
    assert ("commit", "3a31d8b") in refs
    assert refs[("commit", "3a31d8b")].link_type == "fixes"


def test_extract_bare_hex_without_cue_is_ignored():
    # A bare hex string with no commit context word (e.g. a log fragment) must not be falsely extracted
    refs = extract_refs("The token deadbeef99 appeared in the data of session abff40af")
    assert all(r.to_kind != "commit" for r in refs)


def test_extract_uuid_and_memory_link():
    text = (
        "Related task 8d1feb9e-9485-416f-9dae-c884598a1fb6,"
        "Background: see [[ai-team-os-attribution-principle]]"
    )
    kinds = {(r.to_kind, r.to_id) for r in extract_refs(text)}
    assert ("task", "8d1feb9e-9485-416f-9dae-c884598a1fb6") in kinds
    assert ("memory", "ai-team-os-attribution-principle") in kinds


def test_extract_dedup_and_context_snapshot():
    text = "wf_12345678 appears twice: wf_12345678 is also here"
    refs = extract_refs(text)
    assert len([r for r in refs if r.to_kind == "run"]) == 1
    assert "wf_12345678" in refs[0].context


def test_extract_empty_and_plain_chinese():
    assert extract_refs("") == []
    assert extract_refs("纯中文内容没有任何引用标识") == []


# ── Integration: writes automatically create edges → queries → fan-out ──


def test_memo_write_creates_links_and_fanout(repo_and_client):
    repo, client = repo_and_client
    import asyncio

    loop = asyncio.get_event_loop()
    project = loop.run_until_complete(
        repo.create_project(name="kl-test", root_path="/tmp/kl-test")
    )
    project_id = project.id
    team = loop.run_until_complete(
        repo.create_team(name="kl-team", mode="coordinate", project_id=project_id)
    )
    task = loop.run_until_complete(
        repo.create_task(
            team_id=team.id, title="Knowledge layer test task", project_id=project_id
        )
    )
    task_id = task.id

    # Write a memo: reference a run and a commit (with a fix context)
    r = client.post(
        f"/api/tasks/{task_id}/memo",
        json={
            "content": "Fixed the broken link in wf_deadbeef, commit abc1234 committed",
            "author": "leader",
            "type": "progress",
        },
    )
    assert r.status_code == 200

    # Forward lookup: who references this run (in direction)
    links = client.get(
        "/api/links", params={"kind": "run", "id": "wf_deadbeef", "direction": "in"}
    ).json()["data"]
    assert len(links) == 1
    assert links[0]["from_kind"] == "task_memo"
    assert links[0]["link_type"] == "fixes"
    assert links[0]["project_id"] == project_id

    # Fan-out: starting from the commit, the run should be reachable in 2 hops (via the same memo)
    fanout = client.get(
        "/api/links/fanout", params={"kind": "commit", "id": "abc1234", "depth": 2}
    ).json()["data"]
    reachable = {(n["kind"], n["id"]) for n in fanout}
    assert ("task_memo", links[0]["from_id"]) in reachable
    assert ("run", "wf_deadbeef") in reachable


def test_duplicate_memo_links_are_idempotent(repo_and_client):
    repo, client = repo_and_client
    # Idempotency verification targets UNIQUE conflicts for the same from_id; simulate a rescan rerun by inserting
    # directly via the repo
    import asyncio

    from aiteam.types import KnowledgeLink

    lk = KnowledgeLink(
        from_kind="report",
        from_id="rep-1",
        to_kind="run",
        to_id="wf_11112222",
        link_source="regex-report",
    )
    loop = asyncio.get_event_loop()
    n1 = loop.run_until_complete(repo.insert_knowledge_links([lk]))
    n2 = loop.run_until_complete(repo.insert_knowledge_links([lk]))
    assert n1 == 1 and n2 == 0  # A rerun adds nothing new


def test_report_write_creates_links(repo_and_client):
    repo, client = repo_and_client
    r = client.post(
        "/api/reports",
        json={
            "author": "leader",
            "topic": "kl-report",
            "content": "Analyzed the run data of wf_aabbccdd, see [[some-memory-note]]",
        },
    )
    assert r.status_code == 201
    links = client.get(
        "/api/links", params={"kind": "run", "id": "wf_aabbccdd", "direction": "in"}
    ).json()["data"]
    assert len(links) == 1
    assert links[0]["from_kind"] == "report"
    mem = client.get(
        "/api/links",
        params={"kind": "memory", "id": "some-memory-note", "direction": "in"},
    ).json()["data"]
    assert len(mem) == 1


# ── P1b: Unified search, three-arm RRF ──


def test_unified_search_bm25_chinese(repo_and_client):
    repo, client = repo_and_client
    import asyncio

    loop = asyncio.get_event_loop()
    project = loop.run_until_complete(
        repo.create_project(name="搜索测试", root_path="/tmp/us-test")
    )
    team = loop.run_until_complete(
        repo.create_team(name="us-team", mode="coordinate", project_id=project.id)
    )
    task = loop.run_until_complete(
        repo.create_task(
            team_id=team.id, title="火星归属修复批次", project_id=project.id
        )
    )
    client.post(
        f"/api/tasks/{task.id}/memo",
        json={
            "content": "火星 workflow 误入 OS 项目，归属改文件真相源后迁回火星项目",
            "author": "leader",
            "type": "progress",
        },
    )

    # 中文查询（bigram 命中 memo 与任务标题）
    r = client.get("/api/search", params={"q": "火星归属"}).json()["data"]
    assert r, "中文 bigram 查询应有结果"
    kinds = {x["kind"] for x in r}
    assert "task" in kinds or "task_memo" in kinds
    assert any("火星" in x["snippet"] or "火星" in x["title"] for x in r)


def test_unified_search_graph_arm_by_id(repo_and_client):
    repo, client = repo_and_client
    import asyncio

    loop = asyncio.get_event_loop()
    project = loop.run_until_complete(
        repo.create_project(name="Graph arm test", root_path="/tmp/us-graph")
    )
    team = loop.run_until_complete(
        repo.create_team(name="ga-team", mode="coordinate", project_id=project.id)
    )
    task = loop.run_until_complete(
        repo.create_task(team_id=team.id, title="Observability fix", project_id=project.id)
    )
    client.post(
        f"/api/tasks/{task.id}/memo",
        json={
            "content": "Fixed the broken-link issue in wf_99887766, commit fedcba9 committed",
            "author": "leader",
            "type": "progress",
        },
    )

    # Query by run ID → the graph arm should recall the memo that references it
    r = client.get("/api/search", params={"q": "wf_99887766"}).json()["data"]
    assert any(x["kind"] == "task_memo" for x in r), "The graph arm should recall the memo that references this run"

    # Query by the associated commit → the same memo is still reachable within 2 hops of fan-out
    r2 = client.get("/api/search", params={"q": "fedcba9"}).json()["data"]
    assert any(x["kind"] == "task_memo" for x in r2)


def test_unified_search_empty_and_no_hit(repo_and_client):
    repo, client = repo_and_client
    r = client.get("/api/search", params={"q": "zzz nonexistent query term xyz"}).json()["data"]
    assert isinstance(r, list)
