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
    `counts.documents`.
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


def expected_facts_from_manifest(ground_truth: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive the EQ3/EQ9 "planted fact" list (`{natural_key, type, aliases,
    source_doc_ids}` -- what `precision_recall_by_type`/`cluster_purity`
    expect) from the manifest's `nodes[]` (producer wire rows -- one row per
    evidence-bearing claim, design spec Sec. 7.0). The same planted fact can
    appear as multiple rows sharing one `id` (e.g. revisited by a later
    document, or the AN2 Czech-inflection pair); dedupe by `id` so it counts
    once. `natural_key` is the node id itself -- this manifest shape has no
    separate alias field, unlike the older ground-truth shape this replaces.
    An entity-resolution challenge is instead expressed as two DISTINCT ids
    (e.g. `client:corundum-foods` vs. `client:corundum-foods-inc`, the
    `entity_resolution` trap) -- the extraction is expected to resolve them,
    not the ground truth to alias them."""
    by_id: dict[str, dict[str, Any]] = {}
    for node in ground_truth.get("nodes", []):
        entry = by_id.setdefault(
            node["id"],
            {"natural_key": node["id"], "type": node["type"], "aliases": [], "source_doc_ids": []},
        )
        for ev in node.get("evidence", []):
            doc_id = ev.get("doc_id")
            if doc_id and doc_id not in entry["source_doc_ids"]:
                entry["source_doc_ids"].append(doc_id)
    return list(by_id.values())


def expected_edges_from_manifest(ground_truth: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive the expected-edge list (`{src_natural_key, type, dst_natural_key,
    source_doc_ids}`) from the manifest's `edges[]` (producer wire rows).
    Dedupe by (`src`, `type`, `dst`) -- the same merge key the ingest
    endpoint uses (design spec Sec. 7.0) -- so a planted edge revisited by
    more than one document still counts once."""
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for edge in ground_truth.get("edges", []):
        key = (edge["src"], edge["type"], edge["dst"])
        entry = by_key.setdefault(
            key,
            {
                "src_natural_key": edge["src"],
                "type": edge["type"],
                "dst_natural_key": edge["dst"],
                "source_doc_ids": [],
            },
        )
        for ev in edge.get("evidence", []):
            doc_id = ev.get("doc_id")
            if doc_id and doc_id not in entry["source_doc_ids"]:
                entry["source_doc_ids"].append(doc_id)
    return list(by_key.values())


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

        # One planted fact can be matched at most ONCE. A second subject
        # overlapping an already-matched cluster is not a second hit — it is
        # the entity-resolution failure EQ9 measures separately, and for EQ3
        # it is a spurious extra subject, i.e. a false positive. Counting it
        # as a true positive let TP exceed the planted count, overstating
        # precision (and letting recall exceed 1) on exactly the runs where
        # the system duplicated entities.
        matched_expected = [False] * len(expected_clusters)
        true_positives = 0
        false_positives = 0
        for subject in actual:
            subject_aliases = set(subject.get("aliases") or [])
            hits = [i for i, cluster in enumerate(expected_clusters) if subject_aliases & cluster]
            # Prefer a cluster nobody has claimed yet, so greedy assignment
            # cannot invent a false positive purely from input order when a
            # subject overlaps both a taken and a free cluster.
            hit_index = next((i for i in hits if not matched_expected[i]), None)
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


# ---------------------------------------------------------------------------
# Alias-search ranking -- ordering-aware companion to precision/recall
# (facts real-name lookup follow-up, `POST /api/facts/search {q}`).
# ---------------------------------------------------------------------------


def alias_search_hit_rank(actual_subjects: list[dict[str, Any]], target_alias: str) -> int | None:
    """0-based rank of the first subject in an already `q=`-filtered
    `POST /api/facts/search` response whose `aliases` contains
    `target_alias`. `precision_recall_by_type`/`cluster_purity` above are
    SET metrics -- order-blind, matching the API's pre-`q` behavior -- this
    is the ordering-aware companion: it measures whether `q` actually RANKS
    the intended subject near the top of `actual_subjects`, not merely
    whether it appears anywhere. `None` when no subject in the response
    carries the alias at all (a miss, not rank 0)."""
    for rank, subject in enumerate(actual_subjects):
        if target_alias in (subject.get("aliases") or []):
            return rank
    return None


def top_k_alias_hit_rate(ranks: list[int | None], *, k: int = 1) -> float | None:
    """Fraction of `alias_search_hit_rank` results landing within the top
    `k` (0-based rank < `k`) -- a `None` rank (alias never found) always
    counts as a miss. `None` overall when `ranks` is empty (nothing to
    score), matching every other metric's "no data" convention here."""
    if not ranks:
        return None
    return sum(1 for r in ranks if r is not None and r < k) / len(ranks)


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
    corpus_id: str,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Fetch every fact type present in `ground_truth` from a live Agnes
    instance and compute EQ3+EQ9. Returns the exact dict `write_metrics_
    record` persists -- callers that want the raw API responses too should
    fetch separately and call the pure functions above directly.

    `corpus_id` is the Collection this manifest's corpus was ingested into
    (`claims.corpus_id` in the live schema) -- an ingest-time id the
    generator's manifest itself has no way to know, so the caller (who did
    the ingest) supplies it."""
    facts = expected_facts_from_manifest(ground_truth)
    fact_types = sorted({f["type"] for f in facts})
    document_count = ground_truth["counts"]["documents"]

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

        # Ordering-aware companion to precision/recall above: for each
        # planted fact, query `q=` with a name derived from its own natural
        # key (`<type>:<kebab-slug>` -> "kebab slug", the shape a human
        # would actually type) and record where its OWN subject lands in
        # that query's ranked results.
        alias_ranks: list[int | None] = []
        for fact in facts:
            query_text = _alias_query_text(fact["natural_key"])
            if not query_text:
                continue
            resp = client.post("/api/facts/search", json={"type": fact["type"], "q": query_text, "limit": 100})
            resp.raise_for_status()
            alias_ranks.append(alias_search_hit_rank(resp.json().get("subjects", []), fact["natural_key"]))

    pr_by_type = precision_recall_by_type(facts, actual_subjects)
    return {
        "release_id": release_id,
        "corpus_id": corpus_id,
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
        "alias_search_top1_hit_rate": top_k_alias_hit_rate(alias_ranks, k=1),
    }


def _alias_query_text(natural_key: str) -> str:
    """`<type>:<kebab-slug>` -> `"kebab slug"` -- an approximation of the
    natural-language name a human would type for `q=`, symmetric with the
    repository's own normalization (casefold, spaces -> hyphens)."""
    slug = natural_key.split(":", 1)[-1]
    return slug.replace("-", " ").strip()


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
