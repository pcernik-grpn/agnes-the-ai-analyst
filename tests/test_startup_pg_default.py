"""A fresh instance seeds Postgres side-car app-state, not DuckDB (A1)."""

from pathlib import Path

TPL = Path("infra/modules/customer-instance/startup-script.sh.tpl").read_text()


def test_first_boot_seed_is_side_car():
    # The first-boot instance.yaml seed block must default new instances
    # to the Postgres side-car backend.
    assert "backend: side_car" in TPL


def test_duckdb_is_not_the_seeded_default():
    # The literal seed line for the backend must no longer say duckdb.
    # (DuckDB remains a valid *persisted* state for existing instances;
    # only the fresh-install seed changes.)
    assert "backend: duckdb" not in TPL


def test_state_machine_has_no_separate_fresh_install_default():
    # Task 2 investigation (A1): src/db_state_machine.py has no
    # current_state(default=...)/initial_state()-shaped API — the only
    # notion of "fresh-install default" is this seeded instance.yaml file
    # (this module, above). read_backend_state()'s own DUCKDB fallback for
    # a *missing* overlay is a separate, safe zero-config fallback (no
    # persisted state at all, e.g. local dev without instance.yaml) — it is
    # deliberately left unchanged: use_pg() (src/repositories/__init__.py,
    # a reserved file) falls through to it and env-var detection when the
    # overlay is absent, and flipping it to SIDE_CAR would make use_pg()
    # assume Postgres is reachable with zero configuration, which is not a
    # safe default. See src/db_state_machine.py's module docstring and
    # read_backend_state() docstring for the corresponding prose update.
    pass
