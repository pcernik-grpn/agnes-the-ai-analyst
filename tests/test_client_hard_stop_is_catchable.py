"""A redirect must not kill the callers that handle their own failures.

`cli/client.py` refuses to follow a redirect, from an httpx *response event
hook* that runs deep inside somebody else's `api_get(...)`. It used to end
the process there with `sys.stderr.write` + `sys.exit(2)`. `SystemExit`
derives from `BaseException`, so it walked straight through `except
Exception` — voiding the two callers built precisely to survive a failed
request:

- `agnes diagnose` records a row per failed check. Measured against the real
  relocated hostname on 2026-08-12 — an unreachable server gives exit 0 and
  the full JSON checklist, a redirect gave exit 2, empty stdout and not one
  check, from the command whose entire job is to say what is wrong.
- `agnes update` wraps every step so one failure cannot abort the run.
  `_run_step` did not contain it either, so the process died before the
  report was written.

It now raises `RedirectHardStop`, which **also derives from
`BaseException`** — deliberately. The first draft of this made it an
ordinary `Exception`, which fixed those two by exposing the stop to every
broad `except Exception` in the CLI. Devin Review on #1277 listed the
damage, and `TestCommandsThatDidNotOptInAreUntouched` below is that list
turned into tests: `agnes diagnose system` printed `Cannot reach server:
<…answered HTTP 308…>` — a heading contradicting its own body, the very lie
this line of work exists to remove — while `query`, `pull` and `chat`
relabelled it and exited 1 instead of 2.

So the rule is opt-in by name: a caller that wants to survive a redirect
writes `except RedirectHardStop`, and everything else behaves exactly as it
did. The version floor next door stays an unconditional `sys.exit` — a
server refusing this CLI version must stop the run, not become one row in a
report while the remaining steps keep talking to it.
"""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
import logging

import pytest
from typer.testing import CliRunner

from cli.client import RedirectHardStop, _check_moved_server

runner = CliRunner()


def _redirect(status: int = 308, location: str = "https://new.example/api/health") -> httpx.Response:
    return httpx.Response(
        status_code=status,
        headers={"Location": location},
        request=httpx.Request("GET", "https://old.example/api/health"),
    )


@pytest.fixture
def moved_server(monkeypatch):
    """Every `api_get` reachable from `diagnose` meets a server that has moved.

    Patched at the USE site, not only at the definition site.
    `cli/commands/diagnose.py` does `from cli.client import api_get` at module
    scope, so it holds its own binding and never sees a patch of
    `cli.client.api_get`. Patching only the latter made these tests pass or
    fail on import order alone: run by themselves they were green, because
    the diagnose module was first imported *inside* the test, after the
    patch — put `tests/test_cli_diagnose.py` in front and the module was
    already in `sys.modules` with the real function bound, and the redirect
    silently became "Can't reach the agnes server".
    """
    monkeypatch.setattr("cli.client.get_server_url", lambda: "https://old.example")

    def _raise(*_a, **_kw):
        _check_moved_server(_redirect())

    import cli.commands.diagnose  # noqa: F401 — ensure the binding exists to patch

    monkeypatch.setattr("cli.client.api_get", _raise)
    monkeypatch.setattr("cli.commands.diagnose.api_get", _raise)
    return _raise


class TestOnlyAnOptInHandlerSeesIt:
    def test_a_broad_except_exception_does_not_swallow_it(self):
        """The property the whole design rests on.

        If this ever starts catching, every broad handler in the CLI starts
        relabelling a moved server as its own kind of failure again.
        """
        with pytest.raises(RedirectHardStop):
            try:
                _check_moved_server(_redirect())
            except Exception as exc:  # noqa: BLE001 — the point is that it does not fire
                pytest.fail(f"a broad handler swallowed it as {type(exc).__name__}")

    def test_naming_it_explicitly_does_catch_it(self):
        caught = None
        try:
            _check_moved_server(_redirect())
        except RedirectHardStop as exc:
            caught = exc
        assert caught is not None, "an aggregator cannot opt in at all"

    def test_the_message_still_carries_the_remedy(self):
        with pytest.raises(RedirectHardStop) as exc:
            _check_moved_server(_redirect())
        assert "new.example" in exc.value.user_message
        assert "AGNES_SERVER" in exc.value.user_message


class TestDiagnoseStillDiagnoses:
    """The command that exists to report failures must report this one."""

    def _run(self, args: list[str]):
        from cli.commands.diagnose import diagnose_app

        return runner.invoke(diagnose_app, args)

    def test_it_produces_a_checklist_instead_of_dying(self, moved_server):
        result = self._run(["--json"])
        assert result.stdout.strip(), "a moved server produced no output at all"
        payload = json.loads(result.stdout)
        assert payload["checks"], "not a single check ran"

    def test_the_api_check_names_where_the_server_went(self, moved_server):
        payload = json.loads(self._run(["--json"]).stdout)
        api = next(c for c in payload["checks"] if c["name"] == "api")
        assert api["status"] == "error"
        assert "new.example" in api["detail"]

    def test_the_other_checks_still_run(self, moved_server):
        """One dead check must not take the local-side checks with it."""
        payload = json.loads(self._run(["--json"]).stdout)
        assert {c["name"] for c in payload["checks"]} - {"api"}, "only the failing check survived"

    def test_a_redirect_on_only_the_detailed_probe_does_not_duplicate_the_row(self, monkeypatch):
        """`/api/health` fine, `/api/health/detailed` redirected — one row, not two.

        The outer `try` spans both requests. Because `RedirectHardStop` skips
        `except Exception`, a redirect on the *second* one unwinds past the
        nested best-effort handler into the outer opt-in, which appends a
        second `api` row saying `error` next to the `ok` one already there.
        A reader is told the server is both fine and broken, and anything
        doing `next(c for c in checks if c["name"] == "api")` gets whichever
        landed first. (Devin Review on #1277.)
        """
        import cli.commands.diagnose  # noqa: F401

        def _selective(path, *_a, **_kw):
            if path == "/api/health":
                ok = httpx.Response(
                    200, json={"status": "ok"}, request=httpx.Request("GET", "https://old.example/api/health")
                )
                # `.elapsed` raises on a hand-built response, and diagnose
                # reads it for latency_ms — without this the healthy row is
                # never appended and the test measures the wrong thing.
                ok.elapsed = timedelta(milliseconds=5)
                return ok
            _check_moved_server(_redirect(location="https://new.example/api/health/detailed"))

        monkeypatch.setattr("cli.client.get_server_url", lambda: "https://old.example")
        monkeypatch.setattr("cli.commands.diagnose.api_get", _selective)

        payload = json.loads(self._run(["--json"]).stdout)
        api_rows = [c for c in payload["checks"] if c["name"] == "api"]
        assert len(api_rows) == 1, f"contradictory rows for one check: {api_rows}"
        assert api_rows[0]["status"] == "ok", "the reachability probe succeeded"


class TestUpdateStepIsolationHolds:
    """`_run_step` exists so one bad step cannot abort a convergence run."""

    def test_a_hard_stop_is_contained_as_an_error_row(self):
        from cli.commands.update import _run_step

        report: list[dict] = []
        _run_step("probe", lambda: _check_moved_server(_redirect()), report)
        assert report, "the step took the whole run down instead of reporting"
        assert report[0]["status"] == "error"


class TestCommandsThatDidNotOptInAreUntouched:
    """Devin Review's list from the first draft, turned into assertions.

    Each of these wraps its API call in a broad `except Exception` and
    relabels the failure. None of them opted into handling a redirect, so
    none of them may see one — otherwise the PR's central claim ("nothing
    changes for the commands this was written for") is false, which is
    exactly how it was false the first time.
    """

    def _redirect_reaches(self, handler_body) -> bool:
        """True when a handler shaped like the command's own catches it."""
        try:
            handler_body()
        except RedirectHardStop:
            return False
        return True

    def test_a_diagnose_system_style_handler_cannot_relabel_it(self):
        """It printed `Cannot reach server: …answered HTTP 308…` — a heading
        that contradicts its own body, and the precise lie this whole line of
        work exists to remove."""

        def like_diagnose_system():
            try:
                _check_moved_server(_redirect())
            except Exception as e:  # noqa: BLE001 — mirrors cli/commands/diagnose.py
                raise AssertionError(f"relabelled as 'Cannot reach server: {e}'") from None

        assert not self._redirect_reaches(like_diagnose_system)

    def test_a_query_style_handler_cannot_relabel_it(self):
        def like_query():
            try:
                _check_moved_server(_redirect())
            except Exception as e:  # noqa: BLE001 — mirrors cli/commands/query.py
                raise AssertionError(f"relabelled as 'Query error: {e}'") from None

        assert not self._redirect_reaches(like_query)

    def test_a_chat_style_handler_cannot_turn_it_into_a_per_turn_error(self):
        """chat catches `AgnesTransportError` to keep the REPL alive. If a
        redirect lands there, every following turn hits it again and the user
        is told forever instead of once."""
        from cli.client import AgnesTransportError

        def like_chat():
            try:
                _check_moved_server(_redirect())
            except AgnesTransportError:
                raise AssertionError("became a per-turn transport error; the REPL survives") from None

        assert not self._redirect_reaches(like_chat)

    def test_it_is_not_an_agnes_transport_error_at_all(self):
        """The subclassing is what made chat and the renderer swallow it."""
        from cli.client import AgnesTransportError

        with pytest.raises(RedirectHardStop) as exc:
            _check_moved_server(_redirect())
        assert not isinstance(exc.value, AgnesTransportError)


class TestTheVersionFloorStaysUnconditional:
    """A server that refuses this CLI must stop the run, not file a row.

    Making the floor catchable alongside the redirect was the other half of
    the first draft. `_run_step` then recorded it as one error row and kept
    running every remaining convergence step against a server that had just
    declared this binary too old.
    """

    def test_it_still_exits_the_process(self):
        from unittest.mock import patch

        from cli.client import _check_version_headers

        resp = httpx.Response(
            status_code=200,
            headers={"X-Agnes-Latest-Version": "0.40.0", "X-Agnes-Min-Version": "0.35.0"},
            content=b"{}",
            request=httpx.Request("GET", "https://x/"),
        )
        with patch("cli.client._installed_version", return_value="0.30.0"):
            with pytest.raises(SystemExit) as exc:
                _check_version_headers(resp)
        assert exc.value.code == 2

    def test_update_step_isolation_does_not_contain_it(self):
        from unittest.mock import patch

        from cli.commands.update import _run_step
        from cli.client import _check_version_headers

        resp = httpx.Response(
            status_code=200,
            headers={"X-Agnes-Latest-Version": "0.40.0", "X-Agnes-Min-Version": "0.35.0"},
            content=b"{}",
            request=httpx.Request("GET", "https://x/"),
        )
        report: list[dict] = []
        with patch("cli.client._installed_version", return_value="0.30.0"):
            with pytest.raises(SystemExit):
                _run_step("probe", lambda: _check_version_headers(resp), report)
        assert report == [], "the run carried on against a server that refused this CLI"


class TestTheUserFacingContractIsUnchanged:
    """Catchable is the only thing that changed. Not the text, not the code."""

    def test_it_still_exits_2(self):
        with pytest.raises(RedirectHardStop) as exc:
            _check_moved_server(_redirect())
        assert exc.value.exit_code == 2

    def test_main_renders_it_exactly_as_the_hook_used_to(self, monkeypatch, capsys):
        """Rendering moved from the hook to `cli/main.py`; the bytes did not.

        Drives the real `main()` — asserting against a hand-rolled copy of
        its handler would only prove the copy.
        """
        import cli.main as m

        def boom():
            raise RedirectHardStop("old.example answered HTTP 308 (redirect to https://new.example/x)")

        monkeypatch.setattr(m, "app", boom)
        with pytest.raises(SystemExit) as exit_exc:
            m.main()
        assert exit_exc.value.code == 2, "the exit code scripts read has changed"
        err = capsys.readouterr().err
        assert err.startswith("error: "), f"prefix changed: {err[:40]!r}"
        assert "new.example" in err

    def test_a_hard_stop_is_not_reported_as_an_error(self, monkeypatch, caplog):
        """It was a `SystemExit`, which this wrapper never reported.

        Making it catchable must not also start reporting it as a failure —
        that is a separate decision, not a refactor. This used to be pinned
        against the CLI's telemetry forwarder; that forwarder is gone, so
        the surviving way to report a hard stop is a log record, and a
        redirect the CLI declines to follow is a message to the user, not a
        failure of the run.
        """
        import cli.main as m

        monkeypatch.setattr(m, "app", lambda: (_ for _ in ()).throw(RedirectHardStop("moved")))
        with caplog.at_level(logging.ERROR):
            with pytest.raises(SystemExit):
                m.main()
        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == [], (
            "a structural fix quietly started reporting a hard stop as an error"
        )
