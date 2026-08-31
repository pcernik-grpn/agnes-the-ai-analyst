"""The chat path records prompt-cache tokens, and the budget counts them the
same way everywhere.

`app/chat/runner.py` used to read only `input_tokens`/`output_tokens` off the
SDK usage object. Since `input_tokens` counts UNCACHED input only, a long
session's actual context volume — the term that dominates its cost — had no
column at all, so it could only ever be modelled after the fact. These tests
pin the plumbing (runner -> frame -> repository) and the one arithmetic
convention the two budget surfaces must share.
"""

from __future__ import annotations

import inspect

from app.chat.manager import ChatManager
from src.llm_pricing import budget_tokens


class TestRunnerCapturesBothCacheFields:
    """The runner runs inside the sandbox against the live SDK, so its
    contract is asserted on the source: the two usage fields must be read
    and must reach the outbound frame."""

    @staticmethod
    def _runner_source() -> str:
        import app.chat.runner as runner

        return inspect.getsource(runner)

    def test_reads_both_usage_fields(self):
        src = self._runner_source()
        assert 'msg.usage.get("cache_read_input_tokens"' in src
        assert 'msg.usage.get("cache_creation_input_tokens"' in src

    def test_puts_them_on_the_assistant_message_frame(self):
        """Alongside tokens_in/tokens_out in the turn-end frame — the manager
        reads them straight off it."""
        src = self._runner_source()
        marker = '"tokens_in": tokens_in,'
        blocks = src.split(marker)[1:]
        assert blocks, "no token-carrying assistant_message frame found"
        # EVERY such frame, not just the turn-end one: the idle-timeout
        # partial-save path persists a turn's usage too, and a turn that
        # times out still burned (and wrote) cached context.
        for block in blocks:
            assert '"cache_read_tokens": cache_read_tokens' in block[:1200]
            assert '"cache_creation_tokens": cache_creation_tokens' in block[:1200]


class TestBudgetConvention:
    """`input + output + cache_creation`, cache reads excluded — the same
    definition the agent `token_budget_monthly` path already used. If the
    live counter and the DB aggregate it re-seeds from ever disagree, a
    restart silently moves a user's remaining budget."""

    def test_shared_helper_agrees(self):
        assert budget_tokens(input_tokens=1000, output_tokens=2000, cache_creation_tokens=500) == 3500

    def test_pg_aggregate_folds_cache_writes_into_the_in_total(self):
        """The durable aggregate the counter re-seeds from must fold in the
        same term the counter does."""
        import src.repositories.chat_messages_pg as mod

        src = inspect.getsource(mod.ChatMessagePgRepository.daily_anthropic_tokens)
        assert "cache_creation_tokens" in src

    def test_manager_prices_through_the_shared_module(self):
        """No hardcoded per-model constants left in the spend guardrail —
        they were pinned to one model's rates and had no cache rate at all."""
        import app.chat.manager as mod

        src = inspect.getsource(mod)
        assert "_PRICE_IN_PER_MTOK" not in src
        assert "_PRICE_OUT_PER_MTOK" not in src
        assert "cost_usd(" in src


def test_chatmanager_record_daily_tokens_signature_is_backward_compatible():
    params = list(inspect.signature(ChatManager._record_daily_tokens).parameters)
    assert params[:4] == ["self", "user_email", "tokens_in", "tokens_out"]
    assert params[4] == "cache_creation_tokens"
