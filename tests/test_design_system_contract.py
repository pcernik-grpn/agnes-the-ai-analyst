"""Design-system invariants. Fails if a future PR undoes the design-pass."""

from pathlib import Path
import re

import pytest


TEMPLATES = Path("app/web/templates")
STATIC = Path("app/web/static")


def _all_html() -> list[Path]:
    """All HTML- or JINJA-templated files that ship with the app and may
    reference design-system tokens. Includes `*.jinja` (e.g.
    `_claude_setup_cta.jinja`) so the token-sweep regression guards
    cover them too."""
    return sorted(list(TEMPLATES.rglob("*.html")) + list(TEMPLATES.rglob("*.jinja")))


# Match every class="..." or class='...' attribute, possibly multi-line.
# Jinja templates frequently break class attributes across lines for the
# {% if … %}is-active{% endif %} pattern, so re.DOTALL is required.
_CLASS_ATTR_RE = re.compile(r"""class\s*=\s*(["'])(.*?)\1""", re.DOTALL)


def _classes_in_template(text: str) -> set[str]:
    """Extract every literal class token used in the template. Tokenizes the
    class attribute on whitespace so multi-class attrs ("btn btn-primary")
    and multi-line attrs split cleanly. Jinja conditionals (tokens that
    contain `{{`, `{%`, `}`) are skipped — only authors' literal class
    names are returned, since constructed names can't be statically
    audited without a render."""
    tokens: set[str] = set()
    for match in _CLASS_ATTR_RE.finditer(text):
        attr_value = match.group(2)
        for tok in attr_value.split():
            if "{" in tok or "}" in tok:
                continue
            tokens.add(tok)
    return tokens


# Single class tokens. Multi-token patterns (like "modal-btn primary") are
# caught by the single-token entry (.modal-btn) — no need to special-case.
DEPRECATED_CLASSES = {
    "btn-primary-v2": "btn-primary",
    "btn-secondary-v2": "btn-secondary",
    "btn-warning": "btn-danger",
    "modal-btn": "btn + .btn-primary / .btn-secondary",
    "users-table": "data-table",
    "gp-table": "data-table",
    "marketplaces-table": "data-table",
    "audit-table": "data-table",
    "stats-table": "data-table",
    "users-search": "search-input",
    "marketplaces-search": "search-input",
    "kb-search": "search-input",
    "filters-card": "filter-bar",
}


# A bare top-level element selector (`nav { display: flex; … }`) applies to
# EVERY instance of that tag app-wide, not just the one the author had in
# mind — the #1207 bug: a `nav {}` rule written for the header's primary nav
# silently turned `.sl-cat-nav` (the semantic-layer category sidebar) into a
# horizontal row, clipped by its overflow:hidden parent, reading as blank.
# `html`/`body`/`*` are foundational. `header`/`footer`/`code` are pre-#367
# legacy resets and are allowlisted as DEBT, not as innocents: they are the
# same bug class as the `nav {}` rule this guard was written for, and they are
# live, not dead. `header {}` (flex + border-bottom + padding) applies to eight
# bare `<header>` elements across admin_server_config.html,
# admin_initial_workspace.html and home_not_onboarded.html; `footer {}`
# (margin + border-top) applies to base_ds.html's bare `<footer>`, i.e. every
# design-system page. Unpicking them means auditing each call site for a visual
# change and is out of scope for #1207 — but a future `<header>`/`<footer>`
# that does not declare its own layout WILL inherit these silently. Narrow the
# allowlist, don't grow it.
_BARE_ELEMENT_SELECTOR_ALLOWLIST = {"html", "body", "*", "header", "footer", "code"}
# Keyframe stop names, not element selectors — `@keyframes … { from { … } to { … } }`.
_KEYFRAME_STOPS = {"from", "to"}
_BARE_ELEMENT_SELECTOR_RE = re.compile(r"(?m)^[ \t]*([a-z][a-zA-Z0-9]*)(?:[ \t]*,[ \t]*[a-z][a-zA-Z0-9]*)*[ \t]*\{")


def test_no_bare_element_selector_in_style_custom() -> None:
    """A future bare `nav {}` (or any other unscoped tag selector) must fail
    the build, not ship — see #1207. Selectors are only "bare" when the
    element name stands alone (no class/id/attribute/combinator); a
    descendant or compound selector like `.app-header nav` or `a.logo` is
    correctly scoped and not flagged."""
    css = (STATIC / "style-custom.css").read_text(encoding="utf-8")
    offenders: list[str] = []
    for m in _BARE_ELEMENT_SELECTOR_RE.finditer(css):
        selector_line = m.group(0)
        tags = re.findall(r"[a-z][a-zA-Z0-9]*", selector_line)
        for tag in tags:
            if tag in _KEYFRAME_STOPS or tag in _BARE_ELEMENT_SELECTOR_ALLOWLIST:
                continue
            offenders.append(f"{tag!r} (line {css.count(chr(10), 0, m.start()) + 1})")
    assert not offenders, (
        "bare element selector(s) found in style-custom.css — scope to a class "
        f"instead of styling every instance of the tag app-wide (#1207): {offenders}"
    )


def test_style_css_deleted() -> None:
    """style.css must stay deleted — all rules live in style-custom.css."""
    assert not (STATIC / "style.css").exists(), "style.css must stay deleted — all rules live in style-custom.css"


def test_no_template_references_style_css() -> None:
    """No template should link the deleted stylesheet."""
    offenders: list[str] = []
    for path in _all_html():
        text = path.read_text(encoding="utf-8")
        if "static_url('style.css')" in text or 'static_url("style.css")' in text:
            offenders.append(str(path))
    assert not offenders, f"templates still link style.css: {offenders}"


def test_style_custom_has_single_root_block() -> None:
    """Exactly one :root { … } block (plus optional :root[data-theme] siblings).
    Multiple bare :root blocks signal a merge gone wrong — the cascade order
    becomes load-bearing for tokens, which we don't want."""
    css = (STATIC / "style-custom.css").read_text(encoding="utf-8")
    # Match :root { (no attribute selectors after it).
    bare_root = re.findall(r"^:root\s*\{", css, flags=re.MULTILINE)
    assert len(bare_root) == 1, f"expected exactly one bare :root block, found {len(bare_root)}"


def test_canonical_primitives_defined() -> None:
    """Every primitive the design-pass migration produces must be declared
    in style-custom.css. Tasks 4–7 introduce them; this test starts failing
    after Task 3 lands and goes green when the last primitive lands."""
    css = (STATIC / "style-custom.css").read_text(encoding="utf-8")
    required = [
        # buttons
        ".btn",
        ".btn-primary",
        ".btn-secondary",
        ".btn-ghost",
        ".btn-danger",
        ".btn-required",
        # form controls
        ".search-input",
        ".filter-bar",
        ".filter-pill",
        # page header
        ".page-header",
        ".page-header__title",
        ".page-header__subtitle",
        ".page-header__actions",
        # data display
        ".data-table",
        ".empty-state",
        # global feedback
        ".toast",
    ]
    missing = [sel for sel in required if sel not in css]
    assert not missing, f"missing canonical primitive selectors: {missing}"


def test_no_deprecated_class_in_templates() -> None:
    """Templates must use canonical primitives, not legacy aliases.

    Migration tasks (8–15) drive this to green by sweeping each page; Task
    16 removes the supporting CSS aliases. A regression that re-adds one of
    these class names fails the build.
    """
    offenders: dict[str, list[str]] = {}
    for path in _all_html():
        text = path.read_text(encoding="utf-8")
        used = _classes_in_template(text)
        for cls in DEPRECATED_CLASSES:
            if cls in used:
                offenders.setdefault(cls, []).append(path.name)
    assert not offenders, "deprecated classes found in templates:\n" + "\n".join(
        f"  .{cls} → use {DEPRECATED_CLASSES[cls]} ({sorted(files)})" for cls, files in offenders.items()
    )


_LEGACY_TOKEN_FALLBACK_ALLOWLIST: set[str] = set()
# Allowlist drained — every template now references --ds-primary explicitly
# (#419 follow-up sweep). The stricter
# `test_no_unprefixed_primary_token_in_templates` guards regressions; the
# old `var(--primary, #hex)` fallback pattern this test catches is no
# longer present in any tracked file. Re-populate if a future PR
# legitimately needs an interim fallback.


def test_no_legacy_primary_token_with_hex_fallback() -> None:
    """var(--primary, #XXXXXX) encodes the old blue colour as a fallback.
    If the compat shim in design-tokens.css is ever removed the fallback
    fires and the element reverts to blue. Use var(--ds-primary) instead.

    Files in _LEGACY_TOKEN_FALLBACK_ALLOWLIST are known-unconverted templates
    tracked for cleanup in dedicated follow-up PRs — remove from the list
    as each template is converted."""
    pattern = re.compile(r"var\(--primary\s*,\s*#")
    offenders: list[str] = []
    for path in _all_html():
        if path.name in _LEGACY_TOKEN_FALLBACK_ALLOWLIST:
            continue
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path))
    assert not offenders, "var(--primary, #<hex>) found — use var(--ds-primary) instead:\n" + "\n".join(
        f"  {p}" for p in offenders
    )


_NO_RAW_HEX_TEMPLATES = (
    "profile.html",
    "setup.html",
    "me_activity.html",
    # Paper-theme rebrand sweep: page-local colours moved to --ds-* tokens.
    # (marketplace_plugin_detail.html is intentionally excluded — its dark
    # hero + terminal-mock retain fixed hex by design.)
    "memory_domain_detail.html",
    "activity_center.html",
)


def test_swept_templates_use_no_raw_hex() -> None:
    """The #419 follow-up sweep targets three templates that previously
    inlined raw `#RRGGBB` color literals. After conversion every colour
    must reference a `--ds-*` token instead — adding a new raw hex regress
    the sweep silently otherwise."""
    pattern = re.compile(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b")
    offenders: dict[str, list[str]] = {}
    for name in _NO_RAW_HEX_TEMPLATES:
        text = (TEMPLATES / name).read_text(encoding="utf-8")
        hexes = pattern.findall(text)
        if hexes:
            offenders[name] = hexes
    assert not offenders, "raw hex literals found in swept templates:\n" + "\n".join(
        f"  {n}: {hs}" for n, hs in offenders.items()
    )


# (template, css selector) pairs whose BACKGROUND must resolve through a
# `--ds-*` token. Narrower than the sweep above on purpose: these are the
# surfaces where a fixed light background sits under theme-aware text, so a
# literal there is not a cosmetic drift — it makes the row unreadable in the
# other theme. #1193 was exactly this: `.token-card.is-revoked` carried a
# hardcoded near-white while `.token-name` followed `--text-secondary`.
_THEME_AWARE_SURFACES = (
    ("_profile_tokens.html", ".token-card.is-revoked"),
    ("_profile_tokens.html", ".token-card.is-expired"),
)


@pytest.mark.parametrize(("template", "selector"), _THEME_AWARE_SURFACES)
def test_theme_aware_surface_background_is_tokenized(template: str, selector: str) -> None:
    text = (TEMPLATES / template).read_text(encoding="utf-8")
    assert selector in text, f"{template}: selector {selector} disappeared — update _THEME_AWARE_SURFACES"
    # Grab the rule block this selector opens (selectors may be grouped, so
    # scan from the selector to the first closing brace after it).
    start = text.index(selector)
    block = text[start : text.index("}", start)]
    backgrounds = re.findall(r"background(?:-color)?\s*:\s*([^;}]+)", block)
    assert backgrounds, f"{template}: {selector} no longer sets a background — update _THEME_AWARE_SURFACES"
    for value in backgrounds:
        assert "var(--ds-" in value, (
            f"{template}: {selector} background is `{value.strip()}` — must reference a "
            "--ds-* token so it has a dark-theme value (see #1193)"
        )


# ── Contrast, computed rather than eyeballed ────────────────────────────────
#
# A tint/ink pair written as two literals is a LIGHT-THEME pair, and nothing in
# the review process notices that it stops working when the surface flips. The
# token-card status pills shipped that way and measured 1.87–2.97:1 against the
# dark card — the pill naming a token's state was the least readable thing on
# its row. Structure alone can't catch that (a literal pair looks like any
# other CSS), so this resolves the tokens and does the arithmetic.


def _relative_luminance(rgb: tuple[float, float, float]) -> float:
    def channel(c: float) -> float:
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _parse_color(value: str) -> tuple[float, float, float, float]:
    """`#rgb` / `#rrggbb` / `rgba(r, g, b, a)` → (r, g, b, alpha)."""
    value = value.strip()
    m = re.fullmatch(r"rgba?\(([^)]+)\)", value)
    if m:
        parts = [p.strip() for p in m.group(1).split(",")]
        r, g, b = (float(p) for p in parts[:3])
        return r, g, b, float(parts[3]) if len(parts) > 3 else 1.0
    hexpart = value.lstrip("#")
    if len(hexpart) == 3:
        hexpart = "".join(c * 2 for c in hexpart)
    return (*(int(hexpart[i : i + 2], 16) for i in (0, 2, 4)), 1.0)  # type: ignore[return-value]


def _contrast(ink: str, tint: str, surface: str) -> float:
    """Contrast of `ink` over `tint` composited on `surface`."""
    ir, ig, ib, _ = _parse_color(ink)
    tr, tg, tb, ta = _parse_color(tint)
    sr, sg, sb, _ = _parse_color(surface)
    bg = tuple(ta * t + (1 - ta) * s for t, s in zip((tr, tg, tb), (sr, sg, sb)))
    li, lb = _relative_luminance((ir, ig, ib)), _relative_luminance(bg)  # type: ignore[arg-type]
    hi, lo = max(li, lb), min(li, lb)
    return (hi + 0.05) / (lo + 0.05)


def _dark_theme_tokens() -> dict[str, str]:
    """Every `--ds-*` value a plain `<html data-theme="dark">` page resolves to.

    Dark is declared across MORE THAN ONE `:root[data-theme="dark"]` block (the
    accents in one, the surfaces in another), so a reader that stops at the
    first one silently returns light-theme values and the contrast arithmetic
    comes out meaningless. Blocks are merged in document order, later winning,
    which is what the cascade does. The
    `:root[data-theme="dark"][data-theme-variant="blue"]` block is deliberately
    excluded — it needs a second attribute that the default dark page does not
    carry, so the anchored `\\s*\\{` match skips it.
    """
    css = (STATIC / "css" / "design-tokens.css").read_text(encoding="utf-8")
    tokens: dict[str, str] = {}
    blocks = list(re.finditer(r':root\[data-theme="dark"\]\s*\{', css))
    assert blocks, 'no `:root[data-theme="dark"]` block found — did the token file move?'
    for m in blocks:
        block = css[m.end() : css.index("\n}", m.end())]
        tokens.update({name: value.strip() for name, value in re.findall(r"(--ds-[\w-]+)\s*:\s*([^;]+);", block)})
    return tokens


# Each entry: the pill's tint token, its ink token, and the card it sits on.
# Dark is the theme the pills failed in and the one the contract pins; the
# light themes were already passing and are not re-litigated here.
_PILL_PAIRS = (
    ("status-active", "--ds-accent-success-bg", "--ds-accent-success-ink"),
    ("status-expiring", "--ds-accent-warn-bg", "--ds-accent-warn-ink"),
    ("status-expired", "--ds-accent-danger-bg", "--ds-accent-danger-ink"),
    ("status-revoked", "--ds-surface-dim", "--ds-text-secondary"),
)


@pytest.mark.parametrize(("pill", "tint_token", "ink_token"), _PILL_PAIRS)
@pytest.mark.parametrize("card_token", ["--ds-surface", "--ds-surface-sunken"])
def test_token_status_pills_are_readable_in_dark_theme(
    pill: str, tint_token: str, ink_token: str, card_token: str
) -> None:
    """Both cards, because a revoked token's pill sits on the sunken one."""
    tokens = _dark_theme_tokens()
    for token in (tint_token, ink_token, card_token):
        assert token in tokens, f"{token} has no dark-theme value — the pill would inherit a light-theme colour"
    ratio = _contrast(tokens[ink_token], tokens[tint_token], tokens[card_token])
    assert ratio >= 4.5, (
        f".status-pill.{pill} on {card_token} measures {ratio:.2f}:1 in dark theme "
        f"({ink_token} on {tint_token}) — below the 4.5:1 floor (see #1193)"
    )


def test_status_pills_reference_tokens_not_literals() -> None:
    """The structural half: a literal pair is what fails silently on a theme
    flip, so none may appear on these rules in the first place."""
    text = (TEMPLATES / "_profile_tokens.html").read_text(encoding="utf-8")
    for pill, _tint, _ink in _PILL_PAIRS:
        rules = re.findall(rf"\.status-pill\.{pill}\b[^{{]*\{{([^}}]*)\}}", text)
        assert rules, f".status-pill.{pill} rule disappeared — update _PILL_PAIRS"
        for body in rules:
            assert not re.search(r"#[0-9a-fA-F]{3,6}\b|rgba?\(", body), (
                f".status-pill.{pill} carries a literal colour (`{body.strip()}`) — "
                "tint/ink pairs must be --ds-* tokens so they survive a theme flip (#1193)"
            )


def test_no_unprefixed_primary_token_in_templates() -> None:
    """`var(--primary)` (no `--ds-` prefix) rides the legacy blue token via
    the compat shim in design-tokens.css. Explicit `var(--ds-primary)`
    reads self-documenting in code review and survives a future shim
    removal.

    Per #419 follow-up sweep: every template MUST reference `--ds-primary`
    explicitly. `base.html` and `base_ds.html` are exempt — both only
    mention `--primary` inside CSS-comment blocks documenting the legacy
    compat shim, not as live token references.
    """
    pattern = re.compile(r"var\(\s*--primary[-)\s,]")
    exempt = {"base.html", "base_ds.html"}
    offenders: list[str] = []
    for path in _all_html():
        if path.name in exempt:
            continue
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path))
    assert not offenders, "`var(--primary…)` found — use `var(--ds-primary…)` instead:\n" + "\n".join(
        f"  {p}" for p in offenders
    )


_COMPONENTS_HTML = TEMPLATES / "_components.html"

# Button macro composes ['btn', 'btn-' ~ variant] + optional 'btn-' ~ size
# + 'btn--icon' (see _components.html:44). These are the variants actually
# emitted across the codebase today — re-survey if a new variant is added.
_BUTTON_VARIANTS = ("primary", "secondary", "ghost", "danger", "google", "required")
_BUTTON_SIZES = ("sm", "lg")

# CSS files where canonical rules live. Class-coverage is satisfied if the
# selector appears in ANY of these. The four sheets imported by base.html
# and base_ds.html (style-custom + components + design-tokens + stack_card)
# are globally loaded; the per-page sheets under `css/*.css` ship with the
# pages whose macros use them — coverage is still satisfied because the
# macro emits the class only on pages that load the matching sheet.
_CANONICAL_CSS = (
    STATIC / "style-custom.css",
    *sorted((STATIC / "css").glob("*.css")),
)


def test_component_macros_emit_only_classes_with_css_rules() -> None:
    """Every class token a macro in `_components.html` emits MUST resolve
    to a CSS rule in one of the canonical sheets (style-custom.css,
    components.css, design-tokens.css). A typo'd class on a macro renders
    nothing — this contract catches it before the macro ships.

    Approach: static extraction (no Jinja render). Literal classes are
    pulled from `class="…"` attribute values in `_components.html`,
    Jinja-templated portions (`{{ … }}` / `{% … %}`) skipped, and the
    button macro's computed `btn-<variant>` / `btn-<size>` classes are
    enumerated from the documented variant tuples above.
    """
    text = _COMPONENTS_HTML.read_text(encoding="utf-8")

    # Strip Jinja blocks/expressions/comments before tokenising — we only
    # want the literal class strings the author wrote, not Jinja runtime
    # gunk or comment-block examples (`{# … class="…" … #}`).
    jinja_free = re.sub(
        r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}",
        " ",
        text,
        flags=re.DOTALL,
    )

    static_classes: set[str] = set()
    for m in _CLASS_ATTR_RE.finditer(jinja_free):
        for token in m.group(2).split():
            if "{" in token or "}" in token:
                continue
            static_classes.add(token)

    # Button macro variants + sizes that get composed at runtime.
    button_classes = {"btn", "btn--icon"}
    button_classes.update(f"btn-{v}" for v in _BUTTON_VARIANTS)
    button_classes.update(f"btn-{s}" for s in _BUTTON_SIZES)

    # T11-T17 macros compose variant-driven root classes (variant arg ⇒
    # different selector) and bespoke accent modifiers. Enumerate the
    # documented variant values explicitly so a typo in the macro fails
    # this contract loudly.
    variant_classes: set[str] = {
        # tabs_rich
        "mp-tabs",
        "stack-tabs",
        # segmented_strip
        "os-tabs",
        "mode-tabs",
        # hero_search_btn
        "search-btn",
        "stack-hero__search-btn",
        # info_panel_accent — all four canonical accents
        "info-panel-accent",
        "info-panel-accent--info",
        "info-panel-accent--warn",
        "info-panel-accent--success",
        "info-panel-accent--danger",
    }

    expected = static_classes | button_classes | variant_classes
    assert expected, "extracted no classes from _components.html — extraction broken"

    # Load every canonical sheet once.
    css_blob = "\n".join(p.read_text(encoding="utf-8") for p in _CANONICAL_CSS)

    missing: list[str] = []
    for cls in sorted(expected):
        # Selector match: `.cls` followed by a non-class-name char so
        # `.btn` doesn't match `.btn-primary`. CSS rules also appear in
        # compound selectors (`.btn.is-active`) — the simple lookahead
        # is enough because we only need ONE occurrence.
        if not re.search(r"\." + re.escape(cls) + r"(?![\w-])", css_blob):
            missing.append(cls)
    assert not missing, (
        f"_components.html emits classes with no CSS rule in any of "
        f"{[str(p) for p in _CANONICAL_CSS]}:\n" + "\n".join(f"  .{m}" for m in missing)
    )


def test_app_js_referenced_by_base_only() -> None:
    """app.js carries dropdown wiring scoped to the authed nav. base_login.html
    has no nav, so it must NOT load app.js — that would let login pages call
    window.appUI / window.appToast (defined later), which is not their
    contract. The opposite (base.html missing app.js) would break the
    Admin dropdown."""
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    base_login = (TEMPLATES / "base_login.html").read_text(encoding="utf-8")
    assert "app.js" in base, "base.html must load app.js"
    assert "app.js" not in base_login, "base_login.html must not load app.js"


# Helper-level unit tests for the class-tokenizer itself — keeps the
# audit logic honest as the design-pass evolves.


def test_classes_helper_multiline_attr() -> None:
    """class= attributes split across lines (typical Jinja conditional
    pattern) must still tokenize cleanly."""
    sample = """
    <a class="app-nav-link
        is-active"
       href="/">Home</a>
    """
    assert _classes_in_template(sample) == {"app-nav-link", "is-active"}


def test_classes_helper_skips_jinja_tokens() -> None:
    """Jinja-constructed class fragments don't get audited (can't be statically
    resolved). Verify the {% if %}, {{ … }} pieces are filtered out, real
    literal tokens around them stay."""
    sample = """<button class="btn {% if active %}is-active{% endif %} btn-primary">Go</button>"""
    tokens = _classes_in_template(sample)
    assert "btn" in tokens
    assert "btn-primary" in tokens
    # Jinja control-flow tokens get skipped — they contain `{` or `}`.
    for tok in tokens:
        assert "{" not in tok and "}" not in tok


def test_classes_helper_compound_match_is_not_false_positive() -> None:
    """Prose containing the word 'pill' or 'btn' in a comment should NOT be
    detected as a deprecated class. Only class= attribute values count."""
    sample = """
    <!-- this is the filter pill row -->
    <p>The button (btn) below opens the menu.</p>
    <span class="badge">x</span>
    """
    assert _classes_in_template(sample) == {"badge"}


# --------------------------------------------------------------------------- #
# #367 — page-shell layout guards.
#
# Leaf templates must not re-introduce the per-page chrome drift the
# design-system page-shell (`base_page.html` / `base_ds.html` + `.container`)
# exists to remove: container opt-outs and bare `:root` token-shadow blocks.
# The canonical bases + theme shim are exempt.
#
# NOTE: a broad `.X-page { max-width }` fence is intentionally NOT added yet —
# many pages still carry legitimate inner-width wrappers pending the full
# base_page.html migration (tracked as a #367 follow-up). It would false-
# positive today.
# --------------------------------------------------------------------------- #

# Templates allowed to own layout/theme CSS (the canonical bases + theme shim).
_CANONICAL_LAYOUT_FILES = {"base.html", "base_ds.html", "base_page.html", "_theme.html"}

_JINJA_RE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", re.DOTALL)
_STYLE_BLOCK_RE = re.compile(r"<style[^>]*>(.*?)</style>", re.DOTALL | re.IGNORECASE)


def _inline_styles_in_template(text: str) -> str:
    """Concatenated content of a template's inline <style> blocks, with Jinja
    expressions / blocks / comments stripped first so documented examples
    inside `{# … #}` don't trip the scanners."""
    jinja_free = _JINJA_RE.sub(" ", text)
    return "\n".join(_STYLE_BLOCK_RE.findall(jinja_free))


def test_no_container_has_optout_in_leaf_templates() -> None:
    """`.container:has(.X-page) { max-width: none }` is the per-page container
    opt-out the page-shell replaced (#367). Width changes belong on the
    canonical `.container--wide/--narrow/--full` modifiers, not a leaf opt-out."""
    offenders: list[str] = []
    for path in _all_html():
        if path.name in _CANONICAL_LAYOUT_FILES:
            continue
        if ".container:has(" in _inline_styles_in_template(path.read_text(encoding="utf-8")):
            offenders.append(str(path))
    assert not offenders, (
        "`.container:has(` opt-out found in a leaf template — use a canonical "
        "`.container--wide/--narrow/--full` modifier instead:\n" + "\n".join(f"  {p}" for p in offenders)
    )


# `body.X-page .container { max-width: … }` — the same opt-out written with a
# descendant selector instead of `:has()`, and the one the `:has()` guard above
# does not see. It outranks the canonical modifiers (0,2,1 beats `.container--full`'s
# 0,1,0), so a leaf template silently re-narrows and re-centres the whole page
# shell — including the admin sidebar, which base_admin.html deliberately keeps
# flush with the rail.
#
# `(?![\w-])` so sibling classes of their own (`.container-memory`) and the
# canonical modifiers (`.container--wide`) are not read as the shell itself.
_CONTAINER_GEOMETRY_RE = re.compile(
    r"[^{}]*\.container(?![\w-])[^{}]*\{[^}]*\b(?:max-width|min-width|width|padding(?:-(?:left|right))?|margin)\s*:",
    re.MULTILINE,
)


def test_no_container_geometry_override_in_leaf_templates() -> None:
    """A leaf template must not redeclare the page shell's box geometry on
    `.container`. Width/gutters belong to the canonical
    `.container--wide/--narrow/--full` modifiers, which any page-scoped
    descendant selector would silently outrank."""
    offenders: list[str] = []
    for path in _all_html():
        if path.name in _CANONICAL_LAYOUT_FILES:
            continue
        css = _inline_styles_in_template(path.read_text(encoding="utf-8"))
        # Strip CSS comments — several templates *document* the shell's rules.
        css = re.sub(r"/\*.*?\*/", " ", css, flags=re.DOTALL)
        for match in _CONTAINER_GEOMETRY_RE.finditer(css):
            offenders.append(f"{path}: {' '.join(match.group(0).split())[:90]}")
    assert not offenders, (
        "`.container` geometry override found in a leaf template — set width via a "
        "canonical `.container--wide/--narrow/--full` modifier on the "
        "`container_modifier` block instead:\n" + "\n".join(f"  {o}" for o in offenders)
    )


def test_no_bare_root_block_in_leaf_templates() -> None:
    """A bare `:root { … }` block in a leaf template shadows the canonical
    design tokens (cf. the `admin_tables :root{--primary:…}` collapse, #367).
    Only `_theme.html` and the bases may declare `:root`."""
    pattern = re.compile(r":root\s*\{")
    offenders: list[str] = []
    for path in _all_html():
        if path.name in _CANONICAL_LAYOUT_FILES:
            continue
        if pattern.search(_inline_styles_in_template(path.read_text(encoding="utf-8"))):
            offenders.append(str(path))
    assert not offenders, (
        "bare `:root {` block found in a leaf template — design tokens live in "
        "design-tokens.css / _theme.html, not per-page:\n" + "\n".join(f"  {p}" for p in offenders)
    )


def test_base_ds_carries_operator_custom_scripts() -> None:
    """`base_ds.html` (and thus `base_page.html`) must fire all three operator
    `custom_scripts` placements like `base.html` does. Without them every page
    migrated onto the design-system base silently drops operator-injected
    analytics / feedback widgets (#367 base_ds parity; surfaced during the #482
    page migration). `test_custom_scripts_render.py` proves the loop mechanism
    renders; this guard just keeps the loops present in base_ds."""
    text = (TEMPLATES / "base_ds.html").read_text(encoding="utf-8")
    missing = [p for p in ("head_start", "head_end", "body_end") if f"s.placement == '{p}'" not in text]
    assert not missing, (
        f"base_ds.html is missing operator custom_scripts loop(s): {missing} — "
        "pages migrated onto base_ds will drop operator scripts"
    )


# Bases that legitimately own the <html>/<head>/<body> scaffold. Every other
# page-level template must `{% extends %}` one of these (directly, or via the
# base_page → base_ds chain) rather than ship its own standalone document.
_PAGE_SCAFFOLD_BASES = {
    "base.html",
    "base_ds.html",
    "base_page.html",
    "base_login.html",
}

# Leaf pages still on a standalone scaffold, pending migration onto the
# design-system base. Entries are tolerated ONLY so the guard locks in today's
# state — drop an entry when its page is migrated, and never add a new one (a
# fresh standalone is exactly the regression this guard exists to block).
#
# ONE deliberate, permanent exception: `data_app_waking.html` (Task 8, the
# data-apps ingress proxy's holding page — app/api/data_apps_proxy.py). This
# isn't "forgot to extend a base" migration debt — it's categorically not an
# Agnes UI page at all. It renders while a user is waiting on someone ELSE's
# hosted app to wake up, is served from inside `/apps/{slug}/...` (often
# embedded in that app's own iframe/deep link), and must never carry Agnes's
# own nav/theme/branding chrome into that context. Same rationale as why
# `base_login.html` exists as its own scaffold rather than forcing every
# pre-auth page onto base_ds.
_STANDALONE_ALLOWLIST: set[str] = {"data_app_waking.html"}

_EXTENDS_RE = re.compile(r"\{%-?\s*extends")
_SCAFFOLD_RE = re.compile(r"<!DOCTYPE|<html[\s>]", re.IGNORECASE)


def test_no_new_standalone_page_templates() -> None:
    """Page-level templates must extend a design-system base, not ship their
    own <html>/<head>/<body>. Anti-regression guard for the standalone→base_ds
    migration (#284/#481/#482): before it, shared infrastructure (app.js, the
    theme include, the nav, the Inter font) lived only in base.html, so any
    standalone page silently lost it — the original dead Admin-dropdown bug on
    /catalog, /admin/tables, /corporate-memory. Partials (`_`-prefixed) and the
    bases themselves are exempt; known-standalone leaves sit in
    _STANDALONE_ALLOWLIST until migrated."""
    offenders: list[str] = []
    for path in _all_html():
        name = path.name
        if name.startswith("_") or name in _PAGE_SCAFFOLD_BASES or name in _STANDALONE_ALLOWLIST:
            continue
        text = path.read_text(encoding="utf-8")
        if _EXTENDS_RE.search(text):
            continue
        # No `{% extends %}`: a regression only if it ships a real page
        # scaffold. A non-`_` include fragment without one is harmless.
        if _SCAFFOLD_RE.search(text):
            offenders.append(str(path.relative_to(TEMPLATES)))
    assert not offenders, (
        "standalone page template(s) found — extend a design-system base "
        "(base_page.html / base_ds.html) instead of shipping your own "
        "<html>/<head>/<body>:\n" + "\n".join(f"  {o}" for o in offenders)
    )


def test_setup_html_uses_design_system_base() -> None:
    """The first-time-setup wizard (`setup.html`, served at /first-time-setup)
    must ride the canonical design-system base, not the bespoke
    `base_login.html` card chrome (#586). It opts into the 800px narrow shell
    via `.container--narrow` and carries none of the login-card wrapper divs
    or their hardcoded `max-width: 520px` inline widths."""
    text = (TEMPLATES / "setup.html").read_text(encoding="utf-8")
    # (a) extends the design-system base, not base_login.
    assert '{% extends "base_ds.html" %}' in text, "setup.html must extend base_ds.html"
    assert "base_login.html" not in text, "setup.html must no longer reference base_login.html"
    # (b) the hardcoded card width is gone (both inline occurrences).
    assert "max-width: 520px" not in text, "setup.html must not hardcode `max-width: 520px`"
    # (c) opts into the canonical narrow shell.
    assert "container--narrow" in text, "setup.html must opt into the .container--narrow design-system shell"
    # (d) the login-card chrome wrapper divs are removed.
    for cls in ('class="login-page"', 'class="login-card-wrapper"', 'class="login-card"'):
        assert cls not in text, f"setup.html must not carry the login-chrome wrapper ({cls})"


def test_standalone_allowlist_has_no_stale_entries() -> None:
    """Every _STANDALONE_ALLOWLIST entry must still exist AND still be a
    standalone (no `{% extends %}`). When a page is migrated onto a base its
    allowlist entry goes stale — this fails so the entry is removed, keeping
    the allowlist honest instead of silently masking a now-compliant page."""
    stale: list[str] = []
    for name in sorted(_STANDALONE_ALLOWLIST):
        path = TEMPLATES / name
        if not path.exists() or _EXTENDS_RE.search(path.read_text(encoding="utf-8")):
            stale.append(name)
    assert not stale, (
        f"stale _STANDALONE_ALLOWLIST entr(ies) — page migrated or removed, drop from the allowlist: {stale}"
    )


# --------------------------------------------------------------------------- #
# #400 — legacy section design-token migration guard.
#
# The selectors in the absorbed style.css block (~lines 130–1270 of
# style-custom.css) were migrated from hardcoded hex/rem literals to
# --* token variables in issue #400.  These tests assert that the
# migrated class families no longer contain raw literals so a future
# change cannot regress without a CI failure.
# --------------------------------------------------------------------------- #

# Selectors whose declarations must NOT contain hardcoded hex literals or
# bare rem values after the #400 migration.  The check extracts the rule body
# for each selector and scans it for raw colour/size literals.
_MIGRATED_LEGACY_SELECTORS: tuple[str, ...] = (
    ".card-error",
    ".card-highlight",
    ".card-ai",
    ".card-success",
    ".badge-analyst",
    ".badge-privileged",
    ".badge-admin",
    ".flash-success",
    ".flash-error",
    ".flash-info",
    ".flash-warning",
    ".flash-success-v2",
    ".flash-error-v2",
    ".code-block",
    ".username-box",
    ".btn-copy",
    ".btn-copy-inline",
    ".btn-copy-block",
    ".info-box",
    ".cc-setup-card",
    ".support-info",
)

# Matches a raw hex colour literal outside of a CSS comment.
_HEX_LITERAL_RE = re.compile(r"(?<!#)#[0-9a-fA-F]{3,6}\b")
# Matches a bare rem value (e.g. 0.8125rem) that is NOT inside a token definition.
_REM_LITERAL_RE = re.compile(r"\b\d+\.\d+rem\b")


def _extract_rule_body(css: str, selector: str) -> str:
    """Return the concatenated bodies of every rule block whose selector
    matches *selector* exactly (possibly followed by a pseudo-class, modifier,
    or space, but NOT by an alphanumeric/-_ character that would indicate a
    longer class name such as ``.flash-success-v2`` when checking
    ``.flash-success``).  Returns an empty string if the selector is not found."""
    escaped = re.escape(selector)
    # The selector token must end at a word boundary: followed by whitespace,
    # `{`, `:`, `.`, `+`, `>`, `~`, or `[` — anything that is a valid CSS
    # combinator or the start of a rule block — but NOT another word character
    # (which would indicate ``.flash-success-v2`` etc.).
    pattern = re.compile(
        escaped + r"(?![\w-])[^{]*\{([^}]*)\}",
        re.DOTALL,
    )
    bodies = pattern.findall(css)
    return "\n".join(bodies)


def test_legacy_selectors_use_no_raw_hex_literals() -> None:
    """Migrated legacy selectors must not contain hardcoded hex colours.

    Each selector in _MIGRATED_LEGACY_SELECTORS was converted in #400 to
    reference a --* token instead of a raw ``#RRGGBB`` literal.  A future
    edit that re-introduces a literal breaks the operator-override contract
    (instance.yaml overrides only propagate through token variables).
    """
    css = (STATIC / "style-custom.css").read_text(encoding="utf-8")
    offenders: dict[str, list[str]] = {}
    for sel in _MIGRATED_LEGACY_SELECTORS:
        body = _extract_rule_body(css, sel)
        # Strip CSS-comment lines (/* … */) before scanning so documented
        # token values inside comments don't trigger false positives.
        body_no_comments = re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL)
        hits = _HEX_LITERAL_RE.findall(body_no_comments)
        if hits:
            offenders[sel] = hits
    assert not offenders, (
        "hardcoded hex literals found in migrated legacy selectors "
        "(use a --* token instead):\n" + "\n".join(f"  {sel}: {vals}" for sel, vals in offenders.items())
    )


def test_legacy_selectors_use_no_bare_rem_literals() -> None:
    """Migrated legacy selectors must not contain bare rem font-size values.

    The #400 migration replaced rem literals (e.g. ``0.8125rem``) with
    ``var(--text-13)`` / ``var(--text-15)`` etc. so operator font-scale
    overrides propagate correctly.
    """
    css = (STATIC / "style-custom.css").read_text(encoding="utf-8")
    offenders: dict[str, list[str]] = {}
    for sel in _MIGRATED_LEGACY_SELECTORS:
        body = _extract_rule_body(css, sel)
        body_no_comments = re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL)
        hits = _REM_LITERAL_RE.findall(body_no_comments)
        if hits:
            offenders[sel] = hits
    assert not offenders, (
        "bare rem literals found in migrated legacy selectors "
        "(use a --text-* token instead):\n" + "\n".join(f"  {sel}: {vals}" for sel, vals in offenders.items())
    )


def test_legacy_token_aliases_defined_in_root() -> None:
    """The new tokens added by #400 must be declared in the :root block.

    A missing token declaration means the selector falls back to ``unset``
    (invisible / zero-size) when no operator override is supplied.
    """
    css = (STATIC / "style-custom.css").read_text(encoding="utf-8")
    required_tokens = [
        "--text-13",
        "--text-15",
        "--text-22",
        "--text-28",
        "--card-error-bg",
        "--card-highlight-bg",
        "--card-ai-bg",
        "--card-success-bg",
        "--badge-success-bg",
        "--badge-success-ink",
        "--badge-warn-bg",
        "--badge-warn-ink",
        "--badge-info-bg",
        "--badge-info-ink",
        "--flash-success-bg",
        "--flash-success-ink",
        "--flash-success-border",
        "--flash-error-bg",
        "--flash-error-ink",
        "--flash-error-border",
        "--flash-info-bg",
        "--flash-info-ink",
        "--flash-info-border",
        "--flash-warn-bg",
        "--flash-warn-ink",
        "--flash-warn-border",
        "--code-dark-bg",
        "--code-dark-ink",
        "--code-dark-border",
        "--code-wrapper-ink",
        "--placeholder-color",
        "--username-box-bg",
        "--username-box-line",
        "--username-ink",
        "--btn-copy-bg",
        "--btn-copy-hover",
        "--btn-copy-success",
        "--info-box-bg",
        "--info-box-border",
        "--cc-gradient-from",
        "--cc-gradient-to",
        "--support-info-bg",
        "--username-preview-color",
        "--flash-success-v2-ink",
        "--flash-error-v2-ink",
    ]
    missing = [t for t in required_tokens if f"{t}:" not in css]
    assert not missing, "tokens introduced by #400 are missing from style-custom.css :root:\n" + "\n".join(
        f"  {t}" for t in missing
    )


# ── Light-only tint guard ────────────────────────────────────────────────
# The `--ds-*-soft` family is the DS tint vocabulary, and every one of them is
# declared ONCE in the global `:root` — none has a `data-theme="dark"`
# override, so a `-soft` fill stays near-white under dark while the
# `--ds-text-*` ink flips near-white with the theme. That pairing is invisible
# text, and it shipped once: `.cbn` (the connect banner) filled with
# `--ds-agnes-soft` and drew its title in `--ds-text-primary`, which measured
# ~1:1 in dark mode. The fix is a dark-scoped override tinting the CURRENT
# surface (`color-mix(… var(--ds-agnes) 18%, var(--ds-surface))`), the idiom
# `.ag-pv-bubble` already used. This guard stops the next one.
#
# Deliberately NOT flagged, because they are correct:
#   - a `-soft` fill whose own ink is a deep accent (`--ds-agnes`,
#     `--ds-kind-*`): dark-on-light stays legible when the tint can't flip,
#     so `.ag-ava` / `.ag-instack` are fine.
#   - `--ds-text-inverse` ink: that token IS the pairing for solid fills.
#   - rules already scoped to a `[data-theme="…"]`: a dark block carries its
#     own colours, and a light theme (`paper`, `blue`, `navy`) can never be
#     the active theme when the app is dark.
_TOKENS_CSS = STATIC / "css" / "design-tokens.css"

_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_THEME_SCOPED_RE = re.compile(r'\[data-theme="[a-z]+"\]')
_TOKEN_DECL_RE = re.compile(r"(--ds-[a-z0-9-]+)\s*:")
_BACKGROUND_DECL_RE = re.compile(r"^\s*background(-color)?\s*:")
# `color:` but not `-color:` (so `border-color`/`background-color` don't count
# as ink), and not the inverse token (the deliberate solid-fill pairing).
_FLIPPING_INK_RE = re.compile(r"(?<![a-z-])color\s*:\s*var\(--ds-text-(?!inverse)")


def _css_rules(css: str) -> list[tuple[str, str]]:
    """Every declaration block as `(selector, body)`.

    At-rule wrappers (`@media`, `@supports`) are transparent — their nested
    rules are returned with their own selectors, so a rule inside a media
    query is audited like any other. Comments are stripped first.
    """
    css = _CSS_COMMENT_RE.sub("", css)
    rules: list[tuple[str, str]] = []
    stack: list[str | None] = []
    buf = ""
    for ch in css:
        if ch == "{":
            selector = buf.strip()
            buf = ""
            # `None` marks a wrapper whose body holds rules, not declarations.
            stack.append(None if selector.startswith("@") else selector)
        elif ch == "}":
            closed = stack.pop() if stack else None
            if closed is not None:
                rules.append((closed, buf))
            buf = ""
        else:
            buf += ch
    return rules


def _light_only_soft_tokens() -> set[str]:
    """`--ds-*-soft` tokens with no `data-theme="dark"` declaration anywhere in
    design-tokens.css — i.e. the tints that keep their light value under dark."""
    rules = _css_rules(_TOKENS_CSS.read_text(encoding="utf-8"))
    light: set[str] = set()
    dark: set[str] = set()
    for selector, body in rules:
        target = dark if '[data-theme="dark"]' in selector else light
        target.update(_TOKEN_DECL_RE.findall(body))
    return {t for t in light - dark if t.endswith("-soft")}


def _anchor_class(selector_part: str) -> str | None:
    """The rightmost class in a selector — the element the background paints."""
    classes = re.findall(r"\.([A-Za-z0-9_-]+)", selector_part)
    return classes[-1] if classes else None


def _paints_ink_on(anchor: str, selector_part: str, candidate: str) -> bool:
    """Whether `candidate`'s ink lands on the background painted by
    `selector_part`: either the very same rule, or a descendant / BEM child of
    the anchor class (`.cbn` → `.cbn-title`, `.cbn__title`, `.cbn .title`)."""
    if candidate.strip() == selector_part:
        return True
    return re.search(rf"\.{re.escape(anchor)}(?:[-_][A-Za-z0-9_-]+)?(?=[\s:.\[]|$)", candidate) is not None


def light_only_tint_offenders(css: str, light_only: set[str]) -> list[str]:
    """Rules that fill with a light-only `-soft` tint while ink that flips with
    the theme is drawn on them, and that ship no dark-theme override.

    The override has to live in the same stylesheet as the rule it fixes —
    keeping the two together is the point, and a cross-file search would make
    the guard depend on file load order.
    """
    rules = _css_rules(css)
    offenders: list[str] = []
    for selector, body in rules:
        if _THEME_SCOPED_RE.search(selector):
            continue
        for declaration in body.split(";"):
            if not _BACKGROUND_DECL_RE.match(declaration):
                continue
            tints = sorted(t for t in light_only if f"var({t})" in declaration)
            if not tints:
                continue
            for part in (p.strip() for p in selector.split(",")):
                anchor = _anchor_class(part)
                if anchor is None:
                    continue
                inked_by = [
                    other
                    for other, other_body in rules
                    if _FLIPPING_INK_RE.search(other_body)
                    and any(_paints_ink_on(anchor, part, p) for p in other.split(","))
                ]
                if not inked_by:
                    continue
                has_dark_override = any(
                    '[data-theme="dark"]' in other and re.search(rf"\.{re.escape(anchor)}\b", other)
                    for other, _ in rules
                )
                if has_dark_override:
                    continue
                offenders.append(f"{part} (fill {', '.join(tints)}; ink from {inked_by[0].strip()})")
    return offenders


def _css_sources() -> dict[str, str]:
    """Every stylesheet the app ships, plus each template's inline <style>."""
    sources = {str(p): p.read_text(encoding="utf-8") for p in sorted(STATIC.rglob("*.css"))}
    for path in _all_html():
        inline = _inline_styles_in_template(path.read_text(encoding="utf-8"))
        if inline.strip():
            sources[str(path)] = inline
    return sources


def test_light_only_soft_tint_never_backs_theme_flipping_ink() -> None:
    """A `--ds-*-soft` fill + `--ds-text-*` ink needs a dark-theme override.

    Without one the fill stays light while the ink goes light — the connect
    banner's title measured ~1:1 in dark mode this way. Fix a finding either by
    tinting the current surface under `:root[data-theme="dark"]`
    (`color-mix(in srgb, var(--ds-accent) N%, var(--ds-surface))`) or by giving
    the component ink that does not flip.
    """
    light_only = _light_only_soft_tokens()
    assert light_only, "expected the --ds-*-soft tints to have no dark-theme override — detector needs rewriting"

    offenders = {
        name: found for name, css in _css_sources().items() if (found := light_only_tint_offenders(css, light_only))
    }
    assert not offenders, "light-only -soft tint painted under theme-flipping ink with no dark override:\n" + "\n".join(
        f"  {name}:\n" + "\n".join(f"    {o}" for o in found) for name, found in offenders.items()
    )


_REGRESSION_CSS = """
.cbn { background: var(--ds-agnes-soft); border-radius: 16px; }
.cbn-title { color: var(--ds-text-primary); font-weight: 750; }
"""


def test_light_only_tint_detector_fires_on_the_connect_banner_regression() -> None:
    """The exact pre-fix shape must be reported, or the guard above is vacuous."""
    offenders = light_only_tint_offenders(_REGRESSION_CSS, {"--ds-agnes-soft"})
    assert len(offenders) == 1, offenders
    assert offenders[0].startswith(".cbn ")


def test_light_only_tint_detector_accepts_a_dark_override() -> None:
    """The shipped fix — a dark-scoped fill over the current surface — is clean."""
    fixed = _REGRESSION_CSS + (
        ':root[data-theme="dark"] .cbn { background: color-mix(in srgb, var(--ds-agnes) 18%, var(--ds-surface)); }'
    )
    assert light_only_tint_offenders(fixed, {"--ds-agnes-soft"}) == []


def test_light_only_tint_detector_ignores_accent_ink_on_a_tint() -> None:
    """A tint whose own ink is a deep accent is legible in both themes."""
    chip_css = ".ag-instack { background: var(--ds-agnes-soft); color: var(--ds-agnes); }"
    assert light_only_tint_offenders(chip_css, {"--ds-agnes-soft"}) == []


def test_light_only_tint_detector_flattens_media_queries() -> None:
    """A rule inside `@media` is audited like any other — nesting is not a hole."""
    nested = "@media (max-width: 620px) {" + _REGRESSION_CSS + "}"
    assert len(light_only_tint_offenders(nested, {"--ds-agnes-soft"})) == 1


def test_plugin_detail_chip_icons_are_sized_wherever_the_chip_renders() -> None:
    """`buildInnerCardChip()` emits ONE chip into TWO structures — the legacy
    photo card's body and the redesigned object row's trailing meta slot — so its
    stylesheet must be scoped to the page, not to `.inner-card`.

    The regression: the redesign moved the chip into `.detail-object__meta`,
    outside `.inner-card`, and the whole rule block (including
    `width: 13px; height: 13px` on the inline SVGs) stopped matching. Inline SVG
    with a `viewBox` and no dimensions falls back to the intrinsic
    replaced-element box, so each glyph rendered at ~160px — three of them across
    the row, in a chip meant to be 13px punctuation.
    """
    src = (TEMPLATES / "marketplace_plugin_detail.html").read_text()
    # The redesigned renderer really does put the chip outside `.inner-card` —
    # if this changes, the rest of this test is asserting about nothing. Matched
    # loosely (bare call or hoisted local) so this stays a check on WHERE the
    # chip lands, not on how the renderer spells it.
    assert re.search(r'class="detail-object__meta">\$\{\s*(?:chip|buildInnerCardChip\(it\))\s*\}', src), (
        "the object-row renderer no longer wraps the chip in .detail-object__meta"
    )
    assert ".plugin-detail .inv-chip svg" in src, "the chip's SVG size rule is missing"
    assert ".inner-card .inv-chip" not in src, (
        "chip rules are scoped to .inner-card again — the object-row chip loses all of them, including the SVG size"
    )


# --------------------------------------------------------------------------- #
# #497 §1 — native-dialog contract guard.
#
# PR #508 converted ~98 confirm()/alert()/prompt() calls to the promise-based
# window.confirmModal/alertModal/promptModal (app/web/static/js/modal.js,
# loaded on every page via _app_scripts.html — see the block comment there).
# Nothing then stopped the pattern from creeping back in: the 2026-08-13
# re-audit counted native calls across 11 files, and data_app_detail.html
# (added 2026-08-14, after modal.js was already loaded on every page) shipped
# 4 more alert() calls the same day. This guard stops the next one from
# landing silently.
# --------------------------------------------------------------------------- #

# Bare `alert(`/`confirm(`/`prompt(`, or the same three prefixed with
# `window.`. The leading `\b` keeps identifiers like `reconfirm(` from
# matching; requiring the `(` immediately after the name (no `\s*`) keeps
# prose like "System prompt (optional)" from matching. Deliberately does NOT
# match the Modal-suffixed replacements: "confirmModal(" never contains the
# substring "confirm(" (the character right after "confirm" is "M", not
# "("), so no extra negative lookahead is needed — proved by
# test_native_dialog_canonical_forms_do_not_trip_guard below.
_NATIVE_DIALOG_RE = re.compile(r"\b(?:window\.)?(?:alert|confirm|prompt)\(")

# Grandfather allowlist — every template carrying a native dialog call as of
# the #497 §1 re-audit (2026-08-15). MAY ONLY SHRINK: convert a page's calls
# to confirmModal()/alertModal()/promptModal() and drop its entry in the SAME
# change; never add a new one.
#
#   - `_legacy`-suffixed templates are frozen byte-for-byte (repo
#     convention) and stay grandfathered until the legacy page itself is
#     retired, not "fixed" in place.
#   - agents.html keeps exactly one `window.confirm(` on purpose —
#     tests/test_ui_layout_theme.py asserts it is present on /agents — so it
#     stays grandfathered here rather than converted.
#   - A few entries (_app_scripts.html, admin_marketplaces.html,
#     admin_tables.html, catalog_legacy.html) match only inside a `//` or
#     `<!-- -->` comment that talks ABOUT confirm()/alert() rather than
#     calling it. This guard does not try to strip those comments — a
#     JS-aware comment stripper is its own hazard (string/regex literals and
#     `https://` URLs all contain `//`) — so a harmless comment mention sits
#     in the allowlist next to the real offenders. Being here for a comment
#     is harmless; being missing here for a live call is the regression this
#     guard exists to catch.
_NATIVE_DIALOG_ALLOWLIST: set[str] = {
    "_app_scripts.html",
    "admin_chat.html",
    "admin_data_sources.html",
    "admin_linked_apps.html",
    "admin_marketplaces.html",
    "admin_mcp_source_detail.html",
    "admin_tables.html",
    "agents.html",
    "agents_legacy.html",
    "catalog_legacy.html",
    "data_app_detail_legacy.html",
    "library_detail.html",
    "library_detail_legacy.html",
    "me_connections.html",
    "me_connections_legacy.html",
    "skills.html",
}


def _native_dialog_offenders(text: str) -> list[str]:
    """Native alert()/confirm()/prompt() calls in *text* — see
    _NATIVE_DIALOG_RE. Jinja syntax is stripped first so a doc comment inside
    `{# … #}` (see _claude_setup_cta.jinja, which documents modal.js's
    fallback behaviour by name) isn't mistaken for a live call."""
    return _NATIVE_DIALOG_RE.findall(_JINJA_RE.sub(" ", text))


def test_no_native_dialog_calls_in_templates() -> None:
    """A new page must call confirmModal()/alertModal()/promptModal()
    (app/web/static/js/modal.js), never the native browser dialogs — #497
    §1. Grandfathered templates in _NATIVE_DIALOG_ALLOWLIST are pre-existing
    debt (see its comment); every other template must be clean. Iterating
    _all_html() means a grandfathered template deleted by a concurrent
    change is simply absent from the walk — never dereferenced, so it can't
    fail this test either way."""
    offenders: dict[str, list[str]] = {}
    for path in _all_html():
        if path.name in _NATIVE_DIALOG_ALLOWLIST:
            continue
        found = _native_dialog_offenders(path.read_text(encoding="utf-8"))
        if found:
            offenders[str(path)] = found
    assert not offenders, (
        "native alert()/confirm()/prompt() found — use the promise-based "
        "confirmModal()/alertModal()/promptModal() from modal.js instead "
        "(#497 §1):\n" + "\n".join(f"  {p}: {matches}" for p, matches in offenders.items())
    )


def test_native_dialog_allowlist_has_no_stale_entries() -> None:
    """Every allowlist entry must still exist AND still carry a native call.
    A file deleted by a concurrent change is tolerated (skipped, not
    failed) — there is nothing left to convert. A file that's still there
    but fully converted is stale: leaving its entry in place would silently
    mask a real future regression on that same page, so this fails until
    the entry is dropped."""
    stale: list[str] = []
    for name in sorted(_NATIVE_DIALOG_ALLOWLIST):
        path = TEMPLATES / name
        if not path.exists():
            continue
        if not _native_dialog_offenders(path.read_text(encoding="utf-8")):
            stale.append(name)
    assert not stale, (
        f"stale _NATIVE_DIALOG_ALLOWLIST entr(ies) — no native dialog call left, drop from the allowlist: {stale}"
    )


def test_native_dialog_canonical_forms_do_not_trip_guard() -> None:
    """The design-system replacements — bare or window.-prefixed, awaited or
    not — must never be flagged, or every already-converted page would
    immediately regress this guard."""
    sample = """
    <script>
      window.confirmModal(x); window.alertModal(y); window.promptModal(z);
      const ok = await confirmModal('Delete?');
      await alertModal('Saved');
      const name = await promptModal('Name?');
    </script>
    """
    assert _native_dialog_offenders(sample) == []


def test_native_dialog_detector_fires_on_bare_and_window_prefixed_calls() -> None:
    """Sanity-check the detector actually catches what it exists to catch."""
    assert _native_dialog_offenders("<script>alert('hi');</script>") == ["alert("]
    assert _native_dialog_offenders("<script>if (!confirm('Sure?')) return;</script>") == ["confirm("]
    assert _native_dialog_offenders("<script>var n = prompt('Name?');</script>") == ["prompt("]
    assert _native_dialog_offenders("<script>window.confirm('Sure?');</script>") == ["window.confirm("]
