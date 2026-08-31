"""The numbers must agree: every surface that reports one user's token usage
reports the SAME tokens, and the same cost for them.

Four surfaces read ``usage_session_summary`` for "how much did this person
spend": the analyst's own ``/api/me/stats/tokens``, an analyst SELECT over the
internal ``agnes_sessions`` table, the admin adoption drill-down, and the admin
telemetry KPI cards. Before the shared read model
(``app/services/usage_stats.py``) each computed its own aggregate, which is how
``/me/activity`` could render zeros for a user the admin dashboards showed as
the busiest on the instance.

This file is the regression net for that class of bug. It is deliberately
driven by a NON-ADMIN analyst wherever the analyst-facing surface is the
subject — Admin is a god-mode short-circuit on every access check, so an
admin-driven visibility assertion asserts nothing. Reading ``agnes_sessions``
at all now requires the seeded ``agnes-usage`` data package in the caller's
stack (Task 8), so the fixture grants it the way an admin would.

The PG-only half of the contract — per-turn rows, and a chat session whose
cache tokens are recorded live under a bare ``chat-<id>.jsonl`` key — lives in
``tests/db_pg/test_usage_surfaces_agree_pg.py``, where the backend-parametrized
fixtures are.

Plan: ``docs/superpowers/plans/2026-08-31-usage-package-per-turn-tokens.md``
Task 10; design: ``docs/superpowers/specs/2026-08-31-usage-package-and-
per-turn-tokens-design.md`` §4 + §6.
"""

from __future__ import annotations

import pytest

from connectors.internal.registry import (
    USAGE_PACKAGE_SLUG,
    ensure_internal_package_seeded,
    ensure_internal_tables_registered,
)
from src.db import get_system_db
from src.llm_pricing import cost_usd
from src.repositories import data_packages_repo

ANALYST_ID = "analyst1"
ANALYST_EMAIL = "analyst@test.com"
VIEWER_ID = "viewer1"

MODEL = "claude-sonnet-5"

#: (session_file, session_id, username, user_id, in, out, cache_read, cache_creation)
_SESSIONS = [
    (f"{ANALYST_ID}/s1.jsonl", "s-an-1", ANALYST_EMAIL, ANALYST_ID, 100, 200, 300, 40),
    (f"{ANALYST_ID}/s2.jsonl", "s-an-2", ANALYST_EMAIL, ANALYST_ID, 7, 11, 13, 17),
    (f"{VIEWER_ID}/s1.jsonl", "s-vi-1", "viewer@test.com", VIEWER_ID, 1000, 2000, 3000, 4000),
]

#: What every surface must report for the analyst — the sum of their two
#: sessions and nothing of the viewer's.
EXPECTED = {"input": 107, "output": 211, "cache_read": 313, "cache_creation": 57}
EXPECTED_TOTAL = sum(EXPECTED.values())
EXPECTED_COST = cost_usd(
    MODEL,
    input_tokens=EXPECTED["input"],
    output_tokens=EXPECTED["output"],
    cache_read_tokens=EXPECTED["cache_read"],
    cache_creation_tokens=EXPECTED["cache_creation"],
)

#: The API rounds USD to the microdollar for transport (a template must not be
#: handed 0.30000000000000004), so comparisons against the raw price are exact
#: only to that place.
USD = 1e-6


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_sessions(conn) -> None:
    """Two analyst sessions + one belonging to somebody else.

    ``started_at`` is an hour ago so every window under test (24h upwards)
    contains all of them; a NULL would drop out of the windowed reads and make
    the comparison vacuous.
    """
    for sf, sid, username, uid, tin, tout, tcr, tcc in _SESSIONS:
        conn.execute(
            "INSERT INTO usage_session_summary "
            "(session_file, session_id, username, user_id, primary_model, started_at, "
            " active_seconds, wall_seconds, user_messages, tool_calls, tool_errors, "
            " input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, "
            " processor_version) "
            "VALUES (?, ?, ?, ?, ?, current_timestamp - INTERVAL 1 HOUR, "
            "        60, 60, 1, 1, 0, ?, ?, ?, ?, 1)",
            [sf, sid, username, uid, MODEL, tin, tout, tcr, tcc],
        )


def _grant_usage_package(conn, user_id: str, *, group_name: str = "usage-agree-testers") -> None:
    """Put the seeded ``agnes-usage`` package in *user_id*'s stack — what an
    admin does at /admin/access, and the precondition for the analyst reading
    ``agnes_sessions`` at all since the tables became stack-gated."""
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    pkg = data_packages_repo().get_by_slug(USAGE_PACKAGE_SLUG)
    assert pkg is not None, "the agnes-usage package must be seeded first"

    groups = UserGroupsRepository(conn)
    grp = groups.get_by_name(group_name) or groups.create(name=group_name, description="test", created_by="test")
    members = UserGroupMembersRepository(conn)
    if not members.has_membership(user_id, grp["id"]):
        members.add_member(user_id, grp["id"], source="admin", added_by="test")
    grants = ResourceGrantsRepository(conn)
    if not grants.has_grant([grp["id"]], "data_package", pkg["id"]):
        grants.create(
            group_id=grp["id"],
            resource_type="data_package",
            resource_id=pkg["id"],
            assigned_by="test",
            requirement="required",
        )


@pytest.fixture
def agree(seeded_app):
    """Seeded app + the boot-time internal seeding replayed + usage rows.

    ``seeded_app``'s TestClient never runs the ASGI lifespan, so the seeding
    ``app/main.py`` performs at boot is replayed explicitly here.
    """
    newly_registered = ensure_internal_tables_registered()
    ensure_internal_package_seeded(newly_registered=newly_registered)
    conn = get_system_db()
    try:
        _seed_sessions(conn)
        _grant_usage_package(conn, ANALYST_ID)
    finally:
        conn.close()
    return seeded_app


# ---------------------------------------------------------------------------
# The four surfaces
# ---------------------------------------------------------------------------


def _me_totals(agree) -> dict:
    resp = agree["client"].get("/api/me/stats/tokens?days=30", headers=_auth(agree["analyst_token"]))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _query_sums(agree) -> dict:
    resp = agree["client"].post(
        "/api/query",
        json={
            "sql": "SELECT SUM(input_tokens), SUM(output_tokens), "
            "SUM(cache_read_tokens), SUM(cache_creation_tokens) FROM agnes_sessions"
        },
        headers=_auth(agree["analyst_token"]),
    )
    assert resp.status_code == 200, resp.text
    row = resp.json()["rows"][0]
    return dict(zip(("input", "output", "cache_read", "cache_creation"), (int(v) for v in row)))


def _adoption_user(agree, window: str = "30d") -> dict:
    resp = agree["client"].get(
        f"/api/admin/adoption/users/{ANALYST_ID}/kpis?window={window}",
        headers=_auth(agree["admin_token"]),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _telemetry_kpis(agree, *, since_minutes: int = 43200, username: str | None = ANALYST_EMAIL) -> dict:
    url = f"/api/admin/telemetry/kpis?since_minutes={since_minutes}"
    if username:
        url += f"&username={username}"
    resp = agree["client"].get(url, headers=_auth(agree["admin_token"]))
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_me_stats_reports_the_seeded_totals(agree):
    """Baseline: the analyst's own view is not zero and is not everybody's."""
    totals = _me_totals(agree)["totals"]
    for key, want in EXPECTED.items():
        assert totals[key] == want, key
    assert totals["total"] == EXPECTED_TOTAL
    assert totals["sessions"] == 2


def test_self_view_and_internal_table_agree(agree):
    """``/api/me/stats`` and ``SELECT ... FROM agnes_sessions`` are two reads of
    the same rows through two different code paths (repo aggregate vs the
    RBAC-scoped CTE wrapper). They must not disagree."""
    totals = _me_totals(agree)["totals"]
    sums = _query_sums(agree)
    for key in EXPECTED:
        assert sums[key] == totals[key], key


def test_admin_slice_agrees_with_the_users_own_view(agree):
    """The admin drill-down for one user reports that user's tokens — the same
    total the user sees, never the instance's."""
    totals = _me_totals(agree)["totals"]
    kpis = _adoption_user(agree)
    assert kpis["tokens"] == totals["total"] == EXPECTED_TOTAL


def test_admin_instance_view_is_the_sum_of_its_slices(agree):
    """Guards the direction the per-user assertion cannot: an endpoint that
    ignored its user filter would pass "slice == own view" only if the slice
    really is a slice."""
    resp = agree["client"].get("/api/admin/adoption/kpis?window=30d", headers=_auth(agree["admin_token"]))
    assert resp.status_code == 200, resp.text
    everyone = sum(sum(row[4:]) for row in _SESSIONS)
    assert resp.json()["tokens"] == everyone
    assert everyone > EXPECTED_TOTAL


# ---------------------------------------------------------------------------
# Cost — the same tokens, priced once
# ---------------------------------------------------------------------------


def test_cost_is_the_price_of_the_reported_tokens(agree):
    body = _me_totals(agree)
    assert body["totals"]["cost_usd"] == pytest.approx(EXPECTED_COST, abs=USD)


def test_cost_column_is_present_per_model(agree):
    body = _me_totals(agree)
    rows = {m["model"]: m for m in body["by_model"]}
    assert set(rows) == {MODEL}
    assert rows[MODEL]["cost_usd"] == pytest.approx(EXPECTED_COST, abs=USD)
    # The column adds up to the headline figure — a per-row cost that does not
    # sum to the total shown above it is the bug this asserts against.
    assert sum(m["cost_usd"] for m in body["by_model"]) == pytest.approx(body["totals"]["cost_usd"], rel=1e-9)


def test_every_surface_prices_the_user_identically(agree):
    """The point of the shared read model: one cost per (user, window), no
    matter which dashboard asks."""
    me = _me_totals(agree)["totals"]["cost_usd"]
    adoption = _adoption_user(agree)["cost_usd"]
    telemetry = _telemetry_kpis(agree)["cost_usd"]
    assert me == pytest.approx(EXPECTED_COST, abs=USD)
    assert adoption == pytest.approx(me, abs=USD)
    assert telemetry == pytest.approx(me, abs=USD)


def test_unfiltered_telemetry_cost_covers_the_instance(agree):
    """Without a username filter the KPI card prices the whole instance, so it
    must be strictly more than one analyst's slice (or unavailable — the
    instance-wide breakdown needs the Postgres-only per-turn table)."""
    body = _telemetry_kpis(agree, username=None)
    assert "cost_usd" in body
    if body["cost_usd"] is not None:
        assert body["cost_usd"] > EXPECTED_COST


def test_existing_kpi_keys_are_untouched(agree):
    """Cost is an ADDITION. Every key the dashboards already read must survive
    — this is what makes the wiring mechanical rather than a rewrite."""
    telemetry = _telemetry_kpis(agree)
    assert {
        "window_minutes",
        "events_total",
        "distinct_users",
        "distinct_tools",
        "errors",
        "error_rate",
    } <= set(telemetry)

    adoption = _adoption_user(agree)
    assert {
        "window",
        "user_id",
        "username",
        "email",
        "active_hours",
        "wall_hours",
        "sessions",
        "prompts",
        "tokens",
        "tool_calls",
        "tool_errors",
        "models",
    } <= set(adoption)

    tokens = _me_totals(agree)
    assert {"days", "daily", "by_model", "top_sessions", "totals"} == set(tokens)
    assert {"input", "output", "cache_read", "cache_creation", "total", "sessions"} <= set(tokens["totals"])


# ---------------------------------------------------------------------------
# The page itself — a cost the API computes but the dashboard never renders is
# not a feature.
# ---------------------------------------------------------------------------


def _activity_page(agree, token: str) -> str:
    resp = agree["client"].get("/me/activity", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    return resp.text


def test_activity_page_renders_a_cost_column(agree):
    html = _activity_page(agree, agree["analyst_token"])
    assert "data-tok-cost" in html  # the headline figure on the By-model card
    assert html.count(">Cost</th>") >= 1


def test_empty_state_says_nothing_was_uploaded_here(agree):
    """A user with no sessions gets an explanation, not a blank tab. The
    wording matters: "zero tokens" reads as a broken dashboard, while "nothing
    was uploaded to THIS server" points at the actual cause (a workspace
    pushing somewhere else)."""
    html = _activity_page(agree, agree["km_admin_token"])
    assert "No sessions have been uploaded to this server yet." in html


def test_empty_state_names_the_last_upload_when_there_was_one(agree):
    """Uploaded but not yet processed is a different story from never uploaded,
    and the audit trail is the only place that knows which one it is."""
    from src.repositories import audit_repo

    audit_repo().log(
        user_id="km_admin1",
        action="session.upload",
        params={"filename": "s.jsonl"},
        result="success",
    )
    html = _activity_page(agree, agree["km_admin_token"])
    assert "No sessions have been uploaded to this server yet." not in html
    assert "most recent" in html and "upload" in html
    assert "UTC" in html
