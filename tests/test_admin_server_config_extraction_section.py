"""``extraction.*`` — admin-editable producer config (T3), reversing the
`extraction` switch's original deploy-time-only stance from #1652/T2.

Precedence after this change: env (Terraform-rendered, T2) > admin
server-config overlay > static instance.yaml — unchanged from T2, this
suite proves the write path never silently loses to it.

Behaviour contract:
  - GET exposes `extraction` in `editable_sections`/`sections`/`known_fields`
    with all six fields (`enabled`, `producer.command`, `producer.module`,
    `producer.env_passthrough`, `schedule`, `timeout_s`).
  - POST with valid values persists; GET reflects them.
  - Field-level validation: `env_passthrough` entries must match
    `^[A-Z][A-Z0-9_]*$`; `schedule` must parse via `src.scheduler.
    is_valid_schedule`; `timeout_s` must be within [60, 86400]; `enabled`
    must be a bool.
  - Env-lock honesty: `AGNES_EXTRACTION_ENABLED` /
    `AGNES_EXTRACTION_PRODUCER_COMMAND` / `AGNES_EXTRACTION_PRODUCER_MODULE`
    each pin their leaf — GET's `known_fields` marks it `env_locked` and
    GET's `sections` shows the ACTUAL env-resolved value, and POST touching
    a pinned leaf 409s with `field_locked_by_deployment` rather than
    persisting a value the runtime would never read.
  - A web-set `enabled`/`producer.command` is picked up live by
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
    monkeypatch.delenv("AGNES_EXTRACTION_ENABLED", raising=False)
    monkeypatch.delenv("AGNES_EXTRACTION_PRODUCER_COMMAND", raising=False)
    monkeypatch.delenv("AGNES_EXTRACTION_PRODUCER_MODULE", raising=False)


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
    assert fields["enabled"]["kind"] == "bool"
    assert fields["enabled"]["default"] is False
    producer = fields["producer"]
    assert producer["kind"] == "object"
    assert producer["fields"]["command"]["kind"] == "string"
    assert producer["fields"]["module"]["kind"] == "string"
    assert producer["fields"]["env_passthrough"]["kind"] == "array"
    assert producer["fields"]["env_passthrough"]["item_kind"] == "string"
    assert fields["schedule"]["kind"] == "string"
    assert fields["timeout_s"]["kind"] == "int"
    assert fields["timeout_s"]["default"] == 3600


def test_extraction_switch_is_editable(seeded_app, monkeypatch):
    """The registry itself — `extraction.enabled`'s reversal is visible in
    the feature_flags inventory too, not just known_fields."""
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    resp = client.get("/api/admin/server-config", headers=_auth(seeded_app["admin_token"]))
    flags = {f["name"]: f for f in resp.json()["feature_flags"]}
    assert flags["extraction"]["editable"] is True
    assert flags["extraction"]["lock_reason"] == ""


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
                    "enabled": True,
                    "producer": {
                        "command": "python -m fake_producer",
                        "env_passthrough": ["MY_CUSTOM_VAR"],
                    },
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
    assert loaded["extraction"]["enabled"] is True
    assert loaded["extraction"]["producer"]["command"] == "python -m fake_producer"
    assert loaded["extraction"]["schedule"] == "every 30m"
    assert loaded["extraction"]["timeout_s"] == 1800

    resp2 = client.get("/api/admin/server-config", headers=headers)
    section = resp2.json()["sections"]["extraction"]
    assert section["enabled"] is True
    assert section["producer"]["command"] == "python -m fake_producer"
    assert section["producer"]["env_passthrough"] == ["MY_CUSTOM_VAR"]
    assert section["schedule"] == "every 30m"
    assert section["timeout_s"] == 1800


def test_post_extraction_effect_is_live_not_restart(seeded_app, monkeypatch):
    """Every extraction leaf is read fresh per call — a save must not force
    `restart_required`."""
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    resp = client.post(
        "/api/admin/server-config",
        json={"sections": {"extraction": {"enabled": True}}},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["sections_effect"]["extraction"] == "live"
    assert resp.json()["restart_required"] is False


# ---------------------------------------------------------------------------
# POST — validation
# ---------------------------------------------------------------------------


def test_enabled_must_be_bool(seeded_app, monkeypatch):
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    resp = client.post(
        "/api/admin/server-config",
        json={"sections": {"extraction": {"enabled": "yes"}}},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 422, resp.text


def test_producer_command_must_be_string(seeded_app, monkeypatch):
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    resp = client.post(
        "/api/admin/server-config",
        json={"sections": {"extraction": {"producer": {"command": 123}}}},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 422, resp.text


def test_env_passthrough_rejects_invalid_names(seeded_app, monkeypatch):
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    resp = client.post(
        "/api/admin/server-config",
        json={"sections": {"extraction": {"producer": {"env_passthrough": ["not_upper", "OK_NAME"]}}}},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 422, resp.text
    assert "not_upper" in resp.text


def test_env_passthrough_accepts_valid_names(seeded_app, monkeypatch):
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    resp = client.post(
        "/api/admin/server-config",
        json={"sections": {"extraction": {"producer": {"env_passthrough": ["HTTP_PROXY_EXTRA", "MY_VAR2"]}}}},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 200, resp.text


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
# Env-lock honesty
# ---------------------------------------------------------------------------


class TestEnvLockHonesty:
    def test_enabled_locked_when_env_var_present(self, seeded_app, monkeypatch):
        _clear_extraction_env(monkeypatch)
        monkeypatch.setenv("AGNES_EXTRACTION_ENABLED", "1")
        client = seeded_app["client"]
        resp = client.get("/api/admin/server-config", headers=_auth(seeded_app["admin_token"]))
        spec = resp.json()["known_fields"]["extraction"]["enabled"]
        assert spec["env_locked"] is True
        assert spec["env_var"] == "AGNES_EXTRACTION_ENABLED"
        # The panel must show the ACTUAL resolved value, not a stale yaml one.
        assert resp.json()["sections"]["extraction"]["enabled"] is True

    def test_enabled_not_locked_when_env_var_absent(self, seeded_app, monkeypatch):
        _clear_extraction_env(monkeypatch)
        client = seeded_app["client"]
        resp = client.get("/api/admin/server-config", headers=_auth(seeded_app["admin_token"]))
        spec = resp.json()["known_fields"]["extraction"]["enabled"]
        assert spec["env_locked"] is False
        assert spec["env_var"] == "AGNES_EXTRACTION_ENABLED"

    def test_producer_command_locked_when_env_var_present(self, seeded_app, monkeypatch):
        _clear_extraction_env(monkeypatch)
        monkeypatch.setenv("AGNES_EXTRACTION_PRODUCER_COMMAND", "python /opt/producer/agnes_lane.py")
        client = seeded_app["client"]
        resp = client.get("/api/admin/server-config", headers=_auth(seeded_app["admin_token"]))
        body = resp.json()
        spec = body["known_fields"]["extraction"]["producer"]["fields"]["command"]
        assert spec["env_locked"] is True
        assert spec["env_var"] == "AGNES_EXTRACTION_PRODUCER_COMMAND"
        assert body["sections"]["extraction"]["producer"]["command"] == "python /opt/producer/agnes_lane.py"

    def test_producer_module_locked_when_env_var_present(self, seeded_app, monkeypatch):
        _clear_extraction_env(monkeypatch)
        monkeypatch.setenv("AGNES_EXTRACTION_PRODUCER_MODULE", "your_producer.run")
        client = seeded_app["client"]
        resp = client.get("/api/admin/server-config", headers=_auth(seeded_app["admin_token"]))
        body = resp.json()
        spec = body["known_fields"]["extraction"]["producer"]["fields"]["module"]
        assert spec["env_locked"] is True
        assert body["sections"]["extraction"]["producer"]["module"] == "your_producer.run"

    def test_schedule_and_timeout_are_never_env_locked(self, seeded_app, monkeypatch):
        """Only the three T2 env carriers lock a field — schedule/timeout_s/
        env_passthrough carry no such deploy-time env var and must stay
        plain editable fields regardless of any of the three being set."""
        _clear_extraction_env(monkeypatch)
        monkeypatch.setenv("AGNES_EXTRACTION_ENABLED", "1")
        client = seeded_app["client"]
        resp = client.get("/api/admin/server-config", headers=_auth(seeded_app["admin_token"]))
        fields = resp.json()["known_fields"]["extraction"]
        assert "env_locked" not in fields["schedule"]
        assert "env_locked" not in fields["timeout_s"]
        assert "env_locked" not in fields["producer"]["fields"]["env_passthrough"]

    def test_post_enabled_refused_with_409_when_env_locked(self, seeded_app, monkeypatch):
        _clear_extraction_env(monkeypatch)
        monkeypatch.setenv("AGNES_EXTRACTION_ENABLED", "1")
        client = seeded_app["client"]
        resp = client.post(
            "/api/admin/server-config",
            json={"sections": {"extraction": {"enabled": False}}},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "field_locked_by_deployment"
        assert detail["field"] == "enabled"
        assert detail["env_var"] == "AGNES_EXTRACTION_ENABLED"

    def test_post_producer_command_refused_with_409_when_env_locked(self, seeded_app, monkeypatch):
        _clear_extraction_env(monkeypatch)
        monkeypatch.setenv("AGNES_EXTRACTION_PRODUCER_COMMAND", "python /opt/producer/agnes_lane.py")
        client = seeded_app["client"]
        resp = client.post(
            "/api/admin/server-config",
            json={"sections": {"extraction": {"producer": {"command": "python -m evil"}}}},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "field_locked_by_deployment"
        assert detail["field"] == "producer.command"

    def test_post_producer_module_refused_with_409_when_env_locked(self, seeded_app, monkeypatch):
        _clear_extraction_env(monkeypatch)
        monkeypatch.setenv("AGNES_EXTRACTION_PRODUCER_MODULE", "your_producer.run")
        client = seeded_app["client"]
        resp = client.post(
            "/api/admin/server-config",
            json={"sections": {"extraction": {"producer": {"module": "evil.run"}}}},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["field"] == "producer.module"

    def test_locked_leaf_touched_alongside_an_unlocked_one_still_refuses(self, seeded_app, monkeypatch):
        """A patch that touches BOTH a locked leaf and an unlocked one (e.g.
        schedule) must refuse the whole save — accepting the unlocked half
        while silently dropping the locked one would be its own lie."""
        _clear_extraction_env(monkeypatch)
        monkeypatch.setenv("AGNES_EXTRACTION_ENABLED", "1")
        client = seeded_app["client"]
        resp = client.post(
            "/api/admin/server-config",
            json={"sections": {"extraction": {"enabled": False, "schedule": "every 15m"}}},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 409, resp.text

    def test_post_unrelated_leaf_succeeds_while_another_is_env_locked(self, seeded_app, monkeypatch):
        """Only the TOUCHED locked leaf refuses — a save that doesn't
        mention `enabled` must still be able to set `schedule`/`timeout_s`
        even while `AGNES_EXTRACTION_ENABLED` is pinned."""
        _clear_extraction_env(monkeypatch)
        monkeypatch.setenv("AGNES_EXTRACTION_ENABLED", "1")
        client = seeded_app["client"]
        resp = client.post(
            "/api/admin/server-config",
            json={"sections": {"extraction": {"schedule": "every 15m"}}},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200, resp.text

    def test_post_succeeds_when_env_var_absent(self, seeded_app, monkeypatch):
        """Regression guard: absent the T2 env vars, ordinary writes to
        enabled/producer.command must keep working exactly as any other
        editable field."""
        _clear_extraction_env(monkeypatch)
        client = seeded_app["client"]
        resp = client.post(
            "/api/admin/server-config",
            json={"sections": {"extraction": {"enabled": True, "producer": {"command": "python -m producer"}}}},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Audit — the FULL new command value lands in the generic diff, unmasked
# ---------------------------------------------------------------------------


def test_audit_log_carries_the_full_new_command_value(seeded_app, monkeypatch):
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    resp = client.post(
        "/api/admin/server-config",
        json={"sections": {"extraction": {"producer": {"command": "python -m my_special_producer --flag"}}}},
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
    matching = [d for d in diff_entries if d["path"] == "extraction.producer.command"]
    assert matching, diff_entries
    assert matching[0]["after"] == "python -m my_special_producer --flag"


# ---------------------------------------------------------------------------
# Live pickup — no restart — of a web-set producer config (task item 5)
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
        json={
            "sections": {
                "extraction": {
                    "enabled": True,
                    "producer": {"command": "python -m fake_producer"},
                }
            }
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    # Same process, no restart, no monkeypatch of get_value — the overlay
    # write above is the only thing that changed.
    usable, err = _extraction_readiness()
    assert usable is True, err


def test_extraction_trigger_endpoint_picks_up_web_saved_producer(seeded_app, monkeypatch):
    """End-to-end through the real HTTP surface: POST server-config sets
    the producer, then the manual trigger endpoint enqueues successfully
    with no restart and no direct get_value monkeypatch."""
    _clear_extraction_env(monkeypatch)
    client = seeded_app["client"]
    headers = _auth(seeded_app["admin_token"])

    resp = client.post(
        "/api/admin/server-config",
        json={
            "sections": {
                "extraction": {
                    "enabled": True,
                    "producer": {"command": "python -m fake_producer"},
                    "timeout_s": 60,
                }
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
