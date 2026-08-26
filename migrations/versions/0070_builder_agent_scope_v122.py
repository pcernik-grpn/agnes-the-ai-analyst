"""builder agent scope backfill — enforce what the /agents builder already showed

Mirrors DuckDB ``_v121_to_v122``. Data-only: no column change.

A ``/agents`` builder row (``agt_`` id prefix — see
0061_agent_status_backfill_v115 for why the prefix, not the slug, is the
discriminator) recorded the user's picks in the ``knowledge``/``plugins`` JSON
columns but left all four ``*_mode`` columns at the repository default
``'all'``. All-``'all'`` is the passthrough shape
(``src/agent_scope_intersection.py::agent_is_passthrough``), so such an agent
ran with its owner's ENTIRE stack regardless of what the page showed — the UI
promised a narrowing the runtime never applied. ``POST``/``PATCH``
``/api/agents`` now derive ``agent_scope`` from those columns and set the modes
to ``'selected'``; this migration does the same for rows created before that
fix.

Per row: ``knowledge`` ids become ``agent_scope`` rows typed by which registry
the id resolves in (data package / memory domain / collection), ``plugins`` ids
become ``('plugin', id)`` rows, and the four modes flip to ``'selected'``. Ids
resolving nowhere are skipped — an enforced-scope row that can never resolve is
indistinguishable from a typo and would only widen the diff an operator has to
audit.

A row with an empty declaration therefore ends up enforcing an EMPTY scope.
That is the honest reading of a builder agent showing "0 sources · 0 tools",
and it is the fail-closed direction; the owner widens it by picking sources in
the builder, which now writes scope.

Excluded: ``is_default`` (the seeded per-owner agent web chat is attributed to
— infrastructure that must keep passing the owner's own authority through) and
any row whose modes are already not all ``'all'`` (a governance-API agent, or a
builder agent already fixed via ``agnes agent scope set``) — touching those
would overwrite a deliberately-set scope with a re-derivation from columns the
governance surface never wrote.

Idempotent: after the flip the all-``'all'`` predicate no longer matches, so a
re-run is a no-op.

``downgrade()`` is deliberately a no-op: nothing here marks which rows this
step wrote, so reverting would also revert scope legitimately set afterwards —
and re-widening an agent to passthrough is the unsafe direction to guess in.

Revision ID: 0070_builder_scope_v122
Revises: 0069_grant_allow_mutating_v121
Create Date: 2026-08-23

Note on the revision id: ``alembic_version.version_num`` is ``VARCHAR(32)``,
so keep new ids short (see 0051_store_publisher_v104's note).
"""

from __future__ import annotations

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0070_builder_scope_v122"
down_revision: Union[str, None] = "0069_grant_allow_mutating_v121"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_MODE_COLS = ("tables_mode", "plugins_mode", "connections_mode", "memory_mode")


def _ids(raw) -> list:
    """The JSON id-list column as a clean list of non-blank strings."""
    try:
        val = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except (ValueError, TypeError):
        return []
    return [v.strip() for v in val if isinstance(v, str) and v.strip()] if isinstance(val, list) else []


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    if "agents" not in tables or "agent_scope" not in tables:
        return
    cols = {c["name"] for c in insp.get_columns("agents")}
    if not {"knowledge", "plugins", "is_default", *_MODE_COLS} <= cols:
        return

    rows = bind.execute(
        sa.text(
            r"""
            SELECT id, knowledge, plugins
              FROM agents
             WHERE id LIKE 'agt\_%' ESCAPE '\'
               AND NOT COALESCE(is_default, FALSE)
               AND COALESCE(tables_mode, 'all') = 'all'
               AND COALESCE(plugins_mode, 'all') = 'all'
               AND COALESCE(connections_mode, 'all') = 'all'
               AND COALESCE(memory_mode, 'all') = 'all'
            """
        )
    ).fetchall()
    if not rows:
        return

    # Registries a knowledge id may resolve in, probed in this order — the
    # same three the builder's Knowledge section is populated from.
    registries = [
        (item_type, table)
        for item_type, table in (
            ("data_package", "data_packages"),
            ("memory_domain", "memory_domains"),
            ("collection", "file_corpora"),
        )
        if table in tables
    ]

    insert = sa.text(
        "INSERT INTO agent_scope (agent_id, item_type, item_id) VALUES (:a, :t, :i) ON CONFLICT DO NOTHING"
    )
    for agent_id, knowledge_json, plugins_json in rows:
        pairs: list = []
        for item_id in _ids(knowledge_json):
            for item_type, table in registries:
                hit = bind.execute(
                    sa.text(f"SELECT 1 FROM {table} WHERE id = :i"),  # table name from the literal list above
                    {"i": item_id},
                ).fetchone()
                if hit:
                    pairs.append((item_type, item_id))
                    break
        pairs += [("plugin", p) for p in _ids(plugins_json)]
        for item_type, item_id in pairs:
            bind.execute(insert, {"a": agent_id, "t": item_type, "i": item_id})
        bind.execute(
            sa.text(
                """UPDATE agents
                      SET tables_mode = 'selected', plugins_mode = 'selected',
                          connections_mode = 'selected', memory_mode = 'selected',
                          updated_at = now()
                    WHERE id = :a"""
            ),
            {"a": agent_id},
        )


def downgrade() -> None:
    # See the module docstring: this backfill cannot be safely inverted.
    pass
