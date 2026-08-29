"""LLM-review wiring tests — Anthropic call mocked.

These tests cover the runner's persistence behavior on each verdict
shape (safe / risky / error). The actual prompt engineering lives in
``src/store_guardrails/prompts.py`` and is exercised at integration
time, not here.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from connectors.llm.exceptions import LLMTimeoutError
from src import db as src_db
from src.repositories.store_entities import StoreEntitiesRepository
from src.repositories.store_submissions import StoreSubmissionsRepository
from src.store_guardrails.runner import LlmResult, run_llm_review


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    src_db._system_db_conn = None
    src_db._system_db_path = None
    c = src_db.get_system_db()
    yield c
    c.close()


@pytest.fixture
def plugin_dir():
    d = Path(tempfile.mkdtemp(prefix="agnes_llm_test_"))
    (d / "SKILL.md").write_text("# Test\nbody " * 30)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _seed_pending_submission(conn, plugin_dir: Path) -> tuple[str, str]:
    """Stage a store_entities row + a pending_llm submission.

    Returns ``(entity_id, submission_id)`` so the test can assert against
    final state.
    """
    from src.repositories.users import UserRepository

    UserRepository(conn).create(id="u1", email="alice@x.com", name="alice")
    ents = StoreEntitiesRepository(conn)
    ents.create(
        id="e1",
        owner_user_id="u1",
        owner_username="alice",
        type="skill",
        name="probe",
        description="probe skill",
        category=None,
        version="1.0.0",
        file_size=10,
        visibility_status="pending",
    )
    subs = StoreSubmissionsRepository(conn)
    sub_id = subs.create(
        submitter_id="u1",
        submitter_email="alice@x.com",
        type="skill",
        name="probe",
        version="1.0.0",
        status="pending_llm",
        entity_id="e1",
        inline_checks={"manifest": {"status": "pass"}},
    )
    return "e1", sub_id


# The runner closes its own cursor in `finally`. Hand it a *fresh* cursor
# each call (mirrors the production `get_system_db` behavior) so closing
# it doesn't poison the test's primary cursor used for assertions.
def _conn_factory(_unused):
    def _f():
        return src_db.get_system_db()

    return _f


# ---------------------------------------------------------------------------
# run_llm_review outcomes
# ---------------------------------------------------------------------------


class TestLlmReviewRunner:
    def test_safe_verdict_approves_entity(self, conn, plugin_dir):
        eid, sub_id = _seed_pending_submission(conn, plugin_dir)

        verdict = {
            "risk_level": "safe",
            "summary": "OK",
            "findings": [],
            "template_placeholders_found": 0,
            "reviewed_by_model": "claude-haiku-4-5-20251001",
            "error": None,
        }
        with patch(
            "src.store_guardrails.runner.llm_review.review_bundle",
            return_value=verdict,
        ):
            result = run_llm_review(
                sub_id,
                plugin_dir=plugin_dir,
                conn_factory=_conn_factory(conn),
                api_key_loader=lambda: "sk-test",
                model_loader=lambda: "claude-haiku-4-5-20251001",
            )

        assert isinstance(result, LlmResult)
        assert result.passed
        sub = StoreSubmissionsRepository(conn).get(sub_id)
        assert sub["status"] == "approved"
        ent = StoreEntitiesRepository(conn).get(eid)
        assert ent["visibility_status"] == "approved"

    def test_high_risk_verdict_blocks(self, conn, plugin_dir):
        eid, sub_id = _seed_pending_submission(conn, plugin_dir)

        verdict = {
            "risk_level": "high",
            "summary": "exfil",
            "findings": [
                {
                    "severity": "high",
                    "category": "exfiltration",
                    "file": "run.py",
                    "explanation": "ships token to remote",
                    "fix_hint": "remove the POST",
                }
            ],
            "template_placeholders_found": 0,
            "reviewed_by_model": "claude-haiku-4-5-20251001",
            "error": None,
        }
        with patch(
            "src.store_guardrails.runner.llm_review.review_bundle",
            return_value=verdict,
        ):
            run_llm_review(
                sub_id,
                plugin_dir=plugin_dir,
                conn_factory=_conn_factory(conn),
                api_key_loader=lambda: "sk-test",
                model_loader=lambda: "claude-haiku-4-5-20251001",
            )

        sub = StoreSubmissionsRepository(conn).get(sub_id)
        assert sub["status"] == "blocked_llm"
        ent = StoreEntitiesRepository(conn).get(eid)
        # Entity stays pending — not visible until override.
        assert ent["visibility_status"] == "pending"

    def test_low_risk_with_high_finding_blocks(self, conn, plugin_dir):
        """Pass condition requires BOTH risk_level<=low AND no high findings.
        A 'low' verdict with a high finding still blocks."""
        eid, sub_id = _seed_pending_submission(conn, plugin_dir)

        verdict = {
            "risk_level": "low",
            "summary": "mostly ok",
            "findings": [
                {
                    "severity": "high",
                    "category": "credentials",
                    "file": "creds.py",
                    "explanation": "key in source",
                }
            ],
            "template_placeholders_found": 0,
            "reviewed_by_model": "claude-haiku-4-5-20251001",
            "error": None,
        }
        with patch(
            "src.store_guardrails.runner.llm_review.review_bundle",
            return_value=verdict,
        ):
            run_llm_review(
                sub_id,
                plugin_dir=plugin_dir,
                conn_factory=_conn_factory(conn),
                api_key_loader=lambda: "sk-test",
                model_loader=lambda: "claude-haiku-4-5-20251001",
            )

        sub = StoreSubmissionsRepository(conn).get(sub_id)
        assert sub["status"] == "blocked_llm"

    def test_content_quality_fail_blocks(self, conn, plugin_dir):
        """Safe security verdict but content_quality.verdict=fail must still
        block. The LLM substantive layer is a hard gate — descriptions that
        clear the mechanical floor can still get flagged for vagueness."""
        eid, sub_id = _seed_pending_submission(conn, plugin_dir)

        verdict = {
            "risk_level": "safe",
            "summary": "OK",
            "findings": [],
            "template_placeholders_found": 0,
            "content_quality": {
                "verdict": "fail",
                "issues": [
                    {
                        "file": "skills/probe/SKILL.md",
                        "field": "frontmatter.description",
                        "issue": "describes WHAT the skill is, not WHEN to invoke it",
                        "hint": "Rewrite as 'Use when reviewing PRs to flag missing tests.'",
                    }
                ],
            },
            "reviewed_by_model": "claude-haiku-4-5-20251001",
            "error": None,
        }
        with patch(
            "src.store_guardrails.runner.llm_review.review_bundle",
            return_value=verdict,
        ):
            run_llm_review(
                sub_id,
                plugin_dir=plugin_dir,
                conn_factory=_conn_factory(conn),
                api_key_loader=lambda: "sk-test",
                model_loader=lambda: "claude-haiku-4-5-20251001",
            )

        sub = StoreSubmissionsRepository(conn).get(sub_id)
        assert sub["status"] == "blocked_llm"
        # The content_quality verdict + issues persisted on the submission
        # so the quarantine banner can render the rewrite hints.
        assert sub["llm_findings"]["content_quality"]["verdict"] == "fail"
        assert len(sub["llm_findings"]["content_quality"]["issues"]) == 1

    def test_content_quality_missing_treated_as_pass(self, conn, plugin_dir):
        """Backward compat — older recorded verdicts without content_quality
        must not retroactively block. The wire format adds the field; absent
        means pass."""
        eid, sub_id = _seed_pending_submission(conn, plugin_dir)

        verdict = {
            "risk_level": "safe",
            "summary": "OK",
            "findings": [],
            "template_placeholders_found": 0,
            # content_quality intentionally absent
            "reviewed_by_model": "claude-haiku-4-5-20251001",
            "error": None,
        }
        with patch(
            "src.store_guardrails.runner.llm_review.review_bundle",
            return_value=verdict,
        ):
            run_llm_review(
                sub_id,
                plugin_dir=plugin_dir,
                conn_factory=_conn_factory(conn),
                api_key_loader=lambda: "sk-test",
                model_loader=lambda: "claude-haiku-4-5-20251001",
            )

        sub = StoreSubmissionsRepository(conn).get(sub_id)
        assert sub["status"] == "approved"

    def test_medium_finding_with_safe_risk_passes(self, conn, plugin_dir):
        """Medium findings shouldn't block when overall risk is safe — that's
        the 'noise but no exploit' band the operator opted into when picking
        Haiku as the tier. Operators who want stricter pin Sonnet/Opus."""
        eid, sub_id = _seed_pending_submission(conn, plugin_dir)

        verdict = {
            "risk_level": "safe",
            "summary": "noise",
            "findings": [
                {
                    "severity": "medium",
                    "category": "code_quality",
                    "file": "x.py",
                    "explanation": "could be cleaner",
                }
            ],
            "template_placeholders_found": 1,
            "reviewed_by_model": "claude-haiku-4-5-20251001",
            "error": None,
        }
        with patch(
            "src.store_guardrails.runner.llm_review.review_bundle",
            return_value=verdict,
        ):
            run_llm_review(
                sub_id,
                plugin_dir=plugin_dir,
                conn_factory=_conn_factory(conn),
                api_key_loader=lambda: "sk-test",
                model_loader=lambda: "claude-haiku-4-5-20251001",
            )

        sub = StoreSubmissionsRepository(conn).get(sub_id)
        assert sub["status"] == "approved"

    def test_review_error_keeps_pending(self, conn, plugin_dir):
        eid, sub_id = _seed_pending_submission(conn, plugin_dir)

        # llm_review.review_bundle catches LLMError and returns a dict with
        # error set; the runner records review_error.
        verdict = {
            "risk_level": None,
            "summary": None,
            "findings": [],
            "template_placeholders_found": 0,
            "reviewed_by_model": "claude-haiku-4-5-20251001",
            "error": "LLMTimeoutError: Anthropic connection error",
        }
        with patch(
            "src.store_guardrails.runner.llm_review.review_bundle",
            return_value=verdict,
        ):
            run_llm_review(
                sub_id,
                plugin_dir=plugin_dir,
                conn_factory=_conn_factory(conn),
                api_key_loader=lambda: "sk-test",
                model_loader=lambda: "claude-haiku-4-5-20251001",
            )

        sub = StoreSubmissionsRepository(conn).get(sub_id)
        assert sub["status"] == "review_error"
        ent = StoreEntitiesRepository(conn).get(eid)
        assert ent["visibility_status"] == "pending"

    def test_missing_plugin_dir_records_review_error(self, conn, tmp_path):
        eid, sub_id = _seed_pending_submission(conn, tmp_path / "exists")
        # Point at a path that doesn't exist.
        ghost = tmp_path / "ghost-plugin-dir"
        with patch("src.store_guardrails.runner.llm_review.review_bundle") as m:
            run_llm_review(
                sub_id,
                plugin_dir=ghost,
                conn_factory=_conn_factory(conn),
                api_key_loader=lambda: "sk-test",
                model_loader=lambda: "claude-haiku-4-5-20251001",
            )
            m.assert_not_called()

        sub = StoreSubmissionsRepository(conn).get(sub_id)
        assert sub["status"] == "review_error"

    def test_safe_verdict_skipped_when_admin_archived_during_review(self, conn, plugin_dir):
        """#3 — BG-task race: admin archives entity while LLM review is
        in flight. Pre-fix the verdict's set_visibility('approved')
        clobbered the archive. Post-fix the guarded variant refuses
        and audit-logs the skip."""
        eid, sub_id = _seed_pending_submission(conn, plugin_dir)
        # Admin archives BEFORE the LLM verdict lands.
        StoreEntitiesRepository(conn).archive(eid, by_user_id="admin")

        verdict = {
            "risk_level": "safe",
            "summary": "OK",
            "findings": [],
            "template_placeholders_found": 0,
            "reviewed_by_model": "claude-haiku-4-5-20251001",
            "error": None,
        }
        with patch(
            "src.store_guardrails.runner.llm_review.review_bundle",
            return_value=verdict,
        ):
            run_llm_review(
                sub_id,
                plugin_dir=plugin_dir,
                conn_factory=_conn_factory(conn),
                api_key_loader=lambda: "sk-test",
                model_loader=lambda: "claude-haiku-4-5-20251001",
            )

        ent = StoreEntitiesRepository(conn).get(eid)
        assert ent["visibility_status"] == "archived", "BG verdict must NOT clobber an admin archive"
        # Submission still flips to 'approved' (verdict is forensic
        # record); the lifecycle stays archived because the admin won.
        sub = StoreSubmissionsRepository(conn).get(sub_id)
        assert sub["status"] == "approved"

        # And we wrote an audit row explaining the skip.
        audits = conn.execute(
            "SELECT action, params FROM audit_log WHERE resource = ? AND action = ?",
            [f"store_submission:{sub_id}", "store.submission.bg_verdict_skipped"],
        ).fetchall()
        assert audits, "missing skip audit row"

    def test_config_loader_failure_records_review_error(self, conn, plugin_dir):
        eid, sub_id = _seed_pending_submission(conn, plugin_dir)

        def boom():
            raise RuntimeError("no api key")

        run_llm_review(
            sub_id,
            plugin_dir=plugin_dir,
            conn_factory=_conn_factory(conn),
            api_key_loader=boom,
            model_loader=lambda: "claude-haiku-4-5-20251001",
        )

        sub = StoreSubmissionsRepository(conn).get(sub_id)
        assert sub["status"] == "review_error"


# ---------------------------------------------------------------------------
# TCRD-235 — submitter notified when the async LLM review lands a
# terminal verdict (approved / blocked_llm). No signal on intermediate
# states (review_error), and a broken notification channel must never
# break the review itself.
# ---------------------------------------------------------------------------


class TestLlmReviewRunnerNotifiesSubmitter:
    def test_safe_verdict_notifies_submitter_with_approved_decision(self, conn, plugin_dir, monkeypatch):
        eid, sub_id = _seed_pending_submission(conn, plugin_dir)
        calls = []
        monkeypatch.setattr(
            "app.notifications.publish_notification",
            lambda user, payload: calls.append((user, payload)),
        )

        verdict = {
            "risk_level": "safe",
            "summary": "OK",
            "findings": [],
            "template_placeholders_found": 0,
            "reviewed_by_model": "claude-haiku-4-5-20251001",
            "error": None,
        }
        with patch(
            "src.store_guardrails.runner.llm_review.review_bundle",
            return_value=verdict,
        ):
            run_llm_review(
                sub_id,
                plugin_dir=plugin_dir,
                conn_factory=_conn_factory(conn),
                api_key_loader=lambda: "sk-test",
                model_loader=lambda: "claude-haiku-4-5-20251001",
            )

        assert len(calls) == 1
        user, payload = calls[0]
        assert user == "u1"  # submitter_id from _seed_pending_submission
        assert payload["kind"] == "store_submission"
        assert payload["decision"] == "approved"
        assert payload["submission_id"] == sub_id
        assert payload["entity_id"] == eid
        assert payload["name"] == "probe"

    def test_blocked_verdict_notifies_submitter_with_rejected_decision(self, conn, plugin_dir, monkeypatch):
        eid, sub_id = _seed_pending_submission(conn, plugin_dir)
        calls = []
        monkeypatch.setattr(
            "app.notifications.publish_notification",
            lambda user, payload: calls.append((user, payload)),
        )

        verdict = {
            "risk_level": "high",
            "summary": "exfil",
            "findings": [
                {
                    "severity": "high",
                    "category": "exfiltration",
                    "file": "run.py",
                    "explanation": "ships token to remote",
                }
            ],
            "template_placeholders_found": 0,
            "reviewed_by_model": "claude-haiku-4-5-20251001",
            "error": None,
        }
        with patch(
            "src.store_guardrails.runner.llm_review.review_bundle",
            return_value=verdict,
        ):
            run_llm_review(
                sub_id,
                plugin_dir=plugin_dir,
                conn_factory=_conn_factory(conn),
                api_key_loader=lambda: "sk-test",
                model_loader=lambda: "claude-haiku-4-5-20251001",
            )

        assert len(calls) == 1
        user, payload = calls[0]
        assert user == "u1"
        assert payload["decision"] == "blocked_llm"
        assert payload["submission_id"] == sub_id
        assert payload["entity_id"] == eid

    def test_review_error_does_not_notify(self, conn, plugin_dir, monkeypatch):
        """review_error is inconclusive, not a decision — no notification."""
        eid, sub_id = _seed_pending_submission(conn, plugin_dir)
        calls = []
        monkeypatch.setattr(
            "app.notifications.publish_notification",
            lambda user, payload: calls.append((user, payload)),
        )

        verdict = {
            "risk_level": None,
            "summary": None,
            "findings": [],
            "template_placeholders_found": 0,
            "reviewed_by_model": "claude-haiku-4-5-20251001",
            "error": "LLMTimeoutError: Anthropic connection error",
        }
        with patch(
            "src.store_guardrails.runner.llm_review.review_bundle",
            return_value=verdict,
        ):
            run_llm_review(
                sub_id,
                plugin_dir=plugin_dir,
                conn_factory=_conn_factory(conn),
                api_key_loader=lambda: "sk-test",
                model_loader=lambda: "claude-haiku-4-5-20251001",
            )

        assert calls == []

    def test_notification_channel_failure_does_not_break_review(self, conn, plugin_dir, monkeypatch):
        """A broken notification channel must never fail the review itself —
        the submission still lands at 'approved' even though the notify
        call raised."""

        def boom(user, payload):
            raise RuntimeError("coordination backend exploded")

        monkeypatch.setattr("app.notifications.publish_notification", boom)

        eid, sub_id = _seed_pending_submission(conn, plugin_dir)
        verdict = {
            "risk_level": "safe",
            "summary": "OK",
            "findings": [],
            "template_placeholders_found": 0,
            "reviewed_by_model": "claude-haiku-4-5-20251001",
            "error": None,
        }
        with patch(
            "src.store_guardrails.runner.llm_review.review_bundle",
            return_value=verdict,
        ):
            result = run_llm_review(
                sub_id,
                plugin_dir=plugin_dir,
                conn_factory=_conn_factory(conn),
                api_key_loader=lambda: "sk-test",
                model_loader=lambda: "claude-haiku-4-5-20251001",
            )

        assert isinstance(result, LlmResult)
        assert result.passed
        sub = StoreSubmissionsRepository(conn).get(sub_id)
        assert sub["status"] == "approved"
        ent = StoreEntitiesRepository(conn).get(eid)
        assert ent["visibility_status"] == "approved"


# ---------------------------------------------------------------------------
# llm_review.review_bundle — single-shot transport-error path
# ---------------------------------------------------------------------------


class TestReviewBundleErrorTransport:
    def test_anthropic_timeout_returns_error_dict(self, plugin_dir):
        from src.store_guardrails import llm_review

        with patch("src.store_guardrails.llm_review.AnthropicExtractor") as MockEx:
            inst = MockEx.return_value
            inst.extract_json.side_effect = LLMTimeoutError("connection error")
            result = llm_review.review_bundle(
                plugin_dir,
                type_="skill",
                name="x",
                version="1.0.0",
                description="x" * 30,
                api_key="sk-test",
                model="claude-haiku-4-5-20251001",
            )
            assert result["error"]
            assert result["risk_level"] is None
            assert result["reviewed_by_model"] == "claude-haiku-4-5-20251001"

    def test_missing_risk_level_surfaces_as_review_error(self, plugin_dir):
        """#10 — model returns a structured response with no/empty
        risk_level. Pre-fix this defaulted to 'medium' and looked like a
        model-decided block. Post-fix it surfaces as
        ``error='missing_risk_level'`` so the runner persists
        ``status='review_error'`` (admin gets the retry button)."""
        from src.store_guardrails import llm_review

        with patch("src.store_guardrails.llm_review.AnthropicExtractor") as MockEx:
            inst = MockEx.return_value
            inst.extract_json.return_value = {
                "summary": "model didn't fill risk_level",
                "findings": [],
                "template_placeholders_found": 0,
            }
            result = llm_review.review_bundle(
                plugin_dir,
                type_="skill",
                name="x",
                version="1.0.0",
                description="x" * 30,
                api_key="sk-test",
                model="claude-haiku-4-5-20251001",
            )
            assert result["risk_level"] is None
            assert result["error"] == "missing_risk_level"

    def test_system_prompt_passed_via_dedicated_parameter(self, plugin_dir):
        """#1 — the system prompt MUST be passed via the SDK's separate
        ``system=`` parameter, not concatenated into user content. This
        is the trust boundary that keeps a crafted README inside the
        bundle from overriding the reviewer rules."""
        from src.store_guardrails import llm_review
        from src.store_guardrails.prompts import SYSTEM_PROMPT

        with patch("src.store_guardrails.llm_review.AnthropicExtractor") as MockEx:
            inst = MockEx.return_value
            inst.extract_json.return_value = {
                "risk_level": "safe",
                "summary": "ok",
                "findings": [],
                "template_placeholders_found": 0,
            }
            llm_review.review_bundle(
                plugin_dir,
                type_="skill",
                name="x",
                version="1.0.0",
                description="x" * 30,
                api_key="sk-test",
                model="claude-haiku-4-5-20251001",
            )
            call = inst.extract_json.call_args
            assert call.kwargs.get("system") == SYSTEM_PROMPT, (
                "SYSTEM_PROMPT must be passed via system= so it lands in the SDK's system role — not in user content"
            )
            user_payload = call.kwargs.get("prompt") or ""
            assert "<bundle>" in user_payload and "</bundle>" in user_payload, (
                "user payload must wrap the bundle files in trust-boundary sentinel tags"
            )
            assert SYSTEM_PROMPT not in user_payload, "SYSTEM_PROMPT must NOT be inlined into user content"


class TestNormalizeContentQualityVerdict:
    """The verdict is an aggregate of the evidence — both directions
    of the asymmetry collapsed in #277 LOW #2."""

    def test_fail_with_no_issues_downgrades_to_pass(self):
        from src.store_guardrails.llm_review import _normalize_content_quality

        result = _normalize_content_quality({"verdict": "fail", "issues": []})
        assert result["verdict"] == "pass"
        assert result["issues"] == []

    def test_pass_with_issues_promoted_to_fail(self):
        # The #277 LOW #2 fix.
        from src.store_guardrails.llm_review import _normalize_content_quality

        result = _normalize_content_quality(
            {
                "verdict": "pass",
                "issues": [
                    {
                        "file": "skills/foo/SKILL.md",
                        "field": "frontmatter.description",
                        "issue": "vague",
                        "hint": "be specific",
                    }
                ],
            }
        )
        assert result["verdict"] == "fail"
        assert len(result["issues"]) == 1
        assert result["issues"][0]["issue"] == "vague"

    def test_aligned_pass_with_no_issues_stays_pass(self):
        from src.store_guardrails.llm_review import _normalize_content_quality

        assert _normalize_content_quality({"verdict": "pass", "issues": []})["verdict"] == "pass"

    def test_aligned_fail_with_issues_stays_fail(self):
        from src.store_guardrails.llm_review import _normalize_content_quality

        assert (
            _normalize_content_quality(
                {
                    "verdict": "fail",
                    "issues": [{"file": "x.md", "field": "f", "issue": "i", "hint": "h"}],
                }
            )["verdict"]
            == "fail"
        )

    def test_malformed_value_returns_safe_pass(self):
        from src.store_guardrails.llm_review import _normalize_content_quality

        assert _normalize_content_quality(None) == {"verdict": "pass", "issues": []}
        assert _normalize_content_quality("garbage") == {"verdict": "pass", "issues": []}
        assert _normalize_content_quality(42) == {"verdict": "pass", "issues": []}


class TestReviewBundleConstructorFailure:
    def test_constructor_failure_returns_error_verdict(self, tmp_path):
        """AnthropicExtractor construction (which lazily imports the SDK)
        failing must yield a structured review_error verdict, not escape
        review_bundle — an escaped exception bubbles through BackgroundTasks
        and pins the submission at pending_llm."""
        from src.store_guardrails import llm_review

        (tmp_path / "SKILL.md").write_text("---\nname: x\ndescription: d\n---\nbody\n")
        with patch(
            "src.store_guardrails.llm_review.AnthropicExtractor",
            side_effect=RuntimeError("client construction failed"),
        ):
            verdict = llm_review.review_bundle(
                tmp_path,
                type_="skill",
                name="x",
                version="1",
                description=None,
                api_key="sk-test",
                model="claude-haiku-4-5-20251001",
            )
        assert verdict["error"] == "RuntimeError: client construction failed"
        assert verdict["risk_level"] is None
        assert verdict["findings"] == []


class TestVertexMode:
    """ai.provider: vertex — keyless guardrails (Google ADC signs the calls)."""

    def test_default_api_key_loader_returns_sentinel_in_vertex_mode(self, monkeypatch):
        from src.store_guardrails.runner import default_api_key_loader

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        with patch("connectors.llm.factory.vertex_config_or_none", return_value=("p", "global")):
            assert default_api_key_loader() == ""

    def test_default_api_key_loader_error_mentions_vertex(self, monkeypatch):
        from src.store_guardrails.runner import default_api_key_loader

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        with (
            patch("connectors.llm.factory.vertex_config_or_none", return_value=None),
            pytest.raises(RuntimeError, match="ai.provider: vertex"),
        ):
            default_api_key_loader()

    def test_static_key_still_wins(self, monkeypatch):
        from src.store_guardrails.runner import default_api_key_loader

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-static")
        assert default_api_key_loader() == "sk-static"

    def test_llm_review_builds_vertex_extractor_on_empty_key(self, plugin_dir):
        from src.store_guardrails.llm_review import review_bundle

        with (
            patch("src.store_guardrails.llm_review.vertex_config_or_none", return_value=("p", "global")),
            patch("src.store_guardrails.llm_review.create_vertex_extractor") as mock_factory,
        ):
            mock_factory.return_value.extract_json.return_value = {
                "risk_level": "safe",
                "summary": "fine",
                "findings": [],
                "template_placeholders_found": False,
            }
            verdict = review_bundle(
                plugin_dir,
                type_="skill",
                name="probe",
                version="1.0.0",
                description="d",
                api_key="",
                model="claude-haiku-4-5-20251001",
            )
        assert verdict["risk_level"] == "safe"
        assert verdict["error"] is None
        mock_factory.assert_called_once_with("claude-haiku-4-5-20251001")

    def test_guardrails_provider_ready_true_in_vertex_mode(self, monkeypatch):
        from app.instance_config import get_guardrails_llm_provider_ready

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        with patch("connectors.llm.factory.vertex_config_or_none", return_value=("p", "global")):
            assert get_guardrails_llm_provider_ready() is True
        with patch("connectors.llm.factory.vertex_config_or_none", return_value=None):
            assert get_guardrails_llm_provider_ready() is False
