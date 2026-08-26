"""POST /api/admin/server-config refuses an unconfirmed connection repoint.

Changing a connection coordinate under ``data_source.<source>`` — Snowflake's
account/database/warehouse, BigQuery's project, Databricks' host — silently
invalidates every registry row of that source. The stored view still names the
old database/schema, so a ``query_mode='remote'`` read fails at bind time
(``schema "X" does not exist``) and a materialized sync fails at COPY time,
while ``last_sync_status`` keeps reporting the last *successful* run. Nothing
in the save path told the operator how many tables they were about to break.

The guard counts the affected registrations, names the coordinates that
change, and refuses with 409 until the caller resends with
``confirm_connection_change: true``.
"""

import pytest
import yaml

from app.connection_identity import CONNECTION_IDENTITY_LEAVES, identity_changes


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


SNOWFLAKE_BEFORE = {
    "account": "acme-prod123",
    "user": "AGNES_SVC",
    "database": "PROD",
    "warehouse": "ANALYTICS_WH",
    "auth_type": "key_pair",
}


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    """DATA_DIR with an instance.yaml overlay carrying a Snowflake connection."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    path = state / "instance.yaml"
    path.write_text(yaml.dump({"data_source": {"snowflake": dict(SNOWFLAKE_BEFORE)}}))

    from app.instance_config import reset_cache

    reset_cache()
    yield path
    reset_cache()


@pytest.fixture
def registered_snowflake_tables():
    """Two Snowflake registrations that a repoint would invalidate."""
    from src.repositories import table_registry_repo

    repo = table_registry_repo()
    ids = ["cguard_gold_orders", "cguard_gold_invoices"]
    for table_id in ids:
        repo.register(
            id=table_id,
            name=table_id,
            source_type="snowflake",
            bucket="GOLD",
            source_table=table_id.upper(),
            query_mode="remote",
        )
    yield ids
    for table_id in ids:
        try:
            repo.unregister(table_id)
        except Exception:
            pass


class TestConnectionRepointGuard:
    def test_repoint_with_registrations_is_refused(self, seeded_app, overlay, registered_snowflake_tables):
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"data_source": {"snowflake": {"database": "GOLD"}}}},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "connection_change_affects_registrations"
        assert detail["source"] == "snowflake"
        assert detail["affected_tables"] == len(registered_snowflake_tables)
        assert set(detail["sample_tables"]) <= set(registered_snowflake_tables)
        assert detail["changes"] == [{"field": "database", "before": "PROD", "after": "GOLD"}]
        # The refusal must name the way out, not just say no.
        assert "confirm_connection_change" in detail["hint"]

    def test_refused_repoint_leaves_the_overlay_untouched(self, seeded_app, overlay, registered_snowflake_tables):
        c = seeded_app["client"]
        before = overlay.read_text()
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"data_source": {"snowflake": {"account": "other-acct"}}}},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 409, resp.text
        assert overlay.read_text() == before

    def test_repoint_applies_when_confirmed(self, seeded_app, overlay, registered_snowflake_tables):
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/server-config",
            json={
                "sections": {"data_source": {"snowflake": {"database": "GOLD"}}},
                "confirm_connection_change": True,
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200, resp.text
        written = yaml.safe_load(overlay.read_text())
        assert written["data_source"]["snowflake"]["database"] == "GOLD"

    def test_non_identity_leaf_is_not_guarded(self, seeded_app, overlay, registered_snowflake_tables):
        """A tuning knob is not a repoint — it must save without confirmation."""
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"data_source": {"snowflake": {"max_bytes_per_materialize": 1024}}}},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200, resp.text

    def test_unchanged_identity_leaf_is_not_guarded(self, seeded_app, overlay, registered_snowflake_tables):
        """Re-sending the same value (the GET round-trip) is not a change."""
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"data_source": {"snowflake": dict(SNOWFLAKE_BEFORE)}}},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200, resp.text

    def test_repoint_without_registrations_is_not_guarded(self, seeded_app, overlay):
        """First-time setup has nothing to break, so it must not nag."""
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/server-config",
            json={"sections": {"data_source": {"snowflake": {"database": "GOLD"}}}},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200, resp.text


class TestIdentityChanges:
    def test_reports_only_identity_leaves(self):
        changes = identity_changes(
            "snowflake",
            {"database": "PROD", "max_bytes_per_materialize": 1},
            {"database": "GOLD", "max_bytes_per_materialize": 2},
        )
        assert changes == [{"field": "database", "before": "PROD", "after": "GOLD"}]

    def test_setting_a_previously_unset_coordinate_counts(self):
        """`role: unset -> AGNES_SVC` changes which grants apply — it is a repoint.

        Reported as `"" -> "AGNES_SVC"`: the effective previous value, which is
        what the instance was actually connecting with."""
        changes = identity_changes("snowflake", {}, {"role": "AGNES_SVC"})
        assert changes == [{"field": "role", "before": "", "after": "AGNES_SVC"}]

    def test_a_leaf_left_at_its_default_is_not_a_change_when_resent_explicitly(self):
        """The setup wizard always writes `token_env: KEBOOLA_STORAGE_TOKEN` and
        the Add-data wizard always writes `auth_type` — on an instance that
        relied on those defaults, comparing raw values scored a phantom repoint,
        and `token_env` masks to `***` so the refusal explained nothing."""
        assert identity_changes("keboola", {}, {"token_env": "KEBOOLA_STORAGE_TOKEN"}) == []
        assert identity_changes("snowflake", {}, {"auth_type": "password"}) == []
        assert identity_changes("databricks", {}, {"token_env": "DATABRICKS_TOKEN"}) == []

    def test_blank_and_absent_mean_the_same_thing(self):
        """Readers resolve both as `get_value(...) or DEFAULT`, so a guard that
        told them apart would fire on a difference the running system cannot
        see."""
        assert identity_changes("snowflake", {"role": ""}, {"role": None}) == []
        assert identity_changes("snowflake", {"token_env": ""}, {"token_env": "SNOWFLAKE_PASSWORD"}) == []

    def test_moving_off_a_default_is_still_a_change(self):
        assert identity_changes("keboola", {}, {"token_env": "OTHER_TOKEN"}) == [
            {"field": "token_env", "before": "KEBOOLA_STORAGE_TOKEN", "after": "OTHER_TOKEN"}
        ]

    def test_bigquery_location_is_not_connection_identity(self):
        """A region is not a coordinate a registration resolves against, and a
        wrong one fails loudly at query time rather than silently. Guarding it
        also produced a standing false positive: the setup form pre-fills `us`,
        so an instance that never set a region was told every BigQuery table
        would break on a save that changed no project."""
        assert identity_changes("bigquery", {}, {"location": "us"}) == []
        assert identity_changes("bigquery", {"project": "a"}, {"project": "b", "location": "eu"}) == [
            {"field": "project", "before": "a", "after": "b"}
        ]

    def test_unknown_source_has_no_identity(self):
        assert identity_changes("nope", {"host": "a"}, {"host": "b"}) == []


class TestConfigureWizardIsGuardedToo:
    """POST /api/admin/configure writes the same `data_source.<source>` block,
    so it must not be a way around the confirmation."""

    @pytest.fixture
    def bigquery_overlay(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "instance.yaml").write_text(
            yaml.dump(
                {
                    "data_source": {
                        "type": "bigquery",
                        "bigquery": {"project": "proj-before", "location": "us"},
                    }
                }
            )
        )
        from app.instance_config import reset_cache

        reset_cache()
        yield state / "instance.yaml"
        reset_cache()

    @pytest.fixture
    def registered_bq_table(self):
        from src.repositories import table_registry_repo

        repo = table_registry_repo()
        repo.register(
            id="cguard_bq_sessions",
            name="cguard_bq_sessions",
            source_type="bigquery",
            bucket="analytics",
            source_table="SESSIONS",
            query_mode="remote",
        )
        yield "cguard_bq_sessions"
        try:
            repo.unregister("cguard_bq_sessions")
        except Exception:
            pass

    def test_wizard_repoint_is_refused(self, seeded_app, bigquery_overlay, registered_bq_table):
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/configure",
            json={"data_source": "bigquery", "bigquery_project": "proj-after"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "connection_change_affects_registrations"
        assert detail["source"] == "bigquery"
        assert detail["affected_tables"] >= 1
        assert {"field": "project", "before": "proj-before", "after": "proj-after"} in detail["changes"]
        assert yaml.safe_load(bigquery_overlay.read_text())["data_source"]["bigquery"]["project"] == "proj-before"

    def test_wizard_resave_with_only_a_region_is_not_a_repoint(self, seeded_app, bigquery_overlay, registered_bq_table):
        """The setup form pre-fills `us`, so re-confirming an existing project
        must not claim every registered table is about to break."""
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/configure",
            json={
                "data_source": "bigquery",
                "bigquery_project": "proj-before",
                "bigquery_location": "us",
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200, resp.text

    def test_wizard_repoint_applies_when_confirmed(
        self, seeded_app, bigquery_overlay, registered_bq_table
    ):
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/configure",
            json={
                "data_source": "bigquery",
                "bigquery_project": "proj-after",
                "confirm_connection_change": True,
            },
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200, resp.text
        written = yaml.safe_load(bigquery_overlay.read_text())
        assert written["data_source"]["bigquery"]["project"] == "proj-after"

    def test_wizard_first_time_setup_is_not_guarded(self, seeded_app, tmp_path, monkeypatch):
        """No registrations yet — the wizard must stay a straight line."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)
        from app.instance_config import reset_cache

        reset_cache()
        c = seeded_app["client"]
        resp = c.post(
            "/api/admin/configure",
            json={"data_source": "bigquery", "bigquery_project": "proj-fresh"},
            headers=_auth(seeded_app["admin_token"]),
        )
        reset_cache()
        assert resp.status_code == 200, resp.text


class TestEveryConnectionSaveSurfaceIsWired:
    """The 409 is only half the guard. Four UI surfaces write a `data_source`
    connection, and a confirm flow taught to only one of them turns the other
    three into dead ends — an operator rotating a credential from the Add-data
    wizard would be stopped by a refusal that page cannot answer."""

    SURFACES = [
        "app/web/templates/admin_server_config.html",  # instance settings
        "app/web/templates/admin_data_sources.html",  # Snowflake + Databricks wizards
        "app/web/templates/setup.html",  # first-boot / re-run setup
    ]

    @pytest.mark.parametrize("template", SURFACES)
    def test_surface_saves_through_the_shared_confirm_helper(self, template):
        from pathlib import Path

        body = Path(template).read_text()
        assert "saveConnectionConfig" in body, (
            f"{template} writes a data_source connection but does not go through "
            "saveConnectionConfig, so it cannot answer the repoint 409"
        )

    @pytest.mark.parametrize("template", SURFACES)
    def test_surface_renders_an_object_detail_instead_of_stringifying_it(self, template):
        """This 409's `detail` is an object (a 422's is an array); interpolating
        either into a template literal prints "[object Object]" at the exact
        moment the operator needs to read why."""
        from pathlib import Path

        body = Path(template).read_text()
        assert "apiDetailText" in body

    def test_the_helper_is_loaded_on_every_page(self):
        from pathlib import Path

        assert "js/connection_repoint.js" in Path("app/web/templates/_app_scripts.html").read_text()

    def test_the_helper_confirms_and_retries(self):
        from pathlib import Path

        helper = Path("app/web/static/js/connection_repoint.js").read_text()
        assert "connection_change_affects_registrations" in helper
        assert "confirm_connection_change" in helper
        # Declining must be a no-op the caller can tell apart from a failure.
        assert "cancelled" in helper

    def test_the_page_actually_serves_the_helper(self, seeded_app):
        c = seeded_app["client"]
        c.cookies.set("access_token", seeded_app["admin_token"])
        try:
            body = c.get("/admin/server-config", headers={"Accept": "text/html"}).text
        finally:
            c.cookies.clear()
        assert "connection_repoint.js" in body


class TestIdentityMapCoverage:
    def test_every_config_backed_connector_declares_its_identity_leaves(self):
        """Ratchet: a new connector reading `data_source.<name>.*` must declare
        which of its leaves are connection identity, or this guard silently
        stops covering it."""
        import re
        from pathlib import Path

        pattern = re.compile(r'get_value\(\s*"data_source",\s*"([a-z_]+)"')
        found: set[str] = set()
        for path in Path("connectors").rglob("*.py"):
            found |= set(pattern.findall(path.read_text()))

        missing = sorted(found - set(CONNECTION_IDENTITY_LEAVES))
        assert not missing, (
            f"connector(s) {missing} read data_source config but declare no "
            "identity leaves in app/connection_identity.py — a repoint of "
            "their connection would break registrations unguarded"
        )
