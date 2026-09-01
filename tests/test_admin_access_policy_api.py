"""Task 14 — admin API: mandatory ``access_policy_note`` (design doc §4) and
``POST /api/admin/registry/{table_id}/policy/preview`` (design doc §13.1).

``PUT /registry/{table_id}`` already accepts/validates/persists a policy
(Tasks 2/4/12); this task closes the one gap those tasks deliberately left
to the API layer (§4's "required when access_policy_sql is set" is a
product rule, not a repository-setter rule — the setter itself stays
permissive so a future non-HTTP caller isn't forced through this same
gate), and adds the read-only preview surface admins use to see what a
candidate or already-saved policy actually does for a chosen persona
*before* trusting it, per §13.1's "the preview is a matrix, not a run" —
this is the single-persona primitive that matrix is built from.

Mirrors ``tests/test_journey_access_policy_interlock.py`` for the
HTTP-level admin-token style, and
``tests/test_access_policy_table_id_surfaces.py`` /
``tests/test_access_policy_effective_schema.py`` for the real-data fixture
(``mock_extract_factory`` + ``SyncOrchestrator.rebuild`` +
``set_access_policy``) the preview tests need for a meaningful
``rows_visible < rows_total`` split.
"""

from __future__ import annotations

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(c, token, **kwargs) -> str:
    kwargs.setdefault("source_type", "keboola")
    kwargs.setdefault("query_mode", "local")
    resp = c.post("/api/admin/register-table", json=kwargs, headers=_auth(token))
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _audit_rows(**filters):
    from src.repositories import audit_repo

    result = audit_repo().query(**filters)
    return list(result[0] if isinstance(result, tuple) else result)


# ── Deliverable 1: access_policy_note is mandatory (§4) ────────────────


@pytest.mark.journey
class TestMandatoryNote:
    def test_attach_with_no_note_is_rejected(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        table_id = _register(c, token, name="no_note_tbl", server_only=True)

        resp = c.put(
            f"/api/admin/registry/{table_id}",
            json={"access_policy_sql": "SELECT * FROM no_note_tbl"},
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "policy_note_required" in resp.text

        from src.repositories import table_registry_repo

        assert table_registry_repo().get(table_id)["access_policy_sql"] is None

    def test_attach_with_whitespace_only_note_is_rejected(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        table_id = _register(c, token, name="blank_note_tbl", server_only=True)

        resp = c.put(
            f"/api/admin/registry/{table_id}",
            json={
                "access_policy_sql": "SELECT * FROM blank_note_tbl",
                "access_policy_note": "   ",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "policy_note_required" in resp.text

    def test_attach_with_explicit_null_note_is_rejected(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        table_id = _register(c, token, name="null_note_tbl", server_only=True)

        resp = c.put(
            f"/api/admin/registry/{table_id}",
            json={
                "access_policy_sql": "SELECT * FROM null_note_tbl",
                "access_policy_note": None,
            },
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "policy_note_required" in resp.text

    def test_attach_with_a_real_note_succeeds(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        table_id = _register(c, token, name="good_note_tbl", server_only=True)

        resp = c.put(
            f"/api/admin/registry/{table_id}",
            json={
                "access_policy_sql": "SELECT * FROM good_note_tbl",
                "access_policy_note": "restrict rows to the caller's cost centre",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text

    def test_clearing_the_policy_needs_no_note(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        table_id = _register(c, token, name="clear_no_note_tbl", server_only=True)

        attach = c.put(
            f"/api/admin/registry/{table_id}",
            json={
                "access_policy_sql": "SELECT * FROM clear_no_note_tbl",
                "access_policy_note": "restrict rows",
            },
            headers=_auth(token),
        )
        assert attach.status_code == 200, attach.text

        clear = c.put(
            f"/api/admin/registry/{table_id}",
            json={"access_policy_sql": None},
            headers=_auth(token),
        )
        assert clear.status_code == 200, clear.text

    def test_blanking_the_note_while_the_policy_stays_attached_is_rejected(self, seeded_app, monkeypatch):
        """The gap a naive "only check when THIS PUT touches sql" rule would
        miss: a SEPARATE PUT that clears only the note, leaving the SQL
        policy attached, must not be allowed to strip the explanation — the
        merged/final record is what must always carry a note, not merely
        every PUT that happens to mention ``access_policy_sql``."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        table_id = _register(c, token, name="blank_after_attach_tbl", server_only=True)

        attach = c.put(
            f"/api/admin/registry/{table_id}",
            json={
                "access_policy_sql": "SELECT * FROM blank_after_attach_tbl",
                "access_policy_note": "restrict rows",
            },
            headers=_auth(token),
        )
        assert attach.status_code == 200, attach.text

        resp = c.put(
            f"/api/admin/registry/{table_id}",
            json={"access_policy_note": ""},
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "policy_note_required" in resp.text

        from src.repositories import table_registry_repo

        row = table_registry_repo().get(table_id)
        assert row["access_policy_note"] == "restrict rows"

    def test_unrelated_edit_on_an_already_noted_policy_is_untouched(self, seeded_app, monkeypatch):
        """No false positive: editing an unrelated field on a table that
        already carries a valid policy + note must not re-demand the note."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        table_id = _register(c, token, name="already_noted_tbl", server_only=True)

        attach = c.put(
            f"/api/admin/registry/{table_id}",
            json={
                "access_policy_sql": "SELECT * FROM already_noted_tbl",
                "access_policy_note": "restrict rows",
            },
            headers=_auth(token),
        )
        assert attach.status_code == 200, attach.text

        resp = c.put(
            f"/api/admin/registry/{table_id}",
            json={"description": "an unrelated edit"},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text


# ── #1979: the policy SQL body must never land in audit_log.params ─────
#
# `access_policy_sql` is already persisted verbatim on `table_registry`
# (the durable record) and `access_policy_updated_at`/`_by` already say
# who/when. The audit row is a WHO/WHEN/WHAT-CHANGED trail, not a second
# copy of the content -- per the audit playbook's "content never enters
# params" rule (docs: `.claude/skills/agnes-conventions/references/audit.md`).


@pytest.mark.journey
class TestPolicyAuditRedaction:
    @staticmethod
    def _sentinel_sql(table_name: str) -> str:
        # `SELECT * FROM <self>` -- the policy validator (§14.6 live probe)
        # requires a policy reference its own table, so the sentinel must be
        # keyed on whatever name the calling test just registered.
        return f"SELECT * /* SENTINEL_POLICY_BODY_1979 */ FROM {table_name}"

    def test_update_table_audit_redacts_the_policy_sql(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        table_id = _register(c, token, name="redact_policy_tbl", server_only=True)
        sentinel_sql = self._sentinel_sql(table_id)

        resp = c.put(
            f"/api/admin/registry/{table_id}",
            json={
                "access_policy_sql": sentinel_sql,
                "access_policy_note": "restrict rows to the caller's unit",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text

        rows = _audit_rows(action="update_table", resource=table_id)
        assert rows, "update_table audit entry not found"
        import json as _json

        raw_params = rows[0]["params"]
        params = _json.loads(raw_params) if isinstance(raw_params, str) else raw_params

        # The policy body must not be recoverable from the row at all --
        # neither under its own key nor smuggled anywhere else in params.
        assert sentinel_sql not in (raw_params if isinstance(raw_params, str) else _json.dumps(params))
        assert params["access_policy_sql"] != sentinel_sql

        # But the audit trail must still show THAT the policy changed, by
        # whom, and when -- `updated_fields` plus the repo's own
        # access_policy_updated_at/_by (not audit params) carry that.
        assert "access_policy_sql" in params["updated_fields"]

        from src.repositories import table_registry_repo

        row = table_registry_repo().get(table_id)
        assert row["access_policy_sql"] == sentinel_sql
        assert row["access_policy_updated_by"]
        assert row["access_policy_updated_at"]

    def test_update_table_audit_keeps_the_policy_note(self, seeded_app, monkeypatch):
        """`access_policy_note` is a human "why", not a SQL content field --
        it stays in params, same treatment as `description`."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        table_id = _register(c, token, name="redact_note_stays_tbl", server_only=True)
        sentinel_sql = self._sentinel_sql(table_id)

        resp = c.put(
            f"/api/admin/registry/{table_id}",
            json={
                "access_policy_sql": sentinel_sql,
                "access_policy_note": "restrict rows to the caller's unit",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text

        rows = _audit_rows(action="update_table", resource=table_id)
        import json as _json

        raw_params = rows[0]["params"]
        params = _json.loads(raw_params) if isinstance(raw_params, str) else raw_params
        assert params["access_policy_note"] == "restrict rows to the caller's unit"


# ── Deliverable 2: POST /registry/{table_id}/policy/preview (§13.1) ────


@pytest.fixture
def policied_invoices_for_preview(seeded_app, mock_extract_factory, monkeypatch):
    """A ``server_only`` table with a real row+column policy over real
    synced data — two rows in group ``Finance``, one in ``Ops`` — so a
    preview as a persona in only one of those groups has a genuine
    ``rows_visible < rows_total`` split to assert on, and the ``EXCLUDE``d
    ``secret`` column has something to hide.
    """
    from src.db import get_system_db
    from src.orchestrator import SyncOrchestrator
    from src.repositories.table_registry import TableRegistryRepository

    monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")

    env = seeded_app["env"]
    mock_extract_factory(
        "keboola",
        [
            {
                "name": "preview_invoices",
                "data": [
                    {"id": "1", "unit": "Finance", "secret": "s1", "amount": "100"},
                    {"id": "2", "unit": "Finance", "secret": "s2", "amount": "150"},
                    {"id": "3", "unit": "Ops", "secret": "s3", "amount": "300"},
                ],
            }
        ],
    )
    SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

    conn = get_system_db()
    try:
        registry = TableRegistryRepository(conn)
        registry.register(
            id="preview_invoices",
            name="preview_invoices",
            source_type="keboola",
            query_mode="local",
            server_only=True,
        )
        registry.set_access_policy(
            "preview_invoices",
            sql=("SELECT * EXCLUDE (secret) FROM preview_invoices WHERE list_contains($user_groups, unit)"),
            note="restrict to the caller's unit",
            updated_by="admin",
        )
    finally:
        conn.close()

    return seeded_app


@pytest.mark.journey
class TestPolicyPreview:
    def test_preview_stored_policy_filters_rows_and_hides_the_excluded_column(self, policied_invoices_for_preview):
        c = policied_invoices_for_preview["client"]
        token = policied_invoices_for_preview["admin_token"]

        resp = c.post(
            "/api/admin/registry/preview_invoices/policy/preview",
            json={"as_groups": ["Finance"]},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["rows_total"] == 3
        assert body["rows_visible"] == 2
        assert body["rows_visible"] < body["rows_total"]
        assert {r["id"] for r in body["sample_rows"]} == {"1", "2"}

        by_name = {col["name"]: col for col in body["columns"]}
        assert by_name["secret"]["hidden"] is True
        assert by_name["unit"]["hidden"] is False
        assert by_name["amount"]["hidden"] is False

    def test_preview_a_different_persona_sees_a_disjoint_slice(self, policied_invoices_for_preview):
        c = policied_invoices_for_preview["client"]
        token = policied_invoices_for_preview["admin_token"]

        resp = c.post(
            "/api/admin/registry/preview_invoices/policy/preview",
            json={"as_groups": ["Ops"]},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["rows_visible"] == 1
        assert {r["id"] for r in body["sample_rows"]} == {"3"}

    def test_preview_returns_base_rows_for_a_before_after_view(self, policied_invoices_for_preview):
        # Slice 2: the persona before/after preview needs the RAW sample the
        # authoring admin (god-mode) may see, so the UI can strike through the
        # rows the policy drops and show real->masked cells side by side.
        c = policied_invoices_for_preview["client"]
        token = policied_invoices_for_preview["admin_token"]

        resp = c.post(
            "/api/admin/registry/preview_invoices/policy/preview",
            json={"as_groups": ["Finance"]},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        # base sample carries EVERY row (incl. the Ops row the policy filters
        # out) and the raw `secret` column the policy hides — that is the
        # "before" the UI diffs the policied "after" against.
        assert "base_sample_rows" in body
        assert {r["id"] for r in body["base_sample_rows"]} == {"1", "2", "3"}
        assert any("secret" in r for r in body["base_sample_rows"])
        # the policied slice stays filtered + masked
        assert {r["id"] for r in body["sample_rows"]} == {"1", "2"}
        assert all("secret" not in r for r in body["sample_rows"])

    def test_preview_candidate_sql_before_saving_does_not_touch_the_stored_policy(self, policied_invoices_for_preview):
        c = policied_invoices_for_preview["client"]
        token = policied_invoices_for_preview["admin_token"]

        resp = c.post(
            "/api/admin/registry/preview_invoices/policy/preview",
            json={
                "sql": "SELECT * FROM preview_invoices WHERE list_contains($user_groups, unit)",
                "as_groups": ["Ops"],
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["rows_visible"] == 1
        assert body["rows_total"] == 3
        # The candidate does not EXCLUDE secret -- unlike the stored policy.
        by_name = {col["name"]: col for col in body["columns"]}
        assert by_name["secret"]["hidden"] is False

        from src.repositories import table_registry_repo

        stored = table_registry_repo().get("preview_invoices")["access_policy_sql"]
        assert "EXCLUDE" in stored

    def test_preview_of_invalid_candidate_sql_returns_the_validation_reason(self, policied_invoices_for_preview):
        c = policied_invoices_for_preview["client"]
        token = policied_invoices_for_preview["admin_token"]

        resp = c.post(
            "/api/admin/registry/preview_invoices/policy/preview",
            json={"sql": "SELECT * FROM some_unrelated_table", "as_groups": ["Finance"]},
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "policy_unlisted_table_reference" in resp.text

    def test_preview_writes_an_audit_row(self, policied_invoices_for_preview):
        c = policied_invoices_for_preview["client"]
        token = policied_invoices_for_preview["admin_token"]

        resp = c.post(
            "/api/admin/registry/preview_invoices/policy/preview",
            json={"as_groups": ["Finance"]},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text

        rows = _audit_rows(action="access_policy.preview", resource="preview_invoices")
        assert rows, "the preview left no audit trail -- §13.1 requires it be audited"

    def test_preview_audit_redacts_the_candidate_sql(self, policied_invoices_for_preview):
        """#1979 -- a candidate SQL body previewed before ever being saved
        must not leak into audit params either."""
        c = policied_invoices_for_preview["client"]
        token = policied_invoices_for_preview["admin_token"]
        sentinel_sql = "SELECT id, unit /* SENTINEL_CANDIDATE_1979 */ FROM preview_invoices"

        resp = c.post(
            "/api/admin/registry/preview_invoices/policy/preview",
            json={"sql": sentinel_sql, "as_groups": ["Finance"]},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text

        rows = _audit_rows(action="access_policy.preview", resource="preview_invoices")
        assert rows, "the preview left no audit trail -- §13.1 requires it be audited"
        import json as _json

        raw_params = rows[0]["params"]
        params = _json.loads(raw_params) if isinstance(raw_params, str) else raw_params
        assert sentinel_sql not in (raw_params if isinstance(raw_params, str) else _json.dumps(params))
        assert params["candidate_sql"] != sentinel_sql

    def test_preview_requires_admin(self, policied_invoices_for_preview):
        c = policied_invoices_for_preview["client"]
        token = policied_invoices_for_preview["analyst_token"]

        resp = c.post(
            "/api/admin/registry/preview_invoices/policy/preview",
            json={"as_groups": ["Finance"]},
            headers=_auth(token),
        )
        assert resp.status_code == 403, resp.text

    def test_preview_as_a_real_user_uses_their_live_group_membership(self, policied_invoices_for_preview):
        from src.db import get_system_db
        from src.repositories.user_group_members import UserGroupMembersRepository
        from src.repositories.user_groups import UserGroupsRepository
        from src.repositories.users import UserRepository

        conn = get_system_db()
        try:
            UserRepository(conn).create(id="u_finance_preview", email="finance-preview@example.com", name="Finance")
            gid = UserGroupsRepository(conn).create(name="Finance")["id"]
            UserGroupMembersRepository(conn).add_member("u_finance_preview", gid, source="admin")
        finally:
            conn.close()

        c = policied_invoices_for_preview["client"]
        token = policied_invoices_for_preview["admin_token"]
        resp = c.post(
            "/api/admin/registry/preview_invoices/policy/preview",
            json={"as_user": "finance-preview@example.com"},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["rows_visible"] == 2

    def test_preview_as_a_user_in_a_wildcard_named_group_is_refused(self, policied_invoices_for_preview):
        """`as_groups` is checked for `%`/`_` because a wildcard-named group
        silently widens a LIKE-adjacent policy — but the LIVE resolver
        (`src/access_policy.py`) raises `PolicyError` for ANY bound group
        name carrying one, and the `as_user` branch bound a real user's
        live group names unchecked. A user in a group named `R&D%` would
        preview a slice the product can never actually serve: the preview
        succeeds, every real read by that user fails."""
        from src.db import get_system_db
        from src.repositories.user_group_members import UserGroupMembersRepository
        from src.repositories.user_groups import UserGroupsRepository
        from src.repositories.users import UserRepository

        conn = get_system_db()
        try:
            UserRepository(conn).create(id="u_wildcard", email="wildcard@example.com", name="Wildcard")
            gid = UserGroupsRepository(conn).create(name="R&D%")["id"]
            UserGroupMembersRepository(conn).add_member("u_wildcard", gid, source="admin")
        finally:
            conn.close()

        c = policied_invoices_for_preview["client"]
        token = policied_invoices_for_preview["admin_token"]
        resp = c.post(
            "/api/admin/registry/preview_invoices/policy/preview",
            json={"as_user": "wildcard@example.com"},
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "policy_preview_unsafe_live_group_name" in resp.text
        assert "R&D%" in resp.text

    def test_preview_as_a_wildcard_group_user_is_fine_when_the_policy_ignores_groups(
        self, policied_invoices_for_preview
    ):
        """Mirrors the resolver exactly: it only rejects the name when the
        policy actually binds `$user_groups`. A policy that never
        references them serves that user fine live, so the preview must
        not invent a rejection."""
        from src.db import get_system_db
        from src.repositories.user_group_members import UserGroupMembersRepository
        from src.repositories.user_groups import UserGroupsRepository
        from src.repositories.users import UserRepository

        conn = get_system_db()
        try:
            UserRepository(conn).create(id="u_wildcard2", email="wildcard2@example.com", name="Wildcard2")
            gid = UserGroupsRepository(conn).create(name="Ops%")["id"]
            UserGroupMembersRepository(conn).add_member("u_wildcard2", gid, source="admin")
        finally:
            conn.close()

        c = policied_invoices_for_preview["client"]
        token = policied_invoices_for_preview["admin_token"]
        resp = c.post(
            "/api/admin/registry/preview_invoices/policy/preview",
            json={
                "as_user": "wildcard2@example.com",
                "sql": "SELECT * EXCLUDE (secret) FROM preview_invoices WHERE unit = 'Finance'",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["rows_visible"] == 2

    def test_preview_unknown_as_user_is_404(self, policied_invoices_for_preview):
        c = policied_invoices_for_preview["client"]
        token = policied_invoices_for_preview["admin_token"]
        resp = c.post(
            "/api/admin/registry/preview_invoices/policy/preview",
            json={"as_user": "nobody@example.com"},
            headers=_auth(token),
        )
        assert resp.status_code == 404, resp.text

    def test_preview_requires_a_persona(self, policied_invoices_for_preview):
        c = policied_invoices_for_preview["client"]
        token = policied_invoices_for_preview["admin_token"]
        resp = c.post(
            "/api/admin/registry/preview_invoices/policy/preview",
            json={},
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "policy_preview_persona_required" in resp.text

    def test_preview_rejects_both_persona_selectors_at_once(self, policied_invoices_for_preview):
        c = policied_invoices_for_preview["client"]
        token = policied_invoices_for_preview["admin_token"]
        resp = c.post(
            "/api/admin/registry/preview_invoices/policy/preview",
            json={"as_user": "admin@test.com", "as_groups": ["Finance"]},
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "policy_preview_persona_conflict" in resp.text

    def test_preview_404_for_unknown_table(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.post(
            "/api/admin/registry/does-not-exist/policy/preview",
            json={"as_groups": ["X"]},
            headers=_auth(token),
        )
        assert resp.status_code == 404, resp.text

    def test_preview_422_when_no_stored_policy_and_no_candidate_sql(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        table_id = _register(c, token, name="no_policy_tbl")

        resp = c.post(
            f"/api/admin/registry/{table_id}/policy/preview",
            json={"as_groups": ["X"]},
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert "policy_preview_no_policy" in resp.text


@pytest.fixture
def policied_wide_table_for_preview(seeded_app, mock_extract_factory, monkeypatch):
    """A table with MORE rows than ``_POLICY_PREVIEW_SAMPLE_LIMIT`` where
    the only rows a persona can see sit *outside* the raw sample window.

    This is the shape that breaks a naive before/after preview: the raw
    sample is ``... LIMIT 20`` and the policied sample is an independent
    ``SELECT * FROM (policy) LIMIT 20``, so the two lists can cover
    disjoint sets of source rows and the UI ends up diffing unrelated
    rows against each other.
    """
    from src.db import get_system_db
    from src.orchestrator import SyncOrchestrator
    from src.repositories.table_registry import TableRegistryRepository

    monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")

    env = seeded_app["env"]
    rows = [{"id": f"{i:03d}", "unit": "Ops" if i < 25 else "Finance"} for i in range(30)]
    mock_extract_factory("keboola", [{"name": "preview_wide", "data": rows}])
    SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

    conn = get_system_db()
    try:
        registry = TableRegistryRepository(conn)
        registry.register(
            id="preview_wide",
            name="preview_wide",
            source_type="keboola",
            query_mode="local",
            server_only=True,
        )
        registry.set_access_policy(
            "preview_wide",
            sql="SELECT * FROM preview_wide WHERE list_contains($user_groups, unit)",
            note="restrict to the caller's unit",
            updated_by="admin",
        )
    finally:
        conn.close()

    return seeded_app


@pytest.mark.journey
class TestPolicyPreviewSampleWindow:
    def test_both_samples_cover_the_same_bounded_rows(self, policied_wide_table_for_preview):
        """The before/after view is only meaningful if the "after" list is
        the policy applied to the SAME rows the "before" list shows.

        Here the Finance rows all sit past the sample window, so an
        independent ``SELECT * FROM (policy) LIMIT 20`` returns rows the
        raw sample never contains -- and the UI pairs unrelated rows,
        rendering false "dropped" rows and false masked-cell diffs. Every
        policied sample row must come from the raw sample window.
        """
        c = policied_wide_table_for_preview["client"]
        token = policied_wide_table_for_preview["admin_token"]

        resp = c.post(
            "/api/admin/registry/preview_wide/policy/preview",
            json={"as_groups": ["Finance"]},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        # The counts are still whole-table facts, not window facts.
        assert body["rows_total"] == 30
        assert body["rows_visible"] == 5

        base_ids = {r["id"] for r in body["base_sample_rows"]}
        sample_ids = {r["id"] for r in body["sample_rows"]}
        assert len(base_ids) == 20
        assert sample_ids <= base_ids, (
            f"policied sample escaped the raw sample window: {sorted(sample_ids - base_ids)} are not in the before list"
        )
        # ... and the response says so, so the UI knows it may diff them.
        assert body["base_sample_comparable"] is True

    def test_a_narrow_table_still_diffs_the_whole_table(self, policied_invoices_for_preview):
        """The 3-row fixture is smaller than the sample limit, so the raw
        sample IS the table and the policied sample IS the whole policied
        output -- the diff stays exact and still shows the dropped row."""
        c = policied_invoices_for_preview["client"]
        token = policied_invoices_for_preview["admin_token"]

        resp = c.post(
            "/api/admin/registry/preview_invoices/policy/preview",
            json={"as_groups": ["Finance"]},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["base_sample_comparable"] is True
        assert {r["id"] for r in body["base_sample_rows"]} == {"1", "2", "3"}
        assert {r["id"] for r in body["sample_rows"]} == {"1", "2"}

    def test_a_policy_whose_table_reads_cannot_be_bounded_is_flagged(self, policied_wide_table_for_preview):
        """A qualified self-reference (``main.t``) binds to the real table,
        not to the bounded-sample CTE -- so the two lists may cover
        different rows and the response must say so instead of inviting a
        false diff. Never resolved by rewriting the policy body: editing
        untrusted SQL by string substitution is exactly the footgun this
        avoids."""
        c = policied_wide_table_for_preview["client"]
        token = policied_wide_table_for_preview["admin_token"]

        resp = c.post(
            "/api/admin/registry/preview_wide/policy/preview",
            json={
                "sql": "SELECT * FROM main.preview_wide WHERE list_contains($user_groups, unit)",
                "as_groups": ["Finance"],
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["base_sample_comparable"] is False
