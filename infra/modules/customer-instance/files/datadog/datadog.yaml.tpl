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
# configuration, no APM, no logs, no DogStatsD, no process/container/discovery
# collection, no runtime security or compliance, no SBOM or image collection,
# no inventory uploads, and the IPC endpoint bound to loopback. What is left is
# an outbound metrics push to ${site} and nothing else.
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
# them out avoids a series per boot that nothing ever queries.
container_exclude:
  - "name:^agnes-(migrate|data-migrate|kai-agent-migrate|extract)-[0-9]+$"
exclude_pause_container: true

bind_host: 127.0.0.1

logs_enabled: false
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
