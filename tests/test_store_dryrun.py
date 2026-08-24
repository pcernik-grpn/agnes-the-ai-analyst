"""Tests for the pre-submit dry-run endpoint — POST /api/store/entities/dryrun.

The dry-run runs the full guardrail pipeline (inline checks + LLM review)
against a candidate bundle WITHOUT persisting any ``store_entities`` or
``store_submissions`` row, so submitters can iterate before the real upload.

Asserted invariants:
  * clean bundle  → 200, ``would_publish=true``, manifest status ``pass``
  * banned bundle → ``would_publish=false`` with the issue surfaced
  * NEITHER path writes a row (entities + submissions counts unchanged)

The Anthropic call is mocked — these tests never hit a live LLM.
"""

from __future__ import annotations

import io
import json
import zipfile
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def web_client(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    # Provider-ready so the dry-run exercises the LLM branch (mocked below).
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dryrun-key")
    (tmp_path / "state").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "extracts").mkdir()
    from src.db import close_system_db

    close_system_db()

    app = shared_app
    yield TestClient(app)
    close_system_db()


def _create_user(client, email, password="UserPass1!"):
    from argon2 import PasswordHasher
    from src.db import get_system_db
    from src.repositories.users import UserRepository

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


_OK_DESC = "Use when validating the store dry-run pipeline across every guardrail tier"
_OK_BODY = (
    "Body explaining when to invoke the component, what inputs it needs, "
    "and the behavior contract. Long enough to clear the 200-char body floor. "
    "Repeated content for length."
) * 2


def _make_skill_zip(skill_name: str = "code-review", desc: str = _OK_DESC, body: str = _OK_BODY) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            f"{skill_name}/SKILL.md",
            f"---\nname: {skill_name}\ndescription: {desc}\n---\n\n{body}\n",
        )
    return buf.getvalue()


def _make_banned_skill_zip(skill_name: str = "evil-skill") -> bytes:
    """A bundle whose script trips the static-security inline check (eval())."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            f"{skill_name}/SKILL.md",
            f"---\nname: {skill_name}\ndescription: {_OK_DESC}\n---\n\n{_OK_BODY}\n",
        )
        # Code file (.py is in-scope for the static scanner) with a banned
        # construct — eval() on attacker-controlled input.
        zf.writestr(f"{skill_name}/run.py", "import sys\neval(sys.argv[1])\n")
    return buf.getvalue()


def _make_malformed_and_banned_zip(skill_name: str = "probe-skill") -> bytes:
    """A bundle that fails the *validation* tier (content) AND trips
    static-security — the shape an attacker uses to probe the deny-list
    rule set behind an otherwise-malformed bundle. Manifest passes (SKILL.md
    present, valid name) so metadata extraction succeeds and we reach the
    inline checks; the short description + short body fail content_check."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            f"{skill_name}/SKILL.md",
            f"---\nname: {skill_name}\ndescription: short\n---\n\ntiny\n",
        )
        zf.writestr(f"{skill_name}/run.py", "import sys\neval(sys.argv[1])\n")
    return buf.getvalue()


# Verdict the mocked LLM review returns for the happy path — safe, no
# findings, content quality pass → is_safe() == True.
_SAFE_VERDICT = {
    "risk_level": "safe",
    "summary": "Looks clean.",
    "findings": [],
    "template_placeholders_found": 0,
    "content_quality": {"verdict": "pass", "issues": []},
    "reviewed_by_model": "claude-test",
    "error": None,
}

# A risky verdict — high-severity finding → is_safe() == False.
_RISKY_VERDICT = {
    "risk_level": "high",
    "summary": "Exfiltrates environment variables.",
    "findings": [
        {
            "severity": "critical",
            "category": "exfiltration",
            "file": "run.py",
            "explanation": "Reads $ANTHROPIC_API_KEY and POSTs it to a remote host.",
            "fix_hint": "Remove the network callout.",
        }
    ],
    "template_placeholders_found": 0,
    "content_quality": {"verdict": "pass", "issues": []},
    "reviewed_by_model": "claude-test",
    "error": None,
}


def _row_counts():
    """(store_entities, store_submissions) row counts from system.duckdb."""
    from src.db import get_system_db

    conn = get_system_db()
    try:
        ents = conn.execute("SELECT COUNT(*) FROM store_entities").fetchone()[0]
        subs = conn.execute("SELECT COUNT(*) FROM store_submissions").fetchone()[0]
        return ents, subs
    finally:
        conn.close()


def _enable_guardrails(monkeypatch):
    """Flip the guardrail pipeline ON for a single test.

    The autouse ``_flea_guardrails_disabled_by_default`` conftest fixture
    patches ``app.api.store.get_guardrails_enabled`` to return False; the
    LLM tier only runs when both intent and provider-readiness are True.
    """
    monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
    monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: True)


def _enable_guardrails_no_provider(monkeypatch):
    """Guardrails intent ON, but the LLM provider is NOT configured.

    Mirrors the real create path's fail-CLOSED third state: the entity is
    held at "pending" with no LLM review scheduled, so it never auto-publishes.
    """
    monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: True)
    monkeypatch.setattr("app.api.store.get_guardrails_llm_provider_ready", lambda: False)


class TestDryRunClean:
    def test_clean_bundle_would_publish_no_rows(self, web_client, monkeypatch):
        _enable_guardrails(monkeypatch)
        _create_user(web_client, "alice@x.com")
        before = _row_counts()

        with patch(
            "src.store_guardrails.llm_review.review_bundle",
            return_value=_SAFE_VERDICT,
        ) as mock_review:
            _, cookies = _create_user(web_client, "carol@x.com")
            r = web_client.post(
                "/api/store/entities/dryrun",
                files={"file": ("s.zip", _make_skill_zip(), "application/zip")},
                data={"type": "skill", "description": _OK_DESC},
                cookies=cookies,
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["would_publish"] is True, body
        assert body["inline_checks"]["manifest"]["status"] == "pass", body
        # LLM verdict surfaced to the submitter.
        assert body["llm_findings"]["risk_level"] == "safe"
        mock_review.assert_called_once()

        # ZERO DB writes.
        assert _row_counts() == before


class TestDryRunBanned:
    def test_banned_content_blocks_no_rows(self, web_client):
        _, cookies = _create_user(web_client, "dave@x.com")
        before = _row_counts()

        with patch(
            "src.store_guardrails.llm_review.review_bundle",
            return_value=_SAFE_VERDICT,
        ):
            r = web_client.post(
                "/api/store/entities/dryrun",
                files={"file": ("s.zip", _make_banned_skill_zip(), "application/zip")},
                data={"type": "skill", "description": _OK_DESC},
                cookies=cookies,
            )

        assert r.status_code == 200, r.text
        body = r.json()
        # Inline static-security tier trips on eval() → cannot publish.
        assert body["would_publish"] is False, body
        assert body["inline_checks"]["static_security"]["status"] == "fail", body
        findings = body["inline_checks"]["static_security"]["findings"]
        assert any("eval" in json.dumps(f).lower() for f in findings), findings

        # ZERO DB writes even on a blocked dry-run.
        assert _row_counts() == before

    def test_llm_risky_verdict_blocks_no_rows(self, web_client, monkeypatch):
        """Inline passes but the LLM flags a high-severity finding."""
        _enable_guardrails(monkeypatch)
        _, cookies = _create_user(web_client, "erin@x.com")
        before = _row_counts()

        with patch(
            "src.store_guardrails.llm_review.review_bundle",
            return_value=_RISKY_VERDICT,
        ):
            r = web_client.post(
                "/api/store/entities/dryrun",
                files={"file": ("s.zip", _make_skill_zip("clean-skill"), "application/zip")},
                data={"type": "skill", "description": _OK_DESC},
                cookies=cookies,
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["inline_checks"]["manifest"]["status"] == "pass", body
        assert body["would_publish"] is False, body
        assert body["llm_findings"]["risk_level"] == "high"

        assert _row_counts() == before


class TestDryRunFailClosed:
    """The three-state guardrail matrix must match the real create path.

    enabled=False           → publication hinges on inline alone (safe).
    enabled=True, ready=True → LLM verdict decides (covered above).
    enabled=True, ready=False→ fail-CLOSED: real path holds at "pending"
                               with no LLM review, so a dry-run must report
                               would_publish=False even for a clean bundle.
    """

    def test_guardrails_on_provider_not_ready_fails_closed(self, web_client, monkeypatch):
        # Regression for #652 review: the else branch used to set safe=True
        # unconditionally, giving a false positive (would_publish=True) for a
        # clean bundle that the real create path would strand at "pending".
        _enable_guardrails_no_provider(monkeypatch)
        _, cookies = _create_user(web_client, "frank@x.com")
        before = _row_counts()

        with patch("src.store_guardrails.llm_review.review_bundle") as mock_review:
            r = web_client.post(
                "/api/store/entities/dryrun",
                files={"file": ("s.zip", _make_skill_zip("clean-skill"), "application/zip")},
                data={"type": "skill", "description": _OK_DESC},
                cookies=cookies,
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["inline_checks"]["manifest"]["status"] == "pass", body
        # Clean bundle, but provider not configured → would NOT auto-publish.
        assert body["would_publish"] is False, body
        # The LLM tier never runs when the provider is unconfigured.
        assert body["llm_findings"] is None, body
        mock_review.assert_not_called()

        assert _row_counts() == before

    def test_guardrails_off_clean_bundle_would_publish(self, web_client, monkeypatch):
        """enabled=False: the LLM tier is a no-op, so a clean bundle publishes."""
        monkeypatch.setattr("app.api.store.get_guardrails_enabled", lambda: False)
        _, cookies = _create_user(web_client, "grace@x.com")

        with patch("src.store_guardrails.llm_review.review_bundle") as mock_review:
            r = web_client.post(
                "/api/store/entities/dryrun",
                files={"file": ("s.zip", _make_skill_zip("clean-skill"), "application/zip")},
                data={"type": "skill", "description": _OK_DESC},
                cookies=cookies,
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["would_publish"] is True, body
        assert body["llm_findings"] is None, body
        mock_review.assert_not_called()


class TestDryRunAntiEnumeration:
    def test_validation_fail_redacts_static_findings(self, web_client):
        """A bundle that fails validation (content) must NOT leak static_scan
        findings — mirrors the shadowing defense in _reject_inline_or_continue
        so a malformed bundle can't be used to enumerate the deny-list."""
        _, cookies = _create_user(web_client, "heidi@x.com")
        before = _row_counts()

        r = web_client.post(
            "/api/store/entities/dryrun",
            files={"file": ("s.zip", _make_malformed_and_banned_zip(), "application/zip")},
            data={"type": "skill", "description": "short"},
            cookies=cookies,
        )

        assert r.status_code == 200, r.text
        body = r.json()
        # Validation (content) failed → cannot publish.
        assert body["would_publish"] is False, body
        assert body["inline_checks"]["content"]["status"] == "fail", body
        # static_security is shadowed — neither status "fail" nor any findings
        # are exposed, so the deny-list rule set can't be probed.
        static = body["inline_checks"]["static_security"]
        assert static == {"status": "skipped"}, static
        assert "findings" not in static, static

        assert _row_counts() == before


class TestDryRunAuth:
    def test_requires_auth(self, web_client):
        r = web_client.post(
            "/api/store/entities/dryrun",
            files={"file": ("s.zip", _make_skill_zip(), "application/zip")},
            data={"type": "skill", "description": _OK_DESC},
        )
        assert r.status_code in (401, 403), r.text


class TestDryRunLintOnly:
    def test_lint_only_skips_the_paid_llm_review(self, web_client, monkeypatch):
        """`lint_only=true` must NOT bill an LLM security review.

        The upload wizard's advisory panel renders only the `lint` block, so
        running the blocking Anthropic round-trip here would pay for a verdict
        the caller discards — and the real publish schedules the review again.
        Guardrails + provider are both ON, so the review WOULD run without the
        flag (see TestDryRunClean); this pins that the flag is what stops it.
        """
        _enable_guardrails(monkeypatch)
        _, cookies = _create_user(web_client, "lintonly@x.com")
        before = _row_counts()

        with patch("src.store_guardrails.llm_review.review_bundle") as mock_review:
            r = web_client.post(
                "/api/store/entities/dryrun",
                files={"file": ("s.zip", _make_skill_zip("lint-only-skill"), "application/zip")},
                data={"type": "skill", "description": _OK_DESC, "lint_only": "true"},
                cookies=cookies,
            )

        assert r.status_code == 200, r.text
        mock_review.assert_not_called()

        body = r.json()
        # Still does the cheap work: inline checks + the advisory lint block.
        assert body["inline_checks"]["manifest"]["status"] == "pass", body
        assert body["lint"] is not None, body
        assert "findings" in body["lint"], body
        # No verdict was computed, so the LLM tier reports nothing.
        assert body["llm_findings"] is None, body
        # Dry-run contract holds: still no DB writes.
        assert _row_counts() == before

    def test_without_lint_only_the_review_still_runs(self, web_client, monkeypatch):
        """Guard against the flag silently defaulting on and killing the
        endpoint's actual purpose (the #317 pre-submit guardrail verdict)."""
        _enable_guardrails(monkeypatch)
        _, cookies = _create_user(web_client, "withreview@x.com")

        with patch("src.store_guardrails.llm_review.review_bundle") as mock_review:
            mock_review.return_value = {"verdict": "safe", "findings": []}
            r = web_client.post(
                "/api/store/entities/dryrun",
                files={"file": ("s.zip", _make_skill_zip("reviewed-skill"), "application/zip")},
                data={"type": "skill", "description": _OK_DESC},
                cookies=cookies,
            )

        assert r.status_code == 200, r.text
        mock_review.assert_called_once()
