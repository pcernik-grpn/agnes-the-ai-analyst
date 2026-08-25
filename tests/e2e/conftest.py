"""Shared fixtures for the cloud-chat E2E suite.

The fixtures here are intentionally heavyweight (real docker-compose +
real Chromium) and gated behind env vars so they never run in the
default `pytest` invocation. Without `AGNES_E2E=1` every test that
depends on `e2e_agnes` skips cleanly; without `AGNES_E2E_ANTHROPIC=1`
every test marked `real_llm` skips on top of that; without
`AGNES_E2E_DOCKER=1` tests that need a real docker sandbox spawn (the
apps-runner sidecar up + the agnes-chat-sandbox image built) skip too.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# real_llm marker (Task E.3)
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers used by the E2E suite.

    `real_llm` — see `pytest_collection_modifyitems` below. Registering
    the marker explicitly stops pytest from emitting an `UnknownMark`
    warning when --strict-markers is on (the project doesn't enable
    that today but might).
    """
    config.addinivalue_line(
        "markers",
        "real_llm: requires a real Anthropic API key — runs only when "
        "AGNES_E2E_ANTHROPIC=1 (set the key in ANTHROPIC_API_KEY).",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip `real_llm` tests when AGNES_E2E_ANTHROPIC isn't set.

    Lets the rest of the E2E suite run against the fake-agent runner
    (deterministic, no API spend) by default. Operators opt into the
    real-LLM path by exporting AGNES_E2E_ANTHROPIC=1 alongside an
    ANTHROPIC_API_KEY.

    Mark a test like:

        @pytest.mark.real_llm
        def test_catalog_discovery_via_natural_language(...):
            ...
    """
    if os.environ.get("AGNES_E2E_ANTHROPIC"):
        return
    skip_real_llm = pytest.mark.skip(
        reason="real_llm: set AGNES_E2E_ANTHROPIC=1 (and ANTHROPIC_API_KEY) to enable",
    )
    for item in items:
        if "real_llm" in item.keywords:
            item.add_marker(skip_real_llm)


# ---------------------------------------------------------------------------
# docker-compose fixture (Task E.1)
# ---------------------------------------------------------------------------

_COMPOSE_FILE = Path(__file__).parent / "docker-compose.e2e.yml"
# Host port is overridable (AGNES_E2E_PORT) so the suite can run when the
# default 8000 is already taken by another local container. The compose file
# maps ${AGNES_E2E_PORT:-8000}:8000; the in-container app + healthcheck always
# listen on 8000.
_E2E_PORT = os.environ.get("AGNES_E2E_PORT", "8000")
_BASE_URL = f"http://localhost:{_E2E_PORT}"
_HEALTH_PATH = "/api/health"
_HEALTH_TIMEOUT_SECONDS = 120


def _docker_available() -> bool:
    """Quick check that `docker compose` (v2) is on PATH."""
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "compose", "version"],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0


def _wait_for_health(base_url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base_url + _HEALTH_PATH, timeout=2) as resp:
                if 200 <= resp.status < 300:
                    return
        except (urllib.error.URLError, OSError) as exc:
            last_err = exc
        time.sleep(1.5)
    raise RuntimeError(f"agnes container did not become healthy within {timeout}s: {last_err!r}")


@pytest.fixture(scope="session")
def e2e_agnes() -> str:
    """Bring up docker-compose.e2e.yml, yield the base URL, tear down.

    Skips unless `AGNES_E2E=1`. Requires docker compose v2 + an
    ANTHROPIC_API_KEY on the host so the compose file's
    `${ANTHROPIC_API_KEY:?...}` resolves.

    The fixture is session-scoped so multiple E2E tests share one
    stack — image build is the expensive step (pip install of all the
    Agnes deps + ~250 MB sample data layer cache).
    """
    if not os.environ.get("AGNES_E2E"):
        pytest.skip("E2E env disabled — set AGNES_E2E=1 to run docker-compose suite")
    if not _docker_available():
        pytest.skip("docker compose (v2) not available on PATH")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip(
            "ANTHROPIC_API_KEY not set on host — compose file requires it "
            "(use AGNES_E2E_FAKE_AGENT=1 to flip the runner into echo mode)"
        )

    compose_args = ["docker", "compose", "-f", str(_COMPOSE_FILE)]

    # `up -d --build`: detached so we can poll healthz; --build forces a
    # rebuild when source changes between runs (the image layer caches
    # the dep install, so this is usually fast).
    subprocess.run([*compose_args, "up", "-d", "--build"], check=True)
    try:
        _wait_for_health(_BASE_URL, _HEALTH_TIMEOUT_SECONDS)
        yield _BASE_URL
    finally:
        # `down -v` clears the `agnes_data` named volume so the next
        # session starts from a clean DuckDB.
        subprocess.run(
            [*compose_args, "down", "-v"],
            check=False,
        )


# Back-compat alias — F.* tests reference docker_e2e_agnes; keep the
# old name pointed at the new fixture so we don't have to chase every
# call site in the same diff that renames it.
@pytest.fixture(scope="session")
def docker_e2e_agnes(e2e_agnes) -> str:
    return e2e_agnes
