"""Cross-backend mutation status-parity sweep (POST/PUT/PATCH/DELETE).

Companion to the GET sweep: every parameter-free mutation route is called with
an empty JSON body on DuckDB and on Postgres (identical seed) and the HTTP
status must match. A status that differs between backends (e.g. 422 on DuckDB,
500 on Postgres) is the signature of a handler that reads state off a raw
``Depends(_get_db)`` connection during auth/validation.

Safe to run blindly: each backend client uses a per-test ephemeral database, so
a side-effecting mutation only touches a throwaway DB. Heavy / side-effecting /
binary / streaming endpoints are skipped — they're slow and an empty body
wouldn't exercise the read path meaningfully anyway.

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

_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Substrings of paths to skip: heavy/side-effecting ops + binary/stream routes.
_SKIP_SUBSTR = (
    "stream",
    "sse",
    "/events",
    "trigger",
    "/run",
    "run-",
    "warmup",
    "materialize",
    "scan",
    "refresh",
    "rebuild",
    "upgrade",
    "restart",
    "shutdown",
    "export",
    "download",
    ".zip",
    ".git",
    "throw",
)

# A3 PG-first ratchet (CLAUDE.md -> "Dual-backend discipline"): mutation
# routes backed by a Postgres-only repository are expected to diverge
# (DuckDB has no implementation) — list them here (route -> one-line reason)
# instead of letting the sweep flag them. `assert_pg_only_exemptions_fail_clean`
# below still requires each one to fail CLEAN (a typed 501) on DuckDB, not
# crash or merely return some unrelated 4xx.
#
# First PG-only route (Track C): the fact graph over Collections
# (docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md).
# `POST /api/facts/search` reaches ``facts_repo()`` with an empty (all-fields-
# optional) body, so it genuinely diverges: DuckDB -> typed 501, Postgres ->
# 200 (empty result set, nothing seeded). `POST /api/facts/neighbors` is
# deliberately NOT listed — its required `subject_id` field 422s identically
# on both backends BEFORE the repo is ever reached (Pydantic validation runs
# ahead of the handler body), so it never diverges and needs no exemption.
# `GET /api/facts/{subject_id}/claims` is a path-param route, out of scope
# for both sweeps by construction (`collect_statuses` skips any path
# containing "{") — same treatment as every other path-param endpoint here.
_PG_ONLY_ROUTE_EXEMPTIONS: dict[str, str] = {
    "POST /api/facts/search": (
        "facts_repo() is PG-only (A3 ratchet) -- DuckDB has no implementation "
        "to resolve; see src/repositories/facts_pg.py"
    ),
}


def test_mutation_status_is_identical_across_backends(tmp_path, monkeypatch, pg_engine):
    # facts.enabled defaults OFF (the router-level 404 gate would otherwise
    # make BOTH backends answer 404 identically, hiding the PG-only
    # divergence the exemption above is supposed to prove) — force it on for
    # the duration of this sweep so `assert_pg_only_exemptions_fail_clean`
    # genuinely exercises `facts_repo()` on DuckDB.
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")

    duck_client, duck_token = build_seeded_client("duckdb", tmp_path / "duck", monkeypatch, pg_engine)
    duck = collect_statuses(duck_client, duck_token, methods=_METHODS, skip_substr=_SKIP_SUBSTR)

    # The fail-clean check MUST run before the pg client is built:
    # build_seeded_client("pg", ...) sets AGNES_DB_URL, and use_pg() reads it
    # live on every *_repo() call, so after that point requests through the
    # "DuckDB" client resolve repos on Postgres and the typed-501-on-DuckDB
    # check would exercise the wrong backend. Ordering pinned by
    # test_pg_only_route_exemption_mechanism.py::
    # test_sweeps_run_fail_clean_check_before_pg_client_build.
    assert_pg_only_exemptions_fail_clean(duck_client, duck_token, _PG_ONLY_ROUTE_EXEMPTIONS)

    pg_client, pg_token = build_seeded_client("pg", tmp_path / "pg", monkeypatch, pg_engine)
    pg = collect_statuses(pg_client, pg_token, methods=_METHODS, skip_substr=_SKIP_SUBSTR)

    divergences = diff_statuses(duck, pg, exempt=_PG_ONLY_ROUTE_EXEMPTIONS)
    assert not divergences, "Mutation status diverges between DuckDB and Postgres (backend-split):\n" + "\n".join(
        f"  {k}: duck={d} pg={g}" for k, (d, g) in sorted(divergences.items())
    )
