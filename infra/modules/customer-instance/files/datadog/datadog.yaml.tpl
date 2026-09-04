# Datadog Agent core configuration for an Agnes customer-instance VM.
#
# Rendered by the module and installed by the startup script as
# /etc/datadog-agent/datadog.yaml (root:dd-agent 0640). @@DD_API_KEY@@ is
# substituted on the host from a Secret Manager value; the key is never in
# Terraform state, never in /opt/agnes/.env and never on a command line.
#
# The agent runs as dd-agent, which is a member of the docker group — i.e.
# root-equivalent on this host. Everything below that could turn that
# membership into an inbound or remote-controlled capability is off: no remote
# configuration, no APM, no DogStatsD, no process/container/discovery
# collection, no runtime security or compliance, no SBOM or image collection,
# no inventory uploads, and the IPC endpoint bound to loopback.
#
# Log collection (container_logs_destination = "datadog", off by default) used
# to be on that list, and it is worth being exact about why it left. It adds no
# inbound surface and no remote control: the logs pipeline is outbound-only, to
# ${site}. What it adds is EXFILTRATION surface — through the docker socket
# dd-agent could already READ every container's logs; this is the first setting
# that sends them off the host. Its compensating controls are therefore about
# CONTENT, not access, and they are:
#
#   * Agnes never logs prompts or completions, only their sizes
#     (docs/observability.md). The OTLP path's span-content capture is
#     separately opt-in and defaults off, for the same reason.
#   * container_env_as_tags stays {} — this stack passes secrets through the
#     environment, and a tag is not maskable.
#   * logs_config.processing_rules below mask the credential shapes that can
#     still reach a line through an error string.
#
# None of that can mask what nobody anticipated. Every log line added from here
# on is a line that leaves the host.
api_key: "@@DD_API_KEY@@"
site: ${site}
env: ${env}

tags:
%{ for t in tags ~}
  - ${t}
%{ endfor ~}

# Gives every container metric a compose_service dimension, which is what the
# consumer-side monitors group by. container_labels_as_tags is the current key;
# docker_labels_as_tags is deprecated.
container_labels_as_tags:
  com.docker.compose.service: compose_service
  com.docker.compose.project: compose_project

# Container env vars never become tags: this stack passes secrets through the
# environment.
container_env_as_tags: {}

# The oneshot migration/extract containers exist for seconds at a time; keeping
# them out avoids a series per boot that nothing ever queries. METRICS ONLY:
# the generic `container_exclude` suppresses logs as well, and there is no
# interaction between the global list and the scoped ones — a container
# excluded globally cannot be brought back with container_include_logs. Their
# LOGS are the opposite of noise; a failed migration is exactly what an
# operator goes looking for.
#
# Note what else moved with it: the generic list also feeds SBOM, compliance,
# runtime security, process_config.container_collection and container
# image/lifecycle collection. All of those are off below, so this is inert
# today — but turning any of them on brings the oneshot containers back with
# it, and the exclusion would have to be restated for that surface.
container_exclude_metrics:
  - "name:^agnes-(migrate|data-migrate|kai-agent-migrate|extract)-[0-9]+$"
exclude_pause_container: true

bind_host: 127.0.0.1

logs_enabled: ${enable_logs}
use_dogstatsd: false

remote_configuration:
  enabled: false

apm_config:
  enabled: false

process_config:
  process_collection:
    enabled: false
  container_collection:
    enabled: false
  process_discovery:
    enabled: false

runtime_security_config:
  enabled: false

compliance_config:
  enabled: false

sbom:
  enabled: false
  container_image:
    enabled: false
  host:
    enabled: false

container_image:
  enabled: false

container_lifecycle:
  enabled: false

inventories_configuration_enabled: false
inventories_checks_configuration_enabled: false

%{ if enable_logs ~}
# --- Container log collection (container_logs_destination = "datadog") -------
#
# The listener and the config provider are what turn running containers into
# log sources; logs_enabled alone only collects the agent's own files.
listeners:
  - name: docker
config_providers:
  - name: docker
    polling: true

logs_config:
  # Every container, not an allowlist. An allowlist is precisely how
  # docker-compose.gcp-logging.yml lost extraction-worker, apps-runner,
  # egress-proxy and kai-agent-stub for months (see its header): a per-service
  # list drifts from the compose file, and the drift is invisible until an
  # operator goes looking for logs that were never shipped. Default-on makes a
  # new service loud instead of silent. Volume is controlled where it is
  # actually billed — an exclusion filter on the caller's log index.
  container_collect_all: true

  # Read through the Docker API, not /var/lib/docker/containers/<id>/*-json.log.
  # The agent PREFERS the file, but dd-agent cannot open it: those directories
  # are root-owned and mode 0700, and docker-group membership grants the
  # SOCKET, not the filesystem. A default POSIX ACL cannot rescue it either —
  # the 0700 creation mode's zero group bits clamp the ACL mask, so an
  # inherited u:dd-agent entry is masked out. Left at its default `true` the
  # agent would fail the open and fall back here anyway, once per container and
  # loudly; saying it outright is deterministic and touches no directory Docker
  # owns and re-asserts on upgrade.
  docker_container_use_file: false

  # Global rules: they apply to every source this agent has, which is container
  # logs and nothing else. DO NOT add a com.datadoghq.ad.logs label to a
  # container without moving these rules onto it — an integration-level log
  # config COMPLETELY OVERRIDES the global processing_rules, which would
  # silently un-redact that container.
  #
  # RE2, so no lookarounds. Where a value has to be masked but its label kept,
  # the label is captured and re-emitted as $1.
  processing_rules:
    # A DSN with inline credentials. instance.yaml carries one (which is why
    # that file is 0600), and any connection error that formats it into an
    # exception puts the password on a log line.
    # The username atom is `*`, not `+`: `redis://:secret@host` is a legal
    # URL and the password is the part that matters.
    - type: mask_sequences
      name: mask_url_credentials
      pattern: '([a-zA-Z][a-zA-Z0-9+.\-]*)://[^\s/:@]*:[^\s/@]+@'
      replace_placeholder: '$1://***:***@'
    # Both quote styles: Python's dict repr writes {'Authorization': '…'},
    # so a rule that only accepted double quotes missed every header logged
    # through a repr — which is most of them.
    #
    # The scheme word is optional AND alternated. With `bearer` alone, a
    # `Basic <base64>` header slipped through whole: once the bearer branch
    # failed, the value class had to match from `Basic`, and the space after it
    # is not in the class. This stack speaks Basic in several places — the Jira
    # connector, the MCP client, the marketplace git router's PAT header and
    # the PAT resolver — so that was the more likely leak of the two.
    - type: mask_sequences
      name: mask_authorization_values
      pattern: '(?i)((?:authorization|x-api-key|x-storageapi-token)["'']?\s*[:=]\s*["'']?\s*)(?:bearer|basic|token)?\s*[A-Za-z0-9._~+/-]{12,}'
      replace_placeholder: '$1***'
    - type: mask_sequences
      name: mask_known_key_shapes
      # `sk-` accepts - and _ so a project-scoped key (sk-proj-…, sk-ant-…)
      # is not cut short at its second hyphen. That subsumes an explicit
      # sk-ant- branch exactly — sk-ant- plus 16 is sk- plus 20 — so there
      # isn't one.
      pattern: '(sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[A-Za-z0-9_-]{20,})'
      replace_placeholder: '***'
    # The payload segment's bound is deliberately loose. A JWT whose claims
    # are small base64s to very little — `{}` is `e30`, three characters — so
    # a bound tuned to a typical payload lets exactly the smallest tokens
    # through unmasked. `eyJ` plus two dots plus base64url is already specific
    # enough that lowering it costs no false positives.
    - type: mask_sequences
      name: mask_jwt
      pattern: 'eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{8,}'
      replace_placeholder: '***'

# The oneshot containers are excluded from METRICS above. Stated here as an
# explicit empty list so the two filters can never drift into one another: a
# failed migration must still reach the log pipeline.
container_exclude_logs: []
%{ endif ~}
