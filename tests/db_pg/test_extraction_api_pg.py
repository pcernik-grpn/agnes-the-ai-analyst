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
