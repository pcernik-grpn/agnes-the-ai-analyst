"""Agent-builder ingredients — the RBAC-scoped lists a builder may draw from.

The ``/agents`` page renders its Knowledge picker from these rows, and the
builder assistant (``app/api/agent_builder.py``) is shown the same list as
its candidate set. One function so the two can never disagree: an assistant
that offered a data package the picker doesn't list would be proposing
access the owner cannot actually grant.

Every entry is resolved through the caller's own stack / grants, so the list
is already narrowed to what this user reaches. That is a *usability*
boundary, not the security one — an agent's real authority is
``owner grants ∩ agent scope`` recomputed live per request
(``src/agent_scope_intersection.py``), so a stale or wrong id in an agent's
declared scope conveys nothing on its own.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def policy_disclosure_for_knowledge(item_ids: List[str], owner: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Tables reachable through ``item_ids`` (an agent's declared/candidate
    ``knowledge`` ids) that carry an access policy, each diagnosed for
    ``owner`` — the same self-audit machinery ``/api/me/effective-access``
    already trusts (§10.2), reused here for §12: an agent surface bound to
    the OWNER's identity — a Slack channel bound to the agent, a scheduled
    run — answers with the OWNER's slice regardless of who is actually
    asking, so the owner must see that slice on their own agent's builder
    page before it ever reaches either surface. A direct caller
    (agent-as-API, chat, a delegated turn) is filtered by ITS OWN identity
    instead (``src/access_policy.py::_resolve_identity``) and is unaffected
    by what this reports.

    Only ``data_package`` ids expand to member tables today — the
    ``knowledge`` axis never carries a bare table id (the builder's Data &
    resources section offers packages, memory domains and file collections,
    never a raw table — see ``agents_builder_shared._KNOWLEDGE_ITEM_TYPES``).
    A memory-domain or collection id simply resolves to zero member tables
    (``list_tables`` returns an empty join), so passing every ``knowledge``
    id through unconditionally is correct and needs no extra branching.

    Each output row is ``{table_id, name, access_policy: True, policy: {...}}``
    — only policied tables appear, matching the "always applies=True" shape
    of :class:`app.api.access.TablePolicyDiagnosis` at the entry level (an
    unpolicied table is simply absent rather than reported with
    ``access_policy=False``).
    """
    if not item_ids:
        return []

    from app.api.access import table_policy_diagnosis
    from src.repositories import data_packages_repo, table_registry_repo

    pkg_repo = data_packages_repo()
    tr_repo = table_registry_repo()
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for raw_id in item_ids or []:
        item_id = (raw_id or "").strip()
        if not item_id:
            continue
        try:
            member_tables = pkg_repo.list_tables(item_id)
        except Exception as e:
            logger.warning("agent ingredients: could not list tables for %s: %s", item_id, e)
            continue
        for member in member_tables:
            table_id = member["id"]
            if table_id in seen:
                continue
            seen.add(table_id)
            try:
                row = tr_repo.get(table_id)
            except Exception as e:
                logger.warning("agent ingredients: could not load table %s: %s", table_id, e)
                continue
            if not row or not row.get("access_policy_sql"):
                continue
            out.append(
                {
                    "table_id": table_id,
                    "name": row.get("name") or table_id,
                    "access_policy": True,
                    "policy": table_policy_diagnosis(row, owner),
                }
            )
    return out


def knowledge_sources_for(user: dict) -> List[Dict[str, Any]]:
    """Data packages, memory domains and artefact collections ``user`` reaches.

    Each row is ``{id, kind, name, description, meta}`` where ``kind`` is one
    of ``data`` / ``memory`` / ``file``. Every section is independently
    wrapped: one unreachable subsystem degrades that section to empty rather
    than failing the whole builder.
    """
    from app.auth.access import accessible_collection_ids
    from app.resource_types import ResourceType
    from app.services.stack_resolver import StackResolver
    from src.repositories import (
        corpus_files_repo,
        data_packages_repo,
        file_corpora_repo,
        memory_domains_repo,
    )

    resolver = StackResolver()
    sources: List[Dict[str, Any]] = []

    try:
        pkg_repo = data_packages_repo()
        for entry in resolver.stack(user["id"], ResourceType.DATA_PACKAGE):
            try:
                tables = len(pkg_repo.list_tables(entry.id))
            except Exception:
                tables = 0
            # Design doc §12 — a candidate the picker offers ("selectable
            # for" an agent's scope) is shown with the SAME warning an
            # already-attached one would carry, computed for the caller
            # (who, on this page, is always the agent's OWNER — only an
            # owner may edit their own agent's builder): the owner should
            # see the caveat before grounding the agent in the package, not
            # only after.
            policied_tables = policy_disclosure_for_knowledge([entry.id], user)
            sources.append(
                {
                    "id": entry.id,
                    "kind": "data",
                    "name": entry.name,
                    "description": entry.description or "",
                    "meta": f"{tables} table{'' if tables == 1 else 's'}",
                    "access_policy": bool(policied_tables),
                    "policied_tables": policied_tables,
                }
            )
    except Exception as e:
        logger.warning("agent ingredients: could not resolve data stack: %s", e)

    try:
        domains_repo = memory_domains_repo()
        for entry in resolver.stack(user["id"], ResourceType.MEMORY_DOMAIN):
            try:
                items_count = len(domains_repo.list_items_of_domain(entry.id, limit=10000))
            except Exception:
                items_count = 0
            sources.append(
                {
                    "id": entry.id,
                    "kind": "memory",
                    "name": entry.name,
                    "description": entry.description or "",
                    "meta": f"{items_count} item{'' if items_count == 1 else 's'}",
                }
            )
    except Exception as e:
        logger.warning("agent ingredients: could not resolve memory stack: %s", e)

    # Artefacts (file collections) the caller can reach — owned ∪ shared with a
    # group they belong to (admin → all). These are a third knowledge kind the
    # agent can be grounded in, alongside governed data + memory. Same access
    # resolution the /artefacts page uses, so the builder never offers a file
    # the caller can't actually open.
    try:
        allowed = accessible_collection_ids(user)  # None => admin sees all
        cf_repo = corpus_files_repo()
        # One grouped count for every corpus at once. The per-collection
        # `len(list_for_corpus(...))` this replaces was an N+1 that also read
        # every ROW of every collection to arrive at a number — on an instance
        # where each chat file-drop is its own one-file collection, that is the
        # common case rather than the pathological one. A corpus with no files
        # is simply absent from the mapping, hence the `.get(..., 0)`.
        # `None` means "the count could not be read", which is NOT the same as
        # zero. Swallowing the failure into an empty mapping labelled every
        # reachable collection "0 files" — so one transient repository error
        # made a populated Library look empty, and read as an access problem
        # rather than a hiccup (Devin Review on #2062).
        try:
            file_counts = cf_repo.count_by_corpus()
        except Exception as exc:  # noqa: BLE001 — a count is metadata, never the listing
            logger.warning("agent ingredients: could not count collection files: %s", exc)
            file_counts = None
        for col in file_corpora_repo().list_all():
            if allowed is not None and col["id"] not in allowed:
                continue
            fcount = None if file_counts is None else file_counts.get(col["id"], 0)
            sources.append(
                {
                    "id": col["id"],
                    "kind": "file",
                    "name": col.get("name") or col.get("slug"),
                    "description": col.get("description") or "",
                    "meta": (
                        "file count unavailable"
                        if fcount is None
                        else f"{fcount} file{'' if fcount == 1 else 's'}"
                    ),
                }
            )
    except Exception as e:
        logger.warning("agent ingredients: could not resolve artefacts: %s", e)

    return sources
