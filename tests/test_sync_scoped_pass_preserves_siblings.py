"""A scoped Keboola sync must not delete the master views of the tables it
was not asked about.

`connectors.keboola.extractor.run(merge=False)` rebuilds
`extracts/keboola/extract.duckdb` from scratch — a fresh `.tmp` holding only
that call's tables, atomically moved over the old file. That implicit prune is
how deleted / renamed registry rows disappear, and it is only correct for a
pass that covers the whole Keboola local surface.

`_run_sync` used to key `merge` off the credential GROUP alone (`merge=not
_first_group`), so the first group of ANY pass pruned — including a pass
narrowed to a subset:

  - `POST /api/sync/trigger` with `{"tables": [...]}` — documented in
    `_run_sync` as "manual operator override … sync these specific tables
    now";
  - a `sync_schedule` cadence filter that left only some rows due.

`query_mode='local'` rows have no recovery path once that happens: the
orchestrator's filesystem-fallback rebuilds master views from `data/*.parquet`
only for `query_mode='materialized'` rows, so the parquet sits on disk while
every read 400s with `Catalog Error: Table with name X does not exist`. The
catalog keeps listing the table (that reads the registry), and `schema` /
`sample` keep answering, so the agent gets no signal and retries the same
dead name.

Found in production: a single-table re-sync left an extract holding that one
table and wiped the master views of every sibling of the source. Reads against
them 400d for hours, and the cadence sync could not heal it because on that
deployment it never carried the full surface either (see layer 3).

Three layers:
  1. `_pass_covers_surface` / `_keboola_extract_surface` — the predicate and
     the surface it is measured against, including the empty-surface,
     id-vs-name, remote-row and legacy-`source_type` cases.
  2. `_run_sync` end-to-end — the extractor subprocess is invoked with
     `--merge` for a scoped pass and WITHOUT it for a full sweep. This is the
     layer that was broken; a predicate-only test would pass on the bug.
  3. the other half of the incident — a bare sweep selected local rows by the
     INSTANCE's `data_source.type` rather than by the explicit `?source=`
     filter, so on this deployment no full sweep ever ran to undo the prune.
"""

import pytest

from app.api import sync as sync_module
from src.repositories.table_registry import TableRegistryRepository


def _row(key, name=None):
    return {"id": key, "name": name or key}


class TestPassCoversSurface:
    def test_a_full_sweep_covers_the_surface(self):
        surface = [_row("orders"), _row("customers")]
        assert sync_module._pass_covers_surface(list(surface), surface) is True

    def test_a_superset_still_covers_the_surface(self):
        """A bare `list_local()` sweep carries other sources' local rows too —
        extra rows must not read as "does not cover"."""
        surface = [_row("orders"), _row("customers")]
        pass_rows = surface + [_row("bq_sessions")]
        assert sync_module._pass_covers_surface(pass_rows, surface) is True

    def test_a_single_table_pass_does_not(self):
        surface = [_row("orders"), _row("customers"), _row("invoices")]
        assert sync_module._pass_covers_surface([_row("orders")], surface) is False

    def test_a_due_filtered_pass_does_not(self):
        surface = [_row("orders"), _row("customers"), _row("invoices")]
        assert sync_module._pass_covers_surface([_row("orders"), _row("invoices")], surface) is False

    def test_an_empty_pass_does_not(self):
        assert sync_module._pass_covers_surface([], [_row("orders")]) is False

    def test_an_empty_surface_never_prunes(self):
        """Nothing to prune, and the extract may hold `_meta` rows a
        materialize pass registered — keeping them beats deleting them."""
        assert sync_module._pass_covers_surface([_row("orders")], []) is False

    def test_rows_are_keyed_by_id_not_name(self):
        """Auto-discovered Keboola rows have `id != name` (id
        "in_c-crm_company", name "company"). Keying on `name` would let a pass
        carrying a DIFFERENT row with a colliding name read as covering."""
        surface = [{"id": "in_c-crm_company", "name": "company"}]
        assert sync_module._pass_covers_surface([{"id": "in_c-crm_company", "name": "x"}], surface) is True
        assert sync_module._pass_covers_surface([{"id": "other", "name": "company"}], surface) is False


class TestKeboolaExtractSurface:
    """What the from-scratch rebuild would drop, and therefore what a pass has
    to account for before it is allowed to prune."""

    def test_local_and_remote_rows_are_both_in_the_surface(self):
        """`run()` writes a parquet view for a local row and a `kbc`-extension
        view plus `_remote_attach` for a remote one. A rebuild takes out
        either, so a pass that omits a remote sibling must not read as
        covering — that would delete the remote view and the attach row."""
        rows = [
            {"id": "kbc_orders", "query_mode": "local", "source_type": "keboola"},
            {"id": "kbc_live", "query_mode": "remote", "source_type": "keboola"},
        ]
        assert {r["id"] for r in sync_module._keboola_extract_surface(rows)} == {"kbc_orders", "kbc_live"}
        assert sync_module._pass_covers_surface([{"id": "kbc_orders"}], sync_module._keboola_extract_surface(rows)) is (
            False
        )

    def test_materialized_rows_are_not_in_the_surface(self):
        """`run()` skips them, and the orchestrator's filesystem-fallback
        recreates their master view from the parquet — the one query_mode with
        a recovery path, so it must not block the prune."""
        rows = [
            {"id": "kbc_orders", "query_mode": "local", "source_type": "keboola"},
            {"id": "kbc_rollup", "query_mode": "materialized", "source_type": "keboola"},
        ]
        assert {r["id"] for r in sync_module._keboola_extract_surface(rows)} == {"kbc_orders"}

    def test_other_sources_are_not_in_the_surface(self):
        rows = [
            {"id": "kbc_orders", "query_mode": "local", "source_type": "keboola"},
            {"id": "sf_ledger", "query_mode": "local", "source_type": "snowflake"},
        ]
        assert {r["id"] for r in sync_module._keboola_extract_surface(rows)} == {"kbc_orders"}

    def test_a_legacy_row_with_no_source_type_counts_as_keboola(self):
        """Keboola was the only connector when the column was added. The
        extractor-input filter reads such a row as Keboola, so the surface must
        too — counting it on one side only would let a pass omitting it still
        read as covering, and the prune would delete it."""
        rows = [{"id": "legacy", "query_mode": "local"}]
        assert {r["id"] for r in sync_module._keboola_extract_surface(rows)} == {"legacy"}
        assert sync_module._is_keboola_row({"id": "legacy"}) is True
        assert sync_module._is_keboola_row({"id": "sf", "source_type": "snowflake"}) is False

    def test_a_missing_query_mode_reads_as_local(self):
        assert {r["id"] for r in sync_module._keboola_extract_surface([{"id": "x"}])} == {"x"}


# ---- Layer 2: _run_sync end-to-end ----------------------------------------

_SURFACE = ("kbc_orders", "kbc_customers", "kbc_invoices")


def _harness(tmp_path, monkeypatch, *, data_source="keboola", extra_rows=()):
    """Drive `_run_sync` against a real registry holding three Keboola local
    rows (plus ``extra_rows``), with the extractor subprocess faked. Returns
    the list of argv lists the subprocess was launched with, so a test can
    assert on `--merge` and on whether it ran at all.

    ``data_source`` is what the instance reports as its primary connector —
    the knob that made a cadence sync skip a registry's own Keboola tables.
    """
    import src.db as _db_mod

    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("KEBOOLA_STACK_URL", "https://example.invalid")
    monkeypatch.setenv("KEBOOLA_STORAGE_TOKEN", "fake")

    conn = _db_mod.get_system_db()
    repo = TableRegistryRepository(conn)
    for name in _SURFACE:
        repo.register(
            id=name,
            name=name,
            source_type="keboola",
            query_mode="local",
            bucket="in.c-foo",
            source_table=name,
        )
    for kwargs in extra_rows:
        repo.register(**kwargs)
    _db_mod.close_system_db()

    monkeypatch.setattr("app.instance_config.get_data_source_type", lambda: data_source)
    monkeypatch.setattr(
        "app.instance_config.get_value",
        lambda *args, **kw: kw.get("default", ""),
    )

    cmds: list[list] = []

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            cmds.append(list(cmd))
            self.returncode = 0
            self.pid = 999

        def communicate(self, input=None, timeout=None):
            return ("{}", "")

    monkeypatch.setattr(sync_module.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(
        "app.api.sync._run_materialized_pass",
        lambda _conn, _bq, *, tables=None, source_type=None: {"materialized": [], "skipped": [], "errors": []},
    )

    class _OrchStub:
        def rebuild(self):
            return {}

    monkeypatch.setattr("src.orchestrator.SyncOrchestrator", lambda *a, **kw: _OrchStub())

    return cmds


@pytest.fixture
def extractor_cmds(tmp_path, monkeypatch):
    return _harness(tmp_path, monkeypatch)


def test_a_single_table_trigger_merges_instead_of_rebuilding(extractor_cmds):
    """The production incident: `tables=["kbc_orders"]` must not take the
    prune path, or the other two rows lose their `_meta` entry and inner
    view."""
    sync_module._run_sync(tables=["kbc_orders"])

    assert len(extractor_cmds) == 1, "the scoped pass should invoke the extractor exactly once"
    assert "--merge" in extractor_cmds[0], (
        "a `tables=[...]` pass rebuilt extract.duckdb from scratch — every "
        "Keboola table it was not asked about loses its master view and every "
        "read against it 400s until the next full sweep"
    )


def test_a_partial_multi_table_trigger_also_merges(extractor_cmds):
    """Two of three is still a subset — the third must survive."""
    sync_module._run_sync(tables=["kbc_orders", "kbc_invoices"])

    assert len(extractor_cmds) == 1
    assert "--merge" in extractor_cmds[0]


def test_a_full_sweep_still_prunes(extractor_cmds):
    """The prune must stay for a pass that accounts for the whole surface —
    it is what makes a deleted / renamed registry row disappear from the
    extract."""
    sync_module._run_sync(tables=None)

    assert len(extractor_cmds) == 1
    assert "--merge" not in extractor_cmds[0], (
        "a full sweep must keep the implicit prune, otherwise deleted and "
        "renamed registry rows keep their views forever"
    )


def test_an_explicit_whole_surface_trigger_still_prunes(extractor_cmds):
    """`tables=[...]` naming every row is a full sweep by another route, and
    should behave like one."""
    sync_module._run_sync(tables=list(_SURFACE))

    assert len(extractor_cmds) == 1
    assert "--merge" not in extractor_cmds[0]


# ---- Layer 3: a bare sweep must cover every registered source -------------


def test_a_bare_sweep_covers_keboola_on_an_instance_provisioned_as_something_else(tmp_path, monkeypatch):
    """The shape that hid the bug: the instance's `data_source.type` resolves
    to "local" (a CSV/local provisioning) while the registry holds Keboola
    local rows behind a named source_connection. Selecting local rows by the
    INSTANCE source type made every cadence tick a no-op, so the tables went
    stale and — the reason it mattered — a full sweep never ran to undo a
    scoped pass's prune.

    The materialized pass already ignores the instance type on a bare sweep
    (`source_type=source_type_filter`); this pins the local half to the same
    rule."""
    cmds = _harness(tmp_path, monkeypatch, data_source="local")

    sync_module._run_sync(tables=None)

    assert len(cmds) == 1, (
        "a bare sweep skipped the Keboola extractor because the instance calls "
        "itself a 'local' deployment — its 28 registered Keboola tables were "
        "never re-extracted on a schedule"
    )
    assert "--merge" not in cmds[0], "a bare sweep carries the whole surface, so it should still prune"


def test_an_explicit_source_filter_still_scopes_the_sweep(tmp_path, monkeypatch):
    """`?source=bigquery` must keep excluding Keboola rows — the filter is how
    a dual-source deployment scopes a rebuild, and broadening the bare sweep
    must not broaden that too."""
    cmds = _harness(tmp_path, monkeypatch, data_source="keboola")

    sync_module._run_sync(tables=None, source_type_filter="bigquery")

    assert cmds == [], "a BQ-scoped rebuild must not touch the Keboola extract"


def test_a_pass_with_no_keboola_rows_never_spawns_the_keboola_extractor(tmp_path, monkeypatch):
    """The extractor subprocess only knows how to extract Keboola rows. A
    pass naming a foreign local row must not reach it — its rebuild would
    empty extracts/keboola/extract.duckdb outright."""
    cmds = _harness(
        tmp_path,
        monkeypatch,
        extra_rows=[
            dict(
                id="sf_ledger",
                name="sf_ledger",
                source_type="snowflake",
                query_mode="local",
                bucket="GOLD",
                source_table="LEDGER",
            )
        ],
    )

    sync_module._run_sync(tables=["sf_ledger"])

    assert cmds == [], (
        "a non-Keboola row was handed to the Keboola extractor, whose "
        "from-scratch rebuild would have wiped every Keboola table's view"
    )


def test_a_mixed_bare_sweep_hands_the_extractor_only_keboola_rows(tmp_path, monkeypatch):
    """And when both are in scope, the foreign row is filtered out rather than
    passed through — coverage of the Keboola surface is judged on the Keboola
    subset, so the prune still applies."""
    cmds = _harness(
        tmp_path,
        monkeypatch,
        extra_rows=[
            dict(
                id="sf_ledger",
                name="sf_ledger",
                source_type="snowflake",
                query_mode="local",
                bucket="GOLD",
                source_table="LEDGER",
            )
        ],
    )

    sync_module._run_sync(tables=None)

    assert len(cmds) == 1
    assert "--merge" not in cmds[0]


def test_a_pass_omitting_a_remote_sibling_merges(tmp_path, monkeypatch):
    """End-to-end counterpart of the surface unit test: with a Keboola remote
    row registered, a bare sweep selects local rows only (`list_local`), so it
    cannot account for the remote half and must merge. Otherwise the rebuild
    drops the remote view and the `_remote_attach` row the orchestrator needs
    to re-ATTACH the extension."""
    cmds = _harness(
        tmp_path,
        monkeypatch,
        extra_rows=[
            dict(
                id="kbc_live",
                name="kbc_live",
                source_type="keboola",
                query_mode="remote",
                bucket="in.c-foo",
                source_table="live",
            )
        ],
    )

    sync_module._run_sync(tables=None)

    assert len(cmds) == 1
    assert "--merge" in cmds[0], (
        "a sweep that carries no remote rows still pruned — the remote view and "
        "`_remote_attach` row would be deleted from the extract"
    )


def test_custom_connectors_run_for_a_pass_with_no_keboola_rows(tmp_path, monkeypatch):
    """The custom-connector loop must not hang off the Keboola predicate: a
    deployment whose only local rows come from a mounted custom connector has
    no Keboola rows at all, and its connectors still have to run."""
    connectors_dir = tmp_path / "custom"
    (connectors_dir / "acme").mkdir(parents=True)
    (connectors_dir / "acme" / "extractor.py").write_text("")
    monkeypatch.setenv("CONNECTORS_DIR", str(connectors_dir))

    ran: list = []
    _harness(
        tmp_path,
        monkeypatch,
        extra_rows=[
            dict(
                id="sf_ledger",
                name="sf_ledger",
                source_type="snowflake",
                query_mode="local",
                bucket="GOLD",
                source_table="LEDGER",
            )
        ],
    )
    monkeypatch.setattr(
        sync_module.subprocess,
        "run",
        lambda cmd, **kw: ran.append(cmd) or _CompletedStub(),
    )

    sync_module._run_sync(tables=["sf_ledger"])

    assert ran, "a custom connector was skipped because the pass carried no Keboola rows"


class _CompletedStub:
    returncode = 0
    stdout = ""
    stderr = ""
