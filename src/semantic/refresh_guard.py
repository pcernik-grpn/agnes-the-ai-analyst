"""Single-flight guards shared by two writers of the same semantic rows.

The scheduled sweep (``app/api/semantic_sources_refresh.py``) and the Keboola
login-triggered background sync
(``app/api/keboola_semantic_layer_refresh.py``) both write — and prune — the
``(source='keboola_metastore', source_ref=<connection id>)`` rows of every
connected Keboola project. Each used to guard only ITSELF, with its own lock
in its own module, so the two could overlap: one pass upserting while the
other pruned the same scope against a half-written picture.

One guard, claimed by both, makes that overlap impossible. Deliberately:

* **``threading``, not ``asyncio``.** The sweep runs off the event loop
  (``asyncio.to_thread``) while the login path claims from the loop thread
  itself; only a thread-level primitive is visible to both.
* **Non-blocking claim.** Both callers want "skip if busy", never "queue
  behind": a queued second pass is a duplicate run against rows the first one
  just wrote, and the next login or the next scheduler tick catches up anyway.
* **Process-local.** Same scope as the per-module locks it replaces. A
  role-split deployment runs the sweep in one process and logins in another,
  where neither this guard nor its predecessors serialize anything — that is
  a separate (advisory-lock-shaped) problem, not one this change pretends to
  have solved.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator, Optional


class SingleFlight:
    """A named, process-local, non-blocking single-flight slot."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._lock = threading.Lock()
        self._holder: Optional[str] = None

    @property
    def busy(self) -> bool:
        return self._holder is not None

    @property
    def holder(self) -> Optional[str]:
        """Who holds the slot, for the log line the loser writes."""
        return self._holder

    @contextmanager
    def try_claim(self, holder: str) -> Iterator[bool]:
        """Yield True when this caller took the slot, False when someone else
        already holds it. Released on the way out, exception or not — a run
        that blew up must not wedge the guard until the next restart."""
        acquired = self._lock.acquire(blocking=False)
        if acquired:
            self._holder = holder
        try:
            yield acquired
        finally:
            if acquired:
                self._holder = None
                self._lock.release()


#: The Keboola semantic layer's writers: the scheduled sweep's import of a
#: ``keboola_metastore``-adapter source, and the login-triggered sync.
KEBOOLA_SEMANTIC_REFRESH = SingleFlight("keboola_semantic_refresh")
