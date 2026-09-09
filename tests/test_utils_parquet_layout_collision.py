"""Precedence when a table has BOTH a flat `<table>.parquet` file and a
`<table>/` partition directory at once (#1339).

The FRESHER layout wins, compared by mtime — not the directory unconditionally.
In the direction #1339 reported (a `sync_strategy: partitioned` flip writes the
parts and leaves the previous strategy's flat file behind) the directory is the
fresher one and wins, which is the behavior change: before this, the flat file
won on every read surface — `/api/v2/schema`, `/api/v2/scan`, `/api/v2/sample`
and the catalog all resolve through `resolve_local_parquet_glob` — so a stale
pre-conversion copy was served indefinitely with nothing to say so. In the
mirror direction (flipped BACK to a flat write, stale directory left behind)
the flat file is fresher and keeps winning.

This precedence MUST match `src/orchestrator.py::_update_sync_state`, which
decides what the manifest advertises, so both sites share ONE comparator
(`src/parquet_publish.py::partition_dir_supersedes_flat`). If the two disagree,
the read surfaces and `agnes pull` serve different data — a worse bug than the
one #1339 describes. `tests/test_orchestrator_sync_state_hash.py` pins the
other half.

The read path never DELETES either layout: reclaiming the stale flat sibling
belongs to the rebuild (which holds the rebuild lock and publishes before it
reclaims), not to a hot, concurrent, request-scoped resolver.
"""

import logging
import os

import pytest


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    d = tmp_path / "extracts" / "keboola" / "data"
    d.mkdir(parents=True)
    return d


def _both_layouts(data_dir, *, flat_mtime: float = 1000.0, part_mtime: float = 2000.0):
    """Both layouts for `orders`, mtimes set explicitly — the winner is decided
    by freshness. Default: the part is newer (the direction #1339 reported)."""
    flat = data_dir / "orders.parquet"
    flat.write_bytes(b"flat-bytes")
    part_dir = data_dir / "orders"
    part_dir.mkdir()
    part = part_dir / "2025_11.parquet"
    part.write_bytes(b"partitioned-bytes")
    os.utime(flat, (flat_mtime, flat_mtime))
    os.utime(part, (part_mtime, part_mtime))
    return flat, part_dir


def test_both_layouts_serves_the_partition_directory(data_dir, caplog):
    flat, part_dir = _both_layouts(data_dir)

    from app.utils import resolve_local_parquet_glob

    with caplog.at_level(logging.ERROR, logger="app.utils"):
        result = resolve_local_parquet_glob("orders", "keboola")

    assert result == str(part_dir / "*.parquet"), "the partition directory wins (#1339)"

    collision_records = [
        r
        for r in caplog.records
        if r.levelname == "ERROR"
        and "orders" in r.getMessage()
        and str(flat) in r.getMessage()
        and str(part_dir) in r.getMessage()
    ]
    assert collision_records, (
        "expected an ERROR log naming the table id and BOTH concrete paths; "
        f"got: {[r.getMessage() for r in caplog.records]}"
    )


def test_both_layouts_source_type_agnostic_lookup_also_flips(data_dir, caplog):
    """The `source_type`-omitted (rglob fallback) path must flip too — the
    precedence follows wherever `single` resolved."""
    flat, part_dir = _both_layouts(data_dir)

    from app.utils import resolve_local_parquet_glob

    with caplog.at_level(logging.ERROR, logger="app.utils"):
        result = resolve_local_parquet_glob("orders")

    assert result == str(part_dir / "*.parquet")
    assert [r for r in caplog.records if r.levelname == "ERROR" and "orders" in r.getMessage()]
    assert flat.exists(), "a read surface must never delete anything"


def test_both_layouts_resolves_a_nested_hive_directory_too(data_dir):
    """The winning directory is read with the same flat-then-recursive rule the
    directory-only case uses, so a Jira-shaped `month=…/data.parquet` layout
    wins as a recursive glob rather than resolving to nothing."""
    flat = data_dir / "issues.parquet"
    flat.write_bytes(b"flat-bytes")
    month = data_dir / "issues" / "month=2025-11"
    month.mkdir(parents=True)
    part = month / "data.parquet"
    part.write_bytes(b"partitioned-bytes")
    os.utime(flat, (1000.0, 1000.0))
    os.utime(part, (2000.0, 2000.0))

    from app.utils import resolve_local_parquet_glob

    assert resolve_local_parquet_glob("issues", "keboola") == str(data_dir / "issues" / "**" / "*.parquet")


class TestMirrorDirection:
    """A STALE partition directory beside a FRESHER flat parquet — a table
    flipped back from `sync_strategy: partitioned` to a flat write, leaving the
    old directory behind. As reachable as the direction #1339 reported: no
    writer in the tree removes the other layout in either direction.

    The flat file is the fresher data here, so it wins and nothing is deleted.
    """

    def test_the_fresher_flat_parquet_wins_on_the_read_surfaces(self, data_dir):
        flat, _ = _both_layouts(data_dir, flat_mtime=9000.0, part_mtime=1000.0)

        from app.utils import resolve_local_parquet_glob

        assert resolve_local_parquet_glob("orders", "keboola") == str(flat)

    def test_neither_layout_is_deleted(self, data_dir):
        flat, part_dir = _both_layouts(data_dir, flat_mtime=9000.0, part_mtime=1000.0)

        from app.utils import local_parquet_size_bytes, resolve_local_parquet_glob

        resolve_local_parquet_glob("orders", "keboola")
        local_parquet_size_bytes("orders", "keboola")

        assert flat.exists(), "the fresher flat parquet must survive"
        assert (part_dir / "2025_11.parquet").exists()

    def test_the_size_hint_follows_the_flat_file(self, data_dir):
        """The resolver and the size helper are a pair — in this direction both
        must describe the flat file, or the catalog advertises the stale
        directory's size for data the read surfaces serve from the flat file."""
        _both_layouts(data_dir, flat_mtime=9000.0, part_mtime=1000.0)

        from app.utils import local_parquet_size_bytes

        assert local_parquet_size_bytes("orders", "keboola") == len(b"flat-bytes")

    def test_the_collision_is_still_disclosed(self, data_dir, caplog):
        """Detection shipped in v0.83.25 (#1340) and must not regress: this
        direction is NOT self-healing (only the flat sibling is ever
        reclaimable), so it has to keep naming both paths."""
        flat, part_dir = _both_layouts(data_dir, flat_mtime=9000.0, part_mtime=1000.0)

        from app.utils import resolve_local_parquet_glob

        with caplog.at_level(logging.ERROR, logger="app.utils"):
            resolve_local_parquet_glob("orders", "keboola")

        assert [
            r
            for r in caplog.records
            if r.levelname == "ERROR" and str(flat) in r.getMessage() and str(part_dir) in r.getMessage()
        ], f"expected an ERROR naming both paths; got {[r.getMessage() for r in caplog.records]}"


def test_an_equal_mtime_tie_goes_to_the_partition_directory(data_dir):
    """Pins `>=`, not `>`: a genuine flip can land both in the same filesystem
    tick, and a tie must keep the #1339 behavior."""
    _, part_dir = _both_layouts(data_dir, flat_mtime=2000.0, part_mtime=2000.0)

    from app.utils import resolve_local_parquet_glob

    assert resolve_local_parquet_glob("orders", "keboola") == str(part_dir / "*.parquet")


class TestProfileTargetFollowsTheSameComparator:
    """`resolve_local_layout_target` is the Path-returning form the profiler
    needs (`app/api/catalog.py::refresh_profile` passes a file OR a directory
    to `profile_table`). It was the third precedence site, unflipped; it now
    calls the same comparator, so a manual profile refresh cannot compute
    statistics from the layout the read surfaces do not serve."""

    def test_the_fresher_directory_is_the_profile_target(self, data_dir):
        _, part_dir = _both_layouts(data_dir)

        from app.utils import resolve_local_layout_target

        assert resolve_local_layout_target("orders") == part_dir

    def test_the_fresher_flat_file_is_the_profile_target(self, data_dir):
        flat, _ = _both_layouts(data_dir, flat_mtime=9000.0, part_mtime=1000.0)

        from app.utils import resolve_local_layout_target

        assert resolve_local_layout_target("orders") == flat

    def test_flat_only_and_dir_only_are_unchanged(self, data_dir):
        from app.utils import resolve_local_layout_target

        flat = data_dir / "solo.parquet"
        flat.write_bytes(b"flat-bytes")
        assert resolve_local_layout_target("solo") == flat

        part_dir = data_dir / "parted"
        part_dir.mkdir()
        (part_dir / "2025_11.parquet").write_bytes(b"partitioned-bytes")
        assert resolve_local_layout_target("parted") == part_dir

        assert resolve_local_layout_target("nothing_here") is None

    def test_a_partition_dir_under_ANOTHER_source_does_not_compete(self, tmp_path, monkeypatch):
        """Cross-source resolution stays exactly as it was: the comparator only
        ever arbitrates a TRUE sibling (same `data/` directory). A same-named
        directory under a different source is a different table's storage, not
        a fresher copy of this one."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        a = tmp_path / "extracts" / "srcA" / "data"
        a.mkdir(parents=True)
        flat = a / "orders.parquet"
        flat.write_bytes(b"flat-bytes")
        os.utime(flat, (1000.0, 1000.0))
        b = tmp_path / "extracts" / "srcB" / "data" / "orders"
        b.mkdir(parents=True)
        other = b / "2025_11.parquet"
        other.write_bytes(b"partitioned-bytes")
        os.utime(other, (9000.0, 9000.0))

        from app.utils import resolve_local_layout_target

        assert resolve_local_layout_target("orders") == flat


def test_the_read_path_does_not_delete_the_stale_sibling(data_dir):
    """Reclaiming the loser is the rebuild's job. A request-scoped resolver
    runs concurrently with everything, holds no lock, and can be called for a
    table mid-extract — deleting from here is how you unlink a file another
    request is about to open."""
    flat, _ = _both_layouts(data_dir)

    from app.utils import resolve_local_parquet_glob

    resolve_local_parquet_glob("orders", "keboola")

    assert flat.exists()


def test_an_empty_sibling_directory_does_not_shadow_the_flat_file(data_dir, caplog):
    """A partition directory holding no part yet is not fresher data — it is
    the pending-first-sync case. Letting it win would resolve a healthy
    single-file table to a glob that matches nothing."""
    flat = data_dir / "orders.parquet"
    flat.write_bytes(b"flat-bytes")
    (data_dir / "orders").mkdir()

    from app.utils import resolve_local_parquet_glob

    assert resolve_local_parquet_glob("orders", "keboola") == str(flat)


def test_a_symlinked_sibling_directory_is_not_followed(data_dir, tmp_path):
    """Containment: a `<table>/` symlink pointing out of the extracts tree must
    not become the served target."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.parquet").write_bytes(b"evil")
    flat = data_dir / "orders.parquet"
    flat.write_bytes(b"flat-bytes")
    (data_dir / "orders").symlink_to(outside, target_is_directory=True)

    from app.utils import resolve_local_parquet_glob

    assert resolve_local_parquet_glob("orders", "keboola") == str(flat)


def test_flat_only_is_unchanged(data_dir, caplog):
    """Regression pin: the ordinary single-file case — the common one — must
    keep behaving exactly as before."""
    flat = data_dir / "orders.parquet"
    flat.write_bytes(b"flat-bytes")

    from app.utils import resolve_local_parquet_glob

    with caplog.at_level(logging.ERROR, logger="app.utils"):
        result = resolve_local_parquet_glob("orders", "keboola")

    assert result == str(flat)
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def test_dir_only_is_unchanged(data_dir, caplog):
    """Regression pin: the ordinary partitioned-only case (no flat sibling)
    must keep behaving exactly as before either."""
    part_dir = data_dir / "orders"
    part_dir.mkdir()
    (part_dir / "2025_11.parquet").write_bytes(b"partitioned-bytes")

    from app.utils import resolve_local_parquet_glob

    with caplog.at_level(logging.ERROR, logger="app.utils"):
        result = resolve_local_parquet_glob("orders", "keboola")

    assert result == str(part_dir / "*.parquet")
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


class TestSizeHintFollowsTheSameWinner:
    """`local_parquet_size_bytes` and `resolve_local_parquet_glob` are a pair —
    its own docstring says they must agree on what "this table's data" means.
    Left unflipped, the catalog would publish the STALE flat file's size for a
    table every read surface now serves from the directory."""

    def test_both_layouts_sums_the_partition_directory(self, data_dir):
        _both_layouts(data_dir)

        from app.utils import local_parquet_size_bytes

        assert local_parquet_size_bytes("orders", "keboola") == len(b"partitioned-bytes")

    def test_flat_only_still_reports_the_files_size(self, data_dir):
        flat = data_dir / "orders.parquet"
        flat.write_bytes(b"flat-bytes")

        from app.utils import local_parquet_size_bytes

        assert local_parquet_size_bytes("orders", "keboola") == len(b"flat-bytes")

    def test_an_empty_sibling_directory_still_reports_the_flat_size(self, data_dir):
        flat = data_dir / "orders.parquet"
        flat.write_bytes(b"flat-bytes")
        (data_dir / "orders").mkdir()

        from app.utils import local_parquet_size_bytes

        assert local_parquet_size_bytes("orders", "keboola") == len(b"flat-bytes")


class TestReadSurfacesServeTheDirectorysData:
    """Value-level proof through the surfaces themselves, not just the resolver:
    the flat file and the directory carry DIFFERENT data, and what comes back
    must be the directory's."""

    def _row(self, table_id: str) -> dict:
        return {
            "id": table_id,
            "source_type": "keboola",
            "query_mode": "local",
            "bucket": "in.c-main",
            "source_table": table_id,
        }

    def _write_divergent_layouts(self, data_dir, table_id: str):
        pa = pytest.importorskip("pyarrow")
        pq = pytest.importorskip("pyarrow.parquet")
        pq.write_table(pa.table({"stale_col": [1, 2]}), data_dir / f"{table_id}.parquet")
        part_dir = data_dir / table_id
        part_dir.mkdir()
        pq.write_table(pa.table({"fresh_col": [7, 8, 9]}), part_dir / "2025_11.parquet")

    def test_schema_reports_the_directorys_columns(self, data_dir):
        self._write_divergent_layouts(data_dir, "kbc_sales")

        from app.api.v2_schema import build_schema_uncached

        payload = build_schema_uncached(conn=None, table_id="kbc_sales", bq=object(), row=self._row("kbc_sales"))

        assert {c["name"] for c in payload["columns"]} == {"fresh_col"}
