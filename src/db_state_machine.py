"""State machine for app-state DB backend.

Four production backends + one future placeholder:

  * ``DUCKDB``        — single-process DuckDB ($0, zero external deps). Was
                        the fresh-install default; since A1 a VM-provisioned
                        fresh install seeds ``SIDE_CAR`` instead (see
                        ``infra/modules/customer-instance/startup-script.sh.tpl``).
                        DUCKDB remains fully supported — the persisted state
                        for existing instances, and the module's own safe
                        fallback (below) when no state is configured at all.
  * ``SIDE_CAR``      — Postgres container on-VM, app+scheduler reach it
                        over the compose network (multi-process safe today)
  * ``CLOUD``         — managed Postgres (Cloud SQL / RDS / Supabase / …)
                        via Auth Proxy; HA, managed backups, PITR
  * ``DUCKDB_QUACK``  — DuckDB's multi-process Quack protocol. Placeholder
                        state; production-ready in DuckDB 2.0 (~fall 2026).
                        Accepted as a target only when the runtime DuckDB
                        version supports Quack — until then transitions
                        into this state raise ``NotImplementedError``.

Transitions are **multi-destination** (not forward-only). Any stable
backend can migrate to any other stable backend — operators move between
them as cost, HA needs, or compliance requirements shift. The migrator
handles both directions via the appropriate copy primitive:

  * DUCKDB → SIDE_CAR / CLOUD       — ``copy_duckdb_to_pg``
  * SIDE_CAR ↔ CLOUD                — ``copy_pg_to_pg``
  * SIDE_CAR / CLOUD → DUCKDB       — ``copy_pg_to_duckdb`` (DuckDB UPSERT)
  * DUCKDB ↔ DUCKDB_QUACK           — (future) Quack-aware file conversion

Transient ``*_in_progress`` states track in-flight migrations so the API
can reject concurrent attempts and the app can detect crashed migrations
on startup. Cancel-mid-migration reverts to the source backend (B1 fix).

Spec: docs/superpowers/specs/2026-05-27-db-backend-state-machine-design.md
"""

from __future__ import annotations
import fcntl
import logging
import os
from enum import StrEnum
from pathlib import Path
from typing import Self

import yaml

logger = logging.getLogger(__name__)


class BackendState(StrEnum):
    """Backend states for the app-state DB layer.

    Values are persisted verbatim in ``instance.yaml::database.backend`` and
    in audit-log rows — do not rename without a migration that rewrites
    persisted state.
    """

    DUCKDB = "duckdb"
    SIDE_CAR = "side_car"
    CLOUD = "cloud"
    # DuckDB Quack — placeholder. Production-ready in DuckDB 2.0 (~fall
    # 2026); until then ``validate_transition`` will reject targets that
    # equal this value with ``NotImplementedError``. Reserved here so the
    # state machine API + persisted-state contract are stable when
    # support lands; operators encountering this value in an old
    # instance.yaml see a clear "not yet supported" error rather than
    # an unknown-state crash.
    DUCKDB_QUACK = "duckdb_quack"
    SIDE_CAR_IN_PROGRESS = "side_car_in_progress"
    CLOUD_IN_PROGRESS = "cloud_in_progress"
    DUCKDB_QUACK_IN_PROGRESS = "duckdb_quack_in_progress"


class InvalidTransitionError(ValueError):
    """Requested transition is not allowed from the current state."""


class BackendNotYetSupportedError(NotImplementedError):
    """Requested target backend is reserved in the API but not yet
    runtime-supported. Currently only ``DUCKDB_QUACK`` — production-ready
    in DuckDB 2.0 (~fall 2026).
    """


# Multi-destination transition matrix.
#
# Every stable backend can migrate to every other stable backend. The
# migrator dispatches based on the (source, target) pair to the right
# copy primitive (``copy_duckdb_to_pg``, ``copy_pg_to_pg``,
# ``copy_pg_to_duckdb``, …) — see ``scripts/db_state_migrator.py``.
#
# Each stable backend's data is preserved on-disk after a cutover (the
# compressed backup is the recovery artifact + a re-source if the
# operator decides to migrate back). DuckDB → PG → DuckDB cycles are
# supported by design; the migrator's UPSERT path makes reverse
# migrations idempotent.
#
# ``DUCKDB_QUACK`` is in the transition graph but ``validate_transition``
# raises ``BackendNotYetSupportedError`` for any transition targeting it
# until DuckDB 2.0 ships the production-grade Quack protocol.
#
# In-progress states retain their old "revert to stable" reads so a
# crashed migration can be retried; the cancel API path uses those
# (B1 fix: cancel reverts to source_backend, not target).
_STABLE_BACKENDS: tuple[BackendState, ...] = (
    BackendState.DUCKDB,
    BackendState.SIDE_CAR,
    BackendState.CLOUD,
    BackendState.DUCKDB_QUACK,
)

_ALLOWED_TRANSITIONS: dict[BackendState, list[BackendState]] = {
    # Any stable backend → any other stable backend.
    BackendState.DUCKDB: [b for b in _STABLE_BACKENDS if b != BackendState.DUCKDB],
    BackendState.SIDE_CAR: [b for b in _STABLE_BACKENDS if b != BackendState.SIDE_CAR],
    BackendState.CLOUD: [b for b in _STABLE_BACKENDS if b != BackendState.CLOUD],
    BackendState.DUCKDB_QUACK: [b for b in _STABLE_BACKENDS if b != BackendState.DUCKDB_QUACK],
    # In-progress states → only revert to the stable variant (retry semantic).
    BackendState.SIDE_CAR_IN_PROGRESS: [BackendState.SIDE_CAR],
    BackendState.CLOUD_IN_PROGRESS: [BackendState.CLOUD],
    BackendState.DUCKDB_QUACK_IN_PROGRESS: [BackendState.DUCKDB_QUACK],
}

# Targets not yet runtime-implemented. Validated separately from the
# transition graph so operators see a clear "not yet supported" error
# instead of "invalid transition" when they hit a placeholder state.
_NOT_YET_SUPPORTED_TARGETS: frozenset[BackendState] = frozenset(
    {
        BackendState.DUCKDB_QUACK,
        BackendState.DUCKDB_QUACK_IN_PROGRESS,
    }
)


def allowed_transitions(current: BackendState) -> list[BackendState]:
    """List of allowed target states from ``current`` (graph-level).

    Includes placeholder targets like ``DUCKDB_QUACK`` — the API surface
    advertises them so clients (admin UI, CLI) can show "available when
    DuckDB 2.0 ships". The actual migrate endpoint will reject them via
    :func:`validate_transition`'s ``BackendNotYetSupportedError`` until
    runtime support lands.
    """
    return _ALLOWED_TRANSITIONS[current]


def validate_transition(current: BackendState, target: BackendState) -> None:
    """Raise on invalid transition.

    * ``InvalidTransitionError`` — target is not reachable from current
      in the transition graph (or current is in_progress and target is
      not its stable variant).
    * ``BackendNotYetSupportedError`` — target is graph-reachable but
      not yet runtime-implemented (placeholder state, e.g.
      ``DUCKDB_QUACK`` until DuckDB 2.0).
    """
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidTransitionError(
            f"Transition {current.value} → {target.value} not allowed. "
            f"From {current.value}, allowed targets: "
            f"{[t.value for t in _ALLOWED_TRANSITIONS[current]] or 'none (terminal state)'}"
        )
    if target in _NOT_YET_SUPPORTED_TARGETS:
        raise BackendNotYetSupportedError(
            f"Target backend {target.value!r} is reserved in the state "
            f"machine API but not yet runtime-supported. "
            f"``DUCKDB_QUACK`` becomes available with DuckDB 2.0 "
            f"(currently in beta as of DuckDB 1.5.2; production-ready "
            f"expected ~fall 2026)."
        )


_OVERLAY_PATH = Path(os.environ.get("DATA_DIR", "/data")) / "state" / "instance.yaml"
_LOCK_PATH = Path(os.environ.get("DATA_DIR", "/data")) / "state" / "db-migration.lock"

# Process-lifetime memoization of the parsed overlay.
#
# ``read_backend_state()`` runs on EVERY ``*_repo()`` factory access via
# ``use_pg()`` — a single RBAC/catalog request fans out into dozens of repo
# calls, each of which was re-reading and re-parsing this small YAML file.
# Pure-Python PyYAML holds the GIL, so on a single-uvicorn instance the
# repeated ``yaml.safe_load`` serialized the event loop and capped catalog
# throughput at ~2 req/s (py-spy: ~48% of request CPU in yaml.safe_load).
#
# The backend never changes within one process lifetime: a migration runs
# the migrator as a host SUBPROCESS and then RESTARTS the app on the new
# backend (app/api/db_state.py), so a fresh process always reads the new
# state with a cold cache. That makes a plain parse-once cache correct with
# no mtime guard — parse the overlay once, hold the result for the life of
# the process. ``write_backend_state()`` still resets the cache so an
# in-process write (and every test that writes-then-reads) is observed, and
# ``reset_backend_state_cache()`` is the explicit invalidation hook.
_STATE_CACHE: "tuple[BackendState, str | None] | None" = None


def reset_backend_state_cache() -> None:
    """Drop the in-process cache of the parsed overlay state.

    The next :func:`read_backend_state` call re-reads + re-parses the
    overlay from disk. Runtime correctness does not depend on this (a
    backend transition restarts the process — see the module note above);
    it exists for tests that simulate a backend flip in one process and as
    the single invalidation point wired into
    ``app.instance_config.reset_database_cache``.
    """
    global _STATE_CACHE
    _STATE_CACHE = None


def read_backend_state() -> tuple[BackendState, str | None]:
    """Read current backend + url from instance.yaml overlay.

    Memoized for the life of the process; see the module note on
    ``_STATE_CACHE``. Returns (BackendState.DUCKDB, None) when the overlay
    is missing or the ``database`` key is absent — a safe zero-config
    fallback, NOT the fresh-install default (since A1 a VM-provisioned
    fresh install writes ``backend: side_car`` into the overlay before the
    app ever starts — see ``infra/modules/customer-instance/startup-script.sh.tpl``).
    This branch only fires when no state is configured at all (e.g. local
    dev without ``instance.yaml``); it deliberately does not assume
    Postgres, since nothing here confirms one is actually reachable.
    """
    global _STATE_CACHE
    cached = _STATE_CACHE
    if cached is not None:
        return cached

    if not _OVERLAY_PATH.exists():
        result: "tuple[BackendState, str | None]" = (BackendState.DUCKDB, None)
        _STATE_CACHE = result
        return result
    try:
        data = yaml.safe_load(_OVERLAY_PATH.read_text()) or {}
    except yaml.YAMLError as e:
        # B2-NEW: pre-fix this silently returned DUCKDB. The bash fallback
        # writer could leave a malformed overlay (URL with YAML-special
        # chars interpolated unquoted) and the API would serve
        # `backend=duckdb` while data was actually on PG. Log at WARNING
        # so the operator + log-shipper notice. The safe-fallback behaviour
        # is preserved — refusing to boot the app is worse than a warning.
        #
        # Deliberately NOT cached: a malformed overlay is rare and the
        # parse is cheap relative to the happy path, and not memoizing the
        # error means a repaired overlay is picked up on the next read
        # without waiting for a restart.
        logger.warning(
            "instance.yaml YAML parse failed (path=%s, err=%s); "
            "failing safe to (DUCKDB, None) — investigate the overlay",
            _OVERLAY_PATH,
            e,
        )
        return BackendState.DUCKDB, None
    db = data.get("database") or {}
    backend_str = db.get("backend", "duckdb")
    try:
        state = BackendState(backend_str)
    except ValueError:
        state = BackendState.DUCKDB
    result = (state, db.get("url"))
    _STATE_CACHE = result
    return result


def write_backend_state(target: BackendState, *, url: "str | None" = ...) -> None:  # type: ignore[assignment]
    """Atomically update instance.yaml::database = {backend, url}.

    Uses tmp + os.replace for atomicity (same pattern as
    app/api/admin.py overlay writer). Caller is responsible for
    transition validation; this function performs no policy check.

    ``url`` sentinel semantics:

    * ``url=...`` (``Ellipsis``, the default) — leave the existing url
      key in instance.yaml unchanged.  Used when marking a migration
      in-progress so the live DATABASE_URL is not erased while the
      applier runs (B4 fix).
    * ``url="postgresql://..."`` — set the url to the given string.
    * ``url=None`` — remove the url key (transition to a stateless
      backend such as DuckDB).

    All other top-level keys (logging, auth, feature flags) are
    preserved; only the ``database`` subkey is touched.
    """
    _OVERLAY_PATH.parent.mkdir(parents=True, exist_ok=True)

    if _OVERLAY_PATH.exists():
        data = yaml.safe_load(_OVERLAY_PATH.read_text()) or {}
    else:
        data = {}

    db: dict = dict(data.get("database") or {})
    db["backend"] = target.value
    if url is ...:
        # Sentinel: keep whatever url is currently stored.
        pass
    elif url is None:
        db.pop("url", None)
    else:
        db["url"] = url
    data["database"] = db

    tmp = _OVERLAY_PATH.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=True))
    # 0600 on the TEMP file, before the rename. This used to chmod the
    # destination afterwards, which leaves the real path observable at the
    # umask default for the window between the two calls — and the file holds
    # the database url with its password inline. `os.replace` is atomic and
    # carries the temp file's mode, so doing it here closes the window
    # entirely. The overlay's other writers (app/api/admin.py,
    # app/api/initial_workspace.py) follow the same order.
    os.chmod(tmp, 0o600)
    os.replace(tmp, _OVERLAY_PATH)

    # Invalidate the parse-once cache so an in-process write is
    # observed by the next read — most write_backend_state callers are the
    # migrator, but tests and any same-process flip rely on this.
    reset_backend_state_cache()


class MigrationInProgressError(RuntimeError):
    """A migration is already running; second concurrent attempt rejected."""


class MigrationLock:
    """Non-blocking flock at _LOCK_PATH.

    Usage:
        with MigrationLock():
            # exclusive section
            ...
    """

    def __init__(self) -> None:
        self.held = False
        self._fd: int | None = None

    def __enter__(self) -> Self:
        _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(_LOCK_PATH), os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            os.close(self._fd)
            self._fd = None
            raise MigrationInProgressError(f"Migration already in progress (lock held at {_LOCK_PATH})") from e
        self.held = True
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
        self.held = False
