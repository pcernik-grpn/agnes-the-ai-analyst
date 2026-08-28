"""Cross-backend GET status-parity sweep.

Every parameter-free GET route is hit with a seeded admin token on DuckDB and on
Postgres (identical seed) and the HTTP status must match. A status that differs
between backends (e.g. 200 vs 302, or 200 vs 500) is the signature of a handler
that reads state off a raw ``Depends(_get_db)`` connection — the backend-split
class the static ``test_backend_split_guard.py`` ratchet can't see.

This found ``/first-time-setup`` returning 200 on PG vs 302 on DuckDB (the
wizard counted users off the always-DuckDB connection); that specific fix has a
dedicated regression test in ``test_first_time_setup_parity.py``.

Single test, both backends collected in-process — see ``_parity_sweep_util`` for
why (the older parametrized-fixture + module-dict pattern was dead under
``pytest -n auto``).
"""

from __future__ import annotations

from ._parity_sweep_util import (
    assert_pg_only_exemptions_fail_clean,
    build_seeded_client,
    collect_statuses,
    diff_statuses,
)

# Intentional-throw debug routes + streaming/SSE (would hang) — never swept.
_SKIP_SUBSTR = ("throw", "stream", "sse", "/events")

# A3 PG-first ratchet (CLAUDE.md -> "Dual-backend discipline"): GET routes
# backed by a Postgres-only repository are expected to diverge (DuckDB has no
# implementation) — list them here (route -> one-line reason) instead of
# letting the sweep flag them. `assert_pg_only_exemptions_fail_clean` below
# still requires each one to fail CLEAN (a typed 501) on DuckDB, not crash or
# merely return some unrelated 4xx. Empty until the first PG-only route ships
# (Track C); the mechanism itself is proven in
# `tests/db_pg/test_pg_only_route_exemption_mechanism.py`.
#
# The fact graph over Collections read surface (build order steps 2+3) has
# no GET entry here: its one GET route, `/api/facts/{subject_id}/claims`,
# carries a path param and is out of scope for THIS sweep by construction
# (`collect_statuses` skips any path containing "{") — same treatment as
# every other path-param endpoint in the codebase, none of which are
# exempted here either. `search`/`neighbors` are POST, so they live in the
# mutation sweep instead.
#
# Build order step 4 (write path) DOES add one: `GET /api/facts/corrections`
# (the producer export, spec §7.4) is genuinely parameter-free and reaches
# `facts_repo().list_wrong_corrections()` — DuckDB -> typed 501, Postgres ->
# 200 (empty list, nothing seeded). Requires the flag forced on below (this
# sweep leaves it off by default, unlike the mutation sweep) so the route
# is actually reached rather than 404ing identically on both backends.
#
# `GET /api/facts/ingest-runs` (spec §13.2 source card) is the same shape:
# parameter-free (its `limit` is a query param with a default, so the
# route matches with no path template), reaches
# `facts_ingest_runs_repo().list_recent(...)` — a SEPARATE PG-only repo
# from `facts_repo()` (see src/repositories/facts_ingest_runs_pg.py) so it
# needs its own exemption entry even though the reason reads similarly.
_PG_ONLY_ROUTE_EXEMPTIONS: dict[str, str] = {
    "GET /api/facts/corrections": (
        "facts_repo() is PG-only (A3 ratchet) -- DuckDB has no implementation "
        "to resolve; see src/repositories/facts_pg.py"
    ),
    # Ontology builder draft persistence (spec §13.2) — genuinely
    # parameter-free and reaches ontology_drafts_repo() -- DuckDB -> typed
    # 501, Postgres -> 200 (empty list, nothing seeded).
    "GET /api/admin/ontology/drafts": (
        "ontology_drafts_repo() is PG-only (A3 ratchet) -- DuckDB has no "
        "implementation to resolve; see src/repositories/ontology_drafts_pg.py"
    ),
    "GET /api/facts/ingest-runs": (
        "facts_ingest_runs_repo() is PG-only (A3 ratchet) -- DuckDB has no "
        "implementation to resolve; see src/repositories/facts_ingest_runs_pg.py"
    ),
    # External SSO login config (design 2026-08-28) — both parameter-free
    # GETs reach a PG-only repo before any other validation.
    "GET /api/admin/sso/config": (
        "sso_config_repo() is PG-only (A3 ratchet) -- DuckDB has no "
        "implementation to resolve; see src/repositories/sso_config_pg.py"
    ),
    "GET /api/admin/sso/identities": (
        "user_external_identities_repo() is PG-only (A3 ratchet) -- DuckDB has "
        "no implementation to resolve; see src/repositories/user_external_identities_pg.py"
    ),
    "GET /api/me/external-identity": (
        "user_external_identities_repo() is PG-only (A3 ratchet) -- DuckDB has "
        "no implementation to resolve; see src/repositories/user_external_identities_pg.py"
    ),
    # Agent-sharing approval queue (Track C6) — genuinely parameter-free
    # (`status`/`limit`/`skip` are query params with defaults) and reaches
    # share_requests_repo() -- DuckDB -> typed 501, Postgres -> 200 (empty
    # list, nothing seeded). This is the admin QUEUE surface only; sharing
    # ITSELF (`PUT /api/sharing/agent/{id}`) falls back to an instant grant
    # on DuckDB instead of a 501 — see app/services/library_sharing.py.
    "GET /api/admin/share-requests": (
        "share_requests_repo() is PG-only (A3 ratchet) -- DuckDB has no "
        "implementation to resolve; see src/repositories/share_requests_pg.py"
    ),
}


def test_get_status_is_identical_across_backends(tmp_path, monkeypatch, pg_engine):
    # facts.enabled defaults OFF (the router-level 404 gate would otherwise
    # make BOTH backends answer 404 identically, hiding the PG-only
    # divergence the exemption above is supposed to prove) — force it on for
    # the duration of this sweep, mirroring test_mutation_status_parity_sweep.py.
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")

    duck_client, duck_token = build_seeded_client("duckdb", tmp_path / "duck", monkeypatch, pg_engine)
    duck = collect_statuses(duck_client, duck_token, methods={"GET"}, skip_substr=_SKIP_SUBSTR)

    # The fail-clean check MUST run before the pg client is built:
    # build_seeded_client("pg", ...) sets AGNES_DB_URL, and use_pg() reads it
    # live on every *_repo() call, so after that point requests through the
    # "DuckDB" client resolve repos on Postgres and the typed-501-on-DuckDB
    # check would exercise the wrong backend. Ordering pinned by
    # test_pg_only_route_exemption_mechanism.py::
    # test_sweeps_run_fail_clean_check_before_pg_client_build.
    assert_pg_only_exemptions_fail_clean(duck_client, duck_token, _PG_ONLY_ROUTE_EXEMPTIONS)

    pg_client, pg_token = build_seeded_client("pg", tmp_path / "pg", monkeypatch, pg_engine)
    pg = collect_statuses(pg_client, pg_token, methods={"GET"}, skip_substr=_SKIP_SUBSTR)

    divergences = diff_statuses(duck, pg, exempt=_PG_ONLY_ROUTE_EXEMPTIONS)
    assert not divergences, "GET status diverges between DuckDB and Postgres (backend-split):\n" + "\n".join(
        f"  {k}: duck={d} pg={g}" for k, (d, g) in sorted(divergences.items())
    )
