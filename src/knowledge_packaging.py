"""Per-collection knowledge artifacts (K3, #798) — credential-free local packaging.

Builds one ``<corpus_id>.duckdb`` per Collection under ``DATA_DIR/knowledge/``
containing the corpus's chunks + embeddings (the exact candidate set
``src.ingest.retrieval`` scores) with ``filename`` denormalized in, so the
local reader needs no other table. The manifest lists these artifacts next to
tables (RBAC = collection grants); ``agnes pull`` ships them to laptops.

No new DB state: freshness lives in ``DATA_DIR/knowledge/state.json``
(``{corpus_id: {fingerprint, md5, size_bytes, chunks, built_at}}``). The
fingerprint hashes chunk ids + content, so any ingest/re-ingest/delete flips
it and the next packaging pass rebuilds; unchanged corpora are skipped.

The seam is deliberately generic ("assets" style): manifest entries carry a
``kind`` field ("chunks" today) so K4 digest artifacts ride the same channel
without new plumbing. Rebuilds promote atomically (build into a sidecar,
``os.replace``) so a concurrent download never sees a half-written file.

**TCRD-296 synthesis C.15 (2026-09).** This module used to run inline inside
``POST /api/admin/run-knowledge-packaging`` — the scheduler HTTP-called it
directly, on a client timeout (600s) shorter than a large pass could take, so
overlapping ticks raced each other on a shared ``analytics.duckdb``-style
handle and one overlap OOM-killed the app. It now runs as the
``knowledge-packaging`` worker job kind (``app/worker/kinds.py``), which
supplies the single-run guarantee (idempotency-keyed enqueue + a PG advisory
lock) and the time budget this module honors via ``deadline``. Two changes
here support that:

- **Bounded reads.** ``build_artifact``/``corpus_fingerprint`` used to load an
  entire corpus's chunk rows (text + 384-dim embeddings) into a Python list in
  one call — fine at "dozens of files", not at the hundreds-of-thousands-of-
  chunks scale a real crawl reaches. Both now page through
  ``CorpusChunksRepository.list_for_corpus_batch`` (keyset pagination by
  ``id``), never holding more than one page in memory.
- **Per-run temp file identity.** ``build_artifact``'s tmp DuckDB path used to
  be fixed per corpus (``<corpus_id>.duckdb.build.tmp``) — two overlapping
  calls building the SAME corpus opened the SAME path in the SAME process,
  and DuckDB's Python API shares one underlying database instance across
  connections to an identical path within a process, so the second caller's
  ``CREATE TABLE chunks`` collided with the first's ("Table with name
  'chunks' already exists" — the live incident). The tmp path now carries a
  random per-call suffix, so two concurrent builds (however they'd manage to
  happen — the worker job kind's single-run guarantee is the real fix) can
  never collide on the same file.
- **Checkpointed progress.** ``run_packaging_pass`` now accepts a ``deadline``
  (a ``time.monotonic()`` cutoff) and persists ``state.json`` after EVERY
  collection it finishes, not once at the very end — a run that hits its
  deadline mid-pass loses progress on at most the one collection it was
  building when time ran out; every collection already built or confirmed
  unchanged this pass stays recorded.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from src.duckdb_conn import _open_duckdb

logger = logging.getLogger(__name__)

ARTIFACT_FORMAT_VERSION = 1
_STATE_FILE = "state.json"

#: Default page size for the bounded chunk reads below — small enough to
#: keep memory flat regardless of corpus size (a 384-dim float embedding
#: plus body text is a few KB per row; a few hundred rows per page is a
#: bounded, low-round-trip-count batch), not a tunable — TCRD-296 review:
#: no speculative config knob for a value nothing has yet needed to change.
_DEFAULT_CHUNK_BATCH_SIZE = 500


def artifacts_dir() -> Path:
    from app.utils import get_data_dir

    return get_data_dir() / "knowledge"


# ── data-access seams (patched in tests; factories in production) ──────────


def _list_chunks(corpus_id: str) -> List[Dict[str, Any]]:
    """Unbounded full-corpus chunk fetch — kept as the seam
    ``src.knowledge_digests`` still imports directly (``_list_chunks as f``)
    for its LLM-prompt source text, which is separately hard-capped at
    ``_SOURCE_CHAR_BUDGET`` chars. NOT used by ``build_artifact``/
    ``corpus_fingerprint`` below — those page through
    :func:`_iter_chunk_batches` instead (TCRD-296 synthesis C.15)."""
    from src.repositories import corpus_chunks_repo

    return corpus_chunks_repo().list_for_corpus(corpus_id)


def _list_chunk_batch(corpus_id: str, *, after_id: Optional[str], limit: int) -> List[Dict[str, Any]]:
    from src.repositories import corpus_chunks_repo

    return corpus_chunks_repo().list_for_corpus_batch(corpus_id, after_id=after_id, limit=limit)


def _iter_chunk_batches(corpus_id: str, batch_size: int = _DEFAULT_CHUNK_BATCH_SIZE) -> Iterator[List[Dict[str, Any]]]:
    """Yield successive bounded, id-ordered pages of a corpus's chunks.

    Never materializes more than ``batch_size`` chunk rows (including their
    embeddings) at once — the memory bound ``build_artifact``/
    ``corpus_fingerprint`` rely on. Pages are ALREADY ordered by ``id``
    (the repo method's keyset cursor column), so a caller that wants a
    stable, fetch-order-independent walk (the fingerprint) gets it for free
    without a separate in-memory sort.
    """
    after_id: Optional[str] = None
    while True:
        batch = _list_chunk_batch(corpus_id, after_id=after_id, limit=batch_size)
        if not batch:
            return
        yield batch
        if len(batch) < batch_size:
            return
        after_id = batch[-1]["id"]


def _list_files(corpus_id: str) -> List[Dict[str, Any]]:
    from src.repositories import corpus_files_repo

    return corpus_files_repo().list_for_corpus(corpus_id)


def _list_corpora() -> List[Dict[str, Any]]:
    from src.repositories import file_corpora_repo

    return file_corpora_repo().list_all()


# ── state ──────────────────────────────────────────────────────────────────


def load_state() -> Dict[str, Any]:
    path = artifacts_dir() / _STATE_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError):
        logger.warning("knowledge packaging state.json unreadable; rebuilding all")
        return {}


def _save_state(state: Dict[str, Any]) -> None:
    d = artifacts_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (_STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.replace(tmp, d / _STATE_FILE)


# ── fingerprint / build ─────────────────────────────────────────────────────


def corpus_fingerprint(corpus_id: str, *, batch_size: int = _DEFAULT_CHUNK_BATCH_SIZE) -> str:
    """Content fingerprint: flips on any chunk add/remove/edit/embedding change.

    Walks the corpus's chunks in bounded, id-ordered pages
    (:func:`_iter_chunk_batches`) and hashes id|ordinal|text|has-vector —
    never materializing more than one page at once, unlike the old
    load-the-whole-corpus-then-sort implementation. Pages already arrive in
    ascending ``id`` order (the repo's keyset cursor), so hashing them as
    they stream in is equivalent to the old explicit sort-then-hash.
    """
    h = hashlib.md5()
    for batch in _iter_chunk_batches(corpus_id, batch_size):
        for ch in batch:
            h.update(
                f"{ch.get('id')}|{ch.get('ordinal')}|"
                f"{hashlib.md5((ch.get('text') or '').encode()).hexdigest()}|"
                f"{1 if ch.get('embedding') else 0}|".encode()
            )
    return h.hexdigest()


def _file_md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(8192), b""):
            h.update(block)
    return h.hexdigest()


def build_artifact(corpus_id: str, *, batch_size: int = _DEFAULT_CHUNK_BATCH_SIZE) -> Dict[str, Any]:
    """Build ``<corpus_id>.duckdb`` and promote it atomically.

    Chunks are inserted in bounded pages (:func:`_iter_chunk_batches`) —
    never more than ``batch_size`` rows held in memory at once, regardless
    of corpus size.

    The build happens under a per-call, randomly-suffixed tmp path rather
    than a fixed ``<corpus_id>.duckdb.build.tmp`` — TWO ``duckdb.connect()``
    calls to the SAME path in the SAME process share one underlying
    database instance, so two overlapping builds of the same corpus used to
    collide on each other's ``CREATE TABLE chunks`` (the live incident this
    fix addresses; see the module docstring). Any stale tmp file left behind
    by an earlier crashed run (any suffix) is swept before this run's own
    build starts.

    Returns ``{"md5", "size_bytes", "chunks", "built_at"}``.
    """
    d = artifacts_dir()
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"{corpus_id}.duckdb"
    for stale in d.glob(f"{corpus_id}.duckdb.build.*.tmp"):
        stale.unlink(missing_ok=True)
    tmp = d / f"{corpus_id}.duckdb.build.{uuid.uuid4().hex[:12]}.tmp"

    names = {f["id"]: f.get("filename") for f in _list_files(corpus_id)}
    corpus = next((c for c in _list_corpora() if c["id"] == corpus_id), None)
    built_at = datetime.now(timezone.utc).isoformat()

    chunk_count = 0
    con = _open_duckdb(str(tmp))
    try:
        con.execute(
            "CREATE TABLE chunks ("
            " id VARCHAR PRIMARY KEY, corpus_id VARCHAR, file_id VARCHAR,"
            " filename VARCHAR, ordinal INTEGER, text VARCHAR,"
            " embedding FLOAT[384], section_path VARCHAR, page INTEGER,"
            " bbox VARCHAR, metadata VARCHAR)"
        )
        con.execute("CREATE TABLE artifact_meta (key VARCHAR PRIMARY KEY, value VARCHAR)")
        for batch in _iter_chunk_batches(corpus_id, batch_size):
            for ch in batch:
                emb = ch.get("embedding")
                con.execute(
                    "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        ch.get("id"),
                        ch.get("corpus_id"),
                        ch.get("file_id"),
                        names.get(ch.get("file_id")),
                        ch.get("ordinal"),
                        ch.get("text"),
                        list(emb) if emb else None,
                        ch.get("section_path"),
                        ch.get("page"),
                        ch.get("bbox"),
                        ch.get("metadata"),
                    ],
                )
                chunk_count += 1
        meta = {
            "format_version": str(ARTIFACT_FORMAT_VERSION),
            "kind": "chunks",
            "corpus_id": corpus_id,
            "corpus_name": (corpus or {}).get("name") or "",
            "built_at": built_at,
            "chunk_count": str(chunk_count),
            "embed_dim": "384",
        }
        for k, v in meta.items():
            con.execute("INSERT INTO artifact_meta VALUES (?, ?)", [k, v])
    finally:
        con.close()

    os.replace(tmp, dest)
    return {
        "md5": _file_md5(dest),
        "size_bytes": dest.stat().st_size,
        "chunks": chunk_count,
        "built_at": built_at,
    }


def run_packaging_pass(
    *,
    deadline: Optional[float] = None,
    batch_size: int = _DEFAULT_CHUNK_BATCH_SIZE,
) -> Dict[str, Any]:
    """Rebuild artifacts for changed corpora; prune artifacts for gone corpora.

    Per-corpus errors are aggregated — one broken corpus doesn't stop
    healthy siblings (the `_run_materialized_pass` posture).

    ``deadline`` (optional, a ``time.monotonic()`` cutoff): when the pass
    would otherwise start a NEW collection at/after this point, it stops
    early instead and returns with ``interrupted_reason="timeout"`` — the
    ``knowledge-packaging`` worker job kind's time budget
    (``app/worker/kinds.py``) uses this to bound one run's wall-clock cost.
    ``None`` (the default) runs to completion, unchanged from the pre-budget
    behavior.

    State is checkpointed (``state.json`` written) after every collection
    that finishes — not once at the very end — so an interruption loses
    progress on at most the ONE collection in flight when the deadline hit;
    every earlier collection this pass built or confirmed unchanged stays
    recorded, and the next scheduled run picks up where this one left off
    (an already-recorded, still-matching fingerprint is a skip, not a
    rebuild).

    Pruning (deleting artifacts for corpora that no longer exist) only runs
    after a FULL, uninterrupted sweep of every corpus — an interrupted pass
    has not looked at every corpus, so treating the ones it never reached as
    "gone" would delete artifacts for collections that are still very much
    live.
    """
    started = time.monotonic()
    summary: Dict[str, Any] = {
        "built": [],
        "skipped": [],
        "pruned": [],
        "errors": [],
        "interrupted_reason": None,
    }
    state = load_state()
    live_ids: set[str] = set()
    corpora = _list_corpora()
    interrupted = False
    for corpus in corpora:
        if deadline is not None and time.monotonic() >= deadline:
            interrupted = True
            summary["interrupted_reason"] = "timeout"
            logger.info(
                "knowledge packaging: deadline reached after %d/%d collections; stopping (checkpointed progress kept)",
                len(live_ids),
                len(corpora),
            )
            break
        cid = corpus["id"]
        live_ids.add(cid)
        try:
            fp = corpus_fingerprint(cid, batch_size=batch_size)
            prior = state.get(cid) or {}
            if prior.get("fingerprint") == fp and (artifacts_dir() / f"{cid}.duckdb").exists():
                summary["skipped"].append(cid)
                continue
            info = build_artifact(cid, batch_size=batch_size)
            state[cid] = dict(info, fingerprint=fp)
            summary["built"].append(cid)
            _save_state(state)  # checkpoint immediately — see docstring
        except Exception as exc:
            logger.exception("knowledge packaging failed for %s", cid)
            summary["errors"].append({"corpus_id": cid, "error": str(exc)})
    if not interrupted:
        for cid in sorted(set(state) - live_ids):
            (artifacts_dir() / f"{cid}.duckdb").unlink(missing_ok=True)
            state.pop(cid, None)
            summary["pruned"].append(cid)
        _save_state(state)
    summary["collections_total"] = len(corpora)
    summary["collections_processed"] = len(live_ids)
    summary["duration_s"] = round(time.monotonic() - started, 3)
    return summary
