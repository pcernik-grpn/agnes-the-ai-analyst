"""Static contract for running the app image from an alternate registry.

The default image repository is a public registry; an instance whose image
must come from a private registry instead (e.g. a GCP Artifact Registry
mirror — `release.yml` can already push one) needs every consumer of the
image reference to resolve the SAME repository, or the stack splits between
two registries mid-upgrade. The seam is one env var, `AGNES_IMAGE_REPO`:

* the compose overlays interpolate it (with the public default preserved),
* the recurring host scripts (`agnes-auto-upgrade.sh`,
  `agnes-state-applier.sh`) read it from `/opt/agnes/.env` via `_env_get`
  and build their own image refs from it with the same default,
* `startup-script.sh.tpl` writes it into `.env` from the module's
  `image_repo` variable, and — for `*-docker.pkg.dev` hosts — runs
  `gcloud auth configure-docker` BEFORE the first pull so the VM's service
  account authenticates (the credential helper persists in root's docker
  config, which the recurring ticks inherit).

Pinned here so no consumer silently reverts to a hardcoded repository.
"""

import re
from pathlib import Path

DEFAULT_REPO = "ghcr.io/keboola/agnes-the-ai-analyst"
INTERPOLATED = "${AGNES_IMAGE_REPO:-" + DEFAULT_REPO + "}:${AGNES_TAG:-stable}"
TPL = Path("infra/modules/customer-instance/startup-script.sh.tpl")
AUTO_UPGRADE = Path("scripts/ops/agnes-auto-upgrade.sh")
STATE_APPLIER = Path("scripts/ops/agnes-state-applier.sh")


def test_compose_overlays_interpolate_the_image_repo():
    """Every app-image reference in the compose overlays must go through
    AGNES_IMAGE_REPO (default preserved) — a single hardcoded ref would pin
    that service to the public registry regardless of the instance's
    configured repository."""
    hardcoded = []
    interpolated = 0
    for path in sorted(Path(".").glob("docker-compose*.yml")):
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if "image:" not in line:
                continue
            if INTERPOLATED in line:
                interpolated += 1
            elif DEFAULT_REPO in line:
                hardcoded.append(f"{path}:{i}: {line.strip()}")
    assert not hardcoded, "app-image refs must interpolate ${AGNES_IMAGE_REPO:-...}: " + "; ".join(hardcoded)
    # Sanity that the scan actually saw the app services (7 in the prod
    # overlay + the postgres overlay's migrator/side-car pair).
    assert interpolated >= 9, (
        f"expected at least 9 interpolated app-image refs, found {interpolated} — did the compose overlays move?"
    )


def test_host_scripts_read_image_repo_from_env_with_default():
    """Both recurring host scripts must build their image ref from
    AGNES_IMAGE_REPO with the same public default the compose files use."""
    image_line = 'IMAGE="${AGNES_IMAGE_REPO:-' + DEFAULT_REPO + '}:${AGNES_TAG:-stable}"'
    for script in (AUTO_UPGRADE, STATE_APPLIER):
        body = script.read_text()
        assert "_env_get AGNES_IMAGE_REPO" in body, (
            f"{script} must read AGNES_IMAGE_REPO via _env_get (never by bash-sourcing .env)"
        )
        assert image_line in body, (
            f"{script} must build IMAGE from AGNES_IMAGE_REPO with the public default: {image_line}"
        )
        # Exported so `docker compose` interpolates the same repository the
        # script pulls/inspects — a non-exported read would split the two.
        assert re.search(r"^export .*AGNES_IMAGE_REPO", body, re.MULTILINE), (
            f"{script} must export AGNES_IMAGE_REPO for docker compose interpolation"
        )


def test_tpl_writes_image_repo_into_env():
    body = TPL.read_text()
    env_start = body.index('cat > "$APP_DIR/.env" <<ENVEOF')
    env_end = body.index("ENVEOF", env_start + 40)
    assert "AGNES_IMAGE_REPO=$IMAGE_REPO" in body[env_start:env_end], (
        "startup-script.sh.tpl must write AGNES_IMAGE_REPO into .env — the "
        "compose files and recurring host scripts resolve the repository "
        "from there"
    )


def test_tpl_configures_registry_auth_before_the_first_pull():
    """For a *-docker.pkg.dev image host, `gcloud auth configure-docker`
    must run BEFORE the section-3 `docker pull` that extracts host
    artifacts — after it, the boot has already failed on an unauthenticated
    pull."""
    body = TPL.read_text()
    m = re.search(
        r'IMAGE_HOST="\$\$\{IMAGE_REPO%%/\*\}"\n'
        r'case "\$IMAGE_HOST" in\n'
        r'\s*\*-docker\.pkg\.dev\) gcloud auth configure-docker "\$IMAGE_HOST" --quiet',
        body,
    )
    assert m, (
        "startup-script.sh.tpl must run gcloud auth configure-docker for a "
        "*-docker.pkg.dev IMAGE_REPO host (matching the host precisely, not "
        "any registry) so the VM SA authenticates the pull"
    )
    first_pull = body.index('docker pull "$${IMAGE_REPO}:$${IMAGE_TAG}"')
    assert m.start() < first_pull, "the configure-docker helper must run BEFORE the first docker pull"
    # Best-effort: a failed configure-docker warns instead of aborting the
    # boot under `set -e` (same posture as the kai-agent registry helper).
    assert "WARN: gcloud auth configure-docker $IMAGE_HOST failed" in body
