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
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import date
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
    ) -> str:
        if (fact_id is None) == (edge_id is None):
            raise ValueError("add_claim requires exactly one of fact_id/edge_id")
        claim_id = "c_" + secrets.token_hex(8)
        quote_hash = hashlib.sha256(quote.encode("utf-8")).hexdigest()[:16]
        with self._engine.begin() as conn:
            conn.execute(
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
        return claim_id

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
        withheld/unreadable one cost the same (spec §5 rule 2)."""
        kind_column = "fact_id" if subject_kind == "fact" else "edge_id"
        vis = self._visibility_predicate("c.corpus_id", is_admin)
        if all_evidence:
            has_claim_visibility = (
                f"EXISTS (SELECT 1 FROM claims c WHERE c.{kind_column} = :subject_id) "
                f"AND NOT EXISTS (SELECT 1 FROM claims c WHERE c.{kind_column} = :subject_id AND NOT ({vis}))"
            )
        else:
            has_claim_visibility = f"EXISTS (SELECT 1 FROM claims c WHERE c.{kind_column} = :subject_id AND {vis})"
        sql = sa.text(
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
        filters = filters or {}
        if len(filters) > MAX_SEARCH_FILTERS:
            raise ValueError(f"too many filters (max {MAX_SEARCH_FILTERS})")
        limit = max(1, min(limit, MAX_SEARCH_LIMIT))

        readable = _readable_ids(caller)
        is_admin = readable is None
        all_evidence = _visibility_mode() == "all_evidence"
        vis = self._visibility_predicate("c.corpus_id", is_admin)

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
            visible AS (
                SELECT cand.subject_id, cand.subject_type,
                       (cand.subject_id IN (SELECT subject_id FROM revealed_ids)) AS is_revealed
                FROM candidates cand
                WHERE cand.subject_id IN (SELECT subject_id FROM revealed_ids)
                   OR (
                        NOT {str(all_evidence).upper()}
                        AND EXISTS (SELECT 1 FROM counted_claims cc WHERE cc.subject_id = cand.subject_id)
                   )
                   OR (
                        {str(all_evidence).upper()}
                        AND EXISTS (SELECT 1 FROM claims c2 WHERE c2.fact_id = cand.subject_id)
                        AND NOT EXISTS (
                            SELECT 1 FROM claims c3
                            WHERE c3.fact_id = cand.subject_id AND NOT ({self._visibility_predicate("c3.corpus_id", is_admin)})
                        )
                   )
            ),
            attr_kv AS (
                SELECT cc.subject_id, kv.key, kv.value, cc.document_date
                FROM counted_claims cc
                JOIN visible v ON v.subject_id = cc.subject_id
                CROSS JOIN LATERAL jsonb_each(cc.attrs) AS kv(key, value)
            ),
            key_maxdate AS (
                SELECT subject_id, key, MAX(document_date) AS max_dated
                FROM attr_kv
                WHERE document_date IS NOT NULL
                GROUP BY subject_id, key
            ),
            winning AS (
                SELECT ak.subject_id, ak.key, ak.value, ak.document_date
                FROM attr_kv ak
                LEFT JOIN key_maxdate kmd ON kmd.subject_id = ak.subject_id AND kmd.key = ak.key
                WHERE (kmd.max_dated IS NOT NULL AND ak.document_date = kmd.max_dated)
                   OR (kmd.max_dated IS NULL)
            ),
            attr_proj AS (
                SELECT subject_id, key,
                       COUNT(DISTINCT value) AS n_distinct,
                       MAX(document_date) AS rep_date,
                       jsonb_agg(DISTINCT value) AS values_agg,
                       (array_agg(value))[1] AS single_value
                FROM winning
                GROUP BY subject_id, key
            ),
            attr_value_json AS (
                SELECT subject_id, key,
                    CASE WHEN n_distinct = 1
                        THEN jsonb_build_object('value', single_value, 'document_date', to_jsonb(rep_date))
                        ELSE jsonb_build_object('conflicted', true, 'values', values_agg)
                    END AS proj
                FROM attr_proj
            ),
            subject_attrs AS (
                SELECT subject_id, jsonb_object_agg(key, proj) AS attrs
                FROM attr_value_json
                GROUP BY subject_id
            ),
            counts AS (
                SELECT v.subject_id, COUNT(cc.claim_id) AS claim_count
                FROM visible v
                LEFT JOIN counted_claims cc ON cc.subject_id = v.subject_id
                GROUP BY v.subject_id
            ),
            aliases AS (
                SELECT fact_id AS subject_id, jsonb_agg(natural_key ORDER BY natural_key) AS aliases
                FROM fact_aliases
                GROUP BY fact_id
            )
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
                        SELECT e.id, e.src, e.dst, e.type
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

        return {
            "nodes": [{"id": n["id"], "type": n["type"], "revealed": n["revealed"]} for n in nodes.values()],
            "edges": edges_out,
            "truncated": truncated,
        }

    # ------------------------------------------------------------------
    # claims
    # ------------------------------------------------------------------

    def claims(self, caller, subject_id: str) -> Dict[str, Any]:
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
            document: Dict[str, Any] = {"name": r["filename"], "path": r["path"]}
            if r.get("source_url"):
                document["source_url"] = r["source_url"]
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
