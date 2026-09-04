"""SQLAlchemy model behind ``facts_llm_cache`` — a content-hash LLM
response cache for the fact-extraction stage (cost-levers spec
2026-09-02, lever B).

PG-ONLY (A3 PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend
discipline"): this table lands after the DuckDB app-state backend was
frozen, so there is no ``src/db.py`` ladder step and no DuckDB repository
sibling. A DuckDB-backed instance simply runs the fact-extraction pass
without a cache — see ``connectors/sharepoint/facts_extraction.py``'s
``_resolve_llm_cache`` — the pass degrades to "no cache" (one log line),
never a crash.

Consultancies routinely keep several byte-identical copies of the same
document (v1/v2/final in different folders). ``cache_key`` is a sha256 of
the document's content hash + the model id + the effective prompt/ontology
fingerprint — the same three-way identity ``is_up_to_date`` already uses to
decide whether a document needs re-extraction — plus a call-kind suffix so
the ONE corrective retry's response is cached under a key distinct from the
first-pass response. A hit skips the API call entirely: the stored reply
runs through the exact same parse -> verbatim-gate -> ingest path a fresh
call's reply would.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import DateTime, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class FactsLlmCache(Base):
    __tablename__ = "facts_llm_cache"
    __table_args__ = (sa.Index("idx_facts_llm_cache_sha256", "sha256"),)

    cache_key: Mapped[str] = mapped_column(String, primary_key=True)
    sha256: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    #: The raw LLM reply, exactly what ``_Extractor.call()`` returns —
    #: ``{"text": <reply>}`` rather than a bare string column so a future
    #: field (e.g. ``stop_reason``) can be added without a migration.
    response: Mapped[dict] = mapped_column(JSONB, nullable=False)
    #: Best-effort: the token usage the ORIGINAL (cache-miss) call
    #: recorded, for an operator's own "what did this save" arithmetic.
    #: Never read back into a re-run's own accounting — a cache HIT is
    #: always accounted as zero API tokens, see ``facts_usage.cache_hits``.
    usage: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=sa.text("now()"), nullable=True
    )
