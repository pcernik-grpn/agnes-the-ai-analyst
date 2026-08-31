"""Join the semantic-layer and facts migration lineages into one head.

Empty by design — a merge revision carries NO schema change. It exists
because `mf/semantic-layer-v0` and `integration` each extended the Alembic
chain from the same parent while running in parallel, so merging the two
branches left the repository with two heads:

- ``0086_claims_audience``      — integration's facts/ACL chain (claims.audience)
- ``0090_semantic_models_detach`` — the semantic-layer chain (detach/override columns)

With two heads, ``alembic upgrade head`` refuses ("Multiple head revisions
are present for given argument 'head'"), which fails every Postgres-backed
test at fixture setup and would fail a real deployment's migrate step the
same way. Naming both as ``down_revision`` re-converges the graph: each
branch's revisions keep their own identity and order, and everything after
this point chains onto a single head again.

Chosen over re-chaining ``0090`` onto ``0086`` (the fix ``0090``'s own
docstring describes for the in-branch case) because both lineages are
already published — rewriting a shipped revision's parent would change the
graph under any instance that has already applied it, whereas a merge
revision is additive and safe to apply on top of either lineage.

Revision ID: 0091_merge_semantic_facts
Revises: 0086_claims_audience, 0090_semantic_models_detach
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0091_merge_semantic_facts"
down_revision: str | Sequence[str] | None = (
    "0086_claims_audience",
    "0090_semantic_models_detach",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op: this revision only re-converges the graph."""


def downgrade() -> None:
    """No-op: splitting the head back into two branches is the inverse."""
