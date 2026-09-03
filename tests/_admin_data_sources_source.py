"""Shared helper: the admin_data_sources page's full "logical" source.

Perf follow-up (2026-09-03): the page's own inline JavaScript used to be
~439 KB, re-sent uncacheable on every load. Most of it moved into four
static, cache-eligible files under ``app/web/static/js/admin/`` (a normal
``static_url()``-versioned asset, same mechanism every other page's JS
already uses) — only the per-request data bootstrap (``SOURCE_PIPELINES``/
``DERIVED_SOURCES``) stays inline in the template.

A LOT of tests string-search or ``_extract_function``-lift a specific
function/markup fragment out of ``admin_data_sources.html`` — they do not
care WHICH physical file a fragment lives in, only that it exists and reads
correctly. Rather than have each test know the new split, they read the
page's source through this one helper, which concatenates the template and
every extracted static file in the SAME order the browser loads them
(so `_extract_function`'s "first match wins" behavior — most of these
helpers just take the first index() hit — is unaffected by the split).
"""

from __future__ import annotations

from pathlib import Path

from tests import _ds_page_source

#: The template, the partial it includes, then every classic script it loads,
#: in load order. One list, owned by ``tests/_ds_page_source.py`` — this
#: module is the older spelling of the same helper and stays only so its
#: callers need no edit.
ADMIN_DATA_SOURCES_SOURCE_FILES: tuple[Path, ...] = tuple(
    p
    for p in (_ds_page_source.TEMPLATE, *_ds_page_source._INCLUDED, *_ds_page_source._LOADED)
    # `_LOADED` may name a script the page does not load YET (a planned
    # split); `page_source()` skips it the same way.
    if p.exists()
)


def read_admin_data_sources_source() -> str:
    """The template's HTML plus every JS file extracted from it, concatenated
    in load order. Use this wherever a test used to do
    ``Path(...).read_text()`` on ``admin_data_sources.html`` alone to find a
    JS function or a markup fragment — a fragment that moved into one of the
    static files is still found, at the same relative ordering. Same bytes as
    ``_ds_page_source.page_source()``."""
    return _ds_page_source.page_source()


def fetch_admin_data_sources_page(seeded_app) -> str:
    """``GET /admin/data-sources`` (a REAL round trip through the app —
    exercises auth same as before) with the loaded static JS appended, so
    a test that string-searches the "page" for a JS fragment still finds it
    regardless of which physical file it now lives in (perf follow-up,
    2026-09-03). Reads the static files from disk rather than issuing more
    HTTP requests — same bytes the server would serve, since nothing about
    ``StaticFiles`` transforms file content in flight. The included partial
    is NOT appended: the response already carries it rendered."""
    resp = seeded_app["client"].get(
        "/admin/data-sources",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
    )
    static_js = "\n".join(p.read_text(encoding="utf-8") for p in _ds_page_source._LOADED if p.exists())
    return resp.text + "\n" + static_js
