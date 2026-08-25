"""Shared utilities for the FastAPI application."""

import hashlib
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def get_data_dir() -> Path:
    """Return the configured data directory path."""
    return Path(os.environ.get("DATA_DIR", "./data"))


def uploaded_local_md_dir() -> Path:
    """``${DATA_DIR}/user_local_md`` — where ``POST /api/upload/local-md``
    deposits each analyst's ``CLAUDE.local.md``.

    Resolved per call rather than at import so it follows ``DATA_DIR`` for
    every caller (the corporate-memory collector runs in a different process
    than the upload endpoint).
    """
    return get_data_dir() / "user_local_md"


def local_md_filename(user_email: str) -> str:
    """Stable per-user filename for an uploaded ``CLAUDE.local.md``.

    Hashed rather than raw so no charset surprises from an email reach the
    filesystem; truncated to 24 hex chars, which is ample against collision
    for a single tenant's user set.

    Defined here — not inline at the write site — because BOTH the writer
    (``app/api/upload.py``) and the reader (``services/corporate_memory/
    collector.py``) must derive the identical name. They previously did not
    agree on the *directory*, which silently starved corporate memory of its
    input on every Docker deployment; one shared helper is what keeps the
    name from drifting the same way.
    """
    return hashlib.sha256(user_email.encode()).hexdigest()[:24] + ".md"


def _is_safe_table_segment(name: str) -> bool:
    """True when *name* is usable as ONE path segment under the extracts tree.

    Table ids reach the resolvers below straight off a request path
    (``/api/v2/schema/{table_id}``, ``/api/catalog/profile/{table_name}/…``),
    so they are untrusted input building a filesystem path. Rejects the
    separators, the empty string and the ``.``/``..`` navigators: ``..`` alone
    resolves ``extracts/<source>/data/..`` to the extract source root, which
    the directory resolvers would then recursively glob for every parquet
    under it. Today the routing layer happens to refuse a `%2F`-encoded
    separator before the handler runs, so a multi-segment escape is not
    reachable over HTTP — but that is a property of the transport, not a
    containment guarantee these helpers may lean on.

    Glob metacharacters are rejected for a second reason: the id is not only
    joined into a path, it is INTERPOLATED INTO A GLOB PATTERN —
    ``extracts.rglob(f"data/{table_id}.parquet")`` here and in
    ``app/api/catalog.py``, plus ``extracts.glob(f"*/data/{table_id}")`` below.
    A `*` or `[...]` therefore stops naming one table and starts matching an
    arbitrary one, so `POST /api/catalog/profile/*/refresh` would profile
    whichever parquet the pattern happened to hit and store it under the
    requested name (Devin Review on #1198). No identifier this repo registers —
    Keboola, BigQuery or Jira — can legitimately contain them.
    """
    return bool(name) and name not in (".", "..") and not set(name) & {"/", "\\", "\x00", "*", "?", "[", "]"}


def _contained(path: Path, root: Path) -> bool:
    """True when *path* really lives under *root* — resolved, so neither a
    ``..`` component nor a symlink planted inside the tree escapes it.

    Each ``extracts/*`` entry counts as a root in its own right. Resolving only
    against ``extracts`` itself would reject a whole extract SOURCE directory
    that an operator has symlinked onto another volume
    (``extracts/keboola`` → ``/mnt/big/keboola``) — deployment layout, not an
    escape — and every table under such a source would read as unsynced on every
    surface at once (Devin Review on #1198). Widening to the source roots costs
    nothing in containment: a link planted INSIDE one of them that points
    somewhere else resolves outside its own source root too, so it is still
    refused.
    """
    try:
        real = path.resolve()
        if real.is_relative_to(root.resolve()):
            return True
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            # `extracts/<source>` and `extracts/<source>/data` are both allowed
            # roots. The rule is not "one level deep" but "path components that
            # do NOT come from untrusted input": the source directory name is
            # scanned off disk and `data` is a literal, so an operator may point
            # either at another volume without weakening anything. The table id
            # is the only untrusted segment, and it is always the LAST one — so
            # a link named `<id>.parquet` or `<id>/` still has to resolve inside
            # one of these roots, and one pointing elsewhere is still refused.
            for candidate in (entry, entry / "data"):
                if candidate.is_dir() and real.is_relative_to(candidate.resolve()):
                    return True
        return False
    except OSError:
        return False


def resolve_local_parquet(table_id: str, source_type: str | None = None) -> Path | None:
    """Resolve the on-disk parquet for a local/materialized table.

    The v2 extract.duckdb contract lays parquets out at
    ``${DATA_DIR}/extracts/<source>/data/<table_id>.parquet`` where ``<source>``
    is the extract DIRECTORY NAME the orchestrator scanned — which is NOT
    necessarily equal to the registry ``source_type``. Built-in connectors
    happen to use a directory named after their source_type
    (``keboola``/``bigquery``), but a generic extract.duckdb may live under any
    directory name: e.g. the bundled ``demo`` extract registers its tables with
    ``source_type='local'`` while its parquets live under ``extracts/demo/``.
    Keying the path off ``source_type`` therefore looked up
    ``extracts/local/data/<id>.parquet`` (nonexistent) and crashed ``read_parquet``.

    Resolve by searching for ``data/<table_id>.parquet`` anywhere under the
    extracts tree — the same source-name-agnostic lookup ``app/api/catalog.py``
    and ``app/api/data.py`` already use. ``source_type``, when supplied, is
    tried first as a fast path (preserves the historical layout/behavior for
    built-in connectors and disambiguates the rare case of the same table_id
    appearing under two sources). Returns ``None`` when no parquet exists.
    """
    extracts = get_data_dir() / "extracts"
    if not extracts.exists() or not _is_safe_table_segment(table_id):
        return None
    if source_type and _is_safe_table_segment(source_type):
        direct = extracts / source_type / "data" / f"{table_id}.parquet"
        # Contained like every other candidate. The fast path returned `direct`
        # unchecked while the rglob fallback below and every
        # `_partition_dir_candidates` result were filtered — so a symlink
        # planted at `extracts/<source_type>/data/<id>.parquet` was the one way
        # back out of the tree, and it fed straight into
        # `resolve_local_parquet_glob` and the read surfaces (Devin Review
        # on #1198). A fast path may skip work, not the check.
        if direct.exists() and _contained(direct, extracts):
            return direct
    matches = [p for p in extracts.rglob(f"data/{table_id}.parquet") if _contained(p, extracts)]
    return matches[0] if matches else None


def _partition_dir_candidates(table_id: str, source_type: str | None) -> list[Path]:
    """Directories a partitioned table's parts could live in, best guess first.

    Mirrors :func:`resolve_local_parquet`'s source-name-agnostic lookup for the
    DIRECTORY layout: `source_type` (when supplied) is the fast path, then any
    `extracts/*/data/<table_id>` directory. Returns existing directories only,
    each validated as a single safe segment AND realpath-contained under
    `extracts/` — see :func:`_is_safe_table_segment`.
    """
    extracts = get_data_dir() / "extracts"
    if not extracts.exists() or not _is_safe_table_segment(table_id):
        return []
    out: list[Path] = []
    if source_type and _is_safe_table_segment(source_type):
        fast = extracts / source_type / "data" / table_id
        if fast.is_dir():
            out.append(fast)
    out.extend(p for p in extracts.glob(f"*/data/{table_id}") if p.is_dir() and p not in out)
    return [p for p in out if _contained(p, extracts)]


def resolve_local_partition_dir(table_id: str, source_type: str | None = None) -> Path | None:
    """The DIRECTORY holding a partitioned table's parts, or ``None``.

    For callers that want the directory itself rather than a read target —
    e.g. the profiler, which builds its own recursive read expression from it.
    Recursive existence check, so the nested hive layout
    (``month=YYYY-MM/data.parquet``, Jira) counts as parts too. A directory
    holding no parquet yet is ``None``: that is the pending-first-sync case.
    """
    for d in _partition_dir_candidates(table_id, source_type):
        if any(d.rglob("*.parquet")):
            return d
    return None


#: The read expression EVERY caller of :func:`resolve_local_parquet_glob` must
#: use, with the resolved target bound as the single `?` parameter.
#:
#: A shared symbol rather than a line in a docstring, because the docstring
#: version did not hold: the hive branch below changed what the resolver can
#: return, `v2_schema` and `v2_scan` were updated to match, and `v2_sample` —
#: the third caller — kept a bare `read_parquet(?)` and would have 500-ed on the
#: first Jira table whose monthly parts disagree about columns (Devin Review on
#: #1198). Importing the expression makes a fourth caller inherit the contract
#: instead of having to read about it; `tests/test_partitioned_table_surfaces.py`
#: asserts no caller reconstructs it by hand.
#:
#: `union_by_name` because hive part schemas drift month to month;
#: `hive_partitioning` because the `month=` directory segment is a column the
#: connector's own extract view already exposes. Both are no-ops for the
#: single-file and flat-partition targets.
LOCAL_PARQUET_READ_EXPR = "read_parquet(?, union_by_name=true, hive_partitioning=true)"


def _physical_key_candidates(table_id: str, registry_name: str | None) -> list[str]:
    """Filename keys a table's data may be stored under, best first.

    The write side keys the parquet filename by registry ``name``
    (`app/api/sync.py::_run_materialized_pass`; the extractors' `tc["name"]`),
    while the read surfaces receive the registry ``id`` off the request path.
    The register handler derives the id by slugifying the name (lower +
    spaces→underscores), so the two routinely differ — and then the id-keyed
    lookup misses a healthy, fully-synced table, which the read surfaces
    reported as a pending or failing first sync. Name first (it is what the
    sync writes), id second (rows where the two coincide, and any legacy
    id-keyed parquet).
    """
    out: list[str] = []
    for key in (registry_name, table_id):
        if key and key not in out:
            out.append(key)
    return out


def resolve_local_parquet_glob(
    table_id: str,
    source_type: str | None = None,
    *,
    registry_name: str | None = None,
) -> str | None:
    """A `read_parquet` target for a table, single-file OR partitioned.

    The partitioned sync writes `data/<table_id>/<partition>.parquet` — a
    DIRECTORY, not `data/<table_id>.parquet` — so `resolve_local_parquet` returns
    None for a healthy, fully-synced partitioned table. Callers that concluded
    "no parquet means nothing has landed yet" therefore reported a pending or
    failing first sync for a table whose every sync had succeeded
    (Devin Review on #1189).

    ``registry_name`` is the row's display ``name`` — the key the write side
    actually files the parquet under (see :func:`_physical_key_candidates`).
    Callers that have the registry row loaded should always pass it; omitted,
    the lookup is by ``table_id`` alone, exactly as before it existed.

    Returns the single file path, a flat `<dir>/*.parquet` glob for the
    per-period layout, a recursive `<dir>/**/*.parquet` glob for the nested hive
    layout (`month=YYYY-MM/data.parquet`, Jira), else None when no parquet
    exists in any of them.

    Callers MUST read the returned target through
    :data:`LOCAL_PARQUET_READ_EXPR` — the same expression the Jira extract's own
    view uses (`connectors/jira/extract_init.py`) — rather than a bare
    ``read_parquet(?)``. See that constant for why it is a shared symbol.

    Resolving hive here is what keeps the read surfaces agreeing with the
    catalog: :func:`local_parquet_size_bytes` already recurses, so leaving hive
    unresolved here published a size hint for a table that `/api/v2/schema` and
    `/api/v2/scan` then 404-ed on — an agent reading the catalog concluded the
    table was queryable when it was not (Devin Review on #1198).
    """
    for key in _physical_key_candidates(table_id, registry_name):
        target = _resolve_local_parquet_glob_one(key, source_type)
        if target is not None:
            return target
    return None


def _resolve_local_parquet_glob_one(table_id: str, source_type: str | None) -> str | None:
    """:func:`resolve_local_parquet_glob` for ONE filename key — see there.

    #1339: a table can ALSO have both a flat `<table_id>.parquet` file and a
    sibling `<table_id>/` partition directory at once — the sibling of the
    "directory holding both layouts" case documented in the loop below, one
    level up. The flat file wins here too (unchanged), but that collision is
    now logged at ERROR — see the check right after `single` resolves.
    TODO(#1339): flipping the precedence, or deleting the stale sibling, are
    open human decisions this fix does not make.
    """
    single = resolve_local_parquet(table_id, source_type)
    if single is not None:
        # One cheap `is_dir()` check — no glob — so this stays cheap on a hot
        # read path. `single`'s own directory is where a colliding partition
        # directory for the same table_id would live.
        sibling_dir = single.parent / table_id
        if sibling_dir.is_dir():
            logger.error(
                "Table %r has BOTH a flat parquet (%s) and a partition "
                "directory (%s) — serving the flat file (precedence "
                "unchanged), which may be stale relative to the partitioned "
                "data. See #1339.",
                table_id,
                single,
                sibling_dir,
            )
        return str(single)
    for d in _partition_dir_candidates(table_id, source_type):
        # Flat first, recursive only as a fallback — NOT interchangeable with an
        # unconditional `**`. A directory holding both layouts at once (a
        # `connectors.jira.transform.migrate_flat_to_hive` run interrupted
        # partway) makes `**` + `hive_partitioning` raise outright:
        #   BinderException: Hive partition mismatch between file "…"
        # because the top-level part carries no `month=` segment while the
        # nested one does. Measured on DuckDB 1.5.2.
        #
        # TODO(#1198 review): in that mixed state this reads the flat parts only,
        # while `local_parquet_size_bytes` sums both — the same
        # catalog/read-surface divergence this change set exists to close, just
        # narrowed to a window that should not persist (the migration MOVES flat
        # parts into `month=` dirs, so mixed is transient by construction).
        # Reading it whole needs `**` with `hive_partitioning=false`, which
        # returns every row but drops the `month` column pure-hive tables expose
        # — i.e. the fix is per-layout read options, not one shared expression,
        # and that is a contract change rather than a one-line swap.
        if any(d.glob("*.parquet")):
            return str(d / "*.parquet")
        if any(d.rglob("*.parquet")):
            return str(d / "**" / "*.parquet")
    return None


def local_parquet_size_bytes(
    table_id: str,
    source_type: str | None = None,
    *,
    registry_name: str | None = None,
) -> int | None:
    """Total on-disk bytes of a table's data, single-file OR partitioned.

    The size counterpart of :func:`resolve_local_parquet_glob`: callers that
    ``stat()``-ed the single file lost the number entirely for a partitioned
    table (a directory has no meaningful ``st_size``), so a healthy table
    reported no size at all. A partitioned table's size is the SUM over its
    parts — the same rollup the extractor writes into ``_meta.size_bytes`` and
    the orchestrator into ``sync_state.file_size_bytes``, so all three agree.

    Recurses, so the nested hive layout (``month=YYYY-MM/data.parquet``) is
    summed too. Returns ``None`` — never ``0`` — when no parquet exists in
    either layout: a partition directory holding no part yet is the
    pending-first-sync case, and ``0`` would read as "synced, and empty".

    ``registry_name`` follows the same name-first, id-fallback ordering as
    :func:`resolve_local_parquet_glob` (see :func:`_physical_key_candidates`),
    and for the same reason it must: keyed by id alone, the catalog published
    ``rough_size_hint: null`` for a name-keyed table that ``/schema``,
    ``/scan`` and ``/sample`` resolve and serve — the mirror of the
    disagreement recorded in that function's docstring, where a size hint was
    published for a table those surfaces then 404-ed on. The two lookups are
    a pair; they must agree on what "this table's data" means.
    """
    for key in _physical_key_candidates(table_id, registry_name):
        single = resolve_local_parquet(key, source_type)
        if single is not None:
            return single.stat().st_size
        part_dir = resolve_local_partition_dir(key, source_type)
        if part_dir is not None:
            return sum(p.stat().st_size for p in part_dir.rglob("*.parquet"))
    return None


def get_marketplaces_dir() -> Path:
    """Path where marketplace git repos are cloned by the nightly sync."""
    return get_data_dir() / "marketplaces"


def get_marketplace_cache_dir() -> Path:
    """Root for the curated-marketplace external-asset mirror.

    Each registered marketplace gets a sub-directory keyed by slug holding a
    ``manifest.json`` and one file per mirrored URL. Lives outside the cloned
    git working tree so its contents don't interfere with ``git status`` /
    ``git fetch --depth 1 ; git reset --hard`` semantics. Cleaned up
    alongside the working tree on marketplace unregister
    (``src.marketplace.delete_marketplace_dir``).
    """
    return get_data_dir() / "marketplace-cache"


def get_initial_workspace_dir() -> Path:
    """Path where the admin-configured Initial Workspace Template is cloned.

    Singleton (one per instance) — admin registers the repo via
    /admin/server-config → "Initial Workspace Template" section. Used by
    ``src.initial_workspace`` to clone/fetch and to serve via
    ``/api/initial-workspace.zip``. Layout:

        ${DATA_DIR}/initial-workspace/      ← git working copy
            .git/                            ← present on disk, excluded from zip
            CLAUDE.md, .claude/, ...         ← analyst workspace content
    """
    return get_data_dir() / "initial-workspace"


def get_store_dir() -> Path:
    """Root for community-uploaded Store entities.

    Layout:
        ${DATA_DIR}/store/<entity_id>/plugin/   ← canonical Claude Code plugin tree
        ${DATA_DIR}/store/<entity_id>/assets/   ← photo + docs
    """
    return get_data_dir() / "store"
