"""The builder's contract for composing a plugin from Library items.

Source-inspection, like the other builder guards: the page is one template's
inline JS with no test harness of its own, and the properties below are ones a
plausible refactor would quietly drop.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "app" / "web" / "templates" / "skills.html"
STORE = ROOT / "app" / "api" / "store.py"


@pytest.fixture(scope="module")
def skills() -> str:
    return SKILLS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def store() -> str:
    return STORE.read_text(encoding="utf-8")


class TestBothRoutesIntoAPlugin:
    def test_the_plugin_type_offers_the_library_and_the_zip(self, skills):
        """The .zip was the only way in, which left an author who had written
        their skills here unable to bundle them without packaging one by hand
        somewhere else."""
        block = re.search(r"function pluginModeSwitchHtml\(\) \{(.*?)\n  \}", skills, re.S)
        assert block, "pluginModeSwitchHtml not found"
        body = block.group(1)
        assert "data-sk-psrc=" in body
        assert "btn('library'" in body and "btn('upload'" in body

    def test_the_library_route_is_the_default(self, skills):
        """Most authors reaching for a plugin have items in the Library
        already; landing them on a drop zone is what sent them away to
        package a .zip."""
        assert re.search(r"plugin_source:\s*'library'", skills), "makeDraft does not default to the Library route"
        assert re.search(r"draft\.plugin_source\s*===\s*'upload'\s*\?\s*'upload'\s*:\s*'library'", skills), (
            "pluginSource() no longer treats 'library' as the fallback"
        )

    def test_switching_route_keeps_both_sides_intact(self, skills):
        """Trying one route must not throw the other's work away — the whole
        point of a toggle rather than a choice made once."""
        branch = re.search(r"data-sk-psrc'\)\) \{(.*?)\n    \}", skills, re.S)
        assert branch, "the psrc click branch was not found"
        body = branch.group(1)
        assert "bundle = null" not in body, "switching route drops the attached .zip"
        assert "components = []" not in body, "switching route drops the Library selection"


class TestTheComposedRoutePostsTheRightThing:
    def test_check_and_save_both_reach_from_components(self, skills):
        """Two call sites, one endpoint. A Check that hit a different path
        than Save is how the .zip route once shipped a half-check."""
        assert skills.count("'/api/store/entities/from-components'") == 2

    def test_the_composed_payload_carries_ids_and_no_file(self, skills):
        block = re.search(r"function componentsPayload\(extra\) \{(.*?)\n  \}", skills, re.S)
        assert block, "componentsPayload not found"
        body = block.group(1)
        assert "components: componentIds()" in body
        assert "FormData" not in body, "the composed route must not post a file"

    def test_save_is_gated_on_having_picked_something(self, skills):
        block = re.search(r"function canPublish\(\) \{(.*?)\n  \}", skills, re.S)
        assert block, "canPublish not found"
        body = block.group(1)
        assert "componentIds().length" in body, "Save is not gated on the selection"
        assert "!!bundle" in body, "Save no longer gates the .zip route on the file"


class TestAComposedDraftSurvivesAReload:
    def test_the_selection_lives_in_the_draft(self, skills):
        """Ids, not a File — which is exactly why this route can resume whole
        where the .zip route cannot."""
        assert re.search(r"components:\s*\[\]", skills), "makeDraft carries no components"
        block = re.search(r"function isBlank\(\) \{(.*?)\n  \}", skills, re.S)
        assert block and "componentIds().length" in block.group(1), (
            "isBlank ignores the selection, so a draft with items picked and "
            "nothing typed would be discarded on persist"
        )

    def test_the_reattach_prompt_is_scoped_to_the_zip_route(self, skills):
        """Telling the author of a composed draft to re-attach a file they
        never had is the kind of copy that teaches people to ignore toasts."""
        line = re.search(r".*Draft restored — re-attach the \.zip.*", skills)
        assert line
        window = skills[max(0, line.start() - 400) : line.start()]
        assert "pluginSource() === 'upload'" in window, "the re-attach toast is not gated on the .zip route"


class TestTheCapMatchesTheServer:
    def test_client_and_server_agree_on_the_component_cap(self, skills, store):
        client = re.search(r"var MAX_COMPONENTS = (\d+);", skills)
        server = re.search(r"^MAX_COMPONENTS = (\d+)$", store, re.M)
        assert client and server, "one side no longer declares MAX_COMPONENTS"
        assert client.group(1) == server.group(1), "the builder would let an author pick past what the server accepts"
