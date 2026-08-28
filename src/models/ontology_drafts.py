"""SQLAlchemy model for ``ontology_drafts`` (PG-only, A3 ratchet — no
DuckDB sibling).

The ontology builder's working state (spec §13.2 "Ontology builder"):
node/edge types the admin is drafting, the document sample they picked for
dry-run, and the last translation leftover report — persisted so a page
reload does not lose work. Populating this row is never itself a write to
the live ontology: the row is filled by conversation/import and edited
section-by-section (``PUT /api/admin/ontology/drafts/{id}``) with zero
effect on ``semantic_models``. Only the explicit "Save" action
(``POST /api/admin/ontology/drafts/{id}/save``) translates the draft
(``scripts/ontology/import_ontology.py::translate_ontology``), validates it
against the vendored Ossie schema, and creates/replaces the live semantic
model — the one write the draft's own mutation never performs.

See docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md
§13.2 and §11 (attribute-free / new-type-is-a-re-extraction economics).
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class OntologyDraft(Base):
    __tablename__ = "ontology_drafts"
    __table_args__ = (sa.Index("idx_ontology_drafts_created_by", "created_by"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # node_types / edge_types mirror the producer ontology.yaml shape
    # (``translate_ontology``'s input contract) so Save can hand the draft
    # straight to the translator with no reshaping step.
    node_types: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False)
    edge_types: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False)
    # [{"collection_id", "file_id", "name"}, ...] — references only, never
    # document content (spec §13.2 "document sample").
    document_sample: Mapped[list] = mapped_column(JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False)
    # Rendered text of the last translate_ontology() TranslationReport, shown
    # in the "source" section (spec §13.2 "import + translation leftovers").
    leftover_report: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Last dry-run response ({"facts", "edges", "not_captured"}), so
    # reopening the draft shows the previous result without re-running it.
    dry_run_results: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Set once Save succeeds — the slug of the semantic model this draft
    # became. Non-null means "already saved"; the draft itself is untouched
    # by further edits (Save-only-write), so this can go stale relative to a
    # later edit, which the builder UI surfaces as "unsaved changes".
    saved_model_slug: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=sa.text("now()"), nullable=True
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=sa.text("now()"), nullable=True
    )
