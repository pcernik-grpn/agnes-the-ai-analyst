"""Localhost loopback listener for the browser-based `agnes auth login`.

Implements the CLI half of the gh-style flow: bind an ephemeral port on
127.0.0.1, open the browser to the server's ``/cli/auth/start`` page, and
block until the server redirects the freshly-minted exchange code back to
``http://127.0.0.1:<port>/callback?code=...&state=...``.

The ``state`` is generated here and verified on the callback — it binds the
browser redirect to this exact CLI invocation, so a stray or forged callback
to the loopback port can't smuggle in a foreign code.
"""

import http.server
import secrets
import threading
import webbrowser
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse

# Browser shown when the code is captured / something goes wrong. Kept inline
# (no template engine in the CLI) and deliberately tiny.
_SUCCESS_HTML = (
    b"<!doctype html><meta charset=utf-8>"
    b"<title>Agnes CLI</title>"
    b"<body style='font-family:system-ui;max-width:32rem;margin:4rem auto;text-align:center'>"
    b"<h2>You're signed in.</h2>"
    b"<p>The Agnes CLI captured your token. You can close this tab and return "
    b"to the terminal.</p></body>"
)
_ERROR_HTML = (
    b"<!doctype html><meta charset=utf-8>"
    b"<title>Agnes CLI</title>"
    b"<body style='font-family:system-ui;max-width:32rem;margin:4rem auto;text-align:center'>"
    b"<h2>Something went wrong.</h2>"
    b"<p>Return to the terminal for details.</p></body>"
)


@dataclass
class LoopbackResult:
    code: Optional[str] = None
    error: Optional[str] = None


def capture_code_via_browser(
    server_url: str,
    *,
    open_browser: bool = True,
    timeout: float = 180.0,
) -> str:
    """Open the browser, wait for the loopback callback, return the code.

    Raises ``TimeoutError`` if no callback arrives within ``timeout`` seconds,
    or ``RuntimeError`` on a state mismatch / server-reported error.
    """
    state = secrets.token_urlsafe(24)
    result = LoopbackResult()
    done = threading.Event()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence default stderr access log
            pass

        def _respond(self, status: int, body: bytes):
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 (stdlib casing)
            parsed = urlparse(self.path)
            if parsed.path != "/callback":
                self._respond(204, b"")
                return
            params = parse_qs(parsed.query)
            got_state = (params.get("state") or [""])[0]
            code = (params.get("code") or [""])[0]
            if got_state != state:
                result.error = "state mismatch — ignoring callback"
                self._respond(400, _ERROR_HTML)
                done.set()
                return
            if not code:
                result.error = "callback missing code"
                self._respond(400, _ERROR_HTML)
                done.set()
                return
            result.code = code
            self._respond(200, _SUCCESS_HTML)
            done.set()

    # Port 0 → OS assigns a free ephemeral port. Bind to loopback only.
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = httpd.server_address[1]
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    start_url = server_url.rstrip("/") + "/cli/auth/start?" + urlencode({"port": port, "state": state})
    try:
        opened = webbrowser.open(start_url) if open_browser else False
        # ALWAYS print the URL, whether or not the auto-open reported success.
        # `webbrowser.open()`'s return value is not evidence that a browser
        # actually surfaced: the macOS backend returns `not rc` from the
        # `osascript` pipe, so it says True whenever osascript merely
        # dispatched, and the Unix backends return True on a zero exit from a
        # launcher that may have gone to a text browser or nowhere at all.
        # Gating the print on it withheld the one thing the user needs in
        # exactly the case where the browser did not come up — leaving a
        # silent wait until the timeout with no URL on screen to click.
        # `start_url` carries the loopback `port` and `state`, so it is the
        # only form that can complete the flow; printing it costs one line
        # when the browser did open.
        print(f"Open this URL in your browser to continue:\n  {start_url}")
        if not opened and open_browser:
            print("(could not launch a browser automatically)")
        if not done.wait(timeout=timeout):
            raise TimeoutError(f"timed out after {int(timeout)}s waiting for browser sign-in")
    finally:
        httpd.shutdown()
        httpd.server_close()

    if result.error:
        raise RuntimeError(result.error)
    if not result.code:
        raise RuntimeError("no code captured")
    return result.code
