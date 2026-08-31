"""The Postgres half of the cross-surface usage contract: per-turn records and
a chat session must not change the answer.

``tests/test_usage_surfaces_agree.py`` pins that the dashboards agree about one
user's tokens. This file pins the part that only exists on Postgres, and the
part the shared read model exists to protect:

- ``usage_turns`` (PG-only) gives a finer per-model split than a session
  summary's modal ``primary_model``, so the breakdown SOURCE differs by
  backend — but the totals and the cost must not.
- A built-in chat session records its tokens live, under a bare
  ``chat-<id>.jsonl`` key, while a Claude Code session arrives as an uploaded
  file under ``<dir>/<file>``. Prompt-cache tokens were dropped entirely on the
  chat path before this wave, so the chat leg is where a "cache totals disagree
  between chat and CLI" regression would land.

Both backends run every assertion. That is the point: a figure a user reads off
their own dashboard must not depend on which app-state backend the operator
chose, and the DuckDB leg doubles as the fail-clean proof that a PG-only
repository behind a shared read model degrades to the coarser split instead of
surfacing a 501.

Plan: ``docs/superpowers/plans/2026-08-31-usage-package-per-turn-tokens.md``
Task 10.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from src.llm_pricing import cost_usd
from src.repositories import usage_repo, usage_turns_repo

ANALYST_ID = "analyst1"
ANALYST_EMAIL = "analyst@test.com"
OTHER_ID = "admin1"

MODEL = "claude-sonnet-5"
CHAT_ID = "c0ffee"

#: The analyst's Claude Code session (uploaded file) and their chat session
#: (recorded live). Cache figures are non-zero on BOTH so a path that drops
#: them cannot pass.
CLI_TOKENS = {"input": 100, "output": 200, "cache_read": 300, "cache_creation": 40}
CHAT_TOKENS = {"input": 7, "output": 11, "cache_read": 13, "cache_creation": 17}
OTHER_TOKENS = {"input": 1000, "output": 2000, "cache_read": 3000, "cache_creation": 4000}

EXPECTED = {k: CLI_TOKENS[k] + CHAT_TOKENS[k] for k in CLI_TOKENS}
EXPECTED_TOTAL = sum(EXPECTED.values())
EXPECTED_COST = cost_usd(
    MODEL,
    input_tokens=EXPECTED["input"],
    output_tokens=EXPECTED["output"],
    cache_read_tokens=EXPECTED["cache_read"],
    cache_creation_tokens=EXPECTED["cache_creation"],
)
#: Transport rounds USD to the microdollar.
USD = 1e-6


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _summary(session_file: str, session_id: str, user_id: str, username: str, tokens: dict) -> dict:
    started = datetime.now(timezone.utc) - timedelta(hours=1)
    return {
        "session_file": session_file,
        "session_id": session_id,
        "username": username,
        "user_id": user_id,
        "started_at": started,
        "ended_at": started + timedelta(minutes=5),
        "active_seconds": 300,
        "wall_seconds": 300,
        "user_messages": 1,
        "assistant_messages": 1,
        "tool_calls": 1,
        "tool_errors": 0,
        "primary_model": MODEL,
        "input_tokens": tokens["input"],
        "output_tokens": tokens["output"],
        "cache_read_tokens": tokens["cache_read"],
        "cache_creation_tokens": tokens["cache_creation"],
    }


def _turn(session_file: str, user_id: str, surface: str, tokens: dict) -> dict:
    return {
        "session_file": session_file,
        "session_id": session_file.rsplit("/", 1)[-1].removesuffix(".jsonl"),
        "user_id": user_id,
        "surface": surface,
        "turn_uuid": str(uuid4()),
        "model": MODEL,
        "input_tokens": tokens["input"],
        "output_tokens": tokens["output"],
        "cache_read_tokens": tokens["cache_read"],
        "cache_creation_tokens": tokens["cache_creation"],
        "occurred_at": datetime.now(timezone.utc) - timedelta(minutes=30),
    }


@pytest.fixture
def usage_both(seeded_app_both):
    """Seeded app on both backends + one CLI session, one chat session, and a
    third belonging to somebody else. Turn rows are written only where the
    table exists; the assertions do not branch on that."""
    repo = usage_repo()
    cli_file = f"{ANALYST_ID}/s1.jsonl"
    # Bare key, no directory: this is what the chat manager writes live, and
    # it is why the summary and the turn rows for one chat session are keyed
    # differently from an uploaded file's.
    chat_file = f"chat-{CHAT_ID}.jsonl"

    repo.upsert_summary(_summary(cli_file, "s-cli", ANALYST_ID, ANALYST_EMAIL, CLI_TOKENS), processor_version=1)
    repo.upsert_summary(
        _summary(f"{ANALYST_ID}/{chat_file}", f"chat-{CHAT_ID}", ANALYST_ID, ANALYST_EMAIL, CHAT_TOKENS),
        processor_version=1,
    )
    repo.upsert_summary(
        _summary(f"{OTHER_ID}/s1.jsonl", "s-other", OTHER_ID, "admin@test.com", OTHER_TOKENS),
        processor_version=1,
    )

    if seeded_app_both["backend"] == "pg":
        # The CLI session's tokens split across two assistant turns; the chat
        # session's arrive as one live-written turn. Together they account for
        # exactly the analyst's summary totals, which is the precondition for
        # the finer split being trusted.
        half = {k: v // 2 for k, v in CLI_TOKENS.items()}
        rest = {k: CLI_TOKENS[k] - half[k] for k in CLI_TOKENS}
        usage_turns_repo().insert_batch(
            [
                _turn(cli_file, ANALYST_ID, "claude_code", half),
                _turn(cli_file, ANALYST_ID, "claude_code", rest),
                _turn(chat_file, ANALYST_ID, "web", CHAT_TOKENS),
                _turn(f"{OTHER_ID}/s1.jsonl", OTHER_ID, "claude_code", OTHER_TOKENS),
            ]
        )
    return seeded_app_both


def _me_tokens(usage_both) -> dict:
    resp = usage_both["client"].get("/api/me/stats/tokens?days=30", headers=_auth(usage_both["analyst_token"]))
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_chat_and_cli_tokens_land_in_one_total(usage_both):
    """Two ingest paths, one number. The cache figures are the ones that used
    to be dropped on the chat path."""
    totals = _me_tokens(usage_both)["totals"]
    for key, want in EXPECTED.items():
        assert totals[key] == want, f"{key} on {usage_both['backend']}"
    assert totals["total"] == EXPECTED_TOTAL
    assert totals["cache_read"] == CLI_TOKENS["cache_read"] + CHAT_TOKENS["cache_read"]
    assert totals["cache_creation"] == CLI_TOKENS["cache_creation"] + CHAT_TOKENS["cache_creation"]


def test_cost_is_backend_independent(usage_both):
    """The per-model split comes from a different table on each backend. The
    cost must not."""
    assert _me_tokens(usage_both)["totals"]["cost_usd"] == pytest.approx(EXPECTED_COST, abs=USD)


def test_admin_slice_agrees_with_the_users_own_view(usage_both):
    resp = usage_both["client"].get(
        f"/api/admin/adoption/users/{ANALYST_ID}/kpis?window=30d",
        headers=_auth(usage_both["admin_token"]),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tokens"] == EXPECTED_TOTAL
    assert body["cost_usd"] == pytest.approx(EXPECTED_COST, abs=USD)


def test_telemetry_card_prices_the_same_user_the_same(usage_both):
    resp = usage_both["client"].get(
        f"/api/admin/telemetry/kpis?since_minutes=43200&username={ANALYST_EMAIL}",
        headers=_auth(usage_both["admin_token"]),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["cost_usd"] == pytest.approx(EXPECTED_COST, abs=USD)


def test_per_turn_split_is_used_when_it_reconciles(usage_both, state_backend):
    """Without this the PG leg would be vacuous — every assertion above passes
    on the summary split too, so something has to prove the turn rows are what
    Postgres actually read."""
    from app.services import usage_stats

    result = usage_stats.token_totals(ANALYST_ID, 30)
    assert result["tokens_source"] == ("turns" if state_backend == "pg" else "summaries")
    assert sum(r["cost_usd"] for r in result["by_model"]) == pytest.approx(result["cost_usd"], abs=USD)
    if state_backend == "pg":
        # Three turns for the analyst: two Claude Code, one chat.
        assert sum(r["turns"] or 0 for r in result["by_model"]) == 3


def test_a_backfill_gap_falls_back_instead_of_underpricing(usage_both, state_backend):
    """Turn rows cover only what the processor has walked. When they account
    for less than the summaries report, pricing them would put a cost on screen
    that does not add up to the tokens beside it — so the coarser split wins."""
    if state_backend != "pg":
        pytest.skip("the reconciliation branch only exists where usage_turns does")
    from app.services import usage_stats

    # A summary with no turns behind it — exactly the mid-backfill state.
    usage_repo().upsert_summary(
        _summary(f"{ANALYST_ID}/s2.jsonl", "s-cli-2", ANALYST_ID, ANALYST_EMAIL, CLI_TOKENS),
        processor_version=1,
    )
    result = usage_stats.token_totals(ANALYST_ID, 30)
    assert result["tokens_source"] == "summaries"
    assert result["input"] == EXPECTED["input"] + CLI_TOKENS["input"]
    assert sum(r["cost_usd"] for r in result["by_model"]) == pytest.approx(result["cost_usd"], abs=USD)
