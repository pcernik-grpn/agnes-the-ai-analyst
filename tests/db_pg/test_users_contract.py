"""Cross-engine contract tests for the users repository.

Parametrises over [DuckDB impl, Postgres impl]. The same calls go to
both; the same return shapes must come back. Any divergence is a bug in
whichever side is wrong.

This follows the pattern established in test_audit_contract.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import duckdb
import pytest


# ---------------------------------------------------------------------------
# repo construction helpers — one per backend
# ---------------------------------------------------------------------------


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.repositories.users import UserRepository

    conn = duckdb.connect(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return UserRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
    """Run migrations on the per-test PG engine, then return a PG repo."""
    from pathlib import Path
    from alembic import command
    from alembic.config import Config

    REPO_ROOT = Path(__file__).resolve().parents[2]
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.users_pg import UsersPgRepository

    return UsersPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def users_repo(request, tmp_path, pg_engine, monkeypatch):
    """Yields ``(repo, raw_conn_or_None, backend)`` for both backends."""
    backend = request.param
    if backend == "duckdb":
        repo, conn = _make_duckdb_repo(tmp_path)
        yield repo, conn, backend
        if conn is not None:
            conn.close()
    else:
        repo, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield repo, None, backend


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_user(repo, **kwargs):
    defaults = {"id": "user-1", "email": "u@example.com", "name": "U"}
    defaults.update(kwargs)
    repo.create(**defaults)


def _set_created_at(repo, backend, user_id, ts):
    """Backfill ``created_at`` for a user. ``create()`` stamps now(); the
    recency-ordering test needs deterministic spread, so we bump it directly
    on whichever backend store the repo holds."""
    if backend == "duckdb":
        repo.conn.execute("UPDATE users SET created_at = ? WHERE id = ?", [ts, user_id])
    else:
        import sqlalchemy as sa

        with repo._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE users SET created_at = :ts WHERE id = :id"),
                {"ts": ts, "id": user_id},
            )


def _add_group_and_member(repo, backend, group_id, group_name, user_id):
    """Create a group (FK target) + a membership row on the repo's backend.

    Raw SQL (rather than the group repos) keeps the contract test free of
    those repos' construction quirks — search_recent only reads
    ``user_group_members`` via EXISTS, so a plain membership row is enough."""
    if backend == "duckdb":
        repo.conn.execute(
            "INSERT INTO user_groups (id, name, is_system, created_at) VALUES (?, ?, FALSE, current_timestamp)",
            [group_id, group_name],
        )
        repo.conn.execute(
            "INSERT INTO user_group_members (user_id, group_id, source) VALUES (?, ?, 'admin')",
            [user_id, group_id],
        )
    else:
        import sqlalchemy as sa

        with repo._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO user_groups (id, name, is_system, created_at) "
                    "VALUES (:id, :name, FALSE, current_timestamp)"
                ),
                {"id": group_id, "name": group_name},
            )
            conn.execute(
                sa.text("INSERT INTO user_group_members (user_id, group_id, source) VALUES (:uid, :gid, 'admin')"),
                {"uid": user_id, "gid": group_id},
            )


# ---------------------------------------------------------------------------
# contract tests — same calls, same answers from both engines
# ---------------------------------------------------------------------------


def test_create_then_get_by_id_returns_same_row(users_repo):
    repo, _, _ = users_repo
    _make_user(repo)
    row = repo.get_by_id("user-1")
    assert row is not None
    assert row["id"] == "user-1"
    assert row["email"] == "u@example.com"
    assert row["name"] == "U"


def test_create_then_get_by_email_returns_same_row(users_repo):
    repo, _, _ = users_repo
    _make_user(repo)
    row = repo.get_by_email("u@example.com")
    assert row is not None
    assert row["id"] == "user-1"
    assert row["email"] == "u@example.com"


def test_get_by_id_missing_returns_none(users_repo):
    repo, _, _ = users_repo
    row = repo.get_by_id("nonexistent-user")
    assert row is None


def test_update_password_hash_persists(users_repo):
    repo, _, _ = users_repo
    _make_user(repo)
    repo.update("user-1", password_hash="argon2id$xxxx")
    row = repo.get_by_id("user-1")
    assert row is not None
    assert row["password_hash"] == "argon2id$xxxx"


def test_deactivate_marks_active_false_and_sets_metadata(users_repo):
    repo, _, _ = users_repo
    _make_user(repo)
    now = datetime.now(timezone.utc)
    repo.update("user-1", active=False, deactivated_at=now, deactivated_by="admin@example.com")
    row = repo.get_by_id("user-1")
    assert row is not None
    assert row["active"] is False
    assert row["deactivated_at"] is not None
    assert row["deactivated_by"] == "admin@example.com"


def test_list_all_orders_by_email(users_repo):
    repo, _, _ = users_repo
    _make_user(repo, id="user-a", email="b@x.com", name="B")
    _make_user(repo, id="user-b", email="a@x.com", name="A")
    rows = repo.list_all()
    emails = [r["email"] for r in rows]
    assert emails == sorted(emails)


def test_count_all_increments_on_create(users_repo):
    repo, _, _ = users_repo
    before = repo.count_all()
    _make_user(repo)
    after = repo.count_all()
    assert after == before + 1


# ---------------------------------------------------------------------------
# any_password_holder — cheap existence probe (zero-door email rescue, PR #1548)
#
# Backs `provider_registry._has_usable_password_holder`, which runs on every
# unauthenticated /login render of a no-OAuth instance — hence an existence
# probe with LIMIT 1 rather than a list_all() scan. The predicate must mirror
# the Python filter it replaced: truthiness of the hash (NULL and '' both
# read as "no password") and an email exclusion for the synthetic scheduler
# account.
# ---------------------------------------------------------------------------


def test_any_password_holder_false_on_empty_table(users_repo):
    repo, _, _ = users_repo
    assert repo.any_password_holder() is False


def test_any_password_holder_true_once_a_user_holds_a_hash(users_repo):
    repo, _, _ = users_repo
    _make_user(repo, id="user-nohash", email="a@example.com")
    assert repo.any_password_holder() is False
    _make_user(repo, id="user-hash", email="b@example.com", password_hash="argon2id$xxxx")
    assert repo.any_password_holder() is True


def test_any_password_holder_empty_string_hash_does_not_count(users_repo):
    """The Python predicate this probe replaced tested truthiness, so an
    empty-string hash never counted as a password; the SQL must agree."""
    repo, _, _ = users_repo
    _make_user(repo, password_hash="")
    assert repo.any_password_holder() is False


def test_any_password_holder_excludes_the_given_email(users_repo):
    """A synthetic account (the scheduler user) may carry a hash but cannot
    sign in interactively — with `exclude_email` set it must not count as a
    holder, while any OTHER holder still does."""
    repo, _, _ = users_repo
    _make_user(repo, id="user-sched", email="scheduler@internal.invalid", password_hash="argon2id$sched")
    assert repo.any_password_holder() is True
    assert repo.any_password_holder(exclude_email="scheduler@internal.invalid") is False
    _make_user(repo, id="user-human", email="human@example.com", password_hash="argon2id$human")
    assert repo.any_password_holder(exclude_email="scheduler@internal.invalid") is True


def test_delete_removes_user(users_repo):
    repo, _, _ = users_repo
    _make_user(repo)
    assert repo.get_by_id("user-1") is not None
    repo.delete("user-1")
    assert repo.get_by_id("user-1") is None


def test_set_and_get_by_slack_user_id(users_repo):
    """v71: the Slack identity binding round-trips identically on both engines
    (update(slack_user_id=...) + get_by_slack_user_id)."""
    repo, _, _ = users_repo
    _make_user(repo)

    # Unbound: no slack_user_id, lookup misses.
    row = repo.get_by_id("user-1")
    assert row.get("slack_user_id") is None
    assert repo.get_by_slack_user_id("U999") is None

    repo.update("user-1", slack_user_id="U999")
    row = repo.get_by_id("user-1")
    assert row["slack_user_id"] == "U999"

    bound = repo.get_by_slack_user_id("U999")
    assert bound is not None
    assert bound["id"] == "user-1"


# ---------------------------------------------------------------------------
# search_recent — recency window + backend search/group filter (FAI-23)
# ---------------------------------------------------------------------------


def test_search_recent_orders_by_created_at_desc_and_respects_limit(users_repo):
    repo, _, backend = users_repo
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # Insert oldest→newest; created_at backfilled so order is deterministic.
    for i in range(3):
        _make_user(repo, id=f"user-{i}", email=f"u{i}@x.com", name=f"U{i}")
        _set_created_at(repo, backend, f"user-{i}", base.replace(day=i + 1))

    rows = repo.search_recent(limit=2)
    assert [r["id"] for r in rows] == ["user-2", "user-1"]  # newest first, capped

    rows_all = repo.search_recent(limit=10)
    assert [r["id"] for r in rows_all] == ["user-2", "user-1", "user-0"]


def test_search_recent_filters_by_email_or_name_case_insensitive(users_repo):
    repo, _, _ = users_repo
    _make_user(repo, id="user-a", email="alice@x.com", name="Alice")
    _make_user(repo, id="user-b", email="bob@x.com", name="Bob")

    by_email = repo.search_recent(search="ALICE@")
    assert [r["id"] for r in by_email] == ["user-a"]

    by_name = repo.search_recent(search="bo")
    assert [r["id"] for r in by_name] == ["user-b"]

    none = repo.search_recent(search="zzz")
    assert none == []


def test_search_recent_filters_by_group_membership(users_repo):
    repo, _, backend = users_repo
    _make_user(repo, id="user-in", email="in@x.com", name="In")
    _make_user(repo, id="user-out", email="out@x.com", name="Out")
    _add_group_and_member(repo, backend, "grp-1", "data-team", "user-in")

    rows = repo.search_recent(group_id="grp-1")
    assert [r["id"] for r in rows] == ["user-in"]


def test_search_recent_combines_search_and_group_filter(users_repo):
    repo, _, backend = users_repo
    _make_user(repo, id="user-1", email="alice@x.com", name="Alice")
    _make_user(repo, id="user-2", email="alex@x.com", name="Alex")
    _add_group_and_member(repo, backend, "grp-1", "data-team", "user-1")

    # Both match "al", but only user-1 is in the group.
    rows = repo.search_recent(search="al", group_id="grp-1")
    assert [r["id"] for r in rows] == ["user-1"]


# ---------------------------------------------------------------------------
# must_change_password — forced-rotation flag parity (v77)
# ---------------------------------------------------------------------------


def test_must_change_password_defaults_false(users_repo):
    repo, _, _ = users_repo
    _make_user(repo)
    row = repo.get_by_id("user-1")
    assert row is not None
    assert row["must_change_password"] is False


def test_create_with_must_change_password_true_round_trips(users_repo):
    repo, _, _ = users_repo
    _make_user(repo, must_change_password=True)
    row = repo.get_by_id("user-1")
    assert row is not None
    assert row["must_change_password"] is True


def test_update_toggles_must_change_password(users_repo):
    repo, _, _ = users_repo
    _make_user(repo, must_change_password=True)
    repo.update("user-1", must_change_password=False)
    assert repo.get_by_id("user-1")["must_change_password"] is False
    repo.update("user-1", must_change_password=True)
    assert repo.get_by_id("user-1")["must_change_password"] is True


# ---------------------------------------------------------------------------
# consume_reset_token — backend-aware atomic CAS parity
# ---------------------------------------------------------------------------


def _seed_reset_token(repo, *, created):
    """Create user-1 with a reset token issued at ``created``."""
    _make_user(repo)
    repo.update("user-1", reset_token="rtok", reset_token_created=created)


def test_consume_reset_token_valid_wins_and_stamps(users_repo):
    repo, _, _ = users_repo
    now = datetime.now(timezone.utc)
    _seed_reset_token(repo, created=now)
    won = repo.consume_reset_token(
        email="u@example.com",
        token="rtok",
        cutoff=now - timedelta(hours=24),
        consume_id="CONSUMED:abc",
    )
    assert won == "user-1", "the CAS must report WHICH row it stamped"
    # token is replaced by the consume marker (single-use)
    assert repo.get_by_id("user-1")["reset_token"] == "CONSUMED:abc"


def test_consume_reset_token_matches_the_address_case_insensitively(users_repo):
    """The sign-in paths resolve identity with ``get_by_email_ci``, so the token
    is minted on whichever case variant is the account. A case-SENSITIVE CAS
    would mint a working link and then refuse to open it."""
    repo, _, _ = users_repo
    now = datetime.now(timezone.utc)
    _make_user(repo, id="user-mixed", email="Mixed.Case@Example.com")
    repo.update("user-mixed", reset_token="rtok", reset_token_created=now)
    won = repo.consume_reset_token(
        email="mixed.case@example.com",
        token="rtok",
        cutoff=now - timedelta(hours=24),
        consume_id="CONSUMED:abc",
    )
    assert won == "user-mixed"
    assert repo.get_by_id("user-mixed")["reset_token"] == "CONSUMED:abc"


def test_consume_reset_token_wrong_token_loses(users_repo):
    repo, _, _ = users_repo
    now = datetime.now(timezone.utc)
    _seed_reset_token(repo, created=now)
    won = repo.consume_reset_token(
        email="u@example.com",
        token="WRONG",
        cutoff=now - timedelta(hours=24),
        consume_id="CONSUMED:abc",
    )
    assert won is None
    assert repo.get_by_id("user-1")["reset_token"] == "rtok"  # untouched


def test_consume_reset_token_expired_loses(users_repo):
    repo, _, _ = users_repo
    now = datetime.now(timezone.utc)
    _seed_reset_token(repo, created=now - timedelta(hours=25))  # older than the 24h cutoff
    won = repo.consume_reset_token(
        email="u@example.com",
        token="rtok",
        cutoff=now - timedelta(hours=24),
        consume_id="CONSUMED:abc",
    )
    assert won is None


def test_consume_reset_token_single_use(users_repo):
    repo, _, _ = users_repo
    now = datetime.now(timezone.utc)
    _seed_reset_token(repo, created=now)
    cutoff = now - timedelta(hours=24)
    assert repo.consume_reset_token(email="u@example.com", token="rtok", cutoff=cutoff, consume_id="CONSUMED:1")
    # second attempt with the same original token loses (already consumed)
    assert repo.consume_reset_token(email="u@example.com", token="rtok", cutoff=cutoff, consume_id="CONSUMED:2") is None


# ---------------------------------------------------------------------------
# get_by_ids — bulk id → email map parity
# ---------------------------------------------------------------------------


def test_get_by_ids_maps_present_ids(users_repo):
    repo, _, _ = users_repo
    _make_user(repo, id="user-1", email="a@example.com")
    _make_user(repo, id="user-2", email="b@example.com")
    got = repo.get_by_ids(["user-1", "user-2", "missing"])
    assert got == {"user-1": "a@example.com", "user-2": "b@example.com"}


def test_get_by_ids_empty_input_returns_empty(users_repo):
    repo, _, _ = users_repo
    _make_user(repo)
    assert repo.get_by_ids([]) == {}


# ---------------------------------------------------------------------------
# get_info_by_ids — bulk id → {email, name} map parity
# ---------------------------------------------------------------------------


def test_get_info_by_ids_maps_email_and_name(users_repo):
    repo, _, _ = users_repo
    _make_user(repo, id="user-1", email="a@example.com", name="Alice")
    _make_user(repo, id="user-2", email="b@example.com", name="Bob")
    got = repo.get_info_by_ids(["user-1", "user-2", "missing"])
    assert got == {
        "user-1": {"email": "a@example.com", "name": "Alice"},
        "user-2": {"email": "b@example.com", "name": "Bob"},
    }


def test_get_info_by_ids_empty_input_returns_empty(users_repo):
    repo, _, _ = users_repo
    _make_user(repo)
    assert repo.get_info_by_ids([]) == {}


# ---------------------------------------------------------------------------
# get_by_email_ci — case-insensitive identity lookup parity
#
# Both engines compare strings case-SENSITIVELY on `=`, so the shared
# provisioning path (app/auth/provisioning.py::ensure_user) needs an explicit
# case-folded lookup to keep one person on one account across providers.
# ---------------------------------------------------------------------------


def test_get_by_email_ci_matches_regardless_of_case(users_repo):
    repo, _, _ = users_repo
    _make_user(repo, id="user-mixed", email="Mixed.Case@Example.com")
    assert repo.get_by_email("mixed.case@example.com") is None  # exact match is case-sensitive
    row = repo.get_by_email_ci("mixed.case@example.com")
    assert row is not None
    assert row["id"] == "user-mixed"
    assert row["email"] == "Mixed.Case@Example.com"


def test_get_by_email_ci_no_match_returns_none(users_repo):
    repo, _, _ = users_repo
    _make_user(repo, id="user-a", email="a@example.com")
    assert repo.get_by_email_ci("nobody@example.com") is None


def test_get_by_email_ci_is_not_a_prefix_or_wildcard_match(users_repo):
    repo, _, _ = users_repo
    _make_user(repo, id="user-a", email="alice@example.com")
    assert repo.get_by_email_ci("alic") is None
    assert repo.get_by_email_ci("%@example.com") is None


def test_get_by_email_ci_picks_the_oldest_when_case_variants_coexist(users_repo):
    """Historic rows may already differ only in case. The oldest wins on both
    backends so the answer is deterministic and the original account keeps the
    identity."""
    repo, _, backend = users_repo
    _make_user(repo, id="user-old", email="Dup@example.com")
    _make_user(repo, id="user-new", email="dup@example.com")
    _set_created_at(repo, backend, "user-old", datetime(2025, 1, 1, tzinfo=timezone.utc))
    _set_created_at(repo, backend, "user-new", datetime(2026, 6, 1, tzinfo=timezone.utc))
    row = repo.get_by_email_ci("DUP@EXAMPLE.COM")
    assert row is not None
    assert row["id"] == "user-old"


def test_get_by_email_ci_tiebreaks_deterministically_on_identical_created_at(users_repo):
    """``created_at`` is not unique. Rows written in the same transaction (or
    backfilled with the same timestamp) tie, and a bare ``ORDER BY created_at``
    then leaves the winner to whatever order the engine happens to return —
    which need not agree between DuckDB and Postgres, or between two runs on
    one engine. The id breaks the tie so one identity always resolves to one
    account."""
    repo, _, backend = users_repo
    same = datetime(2025, 3, 1, tzinfo=timezone.utc)
    _make_user(repo, id="user-b", email="Tie@example.com")
    _make_user(repo, id="user-a", email="tie@example.com")
    _make_user(repo, id="user-c", email="TIE@EXAMPLE.COM")
    for uid in ("user-a", "user-b", "user-c"):
        _set_created_at(repo, backend, uid, same)
    row = repo.get_by_email_ci("tie@example.com")
    assert row is not None
    assert row["id"] == "user-a"


def test_consume_reset_token_reports_the_variant_that_held_the_token(users_repo):
    """The CAS finds whichever case variant actually holds the token, while
    ``get_by_email_ci`` returns the OLDEST. A token minted by user id (an
    admin-issued reset) can sit on a newer variant, so the caller must learn
    the row from the CAS — resolving by address afterwards would hand it a
    different account."""
    repo, _, backend = users_repo
    now = datetime.now(timezone.utc)
    _make_user(repo, id="user-old", email="dup@example.com")
    _make_user(repo, id="user-new", email="Dup@Example.com")
    _set_created_at(repo, backend, "user-old", datetime(2025, 1, 1, tzinfo=timezone.utc))
    _set_created_at(repo, backend, "user-new", datetime(2026, 6, 1, tzinfo=timezone.utc))
    # The token lives on the NEWER row.
    repo.update("user-new", reset_token="rtok", reset_token_created=now)

    assert repo.get_by_email_ci("dup@example.com")["id"] == "user-old"
    won = repo.consume_reset_token(
        email="dup@example.com", token="rtok", cutoff=now - timedelta(hours=24), consume_id="CONSUMED:xyz"
    )
    assert won == "user-new"


def test_list_by_email_ci_returns_every_colliding_row_oldest_first(users_repo):
    repo, _, backend = users_repo
    _make_user(repo, id="user-new", email="Dup@Example.com")
    _make_user(repo, id="user-old", email="dup@example.com")
    _make_user(repo, id="user-other", email="someone@example.com")
    _set_created_at(repo, backend, "user-old", datetime(2025, 1, 1, tzinfo=timezone.utc))
    _set_created_at(repo, backend, "user-new", datetime(2026, 6, 1, tzinfo=timezone.utc))
    rows = repo.list_by_email_ci("DUP@example.COM")
    assert [r["id"] for r in rows] == ["user-old", "user-new"]
    assert repo.list_by_email_ci("nobody@example.com") == []


def test_get_by_email_ci_does_not_prefer_an_active_row(users_repo):
    """Selection must not depend on the ``active`` flag.

    Callers gate on the returned row's own ``active`` value, so ranking active
    rows first would let a still-enabled duplicate serve a sign-in the operator
    just disabled — offboarding bypassed by a hidden case variant. Oldest wins
    regardless, which fails closed: the disabled row is returned and the
    caller's gate refuses."""
    repo, _, backend = users_repo
    _make_user(repo, id="user-old", email="Dup@example.com")
    _make_user(repo, id="user-new", email="dup@example.com")
    _set_created_at(repo, backend, "user-old", datetime(2025, 1, 1, tzinfo=timezone.utc))
    _set_created_at(repo, backend, "user-new", datetime(2026, 6, 1, tzinfo=timezone.utc))
    repo.update("user-old", active=False)

    row = repo.get_by_email_ci("dup@example.com")
    assert row is not None
    assert row["id"] == "user-old", "a still-active duplicate must not outrank the deactivated oldest"
    assert row["active"] is False


# ---------------------------------------------------------------------------
# get_by_email_prefix — session-directory-name → user resolution parity
# ---------------------------------------------------------------------------


def _set_updated_at(repo, backend, user_id, ts):
    """Backfill ``updated_at`` for a user (mirrors ``_set_created_at``) so the
    most-recently-updated tiebreak test has a deterministic spread."""
    if backend == "duckdb":
        repo.conn.execute("UPDATE users SET updated_at = ? WHERE id = ?", [ts, user_id])
    else:
        import sqlalchemy as sa

        with repo._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE users SET updated_at = :ts WHERE id = :id"),
                {"ts": ts, "id": user_id},
            )


def test_get_by_email_prefix_matches_local_part(users_repo):
    repo, _, _ = users_repo
    _make_user(repo, id="user-bob", email="bob@example.com")
    row = repo.get_by_email_prefix("bob")
    assert row is not None
    assert row["id"] == "user-bob"


def test_get_by_email_prefix_no_match_returns_none(users_repo):
    repo, _, _ = users_repo
    _make_user(repo, id="user-alice", email="alice@example.com")
    assert repo.get_by_email_prefix("nobody") is None


def test_get_by_email_prefix_picks_most_recently_updated(users_repo):
    repo, _, backend = users_repo
    _make_user(repo, id="user-old", email="zara@old.com")
    _make_user(repo, id="user-new", email="zara@new.com")
    _set_updated_at(repo, backend, "user-old", datetime(2025, 1, 1, tzinfo=timezone.utc))
    _set_updated_at(repo, backend, "user-new", datetime(2026, 6, 1, tzinfo=timezone.utc))
    row = repo.get_by_email_prefix("zara")
    assert row is not None
    assert row["id"] == "user-new"


def test_get_by_email_prefix_underscore_is_not_a_wildcard(users_repo):
    repo, _, _ = users_repo
    _make_user(repo, id="user-x", email="alicexsmith@example.com")
    _make_user(repo, id="user-real", email="alice_smith@example.com")
    row = repo.get_by_email_prefix("alice_smith")
    assert row is not None
    assert row["id"] == "user-real"


# ---------------------------------------------------------------------------
# update_display_name — self-service name edit (issue #1036)
# ---------------------------------------------------------------------------


def test_update_display_name_persists(users_repo):
    """update_display_name stores the new name and rounds-trips on get_by_id."""
    repo, _, _ = users_repo
    _make_user(repo)
    repo.update_display_name("user-1", "Alice Smith")
    row = repo.get_by_id("user-1")
    assert row is not None
    assert row["name"] == "Alice Smith"


def test_update_display_name_empty_string_allowed(users_repo):
    """Clearing the display name to an empty string is permitted."""
    repo, _, _ = users_repo
    _make_user(repo, name="Original")
    repo.update_display_name("user-1", "")
    row = repo.get_by_id("user-1")
    assert row is not None
    assert row["name"] == ""


def test_update_display_name_overwrite_existing(users_repo):
    """A second call to update_display_name replaces the previous value."""
    repo, _, _ = users_repo
    _make_user(repo, name="Old")
    repo.update_display_name("user-1", "New")
    repo.update_display_name("user-1", "Newest")
    row = repo.get_by_id("user-1")
    assert row is not None
    assert row["name"] == "Newest"


# ---------------------------------------------------------------------------
# list_case_variant_duplicates — the reconciliation report
#
# `users` is UNIQUE on `email`, so two rows colliding case-insensitively are
# invisible to every constraint and to every list view sorted by address.
# `get_by_email_ci` silently resolves one of them; this names all of them.
# ---------------------------------------------------------------------------


def test_list_case_variant_duplicates_is_empty_without_collisions(users_repo):
    repo, _, _ = users_repo
    _make_user(repo, id="user-a", email="a@example.com")
    _make_user(repo, id="user-b", email="b@example.com")
    assert repo.list_case_variant_duplicates() == []


def test_list_case_variant_duplicates_groups_the_colliding_rows(users_repo):
    repo, _, backend = users_repo
    _make_user(repo, id="user-old", email="Dup@Example.com")
    _make_user(repo, id="user-new", email="dup@example.com")
    _make_user(repo, id="user-solo", email="solo@example.com")
    _set_created_at(repo, backend, "user-old", datetime(2025, 1, 1, tzinfo=timezone.utc))
    _set_created_at(repo, backend, "user-new", datetime(2026, 6, 1, tzinfo=timezone.utc))

    groups = repo.list_case_variant_duplicates()

    assert len(groups) == 1, "the non-colliding row must not appear"
    g = groups[0]
    assert g["email"] == "dup@example.com", "the address is reported folded"
    assert g["count"] == 2
    assert [u["id"] for u in g["users"]] == ["user-old", "user-new"]
    # Both spellings survive in the rows — the operator needs to see which is which.
    assert [u["email"] for u in g["users"]] == ["Dup@Example.com", "dup@example.com"]


def test_list_case_variant_duplicates_names_the_row_sign_in_resolves_to(users_repo):
    """The whole point of the report: `resolved_id` is the account a sign-in
    actually lands on, so an operator can see that deactivating the OTHER row
    would not have disabled the identity."""
    repo, _, backend = users_repo
    _make_user(repo, id="user-old", email="dup@example.com")
    _make_user(repo, id="user-new", email="DUP@EXAMPLE.COM")
    _set_created_at(repo, backend, "user-old", datetime(2025, 1, 1, tzinfo=timezone.utc))
    _set_created_at(repo, backend, "user-new", datetime(2026, 6, 1, tzinfo=timezone.utc))

    g = repo.list_case_variant_duplicates()[0]
    assert g["resolved_id"] == repo.get_by_email_ci("dup@example.com")["id"]
    assert g["resolved_id"] == g["users"][0]["id"], "users[0] is the resolved row"


def test_list_case_variant_duplicates_tiebreaks_like_get_by_email_ci(users_repo):
    """`created_at` is not unique. If the report ordered a tie differently from
    `get_by_email_ci`, `resolved_id` would name a row that sign-in never
    reaches — the report would be actively misleading on exactly the instances
    it exists for."""
    repo, _, backend = users_repo
    same = datetime(2025, 3, 1, tzinfo=timezone.utc)
    _make_user(repo, id="user-b", email="Tie@example.com")
    _make_user(repo, id="user-a", email="tie@example.com")
    _make_user(repo, id="user-c", email="TIE@EXAMPLE.COM")
    for uid in ("user-a", "user-b", "user-c"):
        _set_created_at(repo, backend, uid, same)

    g = repo.list_case_variant_duplicates()[0]
    assert [u["id"] for u in g["users"]] == ["user-a", "user-b", "user-c"]
    assert g["resolved_id"] == repo.get_by_email_ci("tie@example.com")["id"] == "user-a"


def test_list_case_variant_duplicates_orders_groups_by_folded_address(users_repo):
    """Two backends and two runs must agree on group order, or diffing one
    report against the next shows churn that isn't there."""
    repo, _, _ = users_repo
    _make_user(repo, id="u-z1", email="zeta@example.com")
    _make_user(repo, id="u-z2", email="Zeta@example.com")
    _make_user(repo, id="u-a1", email="alpha@example.com")
    _make_user(repo, id="u-a2", email="Alpha@example.com")

    assert [g["email"] for g in repo.list_case_variant_duplicates()] == [
        "alpha@example.com",
        "zeta@example.com",
    ]


def test_list_case_variant_duplicates_reports_deactivation_state_per_row(users_repo):
    """The failure this report exists to catch: an operator disables the
    account they can see while sign-in resolves to a still-active variant."""
    repo, _, backend = users_repo
    _make_user(repo, id="user-old", email="dup@example.com")
    _make_user(repo, id="user-new", email="Dup@Example.com")
    _set_created_at(repo, backend, "user-old", datetime(2025, 1, 1, tzinfo=timezone.utc))
    _set_created_at(repo, backend, "user-new", datetime(2026, 6, 1, tzinfo=timezone.utc))
    repo.update(
        "user-new",
        active=False,
        deactivated_at=datetime.now(timezone.utc),
        deactivated_by="admin@example.com",
    )

    g = repo.list_case_variant_duplicates()[0]
    by_id = {u["id"]: u for u in g["users"]}
    assert by_id["user-new"]["active"] is False
    assert by_id["user-old"]["active"] is True
    assert g["resolved_id"] == "user-old", "the still-active row is the one sign-in reaches"


def test_list_case_variant_duplicates_never_returns_credentials(users_repo):
    """`users` carries `password_hash`, `setup_token` and `reset_token`. A
    `SELECT *` here would put password hashes and live one-time-link tokens into
    `agnes admin duplicate-accounts --json` — output an operator reconciling
    accounts is likely to redirect to a file or paste into a ticket."""
    repo, _, _ = users_repo
    _make_user(repo, id="user-a", email="Dup@example.com")
    _make_user(repo, id="user-b", email="dup@example.com")
    repo.update(
        "user-a",
        password_hash="$argon2id$v=19$SECRETHASH",
        reset_token="LIVE-RESET-TOKEN",
        setup_token="LIVE-SETUP-TOKEN",
    )

    rows = repo.list_case_variant_duplicates()[0]["users"]

    for row in rows:
        for banned in ("password_hash", "reset_token", "setup_token", "reset_token_created", "setup_token_created"):
            assert banned not in row, f"{banned} must not reach the duplicate report"
    # The useful part survives: whether a password exists, without the hash.
    assert {r["id"]: r["has_password"] for r in rows} == {"user-a": True, "user-b": False}


def test_list_case_variant_duplicates_also_groups_whitespace_padded_rows(users_repo):
    """The sign-in doors normalize their input with strip + lower, so a stored
    ` ann@x` and a stored `ann@x` are the same address to a person and shadow
    each other in the same way a case variant does."""
    repo, _, _ = users_repo
    _make_user(repo, id="user-clean", email="pad@example.com")
    _make_user(repo, id="user-padded", email=" pad@example.com")

    groups = repo.list_case_variant_duplicates()
    assert len(groups) == 1
    assert groups[0]["email"] == "pad@example.com"
    assert {u["id"] for u in groups[0]["users"]} == {"user-clean", "user-padded"}


def test_a_padded_row_is_flagged_unreachable_and_never_named_as_resolved(users_repo):
    """`get_by_email_ci` folds case but does NOT trim the column, so a padded
    row is matched by no address anyone can type. Naming it `resolved_id`
    because it happens to be oldest would point the operator at the one row
    sign-in can never reach — the exact inversion this report exists to prevent.
    """
    repo, _, backend = users_repo
    _make_user(repo, id="user-padded", email=" pad@example.com")
    _make_user(repo, id="user-clean", email="pad@example.com")
    _set_created_at(repo, backend, "user-padded", datetime(2025, 1, 1, tzinfo=timezone.utc))
    _set_created_at(repo, backend, "user-clean", datetime(2026, 6, 1, tzinfo=timezone.utc))

    g = repo.list_case_variant_duplicates()[0]
    by_id = {u["id"]: u for u in g["users"]}
    assert by_id["user-padded"]["unreachable_by_sign_in"] is True
    assert by_id["user-clean"]["unreachable_by_sign_in"] is False
    assert g["resolved_id"] == "user-clean", "the older padded row must not win"
    # And the claim the flag encodes is true of the real lookup, checked with
    # the address a door actually passes. `normalize_email` strips before the
    # call, so the padded row is unreachable however the person typed it —
    # feeding the padded string straight in would match, but no door does that.
    from src.user_identity import normalize_email

    for typed in (" pad@example.com", "pad@example.com ", " PAD@Example.com "):
        assert repo.get_by_email_ci(normalize_email(typed))["id"] == "user-clean"


def test_a_group_of_only_padded_rows_resolves_to_nobody(users_repo):
    """Both rows unreachable means the address signs nobody in at all. `None`
    says that; picking one would invent a winner."""
    repo, _, _ = users_repo
    _make_user(repo, id="user-p1", email=" ghost@example.com")
    _make_user(repo, id="user-p2", email="ghost@example.com ")

    g = repo.list_case_variant_duplicates()[0]
    assert g["resolved_id"] is None
    assert all(u["unreachable_by_sign_in"] for u in g["users"])


def test_tab_padded_duplicates_are_reported_like_space_padded_ones(users_repo):
    """SQL `trim()` strips spaces only, on both engines. Folding in SQL would
    therefore put a tab-padded pair in two single-row groups, `HAVING COUNT(*) >
    1` would drop both, and the collision would never be reported — the
    unreachable-row class this report exists for, missing precisely when the
    padding is least visible. The fold lives in Python so all whitespace counts.
    """
    repo, _, _ = users_repo
    _make_user(repo, id="user-tab", email="\tws@example.com")
    _make_user(repo, id="user-nl", email="ws@example.com\n")
    _make_user(repo, id="user-clean", email="ws@example.com")

    groups = repo.list_case_variant_duplicates()
    assert len(groups) == 1
    g = groups[0]
    assert g["email"] == "ws@example.com"
    assert {u["id"] for u in g["users"]} == {"user-tab", "user-nl", "user-clean"}
    assert g["resolved_id"] == "user-clean", "only the unpadded row is reachable"
    assert {u["id"] for u in g["users"] if u["unreachable_by_sign_in"]} == {"user-tab", "user-nl"}


def test_grouping_survives_rows_whose_folded_addresses_interleave(users_repo):
    """The fold no longer depends on SQL ordering putting colliding rows next to
    each other, so a third address sorting between two variants cannot split a
    group in half."""
    repo, _, _ = users_repo
    _make_user(repo, id="user-a1", email="\tmid@example.com")
    _make_user(repo, id="user-b", email="mia@example.com")
    _make_user(repo, id="user-b2", email="MIA@example.com")
    _make_user(repo, id="user-a2", email="mid@example.com")

    groups = {g["email"]: g for g in repo.list_case_variant_duplicates()}
    assert set(groups) == {"mia@example.com", "mid@example.com"}
    assert {u["id"] for u in groups["mid@example.com"]["users"]} == {"user-a1", "user-a2"}
    assert {u["id"] for u in groups["mia@example.com"]["users"]} == {"user-b", "user-b2"}
