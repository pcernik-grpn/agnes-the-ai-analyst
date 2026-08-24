"""New-instance doctor: app-state-backend check (A1).

Fresh installs run app-state on Postgres (``side_car``/``cloud``) since A1;
DuckDB is legacy-only for NEW deploys. This doctor is explicitly a
new-instance gate — an existing instance still on DuckDB failing this check
is correct and informative, not a bug.

The "Postgres active" branch is tested by calling ``check_app_state_backend()``
directly rather than through the full ``/api/admin/doctor/new-instance``
endpoint: ``use_pg()`` gates every repository factory in the app, so
monkeypatching it True for a whole request would also flip login-door,
chat-grant, and agent-scope onto a Postgres engine that does not exist in
this DuckDB-backed test process, crashing checks unrelated to this one.
"""

import app.services.instance_doctor as doctor
import src.repositories as repositories_module


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


def test_check_flags_duckdb_backend(monkeypatch):
    monkeypatch.setattr(repositories_module, "use_pg", lambda: False)
    check = doctor.check_app_state_backend()
    assert check["status"] == "error"
    assert "legacy" in check["detail"].lower()


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


def test_doctor_flags_duckdb_backend_through_endpoint(seeded_app):
    """Default test environment: no instance.yaml, no DATABASE_URL — the
    doctor's own safe zero-config default (Task 2) — so the check reports
    the legacy-DuckDB error without any mocking."""
    report = _run(seeded_app["client"], seeded_app["admin_token"])
    check = _check(report, "app-state-backend")
    assert check["status"] == "error"


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
