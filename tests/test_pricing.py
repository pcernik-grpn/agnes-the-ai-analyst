"""Operator-configurable model prices — the `pricing:` block of instance.yaml.

`tests/test_llm_pricing.py` pins the in-code price table and the cache math.
This file covers the layer above it: an operator who runs a model Agnes has
no rate for (a fine-tune, a partner-operated endpoint, a model newer than the
release) can state its rates in `instance.yaml` instead of reading stale
numbers off every cost surface.

Two properties matter beyond "the override is applied":

- **Config feeds every cost surface, not just cost_usd.** `price_for_model`
  is what `/api/admin/usage`'s `priced_as` readout renders, so a configured
  rate has to reach it too, or the dashboard would explain a cost with rates
  that did not produce it.
- **Bad config must never be fatal.** These rates are read on the chat spend
  guardrail's path; a typo in instance.yaml may not take chat down, and it
  may not silently zero the cap either.
"""

import pytest

from src.llm_pricing import (
    DEFAULT_PRICE,
    PRICES,
    cost_usd,
    price_for_model,
    resolve_price,
)


def _config(monkeypatch, cfg):
    """Stand in for the operator's `pricing:` block."""
    monkeypatch.setattr("src.llm_pricing._pricing_config", lambda: cfg)


class TestConfiguredModelPrices:
    def test_cost_usd_uses_config_override(self, monkeypatch):
        _config(
            monkeypatch,
            {
                "models": {
                    "claude-opus-5": {
                        "input": 10.0,
                        "output": 50.0,
                        "cache_read": 1.0,
                        "cache_write": 12.5,
                    }
                }
            },
        )
        assert cost_usd("claude-opus-5", 1_000_000, 0) == pytest.approx(10.0)
        assert cost_usd("claude-opus-5", 0, 1_000_000) == pytest.approx(50.0)
        assert cost_usd("claude-opus-5", 0, 0, cache_read_tokens=2_000_000) == pytest.approx(2.0)
        assert cost_usd("claude-opus-5", 0, 0, cache_creation_tokens=1_000_000) == pytest.approx(12.5)

    def test_config_reaches_the_price_readout_too(self, monkeypatch):
        """`priced_as` on the usage dashboard renders these four numbers; if
        they came from the in-code table while the cost came from config, the
        readout would explain a figure it did not produce."""
        _config(monkeypatch, {"models": {"claude-opus-5": {"input": 10.0, "output": 50.0, "cache_read": 1.0}}})
        p = price_for_model("claude-opus-5")
        assert (p.input_per_mtok, p.output_per_mtok) == (10.0, 50.0)
        assert p.cache_read_per_mtok == pytest.approx(1.0)
        # cache_write unstated -> standard 1.25x premium on the CONFIGURED
        # input rate, not on the in-code one it replaced.
        assert p.cache_write_per_mtok == pytest.approx(12.5)

    def test_dated_variant_resolves_to_its_configured_family(self, monkeypatch):
        _config(monkeypatch, {"models": {"claude-opus-5": {"input": 10.0, "output": 50.0}}})
        assert price_for_model("claude-opus-5-20260201").input_per_mtok == 10.0
        assert price_for_model("anthropic.claude-opus-5").input_per_mtok == 10.0

    def test_config_keys_are_case_and_whitespace_insensitive(self, monkeypatch):
        _config(monkeypatch, {"models": {"  Claude-Opus-5 ": {"input": 10.0, "output": 50.0}}})
        assert price_for_model("claude-opus-5").input_per_mtok == 10.0

    def test_partial_entry_keeps_the_in_code_rate_it_does_not_name(self, monkeypatch):
        """Correcting one rate must not zero the others."""
        _config(monkeypatch, {"models": {"claude-sonnet-5": {"output": 20.0}}})
        p = price_for_model("claude-sonnet-5")
        assert p.output_per_mtok == 20.0
        assert p.input_per_mtok == PRICES["claude-sonnet-5"].input_per_mtok

    def test_unconfigured_model_still_uses_the_in_code_table(self, monkeypatch):
        _config(monkeypatch, {"models": {"claude-opus-5": {"input": 10.0, "output": 50.0}}})
        assert price_for_model("claude-sonnet-5") is PRICES["claude-sonnet-5"]
        assert cost_usd("claude-sonnet-5", 1_000_000, 0) == pytest.approx(3.0)


class TestUnknownModels:
    def test_pricing_default_prices_an_unknown_model(self, monkeypatch):
        _config(monkeypatch, {"default": {"input": 1.0, "output": 2.0}})
        assert cost_usd("mystery-model", 1_000_000, 0) == pytest.approx(1.0)
        assert cost_usd(None, 0, 1_000_000) == pytest.approx(2.0)

    def test_unknown_model_never_crashes(self, monkeypatch):
        _config(monkeypatch, {})
        assert cost_usd("mystery-model", 5, 5) >= 0.0

    def test_unknown_model_falls_back_conservatively_not_to_zero(self, monkeypatch):
        """DELIBERATE deviation from the plan's "unknown -> zero cost": these
        rates back the chat daily spend cap (`app/chat/manager.py`
        `enforce_sender_limits`), which prices an unattributed day at
        `model=None`. A zero terminal fallback would not read as "unknown", it
        would read as "$0 spent" and the cap would never fire. An operator who
        wants zero states it — `pricing.default: {input: 0, output: 0}`."""
        _config(monkeypatch, {})
        assert price_for_model("mystery-model") is DEFAULT_PRICE
        assert cost_usd("mystery-model", 1_000_000, 0) > 0.0

        _config(monkeypatch, {"default": {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}})
        assert cost_usd("mystery-model", 5_000_000, 5_000_000, 5_000_000, 5_000_000) == 0.0

    def test_a_configured_model_is_not_reached_by_the_default(self, monkeypatch):
        _config(monkeypatch, {"default": {"input": 999.0, "output": 999.0}})
        assert price_for_model("claude-sonnet-5") is PRICES["claude-sonnet-5"]


class TestMalformedConfig:
    @pytest.mark.parametrize(
        "cfg",
        [
            {"models": "not-a-mapping"},
            {"models": {"claude-sonnet-5": "3/15"}},
            {"models": {"claude-sonnet-5": {"input": "free"}}},
            {"models": {"claude-sonnet-5": {"input": None}}},
            {"default": {"input": []}},
            "pricing-is-not-a-mapping",
        ],
    )
    def test_bad_config_degrades_to_in_code_prices_without_raising(self, monkeypatch, cfg):
        """A typo in instance.yaml may not take the chat spend guardrail down,
        and it may not silently make everything free either."""
        _config(monkeypatch, cfg)
        assert price_for_model("claude-sonnet-5") is PRICES["claude-sonnet-5"]
        assert cost_usd("mystery-model", 1_000_000, 0) > 0.0


class TestWiring:
    def test_pricing_config_reads_the_instance_config_pricing_block(self, monkeypatch):
        """The key is `pricing`, read through the shared instance-config
        loader (so an /admin/server-config edit lands without a restart)."""
        seen = []

        def fake_get_value(*keys, default=None):
            seen.append(keys)
            return {"models": {"claude-sonnet-5": {"input": 7.0}}}

        monkeypatch.setattr("app.instance_config.get_value", fake_get_value)
        from src.llm_pricing import _pricing_config

        assert _pricing_config() == {"models": {"claude-sonnet-5": {"input": 7.0}}}
        assert seen == [("pricing",)]

    def test_a_broken_config_loader_is_not_fatal(self, monkeypatch):
        def boom(*keys, default=None):
            raise RuntimeError("config file is being rewritten")

        monkeypatch.setattr("app.instance_config.get_value", boom)
        from src.llm_pricing import _pricing_config

        assert _pricing_config() == {}
        assert price_for_model("claude-sonnet-5") is PRICES["claude-sonnet-5"]

    def test_resolve_price_is_the_same_function_under_its_historical_name(self):
        """Existing callers (`app/api/admin_usage.py`) import `resolve_price`;
        it must not become a second, config-blind lookup."""
        assert resolve_price is price_for_model

    def test_example_config_documents_the_pricing_block(self):
        from pathlib import Path

        text = Path("config/instance.yaml.example").read_text()
        assert "# pricing:" in text
        assert "cache_read" in text
        assert "USD per million tokens" in text
