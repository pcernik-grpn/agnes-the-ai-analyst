"""Postgres-backed repository for the ``jobs`` table (durable job queue, v94).

Mirrors ``src/repositories/jobs.py``. Idempotency dedup is enforced with
a partial unique index (``idx_jobs_idem`` — ``WHERE idempotency_key IS
NOT NULL AND status IN ('queued', 'running')``, see
``migrations/versions/0041_jobs_v94.py`` and ``src/models/jobs.py``) as
the ``ON CONFLICT`` arbiter for the insert below.

This is deliberately NOT a plain SELECT-then-INSERT: under READ
COMMITTED, two concurrent transactions can both miss each other's
uncommitted row and both insert (empirically: 8 concurrent enqueues of
the same key produced 8 rows). ``INSERT ... ON CONFLICT ... DO NOTHING``
lets Postgres's own unique-index conflict check make the race atomic —
a second transaction inserting the same still-queued/running key blocks
on the first's row lock, then sees the conflict once it commits and
takes the ``DO NOTHING`` branch instead of inserting a duplicate.

The fallback SELECT that finds the winner's row (below) has its own
narrow race window: if the winner's job finishes (or is cancelled) between
our INSERT losing ``ON CONFLICT`` and the SELECT running, the winner's row
has already left ``'queued'``/``'running'`` — the SELECT misses it, but by
the same token the partial index no longer excludes its key. ``enqueue()``
retries the INSERT in that case (bounded — see ``max_fallback_retries``
inline) rather than asserting, since the key is now legitimately free for
reuse.

DuckDB has no partial-index support, so its sibling (``src/db.py`` /
``JobsRepository.enqueue()``) keeps the app-level check-then-insert
(guarded by an in-process lock, safe under DuckDB's single-writer
model) — see that module's docstring. The CONTRACT shared by both
backends is the dedup *behavior* (matching key + queued/running status
returns the existing row, no insert), not the mechanism.

Claim/lease/complete/fail lifecycle (mirrors ``JobsRepository`` — see
that module's docstring for the full shared-contract writeup):

- ``claim_next()`` uses ``SELECT ... FOR UPDATE SKIP LOCKED`` inside a
  single ``WITH ... UPDATE ... FROM ... RETURNING`` statement, so the
  row-select-and-lock and the mutation happen atomically in one
  round-trip — a plain SELECT-then-UPDATE here WOULD be racy under READ
  COMMITTED (two concurrent transactions could both select the same
  row before either commits its UPDATE) and is exactly the bug class
  the enqueue()/``ON CONFLICT`` fix above already had to close for
  inserts. ``MATERIALIZED`` pins the CTE so Postgres can't inline it in
  a way that reorders the lock relative to the join.
- ``heartbeat()``/``complete()``/``fail()`` don't need ``FOR UPDATE``:
  every mutating statement's ``WHERE`` clause re-checks
  ``lease_token = :lease_token AND status = 'running'`` at UPDATE time
  (under READ COMMITTED, each statement re-reads current committed
  state), so if another claim reclaims the job between our read and our
  write, the WHERE clause simply matches zero rows instead of clobbering
  the new owner.

Same-worker double-execution note (mirrors ``JobsRepository``): the guard
is keyed on ``lease_token``, NOT ``leased_by``/``worker_id``. All lane
slots inside one worker *process* share the same ``worker_id``
(``hostname:pid``), so a ``leased_by = :worker_id`` guard cannot tell a
stale slot's late call apart from a same-process reclaim of the same job
by a DIFFERENT slot — empirically reproduced double-execution bug.
``claim_next()`` mints a fresh ``uuid4`` ``lease_token`` on every claim
(including a same-worker reclaim), so the guard distinguishes claims
even when ``worker_id`` is identical. ``worker_id``/``leased_by`` remain
for audit/logging only.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class JobsPgRepository:
    #: Worker-runtime lane identifiers — see ``JobsRepository.HEAVY_LANE``
    #: for why these are duplicated rather than imported from the DuckDB
    #: sibling module.
    HEAVY_LANE = "heavy"
    LIGHT_LANE = "light"
    EXTRACTION_LANE = "extraction"

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @staticmethod
    def _decode(d: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(d.get("payload_json"), str):
            try:
                d["payload_json"] = json.loads(d["payload_json"]) if d["payload_json"] else {}
            except (TypeError, ValueError):
                d["payload_json"] = {}
        return d

    def enqueue(
        self,
        kind: str,
        payload: dict,
        *,
        priority: int = 0,
        run_after: Optional[datetime] = None,
        max_attempts: int = 3,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Insert a queued job and return its row.

        If ``idempotency_key`` matches an existing job whose status is
        still ``'queued'`` or ``'running'``, that row is returned
        unchanged — no new insert (dedup), race-safe under concurrent
        callers. Mirrors ``JobsRepository.enqueue`` (dedup *behavior*,
        not the underlying mechanism — see the module docstring).

        The returned dict always carries a ``"deduped"`` boolean key —
        ``True`` when the row came back via the idempotency dedup path
        (an existing queued/running job, not a fresh insert), ``False``
        for a brand-new row. This is response metadata added on the way
        out, NOT a ``jobs`` table column — see ``JobsRepository.enqueue``
        for why callers should branch on it instead of a racy pre-check.
        """
        now = datetime.now(timezone.utc)

        def _try_insert(conn: sa.engine.Connection) -> Optional[Any]:
            return (
                conn.execute(
                    sa.text(
                        """INSERT INTO jobs
                           (id, kind, payload_json, status, priority, run_after,
                            attempts, max_attempts, idempotency_key, created_at)
                           VALUES (:id, :kind, :payload_json, 'queued', :priority, :run_after,
                                   0, :max_attempts, :idempotency_key, :created_at)
                           ON CONFLICT (idempotency_key)
                               WHERE idempotency_key IS NOT NULL AND status IN ('queued', 'running')
                           DO NOTHING
                           RETURNING *"""
                    ),
                    {
                        "id": uuid.uuid4().hex,
                        "kind": kind,
                        "payload_json": json.dumps(payload or {}),
                        "priority": priority,
                        "run_after": run_after,
                        "max_attempts": max_attempts,
                        "idempotency_key": idempotency_key,
                        "created_at": now,
                    },
                )
                .mappings()
                .first()
            )

        # Bounds the fallback-miss retry loop below (see comment inline for
        # why a retry — rather than the miss being a hard failure — is
        # correct here).
        max_fallback_retries = 3
        with self._engine.begin() as conn:
            row = _try_insert(conn)
            deduped = False
            attempt = 0
            while row is None:
                # Lost the INSERT race: another transaction holds a
                # queued/running row for this key (NULL keys never
                # conflict — the partial index excludes them — so this
                # only happens when idempotency_key is not None). Look up
                # the winner's row to return it unchanged.
                assert idempotency_key is not None
                row = (
                    conn.execute(
                        sa.text(
                            """SELECT * FROM jobs
                               WHERE idempotency_key = :key AND status IN ('queued', 'running')
                               ORDER BY created_at LIMIT 1"""
                        ),
                        {"key": idempotency_key},
                    )
                    .mappings()
                    .first()
                )
                if row is not None:
                    deduped = True
                    break
                # Race window: between our INSERT losing the ON CONFLICT
                # race and this SELECT running, the winning row left
                # 'queued'/'running' (e.g. it finished to 'done'). The
                # SELECT above no longer sees it, but for the same reason
                # the partial unique index no longer excludes its key
                # either — the key is free for reuse. Retry the INSERT
                # instead of asserting; it should now succeed (or, rarely,
                # hit a brand-new live conflict, which loops again, bounded
                # by max_fallback_retries).
                attempt += 1
                if attempt > max_fallback_retries:
                    raise RuntimeError(
                        f"enqueue: exhausted {max_fallback_retries} fallback retries for "
                        f"idempotency_key={idempotency_key!r} — repeated concurrent churn "
                        "on this key prevented both insert and lookup from succeeding"
                    )
                row = _try_insert(conn)
                # If this retry succeeds, `row` is a fresh insert (RETURNING
                # *), so `deduped` correctly stays False; if it misses again,
                # the loop re-enters and either sets deduped=True via the
                # SELECT above or raises past max_fallback_retries.
        assert row is not None  # inserted (first try or a retry), or the conflicting row was found
        result = self._decode(dict(row))
        result["deduped"] = deduped
        if not deduped:
            # Low-latency worker wakeup (three-plane §3.3): a fresh enqueue
            # pings the ``agnes_jobs`` channel so an idle worker lane slot
            # (LISTENing via ``app.worker.wakeup``) claims immediately
            # instead of waiting out its poll interval. Separate short
            # transaction — a NOTIFY need not be atomic with the insert (a
            # spurious early wake just costs one claim attempt), and this
            # keeps it clear of the ON CONFLICT retry loop above. Channel
            # literal must match ``app.worker.wakeup.NOTIFY_CHANNEL``;
            # duplicated rather than imported so this repo carries no
            # dependency on the ``app`` package. Best-effort: a NOTIFY
            # failure must never fail the enqueue (polling is the floor).
            try:
                with self._engine.begin() as conn:
                    conn.execute(sa.text("NOTIFY agnes_jobs"))
            except Exception:
                pass
        return result

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(sa.text("SELECT * FROM jobs WHERE id = :id"), {"id": job_id}).mappings().first()
        return self._decode(dict(row)) if row else None

    def list(
        self,
        *,
        status: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM jobs WHERE 1=1"
        params: Dict[str, Any] = {"limit": limit}
        if status is not None:
            sql += " AND status = :status"
            params["status"] = status
        if kind is not None:
            sql += " AND kind = :kind"
            params["kind"] = kind
        sql += " ORDER BY created_at DESC LIMIT :limit"
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql), params).mappings().all()
        return [self._decode(dict(r)) for r in rows]

    def claim_next(
        self,
        *,
        kinds: List[str],
        worker_id: str,
        lease_seconds: int = 120,
    ) -> Optional[Dict[str, Any]]:
        """Atomically claim the oldest eligible queued job of ``kinds``.

        Eligible = ``status='queued'`` with ``run_after`` unset or due, OR
        a ``'running'`` job whose lease has expired and which hasn't yet
        exhausted its attempts (crash-recovery reclaim — including a
        same-worker reclaim; see the module docstring's same-worker
        double-execution note). Ordered by ``priority DESC, created_at
        ASC``. Race-safe under concurrent claimers via ``FOR UPDATE SKIP
        LOCKED`` — see the module docstring for why a plain
        SELECT-then-UPDATE would double-claim.

        Mints and stores a fresh ``lease_token`` (uuid4 hex) on every
        claim — including a reclaim of a job this same ``worker_id``
        already held — so ``heartbeat()``/``complete()``/``fail()`` can
        tell this claim apart from any other, even one under the
        identical ``worker_id``.
        """
        if not kinds:
            return None
        now = datetime.now(timezone.utc)
        lease_token = uuid.uuid4().hex
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        stmt = sa.text(
            """WITH candidate AS MATERIALIZED (
                   SELECT id FROM jobs
                   WHERE kind IN :kinds
                     AND (
                       (status = 'queued' AND (run_after IS NULL OR run_after <= :now))
                       OR (status = 'running' AND lease_expires_at < :now AND attempts < max_attempts)
                     )
                   ORDER BY priority DESC, created_at ASC
                   LIMIT 1
                   FOR UPDATE SKIP LOCKED
               )
               UPDATE jobs
               SET status = 'running',
                   leased_by = :worker_id,
                   lease_token = :lease_token,
                   started_at = COALESCE(started_at, :now),
                   attempts = attempts + 1,
                   lease_expires_at = :lease_expires_at
               FROM candidate
               WHERE jobs.id = candidate.id
               RETURNING jobs.*"""
        ).bindparams(sa.bindparam("kinds", expanding=True))
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    stmt,
                    {
                        "kinds": list(kinds),
                        "now": now,
                        "worker_id": worker_id,
                        "lease_token": lease_token,
                        "lease_expires_at": lease_expires_at,
                    },
                )
                .mappings()
                .first()
            )
        return self._decode(dict(row)) if row else None

    def heartbeat(self, job_id: str, worker_id: str, lease_token: str, lease_seconds: int = 120) -> bool:
        """Extend the lease on a running job. Returns ``False`` (no-op) if
        the job is no longer ``'running'`` or ``lease_token`` no longer
        matches the row's current one (reclaimed — possibly by another
        slot of this SAME ``worker_id``; see the module docstring) — the
        caller uses that to abandon a job reclaimed out from under it.
        ``worker_id`` is accepted for audit/logging only; it is not part
        of the atomicity guard."""
        now = datetime.now(timezone.utc)
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    sa.text(
                        """UPDATE jobs SET lease_expires_at = :lease_expires_at
                           WHERE id = :id AND lease_token = :lease_token AND status = 'running'
                           RETURNING id"""
                    ),
                    {"lease_expires_at": lease_expires_at, "id": job_id, "lease_token": lease_token},
                )
                .mappings()
                .first()
            )
        return row is not None

    def complete(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        result: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Mark a running job done. No-op (raise-free) if the job is not
        currently ``'running'`` under this exact ``lease_token`` — a
        stale slot finishing a job that was already reclaimed (even by
        another slot of the SAME ``worker_id``) must not clobber the new
        owner's state. ``worker_id`` is accepted for audit/logging only.

        ``result``: see ``JobsRepository.complete``'s docstring — merged
        into ``payload_json["result"]`` in the same transaction as the
        ``'done'`` transition, both guarded by the SELECT/UPDATE running
        under one ``self._engine.begin()`` block.

        Returns ``True`` if the row was actually mutated to ``'done'``,
        ``False`` for the stale-lease no-op above — see
        ``JobsRepository.complete`` for why callers gate the webhook
        notify on this.
        """
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            if result is not None:
                row = conn.execute(sa.text("SELECT payload_json FROM jobs WHERE id = :id"), {"id": job_id}).first()
                payload: Dict[str, Any] = {}
                if row and row[0]:
                    raw = row[0]
                    payload = raw if isinstance(raw, dict) else json.loads(raw)
                payload["result"] = result
                mutated = conn.execute(
                    sa.text(
                        """UPDATE jobs
                           SET status = 'done', finished_at = :now, lease_expires_at = NULL,
                               payload_json = :payload_json
                           WHERE id = :id AND lease_token = :lease_token AND status = 'running'
                           RETURNING id"""
                    ),
                    {
                        "now": now,
                        "id": job_id,
                        "lease_token": lease_token,
                        "payload_json": json.dumps(payload),
                    },
                ).first()
            else:
                mutated = conn.execute(
                    sa.text(
                        """UPDATE jobs
                           SET status = 'done', finished_at = :now, lease_expires_at = NULL
                           WHERE id = :id AND lease_token = :lease_token AND status = 'running'
                           RETURNING id"""
                    ),
                    {"now": now, "id": job_id, "lease_token": lease_token},
                ).first()
        return mutated is not None

    def fail(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        error: str,
        *,
        retry_in_seconds: Optional[int] = None,
    ) -> bool:
        """Record a failure. No-op (raise-free) if the job is not
        currently ``'running'`` under this exact ``lease_token`` (see
        ``complete()``). ``worker_id`` is accepted for audit/logging only.

        If attempts remain (``attempts < max_attempts``) AND
        ``retry_in_seconds`` is given, requeues the job (``'queued'``,
        ``run_after=now+retry_in_seconds``, lease + lease_token cleared,
        ``error`` recorded). Otherwise finalizes to ``'failed'`` with
        ``finished_at`` set.

        The SELECT here (no ``FOR UPDATE``) is safe without a lock: the
        decision (retry vs. finalize) only depends on ``attempts`` /
        ``max_attempts``, which cannot change while this claim still
        holds the lease, and the follow-up UPDATE re-checks
        ``lease_token = :lease_token AND status = 'running'`` anyway — if
        a concurrent reclaim slipped in between, that UPDATE simply
        matches nothing.

        Returns ``True`` only when this call FINALIZED the job to
        ``'failed'`` — ``False`` for the stale-lease no-op or a
        successful requeue. See ``JobsRepository.fail`` for why callers
        gate the terminal webhook notify on this return value rather than
        the ``kind``'s static retry configuration.
        """
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            job = (
                conn.execute(
                    sa.text(
                        """SELECT attempts, max_attempts FROM jobs
                           WHERE id = :id AND lease_token = :lease_token AND status = 'running'"""
                    ),
                    {"id": job_id, "lease_token": lease_token},
                )
                .mappings()
                .first()
            )
            if job is None:
                return False  # stale claim (already reclaimed) — no-op
            if job["attempts"] < job["max_attempts"] and retry_in_seconds is not None:
                run_after = now + timedelta(seconds=retry_in_seconds)
                conn.execute(
                    sa.text(
                        """UPDATE jobs
                           SET status = 'queued', run_after = :run_after, lease_expires_at = NULL,
                               leased_by = NULL, lease_token = NULL, error = :error
                           WHERE id = :id AND lease_token = :lease_token AND status = 'running'"""
                    ),
                    {"run_after": run_after, "error": error, "id": job_id, "lease_token": lease_token},
                )
                return False  # requeued, not terminal
            mutated = conn.execute(
                sa.text(
                    """UPDATE jobs
                       SET status = 'failed', finished_at = :now, lease_expires_at = NULL, error = :error
                       WHERE id = :id AND lease_token = :lease_token AND status = 'running'
                       RETURNING id"""
                ),
                {"now": now, "error": error, "id": job_id, "lease_token": lease_token},
            ).first()
        return mutated is not None

    def reap_exhausted(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Finalize stuck ``'running'`` jobs whose lease has expired AND
        which have already exhausted their attempts. Mirrors
        ``JobsRepository.reap_exhausted`` — see that module's docstring for
        the full rationale (``claim_next()``'s reclaim path requires
        ``attempts < max_attempts``, so an exhausted job's expired lease is
        otherwise never converged). No ``FOR UPDATE`` needed: the WHERE
        clause's own predicates (``status='running' AND lease_expires_at <
        :now AND attempts >= max_attempts``) are exactly the terminal
        condition being applied, so a concurrent claim_next()/heartbeat()
        racing this UPDATE under READ COMMITTED just changes how many rows
        match, not whether the match is correct.

        Returns the list of reaped job rows (id, kind, payload_json, ...)
        rather than just a count — see ``JobsRepository.reap_exhausted``
        for why (the worker's reap sweep needs the row to fire the
        terminal webhook notify a live ``fail()`` call would have).
        """
        now = now or datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        """UPDATE jobs
                           SET status = 'failed',
                               finished_at = :now,
                               lease_expires_at = NULL,
                               error = 'lease expired after max attempts'
                           WHERE status = 'running'
                             AND lease_expires_at < :now
                             AND attempts >= max_attempts
                           RETURNING *"""
                    ),
                    {"now": now},
                )
                .mappings()
                .all()
            )
        return [self._decode(dict(r)) for r in rows]
