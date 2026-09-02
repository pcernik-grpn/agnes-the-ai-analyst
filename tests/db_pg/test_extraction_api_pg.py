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
    assert body["runs_total"] == 0
    assert body["as_of"]


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


def test_fleet_row_flags_a_stuck_run_past_the_fleet_threshold(tmp_path, monkeypatch, pg_engine):
    import sqlalchemy as sa

    import app.api.admin_extraction as mod

    client, token = _pg_client(tmp_path, monkeypatch, pg_engine)
    conn_id = _connection(client, token, name="sp-stuck")

    run_id = _repo().start(connection_id=conn_id)
    from src.db_pg import get_engine

    with get_engine().begin() as conn:
        conn.execute(
            sa.text("UPDATE extraction_runs SET checkpoint_at = now() - make_interval(secs => :s) WHERE id = :id"),
            {"s": mod._FLEET_STUCK_AFTER_S + 60, "id": run_id},
        )

    body = client.get(f"{FLEET_URL}?active=1", headers=_auth(token)).json()
    row = body["connections"][0]
    assert row["stuck"] is True
    assert row["checkpoint_age_s"] > mod._FLEET_STUCK_AFTER_S
    assert body["totals"]["stuck"] == 1


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
