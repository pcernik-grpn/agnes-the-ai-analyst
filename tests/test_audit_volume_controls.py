"""Wave 2 — Task 4: measure audit_log volume, and give operators a control.

Two pieces:

  1. ``src/audit_helpers.py::should_sample`` — a deterministic, per-action
     sampling gate driven by ``audit.sampling.<action>`` in instance config.
  2. ``scripts/audit_volume_estimate.py`` — an operator script reporting
     rows/day, top actions, and a retention-window size projection.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest

from src.audit_helpers import _SAMPLE_COUNTS, should_sample

_SPEC = importlib.util.spec_from_file_location(
    "audit_volume_estimate",
    Path(__file__).resolve().parents[1] / "scripts" / "audit_volume_estimate.py",
)
ave = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ave)


@pytest.fixture(autouse=True)
def _reset_sample_counts():
    _SAMPLE_COUNTS.clear()
    yield
    _SAMPLE_COUNTS.clear()


# ---------------------------------------------------------------------------
# should_sample
# ---------------------------------------------------------------------------


class TestShouldSample:
    def test_unconfigured_action_is_always_sampled(self, monkeypatch):
        import app.instance_config as ic

        monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: default)
        for _ in range(5):
            assert should_sample("some.unconfigured.action") is True

    def test_ratio_one_is_always_true(self, monkeypatch):
        import app.instance_config as ic

        monkeypatch.setattr(
            ic,
            "get_value",
            lambda *keys, default=None: {"some.action": 1.0} if keys == ("audit", "sampling") else default,
        )
        for _ in range(5):
            assert should_sample("some.action") is True

    def test_ratio_zero_is_never_true(self, monkeypatch):
        import app.instance_config as ic

        monkeypatch.setattr(
            ic,
            "get_value",
            lambda *keys, default=None: {"some.action": 0} if keys == ("audit", "sampling") else default,
        )
        for _ in range(10):
            assert should_sample("some.action") is False

    def test_ratio_tenth_is_true_exactly_once_per_ten_calls(self, monkeypatch):
        import app.instance_config as ic

        monkeypatch.setattr(
            ic,
            "get_value",
            lambda *keys, default=None: {"some.action": 0.1} if keys == ("audit", "sampling") else default,
        )
        results = [should_sample("some.action") for _ in range(10)]
        assert results.count(True) == 1

    def test_sampling_is_deterministic_not_random(self, monkeypatch):
        """Same call sequence from a fresh counter reproduces the same pattern —
        a random sampler would not."""
        import app.instance_config as ic

        monkeypatch.setattr(
            ic,
            "get_value",
            lambda *keys, default=None: {"some.action": 0.25} if keys == ("audit", "sampling") else default,
        )
        first = [should_sample("some.action") for _ in range(8)]
        _SAMPLE_COUNTS.clear()
        second = [should_sample("some.action") for _ in range(8)]
        assert first == second
        assert first.count(True) == 2

    def test_each_action_has_its_own_independent_counter(self, monkeypatch):
        import app.instance_config as ic

        monkeypatch.setattr(
            ic,
            "get_value",
            lambda *keys, default=None: {"noisy.action": 0.0} if keys == ("audit", "sampling") else default,
        )
        assert should_sample("noisy.action") is False
        assert should_sample("noisy.action") is False
        assert should_sample("quiet.action") is True
        assert should_sample("quiet.action") is True

    def test_non_numeric_ratio_falls_back_to_always_sampled(self, monkeypatch):
        import app.instance_config as ic

        monkeypatch.setattr(
            ic,
            "get_value",
            lambda *keys, default=None: {"some.action": "garbage"} if keys == ("audit", "sampling") else default,
        )
        assert should_sample("some.action") is True


# ---------------------------------------------------------------------------
# scripts/audit_volume_estimate.py — pure aggregation (build_report)
# ---------------------------------------------------------------------------


class TestBuildReport:
    def test_rows_per_day_and_action_percentages(self):
        report = ave.build_report(
            window_days=7,
            events_total=700,
            top_actions=[
                {"value": "chat.tool_call", "count": 400},
                {"value": "catalog.list", "count": 100},
            ],
            retention_days=365,
        )
        assert report["rows_per_day"] == 100.0
        assert report["top_actions"][0]["action"] == "chat.tool_call"
        assert report["top_actions"][0]["count"] == 400
        assert report["top_actions"][0]["pct"] == pytest.approx(400 / 700, rel=1e-3)
        assert report["projected_rows_at_retention"] == round(100.0 * 365)

    def test_dominant_action_flagged_above_25pct(self):
        report = ave.build_report(
            window_days=7,
            events_total=700,
            top_actions=[{"value": "chat.tool_call", "count": 400}, {"value": "catalog.list", "count": 100}],
            retention_days=365,
        )
        assert report["dominant_action"] == {"action": "chat.tool_call", "pct": pytest.approx(400 / 700, rel=1e-3)}

    def test_no_dominant_action_when_everything_is_under_threshold(self):
        report = ave.build_report(
            window_days=7,
            events_total=1000,
            top_actions=[{"value": f"a{i}", "count": 50} for i in range(4)],
            retention_days=365,
        )
        assert report["dominant_action"] is None

    def test_dominant_action_check_ignores_the_limit_truncation(self):
        """A single action >25% of the FULL window must be flagged even
        when --limit truncated the reported list to just itself."""
        report = ave.build_report(
            window_days=7,
            events_total=1000,
            top_actions=[{"value": "chat.tool_call", "count": 900}],
            retention_days=365,
        )
        assert report["dominant_action"]["action"] == "chat.tool_call"

    def test_retention_zero_means_unbounded_projection(self):
        report = ave.build_report(window_days=7, events_total=700, top_actions=[], retention_days=0)
        assert report["projected_rows_at_retention"] is None

    def test_empty_window_has_no_division_error(self):
        report = ave.build_report(window_days=7, events_total=0, top_actions=[], retention_days=365)
        assert report["rows_per_day"] == 0.0
        assert report["top_actions"] == []
        assert report["dominant_action"] is None
        assert report["projected_rows_at_retention"] == 0

    def test_avg_row_bytes_projects_a_retention_size(self):
        report = ave.build_report(
            window_days=7,
            events_total=700,
            top_actions=[],
            retention_days=365,
            avg_row_bytes=200.0,
        )
        assert report["avg_row_bytes_sampled"] == 200.0
        assert report["projected_bytes_at_retention"] == round(200.0 * report["projected_rows_at_retention"])


# ---------------------------------------------------------------------------
# scripts/audit_volume_estimate.py — end to end against a seeded audit_log
# ---------------------------------------------------------------------------


def test_main_json_reports_seeded_volume(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.repositories import audit_repo

    repo = audit_repo()
    for _ in range(30):
        repo.log(user_id="u1", action="chat.tool_call", result="success")
    for _ in range(10):
        repo.log(user_id="u1", action="catalog.list", result="success")

    rc = ave.main(["--days", "1", "--json"])
    assert rc == 0

    out = json.loads(capsys.readouterr().out)
    assert out["events_total"] == 40
    assert out["backend"] == "duckdb"
    assert out["top_actions"][0]["action"] == "chat.tool_call"
    assert out["top_actions"][0]["count"] == 30
    assert out["dominant_action"]["action"] == "chat.tool_call"
    # since = ISO timestamp string, machine-readable
    datetime.fromisoformat(out["since"])


def test_main_table_output_labels_backend_and_warns_on_empty_window(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    rc = ave.main(["--days", "1"])
    assert rc == 0
    out = capsys.readouterr()
    assert "backend=duckdb" in out.out
    assert "No audit_log rows found" in out.err


def test_main_respects_limit(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.repositories import audit_repo

    repo = audit_repo()
    for i in range(5):
        for _ in range(i + 1):
            repo.log(user_id="u1", action=f"action.{i}", result="success")

    rc = ave.main(["--days", "1", "--limit", "2", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["top_actions"]) == 2
    # Percentages/dominant-action logic still uses the FULL total, not the
    # truncated list's sum.
    assert out["events_total"] == 15


def test_main_uses_configured_retention_days(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import app.instance_config as ic

    monkeypatch.setattr(
        ic,
        "get_value",
        lambda *keys, default=None: 30 if keys == ("audit", "retention_days") else default,
    )
    from src.repositories import audit_repo

    audit_repo().log(user_id="u1", action="catalog.list", result="success")

    rc = ave.main(["--days", "1", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["retention_days"] == 30
