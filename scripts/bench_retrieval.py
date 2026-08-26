#!/usr/bin/env python3
"""Measure Collections retrieval at corpus sizes past its design point.

``src/ingest/retrieval.py`` says what it is: "brute-force at the current
scale (dozens of files)". Every ``search()`` call fetches EVERY chunk of the
caller's granted corpora into Python and scores them there. That is a
deliberate, documented trade — this script exists to find where it stops
being one, before a corpus in the thousands-of-documents range finds out for
us in front of a user.

What it measures, per scale, over N queries:

* ``fetch``  — ``corpus_chunks_repo().list_for_corpora`` (the DB read and its
  materialization into Python dicts)
* ``rank``   — ``rank_chunks`` (tokenizing, IDF, cosine, fusing, sorting)
* ``total``  — the two together, i.e. what a caller waits for
* ``peak RSS`` — resident-set high-water mark for the process

p50 and p95 for each, plus the RSS delta. Latency alone would miss the
likelier wall: ``_SELECT`` includes ``embedding FLOAT[384]``, so every
candidate's 384 floats are materialized as a Python list on EVERY query —
including on a deployment with no embedding model, where nothing reads them.
That cost is memory, not milliseconds, and it scales with corpus size rather
than with what the query needs.

``ru_maxrss`` is a process-lifetime high-water mark — it never goes down —
so measuring several scales sequentially in one process would hand every
later scale the previous scale's peak as its "before" reading and understate
``rss_growth_mb``. A multi-scale run therefore executes each scale in its
own fresh subprocess (the script re-invokes itself with ``--emit-row``) and
aggregates the rows in the parent, which keeps both ``peak_rss_mb`` and
``rss_growth_mb`` per-scale accurate.

Usage::

    scripts/bench_retrieval.py                       # default ladder
    scripts/bench_retrieval.py --scales 1000,50000 --queries 20
    scripts/bench_retrieval.py --scales 200000 --embed   # with vectors present
    scripts/bench_retrieval.py --json bench.json

``--embed`` stores random unit vectors and passes a random query vector,
which measures OUR cosine+fusion cost over embedded chunks without needing
the (heavy, optional) embedding model installed. It does not measure model
inference — that is a separate, fixed per-query cost.

Writes a throwaway DuckDB under a temp dir and deletes it. Reads nothing
from a real instance.
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import random
import resource
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import duckdb  # noqa: E402

from src.duckdb_conn import _open_duckdb  # noqa: E402
from src.ingest.retrieval import rank_chunks  # noqa: E402

_EMBED_DIM = 384

# A small vocabulary makes IDF meaningful (terms genuinely differ in
# frequency) and keeps the generated text shaped like prose rather than
# uniform noise, which would make every lexical score identical.
_VOCAB = [
    "engagement",
    "staffing",
    "deliverable",
    "milestone",
    "scope",
    "diligence",
    "integration",
    "synergy",
    "workstream",
    "governance",
    "roadmap",
    "migration",
    "assessment",
    "baseline",
    "forecast",
    "retention",
    "onboarding",
    "throughput",
    "reconciliation",
    "attrition",
]
# Rare terms appear in a small fraction of chunks — the realistic case for a
# query that should match a few documents out of thousands.
_RARE = ["northwind", "contoso", "fabrikam", "tailspin", "litware"]


def _rss_mb() -> float:
    """Peak RSS in MiB. macOS reports bytes, Linux kilobytes."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def _make_text(rng: random.Random, n_words: int = 120) -> str:
    words = [rng.choice(_VOCAB) for _ in range(n_words)]
    # ~2% of chunks carry a rare term, so a query for one has a small, real
    # answer set rather than matching everything or nothing.
    if rng.random() < 0.02:
        words[rng.randrange(n_words)] = rng.choice(_RARE)
    return " ".join(words)


def _unit_vector(rng: random.Random) -> List[float]:
    vec = [rng.gauss(0, 1) for _ in range(_EMBED_DIM)]
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def seed_corpus(
    conn: duckdb.DuckDBPyConnection,
    n_chunks: int,
    *,
    with_embeddings: bool,
    chunks_per_file: int = 40,
    seed: int = 1234,
) -> str:
    """Create the table and fill it with ``n_chunks`` rows in one corpus.

    Uses the real column shape (``embedding FLOAT[384]``) because the
    materialization cost of that column is part of what is being measured.
    """
    rng = random.Random(seed)
    conn.execute(
        """
        CREATE TABLE corpus_chunks (
            id VARCHAR PRIMARY KEY,
            corpus_id VARCHAR NOT NULL,
            file_id VARCHAR NOT NULL,
            ordinal INTEGER,
            text VARCHAR,
            embedding FLOAT[384],
            section_path VARCHAR,
            page INTEGER,
            bbox VARCHAR,
            metadata VARCHAR,
            created_at TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    corpus_id = "bench-corpus"
    batch: List[tuple] = []
    for i in range(n_chunks):
        batch.append(
            (
                f"chunk-{i}",
                corpus_id,
                f"file-{i // chunks_per_file}",
                i % chunks_per_file,
                _make_text(rng),
                _unit_vector(rng) if with_embeddings else None,
                None,
                None,
                None,
                None,
            )
        )
        if len(batch) >= 5000:
            conn.executemany(
                "INSERT INTO corpus_chunks (id, corpus_id, file_id, ordinal, text, embedding, "
                "section_path, page, bbox, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                batch,
            )
            batch.clear()
    if batch:
        conn.executemany(
            "INSERT INTO corpus_chunks (id, corpus_id, file_id, ordinal, text, embedding, "
            "section_path, page, bbox, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            batch,
        )
    return corpus_id


_COLS = [
    "id",
    "corpus_id",
    "file_id",
    "ordinal",
    "text",
    "embedding",
    "section_path",
    "page",
    "bbox",
    "metadata",
    "created_at",
]


def fetch_candidates(conn: duckdb.DuckDBPyConnection, corpus_id: str) -> List[Dict[str, Any]]:
    """The production candidate fetch, verbatim in shape.

    Mirrors ``CorpusChunksRepository.list_for_corpora`` — same column list
    (``embedding`` included, which is the point) and same dict
    materialization, against the bench database rather than a real instance.
    """
    rows = conn.execute(
        f"SELECT {', '.join(_COLS)} FROM corpus_chunks WHERE corpus_id IN (?) ORDER BY file_id, ordinal",
        [corpus_id],
    ).fetchall()
    return [dict(zip(_COLS, r)) for r in rows]


def _queries(rng: random.Random, n: int) -> List[str]:
    """A mix a real user produces: common terms (match nearly everything),
    rare terms (match a handful), and multi-term phrases."""
    out = []
    for i in range(n):
        kind = i % 3
        if kind == 0:
            out.append(rng.choice(_VOCAB))
        elif kind == 1:
            out.append(rng.choice(_RARE))
        else:
            out.append(" ".join(rng.choice(_VOCAB) for _ in range(3)))
    return out


def bench_scale(
    n_chunks: int,
    n_queries: int,
    *,
    with_embeddings: bool,
    k: int = 10,
) -> Dict[str, Any]:
    rng = random.Random(99)
    tmp = Path(tempfile.mkdtemp(prefix="agnes-bench-"))
    try:
        # `_open_duckdb`, not `duckdb.connect`: the bench database should
        # behave like a production one (UTC-pinned session timezone), and the
        # repo guard in tests/test_duckdb_session_tz.py exists to keep every
        # call site funnelling through here.
        conn = _open_duckdb(str(tmp / "bench.duckdb"))
        rss_before = _rss_mb()
        t0 = time.perf_counter()
        corpus_id = seed_corpus(conn, n_chunks, with_embeddings=with_embeddings)
        seed_s = time.perf_counter() - t0

        q_vec: Optional[List[float]] = _unit_vector(rng) if with_embeddings else None
        fetch_ms: List[float] = []
        rank_ms: List[float] = []
        total_ms: List[float] = []
        hits: List[int] = []

        for q in _queries(rng, n_queries):
            gc.collect()
            t = time.perf_counter()
            chunks = fetch_candidates(conn, corpus_id)
            t_fetch = time.perf_counter()
            # `rank_chunks` calls embed_query(query) internally, which returns
            # None without the extra. With --embed we want the cosine path
            # measured, so the query vector is injected the same way the real
            # code would have received one.
            if q_vec is not None:
                import src.ingest.retrieval as retrieval_mod

                original = retrieval_mod.embed_query
                retrieval_mod.embed_query = lambda _q: q_vec  # type: ignore[assignment]
                try:
                    top, _conf = rank_chunks(chunks, q, k=k)
                finally:
                    retrieval_mod.embed_query = original  # type: ignore[assignment]
            else:
                top, _conf = rank_chunks(chunks, q, k=k)
            t_end = time.perf_counter()

            fetch_ms.append((t_fetch - t) * 1000)
            rank_ms.append((t_end - t_fetch) * 1000)
            total_ms.append((t_end - t) * 1000)
            hits.append(len(top))
            del chunks

        conn.close()
        rss_after = _rss_mb()

        def pct(vals: List[float], p: float) -> float:
            ordered = sorted(vals)
            idx = min(len(ordered) - 1, int(round((p / 100) * (len(ordered) - 1))))
            return ordered[idx]

        return {
            "chunks": n_chunks,
            "embeddings": with_embeddings,
            "queries": n_queries,
            "seed_seconds": round(seed_s, 1),
            "fetch_p50_ms": round(statistics.median(fetch_ms), 1),
            "fetch_p95_ms": round(pct(fetch_ms, 95), 1),
            "rank_p50_ms": round(statistics.median(rank_ms), 1),
            "rank_p95_ms": round(pct(rank_ms, 95), 1),
            "total_p50_ms": round(statistics.median(total_ms), 1),
            "total_p95_ms": round(pct(total_ms, 95), 1),
            "peak_rss_mb": round(rss_after, 1),
            "rss_growth_mb": round(rss_after - rss_before, 1),
            "mean_hits": round(statistics.mean(hits), 1),
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _bench_scale_in_subprocess(n_chunks: int, n_queries: int, *, with_embeddings: bool) -> Dict[str, Any]:
    """Run one scale in a fresh child process and return its result row.

    ``ru_maxrss`` never decreases within a process, so per-scale peak/growth
    numbers are only honest when each scale starts from a fresh high-water
    mark. The child is this same script with ``--emit-row``, which prints the
    row as JSON on stdout; stderr is inherited so a failure is visible.
    """
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--scales",
        str(n_chunks),
        "--queries",
        str(n_queries),
        "--emit-row",
    ]
    if with_embeddings:
        cmd.append("--embed")
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, text=True, check=True)
    row: Dict[str, Any] = json.loads(proc.stdout)
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--scales",
        default="1000,10000,50000",
        help="comma-separated chunk counts (default 1000,10000,50000)",
    )
    ap.add_argument("--queries", type=int, default=15, help="queries per scale (default 15)")
    ap.add_argument(
        "--embed",
        action="store_true",
        help="store random vectors and inject a query vector, so the cosine+fusion path is measured",
    )
    ap.add_argument("--json", dest="json_out", help="write the rows as JSON here")
    ap.add_argument(
        "--emit-row",
        action="store_true",
        help=argparse.SUPPRESS,  # internal: single scale, JSON row on stdout (subprocess mode)
    )
    args = ap.parse_args()

    scales = [int(s.strip()) for s in args.scales.split(",") if s.strip()]

    if args.emit_row:
        if len(scales) != 1:
            print("--emit-row requires exactly one scale", file=sys.stderr)
            return 2
        row = bench_scale(scales[0], args.queries, with_embeddings=args.embed)
        json.dump(row, sys.stdout)
        return 0

    rows = []
    header = (
        f"{'chunks':>8} {'emb':>4} {'fetch p50':>10} {'fetch p95':>10} "
        f"{'rank p50':>9} {'rank p95':>9} {'TOTAL p50':>10} {'TOTAL p95':>10} {'peak RSS':>9}"
    )
    print(header)
    print("-" * len(header))
    for n in scales:
        if len(scales) == 1:
            # A single scale gets a fresh process anyway — no need to fork.
            row = bench_scale(n, args.queries, with_embeddings=args.embed)
        else:
            row = _bench_scale_in_subprocess(n, args.queries, with_embeddings=args.embed)
        rows.append(row)
        print(
            f"{row['chunks']:>8} {'y' if row['embeddings'] else 'n':>4} "
            f"{row['fetch_p50_ms']:>9.1f}ms {row['fetch_p95_ms']:>9.1f}ms "
            f"{row['rank_p50_ms']:>8.1f}ms {row['rank_p95_ms']:>8.1f}ms "
            f"{row['total_p50_ms']:>9.1f}ms {row['total_p95_ms']:>9.1f}ms "
            f"{row['peak_rss_mb']:>7.1f}MB"
        )
        sys.stdout.flush()

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=2)
        print(f"\nJSON written to {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
