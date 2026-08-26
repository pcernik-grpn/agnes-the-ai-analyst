"""Tests for `agnes update-workspace` — safe backup-aware IWT re-apply.

Layers:
  * src.initial_workspace — pure 3-way diff engine
    (classify_workspace_update / update_workspace_from_template)
  * cli.lib.initial_workspace — baseline storage (in the client config dir,
    NOT the workspace) + apply_override writing the baseline on init
  * cli.commands.update_workspace — the Typer command (IWT guard, dry-run,
    confirm/--yes, report) end-to-end with mocked endpoints

Notes:
  * Workspace files are written with ``write_bytes`` (never ``write_text``)
    — on Windows text mode rewrites ``\\n`` to ``\\r\\n``, which would
    diverge from the LF bytes the template zip carries and make "unchanged"
    files look modified.
  * Every test that touches the baseline sets ``AGNES_CONFIG_DIR`` so the
    baseline lands under a tmp dir, never the real ``~/.config/agnes``.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _w(path: Path, data: bytes) -> None:
    """Write exact bytes (no newline translation)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _isolate_config(monkeypatch, tmp_path):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))


# ===========================================================================
# Layer 1: baseline storage (cli.lib) — lives in config dir, keyed by path
# ===========================================================================


def test_baseline_roundtrip(tmp_path, monkeypatch):
    _isolate_config(monkeypatch, tmp_path)
    from cli.config import _config_dir
    from cli.lib.initial_workspace import (
        _baseline_path,
        load_template_baseline,
        save_template_baseline,
    )

    ws = tmp_path / "ws"
    ws.mkdir()
    assert load_template_baseline(ws) is None
    data = _make_zip({"CLAUDE.md": b"x\n"})
    path = save_template_baseline(ws, data)
    # Stored in the config dir, NOT inside the workspace.
    assert path == _baseline_path(ws)
    assert _config_dir() in path.parents
    assert ws not in path.parents
    assert load_template_baseline(ws) == data


def test_baseline_keyed_per_workspace(tmp_path, monkeypatch):
    """Two workspaces on one machine get distinct baseline files."""
    _isolate_config(monkeypatch, tmp_path)
    from cli.lib.initial_workspace import _baseline_path

    a = _baseline_path(tmp_path / "wsA")
    b = _baseline_path(tmp_path / "wsB")
    assert a != b


def test_unique_bak_path(tmp_path):
    from src.initial_workspace import _unique_bak_path

    p = tmp_path / "x.bak.TS"
    assert _unique_bak_path(p) == p  # free
    p.write_text("")
    assert _unique_bak_path(p) == tmp_path / "x.bak.TS.1"
    (tmp_path / "x.bak.TS.1").write_text("")
    assert _unique_bak_path(p) == tmp_path / "x.bak.TS.2"


def test_prune_backups_keeps_only_the_newest_n(tmp_path):
    """#1476: retention caps `.bak.*` growth to the N most recent."""
    from src.initial_workspace import _MAX_BACKUPS_PER_FILE, _prune_backups

    original = tmp_path / "CLAUDE.md"
    original.write_text("current\n")
    stamps = [
        "20260810T000000Z",
        "20260811T000000Z",
        "20260812T000000Z",
        "20260813T000000Z",
        "20260814T000000Z",
    ]
    for stamp in stamps:
        (tmp_path / f"CLAUDE.md.bak.{stamp}").write_text(stamp)

    _prune_backups(original, keep=3)

    remaining = sorted(p.name for p in tmp_path.glob("CLAUDE.md.bak.*"))
    assert remaining == [
        "CLAUDE.md.bak.20260812T000000Z",
        "CLAUDE.md.bak.20260813T000000Z",
        "CLAUDE.md.bak.20260814T000000Z",
    ]
    # Default keep= matches the module constant (no caller passes a literal).
    assert _MAX_BACKUPS_PER_FILE == 3


def test_prune_backups_noop_when_at_or_under_cap(tmp_path):
    from src.initial_workspace import _prune_backups

    original = tmp_path / "CLAUDE.md"
    original.write_text("current\n")
    (tmp_path / "CLAUDE.md.bak.20260814T000000Z").write_text("x")

    _prune_backups(original, keep=3)

    assert list(tmp_path.glob("CLAUDE.md.bak.*")) != []
    assert len(list(tmp_path.glob("CLAUDE.md.bak.*"))) == 1


# ===========================================================================
# Layer 2: pure 3-way diff engine (src)
# ===========================================================================


def test_classify_three_way(tmp_path):
    from src.initial_workspace import classify_workspace_update

    base = _make_zip({"a.md": b"a1\n", "b.md": b"b1\n", "same.md": b"s\n"})
    _w(tmp_path / "a.md", b"MINE\n")  # analyst changed → backed_up
    _w(tmp_path / "b.md", b"b1\n")  # unchanged → updated
    _w(tmp_path / "same.md", b"s\n")  # identical to new → no-op
    _w(tmp_path / "extra.txt", b"mine\n")  # not in template → preserved

    new = _make_zip(
        {
            "a.md": b"a2\n",
            "b.md": b"b2\n",
            "same.md": b"s\n",
            "c.md": b"c\n",  # new → created
        }
    )
    plan = classify_workspace_update(tmp_path, new, base)
    assert plan.created == ["c.md"]
    assert plan.updated == ["b.md"]
    assert plan.backed_up == ["a.md"]


def test_update_applies_three_way_with_backup(tmp_path):
    from src.initial_workspace import update_workspace_from_template

    base = _make_zip({"a.md": b"a1\n", "b.md": b"b1\n"})
    _w(tmp_path / "a.md", b"MINE\n")
    _w(tmp_path / "b.md", b"b1\n")
    _w(tmp_path / "extra.txt", b"keep\n")

    new = _make_zip({"a.md": b"a2\n", "b.md": b"b2\n", "c.md": b"c\n"})
    result = update_workspace_from_template(
        tmp_path,
        new,
        base,
        agnes_version="9.9",
        server_url="http://x",
        template_source="repo",
        template_sha="newsha",
    )

    # a.md: analyst-changed → backed up, then refreshed
    assert [n for n, _ in result.backed_up] == ["a.md"]
    bak_rel = result.backed_up[0][1]
    assert (tmp_path / bak_rel).read_bytes() == b"MINE\n"
    assert (tmp_path / "a.md").read_bytes() == b"a2\n"
    # b.md: unchanged by analyst → updated, no .bak
    assert result.updated == ["b.md"]
    assert not list(tmp_path.glob("b.md.bak*"))
    assert (tmp_path / "b.md").read_bytes() == b"b2\n"
    # c.md: created
    assert result.created == ["c.md"]
    assert (tmp_path / "c.md").read_bytes() == b"c\n"
    # extra.txt: preserved
    assert (tmp_path / "extra.txt").read_bytes() == b"keep\n"
    # sentinel refreshed (baseline persistence is the CLI's job, not the engine's)
    sentinel = (tmp_path / ".claude" / "init-complete").read_text()
    assert "override: true" in sentinel
    assert "template_sha: newsha" in sentinel


def test_update_without_baseline_backs_up_every_change(tmp_path):
    """No baseline → any differing file is treated as analyst-modified and
    backed up (conservative)."""
    from src.initial_workspace import update_workspace_from_template

    _w(tmp_path / "CLAUDE.md", b"local\n")
    new = _make_zip({"CLAUDE.md": b"new\n"})
    result = update_workspace_from_template(
        tmp_path,
        new,
        None,
        agnes_version="9.9",
        server_url="http://x",
        template_source=None,
        template_sha="x",
    )
    assert [n for n, _ in result.backed_up] == ["CLAUDE.md"]
    assert (tmp_path / result.backed_up[0][1]).read_bytes() == b"local\n"
    assert (tmp_path / "CLAUDE.md").read_bytes() == b"new\n"


def test_update_prunes_backups_across_repeated_merges(tmp_path):
    """#1476: each 3-way merge that backs up an analyst-edited file must
    prune old `.bak.*` siblings down to the retention cap, not just the
    DEFAULT-mode (no-IWT) refresh path."""
    from src.initial_workspace import _MAX_BACKUPS_PER_FILE, update_workspace_from_template

    _w(tmp_path / "CLAUDE.md", b"v0 (analyst-edited)\n")
    for i in range(1, _MAX_BACKUPS_PER_FILE + 3):
        new = _make_zip({"CLAUDE.md": f"v{i}\n".encode()})
        update_workspace_from_template(
            tmp_path,
            new,
            None,  # no baseline → always classified as analyst-modified → backed up
            agnes_version="9.9",
            server_url="http://x",
            template_source=None,
            template_sha=f"sha{i}",
        )

    backups = list(tmp_path.glob("CLAUDE.md.bak.*"))
    assert len(backups) == _MAX_BACKUPS_PER_FILE


def test_update_rejects_unsafe_entry_writes_nothing(tmp_path):
    from src.initial_workspace import update_workspace_from_template

    data = _make_zip({"../escape.txt": b"naughty"})
    with pytest.raises(ValueError):
        update_workspace_from_template(
            tmp_path,
            data,
            None,
            agnes_version="9",
            server_url="s",
            template_source=None,
            template_sha=None,
        )
    assert not (tmp_path.parent / "escape.txt").exists()


# ===========================================================================
# Layer 3: apply_override persists the baseline on first init
# ===========================================================================


def test_apply_override_writes_baseline(tmp_path, monkeypatch):
    _isolate_config(monkeypatch, tmp_path)
    from cli.lib import initial_workspace as iw

    ws = tmp_path / "ws"
    zip_bytes = _make_zip({"CLAUDE.md": b"# Template\n"})
    status = iw.StatusInfo(
        configured=True,
        synced=True,
        template_source="https://github.com/acme/t",
        template_sha="sha1",
        files=["CLAUDE.md"],
    )
    monkeypatch.setattr(iw, "download_zip", lambda *a, **k: zip_bytes)
    monkeypatch.setattr(iw, "report_applied", lambda *a, **k: None)
    monkeypatch.setattr(iw, "_fetch_connector_params", lambda *a, **k: None)

    result = iw.apply_override(
        ws,
        status,
        "http://x",
        "t",
        force=False,
        agnes_version="9.9",
    )
    assert (ws / "CLAUDE.md").read_bytes() == b"# Template\n"
    assert result.created == ["CLAUDE.md"]
    # Baseline stored in the config dir, not the workspace.
    assert iw.load_template_baseline(ws) == zip_bytes
    assert not (ws / ".claude" / "agnes" / "installed-template.zip").exists()


# ===========================================================================
# Layer 4: `agnes update-workspace` command end-to-end
# ===========================================================================


from cli.commands.update_workspace import update_workspace_app  # noqa: E402

runner = CliRunner()


def _build_api_get(status: dict | None, zip_bytes: bytes = b""):
    def _api_get(path, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b""
        resp.headers = {}
        if path == "/api/initial-workspace":
            if status is None:
                resp.status_code = 404
            else:
                resp.json.return_value = status
        elif path == "/api/initial-workspace.zip":
            resp.content = zip_bytes
        else:
            resp.json.return_value = {}
        return resp

    return _api_get


def _stub_api_post():
    def _api_post(path, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"status": "ok"}
        return resp

    return _api_post


def _setenv(monkeypatch, tmp_path):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    monkeypatch.setenv("AGNES_SERVER", "http://x")
    monkeypatch.setenv("AGNES_TOKEN", "t")


def _wire(monkeypatch, api_get, api_post=None):
    monkeypatch.setattr("cli.lib.initial_workspace.api_get", api_get, raising=False)
    if api_post is not None:
        monkeypatch.setattr("cli.lib.initial_workspace.api_post", api_post, raising=False)


def test_command_noop_when_not_configured(tmp_path, monkeypatch):
    _setenv(monkeypatch, tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    _wire(monkeypatch, _build_api_get(status={"configured": False}))

    result = runner.invoke(update_workspace_app, ["--workspace", str(ws)])
    assert result.exit_code == 0, result.output
    assert "no Initial Workspace Template" in _clean(result.output)
    # Touched nothing
    assert list(ws.iterdir()) == []


def test_command_noop_on_404(tmp_path, monkeypatch):
    _setenv(monkeypatch, tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    _wire(monkeypatch, _build_api_get(status=None))

    result = runner.invoke(update_workspace_app, ["--workspace", str(ws)])
    assert result.exit_code == 0, result.output
    assert list(ws.iterdir()) == []


def test_command_exits_when_not_synced(tmp_path, monkeypatch):
    _setenv(monkeypatch, tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    _wire(monkeypatch, _build_api_get(status={"configured": True, "synced": False}))

    result = runner.invoke(update_workspace_app, ["--workspace", str(ws)])
    assert result.exit_code == 1
    assert "initial_workspace_not_synced" in _clean(result.output)


def _prep_ws_with_baseline(ws: Path):
    """Workspace where the analyst changed a.md, left b.md, added extra.txt;
    baseline reflects the original {a1,b1}. Requires AGNES_CONFIG_DIR set."""
    from cli.lib.initial_workspace import save_template_baseline

    ws.mkdir(parents=True, exist_ok=True)
    base = _make_zip({"a.md": b"a1\n", "b.md": b"b1\n"})
    save_template_baseline(ws, base)
    _w(ws / "a.md", b"MINE\n")
    _w(ws / "b.md", b"b1\n")
    _w(ws / "extra.txt", b"keep\n")


def _new_status_and_zip():
    new_zip = _make_zip({"a.md": b"a2\n", "b.md": b"b2\n", "c.md": b"c\n"})
    status = {
        "configured": True,
        "synced": True,
        "template_source": "https://github.com/acme/t",
        "template_sha": "newsha",
        "files": ["a.md", "b.md", "c.md"],
    }
    return status, new_zip


def test_command_dry_run_writes_nothing(tmp_path, monkeypatch):
    _setenv(monkeypatch, tmp_path)
    ws = tmp_path / "ws"
    _prep_ws_with_baseline(ws)
    status, new_zip = _new_status_and_zip()
    _wire(monkeypatch, _build_api_get(status, new_zip), _stub_api_post())

    result = runner.invoke(update_workspace_app, ["--workspace", str(ws), "--dry-run"])
    assert result.exit_code == 0, result.output
    out = _clean(result.output)
    assert "a.md" in out and "Would back up" in out
    # Nothing changed on disk
    assert (ws / "a.md").read_bytes() == b"MINE\n"
    assert (ws / "b.md").read_bytes() == b"b1\n"
    assert not (ws / "c.md").exists()
    assert not list(ws.glob("a.md.bak*"))


def test_command_yes_applies_with_backup(tmp_path, monkeypatch):
    from cli.lib.initial_workspace import load_template_baseline

    _setenv(monkeypatch, tmp_path)
    ws = tmp_path / "ws"
    _prep_ws_with_baseline(ws)
    status, new_zip = _new_status_and_zip()
    _wire(monkeypatch, _build_api_get(status, new_zip), _stub_api_post())

    result = runner.invoke(update_workspace_app, ["--workspace", str(ws), "--yes"])
    assert result.exit_code == 0, result.output
    # a.md changed by analyst → backed up + refreshed
    assert (ws / "a.md").read_bytes() == b"a2\n"
    baks = list(ws.glob("a.md.bak.*"))
    assert len(baks) == 1 and baks[0].read_bytes() == b"MINE\n"
    # b.md unchanged → updated, no .bak
    assert (ws / "b.md").read_bytes() == b"b2\n"
    assert not list(ws.glob("b.md.bak*"))
    # c.md created; extra.txt preserved
    assert (ws / "c.md").read_bytes() == b"c\n"
    assert (ws / "extra.txt").read_bytes() == b"keep\n"
    # baseline rewritten by the CLI layer
    assert load_template_baseline(ws) == new_zip
    out = _clean(result.output)
    assert "Backed up: 1" in out


def test_command_prompt_no_aborts(tmp_path, monkeypatch):
    import typer

    _setenv(monkeypatch, tmp_path)
    ws = tmp_path / "ws"
    _prep_ws_with_baseline(ws)
    status, new_zip = _new_status_and_zip()
    _wire(monkeypatch, _build_api_get(status, new_zip), _stub_api_post())
    monkeypatch.setattr(typer, "prompt", lambda *a, **k: "no")

    result = runner.invoke(update_workspace_app, ["--workspace", str(ws)])
    assert result.exit_code == 1
    # Untouched
    assert (ws / "a.md").read_bytes() == b"MINE\n"
    assert not (ws / "c.md").exists()


def test_command_prompt_YES_applies(tmp_path, monkeypatch):
    import typer

    _setenv(monkeypatch, tmp_path)
    ws = tmp_path / "ws"
    _prep_ws_with_baseline(ws)
    status, new_zip = _new_status_and_zip()
    _wire(monkeypatch, _build_api_get(status, new_zip), _stub_api_post())
    monkeypatch.setattr(typer, "prompt", lambda *a, **k: "YES")

    result = runner.invoke(update_workspace_app, ["--workspace", str(ws)])
    assert result.exit_code == 0, result.output
    assert (ws / "a.md").read_bytes() == b"a2\n"


def test_command_already_up_to_date(tmp_path, monkeypatch):
    from cli.lib.initial_workspace import save_template_baseline

    _setenv(monkeypatch, tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    same = _make_zip({"a.md": b"a1\n"})
    save_template_baseline(ws, same)
    _w(ws / "a.md", b"a1\n")
    status = {
        "configured": True,
        "synced": True,
        "template_source": "repo",
        "template_sha": "s",
        "files": ["a.md"],
    }
    _wire(monkeypatch, _build_api_get(status, same), _stub_api_post())

    result = runner.invoke(update_workspace_app, ["--workspace", str(ws)])
    assert result.exit_code == 0, result.output
    assert "already matches" in _clean(result.output)
