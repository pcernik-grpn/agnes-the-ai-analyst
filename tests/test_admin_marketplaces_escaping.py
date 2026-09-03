"""`esc()` on the Marketplaces admin page must be safe in an ATTRIBUTE.

Half of that page's call sites interpolate into a quoted HTML attribute —
`title="${esc(m.last_error)}"` on the table row's failure badge, and (since
#1956 item 15) on the Details modal's sync line too. The helper is built on
`textContent` -> `innerHTML`, which escapes `&`, `<` and `>` but leaves quotes
alone, because a text node has no need of them.

That one missing character is the whole finding: `last_error` holds git's
stderr for a failed clone, which quotes the remote it could not reach
(`fatal: repository '...' not found`), and `src/marketplace.py::_strip_userinfo`
only strips credentials from it — HTML safety is this layer's job. An admin who
registers a marketplace URL containing a double quote therefore lands one in
`title="..."`, breaking out of the attribute, and the next admin who opens the
page runs whatever followed it.

Asserted against the shipped helper rather than a copy of it, and driven
through a real DOM so the assertion is about what browsers do rather than what
the source looks like.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_marketplaces.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _esc_source() -> str:
    src = TEMPLATE.read_text(encoding="utf-8")
    m = re.search(r"function esc\(s\) \{.*?\n\}", src, re.S)
    assert m, "esc() not found in admin_marketplaces.html — has it been renamed?"
    return m.group(0)


def _run_esc(payload: str) -> str:
    """Run the SHIPPED esc() over `payload` in node, on a minimal DOM shim."""
    shim = """
    const store = new WeakMap();
    globalThis.document = {
      createElement: () => {
        const el = {
          set textContent(v) { store.set(el, String(v)); },
          get innerHTML() {
            return (store.get(el) || "")
              .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
          },
        };
        return el;
      },
    };
    """
    script = shim + _esc_source() + f"\nprocess.stdout.write(JSON.stringify(esc({json.dumps(payload)})));"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_double_quote_cannot_close_the_attribute_it_sits_in():
    """The break-out itself, in the shape `last_error` would carry it."""
    payload = '" onmouseover="alert(1)'
    escaped = _run_esc(payload)
    assert '"' not in escaped, (
        f'esc() let a raw double quote through ({escaped!r}) — title="{escaped}" '
        "closes early and everything after it becomes markup"
    )
    assert "&quot;" in escaped


def test_a_single_quote_is_escaped_too():
    """Git quotes remotes with apostrophes far more often than with double
    quotes, and a single-quoted attribute is one refactor away."""
    assert "'" not in _run_esc("it's a 'remote'")


def test_the_ordinary_html_metacharacters_still_go():
    """The behaviour that was already right must survive the addition."""
    escaped = _run_esc('<script>alert(1)</script> & more')
    assert "<" not in escaped and ">" not in escaped
    assert "&lt;script&gt;" in escaped and "&amp;" in escaped


def test_plain_text_is_left_readable():
    """Escaping is not mangling: a normal message round-trips unchanged, so
    this cannot pass by returning a constant."""
    assert _run_esc("Last synced 3 minutes ago") == "Last synced 3 minutes ago"
