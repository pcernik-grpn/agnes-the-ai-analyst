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
