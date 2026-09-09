"""Tests for the CLI auto-update check (cli/update_check.py)."""

import json
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path))
    # Point CLI at a fake server so get_server_url() returns something stable.
    monkeypatch.setenv("AGNES_SERVER", "http://server.test:8000")
    # `agnes onboard` / `agnes update` set AGNES_NO_UPDATE_CHECK=1 in their
    # own process on purpose, so any test that ran either command in-process
    # earlier on this xdist worker leaves `check()` short-circuiting. Start
    # every test here from the switch's absence rather than from whatever
    # the worker inherited.
    monkeypatch.delenv("AGNES_NO_UPDATE_CHECK", raising=False)
    yield tmp_path


def test_check_returns_none_when_disabled(tmp_config, monkeypatch):
    monkeypatch.setenv("AGNES_NO_UPDATE_CHECK", "1")
    from cli import update_check
    assert update_check.check("http://server.test:8000") is None


def test_check_returns_none_when_server_url_missing(tmp_config):
    from cli import update_check
    assert update_check.check("") is None
    assert update_check.check(None) is None  # type: ignore[arg-type]


def test_check_bypass_disabled_overrides_env(monkeypatch, tmp_config):
    """`AGNES_NO_UPDATE_CHECK=1` silences the implicit warning loop, but
    explicit callers (e.g. `agnes self-upgrade`) pass `bypass_disabled=True`
    and must NOT become a silent no-op."""
    from cli import update_check

    monkeypatch.setenv("AGNES_NO_UPDATE_CHECK", "1")
    payload = {
        "version": "9.9.9",
        "wheel_filename": "x.whl",
        "download_url_path": "/cli/wheel/x.whl",
    }
    with patch("cli.update_check._installed_version", return_value="2.0.0"):
        with patch("cli.update_check._fetch_latest", return_value=payload):
            # Default: env var wins, returns None.
            assert update_check.check("http://server.test") is None
            # Bypass: env var ignored.
            info = update_check.check("http://server.test", bypass_disabled=True)
            assert info is not None and info.latest == "9.9.9"


def test_check_returns_none_when_installed_version_unknown(tmp_config):
    from cli import update_check
    with patch("cli.update_check._installed_version", return_value="unknown"):
        assert update_check.check("http://server.test:8000") is None


def test_check_fresh_fetch_and_cache_write(tmp_config):
    from cli import update_check

    payload = {
        "version": "2.1.0",
        "wheel_filename": "agnes_the_ai_analyst-2.1.0-py3-none-any.whl",
        "download_url_path": "/cli/wheel/agnes_the_ai_analyst-2.1.0-py3-none-any.whl",
    }
    with patch("cli.update_check._installed_version", return_value="2.0.0"):
        with patch("cli.update_check._fetch_latest", return_value=payload):
            info = update_check.check("http://server.test:8000")

    assert info is not None
    assert info.installed == "2.0.0"
    assert info.latest == "2.1.0"
    assert info.download_url == (
        "http://server.test:8000/cli/wheel/agnes_the_ai_analyst-2.1.0-py3-none-any.whl"
    )
    assert info.is_outdated() is True

    # Cache file was written and re-reading it returns the same latest.
    cache = json.loads((tmp_config / "update_check.json").read_text())
    assert cache["installed"] == "2.0.0"
    assert cache["latest"] == "2.1.0"


def test_check_uses_cache_within_ttl(tmp_config):
    """Cached entry within 24h skips the network fetch."""
    from cli import update_check

    # Seed a fresh cache entry.
    (tmp_config / "update_check.json").write_text(json.dumps({
        "installed": "2.0.0",
        "server_url": "http://server.test:8000",
        "latest": "2.0.5",
        "download_url": "http://server.test:8000/cli/wheel/agnes_the_ai_analyst-2.0.5-py3-none-any.whl",
        "checked_at": __import__("time").time(),  # now
    }))

    with patch("cli.update_check._installed_version", return_value="2.0.0"):
        with patch("cli.update_check._fetch_latest") as mock_fetch:
            info = update_check.check("http://server.test:8000")

    assert mock_fetch.call_count == 0  # cache hit
    assert info.latest == "2.0.5"
    assert info.is_outdated() is True


def test_check_invalidates_cache_when_installed_version_changed(tmp_config):
    """User ran a fresh install after the cache was written — re-probe."""
    from cli import update_check

    # Seed cache claiming the installed version was 1.9.0.
    (tmp_config / "update_check.json").write_text(json.dumps({
        "installed": "1.9.0",
        "server_url": "http://server.test:8000",
        "latest": "2.0.0",
        "download_url": "http://server.test:8000/cli/wheel/x.whl",
        "checked_at": __import__("time").time(),
    }))

    payload = {"version": "2.1.0", "download_url_path": "/cli/wheel/y.whl"}
    with patch("cli.update_check._installed_version", return_value="2.0.0"):
        with patch("cli.update_check._fetch_latest", return_value=payload) as mock_fetch:
            info = update_check.check("http://server.test:8000")

    assert mock_fetch.call_count == 1  # cache was invalidated
    assert info.latest == "2.1.0"


def test_check_handles_network_failure_silently(tmp_config):
    """A probe that errors out returns None; no exception leaks."""
    from cli import update_check
    with patch("cli.update_check._installed_version", return_value="2.0.0"):
        with patch("cli.update_check._fetch_latest", return_value=None):
            assert update_check.check("http://server.test:8000") is None


def test_negative_cache_avoids_reprobe_on_repeated_failure(tmp_config):
    """Two consecutive check() calls after a failed probe must fire the
    network once — the second call hits the 5-minute negative cache."""
    from cli import update_check

    with patch("cli.update_check._installed_version", return_value="2.0.0"):
        with patch("cli.update_check._fetch_latest", return_value=None) as mock_fetch:
            assert update_check.check("http://server.test:8000") is None
            # Second call within the negative-cache window.
            assert update_check.check("http://server.test:8000") is None

    assert mock_fetch.call_count == 1  # no re-probe


def test_negative_cache_expires_after_ttl(tmp_config):
    """After the negative TTL elapses, the probe fires again."""
    import time
    import json as _json

    from cli import update_check

    # Seed a stale negative-cache entry (older than 5min).
    stale_ts = time.time() - (update_check._NEGATIVE_CACHE_TTL_SECONDS + 60)
    (tmp_config / "update_check.json").write_text(_json.dumps({
        "installed": "2.0.0",
        "server_url": "http://server.test:8000",
        "latest": None,
        "download_url": None,
        "checked_at": stale_ts,
    }))

    payload = {"version": "2.1.0", "download_url_path": "/cli/wheel/x.whl"}
    with patch("cli.update_check._installed_version", return_value="2.0.0"):
        with patch("cli.update_check._fetch_latest", return_value=payload) as mock_fetch:
            info = update_check.check("http://server.test:8000")

    assert mock_fetch.call_count == 1  # cache expired, refetch
    assert info is not None
    assert info.latest == "2.1.0"


def test_is_outdated_false_when_same_version(tmp_config):
    from cli.update_check import UpdateInfo
    info = UpdateInfo(installed="2.0.0", latest="2.0.0", download_url="…")
    assert info.is_outdated() is False


def test_is_outdated_false_when_latest_unknown(tmp_config):
    from cli.update_check import UpdateInfo
    info = UpdateInfo(installed="2.0.0", latest=None, download_url=None)
    assert info.is_outdated() is False


def test_is_outdated_true_when_installed_older(tmp_config):
    from cli.update_check import UpdateInfo
    info = UpdateInfo(installed="2.0.0", latest="2.1.0", download_url="…")
    assert info.is_outdated() is True


def test_is_outdated_false_when_installed_newer_than_server(tmp_config):
    """After a server rollback the CLI may be ahead — don't prompt a downgrade."""
    from cli.update_check import UpdateInfo
    info = UpdateInfo(installed="2.1.0", latest="2.0.0", download_url="…")
    assert info.is_outdated() is False


def test_is_outdated_uses_pep440_comparison(tmp_config):
    """`10.0.0 > 2.1.0` — must not be tripped by lexicographic string compare."""
    from cli.update_check import UpdateInfo
    newer_on_server = UpdateInfo(installed="2.1.0", latest="10.0.0", download_url="…")
    older_on_server = UpdateInfo(installed="10.0.0", latest="2.1.0", download_url="…")
    assert newer_on_server.is_outdated() is True
    assert older_on_server.is_outdated() is False


def test_is_outdated_false_for_unparseable_strings(tmp_config):
    """Unparseable versions default to False — we'd rather miss an upgrade
    hint than suggest a bogus downgrade."""
    from cli.update_check import UpdateInfo
    info = UpdateInfo(installed="nightly-abc", latest="nightly-def", download_url="…")
    assert info.is_outdated() is False


def test_format_outdated_notice_drops_upgrade_line_when_no_download_url(tmp_config):
    """`download_url=None` must NOT produce literal "None" — and must never
    leak a version-pinned `/cli/wheel/` URL that 404s after a server upgrade."""
    from cli.update_check import UpdateInfo, format_outdated_notice
    info = UpdateInfo(installed="2.0.0", latest="2.1.0", download_url=None)
    msg = format_outdated_notice(info)
    assert "None" not in msg
    assert "uv tool install" not in msg
    assert "/cli/wheel/" not in msg
    assert "2.0.0" in msg and "2.1.0" in msg


def test_format_outdated_notice_recommends_self_upgrade_when_url_present(tmp_config):
    """Even with a populated download_url, the banner must recommend
    `agnes self-upgrade` and NOT emit the version-pinned wheel URL — that
    URL 404s after a server upgrade (issue #521)."""
    from cli.update_check import UpdateInfo, format_outdated_notice
    download_url = "http://s/cli/wheel/a-2.1.0-py3-none-any.whl"
    info = UpdateInfo(installed="2.0.0", latest="2.1.0", download_url=download_url)
    msg = format_outdated_notice(info)
    assert "agnes self-upgrade" in msg
    assert "uv tool install" not in msg
    assert "/cli/wheel/" not in msg
    assert download_url not in msg


@pytest.mark.parametrize(
    "download_url",
    ["http://s/cli/wheel/a-2.1.0-py3-none-any.whl", None],
)
def test_format_outdated_notice_recommends_self_upgrade_regardless_of_url(
    tmp_config, download_url
):
    """The upgrade recommendation is URL-independent: it always points at
    `agnes self-upgrade`, never a pinned/None wheel URL."""
    from cli.update_check import UpdateInfo, format_outdated_notice
    info = UpdateInfo(installed="2.0.0", latest="2.1.0", download_url=download_url)
    msg = format_outdated_notice(info)
    assert "agnes self-upgrade" in msg
    assert "is out of date" in msg
    assert "/cli/wheel/" not in msg
    assert "None" not in msg


def test_format_outdated_notice_reports_both_versions(tmp_config):
    """The informational version reporting is preserved after the wording
    change — both the installed and the server's latest version appear."""
    from cli.update_check import UpdateInfo, format_outdated_notice
    info = UpdateInfo(
        installed="2.0.0",
        latest="2.1.0",
        download_url="http://s/cli/wheel/a-2.1.0-py3-none-any.whl",
    )
    msg = format_outdated_notice(info)
    assert info.installed in msg
    assert info.latest in msg


class TestRootCallbackIntegration:
    """The root callback must not crash a command when the probe fails, and
    on version drift must kick off a detached background `agnes update`
    (no interactive prompt, no banner)."""

    def test_probe_failure_does_not_break_command(self, tmp_config):
        with patch("cli.update_check.check", side_effect=RuntimeError("boom")):
            result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0

    def _info(self):
        from cli.update_check import UpdateInfo
        return UpdateInfo(
            installed="2.0.0",
            latest="2.1.0",
            download_url="http://server.test:8000/cli/wheel/x.whl",
        )

    def test_outdated_spawns_background_update(self, tmp_config, monkeypatch):
        """On drift, `_maybe_warn_outdated` spawns a detached `agnes update`
        exactly once with the latest version — no prompt, no banner. The
        spawn helper is patched so the test never starts a real process."""
        from cli.main import _maybe_warn_outdated

        monkeypatch.delenv("AGNES_NO_UPDATE_CHECK", raising=False)
        monkeypatch.setattr("sys.argv", ["agnes", "catalog"])  # ordinary command
        with patch("cli.update_check.check", return_value=self._info()), \
             patch("cli.main._spawn_background_update") as spawn:
            _maybe_warn_outdated()
        spawn.assert_called_once_with("2.1.0")

    def test_no_spawn_for_maintenance_command(self, tmp_config, monkeypatch):
        """The root callback fires BEFORE subcommand dispatch, so it must not
        spawn an update when the command itself is update-family — otherwise
        `agnes update` would recursively spawn itself."""
        from cli.main import _maybe_warn_outdated

        monkeypatch.delenv("AGNES_NO_UPDATE_CHECK", raising=False)
        monkeypatch.setattr("sys.argv", ["agnes", "update", "--quiet"])
        with patch("cli.update_check.check", return_value=self._info()), \
             patch("cli.main._spawn_background_update") as spawn:
            _maybe_warn_outdated()
        spawn.assert_not_called()

    def test_no_spawn_when_update_check_disabled(self, tmp_config, monkeypatch):
        """Inside an in-progress update tree (`AGNES_NO_UPDATE_CHECK=1`) the
        root callback must not spawn another update."""
        from cli.main import _maybe_warn_outdated

        monkeypatch.setenv("AGNES_NO_UPDATE_CHECK", "1")
        monkeypatch.setattr("sys.argv", ["agnes", "catalog"])
        with patch("cli.update_check.check", return_value=self._info()), \
             patch("cli.main._spawn_background_update") as spawn:
            _maybe_warn_outdated()
        spawn.assert_not_called()

    def test_spawn_dedupes_per_version(self, tmp_config):
        """The per-version marker limits spawns to one per distinct server
        version. A freshly-installed binary only goes current NEXT session, so
        `is_outdated()` stays true all session — without this guard every
        command would spawn a detached update (process storm). Popen is patched
        so no real process starts; the assertion is on the spawn count + the
        marker the function persists across calls."""
        from cli.main import _spawn_background_update

        with patch("subprocess.Popen") as popen:
            _spawn_background_update("2.1.0")
            _spawn_background_update("2.1.0")  # same version → no second spawn
            assert popen.call_count == 1
            _spawn_background_update("2.2.0")  # new version → spawns again
            assert popen.call_count == 2

    def test_spawn_background_update_child_contract(self, tmp_config):
        """The detached child is the non-recursive `agnes update --quiet`:
        argv ends with `update --quiet`, env carries AGNES_NO_UPDATE_CHECK=1 so
        it can't re-trigger detection, and stdio is fully detached. Popen is
        patched so no real process starts."""
        import subprocess

        from cli.main import _spawn_background_update

        with patch("subprocess.Popen") as popen:
            _spawn_background_update("3.0.0")

        assert popen.call_count == 1
        argv = popen.call_args.args[0]
        kwargs = popen.call_args.kwargs
        assert argv[-2:] == ["update", "--quiet"]
        assert kwargs["env"]["AGNES_NO_UPDATE_CHECK"] == "1"
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.DEVNULL
