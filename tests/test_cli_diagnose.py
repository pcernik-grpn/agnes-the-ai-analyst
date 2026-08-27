"""Tests for agnes diagnose command."""

import json
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    yield tmp_path


def _resp(status_code=200, json_data=None):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data if json_data is not None else {}
    r.elapsed = MagicMock()
    r.elapsed.total_seconds.return_value = 0.042
    return r


HEALTHY_HEALTH = {
    "status": "ok",
    "instance_name": "Test Instance",
    "services": {
        "duckdb": {"status": "ok", "tables": 5},
        "scheduler": {"status": "ok"},
    },
}


class TestDiagnoseText:
    def test_diagnose_healthy(self):
        """Diagnose healthy system shows 'healthy' overall."""
        with patch("cli.commands.diagnose.api_get", return_value=_resp(200, HEALTHY_HEALTH)):
            result = runner.invoke(app, ["diagnose"])
        assert result.exit_code == 0
        assert "healthy" in result.output.lower()
        assert "api" in result.output
        assert "duckdb" in result.output

    def test_diagnose_api_unreachable(self):
        """Diagnose marks overall as unhealthy when API is down."""
        with patch("cli.commands.diagnose.api_get", side_effect=Exception("Connection refused")):
            result = runner.invoke(app, ["diagnose"])
        assert result.exit_code == 0
        assert "unhealthy" in result.output.lower()
        assert "api" in result.output
        assert "Server unreachable" in result.output or "Suggested actions" in result.output

    def test_diagnose_warning_service(self):
        """Diagnose shows 'degraded' when a service reports warning."""
        health = {
            "services": {
                "duckdb": {"status": "warning", "stale_tables": ["orders"]},
            }
        }
        with patch("cli.commands.diagnose.api_get", return_value=_resp(200, health)):
            result = runner.invoke(app, ["diagnose"])
        assert result.exit_code == 0
        assert "degraded" in result.output.lower()


class TestDiagnoseJson:
    def test_diagnose_json_output(self):
        """--json flag produces parseable JSON with expected structure."""
        with patch("cli.commands.diagnose.api_get", return_value=_resp(200, HEALTHY_HEALTH)):
            result = runner.invoke(app, ["diagnose", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "overall" in data
        assert "checks" in data
        assert "suggested_actions" in data

    def test_diagnose_json_api_down(self):
        """--json flag still emits valid JSON when API is unreachable."""
        with patch("cli.commands.diagnose.api_get", side_effect=Exception("timeout")):
            result = runner.invoke(app, ["diagnose", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["overall"] == "unhealthy"
        assert any(c["name"] == "api" and c["status"] == "error" for c in data["checks"])

    def test_diagnose_json_has_latency(self):
        """Healthy API check includes latency_ms in JSON output."""
        with patch("cli.commands.diagnose.api_get", return_value=_resp(200, HEALTHY_HEALTH)):
            result = runner.invoke(app, ["diagnose", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        api_check = next(c for c in data["checks"] if c["name"] == "api")
        assert "latency_ms" in api_check

    def test_diagnose_json_wires_session_upload_warning(self, tmp_path):
        """`agnes diagnose --json` calls session_upload_health when
        workspace_root is set, appends the `session-upload` check, and surfaces
        the `agnes push --dry-run` suggested action on warning."""
        warning = {
            "name": "session-upload",
            "status": "warning",
            "expected_sessions": 9,
            "uploaded_entries": 1,
            "detail": "session upload may be failing. Try: `agnes push --dry-run`",
        }
        with (
            patch("cli.commands.diagnose.api_get", return_value=_resp(200, HEALTHY_HEALTH)),
            patch("cli.commands.diagnose.get_workspace_root", return_value=str(tmp_path)),
            patch("cli.commands.diagnose.session_upload_health", return_value=warning),
        ):
            result = runner.invoke(app, ["diagnose", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert any(c["name"] == "session-upload" and c["status"] == "warning" for c in data["checks"])
        assert any("agnes push --dry-run" in a for a in data["suggested_actions"])

    def test_diagnose_json_session_upload_info_without_workspace_root(self):
        """No workspace_root → session-upload check is info (not warning) and
        session_upload_health is not even called."""
        with (
            patch("cli.commands.diagnose.api_get", return_value=_resp(200, HEALTHY_HEALTH)),
            patch("cli.commands.diagnose.get_workspace_root", return_value=None),
            patch("cli.commands.diagnose.session_upload_health") as mock_health,
        ):
            result = runner.invoke(app, ["diagnose", "--json"])
        assert result.exit_code == 0
        mock_health.assert_not_called()
        data = json.loads(result.output)
        check = next(c for c in data["checks"] if c["name"] == "session-upload")
        assert check["status"] == "info"


class TestAnalystAudienceFilter:
    """Issue #345 B — analysts shouldn't see ``Overall: degraded`` on fresh
    install just because the server has operator-level warnings (stale
    tables, session-pipeline behind). The role-aware headline lets the
    server tag each check with ``audience`` and report ``caller_role``;
    the CLI filters the overall when caller is analyst.
    """

    def test_analyst_sees_healthy_when_only_operator_checks_warn(self):
        """Analyst role + operator-side warning → ``Overall: healthy``
        with a secondary line surfacing the operator warning count.
        Pre-fix behaviour was ``Overall: degraded`` even though the
        analyst can't act on the stale-tables warning."""
        health = {
            "caller_role": "analyst",
            "services": {
                "duckdb_state": {"status": "ok", "audience": "analyst"},
                "data": {
                    "status": "warning",
                    "audience": "operator",
                    "stale_tables": ["orders"],
                },
                "session_pipeline": {
                    "status": "warning",
                    "audience": "operator",
                    "detail": "verification-detector behind by ~8.8h",
                },
            },
        }
        with patch("cli.commands.diagnose.api_get", return_value=_resp(200, health)):
            result = runner.invoke(app, ["diagnose"])
        assert result.exit_code == 0
        # Headline is analyst-side healthy with a secondary count line
        assert "healthy (analyst-side)" in result.output
        assert "2 operator-side warnings" in result.output
        # Per-check rows still show the warnings — we just don't escalate
        assert "[warning] data" in result.output

    def test_admin_caller_role_aggregates_full_set(self):
        """Admin/operator role auto-promotes to the full aggregation —
        operator-side warnings DO escalate the headline."""
        health = {
            "caller_role": "admin",
            "services": {
                "duckdb_state": {"status": "ok", "audience": "analyst"},
                "data": {
                    "status": "warning",
                    "audience": "operator",
                    "stale_tables": ["orders"],
                },
            },
        }
        with patch("cli.commands.diagnose.api_get", return_value=_resp(200, health)):
            result = runner.invoke(app, ["diagnose"])
        assert result.exit_code == 0
        assert "degraded" in result.output.lower()
        # No analyst-side qualifier — admin sees the full headline directly
        assert "analyst-side" not in result.output

    def test_analyst_with_include_operator_checks_flag(self):
        """``--include-operator-checks`` lets an analyst opt in to the
        full aggregation when they actually want to see the operator
        warnings drive the headline (e.g. when paging an operator)."""
        health = {
            "caller_role": "analyst",
            "services": {
                "duckdb_state": {"status": "ok", "audience": "analyst"},
                "data": {
                    "status": "warning",
                    "audience": "operator",
                    "stale_tables": ["orders"],
                },
            },
        }
        with patch("cli.commands.diagnose.api_get", return_value=_resp(200, health)):
            result = runner.invoke(app, ["diagnose", "--include-operator-checks"])
        assert result.exit_code == 0
        assert "degraded" in result.output.lower()
        assert "analyst-side" not in result.output

    def test_legacy_server_response_keeps_full_aggregation(self):
        """When the server doesn't ship ``caller_role`` (older deploy),
        the CLI must NOT silently start filtering — that would regress
        diagnoses against any pre-#345-B server. Test_diagnose_warning_service
        above already covers the same shape; this test makes the contract
        explicit."""
        health = {
            "services": {
                "duckdb": {"status": "warning", "stale_tables": ["x"]},
            },
            # No caller_role → no role-aware filtering.
        }
        with patch("cli.commands.diagnose.api_get", return_value=_resp(200, health)):
            result = runner.invoke(app, ["diagnose"])
        assert result.exit_code == 0
        assert "degraded" in result.output.lower()


class TestLocalDeliveryCheck:
    """`agnes diagnose` used to report only what the SERVER thinks.

    A fresh analyst whose `agnes pull` had 403'd on the download saw
    "Overall: healthy" including "[ok] data" while no table was reachable.
    They stopped trusting the command and went to raw curl. This check
    compares what the manifest offers against what is on the laptop.
    """

    def _run(self, monkeypatch, tmp_path, *, manifest, on_disk=()):
        """Drive the check against the REAL manifest shape.

        `GET /api/sync/manifest` returns ``{"tables": {table_id: {...}}}`` — a
        dict keyed by table id (`app/api/sync.py:_build_manifest_for_user`),
        which is how every other consumer reads it (`cli/lib/pull.py`:
        ``manifest.get("tables", {})``). A fixture that feeds a LIST hides the
        bug rather than guarding against it.
        """
        from unittest.mock import MagicMock

        import cli.commands.diagnose as mod

        parquet_dir = tmp_path / "server" / "parquet"
        parquet_dir.mkdir(parents=True, exist_ok=True)
        for name in on_disk:
            (parquet_dir / f"{name}.parquet").write_bytes(b"")

        # The check resolves the workspace the way the data commands do
        # (`cli/lib/workspace_resolve.resolve_data_workspace`); the env
        # override is that resolver's first branch and wins over cwd/anchor.
        monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
        resp = MagicMock()
        resp.json.return_value = manifest
        monkeypatch.setattr(mod, "api_get", lambda *a, **k: resp)
        return mod._local_delivery_check()

    @staticmethod
    def _manifest(*names, **overrides):
        """The flat `tables` dict as the server actually emits it."""
        tables = {n: {"query_mode": "local", "server_only": False} for n in names}
        for tid, patch_ in overrides.items():
            tables.setdefault(tid, {"query_mode": "local"}).update(patch_)
        return {"tables": tables}

    def test_nothing_local_while_the_server_offers_tables_warns(self, monkeypatch, tmp_path):
        c = self._run(monkeypatch, tmp_path, manifest=self._manifest("t0"))
        assert c["status"] == "warning", c
        assert "agnes pull" in c["detail"]
        assert c["tables_offered"] == 1 and c["tables_local"] == 0

    def test_everything_local_is_ok(self, monkeypatch, tmp_path):
        c = self._run(monkeypatch, tmp_path, manifest=self._manifest("t0", "t1"), on_disk=("t0", "t1"))
        assert c["status"] == "ok", c

    def test_partial_pull_warns(self, monkeypatch, tmp_path):
        c = self._run(
            monkeypatch,
            tmp_path,
            manifest=self._manifest("t0", "t1", "t2"),
            on_disk=("t0",),
        )
        assert c["status"] == "warning", c

    def test_remote_tables_are_not_expected_on_disk(self, monkeypatch, tmp_path):
        """`query_mode='remote'` rows answer server-side and never land as
        parquet — counting them would report a permanent, unfixable shortfall."""
        c = self._run(monkeypatch, tmp_path, manifest={"tables": {"big": {"query_mode": "remote"}}})
        assert c["status"] == "ok", c
        assert c["tables_offered"] == 0

    def test_server_only_tables_are_not_expected_on_disk(self, monkeypatch, tmp_path):
        """`server_only` rows are kept fresh server-side but their parquet is
        never distributed (`cli/lib/pull.py` skips them), so expecting them
        locally is the same permanent, unfixable shortfall."""
        c = self._run(monkeypatch, tmp_path, manifest=self._manifest(t0={"server_only": True}))
        assert c["status"] == "ok", c
        assert c["tables_offered"] == 0

    def test_partitioned_table_directory_counts_as_local(self, monkeypatch, tmp_path):
        """A partitioned table lives as a DIRECTORY of parts under
        `server/parquet/<table_id>/` (`cli/lib/pull.py:_sync_partitioned_table`)
        and the view rebuild creates one view over it — a top-level
        `*.parquet` glob under-counts it and warns forever."""
        import cli.commands.diagnose as mod

        parts = tmp_path / "server" / "parquet" / "events" / "month=2026-01"
        parts.mkdir(parents=True)
        (parts / "data.parquet").write_bytes(b"")
        # An interrupted partitioned sync leaves this behind; it is never a view.
        (tmp_path / "server" / "parquet" / ".staging-events").mkdir()

        c = self._run(monkeypatch, tmp_path, manifest=self._manifest("events"))
        assert c["status"] == "ok", c
        assert c["tables_local"] == 1
        # The rule now lives in `cli/lib/local_tables.py` so `agnes status` and
        # `agnes diagnose` cannot drift on "what is a local table?"; `diagnose`
        # re-exports it by importing the name.
        assert mod.local_table_names(tmp_path / "server" / "parquet") == {"events"}

    def test_typed_stack_sections_decide_what_is_offered(self, monkeypatch, tmp_path):
        """The flat `tables` dict is gated by `can_access_table`, whose Admin
        short-circuit bypasses the stack, so it over-lists for admins.
        `agnes pull` filters the download set through the typed
        `direct_tables` / `data_packages[].tables[]` sections — the comparison
        must agree with what pull actually fetches."""
        manifest = self._manifest("mine", "someone_elses")
        manifest["direct_tables"] = [{"name": "mine"}]
        manifest["data_packages"] = []

        c = self._run(monkeypatch, tmp_path, manifest=manifest, on_disk=("mine",))
        assert c["status"] == "ok", c
        assert c["tables_offered"] == 1

    def test_pre_v49_server_without_typed_sections_uses_the_flat_dict(self, monkeypatch, tmp_path):
        """A server that ships no typed sections must not collapse `offered`
        to zero — mirrors `run_pull`'s `authorized_names is None` fallback."""
        c = self._run(monkeypatch, tmp_path, manifest=self._manifest("t0"), on_disk=("t0",))
        assert c["status"] == "ok", c
        assert c["tables_offered"] == 1

    def test_a_list_shaped_tables_payload_is_still_tolerated(self, monkeypatch, tmp_path):
        """Defensive: a hand-crafted / proxied payload that sends a list must
        degrade to a comparison, not to an AttributeError swallowed as a warn."""
        c = self._run(
            monkeypatch,
            tmp_path,
            manifest={"tables": [{"id": "t0", "query_mode": "local"}]},
            on_disk=("t0",),
        )
        assert c["status"] == "ok", c
        assert c["tables_offered"] == 1

    def test_resolves_the_workspace_like_the_data_commands_not_the_push_anchor(self, monkeypatch, tmp_path):
        """The check must inspect the workspace `agnes query` reads, which is
        `resolve_data_workspace()`'s answer (`AGNES_LOCAL_DIR` override → cwd
        if workspace-shaped → anchor) — NOT the `workspace_root` config anchor
        that push/session-upload use. Resolving via the anchor while the
        parquets live where the override points reported a false shortfall
        about data the analyst can query fine."""
        from unittest.mock import MagicMock

        import cli.commands.diagnose as mod

        data_ws = tmp_path / "data-ws"
        (data_ws / "server" / "parquet").mkdir(parents=True)
        (data_ws / "server" / "parquet" / "t0.parquet").write_bytes(b"")

        anchor = tmp_path / "push-anchor"  # exists, holds no parquets
        anchor.mkdir()

        monkeypatch.setenv("AGNES_LOCAL_DIR", str(data_ws))
        monkeypatch.setattr(mod, "get_workspace_root", lambda: str(anchor))
        monkeypatch.setattr("cli.lib.workspace_resolve.get_workspace_root", lambda: str(anchor))

        resp = MagicMock()
        resp.json.return_value = {"tables": {"t0": {"query_mode": "local"}}}
        monkeypatch.setattr(mod, "api_get", lambda *a, **k: resp)

        c = mod._local_delivery_check()
        assert c["status"] == "ok", c
        assert c["tables_local"] == 1 and c["tables_offered"] == 1

    def test_env_override_counts_even_when_the_anchor_is_unset(self, monkeypatch, tmp_path):
        """The other face of the same defect: no anchor at all used to read
        as "run `agnes init`" even though `AGNES_LOCAL_DIR` points at a
        workspace full of parquets that every data command would use."""
        from unittest.mock import MagicMock

        import cli.commands.diagnose as mod

        data_ws = tmp_path / "data-ws"
        (data_ws / "server" / "parquet").mkdir(parents=True)
        (data_ws / "server" / "parquet" / "t0.parquet").write_bytes(b"")

        monkeypatch.setenv("AGNES_LOCAL_DIR", str(data_ws))
        monkeypatch.setattr(mod, "get_workspace_root", lambda: None)
        monkeypatch.setattr("cli.lib.workspace_resolve.get_workspace_root", lambda: None)

        resp = MagicMock()
        resp.json.return_value = {"tables": {"t0": {"query_mode": "local"}}}
        monkeypatch.setattr(mod, "api_get", lambda *a, **k: resp)

        c = mod._local_delivery_check()
        assert c["status"] == "ok", c
        assert c["tables_local"] == 1

    def test_no_workspace_says_run_init(self, monkeypatch, tmp_path):
        """A machine with no workspace at all is a benign first-run state —
        `info`, so it never drives the headline. "No workspace" means the
        data resolver finds nothing anywhere: no env override, an unshaped
        cwd, no anchor."""
        import cli.commands.diagnose as mod

        monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
        monkeypatch.setattr("cli.lib.workspace_resolve.get_workspace_root", lambda: None)
        plain = tmp_path / "not-a-workspace"
        plain.mkdir()
        monkeypatch.chdir(plain)
        c = mod._local_delivery_check()
        assert c["status"] == "info"
        assert "agnes init" in c["detail"]

    def test_never_reports_error(self, monkeypatch, tmp_path):
        """An unreadable manifest is the `api` check's business, not a data
        shortfall — this check must never promote the headline to unhealthy."""
        import cli.commands.diagnose as mod

        def _boom(*a, **k):
            raise RuntimeError("manifest unreachable")

        monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
        monkeypatch.setattr(mod, "api_get", _boom)
        c = mod._local_delivery_check()
        assert c["status"] == "info", c

    def test_non_2xx_manifest_is_not_a_false_green(self, monkeypatch, tmp_path):
        """A 401/403/500 manifest comes back as an ordinary response with an
        error body — no `tables` key — which used to read as "the server
        offers nothing": empty offered set, empty shortfall, `[ok]
        local-data` straight through an auth outage. Non-2xx must land in
        the could-not-compare branch, like `cli/lib/pull.py`'s own manifest
        fetch treats it."""
        from unittest.mock import MagicMock

        import httpx

        import cli.commands.diagnose as mod

        (tmp_path / "server" / "parquet").mkdir(parents=True)
        monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))

        resp = MagicMock()
        resp.json.return_value = {"detail": "Not authenticated"}
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "403 Forbidden", request=MagicMock(), response=MagicMock()
        )
        monkeypatch.setattr(mod, "api_get", lambda *a, **k: resp)

        c = mod._local_delivery_check()
        assert c["status"] == "info", c
        assert "could not read the manifest" in c["detail"]

    def test_a_moved_server_does_not_crash_the_command(self, monkeypatch, tmp_path):
        """`RedirectHardStop` derives from BaseException, so the generic
        `except Exception` cannot take it — unhandled, it aborted the whole
        command (exit 2, empty output) on exactly the relocated deployment
        `diagnose` exists to describe. The redirect verdict itself belongs
        to the `api` check; this row just declines the comparison."""
        import cli.commands.diagnose as mod
        from cli.client import RedirectHardStop

        (tmp_path / "server" / "parquet").mkdir(parents=True)
        monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))

        def _redirect(*a, **k):
            raise RedirectHardStop("server moved to https://example.com")

        monkeypatch.setattr(mod, "api_get", _redirect)
        c = mod._local_delivery_check()
        assert c["status"] == "info", c
        assert "server redirected" in c["detail"]

    def test_headline_reflects_the_shortfall(self, monkeypatch, tmp_path):
        """The whole point of the check: a workspace with none of the offered
        tables local must move `Overall:` off `healthy`. `warn` (the setup
        command's vocabulary) is inert here — the aggregation reads
        `warning`."""
        import cli.commands.diagnose as mod

        (tmp_path / "server" / "parquet").mkdir(parents=True)
        monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))

        def _api_get(path, *a, **k):
            if "manifest" in path:
                return _resp(200, {"tables": {"orders": {"query_mode": "local"}}})
            return _resp(200, HEALTHY_HEALTH)

        monkeypatch.setattr(mod, "api_get", _api_get)
        result = runner.invoke(app, ["diagnose"])
        assert result.exit_code == 0
        assert "degraded" in result.output.lower(), result.output


class TestDiagnoseRevealsWorkspace:
    """Issue #1312 (remaining scope, item 3): `agnes diagnose` never named
    the workspace it resolved anywhere — neither on the human path nor in
    `--json` — even though `_local_delivery_check` already resolves and
    inspects one internally."""

    def test_json_includes_resolved_workspace(self, tmp_path, monkeypatch):
        ws = tmp_path / "ws"
        (ws / "server" / "parquet").mkdir(parents=True)
        monkeypatch.setenv("AGNES_LOCAL_DIR", str(ws))
        with patch("cli.commands.diagnose.api_get", return_value=_resp(200, HEALTHY_HEALTH)):
            result = runner.invoke(app, ["diagnose", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["workspace"] == str(ws.resolve())

    def test_human_output_prints_workspace_line(self, tmp_path, monkeypatch):
        ws = tmp_path / "ws"
        (ws / "server" / "parquet").mkdir(parents=True)
        monkeypatch.setenv("AGNES_LOCAL_DIR", str(ws))
        with patch("cli.commands.diagnose.api_get", return_value=_resp(200, HEALTHY_HEALTH)):
            result = runner.invoke(app, ["diagnose"])
        assert result.exit_code == 0, result.output
        assert f"Workspace: {ws.resolve()}" in result.output

    def test_workspace_null_when_none_resolves(self, monkeypatch):
        """No AGNES_LOCAL_DIR, no anchor, cwd not workspace-shaped (the
        isolated `tmp_config` fixture already keeps this test off any real
        `~/.config/agnes/config.yaml`) — `workspace` degrades to `None`, not
        a bogus path."""
        with patch("cli.commands.diagnose.api_get", return_value=_resp(200, HEALTHY_HEALTH)):
            result = runner.invoke(app, ["diagnose", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["workspace"] is None
