"""`agnes push` — scan the workspace's Claude Code session folder and upload
new/grown transcripts (+ CLAUDE.local.md) to the server.

Mechanism (replaces the former stdin-capture + queue, which was unreliable on
macOS where Claude Code delivers empty hook stdin): the workspace root is read
from the Agnes config (``workspace_root``, written by ``agnes init`` and
back-filled by ``agnes self-upgrade``). push encodes it to Claude Code's
projects-dir folder name (``cli/lib/session_paths.py``) and lists the
``*.jsonl`` transcripts there. Each file's stem is its ``session_id``.

Dedup is by ``session_id`` + byte size against the upload ledger
(``cli/lib/upload_log.py``): unseen -> upload; same size -> skip; larger size
(the transcript grew) -> re-upload. The server overwrites by filename, so a
re-upload is idempotent. The ledger row is appended immediately after each
success, so an interrupted push never re-uploads a completed file next run.

No ``workspace_root`` in config -> nothing to find: push exits 0 without
uploading anything (sessions OR CLAUDE.local.md — both are anchored to the
same root, so without it neither can be located). Works identically on
Windows and macOS, with no dependency on hook stdin.

Concurrency: a single-instance lock (``cli/lib/push_lock.py``) means only one
push runs when several SessionEnd hooks fire at once; the rest exit silently.

Private filter: a session whose id is on the ``/agnes-private`` list
(``cli/lib/private_list.py``) is never uploaded; the skip is audit-logged to
``agnes-sessions-private-skipped.txt``.

Token redaction (#753): before either a session transcript or
CLAUDE.local.md leaves the client, JWT-shaped substrings (the Agnes PAT
format) are stripped via ``cli.lib.transcript_redact`` — this is the second
line of defense against the bootstrap heredoc pasting the raw PAT into the
setup session's transcript. The upload ledger still records the on-disk
file size (not the redacted upload size) so size-based dedup/grow detection
is unaffected.

Client-reported audit events (F3 — audit-full-coverage plan, Task 9): after
sessions and CLAUDE.local.md, push also drains ``cli.lib.audit_spool`` (up
to 500 events accumulated by offline ``agnes query``/``agnes explore`` local
runs) and uploads them in one batch to ``POST /api/upload/audit-events``.
Best-effort like everything else here — a failed upload leaves the spool
intact for the next push to retry, never fails the command.
"""

from __future__ import annotations

import gzip
import json
import os
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import typer

from cli.client import api_get, api_post
from cli.config import get_server_url, get_token, get_workspace_root
from cli.error_render import render_error
from cli.lib.audit_spool import commit_drain, drain_spool
from cli.lib.private_list import read_all_private
from cli.lib.push_lock import acquire_or_skip
from cli.lib.session_paths import list_session_files
from cli.lib.transcript_redact import redact_bytes, redact_text
from cli.lib.upload_log import (
    mark_failed_permanent,
    mark_private_skipped,
    mark_uploaded,
    read_failed_sessions,
    read_private_skipped_sessions,
    read_uploaded,
    uploaded_log_path,
)


push_app = typer.Typer(help="Upload sessions and CLAUDE.local.md to the server")


def _is_permanent_failure(info: dict) -> bool:
    """True iff the server response is a deterministic failure retrying won't fix.

    4xx except 401 / 408 / 429: 403 (RBAC), 413 (too large), 400 (validation)
    all re-produce the same answer on re-upload, so we log them to the
    forensic failed-log instead of retrying forever. 401 is recoverable
    (PAT expired — re-auth makes the same upload succeed), and 408 / 429 are
    transient per HTTP spec; all three (plus 5xx and network errors) are left
    unrecorded so the next push retries.
    """
    status = info.get("status")
    if not isinstance(status, int):
        return False  # network error / exception — transient
    if status in (401, 408, 429):
        return False
    return 400 <= status < 500


_GZIP_CAPABILITY = "session-gzip"


def _server_accepts_gzip() -> bool:
    """One capability probe per push run: does the server accept gzip uploads?

    Fail-open to the legacy plain format — env kill-switch set, probe error,
    or an old server without the `X-Agnes-Accepts` header all mean "no".
    A new client must NEVER send `.gz` to an old server: it would store the
    compressed bytes verbatim and silently corrupt the session corpus.
    """
    if os.environ.get("AGNES_PUSH_NO_GZIP") == "1":
        return False
    try:
        resp = api_get("/api/health", timeout=10.0)
    except Exception:
        return False
    caps = resp.headers.get("X-Agnes-Accepts", "")
    return _GZIP_CAPABILITY in [t.strip() for t in caps.split(",")]


def _upload_one(transcript: Path, use_gzip: bool = False) -> tuple[bool, dict]:
    """Upload a single session jsonl. Returns (success, error_or_meta).

    The on-disk bytes are redacted (JWT-shaped tokens stripped, #753) into an
    in-memory buffer before upload — transcripts are bounded in size, so
    holding a redacted copy in memory is fine. With ``use_gzip`` the redacted
    buffer is additionally gzip-compressed and the part filename gains a
    ``.gz`` suffix (server capability ``session-gzip``); redaction ALWAYS
    happens first, on the raw bytes. The ledger records the on-disk size
    (see the caller), not this buffer's size.
    """
    if not transcript.exists():
        return False, {"file": transcript.name, "error": "file not found on disk"}
    try:
        raw = transcript.read_bytes()
        payload = redact_bytes(raw)
        name = transcript.name
        if use_gzip:
            payload = gzip.compress(payload)
            name = f"{name}.gz"
        buf = BytesIO(payload)
        resp = api_post("/api/upload/sessions", files={"file": (name, buf)})
    except Exception as exc:
        return False, {"file": transcript.name, "error": str(exc)}
    if resp.status_code == 200:
        return True, {"file": transcript.name}
    return False, {"file": transcript.name, "status": resp.status_code}


def _partition(
    workspace: Path,
) -> tuple[list[tuple[str, Path, int]], list[tuple[str, Path]], int, int]:
    """Scan the workspace session folder and split transcripts into
    ``(to_upload, private_hits, skipped_unchanged, skipped_failed)``.

    - private (session_id on the private list) -> ``private_hits`` (skip + audit)
    - permanently failed (session_id in the failed-log) -> skipped, never
      retried — the scan-based equivalent of the old queue dropping a
      permanently-rejected entry; without this the same 4xx-rejected file would
      re-upload (and re-fail, and re-log) on every SessionEnd hook
    - already uploaded at the same byte size -> ``skipped_unchanged``
    - everything else (new or grown) -> ``to_upload``
    """
    candidates = list_session_files(workspace)
    uploaded = read_uploaded(workspace)
    private_ids = read_all_private(workspace)
    failed_ids = read_failed_sessions(workspace)

    to_upload: list[tuple[str, Path, int]] = []
    private_hits: list[tuple[str, Path]] = []
    skipped_unchanged = 0
    skipped_failed = 0
    for p in candidates:
        sid = p.stem
        if sid and sid in private_ids:
            private_hits.append((sid, p))
            continue
        if sid and sid in failed_ids:
            skipped_failed += 1
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        prev = uploaded.get(sid)
        if prev is not None and prev == size:
            skipped_unchanged += 1
            continue
        to_upload.append((sid, p, size))
    return to_upload, private_hits, skipped_unchanged, skipped_failed


@push_app.callback(invoke_without_command=True)
def push(
    quiet: bool = typer.Option(
        False,
        "--quiet",
        help="Suppress success stdout (errors still surface on stderr).",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit a single JSON object summarizing the upload."),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="List what would be uploaded without sending anything.",
    ),
):
    """Upload new/grown session jsonls + CLAUDE.local.md from this workspace."""
    server_url = get_server_url()
    if not server_url:
        typer.echo(
            render_error(
                0,
                {
                    "detail": {
                        "kind": "server_unreachable",
                        "hint": "No server configured. Run: agnes init --server-url <URL> --token <PAT>",
                    }
                },
            ),
            err=True,
        )
        raise typer.Exit(1)

    token = get_token()
    if not token:
        typer.echo(
            render_error(
                0,
                {
                    "detail": {
                        "kind": "auth_failed",
                        "hint": "No token. Run: agnes auth import-token --token <PAT>",
                    }
                },
            ),
            err=True,
        )
        raise typer.Exit(1)

    # The workspace root is the ONLY anchor. Without it we can't locate the
    # Claude Code session folder OR the workspace's CLAUDE.local.md, so push
    # is a clean no-op (exit 0). `agnes init` writes it; `agnes self-upgrade`
    # back-fills it on the next SessionStart for older clients.
    workspace_root = get_workspace_root()
    if not workspace_root:
        if as_json:
            typer.echo(
                json.dumps(
                    {
                        "sessions": 0,
                        "local_md": False,
                        "errors": [],
                        "private_skipped": 0,
                        "dropped_permanent": 0,
                        "skipped_unchanged": 0,
                        "skipped_failed": 0,
                        "workspace_root": None,
                    }
                )
            )
        elif not quiet:
            typer.echo(
                "No workspace_root in config — nothing to upload. Run `agnes init` to set it.",
                err=True,
            )
        return

    workspace = Path(workspace_root)
    local_md = workspace / ".claude" / "CLAUDE.local.md"
    has_local_md = local_md.exists()

    # ---- DRY RUN ----------------------------------------------------------
    # Read-only planning; safe to compute outside the push lock.
    if dry_run:
        to_upload, private_hits, skipped_unchanged, skipped_failed = _partition(workspace)
        plan = {
            "dry_run": True,
            "would_upload": {
                "sessions": [str(p) for _sid, p, _sz in to_upload],
                "local_md": str(local_md) if has_local_md else None,
            },
            "would_skip_private": [{"session_id": sid, "path": str(p)} for sid, p in private_hits],
            "summary": {
                "sessions_count": len(to_upload),
                "private_skipped_count": len(private_hits),
                "skipped_unchanged": skipped_unchanged,
                "skipped_failed": skipped_failed,
                "local_md_present": has_local_md,
                "uploaded_log": str(uploaded_log_path(workspace)),
            },
        }
        if as_json:
            typer.echo(json.dumps(plan, indent=2))
            return
        if quiet:
            return
        typer.echo(f"Dry run - would upload {len(to_upload)} session file(s)")
        for _sid, p, _sz in to_upload:
            typer.echo(f"  {p}")
        if private_hits:
            typer.echo(f"Would skip {len(private_hits)} private session(s):")
            for sid, p in private_hits:
                typer.echo(f"  [{sid}] {p}")
        if has_local_md:
            typer.echo(f"Would upload CLAUDE.local.md  ({local_md})")
        else:
            typer.echo("No CLAUDE.local.md to upload")
        return

    # ---- REAL RUN ---------------------------------------------------------
    # Acquire single-instance lock. Silent exit if another push already holds
    # it — typical when several SessionEnd hooks fire at once.
    with acquire_or_skip(workspace) as lock:
        if lock is None:
            return  # another push has the lock; this one no-ops

        # Recompute the scan/partition INSIDE the lock: a push that held the
        # lock while we waited may have uploaded transcripts and grown the
        # ledger (or consulted the private list), so reading before the lock
        # could re-upload an already-handled file or miss a fresh marker.
        to_upload, private_hits, skipped_unchanged, skipped_failed = _partition(workspace)

        results: dict = {
            "sessions": 0,
            "local_md": False,
            "errors": [],
            "private_skipped": 0,
            "dropped_permanent": 0,
            "skipped_unchanged": skipped_unchanged,
            "skipped_failed": skipped_failed,
            "workspace_root": str(workspace),
        }
        now = datetime.now(timezone.utc)

        # Private sessions: never upload. Audit-log each only ONCE — the file
        # stays on disk across runs and the private list is persistent, so
        # re-logging every push would grow the audit trail unboundedly. The
        # per-run counter still reflects all private sessions skipped this run.
        already_skipped = read_private_skipped_sessions(workspace)
        for sid, p in private_hits:
            results["private_skipped"] += 1
            if sid not in already_skipped:
                mark_private_skipped(workspace, sid, p, now)
                already_skipped.add(sid)

        # One capability probe per run, and only when there is work — a
        # no-change push (the common SessionEnd case) makes no extra request.
        use_gzip = bool(to_upload) and _server_accepts_gzip()

        for sid, p, size in to_upload:
            ok, info = _upload_one(p, use_gzip=use_gzip)
            if ok:
                results["sessions"] += 1
                # Record immediately (crash-safe): the next push won't re-send.
                mark_uploaded(workspace, sid, size, now)
                continue
            results["errors"].append(info)
            if _is_permanent_failure(info):
                # 4xx (except 401 / 408 / 429): server will never accept it.
                mark_failed_permanent(workspace, sid, p, info["status"], now)
                results["dropped_permanent"] += 1
            # Transient (401 / 408 / 429 / 5xx / network / file-not-found):
            # leave it OUT of the ledger so the next push retries.

        # Upload CLAUDE.local.md from the anchored workspace root. Redacted
        # (#753) for the same reason as sessions — analysts sometimes paste
        # tokens/credentials into it while documenting local setup.
        if has_local_md:
            try:
                content = redact_text(local_md.read_text(encoding="utf-8"))
                resp = api_post("/api/upload/local-md", json={"content": content})
                if resp.status_code == 200:
                    results["local_md"] = True
                else:
                    results["errors"].append({"file": "CLAUDE.local.md", "status": resp.status_code})
            except Exception as exc:
                results["errors"].append({"file": "CLAUDE.local.md", "error": str(exc)})

        # Client-reported CLI audit events (F3 — audit-full-coverage plan,
        # Task 9). Best-effort, after sessions: drains up to 500 events
        # spooled by `agnes query`/`agnes explore` local runs and uploads
        # them in one batch. A server outage (or any other failure) must
        # NOT fail `agnes push` — `commit_drain()` only runs on a 200, so a
        # failed upload leaves the spool intact for the next push to retry.
        try:
            spooled_events = drain_spool(max_events=500)
            if spooled_events:
                resp = api_post("/api/upload/audit-events", json={"events": spooled_events})
                if resp.status_code == 200:
                    commit_drain()
        except Exception:
            pass

    # Render output.
    if as_json:
        typer.echo(json.dumps(results))
        return

    if quiet:
        if results["errors"]:
            for e in results["errors"]:
                typer.echo(f"warn: {e}", err=True)
        return

    typer.echo(f"Uploaded {results['sessions']} sessions")
    if results["private_skipped"]:
        typer.echo(
            f"Skipped {results['private_skipped']} private session(s) (see .claude/agnes-sessions-private-skipped.txt)"
        )
    if results["dropped_permanent"]:
        typer.echo(
            f"Dropped {results['dropped_permanent']} session(s) with permanent failure "
            f"(see .claude/agnes-sessions-failed.txt)"
        )
    if results["skipped_failed"]:
        typer.echo(
            f"Skipped {results['skipped_failed']} session(s) previously logged as "
            f"permanently failed (clear .claude/agnes-sessions-failed.txt to retry)"
        )
    if results["local_md"]:
        typer.echo("Uploaded CLAUDE.local.md")
    if results["errors"]:
        for e in results["errors"]:
            typer.echo(f"warn: {e}", err=True)
