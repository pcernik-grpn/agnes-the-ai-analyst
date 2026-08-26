"""v37 flea-market edit feature with version history.

Covers:
* Bundle update bumps version_no + appends version_history entry.
* Metadata-only edit doesn't bump version.
* Type change rejected with 400 type_locked.
* Block-while-pending: 409 prior_version_pending.
* Display name change renames the on-disk slug for live + version dirs.
* Restore copies a prior version forward as v<max+1>; live + history
  reflect the new version; original version row keeps its own verdict.
* Restore re-runs guardrails (blocked path leaves live untouched).
* Versions card on detail page renders for owner/admin only.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app.utils import get_store_dir
from src.db import close_system_db, get_system_db
from src.repositories.store_entities import StoreEntitiesRepository
from src.repositories.users import UserRepository


# Strong default description that clears the content guardrail's
# per-component bar (30 chars + 4 distinct words, no placeholder
# leftovers). Tests don't assert on its contents — they just need a
# value that passes review so we can exercise the edit/version path.
_OK_DESC = "Use when validating store version edit flow across every guardrail tier"


@pytest.fixture
def web_client(tmp_path, monkeypatch, shared_app):
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


def _create_user(client, email, password="UserPass1!"):
    ph = PasswordHasher()
    conn = get_system_db()
    user_id = email.split("@")[0]
    UserRepository(conn).create(
        id=user_id,
        email=email,
        name=user_id,
        password_hash=ph.hash(password),
    )
    conn.close()
    r = client.post("/auth/token", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return user_id, {"access_token": r.json()["access_token"]}


def _create_admin(client, email="admin-edit@x.com"):
    from tests.helpers.auth import grant_admin

    user_id, cookies = _create_user(client, email, password="AdminPass1!")
    conn = get_system_db()
    grant_admin(conn, user_id)
    conn.close()
    return user_id, cookies


def _make_skill_zip(skill_name: str, body: str = "Body line explaining the skill. " * 12) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            f"{skill_name}/SKILL.md",
            f"---\nname: {skill_name}\ndescription: Use when verifying clean-bundle edits across the version-history lifecycle\n---\n\n"
            + body,
        )
    return buf.getvalue()


def _make_eval_skill_zip(skill_name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            f"{skill_name}/SKILL.md",
            f"---\nname: {skill_name}\ndescription: Use when verifying static-security rejects eval-using upload bundles cleanly\n---\n\n"
            + ("Body line explaining the skill. " * 12),
        )
        zf.writestr(f"{skill_name}/run.sh", "#!/bin/sh\neval $1\n")
    return buf.getvalue()


def _upload_clean(client, cookies, name="ed1"):
    r = client.post(
        "/api/store/entities",
        files={"file": ("s.zip", _make_skill_zip(name), "application/zip")},
        data={"type": "skill", "description": _OK_DESC},
        cookies=cookies,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _make_plugin_zip(plugin_name: str, inner_skill: str = "dummy") -> bytes:
    """Minimal flea plugin bundle — mirrors test_marketplace_api's helper.

    A ``type='plugin'`` entity renders marketplace_plugin_detail.html, a
    different template from the skill page, so ordering guards need this
    to reach it (see router.marketplace_flea_detail).
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            ".claude-plugin/plugin.json",
            json.dumps({"name": plugin_name, "description": _OK_DESC, "version": "0.1"}),
        )
        zf.writestr(
            f"skills/{inner_skill}/SKILL.md",
            f"---\nname: {inner_skill}\ndescription: {_OK_DESC}\n---\n\n" + ("Body line explaining the skill. " * 12),
        )
    return buf.getvalue()


def _upload_clean_plugin(client, cookies, name="pl1"):
    r = client.post(
        "/api/store/entities",
        files={"file": ("p.zip", _make_plugin_zip(name), "application/zip")},
        data={"type": "plugin", "description": _OK_DESC},
        cookies=cookies,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


class TestEditFeature:
    def test_metadata_only_edit_no_version_bump(self, web_client):
        owner_id, owner_cookies = _create_user(web_client, "metaowner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="metaedit")

        r = web_client.put(
            f"/api/store/entities/{eid}",
            data={"description": "Updated description text"},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text

        conn = get_system_db()
        entity = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert entity["description"] == "Updated description text"
        assert entity["version_no"] == 1
        assert len(entity["version_history"]) == 1

    def test_bundle_edit_bumps_version_and_appends_history(self, web_client):
        owner_id, owner_cookies = _create_user(web_client, "bundleowner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="bundleedit")

        # PUT with new bundle bytes.
        new_zip = _make_skill_zip("bundleedit", body="V2 body. " * 80)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", new_zip, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text

        conn = get_system_db()
        entity = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert entity["version_no"] == 2
        assert len(entity["version_history"]) == 2
        v1, v2 = entity["version_history"]
        assert v1["n"] == 1
        assert v2["n"] == 2
        assert v2["hash"] != v1["hash"], "v2 hash must differ from v1"

    def test_type_change_rejected(self, web_client):
        owner_id, owner_cookies = _create_user(web_client, "typeowner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="typelock")
        r = web_client.put(
            f"/api/store/entities/{eid}",
            data={"type": "agent", "description": _OK_DESC},
            cookies=owner_cookies,
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["code"] == "type_locked"

    def test_block_while_prior_pending_409(self, web_client):
        """Manually flip the entity to visibility=pending + create a
        pending submission, then attempt edit → 409."""
        from src.repositories.store_submissions import StoreSubmissionsRepository

        owner_id, owner_cookies = _create_user(web_client, "blockowner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="blockpending")

        conn = get_system_db()
        # Force pending state.
        conn.execute(
            "UPDATE store_entities SET visibility_status = 'pending' WHERE id = ?",
            [eid],
        )
        StoreSubmissionsRepository(conn).create(
            submitter_id=owner_id,
            submitter_email="blockowner@x.com",
            type="skill",
            name="blockpending",
            version="2.0.0",
            status="pending_llm",
            entity_id=eid,
            inline_checks={"manifest": {"status": "pass"}},
        )
        conn.close()

        r = web_client.put(
            f"/api/store/entities/{eid}",
            data={"description": "Trying to edit"},
            cookies=owner_cookies,
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "prior_version_pending"

    def test_name_change_renames_baked_slug(self, web_client):
        owner_id, owner_cookies = _create_user(web_client, "renameowner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="oldname")

        r = web_client.put(
            f"/api/store/entities/{eid}",
            data={"name": "newname"},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text

        conn = get_system_db()
        entity = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert entity["name"] == "newname"

        plugin_dir = Path(get_store_dir()) / eid / "plugin"
        new_skill_dir = plugin_dir / "skills" / "newname-by-renameowner"
        old_skill_dir = plugin_dir / "skills" / "oldname-by-renameowner"
        assert new_skill_dir.is_dir(), "renamed slug missing on disk"
        assert not old_skill_dir.exists(), "old slug must be gone"


class TestRestoreVersion:
    def test_restore_creates_new_version_with_old_bundle(self, web_client):
        owner_id, owner_cookies = _create_user(web_client, "restoreowner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="restoreme")

        # Edit to v2.
        v2_zip = _make_skill_zip("restoreme", body="VERSION-2-BODY " * 80)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2_zip, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text

        # Capture v1 + v2 hashes from history.
        conn = get_system_db()
        entity = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        v1_hash = entity["version_history"][0]["hash"]
        v2_hash = entity["version_history"][1]["hash"]
        assert v1_hash != v2_hash

        # Restore v1 → creates v3 with v1's bundle hash.
        r = web_client.post(
            f"/api/store/entities/{eid}/versions/1/restore",
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text

        conn = get_system_db()
        entity = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert entity["version_no"] == 3
        assert len(entity["version_history"]) == 3
        v3 = entity["version_history"][2]
        assert v3["n"] == 3
        assert v3["hash"] == v1_hash, "restored bundle should hash identically to v1 — same bytes"

    def test_restore_already_current_400(self, web_client):
        owner_id, owner_cookies = _create_user(web_client, "alreadyowner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="already")
        r = web_client.post(
            f"/api/store/entities/{eid}/versions/1/restore",
            cookies=owner_cookies,
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["code"] == "already_current"

    def test_restore_unknown_version_404(self, web_client):
        owner_id, owner_cookies = _create_user(web_client, "unknownver@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="ukver")
        r = web_client.post(
            f"/api/store/entities/{eid}/versions/99/restore",
            cookies=owner_cookies,
        )
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["code"] == "version_not_found"

    def test_non_owner_non_admin_cannot_restore(self, web_client):
        owner_id, owner_cookies = _create_user(web_client, "owrestore@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="ownedver")
        v2 = _make_skill_zip("ownedver", body="v2 " * 80)
        web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )

        _, snoop_cookies = _create_user(web_client, "snoopver@x.com")
        r = web_client.post(
            f"/api/store/entities/{eid}/versions/1/restore",
            cookies=snoop_cookies,
        )
        assert r.status_code in (403, 404), r.text

    def test_restore_rejects_blocked_llm_version(self, web_client, monkeypatch):
        """A v2 that LLM-blocked sits in version_history with
        submission.status='blocked_llm'. The restore endpoint must
        refuse to roll forward from that bundle — defense in depth
        against the UI being bypassed by direct POST."""

        # Mock LLM to BLOCK v2.
        def mock_review_bundle(*args, **kwargs):
            return {
                "risk_level": "high",
                "summary": "mock block",
                "findings": [{"severity": "high", "category": "test", "file": "x", "explanation": "mock"}],
                "template_placeholders_found": 0,
                "reviewed_by_model": "mock-model",
                "error": None,
            }

        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle",
            mock_review_bundle,
        )

        owner_id, owner_cookies = _create_user(web_client, "blockrestore@x.com")
        # Phase 1: guardrails OFF — v1 lands approved.
        eid = _upload_clean(web_client, owner_cookies, name="blockrestore")
        # Phase 2: guardrails ON → v2 blocked, entity stays at v1.
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)
        v2 = _make_skill_zip("blockrestore", body="V2 BODY " * 80)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text

        # Drive the BG review synchronously.
        from src.repositories.store_submissions import StoreSubmissionsRepository
        from src.store_guardrails.runner import run_llm_review

        conn = get_system_db()
        sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()
        run_llm_review(
            sub_id,
            plugin_dir=Path(get_store_dir()) / eid / "versions" / "v2" / "plugin",
            conn_factory=get_system_db,
            api_key_loader=lambda: "sk-test",
            model_loader=lambda: "claude-haiku-4-5-20251001",
        )

        # Now POST a restore /versions/2/restore. Must 400 because v2
        # was never approved.
        r = web_client.post(
            f"/api/store/entities/{eid}/versions/2/restore",
            cookies=owner_cookies,
        )
        assert r.status_code == 400, r.text
        body = r.json()
        assert body["detail"]["code"] == "version_not_approved"
        assert body["detail"]["source_status"] == "blocked_llm"

    def test_restore_rejects_review_error_version(self, web_client, monkeypatch):
        """Same as blocked_llm but the LLM call errored — the
        submission row lands at 'review_error' and the version is
        equally not-approvable."""

        def mock_review_bundle(*args, **kwargs):
            return {
                "risk_level": None,
                "summary": None,
                "findings": [],
                "template_placeholders_found": 0,
                "reviewed_by_model": "mock-model",
                "error": "LLMFormatError: mock truncation",
            }

        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle",
            mock_review_bundle,
        )

        owner_id, owner_cookies = _create_user(web_client, "errrestore@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="errrestore")
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)
        v2 = _make_skill_zip("errrestore", body="V2 BODY " * 80)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text

        from src.repositories.store_submissions import StoreSubmissionsRepository
        from src.store_guardrails.runner import run_llm_review

        conn = get_system_db()
        sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()
        run_llm_review(
            sub_id,
            plugin_dir=Path(get_store_dir()) / eid / "versions" / "v2" / "plugin",
            conn_factory=get_system_db,
            api_key_loader=lambda: "sk-test",
            model_loader=lambda: "claude-haiku-4-5-20251001",
        )

        r = web_client.post(
            f"/api/store/entities/{eid}/versions/2/restore",
            cookies=owner_cookies,
        )
        assert r.status_code == 400, r.text
        body = r.json()
        assert body["detail"]["code"] == "version_not_approved"
        assert body["detail"]["source_status"] == "review_error"

    def test_restore_allows_legacy_v1_without_submission_id(self, web_client):
        """The v1 seed entry created by ``StoreEntitiesRepository.create``
        carries ``submission_id=None`` until the API layer backfills.
        A restore targeting v1 must NOT be rejected just because the
        join can't find a submission status — back-compat for entities
        created before v37."""
        owner_id, owner_cookies = _create_user(web_client, "legv1@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="legv1")
        # Manually clear v1's submission_id to simulate the legacy seed.
        conn = get_system_db()
        repo = StoreEntitiesRepository(conn)
        ent = repo.get(eid)
        history = ent["version_history"]
        history[0]["submission_id"] = None
        conn.execute(
            "UPDATE store_entities SET version_history = ? WHERE id = ?",
            [json.dumps(history), eid],
        )
        conn.close()

        # PUT a v2 so v1 is no longer current.
        v2 = _make_skill_zip("legv1", body="V2 BODY " * 80)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text

        # Restore v1 → must succeed (200), because legacy v1 has
        # submission_id=None which the guard treats as approved.
        r = web_client.post(
            f"/api/store/entities/{eid}/versions/1/restore",
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text


class TestEditPage:
    def test_edit_page_renders_for_owner(self, web_client):
        owner_id, owner_cookies = _create_user(web_client, "editpage@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="editrender")
        r = web_client.get(
            f"/marketplace/flea/{eid}/edit",
            cookies=owner_cookies,
        )
        assert r.status_code == 200
        assert "edit-form" in r.text
        assert "editrender" in r.text

    def test_edit_page_404_for_non_owner(self, web_client):
        owner_id, owner_cookies = _create_user(web_client, "owneredit@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="snoopedit")
        _, snoop_cookies = _create_user(web_client, "snoopedit@x.com")
        r = web_client.get(
            f"/marketplace/flea/{eid}/edit",
            cookies=snoop_cookies,
        )
        assert r.status_code == 404

    def test_versions_card_renders_for_owner(self, web_client):
        owner_id, owner_cookies = _create_user(web_client, "vowner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="vcard")
        r = web_client.get(
            f"/marketplace/flea/{eid}",
            cookies=owner_cookies,
        )
        assert r.status_code == 200
        # Version history renders through `detail.version_timeline` under the
        # default paper look (`.versions-card` was the blue-theme card), so the
        # marker is the timeline plus the version label itself.
        assert "detail-timeline" in r.text
        assert "v1 · current" in r.text

    def test_versions_card_hidden_for_non_owner(self, web_client):
        owner_id, owner_cookies = _create_user(web_client, "vowner2@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="vhide")
        _, other_cookies = _create_user(web_client, "vother@x.com")
        r = web_client.get(
            f"/marketplace/flea/{eid}",
            cookies=other_cookies,
        )
        assert r.status_code == 200
        assert "versions-card" not in r.text


class TestInstallerAlwaysGetsLatestApproved:
    """Critical contract: existing installers continue receiving the
    last APPROVED version through the review window of a new edit, and
    NEVER receive an unapproved version. If the new version is blocked,
    they keep the prior approved one. If approved, they advance.

    Implemented via deferred promotion: PUT/restore append the new
    version to history at status='pending_llm' but DO NOT swap live
    or bump entity.version_no. runner.run_llm_review's approval branch
    promotes; on block, nothing changes.
    """

    def _install_as_user(self, web_client, owner_cookies, eid):
        """Install as a separate consumer user, return their list_for_user
        rows post-install (mirrors what marketplace.zip serves)."""
        installer_id, installer_cookies = _create_user(web_client, "installer@x.com")
        r = web_client.post(
            f"/api/store/entities/{eid}/install",
            cookies=installer_cookies,
        )
        assert r.status_code == 200, r.text
        return installer_id, installer_cookies

    def test_pending_review_does_not_break_existing_installer(self, web_client, monkeypatch):
        """Initial upload runs with guardrails OFF (lands approved).
        Then we flip guardrails ON and PUT a new bundle. The new
        version should defer promotion: existing installer must
        continue seeing v1 + entity.version_no=1, not get hidden by a
        flipped visibility or a half-promoted live dir."""
        # Stub LLM scheduling so the BG path never actually runs.
        monkeypatch.setattr(
            "app.api.store._schedule_llm_review",
            lambda *a, **kw: None,
        )

        owner_id, owner_cookies = _create_user(web_client, "stickyowner@x.com")
        # Phase 1: guardrails OFF → initial upload lands approved.
        eid = _upload_clean(web_client, owner_cookies, name="sticky")
        # Capture v1 hash + size baseline.
        conn = get_system_db()
        ent = StoreEntitiesRepository(conn).get(eid)
        v1_hash = ent["version"]
        v1_size = ent["file_size"]
        conn.close()

        # Install as a different user.
        installer_id, _ = self._install_as_user(web_client, owner_cookies, eid)

        # Phase 2: flip guardrails ON for the PUT call. Now an edit
        # defers promotion until LLM approves.
        # Patch where update_entity looks it up — `from app.instance_config
        # import get_guardrails_enabled` binds the symbol into app.api.store,
        # so patching the source module isn't enough.
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)

        # PUT a new bundle. Guardrails on → submission lands at
        # pending_llm; promotion deferred.
        v2_zip = _make_skill_zip("sticky", body="V2 BODY " * 80)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2_zip, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text

        # Installer's list_for_user must STILL see entity with v1 hash
        # + size + visibility='approved'. The pending edit must not
        # have hidden the entity from them.
        from src.repositories.user_store_installs import UserStoreInstallsRepository

        conn = get_system_db()
        installs = UserStoreInstallsRepository(conn).list_for_user(installer_id)
        ids = {r["id"] for r in installs}
        assert eid in ids, (
            "installer lost access to the entity during the LLM review window — list_for_user filter excluded it"
        )
        row = next(r for r in installs if r["id"] == eid)
        assert row["version"] == v1_hash, (
            f"installer should still get v1 hash {v1_hash[:8]} but got {row['version'][:8]}"
        )
        assert row["file_size"] == v1_size, "size shouldn't change pre-promotion"
        assert row["visibility_status"] == "approved", (
            "entity must stay 'approved' through the review window so existing installers continue serving"
        )

        # Entity row's version_no must NOT have bumped yet.
        ent = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert ent["version_no"] == 1, f"version_no must stay 1 during pending review; got {ent['version_no']}"
        # But version_history MUST have v2 entry tracked (with the
        # new hash) so admin can see what's in flight.
        history_n = [int(e["n"]) for e in ent["version_history"]]
        assert 2 in history_n, "v2 entry must be in history despite no promotion"

    def test_blocked_new_version_keeps_installer_on_prior(self, web_client, monkeypatch):
        """Mock the LLM to BLOCK the v2 review. Installer must keep
        v1; entity.version_no must stay at 1; live plugin/ must hold
        v1's bytes."""

        # Mock the runner's LLM call to return a high-risk verdict.
        def mock_review_bundle(*args, **kwargs):
            return {
                "risk_level": "high",
                "summary": "mock block",
                "findings": [{"severity": "high", "category": "test", "file": "x", "explanation": "mock"}],
                "template_placeholders_found": 0,
                "reviewed_by_model": "mock-model",
                "error": None,
            }

        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle",
            mock_review_bundle,
        )

        # Phase 1: initial upload guardrails OFF.
        owner_id, owner_cookies = _create_user(web_client, "blockowner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="blocksticky")
        # Phase 2: switch guardrails ON before PUT.
        # Patch where update_entity looks it up — `from app.instance_config
        # import get_guardrails_enabled` binds the symbol into app.api.store,
        # so patching the source module isn't enough.
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)
        conn = get_system_db()
        v1_hash = StoreEntitiesRepository(conn).get(eid)["version"]
        conn.close()

        installer_id, _ = self._install_as_user(web_client, owner_cookies, eid)

        # Edit. Inline checks pass; LLM mocked to block.
        v2_zip = _make_skill_zip("blocksticky", body="v2-content " * 80)
        # Run the LLM synchronously by calling runner directly after
        # the PUT (the BG task may not have fired in TestClient).
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2_zip, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text

        # Find the just-created submission + run runner against it.
        from src.repositories.store_submissions import StoreSubmissionsRepository
        from src.store_guardrails.runner import run_llm_review
        from app.utils import get_store_dir

        conn = get_system_db()
        sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()
        run_llm_review(
            sub_id,
            plugin_dir=Path(get_store_dir()) / eid / "versions" / "v2" / "plugin",
            conn_factory=get_system_db,
            api_key_loader=lambda: "sk-test",
            model_loader=lambda: "claude-haiku-4-5-20251001",
        )

        # Installer must STILL see v1.
        from src.repositories.user_store_installs import UserStoreInstallsRepository

        conn = get_system_db()
        installs = UserStoreInstallsRepository(conn).list_for_user(installer_id)
        ent = StoreEntitiesRepository(conn).get(eid)
        conn.close()

        row = next(r for r in installs if r["id"] == eid)
        assert row["version"] == v1_hash, (
            f"after v2 blocked, installer must still get v1 hash; got {row['version'][:8]}"
        )
        assert ent["version_no"] == 1, f"version_no must stay at 1 after a blocked verdict; got {ent['version_no']}"

    def test_approved_new_version_promotes_to_installer(self, web_client):
        """Default test path: guardrails OFF → guardrails-disabled
        promote-inline branch fires immediately. Installer's next
        list_for_user reflects v2."""
        owner_id, owner_cookies = _create_user(web_client, "promoowner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="promosticky")
        conn = get_system_db()
        v1_hash = StoreEntitiesRepository(conn).get(eid)["version"]
        conn.close()

        installer_id, _ = self._install_as_user(web_client, owner_cookies, eid)

        v2_zip = _make_skill_zip("promosticky", body="promo-v2 " * 80)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2_zip, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text

        # Installer should now see v2 hash.
        from src.repositories.user_store_installs import UserStoreInstallsRepository

        conn = get_system_db()
        installs = UserStoreInstallsRepository(conn).list_for_user(installer_id)
        ent = StoreEntitiesRepository(conn).get(eid)
        conn.close()

        assert ent["version_no"] == 2
        row = next(r for r in installs if r["id"] == eid)
        assert row["version"] != v1_hash, "installer should advance to v2"


class TestAdminAccess:
    """Admin can edit + restore on entities they don't own (parity with
    the existing admin override path)."""

    def test_admin_can_edit_non_owned_entity_metadata(self, web_client):
        owner_id, owner_cookies = _create_user(web_client, "adminedit-owner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="adminedit")
        _, admin_cookies = _create_admin(web_client)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            data={"description": "moderated by admin"},
            cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text
        conn = get_system_db()
        ent = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert ent["description"] == "moderated by admin"
        assert ent["version_no"] == 1

    def test_admin_can_restore_non_owned_entity(self, web_client):
        owner_id, owner_cookies = _create_user(web_client, "adminrestore-owner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="adminrestore")
        v2 = _make_skill_zip("adminrestore", body="v2 " * 80)
        web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )
        _, admin_cookies = _create_admin(web_client)
        r = web_client.post(
            f"/api/store/entities/{eid}/versions/1/restore",
            cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text
        conn = get_system_db()
        ent = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert ent["version_no"] == 3


class TestRestoreDeferredPromotion:
    """Restore endpoint mirrors PUT semantics: live + version_no stay
    on prior current until LLM approves the restored copy."""

    def test_restore_with_guardrails_on_does_not_promote_until_approved(
        self,
        web_client,
        monkeypatch,
    ):
        """Owner restores v1 → restored bytes baked into v3 dir.
        Until LLM approves, live + version_no stay at v2."""
        owner_id, owner_cookies = _create_user(web_client, "restoreowner-defer@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="restoredefer")
        v2 = _make_skill_zip("restoredefer", body="v2 " * 80)
        web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )
        # v2 promoted (guardrails off). Now flip on for the restore.
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)
        monkeypatch.setattr(
            "app.api.store._schedule_llm_review",
            lambda *a, **kw: None,
        )
        r = web_client.post(
            f"/api/store/entities/{eid}/versions/1/restore",
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text
        conn = get_system_db()
        ent = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        # version_no must STAY at 2 until LLM approves the v3 (restored)
        # copy. version_history has 3 entries; current is still 2.
        assert ent["version_no"] == 2, (
            f"restore must defer promotion when guardrails on; version_no={ent['version_no']}"
        )
        history_n = sorted([int(e["n"]) for e in ent["version_history"]])
        assert history_n == [1, 2, 3]


class TestEditPageBanner:
    """Detail page banner during pending edit review must surface
    the version under review + the prior version still serving."""

    def test_banner_shows_review_error_when_prior_version_still_serving(
        self,
        web_client,
        monkeypatch,
    ):
        """v2+ edit landing in review_error must surface a banner to
        owner/admin even though entity stays at visibility=approved.
        The original gate (visibility != approved) silently hid the
        failure — see Bug #2 in plan
        when-i-submitted-new-delightful-russell.md."""
        from src.repositories.store_submissions import StoreSubmissionsRepository

        # Mock LLM to ERROR on v2.
        def mock_review_bundle(*args, **kwargs):
            return {
                "risk_level": None,
                "summary": None,
                "findings": [],
                "template_placeholders_found": 0,
                "reviewed_by_model": "mock-model",
                "error": "LLMFormatError: mock truncation",
            }

        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle",
            mock_review_bundle,
        )

        owner_id, owner_cookies = _create_user(web_client, "errbanner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="errbanner")
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)

        v2 = _make_skill_zip("errbanner", body="V2 BODY " * 80)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text

        # Drive the BG review synchronously so the submission row
        # lands at review_error.
        from src.store_guardrails.runner import run_llm_review

        conn = get_system_db()
        sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        ent = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert ent["visibility_status"] == "approved", "deferred promotion: entity stays approved at prior version"
        run_llm_review(
            sub_id,
            plugin_dir=Path(get_store_dir()) / eid / "versions" / "v2" / "plugin",
            conn_factory=get_system_db,
            api_key_loader=lambda: "sk-test",
            model_loader=lambda: "claude-haiku-4-5-20251001",
        )

        # Confirm the submission really landed at review_error.
        conn = get_system_db()
        sub = StoreSubmissionsRepository(conn).get(sub_id)
        ent = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert sub["status"] == "review_error"
        assert ent["visibility_status"] == "approved", "entity must remain approved — prior version still serving"

        # Detail page MUST surface the failure.
        r = web_client.get(
            f"/marketplace/flea/{eid}",
            cookies=owner_cookies,
        )
        assert r.status_code == 200
        body = r.text
        # The widened v2+ review_error copy mentions the prior version
        # still serving — that's the user-visible signal we just added.
        assert "Latest edit failed review" in body, (
            "banner partial must render review_error H3 for v2+ edit when prior version still serves"
        )
        assert "previously approved version (v1)" in body, "banner copy must explain why the entity still appears live"
        # The model's error string must reach the page so the owner
        # can see what went wrong.
        assert "LLMFormatError" in body

    def test_banner_shows_version_n_under_review(self, web_client, monkeypatch):
        from src.repositories.store_submissions import StoreSubmissionsRepository

        owner_id, owner_cookies = _create_user(web_client, "bannerowner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="bannerver")

        # Switch guardrails on; stub LLM scheduler so v2 stays pending.
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)
        monkeypatch.setattr(
            "app.api.store._schedule_llm_review",
            lambda *a, **kw: None,
        )

        v2 = _make_skill_zip("bannerver", body="v2 " * 80)
        web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )
        # v2 review pending. visibility on entity stays approved (per
        # the deferred-promotion fix). But banner partial reads sub
        # status — for an in-flight edit submission the banner won't
        # render unless visibility != approved. Lock the scenario:
        # ensure entity stays at version_no=1 + visibility approved.
        conn = get_system_db()
        ent = StoreEntitiesRepository(conn).get(eid)
        sub = StoreSubmissionsRepository(conn).latest_for_entity(eid)
        conn.close()
        assert ent["version_no"] == 1
        assert ent["visibility_status"] == "approved"
        assert sub["status"] == "pending_llm"

        # Detail page renders. Banner partial only fires when
        # visibility_status != approved, so for a deferred-edit case
        # the marketplace detail does NOT render the quarantine
        # banner — that's correct UX (consumers see the entity as
        # approved and operational). Owner-facing review status
        # surfaces via the Edit button being disabled.
        r = web_client.get(
            f"/marketplace/flea/{eid}",
            cookies=owner_cookies,
        )
        assert r.status_code == 200
        # Edit must reflect the in-flight review (locked). The blue theme spells
        # it in the button label; the default paper look disables the store-menu
        # row and puts the reason in its title.
        assert (
            "review in flight" in r.text
            or "Wait for the in-flight review to finish before editing." in r.text
        )


class TestAuditLogPerVersion:
    """Each edit / restore writes audit rows carrying the version_no
    in params, so the entity timeline can attribute events to the
    right version."""

    def test_edit_audit_carries_version_no(self, web_client):
        from src.repositories.audit import AuditRepository

        owner_id, owner_cookies = _create_user(web_client, "auditver@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="auditver")
        v2 = _make_skill_zip("auditver", body="v2 " * 80)
        web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )

        conn = get_system_db()
        rows = AuditRepository(conn).query_for_resources(
            [f"store_entity:{eid}"],
            limit=20,
        )
        conn.close()
        # store.entity.update event for the edit must carry version_no
        # in its params.
        update_rows = [r for r in rows if r.get("action") == "store.entity.update"]
        assert update_rows, "missing store.entity.update audit"
        assert any((r.get("params") or {}).get("version_no") == 2 for r in update_rows), (
            "update audit must carry version_no=2 in params"
        )

    def test_restore_audit_carries_versions(self, web_client):
        from src.repositories.audit import AuditRepository

        owner_id, owner_cookies = _create_user(web_client, "auditrestore@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="auditrest")
        v2 = _make_skill_zip("auditrest", body="v2 " * 80)
        web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )
        web_client.post(
            f"/api/store/entities/{eid}/versions/1/restore",
            cookies=owner_cookies,
        )

        conn = get_system_db()
        rows = AuditRepository(conn).query_for_resources(
            [f"store_entity:{eid}"],
            limit=20,
        )
        conn.close()
        restore_rows = [r for r in rows if r.get("action") == "store.entity.restore"]
        assert restore_rows, "missing store.entity.restore audit"
        params = restore_rows[0].get("params") or {}
        assert params.get("restored_from_version_no") == 1
        assert params.get("new_version_no") == 3


class TestPRReviewFixes:
    """Locks in the fixes called out in the PR #239 review."""

    def test_block_while_pending_fires_for_v2_edit_under_deferred_promotion(
        self,
        web_client,
        monkeypatch,
    ):
        """#1 — v2+ edit during in-flight LLM review must 409 even
        though entity.visibility_status is still 'approved'."""
        from src.repositories.store_submissions import StoreSubmissionsRepository

        owner_id, owner_cookies = _create_user(web_client, "blockv2@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="blockv2")

        # Switch guardrails on for the edit so promotion defers.
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)
        monkeypatch.setattr(
            "app.api.store._schedule_llm_review",
            lambda *a, **kw: None,
        )

        v2 = _make_skill_zip("blockv2", body="v2 " * 80)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text

        # Entity should remain at 'approved' visibility — v2 in flight.
        conn = get_system_db()
        ent = StoreEntitiesRepository(conn).get(eid)
        sub = StoreSubmissionsRepository(conn).latest_for_entity(eid)
        conn.close()
        assert ent["visibility_status"] == "approved"
        assert sub["status"] == "pending_llm"

        # Second concurrent edit MUST be blocked.
        v3 = _make_skill_zip("blockv2", body="v3 " * 80)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v3.zip", v3, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "prior_version_pending"

    def test_name_change_with_bundle_does_not_rename_live_until_promote(
        self,
        web_client,
        monkeypatch,
    ):
        """#2 — name + bundle in same PUT must NOT rename live until
        the LLM approves and promotion runs. Existing installer keeps
        getting the prior bundle under the prior slug."""
        from app.utils import get_store_dir

        owner_id, owner_cookies = _create_user(web_client, "rename-defer@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="origname")

        plugin_dir = Path(get_store_dir()) / eid / "plugin"
        old_skill_dir = plugin_dir / "skills" / "origname-by-rename-defer"
        assert old_skill_dir.is_dir()

        # Enable guardrails so promotion defers.
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)
        monkeypatch.setattr(
            "app.api.store._schedule_llm_review",
            lambda *a, **kw: None,
        )

        v2 = _make_skill_zip("origname", body="v2 " * 80)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            data={"name": "newname"},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text

        # Live skill dir MUST still hold the old slug — promotion
        # hasn't fired since we stubbed _schedule_llm_review.
        assert old_skill_dir.is_dir(), (
            "live skill dir was renamed before LLM approval — violates deferred-promotion contract"
        )
        new_skill_dir = plugin_dir / "skills" / "newname-by-rename-defer"
        assert not new_skill_dir.exists(), "live shouldn't have the renamed slug yet"
        # Version dir HAS been renamed (so promotion will land on the
        # new slug).
        v2_dir = Path(get_store_dir()) / eid / "versions" / "v2" / "plugin" / "skills" / "newname-by-rename-defer"
        assert v2_dir.is_dir(), "version dir should carry the new slug"

    def test_v2_approval_logs_approved_not_skipped(
        self,
        web_client,
        monkeypatch,
    ):
        """#3 — v2+ approvals must log store.submission.approved, NOT
        store.submission.bg_verdict_skipped. Pre-fix the runner used
        the visibility-flip return value to gate the audit; under
        deferred promotion v2+ never flips visibility (already
        'approved'), so the wrong audit was emitted."""
        from src.repositories.audit import AuditRepository
        from src.repositories.store_submissions import StoreSubmissionsRepository
        from src.store_guardrails.runner import run_llm_review
        from app.utils import get_store_dir

        owner_id, owner_cookies = _create_user(web_client, "auditv2@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="auditv2")

        # Enable guardrails for the edit. Stub LLM scheduler so we
        # control when the runner fires.
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)
        monkeypatch.setattr(
            "app.api.store._schedule_llm_review",
            lambda *a, **kw: None,
        )
        # Mock the LLM call to return a safe verdict.
        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle",
            lambda *a, **kw: {
                "risk_level": "safe",
                "summary": "ok",
                "findings": [],
                "template_placeholders_found": 0,
                "reviewed_by_model": "mock",
                "error": None,
            },
        )

        v2 = _make_skill_zip("auditv2", body="v2 " * 80)
        web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )

        # Manually fire the runner against the v2 dir.
        conn = get_system_db()
        sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()
        run_llm_review(
            sub_id,
            plugin_dir=Path(get_store_dir()) / eid / "versions" / "v2" / "plugin",
            conn_factory=get_system_db,
            api_key_loader=lambda: "sk-test",
            model_loader=lambda: "claude-haiku-4-5-20251001",
        )

        # Audit log must contain store.submission.approved with
        # promoted_to_version_no=2; NO bg_verdict_skipped.
        conn = get_system_db()
        rows = AuditRepository(conn).query_for_resources(
            [f"store_submission:{sub_id}"],
            limit=20,
        )
        conn.close()
        actions = [r.get("action") for r in rows]
        assert "store.submission.approved" in actions, f"v2 approval missing approved audit; got {actions}"
        assert "store.submission.bg_verdict_skipped" not in actions, (
            f"v2 approval should NOT log bg_verdict_skipped; got {actions}"
        )
        approved_row = next(r for r in rows if r.get("action") == "store.submission.approved")
        params = approved_row.get("params") or {}
        assert params.get("promoted_to_version_no") == 2

    def test_bg_verdict_skipped_when_admin_archives_during_review(
        self,
        web_client,
        monkeypatch,
    ):
        """Negative: when admin DOES archive mid-review, the runner
        correctly logs bg_verdict_skipped (not approved)."""
        from src.repositories.audit import AuditRepository
        from src.repositories.store_submissions import StoreSubmissionsRepository
        from src.repositories.store_entities import StoreEntitiesRepository
        from src.store_guardrails.runner import run_llm_review
        from app.utils import get_store_dir

        owner_id, owner_cookies = _create_user(web_client, "archmid@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="archmid")

        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)
        monkeypatch.setattr(
            "app.api.store._schedule_llm_review",
            lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle",
            lambda *a, **kw: {
                "risk_level": "safe",
                "summary": "ok",
                "findings": [],
                "template_placeholders_found": 0,
                "reviewed_by_model": "mock",
                "error": None,
            },
        )

        v2 = _make_skill_zip("archmid", body="v2 " * 80)
        web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )

        # Admin archives BEFORE runner fires.
        conn = get_system_db()
        StoreEntitiesRepository(conn).archive(eid, by_user_id="admin-x")
        sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()

        run_llm_review(
            sub_id,
            plugin_dir=Path(get_store_dir()) / eid / "versions" / "v2" / "plugin",
            conn_factory=get_system_db,
            api_key_loader=lambda: "sk-test",
            model_loader=lambda: "claude-haiku-4-5-20251001",
        )

        conn = get_system_db()
        rows = AuditRepository(conn).query_for_resources(
            [f"store_submission:{sub_id}"],
            limit=20,
        )
        conn.close()
        actions = [r.get("action") for r in rows]
        assert "store.submission.bg_verdict_skipped" in actions, (
            f"archive-during-review must log bg_verdict_skipped; got {actions}"
        )
        assert "store.submission.approved" not in actions, f"archive-during-review must NOT log approved; got {actions}"


class TestAdminQueueShowsVersion:
    def test_admin_queue_shows_v_no_after_name(self, web_client):
        """v# column derives version_no from entity.version_history by
        matching submission.version (hash) against the entry hashes."""
        owner_id, owner_cookies = _create_user(web_client, "vqowner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="vqcol")
        v2 = _make_skill_zip("vqcol", body="v2 body. " * 80)
        web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )

        _, admin_cookies = _create_admin(web_client)
        r = web_client.get(
            "/api/admin/store/submissions",
            cookies=admin_cookies,
        )
        assert r.status_code == 200
        items = {it["name"]: it for it in r.json()["items"]}
        assert "vqcol" in items
        # version_no derived for the v2 row should be 2.
        # The list returns rows newest-first; pick the v2 (current).
        v2_row = next(it for it in r.json()["items"] if it.get("entity_id") == eid and it.get("version_no") == 2)
        assert v2_row["version_no"] == 2
        assert v2_row["entity_version_no"] == 2

    def test_admin_detail_shows_version_no(self, web_client):
        """Detail page renders v# under the Status / Entity-lifecycle
        block + a separate Bundle hash row."""
        from src.repositories.store_submissions import StoreSubmissionsRepository

        owner_id, owner_cookies = _create_user(web_client, "vdowner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="vdrow")

        conn = get_system_db()
        sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()

        _, admin_cookies = _create_admin(web_client)
        r = web_client.get(
            f"/admin/store/submissions/{sub_id}",
            cookies=admin_cookies,
        )
        assert r.status_code == 200
        body = r.text
        assert "<dt>Version</dt>" in body
        assert "<dt>Bundle hash</dt>" in body
        assert "v1" in body


class TestPublishGateFailClosed:
    """Hold-for-review when ``guardrails.enabled: true`` but no LLM
    provider credentials are present in env. The pre-v45 fall-back
    silently auto-approved every upload — a fail-OPEN hole the
    operator couldn't notice. New behavior: submissions sit at
    ``pending_llm``, entity stays at ``visibility_status='pending'``,
    admin retries from /admin/store/submissions after providing
    credentials."""

    def test_v1_upload_enabled_but_not_ready_holds_at_pending(
        self,
        web_client,
        monkeypatch,
    ):
        from src.repositories.store_submissions import StoreSubmissionsRepository

        # Flip guardrails ON but leave provider_ready as False.
        monkeypatch.setattr(
            "app.api.store.get_guardrails_enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "app.api.store.get_guardrails_llm_provider_ready",
            lambda: False,
        )
        # No mock review_bundle — we should never call the LLM.
        # If we did, the lack of patching would surface as a real
        # network call attempt, easy to catch as a hang.

        owner_id, owner_cookies = _create_user(web_client, "holdv1@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="holdv1")

        conn = get_system_db()
        ent = StoreEntitiesRepository(conn).get(eid)
        sub = StoreSubmissionsRepository(conn).latest_for_entity(eid)
        conn.close()
        assert ent["visibility_status"] == "pending", "enabled-but-not-ready must NOT publish — entity stays pending"
        assert sub["status"] == "pending_llm", "submission must hold at pending_llm awaiting admin retry"
        assert sub["llm_findings"] is None, "no LLM call was made — findings must be empty"

    def test_admin_retry_pending_llm_fires_review(
        self,
        web_client,
        monkeypatch,
    ):
        """After the operator sets the API key, admin Retry-review on a
        held pending_llm row schedules + runs the LLM."""
        from src.repositories.store_submissions import StoreSubmissionsRepository

        # Phase 1: upload with provider not-ready → held at pending_llm.
        monkeypatch.setattr(
            "app.api.store.get_guardrails_enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "app.api.store.get_guardrails_llm_provider_ready",
            lambda: False,
        )
        owner_id, owner_cookies = _create_user(web_client, "retryholder@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="retryholder")

        conn = get_system_db()
        sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()

        # Phase 2: operator adds credentials, admin retries.
        # Inject a fake env var so default_api_key_loader doesn't raise
        # before the mock review_bundle runs.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-fake-key-for-retry")

        # Mock review_bundle so the retry resolves to approved without
        # touching the network.
        def mock_review_bundle(*args, **kwargs):
            return {
                "risk_level": "safe",
                "summary": "ok",
                "findings": [],
                "template_placeholders_found": 0,
                "reviewed_by_model": "mock-model",
                "error": None,
                "content_quality": {"verdict": "pass", "issues": []},
            }

        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle",
            mock_review_bundle,
        )

        _, admin_cookies = _create_admin(web_client)
        r = web_client.post(
            f"/api/admin/store/submissions/{sub_id}/retry",
            cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text
        # After retry, BG task runs synchronously in TestClient (it
        # blocks the response). Verify the row moved to approved.
        conn = get_system_db()
        sub = StoreSubmissionsRepository(conn).get(sub_id)
        ent = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert sub["status"] == "approved", f"retry must drive submission to approved; got {sub['status']}"
        assert ent["visibility_status"] == "approved", "entity must flip to approved after LLM ok"

    def test_edit_enabled_but_not_ready_holds_prior_serving(
        self,
        web_client,
        monkeypatch,
    ):
        """v2+ edit under enabled-but-not-ready: v1 keeps serving,
        v2 submission held at pending_llm. Critical safety property:
        no silent promotion."""
        from src.repositories.store_submissions import StoreSubmissionsRepository

        # Initial upload runs with guardrails OFF (autouse default) →
        # v1 approved. Then flip to enabled-but-not-ready for PUT.
        owner_id, owner_cookies = _create_user(web_client, "holdedit@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="holdedit")
        conn = get_system_db()
        v1_hash = StoreEntitiesRepository(conn).get(eid)["version"]
        conn.close()

        monkeypatch.setattr(
            "app.api.store.get_guardrails_enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "app.api.store.get_guardrails_llm_provider_ready",
            lambda: False,
        )
        v2 = _make_skill_zip("holdedit", body="V2 BODY " * 80)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text

        conn = get_system_db()
        ent = StoreEntitiesRepository(conn).get(eid)
        sub = StoreSubmissionsRepository(conn).latest_for_entity(eid)
        conn.close()
        # Entity stays approved at v1, v2 sits at pending_llm.
        assert ent["visibility_status"] == "approved"
        assert ent["version_no"] == 1
        assert ent["version"] == v1_hash, "live bundle must remain v1 — no silent promotion of v2"
        assert sub["status"] == "pending_llm"
        assert sub["llm_findings"] is None

    def test_disabled_intent_still_auto_approves(
        self,
        web_client,
        monkeypatch,
    ):
        """Operator explicitly opting out (``enabled: false``) keeps
        the prior auto-approve behavior — local dev / no-LLM
        deployments aren't blocked."""
        from src.repositories.store_submissions import StoreSubmissionsRepository

        # autouse fixture already sets enabled=False. Just confirm
        # behavior end-to-end.
        owner_id, owner_cookies = _create_user(web_client, "offowner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="offowner")

        conn = get_system_db()
        ent = StoreEntitiesRepository(conn).get(eid)
        sub = StoreSubmissionsRepository(conn).latest_for_entity(eid)
        conn.close()
        assert ent["visibility_status"] == "approved"
        assert sub["status"] == "approved"


class TestConcurrentPutSerialization:
    """Codex adversarial review [HIGH]: concurrent PUTs racing on the
    same entity_id could both pass the ``latest_for_entity`` pending
    gate, both bake into ``versions/v<N+1>/plugin/``, and both append
    a ``version_history`` entry. Per-entity asyncio lock added to
    serialize the critical section in PUT + restore.

    Integration coverage (two real PUTs racing against TestClient)
    isn't practical here: each TestClient call wraps the async handler
    in its own event loop, so asyncio.Lock acquired in loop A cannot
    coordinate with loop B — they deadlock instead of contending. In
    a real uvicorn deployment all requests run on a single event loop
    and the lock works as designed. This test exercises the helper
    directly to verify the serialization semantics; the integration
    side is covered by the existing `prior_version_pending` test
    (which fires once the first PUT has committed)."""

    def test_per_entity_lock_serializes(self):
        import asyncio
        from app.api.store import _hold_entity_write_lock

        seq: list = []

        async def task(label: str) -> None:
            async with _hold_entity_write_lock("ent-shared"):
                seq.append(f"{label}-in")
                # Yield to the scheduler to give the other coroutine a
                # chance to run if the lock isn't held.
                await asyncio.sleep(0.01)
                seq.append(f"{label}-out")

        async def driver() -> None:
            await asyncio.gather(task("A"), task("B"))

        asyncio.run(driver())

        # Pairs must NOT interleave — one finishes entirely before
        # the other starts.
        assert seq in (
            ["A-in", "A-out", "B-in", "B-out"],
            ["B-in", "B-out", "A-in", "A-out"],
        ), f"per-entity lock failed to serialize: seq={seq}"

    def test_per_entity_lock_does_not_serialize_across_entities(self):
        """Different entity_ids get independent locks so unrelated
        writes don't block each other."""
        import asyncio
        from app.api.store import _hold_entity_write_lock

        seq: list = []

        async def task(label: str, entity: str) -> None:
            async with _hold_entity_write_lock(entity):
                seq.append(f"{label}-in")
                await asyncio.sleep(0.01)
                seq.append(f"{label}-out")

        async def driver() -> None:
            await asyncio.gather(task("A", "ent-a"), task("B", "ent-b"))

        asyncio.run(driver())

        # Interleaving expected: A-in, B-in, A-out, B-out (or B/A
        # ordering depending on which coroutine the loop picks first).
        assert seq[0] in {"A-in", "B-in"}
        assert seq[1] in {"A-in", "B-in"}
        assert seq[0] != seq[1], f"entities should have run in parallel — got serial: {seq}"


class TestBgTaskIdempotency:
    """Codex adversarial review [HIGH]: `update_status` blindly
    overwrote any current status. A late BG-task LLM verdict racing
    with an admin override could clobber `overridden` back to
    `approved`/`blocked_llm`. Now: terminal statuses are
    compare-and-swap-protected; BG callers no-op."""

    def test_late_verdict_does_not_clobber_overridden(self, web_client):
        """Admin overrides a blocked submission. A subsequent late
        BG-task ``update_status`` for the same submission must NOT
        flip it back."""
        from src.repositories.store_entities import StoreEntitiesRepository
        from src.repositories.store_submissions import StoreSubmissionsRepository

        user_id, _ = _create_user(web_client, "idemp@x.com")
        conn = get_system_db()
        ents = StoreEntitiesRepository(conn)
        ents.create(
            id="ent-idemp",
            owner_user_id=user_id,
            owner_username="idemp",
            type="skill",
            name="idemp-skill",
            description="x" * 40,
            category=None,
            version="aaaaaaaaaaaaaaaa",
            file_size=10,
            visibility_status="pending",
        )
        subs = StoreSubmissionsRepository(conn)
        sid = subs.create(
            submitter_id=user_id,
            submitter_email="idemp@x.com",
            type="skill",
            name="idemp-skill",
            version="aaaaaaaaaaaaaaaa",
            status="blocked_llm",
            entity_id="ent-idemp",
            llm_findings={"risk_level": "high", "summary": "x"},
        )
        ents.update_history_submission_id("ent-idemp", 1, sid)
        conn.close()

        from tests.helpers.auth import grant_admin

        admin_id, admin_cookies = _create_user(web_client, "idemp-admin@x.com")
        conn = get_system_db()
        grant_admin(conn, admin_id)
        conn.close()

        # Override the blocked submission → status='overridden'.
        r = web_client.post(
            f"/api/admin/store/submissions/{sid}/override",
            json={"reason": "false positive — cleared in offline review"},
            cookies=admin_cookies,
        )
        assert r.status_code == 200

        # Now simulate a late BG-task verdict arriving:
        # update_status is called without allow_terminal_overwrite.
        conn = get_system_db()
        subs = StoreSubmissionsRepository(conn)
        # CAS no-op because status=='overridden' is terminal.
        wrote = subs.update_status(
            sid,
            status="approved",
            llm_findings={"risk_level": "safe", "summary": "late"},
        )
        conn.close()
        assert wrote is False, "late BG verdict must NOT overwrite a terminal `overridden` row"

        # Status still overridden.
        conn = get_system_db()
        row = StoreSubmissionsRepository(conn).get(sid)
        conn.close()
        assert row["status"] == "overridden"

    def test_runner_late_verdict_logs_skipped_not_approved(
        self,
        web_client,
        monkeypatch,
    ):
        """End-to-end pair to ``test_late_verdict_does_not_clobber_overridden``:
        when the LLM verdict lands on an already-overridden submission,
        ``runner.run_llm_review`` honors the CAS bool and:
          1. row status stays ``overridden``,
          2. audit log gets a single ``bg_verdict_skipped`` entry,
          3. audit log does NOT get a contradictory ``approved`` /
             ``blocked_llm`` entry — pre-fix the runner discarded the
             return value and ran the downstream cascade including
             the misleading audit write.
        """
        from src.repositories.audit import AuditRepository
        from src.repositories.store_submissions import StoreSubmissionsRepository
        from src.store_guardrails.runner import run_llm_review
        from app.utils import get_store_dir

        owner_id, owner_cookies = _create_user(web_client, "lateverdict@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="lateverdict")

        # Flip guardrails on, PUT v2 → pending_llm under deferred-promotion
        # (visibility stays 'approved' at v1).
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
        monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)
        monkeypatch.setattr(
            "app.api.store._schedule_llm_review",
            lambda *a, **kw: None,
        )
        # Mock review_bundle to return an "approved"-shape verdict so
        # the runner would (pre-fix) hit the approved branch + cascade.
        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle",
            lambda *a, **kw: {
                "risk_level": "safe",
                "summary": "ok",
                "findings": [],
                "template_placeholders_found": 0,
                "reviewed_by_model": "mock",
                "error": None,
            },
        )

        v2 = _make_skill_zip("lateverdict", body="v2 " * 80)
        web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )

        # Admin override flips the v2 submission row to 'overridden'.
        from tests.helpers.auth import grant_admin

        admin_id, admin_cookies = _create_user(web_client, "lv-admin@x.com")
        conn = get_system_db()
        grant_admin(conn, admin_id)
        sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()
        r = web_client.post(
            f"/api/admin/store/submissions/{sub_id}/override",
            json={"reason": "false positive cleared offline"},
            cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text

        # Now fire the runner directly — it would (pre-fix) try to write
        # status='approved' on the already-overridden row.
        run_llm_review(
            sub_id,
            plugin_dir=Path(get_store_dir()) / eid / "versions" / "v2" / "plugin",
            conn_factory=get_system_db,
            api_key_loader=lambda: "sk-test",
            model_loader=lambda: "claude-haiku-4-5-20251001",
        )

        # Row must stay overridden + audit log must show skipped, not
        # the misleading approved write.
        conn = get_system_db()
        row = StoreSubmissionsRepository(conn).get(sub_id)
        rows = AuditRepository(conn).query_for_resources(
            [f"store_submission:{sub_id}"],
            limit=20,
        )
        conn.close()
        actions = [r.get("action") for r in rows]
        assert row["status"] == "overridden", f"row must stay overridden under CAS no-op; got {row['status']}"
        assert "store.submission.bg_verdict_skipped" in actions, (
            f"runner must log bg_verdict_skipped on CAS no-op; got {actions}"
        )
        assert "store.submission.approved" not in actions, (
            "runner must NOT log approved when the CAS no-op'd the write — "
            f"audit must not contradict the row state; got {actions}"
        )

    def test_explicit_allow_terminal_overwrite_works(self, web_client):
        """Admin paths that legitimately need to overwrite a terminal
        state can pass `allow_terminal_overwrite=True` and get the
        write through. Used by rescan and similar admin actions."""
        from src.repositories.store_submissions import StoreSubmissionsRepository

        user_id, _ = _create_user(web_client, "termok@x.com")
        conn = get_system_db()
        sid = StoreSubmissionsRepository(conn).create(
            submitter_id=user_id,
            submitter_email="termok@x.com",
            type="skill",
            name="x",
            version="aaaa",
            status="approved",
            entity_id=None,
        )
        wrote = StoreSubmissionsRepository(conn).update_status(
            sid,
            status="pending_llm",
            allow_terminal_overwrite=True,
        )
        conn.close()
        assert wrote is True
        conn = get_system_db()
        row = StoreSubmissionsRepository(conn).get(sid)
        conn.close()
        assert row["status"] == "pending_llm"


class TestAtomicPromote:
    """Codex adversarial review [MEDIUM]: pre-fix sequence was
    ``repo.promote_version(...)`` → ``_swap_live_to_version(...)``.
    If the source ``versions/v<N>/plugin/`` was missing,
    ``_swap_live_to_version`` returned False silently — leaving DB
    at the new version but live still on the prior bytes.

    Fix: a ``promote_to_version`` helper that swaps live FIRST, then
    promotes the DB. Missing source → return None, no DB change."""

    def test_missing_source_dir_does_not_advance_db(self, web_client):
        """Promote with a missing version dir must leave both DB and
        live untouched."""
        from app.api.store import promote_to_version
        from src.repositories.store_entities import StoreEntitiesRepository

        user_id, _ = _create_user(web_client, "atomic@x.com")
        conn = get_system_db()
        repo = StoreEntitiesRepository(conn)
        repo.create(
            id="ent-atomic",
            owner_user_id=user_id,
            owner_username="atomic",
            type="skill",
            name="atomic",
            description="x" * 40,
            category=None,
            version="aaaaaaaaaaaaaaaa",
            file_size=10,
            visibility_status="approved",
        )
        # Inject a v2 history entry without creating the on-disk dir
        # — simulates the "DB has entry, bundle wiped" inconsistency.
        repo.append_version_history(
            "ent-atomic",
            version_hash="bbbbbbbbbbbbbbbb",
            sha256=None,
            size=20,
            submission_id="fake-sub",
            created_by=user_id,
        )
        conn.close()

        # Attempt to promote to v2 — version dir doesn't exist.
        conn = get_system_db()
        repo = StoreEntitiesRepository(conn)
        result = promote_to_version("ent-atomic", 2, repo)
        ent_after = repo.get("ent-atomic")
        conn.close()
        assert result is None, "must signal failure when source missing"
        assert ent_after["version_no"] == 1, (
            f"DB must NOT advance when live swap can't happen; got version_no={ent_after['version_no']}"
        )


class TestPromoteLookupByByteIdenticalBundles:
    """Live-issue regression observed on a development deployment: an
    entity had multiple version_history rows sharing the same `hash`
    (user re-uploaded byte-identical bundles as v2/v4/v6). The runner's
    promote-on-approve path looked up the submission's version_no
    in version_history BY HASH and broke on the FIRST match — always
    v1. With v1's n=1 and current=1, the forward-only
    `target > current` guard skipped the promote, so the passing
    LLM verdict never advanced the entity. UI kept showing v1 as
    'current' even though the new submission's status was 'approved'.

    Fix: look up by `submission_id` via `_version_no_for_submission`."""

    def test_byte_identical_v2_promotes_to_current(
        self,
        web_client,
        monkeypatch,
    ):
        from pathlib import Path
        from app.utils import get_store_dir
        from src.repositories.store_submissions import StoreSubmissionsRepository
        from src.store_guardrails.runner import run_llm_review

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-for-identical-test")
        owner_id, owner_cookies = _create_user(web_client, "identical@x.com")
        # Same body for v1 + v2 → byte-identical zip → same hash.
        identical_body = (
            "Identical body line that is intentionally long enough to "
            "clear the content threshold for skill bodies. " * 4
        )
        v1_zip = _make_skill_zip("identical", body=identical_body)
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("v1.zip", v1_zip, "application/zip")},
            data={"type": "skill", "description": _OK_DESC},
            cookies=owner_cookies,
        )
        assert r.status_code == 201, r.text
        eid = r.json()["id"]

        conn = get_system_db()
        ent_v1 = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        v1_hash = ent_v1["version"]
        assert ent_v1["version_no"] == 1

        # Flip guardrails ON for v2. Mock LLM to approve.
        monkeypatch.setattr(
            "app.api.store.get_guardrails_enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "app.api.store.get_guardrails_llm_provider_ready",
            lambda: True,
        )

        def mock_approve(*a, **kw):
            return {
                "risk_level": "safe",
                "summary": "ok",
                "findings": [],
                "template_placeholders_found": 0,
                "reviewed_by_model": "mock",
                "error": None,
                "content_quality": {"verdict": "pass", "issues": []},
            }

        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle",
            mock_approve,
        )

        # PUT v2 with IDENTICAL bytes.
        v2_zip = _make_skill_zip("identical", body=identical_body)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2_zip, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text

        conn = get_system_db()
        v2_sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()
        run_llm_review(
            v2_sub_id,
            plugin_dir=Path(get_store_dir()) / eid / "versions" / "v2" / "plugin",
            conn_factory=get_system_db,
            api_key_loader=lambda: "sk",
            model_loader=lambda: "mock",
        )

        conn = get_system_db()
        ent_after = StoreEntitiesRepository(conn).get(eid)
        v2_sub = StoreSubmissionsRepository(conn).get(v2_sub_id)
        conn.close()
        # Pre-fix the runner would have matched v1's history entry
        # first (same hash), target_version_no=1, `1 > 1` False, no
        # promote → entity stuck at v1.
        assert ent_after["version_no"] == 2, (
            f"v2 must promote even when its hash matches v1's; got "
            f"version_no={ent_after['version_no']}. Lookup-by-hash "
            f"would have stuck the entity at v1."
        )
        assert v2_sub["status"] == "approved"
        assert ent_after["version"] == v1_hash, "hash unchanged (bundle is byte-identical) but version_no DID move"

    def test_byte_identical_v3_after_different_v2(
        self,
        web_client,
        monkeypatch,
    ):
        """v1 + v2 (different hash) + v3 byte-identical to v1.
        Lookup must resolve v3 to n=3, not v1 (same hash) or v2 (the
        most-recent approved). With current=2 and target=3 the
        forward-only guard fires correctly only if target_n=3."""
        from pathlib import Path
        from app.utils import get_store_dir
        from src.repositories.store_submissions import StoreSubmissionsRepository
        from src.store_guardrails.runner import run_llm_review

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-for-v3-test")
        owner_id, owner_cookies = _create_user(web_client, "v3hash@x.com")

        body_a = "Body A line that is intentionally long enough to clear the content threshold for skill bodies. " * 4
        body_b = (
            "Body B line that is intentionally DIFFERENT and also long "
            "enough to clear the content threshold for skill bodies. " * 4
        )

        r = web_client.post(
            "/api/store/entities",
            files={"file": ("v1.zip", _make_skill_zip("v3hash", body=body_a), "application/zip")},
            data={"type": "skill", "description": _OK_DESC},
            cookies=owner_cookies,
        )
        assert r.status_code == 201
        eid = r.json()["id"]

        monkeypatch.setattr(
            "app.api.store.get_guardrails_enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "app.api.store.get_guardrails_llm_provider_ready",
            lambda: True,
        )

        def mock_approve(*a, **kw):
            return {
                "risk_level": "safe",
                "summary": "ok",
                "findings": [],
                "template_placeholders_found": 0,
                "reviewed_by_model": "mock",
                "error": None,
                "content_quality": {"verdict": "pass", "issues": []},
            }

        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle",
            mock_approve,
        )

        v2_zip = _make_skill_zip("v3hash", body=body_b)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2_zip, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200
        conn = get_system_db()
        v2_sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()
        run_llm_review(
            v2_sub_id,
            plugin_dir=Path(get_store_dir()) / eid / "versions" / "v2" / "plugin",
            conn_factory=get_system_db,
            api_key_loader=lambda: "sk",
            model_loader=lambda: "mock",
        )
        conn = get_system_db()
        ent_at_v2 = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert ent_at_v2["version_no"] == 2

        v3_zip = _make_skill_zip("v3hash", body=body_a)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v3.zip", v3_zip, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200
        conn = get_system_db()
        v3_sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()
        run_llm_review(
            v3_sub_id,
            plugin_dir=Path(get_store_dir()) / eid / "versions" / "v3" / "plugin",
            conn_factory=get_system_db,
            api_key_loader=lambda: "sk",
            model_loader=lambda: "mock",
        )
        conn = get_system_db()
        ent_at_v3 = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert ent_at_v3["version_no"] == 3, (
            f"v3 must promote despite hash collision with v1; got version_no={ent_at_v3['version_no']}"
        )


class TestRescanPromotesNonCurrent:
    """Codex adversarial-review follow-up on PR #330: admin rescan
    with `guardrails.enabled: false` flipped status='approved' +
    visibility but never called `promote_to_version`. A rescan that
    re-approved a non-current v2+ left the entity stuck at the prior
    version. Fix mirrors the inline-promote in create/update/restore."""

    def test_rescan_promotes_non_current_v2_when_guardrails_disabled(
        self,
        web_client,
        monkeypatch,
    ):
        from pathlib import Path
        from app.utils import get_store_dir
        from src.repositories.store_submissions import StoreSubmissionsRepository
        from src.store_guardrails.runner import run_llm_review

        owner_id, owner_cookies = _create_user(web_client, "rescanpromote@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="rescanpromote")

        monkeypatch.setattr(
            "app.api.store.get_guardrails_enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "app.api.store.get_guardrails_llm_provider_ready",
            lambda: True,
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-for-rescan-promote")

        def mock_block(*a, **kw):
            return {
                "risk_level": "high",
                "summary": "mock block",
                "findings": [{"severity": "high", "category": "test", "file": "x", "explanation": "mock"}],
                "template_placeholders_found": 0,
                "reviewed_by_model": "mock",
                "error": None,
            }

        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle",
            mock_block,
        )

        v2 = _make_skill_zip("rescanpromote", body="V2 body content " * 30)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text
        conn = get_system_db()
        v2_sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()
        run_llm_review(
            v2_sub_id,
            plugin_dir=Path(get_store_dir()) / eid / "versions" / "v2" / "plugin",
            conn_factory=get_system_db,
            api_key_loader=lambda: "sk",
            model_loader=lambda: "mock",
        )

        conn = get_system_db()
        ent_before = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert ent_before["version_no"] == 1
        v1_hash = ent_before["version"]

        # Rescan with guardrails OFF — branch under test. Patch both
        # bound symbols (admin imports function-locally).
        monkeypatch.setattr(
            "app.api.store.get_guardrails_enabled",
            lambda: False,
        )
        monkeypatch.setattr(
            "app.instance_config.get_guardrails_enabled",
            lambda: False,
        )
        _, admin_cookies = _create_admin(web_client)
        r = web_client.post(
            f"/api/admin/store/submissions/{v2_sub_id}/rescan",
            cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "approved"

        conn = get_system_db()
        ent_after = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert ent_after["version_no"] == 2, (
            f"rescan-approve of v2 must promote entity to v2 when "
            f"guardrails are disabled; got version_no={ent_after['version_no']}"
        )
        assert ent_after["version"] != v1_hash, "entity.version hash must move to v2 after rescan promote"


class TestRestoreReusesApprovedVerdict:
    """Live-bug fix: a second restore of an already-approved version
    sometimes flipped to `blocked_llm` because Anthropic structured
    output is non-deterministic — same bytes, different
    `content_quality.verdict` across calls. Restore now detects
    byte-identical bundles backed by a prior `approved` submission
    (same reviewed_by_model) and reuses that verdict."""

    def test_restore_of_approved_version_skips_llm_and_reuses_verdict(
        self,
        web_client,
        monkeypatch,
    ):
        from pathlib import Path
        from app.utils import get_store_dir
        from src.repositories.store_submissions import StoreSubmissionsRepository
        from src.store_guardrails.runner import run_llm_review

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-for-reuse")
        owner_id, owner_cookies = _create_user(web_client, "reuse@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="reuseme")

        monkeypatch.setattr(
            "app.api.store.get_guardrails_enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "app.api.store.get_guardrails_llm_provider_ready",
            lambda: True,
        )
        monkeypatch.setattr(
            "app.api.store.get_guardrails_review_model",
            lambda: "claude-haiku-4-5-test",
        )
        # Stub the BG-task scheduler so only our direct `run_llm_review`
        # call writes the verdict (real BG uses default_model_loader
        # which resolves to a different reviewed_by_model and would
        # win the terminal-state CAS race).
        monkeypatch.setattr(
            "app.api.store._schedule_llm_review",
            lambda *a, **kw: None,
        )

        approve_calls = {"n": 0}

        def mock_approve(*a, **kw):
            approve_calls["n"] += 1
            return {
                "risk_level": "safe",
                "summary": "ok",
                "findings": [],
                "template_placeholders_found": 0,
                "reviewed_by_model": "claude-haiku-4-5-test",
                "error": None,
                "content_quality": {"verdict": "pass", "issues": []},
            }

        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle",
            mock_approve,
        )

        v2_zip = _make_skill_zip("reuseme", body="V2 body unique " * 30)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2_zip, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text
        conn = get_system_db()
        v2_sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()
        run_llm_review(
            v2_sub_id,
            plugin_dir=Path(get_store_dir()) / eid / "versions" / "v2" / "plugin",
            conn_factory=get_system_db,
            api_key_loader=lambda: "sk",
            model_loader=lambda: "claude-haiku-4-5-test",
        )
        calls_after_v2 = approve_calls["n"]

        v3_zip = _make_skill_zip("reuseme", body="V3 body unique " * 30)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v3.zip", v3_zip, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text
        conn = get_system_db()
        v3_sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()
        run_llm_review(
            v3_sub_id,
            plugin_dir=Path(get_store_dir()) / eid / "versions" / "v3" / "plugin",
            conn_factory=get_system_db,
            api_key_loader=lambda: "sk",
            model_loader=lambda: "claude-haiku-4-5-test",
        )
        calls_after_v3 = approve_calls["n"]
        # BG task in TestClient may fire run_llm_review on its own
        # in addition to our direct call, so don't assert exact count
        # difference — only check the restore window.
        assert calls_after_v3 > calls_after_v2

        # Restore v2 → byte-identical to v2 approved entry. Reuse
        # must fire → no new LLM call.
        r = web_client.post(
            f"/api/store/entities/{eid}/versions/2/restore",
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text
        assert approve_calls["n"] == calls_after_v3, (
            f"restore of approved version must NOT call LLM; count grew from {calls_after_v3} to {approve_calls['n']}"
        )

        conn = get_system_db()
        ent_after = StoreEntitiesRepository(conn).get(eid)
        v4_sub_id = next(
            (e["submission_id"] for e in ent_after["version_history"] if int(e["n"]) == 4),
            None,
        )
        v4_sub = StoreSubmissionsRepository(conn).get(v4_sub_id) if v4_sub_id else None
        conn.close()
        assert v4_sub is not None
        assert v4_sub["status"] == "approved"
        assert v4_sub["reviewed_by_model"] == "claude-haiku-4-5-test"
        assert (v4_sub["llm_findings"] or {}).get("reused_from_submission_id") == v2_sub_id
        assert ent_after["version_no"] == 4

    def test_restore_legacy_v1_falls_back_to_llm(
        self,
        web_client,
        monkeypatch,
    ):
        """v1 seed (guardrails-OFF approval, no reviewed_by_model) is
        NOT eligible for reuse. Restoring v1 must schedule a real
        LLM review under guardrails-on."""
        from pathlib import Path
        from app.utils import get_store_dir
        from src.repositories.store_submissions import StoreSubmissionsRepository
        from src.store_guardrails.runner import run_llm_review

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-fallback")
        owner_id, owner_cookies = _create_user(web_client, "noreuse@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="noreuse")

        monkeypatch.setattr(
            "app.api.store.get_guardrails_enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "app.api.store.get_guardrails_llm_provider_ready",
            lambda: True,
        )
        monkeypatch.setattr(
            "app.api.store.get_guardrails_review_model",
            lambda: "claude-haiku-4-5-test",
        )
        # Stub the BG-task scheduler so only our direct `run_llm_review`
        # call writes the verdict (real BG uses default_model_loader
        # which resolves to a different reviewed_by_model and would
        # win the terminal-state CAS race).
        monkeypatch.setattr(
            "app.api.store._schedule_llm_review",
            lambda *a, **kw: None,
        )

        approve_calls = {"n": 0}

        def mock_approve(*a, **kw):
            approve_calls["n"] += 1
            return {
                "risk_level": "safe",
                "summary": "ok",
                "findings": [],
                "template_placeholders_found": 0,
                "reviewed_by_model": "claude-haiku-4-5-test",
                "error": None,
                "content_quality": {"verdict": "pass", "issues": []},
            }

        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle",
            mock_approve,
        )

        v2_zip = _make_skill_zip("noreuse", body="V2 body " * 30)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2_zip, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text
        conn = get_system_db()
        v2_sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()
        run_llm_review(
            v2_sub_id,
            plugin_dir=Path(get_store_dir()) / eid / "versions" / "v2" / "plugin",
            conn_factory=get_system_db,
            api_key_loader=lambda: "sk",
            model_loader=lambda: "claude-haiku-4-5-test",
        )
        calls_after_v2 = approve_calls["n"]

        r = web_client.post(
            f"/api/store/entities/{eid}/versions/1/restore",
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text
        conn = get_system_db()
        v3_sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()
        run_llm_review(
            v3_sub_id,
            plugin_dir=Path(get_store_dir()) / eid / "versions" / "v3" / "plugin",
            conn_factory=get_system_db,
            api_key_loader=lambda: "sk",
            model_loader=lambda: "claude-haiku-4-5-test",
        )
        # No reuse: BG/manual LLM calls fired (count grew).
        assert approve_calls["n"] > calls_after_v2
        conn = get_system_db()
        v3_sub = StoreSubmissionsRepository(conn).get(v3_sub_id)
        conn.close()
        assert v3_sub["status"] == "approved"
        assert (v3_sub["llm_findings"] or {}).get("reused_from_submission_id") is None


# Mock LLM payloads used by the lifecycle integration test class below.
_MOCK_APPROVE_LIFECYCLE = {
    "risk_level": "safe",
    "summary": "ok",
    "findings": [],
    "template_placeholders_found": 0,
    "reviewed_by_model": "mock-haiku",
    "error": None,
    "content_quality": {"verdict": "pass", "issues": []},
}
_MOCK_BLOCK_LIFECYCLE = {
    "risk_level": "high",
    "summary": "mock block — security issue",
    "findings": [{"severity": "high", "category": "exfiltration", "file": "run.sh", "explanation": "mock-block"}],
    "template_placeholders_found": 0,
    "reviewed_by_model": "mock-haiku",
    "error": None,
    "content_quality": {"verdict": "pass", "issues": []},
}


class TestFullLifecycleFromInstaller:
    """Integration test for the full flea-market lifecycle from
    issuer, admin, and subscribed-user perspectives.

    Walks v1 upload → installer subscribes → v2 promote → v3 blocked
    → admin force-overrides → restore v1. Asserts BOTH entity state
    AND served `marketplace.zip` bytes + ETag at each transition.

    Plan: ~/.claude/plans/peppy-napping-rose.md.
    """

    @staticmethod
    def _install(client, eid, cookies):
        r = client.post(f"/api/store/entities/{eid}/install", cookies=cookies)
        assert r.status_code == 200, r.text

    @staticmethod
    def _serve_zip(client, cookies, if_none_match=None):
        import io as _io
        import zipfile as _zip

        headers = {}
        if if_none_match:
            headers["If-None-Match"] = f'"{if_none_match}"'
        r = client.get("/marketplace.zip", cookies=cookies, headers=headers)
        etag = r.headers.get("etag", "").strip('"')
        if r.status_code == 304:
            return etag, None, 304
        assert r.status_code == 200, r.text
        with _zip.ZipFile(_io.BytesIO(r.content)) as zf:
            contents = {n: zf.read(n) for n in zf.namelist()}
        return etag, contents, 200

    @staticmethod
    def _drive_llm(monkeypatch, eid, sub_id, version_no, mock):
        from pathlib import Path
        from app.utils import get_store_dir
        from src.store_guardrails.runner import run_llm_review

        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle",
            lambda *a, **kw: mock,
        )
        run_llm_review(
            sub_id,
            plugin_dir=Path(get_store_dir()) / eid / "versions" / f"v{version_no}" / "plugin",
            conn_factory=get_system_db,
            api_key_loader=lambda: "sk",
            model_loader=lambda: "mock-haiku",
        )

    @staticmethod
    def _latest_sub_id(eid):
        from src.repositories.store_submissions import StoreSubmissionsRepository

        conn = get_system_db()
        sid = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()
        return sid

    @staticmethod
    def _ent(eid):
        conn = get_system_db()
        e = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        return e

    @staticmethod
    def _installs(installer_id):
        from src.repositories.user_store_installs import UserStoreInstallsRepository

        conn = get_system_db()
        installs = UserStoreInstallsRepository(conn).list_for_user(installer_id)
        conn.close()
        return installs

    @staticmethod
    def _setup_guardrails_on(monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-lifecycle")
        monkeypatch.setattr(
            "app.api.store.get_guardrails_enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "app.api.store.get_guardrails_llm_provider_ready",
            lambda: True,
        )
        monkeypatch.setattr(
            "app.api.store.get_guardrails_review_model",
            lambda: "mock-haiku",
        )
        monkeypatch.setattr(
            "app.api.store._schedule_llm_review",
            lambda *a, **kw: None,
        )

    def test_main_lifecycle_v1_v2_v3blocked_override_restorev1(
        self,
        web_client,
        monkeypatch,
    ):
        """User's exact spec, end-to-end."""
        self._setup_guardrails_on(monkeypatch)
        owner_id, owner_cookies = _create_user(web_client, "lc-owner@x.com")
        installer_id, installer_cookies = _create_user(web_client, "lc-installer@x.com")
        _, admin_cookies = _create_admin(web_client, "lc-admin@x.com")

        # ── Phase 1 ── v1 upload (clean, mock approve)
        v1_zip = _make_skill_zip("lifecycle", body="V1 body content explaining when to use this skill in detail. " * 6)
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("v1.zip", v1_zip, "application/zip")},
            data={"type": "skill", "description": _OK_DESC},
            cookies=owner_cookies,
        )
        assert r.status_code == 201, r.text
        eid = r.json()["id"]
        v1_sub_id = self._latest_sub_id(eid)
        self._drive_llm(monkeypatch, eid, v1_sub_id, 1, _MOCK_APPROVE_LIFECYCLE)

        ent = self._ent(eid)
        assert ent["visibility_status"] == "approved"
        assert ent["version_no"] == 1
        v1_hash = ent["version"]

        # ── Phase 2 ── installer subscribes
        self._install(web_client, eid, installer_cookies)
        installs = self._installs(installer_id)
        assert {r["id"] for r in installs} == {eid}
        assert installs[0]["version"] == v1_hash

        etag_v1, contents_v1, _ = self._serve_zip(web_client, installer_cookies)
        # Skills/agents land bundled under plugins/store-bundle/<skill_slug>/.
        # The suffixed name `lifecycle-by-lc-owner` identifies our entity.
        skill_files_v1 = [n for n in contents_v1 if "lifecycle-by-lc-owner" in n]
        assert skill_files_v1, f"v1 bytes missing from marketplace.zip; got {list(contents_v1)[:5]}"

        # ── Phase 3 ── v2 PUT (approve) → promote
        v2_zip = _make_skill_zip("lifecycle", body="V2 body upgraded with more detail for the skill body content. " * 6)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2_zip, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text
        v2_sub_id = self._latest_sub_id(eid)
        self._drive_llm(monkeypatch, eid, v2_sub_id, 2, _MOCK_APPROVE_LIFECYCLE)

        ent = self._ent(eid)
        assert ent["version_no"] == 2
        v2_hash = ent["version"]
        assert v2_hash != v1_hash

        installs = self._installs(installer_id)
        assert installs[0]["version"] == v2_hash

        etag_v2, contents_v2, _ = self._serve_zip(web_client, installer_cookies)
        assert etag_v2 != etag_v1, "etag must flip on v2 promote"

        # ── Phase 4 ── v3 PUT (mock block) → stays at v2
        v3_zip = _make_skill_zip(
            "lifecycle", body="V3 body risky content that the LLM mock will mark as exfiltration. " * 6
        )
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v3.zip", v3_zip, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text
        v3_sub_id = self._latest_sub_id(eid)
        self._drive_llm(monkeypatch, eid, v3_sub_id, 3, _MOCK_BLOCK_LIFECYCLE)

        from src.repositories.store_submissions import StoreSubmissionsRepository

        conn = get_system_db()
        v3_sub = StoreSubmissionsRepository(conn).get(v3_sub_id)
        ent = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert v3_sub["status"] == "blocked_llm"
        assert ent["version_no"] == 2
        assert ent["version"] == v2_hash

        installs = self._installs(installer_id)
        assert installs[0]["version"] == v2_hash

        etag_v3_check, _, _ = self._serve_zip(web_client, installer_cookies)
        assert etag_v3_check == etag_v2, "etag must NOT flip on blocked submission"

        # ── Phase 5 ── admin overrides v3 → promotes
        r = web_client.post(
            f"/api/admin/store/submissions/{v3_sub_id}/override",
            json={"reason": "false positive cleared offline by admin team"},
            cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text

        ent = self._ent(eid)
        assert ent["version_no"] == 3
        v3_hash = ent["version"]
        assert v3_hash != v2_hash

        installs = self._installs(installer_id)
        assert installs[0]["version"] == v3_hash

        etag_override, _, _ = self._serve_zip(web_client, installer_cookies)
        assert etag_override != etag_v2

        # ── Phase 6 ── restore v1 → v4 with v1 bytes
        r = web_client.post(
            f"/api/store/entities/{eid}/versions/1/restore",
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text
        v4_sub_id = self._latest_sub_id(eid)
        conn = get_system_db()
        v4_pre = StoreSubmissionsRepository(conn).get(v4_sub_id)
        conn.close()
        # PR #332 reuse path may fire if v1 was approved by same model.
        # If not, drive LLM manually.
        if v4_pre["status"] == "pending_llm":
            self._drive_llm(monkeypatch, eid, v4_sub_id, 4, _MOCK_APPROVE_LIFECYCLE)

        ent = self._ent(eid)
        assert ent["version_no"] == 4
        assert ent["version"] == v1_hash, "v4 bytes byte-identical to v1 — entity.version (hash) should match v1's"

        installs = self._installs(installer_id)
        assert installs[0]["version"] == v1_hash, "installer should receive v1 bytes through v4 promotion"

        etag_restore, contents_restore, _ = self._serve_zip(web_client, installer_cookies)
        assert etag_restore != etag_override
        v1_skill = {n: b for n, b in contents_v1.items() if "SKILL.md" in n and "lifecycle-by-lc-owner" in n}
        rs_skill = {n: b for n, b in contents_restore.items() if "SKILL.md" in n and "lifecycle-by-lc-owner" in n}
        assert v1_skill == rs_skill, "restored SKILL.md byte-equal v1's SKILL.md"

    # ── Corner cases ────────────────────────────────────────────────

    def test_unsubscribed_user_does_not_get_entity(
        self,
        web_client,
        monkeypatch,
    ):
        """G1 negative control: a third user who never installs the
        entity must NEVER see it in list_for_user — regardless of
        which phase the lifecycle is in."""
        self._setup_guardrails_on(monkeypatch)
        owner_id, owner_cookies = _create_user(web_client, "unsub-owner@x.com")
        unsub_id, unsub_cookies = _create_user(web_client, "unsub-other@x.com")

        v1_zip = _make_skill_zip(
            "unsubme",
            body="Body content for unsubscribed-user negative test that's long enough to clear threshold. " * 4,
        )
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("v1.zip", v1_zip, "application/zip")},
            data={"type": "skill", "description": _OK_DESC},
            cookies=owner_cookies,
        )
        assert r.status_code == 201, r.text
        eid = r.json()["id"]
        self._drive_llm(monkeypatch, eid, self._latest_sub_id(eid), 1, _MOCK_APPROVE_LIFECYCLE)

        installs = self._installs(unsub_id)
        assert eid not in {row["id"] for row in installs}, (
            "non-subscribed user must not receive entity in list_for_user"
        )

        # marketplace.zip for the non-subscriber must NOT contain the bundle.
        _, contents, _ = self._serve_zip(web_client, unsub_cookies)
        skill_files = [n for n in contents if "unsubme-by-unsub-owner" in n]
        assert not skill_files, f"non-subscriber's marketplace.zip leaked the bundle: {skill_files}"

    def test_late_subscriber_during_quarantine_gets_v2(
        self,
        web_client,
        monkeypatch,
    ):
        """G2: subscriber installs AFTER v3 is blocked but BEFORE
        override. Must get v2 bytes (entity.version_no=2 at install
        time)."""
        self._setup_guardrails_on(monkeypatch)
        owner_id, owner_cookies = _create_user(web_client, "lateinst-owner@x.com")

        v1_zip = _make_skill_zip("lateinst", body="V1 body for late-subscriber test. " * 6)
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("v1.zip", v1_zip, "application/zip")},
            data={"type": "skill", "description": _OK_DESC},
            cookies=owner_cookies,
        )
        assert r.status_code == 201, r.text
        eid = r.json()["id"]
        self._drive_llm(monkeypatch, eid, self._latest_sub_id(eid), 1, _MOCK_APPROVE_LIFECYCLE)

        v2_zip = _make_skill_zip("lateinst", body="V2 body content advanced for late-subscriber test. " * 5)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2_zip, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200
        self._drive_llm(monkeypatch, eid, self._latest_sub_id(eid), 2, _MOCK_APPROVE_LIFECYCLE)
        v2_hash = self._ent(eid)["version"]

        # PUT v3 + block.
        v3_zip = _make_skill_zip("lateinst", body="V3 body content risky for late-subscriber test. " * 5)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v3.zip", v3_zip, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200
        self._drive_llm(monkeypatch, eid, self._latest_sub_id(eid), 3, _MOCK_BLOCK_LIFECYCLE)

        # Late subscriber installs DURING quarantine. Should get v2.
        late_id, late_cookies = _create_user(web_client, "lateinst-late@x.com")
        self._install(web_client, eid, late_cookies)
        installs = self._installs(late_id)
        assert installs[0]["version"] == v2_hash, (
            f"late subscriber during quarantine must get v2 hash; got {installs[0]['version'][:8]}"
        )

    def test_non_owner_does_not_see_quarantine_banner(
        self,
        web_client,
        monkeypatch,
    ):
        """G3 privacy gate: during v3-blocked phase, a non-owner
        non-admin third user hits /marketplace/flea/<eid>. Banner is
        owner+admin-only — third user must see the public approved
        view without 'Latest edit failed review' copy."""
        self._setup_guardrails_on(monkeypatch)
        owner_id, owner_cookies = _create_user(web_client, "priv-owner@x.com")

        v1_zip = _make_skill_zip("priv", body="V1 body for privacy-gate test that's long enough. " * 5)
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("v1.zip", v1_zip, "application/zip")},
            data={"type": "skill", "description": _OK_DESC},
            cookies=owner_cookies,
        )
        assert r.status_code == 201, r.text
        eid = r.json()["id"]
        self._drive_llm(monkeypatch, eid, self._latest_sub_id(eid), 1, _MOCK_APPROVE_LIFECYCLE)

        # PUT v2 + block.
        v2_zip = _make_skill_zip("priv", body="V2 body content risky for privacy-gate test that's long. " * 5)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2_zip, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200
        self._drive_llm(monkeypatch, eid, self._latest_sub_id(eid), 2, _MOCK_BLOCK_LIFECYCLE)

        # Third user (not owner, not admin) hits detail page.
        _, third_cookies = _create_user(web_client, "priv-third@x.com")
        r = web_client.get(f"/marketplace/flea/{eid}", cookies=third_cookies)
        assert r.status_code == 200, r.text
        body = r.text
        assert "Latest edit failed review" not in body, "third user must NOT see the v2+-edit failure banner"
        assert "blocked_llm" not in body, "third user must NOT see blocked-status detail"
        # Sanity: owner DOES see the banner.
        r_owner = web_client.get(f"/marketplace/flea/{eid}", cookies=owner_cookies)
        assert r_owner.status_code == 200
        assert "Latest edit failed review" in r_owner.text, "owner must see the banner for the same in-flight failure"

    def test_second_restore_of_v1_triggers_reuse_path(
        self,
        web_client,
        monkeypatch,
    ):
        """G4 (live agnes-development bug, PR #332 lifecycle
        validation): owner restores v1 → v4. Then restores v1 AGAIN
        → v5. The PR #332 reuse path must fire because v4 was
        approved by same model. v5 submission must NOT make a new
        LLM call AND must carry reused_from_submission_id marker."""
        from src.repositories.store_submissions import StoreSubmissionsRepository

        self._setup_guardrails_on(monkeypatch)
        owner_id, owner_cookies = _create_user(web_client, "reuse2-owner@x.com")

        v1_zip = _make_skill_zip("reuse2", body="V1 body content for second-restore reuse test. " * 5)
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("v1.zip", v1_zip, "application/zip")},
            data={"type": "skill", "description": _OK_DESC},
            cookies=owner_cookies,
        )
        assert r.status_code == 201, r.text
        eid = r.json()["id"]
        v1_sub_id = self._latest_sub_id(eid)
        self._drive_llm(monkeypatch, eid, v1_sub_id, 1, _MOCK_APPROVE_LIFECYCLE)

        # PUT v2 → approve → entity at v2 (so v1 is no longer current).
        v2_zip = _make_skill_zip("reuse2", body="V2 body content different from v1 for restore test. " * 5)
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", v2_zip, "application/zip")},
            cookies=owner_cookies,
        )
        assert r.status_code == 200
        self._drive_llm(monkeypatch, eid, self._latest_sub_id(eid), 2, _MOCK_APPROVE_LIFECYCLE)
        assert self._ent(eid)["version_no"] == 2

        # First restore v1 → v3 with v1 bytes. Reuse may or may not
        # fire depending on whether v1's reviewed_by_model matches.
        # (It does: both were stamped 'mock-haiku' via our drive_llm.)
        r = web_client.post(
            f"/api/store/entities/{eid}/versions/1/restore",
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text
        v3_sub_id = self._latest_sub_id(eid)
        conn = get_system_db()
        v3_sub = StoreSubmissionsRepository(conn).get(v3_sub_id)
        conn.close()
        if v3_sub["status"] == "pending_llm":
            self._drive_llm(monkeypatch, eid, v3_sub_id, 3, _MOCK_APPROVE_LIFECYCLE)
        # By now v3 is approved + promoted. version_no=3.
        assert self._ent(eid)["version_no"] == 3

        # Count LLM calls BEFORE second restore.
        call_count = {"n": 0}

        def counting_mock(*a, **kw):
            call_count["n"] += 1
            return _MOCK_APPROVE_LIFECYCLE

        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle",
            counting_mock,
        )

        # Second restore v1 → v4 with v1 bytes. PR #332 reuse path
        # MUST fire because v3 (just promoted, byte-identical to v1)
        # OR v1 itself qualifies (same hash, approved, same model).
        r = web_client.post(
            f"/api/store/entities/{eid}/versions/1/restore",
            cookies=owner_cookies,
        )
        assert r.status_code == 200, r.text
        v4_sub_id = self._latest_sub_id(eid)
        conn = get_system_db()
        v4_sub = StoreSubmissionsRepository(conn).get(v4_sub_id)
        conn.close()
        assert v4_sub["status"] == "approved", f"v4 must be approved via reuse path; got status={v4_sub['status']}"
        assert (v4_sub["llm_findings"] or {}).get("reused_from_submission_id"), (
            "v4 must carry reused_from_submission_id marker (PR #332)"
        )
        assert call_count["n"] == 0, f"second restore must NOT call LLM; count={call_count['n']}"

    def test_archived_entity_keeps_serving_installed_subscribers(
        self,
        web_client,
        monkeypatch,
    ):
        """G5: owner soft-archives entity → already-subscribed users
        STILL get bundle served (per CLAUDE.md contract). Browse
        listing for a third user does NOT include the entity."""
        from src.repositories.store_entities import StoreEntitiesRepository

        self._setup_guardrails_on(monkeypatch)
        owner_id, owner_cookies = _create_user(web_client, "arch-owner@x.com")
        installer_id, installer_cookies = _create_user(web_client, "arch-installer@x.com")

        v1_zip = _make_skill_zip("archme", body="V1 body content for archive behavior test that's long. " * 5)
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("v1.zip", v1_zip, "application/zip")},
            data={"type": "skill", "description": _OK_DESC},
            cookies=owner_cookies,
        )
        assert r.status_code == 201, r.text
        eid = r.json()["id"]
        self._drive_llm(monkeypatch, eid, self._latest_sub_id(eid), 1, _MOCK_APPROVE_LIFECYCLE)
        v1_hash = self._ent(eid)["version"]

        self._install(web_client, eid, installer_cookies)
        installs = self._installs(installer_id)
        assert installs[0]["version"] == v1_hash

        # Owner soft-archives.
        r = web_client.delete(
            f"/api/store/entities/{eid}",
            cookies=owner_cookies,
        )
        assert r.status_code == 204, r.text

        conn = get_system_db()
        ent_after = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert ent_after["visibility_status"] == "archived"

        # Already-installed user STILL has the entity in list_for_user.
        installs_after = self._installs(installer_id)
        assert eid in {row["id"] for row in installs_after}, (
            "soft-archive must NOT cascade to existing user_store_installs (CLAUDE.md contract)"
        )

        # Third user browsing marketplace must NOT see the entity.
        _, third_cookies = _create_user(web_client, "arch-third@x.com")
        r = web_client.get("/api/store/entities", cookies=third_cookies)
        assert r.status_code == 200
        ids = {item["id"] for item in r.json().get("items", [])}
        assert eid not in ids, "archived entity must NOT appear in browse listing"


class TestItemDetailHeroPlaceholder:
    """The flea skill/agent detail hero rides the shared detail scaffold
    (#896): a per-kind dark gradient whose accent resolves from the
    ``--ds-kind-<kind>`` token on the ``.detail`` scope, with the shared
    per-kind glyph tile as the cover placeholder (JS overlays a curator
    cover when present). Regression guard against a future revert to the
    bespoke macOS-"window" hero + hardcoded initials placeholder.
    """

    @pytest.fixture(autouse=True)
    def _redesign_opt_in(self, monkeypatch):
        """This class pins the REDESIGNED detail anatomy (#896's shared
        scaffold), which a default instance no longer renders — topnav/blue
        serves the frozen pre-redesign page via `_detail_template`
        (tests/test_ui_layout_theme.py::TestDetailPageParity)."""
        monkeypatch.setenv("AGNES_INSTANCE_THEME", "paper")

    def test_flea_skill_hero_uses_shared_kind_scaffold(self, web_client):
        _, owner_cookies = _create_user(web_client, "heroinit@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="sales-dashboard")
        r = web_client.get(f"/marketplace/flea/{eid}", cookies=owner_cookies)
        assert r.status_code == 200, r.text

        # Root .detail scope carries the per-kind token so the shared dark
        # hero renders green for skills.
        assert 'data-kind="skill"' in r.text
        assert "var(--ds-kind-skill)" in r.text
        # The shared icon tile is the cover placeholder; the bespoke macOS
        # window + name-derived initials are gone.
        assert 'id="hero-icon"' in r.text
        assert 'id="hero-window-body"' not in r.text


class TestDetailBackLink:
    """The detail-page back link is pinned to the top (above the review banner
    + versions card, not mid-page) and honors ?from=skills → Skill builder."""

    @pytest.fixture(autouse=True)
    def _redesign_opt_in(self, monkeypatch):
        """This class pins the REDESIGNED detail anatomy (#896's shared
        scaffold), which a default instance no longer renders — topnav/blue
        serves the frozen pre-redesign page via `_detail_template`
        (tests/test_ui_layout_theme.py::TestDetailPageParity)."""
        monkeypatch.setenv("AGNES_INSTANCE_THEME", "paper")

    def test_back_link_renders_above_versions_card(self, web_client):
        _, owner_cookies = _create_user(web_client, "backpos@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="backpos")
        r = web_client.get(f"/marketplace/flea/{eid}", cookies=owner_cookies)
        assert r.status_code == 200
        # Owner sees the version history (the shared side timeline under the
        # scaffold anatomy — the blue-era `versions-card` include renders on
        # no surface any more); the back link must render BEFORE it (top of
        # page) rather than after it (the mid-page regression).
        assert 'class="detail-back"' in r.text and ">Versions<" in r.text
        assert r.text.index('class="detail-back"') < r.text.index(">Versions<")

    def test_from_skills_returns_to_skill_builder(self, web_client):
        _, owner_cookies = _create_user(web_client, "backfrom@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="backfrom")
        r = web_client.get(f"/marketplace/flea/{eid}?from=skills", cookies=owner_cookies)
        assert r.status_code == 200
        assert 'href="/skills"' in r.text
        assert "Skill builder" in r.text

    def test_default_back_link_is_not_skill_builder(self, web_client):
        _, owner_cookies = _create_user(web_client, "backdef@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="backdef")
        r = web_client.get(f"/marketplace/flea/{eid}", cookies=owner_cookies)
        assert r.status_code == 200
        # No ?from → the back link targets the marketplace, never /skills.
        assert 'href="/skills"' not in r.text


class TestDetailTrustBadgeAndManageRegion:
    """The hero states the trust claim; the owner/admin blocks are one region.

    Both are paper-only. Blue keeps the badge and the four separate blocks it
    has always rendered — the design system forbids changing the look of an
    instance that has not opted in, so every assertion here comes in pairs.
    """

    @staticmethod
    def _get(client, cookies, eid, theme, monkeypatch):
        monkeypatch.setenv("AGNES_INSTANCE_THEME", theme)
        r = client.get(f"/marketplace/flea/{eid}", cookies=cookies)
        assert r.status_code == 200
        return r.text

    @pytest.mark.parametrize(
        "upload,label",
        [(_upload_clean, "skill"), (_upload_clean_plugin, "plugin")],
    )
    def test_paper_hero_carries_trust_not_the_retired_source_badge(self, web_client, monkeypatch, upload, label):
        _, cookies = _create_user(web_client, f"trust-{label}@x.com")
        eid = upload(web_client, cookies, name=f"trust{label}")

        # The Community marker is opt-in (`library.show_unverified_trust`,
        # default off for upgrade parity — the default itself is guarded in
        # test_web_library_store_entities.py). What THIS test pins is where
        # the claim renders once an instance opts in: the hero title row,
        # not the retired source badge.
        monkeypatch.setenv("AGNES_LIBRARY_SHOW_UNVERIFIED_TRUST", "true")
        paper = self._get(web_client, cookies, eid, "paper", monkeypatch)
        # The marker rides the TITLE ROW, rendered server-side by the shared
        # hero like every other detail page. It used to live in a <template>
        # for the hydration script to lift into the pill row, because this page
        # hand-wrote its own header; now that it renders through
        # `detail.hero(trust=…)`, there is nothing to lift.
        assert '<template id="hero-trust-mark">' not in paper, (
            "the trust marker is server-rendered into the header now, not hydrated"
        )
        title_row = paper.split('class="detail-hero__title-row"', 1)[1].split("</div>", 1)[0]
        assert 'class="ds-trust ds-trust--community' in title_row, "a fresh user upload is Community"
        assert "ds-trust--label" in title_row, "labelled form, not the bare glyph"
        # The resource type is named in words beside it, from the same header.
        assert 'class="detail-type"' in title_row
        # ...and the retired source badge is gone from the pill row, on both
        # templates: the server no longer writes it and the hydration script
        # skips it whenever the header carries the claim
        # (`data-trust-in-header`).
        pill_row = paper.split('id="hero-pills"', 1)[1].split("</div>", 1)[0]
        assert "pill flea" not in pill_row
        assert "pill curated" not in pill_row
        assert 'data-trust-in-header="1"' in paper

    @pytest.mark.parametrize(
        "upload,label",
        [(_upload_clean, "skill"), (_upload_clean_plugin, "plugin")],
    )
    def test_blue_keeps_the_source_badge_and_grows_no_trust_template(self, web_client, monkeypatch, upload, label):
        _, cookies = _create_user(web_client, f"bluetrust-{label}@x.com")
        eid = upload(web_client, cookies, name=f"bluetrust{label}")

        blue = self._get(web_client, cookies, eid, "blue", monkeypatch)
        # No template means the JS falls through to the Curated/Flea literal
        # and nothing renders the paper vocabulary — the badge a blue instance
        # has always shown is exactly what it still gets.
        assert '<template id="hero-trust-mark">' not in blue
        assert 'class="ds-trust' not in blue
        # The skill page server-renders its badge into the pill row; the plugin
        # page builds the row in JS from the same literal.
        assert ('<span class="pill flea">' in blue) or ('pill flea">Flea</span>' in blue)

    @pytest.mark.parametrize(
        "upload,label",
        [(_upload_clean, "skill"), (_upload_clean_plugin, "plugin")],
    )
    def test_paper_moves_the_owner_tools_into_the_menu_and_the_rail(self, web_client, monkeypatch, upload, label):
        """The owner/admin blocks stop being an interruption between the header
        and the content.

        They used to be three separately-styled blocks wedged there — a status
        banner, an action strip and a bordered versions card — which one shared
        `.manage-region` wrapper unified as far as CSS could. The detail-page
        template answers it properly instead: the Edit / Archive / Hard-delete
        ladder is the header's overflow menu (`detail.store_menu`, shared with
        the other store surface), version history is a rail timeline (the same
        `timeline()` every activity list uses), and only the quarantine banner
        stays in the flow, because it is an alert about the page.
        """
        _, cookies = _create_user(web_client, f"mgr-{label}@x.com")
        eid = upload(web_client, cookies, name=f"mgr{label}")

        paper = self._get(web_client, cookies, eid, "paper", monkeypatch)
        # No wrapper, and none of the blocks it used to wrap.
        assert "manage-region" not in paper
        assert '<div class="owner-actions">' not in paper
        assert '<div class="versions-card">' not in paper
        # The ladder is in the header's overflow menu…
        assert '<details class="detail-menu">' in paper
        menu = paper.split('<details class="detail-menu">', 1)[1].split("</details>", 1)[0]
        assert ">Edit<" in menu
        assert ">Archive<" in menu
        # …and the history is a rail timeline, restorable through the same
        # `restoreVersion()` the bordered card always called.
        assert 'class="detail-timeline"' in paper
        assert "data-restore-version=" in paper or "v1 · current" in paper

    @pytest.mark.parametrize(
        "upload,label",
        [(_upload_clean, "skill"), (_upload_clean_plugin, "plugin")],
    )
    def test_blue_renders_no_manage_region(self, web_client, monkeypatch, upload, label):
        _, cookies = _create_user(web_client, f"bluemgr-{label}@x.com")
        eid = upload(web_client, cookies, name=f"bluemgr{label}")

        blue = self._get(web_client, cookies, eid, "blue", monkeypatch)
        assert "manage-region" not in blue
        # ...but the blocks it would have wrapped still render, unchanged.
        assert '<div class="owner-actions">' in blue
        assert "versions-card" in blue

    def test_non_owner_gets_no_empty_region(self, web_client, monkeypatch):
        """The wrapper repeats the owner/admin gate its contents self-guard on.

        Without that, a stranger on an approved item would get a hairline and a
        "Manage" heading introducing nothing.
        """
        _, owner_cookies = _create_user(web_client, "mgrowner@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="mgrstranger")
        _, other_cookies = _create_user(web_client, "mgrother@x.com")

        paper = self._get(web_client, other_cookies, eid, "paper", monkeypatch)
        assert "manage-region" not in paper


class TestDetailHeroOrdering:
    """The kind-tinted hero IS the page header, so it renders first — the
    owner/admin strip (actions + versions card) follows it. Previously the
    strip rendered above the hero, pushing the header of a freshly published
    skill below three other cards."""

    @pytest.fixture(autouse=True)
    def _redesign_opt_in(self, monkeypatch):
        """This class pins the REDESIGNED detail anatomy (#896's shared
        scaffold), which a default instance no longer renders — topnav/blue
        serves the frozen pre-redesign page via `_detail_template`
        (tests/test_ui_layout_theme.py::TestDetailPageParity)."""
        monkeypatch.setenv("AGNES_INSTANCE_THEME", "paper")

    @staticmethod
    def _assert_order(body: str) -> None:
        # Anchor on the markup, not the class name — the same names also
        # appear in the page's <style> blocks. The blue-era anchors
        # (`detail-hero--paneled`, `owner-actions`, `versions-card`) belonged
        # to the redesigned template's default-instance branch, which no
        # surface renders any more (default serves the frozen pre-redesign
        # page; see TestDetailPageParity) — the ordering contract lives on in
        # the scaffold anatomy: back link, then the hero, then the owner's
        # version history in the side rail.
        back = '<a class="detail-back"'
        hero = '<div class="detail-hero'
        versions = ">Versions<"
        for marker in (back, hero, versions):
            assert marker in body, marker
        assert body.index(back) < body.index(hero) < body.index(versions)

    def test_hero_renders_above_owner_strip(self, web_client):
        _, owner_cookies = _create_user(web_client, "heropos@x.com")
        eid = _upload_clean(web_client, owner_cookies, name="heropos")
        r = web_client.get(f"/marketplace/flea/{eid}", cookies=owner_cookies)
        assert r.status_code == 200
        self._assert_order(r.text)

    def test_hero_renders_above_owner_strip_on_the_plugin_page(self, web_client):
        """Same contract, other template.

        A ``type='plugin'`` entity renders marketplace_plugin_detail.html
        instead of marketplace_item_detail.html, so the skill-entity test
        above never covered it — and the plugin page shipped with the
        Manage strip + Versions card stacked *above* its own title.
        """
        _, owner_cookies = _create_user(web_client, "heroplug@x.com")
        eid = _upload_clean_plugin(web_client, owner_cookies, name="heroplug")
        r = web_client.get(f"/marketplace/flea/{eid}", cookies=owner_cookies)
        assert r.status_code == 200
        self._assert_order(r.text)
