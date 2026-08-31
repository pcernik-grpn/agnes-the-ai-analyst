"""Resolver matrix for cli/lib/workspace_resolve.py (spec §5.1).

Precedence: AGNES_LOCAL_DIR env (always wins, even unshaped) →
cwd-if-shaped → workspace_root-if-shaped → None. A stale anchor
(deleted dir / unshaped) degrades to None, never to a bogus path.
"""

from pathlib import Path

import pytest

from cli.lib.workspace_resolve import is_workspace_shaped, resolve_data_workspace


def _make_shaped(p: Path, marker: str = "sentinel") -> Path:
    p.mkdir(parents=True, exist_ok=True)
    if marker == "sentinel":
        (p / ".claude").mkdir(parents=True, exist_ok=True)
        (p / ".claude" / "init-complete").write_text("x", encoding="utf-8")
    elif marker == "duckdb":
        (p / "user" / "duckdb").mkdir(parents=True, exist_ok=True)
        (p / "user" / "duckdb" / "analytics.duckdb").write_bytes(b"")
    elif marker == "parquet":
        (p / "server" / "parquet").mkdir(parents=True, exist_ok=True)
    return p


@pytest.mark.parametrize("marker", ["sentinel", "duckdb", "parquet"])
def test_is_workspace_shaped_markers(tmp_path, marker):
    assert is_workspace_shaped(_make_shaped(tmp_path / "ws", marker)) is True


def test_is_workspace_shaped_negative(tmp_path):
    plain = tmp_path / "repo"
    plain.mkdir()
    assert is_workspace_shaped(plain) is False


def test_env_wins_even_when_unshaped(tmp_path, monkeypatch):
    target = tmp_path / "env-target"
    target.mkdir()
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(target))
    monkeypatch.chdir(_make_shaped(tmp_path / "cwd-ws"))
    assert resolve_data_workspace() == target.resolve()


def test_cwd_shaped_beats_anchor(tmp_path, monkeypatch):
    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
    anchor = _make_shaped(tmp_path / "anchor")
    monkeypatch.setattr("cli.lib.workspace_resolve.get_workspace_root", lambda: str(anchor))
    cwd = _make_shaped(tmp_path / "cwd-ws")
    monkeypatch.chdir(cwd)
    assert resolve_data_workspace() == cwd.resolve()


def test_anchor_used_when_cwd_unshaped(tmp_path, monkeypatch):
    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
    anchor = _make_shaped(tmp_path / "anchor")
    monkeypatch.setattr("cli.lib.workspace_resolve.get_workspace_root", lambda: str(anchor))
    plain = tmp_path / "foreign-repo"
    plain.mkdir()
    monkeypatch.chdir(plain)
    assert resolve_data_workspace() == anchor.resolve()


def test_stale_anchor_degrades_to_none(tmp_path, monkeypatch):
    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
    monkeypatch.setattr(
        "cli.lib.workspace_resolve.get_workspace_root",
        lambda: str(tmp_path / "deleted-anchor"),
    )
    plain = tmp_path / "foreign-repo"
    plain.mkdir()
    monkeypatch.chdir(plain)
    assert resolve_data_workspace() is None


def test_no_signals_none(tmp_path, monkeypatch):
    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
    monkeypatch.setattr("cli.lib.workspace_resolve.get_workspace_root", lambda: None)
    plain = tmp_path / "foreign-repo"
    plain.mkdir()
    monkeypatch.chdir(plain)
    assert resolve_data_workspace() is None


def test_precedence_differs_from_update_resolver(tmp_path, monkeypatch):
    """Pin that the two resolvers are DELIBERATELY different (spec §5.1/§10):

    data reads prefer the workspace you stand in (cwd before anchor);
    `agnes update` converges the anchor (anchor before cwd). A dedup
    refactor collapsing them would change foreign-repo-safety behavior.
    """
    from cli.commands.update import _resolve_workspace as update_resolve

    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
    anchor = _make_shaped(tmp_path / "anchor")
    cwd = _make_shaped(tmp_path / "cwd-ws")  # sentinel marker => update sees it too
    monkeypatch.setattr("cli.lib.workspace_resolve.get_workspace_root", lambda: str(anchor))
    monkeypatch.setattr("cli.commands.update.get_workspace_root", lambda: str(anchor))
    monkeypatch.chdir(cwd)
    assert resolve_data_workspace() == cwd.resolve()  # cwd first
    assert update_resolve() == anchor.resolve()  # anchor first


def test_query_run_local_falls_back_to_anchor(tmp_path, monkeypatch, capsys):
    """From an unshaped cwd, _run_local opens the ANCHOR's DuckDB (spec §5.2)."""
    import duckdb

    from cli.commands import query as query_module

    anchor = tmp_path / "anchor"
    (anchor / "user" / "duckdb").mkdir(parents=True)
    con = duckdb.connect(str(anchor / "user" / "duckdb" / "analytics.duckdb"))
    con.execute("CREATE TABLE t AS SELECT 42 AS answer")
    con.close()

    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
    monkeypatch.setattr("cli.lib.workspace_resolve.get_workspace_root", lambda: str(anchor))
    plain = tmp_path / "foreign-repo"
    plain.mkdir()
    monkeypatch.chdir(plain)

    # Captured via capsys, not a patched typer.echo: since #1801 the csv
    # branch of _output() writes through csv.writer(sys.stdout) directly,
    # so typer.echo never sees the rows. This test's subject is the
    # workspace fallback, not the output transport.
    query_module._run_local("SELECT answer FROM t", fmt="csv", limit=10)
    assert "42" in capsys.readouterr().out


def test_query_run_local_none_raises_missing(tmp_path, monkeypatch):
    from cli.commands import query as query_module

    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
    monkeypatch.setattr("cli.lib.workspace_resolve.get_workspace_root", lambda: None)
    plain = tmp_path / "foreign-repo"
    plain.mkdir()
    monkeypatch.chdir(plain)
    with pytest.raises(query_module._LocalDbMissing):
        query_module._run_local("SELECT 1", fmt="csv", limit=10)


def test_pull_refuses_to_scaffold_foreign_cwd(tmp_path, monkeypatch):
    """No workspace anywhere -> typed error, NOTHING written into cwd (§5.2 + §8 guard)."""
    from typer.testing import CliRunner

    from cli.commands.pull import pull_app

    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
    monkeypatch.setattr("cli.lib.workspace_resolve.get_workspace_root", lambda: None)
    monkeypatch.setenv("AGNES_SERVER", "http://localhost:9")  # never reached
    monkeypatch.setenv("AGNES_TOKEN", "t")
    plain = tmp_path / "foreign-repo"
    plain.mkdir()
    monkeypatch.chdir(plain)

    result = CliRunner().invoke(pull_app, [])
    assert result.exit_code == 1
    assert "agnes init" in result.output
    assert not (plain / "server").exists()
    assert not (plain / "user").exists()


def test_pull_workspace_flag_wins(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from cli.commands import pull as pull_module

    target = tmp_path / "explicit-ws"
    target.mkdir()
    seen: dict = {}

    def fake_run_pull(server_url, token, workspace, **kw):
        seen["workspace"] = Path(workspace)

        class R:  # minimal PullResult stand-in: --quiet path reads only .errors
            errors: list = []

        return R()

    monkeypatch.setattr(pull_module, "run_pull", fake_run_pull)
    monkeypatch.setenv("AGNES_SERVER", "http://localhost:9")
    monkeypatch.setenv("AGNES_TOKEN", "t")
    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)

    result = CliRunner().invoke(pull_module.pull_app, ["--workspace", str(target), "--quiet"])
    assert result.exit_code == 0, result.output
    assert seen["workspace"] == target.resolve()


def test_snapshot_local_dir_uses_anchor(tmp_path, monkeypatch):
    from cli.commands import snapshot as snapshot_module

    anchor = _make_shaped(tmp_path / "anchor")
    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
    monkeypatch.setattr("cli.lib.workspace_resolve.get_workspace_root", lambda: str(anchor))
    plain = tmp_path / "foreign-repo"
    plain.mkdir()
    monkeypatch.chdir(plain)
    assert snapshot_module._local_dir() == anchor.resolve()


def test_disk_info_local_dir_falls_back_to_cwd_when_nothing(tmp_path, monkeypatch):
    from cli.commands import disk_info as disk_info_module

    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
    monkeypatch.setattr("cli.lib.workspace_resolve.get_workspace_root", lambda: None)
    plain = tmp_path / "foreign-repo"
    plain.mkdir()
    monkeypatch.chdir(plain)
    assert disk_info_module._local_dir() == plain.resolve()


def test_mcp_query_local_uses_anchor(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    import duckdb

    from cli.mcp import server as mcp_server

    anchor = tmp_path / "anchor"
    (anchor / "user" / "duckdb").mkdir(parents=True)
    con = duckdb.connect(str(anchor / "user" / "duckdb" / "analytics.duckdb"))
    con.execute("CREATE TABLE t AS SELECT 7 AS n")
    con.close()

    monkeypatch.delenv("AGNES_LOCAL_DIR", raising=False)
    monkeypatch.setattr("cli.lib.workspace_resolve.get_workspace_root", lambda: str(anchor))
    plain = tmp_path / "spawned-at-home"
    plain.mkdir()
    monkeypatch.chdir(plain)

    fn = getattr(mcp_server.query_local, "fn", mcp_server.query_local)
    out = fn("SELECT n FROM t")
    assert out["rows"] == [[7]]
