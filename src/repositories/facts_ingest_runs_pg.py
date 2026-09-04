"""Postgres-only repository for ``facts_ingest_runs`` — persisted run reports
for ``POST /api/facts/ingest`` (spec §7.2/§13.2).

PG-first ratchet (A3): brand-new app-state surface added after the freeze,
so there is no DuckDB sibling — see ``docs/migrations.md`` -> "Adding a
PG-only feature". Reach this repo only through
``src.repositories.facts_ingest_runs_repo()``; on a DuckDB-backed instance
that factory call raises ``RequiresPostgresBackend`` (translated to a
``501`` by the app-wide handler).

Deliberately a SEPARATE repository from ``src/repositories/facts_pg.py``
(rather than one more method on ``FactsPgRepository``): a run-report write
must never be part of the ingest transaction, so it wants a name and a call
site that make "this is a best-effort side record, not part of ingest" hard
to miss — see ``app/api/facts.py::facts_ingest``'s log-and-continue wrapper.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

import sqlalchemy as sa
from sqlalchemy.engine import Engine


_LIST_JSON_FIELDS = ("corpus_ids", "claims_rejected", "source_urls_rejected", "deferred", "review_items")

# Approximate per-million-token USD rates (input, output) for a handful of
# common LLM families — cost-VISIBILITY only, never billing-grade: real
# pricing varies by tier/region/contract and drifts over time. Prompt-cache
# discounts/premiums are NOT modeled — a `cache_read_input_tokens` or
# `cache_creation_input_tokens` token is folded into the plain input-token
# bucket at the input rate (a deliberate simplification: it under-counts a
# cache write and over-counts a cache read rather than guess at multipliers
# this module cannot verify). Sampled from each vendor's public pricing
# page, 2026-08-31. A model absent from this table (a fine-tune, a renamed
# release, a non-Anthropic/OpenAI model) is left OUT of the cost estimate
# entirely rather than assigned a guessed rate — see `_price_run_usd`.
_LLM_RATE_CARD_USD_PER_MILLION_TOKENS: Dict[str, Tuple[float, float]] = {
    "claude-opus-4": (15.0, 75.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-3-7-sonnet": (3.0, 15.0),
    "claude-3-5-sonnet": (3.0, 15.0),
    "claude-3-5-haiku": (0.8, 4.0),
    "claude-3-opus": (15.0, 75.0),
    "claude-3-haiku": (0.25, 1.25),
    "gpt-4o-mini": (0.15, 0.6),
    "gpt-4o": (2.5, 10.0),
    "gpt-4.1-mini": (0.4, 1.6),
    "gpt-4.1": (2.0, 8.0),
}


def _lookup_rate(model: str) -> Optional[Tuple[float, float]]:
    """Exact match first, then longest-known-prefix — a producer typically
    reports a date-stamped or versioned model id (``claude-3-5-sonnet-
    20241022``, ``gpt-4o-2024-08-06``) rather than the bare family name."""
    key = (model or "").strip().lower()
    if not key:
        return None
    if key in _LLM_RATE_CARD_USD_PER_MILLION_TOKENS:
        return _LLM_RATE_CARD_USD_PER_MILLION_TOKENS[key]
    for known in sorted(_LLM_RATE_CARD_USD_PER_MILLION_TOKENS, key=len, reverse=True):
        if key.startswith(known):
            return _LLM_RATE_CARD_USD_PER_MILLION_TOKENS[known]
    return None


def _price_run_usd(usage: Dict[str, Any]) -> Optional[float]:
    """Best-effort single-run cost estimate in USD, or ``None`` when this
    run cannot be HONESTLY priced: no model named, more than one model
    named (there is no per-model token breakdown to split them by — a
    fabricated split across mixed models would look precise while being
    invented), or a model absent from the rate card above."""
    models = usage.get("models") or []
    if len(models) != 1:
        return None
    rate = _lookup_rate(models[0])
    if rate is None:
        return None
    rate_in, rate_out = rate
    input_like = (
        (usage.get("input_tokens") or 0)
        + (usage.get("cache_read_input_tokens") or 0)
        + (usage.get("cache_creation_input_tokens") or 0)
    )
    output_tokens = usage.get("output_tokens") or 0
    return (input_like / 1_000_000.0) * rate_in + (output_tokens / 1_000_000.0) * rate_out


def _decode_row(row: Dict[str, Any]) -> Dict[str, Any]:
    row = dict(row)
    for key in _LIST_JSON_FIELDS:
        value = row.get(key)
        if isinstance(value, str):
            try:
                row[key] = json.loads(value)
            except (ValueError, TypeError):
                row[key] = []
    # `anonymization` is a dict, not a list — decoded separately so a bad
    # payload falls back to `{}` (never `[]`, which every OTHER field above
    # correctly falls back to).
    anonymization = row.get("anonymization")
    if isinstance(anonymization, str):
        try:
            row["anonymization"] = json.loads(anonymization)
        except (ValueError, TypeError):
            row["anonymization"] = {}
    elif anonymization is None:
        row["anonymization"] = {}
    # `llm_usage` is genuinely NULLABLE (unlike `anonymization` above) — a
    # run that never reported usage stays `None`, never a fabricated `{}`.
    llm_usage = row.get("llm_usage")
    if isinstance(llm_usage, str):
        try:
            row["llm_usage"] = json.loads(llm_usage)
        except (ValueError, TypeError):
            row["llm_usage"] = None
    if row.get("created_at") is not None:
        row["created_at"] = row["created_at"].isoformat()
    return row


class FactsIngestRunsPgRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create(
        self,
        *,
        corpus_ids: List[str],
        caller: str,
        documents_seen: int,
        claims_written: int,
        claims_rejected: List[Dict[str, Any]],
        deferred: List[Dict[str, Any]],
        subjects_created: int,
        subjects_deleted: int,
        review_items: List[Dict[str, Any]],
        anonymization: Optional[Dict[str, Any]] = None,
        source_urls_rejected: Optional[List[Dict[str, Any]]] = None,
        llm_usage: Optional[Dict[str, Any]] = None,
        edges_skipped_missing_endpoint: int = 0,
    ) -> str:
        """Persist one ingest batch's run report. Returns the generated id.

        Called AFTER ``FactsPgRepository.ingest_batch()`` has already
        returned — this insert is intentionally its own statement, its own
        connection, and its own failure domain (see the module docstring
        and ``app/api/facts.py::facts_ingest``): a report-write failure must
        never look like an ingest failure to the caller.

        ``anonymization`` (spec §9.2) is the producer's OPTIONAL declaration
        that (some of) this batch went through the anonymize-in-front
        pipeline before ingestion — ``None`` (the default, a producer that
        never anonymizes) stores ``{}``, never ``NULL``, so every reader can
        treat the column as always-present.

        ``source_urls_rejected`` (O7 follow-up) is the itemized
        ``{doc_id, reason}`` list of ``documents[].source_url`` values
        ``ingest_batch`` dropped as invalid (never-https, no host, too
        long, ...) — the claim itself still wrote, only its citation link
        is missing. ``None``/omitted (the default, no drops this batch)
        stores ``[]``, same never-``NULL`` contract as every other list
        field here; ``source_urls_rejected_count`` is derived the same way
        ``claims_rejected_count`` is, never trusted from the caller.

        ``llm_usage`` (cost-visibility) is the producer's OPTIONAL per-run
        tally of tokens/prompt-cache/wall-time — ``None``/omitted stores
        SQL ``NULL``, NOT ``{}``: unlike every field above, this one has no
        "producer never uses the feature" default shape to fall back to —
        a run this instance never got a usage figure for should read back
        as "unknown", never a fabricated zero. Never part of the ingest
        fingerprint/idempotency logic — purely descriptive metadata about
        the run that produced this batch.

        ``edges_skipped_missing_endpoint`` is ``ingest_batch``'s count of
        edges NOT written because their ``src``/``dst`` fact was gone by the
        time the INSERT ran (see ``EdgeEndpointMissing``) — a race between
        concurrent facts-extraction passes sharing one fact graph, never a
        producer mistake, so (unlike ``claims_rejected``) there is no
        itemized detail list here. Defaults to ``0``, same never-``NULL``
        contract as every other count on this table.
        """
        run_id = "ir_" + secrets.token_hex(8)
        source_urls_rejected = source_urls_rejected or []
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO facts_ingest_runs "
                    "(id, corpus_ids, caller, documents_seen, claims_written, "
                    " claims_rejected_count, claims_rejected, "
                    " source_urls_rejected_count, source_urls_rejected, deferred, "
                    " subjects_created, subjects_deleted, review_items, anonymization, llm_usage, "
                    " edges_skipped_missing_endpoint) "
                    "VALUES (:id, :corpus_ids, :caller, :documents_seen, :claims_written, "
                    "        :claims_rejected_count, :claims_rejected, "
                    "        :source_urls_rejected_count, :source_urls_rejected, :deferred, "
                    "        :subjects_created, :subjects_deleted, :review_items, :anonymization, :llm_usage, "
                    "        :edges_skipped_missing_endpoint)"
                ),
                {
                    "id": run_id,
                    "corpus_ids": json.dumps(sorted(set(corpus_ids))),
                    "caller": caller,
                    "documents_seen": documents_seen,
                    "claims_written": claims_written,
                    "claims_rejected_count": len(claims_rejected),
                    "claims_rejected": json.dumps(claims_rejected),
                    "source_urls_rejected_count": len(source_urls_rejected),
                    "source_urls_rejected": json.dumps(source_urls_rejected),
                    "deferred": json.dumps(deferred),
                    "subjects_created": subjects_created,
                    "subjects_deleted": subjects_deleted,
                    "review_items": json.dumps(review_items),
                    "anonymization": json.dumps(anonymization or {}),
                    "llm_usage": json.dumps(llm_usage) if llm_usage is not None else None,
                    "edges_skipped_missing_endpoint": edges_skipped_missing_endpoint,
                },
            )
        return run_id

    def get(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(sa.text("SELECT * FROM facts_ingest_runs WHERE id = :id"), {"id": run_id})
                .mappings()
                .first()
            )
        return _decode_row(dict(row)) if row else None

    def list_recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Most recent runs first — the source card's history (spec §13.2);
        ``[0]`` is "the LAST run report" the error badges are drawn from."""
        limit = max(1, min(limit, 200))
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text("SELECT * FROM facts_ingest_runs ORDER BY created_at DESC LIMIT :limit"),
                    {"limit": limit},
                )
                .mappings()
                .all()
            )
        return [_decode_row(dict(r)) for r in rows]

    def llm_usage_rollup(self) -> Dict[str, Any]:
        """Cumulative producer-reported LLM usage across EVERY persisted
        ingest run — honest, ongoing cost VISIBILITY, not a metric to win
        (the token-efficiency criterion this feeds is a separate,
        known-losing comparison against baselines).

        Instance-wide, not per-connection: an ingest run report has no
        column tying it back to the connection that produced it (only the
        ``corpus_ids`` it touched), so a per-connection split here would
        silently misattribute usage the moment a second SharePoint
        connection exists on one instance. Unlike ``_sharepoint_pipeline_
        cell``'s crawl/extract/facts counts (``app/web/router.py``), which
        DO resolve a connection's own scope collections from its
        ``config.scopes[].collection_id``, this rollup has nothing
        equivalent to key off — acceptable for today's common
        single-connection instance; revisit if a run report ever carries
        its own connection id.

        A run whose ``llm_usage`` is ``NULL`` (never reported, or predates
        this feature) contributes nothing and is not counted in
        ``runs_with_usage``. ``estimated_cost_usd`` is ``None`` until at
        least one run can be priced (see ``_price_run_usd``) — never a
        fabricated number built from a guessed rate; ``priced_runs`` vs
        ``runs_with_usage`` tells the caller how much of the total the
        estimate actually covers.
        """
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT llm_usage FROM facts_ingest_runs WHERE llm_usage IS NOT NULL")
            ).fetchall()

        totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "documents": 0,
        }
        wall_seconds = 0.0
        models_seen: set = set()
        runs_with_usage = 0
        priced_runs = 0
        estimated_cost_usd = 0.0

        for (raw,) in rows:
            usage = raw if isinstance(raw, dict) else (json.loads(raw) if isinstance(raw, str) else None)
            if not usage:
                continue
            runs_with_usage += 1
            for key in totals:
                totals[key] += usage.get(key) or 0
            wall_seconds += usage.get("wall_seconds") or 0
            models_seen.update(usage.get("models") or [])
            cost = _price_run_usd(usage)
            if cost is not None:
                priced_runs += 1
                estimated_cost_usd += cost

        return {
            "runs_with_usage": runs_with_usage,
            **totals,
            "wall_seconds": round(wall_seconds, 3),
            "models": sorted(models_seen),
            "priced_runs": priced_runs,
            "estimated_cost_usd": round(estimated_cost_usd, 4) if priced_runs else None,
            "estimated_cost_note": (
                "approximate — sampled vendor list pricing (see "
                "_LLM_RATE_CARD_USD_PER_MILLION_TOKENS), not billing-grade; covers only priced_runs of "
                "runs_with_usage (an unpriced run reported no model, more than one model, or a model absent "
                "from the rate card)"
            ),
        }

    def documents_done_since(self, corpus_ids: Sequence[str], since: datetime) -> int:
        """``sum(documents_seen)`` across every run report persisted at or
        after ``since`` whose ``corpus_ids`` OVERLAPS ``corpus_ids`` — the
        fleet/status throughput signal for a SharePoint connection's own
        facts pass (TCRD-296 gap #67, ``app/api/admin_extraction.py::
        _facts_throughput_and_eta``: "documents done in the last N minutes
        for the connection's collections").

        Connection-scoped by OVERLAP, not equality: a run report's
        ``corpus_ids`` is whatever collections that ONE batch actually
        touched (typically all of a connection's own, but not guaranteed
        — see :meth:`create`'s docstring). ``jsonb_array_elements_text`` +
        ``= ANY(:corpus_ids)`` is exactly "did this run touch at least one
        of THIS connection's own collections" without decoding the column
        in Python — deliberately not the ``?|`` "any key" jsonb operator,
        whose bare ``?`` collides with this driver's bind-parameter style.
        Empty ``corpus_ids`` answers ``0`` without a query — nothing to
        overlap.
        """
        if not corpus_ids:
            return 0
        with self._engine.connect() as conn:
            total = conn.execute(
                sa.text(
                    "SELECT COALESCE(SUM(documents_seen), 0) FROM facts_ingest_runs "
                    "WHERE created_at >= :since AND EXISTS ("
                    "  SELECT 1 FROM jsonb_array_elements_text(corpus_ids) AS cid "
                    "  WHERE cid = ANY(:corpus_ids)"
                    ")"
                ),
                {"since": since, "corpus_ids": list(corpus_ids)},
            ).scalar()
        return int(total or 0)

    def llm_usage_rollup_by_corpus_ids(self, corpus_ids_by_key: Dict[str, Sequence[str]]) -> Dict[str, Dict[str, Any]]:
        """Cumulative LLM-usage rollup PER CALLER-SUPPLIED KEY, attributed by
        ``corpus_ids`` overlap — the batched, connection-scoped sibling of
        :meth:`llm_usage_rollup` (which stays instance-wide, see its own
        docstring for why: a run report has no connection id of its own).

        ``corpus_ids_by_key`` is typically ``{connection_id: [collection_id,
        ...]}`` — the caller's own resolution of a connection's own
        collections (e.g. ``connectors.sharepoint.facts_extraction.
        collection_ids_for``), one entry per SharePoint connection a fleet
        page is about to render. This method issues exactly ONE query
        (bounded by the union of every requested id) regardless of how many
        keys are asked about, never one round trip per key — the fix for
        the fleet view's own cost tile previously reading ~$0 for a
        connection whose facts stage ran as SEPARATE
        ``sharepoint-facts-extraction`` jobs, which persist their spend
        here rather than on the crawl's own ``extraction_runs`` row.

        A key present in ``corpus_ids_by_key`` but matching NO run —
        including one whose own ``corpus_ids`` list is empty, nothing to
        overlap — still gets a full zeroed-out entry (``runs_with_usage:
        0``, ``estimated_cost_usd: None``), never a missing dict key, so a
        caller can index every connection it asked about without a
        membership check first.

        A run whose ``corpus_ids`` overlaps MORE THAN ONE key's own
        collections is counted toward EACH of them — the same "did THIS
        caller's collections see this run" question :meth:`documents_done_
        since` already answers per key, extended to every key at once. It
        is never double-counted WITHIN one key: a run touching two
        collections that both belong to the same key still contributes its
        usage exactly once (de-duplicated in Python, since one JSON row can
        match several of a key's own corpus ids).

        This deliberate multi-key attribution is real and supported — a
        SharePoint bulk-add / consolidation can point more than one
        connection's scope at the SAME collection (see
        ``app/api/admin_sharepoint.py::_collection_still_referenced``) — so
        each key's own ``estimated_cost_usd``/``runs_with_usage`` here is
        an honest "what did THIS connection's collections see", not a
        partition of instance spend; a caller SUMMING several keys'
        ``estimated_cost_usd`` together would double (or N-times) count a
        shared run. Each key's ``runs`` list — ``[{id, estimated_cost_usd},
        ...]``, one entry per contributing run — exists exactly so a caller
        that needs a page-wide TOTAL can de-duplicate by run id itself
        (count a run once no matter how many keys' lists it appears in)
        rather than summing the per-key aggregates directly. See
        ``app/api/admin_extraction.py::fleet_extraction_runs`` for the
        de-duplicated total and its per-row ``cost_shared``/
        ``cost_shared_with`` markers.

        Priced through :mod:`src.llm_pricing` (``cost_usd`` — the SAME
        model-aware, cache-aware price table ``GET /api/admin/telemetry/
        chat-cost`` uses), NOT the sampled rate card :func:`_price_run_usd`
        above uses for the instance-wide rollup: a cache read/write here is
        priced at its real ~0.1x/~1.25x rate rather than folded into the
        plain input-token bucket. A run naming exactly one model is priced
        at that model's rate (falling back to the same "most expensive
        general-purpose tier" guess every other cost surface in Agnes uses
        for an unrecognized model id, never a fabricated zero); a run
        naming zero or more-than-one model cannot be honestly split by
        model and is left OUT of ``estimated_cost_usd`` — it still counts
        toward the token/document totals and toward ``models`` — same
        disclosure contract (``priced_runs`` vs ``runs_with_usage``) as
        :meth:`llm_usage_rollup`.
        """
        keys = list(corpus_ids_by_key)
        id_sets: Dict[str, set] = {key: {str(c) for c in (corpus_ids_by_key.get(key) or [])} for key in keys}
        out: Dict[str, Dict[str, Any]] = {
            key: {
                "runs_with_usage": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "documents": 0,
                "wall_seconds": 0.0,
                "models": [],
                "priced_runs": 0,
                "estimated_cost_usd": None,
                # One entry per contributing run — see this method's own
                # docstring for why a de-duplicating caller needs run ids,
                # not just this key's own aggregate.
                "runs": [],
            }
            for key in keys
        }
        all_ids = sorted({cid for ids in id_sets.values() for cid in ids})
        if not all_ids:
            return out

        from src.llm_pricing import cost_usd

        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT id, corpus_ids, llm_usage FROM facts_ingest_runs "
                    "WHERE llm_usage IS NOT NULL AND EXISTS ("
                    "  SELECT 1 FROM jsonb_array_elements_text(corpus_ids) AS cid "
                    "  WHERE cid = ANY(:all_ids)"
                    ")"
                ),
                {"all_ids": all_ids},
            ).fetchall()

        models_seen: Dict[str, set] = {key: set() for key in keys}
        cost_accum: Dict[str, float] = {key: 0.0 for key in keys}

        for run_id, raw_corpus_ids, raw_usage in rows:
            corpus_ids = (
                raw_corpus_ids
                if isinstance(raw_corpus_ids, list)
                else (json.loads(raw_corpus_ids) if isinstance(raw_corpus_ids, str) else [])
            )
            usage = (
                raw_usage
                if isinstance(raw_usage, dict)
                else (json.loads(raw_usage) if isinstance(raw_usage, str) else None)
            )
            if not usage:
                continue
            models = usage.get("models") or []
            run_cost: Optional[float] = None
            if len(models) == 1:
                run_cost = cost_usd(
                    model=models[0],
                    input_tokens=usage.get("input_tokens") or 0,
                    output_tokens=usage.get("output_tokens") or 0,
                    cache_read_tokens=usage.get("cache_read_input_tokens") or 0,
                    cache_creation_tokens=usage.get("cache_creation_input_tokens") or 0,
                )
            run_corpus_ids = {str(c) for c in corpus_ids}
            for key in keys:
                if not (run_corpus_ids & id_sets[key]):
                    continue
                totals = out[key]
                totals["runs_with_usage"] += 1
                for field in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                    "documents",
                ):
                    totals[field] += usage.get(field) or 0
                totals["wall_seconds"] += usage.get("wall_seconds") or 0
                models_seen[key].update(models)
                totals["runs"].append({"id": str(run_id), "estimated_cost_usd": run_cost})
                if run_cost is not None:
                    totals["priced_runs"] += 1
                    cost_accum[key] += run_cost

        for key in keys:
            out[key]["wall_seconds"] = round(out[key]["wall_seconds"], 3)
            out[key]["models"] = sorted(models_seen[key])
            out[key]["estimated_cost_usd"] = round(cost_accum[key], 4) if out[key]["priced_runs"] else None

        return out
