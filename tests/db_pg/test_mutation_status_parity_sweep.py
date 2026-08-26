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
# crash or merely return some unrelated 4xx. The mechanism itself is proven in
# `tests/db_pg/test_pg_only_route_exemption_mechanism.py`.
_PG_ONLY_ROUTE_EXEMPTIONS: dict[str, str] = {
    "POST /api/admin/semantic-model/coverage/tags": (
        "coverage tagging writes `resource_source_tags`, a PG-only table (F4.1)"
    ),
    "POST /api/semantic-feedback": "filing feedback writes `semantic_feedback`, a PG-only table (F4.5)",
    "POST /api/admin/semantic-layer/mutes": ("muting a check writes `semantic_health_mutes`, a PG-only table (F4.3)"),
}


def test_mutation_status_is_identical_across_backends(tmp_path, monkeypatch, pg_engine):
    duck_client, duck_token = build_seeded_client("duckdb", tmp_path / "duck", monkeypatch, pg_engine)
    duck = collect_statuses(duck_client, duck_token, methods=_METHODS, skip_substr=_SKIP_SUBSTR)

    # WHILE DuckDB IS STILL THE ACTIVE BACKEND — see the identical note in
    # test_get_status_parity_sweep.py: the PG phase below flips AGNES_DB_URL
    # process-wide, after which `duck_client` is no longer a DuckDB client.
    assert_pg_only_exemptions_fail_clean(duck_client, duck_token, _PG_ONLY_ROUTE_EXEMPTIONS)

    pg_client, pg_token = build_seeded_client("pg", tmp_path / "pg", monkeypatch, pg_engine)
    pg = collect_statuses(pg_client, pg_token, methods=_METHODS, skip_substr=_SKIP_SUBSTR)

    divergences = diff_statuses(duck, pg, exempt=_PG_ONLY_ROUTE_EXEMPTIONS)
    assert not divergences, "Mutation status diverges between DuckDB and Postgres (backend-split):\n" + "\n".join(
        f"  {k}: duck={d} pg={g}" for k, (d, g) in sorted(divergences.items())
    )
