"""extraction_runs — per-run observability for the built-in extraction
pipeline (2026-08-31 extraction-observability-ui-design §7.1).

Modelled on ``0078_facts_ingest_runs.py`` and existing for the same reason:
so the SharePoint source card can answer "what is running right now" and
"what did the last N runs do" without an admin having to have been watching
the worker's log. The crawl already emits a 30-field run report
(``connectors/sharepoint/crawler.py::CrawlStats.report``) and already
checkpoints once per delta page; this table is the second destination for
that checkpoint, and the durable home of the final report.

Why not ``jobs``: ``jobs`` owns lifecycle and already carries the FINAL
report in ``payload_json.result``, but it cannot carry live PROGRESS — a
mutable progress column would mean touching a frozen DuckDB<->PG repo pair
on both sides plus its contract test, and it has no per-connection index.
``jobs`` stays the lifecycle owner; ``extraction_runs.job_id`` joins them.

Why not the crawl state file: ``state_path()`` is under ``DATA_DIR``, which
the Compose topology happens to share between ``app`` and
``extraction-worker`` — a deployment coincidence, not a contract, and it
breaks the moment a worker runs on another host.

PG-first ratchet (A3): brand-new app-state table, Alembic-only — no matching
DuckDB ``_vN_to_v(N+1)`` step, ``SCHEMA_VERSION`` does not move.

Revision ID: 0094_extraction_runs
Revises: 0093_merge_train23_semantic
Create Date: 2026-08-31
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0094_extraction_runs"
down_revision: Union[str, None] = "0093_merge_train23_semantic"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "extraction_runs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("connection_id", sa.String(), nullable=False),
        # The `jobs` row this run belongs to, when the caller knows it. The
        # builtin crawl is invoked with the job's PAYLOAD, not its id, so
        # this is nullable rather than a FK — a run recorded without one is
        # still a complete run report, and a wrong-looking join is worse
        # than an honest null.
        sa.Column("job_id", sa.String(), nullable=True),
        # running | done | interrupted | failed. `interrupted` is its own
        # outcome, not a flavour of failure: the run ingested what it
        # ingested and the next one resumes from the persisted cTags.
        sa.Column("status", sa.String(), nullable=False, server_default="running"),
        sa.Column("phase", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        # What the UI's "as of" caption prints — the last checkpoint this
        # run wrote, never "now".
        sa.Column("checkpoint_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("files_seen", sa.Integer(), server_default="0", nullable=False),
        sa.Column("files_done", sa.Integer(), server_default="0", nullable=False),
        # False while the delta enumeration can still raise `files_seen`.
        # Recorded, but deliberately NOT turned into a fraction anywhere:
        # this crawl enumerates and processes in lockstep per delta page, so
        # seen and done are equal at every checkpoint and a percentage over
        # them would be arithmetic dressed up as knowledge. Absolute
        # counters only, everywhere.
        sa.Column("enumeration_done", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        # The final `CrawlStats.report()` — `{}` while the run is live.
        sa.Column("report", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        # Live counters at the last checkpoint (a subset of the same shape).
        sa.Column("progress", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        # LLM token usage, when a detector spends any: {model, calls,
        # input_tokens, output_tokens, cache_creation_input_tokens,
        # cache_read_input_tokens}. `{}` means "no tokens spent", which is
        # a different claim from "$0.00" and must stay tellable apart.
        sa.Column("usage", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        # Capped list of {path, reason, detail} plus a total, so a truncated
        # list is visibly truncated rather than silently short.
        sa.Column("skips", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_extraction_runs_connection_started",
        "extraction_runs",
        ["connection_id", "started_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_extraction_runs_connection_started", "extraction_runs")
    op.drop_table("extraction_runs")
