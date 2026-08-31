"""Model-aware LLM pricing — the one place that turns token counts into USD.

Before this module every USD figure in the codebase came from two constants
hardcoded to one model's rates (``app.chat.manager``'s ``_PRICE_IN_PER_MTOK``
/ ``_PRICE_OUT_PER_MTOK``, labelled "Sonnet pricing"), which had two
consequences:

- **Wrong model.** The constants carried Sonnet 4.6's $3/$15; an instance
  pinned to ``claude-sonnet-5`` ($2/$10) had its daily spend cap
  over-estimate spend by 50%, and one on an Opus model under-estimated it
  by 3x.
- **Cache-blind.** Prompt caching is the single largest lever on the cost of
  a long agent session — a cached read is ~0.1x the input rate and a cache
  write ~1.25x — and neither had a price here at all. Any cost figure that
  charges a re-read of a cached prefix at the full input rate overstates the
  cost of exactly the workload the agent surfaces are built for (a big
  stable prefix + many short turns), which is how a semantic-layer session
  that loads its definitions once can be made to look expensive on paper.

``cache_write_multiplier`` is the 5-minute-TTL rate; a 1h-TTL write is
2x input, not 1.25x. Agnes never sets ``ttl``, so 1.25x is the correct
multiplier for every call it makes today — revisit this if an explicit
``cache_control.ttl`` ever appears in the codebase.

Rates are per million tokens, Anthropic first-party API (which also covers
Microsoft Foundry). Bedrock and Vertex are partner-operated with their own
price lists, so a Vertex-routed instance's absolute USD figures are an
approximation — the relative shape (cached vs uncached) still holds. Such an
instance (or one running a model newer than its Agnes release) can state its
own rates in the ``pricing:`` block of instance.yaml; see
:func:`price_for_model` for the lookup order and
``config/instance.yaml.example`` for the operator-facing documentation.

Nothing here is persisted. Cost is computed from stored token counts at
read time, on purpose: prices change, and a stored cost silently becomes a
number nobody can reproduce.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: Prompt-cache multipliers, relative to a model's own input rate. Uniform
#: across the model family, which is why they are defaults on ModelPrice
#: rather than per-entry columns.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.1


@dataclass(frozen=True)
class ModelPrice:
    """Per-million-token rates for one model."""

    input_per_mtok: float
    output_per_mtok: float
    cache_write_multiplier: float = CACHE_WRITE_MULTIPLIER
    cache_read_multiplier: float = CACHE_READ_MULTIPLIER
    #: Absolute USD/MTok cache rates, used INSTEAD of the multipliers above
    #: when set. Only operator config sets these: an instance.yaml
    #: `pricing:` entry states cache rates absolutely (that is how a
    #: provider's price list reads), while the in-code table derives them
    #: from each model's own input rate.
    cache_write_override: float | None = None
    cache_read_override: float | None = None

    @property
    def cache_write_per_mtok(self) -> float:
        if self.cache_write_override is not None:
            return self.cache_write_override
        return self.input_per_mtok * self.cache_write_multiplier

    @property
    def cache_read_per_mtok(self) -> float:
        if self.cache_read_override is not None:
            return self.cache_read_override
        return self.input_per_mtok * self.cache_read_multiplier


#: Canonical model id -> rates. Keys are the exact API model strings; a
#: dated or suffixed variant resolves by longest-prefix match in
#: ``resolve_price``, so ``claude-sonnet-5-20260101`` prices as
#: ``claude-sonnet-5`` instead of silently falling back to the default.
PRICES: dict[str, ModelPrice] = {
    "claude-fable-5": ModelPrice(10.0, 50.0),
    "claude-mythos-5": ModelPrice(10.0, 50.0),
    "claude-opus-5": ModelPrice(5.0, 25.0),
    "claude-opus-4-8": ModelPrice(5.0, 25.0),
    "claude-opus-4-7": ModelPrice(5.0, 25.0),
    "claude-opus-4-6": ModelPrice(5.0, 25.0),
    # $2/$10 was the introductory rate, through 2026-08-31 only; standard
    # pricing (matching Sonnet 4.6) applies from 2026-09-01.
    "claude-sonnet-5": ModelPrice(3.0, 15.0),
    "claude-sonnet-4-6": ModelPrice(3.0, 15.0),
    "claude-haiku-4-5": ModelPrice(1.0, 5.0),
}

#: Used when the model is unknown or unrecorded (a message row written
#: before the model column existed, a Vertex alias we do not carry). The
#: most expensive GENERAL-PURPOSE tier, deliberately: a spend cap that
#: guesses must guess in the direction that stops sooner, never in the
#: direction that lets an unrecognized model spend unbounded. (The
#: speciality Fable/Mythos tier prices above it and is only ever reached by
#: naming it, so it is not the safe default for an unknown string.)
DEFAULT_PRICE = PRICES["claude-opus-5"]


def _pricing_config() -> dict[str, Any]:
    """The instance's ``pricing:`` block, or ``{}`` when it has none.

    Read through the shared loader on every call rather than cached here:
    the loader already caches, and an /admin/server-config edit has to reach
    the next cost computation without a restart. Never raises — a config
    file mid-rewrite must not take the chat spend guardrail down with it.
    """
    try:
        from app.instance_config import get_value

        cfg = get_value("pricing", default=None)
    except Exception:  # pragma: no cover - defensive, config layer unavailable
        logger.debug("pricing config unreadable; using in-code prices", exc_info=True)
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _table_price(key: str) -> ModelPrice | None:
    """In-code rates for a normalized model id — exact, else longest known
    prefix, else ``None``.

    Prefix matching is what keeps a dated snapshot id
    (``claude-opus-4-5@20251101``, ``claude-sonnet-5-20260101``) priced as
    its family rather than as the default, and it is longest-first so
    ``claude-opus-4-8`` cannot be captured by a shorter ``claude-opus-4``
    style key if one is ever added.
    """
    if not key:
        return None
    exact = PRICES.get(key)
    if exact is not None:
        return exact
    # Vertex spells a dated snapshot `model@date`; strip the platform
    # prefix Bedrock adds (`anthropic.claude-opus-5`) before matching.
    stripped = key.removeprefix("anthropic.")
    for known in sorted(PRICES, key=len, reverse=True):
        if stripped.startswith(known):
            return PRICES[known]
    return None


def _configured_entry(models: Any, key: str) -> Any:
    """The operator's entry for ``key`` — exact, else longest matching
    prefix, mirroring :func:`_table_price` so a configured family covers its
    dated variants exactly like an in-code one does."""
    if not isinstance(models, dict) or not key:
        return None
    normalized = {str(k).strip().lower(): v for k, v in models.items()}
    if key in normalized:
        return normalized[key]
    stripped = key.removeprefix("anthropic.")
    for known in sorted(normalized, key=len, reverse=True):
        if known and stripped.startswith(known):
            return normalized[known]
    return None


def _price_from_entry(entry: Any, base: ModelPrice | None) -> ModelPrice | None:
    """Build rates from one operator entry, or ``None`` if it is unusable.

    An entry states USD per million tokens and overrides only the keys it
    names: correcting an output rate must not silently zero the input one.
    Unstated cache rates keep the standard multipliers applied to the
    EFFECTIVE input rate, so ``{input: 10}`` alone still prices a cached
    read at a tenth of the rate the operator just set.
    """
    if not isinstance(entry, dict):
        if entry is not None:
            logger.warning("pricing config entry is not a mapping; ignoring it")
        return None
    fallback = base if base is not None else ModelPrice(0.0, 0.0)
    try:
        input_per_mtok = float(entry["input"]) if "input" in entry else fallback.input_per_mtok
        output_per_mtok = float(entry["output"]) if "output" in entry else fallback.output_per_mtok
        cache_read = float(entry["cache_read"]) if "cache_read" in entry else None
        cache_write = float(entry["cache_write"]) if "cache_write" in entry else None
    except (TypeError, ValueError):
        logger.warning("pricing config entry has non-numeric rates; ignoring it: %r", entry)
        return None
    return ModelPrice(
        input_per_mtok,
        output_per_mtok,
        cache_read_override=cache_read,
        cache_write_override=cache_write,
    )


def price_for_model(model: str | None) -> ModelPrice:
    """Rates for ``model``.

    Lookup order — operator config first, so an instance running a model
    Agnes has no rate for (or on a partner-operated price list) can state
    its own numbers without a release:

    1. exact ``pricing.models`` entry
    2. longest-prefix ``pricing.models`` entry
    3. exact in-code :data:`PRICES` entry
    4. longest-prefix in-code entry
    5. ``pricing.default``
    6. :data:`DEFAULT_PRICE`

    Step 6 is deliberately the conservative in-code tier rather than a
    zero-cost price: these rates back the chat daily spend cap, which prices
    an unattributed day at ``model=None``. "Free" is not a safe reading of
    "unknown" — an operator who wants it says so in ``pricing.default``.
    """
    key = (model or "").strip().lower()
    cfg = _pricing_config()
    if not isinstance(cfg, dict):
        cfg = {}
    base = _table_price(key)
    configured = _price_from_entry(_configured_entry(cfg.get("models"), key), base)
    if configured is not None:
        return configured
    if base is not None:
        return base
    default = _price_from_entry(cfg.get("default"), None)
    if default is not None:
        return default
    return DEFAULT_PRICE


#: Historical name, kept because existing callers (``app/api/admin_usage.py``)
#: import it. It is the same function, never a second config-blind lookup.
resolve_price = price_for_model


def cost_usd(
    model: str | None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    """USD cost of one call (or a summed set of calls) on ``model``.

    ``input_tokens`` is the UNCACHED input, matching the Anthropic usage
    field of the same name — cached tokens are reported separately and
    priced separately here, never double-counted.
    """
    p = price_for_model(model)
    return (
        input_tokens * p.input_per_mtok
        + output_tokens * p.output_per_mtok
        + cache_read_tokens * p.cache_read_per_mtok
        + cache_creation_tokens * p.cache_write_per_mtok
    ) / 1_000_000


def budget_tokens(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> int:
    """The token total budget/quota accounting charges for.

    ``input + output + cache_creation``, EXCLUDING ``cache_read_tokens`` —
    deliberately the same definition
    ``src.repositories.llm_usage.LlmUsageRepository.usage_breakdown_for_month``
    already uses for agent ``token_budget_monthly``, so the two budget
    surfaces cannot disagree about what a token is. Cached reads are
    heavily discounted and stay informational on both.
    """
    return int(input_tokens) + int(output_tokens) + int(cache_creation_tokens)
