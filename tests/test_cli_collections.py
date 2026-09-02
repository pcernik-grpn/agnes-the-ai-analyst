"""Tests for `agnes collections` CLI commands.

All network calls are monkeypatched — no running server required.
Covers: create, list, show, upload (multipart), rm.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from cli.commands.collections import collections_app

runner = CliRunner()

ROOT = Path(__file__).resolve().parents[1]
FOUNDATION_TOOLS_SRC = ROOT / "app" / "api" / "mcp" / "foundation_tools.py"


# ---------------------------------------------------------------------------
# Help smoke test
# ---------------------------------------------------------------------------


def test_collections_help_lists_subcommands():
    r = runner.invoke(collections_app, ["--help"])
    assert r.exit_code == 0, r.output
    for cmd in ("create", "list", "show", "upload", "rm", "rm-file"):
        assert cmd in r.output, f"missing subcommand {cmd!r} in help"


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_create_prints_id(monkeypatch):
    body = {
        "id": "col_abc123",
        "slug": "my-corpus",
        "name": "My Corpus",
        "description": "test",
        "created_by": "admin",
        "created_at": "2026-06-15T00:00:00",
        "updated_at": None,
    }
    with patch("cli.commands.collections.api_post_json", return_value=body):
        r = runner.invoke(collections_app, ["create", "--name", "My Corpus", "--description", "test"])
    assert r.exit_code == 0, r.output
    assert "col_abc123" in r.output
    assert "My Corpus" in r.output


def test_create_json_flag(monkeypatch):
    body = {
        "id": "col_xyz",
        "slug": "s",
        "name": "N",
        "description": None,
        "created_by": "u",
        "created_at": None,
        "updated_at": None,
    }
    with patch("cli.commands.collections.api_post_json", return_value=body):
        r = runner.invoke(collections_app, ["create", "--name", "N", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["id"] == "col_xyz"


def test_create_server_error_exits_nonzero(monkeypatch):
    from cli.v2_client import V2ClientError

    with patch(
        "cli.commands.collections.api_post_json",
        side_effect=V2ClientError(status_code=403, body={"detail": "Forbidden"}),
    ):
        r = runner.invoke(collections_app, ["create", "--name", "X"])
    assert r.exit_code != 0


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_prints_items(monkeypatch):
    body = {
        "items": [
            {
                "id": "col_1",
                "slug": "first",
                "name": "First",
                "description": None,
                "created_by": "u",
                "created_at": None,
                "updated_at": None,
            },
        ]
    }
    with patch("cli.commands.collections.api_get_json", return_value=body):
        r = runner.invoke(collections_app, ["list"])
    assert r.exit_code == 0, r.output
    assert "col_1" in r.output
    assert "First" in r.output


def test_list_json_flag(monkeypatch):
    body = {"items": []}
    with patch("cli.commands.collections.api_get_json", return_value=body):
        r = runner.invoke(collections_app, ["list", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output) == body


def test_list_empty_table(monkeypatch):
    with patch("cli.commands.collections.api_get_json", return_value={"items": []}):
        r = runner.invoke(collections_app, ["list"])
    assert r.exit_code == 0
    assert "no collections" in r.output.lower()


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def test_show_prints_detail(monkeypatch):
    body = {
        "id": "col_1",
        "slug": "first",
        "name": "First",
        "description": "desc",
        "created_by": "u",
        "created_at": None,
        "updated_at": None,
        "files": [
            {
                "file_id": "cf_a",
                "filename": "notes.txt",
                "processing_status": "pending",
                "size_bytes": 11,
                "file_type": "txt",
                "sha256": "abc",
                "corpus_id": "col_1",
                "created_at": None,
                "processing_detail": None,
            }
        ],
    }
    with patch("cli.commands.collections.api_get_json", return_value=body):
        r = runner.invoke(collections_app, ["show", "col_1"])
    assert r.exit_code == 0, r.output
    assert "First" in r.output
    assert "notes.txt" in r.output


def test_show_json_flag(monkeypatch):
    body = {
        "id": "col_1",
        "slug": "s",
        "name": "N",
        "description": None,
        "created_by": "u",
        "created_at": None,
        "updated_at": None,
        "files": [],
    }
    with patch("cli.commands.collections.api_get_json", return_value=body):
        r = runner.invoke(collections_app, ["show", "col_1", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["id"] == "col_1"


def test_show_not_found_exits_nonzero(monkeypatch):
    from cli.v2_client import V2ClientError

    with patch(
        "cli.commands.collections.api_get_json",
        side_effect=V2ClientError(status_code=404, body={"detail": "collection_not_found"}),
    ):
        r = runner.invoke(collections_app, ["show", "col_missing"])
    assert r.exit_code != 0


# ---------------------------------------------------------------------------
# show — pagination + search (files pagination/search, task D)
# ---------------------------------------------------------------------------


def _detail_body(**overrides) -> dict:
    body = {
        "id": "col_1",
        "slug": "s",
        "name": "N",
        "description": None,
        "created_by": "u",
        "created_at": None,
        "updated_at": None,
        "files": [],
    }
    body.update(overrides)
    return body


def test_show_sends_limit_offset_and_q_to_files_endpoint():
    """`--limit`/`--offset`/`--q` are forwarded as query params to the
    dedicated `/files` endpoint, not silently dropped or applied only to the
    (unparameterized) collection-detail call."""
    calls: list[tuple[str, dict]] = []

    def _fake_get(path, **params):
        calls.append((path, params))
        if path.endswith("/files"):
            return {"files": [], "total": 0, "limit": params.get("limit", 25), "offset": params.get("offset", 0)}
        return _detail_body()

    with patch("cli.commands.collections.api_get_json", _fake_get):
        r = runner.invoke(collections_app, ["show", "col_1", "--limit", "10", "--offset", "5", "--q", "invoice"])
    assert r.exit_code == 0, r.output

    files_calls = [c for c in calls if c[0].endswith("/files")]
    assert len(files_calls) == 1
    assert files_calls[0][1] == {"limit": 10, "offset": 5, "q": "invoice"}


def test_show_blank_q_means_no_filter():
    """`--q ""` must be indistinguishable from omitting `--q` entirely — the
    `?corpus_id=` trap this file's neighbour already shipped once."""
    calls: list[tuple[str, dict]] = []

    def _fake_get(path, **params):
        calls.append((path, params))
        if path.endswith("/files"):
            return {"files": [], "total": 0, "limit": 25, "offset": 0}
        return _detail_body()

    with patch("cli.commands.collections.api_get_json", _fake_get):
        r = runner.invoke(collections_app, ["show", "col_1", "--q", ""])
    assert r.exit_code == 0, r.output

    files_calls = [c for c in calls if c[0].endswith("/files")]
    assert "q" not in files_calls[0][1]


def test_show_truncation_footer_states_the_real_total():
    files = [
        {"file_id": f"cf_{i}", "filename": f"f{i}.txt", "processing_status": "pending", "size_bytes": 1}
        for i in range(25)
    ]

    def _fake_get(path, **params):
        if path.endswith("/files"):
            return {"files": files, "total": 1234, "limit": 25, "offset": 0}
        return _detail_body()

    with patch("cli.commands.collections.api_get_json", _fake_get):
        r = runner.invoke(collections_app, ["show", "col_1"])
    assert r.exit_code == 0, r.output
    assert "Showing 25 of 1234 files. Use --offset 25 for the next page." in r.output


def test_show_no_footer_when_the_page_is_not_truncated():
    files = [{"file_id": "cf_1", "filename": "a.txt", "processing_status": "pending", "size_bytes": 1}]

    def _fake_get(path, **params):
        if path.endswith("/files"):
            return {"files": files, "total": 1, "limit": 25, "offset": 0}
        return _detail_body()

    with patch("cli.commands.collections.api_get_json", _fake_get):
        r = runner.invoke(collections_app, ["show", "col_1"])
    assert r.exit_code == 0, r.output
    assert "Showing" not in r.output


def test_show_no_matches_for_q_hints_the_next_step():
    def _fake_get(path, **params):
        if path.endswith("/files"):
            return {"files": [], "total": 0, "limit": 25, "offset": 0}
        return _detail_body()

    with patch("cli.commands.collections.api_get_json", _fake_get):
        r = runner.invoke(collections_app, ["show", "col_1", "--q", "zzz"])
    assert r.exit_code == 0, r.output
    assert "zzz" in r.output
    assert "--q" in r.output


def test_show_json_emits_the_raw_paged_body_including_total():
    """`--json` reflects the ACTUAL page fetched (limit/offset/total as
    returned by `/files`), not the collection-detail call's own default-page
    fields."""

    def _fake_get(path, **params):
        if path.endswith("/files"):
            return {
                "files": [{"file_id": "cf_1", "filename": "a.txt", "processing_status": "pending", "size_bytes": 1}],
                "total": 42,
                "limit": 5,
                "offset": 10,
            }
        return _detail_body()

    with patch("cli.commands.collections.api_get_json", _fake_get):
        r = runner.invoke(collections_app, ["show", "col_1", "--limit", "5", "--offset", "10", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["id"] == "col_1"
    assert data["total"] == 42
    assert data["limit"] == 5
    assert data["offset"] == 10
    assert len(data["files"]) == 1


def test_show_still_prints_the_fixed_width_file_table():
    """A long filename must not break the FILE_ID / STATUS / SIZE columns —
    filename stays the last, unpadded field."""
    long_name = "a" * 200 + ".txt"
    files = [{"file_id": "cf_1", "filename": long_name, "processing_status": "pending", "size_bytes": 11}]

    def _fake_get(path, **params):
        if path.endswith("/files"):
            return {"files": files, "total": 1, "limit": 25, "offset": 0}
        return _detail_body()

    with patch("cli.commands.collections.api_get_json", _fake_get):
        r = runner.invoke(collections_app, ["show", "col_1"])
    assert r.exit_code == 0, r.output
    assert "cf_1" in r.output
    assert "pending" in r.output
    assert long_name in r.output


# ---------------------------------------------------------------------------
# MCP `collection_get` — same pagination/search contract as `collections show`
# ---------------------------------------------------------------------------


class _FakeMcpResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)
        self.reason_phrase = "OK"
        self.request = None

    def json(self) -> dict:
        return self._payload


def _mcp_collection_get(**kwargs):
    """Invoke the registered `collection_get` foundation tool directly,
    with `httpx.AsyncClient.get` faked so no network/server is needed."""
    pytest.importorskip("mcp", reason="mcp package not installed")
    import app.api.mcp_http as mcp_mod

    calls: list[tuple[str, dict]] = []

    async def _fake_get(url, *, headers=None, params=None, timeout=None):
        calls.append((url, params or {}))
        if url.endswith("/files"):
            p = params or {}
            return _FakeMcpResponse(
                {"files": [], "total": 0, "limit": p.get("limit", 25), "offset": p.get("offset", 0)}
            )
        return {
            "id": "col_1",
            "slug": "s",
            "name": "N",
            "description": None,
            "created_by": "u",
            "created_at": None,
            "updated_at": None,
        }

    async def _fake_get_wrapped(url, *, headers=None, params=None, timeout=None):
        result = await _fake_get(url, headers=headers, params=params, timeout=timeout)
        return result if isinstance(result, _FakeMcpResponse) else _FakeMcpResponse(result)

    token = mcp_mod._current_token.set("tok")
    try:
        with patch("httpx.AsyncClient.get", AsyncMock(side_effect=_fake_get_wrapped)):
            result = asyncio.run(mcp_mod.collection_get(**kwargs))
    finally:
        mcp_mod._current_token.reset(token)
    return result, calls


def test_mcp_collection_get_forwards_limit_offset_and_q():
    result, calls = _mcp_collection_get(collection_id="col_1", limit=10, offset=5, q="invoice")

    files_calls = [c for c in calls if c[0].endswith("/files")]
    assert len(files_calls) == 1
    assert files_calls[0][1] == {"limit": 10, "offset": 5, "q": "invoice"}
    assert result["id"] == "col_1"
    assert result["files_total"] == 0
    assert result["files_truncated"] is False


def test_mcp_collection_get_blank_q_means_no_filter():
    _result, calls = _mcp_collection_get(collection_id="col_1", q="")

    files_calls = [c for c in calls if c[0].endswith("/files")]
    assert "q" not in files_calls[0][1]


def test_mcp_collection_get_default_params():
    """Defaults match the shared contract: limit=25, offset=0, q=""."""
    _result, calls = _mcp_collection_get(collection_id="col_1")

    files_calls = [c for c in calls if c[0].endswith("/files")]
    assert files_calls[0][1] == {"limit": 25, "offset": 0}


def test_mcp_collection_get_docstring_documents_pagination_and_search():
    """The docstring IS the API contract for a tool an agent calls blind —
    it must say what `q` matches, that the result is a page, and how to read
    `files_total`/`files_truncated`."""
    src = FOUNDATION_TOOLS_SRC.read_text(encoding="utf-8")
    m = re.search(
        r"(?:async\s+)?def\s+collection_get\s*\(.*?\)\s*->[^:]*:\s*\"\"\"(.*?)\"\"\"",
        src,
        re.S,
    )
    assert m, "collection_get has no docstring in foundation_tools.py"
    doc = m.group(1).lower()
    assert "files_total" in doc, "docstring does not name files_total"
    assert "files_truncated" in doc, "docstring does not name files_truncated"
    assert "substring" in doc, "docstring does not say `q` is a substring match"
    assert "filename" in doc and "path" in doc, "docstring does not say q matches filename/path"
    assert "offset" in doc, "docstring does not explain how to reach the next page"
    assert "collections_search" in doc, "docstring does not distinguish itself from the content search tool"


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------


def test_upload_sends_multipart_per_file(monkeypatch, tmp_path):
    """Each file in the path list is sent as a separate multipart POST."""
    calls: list[dict] = []

    def _fake_post(path, *, files, data=None):
        calls.append({"path": path, "files": files})
        return [{"file_id": "cf_new", "filename": "a.txt", "processing_status": "pending", "size_bytes": 3}]

    f1 = tmp_path / "a.txt"
    f1.write_bytes(b"abc")
    f2 = tmp_path / "b.pdf"
    f2.write_bytes(b"pdf")

    with patch("cli.commands.collections.api_post_multipart", _fake_post):
        r = runner.invoke(collections_app, ["upload", "col_1", str(f1), str(f2)])

    assert r.exit_code == 0, r.output
    # Two separate POST calls (one per file)
    assert len(calls) == 2
    assert calls[0]["path"] == "/api/collections/col_1/files"
    assert "pending" in r.output


def test_upload_with_path_sends_paths_form_field(monkeypatch, tmp_path):
    """`--path` is forwarded as the `paths` multipart form field for upsert."""
    calls: list[dict] = []

    def _fake_post(path, *, files, data=None):
        calls.append({"path": path, "data": data})
        return [{"file_id": "cf_new", "filename": "a.md", "processing_status": "pending", "path": "docs/a.md"}]

    f1 = tmp_path / "a.md"
    f1.write_bytes(b"alpha")

    with patch("cli.commands.collections.api_post_multipart", _fake_post):
        r = runner.invoke(collections_app, ["upload", "col_1", str(f1), "--path", "docs/a.md"])

    assert r.exit_code == 0, r.output
    assert len(calls) == 1
    assert calls[0]["data"] == {"paths": "docs/a.md"}


def test_upload_path_with_multiple_files_errors(tmp_path):
    """`--path` is single-file only — reject an ambiguous multi-file upload."""
    f1 = tmp_path / "a.md"
    f1.write_bytes(b"a")
    f2 = tmp_path / "b.md"
    f2.write_bytes(b"b")

    called: list = []
    with patch("cli.commands.collections.api_post_multipart", lambda *a, **k: called.append(1) or []):
        r = runner.invoke(collections_app, ["upload", "col_1", str(f1), str(f2), "--path", "docs/x.md"])
    assert r.exit_code != 0
    assert not called  # never hit the network


def test_upload_server_error_exits_nonzero(monkeypatch, tmp_path):
    from cli.v2_client import V2ClientError

    f = tmp_path / "bad.dwg"
    f.write_bytes(b"bin")
    with patch(
        "cli.commands.collections.api_post_multipart",
        side_effect=V2ClientError(status_code=422, body=[{"filename": "bad.dwg", "processing_status": "rejected"}]),
    ):
        r = runner.invoke(collections_app, ["upload", "col_1", str(f)])
    assert r.exit_code != 0


def test_upload_missing_file_exits_nonzero(tmp_path):
    """Typer argument validation: path must exist."""
    r = runner.invoke(collections_app, ["upload", "col_1", str(tmp_path / "no_such.txt")])
    assert r.exit_code != 0


# ---------------------------------------------------------------------------
# rm
# ---------------------------------------------------------------------------


def test_rm_with_yes_flag(monkeypatch):
    called: list[str] = []

    def _fake_delete(path):
        called.append(path)
        return {}

    with patch("cli.commands.collections.api_delete", _fake_delete):
        r = runner.invoke(collections_app, ["rm", "col_1", "--yes"])
    assert r.exit_code == 0, r.output
    assert called == ["/api/collections/col_1"]
    assert "deleted" in r.output.lower()


def test_rm_prompts_without_yes(monkeypatch):
    """Without --yes the command should ask for confirmation."""
    called: list[str] = []

    with patch("cli.commands.collections.api_delete", lambda p: called.append(p) or {}):
        # Simulate user answering "n" to the confirmation prompt
        runner.invoke(collections_app, ["rm", "col_1"], input="n\n")
    # User said no — nothing deleted
    assert not called


def test_rm_server_error_exits_nonzero(monkeypatch):
    from cli.v2_client import V2ClientError

    with patch(
        "cli.commands.collections.api_delete",
        side_effect=V2ClientError(status_code=404, body={"detail": "collection_not_found"}),
    ):
        r = runner.invoke(collections_app, ["rm", "col_missing", "--yes"])
    assert r.exit_code != 0


# ---------------------------------------------------------------------------
# rm-file
# ---------------------------------------------------------------------------


def test_rm_file_with_yes_flag(monkeypatch):
    called: list[str] = []

    def _fake_delete(path):
        called.append(path)
        return {}

    with patch("cli.commands.collections.api_delete", _fake_delete):
        r = runner.invoke(collections_app, ["rm-file", "col_1", "cf_1", "--yes"])
    assert r.exit_code == 0, r.output
    assert called == ["/api/collections/col_1/files/cf_1"]
    assert "deleted" in r.output.lower()


def test_rm_file_prompts_without_yes(monkeypatch):
    """Without --yes the command should ask for confirmation."""
    called: list[str] = []

    with patch("cli.commands.collections.api_delete", lambda p: called.append(p) or {}):
        runner.invoke(collections_app, ["rm-file", "col_1", "cf_1"], input="n\n")
    # User said no — nothing deleted
    assert not called


def test_rm_file_server_error_exits_nonzero(monkeypatch):
    from cli.v2_client import V2ClientError

    with patch(
        "cli.commands.collections.api_delete",
        side_effect=V2ClientError(status_code=404, body={"detail": "file_not_found"}),
    ):
        r = runner.invoke(collections_app, ["rm-file", "col_1", "cf_missing", "--yes"])
    assert r.exit_code != 0


# ---------------------------------------------------------------------------
# reingest
# ---------------------------------------------------------------------------


def test_collections_reingest_posts_to_endpoint(monkeypatch):
    calls = {}

    def fake_post(path, payload):
        calls["path"] = path
        return {"file_id": "cf_1", "processing_status": "pending"}

    with patch("cli.commands.collections.api_post_json", fake_post):
        r = runner.invoke(collections_app, ["reingest", "col_1", "cf_1"])
    assert r.exit_code == 0, r.output
    assert calls["path"] == "/api/collections/col_1/files/cf_1/reingest"
    assert "pending" in r.output


def test_collections_reingest_server_error_exits_nonzero(monkeypatch):
    from cli.v2_client import V2ClientError

    with patch(
        "cli.commands.collections.api_post_json",
        side_effect=V2ClientError(status_code=409, body={"detail": "reingest_in_progress"}),
    ):
        r = runner.invoke(collections_app, ["reingest", "col_1", "cf_1"])
    assert r.exit_code != 0


# ---------------------------------------------------------------------------
# Registration check — `agnes collections` must exist in the top-level app
# ---------------------------------------------------------------------------


def test_collections_registered_in_main():
    """The `collections` sub-app is registered in cli/main.py."""
    from cli.main import app

    group_names = {g.name for g in app.registered_groups if g.name}
    assert "collections" in group_names, (
        "collections_app not registered in cli/main.py — add `app.add_typer(collections_app, name='collections')`"
    )
