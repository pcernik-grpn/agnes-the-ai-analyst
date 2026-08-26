"""/library as the single source of truth for store entities.

The Library lists every store entity the caller is allowed to SEE, using the
store's own visibility rule (the same one `/api/marketplace` browse applies):

  * ``approved`` — readable by every authenticated user, i.e. shared with
    everyone, so it appears in every eligible user's Library; and
  * the caller's OWN entries whatever their review state, so a skill you are
    still drafting is in your Library and nobody else's.

Someone else's ``pending`` / ``hidden`` entry is NOT listed: an admin may be
able to read it, but a submission in review is not shared with anyone
(moderation lives at /admin/store).

Visibility and Stack membership are separate properties. Visibility decides who
can discover the entity; an install row decides whether the default agent
actually uses it. So authoring an entity does not put it in your Stack, and
Add/Remove from stack writes ``POST``/``DELETE
/api/store/entities/{id}/install`` — a different API from an artefact's, which
is why each row carries its own ``data-stack-endpoint``.

AGENTS are deliberately not swept — they keep their own surface at /agents — but
an agent the caller INSTALLED still lists.
"""

from __future__ import annotations

import re
import uuid

import pytest


@pytest.fixture(autouse=True)
def _rail_layout(monkeypatch):
    """This file exercises the RAIL redesign's unified /library. Topnav keeps
    the legacy collections page (the /catalog pattern) — guarded by
    tests/test_ui_layout_theme.py::TestDefaultContentParity."""
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")


@pytest.fixture(autouse=True)
def _paper_theme(monkeypatch):
    """Trust markers are opt-in to the paper theme (css/trustmark.css is scoped
    to it, and `mark()` renders nothing without `paper=True`), so the assertions
    in this file only hold under paper. A default blue instance keeps its own
    spelling — no markers on Library rows at all, which is the pre-v113
    behaviour and is asserted by
    tests/test_ui_layout_theme.py::test_default_instance_renders_no_ds_trust_marker.
    """
    monkeypatch.setenv("AGNES_INSTANCE_THEME", "paper")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _entity(*, owner: str, owner_name: str, etype: str, name: str, status: str) -> str:
    """Seed one store entity and return its id."""
    from src.db import get_system_db
    from src.repositories.store_entities import StoreEntitiesRepository

    eid = uuid.uuid4().hex
    StoreEntitiesRepository(get_system_db()).create(
        id=eid,
        owner_user_id=owner,
        owner_username=owner_name,
        type=etype,
        name=name,
        description=f"{name} description",
        category="Productivity",
        version="1.0.0",
        visibility_status=status,
    )
    return eid


def _install(entity_id: str, user_id: str) -> None:
    from src.db import get_system_db
    from src.repositories.user_store_installs import UserStoreInstallsRepository

    UserStoreInstallsRepository(get_system_db()).install(user_id=user_id, entity_id=entity_id)


def _row(text: str, title: str) -> str | None:
    """The one table row whose data-title is ``title``."""
    m = re.search(r'<tr class="lib-row[^"]*"[^>]*data-title="' + re.escape(title) + r'".*?</tr>', text, re.S)
    return m.group(0) if m else None


# ---------------------------------------------------------------------------
# What the Library contains
# ---------------------------------------------------------------------------


def test_skill_shared_with_everyone_appears_in_every_library(seeded_app):
    """An approved skill is readable by every authenticated user — that IS
    "shared with everyone" — so it belongs in the Library of someone who neither
    authored nor installed it, attributed to its owner."""
    eid = _entity(owner="admin", owner_name="admin", etype="skill", name="Shared Skill", status="approved")
    assert eid

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row(text, "Shared Skill")
    assert row, "an approved skill is missing from another user's Library"
    assert 'data-ownership="shared_with_me"' in row
    assert 'data-visibility="workspace"' in row
    assert 'data-type="skill"' in row


def test_someone_elses_unapproved_skill_stays_out(seeded_app):
    """Pending / hidden entries belonging to others are submissions in review,
    not things shared with anyone."""
    _entity(owner="admin", owner_name="admin", etype="skill", name="Still In Review", status="pending")
    _entity(owner="admin", owner_name="admin", etype="skill", name="Hidden Away", status="hidden")

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    assert "Still In Review" not in text
    assert "Hidden Away" not in text


def test_your_own_unpublished_skill_is_yours_alone(seeded_app):
    """A private item is visible only to its creator — and it reports its real
    store state rather than pretending to be published."""
    _entity(owner="analyst1", owner_name="analyst1", etype="skill", name="My Draft Skill", status="hidden")

    mine = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row(mine, "My Draft Skill")
    assert row, "the caller's own unpublished skill is missing from their Library"
    assert 'data-ownership="mine"' in row
    assert 'data-visibility="private"' in row

    theirs = seeded_app["client"].get("/library", headers=_auth(seeded_app["admin_token"])).text
    assert "My Draft Skill" not in theirs


def test_plugins_are_swept_too_and_agents_are_not(seeded_app):
    """Skills and plugins are listed whether or not they are installed. Agents
    keep their own surface at /agents, so an approved one is not swept."""
    _entity(owner="admin", owner_name="admin", etype="plugin", name="Shared Plugin", status="approved")
    _entity(owner="admin", owner_name="admin", etype="agent", name="Shared Agent", status="approved")

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    assert _row(text, "Shared Plugin"), "an approved plugin is missing from the Library"
    assert "Shared Agent" not in text


def test_installed_agent_still_lists(seeded_app):
    """The one way an agent reaches the Library: the caller installed it."""
    eid = _entity(owner="admin", owner_name="admin", etype="agent", name="Installed Agent", status="approved")
    _install(eid, "analyst1")

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row(text, "Installed Agent")
    assert row, "an installed agent is missing from the Library"
    assert 'data-type="agent"' in row


def test_an_entity_is_listed_exactly_once_when_installed(seeded_app):
    """Regression: skills/plugins come from the sweep whether installed or not,
    so the installed-items pass must not list them a second time."""
    eid = _entity(owner="admin", owner_name="admin", etype="skill", name="Installed Once", status="approved")
    _install(eid, "analyst1")

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    assert text.count('data-title="Installed Once"') == 1


# ---------------------------------------------------------------------------
# Stack membership — a separate property, with its own API
# ---------------------------------------------------------------------------


def test_stack_membership_is_separate_from_visibility(seeded_app):
    """Sharing controls discovery; the Stack controls whether the agent uses it.
    A skill shared with everyone but not installed is therefore visible AND
    out-of-stack, offering Add — wired to the store's install endpoint, not an
    artefact's stack endpoint."""
    eid = _entity(owner="admin", owner_name="admin", etype="skill", name="Discoverable Only", status="approved")

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row(text, "Discoverable Only")
    assert row
    assert 'data-visibility="workspace"' in row  # everyone can discover it
    assert 'data-stack="available"' in row  # …but the agent can't use it yet
    assert f'data-add-to-stack="{eid}"' in row
    assert f'data-stack-endpoint="/api/store/entities/{eid}/install"' in row


def test_installing_makes_the_membership_removable(seeded_app):
    """An install IS the membership, and it is the caller's to drop again — so
    the row offers Remove against the same endpoint."""
    eid = _entity(owner="admin", owner_name="admin", etype="skill", name="Added Skill", status="approved")
    _install(eid, "analyst1")

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row(text, "Added Skill")
    assert row
    assert 'data-stack="in_stack"' in row
    assert f'data-remove-from-stack="{eid}"' in row
    assert f'data-stack-endpoint="/api/store/entities/{eid}/install"' in row


def test_authoring_does_not_add_to_your_stack(seeded_app):
    """Creating a skill puts it in your Library, not in your Stack: the two are
    separate properties, so a fresh one is visible-and-addable."""
    _entity(owner="analyst1", owner_name="analyst1", etype="skill", name="Freshly Authored", status="approved")

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row(text, "Freshly Authored")
    assert row, "a newly created skill must appear in its creator's Library"
    assert 'data-stack="available"' in row
    assert "data-add-to-stack=" in row


def test_admin_required_grant_is_locked_in_stack(seeded_app):
    """An admin-required resource reads "In stack" like any other member — it is
    one — and is marked by a lock plus the locked tooltip. There is no control
    beside it: the membership is the admin's, not the caller's."""
    from src.db import get_system_db
    from src.repositories import data_packages_repo
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    conn = get_system_db()
    groups = UserGroupsRepository(conn)
    grp = groups.get_by_name("req-lock-grp") or groups.create(name="req-lock-grp", description="t", created_by="t")
    UserGroupMembersRepository(conn).add_member("analyst1", grp["id"], source="admin", added_by="t")
    pkg = data_packages_repo().create(
        name="Mandated Data", slug="mandated-data", description="d", icon=None, color=None, created_by="admin"
    )
    ResourceGrantsRepository(conn).create(
        group_id=grp["id"], resource_type="data_package", resource_id=pkg, assigned_by="admin", requirement="required"
    )

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row(text, "Mandated Data")
    assert row
    assert 'data-stack="in_stack"' in row
    assert "Required by your admin and cannot be removed from your stack." in row
    # No control: neither Add nor Remove is the caller's to press.
    assert "data-add-to-stack=" not in row
    assert "data-remove-from-stack=" not in row


# ---------------------------------------------------------------------------
# Trust markers — two chips in the Owner column (org / verified) plus the
# opt-in community indicator, which rides the item's NAME rather than the
# Owner column and is icon-only.
# ---------------------------------------------------------------------------


def _set_verification(entity_id: str, state: str) -> None:
    from src.db import get_system_db
    from src.repositories.store_entities import StoreEntitiesRepository

    StoreEntitiesRepository(get_system_db()).set_verification(entity_id, state, by_user_id="admin")


def _set_publisher_kind(entity_id: str, kind: str) -> None:
    from src.db import get_system_db

    get_system_db().execute(
        "UPDATE store_entities SET publisher_kind = ? WHERE id = ?",
        [kind, entity_id],
    )


def test_unverified_community_marker_present_by_default(seeded_app, monkeypatch):
    """No row is left silently unlabelled: with nothing configured, an unverified
    item states its provenance like the other two levels do.

    This asserted the opposite until the default flipped. The old rationale was
    upgrade parity — but the whole vocabulary is gated to paper (`mark()` renders
    nothing without `paper=True`), so a default blue instance grows no markers
    regardless of this flag, and off bought parity nowhere. What it did buy was a
    Library where Organization and Verified rows carried a marker and every
    unverified row carried none, which reads as the markers being broken rather
    than as a third provenance level. The escape hatch is the next test.
    """
    monkeypatch.delenv("AGNES_LIBRARY_SHOW_UNVERIFIED_TRUST", raising=False)
    _entity(owner="admin", owner_name="admin", etype="skill", name="Default On Skill", status="approved")

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row(text, "Default On Skill")
    assert row, "approved skill must appear in library"
    assert "ds-trust--community" in row


def test_unverified_chip_absent_when_flag_explicitly_off(seeded_app, monkeypatch):
    """The flag survives as an escape hatch: an instance that prefers the older
    silent default (unverified == no marker) can still have it, and turning it off
    must suppress the marker completely rather than merely restyling it."""
    monkeypatch.setenv("AGNES_LIBRARY_SHOW_UNVERIFIED_TRUST", "false")
    _entity(owner="admin", owner_name="admin", etype="skill", name="Flag Off Skill", status="approved")

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row(text, "Flag Off Skill")
    assert row, "approved skill must appear in library"
    assert "ds-trust--community" not in row
    assert "Community" not in row


def test_unverified_chip_renders_when_flag_on(seeded_app, monkeypatch):
    """With the flag enabled, a user-authored unverified Store entity renders the
    community indicator — icon only, with the explanation on hover."""
    monkeypatch.setenv("AGNES_LIBRARY_SHOW_UNVERIFIED_TRUST", "true")
    _entity(owner="admin", owner_name="admin", etype="skill", name="Community Skill", status="approved")
    # verification_state defaults to 'none' on create — unverified by definition.

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row(text, "Community Skill")
    assert row, "approved skill must appear in library"
    assert "ds-trust--community" in row
    # The sentence is both the hover tooltip and the accessible name, so the
    # marker is never colour- or shape-only. Someone else's item, so the
    # authorship half of the sentence is the "other users" reading.
    #
    # `data-tip`, NOT `title`: the marker rides the page's fast tooltip, because
    # the native one's OS show delay is too slow for the only affordance that
    # explains the glyph. A `title` here would also double-fire a second, slower
    # bubble on top of the first.
    tip = "Community item — shared by other users and not verified by your organization."
    assert f'data-tip="{tip}"' in row
    assert f'aria-label="{tip}"' in row
    assert f'title="{tip}"' not in row
    assert "data-tip-instant" in row
    # ICON ONLY: the word "Community" survives in the tooltip sentence, but the
    # retired amber text chip must not come back.
    assert "lib-trust--unverified" not in row  # retired amber text chip
    assert ">Community<" not in row


def test_community_marker_on_your_own_item_does_not_claim_someone_else_shared_it(seeded_app, monkeypatch):
    """Same glyph, same meaning ("nobody has verified this"), honest sentence: on
    the caller's OWN unverified upload, "shared by other users" is false."""
    monkeypatch.setenv("AGNES_LIBRARY_SHOW_UNVERIFIED_TRUST", "true")
    _entity(owner="analyst1", owner_name="Analyst", etype="skill", name="My Own Skill", status="approved")

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row(text, "My Own Skill")
    assert row, "the caller's own approved skill must appear in their library"
    assert "ds-trust--community" in row
    assert 'data-tip="Community item — yours, and not verified by your organization."' in row
    assert "shared by other users" not in row


def test_trust_markers_ride_the_name_and_the_owner_column_holds_only_a_name(seeded_app, monkeypatch):
    """Every trust claim is a statement about the ITEM, so all of them sit on the
    title line inside the name cell — ahead of the Owner cell, which is now a name
    and nothing else."""
    monkeypatch.setenv("AGNES_LIBRARY_SHOW_UNVERIFIED_TRUST", "true")
    _entity(owner="admin", owner_name="admin", etype="skill", name="Placement Skill", status="approved")

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row(text, "Placement Skill")
    assert row, "approved skill must appear in library"
    # Inside the title line, which is inside the name cell.
    assert "lib-name-titlerow" in row
    assert row.index("lib-name-titlerow") < row.index("ds-trust")
    # …and before the Owner cell opens, so it cannot be read as part of it.
    assert row.index("ds-trust") < row.index("lib-owner-name")
    # No chip of any kind is left in the Owner cell.
    assert "lib-trust " not in row and 'lib-trust"' not in row


def test_verified_marker_rides_the_name_and_is_never_gated(seeded_app, monkeypatch):
    """A verified entity carries the verified marker regardless of the unverified
    flag — the flag only ever gated the community branch — and it excludes the
    community marker, the two being mutually exclusive."""
    for flag, name in (("true", "Verified Flag On"), ("false", "Verified Flag Off")):
        monkeypatch.setenv("AGNES_LIBRARY_SHOW_UNVERIFIED_TRUST", flag)
        eid = _entity(owner="admin", owner_name="admin", etype="skill", name=name, status="approved")
        _set_verification(eid, "verified")

        text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
        row = _row(text, name)
        assert row, "approved skill must appear in library"
        assert "ds-trust--verified" in row
        assert 'data-tip="Verified by your organization."' in row
        assert "ds-trust--community" not in row
        # The retired green text chip must not come back.
        assert "lib-trust--verified" not in row
        assert ">Verified<" not in row


def test_default_theme_renders_no_trust_markers_on_populated_rows(seeded_app, monkeypatch):
    """The default-look guarantee, asserted against REAL rows.

    Overrides the module's autouse paper fixture. This exists because the
    equivalent assertion in tests/test_ui_layout_theme.py runs against a client
    whose Library is empty, so it passes vacuously — no rows, no markers, no
    information. Here all three trust levels are seeded and the flag is on, so
    every marker WOULD render under paper; under the default theme none may.

    Library rows carried no trust markers at all before v113, so "none" is the
    pre-existing look, not a degraded one.
    """
    # The trust vocabulary is gated to paper at every mark() callsite, so the
    # theme has to be stated: paper became the DEFAULT in Wave 0 (2026-08), and
    # `delenv` now selects the look where the markers are supposed to render.
    monkeypatch.setenv("AGNES_INSTANCE_THEME", "blue")
    monkeypatch.setenv("AGNES_LIBRARY_SHOW_UNVERIFIED_TRUST", "true")
    org = _entity(owner="admin", owner_name="admin", etype="skill", name="Blue Org", status="approved")
    _set_publisher_kind(org, "organization")
    _entity(owner="analyst1", owner_name="analyst1", etype="skill", name="Blue Community", status="approved")

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    assert _row(text, "Blue Org"), "seeded org skill must be listed"
    assert _row(text, "Blue Community"), "seeded community skill must be listed"
    # Emitted markup only — the page's CSS comments legitimately name the class.
    assert 'class="ds-trust' not in text
    assert "ds-trust--org" not in text
    assert "ds-trust--community" not in text


def test_organization_published_item_carries_the_org_marker_on_the_name(seeded_app, monkeypatch):
    """All three trust levels mark the name, so the strongest one is not the single
    exception that expresses itself in a different column. The Owner cell still
    reads "Your organization" as well; that repetition is accepted deliberately in
    exchange for a trust axis with no hole in it."""
    monkeypatch.setenv("AGNES_LIBRARY_SHOW_UNVERIFIED_TRUST", "true")
    eid = _entity(owner="admin", owner_name="admin", etype="skill", name="Org Skill", status="approved")
    _set_publisher_kind(eid, "organization")

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row(text, "Org Skill")
    assert row, "approved org skill must appear in library"
    assert "ds-trust--org" in row
    assert 'data-tip="Published by your organization."' in row
    assert 'aria-label="Published by your organization."' in row
    assert "data-tip-instant" in row
    # Icon only, like the other two — the retired grey TEXT chip must not return.
    assert "lib-trust--org" not in row
    assert ">Organization<" not in row
    # It is NOT downgraded to the community marker just because it is unverified:
    # publisher_kind is checked first, as it always was.
    assert "ds-trust--community" not in row


def test_org_marker_is_not_gated_by_the_unverified_flag(seeded_app, monkeypatch):
    """Organization, like Verified, is a positive claim and ungated — the flag only
    ever gated the community branch."""
    monkeypatch.setenv("AGNES_LIBRARY_SHOW_UNVERIFIED_TRUST", "false")
    eid = _entity(owner="admin", owner_name="admin", etype="skill", name="Org Skill Flag Off", status="approved")
    _set_publisher_kind(eid, "organization")

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row(text, "Org Skill Flag Off")
    assert row, "approved org skill must appear in library"
    assert "ds-trust--org" in row


class TestLibraryDraftsBand:
    """Unfinished builder drafts are surfaced on /library.

    A draft lives in localStorage and never reaches the server, so the band is
    filled client-side and the server can only be asked for the scaffolding.
    What these guard is the part that must NOT drift: the band is separate from
    the inventory, it starts hidden, and every promise-limiting label the
    design depends on is actually present in the shipped markup.
    """

    def test_drafts_band_ships_hidden_and_outside_the_inventory(self, seeded_app):
        text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
        # Present, and hidden until the client finds drafts — an empty section
        # would be noise on a page that already carries five kinds.
        assert 'id="lib-drafts"' in text
        assert 'id="lib-drafts-rows"' in text
        assert '<div class="lib-drafts" id="lib-drafts" hidden>' in text
        # NOT a `.lib-group`: drafts must never join the united inventory list,
        # whose rows are account-scoped and survive a device switch. Anchor the
        # ordering on the item-count row, which renders whether or not this
        # fixture's user actually has any sections.
        assert 'class="lib-drafts"' in text
        assert "lib-group" not in text[text.index('id="lib-drafts"') : text.index('id="lib-item-count"')]
        assert text.index('id="lib-drafts"') < text.index('id="lib-item-count"'), (
            "drafts band must sit above the inventory, not inside it"
        )

    def test_drafts_band_states_the_durability_limit(self, seeded_app):
        """Placement in the Library implies account-scoped durability that
        localStorage cannot keep, so the limit is stated on the band AND on
        every row — a row can be read on its own."""
        text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
        assert "Saved in this browser only — not yet in your Library" in text
        assert "this browser only" in text
        # A plugin draft never keeps its .zip (a File cannot be serialized),
        # which is a second, type-specific limit.
        assert "bundle not saved — re-attach to publish" in text

    def test_drafts_resume_links_target_the_builder_by_type(self, seeded_app):
        text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
        assert "/skills?type=' + k" in text
        assert "agnes_builder_draft_v1_" in text
        # The pre-redesign single-slot key is still read, so a draft written by
        # the skill-only builder is not orphaned.
        assert "agnes_skill_draft_v1_" in text

    def test_add_menu_carries_no_draft_count(self, seeded_app):
        """The "+ Add" button must NOT badge a draft count.

        It exists only on /library, which already shows the band — so a badge
        could only ever duplicate what is on screen. Worse, it would count
        something its own menu does not contain: the menu offers Build a
        skill / plugin / agent and Upload a file, never a draft.
        """
        text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
        assert "lib-new-draftcount" not in text
        assert "lib-new__count" not in text


# ---------------------------------------------------------------------------
# The sharing label a row publishes to BOTH views
# ---------------------------------------------------------------------------


def test_row_publishes_its_real_sharing_label_for_the_card_to_print(seeded_app):
    """`data-sharing` carries the word the reader sees, and the grid card prints
    it verbatim — which is what stops the two views from disagreeing.

    The regression this guards is not cosmetic. The card used to derive its own
    label from `data-visibility`, a map of the three SCOPE keys only, so a skill
    still in review — `visibility='private'`, correctly labelled "In review" on
    its row — printed "Only you" on its card. Not a wording difference: a false
    statement about the item, and the loss of the one state whose whole value is
    being visible at a glance.

    Also pins "Everyone" over the old "Workspace" for the approved case: the key
    stays `workspace` (nothing downstream re-keys), only the word a user reads
    changes. See app/services/artefact_access.py :: VISIBILITY_LABELS.
    """
    _entity(owner="analyst1", owner_name="analyst1", etype="skill", name="Review Me", status="pending")
    _entity(owner="admin", owner_name="admin", etype="skill", name="Public Skill", status="approved")
    _entity(owner="analyst1", owner_name="analyst1", etype="skill", name="Kept Back", status="hidden")

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text

    pending = _row(text, "Review Me")
    assert pending, "the caller's own pending skill must be in their Library"
    assert 'data-sharing="In review"' in pending
    assert 'data-visibility="private"' in pending  # the KEY is unchanged
    assert "In review" in pending  # …and the badge says so too

    approved = _row(text, "Public Skill")
    assert approved
    assert 'data-sharing="Everyone"' in approved
    assert 'data-visibility="workspace"' in approved
    assert 'data-sharing="Workspace"' not in approved

    private = _row(text, "Kept Back")
    assert private
    assert 'data-sharing="Private"' in private


# ---------------------------------------------------------------------------
# A curated plugin is organization-published — on EVERY surface
# ---------------------------------------------------------------------------


def _granted_curated_plugin(*, marketplace_id: str, name: str, user_id: str) -> None:
    """Seed a curated plugin off an admin-registered marketplace, granted to a
    group ``user_id`` belongs to — the shape the Library lists it in."""
    from src.db import get_system_db
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    conn = get_system_db()
    conn.execute(
        "INSERT INTO marketplace_registry (id, name, url) VALUES (?, 'Curated Co', 'https://example.com/r.git')",
        [marketplace_id],
    )
    conn.execute(
        "INSERT INTO marketplace_plugins (marketplace_id, name, description, is_system) "
        "VALUES (?, ?, 'A curated plugin', FALSE)",
        [marketplace_id, name],
    )
    groups = UserGroupsRepository(conn)
    grp = groups.get_by_name(f"grp-{name}") or groups.create(name=f"grp-{name}", description="t", created_by="t")
    UserGroupMembersRepository(conn).add_member(user_id, grp["id"], source="admin", added_by="t")
    ResourceGrantsRepository(conn).create(
        group_id=grp["id"],
        resource_type="marketplace_plugin",
        resource_id=f"{marketplace_id}/{name}",
        assigned_by="admin",
    )
    conn.close()


def test_curated_plugin_row_is_marked_organization_published(seeded_app):
    """A curated plugin comes off a marketplace an ADMIN registered, so the
    organization stands behind it — and `/api/marketplace/items` says exactly
    that (``publisher_kind="organization"``, publisher "Your organization").

    The Library used to disagree with itself here: it called the same plugin
    "Your workspace" and set none of the three trust fields the row's marker
    reads, so the one class of item that genuinely IS organization-published was
    the one class wearing no Organization marker. Worse, the absence was not
    neutral — on an instance with ``library.show_unverified_trust`` on, every
    OTHER plugin and skill wore a marker, so the org-published one read as the
    least-vouched-for thing on the page.
    """
    _granted_curated_plugin(marketplace_id="org-curated", name="curated-kit", user_id="analyst1")

    text = seeded_app["client"].get("/library", headers=_auth(seeded_app["analyst_token"])).text
    row = _row(text, "curated-kit")
    assert row, "granted curated plugin missing from the Library"
    assert "ds-trust--org" in row
    assert "Published by your organization." in row
    # The Owner column names the ORGANIZATION, the same words /marketplace uses
    # (app.api.store.ORGANIZATION_PUBLISHER_LABEL) — not the generic workspace.
    assert "Your organization" in row
    assert "Your workspace" not in row
    # Organization outranks verification, and a curated plugin has no per-item
    # verification state to promote it with. Never both markers on one row.
    assert "ds-trust--verified" not in row
    assert "ds-trust--community" not in row
