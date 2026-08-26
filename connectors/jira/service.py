"""
Jira API service for fetching issue data.

Handles communication with Jira Cloud REST API to fetch complete issue data
including all fields, comments, and attachments.

After saving issue data and attachments, triggers incremental Parquet transform
for real-time updates available via rsync.
"""

import json
import logging
import os
import re
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from connectors.jira.validation import is_valid_issue_key, safe_join_under

logger = logging.getLogger(__name__)


class JiraFetchError(Exception):
    """Raised by Jira fetch helpers when the API returns an auth (401/403)
    or server (5xx) error. Callers that overlay the result onto cached
    issue JSON (save_issue, backfill processors) MUST catch this and
    skip the overlay; otherwise a transient outage silently wipes
    existing parquet rows for that issue.
    """


# Jira's issue-embed API caps `fields.comment.comments` at this many rows.
JIRA_COMMENT_PAGE_SIZE = 100

# 429 handling for the comment-pagination loop. The batch path runs several
# workers in parallel and adds a request per >100-comment issue, so rate
# limiting is its likeliest failure — without a retry it would routinely leave
# issues marked `_comments_incomplete`. Bounded, because the alternative is a
# worker parked on an endpoint that keeps refusing.
JIRA_COMMENT_RATE_LIMIT_RETRIES = 3
JIRA_COMMENT_RETRY_AFTER_DEFAULT = 60
# A `Retry-After` may legitimately be hours; a batch worker must not sit on one.
JIRA_COMMENT_RETRY_AFTER_MAX = 300


def _retry_after_seconds(response: Any) -> int:
    """`Retry-After` in seconds — defaulted when absent/unparseable, capped.

    The header may also carry an HTTP-date; `int()` on that raises, so parse
    defensively and fall back rather than turning a rate limit into a crash.
    """
    raw = (response.headers or {}).get("Retry-After") if hasattr(response, "headers") else None
    try:
        seconds = int(str(raw).strip())
    except (TypeError, ValueError):
        seconds = JIRA_COMMENT_RETRY_AFTER_DEFAULT
    if seconds <= 0:
        seconds = JIRA_COMMENT_RETRY_AFTER_DEFAULT
    return min(seconds, JIRA_COMMENT_RETRY_AFTER_MAX)


class _JiraConfig:
    """Jira configuration from environment variables."""

    JIRA_DOMAIN = os.environ.get("JIRA_DOMAIN", "")
    JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
    JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")
    JIRA_DATA_DIR = Path(os.environ.get("JIRA_DATA_DIR", "/data/src_data/raw/jira"))
    JIRA_CLOUD_ID = os.environ.get("JIRA_CLOUD_ID", "")
    JIRA_WEBHOOK_SECRET = os.environ.get("JIRA_WEBHOOK_SECRET", "")
    DEBUG = os.environ.get("DEBUG", "").lower() in ("1", "true")


Config = _JiraConfig


def reload_config_from_env() -> None:
    """Re-read ``Config`` from ``os.environ`` and drop the service singleton.

    ``_JiraConfig`` evaluates every value in its **class body**, so the credentials
    freeze when this module is imported. A CLI that calls ``load_dotenv()`` in
    ``main()`` therefore populates ``os.environ`` far too late: the module was
    already imported at the top of the file, ``Config.JIRA_DOMAIN`` is still empty,
    and ``JiraService`` copies those empty values — so the command reports "Jira is
    not configured" and exits 0 having done nothing (Devin Review on #1274).

    Scripts that load a ``.env`` at runtime must call this immediately afterwards.
    Deliberately not solved by making ``_JiraConfig`` lazy: its attributes are
    monkeypatched directly across the test suite, and properties would break every
    one of those call sites for no gain on the deployed paths, where the values are
    exported into the environment before the process starts.
    """
    global _jira_service

    Config.JIRA_DOMAIN = os.environ.get("JIRA_DOMAIN", "")
    Config.JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
    Config.JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")
    Config.JIRA_CLOUD_ID = os.environ.get("JIRA_CLOUD_ID", "")
    Config.JIRA_WEBHOOK_SECRET = os.environ.get("JIRA_WEBHOOK_SECRET", "")
    Config.JIRA_DATA_DIR = Path(os.environ.get("JIRA_DATA_DIR", "/data/src_data/raw/jira"))

    # The singleton captured the stale values in __init__; rebuild it on next use.
    _jira_service = None


_VALID_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _parse_field_spec(raw: str) -> list[tuple[str, str]]:
    """Parse a comma-separated ``id`` / ``id:column`` spec into ``[(id, alias), ...]``.

    Shared by ``refresh_fields`` (issue custom fields) and
    ``organization_detail_fields`` (JSM organization details) so the two specs can
    never drift in how they tokenize. Returns the *raw* alias — an empty string when
    the entry named no column — and leaves validation to the caller, because the two
    consumers fall back differently (issue fields to the field id, organization
    details to a prefixed name, since a detail id like ``38`` is not a legal column).
    """
    out: list[tuple[str, str]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        field_id, _, alias = entry.partition(":")
        field_id = field_id.strip()
        alias = alias.strip()
        if not field_id:
            continue
        out.append((field_id, alias))
    return out


def refresh_fields() -> list[tuple[str, str]]:
    """``[(field_id, column_name), ...]`` parsed from ``JIRA_REFRESH_FIELDS``.

    Format: comma-separated ``field_id`` or ``field_id:column_name``. There are no
    defaults — field ids are assigned per Jira instance, so a hard-coded value would
    be wrong for any other deployment. A ``column_name`` that is not a valid
    SQL/parquet identifier falls back to the field id; entries without an id are
    skipped. Lazy (read at call time, not import) so CLI scripts that load ``.env``
    via ``load_dotenv()`` at runtime see the value. Discover field ids with
    ``verify_sla_access --list-fields``.
    """
    out: list[tuple[str, str]] = []
    for field_id, alias in _parse_field_spec(os.environ.get("JIRA_REFRESH_FIELDS", "")):
        column = alias if alias and _VALID_COLUMN.match(alias) else field_id
        out.append((field_id, column))
    return out


ORGANIZATION_DETAIL_PREFIX = "detail_"


def _organization_json(response: httpx.Response, what: str) -> dict:
    """Decode a 200 body into the JSON object it must be, or ``JiraFetchError``.

    ``response.json()`` raises a plain ``ValueError`` on a body that is not JSON —
    an HTML error page from an intermediary answering 200, say. That escapes the
    per-organization ``except JiraFetchError`` in the refresh sweep, so one bad
    response would abort the whole run before anything was written instead of
    preserving that organization's previous row.

    A body that decodes to something other than an object is rejected the same
    way: every caller reads keys off the payload, so ``null`` would surface as
    ``fetch_organization``'s 404 ``None`` — read by the sweep as "deleted, drop
    the row" — and a list or string would raise ``AttributeError`` outside the
    same boundary (Devin Review on #1274).
    """
    try:
        payload = response.json()
    except ValueError as e:
        raise JiraFetchError(f"{what} failed: malformed JSON in a {response.status_code} response — {e}") from e
    if not isinstance(payload, dict):
        raise JiraFetchError(
            f"{what} failed: a {response.status_code} response decoded to {type(payload).__name__}, not a JSON object"
        )
    return payload


def _organization_reserved_columns() -> frozenset[str]:
    """Built-in `organizations` columns a configured detail must never overwrite.

    Derived from ``ORGANIZATIONS_SCHEMA`` rather than restated, so a column added
    there is protected without a second edit here. The import is function-local
    because ``transform`` imports this module at top level — the same reason
    ``trigger_incremental_transform`` defers its ``incremental_transform`` import.
    """
    from connectors.jira.transform import ORGANIZATIONS_SCHEMA

    return frozenset(ORGANIZATIONS_SCHEMA)


def organization_detail_fields() -> list[tuple[str, str]]:
    """``[(detail_key, column_name), ...]`` parsed from ``JIRA_ORG_DETAIL_FIELDS``.

    Format mirrors ``JIRA_REFRESH_FIELDS``: comma-separated ``detail_key`` or
    ``detail_key:column_name``. ``detail_key`` is matched against a JSM organization
    detail's ``id`` first and its ``name`` second (see
    ``transform.extract_organization_details``), so an operator can configure either
    the stable numeric id or the human label.

    No defaults: detail ids are per-instance, so any hard-coded value would be wrong
    on another deployment. Unlike issue fields, the key cannot double as the column —
    a detail id such as ``38`` is not a legal SQL/parquet identifier — so an entry
    with no usable alias becomes ``detail_<key>``. An alias colliding with a built-in
    column is prefixed the same way rather than silently shadowing it, and a column
    already claimed by an earlier entry is skipped. Read at call time so scripts that
    ``load_dotenv()`` at runtime see the value.
    """
    reserved = _organization_reserved_columns()
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for detail_key, alias in _parse_field_spec(os.environ.get("JIRA_ORG_DETAIL_FIELDS", "")):
        if alias and _VALID_COLUMN.match(alias):
            column = alias
            if column in reserved:
                column = f"{ORGANIZATION_DETAIL_PREFIX}{column}"
                logger.warning(
                    "Jira org detail %s: column %r collides with a built-in organizations column; using %r instead",
                    detail_key,
                    alias,
                    column,
                )
        else:
            # Either no alias, or one that is not a legal identifier. The key itself
            # is usually a bare number, so prefix it into something addressable.
            column = f"{ORGANIZATION_DETAIL_PREFIX}{detail_key}"
            if not _VALID_COLUMN.match(column):
                logger.warning(
                    "Jira org detail %s: cannot derive a valid column name; skipping",
                    detail_key,
                )
                continue
        if column in seen:
            logger.warning(
                "Jira org detail %s: column %r already used by another entry; skipping",
                detail_key,
                column,
            )
            continue
        seen.add(column)
        out.append((detail_key, column))
    return out


def trigger_incremental_transform(issue_key: str, deleted: bool = False, warn_unresolved: bool = True) -> bool:
    """
    Trigger incremental Parquet transform for a single issue.

    This updates only the affected monthly Parquet file, making the change
    immediately available for rsync to analysts.

    Args:
        issue_key: Jira issue key (e.g., "SUPPORT-1234")
        deleted: If True, remove issue from Parquet files
        warn_unresolved: Forwarded to the comments transform. ``False`` silences
            the missing-``jsdPublic`` WARNING for a RE-transform of an issue this
            same request already transformed — see ``save_issue``'s
            post-attachment-download call, the only place that passes it.

    Returns:
        True if transform succeeded, False otherwise
    """
    try:
        from connectors.jira.incremental_transform import transform_single_issue

        success = transform_single_issue(
            issue_key=issue_key,
            # The transform has to read the JSON out of the directory `save_issue`
            # just wrote it to. Left unset, `transform_single_issue` falls back to
            # `$DATA_DIR/extracts/<source>/raw` — derived from DATA_DIR, so it
            # ignores JIRA_DATA_DIR entirely. On any deployment where the two do not
            # coincide (including the default one, where JIRA_DATA_DIR is unset and
            # resolves to the legacy raw path) every webhook transform died on
            # "Issue JSON not found" while the endpoint still answered Jira 200 —
            # so edits to issues already in the parquet silently stopped landing,
            # and only *new* issues appeared, backfilled later by the consistency
            # check.
            raw_dir=Config.JIRA_DATA_DIR,
            # `output_dir` is deliberately NOT passed. Its default is the
            # extract.duckdb-contract location the orchestrator actually serves,
            # which is right on every deployment. `JIRA_PARQUET_DIR` — the obvious
            # candidate to forward — means the LEGACY Data Broker root across this
            # connector (`jira.env.example.txt`, `scripts/consistency_check.py`,
            # `scripts/poll_sla.py`), which nothing serves; forwarding it would send
            # webhook writes off the served layout on exactly the installs that set
            # it, reintroducing this bug from the other side. It would also break
            # `update_meta`, which derives the extract dir as `output_dir.parent`
            # and assumes the `<source>/data` shape.
            deleted=deleted,
            warn_unresolved=warn_unresolved,
        )

        if success:
            logger.info(f"Incremental transform completed for {issue_key}")
            # Rebuild Jira views in master analytics.duckdb — enqueued
            # (wave-2B job queue) rather than run inline. The webhook
            # response must stay fast; the orchestrator rebuild is a full
            # re-ATTACH + view rebuild over analytics.duckdb, cheap per
            # call but wasteful to run once per webhook event during a
            # burst. `idempotency_key="jira-refresh"` collapses any
            # number of pending webhook events into a single queued
            # rebuild — see `app.worker.kinds._run_jira_refresh`.
            try:
                from app.job_correlation import stamp_request_id
                from src.repositories import jobs_repo

                result = jobs_repo().enqueue("jira-refresh", stamp_request_id({}), idempotency_key="jira-refresh")
                # Invariant: every parquet write (above, via transform_single_issue)
                # must be followed by a rebuild that starts AFTER it. Dedup above
                # matches against status IN ('queued', 'running') — if it collapsed
                # onto a job that is already RUNNING, that job may have started (and
                # read parquet) BEFORE this write landed, so this write would
                # otherwise sit stale until some future webhook happens to fire.
                # Enqueue a coalescing follow-up (distinct idempotency key, so it
                # doesn't dedup against the primary) to guarantee a rebuild strictly
                # after this write. Repeated webhooks mid-run all dedup onto this
                # same follow-up row, bounding the pile-up at 1 running + 1 queued.
                if result.get("status") == "running":
                    jobs_repo().enqueue("jira-refresh", stamp_request_id({}), idempotency_key="jira-refresh-followup")
            except Exception as enqueue_err:
                logger.warning(f"Failed to enqueue jira-refresh job: {enqueue_err}")
        else:
            logger.warning(f"Incremental transform failed for {issue_key}")

        return success

    except ImportError as e:
        logger.warning(f"Incremental transform not available: {e}")
        return False
    except Exception as e:
        logger.error(f"Error in incremental transform for {issue_key}: {e}")
        return False


#: Staging files older than this are definitively dead: the download client
#: itself times out at 60s, so an hour-old ``.tmp-*`` can only be the leftover
#: of a killed process.
STALE_STAGING_MAX_AGE_S = 3600


def sweep_stale_attachment_staging(directory: Path, max_age_s: int = STALE_STAGING_MAX_AGE_S) -> None:
    """Remove dead ``.tmp-*`` staging files from an attachments directory.

    The atomic publishers (webhook + backfill) stage each download under a
    ``.tmp-<pid>-<rand>-<name>`` file and unlink it in their exception
    handler — but a handler cannot run when the process is SIGKILLed
    mid-write (the documented gunicorn worker-timeout case). The random
    component means a retry never reuses the orphan, the transform never
    probes hidden names, and nothing else sweeps them — so each interrupted
    download would leak up to ``MAX_ATTACHMENT_SIZE`` forever (Devin on
    #1297). Called by both publishers before staging; age-gated so a
    CONCURRENT writer's live staging file is never yanked out from under it.
    Best-effort: a sweep failure never blocks the download.
    """
    cutoff = time.time() - max_age_s
    try:
        entries = list(directory.glob(".tmp-*"))
    except OSError:
        return
    for stale in entries:
        try:
            if stale.stat().st_mtime < cutoff:
                stale.unlink(missing_ok=True)
                logger.info(f"Removed stale attachment staging file {stale.name}")
        except OSError:
            continue


#: Webhook events meaning THE ISSUE ITSELF was deleted — the only ones for which
#: every stored row should be removed. Matched by name rather than by a
#: ``"deleted" in webhookEvent`` substring, which also caught `comment_deleted`,
#: `attachment_deleted` and `worklog_deleted` and tombstoned the WHOLE issue on
#: what is really an ordinary content change (Devin on #1435).
#:
#: An allowlist of spellings rather than one equality, because the two failure
#: directions are not symmetric. Tombstoning too eagerly costs the issue's rows
#: until its next webhook event rebuilds them — `save_issue` replaces the stored
#: JSON wholesale, so the `_deleted_at` marker does not survive a later event.
#: MISSING a real issue deletion is permanent: deletion webhooks fire once, the
#: consistency check skips nothing to re-delete, and the ghost row stays forever.
#: So when in doubt this errs toward tombstoning.
_ISSUE_DELETED_EVENTS = frozenset({"jira:issue_deleted", "issue_deleted"})


def _incomplete_marker_path(json_path: Path) -> Path:
    """Sidecar marker path for an issue JSON's ``_comments_incomplete`` state.

    A dedicated file next to the JSON — rather than a key inside it — so
    ``--skip-existing`` can answer "does this issue need a re-fetch?" with a
    single ``stat()``, the same cost as the pre-pagination ``json_path.exists()``
    check it replaces. ``_comments_incomplete`` is added to the dict well
    after ``fields`` (often the largest part of a ``fields=*all`` payload),
    so it sits late in the file — a bounded read from the front would
    routinely miss it, and parsing the whole file to find it is exactly the
    six-figure-issue-count cost this sidecar avoids (Devin Review on #1283).
    """
    return json_path.with_suffix(json_path.suffix + ".incomplete")


def _sync_incomplete_marker(json_path: Path, issue_data: dict[str, Any]) -> None:
    """Create/remove the sidecar marker to match ``issue_data``'s current
    ``_comments_incomplete`` state. Called right after writing *json_path*
    by BOTH save paths — ``JiraBackfill.save_issue`` (batch) and
    ``JiraService.save_issue`` (webhook) — so ``_needs_refetch`` sees a
    marked issue no matter which path wrote it last.
    """
    marker_path = _incomplete_marker_path(json_path)
    if issue_data.get("_comments_incomplete") is True:
        marker_path.touch(exist_ok=True)
    else:
        marker_path.unlink(missing_ok=True)


def _dedupe_comments_by_id(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """De-duplicate ``comments`` by ``id``, first occurrence wins, order preserved.

    The embed (``GET /issue/{key}``) carries the NEWEST comments while the
    paginated ``GET .../issue/{key}/comment`` walk starts at the thread head,
    so the two overlap by design — up to a full embed's worth of ids arrives
    from both requests, and ordering drift or a comment added/removed between
    them can add further repeats. Concatenating without dedup would store
    those twice. The caller concatenates PAGES FIRST and the embed second, so
    the merged list stays oldest-first (the order the stored thread has
    always had) and on a duplicate id the paged copy — fetched after the
    embed — wins. A page skipping an id instead of repeating one is a
    different, already-covered risk (the stored-vs-total shortfall WARNING
    below), not something dedup can fix.
    """
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for comment in comments:
        comment_id = comment.get("id")
        if comment_id is not None:
            if comment_id in seen:
                continue
            seen.add(comment_id)
        deduped.append(comment)
    return deduped


def _embedded_comments_are_complete(issue_data: dict[str, Any]) -> bool:
    """Does this payload demonstrably carry the issue's whole comment thread?

    Structural only: a ``fields.comment`` object is present AND the embedded list
    is at least as long as the ``total`` it reports. A payload with no comment
    field at all is NOT evidence that the issue has no comments — treating it as
    such would let an issue-scoped delete-then-insert erase a stored thread — so
    it answers False as well.

    v0.83.70 also required a boolean ``jsdPublic`` on every embedded comment, so
    that this predicate's one caller (the webhook fetch-failure fallback) could
    not replace an observed ``public_visibility`` with NULL. That requirement is
    **superseded**, not refuted: value-protection now lives at the write layer,
    where ``incremental_transform._carry_forward_public_visibility`` carries a
    stored same-version value forward on EVERY incremental write path instead of
    this one path declining to act. Webhook-body ``jsdPublic`` serialization is
    still unsampled — the carry-forward is what makes the answer stop mattering.
    Keeping the flag check here would only buy a deferred update, and cost one on
    every flagless embed.
    """
    fields = issue_data.get("fields") or {}
    comment_field = fields.get("comment")
    if not isinstance(comment_field, dict):
        return False
    comments = comment_field.get("comments")
    if not isinstance(comments, list):
        return False
    total = comment_field.get("total")
    if not isinstance(total, int):
        # No count to check against: a list of unknown completeness.
        return False
    return len(comments) >= total


def complete_issue_comments(
    issue_data: dict[str, Any],
    base_url: str,
    auth: tuple[str, str],
    client: httpx.Client,
    max_retries: int | None = None,
) -> None:
    """Fill in comments Jira's issue payload truncated at its embed page size.

    ``GET /issue/{key}`` embeds ``fields.comment.comments`` capped at 100, and
    the window is the NEWEST 100 — the payload's own ``fields.comment.startAt``
    is ``total - 100``. An issue over the cap therefore arrives missing its
    OLDEST comments, and because every later full-refetch (``fields=*all``)
    re-hits the same cap, the gap never heals on its own.

    ``fields.comment.total`` carries the true count. When it exceeds what's
    embedded, page through ``GET /issue/{key}/comment`` (``startAt``,
    ``maxResults=100``) from the START of the thread (``startAt=0``) until the
    id-deduplicated union of pages + embed reaches ``total``, and replace the
    embedded list with that union — pages first, then the embed's unseen
    tail, so the stored thread stays oldest-first and a comment edited
    between the two requests keeps its fresher paged copy — mutated in place
    on ``issue_data``, using the same ``client``/``auth`` as the issue fetch
    that produced it. Stopping on the union size rather than a raw count of
    fetched items makes no assumption about WHERE the embed window sits: with
    the real newest-anchored embed the walk issues exactly as many page
    requests as one that starts inside the window, merely re-downloading up
    to one embed's worth of already-embedded comments that dedup then drops.
    Against an endpoint that honours ``startAt`` over a stable list it cannot
    stop early with an unfetched gap; a comment DELETED mid-walk shifts
    offsets and can slip one live comment past any offset-paginated walk
    (surfaced by the shortfall WARNING below and marked incomplete, so the
    stored rows are preserved and the sidecar schedules a heal), and an
    endpoint that keeps serving pages without ever growing the union is cut
    off once ``startAt`` passes ``total`` — marked incomplete — rather than
    looping forever.

    This is the single fetch-layer seam shared by both ingestion paths that
    call it right after their issue GET: ``JiraService.fetch_issue`` (webhook
    full-refetch) and ``JiraBackfill.fetch_issue`` (batch/full extract).
    ``transform_issue``/``transform_comments`` stay pure — they just read
    whatever ``fields.comment.comments`` this function already completed.

    Comments can legitimately be added between the two requests, so a
    residual shortfall after completion is only logged (WARNING), never
    raised — this is a best-effort enrichment step, not a hard requirement.

    A ``429`` is retried up to ``max_retries`` times (``JIRA_COMMENT_RATE_LIMIT_RETRIES``
    when ``max_retries`` is ``None``), honouring ``Retry-After`` (defaulted
    when absent or an HTTP-date, capped at ``JIRA_COMMENT_RETRY_AFTER_MAX`` so
    one worker cannot park on an hours-long value) — the same treatment the
    issue and remote-link fetchers give it. Rate limiting is the batch path's
    likeliest failure, and without the retry it would routinely mark issues
    incomplete. The caller-supplied override exists because the webhook path
    runs on an active request: ``JiraService.fetch_issue`` passes a
    much smaller (zero) budget there, so a 429 marks the issue incomplete
    immediately instead of sleeping for up to
    ``JIRA_COMMENT_RATE_LIMIT_RETRIES * JIRA_COMMENT_RETRY_AFTER_MAX`` seconds
    — the marker (persisted as the ``.incomplete`` sidecar by both save
    paths, see ``_sync_incomplete_marker``) plus a later backfill heal
    (``_needs_refetch``) recovers it (Devin Review on #1283). The batch path
    (``JiraBackfill.fetch_issue``) omits the override and keeps the generous
    default.

    If a page request itself fails (RequestError, a non-200 status other than
    a retryable 429, or a 429 that outlives its retries — legitimate
    outages, not "no more comments"), the loop stops and
    ``issue_data["_comments_incomplete"]`` is set to ``True``. This is a
    sibling of the ``_remote_links`` overlay-absent contract
    (``transform_remote_links``): the incremental transform performs an
    issue-scoped delete-then-insert on the comments parquet, so overlaying a
    known-truncated list here would let a transient fetch error wipe a
    previously-complete stored thread down to whatever this attempt managed
    to fetch. ``transform_comments`` reads this marker and returns ``None``
    instead of a truncated list so the incremental caller preserves the
    existing rows (the batch/full rebuild, which preserves nothing, opts out
    with ``preserve_on_incomplete=False`` and keeps the partial list).
    An empty page (200, ``comments: []``) is not a page FAILURE — it just
    ends the walk (nothing exists at offsets >= ``startAt``). Completeness is
    judged afterwards by the stored-vs-total check: any shortfall marks the
    issue incomplete, because an empty page cannot prove no live comment was
    skipped below ``startAt`` after a mid-walk deletion shifted offsets. When
    ``total`` was merely stale (comments deleted between the two requests),
    that marking costs one extra cycle: the sidecar-scheduled refetch sees a
    consistent snapshot, completes cleanly, and the deletion propagates then.
    """
    issue_key = issue_data.get("key")
    fields = issue_data.get("fields") or {}
    comment_field = fields.get("comment") or {}
    embedded = comment_field.get("comments") or []
    total = comment_field.get("total", len(embedded))
    retry_budget = JIRA_COMMENT_RATE_LIMIT_RETRIES if max_retries is None else max_retries

    incomplete = False
    if issue_key and total > len(embedded):
        extra: list[dict[str, Any]] = []
        start_at = 0
        rate_limit_retries = 0
        while len(_dedupe_comments_by_id(extra + embedded)) < total:
            if start_at >= total:
                # A conforming endpoint ends in an empty page (break below)
                # or completes the union before startAt passes total (a page
                # of live comments beyond the captured total only exists when
                # additions already pushed the union over it). Reaching here
                # means pages keep arriving without growing the union — a
                # server/proxy ignoring startAt — and without this cut-off
                # the union-based condition would loop forever.
                logger.warning(
                    "Jira issue %s: comment pagination reached startAt=%d >= comment.total=%d "
                    "without completing — non-conforming pagination, giving up",
                    issue_key,
                    start_at,
                    total,
                )
                incomplete = True
                break
            try:
                response = client.get(
                    f"{base_url}/issue/{issue_key}/comment",
                    auth=auth,
                    params={"startAt": start_at, "maxResults": JIRA_COMMENT_PAGE_SIZE},
                    headers={"Accept": "application/json"},
                )
            except httpx.RequestError as e:
                logger.warning(
                    "Jira issue %s: comment pagination request failed at startAt=%d: %s",
                    issue_key,
                    start_at,
                    e,
                )
                incomplete = True
                break
            if response.status_code == 429:
                if rate_limit_retries >= retry_budget:
                    logger.warning(
                        "Jira issue %s: comment pagination still rate limited at startAt=%d "
                        "after %d retries — giving up",
                        issue_key,
                        start_at,
                        rate_limit_retries,
                    )
                    incomplete = True
                    break
                wait = _retry_after_seconds(response)
                rate_limit_retries += 1
                logger.warning(
                    "Jira issue %s: comment pagination rate limited at startAt=%d, waiting %ds (retry %d/%d)",
                    issue_key,
                    start_at,
                    wait,
                    rate_limit_retries,
                    retry_budget,
                )
                time.sleep(wait)
                continue
            if response.status_code != 200:
                logger.warning(
                    "Jira issue %s: comment pagination page failed at startAt=%d (status %s)",
                    issue_key,
                    start_at,
                    response.status_code,
                )
                incomplete = True
                break
            page = response.json().get("comments") or []
            if not page:
                break
            extra.extend(page)
            start_at += len(page)

        if extra:
            comment_field["comments"] = _dedupe_comments_by_id(extra + embedded)
            fields["comment"] = comment_field
            issue_data["fields"] = fields

    stored = len(comment_field.get("comments", embedded))
    if stored < total:
        if incomplete:
            cause = "pagination gave up early — see the preceding warning for the cause"
        elif not issue_key:
            cause = "pagination was never attempted: issue payload carries no key"
        else:
            cause = "comments were likely deleted mid-fetch, leaving total stale"
        logger.warning(
            "Jira issue %s: stored %d comments but Jira reports comment.total=%d after pagination completion (%s)",
            issue_key,
            stored,
            total,
            cause,
        )
        # Any shortfall marks the issue incomplete, not just a page failure:
        # a stored set shorter than the snapshot's total cannot be proven
        # complete — a comment deleted mid-walk shifts offsets and can hide a
        # LIVE comment from every page (the empty-page break proves only that
        # nothing exists at offsets >= startAt, not that nothing was skipped
        # below it). The marker makes the incremental transform preserve the
        # stored rows and the .incomplete sidecar schedules a refetch, whose
        # fresh snapshot has a consistent total: a real deletion then
        # propagates on that next clean refetch, one cycle late, instead of a
        # skipped live comment silently vanishing from the parquet now
        # (Devin Review on #1396).
        incomplete = True

    if incomplete:
        issue_data["_comments_incomplete"] = True


class JiraService:
    """Service for interacting with Jira Cloud REST API."""

    # Max attachment size to download (50 MB)
    MAX_ATTACHMENT_SIZE = 50 * 1024 * 1024

    def __init__(self) -> None:
        """Initialize Jira service with configuration."""
        self.domain = Config.JIRA_DOMAIN
        self.email = Config.JIRA_EMAIL
        self.api_token = Config.JIRA_API_TOKEN
        self.data_dir = Config.JIRA_DATA_DIR
        self.attachments_dir = self.data_dir / "attachments"
        # Memoized tenant_info lookup — see resolve_cloud_id(). Per-instance, so a
        # long-lived service reuses it while tests get a fresh one each time.
        self._cloud_id: str | None = None

        if not all([self.domain, self.email, self.api_token]):
            logger.warning("Jira credentials not fully configured")

    @property
    def base_url(self) -> str:
        """Get Jira API base URL."""
        return f"https://{self.domain}/rest/api/3"

    @property
    def auth(self) -> tuple[str, str]:
        """Get HTTP Basic auth tuple."""
        return (self.email, self.api_token)

    def _attachment_url_allowed(self, url: str) -> bool:
        """Is ``url`` a legitimate Jira attachment host? (SSRF guard, audit L3)

        ``attachment.content`` arrives in the **webhook payload**, i.e. it is
        caller-supplied: an HMAC-valid webhook for a nonexistent issue reaches
        the fetch-failure fallback and would otherwise make the server GET any
        URL the caller names (blind SSRF — the body lands under ``/data``).

        Guard with an explicit **host allowlist** rather than a private-IP
        denylist: legitimate attachments only ever live on the configured Jira
        host (or Atlassian's cloud API), and a denylist would wrongly break a
        self-hosted Jira that legitimately sits on a private address.

        Parses with ``urlsplit`` and compares ``hostname`` so userinfo tricks
        (``https://jira.example.com@evil.com/``) resolve to the real host.
        """
        try:
            parts = urlsplit(url)
        except ValueError:
            return False
        # base_url is https-only, so legitimate content URLs are too.
        if parts.scheme != "https":
            return False
        host = (parts.hostname or "").lower()
        if not host:
            return False
        allowed = {"api.atlassian.com"}
        if self.domain:
            allowed.add(self.domain.lower())
        return host in allowed

    def is_configured(self) -> bool:
        """Check if Jira service is properly configured."""
        return all([self.domain, self.email, self.api_token])

    def fetch_issue(self, issue_key: str, comment_max_retries: int | None = None) -> dict[str, Any] | None:
        """
        Fetch complete issue data from Jira.

        Args:
            issue_key: Issue key (e.g., "PROJ-123")
            comment_max_retries: Forwarded to ``complete_issue_comments`` as
                ``max_retries`` — ``None`` keeps its own (generous) default.
                ``process_webhook_event`` passes ``0`` here: its caller is an
                active request, so a 429 must mark the issue
                ``_comments_incomplete`` immediately rather than sleep.

        Returns:
            Issue data dict or None if fetch failed
        """
        if not self.is_configured():
            logger.error("Jira service not configured, cannot fetch issue")
            return None

        url = f"{self.base_url}/issue/{issue_key}"
        params = {
            "expand": "renderedFields,changelog",
            "fields": "*all",
        }

        try:
            with httpx.Client(timeout=30) as client:
                response = client.get(
                    url,
                    auth=self.auth,
                    params=params,
                    headers={"Accept": "application/json"},
                )

                if response.status_code == 200:
                    issue_data = response.json()
                    complete_issue_comments(
                        issue_data, self.base_url, self.auth, client, max_retries=comment_max_retries
                    )
                    return issue_data
                elif response.status_code == 404:
                    logger.warning(f"Issue {issue_key} not found")
                    return None
                else:
                    logger.error(f"Failed to fetch issue {issue_key}: {response.status_code} - {response.text[:200]}")
                    return None

        except httpx.RequestError as e:
            logger.error(f"Request error fetching issue {issue_key}: {e}")
            return None

    def fetch_refresh_fields(self, issue_key: str) -> dict[str, Any] | None:
        """
        Fetch the configured refresh fields for an issue using the primary token.

        The field ids come from ``refresh_fields()`` (``JIRA_REFRESH_FIELDS``, no
        defaults); when none are configured this returns ``None`` (nothing to
        fetch). These are ordinary issue custom fields, readable via the regular
        issue REST API with the same primary credentials as ``fetch_issue`` (the
        account needs whatever read permission the field requires — e.g. a JSM
        Agent licence for SLA fields). The base URL is the site domain by default;
        when ``JIRA_CLOUD_ID`` is set (required for a *scoped* API token) the
        ``api.atlassian.com`` gateway is used instead, on the same primary auth.

        Args:
            issue_key: Issue key (e.g., "SUPPORT-123")

        Returns:
            Dict with the fetched field values, or None if not configured/failed
        """
        field_ids = [fid for fid, _ in refresh_fields()]
        if not field_ids:
            logger.debug("No refresh fields configured, skipping fetch")
            return None

        if not self.is_configured():
            logger.error("Jira service not configured, cannot fetch refresh fields")
            return None

        cloud_id = Config.JIRA_CLOUD_ID
        if cloud_id:
            base_url = f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3"
        else:
            base_url = self.base_url
        url = f"{base_url}/issue/{issue_key}"
        params = {"fields": ",".join(field_ids)}

        try:
            with httpx.Client(timeout=30) as client:
                response = client.get(
                    url,
                    auth=self.auth,
                    params=params,
                    headers={"Accept": "application/json"},
                )

            if response.status_code == 200:
                return response.json().get("fields", {})
            else:
                logger.warning(f"Failed to fetch refresh fields for {issue_key}: {response.status_code}")
                return None

        except httpx.RequestError as e:
            logger.warning(f"Refresh-fields fetch error for {issue_key}: {e}")
            return None

    def fetch_remote_links(self, issue_key: str) -> list[dict]:
        """
        Fetch remote links for an issue from Jira.

        Returns the list of remote links on 200; an empty list on 404
        (issue legitimately has no remote links). Raises JiraFetchError
        on ANY other status code or on httpx.RequestError, so callers
        that overlay this onto cached issue data can skip the overlay
        instead of wiping existing rows. Critically, 429 rate limits
        also raise — silently returning [] there would re-trigger the
        same wipe bug (a webhook burst hitting Jira's rate limiter is
        the most likely production scenario).
        """
        # Unconfigured-service case: per the new contract, callers
        # interpret `[]` as "issue legitimately has no remote links"
        # and `JiraFetchError` as "fetch failed, preserve existing
        # rows". Silently returning `[]` here would overlay an empty
        # list onto cached issue JSON and wipe existing parquet rows
        # the next time a webhook fires while creds happen to be
        # missing — the exact regression this PR closes for the 401
        # / 429 / 5xx paths. Raise instead so the overlay site skips.
        if not self.is_configured():
            raise JiraFetchError(
                f"Remote-links fetch for {issue_key} failed: Jira service not configured (missing API credentials)"
            )

        url = f"{self.base_url}/issue/{issue_key}/remotelink"

        try:
            with httpx.Client(timeout=30) as client:
                response = client.get(
                    url,
                    auth=self.auth,
                    headers={"Accept": "application/json"},
                )
        except httpx.RequestError as e:
            raise JiraFetchError(f"Remote-links fetch for {issue_key} failed: connection — {e}") from e

        if response.status_code == 200:
            return response.json()
        if response.status_code == 404:
            return []
        if response.status_code in (401, 403):
            raise JiraFetchError(
                f"Remote-links fetch for {issue_key} failed: auth error "
                f"({response.status_code}) — token may be expired/revoked"
            )
        if response.status_code == 429:
            raise JiraFetchError(f"Remote-links fetch for {issue_key} failed: rate limited (429) — retry later")
        if response.status_code >= 500:
            raise JiraFetchError(f"Remote-links fetch for {issue_key} failed: server error ({response.status_code})")
        raise JiraFetchError(f"Remote-links fetch for {issue_key} failed: unexpected status {response.status_code}")

    def resolve_cloud_id(self) -> str:
        """The site's cloud id, needed to address the Customer Service Management API.

        ``JIRA_CLOUD_ID`` wins when set (it is already the documented switch for a
        *scoped* API token). Otherwise it is read from the site's public
        ``/_edge/tenant_info`` document, which needs no authentication. Unlike the
        issue REST API, the CSM API is only reachable through the
        ``api.atlassian.com`` gateway and therefore *always* needs a cloud id — so
        this cannot simply fall back to the site domain the way
        ``fetch_refresh_fields`` does.

        Memoized per service instance: a site's cloud id is immutable, and the
        organization refresh would otherwise re-request it for every organization.

        Raises:
            JiraFetchError: if the id is unset and tenant_info cannot be read.
        """
        configured = Config.JIRA_CLOUD_ID
        if configured:
            return configured
        if self._cloud_id:
            return self._cloud_id
        if not self.domain:
            raise JiraFetchError("Cannot resolve cloud id: JIRA_DOMAIN is not set and JIRA_CLOUD_ID is empty")

        url = f"https://{self.domain}/_edge/tenant_info"
        try:
            with httpx.Client(timeout=30) as client:
                response = client.get(url, headers={"Accept": "application/json"})
        except httpx.RequestError as e:
            raise JiraFetchError(f"tenant_info lookup for {self.domain} failed: connection — {e}") from e

        if response.status_code != 200:
            raise JiraFetchError(
                f"tenant_info lookup for {self.domain} failed: status {response.status_code}. "
                "Set JIRA_CLOUD_ID explicitly to skip this lookup."
            )
        cloud_id = _organization_json(response, f"tenant_info lookup for {self.domain}").get("cloudId")
        if not cloud_id:
            raise JiraFetchError(f"tenant_info for {self.domain} returned no cloudId")
        self._cloud_id = cloud_id
        return cloud_id

    @property
    def _servicedesk_url(self) -> str:
        """Base URL for the Service Desk API (a sibling of ``/rest/api/3``).

        Switches to the ``api.atlassian.com`` gateway when ``JIRA_CLOUD_ID`` is set,
        mirroring ``fetch_refresh_fields``: a *scoped* API token cannot authenticate
        against the site domain at all. Without this, an instance on a scoped token
        could read an organization through the CSM API (which is gateway-only) but
        not enumerate them here, so the whole refresh aborted on the first call
        (Devin Review on #1274).
        """
        cloud_id = Config.JIRA_CLOUD_ID
        if cloud_id:
            return f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/servicedeskapi"
        return f"https://{self.domain}/rest/servicedeskapi"

    def fetch_organization_ids(self) -> list[str]:
        """Every JSM organization id on the site, following pagination.

        Uses the Service Desk API rather than the CSM API deliberately: CSM exposes
        no list/search operation for organizations (``GET /organization`` answers 405
        — ``POST`` there *creates* one), whereas this endpoint pages through them.
        Only ids are needed; the details come from ``fetch_organization``.

        Raises:
            JiraFetchError: on any non-200, so a partial enumeration can never be
                mistaken for "the site has fewer organizations now" and quietly
                delete rows from the organizations table.
        """
        if not self.is_configured():
            raise JiraFetchError("Organization enumeration failed: Jira service not configured")

        ids: list[str] = []
        seen: set[str] = set()
        start = 0
        limit = 50
        url = f"{self._servicedesk_url}/organization"

        with httpx.Client(timeout=30) as client:
            while True:
                try:
                    response = client.get(
                        url,
                        auth=self.auth,
                        params={"start": start, "limit": limit},
                        headers={"Accept": "application/json"},
                    )
                except httpx.RequestError as e:
                    raise JiraFetchError(f"Organization enumeration failed: connection — {e}") from e

                if response.status_code != 200:
                    raise JiraFetchError(
                        f"Organization enumeration failed at start={start}: status {response.status_code}"
                    )

                payload = _organization_json(response, f"Organization enumeration at start={start}")
                values = payload.get("values") or []
                for org in values:
                    org_id = org.get("id")
                    # De-duplicated because offset pagination over a mutating
                    # collection can serve the same organization on two consecutive
                    # pages; a repeated id would become a duplicate row in the
                    # lookup table and fan out every ticket joined to it (Devin
                    # Review on #1274). Pagination still advances by the raw page
                    # length — the duplicate occupied a slot upstream either way.
                    if org_id is not None and str(org_id) not in seen:
                        seen.add(str(org_id))
                        ids.append(str(org_id))

                if payload.get("isLastPage") or not values:
                    break
                start += len(values)

        logger.info("Enumerated %d Jira organizations", len(ids))
        return ids

    def fetch_organization(self, org_id: str, client: httpx.Client | None = None) -> dict[str, Any] | None:
        """One organization with its detail fields, from the CSM API.

        ``GET /organization/{id}`` is the only CSM operation that returns each
        detail's ``id`` alongside its ``name``; the batched
        ``POST /organization/profile/fetch`` and ``GET /organization/details`` both
        omit it. Matching on the id is what makes the mapping survive a detail-field
        rename, so the per-organization call is worth the extra requests — the
        refresh is a low-frequency job, not a per-ticket one.

        Args:
            org_id: JSM organization id.
            client: Optional client to reuse. A caller sweeping every organization
                should pass one — a per-request client pays a fresh TLS handshake
                each time, which dominates the sweep. Omitted, one is created and
                closed around this single request.

        Returns:
            The organization dict, or ``None`` when it no longer exists (404).

        Raises:
            JiraFetchError: on auth, rate-limit, or server errors — the caller must
                keep the previous row rather than blank a real value.
        """
        if not self.is_configured():
            raise JiraFetchError(f"Organization fetch for {org_id} failed: Jira service not configured")

        cloud_id = self.resolve_cloud_id()
        url = f"https://api.atlassian.com/jsm/csm/cloudid/{cloud_id}/api/v1/organization/{org_id}"

        try:
            if client is not None:
                response = client.get(url, auth=self.auth, headers={"Accept": "application/json"})
            else:
                with httpx.Client(timeout=30) as own_client:
                    response = own_client.get(url, auth=self.auth, headers={"Accept": "application/json"})
        except httpx.RequestError as e:
            raise JiraFetchError(f"Organization fetch for {org_id} failed: connection — {e}") from e

        if response.status_code == 200:
            return _organization_json(response, f"Organization fetch for {org_id}")
        if response.status_code == 404:
            return None
        if response.status_code in (401, 403):
            raise JiraFetchError(
                f"Organization fetch for {org_id} failed: auth error ({response.status_code}) — "
                "the account needs Jira Service Management access for the CSM API"
            )
        if response.status_code == 429:
            raise JiraFetchError(f"Organization fetch for {org_id} failed: rate limited (429) — retry later")
        if response.status_code >= 500:
            raise JiraFetchError(f"Organization fetch for {org_id} failed: server error ({response.status_code})")
        raise JiraFetchError(f"Organization fetch for {org_id} failed: unexpected status {response.status_code}")

    def save_issue(self, issue_data: dict[str, Any]) -> Path | None:
        """
        Save issue data to JSON file.

        Args:
            issue_data: Complete issue data from Jira API

        Returns:
            Path to saved file or None if save failed
        """
        issue_key = issue_data.get("key")
        if not issue_key:
            logger.error("Issue data missing 'key' field")
            return None

        # Defense-in-depth: validate `issue_key` BEFORE any code path
        # uses it — including the HTTP URL constructions in
        # fetch_remote_links / fetch_refresh_fields below. The webhook
        # handler already validates upstream, but a future internal
        # caller invoking save_issue directly with attacker-controlled
        # input would otherwise fire outbound requests with a malicious
        # path component (limited SSRF / path manipulation against the
        # Jira API server) before the filesystem-side guard rejected it.
        # Issue #83 round 3.
        if not is_valid_issue_key(issue_key):
            logger.error(f"Refusing to save issue with malformed key: {issue_key!r}")
            return None

        # Create data directory if needed
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Add metadata
        issue_data["_synced_at"] = datetime.now(UTC).isoformat()

        # Overlay-skip guard: if fetch_remote_links raises (auth/server failure),
        # leave the _remote_links key ABSENT. transform_remote_links treats absent key
        # as "no fresh data, preserve existing parquet rows". A present-but-empty list
        # would be interpreted as "this issue has no remote links — wipe existing".
        issue_key_for_links = issue_data.get("key")
        if issue_key_for_links:
            try:
                issue_data["_remote_links"] = self.fetch_remote_links(issue_key_for_links)
            except JiraFetchError as e:
                logger.warning(
                    f"Skipping _remote_links overlay for {issue_key_for_links}: {e}. "
                    f"Existing parquet rows will be preserved."
                )

        # Overlay the configured refresh fields, fetched with the primary token.
        refreshed = self.fetch_refresh_fields(issue_key)
        if refreshed:
            if "fields" not in issue_data:
                issue_data["fields"] = {}
            for field_id, _ in refresh_fields():
                if field_id in refreshed:
                    issue_data["fields"][field_id] = refreshed[field_id]
            logger.info(f"Overlayed refresh fields for {issue_key}")

        # Save to file (one file per issue for now, later we'll batch to parquet)
        # Path.resolve() containment as second layer; the regex check
        # above is the primary defense.
        issues_dir = self.data_dir / "issues"
        issues_dir.mkdir(parents=True, exist_ok=True)
        try:
            file_path = safe_join_under(issues_dir, f"{issue_key}.json")
        except ValueError as e:
            logger.error(f"Path traversal blocked for issue {issue_key!r}: {e}")
            return None

        try:
            from connectors.jira.file_lock import issue_json_lock

            # Lock protects the JSON write + Parquet transform from concurrent
            # SLA poll writes. Attachment download stays outside the lock.
            with issue_json_lock(issues_dir, issue_key):
                # Atomic write: temp file + replace
                fd, tmp_path = tempfile.mkstemp(dir=str(file_path.parent), suffix=".tmp")
                os.fchmod(fd, 0o660)  # Restore group rw for ACL
                try:
                    with os.fdopen(fd, "w") as f:
                        json.dump(issue_data, f, indent=2, default=str)
                    os.replace(tmp_path, str(file_path))
                except Exception:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    raise
                # Keep the sidecar marker in sync with this write's incomplete
                # state, exactly like JiraBackfill.save_issue — without it a
                # webhook refetch that failed mid-pagination (e.g. one 429
                # under the zero in-request retry budget) would persist a
                # truncated JSON that a later --skip-existing backfill can
                # never see, so the issue would only heal on further activity.
                # Best-effort: the marker only schedules a heal, so a failure
                # here (e.g. a marker file owned by the backfill's OS user
                # that this process cannot touch) must never abort the
                # publish — the JSON is already replaced — or skip the
                # parquet transform below.
                try:
                    _sync_incomplete_marker(file_path, issue_data)
                except OSError as marker_err:
                    logger.warning(f"Could not sync .incomplete marker for {issue_key}: {marker_err}")
                logger.info(f"Saved issue {issue_key} to {file_path}")

                # Trigger incremental Parquet transform FIRST for real-time rsync.
                # This must run before attachment download because large attachments
                # can cause gunicorn worker timeouts (SIGKILL), preventing the
                # transform from ever running. Parquet availability is higher
                # priority than local attachment files.
                trigger_incremental_transform(issue_key, deleted=False)

            # Download attachments OUTSIDE the lock (non-fatal: timeout/failure
            # here should not block the webhook response or prevent Parquet
            # from being updated, and can be slow)
            try:
                downloaded = self.download_all_attachments(issue_data)
                if downloaded:
                    logger.info(f"Downloaded {len(downloaded)} attachments for {issue_key}")
                    # Re-transform now that the files exist: the transform above
                    # deliberately ran BEFORE the download (worker-timeout
                    # rationale), so it catalogued any freshly attached file
                    # with local_path=NULL — the download endpoint would 404
                    # (`attachment_not_stored`) the very attachments users are
                    # most likely to fetch, until some later event happened to
                    # re-transform the issue. Same non-fatal posture as the
                    # download itself (Devin on #1297).
                    #
                    # Re-acquire the per-issue lock for JUST this call (the slow
                    # download stays outside): transform_single_issue reads the
                    # issue JSON before taking only the parquet month lock, so
                    # an unlocked transform here could publish a stale snapshot
                    # over a concurrent poll_sla read-modify-write that holds
                    # this lock across its own write+transform (Devin on #1297).
                    #
                    # `warn_unresolved=False`: this is the SECOND transform of
                    # the same payload in the same request. The first already
                    # reported any missing-`jsdPublic` gap, and logging it twice
                    # doubled the count an operator uses to size the anomaly —
                    # for attachment-bearing events only, which is worse than a
                    # uniform overcount. Same suppression the batch path's
                    # throwaway grouping pass uses.
                    with issue_json_lock(issues_dir, issue_key):
                        trigger_incremental_transform(issue_key, deleted=False, warn_unresolved=False)
            except Exception as att_err:
                logger.warning(f"Attachment download failed for {issue_key}: {att_err}")

            return file_path
        except Exception as e:
            logger.error(f"Failed to save issue {issue_key}: {e}")
            return None

    def download_attachment(self, attachment: dict[str, Any], issue_key: str) -> Path | None:
        """
        Download a single attachment from Jira. Sweeps stale staging files
        first — see :func:`sweep_stale_attachment_staging`.

        Args:
            attachment: Attachment metadata from Jira API
            issue_key: Issue key for organizing files

        Returns:
            Path to the file if THIS call newly published it; ``None`` when
            nothing new landed — download failed, skipped by policy, or the
            file is already on disk. Jira attachment ids are immutable (a
            re-upload mints a new id), so an existing ``<id>_<name>`` file is
            already the right bytes; re-fetching it on every webhook event
            both wasted bandwidth and made ``save_issue``'s post-download
            re-transform gate fire on every event for any attachment-bearing
            issue (Devin on #1297). Callers that need "already present" to
            count as success (the backfill's ``--dry-run`` bookkeeping) use
            the backfill sibling, whose exists-skip RETURNS the path — the
            asymmetry is deliberate.
        """
        content_url = attachment.get("content")
        filename = attachment.get("filename", "unknown")
        size = attachment.get("size", 0)
        attachment_id = attachment.get("id", "unknown")

        if not content_url:
            logger.warning(f"Attachment {filename} has no content URL")
            return None

        # SSRF guard (audit L3): content_url comes from the webhook payload, so
        # it is caller-supplied. Only ever fetch from the configured Jira host.
        if not self._attachment_url_allowed(content_url):
            logger.error(
                f"Refusing attachment download from non-Jira URL for {filename!r}: {content_url!r}",
            )
            return None

        # Skip large attachments
        if size > self.MAX_ATTACHMENT_SIZE:
            logger.warning(f"Skipping attachment {filename} ({size} bytes) - exceeds max size")
            return None

        # Create issue-specific attachment directory.
        # Two-layer guard against path traversal via issue_key (issue #83).
        if not is_valid_issue_key(issue_key):
            logger.error(f"Refusing to download attachment for malformed key: {issue_key!r}")
            return None
        try:
            issue_attachments_dir = safe_join_under(self.attachments_dir, issue_key)
        except ValueError as e:
            logger.error(f"Path traversal blocked for attachment {issue_key!r}: {e}")
            return None
        issue_attachments_dir.mkdir(parents=True, exist_ok=True)
        sweep_stale_attachment_staging(issue_attachments_dir)

        # Use attachment ID in filename to avoid collisions
        safe_filename = f"{attachment_id}_{filename}"
        try:
            file_path = safe_join_under(issue_attachments_dir, safe_filename)
        except ValueError as e:
            logger.error(f"Path traversal blocked for attachment filename {safe_filename!r}: {e}")
            return None

        # Jira attachment ids are immutable, so an existing <id>_<name> file
        # is already the right bytes — but only if it is COMPLETE. A short
        # file (e.g. a worker SIGKILLed mid-write by the pre-atomic writer)
        # must be re-fetched, or the download endpoint serves the truncated
        # bytes with a self-consistent Content-Length forever (Devin on
        # #1297).
        if file_path.exists() and (not size or file_path.stat().st_size == size):
            logger.debug(f"Attachment {safe_filename} already on disk; skipping download")
            return None

        try:
            with httpx.Client(timeout=60, follow_redirects=True) as client:
                response = client.get(
                    content_url,
                    auth=self.auth,
                )

            if response.status_code == 200:
                # Publish atomically (per-process temp + os.replace, like the
                # organizations publish): a reader streaming this exact path —
                # the attachment download endpoint — must never observe a
                # truncated in-place rewrite when a webhook re-downloads the
                # same issue's attachments.
                # Bounded temp name: appending to the full name could push a
                # near-NAME_MAX (255-byte) filename over the limit and make a
                # previously-storable attachment fail to save. 40 codepoints
                # (<=160 UTF-8 bytes) keeps the total well under NAME_MAX and
                # stays unique: the name starts with the attachment id.
                # pid alone is not unique enough: two webhook events for the
                # same issue run concurrently in one process's threadpool and
                # would share the staging name — one os.replace() could publish
                # the other's half-written bytes (Devin on #1297). The random
                # component makes each writer's staging file its own.
                tmp_path = file_path.with_name(f".tmp-{os.getpid()}-{os.urandom(4).hex()}-{file_path.name[:32]}")
                try:
                    with open(tmp_path, "wb") as f:
                        f.write(response.content)
                    # os.replace preserves the TEMP file's mode (0666 & umask),
                    # not the previous inode's — pin it explicitly so a
                    # restrictive deploy-time umask cannot leave the published
                    # file unreadable to the serving process. 0o660, matching
                    # the sibling issue-JSON writer's "Restore group rw for
                    # ACL" pin: 0o644 would grant world-read to attachment
                    # bytes AND collapse any named POSIX-ACL entries to
                    # read-only (chmod resets the ACL mask from the group
                    # bits) — a widening relative to the pre-atomic writer
                    # under the documented 0007-umask ACL deployments (Devin
                    # on #1297).
                    os.chmod(tmp_path, 0o660)
                    os.replace(tmp_path, file_path)
                except BaseException:
                    tmp_path.unlink(missing_ok=True)
                    raise
                logger.info(f"Downloaded attachment {filename} to {file_path}")
                return file_path
            else:
                logger.error(f"Failed to download attachment {filename}: {response.status_code}")
                return None

        except httpx.RequestError as e:
            logger.error(f"Request error downloading attachment {filename}: {e}")
            return None

    def download_all_attachments(self, issue_data: dict[str, Any]) -> list[Path]:
        """
        Download all attachments for an issue (from fields and comments).

        Args:
            issue_data: Complete issue data from Jira API

        Returns:
            List of paths NEWLY downloaded by this call. Files already on
            disk are skipped and not listed (see ``download_attachment``), so
            an empty list means "nothing new" — which is exactly what
            ``save_issue`` gates its post-download re-transform on.
        """
        issue_key = issue_data.get("key", "unknown")
        downloaded = []

        # Get direct attachments from issue fields
        attachments = issue_data.get("fields", {}).get("attachment", [])
        logger.info(f"Issue {issue_key} has {len(attachments)} direct attachments")

        for attachment in attachments:
            path = self.download_attachment(attachment, issue_key)
            if path:
                downloaded.append(path)

        # Check comments for inline attachments (ADF media nodes)
        # Comments in Jira Cloud use Atlassian Document Format (ADF)
        comments_data = issue_data.get("fields", {}).get("comment", {})
        comments = comments_data.get("comments", [])

        for comment in comments:
            # ADF body may contain mediaSingle/mediaInline nodes with attachments
            body = comment.get("body", {})
            media_attachments = self._extract_media_from_adf(body)

            for media_id in media_attachments:
                # Media in comments references attachments by ID
                # Find matching attachment in the attachment list
                for attachment in attachments:
                    if attachment.get("id") == media_id:
                        # Already downloaded above
                        break
                else:
                    # Media not in main attachments - try to fetch directly
                    logger.debug(f"Found media {media_id} in comment, not in attachments")

        logger.info(f"Downloaded {len(downloaded)} attachments for {issue_key}")
        return downloaded

    def _extract_media_from_adf(self, node: dict[str, Any]) -> list[str]:
        """
        Extract media IDs from Atlassian Document Format (ADF) content.

        Args:
            node: ADF node (recursive structure)

        Returns:
            List of media attachment IDs found in the content
        """
        media_ids = []

        if not isinstance(node, dict):
            return media_ids

        # Check if this node is a media node
        node_type = node.get("type", "")
        if node_type in ("mediaSingle", "mediaInline", "media"):
            # ``attrs`` is third-party JSON and need not be an object; reading it
            # as one raised, and the raise escaped the walk, not just this node.
            attrs = node.get("attrs")
            if isinstance(attrs, dict) and (media_id := attrs.get("id")):
                media_ids.append(media_id)

        # Recursively check content
        content = node.get("content")
        if isinstance(content, list):
            for child in content:
                media_ids.extend(self._extract_media_from_adf(child))

        return media_ids

    def process_webhook_event(self, event_data: dict[str, Any]) -> bool:
        """
        Process a webhook event by fetching and saving the related issue.

        Args:
            event_data: Webhook event payload from Jira

        Returns:
            True if processing succeeded, False otherwise
        """
        # Extract issue key from event
        # Jira webhook format: {"webhookEvent": "jira:issue_updated", "issue": {"key": "KSP-123", ...}}
        # Defensive: a payload may carry `"issue": null` rather than
        # omitting the key. The webhook handler normalises this, but
        # do the same here too — process_webhook_event is reachable from
        # internal callers as well as the webhook path.
        issue = event_data.get("issue") or {}
        issue_key = issue.get("key")

        if not issue_key:
            # Try alternative format for some events
            issue_key = event_data.get("issue_key")

        if not issue_key:
            logger.warning(f"Could not extract issue key from webhook event: {event_data.get('webhookEvent')}")
            return False

        # Defense-in-depth: even if the webhook layer's validation is bypassed
        # (e.g. a future internal caller invokes process_webhook_event directly),
        # refuse a malformed key here. Issue #83.
        if not is_valid_issue_key(issue_key):
            logger.error(f"process_webhook_event: refusing malformed issue key {issue_key!r}")
            return False

        webhook_event = event_data.get("webhookEvent", "unknown")
        logger.info(f"Processing webhook event: {webhook_event} for issue {issue_key}")

        # Handle deletion of the ISSUE. A sub-entity deletion
        # (`comment_deleted`, `attachment_deleted`, ...) is an ordinary content
        # change: fall through to the refetch below, which is precisely what
        # drops the deleted comment from the stored thread.
        if webhook_event.lower() in _ISSUE_DELETED_EVENTS:
            return self._handle_deletion(issue_key)

        # Fetch fresh data from API (webhook payload may not have all fields).
        # comment_max_retries=0: this runs off an active request (see
        # app/api/jira_webhooks.py's run_in_threadpool dispatch), so a 429 on
        # the comment-pagination endpoint must mark the issue
        # _comments_incomplete immediately rather than sleep — the marker,
        # plus a later backfill heal (_needs_refetch), recovers it without
        # tying up a request thread for up to 15 minutes (Devin Review on
        # #1283).
        issue_data = self.fetch_issue(issue_key, comment_max_retries=0)
        if not issue_data:
            # If fetch fails, try to use embedded issue data from webhook
            if issue and issue.get("fields"):
                logger.info(f"Using embedded issue data for {issue_key}")
                issue_data = issue
                # The fallback payload never went through complete_issue_comments:
                # its `fields.comment.comments` is whatever Jira chose to embed,
                # and the comments upsert is an issue-scoped delete-then-insert.
                # Treat it as authoritative for comments ONLY when it demonstrably
                # carries the whole THREAD; otherwise mark it incomplete so the
                # incremental transform preserves the stored rows. Per-comment
                # field gaps are not this check's business — the write layer
                # carries a stored same-version `public_visibility` forward, so a
                # flagless embed costs nothing here (see
                # `incremental_transform._carry_forward_public_visibility`).
                #
                # Worth knowing when reading this path: since January 2018 Cloud
                # `jira:issue_*` bodies carry no comment objects at all, so for
                # the highest-volume events the structural checks below already
                # answer False. Only `comment_created`/`comment_updated` embeds
                # reach the length test with a real thread.
                if not _embedded_comments_are_complete(issue_data):
                    logger.info(
                        "Webhook fallback payload for %s does not carry a complete comment "
                        "thread — marking _comments_incomplete so stored comments are preserved",
                        issue_key,
                    )
                    issue_data["_comments_incomplete"] = True
            else:
                return False

        # Save the issue
        return self.save_issue(issue_data) is not None

    def _handle_deletion(self, issue_key: str) -> bool:
        """
        Handle issue deletion by marking it as deleted and updating Parquet.

        Args:
            issue_key: Key of deleted issue

        Returns:
            True if handled successfully
        """
        # Defense-in-depth path-traversal guard (issue #83). Callers should
        # already have validated; refuse anyway.
        if not is_valid_issue_key(issue_key):
            logger.error(f"_handle_deletion: refusing malformed issue key {issue_key!r}")
            return False
        try:
            file_path = safe_join_under(self.data_dir / "issues", f"{issue_key}.json")
        except ValueError as e:
            logger.error(f"_handle_deletion: path traversal blocked for {issue_key!r}: {e}")
            return False

        if file_path.exists():
            # Mark as deleted rather than removing
            try:
                from connectors.jira.file_lock import issue_json_lock

                issues_dir = self.data_dir / "issues"
                with issue_json_lock(issues_dir, issue_key):
                    with open(file_path) as f:
                        data = json.load(f)
                    data["_deleted_at"] = datetime.now(UTC).isoformat()

                    # Atomic write: temp file + replace
                    fd, tmp_path = tempfile.mkstemp(dir=str(file_path.parent), suffix=".tmp")
                    os.fchmod(fd, 0o660)  # Restore group rw for ACL
                    try:
                        with os.fdopen(fd, "w") as f:
                            json.dump(data, f, indent=2, default=str)
                        os.replace(tmp_path, str(file_path))
                    except Exception:
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass
                        raise
                    logger.info(f"Marked issue {issue_key} as deleted")

                    # Remove from Parquet files
                    trigger_incremental_transform(issue_key, deleted=True)

                return True
            except Exception as e:
                logger.error(f"Failed to mark issue {issue_key} as deleted: {e}")
                return False

        logger.info(f"Issue {issue_key} not found locally, nothing to delete")
        return True


# Singleton instance
_jira_service: JiraService | None = None


def get_jira_service() -> JiraService:
    """Get or create Jira service singleton."""
    global _jira_service
    if _jira_service is None:
        _jira_service = JiraService()
    return _jira_service
