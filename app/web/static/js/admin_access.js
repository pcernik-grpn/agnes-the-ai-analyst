(function () {
  "use strict";

  /* ── What the server hands this page ────────────────────────────────
     Everything below used to be a Jinja `{{ … }}` because the whole script
     lived inside the template. It reads one JSON blob now — the same shape
     the chat page uses (`<script type="application/json">`) — so this file
     is a static asset a test can execute, not text a renderer produces.
     Missing or corrupt is survivable: every reader below has a default. */
  const BOOT = (() => {
    try {
      const node = document.getElementById("ax-boot-data");
      return node ? JSON.parse(node.textContent) : {};
    } catch (e) {
      return {};
    }
  })();
  /* The tier's two labels and their help text, from `app/web/vocabulary.py`.
     They are read, not spelled, for the same reason the templates read them:
     this control has been renamed twice and a fourth spelling must not be
     able to enter through a string literal here. */
  const WORDS = BOOT.words || {};

  const GRANTS_API = "/api/admin/grants";
  const REACH_API = "/api/admin/groups/reach"; // distinct people a set of audiences reaches
  const MEMBER_SEARCH_API = "/api/admin/groups/member-search"; // which groups hold a matching person
  const OVERVIEW_API = "/api/admin/access-overview";
  const USERS_LIST_API = "/api/users";        // list + search + single user
  const ADMIN_USERS_API = "/api/admin/users"; // memberships + effective-access
  // Who is looking. Used only to withhold the view-as offer on the caller's
  // own row — the server refuses it either way (`view_as_self`).
  const VIEWER_USER_ID = BOOT.viewer_user_id ?? null;

  // Data packages first — the redesign's spine. Everything else keeps a
  // section but stays collapsed: breadth disclosed, never removed.
  const LEAD_TYPE = "data_package";
  // Search results shown per query. The server does the searching, so this is
  // a "how much fits under an input" number, not a cap on who is reachable.
  const FIND_LIMIT = 8;

  // Server-injected env: empty string = no prefix configured. A Workspace
  // group is STORED under its full email, so without this the left column
  // would read "grp_acme_finance@example.com" where the retired list read
  // "Finance". Same constant, same derivation, as that list used.
  const GOOGLE_GROUP_PREFIX = BOOT.google_group_prefix || "";
  //: The instance's configured sign-in domains. An invited account can only
  //: ever authenticate with one of these, so they are the candidates the
  //: invite field completes with. Empty is normal (no domain configured) —
  //: the field then takes a whole address.
  const INVITE_DOMAINS = BOOT.invite_domains || [];

  let overview = null;   // {groups, grants, resources}
  let selectedGroup = null;
  //: "group" | "bundle" — which way the one list is read. Rides the URL so a
  //: link can hand someone the view that answers their question.
  /* Three ways to read one set of grants. `?lens=simulate` is kept as an
     alias for `?by=person` so every existing link into Simulate — including
     each group row's "See it as a person" — still lands where it always
     did. */
  //: Every piece of view state below is seeded from the URL, so a shared
  //: link restores the whole view rather than just the lens — see `syncUrl()`.
  //: This has to be declared before any of them reads it (`const` is in the
  //: temporal dead zone until its own line).
  /* The lens the URL asks for, or "" for anything this page does not know.
     `bundle` was the old name for the resource lens — a word that named no
     entity in the system and had to be inferred from the shape of the rows.
     It stays readable here, and only here, because it is in shared links and
     bookmarks: everything the page WRITES is the new name. */
  function _normalizeBy(raw) {
    const v = raw === "bundle" ? "resource" : raw;
    return ["resource", "person", "group"].includes(v) ? v : "";
  }

  const _q0 = new URLSearchParams(window.location.search);
  /* ── The filters ─────────────────────────────────────────────────────
     Four facets, multi-select, the Library's shape (`fbar-menu--cats`, one
     submenu per category, a count beside every option, chips for what is
     on). It was ONE facet — Kind — as radio buttons, so the page could
     answer "show me the plugins" and nothing else. The three that joined it
     are the questions an admin actually opens this page with, and each is a
     field the payload already carries, so none of them is a new round trip:

       Reach   — everyone / a group / nobody. The page's own subject. It
                 already renders "granted to nobody" as a state and floats
                 those rows into a drawer; this is the same distinction
                 made askable.
       Tier    — Automatic / Optional. "What am I forcing on people" is the
                 question the tier exists for and there was no way to ask it.
       Origin  — granted here / shared by an owner / managed elsewhere. The
                 axis ticket 10 settled the page on: whether the admin can
                 act on the row. Also the fastest way to find the rows an
                 owner shared, which is what an admin most often opens this
                 page to check.

     Multi-select within a facet is OR (plugins or agents); across facets it
     is AND (plugins, granted to nobody). That is the Library's rule, and the
     one every faceted list a person has used follows. */
  const FACET_KEYS = ["kind", "reach", "tier", "origin"];
  //: `Set` per facet; empty means the facet is off, which is not the same as
  //: every value being ticked (a value that matches nothing would then
  //: silently narrow the list to nothing).
  const facets = new Map(FACET_KEYS.map((k) => [k, new Set()]));
  const facetOn = (k) => facets.get(k).size > 0;
  const facetHas = (k, v) => facets.get(k).has(v);
  const anyFacetOn = () => FACET_KEYS.some(facetOn);
  const clearFacets = () => FACET_KEYS.forEach((k) => facets.get(k).clear());
  for (const k of FACET_KEYS) {
    for (const v of (_q0.get(k) || "").split(",")) if (v) facets.get(k).add(v);
  }
  /* `?kind=` was a single value and is in shared links, so it keeps working
     — it is simply the one-element case of the set now. */

  /* What a row's grants make it. Derived per row rather than stored, because
     every input is already on the payload and a second copy could disagree
     with the rows it is meant to describe. */
  const REACH = { EVERYONE: "everyone", GROUP: "group", NOBODY: "nobody" };
  const reachOfRow = (held) => (!held || !held.length
    ? REACH.NOBODY
    : held.some((g) => g.audience === "everyone") ? REACH.EVERYONE : REACH.GROUP);
  const ORIGIN = { ADMIN: "admin", OWNER: "owner", MANAGED: "managed" };
  /* Three origins, in the order an admin cares about them. `managed_by` with
     `revocable === false` is a row another surface re-asserts — the one kind
     a revoke here cannot remove. `section` is the server's own answer to
     "can the admin act on this", and an owner-shared row is the rest. */
  const originOfGrant = (g) => (g && g.managed_by && g.managed_by.revocable === false
    ? ORIGIN.MANAGED
    : (g && g.source === "library_sharing") ? ORIGIN.OWNER : ORIGIN.ADMIN);
  const tierOfGrant = (g) => ((g && g.requirement) === "required" ? "required" : "available");

  /* One row against every active facet. AND across facets, OR inside one. */
  const rowPassesFacets = (typeKey, held) => {
    if (facetOn("kind") && !facetHas("kind", typeKey)) return false;
    if (facetOn("reach") && !facetHas("reach", reachOfRow(held))) return false;
    if (facetOn("tier") && !(held || []).some((g) => facetHas("tier", tierOfGrant(g)))) return false;
    if (facetOn("origin") && !(held || []).some((g) => facetHas("origin", originOfGrant(g)))) return false;
    return true;
  };
  /* `?user=<id>` on its own used to be discarded in silence — it selected
     nothing and landed you on the group lens — even though it is about as
     unambiguous a request as this page receives. Worse, leaving the person
     lens left exactly that URL in the address bar to be copied and shared.
     A user id names a person, and the person lens is where a person is
     shown, so it implies the lens. */
  let viewMode = _q0.get("lens") === "simulate" ? "person"
    : (_normalizeBy(_q0.get("by")) || (_q0.get("user") ? "person" : "group"));
  let users = [];        // the person lens's picker; the group-list member search asks the server

  const el = (id) => document.getElementById(id);
  //: `CSS.escape` where it exists. A resource id is server-issued, but
  //: it reaches a selector here and an unescaped one would be a syntax
  //: error rather than a miss.
  const cssEsc = (v) => (window.CSS && CSS.escape ? CSS.escape(String(v)) : String(v));
  const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  // datetime.js is loaded `defer`, so window.AgnesTime does not exist until
  // parsing finishes — but boot() starts its fetch during parse. On a cold
  // load the overview endpoint can answer first, and reaching through an
  // undefined AgnesTime threw inside boot(), leaving the page stuck on
  // "Loading groups...". Fall back to the raw timestamp instead.
  const fmtDate = (s) => {
    if (!s) return "";
    const T = window.AgnesTime;
    if (!T || typeof T.formatDateTime !== "function") return String(s);
    return T.formatDateTime(s) || "";
  };

  /* ── What a group is called ───────────────────────────────────────────
     Three shapes, and the retired list page resolved all three the same way:
       · `mapped_email` set  → a system row (Admin / Everyone) wired to a
         Workspace group. The canonical name is the right title; the email is
         the subtitle.
       · google-managed      → the group's `name` IS the Workspace email, so
         the title is the friendly derivation and the email is the subtitle.
       · anything else       → the name, and no subtitle. */
  function deriveDisplayName(fullEmail) {
    if (!fullEmail) return "";
    const local = String(fullEmail).split("@")[0] || String(fullEmail);
    const px = (GOOGLE_GROUP_PREFIX || "").toLowerCase();
    let s = local;
    if (px && s.toLowerCase().startsWith(px)) s = s.slice(px.length);
    s = s.replace(/^[_\-\s]+/, "");
    if (!s) return local;
    return s.charAt(0).toUpperCase() + s.slice(1);
  }
  function titleOf(g) {
    if (!g) return "";
    if (g.mapped_email) return g.name;
    return g.is_google_managed ? deriveDisplayName(g.name) : g.name;
  }
  function subtitleOf(g) {
    if (!g) return "";
    return g.mapped_email || (g.is_google_managed ? g.name : "");
  }
  // System seeds and Google-synced rows are renamed and deleted where they
  // are owned — in the seed, or in Workspace. The retired pages hid both
  // controls on exactly this predicate.
  const isEditable = (g) => !!g && !g.is_system && !g.is_google_managed;

  // `.flash-success` / `.flash-error` — the app's own message vocabulary.
  function toast(msg, ok) {
    const t = el("ax-toast");
    t.textContent = msg;
    t.className = "ax-toast flash show " + (ok ? "flash-success" : "flash-error");
    clearTimeout(t._t);
    t._t = setTimeout(() => { t.className = "ax-toast flash"; }, 3500);
  }

  /* ── Lens ──────────────────────────────────────────────────────────────
     The two lenses are section TABS now, so there is nothing to switch: the
     server already opened the right pane off `?lens=`, and this block only has
     to feed whichever one it opened. Simulate's picker is the one thing the
     editor never needs, so it is still fetched lazily — on load rather than on
     a click.

     What the pane-switch used to protect for free was the SELECTED GROUP: you
     could look at Simulate and come back to the group you were editing. A link
     reloads the page, so the selection has to survive in storage instead.
     `SELECTED_KEY` is sessionStorage, not localStorage, on purpose — it is
     "where I am in this sitting", and a group remembered from last Tuesday
     would silently decide what an admin edits today. ── */
  const SELECTED_KEY = "agnes.admin.access.group";
  //: Kept as a name because several later branches read it, but it is the
  //: switch's mode now, not the pane the server happened to open.
  const LENS = viewMode === "person" ? "sim" : "edit";
  if (LENS === "sim") loadUsers();

  function rememberSelection(groupId) {
    try { sessionStorage.setItem(SELECTED_KEY, groupId || ""); } catch (e) { /* private mode */ }
  }
  function recalledSelection() {
    try { return sessionStorage.getItem(SELECTED_KEY) || ""; } catch (e) { return ""; }
  }

  /* ── Editor ── */

  /* How many things a group holds, from the SAME array the rows are drawn
     from. It used to read `group.grant_count`, a number the server computed
     once at page load and which no mutation ever updated — so adding or
     revoking a grant left the collapsed row saying "1 granted" while the
     family chips beside it (derived from `overview.grants`, and therefore
     live) and the section head both said 2. Three numbers for one fact on
     one screen, and only a hard reload settled it.

     The server's `grant_count` is `COUNT(*) WHERE group_id = ?` over the
     same table `grants` is listed from, so this is the identical number —
     just one source instead of two. */
  // DIRECT only, deliberately. This feeds the group row's count and the
  // delete-group confirmation ("N people lose the M things granted through
  // this group") — deleting a group does not revoke Everyone's grants, so
  // counting inherited ones here would overstate the blast radius of a
  // destructive action. It also keeps matching the server's
  // `COUNT(*) WHERE group_id = ?`, as the comment above says.
  const grantCountOf = (groupId) => grantsFor(groupId, { directOnly: true }).length;

  //: The `Everyone` group's id, or null if this instance has none. Everyone
  //  is auto-membership: every active account is in it, so a grant written
  //  against it reaches the members of every other group too.
  function everyoneGroupId() {
    const g = (overview.groups || []).find((x) => x.is_everyone);
    return g ? g.id : null;
  }

  /* Grants a group's members can actually use — DIRECT grants plus the ones
     inherited from Everyone.

     This filtered on `group_id === groupId` alone, which answers "what was
     written against this group" and not "what can this group see". Since
     every account is in Everyone, a company-wide grant reaches Finance's
     members too — and filtering to Finance hid it, so the page's own
     question ("what each one can use") had an incomplete answer, and an
     admin checking whether Finance could reach something had to remember to
     go and look at Everyone as well.

     Inherited rows are TAGGED, never silently merged: they are not this
     group's to revoke, and the controls key off `inherited` to say so. */
  function grantsFor(groupId, opts) {
    const direct = (overview.grants || []).filter((g) => g.group_id === groupId);
    if (opts && opts.directOnly) return direct;
    const evId = everyoneGroupId();
    if (!evId || groupId === evId) return direct;
    const held = new Set(direct.map((g) => `${g.resource_type}:${g.resource_id}`));
    const inherited = (overview.grants || [])
      // A direct grant WINS: it may carry a different tier, and it is the one
      // this group's row can actually edit.
      .filter((g) => g.group_id === evId && !held.has(`${g.resource_type}:${g.resource_id}`))
      .map((g) => Object.assign({}, g, { inherited: true, inherited_from: "Everyone" }));
    return direct.concat(inherited);
  }
  function grantOf(groupId, type, resourceId) {
    return grantsFor(groupId).find((g) => g.resource_type === type && g.resource_id === resourceId);
  }

  // Everyone first — a grant to it is company-wide, so it belongs where the
  // eye lands rather than alphabetically buried. One function, because the
  // default selection must be the row that renders first: reading the
  // unsorted list landed the page on Admin, whose grants are the one set that
  // changes nothing (admins reach everything anyway).
  function sortedGroups() {
    /* Alphabetical within each half; the halves themselves are ordered in
       renderGroups (custom, then system). `Everyone` used to be hoisted to
       the very top because it was the default selection — that is gone, and
       hoisting it now would only pull one system row out of the block its
       heading introduces. */
    /* The carrier is NOT a group in this list (decision 04). It stays in
       `overview.groups` — every id lookup on the page still resolves, and
       the grant it carries is still stored against it — but it is offered
       to the reader as the audience it is, in its own entry above the list,
       not as a row among groups someone made. A group named Everyone
       invites the assumption that it can be narrowed like any other group,
       which is exactly the ambiguity the scope removes. */
    return (overview.groups || []).filter((g) => !g.is_everyone).slice().sort(
      (a, b) => String(a.name).localeCompare(String(b.name)));
  }

  /* The audience that is not a group. `id` is the sentinel the server
     already speaks (`src/grant_scopes.py::EVERYONE_TARGET_ID`), and the
     shape mimics a group only as far as the picker's own row renderer
     needs — it deliberately has no `member_count`, so anything that tries
     to count it gets `undefined` rather than a plausible wrong number. */
  const EVERYONE_AUDIENCE = { id: "everyone", is_scope: true, name: "Everyone",
                              description: "Every account, and anyone who joins." };

  /* Mirrors `src.grant_scopes.SCOPE_WITHHELD_TYPES` (decision 07). Kept as a
     literal rather than read off the payload because the server enforces it
     with a 422 either way; this only stops the page offering the choice. */
  const SCOPE_WITHHELD_TYPES = new Set(["slack_channel", "table", "memory_domain", "memory_item"]);

  //: The resource item behind a `type:rid`, or null. Scans the overview: it
  //: is called once per applied item, and the alternative is a second index
  //: to keep in step with the first.
  const itemOf = (type, rid) => {
    for (const t of (overview.resources || [])) {
      if (t.type_key !== type) continue;
      for (const b of (t.blocks || [])) for (const i of (b.items || [])) if (i.resource_id === rid) return i;
    }
    return null;
  };

  //: Does an everyone-wide grant on this resource already exist? Asked of the
  //: carrier, since that is where such a row is stored on both backends.
  const everyoneHeld = (type, rid) => {
    const cid = everyoneGroupId();
    return !!(cid && grantOf(cid, type, rid));
  };

  // Group filtering is CLIENT-side, unlike the member search: groups are a
  // small, fully-loaded set (they arrive with the overview in one round trip),
  // so a request per keystroke would buy nothing.
  let groupFilter = _q0.get("q") || "";

  // What the grant tree on the right is narrowed to. Set by the filter box,
  // and pre-set by a `?resource=` deep link from /admin/tables.
  let resourceFilter = "";
  // Which scope the Access list is showing: everything grantable, or only
  // what this group already holds.

  /* The selector. The retired list page's table lived here in two lines a
     row; what is new is the SPLIT: `Admin` and `Everyone` sit under their
     own label at the foot, because neither can be renamed, deleted or (with
     Workspace mapping) have its membership edited — interleaving the two
     rows you cannot change with the ones you do makes the editable set
     something you have to pick out of a list.

     There is no "N groups" count above the card any more: it restated the
     list directly beneath it. When a filter is on, the list says how much
     of the set it is showing, which is the only case the count answered. */
  /* What this row becomes in the granted person's Library. The admin sets
     a tier while looking at the sentence it writes on the other end —
     which is the only place the vocabulary can be kept honest, because
     nothing in the repo tests language. Words are the Library's, not
     invented here. */
  const READS = {
    data_package:       { required: "Required by your admin", available: "Keep a local copy" },
    memory_domain:      { required: "Required by your admin", available: "Keep a local copy" },
    memory_item:        { required: "Required by your admin", available: "Keep a local copy" },
    marketplace_plugin: { required: "Installed · required",   available: "Install" },
    store_entity:       { required: "Installed · required",   available: "Install" },
    recipe:             { any: "Ask Agnes" },
    data_app:           { any: "Open" },
    agent:              { any: "Use as template" },
    semantic_model:     { any: "shapes the answer", quiet: true },
    collection:         { any: "citable", quiet: true },
    corpus_file:        { any: "citable", quiet: true },
    knowledge_digest:   { any: "citable", quiet: true },
    table:              { any: "reached through a package", quiet: true },
    chat:               { any: "not a Library row", quiet: true },
    slack_channel:      { any: "not a Library row", quiet: true },
  };
  /* What the tier toast says, keyed on KIND. It used to be one hard-coded
     pair written for data packages — "permanent, and always downloaded" —
     fired verbatim when an admin flipped a MEMORY DOMAIN or a MARKETPLACE
     PLUGIN, neither of which is downloaded in that sense. The per-kind
     vocabulary already exists one map up in `READS`; this reuses it rather
     than inventing a third set of words, so the sentence the admin is told
     matches the sentence the person will read. */
  const tierSentence = (typeKey, tier) => {
    const m = READS[typeKey] || {};
    const reads = m.any || m[tier];
    const head = tier === "required" ? "Required" : "Available";
    if (!reads) {
      return tier === "required"
        ? "Required — everyone in the group gets it, and cannot opt out"
        : "Available — each person chooses whether to take it";
    }
    return tier === "required"
      ? `Required — their Library will read “${reads}”, and they cannot opt out`
      : `Available — their Library will read “${reads}”, and it is their choice`;
  };

  //: The thing a Revoke confirm is about to take away, named. Falls back to
  //: the id, which is still better than "this item".
  const revokeLabel = (typeKey, rid) => {
    for (const t of (overview.resources || [])) {
      if (t.type_key !== typeKey) continue;
      for (const b of (t.blocks || [])) {
        for (const i of (b.items || [])) {
          if (i.resource_id === rid) return itemName(i);
        }
      }
    }
    return rid;
  };

  /* What to call a grantable thing. Falls through name → slug → id.

     The slug was already in the payload and nothing read it, so an agent
     saved without a title rendered as `agt_797047742dec48998ce45792b9110ce1`
     — as a row title, and worse, as 12 of the 13 options in one group's
     picker, which made the picker unusable for exactly the kind that needs
     it most. `agent-12` is not a great name; it is an enormous improvement
     on 32 hex characters, and it is what the rest of the product calls it. */
  const itemName = (i) => (i && (i.name || i.slug || i.resource_id)) || "";

  /* WHOSE it is, and how much is in it — returns escaped HTML, so callers
     must not re-escape. Only the collection/file projections carry these
     fields today (app/resource_types.py); every other kind renders exactly as
     before, because both bits are simply absent.

     This exists because an inventory of private uploads is unreadable without
     an owner: a chat file-drop becomes a one-file collection named after the
     file, so an admin — who sees every collection by god-mode — got a flat
     list of filenames with nothing saying they belonged to someone else. */
  const itemProvenance = (i, opts) => {
    if (!i) return "";
    const bits = [];
    // Skipped when the row already names this person as the sharer: "Shared
    // by Ada Lovelace · owned by ada@example.com" says one fact twice.
    if (i.owner_email && !(opts && opts.ownerNamed)) bits.push(`owned by ${esc(i.owner_email)}`);
    if (typeof i.file_count === "number") {
      bits.push(`${i.file_count} ${i.file_count === 1 ? "file" : "files"}`);
    }
    return bits.join(" · ");
  };

  /* Admin-side page per kind — where an ADMIN goes to change a grant, as
     opposed to where an analyst goes to use the thing. Only the kinds that
     have one; anything else keeps its analyst link. */
  const ENTITY_PAGE_ADMIN = {
    data_package: (id) => `/admin/data-packages/${encodeURIComponent(id)}`,
    memory_domain: (id) => `/admin/corporate-memory?domain=${encodeURIComponent(id)}`,
  };

  /* The tier control, defined ONCE. Three surfaces render it — the group
     view's rows, the bundle view's rows, and Advanced — and three copies of
     the same two labels is exactly how a control ends up with three names.
     `tests/test_access_vocabulary.py` pins this to a single definition. */
  const tierControl = (tier, granted) => `
    <span class="fbar-seg ax-tier" ${granted ? "" : 'aria-disabled="true"'} role="group" aria-label="Access tier">
      <button type="button" class="fbar-seg__btn ${tier === "available" ? "is-active" : ""}" data-tier="available">${esc(WORDS.tier_optional)}</button>
      <button type="button" class="fbar-seg__btn ${tier === "required" ? "is-active" : ""}" data-tier="required">${esc(WORDS.tier_automatic)}</button>
    </span>`;

  //: Which kinds actually carry the Required/Available tier.
  //: `store_entity` was missing here — in this page AND in the mock it was
  //: built from — while the API has always accepted `required` on it ("In
  //: stack, locked", admissible only on an organization-published item). So
  //: the F8 branch in controlCell was dead code behind `!tiered`: an
  //: organization-published skill rendered no tier control at all, and a
  //: user-published one rendered nothing instead of stating its one legal
  //: tier. Found by seeding both kinds and looking, not by any test.
  const TIERED = new Set(["data_package", "memory_domain", "memory_item", "marketplace_plugin", "store_entity"]);

  /* ── ADMIN CONTROL, one shape for every row ───────────────────────────
     It used to be two different controls depending on kind, and never both:
     a tier pair on packages / memory / plugins, a Revoke on everything else.
     The consequence was not cosmetic — a data package, a memory domain or a
     marketplace plugin could be GRANTED from this page and never un-granted,
     in either lens. The three most consequential kinds were the ones with no
     way back, and an admin had to reach for the API to undo a click.

     So the cell is now the same everywhere: the tier pair, then Revoke.
     Untiered kinds keep the pair in view but disabled, with a tooltip saying
     why — which also answers the question the old asymmetry raised without
     ever addressing it ("why does this row have a choice and that one not?").
     Owner-shared kinds keep their "where it lives ↗" link after the control. */
  const controlCell = (typeKey, tier, opts) => {
    const o = opts || {};
    const tiered = TIERED.has(typeKey);
    /* Render a control where it can act; say nothing where it cannot.

       An untiered kind shows NOTHING here — not a greyed pair. Twelve of the
       sixteen kinds have no Optional/Automatic choice, and the page drew the
       pair on all of them, disabled, with the reason in a tooltip, to keep
       the column uniform. Uniformity bought with a dead control teaches the
       reader that the page cannot be trusted about which of its controls
       work — which is the defect this whole effort keeps undoing — and it
       disagreed with itself: a table granted to everyone printed "Optional ·
       to everyone" one row above a table showing the tier greyed out. The
       kind is already named in the row's own Kind column, so nothing is
       lost. (Audit finding U5.)

       A store entity is tiered, but Automatic is admissible ONLY when the
       organization is the publisher: requiring something conscripts every
       member of the group into carrying it, and an admin cannot make that
       commitment on behalf of a colleague's personal upload. The API refuses
       it with a 422 (`app/api/access.py`), so drawing the pair on a
       user-published row offers a choice that fails on click. Optional is
       the one legal value, and it is already the value — so this is not a
       control with one live button, it is a fact, stated with its reason
       where the sibling row's control makes the absence surprising. (F8.)
       `publisher_kind` rides on the item; the server defaults it to "user",
       and so does this, because an unknown publisher must not be granted
       the wider permission by omission. */
    const orgPublished = (o.publisherKind || "user") === "organization";
    const tierPart = !tiered
      ? ""
      : (typeKey === "store_entity" && !orgPublished)
        ? `<span class="ax-managed" data-tip="Automatic needs an organization-published item — publish it as the organization first.">${
            tier === "required" ? esc(WORDS.tier_automatic) : esc(WORDS.tier_optional)}</span>`
        : tierControl(tier, true);
    /* A grant another surface owns. Marking a plugin Required on
       /admin/marketplaces writes one to EVERY group, and the API then refuses
       to delete them (409 `cannot_revoke_system_grant`). This page was still
       drawing Revoke on all of them, and its confirm modal promised "You can
       grant it again from this page" — a control that could not work, above a
       sentence that was not true.

       Same shape as the `via Everyone →` case a few lines down: state where it
       is owned and send the admin there, rather than offer the act here. The
       tier pair goes too — Optional/Automatic on a mandatory plugin is the
       same false choice. (#1956 item 13.) */
    /* Only a NON-revocable grant loses its tier pair: on those the
       Optional/Automatic choice does not exist (a Required plugin is
       mandatory by definition), and offering it was the same falsehood the
       Revoke was. A seeded default still has a real tier the admin may
       change, so it keeps the control and merely carries its origin. */
    if (o.managedBy && o.managedBy.revocable === false) {
      return `<span class="ax-ctl ax-ctl--managed">
        <span class="ax-managed" data-tip="${esc(o.managedBy.reason || "")}">${esc(o.managedBy.label)}</span></span>`;
    }
    return `<span class="ax-ctl">${tierPart}</span>`;
  };

  /* The row's ACT, in the column "What they will see" used to occupy. Revoke
     for a grant this page owns; the way to the owning surface for one it does
     not (an inherited Everyone grant, a Required plugin). Exactly one of the
     two, because a row either is yours to change or it is not. */
  /* The act on a memory-domain row is not a revoke. A memory_domain grant is
     ADDITIVE — it reveals the domain's items to this group and hides them
     from nobody else — so removing it takes no access away from anyone,
     which is precisely what a button labelled Revoke implies it does. The
     first live finding of the audit (F2) was an admin who would click Revoke
     here, read the success toast, and report a removal that had not
     happened. The label says the true act; the confirm and the toast below
     say what still applies. The permission model is unchanged by this —
     making the grant actually restrict is a separate decision, deferred. */
  const ACT_WORD = (typeKey) => (typeKey === "memory_domain" ? "Stop revealing" : "Revoke");
  const manageCell = (o) => {
    const m = o || {};
    const act = ACT_WORD(m.typeKey);
    if (m.inherited) {
      /* Carries the ROW as well as the destination. Landing on Everyone's
         whole list and leaving the reader to find the thing they were just
         looking at is most of the journey undone — Everyone is the longest
         list on the page by construction. `?resource=` is the same deep
         link /admin/tables already uses, so this needs no new mechanism:
         the list arrives filtered to that one row, and `scrollToPick()`
         brings it into view. */
      const at = (m.typeKey && m.resourceId)
        ? `&resource=${encodeURIComponent(`${m.typeKey}:${m.resourceId}`)}`
        : "";
      return `<a class="ax-inherit" href="?by=group&group=${esc(everyoneGroupId() || "")}${at}"
        title="Granted to Everyone, so it reaches this group's members too. Edit it on Everyone.">via Everyone →</a>`;
    }
    /* A grant another surface wrote. `revocable` is the honest split: several
       writers SEED a default an admin is expected to override (an MCP
       source's visibility, the chat seed for Everyone), and refusing a revoke
       there would be worse than saying nothing. Those keep Revoke and merely
       say where they came from; only the ones that genuinely re-assert — a
       Required plugin, the nightly sync, the SharePoint wizard — replace it
       with the way to their own surface. */
    if (m.managedBy && m.managedBy.revocable === false) {
      return `<a class="ax-inherit" href="${esc(m.managedBy.href)}">${esc(m.managedBy.surface)} →</a>`;
    }
    if (m.managedBy) {
      return `<button type="button" class="ax-revoke" data-revoke><svg class="ax-revoke__x" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>${act}</button>${
        m.managedBy.href ? `<a class="ax-own__edit" href="${esc(m.managedBy.href)}">${esc(m.managedBy.surface)} ↗</a>` : ""}`;
    }
    return `<button type="button" class="ax-revoke" data-revoke><svg class="ax-revoke__x" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>${act}</button>${
      m.href ? `<a class="ax-own__edit" href="${m.href}">${esc(m.hrefLabel || "where it lives ↗")}</a>` : ""}`;
  };

  /* The type, as one word on the row. `display_name` is a plural section
     header ("Skills, agents & plugins"); a chip in a 5rem column is not a
     header, and the row already says which thing it is. */
  const KIND_WORD = {
    data_package: "package", memory_domain: "memory", memory_item: "memory",
    semantic_model: "semantic", recipe: "recipe", collection: "collection",
    corpus_file: "file", knowledge_digest: "digest", data_app: "app",
    table: "table", marketplace_plugin: "plugin", store_entity: "skill",
    agent: "agent", chat: "chat", slack_channel: "slack", mcp_source: "mcp",
  };
  const kindWord = (t) => KIND_WORD[t.type_key] || t.type_display;

  /* The kind's colour comes from the design system's resource family
     (`--ds-kind-*`), which the Library and the detail pages already use — a
     package is the same colour here as it is there. Types with no resource
     colour (a chat, a Slack channel) stay neutral rather than borrowing one
     that means something else. */
  const KIND_TOKEN = {
    data_package: "data", table: "data", semantic_model: "data",
    memory_domain: "memory", memory_item: "memory",
    recipe: "recipe", collection: "library", corpus_file: "file",
    knowledge_digest: "file", data_app: "app",
    marketplace_plugin: "plugin", store_entity: "skill", agent: "agent",
    mcp_source: "plugin",   //: tools an agent calls — the plugin accent
  };
  const kindToken = (t) => KIND_TOKEN[t.type_key] || "";

  /* The kind chip is the shared `.ds-kindtag` now — same glyph, same colour
     and same corner as the Library's per-row kind mark, from one source
     (js/kind_glyph.js + the --ds-kind-* pair). This page uses the WORDED
     density because a group's holdings are of mixed kind, which is exactly
     the case the word exists for. */
  const kindTag = (t) => {
    const k = kindToken(t);
    const glyph = window.AgnesKindGlyph ? window.AgnesKindGlyph.get(k) : "";
    // `type_description` is the registry's own caveat for this kind — that a
    // table grant does NOT give an analyst visibility, that an MCP source
    // grant is ANDed with per-tool grants, that a memory-domain grant is
    // additive. The API has shipped it since the type registry existed
    // (app/api/access.py) and nothing rendered it, so the picker offered
    // Tables beside Data packages looking identical and behaving nothing
    // alike. At minimum it is the tag's tooltip, everywhere a tag appears.
    const caveat = t.type_description ? ` title="${esc(t.type_description)}"` : "";
    return `<span class="ax-r__kd ds-kindtag" data-kind="${esc(k)}"${caveat}>${glyph}<span>${esc(kindWord(t))}</span></span>`;
  };

  /* Who wrote this grant. Two writers, two shapes: the admin API records an
     email, the Library's owner-sharing path records a user id. Resolve both
     against the user list and fall back to whatever is stored — a raw value
     the admin can still recognise beats an empty cell.

     What this deliberately does NOT do is classify the writer as "admin" or
     "owner". One column cannot carry that: an admin can share from the
     Library, and an owner's grant and an admin's grant are the same row.
     Naming the person is true; naming their role would be a guess. */
  /* The sharer, as a name. The server resolves it now (`assigned_by_name`),
     because the fallback below only ever worked when the person lens had
     happened to load the user list — on the group list it had not, and a
     Library share read "shared by <uuid>" on the one page whose job is to
     say who. The old path stays for rows the server could not resolve. */
  function whoGranted(grant) {
    if (grant && grant.assigned_by_name && grant.assigned_by_name !== grant.assigned_by) return grant.assigned_by_name;
    const raw = grant && grant.assigned_by;
    if (!raw) return "";
    const hit = (users || []).find((u) => u.id === raw || u.email === raw);
    if (hit) return hit.name || hit.email || raw;
    return String(raw).includes("@") ? String(raw).split("@")[0] : raw;
  }

  /* `familyCounts()` stood here — `knowledge 1  capabilities 3  surfaces 0`
     on every row. It was written when a collapsed row was suspected of
     saying too little; it turned out to say too much of the wrong thing.
     Three numbers on every row, most of them zero, none of them the one an
     admin reads before deciding to open a group — that one is "N granted",
     two words to the left. The families still organise the list INSIDE a
     group, which is where the distinction earns its keep. */

  /* The panel is one node, moved. Re-rendering the list would otherwise
     destroy it (and every handler bound inside), so it is parked before the
     list is rewritten and re-homed after. */
  function parkWork() {
    const w = el("ax-work"), park = el("ax-work-park");
    if (w && park && w.parentElement !== park) park.appendChild(w);
  }
  function homeWork(gid) {
    const w = el("ax-work");
    const body = gid ? document.querySelector(`[data-gsbody="${window.CSS && CSS.escape ? CSS.escape(gid) : gid}"]`) : null;
    if (w && body) body.appendChild(w);
  }

  function renderGroups() {
    const host = el("ax-groups");
    parkWork();
    const all = sortedGroups();
    if (!all.length) {
      // The old copy sent the reader to the group list to create one. The
      // control is now the row directly below this message, so the empty
      // state points at it instead of at another page.
      host.innerHTML = `<div class="ax-empty">No groups yet — every grant needs one. Start with <b>New group</b> below.</div>`;
      paintCount(0, 0);
      return;
    }
    /* One search, and it reads both ways. Name, description and the
       Workspace address find a group; the name of anything the group HOLDS
       finds it too, because "who gets Revenue Core" is one of the three
       questions people arrive with and it used to answer "no group matches
       revenue" — technically true, useless, and the exact shape of the
       empty-vs-denied confusion this page should not add to. */
    const q = groupFilter.trim().toLowerCase();
    const hay = (g) => `${titleOf(g)} ${g.name || ""} ${g.description || ""} ${g.mapped_email || ""}`.toLowerCase();
    const itemText = new Map();
    for (const t of (overview.resources || [])) {
      for (const b of (t.blocks || [])) {
        for (const i of (b.items || [])) {
          itemText.set(`${t.type_key}:${i.resource_id}`,
            `${i.name || ""} ${i.slug || ""} ${i.resource_id || ""} ${i.owner_email || ""} ${b.name || ""} ${t.type_display || ""}`.toLowerCase());
        }
      }
    }
    const holdsMatch = (g) => grantsFor(g.id).some((gr) =>
      (itemText.get(`${gr.resource_type}:${gr.resource_id}`) || "").includes(q));
    const nameHit = (g) => hay(g).includes(q);
    /* …and the third question admins actually arrive with: "which groups is
       this person in?". The box has always PROMISED this — its placeholder
       says people — and never delivered it, so searching a colleague's
       address returned "0 of 5 groups" on an instance where they were in two.

       Matched against the roster the page already loads for the person
       lens's picker (cached, capped at 500, fetched on the first search
       rather than on every page load). `Everyone` matches any live account
       by construction, which is why it is not consulted by id. */
    /* The server's answer for THIS query, or nothing yet. `memberMatches` is
       filled by `fetchMemberGroups` (below, keyed by query) and the list
       repaints when it lands; until then a person-search simply has no
       member hits, which reads as "still looking" rather than as a wrong
       answer. `Everyone` is a hit whenever anyone matched at all — every
       live account is in it by construction. (Audit S2: the roster no longer
       ships, so there is nothing to match against locally.) */
    const mm = (memberMatches.q === q) ? memberMatches : null;
    const peopleFor = (g) => {
      if (!mm) return [];
      if (g.is_everyone) return mm.matched_people ? [{ name: `${mm.matched_people} matching ${mm.matched_people === 1 ? "person" : "people"}` }] : [];
      return mm.byGroup.get(g.id) || [];
    };
    const memberHit = (g) => q.length >= 2 && peopleFor(g).length > 0;
    /* A group survives the filters if ANY grant it holds does. Filtering
       groups by a property of their grants is the only reading that makes
       sense here — "show me the groups that hold something granted to
       everyone" — and it is what the count beside each option promises. */
    const kindHit = (g) => !anyFacetOn()
      || grantsFor(g.id).some((x) => rowPassesFacets(x.resource_type, [x]));
    /* The filter narrows the list it sits above, and in this view that list
       is GROUPS — so it shows the groups holding that kind, not just the
       rows inside a group somebody happens to have opened. Filtering rows
       nobody can see was the reason this control felt pointless here. */
    const groups = (q ? all.filter((g) => nameHit(g) || holdsMatch(g) || memberHit(g)) : all).filter(kindHit);
    if (!groups.length) {
      // A miss says it is a miss. An admin sees every grant on the instance,
      // so "nothing matched" can never be a permission problem here — and
      // saying so is the difference between refining a search and giving up.
      host.innerHTML = `<div class="ax-empty">Nothing here is called “${esc(groupFilter)}”.
        ${esc(String(all.length))} groups searched, and everything granted in them.</div>`;
      paintCount(0, all.length);
      return;
    }
    // Typing opens what it found: a hit inside a collapsed section reads as
    // "no such thing". The first match opens unless something already is.
    if (q && !groups.some((g) => g.id === selectedGroup)) {
      selectedGroup = groups[0].id;
      resourceFilter = nameHit(groups[0]) ? "" : groupFilter.trim();
    }
    paintCount(groups.length, all.length);

    const row = (g) => {
      const label = titleOf(g);
      const origin = g.origin || "custom";
      const members = g.member_count ?? 0;
      const grants = grantCountOf(g.id);
      // The two numbers that make a group list scannable, in the order the
      // page reads: who it reaches, then how much it carries. "0 granted" is
      // said rather than omitted — an empty group is the thing worth seeing.
      /* Everyone is not a group with a roster, and printing its member count
         here made the same false claim the other two renderers made: that a
         membership list decides this, and that someone leaving would change
         it. It is a SCOPE — every account, and anyone who joins — so the
         reach half of the line says that instead of counting it. The grant
         half is unchanged, because how much it carries is exactly as worth
         seeing here as it is for any other row.

         `is_everyone` is already on the payload (`everyoneGroupId()` reads it
         a few hundred lines up), so this needs nothing new from the server
         and is right on both app-state backends. */
      const meta = g.is_everyone
        ? `every account · ${grants} granted`
        : `${members} ${members === 1 ? "person" : "people"} · ${grants} granted`;
      /* The kebab is a SIBLING of the row button, positioned over its right
         edge — not a child. A <button> inside a <button> is invalid markup
         and the browser resolves it by breaking one of them; the wrapper
         gives the menu its positioning context and keeps both controls real.
         Only editable rows get one: `Admin` and `Everyone` are renamed and
         deleted where they are owned, in the seed or in Workspace. */
      const open = selectedGroup === g.id;
      /* A COLLAPSED row still has to answer something, or it is a shutter
         over hidden content rather than a summary. What it answers is now
         one line: the name, where the group comes from, and "N people · N
         granted".

         Three things came OFF it. The per-family counts — `knowledge 1
         capabilities 3 surfaces 0` on every row — were three numbers, mostly
         zeros, answering a question nobody asks before opening a group; the
         count that decides whether to open is already in `meta`. `created`
         was an age nobody sorts or filters by, printed to the minute. And
         the description moved rather than went: it is the only text on the
         page saying what a group is FOR, and it now sits in the people strip
         inside, where "who are these people" is the question it answers. */
      return `
      <details class="ax-gs${g.is_everyone ? " ax-gs--scope" : ""}" data-gs="${esc(g.id)}" ${open ? "open" : ""}>
        <summary class="ax-gs__hd" title="${esc(g.description || label)}">
          <span class="ax-gs__car" aria-hidden="true">›</span>
          ${AgnesKindGlyph.groupTile()}
          <span class="ax-gs__id ax-gs__id--g">
            <span class="ax-gs__line">
              <span class="ax-g__name">${esc(label)}</span>
              ${origin !== "custom" ? `<span class="ax-orig ax-orig--${esc(origin)}">${esc(origin.replace("_", " "))}</span>` : ""}
              <span class="ax-gs__reach">${esc(meta)}</span>
              ${q && !nameHit(g) ? `<span class="ax-gs__why">${
                memberHit(g)
                  ? `matched ${esc(peopleFor(g).slice(0, 2).map((u) => u.name || u.email).join(", "))}`
                  : "matched something it holds"}</span>` : ""}
            </span>
          </span>
          ${isEditable(g) ? `
          <span class="ax-grow">
            <button type="button" class="ax-gkebab" data-gmenu="${esc(g.id)}"
                    aria-haspopup="true" aria-expanded="false"
                    aria-label="Actions for ${esc(label)}">⋯</button>
            <ul class="ax-menu" data-gmenu-for="${esc(g.id)}" role="menu" hidden>
              <li role="none"><button type="button" role="menuitem" data-grename="${esc(g.id)}">Rename group</button></li>
              <li role="none"><button type="button" role="menuitem" class="danger" data-gdelete="${esc(g.id)}">Delete group</button></li>
            </ul>
          </span>` : ""}
        </summary>
        <div class="ax-gs__body" data-gsbody="${esc(g.id)}"></div>
      </details>`;
    };

    // Only label the halves when BOTH exist. On a fresh instance every group
    // is a system seed, and a lone "SYSTEM" heading over the only two rows
    // there are is a category that categorises nothing. A filter that
    // matches one half likewise gets no heading for the half it emptied.
    const custom = groups.filter((g) => !g.is_system);
    const system = groups.filter((g) => g.is_system);
    const label = (t) => `<div class="ax-glist__label">${t}</div>`;
    /* Custom groups first. System led while `Everyone` was the page's
       DEFAULT SELECTION — putting the row that opened on load at the top —
       and nothing is selected on arrival any more, so that reason went with
       it. What is left is that `Admin` and `Everyone` are the two rows an
       admin can neither rename, delete, nor (under Workspace mapping)
       change the membership of. The groups they actually work on come
       first; the two fixed ones sit at the foot, where a list you have read
       to the end ends. */
    /* The action that makes a group is the first row of the list of groups.
       (The Library keeps creation in its toolbar; here the list is the thing
       being added to, and a row on its own geometry says that better than a
       button above it.) Delegated, because this markup is rewritten on every
       repaint. */
    const newGroupRow = `
      <button type="button" class="ax-newrow" data-new-group>
        <span class="ax-newrow__plus" aria-hidden="true">+</span>
        <span class="ax-newrow__label">New group</span>
      </button>`;
    /* The everyone audience, first and outside the GROUPS / SYSTEM bands.
       First because it is the widest reach on the page and therefore the
       most consequential thing an admin can be wrong about; outside the
       bands because those label kinds of GROUP and this is not one.

       It carries the carrier's id in `data-gs` / `data-gsbody`, which is
       what lets the existing machinery work untouched: the disclosure
       handler sets `selectedGroup` from `data-gs` like any row, and
       `homeWork` moves the one Access panel into `data-gsbody`. So
       selecting it shows exactly the grants that reach every account, with
       their real tier controls and Revoke — the answer to "what has been
       given to everyone, and how do I change it" — without a second
       rendering path to keep in step with the first.

       No member count, no kebab: it cannot be renamed or deleted, and a
       roster number here is the claim three renderers were fixed to stop
       making. Its People panel is already honest for this case —
       `renderMembers` says membership is automatic and offers nothing to
       add or remove. */
    const cid = everyoneGroupId();
    const everyoneEntry = cid ? (() => {
      const open = selectedGroup === cid;
      const grants = grantCountOf(cid);
      return `
      <details class="ax-gs ax-gs--scope" data-gs="${esc(cid)}" ${open ? "open" : ""}>
        <summary class="ax-gs__hd" title="Every account on this instance, and anyone who joins">
          <span class="ax-gs__car" aria-hidden="true">›</span>
          ${AgnesKindGlyph.groupTile()}
          <span class="ax-gs__id ax-gs__id--g">
            <span class="ax-gs__line">
              <span class="ax-g__name">Everyone</span>
              <span class="ax-orig ax-orig--system">audience</span>
              <span class="ax-gs__reach">every account · ${grants} granted</span>
            </span>
            <span class="ax-gs__desc">Not a group — a scope. Anything here reaches every
              account, including anyone who joins later.</span>
          </span>
        </summary>
        <div class="ax-gs__body" data-gsbody="${esc(cid)}"></div>
      </details>`;
    })() : "";

    /* The action sits INSIDE the band it adds to, as that band's first row.
       It was above the GROUPS label — between the Everyone audience and the
       heading — where it belonged to neither: an admin scanning for the
       groups read past a control before reaching the word "Groups", and the
       control itself was inset while every row under it ran full width, so
       it read as a card floating over the list rather than the first line of
       it. Its hint went with the position: "an audience to write grants
       against" defines the word GROUP, which the band directly above it has
       just said. */
    host.innerHTML = everyoneEntry + ((custom.length && system.length)
      ? label("Groups") + newGroupRow + custom.map(row).join("")
        + label("System") + system.map(row).join("")
      : label("Groups") + newGroupRow + groups.map(row).join(""));
    homeWork(selectedGroup);
  }

  /* ── The group itself, pinned ─────────────────────────────────────────
     Name, upstream address, origin, purpose, age, and the lifecycle actions.
     This is the whole of the retired detail page's header — the reason that
     page had to exist once the members and the grants moved here — and it
     is STICKY, because the grant tree below it is tall enough that the name
     of the group being edited used to scroll off the screen. */
  function renderIdentity() {
    const group = selectedGroup
      ? (overview.groups || []).find((g) => g.id === selectedGroup)
      : null;
    const everyone = group && group.is_system && group.name === "Everyone";
    const label = group ? titleOf(group) : "";

    el("ax-what-title").textContent = group ? label : "Pick a group";

    const idsub = el("ax-what-idsub");
    const sub = subtitleOf(group);
    idsub.textContent = sub;
    idsub.hidden = !sub;

    const orig = el("ax-what-origin");
    const origin = group && group.origin ? group.origin : "";
    orig.textContent = origin ? origin.replace("_", " ") : "";
    orig.className = "ax-orig" + (origin ? ` ax-orig--${origin}` : "");
    orig.hidden = !origin;

    /* ONE fact strip. The member count used to be stated three times — a
       sentence, a row of avatars, and a disclosure label over the roster
       (which is simply the People section's body now) — and the
       grant count only in the left column. Both are facts about the subject
       in the header, so both belong to the subject, once. The reach is said
       as its CONSEQUENCE rather than as a number: an empty group is not a
       neutral state, it means everything below reaches nobody. */
    const meta = el("ax-what-meta");
    if (!group) {
      meta.innerHTML = "";
    } else {
      const people = group.member_count ?? 0;
      const grants = grantCountOf(group.id);
      const when = fmtDate(group.created_at);
      const reach = everyone
        ? `<b>Every account</b> on this instance`
        : people
          ? `<b>${people} ${people === 1 ? "person" : "people"}</b>`
          : `<span class="warn">Nobody</span>`;
      /* Empty by design. Reach and grants are on the group's own row, two
         words to the left of where this used to print them, and the purpose
         and age moved onto that row as its second line. What is left in this
         header is the Workspace address and the managed notice — the two
         things a row cannot say — and both render themselves below. */
      meta.innerHTML = "";
      void reach; void grants; void when;
    }

    // The standing explanation for every control Workspace owns.
    const managed = el("ax-what-managed");
    managed.innerHTML = group && group.is_google_managed
      ? `<aside class="info-panel-accent info-panel-accent--success" role="note">
           <div class="info-panel-accent__body">Membership is managed by Google Workspace — add or remove people at
           <a href="https://admin.google.com" target="_blank" rel="noopener">admin.google.com</a>, or sign in again to refresh.
           What this group can use is still granted here.</div>
         </aside>`
      : "";

  }

  /* The per-row action menu. It lives on the group it acts on, so renaming
     `finance` no longer means selecting `finance` first — a navigation to
     perform an edit the pane's contents have nothing to do with. Rows are
     re-rendered on every repaint, so the menu is addressed by group id
     rather than held as an element reference. */
  function closeGroupMenu() {
    document.querySelectorAll("[data-gmenu-for]").forEach((m) => { m.hidden = true; });
    document.querySelectorAll("[data-gmenu]").forEach((b) => b.setAttribute("aria-expanded", "false"));
  }
  function toggleGroupMenu(groupId) {
    const menu = document.querySelector(`[data-gmenu-for="${CSS.escape(groupId)}"]`);
    const btn = document.querySelector(`[data-gmenu="${CSS.escape(groupId)}"]`);
    if (!menu || !btn) return;
    const opening = menu.hidden;
    closeGroupMenu();   // one at a time
    if (!opening) return;
    menu.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    const first = menu.querySelector("button");
    if (first) first.focus();
  }

  /* `setAccessHead()` stood here, writing "5 granted · 2 via Everyone" into
     the Access section head. The head is gone, and so is the count: the
     group's own row already says how much it holds, and every inherited row
     is in the list below labelled "via Everyone →". A number summarising
     rows that are on screen is a third place for the same fact to disagree
     with itself. */

  /* One control. The header search filters the group list AND narrows what
     an open group shows — except when the group's own name is what matched,
     where narrowing its rows to the group name would empty a group you just
     found. Typing is also what opens things: a hit inside a collapsed
     section reads as "no such thing", which is the opposite of the answer. */
  function syncQuery() {
    const q = groupFilter.trim();
    if (PICK) return;                     // a ?resource= arrival asked for its own narrowing
    const g = (overview.groups || []).find((x) => x.id === selectedGroup);
    const nameHit = !!(g && `${titleOf(g)} ${g.name || ""} ${g.description || ""} ${g.mapped_email || ""}`
      .toLowerCase().includes(q.toLowerCase()));
    resourceFilter = (!q || nameHit) ? "" : q;
  }

  function renderResources() {
    renderIdentity();
    const host = el("ax-resources");
    if (!selectedGroup) {
      host.innerHTML = `<div class="ax-empty">Open a group to see what it can use.</div>`;
      return;
    }

    // Nothing grantable on the instance at all — a different problem from
    // "this group has nothing", and it has a different next step.
    const anyItems = (overview.resources || []).some(
      (t) => (t.blocks || []).some((b) => (b.items || []).length));
    if (!anyItems) {
      findWrap.hidden = true;
      host.innerHTML = `<div class="ax-empty">Nothing is registered to grant yet — add a
        <a href="/admin/data-packages">data package</a> or a
        <a href="/admin/marketplaces">marketplace</a> first.</div>`;
      return;
    }

    const types = (overview.resources || []).slice().sort((a, b) => {
      const al = a.type_key === LEAD_TYPE ? 0 : 1;
      const bl = b.type_key === LEAD_TYPE ? 0 : 1;
      return al - bl || String(a.type_display).localeCompare(String(b.type_display));
    });

    // The filter matches everything a person might type to find one item:
    // its name, its id (what a `?resource=` deep link carries), its
    // description, the block it sits in, and the kind it is.
    const fq = resourceFilter.trim().toLowerCase();
    const hits = (t, i) =>
      !fq || `${i.name || ""} ${i.slug || ""} ${i.resource_id || ""} ${i.description || ""} ${i.owner_email || ""} ${i._block || ""} ${t.type_display || ""}`
        .toLowerCase().includes(fq);
    // "What does this group actually HAVE?" — the question that otherwise
    // means opening every type in turn to find the two that hold anything.
    const narrowed = !!fq;

    //: Kinds an owner can share from the Library (app/services/library_sharing.py).
    //: Here they are OVERSIGHT — revoke, and a way to where they are owned —
    //: because the item's own page is the other editor and two editors for
    //: one row is how vocabularies and states drift apart.
    const OWNER_SHARED = new Set(["collection", "agent", "corpus_file", "data_app"]);

    //: Where a row's thing actually lives, when it has a page of its own.
    const ENTITY_PAGE = {
      collection: (id) => `/library/d/${encodeURIComponent(id)}`,
      corpus_file: (id) => `/library/d/${encodeURIComponent(id)}`,
      data_app: (id) => `/apps/${encodeURIComponent(id)}`,
      // /agents is the OWNER's builder, fed by /api/v1/agents (the caller's
      // own agents), so for an admin looking at a colleague's agent it opened
      // on nothing — a dead end labelled "where it lives". There is no admin
      // page for an agent; the agent lives with its owner, so the row's link
      // goes to the owner on /admin/users when the owner is known.
      // An agent lives with its owner, and the owner's People page now has a
      // Shares section — what they own, which groups can use it, and how to
      // change that. So the link lands on something that answers "what is
      // this and who has it". (/agents?agent=<id> is the owner's own builder
      // and shows an admin nothing — the dead end the owner found.)
      agent: (id, item) => (item && item.owner_user_id)
        ? `/admin/users/${encodeURIComponent(item.owner_user_id)}#shares`
        : "",
      // Carries the group out with it. The person lens already round-trips
      // (`?from=simulate&user=`) so a package page opened from an audit says
      // whose audit and offers the way back; the group lens did not, so the
      // same page fell through to its hard-coded "← Packages" and rendered
      // every group the package is shared with — you filtered to one group
      // and it showed you all of them.
      data_package: (id) => `/admin/data-packages/${encodeURIComponent(id)}`
        + (selectedGroup ? `?from=access&group=${encodeURIComponent(selectedGroup)}` : ""),
      table: (id) => `/catalog/t/${encodeURIComponent(id)}`,
    };

    /* ONE row shape, and the open group shows only what the group HAS.
       Ungranted rows with a dead control and the words "not granted" were
       noise: every row looked like a control and most were not one, which is
       what made the page unreadable. Adding is an explicit act now (+ Add),
       so there is nothing on screen to tick and no checkbox column — the
       control column IS the affordance. */
    const grantedRow = (t, i, blockName, grant) => {
      const tier = grant.requirement === "required" ? "required" : "available";
      const owned = OWNER_SHARED.has(t.type_key);
      const hrefFn = ENTITY_PAGE[t.type_key];
      const name = esc(itemName(i));
      const nameHtml = hrefFn ? `<a href="${hrefFn(i.resource_id, i)}">${name}</a>` : name;
      const who = whoGranted(grant);
      // An inherited row is NOT this group's to change: the tier control and
      // Revoke both act on the Everyone grant, so offering them here would
      // let an admin quietly alter a company-wide grant while looking at one
      // group. Say where it comes from and send them there instead.
      const ownHref = owned && hrefFn ? hrefFn(i.resource_id, i) : "";
      // What the link is FOR, by kind. "where it lives" was right for a
      // collection (its Library page) and wrong for an agent, whose only
      // reachable home for an admin is its owner.
      const ownLabel = t.type_key === "agent" ? "owner ↗" : "where it lives ↗";
      // An inherited row is NOT this group's to change: the tier control and
      // Revoke both act on the Everyone grant, so offering them here would
      // let an admin quietly alter a company-wide grant while looking at one
      // group. Say where it comes from and send them there instead.
      // Inherited: the tier is real but it is Everyone's to set, so it is
      // stated rather than offered.
      //
      // It says where it is set, in the cell. The bare word alone sat in the
      // same column as a live control one row below and read as a setting
      // that had stopped working — the reason it cannot be touched here was
      // real but lived in a tooltip, which is to say nowhere. The Manage
      // column does carry the way there; a reader should not have to join
      // two columns to find out why the first one is inert.
      const control = grant.inherited
        ? `<span class="ax-ctl ax-ctl--managed"><span class="ax-managed"
             data-tip="Granted to Everyone, so it reaches this group's members too. Its tier is set there.">${
               tier === "required" ? "Automatic" : "Optional"}<span class="ax-managed__src"> · via Everyone</span></span></span>`
        : controlCell(t.type_key, tier, { href: ownHref, managedBy: grant.managed_by, publisherKind: i.publisher_kind });
      const manage = manageCell({
        inherited: grant.inherited, managedBy: grant.managed_by, href: ownHref, hrefLabel: ownLabel,
        typeKey: t.type_key, resourceId: i.resource_id,
      });
      return `
      <div class="ax-r" data-kind="${esc(kindToken(t))}" data-type="${esc(t.type_key)}" data-rid="${esc(i.resource_id)}"${grant.inherited ? ' data-inherited="1"' : ""}>
        ${kindTag(t)}
        <span class="ax-r__nm">${nameHtml}${blockName ? `<span class="ax-r__blk"> · ${esc(blockName)}</span>` : ""}
          <span class="ax-r__d">${(() => {
            /* Built as a list rather than nested ternaries: the separator
               logic was already the hard-to-read part of this row with two
               possible chunks, and provenance makes three. */
            const bits = [];
            /* On a row someone shared from the Library, WHO shared it is the
               fact the row exists to carry — and it sat last, in grey, after
               the description and the file count. It leads now, as a name,
               with the owner beside it when the two differ. (Audit U7.) */
            // Sharer and owner are the same person when the grant's author IS
            // the item's owner — compared by id, not by guessing from names.
            const sharerIsOwner = owned && who && !!i.owner_user_id && grant.assigned_by === i.owner_user_id;
            /* Two different acts, two different sentences. The OWNER sharing
               their own item is "Shared by Ada". An admin granting Ada's item
               to a group is not a share — Ada was not in the loop — so that
               row says who granted it AND whose it is. The first cut said
               "Shared by <admin>" whenever the kind was owner-shared, which
               credited the owner with a decision they did not make. */
            if (owned && who && sharerIsOwner) {
              bits.push(`<span class="ax-r__shared">Shared by <b>${esc(who)}</b></span>`);
            } else if (owned && who) {
              bits.push(`<span class="ax-r__shared">Granted by <b>${esc(who)}</b>${
                i.owner_email ? ` · owned by ${esc(i.owner_email)}` : ""}</span>`);
            }
            if (i.description) bits.push(esc(String(i.description).slice(0, 120)));
            const prov = itemProvenance(i, { ownerNamed: !!(owned && who) });
            if (prov) bits.push(prov);
            /* An MCP source grant is necessary but not sufficient — it is
               ANDed with per-tool grants set on the source's own page. The
               row used to imply completeness it could not deliver; now it
               says the second condition and where it is set. (Audit F6.)
               "0 of 12" is the state worth seeing most: the server is
               visible and nothing in it can be used. */
            if (t.type_key === "mcp_source" && !grant.inherited) {
              const tc = (overview.mcp_tool_grants || {})[i.resource_id];
              if (tc) {
                const n = (tc.by_group || {})[selectedGroup] || 0;
                const label = tc.total
                  ? `${n} of ${tc.total} ${tc.total === 1 ? "tool" : "tools"} granted`
                  : "no tools registered yet";
                bits.push(`<a class="ax-inherit ax-r__tools${tc.total && !n ? " ax-r__tools--none" : ""}" href="/admin/mcp-sources/${encodeURIComponent(i.resource_id)}" title="Tools are granted per tool on the server's own page">${esc(label)} →</a>`);
              }
            }
            if (who && !owned) bits.push(`<span class="ax-r__who">granted by ${esc(who)}</span>`);
            return bits.join(`<span class="ax-r__sep"> · </span>`);
          })()}</span>
        </span>
        <span class="ax-r__ctl">${control}</span>
        <span class="ax-r__rd ax-r__manage">${manage}</span>
      </div>`;
    };

    /* Which of the two sections a row belongs in.
       `grant.section` is the server's answer (`src/grant_sources.py::
       section_for`), keyed on whether the writing surface RE-ASSERTS the
       row — so a revoke on a `marketplace_sync` row comes back tonight.
       Inheritance is the one case that field cannot describe: a via-Everyone
       row is a client-side projection of Everyone's OWN grant, so it has no
       source of its own, and the axis here is "can the admin act on this
       row, here", which an inherited row fails — the act belongs on
       Everyone. Its control already says `via Everyone` and its Manage cell
       already points there; the section says it once at the top instead of
       twice on every row. */
    /* The selected audience is not always a group now (decision 04), and
       three strings in this list name their subject. Saying "group" over
       the everyone entry is the same category error the row treatments were
       fixed for, one level up: it tells the reader the thing they are
       editing has members. */
    const scopeSelected = !!selectedGroup && selectedGroup === everyoneGroupId();
    /* The Admin group's grants are inert in the mode an admin is in while
       reading this page, and load-bearing in the one they cannot be in
       while reading it: god-mode short-circuits every access check, and only
       when a person pauses their own elevation (/me/profile) do their
       explicit grants apply — at which point every /admin page, this one
       included, answers 403. So the rows below can never be seen in the
       state where they matter, and nothing said so. (Audit F7 — reduced from
       the finding as written, whose scenario a paused admin cannot reach.)
       One sentence, here, where the tier is chosen; not a banner. */
    const _selGrp = (overview.groups || []).find((g) => g.id === selectedGroup);
    const adminSelected = !!_selGrp && (_selGrp.is_admin === true || _selGrp.name === "Admin");
    /* The generic halves of this went with the Access head — "What everyone
       above can use" was a caption for the strip above it. The Admin case
       stays: it is the page's only statement that the elevation mode exists
       and that Admin's grants apply only inside it, which is ticket 14's
       whole resolution. Hidden rather than emptied, so an ordinary group
       pays no height for it. */
    const subEl = el("ax-access-sub");
    if (subEl) {
      subEl.textContent = adminSelected
        ? "These are what admins can use with Admin mode paused. While it is on, god-mode reaches everything regardless of them."
        : "";
      subEl.hidden = !adminSelected;
    }

    const sectionOf = (grant) =>
      grant.inherited ? "set_elsewhere" : (grant.section || "change_here");

    let inheritedN = 0;
    const byFamily = new Map();
    for (const t of types) {
      const rows = [];
      for (const b of (t.blocks || [])) {
        const blockName = (b.name && b.name !== t.type_display) ? b.name : "";
        for (const i of (b.items || [])) {
          const grant = grantOf(selectedGroup, t.type_key, i.resource_id);
          if (!grant) continue;             // the holding, not the catalogue
          if (facetOn("kind") && !facetHas("kind", t.type_key)) continue;
          if (!hits(t, i)) continue;
          /* An inherited row is COUNTED here, not rendered. Every everyone-wide
             grant used to appear in every group's list as its own row, each
             labelled with the reason it was there — a production screenshot
             read "5 granted · 86 via Everyone" over page after page of rows
             the group did not hold. The explanation had become the noise,
             and what the group itself holds — the reason the admin opened it
             — was buried under what everyone holds. (Audit S3.) Counted AFTER
             the kind and search filters, so the summary never claims rows a
             filter hid. They surface as one line at the end of Set elsewhere,
             pointing at the Everyone audience, which is where they can be
             acted on anyway. */
          if (grant.inherited) { inheritedN += 1; continue; }
          rows.push({ html: grantedRow(t, i, blockName, grant), sec: sectionOf(grant) });
        }
      }
      if (!rows.length) continue;
      const key = t.family || "knowledge";
      if (!byFamily.has(key)) byFamily.set(key, []);
      byFamily.get(key).push(...rows);
    }

    const families = (overview.families || []).length
      ? overview.families
      : [{ key: "knowledge", display_name: "Knowledge", blurb: "" }];
    /* ONE list, in family order — no band per family.
       A group holding three things was three headed sections of one row each,
       and each header spent a line restating what its family means ("Read,
       query or open — and what their agents may cite"). That is a definition
       of the taxonomy, printed inside the answer to "what does Finance have",
       which is not the question it answers. Worse, the same split is already
       available as a CHOICE: the toolbar's Kind filter narrows to exactly one
       family, on demand, for the reader who wants that cut.
       Family order is kept — the rows still arrive Knowledge, then
       Capabilities, then Surfaces — so nothing about the reading order
       changes; only the furniture between them is gone. */
    /* Two sections, split on whether the admin can act on the row HERE.
       Family order is kept inside each, so the reading order within a
       section is the one the flat list had.

       Labelled only when BOTH sections have rows — the same rule the group
       list above applies to GROUPS / SYSTEM, for the same reason: a lone
       heading over every row there is categorises nothing. That holds in
       both directions here, including the all-`set_elsewhere` group, and it
       holds because the label is not what explains an unactionable row —
       each one already carries its own `via Everyone →` or owning-surface
       link in the Manage column, on the row, where the reader is looking
       when they wonder why there is no Revoke. The label earns its place
       only as the DIVIDER between two kinds of row, which is precisely when
       both exist. */
    const inSection = (want) => families
      .map((f) => (byFamily.get(f.key) || [])
        .filter((r) => r.sec === want).map((r) => r.html).join(""))
      .join("");
    const changeHere = inSection("change_here");
    /* The inherited rows, as one line where they used to be many. Shaped as a
       row so it sits in the grid; the act is the same `?group=` link the
       per-row `via Everyone →` used, so nothing about where to go changes —
       only how many times the page says it. Never rendered on the Everyone
       entry itself: `grantsFor` returns only direct rows there, so the count
       is zero by construction. */
    const inheritedLine = inheritedN
      ? `<div class="ax-r ax-r--inherited-sum" data-inherited-summary="${inheritedN}">
          <span class="ax-r__kd"></span>
          <span class="ax-r__nm">and everything Everyone has
            <span class="ax-r__d">${inheritedN} ${inheritedN === 1 ? "grant that reaches" : "grants that reach"} every account, this group included</span>
          </span>
          <span class="ax-r__ctl"></span>
          <span class="ax-r__rd ax-r__manage"><a class="ax-inherit" href="?by=group&group=${esc(everyoneGroupId() || "")}">Everyone →</a></span>
        </div>`
      : "";
    const setElsewhere = inSection("set_elsewhere") + inheritedLine;
    const sectionLabel = (t, hint) =>
      `<div class="ax-glist__label ax-gsect">${esc(t)}${
        hint ? `<span class="ax-gsect__hint">${esc(hint)}</span>` : ""}</div>`;
    /* "Change here" is gone as a band. It sat BELOW the column header,
       which put a section heading inside the table it was meant to head,
       and it named the section the "+ Add to this group" row already heads
       — that row sits directly above the header, is the only thing on the
       page that writes a grant, and says what this section is by being
       there. "Set elsewhere" stays: it marks rows a revoke here cannot
       remove, which nothing else on screen says. */
    const sections = changeHere
      + (setElsewhere
        ? sectionLabel("Set elsewhere",
            "A revoke here cannot remove these — Manage points at the surface that owns them.")
        : "")
      + setElsewhere;

    /* Above the list, not after it. At the foot it cost a scroll through
       everything the group already has to reach the thing that adds one
       more — and the more a group holds, the further the control retreats,
       which is exactly backwards. */
    const addRow = `
      <button type="button" class="ax-add" data-add-grant>
        <span class="ax-add__plus" aria-hidden="true">+</span>
        <span class="ax-add__body">
          <span class="ax-add__label">${scopeSelected ? "Add for everyone" : "Add to this group"}</span>
        </span>
      </button>`;

    // The column header belongs to the GROUP, not to each family. Repeated
    // per family it was three copies of four words, and it made each family
    // read as its own table rather than a band of one list.
    /* "What they will see" is gone. It printed the CONSEQUENCE of the tier
       ("In their Library / Keep a local copy"), which is a function of the
       Optional/Automatic choice and not of the row — so the same two strings
       repeated down the column, a legend rendered once per row. The meaning
       moves onto the tier control itself, where the choice is actually made
       and where a reader is already looking when they wonder what it does.

       The column it vacated goes to the ACT: revoke, or the way to the
       surface that owns this grant when it is not this page (a Required
       plugin, or an inherited Everyone grant). Those were previously crowded
       into the same cell as the tier pair. */
    const colhd = sections ? `
      <div class="ax-colhd">
        <span>Kind</span><span>${scopeSelected ? "What every account gets" : "What the group gets"}</span>
        <span>Access tier <span class="ax-tip ax-colhd__key"><span class="ax-tip__btn" tabindex="0" role="img" aria-label="What Automatic and Optional mean" aria-describedby="ax-tierkey-body">i</span></span></span><span class="ax-colhd__u">Manage</span>
      </div>` : "";

    /* Advanced is gone too. It was the same browsing tree one disclosure
       deeper, and it made "where do I grant something" have two answers.
       Granting is one act now: + Add, a picker, Apply. Grants written one
       resource at a time still work and still show — they are held, so they
       are rows here like anything else. */

    // The empty case has two causes and two different next steps: a filter
    // that matched nothing, or a group that simply holds nothing yet.
    /* Action first, then the header, then the rows: the header labels the
       ROWS, and the action is not one of them — it makes one. */
    /* The empty case has two causes and two different next steps, and only
       one of them is "clear the filter".

       A group that simply holds nothing is the state a NEW group is in, so
       it is the one place the Add control matters most — and it was the one
       place the control was not rendered at all. The copy was worse: "Tick
       anything below" described a previous design where this section listed
       everything grantable with checkboxes, so it pointed at a UI that no
       longer exists, and the "show everything" link beside it cleared a
       filter that was not set, which made it a no-op. Both are gone; the
       empty state now carries the action it is asking for. */
    const table = sections ? `<div class="ax-table">${addRow}${colhd}${sections}</div>` : "";
    const painted = table || (fq
      ? `<div class="ax-empty">Nothing matches “${esc(resourceFilter)}”.
         <button type="button" class="ax-linkbtn" data-clear-rfind>Show everything</button></div>`
      : `<div class="ax-table">
           <div class="dsec-empty"><strong>This group has nothing granted yet</strong>
           Nobody in it can reach anything until something is added.</div>
           ${addRow}
         </div>`);
    host.innerHTML = painted;
    paintBucketBoxes();
  }

  /* `indeterminate` is a DOM property, not an attribute — it cannot be set
     from the markup string above, so every render re-applies it. This is the
     state that makes a bucket box honest: "some of this bucket is granted"
     is neither on nor off, and drawing it as off would make the control read
     as "grant this bucket" when half of it is already granted. */
  function paintBucketBoxes() {
    document.querySelectorAll("[data-bucket]").forEach((box) => {
      const state = box.dataset.state;
      box.indeterminate = state === "some";
      box.checked = state === "all";
    });
  }

  /* `scope` is the audience when the audience is not a group. Passing
     `"everyone"` is what decision 04 turned into an explicit act: reaching
     every account used to be a hidden membership fact (a group that happened
     to hold everybody) or a flag on a plugin, and is now a choice made at
     grant time and recorded on the row.

     `group_id` is still sent and still required — the column is NOT NULL, and
     the unique key on it is what keeps one everyone-grant per resource. The
     server does not trust whatever we send for it on a scoped grant: the
     repository's `_carrier_or` forces the seeded carrier, so the audience
     cannot be smuggled in through the group field. We send the carrier
     anyway, because a request that means what it says is easier to read in a
     log than one that relies on being overridden. */
  /* The model this page holds is a COPY, and two admins can hold two. Every
     write used to be applied to the copy optimistically — the requested tier
     stored as if it were the server's, a deleted row's 404 swallowed as "fine,
     already gone" — so two people could each see their own answer until a
     reload, and confidently report contradictory access states. On a
     permissions surface that is worse than an error. (Audit E2.)

     Full conflict detection is not needed; knowing the copy is stale is.
     Three things do that: a write takes the SERVER's row back, a 404 on a
     row we still show means someone else changed it and we refetch and say
     so, and returning to the tab refetches if the copy is old enough to
     matter. Extracted from the inline copy the group-delete path had, so the
     page has one way to refresh its model rather than five. */
  let _overviewFetchedAt = Date.now();
  async function refetchOverview() {
    try {
      const r = await fetch(OVERVIEW_API, { credentials: "include" });
      if (!r.ok) return false;
      overview = await r.json();
      _overviewFetchedAt = Date.now();
      return true;
    } catch (e) { return false; }
  }
  //: The server's row, made a row this page can render: the three fields the
  //: overview computes per grant and a GrantResponse does not carry. A row
  //: this page just wrote is by definition changeable here and machine-free.
  function _rowFromResponse(g) {
    const cid = everyoneGroupId();
    return Object.assign({}, g, {
      managed_by: g.managed_by ?? null,
      section: g.section ?? "change_here",
      audience: g.audience ?? ((g.scope === "everyone" || (cid && g.group_id === cid)) ? "everyone" : g.group_id),
    });
  }
  async function changedElsewhere(what) {
    await refetchOverview();
    toast(`${what} was changed by someone else — the page has been refreshed.`, false);
    await repaint();
  }

  async function writeGrant(type, resourceId, requirement, groupId, scope) {
    const gid = scope === "everyone" ? (everyoneGroupId() || groupId) : (groupId || selectedGroup);
    const r = await fetch(GRANTS_API, {
      method: "POST", credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ group_id: gid, resource_type: type, resource_id: resourceId, requirement,
                             ...(scope ? { scope } : {}) }),
    });
    if (!r.ok) {
      const b = await r.json().catch(() => ({}));
      throw new Error(typeof b.detail === "string" ? b.detail : `HTTP ${r.status}`);
    }
    const created = await r.json();
    /* The optimistic row must carry `audience` too, or the renderers that
       read it (the audience row, the collapsed reach line, the section
       split) treat a freshly-written everyone grant as an ordinary group
       grant until the next full repaint — which is the exact false claim
       those renderers were fixed to stop making. */
    /* The server's row, not our guess at it. The tier we asked for and the
       tier that was written can differ (a store entity refused Automatic, a
       default applied), and the optimistic row used to record the ask. */
    overview.grants.push(_rowFromResponse(created));
  }

  async function updateGrant(grant, requirement) {
    const r = await fetch(`${GRANTS_API}/${encodeURIComponent(grant.id)}`, {
      method: "PUT", credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ requirement }),
    });
    if (r.status === 404) {
      // The row we are showing no longer exists: a colleague revoked it.
      // Applying our change to a copy of a deleted row would show a tier on
      // a grant nobody has any more.
      await changedElsewhere("That grant");
      throw new Error("changed_elsewhere");
    }
    if (!r.ok) {
      const b = await r.json().catch(() => ({}));
      throw new Error(typeof b.detail === "string" ? b.detail : `HTTP ${r.status}`);
    }
    const saved = await r.json().catch(() => null);
    // The server's answer, not the one we asked for.
    grant.requirement = saved && saved.requirement ? saved.requirement : requirement;
    if (saved && "scope" in saved) grant.scope = saved.scope;
  }

  /* A grant this page did not make, and cannot unmake. Marking a plugin
     "Required" on /admin/marketplaces writes a grant to EVERY group, and the
     API then refuses to delete those (409 `cannot_revoke_system_grant`) so
     nobody punches a hole in a mandatory plugin. The refusal is correct; what
     reached the admin was not. This function threw `HTTP 409` without reading
     the body, so the toast read "Could not revoke: HTTP 409" — a dead end with
     no statement of which surface owns the control. That is the smallest, most
     literal instance of the complaint in #1956 item 13: you cannot tell where
     a thing is set.

     Translated here rather than at the call sites: all three of them already
     print `err.message`, so one honest message serves every path. The wider
     fix — recording WHICH surface wrote each grant, so the page can say this
     without a hardcoded case — is argued for in the companion design-note
     PR, and needs a schema change this does not. */
  const GRANT_REFUSALS = {
    cannot_revoke_system_grant:
      "this plugin is marked Required, which grants it to every group. "
      + "Turn Required off on Admin → Content → Marketplaces first, then revoke it here.",
  };

  async function deleteGrant(grant) {
    const r = await fetch(`${GRANTS_API}/${encodeURIComponent(grant.id)}`, {
      method: "DELETE", credentials: "include",
    });
    if (r.status === 404) {
      // Already gone — but not by our hand, and the page was still showing
      // it. That is the fact worth surfacing, not swallowing as success:
      // the admin is about to report "removed" for something a colleague
      // removed a minute ago, and their copy of everything else may be as
      // stale as this row was.
      await changedElsewhere("That grant");
      throw new Error("changed_elsewhere");
    }
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      const code = typeof body.detail === "string" ? body.detail : "";
      throw new Error(GRANT_REFUSALS[code] || code || `HTTP ${r.status}`);
    }
    overview.grants = overview.grants.filter((g) => g.id !== grant.id);
  }

  /* ── People ───────────────────────────────────────────────────────────
     The roster IS the section body. It used to be a disclosure under a row
     of avatars under a sentence — three read-outs of one number, none of
     which was the list itself. The count now lives in the section head and
     in the sticky identity strip; what the body owes the reader is WHO,
     with where each of them came from.

     Fetched per selected group (small, and always current — a cached roster
     is the thing that would make "is Maria in Finance?" answerable and
     wrong). Google-managed groups render read-only with the reason, since
     Workspace owns their membership and the API refuses writes on them. */

  const membersCache = new Map();
  let memberIds = new Set();      // the selected group's roster, for the search
  // user_id → where the membership came from. The search rows need it for the
  // same reason the roster does: Remove is only ours to offer on admin-added
  // membership, and a button that always 4xxs is worse than no button.
  let memberSource = new Map();

  const SOURCE_LABEL = {
    admin: "added by admin",
    google_sync: "synced from Google",
    system_seed: "system-managed",
  };

  /* The people strip: faces, the count, and what the group is FOR.

     `sub` used to be the constant "Who this group reaches." under a section
     heading, which is a caption for the word People rather than anything
     about this group. The group's own description says something only this
     group can say, and it has nowhere else to be now that the list row is
     one line — so it wins here whenever there is one, and the generic line
     is what a group without a description falls back to rather than a gap.
     A caller that passes an explicit `sub` (loading, a failed read) still
     outranks both: a state beats a standing description. */
  function setPeopleHead(text, sub, faces) {
    const sum = el("ax-people-sum");
    if (sum) sum.textContent = text || "";
    const s = el("ax-people-sub");
    if (s) {
      const g = (overview && overview.groups || []).find((x) => x.id === selectedGroup);
      const own = g && String(g.description || "").trim();
      s.textContent = sub || own || "Who this group reaches.";
      s.classList.toggle("is-own", !sub && !!own);
    }
    // Every caller passes the faces it knows about — including the ones that
    // know there are none (no group open, `Everyone`, still loading), which is
    // what keeps a previous group's faces from lingering on the next one.
    const f = el("ax-people-faces");
    if (f) f.innerHTML = faces || "";
    /* The strip IS the control now, so there is no word to keep in step with
       what opening it can do — a caret says "this opens" and claims nothing
       about what you may do inside. (It carried "Manage" / "Show" / "Hide",
       which had to be chosen per group, kept in step with the open state,
       and still sat 900px from the row it belonged to.) */
    const more = el("ax-sec-people");
    if (more) more.hidden = !selectedGroup;
  }

  async function renderMembers() {
    const host = el("ax-members");
    if (!selectedGroup) {
      host.innerHTML = "";
      memberIds = new Set();
      memberSource = new Map();
      setPeopleHead("", "");
      return;
    }
    const group = (overview.groups || []).find((g) => g.id === selectedGroup);
    const everyone = group && group.is_system && group.name === "Everyone";

    // `Everyone` has automatic membership — every account is in it by
    // construction, so there is no audience to shape and nothing to add.
    if (everyone) {
      memberIds = new Set();
      memberSource = new Map();
      const _n = group.member_count ?? 0;
      setPeopleHead(`${_n} ${_n === 1 ? "account" : "accounts"}`,
                    "Every account on this instance, automatically.");
      host.innerHTML = `
        <div class="ax-res__msg">Membership is automatic — every account on this instance
        is in this group by construction, so there is nothing to add or remove.
        <a href="/admin/users">All people →</a></div>`;
      return;
    }

    setPeopleHead("Loading…", "Reading who is in this group…");
    host.innerHTML = `<div class="ax-res__msg">Loading who is in this group…</div>`;
    /* A FAILED read is not an empty group (#2140).

       This used to be `r.ok ? await r.json() : []` with a `catch` that also
       said `[]`, so a 500, a dropped connection and a genuinely empty group
       arrived here as the same value — and the empty state below is not a
       shrug, it is a policy assertion: "anything granted under Access
       reaches no one until someone is added". A live instance showed it
       under a header reading "6 people · 7 granted", an inch apart on the
       same screen. An admin who believes the empty state re-adds members or
       widens grants to compensate; one who believes the header concludes the
       opposite. On the page whose job is answering who can use what, the two
       sources of truth disagreed, confidently.

       So the failure keeps its own value, and — this is the half that made
       it stick — it is NOT cached. Caching `[]` meant collapsing and
       reopening the group could not recover: one bad response poisoned the
       group for the rest of the sitting. */
    let members = membersCache.get(selectedGroup);
    let membersFailed = false;
    if (!members) {
      try {
        const r = await fetch(`/api/admin/groups/${encodeURIComponent(selectedGroup)}/members`, { credentials: "include" });
        if (r.ok) {
          members = await r.json();
          membersCache.set(selectedGroup, members);
        } else {
          membersFailed = true;
          members = [];
        }
      } catch (e) {
        membersFailed = true;
        members = [];
      }
    }
    if (selectedGroup !== (group && group.id)) return; // selection moved on
    memberIds = new Set(members.map((m) => m.user_id));
    memberSource = new Map(members.map((m) => [m.user_id, m.source]));

    const managed = group && group.is_google_managed;
    const inactive = members.filter((m) => m.active === false).length;
    const active = members.length - inactive;
    setPeopleHead(
      membersFailed ? "Unknown"
        : members.length ? `${members.length} ${members.length === 1 ? "member" : "members"}` : "Nobody",
      membersFailed ? "This group's members could not be read." : "",
      membersFailed ? "" : peopleFaces(members));

    // The find box comes FIRST in the body: at any real group size, "is
    // Maria in here?" is asked more often than the whole list is read, and
    // the answer arrives in a popover that does not move the list below it.
    const find = managed
      ? ""
      : `<div class="ax-find-wrap">
           <div class="fbar__search ax-people__find">
             <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="11" cy="11" r="7" stroke="currentColor" stroke-width="2"/><path d="m16.5 16.5 4 4" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
             <input type="search" id="ax-find" autocomplete="off" role="combobox"
                    aria-expanded="false" aria-controls="ax-find-out" aria-autocomplete="list"
                    placeholder="Add someone, or check if they are in ${esc(titleOf(group) || "this group")}…"
                    aria-label="Find a person to add to or remove from this group">
           </div>
           <div class="ax-pop" id="ax-find-out" role="listbox" hidden></div>
         </div>`;

    /* Reserved for a group the server POSITIVELY reports as empty. The
       sentence makes a claim about who can reach what, and it may only be
       made from an answer. */
    if (membersFailed) {
      host.innerHTML = `
        <div class="dsec-empty">
          <strong>Couldn’t load who is in this group</strong>
          The grants below still apply — this is the member list failing, not
          the group emptying.
          <button type="button" class="ax-linkbtn" data-retry-members>Try again</button>
        </div>`;
      return;
    }
    if (!members.length) {
      host.innerHTML = find + `
        <div class="dsec-empty">
          <strong>Nobody is in this group yet</strong>
          Anything granted under Access reaches no one until someone is added.
        </div>`;
      return;
    }

    const warn = inactive
      ? `<div class="ax-res__msg">${inactive} of these ${inactive === 1 ? "account is" : "accounts are"}
         deactivated and ${inactive === 1 ? "gets" : "get"} nothing.</div>`
      : "";

    host.innerHTML = peopleStrip(members) + find + warn + rosterHtml(members);
  }

  /* ── The audience, at a glance ────────────────────────────────────────
     Faces and provenance in one line, above the roster. `initialsOf` reads
     a name when there is one and falls back to the address — never to a
     question mark, which is what an empty avatar looks like it means. */
  function initialsOf(m) {
    const name = String(m.name || "").trim();
    if (name) {
      const parts = name.split(/\s+/).filter(Boolean);
      return (parts.length > 1 ? parts[0][0] + parts[parts.length - 1][0] : parts[0].slice(0, 2))
        .toUpperCase();
    }
    return String(m.email || m.user_id || "?").replace(/[^A-Za-z0-9]/g, "").slice(0, 2).toUpperCase();
  }

  const AVATARS_SHOWN = 5;

  /* The faces, for the section header. Five, then a count — past five the
     initials stop being recognisable one by one and start being a texture,
     and the texture is what "+9" says in less room. */
  function peopleFaces(members) {
    if (!members.length) return "";
    const shown = members.slice(0, AVATARS_SHOWN);
    const rest = members.length - shown.length;
    return shown.map((m) =>
      `<span class="ax-av" title="${esc(m.email || m.user_id || "")}">${esc(initialsOf(m))}</span>`
    ).join("") + (rest > 0 ? `<span class="ax-av ax-av--more">+${rest}</span>` : "");
  }

  function peopleStrip(members) {
    if (!members.length) return "";
    // Provenance in the order it matters: what an admin put here, what
    // Google puts here, what the instance puts here. A source with nobody in
    // it is left out rather than printed as a zero.
    const PHRASE = {
      admin: (n) => `<b>${n}</b> added by an admin`,
      google_sync: (n) => `<b>${n}</b> synced from Google`,
      system_seed: (n) => `<b>${n}</b> system-managed`,
    };
    const tally = new Map();
    for (const m of members) tally.set(m.source, (tally.get(m.source) || 0) + 1);
    const facts = Object.keys(PHRASE)
      .filter((k) => tally.get(k))
      .map((k) => PHRASE[k](tally.get(k)));
    for (const [src, n] of tally) {
      if (!PHRASE[src]) facts.push(`<b>${n}</b> from ${esc(src || "an unknown source")}`);
    }
    const off = members.filter((m) => m.active === false).length;
    if (off) facts.push(`<b>${off}</b> deactivated`);

    /* One bucket holding everyone is not a breakdown — it is the section
       head's own count, restated one line below the section head. The words
       still earn their place (WHERE these people came from decides whether
       they can be removed here), so the number is what goes. */
    const solo = facts.length === 1 && !off;
    const line = solo ? facts[0].replace(/^<b>\d+<\/b>\s*/, "") : facts.join(" · ");
    /* A caption for the table under it, not a row of its own. It read as a
       stray line ("from mock_seed") floating between the strip and the
       search box; what it actually says is where this group's membership is
       decided, which is the sentence that explains why most rows have no
       Remove. Now that each row names its own state, this is the only place
       the SOURCE is named, so it stays — as a caption, phrased as one. */
    return `<p class="ax-memcap">Membership ${line}.</p>`;
  }

  /* The roster: the retired detail page's member table, columns and all —
     including the per-member source, which is what decides whether that
     member can be removed here at all. */
  function rosterHtml(members) {
    const rows = members.map((m) => {
      // Only admin-added membership is ours to undo. Google sync and the
      // system seeds own theirs — the API refuses the write, so the row says
      // who to talk to instead of offering a button that 4xxs.
      /* Where a member came from was printed THREE times on one row — as
         the strip's "from mock_seed" above the table, as a source column,
         and again as "managed by mock_seed" where the Remove button would
         be. All three said the same word. What each was for is different
         though: the strip says where the group's membership comes from
         (once, for the group), and the row needs to say why THIS person has
         no Remove — but only when that is the case, and only as the reason
         the button is missing. The middle column had no job at all. */
      const act = m.source === "admin"
        ? `<button type="button" class="btn btn-secondary btn-sm" data-rmmember="${esc(m.user_id)}">Remove</button>`
        : `<span class="ax-src ax-src--locked">${esc(SOURCE_LABEL[m.source] || "managed elsewhere")}</span>`;
      return `
      <tr>
        <td><a href="/admin/users/${encodeURIComponent(m.user_id)}">${esc(m.email || m.user_id)}</a></td>
        <td>${esc(m.name || "")}${m.active === false ? ` <span class="ax-src ax-src--locked">· deactivated</span>` : ""}</td>
        <td class="ax-mem__when">${m.added_at ? esc(fmtDate(m.added_at)) : ""}</td>
        <td class="ax-mem__act">${act}</td>
      </tr>`;
    }).join("");
    /* No header row. An address, a name, how they got here and a Remove
       button do not need labelling — the content says what each column is,
       and a header over four obvious columns is a row of chrome on a list
       that is usually two rows long. (This was a Jinja comment while the
       script lived in the template, i.e. stripped before the browser saw
       it; inside a template literal in a static file it has to sit out
       here, or it ships as text.) */
    return `
      <table class="data-table data-table--compact">
        <tbody>${rows}</tbody>
      </table>`;
  }

  /* ── Member search ───────────────────────────────────────────────────
     ONE input, two answers: type a name to check whether that person is in
     this group (and remove them if they are), or type someone who isn't and
     add them. Both are the same gesture, which is why they are the same
     control rather than a roster plus a separate add row.

     Results render in an ANCHORED POPOVER over the roster rather than as a
     block inserted above it. Inserting pushed the roster, the tier legend
     and the whole grant tree down by the height of the result list, so the
     row you were reaching for moved while you read it. The popover uses the
     same mechanics as the app's `.ds-dropdown-menu` and the same keyboard
     model: ↓/↑ to walk the results, Enter to act on one, Esc to dismiss.

     The query goes to the SERVER (`/api/users?search=`, which the People
     page already uses for the same purpose) rather than filtering a
     client-side copy of the org: a 500-user prefetch is both slow and wrong
     — it silently stops finding people at whatever limit it was given,
     which on an access page reads as "that person has no account". */
  let findTimer = null;
  let findSeq = 0;
  let findActive = -1;   // keyboard cursor into the popover's rows

  function popEl() { return el("ax-find-out"); }

  function closeFind() {
    const out = popEl();
    if (!out || out.hidden) return;
    out.hidden = true;
    findActive = -1;
    const input = el("ax-find");
    if (input) {
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
    }
  }

  function openFind() {
    const out = popEl();
    if (!out) return;
    out.hidden = false;
    const input = el("ax-find");
    if (input) input.setAttribute("aria-expanded", "true");
  }

  function findRows() {
    const out = popEl();
    return out ? Array.from(out.querySelectorAll(".ax-res__row")) : [];
  }

  function moveFind(delta) {
    const rows = findRows();
    if (!rows.length) return;
    rows.forEach((r) => { r.classList.remove("is-active"); r.setAttribute("aria-selected", "false"); });
    findActive = (findActive + delta + rows.length) % rows.length;
    const row = rows[findActive];
    row.classList.add("is-active");
    /* The cursor was carried in a CLASS only. The rows are `role="option"`
       inside a `role="listbox"`, and the input advertises
       `aria-autocomplete="list"` — so a screen reader was told a combobox
       existed, and then never told which option was current: every row stayed
       `aria-selected="false"` and the input never pointed at one. Sighted
       users saw the highlight move; nobody else did. */
    row.setAttribute("aria-selected", "true");
    if (!row.id) row.id = `ax-find-opt-${findActive}`;
    const input = el("ax-find");
    if (input) input.setAttribute("aria-activedescendant", row.id);
    row.scrollIntoView({ block: "nearest" });
  }

  function bindFind() {
    const input = el("ax-find");
    if (!input) return;
    input.addEventListener("input", () => {
      clearTimeout(findTimer);
      findTimer = setTimeout(() => runFind(input.value), 200);
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { closeFind(); return; }
      if (e.key === "ArrowDown") { e.preventDefault(); moveFind(1); return; }
      if (e.key === "ArrowUp") { e.preventDefault(); moveFind(-1); return; }
      if (e.key === "Enter") {
        /* With exactly ONE result, Enter means that one — requiring an
           ArrowDown first to disambiguate between a single option and itself
           is a step that answers no question. Several results still need the
           cursor moved, because then Enter genuinely is ambiguous. */
        const rows = findRows();
        const row = rows[findActive] || (rows.length === 1 ? rows[0] : null);
        const btn = row && row.querySelector("[data-addmember],[data-rmmember]");
        if (btn) { e.preventDefault(); btn.click(); return; }
        /* Nothing matched, so Enter means the other half of the intent:
           invite the person you were looking for. The address is already
           carried into the field, so a complete one goes straight through
           and a partial one just needs finishing — either way nobody types
           the same text twice.

           Focus is deliberately NOT moved while typing (that would fight the
           search on every keystroke); it moves here, on Enter, because Enter
           is the moment the admin says they are done searching. */
        const one = popEl() && popEl().querySelector(".ax-invite");
        if (!one) return;
        e.preventDefault();
        const go = one.querySelector("[data-invite]");
        if (go) { go.click(); return; }
        const field = one.querySelector(".ax-invite__mail");
        if (!field) return;
        const val = field.value.trim();
        if (/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(val)) {
          one.querySelector("[data-invite-typed]").click();
        } else {
          field.focus();
          /* `setSelectionRange` throws on `type="email"` — that input type
             does not support selection at all. Putting the caret at the end
             is the whole point (the query is already in the field), so the
             type is text and the shape is validated on submit instead. */
          try { field.setSelectionRange(field.value.length, field.value.length); } catch (err) { /* unsupported type */ }
        }
      }
    });
    // Re-opening on focus rather than only on keystroke: someone who
    // dismissed the popover and clicked back in wants what they had.
    input.addEventListener("focus", () => { if (findRows().length) openFind(); });
  }

  async function runFind(raw) {
    const out = popEl();
    if (!out) return;
    const q = (raw || "").trim();
    findActive = -1;
    if (!q) { out.innerHTML = ""; closeFind(); return; }
    const seq = ++findSeq;
    let people = [];
    try {
      const r = await fetch(`${USERS_LIST_API}?search=${encodeURIComponent(q)}&limit=${FIND_LIMIT}`,
                            { credentials: "include" });
      people = r.ok ? await r.json() : [];
      if (!Array.isArray(people)) people = people.users || [];
    } catch (e) { people = []; }
    if (seq !== findSeq) return; // a later keystroke already answered

    if (!people.length) {
      // (the seeded field is focused below, once it is in the DOM)
      /* No account matched — which used to end the job here and send the
         admin to People to start it again. "Add someone to this group" and
         "invite someone" are one intent, and the second half of it was a
         link out of the page.

         Inline rather than a modal: the query is already typed and scoped to
         this group, a modal would discard both and ask for them again, and
         the whole task is one field whose result lands in the roster
         directly below. A modal earns its interruption when a task needs its
         own space; this does not. */
      const typed = q.trim();
      const looksLikeEmail = /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(typed);
      /* Never make them type it twice. The search text IS the start of the
         address, so it is carried into the field — completed to a full
         address when the instance has a sign-in domain (an invited account
         could not authenticate with any other), and otherwise left as the
         local part with the caret after it. */
      const domain = (INVITE_DOMAINS && INVITE_DOMAINS.length === 1) ? INVITE_DOMAINS[0] : "";
      const seed = looksLikeEmail ? typed
        : (domain && typed && !typed.includes("@") ? `${typed}@${domain}` : typed);
      out.innerHTML = looksLikeEmail
        ? `<div class="ax-invite">
             <span class="ax-invite__msg">No account for <b>${esc(q)}</b> yet.</span>
             <button type="button" class="btn btn-primary ax-invite__go" data-invite="${esc(q.trim())}">
               Invite and add to this group
             </button>
           </div>`
        : `<div class="ax-invite" data-invite-seeded>
             <span class="ax-invite__msg">No account matches “${esc(q)}”. Invite them:</span>
             <span class="ax-invite__form">
               <input type="text" inputmode="email" autocomplete="off" class="ax-invite__mail" placeholder="name@company.com"
                      aria-label="Email address to invite" value="${esc(seed)}">
               <button type="button" class="btn btn-primary" data-invite-typed>Invite</button>
             </span>
             <span class="ax-invite__note">${domain
               ? `Completed with <code>@${esc(domain)}</code>, this instance's sign-in domain. `
               : ""}They are added to this group, and to Everyone, as soon as the account exists.</span>
           </div>`;
      openFind();
      return;
    }
    out.innerHTML = people.map((u) => {
      const inGroup = memberIds.has(u.id);
      const src = memberSource.get(u.id);
      // Same rule the roster applies: only admin-added membership can be
      // undone here. A synced member still answers "is Maria in Finance?" —
      // it just says who owns the answer instead of offering to change it.
      const removable = inGroup && src === "admin";
      const label = esc(u.name || u.email);
      return `
      <div class="ax-res__row" role="option" aria-selected="false">
        <span class="ax-res__ava" style="background:${esc(AgnesIdentity.avatarColor(u.email || u.id))}">${esc(AgnesIdentity.initials(u.name || u.email))}</span>
        <span class="ax-res__who"><b>${label}</b><span>${esc(u.email)}</span></span>
        ${inGroup ? `<span class="ax-res__state">In this group</span>` : ""}
        <span class="ax-res__act">
          ${!inGroup
            ? `<button type="button" class="btn btn-primary btn-sm" data-addmember="${esc(u.id)}" data-email="${esc(u.email)}">Add</button>`
            : removable
              ? `<button type="button" class="btn btn-secondary btn-sm" data-rmmember="${esc(u.id)}">Remove</button>`
              : `<span class="ax-src ax-src--locked">managed by ${esc(src || "the system")}</span>`}
        </span>
      </div>`;
    }).join("");
    openFind();
  }

  /* Create the account, then put it in this group. Two calls because they
     are two facts — an account exists, and it is in this audience — and the
     second failing must not silently undo the first: the toast says which
     half landed. */
  //: Enter in the invite field is the same act as pressing Invite beside it.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const field = e.target.closest && e.target.closest(".ax-invite__mail");
    if (!field) return;
    e.preventDefault();
    const btn = field.closest(".ax-invite").querySelector("[data-invite-typed]");
    if (btn) btn.click();
  });

  async function inviteAndAdd(email) {
    const addr = String(email || "").trim();
    if (!addr) return;
    const r = await fetch("/api/users", {
      method: "POST", credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: addr, name: addr.split("@")[0] }),
    });
    if (!r.ok) {
      const b = await r.json().catch(() => ({}));
      // 409 is not a failure of intent: the account exists, so the half that
      // matters here — putting them in the group — can still go ahead.
      if (r.status === 409) { await addMember(addr); return; }
      toast(`Could not invite ${addr}: ${typeof b.detail === "string" ? b.detail : r.status}`, false);
      return;
    }
    await addMember(addr);
    toast(`Invited ${addr} — account created and added to this group`, true);
  }

  async function addMember(email) {
    if (!email) return;
    // Admin is a god-mode short-circuit on every authorization check, so the
    // usual toast — "they now get everything granted to this group" — is
    // materially wrong for it: the grants are not the point, the bypass is.
    // /admin/users/{id} already confirms this correctly, so the two surfaces
    // disagreed about the most consequential membership on the instance.
    const grp = (overview.groups || []).find((g) => g.id === selectedGroup);
    const isAdminGroup = grp && (grp.is_admin === true || grp.name === "Admin");
    if (isAdminGroup) {
      const ok = await window.confirmModal({
        title: `Add ${email} to Admin?`,
        message: `Admin is not an ordinary group: it grants full access to all `
          + `data and every admin action on this instance, regardless of what is `
          + `granted to it. Reversible — you can remove them again.`,
        confirmText: "Add to Admin",
      });
      if (!ok) return;
    }
    const r = await fetch(`/api/admin/groups/${encodeURIComponent(selectedGroup)}/members`, {
      method: "POST", credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email }),
    });
    if (!r.ok) {
      const b = await r.json().catch(() => ({}));
      toast(r.status === 404
        ? `No account for ${email} — invite them on People first.`
        : `Could not add: ${typeof b.detail === "string" ? b.detail : r.status}`, false);
      return;
    }
    membersCache.delete(selectedGroup);
    toast(isAdminGroup
      ? `${email} added to Admin — they now reach everything on this instance.`
      : `${email} added — they now get everything granted to this group.`, true);
    await afterMembershipChange();
  }

  async function removeMember(userId) {
    /* Captured BEFORE the delete, because afterwards there is nothing left to
       look it up from. "Removed from the group" on a roster of a dozen
       similar addresses left you with no way to know whom you had just cut
       off — the Add toast has always named the person. */
    const who = ((membersCache.get(selectedGroup) || [])
      .find((m) => m.id === userId) || {});
    const whoLabel = who.email || who.name || "They";
    const r = await fetch(
      `/api/admin/groups/${encodeURIComponent(selectedGroup)}/members/${encodeURIComponent(userId)}`,
      { method: "DELETE", credentials: "include" },
    );
    if (!r.ok && r.status !== 404) {
      const b = await r.json().catch(() => ({}));
      toast(`Could not remove: ${typeof b.detail === "string" ? b.detail : r.status}`, false);
      return;
    }
    membersCache.delete(selectedGroup);
    toast(`${whoLabel} removed — they lose everything granted through this group.`, true);
    await afterMembershipChange();
  }

  // A membership edit moves three things: the audience sentence, the left
  // column's counts, and the state of whatever the search is currently
  // showing (the row you just acted on must flip, in place, without making
  // you retype the query).
  // A membership edit moves four things: the roster, the section head's
  // count, the sticky identity strip, and the left column's row — and the
  // state of whatever the popover is currently showing, since the row you
  // just acted on must flip in place without making you retype the query.
  async function afterMembershipChange() {
    const q = (el("ax-find") || {}).value || "";
    await renderMembers();
    bindFind();
    const input = el("ax-find");
    if (input && q) { input.value = q; await runFind(q); }
    await refreshGroupCounts();
  }

  // The left column and the sticky header both show counts, so a membership
  // edit has to move them or the halves of this page disagree.
  async function refreshGroupCounts() {
    try {
      const r = await fetch(OVERVIEW_API, { credentials: "include" });
      if (!r.ok) return;
      const fresh = await r.json();
      overview.groups = fresh.groups || overview.groups;
      renderIdentity();
      renderGroups();
    } catch (e) { /* counts are cosmetic — never block the edit */ }
  }

  /* Typing repaints the whole view, not just the list. This used to call
     `renderGroups()` alone, which left the two halves of the page disagreeing
     with each other: the list narrowed to the one group holding "board" and
     auto-opened it, but the work pane beside it was never re-rendered, so an
     OPEN group sat above "Open a group to see what it can use." — and in the
     bundle lens it rendered the group list into the bundle tab outright.
     `syncQuery()` (which decides whether the query also narrows an open
     group's rows) never ran either. */
  el("ax-group-find").addEventListener("input", (e) => {
    groupFilter = e.target.value;
    syncUrl();          // replace: one history entry per keystroke is noise
    /* On By person the search narrows the ROSTER — the same act it performs
       on the other two tabs, which is the point: one control, one behaviour,
       whichever tab you are on. (An earlier pass had it select the first
       match instead, which only helped someone who could already spell the
       name they wanted.) Typing while a chain is open returns to the list, so
       the search always narrows something visible. */
    if (viewMode === "person") {
      const sel = el("ax-sim-user");
      if (sel && sel.value) { sel.value = ""; sel.dispatchEvent(new Event("change", { bubbles: true })); }
      else { loadUsers().then(renderPeopleList); }
      return;
    }
    repaintQuery();
    /* The people-match asks the server for THIS query (audit S2) — one
       request per distinct query, memoised in `fetchMemberGroups`, so
       retyping the same thing costs nothing and the roster never loads for
       this. The repaint after it lands is what turns a "0 of 5" into the real
       answer without the reader retyping.

       No `!users.length` guard here. That guard belonged to the roster
       load it replaced ("fetch once per sitting"); kept, it would have made
       the member search silently never fire on any visit where the person
       lens had already filled `users` — a search that works until you open
       the other tab. */
    if (groupFilter.trim().length >= 2) {
      fetchMemberGroups(groupFilter.trim().toLowerCase()).then((done) => { if (done && groupFilter.trim()) repaintQuery(); });
    }
  });

  /* repaint() without the parts a keystroke cannot change — the tab counts
     and the kind menu — and without re-fetching the roster unless the query
     actually moved the selection, which it does only on the first keystroke
     that lands on a different group. Same ORDER as repaint(), because
     renderGroups() is what adopts the first match as the selection and
     renderResources() has to run after it. */
  async function repaintQuery() {
    setFindPlaceholder(viewMode);
    setLede(viewMode);
    const before = selectedGroup;
    syncQuery();
    if (viewMode === "person") return;
    if (viewMode === "resource") { renderBundles(); bindFind(); return; }
    renderGroups();
    if (selectedGroup !== before) await renderMembers();
    bindFind();
    renderResources();
  }

  /* ── Create a group without leaving ───────────────────────────────────
     The drawer asks for a name and closes. What matters is what happens
     AFTER: the group joins the left column and becomes the SELECTION,
     because someone who just created Finance wanted to grant Finance
     something — that was the whole reason they needed it. The drawer used
     to collect the people and the grants itself, in its own copies of the
     two panes now sitting to the right of this list. */
  async function openGroupDrawer(group) {
    window.AgnesGroupDrawer.open({
      group: group || null,
      onSaved: async (saved) => {
        // Re-read rather than splice the row in: a hand-patched row and the
        // pane beside it drift the moment anything else changed.
        try {
          const r = await fetch(OVERVIEW_API, { credentials: "include" });
          if (r.ok) overview = await r.json();
        } catch (e) { /* keep what we have — the drawer already saved */ }
        if (saved && saved.id) {
          selectedGroup = saved.id;
          rememberSelection(selectedGroup);
        }
        await repaint();
        toast(group
          ? `${saved.name} updated.`
          // Not "selected on the left": there is no left pane, and has not
          // been since the list became one column.
          : `${saved.name} is ready — it is open below, with nothing granted yet.`, true);
      },
    });
  }

  /* Deleting the group itself — the last thing the retired detail page could
     do that this pane could not. The confirmation names the CONSEQUENCE
     (whose access disappears, and how much of it) rather than asking "are you
     sure", because those two numbers are the decision. */
  async function deleteGroup(groupId) {
    const group = (overview.groups || []).find((g) => g.id === groupId);
    if (!isEditable(group)) return;
    const people = group.member_count ?? 0;
    const grants = grantCountOf(group.id);
    const ok = await window.confirmModal({
      title: `Delete “${titleOf(group)}”?`,
      message: `${people} ${people === 1 ? "person" : "people"} lose the ${grants} `
        + `${grants === 1 ? "thing" : "things"} granted through this group. `
        + `Their accounts and anything granted to them by another group are untouched. This cannot be undone.`,
      confirmText: "Delete group",
    });
    if (!ok) return;
    const r = await fetch(`/api/admin/groups/${encodeURIComponent(group.id)}`, {
      method: "DELETE", credentials: "include",
    });
    if (!r.ok && r.status !== 404) {
      const b = await r.json().catch(() => ({}));
      toast(`Could not delete: ${typeof b.detail === "string" ? b.detail : r.status}`, false);
      return;
    }
    membersCache.delete(group.id);
    await refetchOverview();   // falls through on failure — the list re-renders from what we have
    // The selection only has to move if it was the deleted group: deleting
    // `finance` from its own row while `sales` is open must leave `sales`
    // open, or the menu would quietly navigate the pane as a side effect.
    if (selectedGroup === group.id) {
      /* Back to no selection, not to the first row. `sortedGroups()` puts
         `Everyone` and `Admin` at the top, so deleting the open group used to
         open the god-mode group as a side effect of an unrelated delete —
         the one group nobody should land inside by accident. */
      selectedGroup = null;
      rememberSelection(null);
    }
    await repaint();
    toast(`${titleOf(group)} deleted.`, true);
  }

  // One repaint for every path that changes the group or the selection, so
  // the four regions (list, identity, audience, grants) can never disagree
  // about which group is open.
  /* The person view is its own pane (the reason chain and the Library
     preview); the other two are the list. One function decides which is on
     so the switch cannot leave both showing. */
  /* One box, three lenses, and the placeholder was written for all of them
     at once — "Search groups, bundles or people…" — which promised people on
     a tab that could not match them and bundles on a tab that had none. It
     now says what THIS lens searches. */
  const FIND_PLACEHOLDER = {
    group: "Search groups, what they hold, or people in them…",
    resource: "Search resources by name, id or block…",
    person: "Search people…",
  };
  const LEDE = {
    // Not "every group": the list leads with an audience that is not one.
    group: "Everyone, then every group you can write a grant against — and what each one can reach.",
    resource: "One thing at a time — a data package, a memory domain, a plugin, an agent — "
            + "and which groups can reach it. The mirror of By group.",
  };
  function setLede(mode) {
    const l = el("ax-lede");
    if (!l) return;
    l.textContent = LEDE[mode] || "";
    l.hidden = !LEDE[mode];
  }

  function setFindPlaceholder(mode) {
    const f = el("ax-group-find");
    if (!f) return;
    const t = FIND_PLACEHOLDER[mode] || FIND_PLACEHOLDER.group;
    f.placeholder = t;
    f.setAttribute("aria-label", t.replace(/…$/, ""));
  }

  function showPane(mode) {
    setFindPlaceholder(mode);
    const person = mode === "person";
    document.querySelectorAll("[data-axpane]").forEach((p) => {
      p.classList.toggle("is-on", p.dataset.axpane === (person ? "sim" : "edit"));
    });
    /* The search STAYS on every tab. It used to hide here, on the reasoning
       that narrowing a list of groups means nothing while reading one
       person's access — sound while the toolbar sat below the tabs and read
       as that pane's own. Above the tabs it is the page's toolbar, and a
       page-level control that vanishes on one of three tabs reads as a bug,
       not as a judgement.

       So it is given a job here instead of being taken away: on this tab it
       picks the person (see the input handler), which is the one thing a
       search can mean when the pane shows exactly one of them.

       + New group is a different case and still hides — it makes an audience,
       which is not a thing this tab does, and unlike the search there is no
       honest job to give it. */
    const nu = el("ax-new-group");
    if (nu) nu.hidden = person;
  }

  /* A tab that carries its own count says what switching would show before
     you switch — the Library's tabs do the same. */
  /* Built from what the ACTIVE view actually holds, with counts, so the
     control never offers a kind that would empty the list. Hidden in the
     person view, which is not a list of things. */
  /* ── The filter menu ─────────────────────────────────────────────────
     The Library's own shape: a category per facet, a submenu of checkboxes
     inside each, a count beside every option, and a chip row underneath
     saying what is on. Nothing here is bespoke styling — `fbar-menu--cats`,
     `fbar-cat`, `fbar-menu__opt` and `fbar-chip` are the shared classes in
     `filter_toolbar.css`, so this page's filters look and behave like the
     Library's because they ARE the Library's.

     Two rules the Library established and this follows:

       A count beside an option is the number of rows PICKING IT WOULD
       LEAVE, not some other statistic wearing the same slot. Each lens
       counts the thing it lists — the group lens counts groups, the
       resource lens counts resources — because "Marketplace plugins 5"
       above a list that then says "2 of 24 groups" is two different
       questions answered in the same breath.

       No dead filters. A category with nothing to offer is not rendered at
       all, so an empty submenu can never be opened. On this page that
       matters most for Tier, which only exists on five of the sixteen
       kinds: an instance with no tiered grants has no Tier category. */
  function paintKindFilter() {
    const btn = el("ax-filter-btn");
    const menu = el("ax-filter-menu");
    const chips = el("ax-chips");
    const nBadge = el("ax-filter-n");
    if (!btn || !menu || !chips) return;

    /* Switching to a lens that does not offer a facet drops that facet's
       picks. Carrying them across would narrow the new list with no control
       on screen able to undo it — the same invisible-filter state the chip
       row exists to prevent. */
    if (viewMode !== "resource" && facetOn("reach")) facets.get("reach").clear();

    const wrap = btn.closest(".ax-filter");
    if (viewMode === "person") {
      if (wrap) wrap.hidden = true;
      chips.hidden = true;
      return;
    }
    if (wrap) wrap.hidden = false;

    const typeLabel = new Map((overview.resources || []).map((t) => [t.type_key, t.type_display]));
    const rank = new Map((overview.resources || []).map((t, i) => [t.type_key, i]));

    /* Every (row, its grants) pair the ACTIVE lens lists, once. Both the
       option counts and the chip counts are computed from this, so the
       menu and the list can never disagree about what a pick would do. */
    const subjects = [];
    if (viewMode === "resource") {
      const heldBy = new Map();
      for (const g of (overview.grants || [])) {
        const k = `${g.resource_type}\u0000${g.resource_id}`;
        const at = heldBy.get(k);
        if (at) at.push(g); else heldBy.set(k, [g]);
      }
      for (const t of (overview.resources || [])) {
        if (!BUNDLE_LEAD.has(t.type_key)) continue;
        for (const b of (t.blocks || [])) {
          for (const i of (b.items || [])) {
            subjects.push({ typeKey: t.type_key, held: heldBy.get(`${t.type_key}\u0000${i.resource_id}`) || [] });
          }
        }
      }
    } else {
      // A group is one subject holding all of its grants: picking a value
      // keeps the group if ANY of them matches, which is what it counts.
      for (const g of (overview.groups || [])) {
        subjects.push({ typeKey: null, held: grantsFor(g.id) });
      }
    }

    /* What picking one more value would leave: this facet's set with `v`
       added, every other facet as it stands. So a count narrows as other
       facets are picked, which is what makes it a preview rather than a
       standing total. */
    const wouldLeave = (facetKey, v) => {
      const saved = facets.get(facetKey);
      facets.set(facetKey, new Set([...saved, v]));
      let n = 0;
      for (const sub of subjects) {
        const ok = sub.typeKey !== null
          ? rowPassesFacets(sub.typeKey, sub.held)
          : (sub.held || []).some((x) => rowPassesFacets(x.resource_type, [x]));
        if (ok) n++;
      }
      facets.set(facetKey, saved);
      return n;
    };

    //: Values a facet could offer, before any are dropped for being empty.
    const kindsPresent = [...new Set(
      viewMode === "resource"
        ? subjects.map((x) => x.typeKey)
        : (overview.grants || []).map((g) => g.resource_type))]
      .filter((k) => viewMode === "resource" ? BUNDLE_LEAD.has(k) : typeLabel.has(k))
      .sort((a, b) => (rank.get(a) ?? 99) - (rank.get(b) ?? 99));

    const CATS = [
      { key: "kind", label: "Kind",
        values: kindsPresent.map((k) => [k, typeLabel.get(k) || k]) },
      /* Resource lens only, and that is a judgement rather than a
         limitation. "Which groups hold something granted to everyone" is
         answered "all of them" by definition — an everyone-scoped grant
         reaches every group's members — so on the group lens this facet
         counts the whole list and filters nothing. A control whose every
         value is a no-op is the dead filter the Library's rule exists to
         prevent, and a count that equals the total is not a preview. */
      ...(viewMode === "resource" ? [{ key: "reach", label: "Reach", values: [
        [REACH.EVERYONE, "Everyone"],
        [REACH.GROUP, "Specific groups"],
        [REACH.NOBODY, "Nobody"],
      ] }] : []),
      { key: "tier", label: "Tier", values: [
        ["required", WORDS.tier_automatic || "Automatic"],
        ["available", WORDS.tier_optional || "Optional"],
      ] },
      { key: "origin", label: "Where it came from", values: [
        [ORIGIN.ADMIN, "Granted here"],
        [ORIGIN.OWNER, "Shared by an owner"],
        [ORIGIN.MANAGED, "Managed elsewhere"],
      ] },
    ];

    /* A value counting zero is dropped, and a category left with fewer than
       two values goes with it — one option is not a choice, it is the list
       you are already looking at. A value that is currently TICKED always
       survives, or turning a filter on would delete the control that turns
       it off. */
    const live = CATS.map((c) => ({
      ...c,
      values: c.values
        .map(([v, lbl]) => [v, lbl, wouldLeave(c.key, v)])
        .filter(([v, , n]) => n > 0 || facetHas(c.key, v)),
    })).filter((c) => c.values.length > 1 || c.values.some(([v]) => facetHas(c.key, v)));

    menu.innerHTML = live.map((c) => `
      <div class="fbar-cat" data-cat="${esc(c.key)}">
        <button type="button" class="fbar-cat__btn" aria-haspopup="true" aria-expanded="false">
          <span class="fbar-cat__label">${esc(c.label)}</span>
          <span class="fbar-cat__end">
            <span class="fbar-cat__n"${facetOn(c.key) ? "" : " hidden"}>${facets.get(c.key).size}</span>
            <svg class="fbar-cat__caret" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m9 6 6 6-6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </span>
        </button>
        <div class="fbar-cat__pop" hidden>
          ${c.values.map(([v, lbl, n]) => `
          <label class="fbar-menu__opt">
            <input type="checkbox" data-facet="${esc(c.key)}" value="${esc(v)}"${facetHas(c.key, v) ? " checked" : ""}>
            <span class="fbar-menu__opt-text">${esc(lbl)}</span>
            <span class="fbar-menu__opt-n">${n}</span>
          </label>`).join("")}
        </div>
      </div>`).join("") + `
      <div class="fbar-menu__foot">
        <button type="button" data-fbar-clear>Clear</button>
        <button type="button" data-fbar-done>Done</button>
      </div>`;

    const total = FACET_KEYS.reduce((n, k) => n + facets.get(k).size, 0);
    nBadge.textContent = String(total);
    nBadge.hidden = !total;
    btn.classList.toggle("is-on", !!total);

    const row = el("ax-chiprow");
    if (row) row.hidden = false;

    /* One chip per PICKED VALUE, labelled with its category. A chip per
       category ("Kind: 3") would name the count and hide the answer, and
       the thing a reader wants to undo is one value, not a category. */
    const labelOf = (catKey, v) => {
      const c = CATS.find((x) => x.key === catKey);
      const hit = c && c.values.find(([val]) => val === v);
      return hit ? hit[1] : v;
    };
    const picked = [];
    for (const k of FACET_KEYS) {
      for (const v of facets.get(k)) picked.push([k, v]);
    }
    chips.innerHTML = picked.length ? picked.map(([k, v]) => `
      <span class="fbar-chip">
        <span class="fbar-chip__edit">
          <span class="fbar-chip__label">${esc((CATS.find((c) => c.key === k) || {}).label || k)}:</span>
          <span class="fbar-chip__val">${esc(labelOf(k, v))}</span>
        </span>
        <button type="button" class="fbar-chip__x" data-chip-drop="${esc(k)}" data-chip-val="${esc(v)}"
                aria-label="Remove ${esc(labelOf(k, v))} filter">×</button>
      </span>`).join("") + `
      <button type="button" class="fbar-chips__clear" data-chip-clear>Clear all</button>` : "";
    chips.hidden = !picked.length;
  }

  /* "N of M", reported by whoever just rendered the list rather than
     recomputed from the filters here. The count had its own copy of the
     narrowing rules and that copy knew only about the kind chip — so a
     text query that cut the list to one group still read "5 groups". A
     count derived from the render cannot disagree with the render. */
  /* The kinds an admin hands out as a UNIT — the only ones the bundle view
     leads with, and the only ones its kind filter may offer (a filter that
     empties the list it filters is not describing that list). One copy: the
     bundle list, its kind filter and its count all read this.

     `chat` is in the set even though it is a SURFACE rather than a bundle of
     content. It was excluded, and it is granted — on the seed instance it
     carries two live grants — so the lens whose entire job is "who can use
     this thing?" was hiding two real answers and showing no SURFACES family
     at all, while the group lens counted them in its `surfaces` chip. A kind
     that can be granted has to be visible in the lens that reads grants.

     `table` stays out, deliberately: tables are granted through a data
     package, and a real instance has hundreds of them, which would bury
     every row that carries a decision. `bundleFootnote()` says so on the
     page rather than leaving the omission to be discovered. */
  /* `corpus_file` is here so the resource lens can answer "what files exist
     on this instance, and who can reach them" — the question an admin asks
     after seeing a filename they did not recognise. It is the one kind that
     can run to thousands of rows, which is exactly what the kind filter and
     the granted-to-nobody drawer are for: private uploads collect behind one
     counted line at the foot instead of burying the rows that carry a
     decision. (`table` stays out for the same volume reason with none of the
     privacy question attached — a table name is in the catalog already.) */
  const BUNDLE_LEAD = new Set(["data_package", "memory_domain", "semantic_model", "recipe",
                               "marketplace_plugin", "store_entity", "data_app", "agent",
                               "collection", "corpus_file", "chat"]);

  /* How many DISTINCT people a set of groups reaches.

     This was a sum of `member_count`, which double-counts anyone in two of
     the groups — and since `Everyone` holds every account, any bundle
     granted to Everyone plus one other group reported more people than the
     instance has. The seed instance showed "3 groups · 5 people" against 3
     accounts.

     `Everyone` short-circuits to the account total rather than being unioned:
     it is every account by construction, so the answer cannot be larger, and
     the overview deliberately does not ship its roster (see the `member_ids`
     note in app/api/access.py). Everything else unions the real ids. */
  /* The authoritative reach, from the server, where the memberships are.
     Memoised by the exact set asked about, so toggling a group off and back
     on does not re-ask. Resolves to null on any failure so the caller keeps
     the local estimate rather than painting nothing. */
  /* Which groups hold a person matching the group-list search. One request
     per distinct query, memoised; the answer is stored with the query it
     answers so a repaint for a different query cannot read a stale hit. Under
     two characters the server refuses (422) and the page does not ask — the
     same floor `memberHit` always applied. */
  let memberMatches = { q: "", byGroup: new Map(), matched_people: 0 };
  const _memberCache = new Map();
  function fetchMemberGroups(q) {
    if (!q || q.length < 2) { memberMatches = { q, byGroup: new Map(), matched_people: 0 }; return Promise.resolve(false); }
    if (!_memberCache.has(q)) {
      _memberCache.set(q, fetch(`${MEMBER_SEARCH_API}?q=${encodeURIComponent(q)}`, { credentials: "include" })
        .then((r) => (r.ok ? r.json() : null))
        .catch(() => null));
    }
    return _memberCache.get(q).then((b) => {
      if (!b) return false;
      memberMatches = { q, byGroup: new Map((b.matches || []).map((m) => [m.group_id, m.people || []])), matched_people: b.matched_people || 0 };
      return true;
    });
  }

  const _reachCache = new Map();
  function fetchReach(groupIds) {
    const key = groupIds.slice().sort().join(",");
    if (!key) return Promise.resolve(0);
    if (_reachCache.has(key)) return _reachCache.get(key);
    const p = fetch(`${REACH_API}?ids=${encodeURIComponent(key)}`, { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((b) => (b && typeof b.count === "number" ? b.count : null))
      .catch(() => null);
    _reachCache.set(key, p);
    return p;
  }

  function reachOf(groupIds) {
    /* The sentinel is not in `overview.groups` — by design, since it is not
       a group — so it would be silently dropped here and the picker's
       "N people" line would count only the real groups chosen alongside it.
       That reads as everyone being FEWER people than a two-person team. */
    if ((groupIds || []).includes(EVERYONE_AUDIENCE.id)) {
      return overview.account_total ?? 0;
    }
    const groups = (groupIds || [])
      .map((id) => (overview.groups || []).find((g) => g.id === id))
      .filter(Boolean);
    if (groups.some((g) => g.is_everyone)) {
      return overview.account_total ?? groups.reduce((n, g) => Math.max(n, g.member_count ?? 0), 0);
    }
    const seen = new Set();
    let unknown = 0;
    for (const g of groups) {
      const ids = g.member_ids;
      // Rosters no longer ship (audit S2), so this branch is now the rule
      // rather than the exception: the local figure is a sum of counts, an
      // ESTIMATE that overshoots when a person is in two groups. Where the
      // number decides something — the picker footer — `fetchReach` paints
      // the server's answer over it. Elsewhere it is a summary and says so
      // by its clamp to the account total.
      if (!Array.isArray(ids)) { unknown += g.member_count ?? 0; continue; }
      for (const id of ids) seen.add(id);
    }
    const n = seen.size + unknown;
    return overview.account_total != null ? Math.min(n, overview.account_total) : n;
  }

  //: Every bundle the view COULD list — the denominator, counted the same
  //: way the list itself is built (from resources, not from grants: a bundle
  //: granted to nobody is still a row, and the old denominator missed it).
  function bundleTotal() {
    let n = 0;
    for (const t of (overview.resources || [])) {
      if (!BUNDLE_LEAD.has(t.type_key)) continue;
      for (const b of (t.blocks || [])) n += (b.items || []).length;
    }
    return n;
  }

  function paintCount(shown, total) {
    const countEl = el("ax-count");
    if (!countEl) return;
    const noun = viewMode === "resource" ? "resource" : "group";
    const plural = (n) => `${n} ${n === 1 ? noun : noun + "s"}`;
    countEl.textContent = shown < total
      ? `${shown} of ${plural(total)}`
      : plural(total);
  }

  /* Selected state AND the roving tabindex, in one place — they have to move
     together or Tab lands on a tab that is not the current one. */
  function paintTabState() {
    const tabs = [...el("ax-by").querySelectorAll("[data-by]")];
    tabs.forEach((b) => {
      const on = b.dataset.by === viewMode;
      b.classList.toggle("is-active", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
      b.tabIndex = on ? 0 : -1;
    });
    // The panel says which tab named it, so a screen reader entering the
    // panel is told which reading it is looking at.
    const pane = el(viewMode === "person" ? "ax-pane-sim" : "ax-pane-edit");
    if (pane) pane.setAttribute("aria-labelledby", `ax-tab-${viewMode}`);
  }

  /* Left/Right move between tabs, Home/End jump to the ends — the behaviour
     `role="tablist"` promises and this one did not implement, so a keyboard
     user could reach the tabs and then not move through them. */
  el("ax-by").addEventListener("keydown", (e) => {
    const keys = ["ArrowLeft", "ArrowRight", "Home", "End"];
    if (!keys.includes(e.key)) return;
    const tabs = [...el("ax-by").querySelectorAll("[data-by]")];
    const here = tabs.indexOf(document.activeElement);
    if (here < 0) return;
    e.preventDefault();
    const next = e.key === "Home" ? 0
      : e.key === "End" ? tabs.length - 1
      : (here + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    tabs[next].focus();
    tabs[next].click();     // follow-focus: the pattern's usual reading
  });

  function paintTabCounts() {
    const NOUN = { group: "groups", resource: "resources", person: "people" };
    const set = (k, n) => {
      const el2 = document.querySelector(`[data-by-n="${k}"]`);
      if (!el2) return;
      el2.textContent = n == null ? "" : String(n);
      // "By group 5" tells a screen reader a number and not what it counts.
      if (n != null) el2.setAttribute("aria-label", `${n} ${NOUN[k] || ""}`.trim());
      else el2.removeAttribute("aria-label");
    };
    set("group", (overview.groups || []).length);
    /* What the tab LISTS, not what happens to be granted. This counted
       distinct grant rows of every type — so the badge said 9 over a list of
       23, and neither number explained the other. Same source as the list and
       its "N of M" count now. */
    set("resource", bundleTotal());
    /* This computed a number and then threw it away — `? null : null` —
       so the badge rendered as an empty box beside "By group 5" and "By
       bundle 24", which reads as a count still loading. It is the number of
       accounts the lens can show, which the overview now carries. */
    set("person", overview.account_total ?? null);
  }

  /* A tab left open for an hour holds an hour-old model. When it comes back
     into view, refetch if the copy is older than the threshold and repaint —
     so a colleague's change shows up when you look, not when you happen to
     reload. Cheap to skip on a quick alt-tab; the threshold is what keeps a
     busy admin's switching from becoming a request per glance. */
  const STALE_AFTER_MS = 30000;
  document.addEventListener("visibilitychange", async () => {
    if (document.visibilityState !== "visible") return;
    if (Date.now() - _overviewFetchedAt < STALE_AFTER_MS) return;
    if (await refetchOverview()) await repaint();
  });

  async function repaint() {
    // `showPane()` only runs on a lens SWITCH, so the default lens never got
    // its own placeholder on first load.
    setFindPlaceholder(viewMode);
    setLede(viewMode);
    paintTabCounts();
    paintKindFilter();
    if (viewMode === "person") {
      showPane("person");
      // Arriving by clicking the tab, not by URL — same roster, same reason.
      if (!(el("ax-sim-user") || {}).value) loadUsers().then(renderPeopleList);
      return;
    }
    syncQuery();
    if (viewMode === "resource") { renderBundles(); bindFind(); return; }
    renderGroups();
    await renderMembers();
    bindFind();
    renderResources();
  }

  //: Repaint whatever view is on. Grant writes happen in both.
  /* ── Keeping the keyboard's place across a repaint ────────────────────
     Every mutation on this page re-renders its section wholesale, so the
     element you were standing on stops existing and the browser drops focus
     to <body>. For a pointer user that is invisible; for a keyboard user it
     means one tier toggle throws you to the top of the document and you tab
     back through the whole page to reach the next row.

     Elements cannot be held across the re-render, so we hold the row's
     IDENTITY (`data-type` + `data-rid`, plus `data-gid` in the bundle lens,
     which is what the click handlers already resolve grants by) and the
     control's own attribute, then find the equivalent afterwards. When the
     row itself is gone — a Revoke — focus falls to the section's Add
     control, which is the next thing you would reach for. */
  function focusMemo() {
    const a = document.activeElement;
    if (!a || a === document.body) return null;
    const row = a.closest("[data-rid]");
    if (!row) return null;
    const key = a.dataset.tier ? `[data-tier="${a.dataset.tier}"]`
      : a.hasAttribute("data-revoke") ? "[data-revoke]"
      : null;
    if (!key) return null;
    return { rid: row.dataset.rid || "", type: row.dataset.type || "", gid: row.dataset.gid || "", key };
  }

  function focusRestore(memo) {
    if (!memo) return;
    const esq = (v) => (window.CSS && CSS.escape ? CSS.escape(v) : v);
    const sel = `.ax-r[data-rid="${esq(memo.rid)}"][data-type="${esq(memo.type)}"]`
      + (memo.gid ? `[data-gid="${esq(memo.gid)}"]` : "");
    const row = document.querySelector(sel);
    const target = (row && row.querySelector(memo.key))
      || document.querySelector("#ax-resources [data-add-grant]")
      || document.querySelector("#ax-resources [data-share-bundle]");
    if (target && typeof target.focus === "function") target.focus();
  }

  function repaintView() {
    if (viewMode === "resource") { renderBundles(); return; }
    renderResources();
    renderGroups();
  }

  /* ── By bundle ────────────────────────────────────────────────────────
     The same grants, read the other way: one section per grantable thing,
     one row per group that holds it. Two things only this direction can
     show — the same bundle held by two groups (the comparison the two-pane
     layout made impossible), and the bundles granted to NOBODY, which in
     the group view are invisible by construction because you are always
     looking at one group's holdings. */
  /* Which bundles are open. The group view survives a repaint because the
     selected group is state — it is re-emitted with `open` — and the bundle
     view had no equivalent, so every repaint rebuilt the list closed. Any
     write from inside an open bundle (a tier change, a revoke) therefore
     shut the thing you were working in, right after acting on it. */
  const openBundles = new Set();
  /* …and whether the granted-to-nobody drawer is open. It is a `<details>`
     inside a list that re-renders on every share, so it slammed shut after
     each one — and working through fifteen ungranted bundles meant reopening
     it fifteen times and finding your place again. */
  let nobodyOpen = false;
  //: Category runs folded away in the bundle list, by kind.
  /* ── What a collapsed section costs ──────────────────────────────
     Nothing now. It used to cost everything.

     This function built the HTML for EVERY row it could show — every
     ungranted one into the "Granted to nobody" drawer, every kind run
     regardless of size — and handed the lot to `innerHTML`. A closed
     `<details>` still parses its children into the DOM, so "collapsed by
     default" bought the reader nothing: the work was already done.

     That held while every grantable kind was admin-curated. `corpus_file`
     is not: it is one item per crawled file, so the list became the size
     of the crawl. Measured against a seeded 30,000 files — 4.25 MB of
     payload, 751,665 DOM nodes, the tab wedged for over 45 seconds. The
     live report that found this (#2140's sibling) had ~211,000.

     So a run decides its own default from its own size, and an open run
     renders a bounded number of rows and says so. Neither number is a
     guess about the machine: they are the point past which a list stops
     being readable, which is a lower bound than the point past which it
     stops rendering. */
  //: A run this size or smaller opens on arrival; a bigger one waits to be
  //: asked. Twenty-five is about a screenful.
  const RUN_OPEN_MAX = 25;
  //: Rows built for one open run. Past this the run says what it is holding
  //: back and how to narrow it, rather than rendering a wall or lying by
  //: omission.
  const RUN_RENDER_CAP = 200;
  //: Runs the reader explicitly closed, and explicitly opened. Two sets, not
  //: one: without the second, opening a big run could not outrank the
  //: size default, and the click would appear to do nothing.
  const shutKinds = new Set();
  const openedKinds = new Set();
  const runIsShut = (kindKey, size) => (
    openedKinds.has(kindKey) ? false
      : shutKinds.has(kindKey) ? true
        : size > RUN_OPEN_MAX);

  function renderBundles() {
    const host = el("ax-groups");
    parkWork();
    const q = groupFilter.trim().toLowerCase();
    const groupsById = new Map((overview.groups || []).map((g) => [g.id, g]));
    const famName = new Map((overview.families || []).map((f) => [f.key, f.display_name]));

    // Only the kinds an admin hands out as a unit lead this view (the shared
    // `BUNDLE_LEAD`). The rest are reachable in the group view; leading with
    // 600 tables would bury the four rows that carry the decision.
    /* Grants indexed once, by the pair a row is keyed on. This used to be a
       `.filter()` over every grant INSIDE the item loop — fine at a few
       dozen items, quadratic at the size `corpus_file` reaches, and it ran
       before anything had a chance to collapse. */
    const heldBy = new Map();
    for (const g of (overview.grants || [])) {
      const k = `${g.resource_type}\u0000${g.resource_id}`;
      const at = heldBy.get(k);
      if (at) at.push(g); else heldBy.set(k, [g]);
    }
    const rows = [];
    for (const t of (overview.resources || [])) {
      if (!BUNDLE_LEAD.has(t.type_key)) continue;
      if (facetOn("kind") && !facetHas("kind", t.type_key)) continue;
      for (const b of (t.blocks || [])) {
        for (const i of (b.items || [])) {
          const hay = `${i.name || ""} ${i.slug || ""} ${i.resource_id || ""} ${i.owner_email || ""} ${b.name || ""} ${t.type_display || ""}`.toLowerCase();
          if (q && !hay.includes(q)) continue;
          const held = heldBy.get(`${t.type_key}\u0000${i.resource_id}`) || [];
          if (!rowPassesFacets(t.type_key, held)) continue;
          rows.push({ t, b, i, held });
        }
      }
    }
    if (!rows.length) {
      /* "Every bundle on the instance was searched" is the best sentence on
         this page when it is TRUE — it pre-empts exactly the doubt a miss
         creates. With a kind filter on it was false: only that kind was
         searched, and the thing the reader is looking for may be sitting one
         click away behind the filter. So the claim narrows to match the
         scope, and offers the way out. */
      host.innerHTML = `<div class="ax-empty">Nothing here is called “${esc(groupFilter)}”.
        ${anyFacetOn()
          ? `Filters are narrowing this —
             <button type="button" class="ax-linkbtn" data-chip-clear>search everything</button>.`
          : "Every resource on the instance was searched."}</div>`;
      paintCount(0, bundleTotal());
      return;
    }
    paintCount(rows.length, bundleTotal());
    /* Granted-to-nobody is the one state nobody goes looking for — but it
       is also, on a real instance, most of the list: every draft agent
       template anyone ever made. Floating them to the top buries the rows
       that carry a decision under a wall of ids. So they come OUT of the
       list and collect behind one line at the foot, counted by kind. */
    const byName = (a, c) => String(itemName(a.i)).localeCompare(String(itemName(c.i)));
    const nobody = rows.filter((r) => !r.held.length).sort(byName);
    const held = rows.filter((r) => r.held.length).sort(byName);

    //: One row shape across both views. The columns differ (a group's list
    //: names things, a bundle's list names audiences) but the anatomy —
    //: label, subject, control, consequence — does not.
    const groupRow = (r, grant) => {
      const tier = grant.requirement === "required" ? "required" : "available";
      const who = whoGranted(grant);
      /* An everyone-scoped grant is STORED against the seeded `Everyone`
         group as its carrier, so `grant.group_id` names a group here — and
         rendering it as one is exactly the attribution the scope exists to
         stop. It read "Everyone · 41 people", which invites the reader to
         believe a roster decides this, and that a member leaving would
         change it. Neither is true: the audience is every account, and
         anyone who joins later.

         `grant.audience` is the server's answer (`access-overview`), and it
         is computed with `reaches_everyone` so it is right on the frozen
         DuckDB ladder too, where there is no `scope` column at all. */
      const isEveryone = grant.audience === "everyone";
      const gid = isEveryone ? "everyone" : grant.group_id;
      const g = isEveryone ? null : groupsById.get(grant.group_id);
      if (!isEveryone && !g) return "";
      const label = isEveryone ? "Everyone" : titleOf(g);
      /* No count, because there is no number to give that would not be a
         smaller claim than the truth. */
      const detail = isEveryone
        ? "every account, and anyone who joins"
        : `${g.member_count ?? 0} ${(g.member_count ?? 0) === 1 ? "person" : "people"}`;
      return `
      <div class="ax-r${isEveryone ? " ax-r--scope" : ""}" data-kind="${esc(kindToken(r.t))}" data-type="${esc(r.t.type_key)}" data-rid="${esc(r.i.resource_id)}" data-gid="${esc(gid)}">
        <span class="ax-r__nm ax-r__nm--g">${AgnesKindGlyph.groupTile()}${isEveryone
          ? `<span>${esc(label)}</span>`
          /* The group's NAME is a way into the group. By resource named a
             group and stopped there: an admin reading "Data has this" could
             not see who is in Data, and there is no group detail page to
             send them to — /admin/groups/<id> is a 308 back to this page.
             So the name goes where the answer is: the same page, By group,
             with that group opened, which is exactly what the row's own
             audience question needs. Not an icon or a trailing arrow — the
             name IS the link, because the name is what the reader is
             already looking at when the question occurs to them. */
          : `<a class="ax-r__glink" href="?by=group&group=${encodeURIComponent(gid)}"
                title="Open ${esc(label)} — its members and everything else it can use"
                >${esc(label)}</a>`}</span>
        <span class="ax-r__d">${esc(detail)}${
          who ? `<span class="ax-r__sep"> · </span><span class="ax-r__who">granted by ${esc(who)}</span>` : ""}</span>
        <span class="ax-r__ctl">${controlCell(r.t.type_key, tier, { managedBy: grant.managed_by, publisherKind: r.i.publisher_kind })}</span>
        <span class="ax-r__rd ax-r__manage">${manageCell({ managedBy: grant.managed_by, typeKey: r.t.type_key })}</span>
      </div>`;
    };

    /* A bundle is a row that opens to what it holds — the same anatomy as a
       group, because it is the same question asked the other way round.
       Both views were on one page looking like two products: a group was a
       collapsible row with counts on the right, a bundle was a permanently
       open block with a table under it. They share the row component now,
       so the switch changes what the list is ABOUT, not what a list IS. */
    const section = (r) => {
      const nobody = !r.held.length;
      /* COUNT the groups. Naming up to three inline was an attempt to
         answer "who can see this" without expanding the row, and it does
         not survive real data: a package held by three groups printed
         "Data, Sales, Engineering · 8 people" — a list long enough to wrap,
         next to a headcount that is the sum of three rosters and belongs to
         none of them. The row below names every group, with its own reach
         and its own tier, which is the honest place for that detail. The
         summary is the number of audiences, and the caret says the rest is
         one click away. */
      const who = `${r.held.length} ${r.held.length === 1 ? "group" : "groups"}`;
      /* "granted to nobody" is the honest reach of a private upload, but on
         its own it reads as an oversight to fix. Naming the owner in the same
         line says which it is: nobody else can reach this AND it belongs to
         someone — i.e. working as intended. */
      const prov = itemProvenance(r.i);
      /* An everyone-scoped grant DOMINATES: naming the groups and counting
         their members is not merely mis-attributed here, it is the wrong
         quantity — the audience is not a roster, and no other group on the
         line adds anyone to it. `reach` is a count of today's accounts, and
         printing it invites the reader to believe that number is the answer.
         Straight from the server's `audience`, so it is right on DuckDB too. */
      const reachesAll = (r.held || []).some((g) => g.audience === "everyone");
      /* The headcount went with the group names. It is a UNION across the
         groups holding this — a number no row below it shows, and one that
         changes when someone joins a group that has nothing to do with this
         thing. Worse, sitting where it did it read as the answer to "who can
         see this", which is the question the rows below answer per group,
         each with its own reach and its own tier. What the summary owes the
         reader is how many audiences there are to look at. */
      const reachLine = nobody
        ? "granted to nobody"
        : reachesAll
          ? "everyone, and anyone who joins"
          : who;
      const meta = prov ? `${reachLine} · ${prov}` : reachLine;
      const bkey = `${r.t.type_key}:${r.i.resource_id}`;
      return `
      <details class="ax-gs ax-gs--bb${nobody ? " is-nobody" : ""}" data-bb="${esc(bkey)}"${
        openBundles.has(bkey) ? " open" : ""}>
        <summary class="ax-gs__hd">
          <span class="ax-gs__car" aria-hidden="true">›</span>
          ${kindTag(r.t)}
          <span class="ax-gs__id ax-gs__id--bb">
            <span class="ax-gs__line">
              <span class="ax-g__name">${esc(itemName(r.i))}</span>
              <span class="ax-gs__reach">${meta}</span>
            </span>
            ${r.i.description
              ? `<span class="ax-gs__desc">${esc(String(r.i.description).slice(0, 150))}</span>`
              : ""}
          </span>

        </summary>
        <div class="ax-gs__body">
          ${(() => {
            /* When Everyone already has this, every group already has it.
               "7 other groups could have it" was false, and a group grant
               beside an everyone grant does exactly one thing: it WINS for
               that group and can carry a different tier (a direct grant beats
               the inherited one — see grantsFor). So on a tiered kind the
               offer is that, named; on an untiered kind a group grant would
               change nothing, and nothing is offered. */
            const evGrant = r.held.find((g) => g.audience === "everyone");
            const tieredKind = TIERED.has(r.t.type_key);
            if (evGrant && !tieredKind) return "";
            const evTier = evGrant ? ((evGrant.requirement || "available") === "required" ? WORDS.tier_automatic : WORDS.tier_optional) : "";
            const otherTier = evTier === WORDS.tier_automatic ? WORDS.tier_optional : WORDS.tier_automatic;
            const share = `
              <button type="button" class="ax-add ax-add--bb" data-share-bundle
                      data-btype="${esc(r.t.type_key)}" data-brid="${esc(r.i.resource_id)}"
                      data-blabel="${esc(itemName(r.i))}">
                <span class="ax-add__plus" aria-hidden="true">+</span>
                <span class="ax-add__body">
                  <span class="ax-add__label">${evGrant ? "Set a different tier for a group" : nobody ? "Share it with a group" : "Share with another group"}</span>
                  ${(() => {
                    /* One case left, and it is the one that says something
                       the button cannot. Beside an everyone grant, a group
                       grant does not widen access — it can only carry a
                       DIFFERENT TIER, and without saying so the control
                       reads as "share it again" (U8).

                       The other two went. "7 other groups could have it"
                       counted the groups that do not have it, which is a
                       fact about the picker rather than about this thing,
                       and it moved with every revoke. "authored, then never
                       handed to anyone" repeated the row's own reach line
                       ("granted to nobody") two lines below it. */
                    if (evGrant) {
                      return `<span class="ax-add__hint">everyone already has it as ${
                        esc(evTier)} — a group can get it as ${esc(otherTier)} instead</span>`;
                    }
                    return "";
                  })()}
                </span>
              </button>`;
            /* Even with nothing granted the table is the right shape: a
               header, the action as its first row, and no rows under it —
               rather than a button floating beside an empty sentence. */
            return `<div class="ax-table">
                      ${share}
                      <div class="ax-colhd">
                        <span>Group</span><span>Who that reaches</span>
                        <span>Access tier <span class="ax-tip ax-colhd__key"><span class="ax-tip__btn" tabindex="0" role="img" aria-label="What Automatic and Optional mean" aria-describedby="ax-tierkey-body">i</span></span></span><span class="ax-colhd__u">Manage</span>
                      </div>
                      ${r.held.map((grant) => groupRow(r, grant)).join("")}
                    </div>`;
          })()}
        </div>
      </details>`;
    };

    /* An open run renders at most `RUN_RENDER_CAP` rows and then SAYS SO.
       Silently truncating would be the worse half of the bug this replaces:
       a reader who cannot find a file would conclude it is not granted,
       from a list that had simply stopped early. The line names the real
       total and the two controls that narrow it. */
    const cappedRuns = (list, kindDisplay) => {
      const shown = list.slice(0, RUN_RENDER_CAP);
      const hidden = list.length - shown.length;
      const body = shown.map(section).join("");
      if (!hidden) return body;
      /* The kind's own word where there is one ("…of 30,010 files"); nothing
         where the list is already mixed, because the drawer below holds
         several kinds and naming one of them would be wrong. */
      const word = kindDisplay ? ` ${String(kindDisplay).toLowerCase()}` : "";
      return body + `<div class="ax-res__msg ax-more">Showing ${RUN_RENDER_CAP}
        of ${list.length.toLocaleString()}${esc(word)} — search by name, or
        narrow with Filter, to find a specific one.</div>`;
    };

    // "2 data packages, 4 agents" — counted by kind, because "9 things" is
    // not a sentence anyone can act on.
    const tally = new Map();
    for (const r of nobody) {
      const k = r.t.type_display;
      tally.set(k, (tally.get(k) || 0) + 1);
    }
    const nobodyLine = nobody.length ? `
      <details class="ax-nobody" data-nobody${nobodyOpen ? " open" : ""}>
        <summary>
          <b>Granted to nobody:</b>
          ${[...tally].map(([k, n]) => {
            // `type_display` is already plural ("Memory domains"), so a count
            // of one read "1 memory domains".
            const word = String(k).toLowerCase();
            return `${n} ${esc(n === 1 ? word.replace(/s$/, "") : word)}`;
          }).join(", ")}.
          <span class="ax-nobody__hint">Authored, then never handed to anyone.</span>
          <span class="ax-nobody__more">${nobodyOpen ? "Hide" : "Show them"}</span>
        </summary>
        <div>${nobodyOpen ? cappedRuns(nobody, "") : ""}</div>
      </details>` : "";

    /* Grouped, not alphabetical. A flat A-Z list interleaves a plugin, a
       package, a memory domain and a collection — four different kinds of
       decision — so the reader sorts them mentally on every pass. The same
       two levels the group view uses: FAMILY as a band, and the kind as a
       quiet run inside it, so all the packages sit together under Knowledge
       and all the plugins under Capabilities. */
    const famOrder = (overview.families || []).map((f) => f.key);
    const inFamily = new Map();
    for (const r of held) {
      const fam = r.t.family || "knowledge";
      if (!inFamily.has(fam)) inFamily.set(fam, []);
      inFamily.get(fam).push(r);
    }
    const painted = (overview.families || []).map((f) => {
      const mine = inFamily.get(f.key) || [];
      if (!mine.length) return "";
      // Kinds in the registry's own order, names alphabetical inside a kind.
      const kindRank = new Map((overview.resources || []).map((t, i) => [t.type_key, i]));
      mine.sort((a, b) => (kindRank.get(a.t.type_key) ?? 99) - (kindRank.get(b.t.type_key) ?? 99)
        || byName(a, b));
      /* The runs, built as runs. They used to be a flat list with a header
         injected wherever the kind changed, which meant a run never knew
         its own size — and a run has to know that to decide whether it
         opens, and to say how much it is holding back. */
      const runs = [];
      for (const r of mine) {
        const last = runs[runs.length - 1];
        if (last && last.t.type_key === r.t.type_key) last.items.push(r);
        else runs.push({ t: r.t, items: [r] });
      }
      const body = runs.map((run) => {
        const shut = runIsShut(run.t.type_key, run.items.length);
        const head = `<button type="button" class="fbar-grouptoggle ax-kindrun" data-kindrun="${esc(run.t.type_key)}"
                     data-kind="${esc(kindToken(run.t))}" aria-expanded="${shut ? "false" : "true"}">
               <svg class="fbar-group__caret" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m6 9 6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
               <span class="fbar-group__title">${esc(run.t.type_display)}</span>
               <span class="fbar-group__n">${run.items.length}</span>
               ${run.t.type_description ? `<span class="fbar-group__hint ax-kindcaveat">${esc(run.t.type_description)}</span>` : ""}
             </button>`;
        return head + (shut ? "" : cappedRuns(run.items, run.t.type_display));
      }).join("");
      return `
      <div class="ax-fam">
        <div class="fbar-groupband ax-fam__hd">
          <div class="fbar-grouptoggle" role="presentation">
            <span class="fbar-group__title">${esc(f.display_name)}</span>
            <span class="fbar-group__n">${mine.length}</span>
            ${f.blurb ? `<span class="fbar-group__hint">${esc(f.blurb)}</span>` : ""}
          </div>
        </div>
        ${body}
      </div>`;
    }).join("");

    /* `painted` covers only the bundles that ARE granted; the ungranted ones
       live in `nobodyLine` below it. So an empty `painted` means "nothing
       matching is granted", not "nothing on this instance is granted" — and
       with a search or a filter on, the old sentence was flatly false about
       an instance with grants. */
    const noneGrantedMsg = (groupFilter.trim() || anyFacetOn())
      ? "Nothing matching this is granted to any group."
      : "Nothing on this instance is granted to anyone yet.";
    /* Above the list, not under it. "Granted to nobody" is the one state
       nobody goes looking for, which is exactly why it cannot be the last
       line on a page that scrolls: at the foot of a long list it is reached
       only by someone who has already read past everything they came for.
       It is one collapsed line either way, so it costs the reader nothing
       to have it where they will see it. */
    host.innerHTML = nobodyLine + (painted
      || `<div class="ax-empty">${noneGrantedMsg}</div>`);
  }

  //: Delegated: the row lives inside `#ax-groups`, which every repaint
  //: rewrites, so a listener bound to the element would be lost on the first
  //: grant written.
  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-new-group]")) openGroupDrawer(null);
  });

  /* The facet checkboxes. `change`, not `click`: a `<label>` wrapping an
     `<input>` delivers the click twice — once for each — and a click handler
     that toggles a Set would put the value straight back where it started.
     The menu is NOT closed on a pick: multi-select means the next pick is
     the likely next act, and closing after each one would make picking three
     values three trips. `Done` closes it. */
  document.addEventListener("change", async (e) => {
    const box = e.target.closest(".ax-filter [data-facet]");
    if (!box) return;
    const set = facets.get(box.dataset.facet);
    if (!set) return;
    if (box.checked) set.add(box.value); else set.delete(box.value);
    syncUrl({ push: true });
    await repaint();
    // The repaint rebuilt the menu, so the submenu the reader was working
    // in has to be put back — otherwise every tick collapses the category.
    const menu = el("ax-filter-menu");
    const cat = menu && menu.querySelector(`.fbar-cat[data-cat="${box.dataset.facet}"]`);
    if (cat) {
      const pop = cat.querySelector(".fbar-cat__pop");
      if (pop) pop.hidden = false;
      const btn = cat.querySelector(".fbar-cat__btn");
      if (btn) btn.setAttribute("aria-expanded", "true");
    }
  });

  /* The group menu, delegated: every repaint replaces the rows, so binding
     to the buttons themselves would leave the listeners on detached nodes.
     Each handler stops the event reaching the row button underneath — using
     the menu must not also change which group the pane is showing — and
     closes the menu first, since leaving one open behind a drawer or a
     confirm returns you to a popover floating over a row that may be gone. */
  document.addEventListener("click", (e) => {
    const kebab = e.target.closest("[data-gmenu]");
    if (kebab) { e.stopPropagation(); e.preventDefault(); toggleGroupMenu(kebab.dataset.gmenu); return; }

    const rename = e.target.closest("[data-grename]");
    if (rename) {
      e.stopPropagation();
      closeGroupMenu();
      const group = (overview.groups || []).find((g) => g.id === rename.dataset.grename);
      if (group) openGroupDrawer(group);
      return;
    }

    const del = e.target.closest("[data-gdelete]");
    if (del) {
      e.stopPropagation();
      const id = del.dataset.gdelete;
      closeGroupMenu();
      deleteGroup(id);
    }
  }, true);   // capture: beat the row-selection handler to the event

  /* ── Narrowing the grant tree ─────────────────────────────────────────
     The filter the retired detail page had. Both controls re-render rather
     than hide rows, so the per-section counts stay true to what is showing. */
  /* The working set (filter text + scope) survives leaving for an entity
     page and coming back — the commonest interruption on this page is
     "what IS that row?", and returning to a reset tree made every careful
     grant decision cost the search twice. Same store and same reasoning as
     SELECTED_KEY above: this sitting, not forever. */
  const WORKSET_KEY = "agnes.admin.access.workset";
  function rememberWorkset() {
    try {
      sessionStorage.setItem(WORKSET_KEY, JSON.stringify({ f: resourceFilter }));
    } catch (e) { /* private mode */ }
  }
  /* The per-group search input and the All/Granted segment were removed
     with the catalogue they filtered: the header search narrows the list,
     and the list is the holding. Nothing left here to bind. */

  /* The tier key needs no handler: it is a CSS hover/focus tooltip now.
     It was a permanent accent panel between the two sections (where it read
     as an alert about the members above it), then a click-to-open popover
     — which asks for a decision to read one sentence of reference. What it
     does still need is to not reach whatever it sits inside: a click on it
     would otherwise collapse the row underneath.

     DELEGATED, because the key moved onto the ACCESS TIER column header
     when the Access section head was removed — and that header is rebuilt on
     every repaint. Binding to the element found at module-eval time threw on
     a null (there is no `.ax-tip` in the document until the first render),
     which killed the whole module before it could boot. */
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".ax-tip")) return;
    e.stopPropagation();
    e.preventDefault();
  }, true);

  /* Dismissal, once, for all three popovers on the page. Escape closes the
     topmost thing that is open; a click outside closes whichever the click
     was not inside. */
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    closeFind();
    closeGroupMenu();
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".ax-find-wrap")) closeFind();
    if (!e.target.closest(".ax-grow")) closeGroupMenu();
  });

  /* ── Arriving with a resource in mind ─────────────────────────────────
     /admin/tables' per-row "Manage access" sends `?resource=<type>:<id>`.
     Grants key on the group, so a group still has to be chosen — what the
     workspace changes is that choosing one is a click in the left column
     rather than a page load, and the resource is already filtered on the
     right when you get there. The banner explains why the tree is narrowed,
     which the filter box alone cannot: nobody typed that value. */
  const pickParam = new URLSearchParams(window.location.search).get("resource") || "";
  const PICK = pickParam.includes(":")
    ? { type: pickParam.slice(0, pickParam.indexOf(":")), id: pickParam.slice(pickParam.indexOf(":") + 1) }
    : (pickParam ? { type: "", id: pickParam } : null);

  function renderPick() {
    const host = el("ax-pick");
    if (!PICK || !resourceFilter) { host.innerHTML = ""; return; }

    /* A TABLE arriving from /admin/tables is the one case where the obvious
       answer is the wrong one. A per-table grant no longer surfaces the
       table in an analyst's manifest — the package grant does — so pointing
       the admin at a table row would hand them a lever that is not
       connected. The honest answer is which packages carry it, and who
       those reach. */
    if (PICK.type === "table") {
      const pkgType = (overview.resources || []).find((t) => t.type_key === "data_package");
      const pkgs = [];
      for (const b of (pkgType ? pkgType.blocks || [] : [])) {
        for (const i of (b.items || [])) {
          if ((i.contains || []).includes(PICK.id)) pkgs.push(i);
        }
      }
      if (!pkgs.length) {
        host.innerHTML = `<span><code>${esc(PICK.id)}</code> is in no data package, so no analyst
          receives it. A table reaches people by being in a package —
          <a href="/admin/data-packages">put it in one</a>, then grant that.</span>
          <button type="button" class="ax-linkbtn" data-clear-rfind>Show everything</button>`;
        return;
      }
      const held = (pkg) => (overview.grants || [])
        .filter((g) => g.resource_type === "data_package" && g.resource_id === pkg.resource_id)
        .map((g) => (overview.groups || []).find((x) => x.id === g.group_id))
        .filter(Boolean);
      const lines = pkgs.map((pkg) => {
        const gs = held(pkg);
        return `<b>${esc(pkg.name)}</b> — ${gs.length
          ? `granted to ${gs.map((g) => esc(titleOf(g))).join(", ")}`
          : `<i>granted to nobody</i>`}`;
      }).join("; ");
      host.innerHTML = `<span><code>${esc(PICK.id)}</code> reaches people through
        ${pkgs.length === 1 ? "one package" : `${pkgs.length} packages`}: ${lines}.
        Grant the package, not the table.</span>
        <button type="button" class="ax-linkbtn" data-clear-rfind>Show everything</button>`;
      return;
    }

    host.innerHTML = `<span>Granting access to <code>${esc(PICK.id)}</code> — open the group that should
      get it, then tick it.</span>
      <button type="button" class="ax-linkbtn" data-clear-rfind>Show everything</button>`;
  }

  function clearResourceFilter() {
    resourceFilter = "";
    const input = el("ax-group-find");
    if (input) input.value = "";
    groupFilter = "";
    renderPick();
    renderResources();
  }

  document.addEventListener("click", async (e) => {
    if (e.target.closest("[data-clear-rfind]")) { clearResourceFilter(); return; }

    const share = e.target.closest("[data-share-bundle]");
    if (share) {
      openBundlePicker(share.dataset.btype, share.dataset.brid, share.dataset.blabel);
      return;
    }

    if (e.target.closest("[data-add-grant]")) {
      if (selectedGroup) openPicker(selectedGroup);
      return;
    }

    if (e.target.closest("#ax-filter-btn")) {
      const menu = el("ax-filter-menu");
      const btn = el("ax-filter-btn");
      const show = menu.hidden;
      menu.hidden = !show;
      btn.setAttribute("aria-expanded", show ? "true" : "false");
      return;
    }
    if (e.target.closest("[data-chip-clear]")) {
      // "Clear all" left the search term live once, and with the chips gone
      // there was nothing on screen explaining why the list was still
      // narrowed. It means all of it: every facet AND the search.
      clearFacets();
      groupFilter = "";
      const find = el("ax-group-find");
      if (find) find.value = "";
      syncUrl({ push: true });
      await repaint();
      return;
    }
    // One chip's × removes one VALUE, leaving the rest of that facet on.
    const chipDrop = e.target.closest("[data-chip-drop]");
    if (chipDrop) {
      facets.get(chipDrop.dataset.chipDrop).delete(chipDrop.dataset.chipVal);
      syncUrl({ push: true });
      await repaint();
      return;
    }
    // A category head opens its own submenu and closes its siblings — one
    // open pop at a time, the Library's behaviour.
    const catBtn = e.target.closest(".ax-filter .fbar-cat__btn");
    if (catBtn) {
      const cat = catBtn.closest(".fbar-cat");
      const pop = cat.querySelector(".fbar-cat__pop");
      const opening = pop.hidden;
      for (const other of el("ax-filter-menu").querySelectorAll(".fbar-cat__pop")) other.hidden = true;
      for (const other of el("ax-filter-menu").querySelectorAll(".fbar-cat__btn")) other.setAttribute("aria-expanded", "false");
      pop.hidden = !opening;
      catBtn.setAttribute("aria-expanded", opening ? "true" : "false");
      return;
    }
    if (e.target.closest(".ax-filter [data-fbar-clear]")) {
      clearFacets();
      syncUrl({ push: true });
      await repaint();
      return;
    }
    if (e.target.closest(".ax-filter [data-fbar-done]")) {
      el("ax-filter-menu").hidden = true;
      el("ax-filter-btn").setAttribute("aria-expanded", "false");
      return;
    }
    // A click anywhere else closes the menu, as the Library's does.
    if (!e.target.closest("#ax-filter-menu")) {
      const menu = el("ax-filter-menu");
      if (menu && !menu.hidden) {
        menu.hidden = true;
        el("ax-filter-btn").setAttribute("aria-expanded", "false");
      }
    }

    const byBtn = e.target.closest("[data-by]");
    if (byBtn) {
      const next = _normalizeBy(byBtn.dataset.by) || "group";
      if (next !== viewMode) {
        viewMode = next;
        // The view rides the URL, so a link can hand someone the direction
        // that answers their question rather than the one that answers mine.
        // pushState, not replace: switching lens is exactly the move Back
        // should undo, and with replaceState it walked you out of the page.
        syncUrl({ push: true });
        showPane(viewMode);
        // Arriving by URL is no longer the only way into the person view.
        if (viewMode === "person") loadUsers();
        paintTabState();
        await repaint();
      }
      return;
    }

    // The people strip's roster disclosure. The body is rendered already —
    // this only reveals it — so there is nothing to fetch and nothing to
    // repaint, which is what keeps it instant on a large group.
    const pplToggle = e.target.closest("#ax-sec-people");
    if (pplToggle) {
      const body = el("ax-members");
      const open = pplToggle.getAttribute("aria-expanded") !== "true";
      pplToggle.setAttribute("aria-expanded", open ? "true" : "false");
      if (body) body.hidden = !open;
      return;
    }

    // The retry offered by the members error state. Nothing to invalidate —
    // a failed read is never cached — so this is simply the read again.
    if (e.target.closest("[data-retry-members]")) {
      renderMembers();
      return;
    }

    const nobodySum = e.target.closest("[data-nobody] > summary");
    if (nobodySum) {
      // Let <details> toggle natively; record which way, so the repaint after
      // the next share puts it back. The label is swapped here rather than
      // waiting for that repaint — a native toggle does not trigger one, so
      // the summary would otherwise still read "Show them" while open.
      nobodyOpen = !nobodySum.parentElement.hasAttribute("open");
      // The rows inside are built only while this is open (see `nobodyLine`),
      // so a native toggle is no longer enough — opening has to ask for them.
      renderBundles();
      return;
    }

    const kindRun = e.target.closest("[data-kindrun]");
    if (kindRun) {
      const k = kindRun.dataset.kindrun;
      // `aria-expanded` is what the run currently IS, whichever way it got
      // there — the size default included. Reading the DOM rather than the
      // sets is what makes one click always do the opposite of what the
      // reader can see.
      const wasOpen = kindRun.getAttribute("aria-expanded") === "true";
      shutKinds.delete(k);
      openedKinds.delete(k);
      (wasOpen ? shutKinds : openedKinds).add(k);
      renderBundles();
      return;
    }

    const bbSum = e.target.closest(".ax-gs--bb > .ax-gs__hd");
    if (bbSum && !e.target.closest("[data-tier]") && !e.target.closest("[data-revoke]")
        && !e.target.closest("[data-share-bundle]")) {
      // Let the <details> toggle natively; just remember which way it went,
      // so the next repaint can put it back.
      const sec = bbSum.closest("[data-bb]");
      const key = sec && sec.dataset.bb;
      if (key) {
        if (sec.hasAttribute("open")) openBundles.delete(key);
        else openBundles.add(key);
      }
      return;
    }

    /* Opening a section is selecting the group. One at a time: two open
       groups would mean two grant trees on screen with one toolbar between
       them, and the row you clicked scrolled off. The neighbours stay as
       lines, which is the whole reason the pane went away. */
    const sum = e.target.closest(".ax-gs__hd");
    if (sum && !e.target.closest("[data-gmenu]") && !e.target.closest(".ax-gs__sim")) {
      const sec = sum.closest("[data-gs]");
      const gid = sec && sec.dataset.gs;
      if (gid) {
        e.preventDefault();
        const wasOpen = selectedGroup === gid && sec.hasAttribute("open");
        selectedGroup = wasOpen ? null : gid;
        rememberSelection(selectedGroup);
        // Which group is open is the single most linkable fact on this page,
        // and it was the one the URL never carried.
        syncUrl({ push: true });
        await repaint();
      }
      return;
    }

    const inv = e.target.closest("[data-invite]");
    if (inv) {
      inv.disabled = true;
      try { await inviteAndAdd(inv.dataset.invite); } finally { inv.disabled = false; }
      return;
    }
    const invTyped = e.target.closest("[data-invite-typed]");
    if (invTyped) {
      const field = invTyped.closest(".ax-invite").querySelector(".ax-invite__mail");
      const addr = field ? field.value.trim() : "";
      if (!addr) { field && field.focus(); return; }
      invTyped.disabled = true;
      try { await inviteAndAdd(addr); } finally { invTyped.disabled = false; }
      return;
    }

    const add = e.target.closest("[data-addmember]");
    if (add) {
      add.disabled = true;
      try { await addMember(add.dataset.email); } finally { add.disabled = false; }
      return;
    }
    const rm = e.target.closest("[data-rmmember]");
    if (rm) {
      rm.disabled = true;
      try { await removeMember(rm.dataset.rmmember); } finally { rm.disabled = false; }
      return;
    }

    /* ── Grant or revoke a whole bucket ─────────────────────────────────
       The reason the hierarchy is worth having: "give this group the revenue
       bucket" is one gesture rather than four, and "take it all back" is one
       rather than four. The rule for what a click MEANS is the honest one
       for a tri-state box: anything less than fully granted grants the rest;
       a fully granted bucket revokes. That way the box never asks you to
       click twice to get to the state it is already showing.

       Writes go through the same per-item endpoints as a single row — there
       is no bulk API, and inventing one here would put a second grant path
       behind this page. They run in sequence so a failure halfway leaves a
       coherent partial state the re-render then shows accurately. */
    const bucketBox = e.target.closest("[data-bucket]");
    if (bucketBox && selectedGroup) {
      const [type, blockName] = bucketBox.dataset.bucket.split("|");
      const t = (overview.resources || []).find((x) => x.type_key === type);
      const b = t && (t.blocks || []).find((x) => x.name === blockName);
      if (!b) return;
      const items = b.items || [];
      const grantAll = bucketBox.dataset.state !== "all";
      bucketBox.disabled = true;
      let done = 0, failed = 0;
      try {
        for (const i of items) {
          const existing = grantOf(selectedGroup, type, i.resource_id);
          try {
            if (grantAll && !existing) { await writeGrant(type, i.resource_id, "available"); done++; }
            else if (!grantAll && existing) { await deleteGrant(existing); done++; }
          } catch (err) {
            // A colleague got there first; the outcome the admin asked for holds.
            if (err.message === "changed_elsewhere") { done++; continue; }
            failed++;
          }
        }
      } finally {
        bucketBox.disabled = false;
        renderResources();
        renderGroups();
      }
      const noun = `${done} item${done === 1 ? "" : "s"}`;
      toast(failed
        ? `${blockName}: ${noun} changed, ${failed} failed.`
        : grantAll
          ? `${blockName} granted — ${noun} added (optional).`
          : `${blockName} revoked — ${noun} removed.`, !failed);
      return;
    }

    /* NOT `tr[data-rid]`. The group view's rows became CSS-grid <div>s when
       the table was flattened, and this selector kept matching only the
       bundle view's <tr>s — so Available/Required and Revoke silently did
       nothing on the surface people actually use. Match the data attribute,
       which is what identifies a row; the element it sits on is a layout
       decision and may change again. */
    const row = e.target.closest("[data-rid]");
    if (!row) return;
    // By bundle renders one row per GROUP, so the row carries its own; the
    // group view has none and falls back to the open section.
    const rowGroup = row.dataset.gid || selectedGroup;
    if (!rowGroup) return;
    const type = row.dataset.type;
    const rid = row.dataset.rid;

    if (e.target.matches('input[type="checkbox"]')) {
      const box = e.target;
      box.disabled = true;
      try {
        const grant = grantOf(rowGroup, type, rid);
        if (box.checked && !grant) {
          await writeGrant(type, rid, "available", rowGroup);
          toast("Granted — available", true);
        } else if (!box.checked && grant) {
          await deleteGrant(grant);
          toast("Grant removed", true);
        }
      } catch (err) {
        if (err.message === "changed_elsewhere") return;   // already refetched and said so
        box.checked = !box.checked;
        toast("Could not save: " + err.message, false);
      } finally {
        box.disabled = false;
        repaintView();
      }
      return;
    }

    if (e.target.closest("[data-revoke]")) {
      const grant = grantOf(rowGroup, type, rid);
      if (!grant) return;
      /* Revoke used to be one click, instantly, with no confirmation and no
         undo — on a control that takes access away from everyone in a group.
         The delete-group flow already sets the standard: quantify the blast
         radius rather than ask "are you sure?". */
      const g = (overview.groups || []).find((x) => x.id === rowGroup);
      const people = g ? (g.member_count ?? 0) : 0;
      const reveals = type === "memory_domain";
      const okRevoke = await window.confirmModal(reveals ? {
        // Nobody "loses" a memory domain: the grant only revealed it to this
        // group, and it hides nothing from anyone else. Saying "N people lose
        // it" here would be the false claim the relabel exists to stop.
        title: `Stop revealing “${revokeLabel(type, rid)}” to ${g ? titleOf(g) : "this group"}?`,
        message: `This group stops seeing items from this memory domain. `
          + `It hides nothing from anyone else — a memory-domain grant only reveals; it never restricts. `
          + `You can reveal it again from this page.`,
        confirmText: "Stop revealing",
      } : {
        title: `Revoke “${revokeLabel(type, rid)}”?`,
        message: `${people} ${people === 1 ? "person" : "people"} in ${g ? titleOf(g) : "this group"} `
          + `lose it, unless another group also grants it to them. `
          + `You can grant it again from this page.`,
        confirmText: "Revoke",
      });
      if (!okRevoke) return;
      const memo = focusMemo();
      try {
        await deleteGrant(grant);
        // NOT "the owner can share it again from the Library": this is an
        // admin-created group grant, so the admin made it here and remakes it
        // here — the Library's owner is not in that loop at all.
        toast(reveals
          ? `No longer revealed to this group — nothing was hidden from anyone else`
          : `Revoked — grant it again from this page whenever you like`, true);
      } catch (err) {
        toast((reveals ? "Could not change: " : "Could not revoke: ") + err.message, false);
      }
      repaintView();
      focusRestore(memo);
      return;
    }

    const tierBtn = e.target.closest("[data-tier]");
    if (tierBtn) {
      const grant = grantOf(rowGroup, type, rid);
      if (!grant || grant.requirement === tierBtn.dataset.tier) return;
      const tierMemo = focusMemo();
      try {
        await updateGrant(grant, tierBtn.dataset.tier);
        toast(tierSentence(type, tierBtn.dataset.tier), true);
      } catch (err) {
        if (err.message === "changed_elsewhere") return;   // already refetched and said so
        toast("Could not save: " + err.message, false);
      }
      repaintView();
      focusRestore(tierMemo);
    }
  });


  /* ── + Add: the one way to grant ──────────────────────────────────────
     Everything the group does NOT have, in one overlay, chosen and applied
     in a single act. This replaces two browsing surfaces (the All/Granted
     scope and the Advanced tree) that answered the same question in two
     places — and it means the group's own list can stay what it is, the
     holding, with nothing on it that is not held.

     Uses `.ds-drawer`, the app's own overlay chrome (see
     `js/components/group_drawer.js`), rather than a modal invented here. */
  let pickerEls = null;
  /* Two directions, one act. `mode:"resources"` picks things for a group
     (opened from a group's + Add); `mode:"bundle"` picks GROUPS for one
     bundle (opened from a bundle's + Add in the By bundle view). The same
     overlay, the same Apply, the same grant rows written either way — a
     grant has no direction, only the question you arrived with does. */
  let pickerState = {
    mode: "resources", group: null, bundle: null, chosen: new Set(), q: "",
    kind: "",                 //: "" = every kind
    collapsed: new Set(),     //: families folded away, by key
  };

  function buildPicker() {
    if (pickerEls) return pickerEls;
    const root = document.createElement("div");
    root.className = "ds-drawer ax-picker";
    root.hidden = true;
    root.dataset.noEscClose = "1";
    root.innerHTML =
      '<div class="ds-drawer__backdrop" data-pk-close></div>' +
      '<div class="ds-drawer__panel" role="dialog" aria-modal="true" aria-labelledby="pk-title">' +
      '  <div class="ds-drawer__head">' +
      '    <div class="ds-drawer__head-main">' +
      '      <h2 class="ds-drawer__title" id="pk-title">Add to this group</h2>' +
      '      <p class="ds-drawer__sub" data-pk="sub"></p>' +
      '    </div>' +
      '    <button type="button" class="ds-drawer__x" data-pk-close aria-label="Close">&times;</button>' +
      '  </div>' +
      '  <div class="ds-drawer__body">' +
      '    <div class="ax-picker__bar">' +
      '      <div class="fbar__search ax-picker__find">' +
      '        <input type="search" data-pk="q" placeholder="Search everything grantable…" aria-label="Search what to add">' +
      '      </div>' +
      '      <div class="fbar-filter ax-picker__filter">' +
      '        <button type="button" class="fbar-filter__btn" data-pk="filterbtn" aria-haspopup="true" aria-expanded="false">' +
      '          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 5h16M7 12h10M10 19h4"></path></svg>' +
      '          Filter<span class="fbar-filter__n" data-pk="filtern" hidden>0</span>' +
      '        </button>' +
      '        <div class="fbar-menu fbar-menu--cats" data-pk="kinds" role="menu" aria-label="Filter by kind" hidden></div>' +
      '      </div>' +
      '    </div>' +
      '    <div class="fbar-chips ax-picker__chips" data-pk="chips" hidden></div>' +
      '    <div class="ax-picker__list" data-pk="list"></div>' +
      '  </div>' +
      '  <div class="ds-drawer__foot">' +
      /* The tier was decided FOR the admin here and mentioned in grey in the
         subtitle: "Anything with a tier is added as Available." That is the
         one field deciding whether anyone actually RECEIVES the thing —
         Optional leaves it for them to take, Automatic puts it in every
         workspace — chosen silently, by the surface, on their behalf.

         Asked in the footer instead, beside Apply, because that is the last
         thing read before the write. Hidden unless something tiered is
         selected: twelve of the sixteen kinds have no such choice, and a
         control that cannot act is what this effort keeps removing. */
      '    <span class="ax-picker__tier" data-pk="tier" hidden>' +
      '      <span class="ax-picker__tierq">Added as</span>' +
      '      <span class="fbar-seg ax-tier" data-pk="tierseg" role="group" aria-label="Access tier for what is added">' +
      '        <button type="button" class="fbar-seg__btn is-active" data-pk-tier="available">' + esc(WORDS.tier_optional) + '</button>' +
      '        <button type="button" class="fbar-seg__btn" data-pk-tier="required">' + esc(WORDS.tier_automatic) + '</button>' +
      '      </span>' +
      '    </span>' +
      '    <span class="ax-picker__count" data-pk="count">Nothing selected</span>' +
      '    <button type="button" class="btn btn-secondary" data-pk-close>Cancel</button>' +
      '    <button type="button" class="btn btn-primary" data-pk-apply disabled>Apply</button>' +
      '  </div>' +
      '</div>';
    document.body.appendChild(root);
    pickerEls = {
      root,
      sub: root.querySelector('[data-pk="sub"]'),
      q: root.querySelector('[data-pk="q"]'),
      list: root.querySelector('[data-pk="list"]'),
      count: root.querySelector('[data-pk="count"]'),
      tier: root.querySelector('[data-pk="tier"]'),
      tierseg: root.querySelector('[data-pk="tierseg"]'),
      apply: root.querySelector("[data-pk-apply]"),
    };

    root.addEventListener("click", async (e) => {
      if (e.target.closest("[data-pk-close]")) { closePicker(); return; }
      if (e.target.closest("[data-pk-apply]")) { await applyPicker(); return; }
      const tierBtn = e.target.closest("[data-pk-tier]");
      if (tierBtn) {
        pickerState.tier = tierBtn.dataset.pkTier;
        paintPickerTier();
        return;
      }
      const fam = e.target.closest("[data-pk-fam]");
      if (fam) {
        const key = fam.dataset.pkFam;
        if (pickerState.collapsed.has(key)) pickerState.collapsed.delete(key);
        else pickerState.collapsed.add(key);
        paintPicker();
        return;
      }
      if (e.target.closest('[data-pk="filterbtn"]')) {
        // `root`, not `els.root`: this listener is installed inside
        // buildPicker, where the element map is still being assembled and is
        // named `pickerEls`. Reading `els` here threw, so the button did
        // nothing at all — silently, because the throw was inside a handler.
        const menu = root.querySelector('[data-pk="kinds"]');
        const show = menu.hidden;
        menu.hidden = !show;
        e.target.closest('[data-pk="filterbtn"]').setAttribute("aria-expanded", show ? "true" : "false");
        return;
      }
      const kindBtn = e.target.closest("[data-pk-kind]");
      if (kindBtn) {
        const val = kindBtn.dataset.pkKind || "";
        pickerState.kind = (kindBtn.type === "checkbox" && !kindBtn.checked) ? "" : val;
        const menu = root.querySelector('[data-pk="kinds"]');
        if (menu) menu.hidden = true;
        paintPicker();
        return;
      }
      const row = e.target.closest("[data-pk-item]");
      if (row) {
        const key = row.dataset.pkItem;
        if (pickerState.chosen.has(key)) pickerState.chosen.delete(key);
        else pickerState.chosen.add(key);
        paintPicker();
      }
    });
    pickerEls.q.addEventListener("input", (e) => { pickerState.q = e.target.value; paintPicker(); });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !root.hidden) closePicker();
    });
    return pickerEls;
  }

  //: The groups that do NOT already hold this bundle.
  /* Who this resource can be given to. Decision 04: the list holds groups
     someone actually made, and "give this to everyone" sits beside them as
     an explicit choice rather than as a group that happens to hold
     everybody.

     The carrier is filtered OUT as a group and re-offered as the scope. It
     is the same row in the database either way — an everyone grant is stored
     against the seeded group because the column is NOT NULL — but offering
     it twice would let an admin pick "everyone" and "the Everyone group" as
     if they were different audiences, and then hit the unique key. */
  function pickerGroupCandidates() {
    const q = pickerState.q.trim().toLowerCase();
    const b = pickerState.bundle;
    const groups = (overview.groups || []).filter((g) => {
      if (g.is_everyone) return false;
      if (grantOf(g.id, b.type, b.id)) return false;
      const hay = `${titleOf(g)} ${g.name || ""} ${g.description || ""} ${g.mapped_email || ""}`.toLowerCase();
      return !q || hay.includes(q);
    });
    /* Withheld for the four types where a grant does not mean "this audience
       gets the thing" (decision 07) — the same list the server refuses with
       a 422, so the page cannot offer a choice the API would reject. */
    const scopeOk = !SCOPE_WITHHELD_TYPES.has(b.type);
    const held = everyoneHeld(b.type, b.id);
    const offerScope = scopeOk && !held
      && (!q || "everyone every account workspace".includes(q));
    return offerScope ? [EVERYONE_AUDIENCE, ...groups] : groups;
  }

  //: Everything grantable the group does not already hold, by family.
  function pickerCandidates() {
    const q = pickerState.q.trim().toLowerCase();
    const out = new Map();
    for (const t of (overview.resources || [])) {
      for (const b of (t.blocks || [])) {
        for (const i of (b.items || [])) {
          if (grantOf(pickerState.group, t.type_key, i.resource_id)) continue;
          const hay = `${i.name || ""} ${i.slug || ""} ${i.resource_id || ""} ${i.owner_email || ""} ${b.name || ""} ${t.type_display || ""}`.toLowerCase();
          if (q && !hay.includes(q)) continue;
          if (pickerState.kind && t.type_key !== pickerState.kind) continue;
          const fam = t.family || "knowledge";
          if (!out.has(fam)) out.set(fam, []);
          out.get(fam).push({ t, i, block: (b.name && b.name !== t.type_display) ? b.name : "" });
        }
      }
    }
    return out;
  }

  function paintPicker() {
    const els = buildPicker();
    if (pickerState.mode === "bundle") return paintGroupPicker(els);
    const cands = pickerCandidates();
    const families = overview.families || [];
    const total = [...cands.values()].reduce((n, a) => n + a.length, 0);
    if (!total) {
      els.list.innerHTML = pickerState.q
        ? `<div class="ax-empty">Nothing left matches “${esc(pickerState.q)}”.</div>`
        : `<div class="ax-empty">This group already has everything on the instance.</div>`;
    } else {
      els.list.innerHTML = families.map((f) => {
        const rows = cands.get(f.key) || [];
        if (!rows.length) return "";
        /* The Library's own group toggle, so a section folds here exactly as
           it folds there — caret driven off `aria-expanded`, the component
           owning layout and this page owning only the vocabulary inside it. */
        const shut = pickerState.collapsed.has(f.key);
        return `
        <div class="ax-picker__fam${shut ? " is-shut" : ""}">
          <button type="button" class="fbar-grouptoggle" data-pk-fam="${esc(f.key)}"
                  aria-expanded="${shut ? "false" : "true"}">
            <svg class="fbar-group__caret" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m6 9 6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
            <span class="fbar-group__title">${esc(f.display_name)}</span>
            <span class="fbar-group__n">${rows.length}</span>
          </button>
          ${rows.map(({ t, i, block }) => {
            const key = `${t.type_key}:${i.resource_id}`;
            const on = pickerState.chosen.has(key);
            return `
            <button type="button" class="ax-pk-r ${on ? "is-on" : ""}" data-pk-item="${esc(key)}" aria-pressed="${on}">
              <span class="ax-pk-r__box" aria-hidden="true">${on ? "✓" : ""}</span>
              ${kindTag(t)}
              <span class="ax-pk-r__nm">${esc(itemName(i))}${block ? `<span class="ax-r__blk"> · ${esc(block)}</span>` : ""}
                ${i.description ? `<span class="ax-r__d">${esc(String(i.description).slice(0, 100))}</span>` : ""}
              </span>
            </button>`;
          }).join("")}
        </div>`;
      }).join("");
    }
    /* The filter offers the kinds that are actually in front of you, with
       their counts — a filter listing kinds this group could never be
       offered is a filter that lies about the set. */
    /* Counted over everything the group could be offered, not over what the
       current filter left behind — otherwise every chip reads "0" except the
       one that is active, and the filter stops describing the set it filters. */
    const kinds = new Map();
    const wasKind = pickerState.kind;
    pickerState.kind = "";
    for (const list of pickerCandidates().values()) {
      for (const { t } of list) kinds.set(t.type_key, (kinds.get(t.type_key) || 0) + 1);
    }
    pickerState.kind = wasKind;
    const kindsEl = els.root.querySelector('[data-pk="kinds"]');
    const chipsEl = els.root.querySelector('[data-pk="chips"]');
    const nEl = els.root.querySelector('[data-pk="filtern"]');
    const btnEl = els.root.querySelector('[data-pk="filterbtn"]');
    if (kindsEl && chipsEl) {
      const rank = new Map((overview.resources || []).map((t, i) => [t.type_key, i]));
      const label = new Map((overview.resources || []).map((t) => [t.type_key, t.type_display]));
      kindsEl.innerHTML = [...kinds.keys()]
        .sort((a, b) => (rank.get(a) ?? 99) - (rank.get(b) ?? 99))
        .map((k) => `
          <label class="fbar-menu__opt">
            <input type="checkbox" data-pk-kind="${esc(k)}"${pickerState.kind === k ? " checked" : ""}>
            <span class="fbar-menu__opt-text">${esc(label.get(k) || k)}</span>
            <span class="fbar-menu__opt-n">${kinds.get(k)}</span>
          </label>`).join("");
      if (nEl) { nEl.textContent = pickerState.kind ? "1" : "0"; nEl.hidden = !pickerState.kind; }
      if (btnEl) btnEl.classList.toggle("is-on", !!pickerState.kind);
      chipsEl.innerHTML = pickerState.kind ? `
        <span class="fbar-chip">
          <span class="fbar-chip__edit">
            <span class="fbar-chip__label">Kind:</span>
            <span class="fbar-chip__val">${esc(label.get(pickerState.kind) || pickerState.kind)}</span>
          </span>
          <button type="button" class="fbar-chip__x" data-pk-kind="" aria-label="Remove kind filter">×</button>
        </span>
        <button type="button" class="fbar-chips__clear" data-pk-kind="">Clear all</button>` : "";
      chipsEl.hidden = !pickerState.kind;
      // The button is the control; it is never hidden, so the way back to
      // "no filter" cannot disappear with the last unfiltered kind.
      const wrap = btnEl && btnEl.closest(".fbar-filter");
      if (wrap) wrap.hidden = !pickerState.kind && kinds.size < 2;
    }

    const n = pickerState.chosen.size;
    els.count.textContent = n ? `${n} selected` : "Nothing selected";
    paintPickerTier();
    els.apply.disabled = !n;
    els.apply.textContent = n ? `Add ${n} to the group` : "Apply";
  }

  /* Picking groups reuses the row shape, minus the kind chip: what varies
     here is the audience, and a group's own line already says how many
     people that is — the number that decides whether this share is small or
     not. */
  function paintGroupPicker(els) {
    const rows = pickerGroupCandidates();
    if (!rows.length) {
      els.list.innerHTML = pickerState.q
        ? `<div class="ax-empty">No group matches “${esc(pickerState.q)}”.</div>`
        : `<div class="ax-empty">Every group already has this.</div>`;
    } else {
      els.list.innerHTML = rows.map((g) => {
        const key = g.id;
        const on = pickerState.chosen.has(key);
        const n = g.member_count ?? 0;
        /* The scope carries no count. A roster number here would be the same
           false claim the three renderers on the page were fixed to stop
           making — worse in the picker, because it is read at the moment of
           DECIDING, so a stale-looking "41 people" invites "that is fewer
           than I meant" and the choice gets abandoned. */
        const detail = g.is_scope
          ? esc(g.description)
          : `${n} ${n === 1 ? "person" : "people"}${g.description ? ` · ${esc(g.description)}` : ""}`;
        return `
        <button type="button" class="ax-pk-r ax-pk-r--g ${g.is_scope ? "ax-pk-r--scope " : ""}${on ? "is-on" : ""}" data-pk-item="${esc(key)}" aria-pressed="${on}">
          <span class="ax-pk-r__box" aria-hidden="true">${on ? "✓" : ""}</span>
          <span class="ax-pk-r__nm">${AgnesKindGlyph.groupGlyph()} ${esc(titleOf(g))}
            <span class="ax-r__d">${detail}</span>
          </span>
        </button>`;
      }).join("");
    }
    const n = pickerState.chosen.size;
    const chosen = [...pickerState.chosen];
    // The count says PEOPLE, not groups. "3 groups" hides whether this is a
    // share with four people or four hundred, which is the thing worth
    // knowing before pressing Apply.
    const line = (reach) => `${n} ${n === 1 ? "group" : "groups"} · ${reach} ${reach === 1 ? "person" : "people"}`;
    /* Painted twice on purpose. The local estimate lands instantly so the
       footer never blanks while a request is in flight; the server's answer
       replaces it when it arrives, and only if the selection is still the
       one it was asked about. The estimate can be wrong — it double-counts a
       person in two groups whose rosters the payload did not carry — which
       is why it does not get the last word at the moment of deciding.
       (Audit E3.) */
    els.count.textContent = n ? line(reachOf(chosen)) : "No group selected";
    if (n) {
      const asked = chosen.slice().sort().join(",");
      fetchReach(chosen).then((count) => {
        if (count == null) return;                       // server unreachable: the estimate stands
        if ([...pickerState.chosen].sort().join(",") !== asked) return;   // selection moved on
        els.count.textContent = line(count);
      });
    }
    paintPickerTier();
    els.apply.disabled = !n;
    els.apply.textContent = n ? `Share with ${n} ${n === 1 ? "group" : "groups"}` : "Apply";
  }

  function openBundlePicker(typeKey, resourceId, label) {
    const els = buildPicker();
    pickerState = { mode: "bundle", group: null, bundle: { type: typeKey, id: resourceId }, tier: "available",
                    chosen: new Set(), q: "", kind: "", collapsed: new Set() };
    els.q.value = "";
    els.q.placeholder = "Search groups…";
    els.root.querySelector("#pk-title").textContent = `Share ${label}`;
    /* The tier is asked in the footer now, so the subtitle stops announcing
       it. It used to say the answer ("Added as Available") — which was both
       a decision made on the admin's behalf and, on kinds with no tier at
       all, a promise of a choice the row would not have. */
    els.sub.textContent = "Groups that do not have it yet.";
    paintPicker();
    els.root.hidden = false;
    els.root.classList.add("is-open");
    els.q.focus();
  }

  /* Show the tier question only when the selection contains something the
     tier can act on, and reflect the current answer.

     In bundle mode the SUBJECT is one resource and the choices are
     audiences, so whether the question applies is a property of that
     resource. In resources mode the subject is a group and the choices are
     resources of every kind, so it applies as soon as one tiered kind is
     ticked — and stops applying if it is unticked again. */
  function paintPickerTier() {
    const els = pickerEls;
    if (!els || !els.tier) return;
    const chosen = [...pickerState.chosen];
    const applies = pickerState.mode === "bundle"
      ? (chosen.length > 0 && TIERED.has(pickerState.bundle.type))
      : chosen.some((k) => TIERED.has(k.slice(0, k.indexOf(":"))));
    els.tier.hidden = !applies;
    for (const b of els.tierseg.querySelectorAll("[data-pk-tier]")) {
      b.classList.toggle("is-active", b.dataset.pkTier === (pickerState.tier || "available"));
    }
  }

  function openPicker(groupId) {
    const els = buildPicker();
    const g = (overview.groups || []).find((x) => x.id === groupId);
    // Every field, every time: the two views reset this wholesale, and a
    // literal that forgets `kind` or `collapsed` leaves the painter reading
    // `.has` off undefined — which is a blank drawer, not an error anyone
    // sees.
    pickerState = { mode: "resources", group: groupId, bundle: null, tier: "available",
                    chosen: new Set(), q: "", kind: "", collapsed: new Set() };
    els.q.value = "";
    els.q.placeholder = "Search everything grantable…";
    els.root.querySelector("#pk-title").textContent = "Add to this group";
    // This picker offers every kind at once — tiered and untiered — so it
    // cannot promise the tier either. The row that lands says which it got.
    els.sub.textContent = g
      ? `Everything ${titleOf(g)} does not have yet.`
      : "";
    paintPicker();
    // `.ds-drawer` is display:none until `.is-open` — the same two-step the
    // group drawer uses (`group_drawer.js`), because the overlay animates in
    // and `hidden` alone would skip the transition. Setting one without the
    // other opens a drawer nobody can see.
    els.root.hidden = false;
    els.root.classList.add("is-open");
    els.q.focus();
  }

  function closePicker() {
    if (!pickerEls) return;
    pickerEls.root.classList.remove("is-open");
    pickerEls.root.hidden = true;
  }

  /* Apply writes every choice, then says what happened — including what did
     NOT happen. A partial failure that reports success is worse than the
     failure, because the admin walks away believing a grant exists. */
  async function applyPicker() {
    const els = buildPicker();
    const chosen = [...pickerState.chosen];
    const bundleMode = pickerState.mode === "bundle";
    els.apply.disabled = true;
    let ok = 0;
    const failed = [];
    for (const key of chosen) {
      const type = bundleMode ? pickerState.bundle.type : key.slice(0, key.indexOf(":"));
      const rid = bundleMode ? pickerState.bundle.id : key.slice(key.indexOf(":") + 1);
      // In bundle mode the key IS the audience, and one of them is not a
      // group: the sentinel routes to `scope` instead of `group_id`.
      const asScope = bundleMode && key === EVERYONE_AUDIENCE.id;
      const gid = bundleMode ? (asScope ? everyoneGroupId() : key) : pickerState.group;
      try {
        /* The admin's answer, for a kind that has the choice. An untiered
           kind is written at the column default either way — passing the
           answer there would record a tier the kind does not have. */
        /* The footer's answer, for a kind that has the choice — with the one
           per-row exception the API enforces: Automatic on a skill is
           admissible only when the organization is the publisher, and the
           server refuses anything else with a 422. A user-published skill is
           written Optional whatever the footer says, the same rule its row's
           control applies, rather than letting the batch fail on click. */
        const userSkill = type === "store_entity" && ((itemOf(type, rid) || {}).publisher_kind || "user") !== "organization";
        const tier = TIERED.has(type) && !userSkill ? (pickerState.tier || "available") : "available";
        await writeGrant(type, rid, tier, gid, asScope ? "everyone" : undefined);
        ok++;
      } catch (err) {
        failed.push(bundleMode
          ? (asScope ? EVERYONE_AUDIENCE.name
                     : (titleOf((overview.groups || []).find((g) => g.id === key) || {}) || key))
          : rid);
      }
    }
    closePicker();
    toast(failed.length
      ? `${ok} ${bundleMode ? "shared" : "added"}, ${failed.length} failed — ${esc(String(failed[0]))}${failed.length > 1 ? " and others" : ""}`
      : bundleMode
        ? `Shared with ${ok} ${ok === 1 ? "group" : "groups"}`
        // "available to everyone in the group" read as the Available TIER —
        // the word is a label on this very page — on a batch that may hold
        // kinds with no tier at all. Say the reach, which is what actually
        // just happened, and leave the tier to the rows.
        : `${ok} added — everyone in the group can use ${ok === 1 ? "it" : "them"} now`, !failed.length);
    await repaint();
  }

  /* ── Simulate ── */

  // Rebuilds the paired `.ds-dropdown`'s button AND menu from ax-sim-user's
  // own freshly-set <option>s, then re-runs ds_dropdown.js's init via the
  // exported `dsDropdownInit` hook — needed because loadUsers() rebuilds the
  // whole option list once from a fetch. The button is rebuilt too, not just
  // the menu: init() isn't idempotent for its per-element listeners, so
  // leaving the same persisting <button> node across two init() calls (one
  // from ds_dropdown.js's own DOMContentLoaded bootstrap, one from here)
  // double-registers its click handler — the second open()/close() undoes
  // the first, and the menu never appears to open. Recreating the button
  // discards that stale listener along with the old node.
  function syncSimUserDropdown(sel) {
    const dd = document.querySelector('.ds-dropdown[data-ds-dropdown-target="ax-sim-user"]');
    if (!dd) return;
    const opts = Array.from(sel.options);
    const currentOpt = opts.find((o) => o.value === sel.value) || opts[0];
    const menuEl = dd.querySelector(".ds-dropdown-menu");
    const ariaLabel = menuEl ? menuEl.getAttribute("aria-label") || "" : "";
    const nameHtml = ariaLabel ? `<span class="ds-dropdown-name" id="ax-sim-user-dd-name">${esc(ariaLabel)}</span>` : "";
    const labelledBy = ariaLabel ? ` aria-labelledby="ax-sim-user-dd-name ax-sim-user-dd-btn-label"` : "";
    const items = opts
      .map((o) => `<li class="ds-dropdown-menu-item${o === currentOpt ? " is-selected" : ""}" role="menuitemradio" aria-checked="${o === currentOpt}" tabindex="0" data-value="${esc(o.value)}">${esc(o.textContent)}</li>`)
      .join("");
    dd.innerHTML = `<button type="button" class="ds-dropdown-btn" id="ax-sim-user-dd-btn" aria-haspopup="menu" aria-expanded="false" aria-controls="ax-sim-user-dd-menu"${labelledBy}>${nameHtml}<span class="ds-dropdown-btn-label" id="ax-sim-user-dd-btn-label">${currentOpt ? esc(currentOpt.textContent) : ""}</span><svg class="ds-dropdown-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg></button><ul class="ds-dropdown-menu" id="ax-sim-user-dd-menu" role="menu" aria-label="${esc(ariaLabel || "Options")}" hidden>${items}</ul>`;
    if (window.dsDropdownInit) window.dsDropdownInit(dd);
  }

  // Lighter counterpart for when only the SELECTED value changes and the
  // option list itself is untouched (a real click already updated the
  // dropdown's own visuals via ds_dropdown.js; the `?user=` deep link below
  // sets `.value` + dispatches "change" straight from code, which needs this
  // to catch the paper-theme dropdown up). Mutates the EXISTING items in
  // place rather than rebuilding — no button churn on every plain click.
  function syncSimUserSelection(sel) {
    const dd = document.querySelector('.ds-dropdown[data-ds-dropdown-target="ax-sim-user"]');
    if (!dd) return;
    const label = dd.querySelector(".ds-dropdown-btn-label");
    const opt = sel.options[sel.selectedIndex];
    dd.querySelectorAll('.ds-dropdown-menu [role="menuitemradio"]').forEach((item) => {
      const isSelected = item.dataset.value === sel.value;
      item.classList.toggle("is-selected", isSelected);
      item.setAttribute("aria-checked", isSelected ? "true" : "false");
    });
    if (label && opt) label.textContent = opt.textContent.trim();
  }

  async function loadUsers() {
    if (users.length) return;
    try {
      const r = await fetch(`${USERS_LIST_API}?limit=500`, { credentials: "include" });
      users = r.ok ? await r.json() : [];
      if (!Array.isArray(users)) users = users.users || [];
    } catch (e) { users = []; }
    const sel = el("ax-sim-user");
    sel.innerHTML = `<option value="">Pick a person…</option>` + users
      .filter((u) => u.active !== false)
      .map((u) => `<option value="${esc(u.id)}">${esc(u.name || u.email)} — ${esc(u.email)}</option>`)
      .join("");
    syncSimUserDropdown(sel);
  }

  /* The tab lists its subject, like the other two.
     By group lists groups and By resource lists resources; By person listed
     nothing and demanded a name you had to already know. A search alone is
     only usable by someone who can spell the person they are looking for —
     and an admin auditing access very often cannot, because the question is
     "who are these people" in the first place.

     So: no selection renders the roster, the toolbar's search narrows it the
     same way it narrows the other two tabs (one behaviour on all three), and
     picking a row shows that person's chain with a way back. */
  function renderPeopleList() {
    const out = el("ax-sim-out");
    if (!out) return;
    const q = (groupFilter || "").trim().toLowerCase();
    const live = users.filter((u) => u.active !== false);
    const shown = q
      ? live.filter((u) => `${u.name || ""} ${u.email || ""}`.toLowerCase().includes(q))
      : live;
    el("ax-sim-detail").hidden = true;
    if (!live.length) {
      out.innerHTML = `<div class="ax-empty">No accounts yet.</div>`;
      return;
    }
    if (!shown.length) {
      out.innerHTML = `<div class="ax-empty">No one matches “${esc(groupFilter)}”.</div>`;
      return;
    }
    out.innerHTML = `<div class="ax-people">` + shown.map((u) => {
      const label = u.name || u.email;
      return `<button type="button" class="ax-person" data-person="${esc(u.id)}">
        <span class="ax-person__nm">${esc(label)}</span>
        <span class="ax-person__em">${esc(u.email || "")}</span>
      </button>`;
    }).join("") + `</div>`;
  }

  /* Picking one is what the hidden <select> is for — routing through it keeps
     the URL, the dropdown sync and the chain render on the one path they
     already used. */
  document.addEventListener("click", (e) => {
    const row = e.target.closest("[data-person]");
    if (!row) return;
    const sel = el("ax-sim-user");
    if (!sel) return;
    sel.value = row.dataset.person;
    sel.dispatchEvent(new Event("change", { bubbles: true }));
  });

  document.addEventListener("click", (e) => {
    if (!e.target.closest("[data-people-back]")) return;
    const sel = el("ax-sim-user");
    if (!sel) return;
    sel.value = "";
    sel.dispatchEvent(new Event("change", { bubbles: true }));
  });

  el("ax-sim-user").addEventListener("change", async (e) => {
    syncSimUserSelection(e.target);
    const uid = e.target.value;
    // The picked person lives in the URL (?user=) so the preview survives a
    // round trip — "Share it →" leaves for the package page and its back
    // link returns HERE, to the same person, already resolved. replaceState:
    // re-picking is a refinement of the same view, not a history entry.
    const url = new URL(window.location.href);
    url.searchParams.set("lens", "simulate");
    if (uid) url.searchParams.set("user", uid);
    else url.searchParams.delete("user");
    history.replaceState(null, "", url);
    const out = el("ax-sim-out");
    const detailBtn = el("ax-sim-detail");
    const backEl = el("ax-sim-back");
    if (backEl) backEl.hidden = !uid;
    // Read-only view-as form: shown only once a person is picked, and the id
    // is set on the hidden field rather than in the action URL so the POST
    // body carries it alongside the CSRF token.
    const viewAsForm = el("ax-sim-viewas");
    const viewAsUid = el("ax-sim-viewas-uid");
    const viewAsReturn = el("ax-sim-viewas-return");
    const viewAsSelf = el("ax-sim-self");
    if (viewAsSelf) viewAsSelf.hidden = true;
    if (!uid) {
      // With nobody picked the pane IS the roster, so there is no longer a
      // "pick a person" placeholder to paint over it. `renderPeopleList`
      // hides the detail button itself, which is why that is not repeated.
      if (viewAsForm) viewAsForm.hidden = true;
      await loadUsers();
      renderPeopleList();
      return;
    }
    detailBtn.hidden = false;
    detailBtn.href = `/admin/users/${encodeURIComponent(uid)}`;
    // The caller's own row offers no view-as: the entry route refuses it
    // (`view_as_self`), so offering it can only ever produce an error page.
    const isSelf = !!VIEWER_USER_ID && uid === VIEWER_USER_ID;
    if (isSelf) {
      if (viewAsForm) viewAsForm.hidden = true;
      if (viewAsSelf) viewAsSelf.hidden = false;
    } else if (viewAsForm && viewAsUid) {
      viewAsUid.value = uid;
      // Where exiting comes back to: THIS lens with THIS person, read off the
      // URL `history.replaceState` just wrote (path + search only — the server
      // rejects anything that is not a same-origin absolute path). Set here
      // rather than server-side in the template because the person changes
      // without a page load, and a stale origin is worse than none.
      if (viewAsReturn) viewAsReturn.value = url.pathname + url.search;
      viewAsForm.hidden = false;
    }
    out.innerHTML = `<div class="ax-empty">Resolving…</div>`;
    try {
      const [effResp, memResp, prevResp] = await Promise.all([
        fetch(`${ADMIN_USERS_API}/${encodeURIComponent(uid)}/effective-access`, { credentials: "include" }),
        fetch(`${ADMIN_USERS_API}/${encodeURIComponent(uid)}/memberships`, { credentials: "include" }),
        // The RESULT half: what their Library actually shows (the chain
        // below is the why). Failure degrades to no panel, never to a
        // broken simulation.
        fetch(`${ADMIN_USERS_API}/${encodeURIComponent(uid)}/library-preview`, { credentials: "include" }),
      ]);
      const eff = effResp.ok ? await effResp.json() : { items: [], is_admin: false };
      const mem = memResp.ok ? await memResp.json() : [];
      const preview = prevResp.ok ? await prevResp.json() : null;
      renderSimulation(uid, eff, Array.isArray(mem) ? mem : (mem.groups || []), preview);
    } catch (err) {
      out.innerHTML = `<div class="ax-empty">Could not resolve this person's access (${esc(err.message)}).</div>`;
    }
  });

  // Every grantable item on the instance, indexed `type:id → {name, type}`.
  // Without this the chain printed raw ids (`col_1e7d714a1a3ed6d7`), which is
  // the one thing a "why can they see this?" answer must not do.
  function resourceIndex() {
    const idx = new Map();
    for (const t of (overview && overview.resources) || []) {
      for (const b of t.blocks || []) {
        for (const i of b.items || []) {
          idx.set(`${t.type_key}:${i.resource_id}`, {
            name: itemName(i),
            type: t.type_display,
          });
        }
      }
    }
    return idx;
  }

  // The Library-shaped preview panel — the RESULT the chain explains. Rows
  // come from the same StackResolver.browse projection the /library page
  // renders for the person, so this cannot drift from what they'd see; the
  // state chip speaks the product's one vocabulary — the words the person
  // reads in their own Library, not the admin's: "Required by your admin"
  // (a required-tier grant), "In their Library" (added/auto), "Available,
  // no local copy yet" (classic mode, not subscribed — granted but not
  // delivered). This lens exists to show what THEY see; saying it in words
  // they never see would defeat the whole point of it.
  function renderLibraryPreview(preview, uid, isAdmin, ctx) {
    if (!preview || !Array.isArray(preview.sections)) return "";
    /* The joined half: which group carries each grant, what is not shared at
       all, and which grants point at nothing. Normalized rather than
       required, so a caller that has not resolved it yet degrades to the
       granted list alone instead of throwing. */
    ctx = ctx || {};
    const viaNames = ctx.viaNames || new Map();
    const missing = ctx.missing || [];
    const dangling = ctx.dangling || [];
    /* Where a row in this preview should take you.

       It used to be the item's ANALYST url with `?from=admin&user=` glued on
       — `/catalog/p/board?from=admin&user=<id>`. Those pages ignore the
       param, so clicking a row in "what MALCOLM's Library shows" opened YOUR
       Library view of the package ("You have access", "Add to stack"), and
       its back link dropped the person entirely. The memory route had no way
       back at all: a one-way exit out of an audit.

       The admin page for the resource is the right destination, and it
       already knows this trip — `?from=simulate&user=` is exactly what the
       "Share it →" flow passes, which is why that one lands with "← Back to
       preview: <name>" and returns you with "Re-check <name> →". Same
       contract, so the same round trip. Falls back to the analyst href when
       a kind has no admin page rather than losing the link. */
    const previewHref = (kind, it) => {
      const adminPage = ENTITY_PAGE_ADMIN[kind];
      const base = adminPage ? adminPage(it.id) : it.href;
      if (!base) return "";
      const sep = base.includes("?") ? "&" : "?";
      return `${base}${sep}from=simulate&user=${encodeURIComponent(uid)}`;
    };

    /* Which group carries the grant, joined from the effective-access read
       this pane already holds. Absent for a kind the grant list does not
       enumerate, in which case the row simply says nothing rather than
       guessing a route. */
    const viaText = (kind, id) => {
      const names = viaNames.get(`${kind}:${id}`) || [];
      if (!names.length) return "";
      const shown = names.length > 2 ? `${names.slice(0, 2).join(", ")} +${names.length - 2}` : names.join(", ");
      return `<span class="ax-preview__via" title="${esc(names.join(", "))}">via ${esc(shown)}</span>`;
    };
    const row = (name, via, state) =>
      `<li class="ax-preview__row"><span class="ax-preview__name">${name}</span>${via}${state}</li>`;
    const band = (label, count, rows, extra = "", cls = "") =>
      `<div class="ax-preview__band${cls}">
        <div class="ax-preview__label">${esc(label)} <span class="ax-preview__n">${count}</span></div>
        <ul class="ax-preview__rows">${rows}</ul>${extra}</div>`;

    const grantedBands = preview.sections.map((sec) => {
      const rows = (sec.items || []).map((it) => {
        const state = it.requirement === "required"
          ? `<span class="ax-chip ok" title="${esc(WORDS.tier_automatic_help)}">Automatic — set by you</span>`
          : it.in_stack
            ? `<span class="ax-chip ok" title="${it.materialized ? "In their Library, and downloaded on their next pull" : "In their Library"}">In their Library</span>`
            : `<span class="ax-chip info" title="${esc(WORDS.tier_optional_help)}">Optional — no local copy yet</span>`;
        const href = previewHref(sec.kind, it);
        const name = href ? `<a href="${esc(href)}">${esc(it.name)}</a>` : esc(it.name);
        return row(name, viaText(sec.kind, it.id), state);
      }).join("");
      /* "6 of 8" only where the remainder is ON SCREEN — the gap band below
         is where those 2 are listed, so the two numbers are the same two
         arrays and cannot disagree. The old chips claimed a count of their
         own from a third source and capped it at six, which is how the
         screen came to show 6 above a list of 8. */
      const items = sec.items || [];
      const total = sec.kind === "data_package" && missing.length
        ? `${items.length} of ${items.length + missing.length}`
        : `${items.length}`;
      return band(sec.label, total, rows);
    }).join("");

    // "nothing granted yet" is true of the GRANTS and false of the reach,
    // and for an admin the difference is the whole answer.
    const granted = preview.sections.length
      ? grantedBands
      : `<div class="ax-empty">${isAdmin
          ? "The shared bands of their Library are empty — nothing is granted to their groups. They still reach everything as an admin."
          : "The shared bands of their Library are empty — nothing granted yet."}</div>`;

    /* ── The two bands the chips used to be ──────────────────────────────
       Not a decorative move. These are the only two facts on this pane that
       the granted list cannot contain — what is NOT shared, and what is
       granted but points at nothing — and as chips above the panel they were
       outnumbered six to one by green badges restating the list directly
       beneath them. Same list, same vocabulary, same row shape: an admin
       reads down one column and the exceptions are simply where they fall. */
    const GAP_MAX = 12;

    let gaps = "";
    if (missing.length) {
      const shown = missing.slice(0, GAP_MAX);
      const rows = shown.map((p) => {
        const href = `/admin/data-packages/${encodeURIComponent(p.resource_id)}?from=simulate&user=${encodeURIComponent(uid)}`;
        // For an admin this is a fact about the PACKAGE, not a limit on the
        // person — they reach it regardless, and saying "cannot use" to one
        // was the loudest wrong answer on a page about who reaches what.
        const state = isAdmin
          ? `<span class="ax-chip info" title="Granted to no group at all. This person reaches it as an admin; nobody else does.">Granted to nobody</span>`
          : `<span class="ax-chip no" title="No group this person is in has been granted this package.">No group of theirs</span>`;
        return row(`<a href="${esc(href)}">${esc(p.name)}</a>`, "", state);
      }).join("");
      // Never a silent cut: the count in the label is the true one and the
      // overflow says what it is not showing.
      const more = missing.length > GAP_MAX
        ? `<div class="ax-preview__more">+${missing.length - GAP_MAX} more not shared with them</div>`
        : "";
      gaps += band("Not shared with them", missing.length, rows, more, " ax-preview__band--gap");
    }

    if (dangling.length) {
      const rows = dangling.map((p) => {
        const g0 = (p.via_groups || [])[0];
        const gid = g0 && g0.group_id;
        const href = gid ? `/admin/access?group=${encodeURIComponent(gid)}` : "/admin/access";
        // No name to print: nothing this page can list answers to the id,
        // which is the finding. Deliberately "deleted OR unlisted" and not
        // "deleted" — the test is membership in the admin projection, and a
        // package that is soft-deleted or private to its owner fails it while
        // still existing. A local preview surfaced one of each, which is how
        // the stronger wording got caught.
        //
        // The id is set as code so it reads as an identifier rather than as a
        // package someone named `pkg_356b2e8f37e5` — the retired chip ran it
        // together with prose, which is exactly what the chain refuses to do.
        return row(
          `<a href="${esc(href)}"><code>${esc(p.resource_id)}</code></a>`,
          viaText("data_package", p.resource_id) || (g0
            ? `<span class="ax-preview__via">via ${esc(g0.group_name || g0.group_id)}</span>`
            : ""),
          `<span class="ax-chip warn" title="A grant still points at this id, but nothing this page can list answers to it — the package was deleted, or it is private to its owner. Open the group to remove the grant.">Deleted or unlisted</span>`,
        );
      }).join("");
      gaps += band("Dangling grants", dangling.length, rows, "", " ax-preview__band--gap");
    }

    return `<div class="ax-preview">
      <div class="ax-preview__head">What their Library shows</div>${granted}${gaps}</div>`;
  }

  function renderSimulation(uid, eff, memberships, preview) {
    const person = users.find((u) => u.id === uid) || {};
    const items = eff.items || [];
    const idx = resourceIndex();

    const packages = items.filter((i) => i.resource_type === "data_package");
    const others = items.filter((i) => i.resource_type !== "data_package");

    // A grant row whose target the projection no longer lists is a DANGLING
    // grant (package deleted, grant left behind) — the one thing this lens
    // must not paint green. It gets its own band in the panel below.
    const dangling = packages.filter((p) => !idx.has(`data_package:${p.resource_id}`));

    /* No tier words are computed here any more, and that is the shape of
       this pane rather than a deletion.

       A package's tier used to be written three times on one screen: as a
       prose suffix on a chip up here, as a state chip in the Library panel
       ("In their Library"), and again in the why-chain below it. The retired
       suffix is deliberately not quoted here — the guard that keeps those
       words off this page matches any occurrence, comments included, which
       is the property that makes it worth having. Two of the three were prose, which is how the sweep in
       `tests/test_access_vocabulary.py` — written after one Required package
       was named two different ways within two inches of itself — could pass
       while the drift was still on the page. The panel's own chips are the
       only place a tier is worded now, so there is one definition instead of
       three kept in step by a guard. The panel resolves it from
       `library-preview`, the same projection `/library` renders that person's
       bands from, which is a better source than this page's grant rows: it
       already accounts for membership mode and it cannot drift from the page
       it predicts.

       The chips also capped themselves at six while the panel listed eight,
       so the screen carried two disagreeing counts a centimetre apart. The
       panel counts what it renders. */

    // What is NOT shared with them — the half a grant list cannot show, and
    // the reason someone opens this page at all.
    const allPackages = ((overview && overview.resources) || [])
      .filter((t) => t.type_key === "data_package")
      .flatMap((t) => (t.blocks || []).flatMap((b) => b.items || []));
    const has = new Set(packages.map((p) => p.resource_id));
    const missing = allPackages.filter((p) => !has.has(p.resource_id));

    const groupNames = memberships.map((g) => g.name || g.group_name || g.id).filter(Boolean);

    /* Which groups carry each grant, keyed the way the Library panel keys its
       rows — so the panel can print the route in the row it explains instead
       of the chain repeating every package underneath it. */
    const viaNames = new Map();
    for (const it of items) {
      const key = `${it.resource_type}:${it.resource_id}`;
      const names = (it.via_groups || []).map((g) => g.group_name || g.group_id).filter(Boolean);
      if (names.length) viaNames.set(key, [...new Set([...(viaNames.get(key) || []), ...names])]);
    }

    /* Three questions, three sections — not one list of eight bullets.
       The chain used to mix a membership statement, the things they can
       reach, the things they cannot, and a standing note about `agnes pull`
       into a single <ul> with one dot per line. Every line looked equally
       important and the reader had to sort them by reading each one. Split,
       the shape answers the questions in the order they are asked: who are
       they, what can they reach, what can they not. */
    const chain = [];       // "can use" — the bulk
    const chainStop = [];   // "cannot use" — negatives and dangling grants
    let memberLine = "";
    /* Every bolded name below is a GROUP, set in the same type as the package
       names beside it. The inline mark is what tells the two apart in prose,
       where there is no column to say which is which. */
    const gname = (n) => `<b>${AgnesKindGlyph.groupGlyph()} ${esc(n)}</b>`;
    memberLine = (`<li><b>${esc(person.name || person.email || "This person")}</b> is in ${
      groupNames.length ? groupNames.map(gname).join(", ") : "<span class=\"muted\">no groups</span>"
    }</li>`);

    /* The one thing the panel above genuinely cannot show, because it is not
       a grant: an admin short-circuits every authorization check, so the
       list is what they are GRANTED and not what they can reach. It used to
       be a grey chip leading a row of badges; here it sits in the sentence
       about who they are, which is where a reader is already asking. */
    if (eff.is_admin) {
      chain.push(`<li>They are an <b>admin</b> — they reach everything regardless of the grants below.</li>`);
    }

    if (!items.length) {
      // A real link to the editor, carrying the FIRST group this person is
      // actually in — "grant something" is meaningless until a group is
      // selected, and theirs is the one the reader just read about. Falls
      // back to the bare editor when they are in no group at all, which is
      // its own answer.
      const firstGroup = (person.groups || [])[0];
      const href = firstGroup && firstGroup.id
        ? `/admin/access?group=${encodeURIComponent(firstGroup.id)}`
        : "/admin/access";
      chainStop.push(`<li class="stop">Nothing is granted to those groups, so they see no shared data.
        <a href="${href}">Grant something →</a></li>`);
    }

    /* The per-package rows that stood here are gone, and so are the dangling
       and not-shared stops below them: all three are rows in the Library
       panel above now, each carrying the group it travels through. Three
       renderings of one package set — chip, panel row, chain entry — is what
       this pane actually had, and the chain's copy was the one that could
       never show the whole set (`missing` was cut to three, silently).

       What is left here is what a list of packages cannot say: who they are,
       which groups they are in, the kinds the panel does not enumerate, and
       what still has to happen for a grant to reach their machine. */

    // Everything else FOLDED PER TYPE. Listing eight collection rows
    // individually buried the packages under them and answered a question
    // nobody asked; the count plus the groups is what a reader needs, and
    // the per-item detail lives on the user's own profile page.
    //
    // Bucketed on `resource_type`, NOT on each item's looked-up display name:
    // a grant whose target is not in the projection (deleted, or private to
    // its owner and so absent from the admin grant list) has no name to look
    // up, and keying on the fallback split one kind across two buckets —
    // "Collections — 7 items" beside a lowercase "collection — 2 items",
    // which reads as two different things. Those are counted inside their own
    // kind and named as unlisted, because a grant pointing at something this
    // page cannot show is worth saying out loud rather than hiding.
    const typeDisplay = new Map(
      ((overview && overview.resources) || []).map((t) => [t.type_key, t.type_display]),
    );
    const byType = new Map();
    for (const it of others) {
      const key = it.resource_type;
      if (!byType.has(key)) byType.set(key, { n: 0, unlisted: 0, via: new Set() });
      const bucket = byType.get(key);
      bucket.n += 1;
      if (!idx.has(`${key}:${it.resource_id}`)) bucket.unlisted += 1;
      for (const g of it.via_groups || []) bucket.via.add(g.group_name || g.group_id);
    }
    /* A kind the panel renders as its own band (plugins, recipes, memory)
       does NOT get a fold line: that would restate a list sitting directly
       above it, which is the duplication this pane was carrying in three
       places at once. Its `unlisted` count still surfaces, because a grant
       pointing at something no surface can show is the one part of that kind
       the band genuinely cannot contain. */
    const bandKinds = new Set(((preview && preview.sections) || []).map((sec) => sec.kind));
    for (const [key, b] of byType) {
      const label = typeDisplay.get(key) || key.replace(/_/g, " ");
      const unlisted = b.unlisted
        ? ` <span class="muted">(${b.unlisted} not listed here — deleted, or private to its owner)</span>`
        : "";
      if (bandKinds.has(key)) {
        if (b.unlisted) {
          chain.push(`<li class="stop"><b>${esc(label)}</b> <span class="muted">— ${b.unlisted} grant${
            b.unlisted === 1 ? "" : "s"} point at something not listed above</span> <span class="muted">(deleted, or private to its owner)</span></li>`);
        }
        continue;
      }
      chain.push(`<li><b>${esc(label)}</b> <span class="muted">— ${b.n} item${b.n === 1 ? "" : "s"}</span>
        via ${b.via.size ? [...b.via].map(gname).join(", ") : "<span class=\"muted\">a group</span>"}${unlisted}</li>`);
    }

    // "Delivery" here means SHIPPING, and it sat in a list where every other
    // bold word is the group named Delivery. Two testers misread it. It is a
    // standing fact about how granting works, true of every person on this
    // tab — so it closes the pane as a note rather than competing with the
    // findings as a ninth bullet with its own dot.
    const deliveryNote = `<p class="ax-simnote"><b>Getting it there:</b> a granted package reaches
      their machine on the next <code>agnes pull</code> — sharing it is not the same as it
      having arrived.</p>`;

    const section = (label, rows) => rows.length
      ? `<div class="ax-simsec"><div class="ax-simsec__hd">${label}</div>
         <ul class="ax-chain">${rows.join("")}</ul></div>`
      : "";

    el("ax-sim-out").innerHTML =
      `${renderLibraryPreview(preview, uid, !!eff.is_admin, { viaNames, missing, dangling })}
       <ul class="ax-chain">${chain.join("")}</ul>`;
  }

  // The "Grant something →" interceptor lived here. It reached for the
  // editor's in-page pane switch, which stopped existing when the two lenses
  // became section tabs — so the handler threw on a null and the link did
  // nothing at all. The link is an ordinary href now (built where the chain is
  // rendered, carrying the person's own group in the query), which needs no
  // handler and works on middle-click and copy-link-address too.

  /* The sticky header's stuck state. `position: sticky` gives no event and
     no selector for "is it currently pinned?", so a zero-height sentinel
     immediately above it stands in: the moment the sentinel leaves the top
     of the viewport, the header is stuck. */
  (function watchStuck() {
    const sentinel = el("ax-gh-sentinel");
    const head = el("ax-gh");
    if (!sentinel || !head || typeof IntersectionObserver !== "function") return;
    const top = parseInt(
      getComputedStyle(document.documentElement).getPropertyValue("--app-header-height"), 10) || 0;
    new IntersectionObserver(
      ([e]) => head.classList.toggle("is-stuck", !e.isIntersecting),
      { rootMargin: `-${top + 1}px 0px 0px 0px`, threshold: 0 },
    ).observe(sentinel);
  })();

  /* ── The URL is the page's state ──────────────────────────────────────
     An access audit's natural output is a link — "look at this". Three of
     the four things that decide what you are looking at used to be invisible
     to the URL: which group is open, what is typed in the search box, and
     which kind is filtered. So a reload dropped you back to nothing, a
     colleague got a link to the top of the page, and the one param that DID
     round-trip (`?user=`) was left stranded in the address bar after a tab
     switch, pointing at a person the group lens does not show.

     Everything writes through here, so the URL cannot drift from the view.

     push vs replace, deliberately: a discrete choice (a tab, a group, a kind
     filter) earns a history entry, because Back meaning "the thing I was
     looking at before" is the whole point. TYPING does not — one entry per
     keystroke would bury the page under its own search box — so the query
     rides replaceState and is picked up by the next push. */
  function currentState() {
    return {
      by: viewMode,
      group: viewMode === "group" ? (selectedGroup || "") : "",
      q: groupFilter.trim(),
      // One comma-joined value per facet. `?kind=agent` — the single-value
      // shape that is in shared links and bookmarks — reads back unchanged.
      kind: [...facets.get("kind")].join(","),
      reach: [...facets.get("reach")].join(","),
      tier: [...facets.get("tier")].join(","),
      origin: [...facets.get("origin")].join(","),
      user: viewMode === "person" ? ((el("ax-sim-user") || {}).value || "") : "",
    };
  }

  function syncUrl({ push = false } = {}) {
    const st = currentState();
    const u = new URL(window.location.href);
    const set = (k, v) => (v ? u.searchParams.set(k, v) : u.searchParams.delete(k));
    set("by", st.by === "group" ? "" : st.by);
    set("group", st.group);
    set("q", st.q);
    // Every facet, so a filtered view is a link someone can send.
    for (const k of FACET_KEYS) set(k, st[k]);
    set("user", st.user);
    // `?lens=simulate` is the older spelling of `?by=person`; keep writing it
    // only while that lens is on, so an old bookmark and a new link agree.
    if (st.by === "person") u.searchParams.set("lens", "simulate");
    else u.searchParams.delete("lens");
    const next = u.pathname + (u.search || "");
    const now = window.location.pathname + window.location.search;
    if (next === now) return;
    if (push) history.pushState(st, "", next);
    else history.replaceState(st, "", next);
  }

  /* Back / Forward. Without this the entries above would restore the URL and
     leave the page showing something else, which is worse than having no
     entries at all. */
  window.addEventListener("popstate", async () => {
    const params = new URLSearchParams(window.location.search);
    const by = params.get("lens") === "simulate" ? "person"
      : (_normalizeBy(params.get("by")) || "group");
    viewMode = by;
    groupFilter = params.get("q") || "";
    clearFacets();
    for (const k of FACET_KEYS) {
      for (const v of (params.get(k) || "").split(",")) if (v) facets.get(k).add(v);
    }
    selectedGroup = params.get("group") || selectedGroup;
    const find = el("ax-group-find");
    if (find) find.value = groupFilter;
    showPane(viewMode);
    paintTabState();
    if (viewMode === "person") {
      await loadUsers();
      const sel = el("ax-sim-user");
      const wanted = params.get("user") || "";
      if (sel && wanted && [...sel.options].some((o) => o.value === wanted)) {
        sel.value = wanted;
        sel.dispatchEvent(new Event("change"));
      } else {
        // No one named in the URL: the tab opens on its roster, the way By
        // group opens on groups. Without this it opened on an empty card.
        renderPeopleList();
      }
      return;
    }
    await repaint();
  });

  /* ── Boot ───────────────────────────────────────────────────────────
     Gated on DOMContentLoaded, and that is load-bearing rather than
     tidiness. It was written when this script was inline and ran DURING
     parse: the shared libraries it depends on — `window.AgnesTime`
     (datetime.js) and `window.AgnesKindGlyph` (kind_glyph.js) — are
     `<script>` tags near the end of the body, boot() awaits one fetch, and
     on a warm local server that fetch could answer before the parser
     reached them. The first render then read through an undefined global
     and threw, killing the whole render with no visible error and leaving
     the page on "Loading groups…". It happened twice, once per library,
     which is the tell that guarding each call site is the wrong fix.

     A module is deferred, so the narrow version of that race is gone. The
     gate stays anyway: it costs one tick, it is what makes the guarantee —
     EVERY deferred script has run — a property of this file rather than of
     where its tag happens to sit, and the `readyState` branch means a
     module that lands after DOMContentLoaded still boots immediately.
     (/admin/users/{id} already does exactly this, for the same reason.) */
  /* The end of the loading state, both halves of it: the skeleton's
     `aria-busy` comes off the list, and the work panel — hidden so a
     template "Pick a group" card would not sit beside a list that has not
     arrived — becomes visible. Called on BOTH exits from the fetch, because
     a page stuck mid-skeleton after a failure is the state #2140 item 2 was
     reported against, only worse. */
  const settled = () => {
    const list = el("ax-groups");
    if (list) list.removeAttribute("aria-busy");
    const work = el("ax-work");
    if (work) work.hidden = false;
  };

  const bootWhenReady = (fn) => (document.readyState === "loading"
    ? document.addEventListener("DOMContentLoaded", fn, { once: true })
    : fn());

  bootWhenReady(async function boot() {
    try {
      const r = await fetch(OVERVIEW_API, { credentials: "include" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      overview = await r.json();
    } catch (e) {
      // The header must not sit at "Pick a group" over a list that has given
      // up — that reads as "there are no groups", not as "this failed".
      settled();
      el("ax-what-title").textContent = "Access unavailable";
      el("ax-groups").innerHTML = `<div class="ax-empty">Could not load access data (${esc(e.message)}).</div>`;
      return;
    }
    settled();
    // Which group opens, in falling order of how explicit the ask was:
    //   1. ?group=<id> — a deep link, the only one someone TYPED (the retired
    //      /admin/access?group= URL used to land on a group's Access tab, so
    //      that shape keeps working).
    //   2. The group this sitting was last editing — what the old in-page pane
    //      switch preserved for free, and the reason Simulate can be a link.
    //   3. First in the list, which sortedGroups() makes Everyone.
    // Each is checked against the CURRENT list before it wins: a remembered id
    // outlives the group itself the moment someone deletes one, and falling
    // through to the default beats opening on an empty editor.
    const params = new URLSearchParams(window.location.search);
    // The box has to show what the URL says it is filtering by, or the list
    // arrives narrowed with nothing on screen explaining why.
    const findBox = el("ax-group-find");
    if (findBox && groupFilter) findBox.value = groupFilter;
    const groups = sortedGroups();
    /* The Everyone carrier is a real, selectable row on this page — it is
       simply not in `sortedGroups()`, which deliberately excludes it so it
       renders as an audience above the list rather than as a group in it
       (decision 04). Checking a `?group=` against that list alone therefore
       said "that group no longer exists" about the one row every `via
       Everyone →` link points at — the page told the reader their own link
       was broken, then dropped them on an unfiltered list. */
    const known = (id) => !!(id && (id === everyoneGroupId()
      || groups.some((g) => g.id === id)));
    const wanted = params.get("group");
    /* A bookmark outlives the group it points at, and falling through in
       silence looks like the link worked and the group is empty. */
    if (wanted && !known(wanted)) {
      toast(`That group no longer exists — showing all ${groups.length} instead`, false);
    }
    /* COLLAPSED ON ARRIVAL. The page opens as a list of groups, each one
       line, each answering whether it needs you — not on somebody's group
       with the tree already unrolled. Only a deep link opens one: `?group=`,
       or the `?resource=` hand-off from /admin/tables, which arrives knowing
       what it came to grant. A remembered selection no longer auto-opens —
       "where I was last time" is not a reason to unroll the tallest thing on
       the page for someone who came to check something else. */
    const wantedRes = PICK && known(recalledSelection()) ? recalledSelection() : null;
    selectedGroup = known(wanted) ? wanted : wantedRes;
    rememberSelection(selectedGroup);
    el("ax-by").querySelectorAll("[data-by]").forEach((b) => {
      const on = b.dataset.by === viewMode;
      b.classList.toggle("is-active", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
    // A `?resource=` arrival lands with the tree already narrowed to what the
    // caller came to grant — the filter is pre-filled rather than merely
    // suggested, so the row is on screen before anything is clicked.
    if (PICK) {
      resourceFilter = PICK.id;
    } else {
      // Restore this sitting's filter. A ?resource= arrival outranks it —
      // that caller asked for a specific narrowing. The scope half of this
      // went with the All/Granted segment.
      try {
        const ws = JSON.parse(sessionStorage.getItem(WORKSET_KEY) || "null");
        if (ws && ws.f) {
          resourceFilter = ws.f;
          groupFilter = ws.f;
          const gf = el("ax-group-find");
          if (gf) gf.value = ws.f;
        }
      } catch (e) { /* corrupt store → default view */ }
    }
    renderPick();
    await repaint();
    /* A `?resource=` arrival has narrowed the list to one row; put that row
       where the reader is looking. Without this the journey from a "via
       Everyone →" link ends correctly and invisibly: Everyone selected, the
       right row rendered, and the viewport still at the top of the longest
       group on the instance. `center` rather than `start` so the row is not
       hidden under the sticky group header. */
    if (PICK) {
      const at = document.querySelector(
        `[data-type="${cssEsc(PICK.type)}"][data-rid="${cssEsc(PICK.id)}"]`);
      if (at && at.scrollIntoView) {
        at.scrollIntoView({ block: "center", behavior: "smooth" });
        at.classList.add("is-landed");
      }
    }
    // `?user=` — the Simulate deep link. A package page's "Preview access"
    // and the stop rows' round trips arrive with the person pre-resolved
    // instead of "Pick a person…". Validated against the loaded list the
    // same way `?group=` is above.
    if (LENS === "sim") {
      const wantedUser = params.get("user");
      if (wantedUser) {
        await loadUsers();
        const sel = el("ax-sim-user");
        if ([...sel.options].some((o) => o.value === wantedUser)) {
          sel.value = wantedUser;
          sel.dispatchEvent(new Event("change"));
        }
      }
    }
    // The pre-tabs deep link. Now that Simulate is a URL, the hash is just an
    // older spelling of it — rewrite rather than keep a second way in.
    if (window.location.hash === "#simulate") {
      window.location.replace("/admin/access?lens=simulate");
    }
    // `#table:<id>` — the hash the retired /admin/groups took. Rewrite it to
    // the query the workspace speaks, so an old bookmark lands filtered
    // rather than silently ignored.
    const hash = window.location.hash || "";
    if (hash.startsWith("#table:")) {
      window.location.replace(
        `/admin/access?resource=table:${encodeURIComponent(decodeURIComponent(hash.slice(7)))}`);
    }
  });
})();
