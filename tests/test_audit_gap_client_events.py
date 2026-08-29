"""Client-reported CLI audit events — POST /api/upload/audit-events
(F3 — audit-full-coverage plan, Task 9).

The server never trusts the client's chosen action string: only actions in
the server-side ``CLIENT_REPORTED_ACTIONS`` whitelist are accepted; anything
else is rejected (counted, not raised), so one bad event in a batch never
drops the rest.
"""

from __future__ import annotations

import json

from src.repositories import audit_repo

ENDPOINT = "/api/upload/audit-events"


def _event(action="query.local_offline", **params):
    return {
        "action": action,
        "params": {"tables": ["orders"], "sql_hash": "deadbeef01234567", "rows": 3, "duration_ms": 12, **params},
        "observed_at": "2026-08-29T12:00:00Z",
    }


class TestValidBatch:
    def test_valid_batch_accepted_and_rows_written(self, seeded_app, analyst_user):
        c = seeded_app["client"]
        resp = c.post(
            ENDPOINT, json={"events": [_event(), _event(action="explore.local_offline")]}, headers=analyst_user
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["accepted"] == 2
        assert body["rejected"] == 0

        rows, _ = audit_repo().query(action="query.local_offline", limit=5)
        assert rows
        params = json.loads(rows[0]["params"])
        assert params["client_reported"] is True
        assert params["observed_at"] == "2026-08-29T12:00:00Z"
        assert params["tables"] == ["orders"]
        assert params["sql_hash"] == "deadbeef01234567"
        assert rows[0]["client_kind"] == "cli"
        assert rows[0]["result"] == "success"

        explore_rows, _ = audit_repo().query(action="explore.local_offline", limit=5)
        assert explore_rows

    def test_batch_writes_one_audit_events_upload_row(self, seeded_app, analyst_user):
        c = seeded_app["client"]
        resp = c.post(ENDPOINT, json={"events": [_event(), _event()]}, headers=analyst_user)
        assert resp.status_code == 200
        rows, _ = audit_repo().query(action="audit_events.upload", limit=5)
        assert rows
        params = json.loads(rows[0]["params"])
        assert params["accepted"] == 2

    def test_never_stores_sql_text(self, seeded_app, analyst_user):
        """The endpoint has no `sql` field at all — a client that tried to
        smuggle SQL text in via an unexpected params key still only stores
        whatever the client sent under that key, but the contract (CLI side)
        is that `sql_hash` is the only SQL-derived field. This test pins the
        params shape the CLI actually sends never grows a `sql`/`query` key."""
        c = seeded_app["client"]
        resp = c.post(ENDPOINT, json={"events": [_event()]}, headers=analyst_user)
        assert resp.status_code == 200
        rows, _ = audit_repo().query(action="query.local_offline", limit=1)
        params = json.loads(rows[0]["params"])
        assert "sql" not in params
        assert "query" not in params


class TestUnknownAction:
    def test_unknown_action_rejected_others_accepted(self, seeded_app, analyst_user):
        c = seeded_app["client"]
        resp = c.post(
            ENDPOINT,
            json={"events": [_event(), _event(action="admin.delete_everything")]},
            headers=analyst_user,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["accepted"] == 1
        assert body["rejected"] == 1

        rows, _ = audit_repo().query(action="admin.delete_everything", limit=5)
        assert rows == []


class TestBatchSizeCap:
    def test_over_500_events_rejected_with_400(self, seeded_app, analyst_user):
        c = seeded_app["client"]
        events = [_event() for _ in range(501)]
        resp = c.post(ENDPOINT, json={"events": events}, headers=analyst_user)
        assert resp.status_code == 400

    def test_exactly_500_events_accepted(self, seeded_app, analyst_user):
        c = seeded_app["client"]
        events = [_event() for _ in range(500)]
        resp = c.post(ENDPOINT, json={"events": events}, headers=analyst_user)
        assert resp.status_code == 200
        assert resp.json()["accepted"] == 500


class TestOversizedParams:
    def test_oversized_params_rejected(self, seeded_app, analyst_user):
        c = seeded_app["client"]
        big = {"tables": ["x" * 4000], "sql_hash": "deadbeef01234567", "rows": 1, "duration_ms": 1}
        resp = c.post(
            ENDPOINT,
            json={"events": [{"action": "query.local_offline", "params": big, "observed_at": "2026-08-29T12:00:00Z"}]},
            headers=analyst_user,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["accepted"] == 0
        assert body["rejected"] == 1


class TestDoubleUpload:
    def test_identical_batch_uploaded_twice_double_inserts(self, seeded_app, analyst_user):
        """Documented behavior: the server does no de-dup of its own — the
        CLI's drain/commit two-phase is what prevents a re-send, not the
        server. Uploading the SAME batch twice writes TWO rows."""
        c = seeded_app["client"]
        batch = {"events": [_event()]}
        c.post(ENDPOINT, json=batch, headers=analyst_user)
        c.post(ENDPOINT, json=batch, headers=analyst_user)
        rows, _ = audit_repo().query(action="query.local_offline", limit=10)
        # at least 2 rows from this test's two identical uploads (>= guards
        # against other tests in this module contributing rows for the same
        # action within the shared-app session)
        assert len(rows) >= 2


class TestAuth:
    def test_requires_auth(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(ENDPOINT, json={"events": [_event()]})
        assert resp.status_code in (401, 403)


class TestRateLimit:
    """The endpoint is meant to be hit ~once per `agnes push`; a stolen PAT
    or rewritten CLI must not be able to grow audit_log unboundedly by
    looping the batch endpoint (review finding, 2026-08-29)."""

    def test_burst_beyond_limit_is_throttled(self, seeded_app, analyst_user):
        from app.auth.rate_limit import limiter

        limiter.enabled = True
        limiter.reset()
        try:
            c = seeded_app["client"]
            statuses = [
                c.post(ENDPOINT, json={"events": []}, headers=analyst_user).status_code
                for _ in range(31)
            ]
            assert all(s == 200 for s in statuses[:30])
            assert statuses[30] == 429
        finally:
            limiter.enabled = False
            limiter.reset()
