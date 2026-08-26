"""Shared phrase lists for the install-prompt de-escalation guard.

Imported by both `scripts/dev/check_prompt.py` (manual verification loop) and
`tests/test_install_prompt_banned_phrases.py` (the regression guard) so the
two never drift.
"""

from __future__ import annotations

# Force-style / coercive phrasing the rendered prompt must never contain.
# Each was present in the pre-de-escalation prompt and is a real trigger for
# Claude Code's safety classifier stalling mid-install.
BANNED_PHRASES: list[str] = [
    "NODE_TLS_REJECT_UNAUTHORIZED",
    "http.sslVerify",
    "strict-ssl=false",
    "| sh",
    "| bash",
    "--silent",
    "verbatim",
    "REFUSE",
    "PROCEED SILENTLY",
    "Treat empty/Enter",
    "do NOT ask the user",
    "GOCSPX-",
    # Patch 2 (PAT delivered out-of-band, not embedded in the prompt text):
    # the raw access token must never be a substitutable placeholder or a
    # literal JWT fragment inside the rendered body.
    "{token}",
    "eyJ",
    # Patch 3 — read back out of a real install transcript, where the agent
    # quoted these lines as its reason for distrusting the whole prompt.
    #
    # Pre-emptive trust assertions: text that answers a safety question the
    # reader has not asked yet reads as written to defuse the check. State
    # what a step does and leave the judgement to whoever is reading.
    "org's call",
    "verify it with their IT",
    "OK to use",
    # Concealment framing around the credential. The behaviour is fine — the
    # token lives in a file and nothing needs to display it — but phrased as
    # an instruction to keep it out of sight it reads as hiding a credential
    # from oversight rather than as "there is nothing to show here".
    "never print the token",
    "never on the command line",
]

# Deliberately NOT guarded here: the strongest trigger in that transcript was
# a hostname mismatch — a workspace doc naming one fleet-wide Agnes host as
# the legitimate one while the install ran against a per-instance host, which
# the agent read as a look-alike domain. That is a relationship between two
# values, not a phrase, so no substring check can see it; a per-instance
# server URL rendered through the normal templating is the fix, and a phrase
# pattern here would only produce false positives on ordinary host mentions.

# Facts that must survive de-escalation — the wording changed, not the
# underlying information.
#
# The list shrank when the prompt went thin (2026-08-19): the workspace
# triage facts it used to pin (`init-complete`, `agnes update`, the unsafe
# `$HOME` / `/tmp` directories, `--token-file`) are no longer prompt copy —
# `agnes onboard` owns those decisions and reports them at runtime. What the
# prompt still has to say is pinned below.
REQUIRED_FACTS: list[str] = [
    "$HOME",
    "idempotent",
    "~/.agnes/token",
    "agnes onboard",
    "--accept-dir",
]
