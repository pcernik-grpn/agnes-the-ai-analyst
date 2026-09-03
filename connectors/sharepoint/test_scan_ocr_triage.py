"""Tests for the scan-OCR triage flow: stage 0 (metadata rules), stage 1
(preview + classify), stage 2 (continuation), and the run-report counters.

Reuses ``test_scan_ocr.py``'s fakes (``_FakeClient``, ``_enable``, PDF
fixtures) — same "no network, ever" discipline as that module, extended with
``_ToolResponse`` for the classify call's tool-use reply.
"""

from __future__ import annotations

import pytest

from connectors.sharepoint.convert import convert_to_markdown


pypdfium2 = pytest.importorskip("pypdfium2", reason="extraction extra not installed")
pytest.importorskip("PIL", reason="Pillow is required to encode rendered pages")

from connectors.sharepoint import scan_ocr  # noqa: E402
from connectors.sharepoint.scan_ocr import (  # noqa: E402
    ScanOcrSettings,
    ScanTranscriber,
    Stage0Decision,
    stage0_decision,
)
from connectors.sharepoint.test_scan_ocr import _enable, _FakeClient, _scan_pdf  # noqa: E402


# --------------------------------------------------------------- stage 0


def _settings(**overrides) -> ScanOcrSettings:
    return ScanOcrSettings(enabled=True, triage_enabled=True, **overrides)


class TestStage0Decision:
    def test_no_rules_configured_falls_through_to_triage(self):
        decision = stage0_decision(
            source_path="Reports/2025/brief.pdf", num_pages=10, size_bytes=1_000, settings=_settings()
        )
        assert decision == Stage0Decision("triage", "")

    def test_a_skip_path_pattern_match_stops_at_skip(self):
        settings = _settings(skip_path_patterns=("Tax Returns", "Archive"))
        decision = stage0_decision(
            source_path="Clients/Acme/Tax Returns/2024/return.pdf", num_pages=5, size_bytes=1_000, settings=settings
        )
        assert decision.action == "skip"
        assert decision.reason == "skip_path_pattern:Tax Returns"

    def test_skip_pattern_matching_is_case_insensitive_substring(self):
        settings = _settings(skip_path_patterns=("invoices",))
        decision = stage0_decision(
            source_path="Finance/INVOICES/2025/inv-001.pdf", num_pages=2, size_bytes=1_000, settings=settings
        )
        assert decision.action == "skip"

    def test_a_full_path_pattern_match_wins_even_over_a_skip_pattern(self):
        """An explicit operator override always wins — the whole point of
        having two lists rather than one."""
        settings = _settings(
            skip_path_patterns=("Data Room",),
            full_path_patterns=("Data Room/*Contract*",),
        )
        decision = stage0_decision(
            source_path="Deals/Acme/Data Room/Master Contract.pdf",
            num_pages=40,
            size_bytes=1_000,
            settings=settings,
        )
        assert decision.action == "full"
        assert decision.reason == "full_path_pattern:Data Room/*Contract*"

    def test_full_path_pattern_supports_glob(self):
        settings = _settings(full_path_patterns=("CIM*",))
        decision = stage0_decision(source_path="Deals/CIM Draft.pdf", num_pages=3, size_bytes=1_000, settings=settings)
        assert decision.action == "full"

    def test_a_pattern_that_does_not_match_is_ignored(self):
        settings = _settings(skip_path_patterns=("Archive",))
        decision = stage0_decision(
            source_path="Contracts/2025/msa.pdf", num_pages=3, size_bytes=1_000, settings=settings
        )
        assert decision.action == "triage"

    def test_min_pages_sends_a_short_document_straight_to_full(self):
        settings = _settings(min_pages=3, skip_path_patterns=("Contracts",))
        decision = stage0_decision(
            # Would otherwise match the skip pattern — min_pages wins because
            # it is checked first (transcribing 2 pages in full IS the cheap
            # path; triaging it would cost more than it saves).
            source_path="Contracts/short.pdf",
            num_pages=2,
            size_bytes=1_000,
            settings=settings,
        )
        assert decision == Stage0Decision("full", "min_pages")

    def test_min_pages_zero_disables_the_guard(self):
        settings = _settings(min_pages=0)
        decision = stage0_decision(source_path="a.pdf", num_pages=1, size_bytes=1, settings=settings)
        assert decision.action == "triage"

    def test_max_size_mb_guard_forces_skip(self):
        settings = _settings(max_size_mb=5.0)
        decision = stage0_decision(source_path="scans/huge.pdf", num_pages=10, size_bytes=6_000_000, settings=settings)
        assert decision == Stage0Decision("skip", "max_size_mb")

    def test_max_size_mb_under_the_cap_is_untouched(self):
        settings = _settings(max_size_mb=5.0)
        decision = stage0_decision(source_path="scans/small.pdf", num_pages=10, size_bytes=1_000_000, settings=settings)
        assert decision.action == "triage"

    def test_max_pages_for_preview_guard_forces_skip(self):
        settings = _settings(max_pages_for_preview=50)
        decision = stage0_decision(source_path="a.pdf", num_pages=51, size_bytes=1, settings=settings)
        assert decision == Stage0Decision("skip", "max_pages_for_preview")

    def test_max_pages_for_preview_at_the_cap_is_untouched(self):
        settings = _settings(max_pages_for_preview=50)
        decision = stage0_decision(source_path="a.pdf", num_pages=50, size_bytes=1, settings=settings)
        assert decision.action == "triage"


# ------------------------------------------------------- config resolution


class TestTriageConfigResolution:
    def test_disabled_dataclass_default_keeps_existing_direct_construction_on_the_legacy_path(self):
        """The load-bearing compatibility guarantee: every OTHER test in
        this suite (and any external caller) that builds
        ``ScanOcrSettings(enabled=True, ...)`` directly, without mentioning
        triage at all, must keep exercising the byte-identical legacy path."""
        assert ScanOcrSettings(enabled=True).triage_enabled is False

    def test_load_settings_defaults_triage_on_when_scan_ocr_itself_is_on(self, monkeypatch):
        values = {("extraction", "scan_ocr", "enabled"): True}
        monkeypatch.setattr(scan_ocr, "_config_value", lambda *path, default=None: values.get(path, default))
        monkeypatch.setattr(scan_ocr, "default_model", lambda: "claude-haiku-4-5")

        assert scan_ocr.load_settings().triage_enabled is True
        assert scan_ocr.load_settings().preview_pages == scan_ocr.DEFAULT_PREVIEW_PAGES

    def test_load_settings_reads_the_full_triage_block(self, monkeypatch):
        values = {
            ("extraction", "scan_ocr", "enabled"): True,
            ("extraction", "scan_ocr", "triage", "enabled"): True,
            ("extraction", "scan_ocr", "triage", "preview_pages"): 3,
            ("extraction", "scan_ocr", "triage", "skip_path_patterns"): ["Archive", "Tax Returns"],
            ("extraction", "scan_ocr", "triage", "full_path_patterns"): "CIM",
            ("extraction", "scan_ocr", "triage", "max_size_mb"): 25,
            ("extraction", "scan_ocr", "triage", "min_pages"): 2,
            ("extraction", "scan_ocr", "triage", "max_pages_for_preview"): 100,
        }
        monkeypatch.setattr(scan_ocr, "_config_value", lambda *path, default=None: values.get(path, default))
        monkeypatch.setattr(scan_ocr, "default_model", lambda: "claude-haiku-4-5")

        settings = scan_ocr.load_settings()
        assert settings.preview_pages == 3
        assert settings.skip_path_patterns == ("Archive", "Tax Returns")
        assert settings.full_path_patterns == ("CIM",)  # a bare string normalizes to a one-item tuple
        assert settings.max_size_mb == 25
        assert settings.min_pages == 2
        assert settings.max_pages_for_preview == 100

    def test_triage_can_be_explicitly_disabled_even_with_scan_ocr_on(self, monkeypatch):
        values = {
            ("extraction", "scan_ocr", "enabled"): True,
            ("extraction", "scan_ocr", "triage", "enabled"): False,
        }
        monkeypatch.setattr(scan_ocr, "_config_value", lambda *path, default=None: values.get(path, default))
        monkeypatch.setattr(scan_ocr, "default_model", lambda: "claude-haiku-4-5")

        assert scan_ocr.load_settings().triage_enabled is False


# --------------------------------------------------------- preview + verdict


class TestPreviewAndVerdict:
    def test_a_triage_decision_that_continues_transcribes_the_rest_and_marks_the_document(self, tmp_path, monkeypatch):
        client = _FakeClient(
            "page one",
            "page two",
            {
                "doc_type": "lease",
                "language": "en",
                "scan_quality": "good",
                "continue": True,
                "reason": "substantive lease terms",
            },
            "page three",
        )
        settings = _enable(monkeypatch, client, triage_enabled=True, preview_pages=2)

        markdown = ScanTranscriber(settings, client=client, source_path="Leases/tenant.pdf").transcribe(
            _scan_pdf(tmp_path, pages=3)
        )

        assert len(client.calls) == 4  # 2 preview pages + 1 classify + 1 continuation page
        assert (
            "<!-- scan_ocr: preview 2 of 3 pages; triage: lease; continue=true; reason=substantive lease terms -->"
            in markdown
        )
        assert "page one" in markdown and "page two" in markdown and "page three" in markdown

    def test_a_triage_decision_that_stops_never_transcribes_past_the_preview(self, tmp_path, monkeypatch):
        client = _FakeClient(
            "cover sheet",
            "blank page",
            {
                "doc_type": "form",
                "language": "en",
                "scan_quality": "fair",
                "continue": False,
                "reason": "boilerplate only",
            },
        )
        settings = _enable(monkeypatch, client, triage_enabled=True, preview_pages=2)

        markdown = ScanTranscriber(settings, client=client, source_path="Forms/w9.pdf").transcribe(
            _scan_pdf(tmp_path, pages=5)
        )

        assert len(client.calls) == 3  # 2 preview pages + 1 classify — the other 3 pages are never touched
        assert "continue=false; reason=boilerplate only" in markdown
        assert "cover sheet" in markdown and "blank page" in markdown

    def test_an_unparseable_verdict_stops_conservatively_never_continues(self, tmp_path, monkeypatch):
        # No tool_use block at all — a plain text reply where a tool call was required.
        client = _FakeClient("page one", "not a tool call")
        settings = _enable(monkeypatch, client, triage_enabled=True, preview_pages=1)

        markdown = ScanTranscriber(settings, client=client).transcribe(_scan_pdf(tmp_path, pages=4))

        assert "continue=false; reason=triage_unparseable" in markdown
        assert len(client.calls) == 2  # preview page + the failed classify attempt, nothing past it

    def test_a_verdict_missing_the_continue_field_is_unparseable(self, tmp_path, monkeypatch):
        client = _FakeClient(
            "page one", {"doc_type": "x", "language": "en", "scan_quality": "good", "reason": "no continue key"}
        )
        settings = _enable(monkeypatch, client, triage_enabled=True, preview_pages=1)

        markdown = ScanTranscriber(settings, client=client).transcribe(_scan_pdf(tmp_path, pages=2))

        assert "reason=triage_unparseable" in markdown

    def test_a_verdict_where_continue_is_the_string_true_is_unparseable(self, tmp_path, monkeypatch):
        """The near-miss this guards against: a model returning the STRING
        "true" instead of the JSON boolean must never be truthy-coerced."""
        client = _FakeClient(
            "page one", {"doc_type": "x", "language": "en", "scan_quality": "good", "continue": "true", "reason": "r"}
        )
        settings = _enable(monkeypatch, client, triage_enabled=True, preview_pages=1)

        markdown = ScanTranscriber(settings, client=client).transcribe(_scan_pdf(tmp_path, pages=2))

        assert "continue=false; reason=triage_unparseable" in markdown

    def test_a_classify_call_that_raises_stops_conservatively(self, tmp_path, monkeypatch):
        client = _FakeClient("page one", RuntimeError("network blip"))
        settings = _enable(monkeypatch, client, triage_enabled=True, preview_pages=1)

        markdown = ScanTranscriber(settings, client=client).transcribe(_scan_pdf(tmp_path, pages=3))

        assert "reason=triage_unparseable" in markdown

    def test_scan_quality_outside_the_closed_vocabulary_normalizes_to_unknown(self, tmp_path, monkeypatch):
        client = _FakeClient(
            "page one",
            {"doc_type": "x", "language": "en", "scan_quality": "excellent!!1", "continue": False, "reason": "r"},
        )
        settings = _enable(monkeypatch, client, triage_enabled=True, preview_pages=1)

        ScanTranscriber(settings, client=client).transcribe(_scan_pdf(tmp_path, pages=2))
        # No assertion on scan_quality directly (not surfaced in the marker) —
        # the call must simply not raise on an out-of-vocabulary value.

    def test_a_stage0_skip_decision_forces_the_verdict_without_a_classify_call(self, tmp_path, monkeypatch):
        client = _FakeClient("page one", "page two")
        settings = _enable(monkeypatch, client, triage_enabled=True, preview_pages=2, skip_path_patterns=("Archive",))

        markdown = ScanTranscriber(settings, client=client, source_path="Old/Archive/doc.pdf").transcribe(
            _scan_pdf(tmp_path, pages=5)
        )

        assert len(client.calls) == 2  # preview only — no classify call at all
        assert "triage: unclassified; continue=false; reason=skip_path_pattern:Archive" in markdown

    def test_a_stage0_full_decision_transcribes_everything_with_no_marker(self, tmp_path, monkeypatch):
        client = _FakeClient("page one", "page two")
        settings = _enable(monkeypatch, client, triage_enabled=True, preview_pages=1, full_path_patterns=("Contracts",))

        markdown = ScanTranscriber(settings, client=client, source_path="Deals/Contracts/msa.pdf").transcribe(
            _scan_pdf(tmp_path, pages=2)
        )

        assert len(client.calls) == 2
        assert "<!-- scan_ocr" not in markdown
        assert markdown == "page one\n\n---\n\npage two"

    def test_preview_pages_zero_returns_the_pre_triage_empty_route(self, tmp_path, monkeypatch):
        client = _FakeClient("this must never be requested")
        settings = _enable(monkeypatch, client, triage_enabled=True, preview_pages=0, skip_path_patterns=("Archive",))

        markdown = ScanTranscriber(settings, client=client, source_path="Old/Archive/doc.pdf").transcribe(
            _scan_pdf(tmp_path, pages=3)
        )

        assert markdown == ""
        assert client.calls == []

    def test_triage_disabled_is_byte_identical_to_the_pre_triage_full_path(self, tmp_path, monkeypatch):
        client = _FakeClient("page one", "page two")
        settings = _enable(monkeypatch, client, triage_enabled=False)

        markdown = ScanTranscriber(settings, client=client, source_path="Anything/at/all.pdf").transcribe(
            _scan_pdf(tmp_path, pages=2)
        )

        assert markdown == "page one\n\n---\n\npage two"
        assert "<!-- scan_ocr" not in markdown

    def test_the_untrusted_data_boundary_wraps_the_classify_call(self, tmp_path, monkeypatch):
        client = _FakeClient(
            "page one", {"doc_type": "x", "language": "en", "scan_quality": "good", "continue": False, "reason": "r"}
        )
        settings = _enable(monkeypatch, client, triage_enabled=True, preview_pages=1)

        ScanTranscriber(settings, client=client).transcribe(_scan_pdf(tmp_path, pages=2))

        classify_call = client.calls[-1]
        assert classify_call["tool_choice"] == {"type": "tool", "name": "classify_scanned_document"}
        system_text = classify_call["system"][0]["text"]
        assert "UNTRUSTED" in system_text and "NOT instructions" in system_text
        user_text = classify_call["messages"][0]["content"][0]["text"]
        assert "<<<UNTRUSTED_SOURCE_DATA" in user_text
        assert "page one" in user_text


# ----------------------------------------------------------- report counters


class TestTriageRunAccounting:
    def test_previewed_continued_stopped_and_pages_are_counted(self, tmp_path, monkeypatch):
        client = _FakeClient(
            "p1",
            "p2",
            {"doc_type": "x", "language": "en", "scan_quality": "good", "continue": True, "reason": "r"},
            "p3",
        )
        settings = _enable(monkeypatch, client, triage_enabled=True, preview_pages=2)
        scan_ocr.reset_run_usage()

        scan_ocr.transcribe_scan(_scan_pdf(tmp_path, pages=3, name="a.pdf"), settings=settings, client=client)

        usage = scan_ocr.triage_run_usage()
        assert usage["previewed"] == 1
        assert usage["continued"] == 1
        assert usage["stopped"] == 0
        assert usage["pages_transcribed"] == 3

    def test_a_stopped_document_is_counted_with_its_stop_reason_bucketed(self, tmp_path, monkeypatch):
        client = _FakeClient("p1", "p2")
        settings = _enable(monkeypatch, client, triage_enabled=True, preview_pages=2, skip_path_patterns=("Archive",))
        scan_ocr.reset_run_usage()

        scan_ocr.transcribe_scan(
            _scan_pdf(tmp_path, pages=5, name="a.pdf"),
            settings=settings,
            client=client,
            source_path="Old/Archive/a.pdf",
        )

        usage = scan_ocr.triage_run_usage()
        assert usage["previewed"] == 1
        assert usage["stopped"] == 1
        assert usage["continued"] == 0
        assert usage["pages_transcribed"] == 2
        assert usage["stop_reasons"] == {"skip_path_pattern": 1}

    def test_an_unparseable_stop_buckets_under_triage_unparseable(self, tmp_path, monkeypatch):
        client = _FakeClient("p1", "not a tool call")
        settings = _enable(monkeypatch, client, triage_enabled=True, preview_pages=1)
        scan_ocr.reset_run_usage()

        scan_ocr.transcribe_scan(_scan_pdf(tmp_path, pages=2, name="a.pdf"), settings=settings, client=client)

        assert scan_ocr.triage_run_usage()["stop_reasons"] == {"triage_unparseable": 1}

    def test_a_full_stage0_decision_never_touches_the_triage_counters(self, tmp_path, monkeypatch):
        client = _FakeClient("p1", "p2")
        settings = _enable(monkeypatch, client, triage_enabled=True, preview_pages=1, full_path_patterns=("Contracts",))
        scan_ocr.reset_run_usage()

        scan_ocr.transcribe_scan(
            _scan_pdf(tmp_path, pages=2, name="a.pdf"),
            settings=settings,
            client=client,
            source_path="Deals/Contracts/a.pdf",
        )

        assert scan_ocr.triage_run_usage() == {}

    def test_reset_run_usage_zeroes_the_triage_counters_too(self, tmp_path, monkeypatch):
        client = _FakeClient(
            "p1", {"doc_type": "x", "language": "en", "scan_quality": "good", "continue": False, "reason": "r"}
        )
        settings = _enable(monkeypatch, client, triage_enabled=True, preview_pages=1)
        scan_ocr.transcribe_scan(_scan_pdf(tmp_path, pages=2, name="a.pdf"), settings=settings, client=client)
        assert scan_ocr.triage_run_usage()["previewed"] == 1

        scan_ocr.reset_run_usage()

        assert scan_ocr.triage_run_usage() == {}

    def test_the_existing_token_usage_block_keeps_working_alongside_triage(self, tmp_path, monkeypatch):
        """Non-negotiable: the pre-existing ``ocr_usage`` (token) accounting
        must not regress just because triage adds its own counters."""
        client = _FakeClient(
            "p1", {"doc_type": "x", "language": "en", "scan_quality": "good", "continue": False, "reason": "r"}
        )
        settings = _enable(monkeypatch, client, triage_enabled=True, preview_pages=1)
        scan_ocr.reset_run_usage()

        scan_ocr.transcribe_scan(_scan_pdf(tmp_path, pages=2, name="a.pdf"), settings=settings, client=client)

        run_usage = scan_ocr.run_usage()
        assert run_usage["calls"] == 2  # 1 preview page + 1 classify call
        assert run_usage["input_tokens"] > 0


# --------------------------------------------------------- convert.py seam


class TestConvertPySourcePathThreading:
    def test_source_path_reaches_the_triage_stage0_rules_through_convert_to_markdown(self, tmp_path, monkeypatch):
        client = _FakeClient("this must never be requested")
        _enable(monkeypatch, client, triage_enabled=True, preview_pages=0, skip_path_patterns=("Archive",))
        path = tmp_path / "scan.pdf"
        from connectors.sharepoint.test_convert import _build_pdf

        path.write_bytes(_build_pdf([[]]))

        result = convert_to_markdown(path, "application/pdf", source_path="Old/Archive/scan.pdf")

        assert result.engine == "empty"
        assert client.calls == []

    def test_no_source_path_matches_no_pattern(self, tmp_path, monkeypatch):
        client = _FakeClient("page text")
        _enable(monkeypatch, client, triage_enabled=True, preview_pages=1, skip_path_patterns=("Archive",))
        path = tmp_path / "scan.pdf"
        from connectors.sharepoint.test_convert import _build_pdf

        path.write_bytes(_build_pdf([[]]))

        result = convert_to_markdown(path, "application/pdf")

        assert result.engine == "ocr"
        assert "page text" in result.markdown
