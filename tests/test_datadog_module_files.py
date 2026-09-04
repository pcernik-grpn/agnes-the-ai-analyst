"""Contract tests for the opt-in Datadog host monitoring in customer-instance.

Two layers, because the module has two failure modes. The Terraform half is
text assertions (the repo's house style for infra: the module is never applied
in CI, so the source text IS the contract). The artifact half actually RENDERS
the templates and parses the result, because a check config that Terraform is
happy to base64 can still be a YAML the agent silently refuses to load.

The renderer these tests use, `tests/_tf_template.py`, was verified byte-for-byte
against real `terraform console` output on the full 1179-line startup script;
`test_renderer_matches_terraform_semantics` pins the handful of behaviours that
verification depended on, so a future edit to the renderer cannot quietly drift
away from Terraform and take these tests' meaning with it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _tf_template import render_template  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
MODULE = REPO / "infra/modules/customer-instance"
FILES = MODULE / "files/datadog"

VARIABLES_TF = (MODULE / "variables.tf").read_text()
MAIN_TF = (MODULE / "main.tf").read_text()

DD_VARS = (
    "enable_datadog",
    "datadog_api_key_secret",
    "datadog_site",
    "datadog_env",
    "datadog_agent_version",
    "datadog_extra_tags",
    "extra_labels",
)


def _var_block(name: str) -> str:
    start = VARIABLES_TF.index(f'variable "{name}"')
    nxt = VARIABLES_TF.find('\nvariable "', start + 1)
    return VARIABLES_TF[start:] if nxt == -1 else VARIABLES_TF[start:nxt]


def _render(name: str, **variables) -> str:
    return render_template((FILES / name).read_text(), variables)


# --------------------------------------------------------------------------
# Terraform plumbing
# --------------------------------------------------------------------------


def test_variables_are_declared_and_default_to_off():
    for name in DD_VARS:
        assert f'variable "{name}"' in VARIABLES_TF, name
    assert re.search(r'variable "enable_datadog".*?default\s*=\s*false', VARIABLES_TF, re.S)
    assert re.search(r'variable "datadog_site".*?default\s*=\s*"datadoghq\.com"', VARIABLES_TF, re.S)
    # A module bump alone must be an empty diff for every existing consumer.
    for name in ("datadog_api_key_secret", "datadog_env"):
        assert re.search(rf'variable "{name}".*?default\s*=\s*""', VARIABLES_TF, re.S), name


def test_every_datadog_input_that_can_fail_at_apply_time_is_validated_at_plan_time():
    # A secret VALUE pasted where a secret NAME belongs, a site the key does not
    # belong to, and a label value GCE rejects are all apply-time failures
    # otherwise — the expensive kind, mid-rollout.
    for name in (
        "datadog_api_key_secret",
        "datadog_site",
        "datadog_env",
        "datadog_agent_version",
        "datadog_extra_tags",
        "extra_labels",
    ):
        assert "validation" in _var_block(name), f"{name} has no validation block"


def test_extra_labels_validates_every_gce_constraint_separately():
    """GCE constrains label keys, label values and label COUNT differently, and
    each one is an apply-time failure rather than a plan-time one if unchecked —
    the expensive kind, mid-rollout."""
    block = _var_block("extra_labels")
    assert "keys(var.extra_labels)" in block, "label KEY shape is unvalidated"
    assert "values(var.extra_labels)" in block, "label VALUE shape is unvalidated"
    assert "length(var.extra_labels)" in block, "GCE caps a resource at 64 labels and the module spends four of them"
    assert block.count("validation") >= 3


def test_datadog_extra_tags_cannot_shadow_a_module_owned_dimension():
    """A second value for `env` (or role, or service) does not error in Datadog —
    it silently gives the host two, which breaks the one-deployment-per-env
    invariant every consumer-side monitor scopes on."""
    block = _var_block("datadog_extra_tags")
    assert "length(var.datadog_extra_tags) <= 50" in block, "unbounded tag list"
    assert "length(t) <= 200" in block, "a Datadog tag is capped at 200 characters"
    for reserved in ("env", "customer", "app", "service", "role", "agnes_instance", "managed"):
        assert f'"{reserved}"' in block, f"{reserved} is not a reserved tag key"
    assert 'split(":", t)[0]' in block, "the reserved check must look at the tag KEY"


def test_main_tf_ships_every_artifact_under_files_datadog():
    shipped = sorted(p.relative_to(FILES).as_posix() for p in FILES.rglob("*") if p.is_file())
    assert shipped, "files/datadog is empty"
    for rel in shipped:
        assert rel in MAIN_TF, f"files/datadog/{rel} is never referenced by main.tf"


def test_the_agent_key_is_granted_as_its_own_secret_and_never_through_dot_env():
    assert 'google_secret_manager_secret_iam_member" "vm_datadog"' in MAIN_TF
    assert "for_each  = local.datadog_secrets" in MAIN_TF
    # IAM lag would 403 the boot-time fetch, so the VM must wait for the binding.
    depends = re.search(r"depends_on = \[(.*?)\]", MAIN_TF, re.S).group(1)
    assert "google_secret_manager_secret_iam_member.vm_datadog" in depends
    # The secret NAME may be forwarded to the template; the VALUE may not be
    # resolved in Terraform at all.
    assert "datadog_api_key_secret       = var.datadog_api_key_secret" in MAIN_TF
    assert "google_secret_manager_secret_version.datadog" not in MAIN_TF
    assert 'data "google_secret_manager_secret_version"' not in MAIN_TF


def test_the_datadog_secret_grant_is_deduped_against_every_other_grant():
    # Two identical (project, secret, role, member) bindings fail the apply with
    # "already exists" — the trap kai_agent_secrets already documents.
    block = MAIN_TF[MAIN_TF.index("datadog_secrets = ") : MAIN_TF.index("datadog_static_files")]
    for other in (
        "runtime_secret_env",
        "runtime_secret_env_multiline",
        "runtime_secrets",
        "local.dispatcher_secrets",
        "local.kai_agent_secrets",
    ):
        assert other in block, f"datadog_secrets does not subtract {other}"


def test_enabling_datadog_without_a_secret_name_fails_at_plan_time():
    assert "!var.enable_datadog || var.datadog_api_key_secret" in MAIN_TF


def test_extra_labels_reach_all_three_labelled_resources_and_never_win():
    # merge(var.extra_labels, {module keys}) — module keys last, so a caller
    # cannot re-label a VM out from under the log filters that key off them.
    assert MAIN_TF.count("labels = merge(var.extra_labels, {") == 3, (
        "the VM, the data disk and the static IP must all carry extra_labels"
    )
    assert "labels = merge({" not in MAIN_TF, "module keys must be the LAST merge argument"


def test_env_defaults_to_the_gcp_project_id():
    assert "datadog_env = coalesce(var.datadog_env, var.gcp_project_id)" in MAIN_TF


def test_the_check_configs_are_rendered_per_instance_not_once_per_module():
    # datadog.yaml carries the instance's own role/name tags and the HTTP+TLS
    # checks its own hostnames, so a flat module-wide map would give a dev VM
    # the prod VM's identity.
    assert "datadog_files_b64            = local.datadog_files_b64[each.value.name]" in MAIN_TF
    assert "for inst in local.all_instances : inst.name => var.enable_datadog" in MAIN_TF


def test_the_tls_and_http_policy_lives_in_terraform_not_in_the_templates():
    # The templates stay dumb renderers; "does this VM terminate TLS" is decided
    # once, in HCL, where tls_mode and domain actually live.
    assert MAIN_TF.count('inst.tls_mode == "caddy" && inst.domain != ""') == 3
    for name in ("http_check.yaml.tpl", "tls.yaml.tpl"):
        text = (FILES / name).read_text()
        assert "tls_mode" not in text and "%{ if" not in text, name


# --------------------------------------------------------------------------
# Agent artifacts
# --------------------------------------------------------------------------


def test_every_static_check_config_is_valid_yaml_with_instances():
    for path in sorted((FILES / "conf.d").glob("*.yaml")):
        doc = yaml.safe_load(path.read_text())
        assert isinstance(doc, dict) and doc.get("instances"), path.name


HARDENING = (
    "remote_configuration",
    "apm_config",
    "use_dogstatsd: false",
    "process_collection",
    "container_collection",
    "process_discovery",
    "runtime_security_config",
    "compliance_config",
    "sbom",
    "container_image",
    "container_lifecycle",
    "inventories_configuration_enabled: false",
    "inventories_checks_configuration_enabled: false",
    "bind_host: 127.0.0.1",
    "container_env_as_tags",
    "container_labels_as_tags",
    "exclude_pause_container: true",
)


def _datadog_yaml(*, enable_logs: bool) -> dict:
    rendered = _render(
        "datadog.yaml.tpl",
        site="datadoghq.com",
        env="example-project",
        tags=["customer:acme", "app:agnes", "role:prod"],
        enable_logs=enable_logs,
    )
    for key in HARDENING:
        assert key in rendered, key
    return yaml.safe_load(rendered.replace("@@DD_API_KEY@@", "x"))


@pytest.mark.parametrize("enable_logs", [False, True])
def test_datadog_yaml_is_hardened_because_dd_agent_is_in_the_docker_group(enable_logs: bool):
    """The docker-group compensating controls hold in BOTH states.

    Log collection deliberately left this list (it is an egress decision, not
    a privilege one — see the template's header), so it is asserted separately
    below. Everything that could turn docker-group membership into an inbound
    or remote-controlled capability must stay off either way.
    """
    doc = _datadog_yaml(enable_logs=enable_logs)
    assert doc["env"] == "example-project" and doc["site"] == "datadoghq.com"
    assert doc["tags"] == ["customer:acme", "app:agnes", "role:prod"]
    assert doc["remote_configuration"]["enabled"] is False, (
        "remote configuration is the one setting that would let a compromised "
        "Datadog org reach back into a host where dd-agent is docker-group"
    )
    assert doc["apm_config"]["enabled"] is False
    assert doc["container_env_as_tags"] == {}, "this stack passes secrets in the environment"
    assert doc["container_labels_as_tags"]["com.docker.compose.service"] == "compose_service"
    # docker_labels_as_tags is the deprecated spelling and is silently ignored;
    # the assertion is on the parsed config, not the text, because the template
    # names the deprecated key in a comment on purpose.
    assert "docker_labels_as_tags" not in doc


def test_logs_off_renders_no_logs_configuration_at_all():
    """An off render must leave no dangling keys — a `logs_config:` with no
    `logs_enabled` would be a config the agent reads and silently ignores."""
    doc = _datadog_yaml(enable_logs=False)
    assert doc["logs_enabled"] is False
    for key in ("logs_config", "listeners", "config_providers", "container_exclude_logs"):
        assert key not in doc, f"{key} must not appear when logs are off"


def test_logs_on_collects_every_container_through_the_docker_api():
    doc = _datadog_yaml(enable_logs=True)
    assert doc["logs_enabled"] is True
    # logs_enabled alone only collects the agent's own files; the listener and
    # the config provider are what turn running containers into log sources.
    assert doc["listeners"] == [{"name": "docker"}]
    assert doc["config_providers"] == [{"name": "docker", "polling": True}]
    assert doc["logs_config"]["container_collect_all"] is True, (
        "an allowlist drifts from the compose file — the exact failure docker-compose.gcp-logging.yml's header records"
    )
    assert doc["logs_config"]["docker_container_use_file"] is False, (
        "dd-agent cannot open /var/lib/docker/containers (root-owned, 0700; "
        "docker-group grants the socket, not the filesystem), and a default "
        "ACL cannot inherit onto a 0700 directory because the mode's group "
        "bits clamp the mask — so read through the Docker API deliberately "
        "rather than failing the open once per container"
    )


def test_the_oneshot_exclusion_is_metrics_only_so_a_failed_migration_still_logs():
    """`container_exclude` filters logs as well as metrics, and there is no
    interaction between the global list and the scoped ones — a container
    excluded globally cannot be brought back with container_include_logs."""
    for enable_logs in (False, True):
        doc = _datadog_yaml(enable_logs=enable_logs)
        assert "container_exclude" not in doc, (
            "the global list would silently drop the migrate/extract "
            "containers' LOGS, which is what an operator reads when a "
            "migration fails"
        )
        assert doc["container_exclude_metrics"], "the metric-side intent must survive the rename"
    assert _datadog_yaml(enable_logs=True)["container_exclude_logs"] == []


def test_main_tf_renders_the_logs_flag_from_the_resolved_destination():
    assert "enable_logs = local.datadog_logs_active" in MAIN_TF, (
        "datadog.yaml's logs switch must follow the resolved destination, not "
        "var.enable_datadog — a VM can run the agent for metrics only"
    )


def test_datadog_yaml_carries_a_placeholder_not_a_key():
    tpl = (FILES / "datadog.yaml.tpl").read_text()
    assert "@@DD_API_KEY@@" in tpl
    assert "${api_key}" not in tpl and "${datadog_api_key}" not in tpl, (
        "the key must be substituted on the host, never interpolated by Terraform "
        "(that would put it in the plan and in state)"
    )


def test_disk_check_tags_by_mount_and_uses_current_option_names():
    inst = yaml.safe_load((FILES / "conf.d/disk.yaml").read_text())["instances"][0]
    assert inst["use_mount"] is True, "monitors scope on device:/ and device:/data"
    assert inst["service_check_rw"] is True, "a read-only /data remount must surface"
    assert "mount_point_exclude" in inst and "file_system_exclude" in inst
    for legacy in ("excluded_filesystems", "excluded_mountpoint_re", "excluded_disks"):
        assert legacy not in inst, f"{legacy} is the pre-Agent-7 name and is ignored"


def test_systemd_check_watches_the_backup_oneshot_and_treats_dead_as_healthy():
    inst = yaml.safe_load((FILES / "conf.d/systemd.yaml").read_text())["instances"][0]
    assert "agnes-db-backup.service" in inst["unit_names"]
    assert "agnes-datadog-pg-role.timer" in inst["unit_names"]
    mapping = inst["substate_status_mapping"]["agnes-db-backup.service"]
    assert mapping["failed"] == "critical"
    # A oneshot at rest is "dead"/"exited"; calling that critical would page daily.
    assert mapping["dead"] == "ok" and mapping["exited"] == "ok"
    assert "private_socket" not in inst


def test_directory_check_covers_all_four_heartbeats():
    instances = yaml.safe_load((FILES / "conf.d/directory.yaml").read_text())["instances"]
    probes = {t.split(":", 1)[1] for i in instances for t in i["tags"] if t.startswith("agnes_probe:")}
    assert probes == {"state_applier", "auto_upgrade", "backup", "watchdog"}
    for inst in instances:
        assert inst["filegauges"] is True, "the signal is a file's AGE, not a count"
        assert inst["ignore_missing"] is True, "a fresh VM must not report a check error"
    by_probe = {t.split(":", 1)[1]: i for i in instances for t in i["tags"] if t.startswith("agnes_probe:")}
    # The backup writes dated subdirectories, so a non-recursive walk finds no file.
    assert by_probe["backup"]["recursive"] is True
    assert by_probe["backup"]["pattern"] == "*/STATUS"
    assert by_probe["state_applier"]["pattern"] == "agnes-state-applier.tick"
    assert by_probe["auto_upgrade"]["pattern"] == "auto-upgrade.tick"


def test_directory_check_watches_the_same_marker_dir_the_watchdog_writes():
    watchdog = (MODULE / "files/agnes-watchdog.sh").read_text()
    state = re.search(r"^STATE=(\S+)$", watchdog, re.M).group(1)
    # Derived from $STATE, not hardcoded: the bash harness sandboxes host paths
    # by rewriting the STATE assignment, so a sibling literal would send test
    # runs at the real /var/lib.
    sub = re.search(r'^MARK_DIR="\$STATE/(\w+)"$', watchdog, re.M)
    assert sub, 'MARK_DIR must be defined as "$STATE/<name>"'
    marker_dir = f"{state}/{sub.group(1)}"
    instances = yaml.safe_load((FILES / "conf.d/directory.yaml").read_text())["instances"]
    configured = {i["directory"] for i in instances for t in i["tags"] if t == "agnes_probe:watchdog"}
    assert configured == {marker_dir}


def test_postgres_check_is_autodiscovery_on_the_image_and_not_billed_dbm():
    doc = yaml.safe_load(_render("postgres.yaml.tpl", env="example-project", tags=[]))
    assert doc["ad_identifiers"] == ["postgres"], (
        "one template must cover every postgres side-car; the module does not know how many there are"
    )
    inst = doc["instances"][0]
    assert inst["host"] == "%%host%%"
    assert inst["password"] == "@@DD_PG_PASSWORD@@", "rendered on the host, not by Terraform"
    assert inst["dbm"] is False, "Database Monitoring is a separate billed product"
    assert inst["ssl"] == "disable", "loopback-only compose network"


def test_postgres_check_carries_the_deployment_identity_on_every_series():
    """The postgres check attributes everything it emits — `postgresql.*` and
    `postgres.can_connect` alike — to the hostname it RESOLVES for the
    instance. Under Autodiscovery that is the side-car's container IP: a
    phantom host nothing else reports for, which agent-level `env`/host tags
    never join (spec trap #15, found live). The deployment identity must
    therefore ride on the instance itself, or every env-scoped pg monitor in
    the consumer catalogue is permanent no-data."""
    tags = ["customer:acme", "app:agnes", "service:agnes", "role:prod"]
    doc = yaml.safe_load(_render("postgres.yaml.tpl", env="example-project", tags=tags))
    inst = doc["instances"][0]
    assert inst["tags"][0] == "env:example-project", "env is the one dimension every consumer-side monitor scopes on"
    for t in tags:
        assert t in inst["tags"], t
    # Identity is Terraform's job, the credential stays the role script's: the
    # placeholder must survive the Terraform render for agnes-datadog-pg-role.sh
    # to substitute on the host.
    assert inst["password"] == "@@DD_PG_PASSWORD@@"


def test_postgres_template_is_terraform_rendered_from_the_same_tags_as_datadog_yaml():
    """Two render mechanisms split this file's identity from the agent's —
    Terraform templated datadog.yaml while the on-host role script owned this
    template whole — and that split is how the check shipped with no identity
    tags at all. One mechanism now: Terraform renders identity into BOTH from
    one tag local; the host script substitutes only the credential."""
    static_block = MAIN_TF[MAIN_TF.index("datadog_static_files = [") : MAIN_TF.index("datadog_files_b64 =")]
    assert "postgres.yaml.tpl" not in static_block, (
        "shipped raw it would collide with the rendered entry in the merge()"
    )
    assert '"postgres.yaml.tpl" = base64encode(templatefile(' in MAIN_TF
    assert MAIN_TF.count("local.datadog_tags[inst.name]") == 2, (
        "datadog.yaml and the postgres check must share ONE identity tag list"
    )


def test_rendering_the_pg_check_puts_the_password_only_in_the_password_field():
    """agnes-datadog-pg-role.sh renders the template with bash's GLOBAL
    `${rendered//placeholder/$PW}`, so every occurrence of the placeholder
    becomes the real credential — a comment that names the literal token ships
    the password into the rendered file's comments, which is exactly where a
    `grep -v password` redaction pass does not look before the file is shared.
    The substitution runs on the file the VM actually holds, which is the
    Terraform-rendered template — so render first, exactly like the boot does."""
    pw = "s3cr3t-rendered-password"
    tpl = _render("postgres.yaml.tpl", env="example-project", tags=["customer:acme"])
    rendered = tpl.replace("@@DD_PG_PASSWORD@@", pw)
    carrying = [line for line in rendered.splitlines() if pw in line]
    assert len(carrying) == 1 and carrying[0].strip().startswith("password:"), (
        f"the rendered check config must carry the password exactly once, in the password: field; got {carrying!r}"
    )
    assert yaml.safe_load(rendered)["instances"][0]["password"] == pw


def test_pg_role_bootstrap_keeps_the_password_off_argv_and_never_fails_its_unit():
    sh = (FILES / "agnes-datadog-pg-role.sh").read_text()
    assert "PGPASSWORD=" not in sh, "an env var is inherited by children"
    assert "-v pw=" not in sh and "--password" not in sh
    assert "set -x" not in sh, "a trace would print the password"
    assert "psql" in sh and "<<SQL" in sh, "the SQL must arrive on stdin"
    assert "GRANT pg_monitor TO datadog;" in sh
    assert re.search(r"^exit 0$", sh, re.M), (
        "a failed monitoring bootstrap must not mark the systemd unit failed and trip the alerting it is setting up"
    )
    assert "set -uo pipefail" in sh and "set -e" not in sh


def test_pg_role_units_are_a_timer_not_a_boot_time_step():
    timer = (FILES / "agnes-datadog-pg-role.timer").read_text()
    service = (FILES / "agnes-datadog-pg-role.service").read_text()
    # Boot-time only would never re-converge after a side-car volume recreate.
    assert "OnUnitActiveSec=" in timer and "WantedBy=timers.target" in timer
    assert "Type=oneshot" in service
    assert "ExecStart=/usr/local/bin/agnes-datadog-pg-role.sh" in service


def test_health_probe_asserts_on_the_body_because_api_health_is_always_200():
    doc = yaml.safe_load(
        _render("http_check.yaml.tpl", base_url="https://agnes.example.com", acme_hosts=["agnes.example.com"])
    )
    by_name = {i["name"]: i for i in doc["instances"]}
    assert set(by_name) == {"agnes_readyz", "agnes_health_body", "agnes_acme_http"}
    assert by_name["agnes_health_body"]["content_match"], (
        "/api/health answers 200 even when unhealthy — only the body distinguishes"
    )
    assert by_name["agnes_readyz"]["url"].endswith("/readyz"), (
        "/readyz is the one endpoint whose status code carries meaning"
    )
    assert by_name["agnes_acme_http"]["url"].startswith("http://"), (
        "the ACME HTTP-01 probe must be plain HTTP on port 80"
    )
    assert by_name["agnes_acme_http"]["allow_redirects"] is False
    for inst in doc["instances"]:
        assert inst["include_content"] is False, "a response body must not reach Datadog"


def test_a_vm_without_a_domain_probes_loopback_and_gets_no_acme_check():
    doc = yaml.safe_load(_render("http_check.yaml.tpl", base_url="http://127.0.0.1:8000", acme_hosts=[]))
    names = {i["name"] for i in doc["instances"]}
    assert names == {"agnes_readyz", "agnes_health_body"}
    assert all(i["url"].startswith("http://127.0.0.1:8000") for i in doc["instances"])


def test_tls_check_covers_the_alias_whose_acme_account_has_no_contact_email():
    doc = yaml.safe_load(_render("tls.yaml.tpl", hosts=["agnes.example.com", "alias.example.com"]))
    assert {i["server"] for i in doc["instances"]} == {"agnes.example.com", "alias.example.com"}
    for inst in doc["instances"]:
        assert inst["port"] == 443
        assert f"tls_target:{inst['server']}" in inst["tags"]
        assert inst["days_critical"] < inst["days_warning"]


def test_the_health_content_match_matches_the_body_the_app_actually_returns():
    doc = yaml.safe_load(_render("http_check.yaml.tpl", base_url="http://127.0.0.1:8000", acme_hosts=[]))
    pattern = next(i for i in doc["instances"] if i["name"] == "agnes_health_body")["content_match"]
    assert re.search(pattern, '{"status":"ok","vault_key_configured":true}')
    assert re.search(pattern, '{"status": "ok"}')
    assert not re.search(pattern, '{"status":"unhealthy","db_schema":"stale"}')


def test_no_customer_specific_content_leaks_into_the_public_module():
    for path in sorted(p for p in FILES.rglob("*") if p.is_file()):
        text = path.read_text()
        assert "datadoghq.eu" not in text, f"{path.name}: the site is a module input"
        assert "@slack-" not in text, f"{path.name}: notification targets belong to the consumer"


# --------------------------------------------------------------------------
# The renderer these tests lean on
# --------------------------------------------------------------------------


def test_renderer_matches_terraform_semantics():
    # `~}` eats the following spaces/tabs plus AT MOST ONE newline. Verified
    # byte-for-byte against `terraform console` on the real startup script; if
    # this drifts, every rendering assertion above quietly changes meaning.
    assert render_template("%{ if on ~}\n    body\n%{ endif ~}\nafter\n", {"on": True}) == ("    body\nafter\n")
    assert render_template("a\n%{ if on ~}\n%{ endif ~}\n\nb\n", {"on": True}) == "a\n\nb\n"
    # `%{~` trims backwards, and by the same "at most one newline" rule. This
    # case is here because the renderer got it wrong: Python's `$` also matches
    # just before a trailing newline, so re.sub fired twice on a run of blank
    # lines. Real terraform 1.14.3 renders "a\n\nb\n" for this input.
    assert render_template("a\n\n\n%{~ if on }b%{ endif }\n", {"on": True}) == "a\n\nb\n"
    # $${ and %%{ are the escapes for a literal ${ and %{.
    assert render_template("x=$${y//a/$Z} %%{ raw }", {}) == "x=${y//a/$Z} %{ raw }"
    # Two-variable map iteration, in key order.
    assert (
        render_template("%{ for k, v in m ~}\n[${k}=${v}]\n%{ endfor ~}\n", {"m": {"b": "2", "a": "1"}})
        == "[a=1]\n[b=2]\n"
    )
    # An empty collection still finds its endfor.
    assert render_template("%{ for x in xs ~}\n${x}\n%{ endfor ~}\ntail\n", {"xs": []}) == "tail\n"


def test_renderer_refuses_expressions_it_would_have_to_guess_at():
    from _tf_template import TemplateError

    with pytest.raises(TemplateError):
        render_template("${lookup(m, k, 1)}", {})
    with pytest.raises(TemplateError):
        render_template("${nope}", {})


def test_the_pg_role_bootstrap_never_re_modes_a_directory_it_does_not_own():
    """`install -d -m MODE` is not `mkdir -p`: it applies MODE to an ALREADY
    EXISTING directory. Pointed at /data/state — shared with the app and the
    state applier, and one of this feature's own probe paths — it silently made
    it 0700, which cut dd-agent out of the state-applier heartbeat while that
    directory's own `exists` service check stayed green."""
    sh = (FILES / "agnes-datadog-pg-role.sh").read_text()

    pw_file = re.search(r"^PW_FILE=(\S+)$", sh, re.M).group(1)
    assert not pw_file.startswith("/data/"), (
        "the monitoring password must not live under a directory shared with the app"
    )
    assert 'mkdir -p "$(dirname "$PW_FILE")"' in sh

    probe_dirs = {i["directory"] for i in yaml.safe_load((FILES / "conf.d/directory.yaml").read_text())["instances"]}
    for line in sh.splitlines():
        stripped = line.strip()
        if stripped.startswith(("install -d", "chmod ", "chown ")):
            for probe in probe_dirs:
                assert probe not in stripped, f"{stripped!r} changes a directory the agent has to be able to read"


def test_the_backup_probe_pattern_matches_a_path_the_backup_actually_writes():
    """The directory check fnmatches the file's FULL path and its path relative
    to `directory` — never the bare basename. A plain `STATUS` under a recursive
    walk matches nothing, and the probe then reports no metric rather than an
    error, so a backup that stops running looks exactly like a healthy one."""
    from fnmatch import fnmatch
    from os.path import relpath

    instances = yaml.safe_load((FILES / "conf.d/directory.yaml").read_text())["instances"]
    backup = next(i for i in instances if "agnes_probe:backup" in i["tags"])
    root, pattern = backup["directory"], backup["pattern"]

    status = f"{root}/20260903/STATUS"  # what agnes-db-backup.sh writes, last
    assert fnmatch(status, pattern) or fnmatch(relpath(status, root), pattern), (
        f"{pattern!r} matches neither {status!r} nor {relpath(status, root)!r}"
    )
    # ...and not the PG_STATUS sibling, which would double the gauges per day.
    pg = f"{root}/20260903/PG_STATUS"
    assert not (fnmatch(pg, pattern) or fnmatch(relpath(pg, root), pattern))


def test_every_other_probe_pattern_matches_the_file_it_names():
    from fnmatch import fnmatch
    from os.path import relpath

    expected = {
        "state_applier": "agnes-state-applier.tick",
        "auto_upgrade": "auto-upgrade.tick",
        "watchdog": "crash",
    }
    instances = yaml.safe_load((FILES / "conf.d/directory.yaml").read_text())["instances"]
    for probe, basename in expected.items():
        inst = next(i for i in instances if f"agnes_probe:{probe}" in i["tags"])
        path = f"{inst['directory']}/{basename}"
        assert fnmatch(path, inst["pattern"]) or fnmatch(relpath(path, inst["directory"]), inst["pattern"]), (
            f"{probe}: pattern {inst['pattern']!r} does not match {basename!r}"
        )


def test_no_shipped_script_uses_install_d():
    """`install -d -m MODE` applies MODE to an ALREADY EXISTING directory, so
    pointed at anything the module does not exclusively own it is a silent
    chmod on every run. `mkdir -p` is this module's idiom and cannot do that.
    One occurrence survived the first sweep — the agent package ships
    conf.d/postgres.d, and the timer re-moded it every 15 minutes."""
    module_scripts = [
        *(p for p in (MODULE / "files").rglob("*.sh")),
        MODULE / "startup-script.sh.tpl",
    ]
    for path in module_scripts:
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # the comments explain WHY it is not used
            assert "install -d" not in stripped, f"{path.relative_to(MODULE)}:{lineno} uses install -d; use mkdir -p"
