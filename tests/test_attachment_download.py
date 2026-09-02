"""GET /api/attachments/{source}/{attachment_id}/download — the generic
connector-catalogued attachment download surface (Jira first).

Covers the acceptance list: RBAC 403 via the catalogue table, byte-for-byte
success, misses distinguishable from denials (`attachment_not_found` vs
`attachment_not_stored` vs 403), unknown source 404, tampered-path
containment, audit rows for granted AND denied fetches, and a second source
registering with no route change.
"""

from pathlib import Path

import pytest

from src.attachment_sources import _SOURCES, AttachmentSource
from src.db import get_system_db
from tests.conftest import create_mock_extract


def _grant_table(user_id: str, table_id: str, group_name: str) -> None:
    from tests.conftest import grant_table_via_package

    conn = get_system_db()
    try:
        grant_table_via_package(conn, table_id, user_id, group_name=group_name)
    finally:
        conn.close()


def _last_audit_row():
    conn = get_system_db()
    try:
        return conn.execute(
            "SELECT user_id, action, resource, result, params FROM audit_log "
            "WHERE action='attachment.download' ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()


@pytest.fixture
def jira_attachment_env(seeded_app, monkeypatch):
    """A registered + extracted `attachments` catalogue, a real file on
    disk under the permitted root, and Config.JIRA_DATA_DIR repointed at it.

    Rows: id 101 → stored file, id 102 → empty local_path (transform-time
    miss), id 103 → file since removed, id 104 → tampered path escaping the
    root (the target file EXISTS, so only containment can refuse it).
    """
    from connectors.jira.service import Config

    env = seeded_app["env"]
    c = seeded_app["client"]
    jira_data_dir = env["data_dir"] / "src_data" / "raw" / "jira"
    root = jira_data_dir / "attachments"
    (root / "SUP-1").mkdir(parents=True)
    monkeypatch.setattr(Config, "JIRA_DATA_DIR", jira_data_dir)

    stored = root / "SUP-1" / "101_report.pdf"
    payload = b"%PDF-1.4 attachment bytes for the roundtrip test"
    stored.write_bytes(payload)

    secret = env["data_dir"] / "secret.txt"
    secret.write_bytes(b"never served")

    create_mock_extract(
        env["extracts_dir"],
        "jira",
        [
            {
                "name": "attachments",
                "data": [
                    {
                        "attachment_id": "101",
                        "issue_key": "SUP-1",
                        "filename": "quarterly report.pdf",
                        "local_path": str(stored),
                    },
                    {
                        "attachment_id": "102",
                        "issue_key": "SUP-1",
                        "filename": "huge.zip",
                        "local_path": "",
                    },
                    {
                        "attachment_id": "103",
                        "issue_key": "SUP-1",
                        "filename": "gone.png",
                        "local_path": str(root / "SUP-1" / "103_gone.png"),
                    },
                    {
                        "attachment_id": "104",
                        "issue_key": "SUP-1",
                        "filename": "secret.txt",
                        "local_path": str(secret),
                    },
                ],
            }
        ],
    )
    from src.orchestrator import SyncOrchestrator

    SyncOrchestrator().rebuild()

    resp = c.post(
        "/api/admin/register-table",
        json={"name": "attachments", "source_type": "jira", "query_mode": "local"},
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
    )
    assert resp.status_code == 201

    return {**seeded_app, "payload": payload, "root": root}


def test_unknown_source_is_404(seeded_app, analyst_user):
    resp = seeded_app["client"].get("/api/attachments/nope/1/download", headers=analyst_user)
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "unknown_attachment_source"


def test_denied_without_table_access_and_audited(jira_attachment_env, analyst_user):
    resp = jira_attachment_env["client"].get("/api/attachments/jira/101/download", headers=analyst_user)
    assert resp.status_code == 403
    # The refusal is the catalogue table's RBAC message — a client can tell
    # this apart from every 404 miss below.
    assert "attachments" in str(resp.json()["detail"])
    row = _last_audit_row()
    assert row is not None
    assert row[0] == "analyst1"
    # `denied`, not an error spelling: the audit read layer
    # (src/audit_helpers.py) buckets `error%` as malfunctions, and an RBAC
    # refusal is correct policy — it must not inflate the error rate.
    assert row[3] == "denied"


def test_granted_fetch_streams_the_same_bytes(jira_attachment_env, analyst_user):
    _grant_table("analyst1", "attachments", "att-ok")
    resp = jira_attachment_env["client"].get("/api/attachments/jira/101/download", headers=analyst_user)
    assert resp.status_code == 200
    assert resp.content == jira_attachment_env["payload"]
    # The advertised length must equal the body actually delivered (it comes
    # from fstat of the descriptor being streamed).
    assert resp.headers["content-length"] == str(len(jira_attachment_env["payload"]))
    cd = resp.headers.get("content-disposition", "")
    # The catalogue's `filename` (what Jira shows), not the on-disk
    # "<id>_<name>" — RFC 5987-encoded because of the space.
    assert "quarterly%20report.pdf" in cd
    assert "101_" not in cd
    row = _last_audit_row()
    assert row[0] == "analyst1"
    assert row[3] == "success"
    assert str(len(jira_attachment_env["payload"])) in (row[4] or "")


def test_growing_file_never_overruns_advertised_length(jira_attachment_env, analyst_user, monkeypatch):
    """The stream is bounded at the fstat'd size: a writer growing the file
    in place after the endpoint opened it (a non-atomic writer's append, or
    any partial write racing the read) must not push the body past the
    advertised Content-Length — that mismatch is an ASGI protocol error on a
    real server, not a truncated-but-valid response."""
    import app.api.attachments as attachments_mod

    _grant_table("analyst1", "attachments", "att-grow")
    real_open = attachments_mod._open_contained

    def open_then_grow(root, stored):
        fh, st, reason = real_open(root, stored)
        if fh is not None:
            # In-place growth of the very inode being streamed, after
            # open+fstat but before the first read.
            with open(stored, "ab") as w:
                w.write(b"GROWN-AFTER-OPEN")
        return fh, st, reason

    monkeypatch.setattr(attachments_mod, "_open_contained", open_then_grow)
    resp = jira_attachment_env["client"].get("/api/attachments/jira/101/download", headers=analyst_user)
    assert resp.status_code == 200
    payload = jira_attachment_env["payload"]
    assert resp.headers["content-length"] == str(len(payload))
    assert resp.content == payload, "body must stop exactly at the fstat'd size"


def test_admin_god_mode_passes_the_table_gate(jira_attachment_env, admin_user):
    resp = jira_attachment_env["client"].get("/api/attachments/jira/101/download", headers=admin_user)
    assert resp.status_code == 200
    assert resp.content == jira_attachment_env["payload"]


def test_server_only_catalogue_table_keeps_its_binaries(jira_attachment_env, analyst_user, admin_user):
    """`server_only` is the admin's "these bytes do not leave the server"
    lever; the parquet download honours it (`_distribution_refusal`,
    app/api/data.py) and this route must not be the way around it. Asserted
    WITH table access granted — the gate is a property of the table, not an
    authorization check — and for admin god-mode too, exactly like the
    parquet route. Audited as `denied` (policy refusal, not malfunction)."""
    _grant_table("analyst1", "attachments", "att-so")
    c = jira_attachment_env["client"]
    conn = get_system_db()
    try:
        conn.execute("UPDATE table_registry SET server_only = TRUE WHERE id = 'attachments'")
    finally:
        conn.close()

    resp = c.get("/api/attachments/jira/101/download", headers=analyst_user)
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "attachment_table_server_only"
    row = _last_audit_row()
    assert row is not None
    assert row[3] == "denied"

    resp = c.get("/api/attachments/jira/101/download", headers=admin_user)
    assert resp.status_code == 403, "server_only is a table property — god-mode does not bypass it"


def test_unknown_id_is_not_found(jira_attachment_env, analyst_user):
    _grant_table("analyst1", "attachments", "att-noid")
    resp = jira_attachment_env["client"].get("/api/attachments/jira/99999/download", headers=analyst_user)
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "attachment_not_found"


@pytest.mark.parametrize(
    "attachment_id",
    ["102", "103", "104"],
    ids=["no-path-recorded", "file-removed-since", "path-escapes-root"],
)
def test_no_stored_bytes_is_distinguishable(jira_attachment_env, analyst_user, attachment_id):
    """Empty path, vanished file, and a tampered path that escapes the
    permitted root all answer `attachment_not_stored` — a client falls back
    to the upstream API for these, and the escape is never served."""
    _grant_table("analyst1", "attachments", f"att-miss-{attachment_id}")
    resp = jira_attachment_env["client"].get(f"/api/attachments/jira/{attachment_id}/download", headers=analyst_user)
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "attachment_not_stored"
    assert b"never served" not in resp.content


def test_unreadable_stored_file_is_503_not_a_miss(jira_attachment_env, analyst_user, monkeypatch):
    """A catalogued file the OS refuses to open (EACCES/EIO…) is a server
    malfunction, not a miss: `attachment_not_stored` would send every caller
    to the upstream API while the outage looks like normal behaviour. Patched
    at os.open (not chmod) so the test also holds when the suite runs as
    root, where mode bits do not deny reads."""
    import errno
    import os as os_mod

    _grant_table("analyst1", "attachments", "att-unread")
    target = str((jira_attachment_env["root"] / "SUP-1" / "101_report.pdf").resolve())
    real_open = os_mod.open

    def deny(path, flags, *args, **kwargs):
        if str(path) == target:
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("app.api.attachments.os.open", deny)
    resp = jira_attachment_env["client"].get("/api/attachments/jira/101/download", headers=analyst_user)
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "attachment_unreadable"
    row = _last_audit_row()
    assert row[3] == "error.503"
    assert "file_unreadable" in (row[4] or "")


def test_second_source_needs_no_route_change(seeded_app, analyst_user, monkeypatch):
    """Registering another declaration makes the same route + CLI path serve
    it — the acceptance criterion for source-parameterized reuse."""
    env = seeded_app["env"]
    c = seeded_app["client"]
    root = env["data_dir"] / "zendesk_files"
    root.mkdir()
    stored = root / "7_export.csv"
    stored.write_bytes(b"a,b\n1,2\n")

    create_mock_extract(
        env["extracts_dir"],
        "zendesk",
        [
            {
                "name": "zendesk_attachments",
                "data": [{"file_id": "7", "path": str(stored)}],
            }
        ],
    )
    from src.orchestrator import SyncOrchestrator

    SyncOrchestrator().rebuild()

    resp = c.post(
        "/api/admin/register-table",
        json={"name": "zendesk_attachments", "source_type": "keboola", "query_mode": "local"},
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
    )
    assert resp.status_code == 201

    monkeypatch.setitem(
        _SOURCES,
        "zendesk",
        AttachmentSource(
            source="zendesk",
            table="zendesk_attachments",
            id_column="file_id",
            path_column="path",
            root=lambda: root,
        ),
    )
    _grant_table("analyst1", "zendesk_attachments", "att-zd")
    resp = c.get("/api/attachments/zendesk/7/download", headers=analyst_user)
    assert resp.status_code == 200
    assert resp.content == b"a,b\n1,2\n"


class TestOpenContained:
    """Unit coverage of the containment guard, per the security playbook."""

    def _root(self, tmp_path: Path) -> Path:
        root = tmp_path / "attachments"
        root.mkdir()
        return root

    def test_rejects_traversal_and_unsafe_values(self, tmp_path):
        from app.api.attachments import _open_contained

        root = self._root(tmp_path)
        (tmp_path / "secret.txt").write_bytes(b"x")
        for stored in [
            str(tmp_path / "secret.txt"),  # absolute escape
            "../secret.txt",  # relative escape
            "a/../../secret.txt",
            "a\\b.txt",  # backslash
            "a\x00b",  # NUL
        ]:
            fh, st, reason = _open_contained(root, stored)
            assert fh is None, stored
            assert st is None, stored
            assert reason == "path_rejected", stored

    def test_null_and_missing_are_distinct_reasons(self, tmp_path):
        from app.api.attachments import _open_contained

        root = self._root(tmp_path)
        assert _open_contained(root, None) == (None, None, "no_path_recorded")
        assert _open_contained(root, "") == (None, None, "no_path_recorded")
        assert _open_contained(root, str(root / "nope.png")) == (None, None, "file_missing")
        # A directory is not servable bytes either.
        assert _open_contained(root, str(root)) == (None, None, "file_missing")

    def test_accepts_absolute_and_relative_inside_root(self, tmp_path):
        from app.api.attachments import _open_contained

        root = self._root(tmp_path)
        (root / "SUP-1").mkdir()
        f = root / "SUP-1" / "1_a.png"
        f.write_bytes(b"ok")
        for stored in (str(f), "SUP-1/1_a.png"):
            fh, st, reason = _open_contained(root, stored)
            assert fh is not None and fh.read() == b"ok"
            fh.close()
            assert st is not None and st.st_size == 2
            assert reason == ""

    def test_fstat_is_of_the_opened_descriptor(self, tmp_path):
        """The advertised size must describe the inode being streamed: an
        atomic re-publish (os.replace) after open must not change what this
        descriptor reads or the size already fstat'd."""
        import os

        from app.api.attachments import _open_contained

        root = self._root(tmp_path)
        f = root / "1_a.bin"
        f.write_bytes(b"old-bytes")
        fh, st, reason = _open_contained(root, str(f))
        assert reason == "" and st.st_size == 9
        # connector-style atomic rewrite while the response is in flight
        tmp = root / "1_a.bin.tmp-1"
        tmp.write_bytes(b"NEW")
        os.replace(tmp, f)
        assert fh.read() == b"old-bytes"  # complete old file, matching st
        fh.close()

    def test_permission_failure_is_unreadable_not_missing(self, tmp_path, monkeypatch):
        """EACCES (and any non-ENOENT/ENOTDIR OSError) must come back as
        `file_unreadable`, never `file_missing` — the 0o660 pin makes a
        serving process outside the writer's group exactly this case."""
        import errno
        import os as os_mod

        from app.api.attachments import _open_contained

        root = self._root(tmp_path)
        f = root / "1_locked.bin"
        f.write_bytes(b"x")
        real_open = os_mod.open

        def deny(path, flags, *args, **kwargs):
            if str(path) == str(f):
                raise PermissionError(errno.EACCES, "Permission denied", str(path))
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr("app.api.attachments.os.open", deny)
        assert _open_contained(root, str(f)) == (None, None, "file_unreadable")

    def test_fifo_is_refused_without_blocking(self, tmp_path):
        """open() on a FIFO with no writer blocks forever — the guard must
        identify and refuse it via a non-blocking open, not by reading."""
        import os

        from app.api.attachments import _open_contained

        root = self._root(tmp_path)
        fifo = root / "1_pipe"
        os.mkfifo(fifo)
        assert _open_contained(root, str(fifo)) == (None, None, "file_missing")


class TestDeclarationMatchesConnector:
    """Pin the declaration to what the connector actually emits — the
    blocker class this guards against is a declaration naming a table that
    resolves nowhere: master views are named verbatim from _meta.table_name,
    and the Jira connector's table names are UNPREFIXED."""

    def test_jira_table_is_a_real_connector_table(self):
        from connectors.jira.extract_init import JIRA_TABLES
        from src.attachment_sources import get_attachment_source

        decl = get_attachment_source("jira")
        assert decl.table in JIRA_TABLES, (
            f"declared catalogue table {decl.table!r} is not one the Jira "
            f"connector emits ({JIRA_TABLES}) — it would resolve to no view"
        )

    def test_jira_columns_exist_in_the_transform_output(self, tmp_path):
        from connectors.jira.transform import transform_attachments
        from src.attachment_sources import get_attachment_source

        decl = get_attachment_source("jira")
        raw = {
            "key": "SUP-1",
            "fields": {"attachment": [{"id": "1", "filename": "a.png", "author": None}]},
        }
        records = transform_attachments(raw, attachments_dir=tmp_path)
        assert records, "synthetic payload produced no attachment record"
        cols = set(records[0])
        declared = {decl.id_column, decl.path_column, decl.filename_column}
        assert declared <= cols, f"declared columns {declared - cols} missing from {sorted(cols)}"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _attach_policy(table_id: str, sql: str, note: str = "row-level test policy") -> None:
    """Attach a policy DIRECTLY via the repository, bypassing the admin
    write-time interlock (`access_policy_requires_undistributed`) that
    would otherwise force `server_only=True`/`query_mode='remote'` first --
    the same shortcut `test_server_only_catalogue_table_keeps_its_binaries`
    takes with a raw SQL UPDATE. This exercises the row-visibility check on
    its own, independent of whichever tables the interlock currently
    permits a policy on (K3: the gap this closes is not contingent on that
    interlock holding for every present and future attachment source)."""
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    try:
        TableRegistryRepository(conn).set_access_policy(table_id, sql=sql, note=note, updated_by="admin")
    finally:
        conn.close()


@pytest.fixture
def policied_jira_attachment_env(jira_attachment_env):
    """Attach a row-filtering policy to the `attachments` catalogue table
    that hides attachment id 101 from everyone except alice@example.com --
    two non-admin users, both granted TABLE-level access, so any
    difference in what they can download is attributable to the ROW
    policy, not RBAC."""
    from app.auth.jwt import create_access_token
    from src.repositories.users import UserRepository

    conn = get_system_db()
    try:
        users = UserRepository(conn)
        users.create(id="u_alice", email="alice@example.com", name="Alice")
        users.create(id="u_bob", email="bob@example.com", name="Bob")
        from tests.conftest import grant_table_via_package

        grant_table_via_package(conn, "attachments", "u_alice", group_name="att-alice")
        grant_table_via_package(conn, "attachments", "u_bob", group_name="att-bob")
    finally:
        conn.close()

    _attach_policy(
        "attachments",
        "SELECT * FROM attachments WHERE attachment_id != '101' OR $user_email = 'alice@example.com'",
    )

    return {
        **jira_attachment_env,
        "alice_token": create_access_token("u_alice", "alice@example.com"),
        "bob_token": create_access_token("u_bob", "bob@example.com"),
    }


class TestRowLevelAccessPolicy:
    """K3 -- table-level RBAC only answers "can this caller read the
    catalogue table at all"; a table access policy narrows WHICH ROWS once
    they can, and an attachment binary belongs to exactly one row."""

    def test_user_the_policy_admits_downloads_the_row(self, policied_jira_attachment_env):
        resp = policied_jira_attachment_env["client"].get(
            "/api/attachments/jira/101/download", headers=_auth(policied_jira_attachment_env["alice_token"])
        )
        assert resp.status_code == 200
        assert resp.content == policied_jira_attachment_env["payload"]

    def test_user_the_policy_hides_the_row_from_is_blocked(self, policied_jira_attachment_env):
        resp = policied_jira_attachment_env["client"].get(
            "/api/attachments/jira/101/download", headers=_auth(policied_jira_attachment_env["bob_token"])
        )
        assert resp.status_code == 404
        assert resp.json()["detail"]["code"] == "attachment_not_found"
        row = _last_audit_row()
        assert row is not None
        assert row[0] == "u_bob"
        assert row[3] == "error.404"

    def test_hidden_row_is_indistinguishable_from_a_genuinely_missing_one(self, policied_jira_attachment_env):
        c = policied_jira_attachment_env["client"]
        bob = _auth(policied_jira_attachment_env["bob_token"])
        hidden = c.get("/api/attachments/jira/101/download", headers=bob)
        missing = c.get("/api/attachments/jira/999999/download", headers=bob)
        assert hidden.status_code == missing.status_code == 404
        assert hidden.json()["detail"]["code"] == missing.json()["detail"]["code"] == "attachment_not_found"

    def test_admin_god_mode_still_bypasses_the_row_policy(self, policied_jira_attachment_env, admin_user):
        resp = policied_jira_attachment_env["client"].get("/api/attachments/jira/101/download", headers=admin_user)
        assert resp.status_code == 200
        assert resp.content == policied_jira_attachment_env["payload"]

    def test_table_without_a_policy_is_unaffected(self, jira_attachment_env, analyst_user):
        """Regression: a table that never had a policy attached must not
        pay for (or be blocked by) the new check at all."""
        _grant_table("analyst1", "attachments", "att-no-policy")
        resp = jira_attachment_env["client"].get("/api/attachments/jira/101/download", headers=analyst_user)
        assert resp.status_code == 200
        assert resp.content == jira_attachment_env["payload"]

    def test_a_policy_that_fails_to_execute_fails_closed(self, jira_attachment_env, analyst_user):
        """A policy body referencing a column that does not exist is valid
        SQL TEXT (sqlglot parses it fine) but fails at EXECUTION time --
        this must block the download, never fall through to serving the
        raw row because the policy could not be evaluated."""
        _grant_table("analyst1", "attachments", "att-broken-policy")
        _attach_policy(
            "attachments",
            "SELECT * FROM attachments WHERE definitely_not_a_real_column_xyz = 'x'",
            note="broken policy for the fail-closed test",
        )
        resp = jira_attachment_env["client"].get("/api/attachments/jira/101/download", headers=analyst_user)
        assert resp.status_code == 404
        assert resp.json()["detail"]["code"] == "attachment_not_found"
        assert jira_attachment_env["payload"] not in resp.content

    def test_analytics_db_unavailable_fails_closed_not_500(self, policied_jira_attachment_env, monkeypatch):
        """finding C (follow-up review of PR #2023): the policy guard's own
        connection OPEN used to sit outside the guarded try/except, so a
        failure to open the analytics DB propagated as an unhandled 500
        instead of the guard's fail-closed 404 -- and bypassed the route's
        audit entirely. Alice's persona is normally admitted by this
        policy, so a 404 here is attributable ONLY to the guard fail-closing
        on the open failure, not to the policy's own row filter."""
        import app.api.attachments as attachments_mod

        def _boom():
            raise RuntimeError("analytics db unavailable")

        monkeypatch.setattr(attachments_mod, "get_analytics_db_readonly", _boom)

        resp = policied_jira_attachment_env["client"].get(
            "/api/attachments/jira/101/download",
            headers=_auth(policied_jira_attachment_env["alice_token"]),
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"]["code"] == "attachment_not_found"

        row = _last_audit_row()
        assert row is not None
        assert row[3] == "error.404"
        import json as _json

        params = _json.loads(row[4]) if isinstance(row[4], str) else row[4]
        assert params.get("reason") == "policy_check_failed"


class TestRowVisibleUnderAccessPolicyUnit:
    """Direct unit coverage of the resolver-facing helper, independent of
    the HTTP plumbing above -- pins the fail-closed contract for identity
    and resolution failures that are hard to trigger end to end.

    The helper returns ``(visible, failure_reason)``: ``failure_reason`` is
    ``None`` when the policy body actually ran and answered (allow, or a
    genuine row-level deny) -- the route keeps its existing
    ``policied_row_not_visible`` audit reason for that case -- and
    ``"policy_check_failed"`` when visible=False because something failed
    BEFORE the policy could give a real answer (unresolvable identity, the
    analytics DB failing to open, or the existence query itself raising),
    so ops can tell "db unavailable" apart from "this row is not yours" in
    the audit trail (finding C, follow-up review of PR #2023)."""

    def _decl(self):
        from src.attachment_sources import get_attachment_source

        return get_attachment_source("jira")

    def test_returns_true_without_querying_when_not_policied(self, monkeypatch):
        import app.api.attachments as attachments_mod
        from src.access_policy import PoliciedRelation

        monkeypatch.setattr(
            attachments_mod,
            "policied_relation",
            lambda table_id, user: PoliciedRelation(
                relation_sql="SELECT * FROM attachments", params={}, policied=False, table_id=table_id
            ),
        )

        def _boom():
            raise AssertionError("must not open a connection for the non-policied case")

        monkeypatch.setattr(attachments_mod, "get_analytics_db_readonly", _boom)
        assert attachments_mod._row_visible_under_access_policy("attachments", "101", self._decl(), {"id": "u1"}) == (
            True,
            None,
        )

    def test_fails_closed_on_identity_unresolvable(self, monkeypatch):
        import app.api.attachments as attachments_mod
        from src.access_policy import PolicyIdentityUnresolvable

        def _raise(table_id, user):
            raise PolicyIdentityUnresolvable("no identity")

        monkeypatch.setattr(attachments_mod, "policied_relation", _raise)
        assert attachments_mod._row_visible_under_access_policy("attachments", "101", self._decl(), {"id": "u1"}) == (
            False,
            "policy_check_failed",
        )

    def test_fails_closed_on_policy_error(self, monkeypatch):
        import app.api.attachments as attachments_mod
        from src.access_policy import PolicyError

        def _raise(table_id, user):
            raise PolicyError(table_id)

        monkeypatch.setattr(attachments_mod, "policied_relation", _raise)
        assert attachments_mod._row_visible_under_access_policy("attachments", "101", self._decl(), {"id": "u1"}) == (
            False,
            "policy_check_failed",
        )

    def test_fails_closed_when_the_existence_query_raises(self, monkeypatch):
        import app.api.attachments as attachments_mod
        from src.access_policy import PoliciedRelation

        monkeypatch.setattr(
            attachments_mod,
            "policied_relation",
            lambda table_id, user: PoliciedRelation(
                relation_sql="SELECT * FROM attachments", params={}, policied=True, table_id=table_id
            ),
        )

        class _BoomConn:
            def execute(self, sql, params=None):
                raise RuntimeError("engine error")

            def close(self):
                pass

        monkeypatch.setattr(attachments_mod, "get_analytics_db_readonly", lambda: _BoomConn())
        assert attachments_mod._row_visible_under_access_policy("attachments", "101", self._decl(), {"id": "u1"}) == (
            False,
            "policy_check_failed",
        )

    def test_fails_closed_when_opening_the_analytics_db_raises(self, monkeypatch):
        """finding C (follow-up review of PR #2023): the connection OPEN
        itself was previously outside the guarded region -- a failure there
        propagated uncaught past this helper instead of failing closed like
        every other policy-check failure."""
        import app.api.attachments as attachments_mod
        from src.access_policy import PoliciedRelation

        monkeypatch.setattr(
            attachments_mod,
            "policied_relation",
            lambda table_id, user: PoliciedRelation(
                relation_sql="SELECT * FROM attachments", params={}, policied=True, table_id=table_id
            ),
        )

        def _boom():
            raise RuntimeError("analytics db unavailable")

        monkeypatch.setattr(attachments_mod, "get_analytics_db_readonly", _boom)
        assert attachments_mod._row_visible_under_access_policy("attachments", "101", self._decl(), {"id": "u1"}) == (
            False,
            "policy_check_failed",
        )


def test_registry_read_failure_fails_closed_not_open(jira_attachment_env, admin_user, monkeypatch):
    """Devin on #1297 — a transient registry read failure used to fall
    through with reg_row=None, silently skipping the `server_only` gate:
    bytes released by a gate that never ran. Like the parquet route's
    `distribution_check_unavailable`, the download must 503 instead —
    including for admin god-mode, whose RBAC short-circuit never touches
    the registry and so would sail past the broken gate."""

    def _boom():
        raise RuntimeError("system.duckdb lock lost")

    monkeypatch.setattr("src.repositories.table_registry_repo", _boom)

    resp = jira_attachment_env["client"].get("/api/attachments/jira/101/download", headers=admin_user)
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "registry_unavailable"
    row = _last_audit_row()
    assert row is not None
    assert row[3] == "error.503"


def test_broken_catalogue_is_503_not_a_miss(jira_attachment_env, admin_user, monkeypatch):
    """Devin on #1297 — only a genuinely absent view (declared but never
    synced) may read as "no row". A broken analytics DB (re-ATTACH failure
    swallowed upstream) also surfaces as CatalogException, and declaration/
    connector column drift surfaces as BinderException; reporting either as
    404 attachment_not_found sends the client to the upstream API for rows
    that exist. Both must 503 catalogue_unavailable instead."""
    import duckdb as duckdb_mod

    import app.api.attachments as attachments_mod

    c = jira_attachment_env["client"]

    class _Conn:
        def __init__(self, main_exc, probe_result=None, probe_exc=None):
            self._main_exc = main_exc
            self._probe_result = probe_result
            self._probe_exc = probe_exc
            self._first = True

        def execute(self, sql, params=None):
            if self._first:
                self._first = False
                raise self._main_exc
            if self._probe_exc is not None:
                raise self._probe_exc

            class _R:
                def __init__(self, row):
                    self._row = row

                def fetchone(self):
                    return self._row

            return _R(self._probe_result)

        def close(self):
            pass

    cases = [
        # broken DB: catalog error AND the probe itself fails
        _Conn(duckdb_mod.CatalogException("x"), probe_exc=RuntimeError("attach failed")),
        # view registered but the query still failed
        _Conn(duckdb_mod.CatalogException("x"), probe_result=(1,)),
        # declaration/connector column drift
        _Conn(duckdb_mod.BinderException("no column local_path")),
    ]
    for conn in cases:
        monkeypatch.setattr(attachments_mod, "get_analytics_db_readonly", lambda conn=conn: conn)
        resp = c.get("/api/attachments/jira/101/download", headers=admin_user)
        assert resp.status_code == 503, resp.text
        assert resp.json()["detail"]["code"] == "catalogue_unavailable"

    # The one benign case: view genuinely absent -> still a distinguishable 404.
    absent = _Conn(duckdb_mod.CatalogException("x"), probe_result=None)
    monkeypatch.setattr(attachments_mod, "get_analytics_db_readonly", lambda: absent)
    resp = c.get("/api/attachments/jira/101/download", headers=admin_user)
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "attachment_not_found"

    # The OPEN itself failing (read-only open refused while a RW handle is
    # alive, corrupt/locked file, DuckLake catalog connectivity) is the same
    # malfunction class — it must reach the audited 503, not escape as a
    # bare 500 with no audit row (Devin on #1297).
    def _open_boom():
        raise RuntimeError("read-only open refused: RW handle alive")

    monkeypatch.setattr(attachments_mod, "get_analytics_db_readonly", _open_boom)
    resp = c.get("/api/attachments/jira/101/download", headers=admin_user)
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["code"] == "catalogue_unavailable"
    row = _last_audit_row()
    assert row[3] == "error.503"
    assert "catalog_open_failed" in (row[4] or "")
