"""Access-policy revision history — the Postgres happy path (#1979 K1-sweep
finding 1).

The DuckDB half of this route (admin gating, 404-before-repo, the typed
``501 requires_postgres_backend``, and "a save still works with no revision
store") is ``tests/test_admin_access_policy_revisions_api.py``. This file is
the other side of that fork: with the PG backend actually present, every
policy write through ``PUT /api/admin/registry/{id}`` must leave a revision
the modal can list and restore from.

Restore is deliberately NOT an endpoint. The client re-submits the
revision's body through the SAME validated PUT, so a restore pays for every
interlock a fresh save pays for — the §3.1 undistributed check, the
mandatory note, the live probe. The last test here is that claim's proof: a
revision saved while the table was eligible is REFUSED once the table has
become distributable.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _pg_client(tmp_path, monkeypatch, pg_engine):
    monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
    from tests.db_pg._parity_sweep_util import build_seeded_client

    return build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)


def _register(c, token, **kwargs) -> str:
    kwargs.setdefault("source_type", "keboola")
    kwargs.setdefault("query_mode", "local")
    r = c.post("/api/admin/register-table", json=kwargs, headers=_auth(token))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _put(c, token, table_id, body):
    return c.put(f"/api/admin/registry/{table_id}", json=body, headers=_auth(token))


def _revisions(c, token, table_id, **params):
    r = c.get(f"/api/admin/registry/{table_id}/policy/revisions", params=params, headers=_auth(token))
    assert r.status_code == 200, r.text
    return r.json()


def test_attaching_a_policy_records_a_revision(tmp_path, monkeypatch, pg_engine):
    c, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    table_id = _register(c, token, name="rev_attach", server_only=True)

    assert (
        _put(
            c,
            token,
            table_id,
            {"access_policy_sql": f"SELECT * FROM {table_id}", "access_policy_note": "first policy"},
        ).status_code
        == 200
    )

    body = _revisions(c, token, table_id)
    assert body["table_id"] == table_id
    assert body["count"] == 1
    rev = body["revisions"][0]
    assert rev["policy_sql"] == f"SELECT * FROM {table_id}"
    assert rev["policy_note"] == "first policy"
    assert rev["cleared"] is False
    assert rev["saved_by"]
    assert rev["saved_at"]
    assert rev["id"].startswith("apr_")


def test_revisions_list_is_newest_first(tmp_path, monkeypatch, pg_engine):
    c, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    table_id = _register(c, token, name="rev_order", server_only=True)

    for i in range(3):
        assert (
            _put(
                c,
                token,
                table_id,
                {"access_policy_sql": f"SELECT * FROM {table_id}", "access_policy_note": f"note {i}"},
            ).status_code
            == 200
        )

    notes = [r["policy_note"] for r in _revisions(c, token, table_id)["revisions"]]
    assert notes == ["note 2", "note 1", "note 0"]


def test_clearing_a_policy_records_a_cleared_revision(tmp_path, monkeypatch, pg_engine):
    """Removing protection is the single most important row in the history
    — it must be a revision, not a gap the reader has to infer."""
    c, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    table_id = _register(c, token, name="rev_clear", server_only=True)

    _put(c, token, table_id, {"access_policy_sql": f"SELECT * FROM {table_id}", "access_policy_note": "why"})
    assert _put(c, token, table_id, {"access_policy_sql": None}).status_code == 200

    revisions = _revisions(c, token, table_id)["revisions"]
    assert revisions[0]["cleared"] is True
    assert revisions[0]["policy_sql"] is None
    # ...and the policy it replaced is still there to restore FROM, which is
    # the whole point of recording the clear rather than deleting the row.
    assert revisions[1]["cleared"] is False
    assert revisions[1]["policy_sql"] == f"SELECT * FROM {table_id}"


def test_an_unrelated_edit_records_no_revision(tmp_path, monkeypatch, pg_engine):
    """A PUT that never touches the policy fields must not manufacture a
    revision — "recent policy edits" would otherwise fill up with edits that
    changed no policy."""
    c, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    table_id = _register(c, token, name="rev_unrelated", server_only=True)

    _put(c, token, table_id, {"access_policy_sql": f"SELECT * FROM {table_id}", "access_policy_note": "why"})
    assert _put(c, token, table_id, {"description": "unrelated edit"}).status_code == 200

    assert _revisions(c, token, table_id)["count"] == 1


def test_a_mapping_only_edit_records_a_revision(tmp_path, monkeypatch, pg_engine):
    """finding 2 (follow-up review of PR #2023): flipping only the
    "referenceable from other policies" switch is a policy edit — the
    revision carries ``policy_mapping`` and the panel's diff names a mapping
    toggle, so an unrecorded flip leaves the panel diffing against a state
    nothing ever recorded. A no-op resend still records nothing."""
    c, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    table_id = _register(c, token, name="rev_mapping", server_only=True)

    _put(c, token, table_id, {"access_policy_sql": f"SELECT * FROM {table_id}", "access_policy_note": "why"})
    assert _revisions(c, token, table_id)["count"] == 1

    assert _put(c, token, table_id, {"policy_mapping": True}).status_code == 200
    body = _revisions(c, token, table_id)
    assert body["count"] == 2
    latest = body["revisions"][0]
    assert latest["policy_mapping"] is True
    # The body is unchanged — the revision is the CURRENT policy under its
    # new mapping flag, never an empty one.
    assert latest["policy_sql"] == f"SELECT * FROM {table_id}"
    assert latest["policy_note"] == "why"

    # The Edit modal round-trips every field: resending the same value must
    # not manufacture a revision that changed nothing.
    assert _put(c, token, table_id, {"policy_mapping": True}).status_code == 200
    assert _revisions(c, token, table_id)["count"] == 2


def test_a_policy_stored_before_this_feature_is_backfilled_as_a_baseline(tmp_path, monkeypatch, pg_engine):
    """A table already carrying a policy has no revision for it. The first
    write after that must record the state it is REPLACING first, or the
    body it overwrites is gone forever — exactly the loss this feature
    exists to prevent."""
    c, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    table_id = _register(c, token, name="rev_baseline", server_only=True)

    # Straight through the repo, the way a policy attached before this
    # feature shipped got there: no revision is written.
    from src.repositories import table_registry_repo

    table_registry_repo().set_access_policy(
        table_id,
        sql=f"SELECT * FROM {table_id}",
        note="pre-existing policy",
        updated_by="old-admin@example.com",
    )
    assert _revisions(c, token, table_id)["count"] == 0

    assert (
        _put(
            c,
            token,
            table_id,
            {"access_policy_sql": f"SELECT * FROM {table_id} WHERE 1=1", "access_policy_note": "narrowed"},
        ).status_code
        == 200
    )

    revisions = _revisions(c, token, table_id)["revisions"]
    assert len(revisions) == 2
    assert revisions[0]["policy_note"] == "narrowed"
    baseline = revisions[1]
    assert baseline["policy_note"] == "pre-existing policy"
    assert baseline["policy_sql"] == f"SELECT * FROM {table_id}"
    # The baseline is attributed to whoever last wrote it, not to the admin
    # whose edit happened to trigger the backfill.
    assert baseline["saved_by"] == "old-admin@example.com"


def test_the_baseline_is_backfilled_only_once(tmp_path, monkeypatch, pg_engine):
    c, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    table_id = _register(c, token, name="rev_baseline_once", server_only=True)

    from src.repositories import table_registry_repo

    table_registry_repo().set_access_policy(
        table_id, sql=f"SELECT * FROM {table_id}", note="pre-existing", updated_by="old@example.com"
    )
    for i in range(3):
        _put(
            c,
            token,
            table_id,
            {"access_policy_sql": f"SELECT * FROM {table_id}", "access_policy_note": f"edit {i}"},
        )

    notes = [r["policy_note"] for r in _revisions(c, token, table_id)["revisions"]]
    assert notes == ["edit 2", "edit 1", "edit 0", "pre-existing"]


def test_limit_truncates_the_list_but_count_stays_total(tmp_path, monkeypatch, pg_engine):
    """A truncated panel must be able to say "3 of 5" — a silent prefix
    reads as a complete history."""
    c, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    table_id = _register(c, token, name="rev_limit", server_only=True)

    for i in range(5):
        _put(
            c,
            token,
            table_id,
            {"access_policy_sql": f"SELECT * FROM {table_id}", "access_policy_note": f"n{i}"},
        )

    body = _revisions(c, token, table_id, limit=3)
    assert len(body["revisions"]) == 3
    assert body["count"] == 5


def test_restoring_a_revision_goes_through_the_validated_put(tmp_path, monkeypatch, pg_engine):
    """The restore flow: the client re-submits a listed revision's body
    through the ordinary PUT. It lands like any other save — and records a
    revision of its own, so a restore is itself restorable."""
    c, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    table_id = _register(c, token, name="rev_restore", server_only=True)

    _put(c, token, table_id, {"access_policy_sql": f"SELECT * FROM {table_id}", "access_policy_note": "original"})
    _put(
        c,
        token,
        table_id,
        {"access_policy_sql": f"SELECT * FROM {table_id} WHERE 1=0", "access_policy_note": "too strict"},
    )

    original = _revisions(c, token, table_id)["revisions"][-1]
    assert (
        _put(
            c,
            token,
            table_id,
            {"access_policy_sql": original["policy_sql"], "access_policy_note": original["policy_note"]},
        ).status_code
        == 200
    )

    from src.repositories import table_registry_repo

    stored = table_registry_repo().get(table_id)
    assert stored["access_policy_sql"] == original["policy_sql"]
    assert stored["access_policy_note"] == "original"
    assert _revisions(c, token, table_id)["count"] == 3


def test_restoring_is_revalidated_and_can_be_refused(tmp_path, monkeypatch, pg_engine):
    """The reason restore is not its own endpoint. A revision saved while
    the table was undistributed must NOT reattach itself once the table has
    become distributable (§3.1) — going through the PUT means the interlock
    judges the restore exactly as it judges a fresh save."""
    c, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    table_id = _register(c, token, name="rev_revalidated", server_only=True)

    _put(c, token, table_id, {"access_policy_sql": f"SELECT * FROM {table_id}", "access_policy_note": "scoped"})
    revision = _revisions(c, token, table_id)["revisions"][0]

    # Clear the policy, then make the table distributable again.
    assert _put(c, token, table_id, {"access_policy_sql": None}).status_code == 200
    assert _put(c, token, table_id, {"server_only": False}).status_code == 200

    refused = _put(
        c,
        token,
        table_id,
        {"access_policy_sql": revision["policy_sql"], "access_policy_note": revision["policy_note"]},
    )
    assert refused.status_code == 422, refused.text
    assert "access_policy_requires_undistributed" in refused.text


def test_a_restore_missing_its_note_is_refused_like_any_other_save(tmp_path, monkeypatch, pg_engine):
    c, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    table_id = _register(c, token, name="rev_note_required", server_only=True)

    _put(c, token, table_id, {"access_policy_sql": f"SELECT * FROM {table_id}", "access_policy_note": "scoped"})
    revision = _revisions(c, token, table_id)["revisions"][0]
    _put(c, token, table_id, {"access_policy_sql": None})

    refused = _put(c, token, table_id, {"access_policy_sql": revision["policy_sql"], "access_policy_note": "  "})
    assert refused.status_code == 422, refused.text
    assert "policy_note_required" in refused.text


def test_revisions_read_writes_an_audit_row_without_the_sql_bodies(tmp_path, monkeypatch, pg_engine):
    """RBAC-reviewer finding on #1979: each listed revision carries the full
    historical ``policy_sql`` body -- the same content ``.../policy/preview``
    and ``.../preview-groups`` are already audited for. This route must leave
    a real audit row (``access_policy.revisions_view``, not
    ``exempt:ui_support``), and that row's params must carry metadata only,
    never the SQL bodies or notes themselves."""
    c, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    table_id = _register(c, token, name="rev_audit", server_only=True)
    sentinel_sql = f"SELECT id /* SENTINEL_REV_AUDIT_1979 */ FROM {table_id}"

    assert (
        _put(
            c,
            token,
            table_id,
            {"access_policy_sql": sentinel_sql, "access_policy_note": "sensitive rationale"},
        ).status_code
        == 200
    )

    body = _revisions(c, token, table_id, limit=5)
    assert body["count"] == 1

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="access_policy.revisions_view", resource=table_id)
    rows = list(rows)
    assert rows, "the revisions read left no audit trail"

    import json as _json

    raw_params = rows[0]["params"]
    params = _json.loads(raw_params) if isinstance(raw_params, str) else raw_params
    assert params["table_id"] == table_id
    assert params["count"] == 1
    assert params["limit"] == 5
    assert set(params.keys()) == {"table_id", "count", "limit"}

    dumped = raw_params if isinstance(raw_params, str) else _json.dumps(params)
    assert sentinel_sql not in dumped
    assert "sensitive rationale" not in dumped


def test_unregistering_a_table_drops_its_revisions(tmp_path, monkeypatch, pg_engine):
    """Table ids are derived from names, so re-registering the same name
    yields the same id — a new table must not inherit (and be able to
    restore) the policy bodies of the one that used to live there."""
    c, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    table_id = _register(c, token, name="rev_purge", server_only=True)

    _put(c, token, table_id, {"access_policy_sql": f"SELECT * FROM {table_id}", "access_policy_note": "secret shape"})
    assert _revisions(c, token, table_id)["count"] == 1

    assert c.delete(f"/api/admin/registry/{table_id}", headers=_auth(token)).status_code == 204

    reborn = _register(c, token, name="rev_purge", server_only=True)
    assert reborn == table_id
    assert _revisions(c, token, reborn)["count"] == 0


def test_registering_a_table_purges_any_orphaned_revisions_at_its_reused_id(tmp_path, monkeypatch, pg_engine):
    """finding 3 (PR #2023 review): defense in depth. Even if a revision
    somehow survived under a table id that is no longer registered (the
    unregister-time purge above is the primary defense; this is the
    second one), `register_table` must not let a brand-new table at that
    reused id offer the leftover revision as its own restorable history."""
    c, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    table_id = _register(c, token, name="rev_defense_in_depth", server_only=True)
    _put(
        c,
        token,
        table_id,
        {"access_policy_sql": f"SELECT * FROM {table_id}", "access_policy_note": "original owner"},
    )
    assert c.delete(f"/api/admin/registry/{table_id}", headers=_auth(token)).status_code == 204

    # Simulate a revision that leaked past the unregister-time purge (a
    # write racing the DELETE, a bug in a future refactor, debris from
    # before this fix shipped, ...) -- straight through the repo, bypassing
    # the HTTP layer entirely so this test is independent of *how* the
    # orphan got there.
    from src.repositories import access_policy_revisions_repo

    access_policy_revisions_repo().record(
        table_id=table_id,
        policy_sql=f"SELECT * FROM {table_id} WHERE leaked = true",
        policy_note="orphaned revision",
        saved_by="old-admin@example.com",
    )

    reborn = _register(c, token, name="rev_defense_in_depth", server_only=True)
    assert reborn == table_id
    assert _revisions(c, token, reborn)["count"] == 0
