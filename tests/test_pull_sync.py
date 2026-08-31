"""Tests for ``cli/lib/pull_sync.py`` — per-type sync engine (Phase 7,
Task 7.5).

Covers Section 10.3 of the unified-stack spec:

  - First pull from empty.
  - Add package with overlap (shared parquet reused).
  - Remove package no overlap.
  - Remove package with overlap (shared parquet retained).
  - MD5 update.
  - Idempotent re-pull.
  - Orphan parquet detection.
  - Broken symlink auto-heal.
  - Windows fallback (symlink → hardlink → copy strategy tracking).
  - Memory bundle write + md5 short-circuit.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Dict, List

import pytest

from cli.lib.pull_sync import (
    PullStackOptions,
    _link_or_copy,
    _safe_segment,
    audit_invariants,
    run_stack_sync,
    sync_data_packages,
    sync_direct_tables,
    sync_memory_domains,
)


# ---------------------------------------------------------------------------
# Fake server fixtures
# ---------------------------------------------------------------------------


class _FakeServer:
    """In-memory parquet + bundle catalog. ``fetcher`` writes bytes; the
    canonical "parquet body" is just ``b"PAR1" + table_id`` so md5s differ
    per id deterministically."""

    def __init__(self):
        self.fetch_calls: List[tuple] = []
        self.bundle_calls: List[str] = []
        # Map url → bytes; if missing, the fetcher uses a stub based on the
        # last path segment.
        self.responses: Dict[str, bytes] = {}
        self.bundles: Dict[str, bytes] = {}
        self.fail_url: str = ""

    def make_fetcher(self):
        def _fetcher(url: str, target: Path) -> None:
            self.fetch_calls.append((url, str(target)))
            if url == self.fail_url:
                raise RuntimeError("fetch failed")
            body = self.responses.get(url) or (b"PAR1" + url.encode())
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)

        return _fetcher

    def make_bundle_fetcher(self):
        def _bundle_fetcher(slug: str) -> bytes:
            self.bundle_calls.append(slug)
            return self.bundles.get(slug, f"# {slug} bundle\n".encode())

        return _bundle_fetcher

    def make_md5(self):
        def _md5(p: Path) -> str:
            return hashlib.md5(Path(p).read_bytes()).hexdigest()

        return _md5


def _table(id_: str, name: str, md5: str = "", query_mode: str = "local", server_only: bool = False) -> dict:
    return {
        "id": id_,
        "name": name,
        "md5": md5 or hashlib.md5((b"PAR1" + f"/api/data/{id_}/download".encode())).hexdigest(),
        "query_mode": query_mode,
        "server_only": server_only,
        "parquet_url": f"/api/data/{id_}/download",
    }


@pytest.fixture
def server():
    return _FakeServer()


@pytest.fixture
def local_dir(tmp_path):
    return tmp_path / "local"


# ---------------------------------------------------------------------------
# Sync — direct tables
# ---------------------------------------------------------------------------


class TestSyncDirectTables:
    def test_first_pull_writes_shared_and_reference(self, server, local_dir):
        local_dir.mkdir(parents=True, exist_ok=True)
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        tables = [_table("t1", "orders")]
        state, report = sync_direct_tables(
            server_tables=tables,
            local_data_dir=local_data,
            prev_state={},
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        assert report.added == 1
        assert report.removed == 0
        assert (local_data / "_shared" / "t1.parquet").exists()
        assert (local_data / "_direct" / "orders.parquet").exists()
        assert state["orders"]["table_id"] == "t1"
        assert state["orders"]["strategy"] in ("symlink", "hardlink", "copy")

    def test_idempotent_repull_no_fetch(self, server, local_dir):
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        tables = [_table("t1", "orders")]
        fetcher = server.make_fetcher()
        md5 = server.make_md5()
        state1, _ = sync_direct_tables(
            server_tables=tables,
            local_data_dir=local_data,
            prev_state={},
            fetcher=fetcher,
            md5_of=md5,
        )
        assert len(server.fetch_calls) == 1
        state2, report = sync_direct_tables(
            server_tables=tables,
            local_data_dir=local_data,
            prev_state=state1,
            fetcher=fetcher,
            md5_of=md5,
        )
        # No new fetch on idempotent re-pull.
        assert len(server.fetch_calls) == 1
        assert report.added == 0
        assert report.updated == 0
        assert report.removed == 0

    def test_md5_change_refetches(self, server, local_dir):
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        t = _table("t1", "orders")
        fetcher = server.make_fetcher()
        md5 = server.make_md5()
        state1, _ = sync_direct_tables(
            server_tables=[t],
            local_data_dir=local_data,
            prev_state={},
            fetcher=fetcher,
            md5_of=md5,
        )
        # Server flips md5 + payload.
        t2 = dict(t)
        t2["md5"] = "newhash"
        server.responses[t2["parquet_url"]] = b"PAR1" + b"\x99" + b"new payload"

        def _md5_aware(p: Path) -> str:
            content = Path(p).read_bytes()
            if content.startswith(b"PAR1" + b"\x99"):
                return "newhash"
            return hashlib.md5(content).hexdigest()

        state2, report = sync_direct_tables(
            server_tables=[t2],
            local_data_dir=local_data,
            prev_state=state1,
            fetcher=fetcher,
            md5_of=_md5_aware,
        )
        assert report.updated == 1
        assert report.added == 0
        # Two fetches total now.
        assert len(server.fetch_calls) == 2

    def test_remove_drops_reference_and_shared(self, server, local_dir):
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        t = _table("t1", "orders")
        state1, _ = sync_direct_tables(
            server_tables=[t],
            local_data_dir=local_data,
            prev_state={},
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        # Server drops the table.
        state2, report = sync_direct_tables(
            server_tables=[],
            local_data_dir=local_data,
            prev_state=state1,
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        assert report.removed == 1
        assert not (local_data / "_direct" / "orders.parquet").exists()
        # No other reference → shared parquet also removed.
        assert not (local_data / "_shared" / "t1.parquet").exists()

    def test_remote_mode_tables_skipped(self, server, local_dir):
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        t = _table("t_remote", "remote_tbl", query_mode="remote")
        state, report = sync_direct_tables(
            server_tables=[t],
            local_data_dir=local_data,
            prev_state={},
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        assert report.added == 0
        assert server.fetch_calls == []
        assert "remote_tbl" not in state

    def test_server_only_tables_skipped(self, server, local_dir):
        """#1324: a server_only row must never be fetched into
        `.claude/data/_shared` by the typed stack-sync path — mirrors the
        flat-`tables` step 4 skip in `cli/lib/pull.py`."""
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        t = _table("t_so", "server_only_tbl", server_only=True)
        state, report = sync_direct_tables(
            server_tables=[t],
            local_data_dir=local_data,
            prev_state={},
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        assert report.added == 0
        assert server.fetch_calls == []
        assert "server_only_tbl" not in state
        assert not (local_data / "_shared" / "t_so.parquet").exists()

    def test_skip_materialize_omits_materialized_tables(self, server, local_dir):
        """#1304: `skip_materialize=True` must omit `query_mode='materialized'`
        rows from the typed stack-sync path too, not just step 4's flat dict."""
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        t = _table("t_mat", "big_materialized", query_mode="materialized")
        state, report = sync_direct_tables(
            server_tables=[t],
            local_data_dir=local_data,
            prev_state={},
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
            skip_materialize=True,
        )
        assert report.added == 0
        assert server.fetch_calls == []
        assert "big_materialized" not in state

    def test_materialized_tables_sync_by_default(self, server, local_dir):
        """Counterpart: `skip_materialize` defaults to False, so a
        materialized table syncs exactly like any other local-mode table
        unless the caller opts in."""
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        t = _table("t_mat", "big_materialized", query_mode="materialized")
        state, report = sync_direct_tables(
            server_tables=[t],
            local_data_dir=local_data,
            prev_state={},
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        assert report.added == 1
        assert (local_data / "_direct" / "big_materialized.parquet").exists()
        assert "big_materialized" in state


# ---------------------------------------------------------------------------
# Sync — data packages
# ---------------------------------------------------------------------------


class TestSyncDataPackages:
    def test_two_packages_with_overlap_share_parquet(self, server, local_dir):
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        shared_table = _table("t_cust", "customers")
        pkg_sales = {
            "slug": "sales-bundle",
            "tables": [_table("t_orders", "orders"), shared_table],
        }
        pkg_marketing = {
            "slug": "marketing-bundle",
            "tables": [shared_table, _table("t_camp", "campaigns")],
        }
        state, report = sync_data_packages(
            server_packages=[pkg_sales, pkg_marketing],
            local_data_dir=local_data,
            prev_state={},
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        # 3 unique parquets fetched (customers, orders, campaigns).
        unique_urls = {c[0] for c in server.fetch_calls}
        assert len(unique_urls) == 3
        # Both packages have a customers.parquet reference.
        assert (local_data / "sales-bundle" / "customers.parquet").exists()
        assert (local_data / "marketing-bundle" / "customers.parquet").exists()
        # Shared store has 3 entries.
        shared_files = list((local_data / "_shared").iterdir())
        assert len(shared_files) == 3
        # Report sums: 4 added (orders + customers in sales, customers + campaigns in mkt = 4 refs).
        assert report.added == 4

    def test_remove_package_with_overlap_keeps_shared(self, server, local_dir):
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        shared_table = _table("t_cust", "customers")
        pkg_sales = {
            "slug": "sales-bundle",
            "tables": [shared_table, _table("t_orders", "orders")],
        }
        pkg_marketing = {
            "slug": "marketing-bundle",
            "tables": [shared_table],
        }
        state1, _ = sync_data_packages(
            server_packages=[pkg_sales, pkg_marketing],
            local_data_dir=local_data,
            prev_state={},
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        # Remove sales but keep marketing — customers must stay because
        # marketing still references it.
        state2, report = sync_data_packages(
            server_packages=[pkg_marketing],
            local_data_dir=local_data,
            prev_state=state1,
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        assert report.removed == 2  # orders + customers references in sales
        # Customer's _shared parquet still exists (referenced from marketing).
        assert (local_data / "_shared" / "t_cust.parquet").exists()
        # Orders' _shared parquet is gone (no other reference).
        assert not (local_data / "_shared" / "t_orders.parquet").exists()
        # Sales package dir is empty (and removed).
        assert not (local_data / "sales-bundle").exists()
        # Marketing kept its reference.
        assert (local_data / "marketing-bundle" / "customers.parquet").exists()

    def test_remove_package_no_overlap_drops_all(self, server, local_dir):
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        pkg = {
            "slug": "sales-bundle",
            "tables": [_table("t1", "orders"), _table("t2", "customers")],
        }
        state1, _ = sync_data_packages(
            server_packages=[pkg],
            local_data_dir=local_data,
            prev_state={},
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        state2, report = sync_data_packages(
            server_packages=[],
            local_data_dir=local_data,
            prev_state=state1,
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        assert report.removed == 2
        assert not (local_data / "_shared" / "t1.parquet").exists()
        assert not (local_data / "_shared" / "t2.parquet").exists()

    def test_idempotent_package_repull(self, server, local_dir):
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        pkg = {"slug": "sales", "tables": [_table("t1", "orders")]}
        fetcher = server.make_fetcher()
        state1, _ = sync_data_packages(
            server_packages=[pkg],
            local_data_dir=local_data,
            prev_state={},
            fetcher=fetcher,
            md5_of=server.make_md5(),
        )
        assert len(server.fetch_calls) == 1
        state2, report = sync_data_packages(
            server_packages=[pkg],
            local_data_dir=local_data,
            prev_state=state1,
            fetcher=fetcher,
            md5_of=server.make_md5(),
        )
        assert len(server.fetch_calls) == 1
        assert report.added + report.updated + report.removed == 0

    def test_server_only_tables_skipped(self, server, local_dir):
        """#1324: same server_only skip as direct_tables, for a packaged table."""
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        pkg = {"slug": "sales", "tables": [_table("t_so", "server_only_tbl", server_only=True)]}
        state, report = sync_data_packages(
            server_packages=[pkg],
            local_data_dir=local_data,
            prev_state={},
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        assert report.added == 0
        assert server.fetch_calls == []
        assert state["sales"] == {}
        assert not (local_data / "_shared" / "t_so.parquet").exists()
        assert not (local_data / "sales" / "server_only_tbl.parquet").exists()

    def test_skip_materialize_omits_materialized_tables(self, server, local_dir):
        """#1304: `skip_materialize=True` reaches the data_packages loop too."""
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        pkg = {"slug": "sales", "tables": [_table("t_mat", "big_materialized", query_mode="materialized")]}
        state, report = sync_data_packages(
            server_packages=[pkg],
            local_data_dir=local_data,
            prev_state={},
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
            skip_materialize=True,
        )
        assert report.added == 0
        assert server.fetch_calls == []
        assert state["sales"] == {}

    def test_materialized_tables_sync_by_default(self, server, local_dir):
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        pkg = {"slug": "sales", "tables": [_table("t_mat", "big_materialized", query_mode="materialized")]}
        state, report = sync_data_packages(
            server_packages=[pkg],
            local_data_dir=local_data,
            prev_state={},
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        assert report.added == 1
        assert (local_data / "sales" / "big_materialized.parquet").exists()
        assert "big_materialized" in state["sales"]


# ---------------------------------------------------------------------------
# Sync — memory domains
# ---------------------------------------------------------------------------


class TestSyncMemoryDomains:
    def test_first_pull_writes_bundle(self, server, local_dir):
        mem = local_dir / "memory"
        domain = {"slug": "sales-playbook", "md5": "h1"}
        state, report = sync_memory_domains(
            server_domains=[domain],
            local_memory_dir=mem,
            prev_state={},
            bundle_fetcher=server.make_bundle_fetcher(),
        )
        assert report.added == 1
        assert (mem / "sales-playbook" / "bundle.md").exists()
        assert state["sales-playbook"]["md5"] == "h1"

    def test_idempotent_skips_refetch(self, server, local_dir):
        mem = local_dir / "memory"
        domain = {"slug": "sales-playbook", "md5": "h1"}
        bundle = server.make_bundle_fetcher()
        state1, _ = sync_memory_domains(
            server_domains=[domain],
            local_memory_dir=mem,
            prev_state={},
            bundle_fetcher=bundle,
        )
        assert server.bundle_calls == ["sales-playbook"]
        state2, report = sync_memory_domains(
            server_domains=[domain],
            local_memory_dir=mem,
            prev_state=state1,
            bundle_fetcher=bundle,
        )
        assert len(server.bundle_calls) == 1  # no re-fetch
        assert report.added + report.updated == 0

    def test_md5_change_refetches(self, server, local_dir):
        mem = local_dir / "memory"
        bundle = server.make_bundle_fetcher()
        state1, _ = sync_memory_domains(
            server_domains=[{"slug": "x", "md5": "old"}],
            local_memory_dir=mem,
            prev_state={},
            bundle_fetcher=bundle,
        )
        state2, report = sync_memory_domains(
            server_domains=[{"slug": "x", "md5": "new"}],
            local_memory_dir=mem,
            prev_state=state1,
            bundle_fetcher=bundle,
        )
        assert report.updated == 1
        assert len(server.bundle_calls) == 2

    def test_remove_unlinks_bundle(self, server, local_dir):
        mem = local_dir / "memory"
        bundle = server.make_bundle_fetcher()
        state1, _ = sync_memory_domains(
            server_domains=[{"slug": "x", "md5": "h"}],
            local_memory_dir=mem,
            prev_state={},
            bundle_fetcher=bundle,
        )
        state2, report = sync_memory_domains(
            server_domains=[],
            local_memory_dir=mem,
            prev_state=state1,
            bundle_fetcher=bundle,
        )
        assert report.removed == 1
        assert not (mem / "x" / "bundle.md").exists()


# ---------------------------------------------------------------------------
# Windows symlink fallback
# ---------------------------------------------------------------------------


class TestWindowsFallback:
    def test_symlink_fallback_to_hardlink(self, server, local_dir, monkeypatch):
        """When os.symlink raises, _link_or_copy must try os.link."""
        local_data = local_dir / "data"
        (local_data / "_shared").mkdir(parents=True)
        src = local_data / "_shared" / "t1.parquet"
        src.write_bytes(b"PAR1" + b"x")
        dst = local_data / "pkg" / "alias.parquet"

        monkeypatch.setattr("cli.lib.pull_sync.os.symlink", lambda *a, **kw: (_ for _ in ()).throw(OSError("nope")))
        strategy = _link_or_copy(src, dst)
        assert strategy == "hardlink"
        assert dst.exists()
        # Same inode = real hardlink.
        assert dst.stat().st_ino == src.stat().st_ino

    def test_symlink_and_hardlink_fail_falls_back_to_copy(
        self,
        server,
        local_dir,
        monkeypatch,
    ):
        local_data = local_dir / "data"
        (local_data / "_shared").mkdir(parents=True)
        src = local_data / "_shared" / "t1.parquet"
        src.write_bytes(b"PAR1" + b"abc")
        dst = local_data / "pkg" / "alias.parquet"

        monkeypatch.setattr("cli.lib.pull_sync.os.symlink", lambda *a, **kw: (_ for _ in ()).throw(OSError("a")))
        monkeypatch.setattr("cli.lib.pull_sync.os.link", lambda *a, **kw: (_ for _ in ()).throw(OSError("b")))
        strategy = _link_or_copy(src, dst)
        assert strategy == "copy"
        assert dst.exists()
        assert dst.read_bytes() == src.read_bytes()
        # Different inode — independent file.
        assert dst.stat().st_ino != src.stat().st_ino


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


class TestInvariants:
    def test_orphan_shared_parquet_reported(self, local_dir):
        local_data = local_dir / "data"
        (local_data / "_shared").mkdir(parents=True)
        orphan = local_data / "_shared" / "junk.parquet"
        orphan.write_bytes(b"PAR1")
        violations = audit_invariants(local_data, {"data_packages": {}})
        assert any("orphan" in v for v in violations)

    def test_broken_reference_reported(self, local_dir):
        local_data = local_dir / "data"
        (local_data / "_shared").mkdir(parents=True)
        state = {
            "direct_tables": {
                "orders": {
                    "table_id": "t1",
                    "ref_path": str(local_data / "_direct" / "orders.parquet"),
                    "shared_path": str(local_data / "_shared" / "t1.parquet"),
                    "strategy": "symlink",
                }
            }
        }
        violations = audit_invariants(local_data, state)
        assert any("broken reference" in v for v in violations)
        assert any("dangling shared" in v for v in violations)

    def test_clean_state_no_violations(self, server, local_dir):
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        state, _ = sync_direct_tables(
            server_tables=[_table("t1", "orders")],
            local_data_dir=local_data,
            prev_state={},
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        violations = audit_invariants(local_data, {"direct_tables": state})
        assert violations == []


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------


class TestRunStackSync:
    def test_full_first_pull(self, server, local_dir):
        manifest = {
            "direct_tables": [_table("t_direct", "ops")],
            "data_packages": [
                {
                    "slug": "sales",
                    "tables": [
                        _table("t1", "orders"),
                        _table("t_cust", "customers"),
                    ],
                },
                {
                    "slug": "marketing",
                    "tables": [
                        _table("t_cust", "customers"),
                        _table("t_camp", "campaigns"),
                    ],
                },
            ],
            "memory_domains": [
                {"slug": "playbook", "md5": "h1"},
            ],
        }
        opts = PullStackOptions(
            manifest=manifest,
            local_dir=local_dir,
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
            bundle_fetcher=server.make_bundle_fetcher(),
        )
        report = run_stack_sync(opts)
        assert report.direct_tables.added == 1
        assert report.data_packages.added == 4  # 2 + 2 references
        assert report.memory_domains.added == 1
        # Unique parquets in _shared: t_direct, t1, t_cust, t_camp.
        shared_files = list((local_dir / "data" / "_shared").iterdir())
        assert len(shared_files) == 4
        assert report.invariant_violations == []
        # sync_state.json persisted.
        assert (local_dir / "sync_state.json").exists()

    def test_idempotent_full_repull(self, server, local_dir):
        manifest = {
            "direct_tables": [_table("t1", "orders")],
            "data_packages": [],
            "memory_domains": [{"slug": "x", "md5": "h"}],
        }
        opts = PullStackOptions(
            manifest=manifest,
            local_dir=local_dir,
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
            bundle_fetcher=server.make_bundle_fetcher(),
        )
        run_stack_sync(opts)
        fetch_count_first = len(server.fetch_calls)
        bundle_count_first = len(server.bundle_calls)
        report2 = run_stack_sync(opts)
        assert len(server.fetch_calls) == fetch_count_first
        assert len(server.bundle_calls) == bundle_count_first
        assert report2.total_changes() == 0

    def test_remove_package_with_shared_overlap(self, server, local_dir):
        shared = _table("t_cust", "customers")
        manifest_v1 = {
            "direct_tables": [],
            "data_packages": [
                {"slug": "sales", "tables": [shared, _table("t1", "orders")]},
                {"slug": "marketing", "tables": [shared]},
            ],
            "memory_domains": [],
        }
        opts1 = PullStackOptions(
            manifest=manifest_v1,
            local_dir=local_dir,
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
            bundle_fetcher=server.make_bundle_fetcher(),
        )
        run_stack_sync(opts1)
        # Phase 2: remove sales.
        manifest_v2 = {
            "direct_tables": [],
            "data_packages": [
                {"slug": "marketing", "tables": [shared]},
            ],
            "memory_domains": [],
        }
        opts2 = PullStackOptions(
            manifest=manifest_v2,
            local_dir=local_dir,
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
            bundle_fetcher=server.make_bundle_fetcher(),
        )
        report = run_stack_sync(opts2)
        # Marketing still references customers → shared parquet kept.
        assert (local_dir / "data" / "_shared" / "t_cust.parquet").exists()
        assert not (local_dir / "data" / "_shared" / "t1.parquet").exists()
        assert report.data_packages.removed == 2

    def test_first_pull_with_overlap_writes_18_unique_shared(self, server, local_dir):
        """Spec example: 2 packages, 18 unique tables, package_a has 12,
        package_b has 9 (3 overlap). Verifies ref-count dedup at scale."""
        # 12 tables for pkg_a
        a_only = [_table(f"a{i}", f"a_tbl_{i}") for i in range(9)]
        # 3 overlap tables (shared between a and b)
        overlap = [_table(f"x{i}", f"x_tbl_{i}") for i in range(3)]
        # 6 tables for pkg_b
        b_only = [_table(f"b{i}", f"b_tbl_{i}") for i in range(6)]
        manifest = {
            "direct_tables": [],
            "data_packages": [
                {"slug": "pkg-a", "tables": a_only + overlap},
                {"slug": "pkg-b", "tables": b_only + overlap},
            ],
            "memory_domains": [],
        }
        opts = PullStackOptions(
            manifest=manifest,
            local_dir=local_dir,
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
            bundle_fetcher=server.make_bundle_fetcher(),
        )
        run_stack_sync(opts)
        # 9 + 3 + 6 = 18 unique parquets in _shared.
        shared_files = list((local_dir / "data" / "_shared").iterdir())
        assert len(shared_files) == 18
        # pkg-a has 12 references.
        a_files = list((local_dir / "data" / "pkg-a").iterdir())
        assert len(a_files) == 12
        # pkg-b has 9 references.
        b_files = list((local_dir / "data" / "pkg-b").iterdir())
        assert len(b_files) == 9

    def test_skip_materialize_option_reaches_both_typed_sections(self, server, local_dir):
        """#1304: `PullStackOptions.skip_materialize=True` must omit
        materialized rows from BOTH `direct_tables` and `data_packages`."""
        manifest = {
            "direct_tables": [
                _table("t_direct", "ops"),
                _table("t_mat_direct", "big_direct", query_mode="materialized"),
            ],
            "data_packages": [
                {
                    "slug": "sales",
                    "tables": [
                        _table("t1", "orders"),
                        _table("t_mat_pkg", "big_pkg", query_mode="materialized"),
                    ],
                },
            ],
            "memory_domains": [],
        }
        opts = PullStackOptions(
            manifest=manifest,
            local_dir=local_dir,
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
            bundle_fetcher=server.make_bundle_fetcher(),
            skip_materialize=True,
        )
        report = run_stack_sync(opts)
        assert report.direct_tables.added == 1  # ops only, not big_direct
        assert report.data_packages.added == 1  # orders only, not big_pkg
        assert not (local_dir / "data" / "_direct" / "big_direct.parquet").exists()
        assert not (local_dir / "data" / "sales" / "big_pkg.parquet").exists()
        assert (local_dir / "data" / "_direct" / "ops.parquet").exists()
        assert (local_dir / "data" / "sales" / "orders.parquet").exists()

    def test_skip_materialize_defaults_to_false(self, server, local_dir):
        """Counterpart: omitting `skip_materialize` on `PullStackOptions`
        keeps the pre-existing behavior — a materialized row still syncs."""
        manifest = {
            "direct_tables": [_table("t_mat", "big_direct", query_mode="materialized")],
            "data_packages": [],
            "memory_domains": [],
        }
        opts = PullStackOptions(
            manifest=manifest,
            local_dir=local_dir,
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
            bundle_fetcher=server.make_bundle_fetcher(),
        )
        report = run_stack_sync(opts)
        assert report.direct_tables.added == 1
        assert (local_dir / "data" / "_direct" / "big_direct.parquet").exists()


# ---------------------------------------------------------------------------
# Path-segment sanitization (regression: "unsafe path segment: 'Agnes audit log'")
# ---------------------------------------------------------------------------


class TestSafeSegment:
    def test_already_safe_names_pass_through_verbatim(self):
        for name in ("agnes_audit", "foo__bar", "orders-2026", "a.b.c", "T1"):
            assert _safe_segment(name) == name

    def test_display_name_with_spaces_is_sanitized(self):
        # The exact case that crashed every `agnes pull`.
        assert _safe_segment("Agnes audit log") == "Agnes_audit_log"

    def test_mixed_punctuation_is_coerced_and_trimmed(self):
        assert _safe_segment("  spaced / weird!!name  ") == "spaced_weird_name"

    def test_traversal_segments_are_rejected(self):
        for bad in (".", ".."):
            with pytest.raises(ValueError):
                _safe_segment(bad)

    def test_traversal_embedded_in_path_is_neutralized(self):
        # A slash-bearing label can't traverse — the separator is coerced and
        # leading dots stripped.
        assert _safe_segment("../etc") == "etc"

    def test_empty_or_unusable_names_raise(self):
        for bad in ("", "   ", "///", "!!!"):
            with pytest.raises(ValueError):
                _safe_segment(bad)


class TestSyncSanitizesTableNames:
    def test_package_table_with_spaced_name_syncs(self, server, local_dir):
        """Regression: a table whose display name has spaces used to raise
        `unsafe path segment` and abort the whole package sync."""
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        pkg = {"slug": "internal", "tables": [_table("agnes_audit", "Agnes audit log")]}
        state, report = sync_data_packages(
            server_packages=[pkg],
            local_data_dir=local_data,
            prev_state={},
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        assert report.added == 1
        assert (local_data / "internal" / "Agnes_audit_log.parquet").exists()
        # Idempotent re-pull: no refetch, no error, state stable.
        state2, report2 = sync_data_packages(
            server_packages=[pkg],
            local_data_dir=local_data,
            prev_state=state,
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        assert report2.added == 0
        assert report2.updated == 0
        assert set(state2["internal"]) == {"Agnes_audit_log"}

    def test_internal_member_never_reaches_the_manifest(self, server, local_dir, tmp_path, monkeypatch):
        """The server-side counterpart of the test above.

        Since the ``agnes-usage`` package exists, ``agnes_audit`` IS a package
        member — and ``_build_data_packages_section`` iterates member rows
        directly, filtered by neither ``get_accessible_tables`` nor
        ``sync_state``. Without an explicit filter the internal tables would
        ride into ``manifest.data_packages[].tables[]`` with an empty hash and
        ``agnes pull`` would try to materialize every user's audit log, as the
        test above shows it happily does when handed such a row.

        So: the package is visible to a grantee, and its internal members are
        not in the manifest — which leaves the CLI with nothing to fetch.
        """
        data_dir = tmp_path / "agnes_data"
        (data_dir / "state").mkdir(parents=True)
        monkeypatch.setenv("DATA_DIR", str(data_dir))
        monkeypatch.delenv("STATE_DIR", raising=False)

        from src.db import _ensure_schema, close_system_db, get_system_db

        close_system_db()
        conn = get_system_db()
        _ensure_schema(conn)
        try:
            from connectors.internal.registry import (
                USAGE_PACKAGE_SLUG,
                ensure_internal_package_seeded,
                ensure_internal_tables_registered,
            )
            from src.repositories import data_packages_repo
            from src.repositories.user_group_members import UserGroupMembersRepository
            from src.repositories.user_groups import UserGroupsRepository
            from src.repositories.users import UserRepository
            from app.api.sync import _build_manifest_for_user

            ensure_internal_package_seeded(newly_registered=ensure_internal_tables_registered())
            pkg = data_packages_repo().get_by_slug(USAGE_PACKAGE_SLUG)
            assert pkg is not None
            assert {t["id"] for t in data_packages_repo().list_tables(pkg["id"])}, "package has internal members"

            UserRepository(conn).create(id="analyst1", email="analyst@example.com", name="Analyst")
            group = UserGroupsRepository(conn).create(name="UsageGroup", description="", created_by="test")
            gid = group["id"] if isinstance(group, dict) else group
            UserGroupMembersRepository(conn).add_member("analyst1", gid, source="test")
            conn.execute(
                "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
                "requirement, assigned_at, assigned_by) "
                "VALUES (?, ?, 'data_package', ?, 'required', CURRENT_TIMESTAMP, 'test')",
                ["grant-usage-pkg", gid, pkg["id"]],
            )

            manifest = _build_manifest_for_user(conn, {"id": "analyst1", "email": "analyst@example.com"})
            sections = [p for p in manifest["data_packages"] if p["slug"] == USAGE_PACKAGE_SLUG]
            assert sections, "the granted package itself is still surfaced"
            assert sections[0]["tables"] == [], "no internal member may enter the manifest"
            assert "agnes_audit" not in manifest["tables"]
        finally:
            close_system_db()

        # …and therefore `agnes pull` has nothing to materialize for it.
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        state, report = sync_data_packages(
            server_packages=sections,
            local_data_dir=local_data,
            prev_state={},
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        assert report.added == 0
        assert server.fetch_calls == []
        assert not (local_data / USAGE_PACKAGE_SLUG).exists()

    def test_one_unnameable_row_does_not_abort_the_rest(self, server, local_dir):
        """A row whose name can't yield any safe segment is skipped, not fatal —
        the remaining tables in the package still sync."""
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        pkg = {"slug": "mixed", "tables": [_table("t_ok", "orders"), _table("t_bad", "///")]}
        state, report = sync_data_packages(
            server_packages=[pkg],
            local_data_dir=local_data,
            prev_state={},
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        assert report.added == 1
        assert (local_data / "mixed" / "orders.parquet").exists()
        assert set(state["mixed"]) == {"orders"}

    def test_colliding_sanitized_names_resolve_deterministically(self, server, local_dir):
        """Two distinct display names that fold to the same segment must not
        silently overwrite each other, and the survivor must be independent of
        manifest row order (lexicographically smaller raw label wins)."""
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        # Space (0x20) sorts before '/' (0x2f), so "Sales 2024" (t_a) wins.
        a = _table("t_a", "Sales 2024")
        b = _table("t_b", "Sales/2024")
        for order in ([a, b], [b, a]):
            shutil.rmtree(local_data / "sales", ignore_errors=True)
            state, report = sync_data_packages(
                server_packages=[{"slug": "sales", "tables": order}],
                local_data_dir=local_data,
                prev_state={},
                fetcher=server.make_fetcher(),
                md5_of=server.make_md5(),
            )
            # One table synced; no crash, no double-write.
            assert report.added == 1
            assert set(state["sales"]) == {"Sales_2024"}
            assert (local_data / "sales" / "Sales_2024.parquet").exists()
            # Survivor is deterministic regardless of input order.
            assert state["sales"]["Sales_2024"]["table_id"] == "t_a"


class TestStackSyncSkipsPartitioned:
    """Devin #3: the stack-sync path (direct_tables / data_packages) downloads
    one `{name}.parquet` per table and is NOT part-aware, so partitioned
    tables must be skipped there (they 404 otherwise) — they are distributed
    via the main flat-`tables` per-part path into server/parquet/ instead."""

    def test_skip_remote(self):
        from cli.lib.pull_sync import _server_table_skip

        assert _server_table_skip({"query_mode": "remote"}) == "remote"

    def test_skip_partitioned(self):
        from cli.lib.pull_sync import _server_table_skip

        assert (
            _server_table_skip(
                {
                    "query_mode": "local",
                    "parts": [{"path": "month=2026-06/data.parquet", "hash": "aa", "size_bytes": 1}],
                }
            )
            == "parts"
        )

    def test_single_file_not_skipped(self):
        from cli.lib.pull_sync import _server_table_skip

        assert _server_table_skip({"query_mode": "local"}) is None
        assert _server_table_skip({"query_mode": "local", "parts": None}) is None


class TestStackSyncSkipsServerOnly:
    """#1324: step 8 (typed `direct_tables` / `data_packages`) must skip
    `server_only` rows the same way step 4's flat-`tables` loop already
    does (`cli/lib/pull.py`'s `if info.get("server_only"): continue`), so
    a server_only table is never downloaded into `.claude/data/_shared`."""

    def test_skip_server_only(self):
        from cli.lib.pull_sync import _server_table_skip

        assert _server_table_skip({"query_mode": "local", "server_only": True}) == "server_only"

    def test_server_only_false_not_skipped(self):
        from cli.lib.pull_sync import _server_table_skip

        assert _server_table_skip({"query_mode": "local", "server_only": False}) is None

    def test_server_only_absent_not_skipped(self):
        from cli.lib.pull_sync import _server_table_skip

        assert _server_table_skip({"query_mode": "local"}) is None


class TestStackSyncSkipsMaterializedWhenOptedIn:
    """#1304: `agnes pull --skip-materialize` must reach the typed manifest
    sections too, not just step 4's flat `tables` dict."""

    def test_materialized_skipped_when_flag_set(self):
        from cli.lib.pull_sync import _server_table_skip

        assert _server_table_skip({"query_mode": "materialized"}, skip_materialize=True) == "materialized"

    def test_materialized_not_skipped_by_default(self):
        from cli.lib.pull_sync import _server_table_skip

        assert _server_table_skip({"query_mode": "materialized"}) is None

    def test_materialized_not_skipped_when_flag_explicitly_false(self):
        from cli.lib.pull_sync import _server_table_skip

        assert _server_table_skip({"query_mode": "materialized"}, skip_materialize=False) is None

    def test_flag_does_not_affect_other_query_modes(self):
        """skip_materialize=True must not accidentally widen to local/remote."""
        from cli.lib.pull_sync import _server_table_skip

        assert _server_table_skip({"query_mode": "local"}, skip_materialize=True) is None
        assert _server_table_skip({"query_mode": "remote"}, skip_materialize=True) == "remote"  # already skipped anyway


class TestStackSyncPrunesNewlyWithheldTables:
    """A skipped-but-still-listed table must not leak on disk.

    The to_delete loops only prune names absent from the server list, so a
    previously synced table the server *still lists* but newly withholds
    (flipped to server_only — the granted-but-not-materialized package
    transition `app/api/sync.py` produces — or to remote) must be pruned at
    the skip site. Silently dropping its state row instead would leave the
    reference and the `_shared` parquet on disk forever with no handle left
    for any later pull to remove them (Devin review on #1331)."""

    def test_direct_table_flipped_to_server_only_is_pruned(self, server, local_dir):
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        state1, _ = sync_direct_tables(
            server_tables=[_table("t1", "orders")],
            local_data_dir=local_data,
            prev_state={},
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        assert (local_data / "_shared" / "t1.parquet").exists()
        state2, report = sync_direct_tables(
            server_tables=[_table("t1", "orders", server_only=True)],
            local_data_dir=local_data,
            prev_state=state1,
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        assert report.removed == 1
        assert "orders" not in state2
        assert not (local_data / "_direct" / "orders.parquet").exists()
        assert not (local_data / "_shared" / "t1.parquet").exists()

    def test_direct_table_flipped_to_remote_is_pruned(self, server, local_dir):
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        state1, _ = sync_direct_tables(
            server_tables=[_table("t1", "orders")],
            local_data_dir=local_data,
            prev_state={},
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        state2, report = sync_direct_tables(
            server_tables=[_table("t1", "orders", query_mode="remote")],
            local_data_dir=local_data,
            prev_state=state1,
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        assert report.removed == 1
        assert "orders" not in state2
        assert not (local_data / "_direct" / "orders.parquet").exists()
        assert not (local_data / "_shared" / "t1.parquet").exists()

    def test_never_synced_server_only_table_is_a_plain_skip(self, server, local_dir):
        """No prior state row → nothing to prune, nothing to report."""
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        state, report = sync_direct_tables(
            server_tables=[_table("t1", "orders", server_only=True)],
            local_data_dir=local_data,
            prev_state={},
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        assert state == {}
        assert report.added == report.updated == report.removed == 0
        assert server.fetch_calls == []

    def test_package_table_flipped_to_server_only_is_pruned(self, server, local_dir):
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        pkg = {"slug": "sales-bundle", "tables": [_table("t1", "orders")]}
        state1, _ = sync_data_packages(
            server_packages=[pkg],
            local_data_dir=local_data,
            prev_state={},
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        assert (local_data / "sales-bundle" / "orders.parquet").exists()
        flipped = {"slug": "sales-bundle", "tables": [_table("t1", "orders", server_only=True)]}
        state2, report = sync_data_packages(
            server_packages=[flipped],
            local_data_dir=local_data,
            prev_state=state1,
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        assert report.removed == 1
        assert state2["sales-bundle"] == {}
        assert not (local_data / "sales-bundle" / "orders.parquet").exists()
        assert not (local_data / "_shared" / "t1.parquet").exists()


class TestSkipMaterializePreservesTrackedState:
    """`--skip-materialize` is a fetch opt-out, not an unsubscribe: a
    previously synced materialized table keeps both its files AND its state
    row under the flag, so a later pull without the flag can still update
    or prune the copy instead of orphaning it."""

    def test_state_row_carried_forward_and_files_kept(self, server, local_dir):
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        t = _table("t1", "orders", query_mode="materialized")
        state1, _ = sync_direct_tables(
            server_tables=[t],
            local_data_dir=local_data,
            prev_state={},
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        state2, report = sync_direct_tables(
            server_tables=[t],
            local_data_dir=local_data,
            prev_state=state1,
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
            skip_materialize=True,
        )
        assert report.added == report.updated == report.removed == 0
        assert state2["orders"] == state1["orders"]
        assert (local_data / "_direct" / "orders.parquet").exists()
        assert (local_data / "_shared" / "t1.parquet").exists()

    def test_later_pull_without_flag_can_still_prune(self, server, local_dir):
        local_data = local_dir / "data"
        local_data.mkdir(parents=True, exist_ok=True)
        t = _table("t1", "orders", query_mode="materialized")
        state1, _ = sync_direct_tables(
            server_tables=[t],
            local_data_dir=local_data,
            prev_state={},
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        state2, _ = sync_direct_tables(
            server_tables=[t],
            local_data_dir=local_data,
            prev_state=state1,
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
            skip_materialize=True,
        )
        # Server drops the table; the carried-forward row is the handle the
        # to_delete loop needs.
        state3, report = sync_direct_tables(
            server_tables=[],
            local_data_dir=local_data,
            prev_state=state2,
            fetcher=server.make_fetcher(),
            md5_of=server.make_md5(),
        )
        assert report.removed == 1
        assert not (local_data / "_direct" / "orders.parquet").exists()
        assert not (local_data / "_shared" / "t1.parquet").exists()
