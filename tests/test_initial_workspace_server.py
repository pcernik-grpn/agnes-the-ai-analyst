import io
import zipfile
from pathlib import Path

import pytest

from src.initial_workspace import (
    extract_zip_to_workspace,
    initialize_default_workspace,
    initialize_workspace_from_template,
    is_override_workspace,
    read_sentinel_server_url,
    write_sentinel,
)


def _make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, payload in entries.items():
            zf.writestr(name, payload)
    return buf.getvalue()


def test_extract_zip_creates_files(tmp_path: Path):
    zip_bytes = _make_zip({"CLAUDE.md": b"hi", ".claude/settings.json": b"{}"})
    result = extract_zip_to_workspace(zip_bytes, tmp_path)
    assert (tmp_path / "CLAUDE.md").read_bytes() == b"hi"
    assert (tmp_path / ".claude/settings.json").read_bytes() == b"{}"
    assert sorted(result.created) == [".claude/settings.json", "CLAUDE.md"]
    assert result.overwritten == []


def test_extract_zip_rejects_traversal(tmp_path: Path):
    zip_bytes = _make_zip({"../escape.txt": b"x"})
    with pytest.raises(ValueError, match="unsafe"):
        extract_zip_to_workspace(zip_bytes, tmp_path)


def test_write_sentinel_records_metadata(tmp_path: Path):
    write_sentinel(
        tmp_path,
        agnes_version="0.55.0",
        server_url="https://agnes.example.com",
        template_source="https://github.com/example/tpl",
        template_sha="abc123",
        override=True,
    )
    sentinel = (tmp_path / ".claude" / "init-complete").read_text()
    assert "agnes_version: 0.55.0" in sentinel
    assert "override: true" in sentinel
    assert "template_sha: abc123" in sentinel
    assert is_override_workspace(tmp_path) is True


def test_is_override_workspace_false_when_missing(tmp_path: Path):
    assert is_override_workspace(tmp_path) is False


def test_read_sentinel_server_url_roundtrip(tmp_path: Path):
    write_sentinel(
        tmp_path,
        agnes_version="0.55.0",
        server_url="https://agnes.example.com",
        template_source=None,
        template_sha=None,
        override=False,
    )
    assert read_sentinel_server_url(tmp_path) == "https://agnes.example.com"


def test_read_sentinel_server_url_none_when_missing(tmp_path: Path):
    assert read_sentinel_server_url(tmp_path) is None


def test_read_sentinel_server_url_none_without_line(tmp_path: Path):
    sentinel = tmp_path / ".claude" / "init-complete"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("completed_at: 2026-01-01T00:00:00+00:00\nagnes_version: 0.55.0\n")
    assert read_sentinel_server_url(tmp_path) is None


def test_read_sentinel_server_url_tolerates_whitespace(tmp_path: Path):
    sentinel = tmp_path / ".claude" / "init-complete"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("server_url :   http://192.0.2.1:8000  \n")
    assert read_sentinel_server_url(tmp_path) == "http://192.0.2.1:8000"


def test_initialize_workspace_from_template_writes_files_and_sentinel(tmp_path: Path):
    zip_bytes = _make_zip({"CLAUDE.md": b"hello"})
    result = initialize_workspace_from_template(
        tmp_path,
        zip_bytes,
        agnes_version="0.55.0",
        server_url="https://example",
        template_source="src",
        template_sha="sha",
    )
    assert (tmp_path / "CLAUDE.md").read_text() == "hello"
    assert is_override_workspace(tmp_path) is True
    assert result.created == ["CLAUDE.md"]


def test_initialize_default_workspace_copies_bundled(tmp_path: Path):
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "CLAUDE.md").write_text("default")
    (bundled / ".claude").mkdir()
    (bundled / ".claude" / "settings.json").write_text("{}")
    workspace = tmp_path / "ws"
    initialize_default_workspace(
        workspace,
        agnes_version="0.55.0",
        server_url="https://example",
        bundled_template_dir=bundled,
    )
    assert (workspace / "CLAUDE.md").read_text() == "default"
    assert (workspace / ".claude/settings.json").read_text() == "{}"
    assert (workspace / ".claude/init-complete").exists()
    # default init writes override=false
    assert "override: false" in (workspace / ".claude/init-complete").read_text()
