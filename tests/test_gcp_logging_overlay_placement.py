"""Static contract for placing `docker-compose.gcp-logging.yml` on a VM.

Before this fix, the overlay's own header claimed "the customer infra
Terraform startup script" places the file, but nothing in
`infra/modules/customer-instance/` actually did — the resolver
(`scripts/ops/agnes-compose-file.sh::agnes_resolve_compose_file`) only
appends it when the file is physically present on disk, and it never was.

The real mechanism, pinned here:

* `Dockerfile` bakes the overlay into `/opt/agnes-host/` alongside every
  other host artifact (same contract as `docker-compose.tls.yml` etc.).
* `startup-script.sh.tpl`'s existing recursive
  `docker cp .../opt/agnes-host/. $APP_DIR/` extracts it onto every VM
  unconditionally.
* A new per-module Terraform variable, `enable_gcp_logging` (default
  **true**), gates whether the script then REMOVES the extracted file —
  removal is what turns the resolver's file-presence check off for VMs that
  opt out.

Hardened after the 2026-08-25 prod outage (#1557/#1558): the driver
authenticates as the VM service account, the module granted it no logging
role, and Docker refuses to START a container whose log driver cannot
initialize — so the first container recreate with the overlay armed took
the instance down for 9 minutes. Two additional contracts are pinned here:

* `main.tf` grants `roles/logging.logWriter` to the VM SA, gated on the
  same `enable_gcp_logging` variable that arms the driver.
* Engagement requires file presence AND a driver-probe marker
  (`.gcp-logging-ok`), written only after `agnes_gcp_logging_probe`
  proves the driver initializes — one shared gate
  (`agnes_gcp_logging_active`) used by the boot startup script, the
  auto-upgrade tick, and the resolver, so boot and the recurring ticks can
  never disagree about the overlay and a missing IAM role degrades to
  "logging off + loud warning" instead of an outage.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest


MODULE = Path("infra/modules/customer-instance")
OVERLAY = "docker-compose.gcp-logging.yml"
MARKER = ".gcp-logging-ok"
RESOLVER = Path("scripts/ops/agnes-compose-file.sh")
AUTO_UPGRADE = Path("scripts/ops/agnes-auto-upgrade.sh")


def test_dockerfile_ships_the_overlay_to_agnes_host():
    body = Path("Dockerfile").read_text()
    start = body.index("RUN mkdir -p /opt/agnes-host")
    end = body.index("\n\n", start)
    block = body[start:end]
    assert OVERLAY in block, (
        f"Dockerfile must bake {OVERLAY} into /opt/agnes-host/ (inside the RUN "
        "block that copies every other host artifact) so the startup script "
        "can extract it via `docker cp`"
    )


def test_variables_tf_declares_enable_gcp_logging_default_true():
    body = (MODULE / "variables.tf").read_text()
    m = re.search(r'variable\s+"enable_gcp_logging"\s*\{([^}]*)\}', body, re.DOTALL)
    assert m, 'variables.tf must declare variable "enable_gcp_logging"'
    block = m.group(1)
    assert re.search(r"type\s*=\s*bool", block)
    assert re.search(r"default\s*=\s*true", block)
    assert "description" in block


def test_main_tf_forwards_enable_gcp_logging_into_templatefile():
    body = (MODULE / "main.tf").read_text()
    assert re.search(r"enable_gcp_logging\s*=\s*var\.enable_gcp_logging", body), (
        "main.tf must forward var.enable_gcp_logging into templatefile(...)"
    )


def test_tpl_gates_overlay_placement_on_the_tf_var():
    body = (MODULE / "startup-script.sh.tpl").read_text()
    assert OVERLAY in body, "startup-script.sh.tpl must reference the overlay filename"
    assert "%{ if !enable_gcp_logging ~}" in body, (
        "the overlay's placement must be gated on the enable_gcp_logging TF var "
        "(the recursive docker cp extracts it unconditionally; disabling the "
        "var must remove it again)"
    )
    guard = body.index("%{ if !enable_gcp_logging ~}")
    endif = body.index("%{ endif ~}", guard)
    gated_block = body[guard:endif]
    assert OVERLAY in gated_block, "the gated block must act on the overlay file"
    assert "rm -f" in gated_block or "rm " in gated_block


def test_tpl_placement_runs_after_the_extraction_that_ships_it():
    """The gate must run AFTER the recursive `docker cp .../opt/agnes-host/.`
    that actually puts the file on disk — gating before it would be a no-op."""
    body = (MODULE / "startup-script.sh.tpl").read_text()
    extract_idx = body.index('docker cp "$EXTRACT_CONTAINER:/opt/agnes-host/." "$APP_DIR/"')
    gate_idx = body.index("%{ if !enable_gcp_logging ~}")
    assert extract_idx < gate_idx


def test_overlay_header_describes_the_real_mechanism():
    text = Path(OVERLAY).read_text()
    assert "Dockerfile" in text
    assert "enable_gcp_logging" in text
    # The old, inaccurate claim ("the customer infra Terraform startup
    # script... runs exclusively on GCE" places the file) must be gone.
    assert "runs exclusively on GCE" not in text
    # The overlay header claimed "The VM service account already carries
    # roles/logging.logWriter, so no IAM change is required" — false (the
    # module granted no logging role at all), and exactly the belief that
    # let #1557 ship. It must never come back.
    assert "no IAM change is required" not in text


# --- #1557: the IAM half — the module must grant the role it depends on ---


def test_main_tf_grants_log_writer_to_the_vm_sa_gated_on_the_flag():
    body = (MODULE / "main.tf").read_text()
    m = re.search(
        r'resource\s+"google_project_iam_member"\s+"vm_log_writer"\s*\{([^}]*)\}',
        body,
        re.DOTALL,
    )
    assert m, (
        "main.tf must declare google_project_iam_member.vm_log_writer — the "
        "gcplogs driver authenticates as the VM SA, and without "
        "roles/logging.logWriter Docker refuses to start every container the "
        "overlay covers (#1557)"
    )
    block = m.group(1)
    assert "roles/logging.logWriter" in block
    assert "google_service_account.vm.email" in block, (
        "the binding must target the dedicated VM SA the compute instance "
        "actually runs as (service_account block in main.tf)"
    )
    assert re.search(r"count\s*=\s*var\.enable_gcp_logging\s*\?\s*1\s*:\s*0", block), (
        "the binding must be gated on the same variable that arms the driver"
    )
    assert re.search(r"project\s*=\s*var\.gcp_project_id", block)


def test_variables_tf_documents_the_iam_requirement():
    body = (MODULE / "variables.tf").read_text()
    m = re.search(r'variable\s+"enable_gcp_logging"\s*\{([^}]*)\}', body, re.DOTALL)
    assert m
    block = m.group(1)
    assert "logging.logWriter" in block, (
        "enable_gcp_logging's description must state the IAM role the driver "
        "needs (and that the module grants it) — the missing-role failure "
        "mode was invisible precisely because the description only mentioned "
        "'GCE metadata-server credentials'"
    )


# --- #1557/#1558: the runtime half — one shared gate, probe-armed marker ---


def _sh(script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _resolve(compose_dir: Path, state_dir: Path) -> str:
    result = _sh(f'. "{RESOLVER.resolve()}" && agnes_resolve_compose_file "{compose_dir}" "{state_dir}"')
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture()
def compose_dirs(tmp_path):
    compose_dir = tmp_path / "opt-agnes"
    compose_dir.mkdir()
    (compose_dir / OVERLAY).write_text("services: {}\n")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return compose_dir, state_dir


def _fake_docker(tmp_path: Path, exit_code: int) -> dict[str, str]:
    """A PATH with a stub `docker` that records its argv and exits as told."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "docker"
    stub.write_text(f'#!/bin/sh\necho "$@" >> "{tmp_path}/docker-calls.log"\nexit {exit_code}\n')
    stub.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return env


class TestResolverGate:
    """`agnes_resolve_compose_file` requires the probe marker, not mere
    file presence — presence alone is what armed an unauthorized driver and
    took prod down on a cron tick (#1557)."""

    def test_overlay_present_without_marker_is_not_engaged(self, compose_dirs):
        compose_dir, state_dir = compose_dirs
        assert OVERLAY not in _resolve(compose_dir, state_dir)

    def test_overlay_present_with_marker_is_engaged(self, compose_dirs):
        compose_dir, state_dir = compose_dirs
        (compose_dir / MARKER).touch()
        assert _resolve(compose_dir, state_dir).endswith(f":{OVERLAY}")

    def test_marker_without_overlay_file_is_not_engaged(self, compose_dirs):
        compose_dir, state_dir = compose_dirs
        (compose_dir / OVERLAY).unlink()
        (compose_dir / MARKER).touch()
        assert OVERLAY not in _resolve(compose_dir, state_dir)


class TestDriverProbe:
    """`agnes_gcp_logging_probe` arms/clears the marker by actually starting
    a no-op container on the gcplogs driver."""

    def test_probe_success_arms_the_marker_and_the_resolver_engages(self, compose_dirs, tmp_path):
        compose_dir, state_dir = compose_dirs
        env = _fake_docker(tmp_path, exit_code=0)
        result = _sh(
            f'. "{RESOLVER.resolve()}" && agnes_gcp_logging_probe "{compose_dir}" "example/image:tag"',
            env=env,
        )
        assert result.returncode == 0, result.stderr
        assert (compose_dir / MARKER).exists()
        calls = (tmp_path / "docker-calls.log").read_text()
        assert "--log-driver=gcplogs" in calls, (
            "the probe must exercise the actual gcplogs driver — that is where an unauthorized VM SA fails"
        )
        assert OVERLAY in _resolve(compose_dir, state_dir)

    def test_probe_failure_clears_a_stale_marker_and_the_overlay_drops(self, compose_dirs, tmp_path):
        compose_dir, state_dir = compose_dirs
        (compose_dir / MARKER).touch()
        env = _fake_docker(tmp_path, exit_code=1)
        result = _sh(
            f'. "{RESOLVER.resolve()}" && agnes_gcp_logging_probe "{compose_dir}" "example/image:tag"',
            env=env,
        )
        assert result.returncode != 0
        assert not (compose_dir / MARKER).exists()
        assert OVERLAY not in _resolve(compose_dir, state_dir)

    def test_probe_without_the_overlay_file_disarms_without_running_docker(self, compose_dirs, tmp_path):
        compose_dir, _ = compose_dirs
        (compose_dir / OVERLAY).unlink()
        (compose_dir / MARKER).touch()
        env = _fake_docker(tmp_path, exit_code=0)
        result = _sh(
            f'. "{RESOLVER.resolve()}" && agnes_gcp_logging_probe "{compose_dir}" "example/image:tag"',
            env=env,
        )
        assert result.returncode != 0
        assert not (compose_dir / MARKER).exists()
        assert not (tmp_path / "docker-calls.log").exists(), (
            "a deliberately removed overlay (enable_gcp_logging=false) must "
            "disarm the gate without spending a docker run"
        )


class TestBootPathUsesTheSharedGate:
    """#1558: the startup script must not keep a private presence check that
    can disagree with the resolver's gate."""

    def test_tpl_probes_the_driver_after_extraction(self):
        body = (MODULE / "startup-script.sh.tpl").read_text()
        assert '. "$APP_DIR/scripts/ops/agnes-compose-file.sh"' in body, (
            "the boot path must source the shared resolver rather than re-implementing the gate"
        )
        assert 'if [ -f "$APP_DIR/scripts/ops/agnes-compose-file.sh" ]; then' in body, (
            "the source must be presence-guarded: with AGNES_TAG pinned to "
            "an image predating the resolver, a bare `.` under set -e would "
            "fail the whole boot over a logging add-on"
        )
        extract_idx = body.index('docker cp "$EXTRACT_CONTAINER:/opt/agnes-host/." "$APP_DIR/"')
        gate_idx = body.index("%{ if !enable_gcp_logging ~}")
        probe_idx = body.index("agnes_gcp_logging_probe")
        assert extract_idx < gate_idx < probe_idx, (
            "the probe must run after the extraction that ships the overlay "
            "AND after the enable_gcp_logging removal gate — probing a file "
            "the gate is about to remove would arm a marker for nothing"
        )

    def test_tpl_compose_file_append_requires_the_shared_gate(self):
        body = (MODULE / "startup-script.sh.tpl").read_text()
        append = body.index('COMPOSE_FILE_VALUE="$COMPOSE_FILE_VALUE:' + OVERLAY)
        gate = body.rindex('if agnes_gcp_logging_active "$APP_DIR"; then', 0, append)
        assert body[gate:append].count("\n") <= 1, (
            "the boot-time COMPOSE_FILE append must be guarded by "
            "agnes_gcp_logging_active (file presence + probe marker), the "
            "same gate the resolver applies"
        )
        assert f'if [ -f "$APP_DIR/{OVERLAY}" ]; then\n    COMPOSE_FILE_VALUE' not in body, (
            "the old bare file-presence append must be gone — it is the boot "
            "half of the boot/resolver divergence from #1558"
        )


class TestAutoUpgradeTickConverges:
    """The tick re-probes a marker-less overlay so a VM converges (either
    direction) within 5 minutes, and hashes the marker so the transition
    triggers the recreate that actually switches log drivers."""

    def test_tick_probes_only_when_the_marker_is_absent(self):
        body = AUTO_UPGRADE.read_text()
        probe_idx = body.index("agnes_gcp_logging_probe /opt/agnes")
        guard = body.rindex(f"[ ! -f /opt/agnes/{MARKER} ]", 0, probe_idx)
        assert probe_idx - guard < 200, (
            "the tick must probe only a marker-less overlay — re-probing "
            "every tick would let one metadata blip churn two recreates"
        )

    def test_tick_probes_after_sourcing_the_resolver_and_before_hashing(self):
        body = AUTO_UPGRADE.read_text()
        source_idx = body.index('. "$RESOLVER"')
        probe_idx = body.index("agnes_gcp_logging_probe /opt/agnes")
        hash_idx = body.index("CONFIG_AFTER=$(hash_config_files)")
        assert source_idx < probe_idx < hash_idx, (
            "the probe needs the resolver's functions, and must run before "
            "hash_config_files so arming the marker counts as config drift "
            "on the SAME tick (the recreate is what moves containers between "
            "log drivers)"
        )

    def test_hash_config_files_covers_the_marker(self):
        body = AUTO_UPGRADE.read_text()
        assert f'"${{CONFIG_FILES[@]}}" {OVERLAY} {MARKER}; do' in body, (
            "hash_config_files must hash the probe marker alongside the "
            "overlay so an arm/disarm transition triggers a recreate instead "
            "of waiting for an unrelated change"
        )
