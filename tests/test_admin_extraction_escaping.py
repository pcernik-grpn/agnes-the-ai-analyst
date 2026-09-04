"""`esc()` in admin_extraction.js must be safe in ATTRIBUTE position, not just text.

Node-executed against the SHIPPED function (no copy), the pattern
tests/test_chat_facts_rendering_ui.py established — which is why `esc()` is
written as a pure regex rather than the `textContent` -> `innerHTML` round-trip
it used to be: that form needs a DOM this repo has no jsdom for, and it escapes
`&`, `<` and `>` only.

The gap that motivated this: three of the file's call sites interpolate into a
double-quoted attribute (`title="${esc(...)}"` in `shardRowHtml` and `renderRow`,
`id="ext-drawer-${esc(...)}"` in `openFleetCompleteness`). An unescaped `"`
there closes the attribute early and everything after it is parsed as further
ATTRIBUTES — so `" onmouseover="alert(1)` injects an inline handler without ever
needing a `<`, which the old helper did escape. The dashboard CSP does not block
inline handlers (see .claude/skills/agnes-conventions/references/security.md §3).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

EXT_JS = Path("app/web/static/js/admin/admin_extraction.js")


def _read() -> str:
    return EXT_JS.read_text(encoding="utf-8")


def _node_run(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _esc_source(js: str) -> str:
    """The shipped `ESC_ENTITIES` + `esc` pair, verbatim."""
    return js[js.index("const ESC_ENTITIES") : js.index("/* A FastAPI `detail`")]


def _parse_one_tag(html: str, tag: str, *, cls: str) -> dict[str, str]:
    """Attributes of the single `<tag class="...cls...">` in `html`, as a real
    HTML parser sees them — the only reading that answers "did this break out
    of its attribute", since that is a parser-level question."""

    found: list[dict[str, str]] = []

    class _P(HTMLParser):
        def handle_starttag(self, name, attrs):
            d = {k: (v or "") for k, v in attrs}
            if name == tag and cls in d.get("class", "").split():
                found.append(d)

    _P().feed(html)
    assert len(found) == 1, f"expected exactly one <{tag} class~={cls}>, got {len(found)}"
    return found[0]


# ── the helper itself ────────────────────────────────────────────────────────


def test_esc_escapes_both_quote_forms():
    """A double quote is the attribute break-out; a single quote is the same
    bug for any site that switches to single-quoted attributes later."""
    cases = [
        'Q4 "Special" Reports',
        "Rodney's connection",
        '" onmouseover="alert(1)',
        "<script>alert(1)</script>",
        "A & B",
        None,
    ]
    script = _esc_source(_read()) + (
        f"\nprocess.stdout.write(JSON.stringify({json.dumps(cases)}.map(esc)));\n"
    )
    got = json.loads(_node_run(script))

    assert got[0] == "Q4 &quot;Special&quot; Reports"
    assert got[1] == "Rodney&#39;s connection"
    assert got[2] == "&quot; onmouseover=&quot;alert(1)"
    assert got[3] == "&lt;script&gt;alert(1)&lt;/script&gt;"
    assert got[4] == "A &amp; B", "& must still escape, and exactly once"
    assert got[5] == "", "null/undefined stays the empty string, as before"

    for out in got:
        assert '"' not in out and "'" not in out and "<" not in out and ">" not in out


def test_esc_does_not_double_escape_its_own_output():
    """`&amp;` must not become `&amp;amp;` on a value that already contains an
    entity — the replace runs once over the raw string, never over its output."""
    script = _esc_source(_read()) + (
        '\nprocess.stdout.write(JSON.stringify(esc("&amp;")));\n'
    )
    assert json.loads(_node_run(script)) == "&amp;amp;", (
        "escaping a literal `&amp;` yields `&amp;amp;` — correct, and what "
        "proves the pass is single, not recursive"
    )


# ── behaviour at the attribute call site ─────────────────────────────────────


def test_shard_error_cannot_break_out_of_its_title_attribute():
    """The real defect, end to end: `shard.error` is crawl failure text (it
    quotes the Graph path or response that failed), rendered into
    `title="…"`. Feed it an attribute break-out and assert the payload stays
    inside the quoted value."""
    js = _read()
    src = (
        _esc_source(js)
        + js[js.index("function fmtAgo") : js.index("function fmtRate")]
        + js[js.index("function _shardCheckpointAgeS") : js.index("function renderShardDisclosureRow")]
    )
    shard = {
        "label": 'shard "0"',
        "outcome": "failed",
        "index": 0,
        "expected": 10,
        "files_done": 1,
        "files_seen": 2,
        "checkpoint_at": None,
        "error": '" onmouseover="alert(1)" x="',
    }
    script = src + (
        f"\nprocess.stdout.write(JSON.stringify(shardRowHtml({json.dumps(shard)})));\n"
    )
    html = json.loads(_node_run(script))

    assert 'title="&quot; onmouseover=&quot;alert(1)&quot; x=&quot;"' in html

    # Parsed the way a browser parses it: the error <span> must carry exactly
    # the two attributes the template writes, with the payload intact as the
    # VALUE of `title` — not split into an injected event handler.
    span = _parse_one_tag(html, "span", cls="ext-error-cell")
    assert set(span) == {"class", "title"}, f"attribute injected: {sorted(span)}"
    assert span["title"] == shard["error"]

    # The label lands in text position; escaped there too, harmlessly.
    assert "shard &quot;0&quot;" in html
    # And the tag structure is intact: exactly the six cells the row declares.
    assert html.count("<td") == 6


# ── static guards: no future call site may skip the helper ───────────────────

_ATTR_INTERP = re.compile(r'(?P<name>[\w-]+)\s*=\s*"[^"\n]*?\$\{(?P<expr>[^}]*)\}')
# A ternary whose every branch is a string literal is choosing a CSS class from
# literals written in this file — structure, not data, and nothing to escape.
_LITERAL_TERNARY = re.compile(r'^[^"\']*\?\s*"[^"]*"\s*:\s*"[^"]*"\s*$')


def _strip_comments(js: str) -> str:
    """Blank out `//` and `/* */` comments, preserving offsets (and so line
    numbers). Needed because this file DOCUMENTS the very attribute shapes the
    guards below look for — a prose example must not read as a call site.

    Template literals are deliberately NOT skipped: the attributes under audit
    live inside them. Quoted strings are, so a `//` in one cannot blank real
    code — but only to end of line, which JS requires anyway and which keeps an
    unpaired quote inside a regex literal (`/[&<>"\']/g`, in `esc` itself) from
    running away over the rest of the file.
    """
    out = list(js)
    i, n = 0, len(js)
    while i < n:
        c = js[i]
        if c in "\"'":
            quote, j = c, i + 1
            while j < n and js[j] != quote and js[j] != "\n":
                j += 2 if js[j] == "\\" else 1
            i = j + 1 if j < n and js[j] == quote else i + 1
        elif js.startswith("//", i):
            while i < n and js[i] != "\n":
                out[i] = " "
                i += 1
        elif js.startswith("/*", i):
            while i < n and not js.startswith("*/", i):
                if js[i] != "\n":
                    out[i] = " "
                i += 1
            for j in range(i, min(i + 2, n)):
                out[j] = " "
            i += 2
        else:
            i += 1
    return "".join(out)


def _attr_interpolations() -> list[tuple[int, str, str, str]]:
    js = _read()
    scannable = _strip_comments(js).splitlines()
    raw = js.splitlines()
    out = []
    for lineno, line in enumerate(scannable, start=1):
        for m in _ATTR_INTERP.finditer(line):
            out.append((lineno, m.group("name"), m.group("expr"), raw[lineno - 1].strip()[:100]))
    return out


def test_comment_stripper_hides_prose_but_keeps_code():
    """The stripper is load-bearing for both guards, so pin it directly."""
    js = (
        'a = `x=\"${v}\"`; // onclick="f(\'${id}\')"\n'
        # an unpaired quote inside a regex literal must not run away
        'const r = /[&<>\"\']/g;\n'
        '/* title="${p}" */ b = `y="${w}"`;'
    )
    got = _strip_comments(js)
    assert "${v}" in got and "${w}" in got, "template-literal code must survive"
    assert "${id}" not in got and "${p}" not in got, "comment prose must be blanked"
    assert len(got) == len(js) and got.count("\n") == js.count("\n")


# A bare identifier in attribute position is allowed only when it is bound to a
# CSS class picked from string literals written in this file — structure, not
# data. `_assert_literal_only_binding` re-proves that on every run, so the
# exemption cannot rot into covering a binding that later reads from the API.
_LITERAL_CLASS_BINDINGS = {"cls"}

_STRING_LITERAL = re.compile(r'"[^"\n]*"|\'[^\'\n]*\'')
# `cond ? L : cond ? L : … : L`, every result branch a literal.
_LITERAL_TERNARY_CHAIN = re.compile(r"^[^?`]*\?\s*L\s*(?::[^?`]*\?\s*L\s*)*:\s*L$")


def _assert_literal_only_binding(name: str) -> None:
    js = _read()
    bindings = []
    needle = f"const {name} = "
    at = js.find(needle)
    while at != -1:
        end = js.index(";", at)
        bindings.append(" ".join(js[at + len(needle) : end].split()))
        at = js.find(needle, end)
    assert bindings, f"no `const {name} =` binding found — exemption is stale"
    for rhs in bindings:
        skeleton = _STRING_LITERAL.sub("L", rhs)
        assert _LITERAL_TERNARY_CHAIN.match(skeleton), (
            f"`{name}` is exempt from esc() only while every branch is a string "
            f"literal; this binding is not: {rhs}"
        )


def test_literal_class_exemptions_are_still_literal():
    for name in _LITERAL_CLASS_BINDINGS:
        _assert_literal_only_binding(name)


def test_every_attribute_interpolation_routes_through_esc():
    """Pins the audit that motivated this change: a value interpolated into a
    quoted HTML attribute in this file goes through `esc()`. Escaping the
    helper is only half the fix if the next attribute added skips it."""
    offenders = [
        f"{EXT_JS}:{lineno}: {ctx}"
        for lineno, name, expr, ctx in _attr_interpolations()
        if not name.startswith("on")  # inline handlers: separate context, below
        and "esc(" not in expr
        and not _LITERAL_TERNARY.match(expr.strip())
        and expr.strip() not in _LITERAL_CLASS_BINDINGS
    ]
    assert not offenders, (
        "unescaped value interpolated into a quoted HTML attribute — wrap it in "
        "esc(), which escapes both quote forms:\n  " + "\n  ".join(offenders)
    )


# The six `onclick="fn('${id}')"` sites are a JS-string-inside-an-attribute
# context, and `esc()` is deliberately NOT applied there: the browser
# HTML-decodes an attribute value BEFORE parsing it as JS, so `&#39;` becomes a
# real `'` and closes the JS string just as a bare one would — escaping there
# would look like a fix while being none. What actually holds is the value: every
# id interpolated into a handler is server-generated (`str(uuid4())` for a
# connection, `"er_" + secrets.token_hex(8)` for a run), so no quote can occur.
# That is an argument about the DATA, so pin the data: a handler that starts
# interpolating something else — a connection NAME, say — must fail here and be
# rewritten onto `data-` attributes + delegation rather than wrapped in `esc()`.
_HANDLER_SAFE_EXPRS = {
    "row.connection_id",  # str(uuid4())
    "connId",             # ditto, passed through
    "run.id",             # "er_" + secrets.token_hex(8)
}


def test_inline_handlers_interpolate_only_server_generated_ids():
    offenders = [
        f"{EXT_JS}:{lineno}: {name}=... ${{{expr}}}"
        for lineno, name, expr, _ctx in _attr_interpolations()
        if name.startswith("on") and expr.strip() not in _HANDLER_SAFE_EXPRS
    ]
    assert not offenders, (
        "a new value reaches an inline event handler. `esc()` does NOT make this "
        "safe (the attribute is HTML-decoded before the JS is parsed). Move the "
        "value to a `data-` attribute read by a delegated listener:\n  "
        + "\n  ".join(offenders)
    )
