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
work-package acceptance); the live routes that use it are
``/api/admin/semantic-model/coverage*`` (F4.1), listed in both production
sweep files' ``_PG_ONLY_ROUTE_EXEMPTIONS``.

The fail-clean check is intentionally narrow: TYPED 501
(``body["error"] == "requires_postgres_backend"``), not "any 4xx/501" — a
route exempted for the wrong reason (e.g. an unrelated 403/404 that fires
before the PG-only repo is ever resolved) must not pass silently just
because its status also happens to be an error code.
"""

from __future__ import annotations

import pytest

from ._parity_sweep_util import assert_pg_only_exemptions_fail_clean, diff_statuses


class _FakeResponse:
    def __init__(self, status_code: int, json_body=None, text: str | None = None):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text if text is not None else ("" if json_body is None else str(json_body))

    def json(self):
        if self._json_body is None:
            raise ValueError("no JSON body")
        return self._json_body


class _FakeClient:
    """Stand-in for a ``TestClient`` — keyed by ``"METHOD path"`` so a test
    can script exactly what the DuckDB backend answers for an exempted
    route, without spinning up a real app."""

    def __init__(self, responses: dict[str, _FakeResponse]):
        self._responses = responses

    def get(self, path, headers=None, follow_redirects=False):
        return self._responses[f"GET {path}"]

    def request(self, method, path, json=None, headers=None, follow_redirects=False):
        return self._responses[f"{method} {path}"]


# ---------------------------------------------------------------------------
# diff_statuses — the exempt dict shape (route -> reason)
# ---------------------------------------------------------------------------


def test_diff_statuses_excludes_an_exempted_divergence():
    duck = {"GET /a": 200, "GET /b": 501}
    pg = {"GET /a": 200, "GET /b": 200}

    divergences = diff_statuses(duck, pg, exempt={"GET /b": "widgets repo is PG-only"})
    assert divergences == {}


def test_diff_statuses_still_catches_a_non_exempt_divergence():
    """The exemption is scoped to the named key only — an unrelated route
    that diverges must still be reported."""
    duck = {"GET /a": 200, "GET /b": 501, "GET /c": 200}
    pg = {"GET /a": 200, "GET /b": 200, "GET /c": 404}

    divergences = diff_statuses(duck, pg, exempt={"GET /b": "widgets repo is PG-only"})
    assert divergences == {"GET /c": (200, 404)}


def test_diff_statuses_default_exempt_is_empty():
    """No exemption passed = no behavior change from the pre-A3 diff."""
    duck = {"GET /a": 200}
    pg = {"GET /a": 404}
    assert diff_statuses(duck, pg) == {"GET /a": (200, 404)}


# ---------------------------------------------------------------------------
# assert_pg_only_exemptions_fail_clean — the TYPED fail-clean check
# ---------------------------------------------------------------------------


def test_assert_pg_only_exemptions_fail_clean_accepts_typed_501():
    client = _FakeClient({"GET /b": _FakeResponse(501, {"error": "requires_postgres_backend", "feature": "widgets"})})
    assert_pg_only_exemptions_fail_clean(client, "tok", {"GET /b": "widgets repo is PG-only"})


def test_assert_pg_only_exemptions_fail_clean_rejects_plain_4xx():
    """Negative control: a route exempted for the wrong reason — it merely
    404s (e.g. an auth/routing rejection that fires before the PG-only repo
    is ever reached) — must NOT pass just because 404 is "an error status".
    Only the specific typed 501 shape is accepted."""
    client = _FakeClient({"GET /b": _FakeResponse(404, {"detail": "not found"})})
    with pytest.raises(AssertionError, match="did not fail clean"):
        assert_pg_only_exemptions_fail_clean(client, "tok", {"GET /b": "widgets repo is PG-only"})


def test_meta_assert_pg_only_exemptions_fail_clean_rejects_501_with_wrong_body():
    """A 501 that is not actually the RequiresPostgresBackend translation
    (some unrelated "not implemented" path) must still be rejected — status
    alone is not sufficient."""
    client = _FakeClient({"GET /b": _FakeResponse(501, {"error": "something_else"})})
    with pytest.raises(AssertionError, match="did not fail clean"):
        assert_pg_only_exemptions_fail_clean(client, "tok", {"GET /b": "widgets repo is PG-only"})


def test_meta_assert_pg_only_exemptions_fail_clean_rejects_501_with_no_json_body():
    client = _FakeClient({"GET /b": _FakeResponse(501, None, text="Not Implemented")})
    with pytest.raises(AssertionError, match="did not fail clean"):
        assert_pg_only_exemptions_fail_clean(client, "tok", {"GET /b": "widgets repo is PG-only"})


def test_meta_assert_pg_only_exemptions_fail_clean_flags_a_raw_500():
    """The exemption mechanism must not let a real backend-split crash hide
    behind the exempt list — a raw 500 on the exempted route must still fail
    the check."""
    client = _FakeClient({"GET /b": _FakeResponse(500, None, text="Internal Server Error")})
    with pytest.raises(AssertionError, match="did not fail clean"):
        assert_pg_only_exemptions_fail_clean(client, "tok", {"GET /b": "widgets repo is PG-only"})


def test_meta_assert_pg_only_exemptions_fail_clean_flags_a_2xx():
    """A PG-only route that somehow returns 200 on DuckDB (no
    RequiresPostgresBackend was ever raised — the exemption is unjustified)
    must also be flagged, not just an outright crash."""
    client = _FakeClient({"GET /b": _FakeResponse(200, {"ok": True})})
    with pytest.raises(AssertionError, match="did not fail clean"):
        assert_pg_only_exemptions_fail_clean(client, "tok", {"GET /b": "widgets repo is PG-only"})


def test_assert_pg_only_exemptions_fail_clean_requires_a_non_empty_reason():
    """An exemption with no recorded reason is itself a finding — the whole
    point of moving to a dict is that a reviewer can see WHY a route is
    allowed to diverge."""
    client = _FakeClient({"GET /b": _FakeResponse(501, {"error": "requires_postgres_backend"})})
    with pytest.raises(AssertionError, match="no reason recorded"):
        assert_pg_only_exemptions_fail_clean(client, "tok", {"GET /b": ""})
    with pytest.raises(AssertionError, match="no reason recorded"):
        assert_pg_only_exemptions_fail_clean(client, "tok", {"GET /b": "   "})


def test_assert_pg_only_exemptions_fail_clean_no_exemptions_is_a_noop():
    client = _FakeClient({})
    assert_pg_only_exemptions_fail_clean(client, "tok", {})


# ---------------------------------------------------------------------------
# production wiring — every exemption in the two sweep files must carry a
# reason (the type alone doesn't guarantee a non-empty string)
# ---------------------------------------------------------------------------


def test_production_pg_only_exemptions_all_have_reasons():
    """Both sweep files' ``_PG_ONLY_ROUTE_EXEMPTIONS`` are ``dict[str, str]``,
    so an exemption can never be added without a stated reason a reviewer can
    weigh."""
    import tests.db_pg.test_get_status_parity_sweep as get_sweep
    import tests.db_pg.test_mutation_status_parity_sweep as mutation_sweep

    for module in (get_sweep, mutation_sweep):
        exemptions = module._PG_ONLY_ROUTE_EXEMPTIONS
        assert isinstance(exemptions, dict), f"{module.__name__}._PG_ONLY_ROUTE_EXEMPTIONS must be a dict"
        empty_reasons = [k for k, v in exemptions.items() if not v or not v.strip()]
        assert not empty_reasons, f"{module.__name__}: exemption(s) with no reason: {empty_reasons}"
