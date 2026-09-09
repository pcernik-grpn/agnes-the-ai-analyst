"""idx_jobs_idem_pattern — a pattern-ops partner to idx_jobs_idem, PG-only.

2026-09-07 review finding on ``JobsPgRepository.list_by_idempotency_prefix``
(added this same wave for ``connectors.sharepoint.crawler.
_shard_children_still_live``): its docstring claims the ``idempotency_key
LIKE :prefix ESCAPE '\\'`` query is ``idx_jobs_idem``-backed, but a plain
B-tree index only supports Postgres's internal LIKE-to-range rewrite when
the column's collation is ``C`` (verified live: the test database — built
with ``C``/``C.UTF-8`` locale — rewrites the query into an
``idx_jobs_idem`` index range scan automatically; a production instance
initialized with a locale-aware collation, e.g. ``en_US.UTF-8``, would NOT
get that rewrite from the same plain index, and the query would fall back
to a sequential scan as job history grows — silently reproducing the exact
"fleet-wide scan misses a busy connection's own row" class of bug this
whole ``list_by_idempotency_prefix`` mechanism exists to avoid, just one
layer down, at the SQL engine instead of the application).

``text_pattern_ops`` indexes compare byte values directly for pattern-
matching operators (``LIKE``/``~~``) specifically, independent of the
column's actual collation, and Postgres's planner recognizes one is
available for a ``LIKE 'prefix%'`` predicate under ANY locale — the
standard fix per the Postgres docs' own indexes-and-collations guidance.
Mirrors ``idx_jobs_idem``'s own partial predicate exactly (``idempotency_key
IS NOT NULL AND status IN ('queued', 'running')``) — the only rows
``list_by_idempotency_prefix`` is ever called to search among (its sole
caller passes ``statuses=("queued", "running")``) — so this stays a small,
cheap index rather than covering the whole (unbounded, historical) table.

PG-ONLY (A3 PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend discipline").
``jobs``/``jobs_pg`` is an existing pre-A3 pair, so a schema change to its
Postgres side alone is allowed under the ratchet's "existing pairs stay
maintained" rule; there is deliberately no matching ``_vN_to_v(N+1)`` step
in ``src/db.py`` — DuckDB's own LIKE-prefix matching is not locale-gated
the same way, so the DuckDB sibling needs no equivalent.

``jobs`` is a queue table, not an analytics one — nowhere near
``0106_hot_path_indexes``'s multi-million-row tables — so this builds
unconditionally, same as that migration's smaller ``fact_aliases`` index.

Revision ID: 0112_jobs_idem_pattern_index
Revises: 0111_fact_collection_type_counts
Create Date: 2026-09-07
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0112_jobs_idem_pattern_index"
down_revision: Union[str, None] = "0111_fact_collection_type_counts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "idx_jobs_idem_pattern"


def upgrade() -> None:
    op.create_index(
        INDEX_NAME,
        "jobs",
        ["idempotency_key"],
        if_not_exists=True,
        postgresql_ops={"idempotency_key": "text_pattern_ops"},
        postgresql_where=sa.text("idempotency_key IS NOT NULL AND status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="jobs", if_exists=True)
