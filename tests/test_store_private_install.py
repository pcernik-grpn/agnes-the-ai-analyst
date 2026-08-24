"""The Private store tier is usable by its own author — and only by them.

``POST /api/store/entities/{id}/install`` used to refuse anything not
``approved``, so an entity kept Private (``access=private`` on upload, or the
builder's Private choice) could be authored, listed in its owner's Library, and
offered an "Add to Stack" button that always 409'd. The author may now install
their own ``hidden`` entity.

The exemption is narrow, and these tests are the fence around it. ``hidden`` is
written by TWO paths — the Private access choice and guardrail quarantine — so
the separator is the submission verdict, not the visibility status. Fixtures and
seed helpers are reused from ``test_admin_store_submissions`` rather than
recopied; that module owns the quarantine seed contract.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.db import close_system_db
from tests.test_admin_store_submissions import _create_user, _seed_quarantined_entity


@pytest.fixture
def web_client(tmp_path, monkeypatch, shared_app):
    """Same shape as the one in ``test_admin_store_submissions`` — declared here
    rather than imported, because importing a fixture by name trips ruff's F811
    on every test that takes it as a parameter."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    (tmp_path / "state").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "extracts").mkdir()
    close_system_db()

    app = shared_app
    yield TestClient(app)
    close_system_db()


def test_own_private_entity_is_installable_by_its_author(web_client: TestClient):
    """The default path of the chat upload dialog: "Save to my Library" +
    "Add to my stack" with sharing off."""
    owner_id, owner_cookies = _create_user(web_client, "owner@x.com")
    # status='approved' → a hidden entity with NO blocking verdict, i.e. private
    # by choice rather than quarantined.
    entity_id, _sub_id = _seed_quarantined_entity(
        owner_id,
        "owner@x.com",
        "p1",
        status="approved",
    )

    r = web_client.post(
        f"/api/store/entities/{entity_id}/install",
        cookies=owner_cookies,
    )
    assert r.status_code == 200, r.text
    assert r.json()["installed"] is True


def test_others_private_entity_still_refused(web_client: TestClient):
    """Owner-scoped: a third party holding the entity_id of someone else's
    private item gets the same 409 as before."""
    owner_id, _owner_cookies = _create_user(web_client, "owner@x.com")
    entity_id, _sub_id = _seed_quarantined_entity(
        owner_id,
        "owner@x.com",
        "p2",
        status="approved",
    )

    _, other_cookies = _create_user(web_client, "other@x.com")
    r = web_client.post(
        f"/api/store/entities/{entity_id}/install",
        cookies=other_cookies,
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "entity_not_approved"


def test_own_quarantined_entity_still_refused(web_client: TestClient):
    """The guard that the exemption did not widen into "owner may install
    anything hidden". A bundle guardrail review REJECTED stays out of its own
    author's Stack — the case
    ``test_admin_store_submissions::test_install_quarantined_refused_for_non_admin``
    also pins from the admin side.
    """
    owner_id, owner_cookies = _create_user(web_client, "owner@x.com")
    # Default status='blocked_llm' → hidden AND rejected.
    entity_id, _sub_id = _seed_quarantined_entity(owner_id, "owner@x.com", "p3")

    r = web_client.post(
        f"/api/store/entities/{entity_id}/install",
        cookies=owner_cookies,
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "entity_not_approved"


# ---------------------------------------------------------------------------
# Delete (#1177) — the same `hidden` disambiguation, on the other gate.
#
# `delete_entity` refused every non-approved/non-archived status for the owner.
# For a Private row that is permanent, not transient: `access='private'` writes
# `hidden` and nothing ever promotes it. The author could therefore never
# delete a plugin nobody else could even see.
# ---------------------------------------------------------------------------


def test_own_private_entity_is_archivable_by_its_author(web_client: TestClient):
    owner_id, owner_cookies = _create_user(web_client, "owner@x.com")
    entity_id, _sub_id = _seed_quarantined_entity(
        owner_id,
        "owner@x.com",
        "d1",
        status="approved",
    )

    r = web_client.delete(
        f"/api/store/entities/{entity_id}",
        cookies=owner_cookies,
    )
    assert r.status_code == 204, r.text

    from src.repositories import store_entities_repo

    assert store_entities_repo().get(entity_id)["visibility_status"] == "archived"


def test_own_quarantined_entity_still_undeletable(web_client: TestClient):
    """The evidence-preservation the gate exists for: a bundle guardrail review
    REJECTED still cannot be erased by its submitter ahead of admin triage."""
    owner_id, owner_cookies = _create_user(web_client, "owner@x.com")
    entity_id, _sub_id = _seed_quarantined_entity(owner_id, "owner@x.com", "d2")

    r = web_client.delete(
        f"/api/store/entities/{entity_id}",
        cookies=owner_cookies,
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "quarantined_owner_cannot_delete"


def test_others_private_entity_still_undeletable(web_client: TestClient):
    """Ownership is checked before the exemption — a third party is refused as
    `not_owner`, never reaching the `hidden` branch at all."""
    owner_id, _owner_cookies = _create_user(web_client, "owner@x.com")
    entity_id, _sub_id = _seed_quarantined_entity(
        owner_id,
        "owner@x.com",
        "d3",
        status="approved",
    )

    _, other_cookies = _create_user(web_client, "other@x.com")
    r = web_client.delete(
        f"/api/store/entities/{entity_id}",
        cookies=other_cookies,
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "not_owner"


def test_hard_delete_stays_admin_only_for_own_private(web_client: TestClient):
    """The exemption unlocks the soft archive, not the admin-only purge."""
    owner_id, owner_cookies = _create_user(web_client, "owner@x.com")
    entity_id, _sub_id = _seed_quarantined_entity(
        owner_id,
        "owner@x.com",
        "d4",
        status="approved",
    )

    r = web_client.delete(
        f"/api/store/entities/{entity_id}?hard=true",
        cookies=owner_cookies,
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "hard_delete_admin_only"


# ---------------------------------------------------------------------------
# Detail feed (#1178) — the install button reads `installable`, which the
# server resolves with the same predicate as the install gate. Deriving it in
# JS from `visibility_status` alone is what locked the button permanently.
# ---------------------------------------------------------------------------


def test_detail_marks_own_private_entity_installable(web_client: TestClient):
    owner_id, owner_cookies = _create_user(web_client, "owner@x.com")
    entity_id, _sub_id = _seed_quarantined_entity(
        owner_id,
        "owner@x.com",
        "i1",
        status="approved",
    )

    r = web_client.get(
        f"/api/marketplace/flea/{entity_id}/detail",
        cookies=owner_cookies,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["visibility_status"] == "hidden"
    assert body["installable"] is True


def test_detail_marks_own_quarantined_entity_not_installable(web_client: TestClient):
    owner_id, owner_cookies = _create_user(web_client, "owner@x.com")
    entity_id, _sub_id = _seed_quarantined_entity(owner_id, "owner@x.com", "i2")

    r = web_client.get(
        f"/api/marketplace/flea/{entity_id}/detail",
        cookies=owner_cookies,
    )
    assert r.status_code == 200, r.text
    assert r.json()["installable"] is False


def test_detail_page_offers_archive_on_an_own_private_entity(web_client: TestClient):
    """The rendered page, not just the API.

    The API half of #1177 is worth nothing if the owner-actions strip still
    renders "Delete (locked — quarantined)" — a disabled button never fires a
    click handler, so the endpoint it would have called is unreachable.

    This is the skill/agent template (`marketplace_item_detail.html`); the
    plugin one carried its own copy of the same gate and is covered by
    ``test_plugin_detail_page_offers_archive_on_an_own_private_entity``.
    """
    owner_id, owner_cookies = _create_user(web_client, "owner@x.com")
    entity_id, _sub_id = _seed_quarantined_entity(
        owner_id,
        "owner@x.com",
        "page1",
        status="approved",
    )

    r = web_client.get(f"/marketplace/flea/{entity_id}", cookies=owner_cookies)
    assert r.status_code == 200, r.text
    assert 'id="owner-archive-btn"' in r.text, "the owner has no Archive control on their own Private entity"
    assert "Delete (locked — quarantined)" not in r.text
    # The button the id sits on must not also be inert.
    strip = r.text[r.text.index('id="owner-archive-btn"') - 400 : r.text.index('id="owner-archive-btn"') + 200]
    assert "disabled" not in strip, f"Archive rendered disabled:\n{strip}"


def test_redesigned_detail_page_offers_archive_on_an_own_private_entity(web_client: TestClient, monkeypatch):
    """The same unlock, on the look a paper-theme instance actually serves.

    Under the #896 redesign the owner-actions ladder is not the per-template
    button strip the two tests around this one assert — it is the SHARED
    ``detail.store_menu()`` macro, which carried its own copy of the gate and
    kept it pinned to ``visibility_status == 'approved'``. So #1177 was fixed on
    the legacy look only: on a paper instance the author of a Private entity saw
    a permanently greyed-out Delete while the API would have accepted the
    archive (Devin Review on #1196). Asserted on the redesigned render
    specifically, because the legacy assertions pass either way.
    """
    monkeypatch.setenv("AGNES_INSTANCE_THEME", "paper")
    owner_id, owner_cookies = _create_user(web_client, "owner@x.com")
    entity_id, _sub_id = _seed_quarantined_entity(
        owner_id,
        "owner@x.com",
        "paper1",
        status="approved",
    )

    r = web_client.get(f"/marketplace/flea/{entity_id}", cookies=owner_cookies)
    assert r.status_code == 200, r.text
    assert 'id="owner-archive-btn"' in r.text, (
        "the redesigned page gives the owner no Archive control on their own Private entity"
    )
    assert "Submission is quarantined" not in r.text

    # …and the API the menu item calls actually accepts it, so menu and
    # endpoint cannot drift apart again.
    assert web_client.delete(f"/api/store/entities/{entity_id}", cookies=owner_cookies).status_code == 204


def test_plugin_detail_page_offers_archive_on_an_own_private_entity(web_client: TestClient):
    """The plugin template, reached through the real Private upload path.

    `marketplace_plugin_detail.html` is a different file with its own copy of
    the owner-actions gate, and the seed helper only makes skills — so without
    this the plugin half of #1177 would be asserted nowhere. Uploading with
    `access=private` also exercises the actual route into `hidden` (the
    builder's Private choice) rather than a hand-seeded row.
    """
    import io
    import json
    import zipfile

    _, cookies = _create_user(web_client, "owner@x.com")
    desc = "Description long enough to clear the content checks comfortably."
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            ".claude-plugin/plugin.json",
            json.dumps({"name": "priv-plugin", "description": desc, "version": "0.1"}),
        )
        zf.writestr("skills/dummy/SKILL.md", f"---\nname: dummy\ndescription: {desc}\n---\n\n" + "Body text. " * 40)

    created = web_client.post(
        "/api/store/entities",
        files={"file": ("p.zip", buf.getvalue(), "application/zip")},
        data={"type": "plugin", "name": "priv-plugin", "description": desc, "access": "private"},
        cookies=cookies,
    )
    assert created.status_code == 201, created.text
    entity_id = created.json()["id"]

    from src.repositories import store_entities_repo

    assert store_entities_repo().get(entity_id)["visibility_status"] == "hidden", (
        "the Private choice no longer writes `hidden` — this test's premise is gone"
    )

    r = web_client.get(f"/marketplace/flea/{entity_id}", cookies=cookies)
    assert r.status_code == 200, r.text
    assert 'id="owner-archive-btn"' in r.text, "the owner has no Archive control on their own Private plugin"
    assert "Delete (locked — quarantined)" not in r.text

    # …and the API the button calls actually accepts it.
    assert web_client.delete(f"/api/store/entities/{entity_id}", cookies=cookies).status_code == 204


def test_no_quarantine_banner_on_an_own_private_entity(web_client: TestClient):
    """The banner was the fourth surface reading `hidden` as quarantine.

    For the author's own Private row it fell to the "Hidden" fallback, whose
    copy says "nobody can install it" — false since #1178, and rendered directly
    above the now-enabled "+ Add to my stack" (Devin Review on #1196). A
    genuinely quarantined entity must keep its banner, which the test below
    covers.
    """
    owner_id, owner_cookies = _create_user(web_client, "owner@x.com")
    entity_id, _sub_id = _seed_quarantined_entity(
        owner_id,
        "owner@x.com",
        "nobanner1",
        status="approved",
    )

    r = web_client.get(f"/marketplace/flea/{entity_id}", cookies=owner_cookies)
    assert r.status_code == 200, r.text
    assert "vis-banner" not in r.text, "the quarantine banner still greets the author of a Private entity"
    assert "nobody can install it" not in r.text


def test_failed_archive_rollback_keeps_a_private_entity_private(web_client: TestClient, monkeypatch):
    """A disk error during archive must not publish the thing being archived.

    The soft-archive path renames the baked tree after flipping the row, and
    reverts the row if that rename raises. The revert used to hardcode
    ``'approved'`` — accurate while only an approved row could reach it, but
    #1177 lets the author archive their own Private (``hidden``) row, and
    reverting THAT to ``approved`` publishes a private entity to the whole
    organization off an error the author only sees as a 500 (Devin Review on
    #1196). The revert now restores the row's actual pre-archive status.
    """
    from src.repositories import store_entities_repo

    owner_id, owner_cookies = _create_user(web_client, "owner@x.com")
    entity_id, _sub_id = _seed_quarantined_entity(
        owner_id,
        "owner@x.com",
        "rollback1",
        status="approved",
    )
    assert store_entities_repo().get(entity_id)["visibility_status"] == "hidden", (
        "premise: the own-Private row sits at 'hidden'"
    )

    from app.api import store as store_api

    def _boom(**kwargs):
        raise OSError("disk went away mid-rename")

    monkeypatch.setattr(store_api, "_rename_baked_tree", _boom)

    r = web_client.delete(f"/api/store/entities/{entity_id}", cookies=owner_cookies)
    assert r.status_code == 500, r.text

    after = store_entities_repo().get(entity_id)["visibility_status"]
    assert after == "hidden", f"a failed archive published the private entity as {after!r}"


def test_detail_page_still_locks_a_genuinely_quarantined_entity(web_client: TestClient):
    """The other half of the same gate — the page must keep refusing where the
    API refuses, or the owner gets a live button that 403s."""
    owner_id, owner_cookies = _create_user(web_client, "owner@x.com")
    entity_id, _sub_id = _seed_quarantined_entity(owner_id, "owner@x.com", "page2")

    r = web_client.get(f"/marketplace/flea/{entity_id}", cookies=owner_cookies)
    assert r.status_code == 200, r.text
    assert 'id="owner-archive-btn"' not in r.text
    # Blue spells the lock in the label; paper (the default) disables the
    # store-menu row and states the reason in its title.
    assert (
        "Delete (locked — quarantined)" in r.text
        or "Submission is quarantined. Only an admin can delete it." in r.text
    )


def test_detail_installable_agrees_with_the_install_endpoint(web_client: TestClient):
    """The whole point of resolving it server-side: button and endpoint cannot
    disagree. Asserted as a pair so a future edit to either one has to move
    both."""
    owner_id, owner_cookies = _create_user(web_client, "owner@x.com")
    for name, status in (("i3", "approved"), ("i4", "blocked_llm")):
        entity_id, _sub_id = _seed_quarantined_entity(owner_id, "owner@x.com", name, status=status)
        detail = web_client.get(
            f"/api/marketplace/flea/{entity_id}/detail",
            cookies=owner_cookies,
        ).json()
        install = web_client.post(
            f"/api/store/entities/{entity_id}/install",
            cookies=owner_cookies,
        )
        assert detail["installable"] is (install.status_code == 200), (
            f"{name}: detail says installable={detail['installable']} but install returned {install.status_code}"
        )
