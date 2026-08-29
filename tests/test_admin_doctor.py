"""Tests for POST /api/admin/doctor/new-instance — the deployment gate.

Every check in this doctor exists because the corresponding thing silently
failed on a real new-instance deployment and cost hours of debugging:

- login-door: seed-admin failures end in one ``logger.warning`` (app/main.py),
  so an instance can boot with NO usable way to sign in.
- email-delivery: the send path returns HTTP 200 while the relay silently
  drops mail from the default ``noreply@example.com`` sender, and the
  SendGrid SDK branch cannot work at all (package not shipped).
- chat-grant: ``chat.enabled`` without a ``(group, chat, chat)`` grant hides
  chat from everyone including admins (god-mode intentionally does not
  surface the entry point — see ``_compute_can_chat``).
- agent-scope: an agent whose owner-grants ∩ scope is empty answers every
  data question with 403 "not in your stack".
- app-state-backend: since A1, fresh installs run app-state on Postgres
  (``side_car``/``cloud``); DuckDB is legacy-only for new deploys. See
  tests/test_doctor_backend.py for the dedicated coverage.
- branding: ``instance.brand`` set while the rendered login page still shows
  a default title (the title reads ``instance.name``, a different knob).
"""

from types import SimpleNamespace
from unittest.mock import patch


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _run(client, token, body=None):
    resp = client.post("/api/admin/doctor/new-instance", headers=_auth(token), json=body or {})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _check(report, name):
    matches = [c for c in report["checks"] if c["name"] == name]
    assert matches, f"check {name!r} missing from report: {[c['name'] for c in report['checks']]}"
    return matches[0]


class TestDoctorAuth:
    def test_unauthenticated_is_rejected(self, seeded_app):
        resp = seeded_app["client"].post("/api/admin/doctor/new-instance", json={})
        assert resp.status_code == 401

    def test_non_admin_is_rejected(self, seeded_app):
        resp = seeded_app["client"].post(
            "/api/admin/doctor/new-instance",
            headers=_auth(seeded_app["analyst_token"]),
            json={},
        )
        assert resp.status_code == 403

    def test_admin_can_run(self, seeded_app):
        report = _run(seeded_app["client"], seeded_app["admin_token"])
        assert report["status"] in ("ok", "warning", "error")


class TestDoctorShape:
    def test_all_checks_present_with_diagnose_vocabulary(self, seeded_app):
        report = _run(seeded_app["client"], seeded_app["admin_token"])
        names = [c["name"] for c in report["checks"]]
        assert names == [
            "login-door",
            "email-delivery",
            "chat-grant",
            "agent-scope",
            "app-state-backend",
            "branding",
        ]
        for c in report["checks"]:
            assert c["status"] in ("ok", "warning", "error", "info"), c
            assert c["audience"] == "operator"
            assert isinstance(c["detail"], str) and c["detail"]

    def test_overall_status_aggregates_worst(self, seeded_app):
        # The seeded fixture has no password on any user, no OAuth env and no
        # SMTP -> login-door must be an error, and the headline must say so.
        report = _run(seeded_app["client"], seeded_app["admin_token"])
        assert _check(report, "login-door")["status"] == "error"
        assert report["status"] == "error"

    def test_one_crashing_check_does_not_kill_the_report(self, seeded_app, monkeypatch):
        import app.services.instance_doctor as doctor

        def _boom():
            raise RuntimeError("resolver exploded")

        monkeypatch.setattr(doctor, "check_agent_scope", _boom)
        report = _run(seeded_app["client"], seeded_app["admin_token"])
        agent_check = _check(report, "agent-scope")
        assert agent_check["status"] == "error"
        assert "resolver exploded" in agent_check["detail"]
        # The other checks still reported.
        assert len(report["checks"]) == 6


class TestLoginDoor:
    def test_no_usable_door_is_error_with_bootstrap_hint(self, seeded_app):
        report = _run(seeded_app["client"], seeded_app["admin_token"])
        check = _check(report, "login-door")
        assert check["status"] == "error"
        assert "/auth/bootstrap" in check["detail"] or "SEED_ADMIN_PASSWORD" in check["detail"]

    def test_password_holder_opens_the_door(self, seeded_app):
        from src.repositories import users_repo

        users_repo().update(id="admin1", password_hash="argon2-hash-placeholder")
        report = _run(seeded_app["client"], seeded_app["admin_token"])
        check = _check(report, "login-door")
        assert check["status"] == "ok"
        assert "password" in check["detail"]

    def test_scheduler_user_password_does_not_count(self, seeded_app):
        from app.auth.scheduler_token import SCHEDULER_USER_EMAIL
        from src.db import get_system_db
        from src.repositories.users import UserRepository

        conn = get_system_db()
        UserRepository(conn).create(id="sched1", email=SCHEDULER_USER_EMAIL, name="Scheduler", password_hash="hash")
        conn.close()
        report = _run(seeded_app["client"], seeded_app["admin_token"])
        assert _check(report, "login-door")["status"] == "error"

    def test_email_only_door_is_warning_until_delivery_verified(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        # Email is opt-in-only by default (B6) — this scenario ("the ONLY
        # login door is email") now only arises once an operator has
        # explicitly named it in auth.providers.
        monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "email")
        report = _run(seeded_app["client"], seeded_app["admin_token"])
        check = _check(report, "login-door")
        assert check["status"] == "warning"
        assert "email" in check["detail"]


class TestEmailDelivery:
    def test_no_transport_is_info(self, seeded_app):
        report = _run(seeded_app["client"], seeded_app["admin_token"])
        check = _check(report, "email-delivery")
        assert check["status"] == "info"
        assert "SMTP_HOST" in check["detail"]

    def test_sendgrid_key_without_package_is_error(self, seeded_app, monkeypatch):
        # The sendgrid python package is deliberately not a dependency of this
        # project, so setting only the key produces a login page that offers
        # magic links while every send raises ModuleNotFoundError.
        monkeypatch.setenv("SENDGRID_API_KEY", "SG.fake")
        report = _run(seeded_app["client"], seeded_app["admin_token"])
        check = _check(report, "email-delivery")
        assert check["status"] == "error"
        assert "sendgrid" in check["detail"]

    def test_default_sender_is_flagged(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.delenv("SMTP_FROM", raising=False)
        report = _run(seeded_app["client"], seeded_app["admin_token"])
        check = _check(report, "email-delivery")
        assert check["status"] == "warning"
        assert "noreply@example.com" in check["detail"]
        assert "SMTP_FROM" in check["detail"]

    def test_configured_transport_without_send_is_info(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("SMTP_FROM", "no-reply@example.org")
        report = _run(seeded_app["client"], seeded_app["admin_token"])
        check = _check(report, "email-delivery")
        assert check["status"] == "info"
        assert "email_to" in check["detail"]

    def test_real_send_success(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("SMTP_FROM", "no-reply@example.org")
        sent = {}

        def _fake_send(to_email, subject, body_text):
            sent["to"] = to_email
            return True

        with patch("app.auth.providers.password._send_mail", _fake_send):
            report = _run(seeded_app["client"], seeded_app["admin_token"], body={"email_to": "ops@example.org"})
        check = _check(report, "email-delivery")
        assert check["status"] == "ok"
        assert sent["to"] == "ops@example.org"
        # An accepted send is not a delivered send — the operator must confirm.
        assert "confirm" in check["detail"].lower()

    def test_real_send_failure(self, seeded_app, monkeypatch):
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("SMTP_FROM", "no-reply@example.org")
        with patch("app.auth.providers.password._send_mail", lambda *a: False):
            report = _run(seeded_app["client"], seeded_app["admin_token"], body={"email_to": "ops@example.org"})
        assert _check(report, "email-delivery")["status"] == "error"


class TestChatGrant:
    def test_chat_disabled_is_info(self, seeded_app):
        seeded_app["client"].app.state.chat_config = SimpleNamespace(enabled=False)
        report = _run(seeded_app["client"], seeded_app["admin_token"])
        check = _check(report, "chat-grant")
        assert check["status"] == "info"

    def test_chat_enabled_without_grant_is_error(self, seeded_app):
        seeded_app["client"].app.state.chat_config = SimpleNamespace(enabled=True)
        report = _run(seeded_app["client"], seeded_app["admin_token"])
        check = _check(report, "chat-grant")
        assert check["status"] == "error"
        assert "god-mode" in check["detail"] or "admins" in check["detail"]

    def test_chat_enabled_with_grant_is_ok(self, seeded_app):
        from src.db import SYSTEM_EVERYONE_GROUP
        from src.repositories import resource_grants_repo, user_groups_repo

        seeded_app["client"].app.state.chat_config = SimpleNamespace(enabled=True)
        everyone = user_groups_repo().get_by_name(SYSTEM_EVERYONE_GROUP)
        resource_grants_repo().create(group_id=everyone["id"], resource_type="chat", resource_id="chat")
        report = _run(seeded_app["client"], seeded_app["admin_token"])
        check = _check(report, "chat-grant")
        assert check["status"] == "ok"
        assert "Everyone" in check["detail"]


def _create_agent(agent_id, owner, tables_mode="selected", scope_tables=()):
    from src.repositories import agents_repo

    repo = agents_repo()
    repo.create(
        id=agent_id,
        owner_user_id=owner,
        name=f"Agent {agent_id}",
        slug=f"agent-{agent_id}",
        tables_mode=tables_mode,
    )
    if scope_tables:
        repo.set_scope(agent_id, [("table", t) for t in scope_tables])


class TestAgentScope:
    def test_no_table_scoped_agents_is_info(self, seeded_app):
        report = _run(seeded_app["client"], seeded_app["admin_token"])
        assert _check(report, "agent-scope")["status"] == "info"

    def test_passthrough_agent_is_not_flagged(self, seeded_app):
        _create_agent("a-pass", "analyst1", tables_mode="all")
        report = _run(seeded_app["client"], seeded_app["admin_token"])
        assert _check(report, "agent-scope")["status"] == "info"

    def test_empty_intersection_is_error(self, seeded_app):
        # Agent scoped to a table its owner holds NO per-table grant for —
        # the exact 403 "not in your stack" failure.
        _create_agent("a-broken", "analyst1", scope_tables=["orders"])
        report = _run(seeded_app["client"], seeded_app["admin_token"])
        check = _check(report, "agent-scope")
        assert check["status"] == "error"
        assert "agent-a-broken" in check["detail"]
        assert "table" in check["detail"]

    def test_granted_intersection_is_ok(self, seeded_app):
        from src.db import SYSTEM_EVERYONE_GROUP
        from src.repositories import resource_grants_repo, user_groups_repo

        _create_agent("a-good", "analyst1", scope_tables=["orders"])
        everyone = user_groups_repo().get_by_name(SYSTEM_EVERYONE_GROUP)
        resource_grants_repo().create(group_id=everyone["id"], resource_type="table", resource_id="orders")
        # analyst1 must actually be in the group for the intersection to see it.
        from src.repositories import user_group_members_repo

        user_group_members_repo().add_member(user_id="analyst1", group_id=everyone["id"], source="admin")
        report = _run(seeded_app["client"], seeded_app["admin_token"])
        check = _check(report, "agent-scope")
        assert check["status"] == "ok"


class TestBranding:
    def test_default_brand_is_info(self, seeded_app, monkeypatch):
        monkeypatch.delenv("AGNES_INSTANCE_BRAND", raising=False)
        report = _run(seeded_app["client"], seeded_app["admin_token"])
        assert _check(report, "branding")["status"] == "info"

    def test_brand_set_but_default_title_is_error(self, seeded_app, monkeypatch):
        import app.web.router as web_router

        monkeypatch.setenv("AGNES_INSTANCE_BRAND", "Acme IQ")
        monkeypatch.setattr(web_router, "get_instance_name", lambda: "AI Harness")
        report = _run(seeded_app["client"], seeded_app["admin_token"])
        check = _check(report, "branding")
        assert check["status"] == "error"
        assert "instance.name" in check["detail"]

    def test_brand_set_with_named_instance_is_ok(self, seeded_app, monkeypatch):
        import app.web.router as web_router

        monkeypatch.setenv("AGNES_INSTANCE_BRAND", "Acme IQ")
        monkeypatch.setattr(web_router, "get_instance_name", lambda: "Acme")
        report = _run(seeded_app["client"], seeded_app["admin_token"])
        check = _check(report, "branding")
        assert check["status"] == "ok"


class TestProbeProviders:
    """The provider probe helper this doctor rides on (app/auth/provider_registry)."""

    def test_password_always_available(self):
        from app.auth.provider_registry import probe_providers

        rows = {r["name"]: r for r in probe_providers()}
        assert rows["password"]["available"] is True
        assert set(rows) == {"google", "email", "password", "keboola", "microsoft", "sso"}

    def test_rows_carry_allowed_and_available(self):
        from app.auth.provider_registry import probe_providers

        for row in probe_providers():
            assert set(row) == {"name", "allowed", "available"}
            assert isinstance(row["allowed"], bool)
            assert isinstance(row["available"], bool)
