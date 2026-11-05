"""AI Team OS — persistent member configuration API tests."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from aiteam.api.routes import team_config


@pytest.fixture()
def config_client(tmp_path):
    """Create a test client, with the configuration file pointing at a temporary directory."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "team-defaults.json"

    # Write the default configuration
    default = {
        "auto_create_team": True,
        "team_name_prefix": "auto",
        "permanent_members": [
            {
                "name": "qa-observer",
                "role": "Resident QA Observer",
                "model": "claude-sonnet-4-6",
                "enabled": True,
            },
            {
                "name": "bug-fixer",
                "role": "Resident Bug Engineer",
                "model": "claude-sonnet-4-6",
                "enabled": True,
            },
        ],
    }
    config_file.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")

    # Patch the configuration path
    with (
        patch.object(team_config, "CONFIG_DIR", config_dir),
        patch.object(team_config, "CONFIG_FILE", config_file),
    ):
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(team_config.router)
        client = TestClient(app)
        yield client


class TestGetDefaults:
    """Test GET of the default configuration."""

    def test_get_defaults(self, config_client):
        resp = config_client.get("/api/config/team-defaults")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["data"]["auto_create_team"] is True
        assert len(data["data"]["permanent_members"]) == 2

    def test_get_defaults_member_names(self, config_client):
        resp = config_client.get("/api/config/team-defaults")
        members = resp.json()["data"]["permanent_members"]
        names = [m["name"] for m in members]
        assert "qa-observer" in names
        assert "bug-fixer" in names


class TestPutDefaults:
    """Test PUT updating the configuration."""

    def test_put_update_config(self, config_client):
        new_config = {
            "auto_create_team": False,
            "team_name_prefix": "test",
            "permanent_members": [
                {
                    "name": "researcher",
                    "role": "Researcher",
                    "model": "claude-opus-4-7",
                    "enabled": True,
                }
            ],
        }
        resp = config_client.put("/api/config/team-defaults", json=new_config)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["data"]["auto_create_team"] is False
        assert data["data"]["team_name_prefix"] == "test"
        assert len(data["data"]["permanent_members"]) == 1

        # Verify persistence
        resp2 = config_client.get("/api/config/team-defaults")
        assert resp2.json()["data"]["auto_create_team"] is False


class TestPostMember:
    """Test POST adding a member."""

    def test_add_member(self, config_client):
        new_member = {
            "name": "researcher",
            "role": "Researcher",
            "model": "claude-opus-4-7",
            "enabled": True,
        }
        resp = config_client.post("/api/config/team-defaults/members", json=new_member)
        assert resp.status_code == 201
        assert resp.json()["success"] is True

        # Verify it was added
        resp2 = config_client.get("/api/config/team-defaults")
        members = resp2.json()["data"]["permanent_members"]
        assert len(members) == 3
        names = [m["name"] for m in members]
        assert "researcher" in names

    def test_add_duplicate_member(self, config_client):
        dup_member = {
            "name": "qa-observer",
            "role": "Duplicate QA",
        }
        resp = config_client.post("/api/config/team-defaults/members", json=dup_member)
        assert resp.status_code == 409


class TestDeleteMember:
    """Test DELETE removing a member."""

    def test_delete_member(self, config_client):
        resp = config_client.delete("/api/config/team-defaults/members/qa-observer")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        # Verify it was deleted
        resp2 = config_client.get("/api/config/team-defaults")
        members = resp2.json()["data"]["permanent_members"]
        assert len(members) == 1
        assert members[0]["name"] == "bug-fixer"

    def test_delete_nonexistent_member(self, config_client):
        resp = config_client.delete("/api/config/team-defaults/members/nonexistent")
        assert resp.status_code == 404


class TestPatchMember:
    """Test PATCH updating a member."""

    def test_disable_member(self, config_client):
        resp = config_client.patch(
            "/api/config/team-defaults/members/qa-observer",
            json={"enabled": False},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["data"]["enabled"] is False
        assert data["data"]["name"] == "qa-observer"

    def test_update_model(self, config_client):
        resp = config_client.patch(
            "/api/config/team-defaults/members/bug-fixer",
            json={"model": "claude-opus-4-7"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["model"] == "claude-opus-4-7"

    def test_update_role(self, config_client):
        resp = config_client.patch(
            "/api/config/team-defaults/members/qa-observer",
            json={"role": "Senior QA Observer"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["role"] == "Senior QA Observer"

    def test_patch_nonexistent_member(self, config_client):
        resp = config_client.patch(
            "/api/config/team-defaults/members/nonexistent",
            json={"enabled": False},
        )
        assert resp.status_code == 404
