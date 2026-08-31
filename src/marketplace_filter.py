"""Resolver: authenticated user → allowed plugins across all marketplaces.

The marketplace endpoint aggregates plugins from every registered marketplace
and returns only those the caller is allowed to see. Access is resolved
uniformly through ``resource_grants`` (resource_type='marketplace_plugin'):
the caller sees the distinct plugins granted to any of their groups. There
is no implicit Everyone membership and no god-mode shortcut for the
marketplace feed — admins curate their own view by granting plugins to a
group they belong to (Admin or otherwise).

Two distinct identifiers travel through the resolver:

- ``prefixed_name`` (``<slug>-<plugin_name>``) drives the on-disk directory
  layout in the served ZIP / git tree (``plugins/<prefixed_name>/...``) so
  two marketplaces shipping a same-named plugin don't overwrite each other's
  files.
- ``manifest_name`` (read from the plugin's own
  ``.claude-plugin/plugin.json`` ``name`` field, with a fallback to the
  upstream marketplace.json ``name``) is what the synth marketplace.json's
  ``name`` field uses. Claude Code's ``/plugin`` UI resolves a loaded plugin
  back to its catalog entry by ``plugin.json`` ``name``, so the catalog
  entry must match — anything else and the Components panel renders
  "Plugin <X> not found in marketplace".

Same-named plugins from two upstream marketplaces therefore collide in the
served catalog by design; admin RBAC (which grants survive the filter)
decides which one wins, identical to Claude Code's behavior when a user
adds two upstream marketplaces with overlapping plugin names directly.

resource_id format for ``marketplace_plugin`` grants is
``<marketplace_slug>/<plugin_name>`` — the slash is the canonical separator;
neither slugs nor plugin names contain slashes (both regex-constrained).
"""

from __future__ import annotations

import hashlib
import json
import logging
from functools import partial
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional

import duckdb

from app.auth.access import _user_group_ids
from app.utils import get_marketplaces_dir, get_store_dir
from src.marketplace import (
    is_external_plugin_source,
    is_safe_plugin_name,
    is_safe_plugin_source,
)
from src.repositories import (
    marketplace_plugins_repo,
    resource_grants_repo,
    user_curated_subscriptions_repo,
    user_groups_repo,
    user_store_installs_repo,
)

logger = logging.getLogger(__name__)

# Upper bound on a plugin's self-declared `name` (see `resolve_manifest_name`).
# `is_safe_plugin_name` constrains the charset but not the length, and this
# value is served in JSON and rendered as a UI chip. 64 matches the cap the
# Agent Plugins manifest schema puts on the same field, and is far above any
# real plugin name.
MAX_MANIFEST_NAME_LEN = 64

# Bump whenever the BYTES WE SERVE change for an unchanged set of source files.
#
# `compute_etag` is content-addressed over the plugin files on disk, which is
# what makes a stale 304 impossible when a curator edits a plugin — but it also
# means a change to how we *package* those files is invisible to it. Both
# caches key off this ETag (the ZIP channel answers `If-None-Match` with it;
# the git channel names its bare repo `<etag>.v<N>.git`), so without this
# constant a packaging fix reaches only users whose plugins happen to change
# afterwards, and everyone else keeps the old bytes indefinitely.
#
# v2: the served `plugin.json` `name` is forced to the resolved manifest name
#     (`_sanitize_served_plugin_json`), so a plugin whose declared name was
#     rejected no longer ships an identity its catalog entry disagrees with.
# v3: executable files keep their exec bit (ZIP external_attr 755 / git tree
#     mode 100755) — same bytes, different packaging, so caches must roll.
SERVED_FORMAT_VERSION = 3


def _contained_plugin_dir(root: Path, slug: str, name: str, source: Any = None) -> Optional[Path]:
    """The plugin's on-disk directory inside its marketplace clone, or None.

    ``source`` is the catalog entry's declared location relative to the clone
    root (from ``marketplace_plugins.raw``): ``"./"`` for a root-source plugin
    (the plugin IS the repo — the single-plugin-repo shape), a subdirectory
    like ``"./plugins/<name>"``, or an external location whose files live
    outside the clone — Claude Code's object form, or a remote string
    (``is_external_plugin_source``). No source (or a blank one) keeps the
    conventional ``plugins/<name>`` default.

    An external source falls back to that same ``plugins/<name>``
    directory — but ONLY when it actually exists on disk. Before source-aware
    resolution every row resolved there unconditionally, so a curator who
    declared ``{"source": "github", …}`` AND vendored the plugin under
    ``plugins/<name>`` (belt-and-braces) had those files served; dropping the
    row outright is a silent regression that also leaves grants and
    subscriptions pointing at nothing. The fallback cannot be steered by the
    source's contents — it is always ``plugins/<validated name>`` inside this
    marketplace's own clone. With no vendored directory the plugin stays a
    metadata-only catalog entry (None).

    Layer 2 behind the ingest rejections (``is_safe_plugin_name`` /
    ``is_safe_plugin_source``) — security playbook §6 requires both. Rows
    written by an older Agnes version, or edited directly in the DB, never
    passed the ingest check, and this is the last point before the path
    reaches ``rglob`` + ``read_bytes`` in the ZIP and git packagers.
    ``resolve()`` also collapses a symlink pointing out of the tree, which
    string checks alone cannot see. Containment is anchored on the CLONE
    (``root/slug``), not the marketplaces root — a declared source must never
    reach a sibling marketplace's differently-RBAC'd content — and the clone
    itself is re-anchored on ``root`` so a hostile slug row cannot move the
    base.

    The name is used unstripped: rows in ``marketplace_plugins`` were already
    stripped by ``_refresh_plugin_cache``, so the strict helper is correct here.
    """
    if not is_safe_plugin_name(name):
        return None
    clone = root / slug
    if isinstance(source, str) and source.strip() and not is_external_plugin_source(source):
        rel = source.strip()
        if not is_safe_plugin_source(rel):
            return None
        parts = [seg for seg in rel.split("/") if seg not in ("", ".")]
        candidate = clone.joinpath(*parts) if parts else clone
    elif source is None or (isinstance(source, str) and not source.strip()):
        candidate = clone / "plugins" / name
    else:
        # External source — Claude Code's object form, or a remote string
        # (URL / scp-style git address). The plugin's files live outside the
        # clone. Serve a vendored copy if the curator left one at the
        # conventional path, otherwise there is nothing on disk to re-serve.
        candidate = clone / "plugins" / name
        if not candidate.is_dir():
            return None
    try:
        resolved = candidate.resolve()
        resolved.relative_to(clone.resolve())
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return candidate


def _no_local_dir_reason(source: Any) -> str:
    """Operator-facing reason a catalog row resolved to no local directory.

    A plugin that resolves to nothing is invisible on the shelf with only a
    log line to explain it, and the two causes want different reactions: an
    external source is the curator's declared intent (nothing to fix), a
    non-contained path is a broken or hostile catalog entry.
    """
    if (source is not None and not isinstance(source, str)) or is_external_plugin_source(source):
        return (
            "its source is external (a remote URL, or Claude Code's object form) and the clone "
            "carries no vendored plugins/<name> directory — the catalog entry stands, its files "
            "are not served"
        )
    return "no contained local source directory — the declared source escapes the clone, or the name is unsafe"


def required_store_entity_keys(conn: duckdb.DuckDBPyConnection | None, user_id: str | None) -> set[str]:
    """``store_entities.id`` values held at the ``required`` tier by any of the
    user's groups.

    The authored-item counterpart to :func:`required_plugin_keys`. Kept in this
    module (rather than imported from ``app.auth.access``) so the serve path
    stays importable without the FastAPI app — same reason
    ``resolve_allowed_plugins`` reimplements its group lookup here.
    """
    group_ids = _user_group_ids(user_id, conn) if user_id else set()
    if not group_ids:
        return set()
    rows = resource_grants_repo().list_for_groups(list(group_ids), "store_entity")
    return {
        r["resource_id"] for r in rows if (r.get("requirement") or "available") == "required" and r.get("resource_id")
    }


def granted_store_entity_keys(conn: duckdb.DuckDBPyConnection | None, user_id: str | None) -> set[str]:
    """``store_entities.id`` values granted to any of the user's groups, at any
    tier.

    Its Required sibling above answers "what must this person carry"; this
    answers "what may they be served". Private stops meaning "nobody but me"
    and starts meaning "not everyone", so a hidden entity a group was granted
    reaches its members — which the grant always looked like it did.

    Same reason as the sibling for living here rather than importing from
    ``app.auth.access``: the serve path stays importable without the app.
    """
    group_ids = _user_group_ids(user_id, conn) if user_id else set()
    if not group_ids:
        return set()
    rows = resource_grants_repo().list_for_groups(list(group_ids), "store_entity")
    return {r["resource_id"] for r in rows if r.get("resource_id")}


def required_plugin_keys(conn: duckdb.DuckDBPyConnection | None, user_id: str | None) -> set[tuple[str, str]]:
    """``(marketplace_id, plugin_name)`` keys held at the ``required`` tier
    by any of the user's groups.

    v49 gave ``resource_grants`` a ``requirement`` enum
    (``available`` | ``required``) where required is the always-in-stack
    tier: the StackResolver unions required ids into the effective stack
    without an explicit subscription row. Marketplace plugins keep their
    own resolver (design D1), so the same union has to be applied here —
    ``resolve_user_marketplace`` serves
    ``(rbac ∩ (subscriptions ∪ required)) ∪ store_installs`` (for a
    ``plugins_mode='selected'`` agent the trailing union is itself
    scope-filtered — see ``_resolve_principal_marketplace``). Before this
    helper existed, flipping a marketplace_plugin grant to ``required``
    was a silent no-op for the served set (the separate global
    ``is_system`` flag was the only mandatory path).

    ``resource_id`` format is ``<marketplace_slug>/<plugin_name>``; rows
    without a slash are skipped defensively so a hand-written grant can
    never crash the serve path. Reads go through the repo factory (not
    raw SQL on ``conn``) for the same PG-backend reason documented in
    ``resolve_allowed_plugins``.
    """
    group_ids = _user_group_ids(user_id, conn) if user_id else set()
    if not group_ids:
        return set()
    rows = resource_grants_repo().list_for_groups(list(group_ids), "marketplace_plugin")
    keys: set[tuple[str, str]] = set()
    for r in rows:
        if (r.get("requirement") or "available") != "required":
            continue
        resource_id = r.get("resource_id") or ""
        if "/" not in resource_id:
            continue
        slug, _, name = resource_id.partition("/")
        keys.add((slug, name))
    return keys


def _resolve_raw(raw: Any) -> dict:
    """marketplace_plugins.raw is JSON — DuckDB may surface it as str or dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            decoded = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _prefixed_name(slug: str, plugin_name: str) -> str:
    """<slug>-<plugin_name>. Both fields are already regex-constrained to
    characters safe for a Claude Code plugin identifier."""
    return f"{slug}-{plugin_name}"


def resolve_manifest_name(plugin_dir: Path, fallback: str) -> str:
    """Return the plugin's authoritative `name` from its `.claude-plugin/plugin.json`.

    Claude Code resolves a loaded plugin back to its marketplace catalog
    entry by the name declared in the plugin's own `plugin.json`. The synth
    `marketplace.json` we serve must use that same name, otherwise the
    `/plugin` UI Components panel can't link the loaded plugin to its
    catalog entry and renders "Plugin <X> not found in marketplace".

    Falls back to ``fallback`` (the upstream marketplace.json's plugin name)
    when plugin.json is missing, unreadable, has no string `name`, or has
    an empty/whitespace-only `name` — same defensive style as
    ``src.marketplace.read_plugins``: never crash, always return a usable
    value.

    The value is also *validated*, not just read. It comes from a
    curator-controlled file and is emitted verbatim as the ``name`` of the
    synth ``marketplace.json`` entry and of the synth bundle ``plugin.json``
    (``app.marketplace_server.packager``), then rendered in the ``/plugin``
    UI — so it gets the same bar its sibling ``original_name`` already clears
    at ingest (``src.marketplace.is_safe_plugin_name``: exactly one
    filesystem-safe segment, no separators, no control characters), plus a
    length cap. Unlike ``original_name`` this value never builds a path, so
    this is a consistency/hygiene gate rather than a traversal defence — and
    the cap is what keeps an unbounded string out of the served JSON.

    ``fallback`` is returned unchecked: it is ``original_name``, which
    ``is_safe_plugin_name`` already gated when the row was ingested.
    """
    pj = plugin_dir / ".claude-plugin" / "plugin.json"
    if not pj.is_file():
        return fallback
    try:
        data = json.loads(pj.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback
    if not isinstance(data, dict):
        return fallback
    name = data.get("name")
    if not (isinstance(name, str) and name.strip()):
        return fallback
    name = name.strip()
    if not is_safe_plugin_name(name) or len(name) > MAX_MANIFEST_NAME_LEN:
        logger.warning(
            "plugin.json name %r in %s is not a usable plugin identifier; serving upstream name %r instead",
            name[:80],
            plugin_dir,
            fallback,
        )
        return fallback
    return name


def resolve_allowed_plugins(conn: duckdb.DuckDBPyConnection, user: dict) -> List[dict]:
    """Return the distinct, prefixed plugin list this user is allowed to install.

    Each entry:
        {
            "marketplace_id":   str,   # also the slug (they are the same)
            "marketplace_slug": str,
            "original_name":    str,   # name from upstream marketplace.json
            "prefixed_name":    str,   # "<slug>-<original_name>" — drives
                                       # the on-disk dir layout in the ZIP /
                                       # git tree (cross-marketplace files
                                       # don't collide).
            "manifest_name":    str,   # name from the plugin's own
                                       # .claude-plugin/plugin.json (or
                                       # original_name fallback) — drives
                                       # the `name` field in the synth
                                       # marketplace.json we serve, so the
                                       # Claude Code UI's catalog lookup
                                       # matches the loaded plugin's
                                       # namespace.
            "version":          str | None,
            "raw":              dict,  # parsed marketplace.json plugin entry
            "plugin_dir":       Path,  # ${DATA_DIR}/marketplaces/<slug>/plugins/<name>
        }

    Ordering is deterministic: by marketplace registration time, then plugin
    name — so ETag / git commit hash stay stable as long as the underlying
    content is unchanged.
    """
    user_id = user.get("id")
    root = get_marketplaces_dir()

    # Distinct (marketplace_id, plugin_name) across all of the user's
    # groups. If two groups grant the same plugin, it still appears
    # once. Admin is treated as a regular group — admins get only the
    # plugins their groups have been granted.
    #
    # Reads must go through the repo factory rather than raw SQL on the
    # DuckDB-typed ``conn`` parameter — on Postgres-backed deployments
    # ``marketplace_plugins`` / ``resource_grants`` / ``marketplace_registry``
    # rows live in PG; a raw ``conn.execute`` would hit the empty DuckDB
    # tables and silently exclude every plugin from the served set. The
    # symptom was missing plugins for users whose groups hadn't changed
    # (i.e. it wasn't stale Google sync) — the JOIN simply returned 0
    # rows because it was running against an empty DuckDB. The repo's
    # PG mirror runs the same logical JOIN against the live engine.
    group_ids = _user_group_ids(user_id, conn) if user_id else set()
    if not group_ids:
        return []
    rows = marketplace_plugins_repo().list_granted_for_groups(group_ids)

    result: List[dict] = []
    for row in rows:
        marketplace_id = row["marketplace_id"]
        name = row["name"]
        slug = marketplace_id  # registry.id IS the slug (see src/marketplace.py)
        raw = _resolve_raw(row.get("raw"))
        plugin_dir = _contained_plugin_dir(root, slug, name, source=raw.get("source"))
        if plugin_dir is None:
            logger.warning(
                "marketplace %s: not serving granted plugin %r — %s",
                slug,
                name,
                _no_local_dir_reason(raw.get("source")),
            )
            continue
        result.append(
            {
                "marketplace_id": marketplace_id,
                "marketplace_slug": slug,
                "original_name": name,
                "prefixed_name": _prefixed_name(slug, name),
                "manifest_name": resolve_manifest_name(plugin_dir, fallback=name),
                "version": row.get("version"),
                "raw": raw,
                "plugin_dir": plugin_dir,
            }
        )
    return result


STORE_MARKETPLACE_ID = "store"
"""Sentinel slug used for Store-derived plugin entries in the served
marketplace. ``is_valid_slug`` in ``src/marketplace.py`` rejects any admin
marketplace registering ``store`` as its slug, so collisions with admin
content are impossible."""

BUNDLE_PLUGIN_NAME = "flea"
"""Synth plugin that wraps every Store-installed skill and agent for a user
into a single Claude Code plugin. Skill / agent uploads share this single
plugin in the served marketplace; only ``type='plugin'`` Store entities
materialize as their own plugin entry. See ``resolve_user_marketplace``.

v49 phase-4: renamed from ``agnes-store-bundle`` to ``flea``. Clean cut —
``usage_events`` rows whose JSONL was written before the rename stay
attributed as ``source='builtin'``; no legacy-prefix fallback in the
attribution layer (``services/session_processors/usage_lib.py``)."""

BUNDLE_PREFIXED_NAME = "flea"
"""On-disk directory name in the served ZIP / git tree for the bundle plugin.
Lives under ``plugins/flea/...``. v49 phase-4: renamed from ``store-bundle``
for parity with the manifest plugin name."""

BUNDLE_DESCRIPTION = "Skills and agent templates you've installed from the Agnes Store"

# Files we strip from the per-entity tree when we merge it into the bundle —
# each entity's plugin.json is replaced by a single bundle plugin.json that
# the bundle synth-emits.
_BUNDLE_EXCLUDE_DIR = ".claude-plugin"


def is_agnes_only_path(rel_parts: tuple[str, ...]) -> bool:
    """True when a relative path inside a plugin / bundle source is Agnes-only.

    Two patterns covered:

    * any segment named ``.agnes`` — convention dir for cover photos and
      docs that should NEVER reach the synth Claude Code marketplace,
    * file named ``marketplace-metadata.json`` at any depth (typically lives at
      ``.claude-plugin/marketplace-metadata.json`` at the repo root, but this
      catches it wherever it appears).

    Used by both the synth ZIP/git tree assembly path and the ETag
    computation so adding/removing Agnes-only content never invalidates
    user-side caches.
    """
    if not rel_parts:
        return False
    if any(part == ".agnes" for part in rel_parts):
        return True
    if rel_parts[-1] == "marketplace-metadata.json":
        return True
    return False


def is_executable_file(path: Path) -> bool:
    """True when the file carries the owner-exec bit — preserved into the
    served ZIP (external_attr) and git tree (mode 100755) so a plugin's
    executable content (engine launchers its hooks invoke via
    ``${CLAUDE_PLUGIN_ROOT}``) still runs after install. False on stat
    failure: a vanished file degrades to the historical 644, never a crash."""
    try:
        return bool(path.stat().st_mode & 0o100)
    except OSError:
        return False


def is_unserved_path(rel_parts: tuple[str, ...]) -> bool:
    """True when a relative path inside a plugin / bundle source must never be
    served or hashed: Agnes-only enrichment (``is_agnes_only_path``) or VCS
    internals. A root-source plugin's ``plugin_dir`` IS the marketplace git
    clone, so its ``.git/**`` would otherwise be swept wholesale into the
    served ZIP — and into the git channel's synthetic tree, which git refuses
    to check out (no tree may contain a ``.git`` entry)."""
    if is_agnes_only_path(rel_parts):
        return True
    return any(part == ".git" for part in rel_parts)


def _bundle_files(bundle_dirs: list[Path]) -> list[tuple[str, Path]]:
    """Return [(relpath_in_bundle, abs_path)] for every file across the bundle
    sources, dropping each per-entity ``.claude-plugin/`` content (the bundle
    has its own synth plugin.json) AND any Agnes-only file (``.agnes/**`` or
    ``marketplace-metadata.json``) so the served Claude Code marketplace stays
    clean of Agnes-side enrichment."""
    out: list[tuple[str, Path]] = []
    for src in bundle_dirs:
        if not src.is_dir():
            continue
        bases = [src.resolve()]
        for f in sorted(p for p in src.rglob("*") if p.is_file()):
            # F-1b: guard here rather than in each caller — _bundle_files feeds
            # _compute_bundle_version, the ZIP packager, the git backend and the
            # cowork packager, so one check covers all four.
            if escapes_base(f, bases):
                continue
            rel_parts = f.relative_to(src).parts
            if rel_parts and rel_parts[0] == _BUNDLE_EXCLUDE_DIR:
                continue
            if is_unserved_path(rel_parts):
                continue
            out.append((Path(*rel_parts).as_posix(), f))
    return out


def _compute_bundle_version(bundle_dirs: list[Path]) -> str:
    """sha256[:16] of the bundle's bytes — bumps every install / uninstall
    /upload-update so Claude Code's plugin auto-update toggle picks up the
    change. Independent of the marketplace ETag."""
    h = hashlib.sha256()
    for rel, abs_path in _bundle_files(bundle_dirs):
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        h.update(abs_path.read_bytes())
        h.update(b"\x00")
    return h.hexdigest()[:16]


def _grant_key(entry: dict) -> str:
    """``<marketplace_slug>/<plugin_name>`` — the ``resource_grants``
    ``resource_id`` an admin-curated plugin entry is granted under, and
    therefore the id an ``agent_scope`` row / intersection member names."""
    return f"{entry['marketplace_id']}/{entry['original_name']}"


def _store_install_in_scope(row: dict, scoped: frozenset[str]) -> bool:
    """True when an agent's ``plugin`` scope names this Store install.

    Two accepted id forms (see ``_resolve_principal_marketplace``): the store
    entity id — unambiguous, since two owners may upload the same plugin name
    — or the ``store/<name>`` grant-key form, which matches the
    ``<marketplace>/<plugin>`` vocabulary admin-curated scope rows use.
    """
    return row.get("id") in scoped or f"{STORE_MARKETPLACE_ID}/{row.get('name')}" in scoped


def _resolve_principal_marketplace(conn: duckdb.DuckDBPyConnection, principal) -> List[dict]:
    """Served plugin set for a restricted principal (V1d seam 3).

    The live intersection is the authority: an admin-curated entry survives
    only when its ``<slug>/<name>`` grant key is in
    ``intersection['marketplace_plugin']``.

    For an ``AgentPrincipal`` the candidate set is the OWNER's served set,
    not the raw grant table — so the agent can never receive a plugin the
    owner is granted but has not put in their stack, and a scope row naming
    a plugin the owner does not hold is silently dropped (never an
    elevation). A ``SessionPrincipal`` has no single owner, so its candidate
    set is built from the intersection alone.

    **Store installs** (``source`` ``store`` / ``store-bundle``) are the
    owner's PERSONAL installs, resolved through ``user_store_installs`` and
    never through ``resource_grants`` — so they cannot appear in the
    intersection at all, exactly like MCP connections. They are therefore
    filtered against the agent's ``plugin`` scope rows **directly**
    (``agent_scope_filter``), not against the intersection, and only when the
    agent's ``plugins_mode`` is ``'selected'``. Keying off the mode is what
    keeps the common case intact: the broker mints an agent-session token as
    soon as an agent narrows *anything*, so a tables-narrowed agent keeps
    ``plugins_mode='all'`` and must still receive every install its owner has
    — dropping them there would strip plugins the scope never claimed to
    restrict.

    A store scope row may name either the store entity id (unambiguous — two
    owners may upload the same plugin name) or the ``store/<name>`` grant-key
    form that matches the admin-curated vocabulary. Filtering happens on the
    INSTALL rows (via ``store_install_filter``) rather than on the resulting
    entries, so the synthetic ``flea`` bundle — one served plugin wrapping
    every installed skill/agent — is narrowed per entity instead of being
    admitted wholesale by one in-scope sibling.
    """
    from app.resource_types import ResourceType
    from src.agent_scope_intersection import agent_scope_filter

    allowed = frozenset(principal.intersection.get(ResourceType.MARKETPLACE_PLUGIN.value, frozenset()))
    owner_user_id = getattr(principal, "owner_user_id", None)
    if not owner_user_id:
        # Co-session: no owner to resolve a served set from. The intersection
        # (built from the live participant set) is the whole authority.
        return _entries_for_grant_keys(allowed)

    scoped_plugins = agent_scope_filter(getattr(principal, "agent_id", None), "plugins_mode", "plugin")
    store_filter = None if scoped_plugins is None else partial(_store_install_in_scope, scoped=scoped_plugins)

    candidates = resolve_user_marketplace(conn, {"id": owner_user_id}, store_install_filter=store_filter)
    return [e for e in candidates if e.get("source") != "marketplace" or _grant_key(e) in allowed]


def _entries_for_grant_keys(keys: frozenset[str]) -> List[dict]:
    """Admin-curated marketplace entries for explicit ``<slug>/<name>`` grant
    keys, in a deterministic (marketplace_id, name) order so the ETag / git
    commit stays stable. Rows an admin has disabled are dropped, matching
    ``list_granted_for_groups``."""
    if not keys:
        return []
    root = get_marketplaces_dir()
    out: List[dict] = []
    for row in marketplace_plugins_repo().list_all():
        slug = row["marketplace_id"]
        name = row["name"]
        if f"{slug}/{name}" not in keys or row.get("admin_disabled"):
            continue
        raw = _resolve_raw(row.get("raw"))
        plugin_dir = _contained_plugin_dir(root, slug, name, source=raw.get("source"))
        if plugin_dir is None:
            logger.warning(
                "marketplace %s: not serving granted plugin %r — %s",
                slug,
                name,
                _no_local_dir_reason(raw.get("source")),
            )
            continue
        out.append(
            {
                "marketplace_id": slug,
                "marketplace_slug": slug,
                "original_name": name,
                "prefixed_name": _prefixed_name(slug, name),
                "manifest_name": resolve_manifest_name(plugin_dir, fallback=name),
                "version": row.get("version"),
                "raw": raw,
                "plugin_dir": plugin_dir,
                "source": "marketplace",
            }
        )
    return out


def resolve_user_marketplace(
    conn: duckdb.DuckDBPyConnection,
    user,
    *,
    store_install_filter: Optional[Callable[[dict], bool]] = None,
) -> List[dict]:
    """Final, served plugin set for a user (``dict``) or ``Principal``.

    Composition::

        (admin_granted ∩ (subscriptions ∪ required)) ∪ store_installs

    A restricted principal (co-session / agent-session token) takes the
    intersection-filtered path — see ``_resolve_principal_marketplace``.

    ``store_install_filter`` narrows the trailing ``∪ store_installs`` union,
    which the intersection cannot reach (personal installs are not
    ``resource_grants`` rows). It is applied to the raw ``user_store_installs``
    rows, so it also narrows what goes INTO the synthetic ``flea`` bundle and
    into that bundle's content hash. Only ``_resolve_principal_marketplace``
    passes it, and only for a ``plugins_mode='selected'`` agent; ``None``
    (every ordinary user) keeps today's behavior exactly.

    Output entries match ``resolve_allowed_plugins`` shape so that the
    existing ``packager`` / ``git_backend`` machinery iterates them
    transparently. Entries carry an extra ``source`` key (``"marketplace"``
    or ``"store"``) used by ``build_info`` for diagnostics — packager /
    git_backend ignore it.

    Ordering is deterministic: admin entries first (by registration time +
    name), then Store entries (by entity id) — so two requests with the same
    inputs produce byte-identical ZIPs and git commits.
    """
    from app.auth.session_principal import PRINCIPAL_TYPES

    if isinstance(user, PRINCIPAL_TYPES):
        return _resolve_principal_marketplace(conn, user)

    user_id = user.get("id")
    if not user_id:
        return []

    admin = resolve_allowed_plugins(conn, user)

    # Model B (v28+): RBAC grant is only eligibility — the user must explicitly
    # subscribe via /marketplace for a curated plugin to enter their served set.
    # Pre-v28 the filter was (rbac ∖ opt_outs); now it's (rbac ∩ in_stack),
    # where in_stack = subscriptions ∪ required-tier grant keys: a grant at
    # ``requirement='required'`` is always-in-stack for every group member,
    # matching the StackResolver union for data packages / memory domains.
    # Reads through the repo factory so subscriptions resolve correctly on
    # the active backend (PG / DuckDB) — same rationale as
    # ``resolve_allowed_plugins`` above.
    in_stack = user_curated_subscriptions_repo().subscribed_set(user_id) | required_plugin_keys(conn, user_id)
    admin = [p for p in admin if (p["marketplace_id"], p["original_name"]) in in_stack]
    for p in admin:
        p["source"] = "marketplace"

    store_root = get_store_dir()
    installs_repo = user_store_installs_repo()
    # Required-tier store entities are always in the served set. Curated plugins
    # get this from the `in_stack` union above (presence semantics in
    # user_plugin_optouts); an authored entity is served per-person out of
    # user_store_installs, so the union has to be materialized as install rows.
    # Doing it here — at the serve chokepoint — is what picks up someone who
    # joined the group after the grant was written.
    required_entity_ids = required_store_entity_keys(conn, user_id)
    if required_entity_ids:
        installs_repo.install_required_for_user(user_id, sorted(required_entity_ids))
    # A granted hidden entity is served to the group it was granted to; the
    # repo does not read grants, so the set is resolved here.
    installs = installs_repo.list_for_user(user_id, sorted(granted_store_entity_keys(conn, user_id)))
    if store_install_filter is not None:
        installs = [row for row in installs if store_install_filter(row)]
    store_plugin_entries: List[dict] = []
    bundle_rows: List[dict] = []
    for row in installs:
        if row.get("type") in ("skill", "agent"):
            bundle_rows.append(row)
            continue

        # type == 'plugin' (or any other future type that ships its own
        # plugin tree) gets its own entry — Claude Code shows them as
        # standalone plugins. all-or-nothing per spec.
        entity_id = row["id"]
        owner_username = row["owner_username"]
        original_name = row["name"]
        # v49 phase-3: stored synthetic_name from store_entities is the
        # canonical value baked into the on-disk plugin tree
        # (frontmatter name, plugin.json `name`). Reading it from DB
        # keeps the served manifest in lockstep with whatever the upload
        # / edit / archive paths last wrote. The column is NOT NULL +
        # explicitly selected by ``list_for_user``.
        manifest_name = row["synthetic_name"]
        plugin_dir = store_root / entity_id / "plugin"
        store_plugin_entries.append(
            {
                "marketplace_id": STORE_MARKETPLACE_ID,
                "marketplace_slug": STORE_MARKETPLACE_ID,
                "original_name": original_name,
                # Use entity_id (UUID-ish) for the on-disk dir prefix so two
                # owners uploading "code-review" never collide. The ZIP /
                # git tree groups them under plugins/store-<entity_id>/.
                "prefixed_name": f"store-{entity_id}",
                "manifest_name": manifest_name,
                "version": row.get("version"),
                "raw": {
                    "name": manifest_name,
                    "version": row.get("version"),
                    "description": row.get("description") or "",
                },
                "plugin_dir": plugin_dir,
                "source": "store",
                "entity_id": entity_id,
                "type": row.get("type"),
                "owner_username": owner_username,
            }
        )

    bundle_entry: List[dict] = []
    if bundle_rows:
        bundle_dirs = [store_root / r["id"] / "plugin" for r in bundle_rows]
        version = _compute_bundle_version(bundle_dirs)
        bundle_entry.append(
            {
                "marketplace_id": STORE_MARKETPLACE_ID,
                "marketplace_slug": STORE_MARKETPLACE_ID,
                "original_name": BUNDLE_PREFIXED_NAME,
                "prefixed_name": BUNDLE_PREFIXED_NAME,
                "manifest_name": BUNDLE_PLUGIN_NAME,
                "version": version,
                "raw": {
                    "name": BUNDLE_PLUGIN_NAME,
                    "version": version,
                    "description": BUNDLE_DESCRIPTION,
                },
                # No real on-disk root for the bundle — its content is
                # composed at serve time from `bundle_dirs`. packager /
                # git_backend / compute_etag check for `bundle_dirs` and
                # skip the usual `plugin_dir.is_dir()` path.
                "plugin_dir": None,
                "bundle_dirs": bundle_dirs,
                "source": "store-bundle",
                "bundle_entity_ids": [r["id"] for r in bundle_rows],
            }
        )

    return list(admin) + store_plugin_entries + bundle_entry


def resolve_user_groups(conn: duckdb.DuckDBPyConnection, user: dict) -> List[str]:
    """Return the names of groups this user belongs to, sorted alphabetically.

    Diagnostic only — the actual RBAC filtering of the marketplace feed is
    owned by ``resolve_allowed_plugins``. This helper backs the ``groups``
    field in ``/marketplace/info`` and in ``.agnes/version.json`` inside the
    ZIP, so an operator can read those payloads and answer "which groups
    granted me visibility into this plugin set?" without opening the admin UI.

    Membership semantics mirror ``app.auth.access._user_group_ids``:
    only real ``user_group_members`` rows are surfaced; there is no
    implicit Everyone membership.

    A restricted principal returns ``[]``: its served set comes from the live
    intersection, not from group membership, so naming groups here would
    describe an authority path that was never consulted (and a principal has
    no ``.get("id")`` to resolve one from).
    """
    from app.auth.session_principal import PRINCIPAL_TYPES

    if isinstance(user, PRINCIPAL_TYPES):
        return []

    user_id = user.get("id")
    if not user_id:
        return []
    group_ids = _user_group_ids(user_id, conn)
    if not group_ids:
        return []
    # Repo factory routes to the active backend (PG / DuckDB) — same
    # rationale as ``resolve_allowed_plugins``: raw SQL on the
    # DuckDB-typed ``conn`` would return [] on Postgres deployments
    # because the ``user_groups`` rows live in PG.
    return user_groups_repo().list_names_by_ids(group_ids)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def escapes_base(path: Path, bases: Iterable[Path]) -> bool:
    """True if ``path`` is a symlink or resolves outside EVERY base in ``bases``.

    Plugin content is curator-controlled. ``Path.rglob`` does not recurse into
    symlinked directories, but it DOES yield a symlinked *file*, and
    ``read_bytes()`` follows it — so a plugin containing ``leak.txt`` pointing at
    an absolute path exfiltrates that file into the served ZIP / git tree even
    when the plugin's own name is perfectly legitimate. ``cowork_packager`` has
    guarded this since it shipped; the ZIP and git backends and this module's
    ETag walk did not (2026-08-05 audit, F-1b).

    Rejecting every symlink outright, rather than only out-of-base ones, is what
    carries the defense: a symlinked file is refused no matter where it points.
    The repo ships no symlinks anywhere, so nothing legitimate breaks. The base
    comparison is the cheap second half, for a path that reaches outside without
    being a symlink itself.

    On bases, and the division of labour that makes this safe: callers pass the
    RESOLVED content directory being walked, not a global root. A ``plugin_dir``
    that is itself a symlink would re-anchor this check on the escaped target —
    the classic defeat, which ``app/api/marketplace.py:_reject_unsafe_segment``
    documents as audit L1 — but that case never reaches here, because
    ``_contained_plugin_dir`` refuses to produce a ``plugin_dir`` that escapes
    the marketplaces root. Layer A stops a symlinked directory; this is layer B
    and stops symlinked files inside a legitimate one.

    Deliberately NOT anchored on a config-derived global root. ``plugin_dir`` is
    an absolute path the caller supplies; a global anchor silently drops every
    file whenever the two disagree (bind-mounted ``/data``, a symlinked data dir,
    any test or dev override), which turns a hardening measure into a
    served-marketplace-goes-empty outage.
    """
    try:
        if path.is_symlink():
            return True
        resolved = path.resolve()
    except OSError:
        return True
    for base in bases:
        try:
            if resolved.is_relative_to(base.resolve()):
                return False
        except OSError:
            continue
    return True


def _iter_files(root: Path) -> List[Path]:
    # Base resolved once — escapes_base runs per file and this is the ETag hot
    # path (AGNES_MARKETPLACE_ETAG_TTL, default 120s).
    bases = [root.resolve()]
    return sorted(p for p in root.rglob("*") if p.is_file() and not escapes_base(p, bases))


def compute_etag(plugins: Iterable[dict]) -> str:
    """Content-addressed ETag for the user's plugin view.

    Two users with the same allowed set share the same ETag — so they also
    share the same bare-repo cache entry. When the source files change, the
    hash changes, so a stale 304 can never leak.

    ``SERVED_FORMAT_VERSION`` is folded in because content-addressing alone
    cannot see a change in how those files are packaged: a packaging fix
    leaves every hashed byte identical, so both caches would keep answering
    with the pre-fix bytes. Bumping the constant is what makes such a fix
    reach clients that already hold a copy.

    For bundle entries (``bundle_dirs`` set, ``plugin_dir`` is None) we hash
    every file under each source dir except the per-entity ``.claude-plugin/``
    content; the bundle ships one synth plugin.json so the per-entity ones
    don't enter the served tree.

    The per-file executable bit is hashed alongside the bytes because it is
    now part of what gets packaged (ZIP ``external_attr`` / git tree mode).
    A curator commit that only ``chmod +x``-es a launcher — the archetypal
    "fix the broken hook" commit — leaves every byte identical, so hashing
    bytes alone would answer ``If-None-Match`` with 304 and re-serve the
    cached ``<etag>.v<N>.git`` tree, both still mode 644.
    """
    tokens: List[List[Any]] = []
    for plugin in plugins:
        files: List[List[Any]] = []
        if plugin.get("bundle_dirs"):
            for rel, abs_path in _bundle_files(plugin["bundle_dirs"]):
                files.append([rel, _sha256_file(abs_path), is_executable_file(abs_path)])
        else:
            plugin_dir: Path = plugin["plugin_dir"]
            if plugin_dir is not None and plugin_dir.is_dir():
                for f in _iter_files(plugin_dir):
                    rel_parts = f.relative_to(plugin_dir).parts
                    if is_unserved_path(rel_parts):
                        continue
                    rel = f.relative_to(plugin_dir).as_posix()
                    files.append([rel, _sha256_file(f), is_executable_file(f)])
        tokens.append([plugin["prefixed_name"], plugin.get("version") or "", files])
    payload = json.dumps(
        {"format": SERVED_FORMAT_VERSION, "plugins": tokens},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]
