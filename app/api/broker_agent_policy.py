"""Pure-logic helpers backing the secret broker's per-agent LLM policy
(Task 8 of the agent-profiles / agent-as-API V1a plan,
``docs/superpowers/specs/2026-07-21-agent-profiles-and-agent-api-design.md``
§3).

Kept free of FastAPI/HTTP concerns on purpose — ``app/api/broker.py``'s
``anthropic_proxy`` is the ONE chokepoint that wires these in (before the
upstream credential fork, so the check applies to every upstream mode:
static key, workload identity, and the LLM dispatcher), but the functions
here are unit-testable without any of that machinery.

Three responsibilities:

- ``check_model`` — reject a brokered chat-completion request whose
  ``model`` field is outside the agent's allowed set, BEFORE any token is
  spent (403 ``model_not_allowed``).
- ``parse_usage`` / ``UsageAccumulator`` — extract per-call token usage
  from the (buffered) upstream response and batch it into the
  ``llm_usage`` ledger. The hot broker path must never perform one
  synchronous DB write per LLM call — rows are buffered in memory and
  flushed in bulk on a size/age threshold.
- ``check_budget`` / ``cached_month_total`` — enforce
  ``agents.token_budget_monthly`` (429 ``budget_exhausted``, no
  Retry-After — SDKs must not auto-retry a budget exhaustion), with the
  month-to-date total cached in the coordination backend so the hot path
  doesn't hit the ledger table on every call.

Adaptation note: the wiring sketch in the task brief assumed a
``cfg.model_default`` config knob already existed on ``ChatConfig``. It
doesn't — there is no instance-wide "default model" setting anywhere in
this codebase; the Claude Code CLI inside the sandbox picks its own
default unless a session profile overrides it. Design decision
(controller-approved): an agent whose ``model`` column is ``NULL`` has NO
model policy at all — ``check_model`` returns ``None`` (allow) without
even inspecting the request body. Enforcement only kicks in once the
owner has pinned a model on the agent, in which case the allowed set is
``{agent.model} ∪ utility_models``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from app.coordination.base import CoordinationUnavailable
from app.coordination.factory import coordination
from src.repositories import llm_usage_repo

logger = logging.getLogger(__name__)

# Anthropic Messages API usage field names, mapped onto the llm_usage
# ledger's column names.
_USAGE_FIELD_MAP = (
    ("input_tokens", "input_tokens"),
    ("output_tokens", "output_tokens"),
    ("cache_read_input_tokens", "cache_read_tokens"),
    ("cache_creation_input_tokens", "cache_creation_tokens"),
)

DEFAULT_FLUSH_SIZE = 20
DEFAULT_FLUSH_INTERVAL_S = 30.0


# ---------------------------------------------------------------------------
# Model policy
# ---------------------------------------------------------------------------


def canonical_model_id(model: str) -> str:
    """Canonical form for model-id comparison across platform spellings.

    Vertex AI spells dated snapshots with an ``@`` separator
    (``claude-sonnet-4-5@20250929``) where the first-party API uses a dash
    (``claude-sonnet-4-5-20250929``). Operators may write either form in
    ``agents.model`` / ``chat.agent_api_utility_models``; both compare
    equal here. Undated ids pass through unchanged.
    """
    m = (model or "").strip()
    return m.replace("@", "-") if "@" in m else m


def check_model_value(
    model: str | None,
    agent_row: dict[str, Any],
    utility_models: list[str] | None,
) -> str | None:
    """``"model_not_allowed"`` if ``model`` is outside the agent's allowed
    set, else ``None``.

    An agent with ``model IS NULL`` has NO model policy — the owner never
    pinned one, so every model is allowed. Once a model is pinned, allowed
    set = ``{agent_row["model"]} ∪ utility_models``, compared in canonical
    space (:func:`canonical_model_id`) so first-party and Vertex spellings
    of the same snapshot interchange freely.

    A falsy ``model`` is NOT a policy failure — the upstream API rejects
    the malformed request on its own terms.
    """
    pinned_model = agent_row.get("model")
    if not pinned_model:
        return None
    if not model:
        return None
    allowed = {canonical_model_id(pinned_model)}
    allowed.update(canonical_model_id(u) for u in utility_models or [])
    if canonical_model_id(model) not in allowed:
        return "model_not_allowed"
    return None


def check_model(
    body_bytes: bytes,
    agent_row: dict[str, Any],
    utility_models: list[str] | None,
) -> str | None:
    """Body-reading wrapper over :func:`check_model_value`.

    A non-JSON body, a non-object JSON body, or a body with no ``model``
    key is NOT a policy failure — it returns ``None`` and lets the
    upstream API reject the malformed request on its own terms. (Vertex
    native paths carry the model in the URL instead; the broker calls
    :func:`check_model_value` directly there.)
    """
    if not agent_row.get("model"):
        return None
    try:
        body = json.loads(body_bytes) if body_bytes else None
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    if not isinstance(body, dict):
        return None
    model = body.get("model")
    if not model:
        return None
    if not isinstance(model, str):
        # A truthy non-string model was never in the allowed set before the
        # canonicalization refactor either — keep denying it.
        return "model_not_allowed"
    return check_model_value(model, agent_row, utility_models)


# ---------------------------------------------------------------------------
# Usage parsing
# ---------------------------------------------------------------------------


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _normalize_usage(usage: Dict[str, Any], model: Optional[str]) -> Dict[str, Any]:
    row: Dict[str, Any] = {"model": model}
    for src_key, dst_key in _USAGE_FIELD_MAP:
        row[dst_key] = _to_int(usage.get(src_key, 0))
    return row


def _parse_json_usage(resp_body: bytes) -> Optional[Dict[str, Any]]:
    body = json.loads(resp_body)
    if not isinstance(body, dict):
        return None
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return None
    return _normalize_usage(usage, body.get("model"))


def _parse_sse_usage(resp_body: bytes) -> Optional[Dict[str, Any]]:
    """Scan a buffered ``text/event-stream`` body for ``message_start`` /
    ``message_delta`` events and sum their ``usage`` fields.

    ``message_start`` carries the request's true ``input_tokens`` and
    cache-token totals (and an initial, usually-zero ``output_tokens``).
    Each subsequent ``message_delta`` carries the incremental output
    tokens generated since the previous event. Summing field-by-field
    across every scanned event therefore recovers the response's total
    usage. If a future upstream instead reports a cumulative running
    total per ``message_delta`` (multi-delta responses, e.g. under
    extended thinking), this sums to a conservative OVER-count rather
    than an under-count — acceptable for a soft monthly budget guardrail
    (never silently opens the budget wider than it should be), the same
    "not a billing ledger" posture the daily chat token guardrail takes
    (``ChatManager._daily_token_totals``).
    """
    text = resp_body.decode("utf-8", errors="replace")
    model: Optional[str] = None
    totals = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    seen = False
    event_type: Optional[str] = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            event_type = None
            continue
        if line.startswith("event:"):
            event_type = line[len("event:") :].strip()
            continue
        if not line.startswith("data:") or event_type not in ("message_start", "message_delta"):
            continue
        payload = line[len("data:") :].strip()
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if event_type == "message_start":
            message = data.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
            if isinstance(message, dict) and model is None:
                model = message.get("model")
        else:
            usage = data.get("usage")
        if isinstance(usage, dict):
            seen = True
            for key in totals:
                totals[key] += _to_int(usage.get(key, 0))
    if not seen:
        return None
    return {
        "model": model,
        "input_tokens": totals["input_tokens"],
        "output_tokens": totals["output_tokens"],
        "cache_read_tokens": totals["cache_read_input_tokens"],
        "cache_creation_tokens": totals["cache_creation_input_tokens"],
    }


def parse_usage(resp_body: bytes, content_type: str) -> Optional[Dict[str, Any]]:
    """Extract ``{model, input_tokens, output_tokens, cache_read_tokens,
    cache_creation_tokens}`` from a buffered upstream response body, or
    ``None`` if it can't be parsed / carries no usage. Never raises —
    a broken response body must not break the response path.
    """
    try:
        if "text/event-stream" in (content_type or "").lower():
            return _parse_sse_usage(resp_body)
        return _parse_json_usage(resp_body)
    except Exception:
        logger.debug("parse_usage: failed to parse upstream response body", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def check_budget(agent_row: Dict[str, Any], month_total: int) -> Optional[str]:
    """``"budget_exhausted"`` when the agent has a monthly token budget and
    ``month_total >= token_budget_monthly``, else ``None`` (including when
    no budget is configured at all)."""
    budget = agent_row.get("token_budget_monthly")
    if budget is None:
        return None
    if month_total >= budget:
        return "budget_exhausted"
    return None


def budget_cache_key(agent_id: str, year_month: str) -> str:
    return f"agent-budget:{agent_id}:{year_month}"


def _current_year_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def cached_month_total(agent_id: str, ttl_s: int = 60) -> int:
    """Monthly token total for ``agent_id``, cached in the coordination
    backend for ``ttl_s`` seconds.

    Cache miss (or coordination outage) falls back to a direct
    ``llm_usage_repo().month_total_tokens`` read — never raises. A
    coordination outage just means every call reads the ledger directly
    until the backend recovers (fail-open on the cache only; the DB read
    itself is the source of truth).
    """
    year_month = _current_year_month()
    key = budget_cache_key(agent_id, year_month)
    cached: Optional[str] = None
    try:
        cached = coordination().kv_get(key)
    except CoordinationUnavailable:
        logger.warning("budget cache: coordination backend unavailable; reading ledger directly for agent %s", agent_id)
    if cached is not None:
        try:
            return int(cached)
        except ValueError:
            pass
    total = llm_usage_repo().month_total_tokens(agent_id, year_month)
    try:
        coordination().kv_set(key, str(total), ttl_s=ttl_s)
    except CoordinationUnavailable:
        logger.warning("budget cache: coordination backend unavailable; skipping cache write for agent %s", agent_id)
    return total


# ---------------------------------------------------------------------------
# Usage accumulator — batched ledger writes
# ---------------------------------------------------------------------------


class UsageAccumulator:
    """Batches ``llm_usage`` rows in memory and flushes them to
    ``llm_usage_repo().insert_batch`` in bulk.

    The broker's hot path (every brokered LLM call) must never perform a
    synchronous single-row DB write — flush happens when the buffer
    reaches ``flush_size`` rows OR ``flush_interval_s`` seconds have
    elapsed since the last flush, whichever comes first. Thread-safe: the
    ``anthropic_proxy`` async endpoint itself runs single-threaded on the
    event loop, but the lock guards this singleton against any future or
    multi-worker caller that adds/flushes concurrently (e.g. a
    multi-process deployment, or a background flush task sharing the same
    instance) — a call from the request path alone would never race.
    """

    def __init__(
        self,
        flush_size: int = DEFAULT_FLUSH_SIZE,
        flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._flush_size = flush_size
        self._flush_interval_s = flush_interval_s
        self._clock = clock
        self._lock = threading.Lock()
        self._rows: List[Dict[str, Any]] = []
        self._last_flush = clock()

    def add(self, row: Dict[str, Any], *, budget_ttl_s: int = 60) -> None:
        """Buffer one usage row and best-effort bump the agent's cached
        monthly counter, then flush if a threshold is due."""
        with self._lock:
            self._rows.append(row)
        self._incr_budget_counter(row, budget_ttl_s)
        self.maybe_flush()

    @staticmethod
    def _incr_budget_counter(row: Dict[str, Any], ttl_s: int) -> None:
        agent_id = row.get("agent_id")
        if not agent_id:
            return
        delta = (
            _to_int(row.get("input_tokens"))
            + _to_int(row.get("output_tokens"))
            + _to_int(row.get("cache_creation_tokens"))
        )
        if delta <= 0:
            return
        key = budget_cache_key(agent_id, _current_year_month())
        try:
            coordination().incr(key, amount=delta, ttl_s=ttl_s)
        except CoordinationUnavailable:
            logger.warning(
                "llm usage budget counter: coordination backend unavailable; not incremented for agent %s",
                agent_id,
            )

    def maybe_flush(self) -> None:
        with self._lock:
            due_by_size = len(self._rows) >= self._flush_size
            due_by_age = bool(self._rows) and (self._clock() - self._last_flush) >= self._flush_interval_s
        if due_by_size or due_by_age:
            self.flush()

    def flush(self) -> None:
        """Force a flush of whatever is currently buffered. Safe to call
        with an empty buffer (no-op). Callable from other modules — e.g.
        Task 9 flushes before reading usage totals for a just-completed
        one-shot, and app shutdown flushes so a short-lived process
        doesn't drop its tail of buffered rows."""
        with self._lock:
            rows, self._rows = self._rows, []
            self._last_flush = self._clock()
        if not rows:
            return
        try:
            llm_usage_repo().insert_batch(rows)
        except Exception:
            # Usage metering is a best-effort budget guardrail, not a
            # billing ledger of record (same posture as the daily chat
            # token counters) — a write failure must not break the
            # broker's response path, which has already completed by the
            # time this runs.
            logger.exception("llm_usage batch flush failed; %d usage rows dropped", len(rows))


usage_accumulator = UsageAccumulator()
