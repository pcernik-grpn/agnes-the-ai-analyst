"""Startup hardening: block hosted data-app containers from the cloud metadata
server (169.254.169.254) at the host firewall.

A data app runs user-authored code — RCE inside its own container is by design
— and on a plain bridge network it can otherwise read the VM's service-account
token from the metadata server and pivot to the whole cloud project. The
DOCKER-USER DROP rule is host-enforced, so a compromised container cannot undo
it. It is SOURCE-scoped to the agnes-apps bridge subnet, never a blanket block,
so the Agnes app container's own metadata access (e.g. BigQuery GCE-metadata
auth, via its default-network interface) is untouched.

Same marker-delimited-block pattern as ``test_daemon_json_rotation.py`` /
``test_startup_vault_key.py`` — the functional tests execute the real shipped
shell against fake ``docker``/``iptables`` on PATH, never a re-implementation.
"""

import re
import shutil
import stat
import subprocess
from pathlib import Path

TPL = Path("infra/modules/customer-instance/startup-script.sh.tpl")

BEGIN = "# --- container-metadata-hardening begin"
END = "# --- container-metadata-hardening end"

METADATA_IP = "169.254.169.254"
FAKE_SUBNET = "172.30.0.0/16"


def _block() -> str:
    tpl = TPL.read_text()
    m = re.search(re.escape(BEGIN) + r".*?\n(.*?)" + re.escape(END), tpl, re.DOTALL)
    assert m, (
        "startup-script.sh.tpl must contain the marker-delimited "
        f"container-metadata-hardening block ({BEGIN!r} ... {END!r}) — the "
        "functional tests below execute it"
    )
    block = m.group(1)
    assert "${" not in block and "%{" not in block, (
        "container-metadata-hardening block must not use Terraform interpolation "
        "('${' / '%{'); keep it plain bash so tests execute the shipped code verbatim"
    )
    return block


def _write_fake(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text("#!/usr/bin/env bash\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run(
    tmp_path: Path,
    *,
    with_iptables: bool = True,
    rule_already_present: bool = False,
    subnet_output: str | None = None,
) -> subprocess.CompletedProcess:
    """Execute the shipped block with fake docker/iptables on PATH.

    Fakes record their invocations to files under ``tmp_path`` so a test can
    assert exactly which iptables rule was installed.
    """
    bash = shutil.which("bash")
    assert bash, "bash required"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    iptables_log = tmp_path / "iptables.log"

    # docker: `network create` -> ok; `network inspect` -> JSON with a Subnet.
    inspect_json = subnet_output if subnet_output is not None else f'  "Subnet": "{FAKE_SUBNET}"\n'
    _write_fake(
        bin_dir,
        "docker",
        'if [ "$1" = "network" ] && [ "$2" = "inspect" ]; then\n'
        f"  cat <<'JSON'\n{inspect_json}\nJSON\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
    )

    if with_iptables:
        # `-C` (check) exit code drives the idempotency branch; `-I` (insert) is
        # logged. `$*` captures the full argv so the test can assert the rule shape.
        check_rc = "0" if rule_already_present else "1"
        _write_fake(
            bin_dir,
            "iptables",
            f'echo "$*" >> "{iptables_log}"\ncase "$1" in\n  -C) exit {check_rc};;\n  -I) exit 0;;\nesac\nexit 0\n',
        )

    # PATH: fakes first, then the real toolchain (grep/sed/etc.). Drop the real
    # iptables in the no-iptables case by NOT shadowing it AND relying on the
    # block's `command -v iptables` — so point PATH at a dir without iptables.
    import os

    if with_iptables:
        path = f"{bin_dir}:{os.environ['PATH']}"
    else:
        # A minimal PATH that has the coreutils the block needs but no iptables.
        # Real hosts always have iptables; this exercises the fail-soft warning.
        real = shutil.which("grep")
        coreutils_dir = str(Path(real).parent) if real else "/usr/bin:/bin"
        path = f"{bin_dir}:{coreutils_dir}"

    script = "set -euo pipefail\n" + _block()
    return subprocess.run(
        [bash, "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": path},
    )


# ---------------------------------------------------------------------------
# Static guarantees on the shipped block (a future edit can't silently weaken)
# ---------------------------------------------------------------------------


def test_block_targets_docker_user_and_metadata_ip():
    block = _block()
    assert "DOCKER-USER" in block
    assert f'METADATA_IP="{METADATA_IP}"' in block
    assert '-d "$METADATA_IP/32"' in block  # /32 host route — the metadata IP only


def test_block_is_source_scoped_not_a_blanket_block():
    """The DROP must be SOURCE-scoped to the app bridge subnet — a blanket
    `-d metadata -j DROP` would also cut the Agnes app's own metadata access
    (BigQuery GCE-metadata auth)."""
    block = _block()
    assert '-s "$subnet"' in block, "metadata DROP must be scoped to the agnes-apps source subnet"
    assert "agnes-apps" in block


def test_block_is_gated_on_data_apps_enabled():
    tpl = TPL.read_text()
    # Immediately wrapped by `%{ if data_apps_enabled ~}` ... `%{ endif ~}`
    # (the tpl has several data_apps_enabled guards, so check THIS block's own
    # wrapping): hardening is only relevant when hosted apps can run.
    assert "%{ if data_apps_enabled ~}\n" + BEGIN in tpl
    assert END + " ---\n%{ endif ~}" in tpl


# ---------------------------------------------------------------------------
# Functional: execute the real block against fake docker/iptables
# ---------------------------------------------------------------------------


def test_installs_drop_rule_when_absent(tmp_path):
    proc = _run(tmp_path, rule_already_present=False)
    assert proc.returncode == 0, proc.stderr
    log = (tmp_path / "iptables.log").read_text()
    assert f"-I DOCKER-USER -s {FAKE_SUBNET} -d {METADATA_IP}/32 -j DROP" in log


def test_idempotent_when_rule_present(tmp_path):
    proc = _run(tmp_path, rule_already_present=True)
    assert proc.returncode == 0, proc.stderr
    log = (tmp_path / "iptables.log").read_text()
    assert "-C DOCKER-USER" in log  # it checked
    assert "-I DOCKER-USER" not in log  # ...and did not re-insert


def test_no_iptables_is_fail_soft(tmp_path):
    proc = _run(tmp_path, with_iptables=False)
    assert proc.returncode == 0, proc.stderr  # never fails the boot
    assert "iptables not found" in proc.stderr


def test_unresolvable_subnet_is_fail_soft(tmp_path):
    proc = _run(tmp_path, subnet_output="no subnet here")
    assert proc.returncode == 0, proc.stderr
    assert "could not resolve" in proc.stderr
    assert not (tmp_path / "iptables.log").exists() or "-I DOCKER-USER" not in (tmp_path / "iptables.log").read_text()
