#!/usr/bin/env python3
"""Workflow reminder — lightweight PreToolUse/PostToolUse hook.

Only reads/writes local state files and outputs reminders to stdout, no HTTP calls.
Goal is to complete within 100ms to avoid CC hook timeout.
Usage: python -m aiteam.hooks.workflow_reminder <PreToolUse|PostToolUse>
"""

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

_SUPERVISOR_STATE_DIR = os.path.join(os.path.expanduser("~"), ".claude", "data", "ai-team-os")
_SUPERVISOR_STATE_FILE = os.path.join(_SUPERVISOR_STATE_DIR, "supervisor-state.json")
_PORT_FILE = os.path.join(_SUPERVISOR_STATE_DIR, "api_port.txt")
_SUBAGENT_MARKER_DIR = os.path.join(_SUPERVISOR_STATE_DIR, "subagent_sessions")


def _safe_session_id(session_id: str) -> str:
    """Strip anything that isn't alphanumeric, hyphen, or underscore to prevent path traversal."""
    return re.sub(r"[^a-zA-Z0-9_-]", "", session_id)


def _is_subagent_session(session_id: str) -> bool:
    """Return True if the given session was marked as a sub-agent by SubagentStart."""
    if not session_id:
        return False
    safe_id = _safe_session_id(session_id)
    if not safe_id:
        return False
    try:
        return os.path.isfile(os.path.join(_SUBAGENT_MARKER_DIR, safe_id))
    except Exception:
        return False


def _run_git_readonly(args: list[str], cwd: str, timeout: float = 5.0) -> tuple[int, str]:
    """Run a read-only git command, returning (returncode, stripped stdout).

    Short timeout and narrow scope: this only ever runs after a rare, already-matched
    dangerous command pattern (S4 below), never on every Bash call, so it does not
    threaten this hook's general 100ms budget. Any failure (git missing, path not a
    repo, timeout) collapses to returncode 1 with empty output — callers must treat
    that as "cannot determine, do not block on this signal alone".
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "-C", cwd] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout.strip()
    except Exception:
        return 1, ""


def _main_branch_name(path: str) -> str:
    """Best-effort NAME of the repo's main/default branch (never a remote-tracking ref).

    2026-07 incident: this used to return an `origin/<branch>` ref directly and compare
    HEAD's ancestry against it. That's wrong for a batch-push workflow (push done by the
    user in batches, not on every commit) where `origin/<branch>` routinely lags the
    local branch by many commits — a worktree branch merged locally (real merge commit,
    HEAD is a genuine ancestor of local master) was reported as unlanded because it
    wasn't yet an ancestor of the stale `origin/master`, hard-blocking a normal
    post-merge cleanup. `origin/HEAD` (when configured) is only used here to learn the
    branch *name*; ancestry must always be checked against the local branch of that
    name, since a local merge is what "landed" means in this repo's workflow.
    """
    code, out = _run_git_readonly(["symbolic-ref", "refs/remotes/origin/HEAD"], cwd=path)
    if code == 0 and out:
        name = out.rsplit("/", 1)[-1]  # refs/remotes/origin/HEAD -> master
        verify_code, _ = _run_git_readonly(["rev-parse", "--verify", "--quiet", name], cwd=path)
        if verify_code == 0:
            return name
    for candidate in ("master", "main"):
        code, _ = _run_git_readonly(["rev-parse", "--verify", "--quiet", candidate], cwd=path)
        if code == 0:
            return candidate
    return ""  # nothing resolvable; ancestor check below treats this as "not landed"


def _cherry_lines(path: str, main_branch: str, head: str | None = None) -> list[str]:
    """Raw `git cherry <main_branch> [<head>]` output lines, or [] if it couldn't run.

    `head` defaults to HEAD (git's own default) when omitted -- pass it explicitly
    whenever `path`'s checked-out HEAD isn't necessarily the ref being assessed
    (e.g. the `git branch -D` path runs from the main checkout, not a checkout of
    the branch being deleted).
    """
    args = ["cherry", main_branch] + ([head] if head else [])
    code, out = _run_git_readonly(args, cwd=path, timeout=8.0)
    if code != 0 or not out:
        return []
    return out.splitlines()


def _all_commits_patch_equivalent(cherry_lines: list[str]) -> bool:
    """True only when every commit ahead of main_branch is patch-id-equivalent to
    something already in main_branch's history (`git cherry`, every line '-').

    Secondary landed signal, weaker than merge-base --is-ancestor: catches content
    that reached the main branch through a differently shaped commit (squash,
    rebase-and-recommit, independently re-authored) so the branch head is never
    literally an ancestor. 2026-07 real sample: worktree wf_a69e7d46-a66-2's two
    commits both patch-id-match master (both '-') despite predating a rebase that
    changed their hashes — task a1b6a1bf.

    Deliberately all-or-nothing: a MIXED result (some '-', some '+') is NOT treated
    as landed here, even if a '+' commit's content turns out to already be in
    master under inspection (real sample: wf_a69e7d46-a66-1's bfbc1d6 bundles 14
    file changes; master's current versions of all 14 are content-supersets of
    what bfbc1d6 added, but the whole-commit patch-id still misses because master
    kept evolving those same files afterward). Reliably telling "this specific
    '+' commit's effect is fully subsumed" apart from "this is genuinely missing
    work" would need per-file, order-independent hunk containment checking --
    not something to get wrong in a hard-block safety gate. A false ALLOW here
    loses work silently; a false BLOCK just costs one human --force after reading
    the diagnostic hint from _cherry_breakdown. The asymmetry is why mixed results
    stay conservative.
    """
    return bool(cherry_lines) and all(line.startswith("- ") for line in cherry_lines)


def _cherry_breakdown(cherry_lines: list[str]) -> str:
    """One-line diagnostic for a mixed/unmatched `git cherry` result, appended to
    the hard-block message so a human doesn't have to re-derive this by hand."""
    unmatched = [ln[2:14].strip() for ln in cherry_lines if ln.startswith("+ ")]
    matched_n = sum(1 for ln in cherry_lines if ln.startswith("- "))
    if not unmatched:
        return ""
    return (
        f"git cherry: {matched_n}/{len(cherry_lines)} commits are content-equivalent to the main branch, "
        f"{len(unmatched)} are not equivalent ({', '.join(unmatched)}) — "
        "please manually verify whether the files touched by these commits are already reflected on the main branch, "
        "and only --force after confirming they are"
    )


def _assess_unlanded_work(path: str) -> tuple[bool, str | None, str | None]:
    """Read-only "is it safe to tear down this worktree" assessment.

    Returns (dirty, unlanded_reason, warn_reason):
      - dirty: True if there are uncommitted/untracked changes in the working tree.
      - unlanded_reason: set (hard-block worthy) when there are commits reachable only
        from this branch, not yet merged into the base branch, AND not fully pushed to
        an upstream (i.e. would become unrecoverable if the worktree/branch is torn down).
      - warn_reason: set when commits are unmerged but already pushed to an upstream
        (recoverable from the remote, lower risk — advisory only, not a hard block).

    Never raises. If the target isn't a resolvable git worktree at all (bad path, git
    missing), all three come back as (False, None, None) — caller must not block on
    an assessment it could not actually perform.
    """
    status_code, status_out = _run_git_readonly(["status", "--porcelain"], cwd=path)
    if status_code != 0:
        # Not a valid worktree we can inspect — let git's own `worktree remove` surface
        # whatever error is appropriate; this guard has nothing reliable to add here.
        return False, None, None
    dirty = bool(status_out)

    main_branch = _main_branch_name(path)
    landed = False
    cherry_lines: list[str] = []
    if main_branch:
        ancestor_code, _ = _run_git_readonly(
            ["merge-base", "--is-ancestor", "HEAD", main_branch], cwd=path
        )
        landed = ancestor_code == 0
        if not landed:
            # Secondary signal: content-equivalent by patch-id even though HEAD
            # isn't literally an ancestor (squash/rebase-elsewhere landed it).
            # Only trusted when EVERY commit matches; see _all_commits_patch_equivalent
            # for why a mixed result deliberately stays unlanded.
            cherry_lines = _cherry_lines(path, main_branch)
            landed = _all_commits_patch_equivalent(cherry_lines)

    if landed:
        return dirty, None, None

    upstream_code, _ = _run_git_readonly(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd=path
    )
    if upstream_code == 0:
        ahead_code, ahead_out = _run_git_readonly(["rev-list", "@{u}..HEAD"], cwd=path)
        pushed = ahead_code == 0 and not ahead_out
        if pushed:
            return dirty, None, "pushed to the remote but not yet merged into the base branch"

    reason = "There are local unpushed/unmerged commits"
    hint = _cherry_breakdown(cherry_lines)
    if hint:
        reason = f"{reason} ({hint})"
    return dirty, reason, None


def _get_api_url() -> str:
    """Return current API URL. AITEAM_API_URL env var takes highest priority."""
    env_url = os.environ.get("AITEAM_API_URL")
    if env_url:
        return env_url
    try:
        port = int(open(_PORT_FILE).read().strip())
        return f"http://localhost:{port}"
    except (FileNotFoundError, ValueError):
        return "http://localhost:8000"

# Threshold for Leader delegation check
_LEADER_CONSECUTIVE_THRESHOLD = 8

# Tool names considered "delegation" actions (calling these resets the counter)
# Workflow = CC's ultracode orchestration tool. When the Leader calls it, that is delegating execution (handing it to
# CC's built-in workflow),
# which is a delegation action just like direct Agent dispatch, so it should reset the B0.9 "keeps doing it
# themselves" counter
# and stop nagging the Leader with "why not delegate". The task-on-wall (task_create) reminder is unaffected and stays
# as usual.
# TeamCreate no longer exists as of CC v2.1.219 (tool-surface check 2026-07-25); keeping it in the set is
# harmless but meaningless, so it was removed; the sole dispatch entry point is Agent.
_DELEGATION_TOOLS = {"Agent", "SendMessage", "Workflow"}

# Infrastructure tools only Leader can do — don't count toward B0.9 threshold
_INFRA_TOOLS = {
    # MCP task management (Leader managing task wall)
    "mcp__ai-team-os__task_create", "mcp__ai-team-os__task_update",
    "mcp__ai-team-os__task_list_project", "mcp__ai-team-os__task_status",
    # MCP team/project management
    "mcp__ai-team-os__team_create", "mcp__ai-team-os__team_list",
    "mcp__ai-team-os__agent_template_recommend", "mcp__ai-team-os__agent_template_list",
    # MCP meeting management
    "mcp__ai-team-os__meeting_create", "mcp__ai-team-os__meeting_conclude",
}


_API_TIMEOUT = 2


def _api_call(method: str, path: str, body: dict | None = None, project_id: str | None = None) -> dict | None:
    """Make a JSON API call to the OS backend. Returns parsed response or None on failure."""
    api_url = _get_api_url()
    url = f"{api_url}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if project_id:
        headers["X-Project-Id"] = project_id
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


_PROJECT_ID_CACHE_TTL = 300  # 5 minutes


def _resolve_project_id() -> str | None:
    """Resolve current project ID from cwd via OS API, with file-based cache (TTL 5 min)."""
    # Check cache first
    state = _load_supervisor_state()
    cached = state.get("cached_project_id")
    cached_at = state.get("cached_project_id_at", 0)
    if cached and (time.time() - cached_at) < _PROJECT_ID_CACHE_TTL:
        return cached

    # Resolve via API
    api_url = _get_api_url()
    cwd = os.getcwd()
    try:
        req = urllib.request.Request(
            f"{api_url}/api/context/resolve",
            data=json.dumps({"cwd": cwd, "auto_create": False}).encode(),  # ownership iron rule: never auto-create a
            # project
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        project_id = data.get("project_id") or data.get("project", {}).get("id")

        # Cache result
        if project_id:
            state["cached_project_id"] = project_id
            state["cached_project_id_at"] = time.time()
            _save_supervisor_state(state)

        return project_id
    except Exception:
        return cached  # Return stale cache on failure


def _get_running_pipeline_subtask(
    api_url: str, project_id: str | None = None
) -> tuple[str | None, str | None, str | None, str | None]:
    """Return (subtask_id, parent_task_id, stage_name, next_stage_name) for the current running pipeline.

    Scans active teams for a running task with a pipeline, finds the current pending/running stage,
    and returns its subtask_id. Returns (None, None, None, None) when not found.
    """
    try:
        headers: dict[str, str] = {}
        if project_id:
            headers["X-Project-Id"] = project_id
        req = urllib.request.Request(f"{api_url}/api/teams", method="GET", headers=headers)
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            teams = json.loads(resp.read().decode()).get("data", [])
        active_teams = [t for t in teams if t.get("status") == "active"]
        if not active_teams:
            return None, None, None, None

        team_id = active_teams[0].get("id", "")
        if not team_id:
            return None, None, None, None

        req2 = urllib.request.Request(f"{api_url}/api/teams/{team_id}/tasks", method="GET", headers=headers)
        with urllib.request.urlopen(req2, timeout=_API_TIMEOUT) as resp2:
            tasks = json.loads(resp2.read().decode()).get("data", [])

        for task in tasks:
            if task.get("status") not in ("running", "in_progress"):
                continue
            pipeline = (task.get("config") or {}).get("pipeline")
            if not pipeline:
                continue

            stages = pipeline.get("stages", [])
            current_idx = pipeline.get("current_stage_index", 0)
            if current_idx >= len(stages):
                continue

            current_stage = stages[current_idx]
            subtask_id = current_stage.get("subtask_id")
            stage_name = current_stage.get("name", "")

            # Find next stage name
            next_stage_name = None
            for s in stages[current_idx + 1:]:
                if s.get("status") != "skipped":
                    next_stage_name = s.get("name")
                    break

            return subtask_id, task.get("id"), stage_name, next_stage_name

    except Exception:
        pass

    return None, None, None, None


def _bind_subtask_running(api_url: str, project_id: str | None = None) -> str | None:
    """Advisory-only detection of the current pipeline stage subtask on agent dispatch.

    The pipeline has been retired (design doc §7; aligned with pipeline_gate.py:413-419's retirement semantics): no
    longer automatically
    PUTs a subtask to running. The original DB write carried the active_teams[0] heuristic mis-binding risk — a
    dispatched agent does not necessarily
    belong to that pipeline, yet unrelated subtasks would be marked running. Now it only read-only probes legacy
    pipelines and returns a hint
    text for the Leader to decide; returns None when there is no pipeline.
    """
    subtask_id, _parent_task_id, stage_name, _ = _get_running_pipeline_subtask(api_url, project_id=project_id)
    if not subtask_id:
        return None
    return (
        f"Detected a legacy pipeline subtask {subtask_id} (stage: {stage_name})."
        "The hook will not automatically set it to running; use task_update yourself if you need to track it."
    )


def _advance_pipeline_on_completion(api_url: str, project_id: str | None = None) -> str | None:
    """Advisory-only detection of a legacy pipeline when an agent reports completion.

    The pipeline has been retired (design doc §7; aligned with pipeline_gate.py:413-419's retirement semantics): no
    longer automatically
    PUTs a subtask to completed, nor POSTs advance. The original DB write had a dual defect — SendMessage
    completion-keyword
    misjudgment (a Leader saying "report back when done" also triggered it) + the active_teams[0] heuristic
    mis-binding; together they could forge pipeline
    advancement. Now it only read-only probes legacy pipelines and hints; returns None when there is no pipeline.
    """
    subtask_id, parent_task_id, stage_name, next_stage_name = _get_running_pipeline_subtask(
        api_url, project_id=project_id
    )
    if not subtask_id or not parent_task_id:
        return None

    if next_stage_name:
        return (
            f"[OS Reminder] Detected legacy pipeline stage '{stage_name}' → '{next_stage_name}'."
            "The hook will not automatically advance it; use task_update yourself once you confirm completion."
        )
    return (
        f"[OS Reminder] Detected the legacy pipeline's final phase '{stage_name}'."
        "The hook will not automatically wrap it up; use task_update yourself once you confirm completion."
    )


def _load_supervisor_state() -> dict:
    """Load supervisor state file; return default value if missing or corrupted."""
    try:
        with open(_SUPERVISOR_STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_supervisor_state(state: dict) -> None:
    """Save supervisor state to file."""
    try:
        os.makedirs(_SUPERVISOR_STATE_DIR, exist_ok=True)
        with open(_SUPERVISOR_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except OSError:
        pass


# Session-level throttle sub-buckets for nagging reminders (supervisor-state.json is a cross-session global file;
# nagging counts /
# shown flags must be isolated per session, otherwise "at most N times per session" is meaningless). Session buckets
# untouched for 24h
# are pruned automatically, so the global state file does not bloat indefinitely with the session count.
_SESSION_BUCKET_TTL = 24 * 3600


def _session_bucket(state: dict, session_id: str) -> dict:
    """Return (lazily create) the per-session throttle sub-dict for catch-up reminders."""
    sid = _safe_session_id(session_id) or "unknown"
    buckets = state.get("session_scoped")
    if not isinstance(buckets, dict):
        buckets = {}
        state["session_scoped"] = buckets
    now = time.time()
    # Prune stale session buckets so the global state file stays bounded.
    for key in list(buckets.keys()):
        b = buckets.get(key)
        if not isinstance(b, dict) or (now - b.get("_ts", 0)) > _SESSION_BUCKET_TTL:
            buckets.pop(key, None)
    bucket = buckets.get(sid)
    if not isinstance(bucket, dict):
        bucket = {}
        buckets[sid] = bucket
    bucket["_ts"] = now
    return bucket


# The two timelines of A1'(S5)'s commit-time branch ownership assertion (debate 503e07f1, topic A ruling).
# Deliberately two different values, not one:
#   ACTIVE — within this window a claim counts as "a living person is still on this branch", and others running into
# it are hard-blocked.
#   PRUNE  — the line at which records are deleted outright; must be significantly longer than ACTIVE. If the two took
# the same value, expired records would
#            vanish at pruning time too, and "another's record expired → downgrade to a warning" could never be
# emitted (a silent pass),
#            and a legitimate taker-over would also lose the hint "the branch you are taking over was in someone
# else's hands".
_BRANCH_OWNERSHIP_ACTIVE_TTL = 24 * 3600
_BRANCH_OWNERSHIP_PRUNE_TTL = 7 * 24 * 3600

# Only recognizes a git commit that truly appears at a command start (after a line start / pipe / semicolon /
# sub-shell),
# avoiding literals like `echo "git commit"` being taken for a commit. `-C <dir>` and `-c k=v`
# are global options that really occur at commit time; consuming them too is required to get the correct probe
# directory.
_GIT_COMMIT_RE = re.compile(
    r"(?:\A|[\n;&|(])\s*git\s+(?:-C\s+(?P<cdir>\S+)\s+)?(?:-c\s+\S+\s+)*commit\b"
)


def _ownership_bucket(state: dict, checkout: str) -> dict:
    """Return (lazily create) the per-checkout ``{agent_id: {branch, ts}}`` claim map.

    Prunes claims older than _BRANCH_OWNERSHIP_PRUNE_TTL on every access, same
    shape as _session_bucket's TTL sweep - supervisor-state.json is a single
    global file and every agent that ever committed would otherwise stay in it
    forever.
    """
    root = state.get("branch_ownership")
    if not isinstance(root, dict):
        root = {}
        state["branch_ownership"] = root
    now = time.time()
    for ck in list(root.keys()):
        claims = root.get(ck)
        if not isinstance(claims, dict):
            root.pop(ck, None)
            continue
        for aid in list(claims.keys()):
            rec = claims.get(aid)
            if not isinstance(rec, dict) or (now - rec.get("ts", 0)) > _BRANCH_OWNERSHIP_PRUNE_TTL:
                claims.pop(aid, None)
        if not claims and ck != checkout:
            root.pop(ck, None)
    bucket = root.get(checkout)
    if not isinstance(bucket, dict):
        bucket = {}
        root[checkout] = bucket
    return bucket


def _check_commit_branch_ownership(
    event_data: dict, state: dict, cmd: str, base_cwd: str
) -> list[str]:
    """S5: assert, at commit time, that HEAD still belongs to the committing agent.

    Rationale (2026-07-10 incident): two CC sessions shared one checkout, one
    switched branches, and the other's commits silently landed on that branch -
    "code disappeared" until reflog recovery. The cheapest reliable moment to
    catch this is the commit itself: one read-only `git rev-parse` says which
    branch the commit is about to land on, and this hook's own state file says
    who claimed it.

    Deliberately self-contained: state lives in supervisor-state.json, no API
    call, no lock file. Ownership arbitration through a new runtime lock file is
    forbidden by the same ruling; a claim record is an observation, not a lock -
    it never gates anything by itself, it only decides warn vs. block.

    Semantics, in evaluation order:
      * git probe fails / detached HEAD -> fail loud. Neither bless nor block:
        the warning states outright that the check could not run, and nothing is
        recorded (recording a branch we could not read would cement a wrong owner).
      * HEAD == my own claim -> silent, claim refreshed. Refreshing matters: an
        agent working a long stretch on its own branch must not age out and lose
        the branch to whoever wanders in next.
      * HEAD == another agent's claim, claim younger than ACTIVE_TTL -> exit(2).
      * HEAD == another agent's claim, claim older than ACTIVE_TTL -> warn only.
        A dead agent's stale claim must not permanently fence off a branch a
        legitimate successor is picking up.
      * HEAD != my claim (nobody else owns it) -> loud warning showing both the
        recorded branch and current HEAD, never a block. Switching branches on
        purpose is legal; doing it without noticing is the failure mode.
      * no claim yet -> record (checkout, agent) -> branch + timestamp, silent.

    The claim is written once, on first commit, and is never rewritten to a new
    branch - so the self-switch warning keeps firing for as long as the mismatch
    lasts, instead of going quiet after one commit. The cost is that a branch an
    agent switched to is left unclaimed; that is the ruling's shape, and the
    unclaimed side degrades to "no signal", never to a wrong block.
    """
    m = _GIT_COMMIT_RE.search(cmd)
    if not m:
        return []

    cdir = m.group("cdir")
    probe_cwd = (
        os.path.abspath(os.path.join(base_cwd, cdir.strip("'\"")))
        if cdir
        else base_cwd
    )
    agent_id = _safe_session_id(event_data.get("session_id", "")) or "unknown"

    # One read-only call for both facts: repo root (the checkout identity - a cwd
    # deep inside the tree must not fragment into its own key) and branch name.
    code, out = _run_git_readonly(
        ["rev-parse", "--show-toplevel", "--abbrev-ref", "HEAD"], cwd=probe_cwd
    )
    lines = out.splitlines() if out else []
    if code != 0 or len(lines) < 2 or not lines[0].strip() or not lines[1].strip():
        return [
            f"[Security] The branch ownership check could not be executed: the git probe failed (cwd={probe_cwd})."
            "This commit has been neither passed nor blocked — run git rev-parse --abbrev-ref HEAD yourself "
            "to confirm the current branch really is the one you are using, then decide whether to commit."
        ]

    checkout, branch = lines[0].strip(), lines[1].strip()
    if branch == "HEAD":
        return [
            f"[Security] The branch ownership check could not be executed: {checkout} is in a detached HEAD state, "
            "with no branch name to compare. This commit has been neither passed nor blocked — a detached commit "
            "belongs to no branch, "
            "please confirm this is the state you want first."
        ]

    bucket = _ownership_bucket(state, checkout)
    now = time.time()
    warnings: list[str] = []

    mine = bucket.get(agent_id)
    if isinstance(mine, dict) and mine.get("branch") == branch:
        mine["ts"] = now
        return warnings

    for other_id, rec in bucket.items():
        if other_id == agent_id or not isinstance(rec, dict) or rec.get("branch") != branch:
            continue
        age = now - rec.get("ts", 0)
        age_h = age / 3600
        if age <= _BRANCH_OWNERSHIP_ACTIVE_TTL:
            sys.stderr.write(
                f"[OS BLOCK] Branch ownership conflict: {checkout}'s current HEAD is '{branch}', "
                f"and this branch was claimed by agent {other_id} {age_h:.1f} hours ago and is still within its "
                f"validity period."
                "You are about to land a commit on someone else's branch (code was lost exactly this way on "
                "2026-07-10)."
                "First open your own isolated workspace with git worktree add, or have the owner operate it after "
                "confirming with them — "
                "do not replay this blocked command."
            )
            sys.exit(2)
        warnings.append(
            f"[Security] The branch ownership claim on '{branch}' in {checkout} was made by agent {other_id}, "
            f"but that record has expired ({age_h:.1f} hours ago, over the {_BRANCH_OWNERSHIP_ACTIVE_TTL // 3600}h "
            f"limit) — "
            "treated as a legitimate takeover, not blocked. If the other party is actually still alive, confirm "
            "before committing."
        )

    if isinstance(mine, dict):
        warnings.append(
            f"[Security] Warning on branch ownership: the branch you recorded at your first commit in {checkout} was "
            f"'{mine.get('branch')}', but the current HEAD is '{branch}'."
            "The branch was switched without you noticing (the classic symptom of one checkout shared by several "
            "parties)."
            "Confirm this is the branch you want to commit to before continuing; if not, git checkout back first."
        )
    else:
        bucket[agent_id] = {"branch": branch, "ts": now}

    return warnings


def _is_taskwall_tool(tool_name: str) -> bool:
    """True for any OS task-wall operation (task_* / taskwall_*, prefixed or bare).

    Operating the task wall — creating / updating / reading / memoing tasks — should
    reset the "hasn't looked at the task wall in a while" catch-up timer, not only the two explicit view tools.
    Otherwise a session actively managing the wall via task_create/task_update/… still
    gets nagged indefinitely.
    """
    base = tool_name.split("mcp__ai-team-os__", 1)[-1]
    return base.startswith("task_") or base.startswith("taskwall_")


def _check_agent_team_name(event_data: dict) -> str | None:
    """Direct Agent dispatch check (2026-07-22 interception-retired version). Return warning text or None.

    History: it once unconditionally hard-blocked implementation-type direct dispatches without a team_name via
    exit(2) ("local agents are untrackable,
    dispatch forbidden"). On 2026-07-22 the Creator ruled "fully open + uniformly auto-tracked" (task 8705dac2,
    direction memory a67fb0de): hook_translator._on_subagent_start now automatically absorbs direct-dispatch agents
    with no team to belong to
    into the session-<sid8> container team — the "untrackable" premise vanished, and the hard block retired with it.
    Two existing guardrails are kept:
      1. the misuse reminder for explore/plan + team_name (the built-in read-only types do not support SendMessage);
      2. the cross-project dispatch interception for an explicit team_name (the 2026-05-08 leak-incident line of
      defense, untouched).
    """
    tool_name = event_data.get("tool_name", "")
    if tool_name != "Agent":
        return None

    tool_input_dict = event_data.get("tool_input", {})

    readonly_builtins = ["explore", "plan"]  # CC built-in read-only
    subagent_type = tool_input_dict.get("subagent_type", "").lower()
    has_team = bool(tool_input_dict.get("team_name"))
    if subagent_type in readonly_builtins and has_team:
        return (
            "[OS Reminder] Explore/Plan are CC's built-in read-only types and do not support SendMessage team "
            "communication; "
            "if you need back-and-forth communication, use another subagent_type."
        )

    # Explicit team_name → cross-project guardrail (prevents a Leader in project A from dispatching agents into a team
    # of project B).
    team_name = tool_input_dict.get("team_name")
    if team_name:
        cross_project_warn = _check_team_cross_project(team_name)
        if cross_project_warn:
            sys.stderr.write(cross_project_warn)
            sys.exit(2)

    # No team_name (including implementation-type) is uniformly passed — SubagentStart automatically absorbs them into
    # this session's container team.
    return None


def _check_team_cross_project(team_name: str) -> str | None:
    """v1.5.2: Verify the team belongs to the current cwd's project.

    Returns a [OS BLOCK] message string when team.project_id != current project,
    or None when the team is valid for current cwd.

    Bypass: if current cwd has no registered project, skip the check.
    """
    current_pid = _resolve_project_id()
    if not current_pid:
        return None  # No project context — allow (rare, e.g. fresh env)
    api_url = _get_api_url()
    try:
        # Fetch all teams (could be filtered server-side if API supports name)
        req = urllib.request.Request(f"{api_url}/api/teams", method="GET")
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            teams = json.loads(resp.read().decode("utf-8")).get("data", [])
        team = next((t for t in teams if t.get("name") == team_name), None)
        if team is None:
            return None  # Team not found in DB — TeamCreate will handle (let it through)
        team_pid = team.get("project_id")
        if team_pid and team_pid != current_pid:
            return (
                f"[OS BLOCK] Cross-project dispatch blocked: team='{team_name}' belongs to project {team_pid[:8]}, "
                f"but the current cwd project is {current_pid[:8]}. Please use a team from this project, "
                f"or cd to the correct project directory first."
            )
        return None
    except Exception:
        return None  # API unavailable — fail open (don't block legit work)


def _norm_team_key(name: str) -> str:
    """Normalize a team name for OS↔CC matching.

    Mirrors state_reaper._check_team_liveness's cc_dir_name convention
    (name.lower().replace(" ", "-")) so a TeamDelete identifier can be matched
    against OS team names regardless of case/space-vs-hyphen differences.
    """
    return (name or "").lower().replace(" ", "-")


def _extract_team_identifier(tool_input: dict) -> str | None:
    """Best-effort extract the target team's name/id from a TeamDelete tool_input.

    CC's TeamDelete parameter schema isn't guaranteed available to this hook, so
    probe the conventional keys (team_name mirrors TeamCreate). Returns the first
    non-empty string value, or None when nothing usable is present — the caller
    must then fall back to advisory-only (never a blind cross-team write).
    """
    if not isinstance(tool_input, dict):
        return None
    for key in ("team_name", "name", "team_id", "id"):
        val = tool_input.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _check_leader_doing_too_much(event_data: dict, state: dict) -> str | None:
    """Check if Leader is making too many consecutive tool calls without delegating.

    Returns warning text when consecutive non-delegation tool calls exceed threshold.
    Resets counter when Leader calls Agent/TeamCreate/SendMessage.
    Skipped for sub-agent sessions (marked by SubagentStart hook) — hands-on work
    is their job, not a delegation failure.
    """
    tool_name = event_data.get("tool_name", "")
    if not tool_name:
        return None

    if _is_subagent_session(event_data.get("session_id", "")):
        return None

    consecutive = state.get("leader_consecutive_calls", 0)

    if tool_name in _DELEGATION_TOOLS:
        state["leader_consecutive_calls"] = 0
        return None

    # Infrastructure tools (task wall, team mgmt) don't count toward threshold
    if tool_name in _INFRA_TOOLS:
        return None

    consecutive += 1
    state["leader_consecutive_calls"] = consecutive

    # Remind once at threshold+1, then every 10 calls after (avoid noise)
    over = consecutive - _LEADER_CONSECUTIVE_THRESHOLD
    if over == 1 or (over > 1 and over % 10 == 0):
        return (
            f"[AI Team OS] B0.9 reminder: the Leader has executed {consecutive} consecutive tool calls."
            "Should this be delegated to a team member?"
        )

    return None




def _check_workflow_reminders(event_data: dict, state: dict, project_id: str | None = None) -> list[str]:
    """Generate workflow reminders based on tool call patterns."""
    tool_name = event_data.get("tool_name", "")
    session_id = event_data.get("session_id", "")
    warnings: list[str] = []
    now = time.time()

    # 1. After TeamCreate: remind whether task is on the task wall
    if tool_name == "TeamCreate":
        warnings.append(
            "[OS Reminder] A new team has been created. Has a corresponding task been created on the task wall for "
            "this work direction?"
            "→ Use task_run or task_create to add the task"
        )

    # 1b. Workflow (CC ultracode orchestration) — a soft reminder, so output flows back into the OS (the governance
    # layer's positioning).
    # The OS does not block Workflow (a CC platform-level runtime), but reminds the Leader: 1) the overall task still
    # goes on the wall;
    # ② add an OS write-back instruction in the agent prompts so workflow members do their own bookkeeping. Throttled
    # to 300s against noise.
    if tool_name == "Workflow":
        last = state.get("workflow_reminder_at", 0)
        if now - last >= 300:
            state["workflow_reminder_at"] = now
            warnings.append(
                "[OS Reminder] The Workflow run is being automatically tracked as a team. You still need to do two "
                "things: "
                "1) task_create to put the overall task on the wall; 2) embed a write-back instruction in each "
                "workflow agent's prompt "
                "(load task_memo_add/report_save via ToolSearch, then call them), otherwise the internal output "
                "never enters the OS. "
                "See the skill /os-workflow for the template."
            )

    # 1c. Ecosystem research / self-built pipeline entry points — the orchestration layer has migrated to
    # ultracode/Workflow (v1.8.1 decision).
    # ultracode must be enabled manually by the user (not resident); the self-built dispatch layer can run any time
    # but is retired —
    # so the old entry points get a soft reminder: first confirm the user has ultracode on, then orchestrate with
    # Workflow. Throttled to 3600s.
    _ultracode_hint_tools = (
        "ecosystem_claim_shallow",
        "ecosystem_claim_review",
        "ecosystem_deep_review_request",
        "ecosystem_deep_review_request_batch",
        "ecosystem_scan",
        "ecosystem_scan_periodic",
    )
    if tool_name.removeprefix("mcp__ai-team-os__") in _ultracode_hint_tools:
        last = state.get("ultracode_hint_at", 0)
        if now - last >= 3600:
            state["ultracode_hint_at"] = now
            warnings.append(
                "[OS Reminder] The output of ecosystem research must be written back to the ecosystem tables "
                "(ecosystem_apply_shallow_summary / ecosystem_apply_quality_review), "
                "otherwise the ledger and the /ecosystem page cannot see this research."
            )

    # 2. Before Agent creation: check task wall, template usage, and historical memos
    if tool_name == "Agent":
        input_dict = event_data.get("tool_input", {})
        input_str = str(input_dict)
        has_team = bool(input_dict.get("team_name") or input_dict.get("name"))

        if has_team:
            # 2a. Check if active team has running/in_progress tasks; also check pipeline
            has_active_task = False
            running_tasks: list[dict] = []
            try:
                import urllib.request

                api_url = _get_api_url()
                _ph: dict[str, str] = {}
                if project_id:
                    _ph["X-Project-Id"] = project_id
                req = urllib.request.Request(f"{api_url}/api/teams", method="GET", headers=_ph)
                with urllib.request.urlopen(req, timeout=2) as resp:
                    teams = json.loads(resp.read().decode("utf-8")).get("data", [])
                active_teams = [t for t in teams if t.get("status") == "active"]
                if active_teams:
                    team_id = active_teams[0].get("id", "")
                    if team_id:
                        req2 = urllib.request.Request(
                            f"{api_url}/api/teams/{team_id}/tasks",
                            method="GET",
                            headers=_ph,
                        )
                        with urllib.request.urlopen(req2, timeout=2) as resp2:
                            tasks = json.loads(resp2.read().decode("utf-8")).get(
                                "data", []
                            )
                        running_tasks = [
                            t
                            for t in tasks
                            if t.get("status") in ("running", "in_progress")
                        ]
                        has_active_task = len(running_tasks) > 0
            except Exception:
                has_active_task = True  # API unavailable, don't block

            # Fallback: check project-level tasks (team_id=None) when no team tasks are running
            if not has_active_task:
                try:
                    if active_teams and active_teams[0].get("project_id"):
                        pid = active_teams[0]["project_id"]
                        proj_req = urllib.request.Request(
                            f"{api_url}/api/projects/{pid}/tasks/running-count",
                            method="GET",
                            headers=_ph,
                        )
                        with urllib.request.urlopen(proj_req, timeout=_API_TIMEOUT) as proj_resp:
                            proj_data = json.loads(proj_resp.read().decode("utf-8"))
                        if proj_data.get("count", 0) > 0:
                            has_active_task = True
                except Exception:
                    pass

            if not has_active_task:
                warnings.append(
                    "[OS Reminder] There is currently no task in progress. Before dispatching an Agent to work, put "
                    "this matter on the wall with task_create first, "
                    "otherwise the output has nowhere to be booked."
                )
            else:
                # 2a-TW. Pre-check: verify dispatched work matches a task wall item
                try:
                    agent_prompt = (input_dict.get("prompt", "") + " " + input_dict.get("description", "")).lower()
                    if agent_prompt.strip() and project_id:
                        _tw_path = f"/api/projects/{project_id}/task-wall?limit=20&include_completed=false"
                        tw_data = _api_call("GET", _tw_path, project_id=project_id)
                        if tw_data:
                            tw_tasks: list[dict] = []
                            for hg in (tw_data.get("wall") or {}).values():
                                if isinstance(hg, list):
                                    tw_tasks.extend(hg)
                            tw_pending = [t for t in tw_tasks if t.get("status") in ("pending", "running")]
                            # Quick keyword match check
                            agent_words = set(agent_prompt.replace("—", " ").replace("-", " ").split())
                            agent_words -= {
                                "\u7684", "\u662f", "\u5728", "\u4e86", "\u548c", "\u4e0e", "a", "the",
                                "to", "for", "of"
                            }
                            matched_any = False
                            for t in tw_pending:
                                _raw = (t.get("title") or "").lower().replace("—", " ").replace("-", " ")
                                title_words = set(_raw.split())
                                if len(title_words & agent_words) >= 2:
                                    matched_any = True
                                    break
                            if not matched_any and tw_pending:
                                # Throttled to 3600s (audit P1, 2026-07-14: the same reminder once fired at the Leader
                                # 24 times in a row)
                                _wm_last = state.get("wall_match_reminder_at", 0)
                                if now - _wm_last >= 3600:
                                    state["wall_match_reminder_at"] = now
                                    tw_titles = ", ".join(t.get("title", "?")[:20] for t in tw_pending[:3])
                                    warnings.append(
                                        f"[OS Reminder] This Agent's work is unmatched to any task-wall item (on the "
                                        f"wall: {tw_titles})."
                                        "Confirm this work is registered on the task wall? → Put it on the wall with "
                                        "task_create"
                                    )
                except Exception:
                    pass  # Advisory only

            # 2-CP1. Pipeline subtask binding: mark current stage subtask as running
            if has_active_task:
                try:
                    _bind_api_url = _get_api_url()
                    bind_msg = _bind_subtask_running(_bind_api_url, project_id=project_id)
                    if bind_msg:
                        warnings.append(f"[OS Reminder] {bind_msg}")
                except Exception:
                    pass  # Binding is optional — never block agent dispatch

            # 2d.(retired) pipeline strong-mount check — the pipeline has been directionally deprecated (design doc §7
            # Phase1 cut the new entry points): no longer nagging/forcing task_type; orchestration uses CC Workflow
            # instead,
            # and run tracking goes through the observation layer (Dashboard /workflows). See git history for rollback.
            if has_active_task and running_tasks:
                # 2e. Pipeline pending stage detection (retirement period: soft reminders for legacy pipelines only)
                tasks_with_pipeline = [
                    t for t in running_tasks
                    if (t.get("config") or {}).get("pipeline")
                ]
                pending_stage_info: list[tuple[str, str, str]] = []  # (task_title, stage_name, agent_template)
                for t in tasks_with_pipeline:
                    pipeline = t["config"]["pipeline"]
                    stages = pipeline.get("stages", [])
                    current_idx = pipeline.get("current_stage_index", 0)
                    # Look for pending stages after the current index
                    for stage in stages[current_idx + 1:]:
                        if stage.get("status") == "pending":
                            pending_stage_info.append((
                                t.get("title", t.get("id", "?")),
                                stage["name"],
                                stage.get("agent_template", ""),
                            ))
                            break  # Only report first pending per task

                if pending_stage_info:
                    pending_warnings = state.get("pipeline_pending_warnings", 0)
                    first_title, first_stage, first_tpl = pending_stage_info[0]
                    if pending_warnings == 0:
                        warnings.append(
                            f"[OS Reminder] Pipeline stage '{first_stage}' is ready (task: {first_title}), "
                            f"recommended agent_template: {first_tpl}"
                        )
                        state["pipeline_pending_warnings"] = 1
                    elif pending_warnings == 1:
                        warnings.append(
                            f"[OS Reminder] Please advance the pipeline first (stage '{first_stage}' is waiting), "
                            "before assigning other work"
                        )
                        state["pipeline_pending_warnings"] = 2
                    else:
                        # In the retirement period there is no more hard blocking (formerly exit(2)): legacy pipelines
                        # only get continuous soft reminders
                        warnings.append(
                            f"[OS Reminder] The legacy pipeline stage '{first_stage}' is still waiting to advance"
                        )

                # 2f. Agent type matching check vs pipeline recommended template.
                # Skipped for meeting-mode stages: any role can attend a meeting.
                subagent_type_for_check = input_dict.get("subagent_type", "")
                if subagent_type_for_check and tasks_with_pipeline:
                    # Find the current active stage's recommended template
                    recommended_template: str | None = None
                    current_stage_name: str | None = None
                    current_stage_mode: str = "agent"
                    for t in tasks_with_pipeline:
                        pipeline = t["config"]["pipeline"]
                        stages = pipeline.get("stages", [])
                        current_idx = pipeline.get("current_stage_index", 0)
                        if current_idx < len(stages):
                            current_stage = stages[current_idx]
                            if current_stage.get("status") in ("pending", "running"):
                                recommended_template = current_stage.get("agent_template", "")
                                current_stage_name = current_stage.get("name", "")
                                current_stage_mode = current_stage.get("mode", "agent")
                                break

                    if recommended_template and current_stage_name:
                        if current_stage_mode == "meeting":
                            # Meeting stages allow any role — skip type check entirely
                            pass
                        elif subagent_type_for_check == recommended_template:
                            # Correct template — reset pending warnings counter
                            state["pipeline_pending_warnings"] = 0
                        else:
                            warnings.append(
                                f"[OS Reminder] The current stage '{current_stage_name}' recommends "
                                f"{recommended_template}, "
                                f"but you dispatched {subagent_type_for_check}. Confirm using it?"
                            )

        # 2b. Template usage reminder (check if subagent_type is a known template)
        subagent_type = input_dict.get("subagent_type", "")
        if subagent_type in ("general-purpose", "") or not subagent_type:
            last_tpl = state.get("last_template_reminder", 0)
            if now - last_tpl > 600:  # 10-min cooldown
                warnings.append(
                    "[OS Reminder] Before dispatching a general-purpose Agent, take a look at whether a "
                    "better-fitting template exists: "
                    "agent_template_recommend(task description)"
                )
                state["last_template_reminder"] = now

        # 2c. Memo reminder (with 5-min cooldown)
        if has_team:
            last_memo = state.get("last_memo_reminder", 0)
            if now - last_memo > 300:
                warnings.append(
                    "[OS Reminder] Before assigning a new member: does this task have historical work records?"
                    "→ Consider checking the prior work first with task_memo_read"
                )
                state["last_memo_reminder"] = now

    # 3. Before SendMessage(shutdown): remind about task completion
    if tool_name == "SendMessage":
        input_str = str(event_data.get("tool_input", {}))
        if "shutdown" in input_str.lower() or "shut down" in input_str.lower():
            warnings.append(
                "[OS Reminder] Close Agent checklist: has this Agent's task been marked completed?"
                "→ Update the task status and add a summary memo (task_memo_add type=summary)"
            )

    # 4. On TeamDelete: sync-close ONLY the corresponding OS team.
    # Historical defect (audit A2, 2026-07-14, high): this code once iterated and blindly PUT all status=active teams to
    # completed with no scoping — under multi-session, multi-team parallelism (this repo's norm; 5+ active at the
    # time), deleting any one
    # CC team would mis-mark all teams still used by other sessions as completed, distorting cross-team state. Changed
    # to: precisely extract the deleted team's identifier from
    # tool_input, and close only that one per the OS↔CC naming convention (_norm_team_key, consistent with state_reaper.
    # _check_team_liveness's cc_dir_name); when no reliable identifier is obtained or nothing matches, only
    # remind without writing to the DB — state_reaper._check_team_liveness probes liveness from the CC config and
    # closes in sync as a fallback, so no blind write is needed.
    if tool_name == "TeamDelete":
        target_ident = _extract_team_identifier(event_data.get("tool_input", {}))
        if not target_ident:
            warnings.append(
                "[OS Reminder] A TeamDelete was detected, but the deleted team's identifier cannot be determined "
                "from the parameters, so no OS team was automatically closed in sync. "
                "If the OS side still shows the team as active, close it manually with team_close (the "
                "state_reaper's config liveness probing will also cover it as a fallback)."
            )
        else:
            try:
                import urllib.request

                api_url = _get_api_url()
                _tdh: dict[str, str] = {}
                if project_id:
                    _tdh["X-Project-Id"] = project_id
                req = urllib.request.Request(f"{api_url}/api/teams", method="GET", headers=_tdh)
                with urllib.request.urlopen(req, timeout=2) as resp:
                    teams = json.loads(resp.read().decode("utf-8")).get("data", [])
                _target_key = _norm_team_key(target_ident)
                matched = [
                    t
                    for t in teams
                    if t.get("status") == "active"
                    and (
                        _norm_team_key(t.get("name", "")) == _target_key
                        or t.get("id") == target_ident
                    )
                ]
                if not matched:
                    warnings.append(
                        f"[OS Reminder] The TeamDelete target team '{target_ident}' is unmatched to any active OS "
                        f"team; nothing was written to the database "
                        "(it may already be closed, or was never created in the OS; the state_reaper's config "
                        "liveness probing will sync it as a fallback)."
                    )
                for t in matched:
                    _close_h = {"Content-Type": "application/json"}
                    if project_id:
                        _close_h["X-Project-Id"] = project_id
                    close_req = urllib.request.Request(
                        f"{api_url}/api/teams/{t['id']}",
                        data=json.dumps({"status": "completed"}).encode(),
                        headers=_close_h,
                        method="PUT",
                    )
                    urllib.request.urlopen(close_req, timeout=2)
            except Exception:
                pass  # Silently handle — state_reaper._check_team_liveness is the backstop

    # 5. After TeamCreate: check if active teams already exist
    if tool_name == "TeamCreate":
        try:
            import urllib.request

            api_url = _get_api_url()
            _tch: dict[str, str] = {}
            if project_id:
                _tch["X-Project-Id"] = project_id
            req = urllib.request.Request(f"{api_url}/api/teams", method="GET", headers=_tch)
            with urllib.request.urlopen(req, timeout=2) as resp:
                teams = json.loads(resp.read().decode("utf-8")).get("data", [])
            active_teams = [t for t in teams if t.get("status") == "active"]
            # Newly created team is also active, so check if >1 active teams
            if len(active_teams) > 1:
                other = active_teams[0].get("name", "Unknown")
                warnings.append(
                    f"[OS Reminder] An active team already exists: '{other}'."
                    "Suggestion: 1) add members to the existing team 2) close the old team before creating a new one"
                )
        except Exception:
            pass  # Silently skip when API unavailable

    # 6. After SendMessage: check parallel task assignment (idle Agent + pending task matching)
    if tool_name == "SendMessage":
        try:
            import urllib.request

            api_url = _get_api_url()
            _smh: dict[str, str] = {}
            if project_id:
                _smh["X-Project-Id"] = project_id
            req = urllib.request.Request(f"{api_url}/api/teams", method="GET", headers=_smh)
            with urllib.request.urlopen(req, timeout=2) as resp:
                teams = json.loads(resp.read().decode("utf-8")).get("data", [])
            active_teams = [t for t in teams if t.get("status") == "active"]
            if active_teams:
                team_id = active_teams[0].get("id", "")
                if team_id:
                    req2 = urllib.request.Request(
                        f"{api_url}/api/teams/{team_id}/agents",
                        method="GET",
                        headers=_smh,
                    )
                    with urllib.request.urlopen(req2, timeout=2) as resp2:
                        agents = json.loads(resp2.read().decode("utf-8")).get("data", [])
                    non_leader_agents = [a for a in agents if a.get("role") != "leader"]
                    busy_count = sum(1 for a in non_leader_agents if a.get("status") == "busy")
                    idle_agents = [
                        a for a in non_leader_agents if a.get("status") in ("waiting", "offline")
                    ]
                    if busy_count < 3 and idle_agents:
                        # Try to fetch pending tasks for matching suggestions
                        match_hints: list[str] = []
                        try:
                            req3 = urllib.request.Request(
                                f"{api_url}/api/teams/{team_id}/tasks",
                                method="GET",
                                headers=_smh,
                            )
                            with urllib.request.urlopen(req3, timeout=2) as resp3:
                                tasks = json.loads(resp3.read().decode("utf-8")).get("data", [])
                            pending_tasks = [
                                t
                                for t in tasks
                                if t.get("status") in ("pending",) and not t.get("assigned_to")
                            ]
                            for idle in idle_agents[:3]:  # Show at most 3 idle Agents
                                agent_role = (idle.get("role") or idle.get("name") or "").lower()
                                agent_name = idle.get("name", "?")
                                # Find tasks whose tags overlap with agent role
                                matched = next(
                                    (
                                        t
                                        for t in pending_tasks
                                        if any(
                                            tag.lower() in agent_role or agent_role in tag.lower()
                                            for tag in (t.get("tags") or [])
                                        )
                                    ),
                                    pending_tasks[0] if pending_tasks else None,
                                )
                                if matched:
                                    tags_str = ",".join(matched.get("tags") or [])
                                    hint = (
                                        f"Idle agent: {agent_name}({idle.get('role', '')}), "
                                        f"To-do: {matched['title']}"
                                        + (f"(tags:{tags_str})" if tags_str else "")
                                        + " → assignment suggested"
                                    )
                                    match_hints.append(hint)
                        except Exception:
                            pass
                        # Nagging throttle: at most 1 "parallel assignment possible" hint per session, then silence
                        # (high-frequency nagging gets systematically ignored = a pure token and attention tax).
                        _idle_bucket = _session_bucket(state, session_id)
                        # Only remind when a concrete "who is free + which to-do" pairing can be given; when no pairing
                        # can be given, do not send it — platitudes like "parallel work improves efficiency" carry no
                        # information.
                        if match_hints and not _idle_bucket.get("idle_member_reminder_shown"):
                            warnings.append(
                                f"[OS Reminder] Only {busy_count} members are working; idle members can be paired "
                                f"with to-dos:\n"
                                + "\n".join(f"  • {h}" for h in match_hints)
                            )
                            _idle_bucket["idle_member_reminder_shown"] = True
        except Exception:
            pass  # Silently skip when API unavailable

    # 7. Task-wall catch-up: nag when a session hasn't touched the wall for a while.
    # Nagging class = low-frequency + silenceable: 1) the timer-resetting events are expanded to all task_*/taskwall_*
    # tools (no longer recognizing only
    # the two view tools, otherwise sessions managing the task wall with task_create/task_update would still be nagged
    # endlessly);
    # 2) the interval goes 900s→1800s; 3) at most 2 nags per session, then silence (counts go into the session bucket).
    if _is_taskwall_tool(tool_name):
        state["last_taskwall_view"] = now
    else:
        last_view = state.get("last_taskwall_view", 0)
        if last_view == 0:
            # First tool call in session — start the countdown from now
            state["last_taskwall_view"] = now
        elif (now - last_view) > 1800:
            bucket = _session_bucket(state, session_id)
            catchup_count = bucket.get("taskwall_catchup_count", 0)
            if catchup_count < 2:
                minutes = int((now - last_view) / 60)
                warnings.append(
                    f"[OS Reminder] It has been {minutes} minutes since the task wall was last viewed. → Consider "
                    f"task_list_project() "
                    f"to view the project task wall (pass team_id= to see only one team)"
                )
                bucket["taskwall_catchup_count"] = catchup_count + 1
            state["last_taskwall_view"] = now

    # 9. Handoff reminder: when Agent reports completion, remind to assign follow-up tasks
    if tool_name == "SendMessage":
        input_str = str(event_data.get("tool_input", {}))
        completion_keywords = ["\u5b8c\u6210", "complete", "done", "finished", "\u6c47\u62a5"]
        is_completion = any(kw in input_str.lower() for kw in completion_keywords)
        # Exclude shutdown messages (already handled by rule 3)
        is_shutdown = "shutdown" in input_str.lower() or "shut down" in input_str.lower()
        if is_completion and not is_shutdown:
            # 9-CP2. Pipeline auto-advance: mark subtask completed and advance pipeline
            try:
                _advance_api_url = _get_api_url()
                advance_msg = _advance_pipeline_on_completion(_advance_api_url, project_id=project_id)
                if advance_msg:
                    warnings.append(advance_msg)
            except Exception:
                pass  # Advancing is optional — never block completion message

            try:
                api_url = _get_api_url()
                _r9h: dict[str, str] = {}
                if project_id:
                    _r9h["X-Project-Id"] = project_id
                req = urllib.request.Request(f"{api_url}/api/teams", method="GET", headers=_r9h)
                with urllib.request.urlopen(req, timeout=2) as resp:
                    teams = json.loads(resp.read().decode("utf-8")).get("data", [])
                active_teams = [t for t in teams if t.get("status") == "active"]
                if active_teams:
                    team_id = active_teams[0].get("id", "")
                    if team_id:
                        req2 = urllib.request.Request(
                            f"{api_url}/api/teams/{team_id}/tasks",
                            method="GET",
                            headers=_r9h,
                        )
                        with urllib.request.urlopen(req2, timeout=2) as resp2:
                            tasks = json.loads(resp2.read().decode("utf-8")).get("data", [])
                        pending = [
                            t
                            for t in tasks
                            if t.get("status") == "pending" and not t.get("assigned_to")
                        ]
                        if pending:
                            pending_titles = ", ".join(t["title"] for t in pending[:3])
                            more = f" and {len(pending)} in total" if len(pending) > 3 else ""
                            warnings.append(
                                f"[OS Reminder] An Agent has finished reporting, and there are still unassigned "
                                f"tasks: {pending_titles}{more}."
                                "→ Assign them to idle members to keep things moving?"
                            )
            except Exception:
                pass

    # 10. After meeting_create: remind to notify participants and use skills
    if tool_name in ("meeting_create", "mcp__ai-team-os__meeting_create"):
        warnings.append(
            "[OS Reminder] The meeting has been created, but the OS will not notify anyone for you — "
            "notify participants one by one via SendMessage with the meeting_id and the agenda, and only then will "
            "they show up."
        )

    # 11. After meeting_conclude: remind to add action items to task wall
    if tool_name in ("meeting_conclude", "mcp__ai-team-os__meeting_conclude"):
        warnings.append(
            "[OS Reminder] The meeting has ended. The action items in the conclusions must go on the wall via "
            "task_create — written only in the minutes is the same as never raised."
        )

    # 12. When task marked complete: remind QA acceptance testing
    if tool_name in ("task_status", "mcp__ai-team-os__task_status"):
        input_str = str(event_data.get("tool_input", {}))
        if "completed" in input_str.lower():
            warnings.append(
                "[OS Reminder] The task has been marked completed. If the change affects system behavior or the "
                "frontend display, please tell QA what to observe"
            )

    # 13. Bottleneck detection: remind to hold meeting when all tasks done or many blocked
    # Check every 50 tool calls (throttled)
    bottleneck_count = state.get("bottleneck_check_count", 0) + 1
    state["bottleneck_check_count"] = bottleneck_count
    # v1.8.1 fix: judge by the project-level task wall rather than by each active team being empty —
    # the team dimension misses project-level tasks with team_id=null, and one team emptying would falsely report "all
    # complete"
    if bottleneck_count % 50 == 0 and project_id:
        try:
            import urllib.request

            api_url = _get_api_url()
            _b13h: dict[str, str] = {"X-Project-Id": project_id}
            req = urllib.request.Request(
                f"{api_url}/api/projects/{project_id}/task-wall",
                method="GET",
                headers=_b13h,
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            wall_data = payload.get("data", payload)
            by_status = (wall_data.get("stats") or {}).get("by_status", {})
            pending_n = by_status.get("pending", 0)
            running_n = by_status.get("running", 0)
            blocked_n = by_status.get("blocked", 0)
            if pending_n + running_n + blocked_n == 0:
                warnings.append("[OS Reminder] The task wall is clear: no pending/running/blocked tasks")
            elif blocked_n > running_n and blocked_n >= 2:
                warnings.append(f"[OS Reminder] {blocked_n} tasks are blocked, more than the {running_n} in progress")
        except Exception:
            pass

    # 14. Report format validation — only for member completion reports.
    # ⑤ The old implementation did not distinguish message direction: dispatch/acceptance messages sent by the Leader
    # were also required to follow the "Completed content/Modified files/
    # Test results" format. Changed to trigger only in **subagent sessions** (members reporting to the Leader),
    # excluding main/Leader
    # sessions; and a session-level throttle was added (at most once per session), replacing the original global 3600s
    # throttle.
    if tool_name == "SendMessage" and _is_subagent_session(session_id):
        input_str = str(event_data.get("tool_input", {}))
        completion_keywords = ["\u5b8c\u6210", "complete", "done", "finished", "\u6c47\u62a5"]
        if (
            any(kw in input_str.lower() for kw in completion_keywords)
            and "shutdown" not in input_str.lower() and "shut down" not in input_str.lower()
        ):
            required_fields = ["Completed content", "Modified files", "Test results"]
            missing = [f for f in required_fields if f not in input_str]
            if missing and len(input_str) > 100:  # Only check longer reports
                _rf_bucket = _session_bucket(state, session_id)
                if not _rf_bucket.get("report_fields_reminder_shown"):
                    _rf_bucket["report_fields_reminder_shown"] = True
                    warnings.append(
                        f"[OS Reminder] The report may be missing standard fields: {', '.join(missing)}. "
                        "Standard format: Completed content/Modified files/Test results/Suggested task "
                        "status/Suggested memo"
                    )

    # ── Safety guardrail rules ──────────────────────────────────────────

    tool_input = event_data.get("tool_input", {})

    # S1: Dangerous command interception (Bash)
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        # Strip heredoc blocks (<<'EOF'...EOF, <<"EOF"...EOF, <<EOF...EOF) so that
        # text inside commit messages or other string literals does not trigger S1.
        # Only the executable shell syntax outside heredoc delimiters is scanned.
        cmd_for_s1 = re.sub(r"<<['\"]?(\w+)['\"]?.*?\n.*?\1", "", cmd, flags=re.DOTALL)
        cmd_lower = cmd_for_s1.lower()
        # Recursive delete of root/home directory -> exit(2) hard block
        if re.search(r"rm\s+-[^\s]*[rR][^\s]*\s+(/|~/|~)(\s|$|[^a-zA-Z])", cmd_for_s1):
            sys.stderr.write("[OS BLOCK] Dangerous: recursive delete of root/home directory blocked")
            sys.exit(2)
        # Recursive delete of other dangerous targets -> warning
        if re.search(r"rm\s+-[^\s]*[rR][^\s]*\s+\*", cmd_for_s1):
            warnings.append("[Security] Danger: a recursive deletion wildcard command was detected; please confirm "
                            "the operation target")
        # Destructive database operations
        if re.search(r"\b(DROP\s+TABLE|DROP\s+DATABASE|TRUNCATE)\b", cmd_for_s1, re.IGNORECASE):
            warnings.append("[Security] Danger: a destructive database operation (DROP/TRUNCATE) was detected; "
                            "please confirm")
        # force push
        if "push" in cmd_lower and "--force" in cmd_lower:
            warnings.append("[Security] Note: a force push was detected; it may overwrite remote history")
        # Overly permissive file permissions
        if "chmod 777" in cmd_for_s1:
            warnings.append("[Security] Security: overly open file permissions (chmod 777); stricter permissions "
                            "are recommended")
        # S3: Sensitive file commit interception (git add) -> exit(2) hard block
        if "git add" in cmd_lower:
            block_patterns = [".env", "id_rsa", ".pem", ".key"]
            for pat in block_patterns:
                if pat in cmd_lower:
                    sys.stderr.write(f"[OS BLOCK] Committing sensitive files is forbidden ({pat})")
                    sys.exit(2)
            # credentials keep as warning (filename is ambiguous, may not be a key file)
            if "credentials" in cmd_lower:
                warnings.append(
                    "[Security] Security: an attempt to commit a credentials file was detected; "
                    "please confirm the file contains no secret information and is already in .gitignore"
                )

        # S4: Worktree teardown protection — never tear down unlanded work.
        # Covers three teardown paths: `git worktree remove`, `git branch -D` on a
        # worktree-prefixed branch, and raw `rm -rf` on a worktree directory (the last
        # bypasses git's own dirty-tree check entirely). See
        # docs/worktree-governance-design.md §3 for the design and rationale.
        def _first_path_token(rest: str) -> str:
            for tok in re.findall(r'"[^"]+"|\'[^\']+\'|\S+', rest.strip()):
                stripped = tok.strip("'\"")
                if stripped not in ("--force", "-f"):
                    return stripped
            return ""

        base_cwd = event_data.get("cwd") or os.getcwd()

        wt_remove_m = re.search(
            r"git\s+(?:-C\s+(?P<cdir>\S+)\s+)?worktree\s+remove\s+(?P<rest>.+)",
            cmd_for_s1,
        )
        rm_worktree_m = re.search(
            r"rm\s+-[^\s]*[rR][^\s]*\s+(?P<rest>\S*\.claude/worktrees/\S+)",
            cmd_for_s1,
        )
        branch_d_m = re.search(r"git\s+branch\s+-D\s+(?P<rest>.+)", cmd_for_s1)

        def _block_on_worktree(target: str, via: str) -> None:
            dirty, unlanded, warn = _assess_unlanded_work(target)
            if dirty or unlanded:
                reason = "There are uncommitted/untracked changes" if dirty else unlanded
                sys.stderr.write(
                    f"[OS BLOCK] Refusing to {via} {target}: {reason}."
                    "Merge or push a backup first; abandoning these changes must be handled manually in person — do "
                    "not replay this blocked command."
                )
                sys.exit(2)
            if warn:
                warnings.append(f"[Security] Note: worktree {target} {warn}; please confirm before deleting")

        if wt_remove_m:
            raw_path = _first_path_token(wt_remove_m.group("rest"))
            cdir = wt_remove_m.group("cdir") or base_cwd
            target = os.path.abspath(os.path.join(cdir, raw_path)) if raw_path else ""
            if target and os.path.isdir(target):
                _block_on_worktree(target, "delete worktree")

        if rm_worktree_m:
            raw_path = _first_path_token(rm_worktree_m.group("rest"))
            target = os.path.abspath(os.path.join(base_cwd, raw_path)) if raw_path else ""
            if target and os.path.isdir(target):
                _block_on_worktree(target, "delete the worktree directory with rm -rf")

        if branch_d_m:
            branch = _first_path_token(branch_d_m.group("rest"))
            if branch.startswith("worktree-"):
                exists_code, _ = _run_git_readonly(
                    ["rev-parse", "--verify", "--quiet", branch], cwd=base_cwd
                )
                if exists_code == 0:
                    main_branch = _main_branch_name(base_cwd)
                    ancestor_code = 1
                    cherry_lines: list[str] = []
                    if main_branch:
                        ancestor_code, _ = _run_git_readonly(
                            ["merge-base", "--is-ancestor", branch, main_branch], cwd=base_cwd
                        )
                        if ancestor_code != 0:
                            # Same secondary patch-id signal as the worktree-remove
                            # path (see _all_commits_patch_equivalent): a branch
                            # landed via a differently shaped commit elsewhere is
                            # still safe to delete even though it's not a literal
                            # ancestor. Pass `branch` explicitly as the head to
                            # compare -- base_cwd's own checked-out HEAD (the main
                            # checkout, typically master) is not the ref in question.
                            cherry_lines = _cherry_lines(base_cwd, main_branch, head=branch)
                            if _all_commits_patch_equivalent(cherry_lines):
                                ancestor_code = 0
                    if ancestor_code != 0:
                        upstream_code, _ = _run_git_readonly(
                            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", f"{branch}@{{u}}"],
                            cwd=base_cwd,
                        )
                        pushed = False
                        if upstream_code == 0:
                            ahead_code, ahead_out = _run_git_readonly(
                                ["rev-list", f"{branch}@{{u}}..{branch}"], cwd=base_cwd
                            )
                            pushed = ahead_code == 0 and not ahead_out
                        if not pushed:
                            reason = "There are local unpushed/unmerged commits"
                            hint = _cherry_breakdown(cherry_lines)
                            if hint:
                                reason = f"{reason} ({hint})"
                            sys.stderr.write(
                                f"[OS BLOCK] Refused force-deleted branch {branch}: {reason}."
                                "Merge or push a backup first; abandoning these changes must be handled manually in "
                                "person — do not replay this blocked command."
                            )
                            sys.exit(2)
                        warnings.append(
                            f"[Security] Note: branch {branch} has been pushed to the remote but not yet merged into "
                            f"the base branch; please confirm before deleting"
                        )

        # S5: commit-time branch ownership assertion (A1', debate 503e07f1).
        # Same domain as S4 - both are read-only git assertions guarding the
        # multi-session worktree discipline - but at the opposite end: S4 stops
        # finished work from being torn down, S5 stops new work from landing on
        # someone else's branch. PreToolUse only: asserting after the commit has
        # already been made answers a question nobody can act on any more.
        if event_data.get("hook_event_name") == "PreToolUse":
            warnings.extend(
                _check_commit_branch_ownership(event_data, state, cmd_for_s1, base_cwd)
            )

    # 15. Team directory cleanup reminder: check every 100 tool calls.
    # ② This reminder is already sent once on the session_bootstrap startup side; here (at tool time) a session-level
    # throttle is added —
    # at most once per session, avoiding a resend every 100 tool calls within the same session (the startup side stays
    # untouched).
    team_cleanup_count = state.get("team_cleanup_check_count", 0) + 1
    state["team_cleanup_check_count"] = team_cleanup_count
    if team_cleanup_count % 100 == 0:
        _td_bucket = _session_bucket(state, session_id)
        if not _td_bucket.get("team_dir_reminder_shown"):
            teams_dir = Path.home() / ".claude" / "teams"
            if teams_dir.exists():
                try:
                    team_dirs = [p for p in teams_dir.iterdir() if p.is_dir()]
                    if len(team_dirs) > 5:
                        warnings.append(
                            f"[OS Reminder] {len(team_dirs)} historical session team directories have accumulated "
                            f"under ~/.claude/teams/; "
                            "old directories can be deleted manually (do not delete the current session's)"
                        )
                        _td_bucket["team_dir_reminder_shown"] = True
                except Exception:
                    pass

    # ── Safety guardrail rules ──────────────────────────────────────────

    # S2: Sensitive information detection (Write/Edit)
    if tool_name in ("Write", "Edit"):
        # Get content to be written
        content = tool_input.get("content", "") or tool_input.get("new_string", "")
        # Hardcoded secret detection
        if re.search(
            r"(password|secret|api_key|token)\s*=\s*['\"][^'\"]+['\"]",
            content,
            re.IGNORECASE,
        ):
            warnings.append("[Security] Security: a possible hard-coded secret was detected; using environment "
                            "variables is recommended")
        # .env file write reminder
        file_path = tool_input.get("file_path", "")
        if file_path.endswith(".env") or "/.env" in file_path or "\\.env" in file_path:
            warnings.append("[Security] Note: .env files should not be committed to version control; please confirm "
                            ".gitignore includes .env")
        # Reports data directory hard block: only block writes to the actual reports data
        # dirs under ~/.claude/data/. Any other .md write (README, docs, src) is allowed.
        # Conditions (both must be true):
        #   1. Path contains the exact data dir prefix for reports
        #   2. File extension is .md
        _fp_normalized = file_path.replace("\\", "/")
        _is_report_data_dir = (
            ".claude/data/ai-team-os/reports/" in _fp_normalized
            or (
                ".claude/data/ai-team-os/projects/" in _fp_normalized
                and "/reports/" in _fp_normalized
            )
        )
        if _is_report_data_dir and file_path.endswith(".md"):
            warnings.append(
                "[OS Reminder] Reports should be saved to the database via the report_save tool (writing a file "
                "directly is not tracked by the system). "
                "→ report_save(author=your name, topic=the topic, content=the markdown content,"
                " report_type=research/design/analysis/meeting-minutes)"
            )
        # The cooperative file-lock conflict reminder has been retired (batch 8b, 2026-07-27): locks could only be
        # established by the file_lock_*
        # four MCP tools, and those four tools were never called by anyone — the lock file was measured to be
        # constantly {}, so this judgment
        # could never enter the if. The conflict detection actually in use is on the hook_translator side:
        # _check_file_edit_conflict (judged by recent edit events, backed by 212 real events) +
        # get_file_hotspots; neither was touched by a single line.

    return warnings


def _post_tool_taskwall_sync(event_data: dict, state: dict, project_id: str | None = None) -> list[str]:
    """PostToolUse: auto-sync task wall when Agent dispatched or completion reported.

    1. After Agent dispatch → find matching pending task on project wall → auto-update to running
    2. After SendMessage(completion) → remind to update task to completed
    """
    tool_name = event_data.get("tool_name", "")
    warnings: list[str] = []

    if not project_id:
        return warnings

    # 1. After Agent dispatch: auto-link to task wall item
    if tool_name == "Agent":
        input_dict = event_data.get("tool_input", {})
        # Only for team agents (non-readonly)
        if not input_dict.get("team_name"):
            return warnings

        agent_prompt = input_dict.get("prompt", "")
        agent_desc = input_dict.get("description", "")
        agent_text = f"{agent_desc} {agent_prompt}".lower()

        if not agent_text.strip():
            return warnings

        try:
            # Query project task wall for pending tasks
            _wall_path = f"/api/projects/{project_id}/task-wall?limit=20&include_completed=false"
            wall_data = _api_call("GET", _wall_path, project_id=project_id)
            if not wall_data:
                return warnings

            wall_tasks: list[dict] = []
            for horizon_group in (wall_data.get("wall") or {}).values():
                if isinstance(horizon_group, list):
                    wall_tasks.extend(horizon_group)

            # Find matching pending task by keyword overlap
            pending_tasks = [t for t in wall_tasks if t.get("status") in ("pending", "running")]
            best_match: dict | None = None
            best_score = 0

            for task in pending_tasks:
                title = (task.get("title") or "").lower()
                tags = [t.lower() for t in (task.get("tags") or [])]
                desc = (task.get("description") or "").lower()

                # Simple keyword overlap scoring
                score = 0
                title_words = set(title.replace("—", " ").replace("-", " ").split())
                agent_words = set(agent_text.replace("—", " ").replace("-", " ").split())
                # Remove common stop words
                stop_words = {
                    "\u7684", "\u662f", "\u5728", "\u4e86", "\u548c", "\u4e0e", "a", "the", "to", "and",
                    "for", "of", "in", "on"
                }
                title_words -= stop_words
                agent_words -= stop_words

                overlap = title_words & agent_words
                score += len(overlap) * 2

                # Tag matching
                for tag in tags:
                    if tag in agent_text:
                        score += 3

                # Description keyword overlap
                if desc:
                    desc_words = set(desc.replace("—", " ").replace("-", " ").split()) - stop_words
                    score += len(desc_words & agent_words)

                if score > best_score:
                    best_score = score
                    best_match = task

            if best_match and best_score >= 3:
                task_id = best_match["id"]
                task_title = best_match.get("title", "")
                task_status = best_match.get("status", "pending")

                if task_status == "pending":
                    # Auto-update to running
                    _api_call("PUT", f"/api/tasks/{task_id}", {"status": "running"}, project_id=project_id)
                    warnings.append(
                        f"[OS Reminder] Automatically linked to the task wall: '{task_title}' → running"
                    )
                else:
                    warnings.append(
                        f"[OS Reminder] Current work is linked to the task wall: '{task_title}' (status: {task_status})"
                    )

                # Save matched task ID for later completion tracking
                state["last_dispatched_task_id"] = task_id
                state["last_dispatched_task_title"] = task_title
            elif best_score < 3 and pending_tasks:
                # No good match - warn to create on wall (throttled to 3600s, sharing the key with the PreToolUse side)
                _wm_last = state.get("wall_match_reminder_at", 0)
                _wm_now = time.time()
                if _wm_now - _wm_last >= 3600:
                    state["wall_match_reminder_at"] = _wm_now
                    warnings.append(
                        "[OS Reminder] This Agent's work is unmatched to any task-wall item. Consider putting it on "
                        "the wall with task_create first, so the work is traceable. "
                        f"The task wall currently has {len(pending_tasks)} to-do tasks"
                    )

        except Exception:
            pass  # Task wall sync is advisory — never block

    # 2. After SendMessage with completion keywords: advisory reminder only.
    # NOTE (2026-07-14): this branch used to auto-PUT the task to completed.
    # That was wrong-direction inference — Leader outbound messages like
    # "Report back to me once finished" hit the substring match and marked in-progress tasks
    # completed, bypassing Leader acceptance. Hook must never write task
    # status here; it only nudges the Leader to use task_update explicitly.
    if tool_name == "SendMessage":
        input_str = str(event_data.get("tool_input", {}))
        completion_keywords = ["\u5b8c\u6210", "complete", "done", "finished", "\u6c47\u62a5"]
        is_completion = any(kw in input_str.lower() for kw in completion_keywords)
        is_shutdown = "shutdown" in input_str.lower() or "shut down" in input_str.lower()

        if is_completion and not is_shutdown:
            last_task_id = state.get("last_dispatched_task_id")
            last_task_title = state.get("last_dispatched_task_title")
            if last_task_id:
                warnings.append(
                    f"[OS Reminder] Completion-style message detected. If '{last_task_title}' is truly complete and "
                    f"accepted, "
                    "use task_update to set it to completed (the hook does not write to the database automatically)"
                )
                # Clear tracking — remind once, then stop nagging
                state.pop("last_dispatched_task_id", None)
                state.pop("last_dispatched_task_title", None)

    return warnings


def main() -> None:
    # Force UTF-8 output on Windows (default is gbk, causes garbled Chinese)
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    try:
        raw = sys.stdin.buffer.read().decode("utf-8")
        if not raw.strip():
            return
        payload = json.loads(raw)
    except Exception:
        return

    # CC hook payload doesn't include event type name; inject via CLI arg
    if len(sys.argv) > 1 and "hook_event_name" not in payload:
        payload["hook_event_name"] = sys.argv[1]

    event_name = payload.get("hook_event_name", "")
    state = _load_supervisor_state()
    warnings: list[str] = []

    # Resolve project ID once; propagate to all project-scoped API calls
    project_id = _resolve_project_id()

    if event_name == "PreToolUse":
        w = _check_agent_team_name(payload)
        if w:
            warnings.append(w)
        w = _check_leader_doing_too_much(payload, state)
        if w:
            warnings.append(w)
    if event_name == "PostToolUse":
        # Auto-update task wall when Agent is dispatched or reports completion
        post_warnings = _post_tool_taskwall_sync(payload, state, project_id=project_id)
        warnings.extend(post_warnings)

    # Workflow reminders (checked for both PreToolUse and PostToolUse)
    if event_name in ("PreToolUse", "PostToolUse"):
        wf_warnings = _check_workflow_reminders(payload, state, project_id=project_id)
        warnings.extend(wf_warnings)

    _save_supervisor_state(state)

    # PreToolUse/PostToolUse hooks inject text into conversation via hookSpecificOutput
    #
    # Never fill permissionDecision (Creator ruling, 2026-07-27): that field is an **optional** position slot
    # (allow = pass directly / deny = reject / ask = inquire / absent = no position, following CC's default flow); the
    # old implementation
    # treated it as "an output format PreToolUse must carry" and filled allow every time, with a priority higher than
    # the permission mode the user chose
    # — default/plan/acceptEdits were all overridden, and the permission prompts of the five tool classes
    # Agent|Bash|Edit|Write|Workflow
    # were all muted (43,605 calls in 30 days without a single prompt). CC has its own complete permission modes
    # for the user to choose; the OS does not decide for it: this hook only injects reminder text and hands
    # permissions back to CC.
    output = {"hookSpecificOutput": {"hookEventName": event_name}}
    if warnings:
        output["hookSpecificOutput"]["additionalContext"] = "\n".join(warnings)
    sys.stdout.write(json.dumps(output))


def _yield_if_superseded() -> None:
    """Backup-chain yield: exit 0 if the source-install main chain covers this hook.

    When AI Team OS is present both as a marketplace plugin and via the source
    installer, CC fires two byte-identical copies of every hook. To keep exactly
    one chain speaking, the plugin-mode copy exits silently iff ~/.claude/settings.json
    already registers this same script under ~/.claude/hooks/ai-team-os/. Hooks the
    main chain does not register keep running from the plugin (out-of-box backup —
    no coverage gap). Only the plugin-mode copy ever yields: CLAUDE_PLUGIN_ROOT is
    set by CC for plugin hooks only and __file__ lives under it; the runtime
    main-chain copy and any direct/repo/test run lack that and never yield. Pure
    stdlib, one small file read; any error falls through and runs (fail-safe).
    """
    import os
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if not plugin_root:
        return
    try:
        import json
        from pathlib import Path
        here = Path(__file__).resolve()
        if Path(plugin_root).resolve() not in here.parents:
            return
        settings = Path.home() / ".claude" / "settings.json"
        registered = json.loads(settings.read_text(encoding="utf-8")).get("hooks", {})
    except Exception:
        return
    name = os.path.basename(__file__)
    for groups in registered.values():
        for group in groups:
            for hook in group.get("hooks", []):
                cmd = hook.get("command", "")
                if "ai-team-os" in cmd and name in cmd:
                    raise SystemExit(0)


if __name__ == "__main__":
    _yield_if_superseded()
    main()
