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
cadence) and `timeout_s` (the per-run ceiling the crawl enforces itself).
Precedence is unchanged: env (Terraform-rendered) > admin server-config
overlay > static instance.yaml.

Behaviour contract:
  - GET exposes `extraction` in `editable_sections`/`sections`/`known_fields`
    with exactly `schedule` and `timeout_s` — no `enabled` leaf, no
    `producer` object.
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

import yaml


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _clear_extraction_env(monkeypatch):
    monkeypatch.delenv("AGNES_SHAREPOINT_ENABLED", raising=False)


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
