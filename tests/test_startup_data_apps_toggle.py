"""Static contract for the data-apps enablement's Terraform → startup plumbing.

Mirrors ``test_startup_studio_toggle.py``. Pins the three-part infra contract so
a rename or dropped template argument can't silently break durable enablement:

* ``variables.tf`` carries ``data_apps_enabled`` as a PER-VM field
  (``optional(bool, false)``) on both prod_instance and dev_instances — like
  ``dispatcher_enabled``, so enabling it targets one VM, never all of them —
  plus the instance-wide ``data_apps_runtime_image`` variable;
* ``main.tf`` forwards ``each.value.data_apps_enabled`` (per-VM) +
  ``var.data_apps_runtime_image`` into ``templatefile(...)``;
* ``startup-script.sh.tpl`` — ONLY when ``data_apps_enabled`` — mints/persists
  ``APPS_RUNNER_TOKEN``, resolves ``DOCKER_GID`` from the docker socket, and
  emits ``COMPOSE_PROFILES=apps`` + ``AGNES_DATA_APPS_ENABLED=true`` (+ the
  runner token/prefix/gid) into the app ``.env``. Disabled instances render a
  byte-identical ``.env`` (the whole block is absent).
"""

import re
from pathlib import Path

MODULE = Path("infra/modules/customer-instance")


def test_data_apps_enabled_is_per_vm_field():
    body = (MODULE / "variables.tf").read_text()
    # Per-VM field on BOTH instance object types (like dispatcher_enabled), so a
    # dev-first enable can't flip prod. Exactly two declarations, both optional.
    decls = re.findall(r"data_apps_enabled\s*=\s*optional\(bool,\s*false\)", body)
    assert len(decls) == 2, f"expected data_apps_enabled optional on prod+dev object types, got {len(decls)}"
    # NOT a module-global variable (that would enable every VM at once).
    assert not re.search(r'variable\s+"data_apps_enabled"\s*\{', body)
    # runtime image stays an instance-wide variable.
    assert re.search(r'variable\s+"data_apps_runtime_image"\s*\{', body)


def test_main_tf_forwards_per_vm_toggle_into_templatefile():
    body = (MODULE / "main.tf").read_text()
    # Per-VM: read off each.value, mirroring dispatcher_enabled.
    assert re.search(r"data_apps_enabled\s*=\s*each\.value\.data_apps_enabled", body)
    assert not re.search(r"data_apps_enabled\s*=\s*var\.data_apps_enabled", body)
    assert re.search(r"data_apps_runtime_image\s*=\s*var\.data_apps_runtime_image", body)


def test_tpl_env_block_guarded_by_toggle():
    body = (MODULE / "startup-script.sh.tpl").read_text()
    # The .env keys the feature needs, all inside a single `if data_apps_enabled`.
    for key in (
        "AGNES_DATA_APPS_ENABLED=true",
        "APPS_RUNNER_TOKEN=$APPS_RUNNER_TOKEN",
        "DOCKER_GID=$DOCKER_GID",
    ):
        assert key in body, key
    # The `apps` profile is a --profile FLAG, never COMPOSE_PROFILES in .env:
    # compose ignores that env var whenever any --profile flag (e.g. tls) is
    # present, so an .env COMPOSE_PROFILES=apps would be dropped on TLS instances.
    assert "COMPOSE_PROFILES_ARG --profile apps" in body
    assert "COMPOSE_PROFILES=apps" not in body
    # No unconditional AGNES_DATA_APPS_ENABLED leak.
    assert body.count("AGNES_DATA_APPS_ENABLED=true") == 1
    # Token minted with the same read-back-then-openssl pattern as the scheduler token.
    assert "APPS_RUNNER_TOKEN=$(openssl rand -hex 32)" in body
    # DOCKER_GID resolved from the socket (so uid 999 can reach the daemon).
    assert "stat -c '%g' /var/run/docker.sock" in body


def test_tpl_data_apps_blocks_are_toggle_gated():
    body = (MODULE / "startup-script.sh.tpl").read_text()
    # Four data-apps-only blocks (the runtime-image half of the sidecar prep,
    # the .env keys, the runtime-image pre-pull, and the container-metadata-
    # hardening firewall rule) — so a default instance renders none of it, and
    # in particular never spends boot time, bandwidth or disk pulling a ~1.3 GB
    # image, nor touches iptables, for a feature it does not run.
    assert body.count("%{ if data_apps_enabled ~}") == 4
    # Two blocks are SHARED with chat.provider=docker, which spawns its
    # sandboxes through the same apps-runner: the token/DOCKER_GID prep and the
    # --profile apps flag. Either feature alone must render them.
    assert body.count('%{ if data_apps_enabled || chat_provider == "docker" ~}') == 2
    # The one negated guard exists solely to keep APPS_RUNNER_TOKEN/DOCKER_GID
    # from being written to .env twice when BOTH features are on — never to
    # gate data-apps behavior itself.
    assert body.count("!data_apps_enabled") == 1
    assert '%{ if chat_provider == "docker" && !data_apps_enabled ~}' in body
    # The APPS_RUNNER_TOKEN prep must precede its use in the .env heredoc.
    assert body.index("APPS_RUNNER_TOKEN=$(openssl") < body.index("APPS_RUNNER_TOKEN=$APPS_RUNNER_TOKEN")


def test_auto_upgrade_appends_apps_profile_flag():
    # The recurring upgrade tick must also add `--profile apps` (not rely on
    # COMPOSE_PROFILES) so the sidecar survives upgrades on TLS instances.
    body = Path("scripts/ops/agnes-auto-upgrade.sh").read_text()
    assert "AGNES_DATA_APPS_ENABLED" in body
    assert "PROFILE_ARGS+=( --profile apps )" in body
    # ...and the tls append must be `+=`, not `=`, or it would clobber the
    # COMPOSE_PROFILES-folded flags below.
    assert "PROFILE_ARGS+=( --profile tls )" in body
    assert "PROFILE_ARGS=( --profile tls )" not in body


def test_auto_upgrade_folds_compose_profiles_into_flags():
    # Any COMPOSE_PROFILES from .env (e.g. mtier) is converted to --profile flags
    # so adding --profile tls/apps never silently drops it (compose ignores the
    # env var once any --profile flag is present).
    body = Path("scripts/ops/agnes-auto-upgrade.sh").read_text()
    assert "IFS=',' read -ra _cp_list <<< \"$COMPOSE_PROFILES\"" in body
    assert 'PROFILE_ARGS+=( --profile "$_cp" )' in body


# --- subdomain base (origin isolation) -------------------------------------
# `data_apps_subdomain_base` is what moves apps off the main origin. It rides
# the SAME per-VM plumbing as `data_apps_enabled` above, and its env line must
# stay nested inside the enabled block: the value widens the session cookie to
# the base's parent domain, so it must never reach a VM whose data-apps feature
# is off.


def test_subdomain_base_is_per_vm_field():
    body = (MODULE / "variables.tf").read_text()
    decls = re.findall(r'data_apps_subdomain_base\s*=\s*optional\(string,\s*""\)', body)
    assert len(decls) == 2, f"expected the base optional on prod+dev object types, got {len(decls)}"
    # Not a module-global — one base for every VM would force a shared cookie
    # parent across instances that may not share a domain.
    assert not re.search(r'variable\s+"data_apps_subdomain_base"\s*\{', body)


def test_main_tf_forwards_subdomain_base_per_vm():
    body = (MODULE / "main.tf").read_text()
    assert re.search(r"data_apps_subdomain_base\s*=\s*each\.value\.data_apps_subdomain_base", body)
    assert not re.search(r"data_apps_subdomain_base\s*=\s*var\.data_apps_subdomain_base", body)


def test_tpl_subdomain_base_env_line_is_conditional_and_nested():
    body = (MODULE / "startup-script.sh.tpl").read_text()
    assert "AGNES_DATA_APPS_SUBDOMAIN_BASE=${data_apps_subdomain_base}" in body
    # Empty value writes NO env line: an empty-but-set var would hand the app
    # an empty base, and `""` already means path-prefix mode there.
    assert re.search(
        r'%\{ if data_apps_subdomain_base != "" ~\}\s*\n'
        r"AGNES_DATA_APPS_SUBDOMAIN_BASE=\$\{data_apps_subdomain_base\}",
        body,
    )
    # Nested INSIDE the `if data_apps_enabled` block — slice from that opener to
    # its matching close and assert the line lives in there.
    start = body.index("%{ if data_apps_enabled ~}\nAGNES_DATA_APPS_ENABLED=true")
    end = body.index("%{ endif ~}", body.index("DOCKER_GID=$DOCKER_GID", start))
    assert "AGNES_DATA_APPS_SUBDOMAIN_BASE" in body[start:end], (
        "the base env line must sit inside the data_apps_enabled block — it widens "
        "the session cookie and must not reach a VM with the feature off"
    )


# --- Caddy: per-app certificates (on-demand TLS) ---------------------------
# The app refuses main-origin serving, so apps are only reachable once Caddy
# terminates TLS for `*.<base>`. We issue one cert per hostname rather than a
# wildcard, because a wildcard needs DNS-01 — a DNS-zone credential on the host
# that runs user-authored code.

ASK_PATH = "/api/data-apps-tls-check"


def test_apps_subdomain_vhost_ships_in_the_image():
    """The VM extracts host artifacts from the image and downloads nothing at
    boot, so a file the Dockerfile does not bake never reaches the box."""
    dockerfile = Path("Dockerfile").read_text()
    assert "Caddyfile.apps-subdomain" in dockerfile


def test_vhost_uses_on_demand_not_a_wildcard_cert():
    body = Path("deploy/caddy/Caddyfile.apps-subdomain").read_text()
    assert "*.{$APPS_SUBDOMAIN_BASE}" in body
    assert re.search(r"tls\s*\{\s*on_demand\s*\}", body), "per-app certs, not a wildcard"


def test_tpl_declares_the_ask_endpoint_and_it_matches_the_route():
    """The `ask` URL is a contract with a real FastAPI route — a rename on
    either side silently turns every certificate request into a refusal."""
    tpl = (MODULE / "startup-script.sh.tpl").read_text()
    assert "on_demand_tls" in tpl
    assert ASK_PATH in tpl
    route = Path("app/api/data_apps_proxy.py").read_text()
    assert f'@router.get("{ASK_PATH}"' in route, "ask URL and route path drifted apart"


def test_tpl_caddy_wiring_is_conditional_and_idempotent():
    tpl = (MODULE / "startup-script.sh.tpl").read_text()
    # Only when a base is configured: an empty value would render the site
    # address `*.` and Caddy refuses to start on it.
    assert re.search(r'if \[ -n "\$APPS_SUBDOMAIN_BASE" \]', tpl)
    # The startup script runs on EVERY boot. Prepending the global options
    # block twice is a Caddyfile Caddy will not parse.
    assert "on_demand_tls" in tpl and re.search(r"grep -q .*on_demand_tls", tpl), (
        "the Caddyfile edit must be guarded so a second boot cannot duplicate it"
    )


# ---------------------------------------------------------------------------
# The wiring has to survive the 5-minute upgrade tick, not just the boot.
# `agnes-auto-upgrade.sh` re-fetches a PRISTINE Caddyfile from main on every
# tick (it is in CONFIG_FILES), so wiring it only at boot meant a VM lost its
# `*.<base>` vhost within five minutes of coming up — and hosted apps, refused
# on the main origin by default, became unreachable entirely.
# ---------------------------------------------------------------------------

UPGRADE = Path("scripts/ops/agnes-auto-upgrade.sh")
_BLOCK_RE = r"^# --- apps-subdomain-caddy begin.*?$\n.*?^# --- apps-subdomain-caddy end ---$"


def _block(path: Path) -> str:
    m = re.search(_BLOCK_RE, path.read_text(), re.S | re.M)
    assert m, f"apps-subdomain-caddy markers missing from {path}"
    return m.group(0)


def test_upgrade_tick_refetches_the_caddyfile():
    """The premise of the two tests below. If the Caddyfile ever stops being
    re-fetched every tick, the mirrored block becomes dead weight rather than a
    fix, and this test should be the one that says so."""
    body = UPGRADE.read_text()
    files = re.search(r"CONFIG_FILES=\((.*?)\)", body, re.S)
    assert files and "Caddyfile" in files.group(1)


def test_caddy_wiring_block_is_mirrored_into_the_upgrade_tick():
    """Byte-identical, deliberately: the docker test validates ONE copy through
    Caddy's own parser, and this equality is what makes that verdict cover both.
    Editing one copy without the other fails here."""
    assert _block(MODULE / "startup-script.sh.tpl") == _block(UPGRADE)


def test_upgrade_tick_wires_caddy_before_hashing_config():
    """Order matters twice over. The drift hash must describe the file Caddy
    will actually load, and adding/clearing APPS_SUBDOMAIN_BASE in .env must
    move that hash — the recreate it triggers is what puts the vhost into
    effect without a reboot."""
    body = UPGRADE.read_text()
    end = body.index("# --- apps-subdomain-caddy end ---")
    assert end < body.index("CONFIG_AFTER=$(hash_config_files)")
    # And the base comes from .env via the safe reader, not a bash `source`.
    assert 'APPS_SUBDOMAIN_BASE="$(_env_get APPS_SUBDOMAIN_BASE)"' in body
    assert body.index('_env_get() {') < body.index('_env_get APPS_SUBDOMAIN_BASE')
