"""`services.corporate_memory.contradiction.find_candidates` must not stay
silent when the `LIMIT`-bound query it wraps (``KnowledgeRepository
.find_contradiction_candidates``, driven by ``DEFAULT_CANDIDATE_LIMIT``)
truncates a domain's corpus (#63).

A contradiction *scanner* that never loaded the older approved items past
the cap can report "no contradictions" as a false all-clear rather than a
real one. This pins the WARNING that fires exactly at the truncation
boundary (returned count == limit), and that it stays silent under it.

Domain sharding / the Batch API are explicitly out of scope for this fix —
see the `Consider domain sharding (V2 TODO)` comment left in place in
``services/corporate_memory/contradiction.py``.
"""

import logging
from unittest.mock import MagicMock

import pytest

from services.corporate_memory.contradiction import find_candidates


def _repo_returning(n: int) -> MagicMock:
    repo = MagicMock()
    repo.find_contradiction_candidates.return_value = [
        {"id": f"c{i}", "title": f"t{i}", "content": f"x{i}"} for i in range(n)
    ]
    return repo


def test_candidate_count_at_the_cap_logs_a_warning_naming_domain_and_cap(caplog):
    repo = _repo_returning(3)
    new_item = {"id": "new", "domain": "finance"}

    with caplog.at_level(logging.WARNING, logger="services.corporate_memory.contradiction"):
        candidates = find_candidates(repo, new_item, max_candidates=3)

    assert len(candidates) == 3
    warnings = [
        r for r in caplog.records if r.levelname == "WARNING" and "finance" in r.getMessage() and "3" in r.getMessage()
    ]
    assert warnings, (
        "expected a WARNING naming the domain and the cap when the candidate "
        f"count equals the limit; got: {[r.getMessage() for r in caplog.records]}"
    )


@pytest.mark.parametrize("n", [0, 1, 2])
def test_candidate_count_under_the_cap_does_not_warn(caplog, n):
    repo = _repo_returning(n)
    new_item = {"id": "new", "domain": "finance"}

    with caplog.at_level(logging.WARNING, logger="services.corporate_memory.contradiction"):
        candidates = find_candidates(repo, new_item, max_candidates=3)

    assert len(candidates) == n
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_domainless_item_still_names_the_cap(caplog):
    """A new item with no domain still scans (all-domain candidates); the
    warning must still fire and name the cap even with no domain to name."""
    repo = _repo_returning(5)
    new_item = {"id": "new"}

    with caplog.at_level(logging.WARNING, logger="services.corporate_memory.contradiction"):
        find_candidates(repo, new_item, max_candidates=5)

    assert [r for r in caplog.records if r.levelname == "WARNING" and "5" in r.getMessage()]


def test_the_judgment_call_says_which_workload_it_belongs_to():
    """One batched judgment per new item is a real per-item cost — it is
    labelled so a contradiction sweep is visible next to the extraction
    that fed it."""
    from services.corporate_memory.contradiction import find_and_judge
    from src.observability.llm_context import current_llm_context

    seen = {}

    class _RecordingExtractor:
        def extract_json(self, **_kwargs):
            seen["context"] = current_llm_context()
            return {"judgments": []}

    repo = _repo_returning(1)
    find_and_judge(_RecordingExtractor(), {"id": "new", "domain": "finance"}, repo, max_candidates=5)

    ctx = seen["context"]
    assert (ctx.workload, ctx.purpose) == ("corporate_memory", "contradiction")
