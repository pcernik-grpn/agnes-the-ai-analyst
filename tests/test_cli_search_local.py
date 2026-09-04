"""`agnes search --local` + stdio-MCP offline fallback (K3, #798)."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
from typer.testing import CliRunner

from cli.commands.search import search_app

runner = CliRunner()

CHUNKS = [
    {
        "id": "ck1",
        "corpus_id": "col_a",
        "file_id": "f1",
        "ordinal": 0,
        "text": "invoices are monthly",
        "embedding": None,
        "section_path": None,
        "page": None,
        "bbox": None,
        "metadata": None,
        "created_at": None,
    },
    {
        "id": "ck2",
        "corpus_id": "col_a",
        "file_id": "f1",
        "ordinal": 1,
        "text": "vacation policy is generous",
        "embedding": None,
        "section_path": None,
        "page": None,
        "bbox": None,
        "metadata": None,
        "created_at": None,
    },
]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Workspace with one artifact built by the REAL packaging builder —
    reused from Task 4's fixture pattern (tests/test_search_local.py)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "srv"))
    from src.knowledge_packaging import artifacts_dir, build_artifact

    with (
        patch(
            "src.knowledge_packaging._list_chunk_batch",
            lambda cid, *, after_id, limit: list(CHUNKS) if after_id is None else [],
        ),
        patch("src.knowledge_packaging._list_files", lambda cid: [{"id": "f1", "filename": "handbook.md"}]),
        patch("src.knowledge_packaging._list_corpora", lambda: [{"id": "col_a", "name": "Handbook"}]),
    ):
        build_artifact("col_a")
    ws = tmp_path / "ws"
    kdir = ws / "user" / "knowledge"
    kdir.mkdir(parents=True)
    (artifacts_dir() / "col_a.duckdb").rename(kdir / "col_a.duckdb")
    return ws


# ── CLI: agnes search --local ───────────────────────────────────────────────


def test_search_local_happy_path(workspace):
    with patch("cli.config.get_workspace_root", return_value=str(workspace)):
        r = runner.invoke(search_app, ["--local", "monthly invoices"])
    assert r.exit_code == 0, r.output
    assert "handbook.md" in r.output


def test_search_local_no_workspace_exits_1(tmp_path):
    with patch("cli.config.get_workspace_root", return_value=None):
        r = runner.invoke(search_app, ["--local", "invoices"])
    assert r.exit_code == 1
    assert "agnes init" in r.output


def test_search_local_prints_offline_scope_warning_and_sources_line(workspace):
    with patch("cli.config.get_workspace_root", return_value=str(workspace)):
        r = runner.invoke(search_app, ["--local", "monthly invoices"])
    assert r.exit_code == 0, r.output
    assert "offline scope: documents only — knowledge + catalog need the server" in r.output
    assert "sources: documents (local)" in r.output


def test_search_scope_local_equivalent_to_flag(workspace):
    with patch("cli.config.get_workspace_root", return_value=str(workspace)):
        r = runner.invoke(search_app, ["--scope", "local", "monthly invoices"])
    assert r.exit_code == 0, r.output
    assert "handbook.md" in r.output
    assert "sources: documents (local)" in r.output


# ── MCP: offline fallback ───────────────────────────────────────────────────


def test_mcp_knowledge_search_falls_back_on_transport_error(workspace):
    pytest.importorskip("mcp", reason="mcp package not installed")
    from cli.mcp import server as mcp_server

    with (
        patch("cli.mcp.server.api_get_json", side_effect=httpx.ConnectError("boom")),
        patch("cli.config.get_workspace_root", return_value=str(workspace)),
    ):
        result = mcp_server.knowledge_search("monthly invoices")
    assert result["source"] == "local"
    assert result["results"] and result["results"][0]["chunk_id"] == "ck1"
    # #898: the offline response labels the LOCAL ranking mode too.
    assert result["retrieval"] in ("hybrid", "lexical_only")


def test_mcp_knowledge_search_does_not_fall_back_on_http_error():
    pytest.importorskip("mcp", reason="mcp package not installed")
    from cli.mcp import server as mcp_server
    from cli.v2_client import V2ClientError

    with patch(
        "cli.mcp.server.api_get_json",
        side_effect=V2ClientError(status_code=500, body={"detail": "boom"}),
    ):
        with pytest.raises(ValueError):
            mcp_server.knowledge_search("invoices")
