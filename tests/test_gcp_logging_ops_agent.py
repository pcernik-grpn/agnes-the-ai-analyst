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
    assert any(r.get("type") == "fluent_forward" for r in receivers.values()), receivers

    processors = logging_cfg["processors"]
    assert any(p.get("type") == "parse_json" for p in processors.values()), processors
    lifts_severity = any(
        "severity" in (p.get("fields") or {}) for p in processors.values() if p.get("type") == "modify_fields"
    )
    assert lifts_severity, "nothing maps the app's `severity` field onto the entry's severity"

    (pipeline,) = logging_cfg["service"]["pipelines"].values()
    assert set(pipeline["receivers"]) <= set(receivers)
    assert set(pipeline["processors"]) <= set(processors)


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
