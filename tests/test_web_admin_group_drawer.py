"""Creating a group asks for a name, and hands you the group.

A group is never just a name: it reaches people and it carries what those
people can use. The drawer once taught that by carrying its OWN copy of the
member editor and the grant editor as steps 2 and 3 — which left the product
with three of each over one pair of tables. The lesson now lives where it
cannot drift: creating a group SELECTS it in the Access workspace, whose two
panes open on an empty audience and an empty grant list.

What these tests pin:

  * the drawer is one step — the create/rename form — and the duplicated
    people and access steps are gone rather than hidden;
  * it still writes through the existing group APIs, with no batched submit;
  * `/admin/access` opens it, and can create a group without navigating away;
  * the name-only modal it replaced is gone rather than left dangling;
  * the Add-data wizard rides the same shared drawer chrome, keeping every
    id its own JS drives;
  * `/api/admin/access-overview` carries each grant's tier, without which
    the editor renders every grant as Optional.
"""

from __future__ import annotations

from pathlib import Path

STATIC = Path("app/web/static")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestOneSharedComponent:
    """The same drawer, whichever surface opens it."""

    def test_the_retired_group_pages_redirect_here(self, seeded_app):
        """`/admin/groups` was the other entry point. It is now the same
        page, so there is one create experience by construction."""
        c = seeded_app["client"]
        r = c.get("/admin/groups", headers=_auth(seeded_app["admin_token"]), follow_redirects=False)
        assert r.status_code == 308
        assert r.headers["location"] == "/admin/access"

    def test_access_page_loads_the_drawer(self, seeded_app):
        c = seeded_app["client"]
        body = c.get("/admin/access", headers=_auth(seeded_app["admin_token"])).text
        assert "css/drawer.css" in body
        assert "css/group_drawer.css" in body
        assert "js/components/group_drawer.js" in body

    def test_the_old_name_only_modal_is_gone(self, seeded_app):
        """Not merely bypassed — removed. A dormant second dialog on the page
        is one stray `openModal("group-modal")` away from coming back."""
        c = seeded_app["client"]
        body = c.get("/admin/access", headers=_auth(seeded_app["admin_token"])).text
        assert 'id="group-modal"' not in body
        assert 'id="group-save-btn"' not in body
        # Deleting a group is a genuine one-decision dialog and stays — as the
        # app's SHARED confirm, not a fourth page-local modal-backdrop.
        assert "confirmModal(" in body
        assert 'class="modal-backdrop"' not in body


class TestAccessCanCreateInPlace:
    """The audience that does not exist yet is the reason someone lands on
    Access and leaves. The control has to be in the group list itself."""

    def test_group_list_carries_a_create_control(self, seeded_app):
        c = seeded_app["client"]
        body = c.get("/admin/access", headers=_auth(seeded_app["admin_token"])).text
        # The control is the list's own first row now, not a toolbar button
        # with an id: `#ax-groups` is rewritten on every repaint, so it is
        # addressed by attribute and bound by delegation.
        assert "data-new-group" in body
        assert "New group" in body

    def test_it_opens_the_drawer_rather_than_navigating(self, seeded_app):
        """A link to /admin/groups loses the selection and the scroll — the
        whole reason this page is a workspace."""
        c = seeded_app["client"]
        body = c.get("/admin/access", headers=_auth(seeded_app["admin_token"])).text
        assert "AgnesGroupDrawer.open" in body
        assert '<a href="/admin/access">Create one' not in body


class TestTheFlowItself:
    """Read from the component, not from a page: this is where the one
    remaining step, and the absence of the other three, actually live."""

    def test_it_asks_for_a_name_and_stops(self):
        js = (STATIC / "js" / "components" / "group_drawer.js").read_text(encoding="utf-8")
        assert "gdw-name" in js and "gdw-desc" in js

    def test_the_duplicated_editors_are_gone_not_hidden(self):
        """Steps 2 and 3 were a second member editor and a second grant
        editor. /admin/access owns both; a dormant copy here is how the two
        would start disagreeing about one pair of tables.

        The drawer DOES seed a new group's first people, which is why this
        no longer bans the member endpoints outright — what it bans is the
        editor those endpoints were part of. The line between the two is
        drawn by the three tests below, not by the absence of a URL."""
        js = (STATIC / "js" / "components" / "group_drawer.js").read_text(encoding="utf-8")
        for gone in (
            "'/api/admin/grants'",
            "'/api/admin/access-overview'",
            "renderPeople",
            "renderAccess",
        ):
            assert gone not in js, f"drawer still carries {gone}"

    def test_seeding_is_additive_only(self):
        """A member editor is one that can take membership away. This one
        adds, and the roster that would let you do anything else is what
        /admin/access exists for — so no DELETE, and no roster row."""
        js = (STATIC / "js" / "components" / "group_drawer.js").read_text(encoding="utf-8")
        assert "'DELETE'" not in js, "the drawer can remove a member — that is the editor returning"
        assert "rmmember" not in js
        # What it may do: find someone, and put them in the new group. The
        # find itself goes through the shared lookup (js/people_search.js),
        # not a hand-rolled `/api/users` fetch of its own — see
        # TestPeopleSearchIsOneImplementation below for why that matters.
        assert "window.AgnesPeopleSearch.search(" in js and "/members" in js

    def test_seeding_is_creation_only(self):
        """On an existing group the field must be gone, not merely empty —
        two live places to add a member is the same divergence in slower
        motion."""
        js = (STATIC / "js" / "components" / "group_drawer.js").read_text(encoding="utf-8")
        assert "els.people.hidden = !!g" in js, "the people field survives into edit mode"

    def test_a_partial_seed_does_not_close_over_the_failure(self):
        """The group is created before anyone is added, so a half-failure
        has to stay on screen — closing would report success for work that
        did not happen."""
        js = (STATIC / "js" / "components" / "group_drawer.js").read_text(encoding="utf-8")
        assert "failed.length" in js and "st.picked = failed" in js

    def test_it_writes_through_the_existing_group_api(self):
        """No new storage and no batched submit — the group exists when the
        drawer closes, which is what makes landing in the workspace honest."""
        js = (STATIC / "js" / "components" / "group_drawer.js").read_text(encoding="utf-8")
        assert "'/api/admin/groups'" in js
        assert "'POST'" in js and "'PATCH'" in js


class TestDrawerChromeIsShared:
    """One drawer sheet, used by the group flow and the Add-data wizard —
    the point of extracting it."""

    def test_the_sheet_is_token_only(self):
        css = (STATIC / "css" / "drawer.css").read_text(encoding="utf-8")
        assert "--ds-surface" in css and "--ds-border" in css
        # Motion is honoured, not assumed.
        assert "prefers-reduced-motion" in css

    def test_add_data_wizard_rides_it(self, seeded_app):
        c = seeded_app["client"]
        body = c.get("/admin/data-sources", headers=_auth(seeded_app["admin_token"])).text
        assert 'id="ds-wizard-overlay"' in body
        assert "ds-drawer" in body
        assert "ds-modal-overlay" not in body
        # Its own JS drives these by id — the chrome changed, the flow did not.
        for el_id in (
            "ds-wizard-step-connect",
            "ds-wizard-step-tables",
            "ds-wizard-step-bundle",
            "ds-wizard-step-share",
            "ds-wizard-connect-btn",
            "ds-wizard-close",
        ):
            assert f'id="{el_id}"' in body, f"wizard lost {el_id}"


class TestOverviewCarriesTheTier:
    def test_grants_report_their_requirement(self, seeded_app):
        """Without it the editor draws every grant as Optional, so a grant
        saved as Automatic (in the drawer, or by `agnes admin grant`) reads
        back wrong on the page that owns the control."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        groups = c.get("/api/admin/groups", headers=_auth(token)).json()
        gid = groups[0]["id"]
        made = c.post(
            "/api/admin/grants",
            headers=_auth(token),
            json={
                "group_id": gid,
                "resource_type": "data_package",
                "resource_id": "tier-probe",
                "requirement": "required",
            },
        )
        assert made.status_code == 201, made.text

        overview = c.get("/api/admin/access-overview", headers=_auth(token)).json()
        row = next(g for g in overview["grants"] if g["resource_id"] == "tier-probe")
        assert row["requirement"] == "required"


class TestPeopleSearchIsOneImplementation:
    """The New-group modal's People field (js/components/group_drawer.js)
    and the group detail pane's own "add someone" search
    (admin_access.html, `.ax-people__find`) answer the same question — is
    there an account matching this text — and used to each carry their own
    copy of the fetch + response-shape handling to get there. Nothing kept
    the two copies agreeing, which is how the modal's copy went stale
    while the detail pane's kept working: same-looking code, two places to
    fix a bug in, one of them missed.

    `js/people_search.js` (`window.AgnesPeopleSearch.search`) is now the
    ONLY place that builds the `/api/users?search=` request; both call it.
    These tests pin that there is exactly one implementation left, not
    that the URL string happens to match today — a second, independently
    -written copy could rot the same way even if it starts out identical.
    """

    TEMPLATES = Path("app/web/templates")

    def test_the_shared_lookup_hits_the_admin_search_endpoint(self):
        js = (STATIC / "js" / "people_search.js").read_text(encoding="utf-8")
        assert "window.AgnesPeopleSearch" in js
        assert "/api/users" in js
        assert "?search=" in js and "encodeURIComponent" in js
        # Never a wrapped `{ users: [...] }` assumption without a bare-array
        # fallback — `GET /api/users` returns a bare JSON array.
        assert "Array.isArray" in js

    def test_it_is_loaded_globally_before_it_is_used(self):
        """Both callers live on pages that include `_app_scripts.html`; the
        helper has to be registered there, not copy-pasted into either
        template's own `<script src>` list."""
        app_scripts = (self.TEMPLATES / "_app_scripts.html").read_text(encoding="utf-8")
        assert "js/people_search.js" in app_scripts

    def test_group_drawer_calls_the_shared_lookup_not_its_own_fetch(self):
        js = (STATIC / "js" / "components" / "group_drawer.js").read_text(encoding="utf-8")
        assert "window.AgnesPeopleSearch.search(" in js
        # No second, hand-rolled build of the same URL — the exact
        # divergence that let this field go stale while the detail pane's
        # search kept working.
        assert "'/api/users'" not in js
        assert "?search=" not in js

    def test_group_detail_search_calls_the_shared_lookup_not_its_own_fetch(self):
        html = (self.TEMPLATES / "admin_access.html").read_text(encoding="utf-8")
        assert "window.AgnesPeopleSearch.search(" in html
        # The ax-find "add someone" box's OWN runFind must not still build
        # its own `/api/users?search=` URL — the shared helper is the only
        # thing allowed to.
        assert "USERS_LIST_API}?search=" not in html



class TestPeopleSearchDistinguishesErrorFromEmpty:
    """A non-ok response from `GET /api/users` (403/500/501, a network
    failure) used to collapse into the SAME empty array a genuine
    zero-match search produces — so a caller could not tell "nobody has
    that account" from "the search itself is broken right now", and
    rendered "No account matches that." for both. That is precisely what
    would hide a real outage behind a wrong "no such person" reading: the
    error text a reporter would need to diagnose the failure was thrown
    away by the helper before either caller ever saw it.

    `window.AgnesPeopleSearch.search()` now resolves `{ people, error }` —
    `error` is null only on a genuine (possibly empty) result. These tests
    pin that shape, and that BOTH callers render a distinct failure line
    when `error` is set, keeping the "No account matches" text reserved
    for `error === null`.
    """

    TEMPLATES = Path("app/web/templates")

    def test_the_shared_lookup_reports_error_on_non_ok(self):
        js = (STATIC / "js" / "people_search.js").read_text(encoding="utf-8")
        # Success resolves error: null; a non-ok response and a network
        # failure both set a human-readable error string instead of
        # silently returning the same empty array as a real zero-match.
        assert "error: null" in js
        assert "error:" in js and "r.status" in js
        assert "network error" in js

    def test_group_drawer_renders_the_failure_distinctly_from_no_match(self):
        js = (STATIC / "js" / "components" / "group_drawer.js").read_text(encoding="utf-8")
        assert "result.error" in js
        assert "gdw-found__error" in js
        # The no-match copy must still exist, and only outside the error
        # branch — an error must not fall through to "No account matches".
        assert "No account matches that." in js

    def test_group_drawer_error_css_uses_design_system_tokens(self):
        css = (STATIC / "css" / "group_drawer.css").read_text(encoding="utf-8")
        assert ".gdw-found__error" in css
        assert "var(--ds-" in css.split(".gdw-found__error", 1)[1].split("}", 1)[0]

    def test_group_detail_search_renders_the_failure_distinctly_from_no_match(self):
        html = (self.TEMPLATES / "admin_access.html").read_text(encoding="utf-8")
        assert "const { people, error } = await window.AgnesPeopleSearch.search(" in html
        assert "ax-res__msg--error" in html
        # The no-match / invite copy must still exist, and only reached
        # when the lookup succeeded with zero results.
        assert "No account for" in html or "No account matches" in html

    def test_admin_access_error_css_uses_design_system_tokens(self):
        html = (self.TEMPLATES / "admin_access.html").read_text(encoding="utf-8")
        assert ".ax-res__msg--error" in html
        after = html.split(".ax-res__msg--error", 1)[1].split("}", 1)[0]
        assert "var(--ds-" in after
