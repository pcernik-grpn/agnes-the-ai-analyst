"""Functional merge test for the customer-instance extraction overlay.

``test_startup_extraction_lane_toggle.py`` pins the Terraform → startup-script
plumbing textually; this file executes the part whose semantics no grep can
prove: what docker compose actually produces when the module-written
``docker-compose.extraction.yml`` is layered onto the real base + prod +
host-mount chain. The overlay bytes come from the template itself (the
EXTRYAML heredoc, unescaped exactly as ``templatefile`` + the shell heredoc
produce them on a VM), so a template edit that breaks the merge fails here,
not on a customer VM.

The merge facts worth a real ``docker compose config`` run:

* ``profiles: !reset []`` clears the base service's profile gate — the
  worker is always-on with the overlay present, absent without it. This is
  the overlay's entire activation mechanism and it rides a compose merge
  feature (``!reset``) that plain YAML tooling does not implement.
* The overlay's ``image:`` re-pin beats docker-compose.prod.yml's
  plain-app-image pin (file order), while ``AGNES_ROLE``/``AGNES_WORKER_LANES``
  and the resource-limit interpolations inherit from the base service.
* ``depends_on`` merges additively (app: service_healthy stays, redis:
  service_healthy joins).
* The app service is untouched except for what ``env_file: .env`` carries —
  the coordination declaration reaches app and worker through .env, which is
  exactly why the state-applier's managed-overlay-only recreates cannot strip
  it.

Skips when docker compose is unavailable or predates the ``!reset`` merge tag
(compose 2.24+); any other failure is a real regression and fails loudly.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TPL = REPO / "infra/modules/customer-instance/startup-script.sh.tpl"

WORKER_IMAGE = "registry.example.com/agnes/extraction-worker:1.2.3-producer"

VM_ENV = """\
AGNES_TAG=stable
AGNES_IMAGE_REPO=ghcr.io/keboola/agnes-the-ai-analyst
AGNES_COORDINATION_BACKEND=redis
AGNES_REDIS_URL=redis://redis:6379/0
AGNES_EXTRACTION_WORKER_IMAGE={image}
AGNES_EXTRACTION_WORKER_MEM_LIMIT=4g
AGNES_EXTRACTION_WORKER_CPUS=2.0
JWT_SECRET_KEY=test-jwt
SESSION_SECRET=test-session
""".format(image=WORKER_IMAGE)


def overlay_as_written_on_vm() -> str:
    """The exact docker-compose.extraction.yml bytes a VM ends up with.

    The template writes the overlay through a quoted heredoc, so the only
    transformation between template text and on-disk file is Terraform's
    ``$${`` → ``${`` unescape (templatefile), which the shell's quoted
    heredoc then passes through verbatim.
    """
    m = re.search(r"<<'EXTRYAML'\n(.*?)\nEXTRYAML\n", TPL.read_text(), re.DOTALL)
    assert m, "startup-script.sh.tpl must write the extraction overlay via an EXTRYAML heredoc"
    return m.group(1).replace("$${", "${") + "\n"


def _compose_config(project: Path, files: list[str]) -> dict:
    cmd = ["docker", "compose"]
    for f in files:
        cmd += ["-f", f]
    cmd += ["config", "--format", "json"]
    proc = subprocess.run(cmd, cwd=project, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        stderr = proc.stderr.lower()
        if "!reset" in stderr or "unknown tag" in stderr or "yaml" in stderr and "reset" in stderr:
            pytest.skip(f"docker compose too old for the !reset merge tag: {proc.stderr[:200]}")
        raise AssertionError(f"docker compose config failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def compose_available():
    if shutil.which("docker") is None:
        pytest.skip("docker not on PATH")
    probe = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True, timeout=60)
    if probe.returncode != 0:
        pytest.skip("docker compose plugin unavailable")


@pytest.fixture()
def project(tmp_path: Path, compose_available) -> Path:
    """A VM-like compose project dir: the repo's real compose chain + the
    overlay exactly as the startup script writes it + a VM-like .env."""
    for name in ("docker-compose.yml", "docker-compose.prod.yml", "docker-compose.host-mount.yml"):
        shutil.copy(REPO / name, tmp_path / name)
    (tmp_path / "docker-compose.extraction.yml").write_text(overlay_as_written_on_vm())
    (tmp_path / ".env").write_text(VM_ENV)
    return tmp_path


BASE_CHAIN = ["docker-compose.yml", "docker-compose.prod.yml", "docker-compose.host-mount.yml"]


def test_without_overlay_worker_stays_profile_gated(project: Path):
    cfg = _compose_config(project, BASE_CHAIN)
    assert "extraction-worker" not in cfg["services"], (
        "without the overlay the base profile gate must keep the worker out of the default service set"
    )
    assert "redis" not in cfg["services"]


def test_overlay_activates_and_repins_the_worker(project: Path):
    cfg = _compose_config(project, BASE_CHAIN + ["docker-compose.extraction.yml"])
    services = cfg["services"]

    # Activation: !reset cleared the profile gate; both services are in the
    # default (no --profile) service set.
    assert "extraction-worker" in services
    assert "redis" in services
    worker = services["extraction-worker"]
    assert not worker.get("profiles"), "profiles must be cleared, not merged"

    # Image re-pin beats docker-compose.prod.yml's plain-app-image pin …
    assert worker["image"] == WORKER_IMAGE
    # … while the prod pin still governs the app service.
    assert services["app"]["image"] == "ghcr.io/keboola/agnes-the-ai-analyst:stable"

    # Base-service inheritance the overlay must not disturb.
    env = worker["environment"]
    assert env["AGNES_ROLE"] == "worker"
    assert env["AGNES_WORKER_LANES"] == "extraction"

    # .env interpolation of the module-written resource ceilings. Compose
    # serializes these as strings or numbers depending on version — compare
    # numerically.
    assert int(worker["mem_limit"]) == 4 * 1024**3
    assert float(worker["cpus"]) == 2.0

    # Additive depends_on merge: the base app-healthy gate survives, the
    # overlay's redis-healthy gate joins it.
    deps = {k: v["condition"] for k, v in worker["depends_on"].items()}
    assert deps == {"app": "service_healthy", "redis": "service_healthy"}


def test_coordination_env_reaches_app_and_worker_via_env_file(project: Path):
    """The declaration must ride env_file: .env — that is what keeps it on
    the app service across the state-applier's managed-overlay-only
    force-recreate (the applier's chain does not include this overlay)."""
    cfg = _compose_config(project, BASE_CHAIN + ["docker-compose.extraction.yml"])
    for svc in ("app", "scheduler", "extraction-worker"):
        env = cfg["services"][svc]["environment"]
        assert env.get("AGNES_COORDINATION_BACKEND") == "redis", svc
        assert env.get("AGNES_REDIS_URL") == "redis://redis:6379/0", svc

    # And the applier's own view — the managed chain WITHOUT the overlay —
    # must still deliver the coordination env to the app it force-recreates.
    applier_cfg = _compose_config(project, BASE_CHAIN)
    env = applier_cfg["services"]["app"]["environment"]
    assert env.get("AGNES_COORDINATION_BACKEND") == "redis"
    assert env.get("AGNES_REDIS_URL") == "redis://redis:6379/0"


def test_redis_is_internal_and_ephemeral(project: Path):
    cfg = _compose_config(project, BASE_CHAIN + ["docker-compose.extraction.yml"])
    redis = cfg["services"]["redis"]
    assert not redis.get("ports"), "redis must not expose host ports"
    assert not redis.get("volumes"), "redis must not persist anything to disk"
    assert redis["command"] == ["redis-server", "--save", "", "--appendonly", "no"]


def test_strict_boot_strip_lines_execute_correctly():
    """Execute the template's actual suffix-strip lines (unescaped exactly as
    templatefile renders them) for both kai on/off combinations — the strips
    are order-sensitive and a regression here strands the strict boot phase
    pulling a private image it must not gate on."""
    body = TPL.read_text()
    kai_strip = 'export COMPOSE_FILE="$${COMPOSE_FILE%:docker-compose.kai-agent.yml}"'
    extraction_strip = 'export COMPOSE_FILE="$${COMPOSE_FILE%:docker-compose.extraction.yml}"'
    assert kai_strip in body and extraction_strip in body
    kai_line = kai_strip.replace("$${", "${")
    extraction_line = extraction_strip.replace("$${", "${")

    base = "docker-compose.yml:docker-compose.prod.yml:docker-compose.host-mount.yml"

    def run(compose_file: str, lines: list[str]) -> str:
        script = f'COMPOSE_FILE="{compose_file}"\n' + "\n".join(lines) + '\nprintf %s "$COMPOSE_FILE"\n'
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    # kai + extraction both enabled: kai strips first (suffix), extraction second.
    both = f"{base}:docker-compose.extraction.yml:docker-compose.kai-agent.yml"
    assert run(both, [kai_line, extraction_line]) == base
    # extraction alone.
    alone = f"{base}:docker-compose.extraction.yml"
    assert run(alone, [extraction_line]) == base


def _tolerant_block() -> str:
    """The template's tolerant bring-up block, rendered as the shell sees it.

    The block sits between its own ``%{ if extraction_worker_enabled ~}``
    guard and the following ``%{ endif ~}`` and contains no ``${...}``
    Terraform interpolation — only ``$${...}`` shell escapes — so unescaping
    yields exactly the bash a flag-on render ships.
    """
    body = TPL.read_text()
    start = body.index("# Now the extraction lane, tolerantly")
    end = body.index("%{ endif ~}", start)
    block = body[start:end].replace("$${", "${")
    assert "${" not in block.replace("${COMPOSE_PROFILES_ARG", "").replace(
        "${EXTRACTION_FULL_COMPOSE_FILE", ""
    ).replace("${COMPOSE_FILE", ""), "tolerant block must carry no Terraform interpolation beyond shell vars"
    return block


def _run_tolerant_phase(tmp_path: Path, failing_pulls: str) -> tuple[str, str]:
    """Execute the tolerant block with a transcript-recording fake docker.

    ``failing_pulls`` is a space-separated list of services whose
    ``docker compose pull <svc>`` should fail. Returns (transcript, stderr).
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    transcript = tmp_path / "transcript.log"
    fake = bin_dir / "docker"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'echo "docker $*" >> "$TRANSCRIPT"\n'
        'if [ "$1 $2" = "compose pull" ]; then\n'
        '  for f in $FAILING_PULLS; do [ "$3" = "$f" ] && exit 1; done\n'
        "fi\n"
        "exit 0\n"
    )
    fake.chmod(0o755)
    script = (
        "set -euo pipefail\n"
        'COMPOSE_PROFILES_ARG=""\n'
        'EXTRACTION_FULL_COMPOSE_FILE="docker-compose.yml:docker-compose.extraction.yml"\n' + _tolerant_block()
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "TRANSCRIPT": str(transcript),
            "FAILING_PULLS": failing_pulls,
        },
    )
    assert proc.returncode == 0, f"tolerant block must never fail the boot (set -e is active): {proc.stderr}"
    return transcript.read_text() if transcript.exists() else "", proc.stderr


def test_tolerant_phase_brings_redis_up_before_the_worker(tmp_path: Path):
    transcript, stderr = _run_tolerant_phase(tmp_path, failing_pulls="")
    lines = [line for line in transcript.splitlines() if line]
    assert lines == [
        "docker compose pull redis",
        "docker compose up -d redis",
        "docker compose pull extraction-worker",
        "docker compose up -d extraction-worker",
    ], "redis must be pulled+started before the worker's private-registry pull"
    assert stderr == ""


def test_tolerant_phase_survives_a_broken_worker_image(tmp_path: Path):
    """A private-registry failure on the worker image must neither abort the
    boot (cron/watchdog install after this block) nor take redis down — the
    app is already running with redis coordination declared in .env."""
    transcript, stderr = _run_tolerant_phase(tmp_path, failing_pulls="extraction-worker")
    lines = [line for line in transcript.splitlines() if line]
    assert "docker compose up -d redis" in lines, "redis must come up despite the worker failure"
    assert "docker compose up -d extraction-worker" not in lines, (
        "a failed worker pull must not be followed by an up for it"
    )
    assert "extraction-worker failed to pull or start" in stderr


def test_tolerant_phase_warns_but_continues_when_redis_fails(tmp_path: Path):
    transcript, stderr = _run_tolerant_phase(tmp_path, failing_pulls="redis extraction-worker")
    assert "redis coordination backend failed" in stderr
    assert "extraction-worker failed to pull or start" in stderr
