"""Backfill ``corpus_chunks.tsv`` — the stored tokenized body the retrieval
ranking reads (migration ``0114_corpus_chunks_tsv``) — on an instance whose
table was too large for the migration to populate in place.

Postgres only. Run it once, off-peak, from inside the app container (where
the database URL — ``DATABASE_URL`` or the legacy ``AGNES_DB_URL`` — is
set)::

    python scripts/backfill_corpus_chunks_tsv.py [--batch-size 5000] [--sleep 0.0]

Walks the table by primary key in batches, one short transaction per batch —
never a long lock, never a long-running transaction pinning vacuum — and
only touches rows whose ``tsv`` is still NULL, so it is idempotent: safe to
interrupt and re-run, and a no-op once everything is populated. Search keeps
working throughout (a not-yet-backfilled row ranks via the per-row
``COALESCE`` fallback in ``CorpusChunksPgRepository.search_candidates``);
``--sleep`` throttles the batches to spare a live crawl. Expect the table's
heap to grow by up to the size of the rows rewritten until autovacuum
reclaims the old row versions.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

import sqlalchemy as sa
from sqlalchemy.engine import Engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 5000
_PROGRESS_EVERY_BATCHES = 20

# The same tokenizer config the WHERE expression, the ``0101`` GIN index and
# ``CorpusChunksPgRepository`` use — a stored vector built with any other
# config would rank differently from the fallback it stands in for.
# ``text IS NOT NULL`` on both: ``to_tsvector('simple', NULL)`` is NULL, so a
# chunk with no text can never gain a vector — rewriting it would churn the
# heap for nothing and count as "updated" without changing anything.
_NEXT_BATCH_SQL = sa.text(
    "SELECT id FROM corpus_chunks WHERE id > :after AND tsv IS NULL AND text IS NOT NULL ORDER BY id LIMIT :batch_size"
)
_UPDATE_BATCH_SQL = sa.text(
    "UPDATE corpus_chunks SET tsv = to_tsvector('simple', text) "
    "WHERE id = ANY(:ids) AND tsv IS NULL AND text IS NOT NULL"
).bindparams(sa.bindparam("ids", type_=sa.ARRAY(sa.String)))


def backfill(engine: Engine, *, batch_size: int = DEFAULT_BATCH_SIZE, sleep_seconds: float = 0.0) -> int:
    """Populate every NULL ``tsv`` in ``corpus_chunks`` whose ``text`` is not
    NULL; return the number of rows updated. Keyset-paginated by ``id`` (the
    primary key) so each batch is an index-ordered slice, never a rescan of
    already-visited rows."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    after = ""
    total = 0
    batches = 0
    started = time.monotonic()
    while True:
        with engine.connect() as conn:
            ids = [row[0] for row in conn.execute(_NEXT_BATCH_SQL, {"after": after, "batch_size": batch_size}).all()]
        if not ids:
            break
        with engine.begin() as conn:
            total += conn.execute(_UPDATE_BATCH_SQL, {"ids": ids}).rowcount or 0
        after = ids[-1]
        batches += 1
        if batches % _PROGRESS_EVERY_BATCHES == 0:
            log.info(
                "backfill_corpus_chunks_tsv: %d rows updated in %d batches (%.0fs), last id %s",
                total,
                batches,
                time.monotonic() - started,
                after,
            )
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    log.info(
        "backfill_corpus_chunks_tsv: done — %d rows updated in %d batches (%.0fs)",
        total,
        batches,
        time.monotonic() - started,
    )
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="rows per transaction")
    parser.add_argument("--sleep", type=float, default=0.0, help="seconds to pause between batches")
    args = parser.parse_args(argv)

    if not (os.environ.get("DATABASE_URL") or os.environ.get("AGNES_DB_URL")):
        log.error(
            "no Postgres URL in the environment (DATABASE_URL / AGNES_DB_URL) — "
            "this backfill applies to the Postgres app-state backend only"
        )
        return 2

    from src.db_pg import get_engine

    backfill(get_engine(), batch_size=args.batch_size, sleep_seconds=args.sleep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
