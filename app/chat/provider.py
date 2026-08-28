"""SandboxProvider Protocol — runtime extension point for sandbox engines."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol, runtime_checkable

# Default workdir inside the sandbox; matches the path the sandbox image
# creates (``RUN mkdir -p /work`` in
# app/initial_workspace_default/docker-sandbox/Dockerfile).
SANDBOX_WORKDIR = "/work"


class SandboxCapacityError(RuntimeError):
    """A provider with a host-wide sandbox ceiling has reached it.

    Typed rather than a bare ``RuntimeError`` so ChatManager can tell "this
    host is full — free a slot and retry" apart from "this spawn is broken"
    (see ``ChatManager._spawn_with_capacity_reclaim``). A ``RuntimeError``
    subclass so callers that only catch the base class keep working.
    """


def _coerce_to_bytes(data) -> bytes:
    if isinstance(data, bytes):
        return data
    if isinstance(data, str):
        return data.encode("utf-8", errors="replace")
    return str(data).encode("utf-8", errors="replace")


class _StreamReaderAdapter:
    """asyncio.StreamReader-like wrapper around an asyncio.Queue of bytes.

    Only the methods the chat stack actually uses are implemented
    (``readline`` and ``read``). Both honour an EOF sentinel pushed by
    the owning provider handle when the underlying command exits.
    """

    _EOF = object()

    def __init__(self) -> None:
        self._buf = bytearray()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._eof = False

    def feed(self, chunk: bytes) -> None:
        # Called from the provider's callback context. Non-blocking; the
        # queue is unbounded so a slow consumer never blocks the producer.
        self._queue.put_nowait(chunk)

    def feed_eof(self) -> None:
        self._queue.put_nowait(self._EOF)

    async def _pump(self) -> bool:
        """Consume one queue item; True if got data, False on EOF."""
        item = await self._queue.get()
        if item is self._EOF:
            self._eof = True
            return False
        self._buf.extend(item)
        return True

    async def readline(self) -> bytes:
        while True:
            # Find newline in current buffer.
            idx = self._buf.find(b"\n")
            if idx != -1:
                line = bytes(self._buf[: idx + 1])
                del self._buf[: idx + 1]
                return line
            if self._eof:
                if self._buf:
                    line = bytes(self._buf)
                    self._buf.clear()
                    return line
                return b""
            ok = await self._pump()
            if not ok and not self._buf:
                return b""

    async def read(self, n: int = -1) -> bytes:
        if n < 0:
            # Drain until EOF
            while not self._eof:
                await self._pump()
            data = bytes(self._buf)
            self._buf.clear()
            return data
        while len(self._buf) < n and not self._eof:
            await self._pump()
        take = min(n, len(self._buf))
        data = bytes(self._buf[:take])
        del self._buf[:take]
        return data


@runtime_checkable
class SandboxHandle(Protocol):
    pid: int
    sandbox_id: str  # provider-scoped id used for pause/resume
    stdin: asyncio.StreamWriter
    stdout: asyncio.StreamReader
    stderr: asyncio.StreamReader

    async def wait(self) -> int: ...
    async def kill(self, *, grace_sec: float = 5.0) -> None: ...


@runtime_checkable
class SandboxProvider(Protocol):
    async def spawn(
        self,
        *,
        workdir: Path,
        env: dict[str, str],
        argv: list[str],
    ) -> SandboxHandle: ...

    async def pause(self, handle: SandboxHandle) -> None:
        """Snapshot the sandbox (memory + fs + running processes) and detach."""
        ...

    async def resume(
        self,
        *,
        sandbox_id: str,
        runner_pid: int,
        env: dict[str, str],
    ) -> SandboxHandle:
        """Reconnect a paused sandbox and reattach to the still-running runner."""
        ...

    async def keepalive(self, handle: SandboxHandle, *, timeout_seconds: int) -> None:
        """Extend the sandbox's external timeout. No-op for local providers."""
        ...

    async def destroy(self, *, sandbox_id: str) -> None:
        """Delete a paused sandbox without resuming it. Used by the paused-TTL reaper."""
        ...
