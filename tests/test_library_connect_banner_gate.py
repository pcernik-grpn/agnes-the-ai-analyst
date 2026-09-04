"""`/library`'s connect-invitation gate — `app.web.router._has_connected_tools`.

The banner exists to tell a user who has not connected Agnes to anything that
they can. Getting the gate wrong in the "already connected" direction is the
expensive one: the invitation is withheld from exactly the person who needs
it, and nothing surfaces that it happened.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.web import router as web_router


class _Tokens:
    def __init__(self, rows):
        self._rows = rows

    def list_for_user(self, user_id, include_revoked=True):
        # Mirrors the real repository: it filters revoked_at and NOTHING else.
        return list(self._rows)


class _Journey:
    def __init__(self, state=None):
        self._state = state or {}

    def get(self, user_id):
        return dict(self._state)


def _install(monkeypatch, *, tokens, journey=None):
    import src.repositories as repos

    monkeypatch.setattr(repos, "access_token_repo", lambda: _Tokens(tokens), raising=False)
    monkeypatch.setattr(repos, "user_journey_repo", lambda: _Journey(journey), raising=False)


def _at(delta: timedelta) -> datetime:
    return datetime.now(timezone.utc) + delta


@pytest.mark.parametrize(
    "expires_at",
    [
        _at(timedelta(days=-1)),
        _at(timedelta(days=-1)).isoformat(),
        _at(timedelta(days=-1)).replace(tzinfo=None),
    ],
    ids=["datetime", "iso-string", "naive"],
)
def test_an_expired_token_is_not_a_connection(monkeypatch, expires_at):
    """An expired PAT must not read as connected.

    `list_for_user(include_revoked=False)` appends `AND revoked_at IS NULL`
    and has no expiry predicate, so a token that lapsed years ago still comes
    back — and counting it told a user with nothing usable that they were
    already connected. All three storage shapes are covered because they all
    occur: Postgres hands back a `datetime`, DuckDB an ISO string, and a
    stored value can be naive where the comparison is aware.
    """
    _install(monkeypatch, tokens=[{"id": "t1", "expires_at": expires_at}])
    assert web_router._has_connected_tools({"id": "u1"}) is False


def test_a_live_token_is_a_connection(monkeypatch):
    """The other direction, so the fix cannot be "return False always"."""
    _install(monkeypatch, tokens=[{"id": "t1", "expires_at": _at(timedelta(days=30))}])
    assert web_router._has_connected_tools({"id": "u1"}) is True


def test_a_token_with_no_expiry_never_expires(monkeypatch):
    """`expires_at IS NULL` means "never expires", not "expired" — the shape
    `agnes init` writes by default."""
    _install(monkeypatch, tokens=[{"id": "t1", "expires_at": None}])
    assert web_router._has_connected_tools({"id": "u1"}) is True


def test_one_live_token_among_expired_ones_still_counts(monkeypatch):
    """The gate asks "any live", not "the newest is live"."""
    _install(
        monkeypatch,
        tokens=[
            {"id": "old", "expires_at": _at(timedelta(days=-9))},
            {"id": "live", "expires_at": _at(timedelta(days=9))},
        ],
    )
    assert web_router._has_connected_tools({"id": "u1"}) is True


def test_the_journey_flag_short_circuits_before_tokens(monkeypatch):
    """Reaching the connect page counts on its own — the token read is the
    second question, not the only one."""
    _install(monkeypatch, tokens=[], journey={"use_anywhere": True})
    assert web_router._has_connected_tools({"id": "u1"}) is True
