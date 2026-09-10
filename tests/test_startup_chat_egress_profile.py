"""The `chat-docker-egress` compose profile is DERIVED from the configured
egress mode, not from an operator remembering a flag (#1250).

`chat.docker_egress_mode: allowlist` puts every chat sandbox on an
`internal: true` network with no route out and makes the `services/egress_proxy`
sidecar the only way off it. The sidecar sits behind the `chat-docker-egress`
compose profile, so before this coupling a deployment could configure allowlist
mode and simply never run the container that enforces it — and, worse, with the
profile inactive `docker compose up -d` neither recreates, restarts nor
health-checks a proxy container that already exited, which is how one sat
`Exited` for five days with nothing surfacing it.

The coupling has two halves, and this file pins the provisioning one:

* `scripts/ops/agnes-compose-file.sh` — the single shared place where every
  host-side `docker compose` decision is derived from
  `/data/state/instance.yaml` (the same file the app reads its chat config
  from, so the two can never disagree) — grows
  `agnes_chat_egress_allowlist_active`;
* `startup-script.sh.tpl` (VM boot) and `agnes-auto-upgrade.sh` (every 5 min)
  both activate the profile from that one gate, and the tick additionally
  brings a down proxy back up.

The app-side half — a boot refusal naming the profile when the proxy is
unreachable — lives in `tests/test_chat_deployment_gates.py`.

Both directions of a wrong answer here fail safe: a false positive starts a
proxy nothing uses, and a false negative leaves the app's boot gate to refuse
chat loudly.
"""

import subprocess
from pathlib import Path

import pytest

MODULE = Path("infra/modules/customer-instance")
HELPER = Path("scripts/ops/agnes-compose-file.sh")
UPGRADE = Path("scripts/ops/agnes-auto-upgrade.sh")
PROFILE = "chat-docker-egress"
GATE = "agnes_chat_egress_allowlist_active"


def _gate(tmp_path: Path, instance_yaml: str | None) -> int:
    """Run the REAL shell gate against a state dir, return its exit status."""
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    if instance_yaml is not None:
        (state / "instance.yaml").write_text(instance_yaml)
    return subprocess.run(
        ["sh", "-c", f'. "{HELPER.resolve()}"; {GATE} "$1"', "_", str(state)],
        capture_output=True,
        text=True,
        check=False,
    ).returncode


# ---------------------------------------------------------------------------
# The gate itself, exercised as shipped (POSIX sh, sourced by three callers)
# ---------------------------------------------------------------------------


def test_gate_fires_for_allowlist_mode(tmp_path):
    assert _gate(tmp_path / "a", "chat:\n  docker_egress_mode: allowlist\n") == 0
    # Quoted, and with the trailing comment / whitespace shapes PyYAML writers
    # and hand edits both produce.
    assert _gate(tmp_path / "b", 'chat:\n  docker_egress_mode: "allowlist"\n') == 0
    assert _gate(tmp_path / "c", "chat:\n    docker_egress_mode:   allowlist   \n") == 0


def test_gate_stays_silent_for_open_and_none(tmp_path):
    """`open` (unrestricted bridge) and `none` (the secure default) have no
    proxy in the picture — a default instance must render no profile at all."""
    assert _gate(tmp_path / "open", "chat:\n  docker_egress_mode: open\n") == 1
    assert _gate(tmp_path / "none", "chat:\n  docker_egress_mode: none\n") == 1


def test_gate_stays_silent_when_nothing_is_configured(tmp_path):
    """The overwhelmingly common shape: no chat section, no key, and — on a
    first boot before the app has ever written it — no file."""
    assert _gate(tmp_path / "nokey", "chat:\n  enabled: true\n") == 1
    assert _gate(tmp_path / "empty", "") == 1
    assert _gate(tmp_path / "absent", None) == 1


def test_gate_ignores_a_commented_out_key(tmp_path):
    """`config/instance.yaml.example` ships the key commented out, and an
    operator's overlay often carries the same shape — a comment configures
    nothing and must not provision a sidecar."""
    assert _gate(tmp_path / "cmt", "chat:\n  # docker_egress_mode: allowlist\n") == 1


@pytest.mark.parametrize(
    "scalar",
    [
        "allowlist # restrict sandbox traffic",
        '"allowlist" # restrict sandbox traffic',
        "allowlist\t# tab before the hash",
    ],
)
def test_gate_survives_a_yaml_inline_comment(tmp_path, scalar):
    """An inline comment is legal YAML and the app's own loader reads the
    value as `allowlist`, so the host must too. Leaving the comment attached
    made the shell compare `allowlist # …` against `allowlist`, omit
    `--profile chat-docker-egress`, and leave a configured allowlist
    deployment with no proxy — the host/app drift this resolver exists to
    prevent (Devin Review on #2417)."""
    assert _gate(tmp_path / scalar[:12].strip(), f"chat:\n  docker_egress_mode: {scalar}\n") == 0


def test_an_inline_comment_does_not_turn_another_mode_into_allowlist(tmp_path):
    """The comment strip must not be able to manufacture a match: `open` with
    a comment that merely mentions the word stays inactive."""
    assert (
        _gate(tmp_path / "open-cmt", "chat:\n  docker_egress_mode: open # not allowlist yet\n")
        == 1
    )


# ---------------------------------------------------------------------------
# Wiring: one gate, three call sites, no second copy of the logic
# ---------------------------------------------------------------------------


def test_the_gate_lives_in_the_shared_resolver_and_reads_instance_yaml():
    body = HELPER.read_text()
    assert f"{GATE}()" in body, f"{GATE} must live in the single shared resolver"
    # Derived from the configured mode in the state overlay — the same file
    # app/main.py loads its ChatConfig from — never from .env, which the boot
    # script rewrites and which no admin edit ever touches.
    assert "docker_egress_mode" in body
    assert "instance.yaml" in body


def test_boot_activates_the_profile_from_the_configured_mode():
    body = (MODULE / "startup-script.sh.tpl").read_text()
    assert f"--profile {PROFILE}" in body, "a VM configured for allowlist must start the enforcing sidecar"
    gate_at = body.index(f"if {GATE} ")
    profile_at = body.index(f"COMPOSE_PROFILES_ARG --profile {PROFILE}")
    assert gate_at < profile_at, "the profile must be gated on the configured mode, not appended unconditionally"
    # Never through COMPOSE_PROFILES in .env: compose ignores that env var the
    # moment any --profile flag (e.g. `--profile tls`) is present, so the
    # sidecar would be silently dropped on every TLS instance.
    assert f"COMPOSE_PROFILES={PROFILE}" not in body


def test_boot_degrades_safely_when_the_resolver_is_missing():
    """An operator can pin AGNES_TAG to an image predating the gate. The
    startup script already stubs `agnes_gcp_logging_active` to false in that
    branch; this gate needs the same treatment or section 5 dies with
    command-not-found — and the fail-safe answer is "no profile", which the
    app's own boot gate then reports as a refusal."""
    body = (MODULE / "startup-script.sh.tpl").read_text()
    assert f"{GATE}() {{ false; }}" in body


def test_the_upgrade_tick_keeps_the_proxy_running():
    """The tick is where #1250 actually bit: it runs `docker compose up -d`
    every five minutes, and without the profile that command does not manage
    the proxy at all — an exited container stays exited and invisible."""
    body = UPGRADE.read_text()
    assert f"PROFILE_ARGS+=( --profile {PROFILE} )" in body
    # Appended once, and only for allowlist instances.
    assert body.count(f"--profile {PROFILE}") == 1
    assert GATE in body
    # Every-tick down-retry, mirroring the kai-agent sidecar: the drift-gated
    # recreate never fires on a no-change tick, so a proxy that exited on a
    # quiet box would stay down until something unrelated changed.
    assert "--status running egress-proxy" in body
    # …and the gate is read AFTER the resolver is sourced, or it is undefined.
    assert body.index('. "$RESOLVER"') < body.index(f"PROFILE_ARGS+=( --profile {PROFILE} )")
