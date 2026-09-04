# Shipping logs to Google Cloud Logging

Agnes itself does not know about Cloud Logging: it writes one JSON object per
line to stdout ([`observability.md`](observability.md)) and stops there. This
page describes the pipeline the bundled Terraform module
(`infra/modules/customer-instance/`) wires up on a GCE-hosted instance. Any
other deployment collects stdout however it already does.

## The pipeline

```
container stdout (JSON per line)
        │  Docker fluentd log driver, async, 127.0.0.1:24224
        ▼
Google Cloud Ops Agent  ── parse_json ──> fields
        │               ── modify_fields ──> entry severity
        ▼
Cloud Logging (structured entries)
```

Two files carry it:

| file | role |
|---|---|
| `docker-compose.gcp-logging.yml` | puts every service on the `fluentd` driver, addressed at loopback |
| `infra/modules/customer-instance/files/ops-agent-config.yaml` | the agent's receiver + the two processors that make the entries structured |

## Why not Docker's `gcplogs` driver

It parses nothing. Each line is forwarded as an opaque string, so every entry
arrives at severity `DEFAULT` with the app's JSON sitting in
`jsonPayload.data` as text — filterable by substring only, which is the same
as having no levels at all. The Ops Agent is what turns the line back into
fields and lifts `severity` onto the entry.

## Why the driver is async

**This is a safety property, not a tuning knob.** Docker refuses to *start* a
container whose log driver cannot initialize. On 2026-08-25 an armed
synchronous driver on a VM whose service account lacked
`roles/logging.logWriter` left `app` and `scheduler` stuck in `created` — a
9-minute outage caused entirely by a logging add-on (#1557). With
`fluentd-async: true` the driver connects in the background: a collector that
is down, still booting, or never installed costs log lines and nothing else.

## Enabling and disabling

`enable_gcp_logging` on the module (default **true**) gates four things
together: installing and configuring the Ops Agent, leaving the overlay file
on disk, and two project-level IAM grants to the VM service account —
`roles/logging.logWriter` and `roles/monitoring.metricWriter`. Set it to
`false` and the VM stays on Docker's default `json-file` driver.

The metric grant is not there for metrics this module wants. The Ops Agent
bundles an OpenTelemetry sub-agent that cannot be switched off, only emptied,
and it exports the agent's own `agent.googleapis.com/agent/*` self-metrics
whatever its config says. Those are free, but without the role every export
cycle fails and the serial console fills with `monitoring.timeSeries.create`
`PermissionDenied` once a minute. The metrics Cloud Monitoring *does* bill
for — the `hostmetrics` receiver's CPU, disk, memory, network and process
series — are switched off in the agent config, because `enable_datadog` is
the path that collects those.

**Upgrading an existing VM: recreate it, or pay for host metrics until you
do.** The IAM grant lands on `terraform apply`; the agent config lives in
`metadata_startup_script`, which is in `ignore_changes`, so it reaches the VM
only on instance replacement. Between the two, an already-provisioned VM
still runs the built-in `hostmetrics` receiver and — now that the role
permits it — exports it successfully, billed by ingested bytes. Closing that
window is the same `terraform apply -replace=...` that propagates every other
startup-script change from this module.

Engagement is **placement + probe**. The overlay is appended to
`COMPOSE_FILE` only when the file exists *and*
`scripts/ops/agnes-compose-file.sh::agnes_gcp_logging_probe` has confirmed
something is accepting connections on `127.0.0.1:24224`, leaving a
`.gcp-logging-ok` marker. Every builder of the compose list — boot,
`agnes-auto-upgrade.sh`, `agnes-state-applier.sh` — asks the same gate, so
boot and the recurring ticks cannot disagree (#1558). A VM whose agent never
came up logs a loud warning and runs without Cloud Logging; the next
auto-upgrade tick re-probes, so fixing the agent re-arms it without a reboot.

## Reading the logs

Entries land under the `gce_instance` resource. The fields the app writes are
promoted to `jsonPayload`, so these all work as real filters:

```
severity >= WARNING
jsonPayload.service = "scheduler"
jsonPayload.env = "production"
jsonPayload.request_id = "…"
jsonPayload.event = "llm_generation"
```

## Verifying it on a VM

```bash
systemctl status google-cloud-ops-agent
```

```bash
sudo journalctl -u google-cloud-ops-agent -n 50
```

```bash
ls -l /opt/agnes/.gcp-logging-ok
```

The marker's presence is the single answer to "is the overlay engaged". With
`DEBUG=1` on the instance, `GET /api/debug/throw` raises a real unhandled
exception end to end — the fastest way to confirm an `ERROR` entry arrives
with its `request_id` attached.

## Local `docker logs`

Unaffected. Docker's dual-logging cache (>= 20.10) keeps `docker logs` and
`docker compose logs` working while a non-local driver is active.
