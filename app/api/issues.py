"""Issue reports — REST surface ("Report a problem", step 1).

**Postgres-only** (A3 PG-first ratchet). Every route resolves
``issue_reports_repo()`` through a dependency, so on a DuckDB-backed instance
``RequiresPostgresBackend`` is raised *before* the request body is even
validated and ``app/main.py`` translates it to a typed
``501 requires_postgres_backend`` — never a hand-rolled try/except, and never
a route that answers 422 on an instance whose real answer is "this needs
Postgres". See ``docs/superpowers/specs/2026-09-09-issue-reporting-step1-
design.md`` and ``CLAUDE.md`` -> "Dual-backend discipline".

RBAC shape, same reasoning as ``app/api/semantic_feedback.py``:

* ``POST /api/issues`` and everything under ``/api/issues/*`` — **any
  signed-in caller**. Restricting "report a problem" to admins would mean
  the only people who can flag one are the ones who never hit it.
* Ownership on a single report is enforced with a 404, not a 403 — "no such
  issue you can see" rather than "forbidden", so ids are not probeable.
* ``/api/admin/issues*`` — admin only: the queue across every reporter, and
  resolving one.

The operator mirror (``app.services.issue_notifier``) runs as a
``BackgroundTasks`` job after the 201 response: the record in
``issue_reports`` is the source of truth, the webhook post is a best-effort
copy that must never slow down or fail the request that created the row.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.auth.access import is_user_admin, require_admin
from app.auth.dependencies import get_current_user
from app.utils import get_data_dir
from src.models.issue_reports import ISSUE_STATUSES

logger = logging.getLogger(__name__)
router = APIRouter(tags=["issues"])

_MAX_CONTEXT_BYTES = 32 * 1024
_MAX_CONTEXT_STRING = 300
_MAX_SCREENSHOT_BYTES = 3 * 1024 * 1024
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

#: How long the operator mirror waits for a screenshot the client said it is
#: about to upload, before posting the summary without one. Comfortably longer
#: than a page capture (measured ~10 s on a heavy page) and far shorter than a
#: person's patience for the message to appear in chat.
_SCREENSHOT_WAIT_SEC = 20.0
_SCREENSHOT_POLL_SEC = 1.0


def _issues_repo() -> Any:
    """Resolve the PG-only repo AS A DEPENDENCY.

    Not inside the handler body: FastAPI solves dependencies before it
    validates the request body, so raising here is what makes a DuckDB-backed
    instance answer the typed ``501`` rather than a ``422`` about a field it
    could never have used anyway.
    """
    from src.repositories import issue_reports_repo

    return issue_reports_repo()


class IssueCreate(BaseModel):
    """One report. Only the title is required.

    The caps are the point of declaring these at all: ``context`` is
    client-captured (browser, recent errors) and an uncapped field is an
    invitation to store megabytes of paste per row.
    """

    title: str = Field(max_length=200)
    body: str | None = Field(default=None, max_length=8000)
    kind: Literal["bug", "wrong_answer", "request", "question", "other"] = "bug"
    page_url: str | None = Field(default=None, max_length=2000)
    context: dict[str, Any] | None = None
    #: "A screenshot is coming on the follow-up PUT." Only the client knows —
    #: it has the checkbox (web) or the `--screenshot` flag (CLI) — and the
    #: operator mirror uses it to wait for the upload instead of posting a
    #: summary that promises a link the row does not have yet.
    expect_screenshot: bool = False


class CommentCreate(BaseModel):
    body: str = Field(max_length=8000)


class IssueResolve(BaseModel):
    resolution_note: str | None = Field(default=None, max_length=4000)


def _err(status: int, code: str, message: str, hint: str | None = None) -> HTTPException:
    detail: dict[str, Any] = {"error": code, "message": message}
    if hint:
        detail["hint"] = hint
    return HTTPException(status_code=status, detail=detail)


def _clean(value: str | None) -> str | None:
    """Trim; treat whitespace-only as absent."""
    if value is None:
        return None
    s = value.strip()
    return s or None


def _cap_context(ctx: dict[str, Any] | None) -> dict[str, Any] | None:
    """Truncate every string leaf to 300 chars, refuse >32 KB after capping.

    Client-supplied, so shape is not trusted: anything that is not a dict
    becomes ``None`` rather than raising — a malformed ``context`` is not
    worth failing the whole report over.
    """
    if not isinstance(ctx, dict):
        return None

    def cap(v: Any) -> Any:
        if isinstance(v, str):
            return v if len(v) <= _MAX_CONTEXT_STRING else v[:_MAX_CONTEXT_STRING]
        if isinstance(v, dict):
            return {str(k)[:64]: cap(x) for k, x in list(v.items())[:64]}
        if isinstance(v, list):
            return [cap(x) for x in v[:50]]
        return v

    capped = cap(ctx)
    # Encoded bytes, not code points: the cap is named in bytes and one
    # emoji is four of them, so counting characters accepted payloads several
    # times over the promised limit (Devin review on #2402).
    if len(json.dumps(capped, default=str).encode("utf-8")) > _MAX_CONTEXT_BYTES:
        raise _err(400, "context_too_large", "context must be under 32 KB after truncation")
    return capped


def _server_context(request: Request) -> dict[str, Any]:
    from app.logging_config import request_id_var
    from app.version import APP_VERSION

    return {
        "app_version": APP_VERSION,
        "app_commit": os.environ.get("AGNES_COMMIT_SHA", "unknown"),
        # `RequestIdMiddleware` binds the per-request id into this ContextVar
        # (never `request.state`) so it also propagates into BackgroundTasks —
        # see `app/job_correlation.py` for the same read pattern.
        "request_id": request.headers.get("x-request-id") or request_id_var.get(),
    }


def _surface(request: Request) -> str:
    kind = (request.headers.get("x-agnes-client") or "").lower()
    return kind if kind in ("cli", "mcp") else "web"


def _public_base_url(request: Request) -> str:
    try:
        from app.instance_config import get_value

        configured = str(get_value("server", "public_url", default="") or "").strip()
    except Exception:  # noqa: BLE001 - a missing/broken config must not fail the mirror
        configured = ""
    return configured.rstrip("/") or str(request.base_url).rstrip("/")


def _reporter(user: Any) -> tuple[str, str | None]:
    """Who is filing, as ``(user_id, email)`` — never index ``user`` directly.

    ``get_current_user`` hands back EITHER a user dict OR one of the frozen
    restricted principals (``app/auth/session_principal.py``), returned
    verbatim precisely because they are not dicts. Every route in this module
    goes through here, so a sandboxed caller gets a decision rather than a
    ``TypeError`` on ``user["id"]`` — which is what the in-sandbox
    ``report_issue`` tool used to hit, i.e. the one path this whole channel
    was built for (Devin review on #2402).

    A principal that carries exactly one human is attributed to that human;
    one that carries several, or none, has no single reporter and is refused
    rather than guessed at:

    * ``AgentPrincipal`` -> its CALLER (C2.3), the person whose turn is
      running, falling back to the owner for a single-owner agent. This is
      the agent-offers-to-file case.
    * ``SessionPrincipal`` -> the sole participant of a one-person chat. A
      co-session with several participants is ambiguous and refused: a report
      would otherwise be filed against whoever happens to sort first.
    * ``DataAppViewerPrincipal`` -> refused. It is deliberately the narrowest
      principal (no internal-table carve-out, #2383), and ``agnes_issues`` is
      an internal table; widening it here would quietly undo that decision.
    """
    if isinstance(user, dict):
        uid = user.get("id")
        if not uid:
            raise _err(403, "reporter_unidentified", "This credential has no user identity to file a report under.")
        return uid, user.get("email")

    from app.auth.session_principal import AgentPrincipal, SessionPrincipal

    if isinstance(user, AgentPrincipal):
        return (user.caller_user_id or user.owner_user_id, user.caller_email or user.owner_email)
    if isinstance(user, SessionPrincipal) and len(user.participant_user_ids) == 1:
        email = user.participant_emails[0] if user.participant_emails else None
        return user.participant_user_ids[0], email
    raise _err(
        403,
        "reporter_unidentified",
        "This session has no single person to file the report as.",
        hint='File it from your own account: agnes issue report "<what is wrong>"',
    )


def _is_admin(user: Any) -> bool:
    """Restricted principals are never admin — the admin seam denies them
    outright, so asking ``is_user_admin`` about the human behind one would
    hand an agent its owner's authority."""
    if not isinstance(user, dict):
        return False
    return bool(is_user_admin(user.get("id")))


def _owned_or_admin(repo: Any, issue_ref: str, user: Any) -> dict:
    """404 (not 403) for someone else's issue: ids must not be probeable."""
    reporter_id, _ = _reporter(user)
    row = repo.get(issue_ref)
    if row is None or (row["created_by"] != reporter_id and not _is_admin(user)):
        raise _err(404, "issue_not_found", f"No issue {issue_ref!r} you can see.", hint="List yours: agnes issue list")
    return row


def _screenshot_dir(issue_id: str) -> Path:
    base = (Path(get_data_dir()) / "issues").resolve()
    target = (base / issue_id).resolve()
    if not target.is_relative_to(base):
        raise _err(400, "issue_not_found", "invalid issue id")
    return target


def _envelope(rows: list[dict], total: int, limit: int) -> dict[str, Any]:
    truncated = {"limit": limit, "total": total} if total > len(rows) else None
    return {"data": rows, "count": len(rows), "truncated": truncated}


def _clamp(limit: int) -> int:
    return max(1, min(int(limit), 500))


def _status_or_400(status: str | None) -> str | None:
    if status in (None, "", "all"):
        return None
    if status not in ISSUE_STATUSES:
        raise _err(400, "invalid_status", f"status must be one of {', '.join(ISSUE_STATUSES)} or all")
    return status


@router.post("/api/issues", status_code=201)
async def create_issue(
    body: IssueCreate,
    request: Request,
    background: BackgroundTasks,
    user: dict = Depends(get_current_user),
    repo: Any = Depends(_issues_repo),
):
    """Report a problem (any signed-in caller). Stored in the instance; a text
    summary is mirrored to ``issues.webhook_url`` in the background."""
    reporter_id, reporter_email = _reporter(user)
    title = _clean(body.title)
    if not title:
        raise _err(400, "missing_title", "title is required")
    context = {**(_cap_context(body.context) or {}), **{k: v for k, v in _server_context(request).items() if v}}
    row = repo.create(
        title=title,
        body=_clean(body.body),
        kind=body.kind,
        created_by=reporter_id,
        created_by_email=reporter_email,
        source_surface=_surface(request),
        page_url=_clean(body.page_url),
        context=context,
    )

    from src.audit_helpers import log_safe

    log_safe(
        user_id=reporter_id,
        action="issue.report",
        resource=row["id"],
        params={"kind": row["kind"], "surface": row["source_surface"]},
    )
    base_url = _public_base_url(request)

    async def _mirror() -> None:
        """Post the operator summary once, from the report's CURRENT state.

        Deliberately re-reads instead of mirroring the ``row`` captured above:
        a screenshot arrives on a SEPARATE request (``PUT .../screenshot``)
        that the client cannot even start until this 201 has been received, so
        the creation snapshot never has ``screenshot_path`` set and the
        message promised a link it never carried (Devin review on #2402).

        When the client says a screenshot is coming, wait for it — bounded, so
        a capture that fails or never uploads costs the operator a short delay
        and not the notification itself. Exactly one message is sent either
        way; the wait is what decides whether it can name the screenshot.

        ``async def`` on purpose, and the single most important thing about
        this function. Starlette runs a SYNCHRONOUS background callback in
        the same bounded anyio worker pool that serves synchronous
        dependencies — ``get_current_user`` among them — so a blocking
        ``time.sleep`` here would park a request thread for the whole wait.
        The web dialog ticks the screenshot box by default, so enough
        simultaneous reports would exhaust the pool and stall authentication
        for every other request, including the very screenshot upload each
        wait is waiting for: a self-inflicted deadlock under load (Devin
        review on #2402). Sleeping on the event loop costs no thread, and the
        blocking repository and webhook calls are offloaded individually.
        """
        import anyio

        from app.services.issue_notifier import notify_issue_filed

        loop_deadline = anyio.current_time() + _SCREENSHOT_WAIT_SEC if body.expect_screenshot else 0.0
        current = row
        while True:
            fresh = await anyio.to_thread.run_sync(lambda: repo.get(row["id"]))
            if fresh is None:  # deleted under us — nothing to mirror
                return
            current = fresh
            if current.get("screenshot_path") or anyio.current_time() >= loop_deadline:
                break
            await anyio.sleep(_SCREENSHOT_POLL_SEC)

        posted = await anyio.to_thread.run_sync(lambda: notify_issue_filed(current, public_base_url=base_url))
        if posted:
            await anyio.to_thread.run_sync(lambda: repo.mark_webhook_delivered(current["id"]))

    background.add_task(_mirror)
    return row


@router.put("/api/issues/{issue_id}/screenshot", status_code=204)
async def put_screenshot(
    issue_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
    repo: Any = Depends(_issues_repo),
):
    """Attach a PNG screenshot to a report — owner only, never an admin on
    someone else's behalf: the screenshot is what the REPORTER saw."""
    row = _owned_or_admin(repo, issue_id, user)
    if row["created_by"] != _reporter(user)[0]:
        raise _err(404, "issue_not_found", "only the reporter can attach the screenshot")
    data = await request.body()
    if len(data) > _MAX_SCREENSHOT_BYTES:
        raise _err(413, "screenshot_too_large", "screenshot must be under 3 MiB")
    if not data.startswith(_PNG_MAGIC):
        raise _err(400, "screenshot_not_png", "screenshot must be a PNG")
    target = _screenshot_dir(row["id"])
    target.mkdir(parents=True, exist_ok=True)
    # Publish atomically: write a uniquely-named neighbour, then rename over
    # the live file. `os.replace` is atomic within a filesystem, so a reader
    # holding `screenshot.png` open (the GET route below, or an operator
    # following the webhook link) sees either the whole old image or the whole
    # new one — never the half-written bytes a direct overwrite exposes when a
    # replacement upload lands mid-read (Devin review on #2402).
    final = target / "screenshot.png"
    tmp = target / f".screenshot.{uuid4().hex}.part"
    try:
        tmp.write_bytes(data)
        os.replace(tmp, final)
    except BaseException:
        tmp.unlink(missing_ok=True)  # never leave a .part behind on failure
        raise
    repo.set_screenshot(row["id"], f"issues/{row['id']}/screenshot.png")

    from src.audit_helpers import log_safe

    log_safe(user_id=_reporter(user)[0], action="issue.screenshot", resource=row["id"], params={"bytes": len(data)})
    return Response(status_code=204)


@router.get("/api/issues/{issue_id}/screenshot")
async def get_screenshot(
    issue_id: str,
    user: dict = Depends(get_current_user),
    repo: Any = Depends(_issues_repo),
):
    """Stream the attached screenshot back — the reporter, or any admin."""
    row = _owned_or_admin(repo, issue_id, user)
    if not row.get("screenshot_path"):
        raise _err(404, "issue_not_found", "no screenshot on this issue")
    path = _screenshot_dir(row["id"]) / "screenshot.png"
    if not path.is_file():
        raise _err(404, "issue_not_found", "screenshot file is missing")
    return FileResponse(
        path,
        media_type="image/png",
        headers={
            "Content-Disposition": 'inline; filename="screenshot.png"',
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "SAMEORIGIN",
            "Cache-Control": "private, no-store",
            "Content-Security-Policy": "frame-ancestors 'self'; object-src 'none'; base-uri 'none'",
        },
    )


@router.get("/api/issues/mine")
async def list_my_issues(
    status: str | None = None,
    limit: int = 50,
    user: dict = Depends(get_current_user),
    repo: Any = Depends(_issues_repo),
):
    """The caller's own reports, newest activity first."""
    st, lim = _status_or_400(status), _clamp(limit)
    reporter_id, _ = _reporter(user)
    rows = repo.list_for_user(reporter_id, status=st, limit=lim)
    return _envelope(rows, repo.count_for_user(reporter_id, status=st), lim)


@router.get("/api/issues/{issue_id}")
async def get_issue(
    issue_id: str,
    user: dict = Depends(get_current_user),
    repo: Any = Depends(_issues_repo),
):
    """One report and its comment thread — the reporter's own, or any as admin."""
    row = _owned_or_admin(repo, issue_id, user)
    return {**row, "comments": repo.list_comments(row["id"])}


@router.post("/api/issues/{issue_id}/comments", status_code=201)
async def add_comment(
    issue_id: str,
    body: CommentCreate,
    user: dict = Depends(get_current_user),
    repo: Any = Depends(_issues_repo),
):
    """Add a public comment — the reporter follows up, or an admin replies."""
    row = _owned_or_admin(repo, issue_id, user)
    text = _clean(body.body)
    if not text:
        raise _err(400, "missing_body", "comment body is required")
    author_id, author_email = _reporter(user)
    kind = "reporter" if row["created_by"] == author_id else "admin"
    comment = repo.add_comment(row["id"], author_id=author_id, author_email=author_email, author_kind=kind, body=text)

    from src.audit_helpers import log_safe

    log_safe(user_id=author_id, action="issue.comment", resource=row["id"], params={"author_kind": kind})
    return comment


@router.get("/api/admin/issues")
async def list_issue_queue(
    status: str | None = None,
    limit: int = 100,
    _admin: dict = Depends(require_admin),
    repo: Any = Depends(_issues_repo),
):
    """The full queue across every reporter (admin only)."""
    st, lim = _status_or_400(status), _clamp(limit)
    return _envelope(repo.list_all(status=st, limit=lim), repo.count_all(status=st), lim)


@router.post("/api/admin/issues/{issue_id}/resolve")
async def resolve_issue(
    issue_id: str,
    body: IssueResolve,
    admin: dict = Depends(require_admin),
    repo: Any = Depends(_issues_repo),
):
    """Close one report, on the record (admin only).

    404 when it does not exist; 409 when somebody already resolved it — the
    repository's guarded transition refuses to overwrite the first admin's
    note, so the queue never loses who actually fixed the thing.
    """
    from src.repositories.issue_reports_pg import IssueAlreadyResolved

    row = repo.get(issue_id)
    if row is None:
        raise _err(404, "issue_not_found", f"No issue {issue_id!r}.", hint="Find the id: agnes admin issue list")
    try:
        done = repo.resolve(
            row["id"], resolved_by=admin.get("email") or admin.get("id"), resolution_note=_clean(body.resolution_note)
        )
    except IssueAlreadyResolved:
        # Re-read: `row` was fetched while the report was still open, so it
        # carries no resolver and no timestamp. Formatting the conflict from
        # it told the losing admin "#42 was already resolved by None at None"
        # — the one thing this response exists to say, missing (Devin review
        # on #2402).
        winner = repo.get(row["id"]) or row
        by, at = winner.get("resolved_by"), winner.get("resolved_at")
        detail = f" by {by}" if by else ""
        when = f" at {at}" if at else ""
        raise _err(409, "already_resolved", f"#{row['number']} was already resolved{detail}{when}")

    from src.audit_helpers import log_safe

    log_safe(user_id=admin.get("id"), action="issue.resolved", resource=row["id"], params={})
    return done
