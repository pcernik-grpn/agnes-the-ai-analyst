"""Extraction observability API (2026-08-31 design §9, A1/A2/A3/A5).

Two layers, both DuckDB-backed (the default test backend):

* the route contract — admin gating, 404-before-any-repo-work, and the typed
  ``501 requires_postgres_backend`` the three run routes owe a DuckDB
  instance (``extraction_runs`` is a post-A3 PG-only table);
* the pure read-side rules — outcome precedence and derived liveness — as
  unit tests over ``_derived_outcome``, because those are the rules that keep
  a card from rendering a crashed or abandoned run as healthy, and they must
  be checkable without a database.

The PG happy path (a recorded run actually surfacing through the API) lives
in ``tests/db_pg/test_extraction_api_pg.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

BASE = "/api/admin/sharepoint/connections"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_connection(client, token, *, name="corp-sharepoint"):
    resp = client.post(
        "/api/admin/source-connections",
        json={
            "name": name,
            "source_type": "sharepoint",
            "config": {"tenant_id": "tenant-1", "client_id": "client-1"},
        },
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


_ROUTES = (
    "extraction/status",
    "extraction/runs",
    "extraction/runs/er_whatever",
    "extraction/config",
)


class TestAuthGating:
    def test_every_route_requires_auth(self, seeded_app):
        for suffix in _ROUTES:
            r = seeded_app["client"].get(f"{BASE}/nope/{suffix}")
            assert r.status_code == 401, suffix

    def test_every_route_requires_admin(self, seeded_app):
        token = seeded_app["analyst_token"]
        for suffix in _ROUTES:
            r = seeded_app["client"].get(f"{BASE}/nope/{suffix}", headers=_auth(token))
            assert r.status_code == 403, suffix


class TestUnknownConnection:
    def test_unknown_connection_is_404_not_501(self, seeded_app):
        """The connection lookup runs BEFORE any PG-only repo, so a typo'd
        id is a 404 on every backend — a 501 would tell an admin to migrate
        their database over a misspelling."""
        client, token = seeded_app["client"], seeded_app["admin_token"]
        for suffix in _ROUTES:
            r = client.get(f"{BASE}/does-not-exist/{suffix}", headers=_auth(token))
            assert r.status_code == 404, (suffix, r.status_code)
            assert r.json()["detail"] == "connection_not_found"

    def test_non_sharepoint_connection_is_404(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        created = client.post(
            "/api/admin/source-connections",
            json={"name": "kbc", "source_type": "keboola", "config": {"stack_url": "https://connection.example.com"}},
            headers=_auth(token),
        )
        assert created.status_code == 201, created.text
        r = client.get(f"{BASE}/{created.json()['id']}/extraction/status", headers=_auth(token))
        assert r.status_code == 404


class TestDuckDbDegradesCleanly:
    """A3 ratchet: `extraction_runs` is PG-only, so a DuckDB instance gets a
    TYPED 501 the card can recognize and stop polling on — never a raw 500,
    and never an empty-but-healthy-looking answer."""

    def test_status_is_typed_501(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token)
        r = client.get(f"{BASE}/{conn_id}/extraction/status", headers=_auth(token))
        assert r.status_code == 501
        body = r.json()
        assert body["error"] == "requires_postgres_backend"
        assert body["feature"] == "extraction_runs"

    def test_runs_list_is_typed_501(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="sp-runs")
        r = client.get(f"{BASE}/{conn_id}/extraction/runs", headers=_auth(token))
        assert r.status_code == 501
        assert r.json()["error"] == "requires_postgres_backend"

    def test_run_detail_is_typed_501(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="sp-run-detail")
        r = client.get(f"{BASE}/{conn_id}/extraction/runs/er_x", headers=_auth(token))
        assert r.status_code == 501
        assert r.json()["error"] == "requires_postgres_backend"


class TestExtractionConfig:
    """The config read-out touches no run rows, so it answers on BOTH
    backends: an admin locked out of reading their own configuration because
    the instance is on DuckDB would be a degradation with no cause."""

    def test_answers_200_on_duckdb(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="sp-config")
        r = client.get(f"{BASE}/{conn_id}/extraction/config", headers=_auth(token))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["connection_id"] == conn_id
        # Editability is read from the registry at render time (UX review
        # M5): with the producer command line gone, the section is
        # admin-editable and the drawer must not claim otherwise.
        assert body["section_editable"] is True
        assert body["section_lock_reason"] is None
        assert body["as_of"]

    def test_every_row_names_its_origin_and_lock_state(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="sp-config-rows")
        rows = client.get(f"{BASE}/{conn_id}/extraction/config", headers=_auth(token)).json()["effective"]
        assert rows
        for row in rows:
            assert row["origin"] in ("env", "yaml", "default", "builtin"), row
            assert isinstance(row["editable"], bool)
            if not row["editable"]:
                assert row["lock_reason"], row

    def test_unset_value_reads_as_default_not_as_yaml(self, seeded_app):
        """ "50 MB because that is the default" and "50 MB because someone
        chose it" are different facts, and the drawer must not blur them."""
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="sp-config-default")
        rows = client.get(f"{BASE}/{conn_id}/extraction/config", headers=_auth(token)).json()["effective"]
        by_key = {r["key"]: r for r in rows if r["key"]}
        cap = by_key["extraction.crawler.max_file_mb"]
        assert cap["origin"] == "default"
        assert cap["value"] == 50

    def test_detector_defaults_to_regex_and_says_no_tokens_are_spent(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="sp-config-detector")
        rows = client.get(f"{BASE}/{conn_id}/extraction/config", headers=_auth(token)).json()["effective"]
        by_key = {r["key"]: r for r in rows if r["key"]}
        detector = by_key["extraction.anonymization.detector"]
        assert detector["value"] == "regex"
        assert detector["origin"] == "default"
        assert "No LLM call" in (detector["note"] or "")

    def test_the_detector_note_describes_the_value_actually_in_force(self, monkeypatch):
        """A drawer that explains `regex` while the instance is set to `llm`
        describes a pipeline nobody is running. The note follows the value."""
        import app.api.admin_extraction as mod

        def _row_for(value):
            monkeypatch.setattr(mod, "_config_row", lambda *a, **k: {"value": value, "note": None})
            return mod._detector_row()

        assert "No LLM call" in _row_for("regex")["note"]

        llm = _row_for("llm")["note"]
        # The llm setting ADDS the LLM tier to the regex one; saying it
        # replaces regex would understate what still runs deterministically.
        assert "not swapped out" in llm
        assert "Spends tokens" in llm

        # An unrecognized value is NOT an error at runtime: anything but
        # `llm` runs the regex tier. The note has to say both — the value is
        # wrong, AND here is what actually executes — or an operator is left
        # guessing whether anything ran at all.
        unknown = _row_for("magic")["note"]
        assert "unrecognized value" in unknown
        assert "runs the regex tier" in unknown
        assert "No LLM call" in unknown

    def test_the_detector_note_matches_the_way_the_runtime_normalizes(self):
        """`crawler._entity_detector` lowercases and strips before comparing
        against "llm". A drawer that read "LLM" as unrecognized would
        disagree with the engine it is describing."""
        import app.api.admin_extraction as mod

        def _row_for(value):
            import unittest.mock as m

            with m.patch.object(mod, "_config_row", lambda *a, **k: {"value": value, "note": None}):
                return mod._detector_row()

        for spelling in ("llm", "LLM", " llm ", "Llm"):
            assert "not swapped out" in _row_for(spelling)["note"], spelling

    def test_an_unset_detector_reads_as_the_deterministic_tier(self):
        """An empty value resolves to the regex tier at runtime, so it must
        not be reported as an unrecognized one."""
        import app.api.admin_extraction as mod

        def _row_for(value):
            import unittest.mock as m

            with m.patch.object(mod, "_config_row", lambda *a, **k: {"value": value, "note": None}):
                return mod._detector_row()

        for empty in ("", "   ", None):
            note = _row_for(empty)["note"]
            assert "No LLM call" in note, empty
            assert "unrecognized" not in note, empty

    def test_the_timeout_row_describes_the_in_process_ceiling(self, seeded_app):
        """It used to be a subprocess kill that did not apply to the built-in
        crawl; it now genuinely bounds the run. A note still saying the old
        thing would tell an operator a cap they set does nothing."""
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="sp-config-timeout")
        rows = client.get(f"{BASE}/{conn_id}/extraction/config", headers=_auth(token)).json()["effective"]
        by_key = {r["key"]: r for r in rows if r["key"]}
        note = by_key["extraction.timeout_s"]["note"]
        assert "not a subprocess" not in note
        assert "resumes" in note
        assert "0 = unbounded" in note

    def test_a_code_constant_and_an_unset_env_knob_are_told_apart(self):
        """ "built in" means there is nothing to set; "default" means nobody
        has set it. Collapsing the two would send an admin hunting for a knob
        that does not exist, or stop them setting one that does."""
        from app.api.admin_extraction import _config_row

        constant = _config_row("Checkpoint granularity", (), default="every 200 delta rows")
        assert constant["origin"] == "builtin"
        assert "no setting to change" in constant["lock_reason"]

        env_only = _config_row("Scan transcription model", (), env_var="AGNES_VISION_MODEL_UNSET_IN_TESTS")
        assert env_only["origin"] == "default"
        assert "AGNES_VISION_MODEL_UNSET_IN_TESTS" in env_only["lock_reason"]

    def test_env_set_value_is_reported_as_env_and_locked(self, seeded_app, monkeypatch):
        """An admin edit writes YAML, which the environment overrides —
        offering the edit would be offering a change that does nothing."""
        monkeypatch.setenv("AGNES_SHAREPOINT_ENABLED", "1")
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="sp-config-env")
        rows = client.get(f"{BASE}/{conn_id}/extraction/config", headers=_auth(token)).json()["effective"]
        by_key = {r["key"]: r for r in rows if r["key"]}
        enabled = by_key["sharepoint.enabled"]
        assert enabled["origin"] == "env"
        assert enabled["env_name"] == "AGNES_SHAREPOINT_ENABLED"
        assert enabled["editable"] is False
        assert "AGNES_SHAREPOINT_ENABLED" in enabled["lock_reason"]

    def test_no_producer_command_row_is_rendered(self, seeded_app):
        """The built-in pipeline has no producer command; rendering one an
        admin cannot change would be a pointer at an executable for nothing."""
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="sp-config-noproducer")
        rows = client.get(f"{BASE}/{conn_id}/extraction/config", headers=_auth(token)).json()["effective"]
        keys = [r["key"] for r in rows if r["key"]]
        assert not any(k.startswith("extraction.producer") for k in keys)

    def test_credential_env_vars_are_named_never_valued(self, seeded_app, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-do-not-leak-me")
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="sp-config-secret")
        raw = client.get(f"{BASE}/{conn_id}/extraction/config", headers=_auth(token)).text
        assert "sk-do-not-leak-me" not in raw


class TestDerivedOutcome:
    """Liveness is DERIVED, never trusted, and outcome precedence is
    severity-first — the two rules that stop a card from rendering a crashed
    or abandoned run as healthy."""

    def _run(self, **over):
        row = {
            "id": "er_1",
            "status": "running",
            "job_id": None,
            "checkpoint_at": datetime.now(timezone.utc).isoformat(),
        }
        row.update(over)
        return row

    def test_a_fresh_running_run_is_running(self):
        from app.api.admin_extraction import _derived_outcome

        out = _derived_outcome(self._run())
        assert out["outcome"] == "running"
        assert out["evidence"] is None

    def test_a_long_silent_running_run_is_stalled_with_its_age(self):
        from app.api.admin_extraction import _STALL_AFTER_S, _derived_outcome

        stale = datetime.now(timezone.utc) - timedelta(seconds=_STALL_AFTER_S + 600)
        out = _derived_outcome(self._run(checkpoint_at=stale.isoformat()))
        assert out["outcome"] == "stalled"
        assert out["stale_s"] > _STALL_AFTER_S
        # The card must be able to say WHY, not merely assert.
        assert "checkpoint" in out["evidence"]
        # The stored value is reported separately — the two are never merged.
        assert out["stored_status"] == "running"

    def test_a_run_whose_job_failed_renders_failed_not_running(self, monkeypatch):
        """A worker killed outright finalizes nothing. If the job it belonged
        to is already failed, the run is failed — severity wins."""
        import app.api.admin_extraction as mod

        monkeypatch.setattr(mod, "_job_status", lambda job_id: "failed")
        out = mod._derived_outcome(self._run(job_id="job_1"))
        assert out["outcome"] == "failed"
        assert "failed" in out["evidence"]

    def test_a_finalized_run_is_never_second_guessed(self, monkeypatch):
        import app.api.admin_extraction as mod

        monkeypatch.setattr(mod, "_job_status", lambda job_id: "failed")
        for stored in ("done", "interrupted", "failed"):
            out = mod._derived_outcome(self._run(status=stored, job_id="job_1"))
            assert out["outcome"] == stored

    def test_precedence_order_puts_failure_first(self):
        from app.api.admin_extraction import OUTCOME_PRECEDENCE

        assert OUTCOME_PRECEDENCE.index("failed") < OUTCOME_PRECEDENCE.index("interrupted")
        assert OUTCOME_PRECEDENCE.index("stalled") < OUTCOME_PRECEDENCE.index("done")


class TestRunProjection:
    def test_run_out_reports_absolute_counters_and_no_progress_fraction(self):
        """No percentage, no bar, no ETA: the crawl enumerates and processes
        in lockstep, and files_per_s counts only new+changed."""
        from app.api.admin_extraction import _run_out

        out = _run_out(
            {
                "id": "er_1",
                "status": "running",
                "checkpoint_at": datetime.now(timezone.utc).isoformat(),
                "files_done": 812,
                "files_seen": 812,
                "progress": {"new": 800, "unchanged": 12, "elapsed_s": 391.0, "http_429": 4},
            }
        )
        assert out["files_done"] == 812
        assert out["new"] == 800
        assert out["elapsed_s"] == 391.0
        assert out["http_429"] == 4
        for forbidden in ("percent", "progress_pct", "eta_s", "eta"):
            assert forbidden not in out

    def test_run_out_never_restamps_freshness(self):
        """`checkpoint_at` is when the numbers were last TRUE — a read must
        not quietly refresh it to now."""
        from app.api.admin_extraction import _run_out

        stamp = "2026-08-31T14:08:41+00:00"
        out = _run_out({"id": "er_1", "status": "done", "checkpoint_at": stamp, "report": {}})
        assert out["checkpoint_at"] == stamp

    def test_usage_empty_dict_survives_the_projection(self):
        from app.api.admin_extraction import _run_out

        out = _run_out({"id": "er_1", "status": "done", "report": {}, "usage": {}})
        assert out["usage"] == {}

    def test_a_recorded_stop_reason_is_surfaced(self):
        """A run that hit the timeout ceiling names its exit; an operator
        should never have to infer "it ended short" from a duration."""
        from app.api.admin_extraction import _run_out

        out = _run_out(
            {
                "id": "er_1",
                "status": "failed",
                "report": {"interrupted": True, "interrupted_reason": "timeout", "duration_s": 3600.0},
            }
        )
        assert out["interrupted_reason"] == "timeout"

    def test_a_timeout_is_resumable_even_though_it_finalized_as_failed(self):
        """The crawl persists deltaLinks/cTags on the way out of a timeout,
        so the next run costs re-work, not coverage. Keying the reassurance
        on the outcome WORD withheld it from exactly the case that earned
        it — an operator then re-runs a four-hour crawl out of doubt."""
        from app.api.admin_extraction import _run_out

        out = _run_out({"id": "er_1", "status": "failed", "report": {"interrupted_reason": "timeout"}})
        assert out["outcome"] == "failed"
        assert out["resumable"] is True

    def test_a_crash_is_never_claimed_resumable(self):
        """Nothing is known about how far the crawl state got before it
        died, and "your work is safe" must never be guessed."""
        from app.api.admin_extraction import _run_out

        for report in ({"interrupted_reason": "error"}, {}, {"interrupted_reason": None}):
            out = _run_out({"id": "er_1", "status": "failed", "report": report})
            assert out["resumable"] is False, report

    def test_a_cancelled_run_stays_resumable(self):
        from app.api.admin_extraction import _run_out

        out = _run_out({"id": "er_1", "status": "interrupted", "report": {}})
        assert out["resumable"] is True

    def test_a_throttle_abort_is_resumable(self):
        """A 429-budget abort stops at the same consistent point a timeout
        does — `_process_item` re-raises it rather than absorbing it as a
        per-file fault — so the persisted cTags describe exactly what was
        ingested and the next run picks up from there."""
        from app.api.admin_extraction import RESUMABLE_STOP_REASONS, _run_out

        assert "throttled" in RESUMABLE_STOP_REASONS
        out = _run_out({"id": "er_1", "status": "failed", "report": {"interrupted_reason": "throttled"}})
        assert out["resumable"] is True

    def test_resumability_matches_the_crawls_own_normalization(self):
        from app.api.admin_extraction import _run_out

        for spelling in ("timeout", "TIMEOUT", " Timeout "):
            out = _run_out({"id": "er_1", "status": "failed", "report": {"interrupted_reason": spelling}})
            assert out["resumable"] is True, spelling

    def test_a_run_that_ended_normally_has_no_stop_reason(self):
        from app.api.admin_extraction import _run_out

        out = _run_out({"id": "er_1", "status": "done", "report": {"duration_s": 12.0}})
        assert out["interrupted_reason"] is None

    def test_a_cooperative_stop_is_resumable(self):
        """An admin-requested stop (`POST …/extraction/stop`) aborts at the
        same consistent point a timeout does — see `_STOP_REASONS` on the
        crawl side — so it earns the same "next run resumes" promise."""
        from app.api.admin_extraction import RESUMABLE_STOP_REASONS, _run_out

        assert "stopped" in RESUMABLE_STOP_REASONS
        out = _run_out({"id": "er_1", "status": "failed", "report": {"interrupted_reason": "stopped"}})
        assert out["interrupted_reason"] == "stopped"
        assert out["resumable"] is True

    def test_activity_rides_the_same_checkpoint_projection(self):
        """`activity` (owner-frustration fix, 2026-09-01) is read from
        whichever of `report`/`progress` `live` resolves to — no separate
        lookup, so it can never disagree with the counters next to it."""
        from app.api.admin_extraction import _run_out

        activity = {"phase": "crawl", "current_path": "Reports/q3.docx", "recent": []}
        out = _run_out({"id": "er_1", "status": "running", "progress": {"activity": activity}})
        assert out["activity"] == activity

    def test_a_finished_run_has_no_live_activity(self):
        """`report` (the FINAL shape `finish()` stores) never grows an
        `activity` key — a completed run honestly has nothing in flight."""
        from app.api.admin_extraction import _run_out

        out = _run_out({"id": "er_1", "status": "done", "report": {"duration_s": 12.0}})
        assert out["activity"] is None
