"""Shared helper for the cross-backend status-parity sweeps.

The sweeps build a fully-seeded ``TestClient`` on each backend and compare the
HTTP status every parameter-free route returns. A status that *differs* between
DuckDB and Postgres (e.g. 200 vs 302, or 200 vs 500) is the signature of a
handler that reads state off a raw ``Depends(_get_db)`` connection — the
backend-split class the static ``test_backend_split_guard.py`` ratchet can't see
(it only scans ``get_system_db()`` callers + direct repo instantiation).

Why both backends are driven from ONE test (not a parametrized fixture + a
module-level result dict): the dict pattern silently dies under
``pytest -n auto`` — each xdist worker is a separate process, so the comparison
test sees an empty dict. Instead we collect both backends sequentially in a
single test process. The repo factory (`src.repositories.use_pg`) reads the
backend decision live on every ``*_repo()`` call, so flipping ``AGNES_DB_URL``
between phases re-routes correctly; we fully collect one backend before
switching to the next.

Comparing (rather than asserting no-5xx) is deliberate: several routes 5xx
identically on both backends in the bare TestClient harness (e.g. handlers that
touch ``app.state`` slots only populated by the lifespan). Those are not
backend-split bugs — a diff ignores them; a flat no-5xx assertion would flag
them as false positives.
"""

from __future__ import annotations

import importlib
import uuid as _uuid
from pathlib import Path


def _alembic_upgrade(pg_engine) -> None:
    from alembic import command
    from alembic.config import Config

    repo_root = Path(__file__).resolve().parents[2]
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")


def _seed_pg_system_groups(pg_engine) -> None:
    import sqlalchemy as sa

    with pg_engine.begin() as conn:
        for name, desc in (
            ("Admin", "System: full access to all data and admin actions"),
            ("Everyone", "System: default group every user is implicitly a member of"),
        ):
            conn.execute(
                sa.text(
                    "INSERT INTO user_groups (id, name, description, is_system, created_by) "
                    "VALUES (:id, :name, :desc, TRUE, 'system:seed') "
                    "ON CONFLICT (name) DO UPDATE SET is_system = TRUE"
                ),
                {"id": _uuid.uuid4().hex, "name": name, "desc": desc},
            )


def build_seeded_client(backend, tmp_path, monkeypatch, pg_engine):
    """Configure ``backend`` ('duckdb'|'pg'), seed admin+analyst users, and
    return ``(TestClient, admin_token)``.

    Mirrors the ``seeded_app_both`` fixture but as a plain callable so a single
    test can build both backends in sequence.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
    for sub in ("extracts", "analytics", "state", "notifications"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)

    if backend == "pg":
        _alembic_upgrade(pg_engine)
        _seed_pg_system_groups(pg_engine)
        monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
        import src.db_pg as db_pg

        db_pg.dispose()
    else:
        monkeypatch.delenv("AGNES_DB_URL", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)

    # Pick up the env change on the next factory call.
    import src.repositories

    importlib.reload(src.repositories)

    # Reset the system-DB singleton so it reopens under the current DATA_DIR.
    from src.db import close_system_db, get_system_db

    close_system_db()
    if backend == "duckdb":
        get_system_db()  # triggers _ensure_schema + _seed_system_groups

    from src.repositories import users_repo, user_group_members_repo

    u = users_repo()
    u.create(id="admin1", email="admin@test.com", name="Admin")
    u.create(id="analyst1", email="analyst@test.com", name="Analyst")

    if backend == "duckdb":
        admin_gid = get_system_db().execute("SELECT id FROM user_groups WHERE name = 'Admin'").fetchone()[0]
    else:
        import sqlalchemy as sa
        from src.db_pg import get_engine

        with get_engine().connect() as conn:
            admin_gid = conn.execute(sa.text("SELECT id FROM user_groups WHERE name = 'Admin'")).scalar()
    user_group_members_repo().add_member("admin1", admin_gid, source="system_seed")

    from app.auth.jwt import create_access_token
    from app.main import create_app
    from fastapi.testclient import TestClient

    client = TestClient(create_app())
    return client, create_access_token("admin1", "admin@test.com")


def collect_statuses(client, token, *, methods, skip_substr=()):
    """Return ``{"METHOD path": status}`` for every parameter-free route whose
    methods intersect ``methods`` and whose path contains no ``skip_substr``."""
    auth = {"Authorization": f"Bearer {token}"}
    seen: dict[str, int] = {}
    want = set(methods)
    for route in client.app.routes:
        path = getattr(route, "path", "") or ""
        if "{" in path or any(s in path for s in skip_substr):
            continue
        route_methods = set(getattr(route, "methods", None) or set())
        for method in sorted(route_methods & want):
            key = f"{method} {path}"
            try:
                if method == "GET":
                    r = client.get(path, headers=auth, follow_redirects=False)
                else:
                    r = client.request(method, path, json={}, headers=auth, follow_redirects=False)
                seen[key] = r.status_code
            except Exception:  # noqa: BLE001 — record transport failure as a sentinel
                seen[key] = -1
    return seen


def diff_statuses(duck, pg, *, exempt=frozenset()):
    """Return ``{key: (duck_status, pg_status)}`` for keys that differ.

    ``exempt`` (A3 PG-first ratchet, see CLAUDE.md -> "Dual-backend
    discipline") names routes backed by a Postgres-only repository — they
    are EXPECTED to differ across backends (DuckDB has no implementation to
    resolve), so they are excluded from the strict diff here. That
    exclusion is only safe combined with :func:`assert_pg_only_exemptions_fail_clean`,
    which proves the DuckDB side fails *clean* (4xx/501, the translated
    ``RequiresPostgresBackend``) rather than hiding an actual crash.
    """
    keys = set(duck) | set(pg)
    return {k: (duck.get(k), pg.get(k)) for k in keys if k not in exempt and duck.get(k) != pg.get(k)}


def assert_pg_only_exemptions_fail_clean(duck_statuses, exempt):
    """For every ``exempt`` route, the DuckDB-backend status must be a clean
    4xx or 501 (a ``RequiresPostgresBackend`` translated to an HTTP error by
    ``app/main.py``), never a raw 5xx crash.

    Call this alongside ``diff_statuses(..., exempt=exempt)`` in every sweep
    that accepts an exemption set — the exemption itself does not verify
    anything; this is what stops it from silently hiding a real 500. A route
    missing from ``duck_statuses`` entirely (e.g. skipped by a ``skip_substr``
    filter) is not checked here — it simply never ran.
    """
    dirty = {
        k: duck_statuses[k]
        for k in exempt
        if k in duck_statuses and not (400 <= duck_statuses[k] < 500 or duck_statuses[k] == 501)
    }
    assert not dirty, (
        "PG-only route exemption(s) did not fail clean on DuckDB (expected a "
        "4xx/501 -- a RequiresPostgresBackend translated to an HTTP error -- "
        "got a raw crash instead):\n" + "\n".join(f"  {k}: {v}" for k, v in sorted(dirty.items()))
    )
