"""Tests for the skill-linter API surfaces (v89, Task 6):

* ``POST /api/store/entities/from-markdown`` with ``dry_run=true``
* the post-publish lint hook (``create_entity`` background task)
* ``app/api/store_lint_admin.py`` — findings list, manual audit, dismiss

LLM: no ``ANTHROPIC_API_KEY``/``LLM_API_KEY`` in the test env, so
``default_craft_caller()`` returns ``None`` and every lint call degrades to
the SL011/SL012 heuristics automatically — no Anthropic mocking needed.

Helper pattern shared with ``test_store_api.py`` (plain functions imported
from there; the client fixture is local because pytest resolves fixtures
per-module — see ``test_marketplace_required_tier.py`` for precedent).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.test_store_api import _OK_DESC, _create_user

# Long enough to clear the 200-char body floor but well under the 8000-char
# SL002 threshold.
_OK_BODY = (
    "Body explaining when to invoke the component, what inputs it needs, "
    "and the behavior contract. Long enough to clear the 200-char body floor. "
    "Repeated content for length."
) * 2

# Comfortably over the default `lint_max_body_chars` (8000) so SL002 fires.
_HUGE_BODY = "word " * 2000  # 10_000 chars


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


def _make_admin(client: TestClient, email: str = "admin@x.com", password: str = "AdminPass1!"):
    """Create a user and grant Admin group membership; return (user_id, cookies)."""
    from argon2 import PasswordHasher
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from tests.helpers.auth import grant_admin

    ph = PasswordHasher()
    conn = get_system_db()
    user_id = email.split("@")[0]
    UserRepository(conn).create(
        id=user_id,
        email=email,
        name=user_id,
        password_hash=ph.hash(password),
    )
    grant_admin(conn, user_id)
    conn.close()
    r = client.post("/auth/token", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return user_id, {"access_token": r.json()["access_token"]}


def _publish(client, cookies, **overrides):
    payload = {
        "name": "md-first-skill",
        "description": _OK_DESC,
        "skill_md": _OK_BODY,
    }
    payload.update(overrides)
    return client.post("/api/store/entities/from-markdown", json=payload, cookies=cookies)


class TestDryRunLint:
    def test_dry_run_returns_lint_block_and_writes_nothing(self, web_client):
        _, cookies = _create_user(web_client, "alice@x.com")

        r = _publish(web_client, cookies, dry_run=True)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["dry_run"] is True
        assert "inline" in body
        assert "lint" in body
        assert set(body["lint"].keys()) >= {"findings", "rules_run", "llm_used", "content_hash"}

        # No DB writes: the store listing is empty and a real publish of the
        # same name still succeeds afterwards (no leftover (owner, name) row).
        listing = web_client.get("/api/store/entities", cookies=cookies)
        assert listing.status_code == 200
        assert listing.json()["total"] == 0

        r2 = _publish(web_client, cookies, dry_run=False)
        assert r2.status_code == 201, r2.text

    def test_dry_run_flags_sl002_for_oversized_body(self, web_client):
        _, cookies = _create_user(web_client, "bob@x.com")

        r = _publish(web_client, cookies, dry_run=True, skill_md=_HUGE_BODY)
        assert r.status_code == 200, r.text
        lint = r.json()["lint"]
        rule_ids = {f["rule_id"] for f in lint["findings"]}
        assert "SL002" in rule_ids

    def test_dry_run_requires_auth(self, web_client):
        r = web_client.post(
            "/api/store/entities/from-markdown",
            json={"name": "x", "skill_md": _OK_BODY, "dry_run": True},
        )
        assert r.status_code in (401, 403)


class TestPostPublishLint:
    def test_publish_persists_findings_with_publish_trigger(self, web_client):
        _, cookies = _create_user(web_client, "carol@x.com")

        # _OK_DESC already contains "Use when" trigger phrasing, so SL011
        # wouldn't fire on it — use a description that lacks any trigger
        # phrase so the degraded-mode SL011 finding is guaranteed to land.
        no_trigger_desc = "A skill for reviewing pull requests and summarizing code changes across a repo"
        r = _publish(web_client, cookies, description=no_trigger_desc)
        assert r.status_code == 201, r.text
        entity_id = r.json()["id"]

        # BackgroundTasks run synchronously under TestClient — by the time
        # the response comes back, _run_publish_lint has already persisted.
        from src.repositories import store_lint_repo

        repo = store_lint_repo()
        findings = repo.latest_findings(entity_id)
        assert any(f["rule_id"] == "SL011" for f in findings)
        last_run = repo.last_run(trigger="publish")
        assert last_run is not None
        assert last_run["entities_linted"] == 1

    def test_publish_lint_persists_even_when_oversized(self, web_client):
        _, cookies = _create_user(web_client, "dave@x.com")

        r = _publish(web_client, cookies, skill_md=_HUGE_BODY)
        assert r.status_code == 201, r.text
        entity_id = r.json()["id"]

        from src.repositories import store_lint_repo

        rule_ids = {f["rule_id"] for f in store_lint_repo().latest_findings(entity_id)}
        assert "SL002" in rule_ids


class TestAdminLintFindings:
    def test_non_admin_forbidden(self, web_client):
        _, cookies = _create_user(web_client, "erin@x.com")
        r = web_client.get("/api/admin/store/lint-findings", cookies=cookies)
        assert r.status_code == 403

    def test_admin_lists_findings(self, web_client):
        _, cookies = _create_user(web_client, "frank@x.com")
        # No trigger phrase in the description guarantees the post-publish
        # hook's degraded-mode SL011 finding lands (this entity has no
        # corpus of other published skills to compare against, so SL012
        # never fires, and the body isn't oversized, so SL002 doesn't
        # either — SL011 is the one rule guaranteed to produce a finding).
        no_trigger_desc = "A skill for reviewing pull requests and summarizing code changes across a repo"
        pub = _publish(web_client, cookies, description=no_trigger_desc)
        assert pub.status_code == 201, pub.text

        _, admin_cookies = _make_admin(web_client)
        r = web_client.get("/api/admin/store/lint-findings", cookies=admin_cookies)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "findings" in body
        assert "last_run" in body
        assert any(f["entity_id"] == pub.json()["id"] for f in body["findings"])


class TestAdminLintAudit:
    def test_non_admin_forbidden(self, web_client):
        _, cookies = _create_user(web_client, "grace@x.com")
        r = web_client.post("/api/admin/store/lint-audit", json={}, cookies=cookies)
        assert r.status_code == 403

    def test_audit_happy_path(self, web_client):
        owner_id, owner_cookies = _create_user(web_client, "heidi@x.com")
        pub = _publish(web_client, owner_cookies)
        assert pub.status_code == 201, pub.text
        entity_id = pub.json()["id"]

        # Publish the entity so it's visible to load_corpus()/the audit loop
        # (visibility_status='approved').
        from src.db import get_system_db
        from src.repositories.store_entities import StoreEntitiesRepository

        conn = get_system_db()
        StoreEntitiesRepository(conn).set_visibility(entity_id, "approved")
        conn.close()

        _, admin_cookies = _make_admin(web_client)
        r = web_client.post("/api/admin/store/lint-audit", json={"force": True}, cookies=admin_cookies)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("skipped") is not True
        # The post-publish hook already linted this entity with the exact
        # same content_hash, so the audit's unchanged-content skip kicks in
        # (carry_forward re-tags the existing findings to this run instead
        # of re-linting identical bytes) — either outcome accounts for the
        # one published skill.
        assert body["entities_linted"] + body["entities_skipped"] == 1
        assert body["trigger"] == "admin"

    def test_audit_self_guard_skips_within_interval(self, web_client):
        _, admin_cookies = _make_admin(web_client)

        first = web_client.post("/api/admin/store/lint-audit", json={"force": True}, cookies=admin_cookies)
        assert first.status_code == 200, first.text
        assert first.json().get("skipped") is not True

        second = web_client.post("/api/admin/store/lint-audit", json={}, cookies=admin_cookies)
        assert second.status_code == 200, second.text
        assert second.json()["skipped"] is True
        assert second.json()["last_run"] is not None

    def test_scheduler_token_audit_is_labeled_scheduler(self, web_client, monkeypatch):
        # The scheduler sidecar authenticates with SCHEDULER_API_TOKEN and sends
        # NO custom header, so the run's trigger label depends entirely on
        # resolving the synthetic scheduler principal. Pin it: a bodyless POST
        # with that token must record trigger='scheduler', not 'admin'.
        token = "scheduler-shared-secret-token-min-len-32chars"
        monkeypatch.setenv("SCHEDULER_API_TOKEN", token)

        r = web_client.post(
            "/api/admin/store/lint-audit",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("skipped") is not True
        assert body["trigger"] == "scheduler"

    def test_audit_accepts_bodyless_post(self, web_client):
        # The scheduler POSTs with no request body — that must be a valid
        # (defaulted force=false) call, not a 422. Regression for the dead
        # weekly-audit bug.
        _, admin_cookies = _make_admin(web_client)
        r = web_client.post("/api/admin/store/lint-audit", cookies=admin_cookies)
        assert r.status_code == 200, r.text
        assert "skipped" in r.json() or "entities_linted" in r.json()

    def test_self_guard_ignores_per_publish_runs(self, web_client):
        # A skill publish writes a trigger='publish' run. That must NOT satisfy
        # the audit self-guard — otherwise routine publishing starves the
        # scheduled retro-audit. A bodyless (non-force) audit right after a
        # publish must still run.
        _, owner_cookies = _create_user(web_client, "ivan@x.com")
        assert _publish(web_client, owner_cookies).status_code == 201

        _, admin_cookies = _make_admin(web_client)
        r = web_client.post("/api/admin/store/lint-audit", cookies=admin_cookies)
        assert r.status_code == 200, r.text
        assert r.json().get("skipped") is not True

    def test_audit_force_overrides_self_guard(self, web_client):
        _, admin_cookies = _make_admin(web_client)

        first = web_client.post("/api/admin/store/lint-audit", json={"force": True}, cookies=admin_cookies)
        assert first.status_code == 200, first.text

        second = web_client.post("/api/admin/store/lint-audit", json={"force": True}, cookies=admin_cookies)
        assert second.status_code == 200, second.text
        assert second.json().get("skipped") is not True


class TestAdminLintDismiss:
    def test_non_admin_forbidden(self, web_client):
        _, cookies = _create_user(web_client, "ivan@x.com")
        r = web_client.post(
            "/api/admin/store/lint-dismiss",
            json={"entity_id": "x", "rule_id": "SL002"},
            cookies=cookies,
        )
        assert r.status_code == 403

    def test_unknown_finding_404s(self, web_client):
        _, admin_cookies = _make_admin(web_client)
        r = web_client.post(
            "/api/admin/store/lint-dismiss",
            json={"entity_id": "nonexistent", "rule_id": "SL002"},
            cookies=admin_cookies,
        )
        assert r.status_code == 404
        assert r.json()["detail"] == "finding_not_found"

    def test_dismiss_hides_finding_and_audit_resurrects_after_content_change(self, web_client):
        """Dismiss via the admin API, then prove the resurrection half of
        the hash-pinning contract through the ADMIN AUDIT endpoint (not a
        raw repo call) — the two Task 6 surfaces working together.

        The re-lint-on-edit hook lives on ``update_entity`` (PUT), which is
        out of this task's scope, so we mimic "the on-disk bundle changed"
        by editing the live ``SKILL.md`` directly, then let
        ``POST /lint-audit`` discover the content_hash drift the way a real
        scheduler tick would.
        """
        _, owner_cookies = _create_user(web_client, "judy@x.com")
        pub = _publish(web_client, owner_cookies, skill_md=_HUGE_BODY)
        assert pub.status_code == 201, pub.text
        entity_id = pub.json()["id"]

        from src.db import get_system_db
        from src.repositories import store_lint_repo
        from src.repositories.store_entities import StoreEntitiesRepository

        conn = get_system_db()
        StoreEntitiesRepository(conn).set_visibility(entity_id, "approved")
        conn.close()

        repo = store_lint_repo()
        findings = repo.latest_findings(entity_id)
        assert any(f["rule_id"] == "SL002" for f in findings)

        _, admin_cookies = _make_admin(web_client)
        r = web_client.post(
            "/api/admin/store/lint-dismiss",
            json={"entity_id": entity_id, "rule_id": "SL002"},
            cookies=admin_cookies,
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"dismissed": True}

        visible = repo.latest_findings(entity_id, include_dismissed=False)
        assert not any(f["rule_id"] == "SL002" for f in visible)

        # Edit the live bundle directly (still oversized, so SL002 keeps
        # firing, but the bytes — and therefore the content_hash — differ
        # from what the dismissal was pinned against).
        from app.api.store import _find_skill_md, _plugin_dir

        skill_md_path = _find_skill_md(_plugin_dir(entity_id))
        assert skill_md_path is not None
        skill_md_path.write_text(
            skill_md_path.read_text(encoding="utf-8") + "\nedited to change the content hash\n",
            encoding="utf-8",
        )

        audit = web_client.post("/api/admin/store/lint-audit", json={"force": True}, cookies=admin_cookies)
        assert audit.status_code == 200, audit.text
        assert audit.json().get("skipped") is not True
        assert audit.json()["entities_linted"] == 1  # hash drift -> NOT the skip path

        resurrected = repo.latest_findings(entity_id, include_dismissed=False)
        assert any(f["rule_id"] == "SL002" for f in resurrected)
