"""Contract tests for the chat icon vocabulary (#1503).

``src/chat_icons.py`` is the canonical list. Three other surfaces must agree
with it, and none of them can import it:

- the vendored Lucide sprite (``app/web/static/vendor/lucide-sprite.svg``)
  must carry a ``<symbol id>`` for every name;
- the browser allowlist in ``app/web/static/js/chat_icons.js`` must equal
  ``CHAT_INLINE_ICON_NAMES`` — that set is what the chat actually renders
  for an ``icon:<name>`` token;
- the prompt rule must offer the model exactly that list, on both prompt
  surfaces: the rendered CLAUDE.md template (``chat_icons`` context var,
  sandbox renders only) and the static fallback
  ``app/initial_workspace_default/CLAUDE.md`` (hardcoded — this test is
  what keeps the hardcopy honest).
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import duckdb
import pytest

from src.chat_icons import ALL_ICON_NAMES, CHAT_INLINE_ICON_NAMES, CHROME_ICON_NAMES
from src.claude_md import compute_default_claude_md
from src.db import _ensure_schema

_REPO = Path(__file__).resolve().parent.parent
_SPRITE = _REPO / "app" / "web" / "static" / "vendor" / "lucide-sprite.svg"
_JS_MODULE = _REPO / "app" / "web" / "static" / "js" / "chat_icons.js"
_FALLBACK_MD = _REPO / "app" / "initial_workspace_default" / "CLAUDE.md"


# ---------------------------------------------------------------------------
# The canonical list itself
# ---------------------------------------------------------------------------


def test_lists_are_sorted_and_disjoint():
    assert list(CHAT_INLINE_ICON_NAMES) == sorted(CHAT_INLINE_ICON_NAMES)
    assert list(CHROME_ICON_NAMES) == sorted(CHROME_ICON_NAMES)
    assert not set(CHAT_INLINE_ICON_NAMES) & set(CHROME_ICON_NAMES)
    assert set(ALL_ICON_NAMES) == set(CHAT_INLINE_ICON_NAMES) | set(CHROME_ICON_NAMES)


def test_names_are_kebab_case():
    # The JS token grammar is /icon:[a-z0-9]+(-[a-z0-9]+)*/ — a name that
    # doesn't match it could never be rendered from a token.
    for name in ALL_ICON_NAMES:
        assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name), name


# ---------------------------------------------------------------------------
# Sprite ↔ list
# ---------------------------------------------------------------------------


def _sprite_symbol_ids() -> set[str]:
    root = ET.fromstring(_SPRITE.read_text(encoding="utf-8"))
    ns = "{http://www.w3.org/2000/svg}"
    return {el.attrib["id"] for el in root.iter(f"{ns}symbol")}


def test_sprite_symbols_match_canonical_list():
    assert _sprite_symbol_ids() == set(ALL_ICON_NAMES)


# ---------------------------------------------------------------------------
# JS allowlist ↔ list
# ---------------------------------------------------------------------------


def test_js_allowlist_matches_inline_names():
    js = _JS_MODULE.read_text(encoding="utf-8")
    m = re.search(
        r"export const CHAT_INLINE_ICON_NAMES = \[(.*?)\];", js, flags=re.DOTALL
    )
    assert m, "CHAT_INLINE_ICON_NAMES array not found in chat_icons.js"
    js_names = re.findall(r'"([a-z0-9-]+)"', m.group(1))
    assert js_names == list(CHAT_INLINE_ICON_NAMES)


# ---------------------------------------------------------------------------
# Prompt surfaces ↔ list
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    c = duckdb.connect(str(tmp_path / "system.duckdb"))
    _ensure_schema(c)
    monkeypatch.setattr("src.repositories.get_system_db", lambda: c)
    yield c
    c.close()


def _user():
    return {"id": "u1", "email": "a@example.com", "name": "A", "is_admin": False, "groups": []}


def test_sandbox_prompt_carries_the_icon_rule(conn):
    out = compute_default_claude_md(
        conn, user=_user(), server_url="https://example.com", is_sandbox=True
    )
    assert "NEVER use emoji" in out
    assert "icon:<name>" in out
    # Every offered name is one the UI renders — and the model is offered all
    # of them (join(', ') of the context's chat_icons).
    assert ", ".join(CHAT_INLINE_ICON_NAMES) in out


def test_laptop_prompt_has_no_icon_rule(conn):
    # A terminal renders no icon: tokens — the rule is chat-sandbox-only.
    out = compute_default_claude_md(
        conn, user=_user(), server_url="https://example.com", is_sandbox=False
    )
    assert "NEVER use emoji" not in out
    assert "icon:<name>" not in out


def test_fallback_workspace_md_carries_the_same_list():
    # app/initial_workspace_default/CLAUDE.md hardcodes the vocabulary (it is
    # a static file, no Jinja) — this is the sync that keeps it honest. The
    # list there is wrapped to 80 cols, so compare after whitespace-folding.
    text = re.sub(r"\s+", " ", _FALLBACK_MD.read_text(encoding="utf-8"))
    assert "NEVER use emoji" in text
    assert ", ".join(CHAT_INLINE_ICON_NAMES) in text
