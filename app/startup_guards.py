"""Refuse unsafe multi-process topologies at boot.

Multi-process = role split (AGNES_ROLE != all) OR UVICORN_WORKERS > 1 OR
coordination.backend=redis. The third trigger closes a deferred wave-1
finding: configuring the Redis coordination backend is itself a
declaration of multi-process intent, even in an otherwise all-in-one
topology (e.g. staging a redis-backed rollout ahead of the actual role
split) — such a deployment must ALSO satisfy the other multi-process
requirements below, not just gain the coordination backend.

Multi-process deployments require: Postgres app-state, explicit shared
secrets, a Redis coordination backend, and — when opted into
``analytics.backend=ducklake`` — an explicit Postgres DuckLake catalog
DSN (a DuckDB-file catalog is hard single-process; see
``src/analytics_backend.py``). Single-process ``all`` mode with the
default memory coordination backend (and, if set, a file-catalog
DuckLake backend) has no new requirements. Spec §3.2/§3.4/§3.7.
"""

import os

from app.roles import active_roles, is_all_in_one


class DeploymentConfigError(RuntimeError):
    pass


def _use_pg() -> bool:
    from src.repositories import use_pg

    return use_pg()


def _coordination_backend() -> str:
    """Effective coordination backend — thin wrapper around
    :func:`app.coordination.factory.resolve_backend_name` (env overrides
    instance.yaml) so this guard reacts to the exact backend the process
    will actually use, not just the yaml-only view of it.

    Kept as its own module-level function (rather than inlining the call at
    every use site) so tests can monkeypatch
    ``app.startup_guards._coordination_backend`` directly — see
    ``tests/test_startup_guards.py``.
    """
    from app.coordination.factory import resolve_backend_name

    return resolve_backend_name()


def _analytics_backend() -> str:
    """Effective analytics backend — thin wrapper around
    :func:`src.analytics_backend.analytics_backend` (env overrides
    instance.yaml), kept as its own module-level function (same reasoning
    as :func:`_coordination_backend`) so tests can monkeypatch
    ``app.startup_guards._analytics_backend`` directly.
    """
    from src.analytics_backend import analytics_backend

    return analytics_backend()


def _ducklake_catalog_dsn() -> str:
    """Effective DuckLake catalog DSN/path — thin wrapper around
    :func:`src.analytics_backend.ducklake_catalog_dsn`, mirroring
    :func:`_analytics_backend`."""
    from src.analytics_backend import ducklake_catalog_dsn

    return ducklake_catalog_dsn()


def _workers() -> int:
    try:
        return int(os.environ.get("UVICORN_WORKERS", "1"))
    except ValueError:
        return 1


def is_multi_process() -> bool:
    return (not is_all_in_one()) or _workers() > 1 or _coordination_backend() == "redis"


def _multi_process_trigger_reasons() -> list[str]:
    """Which of ``is_multi_process()``'s conditions actually fired, in
    concrete terms — so the refusal below can name the real cause (e.g. a
    compose service that set ``AGNES_ROLE=worker``) instead of leaving the
    reader to resolve a generic "AGNES_ROLE split or UVICORN_WORKERS>1"
    disjunction themselves. Order matches :func:`is_multi_process`.
    """
    reasons: list[str] = []
    if not is_all_in_one():
        roles = ",".join(sorted(r.value for r in active_roles()))
        reasons.append(f"AGNES_ROLE={roles!r} is a role split")
    workers = _workers()
    if workers > 1:
        reasons.append(f"UVICORN_WORKERS={workers} > 1")
    if _coordination_backend() == "redis":
        reasons.append("coordination.backend=redis")
    return reasons


def validate_deployment() -> None:
    if not is_multi_process():
        return
    problems: list[str] = []
    if not _use_pg():
        problems.append("app-state backend must be Postgres (set DATABASE_URL or instance.yaml::database.backend)")
    for var in ("JWT_SECRET_KEY", "SESSION_SECRET"):
        if not os.environ.get(var):
            problems.append(f"{var} must be set explicitly (no per-node autogeneration)")
    if _coordination_backend() != "redis":
        problems.append("coordination.backend must be 'redis' (instance.yaml::coordination.backend)")
    if _analytics_backend() == "ducklake":
        dsn = _ducklake_catalog_dsn()
        from src.analytics_backend import is_postgres_dsn

        if not is_postgres_dsn(dsn):
            problems.append(
                "ducklake.catalog_dsn (or AGNES_DUCKLAKE_CATALOG_DSN) must be an explicit Postgres DSN "
                "(postgresql://...) when analytics.backend=ducklake — a DuckDB-file catalog is "
                "single-process only"
            )
    if problems:
        reasons = "; ".join(_multi_process_trigger_reasons())
        raise DeploymentConfigError(
            f"Multi-process deployment ({reasons}) is not safely configured:\n  - "
            + "\n  - ".join(problems)
            + "\nSee docs/DEPLOYMENT.md#multi-process."
        )
