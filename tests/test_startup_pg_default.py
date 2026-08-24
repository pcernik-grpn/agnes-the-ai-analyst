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
