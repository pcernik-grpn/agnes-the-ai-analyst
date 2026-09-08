"""The content-export policy — placement and consent, decided once.

Prompt and completion TEXT leaves this instance only under a recorded
decision: a mode, who runs the collector, on what basis, approved by whom.
A mode without that record is `off` and says so at startup, and the old
`AGNES_OTEL_CAPTURE_CONTENT` switch no longer enables anything on its own.
"""

from __future__ import annotations

import logging

import pytest

from src.observability import content_policy as cp


def _cfg(**block):
    return {"observability": {"content_export": block}}


def test_off_when_absent(monkeypatch):
    monkeypatch.delenv(cp.DEPRECATED_ENV_VAR, raising=False)
    p = cp.load_content_export_policy({})
    assert p.mode == "off" and p.warnings == ()


@pytest.mark.parametrize("mode", ["pseudonymized", "full"])
def test_a_complete_record_enables_the_requested_mode(mode):
    p = cp.load_content_export_policy(
        _cfg(mode=mode, placement="operator", basis="DPA §4", approved_by="A. Person", approved_at="2026-09-01")
    )
    assert p.mode == mode and p.placement == "operator" and p.approved_by == "A. Person"


@pytest.mark.parametrize("missing", ["basis", "approved_by", "placement"])
def test_a_mode_without_a_basis_is_off_loudly(missing):
    block = dict(mode="full", placement="third_party", basis="contract", approved_by="x", approved_at="2026-09-01")
    block[missing] = ""
    p = cp.load_content_export_policy(_cfg(**block))
    assert p.mode == "off" and p.requested_mode == "full"
    assert any("without a recorded basis" in w for w in p.warnings)


def test_unknown_mode_or_placement_is_off():
    assert (
        cp.load_content_export_policy(_cfg(mode="everything", placement="operator", basis="b", approved_by="a")).mode
        == "off"
    )
    assert cp.load_content_export_policy(_cfg(mode="full", placement="cloud", basis="b", approved_by="a")).mode == "off"


def test_yaml_off_parses_as_a_boolean_and_still_means_off(monkeypatch):
    """`mode: off` in YAML 1.1 is the boolean False, not the string."""
    monkeypatch.delenv(cp.DEPRECATED_ENV_VAR, raising=False)
    p = cp.load_content_export_policy(_cfg(mode=False, placement="operator", basis="b", approved_by="a"))
    assert p.mode == "off" and p.requested_mode == "off" and p.warnings == ()


def test_the_env_var_is_a_deprecated_alias_that_never_enables_content(monkeypatch):
    monkeypatch.setenv(cp.DEPRECATED_ENV_VAR, "1")
    p = cp.load_content_export_policy({})
    assert p.mode == "off"
    assert any("deprecated" in w for w in p.warnings)


def test_export_text_by_mode(monkeypatch):
    monkeypatch.setattr(cp, "content_export_mode", lambda: "full")
    assert cp.export_text("Jane <jane@example.com>") == "Jane <jane@example.com>"
    monkeypatch.setattr(cp, "content_export_mode", lambda: "off")
    assert cp.export_text("x") == ""


def test_pseudonymized_export_runs_the_anonymizer(monkeypatch):
    monkeypatch.setattr(cp, "content_export_mode", lambda: "pseudonymized")
    monkeypatch.setattr(cp, "_pseudonym_key", lambda: b"unit-test-key")
    out = cp.export_text("mail jane@example.com now")
    assert "jane@example.com" not in out and "EMAIL_" in out


def test_pseudonymized_export_fails_closed_without_a_key(monkeypatch):
    from src.anonymization_key import AnonymizationKeyError

    def _no_key():
        raise AnonymizationKeyError("none")

    monkeypatch.setattr(cp, "content_export_mode", lambda: "pseudonymized")
    monkeypatch.setattr(cp, "_pseudonym_key", _no_key)
    assert cp.export_text("jane@example.com") == cp.WITHHELD


def test_pseudonymized_export_fails_closed_when_the_anonymizer_raises(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("rules are malformed")

    monkeypatch.setattr(cp, "content_export_mode", lambda: "pseudonymized")
    monkeypatch.setattr(cp, "_pseudonym_key", lambda: b"k")
    monkeypatch.setattr(cp, "anonymize_markdown", _boom)
    assert cp.export_text("jane@example.com") == cp.WITHHELD


def test_announce_logs_once_and_audits(monkeypatch, caplog):
    rows = []
    monkeypatch.setattr(cp, "log_safe", lambda **kw: rows.append(kw))
    monkeypatch.setattr(
        cp,
        "load_content_export_policy",
        lambda config=None: cp.ContentExportPolicy(
            mode="full",
            placement="operator",
            basis="b",
            approved_by="a",
            approved_at="2026-09-01",
            requested_mode="full",
            warnings=(),
        ),
    )
    cp._announced = False
    with caplog.at_level(logging.INFO):
        cp.announce_content_export_policy()
        cp.announce_content_export_policy()
    assert len(rows) == 1 and rows[0]["action"] == "observability.content_export"
    assert rows[0]["params"]["mode"] == "full" and rows[0]["client_kind"] == "system"
    assert rows[0]["user_id"] is None
    assert rows[0]["params"]["basis_recorded"] is True
    # The basis TEXT stays in the config file; the trail records only that
    # one was recorded (content never enters `params`).
    assert "b" not in str(rows[0]["params"].get("basis", ""))


def test_announce_warns_and_never_raises_when_the_trail_is_unavailable(monkeypatch, caplog):
    def _explode(**kw):
        raise RuntimeError("audit repo down")

    monkeypatch.setattr(cp, "log_safe", _explode)
    monkeypatch.setattr(
        cp,
        "load_content_export_policy",
        lambda config=None: cp.ContentExportPolicy(
            mode="off",
            placement="operator",
            basis="",
            approved_by="",
            approved_at="",
            requested_mode="full",
            warnings=("content export requested without a recorded basis; exporting sizes only",),
        ),
    )
    cp._announced = False
    with caplog.at_level(logging.WARNING):
        cp.announce_content_export_policy()  # never raises
    assert any("without a recorded basis" in r.getMessage() for r in caplog.records)


def test_announce_never_runs_the_anonymizer(monkeypatch):
    """Startup must not provision a pseudonym key as a side effect."""
    calls = []
    monkeypatch.setattr(cp, "log_safe", lambda **kw: None)
    monkeypatch.setattr(cp, "_pseudonym_key", lambda: calls.append("key") or b"k")
    monkeypatch.setattr(cp, "anonymize_markdown", lambda *a, **k: calls.append("anon"))
    monkeypatch.setattr(
        cp,
        "load_content_export_policy",
        lambda config=None: cp.ContentExportPolicy(
            mode="pseudonymized",
            placement="operator",
            basis="b",
            approved_by="a",
            approved_at="2026-09-01",
            requested_mode="pseudonymized",
            warnings=(),
        ),
    )
    cp._announced = False
    cp.announce_content_export_policy()
    assert calls == []


def test_content_export_mode_reads_the_config_per_call(monkeypatch):
    """No cache: an operator who fixes the record does not restart to be heard
    by the next batch (the relay reads the mode per request)."""
    modes = iter(["full", "off"])
    monkeypatch.setattr(
        cp,
        "load_content_export_policy",
        lambda config=None: cp.ContentExportPolicy(
            mode=next(modes),
            placement="operator",
            basis="b",
            approved_by="a",
            approved_at="",
            requested_mode="full",
            warnings=(),
        ),
    )
    assert cp.content_export_mode() == "full"
    assert cp.content_export_mode() == "off"
