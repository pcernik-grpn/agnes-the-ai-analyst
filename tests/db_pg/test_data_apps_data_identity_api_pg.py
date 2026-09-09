"""HTTP round-trip for `PATCH /api/data-apps/{slug}` `data_identity`.

The column is Postgres-only (A3 ratchet, revision 0113): the PG half proves
the owner can flip it, the response/GET echo it, the change is audited and a
reachable app is redeployed; the DuckDB half proves the typed 501 — never a
500, never a silent no-op — and that the pre-existing `description` write
still works there.
"""

from __future__ import annotations

import pytest
import yaml

from src.data_apps.runner_client import RunnerUnavailable


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class _FakeRunner:
    def __init__(self):
        self.up_calls: list = []

    def up(self, slug, spec, config_json):
        self.up_calls.append((slug, spec, config_json))
        return {"status": "ok"}

    def stop(self, *a, **kw):
        return {"status": "ok"}

    def status(self, *a, **kw):
        return {"container": "running"}


class _DeadRunner(_FakeRunner):
    def up(self, *a, **kw):
        raise RunnerUnavailable("runner down")


def _enable_data_apps(tmp_path):
    import app.instance_config as instance_config

    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "instance.yaml").write_text(yaml.dump({"data_apps": {"enabled": True}}))
    instance_config._instance_config = None


def _client(backend, tmp_path, monkeypatch, pg_engine, runner=None):
    from tests.db_pg._parity_sweep_util import build_seeded_client

    monkeypatch.setenv("AGNES_VAULT_KEY", "Z3Vlc3Mtd2hhdC10aGlzLWlzLWEtZmVybmV0LWtleS0wMDE=")
    client, admin_token = build_seeded_client(backend, tmp_path, monkeypatch, pg_engine)
    _enable_data_apps(tmp_path)
    from app.coordination.factory import reset_coordination_for_tests

    reset_coordination_for_tests()

    import app.api.data_apps as data_apps_api

    fake = runner or _FakeRunner()
    monkeypatch.setattr(data_apps_api, "_runner", lambda: fake)
    # `redeploy_current` clones from the internal git repo; the fake runner
    # never does, but the spec build must not need a real repo either.
    return client, admin_token, fake


def _owner():
    from app.auth.jwt import create_access_token
    from src.repositories import users_repo

    users_repo().create(id="owner1", email="owner@test.com", name="Owner")
    users_repo().create(id="stranger1", email="stranger@test.com", name="Stranger")
    return create_access_token("owner1", "owner@test.com"), create_access_token("stranger1", "stranger@test.com")


def _create_app(slug="ident-app", owner_id="owner1", state="created"):
    from src.repositories import data_apps_repo

    repo = data_apps_repo()
    app_id = repo.create(slug=slug, name="Ident", owner_user_id=owner_id)
    if state != "created":
        repo.set_state(app_id, state)
        repo.update(app_id, service_token_id="svc-prev")
    return app_id


def _audit_actions():
    from src.repositories import audit_repo

    rows, _cursor = audit_repo().query(action="data_app.data_identity_changed", limit=200)
    return [r["action"] for r in rows]


# ---------------------------------------------------------------------------
# Postgres — the feature
# ---------------------------------------------------------------------------


def test_owner_flips_data_identity_and_it_is_echoed_and_audited(tmp_path, monkeypatch, pg_engine):
    client, _admin, fake = _client("pg", tmp_path, monkeypatch, pg_engine)
    owner_token, stranger_token = _owner()
    _create_app()

    r = client.get("/api/data-apps/ident-app", headers=_auth(owner_token))
    assert r.status_code == 200 and r.json()["data_identity"] == "owner"

    r = client.patch("/api/data-apps/ident-app", json={"data_identity": "viewer"}, headers=_auth(owner_token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data_identity"] == "viewer"
    # `created` -> nothing to re-bake; the next deploy builds the new spec
    assert body["redeploy"] == {"triggered": False}
    assert fake.up_calls == []

    r = client.get("/api/data-apps/ident-app", headers=_auth(owner_token))
    assert r.json()["data_identity"] == "viewer"

    # Idempotent: same value again is a no-op, not a second audit row.
    r = client.patch("/api/data-apps/ident-app", json={"data_identity": "viewer"}, headers=_auth(owner_token))
    assert r.status_code == 200 and r.json()["redeploy"] == {"triggered": False}
    assert _audit_actions().count("data_app.data_identity_changed") == 1

    # A stranger is refused; a stranger's PATCH must not have changed anything.
    r = client.patch("/api/data-apps/ident-app", json={"data_identity": "owner"}, headers=_auth(stranger_token))
    assert r.status_code == 403
    assert client.get("/api/data-apps/ident-app", headers=_auth(owner_token)).json()["data_identity"] == "viewer"


def test_running_app_is_redeployed_synchronously_on_flip(tmp_path, monkeypatch, pg_engine):
    client, _admin, fake = _client("pg", tmp_path, monkeypatch, pg_engine)
    owner_token, _ = _owner()
    _create_app(state="running")

    r = client.patch("/api/data-apps/ident-app", json={"data_identity": "viewer"}, headers=_auth(owner_token))
    assert r.status_code == 200, r.text
    assert r.json()["redeploy"] == {"triggered": True, "ok": True}
    assert len(fake.up_calls) == 1
    slug, spec, config_json = fake.up_calls[0]
    assert slug == "ident-app"
    # The container is re-baked with the NEW mode and the new per-app secret.
    assert spec["env"]["AGNES_DATA_IDENTITY"] == "viewer"
    assert spec["env"]["AGNES_APP_SLUG"] == "ident-app"
    assert config_json["dataApp"]["secrets"]["AGNES_VIEWER_SECRET"]
    assert r.json()["state"] == "running"


def test_dead_runner_keeps_the_setting_and_reports_the_failure(tmp_path, monkeypatch, pg_engine):
    client, _admin, _fake = _client("pg", tmp_path, monkeypatch, pg_engine, runner=_DeadRunner())
    owner_token, _ = _owner()
    _create_app(state="running")

    r = client.patch("/api/data-apps/ident-app", json={"data_identity": "viewer"}, headers=_auth(owner_token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data_identity"] == "viewer"  # the row is the source of truth
    assert body["redeploy"]["triggered"] is True and body["redeploy"]["ok"] is False
    assert "runner" in body["redeploy"]["detail"]
    # ...and the app is NOT left serving a container whose mode contradicts
    # the registry: `_handle_runner_failure` parked it in `error`.
    assert body["state"] == "error"


def test_linked_and_draft_rows_refuse_data_identity(tmp_path, monkeypatch, pg_engine):
    client, admin_token, _fake = _client("pg", tmp_path, monkeypatch, pg_engine)
    _owner()
    from src.repositories import data_apps_repo

    repo = data_apps_repo()
    repo.create(slug="linked-x", name="L", owner_user_id="system", repo_mode="linked")
    parent = repo.create(slug="par", name="P", owner_user_id="owner1")
    repo.create_draft(parent_app_id=parent, slug="par--dev", branch="dev", owner_user_id="owner1")

    r = client.patch("/api/data-apps/linked-x", json={"data_identity": "viewer"}, headers=_auth(admin_token))
    assert r.status_code == 400, r.text
    r = client.patch("/api/data-apps/par--dev", json={"data_identity": "viewer"}, headers=_auth(admin_token))
    assert r.status_code == 400 and r.json()["detail"] == "draft_has_no_data_identity"


# ---------------------------------------------------------------------------
# DuckDB — fails clean, and the old write path still works
# ---------------------------------------------------------------------------


def test_duckdb_answers_a_typed_501_and_changes_nothing(tmp_path, monkeypatch, pg_engine):
    client, _admin, _fake = _client("duckdb", tmp_path, monkeypatch, pg_engine)
    owner_token, _ = _owner()
    _create_app()

    r = client.patch(
        "/api/data-apps/ident-app",
        json={"data_identity": "viewer", "description": "should not land"},
        headers=_auth(owner_token),
    )
    assert r.status_code == 501, r.text
    assert r.json()["error"] == "requires_postgres_backend"
    assert r.json()["feature"] == "data_app.data_identity"
    # Identity is applied FIRST so a mixed body never half-applies.
    got = client.get("/api/data-apps/ident-app", headers=_auth(owner_token)).json()
    assert got["data_identity"] == "owner"
    assert got["effective_description"] == ""

    # The description-only PATCH is untouched by the ratchet.
    r = client.patch("/api/data-apps/ident-app", json={"description": "fine"}, headers=_auth(owner_token))
    assert r.status_code == 200 and r.json()["effective_description"] == "fine"
    assert "redeploy" not in r.json()

    # And an empty body is a 400, not a silent description wipe.
    r = client.patch("/api/data-apps/ident-app", json={}, headers=_auth(owner_token))
    assert r.status_code == 400 and r.json()["detail"] == "nothing_to_update"
    assert client.get("/api/data-apps/ident-app", headers=_auth(owner_token)).json()["effective_description"] == "fine"


def test_contested_op_lease_refuses_before_writing_anything(tmp_path, monkeypatch, pg_engine):
    """A deploy/stop/wake in flight holds the per-slug op lease. The flip
    must then answer 409 with the column UNCHANGED and no
    `data_app.data_identity_changed` row — the lease is taken before the
    write, not after it (review finding on this PR)."""
    from app.api.data_apps import require_op_lease

    client, _admin, fake = _client("pg", tmp_path, monkeypatch, pg_engine)
    owner_token, _ = _owner()
    _create_app(state="running")

    holder = require_op_lease("ident-app")  # somebody else's deploy
    try:
        r = client.patch("/api/data-apps/ident-app", json={"data_identity": "viewer"}, headers=_auth(owner_token))
    finally:
        from app.api.data_apps import release_op_lease

        release_op_lease("ident-app", holder)
    assert r.status_code == 409, r.text
    assert client.get("/api/data-apps/ident-app", headers=_auth(owner_token)).json()["data_identity"] == "owner"
    assert fake.up_calls == []
    assert _audit_actions() == []
