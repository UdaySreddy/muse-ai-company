"""AI Team OS — Permanent member configuration routes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/config", tags=["team-config"])

# Configuration file path
CONFIG_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent / "plugin" / "config"
CONFIG_FILE = CONFIG_DIR / "team-defaults.json"

# Default configuration
_DEFAULT_CONFIG: dict[str, Any] = {
    "auto_create_team": True,
    "team_name_prefix": "auto",
    "permanent_members": [],
}


# ============================================================
# Request models
# ============================================================


class PermanentMember(BaseModel):
    """Permanent member definition."""

    name: str
    role: str
    model: str = ""  # empty = inherit the default startup model (maintenance-free across version changes)
    enabled: bool = True


class TeamDefaultsConfig(BaseModel):
    """Permanent member configuration."""

    auto_create_team: bool = True
    team_name_prefix: str = "auto"
    permanent_members: list[PermanentMember] = Field(default_factory=list)


class MemberUpdate(BaseModel):
    """Update permanent member request (partial update)."""

    role: str | None = None
    model: str | None = None
    enabled: bool | None = None


# ============================================================
# Helper functions
# ============================================================


def _read_config() -> dict[str, Any]:
    """Read configuration file."""
    if not CONFIG_FILE.exists():
        return dict(_DEFAULT_CONFIG)
    text = CONFIG_FILE.read_text(encoding="utf-8")
    return json.loads(text)


def _write_config(data: dict[str, Any]) -> None:
    """Write configuration file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


# ============================================================
# Routes
# ============================================================


@router.get("/team-defaults")
async def get_team_defaults() -> dict[str, Any]:
    """Read current permanent member configuration."""
    config = _read_config()
    return {"success": True, "data": config}


@router.put("/team-defaults")
async def update_team_defaults(body: TeamDefaultsConfig) -> dict[str, Any]:
    """Update configuration (full replacement)."""
    data = body.model_dump()
    _write_config(data)
    return {"success": True, "data": data, "message": "Resident member configuration updated"}


@router.post("/team-defaults/members", status_code=201)
async def add_member(body: PermanentMember) -> dict[str, Any]:
    """Add a permanent member."""
    config = _read_config()
    members: list[dict[str, Any]] = config.get("permanent_members", [])

    # Check if name already exists
    for m in members:
        if m["name"] == body.name:
            raise HTTPException(status_code=409, detail=f"Member '{body.name}' already exists")

    members.append(body.model_dump())
    config["permanent_members"] = members
    _write_config(config)
    return {"success": True, "data": body.model_dump(), "message": f"Resident member '{body.name}' added"}


@router.delete("/team-defaults/members/{name}")
async def remove_member(name: str) -> dict[str, Any]:
    """Remove a permanent member."""
    config = _read_config()
    members: list[dict[str, Any]] = config.get("permanent_members", [])

    original_len = len(members)
    members = [m for m in members if m["name"] != name]

    if len(members) == original_len:
        raise HTTPException(status_code=404, detail=f"Member '{name}' not found")

    config["permanent_members"] = members
    _write_config(config)
    return {"success": True, "message": f"Resident member '{name}' deleted"}


@router.patch("/team-defaults/members/{name}")
async def update_member(name: str, body: MemberUpdate) -> dict[str, Any]:
    """Update a permanent member (e.g., enable/disable, change model)."""
    config = _read_config()
    members: list[dict[str, Any]] = config.get("permanent_members", [])

    target = None
    for m in members:
        if m["name"] == name:
            target = m
            break

    if target is None:
        raise HTTPException(status_code=404, detail=f"Member '{name}' not found")

    updates = body.model_dump(exclude_none=True)
    target.update(updates)
    _write_config(config)
    return {"success": True, "data": target, "message": f"Resident member '{name}' updated"}
