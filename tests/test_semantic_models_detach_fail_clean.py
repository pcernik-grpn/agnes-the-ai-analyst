"""F3 detach/override: the DuckDB side of ``SemanticModelsRepository`` has no
backing columns for any of these methods (PG-only, A3 ratchet — see
migrations/versions/0077_semantic_models_detach.py). Every one of them must
raise the typed ``RequiresPostgresBackend`` rather than crash or silently
no-op."""

from __future__ import annotations

import duckdb
import pytest

from src.repositories import RequiresPostgresBackend
from src.repositories.semantic_models import SemanticModelsRepository


@pytest.fixture
def repo():
    conn = duckdb.connect(":memory:")
    return SemanticModelsRepository(conn)


@pytest.mark.parametrize(
    "call",
    [
        lambda r: r.detach("x", by="a@example.com", base_hash="h"),
        lambda r: r.reattach("x"),
        lambda r: r.update_source_content_hash("x", "h"),
        lambda r: r.mark_source_missing("x"),
        lambda r: r.clear_source_missing("x"),
        lambda r: r.list_detached_with_health_state(),
    ],
    ids=[
        "detach",
        "reattach",
        "update_source_content_hash",
        "mark_source_missing",
        "clear_source_missing",
        "list_detached_with_health_state",
    ],
)
def test_raises_requires_postgres_backend(repo, call):
    with pytest.raises(RequiresPostgresBackend) as exc_info:
        call(repo)
    assert exc_info.value.feature == "semantic_model_detach"
