"""`connectors.llm.factory` must resolve the ``ai:`` block from the MERGED
instance configuration, not only from the static ``config/instance.yaml``.

Live finding (2026-09): an instance whose whole configuration lives in the
admin-writable overlay under the data dir has no static file at all. The
factory read only the static loader, got nothing, fell through to the
env-var path (which yields to a set ``ANTHROPIC_API_KEY``) and answered
"no usable Vertex configuration" for a correctly configured
``ai.provider: vertex`` instance — fact extraction could not switch
providers while the chat (a different code path) was happily on Vertex.
"""

from __future__ import annotations

import pytest

from connectors.llm import factory


def test_merged_config_wins_over_a_missing_static_file(monkeypatch):
    def _get_value(*path, default=None):
        assert path == ("ai",)
        return {"provider": "vertex", "vertex": {"project_id": "proj-1", "region": "global"}}

    monkeypatch.setattr("app.instance_config.get_value", _get_value)

    def _static_loader_raises():
        raise FileNotFoundError("config/instance.yaml")

    monkeypatch.setattr("config.loader.load_instance_config", _static_loader_raises)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-static-key-that-must-not-win")

    assert factory._load_ai_config_or_none() == {
        "provider": "vertex",
        "vertex": {"project_id": "proj-1", "region": "global"},
    }
    assert factory.vertex_config_or_none() == ("proj-1", "global")


def test_static_loader_still_serves_when_the_merged_view_has_no_ai_block(monkeypatch):
    monkeypatch.setattr("app.instance_config.get_value", lambda *path, default=None: default)
    monkeypatch.setattr(
        "config.loader.load_instance_config",
        lambda: {"ai": {"provider": "anthropic", "api_key": "x"}},
    )
    assert factory._load_ai_config_or_none() == {"provider": "anthropic", "api_key": "x"}
    assert factory.vertex_config_or_none() is None


@pytest.mark.parametrize("merged", [None, "not-a-dict"])
def test_a_merged_view_without_a_usable_block_falls_through(monkeypatch, merged):
    monkeypatch.setattr("app.instance_config.get_value", lambda *path, default=None: merged)
    monkeypatch.setattr("config.loader.load_instance_config", lambda: {})
    assert factory._load_ai_config_or_none() is None
