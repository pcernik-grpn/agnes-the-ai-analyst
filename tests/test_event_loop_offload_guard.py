"""Event-loop offload guard — pins the Tier 1 async→plain-def conversions.

Agnes runs as a single-process, single-event-loop uvicorn server. FastAPI runs
a plain ``def`` route handler / dependency in the anyio thread pool, but an
``async def`` one runs directly on the event loop. The auth/RBAC dependencies
below execute SYNCHRONOUS, blocking system-DB reads (on Postgres via a sync
SQLAlchemy engine + psycopg3). They run on nearly every request, so if any were
``async def`` a single slow read would freeze the whole process — the reverse
proxy cuts the request → 503 "system unavailable".

This ratchet asserts each stays a plain ``def`` (``inspect.iscoroutinefunction``
is False), so a future edit that reintroduces ``async def`` fails loudly here
instead of silently regressing latency in production. See PR #188's Tier 1
event-loop unblocking rollout for the convention.
"""

from __future__ import annotations

import inspect

import pytest

from app.api.admin_extraction import (
    extraction_config,
    extraction_run_detail,
    extraction_runs,
    extraction_status,
    fleet_extraction_runs,
    patch_extraction_crawl_config,
    patch_extraction_facts_config,
    preview_anonymization,
    request_extraction_stop,
)
from app.api.admin_sharepoint import facts_graph_counts
from app.api.admin_source_connections import list_connections
from app.api.broker import require_broker_ticket
from app.api.catalog import (
    get_metric,
    get_table_profile,
    list_catalog_tables,
    refresh_profile,
)
from app.api.cowork_bundle import (
    exchange_setup_token,
    generate_bundle,
    list_setup_tokens,
    revoke_setup_token,
)
from app.api.claude_md import (
    admin_get_workspace_template,
    admin_preview_workspace_template,
    admin_put_workspace_template,
    admin_reset_workspace_template,
    get_welcome,
)
from app.api.sync import (
    get_sync_settings,
    get_table_subscriptions,
    pull_confirm,
    sync_manifest,
    sync_status,
    trigger_sync,
    update_sync_settings,
    update_table_subscriptions,
)
from app.auth.access import require_admin, require_resource_access
from app.auth.dependencies import (
    get_current_user,
    get_optional_user,
    require_session_token,
)
from app.marketplace_server.router import (
    cowork_plugin_zip,
    marketplace_info,
    marketplace_zip,
)
from app.resource_types import ResourceType
from app.web.router import admin_data_sources_page, library_facet_search, library_folder_peek_rows


def test_get_current_user_is_not_a_coroutine_function():
    assert not inspect.iscoroutinefunction(get_current_user)


def test_get_optional_user_is_not_a_coroutine_function():
    assert not inspect.iscoroutinefunction(get_optional_user)


def test_require_session_token_is_not_a_coroutine_function():
    assert not inspect.iscoroutinefunction(require_session_token)


def test_require_admin_is_not_a_coroutine_function():
    assert not inspect.iscoroutinefunction(require_admin)


def test_require_broker_ticket_is_not_a_coroutine_function():
    assert not inspect.iscoroutinefunction(require_broker_ticket)


def test_require_resource_access_inner_dep_is_not_a_coroutine_function():
    # The factory itself is a plain def; the dependency FastAPI actually
    # resolves per request is the returned inner ``dep`` — that is the one
    # that must be offloaded to the thread pool.
    dep = require_resource_access(ResourceType.TABLE, "{table_id}")
    assert not inspect.iscoroutinefunction(dep)


# Route handlers in app/api/catalog.py, app/api/claude_md.py and app/api/sync.py
# that PR #890 converted from ``async def`` to plain ``def``. None of those three
# modules contains a single ``await`` — every handler does purely blocking
# synchronous DB/FS work — so each must stay a plain ``def`` to keep FastAPI
# offloading it to the thread pool. A revert to ``async def`` would put the
# blocking work back on the single event loop and stall concurrent requests.
_OFFLOADED_API_HANDLERS = [
    get_table_profile,
    list_catalog_tables,
    get_metric,
    refresh_profile,
    get_welcome,
    admin_get_workspace_template,
    admin_put_workspace_template,
    admin_reset_workspace_template,
    admin_preview_workspace_template,
    sync_manifest,
    pull_confirm,
    sync_status,
    trigger_sync,
    get_sync_settings,
    update_sync_settings,
    get_table_subscriptions,
    update_table_subscriptions,
    # Marketplace / Cowork package serving (the Cowork-package-download-hang
    # fix): these bodies walk plugin trees, hash every file and build ZIPs in
    # memory with zero awaits. As ``async def`` a single large-plugin build
    # froze the whole event loop — health checks, every other request, and
    # the download itself when queued behind another build.
    marketplace_info,
    marketplace_zip,
    cowork_plugin_zip,
    generate_bundle,
    list_setup_tokens,
    revoke_setup_token,
    exchange_setup_token,
    # /admin/data-sources perf fix: on an instance with a large SharePoint
    # corpus this trio's own synchronous work could run for 10+ seconds —
    # as ``async def`` that monopolized the event loop for the whole
    # duration, and every OTHER concurrent request (including unrelated
    # ones) queued behind it and looked slow too, even though its own
    # queries were fast in isolation.
    admin_data_sources_page,
    list_connections,
    fleet_extraction_runs,
    # /admin/data-sources perf follow-up: the lazy per-connection graph-
    # counts endpoint the card fetches after painting — same reasoning,
    # zero awaits, purely blocking PG I/O.
    facts_graph_counts,
    # /library index round 2: the folder-peek fragment a row's twisty fetches
    # on first expand — zero awaits, purely blocking corpus_files/collection
    # reads (Devin Review on #2173).
    library_folder_peek_rows,
    # /library index round 3: the "search facets" typeahead — zero awaits,
    # purely blocking facts_pg/accessible_collection_ids DB work.
    library_facet_search,
    # TCRD-296 gap #72: every OTHER `async def` route in admin_extraction.py
    # had zero awaits — pure blocking `extraction_runs`/jobs/facts-ledger
    # reads and writes run directly on the event loop. `extraction_status`
    # (polled by every open source card) and `fleet_extraction_runs`'s own
    # `_facts_pending_documents` call chain were the ones a live py-spy
    # capture caught mid-request; the rest of the module shared the exact
    # same "async def, no await" shape and are fixed alongside it rather
    # than left for the next incident to find one at a time.
    extraction_status,
    request_extraction_stop,
    patch_extraction_facts_config,
    patch_extraction_crawl_config,
    extraction_runs,
    extraction_run_detail,
    extraction_config,
    preview_anonymization,
]


@pytest.mark.parametrize("handler", _OFFLOADED_API_HANDLERS, ids=lambda h: h.__name__)
def test_api_route_handler_is_not_a_coroutine_function(handler):
    assert not inspect.iscoroutinefunction(handler)
