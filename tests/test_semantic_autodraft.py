"""Unit tests for ``src/semantic_autodraft.py`` (semantic-phase5, wave 2):
the headless auto-draft trigger prompt, and the ``table_registry`` dedup-flag
clearing shared by the ``authoring_suggestions`` approve and reject paths.
"""

from __future__ import annotations

import pytest

from src.semantic_autodraft import build_trigger_prompt, clear_pending_for_document


class TestBuildTriggerPrompt:
    def test_includes_table_identity(self):
        prompt = build_trigger_prompt({"id": "orders", "name": "Orders", "source_type": "keboola"})
        assert "orders" in prompt
        assert "Orders" in prompt
        assert "keboola" in prompt

    def test_includes_survey_commands(self):
        prompt = build_trigger_prompt({"id": "orders", "name": "Orders", "source_type": "local"})
        assert "agnes schema orders" in prompt
        assert "agnes describe orders" in prompt

    def test_overrides_the_wait_for_go_ahead_instruction(self):
        """Regression guard (Task 3's whole reason for existing): the
        semantic-model-builder persona instructs the agent to "get an
        explicit go-ahead before applying" a document. Literally followed in
        a headless session with no human present, the agent drafts, asks a
        question, and never calls apply_semantic_model — silently producing
        zero suggestions with no error. The trigger prompt MUST explicitly
        override that instruction for this context, or a future edit to
        this prompt could silently reintroduce the bug.
        """
        prompt = build_trigger_prompt({"id": "orders", "name": "Orders", "source_type": "local"})
        lowered = prompt.lower()
        # Rule 1: don't end the turn on a question / wait for a go-ahead.
        assert "do not end your turn" in lowered or "not end your turn" in lowered
        assert "question" in lowered
        assert "no human" in lowered or "there is no human" in lowered
        # It must actually instruct the direct apply call.
        assert "apply_semantic_model" in prompt
        # ...and explain this is not a review bypass (the moderation queue
        # still applies to a non-admin caller).
        assert "moderation queue" in lowered or "review" in lowered

    def test_permits_a_minimal_needs_review_model_when_uncertain(self):
        """Rule 2: when the data doesn't support a confident draft, the
        agent must be told to propose a minimal, flagged-for-review model
        rather than invent meaning it cannot verify."""
        prompt = build_trigger_prompt({"id": "orders", "name": "Orders", "source_type": "local"})
        lowered = prompt.lower()
        assert "minimal" in lowered
        assert "needs review" in lowered or "flagged" in lowered or "note that it needs review" in lowered
        assert "guess" in lowered or "invent" in lowered

    def test_handles_missing_optional_fields(self):
        """No name/source_type present — must not raise, and the id still
        appears as the fallback display name."""
        prompt = build_trigger_prompt({"id": "bare_table"})
        assert "bare_table" in prompt


class TestClearPendingForDocument:
    @pytest.fixture
    def system_db(self, e2e_env):
        return e2e_env

    def _register(self, id_, name=None):
        from src.repositories import table_registry_repo

        table_registry_repo().register(id=id_, name=name or id_, source_type="local", query_mode="local")

    def test_clears_the_flag_for_every_resolved_table(self, system_db):
        from src.repositories import table_registry_repo

        self._register("orders")
        self._register("customers")
        table_registry_repo().mark_semantic_draft_pending("orders")
        table_registry_repo().mark_semantic_draft_pending("customers")

        document = {
            "semantic_model": [
                {
                    "name": "retail",
                    "datasets": [
                        {"name": "orders", "source": "orders", "fields": []},
                        {"name": "customers", "source": "customers", "fields": []},
                    ],
                }
            ]
        }
        clear_pending_for_document(document)

        assert table_registry_repo().get("orders")["semantic_draft_pending_at"] is None
        assert table_registry_repo().get("customers")["semantic_draft_pending_at"] is None

    def test_unresolvable_dataset_is_a_noop_not_an_error(self, system_db):
        # No table_registry row matches "ghost" — must not raise.
        clear_pending_for_document(
            {"semantic_model": [{"name": "m", "datasets": [{"name": "ghost", "source": "ghost"}]}]}
        )

    def test_leaves_other_tables_pending_flag_untouched(self, system_db):
        from src.repositories import table_registry_repo

        self._register("orders")
        self._register("untouched")
        table_registry_repo().mark_semantic_draft_pending("orders")
        table_registry_repo().mark_semantic_draft_pending("untouched")

        clear_pending_for_document(
            {"semantic_model": [{"name": "m", "datasets": [{"name": "orders", "source": "orders"}]}]}
        )

        assert table_registry_repo().get("orders")["semantic_draft_pending_at"] is None
        assert table_registry_repo().get("untouched")["semantic_draft_pending_at"] is not None
