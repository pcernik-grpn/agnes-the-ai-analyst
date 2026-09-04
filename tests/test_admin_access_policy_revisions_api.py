"""Access-policy revision history — the route contract on the DEFAULT
(DuckDB) test backend (#1979 K1-sweep finding 1).

``access_policy_revisions`` is a post-A3 PG-only table, so this file owns
the half of the contract a DuckDB instance can prove:

* admin gating and the 404-before-any-repo-work,
* the typed ``501 requires_postgres_backend`` the list route owes,
* and — the property that actually matters — that a policy SAVE still
  succeeds on an instance with nowhere to record its revision. Recording
  history must never be able to block an admin from narrowing access to a
  table.

The Postgres happy path (revisions actually written on save/clear, listed in
order, and the restore round-trip) lives in
``tests/db_pg/test_access_policy_revisions_api_pg.py``.
"""

from __future__ import annotations

import contextlib

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(c, token, **kwargs) -> str:
    kwargs.setdefault("source_type", "keboola")
    kwargs.setdefault("query_mode", "local")
    resp = c.post("/api/admin/register-table", json=kwargs, headers=_auth(token))
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _url(table_id: str) -> str:
    return f"/api/admin/registry/{table_id}/policy/revisions"


class TestAuthGating:
    def test_requires_authentication(self, seeded_app):
        r = seeded_app["client"].get(_url("whatever"))
        assert r.status_code == 401

    def test_requires_admin(self, seeded_app):
        """A non-admin analyst must not read policy bodies — the SQL names
        the columns and identity predicates the policy exists to enforce,
        which is a map of what it is hiding."""
        r = seeded_app["client"].get(_url("whatever"), headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 403


class TestUnknownTable:
    def test_unknown_table_is_404_not_501(self, seeded_app):
        """The registry lookup runs BEFORE the PG-only repo, so a typo'd id
        is a 404 on every backend — a 501 would tell an admin to migrate
        their database over a misspelling."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        r = c.get(_url("does-not-exist"), headers=_auth(token))
        assert r.status_code == 404, r.text
        assert r.json()["detail"] == "Table not found"


class TestDuckDbDegradesCleanly:
    def test_list_is_typed_501(self, seeded_app):
        """A3 ratchet: the revision store is PG-only, so a DuckDB instance
        gets a TYPED 501 the modal can recognize (and fall back to the
        audit-derived history on) — never a raw 500, and never an
        empty-but-healthy-looking "no revisions" answer that would read as
        "this policy was never edited"."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        table_id = _register(c, token, name="rev_duckdb_tbl", server_only=True)

        r = c.get(_url(table_id), headers=_auth(token))
        assert r.status_code == 501, r.text
        body = r.json()
        assert body["error"] == "requires_postgres_backend"
        assert body["feature"] == "access_policy_revisions"

    @pytest.mark.journey
    def test_saving_a_policy_still_works_with_no_revision_store(self, seeded_app, monkeypatch):
        """The write hook is observability, never load-bearing: an admin
        must be able to attach (and clear) a policy on a DuckDB instance,
        where there is nowhere to record the revision."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c, token = seeded_app["client"], seeded_app["admin_token"]
        table_id = _register(c, token, name="rev_duckdb_save", server_only=True)

        r = c.put(
            f"/api/admin/registry/{table_id}",
            json={
                "access_policy_sql": f"SELECT * FROM {table_id}",
                "access_policy_note": "no revision store here",
            },
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text

        from src.repositories import table_registry_repo

        assert table_registry_repo().get(table_id)["access_policy_sql"] is not None

        cleared = c.put(
            f"/api/admin/registry/{table_id}",
            json={"access_policy_sql": None},
            headers=_auth(token),
        )
        assert cleared.status_code == 200, cleared.text
        assert table_registry_repo().get(table_id)["access_policy_sql"] is None

    def test_unregistering_a_table_still_works_with_no_revision_store(self, seeded_app):
        """The revision purge on DELETE degrades the same way the write
        does — a DuckDB instance must still be able to unregister a table."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        table_id = _register(c, token, name="rev_duckdb_delete")

        r = c.delete(f"/api/admin/registry/{table_id}", headers=_auth(token))
        assert r.status_code == 204, r.text

    def test_registering_a_table_still_works_with_no_revision_store_to_purge(self, seeded_app):
        """finding 3 (PR #2023 review): `register_table`'s defense-in-depth
        orphan purge degrades the same way the write hook and the
        unregister-time purge do — a DuckDB instance (no revision store at
        all) must still be able to register a brand-new table."""
        c, token = seeded_app["client"], seeded_app["admin_token"]

        r = c.post(
            "/api/admin/register-table",
            json={"name": "rev_duckdb_register", "source_type": "keboola", "query_mode": "local"},
            headers=_auth(token),
        )
        assert r.status_code == 201, r.text


class TestUnregisterPurgeFailure:
    def test_unregister_aborts_if_purge_fails_for_a_reason_other_than_missing_backend(self, seeded_app, monkeypatch):
        """finding 3 (PR #2023 review): a genuine failure while purging a
        table's access-policy revisions must abort the whole unregistration
        cleanly -- never silently drop the registry row and leave the (now
        unreachable-to-purge) old revisions sitting under an id a
        re-registration of the same name would reuse."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        table_id = _register(c, token, name="rev_purge_fails")

        class _BoomRepo:
            def delete_for_table(self, table_id):
                raise RuntimeError("boom")

        monkeypatch.setattr("app.api.admin.access_policy_revisions_repo", lambda: _BoomRepo())

        r = c.delete(f"/api/admin/registry/{table_id}", headers=_auth(token))
        assert r.status_code != 204, r.text
        assert r.status_code >= 400, r.text

        from src.repositories import table_registry_repo

        assert table_registry_repo().get(table_id) is not None, "registry row must survive a failed purge"


class TestRegisterPurgeOrdering:
    """finding A (follow-up review of PR #2023): the orphan purge in
    `register_table` must run BEFORE the registry insert -- symmetric with
    `unregister_table` -- so a genuine purge failure aborts the whole
    registration instead of silently committing a row over stale, still-
    reachable history."""

    def test_register_aborts_if_purge_fails_for_a_reason_other_than_missing_backend(self, seeded_app, monkeypatch):
        c, token = seeded_app["client"], seeded_app["admin_token"]

        class _BoomRepo:
            def delete_for_table(self, table_id):
                raise RuntimeError("boom")

        monkeypatch.setattr("app.api.admin.access_policy_revisions_repo", lambda: _BoomRepo())

        r = c.post(
            "/api/admin/register-table",
            json={"name": "rev_register_purge_fails", "source_type": "keboola", "query_mode": "local"},
            headers=_auth(token),
        )
        assert r.status_code != 201, r.text
        assert r.status_code >= 400, r.text
        assert r.json()["detail"]["reason"] == "access_policy_revision_purge_failed"

        from src.repositories import table_registry_repo

        assert table_registry_repo().get("rev_register_purge_fails") is None, (
            "registry row must not exist when the purge fails"
        )

    def test_register_succeeds_when_the_purge_raises_requires_postgres_backend(self, seeded_app):
        """The default DuckDB test backend has no revision store at all --
        `access_policy_revisions_repo()` itself raises `RequiresPostgresBackend`,
        which must be swallowed rather than block registration."""
        c, token = seeded_app["client"], seeded_app["admin_token"]

        r = c.post(
            "/api/admin/register-table",
            json={"name": "rev_register_purge_pg_only", "source_type": "keboola", "query_mode": "local"},
            headers=_auth(token),
        )
        assert r.status_code == 201, r.text


class _FakeRevisionStore:
    """A stand-in for the PG-only revision store, so the DuckDB-backed test
    app can prove the ROUTE's contract: what it records, and that it records
    it under the per-table write lock (#1979, review follow-up)."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.records: list[dict] = []
        self._counts: dict[str, int] = {}

    import contextlib as _contextlib

    @_contextlib.contextmanager
    def policy_write_lock(self, table_id: str):
        self.events.append(f"lock_enter:{table_id}")
        try:
            yield
        finally:
            self.events.append(f"lock_exit:{table_id}")

    def count_for_table(self, table_id: str) -> int:
        return self._counts.get(table_id, 0)

    def record(self, *, table_id, policy_sql, policy_note, policy_mapping=False, saved_by=None, saved_at=None):
        self.events.append(f"record:{table_id}")
        self.records.append(
            {
                "table_id": table_id,
                "policy_sql": policy_sql,
                "policy_note": policy_note,
                "policy_mapping": bool(policy_mapping),
                "saved_by": saved_by,
            }
        )
        self._counts[table_id] = self._counts.get(table_id, 0) + 1
        return "apr_fake"

    def delete_for_table(self, table_id: str) -> int:
        self.events.append(f"delete_for_table:{table_id}")
        return 0


@pytest.fixture
def fake_store(monkeypatch):
    store = _FakeRevisionStore()
    monkeypatch.setattr("app.api.admin.access_policy_revisions_repo", lambda: store)
    monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
    return store


class TestMappingOnlyEditsAreRecorded:
    """finding 2 (follow-up review of PR #2023): a revision carries
    ``policy_mapping``, and the history panel's diff calls a mapping toggle
    out by name — so a PUT that flips ONLY the "referenceable from other
    policies" switch has to leave a revision, or the panel diffs against a
    state nothing recorded."""

    def _policied_table(self, c, token, store, name):
        table_id = _register(c, token, name=name, server_only=True)
        r = c.put(
            f"/api/admin/registry/{table_id}",
            json={"access_policy_sql": f"SELECT * FROM {table_id}", "access_policy_note": "why"},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        assert len(store.records) == 1
        return table_id

    def test_flipping_only_policy_mapping_records_a_revision(self, seeded_app, fake_store):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        table_id = self._policied_table(c, token, fake_store, "rev_mapping_on")

        r = c.put(f"/api/admin/registry/{table_id}", json={"policy_mapping": True}, headers=_auth(token))
        assert r.status_code == 200, r.text

        assert len(fake_store.records) == 2
        latest = fake_store.records[-1]
        assert latest["policy_mapping"] is True
        # The body is unchanged — the revision records the table's CURRENT
        # policy under its new mapping flag, not an empty policy.
        assert latest["policy_sql"] == f"SELECT * FROM {table_id}"
        assert latest["policy_note"] == "why"

    def test_resending_the_same_policy_mapping_records_nothing(self, seeded_app, fake_store):
        """A no-op resend (the Edit modal round-trips every field) must not
        manufacture a revision that changed nothing."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        table_id = self._policied_table(c, token, fake_store, "rev_mapping_noop")

        assert (
            c.put(f"/api/admin/registry/{table_id}", json={"policy_mapping": True}, headers=_auth(token)).status_code
            == 200
        )
        assert len(fake_store.records) == 2

        assert (
            c.put(f"/api/admin/registry/{table_id}", json={"policy_mapping": True}, headers=_auth(token)).status_code
            == 200
        )
        assert len(fake_store.records) == 2, fake_store.records

    def test_an_unrelated_edit_still_records_nothing(self, seeded_app, fake_store):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        table_id = self._policied_table(c, token, fake_store, "rev_mapping_unrelated")

        assert (
            c.put(
                f"/api/admin/registry/{table_id}", json={"description": "unrelated"}, headers=_auth(token)
            ).status_code
            == 200
        )
        assert len(fake_store.records) == 1


class TestPolicyWriteLock:
    """finding 1 (follow-up review of PR #2023): the policy write and its
    history append are serialized per table, so two concurrent saves can
    never leave a history whose newest revision is not the stored policy."""

    def test_the_setters_and_the_record_run_inside_the_lock(self, seeded_app, fake_store, monkeypatch):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        table_id = _register(c, token, name="rev_lock_scope", server_only=True)
        # Registration takes the same per-table lock (and purges any orphaned
        # revisions under the reused id) -- not what this test is about.
        fake_store.events.clear()

        from src.repositories import table_registry_repo

        repo_cls = type(table_registry_repo())
        original_set_policy = repo_cls.set_access_policy
        original_set_mapping = repo_cls.set_policy_mapping

        def _spy_set_policy(self, *args, **kwargs):
            fake_store.events.append("set_access_policy")
            return original_set_policy(self, *args, **kwargs)

        def _spy_set_mapping(self, *args, **kwargs):
            fake_store.events.append("set_policy_mapping")
            return original_set_mapping(self, *args, **kwargs)

        monkeypatch.setattr(repo_cls, "set_access_policy", _spy_set_policy)
        monkeypatch.setattr(repo_cls, "set_policy_mapping", _spy_set_mapping)

        r = c.put(
            f"/api/admin/registry/{table_id}",
            json={
                "access_policy_sql": f"SELECT * FROM {table_id}",
                "access_policy_note": "why",
                "policy_mapping": True,
            },
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text

        assert fake_store.events == [
            f"lock_enter:{table_id}",
            "set_access_policy",
            "set_policy_mapping",
            f"record:{table_id}",
            f"lock_exit:{table_id}",
        ], fake_store.events

    def _spy_repo_method(self, monkeypatch, fake_store, name):
        """Record ``name`` on the fake store's event log when the registry
        repo method is called, so a test can place it relative to the lock."""
        from src.repositories import table_registry_repo

        repo_cls = type(table_registry_repo())
        original = getattr(repo_cls, name)

        def _spy(self, *args, **kwargs):
            fake_store.events.append(name)
            return original(self, *args, **kwargs)

        monkeypatch.setattr(repo_cls, name, _spy)

    def test_a_put_with_no_policy_field_still_runs_under_the_lock(self, seeded_app, fake_store, monkeypatch):
        """R17-2: the lock covers the WHOLE read -> validate -> write
        sequence of every registry PUT, not just the policy setters.

        ``update_table`` upserts EVERY column from the row it read on the way
        in, so two concurrent PUTs touching disjoint fields each write the
        other's stale value back. Taking the lock only for policy-carrying
        PUTs left that open; a plain edit must enter it too."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        table_id = _register(c, token, name="rev_lock_plain_put", server_only=True)
        fake_store.events.clear()
        self._spy_repo_method(monkeypatch, fake_store, "register")

        r = c.put(
            f"/api/admin/registry/{table_id}",
            json={"description": "not a policy field in sight"},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text

        assert fake_store.events == [
            f"lock_enter:{table_id}",
            "register",
            f"lock_exit:{table_id}",
        ], fake_store.events

    def test_unregister_purges_and_drops_the_row_under_the_lock(self, seeded_app, fake_store, monkeypatch):
        """R17-1: the DELETE purges the table's revision history and then
        drops the registry row. Unserialized, a policy save already holding
        the lock could append a revision between the two, stranding history
        under a dead table id -- and offering it to whatever is registered at
        that reused id next."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        table_id = _register(c, token, name="rev_lock_delete", server_only=True)
        fake_store.events.clear()
        self._spy_repo_method(monkeypatch, fake_store, "unregister")

        r = c.delete(f"/api/admin/registry/{table_id}", headers=_auth(token))
        assert r.status_code == 204, r.text

        assert fake_store.events == [
            f"lock_enter:{table_id}",
            f"delete_for_table:{table_id}",
            "unregister",
            f"lock_exit:{table_id}",
        ], fake_store.events

    def test_register_purges_and_inserts_under_the_lock(self, seeded_app, fake_store, monkeypatch):
        """The third writer of one table id: a re-registration purges the
        orphaned revisions and inserts the row, and must not interleave with
        either of the other two."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        self._spy_repo_method(monkeypatch, fake_store, "register")

        table_id = _register(c, token, name="rev_lock_register", server_only=True)

        assert fake_store.events == [
            f"lock_enter:{table_id}",
            f"delete_for_table:{table_id}",
            "register",
            f"lock_exit:{table_id}",
        ], fake_store.events

    def test_no_store_means_no_lock_and_the_policy_still_saves(self, seeded_app, monkeypatch):
        """On a DuckDB-backed instance the store raises
        ``RequiresPostgresBackend``; the save then runs exactly as it did
        before this lock existed — history is never load-bearing."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c, token = seeded_app["client"], seeded_app["admin_token"]
        table_id = _register(c, token, name="rev_lock_absent", server_only=True)

        from src.repositories import RequiresPostgresBackend

        calls: list[str] = []

        def _no_store():
            calls.append("resolve")
            raise RequiresPostgresBackend("access_policy_revisions")

        monkeypatch.setattr("app.api.admin.access_policy_revisions_repo", _no_store)

        r = c.put(
            f"/api/admin/registry/{table_id}",
            json={"access_policy_sql": f"SELECT * FROM {table_id}", "access_policy_note": "why"},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        assert calls, "the route never even looked for a revision store"

        from src.repositories import table_registry_repo

        assert table_registry_repo().get(table_id)["access_policy_sql"] is not None


class TestNoOpResendsRecordNothing:
    """finding 1 (second follow-up review of PR #2023): the Edit modal
    round-trips EVERY field, so a save that changed something unrelated
    (or nothing at all) re-sends the identical ``access_policy_sql`` +
    ``access_policy_note``. Keying the policy write on the mere PRESENCE of
    those keys turned each such resend into a `set_access_policy` call (a
    fresh ``access_policy_updated_at``/``_updated_by`` stamp), a dedicated
    ``access_policy.set`` audit row, and a history revision that changed
    nothing -- three lies about a policy nobody touched. The condition now
    compares the FINAL value against the stored one, exactly like its
    ``policy_mapping`` sibling already did.
    """

    def _policied_table(self, c, token, store, name):
        table_id = _register(c, token, name=name, server_only=True)
        r = c.put(
            f"/api/admin/registry/{table_id}",
            json={"access_policy_sql": f"SELECT * FROM {table_id}", "access_policy_note": "why"},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        assert len(store.records) == 1
        return table_id

    def _audit_rows(self, **filters):
        from src.repositories import audit_repo

        result = audit_repo().query(**filters)
        return list(result[0] if isinstance(result, tuple) else result)

    def test_resending_the_identical_policy_records_nothing(self, seeded_app, fake_store):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        table_id = self._policied_table(c, token, fake_store, "rev_body_noop")

        from src.repositories import table_registry_repo

        stamped_at = table_registry_repo().get(table_id)["access_policy_updated_at"]

        r = c.put(
            f"/api/admin/registry/{table_id}",
            json={
                "access_policy_sql": f"SELECT * FROM {table_id}",
                "access_policy_note": "why",
                "description": "an unrelated edit that round-trips the policy fields",
            },
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text

        assert len(fake_store.records) == 1, fake_store.records
        row = table_registry_repo().get(table_id)
        assert row["access_policy_updated_at"] == stamped_at, "a no-op resend re-stamped the policy metadata"
        assert row["description"] == "an unrelated edit that round-trips the policy fields"

        # Exactly one dedicated `access_policy.set` row -- the real attach.
        rows = self._audit_rows(action="access_policy.set", resource=table_id)
        assert len(rows) == 1, rows

    def test_resending_the_identical_policy_calls_no_setter(self, seeded_app, fake_store, monkeypatch):
        """The metadata assertion above proves the observable effect; this
        proves the mechanism -- ``set_access_policy`` is never reached, so
        the write lock and the revision append have nothing to order."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        table_id = self._policied_table(c, token, fake_store, "rev_body_noop_setter")

        from src.repositories import table_registry_repo

        repo_cls = type(table_registry_repo())
        calls: list[str] = []
        original = repo_cls.set_access_policy

        def _spy(self, *args, **kwargs):
            calls.append("set_access_policy")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(repo_cls, "set_access_policy", _spy)

        r = c.put(
            f"/api/admin/registry/{table_id}",
            json={"access_policy_sql": f"SELECT * FROM {table_id}", "access_policy_note": "why"},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        assert calls == [], calls

    def test_changing_only_the_note_still_records(self, seeded_app, fake_store):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        table_id = self._policied_table(c, token, fake_store, "rev_note_only")

        r = c.put(
            f"/api/admin/registry/{table_id}",
            json={"access_policy_sql": f"SELECT * FROM {table_id}", "access_policy_note": "a better reason"},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text

        assert len(fake_store.records) == 2, fake_store.records
        assert fake_store.records[-1]["policy_note"] == "a better reason"
        assert fake_store.records[-1]["policy_sql"] == f"SELECT * FROM {table_id}"

    def test_clearing_the_policy_still_records(self, seeded_app, fake_store):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        table_id = self._policied_table(c, token, fake_store, "rev_clear_still_records")

        r = c.put(
            f"/api/admin/registry/{table_id}",
            json={"access_policy_sql": None},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text

        assert len(fake_store.records) == 2, fake_store.records
        assert fake_store.records[-1]["policy_sql"] is None

        from src.repositories import table_registry_repo

        assert table_registry_repo().get(table_id)["access_policy_sql"] is None

        rows = self._audit_rows(action="access_policy.clear", resource=table_id)
        assert len(rows) == 1, rows

    def test_clearing_an_already_clear_policy_records_nothing(self, seeded_app, fake_store):
        """A table that never had a policy, PUT with an explicit
        ``access_policy_sql: null`` (what the modal sends for an empty
        editor), must not manufacture a "cleared" revision -- nothing was
        cleared."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        table_id = _register(c, token, name="rev_clear_noop", server_only=True)

        r = c.put(
            f"/api/admin/registry/{table_id}",
            json={"access_policy_sql": None, "access_policy_note": None},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        assert fake_store.records == [], fake_store.records
        assert self._audit_rows(action="access_policy.clear", resource=table_id) == []


class TestRevisionSnapshotIsTakenUnderTheLock:
    """finding 3 (third follow-up review of PR #2023): the merged policy
    values a save persists (and records) must be derived from the registry
    row as it is INSIDE the per-table write lock, not from the pre-lock
    snapshot the handler read on the way in.

    The race the lock alone does not close: request A edits the body and
    commits under the lock; request B -- a mapping-only flip -- already
    computed its finals from the pre-A row, then takes the lock and records
    a revision carrying A's PREDECESSOR body. The newest revision is then a
    policy nobody saved, and "restore this version" restores the wrong one.
    """

    def _policied_table(self, c, token, store, name, *, sql, note):
        table_id = _register(c, token, name=name, server_only=True)
        r = c.put(
            f"/api/admin/registry/{table_id}",
            json={"access_policy_sql": sql, "access_policy_note": note},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        assert len(store.records) == 1
        return table_id

    def test_mapping_only_flip_records_the_body_committed_under_the_lock(self, seeded_app, fake_store, monkeypatch):
        c, token = seeded_app["client"], seeded_app["admin_token"]
        table_id = self._policied_table(
            c,
            token,
            fake_store,
            "rev_race_snapshot",
            sql="SELECT * FROM rev_race_snapshot WHERE 1 = 1",
            note="the body request B read on its way in",
        )

        from src.repositories import table_registry_repo

        a_sql = "SELECT * FROM rev_race_snapshot WHERE 2 = 2"
        a_note = "request A's newer reason"

        @contextlib.contextmanager
        def _racing_lock(locked_table_id: str):
            # Request A commits between B's pre-lock read and B taking the
            # lock -- i.e. exactly when B can no longer see it without
            # re-reading. Writing it from ``__enter__`` is what makes this
            # deterministic instead of thread-timing-dependent.
            table_registry_repo().set_access_policy(
                locked_table_id, sql=a_sql, note=a_note, updated_by="request-a@example.com"
            )
            fake_store.events.append(f"lock_enter:{locked_table_id}")
            try:
                yield
            finally:
                fake_store.events.append(f"lock_exit:{locked_table_id}")

        monkeypatch.setattr(fake_store, "policy_write_lock", _racing_lock)

        r = c.put(f"/api/admin/registry/{table_id}", json={"policy_mapping": True}, headers=_auth(token))
        assert r.status_code == 200, r.text

        row = table_registry_repo().get(table_id)
        assert row["access_policy_sql"] == a_sql, "request A's body must survive a mapping-only flip"

        latest = fake_store.records[-1]
        assert latest["policy_sql"] == row["access_policy_sql"]
        assert latest["policy_note"] == row["access_policy_note"] == a_note
        assert latest["policy_mapping"] is True, "and it carries request B's own mapping flip"

    def test_sequential_body_edit_then_mapping_flip_records_both_in_order(self, seeded_app, fake_store):
        """The uncontended path is unchanged: two ordinary saves leave two
        revisions, newest last, each carrying the state it saved."""
        c, token = seeded_app["client"], seeded_app["admin_token"]
        table_id = self._policied_table(
            c,
            token,
            fake_store,
            "rev_sequential_snapshot",
            sql="SELECT * FROM rev_sequential_snapshot WHERE 1 = 1",
            note="v1",
        )

        second_sql = "SELECT * FROM rev_sequential_snapshot WHERE 2 = 2"
        assert (
            c.put(
                f"/api/admin/registry/{table_id}",
                json={"access_policy_sql": second_sql, "access_policy_note": "v2"},
                headers=_auth(token),
            ).status_code
            == 200
        )
        assert (
            c.put(f"/api/admin/registry/{table_id}", json={"policy_mapping": True}, headers=_auth(token)).status_code
            == 200
        )

        assert [(r["policy_sql"], r["policy_note"], r["policy_mapping"]) for r in fake_store.records] == [
            ("SELECT * FROM rev_sequential_snapshot WHERE 1 = 1", "v1", False),
            (second_sql, "v2", False),
            (second_sql, "v2", True),
        ], fake_store.records
