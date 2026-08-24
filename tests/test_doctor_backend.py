"""New-instance doctor: app-state-backend check (A1).

Fresh installs run app-state on Postgres (``side_car``/``cloud``) since A1;
DuckDB is legacy-only for NEW deploys. This doctor is explicitly a
new-instance gate — an existing instance still on DuckDB failing this check
is correct and informative, not a bug.
"""

import app.instance_config as instance_config


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


def test_doctor_flags_duckdb_backend(seeded_app, monkeypatch):
    monkeypatch.setattr(instance_config, "get_database_config", lambda: {"backend": "duckdb", "url": None})
    report = _run(seeded_app["client"], seeded_app["admin_token"])
    check = _check(report, "app-state-backend")
    assert check["status"] == "error"
    assert "legacy" in check["detail"].lower()


def test_doctor_passes_side_car_backend(seeded_app, monkeypatch):
    monkeypatch.setattr(
        instance_config,
        "get_database_config",
        lambda: {"backend": "side_car", "url": "postgresql://x"},
    )
    report = _run(seeded_app["client"], seeded_app["admin_token"])
    check = _check(report, "app-state-backend")
    assert check["status"] == "ok"


def test_doctor_passes_cloud_backend(seeded_app, monkeypatch):
    monkeypatch.setattr(
        instance_config,
        "get_database_config",
        lambda: {"backend": "cloud", "url": "postgresql://x"},
    )
    report = _run(seeded_app["client"], seeded_app["admin_token"])
    check = _check(report, "app-state-backend")
    assert check["status"] == "ok"
