"""Job-kind registry for the worker runtime (wave-2B, spec §3.3).

Task kinds register themselves here (name -> ``JobKind``) before the
worker loop starts — see ``app/worker/kinds.py`` (a later task in the
same wave) for the five real kinds (``data-refresh``, ``jira-refresh``,
``marketplaces-sync``, ``session-collector``, ``corporate-memory``).
Empty by default; this task's tests register fake kinds only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

#: Lane identifiers. Plain string constants matching
#: ``JobsRepository.HEAVY_LANE`` / ``.LIGHT_LANE`` (`src/repositories/jobs.py`)
#: — duplicated rather than imported so this module (which the two backend
#: repo modules know nothing about) doesn't need to pick one of them to
#: depend on. The CONTRACT is the string value ("heavy"/"light"), not the
#: source of truth for it.
HEAVY_LANE = "heavy"
LIGHT_LANE = "light"
#: A third lane (spec §7.5 / §16 step 7, docs/superpowers/specs/
#: 2026-08-27-fact-graph-over-collections-design.md) for document
#: extraction (the ``corpus-extraction`` kind, ``app/worker/kinds.py``) —
#: kept off the HEAVY lane deliberately: a corpus re-extraction sharing
#: HEAVY's concurrency-1 slot with ``data-refresh``/``jira-refresh`` would
#: block every table sync for its whole duration. Concurrency for this lane
#: (``_EXTRACTION_CONCURRENCY``) lives in ``app/worker/runtime.py`` next to
#: the other two lane-concurrency constants.
EXTRACTION_LANE = "extraction"

_VALID_LANES = (HEAVY_LANE, LIGHT_LANE, EXTRACTION_LANE)


@dataclass
class JobKind:
    """A registered handler for one ``jobs.kind`` value.

    ``handler`` is a plain synchronous callable — the worker loop runs it
    via ``asyncio.to_thread`` so a slow/blocking implementation (DB call,
    HTTP request, subprocess) never stalls the event loop. It receives the
    job's decoded ``payload_json`` dict and returns ``None`` on success, OR
    (Task 9) an optional result ``dict`` that ``app/worker/runtime.py``
    passes straight through to ``jobs_repo().complete(..., result=...)`` —
    see that method's docstring for where it lands (``payload_json
    ["result"]``, since the ``jobs`` table has no dedicated result column).
    Any raised exception is caught by the loop and turned into a
    ``fail(..., retry_in_seconds=kind.retry_in_seconds)`` call.
    """

    name: str
    handler: Callable[[dict], Optional[dict]]
    lane: str
    lease_seconds: int = 120
    retry_in_seconds: int | None = 300


#: Process-wide registry: ``kind name -> JobKind``. Populated by
#: ``register_kind()`` calls made before the worker loop starts (see
#: ``app/main.py`` lifespan ordering).
JOB_KINDS: dict[str, JobKind] = {}

#: ``JobsRepository``/``JobsPgRepository.enqueue()``'s own default
#: ``max_attempts`` — named here so a call site never repeats the literal
#: ``3`` it actually means. See :func:`job_max_attempts`.
DEFAULT_JOB_MAX_ATTEMPTS = 3

#: Per-kind ``max_attempts`` override for job kinds whose ``claim_next()``
#: reclaim path (a lease expiring because the worker that held it is gone —
#: an OOM kill, an image-swap recreate, any other lost worker) must not
#: compete for the SAME small budget every other kind uses to detect a
#: genuinely broken handler.
#:
#: ``corpus-extraction`` and ``sharepoint-facts-extraction`` are both
#: registered with ``retry_in_seconds=None`` (``app/worker/kinds.py``) — a
#: raised exception inside the handler always finalizes to ``'failed'`` on
#: its FIRST attempt (``JobsRepository.fail``'s requeue branch requires
#: ``retry_in_seconds is not None``), never consuming a second one. So for
#: these two kinds specifically, every attempt past the first can ONLY be
#: a crash-recovery reclaim of an expired lease — never a second try at a
#: handler that already raised. Raising their budget therefore only ever
#: widens how many worker restarts a long crawl survives; it cannot mask an
#: actually-broken handler, which still fails on attempt 1 regardless of
#: this value.
#:
#: 2026-09 incident: a live multi-hour SharePoint crawl lost 6 of 7
#: `corpus-extraction` jobs to `"lease expired after max attempts"` after
#: one OOM kill and two planned worker recreates on the same host — three
#: reclaims exhausted the shared default of 3 attempts. 25 is generous
#: headroom for a run spanning many hours and several restarts without
#: being unbounded.
JOB_MAX_ATTEMPTS_BY_KIND: dict[str, int] = {
    "corpus-extraction": 25,
    "sharepoint-facts-extraction": 25,
}


def job_max_attempts(kind: str) -> int:
    """The ``max_attempts`` an ``enqueue()`` call for ``kind`` should pass —
    :data:`JOB_MAX_ATTEMPTS_BY_KIND`'s override when one exists, otherwise
    :data:`DEFAULT_JOB_MAX_ATTEMPTS`. ONE shared helper so every enqueue
    call site reads the same number instead of repeating (and inevitably
    drifting) a per-call literal.
    """
    return JOB_MAX_ATTEMPTS_BY_KIND.get(kind, DEFAULT_JOB_MAX_ATTEMPTS)


def register_kind(kind: JobKind) -> None:
    """Register (or replace) a job kind.

    Raises ``ValueError`` for an unknown lane so a typo'd lane string
    fails loudly at registration time rather than silently never being
    polled by either lane runner.
    """
    if kind.lane not in _VALID_LANES:
        raise ValueError(f"JobKind {kind.name!r}: unknown lane {kind.lane!r} (expected one of {_VALID_LANES})")
    JOB_KINDS[kind.name] = kind
