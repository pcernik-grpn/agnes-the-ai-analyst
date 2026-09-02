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

import contextlib
import socket
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


@contextlib.contextmanager
def _collector_listening(port: int = 24224):
    """A real listener on the Ops Agent's forward port, or a skip.

    The probe asks one question — is anything accepting these logs — so the
    honest test is a socket, not a stubbed binary.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError as exc:  # something already owns it on this machine
        sock.close()
        pytest.skip(f"127.0.0.1:{port} is not bindable here ({exc})")
    sock.listen(1)
    try:
        yield
    finally:
        sock.close()


class TestCollectorProbe:
    """`agnes_gcp_logging_probe` arms/clears the marker by asking whether the
    Ops Agent is actually receiving on its forward port.

    It used to start a no-op container on the gcplogs driver, because that
    driver refuses to initialize without credentials and Docker then refuses
    to start the container (#1557). The overlay forwards asynchronously now,
    so a missing collector can no longer stop a container — what is left to
    catch is the quiet failure of buffering every line into a socket nobody
    is listening on.
    """

    def test_probe_arms_the_marker_when_the_collector_is_up(self, compose_dirs):
        compose_dir, state_dir = compose_dirs
        with _collector_listening():
            result = _sh(f'. "{RESOLVER.resolve()}" && agnes_gcp_logging_probe "{compose_dir}" "example/image:tag"')
        assert result.returncode == 0, result.stderr
        assert (compose_dir / MARKER).exists()
        assert OVERLAY in _resolve(compose_dir, state_dir)

    def test_probe_failure_clears_a_stale_marker_and_the_overlay_drops(self, compose_dirs):
        compose_dir, state_dir = compose_dirs
        (compose_dir / MARKER).touch()
        # Nothing listening: whatever this host has on 24224, the probe must
        # not arm on a stale marker alone.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", 24224)) == 0:
                pytest.skip("something is listening on 127.0.0.1:24224 on this host")
        result = _sh(f'. "{RESOLVER.resolve()}" && agnes_gcp_logging_probe "{compose_dir}" "example/image:tag"')
        assert result.returncode != 0
        assert not (compose_dir / MARKER).exists()
        assert OVERLAY not in _resolve(compose_dir, state_dir)

    def test_probe_without_the_overlay_file_disarms_without_probing(self, compose_dirs):
        compose_dir, _ = compose_dirs
        (compose_dir / OVERLAY).unlink()
        (compose_dir / MARKER).touch()
        with _collector_listening():
            result = _sh(f'. "{RESOLVER.resolve()}" && agnes_gcp_logging_probe "{compose_dir}" "example/image:tag"')
        assert result.returncode != 0, (
            "a deliberately removed overlay (enable_gcp_logging=false) must disarm "
            "the gate even while the collector is up"
        )
        assert not (compose_dir / MARKER).exists()


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


class TestAProbeVerdictBelongsToOnePipeline:
    """A refreshed overlay must invalidate the marker that armed the old one.

    The live failure (2026-09-02): the tick refreshes
    `docker-compose.gcp-logging.yml` in place from the pinned image, so a
    running VM picked up the fluentd/Ops-Agent overlay within five minutes of
    the switch landing in `:stable`. Its `.gcp-logging-ok` marker was still
    the one the OLD gcplogs probe had written, and the tick re-probes only a
    marker-LESS overlay — so the gate engaged a pipeline nothing had ever
    verified. Async forwarding meant no outage; it meant every log line went
    to a socket with no listener, on a VM whose Ops Agent arrives only with a
    later Terraform apply. Silent total log loss, which is the failure mode
    the marker exists to prevent.

    A probe verdict is about one pipeline. Change the pipeline and the
    verdict is stale, not inherited.
    """

    def test_a_refreshed_overlay_clears_the_marker(self):
        body = AUTO_UPGRADE.read_text()
        refresh_idx = body.index("extract_host_artifact docker-compose.gcp-logging.yml")
        probe_idx = body.index("agnes_gcp_logging_probe /opt/agnes")
        window = body[refresh_idx:probe_idx]
        assert f"rm -f /opt/agnes/{MARKER}" in window, (
            "refreshing the overlay must drop the marker that armed the "
            "previous one, so the marker-less probe below re-runs against "
            "the pipeline that is now actually on disk"
        )

    def test_the_marker_is_cleared_only_when_the_overlay_really_changed(self):
        """Not on every tick — an unconditional clear would re-probe forever
        and, because the marker is hashed, churn a recreate every five
        minutes."""
        body = AUTO_UPGRADE.read_text()
        refresh_idx = body.index("extract_host_artifact docker-compose.gcp-logging.yml")
        probe_idx = body.index("agnes_gcp_logging_probe /opt/agnes")
        window = body[refresh_idx:probe_idx]
        assert "sha256sum" in window or "cmp -s" in window, (
            "the clear must be conditional on the overlay's content actually "
            "changing, not fire on every refresh"
        )


class TestTheTickNamesTheRightCause:
    """A guard that misreports its own cause sends the operator to the wrong
    fix. The tick's messages described the gcplogs driver and pointed at
    roles/logging.logWriter; the probe now asks whether the Ops Agent is
    accepting on its forward port, which that role has nothing to do with."""

    def test_no_message_blames_the_log_writer_role(self):
        body = AUTO_UPGRADE.read_text()
        for line in body.splitlines():
            if "logger -t agnes-auto-upgrade" in line and "logging.logWriter" in line:
                raise AssertionError(f"message names a cause the probe no longer tests: {line.strip()}")

    def test_the_probe_messages_name_the_collector(self):
        body = AUTO_UPGRADE.read_text()
        probe_idx = body.index("agnes_gcp_logging_probe /opt/agnes")
        window = body[probe_idx : probe_idx + 900]
        assert "24224" in window or "Ops Agent" in window, (
            "the arm/disarm messages must name what was actually probed"
        )


class TestTheTickCanAlwaysDeliverItsOwnFix:
    """The self-update must not sit behind an early exit.

    Observed on the fleet 2026-09-02: a long-running data refresh made every
    tick take the `sync/refresh in flight — deferring recreate` branch, which
    `exit 0`s. Host artifacts are refreshed well before that point, so the VM
    took the new Cloud Logging overlay; the script's own self-update sits
    after it, so the fix for that overlay could not arrive — for over four
    hours, and for as long as the refresh kept running. The first tick that
    finally gets through then recreates containers on the un-fixed logic and
    only self-updates afterwards.

    That is the "self-perpetuating old script" problem the self-update block
    exists to prevent, reintroduced by ordering. A tick must be able to
    deliver its own replacement on any path that reached the image.
    """

    def test_self_update_precedes_the_deferral_exit(self):
        body = AUTO_UPGRADE.read_text()
        self_update = body.index("agnes-auto-upgrade.sh.new")
        defer_exit = body.index("deferring recreate")
        assert self_update < defer_exit, (
            "the self-update must run before the deferral's `exit 0` — behind "
            "it, a VM that defers every tick can never receive a fixed script"
        )

    def test_self_update_follows_the_artifact_extraction(self):
        """It needs the extract container the refresh block creates."""
        body = AUTO_UPGRADE.read_text()
        extract = body.index("EXTRACT_CID=")
        self_update = body.index("agnes-auto-upgrade.sh.new")
        assert extract < self_update


class TestOverlayCoversEveryBaseComposeService:
    """The overlay's service list must track `docker-compose.yml`, both ways.

    It did not. The list was written in #679 against the compose file of the
    day and never revisited; `extraction-worker` (added later by the
    three-plane wave-1 topology), `apps-runner`, `egress-proxy` and
    `kai-agent-stub` all arrived afterwards and silently stayed on
    `json-file`. Verified live on a customer VM: `app`, `scheduler` and
    `caddy` shipped to Cloud Logging while `agnes-extraction-worker-1` —
    the process that runs the connector crawls, i.e. the logs an operator
    actually goes looking for — did not, and its history died with every
    auto-upgrade container recreate.

    The near-miss in the same list is `extract` vs `extraction-worker`:
    two different services (a one-shot Keboola extractor under the
    `extract` profile, and the long-running extraction lane), one of which
    was present and not running while the other ran and was absent.

    The reverse direction is the safety half, and it is the sharper of the
    two. A compose file that names a service without an `image:`/`build:`
    is invalid, and this overlay is engaged from file presence alone —
    independently of which OTHER overlays a given VM loads. Naming
    `redis` / `postgres` / `kai-agent` here (each defined only in an
    overlay that some instances do not load: the module-written
    `docker-compose.extraction.yml`, `docker-compose.postgres.yml` on
    side-car backends only, `docker-compose.kai-agent.yml`) would make
    every `docker compose` call on the instances that lack it fail to
    parse — the fleet-freeze shape of #1557 arriving through a different
    door. Those services need their log driver set in the overlay that
    defines them, not here.
    """

    @staticmethod
    def _services(path: str) -> dict:
        import yaml

        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return doc["services"]

    def test_base_compose_services_are_discovered(self):
        """Guard the guard: an empty base set would pass the check below."""
        base = self._services("docker-compose.yml")
        assert len(base) >= 8, f"expected the full base service list, got {sorted(base)}"
        assert "extraction-worker" in base

    def test_every_base_service_ships_to_cloud_logging(self):
        base = set(self._services("docker-compose.yml"))
        overlay = set(self._services(OVERLAY))
        missing = sorted(base - overlay)
        assert not missing, (
            f"{OVERLAY} does not cover {missing} — these services are defined in "
            "docker-compose.yml but keep the default json-file driver, so their "
            "logs never leave the VM and do not survive a container recreate. "
            "Add a `logging: driver: gcplogs` entry for each."
        )

    def test_overlay_names_no_service_the_base_compose_lacks(self):
        base = set(self._services("docker-compose.yml"))
        overlay = set(self._services(OVERLAY))
        extra = sorted(overlay - base)
        assert not extra, (
            f"{OVERLAY} names {extra}, which docker-compose.yml does not define. "
            "This overlay is engaged from file presence alone, so on any instance "
            "whose COMPOSE_FILE lacks the overlay that DOES define such a service, "
            "compose sees a service with no image/build and refuses to parse the "
            "whole stack — every docker compose call on that VM fails. Set the log "
            "driver in the overlay that defines the service instead."
        )

    def test_every_overlay_entry_only_sets_the_log_driver(self):
        for name, spec in self._services(OVERLAY).items():
            assert set(spec) == {"logging"}, (
                f"{OVERLAY}: service {name!r} must carry a logging block and "
                f"nothing else, got {sorted(spec)} — this file is a log-driver "
                "overlay, not a place to override service config"
            )
            assert spec["logging"]["driver"] == "fluentd", (
                f"{OVERLAY}: service {name!r} is not forwarding to the Ops Agent"
            )
