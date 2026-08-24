"""The leak sweep is exercised against a REAL app, including a known-bad state.

A sweep that has only ever been run against a correctly-configured instance
proves nothing: it would report "clean" just as loudly if its probes were
hitting 404s. So this suite seeds three personas against the real API and
asserts both directions — a correctly-scoped persona yields no findings, and
a persona that reaches MORE than it declared is reported as a LEAK.

Only the urllib transport in ``Client`` is not covered here (the probes are
driven through an ASGI-backed client with the same ``.call`` shape); that
part is exercised by running the script against a live host.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.auth.jwt import create_access_token
from src.db import get_system_db

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


@pytest.fixture(autouse=True)
def _keboola_instance(monkeypatch):
    fake_cfg = {
        "data_source": {
            "type": "keboola",
            "keboola": {
                "stack_url": "https://connection.keboola.com",
                "project_id": "1234",
                "token_env": "KEBOOLA_STORAGE_TOKEN",
            },
        },
    }
    monkeypatch.setattr("app.instance_config.load_instance_config", lambda *_a, **_kw: fake_cfg, raising=False)
    from app.instance_config import reset_cache

    reset_cache()
    yield
    reset_cache()


class AsgiClient:
    """``Client``-shaped adapter over the in-process app.

    Same ``.call(method, path, token, body) -> (status, body)`` contract the
    sweep functions consume, so the probes under test are the real ones.
    """

    def __init__(self, app):
        self._app = app

    def call(self, method: str, path: str, token, body=None):
        async def _run():
            transport = httpx.ASGITransport(app=self._app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                resp = await c.request(method, path, json=body, headers=headers)
                try:
                    return resp.status_code, resp.json()
                except ValueError:
                    return resp.status_code, resp.text

        return asyncio.run(_run())


@pytest.fixture
def matrix_env(e2e_env, mock_extract_factory, shared_app):
    """Two registered tables. ``analyst`` reaches t1 through a data package;
    ``outsider`` belongs to no group and reaches nothing."""
    from src.db import SYSTEM_ADMIN_GROUP
    from src.repositories.table_registry import TableRegistryRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.users import UserRepository
    from tests.conftest import grant_table_via_package

    conn = get_system_db()
    UserRepository(conn).create(id="admin1", email="admin@test.com", name="Admin")
    UserRepository(conn).create(id="analyst1", email="analyst@test.com", name="Analyst")
    UserRepository(conn).create(id="outsider1", email="outsider@elsewhere.com", name="Outsider")
    admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
    UserGroupMembersRepository(conn).add_member("admin1", admin_gid, source="system_seed")
    conn.close()

    app = shared_app
    client = TestClient(app)
    admin_token = create_access_token("admin1", "admin@test.com")

    for name in ("t1", "t2"):
        r = client.post(
            "/api/admin/register-table",
            json={"name": name, "source_type": "keboola", "query_mode": "local", "description": name},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 201, r.text

    mock_extract_factory(
        "keboola",
        [
            {"name": "t1", "data": [{"id": "1", "region": "eu"}]},
            {"name": "t2", "data": [{"id": "1", "region": "secret"}]},
        ],
    )
    from src.orchestrator import SyncOrchestrator

    SyncOrchestrator(analytics_db_path=e2e_env["analytics_db"]).rebuild()

    conn = get_system_db()
    t1_id = TableRegistryRepository(conn).get_by_name("t1")["id"]
    grant_table_via_package(conn, t1_id, "analyst1", group_name="leak-matrix-pkg")
    conn.close()

    return {
        "app": app,
        "t1_id": t1_id,
        "analyst_token": create_access_token("analyst1", "analyst@test.com"),
        "outsider_token": create_access_token("outsider1", "outsider@elsewhere.com"),
    }


def _persona(name, token, *, expect_tables=(), expect_collections=()):
    from leak_matrix import Persona

    return Persona(
        name=name,
        token=token,
        expect_tables=list(expect_tables),
        expect_collections=list(expect_collections),
    )


def test_correctly_scoped_personas_produce_no_findings(matrix_env):
    """The clean direction: an analyst declaring exactly the table they hold
    through a package, and an outsider declaring nothing, both come back
    with no leak and no wrongly-denied finding."""
    from leak_matrix import GAP, cross_check, sweep_persona

    client = AsgiClient(matrix_env["app"])
    analyst = _persona("analyst", matrix_env["analyst_token"], expect_tables=[matrix_env["t1_id"]])
    outsider = _persona("outsider", matrix_env["outsider_token"])

    findings = []
    for p in (analyst, outsider):
        sweep_persona(client, p, [], findings)
        cross_check(client, p, findings)

    real = [f for f in findings if f.severity != GAP]
    assert real == [], [f.line() for f in real]
    assert matrix_env["t1_id"] in analyst.queryable
    assert outsider.queryable == []


def test_a_persona_reaching_more_than_declared_is_reported(matrix_env):
    """The known-bad direction — the one that proves the sweep is not just
    printing 'clean'. The analyst genuinely holds t1, but declares nothing,
    so both the catalog probe and the query probe must fire."""
    from leak_matrix import LEAK, sweep_persona

    client = AsgiClient(matrix_env["app"])
    understated = _persona("analyst-understated", matrix_env["analyst_token"], expect_tables=[])

    findings = []
    sweep_persona(client, understated, [], findings)

    leaks = [f for f in findings if f.severity == LEAK]
    assert leaks, "a persona reaching an undeclared table must be reported"
    assert any(f.surface == "POST /api/query" and f.resource == matrix_env["t1_id"] for f in leaks), (
        "the enforcement probe (query), not just the catalog listing, must catch it"
    )


def test_an_expected_table_that_is_unreachable_is_reported(matrix_env):
    """The opposite mistake, which a leak-only sweep would stay silent about:
    a persona declaring a table they cannot actually reach. Reported as
    WRONGLY-DENIED so a broken grant is visible, not mistaken for safety."""
    from leak_matrix import WRONGLY_DENIED, sweep_persona

    client = AsgiClient(matrix_env["app"])
    overstated = _persona("outsider-overstated", matrix_env["outsider_token"], expect_tables=[matrix_env["t1_id"]])

    findings = []
    sweep_persona(client, overstated, [], findings)

    assert [f for f in findings if f.severity == WRONGLY_DENIED], (
        "a declared-but-unreachable table must surface as a finding"
    )


def test_a_persona_without_a_credential_is_a_reported_gap(matrix_env):
    """A missing credential must never render as an empty, clean-looking
    row — the whole point of the GAP severity."""
    from leak_matrix import GAP, sweep_persona

    client = AsgiClient(matrix_env["app"])
    p = _persona("no-token", None, expect_tables=["whatever"])

    findings = []
    sweep_persona(client, p, [], findings)

    assert [f for f in findings if f.severity == GAP], "an unswept persona must be reported, not silently skipped"


def test_render_states_plainly_that_gaps_are_not_a_clean_result(matrix_env):
    from leak_matrix import GAP, render, sweep_persona

    client = AsgiClient(matrix_env["app"])
    p = _persona("no-token", None)
    findings = []
    sweep_persona(client, p, [], findings)
    text = render([p], findings)

    assert "NOT SWEPT" in text
    assert "not a clean result" in text
    assert any(f.severity == GAP for f in findings)
