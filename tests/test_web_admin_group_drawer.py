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
        would start disagreeing about one pair of tables."""
        js = (STATIC / "js" / "components" / "group_drawer.js").read_text(encoding="utf-8")
        for gone in (
            "'/api/admin/grants'",
            "'/api/admin/access-overview'",
            "'/api/users'",
            "/members",
            "renderPeople",
            "renderAccess",
        ):
            assert gone not in js, f"drawer still carries {gone}"

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
