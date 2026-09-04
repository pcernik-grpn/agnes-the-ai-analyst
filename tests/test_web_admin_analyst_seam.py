"""The admin↔analyst seam: context contract, honest preview, no dead ends.

The 2026-08-18 IA investigation named three verbs the product had been
expressing as one undifferentiated "go somewhere else":

  * **Manage**  — object-scoped, in place (pinned by
    tests/test_web_detail_manage_cluster.py);
  * **Navigate** — rail Admin → /admin, always;
  * **Preview**  — person-parameterized simulation, never self-navigation
    dressed up as "View as analyst".

This suite pins the connective tissue between them — the context contract
(`?from=` origins that make every crossing lossless), the simulate lens's
deep links, and the two analyst dead-ends that used to answer with machine
tokens.
"""

from __future__ import annotations

from pathlib import Path

from tests.helpers.access_page import access_page_source

TEMPLATES = Path("app/web/templates")
RAIL = TEMPLATES / "_app_rail.html"
ADMIN_PKG = TEMPLATES / "admin_package_detail.html"
CATALOG_PKG = TEMPLATES / "catalog_package_detail.html"
# The Access page is a template plus the module it loads; the scans below
# want both halves. See tests/helpers/access_page.py.
ACCESS_READ_TEXT = access_page_source

_HTML = {"Accept": "text/html"}


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _make_package(seeded_app, slug: str, *, granted: bool) -> str:
    """A package created through the admin API; optionally granted to
    Everyone (with analyst1 enrolled) so the analyst is a legitimate reader.
    Returns the package id."""
    from src.repositories import user_group_members_repo, user_groups_repo

    c = seeded_app["client"]
    auth = _auth(seeded_app["admin_token"])
    created = c.post(
        "/api/admin/data-packages",
        headers=auth,
        json={"name": f"Seam {slug}", "slug": slug},
    )
    if created.status_code != 201:
        # Already created by an earlier test in this session — resolve the id.
        listing = c.get("/api/admin/data-packages", headers=auth).json()
        rows = listing if isinstance(listing, list) else listing.get("items", [])
        return next(p["id"] for p in rows if p.get("slug") == slug)
    pkg_id = created.json()["id"]
    if granted:
        everyone = user_groups_repo().get_by_name("Everyone")
        assert everyone, "the Everyone system group must exist"
        members = user_group_members_repo()
        if not members.has_membership("analyst1", everyone["id"]):
            members.add_member("analyst1", everyone["id"], source="test")
        grant = c.post(
            "/api/admin/grants",
            headers=auth,
            json={
                "group_id": everyone["id"],
                "resource_type": "data_package",
                "resource_id": pkg_id,
                "requirement": "available",
            },
        )
        assert grant.status_code in (200, 201, 409), grant.text
    return pkg_id


def _group_with(name: str, *user_ids: str) -> str:
    """A non-system group holding exactly the named seeded users.

    Grants in these tests go to a group somebody is EXPLICITLY enrolled in.
    The seeded users are not in ``Everyone`` on their own — that membership is
    a real ``user_group_members`` row ``ensure_everyone_membership()`` writes
    at sign-in, which the fixture never runs — so a grant to ``Everyone`` and
    a member who was never enrolled would assert nothing.
    """
    from src.repositories import user_group_members_repo, user_groups_repo

    groups = user_groups_repo()
    group = groups.get_by_name(name) or groups.create(name)
    members = user_group_members_repo()
    for uid in user_ids:
        if not members.has_membership(uid, group["id"]):
            members.add_member(uid, group["id"], source="test")
    return group["id"]


def _grant(seeded_app, *, group_id: str, resource_type: str, resource_id: str, requirement: str = "available") -> None:
    r = seeded_app["client"].post(
        "/api/admin/grants",
        headers=_auth(seeded_app["admin_token"]),
        json={
            "group_id": group_id,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "requirement": requirement,
        },
    )
    assert r.status_code in (200, 201, 409), r.text


def _make_plugin(
    *,
    marketplace_id: str = "mp-example",
    name: str = "seam-plugin",
    admin_disabled: bool = False,
) -> str:
    """A curated plugin on a registered marketplace. Returns its grant path
    (``<marketplace_id>/<plugin_name>``) — the id `resource_grants` stores."""
    from src.repositories import marketplace_plugins_repo, marketplace_registry_repo

    marketplace_registry_repo().register(
        id=marketplace_id,
        name="Example marketplace",
        url="https://example.com/marketplace.git",
    )
    marketplace_plugins_repo().replace_for_marketplace(
        marketplace_id,
        [{"name": name, "version": "1.0", "description": "A plugin shared by the organization."}],
    )
    if admin_disabled:
        marketplace_plugins_repo().set_admin_disabled(marketplace_id, name, True)
    return f"{marketplace_id}/{name}"


def _make_recipe(slug: str = "seam-recipe", title: str = "Seam recipe") -> str:
    from src.repositories import recipes_repo

    return recipes_repo().create(
        slug=slug,
        title=title,
        description="A prepared analysis an admin curated.",
        icon=None,
        color=None,
        sql_template="SELECT 1",
        related_table_ids=[],
    )


class TestTheContextContract:
    """Any link that crosses the admin↔analyst seam carries its origin in the
    URL (`?from=`), and the destination's back link honors it. The pattern is
    the one the store pages proved with `?from=admin-moderation`."""

    def test_the_admin_page_opens_the_analyst_page_with_its_origin(self) -> None:
        src = ADMIN_PKG.read_text()
        assert "?from=admin" in src, "the crossing must carry its origin"
        assert "Open analyst page" in src
        # The old label claimed a preview it never was: the admin arrives as
        # THEMSELVES, manage cluster visible. The access preview is the
        # Simulate lens, not this link.
        assert "View as analyst" not in src

    def test_an_admin_arriving_with_from_admin_gets_a_way_back(self, seeded_app) -> None:
        pkg_id = _make_package(seeded_app, "seam-roundtrip", granted=True)
        c = seeded_app["client"]
        html = c.get("/catalog/p/seam-roundtrip?from=admin", headers=_auth(seeded_app["admin_token"])).text
        assert "Back to Admin:" in html
        assert f"/admin/data-packages/{pkg_id}" in html

    def test_without_the_param_the_back_link_is_the_library(self, seeded_app) -> None:
        _make_package(seeded_app, "seam-roundtrip", granted=True)
        c = seeded_app["client"]
        html = c.get("/catalog/p/seam-roundtrip", headers=_auth(seeded_app["admin_token"])).text
        assert "Back to Admin:" not in html
        assert "/library?section=data_package" in html

    def test_the_override_is_admin_gated(self, seeded_app) -> None:
        """A non-admin pasting an admin's URL must NOT get a back link that
        403s on them — the param is inert for readers."""
        _make_package(seeded_app, "seam-roundtrip", granted=True)
        c = seeded_app["client"]
        r = c.get("/catalog/p/seam-roundtrip?from=admin", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 200, r.text
        assert "Back to Admin:" not in r.text

    def test_the_rail_lights_library_on_entity_detail_pages(self, seeded_app) -> None:
        """Every entity detail page the Library links out to keeps Library lit
        — the most-visited detail surfaces must never render "you are
        nowhere". Static half: the rule names the prefixes; behavioral half:
        the rendered catalog page carries an active rail row."""
        src = RAIL.read_text()
        for prefix in ("/catalog", "/memory/d", "/apps/detail", "/marketplace"):
            assert f"_path.startswith('{prefix}')" in src, prefix
        _make_package(seeded_app, "seam-roundtrip", granted=True)
        c = seeded_app["client"]
        html = c.get("/catalog/p/seam-roundtrip", headers=_auth(seeded_app["admin_token"])).text
        assert 'class="rail-i on" href="/library"' in html


class TestThePreviewVerb:
    """Simulate is the one honest access preview: person-parameterized,
    deep-linkable, and its fix-it links carry the person along."""

    def test_the_lens_takes_a_user_deep_link(self) -> None:
        src = ACCESS_READ_TEXT()
        assert 'params.get("user")' in src
        # Re-picking a person rewrites the URL so the preview survives a
        # round trip through a package page and back.
        assert 'url.searchParams.set("user", uid)' in src

    def test_share_it_lands_on_the_package_with_the_person(self) -> None:
        src = ACCESS_READ_TEXT()
        assert "?from=simulate&user=" in src
        # The old link — the package INDEX, person dropped — must be gone
        # from the stop row.
        assert '<a href="/admin/data-packages">Share it' not in src

    def test_the_package_page_renders_the_arrival_banner(self, seeded_app) -> None:
        pkg_id = _make_package(seeded_app, "seam-preview", granted=False)
        c = seeded_app["client"]
        html = c.get(
            f"/admin/data-packages/{pkg_id}?from=simulate&user=admin1",
            headers=_auth(seeded_app["admin_token"]),
        ).text
        assert "Fixing access for" in html
        assert "Back to preview:" in html
        # Rendered hrefs escape the ampersand.
        assert "/admin/access?lens=simulate&amp;user=admin1" in html

    def test_a_garbage_user_id_renders_the_page_without_the_banner(self, seeded_app) -> None:
        pkg_id = _make_package(seeded_app, "seam-preview", granted=False)
        c = seeded_app["client"]
        r = c.get(
            f"/admin/data-packages/{pkg_id}?from=simulate&user=no-such-user",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200
        assert "Fixing access for" not in r.text

    def test_dangling_grants_are_warnings_not_green_chips(self) -> None:
        src = ACCESS_READ_TEXT()
        assert "ax-chip warn" in src
        assert "Dangling grant" in src


class TestThePersonLensIsOneList:
    """One list, one vocabulary, one count per fact.

    The lens grew three renderings of the same package set: a row of chips
    above the panel, the panel's own rows, and a why-chain entry per package
    below it. Two of the three were prose, so the guard in
    `tests/test_access_vocabulary.py` — which matches labels as elements —
    could pass while the same Required package was worded two different ways
    two inches apart. And the chips capped themselves at six while the panel
    listed eight, so the screen showed two disagreeing counts a centimetre
    apart.

    The panel is the list now. The two facts a granted list cannot contain —
    what is NOT shared, and what is granted but points at nothing — are bands
    inside it, in the same row shape and the same vocabulary as everything
    else.

    Source-reading, for the reason the vocabulary suite gives: these rows are
    built in JS from a fetch, so they are not in the first byte and a test
    that reads the response body cannot see them.
    """

    def _src(self) -> str:
        return ACCESS_READ_TEXT()

    def test_the_chips_row_is_gone(self) -> None:
        src = self._src()
        # The green two-thirds: a restatement of the panel directly beneath.
        assert "Can use:" not in src
        # Its red counterpart made a claim that is simply false of an admin.
        assert "Cannot use:" not in src
        # And the container the row lived in — the two `.ax-chips` rules that
        # remain belong to the filter toolbar's chip row, which shares the
        # class name.
        assert '<div class="ax-chips">' not in src

    def test_the_exceptions_survive_as_bands_in_the_list(self) -> None:
        """Removing the chips must not remove what only they were saying."""
        src = self._src()
        assert '"Not shared with them"' in src
        assert '"Dangling grants"' in src
        # Each still carries the action it carried as a chip row.
        assert "?from=simulate&user=" in src          # share the unshared one
        assert "/admin/access?group=" in src          # fix the stale grant

    def test_no_band_cuts_its_list_silently(self) -> None:
        """`missing` was sliced to four in the chips and three in the chain,
        with nothing on screen saying so. A bounded band says what it left
        out — the repo's own no-silent-caps rule."""
        src = self._src()
        assert "missing.slice(0, 4)" not in src
        assert "missing.slice(0, 3)" not in src
        assert "more not shared with them" in src

    def test_the_route_is_in_the_row_it_explains(self) -> None:
        """`via <group>` was a chain entry per package under the panel. It is
        a cell in the panel row now, which is what let the chain's per-package
        rows go without losing the attribution."""
        src = self._src()
        assert "ax-preview__via" in src
        assert "const viaNames = new Map()" in src

    def test_a_tier_is_worded_in_exactly_one_place(self) -> None:
        """The panel's state chips. The chip suffix that used to word it here
        from this page's own grant rows is gone, and with it the second
        source that could disagree with the projection `/library` renders."""
        src = self._src()
        assert "in their stack" not in src
        assert "must add it" not in src
        assert ">In their Library<" in src

    def test_a_kind_the_panel_lists_gets_no_second_fold_line(self) -> None:
        """Plugins, recipes and memory are bands in the panel. The chain's
        per-type fold must skip them, or the duplication simply moves."""
        src = self._src()
        assert "const bandKinds = new Set(" in src
        assert "if (bandKinds.has(key)) {" in src
        # …except the one thing a band cannot carry: a grant pointing at
        # something no surface lists.
        assert "point at something not listed above" in src

    def test_admin_god_mode_is_stated_where_the_person_is(self) -> None:
        """It is not a grant, so it is not a row in a list of grants. It used
        to lead the chip row; it belongs in the sentence about who they are."""
        src = self._src()
        assert "they reach everything regardless of the grants below" in src


class TestNoDeadEnds:
    """The two analyst dead-ends the investigation found, each replaced with
    a door: language + a request path on the exists-but-not-granted 403, and
    an id→slug bridge on an admin URL a teammate pasted."""

    def test_not_shared_renders_language_and_a_request_action(self, seeded_app) -> None:
        _make_package(seeded_app, "seam-locked", granted=False)
        c = seeded_app["client"]
        r = c.get(
            "/catalog/p/seam-locked",
            headers={**_auth(seeded_app["analyst_token"]), **_HTML},
        )
        assert r.status_code == 403
        assert "package_not_shared" not in r.text, "the machine token must not print"
        assert "Not shared with you yet" in r.text
        assert "Seam seam-locked" in r.text, "the copy names the package"
        assert "copy-access-request" in r.text

    def test_the_same_door_opens_for_a_memory_domain(self, seeded_app) -> None:
        """A memory domain the caller cannot reach was a bare `access_denied`
        printed verbatim — the machine string the package page stopped showing.

        It is the same situation for the reader: it exists, an admin can share
        it, and there is a sentence to send them. So it is the same page, with
        the noun swapped. Naming the domain leaks nothing the 403 has not
        already confirmed (the route 404s one that does not exist).
        """
        from src.repositories import memory_domains_repo

        memory_domains_repo().create(
            name="Locked Domain",
            slug="seam-locked-domain",
            description="d",
            icon=None,
            color=None,
            created_by="admin",
        )
        r = seeded_app["client"].get(
            "/memory/d/seam-locked-domain",
            headers={**_auth(seeded_app["analyst_token"]), **_HTML},
        )
        assert r.status_code == 403
        assert "access_denied" not in r.text, "the machine token must not print"
        assert "not_shared" not in r.text
        assert "Not shared with you yet" in r.text
        assert "Locked Domain" in r.text
        assert "memory domain" in r.text, "the copy uses the right noun"
        assert "copy-access-request" in r.text

    def test_a_draft_package_is_not_reachable_by_its_slug(self, seeded_app) -> None:
        """Draft means hidden from every member-facing list. The detail route
        read the package straight from the repo, so an unpublished page was one
        guessed slug away from any member holding a grant — and it rendered as
        though it had shipped.

        404, not 403: browse behaves as though a draft is not there, and a 403
        would confirm the name of something the admin has not published.
        """
        from src.repositories import data_packages_repo

        pkg_id = _make_package(seeded_app, "seam-draft", granted=True)
        data_packages_repo().update(pkg_id, status="draft")
        c = seeded_app["client"]
        r = c.get("/catalog/p/seam-draft", headers={**_auth(seeded_app["analyst_token"]), **_HTML})
        assert r.status_code == 404
        # The admin who is writing it still opens it — that is the whole point
        # of a draft, and a gate that locked the author out would be worse than
        # the leak it closed.
        assert c.get("/catalog/p/seam-draft", headers={**_auth(seeded_app["admin_token"]), **_HTML}).status_code == 200

    def test_an_admin_url_bridges_to_the_page_the_reader_can_try(self, seeded_app) -> None:
        pkg_id = _make_package(seeded_app, "seam-bridge", granted=True)
        c = seeded_app["client"]
        r = c.get(
            f"/admin/data-packages/{pkg_id}",
            headers={**_auth(seeded_app["analyst_token"]), **_HTML},
        )
        assert r.status_code == 403
        assert "/catalog/p/seam-bridge" in r.text, "the 403 must name the analyst page"

    def test_the_bridge_also_fires_for_a_reader_with_no_grant_at_all(self, seeded_app) -> None:
        """The bridge lookup is NOT scoped to packages the caller can reach, so
        this pins the real blast radius rather than the happy path.

        Deliberate, on the argument in ``app/main.py``: ``/catalog/p/{slug}``
        already 403s-with-name for any authenticated caller regardless of grant
        (data packages are the existence-visible kind; collections 404), so the
        bridge reveals nothing that split did not. What it adds is reachability
        from an admin-only URL — and only to someone who already holds the raw
        id, which is ``pkg_`` + 12 hex, not a guessable slug. Possessing the id
        is the access control here.

        Pinned because it is the assertion that makes the decision visible: if
        the bridge is ever narrowed to granted packages, this test is what has
        to change, deliberately, instead of the scope quietly shifting.
        """
        pkg_id = _make_package(seeded_app, "seam-bridge-ungranted", granted=False)
        c = seeded_app["client"]
        r = c.get(
            f"/admin/data-packages/{pkg_id}",
            headers={**_auth(seeded_app["analyst_token"]), **_HTML},
        )
        assert r.status_code == 403
        assert "/catalog/p/seam-bridge-ungranted" in r.text, (
            "the bridge stopped firing for an ungranted reader — if that was intended, "
            "narrow it deliberately and rewrite this test"
        )


class TestPublisherKindAtCreate:
    """An admin publishing from the builder stands behind it as the
    workspace; a non-admin claiming the Organization mark is refused with the
    same authority the post-hoc publisher toggle requires."""

    def test_a_non_admin_cannot_claim_organization(self, seeded_app) -> None:
        c = seeded_app["client"]
        r = c.post(
            "/api/store/entities/from-markdown",
            headers=_auth(seeded_app["analyst_token"]),
            json={
                "name": "seam-org-claim",
                "description": "Use when pinning that the Organization mark is admin-only at create.",
                "category": "Other",
                "skill_md": (
                    "Step one: try to publish as the organization without admin rights. "
                    "Step two: observe the refusal. Step three: publish as Community instead."
                ),
                "publisher_kind": "organization",
            },
        )
        assert r.status_code == 403, r.text
        assert "admin_required_for_publisher_kind" in r.text

    def test_an_admin_publishes_as_the_organization(self, seeded_app) -> None:
        c = seeded_app["client"]
        r = c.post(
            "/api/store/entities/from-markdown",
            headers=_auth(seeded_app["admin_token"]),
            json={
                "name": "seam-org-skill",
                "description": "Use when pinning that an admin's create lands with the Organization mark.",
                "category": "Other",
                "skill_md": (
                    "Step one: publish this skill as an admin with publisher_kind set to organization. "
                    "Step two: read the created entity back from the response body of the create call. "
                    "Step three: assert the publisher_kind field carries the organization trust mark. "
                    "This body is deliberately verbose because the skill linter enforces a 200-character "
                    "minimum on content and refuses shorter bodies with body_too_short."
                ),
                "publisher_kind": "organization",
            },
        )
        assert r.status_code == 201, r.text
        assert r.json().get("publisher_kind") == "organization"


class TestTheLibraryShapedPreview:
    """Simulate's third piece: beside the why-chain, a Library-shaped panel
    showing what the person's /library actually renders — computed by the
    SAME StackResolver.browse projection that page uses (grants-based, no
    admin god-mode), so the preview cannot drift from the page it predicts.
    The rows speak the product's one access vocabulary: "In stack ·
    Automatic", "In stack", "Not in stack yet · Optional"."""

    API = "/api/admin/users/{uid}/library-preview"

    def test_the_endpoint_is_admin_only(self, seeded_app) -> None:
        c = seeded_app["client"]
        r = c.get(self.API.format(uid="analyst1"), headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code in (401, 403), r.text

    def test_an_unknown_person_is_a_404_not_an_empty_preview(self, seeded_app) -> None:
        c = seeded_app["client"]
        r = c.get(self.API.format(uid="u_nobody"), headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 404, r.text

    def test_an_unsubscribed_available_grant_is_granted_not_delivered(self, seeded_app) -> None:
        """An available grant the person never subscribed to is the state the
        preview exists to expose: granted ≠ delivered. ``materialized`` is
        False in EVERY membership mode until they subscribe; ``in_stack``
        follows the instance's mode (auto: every grant is a membership;
        classic: not until subscribed), and the preview reports which mode
        it computed under so the pane words the state honestly."""
        pkg_id = _make_package(seeded_app, "seam-preview-pkg", granted=True)
        c = seeded_app["client"]
        r = c.get(self.API.format(uid="analyst1"), headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["mode"] in ("auto", "classic")
        packages = next(s for s in body["sections"] if s["kind"] == "data_package")
        row = next(i for i in packages["items"] if i["id"] == pkg_id)
        assert row["requirement"] == "available"
        assert row["materialized"] is False
        assert row["in_stack"] is (body["mode"] == "auto")
        # The row links to the analyst page the preview is a projection of.
        assert row["href"] == "/catalog/p/seam-preview-pkg"

    def test_a_subscription_flips_the_row_into_the_stack(self, seeded_app) -> None:
        pkg_id = _make_package(seeded_app, "seam-preview-sub", granted=True)
        c = seeded_app["client"]
        sub = c.post(
            "/api/stack/subscribe",
            headers=_auth(seeded_app["analyst_token"]),
            json={"resource_type": "data_package", "resource_id": pkg_id},
        )
        assert sub.status_code in (200, 201), sub.text
        r = c.get(self.API.format(uid="analyst1"), headers=_auth(seeded_app["admin_token"]))
        packages = next(s for s in r.json()["sections"] if s["kind"] == "data_package")
        row = next(i for i in packages["items"] if i["id"] == pkg_id)
        assert row["in_stack"] is True
        assert row["materialized"] is True

    def test_the_pane_fetches_and_renders_the_preview(self) -> None:
        """Template hooks: the Simulate lens fetches the endpoint and renders
        the panel in the standardized vocabulary."""
        src = ACCESS_READ_TEXT()
        assert "/library-preview" in src
        assert "ax-preview" in src
        assert "What their Library shows" in src
        # The chips speak the person's words now, not the admin's — this
        # lens exists to show what somebody else sees, so "In stack ·
        # Automatic" (the retired admin vocabulary) was the one place it
        # could least afford to say it. See tests/test_access_vocabulary.py.
        assert "Required by your admin" in src
        assert "In their Library" in src
        assert "Optional — no local copy yet" in src

    def test_the_pane_renders_whatever_kinds_the_api_sends(self) -> None:
        """The band renderer is generic over ``sections`` and a kind with no
        admin page falls back to the row's own analyst href — which is what
        lets a widened preview reach the panel with no per-kind JS, and what
        keeps the next kind from needing a template change either."""
        src = ACCESS_READ_TEXT()
        assert "preview.sections.map(" in src
        assert "adminPage ? adminPage(it.id) : it.href" in src


class TestThePreviewCoversEveryGrantedKind:
    """Simulate answers for the kinds the Library actually shows.

    It used to iterate two — data packages and memory domains — so "I can see
    it in admin but they cannot see it in their Library" got a real answer for
    governed data and silence for a curated plugin, which is the same question
    about a different row. Every kind here is resolved the way ``/library``
    resolves it, so the preview cannot claim something the page won't render.
    """

    API = "/api/admin/users/{uid}/library-preview"

    def _preview(self, seeded_app, uid: str) -> dict:
        r = seeded_app["client"].get(self.API.format(uid=uid), headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 200, r.text
        return r.json()

    def _section(self, body: dict, kind: str) -> dict | None:
        return next((s for s in body["sections"] if s["kind"] == kind), None)

    def test_a_granted_plugin_is_in_the_preview(self, seeded_app) -> None:
        """The half the lens used to stay silent on."""
        path = _make_plugin()
        gid = _group_with("seam-plugin-readers", "analyst1")
        _grant(seeded_app, group_id=gid, resource_type="marketplace_plugin", resource_id=path)

        section = self._section(self._preview(seeded_app, "analyst1"), "marketplace_plugin")
        assert section is not None, "a granted plugin must reach the preview"
        assert section["label"] == "Plugins"
        row = next(i for i in section["items"] if i["id"] == path)
        assert row["name"] == "seam-plugin"
        assert row["href"] == "/marketplace/curated/mp-example/seam-plugin"

    def test_an_uninstalled_available_plugin_is_eligible_not_delivered(self, seeded_app) -> None:
        """A plugin is the one granted kind whose membership is NOT automatic:
        an available-tier grant nobody installed is eligibility, and their
        Claude Code does not load it. Saying "in stack" there is exactly the
        false reassurance this pane exists to avoid."""
        path = _make_plugin(name="seam-uninstalled")
        gid = _group_with("seam-plugin-readers", "analyst1")
        _grant(seeded_app, group_id=gid, resource_type="marketplace_plugin", resource_id=path)

        section = self._section(self._preview(seeded_app, "analyst1"), "marketplace_plugin")
        row = next(i for i in section["items"] if i["id"] == path)
        assert row["requirement"] == "available"
        assert row["in_stack"] is False
        assert row["materialized"] is False

    def test_a_required_plugin_grant_is_already_served_to_them(self, seeded_app) -> None:
        path = _make_plugin(name="seam-required")
        gid = _group_with("seam-plugin-readers", "analyst1")
        _grant(
            seeded_app,
            group_id=gid,
            resource_type="marketplace_plugin",
            resource_id=path,
            requirement="required",
        )

        section = self._section(self._preview(seeded_app, "analyst1"), "marketplace_plugin")
        row = next(i for i in section["items"] if i["id"] == path)
        assert row["requirement"] == "required"
        assert row["in_stack"] is True
        assert row["materialized"] is True

    def test_a_plugin_granted_to_a_group_they_are_not_in_stays_out(self, seeded_app) -> None:
        """The non-member proof. ``viewer1`` is in no group holding the grant,
        so their preview must not list the plugin — an admin simulating them
        is asking what THEY see, and no answer may be god-mode."""
        path = _make_plugin()
        gid = _group_with("seam-plugin-readers", "analyst1")
        _grant(seeded_app, group_id=gid, resource_type="marketplace_plugin", resource_id=path)

        body = self._preview(seeded_app, "viewer1")
        assert body["sections"] == []

    def test_an_admin_disabled_plugin_is_in_nobodys_library(self, seeded_app) -> None:
        """Admin-disabled is instance-wide "does not exist" for every
        user-facing surface, grants notwithstanding — the same post-filter
        /library applies."""
        path = _make_plugin(name="seam-disabled", admin_disabled=True)
        gid = _group_with("seam-plugin-readers", "analyst1")
        _grant(seeded_app, group_id=gid, resource_type="marketplace_plugin", resource_id=path)

        assert self._section(self._preview(seeded_app, "analyst1"), "marketplace_plugin") is None

    def test_a_granted_recipe_is_in_the_preview(self, seeded_app) -> None:
        recipe_id = _make_recipe()
        gid = _group_with("seam-recipe-readers", "analyst1")
        _grant(seeded_app, group_id=gid, resource_type="recipe", resource_id=recipe_id)

        section = self._section(self._preview(seeded_app, "analyst1"), "recipe")
        assert section is not None
        assert section["label"] == "Recipes"
        row = next(i for i in section["items"] if i["id"] == recipe_id)
        assert row["name"] == "Seam recipe"
        assert row["href"] == "/catalog/r/seam-recipe"
        # Nothing to opt into and nothing to download: the grant IS the
        # reading right, and `agnes pull` does not distribute recipes.
        assert row["in_stack"] is True
        assert row["materialized"] is False

    def test_a_recipe_granted_elsewhere_stays_out(self, seeded_app) -> None:
        recipe_id = _make_recipe(slug="seam-recipe-private", title="Private seam recipe")
        gid = _group_with("seam-recipe-readers", "analyst1")
        _grant(seeded_app, group_id=gid, resource_type="recipe", resource_id=recipe_id)

        assert self._preview(seeded_app, "viewer1")["sections"] == []

    def test_the_governed_kinds_keep_their_own_sections(self, seeded_app) -> None:
        """Widening adds bands; it does not reshape the two that were here.
        The order mirrors the Library's own reading order, so the preview
        looks like the page it predicts."""
        pkg_id = _make_package(seeded_app, "seam-preview-mixed", granted=True)
        path = _make_plugin(name="seam-mixed")
        recipe_id = _make_recipe(slug="seam-recipe-mixed", title="Mixed seam recipe")
        gid = _group_with("seam-mixed-readers", "analyst1")
        _grant(seeded_app, group_id=gid, resource_type="marketplace_plugin", resource_id=path)
        _grant(seeded_app, group_id=gid, resource_type="recipe", resource_id=recipe_id)

        body = self._preview(seeded_app, "analyst1")
        assert [s["kind"] for s in body["sections"]] == ["data_package", "marketplace_plugin", "recipe"]
        packages = self._section(body, "data_package")
        row = next(i for i in packages["items"] if i["id"] == pkg_id)
        assert row["requirement"] == "available"
        assert row["href"] == "/catalog/p/seam-preview-mixed"

    def test_the_widened_preview_is_still_admin_only(self, seeded_app) -> None:
        """More kinds must not come with a wider door."""
        path = _make_plugin()
        gid = _group_with("seam-plugin-readers", "analyst1")
        _grant(seeded_app, group_id=gid, resource_type="marketplace_plugin", resource_id=path)

        r = seeded_app["client"].get(
            self.API.format(uid="analyst1"),
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 403, r.text
