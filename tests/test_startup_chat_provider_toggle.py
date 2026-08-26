"""Static contract for the chat-provider pin's Terraform → startup plumbing.

``chat_provider`` codifies which engine runs an instance's web-chat sessions
IN TERRAFORM: the per-VM module field writes ``AGNES_CHAT_PROVIDER`` into the
app ``.env``, and ``load_chat_config`` resolves env > instance.yaml >
"kai-agent".
Without it the provider choice lives only in the hand-edited instance.yaml
overlay on the data disk — which survives reboots and recreates but not a
fresh data disk, and is invisible in review.

Same read-the-template pattern as ``test_startup_experience_toggle.py``,
whose field this one mirrors — per-VM (dev-first rollout), empty default
writes NO env line (an ``AGNES_CHAT_PROVIDER=`` empty line is treated as
unset by the resolver, but writing it anyway would imply a pin that does not
exist), and the Terraform allowlist must track the app's boot allowlist in
``app/main.py`` so a typo fails the plan instead of refusing the ChatManager
at runtime on the VM.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path("infra/modules/customer-instance")


def _object_type_blocks(body: str) -> list[str]:
    """Return the prod_instance + dev_instances object-type declarations."""
    blocks = []
    for var in ("prod_instance", "dev_instances"):
        m = re.search(rf'variable\s+"{var}"\s*\{{', body)
        assert m, f"variables.tf must declare {var}"
        depth, i = 1, m.end()
        while i < len(body) and depth:
            if body[i] == "{":
                depth += 1
            elif body[i] == "}":
                depth -= 1
            i += 1
        blocks.append(body[m.start() : i])
    return blocks


def test_both_object_types_declare_the_chat_provider_field():
    body = (MODULE / "variables.tf").read_text()
    decls = re.findall(r'chat_provider\s*=\s*optional\(string,\s*""\)', body)
    assert len(decls) == 2, f"expected chat_provider optional on prod+dev object types, got {len(decls)}"
    # NOT a module-global variable (a provider pin is a per-VM rollout choice).
    assert not re.search(r'variable\s+"chat_provider"\s*\{', body)


def test_main_tf_forwards_chat_provider_per_vm():
    body = (MODULE / "main.tf").read_text()
    assert re.search(r"chat_provider\s*=\s*each\.value\.chat_provider", body)
    assert not re.search(r"chat_provider\s*=\s*var\.chat_provider", body)


def test_tpl_emits_the_env_line_only_when_set():
    body = (MODULE / "startup-script.sh.tpl").read_text()
    # Guarded, so the empty default writes NO line and the instance keeps
    # following instance.yaml / the app default.
    assert '%{ if chat_provider != "" ~}' in body
    assert "AGNES_CHAT_PROVIDER=${chat_provider}" in body
    guard = body.index('%{ if chat_provider != "" ~}')
    line = body.index("AGNES_CHAT_PROVIDER=${chat_provider}")
    endif = body.index("%{ endif ~}", line)
    assert guard < line < endif


def test_tf_allowlist_matches_the_apps_boot_allowlist():
    """The Terraform validation and app/main.py's provider allowlist must
    accept the same set — a value the plan admits but boot refuses turns a
    typo into a VM whose every chat route 503s."""
    vbody = (MODULE / "variables.tf").read_text()
    tf_values = set(re.findall(r'contains\(\["", "docker", "kai-agent"\][^)]*chat_provider\)', vbody))
    assert len(tf_values) >= 1, "chat_provider allowlist validation missing"
    prod_block, dev_block = _object_type_blocks(vbody)
    assert "chat_provider" in prod_block and "chat_provider" in dev_block
    app_main = Path("app/main.py").read_text()
    assert 'not in ("docker", "kai-agent")' in app_main, (
        "app/main.py's provider allowlist changed — update the Terraform validation to match"
    )


def test_kai_agent_pin_requires_the_engine_on_the_same_vm():
    """chat_provider=kai-agent on a VM without kai_agent_enabled would refuse
    every session at mint time — must fail the plan, on both object types."""
    body = (MODULE / "variables.tf").read_text()
    pairings = re.findall(
        r'chat_provider\s*!=\s*"kai-agent"\s*\|\|\s*(?:var\.prod_instance\.|i\.)kai_agent_enabled', body
    )
    assert len(pairings) == 2, f"expected the kai-agent↔engine pairing validated on prod+dev, got {len(pairings)}"


def test_docker_provider_provisions_its_own_sandbox_runner():
    """``chat_provider = "docker"`` must provision the whole backing, not just
    the pin: web chat then runs its sessions as local containers created by the
    apps-runner sidecar, and the app REFUSES the ChatManager at boot when that
    sidecar or the sandbox image is missing. Before this, the sidecar plumbing
    hung off ``data_apps_enabled`` alone — so a TF-pinned docker provider came
    up with every chat route 503ing, which is exactly the state a pin is
    supposed to prevent."""
    body = (MODULE / "startup-script.sh.tpl").read_text()
    # APPS_RUNNER_TOKEN + DOCKER_GID prep and the `apps` compose profile are
    # shared with data apps — either feature alone must render them.
    assert body.count('%{ if data_apps_enabled || chat_provider == "docker" ~}') == 2
    # ... but NOT the app-level data-apps switch: enabling one feature must not
    # surface the other. A docker-chat VM writes the two sidecar keys only.
    docker_env = body.index('%{ if chat_provider == "docker" && !data_apps_enabled ~}')
    endif = body.index("%{ endif ~}", docker_env)
    block = body[docker_env:endif]
    assert "APPS_RUNNER_TOKEN=$APPS_RUNNER_TOKEN" in block
    assert "DOCKER_GID=$DOCKER_GID" in block
    # The env LINE, not the word — the block's comment names the key it
    # deliberately does not write.
    assert "\nAGNES_DATA_APPS_ENABLED=" not in block


def test_docker_provider_builds_the_sandbox_image_before_starting_the_stack():
    """The sandbox image is operator-built (the release pipeline does not
    publish it), so a VM recreate would otherwise come up without it and chat
    would refuse until a human ran a build by hand. It must also be built
    BEFORE `up -d`: the app probes it during its own lifespan."""
    body = (MODULE / "startup-script.sh.tpl").read_text()
    call = "scripts/ops/agnes-chat-sandbox-image.sh"
    assert call in body
    guard = body.rindex('%{ if chat_provider == "docker" ~}')
    assert guard < body.index(call)
    # Best-effort: a failed build must not abort the boot before cron and the
    # watchdog are installed.
    assert '|| echo "WARN: chat sandbox image unavailable' in body
    assert body.index(call) < body.index("docker compose $COMPOSE_PROFILES_ARG up -d")


def test_upgrade_tick_keeps_the_sidecar_and_refreshes_the_sandbox_image():
    """A recurring tick that dropped `--profile apps` would stop the sandbox
    runner on a VM whose chat was working a minute earlier; one that never
    rebuilt the image would run a sandbox from an older release than the app."""
    body = Path("scripts/ops/agnes-auto-upgrade.sh").read_text()
    assert 'CHAT_PROVIDER="$(_env_get AGNES_CHAT_PROVIDER)"' in body
    assert '[ "$CHAT_PROVIDER" = "docker" ] && APPS_PROFILE_WANTED=1' in body
    # Appended once, not twice, on a VM that also carries `apps` in
    # COMPOSE_PROFILES (which this script folds into flags earlier).
    assert body.count("PROFILE_ARGS+=( --profile apps )") == 1
    assert '*" apps "*' in body
    # The rebuild rides the recreate branch, before the compose `up`.
    assert '/opt/agnes/scripts/ops/agnes-chat-sandbox-image.sh "$IMAGE"' in body


def test_upgrade_tick_rebuilds_a_missing_sandbox_image_without_drift():
    """A failed boot build must self-heal within one tick, not wait for drift.

    The boot build is best-effort by design — it must not abort a VM boot — so
    a transient failure leaves the image missing, and the app refuses the
    ChatManager without it (every chat route 503s). The drift-gated refresh
    never fires on a no-change tick, so a stable VM would stay chat-less until
    something unrelated to chat happened to change. Mirrors the kai-agent
    engine's every-tick down-retry, and is gated the same way — on the image
    being ABSENT, so a healthy box does not extract the build context out of
    the app image every five minutes.
    """
    body = Path("scripts/ops/agnes-auto-upgrade.sh").read_text()
    call = '/opt/agnes/scripts/ops/agnes-chat-sandbox-image.sh "$IMAGE"'
    # Two call sites now: the drift-branch refresh (a context that MOVED, which
    # an image-presence check cannot see) and this presence-gated self-heal.
    assert body.count(call) == 2
    guard = "if ! docker image inspect agnes-chat-sandbox:latest >/dev/null 2>&1; then"
    assert guard in body
    # The self-heal runs on EVERY tick, i.e. before the drift branch — which is
    # the whole point; ordering it after would inherit the gate it exists to
    # bypass.
    drift_branch = 'if [ "$IMAGE_DRIFT" = "1" ] || [ "$CONFIG_DRIFT" = "1" ]; then'
    assert body.index(guard) < body.index(drift_branch)
    # Both call sites stay best-effort: a failed rebuild must not abort the
    # tick and leave the config marker unwritten.
    assert body.count(f"{call} \\\n            || logger -t agnes-auto-upgrade") == 2


def test_sandbox_image_helper_is_idempotent_and_shipped_to_the_host():
    helper = Path("scripts/ops/agnes-chat-sandbox-image.sh")
    body = helper.read_text()
    # Rebuild decision keys on the build context's own hash, so an app upgrade
    # that changed the sandbox Dockerfile rebuilds exactly once and an upgrade
    # that did not is free.
    assert "agnes.chat-sandbox.source" in body
    assert 'if [ "$WANT" = "$HAVE" ]' in body
    # …over the whole build context (see the behavioural guard below), never a
    # single file inside it.
    assert 'WANT=$(context_fingerprint "$TMP_CTX")' in body
    # Context comes from the app image, not a moving branch — sandbox and
    # server stay on one release.
    assert "/app/app/initial_workspace_default/docker-sandbox" in body
    # Delivered to the VM through the /opt/agnes-host contract, like every
    # other host-side artifact (the raw-branch curl path cannot serve a
    # private repo).
    dockerfile = Path("Dockerfile").read_text()
    assert "/app/scripts/ops/agnes-chat-sandbox-image.sh" in dockerfile
    assert (
        "chmod 0755 /opt/agnes-host/agnes-auto-upgrade.sh \\\n              /opt/agnes-host/scripts/ops/agnes-chat-sandbox-image.sh"
        in dockerfile
    )


def _extract_shell_function(body: str, name: str) -> str:
    """Return the source of one brace-delimited shell function."""
    m = re.search(rf"^{re.escape(name)}\(\)\s*\{{$", body, re.MULTILINE)
    assert m, f"{name}() must be defined as its own function"
    depth, i = 1, m.end()
    while i < len(body) and depth:
        if body[i] == "{":
            depth += 1
        elif body[i] == "}":
            depth -= 1
        i += 1
    return body[m.start() : i]


def _fingerprint(ctx: Path) -> str:
    """Run the helper's own ``context_fingerprint`` over a directory."""
    fn = _extract_shell_function(
        Path("scripts/ops/agnes-chat-sandbox-image.sh").read_text(),
        "context_fingerprint",
    )
    out = subprocess.run(
        ["bash", "-c", f'set -uo pipefail\n{fn}\ncontext_fingerprint "$1"', "_", str(ctx)],
        capture_output=True,
        text=True,
        check=True,
    )
    digest = out.stdout.strip()
    assert re.fullmatch(r"[0-9a-f]{64}", digest), f"not a sha256: {digest!r} {out.stderr}"
    return digest


@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="needs coreutils sha256sum")
def test_sandbox_rebuild_keys_on_the_whole_context_not_just_the_dockerfile(tmp_path):
    """`docker build` reads the entire context, so the idempotency label has to
    fingerprint the entire context.

    Hashing only ``Dockerfile`` (the original shape) meant a build-affecting
    file COPY'd into the sandbox could be edited without moving the label —
    the helper would report "is current" and the VM would keep running a stale
    sandbox image across upgrades, silently diverging from the app release it
    is supposed to be pinned to.
    """
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM scratch\nCOPY entrypoint.sh /\n")
    (ctx / "entrypoint.sh").write_text("#!/bin/sh\necho v1\n")
    base = _fingerprint(ctx)

    # Deterministic: same context, same digest.
    assert _fingerprint(ctx) == base

    # The regression: a non-Dockerfile, build-affecting edit must move it.
    (ctx / "entrypoint.sh").write_text("#!/bin/sh\necho v2\n")
    assert _fingerprint(ctx) != base

    # So must a newly added file, and a Dockerfile edit still does too.
    (ctx / "requirements.txt").write_text("ruff==0.1.0\n")
    with_added = _fingerprint(ctx)
    assert with_added != base
    (ctx / "Dockerfile").write_text("FROM scratch\nCOPY . /\n")
    assert _fingerprint(ctx) != with_added
