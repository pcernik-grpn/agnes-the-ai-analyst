# Postgres side-car check, delivered through file-based Autodiscovery.
#
# ad_identifiers matches the short IMAGE name, so ONE template covers both
# side-cars (the app's and the kai-agent's) without the module knowing how many
# there are. %%host%% is resolved per container by the agent.
#
# Be precise about the scope, because it is wider than the role bootstrap's:
# Autodiscovery matches on the image across the whole host, while
# agnes-datadog-pg-role.sh deliberately creates the monitoring role only inside
# the Agnes compose project. An unrelated postgres:* container on the same host
# would therefore be probed and report postgres.can_connect CRITICAL rather
# than being ignored. Narrowing this further needs a com.datadoghq.ad.* label
# on the container, which lives in the app image's compose file, not in this
# module — so it is a documented limitation, not an oversight.
#
# @@DD_PG_PASSWORD@@ is substituted on the host by agnes-datadog-pg-role.sh,
# which owns the credential; it is not a Terraform value.
#
# dbm stays false: Database Monitoring is a separate, billed product and this
# check only needs liveness, connection headroom, size and XID age.
ad_identifiers:
  - postgres

init_config: {}

instances:
  - host: "%%host%%"
    port: 5432
    username: datadog
    password: "@@DD_PG_PASSWORD@@"
    dbname: postgres
    ssl: disable
    dbm: false
    collect_database_size_metrics: true
    collect_activity_metrics: false
    collect_wal_metrics: false
    min_collection_interval: 60
    # The check attributes every series and service check to the hostname it
    # resolves for the instance — here the side-car's container IP, a phantom
    # host nothing else reports for — so the agent-level env and host tags
    # never join them. The deployment identity therefore rides on the
    # instance: Terraform renders env and the VM's own tag list (the same
    # list datadog.yaml carries), while the password above stays the role
    # script's job.
    tags:
      - env:${env}
%{ for t in tags ~}
      - ${t}
%{ endfor ~}
