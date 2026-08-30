"""Performance smoke tests for ``StackResolver`` + manifest generation.

Targets from Section 10.9 of the design doc:

  - ``StackResolver.stack(user_id, DATA_PACKAGE)`` averaged over 20 calls
    against a fixture of 1000 users × 50 groups × 200 resources × 800
    grants → **< 50 ms** per call.
  - Manifest generation (``_build_data_packages_section`` +
    ``_build_memory_domains_section`` + ``_build_direct_tables_section``)
    against 100 packages × 20 tables each → **< 200 ms** by design; the
    knob below carries the looser number CI is actually held to.

Thresholds are guidance, not hard gates: over the target records the actual
time and warns, and only a multiple of it (``PERF_CEILING_FACTOR``) fails
the build. Both benchmarks go through ``tests.perf_policy.check_perf``, which
is where that policy lives — and which exists because the plain ``assert``
these used to carry contradicted this paragraph and blocked PRs on runner
noise instead. It is shared rather than local because the same contradiction
sat in ``test_grants_soft_downgrade`` too.
"""

from __future__ import annotations

import os
import time
import uuid


import pytest

from tests.perf_policy import PERF_CEILING_FACTOR, check_perf

from src.db import get_system_db

# pytest.ini's global 60 s timeout is sized for ordinary tests; these two seed
# 1000 users x 50 groups x 200 packages x 800 grants and 100 packages x 20
# tables before they measure anything, and the SEEDING is what has repeatedly
# blown the limit on a contended CI runner — not the thing under test. When it
# fires, pytest-timeout reports "Timeout (>60.0s)", which reads as a
# performance regression and sends the next reader to the wrong place; the
# actual assertions are already flake-tolerant by design (see the module
# docstring: `check_perf` warns at the target and only fails at a multiple of
# it). Locally the whole file is under 8 s, so this ceiling is a hang detector,
# not a budget.
pytestmark = pytest.mark.timeout(300)


# ---------------------------------------------------------------------------
# Knobs
# ---------------------------------------------------------------------------

RESOLVER_TARGET_MS = float(os.environ.get("AGNES_PERF_RESOLVER_MS", "50"))
# Bumped 200 → 500 → 600 → 1000 after persistent CI flake. The actual
# wall-clock for the 100-pkg × 20-tbl fixture in CI cold-cache runs lands
# between 180-550ms, but a contended runner has been seen at 1293ms.
# Tighten via the env var when running on a hot machine.
MANIFEST_TARGET_MS = float(os.environ.get("AGNES_PERF_MANIFEST_MS", "1000"))

# ---------------------------------------------------------------------------
# Fixture seeding helpers
# ---------------------------------------------------------------------------


def _seed_resolver_fixture(
    conn,
    *,
    n_users: int = 1000,
    n_groups: int = 50,
    n_packages: int = 200,
    n_grants: int = 800,
) -> str:
    """Seed users, groups, memberships, data_packages, grants. Returns the
    id of a "representative" user with several group memberships."""
    import random

    rng = random.Random(0)

    # Users
    for i in range(n_users):
        conn.execute(
            "INSERT INTO users(id, email) VALUES (?, ?)",
            [f"u{i}", f"u{i}@x.test"],
        )
    # Groups
    group_ids = []
    for i in range(n_groups):
        gid = f"g{i}"
        conn.execute(
            "INSERT INTO user_groups(id, name, description, created_by) VALUES (?, ?, '', 'test')",
            [gid, f"perf_g{i}"],
        )
        group_ids.append(gid)
    # Memberships — each user joins 3 random groups.
    for i in range(n_users):
        for gid in rng.sample(group_ids, k=min(3, n_groups)):
            conn.execute(
                "INSERT INTO user_group_members(user_id, group_id, source) VALUES (?, ?, 'test')",
                [f"u{i}", gid],
            )
    # Packages
    pkg_ids = []
    for i in range(n_packages):
        pid = f"pkg_{i:04d}"
        conn.execute(
            "INSERT INTO data_packages(id, slug, name) VALUES (?, ?, ?)",
            [pid, f"slug-{i}", f"Pkg {i}"],
        )
        pkg_ids.append(pid)
    # Grants — each grant binds one group → one package.
    for i in range(n_grants):
        gid = group_ids[i % n_groups]
        pid = pkg_ids[i % n_packages]
        requirement = "required" if i % 7 == 0 else "available"
        try:
            conn.execute(
                "INSERT INTO resource_grants(id, group_id, resource_type, "
                "resource_id, requirement, assigned_at, assigned_by) "
                "VALUES (?, ?, 'data_package', ?, ?, CURRENT_TIMESTAMP, 'test')",
                [str(uuid.uuid4()), gid, pid, requirement],
            )
        except Exception:
            # UNIQUE constraint on (group, type, resource) — skip dupes.
            pass

    return "u0"  # caller benchmarks this user's stack()


# ---------------------------------------------------------------------------
# Benchmark — StackResolver.stack()
# ---------------------------------------------------------------------------


def test_stack_resolver_perf_smoke(seeded_app):
    """1000 users × 50 groups × 200 resources × 800 grants — stack() < 50 ms avg."""
    from app.services.stack_resolver import StackResolver
    from app.resource_types import ResourceType

    conn = get_system_db()
    uid = _seed_resolver_fixture(conn)
    resolver = StackResolver(conn)

    # Warm-up: one call to populate any DuckDB query cache.
    resolver.stack(uid, ResourceType.DATA_PACKAGE)

    N = 20
    t0 = time.perf_counter()
    for _ in range(N):
        resolver.stack(uid, ResourceType.DATA_PACKAGE)
    elapsed = (time.perf_counter() - t0) * 1000.0
    avg_ms = elapsed / N
    conn.close()

    # Soft-gate: print the actual number so a regression is visible in CI
    # logs even when the threshold is generous; assert against the target.
    print(f"\nstack_resolver.stack() avg over {N} calls: {avg_ms:.2f} ms")
    check_perf("StackResolver.stack() avg", avg_ms, RESOLVER_TARGET_MS)


# ---------------------------------------------------------------------------
# Benchmark — manifest generation
# ---------------------------------------------------------------------------


def _seed_manifest_fixture(
    conn,
    *,
    n_packages: int = 100,
    tables_per_pkg: int = 20,
) -> str:
    """Seed a single user with grants on N packages, each having T tables."""
    # Single user in a single group; admin god-mode would short-circuit so
    # we use a regular user.
    conn.execute("INSERT INTO users(id, email) VALUES ('perf_u', 'perf@x.test')")
    conn.execute("INSERT INTO user_groups(id, name, description, created_by) VALUES ('perf_g', 'perf_g', '', 'test')")
    conn.execute("INSERT INTO user_group_members(user_id, group_id, source) VALUES ('perf_u', 'perf_g', 'test')")
    pkg_ids = []
    for i in range(n_packages):
        pid = f"mpkg_{i:04d}"
        conn.execute(
            "INSERT INTO data_packages(id, slug, name) VALUES (?, ?, ?)",
            [pid, f"mslug-{i}", f"MPkg {i}"],
        )
        pkg_ids.append(pid)
        # Each package has T tables. Use a stable id pattern so registry
        # lookups in the manifest builder resolve cleanly.
        for j in range(tables_per_pkg):
            tid = f"tbl_{i:04d}_{j:02d}"
            conn.execute(
                """INSERT INTO table_registry
                   (id, name, source_type, bucket, source_table, query_mode,
                    registered_at, profile_after_sync)
                   VALUES (?, ?, 'keboola', 'b', ?, 'local',
                           CURRENT_TIMESTAMP, FALSE)""",
                [tid, tid, tid],
            )
            conn.execute(
                "INSERT INTO data_package_tables(package_id, table_id, added_by) VALUES (?, ?, 'test')",
                [pid, tid],
            )
        # Grant the user's group access (required to short-circuit subscribe).
        conn.execute(
            "INSERT INTO resource_grants(id, group_id, resource_type, "
            "resource_id, requirement, assigned_at, assigned_by) "
            "VALUES (?, 'perf_g', 'data_package', ?, 'required', "
            "CURRENT_TIMESTAMP, 'test')",
            [str(uuid.uuid4()), pid],
        )

    return "perf_u"


def test_manifest_generation_perf_smoke(seeded_app):
    """100 packages × 20 tables — manifest build < 200 ms."""
    from app.api.sync import (
        _build_data_packages_section,
        _build_direct_tables_section,
        _build_memory_domains_section,
    )

    conn = get_system_db()
    uid = _seed_manifest_fixture(conn)
    user = {"id": uid, "email": "perf@x.test", "name": "perf"}

    # Mimic the pieces of `_build_manifest_for_user` that the benchmark
    # cares about. We don't run the full builder because it touches the
    # filesystem (`_get_data_dir`) which is irrelevant to the v49 pieces.
    from src.repositories.table_registry import TableRegistryRepository

    registry_by_name = {t["name"]: t for t in TableRegistryRepository(conn).list_all()}
    states_by_table_id: dict = {}

    # Warm-up.
    _build_data_packages_section(conn, user, registry_by_name, states_by_table_id)

    t0 = time.perf_counter()
    pkgs, packaged_ids, _non_download_names = _build_data_packages_section(
        conn,
        user,
        registry_by_name,
        states_by_table_id,
    )
    _domains = _build_memory_domains_section(conn, user)
    _direct = _build_direct_tables_section(
        conn,
        user,
        registry_by_name,
        states_by_table_id,
        packaged_ids,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    conn.close()

    print(f"\nmanifest build (data_packages + memory_domains + direct_tables): {elapsed_ms:.2f} ms")
    assert len(pkgs) == 100, f"expected 100 packages in manifest, got {len(pkgs)}"
    assert all(len(p["tables"]) == 20 for p in pkgs), "every package should carry 20 tables in the manifest"
    check_perf("manifest build", elapsed_ms, MANIFEST_TARGET_MS)


# ---------------------------------------------------------------------------
# The policy itself
# ---------------------------------------------------------------------------


class TestCheckPerfPolicy:
    """`check_perf` is the whole reason these benchmarks stopped blocking PRs,
    so it gets its own coverage — a silent regression here would quietly turn
    the ceiling off and nobody would notice until a real regression shipped.
    It lives here rather than beside the helper because this is where the
    policy was first needed; every caller relies on these same guarantees."""

    def test_under_target_is_silent(self, recwarn):
        check_perf("x", 10.0, 100.0)
        assert len(recwarn) == 0

    def test_over_target_but_under_ceiling_warns_without_failing(self, recwarn):
        check_perf("manifest build", 150.0, 100.0)
        assert len(recwarn) == 1
        msg = str(recwarn[0].message)
        assert "150.00ms" in msg and "1.5x" in msg, msg
        assert "not failing the build" in msg

    def test_at_the_ceiling_fails(self):
        import pytest

        with pytest.raises(AssertionError, match="regression rather than a slow runner"):
            check_perf("manifest build", 100.0 * PERF_CEILING_FACTOR, 100.0)

    def test_the_failure_names_the_measurement(self):
        import pytest

        with pytest.raises(AssertionError) as exc:
            check_perf("manifest build", 999.0, 100.0)
        assert "999.00ms" in str(exc.value) and "10.0x" in str(exc.value)
