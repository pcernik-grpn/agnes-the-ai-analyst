"""Canonical inventory for the `/admin` sidebar (`_admin_nav.html`) and for
the per-section tab strip (`_admin_tabs.html`).

Single source of truth for both tiers. The IA is the admin's JOBS, in the
order the work happens — the shape decided by the admin redesign
(docs/superpowers/specs/2026-08-12-admin-redesign-exploration.md):

    Overview                     where am I, what needs me, what's next
    ── manage ──
    People                       accounts · groups · tokens
    Data                         sources · tables · packages · semantics
    Access                       who can use what · simulate a person
    ── maintain ──
    Library                      what analysts can find: curation + moderation
    Instance                     the machine: config, secrets, connections
    Activity                     what's happening: audit, telemetry, adoption

Three intent sections under MANAGE (get people in, get data in, get the data
to the people), three maintenance sections under MAINTAIN. Overview sits above
both labels — it is a peer of the whole column, not a member of either half.

── TWO TIERS, NOT ONE LONG LIST ──────────────────────────────────────────
A section is either a DESTINATION or a GROUP, and the difference is what the
sidebar does with it:

  * ``tabs``  → the section is ONE PAGE WITH TABS. The sidebar renders a
    single flat row pointing at ``href``; the tabs render as a strip on the
    page itself (`_admin_tabs.html`, fed by `resolve_section_tabs`). People
    and Data work this way: "Sources · Tables · Packages · Semantic" is one
    place you stand in, not four places you choose between in a column.

  * ``items`` → the legacy disclosure group: the sidebar renders a
    collapsible heading with a row per item. Kept for the three MAINTAIN
    sections until each has a landing page of its own; converting them means
    a 9-tab strip, which is worse than the column they have now.

The two coexist on purpose and the template branches on which key is present.
Nothing may carry both.

Why tabs at all: the sidebar had ~28 rows across seven disclosures, so
"where is Tables?" meant knowing which of seven headings owns it — a
question about our schema, not about the work. As tabs, the sub-navigation is
visible only where it applies and every tab STAYS A REAL URL (a link, not a
pane switch), so deep links, bookmarks and the back button behave exactly as
they did.

Kept as a plain Python module (not inline in the Jinja partial) so
`tests/test_web_admin_nav.py` can import it directly and assert every
`require_admin`-gated, template-rendering GET route in `app/web/router.py` is
reachable from some entry here — a section row, a tab, an item, or the
explicitly-justified `ADMIN_NAV_OFFNAV` list below. The guard fails loudly
the moment a new admin page ships with no way to reach it.

Each section:
    key   — stable slug. For a GROUP it is also the collapse-state key
            (localStorage) and the DOM id suffix for the section's item list;
            never reuse another section's key and never rename one casually,
            it would silently reset every browser's stored preference.
    label — sidebar row text (destination) or section heading (group).
    icon  — name passed to `macros/_icon.html`'s `icon()` macro.
    href  — DESTINATION ONLY: where the sidebar row goes. Must equal the
            first tab's href, so clicking the row and clicking its first tab
            land in the same place.
    tabs  — DESTINATION ONLY: the page's tab strip, in order.
    items — GROUP ONLY: the section's rows.

Each tab / item:
    label — the tab's or row's text.
    href  — the canonical URL it links to.
    match — path prefixes that mark it (and its detail sub-pages) active; a
            route "hits" it when the current request path equals one of
            these or starts with one of them + "/".

This column is now the ONLY navigation for the admin area. `/admin` used to
render a second copy of this inventory as a card grid; that grid is gone (the
hub is a real dashboard — see `admin_signals.py`), so anything reachable only
from the old grid had to move here or become unreachable. Two entries below
exist for that reason and carry constraints worth knowing before editing:

  - `/admin/studio` — the Studio authoring surface is available to every
    signed-in user (`get_current_user`, not `require_admin`); it shares the
    `/admin` URL prefix without being an admin-only page. It carries
    ``"when": "can_studio"``, the ONLY conditional item mechanism here: the
    partial drops the row when that context flag is falsey, matching the gate
    the old hub grid applied. `can_studio` is `get_studio_enabled()`, set on
    every context by `_build_context`.
  - `/admin/chat` — genuinely `require_admin`, but registered from
    `app/api/admin_chat.py` (a router with `prefix="/admin/chat"`), not
    `app/web/router.py`. `tests/test_web_admin_nav.py` reads BOTH modules for
    exactly this row; a nav entry pointing at a route defined in a third
    module would fail its reverse guard until that module is added there too.

Deliberately out of scope:
  - Pure redirects (`/admin/usage`, `/admin/grants`,
    `/admin/scheduler-runs`, `/admin/agent-prompt`, `/admin/workspace-prompt`)
    — they 308 onto a page that already carries a nav entry. (`/admin/access`
    was one of these and is a real page again — the Access section's row.)
  - The API-documentation links, which are not `/admin/*` routes at all — see
    `ADMIN_NAV_DOCS` at the bottom of this module.
"""

from __future__ import annotations

from urllib.parse import parse_qs

# The hub, as the sidebar's FIRST ROW — above the sections, in none of
# them. `/admin` is the landing surface every admin area is reachable from
# (its card grid is the long form of this whole sidebar), so it is a
# destination in its own right, not a heading.
#
# It used to be reachable only by clicking the column's "Admin" TITLE, which
# is not a control anyone reads as a link, and the rail compensated by hanging
# a hover flyout of admin areas off its own Admin row — a second, hand-written
# copy of this inventory that had already drifted (different labels, different
# grouping, and three `/documentation` links that are not admin pages at all:
# that route is gated by `get_current_user`, not `require_admin`). Both are
# retired: the rail's Admin is a plain link here, and here is where the areas
# are listed. See `_app_rail.html`.
#
# NOT a member of ADMIN_NAV_SECTIONS on purpose — a section's `key` drives the
# stored open/closed preference, and this row has no children to disclose. It
# also keeps `resolve_active_href` returning None for the hub, so landing on
# `/admin` still expands no section.
#
# No `icon`: rows in this column are label-only (the icons belong to the
# primary rail, one tier up — see admin-nav.css's header).
ADMIN_NAV_HOME: dict = {"label": "Overview", "href": "/admin"}

# `divider_before` — the LABEL of the half this section opens. The partial
# renders a small labelled rule above the row; the text lives here rather than
# in the template so both the split AND its wording are part of the pinned IA,
# not styling. Exactly two sections carry it — the first of each half — and
# `tests/test_web_admin_nav.py::test_the_two_halves_are_labelled` pins which.
ADMIN_NAV_SECTIONS: list[dict] = [
    {
        # DESTINATION. The tab labels are the nouns, not the table names:
        # "People" for accounts (the page is /admin/users), because the
        # section is already called People and "Users · Groups · Tokens" would
        # say People twice.
        "key": "people",
        "label": "People",
        "icon": "users",
        # Opens the MANAGE half — the three sections you work IN. Without a
        # label the column read as one unlabelled block plus a labelled one, so
        # "Maintain" looked like the name of a subsection rather than the second
        # half of a pair.
        "divider_before": "Manage",
        "href": "/admin/users",
        "tabs": [
            {"label": "People", "href": "/admin/users", "match": ["/admin/users"]},
            # Groups USED to be the middle tab here. It moved to Access (below),
            # because a group is not a kind of person — it is the unit a grant is
            # written against, and every reason to create one ("Finance should
            # reach the revenue package") is an access reason. Under People it
            # also left the instance with two lists of the same objects: this
            # table and the selector Access's editor already had.
            {"label": "Tokens", "href": "/admin/tokens", "match": ["/admin/tokens"]},
        ],
    },
    {
        # DESTINATION. Source → tables → packages → the definitions layered
        # over them: the order the work happens, and the order a reader needs
        # to understand any one of them.
        "key": "data",
        "label": "Data",
        "icon": "data",
        "href": "/admin/data-sources",
        # A section-level `match` on top of the tabs' own: `/admin/sync` has no
        # tab (see ADMIN_NAV_OFFNAV) but it is unambiguously part of Data, and
        # without this a caller who arrived from a source card's SYNC cell got a
        # sidebar that lit nothing — "you are nowhere" on a page one click deep.
        # It lights Data and renders Data's strip with NO tab active, which is
        # the truth: you are in this section, on none of its four lenses, and
        # every one of them is one click away.
        "match": ["/admin/sync"],
        # ── The one strip that is a PIPELINE, not a set of categories ──────
        # Sources, Tables and Packages are not three kinds of thing you choose
        # between; they are one flow seen from three places — where data comes
        # in, what there is, who receives it. Rendered as a flat tab row they
        # read as peers, and "why is Sources next to Packages?" is a fair
        # question with no answer.
        #
        # `chain: True` marks the three the arrows run between.
        # `_admin_tabs.html` switches to its flow variant when it sees a `chain`
        # on any tab, so every other section's strip renders through the
        # unchanged `ds.tabs` component.
        #
        # THE NAME CARRIES THE STAGE — there is no second line under it. Each
        # tab shipped with a purpose caption ("where data comes in", "who
        # receives it"), which is the tell that the name was not doing its job:
        # a label needing a gloss should be relabelled, not annotated, and four
        # captions doubled the strip's height to explain four words. So
        # "Semantic" (an adjective with no noun) became **Semantic layer**, and
        # "Packages" — the word whose meaning nobody could state, which is what
        # started this whole reshape — became **Data packages**, the term the
        # API, the CLI (`agnes stack add data_package`) and the analyst's own
        # Library all already use. It says Data twice under a Data section, and
        # that is the cheaper cost: "Packages" alone collides with the
        # marketplace's plugin packages, and a tab you have to disambiguate by
        # section is worse than one that repeats a word.
        #
        # SEMANTIC CARRIES NO `chain`, on purpose. It is not a fourth stage:
        # `metric_definitions` is ONE instance-wide registry that several
        # Keboola projects write into under their own `source_ref`, so it sits
        # ACROSS the pipeline rather than after Packages in it. It renders past
        # a divider with no arrow into it — reachable from the same strip,
        # visibly not part of the sentence. (The longer argument for why it
        # earns a tab at all is in the comment on its entry below.)
        "tabs": [
            {
                "label": "Sources",
                "href": "/admin/data-sources",
                "match": ["/admin/data-sources"],
                "chain": True,
            },
            {
                "label": "Tables",
                "href": "/admin/tables",
                "match": ["/admin/tables"],
                "chain": True,
            },
            {
                "label": "Data packages",
                "href": "/admin/data-packages",
                "match": ["/admin/data-packages"],
                "chain": True,
            },
            # Semantic layer EARNS a tab where Sync does not, and the reason is
            # what each thing is. A sync run is per-source and per-table — the
            # source card's own SYNC cell is where it belongs, and the only
            # cross-source question ("what failed today") is Activity's. The
            # metric/glossary registry is the opposite: `metric_definitions` is
            # ONE instance-wide table that several Keboola projects write into
            # under their own `source_ref` (connectors/keboola/semantic_layer.py
            # ::sync_semantic_layer), so a per-source panel structurally cannot
            # show the thing that matters most — the same metric defined in two
            # projects. Its singularity is the product promise (`agnes catalog
            # --metrics` must give ONE answer for "what is MRR"), so it gets a
            # surface of its own. Syncing stays per project.
            {
                "label": "Semantic layer",
                "href": "/admin/semantic-layer",
                "match": ["/admin/semantic-layer"],
                # No `chain` — see the block comment above the tabs list.
            },
        ],
    },
    {
        # DESTINATION. TWO tabs, not three: "Groups" was a separate list page
        # over the same rows the workspace's left column already carries, so a
        # section whose first two tabs were both "here are the groups" now has
        # one — the workspace — and one question, Simulate.
        #
        # `/admin/groups` and `/admin/groups/{id}` 308 onto this page (the
        # detail page's members and grants are the workspace's two panes), so
        # every link, bookmark and shortcut aimed at the old URLs still lands.
        #
        # Simulate is a real URL (`?lens=simulate`) rather than an in-page
        # button strip, so both tabs navigate the same way. The editor keeps
        # its selected group across the trip by restoring it from
        # sessionStorage (see admin_access.html) — the state the old
        # pane-switch protected, without a second navigation model on one page.
        "key": "access",
        "label": "Access",
        "icon": "shield-check",
        "href": "/admin/access",
        "tabs": [
            {"label": "Groups", "href": "/admin/access", "match": ["/admin/access"]},
            {
                "label": "Simulate a person",
                "href": "/admin/access?lens=simulate",
                # No `match` of its own: a query string is not a path, so this
                # tab can never win the path-prefix race against "Groups".
                # `resolve_section_tabs` marks it active off the query instead
                # — the one tab whose active-state is not positional.
                "match": [],
            },
        ],
        # The two retired URLs. They redirect, but the nav still has to claim
        # them: a 308 is followed by the browser, and anything that resolves a
        # section from a path (the reverse guard, a mid-redirect render) must
        # light Access rather than nothing.
        "match": ["/admin/grants", "/admin/groups"],
    },
    {
        # Key stays `library` (it is the localStorage collapse key and the
        # active-section anchor — renaming it would forget every browser's
        # disclosure state); only the LABEL changes. "Library" here collided
        # with the analyst Library (/library) while meaning something
        # entirely different — marketplaces, moderation, curation — and the
        # first-time-admin walkthrough (2026-08-18 IA investigation) hit the
        # collision within minutes. "Content" names the actual job.
        "key": "library",
        "label": "Content",
        "icon": "package",
        "divider_before": "Maintain",
        "items": [
            # What analysts can find — curation and moderation are the same
            # job from two directions, so the old Moderation + Content
            # sections merge here.
            {"label": "Marketplaces", "href": "/admin/marketplaces", "match": ["/admin/marketplaces"]},
            {"label": "Store moderation", "href": "/admin/store", "match": ["/admin/store"]},
            # "Submissions", not "Flea submissions": the trust vocabulary
            # moved to Organization/Verified/Community and the seam spec's
            # decision 8 retires the WORD flea, never the URLs.
            {"label": "Submissions", "href": "/admin/store/submissions", "match": ["/admin/store/submissions"]},
            {"label": "Store lint", "href": "/admin/store/lint", "match": ["/admin/store/lint"]},
            {
                "label": "Studio suggestions",
                "href": "/admin/studio/suggestions",
                "match": ["/admin/studio/suggestions"],
            },
            {"label": "Corporate memory", "href": "/admin/corporate-memory", "match": ["/admin/corporate-memory"]},
            {"label": "Knowledge digests", "href": "/admin/knowledge-digests", "match": ["/admin/knowledge-digests"]},
            {"label": "News", "href": "/admin/news", "match": ["/admin/news"]},
            {"label": "Contribute a skill", "href": "/admin/contribute-skill", "match": ["/admin/contribute-skill"]},
            # Conditional — see the module docstring. `match` stays the bare
            # prefix: `/admin/studio/suggestions` is its own row above and
            # wins on longest-prefix, so the two never light together.
            {
                "label": "Studio",
                "href": "/admin/studio",
                "match": ["/admin/studio"],
                "when": "can_studio",
            },
        ],
    },
    {
        "key": "instance",
        "label": "Instance",
        "icon": "tools",
        "items": [
            # The machine itself — configuration, secrets, and outbound
            # connections (the old Connections section was Instance plumbing
            # wearing its own heading).
            {"label": "Server config", "href": "/admin/server-config", "match": ["/admin/server-config"]},
            {"label": "Database backend", "href": "/admin/database", "match": ["/admin/database"]},
            {"label": "Initial workspace", "href": "/admin/initial-workspace", "match": ["/admin/initial-workspace"]},
            {
                "label": "Prompts",
                "href": "/admin/prompts",
                "match": ["/admin/prompts", "/admin/agent-prompt", "/admin/workspace-prompt"],
            },
            {
                "label": "Instance secrets",
                "href": "/admin/datasource-credentials",
                "match": ["/admin/datasource-credentials"],
            },
            {"label": "MCP sources", "href": "/admin/mcp-sources", "match": ["/admin/mcp-sources", "/admin/mcp-tools"]},
            {"label": "Linked apps", "href": "/admin/linked-apps", "match": ["/admin/linked-apps"]},
        ],
    },
    {
        "key": "activity",
        "label": "Activity",
        "icon": "rows",
        "items": [
            {"label": "Audit log", "href": "/admin/activity", "match": ["/admin/activity"]},
            {"label": "Telemetry", "href": "/admin/telemetry", "match": ["/admin/telemetry", "/admin/usage"]},
            {"label": "Analyst sessions", "href": "/admin/sessions", "match": ["/admin/sessions"]},
            {"label": "Chat sessions", "href": "/admin/chat", "match": ["/admin/chat"]},
            {"label": "Adoption", "href": "/admin/adoption", "match": ["/admin/adoption"]},
        ],
    },
]

# API documentation — a footer strip in `_admin_nav.html`, NOT a
# section. These are not `/admin/*` routes (`/documentation/api` is
# `get_current_user`-gated; `/docs` and `/redoc` are FastAPI's own), so they
# cannot be section items without widening
# `tests/test_web_admin_nav.py::test_every_nav_href_is_a_real_admin_route`
# past the job it exists to do — and another section would break the
# deliberate IA that `test_exactly_the_decided_sections_in_order` pins. They landed here when `/admin`'s card grid was replaced by the
# dashboard; the grid was the only place they were linked from.
#
# All three, in a COLLAPSED DISCLOSURE at the foot — the same
# `.admin-nav__group` component the MAINTAIN sections use, keyed `docs`.
#
# It went through both extremes first. As an uppercase heading over three
# permanently-visible rows it spent four elements, in the column's densest
# corner, on links an admin follows once a quarter — the loudest thing in the
# sidebar was its least-used part. Collapsed to a single row pointing at the
# guide (which does link Swagger and ReDoc itself), the two interactive
# references became two clicks and a page-scan away, for no gain: they are how
# you test a call while writing one, which is exactly when you do not want to
# read prose first. A disclosure gives both — one row at rest, all three the
# moment you want them — and each label names the TOOL it opens, because
# "Interactive API" and "API reference" are indistinguishable until you know
# one is Swagger and the other ReDoc.
#
# Default CLOSED and never force-opened: `admin/admin_nav.js` only forces the
# section matching `data-active-section`, and no `/documentation` path ever
# resolves to a section key. A caller who opens it keeps it open (same
# localStorage map as the other groups).
#
# Still a footer row rather than a section: it is not an `/admin/*` route at all
# (`/documentation/api` is `get_current_user`-gated), so it cannot be a section
# item without widening the coverage guard past the job it exists to do.
#
# No `match` key: it never renders active. The admin column is not visible on
# the destination.
ADMIN_NAV_DOCS: list[dict] = [
    {"label": "API guide", "href": "/documentation/api"},
    {"label": "Interactive API (Swagger)", "href": "/docs"},
    {"label": "API reference (ReDoc)", "href": "/redoc"},
]

# Admin pages that are deliberately in NO nav tier — no sidebar row, no tab —
# because a better door already exists on the surface that owns them. The
# reverse coverage guard reads this list, so an entry here is a JUSTIFICATION
# on the record, not a way to silence the guard: state where the page is
# reached from, and if the answer is "nowhere", it needs a nav entry instead.
ADMIN_NAV_OFFNAV: list[dict] = [
    {
        "href": "/admin/sync",
        # Every source card on /admin/data-sources carries a SYNC cell (freshest
        # run, or how many tables are failing) linking here, so the page is one
        # click from the source whose sync you are asking about. As a Data tab it
        # was the odd one out: Sources/Tables/Packages/Semantic are all things
        # you MANAGE, and this is a log you CHECK — and the cross-source version
        # of that question ("what failed today") is what /admin/activity is.
        "reached_from": "the SYNC cell on each source card (/admin/data-sources)",
    },
]


def _prefix_hit(path: str, prefix: str) -> bool:
    """Whether *path* is on *prefix* — exact match, or a sub-page
    (``prefix + "/..."``)."""
    return path == prefix or path.startswith(prefix + "/")


def _section_entries(section: dict) -> list[dict]:
    """A section's navigable children — its ``tabs`` (destination) or its
    ``items`` (group). One accessor so every resolver and every guard walks
    the same set regardless of which shape the section is."""
    return section.get("tabs") or section.get("items") or []


def _section_matches(section: dict) -> list[str]:
    """Every path prefix that belongs to *section* — its children's ``match``
    prefixes plus its own (a tabless destination like Access has no children,
    so its ``match`` is the only thing that can place a path inside it)."""
    prefixes = list(section.get("match") or [])
    for entry in _section_entries(section):
        prefixes.extend(entry["match"])
    return prefixes


def resolve_active_href(path: str) -> str | None:
    """The href of the ONE nav entry — tab or item — that should render active
    for *path*.

    Several entries' ``match`` prefixes can textually overlap (``/admin/store``
    the moderation hub vs. ``/admin/store/submissions`` its own row) — the
    LONGEST matching prefix wins, so a sub-page never leaves its own parent
    lit at the same time. Returns ``None`` when nothing matches (e.g. the
    ``/admin`` hub page itself, which has no section entry — see
    ``ADMIN_NAV_HOME``).
    """
    best_href: str | None = None
    best_len = -1
    for section in ADMIN_NAV_SECTIONS:
        for entry in _section_entries(section):
            for prefix in entry["match"]:
                if _prefix_hit(path, prefix) and len(prefix) > best_len:
                    best_len = len(prefix)
                    best_href = entry["href"]
    return best_href


def resolve_section_tabs(path: str, query: str = "") -> list[dict]:
    """The tab strip to render on *path*, ready for ``ds.tabs`` — the tabs of
    whichever DESTINATION section owns it, each as
    ``{"label", "href", "active"}``.

    ``active`` is computed HERE rather than compared in the template, for the
    same reason ``resolve_active_href`` exists at all: which entry wins is a
    longest-prefix decision over ``match`` prefixes, and a template doing it by
    href equality would light two tabs the first time two of them overlap.

    *query* is the request's raw query string, and it matters for exactly one
    kind of tab: a LENS on a page that already owns a tab of its own (Access's
    "Simulate a person" at ``/admin/access?lens=simulate``). Such a tab shares
    its path with a sibling, so no ``match`` prefix can separate them — it is
    resolved off the query instead, and when it wins it VETOES that sibling, or
    the strip would light both tabs on the same page. Callers that pass no
    query simply never light a lens tab, which is the correct answer for the
    bare path.

    Returns ``[]`` for three legitimate reasons: the path is in a legacy
    disclosure GROUP (its rows are the sidebar's, not a strip's), the section
    is a tabless destination, or the path is off-nav entirely
    (``ADMIN_NAV_OFFNAV``). `_admin_tabs.html` renders nothing in each case, so
    a page never sprouts a one-tab strip.

    Fresh dicts, never the module-level ones: ``active`` is per-request state
    and stamping it onto the inventory would leak one caller's page into the
    next request's strip.
    """
    key = resolve_active_section_key(path)
    if key is None:
        return []
    active_href = resolve_active_href(path)
    params = parse_qs(query or "")
    for section in ADMIN_NAV_SECTIONS:
        if section["key"] != key:
            continue
        tabs = section.get("tabs") or []
        # Pass 1 — does a lens tab win? At most one can: the first whose path
        # is the current path AND whose query pair is present in the request.
        lens_href: str | None = None
        for tab in tabs:
            base, _, tab_query = tab["href"].partition("?")
            if not tab_query or base != path:
                continue
            name, _, value = tab_query.partition("=")
            if value in params.get(name, []):
                lens_href = tab["href"]
                break
        # Pass 2 — everything else by longest-prefix, minus the veto.
        out = []
        for tab in tabs:
            base, _, tab_query = tab["href"].partition("?")
            if tab_query:
                active = tab["href"] == lens_href
            else:
                active = tab["href"] == active_href and not (lens_href and base == path)
            # `chain` rides along for the ONE section whose strip is a pipeline
            # (Data). It is optional and defaults falsey, so `ds.tabs` — which
            # reads only label/href/active — is unaffected for every other
            # section, and `_admin_tabs.html` picks its variant off whether any
            # tab is chained.
            out.append(
                {
                    "label": tab["label"],
                    "href": tab["href"],
                    "active": active,
                    "chain": bool(tab.get("chain")),
                }
            )
        return out
    return []


def resolve_active_section_key(path: str) -> str | None:
    """The ``key`` of the ONE section *path* belongs to.

    Two jobs, one answer. For a DESTINATION the sidebar lights that row and
    the page renders that section's tab strip. For a legacy GROUP it is the
    section that renders expanded BY DEFAULT — server-side, so a first paint
    (before any client JS) neither shows a fully-expanded list nor collapses
    the very section the caller is standing in.

    Resolved on the LONGEST matching prefix across the section's own ``match``
    and all its children's, not on ``resolve_active_href``'s answer: a tabless
    destination (Access) has no child to look up, and the two must never
    disagree about which section a path is in. Returns ``None`` for the
    ``/admin`` hub itself and for off-nav pages (``ADMIN_NAV_OFFNAV``), where
    no row is lit and no strip renders.
    """
    best_key: str | None = None
    best_len = -1
    for section in ADMIN_NAV_SECTIONS:
        for prefix in _section_matches(section):
            if _prefix_hit(path, prefix) and len(prefix) > best_len:
                best_len = len(prefix)
                best_key = section["key"]
    return best_key


def resolve_home_active(path: str) -> bool:
    """Whether the sidebar's first row (``ADMIN_NAV_HOME``) is the active one.

    EXACT match on ``/admin`` — not a prefix. Every ``/admin/*`` path belongs
    to one of the sections, and a prefix rule here would light the hub
    row on all of them, giving the column two active rows at once.
    """
    return path.rstrip("/") == "/admin"
