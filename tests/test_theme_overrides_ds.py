"""Operator `theme:` colour override → design-system `--ds-*` tokens.

The per-instance `theme:` block in `instance.yaml` (`primary`, `background`,
…) used to drive only the legacy `--*` variable family in
`style-custom.css` — and even that was silently broken: the raw YAML key
(e.g. ``primary``) was emitted as the CSS *property* name with no ``--``
prefix, so it set nothing at all. Two problems fixed together:

1. ``get_theme_css_overrides()`` (``app/instance_config.py``) maps every
   known ``theme:`` key to its real ``--*`` variable name AND, where a
   clean equivalent exists, the matching ``--ds-*`` design-system token —
   so an operator can rebrand the "paper"/"navy"/"dark" redesigned
   surfaces from config alone, not just the pre-redesign chrome.
2. ``_theme.html`` emits the override into a selector specific enough to
   beat every built-in ``:root[data-theme="…"]`` block in
   ``design-tokens.css`` — a bare ``:root { … }`` (specificity 0,1,0) lost
   to those blocks (0,2,0 and up) on any non-default theme.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.instance_config import get_theme_css_overrides

DESIGN_TOKENS_CSS = Path("app/web/static/css/design-tokens.css")

# The exact selector list `_theme.html` emits — kept as one constant so the
# specificity test and the rendering test can't drift apart.
_OVERRIDE_SELECTOR_RE = re.compile(
    r":root\s*,\s*:root\[data-theme\]\s*,\s*:root\[data-theme\]\[data-theme-variant\]\s*\{"
)


def _specificity(selector: str) -> tuple[int, int]:
    """``(b, c)`` CSS specificity for a single compound ``:root…`` selector.

    Only the components that ever vary across the selectors this module
    compares: attribute selectors (``[data-theme="x"]``) and pseudo-classes
    (``:root``) both count toward "b"; none of the selectors compared here
    carry an id ("a", always 0) or a type selector ("c", always 0), so this
    is a correct (not merely approximate) specificity for them."""
    attrs = len(re.findall(r"\[[^\]]*\]", selector))
    pseudo_classes = len(re.findall(r":(?!:)[\w-]+", selector))
    return (attrs + pseudo_classes, 0)


class TestThemeCssOverridesMapping:
    """Unit tests for the pure `theme:` → CSS-variable mapping function."""

    @pytest.fixture(autouse=True)
    def _isolate_config(self, monkeypatch):
        """Pin the config cache so a developer's local `config/instance.yaml`
        (which may itself set `theme:`) can never leak into these
        assertions — same isolation `test_web_footer.py::no_yaml_config`
        uses for the same reason."""
        import app.instance_config as ic

        monkeypatch.setattr(ic, "_instance_config", {})
        yield

    def test_no_theme_config_returns_empty(self):
        """Regression: no `theme:` block → no overrides, at all."""
        assert get_theme_css_overrides() == {}

    def test_known_keys_map_to_legacy_and_ds_vars(self, monkeypatch):
        import app.instance_config as ic

        monkeypatch.setattr(
            ic,
            "_instance_config",
            {
                "theme": {
                    "primary": "#112233",
                    "primary_dark": "#0f1e2d",
                    "primary_light": "rgba(17, 34, 51, 0.1)",
                    "background": "#fafafa",
                    "surface": "#ffffff",
                    "border": "#dddddd",
                    "text_primary": "#101010",
                    "text_secondary": "#606060",
                    "success": "#00aa55",
                    "warning": "#cc9900",
                    "error": "#cc3300",
                }
            },
        )
        overrides = get_theme_css_overrides()
        assert overrides["--primary"] == "#112233"
        assert overrides["--ds-primary"] == "#112233"
        assert overrides["--primary-dark"] == "#0f1e2d"
        assert overrides["--ds-primary-dark"] == "#0f1e2d"
        assert overrides["--primary-light"] == "rgba(17, 34, 51, 0.1)"
        assert overrides["--ds-primary-light"] == "rgba(17, 34, 51, 0.1)"
        assert overrides["--background"] == "#fafafa"
        assert overrides["--ds-bg"] == "#fafafa"
        assert overrides["--surface"] == "#ffffff"
        assert overrides["--ds-surface"] == "#ffffff"
        assert overrides["--border"] == "#dddddd"
        assert overrides["--ds-border"] == "#dddddd"
        assert overrides["--text-primary"] == "#101010"
        assert overrides["--ds-text-primary"] == "#101010"
        assert overrides["--text-secondary"] == "#606060"
        assert overrides["--ds-text-secondary"] == "#606060"
        # The design system's status vocabulary is "warn"/"danger", not
        # "warning"/"error" — same colour, different token family name.
        assert overrides["--success"] == "#00aa55"
        assert overrides["--ds-accent-success-ink"] == "#00aa55"
        assert overrides["--warning"] == "#cc9900"
        assert overrides["--ds-accent-warn-ink"] == "#cc9900"
        assert overrides["--error"] == "#cc3300"
        assert overrides["--ds-accent-danger-ink"] == "#cc3300"

    def test_legacy_only_keys_have_no_ds_counterpart(self, monkeypatch):
        """`radius` and `font_primary` stay legacy-only — no single clean
        `--ds-*` token exists for either (see `THEME_CSS_VAR_MAP`)."""
        import app.instance_config as ic

        monkeypatch.setattr(
            ic,
            "_instance_config",
            {"theme": {"radius": "10px", "font_primary": "Georgia, serif"}},
        )
        assert get_theme_css_overrides() == {
            "--radius": "10px",
            "--font-primary": "Georgia, serif",
        }

    def test_font_url_is_not_emitted_as_a_css_variable(self, monkeypatch):
        """`font_url` drives a `<link rel="stylesheet">`, not a custom
        property — it must never leak into the variable dict."""
        import app.instance_config as ic

        monkeypatch.setattr(
            ic,
            "_instance_config",
            {"theme": {"font_url": "https://fonts.example.com/x.css"}},
        )
        assert get_theme_css_overrides() == {}

    def test_empty_string_value_is_never_emitted(self, monkeypatch):
        import app.instance_config as ic

        monkeypatch.setattr(ic, "_instance_config", {"theme": {"primary": ""}})
        assert get_theme_css_overrides() == {}

    def test_unknown_key_is_ignored_not_leaked(self, monkeypatch):
        """A typo'd/future theme key must not crash and must not leak
        through as a bogus CSS declaration — the pre-fix behaviour emitted
        *any* dict key verbatim as a CSS property name."""
        import app.instance_config as ic

        monkeypatch.setattr(ic, "_instance_config", {"theme": {"totally_made_up": "#fff"}})
        assert get_theme_css_overrides() == {}


class TestOverrideSelectorSpecificity:
    """`_theme.html`'s override selector must reliably outrank every
    `:root[data-theme="…"]` block design-tokens.css actually declares —
    computed from the real file so a future, more-specific theme block
    fails this test loudly instead of silently losing the cascade."""

    def test_override_selector_ties_or_beats_every_root_theme_block(self):
        css = DESIGN_TOKENS_CSS.read_text()
        root_selectors = re.findall(r":root(?:\[[^\]]*\])*(?=\s*\{)", css)
        assert root_selectors, "no `:root` selectors found — did design-tokens.css move?"
        highest = max(_specificity(s) for s in root_selectors)
        ours = _specificity(":root[data-theme][data-theme-variant]")
        assert ours >= highest, (
            f"a design-tokens.css block ({highest}) now outranks the operator "
            f"override selector ({ours}) — widen the selector list in _theme.html"
        )

    def test_theme_html_emits_the_expected_selector_list(self):
        """Pin the literal selector list (not just its computed specificity)
        so an accidental simplification back to bare `:root` is caught even
        before the render test below runs it through a live page."""
        text = Path("app/web/templates/_theme.html").read_text()
        assert _OVERRIDE_SELECTOR_RE.search(text), (
            "_theme.html no longer emits the :root, :root[data-theme], "
            ":root[data-theme][data-theme-variant] selector list"
        )


@pytest.fixture
def web_client(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    (tmp_path / "state").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "extracts").mkdir()
    from src.db import close_system_db

    close_system_db()

    app = shared_app
    yield TestClient(app)
    close_system_db()


@pytest.fixture
def admin_cookie(web_client):
    from argon2 import PasswordHasher

    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from tests.helpers.auth import grant_admin

    password = "AdminPass1!"
    conn = get_system_db()
    UserRepository(conn).create(
        id="admin1",
        email="admin@test.com",
        name="Admin",
        password_hash=PasswordHasher().hash(password),
    )
    grant_admin(conn, "admin1")
    conn.close()
    resp = web_client.post("/auth/token", json={"email": "admin@test.com", "password": password})
    assert resp.status_code == 200, f"Bootstrap failed: {resp.text}"
    return {"access_token": resp.json()["access_token"]}


@pytest.fixture
def no_yaml_config(monkeypatch):
    """Same isolation as `TestThemeCssOverridesMapping._isolate_config`,
    for the page-render tests below."""
    import app.instance_config as ic

    monkeypatch.setattr(ic, "_instance_config", {})
    yield


class TestThemeOverrideRenderedOnPage:
    """Rendered against `/library` — an ordinary authed design-system page
    (base_index.html -> base_ds.html), same page `test_web_footer.py` uses
    for the equivalent `config.*` proxy contract."""

    def test_no_theme_config_renders_no_override_block(self, web_client, admin_cookie, no_yaml_config):
        """Regression: an instance with no `theme:` block must see
        byte-identical output — `_theme.html` must not render the
        `<style>` block at all."""
        resp = web_client.get("/library", cookies=admin_cookie)
        assert resp.status_code == 200
        assert not _OVERRIDE_SELECTOR_RE.search(resp.text)
        assert "--ds-primary:" not in resp.text

    def test_configured_theme_recolors_ds_and_legacy_tokens(self, web_client, admin_cookie, monkeypatch):
        import app.instance_config as ic

        monkeypatch.setattr(
            ic,
            "_instance_config",
            {"theme": {"primary": "#112233", "background": "#eeeeee"}},
        )
        resp = web_client.get("/library", cookies=admin_cookie)
        assert resp.status_code == 200
        html = resp.text
        assert "--ds-primary: #112233;" in html
        assert "--primary: #112233;" in html
        assert "--ds-bg: #eeeeee;" in html
        assert "--background: #eeeeee;" in html

    def test_override_wins_the_cascade_over_design_tokens_css(self, web_client, admin_cookie, monkeypatch):
        """Selector present + rendered strictly after design-tokens.css's
        `<link>`, so an equal-specificity tie resolves in the override's
        favour (later rule wins) on every theme, including "paper"."""
        import app.instance_config as ic

        monkeypatch.setattr(ic, "_instance_config", {"theme": {"primary": "#112233"}})
        resp = web_client.get("/library", cookies=admin_cookie)
        html = resp.text

        link_match = re.search(r'href="[^"]*css/design-tokens\.css[^"]*"', html)
        assert link_match, "design-tokens.css stylesheet link not found on the page"
        style_match = _OVERRIDE_SELECTOR_RE.search(html)
        assert style_match, "operator override <style> block not found on the page"
        assert style_match.start() > link_match.start(), (
            "operator theme override must render AFTER design-tokens.css's <link> "
            "so equal-specificity ties resolve in its favour"
        )
