"""Brandable product name — a rebranded instance (`instance.brand` /
`AGNES_INSTANCE_BRAND`) must show its own name in PROSE across the web UI,
with no hardcoded "Agnes" leaking through on a representative page set.

Renders chat, home (not-onboarded), login, and how-it-works with a custom
brand and asserts:
  (a) the custom brand appears in the rendered prose;
  (b) the literal "Agnes" does NOT appear in that prose.

"Prose" excludes `<script>`, `<style>`, `<code>`, and `<pre>` content — CLI
examples, JS identifiers (`window.AgnesTime`), asset paths
(`agnes-orb.png`), and the setup-script clipboard payload (a separate,
intentionally CLI-flavored surface, always rendered inside <pre>/<code> or a
<script> array — see `_claude_setup_instructions.jinja`) are not product-name
prose and are exempt by construction, not by name-matching.

A companion test pins the opposite invariant: a page documenting CLI usage
must still show the lowercase `agnes` command untouched — the sweep must not
have clobbered a technical identifier while chasing prose.
"""

from __future__ import annotations

import re

import pytest

CUSTOM_BRAND = "Zenith Analytics"

_CODE_LIKE_RE = re.compile(r"<(script|style|code|pre)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _prose(html: str) -> str:
    """Strip script/style/code/pre content AND HTML comments, leaving only
    rendered prose. HTML comments (e.g. `_app_scripts.html`'s `<!-- window.
    AgnesTime ... -->` doc header) are never visible to a page reader —
    same exemption as a Jinja `{# ... #}` comment, just a different
    delimiter that survives into the response body."""
    return _HTML_COMMENT_RE.sub("", _CODE_LIKE_RE.sub("", html))


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def branded(seeded_app, monkeypatch):
    """`seeded_app` + a custom brand + an explicit chat grant for Admin.

    The grant matters beyond RBAC: the rail's "Set up {brand}" onboarding
    widget (`_app_rail.html`) only renders when the viewer holds an
    EXPLICIT chat grant (`can_chat`), not merely admin god-mode — see
    `_compute_can_chat`'s docstring. Granting it here is what actually
    exercises that widget's brand-templated title/aria-label on `/chat`,
    instead of silently skipping it.
    """
    monkeypatch.setenv("AGNES_INSTANCE_BRAND", CUSTOM_BRAND)
    monkeypatch.delenv("AGNES_INSTANCE_BRAND_SHORT", raising=False)

    from types import SimpleNamespace

    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.resource_grants import ResourceGrantsRepository

    conn = get_system_db()
    try:
        admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
        ResourceGrantsRepository(conn).create(admin_gid, "chat", "chat", assigned_by="test")
    finally:
        conn.close()

    # `seeded_app`'s TestClient is never used as a context manager, so the
    # app's lifespan (which normally sets app.state.chat_config) never runs —
    # same workaround as tests/test_chat_page_no_grant.py.
    seeded_app["client"].app.state.chat_config = SimpleNamespace(enabled=True)

    return seeded_app


def _representative_pages(branded) -> dict:
    """One rendered response per representative page.

    ``/login`` needs ``?error=deactivated``: its brand-templated copy lives
    in a conditional error banner (`{% if _err and _err_messages.get(_err)
    %}`) — the plain unauthenticated page renders neither the brand nor the
    literal "Agnes" anywhere in prose (the hero H1 deliberately shows
    ``instance_name``, the deployment label, not the product brand — see
    ``get_instance_name`` vs. ``get_instance_brand``'s docstrings), so
    hitting it without an error would test nothing.
    """
    client = branded["client"]
    return {
        "chat": client.get("/chat", headers=_auth(branded["admin_token"])),
        "home_not_onboarded": client.get("/home", headers=_auth(branded["analyst_token"])),
        "login": client.get("/login?error=deactivated"),
        "how_it_works": client.get("/how-it-works", headers=_auth(branded["analyst_token"])),
    }


@pytest.mark.parametrize(
    "page_name",
    ["chat", "home_not_onboarded", "login", "how_it_works"],
)
def test_custom_brand_appears_in_prose(branded, page_name):
    resp = _representative_pages(branded)[page_name]
    assert resp.status_code == 200, resp.text
    assert CUSTOM_BRAND in _prose(resp.text)


@pytest.mark.parametrize(
    "page_name",
    ["chat", "home_not_onboarded", "login", "how_it_works"],
)
def test_no_literal_agnes_in_prose(branded, page_name):
    resp = _representative_pages(branded)[page_name]
    assert resp.status_code == 200, resp.text
    prose = _prose(resp.text)
    assert "Agnes" not in prose, (
        f"literal 'Agnes' leaked into rendered prose on /{page_name} with AGNES_INSTANCE_BRAND={CUSTOM_BRAND!r} set"
    )


def test_cli_command_examples_survive_the_brand_sweep(branded):
    """The lowercase `agnes` binary name in a CLI example is a technical
    identifier, not the product name — the prose sweep must not have
    touched it. `/how-it-works` documents `agnes catalog` inside a
    `<pre>` block."""
    resp = branded["client"].get("/how-it-works", headers=_auth(branded["analyst_token"]))
    assert resp.status_code == 200
    assert "agnes catalog" in resp.text


def test_rail_hands_the_resolved_brand_to_the_onboarding_script(branded):
    """The rail's onboarding card is branded server-side AND rewritten by
    `chat_onboarding.js` once `/api/chat/journey` resolves, so the resolved
    brand has to reach the script — otherwise a rebranded instance shows its
    own name for one frame and then has "Agnes" written over it.

    The seam is `data-brand-short` on `#railGetStarted` (read by
    `brandShort()`); this pins that the attribute is emitted with the
    operator's value, not the fallback.

    Read on /library rather than /chat. The rail is on every page, but only
    /chat computes `admin_setup` — and the analyst journey now stands down
    there for an admin whose setup chain is unfinished, because two six-step
    journeys must not render together. Any other page renders the card
    exactly as before, so this keeps testing the brand seam instead of
    accidentally testing that suppression rule."""
    resp = branded["client"].get("/library", headers=_auth(branded["admin_token"]))
    assert resp.status_code == 200, resp.text
    assert 'id="railGetStarted"' in resp.text, "rail onboarding card did not render — the widget under test is absent"
    assert f'data-brand-short="{CUSTOM_BRAND}"' in resp.text, (
        "the rail does not hand the resolved brand to chat_onboarding.js, so the script's "
        f"rewrite will replace {CUSTOM_BRAND!r} with the literal fallback"
    )


def test_onboarding_script_reads_the_brand_seam_for_the_copy_it_rewrites():
    """pytest cannot execute the module, so this pins the source shape of the
    three strings that overwrite server-rendered branded text: the rail card
    title, the popover heading, and the replay tooltip in that same header.

    Deliberately narrow. The module's other product-name literals (step
    `why` copy, the first-visit greeting) are pre-existing prose that nothing
    server-side contradicts — see `brandShort()`'s comment for what is and
    is not in this seam's reach."""
    from pathlib import Path

    src = Path("app/web/static/js/chat_onboarding.js").read_text()
    # Comment lines mention the old copy on purpose (they explain the flip to
    # "Continue setup"); only executable lines are under test.
    code = "\n".join(line for line in src.splitlines() if not line.strip().startswith("//"))

    assert "`Set up ${brandShort()}`" in code, "rail card title no longer reads the brand seam"
    assert "`Set up ${brand}`" in code, "popover heading no longer reads the brand seam"
    assert "`Replay the ${brand} tour`" in code or "Replay the ${brand} tour" in code, (
        "replay tooltip no longer reads the brand seam"
    )
    assert '"Set up Agnes"' not in code, "a hardcoded product name is back in the onboarding copy"
    assert "Replay Agnes's" not in code, "a hardcoded product name is back in the replay tooltip"
