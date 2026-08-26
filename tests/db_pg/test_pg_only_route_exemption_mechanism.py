"""A3 PG-first ratchet — proves the dynamic parity-sweep exemption mechanism.

``tests/db_pg/test_get_status_parity_sweep.py`` and
``test_mutation_status_parity_sweep.py`` diff every route's HTTP status
between the DuckDB and Postgres backends and fail on any divergence — the
signature of a handler reading state off the wrong backend. A route backed by
a genuinely Postgres-only repository (post-A3) is EXPECTED to diverge (DuckDB
resolves no repo at all), so it needs a documented exemption rather than
either a false-positive failure or a silent blind spot that could hide a real
500.

``_parity_sweep_util.diff_statuses(..., exempt=...)`` +
``assert_pg_only_exemptions_fail_clean`` are that mechanism. These are unit
tests against the mechanism itself (the "error path directly", per the A3
work-package acceptance) — no live route exists yet that uses it (the first
lands with Track C); the two production sweep files already wire an empty
exemption set ready for that.
"""

from __future__ import annotations

import pytest

from ._parity_sweep_util import assert_pg_only_exemptions_fail_clean, diff_statuses


def test_diff_statuses_excludes_an_exempted_divergence():
    duck = {"GET /a": 200, "GET /b": 501}
    pg = {"GET /a": 200, "GET /b": 200}

    divergences = diff_statuses(duck, pg, exempt={"GET /b"})
    assert divergences == {}


def test_diff_statuses_still_catches_a_non_exempt_divergence():
    """The exemption is scoped to the named key only — an unrelated route
    that diverges must still be reported."""
    duck = {"GET /a": 200, "GET /b": 501, "GET /c": 200}
    pg = {"GET /a": 200, "GET /b": 200, "GET /c": 404}

    divergences = diff_statuses(duck, pg, exempt={"GET /b"})
    assert divergences == {"GET /c": (200, 404)}


def test_diff_statuses_default_exempt_is_empty():
    """No exemption passed = no behavior change from the pre-A3 diff."""
    duck = {"GET /a": 200}
    pg = {"GET /a": 404}
    assert diff_statuses(duck, pg) == {"GET /a": (200, 404)}


def test_assert_pg_only_exemptions_fail_clean_accepts_501():
    assert_pg_only_exemptions_fail_clean({"GET /b": 501}, {"GET /b"})


def test_assert_pg_only_exemptions_fail_clean_accepts_4xx():
    assert_pg_only_exemptions_fail_clean({"GET /b": 404}, {"GET /b"})


def test_assert_pg_only_exemptions_fail_clean_ignores_missing_routes():
    """A route the sweep never hit (e.g. filtered by skip_substr) is not an
    assertion failure — it simply never ran."""
    assert_pg_only_exemptions_fail_clean({}, {"GET /never-ran"})


def test_meta_assert_pg_only_exemptions_fail_clean_flags_a_raw_500():
    """Negative control: the exemption mechanism must not let a real
    backend-split crash hide behind the exempt list — a 500 on the exempted
    route must still fail the check."""
    with pytest.raises(AssertionError, match="did not fail clean"):
        assert_pg_only_exemptions_fail_clean({"GET /b": 500}, {"GET /b"})


def test_meta_assert_pg_only_exemptions_fail_clean_flags_a_2xx():
    """A PG-only route that somehow returns 200 on DuckDB (no
    RequiresPostgresBackend was ever raised — the exemption is unjustified)
    must also be flagged, not just an outright crash."""
    with pytest.raises(AssertionError, match="did not fail clean"):
        assert_pg_only_exemptions_fail_clean({"GET /b": 200}, {"GET /b"})
