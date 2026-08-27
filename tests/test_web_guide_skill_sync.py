"""Static guard: the bundled `agnes-web-guide` skill mirrors the live web UI.

The chat agent has no eyes on the product: everything it knows about what the
user is looking at arrives as files in the bundled workspace template
(`app/initial_workspace_default/`). The `agnes-web-guide` skill is that
knowledge — a page-by-page guide to the web UI, so the agent can answer
"where do I ...?" against the same product the user has open. A guide that
names a page the product no longer has, or is silent about one it gained, is
worse than none: the agent confidently sends the user to a URL that 404s, or
answers "there is no such page" about a surface one click away.

Three invariants keep the guide and the product the same:

* every USER-FACING page route — the same set
  ``tests/test_web_nav_user_parity.py`` sweeps out of ``app/web/router.py`` —
  is mentioned in the guide;
* every ADMIN destination in ``app/web/admin_nav.py`` — the inventory
  ``tests/test_web_admin_nav.py`` already holds to the route table — is
  mentioned;
* every path the guide mentions is LIVE (a user-facing route, an admin nav
  entry, or an explicitly justified extra), so a deleted page cannot survive
  as prose and a retired browse URL (``/catalog``, ``/marketplace`` — 302s
  into the Library) cannot be recommended over its replacement.

A path counts as MENTIONED only inside backticks (`` `/library` ``). That
keeps the extraction exact — prose stays free, and a parameterized shape like
`` `/catalog/t/{table_id}` `` is deliberately outside the liveness sweep (the
brace never matches), because detail pages are reached from their list, not
recommended by URL.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.test_web_nav_user_parity import _user_facing_page_routes

SKILL_DIR = Path("app/initial_workspace_default/.claude/skills/agnes-web-guide")

#: Paths the guide may mention that are neither user-facing template routes
#: nor admin-nav entries. Each needs a reason; an entry without one is drift.
ALLOWED_EXTRA_PATHS: dict[str, str] = {}

_MENTION_RE = re.compile(r"`(/[a-z0-9/_=?&-]*)`")


def _guide_files() -> list[Path]:
    return sorted(SKILL_DIR.rglob("*.md"))


def _mentioned_paths() -> set[str]:
    """Every backticked absolute path across the skill's markdown files,
    query string stripped — `` `/admin/access?lens=simulate` `` is a mention
    of ``/admin/access``."""
    out: set[str] = set()
    for md in _guide_files():
        for m in _MENTION_RE.findall(md.read_text(encoding="utf-8")):
            out.add(m.split("?", 1)[0].rstrip("/") or "/")
    return out


def _admin_nav_paths() -> set[str]:
    """Every destination the admin nav offers — the hub row, each section's
    row and entries, the justified off-nav pages, and the docs footer."""
    from app.web.admin_nav import (
        ADMIN_NAV_DOCS,
        ADMIN_NAV_HOME,
        ADMIN_NAV_OFFNAV,
        ADMIN_NAV_SECTIONS,
        _section_entries,
    )

    hrefs = {ADMIN_NAV_HOME["href"]}
    for section in ADMIN_NAV_SECTIONS:
        if section.get("href"):
            hrefs.add(section["href"])
        for entry in _section_entries(section):
            hrefs.add(entry["href"])
    hrefs |= {e["href"] for e in ADMIN_NAV_OFFNAV}
    hrefs |= {e["href"] for e in ADMIN_NAV_DOCS}
    return {h.split("?", 1)[0].rstrip("/") or "/" for h in hrefs}


def test_the_guide_ships_in_the_bundled_template():
    """The skill must exist where `WorkdirManager` copies from, with the
    frontmatter `app/chat/skills_catalog.py` advertises it by."""
    skill_md = SKILL_DIR / "SKILL.md"
    assert skill_md.is_file(), (
        f"{skill_md} is missing — the chat agent's web-UI guide ships as a "
        "bundled workspace-template skill, nothing else reaches every sandbox."
    )
    from src.store_guardrails._frontmatter import parse_frontmatter

    fm = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
    assert fm.get("name") == "agnes-web-guide"
    assert (fm.get("description") or "").strip(), "the slash menu and the Skill tool select on the description"


def test_every_user_facing_page_is_in_the_guide():
    missing = _user_facing_page_routes() - _mentioned_paths()
    assert not missing, (
        f"User-facing pages the web guide never mentions: {sorted(missing)}. "
        "The chat agent answers 'where do I ...?' from this skill alone — a "
        "page it does not know about is one it will deny exists. Add each to "
        f"{SKILL_DIR}/references/user-pages.md (backticked path + what the "
        "user sees there)."
    )


def test_every_admin_nav_destination_is_in_the_guide():
    missing = _admin_nav_paths() - _mentioned_paths()
    assert not missing, (
        f"Admin destinations the web guide never mentions: {sorted(missing)}. "
        f"Add each to {SKILL_DIR}/references/admin-pages.md (backticked path "
        "+ what the admin does there)."
    )


def test_the_guide_names_only_live_paths():
    """The reverse sweep: a page deleted or folded into the Library must take
    its guide entry with it, or the agent keeps recommending a dead URL."""
    live = _user_facing_page_routes() | _admin_nav_paths() | set(ALLOWED_EXTRA_PATHS)
    stale = _mentioned_paths() - live
    assert not stale, (
        f"The web guide mentions paths that are not live pages: {sorted(stale)}. "
        "Remove the entry (the page is gone or now redirects — point at its "
        "replacement instead), or justify it in ALLOWED_EXTRA_PATHS."
    )


def test_every_extra_path_carries_a_reason():
    for path, reason in ALLOWED_EXTRA_PATHS.items():
        assert reason and len(reason) > 20, f"{path} needs a real reason, got {reason!r}"
