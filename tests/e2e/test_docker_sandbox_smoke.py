"""Docker chat-sandbox smoke test — real image + real sidecar, opt-in via
AGNES_E2E_DOCKER=1.

The canary for "the built `agnes-chat-sandbox` image + a running
apps-runner + our provider" (its E2B sibling, tests/e2e/test_e2b_smoke.py,
was deleted with the e2b provider, 2026-08). It talks to
a *deployed* sidecar (`APPS_RUNNER_URL` / `APPS_RUNNER_TOKEN`), so it also
proves the token, the image allowlist, and the bind-mount host-path translation
in the operator's real topology.

Unit coverage is `tests/test_chat_docker_provider.py` (sidecar mocked);
daemon-only coverage is `tests/test_chat_docker_provider_daemon.py`
(`-m docker`, stock image, in-process sidecar).
"""

from __future__ import annotations

import asyncio
import os
import secrets
from pathlib import Path

import pytest


def _skip_unless_docker_smoke():
    if not os.environ.get("AGNES_E2E_DOCKER"):
        pytest.skip("AGNES_E2E_DOCKER=1 not set — skipping real docker-sandbox smoke")
    if not os.environ.get("APPS_RUNNER_TOKEN"):
        pytest.skip("APPS_RUNNER_TOKEN not set — required to reach the sidecar")


def test_docker_sandbox_spawns_and_echoes(tmp_path: Path) -> None:
    """Spawn a real sandbox against the real sidecar, run echo, read stdout."""
    _skip_unless_docker_smoke()

    from app.chat.docker_provider import DockerSandboxProvider

    image = os.environ.get("AGNES_DOCKER_SANDBOX_IMAGE", "agnes-chat-sandbox:latest")
    sdir = tmp_path / "users" / "smoke@example.com" / "sessions" / f"smoke-{secrets.token_hex(4)}"
    sdir.mkdir(parents=True)
    (tmp_path / "users" / "smoke@example.com" / "workspace").mkdir(parents=True)

    async def _run():
        prov = DockerSandboxProvider(
            image=image,
            network=os.environ.get("AGNES_DOCKER_SANDBOX_NETWORK", "agnes-apps"),
            upload_runner=False,  # echo only — no runner needed
        )
        handle = await prov.spawn(
            workdir=sdir,
            env={"AGNES_SESSION_ID": sdir.name, "AGNES_E2E_PROBE": "hello"},
            argv=["/bin/echo", "smoke", "ok"],
        )
        try:
            line = await asyncio.wait_for(handle.stdout.readline(), timeout=60)
            assert b"smoke" in line and b"ok" in line, f"unexpected stdout from echo: {line!r}"
            rc = await asyncio.wait_for(handle.wait(), timeout=60)
            assert rc == 0, f"echo exited rc={rc}"
        finally:
            await handle.kill(grace_sec=2.0)

    asyncio.run(_run())


def test_docker_sandbox_image_satisfies_the_filesystem_contract(tmp_path: Path) -> None:
    """The image must give the runner what ChatManager's env assumes: a
    writable ``$HOME``, a writable ``/work``, and a ``pip install`` whose
    console scripts land on the fixed ``PATH`` (``/usr/local/bin``)."""
    _skip_unless_docker_smoke()

    from app.chat.docker_provider import DockerSandboxProvider

    image = os.environ.get("AGNES_DOCKER_SANDBOX_IMAGE", "agnes-chat-sandbox:latest")
    sdir = tmp_path / "users" / "smoke@example.com" / "sessions" / f"smoke-{secrets.token_hex(4)}"
    sdir.mkdir(parents=True)

    probe = (
        "import os,sys;"
        "open(os.path.join(os.environ['HOME'],'.probe'),'w').write('x');"
        "open('/work/.probe','w').write('x');"
        "open('/usr/local/bin/.probe','w').write('x');"
        "print('contract-ok')"
    )

    async def _run():
        prov = DockerSandboxProvider(
            image=image,
            network=os.environ.get("AGNES_DOCKER_SANDBOX_NETWORK", "agnes-apps"),
            upload_runner=False,
        )
        handle = await prov.spawn(
            workdir=sdir,
            env={"AGNES_SESSION_ID": sdir.name, "HOME": "/home/user", "PATH": "/usr/local/bin:/usr/bin:/bin"},
            argv=["python3", "-c", probe],
        )
        try:
            line = await asyncio.wait_for(handle.stdout.readline(), timeout=60)
            assert b"contract-ok" in line, f"image filesystem contract violated: {line!r}"
        finally:
            await handle.kill(grace_sec=2.0)

    asyncio.run(_run())
