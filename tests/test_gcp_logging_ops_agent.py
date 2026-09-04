"""Contract for shipping Agnes' structured logs into Cloud Logging.

Docker's built-in `gcplogs` driver parses nothing: it forwards each line as
an opaque string, so every entry arrives at severity DEFAULT and the JSON the
app writes is filterable by substring only. The pipeline is therefore
fluentd-forward into the Ops Agent, which parses the JSON and lifts
`severity` out of it.

Two hazards this file pins, both learned the hard way (#1557):

* Docker refuses to start a container whose log driver cannot initialize.
  The fluentd driver must run in async mode so a collector that is down or
  still booting costs log lines, never container starts.
* Engagement stays gated on a probe marker, so a VM whose collector never
  came up degrades to "no Cloud Logging + a loud warning".
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
OVERLAY = REPO / "docker-compose.gcp-logging.yml"
RESOLVER = REPO / "scripts/ops/agnes-compose-file.sh"
STARTUP = REPO / "infra/modules/customer-instance/startup-script.sh.tpl"
AGENT_CONFIG = REPO / "infra/modules/customer-instance/files/ops-agent-config.yaml"


def _services() -> dict:
    return yaml.safe_load(OVERLAY.read_text())["services"]


def test_every_service_forwards_to_the_local_collector():
    services = _services()
    assert services, "the overlay must still cover the base compose services"
    for name, spec in services.items():
        logging_spec = spec["logging"]
        assert logging_spec["driver"] == "fluentd", f"{name} is not on the fluentd driver"
        assert logging_spec["options"]["fluentd-address"].startswith("127.0.0.1:"), (
            f"{name} must forward to the collector on loopback, never off-host"
        )


def test_the_driver_is_async_so_a_down_collector_cannot_block_container_starts():
    """The whole reason #1557 was an outage rather than a logging gap."""
    for name, spec in _services().items():
        assert str(spec["logging"]["options"]["fluentd-async"]).lower() == "true", (
            f"{name} would refuse to start while the collector is down"
        )


def test_each_container_is_tagged_so_services_stay_distinguishable():
    for name, spec in _services().items():
        assert "tag" in spec["logging"]["options"], f"{name} has no log tag"


def test_the_probe_checks_the_collector_is_listening():
    """With async forwarding a container starts regardless, so 'does the
    driver initialize' no longer proves anything — the probe has to ask
    whether the collector is actually accepting the logs."""
    sh = RESOLVER.read_text()
    assert "24224" in sh, "the probe does not look at the collector's port"
    assert "--log-driver=gcplogs" not in sh, "still probing the driver this pipeline no longer uses"


def test_the_agent_config_parses_the_json_and_lifts_the_severity():
    assert AGENT_CONFIG.exists(), "the Ops Agent config is what makes the entries structured"
    cfg = yaml.safe_load(AGENT_CONFIG.read_text())
    logging_cfg = cfg["logging"]

    receivers = logging_cfg["receivers"]
    forward = [r for r in receivers.values() if r.get("type") == "fluent_forward"]
    assert forward, receivers
    for r in forward:
        # The agent refuses to start on an unknown field, and a refusal means
        # nothing accepts the containers' logs at all. `port` was rejected on
        # a live VM; these are the names it takes.
        assert "listen_port" in r and "port" not in r, r

    parsers = [p for p in logging_cfg["processors"].values() if p.get("type") == "parse_json"]
    assert parsers, logging_cfg["processors"]
    for parser in parsers:
        # Docker's fluentd driver hands over a record whose fields are its
        # own; the app's JSON is the string in `log`. Parsing the record
        # instead of that field is a silent no-op.
        assert parser.get("field") == "log", parser

    processors = logging_cfg["processors"]
    assert any(p.get("type") == "parse_json" for p in processors.values()), processors
    lifts_severity = any(
        "severity" in (p.get("fields") or {}) for p in processors.values() if p.get("type") == "modify_fields"
    )
    assert lifts_severity, "nothing maps the app's `severity` field onto the entry's severity"

    (pipeline,) = logging_cfg["service"]["pipelines"].values()
    assert set(pipeline["receivers"]) <= set(receivers)
    assert set(pipeline["processors"]) <= set(processors)


def test_the_agent_ships_no_host_metrics_because_datadog_collects_those():
    """Emptying the default metrics pipeline is Google's documented way to
    stop host-metric collection. Cloud Monitoring bills those by ingested
    bytes, and on a VM with `enable_datadog` they are collected twice over.

    This does NOT stop the collector's own agent.googleapis.com/agent/*
    self-metrics — free, and with no off switch — which is what the module
    grants roles/monitoring.metricWriter for; that half is pinned next to the
    logWriter grant in tests/test_gcp_logging_overlay_placement.py.
    """
    metrics_cfg = yaml.safe_load(AGENT_CONFIG.read_text()).get("metrics")
    assert metrics_cfg, "nothing overrides the built-in metrics pipeline"

    pipelines = metrics_cfg["service"]["pipelines"]
    assert "default_pipeline" in pipelines, (
        "the built-in pipeline is only disarmed by redefining it under its own "
        "id — any other id leaves the hostmetrics receiver collecting"
    )
    for name, pipeline in pipelines.items():
        assert pipeline.get("receivers") == [], (
            f"metrics pipeline {name} ships host metrics Datadog already collects"
        )


def test_the_agent_is_installed_only_when_the_feature_is_on():
    tpl = STARTUP.read_text()
    assert "ops-agent" in tpl or "ops_agent" in tpl, "nothing installs the collector"
    assert "enable_gcp_logging" in tpl


def test_installing_the_agent_can_never_fail_the_boot():
    """A logging add-on that bricks provisioning is worse than no logging."""
    tpl = STARTUP.read_text()
    start = tpl.index("OPS AGENT")
    block = tpl[start : start + 4000]
    assert "|| true" in block or "|| echo" in block or "|| {" in block, (
        "the collector install is not failure-tolerant"
    )


def test_the_probe_marker_records_which_overlay_it_verified():
    """A probe verdict belongs to ONE pipeline, so the marker has to say
    which — otherwise a caller cannot tell a current verdict from a stale one.

    `agnes_gcp_logging_probe` therefore writes the overlay's own sha256 into
    `.gcp-logging-ok` rather than touching it empty."""
    src = (Path("scripts/ops") / "agnes-compose-file.sh").read_text(encoding="utf-8")
    arm = src.split("agnes_gcp_logging_probe()", 1)[1].split("\n}", 1)[0]
    assert "sha256sum" in arm and ".gcp-logging-ok" in arm, (
        "the probe must stamp the marker with the overlay it verified, not touch it empty"
    )


def test_a_stale_verdict_is_detected_by_the_marker_stamp_not_by_a_tick_diff():
    """The auto-upgrade script replaces ITSELF at the end of a tick.

    So on the rollout that introduces this check, the PREVIOUS version of the
    script refreshes `docker-compose.gcp-logging.yml` — keeping the marker,
    because it has no clearing logic — and installs the new script. The new
    script's first run, five minutes later, computes its before-hash from an
    overlay that has ALREADY been refreshed: before == after, the branch never
    fires, and the stale verdict survives exactly the upgrade it exists to
    catch (Devin Review on #2057).

    Comparing the marker's own stamp against the overlay on disk has no such
    blind spot: it is a property of the two files, not of who refreshed them
    or when. This pins that the comparison is the stamped form."""
    src = (Path("scripts/ops") / "agnes-auto-upgrade.sh").read_text(encoding="utf-8")
    block = src.split("docker-compose.gcp-logging.yml is placement-driven", 1)[1][:4000]
    assert "GCP_OVERLAY_BEFORE" not in block, (
        "a before/after comparison within one tick cannot fire on the rollout "
        "that introduces it — the previous script does the refresh"
    )
    assert "cat /opt/agnes/.gcp-logging-ok" in block, (
        "the stale check must read the marker's own stamp"
    )
    assert 'rm -f /opt/agnes/.gcp-logging-ok' in block
