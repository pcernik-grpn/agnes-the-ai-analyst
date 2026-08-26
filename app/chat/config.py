"""Chat feature config (loaded from instance.yaml `chat:` block)."""

from __future__ import annotations

import ipaddress
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.instance_config import coerce_flag_value

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SlackConfig:
    # "http" (default, Events API webhook) | "socket" (Socket Mode WS).
    # Unknown values are normalized to "http" at parse time with a warning.
    # Tokens (SLACK_BOT_TOKEN / SLACK_APP_TOKEN / SLACK_SIGNING_SECRET) are
    # deliberately NOT stored here — resolved at use site via slack_secret
    # (env > vault) so they never leak into a frozen-config echo (e.g.
    # /admin/server-config).
    transport: str = "http"


@dataclass(frozen=True)
class ChatConfig:
    enabled: bool = False
    # Sandbox provider id. ``kai-agent`` (the embedded kai-agent turn engine
    # — sessions run on the engine's own agent loop + sandbox, see
    # app/chat/kai_engine_provider.py; requires the KAI_HOST_JWT_SECRET host
    # wiring from app/api/kai.py) and ``docker`` (self-hosted containers,
    # driven through the apps-runner sidecar) are the production-supported
    # values; a further variant would extend the gate in ``app/main.py``.
    # The ``e2b`` provider was removed in 0.89.0 — a stale ``provider: e2b``
    # boots with chat disabled and an actionable error log.
    provider: str = "kai-agent"
    # Where the embedded engine listens, for ``provider: kai-agent`` only.
    # The default is the compose service name the customer-instance module
    # materializes; the engine is loopback/compose-network-only by design,
    # so this is never a public URL.
    kai_agent_url: str = "http://kai-agent:3000"
    # Agent harness id — which engine drives the in-sandbox session
    # (app/chat/harness.py seam; validated against APPROVED_HARNESSES at
    # boot). ``claude-code`` is the only production harness today.
    harness: str = "claude-code"
    concurrency_per_user: int = 3
    idle_ttl_seconds: int = 30 * 60
    per_tool_call_seconds: int = 90
    per_session_bq_scan_bytes: int = 20 * 1024**3
    daily_anthropic_spend_usd: float = 20.0
    max_session_seconds: int = 4 * 3600
    max_session_tokens: int = 200_000
    rate_messages_per_hour: int = 100
    tool_calls_per_turn_budget: int = 50
    # How long the runner's ApprovalGate waits for the user to answer an
    # approval_request before denying the suspended tool call.
    approval_timeout_seconds: int = 300
    # Operator kill-switch. The gate is armed on every surface and whether a
    # request CAN be answered is decided per request from the attached sinks,
    # so this is not a routing knob — it is the escape hatch for a deployment
    # that would rather run unasked than have tool calls wait. False makes the
    # gate deny instantly with an actionable message.
    approvals_enabled: bool = True
    marketplace_sha_debounce_seconds: int = 5 * 60
    # --- Docker sandbox provider (``provider: docker``) ---------------------
    # Operator-built sandbox image (see
    # app/initial_workspace_default/docker-sandbox/). The apps-runner sidecar
    # additionally enforces CHAT_SANDBOX_IMAGE_PREFIX, so a tag outside the
    # allowlisted prefix is refused at create time.
    docker_image: str = "agnes-chat-sandbox:latest"
    # Docker network the sandbox joins. Must be one the Agnes app is also
    # attached to, or AGNES_SERVER won't resolve from inside the container.
    docker_network: str = "agnes-apps"
    # Always-set resource bounds (a local sandbox contends with the gateway
    # host, unlike an offloaded remote sandbox).
    docker_mem_limit: str = "2g"
    docker_cpus: float = 1.0
    docker_pids_limit: int = 512
    # ``none`` (SECURE DEFAULT) — an ``internal`` bridge where the only
    # reachable origin is whatever else is attached to it, so the agent (which
    # runs with bypassPermissions over a read-write workspace) has no route to
    # exfiltrate data to an arbitrary external host (llm-agency-open-egress-5).
    # In-sandbox package installs stop working in this mode.
    # ``open`` — normal bridge, internet reachable (parity with in-sandbox
    # tools that fetch packages). This is unrestricted egress and must be an
    # explicit operator opt-in (``chat.docker_egress_mode: open``).
    # ``allowlist`` — the internal bridge of ``none`` PLUS an egress-proxy
    # sidecar (services/egress_proxy) dual-homed onto it: sandboxes get
    # HTTP(S)_PROXY pointed at the proxy, which enforces
    # ``docker_egress_allow_hosts`` with a post-resolution IP re-check
    # (DNS-rebinding/metadata protection). Ignoring the proxy is not a
    # bypass — the internal network has no other route out.
    docker_egress_mode: str = "none"
    # Hostnames sandboxes may reach in ``allowlist`` mode (exact or
    # ``*.suffix`` wildcards). Cloud metadata endpoints stay blocked even
    # if listed. Empty = deny everything except the direct internal-network
    # peers (Agnes server / broker relay via NO_PROXY).
    docker_egress_allow_hosts: list[str] = field(default_factory=list)
    # Where sandboxes find the egress proxy in ``allowlist`` mode — must be
    # resolvable on the internal network (the compose service name).
    docker_egress_proxy_url: str = "http://agnes-egress-proxy:3128"
    # Host-wide ceiling on live sandboxes, checked at spawn on top of
    # ``concurrency_per_user``.
    docker_max_total_sandboxes: int = 10
    # Lifecycle when the last sink detaches: "pause" (snapshot, resumable)
    # or "kill" (legacy cost-minimizing behavior).
    on_detach: str = "pause"
    detach_linger_seconds: int = 60
    # Grace window (Tier 1, restart-invariant reuse — see
    # docs/brainstorms/2026-07-23-chat-e2b-architecture-comparison.md §5) a
    # detached-but-still-live sandbox is kept warm before ``_linger_then_pause``
    # actually pauses it: a follow-up message inside this window reuses the
    # already-live sandbox instead of paying a fresh cold spawn. This is the
    # canonical knob ``ChatManager`` reads for that sleep; ``detach_linger_seconds``
    # is kept as a back-compat alias — ``load_chat_config`` defaults
    # ``idle_grace_seconds`` to whatever ``detach_linger_seconds`` resolves to
    # when the operator's instance.yaml doesn't set ``idle_grace_seconds``
    # explicitly, so existing configs need no changes.
    idle_grace_seconds: int = 60
    paused_ttl_seconds: int = 7 * 24 * 3600
    # When true, the caller's RBAC-filtered marketplace skills are delivered
    # into the agent's project scope, so a stack skill is actually invokable
    # (`/<skill-name>`) in chat. One mechanism — the server materializes the
    # skill directories; two placements, since the providers differ in what
    # Agnes owns (`app.chat.skills_catalog.marketplace_delivery` is the gate):
    #   - docker: written into the per-user chat workspace the sandbox
    #     bind-mounts (`app/chat/workdir.py`).
    #   - kai-agent: overlaid into the workspace tarball the engine
    #     materializes into its own project scope (`app/api/kai.py`).
    # ON by default since 0.87.1: with it off the composer's slash menu
    # advertised marketplace skills the agent had never been given, so
    # `/<skill>` came back "Unknown command" (#1552). Turning it off (the
    # `chat_bootstrap_marketplace` switch) makes the menu stop offering
    # marketplace skills rather than lie about them. Independent of the
    # always-on plugin.json sanitization in the marketplace packager.
    bootstrap_marketplace: bool = True
    # How the chat broker authenticates to Anthropic. ``api_key`` (default) uses
    # the static ``ANTHROPIC_API_KEY``. ``workload_identity`` mints a short-lived
    # token from the workload's own OIDC identity via Anthropic Workload Identity
    # Federation (``app/auth/wif.py``) — no long-lived key anywhere. Opt-in; the
    # federation env (ANTHROPIC_FEDERATION_RULE_ID / ORGANIZATION_ID /
    # SERVICE_ACCOUNT_ID / IDENTITY_TOKEN[_FILE]) must be configured for it. Any
    # value other than ``workload_identity`` falls back to ``api_key``.
    llm_auth: str = "api_key"
    # Agent-as-API broker policy (Task 8, agent-profiles V1a): models a
    # brokered chat-completion request may use besides the calling agent's
    # own pinned model — e.g. a cheap utility model every agent is allowed
    # to fall back to regardless of its persona's primary model. An agent
    # with no pinned model has NO model policy at all (every model is
    # allowed; utility_models is irrelevant for it). Empty by default (no
    # extra models allowed). Enforced in
    # ``app/api/broker_agent_policy.py::check_model``.
    agent_api_utility_models: list[str] = field(default_factory=list)
    # TTL (seconds) for the cached agent-monthly-token-total the broker
    # checks on every brokered call before enforcing ``token_budget_monthly``
    # (``app/api/broker_agent_policy.py::cached_month_total``). Trades
    # budget-enforcement freshness for avoiding a ledger table read on every
    # LLM call.
    agent_api_budget_cache_ttl_s: int = 60
    # Per-session artifact harvest caps (V1b Task 5,
    # ``app.chat.artifact_harvest``): ``agent_api_artifact_max_bytes`` is a
    # CUMULATIVE cap on the total bytes harvested for a session across all
    # its files (not a per-file limit) — a file is skipped (logged, scan
    # continues) once harvesting it would push the running total over the
    # cap. The scan stops entirely after ``agent_api_artifact_max_files``
    # files harvested. Both are best-effort guardrails against a run that
    # fills the outputs dir with an unbounded number/size of files, not a
    # retention policy — retention (GC of already-harvested rows/blobs) is
    # a later task.
    agent_api_artifact_max_bytes: int = 25 * 1024 * 1024
    agent_api_artifact_max_files: int = 20
    # Outbound webhook delivery (V1b Task 6, `app.chat.webhook_delivery`):
    # consecutive delivery failures after which a webhook is auto-disabled
    # (`agent_webhooks_repo().disable`) rather than retried forever against
    # a permanently-broken (or no-longer-owned) endpoint.
    agent_api_webhook_max_failures: int = 5
    # Agent memory notebook write endpoint (V1c Task 4, `app.api.agent_memory`):
    # per-write content size cap, rolling hourly write-rate cap (both
    # regardless of `memory_write_mode`), and a total-pending backlog cap
    # (C3) — independent of the hourly rate — that a `propose`-mode agent
    # writing steadily just under the rate limit would otherwise blow past
    # forever, since nothing else shrinks the pending set except the
    # owner's own review/approve action.
    agent_memory_max_chars: int = 2000
    agent_memory_writes_per_hour: int = 20
    agent_memory_max_pending: int = 100
    # Age threshold (days) past which a `pending` memory row is considered
    # stale for a future reaper job. INERT — nothing reads this field today
    # (NOT YET enforced anywhere); see `app.api.agent_memory.remember`'s
    # docstring for why reaping is deferred rather than wired into
    # `count_pending` today. Landed now so that reaper has a config knob to
    # read once it exists — an operator setting this expecting it to bound
    # the pending cap will be silently misled until the reaper lands.
    agent_memory_pending_ttl_days: int = 30
    slack: "SlackConfig" = field(default_factory=SlackConfig)


def _parse_slack_config(raw_chat: dict) -> SlackConfig:
    _s = raw_chat.get("slack")
    raw_slack = _s if isinstance(_s, dict) else {}
    raw_value = raw_slack.get("transport", "http")
    transport = str(raw_value).strip().lower() if raw_value is not None else ""
    if transport not in ("http", "socket"):
        logger.warning(
            "unknown slack transport %r in chat.slack.transport — falling back to 'http'",
            transport,
        )
        transport = "http"
    return SlackConfig(transport=transport)


def _raw_str(raw: dict, key: str, default: str) -> str:
    """``raw[key]`` as a string, treating an absent, blank, or null value as
    ``default``.

    A key written with nothing after it (``docker_egress_mode:``) parses to
    YAML null; the naive ``str(raw.get(key, default))`` then yields ``"None"``
    — or, lowercased, the *valid-looking* mode ``"none"``, silently cutting
    the sandbox off from the internet (the same trap #1148 fixed for the
    egress proxy's mode; it also bit ``provider``/``harness`` here, where a
    blank key turned into the literal provider ``"None"`` and failed the boot
    gate instead of meaning "use the default").
    """
    value = raw.get(key, default)
    text = str(value).strip() if value is not None else ""
    return text or default


def _raw_number(raw: dict, key: str, default, cast):
    """Numeric sibling of :func:`_raw_str`: absent, blank, or null →
    ``default``; a value that doesn't parse warns and falls back instead of
    aborting the whole chat config load (``int(None)``/``float(None)`` used
    to raise out of ``load_chat_config``, turning one blank key into chat
    being disabled at boot)."""
    value = raw.get(key, default)
    if value is None or (isinstance(value, str) and not value.strip()):
        return cast(default)
    try:
        return cast(value)
    except (TypeError, ValueError):
        logger.warning("invalid chat.%s %r — falling back to %r", key, value, default)
        return cast(default)


def _raw_int(raw: dict, key: str, default: int) -> int:
    return _raw_number(raw, key, default, int)


def _raw_float(raw: dict, key: str, default: float) -> float:
    return _raw_number(raw, key, default, float)


def _parse_docker_egress_mode(raw: dict) -> str:
    """``none`` (secure default) | ``open`` | ``allowlist``; anything else warns
    and falls back to the SECURE ``none`` — a misconfigured value must fail
    closed, never grant a sandbox unrestricted internet egress
    (llm-agency-open-egress-5). ``open`` is unrestricted egress and is only ever
    reached by an explicit, correctly-spelled operator opt-in."""
    mode = _raw_str(raw, "docker_egress_mode", "none").lower()
    if mode not in ("open", "none", "allowlist"):
        logger.warning("unknown chat.docker_egress_mode %r — falling back to secure 'none'", mode)
        mode = "none"
    return mode


def _parse_on_detach(raw: dict) -> str:
    on_detach = _raw_str(raw, "on_detach", "").lower()
    if on_detach not in ("pause", "kill"):
        if on_detach:
            logger.warning("unknown chat.on_detach %r — falling back to 'pause'", on_detach)
        if raw.get("e2b_kill_on_ws_disconnect") is not None:
            # Removed alias (0.89.0, with the e2b provider): it used to imply
            # ``on_detach: kill``. Warn-and-ignore — the stale key now gets
            # the ``pause`` default.
            logger.warning("chat.e2b_kill_on_ws_disconnect is removed; use chat.on_detach: kill")
        on_detach = "pause"
    return on_detach


def _resolve_chat_approvals(raw: dict) -> bool:
    """``chat.approvals_enabled`` resolution, mirroring
    :func:`_resolve_chat_enabled`: ``AGNES_CHAT_APPROVALS_ENABLED`` env > the
    ``approvals_enabled`` key in the parsed ``chat:`` block > ``True``.

    Reading only the YAML would have made the env var the registry and
    ``docs/feature-flags.md`` advertise do nothing, and — worse — left
    ``/admin/server-config`` reporting a value the running system does not
    honour, because the admin panel resolves flags through ``feature_enabled``
    (env first) while the gate reads this config (Devin Review on #1157).
    """
    env = os.environ.get("AGNES_CHAT_APPROVALS_ENABLED")
    if env is not None:
        return coerce_flag_value(env, default=True)
    return coerce_flag_value(raw.get("approvals_enabled"), default=True)


def _resolve_chat_enabled(raw: dict) -> bool:
    """``chat.enabled`` resolution: ``AGNES_CHAT_ENABLED`` env (new, additive
    — #1022 feature-flag canonicalization) > the ``enabled`` key in the
    parsed ``chat:`` block > ``False``.

    Kept local rather than calling ``app.instance_config.feature_enabled``
    directly: this function's yaml source is whichever ``instance_yaml``
    path the caller passed to :func:`load_chat_config` (e.g. an isolated
    ``tmp_path`` fixture in tests), not the process-global
    ``load_instance_config()`` merge ``feature_enabled`` reads from — so
    only the truthy-parsing convention
    (:func:`app.instance_config.coerce_flag_value`) is shared here, not the
    value source. See ``docs/feature-flags.md``.
    """
    env = os.environ.get("AGNES_CHAT_ENABLED")
    if env is not None:
        return coerce_flag_value(env, default=False)
    return coerce_flag_value(raw.get("enabled"), default=False)


def _resolve_chat_bootstrap_marketplace(raw: dict) -> bool:
    """``chat.bootstrap_marketplace`` resolution:
    ``AGNES_CHAT_BOOTSTRAP_MARKETPLACE`` env > the ``bootstrap_marketplace``
    key in the parsed ``chat:`` block > ``True``.

    Same precedence convention as :func:`_resolve_chat_enabled` — and the same
    reason for the env layer: infrastructure pins the flag durably (a fresh
    data disk boots with no ``instance.yaml``), and ``/admin/server-config``
    must report the value the running system actually honours rather than the
    YAML's.
    """
    env = os.environ.get("AGNES_CHAT_BOOTSTRAP_MARKETPLACE")
    if env is not None:
        return coerce_flag_value(env, default=True)
    return coerce_flag_value(raw.get("bootstrap_marketplace"), default=True)


def _resolve_chat_provider(raw: dict) -> str:
    """``chat.provider`` resolution: ``AGNES_CHAT_PROVIDER`` env > the
    ``provider`` key in the parsed ``chat:`` block > ``"kai-agent"``.

    The env override exists so INFRASTRUCTURE can pin the provider durably:
    the deployment env rides code-reviewed Terraform (the ``customer-instance``
    module's per-VM ``chat_provider`` field writes it into the app env), while
    ``instance.yaml`` is a hand-edited overlay on the data disk that a fresh
    machine starts without. Same precedence convention as
    ``_resolve_chat_enabled`` above; a blank env value means unset (a key
    written with nothing after it must not override the yaml, mirroring
    ``_raw_str``'s treatment of blank yaml values).
    """
    env = (os.environ.get("AGNES_CHAT_PROVIDER") or "").strip()
    if env:
        return env
    return _raw_str(raw, "provider", "kai-agent")


def _resolve_kai_agent_url(raw: dict) -> str:
    """``chat.kai_agent_url`` resolution: ``AGNES_CHAT_KAI_AGENT_URL`` env >
    the ``kai_agent_url`` key > ``"http://kai-agent:3000"``.

    Same precedence convention and same motivation as
    :func:`_resolve_chat_provider` — the provider it configures is pinned from
    the deployment env, so its endpoint has to be pinnable the same way or the
    pair can only ever be half-configured from infrastructure. It also makes
    the provider reachable from a laptop: the default names a compose service,
    which does not resolve outside the compose network, and the local-dev flow
    deliberately runs without an ``instance.yaml`` (see
    ``docs/kai-agent-local-dev.md``).
    """
    env = (os.environ.get("AGNES_CHAT_KAI_AGENT_URL") or "").strip()
    if env:
        return env
    return _raw_str(raw, "kai_agent_url", "http://kai-agent:3000")


def load_chat_config(instance_yaml: Path) -> ChatConfig:
    if not instance_yaml.exists():
        return ChatConfig(
            enabled=_resolve_chat_enabled({}),
            # A fresh machine (no instance.yaml yet) must still honour an
            # infra-pinned provider — without this the first boot ran the
            # default provider regardless of the deployment env.
            provider=_resolve_chat_provider({}),
            kai_agent_url=_resolve_kai_agent_url({}),
            approvals_enabled=_resolve_chat_approvals({}),
            # Same reason as ``provider``: an infra-pinned flag must survive a
            # machine that has not written its ``instance.yaml`` overlay yet.
            bootstrap_marketplace=_resolve_chat_bootstrap_marketplace({}),
        )
    data = yaml.safe_load(instance_yaml.read_text()) or {}
    raw = data.get("chat", {}) or {}
    detach_linger_seconds = _raw_int(raw, "detach_linger_seconds", 60)
    return ChatConfig(
        enabled=_resolve_chat_enabled(raw),
        provider=_resolve_chat_provider(raw),
        kai_agent_url=_resolve_kai_agent_url(raw),
        harness=_raw_str(raw, "harness", "claude-code"),
        concurrency_per_user=_raw_int(raw, "concurrency_per_user", 3),
        idle_ttl_seconds=_raw_int(raw, "idle_ttl_seconds", 30 * 60),
        per_tool_call_seconds=_raw_int(raw, "per_tool_call_seconds", 90),
        per_session_bq_scan_bytes=_raw_int(raw, "per_session_bq_scan_bytes", 20 * 1024**3),
        daily_anthropic_spend_usd=_raw_float(raw, "daily_anthropic_spend_usd", 20.0),
        max_session_seconds=_raw_int(raw, "max_session_seconds", 4 * 3600),
        max_session_tokens=_raw_int(raw, "max_session_tokens", 200_000),
        rate_messages_per_hour=_raw_int(raw, "rate_messages_per_hour", 100),
        tool_calls_per_turn_budget=_raw_int(raw, "tool_calls_per_turn_budget", 50),
        approval_timeout_seconds=_raw_int(raw, "approval_timeout_seconds", 300),
        approvals_enabled=_resolve_chat_approvals(raw),
        marketplace_sha_debounce_seconds=_raw_int(raw, "marketplace_sha_debounce_seconds", 5 * 60),
        docker_image=str(raw.get("docker_image") or "agnes-chat-sandbox:latest"),
        docker_network=str(raw.get("docker_network") or "agnes-apps"),
        docker_mem_limit=str(raw.get("docker_mem_limit") or "2g"),
        docker_cpus=_raw_float(raw, "docker_cpus", 1.0),
        docker_pids_limit=_raw_int(raw, "docker_pids_limit", 512),
        docker_egress_mode=_parse_docker_egress_mode(raw),
        docker_egress_allow_hosts=[str(h) for h in (raw.get("docker_egress_allow_hosts") or [])],
        docker_egress_proxy_url=_raw_str(raw, "docker_egress_proxy_url", "http://agnes-egress-proxy:3128"),
        docker_max_total_sandboxes=_raw_int(raw, "docker_max_total_sandboxes", 10),
        on_detach=_parse_on_detach(raw),
        detach_linger_seconds=detach_linger_seconds,
        # Falls back to detach_linger_seconds's own resolved value when the
        # operator's instance.yaml doesn't set idle_grace_seconds explicitly
        # — see ChatConfig.idle_grace_seconds's docstring.
        idle_grace_seconds=_raw_int(raw, "idle_grace_seconds", detach_linger_seconds),
        paused_ttl_seconds=_raw_int(raw, "paused_ttl_seconds", 7 * 24 * 3600),
        bootstrap_marketplace=_resolve_chat_bootstrap_marketplace(raw),
        llm_auth=_raw_str(raw.get("llm") or {}, "auth", "api_key").lower(),
        agent_api_utility_models=list(raw.get("agent_api_utility_models") or []),
        agent_api_budget_cache_ttl_s=_raw_int(raw, "agent_api_budget_cache_ttl_s", 60),
        agent_api_artifact_max_bytes=_raw_int(raw, "agent_api_artifact_max_bytes", 25 * 1024 * 1024),
        agent_api_artifact_max_files=_raw_int(raw, "agent_api_artifact_max_files", 20),
        agent_api_webhook_max_failures=_raw_int(raw, "agent_api_webhook_max_failures", 5),
        agent_memory_max_chars=_raw_int(raw, "agent_memory_max_chars", 2000),
        agent_memory_writes_per_hour=_raw_int(raw, "agent_memory_writes_per_hour", 20),
        agent_memory_max_pending=_raw_int(raw, "agent_memory_max_pending", 100),
        # Inert (see the field's own comment above) — parsed and stored for
        # forward-compat with the not-yet-built reaper, not read anywhere yet.
        agent_memory_pending_ttl_days=_raw_int(raw, "agent_memory_pending_ttl_days", 30),
        slack=_parse_slack_config(raw),
    )


#: What `docker-compose.yml` pins for the egress-proxy sidecar. Allowlist
#: mode only works when the app's config agrees with these.
_COMPOSE_EGRESS_NETWORK = "agnes-apps"
_COMPOSE_EGRESS_PROXY_URL = "http://agnes-egress-proxy:3128"


def sandbox_can_reach_directly(host: str) -> bool:
    """Is ``host`` reachable from a sandbox WITHOUT going through the proxy?

    The single definition behind two decisions that must agree: which host
    `_egress_env` puts in NO_PROXY, and which rails URL the boot check warns
    about. They were written separately and immediately drifted — a "dotless
    name" shortcut silently excluded `host.docker.internal`, the very address
    `app/main.py` recommends for bare-host deployments (Devin Review on #1148).

    Directly reachable means: a compose service / container name (dotless),
    the Docker host alias, or a private/loopback IP literal. Everything else
    is a public address the internal no-route-out bridge cannot reach at all,
    so it must go through the proxy — where an operator can allowlist it.
    """
    h = (host or "").strip().lower().strip("[]")
    if not h:
        return False
    if h in ("localhost", "host.docker.internal") or h.endswith(".docker.internal"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return "." not in h  # bare service/container name
    return ip.is_private or ip.is_loopback or ip.is_link_local


def egress_compose_mismatches(cfg: "ChatConfig") -> list[str]:
    """Ways an allowlist-mode instance's config can disagree with compose.

    Allowlist mode is split across two owners: the app decides which
    sandboxes exist, while `docker-compose.yml` owns the proxy sidecar —
    its hostname, its network, and the allowlist it actually enforces. The
    `chat.*` keys for all three read like ordinary knobs, so turning any of
    them produces a *silent* failure: the sandbox network has no route out
    by design, so a proxy that is not on it, or is not told the same hosts,
    denies everything with nothing in the logs pointing at the cause.

    Collected in one place, and checked together, because these are three
    faces of one assumption — that `chat.*` is authoritative for the
    sidecar, when compose holds the enforcing copy (Devin Review on #1148).
    """
    if cfg.docker_egress_mode != "allowlist":
        # The allow-hosts knob reads like it turns the allowlist on, but only
        # the mode does — set alone (mode defaults to "open") the hosts are
        # silently ignored, and in "open" that misconfiguration fails open.
        if cfg.docker_egress_allow_hosts:
            consequence = (
                "sandbox egress stays unrestricted"
                if cfg.docker_egress_mode == "open"
                else "sandboxes have no egress at all"
            )
            return [
                f"chat.docker_egress_allow_hosts is set, but chat.docker_egress_mode is "
                f"{cfg.docker_egress_mode!r}, so the allowlist is not enforced and "
                f"{consequence} — set chat.docker_egress_mode: allowlist to enforce it"
            ]
        return []
    out = []
    if cfg.docker_egress_allow_hosts:
        out.append(
            "chat.docker_egress_allow_hosts is set, but the enforcing copy is the egress-proxy "
            "sidecar's EGRESS_ALLOW_HOSTS environment variable — if the two disagree, sandboxes "
            "are denied hosts you believe you allowed"
        )
    if cfg.docker_network != _COMPOSE_EGRESS_NETWORK:
        out.append(
            f"chat.docker_network is {cfg.docker_network!r}, so sandboxes join "
            f"{cfg.docker_network}-internal, but docker-compose.yml puts the egress proxy on "
            f"{_COMPOSE_EGRESS_NETWORK}-internal — the proxy will not be reachable and ALL egress "
            f"will fail. Allowlist mode requires chat.docker_network: {_COMPOSE_EGRESS_NETWORK}"
        )
    if cfg.docker_egress_proxy_url != _COMPOSE_EGRESS_PROXY_URL:
        out.append(
            f"chat.docker_egress_proxy_url is {cfg.docker_egress_proxy_url!r}, but compose names "
            f"the sidecar container agnes-egress-proxy ({_COMPOSE_EGRESS_PROXY_URL})"
        )
    # The rails URL is the fourth face of the same assumption. Judge the URL
    # that actually WINS — agnes_server_url() prefers SERVER_URL over
    # AGNES_INTERNAL_URL — not merely whether the override happens to be set.
    # Gating on "is AGNES_INTERNAL_URL present" made this go quiet for an
    # operator who followed its own advice while the public URL still won, so
    # the check confirmed a fix that had no effect (Devin Review on #1148).
    rails = (os.environ.get("SERVER_URL") or os.environ.get("AGNES_INTERNAL_URL") or "").strip()
    if rails:
        from urllib.parse import urlparse

        host = urlparse(rails).hostname or ""
        if host and not sandbox_can_reach_directly(host):
            src = "SERVER_URL" if (os.environ.get("SERVER_URL") or "").strip() else "AGNES_INTERNAL_URL"
            out.append(
                f"the sandbox rails URL resolves from {src} to {host!r}, which the internal "
                "no-route-out network cannot reach directly. SERVER_URL takes precedence, so "
                "setting AGNES_INTERNAL_URL alone does NOT fix this — either unset SERVER_URL for "
                "the chat rails or point it at a container-reachable address (e.g. http://app:8000)"
            )
    return out
