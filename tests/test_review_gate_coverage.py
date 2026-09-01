"""Guard: every parquet-writing module the census finds is covered by the
`agnes-reviewer-architecture` gate (issue #1361).

Before this guard, the gate fired only on `connectors/*/extractor.py` /
`connectors/*/extract_init.py` (plus a brand-new file anywhere under
`connectors/`), so a change to an EXISTING module like
`connectors/jira/transform.py` or `connectors/keboola/incremental.py` —
both genuine parquet writers into the extract layout
`src/parquet_publish.py` protects — never triggered the architecture
reviewer at all. This test re-runs the same call-shape grep the #1361
census used across `src/` and `connectors/` (the two trees the gate claims
jurisdiction over — `app/`, `cli/`, and `scripts/` writers are a different
concern: an in-memory admin export, a client-side snapshot cache with its
own lock protocol, and dev/demo tooling, none of which is read by the
orchestrator's hasher/view-glob/`agnes pull`) and asserts every hit's path
matches one of the glob patterns actually wired into the gate — so a NEW
parquet writer that isn't added to the gate fails CI instead of shipping
ungated.

Design note — pinned list, not a markdown parser: the gate's own scope list
mixes machine-checkable globs (`connectors/*/extractor.py`) with a
git-diff-status predicate ("any NEW file under connectors/") that has no
meaning against a static path — a file that already exists is never "new"
again, yet must stay gated when it is MODIFIED (that predicate is exactly
the gap #1361 closed). A generic parser of the agent's bullet list or the
command's table cell would have to special-case that one line anyway, so
this test mirrors the path-matching subset of the gate as a plain Python
list (`GATE_GLOBS`) instead of parsing `.md` structure that is prose, not
data, and free to reformat. To keep the mirror honest without parsing,
`test_gate_patterns_are_present_in_both_gate_files` asserts every pattern
string below appears verbatim in both `.md` files, so editing the glob text
in one place without the other — in either direction — fails immediately.
"""

from __future__ import annotations

import fnmatch
import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
ARCHITECTURE_AGENT = REPO_ROOT / ".claude" / "agents" / "agnes-reviewer-architecture.md"
REVIEW_COMMAND = REPO_ROOT / ".claude" / "commands" / "agnes-review.md"

# Mirrors the path-matching globs in the architecture-gate's scope list
# (`agnes-reviewer-architecture.md`'s "## Scope check" bullets and the
# routing-table row in `agnes-review.md`). Widen this list AND both `.md`
# files together — `test_gate_patterns_are_present_in_both_gate_files`
# fails loudly if they drift apart.
GATE_GLOBS = [
    "src/orchestrator.py",
    "src/db.py",
    "src/parquet_publish.py",
    "src/ingest/tabular.py",
    "connectors/*/extractor.py",
    "connectors/*/extract_init.py",
    "connectors/*/*transform*.py",
    "connectors/*/*incremental*.py",
    "connectors/*/*parquet*.py",
    "connectors/*/*partition*.py",
    "connectors/jira/organizations.py",
    "connectors/keboola/storage_api.py",
]

# The same parquet-write call-shape grep the #1361 census used.
_PARQUET_WRITE_RE = re.compile(
    r"to_parquet|write_parquet|write_dataset|pq\.write_table|ParquetWriter|COPY[^;\n]*TO[^;\n]*parquet",
    re.IGNORECASE,
)

# The two trees the gate claims jurisdiction over — see the module
# docstring for why `app/`, `cli/`, and `scripts/` are out of scope.
_SEARCH_ROOTS = ("src", "connectors")

# Real grep hits that are NOT parquet writers into the served extract
# layout, so `src/parquet_publish.py`'s atomicity invariant does not apply
# and the architecture gate has no business firing on them:
_CENSUS_EXCLUDE = {
    # A denylist of SQL keywords a user query may not call (blocks a
    # `write_parquet(...)` table function from *inside* a query) — not a
    # writer itself.
    "src/remote_query.py": "SQL keyword denylist entry, not a writer",
    # Benchmark tooling: writes a throwaway hive-partitioned dataset under
    # a temp dir to measure bloom-filter lookup performance, never the
    # served extract layout.
    "connectors/jira/scripts/bloom_benchmark.py": "benchmark tooling, not a production writer",
}


def _iter_production_python_files() -> list[tuple[str, pathlib.Path]]:
    found = []
    for root_name in _SEARCH_ROOTS:
        for path in sorted((REPO_ROOT / root_name).rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            parts = rel.split("/")
            if "tests" in parts:
                continue
            fname = parts[-1]
            if fname.startswith("test_") or fname.endswith("_test.py") or fname == "conftest.py":
                continue
            found.append((rel, path))
    return found


def _census_hits() -> list[str]:
    hits = []
    for rel, path in _iter_production_python_files():
        if rel in _CENSUS_EXCLUDE:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if _PARQUET_WRITE_RE.search(text):
            hits.append(rel)
    return sorted(hits)


# Computed once at collection time so a broken grep (e.g. an over-eager
# exclude) shows up as an empty parametrize list, caught by
# `test_census_finds_the_known_writers` below rather than passing vacuously.
_HITS = _census_hits()


def test_census_finds_the_known_writers():
    # A floor, not the full list — proves the grep+roots still find the
    # #1361 writers instead of the parametrized test below silently
    # collecting zero cases if the regex or search roots ever regress.
    expected_floor = {
        "src/parquet_publish.py",
        "src/ingest/tabular.py",
        "connectors/jira/transform.py",
        "connectors/jira/incremental_transform.py",
        "connectors/jira/organizations.py",
        "connectors/keboola/incremental.py",
        "connectors/keboola/parquet_io.py",
        "connectors/keboola/partitioned.py",
        "connectors/keboola/storage_api.py",
    }
    missing = expected_floor - set(_HITS)
    assert not missing, f"census no longer finds known parquet writers: {sorted(missing)}"


@pytest.mark.parametrize("hit", _HITS)
def test_every_parquet_writer_is_covered_by_the_architecture_gate(hit):
    covered = any(fnmatch.fnmatch(hit, pattern) for pattern in GATE_GLOBS)
    assert covered, (
        f"{hit} writes parquet (matches the #1361 census grep) but no pattern in "
        f"GATE_GLOBS covers it. Add a pattern to the scope list in "
        f"{ARCHITECTURE_AGENT.relative_to(REPO_ROOT)} and the routing table in "
        f"{REVIEW_COMMAND.relative_to(REPO_ROOT)}, then mirror it into GATE_GLOBS "
        f"here so agnes-reviewer-architecture actually fires on it."
    )


def test_gate_patterns_are_present_in_both_gate_files():
    agent_text = ARCHITECTURE_AGENT.read_text(encoding="utf-8")
    command_text = REVIEW_COMMAND.read_text(encoding="utf-8")
    missing_agent = [p for p in GATE_GLOBS if p not in agent_text]
    missing_command = [p for p in GATE_GLOBS if p not in command_text]
    assert not missing_agent, f"{ARCHITECTURE_AGENT.relative_to(REPO_ROOT)} scope list is missing: {missing_agent}"
    assert not missing_command, f"{REVIEW_COMMAND.relative_to(REPO_ROOT)} routing table is missing: {missing_command}"


def test_census_excludes_are_still_real_files():
    # A stale exclude (the file was renamed/deleted) would silently narrow
    # the census without anyone noticing.
    missing = [p for p in _CENSUS_EXCLUDE if not (REPO_ROOT / p).is_file()]
    assert not missing, f"_CENSUS_EXCLUDE names files that no longer exist: {missing}"
