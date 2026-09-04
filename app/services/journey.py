"""Onboarding-journey milestones, marked from the actions that earn them.

The onboarding checklist (``user_journey_state``, surfaced by the rail's "Set up
Agnes" card) used to be written from exactly one place: the browser, when the
reader clicked a row in the checklist itself. That made the card a manual to-do
list rather than a record of progress — someone who followed the coach-mark to
the Library, clicked **Add** on a data package and watched the toast confirm it
came back to a checklist that still said *Put knowledge in your stack*. Guiding
someone to do a thing and then not noticing they did it is worse than not
guiding them at all.

So the milestone is recorded where the real work happens — the endpoint that
carries out the action — which also makes it surface-agnostic: putting something
in your stack from the CLI, from chat, from MCP or from the Library page all
count the same, because they all end up in the same handler.

Two rules for every call site:

* **Only ever set a flag to True.** These are "this happened at least once"
  milestones; nothing here may un-tick a step. (The checklist's own "Start over"
  is the single writer allowed to clear them, via PUT /api/chat/journey.)
* **Never let bookkeeping break the action.** A failure here means an onboarding
  card is one tick behind, which is worth nothing next to failing the subscribe
  the user actually asked for — so everything is swallowed. The step is also
  still reachable by hand from the checklist.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def mark_journey(user_id: str | None, **flags: bool) -> None:
    """Best-effort: record onboarding milestones for ``user_id``.

    No-op when there is no user (service tokens, unauthenticated paths), when no
    flag is passed, or when every flag is already set — the read-before-write
    keeps a hot path like "add to stack" from issuing an upsert per call for a
    user who passed this milestone months ago.
    """
    if not user_id or not flags:
        return
    try:
        from src.repositories import user_journey_repo

        repo = user_journey_repo()
        current = repo.get(user_id)
        # True-only, and only what actually changes.
        pending = {k: True for k, v in flags.items() if v and not current.get(k)}
        if not pending:
            return
        repo.update(user_id, **pending)
    except Exception:  # pragma: no cover - defensive; see module docstring
        log.debug("journey: could not mark %s for %s", sorted(flags), user_id, exc_info=True)


#: The checklist's steps, in the order the rail's onboarding card lists them —
#: a mirror of ``STEP_KEYS`` in ``app/web/static/js/chat_onboarding.js``, and
#: pinned equal to it by ``tests/test_rail_onboarding_first_paint.py``. Six of
#: the journey's seven booleans: ``onboarded`` records that the greeting was
#: shown, and is not a step anyone completes.
JOURNEY_STEP_KEYS: tuple[str, ...] = (
    "first_asked",
    "explored_stack",
    "stack_setup_done",
    "catalog_discovered",
    "use_anywhere",
    "agent_created",
)


def resolve_journey_rail(user_id: str | None) -> dict[str, Any] | None:
    """The caller's checklist progress as the rail's onboarding card needs it.

    ``{"done", "total", "complete"}`` — the same three numbers
    ``updateGetStartedIndicator`` in chat_onboarding.js derives from
    ``GET /api/chat/journey`` and writes into the card once that fetch
    resolves. The server renders them FIRST, so the card's first paint is
    already its resolved state: retired at 6/6, otherwise the real title,
    count and arc. Left to the script alone, a caller who had finished
    onboarding saw the card for one paint on every page load, and since it
    sits in the rail foot — whose height positions every row above it —
    Library · Agents · Admin jumped up and dropped back on every navigation
    (62px, measured); a caller mid-way got the same thing 6px tall when the
    empty count line filled in and grew the row.

    None when there is no user or the read fails: the card then renders
    blank and the script resolves it — "we could not check", never a wrong
    number, and never a card retired on a guess. Same fallback direction as
    ``resolve_setup_rail`` for the admin chain beside it.
    """
    if not user_id:
        return None
    try:
        from src.repositories import user_journey_repo

        state = user_journey_repo().get(user_id)
    except Exception:
        log.warning("journey: could not read state for the rail card", exc_info=True)
        return None
    done = sum(1 for key in JOURNEY_STEP_KEYS if state.get(key))
    total = len(JOURNEY_STEP_KEYS)
    return {"done": done, "total": total, "complete": done == total}
