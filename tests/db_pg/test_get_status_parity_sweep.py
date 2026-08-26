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
# implementation) — list them here instead of letting the sweep flag them.
# `assert_pg_only_exemptions_fail_clean` below still requires each one to
# fail CLEAN (4xx/501) on DuckDB, not crash. Empty until the first PG-only
# route ships (Track C); the mechanism itself is proven in
# `tests/db_pg/test_pg_only_route_exemption_mechanism.py`.
_PG_ONLY_ROUTE_EXEMPTIONS: frozenset[str] = frozenset()


def test_get_status_is_identical_across_backends(tmp_path, monkeypatch, pg_engine):
    duck_client, duck_token = build_seeded_client("duckdb", tmp_path / "duck", monkeypatch, pg_engine)
    duck = collect_statuses(duck_client, duck_token, methods={"GET"}, skip_substr=_SKIP_SUBSTR)

    pg_client, pg_token = build_seeded_client("pg", tmp_path / "pg", monkeypatch, pg_engine)
    pg = collect_statuses(pg_client, pg_token, methods={"GET"}, skip_substr=_SKIP_SUBSTR)

    assert_pg_only_exemptions_fail_clean(duck, _PG_ONLY_ROUTE_EXEMPTIONS)
    divergences = diff_statuses(duck, pg, exempt=_PG_ONLY_ROUTE_EXEMPTIONS)
    assert not divergences, "GET status diverges between DuckDB and Postgres (backend-split):\n" + "\n".join(
        f"  {k}: duck={d} pg={g}" for k, (d, g) in sorted(divergences.items())
    )
