"""`src/llm_pricing.py` — the one place token counts become USD.

These tests pin the two properties that make a cost comparison trustworthy:
pricing follows the MODEL, and cached tokens are priced as cached rather
than as full input. Both were wrong before this module existed (two
constants hardcoded to one model's rates, no cache rate at all), which is
how a workload built around a large stable prefix can be made to look
expensive on paper.
"""

from src.llm_pricing import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER,
    DEFAULT_PRICE,
    PRICES,
    budget_tokens,
    cost_usd,
    resolve_price,
)


class TestResolvePrice:
    def test_exact_model_id(self):
        assert resolve_price("claude-sonnet-5").input_per_mtok == 3.0
        assert resolve_price("claude-opus-5").input_per_mtok == 5.0

    def test_case_and_whitespace_insensitive(self):
        assert resolve_price("  Claude-Sonnet-5 ") is PRICES["claude-sonnet-5"]

    def test_dated_snapshot_resolves_to_its_family(self):
        """A dated or platform-prefixed id must not silently fall back to the
        default — that would price a cheap model at the expensive tier."""
        assert resolve_price("claude-sonnet-5-20260101") is PRICES["claude-sonnet-5"]
        assert resolve_price("anthropic.claude-opus-5") is PRICES["claude-opus-5"]

    def test_unknown_and_missing_model_fall_back_conservatively(self):
        """A cap that has to guess must guess in the direction that stops
        sooner, never the one that lets an unrecognized model run free."""
        assert resolve_price("mystery-model") is DEFAULT_PRICE
        assert resolve_price(None) is DEFAULT_PRICE
        assert resolve_price("") is DEFAULT_PRICE
        # At least as expensive as every general-purpose tier an instance is
        # realistically pinned to (the speciality Fable/Mythos tier prices
        # above it, and is only ever reached by naming it explicitly).
        mainstream = {k: v for k, v in PRICES.items() if "fable" not in k and "mythos" not in k}
        assert DEFAULT_PRICE.input_per_mtok == max(p.input_per_mtok for p in mainstream.values())
        assert DEFAULT_PRICE.output_per_mtok == max(p.output_per_mtok for p in mainstream.values())


class TestCostUsd:
    def test_uncached_input_and_output(self):
        assert cost_usd(model="claude-sonnet-5", input_tokens=1_000_000) == 3.0
        assert cost_usd(model="claude-sonnet-5", output_tokens=1_000_000) == 15.0

    def test_cached_read_is_a_tenth_of_input_not_full_input(self):
        """The single most consequential line in this module: the term a
        hand-built cost model is most likely to overcharge by ~10x."""
        full = cost_usd(model="claude-sonnet-5", input_tokens=1_000_000)
        cached = cost_usd(model="claude-sonnet-5", cache_read_tokens=1_000_000)
        assert cached == full * CACHE_READ_MULTIPLIER

    def test_cache_write_is_a_premium_over_input(self):
        full = cost_usd(model="claude-opus-5", input_tokens=1_000_000)
        written = cost_usd(model="claude-opus-5", cache_creation_tokens=1_000_000)
        assert written == full * CACHE_WRITE_MULTIPLIER

    def test_terms_are_additive_and_not_double_counted(self):
        """`input_tokens` is UNCACHED input (the Anthropic field semantics),
        so the four terms sum — a cached token must never be billed twice."""
        parts = sum(
            cost_usd(model="claude-sonnet-5", **{k: 1000})
            for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")
        )
        combined = cost_usd(
            model="claude-sonnet-5",
            input_tokens=1000,
            output_tokens=1000,
            cache_read_tokens=1000,
            cache_creation_tokens=1000,
        )
        assert combined == parts

    def test_zero_tokens_costs_nothing(self):
        assert cost_usd(model="claude-opus-5") == 0.0

    def test_a_cheaper_model_costs_less_for_identical_usage(self):
        usage = dict(input_tokens=500_000, output_tokens=100_000, cache_read_tokens=9_000_000)
        assert cost_usd(model="claude-haiku-4-5", **usage) < cost_usd(model="claude-sonnet-5", **usage)
        assert cost_usd(model="claude-sonnet-5", **usage) < cost_usd(model="claude-opus-5", **usage)


class TestBudgetTokens:
    def test_excludes_cache_reads_and_includes_cache_writes(self):
        """Must match `llm_usage.usage_breakdown_for_month`'s definition, or
        the two budget surfaces disagree about what a token is."""
        assert budget_tokens(input_tokens=10, output_tokens=20, cache_creation_tokens=5) == 35
        assert budget_tokens(input_tokens=10) == 10

    def test_has_no_cache_read_parameter_at_all(self):
        """Structural, not stylistic: a caller cannot accidentally charge a
        budget for cached reads because there is nowhere to pass them."""
        import inspect

        assert "cache_read_tokens" not in inspect.signature(budget_tokens).parameters
