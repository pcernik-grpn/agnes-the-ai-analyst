"""Static-source guards for the newcomer coach-mark tour (`TOURS.welcome`).

The tour engine is pure client-side (no headless browser in CI), so these
assert the source contract the way test_tour_journey_flags.py and
test_design_system_contract.py do.

What this protects:

1. **The step order is the newcomer's own path.** A first-timer lands on an
   empty /chat, so the tour opens on the composer — asking is the product —
   then Library, what "in stack" means, adding your own, sharing it. The
   previous tour walked /stack → /catalog, neither of which is a rail
   destination any more (#1088).
2. **Every anchor exists on the page the step names.** A coach-mark whose
   selector resolves to nothing is silently skipped, so a renamed id degrades
   into a step nobody ever sees rather than a visible break. Each selector is
   checked against the template that has to carry it.
3. **The unattended launch stays gated.** It fires once, for someone who has
   never touched any journey step, on a fresh chat — and gets out of the way
   the moment they actually ask something.
"""

import re
from pathlib import Path

TOUR_JS = Path("app/web/static/js/tour.js")
ONBOARDING_JS = Path("app/web/static/js/chat_onboarding.js")
CHAT_HTML = Path("app/web/templates/chat.html")
RAIL_HTML = Path("app/web/templates/_app_rail.html")
LIBRARY_HTML = Path("app/web/templates/library.html")
BASE_DS_HTML = Path("app/web/templates/base_ds.html")


def _js() -> str:
    return TOUR_JS.read_text(encoding="utf-8")


def _onboarding() -> str:
    return ONBOARDING_JS.read_text(encoding="utf-8")


def _welcome_block() -> str:
    """The `welcome:` array literal out of TOURS, comments and all."""
    js = _js()
    body = js.split("  welcome: [", 1)[1]
    return body.split("\n  ],", 1)[0]


def _steps() -> list[dict]:
    """Each step as {page, selector, title, centered} — parsed off the source.

    Deliberately a regex over the literal rather than a JS parse: the point is
    to pin the authored sequence, and anything that makes this unparseable is
    itself a change worth failing on.
    """
    out = []
    for chunk in _welcome_block().split("\n    {\n")[1:]:
        step = {}
        for key in ("page", "selector", "title"):
            m = re.search(rf"^      {key}: '(.*?)',$", chunk, re.MULTILINE)
            if m:
                step[key] = m.group(1)
        step["centered"] = "centered: true" in chunk
        out.append(step)
    return out


# --- Step sequence -------------------------------------------------------


def test_the_tour_opens_on_the_chat_composer():
    """The landing surface for a newcomer is /chat, and the composer is the
    only thing on it that matters — so it is step 0, not a nav tour."""
    first = _steps()[0]
    assert first["page"] == "/chat"
    assert first["selector"] == ".cloud-chat-composer, #chat-input"


def test_step_pages_walk_chat_then_library():
    """Two steps on /chat (ask, then what the Library is), the rest on
    /library. No step points at /stack or /catalog — neither is a rail
    destination any more, and none at /agents: that surface is taught by its own
    first-visit card (see TOURS.agents), not by dragging a newcomer onto a page
    they didn't ask for mid-walkthrough."""
    pages = [s["page"] for s in _steps()]
    assert pages == ["/chat", "/chat", "/library", "/library", "/library", "/library"]


def test_the_concepts_the_tour_has_to_teach_are_present():
    """Library → in stack → add → share, in that order. Matched on the
    anchors (copy is free to change; what each step POINTS at is the
    contract)."""
    selectors = [s.get("selector", "") for s in _steps()]
    assert "#nav-artefacts" in selectors[1]  # Library
    assert "[data-add-to-stack]" in selectors[2]  # in stack
    assert "#lib-new-btn" in selectors[3]  # upload / build
    assert "[data-share]" in selectors[4]  # share


def test_every_step_has_a_stable_key():
    """Other modules address steps by NAME — the checklist opens the card that
    teaches its own row via tourIndexOf(id, key). A step without a key can't be
    addressed, and a duplicate key silently sends two rows to one card."""
    keys = re.findall(r"^      key: '(.*?)',$", _welcome_block(), re.MULTILINE)
    assert keys == ["ask", "library", "in-stack", "add", "share", "done"]
    assert len(set(keys)) == len(keys)
    assert "export function tourIndexOf(id, key)" in _js()


# --- Agents: shown AND explained -----------------------------------------


def _agents_block() -> str:
    return _js().split("\n  agents: [", 1)[1].split("\n  ],", 1)[0]


def test_agents_is_its_own_first_visit_card_not_a_walkthrough_step():
    """Agents needed explaining — the rail's second destination was an
    unexplained robot icon, and "agent" reads as a duplicate of the chat you are
    already in. But it does NOT belong in the welcome walkthrough: as a step
    there it dragged every newcomer off /library onto a page they hadn't asked
    for, to explain a surface most readers don't need on day one. It is a solo
    card, shown once, on arrival."""
    js = _js()
    assert "\n  agents: [" in js
    block = _agents_block()
    assert "key: 'agents'" in block
    assert "page: '/agents'" in block
    # Exactly one step → the popover renders in its solo form (no dots, no
    # "I'll explore on my own", primary reads "Got it").
    assert block.count("\n    {") == 1
    # ...and it is NOT reachable as a welcome step any more.
    assert "'/agents'" not in _welcome_block()


def test_the_agents_card_explains_what_an_agent_is():
    """It has to draw the distinction, not just point at the page: the general
    chat reaches everything in your stack, an agent is a narrower one you define
    — a role, chosen knowledge, chosen skills — that can never exceed your own
    permissions."""
    block = _agents_block()
    assert "role" in block and "skills" in block
    assert "permissions" in block


def test_the_agents_card_launches_itself_once_on_arrival():
    agents_html = Path("app/web/templates/agents.html").read_text(encoding="utf-8")
    assert 'autoLaunchTour("agents")' in agents_html
    # The anchor it waits for has to be one the page actually renders.
    assert "data-ag-new" in agents_html
    # autoLaunchTour owns the once-only gate, so firing on every load is safe.
    launch = _js().split("export function autoLaunchTour(id) {", 1)[1].split("\n}", 1)[0]
    assert "isSeen(id)" in launch


def test_the_agents_card_waits_for_its_client_rendered_anchor():
    """`[data-ag-new]` is built once /api/agents resolves, so this fires before it
    exists. A single-pass resolve drops the card SILENTLY — nothing looks broken,
    the coach-mark simply never appears — so it opts into the bounded retry, and
    deliberately declares NO fallback selector (a fallback present on the first
    frame would always win the race and the wait would never happen)."""
    block = _agents_block()
    assert "awaitAnchor: true" in block
    assert "," not in block.split("selector: '", 1)[1].split("'", 1)[0]
    js = _js()
    assert "function _resolveAnchorEventually(step, cb)" in js
    assert "step.awaitAnchor" in js


# --- Anchors point at controls, not at look-alikes ------------------------


def test_the_in_stack_step_rings_the_add_control_and_the_filter():
    """The step teaches both halves of one concept — what "in stack" MEANS (the
    filter names the set) and how something joins it (the Add button) — so both
    carry the ring, and the popover anchors on the Add control because that is
    what the copy asks you to click.

    Two bugs got here. The filter used to LEAD the selector chain, so the row
    whose whole job is "put something in your stack" spotlighted a filter. Then
    leading with `[data-add-to-stack]` wasn't enough either: the Library ships
    every group collapsed, so on a first visit that control has a zero-size box
    and resolution fell straight back to the filter — hence `reveal`."""
    step = _welcome_block().split("      key: 'in-stack',", 1)[1].split("\n    },", 1)[0]
    assert step.split("selector: '", 1)[1].split("'", 1)[0] == "[data-add-to-stack]"
    # The ringed "filter" is the Scope segment — it replaced the "In stack
    # only" toggle when the Catalog/Marketplace fold landed.
    assert "extraSpotlight: ['#lib-stack-toggle']" in step
    # Opens the group that HOLDS an Add control, and settles for the scope
    # control only if no group has one at all.
    assert "revealHost: { container: '[data-lib-sec]', toggle: '[data-sec-toggle]' }" in step
    assert "fallbackSelector: '#lib-stack-toggle" in step
    js = _js()
    assert "function _applyExtraSpotlights(step, anchor)" in js
    # Rings are cleaned up on step change AND on end, or they outlive the tour.
    assert js.count("_clearExtraSpotlights()") >= 2


def test_the_add_step_spotlights_the_add_button_itself():
    """The control the reader has to find, and the one the copy names. It briefly
    opened the menu and ringed THAT instead — which put the ring on a popover
    rather than on the button you press to get it, and left the open menu and the
    card competing for the same space below it."""
    step = _welcome_block().split("      key: 'add',", 1)[1].split("\n    },", 1)[0]
    assert step.split("selector: '", 1)[1].split("'", 1)[0] == "#lib-new-btn"
    assert "+ Add" in step  # the copy names the control the ring is on
    assert 'id="lib-new-btn"' in LIBRARY_HTML.read_text(encoding="utf-8")


def test_a_comma_selector_is_a_priority_chain_not_document_order():
    """Every multi-selector step here is authored specific-first ("a row's Add
    control, else the filter"). One querySelectorAll over the whole list returns
    whichever comes first in the DOCUMENT, which for those chains is reliably
    the generic fallback higher up the page."""
    js = _js()
    body = js.split("function _visibleMatch(selector) {", 1)[1].split("\n}", 1)[0]
    assert "split(',')" in body
    assert "querySelectorAll" in body
    assert "offsetWidth" in body and "getClientRects()" in body


# --- The connect micro-tour ----------------------------------------------


def test_the_connect_step_is_its_own_one_card_tour():
    """The last checklist row used to be a bare link to a long orientation page
    with eight sections — "navigated", not "guided"."""
    js = _js()
    assert "\n  connect: [" in js
    block = js.split("\n  connect: [", 1)[1].split("\n  ],", 1)[0]
    assert "key: 'connect'" in block
    # Carries the hash: the destination page opens the right fold off it.
    assert "page: '/how-it-works#connect'" in block
    assert "#connect" in Path("app/web/templates/how_it_works.html").read_text(encoding="utf-8")


def test_a_step_page_with_a_hash_still_compares_as_the_same_page():
    """`page: '/how-it-works#connect'` must navigate WITH the hash and compare
    WITHOUT it — comparing the raw string is an infinite navigation loop (arrive
    at /how-it-works, decide we aren't there, navigate again)."""
    js = _js()
    body = js.split("function pathMatches(page, path) {", 1)[1].split("\n}", 1)[0]
    assert "split('#')[0]" in body


def test_the_last_step_is_the_centered_final_choice():
    last = _steps()[-1]
    assert last["centered"] is True
    assert "finalChoice: true" in _welcome_block()


def test_the_retired_stack_tour_is_gone():
    """`TOURS.stack` walked /stack's tabs zone and the Catalog submit CTA.
    Both surfaces left the rail; the tour must not linger behind them."""
    js = _js()
    assert "\n  stack: [" not in js
    assert "#stack-explore-zone" not in js
    assert "#browse-submit-btn" not in js


# --- Anchors exist on the pages the steps name ---------------------------


def test_composer_anchor_exists_on_the_chat_page():
    chat = CHAT_HTML.read_text(encoding="utf-8")
    # Rail wraps the textarea in the pill; topnav renders the bare textarea.
    assert 'class="cloud-chat-composer"' in chat
    assert 'id="chat-input"' in chat


def test_library_nav_anchor_exists_in_the_rail():
    """Rail is the only chrome since Wave 0 (2026-08 legacy retirement) —
    ``_app_header.html`` is deleted, so this no longer has a topnav twin to
    check."""
    assert 'id="nav-artefacts"' in RAIL_HTML.read_text(encoding="utf-8")


def test_the_share_step_can_open_the_group_that_holds_its_anchor():
    """The Library ships every group collapsed on a first visit, so the sharing
    badge is in the DOM with a zero-size box.

    The step used to name its openers by section key (`files`, `skill`), which was
    guesswork twice over: it opened groups that turned out not to contain a
    badge, and it silently found nothing for any group nobody had listed — a
    shareable row in Plugins or Recipes. `revealHost` walks up from the anchor
    instead, so the group that gets opened is always the one the target is in."""
    js = _js()
    share = _welcome_block().split("      key: 'share',", 1)[1].split("\n    },", 1)[0]
    assert "revealHost: { container: '[data-lib-sec]', toggle: '[data-sec-toggle]' }" in share
    reveal = js.split("function _revealAnchorHost(step) {", 1)[1].split("\n}", 1)[0]
    assert "el.closest(host.container)" in reveal
    # Never collapse something the user already opened...
    assert "aria-expanded" in reveal and "'true'" in reveal
    # ...and put back any group that was opened but didn't hold the target, so a
    # step doesn't rearrange the reader's page to point at one row.
    assert "container.contains(found)" in reveal
    visible = js.split("function _visibleMatch(selector) {", 1)[1].split("\n}", 1)[0]
    assert "querySelectorAll" in visible
    assert "offsetWidth" in visible and "getClientRects()" in visible
    lib = LIBRARY_HTML.read_text(encoding="utf-8")
    assert 'data-sec-toggle="{{ sec.key }}"' in lib
    assert 'data-lib-sec="{{ sec.key }}"' in lib


def test_library_step_anchors_exist_on_the_library_page():
    lib = LIBRARY_HTML.read_text(encoding="utf-8")
    # The step's primary anchor is a row's own Stack control; its fallback is
    # the "In stack only" toggle — both have to exist (in the template source;
    # the toggle renders conditionally) for the step to be reliable on a
    # fresh account.
    assert 'id="lib-stack-toggle"' in lib
    assert "data-add-to-stack=" in lib
    assert 'id="lib-new-btn"' in lib
    assert "data-share=" in lib


# --- Unattended launch ---------------------------------------------------


def test_auto_launch_runs_once_and_never_over_a_running_tour():
    js = _js()
    body = js.split("export function autoLaunchTour(id) {", 1)[1].split("\n}", 1)[0]
    assert "isSeen(id)" in body, "a tour that has been seen must never re-launch itself"
    assert "_active" in body and "_pendingResumesHere()" in body, (
        "must not stack a second engine over a running or resuming tour"
    )
    # ...but yielding to a pending record is now scoped to one that would ACTUALLY
    # resume here. `getPending()` — any record at all — meant abandoning the
    # welcome tour on /library suppressed an unrelated card on /agents for the
    # next twelve seconds, with nothing on screen to explain why.
    guard = _js().split("function _pendingResumesHere() {", 1)[1].split("\n}", 1)[0]
    assert "RESUME_FRESH_MS" in guard
    assert "pathMatches(step.page, window.location.pathname)" in guard


def test_auto_launch_is_gated_on_an_untouched_journey():
    """Not just `first_asked`: someone who worked the checklist by hand, or
    restarted onboarding after finishing it, has seen these surfaces."""
    js = _onboarding()
    newcomer = js.split("function isNewcomer() {", 1)[1].split("\n}", 1)[0]
    assert "STEP_KEYS.every((k) => !journey[k])" in newcomer
    launch = js.split("function maybeAutoLaunchTour() {", 1)[1].split("\n}", 1)[0]
    assert "if (!chatMode) return" in launch
    assert "if (!isNewcomer()) return" in launch
    # A deep link into an existing conversation isn't a first look — and the
    # empty-state composer step 0 points at isn't even what's on screen.
    assert 'get("session")' in launch


def test_submitting_the_first_message_takes_the_coach_mark_down():
    js = _onboarding()
    assert "export function noteComposerSubmitted()" in js
    dismiss = js.split("function dismissTourIfShowing() {", 1)[1].split("\n}", 1)[0]
    assert ".tour-overlay" in dismiss, "no module fetch when no tour is running"
    assert "dismissActiveTour" in dismiss


def test_the_coach_mark_comes_down_on_submit_not_on_a_ready_socket():
    """`onUserMessage` runs only AFTER `ensureWsReady()`, which takes seconds on
    a cold runner and never resolves at all when chat is down — so hanging the
    dismissal off it leaves the card sitting over the composer the user just
    typed into. It belongs in the synchronous feedback block at the top of
    `submitUserMessage`, beside the composer clear."""
    chat = Path("app/web/static/js/chat.js").read_text(encoding="utf-8")
    submit = chat.split("async function submitUserMessage(text) {", 1)[1]
    call = submit.index("onboardingNoteComposerSubmitted()")
    assert call < submit.index("await ensureWsReady()")
    assert call < submit.index("onboardingOnUserMessage")


def test_dismissing_does_not_mark_the_tour_seen():
    """The rest of the walkthrough is still worth having — it stays reachable
    from the checklist's "↻" — so this ends the run WITHOUT retiring it."""
    js = _js()
    body = js.split("export function dismissActiveTour() {", 1)[1].split("\n}", 1)[0]
    assert "_endTour(false)" in body


def test_boot_script_loads_the_engine_only_for_a_pending_resume():
    """The `tourPages` allow-list named /stack and /catalog — a stale pair that
    silently stopped covering the pages the tour runs on. A cross-page hop
    always leaves a sessionStorage record, so that record is the whole test;
    every other entry point imports the module itself."""
    base = BASE_DS_HTML.read_text(encoding="utf-8")
    assert "tourPages" not in base
    assert "agnes.tour.pending" in base
    assert "if (!pending) return;" in base


# --- Checklist parity ----------------------------------------------------


def test_checklist_steps_point_at_the_library_not_the_retired_stack_page():
    """The journey columns keep their names (they are DB columns on both
    backends), but every destination the checklist offers has to be a surface
    that is still in the nav."""
    js = _onboarding()
    meta = js.split("const STEP_META = {", 1)[1].split("\n};", 1)[0]
    hrefs = re.findall(r'href: "([^"]+)"', meta)
    assert "/stack" not in hrefs
    assert "/catalog" not in hrefs
    assert hrefs.count("/library") == 3


def test_the_checklist_runs_in_the_order_the_tour_walks():
    """The card and the tour are two views of one onboarding, so they must tell
    the same story: ask → see your Library → put something in your stack → add
    or share your own → use Agnes elsewhere → build an agent out of it. The old
    order ran the stack step second, ahead of the Library step the tour
    completes for you, so finishing the tour ticked row 3 and left row 2
    pending.

    The agent step is LAST on purpose: it is the only one that presumes the
    others. An agent is scoped to part of your stack, so offering it before
    there is a stack asks someone to choose from nothing.
    """
    js = _onboarding()
    keys = re.findall(r'"(\w+)",', js.split("const STEP_KEYS = [", 1)[1].split("]", 1)[0])
    assert keys == [
        "first_asked",
        "explored_stack",
        "stack_setup_done",
        "catalog_discovered",
        "use_anywhere",
        "agent_created",
    ]


def test_every_step_is_clickable_in_any_order():
    """There is no positional lock at all: progress was never sequential (the
    tour completes the Library step, the middle steps land from real activity
    server-side), so a row ahead of the cursor read as disabled while nothing
    actually stopped it — and a finished row had no way back to what it taught.
    `active` is a "you are here" marker, not a gate."""
    js = _onboarding()
    assert "locked" not in js
    assert 'const cls = s.done ? "done" : i === nextIdx ? "active" : "todo";' in js
    # No guard between the click and runStep.
    assert 'btn.addEventListener("click", () => runStep(btn.getAttribute("data-journey-key")))' in js
    # ...and a sub-action is never suppressed by position either.
    assert "function subActionHtml(step) {" in js


def test_every_checklist_step_opens_guidance_for_itself():
    """The core of it: FIVE steps, five coach-marks, each addressed by the tour
    step key that teaches that row.

    Before this, two rows launched the welcome tour at index 0 — so clicking
    "Put knowledge in your stack" replayed the composer card and left the reader
    to walk four cards forward to their own step — and the other three were bare
    hrefs that dropped them on a page without saying which control was the one
    they had just asked about."""
    js = _onboarding()
    assert 'const WELCOME_TOUR = "welcome";' in js
    assert 'const CONNECT_TOUR = "connect";' in js
    meta = js.split("const STEP_META = {", 1)[1].split("\n};", 1)[0]
    tours = re.findall(r"tour: \{ id: (\w+), step: \"([\w-]+)\" \}", meta)
    assert tours == [
        ("WELCOME_TOUR", "ask"),
        ("WELCOME_TOUR", "in-stack"),
        ("WELCOME_TOUR", "add"),
        ("CONNECT_TOUR", "connect"),
    ]
    # None of them hardcodes an index into the tour.
    assert "launchTour: " not in js
    assert "launchTour(step.tour.id, mod.tourIndexOf(step.tour.id, step.tour.step))" in js


def test_explore_your_library_is_completed_by_visiting_the_library():
    """The one step with NO coach-mark, deliberately. The welcome tour's
    `library` card lives on /chat and rings the rail's Library NAV ITEM — right
    for a walkthrough teaching the chrome, wrong for a row that promises to show
    you your Library: clicked on /chat it drew a ring around a nav link and went
    nowhere. Opening the Library IS the step, so the visit completes it."""
    meta = _onboarding().split("const STEP_META = {", 1)[1].split("\n};", 1)[0]
    step = meta.split("explored_stack: {", 1)[1].split("\n  },", 1)[0]
    assert 'href: "/library"' in step
    assert "tour:" not in step
    # ...and the page render is what records it.
    router = Path("app/web/router.py").read_text(encoding="utf-8")
    handler = router.split("async def library_page(", 1)[1].split("\nasync def ", 1)[0]
    assert "mark_journey(uid, explored_stack=True)" in handler


def test_the_step_keys_the_checklist_asks_for_exist_in_the_tours():
    """The two files are joined by these strings; a rename on either side has to
    fail here rather than degrade into a coach-mark that silently starts from
    step 0 (tourIndexOf's fallback)."""
    meta = _onboarding().split("const STEP_META = {", 1)[1].split("\n};", 1)[0]
    js = _js()
    welcome_keys = set(re.findall(r"^      key: '(.*?)',$", _welcome_block(), re.MULTILINE))
    connect_block = js.split("\n  connect: [", 1)[1].split("\n  ],", 1)[0]
    connect_keys = set(re.findall(r"key: '(.*?)'", connect_block))
    for tour_const, step_key in re.findall(r"tour: \{ id: (\w+), step: \"([\w-]+)\" \}", meta):
        known = welcome_keys if tour_const == "WELCOME_TOUR" else connect_keys
        assert step_key in known, f"{step_key} is not a step of {tour_const}"


def test_the_checklist_carries_the_agent_step_as_a_row_not_a_loose_link():
    """Agents reaches the checklist as a STEP, never as a link between steps.

    This started as the opposite guard. "Or build an agent of your own" used to
    hang under "Add or share something" as a full-width link BETWEEN two step
    rows, so the reader had to work out that the thing in the middle of the list
    was not itself a step — and it was removed for that. A sixth ROW was
    rejected at the same time for a different reason: a new journey column
    defaults FALSE for everyone, so it un-retires the finished card of everyone
    who already completed onboarding.

    That second objection is what the v118 migration answers — it backfills
    ``agent_created`` TRUE for users who own an agent and for users already
    flagged ``onboarded``, so no finished checklist re-opens. With that settled
    the step is a legitimate row, which is what this now pins: a keyed entry in
    STEP_KEYS/STEP_META, not a loose anchor in the rendering path.
    """
    js = _onboarding()
    meta = js.split("const STEP_META = {", 1)[1].split("\n};", 1)[0]
    keys = js.split("const STEP_KEYS = [", 1)[1].split("];", 1)[0]

    assert '"agent_created"' in keys, "the agent milestone must be a tracked step"
    assert "agent_created: {" in meta
    assert '"/agents"' in meta, "the step needs its destination"

    # The retired shape must not come back as rendered markup — the phrase
    # survives only in the comment that records why it went.
    assert 'Or build an agent of your own"' not in js.replace('// No loose "Or build an agent of your own" link', "")

    # The rail is still a route to it, so the surface has a permanent home.
    rail = RAIL_HTML.read_text(encoding="utf-8")
    assert 'href="/agents"' in rail


def test_only_the_notification_sub_action_is_gated_on_telegram():
    """The gate used to be the second line of subActionHtml, so ANY sub-action
    added later inherited a dependency on a Telegram bot being configured — it
    hid an unrelated route on every instance without one."""
    js = _onboarding()
    body = js.split("function subActionHtml(step) {", 1)[1].split("\n}", 1)[0]
    assert 'sub.gate === "telegram"' in body
    telegram_branch = body.split('sub.gate === "telegram"', 1)[1]
    assert "telegramBot()" in telegram_branch
    # ...and the ungated return, after that branch closes, has no bot check.
    assert "telegramBot()" not in body.split("return `<a class=", 3)[-1]


def test_the_rows_are_the_panels_actions_with_no_duplicate_cta():
    """The card carries no "Show me: <next step>" button. It ran the same
    runStep() path as the highlighted row it named, so with every row clickable
    it is a second control for one action — and while it existed it was the only
    thing in the panel that LOOKED clickable, which is what made the rows read as
    inert text. What is left below the list is only the quiet skip."""
    js = _onboarding()
    assert "data-journey-show-me" not in js
    assert "journey-cta" not in js
    assert "Show me" not in js
    assert '${complete ? "Start over" : "Skip onboarding"}' in js
    # One implementation behind every row, reached by the step's own key.
    assert "function runStep(key) {" in js
    assert 'runStep(btn.getAttribute("data-journey-key"))' in js


def test_clicking_a_step_goes_there_and_never_ticks_it():
    """The defect this closes: clicking a row marked the milestone done on the
    spot. Three of them did it, so the card behaved like a to-do list you check
    off yourself — "Explore your Library" went ✓ and struck-through without
    taking anyone to the Library. `runStep` may navigate and guide; the tick has
    to be earned where the work happens, server-side."""
    js = _onboarding()
    run_step = js.split("function runStep(key) {", 1)[1].split("\n}", 1)[0]
    assert "patchJourney" not in run_step
    assert "launchTour" in run_step and "window.location.href = step.href" in run_step
    # Every step has a real destination, or a click has nowhere to go. Matched at
    # the STEP's own indent (4 spaces) so a `sub`'s href — one indent deeper, and
    # not what a row click follows — is not counted as a step of its own.
    meta = js.split("const STEP_META = {", 1)[1].split("\n};", 1)[0]
    keys = re.findall(r'"(\w+)",', js.split("const STEP_KEYS = [", 1)[1].split("]", 1)[0])
    assert len(re.findall(r'^    href: "(/[^"]*)"', meta, re.MULTILINE)) == len(keys)
    # ...and each of the five is marked from the action that earns it. Four are
    # covered by their own suites (test_journey_activity_marks.py for the stack /
    # sharing endpoints, tests/test_chat_api.py for first_asked); the two page
    # visits are asserted here because this file owns the checklist contract.
    router = Path("app/web/router.py").read_text(encoding="utf-8")
    connect = router.split("async def how_it_works_page(", 1)[1].split("\nasync def ", 1)[0]
    assert 'mark_journey(user.get("id"), use_anywhere=True)' in connect


def test_the_step_explanation_is_a_visible_line_not_a_native_tooltip():
    """`title` on every row put the one sentence that explains a step behind a
    hover nobody tries, which never happens on touch — and the OS box it opened
    was wide enough to cover the rows under the one being explained. It is a real
    line now, on the suggested step, which also gives a very plain list a focal
    point."""
    js = _onboarding()
    assert 'title="${escapeAttr(s.why)}"' not in js
    assert 'class="cloud-chat-journey-why"' in js
    assert 'cls === "active"' in js
    # Both chromes style it — chat.css does not load on the other rail pages.
    for sheet in (Path("app/web/static/css/chat.css"), Path("app/web/static/css/rail.css")):
        assert ".cloud-chat-journey-why" in sheet.read_text(encoding="utf-8")


def test_the_rail_restates_the_sub_action_style_it_cannot_inherit():
    """chat.css only loads on /chat, and the checklist popover is mounted on every
    rail page — so with only a font-size override here, "Choose where Agnes
    reaches you" rendered as a raw browser link (blue, purple once visited,
    underlined) inside a card built entirely from --ds-* ink."""
    rail = Path("app/web/static/css/rail.css").read_text(encoding="utf-8")
    block = rail.split(".cloud-chat-journey-sub-action {", 1)[1].split("}", 1)[0]
    assert "color: var(--ds-text-muted)" in block
    assert "text-decoration: none" in block


def test_the_panel_re_reads_progress_before_it_is_shown():
    """Steps now complete from real activity server-side, so the copy loaded at
    page load can be stale by the time the popover opens — and a checklist that
    shows a step you just finished as pending reads as broken."""
    js = _onboarding()
    assert 'const REFRESH_EVENT = "agnes:journey-refresh";' in js
    assert "async function refreshJourney()" in js
    rail = Path("app/web/static/js/rail_history.js").read_text(encoding="utf-8")
    assert 'new CustomEvent("agnes:journey-refresh")' in rail
