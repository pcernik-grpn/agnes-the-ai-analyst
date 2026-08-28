"""Route tests for the generic authoring-agent studio pages."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def studio_on(monkeypatch):
    """Studio is OFF by default since the admin cleanup retired the surface.

    Every test in this module is about what the surface DOES when exposed, so
    they turn it on rather than assert against the shipped default. The two
    tests that check the disabled behavior override this — one by patching
    `app.web.router.get_studio_enabled`, which wins over the env var because it
    replaces the reader itself, the other by setting the env var it owns.
    """
    monkeypatch.setenv("AGNES_STUDIO_ENABLED", "1")


DOMAINS = ["data-package", "mcp", "marketplace", "corporate-memory"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("domain", DOMAINS)
def test_studio_renders_for_admin_in_create_mode(seeded_app, domain):
    c = seeded_app["client"]
    resp = c.get(f"/admin/studio/{domain}", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    body = resp.text
    assert 'id="studio-create"' in body
    assert "/static/js/studio.js" in body
    assert "window.STUDIO" in body
    assert "isAdmin: true" in body
    assert ">Create<" in body  # admin sees the direct-create action


@pytest.mark.parametrize("domain", DOMAINS)
def test_studio_renders_for_non_admin_in_submit_mode(seeded_app, domain):
    c = seeded_app["client"]
    resp = c.get(f"/admin/studio/{domain}", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    body = resp.text
    assert "isAdmin: false" in body
    assert "Submit for approval" in body  # non-admin sees the suggestion action


def test_studio_index_title_carries_instance_name(seeded_app):
    """Regression: /admin/studio rendered ``<title>Studio — </title>`` — the
    title template reads ``config.INSTANCE_NAME`` but ``_chrome_ctx`` didn't
    provide ``config``, so Jinja rendered the undefined as empty. The title
    must carry the instance name (default: "AI Harness")."""
    import re

    c = seeded_app["client"]
    resp = c.get("/admin/studio", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    m = re.search(r"<title>(.*?)</title>", resp.text, re.S)
    assert m is not None
    title = m.group(1).strip()
    assert title.startswith("Studio — ")
    assert len(title) > len("Studio — "), f"empty instance name in title: {title!r}"


def test_studio_requires_login(seeded_app):
    c = seeded_app["client"]
    # No auth header → redirect to login (don't follow it) or 401/403.
    resp = c.get("/admin/studio/data-package", follow_redirects=False)
    assert resp.status_code in (302, 307, 401, 403)
    if resp.status_code in (302, 307):
        assert "/login" in resp.headers.get("location", "")


def test_studio_unknown_domain_404s(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/admin/studio/nope", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 404


def test_suggestions_review_page_renders_for_admin(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/admin/studio/suggestions", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    assert "/static/js/studio_suggestions.js" in resp.text
    assert 'id="sug-list"' in resp.text
    assert 'id="sug-run-mining"' in resp.text  # admin can trigger a mining run


def test_memory_mining_consent_page_renders_for_user(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/me/memory-mining", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    assert 'id="mm-toggle"' in resp.text
    assert "/static/js/me_memory_mining.js" in resp.text


def test_suggestions_review_page_requires_admin(seeded_app):
    c = seeded_app["client"]
    resp = c.get(
        "/admin/studio/suggestions",
        headers=_auth(seeded_app["analyst_token"]),
        follow_redirects=False,
    )
    assert resp.status_code in (302, 307, 401, 403)


def test_skill_domain_registered_as_direct_submit():
    from app.web.studio import STUDIO_DOMAINS, get_domain

    spec = get_domain("skill")
    assert spec is not None
    assert spec.submit_directly is True
    assert spec.endpoint == "/api/store/entities/from-markdown"
    assert spec.profile == "skill-author"
    assert [f.key for f in spec.fields] == ["name", "description", "category", "skill_md"]
    # every other domain except "agent" (the store's other direct-submit
    # type) still routes through the suggestions queue
    assert all(not d.submit_directly for s, d in STUDIO_DOMAINS.items() if s not in ("skill", "agent"))


def test_agent_domain_registered_as_direct_submit():
    from app.web.studio import get_domain

    spec = get_domain("agent")
    assert spec is not None
    assert spec.submit_directly is True
    assert spec.endpoint == "/api/store/entities/from-markdown"
    assert spec.profile == "agent-author"
    assert [f.key for f in spec.fields] == ["name", "description", "category", "skill_md"]


def test_agent_studio_renders_publish_for_non_admin(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/admin/studio/agent", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    body = resp.text
    assert "isAdmin: false" in body
    assert "submitDirect: true" in body
    assert ">Publish<" in body
    assert "Submit for approval" not in body


def test_agent_studio_renders_for_admin(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/admin/studio/agent", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    assert "submitDirect: true" in resp.text
    assert 'id="studio-f-skill_md"' in resp.text  # the agent content textarea rendered
    assert 'domain: "agent"' in resp.text


def test_skill_studio_renders_publish_for_non_admin(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/admin/studio/skill", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    body = resp.text
    assert "isAdmin: false" in body
    assert "submitDirect: true" in body
    assert ">Publish<" in body  # direct-submit domains publish, not suggest
    assert "Submit for approval" not in body
    assert "store" in body.lower()  # footer explains the store review pipeline


def test_skill_studio_renders_for_admin(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/admin/studio/skill", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    assert "submitDirect: true" in resp.text
    assert 'id="studio-f-skill_md"' in resp.text  # the markdown textarea rendered


def test_skills_page_is_the_unified_builder(seeded_app):
    """/skills IS the builder now, and it builds all three authored kinds.

    The separate "your skills" index was retired first (created items land in
    the Library); the single-TYPE builder was retired next. The page opens on
    a type picker, then swaps content per type inside one shell — access
    picker, numbered sections, one primary action."""
    c = seeded_app["client"]
    resp = c.get("/skills", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    body = resp.text
    # Retired: the index container and its "+ New skill" grid card.
    assert 'id="sk-list-view"' not in body
    assert "renderList" not in body
    # The builder is the whole page.
    assert 'id="sk-builder-view"' in body
    assert 'id="sk-categories"' in body  # store-category options island
    # Step 1 — every supported type is offered, and picking one is what opens
    # the form (no type ⇒ no builder).
    # The cards are built client-side from the TYPES table, so assert on the
    # table + the hook each card carries, not on markup the server never emits.
    assert "What are you building?" in body
    assert "data-sk-type" in body
    assert "var TYPE_ORDER = ['skill', 'plugin', 'agent'];" in body
    for kind in ("skill", "plugin", "agent"):
        assert f"key: '{kind}'," in body
    # Type is NOT a step. It used to be section 1 — a card with a tick where
    # every other section has a number, spending the top of the panel
    # re-asking what "+ Add → Build a skill" already answered. It is identity,
    # so it rides in the header beside the title, with Change one click away;
    # the sections are the configuration, numbered 1..3 with no gap.
    assert "typeBadgeHtml" in body, "the type is no longer shown beside the title"
    assert "sk-typechip" in body
    assert "data-sk-change" in body
    assert ">Change<" in body
    for n in ("1", "2", "3"):
        assert f"no: {n}," in body, f"step {n} is not numbered in the shell sections"
    assert "no: 4," not in body, "the sections should end at 3 now that Type is not one"
    # Access is a required choice before saving: Private or the whole org.
    assert 'name="sk-access"' in body
    assert 'value="private"' in body
    assert 'value="everyone"' in body
    assert "Who can use this" in body
    # One primary action. A draft is explicitly local, never a store write —
    # "Publish to marketplace" stays gone.
    assert 'id="sk-save"' in body
    assert "Save to Library" in body
    assert "Publish to marketplace" not in body
    assert 'id="sk-draft"' in body
    # Saving returns to the Library and highlights the new item.
    assert "/library?new=" in body
    # Both publish paths are wired: markdown for skills/agents, multipart for
    # plugin bundles, and the bundle is validated before it can be saved.
    assert "/api/store/entities/from-markdown" in body
    assert "'/api/store/entities'" in body
    assert "/api/store/entities/preview" in body
    # Regression: the builder's buttons use the shared .cc-btn styles.
    assert "css/catalog_card.css" in body


def test_markdown_body_can_be_written_or_uploaded(seeded_app):
    """A skill / shareable agent body can be UPLOADED as a .md, offered as an
    explicit choice next to writing it.

    The file route existed before this, as a 12.5px dashed strip above a 14-row
    textarea — read as decoration, so authors who already had a SKILL.md pasted
    it in by hand. Worse, its "browse" affordance was a <label for> pointing at
    a `hidden` input: a label is not keyboard-focusable and neither is a hidden
    input, so the file route could not be reached without a mouse at all."""
    body = seeded_app["client"].get("/skills", headers=_auth(seeded_app["analyst_token"])).text

    # Both routes, at the same weight, as a pressed-state toggle group.
    assert "data-sk-mode" in body
    assert "'Write it here'" in body
    assert "'Upload a .md file'" in body
    assert 'aria-pressed="' in body
    # Reachable by keyboard: a real button opens the hidden input via .click().
    assert "data-sk-browse" in body
    assert ">Choose a file<" in body
    assert 'for="sk-file-input"' not in body  # the label affordance it replaced
    assert "input.click()" in body
    # Upload is an INPUT METHOD, not a mode: an uploaded file drops into the
    # same body the textarea edits, and there is a way back to the editor.
    assert 'data-sk-mode="write">Edit as text<' in body
    # An uploaded .md carries its own identity — reading it beats making the
    # author retype what they just handed over. Blanks only, never a clobber.
    assert "function parseFrontmatter(" in body
    assert "function applyFrontmatter(" in body
    assert "filled in " in body  # the receipt naming which fields were filled
    # Guards the intake actually needs: text-only, capped, and never a silent
    # overwrite of work already written.
    assert "MAX_MD_BYTES" in body
    assert "is not a Markdown file" in body
    assert "window.confirm('Replace the '" in body
    # The plugin bundle is a .zip and cannot be typed, so it gets no toggle —
    # but it shares the file card, and the same keyboard-reachable browse.
    assert "'.zip,application/zip'" in body


def test_builder_separates_agent_templates_from_agents(seeded_app):
    """A Library agent template and a personal agent were one word apart, and
    the builder used to spend a sentence disowning the one it was NOT making.

    AGT-4 gave the Library concept its own name, so the separation is carried
    by the noun rather than by a disclaimer: the builder says "Agent Template",
    still points at /agents for the other thing, and still refuses to imply the
    author's own authority travels with what they publish. That last part is
    the load-bearing claim — an author who believes their access ships with the
    template will write one that assumes data it will never see.
    """
    c = seeded_app["client"]
    body = c.get("/skills", headers=_auth(seeded_app["analyst_token"])).text
    assert "Agent Template" in body
    assert 'href="/agents"' in body
    # The old name is gone, and so is the sentence that existed to compensate.
    assert "shareable agent" not in body.lower()
    assert "not one of your" not in body.lower()
    # Authority still does not travel with the published resource.
    assert "inherits yours" in body


def test_builder_splits_receipts_from_problems(seeded_app):
    """Feedback rides TWO channels, chosen by "must the author act on it?".

    Everything shared one `.sk-result` span before this, and that span lived
    inside the action row with `flex: 1 1 100%` — so a multi-issue rejection
    (~300 characters, because the same "description too short" fired at both
    component and submission level and carried ZIP-upload advice a Markdown
    author cannot act on) claimed its own full-width row and pushed Save down
    the page as it appeared."""
    body = seeded_app["client"].get("/skills", headers=_auth(seeded_app["analyst_token"])).text

    # The status line that shared one channel and moved the buttons is gone.
    assert 'id="sk-result"' not in body
    assert "sk-result { font-size" not in body
    assert "function setResult(" not in body

    # Receipts → the app-wide toast, out of the layout entirely.
    assert "window.appToast" in body
    assert "Draft saved in this browser." in body
    # …including the one the builder never gave: it used to navigate away and
    # let the Library imply the save worked.
    assert "agnes.flash.toast" in body
    assert "saved to your Library." in body
    assert "once the automated review passes" in body  # published ≠ private

    # Problems → on the page, attributed, dismissible, jumpable.
    assert 'id="sk-alerts"' in body
    assert 'role="alert"' in body
    assert "data-sk-jump=" in body and ">Take me there<" in body
    assert "data-sk-dismiss" in body
    assert "data-sk-err=" in body  # the same fault restated on its own field
    assert "function issueField(" in body  # baked-tree file path → on-screen field

    # Alert = index (headline), field = detail (whole hint). Not both, twice.
    assert "function headline(" in body
    # Advisory phases must not be dressed up as blockers, and hints written for
    # the ZIP path must not survive onto a write-here surface.
    assert "var BLOCKING_PHASES = { manifest: 1, content: 1, static_security: 1 };" in body
    assert "function trimHint(" in body
    assert "quality" not in body.split("BLOCKING_PHASES = {")[1].split("}")[0]

    # Progress rides the control that started the work, not a status line.
    assert "busy('sk-save', 'Saving…')" in body
    assert "busy('sk-check', 'Checking…')" in body


def test_global_toast_is_announced_and_dismissable(seeded_app):
    """The shared toast primitive (window.appToast) is the builder's receipt
    channel, so it has to be one: announced to a screen reader, dismissable by
    keyboard, capped, and tolerant of the bare-string call shape eight existing
    call sites already use (which rendered an EMPTY toast before this)."""
    app_js = (Path("app/web/static/app.js")).read_text(encoding="utf-8")

    assert 'c.setAttribute("aria-live", "polite")' in app_js
    assert 'c.setAttribute("role", "status")' in app_js
    assert 'if (typeof opts === "string") opts = { msg: opts };' in app_js
    assert "TOAST_MAX" in app_js  # a repeated action can't paper over the page
    assert 'close.className = "toast-x"' in app_js
    assert 'close.setAttribute("aria-label", "Dismiss")' in app_js
    # Hovering is how a long receipt gets read — it must not expire mid-sentence.
    assert 'el.addEventListener("mouseenter"' in app_js

    css = (Path("app/web/static/style-custom.css")).read_text(encoding="utf-8")
    assert ".toast-x {" in css
    assert ".toast-msg {" in css
    assert "@media (prefers-reduced-motion: reduce)" in css.split(".toast-container {")[1]


def test_skills_page_arms_new_skill_spotlight(seeded_app):
    """/skills still carries the one-step coach-mark that the Marketplace's
    "Submit a skill or plugin" CTA arrives with (`?spotlight=new-skill`)."""
    c = seeded_app["client"]
    body = c.get("/skills", headers=_auth(seeded_app["analyst_token"])).text

    assert "spotlight" in body and "new-skill" in body
    assert "js/tour.js" in body  # lazy dynamic import of the engine
    assert "launchTour('skill-builder')" in body
    # The guard must accept the type step in EITHER state. It briefly required
    # the name field, which is only in the DOM once a type is chosen — on a
    # cold arrival (step 1 expanded) the coach-mark had no anchor at all.
    assert "'[data-sk-type],[data-sk-change]'" in body
    # One-shot: the param is stripped so a reload doesn't re-pop the coach-mark.
    assert "history.replaceState" in body
    assert "maybeSpotlightNew()" in body
    # The CTA promises "skill or plugin". Plugins are built right here now, so
    # the onward path is the Plugin type itself — the builder must NOT hand the
    # author off to the old curated-marketplace guide, a concept the new UI no
    # longer carries.
    assert "/marketplace/guide/curated" not in body


def test_skill_builder_tour_anchors_on_the_type_step():
    """The `skill-builder` tour is a single step on /skills, anchored on the
    TYPE step — the first thing on the page and the first decision to make.

    Anchor history is the point of this guard: it was the "+ New skill" card,
    then the name field, and the name field broke when type became step 1
    (that field is not in the DOM until a type is picked, so the coach-mark
    pointed at nothing). The type step is present in both its states, so the
    selector must cover the expanded cards AND the collapsed Change button.
    Single-step tours render in the popover's solo form (no dots / no "explore
    on my own"), so guard the branch that produces it too."""
    from pathlib import Path

    js = Path("app/web/static/js/tour.js").read_text()
    assert "'skill-builder':" in js
    assert "'[data-sk-type], [data-sk-change]'" in js
    assert "[data-sk-new]" not in js  # retired anchor
    assert '[data-sk-field="name"]' not in js  # retired anchor — breaks on cold arrival
    assert "page: '/skills'" in js
    # Solo rendering: one step ⇒ 'Got it', no dots, no escape-hatch button.
    assert "const solo = total === 1;" in js
    assert "tour-popover-footer--solo" in js
    assert Path("app/web/static/css/tour.css").read_text().count(".tour-popover-footer--solo"), (
        "solo footer modifier must be styled or the action row sits left"
    )


def test_skills_index_requires_login(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/skills", follow_redirects=False)
    assert resp.status_code in (302, 307, 401, 403)
    if resp.status_code in (302, 307):
        assert "/login" in resp.headers.get("location", "")


def test_existing_domains_keep_suggestion_flow(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/admin/studio/data-package", headers=_auth(seeded_app["analyst_token"]))
    assert "submitDirect: false" in resp.text
    assert "Submit for approval" in resp.text


def test_studio_index_lists_every_domain(seeded_app):
    from app.web.studio import STUDIO_DOMAINS

    c = seeded_app["client"]
    resp = c.get("/admin/studio", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    body = resp.text
    for slug, domain in STUDIO_DOMAINS.items():
        assert f"/admin/studio/{slug}" in body
        assert domain.title in body


def test_studio_index_requires_login(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/admin/studio", follow_redirects=False)
    assert resp.status_code in (302, 307, 401, 403)
    if resp.status_code in (302, 307):
        assert "/login" in resp.headers.get("location", "")


def test_studio_is_reachable_from_the_admin_nav(seeded_app):
    """Studio is linked from the admin inventory, gated on `can_studio`.

    Replaces an assertion that the page links ITSELF (`href="/admin/studio"` on
    /admin/studio), which said nothing about reachability — you are already
    there — and, briefly, an assertion that Studio was in the nav guard's
    KNOWN_UNLINKED list. That second one was worse: it was false (Studio has an
    `app/web/admin_nav.py` row and three command-palette entries) and it could
    not fail, because it tested a dict literal in a test file rather than the
    app.

    Its topnav row and rail dropdown are both gone — the rail's by IA choice —
    but the admin nav is a real door, so this asserts that.
    """
    from app.web.admin_nav import ADMIN_NAV_SECTIONS, _section_entries

    entries = [e for s in ADMIN_NAV_SECTIONS for e in _section_entries(s) if e["href"] == "/admin/studio"]
    assert entries, "/admin/studio is not in the admin nav inventory"
    assert entries[0].get("when") == "can_studio", (
        "the Studio row must stay gated on can_studio, or an opted-out instance shows a row that redirects home"
    )

    # And a RENDERED door, not just an inventory entry: the command palette
    # carries Studio on every authed page. (The sidebar renders only its active
    # section's body server-side, so a row from another section is not in the
    # HTML — see test_web_admin_nav.py::
    # test_only_the_active_section_renders_expanded_server_side.)
    resp = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    assert "href: '/admin/studio'" in resp.text

    # The page itself still renders for a signed-in caller.
    assert seeded_app["client"].get("/admin/studio", headers=_auth(seeded_app["analyst_token"])).status_code == 200


# --- Instance-level enable/disable toggle (studio.enabled / AGNES_STUDIO_ENABLED) ---


def test_studio_routes_redirect_when_disabled(seeded_app, monkeypatch):
    # get_studio_enabled is imported into the router namespace and consulted by
    # every studio handler + both chrome builders — patch it there.
    monkeypatch.setattr("app.web.router.get_studio_enabled", lambda: False)
    c = seeded_app["client"]
    for path in ("/admin/studio", "/admin/studio/data-package", "/admin/studio/suggestions"):
        resp = c.get(path, headers=_auth(seeded_app["admin_token"]), follow_redirects=False)
        assert resp.status_code in (302, 307), path
        assert resp.headers.get("location", "") == "/", path


def test_studio_palette_entries_hidden_when_disabled(seeded_app, monkeypatch):
    """`can_studio` gates the command-palette rows, which are Studio's only
    chrome-level trace now.

    This used to check a `data-tour="nav-studio"` nav link on two pages as
    well. Both premises are gone: `data-tour` anchors went with the guided tour
    and the topnav chrome (Wave 0, 2026-08), and /dashboard is a redirect. The
    palette half is the part that still exists, and it is the part the flag
    actually drives.
    """
    c = seeded_app["client"]
    page = "/me/memory-mining"
    resp = c.get(page, headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    assert "Studio · Data package" in resp.text, "palette row missing while studio is enabled"

    monkeypatch.setattr("app.web.router.get_studio_enabled", lambda: False)
    resp = c.get(page, headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    assert "Studio · Data package" not in resp.text


def test_studio_enabled_env_override(monkeypatch):
    import app.instance_config as ic

    ic.reset_cache()
    # Every documented false-like env spelling disables.
    for falsy in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("AGNES_STUDIO_ENABLED", falsy)
        assert ic.get_studio_enabled() is False, falsy
    monkeypatch.setenv("AGNES_STUDIO_ENABLED", "true")
    assert ic.get_studio_enabled() is True
    monkeypatch.delenv("AGNES_STUDIO_ENABLED", raising=False)
    # No env, no yaml studio block → defaults OFF since the admin cleanup.
    assert ic.get_studio_enabled() is False


def test_studio_enabled_yaml_fallback_and_precedence(monkeypatch):
    """studio.enabled: false in YAML disables; env still wins over YAML."""
    import app.instance_config as ic

    def fake_get_value(*keys, default=None):
        if keys == ("studio", "enabled"):
            return False
        return default

    monkeypatch.setattr(ic, "get_value", fake_get_value)
    monkeypatch.delenv("AGNES_STUDIO_ENABLED", raising=False)
    assert ic.get_studio_enabled() is False  # YAML fallback
    monkeypatch.setenv("AGNES_STUDIO_ENABLED", "1")
    assert ic.get_studio_enabled() is True  # env > YAML


class TestStudioSelectFieldDropdown:
    """Custom design-system dropdown on the "mcp" domain's Transport select
    (#1055). `#studio-f-transport` stays a real `<select>` in the DOM
    (studio.js's collectPayload() reads its `.value` unchanged) with a
    `ds.dropdown()` custom button+menu alongside it. Visibility between the
    two is a CSS theme decision (paper-skin.css), not a template one.
    """

    def test_native_select_still_renders_for_existing_js_wiring(self, seeded_app):
        resp = seeded_app["client"].get(
            "/admin/studio/mcp",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        text = resp.text
        assert '<select id="studio-f-transport" class="ds-dropdown-native">' in text
        assert '<option value="http">http</option>' in text
        assert '<option value="sse">sse</option>' in text
        assert '<option value="stdio">stdio</option>' in text

    def test_custom_dropdown_markup_present(self, seeded_app):
        resp = seeded_app["client"].get(
            "/admin/studio/mcp",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        text = resp.text
        assert 'data-ds-dropdown-target="studio-f-transport"' in text
        assert 'id="studio-f-transport-dd-btn"' in text
        assert 'aria-haspopup="menu"' in text
        assert 'aria-controls="studio-f-transport-dd-menu"' in text
        assert 'id="studio-f-transport-dd-menu"' in text
        assert 'role="menu"' in text
        assert 'role="menuitemradio"' in text
        for value in ("http", "sse", "stdio"):
            assert f'data-value="{value}"' in text

    def test_dropdown_js_module_is_loaded(self, seeded_app):
        resp = seeded_app["client"].get(
            "/admin/studio/mcp",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        assert "js/components/ds_dropdown.js" in resp.text

    def test_domains_without_a_select_field_render_unaffected(self, seeded_app):
        """data-package / marketplace / corporate-memory have no select-type
        field — no dropdown markup should appear on those pages."""
        for domain in ("data-package", "marketplace", "corporate-memory"):
            resp = seeded_app["client"].get(
                f"/admin/studio/{domain}",
                headers=_auth(seeded_app["admin_token"]),
            )
            assert resp.status_code == 200
            assert "ds-dropdown-native" not in resp.text
            assert "ds-dropdown-target" not in resp.text
