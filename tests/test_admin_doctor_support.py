"""Tests for GET /api/admin/doctor/support — the support-bundle doctor.

The endpoint feeds `agnes doctor`'s server section: one structured, redacted
snapshot an operator can attach to a support ticket. The tests pin the three
things the feature exists for:

- admin-only (the payload enumerates security posture — build fingerprints,
  secret presence, schema state);
- **redacted** — secret *values* must never appear anywhere in the body,
  only presence booleans;
- degradations are loud — `retrieval.mode == "lexical_only"` carries a
  non-ok verdict instead of hiding in a field nobody reads (#898).

Design: docs/superpowers/specs/2026-08-23-support-bundle-doctor-design.md.
"""

from unittest.mock import patch


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _run(client, token):
    resp = client.get("/api/admin/doctor/support", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestSupportDoctorAuth:
    def test_unauthenticated_is_rejected(self, seeded_app):
        resp = seeded_app["client"].get("/api/admin/doctor/support")
        assert resp.status_code == 401

    def test_non_admin_is_rejected(self, seeded_app):
        resp = seeded_app["client"].get("/api/admin/doctor/support", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 403

    def test_admin_can_run(self, seeded_app):
        report = _run(seeded_app["client"], seeded_app["admin_token"])
        assert "generated_at" in report


class TestSupportDoctorShape:
    def test_all_sections_present(self, seeded_app):
        report = _run(seeded_app["client"], seeded_app["admin_token"])
        for key in ("build", "schema", "retrieval", "sync", "disk", "process", "secrets"):
            assert key in report, f"section {key!r} missing: {sorted(report)}"

    def test_build_section_carries_version_fields(self, seeded_app):
        build = _run(seeded_app["client"], seeded_app["admin_token"])["build"]
        for key in ("version", "channel", "image_tag", "commit_sha", "deployed_at"):
            assert key in build

    def test_disk_section_reports_data_dir_usage(self, seeded_app):
        disk = _run(seeded_app["client"], seeded_app["admin_token"])["disk"]
        assert disk["total_bytes"] > 0
        assert disk["free_bytes"] >= 0
        assert "data_dir" in disk

    def test_disk_system_db_honors_state_dir_override(self, tmp_path, monkeypatch):
        # STATE_DIR relocates system.duckdb out of ${DATA_DIR}/state
        # (src.db._get_state_dir); the disk section must size the file where
        # get_system_db() actually keeps it, not the default nested path.
        from app.services.support_bundle import _collect_disk

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        state_dir = tmp_path / "relocated-state"
        state_dir.mkdir()
        (state_dir / "system.duckdb").write_bytes(b"x" * 1234)
        monkeypatch.setenv("DATA_DIR", str(data_dir))
        monkeypatch.setenv("STATE_DIR", str(state_dir))

        disk = _collect_disk()
        assert disk["system_db_bytes"] == 1234

    def test_process_section_names_backend_and_roles(self, seeded_app):
        proc = _run(seeded_app["client"], seeded_app["admin_token"])["process"]
        assert proc["state_backend"] in ("duckdb", "postgres")
        assert isinstance(proc["roles"], list) and proc["roles"]

    def test_crashing_collector_reports_itself(self, seeded_app):
        # One crashing resolver must degrade to an error entry, not a 500 —
        # the whole point of a doctor is answering while things are broken.
        with patch(
            "app.services.support_bundle._collect_disk",
            side_effect=RuntimeError("boom"),
        ):
            report = _run(seeded_app["client"], seeded_app["admin_token"])
        assert report["disk"]["status"] == "error"
        assert "boom" in report["disk"]["detail"]


class TestSupportDoctorRetrieval:
    def test_lexical_only_is_a_loud_warning(self, seeded_app):
        with patch("src.ingest.retrieval.retrieval_mode", return_value="lexical_only"):
            retrieval = _run(seeded_app["client"], seeded_app["admin_token"])["retrieval"]
        assert retrieval["mode"] == "lexical_only"
        assert retrieval["status"] == "warning"

    def test_hybrid_is_ok(self, seeded_app):
        with patch("src.ingest.retrieval.retrieval_mode", return_value="hybrid"):
            retrieval = _run(seeded_app["client"], seeded_app["admin_token"])["retrieval"]
        assert retrieval["mode"] == "hybrid"
        assert retrieval["status"] == "ok"


class TestSupportDoctorSync:
    def test_rollup_groups_by_source_and_carries_last_errors(self, seeded_app):
        from src.repositories import sync_state_repo, table_registry_repo

        registry = table_registry_repo()
        registry.register(id="orders", name="orders", source_type="keboola")
        registry.register(id="events", name="events", source_type="keboola")
        registry.register(id="tickets", name="tickets", source_type="jira")

        state = sync_state_repo()
        state.update_sync("orders", rows=10, file_size_bytes=100, hash="h1")
        state.set_error("events", "extract exploded: connection reset")
        state.update_sync("tickets", rows=5, file_size_bytes=50, hash="h2")

        sync = _run(seeded_app["client"], seeded_app["admin_token"])["sync"]
        sources = sync["sources"]
        assert sources["keboola"]["tables"] == 2
        assert sources["keboola"]["errors"] == 1
        assert sources["jira"]["errors"] == 0
        failing = sources["keboola"]["last_errors"]
        assert failing and failing[0]["table_id"] == "events"
        assert "connection reset" in failing[0]["error"]

    def test_sync_state_is_keyed_by_name_not_id(self, seeded_app):
        """`sync_state.table_id` mirrors `table_registry.name`, never `id`.

        The convention is documented at `app/api/sync.py`'s manifest builder
        and `app/api/admin.py`'s registry join, and every writer obeys it
        (`_meta.table_name` → `update_sync`/`set_error`). Nothing enforces
        disjoint id/name namespaces, though: a PUT-rename leaves `id` fixed
        while `name` moves, so one table's `id` can equal another's `name`.
        An id-keyed lookup then returns a truthy but WRONG row and silently
        reports one source's failure against a different source — the worst
        failure mode for an artifact whose whole job is to be trusted in a
        support ticket.
        """
        from src.repositories import sync_state_repo, table_registry_repo

        registry = table_registry_repo()
        # `collide` is the *id* of a bigquery table that never synced, and
        # also the *name* of a keboola table that failed.
        registry.register(id="collide", name="Renamed BQ Table", source_type="bigquery")
        registry.register(id="kbc_row", name="collide", source_type="keboola")
        sync_state_repo().set_error("collide", "keboola extract failed")

        sync = _run(seeded_app["client"], seeded_app["admin_token"])["sync"]
        assert sync["sources"]["keboola"]["errors"] == 1
        # The bigquery table must NOT inherit keboola's failure.
        assert sync["sources"]["bigquery"]["errors"] == 0
        assert sync["sources"]["bigquery"]["never_synced"] == 1
        assert sync["sources"]["bigquery"]["last_errors"] == []

    def test_registered_but_never_synced_table_still_counts(self, seeded_app):
        from src.repositories import table_registry_repo

        table_registry_repo().register(id="fresh", name="fresh", source_type="bigquery")
        sync = _run(seeded_app["client"], seeded_app["admin_token"])["sync"]
        assert sync["sources"]["bigquery"]["tables"] == 1
        assert sync["sources"]["bigquery"]["never_synced"] == 1


class TestSupportDoctorRedaction:
    SENTINEL = "sk-SENTINEL-NEVER-IN-OUTPUT-1234567890"

    def test_secret_values_never_appear_in_body(self, seeded_app, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", self.SENTINEL)
        resp = seeded_app["client"].get("/api/admin/doctor/support", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        assert self.SENTINEL not in resp.text

    def test_upstream_error_text_cannot_smuggle_a_password_out(self, seeded_app):
        """The one field here that is not ours: a driver's own error string.

        An upstream failure routinely quotes the connection URL with the
        password in the userinfo position. The CLI scrubs the bundle it
        writes, but the endpoint must not hand a credential to any consumer
        — a later admin UI panel would not know to scrub.
        """
        from src.repositories import sync_state_repo, table_registry_repo

        table_registry_repo().register(id="leaky", name="leaky", source_type="keboola")
        sync_state_repo().set_error("leaky", "could not connect: postgres://agnes:s3cr3tpw@10.0.0.1:5432/db")

        resp = seeded_app["client"].get("/api/admin/doctor/support", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        assert "s3cr3tpw" not in resp.text
        # The rest of the message survives — it is the diagnostic value.
        assert "10.0.0.1:5432" in resp.text

    def test_a_crashing_collector_cannot_leak_a_credential_either(self, seeded_app):
        """The containment path is a credential route, not just an error path.

        Every collector's exception funnels through one wrapper, and
        ``_collect_schema`` reaches the PG engine — whose driver exceptions
        are known to quote the connection URL. Scrubbing only the one field
        with a reproducing test would leave the generic path open, including
        for collectors added later.
        """
        boom = RuntimeError("could not connect: postgres://agnes:s3cr3tpw@10.0.0.1:5432/db")
        with patch("app.services.support_bundle._collect_schema", side_effect=boom):
            resp = seeded_app["client"].get("/api/admin/doctor/support", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        assert "s3cr3tpw" not in resp.text
        assert resp.json()["schema"]["status"] == "error"

    def test_secrets_section_reports_presence_only(self, seeded_app, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", self.SENTINEL)
        monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
        secrets = _run(seeded_app["client"], seeded_app["admin_token"])["secrets"]
        assert secrets["OPENAI_API_KEY"] is True
        assert secrets["SENDGRID_API_KEY"] is False
        for value in secrets.values():
            assert isinstance(value, bool)
