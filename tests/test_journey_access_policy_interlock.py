"""Task 4 — the distribution interlock on ``PUT /registry/{table_id}``
(table access policies design doc §3.1/§3.2).

Mirrors ``tests/test_journey_server_only.py``: HTTP-level, admin token,
``seeded_app``. A policy may only be attached to a table that is not
distributed (``query_mode='remote'`` or ``server_only=true``); attaching it
is itself gated behind the ``access_policies.enabled`` feature flag.
"""

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(c, token, **kwargs) -> str:
    kwargs.setdefault("source_type", "keboola")
    kwargs.setdefault("query_mode", "local")
    resp = c.post("/api/admin/register-table", json=kwargs, headers=_auth(token))
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _policy_sql(table_name: str) -> str:
    return f"SELECT * FROM {table_name}"


@pytest.mark.journey
class TestFeatureFlagGate:
    def test_attach_rejected_when_flag_disabled(self, seeded_app, monkeypatch):
        """The whole policy-write path is dark until an operator opts in."""
        monkeypatch.delenv("AGNES_ACCESS_POLICIES_ENABLED", raising=False)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        table_id = _register(c, token, name="flag_off_tbl", server_only=True)

        resp = c.put(
            f"/api/admin/registry/{table_id}",
            json={
                "access_policy_sql": _policy_sql("flag_off_tbl"),
                "access_policy_note": "pii masking",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "access_policies_disabled" in resp.text

        # And the write never landed.
        from src.repositories import table_registry_repo

        assert table_registry_repo().get(table_id)["access_policy_sql"] is None


@pytest.mark.journey
class TestInterlockCaseA:
    """§3.1 — attaching a policy to a table that is neither remote nor
    server_only."""

    def test_attach_rejected_on_a_distributed_table(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        # query_mode='local', server_only defaults False -> distributed.
        table_id = _register(c, token, name="distributed_tbl")

        resp = c.put(
            f"/api/admin/registry/{table_id}",
            json={
                "access_policy_sql": _policy_sql("distributed_tbl"),
                "access_policy_note": "pii masking",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "set server_only=true first" in resp.text


@pytest.mark.journey
class TestInterlockCaseB:
    """§3.1 — clearing server_only, or moving query_mode to 'local', on a
    table that currently has a policy. Same validator as case A: the
    interlock is one shared check on the merged record, so it catches both
    directions."""

    def test_clearing_server_only_on_a_policied_table_is_rejected(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        table_id = _register(c, token, name="attach_then_flip", server_only=True)

        attach = c.put(
            f"/api/admin/registry/{table_id}",
            json={
                "access_policy_sql": _policy_sql("attach_then_flip"),
                "access_policy_note": "pii masking",
            },
            headers=_auth(token),
        )
        assert attach.status_code == 200, attach.text

        # A SEPARATE PUT that never mentions access_policy_sql at all —
        # exactly the "one toggle away from publishing the raw table" shape
        # the design doc calls out.
        resp = c.put(
            f"/api/admin/registry/{table_id}",
            json={"server_only": False},
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "access_policy_requires_undistributed" in resp.text

        from src.repositories import table_registry_repo

        row = table_registry_repo().get(table_id)
        assert row["server_only"] is True, "the refused write must not have partially landed"
        assert row["access_policy_sql"] is not None

    def test_moving_a_policied_remote_table_to_local_is_rejected(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        table_id = _register(c, token, name="remote_policied", query_mode="remote")

        attach = c.put(
            f"/api/admin/registry/{table_id}",
            json={
                "access_policy_sql": _policy_sql("remote_policied"),
                "access_policy_note": "pii masking",
            },
            headers=_auth(token),
        )
        assert attach.status_code == 200, attach.text

        resp = c.put(
            f"/api/admin/registry/{table_id}",
            json={"query_mode": "local"},
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "access_policy_requires_undistributed" in resp.text


@pytest.mark.journey
class TestInterlockCaseC:
    """§3.2 — the physical-source twin: a different, distributable row
    resolving to the same physical source as a policied table."""

    def test_attaching_a_policy_while_a_distributable_twin_exists_is_rejected(self, seeded_app, monkeypatch):
        """The ATTACH direction of §3.2 — the one the register-time and
        twin-write checks structurally cannot cover.

        A row carrying a policy is by construction non-distributable
        (§3.1 forces ``query_mode='remote'`` or ``server_only=true``), so
        the "is THIS row a distributable twin of a policied one" check
        short-circuits on the attach path — it only ever rejects the
        TWIN's own write. Registering the twin FIRST and then attaching
        the policy therefore used to be accepted with no scan at all, and
        since nothing ever PUTs the twin again the interlock never ran:
        ``agnes pull`` kept distributing the twin's unfiltered parquet
        forever. The attach must do the symmetric scan itself."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        policied_id = _register(
            c,
            token,
            name="orig_src",
            server_only=True,
            bucket="in.c-main",
            source_table="invoices",
        )
        # Registered while orig_src carries no policy yet — legitimate at
        # that moment (two unpolicied rows sharing a source is not a leak).
        twin_id = _register(
            c,
            token,
            name="twin_src",
            bucket="in.c-main",
            source_table="invoices",
        )

        attach = c.put(
            f"/api/admin/registry/{policied_id}",
            json={
                "access_policy_sql": _policy_sql("orig_src"),
                "access_policy_note": "pii masking",
            },
            headers=_auth(token),
        )
        assert attach.status_code == 422, attach.text
        assert "access_policy_physical_source_conflict" in attach.text
        assert twin_id in attach.text

        # And the refused attach never landed.
        from src.repositories import table_registry_repo

        assert table_registry_repo().get(policied_id)["access_policy_sql"] is None

    def test_a_twin_flipped_to_distributable_after_the_attach_is_rejected(self, seeded_app, monkeypatch):
        """PUT-path defense-in-depth in the other direction:
        ``TestRegisterTimeInterlock`` covers a brand-new twin being caught
        the moment IT is registered; this covers an EXISTING twin that was
        registered first, so nothing would ever re-run the twin-side check
        for it. The attach is what gets refused.

        This test used to assert the opposite — that the attach succeeds
        and only a LATER flip to distributable is refused — because the
        interlock keyed on ``agnes pull``. An undistributed twin leaks the
        same rows through ``/api/query`` under its own name, so the attach
        is now the rejection point."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        policied_id = _register(
            c,
            token,
            name="flip_orig_src",
            server_only=True,
            bucket="in.c-main",
            source_table="credit_notes",
        )
        twin_id = _register(
            c,
            token,
            name="flip_twin_src",
            server_only=True,
            bucket="in.c-main",
            source_table="credit_notes",
        )

        attach = c.put(
            f"/api/admin/registry/{policied_id}",
            json={
                "access_policy_sql": _policy_sql("flip_orig_src"),
                "access_policy_note": "pii masking",
            },
            headers=_auth(token),
        )
        assert attach.status_code == 422, attach.text
        assert "access_policy_physical_source_conflict" in attach.text
        assert twin_id in attach.text

        # Clear the twin out of the way and the same attach is accepted —
        # the rejection is about the twin, not about this policy.
        assert c.delete(f"/api/admin/registry/{twin_id}", headers=_auth(token)).status_code in (200, 204)
        retry = c.put(
            f"/api/admin/registry/{policied_id}",
            json={
                "access_policy_sql": _policy_sql("flip_orig_src"),
                "access_policy_note": "pii masking",
            },
            headers=_auth(token),
        )
        assert retry.status_code == 200, retry.text

    def test_a_twin_that_stays_server_only_is_also_rejected(self, seeded_app, monkeypatch):
        """Physical-source overlap is the conflict, distributability only
        decides the wording.

        The original version of this test asserted that two undistributed
        rows over one source may coexist, on the reasoning that neither is
        downloaded by ``agnes pull``. A live instance disproved it: the
        unpolicied row answers ``/api/query`` server-side under its own
        name and returns exactly the rows the policy withholds, to anyone
        granted it."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        policied_id = _register(
            c,
            token,
            name="orig_src2",
            server_only=True,
            bucket="in.c-main",
            source_table="orders",
        )
        attach = c.put(
            f"/api/admin/registry/{policied_id}",
            json={
                "access_policy_sql": _policy_sql("orig_src2"),
                "access_policy_note": "pii masking",
            },
            headers=_auth(token),
        )
        assert attach.status_code == 200, attach.text

        # Registered BEFORE the policy existed, so it is already on disk —
        # the shape a live instance actually gets into.
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository

        conn = get_system_db()
        try:
            TableRegistryRepository(conn).register(
                id="twin_src2",
                name="twin_src2",
                source_type="keboola",
                query_mode="local",
                server_only=True,
                bucket="in.c-main",
                source_table="orders",
            )
        finally:
            conn.close()

        resp = c.put(
            "/api/admin/registry/twin_src2",
            json={"description": "an unrelated edit"},
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "access_policy_physical_source_conflict" in resp.text
        assert policied_id in resp.text


@pytest.mark.journey
class TestRegisterTimeInterlock:
    """§3.2 at register time — ``POST /api/admin/register-table`` must
    reject a brand-new distributable twin of an already-policied table's
    physical source by itself; it cannot rely on a follow-up PUT to catch
    it. Before this fix, ``register_table`` ran no physical-source check
    at all: a fresh registry row sharing a policied table's physical
    source landed unrejected whenever it was distributable (query_mode
    'local' or 'materialized', server_only left False) — and for a
    materialized row the next sync tick writes the raw, unfiltered rows to
    parquet with no follow-up PUT ever required.
    """

    def test_a_distributable_twin_is_rejected_at_register(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        policied_id = _register(
            c,
            token,
            name="reg_orig_src",
            server_only=True,
            bucket="in.c-main",
            source_table="invoices",
        )
        attach = c.put(
            f"/api/admin/registry/{policied_id}",
            json={
                "access_policy_sql": _policy_sql("reg_orig_src"),
                "access_policy_note": "pii masking",
            },
            headers=_auth(token),
        )
        assert attach.status_code == 200, attach.text

        # The twin: SAME physical source, distributable (query_mode='local',
        # server_only omitted -> False). Must be rejected at the POST itself.
        resp = c.post(
            "/api/admin/register-table",
            json={
                "name": "reg_twin_src",
                "source_type": "keboola",
                "query_mode": "local",
                "bucket": "in.c-main",
                "source_table": "invoices",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "access_policy_physical_source_conflict" in resp.text
        assert policied_id in resp.text

        # And the row never landed in the registry.
        from src.repositories import table_registry_repo

        assert table_registry_repo().get("reg_twin_src") is None

    def test_a_materialized_twin_is_rejected_at_register(self, seeded_app, monkeypatch):
        """Same interlock via query_mode='materialized' — the mode the
        finding singled out, since a materialized row's next sync tick
        writes the raw rows to parquet with no further admin action."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        policied_id = _register(
            c,
            token,
            name="mat_orig_src",
            server_only=True,
            bucket="in.c-main",
            source_table="shipments",
        )
        attach = c.put(
            f"/api/admin/registry/{policied_id}",
            json={
                "access_policy_sql": _policy_sql("mat_orig_src"),
                "access_policy_note": "pii masking",
            },
            headers=_auth(token),
        )
        assert attach.status_code == 200, attach.text

        resp = c.post(
            "/api/admin/register-table",
            json={
                "name": "mat_twin_src",
                "source_type": "keboola",
                "query_mode": "materialized",
                "bucket": "in.c-main",
                "source_table": "shipments",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "access_policy_physical_source_conflict" in resp.text
        assert policied_id in resp.text

        from src.repositories import table_registry_repo

        assert table_registry_repo().get("mat_twin_src") is None

    def test_a_server_only_twin_is_rejected_at_register(self, seeded_app, monkeypatch):
        """Register time refuses an undistributed twin too.

        Asserted the opposite until an undistributed twin was shown to
        serve the same rows through ``/api/query`` under its own name —
        ``agnes pull`` is one way out of the policy, not the only one."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        policied_id = _register(
            c,
            token,
            name="so_orig_src",
            server_only=True,
            bucket="in.c-main",
            source_table="payments",
        )
        attach = c.put(
            f"/api/admin/registry/{policied_id}",
            json={
                "access_policy_sql": _policy_sql("so_orig_src"),
                "access_policy_note": "pii masking",
            },
            headers=_auth(token),
        )
        assert attach.status_code == 200, attach.text

        resp = c.post(
            "/api/admin/register-table",
            json={
                "name": "so_twin_src",
                "source_type": "keboola",
                "query_mode": "local",
                "server_only": True,
                "bucket": "in.c-main",
                "source_table": "payments",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "access_policy_physical_source_conflict" in resp.text

        from src.repositories import table_registry_repo

        assert table_registry_repo().get("so_twin_src") is None


@pytest.mark.journey
class TestHappyPath:
    def test_policy_attaches_cleanly_to_a_server_only_table(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        table_id = _register(c, token, name="clean_attach", server_only=True)

        resp = c.put(
            f"/api/admin/registry/{table_id}",
            json={
                "access_policy_sql": _policy_sql("clean_attach"),
                "access_policy_note": "restrict to the caller's cost center",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        assert "access_policy_sql" in resp.json()["updated"]

        listing = c.get("/api/admin/registry", headers=_auth(token))
        assert listing.status_code == 200, listing.text
        persisted = next(t for t in listing.json()["tables"] if t["id"] == table_id)
        assert persisted["access_policy_sql"] == _policy_sql("clean_attach")
        assert persisted["access_policy_note"] == "restrict to the caller's cost center"
        assert persisted["access_policy_updated_by"] == "admin@test.com"
        assert persisted["access_policy_updated_at"] is not None

        # Clearing works too, and needs no flag (safety valve).
        monkeypatch.delenv("AGNES_ACCESS_POLICIES_ENABLED", raising=False)
        clear = c.put(
            f"/api/admin/registry/{table_id}",
            json={"access_policy_sql": None},
            headers=_auth(token),
        )
        assert clear.status_code == 200, clear.text

        from src.repositories import table_registry_repo

        row = table_registry_repo().get(table_id)
        assert row["access_policy_sql"] is None
        assert row["access_policy_note"] is None
        assert row["access_policy_updated_by"] is None


@pytest.mark.journey
class TestTwoPoliciedRowsStayUnwindable:
    """Two POLICIED rows over one physical source are legal (§3.2 says so
    explicitly: "each read goes through a policy, and which one an admin wants
    where is their call"). What follows from that has to be checked, because
    the twin scan runs on the MERGED record: clearing either policy leaves an
    unpolicied row over a source the other row still policies — the exact
    disclosure the interlock exists to refuse — so BOTH clears are 422 and
    neither ordering unwinds the pair.

    That refusal is correct on the merits and stays. What was not correct is
    the wording: the rejection told an admin who was *removing* a policy to
    "attach a policy to this row too", and the docstring on
    ``_check_policied_row_has_no_unpolicied_twin`` promised a safety valve
    ("an admin can always undo the policy") that this shape does not have.
    Two escapes do exist — repoint the row at a different source first, or
    unregister one of the pair — and the message has to name them.
    """

    def _pair(self, c, token):
        first = _register(c, token, name="pair_a", server_only=True, bucket="in.c-main", source_table="ledger")
        assert (
            c.put(
                f"/api/admin/registry/{first}",
                json={"access_policy_sql": _policy_sql("pair_a"), "access_policy_note": "a"},
                headers=_auth(token),
            ).status_code
            == 200
        )
        # The second row cannot be registered unpolicied (that is the twin
        # interlock), so it arrives pointed elsewhere and is repointed by a
        # PUT that attaches its own policy in the same write.
        second = _register(c, token, name="pair_b", server_only=True, bucket="in.c-main", source_table="other")
        moved = c.put(
            f"/api/admin/registry/{second}",
            json={
                "source_table": "ledger",
                "access_policy_sql": _policy_sql("pair_b"),
                "access_policy_note": "b",
            },
            headers=_auth(token),
        )
        assert moved.status_code == 200, moved.text
        return first, second

    def test_clearing_either_policy_is_refused_in_both_orders(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        first, second = self._pair(c, token)

        for target in (first, second):
            resp = c.put(
                f"/api/admin/registry/{target}",
                json={"access_policy_sql": None},
                headers=_auth(token),
            )
            assert resp.status_code == 422, resp.text
            assert "access_policy_physical_source_conflict" in resp.text
            # The message must not steer the admin into the one action that
            # cannot help — they are removing a policy, not missing one.
            assert "attach a policy to this row too" not in resp.text, resp.text
            # …and it must name an escape that works.
            assert "unregister" in resp.text, resp.text

    def test_repointing_first_unwinds_the_pair(self, seeded_app, monkeypatch):
        """The escape the message now names, exercised end to end: keep the
        policy while moving the row off the shared source, then clear it."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        first, _second = self._pair(c, token)

        moved = c.put(
            f"/api/admin/registry/{first}",
            json={"source_table": "ledger_archive"},
            headers=_auth(token),
        )
        assert moved.status_code == 200, moved.text
        cleared = c.put(
            f"/api/admin/registry/{first}",
            json={"access_policy_sql": None},
            headers=_auth(token),
        )
        assert cleared.status_code == 200, cleared.text


@pytest.mark.journey
class TestUnpinnedConnectionIdIsWildcard:
    """S5 physical-twin gap (issue #1979): an omitted/blank ``connection_id``
    on either side of the twin check must mean "any connection of that
    source type", not a literal ``""`` that fails to intersect a pinned
    ``connection_id``. Reviewer MonikaFeigler's live repro on a
    single-connection instance: register a policied ``order`` row pinned to
    a real ``connection_id``, then register a SECOND row over the same
    bucket/table with ``connection_id`` omitted -- the omitted value used to
    compare unequal to the pin and sail past the check entirely."""

    def _connection(self, conn_id: str = "conn-a") -> str:
        from src.repositories import source_connections_repo

        source_connections_repo().create(
            id=conn_id,
            name=conn_id,
            source_type="keboola",
            config={"stack_url": f"https://{conn_id}.keboola.com"},
        )
        return conn_id

    def test_monika_repro_unpinned_twin_next_to_pinned_policied_row_is_rejected_at_register(
        self, seeded_app, monkeypatch
    ):
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._connection()

        policied_id = _register(
            c,
            token,
            name="order",
            server_only=True,
            bucket="in.c-main",
            source_table="order",
            connection_id=conn_id,
        )
        attach = c.put(
            f"/api/admin/registry/{policied_id}",
            json={
                "access_policy_sql": _policy_sql("order"),
                "access_policy_note": "pii masking",
            },
            headers=_auth(token),
        )
        assert attach.status_code == 200, attach.text

        # Step 1 of the repro: a second row over the same bucket/table with
        # `connection_id` OMITTED. Must now be rejected -- before this fix,
        # signal `(keboola, "", bucket, table)` did not intersect
        # `(keboola, <uuid>, bucket, table)` and this landed with 201.
        resp = c.post(
            "/api/admin/register-table",
            json={
                "name": "order_twin",
                "source_type": "keboola",
                "query_mode": "local",
                "bucket": "in.c-main",
                "source_table": "order",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "access_policy_physical_source_conflict" in resp.text
        assert policied_id in resp.text
        # Tells the admin how to disambiguate if this really is a different
        # connection reusing the same bucket/table label.
        assert "connection_id" in resp.text

        from src.repositories import table_registry_repo

        assert table_registry_repo().get("order_twin") is None

    def test_monika_repro_step2_pinning_the_twin_to_the_same_connection_stays_rejected(self, seeded_app, monkeypatch):
        """Step 2 of the repro, unchanged by this fix: a row that already
        exists unpinned (inserted directly, bypassing the register-time
        check, to reach the shape a live instance had before the fix) is
        still rejected when PUT pins it to the SAME connection as the
        policied row."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._connection()

        policied_id = _register(
            c,
            token,
            name="order2",
            server_only=True,
            bucket="in.c-main",
            source_table="order2",
            connection_id=conn_id,
        )
        assert (
            c.put(
                f"/api/admin/registry/{policied_id}",
                json={"access_policy_sql": _policy_sql("order2"), "access_policy_note": "pii masking"},
                headers=_auth(token),
            ).status_code
            == 200
        )

        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository

        conn = get_system_db()
        try:
            TableRegistryRepository(conn).register(
                id="order2_twin",
                name="order2_twin",
                source_type="keboola",
                query_mode="local",
                bucket="in.c-main",
                source_table="order2",
            )
        finally:
            conn.close()

        resp = c.put(
            "/api/admin/registry/order2_twin",
            json={"connection_id": conn_id},
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "access_policy_physical_source_conflict" in resp.text

    def test_pinned_twin_next_to_unpinned_policied_row_is_rejected_at_register(self, seeded_app, monkeypatch):
        """The mirror direction: the POLICIED row is unpinned and the new
        row PINS a connection over the same bucket/table."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._connection("conn-b")

        policied_id = _register(
            c,
            token,
            name="unpinned_orig",
            server_only=True,
            bucket="in.c-main",
            source_table="invoices3",
        )
        attach = c.put(
            f"/api/admin/registry/{policied_id}",
            json={
                "access_policy_sql": _policy_sql("unpinned_orig"),
                "access_policy_note": "pii masking",
            },
            headers=_auth(token),
        )
        assert attach.status_code == 200, attach.text

        resp = c.post(
            "/api/admin/register-table",
            json={
                "name": "pinned_twin",
                "source_type": "keboola",
                "query_mode": "local",
                "bucket": "in.c-main",
                "source_table": "invoices3",
                "connection_id": conn_id,
            },
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "access_policy_physical_source_conflict" in resp.text
        assert policied_id in resp.text

    def test_attaching_a_policy_while_an_unpinned_twin_exists_is_rejected(self, seeded_app, monkeypatch):
        """The ATTACH-direction interlock must also honor the wildcard: the
        row about to be policied is PINNED to a connection, and the
        existing unpolicied twin is unpinned."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_id = self._connection("conn-c")

        # Registered while unpinned and unpolicied -- legitimate at that
        # moment, two unpolicied rows sharing a source is not a leak.
        twin_id = _register(
            c,
            token,
            name="unpinned_twin",
            bucket="in.c-main",
            source_table="shipments3",
        )
        # Registered pinned, also unpolicied yet -- also legitimate, both
        # rows are still unpolicied at this point.
        pinned_id = _register(
            c,
            token,
            name="pinned_orig",
            server_only=True,
            bucket="in.c-main",
            source_table="shipments3",
            connection_id=conn_id,
        )

        attach = c.put(
            f"/api/admin/registry/{pinned_id}",
            json={
                "access_policy_sql": _policy_sql("pinned_orig"),
                "access_policy_note": "pii masking",
            },
            headers=_auth(token),
        )
        assert attach.status_code == 422, attach.text
        assert "access_policy_physical_source_conflict" in attach.text
        assert twin_id in attach.text

        from src.repositories import table_registry_repo

        assert table_registry_repo().get(pinned_id)["access_policy_sql"] is None

    def test_different_pinned_connections_over_same_bucket_table_do_not_conflict(self, seeded_app, monkeypatch):
        """Two rows that both PIN different, real connection_ids over the
        same bucket/table are genuinely different projects -- the wildcard
        rule must not treat them as twins."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        conn_a = self._connection("conn-d1")
        conn_b = self._connection("conn-d2")

        policied_id = _register(
            c,
            token,
            name="proj_a_orders",
            server_only=True,
            bucket="in.c-main",
            source_table="orders_shared_label",
            connection_id=conn_a,
        )
        attach = c.put(
            f"/api/admin/registry/{policied_id}",
            json={
                "access_policy_sql": _policy_sql("proj_a_orders"),
                "access_policy_note": "pii masking",
            },
            headers=_auth(token),
        )
        assert attach.status_code == 200, attach.text

        resp = c.post(
            "/api/admin/register-table",
            json={
                "name": "proj_b_orders",
                "source_type": "keboola",
                "query_mode": "local",
                "bucket": "in.c-main",
                "source_table": "orders_shared_label",
                "connection_id": conn_b,
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201, resp.text


class TestClearAndDistributeInOnePut:
    """The safety valve the interlock must NOT swallow: clearing the policy
    and making the table distributable again in ONE request.

    Load-bearing for the repository-level invariant (#2147): `register()` now
    refuses an upsert that would leave a still-policied row distributable, and
    it reads the row as it stands on disk — so the handler has to clear the
    policy BEFORE it upserts, not after.
    """

    def test_clearing_the_policy_and_distributing_in_one_put_is_allowed(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        table_id = _register(c, token, name="clear_and_distribute", server_only=True)

        attach = c.put(
            f"/api/admin/registry/{table_id}",
            json={
                "access_policy_sql": _policy_sql("clear_and_distribute"),
                "access_policy_note": "pii masking",
            },
            headers=_auth(token),
        )
        assert attach.status_code == 200, attach.text

        resp = c.put(
            f"/api/admin/registry/{table_id}",
            json={"access_policy_sql": None, "server_only": False},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text

        from src.repositories import table_registry_repo

        row = table_registry_repo().get(table_id)
        assert row["access_policy_sql"] is None
        assert bool(row["server_only"]) is False

    def test_clearing_the_policy_alone_still_leaves_the_table_undistributed(self, seeded_app, monkeypatch):
        """Clearing without asking for distribution changes nothing else."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        table_id = _register(c, token, name="clear_only", server_only=True)

        c.put(
            f"/api/admin/registry/{table_id}",
            json={
                "access_policy_sql": _policy_sql("clear_only"),
                "access_policy_note": "pii masking",
            },
            headers=_auth(token),
        )
        resp = c.put(
            f"/api/admin/registry/{table_id}",
            json={"access_policy_sql": None},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text

        from src.repositories import table_registry_repo

        row = table_registry_repo().get(table_id)
        assert row["access_policy_sql"] is None
        assert row["access_policy_note"] is None
        assert row["access_policy_updated_by"] is None
        assert bool(row["server_only"]) is True


# ---------------------------------------------------------------------------
# Issue #2147, backlog item 4 — the canonical physical-identifier signal.
#
# The first three twin signals (`bq_fqn`, `(source_type, connection_id,
# bucket, source_table)`, verbatim `source_query`) are all *representations*
# of a physical table, and two rows naming ONE table in two different
# representations produced disjoint signal sets: a materialized row whose SQL
# reads the policied table, a `bq_fqn` row beside a `bucket`+`source_table`
# row, a Databricks `sales` bucket beside `main.sales`. Each of those was
# accepted at registration, and for a materialized row the next sync tick
# writes the unfiltered rows to a parquet `agnes pull` distributes.
# ---------------------------------------------------------------------------


@pytest.fixture
def dbx_instance(monkeypatch):
    """An instance whose Databricks default catalog is ``main`` — what makes
    the bucket ``sales`` and the bucket ``main.sales`` the same table."""
    fake_cfg = {
        "data_source": {
            "type": "local",
            "databricks": {"catalog": "main", "workspace_host": "https://example.cloud.databricks.com"},
        },
    }
    monkeypatch.setattr(
        "app.instance_config.load_instance_config",
        lambda: fake_cfg,
        raising=False,
    )
    from app.instance_config import reset_cache

    reset_cache()
    yield fake_cfg
    reset_cache()


def _register_bq(c, token, **payload):
    payload.setdefault("source_type", "bigquery")
    return c.post("/api/admin/register-table", json=payload, headers=_auth(token))


def _bq_policied_remote_row(c, token, *, name, dataset, table):
    """A policied BigQuery row over ``dataset.table``, registered the ordinary
    way (a live BQ registration is coerced to ``query_mode='remote'``, which
    satisfies §3.1 on its own) and given a policy."""
    resp = _register_bq(c, token, name=name, bucket=dataset, source_table=table)
    assert resp.status_code in (200, 201, 202), resp.text
    table_id = resp.json()["id"]
    attach = c.put(
        f"/api/admin/registry/{table_id}",
        json={"access_policy_sql": _policy_sql(name), "access_policy_note": "pii masking"},
        headers=_auth(token),
    )
    assert attach.status_code == 200, attach.text
    return table_id


@pytest.mark.journey
class TestMaterializedSqlOverAPoliciedSource:
    """Gap 1 — a ``query_mode='materialized'`` row whose ``source_query``
    READS the policied table's physical source. Its only pre-fix signal was
    the verbatim SQL text, which by construction never intersects the
    policied row's ``bq_fqn`` / ``bucket_table`` signals."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM `my-test-project.analytics.sales`",
            "SELECT * FROM my-test-project.analytics.sales",
            "SELECT * FROM `my-test-project`.`analytics`.`sales`",
            "SELECT * FROM analytics.sales",
            "SELECT * FROM `analytics.sales`",
            "-- nightly dump\nSELECT s.id, s.amount\nFROM `analytics.sales` AS s\nWHERE s.amount > 0",
        ],
    )
    def test_materialized_row_reading_the_policied_table_is_rejected_at_register(
        self, seeded_app, monkeypatch, bq_instance, stub_bq_extractor, sql
    ):
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        policied_id = _bq_policied_remote_row(c, token, name="sqltwin_src", dataset="analytics", table="sales")

        resp = _register_bq(
            c,
            token,
            name="sqltwin_mat",
            query_mode="materialized",
            source_query=sql,
        )
        assert resp.status_code == 422, resp.text
        assert "access_policy_physical_source_conflict" in resp.text
        assert policied_id in resp.text
        # The note names the physical table reference that matched.
        assert "analytics.sales" in resp.text

        from src.repositories import table_registry_repo

        assert table_registry_repo().get("sqltwin_mat") is None

    def test_put_moving_a_materialized_row_onto_the_policied_table_is_rejected(
        self, seeded_app, monkeypatch, bq_instance, stub_bq_extractor
    ):
        """The update direction: a row registered over a harmless table is
        repointed at the policied one by a later PUT."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        policied_id = _bq_policied_remote_row(c, token, name="sqlput_src", dataset="analytics", table="sales")

        created = _register_bq(
            c,
            token,
            name="sqlput_mat",
            query_mode="materialized",
            source_query="SELECT * FROM `my-test-project.analytics.harmless`",
        )
        assert created.status_code in (200, 201, 202), created.text

        resp = c.put(
            "/api/admin/registry/sqlput_mat",
            json={
                "query_mode": "materialized",
                "source_query": "SELECT * FROM `my-test-project.analytics.sales`",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "access_policy_physical_source_conflict" in resp.text
        assert policied_id in resp.text

        from src.repositories import table_registry_repo

        assert "harmless" in table_registry_repo().get("sqlput_mat")["source_query"]

    def test_attaching_a_policy_while_a_materialized_reader_exists_is_rejected(
        self, seeded_app, monkeypatch, bq_instance, stub_bq_extractor
    ):
        """The ATTACH direction — the materialized reader was registered
        FIRST, so nothing ever PUTs it again and only the mirror scan can
        catch it."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        reader = _register_bq(
            c,
            token,
            name="sqlattach_mat",
            query_mode="materialized",
            source_query="SELECT * FROM `my-test-project.analytics.sales`",
        )
        assert reader.status_code in (200, 201, 202), reader.text

        src = _register_bq(c, token, name="sqlattach_src", bucket="analytics", source_table="sales")
        assert src.status_code in (200, 201, 202), src.text

        resp = c.put(
            "/api/admin/registry/sqlattach_src",
            json={"access_policy_sql": _policy_sql("sqlattach_src"), "access_policy_note": "pii masking"},
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "access_policy_physical_source_conflict" in resp.text
        assert "sqlattach_mat" in resp.text

        from src.repositories import table_registry_repo

        assert table_registry_repo().get("sqlattach_src")["access_policy_sql"] is None

    def test_refusal_lands_before_the_row_can_ever_be_materialized(
        self, seeded_app, monkeypatch, bq_instance, stub_bq_extractor
    ):
        """A materialized row is picked up by the sync trigger pass FROM THE
        REGISTRY — so "not scheduled" means "never persisted". Assert the row
        is absent, no rebuild ran, and no register_table audit entry landed."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        _bq_policied_remote_row(c, token, name="presched_src", dataset="analytics", table="sales")
        stub_bq_extractor.reset_mock()

        resp = _register_bq(
            c,
            token,
            name="presched_mat",
            query_mode="materialized",
            source_query="SELECT * FROM `my-test-project.analytics.sales`",
        )
        assert resp.status_code == 422, resp.text

        from src.repositories import audit_repo, table_registry_repo

        assert table_registry_repo().get("presched_mat") is None
        assert stub_bq_extractor.call_count == 0
        entries, _cursor = audit_repo().query(action="register_table", resource="presched_mat", limit=50)
        assert entries == []

    def test_a_materialized_row_over_a_different_table_is_accepted(
        self, seeded_app, monkeypatch, bq_instance, stub_bq_extractor
    ):
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        _bq_policied_remote_row(c, token, name="negsql_src", dataset="analytics", table="sales")

        resp = _register_bq(
            c,
            token,
            name="negsql_mat",
            query_mode="materialized",
            source_query="SELECT * FROM `my-test-project.analytics.returns`",
        )
        assert resp.status_code in (200, 201, 202), resp.text

        from src.repositories import table_registry_repo

        assert table_registry_repo().get("negsql_mat") is not None

    def test_a_policied_materialized_row_over_its_own_source_stays_legal(
        self, seeded_app, monkeypatch, bq_instance, stub_bq_extractor
    ):
        """A materialized row reads its own physical source by definition;
        the check must never fire against the row's own signals."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        created = _register_bq(
            c,
            token,
            name="selfmat",
            query_mode="materialized",
            bucket="analytics",
            source_table="sales",
            server_only=True,
        )
        assert created.status_code in (200, 201, 202), created.text

        resp = c.put(
            "/api/admin/registry/selfmat",
            json={"access_policy_sql": _policy_sql("selfmat"), "access_policy_note": "pii masking"},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text

        from src.repositories import table_registry_repo

        assert table_registry_repo().get("selfmat")["access_policy_sql"] is not None


@pytest.mark.journey
class TestBigQueryCrossRepresentationTwin:
    """Gap 2 — ``bq_fqn='project.dataset.table'`` on one row and
    ``bucket``+``source_table`` on the other are the same physical table,
    and their raw signals never intersect."""

    def _fqn_only_policied_row(self, c, token, *, name, fqn):
        """A policied row that carries ONLY ``bq_fqn`` as its pointer — its
        ``source_query`` (``SELECT 1``) deliberately references no table, so
        nothing but the fqn↔bucket/source_table match can fire."""
        resp = _register_bq(
            c,
            token,
            name=name,
            query_mode="materialized",
            source_query="SELECT 1",
            bq_fqn=fqn,
            server_only=True,
        )
        assert resp.status_code in (200, 201, 202), resp.text
        attach = c.put(
            f"/api/admin/registry/{name}",
            json={"access_policy_sql": _policy_sql(name), "access_policy_note": "pii masking"},
            headers=_auth(token),
        )
        assert attach.status_code == 200, attach.text
        return resp.json()["id"]

    def test_bucket_source_table_twin_of_a_policied_bq_fqn_row_is_rejected(
        self, seeded_app, monkeypatch, bq_instance, stub_bq_extractor
    ):
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        policied_id = self._fqn_only_policied_row(
            c, token, name="fqn_src", fqn="my-test-project.analytics.sales"
        )

        resp = _register_bq(c, token, name="fqn_twin", bucket="analytics", source_table="sales")
        assert resp.status_code == 422, resp.text
        assert "access_policy_physical_source_conflict" in resp.text
        assert policied_id in resp.text

        from src.repositories import table_registry_repo

        assert table_registry_repo().get("fqn_twin") is None

    def test_bq_fqn_twin_of_a_policied_bucket_row_is_rejected(
        self, seeded_app, monkeypatch, bq_instance, stub_bq_extractor
    ):
        """The mirror representation: the POLICIED row uses bucket +
        source_table, the twin arrives as a bq_fqn."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        policied_id = _bq_policied_remote_row(c, token, name="fqnrev_src", dataset="analytics", table="sales")

        resp = _register_bq(
            c,
            token,
            name="fqnrev_twin",
            query_mode="materialized",
            source_query="SELECT 1",
            bq_fqn="my-test-project.analytics.sales",
        )
        assert resp.status_code == 422, resp.text
        assert "access_policy_physical_source_conflict" in resp.text
        assert policied_id in resp.text

    def test_a_bq_fqn_in_a_different_project_does_not_conflict(
        self, seeded_app, monkeypatch, bq_instance, stub_bq_extractor
    ):
        """Same dataset + table, a DIFFERENT project — genuinely another
        physical table, and both projects are known, so no wildcard."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        _bq_policied_remote_row(c, token, name="fqnproj_src", dataset="analytics", table="sales")

        resp = _register_bq(
            c,
            token,
            name="fqnproj_twin",
            query_mode="materialized",
            source_query="SELECT 1",
            bq_fqn="other-test-project.analytics.sales",
        )
        assert resp.status_code in (200, 201, 202), resp.text


@pytest.mark.journey
class TestDatabricksDefaultCatalogTwin:
    """Gap 3 — ``bucket='sales'`` (schema in the configured default catalog)
    and ``bucket='main.sales'`` are one physical table; as raw strings they
    are two different ``bucket_table`` signals."""

    def _policied_dbx_row(self, c, token, *, name, bucket, source_table):
        resp = c.post(
            "/api/admin/register-table",
            json={
                "name": name,
                "source_type": "databricks",
                "query_mode": "remote",
                "bucket": bucket,
                "source_table": source_table,
            },
            headers=_auth(token),
        )
        assert resp.status_code in (200, 201, 202), resp.text
        attach = c.put(
            f"/api/admin/registry/{name}",
            json={"access_policy_sql": _policy_sql(name), "access_policy_note": "pii masking"},
            headers=_auth(token),
        )
        assert attach.status_code == 200, attach.text
        return resp.json()["id"]

    def test_bare_schema_twin_of_a_catalog_qualified_policied_row_is_rejected(
        self, seeded_app, monkeypatch, dbx_instance
    ):
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        policied_id = self._policied_dbx_row(c, token, name="dbx_src", bucket="main.sales", source_table="orders")

        resp = c.post(
            "/api/admin/register-table",
            json={
                "name": "dbx_twin",
                "source_type": "databricks",
                "query_mode": "remote",
                "bucket": "sales",
                "source_table": "orders",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "access_policy_physical_source_conflict" in resp.text
        assert policied_id in resp.text

        from src.repositories import table_registry_repo

        assert table_registry_repo().get("dbx_twin") is None

    def test_catalog_qualified_twin_of_a_bare_schema_policied_row_is_rejected(
        self, seeded_app, monkeypatch, dbx_instance
    ):
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        policied_id = self._policied_dbx_row(c, token, name="dbxrev_src", bucket="sales", source_table="orders")

        resp = c.post(
            "/api/admin/register-table",
            json={
                "name": "dbxrev_twin",
                "source_type": "databricks",
                "query_mode": "remote",
                "bucket": "main.sales",
                "source_table": "orders",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert policied_id in resp.text

    def test_a_materialized_row_reading_the_policied_databricks_table_is_rejected(
        self, seeded_app, monkeypatch, dbx_instance
    ):
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        policied_id = self._policied_dbx_row(c, token, name="dbxmat_src", bucket="sales", source_table="orders")

        resp = c.post(
            "/api/admin/register-table",
            json={
                "name": "dbxmat_twin",
                "source_type": "databricks",
                "query_mode": "materialized",
                "source_query": "SELECT o_date, SUM(amount) FROM `main`.`sales`.`orders` GROUP BY o_date",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert policied_id in resp.text

        from src.repositories import table_registry_repo

        assert table_registry_repo().get("dbxmat_twin") is None

    def test_a_different_schema_in_the_same_catalog_is_accepted(self, seeded_app, monkeypatch, dbx_instance):
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        self._policied_dbx_row(c, token, name="dbxneg_src", bucket="sales", source_table="orders")

        resp = c.post(
            "/api/admin/register-table",
            json={
                "name": "dbxneg_twin",
                "source_type": "databricks",
                "query_mode": "remote",
                "bucket": "main.finance",
                "source_table": "orders",
            },
            headers=_auth(token),
        )
        assert resp.status_code in (200, 201, 202), resp.text


@pytest.mark.journey
class TestUnparseableMaterializedSql:
    """Fail closed: a materialized ``source_query`` Agnes cannot parse could
    read anything, so beside a policied row of the same engine it is refused
    — with the parse failure named, so the admin knows which escape applies."""

    def test_unparseable_sql_next_to_a_policied_row_is_rejected(
        self, seeded_app, monkeypatch, bq_instance, stub_bq_extractor
    ):
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        policied_id = _bq_policied_remote_row(c, token, name="unparse_src", dataset="analytics", table="sales")

        resp = _register_bq(
            c,
            token,
            name="unparse_mat",
            query_mode="materialized",
            source_query="SELECT * FROM ((( not really sql",
        )
        assert resp.status_code == 422, resp.text
        assert "access_policy_physical_source_conflict" in resp.text
        assert policied_id in resp.text
        assert "could not be parsed" in resp.text

        from src.repositories import table_registry_repo

        assert table_registry_repo().get("unparse_mat") is None

    def test_unparseable_sql_with_no_policied_row_anywhere_is_accepted(
        self, seeded_app, monkeypatch, bq_instance, stub_bq_extractor
    ):
        """The fail-closed rule must not become a general SQL validator —
        with no policy on the instance there is nothing to route around."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        resp = _register_bq(
            c,
            token,
            name="unparse_free",
            query_mode="materialized",
            source_query="SELECT * FROM ((( not really sql",
        )
        assert resp.status_code in (200, 201, 202), resp.text

    def test_a_policied_row_with_unparseable_sql_does_not_block_unrelated_rows(
        self, seeded_app, monkeypatch, bq_instance, stub_bq_extractor
    ):
        """The unknown signal is emitted only for the UNPOLICIED side. A
        policied row whose own SQL is unparseable protects itself through its
        policy; it must not turn every later registration into a 422."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]

        created = _register_bq(
            c,
            token,
            name="unparse_policied",
            query_mode="materialized",
            source_query="SELECT * FROM ((( not really sql",
            server_only=True,
        )
        assert created.status_code in (200, 201, 202), created.text
        attach = c.put(
            "/api/admin/registry/unparse_policied",
            json={
                "access_policy_sql": _policy_sql("unparse_policied"),
                "access_policy_note": "pii masking",
            },
            headers=_auth(token),
        )
        assert attach.status_code == 200, attach.text

        resp = _register_bq(c, token, name="unrelated_bq", bucket="analytics", source_table="returns")
        assert resp.status_code in (200, 201, 202), resp.text


@pytest.mark.journey
class TestCanonicalSignalsLeaveUnrelatedRowsAlone:
    def test_a_keboola_row_is_untouched_by_a_policied_databricks_table(self, seeded_app, monkeypatch, dbx_instance):
        """Identifiers are engine-scoped: a Keboola bucket that happens to
        share a Databricks schema's name is not the same physical table."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        dbx = c.post(
            "/api/admin/register-table",
            json={
                "name": "xengine_dbx",
                "source_type": "databricks",
                "query_mode": "remote",
                "bucket": "sales",
                "source_table": "orders",
            },
            headers=_auth(token),
        )
        assert dbx.status_code in (200, 201, 202), dbx.text
        attach = c.put(
            "/api/admin/registry/xengine_dbx",
            json={"access_policy_sql": _policy_sql("xengine_dbx"), "access_policy_note": "pii masking"},
            headers=_auth(token),
        )
        assert attach.status_code == 200, attach.text

        resp = c.post(
            "/api/admin/register-table",
            json={
                "name": "xengine_kbc",
                "source_type": "keboola",
                "query_mode": "local",
                "bucket": "sales",
                "source_table": "orders",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 201, resp.text
