"""Unified knowledge search endpoint (K2, #797).

REST surface for ``src.search.unified.unified_search``: resolves the caller's
grant sets fail-closed (collection grants, memory-domain grants + audience
groups, ``can_access_table``) and fans the query out over Collections chunks,
knowledge items, and table catalog cards. See the module docstring in
``src/search/unified.py`` for merge semantics.

Grant resolution goes through the repository factories (``is_user_admin``,
``user_group_members_repo``, ``resource_grants_repo``) rather than
``app.api.memory``'s raw-conn helpers, so the endpoint filters correctly on
the Postgres backend too — the raw ``Depends(_get_db)`` connection reads
empty state tables there (the backend-split bug class).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response

from app.auth.access import can_access, can_access_session, is_user_admin, require_resource_access
from app.auth.dependencies import _get_db, get_current_user
from app.auth.session_principal import PRINCIPAL_TYPES
from app.resource_types import ResourceType
from src.audit_helpers import client_kind_from_user
from src.rbac import get_accessible_tables
from src.repositories import audit_repo, resource_grants_repo, table_registry_repo, user_group_members_repo
from src.search.unified import unified_search

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


def _resolve_knowledge_grants(user) -> Tuple[Optional[List[str]], Optional[List[str]]]:
    """(user_groups, granted_domains) for the knowledge source, factory-routed.

    Semantics mirror ``app.api.memory._effective_groups`` /
    ``_caller_granted_memory_domains``: ``None``/``None`` for privileged
    viewers (no filter), ``group:<name>`` audience tokens + granted
    ``memory_domains.id`` values otherwise, ``[]``/``[]`` fail-closed for a
    caller with no memberships. A restricted principal (co-session or
    agent-session) never gets admin god-mode — its domain set comes from the
    live intersection.
    """
    if isinstance(user, PRINCIPAL_TYPES):
        return [], list(user.intersection.get(ResourceType.MEMORY_DOMAIN.value, frozenset()))
    user_id = user.get("id")
    if not user_id:
        return [], []
    if is_user_admin(user_id):
        return None, None
    memberships = user_group_members_repo().list_groups_with_meta_for_user(user_id)
    groups = [f"group:{m['name']}" for m in memberships]
    group_ids = [m["group_id"] for m in memberships]
    grants = resource_grants_repo().list_for_groups(group_ids, resource_type="memory_domain") if group_ids else []
    domains = sorted({g["resource_id"] for g in grants})
    return groups, domains


def _empty_combined_hint(collections: int, tables: int, metrics: int) -> str:
    """Why the combined search is empty, in terms the caller can act on.

    The collections-only sibling (``app.api.collections._empty_search_hint``)
    can read ``searched == 0`` as "you need a grant". Here it cannot: this leg
    fans out over collections, corporate memory, the table catalog, metrics and
    the glossary, so an empty *collection* set says nothing on its own — a
    caller with no collections but forty tables was searched, and telling them
    to ask for a grant would send them to the wrong person.

    "Nothing was searched" is never literally true either: the **glossary** has
    no RBAC (``unified_search`` fetches it for any authenticated caller), so
    something always ran. The all-empty branch therefore says which sources
    were empty rather than claiming the search did not happen.

    The judgement rests only on the three **countable** sources, and the branch
    is worded to fit both audiences it can reach: an ungranted user and an
    admin of an instance with nothing loaded yet. For an admin the counts are
    the whole instance, so "reachable from this account" is literally true of
    both, where "shared with you" would have told an admin to ask themselves. An attempt to
    fold the knowledge leg in as a fourth term made it worse, not better: the
    only signal available here is the caller's group membership, and nearly
    every account is auto-added to the built-in "Everyone" group — so the term
    was true for essentially everyone and the branch below became unreachable,
    telling a brand-new user with nothing shared at all to try different words.
    Knowledge notes and the glossary are instead **named** in both branches, so
    neither over-claims about a leg this endpoint cannot count.
    (Devin Review on this PR, twice.)
    """
    if collections == 0 and tables == 0 and metrics == 0:
        return (
            "No documents, tables or metrics are reachable from this account yet. Company "
            "knowledge notes and the public glossary were searched and nothing matched there "
            "either. If you expected a file or a table to be here, this is about what has been "
            "shared or loaded — not about your wording: call collections_list / catalog to see "
            "what is there, and ask an admin for a grant if it should be."
        )
    # Knowledge notes and the glossary ran too; these two counts are simply the
    # ones a caller can check against `collections_list` / `catalog`.
    scope = f"{collections} collection(s) and {tables} table(s), plus knowledge notes and the glossary,"
    return (
        f"Searched {scope} and found no match. Everything shared with this account "
        "was searched, so an empty result is NOT evidence that access is missing — "
        "far more often it is the wording. (This cannot speak for anything that "
        "has not been shared with you: a term naming that will not match here "
        "either.) "
        "Note: matching is whole word (`test` will not find `Testovaci`) "
        "and there is no wildcard (`*` and an empty query return nothing). File names are "
        "searched too, as a fallback when no document body matches — so nothing here matched "
        "either. Try a distinctive word you expect inside the document, or call "
        "collections_list / catalog to see what is there."
    )


@router.get("/search")
async def knowledge_search(
    q: str = Query(..., min_length=1, description="Search query"),
    k: int = Query(10, ge=1, le=50),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """One query across documents, the knowledge base, and the table catalog.

    Results are typed (``chunk | knowledge | table | metric | glossary``); table hits carry a
    pivot hint (query via SQL) instead of rows. Everything is filtered to the
    caller's grants, fail-closed per source.

    ``retrieval`` (``hybrid | lexical_only``) labels the chunk engine's mode:
    ``lexical_only`` means the embeddings extra is absent and document chunks
    were ranked without semantic scoring (#898). Knowledge and table hits are
    lexical by design and unaffected.

    An empty result carries ``searched_collections``, ``searched_tables`` and a
    ``hint``, for the same reason ``/api/collections/search`` does: a bare
    ``[]`` cannot be told apart from "you may see nothing", and an agent handed
    that ambiguity picks the scarier reading and reports an access problem the
    caller does not have. This is the *combined* leg, so the counts are what
    make the difference checkable — an all-empty fan-out is a grant question,
    anything reachable is a wording one. The glossary is excluded from that
    judgement on purpose: it has no RBAC, so it runs for everyone and its
    presence would make every caller look like they had access.
    """
    from app.api.collections import _accessible_corpus_ids
    from src.ingest.retrieval import retrieval_mode

    corpus_ids = _accessible_corpus_ids(user)
    groups, domains = _resolve_knowledge_grants(user)
    # Resolve the caller's accessible table-id set ONCE per request instead of
    # calling `can_access_table` per row (FAI-132 N+1 collapse: ~115 stack
    # resolutions -> 1). `None` means admin/all, mirroring `can_access_table`.
    _accessible_ids = get_accessible_tables(user, conn)
    allowed = None if _accessible_ids is None else set(_accessible_ids)
    tables = [t for t in table_registry_repo().list_all() if allowed is None or t["id"] in allowed]

    # Metrics are RBAC-gated by table access (#953): a metric is visible only if
    # every table it references is accessible. We already have the full
    # accessible-table list in `tables` — build a name-set from it so the
    # per-metric check is an O(1) dict lookup instead of one get_by_name DB
    # call per referenced table name (avoids N×M DB hits on every keystroke for
    # non-admin callers). Admins short-circuit via allowed=None as before.
    from app.api.metrics import _metric_table_names, plain_description
    from src.repositories import metric_repo

    if allowed is None:
        metrics = metric_repo().list()
    else:
        _accessible_names: set[str] = {t["name"] for t in tables}
        metrics = [m for m in metric_repo().list() if all(n in _accessible_names for n in _metric_table_names(m))]

    # Flatten the description before it reaches the searcher. A metric hit's
    # `description` is projected straight into the result (src/search/unified.py)
    # and read by an agent through the MCP `search` tool, and the same column
    # can hold HTML imported verbatim from an external catalog. Done here rather
    # than at the projection site because these rows are caller-supplied anyway,
    # and flattening before scoring stops tag names being tokenized as search
    # terms. Glossary rows are fetched inside unified_search, so they are
    # flattened there instead.
    metrics = [{**m, "description": plain_description(m)} for m in metrics]

    results = unified_search(
        q,
        corpus_ids=corpus_ids,
        user_groups=groups,
        granted_domains=domains,
        tables=tables,
        metrics=metrics,
        k=k,
    )
    payload: dict = {"query": q, "results": results, "retrieval": retrieval_mode()}
    if not results:
        payload["searched_collections"] = len(corpus_ids)
        payload["searched_tables"] = len(tables)
        payload["hint"] = _empty_combined_hint(len(corpus_ids), len(tables), len(metrics))
    return payload


@router.get("/artifacts/{corpus_id}/download")
async def download_knowledge_artifact(
    corpus_id: str,
    request: Request,
    user=Depends(require_resource_access(ResourceType.COLLECTION, "{corpus_id}")),
):
    """Stream a per-collection knowledge.duckdb artifact (K3, #798).

    Consumed by ``agnes pull``; the PAT is the only credential. RBAC =
    collection grants via ``require_resource_access``. ETag mirrors
    ``/api/data/{table_id}/download``.
    """
    from src.knowledge_packaging import artifacts_dir

    path = artifacts_dir() / f"{corpus_id}.duckdb"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Artifact not built yet")

    stat = path.stat()
    etag = f'"{stat.st_mtime_ns}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304)

    try:
        audit_repo().log(
            user_id=user.get("id") if isinstance(user, dict) else None,
            action="knowledge.artifact_download",
            resource=f"collection:{corpus_id}"[:256],
            params={"bytes": stat.st_size},
            result="success",
            client_kind=client_kind_from_user(user) if isinstance(user, dict) else "web",
        )
    except Exception:
        logger.exception("audit_log write failed for knowledge.artifact_download; continuing")

    return FileResponse(
        path=path,
        filename=f"{corpus_id}.duckdb",
        media_type="application/octet-stream",
        headers={"ETag": etag},
    )


def _caller_can_read_digest(user, digest_id: str) -> bool:
    """Fail-closed ``ResourceType.KNOWLEDGE_DIGEST`` grant check (K4, #799).

    Shared by the manifest section builder
    (``app.api.sync._build_knowledge_artifacts_section``, lazy-imported to
    avoid a cycle) and this module — it's the same ``can_access`` /
    ``can_access_session`` predicate that
    ``require_resource_access(ResourceType.KNOWLEDGE_DIGEST, ...)`` gates the
    content endpoint below with, exposed as a plain boolean so the manifest
    builder can filter a whole list without raising per row. Admin
    short-circuits via ``can_access``; ``SessionPrincipal`` co-session
    callers route through ``can_access_session`` instead — the
    ``_accessible_corpus_ids`` idiom (``app/api/collections.py``).
    """
    if isinstance(user, PRINCIPAL_TYPES):
        return can_access_session(user, ResourceType.KNOWLEDGE_DIGEST.value, digest_id)
    user_id = user.get("id") if isinstance(user, dict) else None
    if not user_id:
        return False
    return can_access(user_id, ResourceType.KNOWLEDGE_DIGEST.value, digest_id)


@router.get("/digests")
async def list_knowledge_digests_for_caller(user=Depends(get_current_user)) -> Dict[str, Any]:
    """The maintained digests THIS caller can read (K4, #799).

    The one missing piece of the analyst-facing digest surface. A digest's
    markdown has been readable at ``/digests/{id}/content`` since K4, and
    ``agnes pull`` already writes every granted one to
    ``.claude/rules/ka_<slug>.md`` — but nothing could ENUMERATE them, so a
    web surface had no way to show a reader which digests exist without
    already knowing an id. The Library's distillate (TCRD-250) needs exactly
    that list.

    Filtered with ``_caller_can_read_digest`` — the same fail-closed
    predicate the manifest builder uses (``app.api.sync._digest_entries``),
    so the web list and the pulled files can never disagree about what a
    caller is entitled to. A digest that has never generated (``pending``,
    empty ``output_md``) is omitted, matching the manifest: there is nothing
    to read, and listing it would promise a page that 404s.

    Staleness travels per row rather than being hidden, same contract as the
    content endpoint — a stale digest still ships its last-good markdown, so
    a reader must be able to see that it is stale.

    Response: ``{"digests": [{"id", "slug", "title", "status",
    "status_reason", "generated_at"}]}``, sorted by slug.
    """
    from src.repositories import knowledge_digests_repo

    out = []
    for d in knowledge_digests_repo().list():
        if not (d.get("output_md") or "").strip():
            continue
        if not _caller_can_read_digest(user, d["id"]):
            continue
        generated_at = d.get("generated_at")
        out.append(
            {
                "id": d["id"],
                "slug": d["slug"],
                "title": d["title"],
                "status": d.get("status") or "pending",
                "status_reason": d.get("status_reason"),
                "generated_at": generated_at.isoformat() if generated_at else None,
            }
        )
    return {"digests": sorted(out, key=lambda e: e["slug"])}


@router.get("/digests/{digest_id}/content")
async def get_knowledge_digest_content(
    digest_id: str,
    user=Depends(require_resource_access(ResourceType.KNOWLEDGE_DIGEST, "{digest_id}")),
):
    """Serve one maintained digest's markdown (K4, #799).

    Consumed by ``agnes pull`` (writes ``.claude/rules/ka_<slug>.md``); the
    PAT is the only credential. RBAC gate is
    ``require_resource_access(ResourceType.KNOWLEDGE_DIGEST, ...)`` — the
    same house style the sibling ``download_knowledge_artifact`` endpoint
    above uses, so an ungranted caller on a real digest id sees **403**
    (matching ``test_download_ungranted_analyst_403``'s posture; supersedes
    the K4 plan's original 404-only guess — see the PR description). Unknown
    id or a digest that has never generated (``pending``, empty
    ``output_md``) is **404** for an admin, who always clears the gate — no
    existence leak beyond "granted but nothing here", the same posture as
    ``test_download_granted_corpus_no_artifact_built_404``. Staleness
    (status + reason) travels in the body so the client can render a
    visible banner — never a silent stale digest.
    """
    from src.repositories import knowledge_digests_repo

    d = knowledge_digests_repo().get(digest_id)
    if d is None or not (d.get("output_md") or "").strip():
        raise HTTPException(status_code=404, detail="Digest not found")

    try:
        audit_repo().log(
            user_id=user.get("id") if isinstance(user, dict) else None,
            action="knowledge.digest_download",
            resource=f"knowledge_digest:{digest_id}"[:256],
            params={"slug": d.get("slug")},
            result="success",
            client_kind=client_kind_from_user(user) if isinstance(user, dict) else "web",
        )
    except Exception:
        logger.exception("audit_log write failed for knowledge.digest_download; continuing")

    generated_at = d.get("generated_at")
    return {
        "id": d["id"],
        "slug": d["slug"],
        "title": d["title"],
        "output_md": d["output_md"],
        "status": d.get("status") or "pending",
        "status_reason": d.get("status_reason"),
        "generated_at": generated_at.isoformat() if generated_at else None,
    }
