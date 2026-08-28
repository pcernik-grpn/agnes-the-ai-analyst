"""Tests for services.corporate_memory.governance.resolve_initial_status (#1573).

Behavioral coverage of the corporate_memory.approval_mode knob — every
assertion here checks that a DIFFERENT config value produces a DIFFERENT
outcome, not merely that the schema's default value round-trips (#1573's
own root cause: a test that only asserted the default value hid three
inert knobs for months).
"""

import logging

from services.corporate_memory.governance import (
    DEFAULT_AUTO_PUBLISH_MIN_CONFIDENCE,
    resolve_initial_status,
)


class TestNoGovernanceConfig:
    def test_missing_config_auto_approves(self):
        """Legacy mode (no corporate_memory: block) — no review, matches the
        documented 'system runs in legacy democratic-wiki mode' default."""
        assert resolve_initial_status(None) == "approved"
        assert resolve_initial_status({}) == "approved"


class TestReviewQueue:
    def test_review_queue_is_pending(self):
        assert resolve_initial_status({"approval_mode": "review_queue"}) == "pending"

    def test_default_approval_mode_is_review_queue(self):
        """A governance config with no approval_mode key at all defaults to
        review_queue per the documented default."""
        assert resolve_initial_status({"distribution_mode": "hybrid"}) == "pending"


class TestAutoPublish:
    def test_auto_publish_is_approved_regardless_of_confidence(self):
        assert resolve_initial_status({"approval_mode": "auto_publish"}) == "approved"
        assert resolve_initial_status({"approval_mode": "auto_publish"}, confidence=0.0) == "approved"


class TestThreshold:
    def test_above_cutoff_auto_publishes(self):
        cfg = {"approval_mode": "threshold", "auto_publish_min_confidence": 0.7}
        assert resolve_initial_status(cfg, confidence=0.9) == "approved"

    def test_below_cutoff_queues(self):
        cfg = {"approval_mode": "threshold", "auto_publish_min_confidence": 0.7}
        assert resolve_initial_status(cfg, confidence=0.5) == "pending"

    def test_exactly_at_cutoff_auto_publishes(self):
        cfg = {"approval_mode": "threshold", "auto_publish_min_confidence": 0.7}
        assert resolve_initial_status(cfg, confidence=0.7) == "approved"

    def test_unknown_confidence_never_guesses_queues(self):
        """No confidence score available (caller couldn't compute one) —
        never auto-publish on an unknown score."""
        cfg = {"approval_mode": "threshold", "auto_publish_min_confidence": 0.1}
        assert resolve_initial_status(cfg, confidence=None) == "pending"

    def test_default_cutoff_used_when_unset(self):
        cfg = {"approval_mode": "threshold"}
        below = DEFAULT_AUTO_PUBLISH_MIN_CONFIDENCE - 0.05
        above = DEFAULT_AUTO_PUBLISH_MIN_CONFIDENCE + 0.05
        assert resolve_initial_status(cfg, confidence=below) == "pending"
        assert resolve_initial_status(cfg, confidence=above) == "approved"

    def test_threshold_differs_from_review_queue_at_high_confidence(self):
        """The exact bug reported in #1573 finding 3: 'threshold' used to be
        indistinguishable from 'review_queue' because nothing branched on
        the mode. High-confidence + threshold must now diverge."""
        review_queue_result = resolve_initial_status({"approval_mode": "review_queue"}, confidence=0.99)
        threshold_result = resolve_initial_status(
            {"approval_mode": "threshold", "auto_publish_min_confidence": 0.5}, confidence=0.99
        )
        assert review_queue_result == "pending"
        assert threshold_result == "approved"
        assert review_queue_result != threshold_result


class TestUnrecognizedMode:
    def test_unrecognized_mode_queues_and_warns(self, caplog):
        with caplog.at_level(logging.WARNING, logger="services.corporate_memory.governance"):
            result = resolve_initial_status({"approval_mode": "auto_approve_typo"})
        assert result == "pending"
        assert any("not recognized" in r.message for r in caplog.records)

    def test_known_modes_do_not_warn(self, caplog):
        with caplog.at_level(logging.WARNING, logger="services.corporate_memory.governance"):
            for mode in ("review_queue", "auto_publish", "threshold"):
                resolve_initial_status({"approval_mode": mode}, confidence=0.1)
        assert caplog.records == []
