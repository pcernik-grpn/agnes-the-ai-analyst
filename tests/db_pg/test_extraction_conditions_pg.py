"""Fleet-level provider-refusal conditions (TCRD-296 synthesis F.25) — the
repository, the classifier, and the streamed-enqueue suppression it feeds.

PG-side by necessity, not by preference: ``extraction_conditions`` is a
Postgres-only table (A3 PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend
discipline"), so Postgres is the only backend the repository can run on at
all. The DuckDB side's contract — every helper fails clean (``[]``/``None``/
a silent no-op, never a crash) — is covered in
``tests/test_facts_provider_limit.py`` (backend-agnostic).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repo(pg_engine):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    from src.repositories.extraction_conditions_pg import ExtractionConditionsPgRepository

    return ExtractionConditionsPgRepository(pg_engine)


class TestTheRepository:
    def test_record_creates_a_new_active_condition(self, repo):
        row = repo.record(
            reason="workspace_limit",
            provider="anthropic",
            model="claude-haiku-4-5-20251001",
            region=None,
            message="Your workspace has hit the API usage limits ... regain access on 2026-10-01",
            retry_after_s=None,
        )
        assert row["reason"] == "workspace_limit"
        assert row["provider"] == "anthropic"
        assert row["model"] == "claude-haiku-4-5-20251001"
        # None normalizes to "" — the workspace-wide reason has no region.
        assert row["region"] == ""
        assert row["cleared_at"] is None
        assert row["first_seen"] is not None
        assert row["last_seen"] is not None

    def test_recording_the_same_condition_twice_refreshes_instead_of_duplicating(self, repo):
        first = repo.record(
            reason="quota_exceeded",
            provider="vertex",
            model="claude-sonnet-4-6",
            region="us-east5",
            message="Quota exceeded for quota metric X",
            retry_after_s=None,
        )
        second = repo.record(
            reason="quota_exceeded",
            provider="vertex",
            model="claude-sonnet-4-6",
            region="us-east5",
            message="Quota exceeded for quota metric X (again)",
            retry_after_s=60,
        )
        assert second["id"] == first["id"]
        active = repo.list_active()
        assert len(active) == 1
        assert active[0]["message"] == "Quota exceeded for quota metric X (again)"
        assert active[0]["retry_after_s"] == 60
        # `first_seen` is untouched by the refresh — it is when the FLEET
        # first saw this condition, not when it was last confirmed.
        assert active[0]["first_seen"] == first["first_seen"]

    def test_different_region_or_model_are_independent_conditions(self, repo):
        repo.record(
            reason="quota_exceeded",
            provider="vertex",
            model="claude-haiku-4-5",
            region="us-east5",
            message="m1",
            retry_after_s=None,
        )
        repo.record(
            reason="quota_exceeded",
            provider="vertex",
            model="claude-haiku-4-5",
            region="europe-west1",
            message="m2",
            retry_after_s=None,
        )
        assert len(repo.list_active()) == 2

    def test_list_active_excludes_a_cleared_condition(self, repo):
        row = repo.record(
            reason="billing_disabled",
            provider="anthropic",
            model="claude-haiku-4-5",
            region=None,
            message="billing is disabled",
            retry_after_s=None,
        )
        assert repo.list_active() != []
        cleared = repo.clear_for_provider("anthropic")
        assert cleared == 1
        assert repo.list_active() == []
        # The row itself still exists (a record, not a disappearance).
        stored = repo.get(row["id"])
        assert stored is not None
        assert stored["cleared_at"] is not None

    def test_clear_for_provider_only_touches_that_provider(self, repo):
        repo.record(
            reason="workspace_limit",
            provider="anthropic",
            model="claude-haiku-4-5",
            region=None,
            message="m",
            retry_after_s=None,
        )
        repo.record(
            reason="quota_exceeded",
            provider="vertex",
            model="claude-sonnet-4-6",
            region="us-east5",
            message="m",
            retry_after_s=None,
        )
        repo.clear_for_provider("anthropic")
        remaining = repo.list_active()
        assert len(remaining) == 1
        assert remaining[0]["provider"] == "vertex"

    def test_clear_for_provider_with_nothing_active_is_a_noop(self, repo):
        assert repo.clear_for_provider("anthropic") == 0

    def test_get_of_an_unknown_id_is_none(self, repo):
        assert repo.get("ecnd_nope") is None

    def test_clear_by_id(self, repo):
        row = repo.record(
            reason="workspace_limit",
            provider="anthropic",
            model="claude-haiku-4-5",
            region=None,
            message="m",
            retry_after_s=None,
        )
        assert repo.clear(row["id"]) is True
        assert repo.list_active() == []
        # Already cleared — clearing again matches nothing.
        assert repo.clear(row["id"]) is False


class TestClassificationAndConditionFlowThroughFactsExtraction:
    """The connector-level helpers — ``classify_provider_limit_error``,
    ``record_provider_limit_condition``/``clear_provider_limit_conditions``,
    and the cooldown gate — driven against a REAL Postgres backend so the
    repository wiring (not just the classifier logic) is exercised.
    """

    @pytest.fixture(autouse=True)
    def _pg_backend(self, monkeypatch, repo, pg_engine):
        # `repo` above already ran the migration; point the app's own
        # factory at the SAME engine so `connectors.sharepoint.
        # facts_extraction`'s lazy `from src.repositories import
        # extraction_conditions_repo` resolves to Postgres too — the same
        # dance `state_backend` (tests/db_pg/conftest.py) does: set the
        # env var, force a fresh `src.db_pg` engine, reload the factory
        # module so it re-reads the env.
        import importlib

        import src.db_pg as db_pg
        import src.repositories

        monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
        db_pg.dispose()
        importlib.reload(src.repositories)

    def test_classify_provider_limit_error_shapes(self):
        from connectors.sharepoint.facts_extraction import classify_provider_limit_error

        assert (
            classify_provider_limit_error(
                Exception("Your workspace has hit the API usage limits for on-demand daily spend")
            )
            == "workspace_limit"
        )
        assert (
            classify_provider_limit_error(
                Exception("Quota exceeded for quota metric X and limit Y for consumer project")
            )
            == "quota_exceeded"
        )
        assert classify_provider_limit_error(Exception("billing is disabled for this project")) == "billing_disabled"
        # An ordinary transient rate limit must NOT classify — that is what
        # `_is_retryable`'s AIMD/backoff already handles.
        assert classify_provider_limit_error(Exception("rate limit exceeded, please retry")) is None
        assert classify_provider_limit_error(Exception("invalid_request_error: prompt is too long")) is None

    def test_record_then_streamed_pass_is_suppressed_until_cooldown(self):
        from connectors.sharepoint.facts_extraction import (
            record_provider_limit_condition,
            streamed_pass_suppressed_by_provider_limit,
        )

        assert streamed_pass_suppressed_by_provider_limit() is None

        record_provider_limit_condition(
            reason="workspace_limit",
            provider="anthropic",
            model="claude-haiku-4-5",
            region=None,
            message="workspace usage limit hit",
            retry_after_s=None,
        )
        condition = streamed_pass_suppressed_by_provider_limit()
        assert condition is not None
        assert condition["reason"] == "workspace_limit"

    def test_a_provider_given_retry_after_wins_over_the_default_cooldown(self, repo, pg_engine):
        import sqlalchemy as sa

        from connectors.sharepoint.facts_extraction import (
            record_provider_limit_condition,
            streamed_pass_suppressed_by_provider_limit,
        )

        record_provider_limit_condition(
            reason="quota_exceeded",
            provider="vertex",
            model="claude-sonnet-4-6",
            region="us-east5",
            message="m",
            retry_after_s=5,
        )
        # Simulate the retry_after window having already elapsed — a
        # provider-given retry_after of 5s wins over the 30-minute default,
        # so this condition should no longer suppress a streamed pass.
        row = repo.list_active()[0]
        with pg_engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE extraction_conditions SET last_seen = :ts WHERE id = :id"),
                {"ts": datetime.now(timezone.utc) - timedelta(seconds=10), "id": row["id"]},
            )
        assert streamed_pass_suppressed_by_provider_limit() is None

    def test_clear_provider_limit_conditions_clears_the_active_one(self):
        from connectors.sharepoint.facts_extraction import (
            clear_provider_limit_conditions,
            record_provider_limit_condition,
            streamed_pass_suppressed_by_provider_limit,
        )

        record_provider_limit_condition(
            reason="workspace_limit",
            provider="anthropic",
            model="claude-haiku-4-5",
            region=None,
            message="m",
            retry_after_s=None,
        )
        assert streamed_pass_suppressed_by_provider_limit() is not None

        clear_provider_limit_conditions("anthropic")
        assert streamed_pass_suppressed_by_provider_limit() is None


class TestTheVertexRegionModelMatrix:
    def test_haiku_supports_all_three_documented_regions(self):
        from connectors.sharepoint.facts_extraction import vertex_region_supports_model

        for region in ("global", "us-east5", "europe-west1"):
            assert vertex_region_supports_model(region, "claude-haiku-4-5-20251001") is True

    def test_sonnet_is_global_only(self):
        from connectors.sharepoint.facts_extraction import vertex_region_supports_model

        assert vertex_region_supports_model("global", "claude-sonnet-4-6") is True
        assert vertex_region_supports_model("us-east5", "claude-sonnet-4-6") is False
        assert vertex_region_supports_model("europe-west1", "claude-sonnet-4-6") is False

    def test_an_unlisted_tier_is_unconstrained(self):
        from connectors.sharepoint.facts_extraction import vertex_region_supports_model

        assert vertex_region_supports_model("us-east5", "claude-opus-4-7") is True
