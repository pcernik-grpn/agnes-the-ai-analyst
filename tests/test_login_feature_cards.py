"""Render-side coverage for the /login feature-card hide toggle.

The resolver itself is unit-tested in ``test_instance_config.py``
(``TestHiddenLoginFeatures``). This pins the template contract: the MCP card
renders by default and disappears when the feature key is toggled off via
``AGNES_INSTANCE_HIDE_LOGIN_FEATURES``, while an always-present card (Data
packages) stays put. ``get_hidden_login_features()`` reads the env var at
request time, so the toggle is exercised without rebuilding the app.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

# Unique heading markers per card — the <p> body of the MCP card also mentions
# "MCP", so match on the heading to test the card specifically.
MCP_CARD = '<h3>MCP <span class="feature-beta">Beta</span></h3>'
DATA_CARD = "<h3>Data packages</h3>"


@pytest.fixture
def web_client(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    (tmp_path / "state").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "extracts").mkdir()
    from src.db import close_system_db

    close_system_db()

    app = shared_app
    yield TestClient(app)
    close_system_db()


def test_all_cards_render_by_default(web_client, monkeypatch):
    monkeypatch.delenv("AGNES_INSTANCE_HIDE_LOGIN_FEATURES", raising=False)
    resp = web_client.get("/login")
    assert resp.status_code == 200
    assert MCP_CARD in resp.text
    assert DATA_CARD in resp.text


def test_mcp_card_hidden_when_toggled_off(web_client, monkeypatch):
    monkeypatch.setenv("AGNES_INSTANCE_HIDE_LOGIN_FEATURES", "mcp")
    resp = web_client.get("/login")
    assert resp.status_code == 200
    # MCP card gone, but the other cards are untouched.
    assert MCP_CARD not in resp.text
    assert DATA_CARD in resp.text
