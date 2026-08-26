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

TEMPLATES = Path("app/web/templates")
RAIL = TEMPLATES / "_app_rail.html"
ADMIN_PKG = TEMPLATES / "admin_package_detail.html"
CATALOG_PKG = TEMPLATES / "catalog_package_detail.html"
ACCESS = TEMPLATES / "admin_access.html"

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
        src = ACCESS.read_text()
        assert 'params.get("user")' in src
        # Re-picking a person rewrites the URL so the preview survives a
        # round trip through a package page and back.
        assert 'url.searchParams.set("user", uid)' in src

    def test_share_it_lands_on_the_package_with_the_person(self) -> None:
        src = ACCESS.read_text()
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
        src = ACCESS.read_text()
        assert "ax-chip warn" in src
        assert "Dangling grant" in src


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
        src = ACCESS.read_text()
        assert "/library-preview" in src
        assert "ax-preview" in src
        assert "What their Library shows" in src
        assert "In stack · Automatic" in src
        assert "Not in stack yet · Optional" in src
