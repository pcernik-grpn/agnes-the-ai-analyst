"""Regression guard for the install-prompt de-escalation.

The install prompt used to lean on force-style phrasing (ALL-CAPS
commands, "do NOT", "verbatim", "REFUSE", "PROCEED SILENTLY", mandatory /
default-yes framing, piped shell installers) that Claude Code's safety
classifier increasingly stalls on mid-install. This test pins the
de-escalated wording so it doesn't regress.

All three tiers are now enforced unconditionally: tier 1 is the
builder-owned scaffolding in `app/web/setup_instructions.py`, tier 2 the
bundled seed's install-prompt template, tier 3 the bundled connector
SKILL.md bodies. The tier 2/3 known-dirty ratchet is gone — the vendored
seed's de-escalation pass has landed, so any banned phrase reappearing
in it is a regression that fails here instead of skipping.

Banned-phrase / required-fact lists are shared with
`scripts/dev/check_prompt.py` via `scripts/dev/prompt_phrases.py` so the
manual verification loop and this guard never drift apart.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DEV = Path(__file__).resolve().parents[1] / "scripts" / "dev"
if str(_SCRIPTS_DEV) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DEV))

from prompt_phrases import BANNED_PHRASES, REQUIRED_FACTS  # noqa: E402


def _assert_clean(text: str, *, label: str) -> None:
    hits = [phrase for phrase in BANNED_PHRASES if phrase in text]
    assert not hits, f"{label}: banned phrase(s) found: {hits}"
    missing = [fact for fact in REQUIRED_FACTS if fact not in text]
    assert not missing, f"{label}: required fact(s) missing: {missing}"


_FAKE_CA_PEM = "-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n"


def test_default_render_is_clean():
    """The thin default render — pure builder scaffolding."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl"))
    _assert_clean(joined, label="default render")


def test_ca_render_is_clean():
    """The TLS trust block (step 0) must also read as calm guidance."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(resolve_lines("agnes.whl", ca_pem=_FAKE_CA_PEM))
    _assert_clean(joined, label="ca render")


def test_operator_preamble_render_is_clean():
    """The one remaining render variant: an operator-authored preamble
    prepended above the scaffolding. Its own text is operator content, but
    the scaffolding around it must still pass the guard unchanged."""
    from app.web.setup_instructions import resolve_lines

    joined = "\n".join(
        resolve_lines(
            "agnes.whl",
            instance_brand="BrandCo",
            workspace_dir="BrandCo",
            custom_preamble="Ask IT before installing anything on a managed laptop.",
        )
    )
    _assert_clean(joined, label="custom-preamble render")


def _scan_bundled_seed_file(rel_path: str) -> list[str]:
    from src.connectors_manifest import bundled_seed_path

    path = bundled_seed_path() / rel_path
    text = path.read_text(encoding="utf-8")
    return [phrase for phrase in BANNED_PHRASES if phrase in text]


# Known-dirty ratchet (pinned 2026-08-05): phrases present in the vendored
# seed today. Anything NEW fails immediately; entries drop out as the seed
# repo's de-escalation pass lands and gets re-mirrored into
# `src/_bundled_seed/`, and once a baseline is empty the corresponding
# test enforces the full banned list unconditionally (the skip branch
# never fires on a clean seed).
#
# Tier 2 reached an empty baseline (2026-08-19): the bundled install-prompt
# template was rewritten to the thin shape, and every phrase it used to be
# pinned for lived in the fat prompt's sections (`REFUSE` / `PROCEED
# SILENTLY` install-location triage, the `Treat empty/Enter` connector
# tiles, the TLS-disabling counter-examples). The guard is now
# unconditional for that file.
_TIER2_KNOWN_DIRTY: frozenset[str] = frozenset()

_TIER3_KNOWN_DIRTY: dict[str, frozenset[str]] = {
    "connector-asana": frozenset(
        {
            "NODE_TLS_REJECT_UNAUTHORIZED",
            "Treat empty/Enter",
            "http.sslVerify",
            "verbatim",
        }
    ),
    "connector-atlassian": frozenset(
        {
            "NODE_TLS_REJECT_UNAUTHORIZED",
            "http.sslVerify",
            "verbatim",
        }
    ),
}


def test_bundled_install_prompt_template_tier2():
    """Tier 2: the bundled install-prompt template — enforced with no
    baseline, so a banned phrase reappearing in the vendored seed fails
    here instead of being skipped.
    """
    hits = _scan_bundled_seed_file("install-prompt/template.md.tmpl")
    assert not hits, f"bundled install-prompt/template.md.tmpl: banned phrase(s) found: {sorted(hits)}"


def test_bundled_connector_skills_tier3():
    """Tier 3: bundled connector SKILL.md bodies — same ratchet as tier
    2, per file. Files without a baseline entry (new connectors) are
    fully enforced from the start.
    """
    import pytest

    from src.connectors_manifest import bundled_seed_path

    root = bundled_seed_path() / "workspace" / ".claude" / "skills"
    known: dict[str, list[str]] = {}
    new: dict[str, list[str]] = {}
    for skill_dir in sorted(root.glob("connector-*")):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        text = skill_md.read_text(encoding="utf-8")
        hits = {phrase for phrase in BANNED_PHRASES if phrase in text}
        baseline = _TIER3_KNOWN_DIRTY.get(skill_dir.name, frozenset())
        if hits - baseline:
            new[skill_dir.name] = sorted(hits - baseline)
        if hits & baseline:
            known[skill_dir.name] = sorted(hits & baseline)

    assert not new, (
        f"bundled connector SKILL.md file(s) gained banned phrase(s) beyond the recorded known-dirty baseline: {new}"
    )
    if known:
        pytest.skip(
            "bundled connector SKILL.md file(s) still have known-dirty "
            f"phrase(s), pending the seed repo re-mirror: {known}"
        )


def test_the_seed_template_carries_the_same_two_hardenings_as_the_renderer():
    """Both install-prompt sources must carry the security-relevant fixes.

    There are two, and which one a deployment serves is an operator setting:
    `resolve_prompt("install", conn)` honors the prompt's `source_mode`, and
    `'git'` binds to the IWT clone's copy of this template instead of
    `app/web/setup_instructions.py`'s renderer (`src/welcome_template.py`).

    So a fix applied only to the Python side silently misses every
    `source_mode='git'` instance. That happened: the renderer gained
    `--max-redirs 0` and the two-signal token pre-check while this template
    kept a bare `curl -fsSL -OJ` and the original "an earlier run already
    saved the credential, so just continue" false positive — which tells the
    agent to proceed on a machine where `/cli/install.sh` wrote `server:` and
    nobody ever signed in.

    The banned-phrase tiers cannot catch this: they scan for phrases that must
    be ABSENT, and both gaps are about text that must be PRESENT.
    """
    from src.connectors_manifest import bundled_seed_path
    from app.web.setup_instructions import resolve_lines

    tmpl = (bundled_seed_path() / "install-prompt" / "template.md.tmpl").read_text(encoding="utf-8")
    # The renderer is compared on its RENDERED output, not its source: its
    # docstrings quote the old wording to explain why it was wrong, and a
    # source scan would read those as the wording itself.
    rendered = "\n".join(resolve_lines("agnes.whl"))

    for source, text in (("seed template", tmpl), ("rendered prompt", rendered)):
        assert "--max-redirs 0" in text, f"{source}: wheel download must refuse redirects"
        assert "test -f ~/.config/agnes/token.json &&" in text, (
            f"{source}: the token pre-check must require a saved credential, not just a server match"
        )
        assert "so just continue" not in text, f"{source}: the false-positive wording is back"
