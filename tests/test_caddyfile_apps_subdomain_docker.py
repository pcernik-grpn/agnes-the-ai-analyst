"""Does the Caddyfile the startup script BUILDS actually parse?

A syntax error here does not merely break data apps — Caddy refuses the whole
config, so the PRIMARY site goes down with it. That is the same failure mode an
empty ``DOMAIN_ALIAS`` once caused, and no amount of string-matching in
``test_startup_data_apps_toggle.py`` can catch it: only Caddy's own parser can.

So this test does not re-implement the wiring — it **extracts the real block**
from ``startup-script.sh.tpl`` (between its ``apps-subdomain-caddy`` markers)
and runs it, exactly as ``test_startup_container_metadata_hardening.py`` does
for the metadata rule. A copy would drift; an extract cannot.

Gated behind Docker + an explicit opt-in — never runs in CI or the default
local suite (``pytest.ini``'s default addopts already exclude the ``docker``
marker):

    AGNES_CADDY_VALIDATE=1 .venv/bin/pytest tests/test_caddyfile_apps_subdomain_docker.py -m docker -q

Requires Docker and the ``caddy:2-alpine`` image named in ``docker-compose.yml``
(pulled on first run).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        not os.environ.get("AGNES_CADDY_VALIDATE"),
        reason="set AGNES_CADDY_VALIDATE=1 to run (needs Docker + the caddy image)",
    ),
]

REPO = Path(__file__).resolve().parents[1]
TPL = REPO / "infra/modules/customer-instance/startup-script.sh.tpl"
CADDY_IMAGE = "caddy:2-alpine"
BASE = "apps.example.com"


def _extract_block() -> str:
    body = TPL.read_text()
    m = re.search(
        r"^# --- apps-subdomain-caddy begin.*?$\n(.*?)^# --- apps-subdomain-caddy end ---$",
        body,
        re.S | re.M,
    )
    assert m, "apps-subdomain-caddy markers missing from the startup script"
    block = m.group(1)
    # The block must be plain shell — a terraform directive would mean the test
    # is executing something the real boot does not.
    assert "${" not in block and "%{" not in block, "block carries template directives"
    return block


def _build_caddyfile(tmp_path: Path) -> Path:
    shutil.copy(REPO / "Caddyfile", tmp_path / "Caddyfile")
    shutil.copy(REPO / "deploy/caddy/Caddyfile.apps-subdomain", tmp_path / "Caddyfile.apps-subdomain")
    script = f'set -eu\nAPP_DIR="{tmp_path}"\nAPPS_SUBDOMAIN_BASE="{BASE}"\n' + _extract_block()
    subprocess.run(["sh", "-c", script], check=True, capture_output=True, text=True)
    return tmp_path / "Caddyfile"


def _validate(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "docker", "run", "--rm",
            "-v", f"{path}:/etc/caddy/Caddyfile:ro",
            "-e", f"APPS_SUBDOMAIN_BASE={BASE}",
            "-e", "DOMAIN=example.com",
            "-e", "CADDY_TLS=tls ops@example.com",
            "-e", "DOMAIN_ALIAS=127.0.0.1:8081",
            CADDY_IMAGE,
            "caddy", "validate", "--config", "/etc/caddy/Caddyfile",
        ],
        capture_output=True,
        text=True,
    )


def test_generated_caddyfile_parses(tmp_path):
    merged = _build_caddyfile(tmp_path)
    text = merged.read_text()
    # Global options block FIRST — Caddy rejects it anywhere else.
    assert text.lstrip().startswith("{"), text[:120]
    assert "on_demand_tls" in text and f"*.{{$APPS_SUBDOMAIN_BASE}}" in text

    r = _validate(merged)
    assert r.returncode == 0, f"caddy validate failed:\n{r.stdout}\n{r.stderr}"


def test_running_the_block_twice_is_idempotent(tmp_path):
    """The startup script runs on EVERY boot. A second global options block is
    a file Caddy will not parse — so the guard has to hold, and the result has
    to still validate."""
    merged = _build_caddyfile(tmp_path)
    first = merged.read_text()

    script = f'set -eu\nAPP_DIR="{tmp_path}"\nAPPS_SUBDOMAIN_BASE="{BASE}"\n' + _extract_block()
    subprocess.run(["sh", "-c", script], check=True, capture_output=True, text=True)

    assert merged.read_text() == first, "second run mutated the Caddyfile"
    assert first.count("on_demand_tls") == 1
    r = _validate(merged)
    assert r.returncode == 0, f"caddy validate failed after a second boot:\n{r.stdout}\n{r.stderr}"


def test_no_base_configured_leaves_the_caddyfile_alone(tmp_path):
    """Path-prefix deployments must render a byte-identical Caddyfile — an
    empty base would produce the site address `*.` and take the primary site
    down at config parse."""
    shutil.copy(REPO / "Caddyfile", tmp_path / "Caddyfile")
    shutil.copy(REPO / "deploy/caddy/Caddyfile.apps-subdomain", tmp_path / "Caddyfile.apps-subdomain")
    before = (tmp_path / "Caddyfile").read_text()

    script = f'set -eu\nAPP_DIR="{tmp_path}"\nAPPS_SUBDOMAIN_BASE=""\n' + _extract_block()
    subprocess.run(["sh", "-c", script], check=True, capture_output=True, text=True)

    assert (tmp_path / "Caddyfile").read_text() == before
