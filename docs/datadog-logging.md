# Shipping logs to Datadog

Agnes itself does not know about Datadog: it writes one JSON object per line to
stdout ([`observability.md`](observability.md)) and stops there. This page
describes the pipeline the bundled Terraform module
(`infra/modules/customer-instance/`) wires up on a GCE-hosted instance when the
container logs are pointed at Datadog instead of Cloud Logging.

Prerequisite: `enable_datadog` — this is the same host agent that already
collects the host, disk, Docker, systemd, TLS, HTTP and Postgres checks
([`DEPLOYMENT.md`](DEPLOYMENT.md)). Turning logs on adds a config block to it.
It installs nothing new.

## The pipeline

```
container stdout (JSON per line)
        │  Docker json-file driver (the daemon default)
        ▼
Datadog Agent  ── Docker API tailer ──> container tags
        │       ── processing_rules ──> redaction
        ▼
Datadog Logs
```

## Choosing the destination

One collector per VM. `container_logs_destination` selects it:

| value | effect |
|---|---|
| `""` / `"auto"` (default) | `datadog` when `enable_datadog`, else `cloud_logging` when `enable_gcp_logging`, else `none` |
| `"cloud_logging"` | the fluentd driver + Ops Agent ([`gcp-logging.md`](gcp-logging.md)); requires `enable_gcp_logging` |
| `"datadog"` | this page; requires `enable_datadog` |
| `"none"` | json-file driver and `docker logs`, nothing shipped |

Asking for a destination whose collector was never provisioned fails at plan
time, not at boot — the failure it prevents is silent, because a VM in that
state boots fine, reports a healthy agent, and ships nothing.

`enable_gcp_logging` remains the **permit** switch: it is what grants the VM
service account `roles/logging.logWriter` and `roles/monitoring.metricWriter`.
Leaving it true while the destination is `datadog` keeps those grants and
installs no Ops Agent, so switching back later is a VM recreate rather than an
IAM change.

## Why the driver has to be json-file

Docker allows exactly one log driver per container, and the two collectors want
different ones.

The Datadog Agent reads containers through the Docker API. Under the `fluentd`
driver that the Cloud Logging overlay installs, `docker inspect .LogPath` is
empty and the API serves Docker's *dual-logging cache* instead. That happens to
work — but it is not a path Datadog documents or supports for remote drivers,
and a production log pipeline should not rest on it. On the default `json-file`
driver the same API serves the driver's own logs, and the path is supported.

So `container_logs_destination = "datadog"` makes the startup script leave
`docker-compose.gcp-logging.yml` off the disk, which disarms the COMPOSE_FILE
resolver's gate (`scripts/ops/agnes-compose-file.sh`) for free and drops the
containers back onto the daemon default, rotated at `50m × 5` by
`/etc/docker/daemon.json`.

Running both destinations at once is therefore not offered. It is not
physically impossible; it would just mean building on undocumented behaviour.

**One thing this buys back:** a Datadog VM has no `fluentd` driver, so it has no
`fluentd-async`, no `.gcp-logging-ok` marker and none of the "a log driver that
cannot initialize keeps the container in `created`" failure class that cost
9 minutes of production in #1557. Log collection is configured *in the agent*,
so it cannot stop a container from starting.

## Why the Docker API and not the log files

The agent prefers to tail
`/var/lib/docker/containers/<id>/<id>-json.log` directly, and this module turns
that off (`logs_config.docker_container_use_file: false`).

`dd-agent` cannot open those files. They are root-owned and mode `0700`, and
docker-group membership grants the *socket*, not the filesystem. A default POSIX
ACL cannot rescue it either: dockerd creates each container directory with mode
`0700`, whose zero group bits clamp the ACL mask, so an inherited `u:dd-agent`
entry is masked out. Left at its default the agent would fail the open and fall
back to the API anyway — once per container and loudly. Saying it outright is
deterministic and touches no directory Docker owns and re-asserts on upgrade.

**This is a real trade, not a free one.** Socket collection makes `dockerd`
stream and serialize every log line, so at high volume it costs daemon CPU and
the agent can drop lines — Datadog's own guidance is that file collection
performs better. It is acceptable here because this stack is quiet by
construction (see *Cost* below), and because the alternative is not "tail the
files" but "run the agent as root", which would undo the posture the whole
Datadog design rests on. If `dockerd` CPU becomes visible, that is the signal to
reduce what is collected (`container_exclude_logs`), not to widen the agent's
privileges. Watch it alongside the log volume on the first VM.

## Scope

Every container (`logs_config.container_collect_all: true`), including
`postgres`, `kai-agent`, the dispatcher, `redis` and the one-shot `migrate` and
`extract` containers.

Deliberately not an allowlist. `docker-compose.gcp-logging.yml`'s header records
what an allowlist does over time: its service list was written once and then
drifted, so `extraction-worker`, `apps-runner`, `egress-proxy` and
`kai-agent-stub` silently stayed off the pipeline for months while the services
that *were* listed made the setup look healthy. Default-on makes a new service
loud instead of silent.

The metric-side exclusion of the one-shot containers is
`container_exclude_metrics`, not the generic `container_exclude`, because the
generic list suppresses logs as well — and there is no interaction between the
global list and the scoped ones, so a container excluded globally cannot be
brought back with `container_include_logs`. A failed migration's log is exactly
what an operator goes looking for.

## What Datadog does with the fields

Three of the app's JSON fields map through Datadog's default JSON preprocessing
with no configuration at all:

| Agnes field | Datadog |
|---|---|
| `severity` | the log's status |
| `service` | the reserved `service` |
| `message` | the log message |

Two need a **caller-side pipeline**, and it should exist *before* the first VM
starts shipping — a pipeline added later does not reprocess what is already
indexed:

- **`time` is not a default date attribute.** Datadog's defaults are
  `@timestamp`, `timestamp`, `_timestamp`, `Timestamp`, `eventTime`, `date`,
  `published_date`, `syslog.timestamp`. Without a Date Remapper on `time`, every
  line carries the *ingestion* timestamp. In steady state that is within a
  second of the truth; it diverges exactly when a backlog is replayed after an
  agent or container restart — i.e. during an incident. Do not rename the app's
  field to fix this: `files/ops-agent-config.yaml` parses on `time_key: time`.
- **`env` means two different things.** The app writes `env` =
  `AGNES_DEPLOYMENT_ENV` (the VM name by default) while the agent emits a host
  tag `env` = the GCP project id, and `env` is reserved in Datadog. Remap the
  attribute (e.g. to `agnes_deployment_env`) so the tag wins.

Worth considering in a shared Datadog org: the app's `service` values are
`app`, `scheduler`, `extract` — generic enough to collide with another team's.
A Service Remapper onto `agnes-%{service}` avoids that.

The agent's own `source` defaults to each container's short image name, so
`postgres` and `caddy` pick up their Datadog integration pipelines for free.

## Redaction

`logs_config.processing_rules` mask four credential shapes on the way out: URL
userinfo (`scheme://user:pass@`), `Authorization` / `X-Api-Key` /
`X-StorageApi-Token` values, known API-key prefixes (`sk-ant-`, `sk-`, `ghp_`,
`xox*`, `AIza`) and JWTs. Each is tested in both directions in
`tests/test_datadog_log_collection.py` — a rule that fails to match is a leak, a
rule that over-matches is silent data loss.

What the pipeline does **not** carry: prompts and completions are never logged,
only their sizes ([`observability.md`](observability.md)); response bodies are
off in every `http_check`; and `container_env_as_tags` stays `{}` because this
stack passes secrets through the environment and a tag is not maskable.

Emails are deliberately *not* masked — the app logs `user=<email>` in places,
and the same identifiers are already in `audit_log` and the chat transcripts, so
masking would cost the log its operational value and buy nothing.

**Do not add a `com.datadoghq.ad.logs` label to a container.** An
integration-level log config completely overrides the global
`processing_rules` for that container, i.e. it un-redacts it. A test guards
this.

Nothing here can mask what nobody anticipated. Every log line added from now on
is a line that leaves the host.

**And leaves the cloud.** Cloud Logging keeps entries inside the deployment's own
GCP project; Datadog is a third-party SaaS, in whatever region `datadog_site`
names. Switching destination is therefore a data-residency and processor
decision as well as an operational one, and on a deployment with commitments
about where operational data may live it is the part to settle first — before
the redaction rules, which bound only *what* is sent, never *where*.

## Cost

Datadog bills ingested GB *and* indexed events, and the second number is the one
that shows up. The lever is a caller-side **index exclusion filter** — for
example index `status:(error OR warn)` in full and sample `INFO`. That is free,
adjustable after the fact, and keeps the excluded lines visible in Live Tail.

Two things keep the baseline low on this stack: `uvicorn.access` is pinned to
`WARNING` in production (`app/logging_config.py`), so there are no per-request
access lines, and the `Caddyfile` has no `log` directive, so Caddy writes none
either. The per-request volume driver that *does* scale with traffic is
`services/egress_proxy/proxy.py`, one line per outbound connection. Measure a
day of real volume on one VM before setting any filter.

## Enabling it

```hcl
enable_datadog             = true
datadog_api_key_secret     = "<secret-manager-secret-name>"
container_logs_destination = "datadog"   # or leave unset — auto resolves here
```

Order matters: create the caller-side pipeline, index and exclusion filters
*first*, then apply, then recreate the instance.

Like everything the startup script renders, this reaches a running VM only
through a recreate:

```bash
terraform apply -replace='module.<name>.google_compute_instance.vm["<vm-name>"]'
```

`datadog.yaml` is not in `agnes-auto-upgrade.sh`'s `CONFIG_FILES`, so the
5-minute tick will never deliver it — the recreate is the only path.

If `enable_gcp_logging` is being turned off in the same change, keep the window
between `apply` and the recreate short. The `apply` removes
`roles/logging.logWriter` and `roles/monitoring.metricWriter` immediately, while
the Ops Agent keeps running until the recreate: every flush then fails and its
OpenTelemetry sub-agent floods the serial console with
`monitoring.timeSeries.create PermissionDenied`. This is the mirror image of the
window `enable_gcp_logging`'s own description documents for the opposite
direction.

## Verifying it on a VM

```bash
# 1. the overlay is gone and the containers are on json-file
ls -l /opt/agnes/.gcp-logging-ok            # must NOT exist
docker inspect --format '{{.LogPath}}' agnes-app-1   # must be a non-empty *-json.log path

# 2. the agent is tailing them
sudo datadog-agent status | sed -n '/Logs Agent/,/^$/p'   # one tailer per container, BytesSent climbing

# 3. the checks that were already there still are
sudo datadog-agent status | grep -A3 'postgres ('

# 4. redaction actually fires
sudo datadog-agent stream-logs
```

An empty `LogPath` in step 1 is the one failure that matters: it means the
overlay is still armed and the agent is reading a dual-logging cache instead.

In Datadog: confirm the status reflects the app's `severity`, that a line's
timestamp matches its `time` attribute (the Date Remapper landed), and that
`compose_service` is present — that is the tag the container metrics already
group by, so logs and metrics join on it.

## Local `docker logs`

Unaffected, and on this destination it is the driver's own store rather than the
dual-logging cache. `infra/modules/customer-instance/files/agnes-watchdog.sh`
depends on it.
