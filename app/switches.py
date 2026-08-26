"""Unified switch registry — every operator-facing toggle in one place.

Before this module, gating lived in six unrelated mechanisms: the
`FEATURE_FLAGS` registry, hand-copied boolean resolvers, enum resolvers with
their own typo behavior, env-only switches with inline truthy parsing,
deployment-time selection, and the admin field metadata in
`app/api/admin.py`. A switch's editability was decided in one file and
justified in another — a test.

Everything a switch needs is declared here. `_EDITABLE_SECTIONS`, the admin
field metadata, the settings panel and the operator documentation all derive
from this tuple; none of them restates it.

Import direction is one-way: this module must not import
`app.instance_config` at module level — that module imports this one. The
local imports inside `switch_value` are deliberate and have precedent in
`src/analytics_backend.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Effect classes — what the system can do with a new value.
#:   live    — read per request; a save takes effect immediately.
#:   restart — read at boot; a save is stored and applies after a restart.
#:   deploy  — not a section of instance.yaml at all; it is what the
#:             container was started with. Nothing to write.
EFFECTS = ("live", "restart", "deploy")

#: Display groups in the settings panel. Independent of `editable`: a
#: `product` row can still be read-only.
CATEGORIES = ("product", "operations", "locked")

#: Kinds `switch_value` knows how to resolve: `bool` coerces through the
#: shared truthy-parsing rule, `int` parses (falling back to `default` on a
#: bad value), `select` validates against `options` (per `on_invalid`).
#: Anything else falls through the bottom of that dispatch and comes back as
#: a lowercased string with no coercion at all — the trap that lets a
#: typo'd `kind="boolean"` return the truthy string `"false"` instead of a
#: coerced `False`. `test_switches.py` asserts every entry's `kind` is one of
#: these.
KINDS = ("bool", "int", "select")

#: What `switch_value` does with a `select` switch's unrecognized value:
#: `default` falls back silently (the common case), `raise` fails loudly at
#: read time (use for a switch where guessing wrong is worse than crashing,
#: e.g. a backend selector). `test_switches.py` asserts every entry's
#: `on_invalid` is one of these.
ON_INVALID = ("default", "raise")


@dataclass(frozen=True)
class Switch:
    """One operator-facing toggle.

    `effect` and `editable` are deliberately orthogonal: `effect` states what
    the system *can* do with a new value, `editable` whether we *offer* one.
    A switch is locked for one of three reasons — nothing to write
    (`effect="deploy"`), a deliberate security lock, or an unmet dependency —
    and `lock_reason` is what the product shows the operator in each case.

    `editable` has no default and every entry must state it explicitly.
    `POST /api/admin/server-config` validates only the SECTION name and then
    deep-merges the patch, so `editable=True` on one switch makes the whole
    section — every key in it, not just this switch's — admin-writable. A
    class default would make that a silent side effect of adding a `Switch`
    rather than a decision stated at each call site.
    """

    name: str
    config_keys: tuple[str, ...]
    env_var: str
    kind: str
    default: Any
    effect: str
    category: str
    description: str
    editable: bool
    options: tuple[str, ...] = ()
    danger: bool = False
    lock_reason: str = ""
    on_invalid: str = "default"
    #: Non-None for a switch whose *running* value is NOT read from the
    #: merged config `switch_value()` resolves from. `chat` and
    #: `chat_approvals` are the current examples: `app/main.py` boots them
    #: via `load_chat_config(DATA_DIR/state/instance.yaml)` — the writable
    #: server-config overlay file alone, never the static `config/
    #: instance.yaml` base `switch_value()` would also consult. The value
    #: here is the attribute name on `ChatConfig` holding the resolved flag
    #: (e.g. `"enabled"`, `"approvals_enabled"`).
    #:
    #: A switch that sets this must never be read through `switch_value()` —
    #: doing so would silently answer from the wrong source (it could return
    #: True for an instance that only set the flag in the static base, while
    #: the runtime it actually gates has it off). `switch_value()` raises
    #: rather than risk that; read the switch through its own runtime path
    #: instead (`app/api/admin.py::_chat_flag_runtime_view` for the two
    #: chat flags today).
    runtime_view: str | None = None


SWITCHES: tuple[Switch, ...] = (
    Switch(
        name="studio",
        config_keys=("studio", "enabled"),
        env_var="AGNES_STUDIO_ENABLED",
        kind="bool",
        default=True,
        effect="live",
        category="product",
        editable=True,
        description="Authoring Studio surface (/admin/studio*). Grandfathered on by default.",
    ),
    Switch(
        name="guardrails",
        config_keys=("guardrails", "enabled"),
        env_var="AGNES_GUARDRAILS_ENABLED",
        kind="bool",
        default=True,
        effect="live",
        category="product",
        editable=True,
        description="Flea-market upload LLM security-review pipeline. Grandfathered on by default.",
    ),
    Switch(
        name="chat_approvals",
        config_keys=("chat", "approvals_enabled"),
        env_var="AGNES_CHAT_APPROVALS_ENABLED",
        kind="bool",
        default=True,
        effect="restart",
        category="product",
        editable=True,
        runtime_view="approvals_enabled",
        description=(
            "Interactive approval prompts for ask-flagged chat tool calls. Off makes the "
            "sandbox gate deny instantly instead of waiting for a human."
        ),
    ),
    Switch(
        name="chat",
        config_keys=("chat", "enabled"),
        env_var="AGNES_CHAT_ENABLED",
        kind="bool",
        default=False,
        effect="restart",
        category="product",
        editable=True,
        runtime_view="enabled",
        description="Cloud-hosted chat (sandboxed agent sessions). New feature — off by default.",
    ),
    Switch(
        name="chat_bootstrap_marketplace",
        config_keys=("chat", "bootstrap_marketplace"),
        env_var="AGNES_CHAT_BOOTSTRAP_MARKETPLACE",
        kind="bool",
        default=True,
        effect="restart",
        category="product",
        editable=True,
        runtime_view="bootstrap_marketplace",
        description=(
            "Deliver the caller's RBAC-filtered marketplace skills into chat sessions so a "
            "stack skill is invokable as `/<skill-name>`. On `docker` the runner installs "
            "them as Claude Code plugins in the sandbox (~10-15 s per spawn); on `kai-agent` "
            "they ride the workspace tarball the engine materializes. Off makes the composer's "
            "slash menu stop offering marketplace skills rather than advertise ones the agent "
            "was never given. Resolved by `load_chat_config` (env > instance.yaml > default) at "
            "boot, not through `switch_value` — hence `runtime_view`."
        ),
    ),
    Switch(
        name="chat_provider",
        config_keys=("chat", "provider"),
        env_var="AGNES_CHAT_PROVIDER",
        kind="select",
        options=("docker", "kai-agent"),
        default="kai-agent",
        effect="restart",
        category="product",
        editable=True,
        runtime_view="provider",
        description=(
            "Which engine runs web/Slack chat sessions: `kai-agent` (the embedded kai-agent "
            "turn engine, the default — see docs/cloud-chat.md) or `docker` (self-hosted "
            "container per session). Resolved by `load_chat_config` "
            "(env > instance.yaml > default) at boot, not through `switch_value` — hence "
            "`runtime_view`. Editable because the whole `chat` section is (a raw section edit "
            "could always write it); the real guards sit elsewhere: every provider rides "
            "deployment-provisioned backing (kai-agent sidecar + KAI_HOST_JWT_SECRET, or the "
            "apps-runner sidecar), and app/main.py's boot gates refuse a provider "
            "whose backing is absent, loudly, at the restart the save already requires. Pin "
            "it in infrastructure via the customer-instance module's per-VM `chat_provider` "
            "(AGNES_CHAT_PROVIDER) so a fresh data disk boots into the right engine."
        ),
    ),
    Switch(
        name="data_apps",
        config_keys=("data_apps", "enabled"),
        env_var="AGNES_DATA_APPS_ENABLED",
        kind="bool",
        default=False,
        effect="live",
        category="product",
        editable=False,
        lock_reason=(
            "The flag itself is read per request, but the apps_runner sidecar sits behind "
            "the `apps` Compose profile — enabling it here would surface a feature whose "
            "backend is absent. Enable the profile and set AGNES_DATA_APPS_ENABLED together."
        ),
        description="Hosted user web apps (data apps). New feature — off by default.",
    ),
    Switch(
        name="data_apps_allow_same_origin",
        config_keys=("data_apps", "allow_same_origin"),
        env_var="AGNES_DATA_APPS_ALLOW_SAME_ORIGIN",
        kind="bool",
        default=False,
        effect="live",
        category="operations",
        editable=False,
        lock_reason=(
            "Deliberate security lock: serving hosted apps on the main origin hands every "
            "app's user-authored JS the viewer's own session (a same-origin read of /api "
            "that no response header can close). Accepting that is a deployment decision — "
            "set it in instance.yaml or AGNES_DATA_APPS_ALLOW_SAME_ORIGIN, next to the "
            "subdomain_base alternative that avoids it — not a live panel toggle. Its "
            "section is locked regardless (see data_apps)."
        ),
        description=(
            "Serve hosted data apps on the MAIN origin (same origin as the Agnes /api) for "
            "ALL apps and callers. Off by default: a hosted app's JS then shares the "
            "viewer's session and can read /api, so the supported isolation is "
            "data_apps.subdomain_base (per-app origins); requests arriving on a data-app "
            "subdomain are always served, and the in-chat preview works without this flag "
            "via its per-app data-app-preview:<slug> token. Turn on only when every app "
            "author is trusted with every viewer's session — see "
            "docs/architecture.md#hosted-data-apps."
        ),
    ),
    Switch(
        name="library_show_unverified_trust",
        config_keys=("library", "show_unverified_trust"),
        env_var="AGNES_LIBRARY_SHOW_UNVERIFIED_TRUST",
        kind="bool",
        default=True,
        effect="live",
        category="product",
        editable=True,
        description=(
            "Show the 'Community' trust marker for unverified Store items in the Library, so "
            "all three provenance levels (Organization / Verified / Community) are stated "
            "positively and no row is left silently unlabelled. Set false for the older silent "
            "reading, where an unverified item is marked by the ABSENCE of a marker."
        ),
    ),
    Switch(
        name="library_auto_share_admin_uploads",
        config_keys=("library", "auto_share_admin_uploads"),
        env_var="AGNES_LIBRARY_AUTO_SHARE_ADMIN_UPLOADS",
        kind="bool",
        default=False,
        effect="live",
        category="product",
        editable=True,
        description=(
            "Auto-share collections an admin creates in the Library to the Everyone group, so "
            "admin uploads are workspace-visible without a manual share step. Writes an ordinary "
            "Everyone grant — revocable per collection in the share dialog. Off by default so an "
            "upgrade never changes who sees data; scope is the Library/API creation path only "
            "(analyst uploads and chat file drops stay private)."
        ),
    ),
    Switch(
        name="experience",
        config_keys=("instance", "experience"),
        env_var="AGNES_INSTANCE_EXPERIENCE",
        kind="select",
        options=("redesign",),
        default="redesign",
        effect="live",
        category="product",
        editable=True,
        on_invalid="default",
        description=(
            "Experience preset — retired as a choice; `redesign` is now the only option and "
            "the default. The entry is kept (rather than removed outright) only so existing "
            "yaml/env values of `instance.experience` don't error — any value other than "
            "`redesign` (including the old `classic`) falls back to the default via "
            '`on_invalid="default"`. Still changes only the DEFAULTS of the coupled knobs '
            "(instance.theme → paper, features.stack_auto_membership → true); any per-knob "
            "env/yaml setting still wins. Chrome layout is NOT among them — the rail is "
            "unconditional, independent of this preset."
        ),
    ),
    Switch(
        name="stack_auto_membership",
        config_keys=("features", "stack_auto_membership"),
        env_var="AGNES_STACK_AUTO_MEMBERSHIP",
        kind="bool",
        default=False,
        effect="live",
        category="product",
        editable=True,
        description=(
            "Stack membership mode. On (auto-membership, the default since Wave 0, 2026-08 — "
            "`experience` is always `redesign` now): every granted resource is in the stack "
            "immediately; subscribe/unsubscribe only control the local copy. Off (classic — "
            "still a fully-supported explicit opt-out, and it always wins over the default): "
            "membership is the subscribe model — required plus subscribed grants — with the "
            "grant-downgrade subscription fan-out, exactly the pre-redesign behavior. "
            "Read per request, so subscriptions are interpreted, never rewritten. "
            "Set in instance.yaml it is cached per process: with role-split or several Uvicorn "
            "workers, saving it here reaches only the process that served the save — restart to "
            "flip the whole deployment, or set AGNES_STACK_AUTO_MEMBERSHIP, which is read fresh. "
            "That matters more here than for a cosmetic switch, because this one gates which "
            "data a user can reach."
        ),
    ),
    Switch(
        name="mcp_query_param_token",
        config_keys=("mcp", "allow_query_param_token"),
        env_var="AGNES_MCP_ALLOW_QUERY_PARAM_TOKEN",
        kind="bool",
        default=True,
        effect="live",
        category="product",
        editable=True,
        description=(
            "Accept the MCP bearer token as a ?token= query param on SSE GET, for clients "
            "that cannot set headers. On by default (grandfathered). The token lands in every "
            "request log when used (CWE-598) — turn this off if all your MCP clients send the "
            "Authorization header."
        ),
    ),
    Switch(
        name="kai_broker_mcp_enabled",
        config_keys=("kai", "broker_mcp_enabled"),
        env_var="KAI_BROKER_MCP_ENABLED",
        kind="bool",
        default=False,
        effect="live",
        category="operations",
        editable=False,
        lock_reason=(
            "Only meaningful on an instance that embeds the kai-agent turn engine, which is "
            "itself gated on the KAI_HOST_JWT_SECRET deployment secret — so the dependency this "
            "switch rides on is not something the admin UI can satisfy."
        ),
        description=(
            "Let the embedded kai-agent engine's sandbox reach this instance's own MCP server, by "
            "issuing the `kai_mcp` ticket scope alongside the `llm` one. Off by default: the engine "
            "then registers no host MCP server and runs on its built-in tools only. Turning it on "
            "gives the agent the caller's own tool surface — the tools that caller could invoke "
            "themselves, under their own stack and grants, never an admin's."
        ),
    ),
    Switch(
        name="mcp_source_url_strict",
        config_keys=("mcp", "source_url_strict"),
        env_var="AGNES_MCP_SOURCE_URL_STRICT",
        kind="bool",
        default=False,
        effect="live",
        category="operations",
        editable=True,
        description=(
            "Hold a registered MCP source's own url to the same bar as its OAuth endpoints: "
            "https to a public address. Off by default, which is not unguarded — the baseline "
            "always refuses link-local / metadata / multicast / reserved addresses and cleartext "
            "http to a public one. What the default permits is a source on an INTERNAL address "
            "(an organization's own tool server, a developer's localhost), because those are "
            "ordinary deployments. Turn on for instances that only ever talk to third-party MCP "
            "services; it makes an intranet source unconfigurable, which is why it is opt-in."
        ),
    ),
    Switch(
        name="mcp_session_pool",
        config_keys=("mcp", "session_pool"),
        env_var="AGNES_MCP_SESSION_POOL",
        kind="bool",
        default=True,
        effect="live",
        category="operations",
        editable=True,
        description=(
            "Keep a stdio MCP server's process warm between tool calls instead of starting "
            "one per call (~6 s of upstream import time each). Read per call, so a save "
            "applies to the next tool call; sessions already warm age out on their own. Turn "
            "off to go back to a process per call — the debugging shape, and the answer for "
            "an upstream that cannot survive being reused. Only stdio sources are affected; "
            "http/sse have no spawn to amortize."
        ),
    ),
    Switch(
        name="keboola_token_header",
        config_keys=("auth", "keboola", "allow_token_header"),
        env_var="AGNES_KEBOOLA_ALLOW_TOKEN_HEADER",
        kind="bool",
        default=False,
        effect="live",
        category="operations",
        editable=True,
        description=(
            "Accept a Keboola Storage API token in the X-StorageApi-Token header as API "
            "authentication. The token is verified against the configured stack per request "
            "(60s cache), must be a master token for the bound project (on a wildcard "
            "multi-project instance — project_id '*'/unset with multi_project_mode "
            "select/auto — for ANY project on the stack), and maps only to an "
            "EXISTING user — it never provisions accounts. Off by default: a plain Storage "
            "token carries no interactive factor, so enabling this bypasses any MFA/SSO the "
            "organization enforces on web logins. It also grants that user's full Agnes "
            "authority (PAT-equivalent): if the mapped user is an admin, admin mutation "
            "endpoints are reachable with the token — the narrowing is on the data-read "
            "surface (credential_surface='stack'), not the admin gate. Credential-minting "
            "endpoints (PAT/MCP/agent/data-app) are blocked regardless."
        ),
    ),
    Switch(
        name="keboola_multi_project_mode",
        config_keys=("auth", "keboola", "multi_project_mode"),
        env_var="AGNES_KEBOOLA_MULTI_PROJECT_MODE",
        kind="select",
        options=("disabled", "select", "auto"),
        default="disabled",
        effect="live",
        category="operations",
        editable=True,
        description=(
            "What a Keboola OAuth sign-in does with the OTHER projects the user can reach. "
            "'disabled' (default): the original single-project behavior — the login is gated "
            "on auth.keboola.project_id and nothing is provisioned (a 'single' value from "
            "older configs falls back here, same behavior). 'select': Agnes discovers the "
            "user's projects at login (introspect on the OAuth host, narrowed by "
            "allowed_roles) and stores the list for a user-driven import via "
            "/api/auth/keboola/projects. 'auto': trusted auto-provisioning — every allowed "
            "project is connected on each login (project-scoped PAT minted and vaulted, "
            "source connection + chat tools created, kbc-<project>-<role> group membership "
            "synced, semantic layer refreshed where the token is a master token). With "
            "auth.keboola.project_id set to '*' (or unset) the login gate itself widens to "
            "'any project the introspect lists with an allowed role'; a concrete project_id "
            "keeps the single-project gate and narrows discovery to that project. NOTE: on "
            "such a wildcard instance the widening applies to BOTH active modes (select and "
            "auto) and — when allow_token_header is also on — to X-StorageApi-Token API auth "
            "too: an existing user's master token from any project on the stack "
            "authenticates. CAUTION: a Keboola role is per-project and every user is admin "
            "of their own project, so on a shared multi-tenant stack the wildcard admits ANY "
            "user of the stack regardless of allowed_roles (which narrows projects, not "
            "organizations) — reserve the wildcard for dedicated single-organization stacks "
            "and pin a concrete project_id on shared ones."
        ),
    ),
    Switch(
        name="mcp_source_url_runtime_enforce",
        config_keys=("mcp", "source_url_runtime_enforce"),
        env_var="AGNES_MCP_SOURCE_URL_RUNTIME_ENFORCE",
        kind="bool",
        default=False,
        effect="live",
        category="operations",
        editable=True,
        description=(
            "Enforce the DNS-free half of the MCP source url policy (scheme + "
            "literal-IP checks) at the two runtime forward seams too, not only when a "
            "source is configured (#1216). Off by default: a source that is enabled "
            "and was registered before this policy existed, or before "
            "`mcp.source_url_strict` was turned on, keeps forwarding exactly as it "
            "does today. BEFORE turning this on, review the `url_policy_verdict` "
            "column on the admin MCP source list (GET /api/admin/mcp-sources or "
            "`agnes admin mcp source list`) for any `would_refuse` row and fix its "
            "url first — this switch converts each one from a silent warning into a "
            "refused call the next time that tool is invoked, with no other notice."
        ),
    ),
    Switch(
        name="mcp_connector_ui",
        config_keys=("mcp", "connector_ui_enabled"),
        env_var="AGNES_MCP_CONNECTOR_UI_ENABLED",
        kind="bool",
        default=True,
        effect="live",
        category="product",
        editable=True,
        description=(
            "User-facing MCP connector surface: the /me/ai-connector and /mcp-connect "
            "install-instruction pages, the MCP tab of /how-it-works#connect, and their nav / "
            "command-palette entries. On by default (current behavior unchanged). Turn off on a "
            "VPN/intranet-only instance where cloud-side MCP clients (e.g. a hosted connector "
            "resolved from outside the network) can never reach the endpoint — so users are not "
            "shown a setup path that cannot work for them. This hides UI ONLY: the MCP protocol "
            "endpoints (/api/mcp/http, /api/mcp/sse) keep serving in-network clients regardless "
            "of this switch."
        ),
    ),
    Switch(
        name="agent_profiles",
        config_keys=("agent_profiles", "enabled"),
        env_var="AGNES_AGENT_PROFILES_ENABLED",
        kind="bool",
        default=True,
        effect="restart",
        category="product",
        editable=False,
        lock_reason=(
            "Deliberately env-var-only kill switch — no runtime-toggle use case identified. "
            "Flip via AGNES_AGENT_PROFILES_ENABLED (or the static instance.yaml) and restart."
        ),
        description=(
            "Agent profiles surface — /agents builder, /api/v1/agents* management + "
            "runtime API, `agnes agent`/`agnes chat` CLI. Grandfathered on by default; "
            "an instance opts out via AGNES_AGENT_PROFILES_ENABLED=0."
        ),
    ),
    Switch(
        name="access_policies",
        config_keys=("access_policies", "enabled"),
        env_var="AGNES_ACCESS_POLICIES_ENABLED",
        kind="bool",
        default=False,
        effect="live",
        category="product",
        editable=True,
        description=(
            "Table access policies — lets an admin attach one SQL policy per "
            "non-distributed table (query_mode='remote' or server_only=true), "
            "substituted for it on every server-side read with the caller's identity "
            "bound in ($user_email/$user_id/$user_groups), filtering rows and masking "
            "columns. Gates ATTACHING a policy (PUT /api/admin/registry/{id}'s setter) "
            "only — a table that already carries one stays protected regardless of this "
            "flag. New feature — off by default."
        ),
    ),
)

_BY_NAME: dict[str, Switch] = {s.name: s for s in SWITCHES}


def get_switch(name: str) -> Switch:
    """The registry entry, or `KeyError` if there is none.

    Deliberately strict: a typo'd switch name is a programming error, and a
    silent `None` would resolve as "off" at the callsite.
    """
    return _BY_NAME[name]


def switch_value(name: str) -> Any:
    """Resolve a switch to its effective value.

    Order, identical for every switch and unchanged from the convention
    `feature_enabled` established:

        env var  >  server-config overlay  >  instance.yaml base  >  default

    The middle two collapse into one step: `config/loader.py` deep-merges the
    writable admin overlay over the static base at load time, so `get_value`
    already returns the fully-resolved value.

    `on_invalid` decides what an unrecognized `select` token does — fall back
    to the default (the common case) or raise (`analytics.backend`, where a
    typo must fail loudly at boot rather than silently pick a backend).

    Raises `ValueError` for a switch that declares `runtime_view`: its
    running value does not come from the merged config this function reads,
    so answering from here would be silently wrong rather than merely
    unavailable. See `Switch.runtime_view`.
    """
    switch = get_switch(name)

    if switch.runtime_view:
        raise ValueError(
            f"switch_value({name!r}): this switch's runtime does not read the merged "
            "config switch_value() resolves from — it reads the writable server-config "
            "overlay file ALONE, via app.chat.config.load_chat_config. Calling "
            "switch_value() here would silently return the wrong value for an instance "
            "that only set it in the static instance.yaml base. Read it through "
            "app/api/admin.py::_chat_flag_runtime_view (or the switch's own runtime "
            "read site) instead."
        )

    # Local import: `app.instance_config` imports this module, so a
    # module-level import here would be circular. Precedent:
    # `src/analytics_backend.py::resolve_analytics_backend_name`.
    import os

    from app.instance_config import coerce_flag_value, get_value

    raw: Any = None
    if switch.env_var:
        raw = os.environ.get(switch.env_var)
    if raw is None and switch.config_keys:
        raw = get_value(*switch.config_keys, default=None)
    if raw is None:
        return switch.default

    if switch.kind == "bool":
        return coerce_flag_value(raw, switch.default)

    if switch.kind == "int":
        try:
            return int(raw)
        except (TypeError, ValueError):
            return switch.default

    value = str(raw).strip().lower()
    if switch.kind == "select" and value not in switch.options:
        if switch.on_invalid == "raise":
            raise ValueError(
                f"invalid value {value!r} for switch {switch.name!r}; expected one of {', '.join(switch.options)}"
            )
        return switch.default
    return value
