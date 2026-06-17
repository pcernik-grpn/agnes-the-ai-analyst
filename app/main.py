"""FastAPI main application — unified server for web UI + API."""

# Silence authlib's internal forward-compat note. Authlib emits an
# AuthlibDeprecationWarning from its own _joserfc_helpers when our
# `from authlib.integrations.starlette_client import OAuth` import
# touches `authlib.jose` paths. The warning is upstream-internal — it's
# telling authlib to migrate to joserfc before its 2.0; it's not
# actionable on our side until either authlib ships the fix or we
# rewrite OAuth handling on top of joserfc directly. Filtering here
# (before authlib gets imported transitively) keeps `make local-dev`
# stdout clean without hiding warnings from any other package.
import warnings as _warnings
from src.repositories import (
    user_group_members_repo,
    user_groups_repo,
    users_repo,
)

try:
    from authlib.deprecate import AuthlibDeprecationWarning as _AuthlibDepr

    _warnings.filterwarnings("ignore", category=_AuthlibDepr)
except ImportError:
    # authlib too old / class moved — fall back to message-based match
    # so the filter still keeps startup clean.
    _warnings.filterwarnings(
        "ignore",
        message=r"authlib\.jose module is deprecated.*",
    )

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import os

# Initialise structured logging BEFORE any module that emits logs at import
# time. setup_logging is idempotent and safe to call once at process start.
from app.logging_config import setup_logging

setup_logging("app")

from app.version import APP_VERSION, MIN_COMPAT_CLI_VERSION

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from app.middleware.request_id import RequestIdMiddleware


def _chat_jwt_secret_ok(chat_config) -> bool:
    """Refuse ``chat.enabled=true`` deployments that lack a real
    ``JWT_SECRET_KEY`` (unset or shorter than 32 bytes).

    The chat path mints session JWTs that authenticate the sandboxed
    runner back to the Agnes server.  If ``JWT_SECRET_KEY`` is unset, the
    auth layer falls back to the public test constant
    (``test-jwt-secret-key-minimum-32-chars!!`` — committed in jwt.py for
    local-dev convenience).  A production deployment that flips
    ``chat.enabled: true`` without setting a real secret would mint and
    verify tokens against that constant — anyone who reads the source
    could mint runner JWTs.  Refuse to enable chat in that state and
    surface a fatal log so the operator knows why.

    Returns True when chat is disabled (irrelevant) or when the secret is
    set and >= 32 bytes; False otherwise.
    """
    if not chat_config.enabled:
        return True
    # Bypass when TESTING=1 — pytest-driven sessions deliberately use the
    # short fallback constant and we don't want every chat-touching test
    # to need a manually-set 32+-byte env var.
    if os.environ.get("TESTING", "").lower() in ("1", "true"):
        return True
    secret = os.environ.get("JWT_SECRET_KEY", "")
    if not secret:
        logger = logging.getLogger("app.main")
        logger.error(
            "chat.enabled=true but JWT_SECRET_KEY is unset — "
            "refusing to enable chat. Set a 32+ byte JWT_SECRET_KEY in "
            "the server env before flipping chat.enabled.",
        )
        return False
    if len(secret) < 32:
        logger = logging.getLogger("app.main")
        logger.error(
            "chat.enabled=true but JWT_SECRET_KEY is only %d bytes — refusing to enable chat (minimum 32 bytes).",
            len(secret),
        )
        return False
    return True


def _chat_anthropic_key_ok(chat_config) -> bool:
    """Refuse ``chat.enabled=true`` deployments that lack ``ANTHROPIC_API_KEY``.

    The chat runner inside the E2B sandbox calls the Anthropic API on
    behalf of each user.  If the key is absent the runner silently fails
    on its first API call.  Refuse to enable chat and surface a fatal
    log so the operator finds the cause immediately rather than after
    users start reporting mysterious errors.

    Returns True when chat is disabled (irrelevant) or when
    ``ANTHROPIC_API_KEY`` is set to a non-empty value; False otherwise.
    """
    if not chat_config.enabled:
        return True
    # Bypass for TESTING=1 — pytest-driven sessions don't need a real key.
    if os.environ.get("TESTING", "").lower() in ("1", "true"):
        return True
    if os.environ.get("ANTHROPIC_API_KEY", ""):
        return True
    logging.getLogger("app.main").error(
        "chat.enabled=true requires ANTHROPIC_API_KEY env to be set; refusing to spawn ChatManager",
    )
    return False


def _chat_e2b_api_key_ok(chat_config) -> bool:
    """Refuse ``chat.enabled=true`` deployments that lack ``E2B_API_KEY``.

    Mirrors ``_chat_anthropic_key_ok``: the E2B SDK requires an API key
    to spawn sandboxes; without it ``AsyncSandbox.create`` would 401 on
    every session start. Refuse the manager rather than letting users
    hit the failure.

    Returns True when chat is disabled or provider is not ``e2b``, or
    when the key is present; False otherwise.
    """
    if not chat_config.enabled:
        return True
    if chat_config.provider != "e2b":
        return True
    if os.environ.get("TESTING", "").lower() in ("1", "true"):
        return True
    if os.environ.get("E2B_API_KEY", ""):
        return True
    logging.getLogger("app.main").error(
        "chat.enabled=true with provider=e2b requires E2B_API_KEY env; refusing to spawn ChatManager",
    )
    return False


def _chat_e2b_template_id_ok(chat_config) -> bool:
    """Refuse ``chat.enabled=true`` without a ``chat.e2b_template_id``.

    The provider can't pick a default template — every operator builds
    their own ``agnes-chat`` template against their E2B account. Without
    the id, the provider would 404 at spawn time. Refuse at boot.
    """
    if not chat_config.enabled:
        return True
    if chat_config.provider != "e2b":
        return True
    if os.environ.get("TESTING", "").lower() in ("1", "true"):
        return True
    if getattr(chat_config, "e2b_template_id", None):
        return True
    logging.getLogger("app.main").error(
        "chat.enabled=true with provider=e2b requires chat.e2b_template_id "
        "to be set in instance.yaml; refusing to spawn ChatManager. "
        "Run `e2b template build` against app/initial_workspace_default/e2b-template "
        "and copy the returned id into instance.yaml.",
    )
    return False


class _SelectiveGZipMiddleware:
    """GZipMiddleware wrapper that skips a set of path prefixes.

    Parquet-serving endpoints send responses that are already columnar-
    compressed (parquet's internal codec) and — for /api/data — can reach
    hundreds of MB. Gzipping them on the way out costs CPU and latency with
    no meaningful size reduction. Skip those paths; every other endpoint
    (JSON manifests, HTML previews, install.sh) still gets compressed.
    """

    def __init__(self, app: ASGIApp, minimum_size: int = 1024, skip_prefixes: tuple[str, ...] = ()) -> None:
        # `self.app` is the Starlette middleware convention — outer middleware
        # (e.g. fastapi-debug-toolbar's APIRouter walker) traverses the chain
        # via `.app` to find the inner FastAPI app. Keep `_raw` as the public
        # alias used by our own __call__ for the skip-path branch.
        self.app = app
        self._raw = app
        self._gzip = GZipMiddleware(app, minimum_size=minimum_size)
        self._skip_prefixes = skip_prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http":
            path = scope.get("path", "")
            if any(path.startswith(p) for p in self._skip_prefixes):
                await self._raw(scope, receive, send)
                return
        await self._gzip(scope, receive, send)


from app.auth.rate_limit import (
    SlowAPIMiddleware as _AuthRateLimitMiddleware,
    RateLimitExceeded as _AuthRateLimitExceeded,
    _rate_limit_exceeded_handler as _auth_rate_limit_handler,
    limiter as _auth_rate_limiter,
)
from app.auth.router import router as auth_router
from app.api.health import router as health_router
from app.api.sync import router as sync_router
from app.api.data import router as data_router
from app.api.query import router as query_router
from app.api.users import router as users_router
from app.api.memory import router as memory_router
from app.api.upload import router as upload_router
from app.api.scripts import router as scripts_router
from app.api.settings import router as settings_router
from app.api.catalog import router as catalog_router
from app.api.telegram import router as telegram_router
from app.api.access import router as access_router, me_router as me_access_router
from app.api.me import router as me_router
from app.api.me_stats import router as me_stats_router
from app.api.admin import router as admin_router
from app.api.admin_bigquery_test import router as admin_bigquery_test_router
from app.api.admin_keboola_test import router as admin_keboola_test_router
from app.api.jira_webhooks import router as jira_webhooks_router
from app.api.metrics import router as metrics_router
from app.api.metadata import router as metadata_router
from app.api.query_hybrid import router as query_hybrid_router
from app.api.cli_artifacts import router as cli_artifacts_router
from app.api.cli_auth import router as cli_auth_router
from app.api.tokens import router as tokens_router, admin_router as tokens_admin_router
from app.api.v2_catalog import router as v2_catalog_router
from app.api.v2_schema import router as v2_schema_router
from app.api.v2_sample import router as v2_sample_router
from app.api.v2_scan import router as v2_scan_router
from app.api.v2_marketplace import router as v2_marketplace_router
from app.api.marketplaces import router as marketplaces_router
from app.api.data_packages import router as data_packages_router
from app.api.admin_mcp import router as admin_mcp_router
from app.api.admin_slack_secrets import router as admin_slack_secrets_router
from app.api.mcp_passthrough import router as mcp_passthrough_router
from app.api.mcp_per_table import router as mcp_per_table_router
from app.api.mcp_user_secrets import router as mcp_user_secrets_router
from app.api.memory_domains import router as memory_domains_router
from app.api.recipes import (
    public_router as recipes_public_router,
    admin_router as recipes_admin_router,
)
from app.api.memory_domain_suggestions import (
    public_router as memory_domain_suggestions_public_router,
    admin_router as memory_domain_suggestions_admin_router,
)
from app.api.authoring_suggestions import (
    public_router as authoring_suggestions_public_router,
    admin_router as authoring_suggestions_admin_router,
)
from app.api.memory_mining import (
    public_router as memory_mining_public_router,
    admin_router as memory_mining_admin_router,
)
from app.api.uploads import router as admin_uploads_router
from app.api.collections import router as collections_router  # Slice 2: file corpus upload
from app.api.stack import router as stack_router
from app.api.stack_views import router as stack_views_router
from app.api.initial_workspace import router as initial_workspace_router
from app.api.config_surface import router as config_surface_router
from app.api.store import router as store_router
from app.api.my_stack import router as my_stack_router
from app.api.marketplace import router as marketplace_router
from app.api.welcome import router as welcome_router
from app.api.connectors import router as connectors_router
from app.api.claude_md import router as claude_md_router
from app.api.prompts import router as prompts_router
from app.api.news import router as news_router
from app.api.cowork_bundle import (
    user_router as cowork_user_router,
    auth_router as cowork_auth_router,
)
from app.api.mcp_http import make_sse_app as _make_mcp_sse_app
from app.api.cache_warmup import router as cache_warmup_router
from app.api.bq_metadata_refresh import router as bq_metadata_refresh_router
from app.api.activity import router as activity_router
from app.api.observability import router as observability_router
from app.api.admin_user_sessions import router as admin_user_sessions_router
from app.api.admin_sessions import router as admin_sessions_router
from app.api.admin_usage import router as admin_usage_router
from app.api.admin_usage_summary import router as admin_usage_summary_router
from app.api.admin_adoption import router as admin_adoption_router
from app.api.db_state import router as db_state_router
from app.marketplace_server.router import router as marketplace_server_router
from app.marketplace_server.git_router import make_git_wsgi_app
from app.web.router import router as web_router
from app.api.chat import router as chat_router
from app.api.chat_copresence import router as chat_copresence_router
from app.api.slack import router as slack_router
from app.api.admin_chat import router as admin_chat_router
from app.instance_config import get_slack_transport
from services.slack_bot.socket_mode_client import (
    SocketModeDispatcher,
    socket_mode_preflight,
)

logger = logging.getLogger(__name__)


def _maybe_rebuild_on_boot() -> bool:
    """When AGNES_REBUILD_ON_BOOT=1, ATTACH all baked extracts and build
    master views before serving. For images that ship baked data and have
    no scheduler (ephemeral/demo). Returns True if a rebuild ran.

    Blocking by design: the dataset is small and baked, and views must
    exist before the first request. Soft-fails (logs) so a corrupt extract
    never wedges boot.
    """
    if os.environ.get("AGNES_REBUILD_ON_BOOT", "").lower() not in ("1", "true"):
        return False
    try:
        from src.orchestrator import SyncOrchestrator

        SyncOrchestrator().rebuild()
        logger.info("AGNES_REBUILD_ON_BOOT: master views rebuilt from baked extracts")
        return True
    except Exception:
        logger.exception("AGNES_REBUILD_ON_BOOT rebuild failed (non-fatal)")
        return False


async def _start_slack_socket_transport(app) -> None:
    """If chat.slack.transport=socket, start one Socket Mode WS behind
    fail-closed gates. On any miss -> log + leave Slack HTTP-only; never
    crash and never start a dead WS. Stashed on app.state for shutdown."""
    app.state.slack_socket_dispatcher = None
    if get_slack_transport() != "socket":
        return
    from services.slack_bot.secrets import slack_secret

    app_token = slack_secret("SLACK_APP_TOKEN") or ""
    bot_token = slack_secret("SLACK_BOT_TOKEN") or ""
    try:
        workers = int(os.environ.get("UVICORN_WORKERS", "1"))
    except ValueError:
        workers = 1
    ok, reason = socket_mode_preflight(
        workers=workers,
        app_token=app_token,
        bot_token=bot_token,
    )
    if not ok:
        logger.error("Slack Socket Mode disabled: %s", reason)
        return
    try:
        dispatcher = SocketModeDispatcher(
            app=app,
            app_token=app_token,
            bot_token=bot_token,
        )
        await dispatcher.start()
        app.state.slack_socket_dispatcher = dispatcher
    except Exception:
        logger.exception("Slack Socket Mode disabled: start() failed")
        app.state.slack_socket_dispatcher = None


@asynccontextmanager
async def lifespan(app):
    # Fail-closed: refuse to serve with a weak/absent JWT signing key in
    # production. Cheap, runs before any request is accepted.
    from app.auth.jwt import validate_jwt_secret_or_raise

    validate_jwt_secret_or_raise()

    # Resolve the instance's absolute base URL once and stash it on app.state
    # so request-less surfaces can build absolute links. The Slack bot (Socket
    # Mode) has no inbound request to derive the host from, so without this its
    # /slack/bind magic links and /chat deep links come out root-relative and
    # are not clickable from Slack. Set before _start_slack_socket_transport so
    # the dispatcher's handlers see it. Empty when PUBLIC_URL / server.public_url
    # is unset — callers degrade to a relative path.
    from app.instance_config import get_public_url

    app.state.public_url = get_public_url()

    # Install operator-provided stdio-MCP wheels from the persistent data
    # volume (${DATA_DIR}/mcp/wheels) and put ~/.local/bin on PATH so their
    # console scripts resolve when the stdio client spawns them. Without
    # this, a wheel installed by hand into the container is wiped on every
    # recreate and the source's scheduled materialize silently breaks with
    # command-not-found. Fail-soft: a bad wheel logs and is retried next
    # boot; never blocks startup.
    try:
        from connectors.mcp.wheel_bootstrap import (
            ensure_user_bin_on_path,
            install_operator_wheels,
        )

        ensure_user_bin_on_path()
        install_operator_wheels()
    except Exception:
        logger.exception("mcp wheel bootstrap failed (non-fatal)")

    # Issue #81 Group A — log the effective remote_attach allowlist at
    # startup so an operator's typo in AGNES_REMOTE_ATTACH_EXTENSIONS
    # (which REPLACES, not extends, the default) is visible.
    try:
        from src.orchestrator_security import log_effective_policy

        log_effective_policy()
    except Exception:
        pass  # never block startup on a logging convenience

    # Bump anyio's default thread pool size from 40 → AGNES_THREADPOOL_SIZE
    # (default 200). FastAPI auto-runs every plain `def` route handler in
    # this pool — the Tier 1 endpoints converted in PR #188 (`/api/query`,
    # `/api/v2/scan`, `/api/v2/sample`, `/api/v2/schema`) all block on
    # synchronous DuckDB / BQ-extension calls inside the handler body and
    # would otherwise serialise once 40 are in flight. 200 keeps the per-
    # process working set well under the BQ extension's connection cap
    # while leaving headroom for concurrent UI / health probes.
    try:
        import anyio.to_thread

        size = int(os.environ.get("AGNES_THREADPOOL_SIZE", "200"))
        anyio.to_thread.current_default_thread_limiter().total_tokens = size
        logger.info("anyio thread pool capacity set to %d", size)
    except Exception as e:
        logger.warning("failed to bump anyio thread pool capacity: %s", e)

    from app.api.cache_warmup import maybe_schedule_startup_warmup

    maybe_schedule_startup_warmup()

    # Sweep stale materialize parquet locks left behind by previous runs
    # that were SIGKILL'd mid-materialize. Lazy reclaim at next acquire
    # already handles correctness, but an active sweep at startup keeps
    # the data directory tidy and gives operators a clear "swept N" log
    # line instead of zombie 0-byte files lingering for days (issue #260).
    try:
        from connectors.bigquery.extractor import sweep_stale_parquet_locks
        from src.db import _get_data_dir as _ddir

        sweep_stale_parquet_locks(_ddir() / "extracts")
    except Exception:
        logger.exception("startup parquet-lock sweep failed (non-fatal)")

    # Seed the internal data-source registry rows so `agnes_sessions /
    # agnes_telemetry / agnes_audit` show up in /admin/tables + `agnes
    # catalog` on every fresh install. Idempotent — re-applies canonical
    # name + description on every boot so operators can't drift them
    # away from the seed.
    try:
        from src.db import get_system_db
        from connectors.internal.registry import ensure_internal_tables_registered

        ensure_internal_tables_registered(get_system_db())
    except Exception:
        logger.exception("internal data-source seed failed; continuing")

    # Baked-data images (no scheduler) need master views built at boot.
    _maybe_rebuild_on_boot()

    # Rebuild the FTS BM25 index over knowledge_items at boot (issue #121).
    # The migration to schema v47 already does this on first upgrade, but
    # for instances that have been on v47 across restarts the boot-time
    # rebuild guarantees the index reflects whatever mutations landed via
    # the BG-task / scheduler paths that bypass the per-mutation hook.
    # Soft-failure — logs WARNING and the repo falls back to ILIKE.
    try:
        from src.db import get_system_db
        from src.fts import ensure_knowledge_fts_index

        ensure_knowledge_fts_index(get_system_db())
    except Exception:
        logger.exception("startup FTS index rebuild failed; falling back to ILIKE on /api/memory?search=")

    # Surface BQ config gaps at startup so the operator sees them in
    # the boot log instead of as cryptic "provider returned no data" /
    # "403 serviceusage" later. Issue #343 — these are the same gaps
    # that silently failed every remote BQ query on a customer prod
    # instance for several days in mid-May 2026 before the cause was
    # traced. Non-fatal: warnings only, no startup abort.
    try:
        from connectors.bigquery.access import validate_bigquery_startup_config

        for warning in validate_bigquery_startup_config():
            logger.warning("BQ config check: %s", warning)
    except Exception:
        logger.exception("BQ startup config validation crashed (non-fatal)")

    # Bring the Postgres schema to the app's expected Alembic head. The
    # DuckDB ladder self-migrates on every connect (src/db.py); Postgres
    # now mirrors that at startup — when the DB is behind, the pending
    # migrations are applied in-process under a Postgres advisory lock
    # (replica-safe). A DB AHEAD of the image (app rollback) still refuses
    # to boot, as does a failed upgrade — never serve on a half-migrated
    # schema (issue #636). AGNES_PG_AUTO_MIGRATE=0 restores the
    # fail-closed check for pipeline-controlled deployments;
    # AGNES_SKIP_PG_REVISION_CHECK=1 skips everything (emergency boots).
    from src.repositories import use_pg

    if use_pg():
        from src.db_pg import ensure_pg_at_head

        ensure_pg_at_head()

    # Seed default source connections from env/yaml on first boot
    # (spec 2026-06-12 §3.4). MUST run after ensure_pg_at_head(): on a
    # Postgres backend the source_connections table is created by Alembic
    # 0026, which ensure_pg_at_head() applies — seeding earlier hits a
    # missing table, gets swallowed by the try/except, and silently no-ops
    # until the next restart (Devin Review on #671). DuckDB is unaffected
    # (get_system_db lazily runs _ensure_schema). One-time; registry rules after.
    try:
        from app.connections_seed import seed_default_connections

        seed_default_connections()
    except Exception:
        logger.exception("source-connection seed failed; continuing")

    # Seed the Admin/Everyone system groups into the ACTIVE state backend.
    # On DuckDB this duplicates src.db._seed_system_groups (idempotent), but
    # that runs ONLY on a DuckDB connect — nothing seeds these groups on a
    # Postgres instance, so without this a fresh PG deploy has no Admin group
    # (require_admin can never pass) and no Everyone group (Everyone-scoped
    # grants like Required onboarding never surface). ensure_system is
    # idempotent and routes through the factory, so it is correct on either
    # backend.
    try:
        from src.db import _SYSTEM_GROUPS_SEED

        _ug_repo = user_groups_repo()
        for _grp_name, _grp_desc in _SYSTEM_GROUPS_SEED:
            _ug_repo.ensure_system(_grp_name, _grp_desc)
    except Exception as e:
        logger.warning("Could not seed system groups: %s", e)

    # Seed (or re-bake) the built-in marketplace from the wheel bundle. Runs
    # after system-groups are ensured so the RBAC seed can look up Admin/Everyone.
    # Non-fatal: a missing bundle dir only means the plugin cache is empty.
    try:
        from src.marketplace import seed_builtin_marketplace

        seed_builtin_marketplace()
    except Exception as e:
        logger.warning("Could not seed built-in marketplace: %s", e)

    # Seed admin user (SEED_ADMIN_EMAIL) and add them to the Admin user_group.
    # Optional SEED_ADMIN_PASSWORD lets the seeded user sign in immediately
    # without going through bootstrap; never overwritten if already set.
    # The Admin/Everyone user_groups were ensured just above (factory →
    # active backend), so this hook only has to handle membership for the
    # seed admin — looking the groups up through the factory too, so it gets
    # the active backend's group ids (a raw DuckDB read returned a DuckDB-only
    # group id that does not exist on a Postgres instance).
    # Lives in lifespan (worker-only), NOT create_app(): the latter runs
    # in the uvicorn --reload master too, and duckdb >=1.5 holds an
    # exclusive per-process file lock on system.duckdb that would then
    # block the worker.
    from app.auth.dependencies import is_local_dev_mode, get_local_dev_email

    seed_email = os.environ.get("SEED_ADMIN_EMAIL") or (get_local_dev_email() if is_local_dev_mode() else None)
    if seed_email:
        try:
            from src.db import SYSTEM_ADMIN_GROUP, SYSTEM_EVERYONE_GROUP

            repo = users_repo()
            groups_repo = user_groups_repo()
            members_repo = user_group_members_repo()
            seed_password = os.environ.get("SEED_ADMIN_PASSWORD") or None
            password_hash = None
            if seed_password:
                from argon2 import PasswordHasher

                password_hash = PasswordHasher().hash(seed_password)
            existing = repo.get_by_email(seed_email)
            if not existing:
                import uuid

                user_id = str(uuid.uuid4())
                repo.create(
                    id=user_id,
                    email=seed_email,
                    name="Admin",
                    password_hash=password_hash,
                    # A seeded password is communicated in plaintext (emailed by
                    # the cloud control-plane, or shared by an operator), so force
                    # a change on first sign-in. SSO-only seed admins (no
                    # password) have nothing to rotate and stay unflagged.
                    must_change_password=bool(password_hash),
                )
                logger.info("Seeded admin user: %s (password=%s)", seed_email, "yes" if password_hash else "no")
            else:
                user_id = existing["id"]
                if password_hash and not existing.get("password_hash"):
                    # Only fires for a still-password-less seed admin, so a user
                    # who already rotated (has a hash) is never re-flagged on a
                    # restart. The seeded password must still be changed.
                    repo.update(id=user_id, password_hash=password_hash, must_change_password=True)
                    logger.info("Set password on existing seed admin: %s", seed_email)
            # Make sure the seed admin is actually in the Admin group — this
            # is what gives them admin access in v12. Idempotent. Look the
            # group up through the factory so we get the ACTIVE backend's id
            # (raw DuckDB read returned a DuckDB group id absent from Postgres).
            admin_group = groups_repo.get_by_name(SYSTEM_ADMIN_GROUP)
            if admin_group:
                members_repo.add_member(
                    user_id=user_id,
                    group_id=admin_group["id"],
                    source="system_seed",
                    added_by="app.main:seed_admin",
                )
            # Also seed Everyone membership — Everyone-scoped grants are the
            # canonical "every-user-sees-this" pattern (Required onboarding,
            # default reference packages). The seed admin not being in
            # Everyone meant their own Required grants didn't surface on
            # /catalog as Required for them, which read as a bug.
            everyone_group = groups_repo.get_by_name(SYSTEM_EVERYONE_GROUP)
            if everyone_group:
                members_repo.add_member(
                    user_id=user_id,
                    group_id=everyone_group["id"],
                    source="system_seed",
                    added_by="app.main:seed_admin",
                )
        except Exception as e:
            logger.warning(f"Could not seed admin: {e}")

    # Seed the synthetic scheduler user when SCHEDULER_API_TOKEN is configured,
    # so the very first cron tick after a fresh deploy already has a valid
    # actor to attribute audit-log entries to. The lazy seed in
    # `app.auth.scheduler_token.get_scheduler_user` covers the case where the
    # secret is rotated mid-life, but doing it here keeps startup observable.
    from app.auth.scheduler_token import get_scheduler_secret

    if get_scheduler_secret():
        try:
            from app.auth.scheduler_token import (
                SCHEDULER_TOKEN_MIN_LENGTH,
                ensure_scheduler_user,
            )
            from src.db import get_system_db

            secret = get_scheduler_secret()
            if len(secret) < SCHEDULER_TOKEN_MIN_LENGTH:
                logger.warning(
                    "SCHEDULER_API_TOKEN is set but only %d chars — auth path"
                    " disabled (minimum %d). Generate a longer secret in .env.",
                    len(secret),
                    SCHEDULER_TOKEN_MIN_LENGTH,
                )
            else:
                conn = get_system_db()
                try:
                    ensure_scheduler_user(conn)
                finally:
                    conn.close()
        except Exception as e:
            logger.warning(f"Could not seed scheduler user: {e}")

    # C8: Warn when no user has a password_hash — bootstrap endpoint is open.
    # This is intentional UX (operator can claim seed admin), but the open
    # window should be visible in startup logs so it's not forgotten.
    if not is_local_dev_mode():
        try:
            from src.db import get_system_db

            conn = get_system_db()
            repo = users_repo()
            all_users = repo.list_all()
            has_password = any(u.get("password_hash") for u in all_users)
            if not has_password:
                logger.warning(
                    "No user has a password set — /auth/bootstrap is reachable. "
                    "Claim the seed admin (or set SEED_ADMIN_PASSWORD) to close this window."
                )
            conn.close()
        except Exception:
            pass  # never block startup on a logging convenience

    # Construct the PostHog client up front so its background flush thread
    # starts before the first request — and so a missing/invalid key fails
    # loud at boot rather than on first capture. No-op when disabled.
    try:
        from src.observability import get_posthog

        pc = get_posthog()
        if pc.enabled:
            logger.info(
                "PostHog observability enabled (host=%s, identify=%s, replay=%s)",
                pc.host,
                pc.identify_mode,
                pc.replay_enabled,
            )
    except Exception:
        logger.exception("PostHog init at startup failed")

    # --- CHAT-INIT -----------------------------------------------------------
    # Always create chat_repo + chat_config regardless of chat.enabled so that
    # the admin_chat and chat API routers (which use app.state.chat_repo) work
    # even when chat is disabled — they degrade gracefully via _get_manager().
    try:
        from src.db import get_system_db as _get_system_db_chat, _get_data_dir as _get_data_dir_chat
        from app.chat.config import load_chat_config
        from app.chat.persistence import ChatRepository

        _chat_data_dir = _get_data_dir_chat()
        _chat_conn = _get_system_db_chat()
        app.state.chat_repo = ChatRepository(_chat_conn)
        app.state.chat_data_dir = _chat_data_dir

        _chat_instance_yaml = _chat_data_dir / "state" / "instance.yaml"
        app.state.chat_config = load_chat_config(_chat_instance_yaml)

        def _get_marketplace_sha() -> str:
            """Return combined SHA over all synced marketplace repos.

            The marketplace ingest pipeline writes
            ``${DATA_DIR}/marketplaces/.combined-sha`` after each nightly
            sync. Read it when it exists; otherwise return empty string so
            WorkdirManager.needs_reinit() falls through to the version check.
            """
            p = _chat_data_dir / "marketplaces" / ".combined-sha"
            try:
                return p.read_text().strip() if p.exists() else ""
            except Exception:
                return ""

        def _server_template_status():
            """Return TemplateStatus if an initial-workspace template is configured."""
            try:
                from src.initial_workspace import TemplateStatus
                from app.api.initial_workspace import _read_section

                section = _read_section()
                if not section.get("url"):
                    return None
                synced = bool(section.get("last_commit_sha"))
                return TemplateStatus(
                    configured=True,
                    synced=synced,
                    template_source=section.get("url"),
                    template_sha=section.get("last_commit_sha"),
                    synced_at=section.get("last_synced_at"),
                )
            except Exception:
                logger.exception("_server_template_status failed (non-fatal)")
                return None

        def _fetch_local_template_zip() -> bytes:
            """Read the cached template zip from disk.

            Passes a system-DB conn so the workspace-prompt admin overlay
            (source_mode='editor') replaces the clone's CLAUDE.md, keeping
            cloud-chat workdirs byte-compatible with laptop override-mode
            `agnes init` (#622)."""
            try:
                from src.db import get_system_db
                from src.initial_workspace import build_zip

                conn = get_system_db()
                try:
                    return build_zip(conn)
                finally:
                    conn.close()
            except Exception:
                logger.exception("_fetch_local_template_zip failed (non-fatal)")
                return b""

        if app.state.chat_config.enabled:
            if app.state.chat_config.provider != "e2b":
                logger.error(
                    "chat.provider=%r is not supported — only 'e2b' is "
                    "accepted in production (per Q7 owner decision, "
                    "MockE2BProvider was dropped). Set chat.provider: e2b "
                    "in instance.yaml or flip chat.enabled: false.",
                    app.state.chat_config.provider,
                )
                app.state.chat_manager = None
            elif int(os.environ.get("UVICORN_WORKERS", "1")) > 1:
                logger.error(
                    "chat.enabled=true but UVICORN_WORKERS > 1 — "
                    "cloud chat requires a single-worker deployment; "
                    "chat_manager disabled"
                )
                app.state.chat_manager = None
            elif not _chat_jwt_secret_ok(app.state.chat_config):
                # Fatal already logged inside the helper.  Disable chat so the
                # runner never spawns with a public-constant secret.
                app.state.chat_manager = None
            elif not _chat_anthropic_key_ok(app.state.chat_config):
                # Fatal already logged inside the helper.  No key → no runner.
                logger.error(
                    "ANTHROPIC_API_KEY missing; disabling chat",
                )
                app.state.chat_manager = None
            elif not _chat_e2b_api_key_ok(app.state.chat_config):
                # Fatal already logged inside the helper.
                app.state.chat_manager = None
            elif not _chat_e2b_template_id_ok(app.state.chat_config):
                # Fatal already logged inside the helper.
                app.state.chat_manager = None
            else:
                from typing import Optional
                from app.chat.workdir import WorkdirManager
                from app.chat.e2b_provider import E2BProvider
                from app.chat.manager import ChatManager
                from app.version import APP_VERSION as _APP_VERSION_CHAT

                _server_url = os.environ.get("SERVER_URL", "http://localhost:8000")

                def _render_workspace_prompt(user_email: str) -> Optional[str]:
                    """Render the analyst CLAUDE.md (admin Workspace Prompt
                    override or shipped default), RBAC-filtered for this user —
                    the same content `agnes init` writes on a laptop via
                    GET /api/welcome. Returns None on any failure so workdir
                    init falls back to the bundled static CLAUDE.md."""
                    try:
                        from src.db import get_system_db
                        from src.claude_md import render_claude_md
                        from src.repositories.users import UserRepository

                        conn = get_system_db()
                        try:
                            u = UserRepository(conn).get_by_email(user_email)
                            if not u:
                                return None
                            return render_claude_md(conn, user=u, server_url=_server_url)
                        finally:
                            conn.close()
                    except Exception:
                        logger.exception("render workspace prompt failed for %s", user_email)
                        return None

                workdir_mgr = WorkdirManager(
                    data_dir=_chat_data_dir,
                    repo=app.state.chat_repo,
                    bundled_template_dir=Path("app/initial_workspace_default"),
                    server_url=_server_url,
                    agnes_version=_APP_VERSION_CHAT,
                    get_marketplace_sha=_get_marketplace_sha,
                    get_template_status=_server_template_status,
                    fetch_template_zip=_fetch_local_template_zip,
                    render_workspace_prompt=_render_workspace_prompt,
                    marketplace_sha_debounce_seconds=app.state.chat_config.marketplace_sha_debounce_seconds,
                )
                # E2B sandboxes are capped at 1 hour (3600 s) by the platform.
                # If chat.max_session_seconds is higher (default 4 h), clamp here
                # so AsyncSandbox.create() doesn't 400. The idle reaper / per-tool
                # caps still enforce shorter limits as configured; this just
                # prevents the spawn call from failing fast on the upper bound.
                E2B_SANDBOX_MAX_SECONDS = 3600
                provider = E2BProvider(
                    api_key=os.environ.get("E2B_API_KEY", ""),
                    template_id=app.state.chat_config.e2b_template_id or "",
                    sandbox_timeout_seconds=min(
                        app.state.chat_config.max_session_seconds,
                        E2B_SANDBOX_MAX_SECONDS,
                    ),
                )
                mgr = ChatManager(
                    provider=provider,
                    workdir_mgr=workdir_mgr,
                    repo=app.state.chat_repo,
                    config=app.state.chat_config,
                )
                mgr.start_idle_reaper()
                app.state.chat_manager = mgr
                logger.info(
                    "chat.enabled: ChatManager started (provider=e2b, "
                    "template=%s, idle_ttl=%ds, concurrency_per_user=%d)",
                    app.state.chat_config.e2b_template_id,
                    app.state.chat_config.idle_ttl_seconds,
                    app.state.chat_config.concurrency_per_user,
                )
        else:
            app.state.chat_manager = None
            logger.info("chat.enabled=false; ChatManager not started")
    except Exception:
        logger.exception("CHAT-INIT failed (non-fatal); chat features will be unavailable")
        app.state.chat_manager = None
    # --- end CHAT-INIT -------------------------------------------------------

    # --- SLACK-INIT: resolve bot user id once (mention loop-guard / strip) ---
    app.state.slack_bot_user_id = None
    try:
        from services.slack_bot.identity import resolve_bot_user_id

        app.state.slack_bot_user_id = await resolve_bot_user_id()
        if app.state.slack_bot_user_id:
            logger.info("slack bot user id resolved: %s", app.state.slack_bot_user_id)
    except Exception:
        logger.exception("SLACK-INIT failed (non-fatal); bot user id unresolved")
    # --- end SLACK-INIT ------------------------------------------------------

    # --- SLACK SOCKET MODE (optional inbound transport) ----------------------
    # Boot-safety boundary: a Slack misconfig (bad transport value, preflight
    # raising, etc.) must NEVER crash app startup. The helper self-guards
    # start(); this covers everything before it.
    try:
        await _start_slack_socket_transport(app)
    except Exception:
        logger.exception("Slack Socket Mode wiring failed (non-fatal)")
    # --- end SLACK SOCKET MODE -----------------------------------------------

    yield
    _socket_disp = getattr(app.state, "slack_socket_dispatcher", None)
    if _socket_disp is not None:
        try:
            await _socket_disp.stop()
        except Exception:
            logger.exception("Slack Socket Mode shutdown failed")
    try:
        from src.observability import get_posthog

        get_posthog().shutdown()
    except Exception:
        logger.exception("PostHog shutdown failed")
    from src.db import close_analytics_db, close_system_db

    close_system_db()
    close_analytics_db()


def _is_truthy_env(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes")


def _debug_enabled() -> bool:
    """Whether the FastAPI debug toolbar is mounted.

    LOCAL_DEV_MODE (auth-bypassed dev) implies DEBUG so operators needn't set
    both. But an *explicit* DEBUG env wins either way — set ``DEBUG=0`` to run
    local-dev WITHOUT the toolbar, whose per-request instrumentation
    (incl. the compose healthcheck) can peg CPU on heavy HTML pages.
    """
    raw = os.environ.get("DEBUG")
    if raw is not None and raw.strip() != "":
        return _is_truthy_env("DEBUG")
    return _is_truthy_env("LOCAL_DEV_MODE")


DEBUG = _debug_enabled()


# Background poll / low-signal endpoints the debug toolbar must NOT attach to.
# They run ~no application queries but fire repeatedly, and every instrumented
# response rewrites the `dtRefresh` cookie — `refresh.js` then repoints the
# toolbar to that request's (near-empty) store and wipes the panel content you
# were reading (the "flickers to 0 / stuck spinner" symptom). Skipping ONLY
# these keeps data XHRs (e.g. /api/marketplace/items, /api/store/entities, whose
# Postgres queries are exactly what you want to inspect) instrumented while the
# pollers stay out of the way. Tunable: add high-frequency, low-signal paths.
#
# Two sets: EXACT skips only the listed path verbatim (so e.g. `/api/health`
# does NOT inadvertently skip `/api/health/detailed`, which is a separate
# authenticated admin diagnostics endpoint — see app/api/health.py). PREFIXES
# skips the listed path AND any sub-path (whole subtree is a poll surface).
_TOOLBAR_SKIP_EXACT = (
    "/api/version",
    "/api/health",
    "/api/memory/stats",
)
_TOOLBAR_SKIP_PREFIXES = ("/api/notifications",)


def _toolbar_show_callback(request, settings) -> bool:
    """Decide whether the debug toolbar attaches to a request.

    Replaces the upstream default (which reads `request.app.debug`) — we keep
    `app.debug=False` so our @app.exception_handler(Exception) runs instead of
    Starlette's debug-only ServerErrorMiddleware, but we still want the
    toolbar mounted. Read DEBUG / LOCAL_DEV_MODE env directly so operators who
    flip the env at runtime (rare) see the change without re-import.

    Document navigations AND data XHRs are instrumented (so async/XHR-loaded
    listings show their queries); only the toolbar's own ``/_debug_toolbar``
    endpoints (always allowed) and the background pollers in
    ``_TOOLBAR_SKIP_PREFIXES`` are special-cased. See that constant for why the
    pollers must be excluded. For comprehensive, request-independent capture of
    EVERY query (incl. async/threadpool), see the DEBUG-gated logger in
    ``app/debug/postgres_panel.py`` (logger ``agnes.db.postgres``).
    """
    if not _debug_enabled():
        return False
    path = request.url.path
    if path.startswith("/_debug_toolbar"):
        return True  # toolbar's own render_panel + static — always, or panels can't load
    if path in _TOOLBAR_SKIP_EXACT:
        return False
    if any(path == p or path.startswith(p + "/") for p in _TOOLBAR_SKIP_PREFIXES):
        return False
    return True


def create_app() -> FastAPI:
    from app.serialization import AgnesJSONResponse

    app = FastAPI(
        title="AI Data Analyst",
        description="Data distribution platform for AI analytical systems",
        version=APP_VERSION,
        lifespan=lifespan,
        # Swagger UI / OpenAPI JSON gated behind authentication — custom
        # routes added below before the web_router catch-all. Setting these
        # to None disables FastAPI's default unauthenticated endpoints.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        # All JSON responses label datetime fields with an explicit UTC
        # offset — see app/serialization.py for the why.
        default_response_class=AgnesJSONResponse,
        # Intentionally NOT debug=DEBUG: FastAPI's debug=True installs
        # Starlette's ServerErrorMiddleware which intercepts unhandled
        # Exceptions and renders a plain-HTML traceback BEFORE our
        # @app.exception_handler(Exception) can run — robbing the 500 page
        # of its chrome and the debug toolbar. We get the toolbar back via
        # SHOW_TOOLBAR_CALLBACK below (reads DEBUG env directly instead of
        # request.app.debug).
        debug=False,
    )

    @app.middleware("http")
    async def _add_version_headers(request, call_next):
        response = await call_next(request)
        # /api/* only — headers are advisory to the agnes CLI; UI/docs/marketplace
        # traffic doesn't consume them.
        if request.url.path.startswith("/api/"):
            response.headers["X-Agnes-Latest-Version"] = APP_VERSION
            response.headers["X-Agnes-Min-Version"] = MIN_COMPAT_CLI_VERSION
        # Server-rendered HTML must not be heuristically cached by the browser.
        # The setup hero (/home, /setup, /install) bakes build-pinned values
        # into the markup at render time — most importantly the current wheel
        # filename, served from the version-pinned `/cli/wheel/{name}` endpoint
        # that 404s for any name but the wheel currently on disk. Without an
        # explicit directive a browser reuses the cached document, so after a
        # redeploy a user is handed a stale page whose baked wheel URL now 404s
        # (the new build replaced the wheel). `no-store` forces a fresh render
        # on every load. Scoped to text/html so JSON APIs and the
        # immutable-cached static / marketplace-image assets are untouched; an
        # explicit Cache-Control set by a route still wins.
        ctype = response.headers.get("content-type", "")
        if ctype.startswith("text/html") and "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        return response

    # FastAPI debug toolbar — only when DEBUG=1 in env. Injects per-request
    # HTML overlay (headers, routes, timer, profiling, logs) on any HTML
    # response; harmless on JSON. Inner try/except is for the import only:
    # if a developer sets DEBUG=1 without installing dev deps, log a warning
    # instead of crashing. The middleware mount itself fails loud if broken.
    #
    # Mounted FIRST (innermost on response) so it sees the raw HTML BEFORE
    # GZip compresses it — debug_toolbar.middleware decodes response bodies
    # as UTF-8 to inject markup, and a gzipped body fails that decode (the
    # toolbar's own `Accept-Encoding` skip-check reads response headers, not
    # request headers, so it never trips).
    if DEBUG:
        try:
            from debug_toolbar.middleware import DebugToolbarMiddleware
            from jinja2 import FileSystemLoader

            # debug_toolbar.middleware splats **kwargs into DebugToolbarSettings
            # (a pydantic-settings model with case-insensitive UPPERCASE fields).
            # Pass field names as kwargs to add_middleware — `panels` becomes
            # `PANELS`, etc. Do NOT wrap them in a `settings={...}` dict —
            # that hits the model's actual `SETTINGS` field (Sequence[BaseSettings])
            # and fails validation. Field reference:
            # https://github.com/mongkok/fastapi-debug-toolbar/blob/master/debug_toolbar/settings.py
            # ProfilingPanel (pyinstrument) is intentionally omitted: it
            # raises "There is already a profiler running" under uvicorn's
            # async context because pyinstrument's stack sampler can't be
            # nested per task. Re-enable per-developer if you really want it
            # via env override; the rest of the panels are async-safe.
            #
            # JINJA_LOADERS prepends our app/debug/templates so DuckDBPanel
            # can resolve `panels/duckdb.html`. The toolbar's built-in loader
            # (PackageLoader for debug_toolbar/templates) stays appended via
            # ChoiceLoader, so first-party panels still render.
            _debug_templates_dir = Path(__file__).parent / "debug" / "templates"
            _toolbar_settings = dict(
                panels=[
                    "debug_toolbar.panels.headers.HeadersPanel",
                    "debug_toolbar.panels.routes.RoutesPanel",
                    "debug_toolbar.panels.settings.SettingsPanel",
                    "debug_toolbar.panels.versions.VersionsPanel",
                    "debug_toolbar.panels.timer.TimerPanel",
                    "debug_toolbar.panels.logging.LoggingPanel",
                    "app.debug.duckdb_panel.DuckDBPanel",
                    "app.debug.postgres_panel.PostgresPanel",
                ],
                jinja_loaders=[FileSystemLoader(str(_debug_templates_dir))],
                show_toolbar_callback="app.main._toolbar_show_callback",
            )
            # Eagerly register the toolbar's own routes
            # (/_debug_toolbar/render_panel/ + /_debug_toolbar/static mount)
            # NOW, before app.web.router's /{full_path:path} catch-all gets
            # added by include_router(web_router). Otherwise the catch-all
            # swallows the toolbar's own GET requests and the panel scripts
            # render our 404 page. We can't construct DebugToolbarMiddleware
            # directly on the FastAPI app (its `while not isinstance(...,
            # APIRouter): self.router = self.router.app` walk fails — FastAPI
            # has `.router`, not `.app`), so call init_toolbar's body
            # ourselves on the APIRouter directly. add_middleware below still
            # works lazily; init_toolbar's NoMatchFound guard skips re-adding
            # routes when called the second time.
            from debug_toolbar.api import render_panel as _render_panel_view
            from debug_toolbar.middleware import show_toolbar as _show_toolbar
            from debug_toolbar.settings import DebugToolbarSettings
            from fastapi import HTTPException as _HTTPException, status as _status
            from fastapi.staticfiles import StaticFiles as _StaticFiles

            _eager_settings = DebugToolbarSettings(**_toolbar_settings)

            async def _require_show_toolbar(request, call_next=None):
                """Mirror DebugToolbarMiddleware.require_show_toolbar: 404 the
                toolbar API for clients that wouldn't see the toolbar."""
                if not _show_toolbar(request, _eager_settings):
                    raise _HTTPException(status_code=_status.HTTP_404_NOT_FOUND)
                return await _render_panel_view(request)

            app.router.get(
                _eager_settings.API_URL,
                name="debug_toolbar.render_panel",
                include_in_schema=False,
            )(_render_panel_view)
            app.router.mount(
                _eager_settings.STATIC_URL,
                _StaticFiles(packages=["debug_toolbar"]),
                name="debug_toolbar.static",
            )

            app.add_middleware(DebugToolbarMiddleware, **_toolbar_settings)
        except ImportError:
            logger.warning(
                "DEBUG=1 but fastapi-debug-toolbar not installed; toolbar disabled",
            )

    # PostHog HTML snippet injection — must run INSIDE the GZip layer so it
    # sees uncompressed HTML before compression. Starlette runs middleware
    # in reverse-registration order on the response, so registering this
    # before _SelectiveGZipMiddleware places it deeper in the stack and
    # therefore earlier in the response chain. Many of this app's templates
    # are standalone (their own <!DOCTYPE>) and never extend base.html, so
    # a per-template include would miss them; the middleware covers
    # everything in one place. No-op when POSTHOG_API_KEY is unset.
    from app.middleware.posthog_inject import PosthogInjectionMiddleware

    app.add_middleware(PosthogInjectionMiddleware)

    # Compress JSON / HTML responses on the wire. Parquet downloads are
    # excluded — they're already columnar-compressed and re-gzipping them
    # just burns CPU with no size win. minimum_size=1024 keeps tiny
    # responses uncompressed too (cheaper than the header overhead).
    app.add_middleware(
        _SelectiveGZipMiddleware,
        minimum_size=1024,
        skip_prefixes=(
            "/api/data/",
            "/api/mcp",  # SSE stream — do not gzip
            "/cli/wheel/",
            "/cli/download",
            "/marketplace.git",  # git smart-HTTP is self-chunked; double-gzip bloats
        ),
    )

    # Per-IP rate limiting on auth endpoints (#45). Wired here so the
    # SlowAPIMiddleware sits in the standard middleware chain (above CORS,
    # below GZip — order doesn't affect correctness, only metric/log
    # ordering). The limiter singleton is created at import time in
    # app.auth.rate_limit; we just register state + middleware + handler.
    app.state.limiter = _auth_rate_limiter
    app.add_middleware(_AuthRateLimitMiddleware)
    app.add_exception_handler(_AuthRateLimitExceeded, _auth_rate_limit_handler)

    # Session middleware (required for OAuth state)
    from app.secrets import get_session_secret

    session_secret = get_session_secret()
    if len(session_secret) < 32:
        # Same gate JWT applies (app/auth/jwt.py:_get_secret_key) — keeps the
        # two HMAC surfaces consistent. session_internal_roles + google_groups
        # are trusted off the cookie signature; a weak SESSION_SECRET means
        # those gates are weak too.
        import warnings as _warnings

        _warnings.warn(
            f"SESSION_SECRET is {len(session_secret)} chars — minimum 32 recommended",
            UserWarning,
            stacklevel=2,
        )
    app.add_middleware(SessionMiddleware, secret_key=session_secret)

    # CORS for CLI and external clients
    cors_origins = os.environ.get("CORS_ORIGINS", "http://localhost:3000,http://localhost:8000").split(",")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in cors_origins],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # RequestIdMiddleware mounted LAST — Starlette inserts middleware at
    # index 0, so the last add_middleware call ends up OUTERMOST and runs
    # FIRST per request. The request_id ContextVar is set before any
    # downstream middleware or handler runs, and every response gets the
    # x-request-id header.
    app.add_middleware(RequestIdMiddleware)

    # Load .env_overlay (persisted by /api/admin/configure)
    from app.secrets import _state_dir

    _overlay = _state_dir() / ".env_overlay"
    if _overlay.exists():
        for line in _overlay.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

    # Load instance config on startup
    try:
        from app.instance_config import load_instance_config

        load_instance_config()
        logger.info("Instance config loaded")
    except Exception as e:
        logger.warning(f"Could not load instance config: {e}")

    # Configure confidence scoring from instance config (corporate_memory.confidence section)
    try:
        from app.instance_config import get_corporate_memory_config
        from services.corporate_memory.confidence import configure as configure_confidence

        cm_config = get_corporate_memory_config()
        if cm_config and "confidence" in cm_config:
            configure_confidence(cm_config["confidence"])
            logger.info("Corporate memory confidence config applied")
    except Exception as e:
        logger.warning(f"Could not configure corporate memory confidence: {e}")

    # Startup banner
    from src.db import SCHEMA_VERSION

    logger.info(
        "Agnes %s | channel: %s | schema v%s",
        os.environ.get("AGNES_VERSION", "dev"),
        os.environ.get("RELEASE_CHANNEL", "dev"),
        SCHEMA_VERSION,
    )

    # LOCAL_DEV_MODE: bypass authentication for local development. DO NOT enable in prod.
    # When on, every protected route auto-logs in as a seeded admin user (default dev@localhost).
    from app.auth.dependencies import (
        is_local_dev_mode,
        get_local_dev_email,
        get_local_dev_groups,
    )

    if is_local_dev_mode():
        logger.warning("=" * 60)
        logger.warning("LOCAL_DEV_MODE is ON — authentication is bypassed.")
        logger.warning("All requests auto-authenticate as: %s", get_local_dev_email())
        # Validate + report LOCAL_DEV_GROUPS at startup so a malformed JSON
        # value gets surfaced loudly here instead of silently warning on the
        # first authenticated request. Empty when unset is fine — just say so.
        raw_groups_env = os.environ.get("LOCAL_DEV_GROUPS", "").strip()
        mocked_groups = get_local_dev_groups()
        if raw_groups_env and not mocked_groups:
            logger.warning(
                "LOCAL_DEV_GROUPS is set but produced no valid groups — check the WARNING above for the parse error.",
            )
        elif mocked_groups:
            logger.warning(
                "LOCAL_DEV_GROUPS: mocking %d group(s) into session: %s",
                len(mocked_groups),
                ", ".join(g["id"] for g in mocked_groups),
            )
        else:
            logger.warning("LOCAL_DEV_GROUPS is unset — session.google_groups will be empty.")
        logger.warning("NEVER enable this in a deployment reachable from the internet.")
        logger.warning("=" * 60)

    # Guardrails misconfig surface — fail-CLOSED matrix means an enabled
    # pipeline with no LLM credentials in env will hold every submission
    # at `pending_llm` indefinitely. Surface this LOUDLY at boot so the
    # operator finds the cause before the submission queue piles up.
    try:
        from app.instance_config import (
            get_guardrails_enabled,
            get_guardrails_llm_provider_ready,
        )

        if get_guardrails_enabled() and not get_guardrails_llm_provider_ready():
            logger.warning("=" * 60)
            logger.warning(
                "GUARDRAILS ENABLED BUT NO LLM PROVIDER CREDENTIALS FOUND.",
            )
            logger.warning(
                "Set ANTHROPIC_API_KEY (or LLM_API_KEY) in the environment, or disable guardrails in instance.yaml.",
            )
            logger.warning(
                "Until then, every flea-market upload will sit at "
                "status='pending_llm' awaiting admin retry — the LLM "
                "review step cannot run.",
            )
            logger.warning("=" * 60)
    except Exception:
        logger.exception("guardrails readiness probe failed at boot")

    # Static files
    static_dir = Path(__file__).parent / "web" / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # v50 admin-uploaded cover images. Lives under ${DATA_DIR}/uploads so
    # it survives across deploys (the app/web/static dir gets bundled into
    # the container image and is treated as read-only). The directory is
    # lazily created by app/api/uploads.py — we mkdir here too so the
    # StaticFiles mount has a real directory on boot even before the first
    # upload (avoids the "directory does not exist" 500 on cold systems).
    from src.db import _get_data_dir as _ddir_uploads

    uploads_dir = _ddir_uploads() / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    app.mount(
        "/uploads",
        StaticFiles(directory=str(uploads_dir)),
        name="uploads",
    )

    # Auth providers (conditional registration)
    from app.auth.providers.google import router as google_auth_router
    from app.auth.providers.password import router as password_auth_router
    from app.auth.providers.email import router as email_auth_router

    # API routers
    app.include_router(auth_router)
    app.include_router(google_auth_router)
    app.include_router(password_auth_router)
    app.include_router(email_auth_router)  # Always register, check availability per-request
    app.include_router(health_router)
    app.include_router(sync_router)
    app.include_router(data_router)
    app.include_router(query_router)
    app.include_router(users_router)
    app.include_router(memory_router)
    app.include_router(upload_router)
    app.include_router(scripts_router)
    app.include_router(settings_router)
    app.include_router(catalog_router)
    app.include_router(telegram_router)
    app.include_router(admin_router)
    app.include_router(admin_bigquery_test_router)
    app.include_router(admin_keboola_test_router)
    app.include_router(access_router)
    app.include_router(me_access_router)
    app.include_router(me_router)
    app.include_router(me_stats_router)
    app.include_router(jira_webhooks_router)
    app.include_router(metrics_router)
    app.include_router(metadata_router)
    app.include_router(query_hybrid_router)
    app.include_router(cli_artifacts_router)
    app.include_router(cli_auth_router)
    app.include_router(tokens_router)
    app.include_router(tokens_admin_router)
    app.include_router(v2_catalog_router)
    app.include_router(v2_schema_router)
    app.include_router(v2_sample_router)
    app.include_router(v2_scan_router)
    app.include_router(v2_marketplace_router)
    app.include_router(marketplaces_router)
    app.include_router(data_packages_router)
    app.include_router(admin_mcp_router)
    app.include_router(admin_slack_secrets_router)
    app.include_router(mcp_passthrough_router)
    app.include_router(mcp_user_secrets_router)
    app.include_router(mcp_per_table_router)
    app.include_router(memory_domains_router)
    app.include_router(recipes_public_router)
    app.include_router(recipes_admin_router)
    app.include_router(memory_domain_suggestions_public_router)
    app.include_router(memory_domain_suggestions_admin_router)
    app.include_router(authoring_suggestions_public_router)
    app.include_router(authoring_suggestions_admin_router)
    app.include_router(memory_mining_public_router)
    app.include_router(memory_mining_admin_router)
    app.include_router(admin_uploads_router)
    app.include_router(collections_router)
    app.include_router(stack_router)
    app.include_router(stack_views_router)
    app.include_router(initial_workspace_router)
    app.include_router(config_surface_router)
    app.include_router(store_router)
    app.include_router(my_stack_router)
    app.include_router(marketplace_router)
    app.include_router(welcome_router)
    app.include_router(connectors_router)
    app.include_router(claude_md_router)
    app.include_router(prompts_router)
    app.include_router(news_router)
    app.include_router(cowork_user_router)
    app.include_router(cowork_auth_router)

    # HTTP MCP (SSE transport) for cowork VM access — must be mounted before
    # web_router's catch-all. GZip excluded below via skip_prefixes.
    app.mount("/api/mcp", _make_mcp_sse_app())

    app.include_router(cache_warmup_router)
    app.include_router(bq_metadata_refresh_router)
    app.include_router(activity_router)
    app.include_router(observability_router)
    app.include_router(admin_user_sessions_router)
    app.include_router(admin_sessions_router)
    app.include_router(admin_usage_router)
    app.include_router(admin_usage_summary_router)
    app.include_router(admin_adoption_router)
    app.include_router(db_state_router)
    app.include_router(marketplace_server_router)
    app.include_router(chat_router)
    app.include_router(chat_copresence_router)
    app.include_router(slack_router)
    app.include_router(admin_chat_router)

    # Git smart-HTTP endpoint for Claude Code: /marketplace.git/*
    # WSGI → ASGI bridge (dulwich is WSGI-native; FastAPI is ASGI).
    from a2wsgi import WSGIMiddleware

    app.mount("/marketplace.git", WSGIMiddleware(make_git_wsgi_app()))

    # Authenticated Swagger / ReDoc / OpenAPI JSON — requires a valid session
    # so the full admin API surface is not visible to unauthenticated callers.
    # Must be registered before web_router (catch-all). /openapi.json is also
    # added to _API_PATH_PREFIXES below so auth failures return JSON 401
    # rather than an HTML redirect.
    from fastapi.openapi.docs import get_swagger_ui_html, get_redoc_html
    from fastapi.responses import HTMLResponse as _HTMLResponse
    from app.auth.dependencies import get_current_user as _get_current_user

    @app.get("/docs", include_in_schema=False, response_class=_HTMLResponse)
    async def swagger_ui(user: dict = Depends(_get_current_user)):
        return get_swagger_ui_html(openapi_url="/openapi.json", title="Agnes API")

    @app.get("/redoc", include_in_schema=False, response_class=_HTMLResponse)
    async def redoc_ui(user: dict = Depends(_get_current_user)):
        return get_redoc_html(openapi_url="/openapi.json", title="Agnes API — ReDoc")

    @app.get("/openapi.json", include_in_schema=False)
    async def openapi_spec(user: dict = Depends(_get_current_user)):
        return app.openapi()

    # Deployment-specific plugin admin routers (generic hook — see app/plugins.py).
    # Mounted before the web_router catch-all so their API paths win. The configured
    # specs come from the operator's instance.yaml; nothing deployment-specific lives here.
    from app.instance_config import get_value as _get_value
    from app.plugins import load_routers as _load_plugin_routers

    for _plugin_router in _load_plugin_routers(
        _get_value("plugins", "admin_routers", default=[]) or []
    ):
        app.include_router(_plugin_router)

    # Web UI router (must be last — has catch-all routes)
    app.include_router(web_router)

    # Paths served as API responses (JSON / ZIP / git smart-HTTP) — never
    # redirect a 401 here to the HTML login page; clients expect the raw 401.
    _API_PATH_PREFIXES: tuple[str, ...] = (
        "/api/",
        "/auth/",
        "/cli/",
        "/openapi.json",
        "/webhooks/",
        "/marketplace.zip",
        "/marketplace.git",
        "/marketplace/",
        "/admin/chat",
    )

    _ERROR_TITLES = {
        400: "Bad request",
        401: "Sign-in required",
        403: "Forbidden",
        404: "Page not found",
        405: "Method not allowed",
        408: "Request timeout",
        413: "Payload too large",
        422: "Unprocessable entity",
        429: "Too many requests",
        500: "Server error",
        502: "Bad gateway",
        503: "Service unavailable",
        504: "Gateway timeout",
    }

    def _wants_html(request) -> bool:
        """True when the client looks like a browser (non-API path, explicit html).

        We deliberately do NOT treat ``Accept: */*`` (curl's default) or an
        empty Accept header as wanting HTML. curl-using operators were
        getting JSON error bodies for non-API paths before this PR; matching
        ``*/*`` here would silently flip them to HTML and break tooling that
        parses ``{"detail": "..."}``. A real browser sends
        ``Accept: text/html,application/xhtml+xml,...`` so the explicit
        substring check below covers that case.
        Devin ANALYSIS_0003 on PR #136 review.
        """
        if request.url.path.startswith(_API_PATH_PREFIXES):
            return False
        accept = request.headers.get("accept", "")
        return "text/html" in accept

    async def _resolve_error_user(request) -> dict | None:
        """Best-effort user resolution for the error page header.

        Mirrors ``app.auth.dependencies.get_optional_user`` precedence
        (LOCAL_DEV_MODE → seeded dev user, else verify JWT from
        Authorization header or ``access_token`` cookie). Returns None on
        any failure — error page still renders, just without the user menu.
        """
        try:
            from app.auth.dependencies import get_current_user
            from src.db import get_system_db

            conn = get_system_db()
            try:
                authorization = request.headers.get("authorization")
                return await get_current_user(request=request, authorization=authorization, conn=conn)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception:
            return None

    async def _render_error(request, code: int, message: str, traceback_str: str | None = None):
        """Render error.html with the same chrome (header, theme, static_url)
        as any other web route. Reuses ``_build_context`` so the page picks up
        ConfigProxy, theme overrides, session user, and ``static_url`` /
        ``url_for`` helpers — without these, base.html + _app_header.html
        silently render empty header/stylesheets."""
        from app.logging_config import request_id_var
        from app.web.router import templates as _web_templates, _build_context

        title = _ERROR_TITLES.get(code, "Error")
        user = await _resolve_error_user(request)
        ctx = _build_context(
            request,
            user=user,
            code=code,
            title=title,
            message=message,
            path=request.url.path,
            traceback=traceback_str,
            request_id=request_id_var.get(),
        )
        return _web_templates.TemplateResponse(request, "error.html", ctx, status_code=code)

    @app.exception_handler(StarletteHTTPException)
    async def _html_auth_redirect_handler(request, exc: StarletteHTTPException):
        """Browser-friendly error rendering for HTML routes; JSON for API routes.

        - 401 GET on a non-API path → redirect to ``/login`` (existing contract).
        - Any other status code on a non-API path with HTML-accepting client →
          render ``error.html`` (toolbar middleware injects panels because the
          ``_catch_all_404`` route at the end of ``app.web.router`` provides a
          matched route for unrouted paths).
        - API prefixes (``/api/``, ``/auth/``, ``/marketplace.zip``,
          ``/marketplace.git``, ``/marketplace/``) and non-HTML clients → JSON
          ``{"detail": "..."}`` per the existing contract.
        """
        path_is_api = request.url.path.startswith(_API_PATH_PREFIXES)

        if exc.status_code == 401 and request.method == "GET" and not path_is_api:
            next_param = quote(request.url.path, safe="")
            return RedirectResponse(url=f"/login?next={next_param}", status_code=302)

        if not path_is_api and _wants_html(request):
            return await _render_error(request, exc.status_code, exc.detail or "")

        from fastapi.exception_handlers import http_exception_handler

        return await http_exception_handler(request, exc)

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request, exc: Exception):
        """Catch-all 500 handler — HTML for browsers, JSON for API clients."""
        import os as _os
        import traceback as _tb

        logger.exception("Unhandled exception on %s %s", request.method, request.url.path)

        # Best-effort: forward the exception to PostHog before rendering the
        # error page. Disabled state is a cheap no-op. Wrapped because a
        # tracing failure must never replace the user-visible 500 with a
        # second exception.
        try:
            from src.observability import get_posthog
            from app.logging_config import request_id_var as _rid_var

            get_posthog().capture_exception(
                exc,
                request=request,
                properties={
                    "request_id": _rid_var.get(),
                    "path": request.url.path,
                    "method": request.method,
                },
            )
        except Exception:
            logger.exception("PostHog capture_exception failed in 500 handler")

        path_is_api = request.url.path.startswith(_API_PATH_PREFIXES)
        debug_on = _os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")
        tb_str = _tb.format_exc() if debug_on else None

        if not path_is_api and _wants_html(request):
            # In production (DEBUG unset), never leak str(exc) to the
            # rendered page — exception messages routinely contain DB paths,
            # SQL fragments, internal hostnames, or credentials embedded in
            # connection strings. Match the JSON branch's debug_on guard.
            # Devin BUG_0001 on PR #136 (b1c6ee9 review).
            visible_message = str(exc) if debug_on else "Internal server error"
            return await _render_error(request, 500, visible_message, tb_str)

        from app.logging_config import request_id_var
        from fastapi.responses import JSONResponse

        body: dict[str, str | None] = {
            "detail": "Internal server error",
            "request_id": request_id_var.get(),
        }
        if debug_on:
            body["error"] = str(exc)
        return JSONResponse(body, status_code=500)

    _patch_openapi_auth_errors(app)

    return app


# ---------------------------------------------------------------------------
# OpenAPI schema post-processing
# ---------------------------------------------------------------------------

#: Paths that are intentionally unauthenticated. Every other /api/* route
#: gets 401 and 403 injected into its declared responses so the spec truthfully
#: reflects that auth errors are possible. FastAPI cannot derive these from
#: Depends() chains automatically.
_PUBLIC_API_PATHS = frozenset(
    {
        "/api/health",
        "/api/health/detailed",
        "/api/version",
    }
)

_HTTP_METHODS = frozenset({"get", "post", "put", "delete", "patch"})


def _add_auth_error_responses(schema: dict) -> dict:
    """Inject 401/403 into every protected /api/* operation."""
    _401 = {"description": "Not authenticated"}
    _403 = {"description": "Insufficient permissions"}
    for path, methods in schema.get("paths", {}).items():
        if not path.startswith("/api/") or path in _PUBLIC_API_PATHS:
            continue
        for method, op in methods.items():
            if method not in _HTTP_METHODS:
                continue
            responses = op.setdefault("responses", {})
            responses.setdefault("401", _401)
            responses.setdefault("403", _403)
    return schema


def _patch_openapi_auth_errors(app: "FastAPI") -> None:
    """Wrap app.openapi() to call _add_auth_error_responses on every generation."""
    original = app.openapi

    def patched() -> dict:
        schema = original()
        return _add_auth_error_responses(schema)

    app.openapi = patched  # type: ignore[method-assign]


app = create_app()
