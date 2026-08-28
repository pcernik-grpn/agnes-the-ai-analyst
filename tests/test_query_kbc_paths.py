"""#1492 — direct ``kbc."bucket"."table"`` paths are registry/grant/policy gated.

Every Keboola sync writes a ``_remote_attach`` row aliased ``kbc``, which the
orchestrator re-ATTACHes onto the read-only analytics connection ``/api/query``
executes against. The ATTACH pairs the *instance* storage token with the
connection, so an ungated ``kbc.*`` read reaches whatever that token can see —
typically wider than any single analyst's grants. These tests pin the same
three-layer gate ``bq``/``sf``/``dbx`` already have (#1486):

- unregistered path → ``kbc_path_not_registered`` (admins included),
- registered but ungranted → ``kbc_path_access_denied`` (non-admins),
- path naming the physical source of a policied row → ``kbc_path_policied``,
- and the non-vacuity proof: a registered, granted, unpolicied path passes.
"""

from unittest.mock import MagicMock

import pytest


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _patch_guardrail(monkeypatch, rows, admin=False):
    repo = MagicMock()
    repo.list_by_source.return_value = rows
    monkeypatch.setattr("src.repositories.table_registry_repo", lambda: repo)
    monkeypatch.setattr("app.api.query._caller_is_unrestricted_admin", lambda *a, **kw: admin)


def test_kbc_guardrail_registered_and_accessible(monkeypatch):
    """Non-vacuity: a registered, granted, unpolicied path returns None —
    otherwise the gate reads as working while having simply broken the prefix."""
    from app.api.query import _kbc_guardrail_inputs

    _patch_guardrail(
        monkeypatch,
        [{"id": "t1", "bucket": "in.c-main", "source_table": "orders", "name": "orders"}],
    )
    result = _kbc_guardrail_inputs(
        'SELECT * FROM kbc."in.c-main"."orders"',
        'select * from kbc."in.c-main"."orders"',
        None,
        {},
        ["t1"],
    )
    assert result is None


def test_kbc_guardrail_unregistered_path(monkeypatch):
    from app.api.query import _kbc_guardrail_inputs

    _patch_guardrail(monkeypatch, [])
    result = _kbc_guardrail_inputs(
        'SELECT * FROM kbc."in.c-main"."missing"',
        'select * from kbc."in.c-main"."missing"',
        None,
        {},
        [],
    )
    assert result is not None
    assert result["reason"] == "kbc_path_not_registered"
    assert "hint" in result


def test_kbc_guardrail_unregistered_refuses_admin_too(monkeypatch):
    """Registration is required for every caller — mirrors bq/sf/dbx, where
    only the grant/policy layers carry the admin bypass."""
    from app.api.query import _kbc_guardrail_inputs

    _patch_guardrail(monkeypatch, [], admin=True)
    result = _kbc_guardrail_inputs(
        'SELECT * FROM kbc."in.c-main"."missing"',
        'select * from kbc."in.c-main"."missing"',
        None,
        {},
        None,
    )
    assert result is not None
    assert result["reason"] == "kbc_path_not_registered"


def test_kbc_guardrail_access_denied(monkeypatch):
    from app.api.query import _kbc_guardrail_inputs

    _patch_guardrail(
        monkeypatch,
        [{"id": "t1", "bucket": "in.c-main", "source_table": "orders", "name": "orders"}],
    )
    result = _kbc_guardrail_inputs(
        'SELECT * FROM kbc."in.c-main"."orders"',
        'select * from kbc."in.c-main"."orders"',
        None,
        {},
        [],
    )
    assert result is not None
    assert result["reason"] == "kbc_path_access_denied"
    assert result["registered_as"] == "orders"


def test_kbc_guardrail_admin_skips_grant_check(monkeypatch):
    from app.api.query import _kbc_guardrail_inputs

    _patch_guardrail(
        monkeypatch,
        [{"id": "t1", "bucket": "in.c-main", "source_table": "orders", "name": "orders"}],
        admin=True,
    )
    result = _kbc_guardrail_inputs(
        'SELECT * FROM kbc."in.c-main"."orders"',
        'select * from kbc."in.c-main"."orders"',
        None,
        {},
        None,
    )
    assert result is None


def test_kbc_guardrail_policied_physical_source(monkeypatch):
    """A granted row whose physical source carries an access policy is refused —
    the direct path would bypass the policy substitution ``rewrite_sql`` keys on
    the registry name."""
    from app.api.query import _kbc_guardrail_inputs

    _patch_guardrail(
        monkeypatch,
        [
            {
                "id": "t1",
                "bucket": "in.c-main",
                "source_table": "orders",
                "name": "orders",
                "access_policy_sql": "SELECT * FROM orders WHERE region = $user_email",
            }
        ],
    )
    result = _kbc_guardrail_inputs(
        'SELECT * FROM kbc."in.c-main"."orders"',
        'select * from kbc."in.c-main"."orders"',
        None,
        {},
        ["t1"],
    )
    assert result is not None
    assert result["reason"] == "kbc_path_policied"
    assert result["registered_as"] == "orders"
    assert "hint" in result


def test_kbc_guardrail_matches_full_id_source_table(monkeypatch):
    """Rows registered by the pre-fix wizard store the FULL Keboola table id
    (``<bucket>.<table>``) in ``source_table``; the live extension exposes the
    bare name, so the gate must normalize before matching (#1189 heritage)."""
    from app.api.query import _kbc_guardrail_inputs

    _patch_guardrail(
        monkeypatch,
        [
            {
                "id": "t1",
                "bucket": "in.c-main",
                "source_table": "in.c-main.orders",
                "name": "orders",
            }
        ],
    )
    result = _kbc_guardrail_inputs(
        'SELECT * FROM kbc."in.c-main"."orders"',
        'select * from kbc."in.c-main"."orders"',
        None,
        {},
        ["t1"],
    )
    assert result is None


def test_kbc_guardrail_no_kbc_path_is_inert(monkeypatch):
    from app.api.query import _kbc_guardrail_inputs

    repo = MagicMock()
    monkeypatch.setattr("src.repositories.table_registry_repo", lambda: repo)
    result = _kbc_guardrail_inputs(
        "SELECT * FROM orders",
        "select * from orders",
        None,
        {},
        [],
    )
    assert result is None
    repo.list_by_source.assert_not_called()


def test_query_endpoint_refuses_unregistered_kbc_path(seeded_app):
    """Call-site wiring: a non-admin's kbc path 403s at /api/query with the
    structured detail, before anything reaches the ATTACHed extension."""
    c = seeded_app["client"]
    resp = c.post(
        "/api/query",
        json={"sql": 'SELECT * FROM kbc."in.c-main"."orders"'},
        headers=_auth(seeded_app["analyst_token"]),
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["reason"] == "kbc_path_not_registered"


def test_internal_refs_cannot_combine_with_kbc_path(seeded_app):
    """The internal-source short-circuit refuses mixed internal + kbc.* SQL the
    same way it refuses bq.*/sf.* — the two live in different DuckDB instances."""
    c = seeded_app["client"]
    resp = c.post(
        "/api/query",
        json={"sql": 'SELECT * FROM agnes_telemetry t JOIN kbc."in.c-main"."orders" o ON 1=1'},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 400
    assert "can't be combined" in resp.json()["detail"]


def test_snapshot_from_query_refuses_unregistered_kbc_path(seeded_app):
    """``run_remote_select_to_arrow`` (``/api/v2/scan --from-query``) executes on
    the same read-only analytics connection whose ``kbc`` catalog gets
    re-ATTACHed, so it needs the same gate — mirrors the sf sibling."""
    from fastapi import HTTPException

    from app.api.query import run_remote_select_to_arrow
    from src.db import get_system_db

    conn = get_system_db()
    try:
        with pytest.raises(HTTPException) as exc:
            run_remote_select_to_arrow(
                conn,
                {"id": "admin1", "email": "admin@test.com"},
                'SELECT * FROM kbc."in.c-main"."unregistered"',
                None,
                None,
            )
    finally:
        conn.close()
    assert exc.value.status_code == 403
    assert exc.value.detail["reason"] == "kbc_path_not_registered"
