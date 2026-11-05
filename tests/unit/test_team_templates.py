"""AI Team OS — team template API tests."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiteam.api.routes import templates


@pytest.fixture()
def templates_client(tmp_path):
    """Create a test client, with the template file pointing at a temporary directory."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    templates_file = config_dir / "team-templates.json"

    data = {
        "templates": [
            {
                "id": "fullstack",
                "name": "Full-Stack Development Team",
                "description": "A standard development team with frontend + backend + testing",
                "members": [
                    {"name": "frontend-engineer", "role": "Frontend Engineer"},
                    {"name": "backend-engineer", "role": "Backend Engineer"},
                ],
            },
            {
                "id": "minimal",
                "name": "Minimal Team",
                "description": "A minimal configuration with only resident members",
                "members": [
                    {"name": "qa-observer", "role": "QA Observer"},
                ],
            },
        ]
    }
    templates_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    with (
        patch.object(templates, "CONFIG_DIR", config_dir),
        patch.object(templates, "TEMPLATES_FILE", templates_file),
    ):
        app = FastAPI()
        app.include_router(templates.router)
        client = TestClient(app)
        yield client


class TestListTemplates:
    """Test GET listing all templates."""

    def test_list_templates(self, templates_client):
        resp = templates_client.get("/api/config/team-templates")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert len(data["data"]) == 2
        ids = [t["id"] for t in data["data"]]
        assert "fullstack" in ids
        assert "minimal" in ids


class TestGetTemplate:
    """Test GET fetching a single template."""

    def test_get_template(self, templates_client):
        resp = templates_client.get("/api/config/team-templates/fullstack")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["data"]["id"] == "fullstack"
        assert data["data"]["name"] == "Full-Stack Development Team"
        assert len(data["data"]["members"]) == 2

    def test_template_not_found(self, templates_client):
        resp = templates_client.get("/api/config/team-templates/nonexistent")
        assert resp.status_code == 404
