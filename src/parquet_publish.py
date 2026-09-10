"""Atomic parquet-publish protocol for the extract layout.

Every extractor eventually writes a parquet file into a location that OTHER,
unrelated code treats as already-complete: `src/orchestrator.py`'s
`_hash_table_parts` MD5s whatever bytes sit under `table_dir.rglob("*.parquet")`,
`cli/lib/pull.py` ships that hash (and the file) to every analyst, and the
master DuckDB views the orchestrator builds glob the same directory on every
query — including mid-write. A writer that lands bytes directly at the served
path, or that stages through a temp file without the guarantees below, can be
hashed, pulled, or queried half-written.

Lives here (`src/`), not under any one `connectors/` package, because the
invariant belongs to the READERS, not to any single writer: the hasher, the
view glob, and `agnes pull` are shared infrastructure every connector answers
to, so the primitive that keeps them safe belongs next to the contract it
protects. `connectors/jira/organizations.py` already had to reach across into
`connectors/jira/transform.py` — a module flagged sensitive in CLAUDE.md — for
what is a generic filesystem primitive with no Jira-specific content; that
reach-across was the tell that the helper had outgrown its original home.

Counter-argument, kept here rather than discarded: a reuse pass on the
original Jira-local helper (#1354) argued for keeping it connector-local,
because the writers publishing through it are genuinely not interchangeable —
different formats (Parquet via PyArrow, via DuckDB ``COPY``, via pandas),
different concurrency stories (a webhook-driven per-issue upsert vs. a
scheduled full-table materialize vs. an admin-triggered one-shot), and two of
the call sites this module now serves are not PyArrow at all. That argument is
right about the writers and wrong about the conclusion: the piece that is
actually shared is the PUBLISH protocol (temp path -> chmod -> replace), not
the writer. So this module exposes exactly that protocol and has no opinion
on format or compression — `atomic_publish` hands back a temp path and gets
out of the way; every call site still writes its own bytes with its own
writer and its own options, visible at the call site, not funneled through a
lowest-common-denominator wrapper here.

Mechanism (unchanged from the Jira original — see git history for
``connectors/jira/transform.py::write_parquet_atomic`` prior to this move):
write the full file to a **per-process** temp path beside the destination,
``chmod`` it ``0o644``, then ``os.replace`` it onto the destination.
``os.replace`` is atomic within a filesystem, so every reader sees either the
whole previous file or the whole new one, never a prefix.

Two incidents shaped this, both worth keeping in mind before changing it:

- The temp name is per-process (``<dest name>.<pid>.tmp``), not a fixed name
  derived only from ``dest``. A shared name let two writers each
  ``os.replace`` the other's in-flight file, with the loser's cleanup then
  deleting the winner's temp out from under it (Devin Review on #1274).
- This deliberately does not use ``tempfile.mkstemp``: it creates the file
  ``0600``, and ``os.replace`` preserves the mode, so the published parquet
  would silently drop from ``0644`` to ``0600`` (incident #203). The explicit
  ``chmod`` also defends the same outcome arriving from the writer's own
  default mode (``0666 & umask``) under a restrictive umask (``0077``, seen
  in some container/systemd units) — without it, the write's own umask would
  decide the published permissions by accident.
- Cleanup on a failed publish lives on the exception path, not in a
  ``finally``: a successful ``os.replace`` has already moved the temp away,
  so a ``finally`` would spend a failing ``unlink(2)`` (or a swallowed
  ``FileNotFoundError`` without ``missing_ok=True``) on every single
  successful publish — several of these call sites run on a poll/schedule
  measured in thousands of invocations per cycle. ``except BaseException``
  (not ``Exception``) keeps the coverage a ``finally`` had for
  ``KeyboardInterrupt``/``SystemExit``; unlinking only the CURRENT process's
  own temp — never a glob, never anything else in ``dest``'s directory — is
  what keeps that cleanup safe while another writer is concurrently
  mid-publish to the same ``dest``. That coverage spans the COMMIT as well
  as the write: the Jira original wrapped write + ``chmod`` + ``replace`` in
  one guard, and the ``chmod``/``replace`` pair is itself two syscalls with
  a real failure window between them (EPERM, ENOSPC, EXDEV, or a signal), so
  `atomic_publish_finalize` unlinks its own temp on failure rather than
  leaving one behind for nobody to collect (Devin review).

The temp name never matches a reader's ``*.parquet`` glob (it always ends in
``.tmp``), so a stray one left behind by a hard kill (SIGKILL, OOM) is inert —
never served, never hashed — until an operator or a later run cleans it up.

Two call shapes, one protocol:

- Most writers fit inside a single ``with`` block::

      with atomic_publish(dest) as tmp:
          pq.write_table(table, tmp, **your_own_options)

  or, for a DuckDB ``COPY``::

      with atomic_publish(dest) as tmp:
          safe = str(tmp).replace("'", "''")
          conn.execute(f"COPY (...) TO '{safe}' (FORMAT PARQUET)")

- A few writers span control flow too complex to nest cleanly inside one
  ``with`` (retries, several branches that may each populate the same temp).
  Those compute the temp path once with `atomic_publish_temp_path`, write to
  it however many steps that takes — cleaning up on their own failure paths,
  same as before this module existed — and call `atomic_publish_finalize`
  once, at the end, to commit.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "atomic_publish",
    "atomic_publish_finalize",
    "atomic_publish_temp_path",
    "partition_dir_supersedes_flat",
    "retire_superseded_parquet",
]

#: Characters a table-derived path segment may never contain. A `*`/`?`/`[`
#: matters beyond path building: sibling code INTERPOLATES the same name into
#: glob patterns (`app/utils.py::_is_safe_table_segment` documents the full
#: case), so a metacharacter stops naming one table and starts matching an
#: arbitrary one.
_UNSAFE_SEGMENT_CHARS = frozenset({"/", "\\", "\x00", "*", "?", "[", "]"})


def atomic_publish_temp_path(dest: Path | str) -> Path:
    """The per-process temp path a publish to *dest* must stage through.

    Exposed so callers that can't use the `atomic_publish` context manager
    directly (a write spanning retries/branches — see the module docstring)
    can still get the exact same per-process name, and so tests can assert
    against it without duplicating the naming scheme. Two processes (or two
    calls with a different ``os.getpid()``) publishing to the same *dest*
    always get two different temp paths.
    """
    dest = Path(dest)
    return dest.parent / f"{dest.name}.{os.getpid()}.tmp"


def atomic_publish_finalize(tmp: Path | str, dest: Path | str) -> Path:
    """Commit a completed temp write: ``chmod 0644``, then atomically replace.

    Pairs with `atomic_publish_temp_path` for call sites whose write is too
    spread out to nest inside `atomic_publish`'s ``with`` block.

    The split of responsibility is by half, not by function: cleaning up
    ``tmp`` if the **write** fails is still the caller's job along the way
    (only the caller knows which of its steps may leave a partial temp),
    but the **commit** cleans up after itself — if the ``chmod``/``replace``
    pair raises, ``tmp`` is unlinked before the exception propagates and
    *dest* is left untouched. Every explicit-pair call site hands the commit
    to this function and none of them wrap it, so a stranded temp here would
    have no owner at all.
    """
    tmp = Path(tmp)
    dest = Path(dest)
    try:
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
    except BaseException:
        # The commit is two syscalls, and everything between "chmod ran" and
        # "replace landed" is a window where the temp still exists and *dest*
        # is untouched: EPERM/EROFS from the chmod, ENOSPC/EXDEV from the
        # replace, or a KeyboardInterrupt/SystemExit arriving between them.
        # The Jira original this protocol came from wrapped write + chmod +
        # replace in ONE `except BaseException: unlink; raise`, so a failure
        # in the commit half still took the temp with it. Cleaning up here,
        # rather than only in `atomic_publish`, keeps that guarantee for the
        # explicit temp-path/finalize callers too — they hand the commit off
        # to this function and none of them wrap it (Devin review).
        # Idempotent: after a successful `os.replace` the temp is already
        # gone, and `missing_ok=True` makes a second unlink a no-op.
        tmp.unlink(missing_ok=True)
        raise
    return dest


@contextmanager
def atomic_publish(dest: Path | str) -> Iterator[Path]:
    """Publish *dest* so no reader ever observes a partial file.

    Yields the per-process temp path (see `atomic_publish_temp_path`) to
    write the FULL new content of *dest* to. Write it with whatever writer
    and options belong at your call site — ``pq.write_table(table, tmp,
    **your_options)``, ``df.to_parquet(tmp)``, or a DuckDB
    ``conn.execute(f"COPY (...) TO '{tmp}' (FORMAT PARQUET)")`` all work
    identically; this context manager has no opinion on format.

    On a clean exit, the temp is committed onto *dest* via
    `atomic_publish_finalize` (``chmod 0o644`` then ``os.replace`` — atomic
    within a filesystem). On any exception raised inside the ``with`` block,
    the temp is removed and the exception propagates; *dest* is left exactly
    as it was before the call. See the module docstring for the
    per-process-naming and cleanup-not-``finally`` reasoning (incidents
    #1274, #203).
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = atomic_publish_temp_path(dest)
    try:
        yield tmp
        atomic_publish_finalize(tmp, dest)
        # Inside the guard, not in an `else:`. `atomic_publish_finalize` already
        # cleans up after its own failure, so this is belt-and-braces — but it
        # keeps THIS function's stated contract ("on any exception the temp is
        # removed and *dest* is left exactly as it was") true on its own terms,
        # rather than resting on an internal detail of another function that a
        # later change could move. The extra unlink is a no-op (`missing_ok`).
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def partition_dir_supersedes_flat(flat: Path | str, table_dir: Path | str) -> bool:
    """Does the ``data/<table>/`` partition directory supersede the flat
    ``data/<table>.parquet`` sitting beside it? (#1339)

    ONE definition of the layout-collision verdict, imported by BOTH sites that
    must agree on it — `src/orchestrator.py::_update_sync_state` (what the
    manifest advertises, i.e. what `agnes pull` ships) and `app/utils.py`'s
    `resolve_local_parquet_glob` / `local_parquet_size_bytes` /
    `resolve_local_layout_target` (what `/api/v2/schema`, `/api/v2/scan`,
    `/api/v2/sample`, the catalog and the profiler read). It is a shared symbol
    rather than a rule written down twice because two copies of a comparator is
    exactly how the manifest and the read surfaces come to serve different data
    with nothing to say so.

    The collision is reached by a `sync_strategy` flip, which writes the new
    layout and leaves the old one in place — nothing in the tree removes the
    other layout, in EITHER direction. So the question is not "which shape do
    we prefer" but "which one is the current data", and the answer is
    **freshness**: ``True`` when at least one part under *table_dir* has an
    mtime ``>=`` *flat*'s.

    - ``any(part >= flat)`` is equivalent to ``max(parts) >= flat``, and the
      early exit is what keeps the common (fresher-directory) case from
      stat-ing the whole directory.
    - ``>=``, not ``>``: a real flip can land the flat file and the first part
      inside one filesystem timestamp tick, and the tie must resolve to the
      directory — that is the direction #1339 was filed for.
    - An empty directory never supersedes: no part means pending-first-sync,
      not fresher data, and letting it win would unpublish a healthy
      single-file table.
    - No flat file means there is no collision to arbitrate (``False``).

    **mtime is a heuristic, not a clock, and this does not pretend otherwise.**
    A coarse-granularity or network filesystem, a restored backup, a `cp`
    without ``-p``, a stray ``touch``, or a clock step can all make the older
    bytes look newer. There is no total order available here — the extract
    layout records no per-layout version — so this is the best available
    evidence, not proof. When it is wrong it picks the wrong layout for the
    manifest AND the read surfaces (they stay consistent with each other,
    which is the property that matters most), and the loser is deleted only in
    the direction where the directory won: a flat file wrongly judged stale is
    reclaimed, while a directory wrongly judged fresh leaves the flat file
    untouched. An operator who suspects a wrong verdict can re-run the extract
    for that table, which rewrites the winning layout with a current mtime.

    Never raises — it is consulted on a hot read path and inside the rebuild's
    per-source loop; an unreadable part is skipped, an unreadable tree is
    ``False``.
    """
    flat = Path(flat)
    table_dir = Path(table_dir)
    try:
        flat_mtime = flat.stat().st_mtime
    except OSError:
        return False
    try:
        for part in table_dir.rglob("*.parquet"):
            try:
                if part.stat().st_mtime >= flat_mtime:
                    return True
            except OSError:
                continue
    except OSError:
        return False
    return False


def retire_superseded_parquet(dest: Path | str, *, root: Path | str) -> bool:
    """Remove a parquet that a fresher layout has superseded, the same way
    `atomic_publish` lands one: through ONE ``os.replace`` onto a name no
    reader resolves or globs.

    The case this exists for (#1339): a table can carry both a flat
    ``data/<table>.parquet`` and a ``data/<table>/`` partition directory —
    a `sync_strategy: partitioned` flip writes the parts and nothing removed
    the previous strategy's flat file. When the directory is the fresher one
    (`partition_dir_supersedes_flat`, the comparator both precedence sites
    share) it wins on the manifest side
    (`src/orchestrator.py::_update_sync_state`) and on the read surfaces
    (`app/utils.py::resolve_local_parquet_glob`), so the flat sibling has to
    GO — a reader that reaches the extracts tree without going through those
    resolvers (an operator's own query, a future call site, an object-store
    mirror walking the directory) would otherwise still find it.

    Only ever called for the LOSER of that comparison, and only in that one
    direction. In the mirror direction (a stale directory beside a fresher flat
    file) nothing is deleted at all: reclaiming the fresher flat file would put
    the table in a loop — the extractor rewrites it, the next rebuild removes
    it again — and it is the only copy of the current data.

    Returns ``True`` when *dest* no longer exists on return — including when
    it was already absent, so a second rebuild over the same table is not
    reported as a persisting collision — and ``False`` when it was left in
    place, which the caller must treat as "the collision persists" and
    surface. Never raises: this runs inside the rebuild's per-source loop,
    where an exception would skip sync_state for every remaining table in the
    source.

    Ordering is the caller's half of the protocol and it is not optional:
    call this only AFTER the superseding data has been published (its parts
    written into the manifest). Reclaiming first would leave a window with
    neither layout published anywhere.

    **What the atomicity does and does not buy.** The name ``dest``
    disappears in a single ``os.replace``, so no reader ever observes a
    truncated or half-removed file, and a descriptor already open on it keeps
    reading the whole old inode to completion (POSIX). What no filesystem
    primitive can give is a reader that resolved the path and has not opened
    it yet: it gets ``ENOENT``. That window is why the resolvers flip in the
    same change — after the flip no read surface hands out this path once the
    directory has parts — and why a plain in-place ``unlink`` was not enough
    on its own. A hard kill between the replace and the unlink leaves the
    staged ``.tmp`` file, which is inert: no reader's ``*.parquet`` glob
    matches it (same property `atomic_publish`'s temp relies on). It is not
    swept by glob here — deleting by pattern in a directory another writer may
    be mid-publish into is exactly what the module docstring forbids.

    **Containment.** ``dest``'s last segment is built from a table name that
    comes from the ``table_registry``, which is fed by connector output — so
    validate AND contain, per the security playbook. Refused (``False``, real
    file untouched): a name that is not one safe segment, a name that is not a
    ``.parquet``, a parent that does not resolve to *root*, a symlink (renaming
    the link would leave the target — which a resolver following the link
    READ — in place, so it is not chased), and anything that is not a regular
    file. *root* itself may be a symlink (an operator pointing a source's
    ``data/`` at another volume is deployment layout, not an escape): both
    sides are resolved before comparison.
    """
    dest = Path(dest)
    root = Path(root)
    name = dest.name
    if not name or name in (".", "..") or set(name) & _UNSAFE_SEGMENT_CHARS:
        logger.warning("Refusing to retire %r — not a single safe path segment", str(dest))
        return False
    if not name.endswith(".parquet"):
        logger.warning("Refusing to retire %s — not a .parquet", dest)
        return False
    try:
        # Containment BEFORE the already-absent shortcut below: a path outside
        # *root* must be refused, not reported gone. "Gone" is the caller's cue
        # that the collision resolved, and it may not be earned by a path this
        # function never established it was allowed to look at.
        if os.path.realpath(dest.parent) != os.path.realpath(root):
            logger.warning("Refusing to retire %s — outside %s", dest, root)
            return False
        if dest.is_symlink():
            logger.warning("Refusing to retire %s — it is a symlink", dest)
            return False
        if not dest.exists():
            return True
        if not dest.is_file():
            logger.warning("Refusing to retire %s — not a regular file", dest)
            return False
        # Distinct from `atomic_publish_temp_path`'s name on purpose: a
        # concurrent publish to this same dest in this same process must not
        # find its in-flight temp replaced by the file we are retiring.
        staged = dest.parent / f"{name}.{os.getpid()}.stale.tmp"
        os.replace(dest, staged)
    except (OSError, ValueError) as e:
        logger.warning("Could not retire superseded parquet %s: %s", dest, e)
        return False
    try:
        staged.unlink(missing_ok=True)
    except OSError as e:
        # The replace already landed, so *dest* — the only name any reader
        # resolves — is gone and the collision IS resolved; reporting a
        # failure here would flag a healthy table forever. All that is left is
        # the inert `.tmp` residue no reader globs, same as after a hard kill
        # between the two steps.
        logger.warning("Retired %s but could not reclaim %s: %s", dest, staged, e)
    return True
