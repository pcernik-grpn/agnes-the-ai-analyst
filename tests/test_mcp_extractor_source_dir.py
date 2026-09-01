"""The extracts directory an MCP source writes to must be a safe identifier.

``SyncOrchestrator`` ATTACHes ``extracts/<dir>/extract.duckdb`` under ``<dir>``
as a bare SQL alias, so the directory name — not merely the display name — has
to satisfy the strict identifier rule. ``mcp_sources.name`` is human-facing and
only the admin CRUD path constrains it (``_require_safe_source_name``), so a
source created any other way could name a directory the orchestrator then
refuses to attach: the extract materialized "successfully" and never reached
the catalog.
"""

from __future__ import annotations

import pytest

from connectors.mcp.extractor import (
    adopt_legacy_output_dir,
    output_dir_for_source,
    source_dir_name,
)
from src.identifier_validation import is_safe_identifier

# The name a derived Keboola source actually carries: ``f"Keboola: {conn}"``
# (src/keboola_chat_tools.py::derived_source_name). Colon and spaces, so the
# strict identifier rule rejects it.
DERIVED_NAME = "Keboola: Example Project - EU"
DERIVED_ID = "kbc-chat-00000000-0000-4000-8000-000000000001"


class TestSourceDirName:
    def test_safe_name_is_used_verbatim(self):
        """Every source created through the guarded admin path already has a
        safe name; its directory must not move, or a working instance would
        orphan its existing extracts on upgrade."""
        assert source_dir_name("any-id", "keboola_mcp_gcp_eu") == "keboola_mcp_gcp_eu"

    def test_unsafe_name_yields_a_safe_directory(self):
        """The regression: a name of this shape produced a directory the
        orchestrator refused to ATTACH."""
        assert not is_safe_identifier(DERIVED_NAME)
        assert is_safe_identifier(source_dir_name(DERIVED_ID, DERIVED_NAME))

    def test_unsafe_name_directory_is_deterministic(self):
        """Two runs of the same source must write to the same directory,
        otherwise every materialize leaves a fresh orphan behind."""
        assert source_dir_name(DERIVED_ID, DERIVED_NAME) == source_dir_name(DERIVED_ID, DERIVED_NAME)

    def test_names_that_slugify_alike_get_distinct_directories(self):
        """Distinct sources must never share a directory — they would
        overwrite each other's ``_meta`` and parquets."""
        a = source_dir_name("id-a", "Keboola: Prod - EU")
        b = source_dir_name("id-b", "Keboola: Prod / EU")
        assert a != b

    def test_long_unsafe_name_stays_within_identifier_bounds(self):
        """The strict rule caps an identifier at 64 characters."""
        name = "Keboola: " + "very long project name " * 10
        result = source_dir_name(DERIVED_ID, name)
        assert is_safe_identifier(result)
        assert len(result) <= 64

    def test_name_with_no_usable_characters_still_safe(self):
        """A name that slugifies to nothing must still produce a directory."""
        assert is_safe_identifier(source_dir_name(DERIVED_ID, ":::"))

    def test_name_starting_with_a_digit_is_not_used_verbatim(self):
        """A leading digit fails the strict rule even with no punctuation."""
        result = source_dir_name(DERIVED_ID, "2026report")
        assert result != "2026report"
        assert is_safe_identifier(result)


class TestOutputDirForSource:
    def test_output_dir_uses_the_safe_directory_name(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGNES_DATA_DIR", str(tmp_path))
        out = output_dir_for_source({"id": DERIVED_ID, "name": DERIVED_NAME})
        assert out.parent == tmp_path / "extracts"
        assert is_safe_identifier(out.name)


class TestAdoptLegacyOutputDir:
    """A directory already written under the raw unsafe name carries real
    rows. It was never ATTACHed (that is the bug), so moving it into the safe
    location is non-destructive and stops the orchestrator rejecting it on
    every rebuild forever."""

    def test_legacy_directory_is_moved_to_the_safe_location(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGNES_DATA_DIR", str(tmp_path))
        legacy = tmp_path / "extracts" / DERIVED_NAME
        (legacy / "data").mkdir(parents=True)
        (legacy / "extract.duckdb").write_bytes(b"payload")

        target = output_dir_for_source({"id": DERIVED_ID, "name": DERIVED_NAME})
        assert adopt_legacy_output_dir(DERIVED_NAME, target) is True

        assert not legacy.exists()
        assert (target / "extract.duckdb").read_bytes() == b"payload"

    def test_existing_target_is_never_clobbered(self, tmp_path, monkeypatch):
        """If both exist the new one is authoritative — overwriting it with
        the stale legacy extract would serve old rows as current."""
        monkeypatch.setenv("AGNES_DATA_DIR", str(tmp_path))
        legacy = tmp_path / "extracts" / DERIVED_NAME
        legacy.mkdir(parents=True)
        (legacy / "extract.duckdb").write_bytes(b"stale")

        target = output_dir_for_source({"id": DERIVED_ID, "name": DERIVED_NAME})
        target.mkdir(parents=True)
        (target / "extract.duckdb").write_bytes(b"current")

        assert adopt_legacy_output_dir(DERIVED_NAME, target) is False
        assert (target / "extract.duckdb").read_bytes() == b"current"
        assert legacy.exists()

    def test_safe_named_source_has_nothing_to_adopt(self, tmp_path, monkeypatch):
        """Its directory already IS the raw name — moving it onto itself must
        not be attempted."""
        monkeypatch.setenv("AGNES_DATA_DIR", str(tmp_path))
        source = {"id": "id-a", "name": "keboola_mcp_gcp_eu"}
        target = output_dir_for_source(source)
        target.mkdir(parents=True)
        (target / "extract.duckdb").write_bytes(b"payload")

        assert adopt_legacy_output_dir(source["name"], target) is False
        assert (target / "extract.duckdb").read_bytes() == b"payload"

    def test_no_legacy_directory_is_a_no_op(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGNES_DATA_DIR", str(tmp_path))
        target = output_dir_for_source({"id": DERIVED_ID, "name": DERIVED_NAME})
        assert adopt_legacy_output_dir(DERIVED_NAME, target) is False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
