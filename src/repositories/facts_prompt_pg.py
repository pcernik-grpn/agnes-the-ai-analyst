"""Postgres-only repository for the fact-extraction prompt override.

Storage is the SHARED ``instance_templates`` table — the same table the
workspace ``CLAUDE.md`` (``key='claude_md'``) and the install prompt
(``key='welcome'``) already live in — under ``key='facts_extraction'``. The
table's key column is free-form, so this adds a row, not a schema change:
no Alembic revision, no ``src/db.py`` step, nothing to migrate.

PG-first ratchet (A3): a repo added after the freeze has no DuckDB sibling.
Reach it only through ``src.repositories.facts_prompt_repo()``; on a
DuckDB-backed instance that factory raises ``RequiresPostgresBackend``,
which :func:`connectors.sharepoint.facts_prompt._stored_override` reads as
"this instance has nowhere to store an override" and falls back to the
built-in default — the only place that exception is swallowed.

Interface is deliberately the one ``app/api/prompts.py`` already calls on
its two sibling repos (``get``/``get_meta``/``set``/``reset``), so adding
this prompt to that endpoint's ``kind`` vocabulary needed no new route and
no special case in the handler. ``set_source_mode``/``bind_git`` are
absent on purpose rather than implemented as no-ops: this prompt is not
bindable to an Initial Workspace Template file (it is not a workspace file
and has no seed path), and ``app/api/prompts.py`` refuses those two
actions for this kind BEFORE reaching the repo. A silently-accepting stub
would let a future edit bind a prompt that nothing can resolve.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

#: The ``instance_templates`` row this repository owns. Mirrors
#: ``connectors.sharepoint.facts_prompt.PROMPT_KEY`` — asserted equal by
#: ``tests/test_facts_extraction_prompt.py`` so the two can never drift.
_KEY = "facts_extraction"

#: This prompt is never git-bound (see the module docstring), so every read
#: reports the only mode it has.
_SOURCE_MODE = "editor"


class FactsPromptPgRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get(self) -> dict[str, Any]:
        """``{content, updated_at, updated_by}`` — ``content`` is ``None``
        when no admin override has been stored, which is what makes the
        built-in default the effective prompt."""
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT content, updated_at, updated_by FROM instance_templates WHERE key = :k"),
                {"k": _KEY},
            ).first()
        if row is None:
            return {"content": None, "updated_at": None, "updated_by": None}
        return {"content": row[0], "updated_at": row[1], "updated_by": row[2]}

    def get_meta(self) -> dict[str, Any]:
        """The shape ``app/api/prompts.py`` reads. ``source_mode`` is always
        ``editor`` and ``git_path``/``base_sha`` always ``None`` — stated as
        constants rather than read from the row, because this prompt has no
        git binding to report and echoing whatever a stray column held would
        invent one."""
        row = self.get()
        return {
            "content": row["content"],
            "source_mode": _SOURCE_MODE,
            "git_path": None,
            "base_sha": None,
            "updated_at": row["updated_at"],
            "updated_by": row["updated_by"],
        }

    def set(self, content: str, *, updated_by: str) -> None:
        """Store (or replace) the admin override. The previous text is kept
        in ``previous_content``, same as the sibling template repos, so a
        bad edit is recoverable from the row itself."""
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO instance_templates (key, content, source_mode, updated_at, updated_by)
                       VALUES (:k, :content, :mode, :now, :ub)
                       ON CONFLICT (key) DO UPDATE SET
                         previous_content = instance_templates.content,
                         content = EXCLUDED.content,
                         updated_at = EXCLUDED.updated_at,
                         updated_by = EXCLUDED.updated_by"""
                ),
                {"k": _KEY, "content": content, "mode": _SOURCE_MODE, "now": now, "ub": updated_by},
            )

    def reset(self, *, updated_by: str) -> None:
        """Drop the override — the built-in default becomes effective again
        and the next run reports ``origin: builtin``."""
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """UPDATE instance_templates
                       SET previous_content = content,
                           content = NULL,
                           updated_at = :now,
                           updated_by = :ub
                       WHERE key = :k"""
                ),
                {"now": now, "ub": updated_by, "k": _KEY},
            )
