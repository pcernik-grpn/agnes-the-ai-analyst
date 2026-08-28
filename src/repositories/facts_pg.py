"""Postgres-only repository for the fact graph over Collections.

facts / fact_aliases / edges / claims / corrections — build order step 2+3
of docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md.

PG-first ratchet (A3): brand-new app-state surface added after the freeze,
so there is no DuckDB sibling — see ``docs/migrations.md`` -> "Adding a
PG-only feature". Reach this repo only through
``src.repositories.facts_repo()``; on a DuckDB-backed instance that factory
call raises ``RequiresPostgresBackend`` (translated to a ``501`` by the
app-wide handler in ``app/main.py``).

**Every read method funnels through the SAME visibility primitives in this
module** (spec §5): a claim counts toward a subject's existence/attrs/quotes
iff its ``corpus_id`` is in the caller's readable-collection set (or the
subject carries an active ``revealed`` correction, which bypasses grants
entirely — spec §4). Never ``can_access_collection`` — see
``app/auth/access.py::accessible_collection_ids`` docstring for why a
restricted ``AgentPrincipal`` would crash or elevate through it.

A nonexistent subject id and a subject with zero readable claims are
**indistinguishable** to every caller (§5 rule 2): both run the exact same
query shape and raise :class:`FactNotFound`, translated to a `404` by the
REST layer — never a `403`.

**Endpoint evidence (spec §4, rev 3.2 — found by the Run P proving run):**
the producer wire contract (§7.0) explicitly permits a node with no evidence
of its own to exist purely to anchor an evidenced edge ("nodes without
evidence are warnings, every edge carries >=1 evidence"). An edge's claim
therefore evidences the relationship AND, implicitly, the existence of its
two endpoints: a FACT subject's existence check runs over its OWN claims
UNION the claims of every edge incident to it that is not itself withheld
(`wrong`/`restricted`) — same corpus-readability predicate, same any/all
`_visibility_mode()` semantics. This applies ONLY to existence — the attrs
projection (`search()`, `collection_facts_summary()`) and the claims listing
(`claims()`) stay OWN-claims-only, so an endpoint-only fact (no own claims
at all) is visible with `attrs: {}` and an empty `claims` list rather than a
`404`. EDGE visibility is unchanged (an edge's own claims only, spec S3);
this is existence propagation for FACTS one hop out over their incident
edges, not the reverse.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine

# Query-surface caps (spec §12) — the repository enforces these itself
# (defense in depth) even though the REST layer's Pydantic models already
# cap the request shape; a future CLI/MCP caller reaches the same floor.
MAX_SEARCH_LIMIT = 100
MAX_NEIGHBORS_DEPTH = 2
MAX_NEIGHBORS_FANOUT = 100
MAX_NEIGHBORS_RESULT = 500
MAX_SEARCH_FILTERS = 20

# Ingest batch caps (spec §7.2): "≤500 documents, ≤5000 claims per request".
# A single document's evidence count over MAX_INGEST_CLAIMS is a protocol
# error (IngestDocumentExceedsClaimCap) rather than a batch-size error — the
# caller cannot fix it by splitting the request, because §7.2 requires a
# `full_documents`-listed document's COMPLETE claim set to arrive together.
MAX_INGEST_DOCUMENTS = 500
MAX_INGEST_CLAIMS = 5000

# One statement-scoped guard against a runaway traversal on power-law data
# (spec §12 — "the hub-node walk is the query that explodes"). Local to the
# repo, not an operator switch — the caps above are the primary defense.
_STATEMENT_TIMEOUT_MS = 5_000


class FactNotFound(RuntimeError):
    """A subject that does not exist OR has no readable claim (spec §5 rule
    2) — the caller of this repository must translate this to a clean `404`
    and never leak which of the two applied."""

    def __init__(self, subject_id: str) -> None:
        self.subject_id = subject_id
        super().__init__(f"fact subject {subject_id!r} not found")


class IngestBatchTooLarge(RuntimeError):
    """A whole-batch size cap (§7.2: ≤500 documents / ≤5000 claims) was
    exceeded. ``detail`` is the itemized reason the REST layer serializes
    on a `413`."""

    def __init__(self, detail: Dict[str, Any]) -> None:
        self.detail = detail
        super().__init__(str(detail))


class IngestDocumentExceedsClaimCap(RuntimeError):
    """A SINGLE document's evidence count exceeds ``MAX_INGEST_CLAIMS`` —
    never split (§7.2): the caller must shrink that document's own claim
    set, not merely paginate the batch. Translated to a `422`."""

    def __init__(self, doc_id: str, count: int) -> None:
        self.doc_id = doc_id
        self.count = count
        super().__init__(f"document {doc_id!r} carries {count} claims (cap {MAX_INGEST_CLAIMS})")


class IngestUnresolvedDocIds(RuntimeError):
    """``documents`` was omitted but not every referenced ``doc_id``
    already resolves (§7.2) — the whole batch is rejected, itemized.
    Translated to a `400`."""

    def __init__(self, unresolved: List[str]) -> None:
        self.unresolved = unresolved
        super().__init__(f"unresolved doc_ids: {unresolved}")


def _decode_jsonb(value: Any) -> Any:
    """PG JSONB columns come back as ``str`` through a raw ``sa.text()``
    query on this driver stack (see ``src/repositories/knowledge_pg.py``);
    decode defensively so an already-adapted value (dict/list) passes
    through unchanged."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def _visibility_mode() -> str:
    from app.switches import switch_value

    mode = switch_value("facts_visibility_mode")
    return mode if mode == "all_evidence" else "any_evidence"


def _readable_ids(caller) -> Optional[frozenset]:
    """The caller's readable-collection set. ``None`` = admin (no filter).

    Delegates entirely to :func:`app.auth.access.accessible_collection_ids`
    — the ONE sanctioned primitive (spec §5); never
    ``can_access_collection``.
    """
    from app.auth.access import accessible_collection_ids

    ids = accessible_collection_ids(caller)
    if ids is None:
        return None
    return frozenset(ids)


def _single_valued_edge_types() -> frozenset:
    """Edge types a src fact should carry exactly one LIVE dst for (spec
    §7.3) — a second distinct dst is a reconciliation problem, not a fact.
    Configurable via ``facts.single_valued_edges`` in instance.yaml (plain
    data, not a bool/select toggle, so this is an ordinary ``get_value``
    read rather than a switch registry entry); default ``["owned_by",
    "for_client"]``. A malformed (non-list) override falls back to the
    default rather than silently disabling detection."""
    from app.instance_config import get_value

    default = ["owned_by", "for_client"]
    configured = get_value("facts", "single_valued_edges", default=default)
    if not isinstance(configured, (list, tuple)):
        configured = default
    return frozenset(str(t) for t in configured)


class FactsPgRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # ------------------------------------------------------------------
    # internal write/seed methods — minimal and obviously correct; the
    # ingest task (build order step 4) builds the real write-path protocol
    # (verbatim gate, union/replace modes, run report) on top of these.
    # ------------------------------------------------------------------

    def create_fact(self, *, type: str, natural_key: Optional[str] = None) -> str:
        fact_id = "f_" + secrets.token_hex(8)
        with self._engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO facts (id, type) VALUES (:id, :type)"), {"id": fact_id, "type": type})
            if natural_key:
                conn.execute(
                    sa.text(
                        "INSERT INTO fact_aliases (fact_id, type, natural_key) VALUES (:fid, :type, :nk) "
                        "ON CONFLICT (type, natural_key) DO UPDATE SET fact_id = EXCLUDED.fact_id"
                    ),
                    {"fid": fact_id, "type": type, "nk": natural_key},
                )
        return fact_id

    def add_alias(self, *, fact_id: str, type: str, natural_key: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO fact_aliases (fact_id, type, natural_key) VALUES (:fid, :type, :nk) "
                    "ON CONFLICT (type, natural_key) DO UPDATE SET fact_id = EXCLUDED.fact_id"
                ),
                {"fid": fact_id, "type": type, "nk": natural_key},
            )

    def create_edge(self, *, src: str, type: str, dst: str) -> str:
        edge_id = "e_" + secrets.token_hex(8)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO edges (id, src, type, dst) VALUES (:id, :src, :type, :dst) "
                    "ON CONFLICT (src, type, dst) DO NOTHING"
                ),
                {"id": edge_id, "src": src, "type": type, "dst": dst},
            )
            row = (
                conn.execute(
                    sa.text("SELECT id FROM edges WHERE src = :src AND type = :type AND dst = :dst"),
                    {"src": src, "type": type, "dst": dst},
                )
                .mappings()
                .first()
            )
        assert row is not None
        return row["id"]

    def add_claim(
        self,
        *,
        fact_id: Optional[str] = None,
        edge_id: Optional[str] = None,
        corpus_file_id: str,
        corpus_id: str,
        file_sha256: str,
        quote: str,
        attrs: Optional[dict] = None,
        document_date: Optional[date] = None,
    ) -> Optional[str]:
        """Insert a claim; ``ON CONFLICT ... DO NOTHING`` on the (subject,
        corpus_file_id, quote_hash) functional unique index makes a replay
        (spec §7.2's union-mode idempotency) a true no-op. Returns the new
        claim id, or ``None`` when this exact triple already existed — the
        ingest write path (build order step 4) uses that to count
        ``claims_written`` accurately across a replayed batch; no other
        caller (read-path fixtures) inspects the return value."""
        if (fact_id is None) == (edge_id is None):
            raise ValueError("add_claim requires exactly one of fact_id/edge_id")
        claim_id = "c_" + secrets.token_hex(8)
        quote_hash = hashlib.sha256(quote.encode("utf-8")).hexdigest()[:16]
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text(
                    "INSERT INTO claims "
                    "(id, fact_id, edge_id, corpus_file_id, corpus_id, file_sha256, attrs, quote, "
                    " quote_hash, document_date) "
                    "VALUES (:id, :fact_id, :edge_id, :corpus_file_id, :corpus_id, :file_sha256, "
                    "        CAST(:attrs AS JSONB), :quote, :quote_hash, :document_date) "
                    "ON CONFLICT (COALESCE(fact_id, edge_id), corpus_file_id, quote_hash) DO NOTHING"
                ),
                {
                    "id": claim_id,
                    "fact_id": fact_id,
                    "edge_id": edge_id,
                    "corpus_file_id": corpus_file_id,
                    "corpus_id": corpus_id,
                    "file_sha256": file_sha256,
                    "attrs": json.dumps(attrs or {}),
                    "quote": quote,
                    "quote_hash": quote_hash,
                    "document_date": document_date,
                },
            )
        return claim_id if result.rowcount else None

    def delete_claims_for_file(self, corpus_file_id: str) -> int:
        """Delete every claim anchored to one ``corpus_files`` row; return the
        count.

        Used when the row's CONTENT is replaced in place (spec §6, "content
        changed"). Since #1655 an upload matching an existing row updates it
        in place instead of delete+insert, so the ``claims.corpus_file_id``
        ``ON DELETE CASCADE`` that used to clear a replaced document's claims
        no longer fires. Every claim carries a verbatim ``quote`` validated
        against the file's chunks at ingest (§8); once ``corpus_files.sha256``
        moves, those bytes are gone (the old blob is refcount-deleted) and the
        quote is unverifiable by construction — so the claims are dropped at
        replace time rather than served as current evidence until the
        producer's next extraction, which may never come for a hand-replaced
        file.

        Deliberately claims-only: facts/edges left with no evidence are
        removed by ``sweep_orphans`` as its own step afterwards (§6), exactly
        like the delete-driven cascade path.
        """
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text("DELETE FROM claims WHERE corpus_file_id = :file_id"),
                {"file_id": corpus_file_id},
            )
        return int(result.rowcount or 0)

    def upsert_correction(
        self,
        *,
        subject_kind: str,
        subject_id: str,
        natural_keys: Any,
        verdict: str,
        reason: str,
        decided_by: str,
    ) -> None:
        if subject_kind not in ("fact", "edge"):
            raise ValueError(f"subject_kind must be 'fact' or 'edge', got {subject_kind!r}")
        if verdict not in ("wrong", "restricted", "revealed"):
            raise ValueError(f"verdict must be one of wrong/restricted/revealed, got {verdict!r}")
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO corrections "
                    "(subject_kind, subject_id, natural_keys, verdict, reason, decided_by) "
                    "VALUES (:kind, :id, CAST(:nk AS JSONB), :verdict, :reason, :by) "
                    "ON CONFLICT (subject_kind, subject_id) DO UPDATE SET "
                    "natural_keys = EXCLUDED.natural_keys, verdict = EXCLUDED.verdict, "
                    "reason = EXCLUDED.reason, decided_by = EXCLUDED.decided_by, decided_at = now()"
                ),
                {
                    "kind": subject_kind,
                    "id": subject_id,
                    "nk": json.dumps(natural_keys),
                    "verdict": verdict,
                    "reason": reason,
                    "by": decided_by,
                },
            )

    def delete_correction(self, *, subject_kind: str, subject_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM corrections WHERE subject_kind = :kind AND subject_id = :id"),
                {"kind": subject_kind, "id": subject_id},
            )

    def list_wrong_corrections(self) -> List[Dict[str, Any]]:
        """The producer export (spec §7.4): every ``wrong`` subject with its
        ``natural_keys`` snapshot, so a re-extraction pass can prune them
        before re-asserting claims. Corrections are enforced at read time
        regardless (§4) -- this is a courtesy, not the enforcement boundary."""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT subject_kind, subject_id, natural_keys, reason, decided_by, decided_at "
                        "FROM corrections WHERE verdict = 'wrong' ORDER BY decided_at"
                    )
                )
                .mappings()
                .all()
            )
        return [
            {
                "subject_kind": r["subject_kind"],
                "subject_id": r["subject_id"],
                "natural_keys": _decode_jsonb(r["natural_keys"]),
                "reason": r["reason"],
                "decided_by": r["decided_by"],
                "decided_at": r["decided_at"].isoformat() if r["decided_at"] else None,
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # shared visibility primitives (spec §5) — every read method below
    # goes through these, and only these.
    # ------------------------------------------------------------------

    @staticmethod
    def _visibility_predicate(column: str, is_admin: bool) -> str:
        """SQL fragment for "this claim's corpus_id is in the caller's
        readable set" — ``TRUE`` for admin (no filter, per
        ``accessible_collection_ids``), else a bound-parameter ``= ANY``
        check. ``column`` is always a literal we control (``c.corpus_id``),
        never caller input."""
        return "TRUE" if is_admin else f"{column} = ANY(:readable)"

    def _subject_status(
        self,
        conn,
        *,
        subject_kind: str,
        subject_id: str,
        is_admin: bool,
        all_evidence: bool,
        readable: Optional[frozenset] = None,
    ) -> Dict[str, bool]:
        """One query, reused by every single-subject visibility check
        (``neighbors`` node expansion, ``claims``) — same shape and cost
        whether ``subject_id`` exists or not, so a nonexistent id and a
        withheld/unreadable one cost the same (spec §5 rule 2).

        For ``subject_kind='fact'`` the existence corpus (``relevant_claims``
        below) is the subject's OWN claims UNION the claims of every
        incident edge that is not itself withheld (module docstring,
        "Endpoint evidence") — an endpoint-only fact (zero own claims) can
        therefore be visible purely because an edge naming it carries a
        readable claim. For ``subject_kind='edge'`` the corpus is unchanged:
        the edge's own claims only (spec S3 — edge visibility is never
        inferred from its endpoints)."""
        vis = self._visibility_predicate("rc.corpus_id", is_admin)
        if subject_kind == "fact":
            relevant_claims_cte = """
                relevant_claims AS (
                    SELECT corpus_id FROM claims WHERE fact_id = :subject_id
                    UNION ALL
                    SELECT c.corpus_id
                    FROM claims c
                    JOIN edges e ON e.id = c.edge_id
                    WHERE (e.src = :subject_id OR e.dst = :subject_id)
                      AND NOT EXISTS (
                        SELECT 1 FROM corrections co
                        WHERE co.subject_kind = 'edge' AND co.subject_id = e.id
                          AND co.verdict IN ('wrong', 'restricted')
                      )
                )
            """
        else:
            relevant_claims_cte = "relevant_claims AS (SELECT corpus_id FROM claims WHERE edge_id = :subject_id)"
        if all_evidence:
            has_claim_visibility = (
                "EXISTS (SELECT 1 FROM relevant_claims) "
                f"AND NOT EXISTS (SELECT 1 FROM relevant_claims rc WHERE NOT ({vis}))"
            )
        else:
            has_claim_visibility = f"EXISTS (SELECT 1 FROM relevant_claims rc WHERE {vis})"
        sql = sa.text(
            f"WITH {relevant_claims_cte} "
            "SELECT "
            "EXISTS (SELECT 1 FROM corrections co WHERE co.subject_kind = :kind AND co.subject_id = :subject_id "
            "        AND co.verdict IN ('wrong', 'restricted')) AS withheld, "
            "EXISTS (SELECT 1 FROM corrections co WHERE co.subject_kind = :kind AND co.subject_id = :subject_id "
            "        AND co.verdict = 'revealed') AS revealed, "
            f"({has_claim_visibility}) AS has_claim_visibility"
        )
        params: Dict[str, Any] = {"kind": subject_kind, "subject_id": subject_id}
        if not is_admin:
            params["readable"] = list(readable) if readable else []
        row = conn.execute(sql, params).mappings().first()
        assert row is not None
        return dict(row)

    def _is_visible(self, status: Dict[str, bool]) -> bool:
        if status["withheld"]:
            return False
        return bool(status["revealed"] or status["has_claim_visibility"])

    @staticmethod
    def _projection_cte_sql(*, with_aliases: bool) -> str:
        """The per-key latest-document_date-wins attrs projection (spec
        §12), factored out of ``search()`` so ``neighbors()`` can serve
        the SAME projected shape on its nodes/edges without duplicating
        the ~40-line CTE chain. Consumes two CTEs the CALLER must already
        have defined earlier in the same ``WITH`` clause:

        - ``target_ids(subject_id)`` — the EXACT set of subjects to
          project (never a broader set — this is projection, not a
          visibility gate; the caller has already decided who is visible).
        - ``counted_claims(claim_id, subject_id, attrs, document_date)`` —
          the OWN, already caller-filtered claims to project from (own-
          claims-only, per S2: never the endpoint-evidence union).

        Produces ``subject_attrs(subject_id, attrs)``,
        ``counts(subject_id, claim_count)`` and, when ``with_aliases``,
        ``aliases(subject_id, aliases)`` (facts only — edges carry no
        aliases)."""
        parts = [
            """attr_kv AS (
                SELECT cc.subject_id, kv.key, kv.value, cc.document_date
                FROM counted_claims cc
                CROSS JOIN LATERAL jsonb_each(cc.attrs) AS kv(key, value)
            )""",
            """key_maxdate AS (
                SELECT subject_id, key, MAX(document_date) AS max_dated
                FROM attr_kv
                WHERE document_date IS NOT NULL
                GROUP BY subject_id, key
            )""",
            """winning AS (
                SELECT ak.subject_id, ak.key, ak.value, ak.document_date
                FROM attr_kv ak
                LEFT JOIN key_maxdate kmd ON kmd.subject_id = ak.subject_id AND kmd.key = ak.key
                WHERE (kmd.max_dated IS NOT NULL AND ak.document_date = kmd.max_dated)
                   OR (kmd.max_dated IS NULL)
            )""",
            """attr_proj AS (
                SELECT subject_id, key,
                       COUNT(DISTINCT value) AS n_distinct,
                       MAX(document_date) AS rep_date,
                       jsonb_agg(DISTINCT value) AS values_agg,
                       (array_agg(value))[1] AS single_value
                FROM winning
                GROUP BY subject_id, key
            )""",
            """attr_value_json AS (
                SELECT subject_id, key,
                    CASE WHEN n_distinct = 1
                        THEN jsonb_build_object('value', single_value, 'document_date', to_jsonb(rep_date))
                        ELSE jsonb_build_object('conflicted', true, 'values', values_agg)
                    END AS proj
                FROM attr_proj
            )""",
            """subject_attrs AS (
                SELECT subject_id, jsonb_object_agg(key, proj) AS attrs
                FROM attr_value_json
                GROUP BY subject_id
            )""",
        ]
        if with_aliases:
            parts.append(
                """aliases AS (
                SELECT fact_id AS subject_id, jsonb_agg(natural_key ORDER BY natural_key) AS aliases
                FROM fact_aliases
                WHERE fact_id IN (SELECT subject_id FROM target_ids)
                GROUP BY fact_id
            )"""
            )
        parts.append(
            """counts AS (
                SELECT t.subject_id, COUNT(cc.claim_id) AS claim_count
                FROM target_ids t
                LEFT JOIN counted_claims cc ON cc.subject_id = t.subject_id
                GROUP BY t.subject_id
            )"""
        )
        return ",\n".join(parts)

    def _project_subjects(
        self,
        conn,
        *,
        kind: str,
        ids_with_revealed: List[tuple],
        is_admin: bool,
        readable: Optional[frozenset],
        with_aliases: bool,
    ) -> Dict[str, Dict[str, Any]]:
        """Own-claims-only attrs/aliases/claim_count projection for an
        EXACT, already-determined set of subject ids — never a visibility
        gate itself (the caller has already decided which ids to project;
        ``neighbors()`` calls this AFTER truncation, per spec §12's cost
        note, so the projection never runs on more nodes/edges than the
        capped result set). Shares :meth:`_projection_cte_sql` with
        ``search()``, so a neighbors node/edge carries byte-identical
        projected ``attrs`` to a search subject (spec §12). A subject
        carrying an active ``revealed`` correction projects from ALL its
        own claims regardless of grants (spec §4, matching ``search()``);
        every other subject projects only from claims the caller can
        read."""
        if not ids_with_revealed:
            return {}
        ids = [i for i, _r in ids_with_revealed]
        revealed_flags = [bool(r) for _i, r in ids_with_revealed]
        kind_column = "fact_id" if kind == "fact" else "edge_id"
        vis = self._visibility_predicate("c.corpus_id", is_admin)
        alias_select = ", COALESCE(al.aliases, '[]'::jsonb) AS aliases" if with_aliases else ""
        alias_join = "LEFT JOIN aliases al ON al.subject_id = t.subject_id" if with_aliases else ""
        sql = sa.text(
            f"""
            WITH target_ids AS (
                SELECT * FROM unnest(CAST(:ids AS text[]), CAST(:revealed AS bool[])) AS t(subject_id, revealed)
            ),
            counted_claims AS (
                SELECT c.id AS claim_id, c.{kind_column} AS subject_id, c.attrs, c.document_date
                FROM claims c
                JOIN target_ids t ON t.subject_id = c.{kind_column}
                WHERE t.revealed OR ({vis})
            ),
            {self._projection_cte_sql(with_aliases=with_aliases)}
            SELECT t.subject_id,
                   COALESCE(cnt.claim_count, 0) AS claim_count{alias_select},
                   COALESCE(satt.attrs, '{{}}'::jsonb) AS attrs
            FROM target_ids t
            LEFT JOIN counts cnt ON cnt.subject_id = t.subject_id
            {alias_join}
            LEFT JOIN subject_attrs satt ON satt.subject_id = t.subject_id
            """
        )
        params: Dict[str, Any] = {"ids": ids, "revealed": revealed_flags}
        if not is_admin:
            params["readable"] = list(readable) if readable else []
        rows = conn.execute(sql, params).mappings().all()
        out: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            out[r["subject_id"]] = {
                "aliases": (_decode_jsonb(r["aliases"]) or []) if with_aliases else [],
                "attrs": _decode_jsonb(r["attrs"]) or {},
                "claim_count": int(r["claim_count"]),
            }
        return out

    # ------------------------------------------------------------------
    # search
    # ------------------------------------------------------------------

    def search(
        self,
        caller,
        *,
        type: Optional[str] = None,
        filters: Optional[Dict[str, Any]] = None,
        limit: int = MAX_SEARCH_LIMIT,
    ) -> Dict[str, Any]:
        """Type/filter search over visible FACT subjects (spec §5). A
        subject's EXISTENCE gate is the endpoint-evidence union (module
        docstring): its own claims OR the claims of a non-withheld incident
        edge. ``attrs``, ``claim_count`` and ``quote_count`` stay OWN-claims
        ONLY (via ``counted_claims``/``attr_kv`` below, never
        ``endpoint_claims``) — an endpoint-only fact (visible purely via an
        edge) always serves ``attrs: {}`` and a ``claim_count`` of 0, closing
        the attribute oracle (S2) exactly as before this refinement."""
        filters = filters or {}
        if len(filters) > MAX_SEARCH_FILTERS:
            raise ValueError(f"too many filters (max {MAX_SEARCH_FILTERS})")
        limit = max(1, min(limit, MAX_SEARCH_LIMIT))

        readable = _readable_ids(caller)
        is_admin = readable is None
        all_evidence = _visibility_mode() == "all_evidence"
        vis = self._visibility_predicate("c.corpus_id", is_admin)
        vis_ec = self._visibility_predicate("ec.corpus_id", is_admin)

        filter_clauses = []
        params: Dict[str, Any] = {"type": type, "limit_plus_one": limit + 1}
        if not is_admin:
            params["readable"] = list(readable)
        for i, (fkey, fval) in enumerate(filters.items()):
            params[f"fkey{i}"] = str(fkey)
            params[f"fval{i}"] = json.dumps(fval)
            filter_clauses.append(
                f"EXISTS (SELECT 1 FROM attr_proj ap WHERE ap.subject_id = v.subject_id "
                f"AND ap.key = :fkey{i} AND ap.n_distinct = 1 AND ap.single_value = CAST(:fval{i} AS JSONB))"
            )
        filter_sql = ("AND " + " AND ".join(filter_clauses)) if filter_clauses else ""

        sql = sa.text(
            f"""
            WITH candidates AS (
                SELECT f.id AS subject_id, f.type AS subject_type
                FROM facts f
                WHERE (CAST(:type AS TEXT) IS NULL OR f.type = :type)
                  AND NOT EXISTS (
                    SELECT 1 FROM corrections co
                    WHERE co.subject_kind = 'fact' AND co.subject_id = f.id
                      AND co.verdict IN ('wrong', 'restricted')
                  )
            ),
            revealed_ids AS (
                SELECT subject_id FROM corrections WHERE subject_kind = 'fact' AND verdict = 'revealed'
            ),
            counted_claims AS (
                SELECT c.id AS claim_id, c.fact_id AS subject_id, c.attrs, c.document_date
                FROM claims c
                JOIN candidates cand ON cand.subject_id = c.fact_id
                WHERE cand.subject_id IN (SELECT subject_id FROM revealed_ids) OR ({vis})
            ),
            endpoint_claims AS (
                -- Existence-only: claims of edges incident to a candidate,
                -- via a non-withheld edge (module docstring, "Endpoint
                -- evidence"). Never joined into attr_kv/counted_claims — the
                -- attrs projection and claim_count stay OWN-claims-only.
                SELECT DISTINCT cand.subject_id AS subject_id, c.corpus_id AS corpus_id
                FROM candidates cand
                JOIN edges e ON (e.src = cand.subject_id OR e.dst = cand.subject_id)
                JOIN claims c ON c.edge_id = e.id
                WHERE NOT EXISTS (
                    SELECT 1 FROM corrections co
                    WHERE co.subject_kind = 'edge' AND co.subject_id = e.id
                      AND co.verdict IN ('wrong', 'restricted')
                )
            ),
            visible AS (
                SELECT cand.subject_id, cand.subject_type,
                       (cand.subject_id IN (SELECT subject_id FROM revealed_ids)) AS is_revealed
                FROM candidates cand
                WHERE cand.subject_id IN (SELECT subject_id FROM revealed_ids)
                   OR (
                        NOT {str(all_evidence).upper()}
                        AND (
                            EXISTS (SELECT 1 FROM counted_claims cc WHERE cc.subject_id = cand.subject_id)
                            OR EXISTS (SELECT 1 FROM endpoint_claims ec WHERE ec.subject_id = cand.subject_id AND {vis_ec})
                        )
                   )
                   OR (
                        {str(all_evidence).upper()}
                        AND (
                            EXISTS (SELECT 1 FROM claims c2 WHERE c2.fact_id = cand.subject_id)
                            OR EXISTS (SELECT 1 FROM endpoint_claims ec WHERE ec.subject_id = cand.subject_id)
                        )
                        AND NOT EXISTS (
                            SELECT 1 FROM claims c3
                            WHERE c3.fact_id = cand.subject_id AND NOT ({self._visibility_predicate("c3.corpus_id", is_admin)})
                        )
                        AND NOT EXISTS (
                            SELECT 1 FROM endpoint_claims ec WHERE ec.subject_id = cand.subject_id AND NOT ({vis_ec})
                        )
                   )
            ),
            target_ids AS (
                SELECT subject_id FROM visible
            ),
            {self._projection_cte_sql(with_aliases=True)}
            SELECT v.subject_id, v.subject_type, v.is_revealed,
                   COALESCE(cnt.claim_count, 0) AS claim_count,
                   COALESCE(al.aliases, '[]'::jsonb) AS aliases,
                   COALESCE(sa.attrs, '{{}}'::jsonb) AS attrs
            FROM visible v
            LEFT JOIN counts cnt ON cnt.subject_id = v.subject_id
            LEFT JOIN aliases al ON al.subject_id = v.subject_id
            LEFT JOIN subject_attrs sa ON sa.subject_id = v.subject_id
            WHERE TRUE {filter_sql}
            ORDER BY v.subject_id
            LIMIT :limit_plus_one
            """
        )
        with self._engine.connect() as conn:
            rows = conn.execute(sql, params).mappings().all()

        limit_applied = len(rows) > limit
        rows = rows[:limit]

        subjects = []
        for r in rows:
            claim_count = int(r["claim_count"])
            is_revealed = bool(r["is_revealed"])
            subjects.append(
                {
                    "id": r["subject_id"],
                    "type": r["subject_type"],
                    "aliases": _decode_jsonb(r["aliases"]) or [],
                    "attrs": _decode_jsonb(r["attrs"]) or {},
                    "claim_count": claim_count,
                    "quote_count": 0 if is_revealed else claim_count,
                    "revealed": is_revealed,
                }
            )
        return {"subjects": subjects, "limit_applied": limit_applied}

    # ------------------------------------------------------------------
    # neighbors
    # ------------------------------------------------------------------

    def neighbors(
        self,
        caller,
        subject_id: str,
        *,
        edge_types: Optional[List[str]] = None,
        depth: int = 1,
        fanout: int = MAX_NEIGHBORS_FANOUT,
        limit: int = MAX_NEIGHBORS_RESULT,
    ) -> Dict[str, Any]:
        depth = max(1, min(depth, MAX_NEIGHBORS_DEPTH))
        fanout = max(1, min(fanout, MAX_NEIGHBORS_FANOUT))
        limit = max(1, min(limit, MAX_NEIGHBORS_RESULT))

        readable = _readable_ids(caller)
        is_admin = readable is None
        all_evidence = _visibility_mode() == "all_evidence"

        with self._engine.begin() as conn:
            conn.execute(sa.text(f"SET LOCAL statement_timeout = {_STATEMENT_TIMEOUT_MS}"))

            root_status = self._subject_status(
                conn,
                subject_kind="fact",
                subject_id=subject_id,
                is_admin=is_admin,
                all_evidence=all_evidence,
                readable=readable,
            )
            if not self._is_visible(root_status):
                raise FactNotFound(subject_id)

            root_row = (
                conn.execute(sa.text("SELECT id, type FROM facts WHERE id = :id"), {"id": subject_id})
                .mappings()
                .first()
            )
            if root_row is None:
                raise FactNotFound(subject_id)

            nodes: Dict[str, Dict[str, Any]] = {
                subject_id: {"id": root_row["id"], "type": root_row["type"], "revealed": bool(root_status["revealed"])}
            }
            edges_out: List[Dict[str, Any]] = []
            edges_seen: set = set()
            edges_revealed: Dict[str, bool] = {}
            visited = {subject_id}
            frontier = {subject_id}
            truncated = {"depth": False, "fanout": False, "result": False}

            vis = self._visibility_predicate("c.corpus_id", is_admin)
            edge_types_clause = "AND e.type = ANY(:edge_types)" if edge_types else ""

            for _hop in range(depth):
                if not frontier:
                    break
                if len(nodes) + len(edges_out) >= limit:
                    truncated["result"] = True
                    break
                next_frontier: set = set()
                for node_id in sorted(frontier):
                    if len(nodes) + len(edges_out) >= limit:
                        truncated["result"] = True
                        break
                    edge_sql = sa.text(
                        f"""
                        SELECT e.id, e.src, e.dst, e.type,
                               EXISTS (
                                 SELECT 1 FROM corrections co2 WHERE co2.subject_kind = 'edge'
                                   AND co2.subject_id = e.id AND co2.verdict = 'revealed'
                               ) AS revealed
                        FROM edges e
                        WHERE (e.src = :node_id OR e.dst = :node_id)
                          {edge_types_clause}
                          AND NOT EXISTS (
                            SELECT 1 FROM corrections co WHERE co.subject_kind = 'edge' AND co.subject_id = e.id
                              AND co.verdict IN ('wrong', 'restricted')
                          )
                          AND (
                            EXISTS (
                              SELECT 1 FROM corrections co WHERE co.subject_kind = 'edge' AND co.subject_id = e.id
                                AND co.verdict = 'revealed'
                            )
                            OR EXISTS (SELECT 1 FROM claims c WHERE c.edge_id = e.id AND {vis})
                          )
                        ORDER BY e.id
                        LIMIT :fanout_plus_one
                        """
                    )
                    params: Dict[str, Any] = {"node_id": node_id, "fanout_plus_one": fanout + 1}
                    if not is_admin:
                        params["readable"] = list(readable)
                    if edge_types:
                        params["edge_types"] = list(edge_types)
                    edge_rows = conn.execute(edge_sql, params).mappings().all()
                    if len(edge_rows) > fanout:
                        truncated["fanout"] = True
                        edge_rows = edge_rows[:fanout]

                    for erow in edge_rows:
                        other_id = erow["dst"] if erow["src"] == node_id else erow["src"]
                        if other_id not in nodes:
                            other_status = self._subject_status(
                                conn,
                                subject_kind="fact",
                                subject_id=other_id,
                                is_admin=is_admin,
                                all_evidence=all_evidence,
                                readable=readable,
                            )
                            if not self._is_visible(other_status):
                                # traversal does not tunnel (§5 rule 3, S4):
                                # the endpoint is invisible -> skip the edge
                                # entirely, never reveal the path continues.
                                continue
                            other_row = (
                                conn.execute(sa.text("SELECT id, type FROM facts WHERE id = :id"), {"id": other_id})
                                .mappings()
                                .first()
                            )
                            if other_row is None:
                                continue
                            nodes[other_id] = {
                                "id": other_row["id"],
                                "type": other_row["type"],
                                "revealed": bool(other_status["revealed"]),
                            }
                        if erow["id"] not in edges_seen:
                            edges_seen.add(erow["id"])
                            edges_revealed[erow["id"]] = bool(erow["revealed"])
                            edges_out.append(
                                {"id": erow["id"], "src": erow["src"], "dst": erow["dst"], "type": erow["type"]}
                            )
                        if other_id not in visited:
                            next_frontier.add(other_id)
                        if len(nodes) + len(edges_out) >= limit:
                            truncated["result"] = True
                            break
                    if len(nodes) + len(edges_out) >= limit:
                        break
                visited |= next_frontier
                frontier = next_frontier
            else:
                # The loop ran all `depth` hops without an early `break` —
                # only then is "we stopped because depth ran out" even a
                # candidate explanation. Check whether the final frontier
                # actually has any further visible edge before flagging
                # truncation: merely discovering nodes at the last hop does
                # NOT mean the graph continues past them (a leaf reached
                # exactly at the depth ceiling must not be reported as cut
                # off).
                if frontier and not truncated["result"]:
                    seen_clause = "AND e.id != ALL(:seen) " if edges_seen else ""
                    check_sql = sa.text(
                        f"""
                        SELECT 1 FROM edges e
                        WHERE (e.src = ANY(:ids) OR e.dst = ANY(:ids))
                          {seen_clause}
                          AND NOT EXISTS (
                            SELECT 1 FROM corrections co WHERE co.subject_kind = 'edge' AND co.subject_id = e.id
                              AND co.verdict IN ('wrong', 'restricted')
                          )
                          AND (
                            EXISTS (
                              SELECT 1 FROM corrections co WHERE co.subject_kind = 'edge' AND co.subject_id = e.id
                                AND co.verdict = 'revealed'
                            )
                            OR EXISTS (SELECT 1 FROM claims c WHERE c.edge_id = e.id AND {vis})
                          )
                        LIMIT 1
                        """
                    )
                    check_params: Dict[str, Any] = {"ids": list(frontier)}
                    if edges_seen:
                        check_params["seen"] = list(edges_seen)
                    if not is_admin:
                        check_params["readable"] = list(readable)
                    more = conn.execute(check_sql, check_params).first()
                    truncated["depth"] = more is not None

            # Project attrs/aliases/claim_count for the FINAL, already-
            # truncated node/edge sets only (spec §12 cost note: "project
            # AFTER truncation") — never the wider candidate set a deeper
            # walk might have touched. Same shared projection `search()`
            # uses, so a node/edge here carries byte-identical `attrs` to
            # a `search()` subject (spec §12).
            node_proj = self._project_subjects(
                conn,
                kind="fact",
                ids_with_revealed=[(n["id"], n["revealed"]) for n in nodes.values()],
                is_admin=is_admin,
                readable=readable,
                with_aliases=True,
            )
            edge_proj = self._project_subjects(
                conn,
                kind="edge",
                ids_with_revealed=[(eid, edges_revealed.get(eid, False)) for eid in edges_seen],
                is_admin=is_admin,
                readable=readable,
                with_aliases=False,
            )

        nodes_out = []
        for n in nodes.values():
            proj = node_proj.get(n["id"], {"aliases": [], "attrs": {}, "claim_count": 0})
            claim_count = proj["claim_count"]
            nodes_out.append(
                {
                    "id": n["id"],
                    "type": n["type"],
                    "aliases": proj["aliases"],
                    "attrs": proj["attrs"],
                    "claim_count": claim_count,
                    "quote_count": 0 if n["revealed"] else claim_count,
                    "revealed": n["revealed"],
                }
            )
        for e in edges_out:
            e["attrs"] = edge_proj.get(e["id"], {"attrs": {}})["attrs"]

        return {
            "nodes": nodes_out,
            "edges": edges_out,
            "truncated": truncated,
        }

    # ------------------------------------------------------------------
    # claims
    # ------------------------------------------------------------------

    def claims(self, caller, subject_id: str) -> Dict[str, Any]:
        """List a subject's OWN claims. The visibility GATE (below, via
        `_subject_status`) uses the endpoint-evidence union for a fact
        subject, so a visible endpoint-only fact (zero own claims, visible
        only via an incident edge's claim) returns `{"claims": [], ...}`
        (200) rather than a 404 — the claims LIST itself stays own-claims
        only, same as `search()`'s attrs projection."""
        readable = _readable_ids(caller)
        is_admin = readable is None
        all_evidence = _visibility_mode() == "all_evidence"

        with self._engine.connect() as conn:
            kind = self._subject_kind(conn, subject_id)
            if kind is None:
                raise FactNotFound(subject_id)

            status = self._subject_status(
                conn,
                subject_kind=kind,
                subject_id=subject_id,
                is_admin=is_admin,
                all_evidence=all_evidence,
                readable=readable,
            )
            if not self._is_visible(status):
                raise FactNotFound(subject_id)

            is_revealed = bool(status["revealed"])
            kind_column = "fact_id" if kind == "fact" else "edge_id"
            vis = self._visibility_predicate("c.corpus_id", is_admin)
            clause = "TRUE" if is_revealed else vis
            sql = sa.text(
                f"""
                SELECT c.id, c.corpus_id, c.corpus_file_id, c.quote, c.attrs, c.document_date,
                       cf.filename, cf.path, cfs.source_url
                FROM claims c
                JOIN corpus_files cf ON cf.id = c.corpus_file_id
                LEFT JOIN corpus_file_sources cfs ON cfs.corpus_file_id = c.corpus_file_id
                WHERE c.{kind_column} = :subject_id AND ({clause})
                ORDER BY c.id
                """
            )
            params: Dict[str, Any] = {"subject_id": subject_id}
            if not is_admin and not is_revealed:
                params["readable"] = list(readable)
            rows = conn.execute(sql, params).mappings().all()

        out = []
        for r in rows:
            # A `revealed` correction reveals the FACT, not the geography of
            # its evidence (spec §4, review tightening 2026-08-28): for a
            # claim whose collection the caller cannot read, the document's
            # human identity (name/path/URL) is withheld — opaque ids only.
            claim_readable = is_admin or r["corpus_id"] in readable
            document: Optional[Dict[str, Any]]
            if claim_readable or not is_revealed:
                document = {"name": r["filename"], "path": r["path"]}
                if r.get("source_url"):
                    document["source_url"] = r["source_url"]
            else:
                document = None
            out.append(
                {
                    "id": r["id"],
                    "corpus_id": r["corpus_id"],
                    "corpus_file_id": r["corpus_file_id"],
                    "document": document,
                    "quote": "" if is_revealed else r["quote"],
                    "attrs": _decode_jsonb(r["attrs"]) or {},
                    "document_date": r["document_date"].isoformat() if r["document_date"] else None,
                }
            )
        return {"claims": out, "revealed": is_revealed}

    def _subject_kind(self, conn, subject_id: str) -> Optional[str]:
        row = conn.execute(sa.text("SELECT 1 FROM facts WHERE id = :id"), {"id": subject_id}).first()
        if row is not None:
            return "fact"
        row = conn.execute(sa.text("SELECT 1 FROM edges WHERE id = :id"), {"id": subject_id}).first()
        if row is not None:
            return "edge"
        return None

    # ------------------------------------------------------------------
    # collection-scoped summaries — spec §13.2 "Surfaces": the Library
    # collection card's "N files · M facts" and the collection-detail facts
    # section. Both are ADDITIONS to this frozen-app-state pair's read
    # surface (the A3 ratchet only forbids a brand-new DuckDB module — an
    # extra method on an existing PG-only repo is unaffected), and both
    # funnel through the SAME `_visible_facts_for_corpus` CTE, so a caller
    # can never see a bigger "M" on the card than the facts section it opens
    # into actually lists.
    # ------------------------------------------------------------------

    def _visible_facts_for_corpus_cte(self, is_admin: bool, all_evidence: bool) -> str:
        """SQL for a ``visible(subject_id, is_revealed)`` CTE: facts with at
        least one OWN claim evidenced by ``:corpus_id`` (bound by the
        caller — candidacy is deliberately still OWN-claims-only: "evidenced
        by this collection" names a claim IN it, not a fact reachable only
        via an edge whose claim happens to live elsewhere), withheld ones
        (`wrong`/`restricted`) excluded, `revealed` ones included
        unconditionally, and everything else gated by the SAME
        any_evidence/all_evidence rule `search()` applies — over the
        subject's claims EVERYWHERE (own OR a non-withheld incident edge's,
        module docstring "Endpoint evidence"), not just this corpus, so a
        candidate whose only OWN claim here is itself unreadable can still
        be revealed by a readable incident-edge claim. Returned as a
        fragment (no leading ``WITH``) so callers can embed it beside their
        own CTEs; every caller must bind ``:corpus_id`` and, when
        ``is_admin`` is False, ``:readable``."""
        vis = self._visibility_predicate("c2.corpus_id", is_admin)
        vis3 = self._visibility_predicate("c3.corpus_id", is_admin)
        vis_ec = self._visibility_predicate("ec.corpus_id", is_admin)
        all_ev_sql = "TRUE" if all_evidence else "FALSE"
        return f"""
            candidates AS (
                SELECT DISTINCT c.fact_id AS subject_id
                FROM claims c
                WHERE c.corpus_id = :corpus_id AND c.fact_id IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM corrections co
                    WHERE co.subject_kind = 'fact' AND co.subject_id = c.fact_id
                      AND co.verdict IN ('wrong', 'restricted')
                  )
            ),
            revealed_ids AS (
                SELECT subject_id FROM corrections WHERE subject_kind = 'fact' AND verdict = 'revealed'
            ),
            endpoint_claims AS (
                SELECT DISTINCT cand.subject_id AS subject_id, c.corpus_id AS corpus_id
                FROM candidates cand
                JOIN edges e ON (e.src = cand.subject_id OR e.dst = cand.subject_id)
                JOIN claims c ON c.edge_id = e.id
                WHERE NOT EXISTS (
                    SELECT 1 FROM corrections co
                    WHERE co.subject_kind = 'edge' AND co.subject_id = e.id
                      AND co.verdict IN ('wrong', 'restricted')
                )
            ),
            visible AS (
                SELECT cand.subject_id,
                       (cand.subject_id IN (SELECT subject_id FROM revealed_ids)) AS is_revealed
                FROM candidates cand
                WHERE cand.subject_id IN (SELECT subject_id FROM revealed_ids)
                   OR (
                        NOT {all_ev_sql}
                        AND (
                            EXISTS (SELECT 1 FROM claims c2 WHERE c2.fact_id = cand.subject_id AND {vis})
                            OR EXISTS (SELECT 1 FROM endpoint_claims ec WHERE ec.subject_id = cand.subject_id AND {vis_ec})
                        )
                   )
                   OR (
                        {all_ev_sql}
                        AND (
                            EXISTS (SELECT 1 FROM claims c2 WHERE c2.fact_id = cand.subject_id)
                            OR EXISTS (SELECT 1 FROM endpoint_claims ec WHERE ec.subject_id = cand.subject_id)
                        )
                        AND NOT EXISTS (
                            SELECT 1 FROM claims c3
                            WHERE c3.fact_id = cand.subject_id AND NOT ({vis3})
                        )
                        AND NOT EXISTS (
                            SELECT 1 FROM endpoint_claims ec WHERE ec.subject_id = cand.subject_id AND NOT ({vis_ec})
                        )
                   )
            )
            """

    def count_visible_facts_for_collection(self, caller, corpus_id: str) -> int:
        """Caller-scoped count of facts evidenced by ``corpus_id`` — the
        Library collection card's "M facts" number (spec §13.2 "Library").
        Cheap: one indexed-`corpus_id` query, no traversal. Two callers with
        different grants on the SAME collection can see a different M: under
        `all_evidence` mode a fact whose OTHER claims live in a collection
        one caller cannot reach is hidden from them but visible to a caller
        who can reach every collection it is evidenced from; a caller with no
        access to `corpus_id` at all (not gated here — the caller of this
        method is expected to have already confirmed collection access)
        would still see only `revealed` subjects, if any."""
        return self.count_visible_facts_for_collections(caller, [corpus_id]).get(corpus_id, 0)

    def count_visible_facts_for_collections(self, caller, corpus_ids: List[str]) -> Dict[str, int]:
        """Batch form of :meth:`count_visible_facts_for_collection` — same
        per-collection numbers, but the caller's readable set is resolved
        **once** for the whole batch.

        That resolution is the expensive half. ``_readable_ids`` delegates to
        ``accessible_collection_ids``, which for a non-admin runs a grants
        query plus a full owned-collections scan; calling the singular method
        in a loop (the Library page renders one card per collection) repeated
        that scan per card, so the page cost grew with the number of
        collections the caller can see. The count query itself is one
        indexed-``corpus_id`` lookup and stays per collection, now over a
        single shared connection.

        Visibility still resolves entirely in here — callers pass ids, never
        a precomputed readable set, so no route can widen what it sees
        (spec §5).
        """
        readable = _readable_ids(caller)
        is_admin = readable is None
        all_evidence = _visibility_mode() == "all_evidence"
        base: Dict[str, Any] = {} if is_admin else {"readable": list(readable)}
        sql = sa.text(f"WITH {self._visible_facts_for_corpus_cte(is_admin, all_evidence)} SELECT COUNT(*) FROM visible")

        out: Dict[str, int] = {}
        if not corpus_ids:
            return out
        with self._engine.connect() as conn:
            for corpus_id in corpus_ids:
                row = conn.execute(sql, {**base, "corpus_id": corpus_id}).first()
                out[corpus_id] = int(row[0]) if row else 0
        return out

    def _visible_edges_for_corpus_cte(self, is_admin: bool) -> str:
        """SQL for an ``edge_visible(subject_id)`` CTE over EDGES evidenced
        by ``:corpus_id`` — the edge analogue of
        :meth:`_visible_facts_for_corpus_cte`, used only by
        :meth:`count_visible_edges_for_collections` (the source card's
        pipeline-strip "edges" number, spec §13.2). Unlike a fact, an edge's
        own claim IS its only evidence path — there is no endpoint-claim
        fallback — so this candidacy/visibility rule is simpler: any_evidence
        semantics only (matching :meth:`neighbors`'s own edge-visibility
        rule — an edge needs its OWN readable claim, never inferred from its
        endpoints), withheld (``wrong``/``restricted``) edges excluded,
        ``revealed`` ones included unconditionally. Returned as a fragment
        (no leading ``WITH``); every caller must bind ``:corpus_id`` and,
        when ``is_admin`` is False, ``:readable``."""
        vis = self._visibility_predicate("c2.corpus_id", is_admin)
        return f"""
            edge_candidates AS (
                SELECT DISTINCT c.edge_id AS subject_id
                FROM claims c
                WHERE c.corpus_id = :corpus_id AND c.edge_id IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM corrections co
                    WHERE co.subject_kind = 'edge' AND co.subject_id = c.edge_id
                      AND co.verdict IN ('wrong', 'restricted')
                  )
            ),
            edge_revealed_ids AS (
                SELECT subject_id FROM corrections WHERE subject_kind = 'edge' AND verdict = 'revealed'
            ),
            edge_visible AS (
                SELECT cand.subject_id
                FROM edge_candidates cand
                WHERE cand.subject_id IN (SELECT subject_id FROM edge_revealed_ids)
                   OR EXISTS (SELECT 1 FROM claims c2 WHERE c2.edge_id = cand.subject_id AND {vis})
            )
            """

    def count_visible_edges_for_collections(self, caller, corpus_ids: List[str]) -> Dict[str, int]:
        """Caller-scoped edge count per collection — added alongside
        :meth:`count_visible_facts_for_collections` for the source card's
        pipeline-strip "edges" number (spec §13.2); there was no edge
        equivalent of that fact counter yet. Same batch-resolves-readable-
        once shape."""
        readable = _readable_ids(caller)
        is_admin = readable is None
        base: Dict[str, Any] = {} if is_admin else {"readable": list(readable)}
        sql = sa.text(f"WITH {self._visible_edges_for_corpus_cte(is_admin)} SELECT COUNT(*) FROM edge_visible")

        out: Dict[str, int] = {}
        if not corpus_ids:
            return out
        with self._engine.connect() as conn:
            for corpus_id in corpus_ids:
                row = conn.execute(sql, {**base, "corpus_id": corpus_id}).first()
                out[corpus_id] = int(row[0]) if row else 0
        return out

    def collection_facts_summary(self, caller, corpus_id: str, *, limit: int = 20, offset: int = 0) -> Dict[str, Any]:
        """Caller-scoped facts section for one collection's detail page
        (spec §13.2 "Collection detail"): fact count by type, a paged list of
        facts evidenced by ``corpus_id`` (display name from the first
        natural-key alias, GLOBAL claim/quote counts matching `search()`'s
        contract so the same subject reads the same numbers everywhere), a
        same-key attribute conflict rendered INLINE on the fact that carries
        it (never a detached queue — every conflicting value with its
        document name/date, from the caller's own readable claims), and
        `possible_duplicate_of` review-item edges whose own claim AND both
        endpoints are independently visible to the caller (the same rule
        `neighbors()` applies to every edge — never inferred from an
        endpoint), and `single_valued_conflict` review items (spec §7.3) for
        a functionally single-valued edge type (`facts.single_valued_edges`)
        whose src fact carries >1 distinct dst with an independently
        visible claim — every item in ``review_items`` carries a ``kind``
        discriminator so the two shapes coexist in one list.

        Honesty note on "conflict": this is a SIMPLER definition than
        `search()`'s attrs projection (which shows a conflict only when the
        LATEST document_date ties across values) — here, every distinct
        value a caller can read for one attribute key is a conflict, because
        this surface's job is showing a reader the disagreement, not picking
        a winner. It intentionally surfaces more than the projection would.
        """
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        readable = _readable_ids(caller)
        is_admin = readable is None
        all_evidence = _visibility_mode() == "all_evidence"
        vis_params: Dict[str, Any] = {"corpus_id": corpus_id}
        if not is_admin:
            vis_params["readable"] = list(readable)
        cte = self._visible_facts_for_corpus_cte(is_admin, all_evidence)

        with self._engine.connect() as conn:
            type_sql = sa.text(
                f"WITH {cte} "
                "SELECT f.type, COUNT(*) AS n FROM visible v JOIN facts f ON f.id = v.subject_id "
                "GROUP BY f.type ORDER BY f.type"
            )
            type_rows = conn.execute(type_sql, vis_params).mappings().all()
            type_counts = {r["type"]: int(r["n"]) for r in type_rows}
            total = sum(type_counts.values())

            page_sql = sa.text(
                f"WITH {cte} "
                "SELECT v.subject_id, v.is_revealed, f.type FROM visible v JOIN facts f ON f.id = v.subject_id "
                "ORDER BY f.type, v.subject_id LIMIT :limit_plus_one OFFSET :offset"
            )
            page_params = dict(vis_params)
            page_params["limit_plus_one"] = limit + 1
            page_params["offset"] = offset
            page_rows = conn.execute(page_sql, page_params).mappings().all()
            limit_applied = len(page_rows) > limit
            page_rows = page_rows[:limit]
            page_ids = [r["subject_id"] for r in page_rows]
            revealed_by_id = {r["subject_id"]: bool(r["is_revealed"]) for r in page_rows}
            type_by_id = {r["subject_id"]: r["type"] for r in page_rows}

            facts_out: List[Dict[str, Any]] = []
            review_items: List[Dict[str, Any]] = []

            if page_ids:
                alias_sql = sa.text(
                    "SELECT fact_id, natural_key FROM fact_aliases WHERE fact_id = ANY(:ids) "
                    "ORDER BY fact_id, natural_key"
                )
                alias_rows = conn.execute(alias_sql, {"ids": page_ids}).mappings().all()
                display_name: Dict[str, str] = {}
                for r in alias_rows:
                    display_name.setdefault(r["fact_id"], r["natural_key"])

                # GLOBAL readable claims for the page's subjects — same rule
                # search()'s counted_claims applies: a revealed subject counts
                # every claim regardless of grants (quotes suppressed instead,
                # not the count), everyone else counts only grant-visible ones.
                claim_vis = self._visibility_predicate("c.corpus_id", is_admin)
                revealed_ids = [fid for fid, rev in revealed_by_id.items() if rev]
                claims_sql = sa.text(
                    f"""
                    SELECT c.fact_id, c.attrs, c.document_date, cf.filename
                    FROM claims c
                    JOIN corpus_files cf ON cf.id = c.corpus_file_id
                    WHERE c.fact_id = ANY(:ids) AND (c.fact_id = ANY(:revealed_ids) OR ({claim_vis}))
                    """
                )
                claims_params: Dict[str, Any] = {"ids": page_ids, "revealed_ids": revealed_ids}
                if not is_admin:
                    claims_params["readable"] = list(readable)
                claim_rows = conn.execute(claims_sql, claims_params).mappings().all()

                claim_count: Dict[str, int] = {}
                # subject_id -> attr key -> value's json key -> {"value", "docs": {(name, date), ...}}
                attr_values: Dict[str, Dict[str, Dict[str, Dict[str, Any]]]] = {}
                for r in claim_rows:
                    fid = r["fact_id"]
                    claim_count[fid] = claim_count.get(fid, 0) + 1
                    attrs = _decode_jsonb(r["attrs"]) or {}
                    doc_date = r["document_date"].isoformat() if r["document_date"] else None
                    for key, value in attrs.items():
                        value_key = json.dumps(value, sort_keys=True)
                        bucket = attr_values.setdefault(fid, {}).setdefault(key, {})
                        entry = bucket.setdefault(value_key, {"value": value, "docs": set()})
                        entry["docs"].add((r["filename"], doc_date))

                for fid in page_ids:
                    is_revealed = revealed_by_id.get(fid, False)
                    n_claims = claim_count.get(fid, 0)
                    conflicts = []
                    for key, values in (attr_values.get(fid) or {}).items():
                        if len(values) < 2:
                            continue
                        entries = []
                        for entry in values.values():
                            for doc_name, doc_date in sorted(entry["docs"], key=lambda t: (t[0] or "", t[1] or "")):
                                entries.append(
                                    {"value": entry["value"], "document_name": doc_name, "document_date": doc_date}
                                )
                        conflicts.append({"key": key, "entries": entries})
                    facts_out.append(
                        {
                            "id": fid,
                            "type": type_by_id.get(fid),
                            "display_name": display_name.get(fid, fid),
                            "claim_count": n_claims,
                            "quote_count": 0 if is_revealed else n_claims,
                            "revealed": is_revealed,
                            "conflicts": conflicts,
                        }
                    )

                review_items = self._review_items_for_corpus(
                    conn, corpus_id=corpus_id, is_admin=is_admin, all_evidence=all_evidence, readable=readable
                )
                review_items += self._single_valued_review_items_for_corpus(
                    conn, corpus_id=corpus_id, is_admin=is_admin, all_evidence=all_evidence, readable=readable
                )

        return {
            "total": total,
            "type_counts": type_counts,
            "facts": facts_out,
            "limit_applied": limit_applied,
            "review_items": review_items,
        }

    def _review_items_for_corpus(
        self, conn, *, corpus_id: str, is_admin: bool, all_evidence: bool, readable: Optional[frozenset]
    ) -> List[Dict[str, Any]]:
        """`possible_duplicate_of` edges touching a fact evidenced by
        ``corpus_id`` — spec §7.2's entity-resolution review items, surfaced
        as rows with both subjects named rather than a detached queue. Every
        edge is independently visibility-checked (its OWN claim, never
        inferred from its endpoints — the same rule S3/S4 pin for
        `neighbors()`), and both endpoints must be independently visible too,
        so this can never announce a fact the caller cannot otherwise see.
        Capped at 50 candidate edges — a review surface, not a full scan."""
        cte = self._visible_facts_for_corpus_cte(is_admin, all_evidence)
        vis_params: Dict[str, Any] = {"corpus_id": corpus_id}
        if not is_admin:
            vis_params["readable"] = list(readable)
        edge_sql = sa.text(
            f"""
            WITH {cte}
            SELECT DISTINCT e.id, e.src, e.dst
            FROM edges e
            WHERE e.type = 'possible_duplicate_of'
              AND (e.src IN (SELECT subject_id FROM visible) OR e.dst IN (SELECT subject_id FROM visible))
            ORDER BY e.id
            LIMIT 50
            """
        )
        edge_rows = conn.execute(edge_sql, vis_params).mappings().all()
        out: List[Dict[str, Any]] = []
        for erow in edge_rows:
            eid, src, dst = erow["id"], erow["src"], erow["dst"]
            edge_status = self._subject_status(
                conn,
                subject_kind="edge",
                subject_id=eid,
                is_admin=is_admin,
                all_evidence=all_evidence,
                readable=readable,
            )
            if not self._is_visible(edge_status):
                continue
            a_status = self._subject_status(
                conn,
                subject_kind="fact",
                subject_id=src,
                is_admin=is_admin,
                all_evidence=all_evidence,
                readable=readable,
            )
            b_status = self._subject_status(
                conn,
                subject_kind="fact",
                subject_id=dst,
                is_admin=is_admin,
                all_evidence=all_evidence,
                readable=readable,
            )
            if not (self._is_visible(a_status) and self._is_visible(b_status)):
                continue
            out.append(
                {
                    "kind": "possible_duplicate_of",
                    "edge_id": eid,
                    "a": self._subject_label(conn, src),
                    "b": self._subject_label(conn, dst),
                }
            )
        return out

    def _single_valued_review_items_for_corpus(
        self, conn, *, corpus_id: str, is_admin: bool, all_evidence: bool, readable: Optional[frozenset]
    ) -> List[Dict[str, Any]]:
        """Functionally single-valued edges (spec §7.3) whose src fact is
        evidenced by ``corpus_id``: >1 distinct dst, each carrying its OWN
        independently-visible claim (the exact discipline
        `_review_items_for_corpus` applies to `possible_duplicate_of` — an
        edge's own claim AND its dst endpoint must each pass
        `_subject_status`/`_is_visible`), for an edge type this instance
        configured as single-valued (`facts.single_valued_edges`). A dst
        the caller cannot independently see is dropped from the group
        rather than hiding the whole conflict — but if that drops the
        group back to <=1 visible dst, the item disappears entirely (never
        announces a conflict whose second edge the caller cannot read).
        Recomputed on every call from live edges/claims, nothing persisted,
        so it clears the moment a dst's claims are gone. Candidate (src,
        type) pairs capped at 50 — a review surface, not a full scan."""
        types = _single_valued_edge_types()
        if not types:
            return []
        cte = self._visible_facts_for_corpus_cte(is_admin, all_evidence)
        vis_params: Dict[str, Any] = {"corpus_id": corpus_id, "types": list(types)}
        if not is_admin:
            vis_params["readable"] = list(readable)
        # Step 1: candidate (src, type) pairs with RAW distinct-dst count >1
        # among live (>=1 claim) edges — cheap pre-filter before the
        # per-edge visibility walk below.
        candidate_sql = sa.text(
            f"""
            WITH {cte}
            SELECT e.src, e.type
            FROM edges e
            WHERE e.type = ANY(:types)
              AND e.src IN (SELECT subject_id FROM visible)
              AND EXISTS (SELECT 1 FROM claims c WHERE c.edge_id = e.id)
            GROUP BY e.src, e.type
            HAVING COUNT(DISTINCT e.dst) > 1
            ORDER BY e.src, e.type
            LIMIT 50
            """
        )
        candidates = conn.execute(candidate_sql, vis_params).mappings().all()
        out: List[Dict[str, Any]] = []
        for cand in candidates:
            src, etype = cand["src"], cand["type"]
            edge_rows = (
                conn.execute(
                    sa.text("SELECT id, dst FROM edges WHERE src = :src AND type = :type"),
                    {"src": src, "type": etype},
                )
                .mappings()
                .all()
            )
            visible_dsts: set = set()
            for erow in edge_rows:
                eid, dst = erow["id"], erow["dst"]
                edge_status = self._subject_status(
                    conn,
                    subject_kind="edge",
                    subject_id=eid,
                    is_admin=is_admin,
                    all_evidence=all_evidence,
                    readable=readable,
                )
                if not self._is_visible(edge_status):
                    continue
                dst_status = self._subject_status(
                    conn,
                    subject_kind="fact",
                    subject_id=dst,
                    is_admin=is_admin,
                    all_evidence=all_evidence,
                    readable=readable,
                )
                if not self._is_visible(dst_status):
                    continue
                visible_dsts.add(dst)
            if len(visible_dsts) < 2:
                continue
            out.append(
                {
                    "kind": "single_valued_conflict",
                    "type": etype,
                    "src": self._subject_label(conn, src),
                    "dsts": [self._subject_label(conn, dst) for dst in sorted(visible_dsts)],
                }
            )
        return out

    def _subject_label(self, conn, fact_id: str) -> Dict[str, Any]:
        row = conn.execute(sa.text("SELECT id, type FROM facts WHERE id = :id"), {"id": fact_id}).mappings().first()
        alias_row = (
            conn.execute(
                sa.text("SELECT natural_key FROM fact_aliases WHERE fact_id = :id ORDER BY natural_key LIMIT 1"),
                {"id": fact_id},
            )
            .mappings()
            .first()
        )
        return {
            "id": fact_id,
            "type": row["type"] if row else None,
            "display_name": (alias_row["natural_key"] if alias_row else fact_id),
        }

    # ------------------------------------------------------------------
    # orphan sweep (spec §6) — deletes zero-claim subjects, counts them.
    # ------------------------------------------------------------------

    def sweep_orphans(self) -> int:
        """Delete every orphaned subject (fact or edge); return the count
        (spec §6, refined per the module docstring's "Endpoint evidence": an
        edge anchors its endpoints, so a fact is orphaned only when it has
        NEITHER an own claim NOR an incident edge carrying any claim).
        Correction-agnostic throughout — this is raw data hygiene, not a
        visibility check; a `wrong`/`restricted` edge with a live claim
        still anchors its endpoints here exactly like any other edge.

        Edges are swept FIRST: an edge with zero claims of its own is
        removed before the fact sweep runs. That ordering is what makes the
        fact predicate cheap — by the time the fact DELETE runs, every
        surviving edge carries >=1 claim (the edge sweep just removed every
        one that didn't), so "zero incident edges carrying any claims"
        collapses to "zero incident edges, period": ``NOT EXISTS (SELECT 1
        FROM edges e WHERE e.src = f.id OR e.dst = f.id)``. Deleting an
        orphaned FACT afterwards can then cascade (``ON DELETE CASCADE``)
        any edge still pointing at it even if THAT edge carried its own
        claims — an edge to a subject this design has garbage-collected
        cannot outlive it, so that knock-on cascade is not separately
        counted here. Safe to call unconditionally (a no-op when nothing is
        orphaned); callers decide when running it is warranted (post-ingest
        — replace mode can orphan a subject a document no longer mentions —
        and post file delete, spec §6's lifecycle table)."""
        with self._engine.begin() as conn:
            edge_ids = (
                conn.execute(
                    sa.text(
                        "DELETE FROM edges e WHERE NOT EXISTS "
                        "(SELECT 1 FROM claims c WHERE c.edge_id = e.id) RETURNING e.id"
                    )
                )
                .scalars()
                .all()
            )
            fact_ids = (
                conn.execute(
                    sa.text(
                        "DELETE FROM facts f WHERE NOT EXISTS "
                        "(SELECT 1 FROM claims c WHERE c.fact_id = f.id) "
                        "AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.src = f.id OR e.dst = f.id) "
                        "RETURNING f.id"
                    )
                )
                .scalars()
                .all()
            )
        return len(edge_ids) + len(fact_ids)

    # ------------------------------------------------------------------
    # corrections management support (spec §3/§4) — natural-key snapshots
    # for admin CRUD, and re-attachment for a subject recreated after
    # deletion (never a producer-writable path; see app/api/facts.py).
    # ------------------------------------------------------------------

    def natural_keys_for(self, subject_kind: str, subject_id: str) -> Any:
        """Snapshot a LIVE subject's natural keys for ``corrections.
        natural_keys`` (spec §3): for a fact, ``{"aliases": [...]}`` (every
        alias currently pointing at it); for an edge, ``[src_key, type,
        dst_key]`` using the first alias found for each endpoint (sorted,
        so the choice is deterministic when an endpoint carries more than
        one). Best-effort — an endpoint with no alias yet yields ``None``
        in its slot rather than raising; the correction is still recorded,
        it just cannot re-attach through that endpoint later."""
        with self._engine.connect() as conn:
            if subject_kind == "fact":
                rows = (
                    conn.execute(
                        sa.text("SELECT natural_key FROM fact_aliases WHERE fact_id = :id ORDER BY natural_key"),
                        {"id": subject_id},
                    )
                    .scalars()
                    .all()
                )
                return {"aliases": list(rows)}
            edge = (
                conn.execute(sa.text("SELECT src, type, dst FROM edges WHERE id = :id"), {"id": subject_id})
                .mappings()
                .first()
            )
            if edge is None:
                return [None, None, None]
            src_key = conn.execute(
                sa.text("SELECT natural_key FROM fact_aliases WHERE fact_id = :id ORDER BY natural_key LIMIT 1"),
                {"id": edge["src"]},
            ).scalar()
            dst_key = conn.execute(
                sa.text("SELECT natural_key FROM fact_aliases WHERE fact_id = :id ORDER BY natural_key LIMIT 1"),
                {"id": edge["dst"]},
            ).scalar()
            return [src_key, edge["type"], dst_key]

    def _reattach_correction(
        self, conn, *, subject_kind: str, subject_id: str, natural_key_probe: Any
    ) -> Optional[Dict[str, Any]]:
        """A subject deleted (all claims cascaded away) and later re-created
        under a NEW surrogate id re-attaches any orphaned correction whose
        snapshot overlaps this subject's natural key (spec §3: "a legal
        hold must not vanish because a document was briefly missing").
        Matches only a correction whose OWN ``subject_id`` no longer names
        a LIVE subject — never steals a correction still attached to a
        different, currently-live subject. Returns the repointed row (for
        the ingest run report's ``corrections_active``) or ``None``."""
        if subject_kind == "fact":
            probe_sql = "natural_keys @> CAST(:probe AS JSONB)"
            probe = json.dumps({"aliases": [natural_key_probe]})
            live_table = "facts"
        else:
            probe_sql = "natural_keys = CAST(:probe AS JSONB)"
            probe = json.dumps(natural_key_probe)
            live_table = "edges"

        row = (
            conn.execute(
                sa.text(
                    f"SELECT subject_id, verdict, reason FROM corrections "
                    f"WHERE subject_kind = :kind AND {probe_sql} "
                    f"AND subject_id != :new_id "
                    f"AND NOT EXISTS (SELECT 1 FROM {live_table} WHERE id = corrections.subject_id) "
                    f"LIMIT 1"
                ),
                {"kind": subject_kind, "probe": probe, "new_id": subject_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        old_subject_id = row["subject_id"]
        conn.execute(
            sa.text("UPDATE corrections SET subject_id = :new_id WHERE subject_kind = :kind AND subject_id = :old_id"),
            {"new_id": subject_id, "kind": subject_kind, "old_id": old_subject_id},
        )
        return {
            "subject_kind": subject_kind,
            "subject_id": subject_id,
            "verdict": row["verdict"],
            "reason": row["reason"],
        }

    def _resolve_alias(self, conn, node_id: str, declared_type: Optional[str]) -> Dict[str, Any]:
        """Resolve a producer node id ``<type>:<slug>`` to a fact_id via
        ``fact_aliases`` (spec §7.2): unknown -> new subject + alias (with
        correction re-attachment, above); a TYPE disagreement against an
        EXISTING alias for the same natural key is rejected, itemized
        (``alias_type_conflict``) rather than hard-exiting like the
        producer's own sandbox loader."""
        parts = node_id.split(":", 1) if node_id else []
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return {"fact_id": None, "created": False, "error": "malformed_node_id", "reattached": None}
        prefix_type = parts[0]
        resolved_type = declared_type or prefix_type

        existing = (
            conn.execute(
                sa.text("SELECT fact_id, type FROM fact_aliases WHERE natural_key = :nk"),
                {"nk": node_id},
            )
            .mappings()
            .first()
        )
        if existing is not None:
            if existing["type"] != resolved_type:
                return {"fact_id": None, "created": False, "error": "alias_type_conflict", "reattached": None}
            return {"fact_id": existing["fact_id"], "created": False, "error": None, "reattached": None}

        fact_id = self.create_fact(type=resolved_type, natural_key=node_id)
        with self._engine.begin() as reconn:
            reattached = self._reattach_correction(
                reconn, subject_kind="fact", subject_id=fact_id, natural_key_probe=node_id
            )
        return {"fact_id": fact_id, "created": True, "error": None, "reattached": reattached}

    # ------------------------------------------------------------------
    # ingest (spec §7.2, build order step 4) — the write path's single
    # entry point. Batch caps, doc_id resolution (§6), the verbatim gate
    # (§8), union/replace modes, alias/edge resolution, correction
    # re-attachment (§3), and the orphan sweep (§6) all happen here; the
    # return value IS the run report (§7.2's response shape).
    # ------------------------------------------------------------------

    def ingest_batch(
        self,
        *,
        documents: Optional[List[Dict[str, Any]]] = None,
        full_documents: Optional[List[str]] = None,
        nodes: Optional[List[Dict[str, Any]]] = None,
        edges: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        documents = documents or []
        full_documents = full_documents or []
        nodes = nodes or []
        edges = edges or []

        if len(documents) > MAX_INGEST_DOCUMENTS:
            raise IngestBatchTooLarge(
                {"reason": "too_many_documents", "count": len(documents), "cap": MAX_INGEST_DOCUMENTS}
            )

        per_doc_claims: Dict[str, int] = {}
        for node in nodes:
            for ev in node.get("evidence") or []:
                doc_id = ev.get("doc_id")
                if doc_id:
                    per_doc_claims[doc_id] = per_doc_claims.get(doc_id, 0) + 1
        for edge in edges:
            for ev in edge.get("evidence") or []:
                doc_id = ev.get("doc_id")
                if doc_id:
                    per_doc_claims[doc_id] = per_doc_claims.get(doc_id, 0) + 1

        for doc_id, count in per_doc_claims.items():
            if count > MAX_INGEST_CLAIMS:
                raise IngestDocumentExceedsClaimCap(doc_id, count)

        total_claims = sum(per_doc_claims.values())
        if total_claims > MAX_INGEST_CLAIMS:
            raise IngestBatchTooLarge({"reason": "too_many_claims", "count": total_claims, "cap": MAX_INGEST_CLAIMS})

        from src.repositories import corpus_chunks_repo, corpus_file_sources_repo, corpus_files_repo

        cf_repo = corpus_files_repo()
        sources_repo = corpus_file_sources_repo()
        chunks_repo = corpus_chunks_repo()

        # ---- documents[]: upsert through the §6 path (match stable_id then
        # path, refresh the corpus_file_sources mapping only) — NEVER
        # creates a new corpus_files row. This flow's content already
        # landed through the normal upload endpoint (spec §7.2, "what the
        # producer uploads"); a document row with no matching corpus_files
        # row stays unresolved.
        doc_id_resolution: Dict[str, str] = {}
        doc_dates: Dict[str, date] = {}
        with self._engine.connect() as doc_conn:
            for raw_doc in documents:
                doc = {k: v for k, v in raw_doc.items() if not k.startswith("_")}
                doc_id = doc.get("doc_id")
                corpus_id = doc.get("corpus_id")
                if not doc_id or not corpus_id:
                    continue
                stable_id = doc.get("stable_id") or None
                path = doc.get("path") or None

                existing = None
                if stable_id:
                    existing_id = sources_repo.resolve(corpus_id, stable_id)
                    if existing_id:
                        existing = cf_repo.get(existing_id)
                if existing is None and path:
                    existing = cf_repo.get_by_path(corpus_id, path)

                if existing is not None:
                    file_id = existing["id"]
                    if stable_id:
                        sources_repo.upsert(
                            corpus_file_id=file_id,
                            corpus_id=corpus_id,
                            source_stable_id=stable_id,
                            source_doc_id=doc_id,
                            source_sha256=doc.get("sha256") or None,
                        )
                    doc_id_resolution[doc_id] = file_id
                else:
                    # Neither stable_id nor path resolved a row directly —
                    # fall back to an ALREADY-ESTABLISHED source_doc_id
                    # mapping (a prior upload/ingest resolved this doc_id
                    # once already) so `modified` still attaches even
                    # though THIS row carries no fresh identity to match.
                    row = doc_conn.execute(
                        sa.text("SELECT corpus_file_id FROM corpus_file_sources WHERE source_doc_id = :doc_id LIMIT 1"),
                        {"doc_id": doc_id},
                    ).first()
                    file_id = row[0] if row is not None else None
                    if file_id is not None:
                        doc_id_resolution[doc_id] = file_id

                if file_id is not None:
                    parsed = _parse_document_date(doc.get("modified"))
                    if parsed is not None:
                        doc_dates[file_id] = parsed

        def _resolve_doc(doc_id: Optional[str], conn) -> Optional[str]:
            if not doc_id:
                return None
            if doc_id in doc_id_resolution:
                return doc_id_resolution[doc_id]
            row = conn.execute(
                sa.text("SELECT corpus_file_id FROM corpus_file_sources WHERE source_doc_id = :doc_id LIMIT 1"),
                {"doc_id": doc_id},
            ).first()
            if row is None:
                return None
            doc_id_resolution[doc_id] = row[0]
            return row[0]

        with self._engine.connect() as ro_conn:
            # Also resolve every `full_documents` id even when it carries NO
            # evidence in this batch at all — a lone "clear this document's
            # claims" replace-mode call (the deletion-driven cascade path,
            # not just a re-extraction) must still be able to find its
            # corpus_file_id.
            to_resolve = set(per_doc_claims) | set(full_documents)
            unresolved = {doc_id for doc_id in to_resolve if _resolve_doc(doc_id, ro_conn) is None}
        # §7.2: "documents may be omitted only when every referenced doc_id
        # already resolves; otherwise the batch is rejected with the
        # unresolved ids itemized." When `documents` WAS supplied but a
        # doc_id still doesn't resolve (content genuinely never uploaded),
        # that is a per-claim rejection below, not a whole-batch reject —
        # supplying the array is exactly the mechanism meant to establish
        # resolution, and it may legitimately fail for a subset.
        if not documents and unresolved:
            raise IngestUnresolvedDocIds(sorted(unresolved))

        # ---- full_documents replace mode: delete ALL existing claims for
        # each listed document BEFORE any incoming claim is written, so a
        # subject the re-extraction no longer mentions loses its stale
        # claim (spec §7.2, test C2).
        replaced_file_ids = {doc_id_resolution[d] for d in full_documents if d in doc_id_resolution}
        if replaced_file_ids:
            with self._engine.begin() as conn:
                conn.execute(
                    sa.text("DELETE FROM claims WHERE corpus_file_id = ANY(:ids)"),
                    {"ids": list(replaced_file_ids)},
                )

        file_row_cache: Dict[str, Optional[dict]] = {}

        def _file_row(file_id: str) -> Optional[dict]:
            if file_id not in file_row_cache:
                file_row_cache[file_id] = cf_repo.get(file_id)
            return file_row_cache[file_id]

        chunk_cache: Dict[str, List[str]] = {}

        def _chunk_texts(file_id: str) -> List[str]:
            if file_id not in chunk_cache:
                chunk_cache[file_id] = [c["text"] for c in chunks_repo.list_for_file(file_id) if c.get("text")]
            return chunk_cache[file_id]

        claims_written = 0
        claims_rejected: List[Dict[str, Any]] = []
        deferred: List[Dict[str, Any]] = []
        subjects_created = 0
        review_items: List[Dict[str, Any]] = []
        corrections_active: List[Dict[str, Any]] = []
        touched_fact_ids: set = set()
        touched_edge_pairs: set = set()
        single_valued_types = _single_valued_edge_types()

        def _write_evidence(
            *,
            kind: str,
            subject_id: str,
            evidence: List[Dict[str, Any]],
            row_ref: str,
            row_attrs: Optional[Dict[str, Any]] = None,
        ) -> None:
            nonlocal claims_written
            with self._engine.connect() as conn:
                for ev_idx, ev in enumerate(evidence):
                    doc_id = ev.get("doc_id")
                    quote = ev.get("quote") or ""
                    item_ref = f"{row_ref}.evidence[{ev_idx}]"
                    if not quote:
                        claims_rejected.append({"row": item_ref, "reason": "empty_quote", "doc_id": doc_id})
                        continue
                    file_id = _resolve_doc(doc_id, conn)
                    frow = _file_row(file_id) if file_id else None
                    if file_id is None or frow is None:
                        claims_rejected.append({"row": item_ref, "reason": "unresolved_doc_id", "doc_id": doc_id})
                        continue
                    if frow.get("processing_status") != "indexed":
                        deferred.append(
                            {
                                "row": item_ref,
                                "doc_id": doc_id,
                                "corpus_file_id": file_id,
                                "reason": "not_indexed",
                                "retry_after_seconds": 60,
                            }
                        )
                        continue
                    texts = _chunk_texts(file_id)
                    if not any(quote in t for t in texts):
                        claims_rejected.append({"row": item_ref, "reason": "verbatim_gate_failed", "doc_id": doc_id})
                        continue
                    written_id = self.add_claim(
                        fact_id=subject_id if kind == "fact" else None,
                        edge_id=subject_id if kind == "edge" else None,
                        corpus_file_id=file_id,
                        corpus_id=frow["corpus_id"],
                        file_sha256=frow.get("sha256") or "",
                        quote=quote,
                        # Per-evidence `attrs` isn't part of the producer's
                        # wire format (spec §7.0) — `attrs` sits on the
                        # node/edge row itself ("what THIS document says",
                        # §3), and the concatenated-per-document pipeline
                        # output means one row object == one document's
                        # occurrence, so the row's own attrs is what each
                        # of its claims should carry. An evidence-level
                        # `attrs` is honored first if a future producer
                        # ever supplies one (forward-compatible, unused
                        # today).
                        attrs=ev.get("attrs") or row_attrs or {},
                        document_date=doc_dates.get(file_id),
                    )
                    if written_id is not None:
                        claims_written += 1
                        if kind == "fact":
                            touched_fact_ids.add(subject_id)

        # ---- nodes: alias resolution + evidence.
        node_fact_ids: Dict[str, str] = {}
        for idx, node in enumerate(nodes):
            node_id = node.get("id")
            row_ref = f"nodes[{idx}]"
            if not node_id:
                claims_rejected.append({"row": row_ref, "reason": "missing_node_id"})
                continue
            with self._engine.connect() as conn:
                resolution = self._resolve_alias(conn, node_id, node.get("type"))
            if resolution["error"] is not None:
                claims_rejected.append({"row": row_ref, "reason": resolution["error"], "node_id": node_id})
                continue
            if resolution["created"]:
                subjects_created += 1
                if resolution.get("reattached"):
                    corrections_active.append(resolution["reattached"])
            node_fact_ids[node_id] = resolution["fact_id"]
            _write_evidence(
                kind="fact",
                subject_id=resolution["fact_id"],
                evidence=node.get("evidence") or [],
                row_ref=row_ref,
                row_attrs=node.get("attrs") or {},
            )

        # ---- edges: endpoints resolve via the SAME alias mechanism (an
        # edge's src/dst are themselves node ids, spec §7.0) + evidence.
        # `possible_duplicate_of` is the one edge type exempt from
        # requiring evidence (§7.0) -- accepted and surfaced as a review
        # item (§7.2) regardless of whether it carries any.
        def _endpoint(node_id: str, conn) -> Dict[str, Any]:
            if node_id in node_fact_ids:
                return {"fact_id": node_fact_ids[node_id], "created": False, "error": None, "reattached": None}
            return self._resolve_alias(conn, node_id, None)

        for idx, edge in enumerate(edges):
            row_ref = f"edges[{idx}]"
            src_id, edge_type, dst_id = edge.get("src"), edge.get("type"), edge.get("dst")
            if not src_id or not edge_type or not dst_id:
                claims_rejected.append({"row": row_ref, "reason": "malformed_edge"})
                continue
            with self._engine.connect() as conn:
                src_res = _endpoint(src_id, conn)
                dst_res = _endpoint(dst_id, conn)
            if src_res.get("error") or dst_res.get("error"):
                claims_rejected.append({"row": row_ref, "reason": "edge_endpoint_unresolved"})
                continue
            for endpoint_id, res in ((src_id, src_res), (dst_id, dst_res)):
                if res.get("created"):
                    subjects_created += 1
                    node_fact_ids[endpoint_id] = res["fact_id"]
                    if res.get("reattached"):
                        corrections_active.append(res["reattached"])

            edge_id = self.create_edge(src=src_res["fact_id"], type=edge_type, dst=dst_res["fact_id"])
            with self._engine.begin() as conn:
                reattached_edge = self._reattach_correction(
                    conn, subject_kind="edge", subject_id=edge_id, natural_key_probe=[src_id, edge_type, dst_id]
                )
            if reattached_edge:
                corrections_active.append(reattached_edge)

            if edge_type == "possible_duplicate_of":
                review_items.append({"type": "possible_duplicate_of", "edge_id": edge_id, "src": src_id, "dst": dst_id})
            if edge_type in single_valued_types:
                touched_edge_pairs.add((src_res["fact_id"], edge_type))
            _write_evidence(
                kind="edge",
                subject_id=edge_id,
                evidence=edge.get("evidence") or [],
                row_ref=row_ref,
                row_attrs=edge.get("attrs") or {},
            )

        # ---- functionally single-valued edges (spec §7.3): a (src, type)
        # pair TOUCHED by this batch (an edge of a configured type was just
        # written for it) with >1 distinct dst carrying a LIVE claim becomes
        # a review item. The query hits `edges`/`claims` directly rather
        # than this batch's rows, so a dst written by an EARLIER batch is
        # picked up the moment a second one arrives now (never persisted —
        # recomputed here, and again at read time in
        # `collection_facts_summary`, so it clears the instant a dst's
        # claims are gone).
        if touched_edge_pairs:
            srcs = list({pair[0] for pair in touched_edge_pairs})
            types_ = list({pair[1] for pair in touched_edge_pairs})
            with self._engine.connect() as conn:
                sv_rows = (
                    conn.execute(
                        sa.text(
                            """
                            SELECT e.src, e.type, e.dst
                            FROM edges e
                            WHERE e.src = ANY(:srcs) AND e.type = ANY(:types)
                              AND EXISTS (SELECT 1 FROM claims c WHERE c.edge_id = e.id)
                            """
                        ),
                        {"srcs": srcs, "types": types_},
                    )
                    .mappings()
                    .all()
                )
            dsts_by_pair: Dict[tuple, set] = {}
            for r in sv_rows:
                pair = (r["src"], r["type"])
                if pair in touched_edge_pairs:
                    dsts_by_pair.setdefault(pair, set()).add(r["dst"])
            for (src_fact_id, edge_type_), dsts in dsts_by_pair.items():
                if len(dsts) > 1:
                    review_items.append(
                        {
                            "kind": "single_valued_conflict",
                            "src": src_fact_id,
                            "type": edge_type_,
                            "dsts": sorted(dsts),
                        }
                    )

        # ---- same-date attribute conflicts introduced by THIS batch become
        # review items (EQ4) -- different-date succession needs none (EQ5),
        # already correct at READ time via search()'s projection; this is
        # the write-side signal a human reviewer would want surfaced now.
        if touched_fact_ids:
            with self._engine.connect() as conn:
                conflict_rows = (
                    conn.execute(
                        sa.text(
                            """
                            SELECT c.fact_id, kv.key, c.document_date, jsonb_agg(DISTINCT kv.value) AS values
                            FROM claims c
                            CROSS JOIN LATERAL jsonb_each(c.attrs) AS kv(key, value)
                            WHERE c.fact_id = ANY(:ids) AND c.document_date IS NOT NULL
                            GROUP BY c.fact_id, kv.key, c.document_date
                            HAVING COUNT(DISTINCT kv.value) > 1
                            """
                        ),
                        {"ids": list(touched_fact_ids)},
                    )
                    .mappings()
                    .all()
                )
            for r in conflict_rows:
                review_items.append(
                    {
                        "type": "attribute_conflict",
                        "subject_id": r["fact_id"],
                        "key": r["key"],
                        "document_date": r["document_date"].isoformat(),
                        "values": _decode_jsonb(r["values"]),
                    }
                )

        subjects_deleted = self.sweep_orphans()

        return {
            "claims_written": claims_written,
            "claims_rejected": claims_rejected,
            "deferred": deferred,
            "subjects_created": subjects_created,
            "subjects_deleted": subjects_deleted,
            "corrections_active": corrections_active,
            "review_items": review_items,
        }

    # ------------------------------------------------------------------
    # entity-resolution repair (spec §3: "a merge is adding an alias...
    # repoint aliases + claims to the canonical subject, audit-logged; a
    # split is the reverse") — EQ7's foundation. Not on the ingest write
    # path itself (ingest never auto-merges); an admin/reconciliation
    # caller invokes these directly.
    # ------------------------------------------------------------------

    def merge_facts(self, *, canonical_id: str, merged_id: str, merged_by: str) -> Dict[str, Any]:
        """Merge ``merged_id`` INTO ``canonical_id``: repoint every alias
        and claim, delete the now-empty ``merged_id`` row, audit-log the
        action. Both facts must share a ``type`` (entity-resolution repair,
        not a type change). Returns a snapshot — ``{canonical_id,
        merged_id, aliases, claim_ids, duplicate_claims}`` — sufficient for
        :meth:`split_fact` to reverse it (union of claims, both aliases, per
        spec EQ7).

        **Shared evidence.** ``uq_claims_subject_file_quote`` is unique on
        ``(COALESCE(fact_id, edge_id), corpus_file_id, quote_hash)``, so a
        bare repoint explodes with an IntegrityError whenever the two facts
        each carry a claim from the same document with the same quote —
        which is exactly the ordinary entity-resolution case (one sentence
        evidencing "Acme Corp" and "Acme Corporation"). Those merged-side
        claims are therefore captured into ``duplicate_claims`` and deleted
        before the repoint; the canonical's identical claim survives, so no
        evidence is lost. ``split_fact`` re-creates them on the new fact from
        that snapshot, which keeps the merge reversible — under fresh claim
        ids, the same way ``split_fact`` already mints a new fact id rather
        than resurrecting ``merged_id`` (ids are opaque and never reused,
        spec §3)."""
        with self._engine.begin() as conn:
            canonical = (
                conn.execute(sa.text("SELECT type FROM facts WHERE id = :id"), {"id": canonical_id}).mappings().first()
            )
            merged = (
                conn.execute(sa.text("SELECT type FROM facts WHERE id = :id"), {"id": merged_id}).mappings().first()
            )
            if canonical is None:
                raise FactNotFound(canonical_id)
            if merged is None:
                raise FactNotFound(merged_id)
            if canonical["type"] != merged["type"]:
                raise ValueError(
                    f"merge_facts requires matching types, got {canonical['type']!r} vs {merged['type']!r}"
                )
            aliases = (
                conn.execute(sa.text("SELECT natural_key FROM fact_aliases WHERE fact_id = :id"), {"id": merged_id})
                .scalars()
                .all()
            )
            # Merged-side claims the canonical already holds for the SAME
            # (document, quote): repointing them would violate
            # uq_claims_subject_file_quote. Capture their content so the
            # split can re-create them, then drop them — the canonical's
            # identical claim carries the evidence forward.
            dup_rows = (
                conn.execute(
                    sa.text(
                        "SELECT id, corpus_file_id, corpus_id, file_sha256, attrs, quote, quote_hash, document_date "
                        "FROM claims m WHERE m.fact_id = :merged AND EXISTS ("
                        "  SELECT 1 FROM claims c WHERE c.fact_id = :canonical "
                        "    AND c.corpus_file_id = m.corpus_file_id AND c.quote_hash = m.quote_hash)"
                    ),
                    {"merged": merged_id, "canonical": canonical_id},
                )
                .mappings()
                .all()
            )
            duplicate_claims = [
                {
                    "corpus_file_id": r["corpus_file_id"],
                    "corpus_id": r["corpus_id"],
                    "file_sha256": r["file_sha256"],
                    "attrs": _decode_jsonb(r["attrs"]) or {},
                    "quote": r["quote"],
                    "quote_hash": r["quote_hash"],
                    "document_date": r["document_date"].isoformat() if r["document_date"] else None,
                }
                for r in dup_rows
            ]
            dup_ids = [r["id"] for r in dup_rows]
            if dup_ids:
                conn.execute(sa.text("DELETE FROM claims WHERE id = ANY(:ids)"), {"ids": dup_ids})

            claim_ids = (
                conn.execute(sa.text("SELECT id FROM claims WHERE fact_id = :id"), {"id": merged_id}).scalars().all()
            )
            conn.execute(
                sa.text("UPDATE fact_aliases SET fact_id = :canonical WHERE fact_id = :merged"),
                {"canonical": canonical_id, "merged": merged_id},
            )
            conn.execute(
                sa.text("UPDATE claims SET fact_id = :canonical WHERE fact_id = :merged"),
                {"canonical": canonical_id, "merged": merged_id},
            )
            conn.execute(sa.text("DELETE FROM facts WHERE id = :id"), {"id": merged_id})

        from src.repositories import audit_repo

        snapshot = {
            "canonical_id": canonical_id,
            "merged_id": merged_id,
            "aliases": list(aliases),
            "claim_ids": list(claim_ids),
            "duplicate_claims": duplicate_claims,
        }
        audit_repo().log(
            user_id=merged_by,
            action="facts.merge",
            resource=f"fact/{canonical_id}",
            params={
                "merged_id": merged_id,
                "aliases": snapshot["aliases"],
                "claim_ids": snapshot["claim_ids"],
                "duplicate_claims_dropped": len(duplicate_claims),
            },
        )
        return snapshot

    def split_fact(self, *, canonical_id: str, snapshot: Dict[str, Any], split_by: str) -> str:
        """Reverse a prior :meth:`merge_facts` using its returned
        ``snapshot``: mint a NEW fact of the canonical's type, repoint the
        snapshotted aliases + claims back onto it, and RE-CREATE any claims
        the merge had to drop as duplicates of the canonical's own evidence
        (``duplicate_claims`` — see :meth:`merge_facts`). Returns the new
        fact id — deliberately NOT the original ``merged_id``, since ids are
        opaque and never reused (spec §3); the re-created claims get fresh
        ids for the same reason."""
        aliases = snapshot.get("aliases") or []
        claim_ids = snapshot.get("claim_ids") or []
        duplicate_claims = snapshot.get("duplicate_claims") or []
        with self._engine.begin() as conn:
            row = (
                conn.execute(sa.text("SELECT type FROM facts WHERE id = :id"), {"id": canonical_id}).mappings().first()
            )
            if row is None:
                raise FactNotFound(canonical_id)
            new_id = "f_" + secrets.token_hex(8)
            conn.execute(
                sa.text("INSERT INTO facts (id, type) VALUES (:id, :type)"), {"id": new_id, "type": row["type"]}
            )
            if aliases:
                conn.execute(
                    sa.text(
                        "UPDATE fact_aliases SET fact_id = :new "
                        "WHERE natural_key = ANY(:aliases) AND fact_id = :canonical"
                    ),
                    {"new": new_id, "aliases": list(aliases), "canonical": canonical_id},
                )
            if claim_ids:
                conn.execute(
                    sa.text("UPDATE claims SET fact_id = :new WHERE id = ANY(:ids)"),
                    {"new": new_id, "ids": list(claim_ids)},
                )
            for dup in duplicate_claims:
                conn.execute(
                    sa.text(
                        "INSERT INTO claims "
                        "(id, fact_id, edge_id, corpus_file_id, corpus_id, file_sha256, attrs, quote, "
                        " quote_hash, document_date) "
                        "VALUES (:id, :fact_id, NULL, :corpus_file_id, :corpus_id, :file_sha256, "
                        "        CAST(:attrs AS JSONB), :quote, :quote_hash, :document_date) "
                        "ON CONFLICT (COALESCE(fact_id, edge_id), corpus_file_id, quote_hash) DO NOTHING"
                    ),
                    {
                        "id": "c_" + secrets.token_hex(8),
                        "fact_id": new_id,
                        "corpus_file_id": dup["corpus_file_id"],
                        "corpus_id": dup["corpus_id"],
                        "file_sha256": dup.get("file_sha256") or "",
                        "attrs": json.dumps(dup.get("attrs") or {}),
                        "quote": dup["quote"],
                        "quote_hash": dup["quote_hash"],
                        "document_date": _parse_document_date(dup.get("document_date")),
                    },
                )

        from src.repositories import audit_repo

        audit_repo().log(
            user_id=split_by,
            action="facts.split",
            resource=f"fact/{canonical_id}",
            params={
                "new_id": new_id,
                "aliases": aliases,
                "claim_ids": claim_ids,
                "duplicate_claims_recreated": len(duplicate_claims),
            },
        )
        return new_id


def _parse_document_date(value: Any) -> Optional[date]:
    """Best-effort parse of a producer-supplied date/datetime string (spec
    §10: Graph's ``lastModifiedDateTime``, ISO-8601) into a plain
    :class:`datetime.date`. Returns ``None`` for anything unparseable —
    the caller treats a missing/invalid date exactly like an absent one
    (§10's "null dates" rule), never raises on malformed producer input."""
    if not value:
        return None
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None
