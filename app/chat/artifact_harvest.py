"""Harvest agent-written output files from a session's REMOTE sandbox into
the object store + ``agent_artifacts`` registry (V1b Task 5).

**Where artifacts actually live.** The chat sandbox is an E2B microVM, not a
host directory — there is no ``workdir/outputs`` on the Agnes host to scan.
The session's local workdir only ever holds the per-user *workspace* that
gets uploaded INTO the VM at spawn time (``app/chat/e2b_workspace_sync.py``
``upload_workspace``, pushed under ``SANDBOX_WORKDIR`` = ``/work`` —
``app/chat/e2b_provider.py``). So harvesting means reading files back OUT of
the live sandbox over the E2B file API (``handle.files.list`` /
``handle.files.read``), not walking a local ``Path``.

**Why a dedicated ``outputs/`` subdir, not all of ``/work``.** ``/work``
also contains the uploaded workspace tree (``CLAUDE.md``, ``.claude/``,
whatever the operator's workspace template staged). Scanning all of
``/work`` would re-harvest that as if the agent had produced it. Instead,
only ``{SANDBOX_WORKDIR}/outputs`` (``/work/outputs``) is scanned — the
agent must write deliverables there for them to become artifacts. This is a
convention this module documents and enforces by scope (not by refusing
writes elsewhere): files written directly under ``/work`` still flow back to
the user's persistent workspace via ``download_workspace`` at session end,
they are simply not treated as harvestable "artifacts".

**Filenames are attacker-controlled input.** An in-VM filename is chosen by
whatever the agent's tool calls did — nothing stops a compromised or
adversarial run from writing a file named ``../../etc/passwd``,
``evil\\r\\nX-Injected: true``, or ``a".txt``. Every filename is sanitized
to a bare, CR/LF/quote-free basename (:func:`sanitize_filename`) before it
is used to build the object-store key or served back in a
``Content-Disposition`` header — this defeats path-traversal-into-the-key,
HTTP header injection, and quote-breakout of the header's ``filename="..."``
value.

**Callers, never the runner itself.** This module is invoked from:

- ``app.chat.headless`` — after a one-shot turn's ``"done"`` frame lands,
  while the sandbox handle is still live (before the sink's detach starts
  the pause/linger countdown).
- ``app.api.agent_sessions`` — on ``DELETE /api/v1/sessions/{id}``, before
  ``manager.kill()`` tears the sandbox down.

Deliberately NOT hooked into every turn of a live multi-turn session (only
one-shot completion + explicit teardown) — see the V1b Task 5 design notes.

**Best-effort, always.** ``object_store()`` returning ``None`` (signed-URL
distribution not configured), a missing/absent outputs dir, or a single
file's read/put failing are all non-fatal: this function never raises into
the run/teardown path it piggybacks on. Worst case it harvests fewer files
than were actually written; it never blocks or crashes the caller.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import uuid
from typing import Any, Optional

from src.object_store import object_store
from src.repositories import agent_artifacts_repo

logger = logging.getLogger(__name__)

#: Sandbox-relative directory (under SANDBOX_WORKDIR) the agent must write
#: deliverables to for them to be harvested. See module docstring for why
#: this is not all of /work.
OUTPUTS_SUBDIR = "outputs"

#: Defaults mirror `agent_api.artifact_max_bytes` / `agent_api.artifact_max_files`
#: in `app/chat/config.py` (ChatConfig.agent_api_artifact_max_bytes /
#: agent_api_artifact_max_files) — duplicated here as the function's own
#: defaults so `harvest_session_artifacts` is independently testable/callable
#: without requiring a ChatConfig instance; call sites normally pass the
#: configured values explicitly.
DEFAULT_ARTIFACT_MAX_BYTES = 25 * 1024 * 1024
DEFAULT_ARTIFACT_MAX_FILES = 20

#: Object-store key prefix every harvested artifact is written under —
#: `agent-artifacts/{session_id}/{safe_filename}`. This is the REAL prefix
#: (not the `artifacts/` prefix an earlier contract test guessed at).
OBJECT_KEY_PREFIX = "agent-artifacts"


def sanitize_filename(raw: str) -> str:
    """Collapse an agent-chosen in-VM filename to a safe, flat basename.

    Three attacks defeated:
    - path traversal into the object-store key / a served download header
      (``../../etc/passwd`` -> ``passwd`` via ``os.path.basename``, which
      also collapses any embedded path separators regardless of depth);
    - HTTP response-header injection via embedded CR/LF (stripped before
      ``os.path.basename`` runs, so a name like ``evil\\r\\nX-Injected:
      true`` becomes a single flat token, not a header-splitting payload);
    - quote-breakout in a ``Content-Disposition: ...; filename="..."``
      header (``"`` is stripped too, so a name like ``a".txt`` can't close
      the quoted string early and inject trailing header syntax).

    An empty result (``""``, ``"."``, or ``".."`` after stripping) falls
    back to ``"unnamed"`` rather than producing a hidden or root-referring
    object key.
    """
    cleaned = raw.replace("\r", "").replace("\n", "").replace('"', "")
    name = os.path.basename(cleaned)
    if not name or name in (".", ".."):
        name = "unnamed"
    return name


def _entry_type(entry: Any) -> str:
    """Normalize an EntryInfo-shaped object's `.type` — mirrors
    `e2b_workspace_sync._entry_type` (kept local rather than imported so
    this module has no dependency on the workspace-sync module, only on
    the SANDBOX_WORKDIR constant)."""
    t = getattr(entry, "type", None)
    if t is None:
        return "FILE"
    return "DIR" if "DIR" in str(t).upper() else "FILE"


async def _read_bytes(files_api: Any, remote_path: str) -> bytes:
    try:
        data = await files_api.read(remote_path, format="bytes")
    except TypeError:
        # Older SDK without a format= kwarg (mirrors e2b_workspace_sync.download_workspace).
        data = await files_api.read(remote_path)
    if isinstance(data, str):
        data = data.encode("utf-8")
    return data


def caps_from_manager(manager: Any) -> dict:
    """Resolve ``{max_bytes, max_files}`` kwargs for
    :func:`harvest_session_artifacts` from a live ``ChatManager``'s
    ``ChatConfig`` (``agent_api_artifact_max_bytes`` /
    ``agent_api_artifact_max_files``) — so an operator's `instance.yaml`
    override actually takes effect at both call sites
    (``app.chat.headless``, ``app.api.agent_sessions``) instead of only the
    function's own hardcoded defaults. Reaches into ``ChatManager._config``
    (private) rather than a public accessor, same documented-adaptation
    pattern ``headless._last_assistant_message`` uses for ``_repo`` — falls
    back to an empty dict (i.e. the function's own defaults) when the
    manager has no ``_config`` (a fake/test manager) or the config lacks
    these attributes."""
    config = getattr(manager, "_config", None)
    if config is None:
        return {}
    kwargs: dict = {}
    max_bytes = getattr(config, "agent_api_artifact_max_bytes", None)
    if max_bytes is not None:
        kwargs["max_bytes"] = max_bytes
    max_files = getattr(config, "agent_api_artifact_max_files", None)
    if max_files is not None:
        kwargs["max_files"] = max_files
    return kwargs


async def harvest_session_artifacts(
    session_id: str,
    agent_id: Optional[str],
    owner_user_id: str,
    handle: Any,
    *,
    max_bytes: int = DEFAULT_ARTIFACT_MAX_BYTES,
    max_files: int = DEFAULT_ARTIFACT_MAX_FILES,
) -> list[dict]:
    """Harvest every file under the sandbox's ``outputs/`` dir.

    Reads back over ``handle.files.list`` / ``handle.files.read`` — the
    live sandbox's file API — NOT a host filesystem scan (see module
    docstring). For each file: sanitize the filename, compute its md5,
    upload to the object store under
    ``agent-artifacts/{session_id}/{safe_filename}``, and insert an
    ``agent_artifacts`` row. Returns the metadata for every artifact
    actually harvested (a subset of what was in the outputs dir if any
    caps were hit or any individual file failed).

    Never raises into the caller:

    - ``object_store()`` returning ``None`` (distribution not configured)
      -> log + return ``[]`` immediately, no listing attempted.
    - Listing the outputs dir failing (most commonly: it doesn't exist —
      the agent never wrote anything this run) -> log + return ``[]``.
    - A single file's read or store-write failing -> log + skip that file,
      keep going.

    Caps are best-effort guardrails, not a promise of exhaustive capture:
    ``max_files`` stops the scan after that many files are harvested
    (later entries are never looked at); ``max_bytes`` is a **per-session
    cumulative** cap — a running total of bytes harvested so far this call
    — and a file is skipped (but does not abort the scan) once harvesting
    it would push that running total over the cap, even if the individual
    file itself is small.

    Already-harvested files (same ``object_key`` already present in
    ``agent_artifacts_repo().list_for_session(session_id)``) are skipped
    without re-inserting a row — this function can be called more than
    once for the same session (one-shot completion, then again at `DELETE
    /api/v1/sessions/{id}` teardown) and must not create duplicate rows
    for files it already harvested.
    """
    from app.chat.provider import SANDBOX_WORKDIR

    store = object_store()
    if store is None:
        logger.info(
            "harvest_session_artifacts: object store not configured — skipping session %s",
            session_id,
        )
        return []

    files_api = getattr(handle, "files", None)
    if files_api is None:
        logger.warning(
            "harvest_session_artifacts: handle has no files API — skipping session %s",
            session_id,
        )
        return []

    outputs_path = f"{SANDBOX_WORKDIR}/{OUTPUTS_SUBDIR}"
    try:
        entries = await files_api.list(outputs_path)
    except Exception:
        # Overwhelmingly the common case: the agent never created an
        # outputs/ dir this run. Not an error condition.
        logger.debug(
            "harvest_session_artifacts: no outputs dir at %s for session %s (or listing failed)",
            outputs_path,
            session_id,
        )
        return []

    try:
        existing_keys = {row["object_key"] for row in agent_artifacts_repo().list_for_session(session_id)}
    except Exception:
        logger.exception(
            "harvest_session_artifacts: list_for_session failed for session %s — proceeding without dedupe",
            session_id,
        )
        existing_keys = set()

    results: list[dict] = []
    bytes_harvested = 0
    for entry in entries:
        if len(results) >= max_files:
            logger.info(
                "harvest_session_artifacts: hit max_files=%d cap for session %s — stopping scan",
                max_files,
                session_id,
            )
            break
        if _entry_type(entry) != "FILE":
            continue
        raw_name = getattr(entry, "name", "") or ""
        if not raw_name:
            continue

        safe_name = sanitize_filename(raw_name)
        object_key = f"{OBJECT_KEY_PREFIX}/{session_id}/{safe_name}"

        if object_key in existing_keys:
            logger.info(
                "harvest_session_artifacts: %s already harvested for session %s — skipping",
                object_key,
                session_id,
            )
            continue

        remote_path = f"{outputs_path}/{raw_name}"
        try:
            data = await _read_bytes(files_api, remote_path)
        except Exception:
            logger.exception(
                "harvest_session_artifacts: failed to read %s for session %s — skipping",
                remote_path,
                session_id,
            )
            continue

        if bytes_harvested + len(data) > max_bytes:
            logger.warning(
                "harvest_session_artifacts: %s (%d bytes) would push session %s's cumulative "
                "harvest past the %d byte cap (already at %d bytes) — skipping",
                safe_name,
                len(data),
                session_id,
                max_bytes,
                bytes_harvested,
            )
            continue

        md5 = hashlib.md5(data).hexdigest()
        content_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"

        try:
            store.put_bytes(object_key, data, md5)
        except Exception:
            logger.exception(
                "harvest_session_artifacts: put_bytes failed for %s (session %s) — skipping",
                object_key,
                session_id,
            )
            continue

        artifact_id = uuid.uuid4().hex
        try:
            agent_artifacts_repo().create(
                id=artifact_id,
                session_id=session_id,
                agent_id=agent_id,
                owner_user_id=owner_user_id,
                filename=safe_name,
                object_key=object_key,
                size_bytes=len(data),
                content_type=content_type,
                md5=md5,
            )
        except Exception:
            logger.exception(
                "harvest_session_artifacts: agent_artifacts_repo().create failed for "
                "%s (session %s) — object already stored, row missing",
                object_key,
                session_id,
            )
            continue

        bytes_harvested += len(data)
        existing_keys.add(object_key)
        results.append(
            {
                "id": artifact_id,
                "filename": safe_name,
                "object_key": object_key,
                "size_bytes": len(data),
                "content_type": content_type,
                "md5": md5,
            }
        )

    return results
