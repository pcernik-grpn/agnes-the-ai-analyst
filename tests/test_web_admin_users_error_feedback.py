"""How /admin/users reports a refusal, and how modal copy wraps.

Two defects that share a shape — a detail the page HAD and failed to present:

  * #1172 — five handlers interpolated the raw response body into the toast,
    so a duplicate email surfaced as
    `Failed: {"detail":"User with this email already exists"}`. The message was
    right there in `detail`; nothing read it.
  * #1171 — `white-space: pre-wrap` was applied to every `.modal-card` heading
    and sub-line to preserve `\\n` in helper-supplied messages. Unscoped, it
    also hit STATIC template copy, where newlines and indentation are source
    formatting — so a helper paragraph written across three template lines
    rendered as three jaggedly-indented lines.

Layer 1 asserts the wiring against the rendered page and the shipped assets;
layer 2 executes `errorText` under node against the response shapes it has to
survive, since that is where "shows the message, never the envelope" actually
lives.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATE = _ROOT / "app" / "web" / "templates" / "admin_users.html"


@pytest.fixture
def web_client(tmp_path, monkeypatch, shared_app):
    """Same shape as the one in ``test_web_ui`` — declared here rather than
    imported, because importing a fixture by name trips ruff's F811 on every
    test that takes it as a parameter."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    (tmp_path / "state").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "extracts").mkdir()
    from src.db import close_system_db

    close_system_db()

    app = shared_app
    yield TestClient(app)
    close_system_db()


@pytest.fixture
def admin_cookie(web_client):
    from argon2 import PasswordHasher

    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from tests.helpers.auth import grant_admin

    password = "AdminPass1!"
    conn = get_system_db()
    UserRepository(conn).create(
        id="admin1",
        email="admin@test.com",
        name="Admin",
        password_hash=PasswordHasher().hash(password),
    )
    grant_admin(conn, "admin1")
    conn.close()
    resp = web_client.post("/auth/token", json={"email": "admin@test.com", "password": password})
    assert resp.status_code == 200, f"Bootstrap failed: {resp.text}"
    return {"access_token": resp.json()["access_token"]}


# ─────────────────────────────── 1. contract ────────────────────────────────


def test_no_handler_dumps_the_raw_response_body(web_client, admin_cookie):
    """The defect itself: `toast("Failed: " + await r.text())`.

    Asserted on the rendered page rather than the file so a re-introduction
    through an include is caught too.
    """
    html = web_client.get("/admin/users", cookies=admin_cookie).text
    assert "r.text()" not in html, "a handler is dumping the raw response body into a toast again"


def test_every_failure_path_routes_through_the_shared_helper(web_client, admin_cookie):
    """Six call sites, one helper. Counting them is the point — the original
    bug was that four siblings had been fixed and the fifth (create-user, the
    one users actually hit) had not. The sixth is the invite drawer's inline
    *new group*; raise this number when a path is added, never delete the
    assertion."""
    html = web_client.get("/admin/users", cookies=admin_cookie).text
    assert html.count("await errorText(r)") == 6


def test_modal_newline_preservation_is_opt_in(web_client):
    """The rule must name an opt-in, not every `.modal-card` on the instance."""
    css = web_client.get("/static/style-custom.css").text
    assert ".modal-card h3, .modal-card p.sub { white-space: pre-wrap; }" not in css, (
        "the unscoped pre-wrap rule is back — it reformats static modal copy (#1171)"
    )
    assert ".modal-card[data-preserve-newlines] h3," in css
    assert ".modal-card[data-preserve-newlines] p.sub { white-space: pre-wrap; }" in css


def test_the_dialogs_that_need_newlines_opt_in(web_client):
    """`alertModal` / `confirmModal` / `promptModal` replace the native dialogs,
    which DID render `\\n` as line breaks — they are the reason the rule exists,
    so they must carry the attribute that now gates it."""
    js = web_client.get("/static/js/modal.js").text
    assert "card.dataset.preserveNewlines = '1';" in js


def test_static_modal_copy_no_longer_depends_on_source_wrapping(web_client, admin_cookie):
    """The invite flow's helper paragraph is still written across several
    template lines — which is fine now, and was the whole bug before.

    Anchored on the group step's lede: the paragraph this used to check
    ("New users start with no group memberships — assign them on the user
    detail page") was the copy the invite drawer removed by MAKING the
    membership step part of the flow.
    """
    html = web_client.get("/admin/users", cookies=admin_cookie).text
    assert "Groups are the only way anything reaches a" in html
    assert "data-preserve-newlines" not in html, (
        "a static admin modal opted into newline preservation — its line breaks are source formatting, not content"
    )


# ───────────────────────────── 2. executable ─────────────────────────────────


def _extract_error_text() -> str:
    """Lift `errorText`'s source out of the page's inline script."""
    src = _TEMPLATE.read_text(encoding="utf-8")
    m = re.search(r"^async function errorText\(r\) \{", src, re.M)
    assert m, "errorText declaration not found"
    depth, j, started = 0, m.start(), False
    while j < len(src):
        if src[j] == "{":
            depth += 1
            started = True
        elif src[j] == "}":
            depth -= 1
            if started and depth == 0:
                j += 1
                break
        j += 1
    return src[m.start() : j]


def _run_error_text(body, status: int = 400) -> str:
    """Execute `errorText` against a response whose `.json()` yields `body`.

    `body` of `None` stands for a body that is not JSON at all — the tool
    rejects it by making `.json()` reject, exactly as `fetch` does.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available — the error-mapping test needs a runtime")
    resolve = "Promise.reject(new Error('not json'))" if body is None else "Promise.resolve(%s)" % json.dumps(body)
    script = (
        _extract_error_text()
        + "\nvar r = { status: %d, json: function () { return %s; } };\n" % (status, resolve)
        + "errorText(r).then(function (v) { process.stdout.write(String(v)); });\n"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, "node failed:\n%s" % out.stderr
    return out.stdout


def test_string_detail_is_returned_as_the_sentence_it_is():
    """The reported case, end to end: the toast says the message, not the
    envelope it arrived in."""
    got = _run_error_text({"detail": "User with this email already exists"}, status=409)
    assert got == "User with this email already exists"
    assert "{" not in got and "detail" not in got


def test_structured_detail_prefers_the_human_half():
    """Other routers answer with `{code, hint}`. A `code` is a lookup key; the
    `hint` is the sentence written for the person reading the toast."""
    got = _run_error_text(
        {"detail": {"code": "quota_exceeded", "hint": "Fix the previous upload errors before retrying."}},
        status=429,
    )
    assert got == "Fix the previous upload errors before retrying."


def test_structured_detail_falls_back_to_the_code():
    """A `{code}` with no prose still beats a bare number."""
    assert _run_error_text({"detail": {"code": "conflict_owner_name"}}, status=409) == "conflict_owner_name"


def test_non_json_body_falls_back_to_the_status():
    """A proxy's HTML 502 has no `detail` to read. The status is a poor message
    but a true one — the raw-body version pasted a page of HTML into a toast."""
    assert _run_error_text(None, status=502) == "502"


def test_empty_detail_falls_back_to_the_status():
    """A blank string is not a message; treating it as one produced a toast
    reading `Failed: ` with nothing after it."""
    assert _run_error_text({"detail": "   "}, status=500) == "500"


def test_body_without_detail_falls_back_to_the_status():
    assert _run_error_text({"error": "nope"}, status=418) == "418"
