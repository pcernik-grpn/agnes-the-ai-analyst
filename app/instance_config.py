"""Instance configuration — loads instance.yaml and exposes to FastAPI."""

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)


class InstanceConfigUnreadable(RuntimeError):
    """The overlay exists but cannot be read.

    A distinct type rather than a bare ``RuntimeError`` because the boot path
    has to be able to tell it apart. ``app/main.py`` deliberately wraps its
    startup config load in ``except Exception`` so that a *soft* config
    problem does not stop an instance from serving — which would have
    swallowed this one too, leaving the process up and 500ing every
    ``get_value()`` consumer instead of refusing to start. That is the worse
    of the two failure modes: it looks healthy from the outside. So the boot
    path re-raises this type specifically and keeps logging everything else.
    """


_instance_config: Optional[dict] = None
# True once this process has successfully built a config. Gates the
# unreadable-overlay refusal to startup — see `load_instance_config`.
_loaded_once: bool = False
# The last config this process built successfully. Survives `reset_cache()`
# on purpose: it is what a live instance falls back to when the overlay
# stops being readable, and it is the only thing that HAS the operator's
# sections at that point — `_instance_config` is None by then, since
# `load_instance_config` returns early whenever it is not.
_last_good_config: Optional[dict] = None

# Why the static `CONFIG_DIR/instance.yaml` could not be loaded, if it could
# not. A validation failure there is NOT fatal — the app boots on built-in
# defaults — which is a footgun: one typo'd key and the instance runs under
# the wrong name, with the wrong data source, and the only evidence is a log
# line. `get_static_config_error()` exposes this for the admin UI to surface
# — not wired up there yet.
_static_config_error: Optional[str] = None

# Keys already warned about once this process — see `_warn_once`. Process
# lifetime, like `_loaded_once` above: a retired config value should log a
# single line at boot, not spam every request that calls the resolver.
_warned_once_keys: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    """Log ``message`` via the module logger the first time this process
    sees ``key``; every later call with the same ``key`` is a no-op.

    Used by resolvers for retired config values (e.g. :func:`get_ui_layout`)
    so a stale ``instance.yaml``/env setting produces one clear warning
    instead of flooding the log on every request."""
    if key in _warned_once_keys:
        return
    _warned_once_keys.add(key)
    logger.warning(message)


def get_static_config_error() -> Optional[str]:
    """Reason the static ``instance.yaml`` failed to load, or ``None``.

    Non-``None`` means the running instance is serving built-in defaults for
    every section the writable overlay does not cover.

    This is a diagnostic accessor, so it must never itself raise — including
    on the process state it exists to explain. ``load_instance_config()``'s
    lenient fallback for an unreadable *overlay* only fires once a good
    config has loaded at least once (``_last_good_config is not None``); on
    a process where the overlay was unreadable from the very first call, it
    raises ``InstanceConfigUnreadable`` instead of falling back. Catch that
    (and any other loader failure) and report it the same way as a static-
    config error rather than propagating — an admin-UI caller of this
    accessor must get a string back on precisely the misconfigured instance
    the page exists to explain.
    """
    try:
        load_instance_config()
    except InstanceConfigUnreadable as exc:
        return str(exc)
    return _static_config_error


def reset_cache() -> None:
    """Drop the in-process instance.yaml cache; the next ``load_instance_config``
    call re-reads from disk. Used by `/api/admin/server-config` after a save.
    Public alias so callers don't have to reach into the private global.

    Also clears ``connectors.bigquery.access.get_bq_access`` so the v2 endpoints
    pick up new BigQuery project IDs after an admin saves `instance.yaml` —
    without this, `get_bq_access`'s `@functools.cache` would freeze the projects
    at first call and require a container restart to pick up changes (Devin
    ANALYSIS_0004 on PR #138). Lazy-imported so this module stays usable in
    environments where the connectors package can't be imported (e.g. unit
    tests of instance_config in isolation)."""
    global _instance_config
    _instance_config = None
    # `_loaded_once` is deliberately NOT reset: it records that this process
    # got a good config at least once, which is what separates "refuse to
    # start" from "do not 500 every live request". This function runs on a
    # live instance after an admin save, and that must not re-arm a
    # boot-time guard.
    try:
        from connectors.bigquery.access import get_bq_access

        get_bq_access.cache_clear()
    except Exception:
        # Connectors module not loaded yet, or BQ deps missing — both fine.
        pass


def get_database_config() -> dict:
    """Return ``{backend: "...", url: "..."}`` from the state machine.

    Centralised so future callers don't reach into src.db_state_machine
    directly. Cache invalidation via reset_database_cache() after
    /api/admin/db/migrate success.
    """
    from src.db_state_machine import read_backend_state

    state, url = read_backend_state()
    return {"backend": state.value, "url": url}


def reset_database_cache() -> None:
    """Drop the in-process backend-state cache.

    Clears the parse-once memoization of the ``instance.yaml`` overlay in
    ``src.db_state_machine``, so the next ``read_backend_state`` /
    ``use_pg`` re-reads from disk. Production backend flips restart the app,
    so runtime correctness gets a fresh process; same-process writes are
    already invalidated inside ``write_backend_state``. This public hook
    exists for tests and any other manual same-process caller.
    """
    from src.db_state_machine import reset_backend_state_cache

    reset_backend_state_cache()


def _deep_merge(base: dict, patch: dict) -> dict:
    """Deep-merge `patch` into `base`, returning a new dict.

    Dict-into-dict recurses; everything else (scalars, lists, None) is
    replaced wholesale. Used so the writable overlay can hold only the
    sections an operator has touched, while everything else flows from
    the static file unchanged. Same semantics as the helper in
    `/api/admin/server-config`'s POST handler.
    """
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_instance_config(*, strict: bool = False) -> dict:
    """Load instance.yaml as a deep-merge of the static file and the
    writable overlay.

    Resolution:
    1. Static base: ``CONFIG_DIR/instance.yaml`` via ``config.loader``
       (the source of truth for sections the editor doesn't expose —
       ``datasets``, ``corporate_memory``, etc.).
    2. Overlay patch: ``DATA_DIR/state/instance.yaml`` (written by
       ``/api/admin/configure`` and ``/api/admin/server-config``;
       contains only the sections those endpoints accept).
    3. Overlay wins per-leaf via deep-merge — operator edits persist,
       static-only sections still flow through.

    Pre-2026-04-28 this function returned the overlay verbatim when it
    existed and only fell back to static when it didn't. That was a
    silent footgun: the moment someone saved any section through the
    new editor (which writes a narrow overlay by design), every
    consumer of static-only sections (corporate memory page, dataset
    list) saw empty defaults. See PR #107.
    """
    global _instance_config, _loaded_once, _last_good_config, _static_config_error
    if _instance_config is not None:
        return _instance_config

    import yaml

    # Static base — strict validation lives in config.loader.
    base: dict = {}
    try:
        from config.loader import load_instance_config as _load

        base = _load() or {}
        _static_config_error = None
        logger.info("Loaded instance.yaml base from config/")
    except Exception as e:
        # ERROR, not WARNING: the app keeps serving, but on built-in
        # defaults — wrong instance name, wrong data source, wrong auth
        # domain. That is a misconfigured instance, not a hiccup, and it
        # is invisible in the UI unless we say so (see
        # `get_static_config_error`, which exists for the admin UI to
        # surface on /admin/server-config — not wired up there yet).
        _static_config_error = str(e)
        logger.error(
            "Could not load static instance.yaml — serving BUILT-IN DEFAULTS for "
            "every section the writable overlay does not cover: %s",
            e,
        )

    # Overlay patch from the writable volume. Best-effort — a corrupt
    # overlay shouldn't take the app offline (we'd rather serve stale/base
    # config than 500 every request), but log loudly with a traceback so
    # the corruption surfaces in the operator's logs immediately. The
    # write-side endpoints (POST /api/admin/server-config and /configure)
    # refuse to overwrite a corrupt overlay with HTTP 500, so an admin
    # noticing the saves break is the second line of defence.
    #
    # ${ENV_VAR} interpolation: ``config.loader.load_instance_config`` runs
    # the static base through ``_resolve_env_refs`` already, but raw
    # ``yaml.safe_load`` here would leave overlay strings like
    # ``${ANTHROPIC_API_KEY}`` as literal placeholders. /api/admin/configure
    # writes exactly that string into the seeded ai: block (#176), so we
    # mirror the resolver here before the deep-merge — without it, the
    # LLM factory receives the literal placeholder and rejects it as an
    # invalid api key (#179 review fix).
    # Resolve via _state_dir() so the path matches the writer in
    # app/api/admin.py — under the flat-mount layout (STATE_DIR=/data-state)
    # both the configure-endpoint and the server-config-endpoint write
    # ``/data-state/instance.yaml``; reading from ``/data/state/...`` here
    # would silently load stale config from the regenerable data disk.
    from app.secrets import _state_dir

    overlay_path = _state_dir() / "instance.yaml"
    if overlay_path.exists():
        try:
            raw = overlay_path.read_text()
        except OSError as exc:
            # The file is THERE and we cannot read it, which since 0600 is
            # reachable through a plain uid mismatch rather than only through
            # disk failure.
            #
            # What this refusal is FOR, stated precisely — an earlier version
            # of this comment claimed it stops an instance whose data is on
            # Postgres from coming up on the DuckDB default, and that is not
            # the mechanism. The backend is not resolved through here: it
            # comes from `src.db_state_machine.read_backend_state`, which
            # reads the overlay directly and catches only `yaml.YAMLError`, so
            # a PermissionError propagates out of `use_pg()` rather than
            # quietly answering DuckDB. Do not relax that handler on the
            # belief that this function guards it.
            #
            # The value here is narrower and still worth having: everything
            # ELSE the overlay carries (auth providers, feature flags, theme,
            # `initial_workspace`) would silently revert to its static value,
            # and the failure would otherwise surface as a raw OSError from
            # deep inside a repository factory on some unrelated request. This
            # names the cause at the one moment an operator is watching.
            #
            # A malformed file keeps the lenient path below: that one is
            # visible to the operator and repairable through the editor.
            #
            # Boot only. `load_instance_config` is reached from `get_value()`,
            # i.e. from essentially every request path, and `reset_cache()`
            # re-runs it on a live instance after an admin save. If the file
            # became unreadable AFTER a good boot — an operator chown, a
            # half-finished manual repair — raising here would turn every
            # subsequent request into a 500 where it used to degrade. That is
            # not the trade being made: the danger this guards against is
            # *starting* against the wrong `database.backend`, and a process
            # that already loaded a good config is not about to do that. So it
            # refuses only when nothing good has ever been loaded, and
            # otherwise keeps serving on the config it has, loudly.
            if not strict and _last_good_config is not None:
                # `_last_good_config`, NOT `_instance_config` — the latter is
                # guaranteed None here, because the function returns early
                # whenever it is set, so `reset_cache()` (which every admin
                # save calls) is exactly the path that reaches this branch.
                # Returning the static base instead would drop every
                # operator-set section while the log claimed they were kept:
                # the silent-wrong-config outcome the refusal exists to
                # prevent, just after boot instead of at it.
                #
                # `strict` is the CALLER's declaration, not a guess from
                # process history. It used to be `_loaded_once`, a module flag
                # meant to mean "we are past startup" — and importing
                # `app.main` sets it, so by the time the startup block ran the
                # refusal was already permanently defused. A guard whose arming
                # depends on nothing else having imported first is not a guard.
                #
                # Cached into `_instance_config` so the next request short-
                # circuits. Without that, every `get_value()` re-reads and
                # re-parses the static YAML and emits another ERROR line — a
                # log storm on top of a config problem.
                logger.error(
                    "instance.yaml overlay at %s became unreadable after startup (%s) — "
                    "serving the last good config; saves through the editor will refuse "
                    "until the file is readable again",
                    overlay_path,
                    exc,
                )
                _instance_config = _last_good_config
                return _instance_config
            raise InstanceConfigUnreadable(
                f"cannot read the instance.yaml overlay at {overlay_path}: {exc}. "
                "The file exists but is not readable by this process — it is mode 0600, "
                "so check that the app runs as its owner. Refusing to start on the static "
                "base config, which would silently use a different `database.backend` than "
                "the one this instance's data is on."
            ) from exc
        except Exception:
            # NOT unreadable — undecodable. `Path.read_text()` raises
            # UnicodeDecodeError (a ValueError) for bytes that are not valid
            # UTF-8, which is the partial-write / disk-corruption shape the
            # lenient path was written for in the first place. Leaving it to
            # propagate would recreate the very failure this split exists to
            # prevent, one exception type over: the process starts, the boot
            # path's broad `except` logs it, `_instance_config` is never
            # assigned, and every later `get_value()` re-raises — an instance
            # that looks healthy and 500s on everything. So it degrades to the
            # base config like any other malformed file.
            logger.exception(
                "instance.yaml overlay at %s could not be decoded — falling back to "
                "static base config; saves through the editor will refuse until the "
                "file is repaired",
                overlay_path,
            )
            raw = None
        try:
            overlay = yaml.safe_load(raw or "") or {}
            from config.loader import _resolve_env_refs

            overlay = _resolve_env_refs(overlay)
            base = _deep_merge(base, overlay)
            logger.info("Merged overlay from %s", overlay_path)
        except Exception:
            logger.exception(
                "instance.yaml overlay at %s is corrupt — falling back to "
                "static base config; saves through the editor will refuse "
                "until the file is repaired",
                overlay_path,
            )

    _instance_config = base
    _last_good_config = base
    _loaded_once = True
    return _instance_config


def get_value(*keys, default=None) -> Any:
    """Get nested value from instance config."""
    config = load_instance_config()
    current = config
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return default
        if current is None:
            return default
    return current


def coerce_flag_value(raw: Any, default: bool = False) -> bool:
    """Shared truthy-parsing convention for every boolean config/env value in
    Agnes (mirrors the inline checks in :func:`get_studio_enabled`,
    :func:`get_home_automode_visibility`, etc.) — factored out so
    :func:`feature_enabled` and any raw-value caller (e.g.
    ``app.chat.config.load_chat_config``, whose ``chat.enabled`` yaml source
    is a caller-supplied path rather than :func:`get_value`'s global merged
    config) share one rule instead of re-deriving it.

    ``None`` -> ``default``. Bools pass through unchanged. Everything else
    is stringified and lowercased: only ``"0"``, ``"false"``, ``"no"``,
    ``"off"``, and ``""`` are false — every other string (including an
    unrecognized typo) is true, so a truthy operator intent never silently
    degrades to disabled.
    """
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "")


def feature_enabled(*keys: str, env_var: Optional[str] = None, default: bool = False) -> bool:
    """Canonical feature-flag resolution used across Agnes (#1022).

    Resolution order: ``env_var`` (if given and present in ``os.environ``) >
    ``get_value(*keys)`` (the ``instance.yaml`` static base deep-merged with
    the ``/admin/server-config`` writable overlay — see
    :func:`load_instance_config`) > ``default``.

    Every boolean value is parsed with :func:`coerce_flag_value`, the same
    truthy-string convention used elsewhere in this module. See
    ``docs/feature-flags.md`` for the convention this backs and
    :data:`FEATURE_FLAGS` for the registry of flags that route through it.
    """
    if env_var:
        raw_env = os.environ.get(env_var)
        if raw_env is not None:
            return coerce_flag_value(raw_env, default)
    return coerce_flag_value(get_value(*keys, default=default), default)


# The switch registry moved to `app/switches.py` so that a switch's type,
# effect class, editability and the reason for it live in one declaration.
# Re-exported under the old name because `app/web/router.py` and
# `app/api/admin.py` import it from here. `Switch` is a superset of the old
# `FeatureFlag`, so every attribute those callers read still resolves.
from app.switches import SWITCHES as FEATURE_FLAGS  # noqa: E402,F401
from app.switches import Switch as FeatureFlag  # noqa: E402,F401


def get_data_source_type() -> str:
    """Primary connector type for this instance (``keboola``/``bigquery``/``local``).

    Resolution order: ``data_source.type`` in ``instance.yaml`` (the overlay
    ``/admin/server-config`` writes) > ``DATA_SOURCE`` env var > default
    ``"local"``. This is the ONE deliberate exception to this module's usual
    env-wins-everything rule (see docs/CONFIGURATION.md) — D1 residual
    (2026-08): the customer-instance Terraform module used to write an
    always-wins ``DATA_SOURCE=...`` line into ``.env`` on every boot, which
    permanently shadowed an admin's UI edit of the same knob. Provisioning no
    longer writes that line (it seeds ``data_source.type`` into the
    first-boot-only ``instance.yaml`` instead — see
    ``infra/modules/customer-instance``), but flipping precedence here is
    what makes a UI edit take effect even on a VM whose on-disk ``.env`` (or
    a running container's already-baked env) still carries a stale
    ``DATA_SOURCE`` line from before this change. ``DATA_SOURCE`` remains
    consulted when the overlay has no value at all, so the local-dev
    convenience of setting it in a laptop ``.env`` with no instance.yaml
    overlay keeps working.
    """
    overlay = get_value("data_source", "type", default="")
    if overlay:
        return str(overlay)
    return os.environ.get("DATA_SOURCE") or "local"


def get_slack_transport() -> str:
    """Inbound Slack transport for this instance: "http" (default) | "socket".

    Resolution: ``SLACK_TRANSPORT`` env (Terraform-friendly, overrides
    everything) > ``chat.slack.transport`` in instance.yaml > default
    ``"http"``. Unknown values fall back to ``"http"`` so a typo never
    starts a dead Socket Mode WS. Mirrors :func:`get_data_source_type`.
    """
    raw = os.environ.get("SLACK_TRANSPORT") or get_value("chat", "slack", "transport", default="http")
    value = (raw or "http").strip().lower()
    if value not in ("http", "socket"):
        return "http"
    return value


def get_home_route() -> str:
    """Path that ``/`` redirects to for an authenticated user.

    Resolution order: ``AGNES_HOME_ROUTE`` env var (Terraform-friendly,
    overrides everything) > ``instance.home_route`` in instance.yaml >
    default ``/dashboard``. The env-overrides-yaml shape mirrors
    :func:`get_data_source_type` (precedent in this file) so operators
    can flip a fork to ``/home`` per-deployment without forking the
    YAML.

    Validated to start with ``/`` and not ``//`` so a misconfigured
    value can't pivot the root redirect to an external host.

    Retired routes are coerced to the default: ``/ask`` (#896) now 302s to
    ``/``, so an instance that had pinned ``home_route: /ask`` would send
    ``/`` -> ``/ask`` -> ``/`` in an infinite loop. Falling back to
    ``/dashboard`` keeps such configs working — on the rail layout the
    dashboard itself forwards to the working chat or My Stack.
    """
    raw = os.environ.get("AGNES_HOME_ROUTE") or get_value("instance", "home_route", default="/dashboard")
    route = (raw or "").strip()
    if not route.startswith("/") or route.startswith("//"):
        return "/dashboard"
    if route == "/ask":
        return "/dashboard"
    return route


def get_public_url() -> str:
    """Absolute base URL of this instance (scheme + host, no trailing slash).

    Resolution order: ``PUBLIC_URL`` env var (Terraform-friendly, overrides
    everything) > ``server.public_url`` in instance.yaml > ``""`` (unset).
    The env-overrides-yaml shape mirrors :func:`get_home_route`.

    Needed by surfaces that have no inbound HTTP request to derive the host
    from — notably the Slack bot, which runs over Socket Mode and must mint
    *absolute* ``/slack/bind`` magic links and ``/chat`` deep links. Callers
    fall back to a root-relative path when this is empty, so an unset value
    degrades gracefully rather than crashing.
    """
    raw = os.environ.get("PUBLIC_URL") or get_value("server", "public_url", default="")
    return (raw or "").strip().rstrip("/")


def get_gws_oauth_credentials() -> dict:
    """Pre-configured Google Workspace CLI OAuth client (client_id + secret).

    Consumer: the server-resolved GWS fallback in ``GET
    /api/connectors/params`` (:func:`app.api.connectors.get_params`).
    When this resolves to a configured client, the fallback merges the
    equivalent ``connector-gws`` params — client id, secret value,
    optional project id — into the response, `agnes init` writes them
    into the analyst's ``.claude/agnes/.env``, and the connector-gws
    seed skill skips the "create your own GCP project" walkthrough.
    When unset, the skill falls back to its manual setup branch.

    OAuth client_id + secret here are app identifiers for an installed
    "Desktop app" OAuth client, not a per-user secret — the redirect-URI
    / scope guardrails on the GCP-side OAuth client are what enforce
    safety. Treat them like a publishable bundle ID, not a credential.

    Resolution order (env-overrides-yaml, mirrors :func:`get_home_route`):

    - ``AGNES_GWS_CLIENT_ID`` env > ``instance.gws.client_id`` YAML > None
    - ``AGNES_GWS_CLIENT_SECRET`` env > ``instance.gws.client_secret`` YAML > None
    - ``AGNES_GWS_OAUTHLIB_INSECURE_TRANSPORT`` env > ``instance.gws.oauthlib_insecure_transport`` YAML > "1"
      (kept as "1" by default because the gws CLI binds an HTTP loopback
       on 127.0.0.1:8080 for the OAuth redirect, and Google's oauthlib
       refuses non-HTTPS redirects without this flag).

    Both id and secret must be set for the configured branch to engage;
    a half-configured instance falls back to manual setup with a warning.
    """
    # Resolution order: env > vault (admin UI) > instance.yaml > ""
    # Lazy import keeps app.datasource_secrets out of the module-level import
    # graph (avoids circular import via src.repositories at startup).
    try:
        from app.datasource_secrets import datasource_secret as _ds_secret
    except Exception:
        _ds_secret = lambda _n: None  # noqa: E731

    cid = (
        os.environ.get("AGNES_GWS_CLIENT_ID")
        or _ds_secret("AGNES_GWS_CLIENT_ID")
        or get_value("instance", "gws", "client_id", default="")
    )
    secret = (
        os.environ.get("AGNES_GWS_CLIENT_SECRET")
        or _ds_secret("AGNES_GWS_CLIENT_SECRET")
        or get_value("instance", "gws", "client_secret", default="")
    )
    insecure = os.environ.get("AGNES_GWS_OAUTHLIB_INSECURE_TRANSPORT") or get_value(
        "instance", "gws", "oauthlib_insecure_transport", default="1"
    )
    project_id = os.environ.get("AGNES_GWS_PROJECT_ID") or get_value("instance", "gws", "project_id", default="")
    cid = (cid or "").strip()
    secret = (secret or "").strip()
    project_id = (project_id or "").strip()
    # Derive project_id from the client_id when not explicitly set. Google's
    # OAuth client_id format is "<numeric-project-number>-<random>.apps.
    # googleusercontent.com"; the numeric prefix is required by the
    # client_secret.json schema (gws CLI's Rust struct treats it as
    # non-Option). Falls back to "" when the client_id is empty or
    # malformed; the configured branch in the template degrades gracefully.
    if not project_id and cid and "-" in cid:
        project_id = cid.split("-", 1)[0]
    return {
        "client_id": cid,
        "client_secret": secret,
        "project_id": project_id,
        "oauthlib_insecure_transport": str(insecure).strip() or "1",
        "configured": bool(cid and secret),
    }


def get_experience() -> str:
    """One-line adoption preset (spec 2026-08-07-default-chrome-ux-parity).

    Retired as a choice in Wave 0 (2026-08, spec
    2026-08-13-grounded-analyst-workspace-design): ``redesign`` is the only
    option and the default. The old ``classic`` value — or any other
    unrecognised value — resolves to ``redesign`` via the ``experience``
    switch's ``on_invalid="default"``, so an older instance.yaml with
    ``experience: classic`` still boots without error; it no longer changes
    any behavior.

    Still changes only the DEFAULTS the remaining experience-coupled knobs
    fall back to (theme, stack membership mode); any per-knob env/yaml
    setting still wins, and each knob's own resolution order is unchanged.
    Chrome layout is no longer part of the coupling — :func:`get_ui_layout`
    hard-wires the rail chrome unconditionally, independent of this preset.

    Resolution: ``AGNES_INSTANCE_EXPERIENCE`` env > ``instance.experience``
    in instance.yaml > ``"redesign"`` — delegated to the ``experience`` entry
    in :data:`app.switches.SWITCHES` (kind ``select``,
    ``on_invalid="default"``), so the preset rides the same registry every
    operator switch is derived from instead of a hand-rolled lookup pair.
    """
    from app.switches import switch_value

    # Warn on a retired value, the same way :func:`get_ui_layout` does. The
    # switch's own `on_invalid="default"` resolves `classic` silently — which
    # is right for BOOTING (an old instance.yaml must not fail) but wrong for
    # telling the operator, who otherwise gets no signal anywhere that the
    # line in their config stopped meaning anything. Read raw rather than
    # through the switch, since the switch has already normalised it away.
    raw = os.environ.get("AGNES_INSTANCE_EXPERIENCE") or get_value("instance", "experience")
    if isinstance(raw, str) and raw.strip() and raw.strip().lower() != "redesign":
        _warn_once(
            "experience",
            f"instance.experience={raw!r} is retired; the redesign experience is always on",
        )

    return switch_value("experience")


#: Feature flags whose DEFAULT follows the ``instance.experience`` preset.
#: The admin flag inventory uses this to resolve honestly (and label the
#: source ``preset``); keep in step with ``preset_flag_default`` below.
PRESET_COUPLED_FLAGS: frozenset[str] = frozenset({"stack_auto_membership"})


def preset_knob_default(name: str) -> str:
    """Preset-implied default for the experience-coupled STRING knobs — the
    single source of the preset mapping, shared by the runtime getters and
    the ``/admin/server-config`` known-fields resolver (so the editable
    panel can never render a default the runtime doesn't use — Devin Review
    on #1199).

    ``theme`` is the only knob left here: ``ui_layout`` used to be
    preset-coupled too, but Wave 0 (2026-08) hard-wired the rail chrome —
    :func:`get_ui_layout` always returns ``"rail"`` unconditionally now, so
    there is no preset-implied default left to resolve for it."""
    redesign = get_experience() == "redesign"
    return {
        "theme": "paper" if redesign else "blue",
    }[name]


def preset_flag_default(name: str) -> bool:
    """Preset-implied default for the experience-coupled feature flags.

    Callsites pass this as ``feature_enabled``'s ``default`` so the knob's
    own env/yaml still wins; the ``FEATURE_FLAGS`` registry keeps a static
    ``default`` for display, with the description naming the preset.

    Deliberately NOT preset-coupled: ``store.verification_enabled`` (needs a
    reviewer, not a theme) and ``library_show_unverified_trust`` (the whole
    trust vocabulary is already gated to the paper theme at every ``mark()``
    callsite, so default-chrome parity comes from the theme gate itself —
    coupling the flag would only make the inventory disagree with the
    runtime's registry-sourced default).
    """
    redesign = get_experience() == "redesign"
    return {
        "stack_auto_membership": redesign,
    }.get(name, False)


def get_stack_auto_membership() -> bool:
    """Stack membership mode (spec 2026-08-07-default-chrome-ux-parity).

    ``True`` (the default since Wave 0, 2026-08 — the ``redesign``
    experience preset, now the only preset, implies this): auto-membership
    — every granted resource is in the stack the moment it's granted;
    subscribe/unsubscribe only control the local copy.
    ``False`` (the classic value — still a fully-supported explicit
    opt-out, and it always wins over the default): membership is the
    subscribe model — ``required ∪ (subscribed ∩ available)`` — exactly the
    pre-redesign behavior, including the grant-downgrade subscription
    fan-out.
    """
    return feature_enabled(
        "features",
        "stack_auto_membership",
        env_var="AGNES_STACK_AUTO_MEMBERSHIP",
        default=preset_flag_default("stack_auto_membership"),
    )


def get_instance_theme() -> str:
    """Active UI theme for this instance — drives the `data-theme`
    attribute on `<html>` so the design-system token set
    (`--ds-*`) flips between palettes without touching markup.

    Values:
      - ``paper``  — prototype-derived light look (issue #896), the
                     default since Wave 0 (2026-08): warm paper canvas,
                     white panels, emerald accent, pill CTAs; see
                     ``[data-theme="paper"]`` in ``design-tokens.css``.
                     Renders on the rail chrome (see :func:`get_ui_layout`,
                     the only chrome there is now).
      - ``blue``   — pre-redesign palette: brand-blue hero gradient,
                     blue CTAs, translucent-white eyebrow. Still fully
                     supported; set explicitly to opt out of ``paper``.
      - ``navy``   — darker palette opted into via server config.
                     Dark navy hero gradient, mint-green CTAs +
                     eyebrow accents.
      - ``dark``   — full dark surface palette (navy-tinted dark
                     stack, pale-ink text); see ``[data-theme="dark"]``
                     in ``design-tokens.css``.
      - ``auto``   — light by default, flips to the ``dark`` palette
                     when the user's OS prefers dark (resolved
                     client-side in ``_theme_resolve.html``).

    Resolution: ``AGNES_INSTANCE_THEME`` env var (Terraform-friendly) >
    ``instance.theme`` in instance.yaml > the ``experience`` preset's
    implied default (``"paper"`` — see :func:`get_experience`).
    Unrecognised values fall back to that same preset-implied default so a
    typo doesn't silently break every page.
    """
    # Preset-implied default (spec 2026-08-07): the `redesign` experience
    # defaults to paper; explicit env/yaml always wins.
    preset_default = preset_knob_default("theme")
    raw = os.environ.get("AGNES_INSTANCE_THEME")
    if raw is None:
        raw = get_value("instance", "theme", default=preset_default)
    if not isinstance(raw, str):
        return preset_default
    value = raw.strip().lower()
    if value not in ("navy", "blue", "dark", "auto", "paper"):
        return preset_default
    return value


def get_ui_layout() -> str:
    """Structural chrome layout — always ``"rail"`` since the classic
    topnav chrome was retired (Wave 0, 2026-08). The function stays so
    template context + config surface keep one source of truth; a
    configured ``AGNES_UI_LAYOUT``/``instance.ui_layout`` is ignored
    (warned once) rather than an error, so old instance.yaml files boot."""
    raw = os.environ.get("AGNES_UI_LAYOUT") or get_value("instance", "ui_layout")
    if raw and raw != "rail":
        _warn_once("ui_layout", f"instance.ui_layout={raw!r} is retired; rail chrome is always on")
    return "rail"


def get_home_automode_visibility() -> bool:
    """Whether /home renders the "Step 3 — turn on auto-accept mode"
    install-block. /home recommends launching with
    `claude --permission-mode auto`, whose classifier auto-approves
    safe actions (file edits and safe Bash) so the setup script runs
    mostly unattended while riskier commands can still prompt. The
    broader-blast-radius YOLO flag (`--dangerously-skip-permissions`)
    is no longer surfaced on /home — it stays documented as an
    advanced option on /setup-advanced.

    Cautious-rollout instances can hide the section by setting
    ``AGNES_HOME_SHOW_AUTOMODE=0`` so users learn the permission flow
    first; the same content stays available on /setup-advanced.

    Resolution: env var > ``instance.home.show_automode`` YAML > True.
    Mirrors :func:`get_home_route` shape so Terraform overrides work
    the same way.
    """
    raw = os.environ.get("AGNES_HOME_SHOW_AUTOMODE")
    if raw is None:
        raw = get_value("instance", "home", "show_automode", default=True)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "")


def get_home_status_frame_visibility() -> bool:
    """Whether /home renders the homepage status frame (Last sync,
    Sessions, Prompts, Tokens, Projects).

    The template ALSO gates rendering on ``users.onboarded`` so a
    fresh user sees a clean install-hero before the all-zero stat
    cards. This helper is the operator-level master switch; the
    onboarding gate is a UX coherence rule layered on top.

    Cautious-rollout instances that would rather not expose token
    counters to analysts yet can disable with
    ``AGNES_HOME_SHOW_STATUS_FRAME=0`` (or
    ``instance.home.show_status_frame: false`` in YAML).

    Resolution: env var > ``instance.home.show_status_frame`` YAML > True.
    Shape mirrors :func:`get_home_automode_visibility` so Terraform
    overrides land the same way.
    """
    raw = os.environ.get("AGNES_HOME_SHOW_STATUS_FRAME")
    if raw is None:
        raw = get_value("instance", "home", "show_status_frame", default=True)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "")


def get_studio_enabled() -> bool:
    """Whether the authoring Studio surface (/admin/studio*) is exposed.

    On by default. Disable per-instance with ``AGNES_STUDIO_ENABLED=0`` (the
    infra/Terraform ``.env`` override) or ``studio.enabled: false`` in
    instance.yaml — hides the Studio nav entry and redirects ``/admin/studio*``
    to home.

    Resolution: env var > ``studio.enabled`` YAML > True — delegates to
    :func:`feature_enabled` (the canonical resolver, #1022); see
    :data:`FEATURE_FLAGS` for the registry entry backing the
    ``/admin/server-config`` inventory panel.
    """
    return feature_enabled("studio", "enabled", env_var="AGNES_STUDIO_ENABLED", default=True)


def get_agent_profiles_enabled() -> bool:
    """Whether the Agent profiles surface (``/agents`` builder, the
    ``/api/v1/agents*`` management + runtime API, and the ``agnes agent`` /
    ``agnes chat`` CLI clients of that API) is exposed.

    On by default — upstream behavior is unchanged; an instance opts out
    per-deployment with ``AGNES_AGENT_PROFILES_ENABLED=0`` (the
    infra/Terraform ``.env`` override) or ``agent_profiles.enabled: false``
    in instance.yaml. Hides the "My agents" nav entry and redirects
    ``/agents`` to home; the guarded REST routers 403 with
    ``{"kind": "agent_profiles_disabled"}``.

    Only gates the HTTP-facing surface. The internal mechanisms a disabled
    instance must keep running regardless — default-agent seeding, chat
    attribution to the default agent, and the broker's agent policy — call
    the repositories directly and never go through this flag.

    Resolution: env var > ``agent_profiles.enabled`` YAML > True — delegates
    to :func:`feature_enabled` (the canonical resolver, #1022); see
    :data:`FEATURE_FLAGS` for the registry entry backing the
    ``/admin/server-config`` inventory panel.
    """
    return feature_enabled("agent_profiles", "enabled", env_var="AGNES_AGENT_PROFILES_ENABLED", default=True)


def get_instance_name() -> str:
    return get_value("instance", "name", default="AI Harness")


def get_instance_subtitle() -> str:
    return get_value("instance", "subtitle", default="")


def get_instance_copyright() -> str:
    """Attribution for the shared page footer — the organization that *deploys*
    this instance, rendered as "Deployed by {value}".

    Three-way distinct from its neighbours: :func:`get_instance_name` is the
    deployment's display name (page titles, email subjects),
    :func:`get_instance_brand` is the *product* (which the footer renders on
    its own left side), and this is the *operator credit*.

    The empty default is load-bearing: the footer omits the attribution line
    entirely rather than falling back to a name nobody chose, which keeps the
    OSS distribution vendor-neutral. Mirrors :func:`get_instance_support`.

    ``instance.copyright`` shipped in ``instance.yaml.example`` and every
    footer template read it as ``config.INSTANCE_COPYRIGHT`` long before this
    resolver existed — but the context builder hardcoded ``""``, so the YAML
    key was inert and every chrome rendered the literal ``'AI Harness'``
    fallback no matter what the operator configured.

    Resolution: ``AGNES_INSTANCE_COPYRIGHT`` env > ``instance.copyright``
    YAML > ``""``.
    """
    raw = os.environ.get("AGNES_INSTANCE_COPYRIGHT")
    if raw is None:
        raw = get_value("instance", "copyright", default="")
    return (raw or "").strip()


def get_instance_brand() -> str:
    """Product-name brand string surfaced to end users in the analyst-facing
    UI (``/home`` hero copy, ``/setup``, ``/login``, the clipboard setup
    script, etc.). Defaults to ``"Agnes"`` — operators rebranding this OSS
    set it to e.g. ``"Foundry AI"`` without forking.

    Distinct from :func:`get_instance_name` which drives page titles and
    represents the deploying organization's display name ("AI Harness").
    Brand is the *product*; name is the *deployment*.

    Resolution: ``AGNES_INSTANCE_BRAND`` env > ``instance.brand`` YAML > ``"Agnes"``.
    Mirrors :func:`get_home_route` shape so Terraform env overrides work.
    """
    raw = os.environ.get("AGNES_INSTANCE_BRAND")
    if raw is None:
        raw = get_value("instance", "brand", default="Agnes")
    value = (raw or "").strip()
    return value or "Agnes"


def get_privacy_policy_url() -> str:
    """The operator's own privacy policy, when they have one.

    Agnes is self-hosted, so the data controller is the organization running
    the instance — not whoever ships the code. The built-in ``/privacy`` page
    can therefore only describe what the *software* does with data; it cannot
    be the controller's policy, and for most operators it should not try.

    Set this and ``/privacy`` redirects there instead of rendering the
    built-in page, so a single URL (``https://<instance>/privacy``) is a
    stable, correct answer whichever way an operator has arranged things —
    which matters because that is the URL handed to a connector directory,
    and both Anthropic's and OpenAI's submissions reject an unreachable one.

    Resolution: ``AGNES_PRIVACY_POLICY_URL`` env > ``instance.privacy_policy_url``
    YAML > ``""`` (render the built-in page).
    """
    raw = os.environ.get("AGNES_PRIVACY_POLICY_URL")
    if raw is None:
        raw = get_value("instance", "privacy_policy_url", default="")
    return (raw or "").strip()


def get_instance_brand_short() -> str:
    """Short form of the product brand for use mid-sentence in body copy
    ("Set up {brand_short} on your machine"), where the full
    :func:`get_instance_brand` value can be unwieldy (e.g. brand
    "Acme Data Analyst" → short "Acme"). ``/home`` keeps the full brand in
    the hero title and appends "Call me {brand_short}." when the two differ.

    Resolution: ``AGNES_INSTANCE_BRAND_SHORT`` env > ``instance.brand_short``
    YAML > :func:`get_instance_brand`. Mirrors :func:`get_instance_brand`
    shape so Terraform env overrides work.
    """
    raw = os.environ.get("AGNES_INSTANCE_BRAND_SHORT")
    if raw is None:
        raw = get_value("instance", "brand_short", default="")
    value = (raw or "").strip()
    return value or get_instance_brand()


def get_instance_logo_svg() -> str:
    """Raw inline ``<svg>`` markup rendered into the nav brand slot
    (``_app_rail.html``). When non-empty, replaces the text brand in
    the nav — typical use is a lockup that already contains the
    brand wordmark. When empty, the nav falls back to
    :func:`get_instance_name` as text.

    Resolution: ``AGNES_INSTANCE_LOGO_SVG`` env > ``instance.logo_svg``
    YAML > ``""``. Mirrors :func:`get_instance_brand` so Terraform env
    overrides work the same way.
    """
    raw = os.environ.get("AGNES_INSTANCE_LOGO_SVG")
    if raw is None:
        raw = get_value("instance", "logo_svg", default="")
    return (raw or "").strip()


def get_instance_favicon() -> str:
    """Favicon href for ``<link rel="icon">`` — resolved to a value templates
    can drop in directly, unlike :func:`get_instance_logo_svg` (raw markup)
    or the CSS/JS asset helpers (which take a bare relative path and expect
    the *template* to call ``static_url()`` on it).

    Contract:
      - A value containing ``"://"`` or starting with ``"data:"`` (an
        absolute URL or a data-URI) is returned AS-IS — the operator is
        pointing at an externally-hosted icon or embedding one inline.
      - Any other non-empty value is treated as a path under
        ``app/web/static/`` and resolved through the same
        ``app.web.router._static_url`` helper the templates already use for
        every other static asset (adds the ``?v=<mtime>`` cache-buster).
      - Unset (env AND YAML both empty) resolves the built-in
        ``img/agnes-orb.png`` asset through that same helper — byte-identical
        to the ``<link>`` this replaces.

    Resolution: ``AGNES_INSTANCE_FAVICON`` env > ``instance.favicon`` YAML >
    the built-in ``img/agnes-orb.png`` asset. Mirrors :func:`get_instance_logo_svg`.

    Lazy-imports ``_static_url`` from ``app.web.router`` — a module-level
    import would be circular (``app.web.router`` imports this module at load
    time); by the time a request reaches this resolver, the app has already
    finished importing. Same pattern as ``src.welcome_template``'s import of
    ``app.web.router._read_agnes_ca_pem``.
    """
    raw = os.environ.get("AGNES_INSTANCE_FAVICON")
    if raw is None:
        raw = get_value("instance", "favicon", default="")
    value = (raw or "").strip()
    if value.startswith("data:") or "://" in value:
        return value

    from app.web.router import _static_url

    return _static_url(value or "img/agnes-orb.png")


def get_instance_overview() -> str:
    """Operator-authored Overview body rendered on ``/home``. Markdown is
    NOT auto-converted — operators paste HTML (matches the existing
    ``news_intro`` ``| safe`` filter). Empty default = section hidden,
    keeping the OSS vendor-neutral when an instance ships without
    operator-specific framing.

    Resolution: ``AGNES_INSTANCE_OVERVIEW`` env > ``instance.overview``
    YAML > ``""``. Mirrors :func:`get_instance_logo_svg`.
    """
    raw = os.environ.get("AGNES_INSTANCE_OVERVIEW")
    if raw is None:
        raw = get_value("instance", "overview", default="")
    return (raw or "").strip()


def get_instance_support() -> str:
    """Operator-authored Support body rendered inside the welcome hero
    on ``/home``. Same ``| safe``-filter shape as
    :func:`get_instance_overview` — operators paste HTML. Distinct
    config field so help/chat pointers can be updated independently
    from the product framing in ``instance.overview``.

    Typical content: a one-line invitation pointing at a chat space
    (Google Chat / Slack / Teams), a mailing list, or an internal
    runbook. Empty default = block hidden, keeping the OSS
    vendor-neutral when an instance ships without an operator-defined
    support channel.

    Resolution: ``AGNES_INSTANCE_SUPPORT`` env > ``instance.support``
    YAML > ``""``. Mirrors :func:`get_instance_overview`.
    """
    raw = os.environ.get("AGNES_INSTANCE_SUPPORT")
    if raw is None:
        raw = get_value("instance", "support", default="")
    return (raw or "").strip()


def get_hidden_login_features() -> frozenset[str]:
    """Stable keys of the ``/login`` feature cards to hide on this instance.

    The ``/login`` left panel renders a fixed set of feature-card tiles, each
    tagged with a stable key (``data``, ``marketplace``, ``mcp``, ``memory``,
    ``anywhere``). Listing a key here drops that card — a generic,
    per-deployment way to trim the landing chrome without forking the
    template.

    Resolution: ``AGNES_INSTANCE_HIDE_LOGIN_FEATURES`` env (comma-separated,
    e.g. ``"mcp,memory"``) > ``instance.hide_login_features`` in instance.yaml
    (a YAML list *or* a comma-separated string) > empty. Values are split on
    commas, stripped, lowercased, de-duplicated, and empties dropped, yielding
    a ``frozenset`` the template membership-tests against.

    The empty default hides nothing — the shipped OSS renders every card, so
    the concrete choice of what to hide stays a deployment-level decision and
    the public repo stays vendor-neutral. Mirrors :func:`get_instance_overview`
    in the env-overrides-yaml shape.
    """
    raw = os.environ.get("AGNES_INSTANCE_HIDE_LOGIN_FEATURES")
    if raw is None:
        raw = get_value("instance", "hide_login_features", default="")
    # Accept a YAML list (``["mcp", "memory"]``) or a comma-separated string
    # (``"mcp, memory"``) interchangeably — split every piece on commas so
    # either form normalizes the same way.
    if isinstance(raw, (list, tuple)):
        tokens: list[str] = []
        for item in raw:
            tokens.extend(str(item).split(","))
    else:
        tokens = str(raw or "").split(",")
    return frozenset(token.strip().lower() for token in tokens if token.strip())


def get_ssrf_allowed_hosts() -> frozenset[str]:
    """Deployer-trusted hostnames exempt from the private/reserved-network
    SSRF guard in ``app.api.admin._validate_url_not_private``.

    Because that is the shared validator, a listed host is exempt on EVERY
    admin URL that routes through it, not just git clone URLs: the marketplace
    + initial-workspace clone URLs, the Keboola ``stack_url`` in the configure
    wizard, and the URL-bearing server-config fields checked by
    ``_validate_urls_in_patch`` (``data_source.keboola.stack_url``,
    ``marketplace.curators_url``).

    The motivating case is an organization hosting its git behind an internal
    GitHub Enterprise / GitLab on a private network — a legitimate clone target
    the guard would otherwise reject because the hostname resolves to an
    RFC-1918 / reserved address. Listing the host is an explicit operator
    opt-in: every affected URL is already admin-gated, so this is a
    deployment-level trust decision, not a user-facing one.

    Empty default keeps the OSS distribution fail-closed and vendor-neutral —
    the concrete internal host is set in deployment config, outside this repo.

    Resolution: ``AGNES_SSRF_ALLOWED_HOSTS`` env (comma-separated) >
    ``security.ssrf_allowed_hosts`` in instance.yaml (a YAML list *or* a
    comma-separated string) > empty. Hostnames are split on commas, stripped,
    and lowercased. Mirrors :func:`get_hidden_login_features` in accepting
    either form.
    """
    raw = os.environ.get("AGNES_SSRF_ALLOWED_HOSTS")
    if raw is None:
        raw = get_value("security", "ssrf_allowed_hosts", default="")
    if isinstance(raw, (list, tuple)):
        tokens: list[str] = []
        for item in raw:
            tokens.extend(str(item).split(","))
    else:
        tokens = str(raw or "").split(",")
    return frozenset(token.strip().lower() for token in tokens if token.strip())


def get_instance_custom_preamble() -> str:
    """Operator-authored preamble injected at the TOP of the install
    prompt (above ``Set up the {instance_brand} CLI…``). Empty/unset
    emits zero lines so the rendered prompt stays byte-identical to the
    default — keeping the OSS vendor-neutral; the brand-specific value is
    set in production config, outside this repo.

    ``{instance_brand}`` (and the other server-side placeholders substituted
    by :func:`app.web.setup_instructions.resolve_lines`) are honored inside
    the preamble, but it MUST NOT contain a literal ``{server_url}`` (that
    one is substituted at click time in the JS clipboard flow, not in the
    preamble body) and MUST NOT reference ``{token}`` at all — the token is
    no longer a prompt placeholder anywhere; it is handed off via /home's
    step 4 into ``~/.agnes/token``.

    Resolution: ``AGNES_INSTANCE_CUSTOM_PREAMBLE`` env > ``instance.custom_preamble``
    YAML > ``""``. Mirrors :func:`get_instance_overview`.
    """
    raw = os.environ.get("AGNES_INSTANCE_CUSTOM_PREAMBLE")
    if raw is None:
        raw = get_value("instance", "custom_preamble", default="")
    return (raw or "").strip()


_CUSTOM_SCRIPT_PLACEMENTS = ("head_start", "head_end", "body_end")


def _custom_script_enabled(value) -> bool:
    """Coerce the per-entry ``enabled`` field tolerant of YAML's many
    truthiness shapes.

    Operators hand-editing YAML (or pasting blocks from another source)
    can land ``enabled: "false"`` (quoted string), ``enabled: 0``, or
    ``enabled: no`` rather than the boolean ``false``. ``bool("false")``
    is ``True`` in Python, so a naive truth check silently keeps the
    script live — a footgun for what's meant to be a kill switch on
    admin-injected JS. Missing / ``None`` → live (default-on, matches
    the registered field shape).
    """
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "no", "0", "off")
    return bool(value)


def get_custom_scripts() -> list[dict]:
    """Operator-injected HTML/JS blocks rendered by ``base.html``.

    Reads ``instance.custom_scripts`` from instance.yaml — a list of
    dicts ``{name, enabled, placement, html}``. Each block lands in one
    of three template slots:

    - ``head_start`` — first thing in ``<head>``, before any CSS/JS
      (rare; GTM dataLayer init).
    - ``head_end`` — last thing in ``<head>`` (default; analytics +
      feedback widgets like Marker.io, Sentry, Hotjar).
    - ``body_end`` — just before ``</body>`` (vendors that explicitly
      ask for bottom placement).

    Trust boundary: admin-only. ``instance.yaml`` is written through
    ``/api/admin/server-config`` (gated by ``require_admin``) and the
    rendered HTML is interpolated with ``| safe``, exactly mirroring
    ``instance.logo_svg`` / ``instance.overview``.

    Normalization:
    - Drop entries whose ``enabled`` resolves to false via
      :func:`_custom_script_enabled` (handles quoted strings, 0/1, etc.
      — not just the Python ``False`` singleton).
    - Drop entries whose ``html`` strips to empty.
    - Default missing ``name`` to "" and missing ``placement`` to
      "head_end".
    - Drop entries whose ``placement`` isn't in the allowlist, with a
      logged warning naming the offending block — admin sees the
      mistake instead of the server crashing.

    No env-var override: the structure is a list of objects, which
    doesn't round-trip cleanly through env vars; deployment-time
    injection happens by writing the YAML from the deploy script.

    Returns ``[]`` when YAML omits the key — empty by default keeps the
    OSS vendor-neutral.
    """
    raw = get_value("instance", "custom_scripts", default=None)
    if not raw:
        return []
    if not isinstance(raw, list):
        logger.warning(
            "instance.custom_scripts must be a list, got %s — ignoring",
            type(raw).__name__,
        )
        return []
    out: list[dict] = []
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            logger.warning(
                "instance.custom_scripts[%d] must be a dict, got %s — skipping",
                idx,
                type(entry).__name__,
            )
            continue
        if not _custom_script_enabled(entry.get("enabled")):
            continue
        html = (entry.get("html") or "").strip()
        if not html:
            continue
        placement = (entry.get("placement") or "head_end").strip()
        if placement not in _CUSTOM_SCRIPT_PLACEMENTS:
            logger.warning(
                "instance.custom_scripts[%d] (name=%r) has unknown placement %r — must be one of %s — skipping",
                idx,
                entry.get("name", ""),
                placement,
                ", ".join(_CUSTOM_SCRIPT_PLACEMENTS),
            )
            continue
        out.append(
            {
                "name": str(entry.get("name") or ""),
                "enabled": True,
                "placement": placement,
                "html": html,
            }
        )
    return out


def get_workspace_dir_name() -> str:
    """Filesystem-safe folder name for the analyst's local workspace
    (``~/<workspace_dir_name>``). Defaults to :func:`get_instance_brand`
    with every non-alphanumeric character stripped, so ``"Foundry AI"``
    becomes ``"FoundryAI"`` and ``"Agnes"`` stays ``"Agnes"``.

    An explicit override exists for operators who want a folder name that
    doesn't follow the strip-whitespace derivation.

    Resolution: ``AGNES_WORKSPACE_DIR_NAME`` env > ``instance.workspace_dir``
    YAML > derived from :func:`get_instance_brand`.
    """
    raw = os.environ.get("AGNES_WORKSPACE_DIR_NAME")
    if raw is None:
        raw = get_value("instance", "workspace_dir", default="")
    explicit = (raw or "").strip()
    if explicit:
        return explicit
    import re

    derived = re.sub(r"[^A-Za-z0-9]", "", get_instance_brand())
    return derived or "Agnes"


def get_workspace_launcher_word() -> str:
    """The one word an analyst types to open their workspace.

    ``agnes init`` installs a launcher script under this name (see
    ``cli/lib/shortcut.py``), derived from the workspace folder name stripped
    to lowercase alphanumerics. The install guide has to name the same word,
    so both sides derive it the same way here rather than each approximating
    it — lowercasing the folder name alone is only equivalent while that name
    is already alphanumeric, which the brand-derived default is but an
    explicit ``AGNES_WORKSPACE_DIR_NAME`` override need not be.

    Not covered: the CLI appends an ``ai`` suffix when the word would shadow
    a shell built-in or a command the toolchain needs (``agnes``, ``claude``).
    That check reads the *client's* PATH, so the server cannot predict it.
    """
    from src.launcher_word import launcher_word

    return launcher_word(get_workspace_dir_name())


def get_instance_admin_email() -> str:
    """Operator-facing contact address shown in user-side prompts that
    suggest the user reach out to their Agnes admin (e.g. the /home GWS
    connector tile renders an "Email admin" mailto button when no shared
    OAuth app is provisioned). Empty string when unset — the template
    branches on the value being truthy, so an empty value hides the
    button rather than rendering a broken `mailto:` link.

    Resolution: ``AGNES_INSTANCE_ADMIN_EMAIL`` env > ``instance.admin_email`` YAML > "".
    Mirrors :func:`get_home_route` shape so Terraform overrides work.
    """
    raw = os.environ.get("AGNES_INSTANCE_ADMIN_EMAIL")
    if raw is None:
        raw = get_value("instance", "admin_email", default="")
    return (raw or "").strip()


def get_infra_repo_url() -> str:
    """Optional URL of the infrastructure/provisioning repository for this
    instance (e.g. the Terraform repo that deployed the VM). Used by the
    built-in ``agnes-operator`` marketplace plugin to give the operator's
    Claude a concrete pointer to where they manage infra for this instance.

    Empty string by default — the OSS distribution ships vendor-neutral;
    an operator sets this so the operator plugin can name the real infra
    repo without hardcoding anything in shipped content.

    Resolution: ``AGNES_INFRA_REPO_URL`` env > ``instance.infra_repo_url``
    YAML > ``""`` (unset). Mirrors :func:`get_instance_admin_email` shape.
    """
    raw = os.environ.get("AGNES_INFRA_REPO_URL")
    if raw is None:
        raw = get_value("instance", "infra_repo_url", default="")
    return (raw or "").strip()


def get_atlassian_base_url() -> str:
    """Operator-provisioned Atlassian Cloud site URL — baked into the
    Atlassian connector prompt so end users don't have to guess /
    paste their org's `https://<myorg>.atlassian.net`.

    When set, the connector prompt's "ask me for the site URL" step
    is replaced by a literal value the helper script substitutes
    directly. When unset (empty string), the prompt falls back to
    asking the user — same flow as today.

    Normalized: trailing slashes and a trailing ``/wiki`` are stripped
    so the value is always the bare site root. Matches the
    normalization the per-user helper script already does at storage
    time (see atlassian_prompt step 4 guard 2).

    Resolution: ``AGNES_ATLASSIAN_BASE_URL`` env > ``instance.atlassian.base_url`` YAML > "".
    Mirrors :func:`get_instance_admin_email` so Terraform overrides
    work the same way.
    """
    raw = os.environ.get("AGNES_ATLASSIAN_BASE_URL")
    if raw is None:
        raw = get_value("instance", "atlassian", "base_url", default="")
    value = (raw or "").strip().rstrip("/")
    if value.endswith("/wiki"):
        value = value[: -len("/wiki")]
    return value


def get_sync_interval() -> str:
    """Human-readable refresh cadence shown in the analyst welcome prompt."""
    return get_value("instance", "sync_interval", default="1 hour")


def get_allowed_domains() -> list:
    """Sign-in domain allowlist, lower-cased.

    Domains are case-insensitive (DNS), but the OAuth providers compare this
    list against an address claim with ``in`` — and Microsoft lower-cases the
    resolved claim before doing so. Folding here means ``allowed_domain:
    "Acme.com"`` doesn't refuse every Microsoft sign-in while leaving Google
    working; the callers fold the claim's domain to match.
    """
    domain = get_value("auth", "allowed_domain", default="")
    if domain:
        return [d.strip().lower() for d in domain.split(",") if d.strip()]
    return []


def get_datasets() -> dict:
    return get_value("datasets", default={})


def get_theme() -> dict:
    return get_value("theme", default={})


# Operator `theme:` colour/style keys (see `config/instance.yaml.example`)
# → the CSS custom properties each one drives. Every key gets its legacy
# `--*` variable (the pre-redesign palette in `style-custom.css`); where a
# clean design-system equivalent exists, it also gets the matching `--ds-*`
# token, so a `theme:` override recolors the "paper"/"navy"/"dark" surfaces
# too, not just the legacy chrome. `radius` and `font_primary` have no
# single `--ds-*` counterpart (the design system splits radius into
# `--ds-radius-btn`/`--ds-radius-pill`, and the font stack is more than a
# swap-in-one-value change) so they stay legacy-only. `font_url` isn't a
# CSS variable at all — it drives a `<link rel="stylesheet">` tag — and is
# intentionally absent here.
#
# The design system's status vocabulary is named "warn"/"danger", not
# "warning"/"error" — same colours the operator keys describe, different
# token family names.
THEME_CSS_VAR_MAP: dict[str, tuple[str, ...]] = {
    "primary": ("--primary", "--ds-primary"),
    "primary_dark": ("--primary-dark", "--ds-primary-dark"),
    "primary_light": ("--primary-light", "--ds-primary-light"),
    "background": ("--background", "--ds-bg"),
    "surface": ("--surface", "--ds-surface"),
    "border": ("--border", "--ds-border"),
    "text_primary": ("--text-primary", "--ds-text-primary"),
    "text_secondary": ("--text-secondary", "--ds-text-secondary"),
    "success": ("--success", "--ds-accent-success-ink"),
    "warning": ("--warning", "--ds-accent-warn-ink"),
    "error": ("--error", "--ds-accent-danger-ink"),
    "font_primary": ("--font-primary",),
    "radius": ("--radius",),
}


def get_theme_css_overrides() -> dict[str, str]:
    """CSS custom-property overrides for the operator `theme:` block.

    Returns ``{css_var_name: value}`` for every key the operator actually
    set (never an empty value) — both the legacy `--*` family and, per
    :data:`THEME_CSS_VAR_MAP`, the matching `--ds-*` design-system token.
    Rendered by ``_theme.html`` into one inline `<style>` block whose
    selector is deliberately at least as specific as every
    `:root[data-theme="…"]` block in ``design-tokens.css``, so the
    override reliably wins regardless of which theme palette is active —
    see that template for the selector rationale.
    """
    theme = get_theme()
    if not isinstance(theme, dict):
        return {}
    overrides: dict[str, str] = {}
    for key, value in theme.items():
        if not value or key == "font_url":
            continue
        for css_var in THEME_CSS_VAR_MAP.get(key, ()):
            overrides[css_var] = value
    return overrides


def get_auth_config() -> dict:
    return get_value("auth", default={})


def get_corporate_memory_config() -> dict:
    return get_value("corporate_memory", default={})


# Defaults backfilled when the feature is enabled via the AGNES_DATA_APPS_ENABLED
# env override but instance.yaml carries no ``data_apps:`` block — mirror
# config/instance.yaml.example so downstream spec-builders (which read
# ``default_sleep_mode``/``default_mem_limit``/… by key) don't KeyError.
_DATA_APPS_ENV_DEFAULTS = {
    "runtime_image": "keboolapublic.azurecr.io/data-app-python-js:1.6.2_python-3.13_node-24",
    "subdomain_base": "",
    # Serve hosted apps on the main origin (same origin as `/api`)? Off by
    # default — see `app/api/data_apps.py::_CONFIG_DEFAULTS`. Resolution
    # (env > merged config > default) lives in the switch registry
    # (`app/switches.py`, entry `data_apps_allow_same_origin`) read by the
    # call site (`same_origin_serving_allowed`), so this backfill only
    # matters for an instance.yaml-configured value.
    "allow_same_origin": False,
    "default_idle_timeout_s": 1800,
    "default_sleep_mode": "recreate",
    "default_mem_limit": "1g",
    "default_cpus": 1.0,
    "max_apps_per_user": 3,
    # Mirrors `app/api/data_apps.py::_CONFIG_DEFAULTS` — read-only rootfs off
    # by default (its tmpfs list is unverified against the shipped runtime
    # image), fork-bomb ceiling on.
    "container_read_only": False,
    "container_pids_limit": 512,
}


def get_data_apps_config() -> dict:
    """``data_apps:`` block — hosted user web apps feature (v96).

    ``get_value(..., default={})`` only substitutes the default when the
    key is absent — an explicit null block in instance.yaml (or a
    config-not-loaded-yet state some bootstrap/test paths hit) can still
    make this come back ``None``. Hardened to always return a dict: this
    accessor runs on every request via ``DataAppSubdomainMiddleware``
    (``app/data_apps_subdomain.py``) and ``session_cookie_domain()`` below
    (itself called from every login flow), so callers should never need
    their own ``(get_data_apps_config() or {})`` guard.

    ``AGNES_DATA_APPS_ENABLED`` env override (Terraform-friendly, mirrors
    :func:`get_home_route` / :func:`get_public_url`): the customer-instance
    module flips the feature on by setting this in ``.env`` — alongside the
    apps-runner token, ``DOCKER_GID`` and ``COMPOSE_PROFILES=apps`` — instead
    of editing the instance.yaml overlay on disk. When set, the env var wins
    over instance.yaml in BOTH directions per the canonical flag resolution
    (#1022, :func:`feature_enabled` / ``docs/feature-flags.md``) — a falsy
    value forces the feature off even when the yaml enables it, keeping this
    accessor consistent with the request gates that resolve the same var.
    Instances without the var are byte-for-byte unchanged. When enabled, the
    example-config defaults are backfilled for any key instance.yaml omits so
    the spec-builders have a complete block, and ``runtime_image`` can be
    further pinned with ``AGNES_DATA_APPS_RUNTIME_IMAGE``.

    ``subdomain_base`` has its own override, ``AGNES_DATA_APPS_SUBDOMAIN_BASE``,
    applied whenever the feature resolves enabled — by either source — because
    it drives :func:`session_cookie_domain`. While the feature resolves
    DISABLED the key is dropped entirely, from yaml as well as env: it widens
    the session cookie and switches on host-based routing, and neither reader
    checks ``enabled`` for itself. See the inline note at the bottom of the body.
    """
    cfg = dict(get_value("data_apps", default={}) or {})
    raw = os.environ.get("AGNES_DATA_APPS_ENABLED")
    if raw is not None:
        if coerce_flag_value(raw, default=False):
            cfg = {**_DATA_APPS_ENV_DEFAULTS, **cfg, "enabled": True}
            env_image = os.environ.get("AGNES_DATA_APPS_RUNTIME_IMAGE")
            if env_image:
                cfg["runtime_image"] = env_image
        else:
            cfg["enabled"] = False
    # ``subdomain_base`` is resolved LAST, and resolved here rather than at its
    # readers, because both of them read it unconditionally:
    # :func:`session_cookie_domain` on every login and
    # ``DataAppSubdomainMiddleware`` (``app/data_apps_subdomain.py``) on every
    # request. Neither re-checks ``enabled``, so this accessor is the single
    # place where "the feature is off" can be made to mean "there is no base".
    #
    # Off ⇒ no base, whatever the SOURCE of the base. Widening the session
    # cookie to the base's parent domain, and routing ``<slug>.<base>`` hosts,
    # are both things a deployment that is serving no apps must not be doing —
    # and the config that asks for them outlives the switch that turned the
    # feature on, in both directions: a stale ``.env`` line, or (the case an
    # earlier cut of this change missed — Devin Review on #1588) a
    # ``subdomain_base`` still sitting in instance.yaml under
    # ``AGNES_DATA_APPS_ENABLED=false``.
    if not cfg.get("enabled"):
        # ``pop``, not ``= ""``: an absent/null ``data_apps:`` block must keep
        # resolving to exactly ``{}`` (three tests pin that hardening).
        cfg.pop("subdomain_base", None)
        return cfg
    # ``AGNES_DATA_APPS_SUBDOMAIN_BASE`` — deliberately keyed on the RESOLVED
    # ``enabled`` above, not on the env-enable path the ``runtime_image`` pin
    # sits inside, so the value does not depend on WHETHER the operator
    # switched data apps on via env or yaml.
    # (TODO: ``AGNES_DATA_APPS_RUNTIME_IMAGE`` above still no-ops silently on a
    # yaml-enabled instance — same treatment, separate change.)
    #
    # An empty value wins too (env decides in both directions, per the #1022
    # convention) — it forces path-prefix mode even when instance.yaml names a
    # base.
    env_base = os.environ.get("AGNES_DATA_APPS_SUBDOMAIN_BASE")
    if env_base is not None:
        cfg["subdomain_base"] = env_base.strip()
    return cfg


def session_cookie_domain() -> Optional[str]:
    """``Domain`` attribute for the session cookie (``access_token``), or
    ``None`` for no ``Domain`` attribute at all — i.e. exactly today's
    pre-data-apps behavior (cookie scoped to the exact host that set it).

    Only set when ``data_apps.subdomain_base`` is configured (Task 8's
    ingress proxy — ``app/data_apps_subdomain.py``): a subdomain like
    ``<slug>.apps.example.com`` needs the cookie minted on the main host to
    also be sent on the data-app subdomain, which requires scoping it to
    the shared parent domain (``.example.com``) rather than the exact host.
    """
    base = (get_data_apps_config().get("subdomain_base") or "").strip()
    if not base or "." not in base:
        return None
    return "." + base.split(".", 1)[1]


def get_guardrails_config() -> dict:
    """Flea-market upload-guardrail config (see docs/STORE_GUARDRAILS.md).

    Returns the ``guardrails:`` block from instance.yaml, or an empty dict
    when not configured. Call site: ``src/store_guardrails/runner.py``.
    """
    return get_value("guardrails", default={})


def get_guardrails_review_model() -> str:
    """Resolved Anthropic model ID used for the LLM security review.

    Reads ``guardrails.review_model`` (one of ``haiku``, ``sonnet``,
    ``opus``, or a concrete ``claude-*`` model ID) and returns the
    concrete model ID. Defaults to Haiku — the cheapest tier — when the
    operator hasn't set the key. Override per-instance for higher-stakes
    review at proportionally higher cost.
    """
    from connectors.llm.factory import resolve_model_tier

    raw = get_value("guardrails", "review_model", default="haiku")
    return resolve_model_tier(raw)


def get_mcp_source_url_strict() -> bool:
    """Whether an MCP source's ``url`` must be https to a public address.

    Reads ``mcp.source_url_strict`` (env ``AGNES_MCP_SOURCE_URL_STRICT``).
    **Defaults to False**, which is not the same as unguarded: the baseline
    always refuses link-local / metadata / multicast / reserved addresses and
    cleartext http to a public one. What the default permits is an MCP source
    on an INTERNAL address — an organization's own tool server, a developer's
    localhost — because those are ordinary deployments, and a check that broke
    them would be traded away rather than adopted.

    Set True to hold a source's url to the same bar as its OAuth endpoints
    (https, public address). Right for instances that only ever talk to
    third-party MCP services; it makes an intranet source unconfigurable, so
    it is opt-in. See ``src/net/mcp_source_url.py``.
    """
    return feature_enabled("mcp", "source_url_strict", env_var="AGNES_MCP_SOURCE_URL_STRICT", default=False)


def get_mcp_connector_ui_enabled() -> bool:
    """Whether the user-facing MCP connector surface is exposed: the
    ``/me/ai-connector`` and ``/mcp-connect`` install-instruction pages, the
    MCP tab of ``/how-it-works#connect``, and their nav / command-palette
    entries.

    On by default — upstream behavior is unchanged. An instance opts out
    with ``AGNES_MCP_CONNECTOR_UI_ENABLED=0`` (or ``mcp.connector_ui_enabled:
    false`` in instance.yaml) when it is reachable only on a private network
    (VPN, intranet hostname/address): in-network MCP clients keep working
    either way, but a cloud-side connector client resolved from outside the
    network can never reach the endpoint, so showing its install
    instructions would show a setup path that cannot work. See
    docs/DEPLOYMENT.md for the VPN/intranet-only posture this backs.

    Gates UI ONLY — never the MCP protocol itself. ``/api/mcp/http`` and
    ``/api/mcp/sse`` keep serving any client that can reach them regardless
    of this switch.

    Resolution: env var > ``mcp.connector_ui_enabled`` YAML > True —
    delegates to :func:`feature_enabled` (the canonical resolver, #1022);
    see :data:`FEATURE_FLAGS` for the registry entry backing the
    ``/admin/server-config`` inventory panel.
    """
    return feature_enabled("mcp", "connector_ui_enabled", env_var="AGNES_MCP_CONNECTOR_UI_ENABLED", default=True)


def get_mcp_source_url_runtime_enforce() -> bool:
    """Whether the DNS-free url-policy check also runs at the two credentialed
    forward seams, not only when a source is configured (#1216).

    Reads ``mcp.source_url_runtime_enforce`` (env
    ``AGNES_MCP_SOURCE_URL_RUNTIME_ENFORCE``). **Defaults to False**: a source
    that is enabled and was registered before ``check_source_url`` existed, or
    before ``mcp.source_url_strict`` was turned on, keeps forwarding exactly as
    it does today until an admin opts in — flipping this on for the first time
    is what turns a working (if policy-violating) integration into a refused
    one, with no prior warning to whoever calls that tool next.

    Before turning this on, review the ``url_policy_verdict`` column on the
    admin MCP source list (``GET /api/admin/mcp-sources`` /
    ``agnes admin mcp source list``) for any ``would_refuse`` row and fix its
    url first — this switch converts each one from a silent warning into a
    refused call. See ``app.api.mcp_policy.enforce_source_url_runtime_policy``,
    the one helper both forward seams share.
    """
    return feature_enabled(
        "mcp",
        "source_url_runtime_enforce",
        env_var="AGNES_MCP_SOURCE_URL_RUNTIME_ENFORCE",
        default=False,
    )


def get_guardrails_blocked_quota_per_day() -> int:
    """Per-submitter cap on `blocked_llm` + `review_error` rows in the
    trailing 24h.

    Defaults to 50. Set to 0 in instance.yaml to disable the quota
    entirely (useful for trusted single-tenant deployments). Bounds
    the worst case where a bot loops on bundles that pass inline
    checks but trip the async LLM reviewer. Inline failures are
    hard-rejected upstream (no row created) and not counted here;
    HTTP-level rate limiting + the
    ``store.upload.security_blocked`` audit trail cover that path.
    """
    val = get_value("guardrails", "blocked_quota_per_day", default=50)
    try:
        return max(0, int(val))
    except (TypeError, ValueError):
        return 50


def get_guardrails_blocked_bundle_ttl_days() -> int:
    """How many days to keep a blocked bundle's bytes on disk.

    Default 30. The submission row + sha256 + size always survive — only
    the bundle bytes get removed. ``bundle_purged_at`` is stamped so the
    detail UI renders *"Bundle purged on …"*. Set to 0 to disable the
    TTL purge entirely (bundles persist indefinitely until manual
    Delete).
    """
    val = get_value("guardrails", "blocked_bundle_ttl_days", default=30)
    try:
        return max(0, int(val))
    except (TypeError, ValueError):
        return 30


def get_guardrails_stuck_review_grace_seconds() -> int:
    """How long a submission may stay at ``status='pending_llm'`` before
    the reaper flips it to ``review_error``.

    The BackgroundTasks worker normally writes a verdict within a few
    seconds. If the worker crashes between status flip and verdict
    write, the row would otherwise sit at pending_llm forever — admin
    queue surfaces it indefinitely; submitter never gets a verdict.

    Default 1800s (30 min) comfortably exceeds the Sonnet/Opus p99
    wall time for the configured ``MAX_REVIEW_BYTES`` payload. Set to
    0 to disable the reaper entirely.
    """
    val = get_value("guardrails", "stuck_review_grace_seconds", default=1800)
    try:
        return max(0, int(val))
    except (TypeError, ValueError):
        return 1800


def get_audit_retention_days() -> int:
    """How many days to keep ``audit_log`` rows before the daily
    ``audit-prune`` scheduler job deletes them.

    Reads ``audit.retention_days``. Default 365. Set to 0 to keep audit
    rows forever (disables the prune job's DELETE, mirroring
    ``blocked_bundle_ttl_days``'s "0 = retain indefinitely" convention).
    Every other audit/observability trail (chat transcripts, CLI session
    JSONLs, usage rollups, sync_history, llm_usage, agent-runtime
    forensics) has no retention policy yet — see docs/observability.md.
    """
    val = get_value("audit", "retention_days", default=365)
    try:
        return max(0, int(val))
    except (TypeError, ValueError):
        return 365


def get_guardrails_min_description_chars() -> int:
    """Minimum character floor for skill / agent / plugin descriptions.

    Reads ``guardrails.min_description_chars`` (default 60). Set the
    floor low (e.g. 30) to relax the inline content check; set high
    (e.g. 120) to push submitters closer to the Claude-skill-ecosystem
    norm of 150–220 chars per description.
    """
    val = get_value("guardrails", "min_description_chars", default=60)
    try:
        return max(1, int(val))
    except (TypeError, ValueError):
        return 60


def get_guardrails_min_command_description_chars() -> int:
    """Minimum character floor for slash-command descriptions.

    Reads ``guardrails.min_command_description_chars`` (default 25).
    Commands are typically one-verb actions — kept tighter than skills.
    """
    val = get_value("guardrails", "min_command_description_chars", default=25)
    try:
        return max(1, int(val))
    except (TypeError, ValueError):
        return 25


def get_guardrails_min_distinct_words() -> int:
    """Minimum distinct-word count for any description string.

    Reads ``guardrails.min_distinct_words`` (default 5). Defends against
    "padding hits the char count but says nothing" cases like
    `"description description description description"`.
    """
    val = get_value("guardrails", "min_distinct_words", default=5)
    try:
        return max(1, int(val))
    except (TypeError, ValueError):
        return 5


def get_guardrails_min_body_chars() -> int:
    """Minimum body-content floor for skill / agent files.

    Reads ``guardrails.min_body_chars`` (default 200). Body = the
    markdown after the YAML frontmatter. 200 chars is a "one paragraph"
    floor that catches stubs; real skill bodies run 500–2000 chars.
    """
    val = get_value("guardrails", "min_body_chars", default=200)
    try:
        return max(1, int(val))
    except (TypeError, ValueError):
        return 200


def get_guardrails_enabled() -> bool:
    """Operator's stated intent for the guardrail pipeline.

    Reads ``guardrails.enabled`` from instance.yaml. Defaults to True.
    Operators can explicitly disable by setting ``guardrails.enabled:
    false`` — useful for local development against the UI without
    burning Anthropic tokens. ``AGNES_GUARDRAILS_ENABLED`` env var wins
    over both (new, additive — #1022 canonicalization; delegates to
    :func:`feature_enabled`, see :data:`FEATURE_FLAGS`).

    Note: this returns intent ONLY. Whether the LLM provider has
    working credentials is a separate concern — see
    :func:`get_guardrails_llm_provider_ready`. The two are kept apart
    so callers can implement fail-CLOSED behavior: hold submissions at
    ``pending_llm`` (instead of silently auto-approving) when intent is
    True but credentials are missing.
    """
    return feature_enabled("guardrails", "enabled", env_var="AGNES_GUARDRAILS_ENABLED", default=True)


def get_store_verification_enabled() -> bool:
    """Whether the org-verification axis is offered on this instance.

    Reads ``store.verification_enabled``. **Defaults to False.**

    Off by default for upgrade parity: an existing instance must not grow a
    verification workflow (author-facing "Request verification" buttons,
    admin "Verify" / "Request changes" strips on item detail) out of a routine
    upgrade nobody opted into — with no reviewer appointed, every request
    would rot at "pending" and the negative marker would print on every card
    while saying nothing. Instances that adopt the trust vocabulary set this
    to True together with ``library.show_unverified_trust``, which makes all
    three levels (Organization / Verified / Community) positive statements
    and gives the admin action a reason to exist.

    When False the verify endpoints 400 and the author-facing "Request
    verification" button disappears.
    """
    return bool(get_value("store", "verification_enabled", default=False))


def get_guardrails_llm_provider_ready() -> bool:
    """Whether the LLM provider has credentials present in the
    environment (or the instance is configured for Vertex, which needs no
    static key — Google ADC signs its calls).

    Independent from :func:`get_guardrails_enabled` (operator intent).
    A False return here when intent is True is a misconfiguration —
    the caller should hold submissions at ``pending_llm`` and surface
    a loud boot-time warning rather than silently auto-approving.
    """
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return True
    if os.environ.get("LLM_API_KEY", "").strip():
        return True
    try:
        from connectors.llm.factory import vertex_config_or_none

        if vertex_config_or_none():
            return True
    except Exception:  # noqa: BLE001 — readiness probe; a broken import means "not ready"
        pass
    return False


def get_lint_max_body_chars() -> int:
    """Maximum body character length for skill linter (SL002 bloat rule).

    Reads ``guardrails.lint_max_body_chars`` (default 8000). Skills whose
    body length exceeds this threshold trigger a linter warning. Set lower
    (e.g. 5000) to tighten the "keep it lean" guideline; set higher to
    relax the constraint.
    """
    val = get_value("guardrails", "lint_max_body_chars", default=8000)
    try:
        return max(1, int(val))
    except (TypeError, ValueError):
        return 8000


def get_lint_duplicate_top_n() -> int:
    """Top N candidate skills the duplicate stage shortlists (feeds SL010's
    LLM confirmation and the SL012 degraded fallback).

    Reads ``guardrails.lint_duplicate_top_n`` (default 5). When linting a
    new skill, the linter compares its description against the top N most
    recent skills in the marketplace to flag possible near-duplicates.
    Increase to broaden the search scope at the cost of more comparisons.
    """
    val = get_value("guardrails", "lint_duplicate_top_n", default=5)
    try:
        return max(1, int(val))
    except (TypeError, ValueError):
        return 5


def get_lint_audit_min_interval_hours() -> int:
    """Minimum interval in hours between full-corpus lint audits (the
    ``/api/admin/store/lint-audit`` self-guard; not tied to any single rule).

    Reads ``guardrails.lint_audit_min_interval_hours`` (default 144 = 6 days).
    When a skill is updated, the linter may trigger a fresh audit. This
    threshold prevents audit fatigue by not re-auditing the same skill more
    frequently than this interval.
    """
    val = get_value("guardrails", "lint_audit_min_interval_hours", default=144)
    try:
        return max(1, int(val))
    except (TypeError, ValueError):
        return 144


# --- Distribution (signed-URL bucket mirror, three-plane wave 2-H) ------

_DISTRIBUTION_SIGNED_URL_MODES = ("auto", "on", "off")
_DEFAULT_DISTRIBUTION_OBJECT_STORE_PREFIX = "agnes/distribution"


def distribution_signed_urls_mode() -> str:
    """Signed-URL distribution mode: ``"auto"`` (default) | ``"on"`` | ``"off"``.

    Resolution: ``AGNES_DISTRIBUTION_SIGNED_URLS`` env (Terraform-friendly,
    overrides everything) > ``distribution.signed_urls`` in instance.yaml >
    default ``"auto"``. Mirrors :func:`get_slack_transport` /
    :func:`src.analytics_backend.resolve_analytics_backend_name`'s
    env-overrides-yaml shape.

    ``auto`` means "on when an object store is configured, off otherwise"
    (decided by :func:`src.object_store.object_store`, not here — this
    function only resolves the mode token). ``off`` is an explicit escape
    hatch that forces the app-served download path even with a store
    configured. An unrecognized token falls back to ``"auto"`` (logged) —
    a typo should degrade to the safe default, not crash sync/manifest
    building.
    """
    raw = os.environ.get("AGNES_DISTRIBUTION_SIGNED_URLS") or get_value("distribution", "signed_urls", default="auto")
    value = (raw or "auto").strip().lower()
    if value not in _DISTRIBUTION_SIGNED_URL_MODES:
        logger.warning(
            "invalid distribution.signed_urls mode %r (AGNES_DISTRIBUTION_SIGNED_URLS env var / "
            "instance.yaml::distribution.signed_urls) — falling back to 'auto'",
            value,
        )
        return "auto"
    return value


def distribution_object_store_config() -> Optional[dict]:
    """Resolved object-store connection config, or ``None`` when no bucket
    is configured (the store is simply not set up for this instance).

    Reads ``distribution.object_store.{endpoint_url,bucket,prefix,region,
    access_key_env,secret_key_env}`` from instance.yaml, with
    ``AGNES_DISTRIBUTION_OBJECT_STORE_*`` env vars winning per-field
    (same env-overrides-yaml shape as everything else in this module).

    Credentials are never stored directly in instance.yaml or read
    straight from an ``AGNES_DISTRIBUTION_OBJECT_STORE_ACCESS_KEY`` /
    ``..._SECRET_KEY`` env var — ``access_key_env`` / ``secret_key_env``
    (or their env-var overrides) name *other* environment variables that
    hold the actual credential, resolved via a plain ``os.environ.get``
    lookup. This mirrors the ``token_env`` indirection used throughout the
    codebase (see ``src/connection_resolver.py``, ``src/marketplace.py``)
    so secrets never round-trip through the instance.yaml editor.

    ``prefix`` defaults to ``"agnes/distribution"`` when unset.
    """
    endpoint_url = os.environ.get("AGNES_DISTRIBUTION_OBJECT_STORE_ENDPOINT_URL") or get_value(
        "distribution", "object_store", "endpoint_url", default=None
    )
    bucket = os.environ.get("AGNES_DISTRIBUTION_OBJECT_STORE_BUCKET") or get_value(
        "distribution", "object_store", "bucket", default=None
    )
    prefix = os.environ.get("AGNES_DISTRIBUTION_OBJECT_STORE_PREFIX") or get_value(
        "distribution", "object_store", "prefix", default=_DEFAULT_DISTRIBUTION_OBJECT_STORE_PREFIX
    )
    region = os.environ.get("AGNES_DISTRIBUTION_OBJECT_STORE_REGION") or get_value(
        "distribution", "object_store", "region", default=None
    )
    access_key_env = os.environ.get("AGNES_DISTRIBUTION_OBJECT_STORE_ACCESS_KEY_ENV") or get_value(
        "distribution", "object_store", "access_key_env", default=None
    )
    secret_key_env = os.environ.get("AGNES_DISTRIBUTION_OBJECT_STORE_SECRET_KEY_ENV") or get_value(
        "distribution", "object_store", "secret_key_env", default=None
    )

    bucket = (bucket or "").strip()
    if not bucket:
        return None

    access_key = os.environ.get(access_key_env) if access_key_env else None
    secret_key = os.environ.get(secret_key_env) if secret_key_env else None

    return {
        "bucket": bucket,
        "endpoint_url": (endpoint_url or "").strip() or None,
        "prefix": (prefix or "").strip() or _DEFAULT_DISTRIBUTION_OBJECT_STORE_PREFIX,
        "region": (region or "").strip() or None,
        "access_key": access_key,
        "secret_key": secret_key,
    }
