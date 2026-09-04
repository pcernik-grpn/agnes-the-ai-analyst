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
    "cloud_logging_logs_active": True,
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
    "otlp_endpoint": "",
    "otlp_headers_secret": "",
    "otlp_capture_content": "0",
    "deployment_env": "agnes-test",
    "kai_agent_image": "",
    "kai_agent_jwt_secret": "",
    "kai_agent_e2b_key_secret": "",
    "extraction_worker_enabled": False,
    "extraction_worker_image": "",
    "extraction_worker_mem_limit": "1g",
    "extraction_worker_cpus": "0.5",
    "extraction_worker_replicas": 1,
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
    # Character offsets on BOTH sides. Comparing a line NUMBER against a
    # character offset here is vacuously true for every possible render, which
    # is what this assertion used to do.
    for match in re.finditer(r"DD_API_KEY_VALUE", on):
        assert match.start() < env_write, (
            f"the key is still referenced at offset {match.start()}, after .env is written"
        )


def test_the_key_never_reaches_argv_or_the_startup_log(on: str):
    assert "set -x" not in on, "a trace would print the key"
    # Substitution is bash parameter expansion on a variable, never sed/argv.
    assert "${_dd_content//@@DD_API_KEY@@/$DD_API_KEY_VALUE}" in on
    block = on[on.index("--- DATADOG AGENT") : on.index("# Boot-time collector probe")]
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


def test_the_artifacts_install_after_the_deb_postinst_that_chowns_the_config_dir(on: str):
    """The order of the apt step and the artifact loop is load-bearing.

    The agent deb's postinst (the embedded fleet installer,
    `installFilesystem` -> `agentConfigPermissions`, verified on 7.82.3)
    enforces dd-agent:dd-agent RECURSIVELY on /etc/datadog-agent — on first
    install and again on every version change. datadog.yaml stays
    root:dd-agent only because the artifact loop runs AFTER that postinst and
    re-installs the file with explicit ownership. Swapping the two — say, to
    have the config in place so the postinst starts the agent already
    configured — would silently hand the agent user ownership of its own
    config file, undoing the root-owned-config property the rendered
    datadog.yaml documents.
    """
    apt_at = on.index('apt-get install -y -qq --allow-downgrades "datadog-agent=')
    first_artifact_at = on.index('_dd_install_artifact "')
    assert apt_at < first_artifact_at, (
        "the artifact loop must stay after the apt step — the deb postinst "
        "recursively chowns /etc/datadog-agent to dd-agent, so artifacts "
        "installed before it would lose their root ownership"
    )


def test_the_agent_joins_the_docker_group_and_the_service_is_enabled(on: str):
    assert "usermod -aG docker dd-agent" in on
    assert "systemctl enable datadog-agent" in on
    assert "systemctl restart datadog-agent" in on
    assert "id dd-agent >/dev/null 2>&1" in on, (
        "every ownership flag below needs the group to exist; a failed apt step must not turn into a failed boot"
    )


def test_the_fleet_installer_unit_is_masked_before_the_agent_can_start(on: str):
    """The mask has to beat the apt step, not merely the `systemctl enable`.

    datadog-agent-installer.service is a soft dependency of
    datadog-agent.service and exits 255 without remote configuration, which
    this module deliberately disables (DataDog/datadog-agent#43052). The deb's
    postinst STARTS the agent — see the artifact-ordering test above, whose
    whole subject is what that postinst does — so the first pull-in happens
    during `apt-get install`, long before anything here enables the service.
    A mask applied after that point arrives one failure too late, and masking
    does not clear a failed state that is already recorded.
    """
    mask_cmd = "ln -sf /dev/null /etc/systemd/system/datadog-agent-installer.service"
    assert mask_cmd in on

    mask = on.index(mask_cmd)
    apt = on.index('apt-get install -y -qq --allow-downgrades "datadog-agent=')
    assert mask < apt, (
        "mask before the package install: its postinst starts the agent, which is what pulls the installer unit in"
    )

    # `systemctl mask` is not used on purpose: it can refuse a unit whose file
    # does not exist yet, which is precisely the state before apt runs.
    assert "systemctl mask datadog-agent-installer.service" not in on

    # A failure a previous boot recorded outlives the mask, so it is cleared too.
    reset = on.index("systemctl reset-failed datadog-agent-installer.service")
    assert reset > apt, "reset-failed only helps after the install that could have failed it"

    # Guarded, like every other step in this block — and asserted on the mask's
    # OWN line, so an unguarded mask cannot be excused by a neighbour's `|| true`.
    mask_line = on[mask : on.index("\n", on.index("|| echo", mask))]
    assert '|| echo "WARNING: could not mask' in mask_line, "the mask step is unguarded"


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


def test_the_artifact_installer_cannot_abort_the_boot(on: str):
    """`set -euo pipefail` is active, and the call is NOT inside a conditional
    unless we put it there. An unguarded failure in the helper — a write on a
    full disk, a chmod on a read-only mount — would then kill the boot instead
    of degrading to a warning, which is the one thing this whole block promises
    not to do. Proven in bash 5.3: an unguarded call aborts the script, and the
    same call with a trailing `|| echo` suppresses errexit for everything inside.
    """
    lines = on.splitlines()
    sites = [ln for ln in lines if '_dd_install_artifact "' in ln]
    assert sites, "no artifact install calls in the rendered script"
    for idx, line in enumerate(lines):
        if '_dd_install_artifact "' not in line:
            continue
        tail = line + "\n" + (lines[idx + 1] if idx + 1 < len(lines) else "")
        assert "||" in tail, f"unguarded artifact install: {line.strip()}"

    body = on[on.index("_dd_install_artifact() {") : on.index('if [ -z "$DD_API_KEY_VALUE" ]')]
    # Every fallible operation inside is guarded too, so a future edit that drops
    # the call-site guard does not silently re-arm the hazard.
    for risky in ('rm -f "$_dd_target"', 'chmod 0600 "$_dd_tmp"', 'rm -f "$_dd_tmp"'):
        idx = body.index(risky)
        assert "||" in body[idx : idx + 60], f"unguarded: {risky}"
    assert 'if ! printf \'%s\\n\' "$_dd_content" > "$_dd_tmp"; then' in body


def test_no_step_of_the_agent_block_can_fail_the_boot(on: str):
    block = on[on.index("--- DATADOG AGENT") : on.index("# Boot-time collector probe")]
    # Code, not comments — the block explains WHY errexit matters here, which is
    # not the same as re-arming it.
    code = "\n".join(ln for ln in block.splitlines() if not ln.lstrip().startswith("#"))
    assert "set -e" not in code, "the Datadog block must not re-arm errexit"
    # The install subshell must chain with && (errexit is suppressed inside a
    # command that is part of an || list, so `set -e` in there buys nothing).
    install = block[block.index("mkdir -p /usr/share/keyrings") : block.index("apt-mark hold")]
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
        "if systemctl is-enabled --quiet agnes-datadog-pg-role.timer 2>/dev/null; then",
        "    systemctl disable --now agnes-datadog-pg-role.timer >/dev/null 2>&1 || true",
    ], code


# --------------------------------------------------------------------------
# The heartbeat, which is independent of Datadog
# --------------------------------------------------------------------------


@pytest.mark.parametrize("enabled", [True, False])
def test_the_auto_upgrade_heartbeat_is_vendor_neutral_and_unconditional(enabled: bool):
    body = _render(enabled)
    cron_line = next(ln for ln in body.splitlines() if ln.lstrip().startswith("CRON_LINE="))
    assert "/usr/local/bin/agnes-auto-upgrade.sh" in cron_line
    assert "/var/lib/agnes/auto-upgrade.tick" in cron_line
    # cron turns an unescaped % in the command field into a newline and hands
    # everything after it to the command as stdin, so `date +%s > file` would
    # run as `date +` and write nothing at all — silently, because the probe
    # that reads the file has ignore_missing set.
    assert "%" not in cron_line.replace("\\%", ""), f"unescaped % in the crontab command field: {cron_line}"
    mkdir_at = body.index("mkdir -p /var/lib/agnes")
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


# --------------------------------------------------------------------------
# The applier's uid, reserved before anything can steal it (#2137 follow-up)
# --------------------------------------------------------------------------


def test_the_applier_uid_is_reserved_before_docker_and_datadog(on: str, off: str):
    """The Datadog agent's apt postinst creates its own `dd-agent` system
    user with no uid pin, and on a fresh image `useradd --system` allocates
    the next free system uid — which used to be $AGNES_APPLIER_UID, because
    agnes-applier's own pinned `useradd` ran later in the script. Whichever
    of the two ran first won the number. Observed live 2026-09-03: dd-agent
    won, the applier fell back to an allocated uid, and the app crash-looped
    on an instance.yaml it could no longer read.

    The reservation must now be the very first thing this script does — in
    both renders, since section 0 has nothing to do with enable_datadog —
    and specifically before the Datadog agent block in the enabled render.
    """
    for label, body in (("on", on), ("off", off)):
        reserve_at = body.index("# --- 0. Reserve the state-applier's pinned uid")
        first_useradd = body.index("if ! id -u agnes-applier")
        docker_at = body.index("# --- 1. Docker (install if missing)")
        assert reserve_at < first_useradd < docker_at, (
            f"[{label}] the applier uid reservation must run, and create the user, before section 1's Docker install"
        )

    agent_at = on.index("--- DATADOG AGENT")
    on_first_useradd = on.index("if ! id -u agnes-applier")
    assert on_first_useradd < agent_at, (
        "the applier's uid reservation must run before the Datadog agent "
        "block, or the agent's own dd-agent user can steal the pinned uid first"
    )


def test_nothing_executable_precedes_the_uid_reservation(on: str, off: str):
    """The reservation's guarantee is "before ANY package activity", and the
    relative anchors above cannot carry it alone: a future `apt-get install`
    (or a `curl | sh`, or another useradd) inserted ABOVE section 0 would
    leave every before-Docker / before-Datadog comparison true while
    re-opening the exact race the reservation exists to close — any
    package's postinst can allocate a system uid, and the top free one is
    the uid the applier pins. So pin the invariant itself: between the top
    of the script and the reservation's `if`, the only executable lines are
    the fixed prelude — the shell options, the log redirect and its chmod,
    plain variable assignments (no command substitution), and the banner.
    """
    prelude_allowed = (
        re.compile(r"^#"),  # comments, including the shebang
        re.compile(r"^\s*$"),  # blank lines
        re.compile(r"^set -euo pipefail$"),
        re.compile(r"^exec > /var/log/agnes-startup\.log 2>&1$"),
        re.compile(r"^chmod 640 /var/log/agnes-startup\.log"),
        # Plain assignments only — `$(` or a backtick would smuggle a
        # command into what this whitelist treats as inert.
        re.compile(r"^[A-Z_][A-Z_0-9]*=(?!.*\$\()(?!.*`).*$"),
        re.compile(r'^echo "=== \[Agnes '),
    )
    for label, body in (("on", on), ("off", off)):
        reservation_at = body.index("if ! id -u agnes-applier")
        offenders = [
            line for line in body[:reservation_at].splitlines() if not any(rx.match(line) for rx in prelude_allowed)
        ]
        assert not offenders, (
            f"[{label}] executable statement(s) before the uid reservation — anything "
            f"running earlier can allocate the pinned uid first: {offenders!r}"
        )


def test_datadog_pre_creates_dd_agent_at_its_own_pinned_uid(on: str, off: str):
    """The second, order-independent guard: dd-agent gets a FIXED uid
    distinct from $AGNES_APPLIER_UID, so even a future reorder that put the
    Datadog block ahead of section 0 again could not hand it uid 999."""
    assert "DATADOG_DD_AGENT_UID=998" in on
    assert "useradd --system --no-create-home --home-dir /opt/datadog-agent" in on
    assert '--uid "$DATADOG_DD_AGENT_UID" --user-group dd-agent' in on

    # The pre-creation must run before the apt install that would otherwise
    # let the package's own postinst create dd-agent unpinned.
    precreate_at = on.index('--uid "$DATADOG_DD_AGENT_UID"')
    apt_install_at = on.index("DEBIAN_FRONTEND=noninteractive apt-get install")
    assert precreate_at < apt_install_at

    # A disabled render carries none of the CODE — same posture as every
    # other Datadog-only artifact
    # (test_the_off_path_is_the_only_datadog_text_in_a_disabled_render).
    # Section 0's own comment forward-references the variable name
    # regardless of enable_datadog (it documents the second guard for a
    # reader looking at section 0 alone), so only comment lines may still
    # mention it in the disabled render.
    off_code_lines = [ln for ln in off.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("DATADOG_DD_AGENT_UID" in ln for ln in off_code_lines)
    assert not any("dd-agent" in ln for ln in off_code_lines)


def test_the_uid_mismatch_guard_fails_loudly_and_falls_back_to_group_read(on: str, off: str):
    """A residual uid collision (something other than Datadog already holds
    $AGNES_APPLIER_UID) must be loud, and must not leave instance.yaml at
    whatever mode it already had — carrying over a stale 0600 from a
    previous good boot onto a now-mismatched owner is the exact failure the
    reservation fix (above) exists to prevent one layer up; this is the
    fallback for every other way the uid could still be taken. Present in
    both renders — this guard has nothing to do with enable_datadog."""
    for label, body in (("on", on), ("off", off)):
        assert 'echo "ERROR: agnes-applier is uid $APPLIER_UID, not $AGNES_APPLIER_UID' in body, label
        assert 'getent passwd "$AGNES_APPLIER_UID"' in body, label
        assert 'chown ":$AGNES_APPLIER_UID" "$INSTANCE_YAML"' in body, label
        assert 'chmod 640 "$INSTANCE_YAML"' in body, label
        assert 'chmod 644 "$INSTANCE_YAML"' in body, label


def test_the_rendered_script_stays_well_inside_the_gce_metadata_limit():
    """Everything the module ships to a VM rides in ONE metadata value, and GCE
    caps a single value at 256 KiB. Each new base64 artifact eats into that, and
    the failure mode is an apply that suddenly refuses a VM — so keep the
    headroom visible rather than discovering it at the ceiling."""
    rendered = _render(True)
    # The fixture stubs the artifact payloads; charge the real ones instead.
    real = sum(len(base64.b64encode(p.read_bytes())) for p in (MODULE / "files/datadog").rglob("*") if p.is_file())
    size = len(rendered.encode()) - sum(len(v) for v in DATADOG_FILES.values()) + real
    assert size < 200_000, f"{size} bytes leaves too little of the 262144-byte budget"
