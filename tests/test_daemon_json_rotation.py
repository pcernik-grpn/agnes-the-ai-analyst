"""Docker log-rotation for the non-gcplogs path (`/etc/docker/daemon.json`).

Without an explicit log-driver config, Docker's default `json-file` driver
never rotates: a routinely-recreated container (agnes-auto-upgrade ticks
every 5 min) can accumulate unbounded log files on the boot disk, and the
recreate itself destroys the previous container's log history regardless.

This writes a bounded rotation config EARLY in boot (before docker compose
brings up any container — a daemon restart is safe there and unsafe after),
and ONLY when no `daemon.json` already exists: an operator-authored file
(custom log driver, registry mirror, other daemon setting) must never be
clobbered by this script re-running on every boot.

Same marker-delimited-block pattern as
``test_startup_dispatcher_pg_password.py`` / ``test_startup_vault_key.py`` —
the functional tests execute the real shipped shell, not a re-implementation.
"""

import re
import shutil
import subprocess
from pathlib import Path

TPL = Path("infra/modules/customer-instance/startup-script.sh.tpl")

BEGIN = "# --- docker-log-rotation begin"
END = "# --- docker-log-rotation end"


def _rotation_block() -> str:
    tpl = TPL.read_text()
    m = re.search(re.escape(BEGIN) + r".*?\n(.*?)" + re.escape(END), tpl, re.DOTALL)
    assert m, (
        "startup-script.sh.tpl must contain the marker-delimited "
        f"docker-log-rotation block ({BEGIN!r} ... {END!r}) — the functional "
        "tests below execute it"
    )
    block = m.group(1)
    assert "${" not in block and "%{" not in block, (
        "docker-log-rotation block must not use Terraform interpolation "
        "('${' / '%{'); keep it plain bash so tests execute the shipped code "
        "verbatim"
    )
    return block


def _run_block(tmp_path: Path, existing_daemon_json: str | None = None) -> tuple[str, Path]:
    """Execute the template's docker-log-rotation block against a sandbox.

    Returns (DOCKER_LOG_ROTATION_WRITTEN value, daemon.json path).
    """
    bash = shutil.which("bash")
    assert bash, "bash required"
    daemon_json = tmp_path / "daemon.json"
    if existing_daemon_json is not None:
        daemon_json.write_text(existing_daemon_json)
    script = (
        "set -euo pipefail\n"
        f'DAEMON_JSON="{daemon_json}"\n' + _rotation_block() + '\nprintf "%s" "$DOCKER_LOG_ROTATION_WRITTEN"\n'
    )
    proc = subprocess.run([bash, "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, f"docker-log-rotation block failed: {proc.stderr}"
    return proc.stdout, daemon_json


def test_tpl_references_the_daemon_json_path():
    body = TPL.read_text()
    assert "/etc/docker/daemon.json" in body


def test_write_is_guarded_by_a_file_existence_check():
    block = _rotation_block()
    assert '[ -f "$DAEMON_JSON" ]' in block, (
        "the write must be guarded by a file-existence check so an operator's daemon.json is never clobbered"
    )


def test_rotation_config_has_max_size_and_max_file():
    block = _rotation_block()
    assert '"log-driver": "json-file"' in block
    assert '"max-size": "50m"' in block
    assert '"max-file": "5"' in block


def test_restart_only_fires_when_newly_written():
    """The daemon restart must be conditioned on this boot having written the
    file — an operator's pre-existing daemon.json must never trigger a
    docker restart from this script."""
    body = TPL.read_text()
    restart_idx = body.index("systemctl restart docker")
    guard_idx = body.rindex('if [ "$DOCKER_LOG_ROTATION_WRITTEN" = "1" ]', 0, restart_idx)
    endif_idx = body.index("fi", restart_idx)
    assert guard_idx < restart_idx < endif_idx


def test_restart_runs_before_any_compose_up():
    """A daemon restart is only safe before containers are started — the
    rotation block (and its restart) must appear before the first
    `docker compose ... up` in the script."""
    body = TPL.read_text()
    rotation_idx = body.index(BEGIN)
    first_up_idx = body.index("docker compose $COMPOSE_PROFILES_ARG up")
    assert rotation_idx < first_up_idx


def test_fresh_boot_writes_daemon_json(tmp_path):
    written, daemon_json = _run_block(tmp_path)
    assert written == "1"
    assert daemon_json.exists()
    content = daemon_json.read_text()
    assert '"max-size": "50m"' in content
    assert '"max-file": "5"' in content


def test_existing_daemon_json_is_never_clobbered(tmp_path):
    existing = '{"log-driver": "syslog"}'
    written, daemon_json = _run_block(tmp_path, existing_daemon_json=existing)
    assert written == "0"
    assert daemon_json.read_text() == existing
