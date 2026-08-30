import pytest

from app.roles import reset_roles_cache
from app.startup_guards import DeploymentConfigError, validate_deployment
from src.analytics_backend import reset_analytics_backend_cache


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in (
        "AGNES_ROLE",
        "UVICORN_WORKERS",
        "JWT_SECRET_KEY",
        "SESSION_SECRET",
        "AGNES_COORDINATION_BACKEND",
        "AGNES_ANALYTICS_BACKEND",
        "AGNES_DUCKLAKE_CATALOG_DSN",
    ):
        monkeypatch.delenv(var, raising=False)
    reset_roles_cache()
    reset_analytics_backend_cache()
    yield
    reset_roles_cache()
    reset_analytics_backend_cache()


def test_all_in_one_passes_with_no_config():
    validate_deployment()  # must not raise — spec §5.4.1 default unchanged


def test_split_role_without_pg_refuses(monkeypatch):
    monkeypatch.setenv("AGNES_ROLE", "api")
    reset_roles_cache()
    monkeypatch.setattr("app.startup_guards._use_pg", lambda: False)
    with pytest.raises(DeploymentConfigError, match="Postgres"):
        validate_deployment()


def test_multi_worker_is_multi_process(monkeypatch):
    monkeypatch.setenv("UVICORN_WORKERS", "2")
    monkeypatch.setattr("app.startup_guards._use_pg", lambda: False)
    with pytest.raises(DeploymentConfigError):
        validate_deployment()


def test_split_role_names_missing_secrets(monkeypatch):
    monkeypatch.setenv("AGNES_ROLE", "api")
    reset_roles_cache()
    monkeypatch.setattr("app.startup_guards._use_pg", lambda: True)
    monkeypatch.setattr("app.startup_guards._coordination_backend", lambda: "redis")
    with pytest.raises(DeploymentConfigError) as exc:
        validate_deployment()
    assert "JWT_SECRET_KEY" in str(exc.value)
    assert "SESSION_SECRET" in str(exc.value)
    assert "docs/DEPLOYMENT.md#multi-process" in str(exc.value)


def test_split_role_requires_redis_coordination(monkeypatch):
    monkeypatch.setenv("AGNES_ROLE", "api")
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("SESSION_SECRET", "y" * 32)
    reset_roles_cache()
    monkeypatch.setattr("app.startup_guards._use_pg", lambda: True)
    monkeypatch.setattr("app.startup_guards._coordination_backend", lambda: "memory")
    with pytest.raises(DeploymentConfigError, match="coordination"):
        validate_deployment()


def test_split_role_fully_configured_passes(monkeypatch):
    monkeypatch.setenv("AGNES_ROLE", "api")
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("SESSION_SECRET", "y" * 32)
    reset_roles_cache()
    monkeypatch.setattr("app.startup_guards._use_pg", lambda: True)
    monkeypatch.setattr("app.startup_guards._coordination_backend", lambda: "redis")
    validate_deployment()


def test_error_names_the_role_split_trigger_not_a_bare_disjunction(monkeypatch):
    """A process whose own AGNES_ROLE is split (e.g. the extraction-worker
    compose service, AGNES_ROLE=worker) gets an error naming ITS actual
    trigger, not the generic "AGNES_ROLE split or UVICORN_WORKERS>1" the
    reader previously had to resolve themselves — this is the exact
    topology that motivated the more specific message."""
    monkeypatch.setenv("AGNES_ROLE", "worker")
    reset_roles_cache()
    monkeypatch.setattr("app.startup_guards._use_pg", lambda: True)
    monkeypatch.setattr("app.startup_guards._coordination_backend", lambda: "memory")
    with pytest.raises(DeploymentConfigError) as exc:
        validate_deployment()
    message = str(exc.value)
    prefix = message.split(")")[0]
    assert "AGNES_ROLE='worker'" in prefix
    assert "UVICORN_WORKERS" not in prefix  # not falsely blamed on a trigger that never fired


def test_error_does_not_blame_role_split_when_coordination_backend_alone_triggers(monkeypatch):
    """coordination.backend=redis alone (no role split, single worker) must
    not be blamed on a role split it didn't cause."""
    monkeypatch.setattr("app.startup_guards._use_pg", lambda: False)
    monkeypatch.setattr("app.startup_guards._coordination_backend", lambda: "redis")
    with pytest.raises(DeploymentConfigError) as exc:
        validate_deployment()
    message = str(exc.value)
    prefix = message.split(")")[0]
    assert "coordination.backend=redis" in prefix
    assert "AGNES_ROLE" not in prefix
    assert "UVICORN_WORKERS" not in prefix


def test_error_names_worker_count_trigger(monkeypatch):
    monkeypatch.setenv("UVICORN_WORKERS", "4")
    reset_roles_cache()
    monkeypatch.setattr("app.startup_guards._use_pg", lambda: False)
    with pytest.raises(DeploymentConfigError) as exc:
        validate_deployment()
    assert "UVICORN_WORKERS=4 > 1" in str(exc.value).split(")")[0]


def test_redis_coordination_alone_triggers_multi_process(monkeypatch):
    """coordination.backend=redis is itself multi-process intent, even in an
    otherwise all-in-one topology (no AGNES_ROLE split, single worker) —
    closes the wave-1 deferred finding."""
    monkeypatch.setattr("app.startup_guards._use_pg", lambda: False)
    monkeypatch.setattr("app.startup_guards._coordination_backend", lambda: "redis")
    with pytest.raises(DeploymentConfigError, match="Postgres"):
        validate_deployment()


def test_redis_coordination_alone_fully_configured_passes(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("SESSION_SECRET", "y" * 32)
    monkeypatch.setattr("app.startup_guards._use_pg", lambda: True)
    monkeypatch.setattr("app.startup_guards._coordination_backend", lambda: "redis")
    validate_deployment()  # all-in-one + redis coordination + PG + secrets — OK


def test_memory_coordination_all_in_one_stays_single_process(monkeypatch):
    monkeypatch.setattr("app.startup_guards._use_pg", lambda: False)
    monkeypatch.setattr("app.startup_guards._coordination_backend", lambda: "memory")
    validate_deployment()  # must not raise — unchanged default topology


def test_coordination_backend_env_override_wins_over_yaml(monkeypatch):
    """``_coordination_backend`` must honor ``AGNES_COORDINATION_BACKEND`` —
    the same env-overrides-yaml resolution used by
    ``app.coordination.factory``, so this guard reacts to the backend the
    process will actually use."""
    from app.startup_guards import _coordination_backend

    monkeypatch.setenv("AGNES_COORDINATION_BACKEND", "redis")
    monkeypatch.setattr(
        "app.instance_config.get_value",
        lambda *a, **k: "memory",
    )
    assert _coordination_backend() == "redis"


# --- DuckLake analytics backend ------------------------------------------


def test_legacy_analytics_backend_multiprocess_unaffected(monkeypatch):
    """``analytics.backend=legacy`` (the default) must not add any new
    requirement to the multi-process guard — the ducklake catalog check
    only engages when the backend is actually ``ducklake``."""
    monkeypatch.setenv("AGNES_ROLE", "api")
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("SESSION_SECRET", "y" * 32)
    reset_roles_cache()
    monkeypatch.setattr("app.startup_guards._use_pg", lambda: True)
    monkeypatch.setattr("app.startup_guards._coordination_backend", lambda: "redis")
    monkeypatch.setattr("app.startup_guards._analytics_backend", lambda: "legacy")
    monkeypatch.setattr("app.startup_guards._ducklake_catalog_dsn", lambda: "/data/analytics/catalog.ducklake")
    validate_deployment()  # must not raise — legacy backend doesn't care about the (unused) file-catalog dsn


def test_ducklake_multiprocess_without_pg_dsn_refuses(monkeypatch):
    monkeypatch.setenv("AGNES_ROLE", "api")
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("SESSION_SECRET", "y" * 32)
    reset_roles_cache()
    monkeypatch.setattr("app.startup_guards._use_pg", lambda: True)
    monkeypatch.setattr("app.startup_guards._coordination_backend", lambda: "redis")
    monkeypatch.setattr("app.startup_guards._analytics_backend", lambda: "ducklake")
    monkeypatch.setattr("app.startup_guards._ducklake_catalog_dsn", lambda: "/data/analytics/catalog.ducklake")
    with pytest.raises(DeploymentConfigError, match="ducklake.catalog_dsn"):
        validate_deployment()


def test_ducklake_multiprocess_with_pg_dsn_passes(monkeypatch):
    monkeypatch.setenv("AGNES_ROLE", "api")
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("SESSION_SECRET", "y" * 32)
    reset_roles_cache()
    monkeypatch.setattr("app.startup_guards._use_pg", lambda: True)
    monkeypatch.setattr("app.startup_guards._coordination_backend", lambda: "redis")
    monkeypatch.setattr("app.startup_guards._analytics_backend", lambda: "ducklake")
    monkeypatch.setattr(
        "app.startup_guards._ducklake_catalog_dsn",
        lambda: "postgresql://agnes:pw@pg-host:5432/agnes_ducklake",
    )
    validate_deployment()  # explicit PG catalog DSN satisfies the guard


def test_ducklake_multiprocess_accepts_postgres_scheme_alias(monkeypatch):
    """``postgres://`` (the libpq-recognized alias for ``postgresql://``)
    must satisfy the guard too, not just the canonical scheme."""
    monkeypatch.setenv("AGNES_ROLE", "api")
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("SESSION_SECRET", "y" * 32)
    reset_roles_cache()
    monkeypatch.setattr("app.startup_guards._use_pg", lambda: True)
    monkeypatch.setattr("app.startup_guards._coordination_backend", lambda: "redis")
    monkeypatch.setattr("app.startup_guards._analytics_backend", lambda: "ducklake")
    monkeypatch.setattr(
        "app.startup_guards._ducklake_catalog_dsn", lambda: "postgres://agnes:pw@pg-host/agnes_ducklake"
    )
    validate_deployment()


def test_ducklake_multiprocess_accepts_sqlalchemy_driver_suffix(monkeypatch):
    """``postgresql+psycopg://`` (the SQLAlchemy ``+driver`` form a copied
    ``DATABASE_URL`` uses) must satisfy the guard — it uses the same
    ``src.analytics_backend.is_postgres_dsn`` predicate as
    ``src.ducklake_session``'s attach path, which already accepted this
    form; a plain ``str.startswith(("postgresql://", "postgres://"))``
    would have rejected it and blocked an otherwise-valid deployment."""
    monkeypatch.setenv("AGNES_ROLE", "api")
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("SESSION_SECRET", "y" * 32)
    reset_roles_cache()
    monkeypatch.setattr("app.startup_guards._use_pg", lambda: True)
    monkeypatch.setattr("app.startup_guards._coordination_backend", lambda: "redis")
    monkeypatch.setattr("app.startup_guards._analytics_backend", lambda: "ducklake")
    monkeypatch.setattr(
        "app.startup_guards._ducklake_catalog_dsn",
        lambda: "postgresql+psycopg://agnes:pw@pg-host:5432/agnes_ducklake",
    )
    validate_deployment()


def test_single_process_ducklake_file_catalog_passes_with_zero_config(monkeypatch):
    """All-in-one + ``analytics.backend=ducklake`` + the default file
    catalog is a fully supported topology — no PG, no secrets, no redis
    coordination required, because ``is_multi_process()`` is False and the
    ducklake check never engages."""
    monkeypatch.setattr("app.startup_guards._use_pg", lambda: False)
    monkeypatch.setattr("app.startup_guards._coordination_backend", lambda: "memory")
    monkeypatch.setattr("app.startup_guards._analytics_backend", lambda: "ducklake")
    monkeypatch.setattr("app.startup_guards._ducklake_catalog_dsn", lambda: "/data/analytics/catalog.ducklake")
    validate_deployment()  # must not raise


def test_analytics_backend_env_override_wins_over_yaml(monkeypatch):
    """``_analytics_backend`` must honor ``AGNES_ANALYTICS_BACKEND`` — the
    same env-overrides-yaml resolution used by
    ``src.analytics_backend``, so this guard reacts to the backend the
    process will actually use."""
    from app.startup_guards import _analytics_backend

    monkeypatch.setenv("AGNES_ANALYTICS_BACKEND", "ducklake")
    monkeypatch.setattr(
        "app.instance_config.get_value",
        lambda *a, **k: "legacy",
    )
    assert _analytics_backend() == "ducklake"
