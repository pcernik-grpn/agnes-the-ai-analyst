"""Task 18 -- snapshot policy fingerprint (table access policies design doc
§3.4, §10.3; plan Task 18).

`agnes snapshot create` deliberately puts a policied table's rows on the
laptop, bypassing every live-read enforcement point the rest of this
feature built (Tasks 5-12) -- the parquet keeps answering from whatever
slice was current at fetch time even after the policy tightens. This closes
that gap: `/api/v2/scan` stamps a fingerprint of (policy SQL, caller's live
groups) onto the `X-Agnes-Policy-Fingerprint` response header;
`SnapshotMeta` stores it; `agnes pull` recomputes the CURRENT fingerprint
from the manifest's per-table `access_policy_fingerprint`
(`app/api/sync.py::_table_manifest_entry`) and withholds a mismatched
snapshot's view -- reusing the SAME `snapshot_views_blocked` mechanism
#1129 already built for a de-authorized or newly-`server_only` table.
"""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock

import pytest

POLICY_SQL = "SELECT * EXCLUDE (secret) FROM orders WHERE list_contains($user_groups, unit)"

TEAM_A_PRINCIPAL = {"id": "u_team_a", "email": "team-a@example.com"}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def policied_orders(seeded_app, mock_extract_factory, monkeypatch):
    """A ``server_only`` ``orders`` table carrying ``POLICY_SQL``, plus a
    non-policied ``line_items`` sibling -- both granted to ``u_team_a``
    (group ``TeamA``). Mirrors the fixture in
    tests/test_access_policy_disclosure.py."""
    from app.auth.jwt import create_access_token
    from src.db import get_system_db
    from src.orchestrator import SyncOrchestrator
    from src.repositories.table_registry import TableRegistryRepository
    from src.repositories.users import UserRepository
    from tests.conftest import grant_table_via_package

    monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "true")

    env = seeded_app["env"]
    mock_extract_factory(
        "keboola",
        [
            {
                "name": "orders",
                "data": [
                    {"id": "1", "unit": "TeamA", "secret": "s1", "amount": "100"},
                    {"id": "2", "unit": "TeamA", "secret": "s2", "amount": "150"},
                    {"id": "3", "unit": "TeamB", "secret": "s3", "amount": "300"},
                ],
            },
            {"name": "line_items", "data": [{"id": "1", "sku": "A1"}]},
        ],
    )
    SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

    conn = get_system_db()
    try:
        registry = TableRegistryRepository(conn)
        registry.register(id="orders", name="orders", source_type="keboola", query_mode="local", server_only=True)
        registry.set_access_policy("orders", sql=POLICY_SQL, note="unit filter", updated_by="admin")
        registry.register(id="line_items", name="line_items", source_type="keboola", query_mode="local")

        users = UserRepository(conn)
        users.create(id="u_team_a", email="team-a@example.com", name="Team A")

        grant_table_via_package(conn, "orders", "u_team_a", group_name="TeamA")
        grant_table_via_package(conn, "line_items", "u_team_a", group_name="TeamA")
    finally:
        conn.close()

    return {
        **seeded_app,
        "team_a_token": create_access_token("u_team_a", "team-a@example.com"),
    }


# ── policy_fingerprint(): the formula ────────────────────────────────────


class TestPolicyFingerprintFormula:
    def test_matches_the_documented_sha256_formula(self, policied_orders):
        from src.access_policy import policy_fingerprint

        fp = policy_fingerprint("orders", TEAM_A_PRINCIPAL)
        expected = hashlib.sha256(f"{POLICY_SQL}|{sorted(['TeamA'])!r}".encode()).hexdigest()
        assert fp == expected

    def test_none_for_a_table_with_no_policy(self, policied_orders):
        from src.access_policy import policy_fingerprint

        assert policy_fingerprint("line_items", TEAM_A_PRINCIPAL) is None

    def test_none_for_admin_bypass(self, policied_orders):
        from src.access_policy import policy_fingerprint

        admin = {"id": "admin1", "email": "admin@test.com"}
        assert policy_fingerprint("orders", admin) is None

    def test_changes_when_the_policy_sql_changes(self, policied_orders):
        from src.access_policy import policy_fingerprint
        from src.repositories import table_registry_repo

        before = policy_fingerprint("orders", TEAM_A_PRINCIPAL)

        table_registry_repo().set_access_policy(
            "orders", sql=POLICY_SQL + " AND 1=1", note="widened", updated_by="admin"
        )

        after = policy_fingerprint("orders", TEAM_A_PRINCIPAL)
        assert before != after

    def test_changes_when_the_callers_groups_change(self, policied_orders):
        from src.access_policy import policy_fingerprint
        from src.repositories import user_group_members_repo, user_groups_repo

        before = policy_fingerprint("orders", TEAM_A_PRINCIPAL)

        groups = user_groups_repo()
        extra = groups.get_by_name("Extra") or groups.create(name="Extra", description="t", created_by="test")
        user_group_members_repo().add_member("u_team_a", extra["id"], source="admin", added_by="test")

        after = policy_fingerprint("orders", TEAM_A_PRINCIPAL)
        assert before != after

    def test_unchanged_policy_and_groups_reproduce_the_same_fingerprint(self, policied_orders):
        from src.access_policy import policy_fingerprint

        assert policy_fingerprint("orders", TEAM_A_PRINCIPAL) == policy_fingerprint("orders", TEAM_A_PRINCIPAL)


# ── /api/v2/scan: X-Agnes-Policy-Fingerprint header ──────────────────────


class TestScanEndpointFingerprintHeader:
    def test_header_present_and_matches_policy_fingerprint(self, policied_orders):
        from src.access_policy import policy_fingerprint

        c = policied_orders["client"]
        r = c.post("/api/v2/scan", json={"table_id": "orders"}, headers=_auth(policied_orders["team_a_token"]))
        assert r.status_code == 200, r.text
        header = r.headers.get("x-agnes-policy-fingerprint")
        assert header is not None
        assert header == policy_fingerprint("orders", TEAM_A_PRINCIPAL)

    def test_header_absent_for_non_policied_table(self, policied_orders):
        c = policied_orders["client"]
        r = c.post("/api/v2/scan", json={"table_id": "line_items"}, headers=_auth(policied_orders["team_a_token"]))
        assert r.status_code == 200, r.text
        assert "x-agnes-policy-fingerprint" not in r.headers

    def test_header_absent_for_admin_bypass(self, policied_orders):
        c = policied_orders["client"]
        r = c.post("/api/v2/scan", json={"table_id": "orders"}, headers=_auth(policied_orders["admin_token"]))
        assert r.status_code == 200, r.text
        assert "x-agnes-policy-fingerprint" not in r.headers

    def test_policy_table_id_header_accompanies_the_fingerprint(self, policied_orders):
        """The fingerprint alone is useless to `agnes pull` for a
        `--from-query` snapshot: `SnapshotMeta.table_id` is the snapshot
        NAME there, never a registry id, so the manifest lookup can never
        resolve it. The scan therefore names WHICH policied table the
        fingerprint belongs to."""
        c = policied_orders["client"]
        r = c.post("/api/v2/scan", json={"table_id": "orders"}, headers=_auth(policied_orders["team_a_token"]))
        assert r.status_code == 200, r.text
        assert r.headers.get("x-agnes-policy-table-id") == "orders"

    def test_policy_table_id_header_absent_for_non_policied_table(self, policied_orders):
        c = policied_orders["client"]
        r = c.post("/api/v2/scan", json={"table_id": "line_items"}, headers=_auth(policied_orders["team_a_token"]))
        assert r.status_code == 200, r.text
        assert "x-agnes-policy-table-id" not in r.headers

    def test_policy_table_id_header_absent_for_admin_bypass(self, policied_orders):
        c = policied_orders["client"]
        r = c.post("/api/v2/scan", json={"table_id": "orders"}, headers=_auth(policied_orders["admin_token"]))
        assert r.status_code == 200, r.text
        assert "x-agnes-policy-table-id" not in r.headers


# ── manifest: access_policy_fingerprint per-table entry ──────────────────


class TestManifestPolicyFingerprint:
    def test_data_package_entry_carries_the_current_fingerprint(self, policied_orders):
        from src.access_policy import policy_fingerprint

        c = policied_orders["client"]
        r = c.get("/api/sync/manifest", headers=_auth(policied_orders["team_a_token"]))
        assert r.status_code == 200, r.text
        entries = {t["id"]: t for pkg in r.json()["data_packages"] for t in pkg["tables"]}
        assert entries["orders"]["access_policy_fingerprint"] == policy_fingerprint("orders", TEAM_A_PRINCIPAL)
        assert entries["line_items"]["access_policy_fingerprint"] is None

    def test_table_manifest_entry_is_none_without_a_principal(self):
        from app.api.sync import _table_manifest_entry

        entry = _table_manifest_entry(
            {"table_id": "orders", "hash": "h"},
            {"id": "orders", "name": "orders", "access_policy_sql": POLICY_SQL},
        )
        assert entry["access_policy_fingerprint"] is None

    def test_table_manifest_entry_is_none_for_admin_principal(self, policied_orders):
        from app.api.sync import _table_manifest_entry

        entry = _table_manifest_entry(
            {"table_id": "orders", "hash": "h"},
            {"id": "orders", "name": "orders", "access_policy_sql": POLICY_SQL},
            principal={"id": "admin1", "email": "admin@test.com"},
        )
        assert entry["access_policy_fingerprint"] is None

    def test_table_manifest_entry_is_none_for_a_table_with_no_policy(self, policied_orders):
        from app.api.sync import _table_manifest_entry

        entry = _table_manifest_entry(
            {"table_id": "line_items", "hash": "h"},
            {"id": "line_items", "name": "line_items"},
            principal=TEAM_A_PRINCIPAL,
        )
        assert entry["access_policy_fingerprint"] is None


# ── SnapshotMeta stores the fingerprint ───────────────────────────────────


def _minimal_meta(**overrides):
    from cli.snapshot_meta import SnapshotMeta

    fields = dict(
        name="x",
        table_id="orders",
        select=None,
        where=None,
        limit=None,
        order_by=None,
        fetched_at="2026-01-01T00:00:00+00:00",
        effective_as_of="2026-01-01T00:00:00+00:00",
        rows=1,
        bytes_local=1,
        estimated_scan_bytes_at_fetch=0,
        result_hash_md5="abc",
    )
    fields.update(overrides)
    return SnapshotMeta(**fields)


class TestSnapshotMetaPolicyFingerprintField:
    def test_field_defaults_to_none(self):
        assert _minimal_meta().policy_fingerprint is None

    def test_round_trips_through_write_and_read_meta(self, tmp_path):
        from cli.snapshot_meta import read_meta, write_meta

        snap_dir = tmp_path / "snapshots"
        write_meta(snap_dir, _minimal_meta(policy_fingerprint="deadbeef"))
        assert read_meta(snap_dir, "x").policy_fingerprint == "deadbeef"

    def test_a_legacy_meta_json_with_no_fingerprint_key_still_parses(self, tmp_path):
        """MUST stay the LAST field with a default -- a meta.json written
        before this feature existed has no `policy_fingerprint` key."""
        import json as json_lib

        snap_dir = tmp_path / "snapshots"
        snap_dir.mkdir(parents=True)
        legacy = {
            "name": "x",
            "table_id": "orders",
            "select": None,
            "where": None,
            "limit": None,
            "order_by": None,
            "fetched_at": "2026-01-01T00:00:00+00:00",
            "effective_as_of": "2026-01-01T00:00:00+00:00",
            "rows": 1,
            "bytes_local": 1,
            "estimated_scan_bytes_at_fetch": 0,
            "result_hash_md5": "abc",
        }
        (snap_dir / "x.meta.json").write_text(json_lib.dumps(legacy), encoding="utf-8")

        from cli.snapshot_meta import read_meta

        meta = read_meta(snap_dir, "x")
        assert meta.policy_fingerprint is None
        assert meta.policy_table_id is None


class TestSnapshotMetaPolicyTableIdField:
    def test_field_defaults_to_none(self):
        assert _minimal_meta().policy_table_id is None

    def test_round_trips_through_write_and_read_meta(self, tmp_path):
        from cli.snapshot_meta import read_meta, write_meta

        snap_dir = tmp_path / "snapshots"
        write_meta(snap_dir, _minimal_meta(policy_fingerprint="deadbeef", policy_table_id="orders"))
        assert read_meta(snap_dir, "x").policy_table_id == "orders"

    def test_a_meta_json_with_a_fingerprint_but_no_table_id_key_still_parses(self, tmp_path):
        """Same LAST-field-with-a-default contract as `policy_fingerprint`
        — a meta.json written between the fingerprint landing and this
        field existing carries the one key and not the other."""
        import json as json_lib

        snap_dir = tmp_path / "snapshots"
        snap_dir.mkdir(parents=True)
        older = {
            "name": "x",
            "table_id": "orders",
            "select": None,
            "where": None,
            "limit": None,
            "order_by": None,
            "fetched_at": "2026-01-01T00:00:00+00:00",
            "effective_as_of": "2026-01-01T00:00:00+00:00",
            "rows": 1,
            "bytes_local": 1,
            "estimated_scan_bytes_at_fetch": 0,
            "result_hash_md5": "abc",
            "expires_at": None,
            "policy_fingerprint": "fp1",
        }
        (snap_dir / "x.meta.json").write_text(json_lib.dumps(older), encoding="utf-8")

        from cli.snapshot_meta import read_meta

        meta = read_meta(snap_dir, "x")
        assert meta.policy_fingerprint == "fp1"
        assert meta.policy_table_id is None


# ── cli.v2_client.api_post_arrow_with_headers ─────────────────────────────


class TestApiPostArrowWithHeaders:
    def test_returns_table_and_headers(self):
        from unittest.mock import patch

        import pyarrow as pa

        from app.api.v2_arrow import arrow_table_to_ipc_bytes
        from cli.v2_client import api_post_arrow_with_headers

        ipc = arrow_table_to_ipc_bytes(pa.table({"x": [1, 2, 3]}))
        resp = MagicMock()
        resp.status_code = 200
        resp.content = ipc
        resp.headers = {
            "X-Agnes-Policy-Fingerprint": "deadbeef",
            "content-type": "application/vnd.apache.arrow.stream",
        }
        with patch("cli.v2_client.httpx.post", return_value=resp):
            table, headers = api_post_arrow_with_headers("/api/v2/scan", {"table_id": "orders"})
        assert table.num_rows == 3
        assert headers.get("X-Agnes-Policy-Fingerprint") == "deadbeef"


# ── `agnes snapshot create`/`refresh` persist the header ─────────────────


class _FakeConn:
    def execute(self, *a, **k):
        return self

    def close(self):
        pass


class TestCreateAndRefreshPersistTheFingerprint:
    def test_create_persists_the_response_header(self, tmp_path, monkeypatch):
        import pyarrow as pa
        from cli.commands import snapshot as snap_mod

        monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
        db = tmp_path / "user" / "duckdb" / "analytics.duckdb"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"")

        table = pa.table({"a": [1, 2, 3]})
        monkeypatch.setattr(
            snap_mod,
            "api_post_arrow_with_headers",
            lambda *a, **k: (table, {"X-Agnes-Policy-Fingerprint": "fp123"}),
        )
        monkeypatch.setattr(snap_mod, "api_post_json", lambda *a, **k: {"estimated_scan_bytes": 0})
        monkeypatch.setattr(snap_mod, "_open_duckdb", lambda *a, **k: _FakeConn())

        snap_mod.create_cmd(
            table_id="orders",
            select=None,
            where=None,
            limit=None,
            order_by=None,
            as_name="orders_snap",
            estimate=False,
            no_estimate=True,
            force=False,
            from_query=None,
            ttl=None,
        )

        from cli.snapshot_meta import read_meta

        meta = read_meta(tmp_path / "user" / "snapshots", "orders_snap")
        assert meta is not None
        assert meta.policy_fingerprint == "fp123"

    def test_a_from_query_create_records_the_policied_table_id(self, tmp_path, monkeypatch):
        """The end-to-end shape the permanent-block bug lived in: with
        ``--from-query`` the positional argument is the snapshot NAME, so
        ``table_id`` is useless as a manifest key and only the recorded
        ``policy_table_id`` lets ``agnes pull`` resolve the source table."""
        import pyarrow as pa
        from cli.commands import snapshot as snap_mod

        monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
        db = tmp_path / "user" / "duckdb" / "analytics.duckdb"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"")

        table = pa.table({"a": [1, 2, 3]})
        monkeypatch.setattr(
            snap_mod,
            "api_post_arrow_with_headers",
            lambda *a, **k: (
                table,
                {"X-Agnes-Policy-Fingerprint": "fp123", "X-Agnes-Policy-Table-Id": "orders"},
            ),
        )
        monkeypatch.setattr(snap_mod, "_open_duckdb", lambda *a, **k: _FakeConn())

        snap_mod.create_cmd(
            table_id="q_snap",
            select=None,
            where=None,
            limit=None,
            order_by=None,
            as_name=None,
            estimate=False,
            no_estimate=False,
            force=False,
            from_query="SELECT * FROM orders",
            ttl=None,
        )

        from cli.snapshot_meta import read_meta

        meta = read_meta(tmp_path / "user" / "snapshots", "q_snap")
        assert meta is not None
        assert meta.table_id == "q_snap", "the --from-query positional really is the snapshot name"
        assert meta.policy_fingerprint == "fp123"
        assert meta.policy_table_id == "orders"

        # …and that recorded id is what makes the pull-side comparison
        # resolve at all, instead of blocking the view forever.
        from cli.lib.pull import _stale_policy_snapshot_names

        manifest = {
            "data_packages": [{"tables": [{"id": "orders", "name": "orders", "access_policy_fingerprint": "fp123"}]}]
        }
        assert _stale_policy_snapshot_names(tmp_path, manifest) == set()

    def test_refresh_re_stamps_the_policied_table_id(self, tmp_path, monkeypatch):
        import pyarrow as pa
        from cli.commands import snapshot as snap_mod
        from cli.snapshot_meta import read_meta, write_meta

        monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
        snap_dir = tmp_path / "user" / "snapshots"
        write_meta(snap_dir, _minimal_meta(name="orders_snap", table_id="orders", policy_fingerprint="old"))

        table = pa.table({"a": [1]})
        monkeypatch.setattr(
            snap_mod,
            "api_post_arrow_with_headers",
            lambda *a, **k: (
                table,
                {"X-Agnes-Policy-Fingerprint": "new", "X-Agnes-Policy-Table-Id": "orders"},
            ),
        )

        snap_mod.refresh_cmd(name="orders_snap", where=None, ttl=None)

        meta = read_meta(snap_dir, "orders_snap")
        assert meta.policy_fingerprint == "new"
        assert meta.policy_table_id == "orders"

    def test_create_leaves_fingerprint_none_when_header_absent(self, tmp_path, monkeypatch):
        import pyarrow as pa
        from cli.commands import snapshot as snap_mod

        monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
        db = tmp_path / "user" / "duckdb" / "analytics.duckdb"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"")

        table = pa.table({"a": [1]})
        monkeypatch.setattr(snap_mod, "api_post_arrow_with_headers", lambda *a, **k: (table, {}))
        monkeypatch.setattr(snap_mod, "api_post_json", lambda *a, **k: {"estimated_scan_bytes": 0})
        monkeypatch.setattr(snap_mod, "_open_duckdb", lambda *a, **k: _FakeConn())

        snap_mod.create_cmd(
            table_id="line_items",
            select=None,
            where=None,
            limit=None,
            order_by=None,
            as_name="plain_snap",
            estimate=False,
            no_estimate=True,
            force=False,
            from_query=None,
            ttl=None,
        )

        from cli.snapshot_meta import read_meta

        meta = read_meta(tmp_path / "user" / "snapshots", "plain_snap")
        assert meta is not None
        assert meta.policy_fingerprint is None

    def test_refresh_re_stamps_the_fingerprint(self, tmp_path, monkeypatch):
        """A refresh after the policy changed must persist the NEW
        fingerprint -- not keep the stale value from the original create --
        otherwise `agnes pull` would go on blocking a view that just fetched
        current, correctly-filtered rows."""
        import pyarrow as pa
        from cli.commands import snapshot as snap_mod
        from cli.snapshot_meta import read_meta, write_meta

        monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
        snap_dir = tmp_path / "user" / "snapshots"
        snap_dir.mkdir(parents=True)
        parquet = snap_dir / "orders_snap.parquet"
        parquet.write_bytes(b"PAR1\x00\x00PAR1")
        write_meta(
            snap_dir,
            _minimal_meta(name="orders_snap", table_id="orders", policy_fingerprint="OLD_FP"),
        )

        table = pa.table({"a": [1, 2]})
        monkeypatch.setattr(
            snap_mod,
            "api_post_arrow_with_headers",
            lambda *a, **k: (table, {"X-Agnes-Policy-Fingerprint": "NEW_FP"}),
        )

        snap_mod.refresh_cmd(name="orders_snap", where=None, ttl=None)

        meta = read_meta(snap_dir, "orders_snap")
        assert meta.policy_fingerprint == "NEW_FP"


# ── `agnes snapshot create`/`refresh` disclose `X-Agnes-Row-Scope` (§10) ──
#
# `/api/v2/scan` has no JSON body to carry `row_scope` in, so it ships the
# same envelope as the `X-Agnes-Row-Scope` response header instead
# (`src/access_policy.py::row_scope_payload`, `app/api/v2_scan.py`). Before
# this fix, `cli/commands/snapshot.py` read the sibling
# `X-Agnes-Policy-Fingerprint`/`X-Agnes-Policy-Table-Id` headers but never
# this one, so an analyst who materializes a filtered slice got no `[scope]`
# note at all -- the same disclosure gap `TestCliQueryRowScopeStderr`
# (tests/test_access_policy_disclosure.py) pins for `agnes query`.


def _row_scope_header(table_id="orders"):
    import json as json_lib

    return json_lib.dumps(
        {
            "policied_tables": [table_id],
            "note": f"rows in '{table_id}' are filtered by an access policy — this is your slice, not the whole table",
        }
    )


class TestCreateAndRefreshDiscloseRowScope:
    def test_create_prints_scope_note_to_stderr(self, tmp_path, monkeypatch, capsys):
        import pyarrow as pa
        from cli.commands import snapshot as snap_mod

        monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
        db = tmp_path / "user" / "duckdb" / "analytics.duckdb"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"")

        table = pa.table({"a": [1, 2, 3]})
        monkeypatch.setattr(
            snap_mod,
            "api_post_arrow_with_headers",
            lambda *a, **k: (table, {"X-Agnes-Row-Scope": _row_scope_header()}),
        )
        monkeypatch.setattr(snap_mod, "api_post_json", lambda *a, **k: {"estimated_scan_bytes": 0})
        monkeypatch.setattr(snap_mod, "_open_duckdb", lambda *a, **k: _FakeConn())

        snap_mod.create_cmd(
            table_id="orders",
            select=None,
            where=None,
            limit=None,
            order_by=None,
            as_name="orders_snap",
            estimate=False,
            no_estimate=True,
            force=False,
            from_query=None,
            ttl=None,
        )

        captured = capsys.readouterr()
        assert "[scope]" in captured.err
        assert "rows in 'orders' are filtered by an access policy" in captured.err
        assert "[scope]" not in captured.out

    def test_create_no_scope_note_when_header_absent(self, tmp_path, monkeypatch, capsys):
        import pyarrow as pa
        from cli.commands import snapshot as snap_mod

        monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
        db = tmp_path / "user" / "duckdb" / "analytics.duckdb"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"")

        table = pa.table({"a": [1]})
        monkeypatch.setattr(snap_mod, "api_post_arrow_with_headers", lambda *a, **k: (table, {}))
        monkeypatch.setattr(snap_mod, "api_post_json", lambda *a, **k: {"estimated_scan_bytes": 0})
        monkeypatch.setattr(snap_mod, "_open_duckdb", lambda *a, **k: _FakeConn())

        snap_mod.create_cmd(
            table_id="line_items",
            select=None,
            where=None,
            limit=None,
            order_by=None,
            as_name="plain_snap",
            estimate=False,
            no_estimate=True,
            force=False,
            from_query=None,
            ttl=None,
        )

        captured = capsys.readouterr()
        assert "[scope]" not in captured.err

    def test_create_malformed_header_does_not_crash(self, tmp_path, monkeypatch, capsys):
        """A disclosure header must never crash a snapshot fetch -- a
        malformed `X-Agnes-Row-Scope` is a silent no-op, same as an absent
        one."""
        import pyarrow as pa
        from cli.commands import snapshot as snap_mod

        monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
        db = tmp_path / "user" / "duckdb" / "analytics.duckdb"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"")

        table = pa.table({"a": [1]})
        monkeypatch.setattr(
            snap_mod,
            "api_post_arrow_with_headers",
            lambda *a, **k: (table, {"X-Agnes-Row-Scope": "{not valid json"}),
        )
        monkeypatch.setattr(snap_mod, "api_post_json", lambda *a, **k: {"estimated_scan_bytes": 0})
        monkeypatch.setattr(snap_mod, "_open_duckdb", lambda *a, **k: _FakeConn())

        snap_mod.create_cmd(
            table_id="line_items",
            select=None,
            where=None,
            limit=None,
            order_by=None,
            as_name="malformed_snap",
            estimate=False,
            no_estimate=True,
            force=False,
            from_query=None,
            ttl=None,
        )

        captured = capsys.readouterr()
        assert "[scope]" not in captured.err
        from cli.snapshot_meta import read_meta

        assert read_meta(tmp_path / "user" / "snapshots", "malformed_snap") is not None

    def test_from_query_create_prints_scope_note(self, tmp_path, monkeypatch, capsys):
        """The `--from-query` path (also used by `agnes query --remote
        --auto-snapshot`) funnels through the same `_create_snapshot` fetch,
        so it must disclose too."""
        import pyarrow as pa
        from cli.commands import snapshot as snap_mod

        monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
        db = tmp_path / "user" / "duckdb" / "analytics.duckdb"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"")

        table = pa.table({"a": [1, 2, 3]})
        monkeypatch.setattr(
            snap_mod,
            "api_post_arrow_with_headers",
            lambda *a, **k: (
                table,
                {
                    "X-Agnes-Row-Scope": _row_scope_header(),
                    "X-Agnes-Policy-Table-Id": "orders",
                },
            ),
        )
        monkeypatch.setattr(snap_mod, "_open_duckdb", lambda *a, **k: _FakeConn())

        snap_mod.create_cmd(
            table_id="q_snap",
            select=None,
            where=None,
            limit=None,
            order_by=None,
            as_name=None,
            estimate=False,
            no_estimate=False,
            force=False,
            from_query="SELECT * FROM orders",
            ttl=None,
        )

        captured = capsys.readouterr()
        assert "[scope]" in captured.err
        assert "[scope]" not in captured.out

    def test_refresh_prints_scope_note_to_stderr(self, tmp_path, monkeypatch, capsys):
        import pyarrow as pa
        from cli.commands import snapshot as snap_mod
        from cli.snapshot_meta import write_meta

        monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
        snap_dir = tmp_path / "user" / "snapshots"
        write_meta(snap_dir, _minimal_meta(name="orders_snap", table_id="orders"))

        table = pa.table({"a": [1, 2]})
        monkeypatch.setattr(
            snap_mod,
            "api_post_arrow_with_headers",
            lambda *a, **k: (table, {"X-Agnes-Row-Scope": _row_scope_header()}),
        )

        snap_mod.refresh_cmd(name="orders_snap", where=None, ttl=None)

        captured = capsys.readouterr()
        assert "[scope]" in captured.err
        assert "rows in 'orders' are filtered by an access policy" in captured.err
        assert "[scope]" not in captured.out

    def test_refresh_no_scope_note_when_header_absent(self, tmp_path, monkeypatch, capsys):
        import pyarrow as pa
        from cli.commands import snapshot as snap_mod
        from cli.snapshot_meta import write_meta

        monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
        snap_dir = tmp_path / "user" / "snapshots"
        write_meta(snap_dir, _minimal_meta(name="orders_snap", table_id="orders"))

        table = pa.table({"a": [1, 2]})
        monkeypatch.setattr(snap_mod, "api_post_arrow_with_headers", lambda *a, **k: (table, {}))

        snap_mod.refresh_cmd(name="orders_snap", where=None, ttl=None)

        captured = capsys.readouterr()
        assert "[scope]" not in captured.err

    def test_refresh_malformed_header_does_not_crash(self, tmp_path, monkeypatch, capsys):
        import pyarrow as pa
        from cli.commands import snapshot as snap_mod
        from cli.snapshot_meta import read_meta, write_meta

        monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
        snap_dir = tmp_path / "user" / "snapshots"
        write_meta(snap_dir, _minimal_meta(name="orders_snap", table_id="orders"))

        table = pa.table({"a": [1, 2]})
        monkeypatch.setattr(
            snap_mod,
            "api_post_arrow_with_headers",
            lambda *a, **k: (table, {"X-Agnes-Row-Scope": "not json at all"}),
        )

        snap_mod.refresh_cmd(name="orders_snap", where=None, ttl=None)

        captured = capsys.readouterr()
        assert "[scope]" not in captured.err
        assert read_meta(snap_dir, "orders_snap") is not None


# ── agnes pull: block a snapshot whose fingerprint went stale ────────────


def _analytics_conn(workspace):
    import duckdb

    return duckdb.connect(str(workspace / "user" / "duckdb" / "analytics.duckdb"))


class TestPullBlocksStaleSnapshotFingerprint:
    @pytest.fixture(autouse=True)
    def _isolate_config_dir(self, tmp_path, monkeypatch):
        cfg = tmp_path / "_agnes_cfg"
        cfg.mkdir()
        monkeypatch.setenv("AGNES_CONFIG_DIR", str(cfg))

    @staticmethod
    def _manifest(fingerprint):
        return {
            "tables": {},
            "data_packages": [
                {
                    "id": "pkg_1",
                    "slug": "pkg-1",
                    "name": "Pkg",
                    "tables": [
                        {
                            "id": "orders",
                            "name": "orders",
                            "access_policy": True,
                            "access_policy_fingerprint": fingerprint,
                            "hash": "",
                            "query_mode": "local",
                        }
                    ],
                    "total_size_bytes": 0,
                }
            ],
        }

    @staticmethod
    def _stub_server(monkeypatch, manifest):
        def _api_get(path, *args, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status = lambda: None
            if path == "/api/sync/manifest":
                resp.json.return_value = manifest
            elif path == "/api/memory/bundle":
                resp.json.return_value = {"mandatory": [], "approved": []}
            else:
                resp.json.return_value = {}
            return resp

        def _stream_download(path, target_path, progress_callback=None):
            raise AssertionError(f"unexpected stream_download({path!r}) in a snapshot-fingerprint-only pull test")

        monkeypatch.setattr("cli.lib.pull.api_get", _api_get, raising=False)
        monkeypatch.setattr("cli.lib.pull.stream_download", _stream_download, raising=False)

    @staticmethod
    def _seed_snapshot(workspace, name, table_id, policy_fingerprint):
        import duckdb

        from cli.snapshot_meta import SnapshotMeta, write_meta

        snap_dir = workspace / "user" / "snapshots"
        snap_dir.mkdir(parents=True, exist_ok=True)
        parquet = snap_dir / f"{name}.parquet"
        c = duckdb.connect()
        try:
            c.execute(f"COPY (SELECT 1 AS id) TO '{parquet}' (FORMAT PARQUET)")
        finally:
            c.close()
        write_meta(
            snap_dir,
            SnapshotMeta(
                name=name,
                table_id=table_id,
                select=None,
                where=None,
                limit=None,
                order_by=None,
                fetched_at="2026-01-01T00:00:00+00:00",
                effective_as_of="2026-01-01T00:00:00+00:00",
                rows=1,
                bytes_local=parquet.stat().st_size,
                estimated_scan_bytes_at_fetch=0,
                result_hash_md5="x",
                policy_fingerprint=policy_fingerprint,
            ),
        )

    def test_mismatched_fingerprint_blocks_the_view(self, tmp_path, monkeypatch):
        from cli.lib.pull import run_pull

        self._seed_snapshot(tmp_path, "orders", "orders", "OLD_FP")
        self._stub_server(monkeypatch, self._manifest("NEW_FP"))

        result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

        assert "orders" in result.snapshot_views_blocked
        conn = _analytics_conn(tmp_path)
        try:
            names = {r[0] for r in conn.execute("SELECT table_name FROM information_schema.tables").fetchall()}
            assert "orders" not in names
        finally:
            conn.close()
        # Not a recall (spec §3.3) -- the parquet stays on disk, reachable
        # by its own path even though the bare name no longer resolves.
        assert (tmp_path / "user" / "snapshots" / "orders.parquet").exists()

    def test_matching_fingerprint_keeps_the_view_resolvable(self, tmp_path, monkeypatch):
        from cli.lib.pull import run_pull

        self._seed_snapshot(tmp_path, "orders", "orders", "SAME_FP")
        self._stub_server(monkeypatch, self._manifest("SAME_FP"))

        result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

        assert "orders" not in result.snapshot_views_blocked
        conn = _analytics_conn(tmp_path)
        try:
            assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        finally:
            conn.close()

    def test_a_custom_as_name_is_blocked_by_its_own_view_name(self, tmp_path, monkeypatch):
        """`--as cz_recent` names the VIEW `cz_recent`, not `orders` -- the
        de-auth mechanism (`_blocked_snapshot_names`) only ever withholds
        the bare table id, so this is the case ONLY the fingerprint check
        covers."""
        from cli.lib.pull import run_pull

        self._seed_snapshot(tmp_path, "cz_recent", "orders", "OLD_FP")
        self._stub_server(monkeypatch, self._manifest("NEW_FP"))

        result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

        assert "cz_recent" in result.snapshot_views_blocked

    def test_a_policy_newly_attached_after_the_fetch_also_goes_stale(self, tmp_path, monkeypatch):
        """The snapshot was fetched before ANY policy existed
        (`policy_fingerprint=None`); the manifest now reports one -- None
        != a real fingerprint, so this also blocks."""
        from cli.lib.pull import run_pull

        self._seed_snapshot(tmp_path, "orders", "orders", None)
        self._stub_server(monkeypatch, self._manifest("NEW_FP"))

        result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

        assert "orders" in result.snapshot_views_blocked

    def test_no_policy_before_or_now_stays_resolvable(self, tmp_path, monkeypatch):
        from cli.lib.pull import run_pull

        self._seed_snapshot(tmp_path, "orders", "orders", None)
        self._stub_server(monkeypatch, self._manifest(None))

        result = run_pull(server_url="http://x", token="t", workspace=tmp_path)

        assert "orders" not in result.snapshot_views_blocked


# ── _stale_policy_snapshot_names / _manifest_policy_fingerprints direct ──


class TestStalePolicySnapshotNamesDirect:
    def test_matching_fingerprint_is_not_stale(self, tmp_path):
        from cli.lib.pull import _stale_policy_snapshot_names
        from cli.snapshot_meta import write_meta

        snap_dir = tmp_path / "user" / "snapshots"
        write_meta(snap_dir, _minimal_meta(name="orders", table_id="orders", policy_fingerprint="fp1"))
        manifest = {
            "data_packages": [{"tables": [{"id": "orders", "name": "orders", "access_policy_fingerprint": "fp1"}]}]
        }

        assert _stale_policy_snapshot_names(tmp_path, manifest) == set()

    def test_mismatched_fingerprint_is_stale(self, tmp_path):
        from cli.lib.pull import _stale_policy_snapshot_names
        from cli.snapshot_meta import write_meta

        snap_dir = tmp_path / "user" / "snapshots"
        write_meta(snap_dir, _minimal_meta(name="orders", table_id="orders", policy_fingerprint="fp1"))
        manifest = {
            "data_packages": [{"tables": [{"id": "orders", "name": "orders", "access_policy_fingerprint": "fp2"}]}]
        }

        assert _stale_policy_snapshot_names(tmp_path, manifest) == {"orders"}

    def test_a_table_id_referenced_by_name_still_resolves(self, tmp_path):
        """`SnapshotMeta.table_id` can be either the registry id or its name
        (`_resolve_table_row`'s own id-or-name fallback) -- the manifest map
        must answer both."""
        from cli.lib.pull import _stale_policy_snapshot_names
        from cli.snapshot_meta import write_meta

        snap_dir = tmp_path / "user" / "snapshots"
        write_meta(
            snap_dir,
            _minimal_meta(name="orders", table_id="Orders Display Name", policy_fingerprint="fp1"),
        )
        manifest = {
            "data_packages": [
                {"tables": [{"id": "orders", "name": "Orders Display Name", "access_policy_fingerprint": "fp1"}]}
            ]
        }

        assert _stale_policy_snapshot_names(tmp_path, manifest) == set()

    def test_missing_snapshots_dir_returns_empty(self, tmp_path):
        from cli.lib.pull import _stale_policy_snapshot_names

        assert _stale_policy_snapshot_names(tmp_path, {}) == set()


class TestStalenessNeedsAResolvableSourceTable:
    """A `--from-query` snapshot (every `agnes query --remote
    --auto-snapshot`) stores the snapshot NAME the caller passed
    positionally in `SnapshotMeta.table_id`, not a registry id — the
    manifest map is keyed only on `data_packages[].tables[].id`/`.name`,
    so that lookup can never resolve. `/api/v2/scan` DID stamp a real
    fingerprint, so a naive `stored != manifest.get(table_id)` compares a
    hash against `None` on EVERY pull: the snapshot is withheld
    permanently, with no recovery. "Unknown" must not mean "stale".
    """

    def test_a_from_query_snapshot_naming_its_policied_table_is_compared(self, tmp_path):
        """The fix's happy path: the scan reported WHICH policied table the
        fingerprint belongs to, so the comparison resolves and stays
        fail-closed."""
        from cli.lib.pull import _stale_policy_snapshot_names
        from cli.snapshot_meta import write_meta

        snap_dir = tmp_path / "user" / "snapshots"
        write_meta(
            snap_dir,
            _minimal_meta(
                name="q_snap",
                table_id="q_snap",  # --from-query: the snapshot name, not a registry id
                policy_fingerprint="fp1",
                policy_table_id="orders",
            ),
        )
        manifest = {
            "data_packages": [{"tables": [{"id": "orders", "name": "orders", "access_policy_fingerprint": "fp1"}]}]
        }

        assert _stale_policy_snapshot_names(tmp_path, manifest) == set()

    def test_a_from_query_snapshot_goes_stale_when_its_policied_table_changes(self, tmp_path):
        """Resolvable => fail-closed, exactly like a table_id snapshot."""
        from cli.lib.pull import _stale_policy_snapshot_names
        from cli.snapshot_meta import write_meta

        snap_dir = tmp_path / "user" / "snapshots"
        write_meta(
            snap_dir,
            _minimal_meta(
                name="q_snap",
                table_id="q_snap",
                policy_fingerprint="fp1",
                policy_table_id="orders",
            ),
        )
        manifest = {
            "data_packages": [{"tables": [{"id": "orders", "name": "orders", "access_policy_fingerprint": "fp2"}]}]
        }

        assert _stale_policy_snapshot_names(tmp_path, manifest) == {"q_snap"}

    def test_an_unresolvable_source_table_is_not_treated_as_stale(self, tmp_path):
        """The permanent-block bug itself: a snapshot whose recorded source
        table matches no manifest entry at all (a `--from-query` snapshot
        written before `policy_table_id` existed) must be left resolvable,
        not withheld forever."""
        from cli.lib.pull import _stale_policy_snapshot_names
        from cli.snapshot_meta import write_meta

        snap_dir = tmp_path / "user" / "snapshots"
        write_meta(snap_dir, _minimal_meta(name="q_snap", table_id="q_snap", policy_fingerprint="fp1"))
        manifest = {
            "data_packages": [{"tables": [{"id": "orders", "name": "orders", "access_policy_fingerprint": "fp1"}]}]
        }

        assert _stale_policy_snapshot_names(tmp_path, manifest) == set()

    def test_a_resolvable_table_with_no_current_fingerprint_still_goes_stale(self, tmp_path):
        """ "Unknown is not stale" must not weaken the resolvable case: a
        table that IS in the manifest and now reports no fingerprint (the
        policy was detached, or the caller's groups changed such that the
        server no longer stamps one) still invalidates a snapshot taken
        under a policy."""
        from cli.lib.pull import _stale_policy_snapshot_names
        from cli.snapshot_meta import write_meta

        snap_dir = tmp_path / "user" / "snapshots"
        write_meta(snap_dir, _minimal_meta(name="orders", table_id="orders", policy_fingerprint="fp1"))
        manifest = {
            "data_packages": [{"tables": [{"id": "orders", "name": "orders", "access_policy_fingerprint": None}]}]
        }

        assert _stale_policy_snapshot_names(tmp_path, manifest) == {"orders"}

    def test_a_newly_attached_policy_still_goes_stale_for_a_resolvable_table(self, tmp_path):
        """The other pre-existing direction: no fingerprint stored, one
        reported now."""
        from cli.lib.pull import _stale_policy_snapshot_names
        from cli.snapshot_meta import write_meta

        snap_dir = tmp_path / "user" / "snapshots"
        write_meta(snap_dir, _minimal_meta(name="orders", table_id="orders", policy_fingerprint=None))
        manifest = {
            "data_packages": [{"tables": [{"id": "orders", "name": "orders", "access_policy_fingerprint": "fp1"}]}]
        }

        assert _stale_policy_snapshot_names(tmp_path, manifest) == {"orders"}

    def test_policy_table_id_wins_over_a_colliding_snapshot_name(self, tmp_path):
        """A `--from-query` snapshot may be named after some OTHER table
        that happens to be in the manifest. The recorded policied table id
        is authoritative — comparing against the coincidental name-match
        would measure the wrong table's policy."""
        from cli.lib.pull import _stale_policy_snapshot_names
        from cli.snapshot_meta import write_meta

        snap_dir = tmp_path / "user" / "snapshots"
        write_meta(
            snap_dir,
            _minimal_meta(
                name="line_items",
                table_id="line_items",
                policy_fingerprint="fp1",
                policy_table_id="orders",
            ),
        )
        manifest = {
            "data_packages": [
                {
                    "tables": [
                        {"id": "orders", "name": "orders", "access_policy_fingerprint": "fp2"},
                        {"id": "line_items", "name": "line_items", "access_policy_fingerprint": "fp1"},
                    ]
                }
            ]
        }

        assert _stale_policy_snapshot_names(tmp_path, manifest) == {"line_items"}
