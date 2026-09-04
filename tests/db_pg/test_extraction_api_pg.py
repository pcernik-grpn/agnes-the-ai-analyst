"""Extraction observability API — the Postgres happy path (2026-08-31
design §9, A1/A2/A3).

The DuckDB half of these routes (admin gating, 404-before-repo, the typed
``501 requires_postgres_backend``) is ``tests/test_admin_extraction.py``.
This file is the other side of that fork: with the PG backend actually
present, a recorded run must reach the card through the API in the shape the
card renders.
"""

from __future__ import annotations

BASE = "/api/admin/sharepoint/connections"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _pg_client(tmp_path, monkeypatch, pg_engine):
    from tests.db_pg._parity_sweep_util import build_seeded_client

    return build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)


def _connection(client, token, name="corp-sharepoint"):
    r = client.post(
        "/api/admin/source-connections",
        json={
            "name": name,
            "source_type": "sharepoint",
            "config": {"tenant_id": "t1", "client_id": "c1"},
        },
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _repo():
    from src.repositories import extraction_runs_repo

    return extraction_runs_repo()


def test_status_reads_never_run_as_null_not_as_zeros(tmp_path, monkeypatch, pg_engine):
    """A connection that has never been crawled has no numbers. Rendering
    zeros would be indistinguishable from "crawled, found nothing"."""
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token)

    body = client.get(f"{BASE}/{conn_id}/extraction/status", headers=_auth(token)).json()
    assert body["running"] is None
    assert body["last_completed"] is None
    assert body["last_failed"] is None
    assert body["runs_total"] == 0
    assert body["as_of"]
    assert body["provider_limit"] is None


def test_status_surfaces_an_active_provider_limit_condition_for_this_connections_provider(
    tmp_path, monkeypatch, pg_engine
):
    """TCRD-296 synthesis F.25 — the source card's own facts line reads this
    to render "paused: provider limit" without polling the fleet endpoint."""
    from src.repositories import extraction_conditions_repo

    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token)

    extraction_conditions_repo().record(
        reason="quota_exceeded",
        provider="anthropic",  # the instance default a connection with no override resolves to
        model="claude-haiku-4-5-20251001",
        region=None,
        message="Quota exceeded",
        retry_after_s=None,
    )

    body = client.get(f"{BASE}/{conn_id}/extraction/status", headers=_auth(token)).json()
    assert body["provider_limit"] is not None
    assert body["provider_limit"]["reason"] == "quota_exceeded"


def test_status_surfaces_an_exhausted_jobs_run_as_last_failed(tmp_path, monkeypatch, pg_engine):
    """2026-09 incident: a job the worker runtime marked 'failed' closes
    its own `extraction_runs` row (`ExtractionRunsPgRepository.
    fail_for_job`) — this run is no longer 'running', so `last_completed`
    (which deliberately excludes 'failed') would otherwise make it
    invisible. `last_failed` is what the source card reads instead, with
    the job's own error text intact."""
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token)

    repo = _repo()
    run_id = repo.start(connection_id=conn_id, job_id="job-1")
    repo.checkpoint(run_id, files_seen=12, files_done=12)
    repo.fail_for_job("job-1", error="lease expired after max attempts")

    body = client.get(f"{BASE}/{conn_id}/extraction/status", headers=_auth(token)).json()
    assert body["running"] is None
    assert body["last_completed"] is None
    assert body["last_failed"]["id"] == run_id
    assert body["last_failed"]["outcome"] == "failed"
    assert body["last_failed"]["error"] == "lease expired after max attempts"
    assert body["last_failed"]["files_done"] == 12


def test_status_hides_last_failed_when_a_newer_run_completed(tmp_path, monkeypatch, pg_engine):
    """An old failure must never eclipse the run that actually finished
    last — a retry that succeeded is the more relevant fact."""
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token)

    repo = _repo()
    repo.start(connection_id=conn_id, job_id="job-1")
    repo.fail_for_job("job-1", error="boom")
    done = repo.start(connection_id=conn_id, job_id="job-2")
    repo.finish(done, status="done", report={"new": 3})

    body = client.get(f"{BASE}/{conn_id}/extraction/status", headers=_auth(token)).json()
    assert body["last_completed"]["id"] == done
    assert body["last_failed"] is None


def test_status_hides_last_failed_while_a_new_run_is_in_progress(tmp_path, monkeypatch, pg_engine):
    """A fresh attempt already running for this connection is the relevant
    signal — an older failure alongside it would only be noise."""
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token)

    repo = _repo()
    repo.start(connection_id=conn_id, job_id="job-1")
    repo.fail_for_job("job-1", error="boom")
    repo.start(connection_id=conn_id, job_id="job-2")  # still running

    body = client.get(f"{BASE}/{conn_id}/extraction/status", headers=_auth(token)).json()
    assert body["running"] is not None
    assert body["last_failed"] is None


def test_a_live_run_surfaces_with_absolute_counters(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token)

    repo = _repo()
    run_id = repo.start(connection_id=conn_id)
    activity = {
        "phase": "crawl",
        "current_path": "Reports/q3.docx",
        "current_started_at": "2026-09-01T00:00:00+00:00",
        "recent": [{"path": "Reports/q2.docx", "outcome": "new"}],
    }
    repo.checkpoint(
        run_id,
        files_seen=400,
        files_done=400,
        progress={
            "new": 12,
            "unchanged": 388,
            "http_429": 4,
            "throttle_wait_s": 38.0,
            "elapsed_s": 391.0,
            "activity": activity,
            "filtered_by_age": 40,
            "age_unknown": 3,
        },
    )

    body = client.get(f"{BASE}/{conn_id}/extraction/status", headers=_auth(token)).json()
    running = body["running"]
    assert running["id"] == run_id
    assert running["outcome"] == "running"
    assert running["files_done"] == 400
    assert running["http_429"] == 4
    assert running["throttle_wait_s"] == 38.0
    # The card's "as of" caption reads this, not the response's own `as_of`.
    assert running["checkpoint_at"]
    # What the crawl is touching RIGHT NOW (owner-frustration fix, 2026-09-01).
    assert running["activity"] == activity
    # An operator watching a LIVE run must be able to tell whether
    # `extraction.crawl.min_modified` is doing anything — not only once the
    # run finishes and `report()` becomes readable.
    assert running["filtered_by_age"] == 40
    assert running["age_unknown"] == 3
    # A cooperative stop (`POST .../extraction/stop`) always exists — it
    # lives on `source_connections`, not this PG-only table.
    assert body["can_stop"] is True


def test_a_stale_running_run_reports_stalled_with_its_age(tmp_path, monkeypatch, pg_engine):
    """A worker killed outright finalizes nothing, so `running` must be
    derived at read time, never trusted from the row."""
    import sqlalchemy as sa

    import app.api.admin_extraction as mod

    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token)

    run_id = _repo().start(connection_id=conn_id)
    from src.db_pg import get_engine

    with get_engine().begin() as conn:
        conn.execute(
            sa.text("UPDATE extraction_runs SET checkpoint_at = now() - make_interval(secs => :s) WHERE id = :id"),
            {"s": mod._STALL_AFTER_S + 600, "id": run_id},
        )

    running = client.get(f"{BASE}/{conn_id}/extraction/status", headers=_auth(token)).json()["running"]
    assert running["outcome"] == "stalled"
    assert running["stored_status"] == "running"
    assert running["stale_s"] > mod._STALL_AFTER_S
    assert running["liveness_note"]


def test_runs_history_is_newest_first_and_reports_the_true_total(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token)

    repo = _repo()
    ids = []
    for i in range(4):
        run_id = repo.start(connection_id=conn_id)
        repo.finish(run_id, status="done", report={"new": i})
        ids.append(run_id)

    body = client.get(f"{BASE}/{conn_id}/extraction/runs?limit=2", headers=_auth(token)).json()
    assert [r["id"] for r in body["runs"]] == [ids[3], ids[2]]
    # "2 more runs" is never a silent truncation.
    assert body["total"] == 4


def test_an_interrupted_run_is_its_own_outcome_not_a_failure(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token)

    repo = _repo()
    run_id = repo.start(connection_id=conn_id)
    repo.finish(run_id, status="interrupted", report={"new": 318, "interrupted": True}, files_done=318)

    runs = client.get(f"{BASE}/{conn_id}/extraction/runs", headers=_auth(token)).json()["runs"]
    assert runs[0]["outcome"] == "interrupted"
    assert runs[0]["files_done"] == 318
    assert runs[0]["error"] is None


def test_a_failed_run_shows_its_error_verbatim(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token)

    repo = _repo()
    run_id = repo.start(connection_id=conn_id)
    repo.finish(run_id, status="failed", error="CrawlError: no confirmed scope to crawl")

    runs = client.get(f"{BASE}/{conn_id}/extraction/runs", headers=_auth(token)).json()["runs"]
    assert runs[0]["outcome"] == "failed"
    assert "no confirmed scope" in runs[0]["error"]


def test_run_detail_carries_the_report_and_a_visibly_capped_skip_list(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token)

    from src.repositories.extraction_runs_pg import cap_skips

    repo = _repo()
    run_id = repo.start(connection_id=conn_id)
    repo.finish(
        run_id,
        status="done",
        report={"new": 5, "duration_s": 12.0, "skipped_oversize": {"files": 7}},
        skips=cap_skips([{"path": "/big.pdf", "reason": "oversize", "detail": "1.4 GB"}], total=9),
    )

    body = client.get(f"{BASE}/{conn_id}/extraction/runs/{run_id}", headers=_auth(token)).json()
    assert body["report"]["new"] == 5
    assert body["skips"]["listed"] == 1
    assert body["skips"]["total"] == 9
    assert body["skips"]["truncated"] is True
    assert body["skips_total"] == 9


def test_run_detail_404s_for_another_connections_run(tmp_path, monkeypatch, pg_engine):
    """A run id is not a capability: reading it through the wrong
    connection's path must not work."""
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    mine = _connection(client, token, name="sp-a")
    theirs = _connection(client, token, name="sp-b")

    run_id = _repo().start(connection_id=theirs)
    r = client.get(f"{BASE}/{mine}/extraction/runs/{run_id}", headers=_auth(token))
    assert r.status_code == 404
    assert r.json()["detail"] == "run_not_found"


def test_config_answers_on_postgres_too(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token)

    body = client.get(f"{BASE}/{conn_id}/extraction/config", headers=_auth(token)).json()
    assert body["section_editable"] is True  # registry-driven since producer.command was removed
    # The on/off leaf moved to the single sharepoint switch (2026-09-01 flag
    # consolidation); the drawer's Enabled row reads it from there.
    assert any(row["key"] == "sharepoint.enabled" for row in body["effective"])
    assert any(row["key"] == "extraction.timeout_s" for row in body["effective"])


def test_status_next_run_at_is_none_by_default(tmp_path, monkeypatch, pg_engine):
    """D.16: with no per-connection override AND no instance-wide
    ``extraction.schedule`` configured (this test env's default), the
    sweep never runs at all — an honest ``None``, not a guess."""
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token)

    body = client.get(f"{BASE}/{conn_id}/extraction/status", headers=_auth(token)).json()
    assert body["next_run_at"] is None


def test_status_next_run_at_is_present_once_a_connection_sets_its_own_interval(tmp_path, monkeypatch, pg_engine):
    """A connection's own cadence (D.16) does not need the instance-wide
    switch to compute a next-run estimate — only to actually be picked up
    by a live sweep."""
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token)
    client.patch(f"{BASE}/{conn_id}/extraction/crawl-config", json={"schedule": "every 6h"}, headers=_auth(token))

    body = client.get(f"{BASE}/{conn_id}/extraction/status", headers=_auth(token)).json()
    assert body["next_run_at"] is not None


# ---------------------------------------------------------------------------
# Fleet view (`GET /api/admin/sharepoint/extraction/runs`, 2026-09-02) — one
# row per SharePoint connection, for an operator running several crawls at
# once. Same PG-only posture as every other route in this module.
# ---------------------------------------------------------------------------

FLEET_URL = "/api/admin/sharepoint/extraction/runs"


def test_fleet_default_scope_only_lists_currently_running_connections(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    idle = _connection(client, token, name="sp-idle")
    running = _connection(client, token, name="sp-running")

    repo = _repo()
    done = repo.start(connection_id=idle)
    repo.finish(done, status="done", report={"new": 3})
    run_id = repo.start(connection_id=running)
    repo.checkpoint(run_id, files_seen=40, files_done=40, progress={"new": 40})

    body = client.get(f"{FLEET_URL}?active=1", headers=_auth(token)).json()
    ids = {row["connection_id"] for row in body["connections"]}
    assert ids == {running}
    assert body["totals"]["connections"] == 1
    assert body["totals"]["active"] == 1
    row = body["connections"][0]
    assert row["run"]["id"] == run_id
    assert row["run"]["files_done"] == 40


def test_fleet_bare_call_defaults_to_the_active_scope(tmp_path, monkeypatch, pg_engine):
    """No query params at all behaves like `?active=1` — the primary
    "is it on pace right now" view, not the fuller `?all=1` picture."""
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    idle = _connection(client, token, name="sp-idle-bare")
    repo = _repo()
    done = repo.start(connection_id=idle)
    repo.finish(done, status="done")

    body = client.get(FLEET_URL, headers=_auth(token)).json()
    assert body["connections"] == []
    assert body["totals"]["connections"] == 0


def test_fleet_all_scope_lists_every_connection_including_idle_ones(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    idle = _connection(client, token, name="sp-idle-all")
    never_run = _connection(client, token, name="sp-never-run")

    repo = _repo()
    done = repo.start(connection_id=idle)
    repo.finish(done, status="done", report={"new": 3})

    body = client.get(f"{FLEET_URL}?all=1", headers=_auth(token)).json()
    ids = {row["connection_id"] for row in body["connections"]}
    assert ids == {idle, never_run}
    by_id = {row["connection_id"]: row for row in body["connections"]}
    assert by_id[idle]["run"]["outcome"] == "done"
    assert by_id[never_run]["run"] is None
    assert by_id[never_run]["facts"]["docs_done"] is None
    assert body["totals"]["connections"] == 2
    assert body["totals"]["active"] == 0


def test_fleet_all_wins_when_both_query_params_are_set(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    idle = _connection(client, token, name="sp-both")
    repo = _repo()
    done = repo.start(connection_id=idle)
    repo.finish(done, status="done")

    body = client.get(f"{FLEET_URL}?active=1&all=1", headers=_auth(token)).json()
    assert {row["connection_id"] for row in body["connections"]} == {idle}


def test_fleet_row_carries_the_facts_stage_from_the_same_run(tmp_path, monkeypatch, pg_engine):
    """Crawl and facts are the SAME run row — the fleet row's `facts` reads
    off it directly, no second per-connection query."""
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token, name="sp-facts")

    repo = _repo()
    run_id = repo.start(connection_id=conn_id)
    repo.checkpoint(
        run_id,
        phase="facts",
        files_seen=900,
        files_done=900,
        enumeration_done=True,
        progress={"facts": {"docs_done": 12, "docs_total": 340}},
    )

    body = client.get(f"{FLEET_URL}?active=1", headers=_auth(token)).json()
    row = body["connections"][0]
    assert row["run"]["phase"] == "facts"
    assert row["facts"]["phase_active"] is True
    assert row["facts"]["docs_done"] == 12
    assert row["facts"]["docs_total"] == 340


def test_fleet_payload_carries_an_active_provider_limit_condition(tmp_path, monkeypatch, pg_engine):
    """TCRD-296 synthesis F.25 — the fleet's own top-level ``conditions``
    list (the banner's read) AND the per-connection ``facts.provider_limit``
    field (the source card's "paused: provider limit" line), both fed by
    the SAME condition row so the two can never disagree."""
    from src.repositories import extraction_conditions_repo

    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token, name="sp-paused")

    extraction_conditions_repo().record(
        reason="workspace_limit",
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        region=None,
        message="Your workspace has hit the API usage limits ... regain access on 2026-10-01",
        retry_after_s=None,
    )

    body = client.get(f"{FLEET_URL}?all=1", headers=_auth(token)).json()
    assert len(body["conditions"]) == 1
    condition = body["conditions"][0]
    assert condition["reason"] == "workspace_limit"
    assert condition["provider"] == "anthropic"

    row = next(r for r in body["connections"] if r["connection_id"] == conn_id)
    # The connection's own resolved facts provider (no override set — the
    # instance default is "anthropic") matches the active condition.
    assert row["facts"]["provider_limit"] is not None
    assert row["facts"]["provider_limit"]["reason"] == "workspace_limit"


def test_fleet_payload_has_no_conditions_when_none_are_active(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    _connection(client, token, name="sp-healthy")

    body = client.get(f"{FLEET_URL}?all=1", headers=_auth(token)).json()
    assert body["conditions"] == []
    row = body["connections"][0]
    assert row["facts"]["provider_limit"] is None


def test_fleet_row_carries_the_age_filter_counters_from_the_same_run(tmp_path, monkeypatch, pg_engine):
    """The fleet row is `_run_out` on the same run — an operator scanning
    the fleet table must be able to tell a connection's `min_modified`
    cutoff is doing something without opening its source card."""
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token, name="sp-age-filtered")

    repo = _repo()
    run_id = repo.start(connection_id=conn_id)
    repo.checkpoint(
        run_id,
        files_seen=100,
        files_done=100,
        progress={"filtered_by_age": 40, "age_unknown": 3},
    )

    body = client.get(f"{FLEET_URL}?active=1", headers=_auth(token)).json()
    row = body["connections"][0]
    assert row["run"]["filtered_by_age"] == 40
    assert row["run"]["age_unknown"] == 3


def test_fleet_row_carries_next_run_at(tmp_path, monkeypatch, pg_engine):
    """D.16 — same best-effort ``next_run_at`` hint the crawl-config PATCH
    response and ``extraction/status`` carry, on every fleet row (``?all=1``
    so an idle, never-run connection is included)."""
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    off_conn = _connection(client, token, name="sp-fleet-schedule-off")
    interval_conn = _connection(client, token, name="sp-fleet-schedule-interval")
    client.patch(f"{BASE}/{off_conn}/extraction/crawl-config", json={"schedule": "off"}, headers=_auth(token))
    client.patch(f"{BASE}/{interval_conn}/extraction/crawl-config", json={"schedule": "every 6h"}, headers=_auth(token))

    body = client.get(f"{FLEET_URL}?all=1", headers=_auth(token)).json()
    rows = {row["connection_id"]: row for row in body["connections"]}
    assert rows[off_conn]["next_run_at"] is None
    assert rows[interval_conn]["next_run_at"] is not None


def test_fleet_row_flags_a_stuck_run_past_the_stall_threshold(tmp_path, monkeypatch, pg_engine):
    """The fleet's `stuck` flag and the per-run `outcome: "stalled"` word
    are ONE rule (2026-09-03 unification) — a row is `stuck` exactly when
    its own `run.outcome` is `stalled`, never a separate, tighter
    threshold that could disagree with the badge sitting right next to it."""
    import sqlalchemy as sa

    import app.api.admin_extraction as mod

    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token, name="sp-stuck")

    run_id = _repo().start(connection_id=conn_id)
    from src.db_pg import get_engine

    with get_engine().begin() as conn:
        conn.execute(
            sa.text("UPDATE extraction_runs SET checkpoint_at = now() - make_interval(secs => :s) WHERE id = :id"),
            {"s": mod._STALL_AFTER_S + 60, "id": run_id},
        )

    body = client.get(f"{FLEET_URL}?active=1", headers=_auth(token)).json()
    row = body["connections"][0]
    assert row["stuck"] is True
    assert row["run"]["outcome"] == "stalled"
    assert row["checkpoint_age_s"] > mod._STALL_AFTER_S
    assert body["totals"]["stuck"] == 1


def test_fleet_row_is_not_stuck_before_the_stall_threshold(tmp_path, monkeypatch, pg_engine):
    """A checkpoint that is merely old (but not yet past
    `extraction.stall_after_s`) must not trip `stuck` — proof the fleet no
    longer uses a separate, tighter tripwire than the run's own outcome."""
    import sqlalchemy as sa

    import app.api.admin_extraction as mod

    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token, name="sp-not-yet-stuck")

    run_id = _repo().start(connection_id=conn_id)
    from src.db_pg import get_engine

    with get_engine().begin() as conn:
        conn.execute(
            sa.text("UPDATE extraction_runs SET checkpoint_at = now() - make_interval(secs => :s) WHERE id = :id"),
            {"s": mod._STALL_AFTER_S - 60, "id": run_id},
        )

    body = client.get(f"{FLEET_URL}?active=1", headers=_auth(token)).json()
    row = body["connections"][0]
    assert row["stuck"] is False
    assert row["run"]["outcome"] == "running"
    assert body["totals"]["stuck"] == 0


def test_stall_after_s_config_override_lowers_the_threshold(tmp_path, monkeypatch, pg_engine):
    """`extraction.stall_after_s` is admin-editable and read fresh — a
    checkpoint just past a LOWERED threshold reports `stalled` even though
    it is well under the built-in 900s default."""
    import sqlalchemy as sa

    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token, name="sp-custom-threshold")

    resp = client.post(
        "/api/admin/server-config",
        json={"sections": {"extraction": {"stall_after_s": 60}}},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text

    run_id = _repo().start(connection_id=conn_id)
    from src.db_pg import get_engine

    with get_engine().begin() as conn:
        conn.execute(
            sa.text("UPDATE extraction_runs SET checkpoint_at = now() - make_interval(secs => :s) WHERE id = :id"),
            {"s": 120, "id": run_id},
        )

    running = client.get(f"{BASE}/{conn_id}/extraction/status", headers=_auth(token)).json()["running"]
    assert running["outcome"] == "stalled"


def test_fleet_totals_sum_across_connections(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_a = _connection(client, token, name="sp-totals-a")
    conn_b = _connection(client, token, name="sp-totals-b")

    repo = _repo()
    run_a = repo.start(connection_id=conn_a)
    repo.checkpoint(run_a, files_seen=100, files_done=100, progress={"new": 100})
    run_b = repo.start(connection_id=conn_b)
    repo.checkpoint(run_b, files_seen=50, files_done=50, progress={"new": 50})

    body = client.get(f"{FLEET_URL}?active=1", headers=_auth(token)).json()
    assert body["totals"]["connections"] == 2
    assert body["totals"]["active"] == 2
    assert body["totals"]["files_done"] == 150
    assert body["totals"]["files_seen"] == 150


# ---------------------------------------------------------------------------
# Fleet cost tile — attributing `facts_ingest_runs` spend to a connection
# (the standalone `sharepoint-facts-extraction` job's own persisted ledger,
# distinct from the crawl run's own inline `usage` block).
# ---------------------------------------------------------------------------


def test_fleet_row_cost_includes_facts_ingest_runs_attributable_spend(tmp_path, monkeypatch, pg_engine):
    """A connection whose facts stage runs as a SEPARATE
    `sharepoint-facts-extraction` job never touches its own `extraction_runs`
    row's `usage` block — it spends through `facts_ingest_runs.llm_usage`
    instead, attributed back to the connection by `corpus_ids` overlap with
    its own scope collections. Before this fix the fleet's cost figure read
    only the crawl run's own (empty) usage, so a connection that had
    genuinely spent real money showed $0."""
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token, name="sp-facts-ledger-cost")

    from src.repositories import facts_ingest_runs_repo, source_connections_repo

    source_connections_repo().update(
        conn_id, config={"scopes": [{"source_scope_id": "s1", "collection_id": "col_ledger"}]}
    )

    repo = _repo()
    run_id = repo.start(connection_id=conn_id)
    repo.finish(run_id, status="done", report={"new": 3})  # facts NEVER ran inline for this run

    facts_ingest_runs_repo().create(
        corpus_ids=["col_ledger"],
        caller="scheduler@system.local",
        documents_seen=500,
        claims_written=10,
        claims_rejected=[],
        deferred=[],
        subjects_created=0,
        subjects_deleted=0,
        review_items=[],
        llm_usage={"input_tokens": 1_000_000, "output_tokens": 200_000, "models": ["claude-haiku-4-5"]},
    )

    body = client.get(f"{FLEET_URL}?all=1", headers=_auth(token)).json()
    row = next(r for r in body["connections"] if r["connection_id"] == conn_id)
    assert row["run"]["usage"] == {}, "the crawl run itself never reported usage — this pins the bug's premise"
    assert row["estimated_cost_usd"] is not None
    assert row["estimated_cost_usd"] > 0
    assert row["cost_status"] == "priced"
    assert "claude-haiku-4-5" in row["cost_models"]


def test_fleet_row_cost_is_none_not_a_fabricated_zero_when_nothing_is_recorded(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token, name="sp-no-usage-anywhere")
    repo = _repo()
    run_id = repo.start(connection_id=conn_id)
    repo.finish(run_id, status="done", report={"new": 3})

    body = client.get(f"{FLEET_URL}?all=1", headers=_auth(token)).json()
    row = next(r for r in body["connections"] if r["connection_id"] == conn_id)
    assert row["estimated_cost_usd"] is None
    assert row["cost_status"] == "no_usage"


def test_fleet_row_cost_status_is_unpriced_when_facts_ledger_tokens_have_no_priceable_model(
    tmp_path, monkeypatch, pg_engine
):
    """Tokens are known (the producer reported usage) but no single named
    model means the figure cannot be honestly priced — a different claim
    from both "priced" and "no usage at all", and the payload must say so."""
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token, name="sp-unpriceable")

    from src.repositories import facts_ingest_runs_repo, source_connections_repo

    source_connections_repo().update(
        conn_id, config={"scopes": [{"source_scope_id": "s1", "collection_id": "col_unpriced"}]}
    )
    facts_ingest_runs_repo().create(
        corpus_ids=["col_unpriced"],
        caller="scheduler@system.local",
        documents_seen=5,
        claims_written=0,
        claims_rejected=[],
        deferred=[],
        subjects_created=0,
        subjects_deleted=0,
        review_items=[],
        llm_usage={"input_tokens": 1000, "output_tokens": 100},  # no model named
    )

    body = client.get(f"{FLEET_URL}?all=1", headers=_auth(token)).json()
    row = next(r for r in body["connections"] if r["connection_id"] == conn_id)
    assert row["estimated_cost_usd"] is None
    assert row["cost_status"] == "unpriced"
    assert row["token_totals"]["input_tokens"] == 1000


def test_fleet_row_cost_sums_crawl_run_and_facts_ingest_without_double_counting(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token, name="sp-both-sources")

    from src.repositories import facts_ingest_runs_repo, source_connections_repo

    source_connections_repo().update(
        conn_id, config={"scopes": [{"source_scope_id": "s1", "collection_id": "col_both"}]}
    )

    repo = _repo()
    run_id = repo.start(connection_id=conn_id)
    repo.finish(
        run_id,
        status="done",
        report={"new": 3},
        usage={
            "facts": {"estimated_cost_usd": 2.0, "input_tokens": 100, "output_tokens": 20, "model": "claude-haiku-4-5"}
        },
    )
    facts_ingest_runs_repo().create(
        corpus_ids=["col_both"],
        caller="scheduler@system.local",
        documents_seen=10,
        claims_written=0,
        claims_rejected=[],
        deferred=[],
        subjects_created=0,
        subjects_deleted=0,
        review_items=[],
        llm_usage={"input_tokens": 1000, "output_tokens": 200, "models": ["claude-haiku-4-5"]},
    )

    body = client.get(f"{FLEET_URL}?all=1", headers=_auth(token)).json()
    row = next(r for r in body["connections"] if r["connection_id"] == conn_id)
    from src.llm_pricing import cost_usd

    expected_facts_ledger_cost = cost_usd(model="claude-haiku-4-5", input_tokens=1000, output_tokens=200)
    assert round(row["estimated_cost_usd"], 4) == round(2.0 + expected_facts_ledger_cost, 4)
    assert row["cost_status"] == "priced"


def test_fleet_total_de_duplicates_a_run_shared_by_two_connections_own_collections(
    tmp_path, monkeypatch, pg_engine
):
    """The exact configuration the whole cost-truth fix came from: a site
    split into siblings (`POST .../split/apply`) — or a bulk-add's shared-
    collection option — can legitimately route more than one connection's
    scope at the SAME collection. A `facts_ingest_runs` run touching that
    collection is honestly attributed in FULL to both connections' own
    rows (neither row understates what it can see), but the fleet-wide
    TOTAL must count that run's cost exactly once, not once per connection
    that shares it — and both rows must say, on screen, that their figure
    is shared."""
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_a = _connection(client, token, name="sp-shared-a")
    conn_b = _connection(client, token, name="sp-shared-b")

    from src.repositories import facts_ingest_runs_repo, source_connections_repo

    source_connections_repo().update(
        conn_a, config={"scopes": [{"source_scope_id": "s1", "collection_id": "col_shared_ab"}]}
    )
    source_connections_repo().update(
        conn_b, config={"scopes": [{"source_scope_id": "s2", "collection_id": "col_shared_ab"}]}
    )

    facts_ingest_runs_repo().create(
        corpus_ids=["col_shared_ab"],
        caller="scheduler@system.local",
        documents_seen=500,
        claims_written=10,
        claims_rejected=[],
        deferred=[],
        subjects_created=0,
        subjects_deleted=0,
        review_items=[],
        llm_usage={"input_tokens": 1_000_000, "output_tokens": 200_000, "models": ["claude-haiku-4-5"]},
    )

    body = client.get(f"{FLEET_URL}?all=1", headers=_auth(token)).json()
    by_id = {r["connection_id"]: r for r in body["connections"]}
    row_a, row_b = by_id[conn_a], by_id[conn_b]

    import pytest

    from src.llm_pricing import cost_usd

    expected_run_cost = cost_usd(model="claude-haiku-4-5", input_tokens=1_000_000, output_tokens=200_000)

    # BOTH rows show the full, un-split figure — neither understates what
    # its own connection's collections can see.
    assert row_a["estimated_cost_usd"] == pytest.approx(expected_run_cost, abs=1e-4)
    assert row_b["estimated_cost_usd"] == pytest.approx(expected_run_cost, abs=1e-4)

    # Both are visibly marked shared, naming the OTHER connection.
    assert row_a["cost_shared"] is True
    assert row_b["cost_shared"] is True
    assert row_a["cost_shared_with"] == ["sp-shared-b"]
    assert row_b["cost_shared_with"] == ["sp-shared-a"]

    # The page total counts the shared run's cost ONCE, not twice — the
    # double-count this fix closes.
    assert body["totals"]["estimated_cost_usd"] == pytest.approx(expected_run_cost, abs=1e-4)
    assert body["totals"]["cost_note"]


def test_fleet_response_never_serializes_the_facts_ingest_runs_id_list(tmp_path, monkeypatch, pg_engine):
    """Payload-size regression: the fleet response must carry only the
    AGGREGATE facts-ingest figures per connection, never the underlying
    per-run id list `llm_usage_rollup_by_corpus_ids` returns internally to
    let the server de-duplicate the page total. `facts_ingest_runs` is
    append-only and only grows, a single connection's own collection can
    match a large share of it, and the fleet page polls every 5s —
    serializing that list on every poll would ship ids the browser never
    reads."""
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token, name="sp-many-runs")

    from src.repositories import facts_ingest_runs_repo, source_connections_repo

    source_connections_repo().update(
        conn_id, config={"scopes": [{"source_scope_id": "s1", "collection_id": "col_many"}]}
    )
    for _ in range(25):
        facts_ingest_runs_repo().create(
            corpus_ids=["col_many"],
            caller="scheduler@system.local",
            documents_seen=1,
            claims_written=0,
            claims_rejected=[],
            deferred=[],
            subjects_created=0,
            subjects_deleted=0,
            review_items=[],
            llm_usage={"input_tokens": 100, "output_tokens": 10, "models": ["claude-haiku-4-5"]},
        )

    resp = client.get(f"{FLEET_URL}?all=1", headers=_auth(token))
    body = resp.json()
    row = next(r for r in body["connections"] if r["connection_id"] == conn_id)

    assert row["facts"]["facts_ingest_usage"]["runs_with_usage"] == 25
    assert "runs" not in row["facts"]["facts_ingest_usage"]
    # Belt and braces: no `ir_`-prefixed facts_ingest_runs id anywhere in
    # the raw response body — proves the list is gone, not just renamed
    # or nested one level deeper.
    assert "ir_" not in resp.text


def test_fleet_row_cost_shared_is_not_marked_when_the_shared_run_is_unpriced(tmp_path, monkeypatch, pg_engine):
    """The shared badge is gated on the row actually showing a priced
    dollar figure — a row whose only facts-ledger run is unpriceable (no
    single named model) renders an em-dash for cost, and `cost_shared`
    must stay `False` even though the SAME run is attributed to another
    connection too, or the badge would explain a number the row does not
    display."""
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_a = _connection(client, token, name="sp-unpriced-shared-a")
    conn_b = _connection(client, token, name="sp-unpriced-shared-b")

    from src.repositories import facts_ingest_runs_repo, source_connections_repo

    source_connections_repo().update(
        conn_a, config={"scopes": [{"source_scope_id": "s1", "collection_id": "col_unpriced_shared"}]}
    )
    source_connections_repo().update(
        conn_b, config={"scopes": [{"source_scope_id": "s2", "collection_id": "col_unpriced_shared"}]}
    )
    facts_ingest_runs_repo().create(
        corpus_ids=["col_unpriced_shared"],
        caller="scheduler@system.local",
        documents_seen=5,
        claims_written=0,
        claims_rejected=[],
        deferred=[],
        subjects_created=0,
        subjects_deleted=0,
        review_items=[],
        llm_usage={"input_tokens": 1000, "output_tokens": 100},  # no model named — unpriceable
    )

    body = client.get(f"{FLEET_URL}?all=1", headers=_auth(token)).json()
    by_id = {r["connection_id"]: r for r in body["connections"]}
    row_a, row_b = by_id[conn_a], by_id[conn_b]

    for row in (row_a, row_b):
        assert row["cost_status"] == "unpriced"
        assert row["estimated_cost_usd"] is None
        assert row["cost_shared"] is False
        assert row["cost_shared_with"] == []


def test_fleet_total_does_not_deduplicate_two_genuinely_different_runs(tmp_path, monkeypatch, pg_engine):
    """A sibling proof for the de-duplication test above: two connections
    with their OWN, non-overlapping collections and their OWN separate
    ingest runs must still sum normally — de-duplication must never
    collapse two genuinely different runs into one."""
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_a = _connection(client, token, name="sp-distinct-a")
    conn_b = _connection(client, token, name="sp-distinct-b")

    from src.repositories import facts_ingest_runs_repo, source_connections_repo

    source_connections_repo().update(
        conn_a, config={"scopes": [{"source_scope_id": "s1", "collection_id": "col_distinct_a"}]}
    )
    source_connections_repo().update(
        conn_b, config={"scopes": [{"source_scope_id": "s2", "collection_id": "col_distinct_b"}]}
    )
    facts_ingest_runs_repo().create(
        corpus_ids=["col_distinct_a"],
        caller="scheduler@system.local",
        documents_seen=10,
        claims_written=0,
        claims_rejected=[],
        deferred=[],
        subjects_created=0,
        subjects_deleted=0,
        review_items=[],
        llm_usage={"input_tokens": 1000, "output_tokens": 100, "models": ["claude-haiku-4-5"]},
    )
    facts_ingest_runs_repo().create(
        corpus_ids=["col_distinct_b"],
        caller="scheduler@system.local",
        documents_seen=10,
        claims_written=0,
        claims_rejected=[],
        deferred=[],
        subjects_created=0,
        subjects_deleted=0,
        review_items=[],
        llm_usage={"input_tokens": 2000, "output_tokens": 200, "models": ["claude-haiku-4-5"]},
    )

    body = client.get(f"{FLEET_URL}?all=1", headers=_auth(token)).json()
    by_id = {r["connection_id"]: r for r in body["connections"]}
    assert by_id[conn_a]["cost_shared"] is False
    assert by_id[conn_b]["cost_shared"] is False

    import pytest

    from src.llm_pricing import cost_usd

    expected_total = cost_usd(model="claude-haiku-4-5", input_tokens=1000, output_tokens=100) + cost_usd(
        model="claude-haiku-4-5", input_tokens=2000, output_tokens=200
    )
    assert body["totals"]["estimated_cost_usd"] == pytest.approx(expected_total, abs=1e-4)


def test_fleet_response_carries_the_instance_wide_cumulative_llm_usage_totals(tmp_path, monkeypatch, pg_engine):
    """The SAME cumulative rollup GET /api/facts/ingest-runs already exposes
    (`llm_usage_totals`) must also ride on the fleet response, so the
    summary strip can show an instance-wide total distinct from any one
    connection's own attributed figure."""
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    _connection(client, token, name="sp-cumulative")

    from src.repositories import facts_ingest_runs_repo

    facts_ingest_runs_repo().create(
        corpus_ids=["col_x"],
        caller="scheduler@system.local",
        documents_seen=1,
        claims_written=0,
        claims_rejected=[],
        deferred=[],
        subjects_created=0,
        subjects_deleted=0,
        review_items=[],
        # `llm_usage_rollup()` (the instance-wide rollup this tile reuses
        # unchanged) prices via its own sampled rate card, which knows
        # "claude-sonnet-4" but not the newer "claude-haiku-4-5" id the
        # per-connection rollup's `src.llm_pricing` table carries — either
        # is fine here since this test only pins that the CUMULATIVE tile
        # rides on the fleet response, not which model it names.
        llm_usage={"input_tokens": 1000, "output_tokens": 100, "models": ["claude-sonnet-4"]},
    )

    body = client.get(f"{FLEET_URL}?all=1", headers=_auth(token)).json()
    assert "llm_usage_totals" in body
    assert body["llm_usage_totals"]["runs_with_usage"] == 1
    assert body["llm_usage_totals"]["estimated_cost_usd"] is not None


def test_fleet_facts_ingest_cost_lookup_is_batched_not_one_query_per_connection(tmp_path, monkeypatch, pg_engine):
    """The facts-ingest cost attribution must be ONE grouped query for the
    whole page, never one round trip per connection — the same batching
    discipline `children_for` already uses for shard rollups."""
    import sqlalchemy as sa

    import src.db_pg as db_pg

    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)

    from src.repositories import facts_ingest_runs_repo, source_connections_repo

    for i in range(8):
        conn_id = _connection(client, token, name=f"sp-cost-batch-{i}")
        source_connections_repo().update(
            conn_id, config={"scopes": [{"source_scope_id": "s1", "collection_id": f"col_batch_{i}"}]}
        )
        facts_ingest_runs_repo().create(
            corpus_ids=[f"col_batch_{i}"],
            caller="scheduler@system.local",
            documents_seen=1,
            claims_written=0,
            claims_rejected=[],
            deferred=[],
            subjects_created=0,
            subjects_deleted=0,
            review_items=[],
            llm_usage={"input_tokens": 100, "output_tokens": 10, "models": ["claude-haiku-4-5"]},
        )

    statements: list = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = db_pg.get_engine()
    sa.event.listen(engine, "before_cursor_execute", _capture)
    try:
        body = client.get(f"{FLEET_URL}?all=1", headers=_auth(token)).json()
    finally:
        sa.event.remove(engine, "before_cursor_execute", _capture)

    assert len(body["connections"]) == 8
    # `llm_usage IS NOT NULL` is the distinguishing clause of the two
    # constant-count queries this feature adds (the per-connection batched
    # rollup, and the instance-wide cumulative one) — deliberately NOT a
    # bare "facts_ingest_runs" substring match, which would also catch the
    # pre-existing, unrelated per-connection `documents_done_since` calls
    # `_facts_throughput_and_eta` already makes for the throughput/ETA line.
    usage_queries = [s for s in statements if "llm_usage IS NOT NULL" in s]
    assert len(usage_queries) <= 2, (
        f"expected the facts-ingest cost lookup to be a CONSTANT number of queries "
        f"regardless of connection count (8 connections here), got {len(usage_queries)}: {usage_queries}"
    )


def test_fleet_ignores_non_sharepoint_connections(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    r = client.post(
        "/api/admin/source-connections",
        json={"name": "kbc-fleet", "source_type": "keboola", "config": {"stack_url": "https://connection.example.com"}},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text

    body = client.get(f"{FLEET_URL}?all=1", headers=_auth(token)).json()
    assert body["connections"] == []


def test_fleet_requires_admin(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    r = client.get(FLEET_URL)
    assert r.status_code == 401


def test_status_reports_an_in_flight_facts_pass_off_the_job_queue(tmp_path, monkeypatch, pg_engine):
    """The standalone facts pass never opens an `extraction_runs` row, so
    the card's poll would otherwise be blind to it; `facts_job` is read off
    the job queue and is `null` — never zeros — when nothing is queued."""
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token)

    body = client.get(f"{BASE}/{conn_id}/extraction/status", headers=_auth(token)).json()
    assert body["facts_job"] is None

    from src.repositories import jobs_repo

    job = jobs_repo().enqueue(
        "sharepoint-facts-extraction",
        {"connection_id": conn_id},
        idempotency_key=f"sharepoint-facts-extraction:{conn_id}",
    )
    body = client.get(f"{BASE}/{conn_id}/extraction/status", headers=_auth(token)).json()
    assert body["facts_job"]["id"] == job["id"]
    assert body["facts_job"]["status"] == "queued"
    assert body["running"] is None, "a facts pass is a job, never claimed to be a crawl run"


# ---------------------------------------------------------------------------
# Retry backlog counts — the source card's "Retry failed (N)"/"Retry empty
# (N)" buttons (TCRD-296 synthesis).
# ---------------------------------------------------------------------------


def test_status_backlog_counts_are_zero_for_a_never_crawled_connection(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token, name="sp-backlog-never")

    body = client.get(f"{BASE}/{conn_id}/extraction/status", headers=_auth(token)).json()
    assert body["failed_items_count"] == 0
    assert body["empty_items_count"] == 0
    assert body["skipped_unsupported_count"] is None


def test_status_backlog_counts_reflect_the_persisted_crawl_state(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token, name="sp-backlog-counts")

    from src.repositories import sharepoint_state_repo

    sharepoint_state_repo().put(
        conn_id,
        "crawl",
        {
            "failed_items": {"graph:1": {"item": {}}, "graph:2": {"item": {}}},
            "empty_items": {"graph:3": {"item": {}}},
        },
    )

    body = client.get(f"{BASE}/{conn_id}/extraction/status", headers=_auth(token)).json()
    assert body["failed_items_count"] == 2
    assert body["empty_items_count"] == 1


def test_status_skipped_unsupported_count_reads_off_the_most_recent_run(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token, name="sp-skipped-count")

    repo = _repo()
    run_id = repo.start(connection_id=conn_id)
    repo.finish(run_id, status="done", report={"skipped_unsupported": 7})

    body = client.get(f"{BASE}/{conn_id}/extraction/status", headers=_auth(token)).json()
    assert body["skipped_unsupported_count"] == 7


def test_status_skipped_unsupported_count_prefers_the_live_run_over_the_last_completed_one(
    tmp_path, monkeypatch, pg_engine
):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token, name="sp-skipped-live")

    repo = _repo()
    done = repo.start(connection_id=conn_id)
    repo.finish(done, status="done", report={"skipped_unsupported": 3})
    running = repo.start(connection_id=conn_id)
    repo.checkpoint(running, files_seen=10, files_done=10, progress={"skipped_unsupported": 9})

    body = client.get(f"{BASE}/{conn_id}/extraction/status", headers=_auth(token)).json()
    assert body["skipped_unsupported_count"] == 9


# ---------------------------------------------------------------------------
# Fleet `jobs` block — the queued-vs-running lane strip (TCRD-296 synthesis).
# ---------------------------------------------------------------------------


def test_fleet_row_carries_the_backlog_counts_for_its_retry_buttons(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token, name="sp-fleet-backlog")

    from src.repositories import sharepoint_state_repo

    sharepoint_state_repo().put(
        conn_id,
        "crawl",
        {"failed_items": {"graph:1": {"item": {}}}, "empty_items": {"graph:2": {}, "graph:3": {}}},
    )

    body = client.get(f"{FLEET_URL}?all=1", headers=_auth(token)).json()
    row = next(r for r in body["connections"] if r["connection_id"] == conn_id)
    assert row["failed_items_count"] == 1
    assert row["empty_items_count"] == 2


def test_fleet_jobs_block_zero_fills_the_known_extraction_kinds(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    _connection(client, token, name="sp-jobs-empty")

    body = client.get(f"{FLEET_URL}?all=1", headers=_auth(token)).json()
    assert body["jobs"] == {
        "corpus-extraction": {"queued": 0, "running": 0},
        "sharepoint-facts-extraction": {"queued": 0, "running": 0},
    }


def test_fleet_jobs_block_reflects_the_queue_independent_of_scope(tmp_path, monkeypatch, pg_engine):
    """The strip must be visible even under the default `active` scope,
    which is exactly when a starved (queued, never yet running) job has no
    `extraction_runs` row and so no table row of its own."""
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    _connection(client, token, name="sp-jobs-queue")

    from src.repositories import jobs_repo

    jobs_repo().enqueue("corpus-extraction", {"connection_id": "whatever"})
    jobs_repo().enqueue("corpus-extraction", {"connection_id": "whatever-2"})
    claimed = jobs_repo().enqueue("sharepoint-facts-extraction", {"connection_id": "whatever"})
    jobs_repo().claim_next(kinds=["sharepoint-facts-extraction"], worker_id="w1")
    assert jobs_repo().get(claimed["id"])["status"] == "running"

    body = client.get(f"{FLEET_URL}?active=1", headers=_auth(token)).json()
    assert body["jobs"]["corpus-extraction"] == {"queued": 2, "running": 0}
    assert body["jobs"]["sharepoint-facts-extraction"] == {"queued": 0, "running": 1}


# ---------------------------------------------------------------------------
# Cancel — force-close a run Stop alone cannot reach (`POST
# /api/admin/sharepoint/extraction/runs/{run_id}/cancel`).
# ---------------------------------------------------------------------------

CANCEL_URL = "/api/admin/sharepoint/extraction/runs"


def test_cancel_closes_the_run_and_finalizes_the_job(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token, name="sp-cancel")

    from src.repositories import jobs_repo

    job = jobs_repo().enqueue("corpus-extraction", {"connection_id": conn_id})
    claimed = jobs_repo().claim_next(kinds=["corpus-extraction"], worker_id="w1", lease_seconds=600)
    assert claimed["id"] == job["id"]

    repo = _repo()
    run_id = repo.start(connection_id=conn_id, job_id=job["id"])
    repo.checkpoint(run_id, files_seen=40, files_done=37, progress={"new": 37})

    r = client.post(f"{CANCEL_URL}/{run_id}/cancel", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["connection_id"] == conn_id
    assert body["outcome"] == "interrupted"
    assert body["interrupted_reason"] == "cancelled"
    assert body["resumable"] is True
    # What the crawl had already ingested is kept, not wiped by the cancel.
    assert body["files_done"] == 37

    job_row = jobs_repo().get(job["id"])
    assert job_row["status"] == "failed"
    assert job_row["error"] == "cancelled_by_admin"
    assert job_row["lease_token"] is None
    assert job_row["lease_expires_at"] is None

    run_row = repo.get(run_id)
    assert run_row["status"] == "interrupted"


def test_cancel_also_sets_the_cooperative_stop_flag(tmp_path, monkeypatch, pg_engine):
    """Cancel = stop + force-close, reusing (not duplicating) the crawl's
    own cooperative-stop path: even a run that is merely SLOW rather than
    truly stuck sees the SAME `stop_requested_at` flag `POST …/extraction/
    stop` sets, so it can still exit cleanly at its next checkpoint
    (`connectors.sharepoint.crawler._StopWatcher`, already covered end to
    end by `tests/test_sharepoint_crawler.py::TestCooperativeStopEndToEnd`)
    instead of relying solely on the force-close below."""
    from connectors.sharepoint.crawler import _stop_requested

    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token, name="sp-cancel-sets-stop-flag")

    assert _stop_requested(conn_id) is None

    run_id = _repo().start(connection_id=conn_id)
    r = client.post(f"{CANCEL_URL}/{run_id}/cancel", headers=_auth(token))
    assert r.status_code == 200, r.text

    assert _stop_requested(conn_id) is not None


def test_cancel_works_even_with_no_owning_job(tmp_path, monkeypatch, pg_engine):
    """A run started outside the worker (a test, a manually-posted payload)
    may carry no `job_id` — cancel must still close the run row."""
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token, name="sp-cancel-no-job")

    run_id = _repo().start(connection_id=conn_id)

    r = client.post(f"{CANCEL_URL}/{run_id}/cancel", headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "interrupted"


def test_cancel_stops_the_heartbeat_loop_via_the_cleared_lease(tmp_path, monkeypatch, pg_engine):
    """The mechanism that stops a stuck worker's lease-extension loop: after
    cancel, the job's own `heartbeat()` (using its old lease_token) must
    return False."""
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token, name="sp-cancel-heartbeat")

    from src.repositories import jobs_repo

    job = jobs_repo().enqueue("corpus-extraction", {"connection_id": conn_id})
    claimed = jobs_repo().claim_next(kinds=["corpus-extraction"], worker_id="w1", lease_seconds=600)

    run_id = _repo().start(connection_id=conn_id, job_id=job["id"])

    r = client.post(f"{CANCEL_URL}/{run_id}/cancel", headers=_auth(token))
    assert r.status_code == 200, r.text

    ok = jobs_repo().heartbeat(job["id"], "w1", claimed["lease_token"], lease_seconds=9999)
    assert ok is False


def test_cancel_unknown_run_is_404(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    r = client.post(f"{CANCEL_URL}/er_does_not_exist/cancel", headers=_auth(token))
    assert r.status_code == 404
    assert r.json()["detail"] == "run_not_found"


def test_cancel_an_already_finished_run_is_409(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token, name="sp-cancel-409")

    repo = _repo()
    run_id = repo.start(connection_id=conn_id)
    repo.finish(run_id, status="done", report={"new": 3})

    r = client.post(f"{CANCEL_URL}/{run_id}/cancel", headers=_auth(token))
    assert r.status_code == 409
    body = r.json()["detail"]
    assert body["error"] == "run_not_active"
    assert body["status"] == "done"


def test_cancel_a_second_time_is_409_not_a_double_close(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token, name="sp-cancel-twice")

    run_id = _repo().start(connection_id=conn_id)
    first = client.post(f"{CANCEL_URL}/{run_id}/cancel", headers=_auth(token))
    assert first.status_code == 200

    second = client.post(f"{CANCEL_URL}/{run_id}/cancel", headers=_auth(token))
    assert second.status_code == 409


def test_cancel_writes_an_audit_row(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token, name="sp-cancel-audit")

    run_id = _repo().start(connection_id=conn_id)
    r = client.post(f"{CANCEL_URL}/{run_id}/cancel", headers=_auth(token))
    assert r.status_code == 200

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="sharepoint_extraction_run.cancel", limit=10)
    matching = [row for row in rows if run_id in str(row.get("resource") or "")]
    assert matching, "cancel must write its own audit row naming the run"
    params = matching[0].get("params") or {}
    if isinstance(params, str):
        import json

        params = json.loads(params)
    assert params.get("connection_id") == conn_id


def test_status_reports_facts_pending_documents_and_pass_running(tmp_path, monkeypatch, pg_engine):
    """TCRD-296 gap #61: `facts_pending_documents` (the connection's whole
    outstanding backlog, a connection-level fact, not a run's own
    progress) and `facts_pass_running` (is anything chasing it right now)
    — the two fields that make "pending but nothing running" visible. The
    backlog COUNT's own correctness is covered end-to-end in
    `tests/db_pg/test_facts_extraction_pg.py`; this proves the status
    endpoint actually surfaces it and keeps `facts_pass_running` in sync
    with `facts_job`."""
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token)

    body = client.get(f"{BASE}/{conn_id}/extraction/status", headers=_auth(token)).json()
    assert body["facts_pending_documents"] == 0
    assert body["facts_pass_running"] is False

    from src.repositories import jobs_repo

    jobs_repo().enqueue(
        "sharepoint-facts-extraction",
        {"connection_id": conn_id},
        idempotency_key=f"sharepoint-facts-extraction:{conn_id}",
    )
    body = client.get(f"{BASE}/{conn_id}/extraction/status", headers=_auth(token)).json()
    assert body["facts_pass_running"] is True


def test_fleet_row_carries_facts_pending_documents_and_pass_running(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token)

    body = client.get(f"{FLEET_URL}?all=1", headers=_auth(token)).json()
    row = next(r for r in body["connections"] if r["connection_id"] == conn_id)
    assert row["facts"]["facts_pending_documents"] == 0
    assert row["facts"]["facts_pass_running"] is False

    from src.repositories import jobs_repo

    jobs_repo().enqueue(
        "sharepoint-facts-extraction",
        {"connection_id": conn_id},
        idempotency_key=f"sharepoint-facts-extraction:{conn_id}",
    )
    body = client.get(f"{FLEET_URL}?all=1", headers=_auth(token)).json()
    row = next(r for r in body["connections"] if r["connection_id"] == conn_id)
    assert row["facts"]["facts_pass_running"] is True


# ---------------------------------------------------------------------------
# Shard roll-up (2026-09-03 auto-parallel-crawl design §4.7, plan Task 8) —
# a PARENT (planner) run row joined with its CHILD rows through the fleet,
# status, history and detail endpoints alike. The runtime that actually
# WRITES these rows (the planner, the shard child crawl, the finalizer)
# lives in `connectors/sharepoint/crawler.py`; these tests drive the same
# `ExtractionRunsPgRepository` primitives it uses directly, exactly the way
# every other test in this file drives `_repo()` rather than running a real
# crawl.
# ---------------------------------------------------------------------------


def _shard_plan_state(conn_id: str, shards: list) -> None:
    """Seed `state["shard_plan"]` — the ONLY place a shard's `expected`
    (plan) document count lives (`app.api.admin_extraction.
    _shard_expected_by_key` reads it back by `state_key`, never off the run
    row itself)."""
    from src.repositories import sharepoint_state_repo

    sharepoint_state_repo().put(conn_id, "crawl", {"shard_plan": {"shards": shards}})


def test_fleet_row_rolls_up_a_sharded_sites_children(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token, name="sp-sharded-fleet")

    _shard_plan_state(
        conn_id,
        [
            {"label": "part 1/2", "targets": [{"state_key": "drive-1:item-1"}], "expected": 30},
            {"label": "part 2/2", "targets": [{"state_key": "drive-1:item-2"}], "expected": 10},
        ],
    )

    repo = _repo()
    parent_id = repo.start(connection_id=conn_id, shards_total=2)
    child_a = repo.start(
        connection_id=conn_id, parent_run_id=parent_id, shard_key="drive-1:item-1", shard_label="part 1/2"
    )
    repo.checkpoint(child_a, files_seen=25, files_done=25, progress={"new": 20, "unchanged": 5})
    child_b = repo.start(
        connection_id=conn_id, parent_run_id=parent_id, shard_key="drive-1:item-2", shard_label="part 2/2"
    )
    repo.finish(child_b, status="done", report={"new": 8, "unchanged": 2}, files_seen=10, files_done=10)
    repo.finish_shard(parent_id)

    body = client.get(f"{FLEET_URL}?all=1", headers=_auth(token)).json()
    rows = [r for r in body["connections"] if r["connection_id"] == conn_id]
    # A child never shows up as its OWN fleet row — one row per SITE.
    assert len(rows) == 1
    run = rows[0]["run"]
    assert run["id"] == parent_id
    assert run["mode"] == "sharded"
    assert run["shards_total"] == 2
    assert run["shards_done"] == 1
    shards = {s["label"]: s for s in run["shards"]}
    assert shards["part 1/2"]["expected"] == 30
    assert shards["part 1/2"]["outcome"] == "running"
    assert shards["part 2/2"]["expected"] == 10
    assert shards["part 2/2"]["outcome"] == "done"
    assert run["expected_documents"] == 40
    assert run["seen_documents"] == (20 + 5) + (8 + 2)


def test_fleet_row_marks_stuck_when_one_shard_stalls_even_if_the_parent_checkpoint_is_fresh(
    tmp_path, monkeypatch, pg_engine
):
    """A sibling shard's own checkpoint keeps bumping the parent's
    `checkpoint_at` — the parent alone would never look stalled. A dead
    shard must still surface."""
    import sqlalchemy as sa

    import app.api.admin_extraction as mod
    from src.db_pg import get_engine

    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token, name="sp-sharded-stuck")

    repo = _repo()
    parent_id = repo.start(connection_id=conn_id, shards_total=2)
    stale_child = repo.start(connection_id=conn_id, parent_run_id=parent_id, shard_key="k1", shard_label="part 1/2")
    live_child = repo.start(connection_id=conn_id, parent_run_id=parent_id, shard_key="k2", shard_label="part 2/2")
    repo.checkpoint(live_child, files_seen=5, files_done=5)  # bumps the PARENT's own checkpoint_at too

    with get_engine().begin() as conn:
        conn.execute(
            sa.text("UPDATE extraction_runs SET checkpoint_at = now() - make_interval(secs => :s) WHERE id = :id"),
            {"s": mod._STALL_AFTER_S + 600, "id": stale_child},
        )

    body = client.get(f"{FLEET_URL}?all=1", headers=_auth(token)).json()
    row = next(r for r in body["connections"] if r["connection_id"] == conn_id)
    assert row["run"]["outcome"] == "running"  # the parent's OWN checkpoint is fresh (bumped by live_child)
    shards = {s["label"]: s for s in row["run"]["shards"]}
    assert shards["part 1/2"]["stuck"] is True
    assert shards["part 2/2"]["stuck"] is False
    assert row["stuck"] is True  # the fleet's own flag folds in any shard's stuck flag


def test_run_detail_includes_shards_for_a_sharded_run(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token, name="sp-sharded-detail")

    repo = _repo()
    parent_id = repo.start(connection_id=conn_id, shards_total=1)
    child_id = repo.start(
        connection_id=conn_id, parent_run_id=parent_id, shard_key="drive-1", shard_label="whole drive"
    )
    repo.finish(child_id, status="done", report={"new": 5}, files_seen=5, files_done=5)
    repo.finish_shard(parent_id)
    repo.finish(parent_id, status="done", report={"new": 5, "shards_total": 1}, files_seen=5, files_done=5)

    detail = client.get(f"{BASE}/{conn_id}/extraction/runs/{parent_id}", headers=_auth(token)).json()
    assert detail["mode"] == "sharded"
    assert len(detail["shards"]) == 1
    assert detail["shards"][0]["outcome"] == "done"
    assert detail["shards"][0]["label"] == "whole drive"


def test_run_history_includes_shards_and_never_lists_a_child_as_its_own_row(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token, name="sp-sharded-history")

    repo = _repo()
    parent_id = repo.start(connection_id=conn_id, shards_total=1)
    child_id = repo.start(
        connection_id=conn_id, parent_run_id=parent_id, shard_key="drive-1", shard_label="whole drive"
    )
    repo.finish(child_id, status="done", report={"new": 5}, files_seen=5, files_done=5)
    repo.finish_shard(parent_id)
    repo.finish(parent_id, status="done", report={"new": 5}, files_seen=5, files_done=5)

    body = client.get(f"{BASE}/{conn_id}/extraction/runs", headers=_auth(token)).json()
    assert [r["id"] for r in body["runs"]] == [parent_id]
    assert body["total"] == 1
    assert body["runs"][0]["shards"][0]["outcome"] == "done"


def test_status_running_includes_the_shard_rollup_for_a_sharded_site(tmp_path, monkeypatch, pg_engine):
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token, name="sp-sharded-status")

    repo = _repo()
    parent_id = repo.start(connection_id=conn_id, shards_total=2)
    repo.start(connection_id=conn_id, parent_run_id=parent_id, shard_key="k1", shard_label="part 1/2")
    repo.start(connection_id=conn_id, parent_run_id=parent_id, shard_key="k2", shard_label="part 2/2")

    body = client.get(f"{BASE}/{conn_id}/extraction/status", headers=_auth(token)).json()
    running = body["running"]
    assert running["id"] == parent_id
    assert running["mode"] == "sharded"
    assert len(running["shards"]) == 2


def test_a_plain_inline_run_reports_inline_mode_through_every_endpoint(tmp_path, monkeypatch, pg_engine):
    """An ordinary (non-sharded) connection's run must not gain a phantom
    `shards` list anywhere — additive keys stay `None` when there is
    nothing to roll up."""
    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token, name="sp-inline")

    repo = _repo()
    run_id = repo.start(connection_id=conn_id)
    repo.finish(run_id, status="done", report={"new": 3}, files_seen=3, files_done=3)

    status = client.get(f"{BASE}/{conn_id}/extraction/status", headers=_auth(token)).json()
    assert status["last_completed"]["mode"] == "inline"
    assert status["last_completed"]["shards"] is None

    fleet = client.get(f"{FLEET_URL}?all=1", headers=_auth(token)).json()
    row = next(r for r in fleet["connections"] if r["connection_id"] == conn_id)
    assert row["run"]["mode"] == "inline"
    assert row["run"]["shards"] is None


# ---------------------------------------------------------------------------
# Partitioned facts passes (TCRD-296 gap #67) — fleet/status additive fields
# ---------------------------------------------------------------------------


def test_status_reports_every_partition_job_and_throughput_eta(tmp_path, monkeypatch, pg_engine):
    from datetime import timedelta

    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token, name="sp-partitioned")

    from src.repositories import facts_ingest_runs_repo, jobs_repo, source_connections_repo

    source_connections_repo().update(conn_id, config={"scopes": [{"source_scope_id": "s1", "collection_id": "col_a"}]})

    jobs_repo().enqueue(
        "sharepoint-facts-extraction",
        {"connection_id": conn_id, "partition": {"index": 0, "count": 2}},
        idempotency_key=f"sharepoint-facts-extraction:{conn_id}:0/2",
    )
    jobs_repo().enqueue(
        "sharepoint-facts-extraction",
        {"connection_id": conn_id, "partition": {"index": 1, "count": 2}},
        idempotency_key=f"sharepoint-facts-extraction:{conn_id}:1/2",
    )
    run_id = facts_ingest_runs_repo().create(
        corpus_ids=["col_a"],
        caller="scheduler@system.local",
        documents_seen=20,
        claims_written=0,
        claims_rejected=[],
        deferred=[],
        subjects_created=0,
        subjects_deleted=0,
        review_items=[],
    )
    with pg_engine.begin() as conn:
        import sqlalchemy as sa

        from datetime import datetime, timezone

        conn.execute(
            sa.text("UPDATE facts_ingest_runs SET created_at = :ts WHERE id = :id"),
            {"ts": datetime.now(timezone.utc) - timedelta(minutes=1), "id": run_id},
        )

    status = client.get(f"{BASE}/{conn_id}/extraction/status", headers=_auth(token)).json()
    assert status["facts_passes_total"] == 2
    assert status["facts_passes_running"] == 0
    assert [j["partition_index"] for j in status["facts_jobs"]] == [0, 1]
    assert status["facts_docs_per_hour"] == 120.0  # 20 documents in 10 minutes -> *6

    fleet = client.get(f"{FLEET_URL}?all=1", headers=_auth(token)).json()
    row = next(r for r in fleet["connections"] if r["connection_id"] == conn_id)
    assert row["facts"]["facts_passes_total"] == 2
    assert len(row["facts"]["facts_jobs"]) == 2
    assert row["facts"]["facts_docs_per_hour"] == 120.0
