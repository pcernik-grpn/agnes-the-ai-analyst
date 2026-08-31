"""Tests for the ``distribution-mirror`` LIGHT job kind (three-plane
wave 2-H, WS F, task WF-3 — see
``docs/superpowers/plans/2026-07-20-three-plane-wave2h-distribution.md``).

Covers:

- the mirror handler (``app.worker.kinds._run_distribution_mirror``):
  uploads changed parquets, skips md5-matches, skips ``remote``/
  ``server_only`` rows, writes the marker index, clean no-op when
  ``object_store()`` is ``None`` (never imports ``boto3``);
- the marker-index helpers (``src.distribution.write_mirror_index`` /
  ``read_mirror_index``): round-trip + fail-open on store error;
- registration: ``distribution-mirror`` is a LIGHT-lane kind, alongside the
  other seven real kinds (worker-role gating is generic — see
  ``app/main.py``'s ``role_enabled(Role.WORKER)`` guard around the whole
  worker loop — so there is nothing kind-specific to gate here beyond
  correct lane registration);
- the ``data-refresh`` → ``distribution-mirror`` chain-enqueue, gated on
  ``object_store()`` being configured and the sync having actually run
  (not a no-op).
"""

from __future__ import annotations

import hashlib
import json

import pytest

from tests.object_store_fakes import FakeObjectStore


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


@pytest.fixture(autouse=True)
def clean_job_kinds_registry():
    from app.worker.registry import JOB_KINDS

    JOB_KINDS.clear()
    yield
    JOB_KINDS.clear()


@pytest.fixture
def mirror_env(tmp_path, monkeypatch):
    """A fresh system.duckdb (DATA_DIR-scoped) with a small table_registry +
    sync_state fixture:

    - ``orders`` — query_mode=local, keboola, on-disk parquet, synced.
    - ``sales_report`` — query_mode=materialized, on-disk parquet, synced.
    - ``bq_view`` — query_mode=remote — never has a local parquet.
    - ``internal_report`` — query_mode=local, server_only=True — has a
      parquet on disk (server keeps it fresh) but must never be mirrored.

    Returns the ``tmp_path`` DATA_DIR so tests can inspect/mutate parquet
    bytes directly.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AGNES_DB_URL", raising=False)

    from src.db import close_system_db, get_system_db
    from src.repositories import sync_state_repo, table_registry_repo

    get_system_db()

    extracts = tmp_path / "extracts" / "keboola" / "data"
    extracts.mkdir(parents=True)

    orders_bytes = b"orders-parquet-v1"
    sales_bytes = b"sales-report-parquet-v1"
    internal_bytes = b"internal-report-parquet-v1"

    (extracts / "orders.parquet").write_bytes(orders_bytes)
    (extracts / "sales_report.parquet").write_bytes(sales_bytes)
    (extracts / "internal_report.parquet").write_bytes(internal_bytes)

    registry = table_registry_repo()
    registry.register(id="orders", name="orders", source_type="keboola", query_mode="local")
    registry.register(id="sales_report", name="sales_report", source_type="keboola", query_mode="materialized")
    registry.register(id="bq_view", name="bq_view", source_type="bigquery", query_mode="remote")
    registry.register(
        id="internal_report",
        name="internal_report",
        source_type="keboola",
        query_mode="local",
        server_only=True,
    )

    state = sync_state_repo()
    state.update_sync(table_id="orders", rows=10, file_size_bytes=len(orders_bytes), hash=_md5(orders_bytes))
    state.update_sync(table_id="sales_report", rows=5, file_size_bytes=len(sales_bytes), hash=_md5(sales_bytes))
    state.update_sync(
        table_id="internal_report", rows=1, file_size_bytes=len(internal_bytes), hash=_md5(internal_bytes)
    )
    # bq_view intentionally has no sync_state row (remote tables never get one).

    yield {
        "data_dir": tmp_path,
        "orders_md5": _md5(orders_bytes),
        "sales_md5": _md5(sales_bytes),
        "internal_md5": _md5(internal_bytes),
    }
    close_system_db()


class TestDistributionMirrorHandler:
    def test_uploads_changed_files(self, mirror_env, monkeypatch):
        fake = FakeObjectStore()
        monkeypatch.setattr("src.object_store.object_store", lambda: fake)

        from app.worker.kinds import _run_distribution_mirror

        _run_distribution_mirror({})

        uploaded_keys = {key for _, key, _ in fake.put_file_calls}
        assert uploaded_keys == {"orders.parquet", "sales_report.parquet"}
        assert fake.objects["orders.parquet"] == b"orders-parquet-v1"
        assert fake.metadata["orders.parquet"]["md5"] == mirror_env["orders_md5"]

    def test_skips_md5_matches(self, mirror_env, monkeypatch):
        fake = FakeObjectStore()
        # Pre-seed the store as already current for `orders`.
        fake.metadata["orders.parquet"] = {"md5": mirror_env["orders_md5"]}
        monkeypatch.setattr("src.object_store.object_store", lambda: fake)

        from app.worker.kinds import _run_distribution_mirror

        _run_distribution_mirror({})

        uploaded_keys = {key for _, key, _ in fake.put_file_calls}
        assert "orders.parquet" not in uploaded_keys
        assert "sales_report.parquet" in uploaded_keys

    def test_skips_remote_and_server_only_tables(self, mirror_env, monkeypatch):
        fake = FakeObjectStore()
        monkeypatch.setattr("src.object_store.object_store", lambda: fake)

        from app.worker.kinds import _run_distribution_mirror

        _run_distribution_mirror({})

        uploaded_keys = {key for _, key, _ in fake.put_file_calls}
        assert "bq_view.parquet" not in uploaded_keys
        assert "internal_report.parquet" not in uploaded_keys
        assert "bq_view.parquet" not in fake.objects
        assert "internal_report.parquet" not in fake.objects

    def test_per_file_failure_logs_and_continues(self, mirror_env, monkeypatch):
        fake = FakeObjectStore()
        calls = {"n": 0}
        real_put_file = fake.put_file

        def flaky_put_file(local_path, key, md5):
            calls["n"] += 1
            if key == "orders.parquet":
                raise RuntimeError("simulated upload failure")
            return real_put_file(local_path, key, md5)

        monkeypatch.setattr(fake, "put_file", flaky_put_file)
        monkeypatch.setattr("src.object_store.object_store", lambda: fake)

        from app.worker.kinds import _run_distribution_mirror

        _run_distribution_mirror({})  # must not raise

        assert "orders.parquet" not in fake.objects
        assert "sales_report.parquet" in fake.objects

    def test_writes_mirror_index_for_currently_mirrored_tables(self, mirror_env, monkeypatch):
        fake = FakeObjectStore()
        monkeypatch.setattr("src.object_store.object_store", lambda: fake)

        from app.worker.kinds import _run_distribution_mirror
        from src.distribution import MIRROR_INDEX_KEY

        _run_distribution_mirror({})

        raw = fake.objects[MIRROR_INDEX_KEY]
        payload = json.loads(raw)
        assert payload["tables"] == {
            "orders": mirror_env["orders_md5"],
            "sales_report": mirror_env["sales_md5"],
        }
        assert "updated" in payload

    def test_marker_index_includes_preexisting_current_tables_not_just_this_runs_uploads(self, mirror_env, monkeypatch):
        fake = FakeObjectStore()
        # `orders` is already mirrored+current before this run (skip path);
        # only `sales_report` is a fresh upload this run.
        fake.metadata["orders.parquet"] = {"md5": mirror_env["orders_md5"]}
        monkeypatch.setattr("src.object_store.object_store", lambda: fake)

        from app.worker.kinds import _run_distribution_mirror
        from src.distribution import MIRROR_INDEX_KEY

        _run_distribution_mirror({})

        payload = json.loads(fake.objects[MIRROR_INDEX_KEY])
        assert payload["tables"] == {
            "orders": mirror_env["orders_md5"],
            "sales_report": mirror_env["sales_md5"],
        }

    def test_noop_when_object_store_is_none(self, mirror_env, monkeypatch):
        monkeypatch.setattr("src.object_store.object_store", lambda: None)

        import builtins

        real_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "boto3" or name.startswith("boto3."):
                raise AssertionError("boto3 must not be imported when object_store() is None")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", guarded_import)

        from app.worker.kinds import _run_distribution_mirror

        _run_distribution_mirror({})  # must not raise, must not touch boto3


class TestDistributionMirrorHashVerification:
    """Issue #1360: the mirror used to stamp every uploaded object with
    ``sync_state.hash`` — a label read from the DB — without ever hashing
    the bytes ``put_file`` actually sent. ``head_md5`` then only ever
    compared that stamp against another label, so a parquet that changed
    under the mirror between the ``sync_state`` read and the upload
    produced a permanently mislabeled object: undetectable by construction,
    because every later run's "already current" check is also label vs
    label.

    These two cases reproduce the original bug report (inverted to assert
    the FIXED behavior — pre-fix, both passed as-is because they observed
    the defect) plus a pin on the untouched fast path's I/O cost.
    """

    def test_a_race_after_the_db_read_is_skipped_not_mislabeled(self, mirror_env, monkeypatch):
        """A concurrent sync rewrites `orders.parquet` in the window
        between `head_md5`'s network round trip and the upload — the exact
        TOCTOU window the issue names, since no lock spans it. Before the
        fix this published v2's bytes stamped with v1's hash. Now the
        pre-upload hash disagrees with `sync_state.hash`, so the table is
        skipped this run rather than published mislabeled, and the other
        (unraced) table is unaffected."""
        parquet = mirror_env["data_dir"] / "extracts" / "keboola" / "data" / "orders.parquet"
        v1, v2 = b"orders-parquet-v1", b"orders-parquet-v2-NEWER"
        assert parquet.read_bytes() == v1

        fake = FakeObjectStore()
        real_head = fake.head_md5

        def head_md5_then_sync_lands(key):
            result = real_head(key)
            if key == "orders.parquet":
                parquet.write_bytes(v2)
            return result

        fake.head_md5 = head_md5_then_sync_lands
        monkeypatch.setattr("src.object_store.object_store", lambda: fake)

        from app.worker.kinds import _run_distribution_mirror

        _run_distribution_mirror({})

        assert "orders.parquet" not in fake.objects, "raced content must never be published"
        assert "orders.parquet" not in fake.metadata, "nothing was uploaded, so nothing was stamped"
        uploaded_keys = {key for _, key, _ in fake.put_file_calls}
        assert "orders.parquet" not in uploaded_keys
        assert "sales_report.parquet" in uploaded_keys, "one table racing must not stall the others"

        from src.distribution import MIRROR_INDEX_KEY

        index = json.loads(fake.objects[MIRROR_INDEX_KEY])
        assert "orders" not in index["tables"], "a raced table must not be advertised as current"
        assert index["tables"]["sales_report"] == mirror_env["sales_md5"]

    def test_a_stale_stamp_does_not_license_publishing_whatever_is_on_disk(self, mirror_env, monkeypatch):
        """`head_md5` disagreeing with `sync_state.hash` means "this object
        needs a refresh" — it must not be read as "so trust the disk and
        publish it". Here the store already holds a genuinely stale object
        (due for a refresh with or without any race) and the file ALSO
        races between `head_md5` and the upload, same window as above. The
        old, self-consistent object is left exactly as it was rather than
        replaced with a second, differently-wrong pairing — this is the
        direct fix for the original repro's point that `head_md5` can never
        tell "already current" from "mislabeled", since both compare label
        to label: it no longer has to, because the content is checked
        before anything is published."""
        parquet = mirror_env["data_dir"] / "extracts" / "keboola" / "data" / "orders.parquet"
        v1, v2 = b"orders-parquet-v1", b"orders-parquet-v2-NEWER"
        assert parquet.read_bytes() == v1

        fake = FakeObjectStore()
        stale_md5 = _md5(b"orders-parquet-v0-OLDER")
        fake.metadata["orders.parquet"] = {"md5": stale_md5}
        fake.objects["orders.parquet"] = b"orders-parquet-v0-OLDER"
        real_head = fake.head_md5

        def head_md5_then_sync_lands_again(key):
            result = real_head(key)
            if key == "orders.parquet":
                parquet.write_bytes(v2)
            return result

        fake.head_md5 = head_md5_then_sync_lands_again
        monkeypatch.setattr("src.object_store.object_store", lambda: fake)

        from app.worker.kinds import _run_distribution_mirror

        _run_distribution_mirror({})

        uploaded_keys = {key for _, key, _ in fake.put_file_calls}
        assert "orders.parquet" not in uploaded_keys, "must not publish v2 stamped as v1"
        assert fake.objects["orders.parquet"] == b"orders-parquet-v0-OLDER", "old object left untouched"
        assert fake.metadata["orders.parquet"]["md5"] == stale_md5

        from src.distribution import MIRROR_INDEX_KEY

        index = json.loads(fake.objects[MIRROR_INDEX_KEY])
        assert "orders" not in index["tables"], "a raced table must not be advertised as current"

    def test_an_unchanged_table_is_skipped_without_rehashing_the_file(self, mirror_env, monkeypatch):
        """The pre-upload hash only runs on the path that is about to
        publish something. An already-current table (label matches label —
        the pre-#1360 fast path) still costs exactly one `head_md5` round
        trip and zero local reads, same as before: `distribution-mirror`
        runs on every successful `data-refresh`, so re-hashing every
        unchanged multi-GB parquet on every run would be real, avoidable
        I/O, not a one-off cost."""
        fake = FakeObjectStore()
        fake.metadata["orders.parquet"] = {"md5": mirror_env["orders_md5"]}
        monkeypatch.setattr("src.object_store.object_store", lambda: fake)

        hashed_paths: list = []
        real_hash_file_md5 = None

        def counting_hash_file_md5(path, *args, **kwargs):
            hashed_paths.append(str(path))
            return real_hash_file_md5(path, *args, **kwargs)

        import src.object_store as object_store_module

        real_hash_file_md5 = object_store_module.hash_file_md5
        monkeypatch.setattr("src.object_store.hash_file_md5", counting_hash_file_md5)

        from app.worker.kinds import _run_distribution_mirror

        _run_distribution_mirror({})

        assert not any(p.endswith("orders.parquet") for p in hashed_paths), (
            "an already-current table must not be re-hashed"
        )
        assert any(p.endswith("sales_report.parquet") for p in hashed_paths), (
            "the table actually being uploaded IS hashed before publishing"
        )
        uploaded_keys = {key for _, key, _ in fake.put_file_calls}
        assert "orders.parquet" not in uploaded_keys
        assert "sales_report.parquet" in uploaded_keys


class TestPartitionedTablesAreNotMirrored:
    """A partitioned table stores its data as a DIRECTORY of per-period parquets,
    while the mirror addresses exactly one ``<table_id>.parquet`` object per
    table. It is deliberately left out of the object-store mirror — analysts
    still receive it, per-part, over the app-served ``/api/data/<id>/download
    ?part=`` route (``cli/lib/pull.py::_sync_partitioned_table``), so this is a
    presign acceleration gap, not a distribution gap.

    Pinned because the mirror used to reach the same outcome by ACCIDENT: the
    single-file lookup found nothing and it logged "no on-disk parquet found",
    the same message a genuinely broken sync produces. Operators reading that
    warning were being told a healthy table was broken.
    """

    @pytest.fixture
    def partitioned_env(self, mirror_env):
        from src.repositories import sync_state_repo, table_registry_repo

        data = mirror_env["data_dir"] / "extracts" / "keboola" / "data" / "kbc_sales"
        data.mkdir(parents=True)
        (data / "2025_11.parquet").write_bytes(b"nov-part")
        (data / "2025_12.parquet").write_bytes(b"dec-part")
        parts = [
            {"path": "2025_11.parquet", "hash": _md5(b"nov-part"), "size_bytes": 8},
            {"path": "2025_12.parquet", "hash": _md5(b"dec-part"), "size_bytes": 8},
        ]
        table_registry_repo().register(id="kbc_sales", name="kbc_sales", source_type="keboola", query_mode="local")
        sync_state_repo().update_sync(
            table_id="kbc_sales",
            rows=3,
            file_size_bytes=16,
            hash="rollup-of-the-parts",
            parts=parts,
        )
        return mirror_env

    def test_partitioned_table_is_skipped_without_warning(self, partitioned_env, monkeypatch, caplog):
        fake = FakeObjectStore()
        monkeypatch.setattr("src.object_store.object_store", lambda: fake)

        from app.worker.kinds import _run_distribution_mirror
        from src.distribution import MIRROR_INDEX_KEY

        with caplog.at_level("WARNING", logger="app.worker.kinds"):
            _run_distribution_mirror({})

        assert "kbc_sales.parquet" not in fake.objects
        assert "kbc_sales" not in json.loads(fake.objects[MIRROR_INDEX_KEY])["tables"]
        assert not [r for r in caplog.records if r.levelname == "WARNING" and "kbc_sales" in r.getMessage()]

    def test_single_file_tables_alongside_it_still_mirror(self, partitioned_env, monkeypatch):
        fake = FakeObjectStore()
        monkeypatch.setattr("src.object_store.object_store", lambda: fake)

        from app.worker.kinds import _run_distribution_mirror

        _run_distribution_mirror({})

        assert {key for _, key, _ in fake.put_file_calls} == {"orders.parquet", "sales_report.parquet"}

    def test_a_genuinely_missing_single_file_parquet_still_warns(self, mirror_env, monkeypatch, caplog):
        """The warning must keep firing where it is true: a non-partitioned
        table whose parquet is gone."""
        (mirror_env["data_dir"] / "extracts" / "keboola" / "data" / "orders.parquet").unlink()
        fake = FakeObjectStore()
        monkeypatch.setattr("src.object_store.object_store", lambda: fake)

        from app.worker.kinds import _run_distribution_mirror

        with caplog.at_level("WARNING", logger="app.worker.kinds"):
            _run_distribution_mirror({})

        assert [r for r in caplog.records if r.levelname == "WARNING" and "orders" in r.getMessage()]


class TestMirrorIndexHelpers:
    def test_write_then_read_round_trips(self):
        fake = FakeObjectStore()
        from src.distribution import read_mirror_index, write_mirror_index

        write_mirror_index(fake, {"orders": "abc123", "sales_report": "def456"})

        assert read_mirror_index(fake) == {"orders": "abc123", "sales_report": "def456"}

    def test_read_returns_empty_dict_when_absent(self):
        fake = FakeObjectStore()
        from src.distribution import read_mirror_index

        assert read_mirror_index(fake) == {}

    def test_read_fails_open_on_store_error(self):
        fake = FakeObjectStore()
        fake.fail_get_bytes = True
        from src.distribution import read_mirror_index

        assert read_mirror_index(fake) == {}

    def test_read_fails_open_on_malformed_json(self):
        fake = FakeObjectStore()
        from src.distribution import MIRROR_INDEX_KEY, read_mirror_index

        fake.objects[MIRROR_INDEX_KEY] = b"not json"

        assert read_mirror_index(fake) == {}


class TestDistributionMirrorRegistration:
    def test_registered_as_light_lane_kind(self):
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS, LIGHT_LANE

        register_all_kinds()

        assert "distribution-mirror" in JOB_KINDS
        assert JOB_KINDS["distribution-mirror"].lane == LIGHT_LANE


@pytest.fixture
def jobs_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AGNES_DB_URL", raising=False)
    from src.db import close_system_db, get_system_db

    get_system_db()
    yield
    close_system_db()


class TestChainedEnqueueAfterDataRefresh:
    def test_enqueues_distribution_mirror_when_store_configured_and_sync_ok(self, jobs_db, monkeypatch):
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS
        from src.repositories import jobs_repo

        register_all_kinds()
        monkeypatch.setattr(
            "app.api.sync._run_sync", lambda tables=None, source_type_filter=None, result_sink=None: True
        )
        monkeypatch.setattr("src.object_store.object_store", lambda: FakeObjectStore())

        JOB_KINDS["data-refresh"].handler({})

        rows = jobs_repo().list(kind="distribution-mirror")
        assert len(rows) == 1

    def test_does_not_enqueue_when_no_object_store_configured(self, jobs_db, monkeypatch):
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS
        from src.repositories import jobs_repo

        register_all_kinds()
        monkeypatch.setattr(
            "app.api.sync._run_sync", lambda tables=None, source_type_filter=None, result_sink=None: True
        )
        monkeypatch.setattr("src.object_store.object_store", lambda: None)

        JOB_KINDS["data-refresh"].handler({})

        rows = jobs_repo().list(kind="distribution-mirror")
        assert rows == []

    def test_does_not_enqueue_when_sync_was_a_noop(self, jobs_db, monkeypatch):
        """`ok is None` means another same-process `_run_sync` call already
        held the lock — a rebuild may still be in flight, so mirroring now
        would risk reading half-written parquet. Only a clean `True` run
        triggers the follow-up."""
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS
        from src.repositories import jobs_repo

        register_all_kinds()
        monkeypatch.setattr(
            "app.api.sync._run_sync", lambda tables=None, source_type_filter=None, result_sink=None: None
        )
        monkeypatch.setattr("src.object_store.object_store", lambda: FakeObjectStore())

        JOB_KINDS["data-refresh"].handler({})

        rows = jobs_repo().list(kind="distribution-mirror")
        assert rows == []

    def test_does_not_enqueue_when_sync_failed(self, jobs_db, monkeypatch):
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS
        from src.repositories import jobs_repo

        register_all_kinds()
        monkeypatch.setattr(
            "app.api.sync._run_sync", lambda tables=None, source_type_filter=None, result_sink=None: False
        )
        monkeypatch.setattr("src.object_store.object_store", lambda: FakeObjectStore())

        with pytest.raises(RuntimeError):
            JOB_KINDS["data-refresh"].handler({})

        rows = jobs_repo().list(kind="distribution-mirror")
        assert rows == []
