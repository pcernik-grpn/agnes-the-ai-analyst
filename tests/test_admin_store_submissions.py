"""Admin endpoints for the flea-market guardrail submissions surface.

Covers:
  * Listing — non-admin gets 403, admin gets the table
  * Override — flips status + entity visibility, writes audit row
  * Retry — re-queues a review_error / blocked_llm submission
  * Delete — wipes both submission row and entity bundle
  * Override edge: inline-blocked submissions without an entity_id
    cannot be overridden (refused with 409)
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
from src.repositories.users import UserRepository


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
        id=user_id, email=email, name=user_id, password_hash=ph.hash(password),
    )
    conn.close()
    r = client.post("/auth/token", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return user_id, {"access_token": r.json()["access_token"]}


def _create_admin(client, email="admin@x.com"):
    from tests.helpers.auth import grant_admin
    user_id, cookies = _create_user(client, email, password="AdminPass1!")
    conn = get_system_db()
    grant_admin(conn, user_id)
    conn.close()
    return user_id, cookies


def _make_skill_zip(skill_name: str = "probe") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            f"{skill_name}/SKILL.md",
            f"---\nname: {skill_name}\ndescription: Use when staging a clean reference bundle for admin-review pipeline tests\n---\n\n"
            + ("Body that is intentionally long enough to clear quality thresholds. " * 6),
        )
    return buf.getvalue()


def _make_eval_skill_zip(skill_name: str = "bad") -> bytes:
    """A skill with a bash-eval script — guaranteed to fail static_security."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            f"{skill_name}/SKILL.md",
            f"---\nname: {skill_name}\ndescription: Use when staging a bundle that intentionally trips static-security review checks\n---\n\n"
            + ("Body. " * 50),
        )
        zf.writestr(f"{skill_name}/run.sh", "#!/bin/sh\neval $1\n")
    return buf.getvalue()


def _seed_quarantined_entity(
    user_id: str,
    user_email: str,
    skill_name: str = "quarantined",
    description: str = "Description seeded for tests — long enough to pass content checks.",
    *,
    status: str = "blocked_llm",
    static_findings=None,
    llm_summary: str = "test stub finding",
):
    """Seed a hidden flea entity + matching submission row + on-disk
    bundle, mimicking the post-LLM-review-blocked state.

    Inline failures (manifest, static-security, content) are now
    hard-rejected upstream and never create DB rows. Tests that
    previously triggered the v30 ``submission_blocked`` path by
    uploading a bad bundle must seed the quarantined state directly
    via this helper. The default status is ``blocked_llm`` — the only
    status path that still creates a hidden+pending entity.

    Returns ``(entity_id, submission_id)``.
    """
    from src.repositories.store_entities import StoreEntitiesRepository
    from src.repositories.store_submissions import StoreSubmissionsRepository
    from src.store_naming import suffixed_name
    import uuid as _uuid

    entity_id = _uuid.uuid4().hex
    username = user_email.split("@")[0]

    store_dir = get_store_dir()
    entity_dir = store_dir / entity_id
    plugin_root = entity_dir / "plugin"
    skill_subdir = plugin_root / "skills" / suffixed_name(skill_name, username)
    skill_subdir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_subdir / "SKILL.md"
    skill_md.write_text(
        f"---\nname: {suffixed_name(skill_name, username)}\n"
        f"description: {description}\n---\n\n"
        + ("Body content. " * 30),
    )
    run_sh = skill_subdir / "run.sh"
    run_sh.write_text("#!/bin/sh\neval $1\n")
    # v1 seed dir so the version download / restore endpoints find it.
    v1_plugin = entity_dir / "versions" / "v1" / "plugin"
    v1_plugin.parent.mkdir(parents=True, exist_ok=True)
    import shutil as _shutil
    _shutil.copytree(plugin_root, v1_plugin)

    # Mirror the InlineResult.to_response_dict() shape that the runner
    # would have produced. Static findings are surfaced verbatim in the
    # quarantine banner template (_quarantine_banner.html).
    findings = static_findings if static_findings is not None else [
        {"file": "run.sh", "line": 2, "category": "code_exec",
         "severity": "high",
         "reason": "shell eval expanding a variable",
         "snippet": "eval $1"},
    ]
    inline_checks = {
        "manifest": {"status": "pass", "issues": []},
        "static_security": {"status": "fail", "findings": findings},
        "content": {"status": "pass", "issues": []},
        "quality": {"status": "pass", "issues": []},
    }

    conn = get_system_db()
    StoreEntitiesRepository(conn).create(
        id=entity_id,
        owner_user_id=user_id,
        owner_username=username,
        type="skill",
        name=skill_name,
        description=description,
        category=None,
        version="1.0.0",
        file_size=512,
        visibility_status="hidden",
    )
    sub_id = StoreSubmissionsRepository(conn).create(
        submitter_id=user_id,
        submitter_email=user_email,
        type="skill",
        name=skill_name,
        version="1.0.0",
        status=status,
        entity_id=entity_id,
        inline_checks=inline_checks,
        llm_findings={"risk_level": "high", "summary": llm_summary,
                      "findings": findings},
        file_size=512,
        bundle_sha256="0" * 64,
    )
    conn.close()
    return entity_id, sub_id


# ---------------------------------------------------------------------------
# /api/admin/store/submissions — listing
# ---------------------------------------------------------------------------


class TestAdminListing:
    def test_non_admin_forbidden(self, web_client):
        _, user_cookies = _create_user(web_client, "user@x.com")
        r = web_client.get("/api/admin/store/submissions", cookies=user_cookies)
        assert r.status_code == 403

    def test_security_upload_creates_no_submission_row(self, web_client):
        """Static-security findings are hard-rejected — no submission row,
        no entity row, no bundle on disk. Replaces the v30 contract
        where inline failures landed in admin's queue at
        ``blocked_inline``.
        """
        from src.repositories.store_entities import StoreEntitiesRepository
        from src.repositories.store_submissions import StoreSubmissionsRepository

        user_id, user_cookies = _create_user(web_client, "u@x.com")
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_eval_skill_zip("bad"), "application/zip")},
            data={"type": "skill"}, cookies=user_cookies,
        )
        assert c.status_code == 422
        detail = c.json()["detail"]
        assert detail["code"] == "security_blocked"
        # Findings are exposed inline so the wizard banner can render them.
        assert detail["checks"]["static_security"]["status"] == "fail"
        assert detail["checks"]["static_security"]["findings"]
        # No DB rows, no quarantined entity for the submitter to inspect.
        assert "submission_id" not in detail
        assert "entity_id" not in detail

        conn = get_system_db()
        items, _total = StoreSubmissionsRepository(conn).list_for_admin(
            submitter_id=user_id,
        )
        assert items == []
        ent_items, _ = StoreEntitiesRepository(conn).list(owner_user_id=user_id)
        assert ent_items == []
        conn.close()

        # Admin queue is empty: no row was ever created.
        _, admin_cookies = _create_admin(web_client)
        r = web_client.get(
            "/api/admin/store/submissions", cookies=admin_cookies,
        )
        assert r.status_code == 200
        items = r.json()["items"]
        assert not any(s["submitter_id"] == user_id for s in items), (
            "security_blocked upload must not surface in admin queue"
        )

    def test_security_upload_emits_audit_log_entry(self, web_client):
        """A static-security rejection writes one ``store.upload.security_blocked``
        audit_log row carrying the findings + sha256 + size. That row is
        the *only* trace of the attempt; admin can grep audit_log for
        repeated offenders.
        """
        from src.repositories.audit import AuditRepository

        user_id, user_cookies = _create_user(web_client, "spammer@x.com")
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_eval_skill_zip("audit"), "application/zip")},
            data={"type": "skill"}, cookies=user_cookies,
        )
        assert c.status_code == 422
        assert c.json()["detail"]["code"] == "security_blocked"

        conn = get_system_db()
        rows, _cursor = AuditRepository(conn).query(
            user_id=user_id, action="store.upload.security_blocked",
            limit=10,
        )
        conn.close()
        assert len(rows) == 1
        params = rows[0].get("params") or {}
        if isinstance(params, str):
            params = json.loads(params)
        assert params.get("finding_count", 0) >= 1
        assert params.get("bundle_sha256")
        assert params.get("submitter_email") == "spammer@x.com"

    def test_inline_validation_returns_validation_failed_code(self, web_client):
        """A bundle that survives pre-bake but fails ``content_check``
        (description too short) goes through ``_reject_inline_or_continue``
        and is rejected with the new two-tier response: 422,
        ``detail.code == 'validation_failed'``, populated
        ``detail.checks`` shape, NO submission row, NO entity row,
        and NO audit_log entry (validation-tier failures are
        operator-fixable, not forensically interesting).

        Distinct from ``test_validation_failure_creates_no_audit_trail``
        below which exercises the pre-bake ``zip_missing_skill_md`` path
        — that one fails before inline checks ever run.
        """
        from src.repositories.audit import AuditRepository
        from src.repositories.store_submissions import StoreSubmissionsRepository

        # Valid skill layout — pre-bake parses frontmatter, layout
        # check passes. But description is < 60 chars → content_check
        # fires inside run_inline_checks → _reject_inline_or_continue
        # returns validation_failed.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(
                "tiny/SKILL.md",
                "---\nname: tiny\ndescription: too short\n---\n\n"
                + ("Body text. " * 50),
            )
        short_desc_zip = buf.getvalue()

        user_id, user_cookies = _create_user(web_client, "shortdesc@x.com")
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", short_desc_zip, "application/zip")},
            data={"type": "skill"}, cookies=user_cookies,
        )
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        assert detail["code"] == "validation_failed", detail
        # Frontend wizard (humanizeError in store_upload.html) reads
        # detail.checks.{manifest,content,quality} — lock the shape.
        assert set(detail["checks"].keys()) == {"manifest", "content", "quality"}
        assert detail["checks"]["content"]["status"] != "pass"

        # Validation-tier failures must not produce DB rows or audit entries.
        conn = get_system_db()
        items, _total = StoreSubmissionsRepository(conn).list_for_admin(
            submitter_id=user_id,
        )
        assert items == []
        rows, _cursor = AuditRepository(conn).query(
            user_id=user_id, action_prefix="store.upload.", limit=10,
        )
        conn.close()
        assert rows == [], (
            "validation-tier rejection must not write audit_log entries"
        )

    def test_validation_failure_creates_no_audit_trail(self, web_client):
        """A bundle that fails manifest validation (missing SKILL.md) is
        a fixable user error — no submission row, no entity row, and
        NO audit_log entry. The submitter just sees the wizard banner.
        """
        from src.repositories.audit import AuditRepository
        from src.repositories.store_submissions import StoreSubmissionsRepository

        # Skill ZIP without the required SKILL.md — manifest_check fails.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("broken/notes.md", "no manifest here\n")
        bad_zip = buf.getvalue()

        user_id, user_cookies = _create_user(web_client, "validation@x.com")
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", bad_zip, "application/zip")},
            data={"type": "skill"}, cookies=user_cookies,
        )
        assert c.status_code == 422
        # zip_missing_skill_md fires at metadata-extract (pre-bake), so
        # the response is a plain ``detail: "zip_missing_skill_md"`` —
        # but the contract under test is "no DB rows, no audit trail",
        # which is what we assert below.
        conn = get_system_db()
        items, _total = StoreSubmissionsRepository(conn).list_for_admin(
            submitter_id=user_id,
        )
        assert items == []
        rows, _cursor = AuditRepository(conn).query(
            user_id=user_id, action_prefix="store.upload.", limit=10,
        )
        conn.close()
        assert rows == [], (
            "validation-tier rejection must not write audit_log entries"
        )


# ---------------------------------------------------------------------------
# Override
# ---------------------------------------------------------------------------


class TestAdminOverride:
    def test_override_blocked_llm_publishes_entity(self, web_client):
        """Manually stage a blocked_llm row + entity, then override — the
        entity must flip to visibility_status='approved' and the
        submission to 'overridden'."""
        from src.repositories.store_entities import StoreEntitiesRepository
        from src.repositories.store_submissions import StoreSubmissionsRepository

        user_id, _ = _create_user(web_client, "submitter@x.com")
        conn = get_system_db()
        ents = StoreEntitiesRepository(conn)
        ents.create(
            id="ent-blk", owner_user_id=user_id, owner_username="submitter",
            type="skill", name="blocked-thing", description="x" * 30,
            category=None, version="1.0.0", file_size=10,
            visibility_status="pending",
        )
        subs = StoreSubmissionsRepository(conn)
        sid = subs.create(
            submitter_id=user_id, submitter_email="submitter@x.com",
            type="skill", name="blocked-thing", version="1.0.0",
            status="blocked_llm", entity_id="ent-blk",
            llm_findings={"risk_level": "high", "summary": "exfil"},
        )
        conn.close()

        _, admin_cookies = _create_admin(web_client)
        r = web_client.post(
            f"/api/admin/store/submissions/{sid}/override",
            json={"reason": "false positive — internal-only constants"},
            cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text

        conn = get_system_db()
        ent = StoreEntitiesRepository(conn).get("ent-blk")
        assert ent["visibility_status"] == "approved"
        sub = StoreSubmissionsRepository(conn).get(sid)
        assert sub["status"] == "overridden"
        assert sub["override_reason"].startswith("false positive")
        conn.close()

    def test_override_v2_edit_promotes_to_current(self, web_client, monkeypatch):
        """When an admin overrides a v2+ edit/restore submission, the
        entity must be promoted to that version — same end state as
        an LLM auto-approval. Pre-fix the override only flipped
        visibility, leaving entity.version_no at the prior approved
        version + live bundle bytes unchanged. Installers kept getting
        the old version."""
        from pathlib import Path
        from app.utils import get_store_dir
        from src.repositories.store_entities import StoreEntitiesRepository
        from src.repositories.store_submissions import StoreSubmissionsRepository
        from src.store_guardrails.runner import run_llm_review

        user_id, user_cookies = _create_user(web_client, "v2over@x.com")

        # Phase 1: clean v1 upload (guardrails off by default in tests) →
        # entity approved at version_no=1.
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("v2over"), "application/zip")},
            data={"type": "skill",
                  "description": (
                      "Use when verifying admin override on a v2+ edit "
                      "promotes the entity to the overridden version "
                      "across the deferred-promotion path."
                  )},
            cookies=user_cookies,
        )
        assert r.status_code == 201, r.text
        eid = r.json()["id"]

        # Phase 2: flip guardrails on (LLM mocked to BLOCK), PUT v2.
        def mock_block(*args, **kwargs):
            return {
                "risk_level": "high",
                "summary": "mock block",
                "findings": [{"severity": "high", "category": "test",
                              "file": "x", "explanation": "mock"}],
                "template_placeholders_found": 0,
                "reviewed_by_model": "mock-model", "error": None,
            }
        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle", mock_block,
        )
        monkeypatch.setattr(
            "app.api.store.get_guardrails_enabled", lambda: True,
        )
        monkeypatch.setattr(
            "app.api.store.get_guardrails_llm_provider_ready", lambda: True,
        )

        # Build a v2 zip inline (slightly different body so the hash diverges).
        import io as _io
        import zipfile as _zip
        buf = _io.BytesIO()
        with _zip.ZipFile(buf, "w") as zf:
            zf.writestr(
                "v2over/SKILL.md",
                "---\nname: v2over\ndescription: "
                "Use when verifying admin override v2 promote behaviour after edit\n---\n\n"
                + ("V2 BODY text that is intentionally different from v1. " * 8),
            )
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", buf.getvalue(), "application/zip")},
            cookies=user_cookies,
        )
        assert r.status_code == 200, r.text

        # Drive the BG review synchronously so v2 lands at blocked_llm.
        conn = get_system_db()
        v2_sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()
        run_llm_review(
            v2_sub_id,
            plugin_dir=Path(get_store_dir()) / eid / "versions" / "v2" / "plugin",
            conn_factory=get_system_db,
            api_key_loader=lambda: "sk-test",
            model_loader=lambda: "mock-model",
        )

        conn = get_system_db()
        ent_before = StoreEntitiesRepository(conn).get(eid)
        sub_before = StoreSubmissionsRepository(conn).get(v2_sub_id)
        conn.close()
        # Pre-condition: blocked at v2, entity stayed at v1.
        assert sub_before["status"] == "blocked_llm"
        assert ent_before["version_no"] == 1
        v1_hash = ent_before["version"]

        # Phase 3: admin overrides v2. Entity must promote to v2 and
        # the on-disk live bundle must reflect v2's bytes.
        _, admin_cookies = _create_admin(web_client)
        r = web_client.post(
            f"/api/admin/store/submissions/{v2_sub_id}/override",
            json={"reason": "false positive — verified clean offline"},
            cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text

        conn = get_system_db()
        ent_after = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert ent_after["version_no"] == 2, (
            f"override must promote entity to v2; got version_no={ent_after['version_no']}"
        )
        assert ent_after["version"] != v1_hash, (
            "entity.version (hash) must move to v2 — stayed at v1 hash"
        )
        # Live plugin/ dir must hold v2's bytes (compare to v2 source dir).
        v2_plugin = Path(get_store_dir()) / eid / "versions" / "v2" / "plugin"
        live_plugin = Path(get_store_dir()) / eid / "plugin"
        v2_files = sorted(p.name for p in v2_plugin.rglob("*") if p.is_file())
        live_files = sorted(p.name for p in live_plugin.rglob("*") if p.is_file())
        assert v2_files == live_files, (
            f"live plugin/ must mirror v2 dir after override; "
            f"v2_files={v2_files} live_files={live_files}"
        )

    def test_override_v1_initial_upload_no_promote(self, web_client):
        """Override on an initial v1 (no prior approved version) must
        still work: entity already at version_no=1, no promotion needed.
        Regression guard so the v2+ promote logic doesn't break v1
        overrides (the audit log used to be the only signal here)."""
        from src.repositories.store_entities import StoreEntitiesRepository
        from src.repositories.store_submissions import StoreSubmissionsRepository

        user_id, _ = _create_user(web_client, "v1over@x.com")
        conn = get_system_db()
        ents = StoreEntitiesRepository(conn)
        ents.create(
            id="ent-v1-over", owner_user_id=user_id, owner_username="v1over",
            type="skill", name="v1-blocked", description="x" * 40,
            category=None, version="aaaaaaaaaaaaaaaa", file_size=10,
            visibility_status="pending",
        )
        subs = StoreSubmissionsRepository(conn)
        sid = subs.create(
            submitter_id=user_id, submitter_email="v1over@x.com",
            type="skill", name="v1-blocked", version="aaaaaaaaaaaaaaaa",
            status="blocked_llm", entity_id="ent-v1-over",
            llm_findings={"risk_level": "high", "summary": "x"},
        )
        # Backfill the v1 history entry submission_id so the promote
        # loop has a target to find.
        ents.update_history_submission_id("ent-v1-over", 1, sid)
        conn.close()

        _, admin_cookies = _create_admin(web_client)
        r = web_client.post(
            f"/api/admin/store/submissions/{sid}/override",
            json={"reason": "false positive — internal-only constants"},
            cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text

        conn = get_system_db()
        ent = StoreEntitiesRepository(conn).get("ent-v1-over")
        sub = StoreSubmissionsRepository(conn).get(sid)
        conn.close()
        assert ent["visibility_status"] == "approved"
        assert ent["version_no"] == 1, (
            "v1 override must NOT trigger phantom promotion"
        )
        assert sub["status"] == "overridden"

    def test_override_short_reason_rejected(self, web_client):
        from src.repositories.store_entities import StoreEntitiesRepository
        from src.repositories.store_submissions import StoreSubmissionsRepository

        user_id, _ = _create_user(web_client, "u@x.com")
        conn = get_system_db()
        StoreEntitiesRepository(conn).create(
            id="e1", owner_user_id=user_id, owner_username="u",
            type="skill", name="x", description="x" * 30, category=None,
            version="1.0.0", file_size=10, visibility_status="pending",
        )
        sid = StoreSubmissionsRepository(conn).create(
            submitter_id=user_id, submitter_email="u@x.com",
            type="skill", name="x", version="1.0.0",
            status="blocked_llm", entity_id="e1",
        )
        conn.close()

        _, admin_cookies = _create_admin(web_client)
        r = web_client.post(
            f"/api/admin/store/submissions/{sid}/override",
            json={"reason": "ok"},  # 2 chars < min_length=4
            cookies=admin_cookies,
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestAdminDelete:
    def test_delete_clears_submission_and_entity(self, web_client):
        from src.repositories.store_entities import StoreEntitiesRepository
        from src.repositories.store_submissions import StoreSubmissionsRepository

        user_id, _ = _create_user(web_client, "u@x.com")
        conn = get_system_db()
        StoreEntitiesRepository(conn).create(
            id="e2", owner_user_id=user_id, owner_username="u",
            type="skill", name="y", description="x" * 30, category=None,
            version="1.0.0", file_size=10, visibility_status="approved",
        )
        sid = StoreSubmissionsRepository(conn).create(
            submitter_id=user_id, submitter_email="u@x.com",
            type="skill", name="y", version="1.0.0",
            status="approved", entity_id="e2",
        )
        conn.close()

        _, admin_cookies = _create_admin(web_client)
        r = web_client.delete(
            f"/api/admin/store/submissions/{sid}",
            cookies=admin_cookies,
        )
        assert r.status_code == 204, r.text

        conn = get_system_db()
        assert StoreEntitiesRepository(conn).get("e2") is None
        assert StoreSubmissionsRepository(conn).get(sid) is None
        conn.close()


# ---------------------------------------------------------------------------
# List filters + pagination
# ---------------------------------------------------------------------------


def _seed_submissions(submitters):
    """Stage rows directly through the repo. Returns list of submission ids
    in insertion order so tests can reference rows."""
    from src.repositories.store_submissions import StoreSubmissionsRepository
    conn = get_system_db()
    ids = []
    subs = StoreSubmissionsRepository(conn)
    for u_id, u_email, type_, name, version, status in submitters:
        ids.append(subs.create(
            submitter_id=u_id, submitter_email=u_email,
            type=type_, name=name, version=version, status=status,
            entity_id=f"ent-{u_id}-{name}",
        ))
    conn.close()
    return ids


class TestAdminListFilters:
    def test_filter_by_submitter(self, web_client):
        u1, _ = _create_user(web_client, "alice@x.com")
        u2, _ = _create_user(web_client, "bob@x.com")
        _seed_submissions([
            (u1, "alice@x.com", "skill", "thing", "1.0", "approved"),
            (u1, "alice@x.com", "skill", "other", "1.0", "blocked_llm"),
            (u2, "bob@x.com",   "skill", "third", "1.0", "approved"),
        ])
        _, admin_cookies = _create_admin(web_client)

        r = web_client.get(f"/api/admin/store/submissions?submitter={u1}", cookies=admin_cookies)
        assert r.status_code == 200
        assert r.json()["total"] == 2

    def test_filter_by_type(self, web_client):
        u, _ = _create_user(web_client, "u@x.com")
        _seed_submissions([
            (u, "u@x.com", "skill",  "a", "1.0", "approved"),
            (u, "u@x.com", "agent",  "b", "1.0", "approved"),
            (u, "u@x.com", "plugin", "c", "1.0", "approved"),
        ])
        _, admin_cookies = _create_admin(web_client)

        r = web_client.get("/api/admin/store/submissions?type=agent", cookies=admin_cookies)
        assert r.status_code == 200
        assert r.json()["total"] == 1
        assert r.json()["items"][0]["type"] == "agent"

    def test_invalid_type_400(self, web_client):
        _, admin_cookies = _create_admin(web_client)
        r = web_client.get("/api/admin/store/submissions?type=bogus", cookies=admin_cookies)
        assert r.status_code == 400

    def test_filter_by_name_substring(self, web_client):
        u, _ = _create_user(web_client, "u@x.com")
        _seed_submissions([
            (u, "u@x.com", "skill", "summarizer-pro",   "1.0", "approved"),
            (u, "u@x.com", "skill", "summarizer-alpha", "1.0", "approved"),
            (u, "u@x.com", "skill", "totally-other",    "1.0", "approved"),
        ])
        _, admin_cookies = _create_admin(web_client)

        r = web_client.get("/api/admin/store/submissions?name=summarizer", cookies=admin_cookies)
        assert r.status_code == 200
        assert r.json()["total"] == 2

    def test_filter_by_version_substring(self, web_client):
        u, _ = _create_user(web_client, "u@x.com")
        _seed_submissions([
            (u, "u@x.com", "skill", "a", "1.0.0", "approved"),
            (u, "u@x.com", "skill", "b", "1.0.1", "approved"),
            (u, "u@x.com", "skill", "c", "2.0.0", "approved"),
        ])
        _, admin_cookies = _create_admin(web_client)

        r = web_client.get("/api/admin/store/submissions?version=1.0", cookies=admin_cookies)
        assert r.status_code == 200
        assert r.json()["total"] == 2

    def test_combined_filters(self, web_client):
        u1, _ = _create_user(web_client, "alice@x.com")
        u2, _ = _create_user(web_client, "bob@x.com")
        _seed_submissions([
            (u1, "alice@x.com", "skill", "x", "1.0", "blocked_llm"),
            (u1, "alice@x.com", "agent", "y", "1.0", "blocked_llm"),
            (u2, "bob@x.com",   "skill", "z", "1.0", "blocked_llm"),
        ])
        _, admin_cookies = _create_admin(web_client)

        r = web_client.get(
            f"/api/admin/store/submissions?submitter={u1}&type=skill",
            cookies=admin_cookies,
        )
        assert r.json()["total"] == 1


class TestAdminListPagination:
    def test_skip_limit_and_total(self, web_client):
        u, _ = _create_user(web_client, "u@x.com")
        _seed_submissions([
            (u, "u@x.com", "skill", f"thing-{i}", "1.0", "approved")
            for i in range(7)
        ])
        _, admin_cookies = _create_admin(web_client)

        r = web_client.get("/api/admin/store/submissions?limit=3&skip=0", cookies=admin_cookies)
        b = r.json()
        assert b["total"] == 7 and len(b["items"]) == 3

        r = web_client.get("/api/admin/store/submissions?limit=3&skip=3", cookies=admin_cookies)
        b = r.json()
        assert b["total"] == 7 and len(b["items"]) == 3

        r = web_client.get("/api/admin/store/submissions?limit=3&skip=6", cookies=admin_cookies)
        b = r.json()
        assert b["total"] == 7 and len(b["items"]) == 1

    def test_limit_clamped(self, web_client):
        _, admin_cookies = _create_admin(web_client)
        r = web_client.get("/api/admin/store/submissions?limit=999999", cookies=admin_cookies)
        # Endpoint clamps to 500. No assertion on content; smoke test.
        assert r.status_code == 200
        assert r.json()["limit"] == 500


# ---------------------------------------------------------------------------
# Detail page
# ---------------------------------------------------------------------------


class TestAdminDetailPage:
    def test_detail_renders_for_existing_submission(self, web_client):
        u, _ = _create_user(web_client, "u@x.com")
        ids = _seed_submissions([
            (u, "u@x.com", "skill", "thing", "1.0", "approved"),
        ])
        _, admin_cookies = _create_admin(web_client)
        r = web_client.get(f"/admin/store/submissions/{ids[0]}", cookies=admin_cookies)
        assert r.status_code == 200
        body = r.text
        assert ids[0] in body
        assert "Back to all submissions" in body

    def test_detail_404_on_missing(self, web_client):
        _, admin_cookies = _create_admin(web_client)
        r = web_client.get("/admin/store/submissions/does-not-exist", cookies=admin_cookies)
        assert r.status_code == 404

    def test_detail_non_admin_forbidden(self, web_client):
        u, user_cookies = _create_user(web_client, "u@x.com")
        ids = _seed_submissions([
            (u, "u@x.com", "skill", "thing", "1.0", "approved"),
        ])
        r = web_client.get(f"/admin/store/submissions/{ids[0]}", cookies=user_cookies)
        assert r.status_code in (302, 401, 403)


# ---------------------------------------------------------------------------
# Rescan
# ---------------------------------------------------------------------------


def _stage_entity_with_bundle(tmp_root, owner_id, name, body=None):
    """Create a real on-disk plugin tree under DATA_DIR/store/<entity_id>/plugin
    so the rescan endpoint sees a bundle to scan."""
    from pathlib import Path
    from src.repositories.store_entities import StoreEntitiesRepository
    import uuid
    entity_id = uuid.uuid4().hex
    plugin_dir = Path(tmp_root) / "store" / entity_id / "plugin" / "skills" / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "SKILL.md").write_text(
        body or (
            "---\nname: " + name
            + "\ndescription: Use when staging a reference clean bundle so the admin rescan flow can re-run inline checks against it\n---\n\n"
            + ("Long body to satisfy quality and content guardrail thresholds. " * 8)
        ),
        encoding="utf-8",
    )
    conn = get_system_db()
    StoreEntitiesRepository(conn).create(
        id=entity_id, owner_user_id=owner_id, owner_username=owner_id,
        type="skill", name=name,
        description="Use when staging an entity row so admin rescan can re-evaluate the on-disk bundle against the inline guardrail tier",
        category=None,
        version="1.0.0", file_size=10, visibility_status="approved",
    )
    conn.close()
    return entity_id


class TestAdminRescan:
    def test_rescan_clean_bundle_pending_llm(self, web_client, tmp_path):
        from src.repositories.store_submissions import StoreSubmissionsRepository
        u, _ = _create_user(web_client, "u@x.com")
        eid = _stage_entity_with_bundle(tmp_path, u, "rescan-clean")
        conn = get_system_db()
        sid = StoreSubmissionsRepository(conn).create(
            submitter_id=u, submitter_email="u@x.com",
            type="skill", name="rescan-clean", version="1.0.0",
            status="approved", entity_id=eid,
        )
        conn.close()

        _, admin_cookies = _create_admin(web_client)
        r = web_client.post(
            f"/api/admin/store/submissions/{sid}/rescan", cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text
        # No ANTHROPIC_API_KEY in test env → guardrails disabled → auto-approved.
        assert r.json()["status"] in {"pending_llm", "approved"}

        conn = get_system_db()
        sub = StoreSubmissionsRepository(conn).get(sid)
        assert "retry_count" not in sub  # v34: column dropped
        conn.close()

    def test_rescan_dirty_bundle_blocks_inline(self, web_client, tmp_path):
        """A bundle that introduces a static-security violation since the
        original review must rescan to blocked_inline."""
        from src.repositories.store_entities import StoreEntitiesRepository
        from src.repositories.store_submissions import StoreSubmissionsRepository
        from pathlib import Path
        u, _ = _create_user(web_client, "u@x.com")
        eid = _stage_entity_with_bundle(tmp_path, u, "rescan-dirty")
        # Inject a bash-eval script — re-rescan must catch it.
        bad = Path(tmp_path) / "store" / eid / "plugin" / "skills" / "rescan-dirty" / "run.sh"
        bad.write_text("#!/bin/sh\neval $1\n", encoding="utf-8")

        conn = get_system_db()
        sid = StoreSubmissionsRepository(conn).create(
            submitter_id=u, submitter_email="u@x.com",
            type="skill", name="rescan-dirty", version="1.0.0",
            status="approved", entity_id=eid,
        )
        conn.close()

        _, admin_cookies = _create_admin(web_client)
        r = web_client.post(
            f"/api/admin/store/submissions/{sid}/rescan", cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "blocked_inline"

        conn = get_system_db()
        ent = StoreEntitiesRepository(conn).get(eid)
        assert ent["visibility_status"] == "hidden"
        sub = StoreSubmissionsRepository(conn).get(sid)
        assert sub["status"] == "blocked_inline"
        # Static-security finding from the new bash-eval is captured.
        ic = sub["inline_checks"]
        assert ic["static_security"]["status"] == "fail"
        assert any(f["category"] == "code_exec" for f in ic["static_security"]["findings"])
        conn.close()

    def test_rescan_without_entity_409(self, web_client):
        from src.repositories.store_submissions import StoreSubmissionsRepository
        u, _ = _create_user(web_client, "u@x.com")
        conn = get_system_db()
        sid = StoreSubmissionsRepository(conn).create(
            submitter_id=u, submitter_email="u@x.com",
            type="skill", name="x", version="1.0.0",
            status="blocked_inline", entity_id=None,
        )
        conn.close()
        _, admin_cookies = _create_admin(web_client)
        r = web_client.post(
            f"/api/admin/store/submissions/{sid}/rescan", cookies=admin_cookies,
        )
        assert r.status_code == 409

    def test_rescan_missing_bundle_410(self, web_client):
        from src.repositories.store_submissions import StoreSubmissionsRepository
        u, _ = _create_user(web_client, "u@x.com")
        conn = get_system_db()
        sid = StoreSubmissionsRepository(conn).create(
            submitter_id=u, submitter_email="u@x.com",
            type="skill", name="x", version="1.0.0",
            status="approved", entity_id="missing-eid",
        )
        conn.close()
        _, admin_cookies = _create_admin(web_client)
        r = web_client.post(
            f"/api/admin/store/submissions/{sid}/rescan", cookies=admin_cookies,
        )
        assert r.status_code == 410

    def test_rescan_non_admin_forbidden(self, web_client):
        from src.repositories.store_submissions import StoreSubmissionsRepository
        u, user_cookies = _create_user(web_client, "u@x.com")
        conn = get_system_db()
        sid = StoreSubmissionsRepository(conn).create(
            submitter_id=u, submitter_email="u@x.com",
            type="skill", name="x", version="1.0.0", status="approved",
        )
        conn.close()
        r = web_client.post(
            f"/api/admin/store/submissions/{sid}/rescan", cookies=user_cookies,
        )
        assert r.status_code == 403


class TestAdminRetryReviewsStagedBundle:
    """Codex adversarial finding [CRITICAL C1]: admin retry was passing
    live `plugin/` to the LLM. For a v2+ pending_llm / blocked_llm /
    review_error submission, live still holds the prior approved
    version. A retry would review the WRONG bytes and the runner's
    hash-match promotion would then advance the entity to staged
    bytes that were never reviewed. Fixed by resolving the staged
    `versions/v<N>/plugin/` from the submission's version_history
    entry."""

    def test_retry_v2_blocked_passes_staged_dir_not_live(
        self, web_client, monkeypatch,
    ):
        from pathlib import Path
        from app.utils import get_store_dir
        from src.repositories.store_entities import StoreEntitiesRepository
        from src.repositories.store_submissions import StoreSubmissionsRepository

        # v1 clean upload (guardrails off by default in tests).
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-for-retry-test")
        user_id, user_cookies = _create_user(web_client, "retrystaged@x.com")
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("retrystaged"), "application/zip")},
            data={"type": "skill",
                  "description": (
                      "Use when verifying retry reads staged version "
                      "bundle bytes not live for v2 blocked submissions"
                  )},
            cookies=user_cookies,
        )
        assert r.status_code == 201, r.text
        eid = r.json()["id"]

        # v2 PUT with guardrails on, LLM mocked to BLOCK → blocked_llm.
        def mock_block(*a, **kw):
            return {
                "risk_level": "high", "summary": "mock block",
                "findings": [{"severity": "high", "category": "test",
                              "file": "x", "explanation": "mock"}],
                "template_placeholders_found": 0,
                "reviewed_by_model": "mock", "error": None,
            }
        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle", mock_block,
        )
        monkeypatch.setattr(
            "app.api.store.get_guardrails_enabled", lambda: True,
        )
        monkeypatch.setattr(
            "app.api.store.get_guardrails_llm_provider_ready", lambda: True,
        )
        import io as _io
        import zipfile as _zip
        buf = _io.BytesIO()
        with _zip.ZipFile(buf, "w") as zf:
            zf.writestr(
                "retrystaged/SKILL.md",
                "---\nname: retrystaged\ndescription: "
                "Use when verifying that admin retry hits the staged dir bundle bytes for v2 blocked submissions\n---\n\n"
                + ("V2-only body text that is intentionally long enough to clear the inline content-quality threshold. " * 4),
            )
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", buf.getvalue(), "application/zip")},
            cookies=user_cookies,
        )
        assert r.status_code == 200, r.text
        from src.store_guardrails.runner import run_llm_review
        conn = get_system_db()
        sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()
        run_llm_review(
            sub_id,
            plugin_dir=Path(get_store_dir()) / eid / "versions" / "v2" / "plugin",
            conn_factory=get_system_db,
            api_key_loader=lambda: "sk", model_loader=lambda: "mock",
        )

        # Capture what review_bundle is called with on retry.
        seen = {}
        def spy_review_bundle(plugin_dir, **kw):
            seen["plugin_dir"] = plugin_dir
            return {
                "risk_level": "safe", "summary": "ok",
                "findings": [], "template_placeholders_found": 0,
                "reviewed_by_model": "mock", "error": None,
                "content_quality": {"verdict": "pass", "issues": []},
            }
        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle", spy_review_bundle,
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-for-retry-loader")

        # Admin retry.
        _, admin_cookies = _create_admin(web_client)
        r = web_client.post(
            f"/api/admin/store/submissions/{sub_id}/retry",
            cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text

        v2_dir = Path(get_store_dir()) / eid / "versions" / "v2" / "plugin"
        live_dir = Path(get_store_dir()) / eid / "plugin"
        assert seen["plugin_dir"] == v2_dir, (
            f"retry must review STAGED bytes ({v2_dir}); "
            f"instead reviewed {seen['plugin_dir']} (live={live_dir})"
        )

    def test_rescan_v2_blocked_passes_staged_dir_not_live(
        self, web_client, monkeypatch,
    ):
        """Same invariant for rescan."""
        from pathlib import Path
        from app.utils import get_store_dir
        from src.repositories.store_submissions import StoreSubmissionsRepository

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-for-rescan-test")
        user_id, user_cookies = _create_user(web_client, "rescanstaged@x.com")
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("rescanstaged"), "application/zip")},
            data={"type": "skill",
                  "description": (
                      "Use when verifying rescan reads staged version "
                      "bundle bytes not live for v2 blocked submissions"
                  )},
            cookies=user_cookies,
        )
        assert r.status_code == 201
        eid = r.json()["id"]

        def mock_block(*a, **kw):
            return {
                "risk_level": "high", "summary": "mock block",
                "findings": [{"severity": "high", "category": "test",
                              "file": "x", "explanation": "mock"}],
                "template_placeholders_found": 0,
                "reviewed_by_model": "mock", "error": None,
            }
        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle", mock_block,
        )
        monkeypatch.setattr(
            "app.api.store.get_guardrails_enabled", lambda: True,
        )
        monkeypatch.setattr(
            "app.api.store.get_guardrails_llm_provider_ready", lambda: True,
        )
        import io as _io
        import zipfile as _zip
        buf = _io.BytesIO()
        with _zip.ZipFile(buf, "w") as zf:
            zf.writestr(
                "rescanstaged/SKILL.md",
                "---\nname: rescanstaged\ndescription: "
                "Use when verifying that admin rescan hits the staged dir bundle bytes for v2 blocked submissions\n---\n\n"
                + ("V2 unique payload body line that is intentionally long enough to clear the inline content-quality threshold. " * 4),
            )
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", buf.getvalue(), "application/zip")},
            cookies=user_cookies,
        )
        assert r.status_code == 200, r.text
        from src.store_guardrails.runner import run_llm_review
        conn = get_system_db()
        sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()
        run_llm_review(
            sub_id,
            plugin_dir=Path(get_store_dir()) / eid / "versions" / "v2" / "plugin",
            conn_factory=get_system_db,
            api_key_loader=lambda: "sk", model_loader=lambda: "mock",
        )

        # Spy on the inline pipeline. admin.py imports
        # `run_inline_checks` function-locally — patch at the source
        # module so the lookup at call time sees the spy.
        seen_inline: list = []
        from src.store_guardrails import run_inline_checks as orig_run_inline
        def spy_inline(plugin_dir, **kw):
            seen_inline.append(plugin_dir)
            return orig_run_inline(plugin_dir, **kw)
        monkeypatch.setattr(
            "src.store_guardrails.run_inline_checks", spy_inline,
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-for-rescan")

        _, admin_cookies = _create_admin(web_client)
        r = web_client.post(
            f"/api/admin/store/submissions/{sub_id}/rescan",
            cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text

        v2_dir = Path(get_store_dir()) / eid / "versions" / "v2" / "plugin"
        assert v2_dir in seen_inline, (
            f"rescan must run inline against STAGED bytes ({v2_dir}); "
            f"got {seen_inline}"
        )


class TestOverrideForwardOnly:
    """Codex adversarial finding [HIGH H1]: override's promote loop
    used `target != current`, which would happily DEMOTE the live
    bundle when admin overrode a stale v2 submission while v3 was
    already approved + live. Forward-only: refuse the promote step
    when target_n <= current.

    Override of the row still flips status + visibility (admin's
    intent on the row itself is preserved); the on-disk live bundle
    just isn't rolled back."""

    def test_override_stale_v2_does_not_demote_when_v3_current(
        self, web_client, monkeypatch,
    ):
        from src.repositories.store_entities import StoreEntitiesRepository
        from src.repositories.store_submissions import StoreSubmissionsRepository

        # v1 clean, v2 PUT (mocked block) → blocked_llm, entity stays v1.
        # Set a fake API key so the BG task `run_llm_review` (scheduled
        # by the API after each PUT) can pass `default_api_key_loader`
        # — it then calls the mocked `review_bundle`, not the network.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-for-override-test")
        user_id, user_cookies = _create_user(web_client, "demote@x.com")
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("demote"), "application/zip")},
            data={"type": "skill",
                  "description": (
                      "Use when verifying override never demotes a "
                      "currently-live newer version of the same entity"
                  )},
            cookies=user_cookies,
        )
        assert r.status_code == 201
        eid = r.json()["id"]

        def mock_block(*a, **kw):
            return {
                "risk_level": "high", "summary": "mock block",
                "findings": [{"severity": "high", "category": "test",
                              "file": "x", "explanation": "mock"}],
                "template_placeholders_found": 0,
                "reviewed_by_model": "mock", "error": None,
            }
        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle", mock_block,
        )
        monkeypatch.setattr(
            "app.api.store.get_guardrails_enabled", lambda: True,
        )
        monkeypatch.setattr(
            "app.api.store.get_guardrails_llm_provider_ready", lambda: True,
        )

        # PUT v2 → blocked_llm.
        import io as _io
        import zipfile as _zip
        def _v2_zip():
            buf = _io.BytesIO()
            with _zip.ZipFile(buf, "w") as zf:
                zf.writestr(
                    "demote/SKILL.md",
                    "---\nname: demote\ndescription: Use when v2 blocked path is exercised by admin override forward-only tests for the demote-protection invariant\n---\n\n"
                    + ("V2 BODY content line long enough to clear the inline content-quality threshold for skill bodies. " * 4),
                )
            return buf.getvalue()
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", _v2_zip(), "application/zip")},
            cookies=user_cookies,
        )
        assert r.status_code == 200
        from pathlib import Path
        from app.utils import get_store_dir
        from src.store_guardrails.runner import run_llm_review
        conn = get_system_db()
        v2_sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()
        run_llm_review(
            v2_sub_id,
            plugin_dir=Path(get_store_dir()) / eid / "versions" / "v2" / "plugin",
            conn_factory=get_system_db,
            api_key_loader=lambda: "sk", model_loader=lambda: "mock",
        )

        # PUT v3 with mocked-APPROVE → v3 becomes current.
        def mock_approve(*a, **kw):
            return {
                "risk_level": "safe", "summary": "ok",
                "findings": [], "template_placeholders_found": 0,
                "reviewed_by_model": "mock", "error": None,
                "content_quality": {"verdict": "pass", "issues": []},
            }
        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle", mock_approve,
        )
        def _v3_zip():
            buf = _io.BytesIO()
            with _zip.ZipFile(buf, "w") as zf:
                zf.writestr(
                    "demote/SKILL.md",
                    "---\nname: demote\ndescription: Use when v3 approved path is exercised by admin override forward-only tests for the demote-protection invariant\n---\n\n"
                    + ("V3 BODY content line long enough to clear the inline content-quality threshold for skill bodies. " * 4),
                )
            return buf.getvalue()
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v3.zip", _v3_zip(), "application/zip")},
            cookies=user_cookies,
        )
        assert r.status_code == 200
        conn = get_system_db()
        v3_sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()
        run_llm_review(
            v3_sub_id,
            plugin_dir=Path(get_store_dir()) / eid / "versions" / "v3" / "plugin",
            conn_factory=get_system_db,
            api_key_loader=lambda: "sk", model_loader=lambda: "mock",
        )
        conn = get_system_db()
        ent_at_v3 = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert ent_at_v3["version_no"] == 3, (
            f"v3 must auto-promote to current; got {ent_at_v3['version_no']}"
        )
        v3_hash = ent_at_v3["version"]

        # Admin overrides stale v2 (status was blocked_llm). MUST NOT
        # demote live from v3 back to v2 bytes.
        _, admin_cookies = _create_admin(web_client)
        r = web_client.post(
            f"/api/admin/store/submissions/{v2_sub_id}/override",
            json={"reason": "cleared in offline review — leaving stale row"},
            cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text

        conn = get_system_db()
        ent_after = StoreEntitiesRepository(conn).get(eid)
        v2_sub_after = StoreSubmissionsRepository(conn).get(v2_sub_id)
        conn.close()
        assert ent_after["version_no"] == 3, (
            f"override of stale v2 must NOT demote live; "
            f"got version_no={ent_after['version_no']} (expected 3)"
        )
        assert ent_after["version"] == v3_hash, (
            "entity.version hash must stay at v3 — got demoted"
        )
        # Submission row still flips to overridden (admin intent on the
        # row itself is preserved; only the on-disk roll-back is gated).
        assert v2_sub_after["status"] == "overridden"

    def test_override_byte_identical_v2_blocked_promotes_correctly(
        self, web_client, monkeypatch,
    ):
        """Codex adversarial-review follow-up on PR #330: confirm
        override's submission_id lookup resolves v2 correctly when
        its hash collides with v1's. Pre-PR-330 the override loop
        did hash-match-first-wins → stuck on v1's n=1; forward-only
        `1 > 1` skipped promote."""
        from pathlib import Path
        from app.utils import get_store_dir
        from src.repositories.store_entities import StoreEntitiesRepository
        from src.repositories.store_submissions import StoreSubmissionsRepository
        from src.store_guardrails.runner import run_llm_review
        import io as _io
        import zipfile as _zip

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-for-override-identical")
        user_id, user_cookies = _create_user(web_client, "override-id@x.com")

        identical_body = (
            "Identical body content long enough to clear the inline "
            "content-quality threshold for skill bodies. " * 4
        )

        def _identical_zip():
            buf = _io.BytesIO()
            with _zip.ZipFile(buf, "w") as zf:
                zf.writestr(
                    "overrideid/SKILL.md",
                    "---\nname: overrideid\ndescription: "
                    "Use when verifying override resolves v2 by "
                    "submission_id even when v2 hash matches v1 hash\n---\n\n"
                    + identical_body,
                )
            return buf.getvalue()

        r = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _identical_zip(), "application/zip")},
            data={"type": "skill",
                  "description": (
                      "Use when verifying override resolves v2 by "
                      "submission_id even when v2 hash matches v1 hash"
                  )},
            cookies=user_cookies,
        )
        assert r.status_code == 201, r.text
        eid = r.json()["id"]

        monkeypatch.setattr(
            "app.api.store.get_guardrails_enabled", lambda: True,
        )
        monkeypatch.setattr(
            "app.api.store.get_guardrails_llm_provider_ready", lambda: True,
        )

        def mock_block(*a, **kw):
            return {
                "risk_level": "high", "summary": "mock block",
                "findings": [{"severity": "high", "category": "test",
                              "file": "x", "explanation": "mock"}],
                "template_placeholders_found": 0,
                "reviewed_by_model": "mock", "error": None,
            }
        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle", mock_block,
        )
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", _identical_zip(), "application/zip")},
            cookies=user_cookies,
        )
        assert r.status_code == 200, r.text
        conn = get_system_db()
        v2_sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()
        run_llm_review(
            v2_sub_id,
            plugin_dir=Path(get_store_dir()) / eid / "versions" / "v2" / "plugin",
            conn_factory=get_system_db,
            api_key_loader=lambda: "sk", model_loader=lambda: "mock",
        )

        conn = get_system_db()
        ent_before = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert ent_before["version_no"] == 1
        v1_hash = ent_before["version"]

        _, admin_cookies = _create_admin(web_client)
        r = web_client.post(
            f"/api/admin/store/submissions/{v2_sub_id}/override",
            json={"reason": "false positive — cleared in offline review"},
            cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text
        conn = get_system_db()
        ent_after = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert ent_after["version_no"] == 2, (
            f"override must promote to v2 even when v2 hash matches v1's; "
            f"got version_no={ent_after['version_no']}"
        )
        # Hash unchanged (identical bundle), but version_no DID move.
        assert ent_after["version"] == v1_hash


# ---------------------------------------------------------------------------
# v30: Download bundle, Sort by size, Quota
# ---------------------------------------------------------------------------


class TestAdminBundleDownload:
    def test_download_returns_zip(self, web_client):
        """Live blocked-LLM bundle is downloadable as a fresh ZIP. Inline
        rejections no longer persist bundles, so this exercises the LLM
        path via the seed helper."""
        user_id, _ = _create_user(web_client, "u@x.com")
        _entity_id, sub_id = _seed_quarantined_entity(
            user_id, "u@x.com", skill_name="dl",
        )

        _, admin_cookies = _create_admin(web_client)
        r = web_client.get(
            f"/api/admin/store/submissions/{sub_id}/bundle.zip",
            cookies=admin_cookies,
        )
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        assert "attachment" in r.headers["content-disposition"]
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            assert any("SKILL.md" in n for n in zf.namelist())
            assert any("run.sh" in n for n in zf.namelist())

    def test_download_410_after_purge(self, web_client):
        from src.repositories.store_submissions import StoreSubmissionsRepository
        u, _ = _create_user(web_client, "u@x.com")
        conn = get_system_db()
        sub_id = StoreSubmissionsRepository(conn).create(
            submitter_id=u, submitter_email="u@x.com",
            type="skill", name="x", version="1.0.0",
            status="blocked_inline", entity_id=None,
        )
        conn.close()

        _, admin_cookies = _create_admin(web_client)
        r = web_client.get(
            f"/api/admin/store/submissions/{sub_id}/bundle.zip",
            cookies=admin_cookies,
        )
        assert r.status_code == 410

    def test_download_non_admin_forbidden(self, web_client):
        from src.repositories.store_submissions import StoreSubmissionsRepository
        u, user_cookies = _create_user(web_client, "u@x.com")
        conn = get_system_db()
        sub_id = StoreSubmissionsRepository(conn).create(
            submitter_id=u, submitter_email="u@x.com",
            type="skill", name="x", version="1.0.0",
            status="approved", entity_id="some-id",
        )
        conn.close()
        r = web_client.get(
            f"/api/admin/store/submissions/{sub_id}/bundle.zip",
            cookies=user_cookies,
        )
        assert r.status_code == 403

    def test_download_v2_blocked_returns_staged_bundle_not_live(
        self, web_client, monkeypatch,
    ):
        """Codex adversarial review [LOW]: pre-fix the download
        streamed live `plugin/` bytes regardless of which submission
        was being inspected. Under deferred promotion (v37+), live
        holds the prior approved version's bytes — so downloading a
        blocked v2 returned v1's safe bundle while the admin was
        deciding whether to override the *staged* v2's risky bytes.
        Fixed: resolve the staged `versions/v<N>/plugin/` per
        submission via `_version_no_for_submission`."""
        from pathlib import Path
        from app.utils import get_store_dir
        from src.repositories.store_entities import StoreEntitiesRepository
        from src.repositories.store_submissions import StoreSubmissionsRepository

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-for-dl-test")
        user_id, user_cookies = _create_user(web_client, "dl-stage@x.com")
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("dlstage"), "application/zip")},
            data={"type": "skill",
                  "description": (
                      "Use when verifying admin forensic download "
                      "serves staged version bytes for v2 blocked "
                      "submissions instead of the live prior version"
                  )},
            cookies=user_cookies,
        )
        assert r.status_code == 201, r.text
        eid = r.json()["id"]

        # PUT v2 with mocked LLM block.
        def mock_block(*a, **kw):
            return {
                "risk_level": "high", "summary": "mock block",
                "findings": [{"severity": "high", "category": "test",
                              "file": "x", "explanation": "mock"}],
                "template_placeholders_found": 0,
                "reviewed_by_model": "mock", "error": None,
            }
        monkeypatch.setattr(
            "src.store_guardrails.llm_review.review_bundle", mock_block,
        )
        monkeypatch.setattr(
            "app.api.store.get_guardrails_enabled", lambda: True,
        )
        monkeypatch.setattr(
            "app.api.store.get_guardrails_llm_provider_ready", lambda: True,
        )
        import io as _io
        import zipfile as _zip
        v2_marker = "V2_STAGED_PAYLOAD_UNIQUE_TOKEN"
        buf = _io.BytesIO()
        with _zip.ZipFile(buf, "w") as zf:
            zf.writestr(
                "dlstage/SKILL.md",
                "---\nname: dlstage\ndescription: "
                "Use when verifying admin download returns staged bytes for v2 blocked submissions\n---\n\n"
                + (f"{v2_marker}. Body content long enough to clear the inline content-quality threshold for skill bodies. " * 3),
            )
        r = web_client.put(
            f"/api/store/entities/{eid}",
            files={"file": ("v2.zip", buf.getvalue(), "application/zip")},
            cookies=user_cookies,
        )
        assert r.status_code == 200, r.text

        # Confirm v2 sub is blocked + entity stayed at v1 (live = v1 bytes).
        conn = get_system_db()
        v2_sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        v2_sub = StoreSubmissionsRepository(conn).get(v2_sub_id)
        ent = StoreEntitiesRepository(conn).get(eid)
        conn.close()
        assert v2_sub["status"] == "blocked_llm"
        assert ent["version_no"] == 1

        # Admin downloads the v2 submission's bundle. Must serve the
        # STAGED v2 bytes (contain v2_marker), NOT live v1 (which
        # doesn't).
        _, admin_cookies = _create_admin(web_client)
        r = web_client.get(
            f"/api/admin/store/submissions/{v2_sub_id}/bundle.zip",
            cookies=admin_cookies,
        )
        assert r.status_code == 200
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            blob = b""
            for n in zf.namelist():
                blob += zf.read(n)
        assert v2_marker.encode() in blob, (
            "admin download must return STAGED v2 bytes "
            "(missing the v2-only marker — got live v1 bytes instead)"
        )


class TestAdminSortBySize:
    def test_sort_file_size_asc_desc(self, web_client):
        from src.repositories.store_submissions import StoreSubmissionsRepository
        u, _ = _create_user(web_client, "u@x.com")
        conn = get_system_db()
        StoreSubmissionsRepository(conn).create(
            submitter_id=u, submitter_email="u@x.com",
            type="skill", name="big", version="1", status="approved",
            file_size=10000,
        )
        StoreSubmissionsRepository(conn).create(
            submitter_id=u, submitter_email="u@x.com",
            type="skill", name="med", version="1", status="approved",
            file_size=5000,
        )
        StoreSubmissionsRepository(conn).create(
            submitter_id=u, submitter_email="u@x.com",
            type="skill", name="tiny", version="1", status="approved",
            file_size=100,
        )
        conn.close()

        # Admin endpoint passes sort/order through to the repo whitelist
        # (#23). Confirm both directions via the API.
        _, admin_cookies = _create_admin(web_client)
        r = web_client.get(
            "/api/admin/store/submissions?sort=file_size&order=asc",
            cookies=admin_cookies,
        )
        assert r.status_code == 200
        names = [i["name"] for i in r.json()["items"]]
        assert names == ["tiny", "med", "big"], names

        r = web_client.get(
            "/api/admin/store/submissions?sort=file_size&order=desc",
            cookies=admin_cookies,
        )
        assert r.status_code == 200
        names = [i["name"] for i in r.json()["items"]]
        assert names == ["big", "med", "tiny"], names

    def test_invalid_sort_key_400(self, web_client):
        """#23 — sort whitelist rejects bogus keys at the API edge.
        Pre-fix, an unknown key fell through to a substring-replace
        chain that could surface 500s; now it's a clean 400."""
        _, admin_cookies = _create_admin(web_client)
        r = web_client.get(
            "/api/admin/store/submissions?sort=injected__column",
            cookies=admin_cookies,
        )
        assert r.status_code == 400, r.text
        assert "invalid_sort_key" in r.text


class TestQuota:
    def test_quota_blocks_after_threshold(self, web_client, monkeypatch):
        """Quota gate triggers on the LLM-tier reject count. Inline
        failures no longer create rows, so the quota is seeded via
        the repo (mimicking two prior blocked_llm verdicts in the
        last 24h). The third upload is gated upstream by 429."""
        from app import instance_config as ic
        from src.repositories.store_submissions import StoreSubmissionsRepository
        monkeypatch.setattr(ic, "get_guardrails_blocked_quota_per_day", lambda: 2)

        user_id, user_cookies = _create_user(web_client, "spammer@x.com")
        conn = get_system_db()
        repo = StoreSubmissionsRepository(conn)
        for i in range(2):
            repo.create(
                submitter_id=user_id, submitter_email="spammer@x.com",
                type="skill", name=f"seed-{i}", version="1.0.0",
                status="blocked_llm", entity_id=None,
            )
        conn.close()

        # Third upload — any clean ZIP would do; expect 429 before the
        # guardrail pipeline runs.
        r = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("clean-after-quota"), "application/zip")},
            data={"type": "skill"}, cookies=user_cookies,
        )
        assert r.status_code == 429, r.text
        body = r.json()["detail"]
        assert body["code"] == "quota_exceeded"
        assert body["limit"] == 2

    def test_quota_disabled_with_zero(self, web_client, monkeypatch):
        """quota=0 disables the gate entirely. Seed many blocked_llm
        rows; clean uploads still succeed."""
        from app import instance_config as ic
        from src.repositories.store_submissions import StoreSubmissionsRepository
        monkeypatch.setattr(ic, "get_guardrails_blocked_quota_per_day", lambda: 0)

        user_id, user_cookies = _create_user(web_client, "trusted@x.com")
        conn = get_system_db()
        for i in range(5):
            StoreSubmissionsRepository(conn).create(
                submitter_id=user_id, submitter_email="trusted@x.com",
                type="skill", name=f"history-{i}", version="1.0.0",
                status="blocked_llm", entity_id=None,
            )
        conn.close()

        r = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("clean-zero-quota"), "application/zip")},
            data={"type": "skill"}, cookies=user_cookies,
        )
        # Clean upload — passes inline guardrails. With ANTHROPIC_API_KEY
        # absent in tests the guardrail pipeline auto-disables so the
        # entity lands at ``approved`` (201). Assert the success codes
        # explicitly so a 500 from an unrelated regression doesn't
        # masquerade as quota-disabled.
        assert r.status_code in (200, 201), r.text

    def test_quota_counter_includes_blocked_llm_and_review_error(self, web_client):
        """The counter narrows to ``blocked_llm`` + ``review_error`` —
        inline failures no longer create rows. Legacy ``blocked_inline``
        rows from pre-cutover instances are intentionally excluded
        (kept in DB as historical audit, not counted toward the live
        quota)."""
        from datetime import datetime, timezone, timedelta
        from src.repositories.store_submissions import StoreSubmissionsRepository

        _, user_cookies = _create_user(web_client, "spammer-9@x.com")
        conn = get_system_db()
        repo = StoreSubmissionsRepository(conn)
        for i, status in enumerate(("blocked_inline", "blocked_llm", "review_error")):
            repo.create(
                submitter_id="spammer-9", submitter_email="spammer-9@x.com",
                type="skill", name=f"q9-{i}", version="1.0.0",
                status=status, entity_id=None,
                inline_checks={"manifest": {"status": "fail"}},
            )
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        count = repo.count_blocked_for_submitter_since("spammer-9", since)
        conn.close()
        assert count == 2, (
            f"counter must skip legacy blocked_inline; got {count}"
        )


# ---------------------------------------------------------------------------
# v32+ quarantine semantics
# ---------------------------------------------------------------------------


class TestQuarantineGates:
    def test_owner_cannot_delete_quarantined(self, web_client):
        """Owner trying to DELETE their own quarantined (blocked_llm)
        entity must be refused — admin investigates first."""
        user_id, user_cookies = _create_user(web_client, "u@x.com")
        entity_id, _sub_id = _seed_quarantined_entity(user_id, "u@x.com", "q1")

        r = web_client.delete(
            f"/api/store/entities/{entity_id}", cookies=user_cookies,
        )
        assert r.status_code == 403
        body = r.json()["detail"]
        assert body["code"] == "quarantined_owner_cannot_delete"

    def test_owner_can_withdraw_a_crashed_review(self, web_client):
        """`review_error` is not a verdict — the owner must have an exit.

        Seen in production: a skill whose inline checks all passed sat in
        `review_error` (the LLM call crashed) for a month. Delete and Edit
        were both locked and re-uploading 409'd with "delete the existing
        entity first", so the author had no way out of a state nothing had
        ever objected to.
        """
        user_id, user_cookies = _create_user(web_client, "crash@x.com")
        entity_id, _ = _seed_quarantined_entity(
            user_id, "crash@x.com", "crashed", status="review_error",
        )

        r = web_client.delete(
            f"/api/store/entities/{entity_id}", cookies=user_cookies,
        )
        assert r.status_code == 204, r.text

    def test_owner_can_withdraw_while_the_verdict_is_pending(self, web_client):
        """Nothing has been judged yet — withdrawing your own not-yet-public
        upload erases no evidence."""
        user_id, user_cookies = _create_user(web_client, "pend@x.com")
        entity_id, _ = _seed_quarantined_entity(
            user_id, "pend@x.com", "pending", status="pending_llm",
        )

        r = web_client.delete(
            f"/api/store/entities/{entity_id}", cookies=user_cookies,
        )
        assert r.status_code == 204, r.text

    def test_a_newer_unjudged_upload_does_not_clear_an_adverse_verdict(self, web_client):
        """Devin Review on #1263: the objection must not expire.

        Reading only the LATEST submission let an author walk out of the gate:
        re-upload once, and the fresh `pending_llm` row becomes "latest" while
        the `blocked_llm` verdict stops counting — exactly the withdrawal this
        refusal exists to prevent. The sibling predicate
        `_entity_review_blocked` scans the whole chain for the same reason.
        """
        from src.db import get_system_db
        from src.repositories.store_submissions import StoreSubmissionsRepository

        user_id, user_cookies = _create_user(web_client, "evade@x.com")
        entity_id, _ = _seed_quarantined_entity(user_id, "evade@x.com", "evader")

        conn = get_system_db()
        StoreSubmissionsRepository(conn).create(
            submitter_id=user_id,
            submitter_email="evade@x.com",
            type="skill",
            name="evader",
            version="1.0.1",
            status="pending_llm",
            entity_id=entity_id,
            inline_checks={"manifest": {"status": "pass", "issues": []}},
        )
        conn.close()

        r = web_client.delete(f"/api/store/entities/{entity_id}", cookies=user_cookies)

        assert r.status_code == 403, r.text
        assert r.json()["detail"]["code"] == "quarantined_owner_cannot_delete"

    def test_admin_can_delete_quarantined(self, web_client):
        user_id, _ = _create_user(web_client, "u@x.com")
        entity_id, _sub_id = _seed_quarantined_entity(user_id, "u@x.com", "q2")

        _, admin_cookies = _create_admin(web_client)
        r = web_client.delete(
            f"/api/store/entities/{entity_id}", cookies=admin_cookies,
        )
        # DELETE returns 204 No Content per the API design rule landed in
        # this PR (tests/test_api_design_rules.py rule 2).
        assert r.status_code == 204, r.text

    def test_non_owner_non_admin_cannot_view_quarantined(self, web_client):
        """Random user navigating to ANY per-entity asset endpoint gets
        404 — same as if the entity didn't exist (no leak via 403).
        Covers every ``_enforce_visibility`` caller in app/api/store.py
        + the marketplace flea detail."""
        owner_id, _ = _create_user(web_client, "owner@x.com")
        entity_id, _sub_id = _seed_quarantined_entity(owner_id, "owner@x.com", "q3")

        _, intruder_cookies = _create_user(web_client, "snoop@x.com")
        r = web_client.get(
            f"/api/store/entities/{entity_id}", cookies=intruder_cookies,
        )
        assert r.status_code == 404, "detail must 404 for non-owner"
        r = web_client.get(
            f"/api/store/entities/{entity_id}/files", cookies=intruder_cookies,
        )
        assert r.status_code == 404, "files must 404 for non-owner"
        r = web_client.get(
            f"/api/store/entities/{entity_id}/photo", cookies=intruder_cookies,
        )
        assert r.status_code == 404, "photo must 404 for non-owner"
        r = web_client.get(
            f"/api/store/entities/{entity_id}/docs/anything.md",
            cookies=intruder_cookies,
        )
        assert r.status_code == 404, "docs must 404 for non-owner"

    def test_quarantined_entity_excluded_from_store_entities_list(self, web_client):
        """Random non-owner non-admin hitting the public flea-listing
        (`/api/store/entities`) must NOT see another user's quarantined
        entry. Mirrors the marketplace-items coverage but on the
        store-namespaced listing."""
        owner_id, owner_cookies = _create_user(web_client, "qowner@x.com")
        entity_id, _sub_id = _seed_quarantined_entity(
            owner_id, "qowner@x.com", "q-list",
        )

        _, intruder_cookies = _create_user(web_client, "qsnoop@x.com")
        r = web_client.get("/api/store/entities", cookies=intruder_cookies)
        assert r.status_code == 200
        ids = {it["id"] for it in r.json().get("items", [])}
        assert entity_id not in ids, (
            "non-owner non-admin saw another user's quarantined entity "
            "in /api/store/entities listing"
        )

        r = web_client.get("/api/store/entities", cookies=owner_cookies)
        owner_ids = {it["id"] for it in r.json().get("items", [])}
        assert entity_id in owner_ids, (
            "owner should see own quarantined entity in their listing"
        )

    def test_owner_can_view_their_quarantined_entity(self, web_client):
        owner_id, owner_cookies = _create_user(web_client, "owner@x.com")
        entity_id, _sub_id = _seed_quarantined_entity(owner_id, "owner@x.com", "q4")

        r = web_client.get(
            f"/api/store/entities/{entity_id}", cookies=owner_cookies,
        )
        assert r.status_code == 200

    def test_install_quarantined_refused_for_non_admin(self, web_client):
        """Even owner cannot add their own quarantined item to my-stack."""
        owner_id, owner_cookies = _create_user(web_client, "owner@x.com")
        entity_id, _sub_id = _seed_quarantined_entity(owner_id, "owner@x.com", "q5")

        r = web_client.post(
            f"/api/store/entities/{entity_id}/install", cookies=owner_cookies,
        )
        assert r.status_code == 409
        assert r.json()["detail"] == "entity_not_approved"


# ---------------------------------------------------------------------------
# v32+ marketplace consolidation: visibility gates + owner-visible cards
# ---------------------------------------------------------------------------


class TestMarketplaceFleaConsolidation:
    def test_marketplace_flea_detail_404_for_non_owner_quarantined(self, web_client):
        """Random non-owner non-admin pasting an entity_id into
        /marketplace/flea/{id} gets 404 — same policy as the now-deleted
        /store/{id}."""
        owner_id, _ = _create_user(web_client, "owner@x.com")
        eid, _sub_id = _seed_quarantined_entity(owner_id, "owner@x.com", "c1")

        _, intruder_cookies = _create_user(web_client, "snoop@x.com")
        r = web_client.get(f"/marketplace/flea/{eid}", cookies=intruder_cookies)
        assert r.status_code == 404
        r = web_client.get(f"/api/marketplace/flea/{eid}/detail", cookies=intruder_cookies)
        assert r.status_code == 404

    def test_marketplace_flea_detail_owner_sees_quarantine_banner(self, web_client):
        """Owner landing on /marketplace/flea/{id} sees the quarantine
        banner with the failure summary AND the actual finding details
        — not just a generic "Quarantined" header."""
        owner_id, owner_cookies = _create_user(web_client, "owner@x.com")
        eid, _sub_id = _seed_quarantined_entity(
            owner_id, "owner@x.com", "c2",
            llm_summary="reviewer flagged the bash eval",
            static_findings=[
                {"file": "run.sh", "line": 2, "severity": "high",
                 "category": "code_exec",
                 "reason": "shell eval expanding a variable",
                 "explanation": "shell eval expanding a variable",
                 "snippet": "eval $1"},
            ],
        )

        r = web_client.get(f"/marketplace/flea/{eid}", cookies=owner_cookies)
        assert r.status_code == 200
        body = r.text
        assert "vis-banner" in body
        assert "Quarantined" in body
        # blocked_llm path renders the LLM verdict summary + per-finding
        # list. Banner must surface BOTH so the submitter knows WHY
        # without having to ping an admin.
        assert "Security findings" in body, (
            "banner missing 'Security findings' section"
        )
        assert "run.sh" in body, "banner missing path of offending file"
        assert "shell eval" in body, "banner missing reviewer summary"

    def test_review_error_banner_shows_error_detail(self, web_client):
        """#review_error — banner must surface the underlying error
        message + any inline_checks the runner captured before bailing.
        Pre-fix the banner only said 'couldn't complete its check' with
        no actionable detail."""
        from src.repositories.store_entities import StoreEntitiesRepository
        from src.repositories.store_submissions import StoreSubmissionsRepository
        owner_id, owner_cookies = _create_user(web_client, "rev-err@x.com")
        # Stage entity + submission directly so we can land in review_error.
        conn = get_system_db()
        StoreEntitiesRepository(conn).create(
            id="ent-rev-err", owner_user_id=owner_id, owner_username="rev-err",
            type="skill", name="rev-err", description="Test review_error banner",
            category=None, version="1.0.0", file_size=10,
            visibility_status="hidden",
        )
        StoreSubmissionsRepository(conn).create(
            submitter_id=owner_id, submitter_email="rev-err@x.com",
            type="skill", name="rev-err", version="1.0.0",
            status="review_error", entity_id="ent-rev-err",
            inline_checks={
                "manifest": {"status": "pass", "issues": []},
                "static_security": {"status": "pass", "findings": []},
                "quality": {"status": "pass", "issues": [],
                            "template_placeholders": 0},
            },
            llm_findings={
                "risk_level": None, "summary": None, "findings": [],
                "template_placeholders_found": 0,
                "reviewed_by_model": None,
                "error": "LLMTimeoutError: Anthropic connection error",
            },
        )
        conn.close()

        r = web_client.get(
            "/marketplace/flea/ent-rev-err", cookies=owner_cookies,
        )
        assert r.status_code == 200
        assert "vis-banner" in r.text
        assert "errored" in r.text or "Under review" in r.text
        # The actionable error detail must surface.
        assert "LLMTimeoutError" in r.text, (
            "review_error banner must surface llm_findings.error"
        )

    def test_legacy_store_detail_url_returns_404(self, web_client):
        """The /store/{id} route was deleted in v32+. Stale bookmarks 404."""
        _, owner_cookies = _create_user(web_client, "u@x.com")
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("legacy"), "application/zip")},
            data={"type": "skill"}, cookies=owner_cookies,
        )
        eid = c.json()["id"]
        r = web_client.get(f"/store/{eid}", cookies=owner_cookies)
        assert r.status_code == 404

    def test_marketplace_listing_includes_owner_quarantined(self, web_client):
        """Submitter sees their own non-approved entries in the
        /api/marketplace/items?tab=flea grid; non-owner does not."""
        owner_id, owner_cookies = _create_user(web_client, "owner@x.com")
        eid, _sub_id = _seed_quarantined_entity(owner_id, "owner@x.com", "c4")

        # Owner — their own quarantined card surfaces with is_viewer_owner=True.
        r = web_client.get("/api/marketplace/items?tab=flea", cookies=owner_cookies)
        assert r.status_code == 200
        items = r.json()["items"]
        own = [it for it in items if it["id"] == f"flea-{eid}"]
        assert own, f"owner should see own quarantined item; got {[it['id'] for it in items]}"
        assert own[0]["visibility_status"] != "approved"
        assert own[0]["is_viewer_owner"] is True

        # Non-owner — same listing must NOT surface the quarantined entry.
        _, snoop_cookies = _create_user(web_client, "snoop@x.com")
        r = web_client.get("/api/marketplace/items?tab=flea", cookies=snoop_cookies)
        assert r.status_code == 200
        items = r.json()["items"]
        snoop = [it for it in items if it["id"] == f"flea-{eid}"]
        assert not snoop, "non-owner must not see another user's quarantined item"


# ---------------------------------------------------------------------------
# v35 archive (soft delete) semantics
# ---------------------------------------------------------------------------


class TestArchiveSoftDelete:
    def _upload_clean(self, web_client, cookies, name="clean1"):
        """Helper: upload a clean skill that lands as approved (no API key
        in test env -> auto-approve via guardrails-disabled fallback)."""
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip(name), "application/zip")},
            data={
                "type": "skill",
                "description": (
                    "Use when verifying lifecycle and admin flows over a "
                    "clean reference bundle that passes every guardrail tier"
                ),
            },
            cookies=cookies,
        )
        assert c.status_code == 201, c.text
        return c.json()["id"]

    def test_archive_frees_name_for_reupload(self, web_client):
        """v36 rename-on-archive: owner uploads `myskill`, archives,
        re-uploads `myskill` → 201. Both rows exist; archived row's
        name carries the `__archived__<epoch>` marker."""
        from src.repositories.store_entities import StoreEntitiesRepository
        from src.store_naming import is_archived_name
        _, owner_cookies = _create_user(web_client, "rename-archive@x.com")
        eid_v1 = self._upload_clean(web_client, owner_cookies, name="myskill")
        # Archive v1.
        r = web_client.delete(
            f"/api/store/entities/{eid_v1}", cookies=owner_cookies,
        )
        assert r.status_code == 204, r.text

        # Re-upload under the original name — must succeed.
        eid_v2 = self._upload_clean(web_client, owner_cookies, name="myskill")
        assert eid_v2 != eid_v1, "re-upload should produce a new entity id"

        conn = get_system_db()
        repo = StoreEntitiesRepository(conn)
        v1 = repo.get(eid_v1)
        v2 = repo.get(eid_v2)
        conn.close()
        assert v1["visibility_status"] == "archived"
        assert is_archived_name(v1["name"]), (
            f"archived row name must carry the rename suffix; got {v1['name']!r}"
        )
        assert v2["visibility_status"] in ("approved", "pending")
        assert v2["name"] == "myskill", (
            f"new row should keep plain name; got {v2['name']!r}"
        )

    def test_archive_renames_baked_skill_dir_on_disk(self, web_client):
        """The on-disk `skills/<old_suffix>/` directory is renamed to
        `skills/<new_suffix>/` and SKILL.md frontmatter is rewritten
        in lockstep so consumers' Claude Code resolves the new slug."""
        from app.utils import get_store_dir
        from src.repositories.store_entities import StoreEntitiesRepository
        from src.store_naming import suffixed_name
        owner_id, owner_cookies = _create_user(web_client, "disk-rename@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="diskskill")

        old_suffix = suffixed_name("diskskill", "disk-rename")
        old_dir = Path(get_store_dir()) / eid / "plugin" / "skills" / old_suffix
        assert old_dir.is_dir(), f"pre-archive: missing {old_dir}"

        web_client.delete(
            f"/api/store/entities/{eid}", cookies=owner_cookies,
        )

        conn = get_system_db()
        new_name = StoreEntitiesRepository(conn).get(eid)["name"]
        conn.close()
        new_suffix = suffixed_name(new_name, "disk-rename")
        new_dir = Path(get_store_dir()) / eid / "plugin" / "skills" / new_suffix
        assert new_dir.is_dir(), f"post-archive: missing {new_dir}"
        assert not old_dir.exists(), (
            f"old slug dir must be gone post-archive; still found {old_dir}"
        )
        # Frontmatter rewritten to new suffix.
        skill_md = (new_dir / "SKILL.md").read_text(encoding="utf-8")
        assert f"name: {new_suffix}" in skill_md, (
            f"SKILL.md frontmatter not updated; got:\n{skill_md[:200]}"
        )

    def test_un_archive_strips_suffix_back_to_original(self, web_client):
        """Admin un-archive (set_visibility('approved') from 'archived')
        strips the `__archived__\\d+$` suffix and restores the original
        name + clears archive metadata."""
        from src.repositories.store_entities import StoreEntitiesRepository
        from src.store_naming import is_archived_name
        owner_id, owner_cookies = _create_user(web_client, "unarch@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="back")
        web_client.delete(f"/api/store/entities/{eid}", cookies=owner_cookies)

        conn = get_system_db()
        repo = StoreEntitiesRepository(conn)
        assert is_archived_name(repo.get(eid)["name"])
        repo.set_visibility(eid, "approved")
        row = repo.get(eid)
        conn.close()
        assert row["name"] == "back"
        assert row["visibility_status"] == "approved"
        assert row["archived_at"] is None
        assert row["archived_by"] is None

    def test_un_archive_when_name_taken_appends_restored_suffix(self, web_client):
        """Owner archives `taken`, re-uploads `taken` (new entity), admin
        un-archives the original. Original entity gets
        `taken-restored-1` since the slot is occupied."""
        from src.repositories.store_entities import StoreEntitiesRepository
        owner_id, owner_cookies = _create_user(web_client, "conflict-arch@x.com")
        eid_v1 = self._upload_clean(web_client, owner_cookies, name="taken")
        web_client.delete(f"/api/store/entities/{eid_v1}", cookies=owner_cookies)
        # Re-upload takes the slot.
        self._upload_clean(web_client, owner_cookies, name="taken")

        conn = get_system_db()
        repo = StoreEntitiesRepository(conn)
        repo.set_visibility(eid_v1, "approved")
        row = repo.get(eid_v1)
        conn.close()
        assert row["name"] == "taken-restored-1", (
            f"un-archive into taken slot must append -restored-1; "
            f"got {row['name']!r}"
        )

    def test_active_same_name_still_409(self, web_client):
        """Regression: when the prior entity is NOT archived, the
        same-name 409 still fires."""
        _, owner_cookies = _create_user(web_client, "still409@x.com")
        self._upload_clean(web_client, owner_cookies, name="active")
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip("active"), "application/zip")},
            data={"type": "skill"}, cookies=owner_cookies,
        )
        assert c.status_code == 409, c.text
        assert c.json()["detail"] == "conflict_owner_name"

    def test_admin_queue_strips_archive_suffix_for_display(self, web_client):
        """Admin queue renders the original name (not the suffixed one)
        in the row's name cell so admins don't see ugly markers."""
        owner_id, owner_cookies = _create_user(web_client, "displaystrip@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="displaytest")
        web_client.delete(f"/api/store/entities/{eid}", cookies=owner_cookies)

        _, admin_cookies = _create_admin(web_client)
        r = web_client.get(
            "/admin/store/submissions?status=archived", cookies=admin_cookies,
        )
        assert r.status_code == 200
        body = r.text
        assert "displaytest" in body
        assert "__archived__" not in body, (
            "admin queue must strip the archive-rename suffix for display"
        )

    def test_owner_can_archive_approved_entity(self, web_client):
        """DELETE without ?hard=true on owner's approved entity = soft archive.
        Bundle stays on disk; existing installs preserved."""
        from src.repositories.store_entities import StoreEntitiesRepository
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="arch-1")

        # Pre-archive sanity.
        conn = get_system_db()
        ent = StoreEntitiesRepository(conn).get(eid)
        assert ent["visibility_status"] == "approved"
        conn.close()

        r = web_client.delete(f"/api/store/entities/{eid}", cookies=owner_cookies)
        assert r.status_code == 204, r.text

        conn = get_system_db()
        ent = StoreEntitiesRepository(conn).get(eid)
        assert ent is not None  # row preserved
        assert ent["visibility_status"] == "archived"
        assert ent["archived_at"] is not None
        assert ent["archived_by"] == "owner"
        # Bundle dir still on disk.
        from app.utils import get_store_dir
        assert (get_store_dir() / eid).exists()
        conn.close()

    def test_owner_hard_delete_refused(self, web_client):
        """Owner cannot pass ?hard=true — admin-only path."""
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="arch-2")
        r = web_client.delete(f"/api/store/entities/{eid}?hard=true", cookies=owner_cookies)
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "hard_delete_admin_only"

    def test_admin_can_hard_delete(self, web_client):
        from src.repositories.store_entities import StoreEntitiesRepository
        from app.utils import get_store_dir
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="arch-3")
        bundle_dir = get_store_dir() / eid
        assert bundle_dir.exists()

        _, admin_cookies = _create_admin(web_client)
        r = web_client.delete(f"/api/store/entities/{eid}?hard=true", cookies=admin_cookies)
        assert r.status_code == 204, r.text

        conn = get_system_db()
        assert StoreEntitiesRepository(conn).get(eid) is None
        conn.close()
        assert not bundle_dir.exists()

    def test_archived_excluded_from_marketplace_listing(self, web_client):
        """Approved → archived: every browse listing hides it (even owner)."""
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="arch-4")
        web_client.delete(f"/api/store/entities/{eid}", cookies=owner_cookies)

        # Owner — own archived must NOT surface in the public-style listing.
        r = web_client.get("/api/marketplace/items?tab=flea", cookies=owner_cookies)
        assert r.status_code == 200
        ids = {it["id"] for it in r.json()["items"]}
        assert f"flea-{eid}" not in ids

    def test_install_refused_on_archived(self, web_client):
        """Archived entities can't be added to my stack."""
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="arch-5")
        web_client.delete(f"/api/store/entities/{eid}", cookies=owner_cookies)

        _, other_cookies = _create_user(web_client, "other@x.com")
        r = web_client.post(
            f"/api/store/entities/{eid}/install", cookies=other_cookies,
        )
        assert r.status_code == 409
        assert r.json()["detail"] == "entity_not_approved"

    def test_archived_still_serves_existing_installs(self, web_client):
        """Pre-existing user_store_installs keep getting the entity in
        list_for_user even after archive (drives marketplace.zip serve)."""
        from src.repositories.store_entities import StoreEntitiesRepository
        from src.repositories.user_store_installs import UserStoreInstallsRepository
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="arch-6")

        # Other user installs while approved.
        other_id, other_cookies = _create_user(web_client, "other@x.com")
        web_client.post(f"/api/store/entities/{eid}/install", cookies=other_cookies)

        # Owner archives.
        web_client.delete(f"/api/store/entities/{eid}", cookies=owner_cookies)

        # Other user's stack still has it.
        conn = get_system_db()
        installs = UserStoreInstallsRepository(conn).list_for_user(other_id)
        ids = {r["id"] for r in installs}
        assert eid in ids, "archived entity must still serve to existing installs"
        # And carries archived flag for the badge.
        row = next(r for r in installs if r["id"] == eid)
        assert row["visibility_status"] == "archived"
        conn.close()

    def test_owner_cannot_archive_quarantined(self, web_client):
        """Owner Delete on quarantined still refused (existing v32 policy)."""
        user_id, user_cookies = _create_user(web_client, "u@x.com")
        eid, _sub_id = _seed_quarantined_entity(user_id, "u@x.com", "q-arch")

        r = web_client.delete(f"/api/store/entities/{eid}", cookies=user_cookies)
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "quarantined_owner_cannot_delete"

    def test_admin_can_archive_quarantined(self, web_client):
        """Admin can archive a quarantined entity (separate from override
        + hard-delete paths — admin keeps full control)."""
        from src.repositories.store_entities import StoreEntitiesRepository
        user_id, _ = _create_user(web_client, "u@x.com")
        eid, _sub_id = _seed_quarantined_entity(user_id, "u@x.com", "q-arch2")

        _, admin_cookies = _create_admin(web_client)
        r = web_client.delete(f"/api/store/entities/{eid}", cookies=admin_cookies)
        assert r.status_code == 204, r.text

        conn = get_system_db()
        ent = StoreEntitiesRepository(conn).get(eid)
        assert ent["visibility_status"] == "archived"
        conn.close()

    def test_owners_endpoint_filters_quarantined_for_non_admin(self, web_client):
        """A user with only quarantined uploads must NOT appear in the
        public /api/store/owners dropdown."""
        user_id, _ = _create_user(web_client, "spammer@x.com")
        _seed_quarantined_entity(user_id, "spammer@x.com", "only-bad")

        _, other_cookies = _create_user(web_client, "other@x.com")
        r = web_client.get("/api/store/owners", cookies=other_cookies)
        assert r.status_code == 200
        owner_ids = {o["user_id"] for o in r.json()}
        assert "spammer" not in owner_ids

    def test_categories_endpoint_filters_quarantined_for_non_owner(self, web_client):
        """`/api/marketplace/categories?tab=flea` aggregates per-category
        counts. The visibility predicate is duplicated inline in
        marketplace.py (drift risk against repo); this test locks the
        parity with marketplace items so a future change to the repo
        clause that misses the inline copy gets caught."""
        owner_id, owner_cookies = _create_user(web_client, "qcat-owner@x.com")
        _seed_quarantined_entity(owner_id, "qcat-owner@x.com", "qcat")

        _, snoop_cookies = _create_user(web_client, "qcat-snoop@x.com")
        r = web_client.get(
            "/api/marketplace/categories?tab=flea", cookies=snoop_cookies,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        total = sum(c.get("count", 0) for c in body.get("items", []))
        assert total == 0, (
            "non-owner saw quarantined entry counted in /categories: "
            f"{body}"
        )

        r = web_client.get(
            "/api/marketplace/categories?tab=flea", cookies=owner_cookies,
        )
        body = r.json()
        owner_total = sum(c.get("count", 0) for c in body.get("items", []))
        assert owner_total >= 1, (
            "owner should count own quarantined entry in /categories: "
            f"{body}"
        )


# ---------------------------------------------------------------------------
# v35 lifecycle marking on submissions (archived / deleted)
# ---------------------------------------------------------------------------


class TestSubmissionLifecycleMarking:
    def _upload_clean(self, web_client, cookies, name="lc"):
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip(name), "application/zip")},
            data={"type": "skill"}, cookies=cookies,
        )
        assert c.status_code == 201, c.text
        return c.json()["id"]

    def test_archive_surfaces_via_entity_visibility_join(self, web_client):
        """Verdict on submission stays immutable; archived chip surfaces the row
        because store_entities.visibility_status flipped to 'archived'.
        Locks in the JOIN-based architecture replacing the prior denormalization."""
        from src.repositories.store_submissions import StoreSubmissionsRepository
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="lc-arch")
        conn = get_system_db()
        sub = StoreSubmissionsRepository(conn).latest_for_entity(eid)
        sid = sub["id"]
        assert sub["status"] == "approved"
        conn.close()

        web_client.delete(f"/api/store/entities/{eid}", cookies=owner_cookies)

        # Verdict UNCHANGED — that's the whole point: submission.status is the
        # forensic record of what was decided at review time, not lifecycle.
        conn = get_system_db()
        sub = StoreSubmissionsRepository(conn).get(sid)
        assert sub["status"] == "approved"
        conn.close()

        # But the JOIN-based archived chip surfaces it because the entity's
        # visibility_status flipped.
        _, admin_cookies = _create_admin(web_client)
        r = web_client.get(
            "/api/admin/store/submissions?status=archived", cookies=admin_cookies,
        )
        assert r.status_code == 200
        names = {s["name"] for s in r.json()["items"]}
        assert "lc-arch" in names

    def test_archived_chip_surfaces_entity_archived_outside_delete_flow(self, web_client):
        """Regression for the user-reported bug: archived entity didn't show up
        in ?status=archived because the prior denormalized field never flipped.
        Pre-seed a submission with status='approved' and manually flip the
        linked entity to visibility_status='archived' (simulating any code path
        that bypasses the soft-delete API), then assert the archived chip
        surfaces it."""
        from src.repositories.store_entities import StoreEntitiesRepository
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="lc-bypass-archive")

        # Bypass the API: flip visibility directly at the repo layer.
        conn = get_system_db()
        StoreEntitiesRepository(conn).set_visibility(eid, "archived")
        conn.close()

        _, admin_cookies = _create_admin(web_client)
        r = web_client.get(
            "/api/admin/store/submissions?status=archived", cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text
        names = {s["name"] for s in r.json()["items"]}
        assert "lc-bypass-archive" in names

        # And the default queue excludes it.
        r = web_client.get("/api/admin/store/submissions", cookies=admin_cookies)
        names = {s["name"] for s in r.json()["items"]}
        assert "lc-bypass-archive" not in names

    def test_hard_delete_marks_submissions_deleted(self, web_client):
        from src.repositories.store_submissions import StoreSubmissionsRepository
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="lc-del")
        conn = get_system_db()
        sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()

        _, admin_cookies = _create_admin(web_client)
        r = web_client.delete(f"/api/store/entities/{eid}?hard=true", cookies=admin_cookies)
        assert r.status_code == 204, r.text

        conn = get_system_db()
        sub = StoreSubmissionsRepository(conn).get(sub_id)
        assert sub["status"] == "deleted"
        # entity_id is preserved as a tombstone — the live entity row
        # is gone, but keeping the pointer lets the detail page
        # resolve the activity timeline by querying audit_log for
        # `store_entity:{entity_id}` even after the row is dropped.
        assert sub["entity_id"] == eid
        conn.close()

    def test_default_listing_excludes_archived_and_deleted(self, web_client):
        """Admin queue defaults to actionable rows. Archived + deleted
        only surface when the user clicks the dedicated chip."""
        from src.repositories.store_submissions import StoreSubmissionsRepository
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid_arch = self._upload_clean(web_client, owner_cookies, name="lc-d-arch")
        self._upload_clean(web_client, owner_cookies, name="lc-d-keep")
        web_client.delete(f"/api/store/entities/{eid_arch}", cookies=owner_cookies)

        _, admin_cookies = _create_admin(web_client)
        r = web_client.get("/api/admin/store/submissions", cookies=admin_cookies)
        assert r.status_code == 200
        names = {s["name"] for s in r.json()["items"]}
        assert "lc-d-keep" in names
        assert "lc-d-arch" not in names

        # Explicit chip surfaces it.
        r = web_client.get(
            "/api/admin/store/submissions?status=archived", cookies=admin_cookies,
        )
        names = {s["name"] for s in r.json()["items"]}
        assert names == {"lc-d-arch"}

    def test_deleted_chip_surfaces_hard_deleted(self, web_client):
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="lc-d-hard")
        _, admin_cookies = _create_admin(web_client)
        web_client.delete(f"/api/store/entities/{eid}?hard=true", cookies=admin_cookies)

        r = web_client.get(
            "/api/admin/store/submissions?status=deleted", cookies=admin_cookies,
        )
        names = {s["name"] for s in r.json()["items"]}
        assert "lc-d-hard" in names

    def test_deleted_submission_detail_renders_timeline(self, web_client):
        """Regression: hard-deleted submissions used to lose their activity
        timeline because mark_deleted_for_entity nulled entity_id, severing
        the audit_log linkage. Tombstone semantics: entity_id is preserved
        post-delete so `store_entity:{entity_id}` audits keep resolving."""
        from src.repositories.store_submissions import StoreSubmissionsRepository
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="lc-d-timeline")

        conn = get_system_db()
        sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()

        _, admin_cookies = _create_admin(web_client)
        r = web_client.delete(f"/api/store/entities/{eid}?hard=true", cookies=admin_cookies)
        assert r.status_code == 204, r.text

        # Detail page must render and include at least one audit row
        # (creation events scoped to store_entity:{eid} would otherwise
        # be invisible after the entity_id link was nulled).
        r = web_client.get(
            f"/admin/store/submissions/{sub_id}", cookies=admin_cookies,
        )
        assert r.status_code == 200
        # Sanity: the deleted submission's body references the original
        # entity_id (the tombstone), proving the linkage survives.
        assert eid in r.text


# ---------------------------------------------------------------------------
# Coverage gap fills (post-pre-PR audit)
# ---------------------------------------------------------------------------


class TestFleaDetailSubmissionStatusField:
    """The quarantine banner's auto-refresh JS polls
    `/api/marketplace/flea/{id}/detail` and reloads when
    `submission_status` flips off the pending verdicts. Visibility
    alone is insufficient because `blocked_llm` keeps the entity at
    `visibility_status='pending'`. This class locks in the contract
    that the field is populated for owner/admin only."""

    def _upload_clean(self, web_client, cookies, name="dst"):
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip(name), "application/zip")},
            data={"type": "skill"}, cookies=cookies,
        )
        assert c.status_code == 201, c.text
        return c.json()["id"]

    def test_owner_sees_submission_status(self, web_client):
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="dst-own")
        r = web_client.get(
            f"/api/marketplace/flea/{eid}/detail", cookies=owner_cookies,
        )
        assert r.status_code == 200
        body = r.json()
        # Verdict landed by the time create returned 201 (clean upload skips
        # LLM since fixtures don't carry a real api key) — value is whatever
        # status the runner left; just assert the field is populated.
        assert body.get("submission_status") is not None

    def test_admin_sees_submission_status_for_any_entity(self, web_client):
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="dst-adm")
        _, admin_cookies = _create_admin(web_client)
        r = web_client.get(
            f"/api/marketplace/flea/{eid}/detail", cookies=admin_cookies,
        )
        assert r.status_code == 200
        assert r.json().get("submission_status") is not None

    def test_other_user_does_not_see_submission_status(self, web_client):
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="dst-other")
        # Non-owner non-admin can hit the endpoint when entity is approved
        # (404 otherwise per the visibility gate). Field must stay null.
        _, viewer_cookies = _create_user(web_client, "viewer@x.com")
        r = web_client.get(
            f"/api/marketplace/flea/{eid}/detail", cookies=viewer_cookies,
        )
        if r.status_code == 200:
            assert r.json().get("submission_status") is None
        # If the entity isn't approved (404 for non-owner non-admin),
        # the leak isn't reachable either way — also acceptable.


class TestDetailPageEntityLifecycleRow:
    """The submission detail page renders Status (verdict) and Entity
    lifecycle side by side so admins see the verdict-vs-lifecycle
    distinction at a glance. Locks in the row's presence so a future
    template refactor can't silently drop it."""

    def _upload_clean(self, web_client, cookies, name="elr"):
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip(name), "application/zip")},
            data={"type": "skill"}, cookies=cookies,
        )
        assert c.status_code == 201, c.text
        return c.json()["id"]

    def test_detail_renders_entity_lifecycle_row(self, web_client):
        from src.repositories.store_submissions import StoreSubmissionsRepository
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="elr-1")
        conn = get_system_db()
        sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        conn.close()
        _, admin_cookies = _create_admin(web_client)
        r = web_client.get(
            f"/admin/store/submissions/{sub_id}", cookies=admin_cookies,
        )
        assert r.status_code == 200
        # Both labels must coexist on the page.
        assert "Status (verdict)" in r.text
        assert "Entity lifecycle" in r.text


class TestAuditLogResourcePrefix:
    """Locks in the contract that submission-event audits land at
    resources the activity-timeline query knows how to find. Two
    paths emit them:

      * `app/api/store.py:_audit` helper hardcodes `store_entity:`
        prefix — submission events written this way live at
        `store_entity:{sub_id}`. Timeline query covers it.
      * `src/store_guardrails/runner.py` uses
        `store_submission:{sub_id}` — the post-fix convention.
        Timeline query covers it.

    Either format must surface in the rendered detail page so admins
    can audit the lifecycle of any submission."""

    def _upload_clean(self, web_client, cookies, name="aud"):
        c = web_client.post(
            "/api/store/entities",
            files={"file": ("s.zip", _make_skill_zip(name), "application/zip")},
            data={"type": "skill"}, cookies=cookies,
        )
        assert c.status_code == 201, c.text
        return c.json()["id"]

    def test_helper_emitted_audits_surface_in_timeline(self, web_client):
        """The `_audit` helper writes resource=`store_entity:{sub_id}`
        for submission events. Timeline query must include that
        pattern so `store.submission.accepted` (or `.approved`) rows
        are visible on the detail page."""
        from src.repositories.store_submissions import StoreSubmissionsRepository
        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="aud-helper")
        conn = get_system_db()
        sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        rows = conn.execute(
            """SELECT resource, action FROM audit_log
                WHERE resource = ?
                  AND action LIKE 'store.submission.%'""",
            [f"store_entity:{sub_id}"],
        ).fetchall()
        conn.close()
        assert rows, (
            "expected at least one store.submission.* audit row at "
            "resource=store_entity:{sub_id} — the helper format the "
            "timeline query relies on"
        )

        # And the rendered timeline must include the action.
        _, admin_cookies = _create_admin(web_client)
        r = web_client.get(
            f"/admin/store/submissions/{sub_id}", cookies=admin_cookies,
        )
        assert r.status_code == 200
        # Either accepted (guardrails on) or approved (guardrails off);
        # both appear in rows returned by the timeline query when the
        # query covers the helper's `store_entity:` resource pattern.
        assert any(a in r.text for a in (
            "store.submission.accepted",
            "store.submission.approved",
        ))

    def test_runner_audit_uses_prefixed_resource(self, monkeypatch, web_client):
        """runner.py's audit calls must use `store_submission:{id}` —
        we drive that path by faking a config-loader failure inside
        run_llm_review so the runner emits a `review_error` audit
        synchronously."""
        from src.repositories.store_submissions import StoreSubmissionsRepository
        from src.store_guardrails import runner as runner_mod

        _, owner_cookies = _create_user(web_client, "owner@x.com")
        eid = self._upload_clean(web_client, owner_cookies, name="aud-runner")
        conn = get_system_db()
        sub_id = StoreSubmissionsRepository(conn).latest_for_entity(eid)["id"]
        plugin_dir = Path(get_store_dir()) / eid / "plugin"
        conn.close()

        def boom() -> str:
            raise RuntimeError("missing api key for test")

        runner_mod.run_llm_review(
            submission_id=sub_id,
            plugin_dir=plugin_dir,
            conn_factory=get_system_db,
            api_key_loader=boom,
            model_loader=lambda: "claude-haiku-4-5-20251001",
        )

        conn = get_system_db()
        rows = conn.execute(
            """SELECT resource, action FROM audit_log
                WHERE resource = ?
                  AND action = 'store.submission.review_error'""",
            [f"store_submission:{sub_id}"],
        ).fetchall()
        conn.close()
        assert rows, (
            "runner.py must emit prefixed store_submission:{id} so the "
            "timeline query resolves it — bare-id format is legacy only"
        )


class TestListPageFilterDropdowns:
    """Custom design-system dropdown on the list page's Type / Per page
    filters (#1055). `#f-type` / `#f-limit` stay real `<select>`s in the DOM
    (the GET form submits their `.value` unchanged) with a `ds.dropdown()`
    custom button+menu alongside each. Visibility between the two is a CSS
    theme decision (paper-skin.css), not a template one.
    """

    def test_native_selects_still_render_for_form_submission(self, web_client):
        _, admin_cookies = _create_admin(web_client)
        r = web_client.get("/admin/store/submissions", cookies=admin_cookies)
        assert r.status_code == 200
        text = r.text
        assert '<select id="f-type" name="type" class="ds-dropdown-native">' in text
        assert '<select id="f-limit" name="limit" class="ds-dropdown-native">' in text

    def test_custom_dropdown_markup_present(self, web_client):
        _, admin_cookies = _create_admin(web_client)
        r = web_client.get("/admin/store/submissions", cookies=admin_cookies)
        assert r.status_code == 200
        text = r.text
        assert 'data-ds-dropdown-target="f-type"' in text
        assert 'data-ds-dropdown-target="f-limit"' in text
        assert 'id="f-type-dd-btn"' in text
        assert 'id="f-limit-dd-btn"' in text
        assert 'role="menu"' in text
        assert 'role="menuitemradio"' in text
        for value in ("", "skill", "agent", "plugin"):
            assert f'data-value="{value}"' in text
        for value in ("25", "50", "100", "200"):
            assert f'data-value="{value}"' in text

    def test_dropdown_reflects_active_filters(self, web_client):
        """When `?type=agent&limit=100` is bookmarked, both the native
        select and the custom dropdown's initial state must agree."""
        _, admin_cookies = _create_admin(web_client)
        r = web_client.get(
            "/admin/store/submissions?type=agent&limit=100",
            cookies=admin_cookies,
        )
        assert r.status_code == 200
        text = r.text
        assert '<option value="agent"  selected>Agent</option>' in text
        assert '<option value="100" selected>100</option>' in text

    def test_dropdown_js_module_is_loaded(self, web_client):
        _, admin_cookies = _create_admin(web_client)
        r = web_client.get("/admin/store/submissions", cookies=admin_cookies)
        assert r.status_code == 200
        assert "js/components/ds_dropdown.js" in r.text


def test_the_withdrawal_gate_reads_the_shared_status_list():
    """Devin Review on #1263: two copies of "review rejected it" would drift.

    `_entity_review_blocked` and `user_store_installs.list_for_user` both
    decide from `BLOCKING_SUBMISSION_STATUSES`; the withdrawal gate must not
    keep its own.
    """
    import inspect

    from app.api import store
    from src.repositories.store_submissions import BLOCKING_SUBMISSION_STATUSES

    # One function answers it now, and both the endpoint and the detail page
    # call it — see `entity_has_adverse_verdict`.
    assert "BLOCKING_SUBMISSION_STATUSES" in inspect.getsource(store.entity_has_adverse_verdict)
    assert "entity_has_adverse_verdict(" in inspect.getsource(store.delete_entity)
    assert "blocked_llm" in BLOCKING_SUBMISSION_STATUSES


class TestTheArchiveButtonAgreesWithTheEndpoint:
    """Devin Review on #1263: the API allowed the withdrawal, the page did not.

    The author only ever sees the button, so an endpoint that permits what the
    template hides is the same trap in a different place — and this change set
    exists to give the author an exit.
    """

    def test_the_owner_sees_archive_on_an_unjudged_upload(self, web_client):
        user_id, cookies = _create_user(web_client, "exit@x.com")
        entity_id, _ = _seed_quarantined_entity(user_id, "exit@x.com", "unjudged", status="review_error")

        page = web_client.get(f"/marketplace/flea/{entity_id}", cookies=cookies)
        assert page.status_code == 200, page.text[:200]
        assert 'id="owner-archive-btn"' in page.text, "the author has no visible way out"

    def test_a_flagged_upload_keeps_the_button_locked(self, web_client):
        user_id, cookies = _create_user(web_client, "flagged@x.com")
        entity_id, _ = _seed_quarantined_entity(user_id, "flagged@x.com", "flagged")

        page = web_client.get(f"/marketplace/flea/{entity_id}", cookies=cookies)
        assert page.status_code == 200
        assert 'id="owner-archive-btn"' not in page.text, "review objected; the button must stay locked"
