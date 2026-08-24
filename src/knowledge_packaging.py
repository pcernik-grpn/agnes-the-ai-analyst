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
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from src.duckdb_conn import _open_duckdb

logger = logging.getLogger(__name__)

ARTIFACT_FORMAT_VERSION = 1
_STATE_FILE = "state.json"


def artifacts_dir() -> Path:
    from app.utils import get_data_dir

    return get_data_dir() / "knowledge"


# ── data-access seams (patched in tests; factories in production) ──────────


def _list_chunks(corpus_id: str) -> List[Dict[str, Any]]:
    from src.repositories import corpus_chunks_repo

    return corpus_chunks_repo().list_for_corpus(corpus_id)


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


def corpus_fingerprint(corpus_id: str) -> str:
    """Content fingerprint: flips on any chunk add/remove/edit/embedding change.

    Loads the corpus's chunks (same cost retrieval pays per query — fine at
    the design's dozens-of-files scale) and hashes id|ordinal|text|has-vector
    in id order so DB fetch order can't flip it.
    """
    h = hashlib.md5()
    for ch in sorted(_list_chunks(corpus_id), key=lambda c: str(c.get("id") or "")):
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


def build_artifact(corpus_id: str) -> Dict[str, Any]:
    """Build ``<corpus_id>.duckdb`` and promote it atomically.

    Returns ``{"md5", "size_bytes", "chunks", "built_at"}``.
    """
    d = artifacts_dir()
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"{corpus_id}.duckdb"
    tmp = d / f"{corpus_id}.duckdb.build.tmp"
    tmp.unlink(missing_ok=True)

    chunks = _list_chunks(corpus_id)
    names = {f["id"]: f.get("filename") for f in _list_files(corpus_id)}
    corpus = next((c for c in _list_corpora() if c["id"] == corpus_id), None)
    built_at = datetime.now(timezone.utc).isoformat()

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
        for ch in chunks:
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
        meta = {
            "format_version": str(ARTIFACT_FORMAT_VERSION),
            "kind": "chunks",
            "corpus_id": corpus_id,
            "corpus_name": (corpus or {}).get("name") or "",
            "built_at": built_at,
            "chunk_count": str(len(chunks)),
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
        "chunks": len(chunks),
        "built_at": built_at,
    }


def run_packaging_pass() -> Dict[str, Any]:
    """Rebuild artifacts for changed corpora; prune artifacts for gone corpora.

    Per-corpus errors are aggregated — one broken corpus doesn't stop
    healthy siblings (the `_run_materialized_pass` posture).
    """
    summary: Dict[str, Any] = {"built": [], "skipped": [], "pruned": [], "errors": []}
    state = load_state()
    live_ids = set()
    for corpus in _list_corpora():
        cid = corpus["id"]
        live_ids.add(cid)
        try:
            fp = corpus_fingerprint(cid)
            prior = state.get(cid) or {}
            if prior.get("fingerprint") == fp and (artifacts_dir() / f"{cid}.duckdb").exists():
                summary["skipped"].append(cid)
                continue
            info = build_artifact(cid)
            state[cid] = dict(info, fingerprint=fp)
            summary["built"].append(cid)
        except Exception as exc:
            logger.exception("knowledge packaging failed for %s", cid)
            summary["errors"].append({"corpus_id": cid, "error": str(exc)})
    for cid in sorted(set(state) - live_ids):
        (artifacts_dir() / f"{cid}.duckdb").unlink(missing_ok=True)
        state.pop(cid, None)
        summary["pruned"].append(cid)
    _save_state(state)
    return summary
