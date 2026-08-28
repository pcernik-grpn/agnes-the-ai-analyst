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
    RequiresPostgresBackend,
    memory_domains_repo,
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

import asyncio
import contextlib
import logging
import math
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import os

# Initialise structured logging BEFORE any module that emits logs at import
# time. setup_logging is idempotent and safe to call once at process start.
from app.logging_config import setup_logging

setup_logging("app")

from app.version import APP_VERSION, MIN_COMPAT_CLI_VERSION, SERVER_CAPABILITIES

from fastapi import Depends, FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from app.middleware.request_id import RequestIdMiddleware


def _chat_coordination_backend() -> str:
    """Thin wrapper around :func:`app.coordination.factory.resolve_backend_name`
    so the multi-worker/multi-replica chat gate and the Slack Socket Mode
    preflight (below) resolve the backend the same way every other
    coordination-aware call site does, and so tests can monkeypatch this one
    function (``app.main._chat_coordination_backend``) instead of reaching
    into ``app.coordination.factory``.
    """
    from app.coordination.factory import resolve_backend_name

    return resolve_backend_name()


def _chat_jwt_secret_ok(chat_config) -> bool:
    """Refuse ``chat.enabled=true`` deployments that lack a real
    ``JWT_SECRET_KEY`` (unset or shorter than 32 bytes).

    The chat path mints session JWTs that authenticate the sandboxed
    runner back to the Agnes server.  ``app.auth.jwt._get_secret_key`` is
    fail-closed — production without ``JWT_SECRET_KEY`` refuses to boot,
    and local dev signs with an auto-generated per-instance key — so this
    gate is the narrower, earlier check: it names chat as the reason and
    logs it, rather than letting the deployment die at lifespan with a
    generic message, and it rejects a key local dev would otherwise accept
    (auto-generated or shorter than 32 bytes).  Anything reachable here is
    a misconfiguration, never the committed test constant: that one is
    gated on ``TESTING=1`` in jwt.py and reachable from no server path.

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


def _chat_llm_provider_ok(chat_config) -> bool:
    """Validate ``chat.llm.provider`` — refuse unknown values and a
    misconfigured vertex mode at boot.

    Wired into the lifespan elif chain BEFORE ``_chat_anthropic_key_ok`` so
    its message wins over a misleading "ANTHROPIC_API_KEY missing" log. An
    unknown provider is refused rather than silently mapped to ``anthropic``
    (a fallback would switch which credential spends money). Config-shape
    conflicts are checked before the TESTING bypass (mirroring
    ``_chat_kai_agent_ok``); only the ADC credential probe is bypassed under
    tests.
    """
    if not chat_config.enabled:
        return True
    log = logging.getLogger("app.main")
    provider = getattr(chat_config, "llm_provider", "anthropic") or "anthropic"
    if provider == "anthropic":
        return True
    if provider != "vertex":
        log.error(
            "chat.llm.provider=%r is not a known provider (allowed: anthropic, vertex); refusing to spawn ChatManager",
            provider,
        )
        return False
    if getattr(chat_config, "llm_auth", "api_key") == "workload_identity":
        log.error(
            "chat.llm.provider=vertex conflicts with chat.llm.auth=workload_identity — "
            "vertex signs upstream requests with Google ADC, workload_identity federates "
            "to the first-party Anthropic API; pick one. Refusing to spawn ChatManager",
        )
        return False
    if os.environ.get("LLM_DISPATCHER_URL", "").strip():
        log.error(
            "LLM_DISPATCHER_URL is set but chat.llm.provider=vertex — the dispatcher "
            "only speaks the first-party Messages API; unset one of the two. "
            "Refusing to spawn ChatManager",
        )
        return False
    missing = [
        key
        for key, value in (
            ("chat.llm.vertex.project_id", getattr(chat_config, "vertex_project_id", "")),
            ("chat.llm.vertex.region", getattr(chat_config, "vertex_region", "")),
        )
        if not value
    ]
    if missing:
        log.error(
            "chat.llm.provider=vertex requires %s to be set in instance.yaml; refusing to spawn ChatManager",
            " and ".join(missing),
        )
        return False
    # Both values are interpolated into the outbound Vertex URL — the region
    # into the HOSTNAME — and are compared for equality against the project /
    # location a sandbox request's path parses to. A value outside the Google
    # resource-id character set would therefore either 403 every request with
    # no boot-time signal, or send the server-side Google OAuth token to a
    # host that is not Google's. Refuse it here instead.
    from connectors.llm.vertex_provider import invalid_vertex_setting

    bad = invalid_vertex_setting(
        getattr(chat_config, "vertex_project_id", ""),
        getattr(chat_config, "vertex_region", ""),
    )
    if bad:
        log.error(
            "chat.llm.vertex.%s is malformed — it is interpolated into the Vertex API "
            "URL (the region becomes part of the hostname) and matched against the "
            "project/location of every sandbox request, so it is held to the Google "
            "resource-id character set. Refusing to spawn ChatManager",
            bad,
        )
        return False
    if os.environ.get("TESTING", "").lower() in ("1", "true"):
        return True
    from app.auth.vertex_gcp import credentials_resolvable

    ok, detail = credentials_resolvable()
    if not ok:
        log.error(
            "chat.llm.provider=vertex but Google credentials are not resolvable: %s. "
            "Set GOOGLE_APPLICATION_CREDENTIALS, run `gcloud auth application-default "
            "login`, or attach a service account to the workload. Refusing to spawn "
            "ChatManager",
            detail,
        )
        return False
    return True


def _chat_anthropic_key_ok(chat_config) -> bool:
    """Refuse ``chat.enabled=true`` deployments that lack ``ANTHROPIC_API_KEY``.

    The chat runner inside the sandbox calls the Anthropic API on
    behalf of each user.  If the key is absent the runner silently fails
    on its first API call.  Refuse to enable chat and surface a fatal
    log so the operator finds the cause immediately rather than after
    users start reporting mysterious errors.

    Returns True when chat is disabled (irrelevant) or when
    ``ANTHROPIC_API_KEY`` is set to a non-empty value; False otherwise.
    """
    if not chat_config.enabled:
        return True
    # Vertex mode needs no Anthropic credential at all — its own gate
    # (_chat_llm_provider_ok, ordered before this one) validated the Google
    # credential chain instead.
    if getattr(chat_config, "llm_provider", "anthropic") == "vertex":
        return True
    # Bypass for TESTING=1 — pytest-driven sessions don't need a real key.
    if os.environ.get("TESTING", "").lower() in ("1", "true"):
        return True
    # Keyless (workload_identity): there is NO static ANTHROPIC_API_KEY by
    # design — the broker mints a federated token from the workload's own
    # identity. Validate the federation env instead so a misconfigured WIF
    # deployment fails loudly at startup rather than with a runtime 502 on the
    # first completion.
    if getattr(chat_config, "llm_auth", "api_key") == "workload_identity":
        missing = [
            var
            for var in (
                "ANTHROPIC_FEDERATION_RULE_ID",
                "ANTHROPIC_ORGANIZATION_ID",
                "ANTHROPIC_SERVICE_ACCOUNT_ID",
            )
            if not os.environ.get(var, "").strip()
        ]
        if not (
            os.environ.get("ANTHROPIC_IDENTITY_TOKEN", "").strip()
            or os.environ.get("ANTHROPIC_IDENTITY_TOKEN_FILE", "").strip()
        ):
            missing.append("ANTHROPIC_IDENTITY_TOKEN|ANTHROPIC_IDENTITY_TOKEN_FILE")
        if missing:
            logging.getLogger("app.main").error(
                "chat.llm.auth=workload_identity requires the federation env to be set "
                "(missing: %s); refusing to spawn ChatManager",
                ", ".join(missing),
            )
            return False
        return True
    if os.environ.get("ANTHROPIC_API_KEY", ""):
        return True
    logging.getLogger("app.main").error(
        "chat.enabled=true requires ANTHROPIC_API_KEY env to be set; refusing to spawn ChatManager",
    )
    return False


def _chat_kai_agent_ok(chat_config) -> bool:
    """Refuse ``chat.provider=kai-agent`` without the engine host wiring.

    The provider authenticates every engine call with a session JWT signed by
    ``KAI_HOST_JWT_SECRET`` (the same shared secret that turns on the
    ``/api/kai/*`` host surface the engine itself depends on — tickets, LLM
    broker, workspace). Without it every spawn would 503 at mint time; refuse
    the manager at boot instead, mirroring the docker gates.
    """
    if not chat_config.enabled:
        return True
    if chat_config.provider != "kai-agent":
        return True
    url = getattr(chat_config, "kai_agent_url", "") or ""
    if not url.startswith(("http://", "https://")):
        # Every engine call carries the session bearer token to this URL —
        # refuse a shape that cannot be the compose-internal engine rather
        # than let a typo ship credentials somewhere surprising.
        logging.getLogger("app.main").error(
            "chat.provider=kai-agent with chat.kai_agent_url=%r — must be an "
            "http(s) URL (default http://kai-agent:3000); refusing to spawn ChatManager",
            url,
        )
        return False
    if os.environ.get("TESTING", "").lower() in ("1", "true"):
        return True
    if os.environ.get("KAI_HOST_JWT_SECRET", "").strip():
        return True
    logging.getLogger("app.main").error(
        "chat.enabled=true with provider=kai-agent requires KAI_HOST_JWT_SECRET "
        "env (the embedded engine's shared secret — the same one that enables "
        "/api/kai/*); refusing to spawn ChatManager",
    )
    return False


def _chat_harness_ok(chat_config) -> bool:
    """Refuse an explicitly configured ``chat.harness`` outside the
    ``APPROVED_HARNESSES`` allowlist (app/chat/harness.py seam).

    Explicit-invalid refuses at boot; the runner separately degrades an
    *inherited* unknown id to the default (version-skew tolerance).
    """
    if not chat_config.enabled:
        return True
    from app.chat.harness import APPROVED_HARNESSES

    harness = getattr(chat_config, "harness", "claude-code")
    if harness in APPROVED_HARNESSES:
        return True
    logging.getLogger("app.main").error(
        "chat.harness=%r is not an approved harness (approved: %s); "
        "refusing to spawn ChatManager. Fix chat.harness in instance.yaml.",
        harness,
        ", ".join(APPROVED_HARNESSES),
    )
    return False


def _chat_docker_rails_url_ok(chat_config) -> bool:
    """Refuse ``chat.provider=docker`` without a container-reachable rails URL.

    ``agnes_server_url()`` falls back to ``http://127.0.0.1:8000`` when neither
    ``SERVER_URL`` nor ``AGNES_INTERNAL_URL`` is set. Inside a container's own
    network namespace that address is the *sandbox*, not Agnes, so every
    brokered call (the sandbox's only network dependency) would fail with a
    connection error deep inside a user's chat. Never silently default around
    it — fail at boot with the fix in the log line.

    Returns True when chat is disabled or the provider is not ``docker``.
    """
    if not chat_config.enabled:
        return True
    if chat_config.provider != "docker":
        return True
    if os.environ.get("TESTING", "").lower() in ("1", "true"):
        return True
    url = (os.environ.get("SERVER_URL") or os.environ.get("AGNES_INTERNAL_URL") or "").strip()
    log = logging.getLogger("app.main")
    if not url:
        log.error(
            "chat.enabled=true with provider=docker requires SERVER_URL or "
            "AGNES_INTERNAL_URL to be set to a URL the sandbox container can "
            "reach (e.g. AGNES_INTERNAL_URL=http://app:8000 under compose, or "
            "http://host.docker.internal:8000 on a bare host); refusing to "
            "spawn ChatManager",
        )
        return False
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    if host in ("127.0.0.1", "localhost", "::1", "0.0.0.0") or host.startswith("127."):
        log.error(
            "chat.enabled=true with provider=docker but the sandbox rails URL "
            "is loopback (%s) — inside the sandbox's network namespace that is "
            "the sandbox itself, not Agnes. Set AGNES_INTERNAL_URL to a "
            "container-reachable address; refusing to spawn ChatManager",
            url,
        )
        return False
    if url.lower().startswith("https://"):
        # Not a refusal — a public-CA cert verifies fine from the sandbox —
        # but the common self-hosted shape (reverse proxy with a private-CA
        # cert) fails every brokered call, and even a valid public origin
        # routes sandbox↔Agnes traffic out through the proxy. docs/cloud-chat.md
        # says to prefer the plain-HTTP internal address.
        log.warning(
            "provider=docker: the sandbox rails URL resolves to an https:// "
            "origin (%s). If that certificate is from a private CA the "
            "in-sandbox relay will reject it (no CA-bundle knob) and every "
            "brokered call will fail — prefer AGNES_INTERNAL_URL with the "
            "plain-HTTP internal address (e.g. http://app:8000)",
            url,
        )
    return True


async def _chat_docker_sandbox_ok(chat_config) -> bool:
    """Refuse ``chat.provider=docker`` when the sandbox runner isn't usable.

    Probes the apps-runner sidecar (the only process holding the Docker socket)
    for daemon reachability + the configured image. A present-but-unbuilt setup
    otherwise only surfaces at the first user's spawn.

    Never raises: a transport failure is a refusal with an actionable log line,
    mirroring the other chat boot gates' behavior.
    """
    if not chat_config.enabled:
        return True
    if chat_config.provider != "docker":
        return True
    if os.environ.get("TESTING", "").lower() in ("1", "true"):
        return True
    from app.chat.sandbox_runner_client import SandboxRunnerClient

    image = getattr(chat_config, "docker_image", "") or ""
    try:
        # Short deadline: this runs inline in the lifespan, and the client's
        # 60 s default would stall the whole server start on a black-holing
        # sidecar address — long enough to trip container health checks.
        # 8 s matches the admin test-connections probe for the same sidecar.
        result = await SandboxRunnerClient(timeout=8.0).probe(image)
    except Exception as exc:  # noqa: BLE001 — classify, never break the lifespan
        logging.getLogger("app.main").error(
            "chat.enabled=true with provider=docker but the apps-runner sidecar "
            "is unreachable (%s). Start it with `docker compose --profile apps up "
            "-d apps-runner` (or `python -m services.apps_runner` on a bare host) "
            "and set APPS_RUNNER_URL/APPS_RUNNER_TOKEN; refusing to spawn ChatManager",
            exc,
        )
        return False
    if not result.get("ok"):
        logging.getLogger("app.main").error(
            "chat.enabled=true with provider=docker but the sandbox runner is not "
            "ready: %s. Build the image from app/initial_workspace_default/"
            "docker-sandbox/ and set chat.docker_image; refusing to spawn ChatManager",
            result.get("detail", "unknown"),
        )
        return False
    return True


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
from app.api.jobs import router as jobs_router
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
from app.api.admin_doctor import router as admin_doctor_router
from app.api.admin_keboola_test import router as admin_keboola_test_router
from app.api.attachments import router as attachments_router
from app.api.jira_webhooks import router as jira_webhooks_router
from app.api.metrics import router as metrics_router
from app.api.glossary import router as glossary_router
from app.api.semantic_models import router as semantic_models_router
from app.api.metadata import router as metadata_router
from app.api.query_hybrid import router as query_hybrid_router
from app.api.cli_artifacts import router as cli_artifacts_router
from app.api.cli_auth import router as cli_auth_router
from app.api.tokens import router as tokens_router, admin_router as tokens_admin_router
from app.api.agents_admin import router as agents_admin_router
from app.api.agent_runtime import router as agent_runtime_router  # noqa: E402
from app.api.agent_sessions import router as agent_sessions_router  # noqa: E402
from app.api.agent_webhooks import router as agent_webhooks_router  # noqa: E402
from app.api.agent_memory import router as agent_memory_router  # noqa: E402
from app.api.agent_schedules import router as agent_schedules_router  # noqa: E402
from app.api.v2_catalog import router as v2_catalog_router
from app.api.v2_schema import router as v2_schema_router
from app.api.v2_sample import router as v2_sample_router
from app.api.v2_scan import router as v2_scan_router
from app.api.v2_marketplace import router as v2_marketplace_router
from app.api.marketplaces import router as marketplaces_router
from app.api.data_packages import router as data_packages_router
from app.api.admin_mcp import router as admin_mcp_router
from app.api.admin_contributed_skills import router as admin_contributed_skills_router
from app.api.admin_datasource_secrets import router as admin_datasource_secrets_router
from app.api.admin_slack_secrets import router as admin_slack_secrets_router
from app.api.admin_source_connections import router as source_connections_admin_router
from app.api.admin_source_discovery import router as source_discovery_admin_router
from app.api.mcp_passthrough import router as mcp_passthrough_router
from app.api.mcp_per_table import router as mcp_per_table_router
from app.api.mcp_user_secrets import router as mcp_user_secrets_router
from app.api.mcp_oauth_connect import router as mcp_oauth_connect_router
from app.api.memory_domains import router as memory_domains_router
from app.api.knowledge_digests import router as knowledge_digests_router
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

# `app.api.agents` is gone — /api/agents was retired into /api/v1/agents
# (Task C1.2) and the module deleted on main, so only the builder routers
# survive this merge.
from app.api.agent_builder import router as agent_builder_router  # builder assistant turns
from app.api.entity_builder import router as entity_builder_router  # /skills builder turns
from app.api.package_builder import router as package_builder_router  # data-package builder turns
from app.api.mcp_builder import router as mcp_builder_router  # MCP-source builder turns
from app.api.facts import router as facts_router  # fact graph over Collections read surface
from app.api.sharing import router as sharing_router  # owner-initiated Library sharing
from app.api.knowledge_search import router as knowledge_search_router  # K2: unified search
from app.api.stack import router as stack_router
from app.api.stack_views import router as stack_views_router
from app.api.initial_workspace import router as initial_workspace_router
from app.api.config_surface import router as config_surface_router
from app.api.store import router as store_router
from app.api.store_lint_admin import router as store_lint_admin_router
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
from app.api.mcp_connect import router as mcp_connect_router  # noqa: E402
from app.api.mcp_http import make_sse_app as _make_mcp_sse_app
from app.api.mcp_streamable import _make_streamable_app as _make_mcp_streamable_app
from app.api.mcp_streamable import _mcp_oauth_discovery_routes
from app.api.mcp_streamable import mount_root_route as _mcp_streamable_mount_root_route
from app.auth.mcp_oauth import make_consent_routes as _make_mcp_consent_routes
from app.api.cache_warmup import router as cache_warmup_router
from app.api.bq_metadata_refresh import router as bq_metadata_refresh_router
from app.api.keboola_semantic_layer_refresh import router as keboola_semantic_layer_refresh_router
from app.api.databricks_semantic_layer_refresh import router as databricks_semantic_layer_refresh_router
from app.api.activity import router as activity_router
from app.api.observability import router as observability_router
from app.api.admin_user_sessions import router as admin_user_sessions_router
from app.api.admin_sessions import router as admin_sessions_router
from app.api.admin_usage import router as admin_usage_router
from app.api.admin_usage_summary import router as admin_usage_summary_router
from app.api.admin_reports import router as admin_reports_router
from app.api.admin_dashboard import router as admin_dashboard_router
from app.api.admin_adoption import router as admin_adoption_router
from app.api.db_state import router as db_state_router
from app.api.admin_analytics import router as admin_analytics_router
from app.marketplace_server.router import router as marketplace_server_router
from app.marketplace_server.git_router import router as marketplace_git_router
from app.api.data_apps import router as data_apps_router
from app.api.data_apps_git import router as data_apps_git_router
from app.api.data_apps_proxy import router as data_apps_proxy_router
from app.web.router import router as web_router
from app.web.router import apps_web_router as data_apps_web_router
from app.api.chat import router as chat_router
from app.api.chat_session_files import router as chat_session_files_router
from app.api.chat_uploads import router as chat_uploads_router
from app.api.chat_copresence import router as chat_copresence_router
from app.api.slack import router as slack_router
from app.api.admin_chat import router as admin_chat_router
from app.api.notifications_ws import router as notifications_ws_router
from app.api.broker import router as broker_router
from app.api.kai import router as kai_router
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
    crash and never start a dead WS.

    Gateway-role gating and the token/workers preflight decide whether
    this PROCESS participates at all — unrelated to cross-process
    exclusivity, so both stay outside the lease below (a non-gateway
    replica, or one with a broken Slack config, never even tries to
    acquire the lease).

    Once a process passes those gates, it races every other
    Role.GATEWAY replica for the `slack-socket-mode` leader lease (see
    app/coordination/leases.py) — Slack's Socket Mode protocol allows
    multiple concurrent WS connections per app, but each delivers a
    disjoint slice of at-least-once events, so multiple replicas
    dispatching independently would double-handle events. The lease
    task is stashed on app.state for shutdown (cancelling it runs the
    lease's own stop()+release() path — see the lifespan teardown
    below).

    FLUSHALL story: if the coordination backend loses its state (Redis
    FLUSHALL/restart, or an outage outliving one ttl_s), this replica's
    lease renew fails -> the dispatcher is stopped -> the lease loop
    re-enters acquire-polling -> some gateway replica (maybe this one)
    re-acquires and reconnects within one ttl_s. In the default `memory`
    backend (single-process) this never happens — the lease is
    process-local and always immediately acquired, so behavior is
    unchanged from before leases existed.
    """
    from app.roles import Role, role_enabled

    app.state.slack_socket_dispatcher = None
    app.state.slack_socket_lease_task = None
    if not role_enabled(Role.GATEWAY):
        logger.info("slack socket mode: skipped (not a gateway-role process)")
        return
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
        backend=_chat_coordination_backend(),
    )
    if not ok:
        logger.error("Slack Socket Mode disabled: %s", reason)
        return

    async def _start() -> None:
        try:
            dispatcher = SocketModeDispatcher(
                app=app,
                app_token=app_token,
                bot_token=bot_token,
            )
            await dispatcher.start()
            app.state.slack_socket_dispatcher = dispatcher
        except Exception:
            # Log here (so the failure is attributed to Slack Socket Mode
            # specifically) then re-raise: a connect failure must propagate
            # to run_with_lease, which releases the lease and backs off
            # before retrying (see app/coordination/leases.py). Swallowing
            # it here would leave this replica believing it holds the
            # lease — and renewing it forever — while never actually
            # delivering events, starving every other (possibly healthy)
            # gateway replica.
            logger.exception("Slack Socket Mode start() failed")
            app.state.slack_socket_dispatcher = None
            raise

    async def _stop() -> None:
        dispatcher = getattr(app.state, "slack_socket_dispatcher", None)
        app.state.slack_socket_dispatcher = None
        if dispatcher is not None:
            try:
                await dispatcher.stop()
            except Exception:
                logger.exception("Slack Socket Mode dispatcher stop failed")

    from app.coordination.leases import default_holder_id, run_with_lease

    app.state.slack_socket_lease_task = asyncio.create_task(
        run_with_lease("slack-socket-mode", default_holder_id(), ttl_s=15, start=_start, stop=_stop),
        name="slack-socket-lease",
    )
    # Yield one scheduler tick so the lease task gets a chance to run before
    # this function returns to the lifespan. This does NOT guarantee the
    # dispatcher's start() (the socket connect) has completed by the time we
    # return — lease_acquire/renew/release all hop off-loop via
    # asyncio.to_thread (see app/coordination/leases.py), so completion now
    # depends on real thread scheduling, not just one tick. Startup simply
    # kicks the lease loop off without waiting for the connect to finish;
    # the dispatcher comes up asynchronously shortly after.
    await asyncio.sleep(0)


def _state_checkpoint_interval_s() -> float:
    """Interval for the periodic system.duckdb CHECKPOINT task (#710).

    ``AGNES_STATE_CHECKPOINT_INTERVAL_S`` env override; default 300 s.
    ``0`` (or any non-positive value) disables the task. Unparsable
    values fall back to the default rather than silently disabling a
    durability safeguard.
    """
    raw = os.environ.get("AGNES_STATE_CHECKPOINT_INTERVAL_S")
    if raw is None or raw.strip() == "":
        return 300.0
    try:
        interval = float(raw)
    except ValueError:
        logger.warning("AGNES_STATE_CHECKPOINT_INTERVAL_S=%r is not a number; using default 300s", raw)
        return 300.0
    if not math.isfinite(interval):
        # nan would silently disable the task (max(nan, 0) is nan, nan > 0 is
        # False) and inf would sleep forever — both defeat a durability
        # safeguard, so treat them like unparsable input.
        logger.warning("AGNES_STATE_CHECKPOINT_INTERVAL_S=%r is not finite; using default 300s", raw)
        return 300.0
    return max(interval, 0.0)


async def _state_checkpoint_loop(interval_s: float) -> None:
    """Periodically fold the system.duckdb WAL into the main file (#710),
    and — on the cadence configured separately — refresh the rolling
    recovery snapshot (#380).

    The app's long-lived singleton connection makes DuckDB defer its own
    threshold checkpoint indefinitely, so without this loop the state-DB
    WAL grows unbounded between graceful restarts (observed: 18.7 MB /
    2 days on prod) and a non-graceful exit puts days of user/PAT/grant
    writes at the mercy of a cross-version WAL replay. CHECKPOINT runs in
    a worker thread — it can block while DuckDB flushes a large WAL.
    Threaded calls go through ``to_thread_drain_on_cancel`` so shutdown
    cancellation waits for an in-flight CHECKPOINT/read instead of letting
    the lifespan's ``close_system_db()`` race it (see that helper's
    docstring in ``app/api/health_probes.py``).

    ``refresh_rolling_snapshot`` piggybacks on this same tick rather than
    getting its own loop/timer: it needs the identical locking discipline
    (the app's own ``system.duckdb`` singleton, never a second connection),
    so reusing this loop is both the simplest wiring and the only safe one.
    It self-gates on its own (much coarser) cadence — see
    ``backups.rolling_snapshot_interval_hours`` — so most ticks here no-op
    for it in a single mtime check.
    """
    from app.api.health_probes import to_thread_drain_on_cancel
    from app.secrets import reapply_all_overlay_tokens_from_vault
    from src.db import checkpoint_operational_db, checkpoint_system_db, refresh_rolling_snapshot

    while True:
        await asyncio.sleep(interval_s)
        try:
            await to_thread_drain_on_cancel(checkpoint_system_db)
            # operational.duckdb is a second long-lived singleton with the same
            # unbounded-WAL exposure; both accessors no-op when their singleton
            # isn't open, so this is cheap on every backend.
            await to_thread_drain_on_cancel(checkpoint_operational_db)
        except Exception:
            # checkpoint_*_db already swallow DB errors; this guards the loop
            # itself (e.g. to_thread failure) so it never dies.
            logger.exception("state-checkpoint tick failed; loop continues")
        try:
            # No-ops instantly on a Postgres-state instance, when no
            # system.duckdb singleton is open yet, or when the rolling
            # snapshot is still within its configured cadence — the
            # EXPORT DATABASE cost only actually runs on the (rare) stale tick.
            await to_thread_drain_on_cancel(refresh_rolling_snapshot)
        except Exception:
            logger.exception("rolling-snapshot refresh tick failed; loop continues")
        try:
            # Belt-and-braces piggyback (wave 2C task 6): re-apply every
            # env_overlay/* vault row to os.environ on every tick. Covers a
            # replica that missed the env-overlay-changed pub/sub event (e.g.
            # it wasn't subscribed yet, or a Redis FLUSHALL dropped it) — see
            # app.secrets.persist_overlay_token's FLUSHALL note. Cheap:
            # no-ops instantly when the vault isn't configured, otherwise one
            # small indexed table scan plus a handful of decrypts.
            await to_thread_drain_on_cancel(reapply_all_overlay_tokens_from_vault)
        except Exception:
            logger.exception("vault overlay periodic re-read failed; loop continues")


def _on_cache_invalidate(message: str) -> None:
    """Coordination-backend subscriber for the ``cache-invalidate`` channel
    (wave 2C) — drops THIS process's local v2 catalog/schema/sample TTL
    caches to mirror a table-registry mutation handled by (usually) another
    api-serving replica.

    ``message`` is ``json.dumps({"scope": "table"|"all", "table": <id or
    None>})`` — see ``app.api.v2_catalog._publish_cache_invalidate``. Routes
    into ``v2_catalog.invalidate_for_table`` / ``invalidate_all`` with
    ``_publish=False`` so reacting to an incoming event never re-publishes
    it — no echo loop back onto the channel (the process that originated
    the invalidation already cleared its own caches before publishing).
    """
    import json

    try:
        payload = json.loads(message)
    except (ValueError, TypeError):
        logger.warning("cache-invalidate: unparseable message %r", message)
        return

    from app.api import v2_catalog

    scope = payload.get("scope")
    if scope == "all":
        v2_catalog.invalidate_all(_publish=False)
    elif scope == "table":
        table = payload.get("table")
        if table:
            v2_catalog.invalidate_for_table(table, _publish=False)
    else:
        logger.warning("cache-invalidate: unknown scope %r", scope)


def _on_env_overlay_changed(env_name: str) -> None:
    """Coordination-backend subscriber for the ``env-overlay-changed``
    channel (wave 2C task 6) — re-reads ``env_name``'s current value from
    the control-plane vault and re-applies it to THIS process's
    ``os.environ``, so an admin rotating a marketplace PAT / chat-sandbox
    key on one api-serving replica propagates to every other
    api/worker/gateway replica without a restart.

    ``message`` is the bare env var name (see
    ``app.secrets.persist_overlay_token``'s vault-write path) — unlike
    ``cache-invalidate`` there's no JSON envelope to parse. Log-and-continue:
    a lookup failure here (vault key rotated mid-flight, transient DB
    hiccup) must not crash the subscriber dispatch loop — the periodic
    belt-and-braces sweep in ``_state_checkpoint_loop`` will retry.
    """
    from app.secrets import reapply_overlay_token_from_vault

    try:
        reapply_overlay_token_from_vault(env_name)
    except Exception:
        logger.exception("env-overlay-changed handler failed for %s (non-fatal)", env_name)


def _register_ducklake_readyz_check() -> None:
    """Register the DuckLake ``/readyz`` check (``app.api.health_probes``)
    UNCONDITIONALLY when the analytics backend is ``ducklake`` — i.e.
    independent of whether the best-effort reader warm-up in the lifespan
    (right after this call) succeeds.

    Why unconditional (wave-2G Task 5 review carry-over, finding 4): this
    registration used to happen only *after* the warm-up call
    (``get_ducklake_read().close()``) had already succeeded, inside the
    same ``try`` block. The single most likely failure window — the
    Postgres catalog not yet reachable at boot (e.g. a rolling deploy
    racing the catalog's own restart) — hit that block's ``except``
    branch, which swallowed the warm-up failure AND skipped registration
    in the same step. The result: a replica whose DuckLake attach is
    actually broken reported ``/readyz`` as fully ready (no ``"ducklake"``
    entry ever appeared in ``failed_checks`` because the check was never
    registered at all), so the load balancer kept routing traffic to
    it — exactly the failure this check exists to surface. Registering
    here, before any warm-up attempt, means a later periodic ``/readyz``
    poll still catches a catalog that was unreachable at boot (and
    recovers if it comes back, or correctly keeps the replica out of
    rotation if it never does).

    The check function itself is a cheap liveness probe: it calls
    :func:`src.ducklake_session.get_ducklake_read` directly (which opens
    the singleton lazily if the warm-up below never ran or failed) and
    issues ``SELECT 1`` on the returned cursor — safe to call whether or
    not the session is already warm.
    """
    from src.analytics_backend import analytics_backend

    if analytics_backend() != "ducklake":
        return

    def _ducklake_readyz_check() -> bool:
        from src.ducklake_session import get_ducklake_read

        try:
            cur = get_ducklake_read()
        except Exception:
            logger.warning("ducklake readyz check: get_ducklake_read() failed", exc_info=True)
            return False
        try:
            cur.execute("SELECT 1").fetchone()
            return True
        except Exception:
            logger.warning("ducklake readyz check: SELECT 1 probe failed", exc_info=True)
            return False
        finally:
            try:
                cur.close()
            except Exception:
                pass

    from app.api.health_probes import register_readiness_check

    register_readiness_check("ducklake", _ducklake_readyz_check)


@asynccontextmanager
async def lifespan(app):
    # Refuse to boot an unsafe multi-process topology (role split or
    # UVICORN_WORKERS>1) before any DB/backend is touched — spec §3.2.
    # No-op in default all-in-one, single-worker mode.
    from app.startup_guards import validate_deployment

    validate_deployment()

    # Surface an unsafe/no-op data-apps posture at startup: enabled, but
    # same-origin serving off and no isolated origin configured, so no hosted
    # app can actually be served (see data_apps_proxy._same_origin_serving_refused).
    from app.api.data_apps import same_origin_serving_warning

    _same_origin_msg = same_origin_serving_warning()
    if _same_origin_msg:
        logger.error("%s", _same_origin_msg)

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

    # Sweep DuckDB spill files orphaned by a previous hard death (SIGKILL,
    # crash, container stop timeout) — DuckDB never cleans these up itself
    # and they accumulate as multi-GB dead weight. Safe here: this process
    # is the only DuckDB writer for the state dir and no connection exists
    # yet. Fail-soft — a cleanup hiccup must never block startup.
    try:
        from src.db import cleanup_orphaned_temp_files

        cleanup_orphaned_temp_files()
    except Exception:
        logger.exception("duckdb-tmp orphan sweep failed (non-fatal)")

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

    # Validate auth.providers at boot so an all-unknown value (a typo in
    # instance.yaml / AGNES_AUTH_PROVIDERS) surfaces its error in the startup
    # log, not only lazily on the first /auth request. configured_allowlist()
    # logs the error itself and fails open (all providers) so a bad value can
    # never lock the instance out; the admin API rejects the same value at
    # set-time. Fail-soft — a config-read hiccup must not block startup.
    try:
        from app.auth.provider_registry import configured_allowlist

        configured_allowlist()
    except Exception:
        pass

    # Bump anyio's default thread pool size from 40 → AGNES_THREADPOOL_SIZE
    # (default 200). FastAPI auto-runs every plain `def` route handler AND
    # every plain `def` dependency in this pool — the Tier 1 endpoints
    # converted in PR #188 (`/api/query`, `/api/v2/scan`, `/api/v2/sample`,
    # `/api/v2/schema`) all block on synchronous DuckDB / BQ-extension calls
    # inside the handler body, and the auth/RBAC dependencies that run on
    # nearly every request (`get_current_user`, `get_optional_user`,
    # `require_session_token`, `require_admin`, `require_resource_access`'s
    # inner dep, `require_broker_ticket`) block on synchronous system-DB reads
    # (Postgres via the sync SQLAlchemy engine in prod) — all would otherwise
    # serialise on the single event loop once 40 are in flight, and a slow
    # auth read would freeze every other request (→ 503 "system unavailable").
    # 200 keeps the per-process working set well under the BQ extension's
    # connection cap while leaving headroom for concurrent UI / health probes.
    try:
        import anyio.to_thread

        size = int(os.environ.get("AGNES_THREADPOOL_SIZE", "200"))
        anyio.to_thread.current_default_thread_limiter().total_tokens = size
        logger.info("anyio thread pool capacity set to %d", size)
    except Exception as e:
        logger.warning("failed to bump anyio thread pool capacity: %s", e)

    from app.roles import Role, role_enabled
    from app.api.cache_warmup import maybe_schedule_startup_warmup

    if role_enabled(Role.WORKER):
        maybe_schedule_startup_warmup()

    # DuckLake reader warm-up (wave-2G Task 5). When analytics.backend is
    # ducklake, open the long-lived reader singleton (src.ducklake_session
    # .get_ducklake_read()) here at boot rather than leaving it fully lazy —
    # so the ``ducklake`` extension INSTALL/LOAD, the catalog ATTACH (a real
    # libpq connection on a Postgres catalog), and the one-time remote-mode
    # extract-source attach all happen during startup instead of stalling
    # the very first analyst query (app/api/query.py, app/api/query_hybrid.py
    # — the two callers of ``src.db.get_analytics_db_readonly()``, which
    # dispatches to the DuckLake reader when this backend is active).
    #
    # Role scope — api AND gateway, not just api: every role process mounts
    # the exact same FastAPI app with every router registered (role gating
    # in this codebase controls which background loops run, not which HTTP
    # routes exist — see the ``include_router`` calls below, none of which
    # are role-conditional), so a gateway-role replica can still serve a
    # query request in principle (e.g. a future in-process agent tool call,
    # or an operator hitting its internal port directly). Warming there too
    # is cheap (one extra ATTACH at boot) and removes that latent
    # cold-start gap; there is no reason to reserve the warm-up for api
    # only. The worker role deliberately does NOT warm the writer here —
    # ``get_ducklake_write()`` opens lazily on the worker's first rebuild
    # (``src.orchestrator``) or ``ducklake-maintenance`` job run
    # (``app/worker/kinds.py``), matching the existing lazy-open contract
    # for both DuckLake singletons.
    #
    # Fail-soft: a warm-up hiccup (e.g. the catalog is briefly unreachable)
    # must never block startup — the same reader opens lazily, exactly as
    # today, on the first real request that needs it.
    if role_enabled(Role.API) or role_enabled(Role.GATEWAY):
        # Registered UNCONDITIONALLY, before the warm-up attempt below —
        # see _register_ducklake_readyz_check's docstring for why this
        # must not live inside the warm-up's own try/except (finding 4:
        # a catalog unreachable at boot must still surface via periodic
        # /readyz polls, the m-tier smoke harness
        # (scripts/dev/mtier-smoke.sh) asserts on this). Its own
        # analytics_backend() resolution is wrapped here too, so a
        # config-resolution error can't block startup either.
        try:
            _register_ducklake_readyz_check()
        except Exception:
            logger.exception("ducklake readyz-check registration failed at startup (non-fatal)")

        # Community extensions that `query_mode='remote'` rows need must be on
        # disk before the first query: the query path LOADs without INSTALL (so
        # a read-only query never reaches the network), and DuckDB's extension
        # directory does not survive a container recreate. Without this, every
        # restart left remote rows answering `Catalog "<alias>" does not exist`
        # until someone re-saved the registration by hand — the ATTACH is
        # skipped silently, so nothing in the response said why.
        try:
            from src.remote_extension_prewarm import prewarm_from_env

            _prewarm = prewarm_from_env()
            if _prewarm["installed"] or _prewarm["failed"] or _prewarm["refused"]:
                logger.info(
                    "remote-attach extension prewarm: installed=%s failed=%s refused=%s",
                    _prewarm["installed"],
                    _prewarm["failed"],
                    _prewarm["refused"],
                )
        except Exception:
            logger.exception(
                "remote-attach extension prewarm failed at startup (non-fatal; remote "
                "rows may answer 'Catalog does not exist' until their extension installs)"
            )

        try:
            from src.analytics_backend import analytics_backend

            if analytics_backend() == "ducklake":
                from src.ducklake_session import get_ducklake_read

                get_ducklake_read().close()  # closes the cursor only; the underlying attach stays open
                logger.info("DuckLake reader session warmed at startup")
        except Exception:
            logger.exception(
                "DuckLake reader warm-up failed at startup (non-fatal; opens lazily on first query, "
                "surfaced via the /readyz check registered above)"
            )

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
        from connectors.internal.registry import ensure_internal_tables_registered

        ensure_internal_tables_registered()
    except Exception:
        logger.exception("internal data-source seed failed; continuing")

    # Subscribe this process to the coordination backend's cache-invalidate
    # channel (wave 2C) — v2 catalog/schema/sample TTL caches are process-
    # local, so a registry mutation handled by ONE api-serving replica must
    # tell every other replica to drop its own copies. Unconditional (not
    # role-gated): the /api/v2/* routers that own these caches are mounted
    # in every role combination, not just Role.API. Memory backend: this is
    # a same-process, in-memory subscriber list — harmless, and behaves
    # exactly like today's single-process-only invalidation. FLUSHALL story:
    # not applicable to pub/sub (nothing to lose but in-flight messages);
    # a message dropped mid-flight just means a stale cache serves until its
    # own TTL expires, same as if this feature didn't exist.
    try:
        from app.coordination.factory import coordination

        app.state.cache_invalidate_unsubscribe = coordination().subscribe("cache-invalidate", _on_cache_invalidate)
    except Exception:
        logger.exception("cache-invalidate subscribe failed (non-fatal)")
        app.state.cache_invalidate_unsubscribe = None

    # Subscribe this process to the coordination backend's env-overlay-changed
    # channel (wave 2C task 6) — see app.secrets.persist_overlay_token and
    # _on_env_overlay_changed above. Unconditional/non-role-gated for the same
    # reason as cache-invalidate above: every role combination (api/worker/
    # gateway/all) reads these env vars (ANTHROPIC_API_KEY, marketplace PATs,
    # ...) somewhere. Harmless when the vault isn't configured (keyless
    # S-tier) — this channel is simply never published to in that mode.
    try:
        from app.coordination.factory import coordination

        app.state.env_overlay_unsubscribe = coordination().subscribe("env-overlay-changed", _on_env_overlay_changed)
    except Exception:
        logger.exception("env-overlay-changed subscribe failed (non-fatal)")
        app.state.env_overlay_unsubscribe = None

    # Baked-data images (no scheduler) need master views built at boot.
    if role_enabled(Role.WORKER):
        _maybe_rebuild_on_boot()

    # Rebuild the FTS BM25 indexes over knowledge_items and glossary_terms at
    # boot (issue #121; glossary_terms joined in for #1294 — a rolling/WAL
    # recovery restore lands a plain system.duckdb with no fts_main_* schemas
    # until something rebuilds them, and this is the only unconditional
    # safety net for that: knowledge_items/glossary_terms mutations rebuild
    # their own index on write, but a table that hasn't been touched since
    # restore would otherwise stay on the ILIKE fallback indefinitely). The
    # migration to schema v47 already does this on first upgrade, but for
    # instances that have been on v47 across restarts the boot-time rebuild
    # guarantees the index reflects whatever mutations landed via the
    # BG-task / scheduler paths that bypass the per-mutation hook.
    # Soft-failure — logs WARNING and the repo falls back to ILIKE.
    #
    # DuckDB-only: the BM25 index is a DuckDB FTS-extension artefact built on
    # the system DuckDB. On Postgres there is no system DuckDB (and opening one
    # is forbidden), so skip entirely — memory search there uses the PG path.
    from src.repositories import use_pg as _use_pg

    if not _use_pg():
        try:
            from src.db import get_system_db
            from src.fts import ensure_glossary_fts_index, ensure_knowledge_fts_index

            _fts_conn = get_system_db()
            ensure_knowledge_fts_index(_fts_conn)
            ensure_glossary_fts_index(_fts_conn)
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

    # Microsoft Entra ID: a tenant that fails the single-tenant check leaves
    # the provider unavailable (the login button silently disappears), and an
    # available provider without auth.allowed_domain has no identity boundary
    # against the tenant's B2B guests. Both must reach the boot log.
    try:
        from app.auth.providers.microsoft import startup_warnings as microsoft_startup_warnings

        for warning in microsoft_startup_warnings():
            logger.warning("Microsoft auth check: %s", warning)
    except Exception:
        logger.exception("Microsoft auth startup check crashed (non-fatal)")

    # Google: unlike Microsoft, there is no tenant to serve as even a partial
    # identity boundary — an enabled provider without auth.allowed_domain
    # means ANY Google account can sign in and self-provision. Same wiring as
    # the Microsoft check above (RBAC review on PR #1569).
    try:
        from app.auth.providers.google import startup_warnings as google_startup_warnings

        for warning in google_startup_warnings():
            logger.warning("Google auth check: %s", warning)
    except Exception:
        logger.exception("Google auth startup check crashed (non-fatal)")

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

    from src.db_pg import seed_lease

    with seed_lease():
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

        # Seed the chat resource grant for Everyone on first boot. Chat
        # visibility is gated on an EXPLICIT grant (app.web.router::
        # _compute_can_chat uses has_explicit_grant, deliberately NOT
        # can_access — admin god-mode does not reveal chat). A fresh
        # instance with chat.enabled: true and no grant ships with a fully
        # working chat backend that nobody, including admins, can see
        # without hand-typing /chat. See app/chat/grant_seed.py for the
        # first-boot-vs-revoked reasoning.
        try:
            from app.chat.config import load_chat_config
            from app.chat.grant_seed import seed_everyone_chat_grant
            from app.secrets import _state_dir as _seed_chat_state_dir

            _chat_cfg_early = load_chat_config(_seed_chat_state_dir() / "instance.yaml")
            if seed_everyone_chat_grant(chat_enabled=_chat_cfg_early.enabled):
                logger.info("Seeded chat resource grant for Everyone (fresh instance, chat.enabled=true)")
        except Exception as e:
            logger.warning("Could not seed chat resource grant: %s", e)

        # Seed the six canonical memory domains into the ACTIVE state backend.
        # On DuckDB the schema ladder already seeds them (fresh-install branch /
        # _v51_to_v52), so ensure_seed no-ops; on Postgres nothing else does —
        # Alembic creates the table empty. ensure_seed never touches an existing
        # row (a soft-deleted row still holds its slug), so admin renames and
        # deletions are not overwritten or resurrected on reboot.
        try:
            from src.db import _CANONICAL_MEMORY_DOMAINS_SEED

            _md_repo = memory_domains_repo()
            for _md_id, _md_slug, _md_name, _md_icon, _md_color in _CANONICAL_MEMORY_DOMAINS_SEED:
                _md_repo.ensure_seed(
                    domain_id=_md_id,
                    slug=_md_slug,
                    name=_md_name,
                    icon=_md_icon,
                    color=_md_color,
                )
        except Exception as e:
            logger.warning("Could not seed canonical memory domains: %s", e)

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

        from src.user_identity import normalize_email

        # Normalized on the way in: this is an account-CREATING path, and a
        # mixed-case SEED_ADMIN_EMAIL over an existing normalized row used to
        # mint a second account and put Admin/Everyone on the copy the person
        # never signs in as (every auth door resolves the OLDEST match).
        seed_email = normalize_email(
            os.environ.get("SEED_ADMIN_EMAIL") or (get_local_dev_email() if is_local_dev_mode() else None) or ""
        )
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
                existing = repo.get_by_email_ci(seed_email)
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
                # Loud on purpose (issue: 26h burned on a customer deploy
                # diagnosing a silent seed failure). "Bootstrap the admin
                # user" is step 6/9 in docs/ONBOARDING.md — a failed seed
                # means the fresh instance has NO working way in, and that
                # used to be visible only by reading container logs on the
                # VM. ERROR + exc_info puts the traceback in the boot log;
                # the audit_log row makes it durable and queryable from
                # /admin/activity (action=startup.seed_admin_failed) even
                # by an operator who only found the instance later. Still
                # Exception (never BaseException) — a hard crash here would
                # take down an instance that may still be reachable by other
                # means (existing OAuth users, an already-provisioned admin).
                logger.error("Seed admin failed for %s: %s", seed_email, e, exc_info=True)
                try:
                    from src.repositories import audit_repo

                    audit_repo().log(
                        user_id=None,
                        action="startup.seed_admin_failed",
                        resource=seed_email,
                        result="error",
                        params={"error": str(e)},
                    )
                except Exception:
                    # A second failure here must never mask the ERROR
                    # already logged above, nor crash startup.
                    logger.debug("Could not record seed-admin failure to audit_log", exc_info=True)

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
                # ensure_scheduler_user routes its reads/writes through the
                # repository factory (honors use_pg()) and ignores ``conn``, so
                # on Postgres pass None — opening the system DuckDB there would
                # create a stale system.duckdb (forbidden invariant).
                from src.repositories import use_pg

                conn = None if use_pg() else get_system_db()
                try:
                    ensure_scheduler_user(conn)
                finally:
                    if conn is not None:
                        conn.close()
        except Exception as e:
            logger.warning(f"Could not seed scheduler user: {e}")

    # C8: Warn when no user has a password_hash — bootstrap endpoint is open.
    # This is intentional UX (operator can claim seed admin), but the open
    # window should be visible in startup logs so it's not forgotten.
    if not is_local_dev_mode():
        try:
            from src.db import get_system_db
            from src.repositories import use_pg

            # users_repo() is factory-routed and ignores ``conn``; on Postgres
            # pass None so the system DuckDB is never opened (forbidden).
            conn = None if use_pg() else get_system_db()
            try:
                from app.auth.scheduler_token import SCHEDULER_USER_EMAIL
                from src.db import SYSTEM_ADMIN_GROUP

                admin_group = user_groups_repo().get_by_name(SYSTEM_ADMIN_GROUP)
                admin_members = (
                    user_group_members_repo().list_members_for_group(admin_group["id"]) if admin_group else []
                )
                # Exclude the synthetic scheduler service user — mirrors the
                # /auth/bootstrap lock so this warning agrees with actual
                # reachability (the scheduler user is auto-added to Admin but
                # doesn't count as a human admin).
                admin_exists = any(m.get("email") != SCHEDULER_USER_EMAIL for m in admin_members)
                has_password = any(u.get("password_hash") for u in users_repo().list_all())
                # /auth/bootstrap is reachable UNAUTHENTICATED only until an admin
                # exists (or a password-holding user does); after that it is locked
                # unless AGNES_BOOTSTRAP_TOKEN is presented. Surface the open window.
                if not admin_exists and not has_password:
                    logger.warning(
                        "No admin exists yet — /auth/bootstrap is reachable UNAUTHENTICATED. "
                        "Provision the first admin (SEED_ADMIN_EMAIL / SEED_ADMIN_PASSWORD, or a "
                        "one-time bootstrap) before exposing the URL; set AGNES_BOOTSTRAP_TOKEN to "
                        "gate later re-bootstraps."
                    )
            finally:
                if conn is not None:
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
        from src.repositories import use_pg as _use_pg_chat
        from app.chat.config import load_chat_config
        from app.chat.persistence import ChatRepository

        _chat_data_dir = _get_data_dir_chat()
        # ChatRepository delegates to the *_pg repositories under use_pg() and
        # leaves ``conn`` unused there; on Postgres pass None so the system
        # DuckDB is never opened (forbidden invariant).
        _chat_conn = None if _use_pg_chat() else _get_system_db_chat()
        app.state.chat_repo = ChatRepository(_chat_conn)
        app.state.chat_data_dir = _chat_data_dir

        # Overlay location must honor STATE_DIR: the admin overlay WRITER
        # (app/api/admin.py) and load_instance_config both resolve via
        # app.secrets._state_dir(), so a flat-mount deployment (STATE_DIR
        # outside DATA_DIR/state) would otherwise toggle chat in a file this
        # bootstrap never reads (Devin review on #1076).
        from app.secrets import _state_dir as _chat_state_dir

        _chat_instance_yaml = _chat_state_dir() / "instance.yaml"
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
                from src.repositories import use_pg
                from src.initial_workspace import build_zip

                # On Postgres pass conn=None — build_zip resolves the admin
                # workspace-prompt overlay through the repository factory when
                # use_pg() is true (opening the system DuckDB is forbidden).
                conn = None if use_pg() else get_system_db()
                try:
                    return build_zip(conn)
                finally:
                    if conn is not None:
                        conn.close()
            except Exception:
                logger.exception("_fetch_local_template_zip failed (non-fatal)")
                return b""

        if app.state.chat_config.enabled:
            if not role_enabled(Role.GATEWAY):
                logger.info("chat: disabled in this process (role split; gateway role owns chat)")
                app.state.chat_manager = None
            elif app.state.chat_config.provider not in ("docker", "kai-agent"):
                if app.state.chat_config.provider == "e2b":
                    logger.error(
                        "chat.provider=e2b is no longer supported — the E2B "
                        "provider was removed in 0.89.0. Set chat.provider in "
                        "instance.yaml (or AGNES_CHAT_PROVIDER / the "
                        "customer-instance module's chat_provider field) to "
                        "'kai-agent' (the embedded turn engine) or 'docker' "
                        "(self-hosted containers via the apps-runner sidecar; "
                        "see docs/cloud-chat.md), then restart. Chat stays "
                        "disabled until then.",
                    )
                else:
                    logger.error(
                        "chat.provider=%r is not supported — the accepted values are "
                        "'docker' (self-hosted containers via the apps-runner "
                        "sidecar) and 'kai-agent' (the embedded kai-agent turn "
                        "engine; see docs/cloud-chat.md). There is deliberately "
                        "no mock provider. Set chat.provider in instance.yaml "
                        "to one of those, or flip chat.enabled: false.",
                        app.state.chat_config.provider,
                    )
                app.state.chat_manager = None
            elif int(os.environ.get("UVICORN_WORKERS", "1")) > 1 and _chat_coordination_backend() != "redis":
                # Multi-worker/multi-replica chat needs its state (tickets,
                # session-routing leases + takeover, frame replay, inbound
                # command streams, notifications) shared across processes —
                # only the redis coordination backend provides that (and
                # app.startup_guards.validate_deployment already refuses to
                # boot that combo without Postgres app-state + explicit
                # secrets, so reaching here with backend=="redis" means the
                # rest of the multi-process contract is already satisfied).
                # The default ``memory`` backend keeps process-local state,
                # so a second worker would silently miss tickets/leases
                # owned by its sibling — same unsafe posture as before.
                logger.error(
                    "chat.enabled=true but UVICORN_WORKERS > 1 and "
                    "coordination.backend != 'redis' — multi-worker/replica "
                    "cloud chat requires the redis coordination backend; "
                    "chat_manager disabled"
                )
                app.state.chat_manager = None
            elif not _chat_jwt_secret_ok(app.state.chat_config):
                # Fatal already logged inside the helper.  Disable chat so the
                # runner never spawns with a public-constant secret.
                app.state.chat_manager = None
            elif not _chat_llm_provider_ok(app.state.chat_config):
                # Fatal already logged inside the helper. Ordered BEFORE the
                # anthropic-key gate so a vertex misconfiguration reports its
                # own cause, not a misleading missing-key message.
                app.state.chat_manager = None
            elif not _chat_anthropic_key_ok(app.state.chat_config):
                # Fatal already logged inside the helper.  No key → no runner.
                logger.error(
                    "ANTHROPIC_API_KEY missing; disabling chat",
                )
                app.state.chat_manager = None
            elif not _chat_kai_agent_ok(app.state.chat_config):
                # Fatal already logged inside the helper.
                app.state.chat_manager = None
            elif not _chat_harness_ok(app.state.chat_config):
                # Fatal already logged inside the helper.
                app.state.chat_manager = None
            elif not _chat_docker_rails_url_ok(app.state.chat_config):
                # Fatal already logged inside the helper.
                app.state.chat_manager = None
            # Last of the gates: the only one that does network I/O.
            elif not await _chat_docker_sandbox_ok(app.state.chat_config):
                # Fatal already logged inside the helper.
                app.state.chat_manager = None
            else:
                from typing import Optional
                from app.chat.workdir import WorkdirManager
                from app.chat.manager import ChatManager, agnes_server_url
                from app.version import APP_VERSION as _APP_VERSION_CHAT

                # Same fallback chain as the sandbox env (AGNES_SERVER in
                # manager.py): SERVER_URL → AGNES_INTERNAL_URL → loopback.
                # Plain-HTTP deployments that keep SERVER_URL unset get their
                # workspace seed pointed at the same rails URL the CLI uses.
                _server_url = agnes_server_url()

                def _render_workspace_prompt(user_email: str) -> Optional[str]:
                    """Render the analyst CLAUDE.md for the chat sandbox.

                    Delegates so the embedded `kai-agent` turn engine, which
                    ships the same prompt inside its workspace tarball
                    (`app/api/kai.py`), cannot drift from what the native
                    sandbox seeds. Returns None on any failure so workdir init
                    falls back to the bundled static CLAUDE.md."""
                    from app.chat.workspace_prompt import render_sandbox_workspace_prompt
                    from src.db import get_system_db
                    from src.repositories import use_pg

                    # Conn resolution stays HERE rather than moving into the
                    # shared helper: the helper opens no connection of its own,
                    # so it needs no `get_system_db()` grandfather entry, and
                    # this path keeps the exact behaviour it had — the
                    # DuckDB-mode conn is handed in, and on Postgres it is None
                    # so the system DuckDB is never opened (forbidden
                    # invariant). Devin review on this PR.
                    conn = None if use_pg() else get_system_db()
                    try:
                        return render_sandbox_workspace_prompt(user_email, server_url=_server_url, conn=conn)
                    finally:
                        if conn is not None:
                            conn.close()

                def _export_marketplace(user_email: str, dest: Path) -> "list[str]":
                    """Write the user's RBAC-filtered marketplace tree at `dest`.

                    Returns the plugin names written — the `<name>@agnes` refs
                    the sandbox installs offline. The content comes from the same
                    builder the served marketplace ZIP uses, so a chat sandbox
                    and an analyst's laptop get byte-identical plugins.

                    Returns [] when nothing installs from a tree: the operator
                    has `chat.bootstrap_marketplace` off (the composer's menu
                    omits the plugins too, so the two agree), the provider
                    delivers flattened components instead (`kai-agent` — it gets
                    them from the workspace tarball, so exporting a tree here
                    would copy the whole marketplace per convergence for nothing),
                    or the user row is gone. In those cases any tree a previous
                    provider left behind is removed, and the [] return also
                    prunes the `@agnes` enabledPlugins entries — there are no
                    installed plugins to enable. Found by Devin Review on #1552.

                    Conn resolution mirrors `_render_workspace_prompt` above:
                    handed in under DuckDB, None on Postgres (where opening the
                    system DuckDB is a forbidden invariant) — the resolver reads
                    its state through the repo factory either way.
                    """
                    import shutil

                    from app.chat.marketplace_payload import export_marketplace_tree
                    from app.chat.skills_catalog import DELIVERY_PLUGIN, marketplace_delivery
                    from src.db import get_system_db
                    from src.repositories import use_pg, users_repo

                    if marketplace_delivery(app.state.chat_config) != DELIVERY_PLUGIN:
                        shutil.rmtree(dest, ignore_errors=True)
                        return []
                    user = users_repo().get_by_email(user_email)
                    if user is None:
                        shutil.rmtree(dest, ignore_errors=True)
                        return []
                    conn = None if use_pg() else get_system_db()
                    try:
                        return export_marketplace_tree(conn, dict(user), dest)
                    finally:
                        if conn is not None:
                            conn.close()

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
                    export_marketplace=_export_marketplace,
                    marketplace_sha_debounce_seconds=app.state.chat_config.marketplace_sha_debounce_seconds,
                )
                if app.state.chat_config.provider == "docker":
                    from app.chat.docker_provider import DockerSandboxProvider

                    if _chat_coordination_backend() == "redis":
                        # Not a refusal: single-host role-split (api/gateway/
                        # worker on one daemon) is supported and looks the
                        # same from here. What docs/cloud-chat.md rules out is
                        # multi-HOST gateways — each host's daemon only sees
                        # its own containers, so cross-gateway takeover cannot
                        # destroy the other host's sandbox. Boot can't tell
                        # the two apart; say it loudly and continue.
                        logging.getLogger("app.main").warning(
                            "provider=docker with coordination.backend=redis: "
                            "multi-HOST gateways are unsupported with the docker "
                            "provider (each host's daemon only sees its own "
                            "containers; cross-gateway takeover cannot reach the "
                            "other host's sandbox — see docs/cloud-chat.md "
                            "Limitations). Single-host role-split is fine.",
                        )
                    # No lifetime clamp: a local container has no platform cap,
                    # so chat.max_session_seconds (default 4 h) applies as
                    # configured and the idle/lifetime reapers enforce it.
                    provider = DockerSandboxProvider(
                        image=app.state.chat_config.docker_image,
                        network=app.state.chat_config.docker_network,
                        mem_limit=app.state.chat_config.docker_mem_limit,
                        cpus=app.state.chat_config.docker_cpus,
                        pids_limit=app.state.chat_config.docker_pids_limit,
                        egress_mode=app.state.chat_config.docker_egress_mode,
                        egress_proxy_url=app.state.chat_config.docker_egress_proxy_url,
                        max_total_sandboxes=app.state.chat_config.docker_max_total_sandboxes,
                    )
                    # Allowlist mode fails silently and totally when the app's
                    # config and the compose-owned proxy sidecar disagree, so
                    # say which knob is wrong at startup instead of leaving an
                    # operator to work back from "no egress at all".
                    from app.chat.config import egress_compose_mismatches

                    for _mismatch in egress_compose_mismatches(app.state.chat_config):
                        logger.warning("chat egress: %s", _mismatch)
                else:  # kai-agent — the allowlist above guarantees membership
                    from app.chat.kai_engine_provider import KaiEngineProvider

                    # Sessions run on the embedded kai-agent turn engine: the
                    # engine owns the agent loop, the transcript store and the
                    # remote sandbox, so there is nothing to spawn locally —
                    # the provider's handles translate the engine's SSE stream
                    # into the runner frame protocol. Gated above on
                    # KAI_HOST_JWT_SECRET (_chat_kai_agent_ok).
                    provider = KaiEngineProvider(base_url=app.state.chat_config.kai_agent_url)
                    # Two cost caps read chat_messages.tokens_in/out, which only
                    # a usage-carrying frame writes; the engine's stream carries
                    # none. Both ship LIVE defaults ($20/day, 200k/session), so
                    # this provider silently removes two budgets instance-wide.
                    # Say so at boot rather than let it surface as a bill —
                    # `/api/chat/readiness` reports the same list as
                    # `unmetered_caps` so /admin can show it too.
                    for _cap in ("daily_anthropic_spend_usd", "max_session_tokens"):
                        if getattr(app.state.chat_config, _cap, None):
                            logger.warning(
                                "chat provider 'kai-agent': %s is configured but NOT enforced — the engine "
                                "stream carries no token usage, so nothing accrues against it. Cap engine "
                                "spend per agent with token_budget_monthly instead.",
                                _cap,
                            )
                mgr = ChatManager(
                    provider=provider,
                    workdir_mgr=workdir_mgr,
                    repo=app.state.chat_repo,
                    config=app.state.chat_config,
                )
                mgr.start_idle_reaper()
                app.state.chat_manager = mgr
                from app.roles import is_all_in_one as _chat_is_all_in_one

                _chat_topology = (
                    "single-process"
                    if int(os.environ.get("UVICORN_WORKERS", "1")) <= 1 and _chat_is_all_in_one()
                    else "multi-worker/replica (coordination.backend=redis)"
                )
                if app.state.chat_config.provider == "docker":
                    _chat_sandbox_desc = (
                        f"image={app.state.chat_config.docker_image}, egress={app.state.chat_config.docker_egress_mode}"
                    )
                else:
                    _chat_sandbox_desc = f"engine={app.state.chat_config.kai_agent_url}"
                logger.info(
                    "chat.enabled: ChatManager started (provider=%s, "
                    "%s, idle_ttl=%ds, concurrency_per_user=%d, "
                    "topology=%s)",
                    app.state.chat_config.provider,
                    _chat_sandbox_desc,
                    app.state.chat_config.idle_ttl_seconds,
                    app.state.chat_config.concurrency_per_user,
                    _chat_topology,
                )
        else:
            app.state.chat_manager = None
            logger.info("chat.enabled=false; ChatManager not started")
    except Exception:
        logger.exception("CHAT-INIT failed (non-fatal); chat features will be unavailable")
        app.state.chat_manager = None
    # Mirror whatever CHAT-INIT settled on (a real manager, or None) onto the
    # process-wide singleton the `agent_response` job-worker handler reads —
    # it has no `Request`/`app` in scope to read `app.state.chat_manager`
    # from directly (see app.chat.manager.get_current_chat_manager).
    from app.chat.manager import set_current_chat_manager

    set_current_chat_manager(app.state.chat_manager)
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

    # Run the streamable MCP session manager for the app's lifetime. Starlette
    # does not run a mounted sub-app's lifespan, so the streamable OAuth MCP
    # endpoint would otherwise raise "Task group is not initialized".
    from app.api.mcp_streamable import streamable_session_manager_lifespan

    # Periodic system.duckdb CHECKPOINT (#710) — see _state_checkpoint_loop.
    # Started here (worker-only), not create_app(): the uvicorn --reload
    # master must not touch system.duckdb (same reasoning as the seeding
    # above). This applies only when the system DuckDB singleton is actually
    # open, i.e. on DuckDB-state instances: checkpoint_system_db() never opens
    # one implicitly (it no-ops when no singleton is held). Postgres-state
    # instances must NOT open the system DuckDB — get_system_db() raises under
    # use_pg(), the startup FTS rebuild is skipped there, and every remaining
    # opener routes through the repository factory — so on Postgres the
    # checkpoint_system_db() arm no-ops, while the same loop still folds the
    # operational.duckdb WAL (checkpoint_operational_db) once a CLI login /
    # Slack bind has opened it.
    _checkpoint_interval = _state_checkpoint_interval_s()
    _checkpoint_task = None
    if _checkpoint_interval > 0:
        _checkpoint_task = asyncio.create_task(_state_checkpoint_loop(_checkpoint_interval), name="state-checkpoint")
        logger.info("Periodic state-DB CHECKPOINT every %.0fs", _checkpoint_interval)
    else:
        logger.info("Periodic state-DB CHECKPOINT disabled (AGNES_STATE_CHECKPOINT_INTERVAL_S=0)")

    # Background write-canary for /readyz — see app.api.health_probes. Same
    # placement/lifecycle as the checkpoint task above: started here (in the
    # uvicorn worker process), not create_app() (the --reload master must not
    # touch the DB). "Worker" means the uvicorn worker process, not
    # `Role.WORKER` — this task is intentionally NOT role-gated. Every
    # replica (api/gateway/worker) serves /readyz and must self-report its
    # own write-path health, so the canary runs on all of them.
    from app.api.health_probes import canary_loop

    _canary_task = asyncio.create_task(canary_loop(), name="readiness-canary")

    # Worker runtime loop (wave-2B job queue, spec §3.3) — claims and runs
    # jobs off the `jobs` table (src/repositories/jobs.py) via heavy/light
    # lanes (app/worker/runtime.py). Role-gated like the seeds/rebuild-on-boot
    # blocks above, NOT unconditional like the canary above: only a process
    # serving Role.WORKER should poll for and execute work. `all` mode (the
    # default, single-container topology) always includes Role.WORKER, so
    # this is by design still running there — enqueued work keeps executing
    # in-process within seconds, no new deployment requirement for existing
    # single-container operators. Same task-create/cancel placement as the
    # canary task above (started here, in the uvicorn worker process, not
    # create_app() — the --reload master must not touch the DB).
    from app.worker.kinds import register_all_kinds
    from app.worker.runtime import default_worker_id, worker_loop

    # Populate the process-wide JOB_KINDS registry before the loop starts
    # claiming work — a lane slot that claims a job whose kind isn't yet
    # registered fails it outright (see `_lane_slot`'s "no registered
    # handler" branch). Registration itself is cheap (dict population, no
    # I/O) and idempotent, so it runs unconditionally here, not gated by
    # `role_enabled(Role.WORKER)` below — a non-worker process importing
    # this module (e.g. a one-off script) gets a harmless no-op registry.
    register_all_kinds()

    _worker_task = None
    if role_enabled(Role.WORKER):
        _worker_task = asyncio.create_task(worker_loop(worker_id=default_worker_id()), name="worker-loop")

    async with streamable_session_manager_lifespan(app):
        yield
    # Start the shared drain budget before cancelling anything: the loops
    # cancelled below drain their in-flight DB call, and they must share one
    # bound rather than get a full one each (see health_probes._drain_budget_s).
    from app.api.health_probes import begin_shutdown

    begin_shutdown()
    try:
        if _checkpoint_task is not None:
            _checkpoint_task.cancel()
            try:
                await _checkpoint_task
            except (asyncio.CancelledError, Exception):
                pass  # shutdown path — close_system_db() below does the final CHECKPOINT
        _canary_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _canary_task
        if _worker_task is not None:
            _worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await _worker_task
        # Cancelling the lease task runs run_with_lease's own cancellation path
        # (stop() the dispatcher if held, then lease_release) — see
        # app/coordination/leases.py and _start_slack_socket_transport above.
        _socket_lease_task = getattr(app.state, "slack_socket_lease_task", None)
        if _socket_lease_task is not None:
            _socket_lease_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await _socket_lease_task
        # Unsubscribe from the cache-invalidate channel — see the subscribe call
        # earlier in this function for why it's unconditional/non-role-gated.
        _cache_invalidate_unsubscribe = getattr(app.state, "cache_invalidate_unsubscribe", None)
        if _cache_invalidate_unsubscribe is not None:
            try:
                _cache_invalidate_unsubscribe()
            except Exception:
                logger.exception("cache-invalidate unsubscribe failed (non-fatal)")
        # Unsubscribe from the env-overlay-changed channel — see the subscribe
        # call earlier in this function.
        _env_overlay_unsubscribe = getattr(app.state, "env_overlay_unsubscribe", None)
        if _env_overlay_unsubscribe is not None:
            try:
                _env_overlay_unsubscribe()
            except Exception:
                logger.exception("env-overlay-changed unsubscribe failed (non-fatal)")
        try:
            from src.observability import get_posthog

            get_posthog().shutdown()
        except Exception:
            logger.exception("PostHog shutdown failed")
        # Flush any buffered llm_usage rows (broker Task 8 — batched ledger
        # writes) BEFORE the system DB closes, so a graceful shutdown doesn't
        # drop the tail of usage the accumulator hadn't hit a size/age
        # threshold for yet.
        try:
            from app.api.broker_agent_policy import usage_accumulator

            usage_accumulator.flush()
        except Exception:
            logger.exception("llm_usage accumulator flush failed during shutdown (non-fatal)")
        # Warm stdio MCP sessions are child processes owned by keeper tasks on
        # THIS loop. Closing them here lets each anyio task group unwind and
        # terminate its subprocess while the loop is still running, instead of
        # leaving them to be reaped when this process dies.
        try:
            from connectors.mcp.session_pool import close_all as close_mcp_sessions

            await close_mcp_sessions()
        except Exception:
            logger.exception("MCP session pool close failed during shutdown (non-fatal)")
        from src.db import close_analytics_db, close_operational_db, close_system_db

        close_system_db()
        close_analytics_db()
        # operational.duckdb (CLI-auth / Slack-binding codes) is a separate
        # long-lived DuckDB singleton — CHECKPOINT + close it too so its WAL is
        # folded on graceful shutdown (on Postgres it is the only written DuckDB
        # file; the checkpoint loop folds it periodically, and this closes it
        # cleanly on the way out).
        close_operational_db()
        # DuckLake reader/writer singletons (wave-2G Task 5) — mirrors the
        # subprocess-handoff path (src.db.close_singleton_connections(), used
        # before a DB-migrator subprocess spawns) which already closes these;
        # graceful process shutdown needs the same release so an open catalog
        # ATTACH (a held libpq connection on a Postgres catalog, or an
        # exclusive file lock on a DuckDB-file catalog) doesn't linger past
        # this process's lifetime. Safe to call unconditionally: a no-op when
        # analytics.backend is legacy or no DuckLake session was ever opened.
        try:
            from src.ducklake_session import close_ducklake_sessions

            close_ducklake_sessions()
        except Exception:
            logger.exception("close_ducklake_sessions failed during shutdown (non-fatal)")

    finally:
        # Paired with begin_shutdown() in a finally: any step above can
        # raise, and leaving the budget marked spent would silently strip
        # the drain from the rest of this process's life.
        from app.api.health_probes import end_shutdown

        end_shutdown()


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
        title="AI Harness",
        description="Self-hosted AI harness: governed data access, skills marketplace, corporate memory, and agent workspaces",
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
    async def _admin_elevation(request, call_next):
        # Admin elevation consent gate (app/auth/elevation.py): stamp the
        # request-scoped paused/elevated flag from the cookie (or instance
        # default) before any authorization runs, reset after. Only ever
        # REDUCES privilege — see the module docstring.
        from app.auth.elevation import (
            ELEVATION_COOKIE,
            request_is_noninteractive,
            reset_for_request,
            resolve_from_cookie,
            set_paused_for_request,
        )

        token = set_paused_for_request(
            resolve_from_cookie(
                request.cookies.get(ELEVATION_COOKIE),
                # Non-interactive callers (Bearer CLI/PAT/service tokens, or a
                # sole X-StorageApi-Token header) have no cookie jar to
                # re-elevate with, so the instance-wide default must not apply
                # (an explicit paused cookie still would). A request carrying a
                # session cookie stays interactive even if it also sends the
                # header — see request_is_noninteractive (Devin review #1288).
                bearer_auth=request_is_noninteractive(
                    authorization=request.headers.get("authorization", ""),
                    has_session_cookie=bool(request.cookies.get("access_token")),
                    has_sapi_header=bool(request.headers.get("x-storageapi-token")),
                ),
            )
        )
        try:
            return await call_next(request)
        finally:
            reset_for_request(token)

    @app.middleware("http")
    async def _add_version_headers(request, call_next):
        response = await call_next(request)
        # /api/* only — headers are advisory to the agnes CLI; UI/docs/marketplace
        # traffic doesn't consume them.
        if request.url.path.startswith("/api/"):
            response.headers["X-Agnes-Latest-Version"] = APP_VERSION
            response.headers["X-Agnes-Min-Version"] = MIN_COMPAT_CLI_VERSION
            response.headers["X-Agnes-Accepts"] = SERVER_CAPABILITIES
        # Server-rendered HTML must not be heuristically cached by the browser.
        # The setup hero (/home, /setup, /install) bakes render-time values
        # into the markup — RBAC-filtered plugin grants, the live connector
        # manifest, the operator's instance brand/host. (The install prompt's
        # CLI step used to also bake a version-pinned `/cli/wheel/{name}` URL
        # that 404s the moment the server upgrades between render and
        # execution; it now downloads via the unversioned `/cli/download`
        # endpoint instead, which is immune to that race — but the page as a
        # whole still isn't safe to cache.) Without an explicit directive a
        # browser reuses a stale cached document across a redeploy. `no-store`
        # forces a fresh render on every load. Scoped to text/html so JSON
        # APIs and the immutable-cached static / marketplace-image assets are
        # untouched; an explicit Cache-Control set by a route still wins.
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
            # Attachment binaries (PDF/PNG/ZIP …) are already compressed;
            # same rationale as the parquet exclusion above.
            "/api/attachments/",
            "/api/mcp",  # SSE stream — do not gzip
            # Chat sandbox LLM proxy: the model completion streams back as
            # text/event-stream. GZipMiddleware buffers a StreamingResponse
            # whole to compress it, which collapses every token delta into one
            # end-of-turn burst (verified live: the in-sandbox CLI saw all SSE
            # events arrive at one timestamp). Skipping gzip here is what makes
            # the broker's stream-through (#1020) actually reach the sandbox
            # incrementally.
            "/api/broker/anthropic",  # SSE stream — do not gzip
            # Embedded kai-agent host routes, for both reasons above at once:
            # /api/kai/mcp proxies a Streamable-HTTP MCP server that may answer
            # as text/event-stream (buffering it collapses a long tool call
            # into one burst at the end), and /api/kai/workspace returns an
            # already-gzipped tarball, where re-compressing only burns CPU.
            "/api/kai",
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
    cors_origins = [
        o.strip()
        for o in os.environ.get("CORS_ORIGINS", "http://localhost:3000,http://localhost:8000").split(",")
        if o.strip()
    ]
    cors_allow_credentials = True
    # Captured HERE, from the same read the middleware is configured with:
    # the readiness handler's per-app CORS grant must agree with the
    # middleware about whether a wildcard is in force, and `create_app`
    # loads overlay env AFTER this point — a request-time env re-read could
    # see a different value than the middleware did (Devin on #1321).
    app.state.cors_has_wildcard = "*" in cors_origins
    # Data-app subdomains are deliberately NOT allowed here. The holding
    # page's readiness poll (data_app_waking.html on `<slug>.<base>`) does
    # need a credentialed cross-origin read of ONE response — but this
    # middleware is app-wide, those subdomains serve user-authored app code,
    # and the session cookie already rides to the main host from them
    # (`Domain=.<parent>`, same-site). A subdomain `allow_origin_regex`
    # paired with `allow_credentials=True` therefore let any hosted app's JS
    # read every authenticated endpoint as its viewer. The poll's allowance
    # lives route-scoped and per-app instead:
    # `app.api.data_apps._readiness_cors_headers` (Devin Review on this PR).
    if "*" in cors_origins:
        # SECURITY: Starlette's CORSMiddleware, when allow_origins contains "*"
        # AND allow_credentials=True, reflects the caller's Origin into
        # Access-Control-Allow-Origin and returns Allow-Credentials: true — an
        # any-origin-with-credentials policy (cookies / Authorization usable
        # from any website), not the browser-rejected literal "*". Refuse that
        # combination: keep the wildcard but drop credentials, so a
        # misconfigured CORS_ORIGINS can't expose authenticated endpoints
        # cross-origin. Operators who need credentialed CORS must list explicit
        # origins.
        logger.error(
            "CORS_ORIGINS contains '*': disabling allow_credentials to avoid an "
            "any-origin-with-credentials CORS policy. Set an explicit origin allowlist."
        )
        cors_allow_credentials = False
        # The wildcard also OVERWRITES per-route CORS grants: with
        # allow_all_origins the middleware stamps `Access-Control-Allow-Origin:
        # *` (credential-less) over response headers a handler set, which
        # breaks the data-app readiness poll's per-app credentialed grant
        # (app/api/data_apps.py::_readiness_cors_headers) — the subdomain
        # holding page then polls forever. Say so where the operator is
        # already being told their CORS config is wrong.
        try:
            from app.instance_config import get_data_apps_config

            if (get_data_apps_config().get("subdomain_base") or "").strip():
                logger.error(
                    "CORS_ORIGINS='*' with data_apps.subdomain_base configured: the "
                    "readiness poll on data-app subdomains needs a credentialed "
                    "per-app CORS grant, which the wildcard overrides — subdomain "
                    "holding pages will not detect the app coming up until "
                    "CORS_ORIGINS lists explicit origins."
                )
        except Exception:
            pass

    # CSRF gate for cookie-authenticated state-changing requests (F2). The
    # double-submit `web_csrf` token covers only the HTML form handlers; the
    # `/api/**` JSON surface is cookie-session authed with no token and its
    # protection was implicit (Pydantic bodies force a pre-flighted
    # `application/json`, `SameSite=Lax` drops a cross-site cookie). That misses
    # no-body mutations (`POST /api/sync/trigger`, the admin `run-*` family are
    # CORS-simple) and the sibling-sub-domain case (a data-app on `.<base>` is
    # same-site, so Lax keeps the cookie). This gate refuses a cookie-only
    # state-changing request the browser reports as cross-origin. Added BEFORE
    # CORSMiddleware so it is the INNERMOST app-wide middleware: it runs after
    # DataAppSubdomainMiddleware rewrites a sub-domain request to
    # `/apps/<slug>/...`, so its proxy-path skip matches those too. It reuses the
    # same parsed `cors_origins`, so an operator's explicit credentialed
    # cross-origin allowlist is honored in one place. The env kill switch
    # mirrors the CORS_ORIGINS / AGNES_TRUSTED_PROXY_HOPS operator-config
    # precedent (a plain env read, not a user-facing switch).
    from app.middleware.csrf_origin import CsrfOriginMiddleware

    csrf_origin_enforce = os.environ.get("AGNES_CSRF_ORIGIN_ENFORCE", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    app.add_middleware(
        CsrfOriginMiddleware,
        allowed_origins=set(cors_origins),
        enabled=csrf_origin_enforce,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=cors_allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Data-apps subdomain rewrite (Task 8): `<slug>.<subdomain_base>` host
    # requests get `scope["path"]` rewritten to `/apps/<slug>/...` before
    # routing. Pure passthrough (a no-op) unless `data_apps.subdomain_base`
    # is configured. Position among the other middlewares doesn't matter —
    # routing itself is innermost regardless of add_middleware order — but
    # it must run for both "http" and "websocket" scopes (the data-app WS
    # bridge lives at the same rewritten path), which rules out
    # http-only middlewares like CORSMiddleware/SessionMiddleware as a
    # model to imitate ordering from.
    from app.data_apps_subdomain import DataAppSubdomainMiddleware

    app.add_middleware(DataAppSubdomainMiddleware)

    # Baseline security response headers (non-breaking CSP subset,
    # X-Frame-Options, nosniff, Referrer-Policy, HSTS on https) — set at the app
    # layer so protection is independent of which TLS terminator is deployed,
    # not only the bundled Caddy. Pure ASGI (SSE-safe). See
    # app/middleware/security_headers.py.
    from app.middleware.security_headers import SecurityHeadersMiddleware

    app.add_middleware(SecurityHeadersMiddleware)

    # RequestIdMiddleware mounted LAST — Starlette inserts middleware at
    # index 0, so the last add_middleware call ends up OUTERMOST and runs
    # FIRST per request. The request_id ContextVar is set before any
    # downstream middleware or handler runs, and every response gets the
    # x-request-id header.
    app.add_middleware(RequestIdMiddleware)

    # Audit-timing contextvar (pure ASGI, zero hot-path overhead) — lets
    # audit_repo().log() auto-fill duration_ms for every HTTP-triggered
    # audit write; see src/audit_context.py.
    from app.middleware.audit_timing import AuditTimingMiddleware

    app.add_middleware(AuditTimingMiddleware)

    # HTTP request metrics (three-plane wave 2D, task 1) — registered as an
    # `@app.middleware("http")` function (not add_middleware) so it becomes
    # the true OUTERMOST layer, even later than RequestIdMiddleware above:
    # duration then captures the full per-request latency including every
    # other middleware (gzip, CORS, session, rate limiting, request-id).
    # Skips METRICS_PATH itself — scraping must never grow the series it's
    # reading. Uses the FastAPI-matched route TEMPLATE
    # (request.scope["route"].path), never the raw path, to keep label
    # cardinality bounded; falls back to UNMATCHED_PATH when no route
    # matched at all (e.g. a CORS preflight short-circuited before routing
    # ran). See app/observability/metrics.py.
    import time as _time

    from app.observability.metrics import METRICS_PATH, UNMATCHED_PATH, observe_http

    @app.middleware("http")
    async def _observe_http_metrics(request, call_next):
        if request.url.path == METRICS_PATH:
            return await call_next(request)
        start = _time.monotonic()
        try:
            response = await call_next(request)
            duration = _time.monotonic() - start
            route = request.scope.get("route")
            path_template = route.path if route is not None else UNMATCHED_PATH
            observe_http(request.method, path_template, response.status_code, duration)
            return response
        except Exception:
            duration = _time.monotonic() - start
            route = request.scope.get("route")
            path_template = route.path if route is not None else UNMATCHED_PATH
            observe_http(request.method, path_template, 500, duration)
            raise

    # Load .env_overlay (persisted by /api/admin/configure)
    from app.secrets import _state_dir

    _overlay = _state_dir() / ".env_overlay"
    if _overlay.exists():
        for line in _overlay.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                # Override, NOT setdefault: the overlay is the admin's
                # persisted runtime configuration (secrets set via
                # /api/admin/configure and the chat "configure secrets" UI,
                # e.g. ANTHROPIC_API_KEY, marketplace PATs). It
                # MUST win over an image-baked default of the same name.
                # With setdefault, a stale baked key already occupying
                # os.environ shadowed the overlay, so rotating a key via the
                # UI was silently discarded on the next restart and chat broke
                # with a 401. This matches persist_overlay_token's own
                # ``os.environ[k] = v`` write semantics — set once at the UI,
                # win consistently across restarts.
                os.environ[k.strip()] = v.strip()

    # Vault-mode overlay tokens (wave 2C task 6) — loaded AFTER the legacy
    # file above so a vault row for the same env_name wins on conflict. This
    # is the vault-mode analogue of the file load: an admin save under
    # AGNES_VAULT_KEY goes straight to the vault (see
    # app.secrets.persist_overlay_token), never touching the file, so this
    # process must also read the vault to see it. No-ops when the vault
    # isn't configured (keyless/S-tier) or has no env_overlay/* rows yet.
    try:
        from app.secrets import reapply_all_overlay_tokens_from_vault

        reapply_all_overlay_tokens_from_vault()
    except Exception:
        logger.exception("vault overlay token load failed at boot (non-fatal)")

    # Load instance config on startup
    try:
        from app.instance_config import InstanceConfigUnreadable, load_instance_config, reset_cache

        # `strict=True` + a cache drop: this is THE boot check, and it has to
        # actually read the file. Importing this module already loads the
        # config once, so without the reset the startup call returns the cache
        # and inspects nothing — which is how the refusal quietly stopped
        # existing when it was gated on a "have we booted yet" flag instead.
        reset_cache()
        load_instance_config(strict=True)
        logger.info("Instance config loaded")
    except InstanceConfigUnreadable:
        # Re-raised, unlike everything else here. The broad `except` below is
        # deliberate — a soft config problem should not stop an instance from
        # serving — but an overlay that exists and cannot be READ is not soft:
        # `database.backend` lives in it, so continuing means either running
        # against the wrong store or 500ing every `get_value()` consumer while
        # looking healthy from the outside. Refusing at boot is the whole point
        # of raising it, and swallowing it here would have made that a comment
        # rather than a behaviour.
        logger.critical("instance config overlay is unreadable — refusing to start")
        raise
    except Exception as e:
        logger.warning(f"Could not load instance config: {e}")

    # Warm the two retired-knob resolvers (`ui_layout`, `experience`) so their
    # one-time warnings land in the BOOT log, which is where an operator
    # upgrading a deployment looks and what CONFIGURATION.md /
    # instance.yaml.example promise ("a one-time startup warning").
    # `_warn_once` fires on first call; without this the first call was
    # whatever request happened to render a page first, so the warning appeared
    # minutes later, interleaved with traffic — or never, on an instance nobody
    # opened. Both resolvers must be called: each owns its own warning.
    try:
        from app.instance_config import get_experience, get_ui_layout

        get_ui_layout()
        get_experience()
    except Exception as e:  # a warning must never be able to stop a boot
        logger.debug(f"Could not warm retired-knob resolvers: {e}")

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
    from app.auth.providers.keboola import router as keboola_auth_router
    from app.auth.providers.microsoft import router as microsoft_auth_router

    # API routers
    app.include_router(auth_router)
    app.include_router(google_auth_router)
    app.include_router(password_auth_router)
    app.include_router(email_auth_router)  # Always register, check availability per-request
    app.include_router(keboola_auth_router)  # Always register, availability + allowlist per-request
    app.include_router(microsoft_auth_router)  # Always register, availability + allowlist per-request
    from app.api.keboola_login_projects import router as keboola_login_projects_router

    app.include_router(keboola_login_projects_router)  # select-mode project import (same allowlist gate)
    app.include_router(health_router)

    from app.api import health_probes

    app.include_router(health_probes.router)  # /healthz + /readyz, unauthenticated LB probes

    from app.observability.metrics import router as prometheus_metrics_router

    app.include_router(prometheus_metrics_router)  # /metrics, unauthenticated Prometheus scrape endpoint
    app.include_router(sync_router)
    app.include_router(jobs_router)
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
    app.include_router(admin_doctor_router)
    app.include_router(admin_keboola_test_router)
    app.include_router(access_router)
    app.include_router(me_access_router)
    app.include_router(me_router)
    app.include_router(me_stats_router)
    app.include_router(attachments_router)
    app.include_router(jira_webhooks_router)
    app.include_router(metrics_router)
    app.include_router(glossary_router)
    app.include_router(semantic_models_router)
    app.include_router(metadata_router)
    app.include_router(query_hybrid_router)
    app.include_router(cli_artifacts_router)
    app.include_router(cli_auth_router)
    app.include_router(tokens_router)
    app.include_router(tokens_admin_router)
    app.include_router(agents_admin_router)
    app.include_router(agent_runtime_router)
    app.include_router(agent_sessions_router)
    app.include_router(agent_webhooks_router)
    app.include_router(agent_memory_router)
    app.include_router(agent_schedules_router)
    app.include_router(v2_catalog_router)
    app.include_router(v2_schema_router)
    app.include_router(v2_sample_router)
    app.include_router(v2_scan_router)
    app.include_router(v2_marketplace_router)
    app.include_router(marketplaces_router)
    app.include_router(data_packages_router)
    app.include_router(admin_mcp_router)
    app.include_router(admin_datasource_secrets_router)
    app.include_router(admin_slack_secrets_router)
    app.include_router(source_connections_admin_router)
    app.include_router(source_discovery_admin_router)
    app.include_router(mcp_passthrough_router)
    app.include_router(mcp_user_secrets_router)
    app.include_router(mcp_oauth_connect_router)
    app.include_router(mcp_per_table_router)
    app.include_router(memory_domains_router)
    app.include_router(knowledge_digests_router)
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
    app.include_router(agent_builder_router)
    app.include_router(entity_builder_router)
    app.include_router(package_builder_router)
    app.include_router(mcp_builder_router)
    app.include_router(facts_router)
    app.include_router(sharing_router)
    app.include_router(knowledge_search_router)
    app.include_router(stack_router)
    app.include_router(stack_views_router)
    app.include_router(initial_workspace_router)
    app.include_router(config_surface_router)
    app.include_router(store_router)
    app.include_router(store_lint_admin_router)
    app.include_router(my_stack_router)
    app.include_router(marketplace_router)
    app.include_router(welcome_router)
    app.include_router(connectors_router)
    app.include_router(claude_md_router)
    app.include_router(prompts_router)
    app.include_router(news_router)
    app.include_router(cowork_user_router)
    app.include_router(cowork_auth_router)
    app.include_router(mcp_connect_router)

    # MCP mounts — registration order matters. Starlette matches mounts in
    # order and `/api/mcp` is a path-prefix of both `/api/mcp/http/*` and
    # `/api/mcp/oauth/*`, so the more specific routes MUST be registered first
    # or the SSE mount shadows them. Order: consent bridge → streamable app →
    # SSE app (broadest, last). All three precede web_router's catch-all and
    # are GZip-excluded below via skip_prefixes.

    # Streamable-HTTP MCP server with native OAuth 2.1 (remote MCP connectors).
    # Boot-safety boundary: a misconfigured public origin must NEVER crash app
    # startup. The canonical trap is SERVER_URL/AGNES_BASE_URL pinning a plain
    # http:// non-localhost origin (tls_mode=none deployments) — the MCP SDK
    # rejects it as OAuth issuer (RFC 8414 requires HTTPS; only localhost /
    # 127.0.0.1 may be http). Degrade: skip the connector, keep the app up.
    try:
        _streamable_app = _make_mcp_streamable_app()
    except Exception:
        _streamable_app = None
        logger.exception(
            "Streamable MCP connector DISABLED: building its OAuth app failed. "
            "Most common cause: AGNES_BASE_URL/SERVER_URL pins a plain-HTTP, "
            "non-localhost origin, which cannot serve as an OAuth issuer "
            "(RFC 8414 requires HTTPS). Set it to an https:// URL — or unset "
            "it and use AGNES_INTERNAL_URL for the chat-sandbox data rails — "
            "to re-enable /api/mcp/http. Everything else keeps running."
        )

    # Native OAuth 2.1 consent/login bridge (/api/mcp/oauth/*). Plain Starlette
    # routes (not a FastAPI router) so this browser OAuth flow stays off the
    # documented JSON-API surface, like the SDK's authorize/token endpoints.
    # Skipped in degraded mode along with the discovery routes below — all
    # three surfaces exist solely for the streamable connector.
    if _streamable_app is not None:
        for _route in _make_mcp_consent_routes():
            app.router.routes.append(_route)

        # Root-level OAuth discovery metadata (RFC 8414 + RFC 9728). The SDK
        # serves these relative to the streamable sub-app (under /api/mcp/http),
        # but standards-compliant MCP clients probe the ORIGIN ROOT, so we also
        # publish them there. Content is identical — endpoints are derived from
        # the issuer URL, not from where the document is served.
        for _route in _mcp_oauth_discovery_routes():
            app.router.routes.append(_route)

        # The bare mount path is the advertised connector URL; Starlette's
        # Mount alone doesn't match it (no trailing slash), so an exact-path
        # route must catch it before the broader SSE mount below does.
        app.router.routes.append(_mcp_streamable_mount_root_route(_streamable_app))
        app.mount("/api/mcp/http", _streamable_app)
    # Lift the FastMCP instance onto the main app so the lifespan can run its
    # session manager (Starlette doesn't run mounted sub-app lifespans). None
    # in degraded mode — streamable_session_manager_lifespan no-ops on None.
    app.state.mcp_streamable_instance = (
        getattr(_streamable_app.state, "mcp_streamable_instance", None) if _streamable_app is not None else None
    )

    # HTTP MCP (SSE transport) for cowork VM access — broadest prefix, last.
    app.mount("/api/mcp", _make_mcp_sse_app())

    app.include_router(cache_warmup_router)
    app.include_router(bq_metadata_refresh_router)
    app.include_router(keboola_semantic_layer_refresh_router)
    app.include_router(databricks_semantic_layer_refresh_router)
    app.include_router(activity_router)
    app.include_router(observability_router)
    app.include_router(admin_user_sessions_router)
    app.include_router(admin_sessions_router)
    app.include_router(admin_usage_router)
    app.include_router(admin_usage_summary_router)
    app.include_router(admin_reports_router)
    app.include_router(admin_dashboard_router)
    app.include_router(admin_adoption_router)
    app.include_router(admin_contributed_skills_router)
    app.include_router(db_state_router)
    app.include_router(admin_analytics_router)
    app.include_router(marketplace_server_router)
    app.include_router(chat_router)
    app.include_router(chat_uploads_router)
    app.include_router(chat_session_files_router)
    app.include_router(chat_copresence_router)
    app.include_router(slack_router)
    app.include_router(admin_chat_router)
    app.include_router(notifications_ws_router)
    app.include_router(broker_router)
    app.include_router(kai_router)

    # Git smart-HTTP endpoint for Claude Code: /marketplace.git/*
    # Native ASGI route that shells out to the real `git http-backend` CLI
    # binary (CGI protocol) — see app/marketplace_server/git_router.py for
    # why this replaced the dulwich/WSGI bridge.
    app.include_router(marketplace_git_router)

    # Git smart-HTTP endpoint for internal-mode data apps:
    # /data-apps.git/{slug}/* — same CGI-subprocess mechanism as
    # marketplace_git_router, gated on per-app owner/Admin/grant RBAC
    # instead of a per-caller filtered repo. See app/api/data_apps_git.py.
    app.include_router(data_apps_git_router)

    # Control-plane REST for hosted data apps: CRUD, deploy, stop, delete,
    # secrets, logs, readiness, admin reap-idle. See app/api/data_apps.py.
    app.include_router(data_apps_router)

    # Web UI for hosted data apps: GET /apps (list) + GET /apps/detail/{slug}
    # (detail) — see app/web/router.py's `apps_web_router`. MUST be
    # registered BEFORE data_apps_proxy_router below: Starlette matches
    # routes in registration-list order (not by specificity), and the
    # proxy's catch-all `/apps/{slug}/{path:path}` would otherwise swallow
    # `/apps/detail/<slug>` as slug="detail", path="<slug>" before these
    # literal routes ever got a look.
    app.include_router(data_apps_web_router)

    # Ingress proxy for hosted data apps: /apps/{slug}/... (+ the matching
    # websocket bridge) — auth-gated stream proxy, wake-on-request, and the
    # holding page. See app/api/data_apps_proxy.py.
    app.include_router(data_apps_proxy_router)

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

    for _plugin_router in _load_plugin_routers(_get_value("plugins", "admin_routers", default=[]) or []):
        app.include_router(_plugin_router)

    # /agents is served by the paper-theme redesign builder page in
    # web_router (app/web/router.py), client-rendered against /api/v1/agents.
    # It used to call its own /api/agents adapter router — deleted in the
    # remediation-program's "one agent model" Track C1 (Task C1.2): v1
    # absorbed every builder-shape operation (Task C1.1), so a second
    # registry over the same `agents` table no longer earns its keep.

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
            from fastapi.concurrency import run_in_threadpool

            from app.auth.dependencies import get_current_user
            from src.db import get_system_db
            from src.repositories import use_pg

            # get_current_user is now a plain ``def`` (Tier 1, PR #188) — it must
            # not be ``await``ed as a coroutine. Offload it to the thread pool so
            # its sync RBAC/DB read never runs on the async exception handler's
            # loop. On Postgres no system DuckDB is opened (use_pg() guard); the
            # dependency routes through the repository factory with conn=None.
            conn = None if use_pg() else get_system_db()
            try:
                authorization = request.headers.get("authorization")
                return await run_in_threadpool(
                    get_current_user,
                    request=request,
                    authorization=authorization,
                    conn=conn,
                )
            finally:
                if conn is not None:
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
        ``url_for`` helpers — without these, base.html + _app_rail.html
        silently render empty header/stylesheets."""
        from app.logging_config import request_id_var
        from app.web.router import templates as _web_templates, _build_context

        title = _ERROR_TITLES.get(code, "Error")
        user = await _resolve_error_user(request)
        # A non-admin opening an admin entity URL (a teammate copied their
        # own address bar) used to dead-end on a generic 403 — but the
        # id→slug mapping is one repo read, so the page can bridge to the
        # surface the caller IS allowed to try. The catalog page enforces
        # its own grant check, so this reveals only what that page's 403
        # already reveals (data packages are the deliberately-403,
        # existence-visible kind — collections 404 instead).
        bridge = None
        if code == 403:
            import re as _re

            m = _re.match(r"^/admin/data-packages/([\w\-]+)$", request.url.path)
            if m:
                try:
                    from src.repositories import data_packages_repo as _dp_repo

                    _pkg = _dp_repo().get(m.group(1))
                    if _pkg and _pkg.get("slug"):
                        bridge = {
                            "href": f"/catalog/p/{_pkg['slug']}",
                            "name": _pkg.get("name") or _pkg["slug"],
                        }
                except Exception:  # noqa: BLE001 — the bridge is chrome; the 403 must render regardless
                    bridge = None
        ctx = _build_context(
            request,
            user=user,
            code=code,
            title=title,
            message=message,
            path=request.url.path,
            bridge=bridge,
            traceback=traceback_str,
            request_id=request_id_var.get(),
        )
        return _web_templates.TemplateResponse(request, "error.html", ctx, status_code=code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(request, exc: RequestValidationError):
        """422 body without the rejected input echoed back.

        FastAPI's default validation error puts the offending value in an
        ``input`` key. On a request body that carries a credential that hands
        the secret straight back to the caller — and into every access log,
        proxy and error tracker on the way. Found live: a ``PUT
        /api/admin/source-connections/{id}/secret`` sent with the wrong field
        name answered 422 with the Keboola master token verbatim in the body.

        Redaction is not field-name-based on purpose. An allowlist of
        "secret-looking" names is a guess that silently misses the next
        endpoint someone adds, and at least five request models already carry a
        credential in a plain ``value`` / ``token`` field
        (``admin_source_connections``, ``admin_datasource_secrets``,
        ``admin_slack_secrets``, ``admin_mcp``, ``cli_auth``).

        So the shape is a keep-list, not a drop-list: only ``loc``, ``msg``,
        ``type`` and ``url`` are echoed. Dropping ``input`` alone was still a
        guess — a Pydantic v2 error also carries ``ctx``, and for a
        ``value_error`` raised by a field validator that mapping holds the
        validator's own exception, whose text routinely embeds the rejected
        value (``got {v!r}`` appears in ~37 validators here). A keep-list also
        covers whatever key a future Pydantic adds. What survives is what a
        client needs to fix the call — the wrong field name in that live case
        is still named. (Devin Review on this PR.)

        The one channel a keep-list cannot close is ``msg`` itself, which is
        author-controlled: a validator that interpolates the value it rejected
        publishes it. That is a rule for validators on credential-carrying
        fields, not something this handler can decide — it cannot tell a
        rejected enum from a rejected token.
        """
        keep = ("loc", "msg", "type", "url")
        redacted = [{k: error[k] for k in keep if k in error} for error in exc.errors()]
        return JSONResponse(status_code=422, content=jsonable_encoder({"detail": redacted}))

    @app.exception_handler(RequiresPostgresBackend)
    async def _requires_postgres_backend_handler(request, exc: RequiresPostgresBackend):
        """A3 PG-first ratchet: a route resolved a Postgres-only repository on
        an instance still running the frozen DuckDB app-state backend. Fail
        clean with a 501 naming the feature — never an unhandled 500 — see
        CLAUDE.md -> "Dual-backend discipline" and docs/migrations.md."""
        return JSONResponse(
            status_code=501,
            content={
                "detail": str(exc),
                "error": "requires_postgres_backend",
                "feature": exc.feature,
            },
        )

    def _main_host_base_url(request) -> str:
        """Absolute ``scheme://host`` of the MAIN Agnes origin, for redirecting
        a caller off a data-app subdomain.

        ``SERVER_URL`` is what the customer-instance module actually writes;
        ``get_public_url()`` covers the ``PUBLIC_URL`` / ``server.public_url``
        configurations. When neither is set, fall back to the parent domain the
        session cookie is scoped to — not a guess: the cookie is scoped there
        precisely so a login on the main host is valid on the app subdomains,
        which only holds when the main host sits under that parent.
        """
        from app.instance_config import get_public_url, session_cookie_domain

        url = get_public_url() or (os.environ.get("SERVER_URL") or "").strip().rstrip("/")
        if url:
            return url
        parent = (session_cookie_domain() or "").lstrip(".")
        return f"{request.url.scheme}://{parent}" if parent else ""

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
            # A request that arrived on a data-app subdomain cannot be sent to a
            # RELATIVE `/login`: `DataAppSubdomainMiddleware` rewrites EVERY path
            # on `<slug>.<base>` to `/apps/<slug>/…` with no carve-out, so the
            # browser would resolve `/login` against the app's own host, land back
            # on the proxy as `/apps/<slug>/login`, 401 again — an infinite
            # redirect loop for anyone not already signed in. Send them to the
            # main host, whose login sets a cookie scoped to cover both.
            #
            # The return URL rides along in `next`. That is only safe because
            # `safe_next_path` (`app/auth/_common.py`) was taught, in this same
            # change and deliberately as its own reviewed edit to that guard,
            # to accept exactly one cross-host shape: an absolute http(s) URL on
            # THIS deployment's own `<slug>.<data_apps.subdomain_base>`. Every
            # other cross-host target is still discarded at the far end.
            if request.scope.get("agnes_data_app_subdomain"):
                main_host = _main_host_base_url(request)
                if main_host:
                    # Absolute return URL in the VISITOR's terms — the app
                    # origin plus the path they actually asked for, not the
                    # `/apps/<slug>/…` form the middleware rewrote it into.
                    # `safe_next_path` accepts exactly this shape (see
                    # `app/auth/_common.py`); anything else login discards.
                    original = request.scope.get("agnes_data_app_original_path") or "/"
                    back = quote(str(request.url.replace(path=original)), safe="")
                    return RedirectResponse(url=f"{main_host}/login?next={back}", status_code=302)
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
