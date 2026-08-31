"""Product vocabulary decisions (TCRD-208).

The design-system contract tests police colour, spacing and component use.
Nothing policed LANGUAGE, which is why sixteen design reviews each re-found a
naming collision independently. This is the missing guard: each assertion
pins one decision, so the word that won stays won.

Deliberately narrow. It pins DECIDED collisions on the files they were
decided on, rather than scanning every template for every banned word — a
broad scanner fires on code comments and internal identifiers, where this
vocabulary is legitimate and where nobody is reading.
"""

from __future__ import annotations

import re
from pathlib import Path

T = Path("app/web/templates")


def _read(name: str) -> str:
    return (T / name).read_text(encoding="utf-8")


# Jinja, HTML and JS block comments, plus JS line comments. Stripped as
# BLOCKS rather than line-by-line: a multi-line comment's continuation lines
# carry no marker of their own, so a per-line filter reports the middle of a
# comment as rendered copy — which is exactly the false positive that would
# teach the next author to ignore this file.
_COMMENTS = re.compile(r"\{#.*?#\}|<!--.*?-->|/\*.*?\*/", re.DOTALL)


def _rendered_lines(src: str) -> list[str]:
    """Lines a user could see. A comment's wording and typography reach
    nobody, so they are not this guard's business."""
    stripped = _COMMENTS.sub("", src)
    return [line for line in stripped.splitlines() if not line.strip().startswith("//")]


# ── internal language reaching users ───────────────────────────────────────


def test_profile_does_not_call_admin_access_god_mode():
    """Engineering slang on a page a customer's administrator reads. It also
    sounds like a joke about a thing that is not one."""
    for line in _rendered_lines(_read("profile.html")):
        assert "god-mode" not in line.lower(), f"god-mode in user copy: {line.strip()[:80]}"


def test_profile_does_not_overload_the_word_consent():
    """'Consent' already means OAuth provider consent everywhere else in
    Agnes. Reusing it for 'are you sure you want to act as an admin' puts one
    word on two mechanisms a user meets in the same session."""
    for line in _rendered_lines(_read("profile.html")):
        assert "consent gate" not in line.lower(), f"consent gate in user copy: {line.strip()[:80]}"


# ── one thing, one name ────────────────────────────────────────────────────


def test_the_connection_pills_state_a_state_and_nothing_else():
    """`Connected` / `Expired` / `Not connected`. A pill that also carries an
    instruction ('Expired — reconnect') is the odd one out beside two that
    don't, and nobody reads a pill for instructions — the remedy belongs in
    the detail line, which has it."""
    pills = re.findall(r'connx-pill--(?:on|off)">([^<]+)', _read("me_connections.html"))
    assert pills, "no connection pills found — did the markup move?"
    for p in pills:
        assert "—" not in p, f"pill carries more than a state: {p!r}"
        assert len(p.split()) <= 2, f"pill is a sentence, not a state: {p!r}"


def test_the_semantic_layer_token_is_referred_to_by_its_real_label():
    """The row was already renamed to `Semantic-layer token` — with the right
    reasoning, in a comment on the row itself. A cross-reference to the old
    label is worse than a collision: it sends a reader looking for a row that
    does not exist.

    Pinned on the page that RENDERS the row (`/admin/data-sources`) rather
    than on a page that merely pointed at it. The #1707 rebuild of
    `/admin/semantic-layer` dropped the Keboola-specific Sources section that
    carried the cross-reference, so the original positive assertion had no
    copy left to hold; the decision itself is unchanged and is now pinned
    where it is actually shown. The ban stays on both pages, so the retired
    label cannot come back on either.
    """
    rendered = "\n".join(_rendered_lines(_read("admin_data_sources.html")))
    assert "Master token (semantic layer)" not in rendered
    assert "Semantic-layer token" in rendered
    assert "Master token (semantic layer)" not in _read("admin_semantic_layer.html")


def test_drift_columns_are_named_for_the_two_places_not_for_the_pipeline():
    """'upstream' is a pipeline word; the reader is thinking about their own
    system, which has a name.

    The columns this decision renamed lived in the per-project Sources table
    on `/admin/semantic-layer`, which the #1707 rebuild removed outright (the
    page is now Coverage · Health · Mute · Feedback, and no surface reports
    per-source metric/glossary drift). There is therefore no positive copy
    left to assert. What survives — and is what this guard was for — is the
    ban, widened from that one page to every template's RENDERED copy: the
    decision was that this phrasing is wrong for users anywhere, and the
    rebuild must not be a way for it to reappear on a different page.
    """
    for path in sorted(T.glob("*.html")):
        rendered = "\n".join(_rendered_lines(path.read_text(encoding="utf-8")))
        assert "stored / upstream" not in rendered, f"{path.name} uses the retired drift-column wording"


# ── casing and typography ──────────────────────────────────────────────────


def test_initialisms_are_uppercase_beside_each_other():
    """`Id` was the odd one out next to `URL`. Both are initialisms, so both
    are uppercase; everything else stays sentence case.

    Retargeted when the linked-apps wizard — the page that first showed the
    lowercase `Id` — was folded into the MCP-source builder. The rule is about
    the vocabulary, not that page, so it now reads the detail surface that
    still puts the two initialisms side by side.
    """
    src = _read("admin_mcp_source_detail.html")
    assert ">Id<" not in src and "<strong>id</strong>" not in src
    assert ">ID<" in src or ">URL<" in src


def test_the_login_page_has_no_straight_apostrophes_in_what_it_shows():
    """One character, on the first screen anyone sees, in a repo that is
    otherwise consistently typographic."""
    offenders = [
        line.strip()[:90] for line in _rendered_lines(_read("login.html")) if re.search(r"[A-Za-z]'[a-z]", line)
    ]
    assert not offenders, "straight apostrophes in rendered login copy: " + "; ".join(offenders)
