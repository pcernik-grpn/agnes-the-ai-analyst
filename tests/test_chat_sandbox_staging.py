"""Provider-agnostic sandbox staging (app/chat/sandbox_staging.py).

``stage_agnes_wheel`` receives the provider-supplied ``async (path, data)``
callable, so a plain recorder is all these tests need — no fake sandbox.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.chat.sandbox_staging import (
    SANDBOX_WHEEL_DIR,
    SANDBOX_WHEEL_READY,
    stage_agnes_wheel,
)


def _recorder():
    written: dict[str, bytes] = {}

    async def stage(path: str, data: bytes) -> None:
        written[path] = data

    return stage, written


def test_stage_agnes_wheel_preserves_pep427_filename(tmp_path: Path, monkeypatch):
    """The wheel is staged under its original PEP 427 name (pip rejects a
    renamed wheel) in the dedicated dir outside /work."""

    async def _run():
        wheel = tmp_path / "agnes_the_ai_analyst-0.55.25-py3-none-any.whl"
        wheel.write_bytes(b"PK\x03\x04 fake wheel bytes")

        # Stub the shared wheel-discovery helper to return our fake wheel.
        monkeypatch.setattr("app.api.cli_artifacts._find_wheel", lambda: wheel)

        stage, written = _recorder()
        dest = await stage_agnes_wheel(stage)

        expected = f"{SANDBOX_WHEEL_DIR}/{wheel.name}"
        assert dest == expected
        # Staged outside the workspace dir, and never flattened to a
        # version-less name pip would reject.
        assert not dest.startswith("/work")
        assert dest.endswith(".whl") and "0.55.25" in dest
        assert expected in written
        assert written[expected] == b"PK\x03\x04 fake wheel bytes"
        # Sentinel written so the runner's wait terminates.
        assert SANDBOX_WHEEL_READY in written

    asyncio.run(_run())


def test_stage_agnes_wheel_noop_when_no_wheel(monkeypatch):
    """A dev image without a built wheel returns None — but still writes the
    sentinel so the runner doesn't block on its bounded wait."""

    async def _run():
        monkeypatch.setattr("app.api.cli_artifacts._find_wheel", lambda: None)
        stage, written = _recorder()
        dest = await stage_agnes_wheel(stage)
        assert dest is None
        assert list(written) == [SANDBOX_WHEEL_READY]

    asyncio.run(_run())
