"""`app.chat.artifact_harvest.harvest_session_artifacts` +
`GET /api/v1/sessions/{id}/artifacts` + `GET .../artifacts/{artifact_id}`
(V1b Task 5).

Harvest unit tests fake the sandbox at the file-API seam (`handle.files.list`
/ `handle.files.read` — the real E2B shape, see `E2BSandboxHandle.files` in
`app/chat/e2b_provider.py`) and the object store / repo at their module-level
factory functions in `app.chat.artifact_harvest` — these are NOT
chat-sandbox integration tests, there is no real E2B sandbox involved
anywhere in this file.

API tests reuse the `env`/`FakeManager`/agent-PAT helpers pattern from
`tests/test_agent_sessions_api.py`: real `chat_session_repo()`-backed
sessions, real `agent_artifacts_repo()` rows, a fake `object_store()`.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.auth.jwt import create_access_token


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Fakes for harvest_session_artifacts unit tests
# ---------------------------------------------------------------------------


class FakeEntry:
    def __init__(self, name: str, is_dir: bool = False) -> None:
        self.name = name
        self.type = "DIR" if is_dir else "FILE"


class FakeFilesAPI:
    """Fakes the E2B `sandbox.files` surface (`.list`/`.read`) at the shape
    `app.chat.artifact_harvest` actually calls it with — NOT a filesystem.
    """

    def __init__(self, outputs: dict[str, bytes] | None = None, raise_on_list: bool = False) -> None:
        self._outputs = outputs or {}
        self._raise_on_list = raise_on_list
        self.read_calls: list[str] = []

    async def list(self, path: str) -> list[FakeEntry]:
        if self._raise_on_list:
            raise FileNotFoundError(path)
        return [FakeEntry(name) for name in self._outputs]

    async def read(self, path: str, format: str = "bytes") -> bytes:  # noqa: A002 - mirrors real SDK kwarg name
        self.read_calls.append(path)
        raw_name = path.split("/outputs/", 1)[-1]
        return self._outputs[raw_name]


class FakeHandle:
    """Fakes `E2BSandboxHandle` at the one surface `harvest_session_artifacts`
    touches: `.files`."""

    def __init__(self, outputs: dict[str, bytes] | None = None, raise_on_list: bool = False) -> None:
        self.files = FakeFilesAPI(outputs=outputs, raise_on_list=raise_on_list)


class FakeObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    def put_bytes(self, key: str, data: bytes, md5: str) -> None:
        self.objects[key] = (data, md5)

    def get_bytes(self, key: str):
        entry = self.objects.get(key)
        return entry[0] if entry else None

    def presign_get(self, key: str, ttl_s: int = 900) -> str:
        return f"https://fake-object-store.example/{key}?ttl={ttl_s}"


class FakeArtifactsRepo:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def create(
        self,
        id,
        session_id,
        agent_id,
        owner_user_id,
        filename,
        object_key,
        size_bytes,
        content_type,
        md5,
    ) -> None:
        self.rows[id] = {
            "id": id,
            "session_id": session_id,
            "agent_id": agent_id,
            "owner_user_id": owner_user_id,
            "filename": filename,
            "object_key": object_key,
            "size_bytes": size_bytes,
            "content_type": content_type,
            "md5": md5,
            "created_at": datetime.now(timezone.utc),
        }

    def get(self, id):
        return self.rows.get(id)

    def list_for_session(self, session_id):
        return [r for r in self.rows.values() if r["session_id"] == session_id]


@pytest.fixture
def fake_store(monkeypatch):
    import app.chat.artifact_harvest as ah

    store = FakeObjectStore()
    monkeypatch.setattr(ah, "object_store", lambda: store)
    return store


@pytest.fixture
def fake_repo(monkeypatch):
    import app.chat.artifact_harvest as ah

    repo = FakeArtifactsRepo()
    monkeypatch.setattr(ah, "agent_artifacts_repo", lambda: repo)
    return repo


# ---------------------------------------------------------------------------
# harvest_session_artifacts — unit tests
# ---------------------------------------------------------------------------


def test_harvest_two_files_creates_two_rows(fake_store, fake_repo):
    from app.chat.artifact_harvest import harvest_session_artifacts

    handle = FakeHandle(outputs={"report.csv": b"a,b,c\n1,2,3\n", "summary.txt": b"done"})
    result = asyncio.run(harvest_session_artifacts("sess-1", "agent-1", "owner-1", handle))

    assert len(result) == 2
    filenames = {r["filename"] for r in result}
    assert filenames == {"report.csv", "summary.txt"}
    for r in result:
        assert r["md5"]
        key = f"agent-artifacts/sess-1/{r['filename']}"
        assert r["object_key"] == key
        assert key in fake_store.objects
    assert len(fake_repo.rows) == 2
    for row in fake_repo.rows.values():
        assert row["object_key"].startswith("agent-artifacts/sess-1/")
        assert row["session_id"] == "sess-1"
        assert row["agent_id"] == "agent-1"
        assert row["owner_user_id"] == "owner-1"


def test_harvest_md5_matches_content(fake_store, fake_repo):
    from app.chat.artifact_harvest import harvest_session_artifacts

    data = b"hello world"
    handle = FakeHandle(outputs={"hello.txt": data})
    result = asyncio.run(harvest_session_artifacts("sess-md5", None, "owner-1", handle))
    assert len(result) == 1
    assert result[0]["md5"] == hashlib.md5(data).hexdigest()


def test_harvest_missing_outputs_dir_returns_empty_no_error(fake_store, fake_repo):
    from app.chat.artifact_harvest import harvest_session_artifacts

    handle = FakeHandle(raise_on_list=True)
    result = asyncio.run(harvest_session_artifacts("sess-2", None, "owner-1", handle))
    assert result == []
    assert fake_repo.rows == {}


def test_harvest_empty_outputs_dir_returns_empty(fake_store, fake_repo):
    from app.chat.artifact_harvest import harvest_session_artifacts

    handle = FakeHandle(outputs={})
    result = asyncio.run(harvest_session_artifacts("sess-3", None, "owner-1", handle))
    assert result == []


def test_harvest_object_store_none_returns_empty_no_raise(monkeypatch, fake_repo):
    import app.chat.artifact_harvest as ah
    from app.chat.artifact_harvest import harvest_session_artifacts

    monkeypatch.setattr(ah, "object_store", lambda: None)
    handle = FakeHandle(outputs={"a.txt": b"1"})
    result = asyncio.run(harvest_session_artifacts("sess-4", None, "owner-1", handle))
    assert result == []
    assert fake_repo.rows == {}


def test_harvest_skips_oversize_file(fake_store, fake_repo):
    from app.chat.artifact_harvest import harvest_session_artifacts

    handle = FakeHandle(outputs={"small.txt": b"ok", "big.bin": b"x" * 100})
    result = asyncio.run(harvest_session_artifacts("sess-5", None, "owner-1", handle, max_bytes=50))
    assert len(result) == 1
    assert result[0]["filename"] == "small.txt"
    assert "agent-artifacts/sess-5/big.bin" not in fake_store.objects


def test_harvest_stops_after_max_files_cap(fake_store, fake_repo):
    from app.chat.artifact_harvest import harvest_session_artifacts

    handle = FakeHandle(outputs={"a.txt": b"1", "b.txt": b"2", "c.txt": b"3"})
    result = asyncio.run(harvest_session_artifacts("sess-6", None, "owner-1", handle, max_files=2))
    assert len(result) == 2


def test_harvest_byte_cap_is_cumulative_per_session(fake_store, fake_repo):
    """max_bytes must bound the SUM of bytes harvested this call, not each
    file individually — three 4-byte files with max_bytes=10 should stop
    after two (8 bytes), skipping the third rather than allowing all three
    (12 bytes total) through just because each is under the cap alone."""
    from app.chat.artifact_harvest import harvest_session_artifacts

    handle = FakeHandle(
        outputs={"a.txt": b"aaaa", "b.txt": b"bbbb", "c.txt": b"cccc"},
    )
    result = asyncio.run(harvest_session_artifacts("sess-cumulative", None, "owner-1", handle, max_bytes=10))

    assert len(result) == 2
    total = sum(r["size_bytes"] for r in result)
    assert total <= 10
    harvested_names = {r["filename"] for r in result}
    assert harvested_names.issubset({"a.txt", "b.txt", "c.txt"})
    assert len(fake_repo.rows) == 2


def test_harvest_sanitizes_path_traversal_filename(fake_store, fake_repo):
    from app.chat.artifact_harvest import harvest_session_artifacts

    handle = FakeHandle(outputs={"../evil": b"pwned"})
    result = asyncio.run(harvest_session_artifacts("sess-7", None, "owner-1", handle))
    assert len(result) == 1
    assert result[0]["filename"] == "evil"
    assert result[0]["object_key"] == "agent-artifacts/sess-7/evil"


def test_harvest_strips_crlf_from_filename(fake_store, fake_repo):
    from app.chat.artifact_harvest import harvest_session_artifacts

    handle = FakeHandle(outputs={"evil\r\nX-Injected: true": b"data"})
    result = asyncio.run(harvest_session_artifacts("sess-8", None, "owner-1", handle))
    assert len(result) == 1
    assert "\r" not in result[0]["filename"]
    assert "\n" not in result[0]["filename"]


def test_sanitize_filename_strips_double_quote():
    """A raw filename containing `"` must not survive into the sanitized
    basename — otherwise a Content-Disposition header built as
    `filename="{name}"` breaks out of the quoted string early."""
    from app.chat.artifact_harvest import sanitize_filename

    assert '"' not in sanitize_filename('a".txt')
    assert '"' not in sanitize_filename('evil".txt"; x=y')


def test_harvest_twice_dedupes_same_object_key(fake_store, fake_repo):
    """Re-harvesting a session (e.g. once at run completion, once again at
    DELETE teardown) must not create a second `agent_artifacts` row for a
    file it already harvested — one row per object_key, not two."""
    from app.chat.artifact_harvest import harvest_session_artifacts

    handle = FakeHandle(outputs={"report.csv": b"a,b,c\n1,2,3\n"})

    first = asyncio.run(harvest_session_artifacts("sess-dedupe", None, "owner-1", handle))
    assert len(first) == 1

    second = asyncio.run(harvest_session_artifacts("sess-dedupe", None, "owner-1", handle))
    assert second == []

    assert len(fake_repo.rows) == 1
    object_keys = {r["object_key"] for r in fake_repo.rows.values()}
    assert object_keys == {"agent-artifacts/sess-dedupe/report.csv"}


def test_harvest_handle_without_files_api_returns_empty(fake_store, fake_repo):
    from app.chat.artifact_harvest import harvest_session_artifacts

    class NoFilesHandle:
        pass

    result = asyncio.run(harvest_session_artifacts("sess-9", None, "owner-1", NoFilesHandle()))
    assert result == []


def test_caps_from_manager_reads_chat_config():
    from app.chat.artifact_harvest import caps_from_manager

    class FakeConfig:
        agent_api_artifact_max_bytes = 123
        agent_api_artifact_max_files = 4

    class FakeManagerWithConfig:
        _config = FakeConfig()

    assert caps_from_manager(FakeManagerWithConfig()) == {"max_bytes": 123, "max_files": 4}


def test_caps_from_manager_empty_dict_when_no_config():
    from app.chat.artifact_harvest import caps_from_manager

    class BareManager:
        pass

    assert caps_from_manager(BareManager()) == {}


# ---------------------------------------------------------------------------
# API tests — GET /api/v1/sessions/{id}/artifacts (+/{artifact_id})
# ---------------------------------------------------------------------------


class FakeManager:
    """Minimal manager fake — only `create_session` is exercised by these
    tests (list/download don't touch the manager at all)."""

    async def create_session(self, *, user_email, surface, agent_id=None, **kwargs):
        from src.repositories import chat_session_repo

        return chat_session_repo().create_session(
            user_email=user_email,
            surface=surface,
            agent_id=agent_id,
        )


@pytest.fixture
def env(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")

    from src.db import SYSTEM_EVERYONE_GROUP, get_system_db
    from src.repositories import agents_repo, resource_grants_repo, user_group_members_repo, user_groups_repo
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="owner1", email="owner@test.com", name="Owner")
    UserRepository(conn).create(id="other1", email="other@test.com", name="Other")
    conn.close()

    everyone = user_groups_repo().get_by_name(SYSTEM_EVERYONE_GROUP)
    user_group_members_repo().add_member("owner1", everyone["id"], source="system_seed")
    user_group_members_repo().add_member("other1", everyone["id"], source="system_seed")
    resource_grants_repo().create(everyone["id"], "chat", "chat")

    agent_id = str(uuid.uuid4())
    agents_repo().create(id=agent_id, owner_user_id="owner1", name="Support Bot", slug="support-bot")
    other_agent_id = str(uuid.uuid4())
    agents_repo().create(id=other_agent_id, owner_user_id="owner1", name="Other Agent", slug="other-agent")

    client = TestClient(shared_app)
    return {
        "client": client,
        "owner_token": create_access_token("owner1", "owner@test.com"),
        "other_token": create_access_token("other1", "other@test.com"),
        "agent_id": agent_id,
        "other_agent_id": other_agent_id,
    }


def _mint_agent_pat(owner_email: str, owner_id: str, agent_id: str, token_id: str) -> str:
    return create_access_token(
        user_id=owner_id,
        email=owner_email,
        token_id=token_id,
        typ="agent_pat",
        extra_claims={"agent_id": agent_id},
    )


def _register_agent_pat_row(owner_id: str, agent_id: str, token: str, token_id: str) -> None:
    from src.repositories import access_token_repo

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    access_token_repo().create(
        id=token_id,
        user_id=owner_id,
        name="agent-pat",
        token_hash=token_hash,
        prefix=token_id.replace("-", "")[:8],
        agent_id=agent_id,
    )


def _create_session(env, monkeypatch, slug="support-bot", token=None) -> str:
    import app.api.agent_sessions as agent_sessions

    manager = FakeManager()
    monkeypatch.setattr(agent_sessions, "get_current_chat_manager", lambda: manager)
    r = env["client"].post(
        f"/api/v1/agents/{slug}/sessions",
        json={},
        headers=_auth(token or env["owner_token"]),
    )
    assert r.status_code == 201, r.text
    return r.json()["session_id"]


def _seed_artifact(session_id: str, agent_id: str, **overrides) -> str:
    from src.repositories import agent_artifacts_repo

    artifact_id = overrides.pop("id", str(uuid.uuid4()))
    kwargs = {
        "id": artifact_id,
        "session_id": session_id,
        "agent_id": agent_id,
        "owner_user_id": "owner1",
        "filename": "report.csv",
        "object_key": f"agent-artifacts/{session_id}/report.csv",
        "size_bytes": 12,
        "content_type": "text/csv",
        "md5": "deadbeef",
    }
    kwargs.update(overrides)
    agent_artifacts_repo().create(**kwargs)
    return artifact_id


def test_list_artifacts_owner(env, monkeypatch):
    session_id = _create_session(env, monkeypatch)
    artifact_id = _seed_artifact(session_id, env["agent_id"])

    r = env["client"].get(f"/api/v1/sessions/{session_id}/artifacts", headers=_auth(env["owner_token"]))
    assert r.status_code == 200
    body = r.json()
    assert body["has_more"] is False
    assert body["next_cursor"] is None
    assert len(body["data"]) == 1
    row = body["data"][0]
    assert row["id"] == artifact_id
    assert row["filename"] == "report.csv"
    assert row["size_bytes"] == 12
    assert row["content_type"] == "text/csv"
    assert row["created_at"]


def test_list_artifacts_cross_owner_returns_404(env, monkeypatch):
    session_id = _create_session(env, monkeypatch)
    _seed_artifact(session_id, env["agent_id"])

    r = env["client"].get(f"/api/v1/sessions/{session_id}/artifacts", headers=_auth(env["other_token"]))
    assert r.status_code == 404


def test_download_artifact_streams_bytes(env, monkeypatch):
    import app.api.agent_sessions as agent_sessions

    session_id = _create_session(env, monkeypatch)
    store = FakeObjectStore()
    key = f"agent-artifacts/{session_id}/report.csv"
    store.put_bytes(key, b"a,b,c\n1,2,3\n", "deadbeef")
    monkeypatch.setattr(agent_sessions, "object_store", lambda: store)

    artifact_id = _seed_artifact(session_id, env["agent_id"], object_key=key)

    r = env["client"].get(
        f"/api/v1/sessions/{session_id}/artifacts/{artifact_id}",
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 200
    assert r.content == b"a,b,c\n1,2,3\n"
    assert r.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="report.csv"' in r.headers["content-disposition"]


def test_download_artifact_sanitizes_filename_in_header(env, monkeypatch):
    import app.api.agent_sessions as agent_sessions

    session_id = _create_session(env, monkeypatch)
    store = FakeObjectStore()
    key = f"agent-artifacts/{session_id}/evil"
    store.put_bytes(key, b"data", "abc")
    monkeypatch.setattr(agent_sessions, "object_store", lambda: store)

    artifact_id = _seed_artifact(
        session_id,
        env["agent_id"],
        filename="../evil",
        object_key=key,
        content_type="text/plain",
    )

    r = env["client"].get(
        f"/api/v1/sessions/{session_id}/artifacts/{artifact_id}",
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 200
    assert 'filename="evil"' in r.headers["content-disposition"]
    assert ".." not in r.headers["content-disposition"]


def test_download_artifact_quote_in_filename_produces_well_formed_header(env, monkeypatch):
    """A stored filename containing `"` (e.g. from a pre-fix row, or any
    path that bypassed sanitize_filename before storage) must not produce
    a malformed `filename="a".txt"` Content-Disposition header — the quote
    is stripped so the header's quoted string stays well-formed."""
    import app.api.agent_sessions as agent_sessions

    session_id = _create_session(env, monkeypatch)
    store = FakeObjectStore()
    key = f"agent-artifacts/{session_id}/quoted"
    store.put_bytes(key, b"data", "abc")
    monkeypatch.setattr(agent_sessions, "object_store", lambda: store)

    artifact_id = _seed_artifact(
        session_id,
        env["agent_id"],
        filename='a".txt',
        object_key=key,
        content_type="text/plain",
    )

    r = env["client"].get(
        f"/api/v1/sessions/{session_id}/artifacts/{artifact_id}",
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 200
    header = r.headers["content-disposition"]
    # Well-formed: exactly two quote characters, the opening/closing pair
    # around the filename value — a quote embedded in the filename itself
    # would otherwise close the value early and leave a dangling `.txt"`.
    assert header.count('"') == 2
    assert header == 'attachment; filename="a.txt"'


def test_download_unknown_artifact_returns_404(env, monkeypatch):
    session_id = _create_session(env, monkeypatch)
    r = env["client"].get(
        f"/api/v1/sessions/{session_id}/artifacts/does-not-exist",
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 404


def test_download_cross_agent_pat_returns_404(env, monkeypatch):
    session_id = _create_session(env, monkeypatch)
    artifact_id = _seed_artifact(session_id, env["agent_id"])

    token_id = str(uuid.uuid4())
    token = _mint_agent_pat("owner@test.com", "owner1", env["other_agent_id"], token_id)
    _register_agent_pat_row("owner1", env["other_agent_id"], token, token_id)

    r = env["client"].get(
        f"/api/v1/sessions/{session_id}/artifacts/{artifact_id}",
        headers=_auth(token),
    )
    assert r.status_code == 404


def test_download_matching_agent_pat_succeeds(env, monkeypatch):
    import app.api.agent_sessions as agent_sessions

    session_id = _create_session(env, monkeypatch)
    store = FakeObjectStore()
    key = f"agent-artifacts/{session_id}/report.csv"
    store.put_bytes(key, b"data", "abc")
    monkeypatch.setattr(agent_sessions, "object_store", lambda: store)
    artifact_id = _seed_artifact(session_id, env["agent_id"], object_key=key)

    token_id = str(uuid.uuid4())
    token = _mint_agent_pat("owner@test.com", "owner1", env["agent_id"], token_id)
    _register_agent_pat_row("owner1", env["agent_id"], token, token_id)

    r = env["client"].get(
        f"/api/v1/sessions/{session_id}/artifacts/{artifact_id}",
        headers=_auth(token),
    )
    assert r.status_code == 200
    assert r.content == b"data"
