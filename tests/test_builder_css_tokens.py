"""Token hygiene for per-page stylesheets, as a ratchet.

The design-system guards in ``tests/test_design_system_contract.py`` that ban
raw hex and the legacy ``var(--primary)`` scan TEMPLATES — they read each
page's inline ``<style>``. That is where nearly all page CSS lived when they
were written.

Moving a page's rules into ``static/css/*.css`` therefore moves them out from
under those guards. Extracting the builder shell out of ``agents.html`` into
``builder.css`` did exactly that to ~200 lines, so this file follows them.

**Why a list and not every stylesheet.** Six sheets still reference
``var(--primary…)`` — ``style-custom.css``, ``home.css``, ``stack_card.css``,
``tour.css``, ``marketplace.css``, and ``design-tokens.css`` (which legitimately
DEFINES the compat shim). Sweeping them is its own change. So this is the
repo's usual ratchet shape: a named cohort held to the rule, which may only
GROW. Sweep a sheet, add it here in the same change; never remove one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS_DIR = Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "css"

# Stylesheets held to the token rules. MAY ONLY GROW — see the module docstring.
_TOKENIZED_SHEETS = ("builder.css",)

# `#` inside a url(), a comment, or an id selector is not a colour literal.
_HEX = re.compile(r"(?<![\w-])#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?(?![\w-])")
_LEGACY_PRIMARY = re.compile(r"var\(\s*--primary[-)\s,]")
_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _code(sheet: str) -> str:
    """Sheet text with comments blanked (newlines kept so lines stay reportable).
    A comment explaining why a rule avoids raw hex must not read as raw hex."""
    text = (CSS_DIR / sheet).read_text(encoding="utf-8")
    return _COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text)


@pytest.mark.parametrize("sheet", _TOKENIZED_SHEETS)
def test_sheet_exists(sheet: str) -> None:
    """A renamed or deleted sheet must not silently stop being checked."""
    assert (CSS_DIR / sheet).is_file(), (
        f"{sheet} is listed in _TOKENIZED_SHEETS but does not exist — if it moved, "
        "update the list; if it is gone, remove the entry deliberately"
    )


@pytest.mark.parametrize("sheet", _TOKENIZED_SHEETS)
def test_no_raw_hex_colour(sheet: str) -> None:
    offenders = [
        f"{sheet}:{n}: {line.strip()}" for n, line in enumerate(_code(sheet).splitlines(), 1) if _HEX.search(line)
    ]
    assert not offenders, (
        "raw hex colour in a tokenized stylesheet — use a --ds-* token so the "
        "rule follows the theme:\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize("sheet", _TOKENIZED_SHEETS)
def test_no_legacy_primary_token(sheet: str) -> None:
    """`var(--primary)` rides the compat shim in design-tokens.css; the explicit
    `var(--ds-primary)` reads self-documenting and survives the shim's removal."""
    offenders = [
        f"{sheet}:{n}: {line.strip()}"
        for n, line in enumerate(_code(sheet).splitlines(), 1)
        if _LEGACY_PRIMARY.search(line)
    ]
    assert not offenders, "`var(--primary…)` found — use `var(--ds-primary…)`:\n" + "\n".join(offenders)


@pytest.mark.parametrize("sheet", _TOKENIZED_SHEETS)
def test_no_bare_root_block(sheet: str) -> None:
    """Same rule leaf templates get: a per-page `:root {}` shadows the canonical
    tokens. Tokens live in design-tokens.css / _theme.html."""
    assert not re.search(r":root\s*\{", _code(sheet)), (
        f"{sheet} declares a bare `:root {{` block — design tokens belong in design-tokens.css, not a per-page sheet"
    )


def test_the_guard_is_not_vacuous() -> None:
    """The detectors must actually fire — a broken regex here would let every
    real offender through while the suite stayed green."""
    assert _HEX.search("color: #ff00aa;")
    assert _HEX.search("color: #f0a;")
    assert not _HEX.search("var(--ds-primary)")
    assert _LEGACY_PRIMARY.search("color: var(--primary);")
    assert not _LEGACY_PRIMARY.search("color: var(--ds-primary);")
    # ...and comments are genuinely blanked rather than merely ignored.
    assert "#ff00aa" not in _COMMENT.sub("", "/* was #ff00aa */ color: var(--ds-primary);")
