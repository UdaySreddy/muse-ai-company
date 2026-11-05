"""AI Team OS — API dependency injection.

Provides TeamManager singleton and StorageRepository lifespan management.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import Request
from sqlalchemy import inspect, text

from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.api.state_reaper import StateReaper
from aiteam.clock import utc_now
from aiteam.loop.task_wall_engine import TaskWallEngine
from aiteam.loop.watchdog import WatchdogChecker, WatchdogRunner
from aiteam.memory.store import MemoryStore
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.connection import close_db, get_engine
from aiteam.storage.repository import StorageRepository
from aiteam.types import AgentStatus

logger = logging.getLogger(__name__)

# Module-level singletons
_repository: StorageRepository | None = None
_memory_store: MemoryStore | None = None
_event_bus: EventBus | None = None
_manager: TeamManager | None = None
_reaper: StateReaper | None = None
_watchdog_runner: WatchdogRunner | None = None
_hook_translator: HookTranslator | None = None
_task_wall_engine: TaskWallEngine | None = None


def _run_alembic_stamp_head(db_url: str) -> None:
    """Stamp the existing database as being at the latest Alembic revision.

    Used for databases that were created/migrated via the legacy _run_migrations()
    approach. Tells Alembic "this DB is already up to date" so future incremental
    migrations can be tracked properly.
    """
    try:
        from alembic import command
        from alembic.config import Config

        # Locate alembic.ini relative to this package (project root)
        ini_path = Path(__file__).parents[4] / "alembic.ini"
        if not ini_path.exists():
            logger.warning("alembic.ini not found at %s, skipping stamp", ini_path)
            return

        alembic_cfg = Config(str(ini_path))
        alembic_cfg.set_main_option("sqlalchemy.url", db_url)
        command.stamp(alembic_cfg, "head")
        logger.info("Alembic: stamped existing DB as head")
    except Exception as exc:
        logger.warning("Alembic stamp failed (non-fatal): %s", exc)


def _get_alembic_current_revision(db_url: str) -> str | None:
    """Return the current Alembic revision for the given DB, or None if untracked."""
    try:
        from alembic.migration import MigrationContext
        from sqlalchemy import create_engine

        # Use sync engine for Alembic inspection (aiosqlite URL -> sqlite URL)
        sync_url = db_url.replace("sqlite+aiosqlite", "sqlite")
        engine = create_engine(sync_url, connect_args={"check_same_thread": False})
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            rev = ctx.get_current_revision()
        engine.dispose()
        return rev
    except Exception:
        return None


async def _run_migrations(db_url: str | None = None) -> None:
    """Run database migrations — add new columns to existing tables.

    SQLAlchemy create_all only creates non-existing tables, it won't ALTER existing ones.
    This function checks and adds columns introduced in M3.1.
    """
    engine = get_engine(db_url)

    # Columns to add: (table_name, column_name, column_type_sql)
    migrations: list[tuple[str, str, str]] = [
        ("teams", "project_id", "VARCHAR(36)"),
        ("teams", "status", "VARCHAR(20) DEFAULT 'active'"),
        ("agents", "project_id", "VARCHAR(36)"),
        ("agents", "current_phase_id", "VARCHAR(36)"),
        ("tasks", "project_id", "VARCHAR(36)"),
        ("tasks", "parent_id", "VARCHAR(36)"),
        ("tasks", "depends_on", "JSON DEFAULT '[]'"),
        ("tasks", "depth", "INTEGER DEFAULT 0"),
        ("tasks", "order", "INTEGER DEFAULT 0"),
        ("tasks", "template_id", "VARCHAR(50)"),
        ("meetings", "project_id", "VARCHAR(36)"),
        ("tasks", "config", "JSON DEFAULT '{}'"),
        # v0.9.0: event log enhancement
        ("events", "entity_id", "VARCHAR(36)"),
        ("events", "entity_type", "VARCHAR(50)"),
        ("events", "state_snapshot", "JSON"),
        # v1.0 P2: agent trust scoring
        ("agents", "trust_score", "REAL DEFAULT 0.5"),
        # Stage B: ecosystem_repo_profiles extension fields
        ("ecosystem_repo_profiles", "pushed_at", "DATETIME"),
        ("ecosystem_repo_profiles", "is_archived", "BOOLEAN DEFAULT 0"),
        ("ecosystem_repo_profiles", "scan_run_id", "VARCHAR(36)"),
        ("ecosystem_repo_profiles", "description_excerpt", "TEXT DEFAULT ''"),
    ]
    # v1.0 P1-6: channel_messages table (created via create_all, no ALTER needed)
    # Stage B: ecosystem_deep_reviews / ecosystem_tags / ecosystem_repo_tags /
    # the ecosystem_relations / ecosystem_scan_runs tables are created automatically by create_all

    async with engine.connect() as conn:
        for table_name, col_name, col_type in migrations:
            # Check if column already exists
            has_column = await conn.run_sync(
                lambda sync_conn, t=table_name, c=col_name: (
                    c in [col["name"] for col in inspect(sync_conn).get_columns(t)]
                    if inspect(sync_conn).has_table(t)
                    else False
                )
            )
            if not has_column:
                # SAFETY: values are hardcoded constants, not user input
                await conn.execute(
                    text(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}")
                )
                logger.info("Migration: added column %s to table %s", col_name, table_name)

        # Value migration: idle -> waiting (three-state model upgrade)
        await conn.execute(text("UPDATE agents SET status='waiting' WHERE status='idle'"))

        # B1: ensure task status index exists on legacy DBs
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_tasks_status ON tasks (status)")
        )

        # Migration: tasks.team_id from NOT NULL to nullable (support project-level tasks)
        team_id_nullable = await conn.run_sync(
            lambda sync_conn: (
                next(
                    (
                        col["nullable"]
                        for col in inspect(sync_conn).get_columns("tasks")
                        if col["name"] == "team_id"
                    ),
                    True,  # If column not found, skip
                )
                if inspect(sync_conn).has_table("tasks")
                else True
            )
        )
        if not team_id_nullable:
            logger.info(
                "Migration: rebuilding tasks table with nullable team_id (project-level tasks)"
            )
            await conn.execute(text("CREATE TABLE tasks_new AS SELECT * FROM tasks"))
            await conn.execute(text("DROP TABLE tasks"))
            await conn.execute(
                text("""
                CREATE TABLE tasks (
                    id VARCHAR(36) PRIMARY KEY,
                    team_id VARCHAR(36),
                    title VARCHAR(500) NOT NULL,
                    description TEXT DEFAULT '',
                    status VARCHAR(20) DEFAULT 'pending',
                    assigned_to VARCHAR(36),
                    result TEXT,
                    parent_id VARCHAR(36),
                    project_id VARCHAR(36),
                    depends_on JSON DEFAULT '[]',
                    depth INTEGER DEFAULT 0,
                    "order" INTEGER DEFAULT 0,
                    template_id VARCHAR(50),
                    priority VARCHAR(20) DEFAULT 'medium',
                    horizon VARCHAR(20) DEFAULT 'short',
                    tags JSON DEFAULT '[]',
                    config JSON DEFAULT '{}',
                    created_at DATETIME,
                    started_at DATETIME,
                    completed_at DATETIME
                )
            """)
            )
            await conn.execute(text("INSERT INTO tasks SELECT * FROM tasks_new"))
            await conn.execute(text("DROP TABLE tasks_new"))
            await conn.execute(text("CREATE INDEX ix_tasks_team_id ON tasks (team_id)"))
            logger.info("Migration: tasks table rebuild complete")

        await conn.commit()


_DEFAULT_ECOSYSTEM_TAGS: list[tuple[str, str, str]] = [
    # capability
    ("memory_system", "capability", "Memory / long-term context system"),
    ("skill_system", "capability", "Skill / skill injection system"),
    ("agent_orchestration", "capability", "Multi-Agent orchestration"),
    ("mcp_server", "capability", "MCP Server implementation"),
    ("mcp_framework", "capability", "MCP framework / SDK (for building MCP)"),
    ("tool_use", "capability", "Tool-use related"),
    ("workflow_engine", "capability", "Workflow engine"),
    ("multi_agent", "capability", "Multi-Agent collaboration system"),
    ("single_agent", "capability", "Single-Agent system"),
    ("claude_code", "capability", "Claude Code ecosystem (skills/hooks/agents)"),
    ("agent_harness", "capability", "Agent runtime / framework shell"),
    # tech_stack
    ("python", "tech_stack", "Python implementation"),
    ("typescript", "tech_stack", "TypeScript implementation"),
    ("javascript", "tech_stack", "JavaScript implementation"),
    ("java", "tech_stack", "Java implementation"),
    ("rust", "tech_stack", "Rust implementation"),
    ("go", "tech_stack", "Go implementation"),
    # maturity
    ("official_anthropic", "maturity", "Official Anthropic project"),
    ("battle_tested", "maturity", "Battle-tested in practice"),
    ("experimental", "maturity", "Experimental / early-tryout stage"),
    # positioning
    ("framework", "positioning", "Framework-level project"),
    ("application", "positioning", "End-user application"),
    ("library", "positioning", "Tool library"),
    ("plugin", "positioning", "Plugin / extension"),
    ("template", "positioning", "Project template"),
    ("docs_only", "positioning", "Docs only / tutorials / awesome-list / cookbook"),
    # v1.5.0-A: lifecycle (positioning) — funnel Stage 3 + failure states, auto-tagged
    ("evaluating", "positioning", "Currently being evaluated (Stage 1-2)"),
    ("reference", "positioning", "Used as an architecture reference"),
    ("integrated", "positioning", "Integrated into this project"),
    ("deleted", "positioning", "The GitHub-side repo has been deleted"),
    ("private_now", "positioning", "The GitHub-side repo has been set to private"),
]


async def ensure_default_tags(repo: StorageRepository) -> None:
    """At startup, ensure the default 31 EcosystemTags exist (INSERT OR IGNORE semantics).

    v1.5.0-A: 26 → 31 (5 new lifecycle positioning tags added:
    evaluating / reference / integrated / deleted / private_now).

    Uses upsert_tag to achieve idempotency: existing entries are kept, and manually edited descriptions are not
    overwritten.
    The actual logic checks via get_tag_by_name before inserting, to avoid overwriting.
    """
    from aiteam.types import EcosystemTag, EcosystemTagCategory

    inserted = 0
    for name, category, description in _DEFAULT_ECOSYSTEM_TAGS:
        existing = await repo.get_tag_by_name(name)
        if existing is not None:
            continue
        await repo.upsert_tag(
            EcosystemTag(
                name=name,
                category=EcosystemTagCategory(category),
                description=description,
            )
        )
        inserted += 1
    if inserted > 0:
        logger.info("Ensured %d default ecosystem tags inserted", inserted)


async def _backfill_ecosystem_default_project(repo: StorageRepository) -> None:
    """Stage J startup migration: backfill historical ecosystem rows whose project_id IS NULL
    to the project matching the current cwd, ensuring the old 188 repos remain visible under the "AI Team OS" project.

    Selection rules:
    1. Prefer the project with the longest cwd prefix match (consistent with _auto_create_projects);
    2. When none is found, fall back to the first project;
    3. When there are no projects at all, skip (the data stays NULL, to be backfilled again after a project is created).

    Idempotent: only NULL rows are migrated; rows with an existing project_id are untouched.
    """
    import os

    try:
        projects = await repo.list_projects()
    except Exception as exc:
        logger.warning("ecosystem backfill: list_projects failed: %s", exc)
        return
    if not projects:
        logger.info("ecosystem backfill: no projects yet, skipping")
        return

    cwd = os.getcwd().replace("\\", "/").rstrip("/").lower()
    target = None
    best_len = -1
    for p in projects:
        rp = (p.root_path or "").replace("\\", "/").rstrip("/").lower()
        if rp and (cwd == rp or cwd.startswith(rp + "/")) and len(rp) > best_len:
            target = p
            best_len = len(rp)
    if target is None:
        target = projects[0]

    try:
        counts = await repo.backfill_ecosystem_to_project(target.id)
    except Exception as exc:
        logger.warning("ecosystem backfill failed: %s", exc)
        return

    total = sum(counts.values())
    if total > 0:
        logger.info(
            "Ecosystem backfill: migrated %d rows to project %s (%s)",
            total,
            target.name,
            target.id[:8],
        )
        for k, v in counts.items():
            if v > 0:
                logger.info("  %s: %d", k, v)
    else:
        logger.info("Ecosystem backfill: no NULL rows to migrate")


async def _backfill_v150_progressive_funnel(repo: StorageRepository) -> None:
    """v1.5.0-A startup migration:

    1) Infer from the existing DeepReview.stage_status
       - completed and has risks_md+learnings_md (meaning a deep-scan summary ran) → DEBATED
       - otherwise has architecture_md → ARCHITECTURE_DONE
       - otherwise has summary_md → SHALLOW_DONE
       - otherwise keep QUEUED
    2) The existing Profile.is_active field (its default True is guaranteed by the column default)
    3) Create a default EcosystemProjectSettings row for each project
       - Projects whose name contains "AI Team OS" use the strict defaults (min_stars=5000, top_n=200)
       - Other projects use the general defaults (min_stars=1000, top_n=100)

    Idempotent: safe to re-run on every startup; existing stage_status / settings are untouched.
    """
    try:
        projects = await repo.list_projects()
    except Exception as exc:
        logger.warning("v1.5.0 backfill: list_projects failed: %s", exc)
        return

    # 1) DeepReview stage_status inference (only migrates historical rows still at stage_status='queued')
    try:
        deep_reviews = await repo.list_deep_reviews(limit=10000, project_id=None)
    except Exception as exc:
        logger.warning("v1.5.0 backfill: list_deep_reviews failed: %s", exc)
        deep_reviews = []

    promoted_counts = {
        "shallow_done": 0,
        "architecture_done": 0,
        "debated": 0,
        "kept_queued": 0,
    }
    from aiteam.types import EcosystemDeepReviewStatus, EcosystemStageStatus

    for review in deep_reviews:
        # Only migrate rows whose stage_status is still the default QUEUED; protect manually advanced states
        if review.stage_status != EcosystemStageStatus.QUEUED:
            continue
        # Inference rules (conservative, from strict to loose)
        if (
            review.status == EcosystemDeepReviewStatus.COMPLETED
            and review.risks_md
            and review.learnings_md
        ):
            new_stage = EcosystemStageStatus.DEBATED
            promoted_counts["debated"] += 1
        elif review.architecture_md:
            new_stage = EcosystemStageStatus.ARCHITECTURE_DONE
            promoted_counts["architecture_done"] += 1
        elif review.summary_md:
            new_stage = EcosystemStageStatus.SHALLOW_DONE
            promoted_counts["shallow_done"] += 1
        else:
            promoted_counts["kept_queued"] += 1
            continue
        try:
            await repo.update_deep_review_stage(
                review.id,
                new_stage,
                project_id=review.project_id,
            )
        except Exception as exc:
            logger.warning(
                "v1.5.0 backfill: update_deep_review_stage(%s) failed: %s",
                review.id[:8],
                exc,
            )

    if any(v > 0 for v in promoted_counts.values()):
        logger.info(
            "v1.5.0 backfill: deep_review stage_status promoted "
            "(shallow_done=%d, architecture_done=%d, debated=%d, kept_queued=%d)",
            promoted_counts["shallow_done"],
            promoted_counts["architecture_done"],
            promoted_counts["debated"],
            promoted_counts["kept_queued"],
        )

    # 2) Project ecosystem_project_settings defaults
    settings_created = 0
    for p in projects:
        is_ai_team_os = "ai team os" in (p.name or "").lower() or "ai-team-os" in (
            p.name or ""
        ).lower()
        try:
            existing = await repo.get_ecosystem_project_settings(p.id)
            if existing is None:
                await repo.ensure_ecosystem_project_settings(
                    p.id, is_ai_team_os=is_ai_team_os
                )
                settings_created += 1
        except Exception as exc:
            logger.warning(
                "v1.5.0 backfill: ensure_ecosystem_project_settings(%s) failed: %s",
                p.name,
                exc,
            )

    if settings_created > 0:
        logger.info(
            "v1.5.0 backfill: created %d EcosystemProjectSettings rows",
            settings_created,
        )


async def _auto_create_projects(repo: StorageRepository) -> None:
    """Back-fill project_id on orphan Teams from each team's OWN members.

    Attribution recognizes only the one authoritative chain of "the team's own members": the Leader row's project_id
    (locked at session start by
    that session's cwd; see the ownership iron rule "the session's startup directory is authoritative") takes priority,
    followed by any already-bound
    member. If nothing resolves, it **stays unowned**.

    2026-07-27 incident: this function originally bound **all** orphan teams at once to "the project matched by the API
    process's own
    os.getcwd() longest-prefix match, and when nothing matched it fell back to existing_projects[0].
    The API process's cwd is global state shared across sessions — whichever directory the server starts from, that
    project swallows
    all unowned teams. In practice it polluted 116 teams (many session-* container teams of AI Team OS sessions were
    recorded as Wenge), and it propagated along _on_subagent_start's `project_id=team.project_id`
    into agent rows and the {project_path} in their system_prompts.
    e812de4 already disabled the same kind of cwd fallback in hook_translator's rebind path; this startup path slipped
    through.
    """
    teams = await repo.list_teams()
    orphan_teams = [t for t in teams if not t.project_id]
    # Workflow teams do not participate in the fallback claim: their ownership is precisely backfilled by the
    # observation layer from the on-disk slug (file source of truth)
    # (user decision 2026-07-07: an external project's workflow team was once sucked into the OS project by the API
    # process's cwd;
    # when no project matches, it stays unowned — never guess).
    orphan_teams = [
        t for t in orphan_teams if (t.config or {}).get("kind") != "workflow"
    ]
    if not orphan_teams:
        return

    linked = 0
    for team in orphan_teams:
        try:
            members = await repo.list_agents(team.id)
        except Exception as exc:  # noqa: BLE001 — a single team's failure must not drag down the whole startup backfill
            logger.warning("orphan team backfill: list_agents(%s) failed: %s", team.id, exc)
            continue
        pid = _project_id_from_members(members)
        if not pid:
            continue
        await repo.update_team(team.id, project_id=pid)
        linked += 1

    logger.info(
        "Orphan team backfill: %d/%d linked via own members; %d left unbound "
        "(no member authority — deliberately not guessed)",
        linked,
        len(orphan_teams),
        len(orphan_teams) - linked,
    )


def _project_id_from_members(members: list) -> str | None:
    """Resolve a team's project from its own member rows. Leader wins."""
    leaders = [a for a in members if getattr(a, "role", "") == "leader"]
    for pool in (leaders, members):
        for a in pool:
            pid = getattr(a, "project_id", None)
            if pid:
                return str(pid)
    return None


async def _backfill_shallow_done_status(repo: StorageRepository) -> None:
    """v1.6.1 backfill: align the status field of stage='shallow_done' rows to 'completed'.

    Idempotent: only modifies dirty rows with status='running'; rows already at 'completed' are untouched.
    """
    try:
        count = await repo.backfill_shallow_done_status_completed()
        if count > 0:
            logger.info(
                "v1.6.1 backfill: fixed %d shallow_done rows with status=running -> completed",
                count,
            )
        else:
            logger.debug("v1.6.1 backfill: no shallow_done/running rows to fix")
    except Exception as exc:
        logger.warning("_backfill_shallow_done_status failed (non-fatal): %s", exc)


async def _backfill_task_memos(repo: StorageRepository) -> None:
    """Memory system v2 P0: one-time backfill of tasks.config['memo'] → the task_memos table.

    Idempotent: a non-empty task_memos table is treated as already migrated and skipped directly; the original JSON is
    kept untouched (archive). Non-fatal.
    """
    try:
        count = await repo.backfill_task_memos_from_config()
        if count > 0:
            logger.info(
                "memory-v2 P0 backfill: migrated %d legacy memos into task_memos", count
            )
        else:
            logger.debug(
                "memory-v2 P0 backfill: task_memos already populated or no legacy memos"
            )
    except Exception as exc:
        logger.warning("_backfill_task_memos failed (non-fatal): %s", exc)


async def _backfill_dual_axis_convergence(repo: StorageRepository) -> None:
    """D5 dual-axis convergence backfill: fully align the status column with the derived semantics (stage_status is the
    single authoritative axis).

    The 5 idempotent UPDATEs run in the fixed R1→R2→F1→F2→F3 order (the iron rule: reverse before forward),
    see repository.backfill_deep_review_dual_axis for details. Must run after
    _backfill_v150_progressive_funnel (infers stage from content markers) and
    _backfill_shallow_done_status (the v1.6.1 subset). Non-fatal.
    """
    try:
        counts = await repo.backfill_deep_review_dual_axis()
        total = sum(counts.values())
        if total > 0:
            logger.info(
                "D5 dual-axis backfill: %d rows updated (%s)",
                total,
                ", ".join(f"{k}={v}" for k, v in counts.items()),
            )
        else:
            logger.debug("D5 dual-axis backfill: no rows to fix")
    except Exception as exc:
        logger.warning("_backfill_dual_axis_convergence failed (non-fatal): %s", exc)


async def _backfill_alert_max_new_per_scan(repo: StorageRepository) -> None:
    """v1.6.1 Phase 2 startup migration: migrate max_new_per_scan from scan_profile.alert_thresholds into settings.

    Only migrates when settings.alert_max_new_per_scan is still the default 50 and scan_profile has a non-default value.
    Idempotent: already-migrated projects are skipped (a settings value != 50 is treated as already migrated or manually
    set by the user).
    """
    try:
        projects = await repo.list_projects()
        if not projects:
            return
        for project in projects:
            try:
                settings = await repo.ensure_ecosystem_project_settings(project.id)
                # If it is already non-default, the user has set it or it has already been migrated; skip
                current_val = getattr(settings, "alert_max_new_per_scan", 50)
                if current_val != 50:
                    continue
                # Find this project's active scan_profile
                scan_profile = await repo.get_active_scan_profile(project.id)
                if scan_profile is None:
                    continue
                profile_data = scan_profile.profile or {}
                alert_thresholds = profile_data.get("alert_thresholds", {})
                sp_max_new = alert_thresholds.get("max_new_per_scan")
                if sp_max_new is not None and sp_max_new != 50:
                    # Migrate the non-default value into settings
                    from aiteam.types import EcosystemProjectSettings as _Settings
                    updated = _Settings(
                        project_id=project.id,
                        min_stars=settings.min_stars,
                        top_n=settings.top_n,
                        refresh_interval_days=settings.refresh_interval_days,
                        auto_shallow_on_archive=settings.auto_shallow_on_archive,
                        focus_topics=settings.focus_topics,
                        focus_languages=settings.focus_languages,
                        shallow_concurrency=settings.shallow_concurrency,
                        deep_concurrency=settings.deep_concurrency,
                        alert_max_new_per_scan=sp_max_new,
                        created_at=settings.created_at,
                    )
                    await repo.upsert_ecosystem_project_settings(updated)
                    logger.info(
                        "backfill alert_max_new_per_scan: project=%s migrated %d from scan_profile",
                        project.id[:8],
                        sp_max_new,
                    )
            except Exception as exc:
                logger.warning(
                    "_backfill_alert_max_new_per_scan project=%s failed: %s",
                    project.id[:8],
                    exc,
                )
    except Exception as exc:
        logger.warning("_backfill_alert_max_new_per_scan failed (non-fatal): %s", exc)


async def _startup_reconciliation(repo: StorageRepository) -> None:
    """Startup reconciliation — reset lingering BUSY agents on API restart.

    Resets stale *state* only. ``session_id`` is **identity** and is never touched
    here: which session a row belongs to does not change because the API process
    restarted.

    The old code cleared it, on the stated rationale that "OS restart means
    previous CC sessions no longer exist". That premise never held — the API is
    started *by* a live CC session (and gets restarted mid-session via the
    restart tool or the watcher), so a restart says nothing about session
    liveness. The effect was a full-table wipe on every boot: production carried
    2,568 agent rows with exactly 2 non-empty ``session_id``, and the events log
    shows the sweep in the act (five rows updated inside one second, changes
    ``["status","current_task","session_id"]`` for busy rows and ``["session_id"]``
    for the rest). This is the same class of bug as the SessionEnd wipe fixed in
    91e07ad and the Stop mode-2 global clobber removed alongside it; this was the
    third and last copy.

    Consequences of the wipe went well beyond a NULL column: leader reuse keys on
    ``find_agents_by_session``, so a wiped session could not find its own leader
    and minted a fresh row on every resume, and token attribution lost the
    agent→session hop entirely.

    Also sets waiting agents with >1 hour of inactivity to offline.
    """
    from datetime import timedelta

    stale_cutoff = utc_now() - timedelta(hours=1)
    teams = await repo.list_teams()
    reconciled = 0
    stale_count = 0
    for team in teams:
        agents = await repo.list_agents(team.id)
        for agent in agents:
            was_busy = agent.status == AgentStatus.BUSY
            if was_busy:
                await repo.update_agent(
                    agent.id,
                    status=AgentStatus.WAITING.value,
                    current_task=None,
                )
                reconciled += 1

            # Clean up stale agents: waiting with >1 hour inactivity -> offline
            effective_status = AgentStatus.WAITING.value if was_busy else agent.status
            if (
                effective_status in (AgentStatus.WAITING, AgentStatus.WAITING.value)
                and agent.last_active_at
                and agent.last_active_at < stale_cutoff
            ):
                await repo.update_agent(agent.id, status=AgentStatus.OFFLINE.value)
                stale_count += 1

    if reconciled > 0:
        logger.warning(
            "Startup reconciliation: %d busy agents reset to waiting (identity kept)",
            reconciled,
        )
    else:
        logger.info("Startup reconciliation: no reset needed")
    if stale_count > 0:
        logger.info("Startup reconciliation: %d stale waiting agents set to offline", stale_count)


async def init_dependencies() -> None:
    """Initialize all dependencies (called during lifespan startup)."""
    global _repository, _memory_store, _event_bus, _manager, _reaper  # noqa: PLW0603
    global _watchdog_runner, _hook_translator, _task_wall_engine  # noqa: PLW0603

    _repository = StorageRepository()
    await _repository.init_db()

    # Determine actual DB URL for Alembic operations
    from aiteam.storage.connection import DEFAULT_DB_URL
    _db_url = _repository._db_url or DEFAULT_DB_URL

    # Always run hand-written migrations first — they are idempotent (check column existence
    # before ALTER TABLE) and cover fields added in v1.0/v1.1 that Alembic stamp head may have
    # skipped. Only stamp head when the DB has never been tracked by Alembic.
    await _run_migrations()

    alembic_revision = _get_alembic_current_revision(_db_url)
    if alembic_revision is None:
        # Legacy DB (no alembic_version table): stamp head so future Alembic revisions can run
        logger.info("Alembic: untracked DB detected, stamping head after legacy migrations")
        _run_alembic_stamp_head(_db_url)
    else:
        logger.info("Alembic: DB is at revision %s", alembic_revision)

    _memory_store = MemoryStore(repository=_repository)
    _event_bus = EventBus(repo=_repository)
    _manager = TeamManager(
        repository=_repository,
        memory=_memory_store,
        event_bus=_event_bus,
    )
    _hook_translator = HookTranslator(repo=_repository, event_bus=_event_bus)
    _task_wall_engine = TaskWallEngine(repo=_repository)

    # Startup reconciliation: clear lingering BUSY states
    await _startup_reconciliation(_repository)

    # Auto-create Projects for Teams without project_id
    await _auto_create_projects(_repository)

    # Ensure default ecosystem tags exist
    await ensure_default_tags(_repository)

    # Stage J: backfill legacy ecosystem rows to the default project
    # so the existing 188 repos remain visible from "AI Team OS" project.
    await _backfill_ecosystem_default_project(_repository)

    # v1.5.0-A backfill: deep_review stage_status + project ecosystem settings
    await _backfill_v150_progressive_funnel(_repository)

    # Stage J: refresh project_dir → project_id resolution cache
    await refresh_project_dir_cache(_repository)

    # v1.6.1 Phase 2: backfill alert_max_new_per_scan from scan_profile to settings
    await _backfill_alert_max_new_per_scan(_repository)

    # User ruling 2026-07-10: the ecosystem periodic-refresh cron is retired (CC is not resident, so a long-term timer
    # is meaningless and its emit_event never had a consumer) — replaced by the on-demand POST /api/ecosystem/refresh
    # + MCP ecosystem_refresh, invoked manually when an update is needed. _ensure_ecosystem_weekly_cron has been
    # deleted.

    # Bug 2 second fix: backfill the status field of stage='shallow_done' rows, running -> completed
    await _backfill_shallow_done_status(_repository)

    # D5 dual-axis convergence: full backfill of the status derived view (R1→R2→F1→F2→F3, idempotent)
    await _backfill_dual_axis_convergence(_repository)

    # Memory system v2 P0: one-time backfill for the task memo table promotion (runs only when the table is empty;
    # idempotent)
    await _backfill_task_memos(_repository)

    # Start StateReaper background harvester
    _reaper = StateReaper(repo=_repository, event_bus=_event_bus)
    _reaper.start()

    # Start WatchdogRunner background patrol
    _watchdog_checker = WatchdogChecker(repo=_repository)
    _watchdog_runner = WatchdogRunner(checker=_watchdog_checker, event_bus=_event_bus)
    _watchdog_runner.start()


async def cleanup_dependencies() -> None:
    """Clean up all dependencies (called during lifespan shutdown)."""
    global _repository, _memory_store, _event_bus, _manager, _reaper  # noqa: PLW0603
    global _watchdog_runner, _hook_translator, _task_wall_engine  # noqa: PLW0603

    # Stop WatchdogRunner first
    if _watchdog_runner is not None:
        await _watchdog_runner.stop()
        _watchdog_runner = None

    # Stop StateReaper
    if _reaper is not None:
        await _reaper.stop()
        _reaper = None

    await close_db()
    _repository = None
    _memory_store = None
    _event_bus = None
    _manager = None
    _hook_translator = None
    _task_wall_engine = None


def get_manager() -> TeamManager:
    """Get TeamManager instance, injected via FastAPI Depends()."""
    if _manager is None:
        msg = "TeamManager not initialized, ensure application has started"
        raise RuntimeError(msg)
    return _manager


def get_repository() -> StorageRepository:
    """Get the default StorageRepository instance."""
    if _repository is None:
        msg = "StorageRepository not initialized"
        raise RuntimeError(msg)
    return _repository


def get_global_repository() -> StorageRepository:
    """Get the default global StorageRepository — always the default DB regardless of project context.

    Used by cross-project message endpoints which must read/write the shared global DB,
    not per-project databases.
    """
    if _repository is None:
        msg = "StorageRepository not initialized"
        raise RuntimeError(msg)
    return _repository


def get_scoped_repository(request: Request) -> StorageRepository:
    """Get a project-scoped StorageRepository.

    Resolution order:
    1. X-Project-Id header (explicit project id, used by Dashboard / MCP).
    2. X-Project-Dir header (path string, resolved to id via root_path
       longest-prefix match). Used by MCP tools that only know the cwd.
    3. Falls back to the global unscoped repository when neither is present.

    When a project scope is resolved, all list/create operations on
    project-aware tables (the ecosystem's 5 data tables + tasks/teams/agents) are
    automatically filtered to that project via _apply_project_filter.
    """
    # Delegate to get_repository() so FastAPI dependency overrides on
    # get_repository (used by integration tests) still work.
    base_repo = get_repository()
    project_id = request.headers.get("X-Project-Id", "")
    project_dir = ""
    if not project_id:
        project_dir = request.headers.get("X-Project-Dir", "")
        if project_dir:
            # Decode percent-encoding: MCP side encodes non-ASCII (e.g. Chinese
            # path components) so the header stays latin-1 safe.
            import urllib.parse as _up
            project_dir = _up.unquote(project_dir)
            project_id = _resolve_project_id_from_dir(project_dir)
    if not project_id:
        # Unregistered dir: carry the cwd through so direction-memory scoping can
        # bucket by a dir fingerprint instead of silently broadcasting into the
        # global "system" bucket (2026-07-21 cross-contamination fix). We keep
        # _project_scope empty — project_id table filtering must not match a
        # non-existent "dir:" project. When there is no cwd at all, return the
        # shared base_repo unchanged (preserves integration-test dependency
        # overrides for header-less calls).
        if project_dir:
            return StorageRepository(
                db_url=base_repo._db_url, unresolved_dir=project_dir
            )
        return base_repo
    return StorageRepository(db_url=base_repo._db_url, project_scope=project_id)


def _resolve_project_id_from_dir(project_dir: str) -> str:
    """Resolve a project_id from a directory path via longest-prefix root_path match.

    Returns "" when no project matches (caller falls back to unscoped repo).
    Cached projects list is fetched lazily; tolerable cost since this only
    runs when the caller did not already supply X-Project-Id.
    """
    if _repository is None or not project_dir:
        return ""
    try:
        cwd = project_dir.replace("\\", "/").rstrip("/").lower()
        # Best-effort sync read by leveraging asyncio loop.run_until_complete is
        # unsafe inside FastAPI request context — instead we keep a tiny in-process
        # cache populated by the lifespan startup hook.
        cache = _project_dir_cache
        if not cache:
            return ""
        best_id = ""
        best_len = -1
        for rp_lower, pid in cache.items():
            if rp_lower and (cwd == rp_lower or cwd.startswith(rp_lower + "/")) and len(rp_lower) > best_len:
                best_id = pid
                best_len = len(rp_lower)
        return best_id
    except Exception:
        return ""


# Module-level cache: lower-cased root_path -> project_id, refreshed at startup.
_project_dir_cache: dict[str, str] = {}


async def refresh_project_dir_cache(repo: StorageRepository) -> None:
    """Reload the project_dir → project_id cache from DB. Called at startup."""
    try:
        projects = await repo.list_projects()
    except Exception:
        return
    new_cache: dict[str, str] = {}
    for p in projects:
        rp = (p.root_path or "").replace("\\", "/").rstrip("/").lower()
        if rp:
            new_cache[rp] = p.id
    _project_dir_cache.clear()
    _project_dir_cache.update(new_cache)


def get_memory_store() -> MemoryStore:
    """Get MemoryStore instance, injected via FastAPI Depends()."""
    if _memory_store is None:
        msg = "MemoryStore not initialized, ensure application has started"
        raise RuntimeError(msg)
    return _memory_store


def get_event_bus() -> EventBus:
    """Get EventBus instance, injected via FastAPI Depends()."""
    if _event_bus is None:
        msg = "EventBus not initialized, ensure application has started"
        raise RuntimeError(msg)
    return _event_bus


def get_hook_translator() -> HookTranslator:
    """Get HookTranslator singleton, injected via FastAPI Depends()."""
    if _hook_translator is None:
        msg = "HookTranslator not initialized, ensure application has started"
        raise RuntimeError(msg)
    return _hook_translator


def get_reaper() -> StateReaper | None:
    """Get the StateReaper singleton (owns the WakeAgentManager used by fleet dispatch).

    Returns None when the reaper has not started yet (e.g. very early startup), so
    callers must handle absence rather than assume it is always available.
    """
    return _reaper


def get_task_wall_engine() -> TaskWallEngine:
    """Get TaskWallEngine instance, injected via FastAPI Depends()."""
    if _task_wall_engine is None:
        msg = "TaskWallEngine not initialized, ensure application has started"
        raise RuntimeError(msg)
    return _task_wall_engine
