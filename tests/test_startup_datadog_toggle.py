"""What the startup script actually renders when Datadog is on, and when it is off.

The rest of the infra suite asserts on the template's SOURCE text. This file
renders it — with `tests/_tf_template.py`, whose output was verified
byte-for-byte against `terraform console` — because the two things that can go
wrong here are only visible after rendering: where the agent block ends up
relative to `docker compose up` (the compose section exits 1 on failure, so a
block placed after it is skipped on exactly the boot that needs monitoring),
and whether the API key is still in scope when `/opt/agnes/.env` is written.
"""

from __future__ import annotations

import base64
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _tf_template import render_template  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
MODULE = REPO / "infra/modules/customer-instance"
TPL = (MODULE / "startup-script.sh.tpl").read_text()

AGENT_VERSION = "7.82.3"
SECRET_NAME = "example-datadog-agent-api-key"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


DATADOG_FILES = {
    "datadog.yaml": _b64('api_key: "@@DD_API_KEY@@"\nsite: datadoghq.com\n'),
    "conf.d/disk.yaml": _b64("init_config: {}\n"),
    "conf.d/http_check.yaml": _b64("init_config: {}\n"),
    # An empty payload is the "remove this check" signal.
    "conf.d/tls.yaml": "",
    "postgres.yaml.tpl": _b64("ad_identifiers:\n  - postgres\n"),
    "agnes-datadog-pg-role.sh": _b64("#!/usr/bin/env bash\nexit 0\n"),
    "agnes-datadog-pg-role.service": _b64("[Service]\nType=oneshot\n"),
    "agnes-datadog-pg-role.timer": _b64("[Timer]\nOnBootSec=2min\n"),
}

# Every non-Datadog input the template needs, at values that exercise the TLS
# path. Kept complete on purpose: `_tf_template` raises on an undefined
# variable, so a new template argument fails here loudly instead of rendering
# something the real boot would never produce.
BASE_VARS: dict = {
    "customer_name": "acme",
    "image_repo": "example.registry/agnes",
    "image_tag": "stable",
    "app_mem_limit": "4g",
    "scheduler_mem_limit": "1g",
    "app_cpus": "2.0",
    "scheduler_cpus": "0.5",
    "upgrade_mode": "auto",
    "upgrade_schedule": "*/5 * * * *",
    "tls_mode": "caddy",
    "domain": "agnes.example.com",
    "domain_alias": "alias.example.com",
    "chat_provider": "",
    "instance_branding_b64": "",
    "acme_email": "ops@example.com",
    "data_source": "keboola",
    "keboola_stack_url": "https://connection.example.com",
    "seed_admin_email": "admin@example.com",
    "seed_admin_password": "",
    "role": "prod",
    "oauth_client_id_secret_name": "google-oauth-client-id",
    "oauth_client_secret_secret_name": "google-oauth-client-secret",
    "runtime_secret_env": {},
    "runtime_secret_env_multiline": {},
    "data_apps_enabled": False,
    "data_apps_subdomain_base": "",
    "data_apps_runtime_image": "example/runtime:1",
    "enable_watchdog": True,
    "enable_gcp_logging": True,
    "alert_webhook_url": "",
    "watchdog_files_b64": {"agnes-watchdog.sh": _b64("#!/bin/bash\n")},
    "ops_agent_config_b64": _b64("logging: {}\n"),
    "dispatcher_enabled": False,
    "dispatcher_image": "",
    "dispatcher_key_secret": "",
    "dispatcher_vertex_sa_secret": "",
    "dispatcher_policies_b64": "",
    "kai_agent_enabled": False,
    "kai_agent_mem_limit": "2g",
    "kai_agent_cpus": "1.0",
    "kai_agent_pg_mem_limit": "1g",
    "kai_agent_broker_mcp_enabled": False,
    "kai_agent_image": "",
    "kai_agent_jwt_secret": "",
    "kai_agent_e2b_key_secret": "",
    "extraction_worker_enabled": False,
    "extraction_worker_image": "",
    "extraction_worker_mem_limit": "1g",
    "extraction_worker_cpus": "0.5",
    "kai_agent_env_b64": "",
}


def _render(enabled: bool) -> str:
    variables = dict(BASE_VARS)
    variables.update(
        enable_datadog=enabled,
        datadog_api_key_secret=SECRET_NAME if enabled else "",
        datadog_agent_version=AGENT_VERSION,
        datadog_files_b64=DATADOG_FILES if enabled else {},
    )
    return render_template(TPL, variables)


@pytest.fixture(scope="module")
def on() -> str:
    return _render(True)


@pytest.fixture(scope="module")
def off() -> str:
    return _render(False)


def test_the_fixture_covers_exactly_the_template_arguments_the_module_passes():
    main = (MODULE / "main.tf").read_text()
    start = main.index("metadata_startup_script = templatefile(")
    block = main[start : main.index("\n  })\n", start)]
    passed = set(re.findall(r"^\s{4}([a-z_0-9]+)\s*=", block, re.M))
    known = set(BASE_VARS) | {
        "enable_datadog",
        "datadog_api_key_secret",
        "datadog_agent_version",
        "datadog_files_b64",
    }
    assert passed == known, (
        "the module's templatefile() arguments drifted from this file's fixture: "
        f"missing here {sorted(passed - known)}, stale here {sorted(known - passed)}"
    )


# --------------------------------------------------------------------------
# Placement
# --------------------------------------------------------------------------


def test_the_agent_installs_before_compose_can_abort_the_boot(on: str):
    agent = on.index("--- DATADOG AGENT")
    # The compose section ends in `exit 1`; anything after it is skipped on the
    # boot where monitoring matters most.
    compose_up = on.index("docker compose $COMPOSE_PROFILES_ARG up -d")
    assert agent < compose_up
    # ...and after the Ops Agent block, so the two host agents install together.
    assert on.index("--- OPS AGENT") < agent


def test_the_pg_role_timer_starts_after_compose_has_converged(on: str):
    timer = on.index("--- DATADOG: Postgres side-car monitoring role")
    assert on.index("docker compose $COMPOSE_PROFILES_ARG up -d") < timer, "the first run must find the side-cars up"
    assert timer < on.index("WD_STAGE=/opt/agnes-watchdog")
    assert "[ -x /usr/local/bin/agnes-datadog-pg-role.sh ]" in on
    assert "systemctl enable --now agnes-datadog-pg-role.timer" in on


# --------------------------------------------------------------------------
# The API key
# --------------------------------------------------------------------------


def test_the_key_fetch_is_the_silent_form_that_cannot_abort_the_boot(on: str):
    assert (
        f'DD_API_KEY_VALUE=$(gcloud secrets versions access latest --secret={SECRET_NAME} 2>/dev/null || echo "")'
    ) in on, "the loud form used for the app's own secrets aborts the boot; monitoring must not"
    assert 'if [ -z "$DD_API_KEY_VALUE" ]; then' in on
    assert "WARNING: Datadog API key secret" in on


def test_the_key_is_out_of_scope_before_anything_writes_dot_env(on: str):
    # The .env heredoc is UNQUOTED, and `set -a; . .env` exports the result into
    # every container's environment. One stray $DD_ line in that window would be
    # a key leak that no test downstream could see.
    unset_at = on.index("unset DD_API_KEY_VALUE")
    env_write = on.index('cat > "$APP_DIR/.env"')
    assert unset_at < env_write
    for line_no, line in enumerate(on.splitlines(), 1):
        if "DD_API_KEY_VALUE" in line:
            assert line_no < env_write, f"line {line_no} touches the key after .env is written"


def test_the_key_never_reaches_argv_or_the_startup_log(on: str):
    assert "set -x" not in on, "a trace would print the key"
    # Substitution is bash parameter expansion on a variable, never sed/argv.
    assert "${_dd_content//@@DD_API_KEY@@/$DD_API_KEY_VALUE}" in on
    block = on[on.index("--- DATADOG AGENT") : on.index("# Boot-time gcplogs driver probe")]
    assert not re.search(r"\bsed\b[^\n]*DD_API_KEY", block), (
        "a sed substitution would put the key on argv, and from there into /proc "
        "and into this script's own log on any error"
    )
    for line in on.splitlines():
        if "DD_API_KEY_VALUE" in line:
            assert not re.match(r"\s*echo\b", line), f"the key must never be echoed: {line}"


def test_datadog_yaml_is_root_owned_and_group_readable_only_by_the_agent(on: str):
    block = on[on.index("_dd_install_artifact() {") : on.index('if [ -z "$DD_API_KEY_VALUE" ]')]
    assert "_dd_target=/etc/datadog-agent/datadog.yaml; _dd_owner=root; _dd_group=dd-agent; _dd_mode=0640" in block
    assert 'install -o "$_dd_owner" -g "$_dd_group" -m "$_dd_mode"' in block


# --------------------------------------------------------------------------
# Install and configure
# --------------------------------------------------------------------------


def test_the_agent_version_is_pinned_and_held(on: str):
    assert f'"datadog-agent=1:{AGENT_VERSION}-1"' in on
    assert "apt-mark hold datadog-agent" in on, "an unheld package drifts on the next apt upgrade"
    assert "apt-mark unhold datadog-agent" in on, "a held package cannot be re-pinned"
    assert "signed-by=$DD_KEYRING" in on
    assert "https://apt.datadoghq.com/ stable 7" in on
    # Reinstalling on every boot would cost minutes and network for nothing.
    assert f'!= "1:{AGENT_VERSION}-1"' in on


def test_the_agent_joins_the_docker_group_and_the_service_is_enabled(on: str):
    assert "usermod -aG docker dd-agent" in on
    assert "systemctl enable datadog-agent" in on
    assert "systemctl restart datadog-agent" in on
    assert "id dd-agent >/dev/null 2>&1" in on, (
        "every ownership flag below needs the group to exist; a failed apt step must not turn into a failed boot"
    )


def test_every_artifact_is_installed_and_an_empty_payload_removes_its_target(on: str):
    for rel, payload in DATADOG_FILES.items():
        assert f'_dd_install_artifact "{rel}" "{payload}"' in on, rel
    block = on[on.index("_dd_install_artifact() {") :]
    assert 'if [ -z "$_dd_b64" ]; then\n        rm -f "$_dd_target"' in block
    # conf.d/<check>.yaml -> /etc/datadog-agent/conf.d/<check>.d/conf.yaml
    assert '_dd_target="/etc/datadog-agent/conf.d/$_dd_check.d/conf.yaml"' in block
    assert 'mkdir -p "$(dirname "$_dd_target")"' in block, (
        "the conf.d keys are nested paths; the decode would fail without this"
    )


def test_no_step_of_the_agent_block_can_fail_the_boot(on: str):
    block = on[on.index("--- DATADOG AGENT") : on.index("# Boot-time gcplogs driver probe")]
    assert "set -euo pipefail" not in block
    # The install subshell must chain with && (errexit is suppressed inside a
    # command that is part of an || list, so `set -e` in there buys nothing).
    install = block[block.index("install -d -m 0755 /usr/share/keyrings") : block.index("apt-mark hold")]
    assert install.count("&&") >= 8
    assert '|| echo "WARNING: Datadog Agent' in block
    for risky in ("usermod -aG docker dd-agent", "systemctl restart datadog-agent"):
        idx = block.index(risky)
        assert "||" in block[idx : idx + 200], f"{risky} is unguarded"


# --------------------------------------------------------------------------
# The off path
# --------------------------------------------------------------------------


def test_disabled_installs_nothing_and_stops_an_agent_a_previous_config_left(off: str):
    assert "datadog-agent=1:" not in off
    assert "DD_API_KEY_VALUE" not in off
    assert "_dd_install_artifact" not in off
    assert "gcloud secrets versions access latest --secret=" in off, "sanity: other secrets remain"
    assert "systemctl disable --now datadog-agent" in off, (
        "a recreated VM that turned monitoring off must stop shipping under a key nobody rotates any more"
    )


def test_the_off_path_is_the_only_datadog_text_in_a_disabled_render(off: str):
    code = [ln for ln in off.splitlines() if "datadog" in ln.lower() and not ln.lstrip().startswith("#")]
    assert code == [
        "if systemctl is-enabled --quiet datadog-agent 2>/dev/null; then",
        "    systemctl disable --now datadog-agent >/dev/null 2>&1 || true",
    ], code


# --------------------------------------------------------------------------
# The heartbeat, which is independent of Datadog
# --------------------------------------------------------------------------


@pytest.mark.parametrize("enabled", [True, False])
def test_the_auto_upgrade_heartbeat_is_vendor_neutral_and_unconditional(enabled: bool):
    body = _render(enabled)
    assert (
        'CRON_LINE="$UPGRADE_SCHEDULE /usr/local/bin/agnes-auto-upgrade.sh '
        '>> /var/log/agnes-auto-upgrade.log 2>&1; date +%s > /var/lib/agnes/auto-upgrade.tick"'
    ) in body
    mkdir_at = body.index("install -d -m 0755 /var/lib/agnes")
    guard_at = body.index('if [ "$UPGRADE_MODE" = "auto" ]')
    assert mkdir_at < guard_at, (
        "the directory must exist even on a manual-upgrade VM, where a MISSING "
        "tick file is the answer rather than a broken check"
    )


# --------------------------------------------------------------------------
# It has to be a shell script
# --------------------------------------------------------------------------


@pytest.mark.parametrize("enabled", [True, False])
def test_the_rendered_script_is_valid_bash(tmp_path: Path, enabled: bool):
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover - every supported dev/CI image has bash
        pytest.skip("bash not available")
    script = tmp_path / "startup.sh"
    script.write_text(_render(enabled))
    proc = subprocess.run([bash, "-n", str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
