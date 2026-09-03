# Postgres side-car check, delivered through file-based Autodiscovery.
#
# ad_identifiers matches the short image name, so ONE template covers every
# postgres:* container of the compose project (the app's side-car and the
# kai-agent's) without the module knowing how many there are. %%host%% is
# resolved per container by the agent.
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
