"""Tests for agnes status (workspace status)."""

import json

import pytest
from typer.testing import CliRunner

from cli.commands.status import status_app

# CI-safety: Typer/rich emits ANSI escapes in --help output. Strip before asserts.
_ANSI_RE = __import__("re").compile(r"\x1b\[[0-9;]*m")


def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)


runner = CliRunner()


def test_status_uninitialized_workspace(tmp_path, monkeypatch):
    """Empty folder → exit 0, output indicates uninitialized state."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    result = runner.invoke(status_app)
    assert result.exit_code in (0, 1)
    out = result.output.lower()
    assert "no" in out  # "Initialized: no" or similar
    assert "agnes init" in _clean(result.output)  # hint to initialize


def test_status_initialized_via_legacy_claude_md_marker(tmp_path, monkeypatch):
    """Pre-#259 workspaces have only the legacy `# AI Data Analyst` string
    in CLAUDE.md (no `.claude/init-complete` sentinel) — keep recognising
    them so older analyst checkouts don't flip to 'Initialized: no' after
    a CLI upgrade."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    (tmp_path / "CLAUDE.md").write_text("# AI Data Analyst\n")
    (tmp_path / "user" / "duckdb").mkdir(parents=True)
    (tmp_path / "user" / "duckdb" / "analytics.duckdb").touch()
    (tmp_path / "server" / "parquet").mkdir(parents=True)
    (tmp_path / "server" / "parquet" / "tbl1.parquet").touch()

    result = runner.invoke(status_app)
    assert result.exit_code == 0
    out = result.output.lower()
    assert "yes" in out
    assert "1" in _clean(result.output)


def test_status_initialized_via_init_complete_sentinel(tmp_path, monkeypatch):
    """Override-mode workspaces (customer-supplied Initial-Workspace
    templates whose CLAUDE.md body legitimately omits the literal
    'AI Data Analyst' substring) must still report 'Initialized: yes'
    when `.claude/init-complete` exists. The sentinel is authoritative
    for override mode; the legacy CLAUDE.md grep is fallback-only."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    # Heading deliberately omits the canonical "AI Data Analyst" marker;
    # this is what a custom override template can look like in the wild.
    (tmp_path / "CLAUDE.md").write_text("# Acme — Custom Workspace\n")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "init-complete").write_text(
        "completed_at: 2026-05-26T11:33:30Z\nagnes_version: 0.55.13\noverride: true\n"
    )
    result = runner.invoke(status_app)
    assert result.exit_code == 0
    assert "yes" in result.output.lower()
    assert "agnes init" not in _clean(result.output)


def test_status_json(tmp_path, monkeypatch):
    """--json flag returns machine-readable output."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "init-complete").write_text("agnes_version: 0.55.13\n")
    result = runner.invoke(status_app, ["--json"])
    assert result.exit_code == 0
    body = json.loads(result.output)
    assert "workspace" in body and "initialized" in body
    assert body["initialized"] is True


# ---------------------------------------------------------------------------
# Table counting. Two numbers, deliberately not summed:
#   queryable  -> tables the DuckDB view rebuild exposes (server/parquet/),
#                 i.e. what `agnes query --local` can resolve
#   no-view    -> parquets the stack sync (step 8) put in .claude/data/_shared/
#                 that no view covers: real bytes on disk, unreachable locally
# Verified against a live workspace: 6 partitioned legacy tables (331 part
# files, which the old top-level glob counted as 0) alongside 37 `_shared`
# parquets with zero corresponding views.
# ---------------------------------------------------------------------------


def _init(workspace):
    (workspace / ".claude").mkdir(parents=True, exist_ok=True)
    (workspace / ".claude" / "init-complete").write_text("agnes_version: test\n")


def _counts(workspace, monkeypatch):
    """(queryable, downloaded-without-view) as `agnes status --json` reports."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(workspace))
    result = runner.invoke(status_app, ["--json"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    return body["parquet_tables"], body["tables_downloaded_no_local_view"]


def test_partitioned_table_counts_once_not_zero(tmp_path, monkeypatch):
    """A partitioned table is a DIRECTORY of parts. The old non-recursive glob
    returned 0 for it however many parts were on disk — the reported bug."""
    _init(tmp_path)
    parts = tmp_path / "server" / "parquet" / "jira_issues"
    (parts / "month=2026-01").mkdir(parents=True)
    (parts / "month=2026-02").mkdir(parents=True)
    (parts / "month=2026-01" / "data.parquet").touch()
    (parts / "month=2026-02" / "data.parquet").touch()

    assert _counts(tmp_path, monkeypatch) == (1, 0)


def test_shared_only_table_is_not_counted_as_queryable(tmp_path, monkeypatch):
    """Nothing registers DuckDB views over `.claude/data/_shared/`, so a table
    present only there is on disk but NOT resolvable by `agnes query --local`.
    It must not inflate the queryable count."""
    _init(tmp_path)
    shared = tmp_path / ".claude" / "data" / "_shared"
    shared.mkdir(parents=True)
    (shared / "orders.parquet").touch()
    (shared / "customers.parquet").touch()

    assert _counts(tmp_path, monkeypatch) == (0, 2)


def test_package_reference_links_do_not_double_count(tmp_path, monkeypatch):
    """`_direct/` and `<package>/` are reference links INTO `_shared`, so a
    table shipped by two packages still counts once — and since #1325 the
    DuckDB view rebuild registers a view per reference NAME, so a table
    reachable through any of these three reference links is queryable, not
    merely downloaded: one view, not three, and it counts in `queryable`
    rather than `downloaded (no local view)`."""
    _init(tmp_path)
    data = tmp_path / ".claude" / "data"
    (data / "_shared").mkdir(parents=True)
    (data / "_shared" / "orders.parquet").touch()
    for ref_dir in ("_direct", "pkg_a", "pkg_b"):
        (data / ref_dir).mkdir(parents=True)
        (data / ref_dir / "orders.parquet").touch()

    assert _counts(tmp_path, monkeypatch) == (1, 0)


def test_same_table_in_both_trees_counts_once_as_queryable(tmp_path, monkeypatch):
    """Both pull phases can land the same table; it is one queryable table and
    must not also be reported as lacking a view."""
    _init(tmp_path)
    legacy = tmp_path / "server" / "parquet"
    legacy.mkdir(parents=True)
    (legacy / "orders.parquet").touch()
    shared = tmp_path / ".claude" / "data" / "_shared"
    shared.mkdir(parents=True)
    (shared / "orders.parquet").touch()

    assert _counts(tmp_path, monkeypatch) == (1, 0)


def test_non_slug_name_matches_across_trees(tmp_path, monkeypatch):
    """The trees are keyed differently: the legacy stem is the flat manifest
    key (`sync_state.table_id` == `table_registry.name`), `_shared/` uses
    `table_registry.id`, and registration derives the id by slugifying the
    name. `Agnes Audit Log` must not count as both queryable and missing."""
    _init(tmp_path)
    legacy = tmp_path / "server" / "parquet"
    legacy.mkdir(parents=True)
    (legacy / "Agnes Audit Log.parquet").touch()
    shared = tmp_path / ".claude" / "data" / "_shared"
    shared.mkdir(parents=True)
    (shared / "agnes_audit_log.parquet").touch()

    assert _counts(tmp_path, monkeypatch) == (1, 0)


def test_non_slug_partitioned_dir_matches_shared_stem(tmp_path, monkeypatch):
    """Same keying mismatch, partitioned layout."""
    _init(tmp_path)
    parts = tmp_path / "server" / "parquet" / "Jira Issues"
    (parts / "month=2026-01").mkdir(parents=True)
    (parts / "month=2026-01" / "data.parquet").touch()
    shared = tmp_path / ".claude" / "data" / "_shared"
    shared.mkdir(parents=True)
    (shared / "jira_issues.parquet").touch()

    assert _counts(tmp_path, monkeypatch) == (1, 0)


def test_staging_and_empty_dirs_are_not_counted(tmp_path, monkeypatch):
    """An interrupted partitioned sync leaves a `.staging-<tid>` scratch dir
    and can leave an empty `<tid>/`; neither is queryable."""
    _init(tmp_path)
    legacy = tmp_path / "server" / "parquet"
    (legacy / ".staging-orders").mkdir(parents=True)
    (legacy / ".staging-orders" / "data.parquet").touch()
    (legacy / "abandoned").mkdir(parents=True)
    (legacy / "real.parquet").touch()

    assert _counts(tmp_path, monkeypatch) == (1, 0)


def test_live_shaped_workspace_reports_both_numbers(tmp_path, monkeypatch):
    """The shape measured on a real workspace: partitioned legacy tables that
    the old counter reported as 0, plus a `_shared` store with no views."""
    _init(tmp_path)
    legacy = tmp_path / "server" / "parquet"
    for name in ("issues", "comments", "changelog", "attachments", "issuelinks", "remote_links"):
        (legacy / name / "month=2026-08").mkdir(parents=True)
        (legacy / name / "month=2026-08" / "data.parquet").touch()
    shared = tmp_path / ".claude" / "data" / "_shared"
    shared.mkdir(parents=True)
    for i in range(37):
        (shared / f"materialized_{i}.parquet").touch()

    assert _counts(tmp_path, monkeypatch) == (6, 37)
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    out = _clean(runner.invoke(status_app).output)
    assert "Tables    : 6 queryable, 37 downloaded (no local view)" in out


def test_no_second_number_when_everything_is_queryable(tmp_path, monkeypatch):
    """The extra clause only appears when there is something to report."""
    _init(tmp_path)
    legacy = tmp_path / "server" / "parquet"
    legacy.mkdir(parents=True)
    (legacy / "orders.parquet").touch()

    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    out = _clean(runner.invoke(status_app).output)
    assert "Tables    : 1" in out
    assert "no local view" not in out


def test_populated_but_uninitialized_workspace_explains_itself(tmp_path, monkeypatch):
    """`agnes pull` only needs a workspace-SHAPED dir, so data can be present
    with no init sentinel. A bare 'no' beside a populated table count reads as
    a contradiction — name the half that is missing."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    legacy = tmp_path / "server" / "parquet"
    legacy.mkdir(parents=True)
    (legacy / "orders.parquet").touch()

    result = runner.invoke(status_app)
    out = _clean(result.output)
    assert "Tables    : 1" in out
    assert "Initialized: no" in out
    assert "holds data" in out and "never ran here" in out


def test_shared_only_data_gets_the_explanation_not_bootstrap(tmp_path, monkeypatch):
    """A workspace whose data all sits in the stack-sync store has
    queryable == 0, so gating the hint on the queryable count alone told it to
    "bootstrap" one line after reporting dozens of downloaded tables. Reachable
    in practice: every pull creates `analytics.duckdb`, so a directory pulled
    into is workspace-shaped even though `agnes init` never ran in it."""
    (tmp_path / "user" / "duckdb").mkdir(parents=True)
    (tmp_path / "user" / "duckdb" / "analytics.duckdb").touch()  # what pull leaves behind
    shared = tmp_path / ".claude" / "data" / "_shared"
    shared.mkdir(parents=True)
    for i in range(37):
        (shared / f"t{i}.parquet").touch()

    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    out = _clean(runner.invoke(status_app).output)
    assert "Tables    : 0 queryable, 37 downloaded (no local view)" in out
    assert "Initialized: no" in out
    assert "holds data (37 tables)" in out, out
    assert "to bootstrap" not in out, out


def test_auto_discovered_id_resolves_via_sync_state(tmp_path, monkeypatch):
    """Keboola auto-discovery derives the registry id from the fully-qualified
    source id (bucket included), so `id != slug(name)` and stem normalization
    cannot match the trees. The recorded id->name relation in
    `.claude/sync_state.json` can, and without it the same table is counted
    once as queryable AND once as downloaded-without-view."""
    _init(tmp_path)
    legacy = tmp_path / "server" / "parquet"
    legacy.mkdir(parents=True)
    (legacy / "orders.parquet").touch()  # keyed by registry NAME
    shared = tmp_path / ".claude" / "data" / "_shared"
    shared.mkdir(parents=True)
    (shared / "in_c-main_orders.parquet").touch()  # keyed by registry ID

    (tmp_path / ".claude" / "sync_state.json").write_text(
        json.dumps(
            {
                "data_packages": {"pkg": {"orders": {"table_id": "in_c-main_orders", "md5": "x"}}},
                "direct_tables": {},
            }
        ),
        encoding="utf-8",
    )

    assert _counts(tmp_path, monkeypatch) == (1, 0)


def test_missing_sync_state_falls_back_to_slug_matching(tmp_path, monkeypatch):
    """No state file (older CLI) must degrade to the stem heuristic, not fail."""
    _init(tmp_path)
    legacy = tmp_path / "server" / "parquet"
    legacy.mkdir(parents=True)
    (legacy / "Agnes Audit Log.parquet").touch()
    shared = tmp_path / ".claude" / "data" / "_shared"
    shared.mkdir(parents=True)
    (shared / "agnes_audit_log.parquet").touch()

    assert not (tmp_path / ".claude" / "sync_state.json").exists()
    assert _counts(tmp_path, monkeypatch) == (1, 0)


def test_malformed_sync_state_does_not_crash_status(tmp_path, monkeypatch):
    """Unreadable state degrades to the heuristic rather than raising."""
    _init(tmp_path)
    (tmp_path / ".claude" / "sync_state.json").write_text("{not json", encoding="utf-8")
    shared = tmp_path / ".claude" / "data" / "_shared"
    shared.mkdir(parents=True)
    (shared / "orders.parquet").touch()

    assert _counts(tmp_path, monkeypatch) == (0, 1)


@pytest.mark.parametrize(
    "bad_state",
    [
        # The SERVER manifest shape: `direct_tables` / `data_packages` as arrays
        # under the same key names (app/api/sync.py). A state file carrying it
        # used to crash `agnes status` outright.
        {"direct_tables": [{"name": "orders", "table_id": "orders"}], "data_packages": []},
        {"direct_tables": {}, "data_packages": [{"slug": "pkg", "tables": []}]},
        # A package whose value is a list rather than {name: entry}.
        {"data_packages": {"pkg": [{"name": "orders", "table_id": "orders"}]}},
        # Scalars where mappings belong.
        {"direct_tables": "nope", "data_packages": 7},
        # Valid JSON, wrong top-level type.
        [],
    ],
    ids=["manifest-shaped-direct", "manifest-shaped-packages", "list-package", "scalars", "top-level-list"],
)
def test_unexpected_sync_state_shapes_degrade_not_crash(tmp_path, monkeypatch, bad_state):
    """`shared_id_to_name` promises that malformed state yields an empty mapping.
    `agnes status` does not guard the call, so any shape it fails to check
    surfaces as a traceback with exit 1 and NO output — strictly worse than an
    imprecise count for the command an analyst runs to ask what is wrong."""
    _init(tmp_path)
    shared = tmp_path / ".claude" / "data" / "_shared"
    shared.mkdir(parents=True)
    (shared / "orders.parquet").touch()
    (tmp_path / ".claude" / "sync_state.json").write_text(json.dumps(bad_state), encoding="utf-8")

    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    result = runner.invoke(status_app)
    assert result.exception is None, f"crashed: {result.exception!r}"
    assert result.exit_code == 0, result.output
    # Falls back to the slug heuristic: the table is still reported.
    assert "1 downloaded (no local view)" in _clean(result.output), result.output


# ---------------------------------------------------------------------------
# Issue #1312 (remaining scope, item 2): `status` printed `Workspace : <path>`
# from `resolve_data_workspace()` (cwd-first) but silently counted
# `Pending uploads` from the DIFFERENT `workspace_root` anchor — the two
# mixed with no label, so an analyst standing in a foreign-but-shaped
# directory saw an upload count for a workspace never named on screen.
# ---------------------------------------------------------------------------


def test_pending_uploads_labeled_when_anchor_differs_from_workspace(tmp_path, monkeypatch):
    from cli.config import set_workspace_root

    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    set_workspace_root(str(anchor))

    ws = tmp_path / "ws"
    _init(ws)
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(ws))

    result = runner.invoke(status_app)
    assert result.exit_code == 0, result.output
    out = _clean(result.output)
    assert f"Workspace : {ws.resolve()}" in out
    assert str(anchor.resolve()) in out
    assert "(anchor:" in out


def test_pending_uploads_json_carries_anchor(tmp_path, monkeypatch):
    from cli.config import set_workspace_root

    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    set_workspace_root(str(anchor))

    ws = tmp_path / "ws"
    _init(ws)
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(ws))

    result = runner.invoke(status_app, ["--json"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["session_anchor"] == str(anchor.resolve())
    assert body["session_anchor_differs_from_workspace"] is True


def test_pending_uploads_unlabeled_when_anchor_matches_workspace(tmp_path, monkeypatch):
    from cli.config import set_workspace_root

    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
    ws = tmp_path / "ws"
    _init(ws)
    set_workspace_root(str(ws))
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(ws))

    result = runner.invoke(status_app)
    assert result.exit_code == 0, result.output
    out = _clean(result.output)
    assert "(anchor:" not in out

    result_json = runner.invoke(status_app, ["--json"])
    body = json.loads(result_json.output)
    assert body["session_anchor_differs_from_workspace"] is False


def test_pending_uploads_without_configured_workspace_root(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
    ws = tmp_path / "ws"
    _init(ws)
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(ws))

    result = runner.invoke(status_app)
    assert result.exit_code == 0, result.output
    assert "(anchor:" not in _clean(result.output)

    result_json = runner.invoke(status_app, ["--json"])
    body = json.loads(result_json.output)
    assert body["session_anchor"] is None
    assert body["session_anchor_differs_from_workspace"] is False
