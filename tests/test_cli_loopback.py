"""Integration tests for the CLI loopback listener (cli/lib/loopback.py).

These exercise the real ephemeral-port HTTP server by faking
``webbrowser.open`` to fire the callback the way a browser redirect would.
"""

import threading
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from cli.lib import loopback


def _make_fake_open(*, code="abc123", state_override=None, delay=0.1):
    """Return a fake webbrowser.open that fires the loopback callback."""

    def fake_open(url):
        q = parse_qs(urlparse(url).query)
        port = int(q["port"][0])
        state = state_override if state_override is not None else q["state"][0]

        def hit():
            params = {"state": state}
            if code is not None:
                params["code"] = code
            try:
                httpx.get(f"http://127.0.0.1:{port}/callback", params=params, timeout=5)
            except Exception:
                pass

        threading.Timer(delay, hit).start()
        return True

    return fake_open


def test_captures_code_from_callback(monkeypatch):
    monkeypatch.setattr(loopback.webbrowser, "open", _make_fake_open(code="abc123"))
    code = loopback.capture_code_via_browser("http://server.test", timeout=5)
    assert code == "abc123"


def test_state_mismatch_raises(monkeypatch):
    monkeypatch.setattr(
        loopback.webbrowser,
        "open",
        _make_fake_open(code="abc123", state_override="WRONG-STATE"),
    )
    with pytest.raises(RuntimeError, match="state mismatch"):
        loopback.capture_code_via_browser("http://server.test", timeout=5)


def test_missing_code_raises(monkeypatch):
    monkeypatch.setattr(loopback.webbrowser, "open", _make_fake_open(code=None))
    with pytest.raises(RuntimeError):
        loopback.capture_code_via_browser("http://server.test", timeout=5)


def test_timeout_raises(monkeypatch):
    # Browser "opens" but never calls back.
    monkeypatch.setattr(loopback.webbrowser, "open", lambda url: True)
    with pytest.raises(TimeoutError):
        loopback.capture_code_via_browser("http://server.test", timeout=0.5)


def test_url_is_printed_even_when_open_claims_success(monkeypatch, capsys):
    """`webbrowser.open()` returning True is not evidence a browser appeared.

    The macOS backend returns `not rc` from the `osascript` pipe, so it
    reports True whenever osascript merely dispatched. Gating the printed URL
    on that value hid it in precisely the case where the user needs it — no
    browser on screen and a silent wait to the timeout. The URL must be on
    screen for every run, and it must be the complete one: the bare
    `/cli/auth/start` cannot finish the flow because the callback needs the
    loopback `port` and the `state`.
    """
    monkeypatch.setattr(loopback.webbrowser, "open", lambda url: True)
    with pytest.raises(TimeoutError):
        loopback.capture_code_via_browser("http://server.test", timeout=0.5)

    out = capsys.readouterr().out
    assert "Open this URL in your browser to continue:" in out
    printed = [ln.strip() for ln in out.splitlines() if "/cli/auth/start" in ln]
    assert printed, f"the sign-in URL was never printed; got:\n{out}"
    q = parse_qs(urlparse(printed[0]).query)
    assert q.get("port") and q["port"][0].isdigit()
    assert q.get("state")
    # A browser that reported success must not also claim it failed to launch.
    assert "could not launch a browser automatically" not in out


def test_no_browser_mode_still_prints_the_url(monkeypatch, capsys):
    """`--no-browser` never calls `webbrowser.open`, and the URL is the whole
    point of that mode."""

    def _must_not_open(url):  # pragma: no cover - must never be called
        raise AssertionError("open_browser=False must not launch a browser")

    monkeypatch.setattr(loopback.webbrowser, "open", _must_not_open)
    with pytest.raises(TimeoutError):
        loopback.capture_code_via_browser("http://server.test", open_browser=False, timeout=0.5)

    out = capsys.readouterr().out
    assert "/cli/auth/start" in out
    # Nothing tried to launch, so the launch-failure hint would be misleading.
    assert "could not launch a browser automatically" not in out
