"""Keboola semantic layer (Metastore) -> Agnes metric_definitions importer.

Design: docs/superpowers/specs/2026-07-15-keboola-semantic-layer-importer-design.md

Maps a Keboola project's semantic-layer metrics (bound to Storage tables via
`semantic-dataset`, annotated with `semantic-constraint` rules) into Agnes's
`metric_definitions` table. Runs on a schedule (see
app/api/keboola_semantic_layer_refresh.py); this module has no HTTP-layer
concerns of its own.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

from src.sql_ident import quote_ident

logger = logging.getLogger(__name__)


class MasterTokenRequiredError(RuntimeError):
    """The configured Keboola token is not a master (owner) Storage API token.

    Verified live (2026-07-15): the Metastore API rejects non-master tokens
    with an opaque ``401 {"exception": "Failed to create project scope"}``
    regardless of the token's bucket permissions. This check turns that into
    an actionable error before any Metastore call is made.
    """


def check_master_token(info: dict) -> None:
    """Raise MasterTokenRequiredError unless ``info`` (a ``verify_token()``
    payload) describes a master (owner) Storage API token.

    Split out of ``require_master_token`` so a caller that already holds the
    ``verify_token()`` response (the sync loop needs it for the project
    identity anyway) can run the same check without a second API round-trip.
    """
    if not info.get("isMasterToken"):
        raise MasterTokenRequiredError(
            "Keboola semantic layer sync requires a master (owner) Storage "
            "API token; the configured token is not a master token. The "
            "Metastore API rejects non-master tokens with an opaque "
            "'Failed to create project scope' error regardless of bucket "
            "permissions — use the project's owner token instead."
        )


def require_master_token(storage_client) -> None:
    """Raise MasterTokenRequiredError unless the client's token is a master token.

    `storage_client` is a `connectors.keboola.storage_api.KeboolaStorageClient`
    (or any object exposing a compatible `verify_token() -> dict` method).
    """
    check_master_token(storage_client.verify_token())


# Cap on how many unresolved table ids a run names — in a log line and in the
# per-source result the admin page renders. A model with hundreds of unregistered
# datasets should still produce a readable line; the count next to it stays exact.
_MAX_REPORTED_UNRESOLVED_TABLES = 20


def _upstream_error_code(exc: Exception) -> str:
    """Classify an upstream Keboola failure for the endpoint's status mapping.

    ``upstream_client_error`` — Keboola understood the request and refused it
    (4xx): an invalid/expired token, or a stack URL pointing at a project the
    token doesn't belong to. The admin fixes that, so the endpoint answers
    4xx. ``upstream_error`` — unreachable, timed out, or 5xx: a real gateway
    failure, and the only case that still deserves a 502.
    """
    from connectors.keboola.storage_api import is_upstream_client_error

    return "upstream_client_error" if is_upstream_client_error(exc) else "upstream_error"


def table_lookup_from_registry(rows: list[dict]) -> dict[tuple[str, str], str]:
    """Build {(bucket, source_table): agnes_view_name} from table_registry
    rows (from `table_registry_repo().list_by_source("keboola")`).

    Keys are normalized with `normalize_source_table`, because
    :func:`resolve_table_name` builds its lookup key by splitting a Keboola
    tableId on the last dot — always a BARE table name. A row registered by the
    pre-fix wizard stores the full `<bucket>.<table>` in `source_table`, so
    without normalizing here the key would be
    `("in.c-main", "in.c-main.orders")` while every lookup asks for
    `("in.c-main", "orders")`: a permanent miss, and the table silently receives
    no descriptions, metrics or glossary links. The export and view paths already
    strip the prefix at use; this is the sibling site they missed
    (Devin Review on #1189).
    """
    from connectors.keboola.storage_api import normalize_source_table

    lookup: dict[tuple[str, str], str] = {}
    for row in rows:
        bucket = row.get("bucket")
        source_table = row.get("source_table")
        name = row.get("name")
        if bucket and source_table and name:
            lookup[(bucket, normalize_source_table(bucket, source_table))] = name
    return lookup


def resolve_table_name(table_id: str, lookup: dict[tuple[str, str], str]) -> str | None:
    """Resolve a Keboola tableId ('bucket.table') to its Agnes
    table_registry view name, or None if that table isn't registered.

    Bucket ids themselves contain dots (e.g. `in.c-example_source`), so the
    tableId must be split on the LAST dot to isolate the table name —
    splitting on the first dot would misparse the bucket.
    """
    if "." not in table_id:
        return None
    bucket, _, source_table = table_id.rpartition(".")
    return lookup.get((bucket, source_table))


# Matches `<alias>."column"` or `<alias>.column` — qualified-column shapes
# observed in live Keboola semantic-metric.sql fragments. Verified live
# (2026-07-15): single-dataset expressions are always bare column references
# (`SUM("amount")`); an alias-qualified reference only appears when the
# expression crosses into a JOINed dataset via semantic-relationship data
# this importer does not have (relationship support is out of scope for
# v1) — so any match here means "skip, cannot safely compose."
_ALIAS_QUALIFIER_RE = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_"])')
# Single-quoted SQL string literal (handles '' escapes).
_SQL_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
# Double-quoted SQL identifier (handles "" escapes).
_SQL_IDENTIFIER_RE = re.compile(r'"(?:[^"]|"")*"')


def _mask_quoted_regions(expression: str) -> str:
    """Blank the CONTENT of double-quoted identifiers and single-quoted string
    literals, keeping the surrounding SQL structure intact, so a dotted enum
    value (`'in.progress'`), a dotted column name (`"total.amount"`), or a
    double-hyphen inside a quoted region is not mistaken for an alias qualifier
    or a line comment.

    Identifiers are masked FIRST: a single quote inside an identifier
    (e.g. `"col'name"`) would otherwise start a spurious string-literal match
    that swallows a following real literal and re-exposes its contents (a
    dotted value or a `--`), causing a valid single-table metric to be skipped.
    """
    masked = _SQL_IDENTIFIER_RE.sub('""', expression)
    masked = _SQL_STRING_LITERAL_RE.sub("''", masked)
    return masked


def references_foreign_alias(expression: str) -> bool:
    """True if `expression` qualifies any column with an `<alias>.` prefix.

    See _ALIAS_QUALIFIER_RE docstring for why this indicates a multi-dataset
    JOIN this importer cannot safely compose in v1.

    Quoted regions are masked first: a dotted value inside a single-quoted
    literal (`WHEN "status" = 'in.progress'`) or a dotted column name inside a
    quoted identifier (`"total.amount"`) is data, not an alias reference, and
    must not cause a valid single-table metric to be skipped.
    """
    return bool(_ALIAS_QUALIFIER_RE.search(_mask_quoted_regions(expression)))


def has_embedded_sql_comment(expression: str) -> bool:
    """True if `expression` contains a `--` SQL line-comment marker outside
    any quoted literal or identifier.

    Verified live (2026-07-15) against a real project: some real Keboola
    metric expressions carry a trailing `--` comment as a free-text author
    note (e.g. flagging a table that doesn't exist in this project, or a
    WHERE condition the author never actually applied to the formula).
    Composing a `SELECT <expression> FROM <table> AS t` statement naively
    appends the FROM clause AFTER such an expression — SQL then treats
    everything from
    `--` onward (including the appended FROM clause) as part of the
    comment, silently dropping it and breaking the query. Confirmed live:
    DuckDB raises a binder error ("FROM clause is missing") rather than
    returning a wrong number, but the query is still unusable — and the
    comment text itself often signals the metric is genuinely incomplete
    for this table. Per this importer's "skip rather than guess" contract,
    any embedded comment means skip, never strip-and-compose.
    """
    return "--" in _mask_quoted_regions(expression)


_QUOTED_REGION_RE = re.compile(f"({_SQL_IDENTIFIER_RE.pattern}|{_SQL_STRING_LITERAL_RE.pattern})")


def _sub_outside_quotes(pattern: str, repl: str, expression: str) -> str:
    """Like re.sub(pattern, repl, expression), but never touches text inside
    a single-quoted string literal or a double-quoted identifier.

    Splits on quoted regions (kept verbatim as a capture group, so they land
    at odd indices) and only substitutes in the unquoted segments — unlike
    _mask_quoted_regions, this preserves the exact original text, so it's
    safe to use for a rewrite (not just detection).
    """
    parts = _QUOTED_REGION_RE.split(expression)
    for i in range(0, len(parts), 2):
        parts[i] = re.sub(pattern, repl, parts[i])
    return "".join(parts)


def compose_sql(expression: str, table_name: str) -> str:
    """Compose a full, runnable metric_definitions.sql from a Keboola
    semantic-metric.sql fragment (a bare aggregation expression, verified
    live to never be a full query) and the resolved Agnes table_registry
    view name.

    Callers MUST check BOTH `references_foreign_alias(expression)` and
    `has_embedded_sql_comment(expression)` first and skip the metric if
    either is True — this function does not itself guard against those
    cases. A foreign-alias reference needs a JOIN this importer can't
    compose; a trailing `--` comment would swallow the appended FROM clause.
    The projector's ``_bind_metric`` (and the coverage predictor
    ``_predict_metric_binding``) perform both checks before calling this.
    """
    return f"SELECT {expression} FROM {quote_ident(table_name)} AS t"


def try_join_composition(
    expression: str,
    dataset_table_id: str,
    table_lookup: dict[tuple[str, str], str],
    relationship_lookup: dict[str, list[dict]],
    column_lookup: dict[str, set[str]],
) -> tuple[Optional[dict], Optional[str]]:
    """Attempt to resolve a foreign-alias-referencing metric expression into
    a JOIN. Returns (fields, None) with 'table_name' / 'tables' / 'sql'
    keys set on success, or (None, skip_reason) — never raises, every
    failure mode is a specific skip_reason (docs/superpowers/specs/
    2026-07-17-keboola-relationship-metrics-design.md — "skip and count,
    never guess"). Any failure not covered by a resolve_relationship()
    skip_reason falls back to "foreign_alias_reference", the pre-existing
    generic reason — so this function never introduces a regression for a
    metric it can't fully resolve.
    """
    relationship, skip_reason = resolve_relationship(dataset_table_id, relationship_lookup)
    if relationship is None:
        return None, skip_reason

    # These two failures are AGNES-side, not definition defects: the metric's
    # SQL is fine, one of the tables it joins simply is not registered here
    # (or is registered without its columns). Reporting them as
    # `foreign_alias_reference` put them in the coverage report's "blocked by
    # their own definition" bucket, telling the admin that registering a table
    # would not help — when registering a table is exactly the fix.
    # Deliberately a new reason rather than a new counter: it folds into
    # `skipped_foreign_alias` below, so the published sync counters are
    # unchanged. (Devin Review on this PR.)
    table_name = resolve_table_name(dataset_table_id, table_lookup)
    joined_table_name = resolve_table_name(relationship["from"], table_lookup)
    if table_name is None or joined_table_name is None:
        return None, "unresolved_joined_table"

    to_columns = column_lookup.get(table_name)
    from_columns = column_lookup.get(joined_table_name)
    if not to_columns or not from_columns:
        return None, "unresolved_joined_table"

    alias_sides = resolve_join_aliases(relationship["on"], from_columns, to_columns)
    if alias_sides is None:
        return None, "foreign_alias_reference"
    to_alias, from_alias = alias_sides

    sql = compose_join_sql(expression, table_name, joined_table_name, relationship["on"], to_alias, from_alias)
    return {"table_name": table_name, "tables": [table_name, joined_table_name], "sql": sql}, None


def _default_keboola_connection() -> Optional[dict]:
    """The Keboola ``source_connections`` row that acts as "the default one":
    the row flagged ``is_default``, or — when none is flagged — the first
    Keboola connection, with admin-created rows preferred over
    login-auto-provisioned ones.

    Shared by credential resolution, the multi-source ordering (default
    connection syncs first) and NULL-``source_ref`` adoption (only the default
    connection adopts legacy, unstamped rows), so all three agree on which
    connection is "the default".
    """
    from app.auth.keboola_provisioning import LOGIN_PROVISIONED_BY
    from src.repositories import source_connections_repo

    conns_repo = source_connections_repo()
    conn = conns_repo.get_default("keboola")
    if conn is not None:
        return conn
    candidates = conns_repo.list(source_type="keboola")
    # ``list`` orders by name, and the multi-project login flow names each
    # auto-provisioned row after its Keboola project — without this
    # preference a user's project whose name happens to sort first would
    # silently become the effective default on any instance that never
    # flagged one, redirecting credential resolution and legacy-row
    # adoption to a connection the admin never chose (Devin Review on
    # PR #1328, ninth round). Auto rows only qualify when nothing
    # admin-created exists.
    admin_created = [c for c in candidates if (c.get("config") or {}).get("provisioned_by") != LOGIN_PROVISIONED_BY]
    pool = admin_created or candidates
    return pool[0] if pool else None


def _resolve_keboola_credentials(
    explicit_url: Optional[str],
    explicit_token: Optional[str],
) -> tuple[str, str]:
    """Resolve (stack_url, token) for the semantic-layer sync — see
    ``_resolve_keboola_credentials_slot`` for the full precedence; this wrapper
    drops the slot label for callers that only need the credentials."""
    url, token, _slot = _resolve_keboola_credentials_slot(explicit_url, explicit_token)
    return url, token


def _resolve_keboola_credentials_slot(
    explicit_url: Optional[str],
    explicit_token: Optional[str],
) -> tuple[str, str, str]:
    """Resolve (stack_url, token, slot) for the semantic-layer sync.

    ``slot`` names WHERE the credentials came from — ``"explicit"``,
    ``"legacy_env"``, ``"connection"`` or ``"none"``. Only the
    ``"connection"`` slot has a connection identity, so only it may stamp
    rows with that connection's ``source_ref`` (spec §3, fallback bullet).

    Precedence: explicit args > legacy ``KEBOOLA_STACK_URL``/
    ``KEBOOLA_STORAGE_TOKEN`` (env or system-secrets vault) > the default
    (or first, if none is marked default) named Keboola ``source_connections``
    row — the same connection the "Add Keboola project" admin wizard
    (``/admin/data-sources``) manages.

    Verified live (2026-07-22, agnes-dev): before this fallback existed, an
    instance that connected a Keboola project only through that wizard —
    never setting the legacy env var — silently failed every semantic-layer
    sync with "credentials not configured", even though the same
    connection's regular table syncs and its own ``/test`` endpoint both
    resolve their token off it fine. Mirrors
    ``app/api/admin_source_connections.py::_resolve_token``'s own
    vault-secret-then-token_env precedence for a single connection.

    Returns ("", "") if nothing resolves — callers treat that as the
    existing "not configured" error.
    """
    if explicit_url and explicit_token:
        return explicit_url, explicit_token, "explicit"

    from app.datasource_secrets import datasource_secret

    url = explicit_url or os.environ.get("KEBOOLA_STACK_URL", "")
    token = explicit_token or datasource_secret("KEBOOLA_STORAGE_TOKEN") or ""
    if url and token:
        return url, token, "legacy_env"

    from src.repositories import connection_secrets_repo

    conn = _default_keboola_connection()
    if conn is None:
        return url, token, "none"

    conn_url = (conn.get("config") or {}).get("stack_url", "")
    conn_token = ""
    try:
        secrets = connection_secrets_repo()
        if secrets.has(conn["id"]):
            conn_token = secrets.get(conn["id"]) or ""
    except Exception:
        conn_token = ""
    if not conn_token:
        # BEHAVIOUR CHANGE, stated rather than slipped in: this read was
        # ungated, so an instance whose `token_env` names something outside
        # the config-secret allowlist (`AGNES_CONFIG_SECRET_ENVS` ∪
        # `AGNES_REMOTE_ATTACH_TOKEN_ENVS`; default: KBC_TOKEN,
        # KBC_STORAGE_TOKEN, KEBOOLA_STORAGE_TOKEN, …) stops syncing on
        # upgrade until the name is added. That is the right side of the
        # trade — the ungated read let an admin point a connection at any host
        # env var and have its value sent to the stack as a token — and the
        # remedy is one operator-set variable, logged by name when it bites.
        # Same gate as `_connection_storage_token` and as `/test` / `/tables`.
        # An admin can point `token_env` at any name, so an ungated read makes
        # a connection row a way to send an arbitrary host environment
        # variable to the configured stack as a Storage token. Pre-existing
        # rather than introduced here, but leaving one of two reads in this
        # module gated and the other not is the shape that gets the gate
        # removed later as "inconsistent". (Devin Review on this PR.)
        conn_token = _token_from_env(conn)

    # url/token here are at most a PARTIAL legacy pair (the full-pair case
    # returned above) — mixing that partial half with the named connection's
    # other half could pair one project's token with a different project's
    # stack URL. Only a full legacy pair counts as "legacy resolved"; a
    # partial one falls through to the named connection as a coherent pair.
    return conn_url, conn_token, "connection"


# Every per-source counter the sync reports. The orchestrator sums these
# across sources for the top-level aggregate, so a new counter only has to be
# added here to appear in both the per-source dict and the total.
_COUNTER_KEYS = (
    "created_or_updated",
    "pruned",
    "skipped_unresolved_table",
    "skipped_foreign_alias",
    "skipped_embedded_comment",
    "skipped_ambiguous_relationship",
    "skipped_unsupported_relationship_type",
    "skipped_unverified_relationship_direction",
    "skipped_conflict",
    "glossary_created_or_updated",
    "glossary_pruned",
    "skipped_missing_term",
    "skipped_duplicate_project",
)


def _empty_counters() -> dict[str, int]:
    return {key: 0 for key in _COUNTER_KEYS}


def _in_scope(row: dict, scope_refs: set, adopt_null: bool) -> bool:
    """True if an existing metric/glossary row belongs to the source currently
    syncing — i.e. is inside that source's prune scope.

    ``scope_refs`` is usually just the source's own ``source_ref``; the legacy
    fallback path widens it to "NULL or the default connection id" so a
    downgrade (last master token removed) still prunes the rows it used to own
    even when the credentials it resolved have no connection identity to stamp.

    Only imported rows are ever in scope (manual/yaml rows are never touched).
    A row stamped with a DIFFERENT connection's ``source_ref`` is out of scope
    and is therefore left intact — including when that connection no longer
    exists (orphaned-but-intact beats silently deleted).
    """
    if row.get("source") != "keboola_semantic_layer":
        return False
    ref = row.get("source_ref")
    return ref in scope_refs or (ref is None and adopt_null)


def _project_key(url: str, token_info: dict) -> Optional[tuple[str, Any]]:
    """Identity of the upstream Keboola project behind (stack URL, token):
    (stack host, token owner id).

    Nothing stops an admin from registering the same project under two
    connections (the uniqueness constraint is on connection *name*). Two
    connections syncing one project would flip-flop ``source_ref`` ownership
    and cross-wipe each other every run, so the orchestrator dedupes on this
    key and skips the later connection.

    Returns ``None`` when the token's ``owner.id`` is missing — a
    ``verify_token`` response without an owner id gives no reliable identity
    to dedupe on, and treating a missing id as an identity (``(host, None)``)
    would collide any two such projects on the same stack, silently skipping
    the second one as a spurious "duplicate". Callers must treat ``None`` as
    "not dedupable" and sync the source normally rather than folding it into
    the seen-keys set.
    """
    from urllib.parse import urlparse

    host = urlparse(url).netloc or url
    owner = token_info.get("owner") or {}
    owner_id = owner.get("id")
    if owner_id is None:
        return None
    return host, owner_id


def _enumerate_master_sources() -> list[dict]:
    """Every Keboola connection that has a master (owner) token in the vault,
    as ``[{"connection_id", "name", "stack_url", "token"}]``.

    Ordered default connection first, then by connection id — a deterministic
    order so first-claim outcomes for same-run name conflicts are stable
    across runs. Connections without a ``stack_url`` are unusable and skipped.
    """
    from app.api.admin_source_connections import master_secret_key
    from src.repositories import connection_secrets_repo, source_connections_repo

    secrets = connection_secrets_repo()
    sources: list[dict] = []
    for conn in source_connections_repo().list(source_type="keboola"):
        try:
            token = secrets.get(master_secret_key(conn["id"])) or ""
        except Exception:
            logger.warning("Could not read master token for connection %s", conn["id"])
            continue
        if not token:
            continue
        stack_url = (conn.get("config") or {}).get("stack_url") or ""
        if not stack_url:
            logger.warning(
                "Keboola connection %s has a master token but no stack_url; skipping",
                conn["id"],
            )
            continue
        config = conn.get("config") or {}
        sources.append(
            {
                "connection_id": conn["id"],
                "name": conn.get("name") or conn["id"],
                "stack_url": stack_url,
                "token": token,
                # The project this connection is bound to, when known. Carried
                # so the sync can refuse to import a DIFFERENT project's
                # semantic layer under this connection's name — the token
                # preflight already fetches the owner, so the check is free.
                "project_id": config.get("project_id"),
                "project_name": config.get("project_name") or "",
            }
        )

    default_conn = _default_keboola_connection()
    default_id = default_conn["id"] if default_conn else None
    sources.sort(key=lambda s: (s["connection_id"] != default_id, s["connection_id"]))
    return sources


def _store_ossie_documents(documents: list[str], *, source: str, source_ref: Optional[str]) -> list[dict]:
    """Validate and store composed Ossie documents into ``semantic_models``,
    returning the parsed ``document_json`` of every document kept.

    The full-fidelity document is the canon: it is stored whole (validate,
    hash, upsert-if-changed, prune-scoped-to-this-source) and queryable via the
    semantic-model REST/CLI/MCP surfaces. The returned parsed documents are
    what the caller then projects into the flat query tables
    (``metric_definitions`` / ``glossary_terms`` / ``column_metadata``) via
    ``src.semantic.projection.project_document`` — the SINGLE writer of those
    tables since the flat-table cutover. (Before the cutover this function
    deliberately stored but did not project, because a legacy flat composer in
    ``_sync_one_source`` still owned those tables and two writers raced for one
    metric name; that composer is gone and the projector now owns them
    outright — see ``tests/test_semantic_layer_cutover_parity.py``.)
    """
    import hashlib
    from datetime import datetime, timezone

    from src.repositories import semantic_model_repo
    from src.semantic.document_validation import validate_document
    from src.semantic.projection import _model_key

    repo = semantic_model_repo()
    existing_by_slug = {m["slug"]: m for m in repo.list_all(source=source, source_ref=source_ref)}
    keep_slugs: list[str] = []
    parsed_documents: list[dict] = []

    for text in documents:
        result = validate_document(text)
        if not result.ok:
            # No slug to key storage on or protect from prune — logged and
            # dropped, consistent with import_documents' own handling of an
            # invalid document.
            logger.warning(
                "Keboola semantic layer: composed Ossie document failed validation for source %s: %s",
                source_ref,
                "; ".join(result.errors),
            )
            continue
        models = (result.parsed or {}).get("semantic_model") or []
        slug = models[0].get("name") if models else None
        if not slug:
            continue
        if slug in keep_slugs:
            # A model NAME is not unique upstream, and the slug is this
            # table's storage key — two models named the same upserted onto
            # ONE row, so the second document silently replaced the first (and
            # `keep_slugs` listed the name twice, hiding it from the prune
            # too). Disambiguate with the model's own stable Metastore id,
            # which the adapter carries. The first model keeps the clean slug,
            # so an existing install's export URL does not move.
            disambiguator = _model_key(models[0]) or str(len(keep_slugs))
            logger.warning(
                "Keboola semantic layer: two composed models share the name %r for source %s; "
                "storing the later one as %r so neither document is lost.",
                slug,
                source_ref,
                f"{slug}-{disambiguator}",
            )
            slug = f"{slug}-{disambiguator}"
        keep_slugs.append(slug)
        if result.parsed is not None:
            parsed_documents.append(result.parsed)

        content_hash = hashlib.sha256(text.encode()).hexdigest()
        existing = existing_by_slug.get(slug)
        if existing is not None and existing.get("content_hash") == content_hash:
            continue

        repo.upsert(
            id="/".join([source, source_ref or "_", slug]),
            slug=slug,
            name=slug,
            description=None,
            document=text,
            document_json=result.parsed,
            spec_version=result.spec_version,
            content_hash=content_hash,
            source=source,
            source_ref=source_ref,
            status="valid",
            validation_errors=None,
            validated_at=datetime.now(timezone.utc),
        )

    repo.delete_missing(source=source, source_ref=source_ref, keep_slugs=keep_slugs)
    return parsed_documents


def _sync_one_source(
    url: str,
    token: str,
    source_ref: Optional[str],
    *,
    adopt_null: bool,
    prune_scope_refs: Optional[set] = None,
    seen_project_keys: Optional[set[tuple[str, Any]]] = None,
    expected_project: Optional[tuple[Any, str]] = None,
) -> dict:
    """Fetch ONE Keboola project's semantic layer (Metastore) and upsert it
    into Agnes's metric_definitions / glossary_terms tables, pruning stale
    rows *of this source only* that no longer exist upstream.

    ``source_ref`` is the originating ``source_connections.id`` stamped onto
    every written row (None when the credentials have no connection identity).
    ``adopt_null`` lets the default connection claim legacy rows written
    before provenance existed (``source_ref IS NULL``).
    ``prune_scope_refs`` widens the owned/prunable set beyond ``source_ref``
    (the legacy fallback owns "NULL or the default connection id" while
    stamping only what the resolved credentials justify); it defaults to
    ``{source_ref}``.

    ``seen_project_keys``, when given, is the set of project identities
    already synced in this run: if this source resolves to one of them it
    short-circuits as ``skipped_duplicate_project`` before touching any row;
    otherwise its own identity is added to the set.

    ``expected_project`` is the ``(project_id, project_name)`` the connection
    is bound to. When the token turns out to open a DIFFERENT project the
    source fails with ``project_mismatch`` instead of importing, because the
    alternative is silent and expensive: another project's metrics land under
    this connection's name and its prune scope, so the next sync of the right
    project deletes them again. The preflight already fetched the owner, so
    the check costs nothing.

    Raises MasterTokenRequiredError if the token is not a master token (see
    require_master_token) — a configuration error the caller surfaces loudly
    (the endpoint maps it to a 400) rather than swallowing into the result.
    """

    import requests

    from connectors.keboola.metastore_client import MetastoreApiError, MetastoreClient
    from connectors.keboola.storage_api import KeboolaStorageClient, StorageApiError
    from src.repositories import glossary_repo, metric_repo, table_registry_repo

    scope_refs: set = set(prune_scope_refs) if prune_scope_refs is not None else {source_ref}

    storage_client = KeboolaStorageClient(url=url, token=token)
    # A Storage API outage during the master-token preflight must abort with a
    # structured error, not an unhandled 500 — same defense the Metastore
    # fetch below gets. MasterTokenRequiredError is intentionally NOT caught:
    # it is a configuration error the endpoint surfaces as a 400.
    try:
        token_info = storage_client.verify_token()
    except (StorageApiError, requests.RequestException) as e:
        logger.error("Keboola Storage API preflight (verify_token) failed: %s", e)
        return {
            "status": "error",
            "error": f"Storage API preflight failed: {e}",
            "code": _upstream_error_code(e),
            "source_ref": source_ref,
        }
    check_master_token(token_info)

    if expected_project is not None:
        expected_id, expected_name = expected_project
        actual_id = (token_info.get("owner") or {}).get("id")
        # Compared as text for the same reason the API-layer check is
        # (app/api/admin_source_connections.py): `expected_id` comes straight
        # out of a JSON config column on either backend, so a 5947 returning
        # as "5947" would fail a correctly-configured project forever — and
        # here the only way out is re-pointing the connection to another
        # stack. Normalising in one layer and not the other was the gap
        # (Devin Review on #1242).
        if expected_id is not None and actual_id is not None and str(actual_id) != str(expected_id):
            logger.error(
                "Keboola semantic layer: connection %s is bound to project %s but its master "
                "token opens project %s; refusing to import one project's semantic layer under "
                "another's name",
                source_ref,
                expected_id,
                actual_id,
            )
            actual_name = (token_info.get("owner") or {}).get("name") or "unnamed"
            return {
                "status": "error",
                "error": (
                    f"Master token belongs to Keboola project {actual_id} ({actual_name}), but this "
                    f"connection is bound to project {expected_id} ({expected_name or 'unnamed'}). "
                    "Replace the master token with one from the bound project, or connect the other "
                    "project as its own data source."
                ),
                "code": "project_mismatch",
                "source_ref": source_ref,
            }

    project_key = _project_key(url, token_info)
    # A missing owner id (project_key is None) gives no reliable identity to
    # dedupe on — two distinct projects lacking an owner id must NOT collide
    # on a shared "no identity" bucket and silently skip the second one.
    # Only a real, comparable identity participates in the seen-keys set.
    if seen_project_keys is not None and project_key is not None:
        if project_key in seen_project_keys:
            logger.warning(
                "Keboola semantic layer: connection %s resolves to a project already "
                "synced in this run; skipping to avoid cross-wiping its rows",
                source_ref,
            )
            return {
                "status": "skipped",
                **_empty_counters(),
                "skipped_duplicate_project": 1,
                "source_ref": source_ref,
                "project_key": project_key,
            }
        seen_project_keys.add(project_key)

    metastore = MetastoreClient(url=url, token=token)

    # A Metastore outage / 401 / 5xx must abort the whole run with a logged,
    # structured error — never propagate as an unhandled 500 and never reach
    # the prune loop (which would delete against an empty seen_ids). Mirrors
    # the defensive fetch-wrapping in app/api/bq_metadata_refresh.py.
    try:
        models = metastore.list_items("semantic-model")
    except (MetastoreApiError, requests.RequestException) as e:
        logger.error("Keboola Metastore fetch failed (semantic-model): %s", e)
        return {
            "status": "error",
            "error": f"Metastore fetch failed: {e}",
            "code": _upstream_error_code(e),
            "source_ref": source_ref,
        }

    empty_result = {"status": "ok", **_empty_counters(), "source_ref": source_ref, "project_key": project_key}
    if not models:
        return empty_result

    # Compose full-fidelity Ossie documents (one per model) and store them
    # whole under source="keboola_metastore" — nothing upstream is discarded,
    # even the fields the flat projection has no column for (per-field
    # descriptions, the declared SQL dialect, relationships, ai.keywords,
    # constraints, glossary terms — see connectors/keboola/semantic_ossie.py).
    # The stored documents are then PROJECTED into the flat query tables by
    # src.semantic.projection.project_document — the SINGLE writer of
    # metric_definitions / glossary_terms / column_metadata since the
    # flat-table cutover (this function's own legacy flat composer was removed;
    # parity is pinned in tests/test_semantic_layer_cutover_parity.py). The
    # adapter runs its own MetastoreClient fetch — the self-contained "hand it
    # connection config, get documents back" contract every semantic-source
    # adapter uses.
    try:
        from connectors.keboola.semantic_ossie import KeboolaMetastoreAdapter

        ossie_documents = KeboolaMetastoreAdapter().extract({"url": url, "token": token})
    except Exception as e:
        # Composition IS the write now — there is no separate flat sync to fall
        # back to — so a failure here is a hard, structured error, never
        # swallowed. Reaching the prune below on a failed compose would delete
        # against an empty projection.
        logger.exception("Keboola semantic layer: Ossie document composition failed for source %s", source_ref)
        return {
            "status": "error",
            "error": f"Ossie document composition failed: {e}",
            "code": _upstream_error_code(e),
            "source_ref": source_ref,
        }

    parsed_documents = _store_ossie_documents(ossie_documents, source="keboola_metastore", source_ref=source_ref)

    # `_store_ossie_documents` logs-and-drops any single composed document
    # that fails schema validation (no slug to key storage on or protect from
    # its own prune). When that happens the merged model list below is a
    # PARTIAL view of what the adapter actually composed — projecting it with
    # pruning on would delete the dropped model's own previously-written rows,
    # which upstream never asked to have removed (mirrors the retired
    # "fetch all models before writing anything" invariant). `partial=True`
    # NARROWS the projector's prune to the models this projection actually
    # carried rather than skipping it wholesale, so a model that IS here and
    # genuinely lost a metric upstream is still reconciled this pass while the
    # dropped model's rows stay out of reach.
    partial_composition = len(parsed_documents) < len(ossie_documents)
    if partial_composition:
        logger.warning(
            "Keboola semantic layer: %d of %d composed document(s) failed validation and were "
            "dropped this pass (source_ref=%s); narrowing the prune to the models that survived, "
            "so the dropped model(s)' previously-written rows are left intact.",
            len(ossie_documents) - len(parsed_documents),
            len(ossie_documents),
            source_ref,
        )

    # Project the stored documents as ONE merged model list. project_document's
    # prune is scoped to (source, source_ref) so it can only touch this
    # project's own rows, and safe_prune keeps an empty-but-valid upstream from
    # wiping the whole registry — the full-wipe guard the retired flat loop
    # carried. Merge-then-project-once matches the golden regression fixture.
    from src.semantic.projection import _agnes_payload, project_document

    merged: dict[str, list] = {"semantic_model": []}
    for doc in parsed_documents:
        merged["semantic_model"].extend(doc.get("semantic_model") or [])
    report = project_document(
        merged, source="keboola_metastore", source_ref=source_ref, safe_prune=True, partial=partial_composition
    )

    # `unresolved_tables` is a plain fact for the admin Semantic layer page: the
    # tables a metric needs but this instance never registered, i.e. the set to
    # go register. Derived from the composed documents (each metric carries its
    # dataset tableId in the AGNES extension), so no extra Metastore round-trip.
    table_lookup = table_lookup_from_registry(table_registry_repo().list_by_source("keboola"))
    unresolved_set: set[str] = set()
    skipped_unresolved_table = 0
    for model in merged["semantic_model"]:
        for metric in model.get("metrics") or []:
            tid = _agnes_payload(metric).get("dataset") or ""
            if tid and resolve_table_name(tid, table_lookup) is None:
                unresolved_set.add(tid)
                skipped_unresolved_table += 1
    unresolved_tables = sorted(unresolved_set)
    if unresolved_tables:
        logger.warning(
            "Keboola semantic layer: %d dataset table(s) referenced by metrics are not registered "
            "in Agnes; those metrics are skipped. Register them to pick the metrics up: %s",
            len(unresolved_tables),
            ", ".join(unresolved_tables[:_MAX_REPORTED_UNRESOLVED_TABLES]),
        )

    # One-time retirement of the pre-cutover source. Rows this project wrote
    # under the old source="keboola_semantic_layer" are superseded by the
    # projection above; purge them within THIS source's scope. Gated on the
    # projection having actually written rows, so an empty/failed upstream
    # (0 written) can never delete the last good copy of a metric — AND on
    # `not partial_composition`, the same guard the projector's own prune
    # uses: when one of several composed documents failed validation and was
    # dropped, the OTHER models still project (metrics_written > 0) while this
    # pass never rewrites the dropped model's rows at all. Without the guard,
    # the purge would delete the dropped model's legacy rows anyway, and
    # nothing this pass recreates them. Idempotent: a later fully-valid sync
    # finds none.
    purged_legacy = 0
    if report.metrics_written and not partial_composition:
        legacy_repo = metric_repo()
        for m in legacy_repo.list():
            if (m.get("source") or "") == "keboola_semantic_layer" and _in_scope(m, scope_refs, adopt_null):
                legacy_repo.delete(m["id"])
                purged_legacy += 1

    # Symmetric one-time retirement for glossary rows — the metric-side purge
    # above has always run this pass, but the glossary side was never added,
    # so a legacy `source="keboola_semantic_layer"` glossary row is a
    # permanent duplicate of its freshly-projected `keboola_metastore` twin.
    # Gated on `report.glossary_written` (NOT `metrics_written`): a project
    # that writes metrics but no glossary terms this pass must not wipe
    # glossary rows it never rewrote. Same `not partial_composition` guard as
    # the metric purge above, for the same reason.
    purged_legacy_glossary = 0
    if report.glossary_written and not partial_composition:
        legacy_glossary_repo = glossary_repo()
        for g in legacy_glossary_repo.list(limit=100_000):
            if (g.get("source") or "") == "keboola_semantic_layer" and _in_scope(g, scope_refs, adopt_null):
                legacy_glossary_repo.delete(g["id"])
                purged_legacy_glossary += 1
        if purged_legacy_glossary:
            legacy_glossary_repo.refresh_search_index()

    return {
        "status": "ok",
        "created_or_updated": report.metrics_written,
        "pruned": report.metrics_pruned + purged_legacy,
        "skipped_unresolved_table": skipped_unresolved_table,
        "glossary_created_or_updated": report.glossary_written,
        "glossary_pruned": report.glossary_pruned + purged_legacy_glossary,
        # Truncated for the payload, but the TOTAL rides along: the admin page
        # presents this list as the set to go and register, so a silent cut
        # meant an admin could register everything shown, sync again, and still
        # lose metrics with nothing naming the rest.
        "unresolved_tables": unresolved_tables[:_MAX_REPORTED_UNRESOLVED_TABLES],
        "unresolved_tables_total": len(unresolved_tables),
        "source_ref": source_ref,
        "project_key": project_key,
    }


def _as_source_entry(connection_id: Optional[str], name: Optional[str], result: dict) -> dict:
    """Normalize one ``_sync_one_source`` result into the per-source entry the
    endpoint publishes: identity first, every counter present (so consumers
    never have to guard on a missing key), no internal ``project_key``.

    ``connection_id`` is the one public identity key; ``result``'s own
    ``source_ref`` (always equal to ``connection_id`` for this call site) is
    dropped rather than published redundantly alongside it.
    """
    entry = {
        "connection_id": connection_id,
        "name": name,
        **_empty_counters(),
        **result,
    }
    entry.pop("project_key", None)
    entry.pop("source_ref", None)
    return entry


def _aggregate_sources(sources: list[dict]) -> dict:
    """Sum per-source counters into the historical top-level result shape and
    attach the per-source breakdown.

    Top-level ``status`` is "ok" when at least one source synced; otherwise
    "error" carrying the first source's error message — which keeps the
    endpoint's error-on-returned-error contract intact for the single-source
    (legacy) shape it has always seen. The first source's ``code`` rides along
    so the endpoint can tell an admin-fixable failure (bad token, nothing
    configured) from a genuine upstream outage instead of calling both 502.
    """
    totals = {key: sum(int(s.get(key) or 0) for s in sources) for key in _COUNTER_KEYS}
    any_ok = any(s.get("status") == "ok" for s in sources)
    result: dict[str, Any] = {"status": "ok" if any_ok else "error", **totals, "sources": sources}
    if not any_ok:
        # BOTH from the same source. Two independent `next(...)` scans could
        # take the message from one failing project and the code from another,
        # so an admin read a reason that did not belong to the failure type
        # they were shown — the worst possible pairing when several projects
        # fail at once and only one of them is the one they are debugging.
        # (Devin Review on this PR.)
        first_failed = next((s for s in sources if s.get("error") or s.get("code")), None) or {}
        result["error"] = first_failed.get("error") or "Keboola semantic layer sync failed"
        if first_failed.get("code"):
            result["code"] = first_failed["code"]
    return result


def sync_semantic_layer(
    keboola_url: Optional[str] = None,
    keboola_token: Optional[str] = None,
) -> dict:
    """Sync every configured Keboola project's semantic layer (Metastore) into
    Agnes's metric_definitions / glossary_terms tables.

    Source selection, in order:

    1. Explicit ``keboola_url`` + ``keboola_token`` — one unstamped source
       (``source_ref=None``), today's single-project behavior.
    2. Every Keboola connection holding a master token in the vault
       (``_enumerate_master_sources``) — each synced under its own
       ``source_ref``, prunes scoped to its own rows, errors isolated per
       source. When at least one master token exists these are the ONLY
       sources: the legacy env pair has no connection identity to scope a
       prune to, and mixing it in would recreate the cross-wipe problem.
    3. Otherwise the legacy single-source resolution
       (``_resolve_keboola_credentials_slot``): rows are stamped with the
       default connection's id only when the credentials came from that
       connection, else left NULL, while the prune scope covers "NULL or the
       default connection id" either way. Rows belonging to other connections
       are left orphaned-but-intact rather than pruned.

    MasterTokenRequiredError propagates in modes 1 and 3 (a configuration
    error the endpoint surfaces as a 400); in mode 2 it is captured into the
    offending source's entry so one misconfigured connection cannot abort the
    whole run.
    """
    if keboola_url and keboola_token:
        result = _sync_one_source(keboola_url, keboola_token, None, adopt_null=True)
        return _aggregate_sources([_as_source_entry(None, None, result)])

    master_sources = _enumerate_master_sources()
    if master_sources:
        default_conn = _default_keboola_connection()
        default_id = default_conn["id"] if default_conn else None
        seen_project_keys: set[tuple[str, Any]] = set()
        entries: list[dict] = []
        for source in master_sources:
            connection_id = source["connection_id"]
            try:
                result = _sync_one_source(
                    source["stack_url"],
                    source["token"],
                    connection_id,
                    # Legacy rows predate provenance and belong to whichever
                    # project was synced before this feature — only the default
                    # connection may claim them.
                    adopt_null=connection_id == default_id,
                    seen_project_keys=seen_project_keys,
                    expected_project=(
                        (source["project_id"], source["project_name"]) if source.get("project_id") is not None else None
                    ),
                )
            except MasterTokenRequiredError as e:
                logger.error("Keboola semantic layer: connection %s rejected: %s", connection_id, e)
                # Coded, because this path CAPTURES the exception instead of
                # letting it propagate: the single-source paths let it reach
                # the endpoint, which maps it to 400. Without a code here the
                # aggregate carries none and the endpoint falls back to 502 —
                # so the very same misconfiguration answered 400 on a
                # one-connection instance and 502 once a master token existed
                # (Devin Review on #1242). Reachable whenever a stored master
                # token is later downgraded upstream; the store-time preflight
                # cannot see that.
                result = {"status": "error", "error": str(e), "code": "master_token_required"}
            except Exception as e:
                # Per-source isolation is the whole point of the loop: one
                # connection blowing up in an unforeseen way (a client bug, an
                # unexpected upstream shape) must not abort the sources after
                # it. Broad on purpose — the error is recorded, not swallowed.
                logger.exception("Keboola semantic layer: connection %s failed", connection_id)
                result = {"status": "error", "error": str(e)}
            entries.append(_as_source_entry(connection_id, source["name"], result))
        return _aggregate_sources(entries)

    url, token, slot = _resolve_keboola_credentials_slot(keboola_url, keboola_token)
    if not url or not token:
        return {
            "status": "error",
            "error": "Keboola credentials not configured",
            "code": "credentials_not_configured",
            **_empty_counters(),
            "sources": [],
        }
    default_conn = _default_keboola_connection()
    default_id = default_conn["id"] if default_conn else None
    # Stamp the default connection's id only when the credentials actually came
    # from that connection — a legacy env pair may well point at a different
    # project, and mislabeling it would hand those rows to the connection on a
    # later master-token sync. The prune scope stays "NULL or default id"
    # either way, so a downgrade still cleans up what it previously owned.
    stamp = default_id if slot == "connection" else None
    # Guard this path too when the credentials carry a connection identity:
    # rows here are stamped with the default connection's id, so the same
    # cross-attribution the master-token loop refuses is reachable whenever
    # that connection's STORAGE token opens a different project than the one
    # it is bound to (Devin Review on #1242). A legacy env pair stamps nothing
    # (`stamp` is None), so there is no binding to contradict and no guard to
    # apply.
    expected_project = None
    if slot == "connection" and default_conn is not None:
        default_config = default_conn.get("config") or {}
        if default_config.get("project_id") is not None:
            expected_project = (default_config["project_id"], default_config.get("project_name") or "")
    result = _sync_one_source(
        url,
        token,
        stamp,
        adopt_null=True,
        prune_scope_refs={None, default_id},
        expected_project=expected_project,
    )
    return _aggregate_sources([_as_source_entry(default_id, default_conn["name"] if default_conn else None, result)])


def relationship_lookup_by_dataset(relationship_items: list[dict]) -> dict[str, list[dict]]:
    """Index semantic-relationship attributes by every tableId that appears
    on either side (from or to), so a metric's dataset can be looked up
    against every relationship touching it in O(1).

    A relationship's attributes dict is stored under BOTH its from and to
    tableId keys — resolve_relationship() below determines which side
    (verified vs. unverified direction) the caller's dataset sits on.
    """
    lookup: dict[str, list[dict]] = {}
    for item in relationship_items:
        attrs = item.get("attributes") or {}
        from_id = attrs.get("from")
        to_id = attrs.get("to")
        if from_id:
            lookup.setdefault(from_id, []).append(attrs)
        if to_id:
            lookup.setdefault(to_id, []).append(attrs)
    return lookup


def resolve_relationship(
    dataset_table_id: str,
    relationship_lookup: dict[str, list[dict]],
) -> tuple[Optional[dict], Optional[str]]:
    """Resolve exactly one semantic-relationship for a metric's dataset,
    restricted to the ONE live-verified-safe case (docs/superpowers/specs/
    2026-07-17-keboola-relationship-metrics-design.md):

    - exactly one relationship touches this dataset (from OR to side) —
      zero or multiple candidates return "ambiguous_relationship";
    - that relationship's type == "left" — the only value observed live;
      anything else returns "unsupported_relationship_type";
    - the dataset is on the relationship's "to" side — the only direction
      verified live to compose FROM t LEFT JOIN joined correctly; a
      dataset on the "from" side returns "unverified_relationship_direction"
      rather than assuming the reverse direction behaves the same way.

    Returns (relationship_attrs, None) on success, (None, skip_reason)
    otherwise. Never raises, never guesses.
    """
    candidates = relationship_lookup.get(dataset_table_id, [])
    if len(candidates) != 1:
        return None, "ambiguous_relationship"

    relationship = candidates[0]
    if relationship.get("type") != "left":
        return None, "unsupported_relationship_type"
    if relationship.get("to") != dataset_table_id:
        return None, "unverified_relationship_direction"

    return relationship, None


# Matches the live-verified semantic-relationship.on shape exactly:
# `<alias>."<column>" = <alias>."<column>"`. Verified live (2026-07-17):
# 29/29 sampled relationships matched this pattern with no variation.
_ON_CLAUSE_RE = re.compile(r'^\s*(\w+)\s*\.\s*"([^"]+)"\s*=\s*(\w+)\s*\.\s*"([^"]+)"\s*$')


def parse_on_clause(on: str) -> Optional[tuple[str, str, str, str]]:
    """Parse a semantic-relationship.on string into (alias1, col1, alias2, col2).

    Returns None if `on` doesn't match the live-verified shape — callers
    must treat that as "can't resolve, skip" rather than a hard error.
    """
    m = _ON_CLAUSE_RE.match(on)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3), m.group(4)


def resolve_join_aliases(
    on: str,
    from_columns: set[str],
    to_columns: set[str],
) -> Optional[tuple[str, str]]:
    """Determine which of the two aliases in `on` belongs to the `to`
    (metric's own) table vs. the `from` (joined) table, by matching each
    side's column name against the real, already-known column sets of
    both tables (column_metadata_repo(), populated by the profiler).

    Returns (to_alias, from_alias) when EXACTLY ONE of the two possible
    pairings is consistent with both tables' real schemas. Returns None —
    "can't resolve with confidence" — when the on-clause doesn't parse,
    when BOTH pairings are consistent (genuinely ambiguous, e.g. both
    tables share a column name used in the join), or when NEITHER pairing
    is consistent (e.g. column metadata is missing or stale).
    """
    parsed = parse_on_clause(on)
    if parsed is None:
        return None
    alias1, col1, alias2, col2 = parsed

    # Candidate A: alias1 is the `to` side, alias2 is the `from` side.
    candidate_a = col1 in to_columns and col2 in from_columns
    # Candidate B: alias1 is the `from` side, alias2 is the `to` side.
    candidate_b = col1 in from_columns and col2 in to_columns

    if candidate_a and not candidate_b:
        return alias1, alias2
    if candidate_b and not candidate_a:
        return alias2, alias1
    return None


def extract_foreign_aliases(expression: str) -> set[str]:
    """Return every distinct alias (excluding `t`) that qualifies a column
    in `expression`, masking quoted regions first (same rationale as
    references_foreign_alias / has_embedded_sql_comment).

    A metric may use more than one local alias spelling for what resolves
    to the SAME single relationship (live-verified real case) — all of
    them get rewritten to the canonical join alias in compose_join_sql.
    """
    masked = _mask_quoted_regions(expression)
    aliases = {m.group(1) for m in _ALIAS_QUALIFIER_RE.finditer(masked)}
    aliases.discard("t")
    return aliases


def compose_join_sql(
    expression: str,
    primary_table: str,
    joined_table: str,
    on: str,
    to_alias: str,
    from_alias: str,
) -> str:
    """Compose a two-table LEFT JOIN metric_definitions.sql.

    `to_alias`/`from_alias` are the on-clause's alias tokens as resolved by
    resolve_join_aliases — `to_alias` corresponds to `primary_table`
    (rewritten to the canonical `t`), `from_alias` to `joined_table`
    (rewritten to the canonical `j`). Every foreign-alias-qualified column
    in `expression` (there may be multiple distinct alias spellings for the
    same joined table — see extract_foreign_aliases) is rewritten to `j.`.

    Callers MUST have already checked references_foreign_alias(expression)
    and has_embedded_sql_comment(expression) — this function does not
    itself guard against those cases (mirrors compose_sql's contract).
    """
    rewritten_expression = expression
    for alias in extract_foreign_aliases(expression):
        # Devin Review, PR #944: rewrite outside quoted regions only, so an
        # alias-qualified-looking substring inside a string literal or a
        # quoted identifier (e.g. `'o.pending'`) is never corrupted.
        rewritten_expression = _sub_outside_quotes(rf"\b{re.escape(alias)}\s*\.", "j.", rewritten_expression)

    on_alias1, on_col1, on_alias2, on_col2 = parse_on_clause(on)  # type: ignore[misc]
    remapped_alias1 = "t" if on_alias1 == to_alias else "j"
    remapped_alias2 = "t" if on_alias2 == to_alias else "j"
    # Aliases (`t`/`j`) are literals chosen above and stay bare; the columns come
    # from the parsed ON clause and are identifiers.
    remapped_on = f"{remapped_alias1}.{quote_ident(on_col1)} = {remapped_alias2}.{quote_ident(on_col2)}"

    return f"SELECT {rewritten_expression} FROM {quote_ident(primary_table)} AS t LEFT JOIN {quote_ident(joined_table)} AS j ON {remapped_on}"


_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


# --- Coverage (live, no stored state) ---------------------------------------

# Skip reasons that mean the metric's own DEFINITION cannot be composed. These
# are defects in the upstream semantic layer — registering a table will not fix
# one — so they are reported separately from `unresolved_table`, which is the
# ordinary steady state of any project whose semantic layer describes more
# tables than the instance registers.
DEFINITION_BLOCKED_REASONS = frozenset(
    {
        "missing_name",
        "embedded_sql_comment",
        "foreign_alias_reference",
        "ambiguous_relationship",
        "unsupported_relationship_type",
        "unverified_relationship_direction",
    }
)


def _token_from_env(conn: dict) -> str:
    """``token_env``'s value, but only for an allowlisted name.

    The single gated reader for both places this module resolves a Storage
    token from the environment. See `src.orchestrator_security` for why the
    allowlist exists: `token_env` is admin-supplied and otherwise unbounded.
    """
    token_env = conn.get("token_env") or ""
    if not token_env:
        return ""
    from src.orchestrator_security import is_config_secret_env_allowed

    if not is_config_secret_env_allowed(token_env):
        logger.warning(
            "connection %s names token_env %r, which is not in the allowlist — not read",
            conn.get("id"),
            token_env,
        )
        return ""
    return os.environ.get(token_env, "")


def _connection_storage_token(conn: dict) -> str:
    """The connection's regular (non-master) Storage token, vault first then
    ``token_env`` — the same precedence
    :func:`_resolve_keboola_credentials_slot` applies to its connection branch.
    """
    from src.repositories import connection_secrets_repo

    try:
        secrets = connection_secrets_repo()
        if secrets.has(conn["id"]):
            token = secrets.get(conn["id"]) or ""
            if token:
                return token
    except Exception:
        logger.warning("Could not read storage token for connection %s", conn.get("id"))
    return _token_from_env(conn)


def _project_identity(url: str, token: str) -> Optional[dict]:
    """``{"id", "name"}`` for whichever project ``token`` belongs to, or None
    when it cannot be established. Never raises and never echoes the token."""
    import requests

    from connectors.keboola.storage_api import KeboolaStorageClient, StorageApiError

    if not (url and token):
        return None
    try:
        info = KeboolaStorageClient(url=url, token=token).verify_token()
        owner = info.get("owner") or {}
    except (StorageApiError, requests.RequestException) as e:
        logger.warning("Project identity lookup failed for %s: %s", url, e)
        return None
    if owner.get("id") is None:
        return None
    # `is_master` rides along because the call that establishes identity is the
    # same call `check_master_token` reads — the sync aborts on a downgraded
    # token, and coverage would otherwise report happily for a project whose
    # sync cannot run. One round-trip, two answers. (Devin Review.)
    return {
        "id": owner.get("id"),
        "name": owner.get("name") or "",
        "is_master": bool(info.get("isMasterToken")),
    }


def _predict_metric_binding(
    metric_item: dict,
    table_lookup: dict[tuple[str, str], str],
    relationship_lookup: dict[str, list[dict]],
    column_lookup: dict[str, set[str]],
) -> tuple[Optional[str], Optional[str]]:
    """Predict whether a Metastore semantic-metric will bind to a table, and if
    not, why — the coverage report's predictor of what ``project_document`` (the
    single writer of the flat query tables since the cutover) does with it.

    Returns ``(table_name, None)`` when the metric binds — simply or via a JOIN
    — or ``(None, skip_reason)`` with the same reasons the projector's binder
    acts on: ``unresolved_table``, ``embedded_sql_comment``,
    ``foreign_alias_reference`` (or the finer relationship reasons from
    ``try_join_composition``). Built on the exact primitives the projector's
    ``_bind_metric`` uses, so predicting here and binding there cannot drift.
    Keboola's ``attributes.sql`` is already the bare aggregation fragment the
    projector reaches via ``resolve_expression``, so the two see the same input.
    """
    attrs = metric_item.get("attributes") or {}
    name = attrs.get("name")
    expression = attrs.get("sql") or ""
    dataset_table_id = attrs.get("dataset") or ""
    if not name:
        return None, "missing_name"
    if references_foreign_alias(expression):
        if has_embedded_sql_comment(expression):
            return None, "embedded_sql_comment"
        fields, join_reason = try_join_composition(
            expression, dataset_table_id, table_lookup, relationship_lookup, column_lookup
        )
        if fields is None:
            return None, join_reason
        return fields["table_name"], None
    if has_embedded_sql_comment(expression):
        return None, "embedded_sql_comment"
    table_name = resolve_table_name(dataset_table_id, table_lookup)
    if table_name is None:
        return None, "unresolved_table"
    return table_name, None


def compute_semantic_coverage(*, warnings_only: bool = False) -> dict:
    """How much of each connected Keboola project's semantic layer actually
    lands in Agnes, computed live against the Metastore and the table registry.

    ``warnings_only=True`` stops after the identity checks — the three token
    comparisons, which are two `verify_token` calls per connection — and skips
    the Metastore enumeration entirely. That is what the Data sources page
    needs for its warning strip, and enumerating every project's whole
    semantic model on every page view to draw it was work nobody asked for.
    (Devin Review on this PR.) Counts come back zeroed in that mode; the
    Semantic layer page still asks for the full report.

    Deliberately stateless: nothing is persisted and no counter from the last
    sync is read. The sync's own counters live in an in-memory dict that resets
    on every process restart, so a status built on them goes blank exactly when
    someone restarts and comes looking. This recomputes on demand instead —
    at the cost of a handful of upstream calls per connection.

    Two things are worth an admin's attention, and they are NOT the same:

    - ``importable == 0`` while the project publishes metrics. Nobody chooses
      that; it means the two sides never meet — most often because a
      connection's storage token and master token point at DIFFERENT projects,
      so tables sync from one project and the semantic layer is read from
      another. Reported as ``token_project_mismatch``.
    - metrics blocked by their own definition
      (:data:`DEFINITION_BLOCKED_REASONS`).

    ``unregistered_tables`` is neither: a semantic layer routinely describes far
    more tables than an instance registers, and those metrics are *supposed* to
    stay out. It is reported as a plain fact, never as a pending queue.

    Constraints are not fetched — they only decorate a metric that already
    binds and cannot change a skip decision, so the extra round-trip buys
    nothing here.

    Counts span EVERY model a project publishes, walked with the same per-model
    scoping the projector uses (``_predict_metric_binding`` mirrors the
    projector's binder) — the report's whole value is that it predicts what the
    sync's projection will land, so any divergence is a defect here. Since the
    projector enforces no name-ownership gate, a name shared with another source
    no longer blocks a metric; the report surfaces that as coexistence
    (``conflicts``) rather than subtracting it from ``importable``.
    """
    import requests

    from connectors.keboola.metastore_client import MetastoreApiError, MetastoreClient
    from src.repositories import (
        column_metadata_repo,
        metric_repo,
        source_connections_repo,
        table_registry_repo,
    )

    conns_by_id = {c["id"]: c for c in source_connections_repo().list(source_type="keboola")}
    metric_repository = metric_repo()
    table_lookup = table_lookup_from_registry(table_registry_repo().list_by_source("keboola"))
    column_metadata = column_metadata_repo()
    column_lookup = {
        name: {c["column_name"] for c in column_metadata.list_for_table(name)} for name in set(table_lookup.values())
    }

    sources: list[dict] = []
    for source in _enumerate_master_sources():
        conn_id = source["connection_id"]
        stack_url = source["stack_url"]
        entry: dict[str, Any] = {
            "connection_id": conn_id,
            "name": source["name"],
            "stack_url": stack_url,
            "project": None,
            "storage_project": None,
            "token_project_mismatch": False,
            "models": [],
            "metrics": {"upstream": 0, "importable": 0},
            "glossary": {"upstream": 0},
            "blocked": [],
            "unregistered_tables": [],
            "conflicts": [],
            "warnings": [],
            "error": None,
        }

        entry["project"] = _project_identity(stack_url, source["token"])
        conn = conns_by_id.get(conn_id) or {}
        entry["storage_project"] = _project_identity(stack_url, _connection_storage_token(conn))

        # The connection's RECORDED project (#1242) is a third identity, and
        # the one the sync actually enforces: a master token that opens a
        # different project than the connection is locked to is refused
        # outright, so the sync never runs. Comparing only storage-vs-master
        # missed that entirely — the report gave a clean bill of health to a
        # connection whose sync cannot start, which is the single most
        # misleading thing this page can say.
        # The sync's own preflight, applied to the same payload: a master
        # token that has been downgraded makes `_sync_one_source` abort with
        # `MasterTokenRequiredError`, and the Metastore rejects it with an
        # opaque "Failed to create project scope" — so a report that skips
        # this check describes a sync that never runs. (Devin Review.)
        if entry["project"] and entry["project"].get("is_master") is False:
            entry["warnings"].append(
                {
                    "code": "master_token_downgraded",
                    "message": (
                        "The stored owner token is no longer a master token, so the sync aborts "
                        "before it reads anything. Store the project's owner token again on the "
                        "Data sources page."
                    ),
                }
            )

        bound_id = (conn.get("config") or {}).get("project_id")
        if bound_id is not None and entry["project"] and str(entry["project"]["id"]) != str(bound_id):
            entry["token_project_mismatch"] = True
            entry["warnings"].append(
                {
                    "code": "master_token_project_mismatch",
                    "message": (
                        f"This connection is locked to project {bound_id}, but its master token "
                        f"opens project {entry['project']['id']}. The sync refuses to run at all "
                        f"until one of the two is corrected — unbind the connection on the Data "
                        f"sources page, or store a master token for the project it is bound to."
                    ),
                }
            )

        both = (entry["project"], entry["storage_project"])
        if all(both) and str(both[0]["id"]) != str(both[1]["id"]):
            entry["token_project_mismatch"] = True
            entry["warnings"].append(
                {
                    "code": "token_project_mismatch",
                    "message": (
                        f"Storage token points at project {both[1]['id']}, master token at "
                        f"project {both[0]['id']}. Tables sync from one project and the semantic "
                        f"layer is read from another, so no metric can bind to a table."
                    ),
                }
            )

        if warnings_only:
            sources.append(entry)
            continue

        try:
            metastore = MetastoreClient(url=stack_url, token=source["token"])
            models = metastore.list_items("semantic-model")
            if not models:
                sources.append(entry)
                continue
            # EVERY model, in the same order the sync walks them (`sorted` on
            # the immutable uuid). Reading `models[0]` described one model and
            # stayed silent about the rest, while the write loop below has
            # imported all of them since multi-model projects became reachable
            # — a report that contradicts the code it reports on is worse than
            # no report.
            models_by_uuid = {m["id"]: m for m in models if m.get("id")}
            model_uuids = sorted(models_by_uuid)
            entry["models"] = [
                {"uuid": uuid, "name": (models_by_uuid[uuid].get("attributes") or {}).get("name") or ""}
                for uuid in model_uuids
            ]
            per_model = [
                (
                    uuid,
                    {
                        "semantic-dataset": metastore.list_items("semantic-dataset", uuid),
                        "semantic-metric": metastore.list_items("semantic-metric", uuid),
                        "semantic-relationship": metastore.list_items("semantic-relationship", uuid),
                        "semantic-glossary": metastore.list_items("semantic-glossary", uuid),
                    },
                )
                for uuid in model_uuids
            ]
        except (MetastoreApiError, requests.RequestException) as e:
            entry["error"] = f"Metastore fetch failed: {e}"
            entry["warnings"].append({"code": "fetch_failed", "message": entry["error"]})
            sources.append(entry)
            continue

        upstream_metrics = 0
        importable = 0
        unregistered: set[str] = set()

        # Datasets and relationships are model-scoped, exactly as in the
        # projector: each model's metrics resolve against its OWN objects, never
        # a merged pool, or a dataset in one model could silently satisfy a
        # metric in another.
        for _model_uuid, model_items in per_model:
            relationship_lookup = relationship_lookup_by_dataset(model_items["semantic-relationship"])
            metrics = model_items["semantic-metric"]
            upstream_metrics += len(metrics)
            entry["glossary"]["upstream"] += len(model_items["semantic-glossary"])

            for item in metrics:
                attrs = item.get("attributes") or {}
                table_name, reason = _predict_metric_binding(item, table_lookup, relationship_lookup, column_lookup)
                if table_name is not None:
                    importable += 1
                    # The projector writes under a scoped id and enforces NO
                    # name-ownership gate (unlike the retired flat composer), so
                    # a name another source also defines no longer blocks this
                    # metric — both now coexist in the catalog. Surface that (it
                    # is the top confusion source) as information, without
                    # subtracting it from importable.
                    name = attrs.get("name") or ""
                    existing = metric_repository.find_by_name(name)
                    if existing is not None and (existing.get("source") or "") != "keboola_metastore":
                        entry["conflicts"].append(
                            {
                                "metric": name,
                                "held_by": existing.get("source") or "",
                                "held_id": existing.get("id") or "",
                            }
                        )
                    continue
                if reason == "unresolved_table":
                    unregistered.add(attrs.get("dataset") or "")
                elif reason in DEFINITION_BLOCKED_REASONS:
                    entry["blocked"].append(
                        {
                            "metric": attrs.get("name") or "",
                            "dataset": attrs.get("dataset") or "",
                            "reason": reason,
                        }
                    )

        entry["metrics"] = {"upstream": upstream_metrics, "importable": importable}
        entry["unregistered_tables"] = sorted(t for t in unregistered if t)

        if upstream_metrics and importable == 0:
            entry["warnings"].append(
                {
                    "code": "no_metrics_bound",
                    "message": (
                        f"None of the {upstream_metrics} metrics this project publishes can bind to a registered table."
                    ),
                }
            )
        if entry["blocked"]:
            entry["warnings"].append(
                {
                    "code": "metrics_blocked",
                    "message": (
                        f"{len(entry['blocked'])} metric(s) cannot be composed from their own "
                        f"definition; registering a table will not fix these."
                    ),
                }
            )
        if entry["conflicts"]:
            names = ", ".join(c["metric"] for c in entry["conflicts"][:5])
            entry["warnings"].append(
                {
                    "code": "name_conflict",
                    "message": (
                        f"{len(entry['conflicts'])} metric name(s) are also defined by another source "
                        f"({names}). Both definitions now coexist in the catalog under different ids, so "
                        f"a lookup by name may return either — reconcile the duplicate definitions."
                    ),
                }
            )
        sources.append(entry)

    return {"sources": sources}
