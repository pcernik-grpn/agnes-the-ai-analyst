"""Static + functional contract for the embedded kai-agent engine's
Terraform → startup plumbing.

Mirrors ``test_startup_data_apps_toggle.py`` (the per-VM toggle contract) and
``test_startup_dispatcher_pg_password.py`` (the executable pg-password block).
Pins the three-part infra contract so a rename or dropped template argument
can't silently break durable enablement:

* ``variables.tf`` carries ``kai_agent_enabled`` as a PER-VM field
  (``optional(bool, false)``) on both prod_instance and dev_instances — like
  ``dispatcher_enabled``, so enabling it targets one VM, never all of them —
  plus the instance-wide ``kai_agent_image`` / ``kai_agent_jwt_secret`` /
  ``kai_agent_e2b_key_secret`` / ``kai_agent_env`` variables;
* ``main.tf`` forwards ``each.value.kai_agent_enabled`` (per-VM) + the
  module-wide config into ``templatefile(...)``, and grants secretAccessor
  only when some instance enables the engine;
* ``startup-script.sh.tpl`` — ONLY when ``kai_agent_enabled`` — fetches the
  shared JWT secret + E2B key (loudly), mints/persists the engine Postgres
  password, writes the engine env + compose overlay, and emits
  ``KAI_HOST_JWT_SECRET`` (the app-side half of the shared-secret pair) into
  the app ``.env``. Disabled instances render a byte-identical ``.env``.
"""

import re
import shutil
import stat
import subprocess
from pathlib import Path

MODULE = Path("infra/modules/customer-instance")
TPL = MODULE / "startup-script.sh.tpl"

BEGIN = "# --- kai-agent-pg-password begin"
END = "# --- kai-agent-pg-password end"


def test_kai_agent_enabled_is_per_vm_field():
    body = (MODULE / "variables.tf").read_text()
    # Per-VM field on BOTH instance object types (like dispatcher_enabled), so
    # a dev-first enable can't flip prod. Exactly two declarations, optional.
    decls = re.findall(r"kai_agent_enabled\s*=\s*optional\(bool,\s*false\)", body)
    assert len(decls) == 2, f"expected kai_agent_enabled optional on prod+dev object types, got {len(decls)}"
    # NOT a module-global variable (that would enable every VM at once).
    assert not re.search(r'variable\s+"kai_agent_enabled"\s*\{', body)
    # Engine image + secrets + env map stay instance-wide variables.
    for name in ("kai_agent_image", "kai_agent_jwt_secret", "kai_agent_e2b_key_secret", "kai_agent_env"):
        assert re.search(r'variable\s+"' + name + r'"\s*\{', body), name


def test_main_tf_forwards_per_vm_toggle_into_templatefile():
    body = (MODULE / "main.tf").read_text()
    # Per-VM: read off each.value, mirroring dispatcher_enabled.
    assert re.search(r"kai_agent_enabled\s*=\s*each\.value\.kai_agent_enabled", body)
    assert not re.search(r"kai_agent_enabled\s*=\s*var\.kai_agent_enabled", body)
    assert re.search(r"kai_agent_image\s*=\s*var\.kai_agent_image", body)
    assert re.search(r"kai_agent_jwt_secret\s*=\s*var\.kai_agent_jwt_secret", body)
    assert re.search(r"kai_agent_e2b_key_secret\s*=\s*var\.kai_agent_e2b_key_secret", body)


def test_main_tf_grants_secret_access_conditionally():
    body = (MODULE / "main.tf").read_text()
    # secretAccessor only when some instance enables the engine, and secrets
    # already granted via runtime_secret_env OR runtime_secrets are
    # subtracted — the same (project, secret, role, member) binding declared
    # twice errors the apply.
    assert re.search(r"kai_agent_any_enabled\s*=\s*anytrue", body)
    assert "setsubtract" in body
    assert "setunion(toset(keys(var.runtime_secret_env)), toset(var.runtime_secrets))" in body
    assert re.search(r'"vm_kai_agent"\s*\{', body)
    # The VM must wait on the grant, or the boot-time fetch can 403 on IAM lag.
    assert "google_secret_manager_secret_iam_member.vm_kai_agent," in body


def test_tpl_env_block_guarded_by_toggle():
    body = TPL.read_text()
    # The app-side half of the shared secret + the compose-expanded values,
    # all inside `if kai_agent_enabled` blocks.
    for key in (
        "KAI_HOST_JWT_SECRET=$KAI_HOST_JWT_SECRET",
        "KAI_AGENT_PG_PASSWORD=$KAI_AGENT_PG_PASSWORD",
    ):
        assert key in body, key
    # Secret fetches fail LOUDLY (no ||-fallback) — an enabled engine without
    # them cannot serve a turn, so a visible boot failure beats a silent one.
    assert "KAI_HOST_JWT_SECRET=$(gcloud secrets versions access latest --secret=${kai_agent_jwt_secret})" in body
    assert "KAI_E2B_API_KEY=$(gcloud secrets versions access latest --secret=${kai_agent_e2b_key_secret})" in body
    # The engine Postgres data dir must be excluded from the blanket data-disk
    # chown (postgres runs as uid 70, the app as 999).
    assert "! -name kai-agent-postgres" in body
    # The overlay joins COMPOSE_FILE so auto-upgrade pulls + ups it too.
    assert "COMPOSE_FILE_VALUE=\"$COMPOSE_FILE_VALUE:docker-compose.kai-agent.yml\"" in body


def test_tpl_kai_blocks_are_toggle_gated():
    body = TPL.read_text()
    # Four positive `if kai_agent_enabled` blocks (the 4c setup block, the
    # .env keys, the overlay strip before the strict base up, and the
    # tolerant engine up after it), only positive guards, so a default
    # instance renders none of it — in particular it never pulls the engine
    # image.
    assert body.count("%{ if kai_agent_enabled ~}") == 4
    assert "!kai_agent_enabled" not in body
    # The password prep must precede its use in the .env heredoc.
    assert body.index("KAI_AGENT_PG_PASSWORD=$(openssl") < body.index("KAI_AGENT_PG_PASSWORD=$KAI_AGENT_PG_PASSWORD")


def test_tpl_engine_failure_cannot_gate_the_machine():
    body = TPL.read_text()
    # The engine's one-shot migrate is a hard start condition for the engine
    # SERVICE only. Boot pulls + starts the base stack STRICTLY with the
    # overlay stripped, then brings the engine up tolerantly — a broken
    # engine image or failed migration must not abort the script before the
    # auto-upgrade cron and watchdog sections.
    strip = body.index('export COMPOSE_FILE="$${COMPOSE_FILE%:docker-compose.kai-agent.yml}"')
    strict_up = body.index("if docker compose $COMPOSE_PROFILES_ARG up -d; then")
    restore = body.index('export COMPOSE_FILE="$KAI_FULL_COMPOSE_FILE"')
    cron = body.index("--- 6. Auto-upgrade via cron")
    assert strip < strict_up < restore < cron
    # The tolerant block warns instead of exiting.
    assert "WARN: kai-agent engine sidecar failed to pull or start" in body
    # ...and is TARGETED at the engine services + gated on materialization: a
    # full-list pull/up would fetch and start the whole stack a second time
    # on every boot and blame the engine for unrelated base-stack failures,
    # and a skipped boot has no overlay to bring up.
    assert "pull kai-agent kai-agent-migrate kai-agent-pg" in body
    tolerant_up = body.index("up -d kai-agent", restore)
    assert restore < tolerant_up < cron
    assert body.rindex('if [ "$KAI_AGENT_MATERIALIZE" = "1" ]; then') < restore
    # The strip only works while the overlay is appended LAST — keep it last.
    assert body.index("COMPOSE_FILE_VALUE=\"$COMPOSE_FILE_VALUE:docker-compose.kai-agent.yml\"") < strip


def test_tpl_engine_requires_public_origin():
    body = TPL.read_text()
    # An enabled engine on a VM whose SERVER_URL could not be resolved must
    # neither configure HOST_BROKER_LLM_URL as the meaningless
    # "/api/broker/anthropic" NOR abort the boot (SERVER_URL emptiness can be
    # a transient metadata blip, and the engine must never turn a degraded
    # add-on into an unprovisioned machine): the engine materialization is
    # SKIPPED for that boot with a loud warning, and the guard opens before
    # the URL is used.
    guard = body.index("WARN: kai-agent engine SKIPPED this boot")
    gate = body.index('if [ "$KAI_AGENT_MATERIALIZE" = "1" ]; then')
    url_use = body.index("HOST_BROKER_LLM_URL=$SERVER_URL/api/broker/anthropic")
    assert guard < gate < url_use
    # The transient case is retried in-place BEFORE concluding skip — a
    # skipped boot costs the engine until reboot (teardown + overlay drop),
    # so a mere metadata blip must get a second chance.
    retry = body.index("for _kai_ip_try in 1 2 3")
    assert retry < guard
    # The whole materialization (env, overlay, COMPOSE_FILE append) sits
    # inside the gate, so a skipped boot references no overlay.
    assert gate < body.index('COMPOSE_FILE_VALUE="$COMPOSE_FILE_VALUE:docker-compose.kai-agent.yml"')
    # No hard abort anywhere in the engine block.
    assert "ERROR: kai_agent_enabled requires" not in body
    # A caddy VM without a domain derives http://<ip>:8000, which its
    # firewall does not expose — an origin that RESOLVES but is unreachable
    # from the sandbox must be skipped as loudly as the empty one.
    closed_port_guard = body.index("raw :8000 shape on a tls_mode=caddy VM")
    assert guard < closed_port_guard < url_use


def test_engine_caps_are_per_vm_fields():
    # The engine's resource ceilings must be per-VM TF fields (like
    # app_mem_limit), NOT .env hand-edits: the startup script rewrites .env
    # from scratch every boot, so a hand-raised ceiling silently drops back
    # to the default on reboot.
    vbody = (MODULE / "variables.tf").read_text()
    for name in ("kai_agent_mem_limit", "kai_agent_cpus", "kai_agent_pg_mem_limit"):
        decls = re.findall(name + r"\s*=\s*optional\(string,", vbody)
        assert len(decls) == 2, f"{name}: expected on prod+dev object types, got {len(decls)}"
    mbody = (MODULE / "main.tf").read_text()
    assert re.search(r"kai_agent_mem_limit\s*=\s*each\.value\.kai_agent_mem_limit", mbody)
    tbody = TPL.read_text()
    for line in (
        "KAI_AGENT_MEM_LIMIT=${kai_agent_mem_limit}",
        "KAI_AGENT_CPUS=${kai_agent_cpus}",
        "KAI_AGENT_PG_MEM_LIMIT=${kai_agent_pg_mem_limit}",
    ):
        assert line in tbody, line


def test_broker_mcp_toggle_is_per_vm_field():
    # The engine→instance MCP surface is opt-in PER VM (like
    # kai_agent_enabled itself), not module-global: it hands the engine's
    # sandbox the caller's tool surface, so a dev-first enable must not
    # widen prod.
    vbody = (MODULE / "variables.tf").read_text()
    decls = re.findall(r"kai_agent_broker_mcp_enabled\s*=\s*optional\(bool,\s*false\)", vbody)
    assert len(decls) == 2, f"expected kai_agent_broker_mcp_enabled optional on prod+dev object types, got {len(decls)}"
    assert not re.search(r'variable\s+"kai_agent_broker_mcp_enabled"\s*\{', vbody)
    mbody = (MODULE / "main.tf").read_text()
    assert re.search(r"kai_agent_broker_mcp_enabled\s*=\s*each\.value\.kai_agent_broker_mcp_enabled", mbody)


def test_tpl_broker_mcp_halves_are_flag_gated_and_paired():
    body = TPL.read_text()
    # One flag renders BOTH halves of a pair that only works together: the
    # engine-side broker URL and the app-side ticket-scope switch. Either
    # half alone is a silent runtime failure (URL without the scope 503s
    # every tool call; scope without the URL never registers the server).
    assert body.count("%{ if kai_agent_broker_mcp_enabled ~}") == 2
    assert "!kai_agent_broker_mcp_enabled" not in body
    url_line = "HOST_BROKER_MCP_URL=$SERVER_URL/api/kai/mcp"
    app_line = "KAI_BROKER_MCP_ENABLED=true"
    assert url_line in body
    assert app_line in body
    # The E2B sandbox egresses to the MCP broker from the public internet —
    # the URL must ride the PUBLIC origin (SERVER_URL), same as the LLM
    # broker line, never the compose-DNS app address.
    assert "HOST_BROKER_MCP_URL=http://app:8000" not in body
    # Engine half: inside the materialization gate (where SERVER_URL is
    # verified non-empty) and BEFORE the kai_agent_env append, so the
    # caller's map can still override the derived URL (env_file last-wins).
    gate = body.index('if [ "$KAI_AGENT_MATERIALIZE" = "1" ]; then')
    assert gate < body.index(url_line) < body.index('echo "${kai_agent_env_b64}" | base64 -d')
    # App half: inside the app .env's kai block, so a VM without the engine
    # renders neither line.
    kai_block_start = body.index("KAI_HOST_JWT_SECRET=$KAI_HOST_JWT_SECRET", body.index('cat > "$APP_DIR/.env" <<ENVEOF'))
    assert kai_block_start < body.index(app_line) < body.index("COMPOSE_FILE=$COMPOSE_FILE_VALUE")


def test_kai_agent_env_rejects_multiline_values():
    body = (MODULE / "variables.tf").read_text()
    # The map becomes KEY=VALUE lines in the engine's env_file; an embedded
    # newline truncates the value and corrupts the next line — the engine
    # then silently never starts. Must fail the plan, not the runtime.
    assert 'for k, v in var.kai_agent_env' in body
    assert '!strcontains(v, "\\n")' in body
    assert '!strcontains(k, "=")' in body


def test_main_tf_precondition_covers_required_engine_env():
    body = (MODULE / "main.tf").read_text()
    # Enabling the flag with an empty kai_agent_env plans cleanly but
    # crash-loops the engine at runtime (its env validation requires the
    # identity and provider keys) — the precondition must catch it at plan
    # time, like the dispatcher's policies check.
    assert 'contains(keys(var.kai_agent_env), "HOST_AGENT_IDENTITY")' in body
    assert 'contains(keys(var.kai_agent_env), "CLOUD_LLM_PROVIDER")' in body
    # The upstream pair is required only for the anthropic provider.
    assert 'lookup(var.kai_agent_env, "CLOUD_LLM_PROVIDER", "") != "anthropic"' in body
    assert 'contains(keys(var.kai_agent_env), "ANTHROPIC_UPSTREAM_API_KEY")' in body


def test_tpl_registry_helper_is_best_effort_and_precise():
    body = TPL.read_text()
    # configure-docker must not gate the machine under `set -e` — a failure
    # warns, and a genuine credential problem surfaces at the tolerant pull.
    assert "gcloud auth configure-docker $KAI_AGENT_IMAGE_HOST failed" in body
    # Match Artifact Registry hosts precisely (<region>-docker.pkg.dev),
    # not any host that merely ends in "pkg.dev".
    assert "*-docker.pkg.dev)" in body


def test_auto_upgrade_tick_cannot_be_gated_by_the_engine():
    # The recurring tick recreates with the FULL COMPOSE_FILE under `set -e`;
    # without the split a broken engine image aborts every tick before the
    # config marker is written and the self-update runs. The single-container
    # branch must mirror the startup script: base strictly (overlay filtered
    # out), engine tolerantly.
    body = Path("scripts/ops/agnes-auto-upgrade.sh").read_text()
    assert ":docker-compose.kai-agent.yml:" in body
    assert "grep -vx 'docker-compose.kai-agent.yml'" in body
    assert "WARN: kai-agent engine sidecar failed to start; base stack recreated" in body


def test_auto_upgrade_tick_retries_a_downed_engine_every_tick():
    # The drift-gated recreate never fires on a no-change tick, so an engine
    # that failed at boot would stay down indefinitely without this: a
    # tolerant, ENGINE-TARGETED `up -d kai-agent` runs on every tick the
    # engine is found down, placed BEFORE the drift detection. Targeting the
    # service (not the full list) is load-bearing — a full-list up here would
    # recreate drifted app services and bypass the sync-defer guard. So is
    # the running-check gate: an unconditional up would re-run the one-shot
    # migrator (service_completed_successfully dependency) every 5 minutes
    # forever on a healthy box.
    body = Path("scripts/ops/agnes-auto-upgrade.sh").read_text()
    gate = body.index("ps -q --status running kai-agent")
    retry = body.index("up -d kai-agent >/dev/null")
    assert "retrying next tick" in body
    assert gate < retry < body.index("Drift-based change detection")
    # Role-split VMs: the rolling recreate never touches the engine and the
    # every-tick retry only starts a DOWN one, so the role-split branch must
    # recreate the engine itself or an image bump never lands via the tick.
    role_split_abort = body.index("role-split rolling recreate ABORTED")
    engine_after_rollout = body.index("up -d kai-agent", role_split_abort)
    assert engine_after_rollout < body.index("elif [[ \":$COMPOSE_FILE:\"", role_split_abort)


def test_tpl_skipped_boot_tears_down_stale_engine_containers():
    # A boot that SKIPS materialization on a VM where a previous boot did
    # materialize would otherwise leave the old containers running under
    # restart:always with a stale env (stale broker URL / rotated secret),
    # outside compose management — the rewritten .env drops the overlay from
    # COMPOSE_FILE, so neither the tick's retry nor any compose invocation
    # sees them again. The skip path must converge to a clean "engine off".
    body = TPL.read_text()
    # `rm -sf` and not `down`: down also removes the project's default
    # network, which the base stack still holds — the removal fails with
    # "active endpoints" and a false WARN on a teardown that succeeded.
    teardown = body.index("docker compose -f docker-compose.kai-agent.yml rm -sf")
    assert "docker compose -f docker-compose.kai-agent.yml down" not in body
    assert '[ "$KAI_AGENT_MATERIALIZE" = "0" ]' in body
    assert 'rm -f "$APP_DIR/docker-compose.kai-agent.yml"' in body
    # The teardown must run BEFORE the .env rewrite (the old .env still holds
    # the overlay's interpolation variables) and before the materialize gate.
    assert teardown < body.index('if [ "$KAI_AGENT_MATERIALIZE" = "1" ]; then')
    assert teardown < body.index('cat > "$APP_DIR/.env" <<ENVEOF')


def test_tpl_engine_services_are_bounded():
    body = TPL.read_text()
    # Caps + hardening, same posture as the app/scheduler caps and the
    # data-app container hardening: overridable ceilings via .env, and
    # no-new-privileges on every engine service.
    assert "mem_limit: $${KAI_AGENT_MEM_LIMIT:-2g}" in body
    assert "cpus: $${KAI_AGENT_CPUS:-1.0}" in body
    assert "mem_limit: $${KAI_AGENT_PG_MEM_LIMIT:-1g}" in body
    assert "pids_limit: 512" in body
    kai_yaml = body[body.index("<<'KAIYAML'") : body.index("\nKAIYAML")]
    assert kai_yaml.count("no-new-privileges:true") == 3


def test_tpl_engine_env_derivation():
    body = TPL.read_text()
    # Sandbox-facing broker URL rides the PUBLIC origin; server-to-server
    # fetches ride compose DNS to the app (no TLS hairpin). Issuer/audience
    # mirror the app-side defaults in app/api/kai.py.
    assert "HOST_BROKER_LLM_URL=$SERVER_URL/api/broker/anthropic" in body
    assert "HOST_BROKER_TICKET_URL=http://app:8000/api/kai/tickets" in body
    assert "HOST_WORKSPACE_URL=http://app:8000/api/kai/workspace" in body
    assert "HOST_JWT_ISSUER=agnes\n" in body
    assert "HOST_JWT_AUDIENCE=kai-agent\n" in body
    # Caller env is appended AFTER the derived lines (env_file last-wins).
    assert body.index("E2B_API_KEY=$KAI_E2B_API_KEY") < body.index('echo "${kai_agent_env_b64}" | base64 -d')
    # The one-shot migrate runs from the engine's app dir (its migrator
    # resolves ./drizzle relative to the cwd) and gates the engine start.
    assert "working_dir: /app/apps/kai-agent" in body
    assert 'command: ["node", "dist/db/migrate.js"]' in body
    assert "condition: service_completed_successfully" in body


# --- functional: the marker-delimited pg-password block, executed verbatim ---


def _password_block() -> str:
    tpl = TPL.read_text()
    m = re.search(re.escape(BEGIN) + r".*?\n(.*?)" + re.escape(END), tpl, re.DOTALL)
    assert m, (
        "startup-script.sh.tpl must contain the marker-delimited "
        f"kai-agent-pg-password block ({BEGIN!r} ... {END!r}) — the "
        "functional tests below execute it"
    )
    block = m.group(1)
    assert "${" not in block and "%{" not in block, (
        "kai-agent-pg-password block must not use Terraform interpolation "
        "('${' / '%{'); keep it plain bash so tests execute the shipped code "
        "verbatim"
    )
    return block


def _run_block(
    tmp_path: Path, env_content: str | None = None, keyfile_content: str | None = None
) -> tuple[str, Path, Path]:
    """Execute the template's kai-agent-pg-password block against a sandbox.

    Returns (captured KAI_AGENT_PG_PASSWORD value, keyfile path, .env path).
    """
    bash = shutil.which("bash")
    assert bash, "bash required"
    app_dir = tmp_path / "app"
    data_mnt = tmp_path / "data"
    app_dir.mkdir(exist_ok=True)
    data_mnt.mkdir(exist_ok=True)
    if env_content is not None:
        (app_dir / ".env").write_text(env_content)
    keyfile = data_mnt / "state" / "kai-agent-pg-password"
    if keyfile_content is not None:
        keyfile.parent.mkdir(parents=True, exist_ok=True)
        keyfile.write_text(keyfile_content)
    script = (
        "set -euo pipefail\n"
        f'APP_DIR="{app_dir}"\n'
        f'DATA_MNT="{data_mnt}"\n' + _password_block() + '\nprintf "%s" "$KAI_AGENT_PG_PASSWORD"\n'
    )
    proc = subprocess.run([bash, "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, f"kai-agent-pg-password block failed: {proc.stderr}"
    return proc.stdout, keyfile, app_dir / ".env"


def test_fresh_boot_mints_password_and_persists_it(tmp_path):
    password, keyfile, _ = _run_block(tmp_path)
    assert password, "a password must be minted"
    assert keyfile.read_text().strip() == password
    mode = stat.S_IMODE(keyfile.stat().st_mode)
    assert mode == 0o600, f"keyfile must be 0600, got {oct(mode)}"


def test_keyfile_is_not_inside_the_postgres_data_dir(tmp_path):
    """postgres:16-alpine's initdb aborts on first boot if PGDATA (bind-mounted
    from $DATA_MNT/kai-agent-postgres) contains anything but "lost+found" —
    the keyfile must live outside that directory or the engine DB never
    starts on a fresh machine."""
    _, keyfile, _ = _run_block(tmp_path)
    pgdata = tmp_path / "data" / "kai-agent-postgres"
    assert pgdata not in keyfile.parents and keyfile != pgdata, (
        f"keyfile {keyfile} must not live inside the Postgres data dir {pgdata}"
    )


def test_reboot_preserves_existing_keyfile(tmp_path):
    """A VM reboot (data disk persists) must not re-mint the password —
    doing so would desync it from the already-initialized engine database."""
    existing = "existing-hex-password"
    password, keyfile, _ = _run_block(tmp_path, keyfile_content=existing + "\n")
    assert password == existing
    assert keyfile.read_text().strip() == existing


def test_hand_added_env_password_is_adopted_into_keyfile(tmp_path):
    """A password already present in .env (e.g. hand-provisioned before the
    module rollout) is adopted into the durable keyfile rather than being
    clobbered by a fresh mint."""
    existing = "legacy-env-password"
    env = f"JWT_SECRET_KEY=x\nKAI_AGENT_PG_PASSWORD={existing}\nDATA_DIR=/data\n"
    password, keyfile, _ = _run_block(tmp_path, env_content=env)
    assert password == existing
    assert keyfile.read_text().strip() == existing


def test_keyfile_wins_over_env(tmp_path):
    """VM recreate: the boot disk's .env is freshly templated (or stale) —
    the persistent-disk keyfile paired with the surviving engine DB must
    take precedence, or the engine can no longer authenticate to it."""
    file_password = "data-disk-password"
    env_password = "stale-boot-disk-password"
    password, _, _ = _run_block(
        tmp_path,
        env_content=f"KAI_AGENT_PG_PASSWORD={env_password}\n",
        keyfile_content=file_password + "\n",
    )
    assert password == file_password


def test_two_runs_are_stable(tmp_path):
    first, keyfile, _ = _run_block(tmp_path)
    second, _, _ = _run_block(tmp_path, keyfile_content=keyfile.read_text())
    assert first == second


def test_the_documented_half_configured_failure_code_matches_the_code():
    """The operator-facing text must name the status an operator will actually
    see, because it is the string they grep when a pairing is half-configured.

    `_require_mcp_surface` (`app/api/kai.py`) is declared AHEAD of the ticket
    dependency precisely so an unconfigured instance answers 503
    `kai_mcp_not_enabled` rather than `401 missing_broker_ticket` — its own
    docstring records that 401 was the pre-fix behavior. Two tests in
    `tests/test_kai_host.py` pin the 503. So a doc claiming 401 describes a
    surface that no longer exists.
    """
    from pathlib import Path

    handler = Path("app/api/kai.py").read_text()
    surface = handler[handler.index("def _require_mcp_surface") :]
    surface = surface[: surface.index("\n@router")]
    assert 'status_code=503, detail="kai_mcp_not_enabled"' in surface, (
        "the surface's refusal changed — update the docs this test guards"
    )

    vbody = (MODULE / "variables.tf").read_text()
    block = vbody[: vbody.index("kai_agent_broker_mcp_enabled = optional")]
    block = block[-900:]
    assert "401" not in block, "the flag's own comment names a status the surface does not return"
    assert "kai_mcp_not_enabled" in block

    changelog = Path("CHANGELOG.md").read_text()
    entry = changelog[changelog.index("per-VM `kai_agent_broker_mcp_enabled` flag") :][:2000]
    assert "401s every tool call" not in entry
    assert "kai_mcp_not_enabled" in entry


def test_kai_agent_env_docs_point_at_the_flag_not_the_manual_pairing():
    """`kai_agent_env` used to instruct operators to hand-set
    HOST_BROKER_MCP_URL "only when the instance also enables the app-side
    switch" — a pairing the module offered no way to complete until this
    flag existed. It must point at the flag, not at the dead manual path."""
    vbody = (MODULE / "variables.tf").read_text()
    block = vbody[vbody.index('variable "kai_agent_env"') :]
    block = block[: block.index("type ")]
    assert "kai_agent_broker_mcp_enabled" in block, (
        "the kai_agent_env docs must point at the flag that completes the pairing"
    )
    assert "only when the instance also enables the app-side" not in block
