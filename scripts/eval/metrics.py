"""EQ3/EQ9 -- fact-graph extraction quality metrics, tracked per release
(design spec Sec 15.3):

  EQ3  precision/recall per fact type against a planted ground-truth
       manifest (`tests/fixtures/eval/ground_truth.schema.json`).
  EQ9  entity-resolution cluster purity, conflict rate per 1000 documents,
       and orphan rate per run.

Every computation here is a PURE function over already-fetched API
responses / a loaded ground-truth manifest -- no network calls -- so the
metrics math is unit-testable on a tiny synthetic manifest without a live
server (see `tests/test_eval_harness.py`). The HTTP orchestration
(`fetch_and_score_release`) is the only piece that talks to a live Agnes
instance, against the response shapes documented in the design spec Sec 12:

    search    -> {"subjects": [{"id","type","aliases":[..],
                   "attrs": {...}, "claim_count","quote_count"}], "limit_applied"}
    neighbors -> {"nodes": [...], "edges": [{"id","src","dst","type","attrs":{...}}],
                   "truncated": {...}}

The `/api/facts/*` endpoints themselves land in a parallel build-order
step (Sec 16 step 6) -- `probe_facts_endpoint` lets a caller (or a test's
`pytest.mark.skipif`) check whether they exist on a given server before
calling `fetch_and_score_release` against it.

Definitions used below, spelled out because the design spec names these
metrics without defining the exact match rule:

  - "fact type" match: an actual subject counts as matching a planted fact
    when the subject's `type` equals the planted fact's `type` AND at
    least one of the subject's returned `aliases` equals the planted
    fact's `natural_key` or one of its `aliases` (the resolution
    challenge -- see the ground-truth schema doc).
  - "entity-resolution cluster purity": classic purity -- for each planted
    fact's expected cluster (its `natural_key` plus `aliases`), the
    largest single actual subject's overlap with that cluster, summed
    over all planted clusters and divided by the total planted alias
    count. 1.0 = every planted variant landed in exactly the subject it
    should have; a merge that fuses two distinct planted entities, or a
    split that fragments one planted entity across multiple subjects,
    both reduce it.
  - "conflict rate per 1000 documents": count of attribute keys the API
    returned as `{"conflicted": true, ...}` (design spec Sec 12/4) across
    all fetched subjects, normalized per 1000 documents in the manifest's
    `document_count`.
  - "orphan rate": fraction of returned subjects with zero edges (neither
    src nor dst of any edge) -- a disconnected graph node. Not to be
    confused with a zero-claim subject, which is structurally impossible
    by design (Sec 15.6: zero-claim subjects are garbage-collected).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

_REPO_ROOT = Path(__file__).resolve().parents[2]
GROUND_TRUTH_SCHEMA_PATH = _REPO_ROOT / "tests" / "fixtures" / "eval" / "ground_truth.schema.json"


# ---------------------------------------------------------------------------
# Ground-truth manifest loading
# ---------------------------------------------------------------------------


def load_ground_truth(path: Path) -> dict[str, Any]:
    """Load and lightly validate a ground-truth manifest against
    `ground_truth.schema.json` (jsonschema, imported lazily -- not a
    runtime dependency of the rest of the harness)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    import jsonschema

    schema = json.loads(GROUND_TRUTH_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(instance=data, schema=schema)
    return data


def _fact_cluster(fact: dict[str, Any]) -> set[str]:
    return {fact["natural_key"], *fact.get("aliases", [])}


# ---------------------------------------------------------------------------
# EQ3 -- precision / recall per fact type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrecisionRecall:
    fact_type: str
    true_positives: int
    false_positives: int
    false_negatives: int

    @property
    def precision(self) -> float | None:
        denom = self.true_positives + self.false_positives
        return (self.true_positives / denom) if denom else None

    @property
    def recall(self) -> float | None:
        denom = self.true_positives + self.false_negatives
        return (self.true_positives / denom) if denom else None


def precision_recall_by_type(
    ground_truth_facts: list[dict[str, Any]], actual_subjects: list[dict[str, Any]]
) -> dict[str, PrecisionRecall]:
    """`actual_subjects` is the concatenated `subjects` list from one or
    more `POST /api/facts/search` calls (one per fact type present in the
    manifest, per the design spec's documented `{type, filters, limit}`
    request shape)."""
    by_type_expected: dict[str, list[dict[str, Any]]] = {}
    for fact in ground_truth_facts:
        by_type_expected.setdefault(fact["type"], []).append(fact)

    by_type_actual: dict[str, list[dict[str, Any]]] = {}
    for subject in actual_subjects:
        by_type_actual.setdefault(subject["type"], []).append(subject)

    results: dict[str, PrecisionRecall] = {}
    for fact_type in sorted(set(by_type_expected) | set(by_type_actual)):
        expected = by_type_expected.get(fact_type, [])
        actual = by_type_actual.get(fact_type, [])
        expected_clusters = [_fact_cluster(f) for f in expected]

        matched_expected = [False] * len(expected_clusters)
        true_positives = 0
        false_positives = 0
        for subject in actual:
            subject_aliases = set(subject.get("aliases") or [])
            hit_index = next(
                (i for i, cluster in enumerate(expected_clusters) if subject_aliases & cluster),
                None,
            )
            if hit_index is None:
                false_positives += 1
            else:
                true_positives += 1
                matched_expected[hit_index] = True
        false_negatives = sum(1 for matched in matched_expected if not matched)
        results[fact_type] = PrecisionRecall(
            fact_type=fact_type,
            true_positives=true_positives,
            false_positives=false_positives,
            false_negatives=false_negatives,
        )
    return results


# ---------------------------------------------------------------------------
# EQ9 -- entity-resolution cluster purity, conflict rate, orphan rate
# ---------------------------------------------------------------------------


def cluster_purity(ground_truth_facts: list[dict[str, Any]], actual_subjects: list[dict[str, Any]]) -> float | None:
    total_planted = 0
    total_best_overlap = 0
    for fact in ground_truth_facts:
        expected_cluster = _fact_cluster(fact)
        if not expected_cluster:
            continue
        total_planted += len(expected_cluster)
        best_overlap = 0
        for subject in actual_subjects:
            subject_aliases = set(subject.get("aliases") or [])
            overlap = len(subject_aliases & expected_cluster)
            best_overlap = max(best_overlap, overlap)
        total_best_overlap += best_overlap
    if total_planted == 0:
        return None
    return total_best_overlap / total_planted


def conflict_rate_per_1000_docs(actual_subjects: list[dict[str, Any]], document_count: int) -> float | None:
    if document_count <= 0:
        return None
    conflicted = 0
    for subject in actual_subjects:
        for value in (subject.get("attrs") or {}).values():
            if isinstance(value, dict) and value.get("conflicted"):
                conflicted += 1
    return conflicted / document_count * 1000


def orphan_rate(subject_ids: list[str], edge_counts: dict[str, int]) -> float | None:
    """`edge_counts` maps subject id -> number of edges touching it
    (from `POST /api/facts/neighbors {subject_id, depth: 1}` -- count of
    `len(response["edges"])`). A subject id missing from `edge_counts` is
    treated as zero edges (orphan) -- a caller that fetched neighbors for
    every subject should never actually hit this, but a partial fetch
    (e.g. truncated by the API's own caps) must not silently undercount
    orphans as a result."""
    if not subject_ids:
        return None
    orphans = sum(1 for sid in subject_ids if edge_counts.get(sid, 0) == 0)
    return orphans / len(subject_ids)


# ---------------------------------------------------------------------------
# Live orchestration -- talks to a real Agnes instance
# ---------------------------------------------------------------------------


def probe_facts_endpoint(base_url: str, token: str, *, timeout: float = 5.0) -> bool:
    """True iff `POST /api/facts/search` exists on `base_url` (any status
    other than 404/connection failure counts -- a 401/422 still proves the
    route is registered). Never raises -- a caller uses this to decide
    whether to run the live integration test at all."""
    try:
        with httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout) as client:
            resp = client.post(
                "/api/facts/search",
                json={"type": "__eq3_probe__", "limit": 1},
                headers={"Authorization": f"Bearer {token}"} if token else {},
            )
        return resp.status_code != 404
    except httpx.HTTPError:
        return False


def fetch_and_score_release(
    base_url: str,
    token: str,
    ground_truth: dict[str, Any],
    *,
    release_id: str,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Fetch every fact type present in `ground_truth` from a live Agnes
    instance and compute EQ3+EQ9. Returns the exact dict `write_metrics_
    record` persists -- callers that want the raw API responses too should
    fetch separately and call the pure functions above directly."""
    facts = ground_truth.get("facts", [])
    fact_types = sorted({f["type"] for f in facts})
    document_count = ground_truth["document_count"]

    actual_subjects: list[dict[str, Any]] = []
    with httpx.Client(
        base_url=base_url.rstrip("/"), timeout=timeout, headers={"Authorization": f"Bearer {token}"}
    ) as client:
        for fact_type in fact_types:
            resp = client.post("/api/facts/search", json={"type": fact_type, "limit": 100})
            resp.raise_for_status()
            actual_subjects.extend(resp.json().get("subjects", []))

        edge_counts: dict[str, int] = {}
        for subject in actual_subjects:
            resp = client.post("/api/facts/neighbors", json={"subject_id": subject["id"], "depth": 1})
            resp.raise_for_status()
            edge_counts[subject["id"]] = len(resp.json().get("edges", []))

    pr_by_type = precision_recall_by_type(facts, actual_subjects)
    return {
        "release_id": release_id,
        "corpus_id": ground_truth["corpus_id"],
        "document_count": document_count,
        "subjects_fetched": len(actual_subjects),
        "precision_recall_by_type": {
            t: {
                "precision": r.precision,
                "recall": r.recall,
                "tp": r.true_positives,
                "fp": r.false_positives,
                "fn": r.false_negatives,
            }
            for t, r in pr_by_type.items()
        },
        "cluster_purity": cluster_purity(facts, actual_subjects),
        "conflict_rate_per_1000_docs": conflict_rate_per_1000_docs(actual_subjects, document_count),
        "orphan_rate": orphan_rate([s["id"] for s in actual_subjects], edge_counts),
    }


def write_metrics_record(runs_dir: Path, release_id: str, record: dict[str, Any], *, ts: str) -> Path:
    """One JSON file per release under `runs/metrics/` (design spec Sec
    15.3 EQ9: "numbers, recorded per release, not one-off assertions") --
    never overwritten by a later release, so the directory accumulates a
    trend line."""
    out_dir = Path(runs_dir) / "metrics"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{release_id}_{ts}.json"
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
