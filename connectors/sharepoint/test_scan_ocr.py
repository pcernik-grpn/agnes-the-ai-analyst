"""Tests for :mod:`connectors.sharepoint.scan_ocr` and its converter seam.

No test here reaches a model: every one drives a fake client whose
``messages.create`` records the request it was handed and replays a scripted
reply (or raises a scripted exception). That is deliberate and load-bearing —
the feature's whole point is that it costs money, so its test suite must not.

PDF fixtures are built by ``test_convert._build_pdf``, reused rather than
copied: it hand-writes a conforming PDF (no authoring library exists in this
repo's dependency set), and a page whose placement list is empty is exactly the
fixture this module needs — a real PDF page with no text layer at all, i.e. a
scan as far as the text route can tell.
"""

from __future__ import annotations

import threading
import time

import pytest

from connectors.sharepoint.convert import ConversionError, convert_to_markdown


pypdfium2 = pytest.importorskip("pypdfium2", reason="extraction extra not installed")
pytest.importorskip("PIL", reason="Pillow is required to encode rendered pages")

from connectors.sharepoint import scan_ocr  # noqa: E402
from connectors.sharepoint.scan_ocr import (  # noqa: E402
    MAX_PAGES_CEILING,
    ScanOcrSettings,
    ScanOcrUnavailable,
    ScanTranscriber,
)
from connectors.sharepoint.test_convert import _build_pdf  # noqa: E402


# ----------------------------------------------------------------- fixtures


def _scan_pdf(tmp_path, pages: int = 2, name: str = "scan.pdf"):
    """A PDF with ``pages`` real pages and no text layer on any of them."""
    path = tmp_path / name
    path.write_bytes(_build_pdf([[] for _ in range(pages)]))
    return path


class _Usage:
    def __init__(self, input_tokens=100, output_tokens=20, cache_creation=0, cache_read=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_creation_input_tokens = cache_creation
        self.cache_read_input_tokens = cache_read


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Response:
    def __init__(self, text, usage=None):
        self.content = [_Block(text)]
        self.usage = usage or _Usage()


class _ToolUseBlock:
    def __init__(self, name, input):  # noqa: A002 - mirrors the SDK's own field name
        self.type = "tool_use"
        self.name = name
        self.input = input


class _ToolResponse:
    """A tool-use reply — for the triage classify call. A script entry that
    is a plain ``dict`` is interpreted as the tool's ``input`` payload; the
    tool NAME is read off the request's own ``tool_choice`` so this fake
    never has to know :data:`scan_ocr._TRIAGE_TOOL_NAME` by name."""

    def __init__(self, name, payload, usage=None):
        self.content = [_ToolUseBlock(name, payload)]
        self.usage = usage or _Usage()


class _Messages:
    def __init__(self, client):
        self._client = client

    def create(self, **kwargs):
        self._client.calls.append(kwargs)
        script = self._client.script
        reply = script[min(len(self._client.calls) - 1, len(script) - 1)]
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, dict):
            tool_name = (kwargs.get("tool_choice") or {}).get("name") or "unknown_tool"
            return _ToolResponse(tool_name, reply)
        return _Response(reply)


class _FakeClient:
    """A stand-in for ``anthropic.Anthropic`` — records requests, replays replies.

    ``script`` is one entry per call; the last entry repeats, so a one-element
    script answers every page identically.
    """

    def __init__(self, *script):
        self.script = list(script) or [""]
        self.calls: list[dict] = []
        self.messages = _Messages(self)


def _enable(monkeypatch, client, **overrides):
    """Turn the feature on for one test, with ``client`` as the only backend.

    ``concurrency=1`` unless a test says otherwise: ``_FakeClient`` replays its
    script in call-arrival order, which is only deterministic while pages are
    sequential. The concurrent path has its own tests below, written so that
    ordering is asserted rather than assumed.
    """
    overrides.setdefault("concurrency", 1)
    settings = ScanOcrSettings(enabled=True, model="claude-haiku-4-5", **overrides)
    monkeypatch.setattr(scan_ocr, "scan_ocr_enabled", lambda: True)
    monkeypatch.setattr(scan_ocr, "load_settings", lambda: settings)
    monkeypatch.setattr(scan_ocr, "build_client", lambda model, timeout_s: (client, model))
    scan_ocr.reset_run_usage()
    return settings


# --------------------------------------------------------- the enabled fork


def test_disabled_scan_is_byte_identical_to_the_empty_route(tmp_path, monkeypatch):
    """OFF is the default and must cost nothing — not one render, not one call."""

    def _boom(*args, **kwargs):  # pragma: no cover - asserted by not firing
        raise AssertionError("scan OCR ran while extraction.scan_ocr.enabled was false")

    monkeypatch.setattr(scan_ocr, "scan_ocr_enabled", lambda: False)
    monkeypatch.setattr(scan_ocr, "transcribe_scan", _boom)

    result = convert_to_markdown(_scan_pdf(tmp_path), "application/pdf")

    assert result.markdown == ""
    assert result.engine == "empty"


def test_disabled_is_the_default_without_any_configuration(monkeypatch):
    monkeypatch.setattr(scan_ocr, "_config_value", lambda *path, default=None: default)

    assert scan_ocr.scan_ocr_enabled() is False
    assert scan_ocr.load_settings() == ScanOcrSettings(enabled=False)


def test_enabled_scan_is_transcribed_and_reports_engine_ocr(tmp_path, monkeypatch):
    client = _FakeClient("page one text", "page two text")
    _enable(monkeypatch, client)

    result = convert_to_markdown(_scan_pdf(tmp_path), "application/pdf")

    assert result.engine == "ocr"
    assert result.markdown == "page one text\n\n---\n\npage two text"
    assert len(client.calls) == 2


def test_a_pdf_with_a_text_layer_never_pays_for_ocr(tmp_path, monkeypatch):
    """The seam fires ONLY for a genuinely empty text layer."""
    client = _FakeClient("this reply must never be requested")
    _enable(monkeypatch, client)
    path = tmp_path / "readable.pdf"
    path.write_bytes(_build_pdf([[("Revenue is recognised on delivery.", 72, 700)]]))

    result = convert_to_markdown(path, "application/pdf")

    assert result.engine == "pypdfium2"
    assert "Revenue is recognised" in result.markdown
    assert client.calls == []


# ------------------------------------------------------------- the page cap


def test_page_cap_transcribes_the_first_n_and_marks_the_truncation(tmp_path, monkeypatch):
    client = _FakeClient("a page")
    _enable(monkeypatch, client, max_pages=2)

    markdown = ScanTranscriber(scan_ocr.load_settings(), client=client).transcribe(_scan_pdf(tmp_path, pages=5))

    assert len(client.calls) == 2, "a 5-page scan must not be transcribed past the cap"
    assert markdown.startswith("a page\n\n---\n\na page")
    assert "[truncated: scan OCR transcribed the first 2 of 5 pages" in markdown
    assert "extraction.scan_ocr.max_pages" in markdown


def test_configured_max_pages_is_clamped_to_the_ceiling(monkeypatch):
    values = {("extraction", "scan_ocr", "enabled"): True, ("extraction", "scan_ocr", "max_pages"): 5000}
    monkeypatch.setattr(scan_ocr, "_config_value", lambda *path, default=None: values.get(path, default))
    monkeypatch.setattr(scan_ocr, "default_model", lambda: "claude-haiku-4-5")

    assert scan_ocr.load_settings().max_pages == MAX_PAGES_CEILING


def test_nonsense_max_pages_falls_back_to_the_default(monkeypatch):
    values = {("extraction", "scan_ocr", "enabled"): True, ("extraction", "scan_ocr", "max_pages"): "lots"}
    monkeypatch.setattr(scan_ocr, "_config_value", lambda *path, default=None: values.get(path, default))
    monkeypatch.setattr(scan_ocr, "default_model", lambda: "claude-haiku-4-5")

    assert scan_ocr.load_settings().max_pages == scan_ocr.DEFAULT_MAX_PAGES


# ------------------------------------------------------- per-page resilience


def test_one_failed_page_never_kills_the_document(tmp_path, monkeypatch):
    client = _FakeClient("alpha", RuntimeError("model hiccup"), "gamma")
    settings = _enable(monkeypatch, client, max_attempts=1)

    transcriber = ScanTranscriber(settings, client=client)
    markdown = transcriber.transcribe(_scan_pdf(tmp_path, pages=3))

    assert "alpha" in markdown and "gamma" in markdown
    # The failed page contributes an EMPTY chunk, so the ``---`` separators
    # still line up with the real page numbers.
    assert markdown == "alpha\n\n---\n\n\n\n---\n\ngamma"
    assert transcriber.last_usage["failed_pages"] == 1
    assert transcriber.last_usage["transcribed_pages"] == 2
    assert transcriber.last_usage["pages"] == 3


def test_a_transient_failure_is_retried_before_the_page_is_given_up(tmp_path, monkeypatch):
    transient = RuntimeError("503 upstream")
    transient.status_code = 503  # type: ignore[attr-defined]
    client = _FakeClient(transient, "recovered")
    settings = _enable(monkeypatch, client, max_attempts=2, backoff_s=0.0)

    transcriber = ScanTranscriber(settings, client=client, sleep=lambda _: None)
    markdown = transcriber.transcribe(_scan_pdf(tmp_path, pages=1))

    assert markdown == "recovered"
    assert len(client.calls) == 2
    assert transcriber.last_usage["failed_pages"] == 0


# ---------------------------------------------------------- hard failures


def test_a_document_whose_every_page_fails_raises_rather_than_returning_empty(tmp_path, monkeypatch):
    client = _FakeClient(RuntimeError("permanently broken"))
    settings = _enable(monkeypatch, client, max_attempts=1)

    with pytest.raises(ScanOcrUnavailable) as excinfo:
        ScanTranscriber(settings, client=client).transcribe(_scan_pdf(tmp_path, pages=2))

    assert "scan OCR" in str(excinfo.value)


def test_a_run_of_leading_failures_aborts_instead_of_burning_the_budget(tmp_path, monkeypatch):
    client = _FakeClient(RuntimeError("wrong model id"))
    settings = _enable(monkeypatch, client, max_attempts=1)

    with pytest.raises(ScanOcrUnavailable):
        ScanTranscriber(settings, client=client).transcribe(_scan_pdf(tmp_path, pages=40))

    assert len(client.calls) == scan_ocr.ABORT_AFTER_LEADING_FAILURES


def test_missing_credentials_raise_rather_than_emptying_the_document(tmp_path, monkeypatch):
    def _no_credentials(model, timeout_s):
        raise ScanOcrUnavailable("scan OCR needs LLM credentials")

    monkeypatch.setattr(scan_ocr, "build_client", _no_credentials)
    settings = ScanOcrSettings(enabled=True, model="claude-haiku-4-5")

    with pytest.raises(ScanOcrUnavailable):
        ScanTranscriber(settings).transcribe(_scan_pdf(tmp_path, pages=2))


def test_the_converter_turns_a_hard_ocr_failure_into_a_conversion_error(tmp_path, monkeypatch):
    """A crawl counts this in ``convert_failed`` — never a silent empty document."""
    client = _FakeClient(RuntimeError("permanently broken"))
    _enable(monkeypatch, client, max_attempts=1)

    with pytest.raises(ConversionError) as excinfo:
        convert_to_markdown(_scan_pdf(tmp_path, pages=2), "application/pdf")

    assert excinfo.value.engine == "ocr"
    assert excinfo.value.filename == "scan.pdf"


def test_an_unopenable_pdf_is_a_typed_failure(tmp_path, monkeypatch):
    client = _FakeClient("unused")
    settings = _enable(monkeypatch, client)
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4 this is not a pdf")

    with pytest.raises(ScanOcrUnavailable):
        ScanTranscriber(settings, client=client).transcribe(path)


# --------------------------------- permanent provider refusal (TCRD-296 #68)
#
# A 400/401/403, or a closed-set `workspace_limit`/`quota_exceeded`/
# `billing_disabled` refusal, must not be retried per page and must pause
# scan OCR for the REST OF THE RUN — not just this document — so a crawl of
# a thousand scans does not burn a bounded set of failed attempts per
# document against a provider that will refuse every one of them.


class _ProviderError(Exception):
    """A provider refusal carrying a real HTTP-shaped ``status_code`` —
    everything ``_permanent_refusal_reason``/``_is_retryable`` classify on."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def _fixed_provider(monkeypatch, provider="anthropic", region=None):
    """Pin :func:`scan_ocr._resolved_provider_and_region` so these tests
    never depend on ambient instance.yaml state — provider resolution
    itself is covered separately in ``tests/test_anonymization_ner.py``."""
    monkeypatch.setattr(scan_ocr, "_resolved_provider_and_region", lambda: (provider, region))


def test_a_400_pauses_the_run_after_one_attempt_never_retried(tmp_path, monkeypatch):
    client = _FakeClient(_ProviderError(400, "invalid_request_error: unsupported model"))
    settings = _enable(monkeypatch, client, max_attempts=3)
    _fixed_provider(monkeypatch)

    markdown = ScanTranscriber(settings, client=client).transcribe(_scan_pdf(tmp_path, pages=2))

    assert markdown == ""
    assert len(client.calls) == 1, "a 400 must not be retried per page"
    disabled = scan_ocr.run_disabled()
    assert disabled == {"disabled_reason": "http_400", "provider_error": "invalid_request_error: unsupported model"}


@pytest.mark.parametrize("status", [400, 401, 403])
def test_401_and_403_pause_the_run_exactly_like_400(tmp_path, monkeypatch, status):
    client = _FakeClient(_ProviderError(status, "refused"))
    settings = _enable(monkeypatch, client, max_attempts=3)
    _fixed_provider(monkeypatch)

    markdown = ScanTranscriber(settings, client=client).transcribe(_scan_pdf(tmp_path, pages=1))

    assert markdown == ""
    assert len(client.calls) == 1
    assert scan_ocr.run_disabled()["disabled_reason"] == f"http_{status}"


def test_a_workspace_limit_400_pauses_the_run_with_the_classified_reason(tmp_path, monkeypatch):
    """The exact live shape (TCRD-296): a 400 whose MESSAGE names a
    workspace usage-limit exhaustion — classified by
    ``classify_provider_limit_error``, same as the facts pass."""
    client = _FakeClient(_ProviderError(400, "Your workspace has hit the API usage limits for on-demand daily spend."))
    settings = _enable(monkeypatch, client, max_attempts=3)
    _fixed_provider(monkeypatch)

    markdown = ScanTranscriber(settings, client=client).transcribe(_scan_pdf(tmp_path, pages=1))

    assert markdown == ""
    assert scan_ocr.run_disabled()["disabled_reason"] == "workspace_limit"


def test_a_quota_exceeded_429_is_retried_before_pausing(tmp_path, monkeypatch):
    """429 is retryable-SHAPED, so the AIMD/backoff gets its chances first —
    a structural zero-allocation quota bucket is told apart from an ordinary
    transient spike only once every attempt fails identically, exactly the
    two-stage order ``_Extractor.call`` uses for the facts pass."""
    quota_error = _ProviderError(429, "Quota exceeded for quota metric X and limit Y for consumer Z")
    client = _FakeClient(quota_error, quota_error, quota_error)
    settings = _enable(monkeypatch, client, max_attempts=3, backoff_s=0.0)
    _fixed_provider(monkeypatch)

    markdown = ScanTranscriber(settings, client=client, sleep=lambda _s: None).transcribe(_scan_pdf(tmp_path, pages=1))

    assert markdown == ""
    assert len(client.calls) == 3, "429 must exhaust its retries before being classified as permanent"
    assert scan_ocr.run_disabled()["disabled_reason"] == "quota_exceeded"


def test_a_document_that_discovers_the_refusal_is_convert_empty_not_convert_failed(tmp_path, monkeypatch):
    """The document that discovers the break gets the SAME `convert_empty`
    outcome every later document gets — never a one-off `convert_failed`
    for just this one."""
    client = _FakeClient(_ProviderError(400, "refused"))
    _enable(monkeypatch, client, max_attempts=1)
    _fixed_provider(monkeypatch)

    result = convert_to_markdown(_scan_pdf(tmp_path, pages=2), "application/pdf")

    assert result.markdown == ""
    assert result.engine == "empty"


def test_the_rest_of_the_run_makes_no_further_provider_calls(tmp_path, monkeypatch):
    """The FIRST permanent refusal wins; every document reached after it —
    even a fresh ``ScanTranscriber`` — returns "" without rendering a page
    or contacting the provider at all."""
    client = _FakeClient(_ProviderError(400, "refused"))
    settings = _enable(monkeypatch, client, max_attempts=1)
    _fixed_provider(monkeypatch)

    ScanTranscriber(settings, client=client).transcribe(_scan_pdf(tmp_path, pages=2, name="one.pdf"))
    assert len(client.calls) == 1

    def _boom(*args, **kwargs):  # pragma: no cover - asserted by not firing
        raise AssertionError("scan OCR must not contact the provider once the run is paused")

    client.messages.create = _boom
    second = ScanTranscriber(settings, client=client).transcribe(_scan_pdf(tmp_path, pages=3, name="two.pdf"))

    assert second == ""


def test_a_permanent_refusal_records_the_fleet_level_provider_limit_condition(tmp_path, monkeypatch):
    """Reuses (never reimplements) the facts pass's own
    ``record_provider_limit_condition`` — the ``ocr_`` prefix on ``reason``
    is the ONLY thing distinguishing an OCR-authored condition from a
    facts-authored one in that shared table."""
    captured = {}

    def fake_record(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("connectors.sharepoint.facts_extraction.record_provider_limit_condition", fake_record)

    client = _FakeClient(_ProviderError(400, "workspace refused"))
    settings = _enable(monkeypatch, client, max_attempts=1)
    _fixed_provider(monkeypatch, provider="vertex", region="us-central1")

    ScanTranscriber(settings, client=client).transcribe(_scan_pdf(tmp_path, pages=1))

    assert captured["reason"] == "ocr_http_400"
    assert captured["provider"] == "vertex"
    assert captured["model"] == settings.model
    assert captured["region"] == "us-central1"
    assert captured["message"] == "workspace refused"


def test_a_transient_failure_is_unaffected_by_the_pause_mechanism(tmp_path, monkeypatch):
    """Negative control: an ordinary transient (5xx-shaped) failure still
    goes through the pre-existing bounded retry and recovers, never the
    whole-run pause."""
    transient = RuntimeError("503 upstream")
    transient.status_code = 503  # type: ignore[attr-defined]
    client = _FakeClient(transient, "recovered")
    settings = _enable(monkeypatch, client, max_attempts=2, backoff_s=0.0)
    _fixed_provider(monkeypatch)

    markdown = ScanTranscriber(settings, client=client, sleep=lambda _s: None).transcribe(_scan_pdf(tmp_path, pages=1))

    assert markdown == "recovered"
    assert scan_ocr.run_disabled() is None


def test_reset_run_usage_clears_the_paused_state(tmp_path, monkeypatch):
    client = _FakeClient(_ProviderError(400, "refused"))
    settings = _enable(monkeypatch, client, max_attempts=1)
    _fixed_provider(monkeypatch)

    ScanTranscriber(settings, client=client).transcribe(_scan_pdf(tmp_path, pages=1))
    assert scan_ocr.run_disabled() is not None

    scan_ocr.reset_run_usage()

    assert scan_ocr.run_disabled() is None


def test_the_run_report_carries_the_disabled_reason_and_provider_error(tmp_path, monkeypatch):
    client = _FakeClient(_ProviderError(403, "credential revoked"))
    settings = _enable(monkeypatch, client, max_attempts=1)
    _fixed_provider(monkeypatch)

    ScanTranscriber(settings, client=client).transcribe(_scan_pdf(tmp_path, pages=1))

    report = scan_ocr.triage_run_usage()
    assert report["disabled_reason"] == "http_403"
    assert report["provider_error"] == "credential revoked"


def test_the_provider_is_logged_once_per_run_not_once_per_document(tmp_path, monkeypatch, caplog):
    client = _FakeClient("p1", "p2")
    settings = _enable(monkeypatch, client)
    _fixed_provider(monkeypatch, provider="vertex", region="us-central1")

    with caplog.at_level("INFO", logger="connectors.sharepoint.scan_ocr"):
        ScanTranscriber(settings, client=client).transcribe(_scan_pdf(tmp_path, pages=1, name="one.pdf"))
        ScanTranscriber(settings, client=client).transcribe(_scan_pdf(tmp_path, pages=1, name="two.pdf"))

    provider_lines = [r for r in caplog.records if "using provider=" in r.getMessage()]
    assert len(provider_lines) == 1
    assert "provider=vertex" in provider_lines[0].getMessage()
    assert "region=us-central1" in provider_lines[0].getMessage()


# ------------------------------------------------------- usage accounting


def test_usage_is_accounted_per_document_and_per_run(tmp_path, monkeypatch):
    client = _FakeClient("text")
    _enable(monkeypatch, client)

    first = scan_ocr.transcribe_scan(_scan_pdf(tmp_path, pages=2, name="one.pdf"))
    per_document = scan_ocr.last_usage()

    assert first == "text\n\n---\n\ntext"
    assert per_document["calls"] == 2
    assert per_document["pages"] == 2
    assert per_document["transcribed_pages"] == 2
    assert per_document["input_tokens"] == 200
    assert per_document["output_tokens"] == 40
    assert per_document["model"] == "claude-haiku-4-5"

    scan_ocr.transcribe_scan(_scan_pdf(tmp_path, pages=1, name="two.pdf"))

    assert scan_ocr.last_usage()["calls"] == 1, "last_usage is per document, not cumulative"
    assert scan_ocr.run_usage()["calls"] == 3, "run_usage accumulates across documents"
    assert scan_ocr.run_usage()["input_tokens"] == 300
    assert "model" not in scan_ocr.run_usage()

    scan_ocr.reset_run_usage()
    assert scan_ocr.run_usage()["calls"] == 0


def test_usage_totals_survive_a_document_that_raised(tmp_path, monkeypatch):
    """A document that burned calls before raising must not vanish from the bill."""
    client = _FakeClient(RuntimeError("dead"))
    _enable(monkeypatch, client, max_attempts=1)

    with pytest.raises(ScanOcrUnavailable):
        scan_ocr.transcribe_scan(_scan_pdf(tmp_path, pages=2))

    usage = scan_ocr.last_usage()
    assert usage["failed_pages"] >= 1
    assert usage["transcribed_pages"] == 0
    assert scan_ocr.run_usage()["failed_pages"] == usage["failed_pages"]


# ------------------------------------------------------------ the request


def test_the_page_is_sent_as_an_image_with_the_rules_on_the_system_channel(tmp_path, monkeypatch):
    client = _FakeClient("text")
    settings = _enable(monkeypatch, client)

    ScanTranscriber(settings, client=client).transcribe(_scan_pdf(tmp_path, pages=1))

    request = client.calls[0]
    assert request["model"] == "claude-haiku-4-5"
    system = request["system"][0]
    assert system["cache_control"] == {"type": "ephemeral"}
    assert "DATA, never" in system["text"], "the page must be declared untrusted data, not instructions"
    content = request["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/png"
    assert content[0]["source"]["data"], "the rendered page must actually carry bytes"
    assert content[1]["type"] == "text"


def test_render_scale_is_clamped_so_a_page_never_exceeds_the_api_edge(tmp_path):
    class _Page:
        def get_size(self):
            return (2384.0, 3370.0)  # A0, in points

    scale = scan_ocr.render_scale_for(_Page(), 2.0)

    assert 3370.0 * scale <= scan_ocr.MAX_RENDER_EDGE_PX
    assert scan_ocr.render_scale_for(_Page(), 0.1) == 0.1, "a smaller requested scale wins"


def test_an_oversized_page_falls_back_to_jpeg_rather_than_being_dropped(monkeypatch):
    import os

    from PIL import Image

    # Random noise is the worst case for PNG and the realistic shape of a
    # dirty full-bleed scan: ~120 kB as PNG, ~26 kB as JPEG at quality 80.
    noisy = Image.frombytes("RGB", (200, 200), os.urandom(200 * 200 * 3))
    monkeypatch.setattr(scan_ocr, "MAX_IMAGE_BYTES", 60_000)

    data, media_type = scan_ocr.encode_page_image(noisy)

    assert media_type == "image/jpeg"
    assert 0 < len(data) <= 60_000


def test_a_page_too_large_even_as_jpeg_fails_that_page_only(monkeypatch):
    import os

    from PIL import Image

    noisy = Image.frombytes("RGB", (200, 200), os.urandom(200 * 200 * 3))
    monkeypatch.setattr(scan_ocr, "MAX_IMAGE_BYTES", 100)

    with pytest.raises(scan_ocr._PageFailed):
        scan_ocr.encode_page_image(noisy)


# ---------------------------------------------------- page concurrency


def _rendezvous_pages(order: list[int], workers: int, delays: dict[int, float]):
    """A ``_transcribe_page`` stand-in that proves the pages really overlap.

    Every page waits on a barrier sized to the worker count, so the fake can
    only complete if ``workers`` pages are genuinely in flight at the same
    moment — a sequential implementation deadlocks into a ``BrokenBarrier``
    instead of quietly passing. Past the barrier each page sleeps its own
    delay, which makes COMPLETION order the reverse of page order without
    depending on how long a page took to render.
    """
    barrier = threading.Barrier(workers, timeout=10)

    def fake(self, index, image, media_type):
        barrier.wait()
        time.sleep(delays.get(index, 0.0))
        order.append(index)
        return f"page {index}"

    return fake


class _InFlight:
    """A ``_transcribe_page`` stand-in that records peak parallelism.

    Patched onto the class as an INSTANCE, which is not a descriptor — so it
    is handed the transcriber's arguments without the transcriber itself.
    """

    def __init__(self) -> None:
        self.peak = 0
        self._now = 0
        self._lock = threading.Lock()

    def __call__(self, index, image, media_type):
        with self._lock:
            self._now += 1
            self.peak = max(self.peak, self._now)
        time.sleep(0.02)
        with self._lock:
            self._now -= 1
        return f"page {index}"


def test_the_default_concurrency_is_three_and_is_clamped_to_the_ceiling(monkeypatch):
    assert ScanOcrSettings().concurrency == scan_ocr.DEFAULT_CONCURRENCY == 3
    assert scan_ocr.clamp_concurrency(99) == scan_ocr.MAX_CONCURRENCY == 8
    assert scan_ocr.clamp_concurrency(0) == 1
    assert scan_ocr.clamp_concurrency(-4) == 1
    assert scan_ocr.clamp_concurrency("plenty") == scan_ocr.DEFAULT_CONCURRENCY

    values = {("extraction", "scan_ocr", "enabled"): True, ("extraction", "scan_ocr", "concurrency"): 50}
    monkeypatch.setattr(scan_ocr, "_config_value", lambda *path, default=None: values.get(path, default))
    monkeypatch.setattr(scan_ocr, "default_model", lambda: "claude-haiku-4-5")

    assert scan_ocr.load_settings().concurrency == scan_ocr.MAX_CONCURRENCY


def test_pages_are_joined_in_page_order_regardless_of_completion_order(tmp_path, monkeypatch):
    completion: list[int] = []
    client = _FakeClient("unused")
    settings = _enable(monkeypatch, client, concurrency=3)
    # All three pages meet at the barrier, then page 0 answers LAST.
    monkeypatch.setattr(
        ScanTranscriber,
        "_transcribe_page",
        _rendezvous_pages(completion, 3, {0: 0.06, 1: 0.03, 2: 0.0}),
    )

    markdown = ScanTranscriber(settings, client=client).transcribe(_scan_pdf(tmp_path, pages=3))

    assert markdown == "page 0\n\n---\n\npage 1\n\n---\n\npage 2"
    assert completion == [2, 1, 0], "the fixture must genuinely finish out of page order"


def test_concurrency_one_is_sequential_and_byte_identical(tmp_path, monkeypatch):
    client = _FakeClient("unused")
    path = _scan_pdf(tmp_path, pages=3)

    one = _InFlight()
    monkeypatch.setattr(ScanTranscriber, "_transcribe_page", one)
    sequential = ScanTranscriber(_enable(monkeypatch, client, concurrency=1), client=client).transcribe(path)
    assert one.peak == 1, "concurrency 1 must not overlap pages at all"

    # The concurrent run uses the barrier fake rather than a sleep: pages are
    # rendered on the calling thread between submissions, so a wall-clock
    # comparison would be measuring PDFium, not parallelism.
    monkeypatch.setattr(ScanTranscriber, "_transcribe_page", _rendezvous_pages([], 3, {}))
    concurrent = ScanTranscriber(_enable(monkeypatch, client, concurrency=3), client=client).transcribe(path)

    assert sequential == concurrent == "page 0\n\n---\n\npage 1\n\n---\n\npage 2"


def test_a_failed_page_is_isolated_on_the_concurrent_path_too(tmp_path, monkeypatch):
    def fake(self, index, image, media_type):
        if index == 1:
            raise scan_ocr._PageFailed("page 2 transcription failed")
        return f"page {index}"

    client = _FakeClient("unused")
    settings = _enable(monkeypatch, client, concurrency=3)
    monkeypatch.setattr(ScanTranscriber, "_transcribe_page", fake)

    transcriber = ScanTranscriber(settings, client=client)
    markdown = transcriber.transcribe(_scan_pdf(tmp_path, pages=3))

    assert markdown == "page 0\n\n---\n\n\n\n---\n\npage 2"
    assert transcriber.last_usage["failed_pages"] == 1
    assert transcriber.last_usage["transcribed_pages"] == 2


def test_the_page_cap_bounds_submission_on_the_concurrent_path(tmp_path, monkeypatch):
    client = _FakeClient("a page")
    settings = _enable(monkeypatch, client, max_pages=2, concurrency=8)

    ScanTranscriber(settings, client=client).transcribe(_scan_pdf(tmp_path, pages=40))

    assert len(client.calls) == 2, "pages past the cap must never be submitted"


def test_leading_failures_abort_the_concurrent_path_within_one_wave(tmp_path, monkeypatch):
    client = _FakeClient(RuntimeError("wrong model id"))
    settings = _enable(monkeypatch, client, max_attempts=1, concurrency=3)

    with pytest.raises(ScanOcrUnavailable):
        ScanTranscriber(settings, client=client).transcribe(_scan_pdf(tmp_path, pages=40))

    # At most one wave beyond the guard's threshold — never the whole cap.
    assert len(client.calls) <= scan_ocr.ABORT_AFTER_LEADING_FAILURES + settings.concurrency


def test_effective_concurrency_is_reported_in_the_usage_block(tmp_path, monkeypatch):
    client = _FakeClient("text")
    _enable(monkeypatch, client, concurrency=99)

    scan_ocr.transcribe_scan(_scan_pdf(tmp_path, pages=2))

    assert scan_ocr.last_usage()["concurrency"] == scan_ocr.MAX_CONCURRENCY
    assert scan_ocr.run_usage()["concurrency"] == scan_ocr.MAX_CONCURRENCY, "run block keeps the max, never a sum"


# ------------------------------------------------------- model resolution


def test_model_resolution_prefers_the_feature_key_then_the_shared_one(monkeypatch):
    monkeypatch.delenv("AGNES_VISION_MODEL", raising=False)

    values = {("extraction", "scan_ocr", "model"): "sonnet", ("extraction", "model"): "claude-haiku-4-5"}
    monkeypatch.setattr(scan_ocr, "_config_value", lambda *path, default=None: values.get(path, default))
    assert scan_ocr.default_model().startswith("claude-")
    assert scan_ocr.default_model() != "claude-haiku-4-5"

    values.pop(("extraction", "scan_ocr", "model"))
    assert scan_ocr.default_model() == "claude-haiku-4-5"


def test_model_resolution_falls_back_to_the_vision_env_then_the_default(monkeypatch):
    monkeypatch.setattr(scan_ocr, "_config_value", lambda *path, default=None: default)

    monkeypatch.setenv("AGNES_VISION_MODEL", "claude-haiku-4-5-20251001")
    assert scan_ocr.default_model() == "claude-haiku-4-5-20251001"

    monkeypatch.delenv("AGNES_VISION_MODEL", raising=False)
    assert scan_ocr.default_model() == scan_ocr.FALLBACK_MODEL
