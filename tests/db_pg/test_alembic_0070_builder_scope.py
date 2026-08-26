"""Alembic 0070 (DuckDB v122) — builder-agent scope backfill.

Mirrors ``tests/test_db_schema_version.py``'s ``_v121_to_v122`` coverage for
the Postgres ladder: a builder row (``agt_`` prefix) still sitting on the
all-``'all'`` passthrough shape has its ``knowledge``/``plugins``
declaration turned into ``agent_scope`` rows and its four modes flipped to
``'selected'``; the seeded default agent, a governance-created row, and any
agent whose scope was already set deliberately are left alone.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]

_PREV = "0069_grant_allow_mutating_v121"
_THIS = "0070_builder_scope_v122"


def _alembic_config(db_url: str):
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = db_url
    return cfg


def _builder_id() -> str:
    """A builder-created row's id shape — ``app/api/agents.py::create_agent``
    always mints ``"agt_" + uuid4().hex``."""
    return "agt_" + uuid.uuid4().hex


def _insert_agent(
    conn,
    *,
    id,
    slug,
    knowledge=(),
    plugins=(),
    modes="all",
    is_default=False,
):
    conn.execute(
        sa.text(
            "INSERT INTO agents (id, owner_user_id, name, slug, knowledge, plugins, "
            "tables_mode, plugins_mode, connections_mode, memory_mode, is_default, "
            "created_at, updated_at) "
            "VALUES (:id, 'u1', :slug, :slug, :knowledge, :plugins, "
            ":m, :m, :m, :m, :is_default, now(), now())"
        ),
        {
            "id": id,
            "slug": slug,
            "knowledge": json.dumps(list(knowledge)),
            "plugins": json.dumps(list(plugins)),
            "m": modes,
            "is_default": is_default,
        },
    )


def _insert_package(conn, pkg_id: str) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO data_packages (id, name, slug, created_at, updated_at) VALUES (:id, :id, :id, now(), now())"
        ),
        {"id": pkg_id},
    )


def _modes(conn, agent_id: str) -> set:
    row = conn.execute(
        sa.text("SELECT tables_mode, plugins_mode, connections_mode, memory_mode FROM agents WHERE id = :id"),
        {"id": agent_id},
    ).one()
    return set(row)


def _scope(conn, agent_id: str) -> set:
    return {
        (r[0], r[1])
        for r in conn.execute(
            sa.text("SELECT item_type, item_id FROM agent_scope WHERE agent_id = :id"),
            {"id": agent_id},
        ).all()
    }


def test_0070_turns_a_builder_declaration_into_enforced_scope(pg_engine):
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, _PREV)

    agent_id = _builder_id()
    pkg_id = str(uuid.uuid4())
    with pg_engine.begin() as conn:
        _insert_package(conn, pkg_id)
        _insert_agent(conn, id=agent_id, slug="scoped", knowledge=[pkg_id], plugins=["plug-1"])

    command.upgrade(cfg, _THIS)

    with pg_engine.connect() as conn:
        assert _modes(conn, agent_id) == {"selected"}
        assert _scope(conn, agent_id) == {("data_package", pkg_id), ("plugin", "plug-1")}


def test_0070_leaves_the_default_agent_and_governance_rows_alone(pg_engine):
    """The seeded default agent is web chat's own attribution row — it must
    keep passing the owner's authority through. A governance-created row
    (bare uuid) was never described by the builder's JSON columns."""
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, _PREV)

    default_id = str(uuid.uuid4())
    governance_id = str(uuid.uuid4())
    with pg_engine.begin() as conn:
        _insert_agent(conn, id=default_id, slug="default", is_default=True)
        _insert_agent(conn, id=governance_id, slug="governance")

    command.upgrade(cfg, _THIS)

    with pg_engine.connect() as conn:
        assert _modes(conn, default_id) == {"all"}
        assert _modes(conn, governance_id) == {"all"}
        assert _scope(conn, default_id) == set()


def test_0070_does_not_overwrite_a_deliberately_set_scope(pg_engine):
    """A builder agent already narrowed by hand (``agnes agent scope set``)
    is out of the cohort: re-deriving would replace a real scope with one
    read off columns the governance surface never wrote."""
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, _PREV)

    agent_id = _builder_id()
    pkg_id = str(uuid.uuid4())
    with pg_engine.begin() as conn:
        _insert_package(conn, pkg_id)
        # tables_mode already 'selected' → not all-'all', so out of scope.
        _insert_agent(conn, id=agent_id, slug="hand-scoped", knowledge=[pkg_id], modes="selected")
        conn.execute(
            sa.text("INSERT INTO agent_scope (agent_id, item_type, item_id) VALUES (:a, 'table', 't-existing')"),
            {"a": agent_id},
        )

    command.upgrade(cfg, _THIS)

    with pg_engine.connect() as conn:
        assert _scope(conn, agent_id) == {("table", "t-existing")}


def test_0070_skips_ids_that_resolve_nowhere(pg_engine):
    """An id matching no registry becomes no scope row — an enforced-scope
    row that can never resolve is indistinguishable from a typo."""
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, _PREV)

    agent_id = _builder_id()
    with pg_engine.begin() as conn:
        _insert_agent(conn, id=agent_id, slug="ghost", knowledge=["no-such-resource"])

    command.upgrade(cfg, _THIS)

    with pg_engine.connect() as conn:
        assert _scope(conn, agent_id) == set()
        # Still flipped off passthrough: an empty declaration means the agent
        # reaches nothing, which is the fail-closed reading of "0 sources".
        assert _modes(conn, agent_id) == {"selected"}


def test_0070_is_idempotent(pg_engine):
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, _PREV)

    agent_id = _builder_id()
    pkg_id = str(uuid.uuid4())
    with pg_engine.begin() as conn:
        _insert_package(conn, pkg_id)
        _insert_agent(conn, id=agent_id, slug="scoped", knowledge=[pkg_id])

    command.upgrade(cfg, _THIS)
    command.downgrade(cfg, _PREV)
    command.upgrade(cfg, _THIS)  # must not raise on the second pass

    with pg_engine.connect() as conn:
        assert _scope(conn, agent_id) == {("data_package", pkg_id)}


def test_0070_noops_gracefully_when_agents_table_is_absent(pg_engine):
    """Alembic can run before the app's first boot — a missing table must
    not raise (mirrors 0061's guard)."""
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, _PREV)

    with pg_engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE IF EXISTS agents CASCADE"))

    command.upgrade(cfg, _THIS)  # must not raise


def test_0070_downgrade_is_a_documented_noop(pg_engine):
    """Nothing marks which rows this step wrote, and re-widening an agent to
    passthrough is the unsafe direction to guess in."""
    from alembic import command

    cfg = _alembic_config(str(pg_engine.url))
    command.upgrade(cfg, _PREV)

    agent_id = _builder_id()
    with pg_engine.begin() as conn:
        _insert_agent(conn, id=agent_id, slug="scoped")

    command.upgrade(cfg, _THIS)
    command.downgrade(cfg, _PREV)

    with pg_engine.connect() as conn:
        assert _modes(conn, agent_id) == {"selected"}, "downgrade must not re-widen an agent to passthrough"
