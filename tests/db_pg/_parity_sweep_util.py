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

    fastapi_app = create_app()
    _reregister_requires_pg_handler(fastapi_app)
    client = TestClient(fastapi_app)
    return client, create_access_token("admin1", "admin@test.com")


def _reregister_requires_pg_handler(fastapi_app) -> None:
    """Re-bind app/main's typed-501 handler to the *current*
    ``RequiresPostgresBackend`` class.

    ``build_seeded_client`` reloads ``src.repositories``, which rebinds
    ``RequiresPostgresBackend`` to a NEW class object. ``app.main`` imported
    the class once, at ITS import time, and ``create_app`` registers the
    501-translation handler against that original object; Starlette resolves
    handlers by walking the raised exception's MRO, which never contains the
    pre-reload class. So without this step, the first real PG-only exemption
    would raise the post-reload class, miss the handler, and fall through to
    the catch-all 500 — and ``assert_pg_only_exemptions_fail_clean`` would
    report a crash where production (which never reloads the module) answers
    the typed 501. Test-harness repair for a test-harness artifact: the
    production registration in ``app/main.py`` stays untouched.

    Regression: ``test_pg_only_route_exemption_mechanism.py::
    test_post_reload_raise_still_translates_to_typed_501``.
    """
    import app.main as app_main
    import src.repositories

    current_cls = src.repositories.RequiresPostgresBackend
    if current_cls in fastapi_app.exception_handlers:
        return  # app.main's binding is already the current class — nothing to do
    handler = fastapi_app.exception_handlers.get(app_main.RequiresPostgresBackend)
    assert handler is not None, (
        "app/main.py no longer registers an exception handler for "
        "RequiresPostgresBackend — the parity sweeps' fail-clean check "
        "depends on that typed 501 translation"
    )
    fastapi_app.add_exception_handler(current_cls, handler)


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


def diff_statuses(duck, pg, *, exempt: dict[str, str] | None = None):
    """Return ``{key: (duck_status, pg_status)}`` for keys that differ.

    ``exempt`` (A3 PG-first ratchet, see CLAUDE.md -> "Dual-backend
    discipline") maps a route key (``"METHOD path"``) to a one-line reason it
    is backed by a Postgres-only repository — it is EXPECTED to differ across
    backends (DuckDB has no implementation to resolve), so it is excluded
    from the strict diff here. That exclusion is only safe combined with
    :func:`assert_pg_only_exemptions_fail_clean`, which proves the DuckDB
    side fails *clean* — a TYPED 501, the translated
    ``RequiresPostgresBackend`` — rather than hiding an actual crash (or an
    unrelated 4xx that happens to also be an error status).
    """
    exempt = exempt or {}
    keys = set(duck) | set(pg)
    return {k: (duck.get(k), pg.get(k)) for k in keys if k not in exempt and duck.get(k) != pg.get(k)}


def assert_pg_only_exemptions_fail_clean(client, token, exempt: dict[str, str]):
    """For every ``exempt`` route (``{"METHOD path": reason}``), calling it
    against ``client`` (the DuckDB-backed ``TestClient``) must answer a
    TYPED clean failure — status ``501`` AND a JSON body with
    ``error == "requires_postgres_backend"`` (the exact
    ``RequiresPostgresBackend`` translation ``app/main.py`` performs) —
    never a raw crash, and never an unrelated 4xx (e.g. a 403/404 that fires
    before the PG-only repo is ever reached) passing silently just because
    it also happens to be an error status.

    Every exemption must also carry a non-empty ``reason`` — an
    undocumented exemption is itself a finding, since the whole point of the
    mechanism is that a reviewer can tell at a glance *why* a route is
    allowed to diverge.

    Call this alongside ``diff_statuses(..., exempt=exempt)`` in every sweep
    that accepts an exemption dict — the exemption itself proves nothing;
    this is what stops it from silently hiding a real bug.
    """
    auth = {"Authorization": f"Bearer {token}"}
    bad: dict[str, str] = {}
    for key, reason in exempt.items():
        if not reason or not reason.strip():
            bad[key] = "exemption has no reason recorded"
            continue
        method, _, path = key.partition(" ")
        try:
            if method == "GET":
                r = client.get(path, headers=auth, follow_redirects=False)
            else:
                r = client.request(method, path, json={}, headers=auth, follow_redirects=False)
        except Exception as exc:  # noqa: BLE001 — record as a failure, not a test crash
            bad[key] = f"transport error calling the route: {exc}"
            continue
        if r.status_code != 501:
            bad[key] = f"expected a clean 501, got {r.status_code}"
            continue
        try:
            body = r.json()
        except ValueError:
            bad[key] = f"501 but the body is not JSON: {r.text[:200]!r}"
            continue
        if body.get("error") != "requires_postgres_backend":
            bad[key] = f"501 but body['error'] = {body.get('error')!r}, expected 'requires_postgres_backend'"
    assert not bad, (
        "PG-only route exemption(s) did not fail clean on DuckDB (expected "
        "the typed RequiresPostgresBackend translation -- status 501 with "
        "body['error'] == 'requires_postgres_backend' -- got something "
        "else):\n" + "\n".join(f"  {k}: {v}" for k, v in sorted(bad.items()))
    )
