"""facts_ingest_runs.anonymization — the producer's per-batch anonymization
declaration (PG-only, A3 ratchet; column added to an already PG-only table,
per docs/migrations.md -> "Adding a PG-only feature").

Design spec §9/§9.2's anonymize-in-front pipeline marks a scope
`anonymize=true` in the connect wizard (`app/api/admin_sharepoint.py`), but
Agnes had no record of whether a producer actually ran a batch through the
anonymizer before uploading — the wizard badge could only ever show the
WISH, never a fact. This column lets `POST /api/facts/ingest` accept an
OPTIONAL `anonymization: {declared, scopes: {corpus_id: {docs_anonymized,
docs_skipped}}}` block and persist it alongside the rest of the run report
(`src/repositories/facts_ingest_runs_pg.py`), so
`GET /api/admin/sharepoint/connections/{id}/scopes`
(`app/api/admin_sharepoint.py::_latest_run_anonymized_corpus_ids`) and the
source card can distinguish "requested" from "declared" (spec §13.2: "on a
collection detail it is a state plus a named batch task, never a toggle").

Nullable-free JSONB with a `'{}'::jsonb` default, same pattern as every
other list/dict column on this table — a producer that never anonymizes
omits the field entirely and every existing row backfills to `{}`.

Revision ID: 0080_ingest_runs_anonymize
Revises: 0079_sso_login
Create Date: 2026-08-28

Note: the revision id is deliberately SHORTER than the migration's subject
(``facts_ingest_runs_anonymization`` would be the natural name) — Alembic's
own bookkeeping table, ``alembic_version.version_num``, is ``VARCHAR(32)``
and a longer id fails at upgrade time with `StringDataRightTruncation`, not
at authoring time.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0080_ingest_runs_anonymize"
down_revision: Union[str, None] = "0079_sso_login"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "facts_ingest_runs",
        sa.Column("anonymization", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("facts_ingest_runs", "anonymization")
