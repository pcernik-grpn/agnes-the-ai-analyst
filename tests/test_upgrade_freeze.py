"""TCRD-238: a per-instance deploy freeze an admin can set without SSH.

An auto-upgrade tick once landed in the middle of a customer demo; the
mitigation was "ping the operator an hour ahead", a human lock. The freeze is
a marker file on the state disk — `{DATA_DIR}/state/upgrade-freeze-until`,
holding a UTC epoch — written by an admin endpoint and honored by the VM's
`agnes-auto-upgrade.sh` tick (host `STATE_DIR` and container
`{DATA_DIR}/state` are the same disk).

Two halves pinned here: the admin API that manages the marker, and a
contract test that the host script actually consults it (the script runs on
VMs — it cannot be executed in CI, but its freeze block can be pinned the
way template contracts are).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch, seeded_app, admin_user, analyst_user):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return {"client": seeded_app["client"], "admin": admin_user, "analyst": analyst_user, "data_dir": tmp_path}


def _marker(data_dir: Path) -> Path:
    return data_dir / "state" / "upgrade-freeze-until"


class TestFreezeEndpoint:
    def test_status_reports_no_freeze_initially(self, client):
        r = client["client"].get("/api/admin/upgrade-freeze", headers=client["admin"])
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["active"] is False
        assert body["until"] is None

    def test_post_writes_the_marker_and_status_reflects_it(self, client):
        r = client["client"].post("/api/admin/upgrade-freeze", headers=client["admin"], json={"hours": 2})
        assert r.status_code == 200, r.text
        until = r.json()["until_epoch"]
        assert until == pytest.approx(time.time() + 2 * 3600, abs=120)

        marker = _marker(client["data_dir"])
        assert marker.is_file(), "the freeze must be the marker file the VM tick reads"
        assert int(marker.read_text().strip()) == until

        status = client["client"].get("/api/admin/upgrade-freeze", headers=client["admin"]).json()
        assert status["active"] is True

    def test_delete_lifts_the_freeze(self, client):
        client["client"].post("/api/admin/upgrade-freeze", headers=client["admin"], json={"hours": 1})
        r = client["client"].delete("/api/admin/upgrade-freeze", headers=client["admin"])
        assert r.status_code == 200, r.text
        assert not _marker(client["data_dir"]).exists()
        status = client["client"].get("/api/admin/upgrade-freeze", headers=client["admin"]).json()
        assert status["active"] is False

    def test_hours_are_bounded(self, client):
        """A typo'd freeze must not silently disable upgrades for a month."""
        for bad in (0, -1, 100):
            r = client["client"].post("/api/admin/upgrade-freeze", headers=client["admin"], json={"hours": bad})
            assert r.status_code == 422, f"hours={bad} must be rejected"

    def test_expired_marker_reads_as_inactive(self, client):
        marker = _marker(client["data_dir"])
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(int(time.time()) - 60))
        status = client["client"].get("/api/admin/upgrade-freeze", headers=client["admin"]).json()
        assert status["active"] is False, "a freeze in the past is no freeze"

    def test_admin_only(self, client):
        assert client["client"].get("/api/admin/upgrade-freeze", headers=client["analyst"]).status_code == 403
        assert (
            client["client"].post("/api/admin/upgrade-freeze", headers=client["analyst"], json={"hours": 1}).status_code
            == 403
        )

    def test_freeze_actions_are_audited(self, client):
        client["client"].post("/api/admin/upgrade-freeze", headers=client["admin"], json={"hours": 1})
        client["client"].delete("/api/admin/upgrade-freeze", headers=client["admin"])
        from src.db import get_system_db

        conn = get_system_db()
        try:
            actions = [
                r[0]
                for r in conn.execute(
                    "SELECT action FROM audit_log WHERE action LIKE 'upgrade_freeze%' ORDER BY timestamp"
                ).fetchall()
            ]
        finally:
            conn.close()
        assert "upgrade_freeze.set" in actions
        assert "upgrade_freeze.lift" in actions


class TestHostScriptHonorsTheMarker:
    """The script runs on VMs, not in CI — pin its freeze block by contract,
    the same way template contracts are pinned."""

    SCRIPT = Path("scripts/ops/agnes-auto-upgrade.sh")

    def test_freeze_block_exists_and_reads_the_marker(self):
        src = self.SCRIPT.read_text(encoding="utf-8")
        assert "upgrade-freeze-until" in src, "the tick must consult ${STATE_DIR}/upgrade-freeze-until before pulling"

    def test_freeze_check_runs_before_the_pull(self):
        src = self.SCRIPT.read_text(encoding="utf-8")
        # Anchor on the actual command line, not the word in a comment.
        assert src.index("upgrade-freeze-until") < src.index("docker compose pull >"), (
            "the freeze must short-circuit the tick before any image pull"
        )

    def test_garbage_marker_fails_open(self):
        """A corrupt marker must not freeze the fleet forever — the block
        must validate the value is numeric before honoring it."""
        src = self.SCRIPT.read_text(encoding="utf-8")
        block_start = src.index("upgrade-freeze-until")
        block = src[block_start : block_start + 1500]
        assert "[0-9]" in block or "^[0-9]+$" in block, (
            "the freeze block must validate the marker is a plain epoch and ignore (fail open on) anything else"
        )
