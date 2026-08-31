"""Docker-daemon integration for the chat sandbox provider.

`@pytest.mark.docker` — excluded from the default run (`pytest.ini`), run with
`-m docker` on a machine with a daemon. Everything below is real except the
sandbox image: the sidecar runs in a background uvicorn on an ephemeral port
(the real handlers making real Docker SDK calls), and a small stock image
stands in for `agnes-chat-sandbox` so the test needs no image build.

The sidecar has to be a *real* HTTP server here, not an in-process ASGI
transport: `httpx.ASGITransport` buffers a response to completion before
returning it, and the attach stream never completes.

Covers the full round trip the unit tests can only mock: create → attach →
stdin frame → stdout frame → pause → unpause → reattach → destroy.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.docker

#: Any image with a POSIX shell will do — this exercises the sidecar's Docker
#: calls and the provider's stream plumbing, not the sandbox image (that is
#: `tests/e2e/test_docker_sandbox_smoke.py`). Override with
#: AGNES_DOCKER_TEST_IMAGE to reuse an image already on the machine.
IMAGE = os.environ.get("AGNES_DOCKER_TEST_IMAGE", "python:3.13-slim")

#: Line-echo loop: prints `ready`, then mirrors each stdin line back on stdout —
#: the same shape as the runner's JSONL protocol, minus the JSON.
ECHO_PROGRAM = 'echo ready; while IFS= read -r line; do echo "echo: $line"; done'

#: Long enough to clear the 32-character floor `_check_token` enforces on
#: both sides of this channel (`RUNNER_TOKEN_MIN_LENGTH` in
#: services/apps_runner/api.py and app/api/data_apps.py). The floor arrived
#: with the wave-2 audit hardening, which replaced a plain `!=` compare; a
#: shorter fixture token had matched fine until then and afterwards was
#: rejected before any comparison, as a flat `401 bad_runner_token`.
RUNNER_TOKEN = "daemon-test-token-daemon-test-token"


def _docker_or_skip():
    docker = pytest.importorskip("docker")
    try:
        client = docker.from_env()
        client.ping()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"docker daemon unreachable: {exc}")
    try:
        client.images.get(IMAGE)
    except Exception:  # noqa: BLE001
        pytest.skip(f"{IMAGE} not present locally — `docker pull {IMAGE}` first")
    return client


@contextlib.contextmanager
def _sidecar():
    """Run the apps-runner app on an ephemeral port; yield its base URL."""
    import uvicorn

    from services.apps_runner import api

    server = uvicorn.Server(uvicorn.Config(api.app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("apps-runner test server failed to start")
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _provider_and_client(base_url: str):
    from app.chat.docker_provider import DockerSandboxProvider
    from app.chat.sandbox_runner_client import SandboxRunnerClient

    client = SandboxRunnerClient(base_url=base_url, token=RUNNER_TOKEN)
    with patch("app.chat.docker_provider.SandboxRunnerClient", return_value=client):
        provider = DockerSandboxProvider(
            image=IMAGE,
            # Empty network → the daemon's default bridge; this test never
            # talks to Agnes, and it leaves no stray network behind.
            network="",
            mem_limit="512m",
            cpus=1.0,
            pids_limit=128,
            upload_runner=False,
        )
    return provider, client


def _session_dir(tmp_path: Path) -> Path:
    sdir = tmp_path / "users" / "a@b.com" / "sessions" / f"daemon-{secrets.token_hex(4)}"
    sdir.mkdir(parents=True)
    (tmp_path / "users" / "a@b.com" / "workspace").mkdir(parents=True, exist_ok=True)
    return sdir


def test_docker_sandbox_full_roundtrip(tmp_path: Path, monkeypatch):
    _docker_or_skip()
    monkeypatch.setenv("APPS_RUNNER_TOKEN", RUNNER_TOKEN)
    monkeypatch.setenv("CHAT_SANDBOX_IMAGE_PREFIX", IMAGE.split(":")[0])

    from app.chat.docker_provider import container_name

    sdir = _session_dir(tmp_path)
    chat_id = sdir.name

    async def _run(prov, client):
        handle = await prov.spawn(
            workdir=sdir,
            env={"AGNES_SESSION_ID": chat_id},
            argv=["/bin/sh", "-c", ECHO_PROGRAM],
        )
        try:
            assert (await asyncio.wait_for(handle.stdout.readline(), timeout=30)).strip() == b"ready"

            handle.stdin.write(b"before-pause\n")
            await handle.stdin.drain()
            assert b"echo: before-pause" in await asyncio.wait_for(handle.stdout.readline(), timeout=30)

            await prov.pause(handle)
            assert (await client.status(handle.sandbox_id))["container"] == "paused"

            resumed = await prov.resume(sandbox_id=handle.sandbox_id, runner_pid=1, env={})
            resumed.stdin.write(b"after-resume\n")
            await resumed.stdin.drain()
            assert b"echo: after-resume" in await asyncio.wait_for(resumed.stdout.readline(), timeout=30)

            # The ownership label makes the sandbox visible to the orphan sweep.
            rows = await prov.list_sandboxes()
            assert container_name(chat_id) in {r["name"] for r in rows}

            await resumed._detach()
            await prov.destroy(sandbox_id=handle.sandbox_id)
            assert (await client.status(handle.sandbox_id))["container"] == "absent"
        finally:
            await prov.destroy(sandbox_id=handle.sandbox_id)

    with _sidecar() as base_url:
        prov, client = _provider_and_client(base_url)
        asyncio.run(_run(prov, client))


def test_docker_sandbox_file_staging_and_harvest_shim(tmp_path: Path, monkeypatch):
    """`stage_file` writes to a container-only path; the `files` shim reads
    `/work/outputs` back the way `artifact_harvest` does."""
    _docker_or_skip()
    monkeypatch.setenv("APPS_RUNNER_TOKEN", RUNNER_TOKEN)
    monkeypatch.setenv("CHAT_SANDBOX_IMAGE_PREFIX", IMAGE.split(":")[0])

    sdir = _session_dir(tmp_path)
    (sdir / "outputs").mkdir()
    (sdir / "outputs" / "report.csv").write_text("a,b\n1,2\n")

    async def _run(prov):
        handle = await prov.spawn(
            workdir=sdir,
            env={"AGNES_SESSION_ID": sdir.name},
            argv=["/bin/sh", "-c", "sleep 60"],
        )
        try:
            await prov.stage_file(handle, "/tmp/agnes-cli/.ready", b"")
            assert await handle.files.read("/tmp/agnes-cli/.ready", format="bytes") == b""

            entries = await handle.files.list("/work/outputs")
            assert [e.name for e in entries] == ["report.csv"]
            assert await handle.files.read("/work/outputs/report.csv", format="bytes") == b"a,b\n1,2\n"
        finally:
            await handle.kill(grace_sec=1.0)

    with _sidecar() as base_url:
        prov, _client = _provider_and_client(base_url)
        asyncio.run(_run(prov))
