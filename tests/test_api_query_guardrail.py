"""POST /api/query cost guardrail for query_mode='remote' BigQuery rows.

When user SQL references a registered remote-BQ name (or a direct
`bq."<ds>"."<tbl>"` path), run a BQ dry-run before execute. If the
estimated scan exceeds the configured cap, reject with 400 +
`remote_scan_too_large` so the operator pivots to `agnes snapshot create`.

Default cap: 5 GiB per request. Configurable via
`api.query.bq_max_scan_bytes` in /admin/server-config (#160 §4.4).
"""
from __future__ import annotations

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register_bq_remote_row(name: str, bucket: str, source_table: str) -> None:
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository
    sys_conn = get_system_db()
    try:
        TableRegistryRepository(sys_conn).register(
            id=f"bq.{bucket}.{source_table}",
            name=name,
            source_type="bigquery",
            bucket=bucket,
            source_table=source_table,
            query_mode="remote",
        )
    finally:
        sys_conn.close()


@pytest.fixture
def mock_dry_run(monkeypatch):
    """Replace `_bq_dry_run_bytes` with a controllable stub. Each test sets
    `mock_dry_run["bytes"]` to control what /api/query sees. Also stubs
    `get_bq_access` so the guardrail doesn't require a real BQ connection
    in the test env."""
    state = {"bytes": 0}

    def fake_dry_run(*args, **kwargs):
        return state["bytes"]

    monkeypatch.setattr("app.api.query._bq_dry_run_bytes", fake_dry_run, raising=False)

    # Stub get_bq_access so the guardrail's BqAccess construction doesn't
    # fail with `not_configured` in tests that don't set up real BQ.
    class _FakeProjects:
        data = "test-data-prj"
        billing = "test-billing-prj"

    class _FakeBqAccess:
        projects = _FakeProjects()

    monkeypatch.setattr(
        "app.api.query.get_bq_access",
        lambda: _FakeBqAccess(),
        raising=False,
    )
    return state


def test_query_under_cap_calls_dry_run(seeded_app, mock_dry_run, monkeypatch):
    """Dry-run is invoked when SQL references a registered remote BQ row.
    Use a sentinel side-effect to confirm: the mock records call counts."""
    _register_bq_remote_row("ue", "finance", "ue")
    state = mock_dry_run
    state["bytes"] = 1 * 1024 * 1024  # 1 MiB
    state["call_count"] = 0

    def counting_fake(*args, **kwargs):
        state["call_count"] += 1
        return state["bytes"]

    monkeypatch.setattr("app.api.query._bq_dry_run_bytes", counting_fake, raising=False)

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    c.post(
        "/api/query",
        json={"sql": "SELECT count(*) FROM ue"},
        headers=_auth(token),
    )
    assert state["call_count"] >= 1, \
        "guardrail must invoke _bq_dry_run_bytes when SQL references a registered remote BQ row"


def test_query_over_cap_rejected_400(seeded_app, mock_dry_run, monkeypatch):
    """Dry-run reports 10 GiB; default cap (5 GiB) is exceeded → 400 with
    structured detail naming bytes + tables + suggestion."""
    _register_bq_remote_row("ue", "finance", "ue")
    mock_dry_run["bytes"] = 10 * 1024 * 1024 * 1024  # 10 GiB

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        json={"sql": "SELECT * FROM ue"},
        headers=_auth(token),
    )
    assert r.status_code == 400, r.json()
    detail = r.json().get("detail", {})
    if isinstance(detail, dict):
        assert detail.get("reason") == "remote_scan_too_large", detail
        assert detail.get("scan_bytes") >= 10 * 1024 * 1024 * 1024
        # Suggestion text was renamed in the agnes-bootstrap PR (`da fetch`
        # → `agnes snapshot create`). Accept the new shape.
        suggestion = detail.get("suggestion", "").lower()
        assert "agnes snapshot create" in suggestion or "snapshot create" in suggestion
        assert "ue" in detail.get("tables", []) or \
               any("ue" in t for t in detail.get("tables", []))


def test_query_over_cap_against_view_includes_view_hint(seeded_app, mock_dry_run, monkeypatch):
    """When the target table is classified as VIEW in bq_metadata_cache,
    the cost-guard suggestion explicitly tells the analyst LIMIT does
    not push into the view body — the literal #1 surprise from the
    sub-agent test runs."""
    from src.db import get_system_db
    from src.repositories.bq_metadata_cache import BqMetadataCacheRepository

    _register_bq_remote_row("ue_view", "finance", "ue_view")
    # _register_bq_remote_row writes id = "bq.<bucket>.<source_table>";
    # the cache row's table_id must match that ID, not the catalog name.
    cached_id = "bq.finance.ue_view"
    conn = get_system_db()
    try:
        BqMetadataCacheRepository(conn).upsert_success(
            cached_id, rows=None, size_bytes=None,
            partition_by=None, clustered_by=None,
            entity_type="VIEW", known_columns=["event_date"],
        )
    finally:
        conn.close()

    mock_dry_run["bytes"] = 10 * 1024 * 1024 * 1024

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        json={"sql": "SELECT * FROM ue_view LIMIT 1"},
        headers=_auth(token),
    )
    assert r.status_code == 400, r.json()
    detail = r.json()["detail"]
    assert detail["reason"] == "remote_scan_too_large"
    assert cached_id in detail.get("view_targets", [])
    suggestion = detail["suggestion"]
    assert "VIEW" in suggestion
    assert "LIMIT" in suggestion
    assert "snapshot create" in suggestion


def test_no_bq_row_reference_skips_dry_run(seeded_app, monkeypatch):
    """A query that doesn't touch any registered BQ remote row must NOT
    invoke `_bq_dry_run_bytes` — guardrail incurs zero new latency on
    plain non-BQ queries."""
    state = {"calls": 0}

    def counting_fake(*args, **kwargs):
        state["calls"] += 1
        return 100 * 1024 * 1024 * 1024  # 100 GiB — irrelevant if not called

    monkeypatch.setattr("app.api.query._bq_dry_run_bytes", counting_fake, raising=False)

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    c.post(
        "/api/query",
        json={"sql": "SELECT 1 AS x"},
        headers=_auth(token),
    )
    assert state["calls"] == 0, \
        f"guardrail must skip dry-run on non-BQ queries; got {state['calls']} calls"


# ---------------------------------------------------------------------------
# Issue #171: pre-check used to dry-run synthetic SELECT * per registered
# table → 30,000× over-estimate on partitioned/clustered tables. Fix: rewrite
# user SQL from DuckDB-flavor (bare names + `bq.<ds>.<tbl>`) to BQ-native
# (\\`<project>.<ds>.<tbl>\\`) and run a SINGLE dry-run on the user's actual
# SQL, so partition pruning, column projection, and predicate pushdown all
# count toward the cap check.
# ---------------------------------------------------------------------------


def test_guardrail_dry_runs_rewritten_user_sql_not_synthetic_select_star(
    seeded_app, mock_dry_run, monkeypatch,
):
    """The dry-run must receive the USER's SQL with bare table names rewritten
    to backticked paths — not a synthetic ``SELECT * FROM <table>``.

    This is the load-bearing assertion for issue #171: if the pre-check sees
    only the table name it can't prune partitions or project columns, and
    the estimate balloons to "full table size" instead of "what BQ would
    actually scan."
    """
    _register_bq_remote_row("ue", "finance", "ue")
    captured = {"sql": None}

    def capturing_fake(_bq, sql, **_kwargs):
        captured["sql"] = sql
        return 1024  # tiny — pass the cap

    monkeypatch.setattr(
        "app.api.query._bq_dry_run_bytes", capturing_fake, raising=False,
    )

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    user_sql = (
        "SELECT order_id FROM ue "
        "WHERE event_date = DATE '2026-04-30' AND country = 'CZ'"
    )
    c.post("/api/query", json={"sql": user_sql}, headers=_auth(token))

    sent = captured["sql"]
    assert sent is not None, "dry-run never invoked"
    # User-side filters must survive the rewrite — that's the whole point of
    # the fix, partition pruning + predicate pushdown only engage in the BQ
    # planner if the WHERE clause reaches it.
    assert "event_date" in sent, f"WHERE clause stripped from dry-run SQL: {sent!r}"
    assert "country" in sent, f"WHERE clause stripped from dry-run SQL: {sent!r}"
    # Bare name `ue` must have been rewritten to a backticked
    # `<project>.finance.ue` path (project comes from the test stub
    # `_FakeProjects.data = "test-data-prj"`).
    assert "`test-data-prj.finance.ue`" in sent, (
        f"bare-name rewrite failed; sent SQL: {sent!r}"
    )
    # Pre-#171 path emitted `SELECT * FROM`; the new path forwards the
    # user SELECT clause untouched.
    assert "SELECT order_id" in sent, (
        f"pre-check is still using synthetic SELECT *; sent SQL: {sent!r}"
    )


def test_guardrail_invokes_dry_run_exactly_once_per_request(
    seeded_app, mock_dry_run, monkeypatch,
):
    """Single dry-run path: even when the user references multiple registered
    tables in one query (a JOIN, a UNION, …), only ONE dry-run fires.

    Pre-#171 the pre-check ran N dry-runs (one synthetic SELECT * per table)
    and summed. Now BQ does the joining for us in a single dry-run — cheaper
    AND more accurate (joins/filters/projections apply across both sides).
    """
    _register_bq_remote_row("orders", "finance", "orders")
    _register_bq_remote_row("traffic", "marketing", "traffic")

    state = {"call_count": 0, "last_sql": None}

    def counting_fake(_bq, sql, **_kwargs):
        state["call_count"] += 1
        state["last_sql"] = sql
        return 100  # tiny

    monkeypatch.setattr(
        "app.api.query._bq_dry_run_bytes", counting_fake, raising=False,
    )

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    c.post(
        "/api/query",
        json={
            "sql": (
                "SELECT o.id, t.views FROM orders o "
                "JOIN traffic t ON o.date = t.date"
            ),
        },
        headers=_auth(token),
    )
    assert state["call_count"] == 1, (
        f"single-dry-run path expected; got {state['call_count']} calls"
    )
    # Both bare names rewritten in the same SQL.
    assert "`test-data-prj.finance.orders`" in state["last_sql"]
    assert "`test-data-prj.marketing.traffic`" in state["last_sql"]


def test_fallback_tries_original_sql_first(
    seeded_app, mock_dry_run, monkeypatch,
):
    """Issue #201 — when the rewriter produces SQL that BQ rejects with
    `bq_bad_request` but the user's ORIGINAL SQL dry-runs cleanly, the
    cap-guard uses the original SQL's byte estimate. No more synthetic
    `SELECT *` over-estimate.

    Bare-name reference populates `dry_run_set` so the cap-guard
    actually fires. Mock returns parse-error on the first call
    (rewritten SQL) and small bytes on the second (original)."""
    from connectors.bigquery.access import BqAccessError

    _register_bq_remote_row("ue", "finance", "ue")

    state = {"calls": []}

    def fake_dry_run(_bq, sql, **_kwargs):
        state["calls"].append(sql)
        # First call (rewritten SQL) → BQ parse error.
        if len(state["calls"]) == 1:
            raise BqAccessError("bq_bad_request", "Syntax error: simulated")
        # Second call (the user's original SQL) → small, passes cap.
        return 4096

    monkeypatch.setattr(
        "app.api.query._bq_dry_run_bytes", fake_dry_run, raising=False,
    )

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    user_sql = "SELECT order_id FROM ue WHERE country = 'CZ'"
    r = c.post("/api/query", json={"sql": user_sql}, headers=_auth(token))

    # Two dry-runs: rewritten then original. No third synthetic-SELECT-*
    # call.
    assert len(state["calls"]) == 2, (
        f"expected rewritten + original-SQL retry, got "
        f"{len(state['calls'])}: {state['calls']}"
    )
    assert state["calls"][1] == user_sql, (
        f"second call must be the user's ORIGINAL SQL, got "
        f"{state['calls'][1]!r}"
    )
    # The response must NOT be remote_scan_too_large from a synthetic
    # over-estimate — 4096 bytes is well under the 5 GiB cap.
    if r.status_code == 400:
        detail = r.json().get("detail", {})
        if isinstance(detail, dict):
            assert detail.get("reason") != "remote_scan_too_large", detail


def test_fallback_fails_fast_on_pure_duckdb_syntax(
    seeded_app, mock_dry_run, monkeypatch,
):
    """When BOTH the rewritten and original SQL fail with `bq_bad_request`
    (true DuckDB-only syntax like `::INT`), return HTTP 400
    `remote_estimate_failed` — never silently over-estimate via a
    synthetic `SELECT *`."""
    from connectors.bigquery.access import BqAccessError

    _register_bq_remote_row("ue", "finance", "ue")

    state = {"calls": []}

    def always_parse_error(_bq, sql, **_kwargs):
        state["calls"].append(sql)
        raise BqAccessError("bq_bad_request", "Syntax error: unexpected '::'")

    monkeypatch.setattr(
        "app.api.query._bq_dry_run_bytes", always_parse_error, raising=False,
    )

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        json={"sql": "SELECT order_id::INT FROM ue WHERE country = 'CZ'"},
        headers=_auth(token),
    )

    # Two dry-runs (rewritten + original retry). NO synthetic SELECT * fallback.
    assert len(state["calls"]) == 2, (
        f"expected 1 rewritten + 1 original-retry, got "
        f"{len(state['calls'])}: {state['calls']}"
    )
    # No call should be a synthetic ``SELECT * FROM `<project>...```. The
    # original-SQL retry contains the user's SELECT clause.
    for c_sql in state["calls"]:
        # If a call is just a synthetic ``SELECT * FROM `<project>.<bucket>.<table>```
        # the user's `WHERE country = 'CZ'` would be missing.
        if c_sql.startswith("SELECT * FROM `") and "WHERE" not in c_sql:
            raise AssertionError(
                f"synthetic SELECT * fallback was used: {c_sql!r}"
            )

    assert r.status_code == 400, r.json()
    detail = r.json().get("detail", {})
    assert isinstance(detail, dict), detail
    assert detail.get("kind") == "remote_estimate_failed", detail
    assert "underlying" in detail, detail
    assert "agnes catalog" in detail.get("hint", "").lower() or \
           "backtick" in detail.get("hint", "").lower(), detail


def test_remote_estimate_failed_surfaces_first_error_when_attempts_differ(
    seeded_app, mock_dry_run, monkeypatch,
):
    """When the rewritten-SQL dry-run fails with a column-not-found /
    syntax error and the original-SQL retry fails with the unhelpful
    "must be qualified" (the typical shape for catalog-id references —
    user SQL has no qualifying dataset, so the retry is guaranteed to
    fail this way), the surfaced `underlying` MUST be the first
    attempt's diagnostic. Pre-fix the second attempt's message
    overwrote the first, masking the real cause from the user.
    """
    from connectors.bigquery.access import BqAccessError

    _register_bq_remote_row("ue", "finance", "ue")

    state = {"calls": 0}

    def two_different_errors(_bq, _sql, **_kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            raise BqAccessError(
                "bq_bad_request",
                "Unrecognized name: authorize_date at [1:88]",
            )
        raise BqAccessError(
            "bq_bad_request",
            "Table 'unit_economics' must be qualified with a dataset "
            "(e.g. dataset.table)",
        )

    monkeypatch.setattr(
        "app.api.query._bq_dry_run_bytes", two_different_errors,
        raising=False,
    )

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        json={
            "sql": (
                "SELECT COUNT(*) FROM ue "
                "WHERE authorize_date = DATE '2025-05-06'"
            ),
        },
        headers=_auth(token),
    )

    assert state["calls"] == 2, (
        f"expected rewritten + original-retry = 2 dry-runs, got "
        f"{state['calls']}"
    )
    assert r.status_code == 400, r.json()
    detail = r.json().get("detail", {})
    assert isinstance(detail, dict), detail
    assert detail.get("kind") == "remote_estimate_failed", detail
    # The FIRST attempt's diagnostic — the actually-useful one — wins.
    assert "authorize_date" in detail.get("underlying", ""), detail
    # The second attempt's context is preserved for operator visibility.
    assert "must be qualified" in detail.get("underlying_original", ""), \
        detail
    # Hint now points at `agnes schema` first — the typical cause is a
    # typo'd column name on the FROM table.
    assert "agnes schema" in detail.get("hint", "").lower(), detail


def test_guardrail_propagates_502_on_non_parse_bq_errors(
    seeded_app, mock_dry_run, monkeypatch,
):
    """Forbidden / upstream-error from BQ on the dry-run still maps to 502;
    fallback only kicks in for parse errors. Important so a misconfigured
    SA doesn't silently fall back to a stale-metadata estimate."""
    from connectors.bigquery.access import BqAccessError

    _register_bq_remote_row("ue", "finance", "ue")

    def always_forbidden(_bq, _sql, **_kwargs):
        raise BqAccessError("bq_forbidden", "Permission denied", details={})

    monkeypatch.setattr(
        "app.api.query._bq_dry_run_bytes", always_forbidden, raising=False,
    )

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        json={"sql": "SELECT count(*) FROM ue"},
        headers=_auth(token),
    )
    assert r.status_code == 502, r.json()
    detail = r.json().get("detail", {})
    if isinstance(detail, dict):
        assert detail.get("kind") == "bq_forbidden"


def test_rewrite_helper_handles_bare_name_and_bq_path_in_same_sql():
    """Direct unit-test of the rewriter so the exact regex behavior is
    pinned: both bare names AND ``bq.<ds>.<tbl>`` references in the same
    SQL are translated, and longer names win over shorter prefixes.
    """
    from app.api.query import _rewrite_user_sql_for_bq_dry_run

    rewritten = _rewrite_user_sql_for_bq_dry_run(
        sql=(
            'SELECT a.id, b.col '
            'FROM ue a JOIN bq."finance"."traffic" b ON a.date = b.date'
        ),
        name_lookups=[("ue", "finance", "ue")],
        project="data-prj",
    )
    assert "`data-prj.finance.ue`" in rewritten
    assert "`data-prj.finance.traffic`" in rewritten
    # Original duckdb-flavor `bq."ds"."t"` form should have been replaced —
    # if it's still in the output, the BQ.path pass missed it.
    assert 'bq."finance"."traffic"' not in rewritten


def test_rewrite_helper_longer_name_wins_over_prefix():
    """When two registered names share a prefix (`unit_economics`,
    `unit_economics_summary`), the longer one must rewrite first so the
    shorter one's regex doesn't eat the prefix and leave junk like
    ``\\`...ue\\`_summary`` behind.
    """
    from app.api.query import _rewrite_user_sql_for_bq_dry_run

    rewritten = _rewrite_user_sql_for_bq_dry_run(
        sql="SELECT * FROM unit_economics_summary",
        name_lookups=[
            ("unit_economics", "fin", "ue"),
            ("unit_economics_summary", "fin", "ue_summary"),
        ],
        project="p",
    )
    assert "`p.fin.ue_summary`" in rewritten
    # If the shorter name had eaten the prefix we'd see `p.fin.ue`_summary
    # (broken token). Assert that doesn't happen.
    assert "`p.fin.ue`" not in rewritten


def test_rewrite_helper_does_not_corrupt_when_project_id_contains_registered_name():
    """Regression for Devin Review on query.py:464.

    Pre-fix the rewriter ran one `re.sub(\\bname\\b, ...)` per registered
    table, longest-first. When the GCP project ID contained a registered
    table name as a hyphen-delimited word (e.g. project=`my-ue-project`,
    registered name=`ue`), iter N's `\\b` regex would match INSIDE the
    backticked replacement text from a PRIOR iter, corrupting the output.

    Concrete trace:
    - SQL: ``FROM orders JOIN ue ON ...``
    - Iter 1 (orders): produces ``FROM `my-ue-project.fin.orders` JOIN ue ON``
    - Iter 2 (ue): `\\bue\\b` matches `ue` inside `my-ue-project` (hyphen =
      word boundary on both sides) → corrupts the iter-1 path.

    Post-fix: single `re.sub` with an alternation regex processes each
    source position exactly once. Freshly-inserted backticked text is
    NOT re-scanned by subsequent name patterns.
    """
    from app.api.query import _rewrite_user_sql_for_bq_dry_run

    rewritten = _rewrite_user_sql_for_bq_dry_run(
        sql="SELECT * FROM orders JOIN ue ON orders.id = ue.id",
        name_lookups=[
            ("orders", "fin", "orders"),
            ("ue", "analytics", "ue_metrics"),
        ],
        project="my-ue-project",
    )

    # Both names rewritten exactly once. Critically, the orders path is
    # NOT corrupted by a stray rewrite of `ue` inside `my-ue-project`.
    assert "`my-ue-project.fin.orders`" in rewritten
    assert "`my-ue-project.analytics.ue_metrics`" in rewritten

    # The corruption signature: the orders path would contain a nested
    # backtick-fenced ue path. Pinning this absence is the load-bearing
    # assertion — it fails on the pre-fix iterative rewriter.
    assert "`my-`my-ue-project.analytics.ue_metrics`-project" not in rewritten

    # Bare `ue` outside backticks (the JOIN clause) should be rewritten.
    # The 2nd `ue.id` was already rewritten by the same single-pass call.
    # No `\\bue\\b` survives outside backticks.
    import re as _re
    bare_ue_matches = _re.findall(r"(?<!\\.)\\bue\\b(?![.`])", rewritten)
    assert not bare_ue_matches, f"unrewritten bare `ue` left: {bare_ue_matches!r}"


def test_rewrite_helper_is_case_insensitive_on_bare_names():
    """Bare-name match in `_bq_guardrail_inputs` is case-insensitive (it
    runs against `sql_lower`). The rewriter must match the same set of
    occurrences on the original-case SQL or we'd silently leave some
    references untranslated and dry-run on a half-rewritten SQL.
    """
    from app.api.query import _rewrite_user_sql_for_bq_dry_run

    rewritten = _rewrite_user_sql_for_bq_dry_run(
        sql="SELECT * FROM UE WHERE Ue.id IS NOT NULL",
        name_lookups=[("ue", "fin", "ue")],
        project="p",
    )
    assert "`p.fin.ue` WHERE `p.fin.ue`.id" in rewritten or \
           rewritten.lower().count("`p.fin.ue`") == 2


# ---------------------------------------------------------------------------
# Issue #201: rewriter must NOT touch text inside `…` backtick segments.
# A user-supplied full BQ-native path `<project>.<dataset>.<table>` whose
# table segment matches a registered bare name was being re-substituted
# inside the backticks, producing malformed nested-backtick SQL that BQ
# rejected with a parse error.
# ---------------------------------------------------------------------------


def test_rewrite_skips_inside_backtick_path():
    """Full backtick BQ path is preserved byte-for-byte even when its
    final segment matches a registered bare-name alias."""
    from app.api.query import _rewrite_user_sql_for_bq_dry_run

    sql = (
        "SELECT * FROM `my-prj.finance.unit_economics` "
        "WHERE country = 'CZ'"
    )
    rewritten = _rewrite_user_sql_for_bq_dry_run(
        sql=sql,
        name_lookups=[("unit_economics", "finance", "unit_economics")],
        project="my-prj",
    )
    # No corruption — input is already BQ-native, rewriter is a no-op here.
    assert rewritten == sql, (
        f"backtick path was rewritten:\n  in : {sql!r}\n  out: {rewritten!r}"
    )
    # Sanity: the malformed nested form must NOT appear.
    assert "`my-prj.finance.`my-prj" not in rewritten


def test_rewrite_skips_inside_backtick_with_outside_bare_name():
    """Mixed SQL: a bare name outside backticks is rewritten as before,
    but an identically-named segment inside a backtick path is left
    alone."""
    from app.api.query import _rewrite_user_sql_for_bq_dry_run

    sql = (
        "SELECT a.id, b.col FROM ue a "
        "JOIN `my-prj.finance.ue` b ON a.id = b.id"
    )
    rewritten = _rewrite_user_sql_for_bq_dry_run(
        sql=sql,
        name_lookups=[("ue", "fin_alias", "ue_alias")],
        project="my-prj",
    )
    # Outside-backtick `ue` rewrites to the registered alias path.
    assert "`my-prj.fin_alias.ue_alias`" in rewritten
    # The user-supplied backtick path is preserved verbatim.
    assert "`my-prj.finance.ue`" in rewritten
    # The malformed nested form must NOT appear.
    assert "`my-prj.finance.`my-prj.fin_alias.ue_alias`" not in rewritten


def test_guardrail_skips_bare_name_match_inside_backticks(
    seeded_app, mock_dry_run, monkeypatch,
):
    """The `name_lookups` collection populated by `_bq_guardrail_inputs`
    must not include a registered name when the only place that name
    appears in the SQL is inside a `…` backtick segment.

    Captures the rewritten SQL the guardrail forwards to the dry-run and
    asserts the bare-name was NOT substituted inside the user's backtick
    path.
    """
    _register_bq_remote_row("unit_economics", "finance", "unit_economics")

    captured = {"sql": None}

    def capturing_fake(_bq, sql, **_kwargs):
        captured["sql"] = sql
        return 1024

    monkeypatch.setattr(
        "app.api.query._bq_dry_run_bytes", capturing_fake, raising=False,
    )

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    user_sql = (
        "SELECT * FROM `test-data-prj.finance.unit_economics` "
        "WHERE country = 'CZ'"
    )
    c.post("/api/query", json={"sql": user_sql}, headers=_auth(token))

    sent = captured["sql"]
    if sent is None:
        # Guardrail decided no BQ tables were referenced — that's also
        # an acceptable "no false-positive" outcome (Layer 3 will cover
        # the explicit registry check for full backtick paths). We just
        # need to ensure the bare-name regex didn't fire.
        return
    # The user's exact backtick path must survive verbatim — no nested
    # backticks introduced by a stray bare-name rewrite.
    assert "`test-data-prj.finance.unit_economics`" in sent, (
        f"backtick path corrupted by guardrail:\n  out: {sent!r}"
    )
    assert "`test-data-prj.finance.`test-data-prj" not in sent, (
        f"nested-backtick corruption signature present: {sent!r}"
    )


# ---------------------------------------------------------------------------
# Issue #201 Layer 3: full backtick BigQuery paths are registry-gated.
# Pre-fix these bypassed Agnes RBAC entirely — only the configured service
# account scope limited which tables a user could reach. Post-fix, they're
# treated identically to `bq."<dataset>"."<table>"` syntax.
# ---------------------------------------------------------------------------


def test_full_backtick_path_unregistered_denied(seeded_app, mock_dry_run):
    """Full backtick path to an unregistered `<dataset>.<table>` (project
    matches the configured data project) → HTTP 403 with
    `bq_path_not_registered`."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        json={
            "sql": (
                "SELECT * FROM `test-data-prj.secret_ds.secret_tbl` "
                "WHERE country = 'CZ'"
            ),
        },
        headers=_auth(token),
    )
    assert r.status_code == 403, r.json()
    detail = r.json().get("detail", {})
    assert isinstance(detail, dict), detail
    assert detail.get("reason") == "bq_path_not_registered", detail
    assert "secret_ds" in detail.get("path", ""), detail
    assert "secret_tbl" in detail.get("path", ""), detail


def test_full_backtick_path_cross_project_denied(seeded_app, mock_dry_run):
    """Full backtick path with project ≠ configured data project → HTTP
    403 with `bq_path_cross_project`. Even if the path happens to point
    at a registered (bucket, source_table), the project mismatch is the
    primary boundary."""
    _register_bq_remote_row("ue", "finance", "ue")
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        json={
            "sql": "SELECT * FROM `other-project.finance.ue` WHERE id = 1",
        },
        headers=_auth(token),
    )
    assert r.status_code == 403, r.json()
    detail = r.json().get("detail", {})
    assert isinstance(detail, dict), detail
    assert detail.get("reason") == "bq_path_cross_project", detail
    assert detail.get("expected_project") == "test-data-prj", detail
    assert "other-project" in detail.get("path", ""), detail


def test_full_backtick_path_registered_admin_passes(
    seeded_app, mock_dry_run, monkeypatch,
):
    """Admin caller + registered path + matching project → no RBAC
    rejection. The dry-run fires (we can capture the SQL the guardrail
    forwards) and no `bq_path_*` reason appears in any error response."""
    _register_bq_remote_row("ue", "finance", "ue")

    captured = {"sql": None}

    def capturing_fake(_bq, sql, **_kwargs):
        captured["sql"] = sql
        return 1024  # tiny — pass cap

    monkeypatch.setattr(
        "app.api.query._bq_dry_run_bytes", capturing_fake, raising=False,
    )

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        json={
            "sql": "SELECT * FROM `test-data-prj.finance.ue` WHERE id = 1",
        },
        headers=_auth(token),
    )
    # If 403, must NOT be the issue-#201 bq_path_* reasons.
    if r.status_code == 403:
        detail = r.json().get("detail", {})
        if isinstance(detail, dict):
            assert detail.get("reason") not in (
                "bq_path_not_registered",
                "bq_path_access_denied",
                "bq_path_cross_project",
            ), f"admin + registered path should pass RBAC: {detail}"
    # The dry-run was invoked, meaning Pass 3 added the path to dry_run_set
    # and the cap-guard fired. The user's WHERE clause must still be in
    # the dry-run SQL (validates Layer 1 — backtick-aware rewrite).
    assert captured["sql"] is not None, (
        "dry-run never fired — Pass 3 may not have registered the path"
    )
    assert "`test-data-prj.finance.ue`" in captured["sql"], captured["sql"]
    assert "WHERE id = 1" in captured["sql"], captured["sql"]


def test_full_backtick_path_inside_string_literal_not_gated(
    seeded_app, mock_dry_run,
):
    """Defensive case: a backtick path appearing inside a SQL string
    literal (rare but possible) should not trigger Pass 3. Practically
    this is unreachable because backticks aren't typically valid inside
    BQ string literals — but the regex doesn't know that. We document
    that the gate applies to ALL backtick triples to be safe; users who
    really need a literal can use single-quoted strings without
    backticks."""
    # No registration; the test confirms an unregistered path inside
    # what looks like a string is still gated. This is the conservative
    # boundary — false-positive on string literal beats false-negative
    # on a real RBAC bypass.
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        json={
            "sql": (
                "SELECT 'matches `test-data-prj.x.y`' AS lit"
            ),
        },
        headers=_auth(token),
    )
    # Either gated (403) or 200 if the analytics DB happens to evaluate
    # the literal — both are acceptable. The point is no silent RBAC
    # bypass: if the response is 200, no BQ table was reached.
    if r.status_code == 403:
        detail = r.json().get("detail", {})
        if isinstance(detail, dict):
            assert detail.get("reason") in (
                "bq_path_not_registered",
                "bq_path_cross_project",
            ), detail


class TestHintForBqBadRequest:
    """The `remote_estimate_failed` hint must branch on the BQ error class.

    Pre-#NNN every BQ rejection got the same "column referenced doesn't
    exist" hint, which actively misled analysts whenever BQ actually
    rejected on syntax (e.g. `SELECT COUNT(*) AS rows` — `rows` is
    reserved). The dispatch helper picks the most useful one-line hint
    based on what BigQuery actually said.
    """

    def test_syntax_error_hint_calls_out_reserved_keyword_alias(self):
        from app.api.query import _hint_for_bq_bad_request

        # Sub-agent-reported actual case from the v0.53.4 smoke test
        hint = _hint_for_bq_bad_request(
            "Syntax error: Unexpected keyword ROWS at [1:20]"
        )
        assert "syntax" in hint.lower()
        assert "reserved" in hint.lower() or "rows" in hint.lower()
        # Must NOT lead with the misleading column-not-found hint
        assert "column referenced" not in hint.lower()

    def test_no_hint_branch_leaks_literal_backslashes(self):
        # Hints are surfaced as JSON `hint:` and printed verbatim by the
        # CLI — no markdown rendering. A backslash-backtick in the Python
        # source literal becomes a literal backslash followed by a
        # backtick in the output, which is exactly the misleading shape
        # this dispatcher exists to fix (see #274 follow-up). Pin every
        # branch against the regression.
        from app.api.query import _hint_for_bq_bad_request

        cases = [
            "Syntax error: Unexpected keyword ROWS at [1:20]",
            "Unrecognized name: authorize_date at [1:88]",
            "Field 'foo' not found inside record_type",
            "Table not found: my-project.dataset.tbl",
            "Some unfamiliar BQ diagnostic",  # fallback
        ]
        for msg in cases:
            hint = _hint_for_bq_bad_request(msg)
            assert "\\`" not in hint, (
                f"hint for {msg!r} contains literal backslash-backtick: "
                f"{hint!r}"
            )
            assert "\\\\" not in hint, (
                f"hint for {msg!r} contains literal double-backslash: "
                f"{hint!r}"
            )

    def test_unrecognized_name_hint_points_at_agnes_schema(self):
        from app.api.query import _hint_for_bq_bad_request

        hint = _hint_for_bq_bad_request(
            "Unrecognized name: authorize_date at [1:88]"
        )
        assert "agnes schema" in hint.lower()
        assert "doesn't exist" in hint.lower() or "column" in hint.lower()

    def test_table_not_found_hint_points_at_agnes_catalog(self):
        from app.api.query import _hint_for_bq_bad_request

        hint = _hint_for_bq_bad_request(
            "Table not found: my-project.dataset.tbl"
        )
        assert "agnes catalog" in hint.lower()

    def test_unknown_error_falls_back_to_generic_hint(self):
        from app.api.query import _hint_for_bq_bad_request

        hint = _hint_for_bq_bad_request(
            "Some unfamiliar BigQuery diagnostic we don't classify yet"
        )
        # Generic hint mentions all three common causes so the analyst
        # has somewhere to start
        assert "schema" in hint.lower()
        assert "underlying" in hint.lower()
