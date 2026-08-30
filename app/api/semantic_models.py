"""Semantic-model / semantic-source admin API, plus the public export and
search surface (open semantic-layer contract, Task 10).

Two tiers:

- ``/api/admin/semantic-models`` and ``/api/admin/semantic-sources`` are
  ``require_admin`` — creating/editing/deleting the canonical Ossie
  documents and configuring where they sync from is an admin action.
- ``/api/semantic-models/{slug}.yaml`` (export) and
  ``/api/semantic-models/search`` are any-authenticated-user, gated instead
  on the linked Data Package's grant (``data_package_semantic_models``) —
  a model rides the same visibility as the package(s) it belongs to, the
  same way a table's visibility rides its package membership
  (``can_access_table``). A model with no linked package is reachable by
  admins only, since there is no package grant to check.

Ownership rule (``_is_source_owned``, the single predicate every mutating
endpoint below shares): a model whose ``source`` is not ``'manual'`` was
written by a sync (``import_source``) and refuses create/update/delete with
409 ``source_owned`` — a manual create colliding with its slug, an edit, or
a delete would each be silently reverted or fought by the next sync, so
editing at the source (or detaching, F3) is the only way to make a change
stick.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel

from app.auth.access import require_admin
from app.auth.dependencies import _get_db, get_current_user
from app.instance_config import get_studio_enabled
from app.resource_types import ResourceType
from src.audit_helpers import client_kind_from_user
from src.repositories import (
    RequiresPostgresBackend,
    audit_repo,
    data_packages_repo,
    semantic_model_repo,
    semantic_source_repo,
    use_pg,
)
from src.semantic.cache_render import DEFAULT_TTL_SECONDS
from src.semantic.document_validation import validate_document
from src.semantic.ownership import with_owned_model_count
from src.semantic.projection import project_document, prune_model
from src.semantic_context import get_semantic_context as _get_semantic_context
from src.semantic_context import get_semantic_schema as _get_semantic_schema
from src.semantic_validation import validate_query

logger = logging.getLogger(__name__)

router = APIRouter(tags=["semantic-models"])

# Auto-draft sweep (semantic-phase5 wave 2) — how many uncovered tables one
# sweep tick drafts, and how long it waits for each headless session before
# moving on. Tuning knobs, not magic numbers: a batch of 3 keeps one sweep
# tick's LLM spend bounded, and the scheduler re-fires every 55 minutes
# (services/scheduler/__main__.py) so a large backlog drains gradually
# rather than in one expensive tick.
_SWEEP_BATCH_SIZE = 3
_SWEEP_SESSION_TIMEOUT_S = 60

# How stale a ``semantic_draft_pending_at`` stamp has to be before the sweep
# treats the table as a candidate again. Seven days, and it is the ONLY thing
# that re-opens a table whose session ran but filed nothing an admin can
# resolve.
#
# The alternative — clearing the stamp on the way out of such a tick — was
# wrong twice over. (1) ``run_one_shot`` reports a wait timeout by RETURNING
# ``timed_out=True``, not by raising, and the sandbox keeps processing the
# turn after it returns (``app/chat/headless.py``): every session slower than
# ``_SWEEP_SESSION_TIMEOUT_S`` therefore looked like "filed nothing", got
# un-stamped, and then filed in the background — so the table was re-drafted
# on every following tick, one duplicate pending suggestion each. (2) An
# immediate clear also makes the table eligible again on the very next tick,
# and with ``list_all()``'s stable order a handful of tables the drafter keeps
# declining occupy the whole batch forever, starving everything behind them.
#
# Seven days is a scheduler-relative number, not a magic one: the sweep fires
# every 55 minutes, so it is ~180 skipped ticks — long enough that a declined
# table costs about one retry a week rather than one an hour, short enough
# that a table the drafter declined because of a transient gap (a missing
# profile, an empty catalog, a model having a bad day) is not shelved for a
# quarter. Fresh, never-stamped tables always take the batch ahead of
# stale-stamped ones, so this retry stream can never starve a new table.
_SWEEP_STAMP_RETRY_AFTER_S = 7 * 24 * 60 * 60

# Sort sentinel for a never-stamped candidate — see ``_stamp_sort_key``.
_SWEEP_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _stamp_as_utc(stamp: Any) -> datetime:
    """Coerce a ``semantic_draft_pending_at`` value to an aware UTC datetime.

    Postgres hands back an aware datetime for its ``timestamptz`` column;
    the naive and ISO-string branches are defensive (a driver that decodes
    differently must not crash the sweep), and a naive value is read as UTC
    because that is what the writer stored.
    """
    if isinstance(stamp, str):
        stamp = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


def _stamp_sort_key(stamp: Any) -> tuple[int, datetime]:
    """Ordering key for one candidate: never-stamped first, then oldest
    stamp first.

    A single ``(group, when)`` tuple rather than two passes, so ``sorted``'s
    stability preserves ``list_all()``'s own order inside the unstamped
    group. Both slots always hold the same types, so the tuples are always
    comparable (a ``None`` in slot 2 would raise on the first comparison
    against a datetime).
    """
    if stamp is None:
        return (0, _SWEEP_EPOCH)
    return (1, _stamp_as_utc(stamp))


def _sweep_candidates(tables: list[dict], *, now: datetime | None = None) -> list[dict]:
    """Filter + order one tick's candidate tables.

    Eligible: no ``semantic_draft_pending_at`` stamp at all, or one older
    than :data:`_SWEEP_STAMP_RETRY_AFTER_S`. Ordered never-stamped first
    (see :func:`_stamp_sort_key`), so a backlog of repeatedly-declined
    tables reclaiming their eligibility can never take the batch away from
    a table that has not been tried once.

    A stamp that cannot be read at all (unparseable string, odd type) is
    treated as "stamped and fresh" — the table is skipped this tick. Erring
    toward skipping is the safe direction: the opposite would draft a table
    whose session may still be running.
    """
    now = now or datetime.now(UTC)
    eligible: list[dict] = []
    for table in tables:
        stamp = table.get("semantic_draft_pending_at")
        if not stamp:
            eligible.append(table)
            continue
        try:
            age = (now - _stamp_as_utc(stamp)).total_seconds()
        except (TypeError, ValueError, AttributeError):
            logger.warning(
                "semantic auto-draft sweep: unreadable semantic_draft_pending_at on table %s — skipping this tick",
                table.get("id"),
            )
            continue
        if age >= _SWEEP_STAMP_RETRY_AFTER_S:
            eligible.append(table)
    return sorted(eligible, key=lambda t: _stamp_sort_key(t.get("semantic_draft_pending_at")))


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class SemanticModelCreate(BaseModel):
    document: str
    description: str | None = None


class SemanticModelApply(BaseModel):
    document: str
    description: str | None = None
    # Optimistic lock for read → modify → apply loops: the content_hash the
    # caller's edit was based on. A mismatch 409s instead of overwriting.
    expected_content_hash: str | None = None


class SemanticModelUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class SemanticModelPackageLink(BaseModel):
    package_id: str


class SemanticModelPackageIds(BaseModel):
    package_ids: list[str]


class SemanticQueryValidate(BaseModel):
    sql: str
    expected: list[dict] | None = None
    target_engine: str = "duckdb"


class SemanticSourceCreate(BaseModel):
    kind: str
    name: str
    adapter: str = "native"
    config: dict = {}
    enabled: bool = True


class SemanticSourceUpdate(BaseModel):
    name: str | None = None
    adapter: str | None = None
    config: dict | None = None
    enabled: bool | None = None


_VALID_KINDS = ("git", "upload", "connection")


def _assert_no_provenance_override(config: dict | None) -> None:
    """Refuse an admin-supplied ``config.provenance``.

    ``provenance`` names the ``(source, source_ref)`` pair a source's models,
    metrics, glossary terms and column descriptions are written AND PRUNED
    under (see ``src/semantic/transports.py``). It exists for exactly one
    writer — the auto-migration of the retired per-connector semantic
    refreshes, which writes its rows through the repository, never through
    this API — so accepting it here would let an admin-authored source claim
    a migrated connection's prune scope and have the next sweep delete that
    connection's rows.

    Refused outright rather than validated: one enforcement story, and no
    second place the field can enter the system. (``resolve_provenance``
    still validates every stored override on read — this is the outer wall,
    not the only one.)
    """
    if config and "provenance" in config:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "provenance_not_settable",
                "hint": (
                    "config.provenance is managed by Agnes (it is how a source migrated off a retired "
                    "connector refresh keeps owning the rows it already wrote) and cannot be set through "
                    "this API. Remove it and register the source normally."
                ),
            },
        )


def _assert_known_adapter(name: str) -> None:
    """Refuse an adapter name nothing is registered under.

    Without this a typo registers happily and only fails on the first sync —
    the "registered but never runs" state the connector validators already
    exist to prevent. `UnknownAdapter` already names what IS available.
    """
    from src.semantic.adapters import UnknownAdapter, get_adapter

    try:
        get_adapter(name)
    except UnknownAdapter as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


# ---------------------------------------------------------------------------
# Access helper — the linked-package grant gate shared by export + search
# ---------------------------------------------------------------------------


def _can_read_model(user: dict, model_row: dict, conn: duckdb.DuckDBPyConnection) -> bool:
    """True iff ``user`` may read ``model_row``: admin, a grant on any Data
    Package the model is linked to, or a direct grant on the model itself.

    The direct grant layers UNDER the package path the way per-table grants
    layer under the package stack. It is not decoration: ``ResourceType
    .SEMANTIC_MODEL`` is registered, so ``/admin/access`` offers it as a
    grantable resource — and a control an admin can set but nothing ever reads
    is worse than no control at all, because it reports success while granting
    nothing.
    """
    from app.auth.access import can_access, is_user_admin

    user_id = user.get("id") if isinstance(user, dict) else None
    if not user_id:
        return False
    if is_user_admin(user_id, conn):
        return True
    if can_access(user_id, ResourceType.SEMANTIC_MODEL.value, model_row["id"], conn):
        return True
    package_ids = semantic_model_repo().list_packages_for_model(model_row["id"])
    return any(can_access(user_id, ResourceType.DATA_PACKAGE.value, pkg_id, conn) for pkg_id in package_ids)


def _export_denied_message(slug: str) -> str:
    return (
        f"Semantic model '{slug}' is not linked to a Data Package you have access to. "
        "Ask an admin to link it to a Data Package you have access to, or grant you one it already belongs to."
    )


def _project(document_json: dict | None, *, source: str, source_ref: str | None) -> None:
    """Project one stored model's document into the flat tables
    (``metric_definitions``, ``glossary_terms``, ``column_metadata``) —
    the same call ``src/semantic/importer.py`` makes for a synced source, so
    a model created or edited through this admin API also reaches
    ``agnes catalog --metrics``, chat, and search rather than sitting in
    ``semantic_models`` unread.

    ``partial=True``: every ``source='manual'`` row shares one provenance
    tuple (``source='manual', source_ref=None``) — unlike a git/upload
    source sync, where ``import_documents`` merges every document of ONE
    sync batch before a single ``project_document`` call. Here each POST/PUT
    is its own call for just ONE model, so an unscoped prune would delete a
    *sibling* manual model's already-projected rows on every unrelated
    write. ``partial`` narrows the prune to this document's own model-id
    prefix (see ``project_document``'s docstring), leaving every other
    model's rows untouched.

    ``column_metadata`` is one further step removed: the admin metadata API
    (``app/api/metadata.py``) writes the same ``(table_id, column_name)``
    key under ``source='manual'`` too, so the projection stores a manual
    model's dataset fields under its own distinct source
    (``MANUAL_MODEL_COLUMN_SOURCE``) and never overwrites a row another
    writer owns — admin-authored descriptions win, and the projection's
    prune cannot reach them (see ``src/semantic/projection.py::
    _column_source``).
    """
    if not document_json:
        return
    project_document(document_json, source=source, source_ref=source_ref, partial=True)


def _is_source_owned(row: dict) -> bool:
    """True iff ``row`` was written by a sync and hasn't been detached (F3)
    — the single ownership predicate every mutating endpoint (create
    collision, update, delete, ``_check_apply``) must agree on, so a
    manual write can never coexist or race with the source that owns the
    slug. A DETACHED source-owned row is exempt, same as everywhere else
    this predicate is applied: detaching is the deliberate escape hatch."""
    return row.get("source") != "manual" and row.get("sync_mode") != "detached"


def _source_owned_message(row: dict) -> str:
    return (
        f"this model is owned by source '{row['source']}'"
        + (f" (source_ref={row['source_ref']!r})" if row.get("source_ref") else "")
        + " — edit it there, then re-sync, rather than here"
    )


def _raise_source_owned(row: dict) -> None:
    raise HTTPException(
        status_code=409,
        detail={"code": "source_owned", "message": _source_owned_message(row)},
    )


def _resolve_model(model_ref: str) -> dict | None:
    """Accept either a model id or its slug — ids are opaque
    (``<source>/<source_ref>/<slug>``), so a slug is the friendlier handle
    for an interactive admin."""
    repo = semantic_model_repo()
    return repo.get(model_ref) or repo.get_by_slug(model_ref)


# ---------------------------------------------------------------------------
# Apply pipeline — shared by the /apply endpoint and the moderation-queue
# replay (spec 2026-08-24-semantic-layer-chat-authoring)
# ---------------------------------------------------------------------------


class SemanticApplyError(ValueError):
    """A refused apply, carrying a machine-readable ``code``.

    A plain ``ValueError`` subclass so the authoring-suggestions replay path
    (which maps any exception onto 409 ``create_failed`` + reopen) needs no
    knowledge of this module's HTTP vocabulary."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _check_apply(document: str, expected_content_hash: str | None = None):
    """Validate ``document`` and run the write-independent guards.

    Returns ``(slug, validation_result)``. Raises ``SemanticApplyError``:

    - ``invalid_document`` — schema errors, or no named ``semantic_model``.
    - ``source_owned`` — the slug belongs to an imported model that hasn't
      been detached (F3). Apply refuses for admins and non-admins alike, the
      same as the raw admin POST/PUT/DELETE (see ``_is_source_owned``): the
      next source sync would not revert the write, it would coexist with
      it, and ``get_by_slug`` would resolve ambiguously. A DETACHED
      source-owned model is exempt: that's exactly the danger-flow escape
      hatch F3 exists for (see ``POST .../detach``) — sync already refuses
      to touch a detached row, so there is no second-writer ambiguity to
      guard against here.
    - ``stale_document`` — ``expected_content_hash`` no longer matches.
    """
    result = validate_document(document)
    if not result.ok:
        raise SemanticApplyError("invalid_document", "; ".join(str(e) for e in result.errors))
    models = (result.parsed or {}).get("semantic_model") or []
    slug = models[0].get("name") if models else None
    if not slug:
        raise SemanticApplyError("invalid_document", "Document declares no semantic_model entry with a name")

    existing = semantic_model_repo().get_by_slug(slug)
    if existing is not None and _is_source_owned(existing):
        raise SemanticApplyError(
            "source_owned",
            f"slug '{slug}' is owned by source '{existing['source']}'"
            + (f" (source_ref={existing['source_ref']!r})" if existing.get("source_ref") else "")
            + " — edit it there, then re-sync, rather than here",
        )
    if expected_content_hash is not None:
        current = existing.get("content_hash") if existing is not None else None
        if current != expected_content_hash:
            raise SemanticApplyError(
                "stale_document",
                f"model '{slug}' changed since it was read — re-read it and re-apply",
            )
    return slug, result


def apply_manual_model(
    document: str,
    description: str | None = None,
    expected_content_hash: str | None = None,
) -> dict:
    """The one write pipeline for a hand-authored model: guards → write →
    project. Used by the ``/apply`` admin branch AND the moderation-queue
    replay, so the two paths cannot diverge. Raises ``SemanticApplyError``
    (a ``ValueError``).

    F3: editing an already-DETACHED model (``_check_apply`` let it through
    on that basis, not ``source == 'manual'``) rewrites the existing row in
    place — ``update_document``, not ``upsert`` — so it keeps its original
    ``source``/``source_ref`` (the importer needs that provenance to
    recognize the row as detached on the next sync) and its
    ``sync_mode='detached'``/detach-tracking columns untouched. A brand-new
    or already-``manual`` model is unaffected: same ``upsert`` path as
    before, under ``source='manual'``.
    """
    slug, result = _check_apply(document, expected_content_hash)
    content_hash = hashlib.sha256(document.encode()).hexdigest()
    existing = semantic_model_repo().get_by_slug(slug)
    if existing is not None and existing.get("sync_mode") == "detached":
        row = semantic_model_repo().update_document(
            existing["id"],
            name=slug,
            description=description,
            document=document,
            document_json=result.parsed,
            spec_version=result.spec_version,
            content_hash=content_hash,
            status="valid",
            validation_errors=None,
            validated_at=datetime.now(UTC),
        )
        _project(result.parsed, source=existing["source"], source_ref=existing.get("source_ref"))
        return row
    row = semantic_model_repo().upsert(
        id=f"manual/_/{slug}",
        slug=slug,
        name=slug,
        description=description,
        document=document,
        document_json=result.parsed,
        spec_version=result.spec_version,
        content_hash=content_hash,
        source="manual",
        source_ref=None,
        status="valid",
        validation_errors=None,
        validated_at=datetime.now(UTC),
    )
    _project(result.parsed, source="manual", source_ref=None)
    return row


def _apply_error_to_http(exc: SemanticApplyError) -> HTTPException:
    if exc.code == "invalid_document":
        return HTTPException(status_code=422, detail={"code": exc.code, "errors": str(exc)})
    return HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)})


# ---------------------------------------------------------------------------
# Admin: semantic-models CRUD
# ---------------------------------------------------------------------------


@router.get("/api/admin/semantic-models")
async def list_semantic_models(
    source: str | None = None,
    source_ref: str | None = None,
    user: dict = Depends(require_admin),
):
    """List every stored semantic model (any status), admin-only."""
    return semantic_model_repo().list_all(source=source, source_ref=source_ref)


@router.get("/api/admin/semantic-coverage")
async def get_semantic_coverage(user: dict = Depends(require_admin)):
    """Registered tables with NO valid semantic model describing them.

    Source-agnostic — unlike ``GET /api/admin/semantic-layer/coverage``
    (Keboola-only, predicts live against one connected project's Metastore),
    this reads what is already stored in ``semantic_models`` regardless of
    source (Keboola, git, manual, upload, connection) and answers a
    narrower question: does a registered table appear in ANY valid model's
    datasets at all. See ``src.semantic_coverage.tables_without_semantic_
    coverage`` for the resolution rules.

    Returns ``{"tables": [...]}`` — full ``table_registry`` rows.
    """
    from src.semantic_coverage import tables_without_semantic_coverage

    return {"tables": tables_without_semantic_coverage()}


@router.post("/api/admin/semantic-auto-draft-sweep")
async def semantic_auto_draft_sweep(user: dict = Depends(require_admin)):
    """Draft a semantic model for tables with zero semantic-layer coverage.

    Scheduler-triggered every 55 minutes (``services/scheduler/__main__.py``)
    — admins can also fire it on demand. For up to ``_SWEEP_BATCH_SIZE``
    uncovered, eligible tables (``tables_without_semantic_coverage`` narrowed
    and ordered by :func:`_sweep_candidates`), runs a headless
    ``semantic-model-builder`` chat session (``app.chat.headless.
    run_one_shot``) authenticated as the non-admin ``semantic-drafter``
    system identity (``app.auth.system_users``) — so every draft it
    produces lands in the ``authoring_suggestions`` moderation queue
    exactly like a human-submitted proposal, never applied directly.

    Dedup: each selected table's ``semantic_draft_pending_at`` is stamped
    BEFORE its session is invoked, not after — a concurrent or overlapping
    sweep tick can then never pick up the same table twice. The flag
    clears when an admin resolves the resulting suggestion, approve or
    reject alike (``app/api/authoring_suggestions.py``).

    Re-eligibility is by stamp AGE, not by clearing the stamp on the way
    out of a tick that filed nothing: a stamp older than
    ``_SWEEP_STAMP_RETRY_AFTER_S`` (7 days) makes the table a candidate
    again, and never-stamped tables sort ahead of stale-stamped ones. That
    is what keeps two things true at once — a slow session's table is not
    re-drafted while its sandbox is still working on the turn, and a table
    the drafter keeps declining does not sit at the head of every batch
    forever. See the constant's own comment for the two bugs an immediate
    clear caused.

    A session hitting the chat manager's per-user concurrency cap
    (``ConcurrencyCapHit``) is counted and skipped, never raised as a
    500 — and its dedup flag IS cleared again on the way out, so the
    table is eligible on the very next tick rather than in a week. That
    un-stamping is safe here and nowhere else: the cap is enforced inside
    ``ChatManager.create_session``, before the prompt is ever sent, so a
    capped table's session provably never started and cannot file anything
    in the background.

    ANY other failure from a table's session is treated the same way
    (counted in ``errored``): the table is un-stamped, logged, and the
    sweep moves on rather than letting one transient broker/LLM/spawn
    error 500 the whole tick and abandon the rest of the batch. Un-stamping
    on an error the session may have survived can at worst cost a duplicate
    draft — one extra queued suggestion an admin rejects — and unlike the
    timeout case it is not the routine outcome, so paying for a fast retry
    is the right trade.

    Returns ``{"triggered": N, "applied": A, "no_apply_call": X,
    "timed_out": T, "skipped_cap": M, "errored": E, "remaining": R}``.
    ``applied`` counts a table whose session produced a NEW
    ``authoring_suggestions`` row before this call's wait ended, detected
    by diffing the semantic-drafter's pending suggestion count immediately
    before and after each session (sessions run strictly in order, one at a
    time, so the diff cannot be confused by another table's suggestion).
    ``timed_out`` counts a session whose wait hit ``_SWEEP_SESSION_TIMEOUT_S``
    with nothing filed yet — ``run_one_shot`` reports that by RETURNING
    ``timed_out=True`` rather than raising, and the sandbox keeps
    processing the turn after it returns, so the table keeps its stamp and
    a suggestion may still arrive. ``no_apply_call`` is a session that
    genuinely finished and chose to file nothing; it keeps its stamp too
    and comes back via the TTL. ``remaining`` is how many eligible tables
    were left over after this tick's batch.

    A3 PG-first ratchet: the dedup flag this sweep relies on
    (``table_registry.mark_semantic_draft_pending`` /
    ``clear_semantic_draft_pending``) is a Postgres-only capability — the
    DuckDB app-state ladder is frozen at v124 and never gained the backing
    column. On a DuckDB-backend instance this raises
    :class:`~src.repositories.RequiresPostgresBackend`, translated by the
    app-wide handler in ``app/main.py`` into a clean ``501`` naming the
    feature, before any real work (no chat session, no table scan) runs.
    """
    if not use_pg():
        raise RequiresPostgresBackend("semantic-auto-draft-sweep")

    from app.auth.system_users import SEMANTIC_DRAFTER_USER_EMAIL, ensure_semantic_drafter_user
    from app.chat.headless import run_one_shot
    from app.chat.manager import ConcurrencyCapHit, get_current_chat_manager
    from src.repositories import authoring_suggestions_repo, table_registry_repo
    from src.semantic_autodraft import build_trigger_prompt
    from src.semantic_coverage import tables_without_semantic_coverage

    candidates = _sweep_candidates(tables_without_semantic_coverage())

    manager = get_current_chat_manager()
    if manager is None:
        # Chat disabled instance-wide — nothing this tick can do. Leave
        # every candidate untouched (no pending stamp) for a later tick.
        result = {
            "triggered": 0,
            "applied": 0,
            "no_apply_call": 0,
            "timed_out": 0,
            "skipped_cap": 0,
            "errored": 0,
            "remaining": len(candidates),
        }
        audit_repo().log(
            user_id=user.get("id"),
            client_kind=client_kind_from_user(user),
            action="semantic_auto_draft_sweep",
            resource="job:semantic-auto-draft-sweep",
            params=result,
        )
        return result

    ensure_semantic_drafter_user()

    batch = candidates[:_SWEEP_BATCH_SIZE]
    remaining = len(candidates) - len(batch)

    suggestions = authoring_suggestions_repo()
    registry = table_registry_repo()

    def _pending_count() -> int:
        return len(
            suggestions.list(
                status="pending",
                domain="semantic-layer",
                created_by=SEMANTIC_DRAFTER_USER_EMAIL,
                limit=100_000,
            )
        )

    triggered = 0
    applied = 0
    no_apply_call = 0
    timed_out = 0
    skipped_cap = 0
    errored = 0

    for table in batch:
        registry.mark_semantic_draft_pending(table["id"])
        before = _pending_count()
        try:
            outcome = await run_one_shot(
                manager,
                user_email=SEMANTIC_DRAFTER_USER_EMAIL,
                agent_id=None,
                prompt=build_trigger_prompt(table),
                timeout_s=_SWEEP_SESSION_TIMEOUT_S,
                profile="semantic-model-builder",
            )
        except ConcurrencyCapHit:
            # The cap is checked inside ``manager.create_session``, which
            # ``run_one_shot`` calls before the prompt is ever sent — so
            # this table's session never started and no suggestion will
            # EVER be created for it, meaning nothing would ever clear the
            # stamp we just wrote. Left set, the table is excluded from
            # every future tick's candidates and is never drafted again
            # without an admin clearing the flag by hand. Clear it here so
            # the table simply falls back into the pool for a later tick
            # once the cap has room.
            registry.clear_semantic_draft_pending(table["id"])
            skipped_cap += 1
            continue
        except Exception:
            # Same stuck-flag hazard as the cap branch, one step wider: a
            # broker/LLM error, a session-spawn failure, anything at all.
            # Left stamped, the table is filtered out of every future
            # tick's candidates and is never drafted again — silently.
            # Un-stamping can at worst cost a duplicate draft (if the
            # session did start and still lands a suggestion later, an
            # admin rejects one extra queued proposal); that is bounded and
            # visible, where permanent exclusion is neither. Swallowing the
            # error also keeps one bad table from 500-ing the tick and
            # abandoning the rest of the batch.
            logger.exception(
                "semantic auto-draft sweep: session failed for table %s — "
                "clearing its pending flag so a later tick can retry it",
                table["id"],
            )
            registry.clear_semantic_draft_pending(table["id"])
            errored += 1
            continue
        triggered += 1
        if _pending_count() > before:
            # Filing happens mid-turn, so this is checked before the timeout
            # branch: a session can land its suggestion and STILL have its
            # wait time out afterwards. Either way an admin resolution is
            # now what clears the stamp.
            applied += 1
        elif (outcome or {}).get("timed_out"):
            # NOT a failure and NOT "filed nothing": `run_one_shot` returns
            # `timed_out=True` without raising, and the sandbox keeps
            # processing the turn after it returns
            # (`app/chat/headless.py`). Un-stamping here — which is what
            # the old code did, by discarding this return and falling into
            # the branch below — let the background session file its
            # suggestion AFTER the table was made eligible again, so the
            # next tick drafted it a second time, and the one after that a
            # third. The stamp stays; if a suggestion does arrive, its
            # resolution clears it, and if none ever does, the stamp ages
            # past `_SWEEP_STAMP_RETRY_AFTER_S` and the table comes back.
            logger.info(
                "semantic auto-draft sweep: session for table %s is still running past %ss — "
                "keeping its pending flag so a later tick cannot double-draft it",
                table["id"],
                _SWEEP_SESSION_TIMEOUT_S,
            )
            timed_out += 1
        else:
            # The session ran to completion and chose not to submit a
            # suggestion. The stamp stays here too: an immediate clear made
            # the table eligible on the very next tick, and a few tables the
            # drafter keeps declining then hold the whole batch forever
            # (`list_all()` order is stable), so nothing behind them is ever
            # reached. `_SWEEP_STAMP_RETRY_AFTER_S` is what brings it back,
            # behind any table that has never been tried.
            no_apply_call += 1

    result = {
        "triggered": triggered,
        "applied": applied,
        "no_apply_call": no_apply_call,
        "timed_out": timed_out,
        "skipped_cap": skipped_cap,
        "errored": errored,
        "remaining": remaining,
    }
    audit_repo().log(
        user_id=user.get("id"),
        client_kind=client_kind_from_user(user),
        action="semantic_auto_draft_sweep",
        resource="job:semantic-auto-draft-sweep",
        params=result,
    )
    return result


@router.post("/api/admin/semantic-models", status_code=201)
async def create_semantic_model(
    body: SemanticModelCreate,
    user: dict = Depends(require_admin),
):
    """Create (or replace) a hand-authored (``source='manual'``) model from
    a pasted Ossie document. Invalid input 422s with the schema errors —
    never stored half-valid.

    A slug colliding with an existing source-owned model 409s ``source_owned``
    (``_is_source_owned``, shared with PUT/DELETE/``_check_apply``) instead of
    creating a shadow ``manual/_/<slug>`` row next to the imported one — two
    rows sharing a slug would leave ``get_by_slug`` (``ORDER BY updated_at
    DESC LIMIT 1``) to resolve the collision nondeterministically. A slug
    matching a DETACHED source-owned row is exempt from that 409 (F3), but
    still can't take the plain upsert path below: that row already owns the
    slug, so writing a second, new ``manual/_/<slug>`` id next to it would
    recreate the exact same collision this guard exists to prevent. It's
    updated in place instead — same handling ``apply_manual_model`` already
    gives this case."""
    result = validate_document(body.document)
    if not result.ok:
        raise HTTPException(status_code=422, detail={"errors": result.errors})

    models = (result.parsed or {}).get("semantic_model") or []
    slug = models[0].get("name") if models else None
    if not slug:
        raise HTTPException(
            status_code=422,
            detail={"errors": ["Document declares no semantic_model entry with a name"]},
        )

    existing = semantic_model_repo().get_by_slug(slug)
    if existing is not None and _is_source_owned(existing):
        _raise_source_owned(existing)

    content_hash = hashlib.sha256(body.document.encode()).hexdigest()
    if existing is not None and existing.get("sync_mode") == "detached":
        row = semantic_model_repo().update_document(
            existing["id"],
            name=slug,
            description=body.description,
            document=body.document,
            document_json=result.parsed,
            spec_version=result.spec_version,
            content_hash=content_hash,
            status="valid",
            validation_errors=None,
            validated_at=datetime.now(UTC),
        )
        _project(result.parsed, source=existing["source"], source_ref=existing.get("source_ref"))
        return row

    row = semantic_model_repo().upsert(
        id=f"manual/_/{slug}",
        slug=slug,
        name=slug,
        description=body.description,
        document=body.document,
        document_json=result.parsed,
        spec_version=result.spec_version,
        content_hash=content_hash,
        source="manual",
        source_ref=None,
        status="valid",
        validation_errors=None,
        validated_at=datetime.now(UTC),
    )
    _project(result.parsed, source="manual", source_ref=None)
    return row


@router.get("/api/admin/semantic-models/{model_id:path}")
async def get_semantic_model(model_id: str, user: dict = Depends(require_admin)):
    row = _resolve_model(model_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Semantic model '{model_id}' not found")
    return row


@router.put("/api/admin/semantic-models/{model_id:path}")
async def update_semantic_model(model_id: str, body: SemanticModelUpdate, user: dict = Depends(require_admin)):
    row = _resolve_model(model_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Semantic model '{model_id}' not found")
    if _is_source_owned(row):
        _raise_source_owned(row)
    # F3: `update_document`, not `upsert` — a detached row's provenance
    # (source/source_ref) and sync_mode/detach-tracking must survive a
    # name/description-only edit unchanged. Harmless on a manual row too
    # (no detach state to preserve there).
    updated = semantic_model_repo().update_document(
        row["id"],
        name=body.name if body.name is not None else row["name"],
        description=body.description if body.description is not None else row["description"],
        document=row["document"],
        document_json=row["document_json"],
        spec_version=row["spec_version"],
        content_hash=row["content_hash"],
        status=row["status"],
        validation_errors=row["validation_errors"],
        validated_at=row["validated_at"],
    )
    # The document itself is unchanged here (this endpoint only touches
    # name/description), so this is normally a no-op re-projection — a
    # safety net that keeps the projected rows in sync should an earlier
    # write ever have failed to project.
    _project(updated["document_json"], source=updated["source"], source_ref=updated["source_ref"])
    return updated


class DetachRequest(BaseModel):
    confirm_detach: bool = False


class ReattachRequest(BaseModel):
    confirm_reattach: bool = False


@router.post("/api/admin/semantic-models/{model_id:path}/detach")
async def detach_semantic_model(model_id: str, body: DetachRequest, user: dict = Depends(require_admin)):
    """F3: danger-flow escape hatch out of the flat ``409 source_owned``
    guard — copies nothing, flips ``sync_mode`` on the SAME row in place.
    From here on, sync tracks drift (``source_content_hash``) instead of
    overwriting; the admin edits freely through ``PUT``/``/apply``."""
    if not use_pg():
        raise RequiresPostgresBackend("semantic_model_detach")
    row = _resolve_model(model_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Semantic model '{model_id}' not found")
    if row["source"] == "manual":
        raise HTTPException(
            status_code=400,
            detail={"code": "not_source_owned", "message": "this model has no source to detach from"},
        )
    if row.get("sync_mode") == "detached":
        # Idempotence guard, not a silent no-op: a second click must not
        # overwrite detached_at/by and lose the original audit record.
        raise HTTPException(status_code=409, detail={"code": "already_detached", "message": "already detached"})
    if not body.confirm_detach:
        raise HTTPException(
            status_code=400,
            detail={"code": "confirm_required", "message": "detach requires confirm_detach=true"},
        )
    updated = semantic_model_repo().detach(row["id"], by=user["email"], base_hash=row["content_hash"])
    audit_repo().log(
        user_id=user.get("id"),
        client_kind=client_kind_from_user(user),
        action="semantic_model.detach",
        resource=row["id"],
        params={"slug": row["slug"]},
    )
    return updated


@router.post("/api/admin/semantic-models/{model_id:path}/reattach")
async def reattach_semantic_model(model_id: str, body: ReattachRequest, user: dict = Depends(require_admin)):
    """F3: return a detached model to the sync path. Without confirmation,
    returns a staleness preview instead of acting — has the source changed
    since detach, and when was it detached. Confirming flips
    ``sync_mode='synced'``; the NEXT sync run is what actually rewrites
    ``document`` back onto the model (this endpoint doesn't fabricate that
    content itself — the importer is the only place that knows how to
    assemble it)."""
    if not use_pg():
        raise RequiresPostgresBackend("semantic_model_detach")
    row = _resolve_model(model_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Semantic model '{model_id}' not found")
    if row.get("sync_mode") != "detached":
        raise HTTPException(status_code=409, detail={"code": "not_detached", "message": "this model is not detached"})
    if row.get("source_missing_since") is not None:
        # The source stopped sending this slug entirely — "return to sync"
        # has nothing to return to. See phase3.md §13 open question 1.
        raise HTTPException(
            status_code=409,
            detail={
                "code": "source_gone",
                "message": "the source no longer has this slug — re-attach would have nothing to sync from",
            },
        )
    if not body.confirm_reattach:
        detached_at = row.get("detached_at")
        source_content_hash = row.get("source_content_hash")
        raise HTTPException(
            status_code=400,
            detail={
                "code": "confirm_required",
                "message": "reattach requires confirm_reattach=true",
                # NULL source_content_hash means no sync has run since
                # detach — "unknown yet", not "changed". A bare `!=` would
                # misreport that as changed (None != "h1" is True in Python).
                "source_changed_since_detach": source_content_hash is not None
                and source_content_hash != row.get("detach_base_hash"),
                # HTTPException.detail goes through a plain json.dumps, not
                # FastAPI's jsonable_encoder — a raw datetime here 500s.
                "detached_at": detached_at.isoformat() if detached_at else None,
            },
        )
    updated = semantic_model_repo().reattach(row["id"])
    audit_repo().log(
        user_id=user.get("id"),
        client_kind=client_kind_from_user(user),
        action="semantic_model.reattach",
        resource=row["id"],
        params={"slug": row["slug"]},
    )
    return updated


@router.post("/api/admin/semantic-models/{slug}/packages", response_model=SemanticModelPackageIds)
async def link_semantic_model_package(
    slug: str,
    body: SemanticModelPackageLink,
    user: dict = Depends(require_admin),
):
    """Link a semantic model to a Data Package, giving it that package's
    visibility to non-admin readers (see the module docstring's "A model
    with no linked package is reachable by admins only").

    Not gated by the ownership rule: the link lives in the
    ``data_package_semantic_models`` junction, not in the model's document
    or its ``semantic_models`` row, so a source re-sync (which only rewrites
    those) can never revert it — the exact concern the ownership rule
    guards against does not apply here. An admin may link ANY model,
    hand-authored or source-owned, the same way a direct ``semantic_model``
    resource grant already applies regardless of a model's ``source``
    (``_can_read_model`` above has no ownership check either).

    Idempotent (``link_package`` itself is delete-then-insert). 404s if
    ``slug`` or ``package_id`` doesn't resolve to an existing row.
    """
    repo = semantic_model_repo()
    model = repo.get_by_slug(slug)
    if model is None:
        raise HTTPException(status_code=404, detail=f"Semantic model '{slug}' not found")
    if not data_packages_repo().get(body.package_id):
        raise HTTPException(status_code=404, detail="data_package_not_found")
    repo.link_package(body.package_id, model["id"])
    return {"package_ids": repo.list_packages_for_model(model["id"])}


@router.delete("/api/admin/semantic-models/{slug}/packages/{package_id}", response_model=SemanticModelPackageIds)
async def unlink_semantic_model_package(
    slug: str,
    package_id: str,
    user: dict = Depends(require_admin),
):
    """Unlink a semantic model from a Data Package.

    Idempotent on an already-unlinked pair or an unknown ``package_id`` —
    the junction delete is a no-op either way, mirroring
    ``remove_table_from_package``'s junction-row semantics
    (``app/api/data_packages.py``). Only ``slug`` 404s.

    Deliberately returns 200 with the remaining ``package_ids`` rather than
    204 (see ``_DELETE_200_WITH_BODY_ALLOWLIST`` in
    ``tests/test_api_design_rules.py``) — symmetric with the linking POST
    above, and the CLI's ``unlink-package`` echoes the remaining packages
    without a follow-up GET.
    """
    repo = semantic_model_repo()
    model = repo.get_by_slug(slug)
    if model is None:
        raise HTTPException(status_code=404, detail=f"Semantic model '{slug}' not found")
    repo.unlink_package(package_id, model["id"])
    return {"package_ids": repo.list_packages_for_model(model["id"])}


@router.delete("/api/admin/semantic-models/{model_id:path}", status_code=204)
async def delete_semantic_model(model_id: str, user: dict = Depends(require_admin)):
    row = _resolve_model(model_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Semantic model '{model_id}' not found")
    if _is_source_owned(row):
        # A scheduled sync would just recreate it — deleting here would
        # either destroy provenance history or fight the next sync tick.
        _raise_source_owned(row)
    # Prune the flat projection (metric_definitions/glossary_terms/
    # column_metadata) BEFORE deleting the document row — the row is the
    # only place that still carries the document once this call returns, and
    # `prune_model` needs it to derive the exact model-id prefix `_project`
    # wrote under. Otherwise a model created/edited through this API (which
    # now projects, see `_project`) would leave those rows orphaned forever.
    if row.get("document_json"):
        prune_model(row["document_json"], source=row["source"], source_ref=row["source_ref"])
    semantic_model_repo().delete(row["id"])


# ---------------------------------------------------------------------------
# Admin: semantic-sources CRUD + sync
#
# Every read AND write response goes through `with_owned_model_count`, so a
# caller that renders any of them into the same table never has to special-
# case a missing field (#1707).
# ---------------------------------------------------------------------------


@router.get("/api/admin/semantic-sources")
async def list_semantic_sources(enabled_only: bool = False, user: dict = Depends(require_admin)):
    """Every registered source, each with the number of semantic models it
    OWNS (``owned_model_count``).

    The count is derived, never stored (see ``src/semantic/ownership.py``):
    ``last_sync_status='ok'`` answers "did the fetch work", not "did it bring
    anything back", so a source scoped at an upstream with nothing in it is
    otherwise indistinguishable from a healthy one (#1707). ``null`` means the
    source's provenance could not be resolved — "cannot say", not "owns none".
    """
    return with_owned_model_count(semantic_source_repo().list_all(enabled_only=enabled_only))


@router.post("/api/admin/semantic-sources", status_code=201)
async def create_semantic_source(body: SemanticSourceCreate, user: dict = Depends(require_admin)):
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="name is required")
    if body.kind not in _VALID_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown kind {body.kind!r} (expected one of {', '.join(_VALID_KINDS)})",
        )
    _assert_known_adapter(body.adapter)
    _assert_no_provenance_override(body.config)
    from uuid import uuid4

    source_id = f"ss_{uuid4().hex[:12]}"
    created = semantic_source_repo().create(
        id=source_id,
        kind=body.kind,
        name=body.name.strip(),
        adapter=body.adapter,
        config=body.config,
        enabled=body.enabled,
    )
    # Same shape as the list row — a caller that renders the POST response
    # straight into the table must not have to special-case a missing field
    # (it is 0 here by definition: nothing has synced yet).
    return with_owned_model_count([created])[0]


@router.get("/api/admin/semantic-sources/{source_id}")
async def get_semantic_source(source_id: str, user: dict = Depends(require_admin)):
    """One source, same shape as the list row — ``owned_model_count`` and all."""
    row = semantic_source_repo().get(source_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Semantic source '{source_id}' not found")
    return with_owned_model_count([row])[0]


@router.put("/api/admin/semantic-sources/{source_id}")
async def update_semantic_source(source_id: str, body: SemanticSourceUpdate, user: dict = Depends(require_admin)):
    repo = semantic_source_repo()
    if repo.get(source_id) is None:
        raise HTTPException(status_code=404, detail=f"Semantic source '{source_id}' not found")
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        return with_owned_model_count([repo.get(source_id)])[0]
    if fields.get("adapter") is not None:
        _assert_known_adapter(fields["adapter"])
    _assert_no_provenance_override(fields.get("config"))
    return with_owned_model_count([repo.update(source_id, **fields)])[0]


@router.delete("/api/admin/semantic-sources/{source_id}", status_code=204)
async def delete_semantic_source(source_id: str, user: dict = Depends(require_admin)):
    if not semantic_source_repo().delete(source_id):
        raise HTTPException(status_code=404, detail=f"Semantic source '{source_id}' not found")


@router.post("/api/admin/semantic-sources/{source_id}/sync")
async def sync_semantic_source(source_id: str, user: dict = Depends(require_admin)):
    row = semantic_source_repo().get(source_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Semantic source '{source_id}' not found")
    # `enabled=False` excludes a source from BOTH the scheduled sweep
    # (app/api/semantic_sources_refresh.py) and this manual escape hatch —
    # an admin who disabled a source expects nothing to touch it until they
    # flip it back on.
    if row.get("enabled") is False:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "source_disabled",
                "hint": (
                    f"Semantic source '{source_id}' is disabled and excluded from sync. "
                    f"Re-enable it first: PUT /api/admin/semantic-sources/{source_id} "
                    '{"enabled": true}'
                ),
            },
        )

    from dataclasses import asdict

    from src.semantic.transports import import_source

    try:
        report = import_source(source_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"sync failed: {exc}") from exc
    return asdict(report)


# ---------------------------------------------------------------------------
# Public: search + export, gated on the linked Data Package's grant
# ---------------------------------------------------------------------------


@router.get("/api/semantic-models/search")
async def search_semantic_models(
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(10, ge=1, le=100),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Case-insensitive substring search over slug/name/description, RBAC
    filtered to models linked to a Data Package the caller can access
    (admins see everything)."""
    needle = q.lower()
    matches: list[dict[str, Any]] = []
    for row in semantic_model_repo().list_all():
        haystack = " ".join(filter(None, [row.get("slug"), row.get("name"), row.get("description")])).lower()
        if needle not in haystack:
            continue
        if not _can_read_model(user, row, conn):
            continue
        matches.append(row)
        if len(matches) >= limit:
            break
    return {"query": q, "models": matches, "count": len(matches)}


@router.get("/api/semantic-models/{slug}.yaml")
async def export_semantic_model(
    slug: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Export the stored document byte-for-byte — never re-serialized, so
    comments and key order survive."""
    row = semantic_model_repo().get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Semantic model '{slug}' not found")
    if not _can_read_model(user, row, conn):
        raise HTTPException(status_code=403, detail=_export_denied_message(slug))
    return Response(content=row["document"], media_type="text/yaml")


# ---------------------------------------------------------------------------
# Public: query validation against the caller's semantic models (parity
# spec §5) — same RBAC tier as search/export (a Data Package or direct
# model grant, not admin-only).
# ---------------------------------------------------------------------------


_NO_MODEL_MESSAGE = (
    "No semantic model is available to validate against. Ask an admin to "
    "import or author one (see docs/semantic-layer.md)."
)


def _accessible_valid_documents(
    user: dict, conn: duckdb.DuckDBPyConnection, model_refs: set[str] | None = None
) -> list[dict[str, Any]]:
    """The individual model dicts (``document_json["semantic_model"]``
    entries) of every ``status='valid'`` semantic-model row ``user`` may
    read.

    ``validate_query`` operates on a list of single-model dicts (its own
    module docstring's shape — ``datasets``/``metrics``/``custom_extensions``
    at the top level), not the stored row's full parsed-YAML wrapper
    (``{"semantic_model": [...]}``, per ``document_validation.py``'s Ossie
    schema) — so this unwraps one level. A stored row's ``semantic_model``
    list is usually one entry, but is flattened in full in case a document
    ever declares more than one model.

    Scoped the same way search/export are (``_can_read_model``): a query may
    span more than one row, so every accessible valid document is handed to
    the validator, which unions its detection across all of them.
    """
    documents: list[dict[str, Any]] = []
    # Case-folded once, not per row (the match below is case-insensitive like
    # object-id matching).
    refs_cf = {str(r).casefold() for r in model_refs} if model_refs is not None else None
    for row in semantic_model_repo().list_all():
        if row.get("status") != "valid" or not row.get("document_json"):
            continue
        if not _can_read_model(user, row, conn):
            continue
        models = row["document_json"].get("semantic_model")
        if not isinstance(models, list):
            continue
        model_dicts = [m for m in models if isinstance(m, dict)]
        if refs_cf is None:
            documents.extend(model_dicts)
            continue
        # `model_refs` restricts to specific models, case-insensitively (like
        # object-id matching, so `--model Retail` works as `--id ORDERS` does).
        # An id (`<source>/<source_ref>/<slug>`) or slug match selects the WHOLE
        # row; a match on a document model NAME narrows to THAT model entry only
        # — a multi-model row must not leak the models the caller didn't ask
        # for, and the `model` label each object carries is that name. Accepting
        # the name at all is what lets that returned label round-trip back into
        # `model_ids` (Devin review on #1398).
        if (str(row.get("id") or "")).casefold() in refs_cf or (str(row.get("slug") or "")).casefold() in refs_cf:
            documents.extend(model_dicts)
            continue
        matched = [m for m in model_dicts if str(m.get("name") or "").casefold() in refs_cf]
        documents.extend(matched)
    return documents


def _accessible_valid_rows(user: dict, conn: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    """Every ``status='valid'`` semantic-model ROW ``user`` may read — same
    ``_can_read_model`` gate as ``_accessible_valid_documents``, but returns
    the full row (``slug``, ``content_hash``, ``source``, ``document_json``,
    …) rather than the unwrapped per-model dict.

    A deliberately simpler sibling of ``_accessible_valid_documents``: it has
    no ``model_refs`` narrowing (nothing here needs the finer-than-row,
    per-document-model-name slice that function's ``model_ids`` restriction
    supports), because both of its callers want "every row the caller may
    read" outright — the pull-bundle endpoint renders one directory per row,
    and the context endpoint's ``model_hashes`` map is a courtesy listing of
    every accessible model's hash, not itself narrowed by a ``model_ids``
    filter on the request.
    """
    rows: list[dict[str, Any]] = []
    for row in semantic_model_repo().list_all():
        if row.get("status") != "valid" or not row.get("document_json"):
            continue
        if not _can_read_model(user, row, conn):
            continue
        rows.append(row)
    return rows


# Object detection is best-effort text matching over declared names, not SQL
# parsing (``src/semantic_validation.py``'s own LIMITATIONS). Said out loud in
# the payload and in every warning line: a column that shares a metric's name
# matches too, so an unqualified warning would present a heuristic hit as a
# confirmed violation.
_DETECTION_NOTE = "best-effort text match"
_DETECTION_NOTE_LONG = (
    "Datasets and metrics were detected by a best-effort text match on their declared names, not by parsing "
    "the SQL — a column or alias that shares a name matches too. Treat this as a prompt to check, not a proof."
)


def semantic_validation_for_query(
    sql: str,
    user: dict,
    conn: duckdb.DuckDBPyConnection,
    *,
    target_engine: str = "duckdb",
) -> dict[str, Any] | None:
    """The soft-enforce advisory for ``sql``, or ``None`` when the semantic
    layer has nothing to say about it.

    Called from the ``POST /api/query`` success path so a caller who never
    asks for validation still hears about a violated constraint. Enforcement
    is SOFT by product decision: this returns an advisory that rides an
    otherwise untouched 200 — it never blocks, never changes a status code,
    and never alters a row.

    "Something to say" is deliberately narrow, because a field that appears
    on every query is a field agents learn to ignore:

    * an **error**-severity constraint violation (a warning-severity one is
      carried along once the advisory exists, but never triggers it on its
      own), or
    * a used metric that is not executable on ``target_engine`` — the number
      the caller just computed is not the declared metric.

    RBAC is the same tier as every other read here (``_can_read_model`` via
    ``_accessible_valid_documents``): a model the caller cannot read cannot
    warn them. The cheap existence check runs FIRST — an instance with no
    valid model at all returns on one ``COUNT(*)``, before a single row's
    document is loaded or a single per-model grant is resolved. That is the
    common case, and it is on the latency path of every query on the
    instance.
    """
    # `count_valid()` is the whole reason this is affordable: the question is
    # "is there a semantic layer at all?", and `list_all()` would answer it by
    # dragging `document` + `document_json` for every row. It over-counts
    # rather than under-counts (see the repo docstring) — an over-count costs
    # the load below, an under-count would silently switch the advisory off.
    if semantic_model_repo().count_valid() == 0:
        return None
    documents = _accessible_valid_documents(user, conn)
    if not documents:
        return None

    result = validate_query(sql, documents, target_engine=target_engine)
    violations = result.get("violations") or []
    blocking = [v for v in violations if v.get("severity") == "error"]
    locally_executable = bool(result.get("locally_executable", True))
    if not blocking and locally_executable:
        return None

    warnings: list[str] = []
    for violation in blocking:
        metrics = ", ".join(str(m) for m in (violation.get("metrics") or [])) or "this query"
        warnings.append(
            f"constraint '{violation.get('name')}' on {metrics} ({_DETECTION_NOTE}): {violation.get('reason')}"
        )
    if not locally_executable:
        # Name the metrics that are actually unexecutable, never every metric
        # the statement mentioned: `revenue` composing fine is not something
        # to warn about because `margin` next to it does not. The fallback
        # keeps the sentence honest if the validator ever reports the flag
        # without the names.
        offenders = result.get("not_executable_metrics") or result.get("used_metrics") or []
        used = ", ".join(str(m) for m in offenders) or "a used metric"
        warnings.append(
            f"{used} ({_DETECTION_NOTE}): no expression declared for {target_engine} — this result is not the "
            "declared metric, check `agnes semantic-model context metric` before reporting it"
        )
    return {
        "valid": result.get("valid", True),
        "warnings": warnings,
        # How the objects above were detected. Named in the payload AND in
        # every warning line, because the CLI prints only the warnings: a
        # column that happens to share a metric's name matches too, and
        # without this a heuristic hit reads as a confirmed violation.
        "detection": _DETECTION_NOTE_LONG,
        # The raw engine output for the two findings above, so a UI can render
        # more than the prose line. `violations` carries EVERY violation once
        # the advisory exists (see the docstring) — hiding the advisory-
        # severity ones next to a blocking one would misreport the total.
        "violations": violations,
        # Rules that cannot be checked before running (issue #1707 decision 7:
        # allowed, not validated, surfaced as information). Forwarded, NEVER
        # evaluated — this caller has the rows but evaluating a business rule
        # over them is a different feature with a different failure mode, and
        # a guessed verdict is exactly what the validator refuses to produce.
        # They never raise the advisory on their own either (see the trigger
        # above); they only ride one that already exists.
        "post_execution_checks": result.get("post_execution_checks") or [],
        "locally_executable": locally_executable,
        "not_executable_metrics": result.get("not_executable_metrics") or [],
        "used_metrics": result.get("used_metrics") or [],
        "used_datasets": result.get("used_datasets") or [],
        "summary": result.get("summary", ""),
    }


@router.post("/api/semantic-models/validate-query")
async def validate_semantic_query(
    body: SemanticQueryValidate,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Validate a SQL statement against the caller's accessible semantic
    models: constraint violations, dialect fit, and (optionally) which
    expected datasets/metrics/relationships it hits.

    Wraps the pure ``src.semantic_validation.validate_query`` — best-effort
    text matching against declared document content, not SQL parsing; see
    that module's own LIMITATIONS. Gated fail-closed: when the caller has no
    accessible ``status='valid'`` model, this returns ``{"available": False,
    ...}`` rather than the engine's all-clear default for an empty document
    list, which would otherwise read as a false "valid: true".
    """
    documents = _accessible_valid_documents(user, conn)
    if not documents:
        return {"available": False, "error": "no_semantic_model", "message": _NO_MODEL_MESSAGE}
    result = validate_query(body.sql, documents, expected=body.expected, target_engine=body.target_engine)
    result["available"] = True
    return result


# ---------------------------------------------------------------------------
# Public: agent read-parity tools (parity spec §4/§5) — get_semantic_context
# and get_semantic_schema. Same RBAC tier as search/export/validate-query (a
# Data Package or direct model grant, not admin-only): read tier, analysts
# and agents are the audience.
# ---------------------------------------------------------------------------


@router.get("/api/semantic-models/context")
async def get_semantic_context_endpoint(
    selections: str = Query(
        ...,
        description=(
            'JSON list of {"semantic_type": "dataset"|"metric"|"relationship", "ids": [...]?} objects. '
            "Absent/empty ids returns every object of that type compactly; explicit ids return full attributes."
        ),
    ),
    model_ids: list[str] | None = Query(
        None,
        description=(
            "Restrict to these models by id, slug, or model name (the `model` label each object "
            "carries; repeatable, case-insensitive); default = every accessible model."
        ),
    ),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Typed context lookup over the caller's accessible semantic models —
    the ``get_semantic_context`` parity tool.

    Wraps the pure ``src.semantic_context.get_semantic_context`` over the
    same ``_accessible_valid_documents`` RBAC tier as search/export/
    validate-query. An empty result (no accessible model, or no object of
    the requested type/id) is not an error — this endpoint has no
    misleading "all clear" to gate against, unlike ``validate-query``.

    The response also carries ``model_hashes`` — ``{slug: content_hash}``
    for every accessible model (not narrowed by ``model_ids``, unlike
    ``results``) — so a caller re-verifying an expired local semantic cache
    (Fáze 1 physical distribution, ``config/claude_md_template.txt``'s TTL
    policy) can compare the cache file's own header ``content_hash`` against
    the live value without a second round trip.
    """
    try:
        parsed_selections = json.loads(selections)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"selections is not valid JSON: {exc}") from exc
    if not isinstance(parsed_selections, list):
        raise HTTPException(status_code=400, detail="selections must be a JSON list of {semantic_type, ids?} objects")

    documents = _accessible_valid_documents(user, conn, model_refs=set(model_ids) if model_ids else None)
    result = _get_semantic_context(documents, parsed_selections)
    result["model_hashes"] = {
        row["slug"]: row.get("content_hash") for row in _accessible_valid_rows(user, conn) if row.get("slug")
    }
    return result


@router.get("/api/semantic-models/schema")
async def get_semantic_schema_endpoint(
    semantic_types: list[str] = Query(
        ..., description="Object types to describe: dataset, metric, relationship (repeatable)."
    ),
    user: dict = Depends(get_current_user),
):
    """The vendored Apache Ossie JSON Schema for the requested object types —
    the ``get_semantic_schema`` parity tool.

    Not RBAC-gated on any model (there is nothing model-specific to hide —
    it reflects the schema every model is validated against), only on
    ``get_current_user`` — any authenticated user, same floor as the rest of
    this read surface. Served straight from
    ``src.semantic.document_validation``'s vendored, pinned schema, never a
    hand-written copy.
    """
    del user  # authentication-only dependency — nothing model-specific to gate on
    return _get_semantic_schema(semantic_types)


@router.get("/api/semantic-models/bundle")
async def semantic_models_bundle(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """RBAC-scoped bundle of every accessible ``status='valid'`` semantic
    model, consumed by ``agnes pull`` to render the read-only local cache
    under ``<workspace>/semantic/<slug>/…`` (Fáze 1 — "distribuce jako
    fyzická cache s TTL"; ``src/semantic/cache_render.py`` does the actual
    rendering, client-side, from this response).

    Same delivery-channel shape and same non-interactive posture as
    ``/api/memory/bundle`` and ``/api/knowledge/digests/{digest_id}/
    content`` — one GET the CLI calls on every pull, never an agent tool
    (see the triple-surface ``_EXEMPT`` entry). Same RBAC tier as search/
    export/context (``_can_read_model``): admin, a grant on the model
    itself, or a grant on a Data Package it's linked to.

    Each model entry carries its own ``content_hash`` (the same
    ``semantic_models.content_hash`` every other surface reads — never
    recomputed) and the full ``document_json`` the renderer needs; the
    top-level ``ttl_seconds`` is the value the CLI stamps into every
    rendered file's header and the TTL an agent's local-cache trust policy
    (``config/claude_md_template.txt``) is written against.
    """
    rows = _accessible_valid_rows(user, conn)
    return {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ttl_seconds": DEFAULT_TTL_SECONDS,
        "models": [
            {
                "id": row.get("id"),
                "slug": row.get("slug"),
                "name": row.get("name"),
                "description": row.get("description"),
                "source": row.get("source"),
                "source_ref": row.get("source_ref"),
                "content_hash": row.get("content_hash"),
                "document_json": row.get("document_json"),
            }
            for row in rows
        ],
    }


@router.post("/api/semantic-models/apply")
async def apply_semantic_model_endpoint(
    body: SemanticModelApply,
    user: dict = Depends(get_current_user),
):
    """Apply a hand-authored Ossie document — the one semantic-layer write
    surface for chat, CLI, and the studio builder.

    What "apply" means depends on the caller's authority, and the response
    labels the outcome so callers (human or agent) never have to guess:

    - **admin** → the document is validated, stored as a ``source='manual'``
      model (create-or-replace by slug), and projected into the flat tables.
      Response: ``{"outcome": "applied", "model": {...}}``.
    - **non-admin** → the document is validated, then queued as an
      ``authoring_suggestions`` row (domain ``semantic-layer``) for admin
      moderation — it never touches ``semantic_models`` before approval.
      Response: ``{"outcome": "submitted_for_review", "suggestion_id": ...}``.

    Shared guards, both roles: schema-invalid documents 422; a slug owned by
    an imported source 409 ``source_owned``; a supplied
    ``expected_content_hash`` that no longer matches 409 ``stale_document``.
    The non-admin branch additionally 409s ``duplicate_pending`` while an
    earlier proposal for the same slug awaits review, and 403s
    ``studio_disabled`` when the instance-level Studio toggle is off (the
    admin branch is a plain admin write, not Studio-gated).
    """
    from app.auth.access import is_user_admin
    from src.repositories import authoring_suggestions_repo

    try:
        slug, _result = _check_apply(body.document, body.expected_content_hash)
    except SemanticApplyError as exc:
        raise _apply_error_to_http(exc) from None

    if is_user_admin(user["id"]):
        try:
            row = apply_manual_model(body.document, body.description, body.expected_content_hash)
        except SemanticApplyError as exc:  # raced with a concurrent write between check and apply
            raise _apply_error_to_http(exc) from None
        return {"outcome": "applied", "model": row}

    if not get_studio_enabled():
        raise HTTPException(status_code=403, detail={"kind": "studio_disabled"})

    repo = authoring_suggestions_repo()
    # One pending proposal per slug: a second submission while the first
    # awaits review points at the existing one instead of stacking dupes.
    # (Suggestions submitted through the raw studio-page POST carry no
    # ``slug`` key and are invisible to this guard — a courtesy check, not
    # an invariant; the replay handles any residual collision by upserting.)
    for sug in repo.list(status="pending", domain="semantic-layer"):
        if (sug.get("payload") or {}).get("slug") == slug:
            raise HTTPException(
                status_code=409,
                detail={"kind": "duplicate_pending", "suggestion_id": sug["id"]},
            )

    payload = {
        "slug": slug,
        "document": body.document,
        "description": body.description,
        "expected_content_hash": body.expected_content_hash,
    }
    sid = repo.create(domain="semantic-layer", payload=payload, created_by=user["email"])
    audit_repo().log(
        user_id=user["id"],
        action="authoring_suggestion.submit",
        resource=sid,
        params={"domain": "semantic-layer", "slug": slug},
    )
    return {"outcome": "submitted_for_review", "suggestion_id": sid, "slug": slug}
