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
work-package acceptance), driven with fakes so they stay independent of
whichever route is the current example. The first live production use is
``POST /api/admin/semantic-auto-draft-sweep`` (semantic-phase5 wave 2) in
``tests/db_pg/test_mutation_status_parity_sweep.py``.

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


def test_sweeps_run_fail_clean_check_before_pg_client_build():
    """The fail-clean check must run while DuckDB is still the live backend.

    ``build_seeded_client("pg", ...)`` sets ``AGNES_DB_URL``, and
    ``src.repositories.use_pg`` reads it live on every ``*_repo()`` call — a
    ``TestClient`` does not pin the backend it was built under. So once the pg
    client exists, requests through the "DuckDB" client resolve repos on
    Postgres and the typed-501-on-DuckDB check silently exercises the wrong
    backend (the PG-only repo then EXISTS, no ``RequiresPostgresBackend`` is
    raised, and the check fails for the wrong reason — or worse, a handler
    that swallows the error passes). Pin the call order in both sweeps.
    """
    import inspect

    import tests.db_pg.test_get_status_parity_sweep as get_sweep
    import tests.db_pg.test_mutation_status_parity_sweep as mutation_sweep

    for fn in (
        get_sweep.test_get_status_is_identical_across_backends,
        mutation_sweep.test_mutation_status_is_identical_across_backends,
    ):
        # Drop comment lines first — the sweeps carry an explanatory comment
        # that itself names build_seeded_client("pg", ...), which would
        # otherwise shadow the real call site.
        source = "\n".join(line for line in inspect.getsource(fn).splitlines() if not line.lstrip().startswith("#"))
        check_at = source.index("assert_pg_only_exemptions_fail_clean(")
        pg_build_at = source.index('build_seeded_client("pg"')
        assert check_at < pg_build_at, (
            f"{fn.__module__}.{fn.__name__}: assert_pg_only_exemptions_fail_clean "
            "must be called BEFORE build_seeded_client('pg', ...) — the pg build "
            "sets AGNES_DB_URL and use_pg() reads it live per *_repo() call, so "
            "afterwards the DuckDB client resolves repos on Postgres and the "
            "typed-501-on-DuckDB check never exercises DuckDB"
        )


def test_post_reload_raise_still_translates_to_typed_501(tmp_path, monkeypatch):
    """``build_seeded_client`` reloads ``src.repositories``, rebinding
    ``RequiresPostgresBackend`` to a NEW class object, while ``app.main``
    registered its 501 handler against the class it imported at its own import
    time — Starlette resolves handlers via the raised exception's MRO, which
    never contains the pre-reload class, so without the harness re-registration
    (``_reregister_requires_pg_handler``) the raise would fall through to the
    catch-all 500. Prove end-to-end that a route raising the CURRENT
    (post-reload) class on the DuckDB-built client answers the exact typed 501
    that ``assert_pg_only_exemptions_fail_clean`` demands."""
    import app.main  # bind app.main's class reference BEFORE the reload below  # noqa: F401

    from ._parity_sweep_util import build_seeded_client

    client, token = build_seeded_client("duckdb", tmp_path, monkeypatch, None)

    import src.repositories

    exc_cls = src.repositories.RequiresPostgresBackend  # the post-reload class
    assert exc_cls is not app.main.RequiresPostgresBackend, (
        "precondition: the reload must have rebound RequiresPostgresBackend, otherwise this test exercises nothing"
    )

    from fastapi import APIRouter

    probe_router = APIRouter()

    @probe_router.get("/_test/pg-only-probe")
    def _probe():
        raise exc_cls("widgets")

    # Insert FIRST, not append: the web router ends with a catch-all route, so
    # an appended route would never match (404) and prove nothing.
    client.app.router.routes.insert(0, probe_router.routes[0])

    r = client.get("/_test/pg-only-probe", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 501, f"expected the typed 501 translation, got {r.status_code}"
    body = r.json()
    assert body["error"] == "requires_postgres_backend"
    assert body["feature"] == "widgets"


def test_production_pg_only_exemptions_all_have_reasons():
    """Both sweep files' ``_PG_ONLY_ROUTE_EXEMPTIONS`` must be dicts, and
    every entry (the mutation sweep now carries one — the semantic-layer
    auto-draft sweep, semantic-phase5 wave 2) must carry a non-empty
    reason, so an exemption can never be added silently."""
    import tests.db_pg.test_get_status_parity_sweep as get_sweep
    import tests.db_pg.test_mutation_status_parity_sweep as mutation_sweep

    for module in (get_sweep, mutation_sweep):
        exemptions = module._PG_ONLY_ROUTE_EXEMPTIONS
        assert isinstance(exemptions, dict), f"{module.__name__}._PG_ONLY_ROUTE_EXEMPTIONS must be a dict"
        empty_reasons = [k for k, v in exemptions.items() if not v or not v.strip()]
        assert not empty_reasons, f"{module.__name__}: exemption(s) with no reason: {empty_reasons}"
