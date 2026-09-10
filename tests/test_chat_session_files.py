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

    def __init__(self, sessions: dict[str, str], sandbox_ids: dict[str, str | None] | None = None):
        # chat_id -> owner email
        self._sessions = sessions
        # chat_id -> sandbox_id override. Chats not listed here default to
        # an already-has-a-sandbox placeholder, so the many pre-existing
        # engine tests below keep exercising the real engine round trip they
        # were written against; only the no-sandbox-yet tests opt out with
        # an explicit ``None``.
        self._sandbox_ids = sandbox_ids or {}

    def get_session(self, chat_id: str):
        email = self._sessions.get(chat_id)
        if email is None:
            return None
        sandbox_id = self._sandbox_ids.get(chat_id, f"kai-engine:{chat_id}")
        return SimpleNamespace(id=chat_id, user_email=email, sandbox_id=sandbox_id)


#: The host-path harness default. Explicit on purpose: the instance default
#: provider is ``kai-agent``, and _chat_config_for_delivery's fallback would
#: load exactly that — flipping every host-walk test onto the engine branch.
_DOCKER_CONFIG = SimpleNamespace(provider="docker")


def _make_app(
    *,
    data_dir: Path,
    sessions: dict[str, str] | None = None,
    sandbox_ids: dict[str, str | None] | None = None,
    chat_config: object | None = _DOCKER_CONFIG,
) -> FastAPI:
    os.environ["DATA_DIR"] = str(data_dir)

    from app.api.chat_session_files import require_chat_access
    from app.api.chat_session_files import router as files_router

    app = FastAPI()
    app.include_router(files_router)
    app.state.chat_repo = _FakeChatRepo(
        sessions if sessions is not None else {CHAT_ID: TEST_USER["email"]}, sandbox_ids
    )
    if chat_config is not None:
        app.state.chat_config = chat_config
    app.dependency_overrides[require_chat_access] = lambda: TEST_USER
    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_module_level_engine_state():
    """`_ENGINE_LISTING_FAILURE_LOGGED` is process-scoped by design — which
    chat has already logged a traceback outlives a single request. That makes
    it leak between tests: one case exercising a failing listing would
    otherwise decide what a later, unrelated case sees. Cleared per test so
    each states its own premise.

    (There used to be a second memo here — an engine capability cache keyed
    by base URL — removed because it could be poisoned by one chat's
    session-specific failure and then mislead every other chat on the same
    engine; see the block comment above `_ENGINE_LISTING_FAILURE_LOGGED` in
    `chat_session_files.py`.)
    """
    from app.api import chat_session_files as mod

    mod._ENGINE_LISTING_FAILURE_LOGGED.clear()
    yield
    mod._ENGINE_LISTING_FAILURE_LOGGED.clear()


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


def test_list_excludes_the_workspace_template(client: TestClient, session_dir: Path) -> None:
    """The template trees symlinked into EVERY session dir are not session
    output. Listing them buried the real deliverable under the operator's
    bundled skills and scaffolds — observed live, dozens of
    `scaffolds/nodejs-dashboard/...` rows above the file the user asked for.

    (This reverses the original #1611 reading, which followed `.claude/`
    because the reported skill wrote there. The engine's own sandbox browser
    filters dot-directories anyway, so such a file was never reachable on that
    surface; the workspace prompt now directs deliverables to `outputs/`.)"""
    ws = session_dir.resolve().parent.parent / "workspace"
    (ws / ".claude" / "skills" / "sales-proposal").mkdir(parents=True, exist_ok=True)
    (ws / ".claude" / "skills" / "sales-proposal" / "SKILL.md").write_bytes(b"template")
    (ws / "scaffolds" / "nodejs-dashboard" / "src").mkdir(parents=True, exist_ok=True)
    (ws / "scaffolds" / "nodejs-dashboard" / "src" / "App.tsx").write_bytes(b"template")
    (ws / "CLAUDE.md").write_bytes(b"template")
    for entry in (".claude", "scaffolds", "CLAUDE.md"):
        link = session_dir / entry
        if not link.exists():
            link.symlink_to(ws / entry)

    (session_dir / "outputs").mkdir(exist_ok=True)
    (session_dir / "outputs" / "report.docx").write_bytes(b"real deliverable")

    resp = client.get(f"/api/chat/sessions/{CHAT_ID}/files")
    assert resp.status_code == 200
    paths = {f["path"] for f in resp.json()["files"]}
    assert "outputs/report.docx" in paths
    assert not any(p.startswith((".claude/", "scaffolds/")) or p == "CLAUDE.md" for p in paths), (
        f"workspace-template entries leaked into the listing: {sorted(paths)}"
    )


def test_outputs_sort_ahead_of_other_session_files(client: TestClient, session_dir: Path) -> None:
    """`outputs/` is where the prompt tells the agent to leave deliverables, so
    it leads the list even when incidental scratch is newer."""
    (session_dir / "outputs").mkdir(exist_ok=True)
    deliverable = session_dir / "outputs" / "report.docx"
    scratch = session_dir / "scratch.txt"
    deliverable.write_bytes(b"deliverable")
    scratch.write_bytes(b"scratch")
    os.utime(deliverable, (1_000_000_000, 1_000_000_000))  # deliberately OLDER

    files = client.get(f"/api/chat/sessions/{CHAT_ID}/files").json()["files"]
    assert [f["path"] for f in files][0] == "outputs/report.docx"


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


def _engine_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the engine transport at a 404-everything engine (an engine
    build without the sandbox-files routes / an unknown chat) and stub the
    JWT mint. The full proxy behavior matrix lives in
    tests/test_chat_kai_engine_files.py."""
    import httpx

    import app.api.chat_session_files as mod
    from app.api import kai

    monkeypatch.setattr(mod, "_ENGINE_TRANSPORT", httpx.MockTransport(lambda request: httpx.Response(404)))
    monkeypatch.setattr(kai, "mint_engine_session_token", lambda email, chat_id: ("jwt", 0))


def test_list_under_kai_agent_reports_unsupported_not_workspace_noise(
    data_dir: Path, session_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under kai-agent the host session dir exists but holds only workspace
    template symlinks — listing it would surface hundreds of files that are
    not session output. When the engine has no files channel, the listing
    must be an honest ``supported: false``, never the template noise."""
    _engine_gone(monkeypatch)
    noise = session_dir.resolve() / ".claude" / "skills" / "sales-proposal" / "SKILL.md"
    noise.write_bytes(b"template noise")
    app = _make_app(data_dir=data_dir, chat_config=_KAI_CONFIG)
    resp = TestClient(app).get(f"/api/chat/sessions/{CHAT_ID}/files")
    assert resp.status_code == 200
    body = resp.json()
    assert body["files"] == []
    assert body["source"] == "engine"
    assert body["supported"] is False


def test_list_under_kai_agent_skips_the_engine_when_no_sandbox_exists_yet(
    data_dir: Path, session_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session with no ``sandbox_id`` — a brand-new chat, or one whose
    sandbox was torn down — must never be asked about: the panel polls this
    route the instant a conversation opens, well before the first turn can
    mint a sandbox, and the engine cannot possibly know a chat id it has
    never seen. Proven with a transport that would hand back a real file if
    it were ever reached — the gate must mean the listing never gets there,
    not merely that this particular engine happens to 404."""
    import httpx

    import app.api.chat_session_files as mod
    from app.api import kai

    def _must_not_be_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError("engine must not be called before a sandbox exists")

    monkeypatch.setattr(mod, "_ENGINE_TRANSPORT", httpx.MockTransport(_must_not_be_called))
    monkeypatch.setattr(kai, "mint_engine_session_token", lambda email, chat_id: ("jwt", 0))
    app = _make_app(data_dir=data_dir, chat_config=_KAI_CONFIG, sandbox_ids={CHAT_ID: None})
    resp = TestClient(app).get(f"/api/chat/sessions/{CHAT_ID}/files")
    assert resp.status_code == 200
    body = resp.json()
    assert body["files"] == []
    assert body["source"] == "engine"
    # Supported, not unsupported: the drawer renders supported=False as an
    # "upgrade your engine" warning, the wrong sentence for a chat the
    # reader opened seconds ago. Nothing here says the channel is absent —
    # and nothing here EVER could, since this chat has no sandbox to test
    # with. `True` is not a claim that the channel is proven; it is the
    # honest absence of a claim that it is not — see
    # test_a_legacy_chats_engine_failure_does_not_poison_a_later_sandboxless_chat
    # below for why this can never be downgraded by another chat's failure.
    assert body["supported"] is True


def test_list_under_kai_agent_with_a_sandbox_still_reaches_the_engine(
    data_dir: Path, session_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-sandbox-yet gate must not swallow a session that already has
    one — its listing still reaches the engine, proven by an engine that
    hands back a real file the response must actually surface."""
    import httpx

    import app.api.chat_session_files as mod
    from app.api import kai

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"entries": [{"name": "report.txt", "path": "report.txt", "type": "file", "size": 5}]},
        )

    monkeypatch.setattr(mod, "_ENGINE_TRANSPORT", httpx.MockTransport(_handler))
    monkeypatch.setattr(kai, "mint_engine_session_token", lambda email, chat_id: ("jwt", 0))
    app = _make_app(data_dir=data_dir, chat_config=_KAI_CONFIG, sandbox_ids={CHAT_ID: f"kai-engine:{CHAT_ID}"})
    resp = TestClient(app).get(f"/api/chat/sessions/{CHAT_ID}/files")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "engine"
    assert body["supported"] is True
    assert {f["path"] for f in body["files"]} == {"report.txt"}


def test_download_and_save_artefact_404_under_kai_agent_without_engine(
    data_dir: Path, session_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Host files must be unreachable through an engine-backed session even
    when they exist on disk — the engine (which 404s here) is the only
    source of truth for what the session produced."""
    _engine_gone(monkeypatch)
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
# Save to Library (artifact)
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


# ---------------------------------------------------------------------------
# Preview (#1611 follow-up) — "did it build the deck I asked for?"
#
# The complaint that motivated these: asked for a PowerPoint, the agent built
# one and the drawer could say nothing about it but its name and byte count.
# A .pptx has no browser renderer, so the server sends the deck's words.
# ---------------------------------------------------------------------------


def _pptx_bytes(*slides: tuple[str, list[str]]) -> bytes:
    """A minimal but structurally real .pptx — one part per slide, DrawingML
    paragraphs and text runs, deliberately numbered out of archive order so a
    test proves the deck is returned in SLIDE order and not zip order."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for number, (title, lines) in reversed(list(enumerate(slides, start=1))):
            paragraphs = "".join(f"<a:p><a:r><a:t>{text}</a:t></a:r></a:p>" for text in [title, *lines] if text)
            zf.writestr(
                f"ppt/slides/slide{number}.xml",
                '<?xml version="1.0"?><p:sld xmlns:p="p" xmlns:a="a"><p:cSld><p:spTree>'
                f"<p:sp><p:txBody>{paragraphs}</p:txBody></p:sp></p:spTree></p:cSld></p:sld>",
            )
        zf.writestr("[Content_Types].xml", "<Types/>")
    return buf.getvalue()


def _preview(client: TestClient, path: str):
    return client.get(f"/api/chat/sessions/{CHAT_ID}/files/preview", params={"path": path})


def test_preview_reads_a_pptx_slide_by_slide(client: TestClient, session_dir: Path) -> None:
    """The headline case. The reader gets the deck's contents without a
    download and without PowerPoint."""
    (session_dir / "outputs").mkdir()
    (session_dir / "outputs" / "engagement_type_breakdown.pptx").write_bytes(
        _pptx_bytes(
            ("Engagement Type Breakdown", ["Current Portfolio Overview"]),
            ("Full Portfolio", ["All 30 engagement types", "Two-column table"]),
        )
    )

    body = _preview(client, "outputs/engagement_type_breakdown.pptx").json()
    assert body["kind"] == "slides"
    assert body["source"] == "extracted"
    assert body["truncated"] is False
    assert [s["index"] for s in body["slides"]] == [1, 2]
    assert body["slides"][0]["title"] == "Engagement Type Breakdown"
    assert body["slides"][0]["lines"] == ["Current Portfolio Overview"]
    assert body["slides"][1]["lines"] == ["All 30 engagement types", "Two-column table"]


def test_preview_of_a_pptx_that_is_not_really_a_pptx_is_none_not_500(client: TestClient, session_dir: Path) -> None:
    """An agent picks the filename, so the extension is a claim, not a fact.
    A wrong one degrades to the row's own download affordance."""
    (session_dir / "broken.pptx").write_bytes(b"this is not a zip archive")

    body = _preview(client, "broken.pptx").json()
    assert body["kind"] == "none"
    assert "download" in body["reason"].lower()


def test_preview_of_a_textual_file_returns_its_own_bytes(client: TestClient, session_dir: Path) -> None:
    (session_dir / "notes.md").write_text("# Q3\n\nRevenue up 12%.")

    body = _preview(client, "notes.md").json()
    assert body["kind"] == "text"
    assert body["source"] == "file"
    assert "Revenue up 12%." in body["text"]


def test_preview_truncates_a_long_textual_file_and_says_so(client: TestClient, session_dir: Path) -> None:
    from app.api.chat_session_files import _PREVIEW_MAX_CHARS

    (session_dir / "big.csv").write_text("x" * (_PREVIEW_MAX_CHARS + 500))

    body = _preview(client, "big.csv").json()
    assert body["truncated"] is True
    assert len(body["text"]) == _PREVIEW_MAX_CHARS


def test_preview_points_an_image_at_the_raw_viewer_without_reading_it(client: TestClient, session_dir: Path) -> None:
    """The browser fetches these bytes itself — the preview endpoint must not
    slurp a 20 MB PNG into memory just to say "it's an image"."""
    (session_dir / "chart.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

    body = _preview(client, "chart.png").json()
    assert body["kind"] == "image"
    assert body["raw_url"] == f"/api/chat/sessions/{CHAT_ID}/files/raw?path=chart.png"
    assert body["text"] is None


def _xlsx_bytes(*sheets: tuple[str, list[list[str]]]) -> bytes:
    """A minimal but structurally real .xlsx — a workbook part naming the tabs
    in order, its relationship part, and one worksheet part each, every value
    written as an inline string so the fixture needs no shared-string table.
    Parts are written in REVERSE so a test proves the workbook's tab order
    wins over the archive's."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        tabs = "".join(
            f'<sheet name="{name}" sheetId="{n}" r:id="rId{n}"/>' for n, (name, _) in enumerate(sheets, start=1)
        )
        zf.writestr(
            "xl/workbook.xml",
            f'<?xml version="1.0"?><workbook xmlns:r="r"><sheets>{tabs}</sheets></workbook>',
        )
        rels = "".join(
            f'<Relationship Id="rId{n}" Type="t" Target="worksheets/sheet{n}.xml"/>' for n in range(1, len(sheets) + 1)
        )
        zf.writestr("xl/_rels/workbook.xml.rels", f'<?xml version="1.0"?><Relationships>{rels}</Relationships>')
        for number, (_, rows) in reversed(list(enumerate(sheets, start=1))):
            body = ""
            for r, cells in enumerate(rows, start=1):
                cs = "".join(
                    f'<c r="{chr(65 + c)}{r}" t="inlineStr"><is><t>{value}</t></is></c>'
                    for c, value in enumerate(cells)
                    if value
                )
                body += f'<row r="{r}">{cs}</row>'
            zf.writestr(
                f"xl/worksheets/sheet{number}.xml",
                f'<?xml version="1.0"?><worksheet xmlns="x"><sheetData>{body}</sheetData></worksheet>',
            )
        zf.writestr("[Content_Types].xml", "<Types/>")
    return buf.getvalue()


def test_preview_reads_an_xlsx_sheet_by_sheet(client: TestClient, session_dir: Path) -> None:
    """The workbook half of the same complaint (#1975): the prompt tells an
    agent an `.xlsx` is a deliverable, so a spreadsheet it built must be
    glanceable in the drawer rather than answering "no preview for '.xlsx'"."""
    (session_dir / "outputs").mkdir()
    (session_dir / "outputs" / "q3-revenue.xlsx").write_bytes(
        _xlsx_bytes(
            ("Q3 Revenue", [["Region", "Revenue"], ["EMEA", "1250000"], ["AMER", "980000"]]),
            ("Notes", [["draft"]]),
        )
    )

    body = _preview(client, "outputs/q3-revenue.xlsx").json()
    assert body["kind"] == "sheets"
    assert body["source"] == "extracted"
    assert body["truncated"] is False
    assert [s["name"] for s in body["sheets"]] == ["Q3 Revenue", "Notes"]
    assert body["sheets"][0]["rows"] == [
        ["Region", "Revenue"],
        ["EMEA", "1250000"],
        ["AMER", "980000"],
    ]
    assert body["sheets"][0]["truncated"] is False
    assert body["slides"] is None


def test_preview_of_an_xlsx_that_is_not_really_an_xlsx_is_none_not_500(client: TestClient, session_dir: Path) -> None:
    """Same posture as the deck: the extension is the agent's claim about the
    bytes, and a wrong one degrades to the row's own download affordance."""
    (session_dir / "broken.xlsx").write_bytes(b"this is not a zip archive")

    body = _preview(client, "broken.xlsx").json()
    assert body["kind"] == "none"
    assert "download" in body["reason"].lower()


def test_preview_of_an_unpreviewable_format_says_download_it(client: TestClient, session_dir: Path) -> None:
    (session_dir / "data.parquet").write_bytes(b"PAR1")

    body = _preview(client, "data.parquet").json()
    assert body["kind"] == "none"
    assert ".parquet" in body["reason"]


def test_preview_refuses_the_same_paths_the_download_route_does(client: TestClient) -> None:
    assert _preview(client, "../secret.txt").status_code == 400
    assert _preview(client, "/etc/passwd").status_code == 400
    assert _preview(client, "missing.pptx").status_code == 404


def test_preview_404s_for_a_session_the_caller_does_not_own(data_dir: Path, session_dir: Path) -> None:
    app = _make_app(data_dir=data_dir, sessions={CHAT_ID: OTHER_USER["email"]})
    assert (
        TestClient(app).get(f"/api/chat/sessions/{CHAT_ID}/files/preview", params={"path": "notes.md"}).status_code
        == 404
    )


def test_preview_declines_a_file_too_large_to_glance_at(client: TestClient, session_dir: Path, monkeypatch) -> None:
    import app.api.chat_session_files as mod

    monkeypatch.setattr(mod, "_PREVIEW_MAX_BYTES", 16)
    (session_dir / "huge.md").write_text("x" * 64)

    body = _preview(client, "huge.md").json()
    assert body["kind"] == "none"
    assert "download" in body["reason"].lower()


# ---------------------------------------------------------------------------
# Raw viewer — the ONE route in this module that does not force an attachment
# ---------------------------------------------------------------------------


def _raw(client: TestClient, path: str):
    return client.get(f"/api/chat/sessions/{CHAT_ID}/files/raw", params={"path": path})


def test_raw_serves_an_allowlisted_image_inline_and_framable_by_us(client: TestClient, session_dir: Path) -> None:
    (session_dir / "chart.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    resp = _raw(client, "chart.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["content-disposition"].startswith("inline")
    assert resp.headers["x-content-type-options"] == "nosniff"
    # The app-wide default is DENY/'none'; the modal's iframe needs SELF.
    assert resp.headers["x-frame-options"] == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in resp.headers["content-security-policy"]


def test_raw_refuses_active_content_outright(client: TestClient, session_dir: Path) -> None:
    """The whole reason this route can serve inline at all is that its media
    map is closed. An agent-authored .html or .svg served inline from our own
    origin is stored XSS against the person reading the drawer — so there is
    no inline route for them at all, and `…/preview` shows them as source."""
    (session_dir / "evil.html").write_text("<script>alert(1)</script>")
    (session_dir / "evil.svg").write_text('<svg onload="alert(1)"/>')

    for name in ("evil.html", "evil.svg"):
        assert _raw(client, name).status_code == 415

    body = _preview(client, "evil.html").json()
    assert body["kind"] == "text"
    assert body["raw_url"] is None
    assert "<script>" in body["text"]


def test_raw_refuses_a_pptx_rather_than_streaming_it_inline(client: TestClient, session_dir: Path) -> None:
    (session_dir / "deck.pptx").write_bytes(_pptx_bytes(("Title", [])))
    assert _raw(client, "deck.pptx").status_code == 415


def test_raw_enforces_containment_and_ownership(data_dir: Path, session_dir: Path, client: TestClient) -> None:
    assert _raw(client, "../secret.png").status_code == 400
    assert _raw(client, "missing.png").status_code == 404

    other = TestClient(_make_app(data_dir=data_dir, sessions={CHAT_ID: OTHER_USER["email"]}))
    assert other.get(f"/api/chat/sessions/{CHAT_ID}/files/raw", params={"path": "chart.png"}).status_code == 404


def test_a_legacy_chats_engine_failure_does_not_poison_a_later_sandboxless_chat(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug a prior version of this fix had: a legacy ``chat_<hex>``
    session (minted before the instance's provider switched to
    ``kai-agent``) carries a ``sandbox_id`` too — ``KaiEngineProvider.
    _handle`` mints a (dead) handle for a malformed legacy id, and the
    manager persists every spawned handle before the engine ever creates a
    chat. So this chat's listing DOES reach the engine and DOES 400 at the
    root — evidence about that one chat's id, never about the engine. A
    second, unrelated chat on the SAME engine that has not spawned a sandbox
    yet must report the same optimistic ``supported=True`` it always would,
    regardless of what the legacy chat's own request just answered — there
    must be no shared, poisonable state between them."""
    import httpx

    from app.api import chat_session_files as mod
    from app.api import kai

    legacy_chat_id = "chat_deadbeef00"
    fresh_chat_id = "0b8a1c9e-7d2f-4e5a-9c3b-1f2e3d4c5b6a"

    def _handler(request: httpx.Request) -> httpx.Response:
        # The engine's real behavior for a malformed legacy id: 400 at the
        # listing root, well-formed error body and all.
        return httpx.Response(400, json={"error": {"type": "KaiError", "message": "invalid chat id"}})

    monkeypatch.setattr(mod, "_ENGINE_TRANSPORT", httpx.MockTransport(_handler))
    monkeypatch.setattr(kai, "mint_engine_session_token", lambda email, chat_id: ("jwt", 0))
    app = _make_app(
        data_dir=data_dir,
        chat_config=_KAI_CONFIG,
        sessions={legacy_chat_id: TEST_USER["email"], fresh_chat_id: TEST_USER["email"]},
        sandbox_ids={legacy_chat_id: f"kai-engine:{legacy_chat_id}", fresh_chat_id: None},
    )
    client = TestClient(app)

    legacy = client.get(f"/api/chat/sessions/{legacy_chat_id}/files").json()
    assert legacy["supported"] is False  # honest about THIS chat's own failed listing

    fresh = client.get(f"/api/chat/sessions/{fresh_chat_id}/files").json()
    assert fresh["supported"] is True  # unaffected by the legacy chat's failure


def test_traceback_suppression_lifts_after_a_recovery(monkeypatch):
    """fail → success → fail must log the stack twice. Without the reset the
    first outage silenced every later one for the life of the process, and
    nothing pinned that, so the suppression could quietly become permanent
    again."""
    from app.api import chat_session_files as mod

    monkeypatch.setattr(mod, "_ENGINE_LISTING_FAILURE_LOGGED", type(mod._ENGINE_LISTING_FAILURE_LOGGED)())
    chat = "11111111-1111-1111-1111-111111111111"
    assert mod._first_engine_listing_failure(chat) is True
    assert mod._first_engine_listing_failure(chat) is False
    mod._engine_listing_recovered(chat)
    assert mod._first_engine_listing_failure(chat) is True
