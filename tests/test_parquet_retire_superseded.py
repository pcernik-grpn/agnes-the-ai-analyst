"""`src.parquet_publish.retire_superseded_parquet` — the RECLAIM half of the
atomic-publish protocol (#1339).

A table can carry both a flat `data/<table>.parquet` and a `data/<table>/`
partition directory at once (a `sync_strategy: partitioned` flip writes the
parts and leaves the old flat file behind). The partition directory is the
fresher data and now wins everywhere, so the flat sibling has to be removed —
otherwise every reader that does not go through the flipped resolvers can still
find it.

This is DELETE code, and the path it deletes is built from a table name that
arrives from the `table_registry`, which is fed by connector output — untrusted
by the security playbook's definition. So containment is the contract, not a
nicety: every test below that asserts a REFUSAL is asserting that a real file
survived.

The atomicity requirement is the same one `atomic_publish` answers on the write
side: the served name must disappear in ONE `os.replace`, never as a truncated
or half-deleted file, and any residue a hard kill leaves behind must be inert
to every reader's `*.parquet` glob.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.parquet_publish import partition_dir_supersedes_flat, retire_superseded_parquet


@pytest.fixture
def root(tmp_path) -> Path:
    d = tmp_path / "extracts" / "keboola" / "data"
    d.mkdir(parents=True)
    return d


def _at(path: Path, mtime: float) -> Path:
    os.utime(path, (mtime, mtime))
    return path


class TestSupersedesComparator:
    """`partition_dir_supersedes_flat` — the one definition of WHICH layout
    wins, shared by the manifest side and the read surfaces so they cannot
    drift apart.

    The rule is freshness, not file shape: the partition directory wins only
    when at least one of its parts is at least as new as the flat sibling.
    That is what makes the mirror direction (a table flipped BACK to a flat
    write, leaving a stale directory) safe — the fresh flat file keeps
    winning and is never reclaimed.
    """

    def _both(self, root: Path, *, flat_mtime: float, part_mtimes: tuple[float, ...]) -> tuple[Path, Path]:
        flat = root / "orders.parquet"
        flat.write_bytes(b"PAR1flatPAR1")
        _at(flat, flat_mtime)
        table_dir = root / "orders"
        table_dir.mkdir(exist_ok=True)
        for i, m in enumerate(part_mtimes):
            part = table_dir / f"2025_{i + 1:02d}.parquet"
            part.write_bytes(b"PAR1partPAR1")
            _at(part, m)
        return flat, table_dir

    def test_a_fresher_directory_supersedes(self, root):
        flat, table_dir = self._both(root, flat_mtime=1000, part_mtimes=(2000,))
        assert partition_dir_supersedes_flat(flat, table_dir) is True

    def test_a_staler_directory_does_not(self, root):
        """The mirror direction — the whole reason this comparator exists."""
        flat, table_dir = self._both(root, flat_mtime=2000, part_mtimes=(1000, 1500))
        assert partition_dir_supersedes_flat(flat, table_dir) is False

    def test_an_equal_mtime_tie_goes_to_the_directory(self, root):
        """Pins `>=`, not `>`: on a coarse-granularity filesystem a genuine
        flip can land both in the same tick, and a tie must keep the #1339
        behavior rather than silently reverting to flat-wins."""
        flat, table_dir = self._both(root, flat_mtime=2000, part_mtimes=(2000,))
        assert partition_dir_supersedes_flat(flat, table_dir) is True

    def test_one_fresh_part_among_stale_ones_is_enough(self, root):
        """`any(part >= flat)` is equivalent to `max(parts) >= flat`, and the
        early exit is what keeps the reported direction cheap."""
        flat, table_dir = self._both(root, flat_mtime=1500, part_mtimes=(1000, 1200, 9000))
        assert partition_dir_supersedes_flat(flat, table_dir) is True

    def test_an_empty_directory_never_supersedes(self, root):
        flat = root / "orders.parquet"
        flat.write_bytes(b"PAR1flatPAR1")
        (root / "orders").mkdir()
        assert partition_dir_supersedes_flat(flat, root / "orders") is False

    def test_a_nested_hive_part_is_compared_too(self, root):
        flat = root / "issues.parquet"
        flat.write_bytes(b"PAR1flatPAR1")
        _at(flat, 1000)
        month = root / "issues" / "month=2025-11"
        month.mkdir(parents=True)
        part = month / "data.parquet"
        part.write_bytes(b"PAR1partPAR1")
        _at(part, 2000)
        assert partition_dir_supersedes_flat(flat, root / "issues") is True

    def test_no_flat_file_means_nothing_to_arbitrate(self, root):
        table_dir = root / "orders"
        table_dir.mkdir()
        (table_dir / "2025_11.parquet").write_bytes(b"PAR1partPAR1")
        assert partition_dir_supersedes_flat(root / "orders.parquet", table_dir) is False

    def test_a_missing_directory_never_supersedes(self, root):
        flat = root / "orders.parquet"
        flat.write_bytes(b"PAR1flatPAR1")
        assert partition_dir_supersedes_flat(flat, root / "orders") is False


class TestRetires:
    def test_removes_the_superseded_flat_parquet(self, root):
        dest = root / "orders.parquet"
        dest.write_bytes(b"PAR1stalePAR1")

        assert retire_superseded_parquet(dest, root=root) is True
        assert not dest.exists()

    def test_leaves_no_residue_at_all_behind(self, root):
        dest = root / "orders.parquet"
        dest.write_bytes(b"PAR1stalePAR1")

        retire_superseded_parquet(dest, root=root)

        assert list(root.iterdir()) == [], "the reclaim must not leave a temp file behind"

    def test_an_already_absent_file_is_reported_gone_not_failed(self, root):
        """Idempotent: two rebuilds in a row, or a concurrent sweep, must not
        make the caller think the collision persists."""
        assert retire_superseded_parquet(root / "orders.parquet", root=root) is True

    def test_a_sibling_partition_directory_is_untouched(self, root):
        dest = root / "orders.parquet"
        dest.write_bytes(b"PAR1stalePAR1")
        part = root / "orders" / "2025_11.parquet"
        part.parent.mkdir()
        part.write_bytes(b"PAR1freshPAR1")

        retire_superseded_parquet(dest, root=root)

        assert part.read_bytes() == b"PAR1freshPAR1"


class TestAtomicity:
    def test_the_name_disappears_in_one_replace(self, root, monkeypatch):
        """The served name must never be observable in a half-removed state:
        the removal is an `os.replace` onto a name no reader resolves, exactly
        as `atomic_publish_finalize` commits a write."""
        dest = root / "orders.parquet"
        dest.write_bytes(b"PAR1stalePAR1")
        seen: list[tuple[str, str]] = []
        real_replace = os.replace

        def spy(src, dst, **kw):
            seen.append((str(src), str(dst)))
            return real_replace(src, dst, **kw)

        monkeypatch.setattr(os, "replace", spy)
        assert retire_superseded_parquet(dest, root=root) is True
        assert seen and seen[0][0] == str(dest), f"expected an os.replace off {dest}; got {seen!r}"

    def test_residue_from_a_kill_between_replace_and_unlink_is_inert(self, root, monkeypatch):
        """SIGKILL/OOM can land between the replace and the unlink. Whatever is
        left must not be servable: not the resolvable `<table>.parquet` name, and
        not matched by any reader's `*.parquet` glob (`_hash_table_parts`,
        the master view globs, `agnes pull`)."""
        dest = root / "orders.parquet"
        dest.write_bytes(b"PAR1stalePAR1")
        monkeypatch.setattr(Path, "unlink", lambda self, **kw: None)

        retire_superseded_parquet(dest, root=root)

        assert not dest.exists(), "the resolvable name must be gone regardless"
        assert list(root.glob("*.parquet")) == []
        assert list(root.rglob("*.parquet")) == []
        residue = list(root.iterdir())
        assert residue and all(p.name.endswith(".tmp") for p in residue), (
            f"residue must be inert (.tmp, never globbed by a reader); got {[p.name for p in residue]}"
        )

    def test_a_failed_reclaim_of_the_staged_file_still_reports_gone(self, root, monkeypatch):
        """Once the replace lands, the only name a reader resolves is gone —
        the collision IS resolved. Reporting failure because the inert `.tmp`
        residue survived would flag a healthy table forever."""
        dest = root / "orders.parquet"
        dest.write_bytes(b"PAR1stalePAR1")

        def boom(self, **kw):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(Path, "unlink", boom)

        assert retire_superseded_parquet(dest, root=root) is True
        assert not dest.exists()


class TestContainment:
    """Every refusal below is a real file that must survive the call."""

    def test_refuses_a_parent_outside_the_root(self, root, tmp_path):
        victim = tmp_path / "victim.parquet"
        victim.write_bytes(b"customer data")

        assert retire_superseded_parquet(root / ".." / ".." / ".." / "victim.parquet", root=root) is False
        assert victim.exists()

    @pytest.mark.parametrize("evil", ["../orders", "../../etc/orders", "a/b", "orders\x00"])
    def test_refuses_an_unsafe_table_segment(self, root, tmp_path, evil):
        victim = tmp_path / "victim.parquet"
        victim.write_bytes(b"customer data")

        assert retire_superseded_parquet(root / f"{evil}.parquet", root=root) is False
        assert victim.exists()

    @pytest.mark.parametrize("pattern", ["*", "?", "orders[1]"])
    def test_refuses_a_glob_metacharacter_name(self, root, pattern):
        """The name is not only joined into a path — sibling code interpolates
        it into glob patterns. A `*` stops naming one table and starts matching
        an arbitrary one, so it is refused here too rather than relying on
        `os.replace` happening to treat it literally."""
        bystander = root / "orders.parquet"
        bystander.write_bytes(b"PAR1freshPAR1")

        assert retire_superseded_parquet(root / f"{pattern}.parquet", root=root) is False
        assert bystander.exists()

    def test_refuses_a_symlink_pointing_out_of_the_root(self, root, tmp_path):
        """`os.replace` renames the LINK, but a resolver that followed it read
        the target — so a symlink is refused outright rather than chased."""
        victim = tmp_path / "victim.parquet"
        victim.write_bytes(b"customer data")
        link = root / "orders.parquet"
        link.symlink_to(victim)

        assert retire_superseded_parquet(link, root=root) is False
        assert victim.read_bytes() == b"customer data"
        assert link.is_symlink()

    def test_refuses_a_name_that_is_not_a_parquet(self, root):
        other = root / "orders.duckdb"
        other.write_bytes(b"not a parquet")

        assert retire_superseded_parquet(other, root=root) is False
        assert other.exists()

    def test_refuses_a_directory(self, root):
        d = root / "orders.parquet"
        d.mkdir()
        (d / "inner.parquet").write_bytes(b"PAR1")

        assert retire_superseded_parquet(d, root=root) is False
        assert (d / "inner.parquet").exists()

    def test_a_symlinked_data_directory_is_still_a_valid_root(self, tmp_path):
        """Deployment layout, not an escape: an operator may point a source's
        `data/` at another volume. Refusing that would leave every collision
        under it unreclaimed forever."""
        elsewhere = tmp_path / "volume" / "data"
        elsewhere.mkdir(parents=True)
        (elsewhere / "orders.parquet").write_bytes(b"PAR1stalePAR1")
        root = tmp_path / "extracts" / "keboola" / "data"
        root.parent.mkdir(parents=True)
        root.symlink_to(elsewhere, target_is_directory=True)

        assert retire_superseded_parquet(root / "orders.parquet", root=root) is True
        assert not (elsewhere / "orders.parquet").exists()
