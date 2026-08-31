"""Re-converge the merge-train-23 and semantic-layer lineages into one head.

Empty by design — a merge revision carries NO schema change. It exists
because merge train 23 and `mf/semantic-layer-v0` each extended the Alembic
chain in parallel, from a parent both already carried
(``0086_claims_audience``), so merging the two branches left the repository
with two heads:

- ``0088_ingest_runs_llm_usage``     — the train's corpus/facts chain
  (``0086_claims_audience`` -> ``0087_corpus_file_events`` -> here)
- ``0092_chat_messages_cache_tokens`` — the semantic-layer chain, already
  re-converged onto ``0086_claims_audience`` once by
  ``0091_merge_semantic_facts``

With two heads, ``alembic upgrade head`` refuses ("Multiple head revisions
are present for given argument 'head'"), which fails every Postgres-backed
test at fixture setup and would fail a real deployment's migrate step the
same way. Neither branch can see this on its own: each has exactly one head
in isolation, and the revision files never conflict textually.

Naming both as ``down_revision`` re-converges the graph: each branch's
revisions keep their own identity and order, and everything after this point
chains onto a single head again.

Chosen over renumbering the semantic-layer branch's ``0087_resource_source_tags``
/ ``0088_semantic_feedback`` and re-chaining the five revisions above them:
the revision IDs are distinct strings, so Alembic already treats the two
sides as separate revisions rather than a collision — the only real defect
is the pair of heads. Renumbering would rewrite published revisions' parents
under any instance that has already applied them, whereas a merge revision
is additive and safe to apply on top of either lineage. Same precedent as
``0091_merge_semantic_facts`` on this very branch.

Revision ID: 0093_merge_train23_semantic
Revises: 0088_ingest_runs_llm_usage, 0092_chat_messages_cache_tokens
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0093_merge_train23_semantic"
down_revision: str | Sequence[str] | None = (
    "0088_ingest_runs_llm_usage",
    "0092_chat_messages_cache_tokens",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op: this revision only re-converges the graph."""


def downgrade() -> None:
    """No-op: splitting the head back into two branches is the inverse."""
