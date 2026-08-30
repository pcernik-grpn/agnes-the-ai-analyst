"""What did this semantic source actually SCAN?

Product-owner finding A17 on #1707 (2026-08-30 review, reproduced live): a
Snowflake semantic view existed, the role Agnes connects as held no privilege
on it, and ``SHOW SEMANTIC VIEWS`` therefore returned an empty list. Agnes
recorded ``last_sync_status='ok'``, owned zero models, and read green.

"There is nothing upstream" and "I cannot see it, and I am not saying so" are
different statements, and the second is a misconfiguration an admin has to
fix. Neither the sync status nor the owned-model count (#1821,
``src/semantic/ownership.py``) can tell them apart on its own: both are
identical in the two cases. Naming the scope is what closes the gap — an
admin reading ``ok · 0 models · scanned ESHOP_DEMO.RAW as ESHOP_DEMO_ROLE``
knows exactly which grant to check, where ``0 models`` alone gives them
nowhere to start. This is an addition to the count, not a replacement for it.

**Derived from the source's own config at read time, never stored.** The
scope is a function of the row (plus the connection it names), not of a
particular run, so it cannot go stale between syncs the way a recorded value
could; persisting it would also mean a new column on ``semantic_sources``, a
frozen pre-A3 DuckDB↔PG pair whose migration ladder is frozen. Each resolver
below reads the SAME config keys its fetch path reads and names that path, so
a key renamed upstream breaks in one place rather than drifting silently.

Dispatch mirrors :func:`src.semantic.transports.load_documents`: a ``git`` or
``upload`` source is scoped by its TRANSPORT (the adapter only ever sees
documents the transport already fetched), a ``connection`` source by its
ADAPTER, which owns the fetch. An adapter with no resolver reports ``None``
and every surface simply omits the scope, so a new adapter stays additive
here too.

Two invariants hold for every resolver: it never raises (one unreadable row
must not cost the list every other row's scope — same rule as
``owned_model_counts``), and it never carries a secret. Resolvers return
coordinates only — database, schema, role, project, catalog, repository —
and any URL or host they echo goes through :func:`_without_credentials`
first. Tokens, passwords and the env-var names that hold them are never read
here at all.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

#: The field name every surface publishes the scope under. One spelling, so a
#: rename is one edit rather than four.
SCAN_SCOPE_FIELD = "scan_scope"


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _without_credentials(value: str) -> str:
    """``value`` — a URL or a bare host — with any ``user:pass@`` userinfo,
    query and fragment removed.

    Structural rather than token-dependent, for the reason
    ``src.marketplace._strip_userinfo`` documents: the value most in need of
    sanitising is a repository URL that still embeds a PAT from before that
    fix, and the token we could redact against may already have been rotated.
    That helper is not reused as-is because it only strips userinfo when
    ``urlparse`` finds a hostname, which a scheme-less
    ``user:token@host/repo.git`` has none of — and a scan scope is rendered
    straight into an admin page.
    """
    value = _text(value)
    if not value:
        return ""
    value = value.split("#", 1)[0].split("?", 1)[0]
    scheme, sep, rest = value.partition("://")
    if not sep:
        scheme, rest = "", value
    authority, slash, path = rest.partition("/")
    if "@" in authority:
        # Partition on the LAST "@": userinfo may itself contain one.
        authority = authority.rpartition("@")[2]
    return f"{scheme}{sep}{authority}{slash}{path}"


def _cached(cache: Dict[str, Any], key: str, loader: Callable[[], Any]) -> Any:
    """Resolve ``key`` once per batch.

    Connection settings are per-instance, not per-source, and resolving them
    reads the connection registry and the vault — so a list of ten Snowflake
    sources must not mean ten resolutions. A failed load is cached as ``None``
    for the same reason: it will fail the same way for every other row.
    """
    if key not in cache:
        try:
            cache[key] = loader()
        except Exception as exc:  # noqa: BLE001 - unresolvable settings are "cannot say"
            logger.warning("Semantic scan scope: could not resolve %s settings: %s", key, exc)
            cache[key] = None
    return cache[key]


# ---------------------------------------------------------------------------
# Per-transport resolvers (kind='git' / kind='upload')
# ---------------------------------------------------------------------------


def _git_scope(config: Dict[str, Any], cache: Dict[str, Any]) -> Optional[str]:
    """Repository + ref — what ``transports._clone`` clones, minus the
    credential a URL may embed. No ``ref`` means git's own default branch,
    which is what ``--branch`` being omitted resolves to.
    """
    repo_url = _text(config.get("repo_url"))
    if not repo_url:
        return None
    ref = _text(config.get("ref"))
    return f"{_without_credentials(repo_url)} @ {ref or 'default branch'}"


def _upload_scope(config: Dict[str, Any], cache: Dict[str, Any]) -> Optional[str]:
    """How many documents were uploaded.

    ``load_documents`` reads ``config.documents or []``, so a missing key is
    not "unknown" for an upload source — it is a source carrying nothing,
    which is worth saying out loud.
    """
    documents = config.get("documents") or []
    return f"uploaded documents ({len(documents)})"


# ---------------------------------------------------------------------------
# Per-adapter resolvers (kind='connection' — the adapter owns the fetch)
# ---------------------------------------------------------------------------


def _native_scope(config: Dict[str, Any], cache: Dict[str, Any]) -> Optional[str]:
    """``NativeAdapter`` reads ``config.documents``, exactly as an upload
    source does — but a connection-kind row that carries no documents at all
    has no scope to state, rather than a scope of zero."""
    if not isinstance(config.get("documents"), list):
        return None
    return _upload_scope(config, cache)


def _snowflake_scope(config: Dict[str, Any], cache: Dict[str, Any]) -> Optional[str]:
    """Database, schema (or the whole database) and the ROLE Agnes connects as.

    The first three are the scope ``SnowflakeSemanticAdapter.extract`` builds
    its ``SHOW SEMANTIC VIEWS`` from (``config.{database, schema, like}``,
    database falling back to the connection's own); the role comes from the
    same ``resolve_snowflake_settings()`` the adapter passes to
    ``attach_snowflake``.

    The role is the finding. ``SHOW SEMANTIC VIEWS`` returns only what the
    connecting role can see, so a view it holds no grant on is invisible and
    indistinguishable from one that does not exist. An unconfigured instance
    reports the config-supplied part and stays silent about the role rather
    than inventing one — the sync itself raises there.
    """

    def _load() -> Any:
        # Imported at call time so a test patching the defining module reaches
        # this lookup — the same reason the adapter itself imports it here.
        from connectors.snowflake.settings import resolve_snowflake_settings

        return resolve_snowflake_settings()

    settings = _cached(cache, "snowflake", _load) or {}
    database = _text(config.get("database")) or _text(settings.get("database"))
    if not database:
        return None
    schema = _text(config.get("schema"))
    scope = f"{database}.{schema}" if schema else f"{database} (whole database)"
    like = _text(config.get("like"))
    if like:
        scope = f"{scope} matching '{like}'"
    if not settings:
        return scope
    role = _text(settings.get("role"))
    return f"{scope} as {role}" if role else f"{scope} as the connection's default role"


def _keboola_scope(config: Dict[str, Any], cache: Dict[str, Any]) -> Optional[str]:
    """The Keboola project this source reads.

    ``KeboolaMetastoreAdapter`` resolves its credentials per sync from scope
    alone (``connectors.keboola.semantic_layer._semantic_source_credentials``):
    ``config.connection_id`` names a registered connection whose ``config``
    carries the ``(project_id, project_name)`` binding the sync preflights
    against, and ``config.legacy_credentials`` means the environment pair. The
    stack URL and token that connection holds are deliberately not echoed —
    the project is the coordinate, and the rest is credential surface.
    """
    connection_id = _text(config.get("connection_id"))
    if connection_id:
        from src.repositories import source_connections_repo

        connection = source_connections_repo().get(connection_id)
        if connection is None:
            # The sync raises the same way; saying so here is what tells the
            # admin the scope is gone rather than empty.
            return f"Keboola connection {connection_id} (no longer registered)"
        connection_config = connection.get("config") or {}
        project_id = _text(connection_config.get("project_id"))
        project_name = _text(connection_config.get("project_name"))
        if project_id:
            return f"Keboola project {project_id}" + (f" ({project_name})" if project_name else "")
        return f"Keboola connection {_text(connection.get('name')) or connection_id} (no project bound)"
    if config.get("legacy_credentials"):
        return "Keboola project of the legacy environment credentials"
    return None


def _databricks_scope(config: Dict[str, Any], cache: Dict[str, Any]) -> Optional[str]:
    """Workspace + the Unity Catalog catalogs enumerated for metric views.

    ``DatabricksMetricViewAdapter.extract`` enumerates ``config.catalogs``,
    falling back to the connection's own ``catalogs``
    (``resolve_databricks_settings()``); the workspace host is the same
    settings' ``host``. No catalog at all is a real state — the sync refuses
    — so it is stated rather than hidden.
    """

    def _load() -> Any:
        # Call-time import, same reason as the Snowflake resolver above.
        from connectors.databricks.semantic_layer import resolve_databricks_settings

        return resolve_databricks_settings()

    settings = _cached(cache, "databricks", _load) or {}
    catalogs = config.get("catalogs") or settings.get("catalogs") or []
    if isinstance(catalogs, str):
        catalogs = catalogs.split(",")
    catalogs = [_text(c) for c in catalogs if _text(c)]
    host = _without_credentials(_text(settings.get("host")))

    if catalogs:
        noun = "catalog" if len(catalogs) == 1 else "catalogs"
        scope = f"Unity Catalog {noun} {', '.join(catalogs)}"
    elif host:
        scope = "Unity Catalog (no catalog configured)"
    else:
        return None
    return f"{scope} on {host}" if host else scope


#: ``kind='connection'`` adapter -> resolver. An adapter absent from this map
#: reports ``None``; every surface omits the scope rather than guessing.
_ADAPTER_SCOPES: Dict[str, Callable[[Dict[str, Any], Dict[str, Any]], Optional[str]]] = {
    "native": _native_scope,
    "snowflake_semantic": _snowflake_scope,
    "keboola_metastore": _keboola_scope,
    "databricks_metric_views": _databricks_scope,
}

#: ``kind`` -> resolver, for the two transports that fetch before any adapter
#: sees a document.
_TRANSPORT_SCOPES: Dict[str, Callable[[Dict[str, Any], Dict[str, Any]], Optional[str]]] = {
    "git": _git_scope,
    "upload": _upload_scope,
}


def scan_scope(source: Dict[str, Any], *, cache: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """What this source scans, as one short human-readable line — or ``None``
    when nothing faithful can be said (an adapter with no resolver, or a
    config too incomplete to name a scope).

    ``cache`` is a caller-owned dict shared across one batch; see
    :func:`_cached`.
    """
    cache = {} if cache is None else cache
    try:
        kind = _text(source.get("kind"))
        config = source.get("config") or {}
        if not isinstance(config, dict):
            return None
        resolver = _TRANSPORT_SCOPES.get(kind)
        if resolver is None and kind == "connection":
            resolver = _ADAPTER_SCOPES.get(_text(source.get("adapter")) or "native")
        if resolver is None:
            return None
        return resolver(config, cache)
    except Exception as exc:  # noqa: BLE001 - one bad row must not blank the list
        logger.warning(
            "Semantic source %s: cannot derive what it scans, so the scan scope is omitted: %s",
            source.get("id"),
            exc,
        )
        return None


def scan_scopes(sources: Iterable[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    """``{source id: scan scope}``, resolving each connection's settings once
    for the whole batch."""
    cache: Dict[str, Any] = {}
    return {s.get("id"): scan_scope(s, cache=cache) for s in sources}


def with_scan_scope(sources: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The same rows, each with :data:`SCAN_SCOPE_FIELD` added.

    Returns copies — the repository row a caller passed in is left untouched,
    so annotating for a response can never leak a derived field back into a
    write path that spreads the row (``{**source, …}``). Same contract as
    ``src.semantic.ownership.with_owned_model_count``, which it is normally
    composed with.
    """
    sources = list(sources)
    scopes = scan_scopes(sources)
    return [{**s, SCAN_SCOPE_FIELD: scopes.get(s.get("id"))} for s in sources]
