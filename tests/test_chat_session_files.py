"""Tests for the chat session-workspace file endpoints (#1611).

TDD suite: written before the implementation. Covers:

  - GET  /api/chat/sessions/{chat_id}/files            (list)
  - GET  /api/chat/sessions/{chat_id}/files/download   (download one file)
  - POST /api/chat/sessions/{chat_id}/files/save-artefact

Ownership: every route 404s for an unknown session and for a session owned
by a different user (same non-leaking posture as GET .../messages).

Containment: the requested path resolves inside the caller's session dir or
their own workspace (the session dir symlinks ``.claude``/``snapshots``/…
into the workspace, and skills write deliverables through those links).
A symlink escaping both bases is a 404 — the agent controls filenames and
symlink targets inside the sandbox, so they are adversarial input.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

TEST_USER = {"id": "user_files_tester", "email": "files@test.com", "is_admin": False}
OTHER_USER = {"id": "user_other", "email": "other@test.com", "is_admin": False}

CHAT_ID = "chat_filestest01"


class _FakeChatRepo:
    """get_session-only stand-in for app.state.chat_repo."""

    def __init__(self, sessions: dict[str, str]):
        # chat_id -> owner email
        self._sessions = sessions

    def get_session(self, chat_id: str):
        email = self._sessions.get(chat_id)
        if email is None:
            return None
        return SimpleNamespace(id=chat_id, user_email=email)


#: The host-path harness default. Explicit on purpose: the instance default
#: provider is ``kai-agent``, and _chat_config_for_delivery's fallback would
#: load exactly that — flipping every host-walk test onto the engine branch.
_DOCKER_CONFIG = SimpleNamespace(provider="docker")


def _make_app(
    *,
    data_dir: Path,
    sessions: dict[str, str] | None = None,
    chat_config: object | None = _DOCKER_CONFIG,
) -> FastAPI:
    os.environ["DATA_DIR"] = str(data_dir)

    from app.api.chat_session_files import require_chat_access
    from app.api.chat_session_files import router as files_router

    app = FastAPI()
    app.include_router(files_router)
    app.state.chat_repo = _FakeChatRepo(sessions if sessions is not None else {CHAT_ID: TEST_USER["email"]})
    if chat_config is not None:
        app.state.chat_config = chat_config
    app.dependency_overrides[require_chat_access] = lambda: TEST_USER
    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture
def session_dir(data_dir: Path) -> Path:
    """A session dir shaped like WorkdirManager.prepare_session_dir builds it:
    real files in the session dir + a ``.claude`` symlink into the workspace."""
    from app.chat.workdir import _safe_email_dir

    slug = _safe_email_dir(TEST_USER["email"])
    ws = data_dir / "users" / slug / "workspace"
    (ws / ".claude" / "skills" / "sales-proposal").mkdir(parents=True)
    sdir = data_dir / "users" / slug / "sessions" / CHAT_ID
    sdir.mkdir(parents=True)
    (sdir / ".claude").symlink_to(ws / ".claude")
    return sdir


@pytest.fixture
def client(data_dir: Path, session_dir: Path) -> TestClient:
    return TestClient(_make_app(data_dir=data_dir))


# ---------------------------------------------------------------------------
# Ownership / auth
# ---------------------------------------------------------------------------


def test_unknown_session_404(client: TestClient) -> None:
    assert client.get("/api/chat/sessions/chat_nope/files").status_code == 404


def test_foreign_session_404(data_dir: Path, session_dir: Path) -> None:
    app = _make_app(data_dir=data_dir, sessions={CHAT_ID: OTHER_USER["email"]})
    client = TestClient(app)
    assert client.get(f"/api/chat/sessions/{CHAT_ID}/files").status_code == 404
    assert client.get(f"/api/chat/sessions/{CHAT_ID}/files/download", params={"path": "x.txt"}).status_code == 404
    assert client.post(f"/api/chat/sessions/{CHAT_ID}/files/save-artefact", json={"path": "x.txt"}).status_code == 404


def test_unauthenticated_rejected(data_dir: Path, session_dir: Path) -> None:
    from app.api.chat_session_files import router as files_router

    app = FastAPI()
    app.include_router(files_router)
    app.state.chat_repo = _FakeChatRepo({CHAT_ID: TEST_USER["email"]})
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get(f"/api/chat/sessions/{CHAT_ID}/files")
    assert resp.status_code in (401, 403)


def test_restricted_principal_403(data_dir: Path, session_dir: Path) -> None:
    from app.api.chat_session_files import require_chat_access
    from app.api.chat_session_files import router as files_router
    from app.auth.session_principal import SessionPrincipal

    app = FastAPI()
    app.include_router(files_router)
    app.state.chat_repo = _FakeChatRepo({CHAT_ID: TEST_USER["email"]})
    principal = SessionPrincipal(
        session_id=CHAT_ID,
        participant_user_ids=[TEST_USER["id"]],
        participant_emails=[TEST_USER["email"]],
        intersection={},
    )
    app.dependency_overrides[require_chat_access] = lambda: principal
    client = TestClient(app)
    assert client.get(f"/api/chat/sessions/{CHAT_ID}/files").status_code == 403


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_list_files_in_session_dir(client: TestClient, session_dir: Path) -> None:
    (session_dir / "outputs").mkdir()
    (session_dir / "outputs" / "report.docx").write_bytes(b"docx-bytes")
    (session_dir / "chart.svg").write_bytes(b"<svg/>")

    resp = client.get(f"/api/chat/sessions/{CHAT_ID}/files")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    paths = {f["path"] for f in body["files"]}
    assert "outputs/report.docx" in paths
    assert "chart.svg" in paths
    row = next(f for f in body["files"] if f["path"] == "outputs/report.docx")
    assert row["name"] == "report.docx"
    assert row["size_bytes"] == len(b"docx-bytes")
    assert row["modified_at"]


def test_list_includes_workspace_symlinked_files(client: TestClient, session_dir: Path) -> None:
    """Deliverables written through the .claude symlink (the #1611 repro wrote
    into .claude/skills/sales-proposal/) must surface in the listing."""
    target = session_dir.resolve() / ".claude" / "skills" / "sales-proposal" / "Meridian_SOW_draft.docx"
    target.write_bytes(b"sow")
    resp = client.get(f"/api/chat/sessions/{CHAT_ID}/files")
    assert resp.status_code == 200
    paths = {f["path"] for f in resp.json()["files"]}
    assert ".claude/skills/sales-proposal/Meridian_SOW_draft.docx" in paths


def test_list_sorted_by_mtime_desc(client: TestClient, session_dir: Path) -> None:
    old = session_dir / "old.txt"
    new = session_dir / "new.txt"
    old.write_bytes(b"old")
    new.write_bytes(b"new")
    os.utime(old, (1_000_000_000, 1_000_000_000))
    resp = client.get(f"/api/chat/sessions/{CHAT_ID}/files")
    files = resp.json()["files"]
    assert [f["name"] for f in files] == ["new.txt", "old.txt"]


def test_list_skips_noise_dirs_and_escaping_symlinks(client: TestClient, session_dir: Path, data_dir: Path) -> None:
    (session_dir / "__pycache__").mkdir()
    (session_dir / "__pycache__" / "junk.pyc").write_bytes(b"x")
    (session_dir / ".git").mkdir()
    (session_dir / ".git" / "HEAD").write_bytes(b"ref")

    # Symlink escaping both containment bases (points at DATA_DIR/state).
    secret_dir = data_dir / "state"
    secret_dir.mkdir()
    (secret_dir / "system.duckdb").write_bytes(b"top-secret")
    (session_dir / "loot").symlink_to(secret_dir)
    (session_dir / "loot.txt").symlink_to(secret_dir / "system.duckdb")

    resp = client.get(f"/api/chat/sessions/{CHAT_ID}/files")
    paths = {f["path"] for f in resp.json()["files"]}
    assert not any(p.startswith(("__pycache__", ".git")) for p in paths)
    assert not any(p.startswith("loot") for p in paths)


def test_list_empty_when_session_dir_missing(data_dir: Path) -> None:
    """A docker-provider session that never spawned lists empty (host path)."""
    app = _make_app(data_dir=data_dir)
    resp = TestClient(app).get(f"/api/chat/sessions/{CHAT_ID}/files")
    assert resp.status_code == 200
    body = resp.json()
    assert body["files"] == []
    assert body["source"] == "host"
    assert body["supported"] is True


# ---------------------------------------------------------------------------
# Provider gating (kai-agent = engine sandbox; host dir is template noise)
# ---------------------------------------------------------------------------

_KAI_CONFIG = SimpleNamespace(provider="kai-agent", kai_agent_url="http://engine.invalid:3000")


def test_list_under_kai_agent_reports_unsupported_not_workspace_noise(data_dir: Path, session_dir: Path) -> None:
    """Under kai-agent the host session dir exists but holds only workspace
    template symlinks — listing it would surface hundreds of files that are
    not session output. Until the engine proxy answers, the listing must be
    an honest ``supported: false``, never the template noise."""
    noise = session_dir.resolve() / ".claude" / "skills" / "sales-proposal" / "SKILL.md"
    noise.write_bytes(b"template noise")
    app = _make_app(data_dir=data_dir, chat_config=_KAI_CONFIG)
    resp = TestClient(app).get(f"/api/chat/sessions/{CHAT_ID}/files")
    assert resp.status_code == 200
    body = resp.json()
    assert body["files"] == []
    assert body["source"] == "engine"
    assert body["supported"] is False


def test_download_and_save_artefact_404_under_kai_agent_without_engine(data_dir: Path, session_dir: Path) -> None:
    (session_dir / "real.txt").write_bytes(b"host bytes the engine session must not serve")
    app = _make_app(data_dir=data_dir, chat_config=_KAI_CONFIG)
    client = TestClient(app)
    assert client.get(f"/api/chat/sessions/{CHAT_ID}/files/download", params={"path": "real.txt"}).status_code == 404
    assert (
        client.post(f"/api/chat/sessions/{CHAT_ID}/files/save-artefact", json={"path": "real.txt"}).status_code == 404
    )
    # Path validation still runs first — same 400-before-404 ordering as host.
    assert client.get(f"/api/chat/sessions/{CHAT_ID}/files/download", params={"path": "../x"}).status_code == 400


def test_config_double_without_provider_stays_on_host_path(data_dir: Path, session_dir: Path) -> None:
    """A chat_config double lacking ``provider`` (the test_chat_web_deeplink
    harness shape) must resolve to the local host path, mirroring the
    duck-typed-double rule for provider capability flags."""
    (session_dir / "hello.txt").write_bytes(b"hi")
    app = _make_app(data_dir=data_dir, chat_config=SimpleNamespace(enabled=True))
    resp = TestClient(app).get(f"/api/chat/sessions/{CHAT_ID}/files")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "host"
    assert {f["path"] for f in body["files"]} == {"hello.txt"}


def test_files_routes_are_provider_gated_defensively() -> None:
    """Source-text guard (pattern: tests/test_kai_engine_provider.py): every
    route resolves the files source via the defensive config read before any
    host-dir access, so a MagicMock/duck-typed config can never flip a test
    double onto the outbound-HTTP engine branch."""
    src = (Path(__file__).parent.parent / "app" / "api" / "chat_session_files.py").read_text()
    assert "_ENGINE_SANDBOX_PROVIDERS" in src
    assert 'getattr(chat_config, "provider", "")' in src
    for fn in (
        "async def list_session_files",
        "async def download_session_file",
        "async def save_session_file_as_artefact",
    ):
        body = src[src.index(fn) :]
        nxt = body.find("\n@router")
        body = body[: nxt if nxt != -1 else len(body)]
        assert "_files_source" in body, f"{fn} is not provider-gated"


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def test_download_file(client: TestClient, session_dir: Path) -> None:
    (session_dir / "outputs").mkdir()
    (session_dir / "outputs" / "deck.pptx").write_bytes(b"pptx-bytes")
    resp = client.get(
        f"/api/chat/sessions/{CHAT_ID}/files/download",
        params={"path": "outputs/deck.pptx"},
    )
    assert resp.status_code == 200
    assert resp.content == b"pptx-bytes"
    assert resp.headers["x-content-type-options"] == "nosniff"
    cd = resp.headers["content-disposition"]
    assert cd.startswith("attachment")
    assert 'filename="deck.pptx"' in cd


def test_download_workspace_symlinked_file(client: TestClient, session_dir: Path) -> None:
    target = session_dir.resolve() / ".claude" / "skills" / "sales-proposal" / "deck.pptx"
    target.write_bytes(b"via-symlink")
    resp = client.get(
        f"/api/chat/sessions/{CHAT_ID}/files/download",
        params={"path": ".claude/skills/sales-proposal/deck.pptx"},
    )
    assert resp.status_code == 200
    assert resp.content == b"via-symlink"


def test_download_active_content_served_as_octet_stream(client: TestClient, session_dir: Path) -> None:
    """HTML/SVG must not come back with a renderable content type."""
    (session_dir / "page.html").write_bytes(b"<script>alert(1)</script>")
    resp = client.get(f"/api/chat/sessions/{CHAT_ID}/files/download", params={"path": "page.html"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/octet-stream")


@pytest.mark.parametrize(
    "name",
    ["分析報告.docx", "отчёт.pptx", "résumé.pdf", "report 📊.xlsx", "quarterly report.csv"],
)
def test_download_non_ascii_filename(client: TestClient, session_dir: Path, name: str) -> None:
    """A filename the agent chose may contain any code point.

    Starlette encodes header values as latin-1, so interpolating such a name
    straight into ``filename="…"`` raises UnicodeEncodeError while the
    response is built and the download 500s. The header must use the RFC 5987
    extended form whenever the raw name would not survive verbatim (same
    shapes Starlette's own ``FileResponse(filename=…)`` emits).
    """
    from urllib.parse import quote

    (session_dir / name).write_bytes(b"payload")
    resp = client.get(f"/api/chat/sessions/{CHAT_ID}/files/download", params={"path": name})
    assert resp.status_code == 200
    assert resp.content == b"payload"
    cd = resp.headers["content-disposition"]
    assert cd.startswith("attachment")
    assert f"filename*=utf-8''{quote(name)}" in cd
    # Whatever we emit must be latin-1 encodable — that is the ASGI constraint
    # the naive interpolation violated.
    cd.encode("latin-1")


def test_download_ascii_filename_keeps_plain_form(client: TestClient, session_dir: Path) -> None:
    """A plain ASCII name stays in the widely-understood unextended form."""
    (session_dir / "deck.pptx").write_bytes(b"x")
    resp = client.get(f"/api/chat/sessions/{CHAT_ID}/files/download", params={"path": "deck.pptx"})
    assert resp.status_code == 200
    assert resp.headers["content-disposition"] == 'attachment; filename="deck.pptx"'


@pytest.mark.parametrize(
    "bad",
    ["../../../etc/passwd", "/etc/passwd", "..", "a/../../b.txt", "", "a\\..\\b"],
)
def test_download_rejects_traversal(client: TestClient, bad: str) -> None:
    resp = client.get(f"/api/chat/sessions/{CHAT_ID}/files/download", params={"path": bad})
    assert resp.status_code == 400, f"{bad!r} -> {resp.status_code}"


def test_download_escaping_symlink_404(client: TestClient, session_dir: Path, data_dir: Path) -> None:
    secret = data_dir / "state"
    secret.mkdir()
    (secret / "system.duckdb").write_bytes(b"top-secret")
    (session_dir / "steal.txt").symlink_to(secret / "system.duckdb")
    resp = client.get(f"/api/chat/sessions/{CHAT_ID}/files/download", params={"path": "steal.txt"})
    assert resp.status_code == 404


def test_download_missing_file_404(client: TestClient) -> None:
    resp = client.get(f"/api/chat/sessions/{CHAT_ID}/files/download", params={"path": "nope.txt"})
    assert resp.status_code == 404


def test_download_directory_404(client: TestClient, session_dir: Path) -> None:
    (session_dir / "outputs").mkdir()
    resp = client.get(f"/api/chat/sessions/{CHAT_ID}/files/download", params={"path": "outputs"})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Save to Library (artefact)
# ---------------------------------------------------------------------------


def test_save_artefact_creates_single_file_artefact(client: TestClient, session_dir: Path) -> None:
    (session_dir / "outputs").mkdir()
    (session_dir / "outputs" / "notes.md").write_bytes(b"# session notes")
    resp = client.post(
        f"/api/chat/sessions/{CHAT_ID}/files/save-artefact",
        json={"path": "outputs/notes.md"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["artefact_slug"]
    assert body["library_url"] == f"/library/{body['artefact_slug']}"

    from src.repositories import file_corpora_repo

    row = file_corpora_repo().get_by_slug(body["artefact_slug"])
    assert row is not None
    assert row["created_by"] == TEST_USER["id"]


def test_save_artefact_unsupported_extension_415(client: TestClient, session_dir: Path) -> None:
    (session_dir / "blob.xyz").write_bytes(b"???")
    resp = client.post(
        f"/api/chat/sessions/{CHAT_ID}/files/save-artefact",
        json={"path": "blob.xyz"},
    )
    assert resp.status_code == 415


def test_save_artefact_traversal_400_and_missing_404(client: TestClient) -> None:
    assert (
        client.post(
            f"/api/chat/sessions/{CHAT_ID}/files/save-artefact",
            json={"path": "../secret.txt"},
        ).status_code
        == 400
    )
    assert (
        client.post(
            f"/api/chat/sessions/{CHAT_ID}/files/save-artefact",
            json={"path": "missing.md"},
        ).status_code
        == 404
    )
