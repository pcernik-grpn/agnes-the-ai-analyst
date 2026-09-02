"""``extraction.*`` — admin-editable built-in crawl config.

2026-09-01, two changes met in this section:

- The connector's own on/off state moved off it entirely — it now lives on
  the single `sharepoint` switch (`app/switches.py`), which also gates the
  connect wizard, admin routes, and ACL mirroring. See
  `tests/test_switches.py::TestSharePointSwitch` for the switch itself.
- The external-producer mode was removed outright (owner decision
  2026-08-31: the built-in in-process pipeline is the ONLY pipeline), and
  `producer.command`/`.module`/`.env_passthrough` — and the
  `AGNES_EXTRACTION_PRODUCER_*` env locks that pinned them — went with it.
  `app/api/admin.py::_EXTRACTION_ENV_LOCKS` is deliberately empty now.

What remains admin-editable here: `schedule` (the instance-wide crawl
cadence), `timeout_s` (the per-run ceiling the crawl enforces itself) and,
since the extraction pilot, `crawler.concurrency` (files in flight per run —
the extraction worker's memory lever; see the bottom of this file).
Precedence is unchanged: env (Terraform-rendered) > admin server-config
overlay > static instance.yaml.

Behaviour contract:
  - GET exposes `extraction` in `editable_sections`/`sections`/`known_fields`
    with `schedule`, `timeout_s`, the `crawler` object and the `facts`
    object — no `enabled` leaf, no `producer` object.
  - POST with valid values persists; GET reflects them; the save is `live`
    (never `restart_required`).
  - Field-level validation: `schedule` must parse via
    `src.scheduler.is_valid_schedule`; `timeout_s` must be within
    [60, 86400].
  - No extraction leaf is env-locked anymore — `known_fields` must not mark
    any of them `env_locked` even with stale producer env vars set.
  - A web-set `sharepoint.enabled` is picked up live by
    `_extraction_readiness()` and the manual trigger endpoint, no restart.

Fixture `seeded_app` is auto-discovered from `tests/conftest.py` — DO NOT
import. `e2e_env` (pulled in by `seeded_app`) already sets DATA_DIR to a
fresh `tmp_path` with `state/` created, and an autouse fixture resets
`app.instance_config._instance_config` before every test — no manual
DATA_DIR/cache plumbing needed here.
"""

from __future__ import annotations

import pytest

import yaml


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _clear_extraction_env(monkeypatch):
    monkeypatch.delenv("AGNES_SHAREPOINT_ENABLED", raising=False)


def _client(seeded_app, monkeypatch):
    """(client, admin token) with the extraction env cleared — the shape the
    run-knob tests below share."""
    _clear_extraction_env(monkeypatch)
    return seeded_app["client"], seeded_app["admin_token"]


# ---------------------------------------------------------------------------
# GET — default state
# ---------------------------------------------------------------------------


def test_get_returns_extraction_in_editable_sections(seeded_app, monkeypatch):
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    resp = client.get("/api/admin/server-config", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    assert "extraction" in resp.json()["editable_sections"]


def test_get_returns_extraction_section_key(seeded_app, monkeypatch):
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    resp = client.get("/api/admin/server-config", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    assert "extraction" in resp.json()["sections"]


def test_get_returns_extraction_known_fields(seeded_app, monkeypatch):
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    resp = client.get("/api/admin/server-config", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    fields = resp.json()["known_fields"]["extraction"]
    assert "enabled" not in fields, "enabled moved to the sharepoint switch (2026-09-01 flag consolidation)"
    assert "producer" not in fields, "the external-producer mode was removed — builtin is the only pipeline"
    assert fields["schedule"]["kind"] == "string"
    assert fields["timeout_s"]["kind"] == "int"
    assert fields["timeout_s"]["default"] == 3600


def test_sharepoint_switch_is_editable(seeded_app, monkeypatch):
    """The connector's on/off state — `sharepoint.enabled` — is visible in
    the feature_flags inventory, not `extraction` (which carries no switch
    of its own anymore)."""
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    resp = client.get("/api/admin/server-config", headers=_auth(seeded_app["admin_token"]))
    flags = {f["name"]: f for f in resp.json()["feature_flags"]}
    assert "extraction" not in flags
    assert flags["sharepoint"]["editable"] is True
    assert flags["sharepoint"]["lock_reason"] == ""


# ---------------------------------------------------------------------------
# POST — update and read back
# ---------------------------------------------------------------------------


def test_post_updates_extraction_and_get_reflects_it(seeded_app, monkeypatch):
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    headers = _auth(seeded_app["admin_token"])
    resp = client.post(
        "/api/admin/server-config",
        json={
            "sections": {
                "extraction": {
                    "schedule": "every 30m",
                    "timeout_s": 1800,
                }
            }
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    from app.secrets import _state_dir

    loaded = yaml.safe_load((_state_dir() / "instance.yaml").read_text())
    assert loaded["extraction"]["schedule"] == "every 30m"
    assert loaded["extraction"]["timeout_s"] == 1800

    resp2 = client.get("/api/admin/server-config", headers=headers)
    section = resp2.json()["sections"]["extraction"]
    assert section["schedule"] == "every 30m"
    assert section["timeout_s"] == 1800


def test_post_extraction_effect_is_live_not_restart(seeded_app, monkeypatch):
    """Every extraction leaf is read fresh per call — a save must not force
    `restart_required`."""
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    resp = client.post(
        "/api/admin/server-config",
        json={"sections": {"extraction": {"schedule": "every 30m"}}},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["sections_effect"]["extraction"] == "live"
    assert resp.json()["restart_required"] is False


# ---------------------------------------------------------------------------
# POST — validation
# ---------------------------------------------------------------------------


def test_schedule_rejects_invalid_value(seeded_app, monkeypatch):
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    resp = client.post(
        "/api/admin/server-config",
        json={"sections": {"extraction": {"schedule": "whenever"}}},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 422, resp.text


def test_schedule_accepts_empty_string_as_off(seeded_app, monkeypatch):
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    resp = client.post(
        "/api/admin/server-config",
        json={"sections": {"extraction": {"schedule": ""}}},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 200, resp.text


def test_schedule_accepts_cron_form(seeded_app, monkeypatch):
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    resp = client.post(
        "/api/admin/server-config",
        json={"sections": {"extraction": {"schedule": "cron 0 5 7 * *"}}},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 200, resp.text


def test_timeout_s_below_min_rejected(seeded_app, monkeypatch):
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    resp = client.post(
        "/api/admin/server-config",
        json={"sections": {"extraction": {"timeout_s": 59}}},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 422, resp.text


def test_timeout_s_above_max_rejected(seeded_app, monkeypatch):
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    resp = client.post(
        "/api/admin/server-config",
        json={"sections": {"extraction": {"timeout_s": 86401}}},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 422, resp.text


def test_timeout_s_boundaries_accepted(seeded_app, monkeypatch):
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    for value in (60, 86400):
        resp = client.post(
            "/api/admin/server-config",
            json={"sections": {"extraction": {"timeout_s": value}}},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Env-lock honesty — there are none left
# ---------------------------------------------------------------------------


def test_no_extraction_field_is_env_locked(seeded_app, monkeypatch):
    """The `AGNES_EXTRACTION_PRODUCER_*` locks died with the producer mode.
    Even with the stale env vars still exported (a not-yet-cleaned deploy),
    no extraction leaf may report itself locked — the vars pin nothing."""
    _clear_extraction_env(monkeypatch)
    monkeypatch.setenv("AGNES_EXTRACTION_PRODUCER_COMMAND", "python /opt/producer/agnes_lane.py")
    monkeypatch.setenv("AGNES_EXTRACTION_PRODUCER_MODULE", "your_producer.run")
    client = seeded_app["client"]
    resp = client.get("/api/admin/server-config", headers=_auth(seeded_app["admin_token"]))
    fields = resp.json()["known_fields"]["extraction"]
    for name, spec in fields.items():
        assert not spec.get("env_locked"), f"extraction.{name} claims an env lock that no longer exists"


def test_env_locks_registry_is_empty():
    """Companion static pin: the lock table itself must be empty, so a
    future lock is a deliberate addition here, not a leftover."""
    from app.api.admin import _EXTRACTION_ENV_LOCKS

    assert _EXTRACTION_ENV_LOCKS == ()


# ---------------------------------------------------------------------------
# Audit — the new value lands in the generic diff
# ---------------------------------------------------------------------------


def test_audit_log_carries_the_new_schedule_value(seeded_app, monkeypatch):
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    resp = client.post(
        "/api/admin/server-config",
        json={"sections": {"extraction": {"schedule": "every 45m"}}},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 200, resp.text

    import json

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="instance_config.update", limit=5)
    assert rows, "expected an instance_config.update row"
    row = rows[0]
    params = row.get("params")
    params = json.loads(params) if isinstance(params, str) else (params or {})
    diff_entries = params["diff"]
    matching = [d for d in diff_entries if d["path"] == "extraction.schedule"]
    assert matching, diff_entries
    assert matching[0]["after"] == "every 45m"


# ---------------------------------------------------------------------------
# Live pickup — no restart — of a web-set sharepoint switch
# ---------------------------------------------------------------------------


def test_extraction_readiness_flips_after_a_web_save_no_restart(seeded_app, monkeypatch):
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    headers = _auth(seeded_app["admin_token"])

    from app.api.admin_sharepoint import _extraction_readiness

    usable, err = _extraction_readiness()
    assert usable is False
    assert err["error"] == "extraction_disabled"

    resp = client.post(
        "/api/admin/server-config",
        json={"sections": {"sharepoint": {"enabled": True}}},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    # Same process, no restart, no monkeypatch of get_value — the overlay
    # write above is the only thing that changed.
    usable, err = _extraction_readiness()
    assert usable is True, err


def test_extraction_trigger_endpoint_picks_up_web_saved_switch(seeded_app, monkeypatch):
    """End-to-end through the real HTTP surface: POST server-config flips
    the sharepoint switch, then the manual trigger endpoint enqueues
    successfully with no restart and no direct get_value monkeypatch."""
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    headers = _auth(seeded_app["admin_token"])

    resp = client.post(
        "/api/admin/server-config",
        json={
            "sections": {
                "sharepoint": {"enabled": True},
                "extraction": {"timeout_s": 60},
            }
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    conn_resp = client.post(
        "/api/admin/source-connections",
        json={
            "name": "extraction-web-config-e2e",
            "source_type": "sharepoint",
            "config": {"tenant_id": "tenant-1", "client_id": "client-1"},
        },
        headers=headers,
    )
    assert conn_resp.status_code == 201, conn_resp.text
    conn_id = conn_resp.json()["id"]

    trigger_resp = client.post(
        f"/api/admin/sharepoint/connections/{conn_id}/extract",
        headers=headers,
    )
    assert trigger_resp.status_code == 202, trigger_resp.text


# ---------------------------------------------------------------------------
# extraction.crawler.concurrency — the worker's memory lever, admin-editable
# ---------------------------------------------------------------------------
#
# `extraction.crawler.concurrency` decides how many files ONE crawl holds in
# flight (download → convert → anonymize → ingest), which is what decides the
# extraction worker's peak memory. It was readable by the crawler from the
# overlay all along, but the panel did not render it, so lowering it on a
# live instance meant editing the file on the data disk by hand.


def test_crawler_concurrency_is_a_known_field_with_the_crawlers_own_default(seeded_app, monkeypatch):
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    resp = client.get("/api/admin/server-config", headers=_auth(seeded_app["admin_token"]))
    fields = resp.json()["known_fields"]["extraction"]
    crawler = fields["crawler"]
    assert crawler["kind"] == "object"
    spec = crawler["fields"]["concurrency"]
    assert spec["kind"] == "int"

    from connectors.sharepoint.crawler import _DEFAULT_CONCURRENCY

    assert spec["default"] == _DEFAULT_CONCURRENCY == 6
    # The hint must say what the knob actually governs: it is the memory
    # lever, not a throughput dial.
    assert "memory" in spec["hint"].lower()


def test_crawler_concurrency_bounds_match_the_crawlers_clamp():
    """The panel refuses what the crawler would silently re-clamp — so the
    two bounds must be the same number, pinned here rather than trusted."""
    from app.api.admin import _CRAWLER_CONCURRENCY_MAX, _CRAWLER_CONCURRENCY_MIN
    from connectors.sharepoint.crawler import _MAX_CONCURRENCY

    assert _CRAWLER_CONCURRENCY_MIN == 1
    assert _CRAWLER_CONCURRENCY_MAX == _MAX_CONCURRENCY == 64


def test_post_crawler_concurrency_persists_and_get_reflects_it(seeded_app, monkeypatch):
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    headers = _auth(seeded_app["admin_token"])
    resp = client.post(
        "/api/admin/server-config",
        json={"sections": {"extraction": {"crawler": {"concurrency": 2}}}},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["sections_effect"]["extraction"] == "live"

    from app.secrets import _state_dir

    loaded = yaml.safe_load((_state_dir() / "instance.yaml").read_text())
    assert loaded["extraction"]["crawler"]["concurrency"] == 2

    resp2 = client.get("/api/admin/server-config", headers=headers)
    assert resp2.json()["sections"]["extraction"]["crawler"]["concurrency"] == 2


def test_post_crawler_concurrency_keeps_sibling_crawler_keys(seeded_app, monkeypatch):
    """A save of the one rendered leaf must not wipe the crawler keys the
    panel does NOT render yet (max_file_mb, item_timeout_s, ...) — the
    overlay is deep-merged, never replaced per object."""
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    headers = _auth(seeded_app["admin_token"])
    first = client.post(
        "/api/admin/server-config",
        json={"sections": {"extraction": {"crawler": {"max_file_mb": 20}}}},
        headers=headers,
    )
    assert first.status_code == 200, first.text
    second = client.post(
        "/api/admin/server-config",
        json={"sections": {"extraction": {"crawler": {"concurrency": 3}}}},
        headers=headers,
    )
    assert second.status_code == 200, second.text

    from app.secrets import _state_dir

    loaded = yaml.safe_load((_state_dir() / "instance.yaml").read_text())
    assert loaded["extraction"]["crawler"] == {"max_file_mb": 20, "concurrency": 3}


def test_crawler_concurrency_out_of_range_is_refused_not_reclamped(seeded_app, monkeypatch):
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    for value in (0, 65, -1):
        resp = client.post(
            "/api/admin/server-config",
            json={"sections": {"extraction": {"crawler": {"concurrency": value}}}},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 422, (value, resp.text)
        assert "extraction.crawler.concurrency" in resp.text


def test_crawler_concurrency_must_be_an_integer(seeded_app, monkeypatch):
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    for value in ("6", 6.5, True):
        resp = client.post(
            "/api/admin/server-config",
            json={"sections": {"extraction": {"crawler": {"concurrency": value}}}},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 422, (value, resp.text)


def test_crawler_concurrency_boundaries_accepted(seeded_app, monkeypatch):
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    for value in (1, 32):
        resp = client.post(
            "/api/admin/server-config",
            json={"sections": {"extraction": {"crawler": {"concurrency": value}}}},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200, (value, resp.text)


def test_crawler_object_that_is_not_a_mapping_is_refused(seeded_app, monkeypatch):
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    resp = client.post(
        "/api/admin/server-config",
        json={"sections": {"extraction": {"crawler": 4}}},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 422, resp.text


def test_saved_crawler_concurrency_is_what_the_next_crawl_run_reads(seeded_app, monkeypatch):
    """End to end through the real save path: what the panel stores is
    exactly what the crawler resolves as its configured cap on the next
    run — no restart, no monkeypatch of get_value. (The cross-process
    half — the extraction worker noticing the app container's save — is
    `tests/test_instance_config_hot_reload.py`.)"""
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]

    from connectors.sharepoint.crawler import _crawl_concurrency, _resolve_concurrency

    assert _crawl_concurrency() == 6  # the crawler's own default before any save

    resp = client.post(
        "/api/admin/server-config",
        json={"sections": {"extraction": {"crawler": {"concurrency": 2}}}},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 200, resp.text

    assert _crawl_concurrency() == 2
    # A run with no per-run override uses the configured value, and says so.
    assert _resolve_concurrency(None) == (2, 2, "config")


# ---------------------------------------------------------------------------
# extraction.crawler.convert_child_memory_limit_mb — the conversion child's
# RLIMIT_AS HEADROOM, admin-editable (2026-09-02 live-deployment follow-up).
#
# The key already existed and was already read by the crawler
# (`_convert_child_memory_limit_bytes`) — only the server-config declaration
# and its range validation are new here, matching how `concurrency` above
# was surfaced.
# ---------------------------------------------------------------------------


def test_convert_child_memory_limit_is_a_known_field_with_the_crawlers_own_default(seeded_app, monkeypatch):
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    resp = client.get("/api/admin/server-config", headers=_auth(seeded_app["admin_token"]))
    spec = resp.json()["known_fields"]["extraction"]["crawler"]["fields"]["convert_child_memory_limit_mb"]
    assert spec["kind"] == "int"

    from connectors.sharepoint.crawler import _DEFAULT_CONVERT_CHILD_MEMORY_LIMIT_MB

    assert spec["default"] == _DEFAULT_CONVERT_CHILD_MEMORY_LIMIT_MB == 1536
    # The hint must say this is HEADROOM, not an absolute ceiling — the
    # exact live-deployment bug this field's own validation follow-up fixed.
    assert "headroom" in spec["hint"].lower()


def test_post_convert_child_memory_limit_persists_and_get_reflects_it(seeded_app, monkeypatch):
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    headers = _auth(seeded_app["admin_token"])
    resp = client.post(
        "/api/admin/server-config",
        json={"sections": {"extraction": {"crawler": {"convert_child_memory_limit_mb": 3072}}}},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    from app.secrets import _state_dir

    loaded = yaml.safe_load((_state_dir() / "instance.yaml").read_text())
    assert loaded["extraction"]["crawler"]["convert_child_memory_limit_mb"] == 3072

    resp2 = client.get("/api/admin/server-config", headers=headers)
    assert resp2.json()["sections"]["extraction"]["crawler"]["convert_child_memory_limit_mb"] == 3072


def test_convert_child_memory_limit_zero_disables_the_cap_and_is_accepted(seeded_app, monkeypatch):
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    resp = client.post(
        "/api/admin/server-config",
        json={"sections": {"extraction": {"crawler": {"convert_child_memory_limit_mb": 0}}}},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 200, resp.text


def test_convert_child_memory_limit_out_of_range_is_refused(seeded_app, monkeypatch):
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    for value in (-1, 65537):
        resp = client.post(
            "/api/admin/server-config",
            json={"sections": {"extraction": {"crawler": {"convert_child_memory_limit_mb": value}}}},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 422, (value, resp.text)
        assert "extraction.crawler.convert_child_memory_limit_mb" in resp.text


def test_convert_child_memory_limit_must_be_an_integer(seeded_app, monkeypatch):
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    for value in ("1536", 1536.5, True):
        resp = client.post(
            "/api/admin/server-config",
            json={"sections": {"extraction": {"crawler": {"convert_child_memory_limit_mb": value}}}},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 422, (value, resp.text)


def test_post_convert_child_memory_limit_keeps_sibling_crawler_keys(seeded_app, monkeypatch):
    """Same deep-merge contract `concurrency` already has — saving this one
    leaf must not wipe the crawler keys the panel does not render yet."""
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    headers = _auth(seeded_app["admin_token"])
    first = client.post(
        "/api/admin/server-config",
        json={"sections": {"extraction": {"crawler": {"concurrency": 2}}}},
        headers=headers,
    )
    assert first.status_code == 200, first.text
    second = client.post(
        "/api/admin/server-config",
        json={"sections": {"extraction": {"crawler": {"convert_child_memory_limit_mb": 2048}}}},
        headers=headers,
    )
    assert second.status_code == 200, second.text

    from app.secrets import _state_dir

    loaded = yaml.safe_load((_state_dir() / "instance.yaml").read_text())
    assert loaded["extraction"]["crawler"] == {"concurrency": 2, "convert_child_memory_limit_mb": 2048}


def test_saved_convert_child_memory_limit_is_what_the_next_crawl_run_reads(seeded_app, monkeypatch):
    """End to end through the real save path — no restart, no monkeypatch
    of get_value."""
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]

    from connectors.sharepoint.crawler import _convert_child_memory_limit_bytes

    assert _convert_child_memory_limit_bytes() == 1536 * 1024 * 1024  # the crawler's own default before any save

    resp = client.post(
        "/api/admin/server-config",
        json={"sections": {"extraction": {"crawler": {"convert_child_memory_limit_mb": 256}}}},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 200, resp.text
    assert _convert_child_memory_limit_bytes() == 256 * 1024 * 1024


# ---------------------------------------------------------------------------
# Run knobs an admin needs without server access (2026-09-02): lane
# concurrency, and the facts stage's stream_every / run_timeout_s / transport /
# retry_mode instance defaults — declared, validated, persisted.
# ---------------------------------------------------------------------------


def test_run_knobs_are_known_fields_with_the_stages_own_defaults(seeded_app, monkeypatch):
    client, token = _client(seeded_app, monkeypatch)
    fields = client.get("/api/admin/server-config", headers=_auth(token)).json()["known_fields"]["extraction"]
    from app.worker.runtime import _DEFAULT_EXTRACTION_CONCURRENCY
    from connectors.sharepoint.facts_extraction import DEFAULT_STANDALONE_TIMEOUT_S

    assert fields["concurrency"]["kind"] == "int"
    assert fields["concurrency"]["default"] == _DEFAULT_EXTRACTION_CONCURRENCY
    facts = fields["facts"]["fields"]
    assert facts["stream_every"] == {**facts["stream_every"], "kind": "int", "default": 0}
    assert facts["run_timeout_s"]["default"] == DEFAULT_STANDALONE_TIMEOUT_S
    assert facts["transport"]["default"] == "sync"
    assert facts["retry_mode"]["default"] == "on_gate_fail"


def test_caps_match_the_stages_own_clamps():
    """`extraction.concurrency` (the LANE cap) must equal the worker
    runtime's own clamp — a live run posted 12, the runtime silently
    re-clamped it to 8 and logged a warning nobody saw until after the
    fact. `extraction.facts.concurrency` is a different stage (document
    concurrency inside one facts pass) with its own, unrelated ceiling."""
    from app.api.admin import _FACTS_CONCURRENCY_MAX, _LANE_CONCURRENCY_MAX
    from app.worker.runtime import _MAX_EXTRACTION_CONCURRENCY
    from connectors.sharepoint.facts_extraction import MAX_CONCURRENCY

    assert _FACTS_CONCURRENCY_MAX == MAX_CONCURRENCY == 64
    assert _LANE_CONCURRENCY_MAX == _MAX_EXTRACTION_CONCURRENCY == 8


def test_post_run_knobs_persist_and_get_reflects_them(seeded_app, monkeypatch):
    client, token = _client(seeded_app, monkeypatch)
    resp = client.post(
        "/api/admin/server-config",
        json={
            "sections": {
                "extraction": {
                    "concurrency": 6,
                    "facts": {"stream_every": 300, "transport": "batch", "retry_mode": "off", "run_timeout_s": 7200},
                }
            }
        },
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    got = client.get("/api/admin/server-config", headers=_auth(token)).json()["sections"]["extraction"]
    assert got["concurrency"] == 6
    assert got["facts"]["stream_every"] == 300
    assert got["facts"]["transport"] == "batch"
    assert got["facts"]["retry_mode"] == "off"
    assert got["facts"]["run_timeout_s"] == 7200


@pytest.mark.parametrize(
    "patch",
    [
        {"concurrency": 0},
        {"concurrency": 9},
        {"facts": {"concurrency": 65}},
        {"facts": {"stream_every": -1}},
        {"facts": {"run_timeout_s": 5}},
        {"facts": {"transport": "carrier-pigeon"}},
        {"facts": {"retry_mode": "sometimes"}},
        {"facts": {"stream_every": "300"}},
    ],
)
def test_run_knobs_out_of_range_or_wrong_type_are_refused(seeded_app, monkeypatch, patch):
    client, token = _client(seeded_app, monkeypatch)
    resp = client.post("/api/admin/server-config", json={"sections": {"extraction": patch}}, headers=_auth(token))
    assert resp.status_code == 422, resp.text
