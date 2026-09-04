"""Every `esc()` in the web layer must be safe in ATTRIBUTE position.

The bug this pins, found on the extraction-fleet page and then present in ten
more templates: `esc()` written as a `textContent` -> `innerHTML` round-trip
escapes `&`, `<` and `>` — what a TEXT node needs — but not `"`. Half of these
pages interpolate into a double-quoted attribute (`title="${esc(...)}"`), where
a bare `"` closes the value early and everything after it is parsed as further
ATTRIBUTES. So `" onmouseover="alert(1)` injects a live inline handler without
ever needing a `<`, which the old helper did escape, and the dashboard CSP does
not block inline handlers (see
`.claude/skills/agnes-conventions/references/security.md`).

It is reachable with data the server does not author: a tool name from a
third-party MCP server, a token name any user picks, a group name from the
Google/Entra sync, git's stderr from a failed clone.

Two guards, deliberately separate:

* `test_every_esc_helper_escapes_both_quote_forms` is the STATIC one — it finds
  every `esc()` in the tree and refuses one that cannot produce `&quot;` and
  `&#39;`. It is what stops a new page copying the old shape.
* `test_esc_helpers_actually_escape_a_break_out` EXECUTES each helper under
  node against a real break-out payload, so a helper that merely mentions the
  entities in a comment does not pass.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOTS = (Path("app/web/static/js"), Path("app/web/templates"))


def _esc_bodies() -> list[tuple[Path, str]]:
    """Every `function esc(...) { ... }` in the web layer, brace-matched."""
    out: list[tuple[Path, str]] = []
    for root in ROOTS:
        for path in sorted(root.rglob("*")):
            if path.suffix not in {".js", ".html"} or not path.is_file():
                continue
            src = path.read_text(encoding="utf-8")
            at = src.find("function esc(")
            while at != -1:
                depth, i = 0, src.index("{", at)
                j = i
                while j < len(src):
                    if src[j] == "{":
                        depth += 1
                    elif src[j] == "}":
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                out.append((path, src[at : j + 1]))
                at = src.find("function esc(", j)
    return out


def _strip_comments(js: str) -> str:
    """Blank `//` and `/* */` so a helper cannot pass on prose alone — several
    of these bodies document the very entities the guard looks for."""
    out, i, n = [], 0, len(js)
    while i < n:
        if js.startswith("//", i):
            while i < n and js[i] != "\n":
                i += 1
        elif js.startswith("/*", i):
            i = js.find("*/", i)
            i = n if i == -1 else i + 2
        else:
            out.append(js[i])
            i += 1
    return "".join(out)


def test_esc_helpers_exist_where_expected():
    """A canary: if this drops to zero the discovery above silently stopped
    guarding anything."""
    assert len(_esc_bodies()) >= 20


# Three legitimate shapes a correct helper can take, beyond escaping inline:
#   * `return window.BuilderShell.esc(s)`      — cross-module delegation
#   * `return escapeHtml(s).replace(/'/g, …)`  — wrapping a base escaper
#   * `.replace(/[&<>"']/g, c => ENTITIES[c])` — entity table outside the body
# All three put the entity strings somewhere other than between the braces, so
# the rule is "self-covering, or covering somewhere this helper actually reaches"
# rather than "the entities appear in the body".
_DELEGATES_IN_FILE = re.compile(r"\b(?:escapeHtml|ENTITIES|ESC_ENTITIES)\b")
_DELEGATES_CROSS_MODULE = re.compile(r"return\s+(?:window\.)?[A-Z]\w*\.esc\s*\(")


def _quote_covering(code: str) -> bool:
    dq = "&quot;" in code or "&#34;" in code
    sq = "&#39;" in code or "&apos;" in code
    return dq and sq


@pytest.mark.parametrize(
    "path,body", _esc_bodies(), ids=lambda v: str(v) if isinstance(v, Path) else ""
)
def test_every_esc_helper_escapes_both_quote_forms(path: Path, body: str):
    """The rule is stated as the bug's own shape, so it neither over- nor
    under-fires. A helper is fine when it escapes both quote forms itself, or
    when what it delegates to does — several correct helpers keep their entity
    table or base escaper outside the function body, and demanding the
    entities between the braces would fail them for being factored well.
    """
    code = _strip_comments(body)
    if _quote_covering(code):
        return

    if _DELEGATES_CROSS_MODULE.search(code):
        # The target is a shared component that this same parametrized test
        # covers in its own file, so it cannot be the unsafe shape unnoticed.
        return

    if _DELEGATES_IN_FILE.search(code):
        whole = _strip_comments(path.read_text(encoding="utf-8"))
        assert _quote_covering(whole), (
            f"{path}: esc() delegates within this file, but nothing here "
            "escapes both quote forms — the delegation target is unsafe too."
        )
        return

    assert False, (
        f"{path}: esc() escapes neither quote form itself nor delegates to "
        'something that does. In attribute position a bare `"` is an '
        "attribute break-out — write the five-character form:\n"
        '  String(s == null ? "" : s).replace(/&/g,"&amp;")…'
        '.replace(/"/g,"&quot;").replace(/\'/g,"&#39;")'
    )


def _node(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    r = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


_DOMLESS = re.compile(r"document\s*\.\s*createElement")

_PAYLOAD = '" onmouseover="alert(1)'


@pytest.mark.parametrize(
    "path,body", _esc_bodies(), ids=lambda v: str(v) if isinstance(v, Path) else ""
)
def test_esc_helpers_actually_escape_a_break_out(path: Path, body: str):
    """Run the shipped helper against a real break-out payload, so a helper
    that only MENTIONS the entities (in a comment, or in dead code) cannot
    pass the static check by accident.

    Two skips, both harness limits rather than findings: a DOM-based helper
    (there is no jsdom here) and one whose entity table or base escaper lives
    outside the function body, which raises a ReferenceError under node. That
    the regex form is directly executable is a reason to prefer it.
    """
    code = _strip_comments(body)
    if _DOMLESS.search(code):
        pytest.skip(f"{path}: DOM-based helper, not node-executable")
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")

    script = body + f"\nprocess.stdout.write(JSON.stringify(esc({json.dumps(_PAYLOAD)})));\n"
    r = subprocess.run([node, "-e", script], capture_output=True, text=True)
    if r.returncode != 0:
        if "is not defined" in r.stderr:
            pytest.skip(f"{path}: helper depends on something outside its body")
        pytest.fail(f"{path}: helper crashed under node:\n{r.stderr}")

    got = json.loads(r.stdout)
    assert '"' not in got, f"{path}: `\"` survived escaping — attribute break-out"
    assert got == "&quot; onmouseover=&quot;alert(1)", f"{path}: unexpected output {got!r}"
    once = json.loads(_node(body + '\nprocess.stdout.write(JSON.stringify(esc("A & B")));\n'))
    assert once == "A &amp; B", f"{path}: `&` escaping is wrong: {once!r}"
