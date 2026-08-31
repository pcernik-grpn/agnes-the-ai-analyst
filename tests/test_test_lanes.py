"""Guards for the local test lanes (`pytest --lane fast|impacted`).

The lanes exist so the pre-push gate is a minute instead of half an hour, with
the full suite running in CI on the push. Two properties have to hold or the
gate is worse than useless:

1. **It must not silently shrink.** A test with no recorded duration — i.e. the
   one you just wrote — has to survive the fast lane.
2. **It must actually be fast.** The lane is derived from `.test_durations`,
   which is regenerated on CI hardware (`.github/workflows/update-test-durations.yml`);
   a regeneration that shifts the absolute scale would quietly turn the fast
   lane into the slow one. The budget assertion below is the ratchet that makes
   that fail loudly instead.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from conftest import (
    DEFAULT_LANE_BUDGET_MS,
    load_recorded_durations,
    partition_fast_lane,
)
from scripts.dev.impacted_tests import (
    MAX_FILES_PER_TOKEN,
    MERGE_MAGNETS,
    is_test_path,
    search_tokens,
    select_for_paths,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# The fast lane's whole promise. Recorded seconds, not wall clock: the suite
# runs under `-n auto`, so this is the serial cost the lane is allowed to have.
# ~220 s today across ~11 900 tests; the headroom is for growth, and blowing
# past it means the threshold needs retuning, not raising.
FAST_LANE_RECORDED_BUDGET_SECONDS = 400.0

# The lane is worth having only while it covers a real share of the suite. If a
# durations regeneration shifted the scale the other way, the lane would go
# green having run almost nothing.
FAST_LANE_MIN_SHARE = 0.30


class TestFastLanePartition:
    def test_a_test_with_no_recorded_duration_is_kept(self):
        """The test you just wrote has no entry — and is the one you most need."""
        kept, dropped = partition_fast_lane(["tests/test_new.py::test_just_written"], durations={}, budget_ms=250)
        assert kept == ["tests/test_new.py::test_just_written"]
        assert dropped == []

    def test_a_slow_recorded_test_is_deselected(self):
        kept, dropped = partition_fast_lane(
            ["tests/test_slow.py::test_builds_a_database"],
            durations={"tests/test_slow.py::test_builds_a_database": 0.9},
            budget_ms=250,
        )
        assert kept == []
        assert dropped == ["tests/test_slow.py::test_builds_a_database"]

    def test_the_budget_is_the_boundary(self):
        durations = {"a::t": 0.249, "b::t": 0.250, "c::t": 0.251}
        kept, _ = partition_fast_lane(list(durations), durations, budget_ms=250)
        assert kept == ["a::t"]

    def test_a_missing_durations_file_keeps_everything(self, tmp_path):
        """A gate that cannot read its input must run more, never less."""
        assert load_recorded_durations(tmp_path / "absent.json") == {}
        kept, dropped = partition_fast_lane(["a::t", "b::t"], {}, budget_ms=250)
        assert kept == ["a::t", "b::t"] and dropped == []

    def test_a_malformed_durations_file_keeps_everything(self, tmp_path):
        broken = tmp_path / ".test_durations"
        broken.write_text("{not json")
        assert load_recorded_durations(broken) == {}

    def test_a_durations_file_that_is_not_a_mapping_keeps_everything(self, tmp_path):
        listy = tmp_path / ".test_durations"
        listy.write_text(json.dumps(["a::t"]))
        assert load_recorded_durations(listy) == {}


class TestFastLaneStaysFast:
    """Ratchets against the committed `.test_durations`."""

    @pytest.fixture(scope="class")
    def durations(self) -> dict[str, float]:
        recorded = load_recorded_durations(REPO_ROOT / ".test_durations")
        if not recorded:
            pytest.skip(".test_durations not present in this checkout")
        return recorded

    def test_the_lane_fits_its_recorded_budget(self, durations):
        kept, _ = partition_fast_lane(list(durations), durations, DEFAULT_LANE_BUDGET_MS)
        cost = sum(durations[nodeid] for nodeid in kept)
        assert cost < FAST_LANE_RECORDED_BUDGET_SECONDS, (
            f"the fast lane now costs {cost:.0f}s of recorded time "
            f"(budget {FAST_LANE_RECORDED_BUDGET_SECONDS:.0f}s). Either "
            "`.test_durations` was regenerated on a slower scale — retune "
            "DEFAULT_LANE_BUDGET_MS in conftest.py — or a batch of slow tests "
            "landed under the threshold. Do not just raise this number: the "
            "lane is only a pre-push gate while it stays about a minute."
        )

    def test_the_lane_still_covers_a_meaningful_share_of_the_suite(self, durations):
        kept, _ = partition_fast_lane(list(durations), durations, DEFAULT_LANE_BUDGET_MS)
        share = len(kept) / len(durations)
        assert share > FAST_LANE_MIN_SHARE, (
            f"the fast lane now selects only {share:.0%} of recorded tests. A gate "
            "that runs almost nothing is worse than no gate — it reads as green."
        )


class TestImpactedSelection:
    def test_a_changed_test_file_selects_itself(self):
        paths, _ = select_for_paths(REPO_ROOT, ["tests/test_test_lanes.py"])
        assert "tests/test_test_lanes.py" in paths

    def test_a_changed_test_file_that_no_longer_exists_is_dropped(self):
        """A deleted test file is in the diff but must not reach pytest."""
        paths, _ = select_for_paths(REPO_ROOT, ["tests/test_deleted_long_ago.py"])
        assert paths == []

    def test_a_merge_magnet_refuses_to_guess(self):
        for magnet in ("src/db.py", "tests/conftest.py", "app/main.py"):
            assert magnet in MERGE_MAGNETS
            paths, reason = select_for_paths(REPO_ROOT, [magnet])
            assert paths == [], f"{magnet} should not produce a name-based selection"
            assert "merge magnet" in reason

    def test_no_changed_files_selects_nothing(self):
        paths, reason = select_for_paths(REPO_ROOT, [])
        assert paths == []
        assert "no changed files" in reason

    def test_a_source_module_finds_the_tests_that_name_it(self):
        paths, _ = select_for_paths(REPO_ROOT, ["scripts/dev/impacted_tests.py"])
        assert "tests/test_test_lanes.py" in paths

    def test_tokens_cover_both_the_dotted_module_and_the_file_path(self):
        tokens = search_tokens("src/remote_engines.py")
        assert "src/remote_engines.py" in tokens
        assert "src.remote_engines" in tokens
        assert "src import remote_engines" in tokens

    def test_a_non_python_file_is_searched_by_its_basename(self):
        tokens = search_tokens("app/web/templates/base_ds.html")
        assert "base_ds.html" in tokens

    def test_test_path_recognition(self):
        assert is_test_path("tests/test_foo.py")
        assert is_test_path("connectors/jira/tests/test_bar.py")
        assert is_test_path("tests/foo_test.py")
        assert not is_test_path("tests/conftest.py")
        assert not is_test_path("src/db.py")


class TestImpactedSelectionIsNotVacuous:
    """The selector has to be usable end to end, not just importable."""

    def test_the_script_runs_and_reports_a_reason(self):
        result = subprocess.run(
            ["python3", "scripts/dev/impacted_tests.py", "--json", "--base", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode in (0, 1), result.stderr
        payload = json.loads(result.stdout)
        assert set(payload) == {"paths", "reason"}
        assert payload["reason"]

    def test_a_broad_match_set_degrades_instead_of_widening(self):
        """`MAX_FILES_PER_TOKEN` keeps one generic path from dragging in the suite."""
        assert MAX_FILES_PER_TOKEN > 0
        paths, reason = select_for_paths(REPO_ROOT, ["CHANGELOG.md"])
        # CHANGELOG.md is named by many tests and identifies none of them; the
        # selection must stay small or say it cannot target.
        assert paths == [] or len(paths) <= MAX_FILES_PER_TOKEN, reason
