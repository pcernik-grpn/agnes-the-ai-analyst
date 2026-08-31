"""Root conftest — test-lane selection.

Why this file exists
--------------------
The pre-push gate used to be the whole suite: ~24 000 tests, 12 CI jobs, and
locally somewhere between 20 and 60 minutes depending on how contended the
machine was. CI runs that same suite again on the push, so a review cycle that
touched three files paid for the full suite three or four times over. The
dominant cost was never the assertions — it is that a test which creates a
``system.duckdb`` pays ~150 ms building the schema before its first line runs.

``--lane fast`` is the cheap gate that replaces it locally, and
``--lane impacted`` is its targeted sibling. It keeps every test
whose *recorded* runtime (setup + call + teardown, from ``.test_durations``) is
below ``--lane-budget-ms``, which sits under that DuckDB-creation floor — so
what survives is, by construction, the half of the suite that never builds a
database: parsers, validators, redirect guards, registry ratchets, schema
projections. Half the tests, ~2% of the wall-clock.

Two deliberate choices:

* **A test with no recorded duration is KEPT.** A test you just wrote has no
  entry in ``.test_durations``, and it is the one test you most want the gate
  to run. The lane therefore fails *open*, never silently skipping new work.
* **The budget is a recorded-time threshold, not a wall-clock one.**
  ``.test_durations`` is regenerated on CI hardware under ``-n auto``
  (``.github/workflows/update-test-durations.yml``), so its absolute numbers
  drift with runner contention. ``tests/test_lane_selection.py`` pins the
  lane's total recorded cost against a budget, so a regeneration that shifts
  the scale fails loudly instead of quietly turning the fast lane into the
  slow one.

``--lane impacted`` answers the other half: of the tests that exist, which ones
could *this diff* have broken? The selection lives in
``scripts/dev/impacted_tests.py`` (runnable on its own with ``--json`` when you
want to see what it picked and why). When the diff is too broad to target
honestly — a merge magnet like ``src/db.py``, or a match set covering a quarter
of the suite's runtime — it does not quietly expand to 24 000 tests: it says so
and falls back to the fast lane, which is the correct answer for a broad diff.

The full suite still runs — in CI, on every push, where it is parallel and free
of the developer's attention. See ``CONTRIBUTING.md`` → *Verification loop*.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

DURATIONS_PATH = Path(__file__).parent / ".test_durations"

# Default threshold in milliseconds of RECORDED duration. 250 ms sits just
# above the ~150 ms a fresh `get_system_db()` spends building the schema, so
# the lane splits almost exactly along "does this test create a database".
DEFAULT_LANE_BUDGET_MS = 250.0


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("lane", "test lane selection")
    group.addoption(
        "--lane",
        action="store",
        default="full",
        choices=("full", "fast", "impacted"),
        help=(
            "full (default) = every collected test. "
            "fast = only tests whose recorded duration is under --lane-budget-ms, "
            "plus every test with no recorded duration (i.e. newly written ones). "
            "impacted = only test files this branch's diff plausibly touches, "
            "falling back to the fast lane when the diff is too broad to target."
        ),
    )
    group.addoption(
        "--lane-base",
        action="store",
        default="origin/main",
        help="Base ref --lane impacted diffs against (default: origin/main).",
    )
    group.addoption(
        "--lane-budget-ms",
        action="store",
        type=float,
        default=DEFAULT_LANE_BUDGET_MS,
        help=(f"Recorded-duration ceiling for --lane fast, in milliseconds (default {DEFAULT_LANE_BUDGET_MS:g})."),
    )


def load_recorded_durations(path: Path = DURATIONS_PATH) -> dict[str, float]:
    """Recorded per-test durations, or ``{}`` when the file is absent/unreadable.

    An empty mapping makes every test "unknown", which the fast lane keeps —
    a missing durations file degrades to the full suite rather than to a gate
    that silently asserts nothing.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: float(v) for k, v in data.items() if isinstance(v, (int, float))}


def partition_fast_lane(
    nodeids: list[str],
    durations: dict[str, float],
    budget_ms: float,
) -> tuple[list[str], list[str]]:
    """Split ``nodeids`` into ``(kept, deselected)`` for the fast lane.

    Kept = recorded under the budget, OR not recorded at all (new test).
    """
    ceiling = budget_ms / 1000.0
    kept: list[str] = []
    dropped: list[str] = []
    for nodeid in nodeids:
        recorded = durations.get(nodeid)
        if recorded is None or recorded < ceiling:
            kept.append(nodeid)
        else:
            dropped.append(nodeid)
    return kept, dropped


def _impacted_files(config: pytest.Config) -> tuple[list[str] | None, str]:
    """``(test files, reason)`` for --lane impacted; ``None`` = too broad."""
    from scripts.dev.impacted_tests import select

    try:
        paths, reason = select(Path(__file__).parent, config.getoption("--lane-base"))
    except Exception as exc:  # a broken selector must not silently narrow the run
        return None, f"selector failed ({exc}) — falling back to the fast lane"
    # An EMPTY selection collapses to the same `None` as "too broad", and that
    # is deliberate: running zero tests is a green run that asserted nothing,
    # which is the one outcome a gate must never produce. The reason string
    # still distinguishes the two cases for whoever reads the summary line.
    return (paths or None), reason


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    lane = config.getoption("--lane")
    if lane == "full":
        return

    notes: list[str] = []
    # Fail open: an unrecognised lane keeps everything rather than deselecting
    # everything. The `choices=` above makes that unreachable today; it stops a
    # lane added later from silently emptying the run before anyone notices.
    keep: set[str] = {item.nodeid for item in items}

    if lane == "impacted":
        files, reason = _impacted_files(config)
        notes.append(f"impacted: {reason}")
        if files is not None:
            wanted = set(files)
            keep = {item.nodeid for item in items if item.nodeid.split("::")[0] in wanted}
            lane = "impacted"
        else:
            notes.append("degraded to lane=fast")
            lane = "fast"

    if lane == "fast":
        budget_ms = config.getoption("--lane-budget-ms")
        durations = load_recorded_durations()
        kept_ids, _ = partition_fast_lane([item.nodeid for item in items], durations, budget_ms)
        keep = set(kept_ids)
        unknown = sum(1 for nodeid in kept_ids if nodeid not in durations)
        notes.append(
            f"fast: budget={budget_ms:g}ms, {unknown} of them with no recorded duration"
            + ("  [.test_durations missing — nothing could be deselected]" if not durations else "")
        )

    selected = [item for item in items if item.nodeid in keep]
    deselected = [item for item in items if item.nodeid not in keep]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected

    config.stash[_LANE_REPORT] = f"lane: kept {len(selected)}, deselected {len(deselected)} — " + "; ".join(notes)


_LANE_REPORT: pytest.StashKey[str] = pytest.StashKey()


def pytest_report_header(config: pytest.Config) -> str | None:
    """Say which lane ran, so a green run can never be mistaken for a full one."""
    lane = config.getoption("--lane")
    if lane == "full":
        return None
    return f"test lane: {lane} (the full suite runs in CI on push)"


def pytest_terminal_summary(terminalreporter) -> None:  # type: ignore[no-untyped-def]
    report = terminalreporter.config.stash.get(_LANE_REPORT, None)
    if report:
        terminalreporter.write_line(report)
