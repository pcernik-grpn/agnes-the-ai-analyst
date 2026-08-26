"""SyncOrchestrator._update_sync_state must store the content MD5.

`agnes pull` re-hashes the downloaded parquet bytes and compares against
the manifest's hash for that table. If the orchestrator stores a
fingerprint (mtime+size) or a truncated MD5, every `agnes pull` of a
Keboola local-mode table fails with `hash mismatch: expected … got …`.
"""

import hashlib
import logging
from unittest.mock import patch

import duckdb
import pytest

from src.db import _ensure_schema
from src.orchestrator import SyncOrchestrator
from src.repositories.sync_state import SyncStateRepository
from src.repositories.table_registry import TableRegistryRepository


@pytest.fixture
def system_db_path(tmp_path):
    """Path to a system.duckdb the orchestrator opens via get_system_db."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    try:
        _ensure_schema(conn)
        TableRegistryRepository(conn).register(
            id="orders",
            name="orders",
            source_type="keboola",
            bucket="in.c-crm",
            source_table="orders",
            query_mode="local",
            description="",
        )
    finally:
        conn.close()
    return db_path


@pytest.fixture
def parquet_with_known_md5(tmp_path):
    """Lay down /tmp/data/extracts/keboola/data/orders.parquet with bytes
    whose MD5 the test knows up front."""
    extracts = tmp_path / "extracts" / "keboola" / "data"
    extracts.mkdir(parents=True)
    pq = extracts / "orders.parquet"
    bytes_payload = b"PAR1" + b"x" * 1024 + b"PAR1"
    pq.write_bytes(bytes_payload)
    return pq, hashlib.md5(bytes_payload).hexdigest()


def _run_update(system_db_path, meta_rows, data_dir):
    """Helper: invoke `_update_sync_state` with `get_system_db` redirected
    at our test DB and `_get_extracts_dir` redirected at our temp tree."""

    def fake_get_system_db():
        return duckdb.connect(str(system_db_path))

    # The orchestrator now writes sync_state through the repo factory, which
    # binds get_system_db at src.repositories import time — patch both the
    # source and the factory's binding so the redirect takes effect.
    with (
        patch("src.db.get_system_db", side_effect=fake_get_system_db),
        patch("src.repositories.get_system_db", side_effect=fake_get_system_db),
        patch("src.orchestrator._get_extracts_dir", return_value=data_dir / "extracts"),
    ):
        orch = SyncOrchestrator.__new__(SyncOrchestrator)
        orch._update_sync_state(meta_rows=meta_rows, source_name="keboola")


def test_update_sync_state_stores_content_md5(system_db_path, parquet_with_known_md5, tmp_path):
    """The hash written into sync_state must equal MD5 of the parquet's
    raw bytes, full 32 hex chars — same shape as the CLI's `_md5_file`."""
    pq_path, expected_md5 = parquet_with_known_md5
    _run_update(
        system_db_path,
        meta_rows=[("orders", 100, pq_path.stat().st_size, "local")],
        data_dir=tmp_path,
    )

    conn = duckdb.connect(str(system_db_path))
    try:
        state = SyncStateRepository(conn).get_table_state("orders")
    finally:
        conn.close()

    assert state is not None, "sync_state row should exist"
    stored = state["hash"]
    assert stored == expected_md5, (
        f"sync_state.hash must be the content MD5 ({expected_md5}) "
        f"so `agnes pull` post-download integrity check passes; got {stored!r}"
    )
    assert len(stored) == 32, "full hex MD5, not truncated"


def test_update_sync_state_empty_hash_when_parquet_missing(system_db_path, tmp_path):
    """If the parquet isn't on disk (race / failed extract), store empty
    string rather than crashing or writing a stale hash."""
    (tmp_path / "extracts" / "keboola" / "data").mkdir(parents=True)
    _run_update(
        system_db_path,
        meta_rows=[("orders", 0, 0, "local")],
        data_dir=tmp_path,
    )

    conn = duckdb.connect(str(system_db_path))
    try:
        state = SyncStateRepository(conn).get_table_state("orders")
    finally:
        conn.close()
    assert state is not None
    assert state["hash"] == ""


def test_update_sync_state_warns_when_both_layouts_present(system_db_path, parquet_with_known_md5, tmp_path, caplog):
    """A flat parquet sitting beside a partition dir freezes distribution
    SILENTLY, so it has to be logged.

    `_update_sync_state` prefers the flat file, which means the manifest
    advertises the table as single-file hashed from the STALE parquet. The
    client downloads it and the md5 MATCHES — `agnes pull` reports success
    while the analyst keeps receiving pre-conversion data indefinitely and the
    server's own view reads the fresh partitions. Nothing else surfaces this,
    and nothing removes the stale sibling (a Keboola table flipped to
    `sync_strategy: partitioned` leaves it behind; the client-side
    `_drop_stale_layout` has no server equivalent).
    """
    pq_path, _ = parquet_with_known_md5
    # Same table, now ALSO partitioned: extracts/keboola/data/orders/<part>.
    part_dir = pq_path.parent / "orders" / "month=2026-06"
    part_dir.mkdir(parents=True)
    (part_dir / "data.parquet").write_bytes(b"PAR1fresh-partitioned-dataPAR1")

    with caplog.at_level("WARNING"):
        _run_update(
            system_db_path,
            meta_rows=[("orders", 100, pq_path.stat().st_size, "local")],
            data_dir=tmp_path,
        )

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("BOTH a flat parquet" in w and "orders" in w for w in warnings), (
        f"both-layouts-on-disk must warn — it is otherwise invisible; got {warnings!r}"
    )

    # Logging only: the warning must not change what gets written. Precedence
    # is unchanged (flat file still wins, `parts` still NULL) — flipping it is
    # the deferred follow-up the TODO in `_update_sync_state` describes.
    conn = duckdb.connect(str(system_db_path))
    try:
        state = SyncStateRepository(conn).get_table_state("orders")
    finally:
        conn.close()
    assert state["hash"] == hashlib.md5(pq_path.read_bytes()).hexdigest(), (
        "the flat file must still win — this change only reports the condition"
    )
    assert not state.get("parts"), "parts must still be NULL when the flat file wins"


def test_update_sync_state_silent_when_only_one_layout_present(
    system_db_path, parquet_with_known_md5, tmp_path, caplog
):
    """The healthy single-file case must NOT warn, or the signal is noise."""
    pq_path, _ = parquet_with_known_md5
    with caplog.at_level("WARNING"):
        _run_update(
            system_db_path,
            meta_rows=[("orders", 100, pq_path.stat().st_size, "local")],
            data_dir=tmp_path,
        )
    assert not [r for r in caplog.records if "BOTH a flat parquet" in r.getMessage()], (
        "a normal single-file table must not warn"
    )


# ---------------------------------------------------------------------------
# Partitioned tables: per-part hashing (partitioned distribution).
# A table stored as a directory of parquet parts (Jira hive
# `month=*/data.parquet`, Keboola flat `<key>.parquet`) gets a `parts`
# list + a rollup hash in sync_state, instead of the empty hash it gets
# today (no single `{table}.parquet` for the single-file path to find).
# ---------------------------------------------------------------------------

from src.orchestrator import _hash_table_parts, _parts_rollup_hash  # noqa: E402


def test_hash_table_parts_hive_layout(tmp_path):
    tdir = tmp_path / "issues"
    (tdir / "month=2026-06").mkdir(parents=True)
    (tdir / "month=2026-07").mkdir(parents=True)
    b6, b7 = b"PAR1" + b"jun" * 10 + b"PAR1", b"PAR1" + b"july" * 20 + b"PAR1"
    (tdir / "month=2026-06" / "data.parquet").write_bytes(b6)
    (tdir / "month=2026-07" / "data.parquet").write_bytes(b7)

    parts, rejected = _hash_table_parts(tdir)
    assert parts == [
        {"path": "month=2026-06/data.parquet", "hash": hashlib.md5(b6).hexdigest(), "size_bytes": len(b6)},
        {"path": "month=2026-07/data.parquet", "hash": hashlib.md5(b7).hexdigest(), "size_bytes": len(b7)},
    ]
    assert rejected == []


def test_hash_table_parts_flat_layout(tmp_path):
    tdir = tmp_path / "cost"
    tdir.mkdir()
    b = b"PAR1" + b"data123" + b"PAR1"
    (tdir / "2025_11.parquet").write_bytes(b)
    parts, rejected = _hash_table_parts(tdir)
    assert parts == [{"path": "2025_11.parquet", "hash": hashlib.md5(b).hexdigest(), "size_bytes": len(b)}]
    assert rejected == []


def test_hash_table_parts_none_when_not_a_dir(tmp_path):
    parts, rejected = _hash_table_parts(tmp_path / "nope")
    assert parts is None
    assert rejected == []


def test_hash_table_parts_none_when_no_parquets(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    (d / "readme.txt").write_text("x")
    parts, rejected = _hash_table_parts(d)
    assert parts is None
    assert rejected == []


# ---------------------------------------------------------------------------
# #1364 — refuse a corrupt parquet part at hash time, so it never enters the
# manifest and never gets distributed. The check is structural only (leading
# + trailing PAR1 magic): cheap, catches truncation/footerless writes (the
# #1354 failure mode), NOT subtle internal corruption.
# ---------------------------------------------------------------------------

CORRUPT_PARQUET_BYTES = b"PAR1" + b"\x00" * 64  # good header, no footer magic — truncated-write shape


def test_hash_table_parts_rejects_corrupt_part_and_warns(tmp_path, caplog):
    """A corrupt part is excluded from `parts` and reported in `rejected`;
    a WARNING names the exact path and the reason."""
    tdir = tmp_path / "issues"
    tdir.mkdir()
    (tdir / "month=2026-01" / "data.parquet").parent.mkdir(parents=True)
    (tdir / "month=2026-01" / "data.parquet").write_bytes(CORRUPT_PARQUET_BYTES)

    with caplog.at_level("WARNING", logger="src.orchestrator"):
        parts, rejected = _hash_table_parts(tdir)

    assert parts is None
    assert rejected == ["month=2026-01/data.parquet"]
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("month=2026-01/data.parquet" in w and "PAR1" in w for w in warnings), (
        f"expected a WARNING naming the corrupt part's path; got {warnings!r}"
    )


def test_hash_table_parts_one_bad_month_does_not_cost_the_table(tmp_path):
    """The healthy parts of the SAME table still hash and are still listed —
    one bad month must not cost the table."""
    tdir = tmp_path / "issues"
    good = b"PAR1" + b"good" * 20 + b"PAR1"
    (tdir / "month=2026-01").mkdir(parents=True)
    (tdir / "month=2026-01" / "data.parquet").write_bytes(CORRUPT_PARQUET_BYTES)
    (tdir / "month=2026-02").mkdir(parents=True)
    (tdir / "month=2026-02" / "data.parquet").write_bytes(good)

    parts, rejected = _hash_table_parts(tdir)
    assert rejected == ["month=2026-01/data.parquet"]
    assert parts == [
        {"path": "month=2026-02/data.parquet", "hash": hashlib.md5(good).hexdigest(), "size_bytes": len(good)}
    ]


def test_hash_table_parts_all_corrupt_returns_none_but_reports_every_rejection(tmp_path):
    """An all-corrupt table degrades to the same `parts=None` contract as an
    empty directory — nothing publishable, but every bad part is named."""
    tdir = tmp_path / "issues"
    (tdir / "month=2026-01").mkdir(parents=True)
    (tdir / "month=2026-01" / "data.parquet").write_bytes(CORRUPT_PARQUET_BYTES)
    (tdir / "month=2026-02").mkdir(parents=True)
    (tdir / "month=2026-02" / "data.parquet").write_bytes(CORRUPT_PARQUET_BYTES)

    parts, rejected = _hash_table_parts(tdir)
    assert parts is None
    assert set(rejected) == {"month=2026-01/data.parquet", "month=2026-02/data.parquet"}


def test_hash_table_parts_accepts_real_pyarrow_parquet(tmp_path):
    """Guard against a check that rejects everything: a valid parquet
    written by the real writer (pyarrow), not a hand-built byte string,
    must pass."""
    pa = pytest.importorskip("pyarrow")
    pq_mod = pytest.importorskip("pyarrow.parquet")
    tdir = tmp_path / "orders"
    tdir.mkdir()
    table = pa.table({"id": [1, 2, 3], "amount": [10.0, 20.0, 30.0]})
    pq_mod.write_table(table, tdir / "2026_01.parquet")

    parts, rejected = _hash_table_parts(tdir)
    assert rejected == []
    assert parts is not None
    assert parts[0]["path"] == "2026_01.parquet"
    assert parts[0]["hash"] == hashlib.md5((tdir / "2026_01.parquet").read_bytes()).hexdigest()


def test_hash_table_parts_accepts_encrypted_footer_magic(tmp_path):
    """An encrypted-footer parquet carries PARE at BOTH ends, not just the
    tail — pyarrow's own `verify_file_encrypted` asserts the file's FIRST
    four bytes are PARE. A head check pinned to PAR1 would refuse every
    such file as corrupt and make this branch unreachable (Devin review
    on #1559)."""
    tdir = tmp_path / "orders"
    tdir.mkdir()
    b = b"PARE" + b"x" * 32 + b"PARE"
    (tdir / "2026_01.parquet").write_bytes(b)

    parts, rejected = _hash_table_parts(tdir)
    assert rejected == []
    assert parts == [{"path": "2026_01.parquet", "hash": hashlib.md5(b).hexdigest(), "size_bytes": len(b)}]


def test_hash_table_parts_rejects_mismatched_end_magics(tmp_path):
    """The two ends must AGREE. A PAR1 head with a PARE tail (or the
    reverse) is not a shape the format produces, so it reads as damage
    rather than as an encrypted file."""
    tdir = tmp_path / "orders"
    tdir.mkdir()
    (tdir / "mixed_a.parquet").write_bytes(b"PAR1" + b"x" * 32 + b"PARE")
    (tdir / "mixed_b.parquet").write_bytes(b"PARE" + b"x" * 32 + b"PAR1")

    parts, rejected = _hash_table_parts(tdir)
    # Every part rejected -> the documented all-rejected contract (None),
    # same shape as an empty directory; both paths still reported.
    assert parts is None
    assert sorted(rejected) == ["mixed_a.parquet", "mixed_b.parquet"]


def test_merge_frozen_parts_keeps_prior_good_entry_for_rejected_path():
    """The core of the #1364 fix: a rejected path with a prior known-good
    entry is reintroduced UNCHANGED, not dropped — dropping it would make
    `agnes pull`'s `_diff_parts` prune (delete) the analyst's local copy."""
    from src.orchestrator import _merge_frozen_parts

    fresh = [{"path": "month=2026-02/data.parquet", "hash": "freshhash", "size_bytes": 10}]
    rejected = ["month=2026-01/data.parquet"]
    previous_by_path = {
        "month=2026-01/data.parquet": {"path": "month=2026-01/data.parquet", "hash": "oldgoodhash", "size_bytes": 5},
    }
    merged = _merge_frozen_parts(fresh, rejected, previous_by_path)
    assert {"path": "month=2026-01/data.parquet", "hash": "oldgoodhash", "size_bytes": 5} in merged
    assert len(merged) == 2


def test_merge_frozen_parts_omits_rejected_path_with_no_prior_entry():
    """A rejected path that was never distributed good has nothing local to
    protect — it stays omitted."""
    from src.orchestrator import _merge_frozen_parts

    merged = _merge_frozen_parts([], ["month=2026-01/data.parquet"], {})
    assert merged == []


def test_parts_rollup_hash_order_independent_and_full_md5():
    a = [
        {"path": "month=1/data.parquet", "hash": "aa", "size_bytes": 1},
        {"path": "month=2/data.parquet", "hash": "bb", "size_bytes": 2},
    ]
    assert _parts_rollup_hash(a) == _parts_rollup_hash(list(reversed(a)))
    assert len(_parts_rollup_hash(a)) == 32


def test_parts_rollup_hash_changes_when_a_part_changes():
    a = [{"path": "month=1/data.parquet", "hash": "aa", "size_bytes": 1}]
    b = [{"path": "month=1/data.parquet", "hash": "cc", "size_bytes": 1}]
    assert _parts_rollup_hash(a) != _parts_rollup_hash(b)


def test_update_sync_state_stores_parts_for_partitioned_table(system_db_path, tmp_path):
    """A table whose data is a directory of parts (no single {table}.parquet)
    gets a parts list + rollup hash + summed size in sync_state."""
    tdir = tmp_path / "extracts" / "keboola" / "data" / "orders" / "month=2026-06"
    tdir.mkdir(parents=True)
    b = b"PAR1" + b"y" * 512 + b"PAR1"
    (tdir / "data.parquet").write_bytes(b)

    _run_update(system_db_path, meta_rows=[("orders", 50, 0, "local")], data_dir=tmp_path)

    conn = duckdb.connect(str(system_db_path))
    try:
        state = SyncStateRepository(conn).get_table_state("orders")
    finally:
        conn.close()

    assert state["parts"] == [
        {"path": "month=2026-06/data.parquet", "hash": hashlib.md5(b).hexdigest(), "size_bytes": len(b)}
    ]
    assert state["hash"] == _parts_rollup_hash(state["parts"])
    assert state["file_size_bytes"] == len(b)


def test_update_sync_state_single_file_still_has_no_parts(system_db_path, parquet_with_known_md5, tmp_path):
    """Backward-compat: a single-file table writes parts=None (NULL)."""
    pq_path, _ = parquet_with_known_md5
    _run_update(
        system_db_path,
        meta_rows=[("orders", 100, pq_path.stat().st_size, "local")],
        data_dir=tmp_path,
    )
    conn = duckdb.connect(str(system_db_path))
    try:
        state = SyncStateRepository(conn).get_table_state("orders")
    finally:
        conn.close()
    assert state["parts"] is None


# ---------------------------------------------------------------------------
# #1364 end-to-end through `_update_sync_state`: a corrupt part/file must
# never be published with a hash describing its corrupt bytes, and — the
# STOP-AND-VERIFY finding this fix is built around — when the analyst
# already has a good local copy, that copy must be neither overwritten NOR
# pruned. `cli/lib/pull.py::_diff_parts` treats "on disk locally, absent
# from the fresh manifest" as an intentional server-side deletion and
# PRUNES it (`test_diff_parts_prunes_dropped_month`), so naive omission
# would delete the good copy — the opposite of the goal. The fix instead
# freezes a corrupt part's manifest entry at its last known-good hash when
# one exists, and omits it outright only when nothing was ever distributed.
# ---------------------------------------------------------------------------


def test_update_sync_state_partitioned_corrupt_part_excluded_when_no_prior_state(system_db_path, tmp_path, caplog):
    """(a) First-ever sync, one corrupt part: excluded from the manifest —
    nothing local depends on it yet — and a WARNING names it."""
    tdir = tmp_path / "extracts" / "keboola" / "data" / "orders" / "month=2026-01"
    tdir.mkdir(parents=True)
    (tdir / "data.parquet").write_bytes(CORRUPT_PARQUET_BYTES)

    with caplog.at_level("WARNING", logger="src.orchestrator"):
        _run_update(system_db_path, meta_rows=[("orders", 0, 0, "local")], data_dir=tmp_path)

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("month=2026-01/data.parquet" in w for w in warnings)

    conn = duckdb.connect(str(system_db_path))
    try:
        state = SyncStateRepository(conn).get_table_state("orders")
    finally:
        conn.close()
    assert state is not None
    assert not state.get("parts")
    assert state["hash"] == ""


def test_update_sync_state_partitioned_healthy_part_still_published_alongside_corrupt_one(system_db_path, tmp_path):
    """(b) One bad month must not cost the table: the healthy sibling part
    still hashes and is still listed."""
    base = tmp_path / "extracts" / "keboola" / "data" / "orders"
    (base / "month=2026-01").mkdir(parents=True)
    (base / "month=2026-01" / "data.parquet").write_bytes(CORRUPT_PARQUET_BYTES)
    good = b"PAR1" + b"good" * 20 + b"PAR1"
    (base / "month=2026-02").mkdir(parents=True)
    (base / "month=2026-02" / "data.parquet").write_bytes(good)

    _run_update(system_db_path, meta_rows=[("orders", 0, 0, "local")], data_dir=tmp_path)

    conn = duckdb.connect(str(system_db_path))
    try:
        state = SyncStateRepository(conn).get_table_state("orders")
    finally:
        conn.close()
    assert state["parts"] == [
        {"path": "month=2026-02/data.parquet", "hash": hashlib.md5(good).hexdigest(), "size_bytes": len(good)}
    ]


def test_update_sync_state_partitioned_corrupt_part_with_prior_good_copy_is_frozen_not_pruned(system_db_path, tmp_path):
    """The STOP-AND-VERIFY case: a part that WAS published good, then goes
    corrupt on a later rebuild, keeps its LAST KNOWN-GOOD manifest entry
    instead of being dropped — dropping it would make `agnes pull` prune
    (delete) the analyst's already-downloaded good copy."""
    base = tmp_path / "extracts" / "keboola" / "data" / "orders"
    good = b"PAR1" + b"good-january" * 5 + b"PAR1"
    (base / "month=2026-01").mkdir(parents=True)
    part_path = base / "month=2026-01" / "data.parquet"
    part_path.write_bytes(good)

    # First pass: publishes the good hash.
    _run_update(system_db_path, meta_rows=[("orders", 0, 0, "local")], data_dir=tmp_path)

    conn = duckdb.connect(str(system_db_path))
    try:
        before = SyncStateRepository(conn).get_table_state("orders")
    finally:
        conn.close()
    assert before["parts"] == [
        {"path": "month=2026-01/data.parquet", "hash": hashlib.md5(good).hexdigest(), "size_bytes": len(good)}
    ]

    # The part goes corrupt on disk (e.g. a killed webhook write) before the
    # next rebuild — the SAME failure mode #1354 documents.
    part_path.write_bytes(CORRUPT_PARQUET_BYTES)
    _run_update(system_db_path, meta_rows=[("orders", 0, 0, "local")], data_dir=tmp_path)

    conn = duckdb.connect(str(system_db_path))
    try:
        after = SyncStateRepository(conn).get_table_state("orders")
    finally:
        conn.close()
    # Frozen: identical manifest entry to before the corruption, NOT dropped
    # and NOT the corrupt bytes' own (self-consistent) hash.
    assert after["parts"] == before["parts"]
    assert after["hash"] == before["hash"]


def test_update_sync_state_partitioned_all_corrupt_first_sync_publishes_nothing(system_db_path, tmp_path):
    """(c) All-corrupt table, never synced before: degrades to the same
    empty contract as an empty directory — nothing publishable, no crash."""
    base = tmp_path / "extracts" / "keboola" / "data" / "orders"
    for month in ("2026-01", "2026-02"):
        d = base / f"month={month}"
        d.mkdir(parents=True)
        (d / "data.parquet").write_bytes(CORRUPT_PARQUET_BYTES)

    _run_update(system_db_path, meta_rows=[("orders", 0, 0, "local")], data_dir=tmp_path)

    conn = duckdb.connect(str(system_db_path))
    try:
        state = SyncStateRepository(conn).get_table_state("orders")
    finally:
        conn.close()
    assert state is not None
    assert not state.get("parts")
    assert state["hash"] == ""


def test_update_sync_state_partitioned_all_corrupt_with_prior_state_stays_fully_frozen(system_db_path, tmp_path):
    """(c) All-corrupt table that WAS fully synced before: the whole
    table's manifest entry freezes unchanged rather than collapsing to
    empty — every already-downloaded part stays protected from pruning."""
    base = tmp_path / "extracts" / "keboola" / "data" / "orders"
    goods = {}
    for month in ("2026-01", "2026-02"):
        d = base / f"month={month}"
        d.mkdir(parents=True)
        b = f"PAR1good-{month}".encode() + b"PAR1"
        (d / "data.parquet").write_bytes(b)
        goods[month] = b

    _run_update(system_db_path, meta_rows=[("orders", 0, 0, "local")], data_dir=tmp_path)
    conn = duckdb.connect(str(system_db_path))
    try:
        before = SyncStateRepository(conn).get_table_state("orders")
    finally:
        conn.close()
    assert before["parts"] is not None and len(before["parts"]) == 2

    for month in ("2026-01", "2026-02"):
        (base / f"month={month}" / "data.parquet").write_bytes(CORRUPT_PARQUET_BYTES)
    _run_update(system_db_path, meta_rows=[("orders", 0, 0, "local")], data_dir=tmp_path)

    conn = duckdb.connect(str(system_db_path))
    try:
        after = SyncStateRepository(conn).get_table_state("orders")
    finally:
        conn.close()
    assert after["parts"] == before["parts"]
    assert after["hash"] == before["hash"]


def test_update_sync_state_single_file_corrupt_no_prior_state_publishes_nothing(system_db_path, tmp_path, caplog):
    """Single-file sibling, (a): a corrupt flat parquet with no prior good
    sync leaves no sync_state row — never published — and warns."""
    extracts = tmp_path / "extracts" / "keboola" / "data"
    extracts.mkdir(parents=True)
    (extracts / "orders.parquet").write_bytes(CORRUPT_PARQUET_BYTES)

    with caplog.at_level("WARNING", logger="src.orchestrator"):
        _run_update(system_db_path, meta_rows=[("orders", 0, 0, "local")], data_dir=tmp_path)

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("orders.parquet" in w and "PAR1" in w for w in warnings)

    conn = duckdb.connect(str(system_db_path))
    try:
        state = SyncStateRepository(conn).get_table_state("orders")
    finally:
        conn.close()
    assert state is None


def test_update_sync_state_single_file_corrupt_with_prior_good_state_is_frozen(system_db_path, tmp_path):
    """Single-file sibling: a table that WAS published good, then its flat
    parquet goes corrupt on a later rebuild, keeps its LAST KNOWN-GOOD
    sync_state row untouched — not overwritten with the corrupt bytes'
    (self-consistent) hash, and not blanked out either."""
    extracts = tmp_path / "extracts" / "keboola" / "data"
    extracts.mkdir(parents=True)
    pq_path = extracts / "orders.parquet"
    good = b"PAR1" + b"good-data" * 10 + b"PAR1"
    pq_path.write_bytes(good)

    _run_update(system_db_path, meta_rows=[("orders", 100, pq_path.stat().st_size, "local")], data_dir=tmp_path)
    conn = duckdb.connect(str(system_db_path))
    try:
        before = SyncStateRepository(conn).get_table_state("orders")
    finally:
        conn.close()
    assert before["hash"] == hashlib.md5(good).hexdigest()

    pq_path.write_bytes(CORRUPT_PARQUET_BYTES)
    _run_update(system_db_path, meta_rows=[("orders", 100, pq_path.stat().st_size, "local")], data_dir=tmp_path)

    conn = duckdb.connect(str(system_db_path))
    try:
        after = SyncStateRepository(conn).get_table_state("orders")
    finally:
        conn.close()
    assert after["hash"] == before["hash"]
    assert after["rows"] == before["rows"]


def test_update_sync_state_single_file_valid_real_writer_parquet_still_passes(system_db_path, tmp_path):
    """(d) Guard against a check that rejects everything: a real
    pyarrow-written single-file parquet still hashes and publishes."""
    pa = pytest.importorskip("pyarrow")
    pq_mod = pytest.importorskip("pyarrow.parquet")
    extracts = tmp_path / "extracts" / "keboola" / "data"
    extracts.mkdir(parents=True)
    pq_path = extracts / "orders.parquet"
    table = pa.table({"id": [1, 2, 3]})
    pq_mod.write_table(table, pq_path)

    _run_update(system_db_path, meta_rows=[("orders", 3, pq_path.stat().st_size, "local")], data_dir=tmp_path)

    conn = duckdb.connect(str(system_db_path))
    try:
        state = SyncStateRepository(conn).get_table_state("orders")
    finally:
        conn.close()
    assert state["hash"] == hashlib.md5(pq_path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Both-layouts collision (#1339): a flat `<table>.parquet` file AND a
# `<table>/` partition directory present at the same time. The flat file
# silently wins today — unchanged by this fix (precedence + stale-sibling
# cleanup are open human decisions, see the TODO(#1339) in the source) — but
# until now the collision was invisible: the manifest kept advertising the
# flat file's (possibly stale) hash with nothing to say a fresher directory
# sat right beside it. This must be loud, not silent.
# ---------------------------------------------------------------------------


def _write_both_layouts(tmp_path):
    """Flat `orders.parquet` + a sibling `orders/` partition directory."""
    extracts = tmp_path / "extracts" / "keboola" / "data"
    extracts.mkdir(parents=True)
    flat_bytes = b"PAR1" + b"stale" * 50 + b"PAR1"
    pq_path = extracts / "orders.parquet"
    pq_path.write_bytes(flat_bytes)
    table_dir = extracts / "orders"
    table_dir.mkdir()
    (table_dir / "2025_11.parquet").write_bytes(b"fresh-partitioned-bytes")
    return pq_path, table_dir, flat_bytes


def test_both_layouts_collision_logs_error_naming_both_paths_and_table(system_db_path, tmp_path, caplog):
    pq_path, table_dir, _ = _write_both_layouts(tmp_path)

    with caplog.at_level(logging.ERROR, logger="src.orchestrator"):
        _run_update(
            system_db_path,
            meta_rows=[("orders", 100, pq_path.stat().st_size, "local")],
            data_dir=tmp_path,
        )

    collision_records = [
        r
        for r in caplog.records
        if r.levelname == "ERROR"
        and "orders" in r.getMessage()
        and str(pq_path) in r.getMessage()
        and str(table_dir) in r.getMessage()
    ]
    assert collision_records, (
        "expected an ERROR log naming the table id and BOTH concrete paths; "
        f"got: {[r.getMessage() for r in caplog.records]}"
    )


def test_both_layouts_collision_flags_sync_state_but_keeps_flat_hash(system_db_path, tmp_path):
    """The served bytes must stay byte-for-byte identical to the flat-only
    case (the flat file still wins) — only the flagging is new."""
    pq_path, table_dir, flat_bytes = _write_both_layouts(tmp_path)

    _run_update(
        system_db_path,
        meta_rows=[("orders", 100, pq_path.stat().st_size, "local")],
        data_dir=tmp_path,
    )

    conn = duckdb.connect(str(system_db_path))
    try:
        state = SyncStateRepository(conn).get_table_state("orders")
    finally:
        conn.close()

    assert state is not None
    # Bytes served: identical to the flat-only case — precedence unchanged.
    assert state["hash"] == hashlib.md5(flat_bytes).hexdigest()
    assert state["parts"] is None
    assert state["rows"] == 100
    # No longer invisible: flagged via the existing sync_state error
    # mechanism (the same set_error() `GET /api/admin/registry` already
    # surfaces as `last_sync_error`).
    assert state["status"] == "error"
    assert str(pq_path) in (state["error"] or "")
    assert str(table_dir) in (state["error"] or "")


def test_flat_only_layout_is_not_flagged_as_a_collision(system_db_path, parquet_with_known_md5, tmp_path, caplog):
    """Regression pin: the ordinary single-file case must keep behaving
    exactly as before — no ERROR log, no sync_state error flip."""
    pq_path, expected_md5 = parquet_with_known_md5
    with caplog.at_level(logging.ERROR, logger="src.orchestrator"):
        _run_update(
            system_db_path,
            meta_rows=[("orders", 100, pq_path.stat().st_size, "local")],
            data_dir=tmp_path,
        )

    assert not [r for r in caplog.records if r.levelname == "ERROR" and "orders" in r.getMessage()]

    conn = duckdb.connect(str(system_db_path))
    try:
        state = SyncStateRepository(conn).get_table_state("orders")
    finally:
        conn.close()
    assert state["hash"] == expected_md5
    assert state["status"] == "ok"
    assert not state.get("error")


def test_dir_only_layout_is_not_flagged_as_a_collision(system_db_path, tmp_path, caplog):
    """Regression pin: the ordinary partitioned-only case (no flat sibling)
    must keep behaving exactly as before either."""
    tdir = tmp_path / "extracts" / "keboola" / "data" / "orders" / "month=2026-06"
    tdir.mkdir(parents=True)
    (tdir / "data.parquet").write_bytes(b"PAR1" + b"y" * 512 + b"PAR1")

    with caplog.at_level(logging.ERROR, logger="src.orchestrator"):
        _run_update(system_db_path, meta_rows=[("orders", 50, 0, "local")], data_dir=tmp_path)

    assert not [r for r in caplog.records if r.levelname == "ERROR" and "orders" in r.getMessage()]

    conn = duckdb.connect(str(system_db_path))
    try:
        state = SyncStateRepository(conn).get_table_state("orders")
    finally:
        conn.close()
    assert state["status"] == "ok"
    assert not state.get("error")
