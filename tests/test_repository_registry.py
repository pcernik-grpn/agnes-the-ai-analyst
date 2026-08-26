"""Contract for the repository factory's declarative backend registry.

The factory (`src/repositories/__init__.py`) dispatches every `<name>_repo()`
through a single `_REGISTRY` table: ``key -> {backend: (module, class)}``.
These tests lock the table's integrity so the dispatch stays correct as repos
and backends are added:

  - every public `<name>_repo` factory has a registry entry (and vice versa);
  - every entry registers the SAME set of backends (no repo that exists on one
    backend but silently not another) — the structural half of the dual-backend
    discipline, complementing `tests/db_pg/test_repo_method_parity.py` (method
    parity) and the `*_contract.py` suites (behavioural parity);
  - the registry's backends match the connection-arg providers;
  - every registered `(module, class)` is importable and is a class.

Pure import-level checks — no database required, so this runs on any box.
"""

from __future__ import annotations

import importlib
import inspect

import pytest

import src.repositories as factory


def _public_repo_funcs() -> set[str]:
    return {n for n in factory.__all__ if n.endswith("_repo")}


def test_every_public_factory_has_a_registry_entry():
    missing = []
    for fname in sorted(_public_repo_funcs()):
        key = fname[: -len("_repo")]
        fn = getattr(factory, fname, None)
        assert callable(fn), f"{fname} in __all__ but not a callable on the module"
        if key not in factory._REGISTRY:
            missing.append(f"{fname} -> expected registry key '{key}'")
    assert not missing, "public factory functions with no _REGISTRY entry:\n  " + "\n  ".join(missing)


def test_no_orphan_registry_entries():
    keys_from_funcs = {f[: -len("_repo")] for f in _public_repo_funcs()}
    orphans = sorted(set(factory._REGISTRY) - keys_from_funcs)
    assert not orphans, (
        "_REGISTRY keys with no matching public <key>_repo factory function "
        f"(dead entries — remove or add the function): {orphans}"
    )


def test_registry_backends_are_symmetric():
    """Every repo must register EITHER every backend (a frozen pre-A3 pair)
    OR Postgres-only (a post-A3 PG-first repo) — never DuckDB alone.

    A3 PG-first ratchet (see CLAUDE.md -> "Dual-backend discipline"): the
    DuckDB app-state backend is frozen, so a brand-new repo registers with
    only the ``PG`` backend (no DuckDB fallback exists by design — resolving
    it on a DuckDB-backed instance raises ``RequiresPostgresBackend``
    instead). A repo present on DuckDB with no Postgres backend at all is
    never a legal shape, before or after the ratchet — that would be a
    permanently orphaned DuckDB-only surface.

    ``tests/test_repository_registry_pg_first_ratchet.py`` is the
    complementary ratchet: it pins the exact set of keys still allowed to
    carry a DuckDB backend, so a brand-new *full pair* (not just a new
    DuckDB-only entry) is also caught.
    """
    full = set(factory._ARG_PROVIDERS)  # {"duckdb", "pg"} — the frozen shape
    pg_only = {factory.PG}  # the sanctioned post-A3 shape
    bad_shape = {
        key: sorted(backends) for key, backends in factory._REGISTRY.items() if set(backends) not in (full, pg_only)
    }
    assert not bad_shape, (
        "every repo must register either every backend (frozen pre-A3 pair) "
        "or Postgres-only (post-A3 PG-first repo); no repo may register a "
        "DuckDB backend without a Postgres one:\n  "
        + "\n  ".join(f"{k}: has {v}" for k, v in sorted(bad_shape.items()))
    )


def test_registry_backends_match_arg_providers():
    all_backends = {b for entry in factory._REGISTRY.values() for b in entry}
    assert all_backends == set(factory._ARG_PROVIDERS), (
        "backends used in _REGISTRY must exactly match _ARG_PROVIDERS keys; "
        f"registry={sorted(all_backends)} providers={sorted(factory._ARG_PROVIDERS)}"
    )


@pytest.mark.parametrize(
    "key,backend,module_path,class_name",
    [
        (key, backend, mod, cls)
        for key, entry in sorted(factory._REGISTRY.items())
        for backend, (mod, cls) in entry.items()
    ],
)
def test_every_registered_class_is_importable(key, backend, module_path, class_name):
    module = importlib.import_module(module_path)
    obj = getattr(module, class_name, None)
    assert obj is not None, f"{key}[{backend}] -> {module_path}.{class_name} does not exist"
    assert inspect.isclass(obj), f"{module_path}.{class_name} is not a class"
