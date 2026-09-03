"""API ↔ CLI parity for `agnes admin service-account …` (issue #1534).

Service accounts are PG-only (A3 ratchet — `users.kind`), so this parity
case lives here (with the ``pg_engine`` fixture) rather than in
``tests/test_cli_api_parity.py``, whose harness snapshots the DuckDB
app-state DB. Same discipline as ``test_sso_cli_parity.py``: fire the HTTP
path and the CLI path against the same backend and assert they leave
identical state.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from typer.testing import CliRunner

from cli.main import app as cli_app
from tests.db_pg._parity_sweep_util import build_seeded_client
from tests.test_cli_api_parity import _patch_cli_to_testclient

_SERVICE_ACCOUNT_CLI_MODULES = ["cli.commands.admin_service_account"]

runner = CliRunner()


@pytest.fixture
def service_account_cli(tmp_path, monkeypatch, pg_engine):
    client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
    _patch_cli_to_testclient(monkeypatch, _SERVICE_ACCOUNT_CLI_MODULES, client, admin_token)
    return client, admin_token


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _account_state(pg_engine, slug: str):
    with pg_engine.connect() as conn:
        row = (
            conn.execute(
                sa.text("SELECT email, name, kind, active FROM users WHERE email = :email"),
                {"email": f"{slug}@service.local"},
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


def _reset(pg_engine, slug: str):
    with pg_engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM users WHERE email = :email"), {"email": f"{slug}@service.local"})


def test_create_parity(service_account_cli, pg_engine):
    client, admin_token = service_account_cli

    # API path
    r = client.post(
        "/api/admin/service-accounts",
        json={"name": "Parity Bot", "slug": "parity-bot"},
        headers=_h(admin_token),
    )
    assert r.status_code == 201, r.text
    api_state = _account_state(pg_engine, "parity-bot")
    assert api_state is not None

    _reset(pg_engine, "parity-bot")

    # CLI path — same inputs.
    result = runner.invoke(cli_app, ["admin", "service-account", "create", "Parity Bot", "--slug", "parity-bot"])
    assert result.exit_code == 0, result.output
    cli_state = _account_state(pg_engine, "parity-bot")

    assert cli_state == api_state
    assert cli_state["kind"] == "service"
    assert cli_state["active"] is True


def test_the_minted_pat_authenticates_via_the_real_cli_client_path(service_account_cli, monkeypatch):
    """Not just REST via TestClient directly: the SA's own PAT authenticates
    through cli/client.py's actual code path -- `agnes auth token list` run
    AS the service account, exactly like an analyst's own PAT would."""
    client, admin_token = service_account_cli

    r = client.post(
        "/api/admin/service-accounts",
        json={"name": "CLI Bot", "slug": "cli-bot"},
        headers=_h(admin_token),
    )
    assert r.status_code == 201, r.text
    account_id = r.json()["id"]

    r = client.post(
        f"/api/admin/service-accounts/{account_id}/tokens",
        json={"name": "cli-token"},
        headers=_h(admin_token),
    )
    assert r.status_code == 201, r.text
    sa_token = r.json()["token"]

    # Re-point the CLI's own token-listing command at the SA's credential —
    # a real `cli.commands.tokens.list_tokens` invocation, not a raw HTTP call.
    _patch_cli_to_testclient(monkeypatch, ["cli.commands.tokens"], client, sa_token)
    result = runner.invoke(cli_app, ["auth", "token", "list", "--json"])
    assert result.exit_code == 0, result.output
    import json as _json

    rows = _json.loads(result.output)
    assert len(rows) == 1
    assert rows[0]["name"] == "cli-token"


def test_revoking_the_service_account_token_stops_it(service_account_cli):
    """Reuses the existing admin token-revoke surface -- DELETE
    /auth/admin/tokens/{id} already works admin-on-behalf, no new endpoint."""
    client, admin_token = service_account_cli

    r = client.post(
        "/api/admin/service-accounts",
        json={"name": "Revoke Bot", "slug": "revoke-bot"},
        headers=_h(admin_token),
    )
    assert r.status_code == 201, r.text
    account_id = r.json()["id"]

    r = client.post(
        f"/api/admin/service-accounts/{account_id}/tokens",
        json={"name": "to-revoke"},
        headers=_h(admin_token),
    )
    assert r.status_code == 201, r.text
    token_id, sa_token = r.json()["id"], r.json()["token"]
    sa_headers = _h(sa_token)

    assert client.get("/auth/tokens", headers=sa_headers).status_code == 200

    r = client.delete(f"/auth/admin/tokens/{token_id}", headers=_h(admin_token))
    assert r.status_code == 204, r.text

    assert client.get("/auth/tokens", headers=sa_headers).status_code == 401
