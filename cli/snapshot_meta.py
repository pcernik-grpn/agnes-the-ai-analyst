"""Snapshot sidecar metadata + file lock helpers (spec §4.2)."""

from __future__ import annotations
import contextlib
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# `fcntl` is POSIX-only. The CLI is primarily targeted at Mac/Linux laptops, but
# import-time failure on Windows would make the whole module (incl. read_meta /
# list_snapshots) unusable. Make the import lazy so non-locking helpers still
# work; `snapshot_lock` raises a clear error if anything tries to acquire it.
try:
    import fcntl as _fcntl  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — exercised only on Windows
    _fcntl = None  # type: ignore[assignment]


@dataclass
class SnapshotMeta:
    name: str
    table_id: str
    select: Optional[list[str]]
    where: Optional[str]
    limit: Optional[int]
    order_by: Optional[list[str]]
    fetched_at: str  # ISO 8601 UTC
    effective_as_of: str  # ISO 8601 UTC, server-side eval time
    rows: int
    bytes_local: int
    estimated_scan_bytes_at_fetch: int
    result_hash_md5: str
    # TTL expiry (#407). ISO 8601 UTC instant after which a lazy sweep is
    # allowed to delete this snapshot. None = no TTL (never auto-expires).
    # MUST stay the LAST field with a default so a legacy `meta.json` written
    # before TTL existed (no `expires_at` key) still deserializes via
    # `SnapshotMeta(**data)`.
    expires_at: Optional[str] = None
    # Table access policies §3.4/§10.3 (plan Task 18): the
    # `X-Agnes-Policy-Fingerprint` value `/api/v2/scan` returned when this
    # snapshot was fetched -- `sha256(access_policy_sql + '|' +
    # repr(caller_email) + '|' + repr(sorted(caller_group_names)))`, or
    # None when the source table carried no policy (or the fetch ran as
    # the admin bypass). `agnes pull` recomputes the CURRENT fingerprint
    # from the manifest and withholds this snapshot's view once the two
    # disagree. Also MUST stay LAST with
    # a default, same reason as `expires_at` above -- a `meta.json` written
    # before this feature existed has no such key.
    policy_fingerprint: Optional[str] = None
    # The registry id of the policied table `policy_fingerprint` was
    # computed for -- the `X-Agnes-Policy-Table-Id` header `/api/v2/scan`
    # sends alongside the fingerprint. Needed because `table_id` above is
    # NOT a registry id on the `--from-query` path (it is the snapshot name
    # the caller passed positionally), so `agnes pull` would otherwise have
    # no key to look the CURRENT fingerprint up by in the manifest, read
    # back `None`, and withhold the snapshot forever. Same LAST-with-a-
    # default rule as the two fields above.
    policy_table_id: Optional[str] = None


def _meta_path(snap_dir: Path, name: str) -> Path:
    return snap_dir / f"{name}.meta.json"


def write_meta(snap_dir: Path, meta: SnapshotMeta) -> None:
    snap_dir.mkdir(parents=True, exist_ok=True)
    with _meta_path(snap_dir, meta.name).open("w", encoding="utf-8") as f:
        json.dump(asdict(meta), f, indent=2)


def read_meta(snap_dir: Path, name: str) -> Optional[SnapshotMeta]:
    p = _meta_path(snap_dir, name)
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    return SnapshotMeta(**data)


def list_snapshots(snap_dir: Path) -> list[SnapshotMeta]:
    if not snap_dir.exists():
        return []
    out = []
    for meta_file in snap_dir.glob("*.meta.json"):
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
            out.append(SnapshotMeta(**data))
        except (json.JSONDecodeError, TypeError):
            continue
    return out


def delete_snapshot(snap_dir: Path, name: str) -> bool:
    """Delete the snapshot's parquet + meta. Returns True if removed, False if missing."""
    parquet = snap_dir / f"{name}.parquet"
    meta = _meta_path(snap_dir, name)
    removed = False
    if parquet.exists():
        parquet.unlink()
        removed = True
    if meta.exists():
        meta.unlink()
        removed = True
    return removed


def sweep_expired_snapshots(snap_dir: Path) -> list[str]:
    """Delete every snapshot whose `expires_at` is in the past (#407).

    Iterates `list_snapshots`, deletes the parquet + meta of each expired
    snapshot under the `snapshot_lock`, and returns the names swept (sorted).

    Tolerant by design — this runs on the lazy `agnes pull` path and must
    never block a pull:
    - snapshots with `expires_at=None` (no TTL) are left alone;
    - a future-dated `expires_at` is left alone;
    - an unparsable `expires_at` is left alone (treated as "no opinion"),
      never raised.

    The lock is acquired only when there is something to delete, so a pull
    against a TTL-free snapshot dir does no extra locking.
    """
    snaps = list_snapshots(snap_dir)
    now = datetime.now(timezone.utc)
    expired: list[str] = []
    for s in snaps:
        if not s.expires_at:
            continue
        try:
            exp = datetime.fromisoformat(s.expires_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            # Unparsable timestamp — don't guess, don't delete.
            continue
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp <= now:
            expired.append(s.name)

    if not expired:
        return []

    swept: list[str] = []
    with snapshot_lock(snap_dir):
        # Re-read meta under the lock and re-verify expiry: a concurrent
        # `agnes snapshot refresh --ttl <d>` (which acquires the same
        # `snapshot_lock`) may have re-anchored `expires_at` between our
        # initial read and the lock acquisition. Mirrors the TOCTOU guard
        # in `create_cmd` (cli/commands/snapshot.py: "re-check existence
        # here to close the TOCTOU window"). Devin Review BUG_0001 on #599.
        now_under_lock = datetime.now(timezone.utc)
        for name in expired:
            meta = read_meta(snap_dir, name)
            if meta is None or not meta.expires_at:
                continue
            try:
                exp = datetime.fromisoformat(meta.expires_at.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp > now_under_lock:
                # Refreshed between unlocked read and the lock — skip.
                continue
            if delete_snapshot(snap_dir, name):
                swept.append(name)
    return sorted(swept)


@contextlib.contextmanager
def snapshot_lock(snap_dir: Path):
    """Exclusive flock on snap_dir/.lock — serializes snapshot installs.

    Concurrent `agnes snapshot create` invocations queue here.
    """
    if _fcntl is None:
        raise RuntimeError(
            "snapshot_lock requires POSIX fcntl — Windows is not supported. "
            "Run `agnes` from a Mac or Linux machine, or use a WSL shell."
        )
    snap_dir.mkdir(parents=True, exist_ok=True)
    lock_file = snap_dir / ".lock"
    lock_file.touch(exist_ok=True)
    fd = open(lock_file, "r+")
    try:
        _fcntl.flock(fd.fileno(), _fcntl.LOCK_EX)
        yield
    finally:
        _fcntl.flock(fd.fileno(), _fcntl.LOCK_UN)
        fd.close()
