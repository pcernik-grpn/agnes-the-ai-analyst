"""`--json` on read commands that were missing it (B4): `skills list`,
`store mine`, `store status`, `admin news versions`, `admin news current`
(the latter is `agnes admin news show` without `--version`, which hits
`GET /api/admin/news/current`).

Each test asserts the command emits parseable JSON containing the same
data the human-readable view shows. Network calls are mocked at the same
seam the existing CLI test suites for these commands already use.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from typer.testing import CliRunner

runner = CliRunner()


def _resp(status_code=200, json_data=None, text=""):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data if json_data is not None else {}
    r.text = text
    return r


# ---------------------------------------------------------------------------
# `agnes skills list --json`
# ---------------------------------------------------------------------------


class TestSkillsListJson:
    def test_json_flag_emits_parseable_list(self):
        from cli.commands.skills import skills_app

        result = runner.invoke(skills_app, ["list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert data, "expected at least one built-in skill"
        for row in data:
            assert set(row.keys()) == {"name", "description"}

    def test_json_flag_matches_human_view_names(self):
        from cli.commands.skills import skills_app

        json_result = runner.invoke(skills_app, ["list", "--json"])
        human_result = runner.invoke(skills_app, ["list"])
        names = {row["name"] for row in json.loads(json_result.output)}
        for name in names:
            assert name in human_result.output


# ---------------------------------------------------------------------------
# `agnes store status --json`
# ---------------------------------------------------------------------------


def _status_body(status: str) -> dict:
    return {
        "entity_id": "e1",
        "name": "my-skill",
        "type": "skill",
        "visibility_status": "approved",
        "version_no": 1,
        "submission": {"id": "s1", "status": status, "error": None},
    }


class TestStoreStatusJson:
    def test_json_flag_emits_parseable_status(self, monkeypatch):
        import cli.commands.store as store_mod

        monkeypatch.setattr(store_mod, "api_get_json", lambda path: _status_body("approved"))
        result = runner.invoke(store_mod.store_app, ["status", "e1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["entity_id"] == "e1"
        assert data["submission"]["status"] == "approved"

    def test_json_flag_nonzero_exit_on_blocked(self, monkeypatch):
        import cli.commands.store as store_mod

        monkeypatch.setattr(store_mod, "api_get_json", lambda path: _status_body("blocked_inline"))
        result = runner.invoke(store_mod.store_app, ["status", "e1", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["submission"]["status"] == "blocked_inline"


# ---------------------------------------------------------------------------
# `agnes store mine --json`
# ---------------------------------------------------------------------------


class TestStoreMineJson:
    def test_json_flag_reports_bytes_and_path(self, monkeypatch, tmp_path):
        import cli.commands.store as store_mod

        def _stream(path, dest, **params):
            with open(dest, "wb") as f:
                f.write(b"PK\x03\x04mine")
            return 7

        monkeypatch.setattr(store_mod, "api_get_stream", _stream)

        out = tmp_path / "mine.zip"
        result = runner.invoke(store_mod.store_app, ["mine", "-o", str(out), "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data == {"bytes": 7, "path": str(out)}

    def test_json_flag_reports_unpack_dir(self, monkeypatch, tmp_path):
        import zipfile

        import cli.commands.store as store_mod

        def _stream(path, dest, **params):
            with zipfile.ZipFile(dest, "w") as zf:
                zf.writestr("entities/e1/manifest.json", "{}")
            return 7

        monkeypatch.setattr(store_mod, "api_get_stream", _stream)

        target = tmp_path / "unpacked"
        result = runner.invoke(store_mod.store_app, ["mine", "--unpack", str(target), "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data == {"unpacked_to": str(target)}


# ---------------------------------------------------------------------------
# `agnes admin news versions --json` / `agnes admin news show --json`
# (`show` without `--version` is the "current" read — GET /api/admin/news/current)
# ---------------------------------------------------------------------------


class TestAdminNewsJson:
    def test_versions_json_flag_emits_parseable_rows(self, monkeypatch):
        import cli.commands.admin_news as mod

        rows = [
            {"version": 2, "status": "draft", "created_at": "2026-01-02", "created_by": "a@test.com"},
            {"version": 1, "status": "published", "created_at": "2026-01-01", "created_by": "a@test.com"},
        ]
        monkeypatch.setattr(mod, "api_get", lambda path: _resp(200, {"versions": rows}))
        result = runner.invoke(mod.admin_news_app, ["versions", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == rows

    def test_current_json_flag_emits_parseable_body(self, monkeypatch):
        """`agnes admin news show --json` (no --version) hits GET .../current."""
        import cli.commands.admin_news as mod

        body = {"version": 3, "published": True, "intro": "<p>hi</p>", "content": "<h1>hi</h1>"}
        captured: dict = {}

        def _get(path):
            captured["path"] = path
            return _resp(200, body)

        monkeypatch.setattr(mod, "api_get", _get)
        result = runner.invoke(mod.admin_news_app, ["show", "--json"])
        assert result.exit_code == 0
        assert captured["path"] == "/api/admin/news/current"
        data = json.loads(result.output)
        assert data == body

    def test_current_json_flag_when_nothing_published(self, monkeypatch):
        import cli.commands.admin_news as mod

        monkeypatch.setattr(mod, "api_get", lambda path: _resp(200, {"published": False}))
        result = runner.invoke(mod.admin_news_app, ["show", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == {"published": False}
