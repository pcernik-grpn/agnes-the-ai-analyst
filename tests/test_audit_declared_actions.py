"""Declarative action emission (Wave 2 — audit-coverage plan, Task 1).

``src/audit_posture.py`` no longer has a `"fallback"` vocabulary value: every
mutating route's posture entry IS its declared action, and
``AuditFallbackMiddleware`` emits that action (never a generic
``http.request`` placeholder) when the handler wrote no row of its own.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from src.audit_events import is_cataloged
from src.audit_posture import POSTURE, declared_action

REPO_ROOT = Path(__file__).resolve().parents[1]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_posture_has_no_fallback_values_left():
    leftovers = sorted(k for k, v in POSTURE.items() if v == "fallback")
    assert not leftovers, (
        f"Every mutating route must declare a REAL action; 'fallback' is no longer a legal posture value: {leftovers}"
    )


def test_every_declared_action_is_cataloged():
    bad = sorted(f"{k} -> {v}" for k, v in POSTURE.items() if not v.startswith("exempt:") and not is_cataloged(v))
    assert not bad, f"Posture names an uncataloged action: {bad}"


def test_declared_action_resolves_and_skips_exempt():
    key = next(k for k, v in POSTURE.items() if not v.startswith("exempt:"))
    method, template = key.split(" ", 1)
    assert declared_action(method, template) == POSTURE[key]

    ex = next((k for k, v in POSTURE.items() if v.startswith("exempt:")), None)
    assert ex is not None, "sanity: at least one exempt route should exist"
    m, t = ex.split(" ", 1)
    assert declared_action(m, t) is None

    assert declared_action("POST", "/no/such/route") is None


def test_unhandled_mutation_emits_its_declared_action(tmp_path, monkeypatch, seeded_app):
    """POST /api/stack/subscribe writes no audit row of its own (only
    usage_events telemetry) — the middleware must emit ITS declared action
    (`stack.subscribe`), not a generic `http.request` row."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    client = seeded_app["client"]
    resp = client.post(
        "/api/stack/subscribe",
        json={"resource_type": "data_package", "resource_id": "posture-test-pkg"},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 200

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="stack.subscribe", limit=10)
    matches = [r for r in rows if r["resource"] == "/api/stack/subscribe"]
    assert matches, "middleware must emit the route's DECLARED action"
    assert matches[0]["user_id"] == "admin1"

    http_request_rows, _ = audit_repo().query(action="http.request", limit=10)
    assert not any(r["resource"] == "/api/stack/subscribe" for r in http_request_rows), (
        "no writer should emit the generic http.request action any more"
    )


def test_resource_carries_path_params(tmp_path, monkeypatch, seeded_app):
    """DELETE /api/stack/subscription/{resource_type}/{resource_id} records
    which subscription was targeted, via its resolved path params."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    client = seeded_app["client"]
    resp = client.delete(
        "/api/stack/subscription/data_package/posture-test-pkg",
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 204

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="stack.unsubscribe", limit=10)
    assert rows, "middleware must emit the route's DECLARED action"
    assert "resource_id=posture-test-pkg" in rows[0]["resource"]
    assert "resource_type=data_package" in rows[0]["resource"]


def test_handler_written_row_is_not_duplicated(tmp_path, monkeypatch, seeded_app):
    """POST /api/sync/trigger already writes its own `sync.trigger` row —
    the middleware must not ALSO write a row for it."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    client = seeded_app["client"]
    resp = client.post("/api/sync/trigger", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code in (200, 409)

    from src.repositories import audit_repo

    trigger_rows, _ = audit_repo().query(action="sync.trigger", limit=10)
    assert trigger_rows, "the route's own audit row is missing"
    assert len(trigger_rows) == 1, "exactly one row — no middleware duplicate"


def test_no_writer_emits_http_request_any_more():
    out = subprocess.run(
        ["grep", "-rIn", "--include=*.py", '"http.request"', str(REPO_ROOT / "app"), str(REPO_ROOT / "src")],
        capture_output=True,
        text=True,
    ).stdout
    offenders = [ln for ln in out.splitlines() if "audit_events.py" not in ln and "audit_fallback.py" not in ln]
    assert not offenders, offenders
