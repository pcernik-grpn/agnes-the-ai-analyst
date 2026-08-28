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
            sources.append(
                {
                    "id": entry.id,
                    "kind": "data",
                    "name": entry.name,
                    "description": entry.description or "",
                    "meta": f"{tables} table{'' if tables == 1 else 's'}",
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
        # TODO: the per-collection file count below is an N+1 (one query per
        # reachable collection, so per *every* collection for an admin). A bulk
        # `counts_by_corpus()` on both corpus_files backends would collapse it
        # into one query.
        for col in file_corpora_repo().list_all():
            if allowed is not None and col["id"] not in allowed:
                continue
            try:
                fcount = len(cf_repo.list_for_corpus(col["id"]))
            except Exception:
                fcount = 0
            sources.append(
                {
                    "id": col["id"],
                    "kind": "file",
                    "name": col.get("name") or col.get("slug"),
                    "description": col.get("description") or "",
                    "meta": f"{fcount} file{'' if fcount == 1 else 's'}",
                }
            )
    except Exception as e:
        logger.warning("agent ingredients: could not resolve artefacts: %s", e)

    return sources
