"""AI Team OS — Configuration management.

Responsible for loading and validating aiteam.yaml config files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from aiteam.types import OrchestrationMode

# ============================================================
# Configuration models
# ============================================================


class ProjectInfo(BaseModel):
    """Project basic info."""

    name: str = ""
    description: str = ""
    language: str = "zh"


class InfrastructureConfig(BaseModel):
    """Infrastructure configuration."""

    storage_backend: Literal["sqlite", "postgresql"] = "sqlite"
    memory_backend: Literal["file"] = "file"
    cache_backend: Literal["memory", "redis"] = "memory"
    redis_url: str = "redis://localhost:6379/0"
    dashboard_port: int = 3000
    api_port: int = 8000
    db_url: str = ""

    def get_db_url(self, project_dir: Path) -> str:
        """Get database URL, defaults to SQLite in project directory."""
        if self.db_url:
            return self.db_url
        if self.storage_backend == "postgresql":
            return "postgresql+asyncpg://localhost/aiteam"
        return f"sqlite+aiosqlite:///{project_dir / '.aiteam' / 'aiteam.db'}"


class DefaultsConfig(BaseModel):
    """Default configuration."""

    model: str = ""  # Empty = inherit the CC session model (do not bake in a concrete model again — the 4-7 phantom
    # lesson)
    max_context_ratio: float = Field(default=0.8, ge=0.1, le=1.0)


class AgentConfig(BaseModel):
    """Agent configuration."""

    name: str
    role: str
    system_prompt: str = ""
    model: str | None = None


class TeamMemberConfig(BaseModel):
    """Team configuration."""

    name: str = ""
    mode: str = "coordinate"
    leader: AgentConfig | None = None
    members: list[AgentConfig] = Field(default_factory=list)

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        valid = [m.value for m in OrchestrationMode]
        if v not in valid:
            msg = f"Invalid orchestration mode '{v}', supported modes: {', '.join(valid)}"
            raise ValueError(msg)
        return v


class ProjectConfig(BaseModel):
    """Complete configuration model for aiteam.yaml."""

    project: ProjectInfo = Field(default_factory=ProjectInfo)
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    infrastructure: InfrastructureConfig = Field(default_factory=InfrastructureConfig)
    team: TeamMemberConfig | None = None


# ============================================================
# Configuration loading
# ============================================================

CONFIG_FILENAME = "aiteam.yaml"
AITEAM_DIR = ".aiteam"

# ============================================================
# StateReaper timeout configuration
# ============================================================

REAPER_CHECK_INTERVAL = 60  # Reaper polling interval (seconds)
HOOK_SOURCE_TIMEOUT = 300  # hook-source agent heartbeat timeout (5min inactive -> offline)
API_SOURCE_TIMEOUT_WITH_FILE = 1200  # api-source timeout with team file (20 minutes)
API_SOURCE_TIMEOUT_NO_FILE = 600  # api-source timeout without team file (10 minutes)
MEETING_EXPIRY_MINUTES = 45  # Meeting auto-concludes after this many minutes without new messages
WATCHDOG_CHECK_INTERVAL = 60  # Watchdog patrol interval (seconds)
CLAUDE_HOME = "~/.claude"  # Claude Code home directory

# Retention for spent ``session-<sid8>`` container teams (days). One CC session owns
# exactly one container team and never reuses another's, so a long-lived process that
# starts new sessions leaves a trail of completed husks behind it — 78 of them on the
# author's machine (2026-07-27 measurement), all with zero tasks and zero meetings.
# The reaper purges only husks past this age; 0 or negative disables the purge entirely.
CONTAINER_TEAM_RETENTION_DAYS = 7
# Ceiling per reap cycle, so a first run against a long-neglected database cannot turn
# one tick into a long transaction (the cycle has a 30s hard timeout).
CONTAINER_TEAM_PURGE_MAX_PER_CYCLE = 20

# Silence grace (hours) before a workflow team that owns NO run record at all may be
# closed. Teams whose runs are all terminal settle after the ordinary 30-minute stale
# threshold — the run rows are proof the work finished. With zero run rows that proof
# is missing entirely: member silence could equally mean "done" or "the observability
# layer never caught up" (wf_id arriving late, the API being down while the run ran).
# Waiting longer costs a stale row on the Teams page; closing too early hides a live
# team from every active-only view (2026-07-25 wenge incident).
WORKFLOW_TEAM_NO_RUN_GRACE_HOURS = 24

# Notification / webhook configuration
# Set SLACK_WEBHOOK_URL in the environment or via the /api/settings/webhook API.
SLACK_WEBHOOK_URL: str = ""
# Events that trigger a webhook notification when SLACK_WEBHOOK_URL is configured.
NOTIFICATION_EVENTS: list[str] = [
    "task.completed",
    "task.failed",
    "briefing.added",
]

# Wake Agent settings
MAX_CONCURRENT_WAKES: int = 2
WAKE_TIMEOUT_SECONDS: int = 300
WAKE_MAX_TURNS: int = 10
WAKE_FUSE_THRESHOLD: int = 3

# Throttling for the Leader main session's token-usage collection (token attribution v1 stage 4, see
# docs/token-attribution-v1-design.md §3.3).
# Stop fires on every conversation turn, while the main session's transcript is a cumulative file whose full parse grows
# linearly more expensive with the session
# (measured 45.1 MB / 0.18 s). So Stop goes through the gate of these two thresholds, while SessionStart / SessionEnd /
# PostCompact always force a freeze and are not bound by this.
# ① The minimum interval since the last parse: in steady state, at most one parse per session per 5 minutes.
LEADER_USAGE_MIN_INTERVAL_SECONDS: int = 300
# ② The threshold for how far the transcript's on-disk time has advanced relative to the last measurement: when the
# window has not arrived but this turn has already been writing this long,
#    the magnitude may have changed significantly in the meantime, and measuring early is closer to reality than waiting
# for the window.
LEADER_USAGE_MTIME_ADVANCE_SECONDS: int = 600

# Fleet dispatch (fleet-layer design §4.3 / §9): minimum idle time before a ship
# (CC session) may be targeted by a headless `claude -p --resume` dispatch. Set more
# conservatively than session_probe's 15min live window so a dispatch never competes
# with a user who is actively typing in that session. Tunable per the §9 open question.
FLEET_DISPATCH_MIN_IDLE_SECONDS: int = 1800  # 30 minutes


def find_config_file(start_dir: Path | None = None) -> Path | None:
    """Find aiteam.yaml by searching upward from current directory."""
    current = start_dir or Path.cwd()
    while True:
        config_path = current / CONFIG_FILENAME
        if config_path.exists():
            return config_path
        parent = current.parent
        if parent == current:
            return None
        current = parent


def load_config(config_path: Path | None = None) -> ProjectConfig:
    """Load and validate configuration file."""
    if config_path is None:
        config_path = find_config_file()
    if config_path is None or not config_path.exists():
        return ProjectConfig()

    with open(config_path, encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}

    return ProjectConfig.model_validate(raw)


def generate_default_config() -> str:
    """Generate default aiteam.yaml content."""
    return """\
# AI Team OS project configuration
project:
  name: "my-project"
  description: "Project description"
  language: "zh"

defaults:
  model: "opus"  # tier alias, automatically follows the latest version
  max_context_ratio: 0.8

infrastructure:
  storage_backend: "sqlite"     # sqlite | postgresql
  memory_backend: "file"        # file (single SQLite backend)
  dashboard_port: 3000
  api_port: 8000

# team:
#   name: "dev-team"
#   mode: "coordinate"           # coordinate | broadcast | route | meet
#   leader:
#     name: "lead"
#     role: "Technical Director"
#   members:
#     - name: "dev-1"
#       role: "Backend Developer"
#     - name: "dev-2"
#       role: "Frontend Developer"
"""
