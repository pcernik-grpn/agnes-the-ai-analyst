"""QUICKSTART's primary path is the Postgres compose chain (A1)."""

from pathlib import Path


def test_quickstart_primary_path_is_pg():
    text = Path("docs/QUICKSTART.md").read_text()
    assert "docker-compose.postgres.yml" in text


def test_env_template_requires_postgres_password():
    text = Path("config/.env.template").read_text()
    # An uncommented assignment line, not just prose mention.
    assert any(line.strip().startswith("POSTGRES_PASSWORD=") for line in text.splitlines())
