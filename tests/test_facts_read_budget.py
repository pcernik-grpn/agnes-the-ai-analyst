"""`_ReadBudget` — one wall-clock budget across a multi-statement facts read.

`statement_timeout` is per statement; without a shared budget a read that
runs N statements gets N × the guard. These tests pin the mechanism: the
timeout re-armed before each statement is the time LEFT, an exhausted
budget raises the typed error before starting another statement, and the
value handed to Postgres is never 0 (which Postgres reads as "no timeout").
"""

from __future__ import annotations

import pytest

import src.repositories.facts_pg as facts_pg
from src.repositories.facts_pg import FactsQueryTimeout, _ReadBudget


class _FakeConn:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, clause, *args, **kwargs):
        self.statements.append(str(clause))


def test_budget_rearms_with_the_time_left(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(facts_pg.time, "monotonic", lambda: now[0])
    budget = _ReadBudget(20_000, reason="facts_neighbors_timeout", message="slow")
    conn = _FakeConn()

    budget.arm(conn)
    now[0] += 15.0
    budget.arm(conn)

    assert conn.statements == ["SET LOCAL statement_timeout = 20000", "SET LOCAL statement_timeout = 5000"]


def test_exhausted_budget_raises_typed_error_and_never_arms_zero(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(facts_pg.time, "monotonic", lambda: now[0])
    budget = _ReadBudget(20_000, reason="facts_summary_timeout", message="too slow, narrow it")
    conn = _FakeConn()

    budget.arm(conn)
    now[0] += 20.0  # exactly spent: remaining == 0 must NOT reach Postgres (0 disables the timeout)
    with pytest.raises(FactsQueryTimeout) as excinfo:
        budget.arm(conn)

    assert conn.statements == ["SET LOCAL statement_timeout = 20000"]
    assert excinfo.value.reason == "facts_summary_timeout"
    assert str(excinfo.value) == "too slow, narrow it"


def test_typed_statement_timeout_translates_only_cancellation():
    import sqlalchemy as sa

    class _Canceled(Exception):
        sqlstate = "57014"

    class _Other(Exception):
        sqlstate = "23505"

    budget = _ReadBudget(1_000, reason="facts_neighbors_timeout", message="hint")
    with pytest.raises(FactsQueryTimeout) as excinfo:
        with facts_pg._typed_statement_timeout(budget):
            raise sa.exc.DBAPIError("stmt", {}, _Canceled())
    assert excinfo.value.reason == "facts_neighbors_timeout" and str(excinfo.value) == "hint"

    with pytest.raises(sa.exc.DBAPIError):
        with facts_pg._typed_statement_timeout(budget):
            raise sa.exc.DBAPIError("stmt", {}, _Other())
