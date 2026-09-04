"""Provider-limit classification and the fleet-level condition it feeds
(TCRD-296 synthesis F.25, gaps #25/#48).

No network, no Postgres: every test here drives a mocked client or calls
the module's own pure helpers directly. The end-to-end half — the
repository, and ``run_facts_extraction``/``_run_batch_pass`` actually
stopping cleanly and recording a real condition row — lives in
``tests/db_pg/test_extraction_conditions_pg.py`` and
``tests/db_pg/test_facts_extraction_pg.py`` (both Postgres-only, since
``extraction_conditions`` is a PG-only table, A3 ratchet).
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

import connectors.sharepoint.facts_extraction as fe
from connectors.sharepoint.facts_extraction import (
    FactsDocumentError,
    FactsExtractionUnavailable,
    ProviderLimitHit,
    _Extractor,
    classify_provider_limit_error,
    vertex_region_supports_model,
)

# ---------------------------------------------------------------------------
# Fakes (local, minimal — see tests/test_facts_extraction.py for the fuller
# versions used by the rest of this module's test suite)
# ---------------------------------------------------------------------------


class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Usage:
    def __init__(self) -> None:
        self.input_tokens = 10
        self.output_tokens = 5
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = 0


class _Response:
    def __init__(self, text: str = "NODES\nEDGES\n") -> None:
        self.content = [_Block(text)]
        self.usage = _Usage()


class _Messages:
    def __init__(self, outer: "FakeClient") -> None:
        self._outer = outer

    def create(self, **kwargs):
        with self._outer.lock:
            self._outer.calls.append(kwargs)
            index = min(len(self._outer.calls) - 1, len(self._outer.script) - 1)
        step = self._outer.script[index]
        if isinstance(step, BaseException):
            raise step
        return step


class FakeClient:
    def __init__(self, *script) -> None:
        self.script = list(script) or [_Response()]
        self.calls: list[dict] = []
        self.lock = threading.Lock()
        self.messages = _Messages(self)


class _WorkspaceLimitError(Exception):
    """Shape observed live: a 400 whose ``.type`` is ``invalid_request_error``
    and whose message names the workspace usage limit — the same status/type
    a per-document "prompt is too long" error carries, distinguished only by
    message content (see ``classify_provider_limit_error``'s docstring)."""

    status_code = 400
    type = "invalid_request_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class _VertexQuotaExhausted(Exception):
    """Shape observed live: Vertex's own 429 whose message names the
    exhausted quota metric — retryable-SHAPED (429), but retrying the SAME
    region reproduces it identically."""

    status_code = 429

    def __init__(self, message: str) -> None:
        super().__init__(message)


# ---------------------------------------------------------------------------
# classify_provider_limit_error — one test per closed-set reason, plus the
# negative cases the AIMD/backoff (or the per-document classifier) already
# owns.
# ---------------------------------------------------------------------------


class TestClassifyProviderLimitError:
    def test_workspace_usage_limit_exhaustion(self):
        exc = _WorkspaceLimitError(
            "Your workspace has hit the API usage limits for on-demand daily spend. You'll regain access on 2026-10-01."
        )
        assert classify_provider_limit_error(exc) == "workspace_limit"

    def test_vertex_quota_bucket_with_zero_allocation(self):
        exc = _VertexQuotaExhausted(
            "429 Resource exhausted. Quota exceeded for quota metric "
            "'aiplatform.googleapis.com/generate_content_requests' and limit "
            "'GenerateContentRequestsPerMinutePerProjectPerRegion' for consumer 'project_number:123'."
        )
        assert classify_provider_limit_error(exc) == "quota_exceeded"

    def test_billing_disabled(self):
        assert classify_provider_limit_error(Exception("billing is disabled for this project")) == "billing_disabled"
        assert (
            classify_provider_limit_error(Exception("Billing account is inactive for this project"))
            == "billing_disabled"
        )

    def test_an_ordinary_transient_rate_limit_does_not_classify(self):
        """This is exactly what `_is_retryable`'s AIMD/backoff already
        handles — classifying it here would short-circuit a retry that
        would likely have succeeded."""
        assert classify_provider_limit_error(Exception("rate limit exceeded, please retry")) is None
        assert classify_provider_limit_error(Exception("too many requests")) is None

    def test_a_per_document_invalid_request_does_not_classify(self):
        """ "prompt is too long" is `_classify_permanent_error`'s own
        territory (`FactsDocumentError`), not a fleet-level condition."""
        assert classify_provider_limit_error(Exception("invalid_request_error: prompt is too long")) is None

    def test_an_unrelated_5xx_does_not_classify(self):
        assert classify_provider_limit_error(Exception("internal server error")) is None


# ---------------------------------------------------------------------------
# _Extractor.call() — where the classification actually changes the raised
# exception type
# ---------------------------------------------------------------------------


class TestExtractorRaisesProviderLimitHit:
    def test_a_workspace_limit_error_is_raised_immediately_without_retrying(self):
        """Non-retryable shaped (400) AND classified — no backoff sleep, no
        second attempt: retrying an identical resend would only reproduce
        the same refusal."""
        client = FakeClient(_WorkspaceLimitError("Your workspace has hit the API usage limits ..."))
        extractor = _Extractor(
            system_prompt="SYSTEM", model="claude-haiku-4-5", client=client, max_attempts=3, sleep=lambda _s: None
        )
        with pytest.raises(ProviderLimitHit) as exc_info:
            extractor.call("hello")
        assert exc_info.value.reason == "workspace_limit"
        assert len(client.calls) == 1

    def test_a_vertex_quota_error_is_raised_only_after_retries_exhaust(self):
        """429-shaped (retryable per `_is_retryable`) — the AIMD/backoff DOES
        get its chances; classification only fires once every attempt has
        failed identically, which is what tells a structural zero-
        allocation bucket apart from an ordinary transient spike."""
        client = FakeClient(
            _VertexQuotaExhausted("Quota exceeded for quota metric X"),
            _VertexQuotaExhausted("Quota exceeded for quota metric X"),
            _VertexQuotaExhausted("Quota exceeded for quota metric X"),
        )
        extractor = _Extractor(
            system_prompt="SYSTEM", model="claude-sonnet-4-6", client=client, max_attempts=3, sleep=lambda _s: None
        )
        with pytest.raises(ProviderLimitHit) as exc_info:
            extractor.call("hello")
        assert exc_info.value.reason == "quota_exceeded"
        assert len(client.calls) == 3

    def test_a_genuinely_transient_429_still_succeeds_on_retry(self):
        """The negative control: classification must never intercept an
        ordinary transient failure that recovers on its own."""
        client = FakeClient(_VertexQuotaExhausted("rate limited, try again"), _Response("NODES\nEDGES\n"))
        extractor = _Extractor(
            system_prompt="SYSTEM", model="claude-sonnet-4-6", client=client, max_attempts=3, sleep=lambda _s: None
        )
        assert extractor.call("hello") == "NODES\nEDGES\n"
        assert len(client.calls) == 2

    def test_a_bare_prompt_too_long_still_raises_facts_document_error(self):
        """Regression guard: the provider-limit check must not shadow the
        existing per-document classification for an ordinary 400."""

        class _Boom(Exception):
            status_code = 400

        client = FakeClient(_Boom())
        extractor = _Extractor(
            system_prompt="SYSTEM", model="claude-haiku-4-5", client=client, max_attempts=3, sleep=lambda _s: None
        )
        with pytest.raises(FactsDocumentError):
            extractor.call("hello")

    def test_an_unclassified_exhausted_failure_still_raises_facts_extraction_unavailable(self):
        class _Boom(Exception):
            status_code = 503

        client = FakeClient(_Boom(), _Boom(), _Boom())
        extractor = _Extractor(
            system_prompt="SYSTEM", model="claude-haiku-4-5", client=client, max_attempts=3, sleep=lambda _s: None
        )
        with pytest.raises(FactsExtractionUnavailable):
            extractor.call("hello")


# ---------------------------------------------------------------------------
# retry_after extraction
# ---------------------------------------------------------------------------


class TestRetryAfterSeconds:
    def test_reads_a_retry_after_attribute(self):
        exc = Exception("quota exceeded")
        exc.retry_after = 42
        assert fe._retry_after_seconds(exc) == 42

    def test_reads_a_response_header(self):
        exc = Exception("quota exceeded")
        exc.response = type("_R", (), {"headers": {"retry-after": "17"}})()
        assert fe._retry_after_seconds(exc) == 17

    def test_absent_is_none(self):
        assert fe._retry_after_seconds(Exception("quota exceeded")) is None

    def test_a_non_positive_value_is_none(self):
        exc = Exception("quota exceeded")
        exc.retry_after = 0
        assert fe._retry_after_seconds(exc) is None


# ---------------------------------------------------------------------------
# _submit_batch — the live incident's exact failure point
# ---------------------------------------------------------------------------


class _RefusingBatchesEndpoint:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def create(self, *, requests):
        raise self._exc


def test_submit_batch_classifies_a_workspace_limit_refusal():
    from connectors.sharepoint.facts_extraction import _Work, _submit_batch

    endpoint = _RefusingBatchesEndpoint(_WorkspaceLimitError("Your workspace has hit the API usage limits ..."))
    client = type("_Client", (), {"messages": type("_M", (), {"batches": endpoint})()})()
    work = _Work(
        file_id="cf_1",
        doc_id="doc1",
        collection_id="col_a",
        filename="f.md",
        path="f.md",
        sha256="sha-1",
        mapping={},
        chunk_texts=["text"],
        user_message="hello",
    )
    with pytest.raises(ProviderLimitHit) as exc_info:
        _submit_batch(
            client,
            model="claude-haiku-4-5",
            system_prompt="sys",
            works=[work],
            messages_by_file={"cf_1": "hello"},
            max_output_tokens=100,
        )
    assert exc_info.value.reason == "workspace_limit"


def test_submit_batch_still_wraps_an_unrelated_failure_as_unavailable():
    from connectors.sharepoint.facts_extraction import _Work, _submit_batch

    endpoint = _RefusingBatchesEndpoint(Exception("connection reset"))
    client = type("_Client", (), {"messages": type("_M", (), {"batches": endpoint})()})()
    work = _Work(
        file_id="cf_1",
        doc_id="doc1",
        collection_id="col_a",
        filename="f.md",
        path="f.md",
        sha256="sha-1",
        mapping={},
        chunk_texts=["text"],
        user_message="hello",
    )
    with pytest.raises(FactsExtractionUnavailable):
        _submit_batch(
            client,
            model="claude-haiku-4-5",
            system_prompt="sys",
            works=[work],
            messages_by_file={"cf_1": "hello"},
            max_output_tokens=100,
        )


# ---------------------------------------------------------------------------
# The Vertex region × model quota matrix (live finding (b))
# ---------------------------------------------------------------------------


class TestVertexRegionModelMatrix:
    @pytest.mark.parametrize("region", ["global", "us-east5", "europe-west1"])
    def test_haiku_supports_every_documented_region(self, region):
        assert vertex_region_supports_model(region, "claude-haiku-4-5-20251001") is True

    def test_sonnet_is_global_only(self):
        assert vertex_region_supports_model("global", "claude-sonnet-4-6") is True

    @pytest.mark.parametrize("region", ["us-east5", "europe-west1", "us-central1"])
    def test_sonnet_outside_global_has_no_bucket(self, region):
        """The live finding, verbatim: Sonnet answers 429 outside `global`
        even for a 5-token request, because the project has no regional
        bucket at all — this is the guardrail that stops the pass BEFORE
        it ever makes that call."""
        assert vertex_region_supports_model(region, "claude-sonnet-4-6") is False

    def test_an_unlisted_tier_is_unconstrained(self):
        assert vertex_region_supports_model("us-east5", "claude-opus-4-7") is True

    def test_an_empty_region_or_model_is_unconstrained(self):
        assert vertex_region_supports_model("", "claude-sonnet-4-6") is True
        assert vertex_region_supports_model("us-east5", "") is True


# ---------------------------------------------------------------------------
# DuckDB-backend fail-clean posture — every condition helper is a no-op,
# never a crash, when `extraction_conditions_repo()` raises
# `RequiresPostgresBackend` (the PG-only table on a DuckDB-backed instance).
# ---------------------------------------------------------------------------


class TestDuckDbBackendFailsClean:
    def test_active_conditions_is_empty_not_an_error(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("AGNES_DB_URL", raising=False)
        assert fe.active_provider_limit_conditions() == []

    def test_streamed_pass_suppression_check_is_none_not_an_error(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("AGNES_DB_URL", raising=False)
        assert fe.streamed_pass_suppressed_by_provider_limit() is None

    def test_record_and_clear_are_silent_no_ops(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("AGNES_DB_URL", raising=False)
        fe.record_provider_limit_condition(
            reason="workspace_limit",
            provider="anthropic",
            model="claude-haiku-4-5",
            region=None,
            message="m",
            retry_after_s=None,
        )
        fe.clear_provider_limit_conditions("anthropic")  # must not raise


# ---------------------------------------------------------------------------
# The cooldown window itself
# ---------------------------------------------------------------------------


class TestConditionCooldown:
    def test_a_fresh_condition_is_still_cooling_down(self):
        condition = {"last_seen": datetime.now(timezone.utc), "retry_after_s": None}
        assert fe._condition_still_cooling_down(condition) is True

    def test_the_default_cooldown_elapses(self):
        condition = {
            "last_seen": datetime.now(timezone.utc) - timedelta(seconds=fe.PROVIDER_LIMIT_COOLDOWN_S + 5),
            "retry_after_s": None,
        }
        assert fe._condition_still_cooling_down(condition) is False

    def test_a_provider_given_retry_after_overrides_the_default(self):
        condition = {"last_seen": datetime.now(timezone.utc) - timedelta(seconds=10), "retry_after_s": 5}
        assert fe._condition_still_cooling_down(condition) is False

    def test_an_unparseable_last_seen_is_treated_as_still_active(self):
        condition = {"last_seen": "not-a-timestamp", "retry_after_s": None}
        assert fe._condition_still_cooling_down(condition) is True
