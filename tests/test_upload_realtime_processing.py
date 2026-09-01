"""Uploaded sessions are summarized on arrival, not on the next sweep tick.

``POST /api/upload/sessions`` schedules ``process_single_session`` as a
FastAPI background task, so the tokens of a session the CLI just pushed are
queryable seconds later instead of up to one ten-minute sweep later. Every
assertion below is about what the upload request ALONE leaves behind — the
sweep is never invoked in this file, which is the whole point.

The sweep still exists as the catch-up path, and the two must not fight: the
``session_processor_state`` hash ledger makes whichever runs second a no-op,
covered here from the upload side (a re-upload of unchanged content) and in
``tests/test_usage_processor_turns.py`` from the processor side.
"""

from __future__ import annotations

import io
import json

import pytest


#: ``_seed_users_and_mint_tokens``'s non-admin user. Uploads land in
#: ``${DATA_DIR}/user_sessions/<user id>/``, so this is also the directory
#: name the one-shot resolves identity from.
ANALYST_ID = "analyst1"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _jsonl(events: list[dict]) -> bytes:
    return ("\n".join(json.dumps(e) for e in events) + "\n").encode()


def _one_turn_session() -> bytes:
    """One user + one assistant turn carrying a full ``message.usage`` block."""
    return _jsonl(
        [
            {
                "type": "user",
                "uuid": "u-1",
                "parentUuid": None,
                "sessionId": "sess-upload-1",
                "timestamp": "2026-08-31T10:00:00.000Z",
                "message": {"role": "user", "content": "hello"},
            },
            {
                "type": "assistant",
                "uuid": "a-1",
                "parentUuid": "u-1",
                "sessionId": "sess-upload-1",
                "timestamp": "2026-08-31T10:00:05.000Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-5",
                    "content": [{"type": "text", "text": "hi"}],
                    "usage": {
                        "input_tokens": 11,
                        "output_tokens": 22,
                        "cache_read_input_tokens": 333,
                        "cache_creation_input_tokens": 44,
                    },
                },
            },
        ]
    )


def _summary(filename: str) -> dict | None:
    from src.repositories import usage_repo

    return usage_repo().get_session_summary(f"{ANALYST_ID}/{filename}")


@pytest.fixture
def upload_client(seeded_app, monkeypatch):
    """``seeded_app``'s client with the session root pinned to its DATA_DIR.

    The pipeline reads ``$SESSION_DATA_DIR`` while the endpoint writes under
    ``${DATA_DIR}/user_sessions``; a normal deployment has them equal, and
    pinning them here keeps the test from depending on whatever
    ``/data/user_sessions`` happens to hold on the machine running it.
    """
    monkeypatch.setenv("SESSION_DATA_DIR", str(seeded_app["env"]["data_dir"] / "user_sessions"))
    return seeded_app["client"]


def _upload(client, token: str, name: str, content: bytes):
    return client.post(
        "/api/upload/sessions",
        files={"file": (name, io.BytesIO(content), "application/jsonl")},
        headers=_auth(token),
    )


class TestRealtimeIngestOnUpload:
    def test_the_session_is_summarized_by_the_upload_alone(self, upload_client, seeded_app):
        """No sweep runs in this test; the summary must exist regardless."""
        resp = _upload(upload_client, seeded_app["analyst_token"], "realtime.jsonl", _one_turn_session())
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        row = _summary("realtime.jsonl")
        assert row is not None
        assert row["session_id"] == "sess-upload-1"
        assert row["assistant_messages"] == 1
        assert row["input_tokens"] == 11
        assert row["output_tokens"] == 22
        assert row["cache_read_tokens"] == 333
        assert row["cache_creation_tokens"] == 44

    def test_reuploading_unchanged_content_is_a_no_op(self, upload_client, seeded_app):
        """The CLI re-pushes a session it already pushed; the hash ledger must
        absorb it — one summary row, unchanged totals, ledger still current."""
        from services.session_pipeline.lib import compute_file_hash
        from src.repositories import session_processor_state_repo, usage_repo

        token = seeded_app["analyst_token"]
        assert _upload(upload_client, token, "twice.jsonl", _one_turn_session()).status_code == 200
        assert _upload(upload_client, token, "twice.jsonl", _one_turn_session()).status_code == 200

        row = _summary("twice.jsonl")
        assert row["input_tokens"] == 11
        assert row["cache_read_tokens"] == 333

        sessions = usage_repo().list_sessions_for_user_self(ANALYST_ID)
        assert [s["session_file"] for s in sessions] == [f"{ANALYST_ID}/twice.jsonl"]

        path = seeded_app["env"]["data_dir"] / "user_sessions" / ANALYST_ID / "twice.jsonl"
        assert session_processor_state_repo().is_processed(
            "usage", f"{ANALYST_ID}/twice.jsonl", compute_file_hash(path)
        )

    def test_a_corrupt_upload_still_answers_ok_and_records_nothing(self, upload_client, seeded_app):
        """A file the processor cannot read (here: not even UTF-8) must not
        turn into a failed upload — the one-shot is fire-and-forget, so its
        failure is logged, the transcript is stored, and the sweep retries."""
        resp = _upload(upload_client, seeded_app["analyst_token"], "corrupt.jsonl", b"\xff\xfe not json\n")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        assert _summary("corrupt.jsonl") is None

        stored = seeded_app["env"]["data_dir"] / "user_sessions" / ANALYST_ID / "corrupt.jsonl"
        assert stored.exists(), "the upload itself must still have been written"

    def test_a_failing_one_shot_never_escapes_into_the_response(self, upload_client, seeded_app, monkeypatch):
        """Background tasks run inside the ASGI call: an exception raised there
        would surface as a 500 on an upload that in fact succeeded."""
        from services.session_pipeline import runner

        monkeypatch.setattr(
            runner,
            "compute_file_hash",
            lambda _p: (_ for _ in ()).throw(OSError("disk went away mid-read")),
        )

        resp = _upload(upload_client, seeded_app["analyst_token"], "unreadable.jsonl", _one_turn_session())
        assert resp.status_code == 200
        assert _summary("unreadable.jsonl") is None
