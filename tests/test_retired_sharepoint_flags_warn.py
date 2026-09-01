"""A retired SharePoint switch still set must produce a startup warning.

`sharepoint.enabled` replaced four per-feature flags and nothing reads the old
keys any more. That is exactly the hazard: an instance running extraction, the
Graph receiver or ACL mirroring today has one of the four set and NOT the new
one, so the upgrade turns the whole connector off — extraction stops, the
receiver 404s, ACL mirroring stops — with nothing in the log naming the cause.

The warning is the whole remedy. These tests pin that it fires, that it says
what to do, that it does not fire when the operator has already migrated, and
— the one worth stating — that it never INFERS the new switch from an old one.
"""

from __future__ import annotations

import importlib

import pytest

RETIRED = [
    "AGNES_EXTRACTION_ENABLED",
    "AGNES_EXTRACTION_WEBHOOK_ENABLED",
    "AGNES_ACL_MIRRORING_ENABLED",
    "AGNES_ACL_ZONES_ENABLED",
]


@pytest.fixture
def cfg(monkeypatch):
    """A fresh module so `_warned_once_keys` starts empty each test."""
    import app.instance_config as m

    m = importlib.reload(m)
    for var in RETIRED + ["AGNES_SHAREPOINT_ENABLED"]:
        monkeypatch.delenv(var, raising=False)
    # No instance.yaml in the test env, so every get_value() falls through to
    # its default — the env vars are the whole input surface here.
    return m


@pytest.mark.parametrize("env_var", RETIRED)
def test_each_retired_flag_warns(env_var, cfg, monkeypatch, caplog):
    monkeypatch.setenv(env_var, "1")
    with caplog.at_level("WARNING"):
        warned = cfg.warn_retired_sharepoint_flags()
    assert warned == [env_var]
    assert any(env_var in r.message for r in caplog.records)


def test_the_warning_names_the_action_when_the_new_switch_is_absent(cfg, monkeypatch, caplog):
    """The operator's next step is the point of the line — an upgrade that
    silently disables a data-ingesting connector must say what turns it back on."""
    monkeypatch.setenv("AGNES_EXTRACTION_ENABLED", "1")
    with caplog.at_level("WARNING"):
        cfg.warn_retired_sharepoint_flags()
    text = " ".join(r.message for r in caplog.records)
    assert "sharepoint.enabled" in text
    assert "AGNES_SHAREPOINT_ENABLED" in text


def test_no_warning_once_nothing_retired_is_set(cfg, caplog):
    with caplog.at_level("WARNING"):
        assert cfg.warn_retired_sharepoint_flags() == []
    assert not [r for r in caplog.records if "retired" in r.message]


def test_it_never_infers_the_new_switch_from_a_retired_one(cfg, monkeypatch):
    """Warn, never act. Turning a connector that ingests documents and mirrors
    ACLs back on is the operator's call; inferring it from a retired key would
    be the same silent behaviour pointing the other way."""
    monkeypatch.setenv("AGNES_EXTRACTION_ENABLED", "1")
    cfg.warn_retired_sharepoint_flags()
    assert (
        cfg.feature_enabled("sharepoint", "enabled", env_var="AGNES_SHAREPOINT_ENABLED", default=False)
        is False
    )


def test_a_migrated_instance_still_hears_about_the_stale_key(cfg, monkeypatch, caplog):
    """Both set: the connector works, but the dead key is still noise in the
    config and the line says which one is actually deciding."""
    monkeypatch.setenv("AGNES_EXTRACTION_ENABLED", "1")
    monkeypatch.setenv("AGNES_SHAREPOINT_ENABLED", "1")
    with caplog.at_level("WARNING"):
        warned = cfg.warn_retired_sharepoint_flags()
    assert warned == ["AGNES_EXTRACTION_ENABLED"]
    assert any("comes from there" in r.message for r in caplog.records)


def test_warning_is_once_per_key_per_process(cfg, monkeypatch, caplog):
    monkeypatch.setenv("AGNES_ACL_MIRRORING_ENABLED", "1")
    with caplog.at_level("WARNING"):
        cfg.warn_retired_sharepoint_flags()
        cfg.warn_retired_sharepoint_flags()
    assert sum("AGNES_ACL_MIRRORING_ENABLED" in r.message for r in caplog.records) == 1
