"""A partition-synced table stores its data as a DIRECTORY of per-period
parquets (``data/<table_id>/<partition>.parquet``) instead of a single
``data/<table_id>.parquet``.

PR #1189 taught the PREVIEW surface that layout (``resolve_local_parquet_glob``);
the other read surfaces still used the single-file lookup and therefore
reported a healthy, fully-synced partitioned table as having no data — a 404
from ``/api/v2/schema``, a 404 from ``/api/v2/scan``, and a missing size hint
in ``/api/v2/catalog`` (Devin Review on #1189).

The pending-first-sync case must keep behaving as before: a partition
directory that holds no parquet yet is genuinely "no data".
"""

from __future__ import annotations

import datetime

import pyarrow as pa
import pyarrow.parquet as pq
import pytest


def _write_partitions(data_dir, table_id: str) -> dict[str, int]:
    """Write two per-period parquets under ``data/<table_id>/``. Returns
    ``{filename: size_bytes}``."""
    part_dir = data_dir / table_id
    part_dir.mkdir(parents=True, exist_ok=True)
    nov = pa.table(
        {
            "v": [1, 2],
            "d": [datetime.date(2025, 11, 1), datetime.date(2025, 11, 15)],
        }
    )
    dec = pa.table({"v": [3], "d": [datetime.date(2025, 12, 1)]})
    pq.write_table(nov, part_dir / "2025_11.parquet")
    pq.write_table(dec, part_dir / "2025_12.parquet")
    return {p.name: p.stat().st_size for p in part_dir.glob("*.parquet")}


def _write_hive_partitions(data_dir, table_id: str) -> None:
    """Write the NESTED hive layout (``month=YYYY-MM/data.parquet``) the Jira
    connector produces.

    The December part deliberately carries a column November does not: hive part
    schemas drift month to month, so a recursive glob only reads without
    ``union_by_name`` by accident. This is what makes that flag load-bearing
    rather than decorative.
    """
    part_dir = data_dir / table_id
    nov = pa.table({"v": [1, 2], "d": [datetime.date(2025, 11, 1), datetime.date(2025, 11, 15)]})
    dec = pa.table({"v": [3], "d": [datetime.date(2025, 12, 1)], "resolution": ["Done"]})
    for month, tbl in (("2025-11", nov), ("2025-12", dec)):
        month_dir = part_dir / f"month={month}"
        month_dir.mkdir(parents=True, exist_ok=True)
        pq.write_table(tbl, month_dir / "data.parquet")


class TestPartitionedSizeBytes:
    """``app.utils.local_parquet_size_bytes`` — one number for either layout."""

    def test_sums_every_partition_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        sizes = _write_partitions(tmp_path / "extracts" / "keboola" / "data", "kbc_sales")

        from app.utils import local_parquet_size_bytes

        assert local_parquet_size_bytes("kbc_sales", "keboola") == sum(sizes.values())
        assert len(sizes) == 2, "precondition: the sum must span more than one file"

    def test_single_file_layout_reports_that_files_size(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        data = tmp_path / "extracts" / "keboola" / "data"
        data.mkdir(parents=True)
        pq.write_table(pa.table({"a": [1]}), data / "kbc_orders.parquet")

        from app.utils import local_parquet_size_bytes

        expected = (data / "kbc_orders.parquet").stat().st_size
        assert local_parquet_size_bytes("kbc_orders", "keboola") == expected

    def test_nested_hive_parts_are_counted_too(self, tmp_path, monkeypatch):
        """The Jira layout nests one level (``month=YYYY-MM/data.parquet``);
        its bytes are on disk exactly like a flat partition's."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        hive = tmp_path / "extracts" / "jira" / "data" / "issues" / "month=2025-11"
        hive.mkdir(parents=True)
        pq.write_table(pa.table({"a": [1]}), hive / "data.parquet")

        from app.utils import local_parquet_size_bytes

        assert local_parquet_size_bytes("issues", "jira") == (hive / "data.parquet").stat().st_size

    def test_empty_partition_directory_is_no_data_yet(self, tmp_path, monkeypatch):
        """A directory with no parquet in it means the sync has not produced a
        partition yet — that IS the pending-sync case, so there is no size to
        report (0 would read as "synced, empty")."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        (tmp_path / "extracts" / "keboola" / "data" / "kbc_empty").mkdir(parents=True)

        from app.utils import local_parquet_size_bytes

        assert local_parquet_size_bytes("kbc_empty", "keboola") is None

    def test_no_layout_at_all_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        (tmp_path / "extracts" / "keboola" / "data").mkdir(parents=True)

        from app.utils import local_parquet_size_bytes

        assert local_parquet_size_bytes("kbc_missing", "keboola") is None


class TestPathContainment:
    """A table id reaches these resolvers straight off a request path, and the
    DIRECTORY layout made them a far wider read primitive than the single-file
    lookup was: the old one at least pinned the last segment to
    `<id>.parquet`, while a directory is recursively globbed for every parquet
    under it. So the id must be a plain path segment resolving inside
    `extracts/` — `..` alone reaches the extract source root, and the routing
    layer refusing `%2F` today is transport luck, not a containment guarantee
    (`/agnes-review`, RBAC reviewer on #1198)."""

    @pytest.fixture
    def data_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        data = tmp_path / "extracts" / "keboola" / "data"
        data.mkdir(parents=True)
        pq.write_table(pa.table({"a": [1]}), data / "ok.parquet")
        (tmp_path / "outside").mkdir()
        pq.write_table(pa.table({"secret": [1]}), tmp_path / "outside" / "leak.parquet")
        return tmp_path

    @pytest.mark.parametrize("evil", ["..", ".", "../..", "../../../outside", "a/b", "a\\b", ""])
    def test_traversal_ids_resolve_to_nothing(self, data_dir, evil):
        from app.utils import (
            local_parquet_size_bytes,
            resolve_local_parquet,
            resolve_local_parquet_glob,
            resolve_local_partition_dir,
        )

        assert resolve_local_partition_dir(evil) is None
        assert resolve_local_parquet_glob(evil) is None
        assert local_parquet_size_bytes(evil) is None
        assert resolve_local_parquet(evil) is None

    def test_a_symlink_out_of_the_extracts_tree_is_not_followed(self, data_dir):
        """Containment is checked on the REAL path, so a link planted inside
        `extracts/` cannot hand a caller a directory outside it."""
        (data_dir / "extracts" / "keboola" / "data" / "linked").symlink_to(data_dir / "outside")

        from app.utils import local_parquet_size_bytes, resolve_local_partition_dir

        assert resolve_local_partition_dir("linked") is None
        assert local_parquet_size_bytes("linked") is None

    def test_the_source_type_fast_path_is_contained_too(self, data_dir):
        """The FILE half of the symlink case, on the one branch that skipped it.

        `resolve_local_parquet` returns its `source_type` fast path directly,
        while the `rglob` fallback beside it and every
        `_partition_dir_candidates` result are filtered — so a link planted at
        `extracts/<source_type>/data/<id>.parquet` was the remaining way out of
        the tree, and it feeds `resolve_local_parquet_glob` and the read
        surfaces (Devin Review on #1198).
        """
        link = data_dir / "extracts" / "keboola" / "data" / "leaky.parquet"
        link.symlink_to(data_dir / "outside" / "leak.parquet")

        from app.utils import resolve_local_parquet, resolve_local_parquet_glob

        # `source_type` supplied is precisely what takes the fast path.
        assert resolve_local_parquet("leaky", "keboola") is None
        assert resolve_local_parquet_glob("leaky", "keboola") is None
        # …and the source-agnostic fallback must not readmit it either.
        assert resolve_local_parquet("leaky") is None

    def test_a_symlinked_source_directory_is_still_readable(self, tmp_path, monkeypatch):
        """Containment must not cost an operator a legitimate layout.

        Symlinking a whole extract source onto a larger volume
        (`extracts/keboola` -> /mnt/big/keboola) is deployment, not an escape —
        but resolving only against `extracts/` rejected it, and every table
        under that source read as unsynced on every surface at once (Devin
        Review on #1198). The link planted INSIDE such a source is still refused
        by the test above, since it resolves outside its own source root too.
        """
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        elsewhere = tmp_path / "volume" / "keboola" / "data"
        elsewhere.mkdir(parents=True)
        pq.write_table(pa.table({"a": [1]}), elsewhere / "ok.parquet")
        (tmp_path / "extracts").mkdir()
        (tmp_path / "extracts" / "keboola").symlink_to(tmp_path / "volume" / "keboola")

        from app.utils import local_parquet_size_bytes, resolve_local_parquet

        assert resolve_local_parquet("ok", "keboola") is not None
        assert local_parquet_size_bytes("ok", "keboola") is not None

    def test_a_symlinked_data_directory_is_still_readable(self, tmp_path, monkeypatch):
        """One level deeper than the source-dir case, and allowed for the same
        reason: `data` is a literal path component, not the untrusted segment,
        so an operator may point it at another volume. The untrusted `<id>` is
        always the LAST component, and a link named for it is still refused by
        the tests above (Devin Review on #1198)."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        elsewhere = tmp_path / "volume" / "kbc-data"
        elsewhere.mkdir(parents=True)
        pq.write_table(pa.table({"a": [1]}), elsewhere / "ok.parquet")
        (tmp_path / "extracts" / "keboola").mkdir(parents=True)
        (tmp_path / "extracts" / "keboola" / "data").symlink_to(elsewhere)

        from app.utils import local_parquet_size_bytes, resolve_local_parquet

        assert resolve_local_parquet("ok", "keboola") is not None
        assert local_parquet_size_bytes("ok", "keboola") is not None

    @pytest.mark.parametrize("pattern", ["*", "?k", "[o]k", "*.parquet", "o*"])
    def test_glob_metacharacter_ids_match_nothing(self, data_dir, pattern):
        """The id is interpolated into a GLOB, not only joined into a path.

        `extracts.rglob(f"data/{table_id}.parquet")` and
        `extracts.glob(f"*/data/{table_id}")` mean a `*` or `[...]` stops naming
        one table and starts matching an arbitrary one — `ok.parquet` here — so
        the resolvers would hand back someone else's data under the requested
        name (Devin Review on #1198).
        """
        from app.utils import (
            local_parquet_size_bytes,
            resolve_local_parquet,
            resolve_local_parquet_glob,
            resolve_local_partition_dir,
        )

        assert resolve_local_parquet(pattern) is None
        assert resolve_local_parquet(pattern, "keboola") is None
        assert resolve_local_parquet_glob(pattern) is None
        assert resolve_local_partition_dir(pattern) is None
        assert local_parquet_size_bytes(pattern) is None

    @pytest.mark.parametrize("evil", ["..", "*", "o*", "[o]k"])
    def test_an_evil_id_does_not_profile_anything(self, data_dir, monkeypatch, evil):
        """End to end on the one surface that resolves a directory without a
        registry-existence check first.

        The wildcard cases are the sharper half and were live until this branch:
        `refresh_profile` built its own `rglob(f"data/{name}.parquet")` instead
        of going through the validated resolver, so `*` matched `ok.parquet` and
        profiled it — storing one table's statistics under the requested name,
        with no error anywhere (Devin Review on #1198). A 404 is the only
        acceptable answer for a name that does not denote one table.
        """
        import importlib

        import src.db as db_module
        from fastapi import HTTPException

        importlib.reload(db_module)
        from app.api import catalog

        monkeypatch.setattr(catalog, "can_access_table", lambda *a, **kw: True)
        conn = db_module.get_system_db()
        try:
            with pytest.raises(HTTPException) as exc_info:
                catalog.refresh_profile(evil, user={"id": "admin1"}, conn=conn)
        finally:
            conn.close()
        assert exc_info.value.status_code == 404


class TestCatalogSizeHint:
    """``/api/v2/catalog``'s ``rough_size_hint`` for a partitioned row."""

    def test_hint_is_bucketed_from_the_summed_partition_bytes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        sizes = _write_partitions(tmp_path / "extracts" / "keboola" / "data", "kbc_sales")

        from app.api import v2_catalog

        seen: list[int] = []
        monkeypatch.setattr(v2_catalog, "_bucket_size", lambda n: seen.append(n) or "small")

        hint = v2_catalog._materialized_parquet_size_bucket("kbc_sales", "keboola", "local")

        assert hint == "small"
        assert seen == [sum(sizes.values())], "the whole table's bytes, not one partition's"

    def test_empty_partition_directory_reports_no_hint(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        (tmp_path / "extracts" / "keboola" / "data" / "kbc_empty").mkdir(parents=True)

        from app.api import v2_catalog

        assert v2_catalog._materialized_parquet_size_bucket("kbc_empty", "keboola", "local") is None


    def test_hint_follows_the_registry_name_like_the_read_surfaces(self, tmp_path, monkeypatch):
        """A row whose parquet is filed under its `name`, not its slugified `id`.

        `resolve_local_parquet_glob` takes `registry_name` so /schema, /scan
        and /sample resolve these rows. Its size counterpart must follow the
        same name-first ordering, or the catalog publishes `rough_size_hint:
        null` for a table those three surfaces serve happily — the mirror of
        the disagreement `resolve_local_parquet_glob`'s own docstring records
        (a hint for a table /schema then 404-ed on).
        """
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        data = tmp_path / "extracts" / "keboola" / "data"
        data.mkdir(parents=True)
        # The exact pair the register handler's slugify produces.
        table_id, registry_name = "orders_90d", "Orders 90d"
        (data / f"{registry_name}.parquet").write_bytes(b"x" * 4096)

        from app.api import v2_catalog

        seen: list[int] = []
        monkeypatch.setattr(v2_catalog, "_bucket_size", lambda n: seen.append(n) or "small")

        hint = v2_catalog._materialized_parquet_size_bucket(
            table_id, "keboola", "local", registry_name=registry_name
        )

        assert hint == "small", "the name-keyed parquet must be found"
        assert seen == [4096]

    def test_hint_for_row_passes_the_registry_name_through(self, tmp_path, monkeypatch):
        """The catalog row carries `name`; `_hint_for_row` must forward it.

        Threading the keyword only as far as the helper would leave the real
        call path — the one the catalog response actually uses — still keyed
        by id alone.
        """
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        data = tmp_path / "extracts" / "keboola" / "data"
        data.mkdir(parents=True)
        (data / "Orders 90d.parquet").write_bytes(b"x" * 4096)

        from app.api import v2_catalog

        hint = v2_catalog._hint_for_row(
            {
                "id": "orders_90d",
                "name": "Orders 90d",
                "source_type": "keboola",
                "query_mode": "local",
            },
            {},
        )

        assert hint["rough_size_hint"] is not None, (
            "catalog hint must agree with the read surfaces for a name-keyed row"
        )


class TestProfileRefreshSurface:
    """``POST /api/catalog/profile/{table}/refresh``. The profiler itself has
    always understood a directory of parts (``src/profiler.py`` globs ``**``,
    and the scheduled run passes it a directory) — only this manual endpoint's
    lookup was single-file, so an admin could not re-profile a partitioned
    table the nightly run profiles fine."""

    @pytest.fixture
    def db(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import importlib

        import src.db as db_module

        importlib.reload(db_module)
        yield db_module

    def _refresh(self, db, monkeypatch, table_name: str):
        from app.api import catalog

        monkeypatch.setattr(catalog, "can_access_table", lambda *a, **kw: True)
        conn = db.get_system_db()
        try:
            return catalog.refresh_profile(table_name, user={"id": "u1"}, conn=conn)
        finally:
            conn.close()

    def test_profiles_a_partitioned_table(self, db, tmp_path, monkeypatch):
        _write_partitions(tmp_path / "extracts" / "keboola" / "data", "kbc_sales")

        out = self._refresh(db, monkeypatch, "kbc_sales")

        assert out["status"] == "ok"
        assert out["columns"] == 2

    def test_empty_partition_directory_still_404s(self, db, tmp_path, monkeypatch):
        from fastapi import HTTPException

        (tmp_path / "extracts" / "keboola" / "data" / "kbc_empty").mkdir(parents=True)

        with pytest.raises(HTTPException) as exc_info:
            self._refresh(db, monkeypatch, "kbc_empty")
        assert exc_info.value.status_code == 404


class TestSchemaSurface:
    """``/api/v2/schema`` reads columns from the parquet."""

    def _row(self, table_id: str) -> dict:
        return {
            "id": table_id,
            "source_type": "keboola",
            "query_mode": "local",
            "bucket": "in.c-main",
            "source_table": table_id,
        }

    def test_columns_resolve_from_a_partition_directory(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        _write_partitions(tmp_path / "extracts" / "keboola" / "data", "kbc_sales")

        from app.api.v2_schema import build_schema_uncached

        payload = build_schema_uncached(conn=None, table_id="kbc_sales", bq=object(), row=self._row("kbc_sales"))

        assert {c["name"] for c in payload["columns"]} == {"v", "d"}
        assert payload["sql_flavor"] == "duckdb"

    def test_columns_resolve_from_a_nested_hive_directory(self, tmp_path, monkeypatch):
        """The catalog already sums hive parts for its size hint, so schema has
        to resolve the same layout — otherwise the catalog advertises a size for
        a table this surface 404s on (Devin Review on #1198)."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        _write_hive_partitions(tmp_path / "extracts" / "jira" / "data", "issues")

        from app.api.v2_schema import build_schema_uncached

        row = self._row("issues") | {"source_type": "jira"}
        payload = build_schema_uncached(conn=None, table_id="issues", bq=object(), row=row)

        names = {c["name"] for c in payload["columns"]}
        # `resolution` proves union_by_name (only the December part has it);
        # `month` proves hive_partitioning turned the directory key into a column,
        # the same shape the Jira extract's own view exposes.
        assert {"v", "d", "resolution", "month"} <= names

    def test_empty_partition_directory_still_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        (tmp_path / "extracts" / "keboola" / "data" / "kbc_empty").mkdir(parents=True)

        from app.api.v2_schema import NotFound, build_schema_uncached

        with pytest.raises(NotFound):
            build_schema_uncached(conn=None, table_id="kbc_empty", bq=object(), row=self._row("kbc_empty"))


class TestScanSurface:
    """``/api/v2/scan`` executes locally against the parquet."""

    SCHEMA = {"v": "INT64", "d": "DATE"}

    @pytest.fixture
    def reload_db(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import importlib

        import src.db as db_module

        importlib.reload(db_module)
        yield db_module

    def _seed(self, conn, table_id: str):
        from src.db import SYSTEM_ADMIN_GROUP
        from src.repositories.table_registry import TableRegistryRepository
        from src.repositories.user_group_members import UserGroupMembersRepository
        from src.repositories.users import UserRepository

        if UserRepository(conn).get_by_id("admin1") is None:
            UserRepository(conn).create(id="admin1", email="admin1@test.com", name="Admin")
        gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()
        if gid:
            UserGroupMembersRepository(conn).add_member("admin1", gid[0], source="system_seed")
        TableRegistryRepository(conn).register(
            id=table_id,
            name=table_id,
            source_type="keboola",
            bucket="in.c-main",
            source_table=table_id,
            query_mode="local",
        )

    def _run(self, reload_db, monkeypatch, req):
        from app.api import v2_scan

        monkeypatch.setattr(v2_scan, "_resolve_schema", lambda *a, **kw: dict(self.SCHEMA))
        conn = reload_db.get_system_db()
        try:
            self._seed(conn, req["table_id"])
            from connectors.bigquery.access import BqAccess, BqProjects

            return v2_scan.run_scan(
                conn,
                {"id": "admin1", "email": "a@x.com"},
                req,
                bq=BqAccess(BqProjects(billing="b", data="d")),
                quota=v2_scan._build_quota_tracker(),
            )
        finally:
            conn.close()

    def test_scan_reads_every_partition(self, reload_db, tmp_path, monkeypatch):
        _write_partitions(tmp_path / "extracts" / "keboola" / "data", "kbc_sales")

        from app.api.v2_arrow import parse_ipc_bytes

        ipc = self._run(reload_db, monkeypatch, {"table_id": "kbc_sales", "select": ["v"]})

        assert sorted(parse_ipc_bytes(ipc).column("v").to_pylist()) == [1, 2, 3]

    def test_scan_filters_across_partitions(self, reload_db, tmp_path, monkeypatch):
        _write_partitions(tmp_path / "extracts" / "keboola" / "data", "kbc_sales")

        from app.api.v2_arrow import parse_ipc_bytes

        ipc = self._run(
            reload_db,
            monkeypatch,
            {"table_id": "kbc_sales", "select": ["v"], "where": "v > 1", "order_by": ["v"]},
        )

        assert parse_ipc_bytes(ipc).column("v").to_pylist() == [2, 3]

    def test_scan_reads_every_hive_partition(self, reload_db, tmp_path, monkeypatch):
        """Same divergence as schema: the nested layout has to be scannable, or
        the catalog's size hint points at a table scan refuses to read."""
        _write_hive_partitions(tmp_path / "extracts" / "keboola" / "data", "kbc_sales")

        from app.api.v2_arrow import parse_ipc_bytes

        ipc = self._run(reload_db, monkeypatch, {"table_id": "kbc_sales", "select": ["v"]})

        assert sorted(parse_ipc_bytes(ipc).column("v").to_pylist()) == [1, 2, 3]

    def test_empty_partition_directory_still_raises_not_found(self, reload_db, tmp_path, monkeypatch):
        (tmp_path / "extracts" / "keboola" / "data" / "kbc_empty").mkdir(parents=True)

        with pytest.raises(FileNotFoundError):
            self._run(reload_db, monkeypatch, {"table_id": "kbc_empty", "select": ["v"]})


class TestPreviewSurface:
    """``/api/v2/sample`` — the surface #1189 taught the partition layout first.

    It is also the one that broke when the resolver learned hive: widening what
    ``resolve_local_parquet_glob`` can return changed the CONTRACT of its return
    value, and this caller kept a bare ``read_parquet(?)`` (Devin Review on
    #1198).

    The symptom is SILENT, not a crash — worth stating precisely, because it
    decides what this test asserts. On DuckDB 1.5.2 a bare read of a
    ``**/*.parquet`` target does not raise: hive partitioning is auto-detected,
    so ``month`` comes back either way. What ``union_by_name=false`` does is take
    the schema of the FIRST part and drop everything the later ones added — so
    Preview rendered a Jira table missing whichever columns arrived in a later
    month, with no error anywhere to say so. Asserting on rows alone would pass
    against the broken read; the column set is the assertion that bites.
    """

    @pytest.fixture
    def reload_db(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import importlib

        import src.db as db_module

        importlib.reload(db_module)
        yield db_module

    def _seed(self, conn, table_id: str):
        from src.db import SYSTEM_ADMIN_GROUP
        from src.repositories.table_registry import TableRegistryRepository
        from src.repositories.user_group_members import UserGroupMembersRepository
        from src.repositories.users import UserRepository

        if UserRepository(conn).get_by_id("admin1") is None:
            UserRepository(conn).create(id="admin1", email="admin1@test.com", name="Admin")
        gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()
        if gid:
            UserGroupMembersRepository(conn).add_member("admin1", gid[0], source="system_seed")
        TableRegistryRepository(conn).register(
            id=table_id,
            name=table_id,
            source_type="keboola",
            bucket="in.c-main",
            source_table=table_id,
            query_mode="local",
        )

    def test_preview_reads_a_nested_hive_table(self, reload_db, tmp_path, monkeypatch):
        _write_hive_partitions(tmp_path / "extracts" / "keboola" / "data", "kbc_sales")

        from app.api import v2_sample

        monkeypatch.setattr(v2_sample._sample_cache, "get", lambda *a, **kw: None)
        monkeypatch.setattr(v2_sample._sample_cache, "set", lambda *a, **kw: None)

        conn = reload_db.get_system_db()
        try:
            self._seed(conn, "kbc_sales")
            payload = v2_sample.build_sample(conn, {"id": "admin1", "email": "a@x.com"}, "kbc_sales", n=10, bq=object())
        finally:
            conn.close()

        assert sorted(r["v"] for r in payload["rows"]) == [1, 2, 3]
        # `resolution` exists only in the December part. Without `union_by_name`
        # the preview silently drops it — this is the assertion that fails
        # against a bare `read_parquet(?)`.
        assert any("resolution" in r for r in payload["rows"]), (
            "a column that arrived in a later partition is missing from the preview"
        )


class TestReadExpressionContract:
    """The resolver's return value is only safe read one way, so that way is a
    shared symbol rather than a sentence in a docstring.

    The docstring version already failed once: the hive branch landed with
    ``v2_schema`` and ``v2_scan`` updated and ``v2_sample`` left on a bare
    ``read_parquet(?)``. This asserts the shape rather than the prose, so a
    fourth caller cannot reintroduce the same gap quietly.
    """

    CALLERS = ("app/api/v2_sample.py", "app/api/v2_schema.py", "app/api/v2_scan.py")

    def test_no_caller_hand_rolls_the_read_expression(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        offenders = [p for p in self.CALLERS if "read_parquet(?" in (root / p).read_text()]

        assert not offenders, (
            f"{offenders} build their own read_parquet target; import "
            "app.utils.LOCAL_PARQUET_READ_EXPR instead so hive tables keep working"
        )

    def test_every_caller_imports_the_shared_expression(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        missing = [
            p
            for p in self.CALLERS
            if "resolve_local_parquet_glob" in (root / p).read_text()
            and "LOCAL_PARQUET_READ_EXPR" not in (root / p).read_text()
        ]

        assert not missing, f"{missing} resolve a local parquet target but do not read it through the contract"
