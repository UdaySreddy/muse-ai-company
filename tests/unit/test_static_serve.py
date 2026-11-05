"""AI Team OS — Dashboard static file mounting tests.

Verifies FastAPI serves the Dashboard static files correctly and does not intercept API routes.
"""

from __future__ import annotations

import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from aiteam.api.app import create_app


@asynccontextmanager
async def _noop_lifespan(app):
    yield


def _make_client(app):
    """Create a test client, skipping lifespan initialization."""
    app.router.lifespan_context = _noop_lifespan
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def fake_dist(tmp_path):
    """Create a mock dashboard/dist directory structure."""
    dist = tmp_path / "dashboard" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text(
        "<html><head><title>AI Team OS Dashboard</title></head>"
        "<body><div id='root'></div></body></html>",
        encoding="utf-8",
    )
    assets = dist / "assets"
    assets.mkdir()
    (assets / "index-abc123.js").write_text("console.log('app');", encoding="utf-8")
    (assets / "index-abc123.css").write_text("body{margin:0}", encoding="utf-8")
    return tmp_path


def test_root_serves_html(fake_dist):
    """When dist exists, / returns the Dashboard HTML."""
    with patch.object(
        Path, "resolve", lambda self: fake_dist / "src" / "aiteam" / "api" / "app.py"
    ):
        app = create_app()

    client = _make_client(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "AI Team OS Dashboard" in resp.text


def test_assets_served(fake_dist):
    """When dist exists, files under /assets/ are accessible."""
    with patch.object(
        Path, "resolve", lambda self: fake_dist / "src" / "aiteam" / "api" / "app.py"
    ):
        app = create_app()

    client = _make_client(app)
    resp = client.get("/assets/index-abc123.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]


def test_api_routes_not_intercepted(fake_dist):
    """API routes are not intercepted by static files; /api/teams returns JSON."""
    with patch.object(
        Path, "resolve", lambda self: fake_dist / "src" / "aiteam" / "api" / "app.py"
    ):
        app = create_app()

    client = _make_client(app)
    resp = client.get("/api/teams")
    # The API should respond (possibly 500 because deps are not initialized, but should not return HTML)
    assert resp.headers.get("content-type", "").startswith("application/json")


def test_app_works_without_dist():
    """When dist does not exist, the app starts normally and the API works."""
    with patch.object(
        Path,
        "resolve",
        lambda self: Path(tempfile.mkdtemp()) / "src" / "aiteam" / "api" / "app.py",
    ):
        app = create_app()

    client = _make_client(app)
    # The API should be accessible (possibly 500 because there are no deps, but the app itself does not crash)
    resp = client.get("/api/teams")
    assert resp.headers.get("content-type", "").startswith("application/json")
