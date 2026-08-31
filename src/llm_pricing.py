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
approximation — the relative shape (cached vs uncached) still holds.
"""

from __future__ import annotations

from dataclasses import dataclass

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

    @property
    def cache_write_per_mtok(self) -> float:
        return self.input_per_mtok * self.cache_write_multiplier

    @property
    def cache_read_per_mtok(self) -> float:
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


def resolve_price(model: str | None) -> ModelPrice:
    """Rates for ``model`` — exact match, else longest known prefix, else
    :data:`DEFAULT_PRICE`.

    Prefix matching is what keeps a dated snapshot id
    (``claude-opus-4-5@20251101``, ``claude-sonnet-5-20260101``) priced as
    its family rather than as the default, and it is longest-first so
    ``claude-opus-4-8`` cannot be captured by a shorter ``claude-opus-4``
    style key if one is ever added.
    """
    if not model:
        return DEFAULT_PRICE
    key = model.strip().lower()
    exact = PRICES.get(key)
    if exact is not None:
        return exact
    # Vertex spells a dated snapshot `model@date`; strip the platform
    # prefix Bedrock adds (`anthropic.claude-opus-5`) before matching.
    key = key.removeprefix("anthropic.")
    for known in sorted(PRICES, key=len, reverse=True):
        if key.startswith(known):
            return PRICES[known]
    return DEFAULT_PRICE


def cost_usd(
    *,
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
    p = resolve_price(model)
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
