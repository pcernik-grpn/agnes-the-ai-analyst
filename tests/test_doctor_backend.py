"""New-instance doctor: app-state-backend check (A1).

Fresh installs run app-state on Postgres (``side_car``/``cloud``) since A1;
DuckDB is legacy-only for NEW deploys. The check grades a DuckDB verdict
rather than flat-erroring it: an instance that predates the Postgres default
(persisted overlay state, or prior-service evidence in the users table) is a
supported legacy state and reports ``warning`` — critical because
``scripts/ops/post-deploy-smoke-test.sh`` maps any ``error`` row to a FAIL of
the whole post-deploy gate, so a flat error would fail every existing DuckDB
instance's upgrade. ``error`` is reserved for a day-zero fresh install that
came up on DuckDB despite both install paths defaulting to Postgres.

The "Postgres active" branch is tested by calling ``check_app_state_backend()``
directly rather than through the full ``/api/admin/doctor/new-instance``
endpoint: ``use_pg()`` gates every repository factory in the app, so
monkeypatching it True for a whole request would also flip login-door,
chat-grant, and agent-scope onto a Postgres engine that does not exist in
this DuckDB-backed test process, crashing checks unrelated to this one.
"""

import pytest

import app.services.instance_doctor as doctor
import src.repositories as repositories_module
from app.auth.scheduler_token import SCHEDULER_USER_EMAIL


@pytest.fixture(autouse=True)
def _fresh_state_cache():
    """The overlay parse is memoized per-process; reset around every test so
    an overlay written (or pointed away) here is actually observed, and a
    cached verdict never bleeds into later tests."""
    import src.db_state_machine as sm

    sm.reset_backend_state_cache()
    yield
    sm.reset_backend_state_cache()


class _FakeUsers:
    def __init__(self, rows):
        self._rows = rows

    def list_all(self):
        return self._rows


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _run(client, token):
    resp = client.post("/api/admin/doctor/new-instance", headers=_auth(token), json={})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _check(report, name):
    matches = [c for c in report["checks"] if c["name"] == name]
    assert matches, f"check {name!r} missing from report: {[c['name'] for c in report['checks']]}"
    return matches[0]


def _no_pg_env(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AGNES_DB_URL", raising=False)


def test_check_passes_when_use_pg_true(monkeypatch):
    monkeypatch.setattr(repositories_module, "use_pg", lambda: True)
    check = doctor.check_app_state_backend()
    assert check["status"] == "ok"


def test_check_passes_via_database_url_env_without_instance_yaml(monkeypatch, tmp_path):
    """Self-hosted managed-Postgres deployments (per docker-compose.postgres.yml's
    header) and the local/CI docker-compose.postgres.yml overlay both activate
    Postgres purely via the DATABASE_URL env var, WITHOUT ever writing
    instance.yaml — the exact scenario that makes a raw
    get_database_config()-only read insufficient (it would misreport these as
    legacy DuckDB, since instance.yaml::database.backend is never set). Exercises
    the real use_pg() precedence chain, not a mock of it."""
    import src.db_state_machine as sm

    monkeypatch.setattr(sm, "_OVERLAY_PATH", tmp_path / "missing-instance.yaml")
    sm.reset_backend_state_cache()
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/agnes")

    check = doctor.check_app_state_backend()
    assert check["status"] == "ok"


def test_check_warns_for_persisted_duckdb_overlay(monkeypatch, tmp_path):
    """A persisted ``backend: duckdb`` is exactly what the pre-A1 startup
    script seeded (and what a deliberate PG → DuckDB migration writes) — a
    supported legacy state, so the verdict is warning, never error: the
    post-deploy gate maps error to FAIL and every existing DuckDB instance
    runs that gate on upgrade."""
    import src.db_state_machine as sm

    overlay = tmp_path / "instance.yaml"
    overlay.write_text("database:\n  backend: duckdb\n")
    monkeypatch.setattr(sm, "_OVERLAY_PATH", overlay)
    sm.reset_backend_state_cache()
    _no_pg_env(monkeypatch)

    check = doctor.check_app_state_backend()
    assert check["status"] == "warning"
    assert "legacy" in check["detail"].lower()


def test_check_warns_when_users_exist_without_persisted_state(monkeypatch, tmp_path):
    """No overlay at all (pre-state-machine instance) but the users table is
    already populated: the instance was in service before A1's default —
    legacy, warning."""
    import src.db_state_machine as sm

    monkeypatch.setattr(sm, "_OVERLAY_PATH", tmp_path / "missing-instance.yaml")
    sm.reset_backend_state_cache()
    _no_pg_env(monkeypatch)
    monkeypatch.setattr(
        repositories_module,
        "users_repo",
        lambda: _FakeUsers([{"email": "admin@example.com"}, {"email": SCHEDULER_USER_EMAIL}]),
    )

    check = doctor.check_app_state_backend()
    assert check["status"] == "warning"


def test_check_errors_for_day_zero_fresh_install(monkeypatch, tmp_path):
    """The one state A1's contract genuinely forbids: no persisted database
    state, no non-system users (the synthetic scheduler user is auto-seeded
    at boot and does not count as prior service) — a day-zero fresh install
    that should have come up on Postgres."""
    import src.db_state_machine as sm

    monkeypatch.setattr(sm, "_OVERLAY_PATH", tmp_path / "missing-instance.yaml")
    sm.reset_backend_state_cache()
    _no_pg_env(monkeypatch)
    monkeypatch.setattr(
        repositories_module,
        "users_repo",
        lambda: _FakeUsers([{"email": SCHEDULER_USER_EMAIL}]),
    )

    check = doctor.check_app_state_backend()
    assert check["status"] == "error"
    assert "fresh install" in check["detail"].lower()


def test_doctor_reports_legacy_duckdb_warning_through_endpoint(seeded_app):
    """Default test environment: DuckDB backend, seeded (non-system) users —
    the shape of every existing legacy instance running the post-deploy
    gate. The verdict must be warning: post-deploy-smoke-test.sh maps error
    to FAIL, and an upgrade must not fail the fleet."""
    report = _run(seeded_app["client"], seeded_app["admin_token"])
    check = _check(report, "app-state-backend")
    assert check["status"] == "warning"


def test_doctor_wires_the_check_into_the_report(seeded_app, monkeypatch):
    """The endpoint surfaces whatever check_app_state_backend() returns —
    pins the wiring (name, run_new_instance_doctor's sync_checks list)
    independently of the check's own pass/fail logic above."""
    monkeypatch.setattr(
        doctor,
        "check_app_state_backend",
        lambda: {"name": "app-state-backend", "status": "ok", "audience": "operator", "detail": "stubbed"},
    )
    report = _run(seeded_app["client"], seeded_app["admin_token"])
    check = _check(report, "app-state-backend")
    assert check["status"] == "ok"
    assert check["detail"] == "stubbed"
