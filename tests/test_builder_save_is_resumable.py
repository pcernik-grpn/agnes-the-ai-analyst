"""Two partial-save bugs in the MCP-source and linked-apps builders.

Both are the same shape: a Save that makes SEVERAL calls, and a first failure
that leaves the world half-changed and the retry unable to finish the job.

  * MCP source — the row is created first, then the secret, then one grant per
    group. A failure in a later step left a registered source on screen with an
    error; pressing Save again re-ran the whole sequence and registered a
    SECOND source. The only recovery from "secret stored, grant failed" was a
    duplicate.

  * Linked apps — one grant per (app x group) through ``Promise.all``. An
    already-existing grant answers 409, which the code counted as a failure, so
    one already-granted pair failed the whole save. Worse on retry: the pairs
    that succeeded the first time were now 409s too, so every retry failed
    identically and Save became permanently unreachable. The pre-builder
    wizard (``admin_linked_apps.html``) already read 409 as done; the builder
    that replaced it lost that.

These run the SHIPPED functions under node, driving them through the real
click/input handlers with a scripted ``fetch`` — not a transcription of the
logic into Python, which would pass whatever the JS actually does. The DOM shim
is deliberately thin: every element lookup these paths make is guarded, so
returning null is enough, and the one thing the harness must be honest about is
the sequence of HTTP calls the module makes.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "app" / "web" / "static" / "js" / "components"
SHELL = JS / "builder_shell.js"
#: The MCP builder renders its apps section through this, so the harness has
#: to load it for the same reason the page does.
APPS_PANEL = JS / "linked_apps_panel.js"
MCP = JS / "mcp_builder.js"
LINKED = JS / "linked_apps_builder.js"


def _node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# The shim. `document` returns null for every lookup (all guarded) and records
# the click/input handlers so a test can drive the module the way a person
# does. `fetch` is scripted per test: it appends to CALLS and answers from a
# per-url queue, so "what did Save actually send, in order" is observable.
_HARNESS = r"""
const CALLS = [];
// Request bodies, keyed the same way as CALLS ("METHOD url") and appended in
// order. CALLS answers "what did Save send, in what order"; BODIES answers
// "and with what" — needed once a step's PAYLOAD is the point of the test
// (which tools got registered, in what mode) rather than just its existence.
const BODIES = {};
function el(extra) {
  return Object.assign({
    innerHTML: '', textContent: '', value: '', disabled: false, style: {},
    scrollHeight: 20,
    addEventListener() {}, removeEventListener() {}, focus() {}, blur() {},
    querySelector: () => null, querySelectorAll: () => [],
    getAttribute: () => null, setAttribute() {}, hasAttribute: () => false,
    classList: { add() {}, remove() {}, toggle() {} },
    closest: () => null,
  }, extra || {});
}
const handlers = {};
global.document = {
  addEventListener(kind, fn) { (handlers[kind] = handlers[kind] || []).push(fn); },
  removeEventListener() {},
  getElementById: () => null,
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: () => el(),
  body: el(),
};
const mount = el();
// `window` IS the global object in a browser, so a module that does
// `window.X = ...` and then reads a bare `X` works there. A shim that made
// `window` a plain object broke exactly that, and every builder reads the
// shell through a bare `BuilderShell` somewhere — so the shim aliases the
// global instead of standing in for it.
global.window = global;
global.window.location = { href: '' };
global.self = global.window;

// A synthetic target: `closest` returns itself so the module's delegation
// finds it, and attributes answer from the object the test passes in.
function target(attrs) {
  const t = el({
    id: attrs.id || '',
    value: attrs.value === undefined ? '' : attrs.value,
    getAttribute: (k) => (k in attrs ? attrs[k] : null),
    hasAttribute: (k) => k in attrs,
  });
  t.closest = () => t;
  return t;
}
function fire(kind, attrs) {
  (handlers[kind] || []).forEach((fn) => fn({ target: target(attrs), preventDefault() {} }));
}

// `responses` maps "METHOD url" -> either ONE {status, body} that answers
// every call (a read the module may repeat — it reloads a source's tools on
// each pick, and a one-shot queue left the second call empty, which read as
// "no lister tool" and silently short-circuited the flow), or an ARRAY
// consumed in order (a write whose Nth attempt is the point of the test). An
// unlisted url answers 200 {}.
//
// Matching is EXACT, not by prefix: with a prefix,
// 'POST /api/admin/mcp-sources' also swallowed
// 'POST /api/admin/mcp-sources/builder/turn' — the builder's own opening turn
// consumed the create's queued response, and the test then measured the
// harness rather than the module.
function installFetch(responses) {
  global.fetch = (url, opts) => {
    const method = ((opts || {}).method || 'GET').toUpperCase();
    const key = method + ' ' + url;
    CALLS.push(key);
    if ((opts || {}).body) {
      try { (BODIES[key] = BODIES[key] || []).push(JSON.parse(opts.body)); }
      catch (_) { (BODIES[key] = BODIES[key] || []).push(opts.body); }
    }
    const entry = responses[key];
    let queued = null;
    if (Array.isArray(entry)) queued = entry.length ? entry.shift() : null;
    else if (entry) queued = entry;
    const status = queued ? queued.status : 200;
    const body = queued ? (queued.body || {}) : {};
    return Promise.resolve({
      ok: status >= 200 && status < 300,
      status,
      json: () => Promise.resolve(body),
    });
  };
}
const flush = () => new Promise((r) => setTimeout(r, 0));
"""


def _load(*paths: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in paths)


# ── MCP source: Save resumes, it does not re-register ────────────────────────


def test_a_failed_grant_does_not_make_the_retry_register_a_second_source():
    script = (
        _HARNESS
        + _load(SHELL, APPS_PANEL, MCP)
        + r"""
installFetch({
  'GET /api/admin/groups': { status: 200, body: [{ id: 'g1', name: 'Analysts' }] },
  // The create succeeds and hands back an id. Keyed exactly, so the
  // builder's own opening turn (POST .../builder/turn) cannot consume it.
  'POST /api/admin/mcp-sources/preview-introspect': {
    status: 200, body: { tools: [{ name: 'search', description: 'find things', read_only: true }] },
  },
  'POST /api/admin/mcp-sources': [{ status: 201, body: { id: 'src-1' } }],
  // ...and the FIRST grant attempt fails. The retry's grant is unqueued, so
  // it answers 200 — the resumed save must then finish.
  'POST /api/admin/mcp-sources/src-1/grants': [{ status: 500, body: { detail: 'boom' } }],
});
window.AgnesMcpBuilder.open({ mount });
(async () => {
  await flush();
  // Type a name and a url, then check the connection — Register source is
  // gated on a real introspection, because a source with no tools is a source
  // that can answer nothing.
  fire('input', { 'data-mcp-field': 'name', value: 'CRM' });
  fire('input', { 'data-mcp-field': 'url', value: 'https://mcp.example.com/sse' });
  fire('click', { 'data-mcp-check': '1' });
  for (let i = 0; i < 8; i++) await flush();
  // Pick a group so there is a grant step at all. The picker has to be opened
  // first: `toggleGroup` resolves the id against the loaded group list, and
  // the picker is what loads it. Without this the group list stayed empty, the
  // grant step was skipped entirely, and both of these tests passed against a
  // Save that had no partial state to get wrong.
  fire('click', { 'data-mcp-openpick': '1' });
  for (let i = 0; i < 8; i++) await flush();
  fire('click', { 'data-mcp-pick': 'g1' });
  for (let i = 0; i < 4; i++) await flush();
  fire('click', { id: 'mcp-save' });
  for (let i = 0; i < 12; i++) await flush();
  const afterFirst = CALLS.slice();
  // Retry: the grant now succeeds.
  fire('click', { id: 'mcp-save' });
  for (let i = 0; i < 12; i++) await flush();
  process.stdout.write(JSON.stringify({
    afterFirst,
    all: CALLS,
    creates: CALLS.filter((c) => c === 'POST /api/admin/mcp-sources').length,
    href: window.location.href,
  }));
})();
"""
    )
    res = _node(script)
    assert res["creates"] == 1, f"Save re-registered the source on retry: {res['all']}"
    assert res["href"] == "/admin/mcp-sources/src-1", (
        f"the resumed save never finished: href={res['href']!r}, calls={res['all']}"
    )


def test_an_already_granted_group_does_not_fail_the_mcp_save():
    script = (
        _HARNESS
        + _load(SHELL, APPS_PANEL, MCP)
        + r"""
installFetch({
  'GET /api/admin/groups': { status: 200, body: [{ id: 'g1', name: 'Analysts' }] },
  'POST /api/admin/mcp-sources/preview-introspect': {
    status: 200, body: { tools: [{ name: 'search', description: 'find things', read_only: true }] },
  },
  'POST /api/admin/mcp-sources': [{ status: 201, body: { id: 'src-2' } }],
  // 409 = the group can already reach it, which is the end state Save wants.
  'POST /api/admin/mcp-sources/src-2/grants': {
    status: 409, body: { detail: 'Grant already exists for this group/resource_type/resource_id' },
  },
});
window.AgnesMcpBuilder.open({ mount });
(async () => {
  await flush();
  fire('input', { 'data-mcp-field': 'name', value: 'CRM' });
  fire('input', { 'data-mcp-field': 'url', value: 'https://mcp.example.com/sse' });
  fire('click', { 'data-mcp-check': '1' });
  for (let i = 0; i < 8; i++) await flush();
  fire('click', { 'data-mcp-openpick': '1' });
  for (let i = 0; i < 8; i++) await flush();
  fire('click', { 'data-mcp-pick': 'g1' });
  for (let i = 0; i < 4; i++) await flush();
  fire('click', { id: 'mcp-save' });
  for (let i = 0; i < 12; i++) await flush();
  process.stdout.write(JSON.stringify({ href: window.location.href, all: CALLS }));
})();
"""
    )
    res = _node(script)
    assert res["href"] == "/admin/mcp-sources/src-2", (
        f"a 409 grant was read as a failed save: href={res['href']!r}, calls={res['all']}"
    )


# ── Publishing apps: 409 is done, and one failure does not hide the rest ────


def _apps_script(grant_responses: str, tail: str) -> str:
    """Drive the MCP builder through registration and into the apps section.

    This used to be its own builder (`linked_apps_builder.js`) and its own
    page. It could not stand alone — its first step asked which MCP source to
    read apps from, and dead-ended if none was registered — so it is now the
    last section of the builder that registers the source, and these are the
    same two invariants against the folded code.
    """
    return (
        _HARNESS
        + _load(SHELL, APPS_PANEL, MCP)
        + r"""
installFetch({
  // A lister tool, which is what makes the apps section appear at all.
  'POST /api/admin/mcp-sources/preview-introspect': { status: 200, body: { tools: [
    { name: 'list_data_apps', description: 'apps hosted upstream', read_only: true },
  ] } },
  'GET /api/admin/groups': { status: 200, body: [
    { id: 'g1', name: 'Analysts' }, { id: 'g2', name: 'Finance' },
  ] },
  'POST /api/admin/mcp-sources': [{ status: 201, body: { id: 's1' } }],
  'GET /api/data-apps?kind=linked&source=s1': { status: 200, body: { apps: [
    { id: 'app-a', name: 'Churn' }, { id: 'app-b', name: 'Revenue' },
  ] } },
"""
        + grant_responses
        + r"""
});
window.AgnesMcpBuilder.open({ mount, dataAppsEnabled: true });
(async () => {
  await flush();
  fire('input', { 'data-mcp-field': 'name', value: 'keboola' });
  fire('input', { 'data-mcp-field': 'url', value: 'https://mcp.example.com/sse' });
  fire('click', { 'data-mcp-check': '1' });
  for (let i = 0; i < 8; i++) await flush();
  // Pick the group before registering: the same groups that get the source
  // get its apps, which is the whole reason the section lives here.
  fire('click', { 'data-mcp-openpick': '1' });
  for (let i = 0; i < 8; i++) await flush();
  fire('click', { 'data-mcp-pick': 'g1' });
  for (let i = 0; i < 4; i++) await flush();
  fire('click', { id: 'mcp-save' });
  for (let i = 0; i < 14; i++) await flush();
  // Registered — and still here, because there are apps to catalogue.
  const hrefAfterRegister = window.location.href;
  fire('click', { 'data-la-read': '1' });
  for (let i = 0; i < 14; i++) await flush();
"""
        + tail
        + r"""
})();
"""
    )


def test_registering_a_source_that_lists_apps_stays_on_the_page():
    """The fold is only worth anything if the builder does not navigate away
    from the section it just revealed."""
    script = _apps_script(
        "",
        r"""
  process.stdout.write(JSON.stringify({
    hrefAfterRegister,
    href: window.location.href,
    all: CALLS,
    materialized: CALLS.filter((c) => c.indexOf('/materialize') >= 0).length,
  }));
""",
    )
    res = _node(script)
    assert res["hrefAfterRegister"] == "", f"it redirected instead of showing the apps: {res['all']}"
    assert res["materialized"] == 1, f"the lister was never materialized: {res['all']}"


def test_an_instance_with_data_apps_off_is_never_offered_the_section():
    """The retired builder's whole failure mode: it was reachable on an
    instance where data apps are switched off, and every path through it ended
    at `data_apps_disabled` — AFTER it had already switched a shared tool to
    materialize mode. Off means the section does not exist."""
    script = (
        _HARNESS
        + _load(SHELL, APPS_PANEL, MCP)
        + r"""
installFetch({
  'POST /api/admin/mcp-sources/preview-introspect': { status: 200, body: { tools: [
    { name: 'list_data_apps', description: 'apps hosted upstream', read_only: true },
  ] } },
  'POST /api/admin/mcp-sources': [{ status: 201, body: { id: 's9' } }],
});
window.AgnesMcpBuilder.open({ mount, dataAppsEnabled: false });
(async () => {
  await flush();
  fire('input', { 'data-mcp-field': 'name', value: 'keboola' });
  fire('input', { 'data-mcp-field': 'url', value: 'https://mcp.example.com/sse' });
  fire('click', { 'data-mcp-check': '1' });
  for (let i = 0; i < 8; i++) await flush();
  fire('click', { id: 'mcp-save' });
  for (let i = 0; i < 14; i++) await flush();
  process.stdout.write(JSON.stringify({ href: window.location.href, all: CALLS }));
})();
"""
    )
    res = _node(script)
    assert res["href"] == "/admin/mcp-sources/s9", (
        f"it waited for an apps section that cannot exist here: {res['all']}"
    )
    assert not [c for c in res["all"] if "/materialize" in c], (
        f"it materialized a lister on an instance that cannot show apps: {res['all']}"
    )


def test_reading_the_app_list_sends_what_the_registry_requires():
    """The mode is what makes the read possible — the projection reads a table
    the run writes — and `tool_registry.upsert` refuses `materialize` without
    a schedule string, so both go.

    What was removed is the UI's claim ABOUT that schedule. Nothing in the
    scheduler reads a `tool_registry` row, so the `daily 03:00` the old wizard
    wrote never fired; a "keep this list current" switch would have promised a
    refresh the product does not perform. The panel says the catalogue is a
    snapshot instead, which is true.
    """
    script = _apps_script(
        "",
        r"""
  process.stdout.write(JSON.stringify({
    bodies: BODIES['PUT /api/admin/mcp-tools/s1__list_data_apps'] || [],
    all: CALLS,
  }));
""",
    )
    res = _node(script)
    assert res["bodies"], "the lister tool was never put into materialize mode"
    assert res["bodies"][0] == {"mode": "materialize", "schedule": "daily 03:00"}, (
        f"the registry would refuse this: {res['bodies'][0]}"
    )
    panel = (JS / "linked_apps_panel.js").read_text(encoding="utf-8")
    assert "data-la-refresh" not in panel, (
        "the refresh switch is back — nothing refreshes these tools, so it promises what "
        "the product does not do"
    )


def test_an_existing_app_grant_is_read_as_done():
    """Every pair 409s — the apps are already granted to that group. That is
    the state the admin asked for, so Done must complete."""
    script = _apps_script(
        r"""  'POST /api/admin/grants': [
    { status: 409, body: { detail: 'Grant already exists for this group/resource_type/resource_id' } },
    { status: 409, body: { detail: 'Grant already exists for this group/resource_type/resource_id' } },
  ],""",
        r"""
  fire('click', { id: 'mcp-done' });
  for (let i = 0; i < 14; i++) await flush();
  process.stdout.write(JSON.stringify({
    href: window.location.href,
    grants: CALLS.filter((c) => c === 'POST /api/admin/grants').length,
    all: CALLS,
  }));
""",
    )
    res = _node(script)
    assert res["grants"] == 2, f"both apps should have been granted: {res['all']}"
    assert res["href"] == "/admin/mcp-sources/s1", (
        f"409s were read as failures: href={res['href']!r}, calls={res['all']}"
    )


def test_a_failed_app_grant_is_named_and_keeps_the_admin_on_the_page():
    """A grant that did not land is the difference between "shared" and
    "invisible", and the admin is the only one who can retry it — so it is
    reported here rather than carried silently through a redirect."""
    script = _apps_script(
        r"""  'POST /api/admin/grants': [
    { status: 200, body: {} },
    { status: 500, body: { detail: 'boom' } },
    { status: 409, body: { detail: 'Grant already exists' } },
    { status: 200, body: {} },
  ],""",
        r"""
  fire('click', { id: 'mcp-done' });
  for (let i = 0; i < 14; i++) await flush();
  const hrefAfterPartial = window.location.href;
  fire('click', { id: 'mcp-done' });
  for (let i = 0; i < 14; i++) await flush();
  process.stdout.write(JSON.stringify({
    hrefAfterPartial,
    href: window.location.href,
    grants: CALLS.filter((c) => c === 'POST /api/admin/grants').length,
    all: CALLS,
  }));
""",
    )
    res = _node(script)
    assert res["hrefAfterPartial"] == "", f"a partial failure navigated away: {res['all']}"
    assert res["href"] == "/admin/mcp-sources/s1", (
        f"the retry never finished: href={res['href']!r}, calls={res['all']}"
    )



# ── MCP source: the tool toggles have to reach the registry ──────────────────
#
# A source with no `tool_registry` rows exposes NOTHING — the MCP server builds
# its list from `list_by_mode('passthrough', enabled_only=True)`
# (app/api/mcp/tools_generator.py) — and the builder's Save registered the
# source, the secret and the grants, but never a tool. So it handed back a
# registered, granted source with zero callable tools, while the Tools panel
# invited the admin to "turn off anything agents should not call" and counted
# "N of M" on. The toggles described an outcome Save did not produce, in both
# directions: the ones left on were not callable, and the ones turned off were
# no less callable than the rest.
#
# Raised by a review bot on PR #1679 — reported as "every introspected tool
# stays callable regardless", which is the inverse of what was happening. The
# tests below pin the behaviour rather than that description.


def _mcp_script(responses_js: str, drive_js: str) -> str:
    """Open the MCP builder, introspect two tools, then run `drive_js`."""
    return (
        _HARNESS
        + _load(SHELL, APPS_PANEL, MCP)
        + "installFetch("
        + responses_js
        + """);
window.AgnesMcpBuilder.open({ mount });
(async () => {
  await flush();
  fire('input', { 'data-mcp-field': 'name', value: 'CRM' });
  fire('input', { 'data-mcp-field': 'url', value: 'https://mcp.example.com/sse' });
  // Introspect, so there are tools to toggle at all.
  fire('click', { 'data-mcp-check': '1' });
  for (let i = 0; i < 8; i++) await flush();
"""
        + drive_js
        + """
})();
"""
    )


_TWO_TOOLS = """{
  'GET /api/admin/groups': { status: 200, body: [{ id: 'g1', name: 'Analysts' }] },
  'POST /api/admin/mcp-sources/preview-introspect': {
    status: 200,
    body: { tools: [
      { name: 'search_crm', description: 'Search', input_schema: { type: 'object' } },
      { name: 'delete_account', description: 'Danger' },
    ] },
  },
  'POST /api/admin/mcp-sources': [{ status: 201, body: { id: 'src-t' } }],
}"""


def test_the_tools_left_on_are_registered_so_an_agent_can_call_them():
    res = _node(
        _mcp_script(
            _TWO_TOOLS,
            r"""
  fire('click', { id: 'mcp-save' });
  for (let i = 0; i < 16; i++) await flush();
  process.stdout.write(JSON.stringify({
    all: CALLS,
    tools: CALLS.filter((c) => c === 'POST /api/admin/mcp-tools').length,
    bodies: BODIES['POST /api/admin/mcp-tools'] || [],
    href: window.location.href,
  }));
""",
        )
    )
    assert res["tools"] == 2, f"Save registered no tools — the source is callable by nobody: {res['all']}"
    names = sorted(b["original_name"] for b in res["bodies"])
    assert names == ["delete_account", "search_crm"]
    for body in res["bodies"]:
        assert body["mode"] == "passthrough", "the toggle is about being callable by an agent"
        assert body["enabled"] is True
        assert body["source_id"] == "src-t"
        assert body["tool_id"] == "src-t__" + body["original_name"], (
            "tool_id must be the deterministic composite, or a second Save duplicates the row"
        )
    schema = [b for b in res["bodies"] if b["original_name"] == "search_crm"][0]["input_schema"]
    assert schema == {"type": "object"}, (
        "the introspected input schema must survive to the registry — it is how an agent knows the tool's arguments"
    )
    assert res["href"] == "/admin/mcp-sources/src-t"


def test_a_tool_toggled_off_is_never_registered():
    res = _node(
        _mcp_script(
            _TWO_TOOLS,
            r"""
  fire('click', { 'data-mcp-tool': 'delete_account' });
  for (let i = 0; i < 4; i++) await flush();
  fire('click', { id: 'mcp-save' });
  for (let i = 0; i < 16; i++) await flush();
  process.stdout.write(JSON.stringify({
    all: CALLS,
    bodies: BODIES['POST /api/admin/mcp-tools'] || [],
  }));
""",
        )
    )
    names = [b["original_name"] for b in res["bodies"]]
    assert names == ["search_crm"], (
        f"a tool the admin switched off was registered anyway (or the on one was not): {names}"
    )


def test_the_tools_are_registered_before_the_groups_are_granted():
    """Order matters for what the admin is told they did: a group pointed at a
    source with no callable tools has been given nothing."""
    res = _node(
        _mcp_script(
            _TWO_TOOLS,
            r"""
  fire('click', { 'data-mcp-openpick': '1' });
  for (let i = 0; i < 8; i++) await flush();
  fire('click', { 'data-mcp-pick': 'g1' });
  for (let i = 0; i < 4; i++) await flush();
  fire('click', { id: 'mcp-save' });
  for (let i = 0; i < 20; i++) await flush();
  process.stdout.write(JSON.stringify({ all: CALLS }));
""",
        )
    )
    calls = res["all"]
    assert "POST /api/admin/mcp-tools" in calls, f"no tool was registered at all: {calls}"
    assert any(c.endswith("/grants") for c in calls), f"no grant was made: {calls}"
    first_tool = calls.index("POST /api/admin/mcp-tools")
    first_grant = next(i for i, c in enumerate(calls) if c.endswith("/grants"))
    assert first_tool < first_grant, f"granted access before there was anything to call: {calls}"


def test_a_second_save_does_not_duplicate_the_tool_rows():
    """The 409 an existing tool_id answers is the end state Save wanted, not a
    failure — the same rule the grant step follows. A retry after a later step
    failed must finish, not stall on the tools it already registered."""
    res = _node(
        _mcp_script(
            """{
  'GET /api/admin/groups': { status: 200, body: [{ id: 'g1', name: 'Analysts' }] },
  'POST /api/admin/mcp-sources/preview-introspect': {
    status: 200, body: { tools: [{ name: 'search_crm', description: 'Search' }] },
  },
  'POST /api/admin/mcp-sources': [{ status: 201, body: { id: 'src-r' } }],
  // The tool registers fine; the GRANT fails once, so Save is retried with
  // the tool already in place — the retry's re-register answers 409.
  'POST /api/admin/mcp-tools': [{ status: 201, body: {} }, { status: 409, body: { detail: 'tool_id_exists' } }],
  'POST /api/admin/mcp-sources/src-r/grants': [{ status: 500, body: { detail: 'boom' } }],
}""",
            r"""
  fire('click', { 'data-mcp-openpick': '1' });
  for (let i = 0; i < 8; i++) await flush();
  fire('click', { 'data-mcp-pick': 'g1' });
  for (let i = 0; i < 4; i++) await flush();
  fire('click', { id: 'mcp-save' });
  for (let i = 0; i < 20; i++) await flush();
  fire('click', { id: 'mcp-save' });
  for (let i = 0; i < 20; i++) await flush();
  process.stdout.write(JSON.stringify({
    all: CALLS,
    creates: CALLS.filter((c) => c === 'POST /api/admin/mcp-sources').length,
    tools: CALLS.filter((c) => c === 'POST /api/admin/mcp-tools').length,
    href: window.location.href,
  }));
""",
        )
    )
    assert res["creates"] == 1, f"the retry re-registered the source: {res['all']}"
    # Two attempts, one tool: registered on the first pass, re-offered on the
    # retry and answered 409. Asserted so this test cannot pass against a Save
    # that registers no tools at all — the state it was written for.
    assert res["tools"] == 2, f"the tool was not registered on both passes: {res['all']}"
    assert res["href"] == "/admin/mcp-sources/src-r", (
        f"the resumed save stalled on a 409 from a tool it had already registered: {res['all']}"
    )


# ── MCP source: a 409 is not one thing ───────────────────────────────────────
#
# `POST /api/admin/mcp-sources/{id}/grants` answers 409 `no_tools_registered`
# when the source has no ENABLED tool row — nothing was granted, the group has
# no access, and re-enabling the tools later does not go back and grant anyone.
# Already-granted is NOT a 409 on that endpoint at all: it answers 200 with an
# `already` count.
#
# So the blanket "409 means already granted" in `grantGroup` reported a failed
# access change as success, and the builder can cause exactly that state: turn
# every introspected tool off, pick a group, Save. Raised by a review bot on
# PR #1679 against the tool-registration fix.
#
# Sibling checked: the linked-apps builder's `grantPair` swallows 409 against
# `/api/admin/grants`, whose single 409 genuinely is "grant already exists"
# (app/api/access.py). That one is correct and stays.


def test_a_no_tools_registered_409_is_a_failure_not_a_shrug():
    """Every tool off, a group picked: the grant cannot succeed, and Save must
    say so rather than redirecting as if access had been granted."""
    res = _node(
        _mcp_script(
            """{
  'GET /api/admin/groups': { status: 200, body: [{ id: 'g1', name: 'Analysts' }] },
  'POST /api/admin/mcp-sources/preview-introspect': {
    status: 200, body: { tools: [{ name: 'search_crm', description: 'Search' }] },
  },
  'POST /api/admin/mcp-sources': [{ status: 201, body: { id: 'src-n' } }],
  'POST /api/admin/mcp-sources/src-n/grants': {
    status: 409,
    body: { detail: { error: 'no_tools_registered', message: "Source 'src-n' has no enabled tools to grant. Register them first, then grant." } },
  },
}""",
            r"""
  // Turn the only tool OFF, then pick a group anyway.
  fire('click', { 'data-mcp-tool': 'search_crm' });
  for (let i = 0; i < 4; i++) await flush();
  fire('click', { 'data-mcp-openpick': '1' });
  for (let i = 0; i < 8; i++) await flush();
  fire('click', { 'data-mcp-pick': 'g1' });
  for (let i = 0; i < 4; i++) await flush();
  fire('click', { id: 'mcp-save' });
  for (let i = 0; i < 20; i++) await flush();
  process.stdout.write(JSON.stringify({
    all: CALLS,
    tools: CALLS.filter((c) => c === 'POST /api/admin/mcp-tools').length,
    href: window.location.href,
    err: (mount.innerHTML.match(/no enabled tools to grant/) || [''])[0],
  }));
""",
        )
    )
    assert res["tools"] == 0, "a tool the admin switched off was registered anyway"
    assert res["href"] == "", (
        f"Save redirected as though the group had been granted access it does not have: {res['all']}"
    )
    assert res["err"] == "no enabled tools to grant", (
        "the server's own sentence — which names the fix — never reached the admin"
    )


def test_an_ordinary_409_is_still_read_as_already_granted():
    """The narrowing must not undo the earlier fix: a 409 that is NOT
    `no_tools_registered` still counts as the end state Save wanted, so a
    resumed Save cannot stall on a grant it already made."""
    res = _node(
        _mcp_script(
            """{
  'GET /api/admin/groups': { status: 200, body: [{ id: 'g1', name: 'Analysts' }] },
  'POST /api/admin/mcp-sources/preview-introspect': {
    status: 200, body: { tools: [{ name: 'search_crm', description: 'Search' }] },
  },
  'POST /api/admin/mcp-sources': [{ status: 201, body: { id: 'src-a' } }],
  'POST /api/admin/mcp-sources/src-a/grants': {
    status: 409, body: { detail: 'Grant already exists for this group/resource_type/resource_id' },
  },
}""",
            r"""
  fire('click', { 'data-mcp-openpick': '1' });
  for (let i = 0; i < 8; i++) await flush();
  fire('click', { 'data-mcp-pick': 'g1' });
  for (let i = 0; i < 4; i++) await flush();
  fire('click', { id: 'mcp-save' });
  for (let i = 0; i < 20; i++) await flush();
  process.stdout.write(JSON.stringify({ all: CALLS, href: window.location.href }));
""",
        )
    )
    assert res["href"] == "/admin/mcp-sources/src-a", f"an already-granted 409 broke the save again: {res['all']}"


def test_the_structured_detail_survives_to_the_caller():
    """`grantGroup` decides on `detail.error`, so readJson must carry the
    object rather than flattening it to a sentence — matching on message text
    is what breaks the day the wording changes."""
    src = MCP.read_text(encoding="utf-8")
    assert "e.detail = d;" in src
    assert "kind !== 'no_tools_registered'" in src, (
        "the 409 swallow must exclude no_tools_registered by its stable error code"
    )


# ── Editing a registered source: save sends the DIFFERENCE ──────────────────


def _edit_script(tail: str) -> str:
    """Open the builder on a source that already exists and drive one edit.

    Registering was a builder and revising was the detail page's own form, so
    the connection, the tool curation and the grants were entered in one
    vocabulary and changed in another. Now it is one surface — and the save it
    performs cannot be the create path's "post everything", because a tool
    turned off has to be DELETED and a group unpicked has to be REVOKED.
    """
    return (
        _HARNESS
        + _load(SHELL, APPS_PANEL, MCP)
        + r"""
installFetch({
  'GET /api/admin/groups': { status: 200, body: [
    { id: 'g1', name: 'Analysts' }, { id: 'g2', name: 'Finance' },
  ] },
  'GET /api/admin/mcp-sources/s1': { status: 200, body: {
    id: 's1', name: 'acme_crm', transport: 'http', url: 'https://mcp.example.com/sse',
    auth_method: '', auth_secret_env: '', scope: 'shared',
    tools: [
      { tool_id: 's1__search', original_name: 'search', description: 'read', mutating: false },
      { tool_id: 's1__write', original_name: 'write_row', description: 'writes', mutating: true },
    ],
    grants: ['g2'], partial_grants: [],
  } },
});
window.AgnesMcpBuilder.open({ mount, editSourceId: 's1' });
(async () => {
  for (let i = 0; i < 10; i++) await flush();
"""
        + tail
        + r"""
})();
"""
    )


def test_the_loaded_source_is_the_panel_and_the_baseline():
    """Both, from one read: what the admin sees, and what save compares to."""
    res = _node(_edit_script(r"""
  process.stdout.write(JSON.stringify({ all: CALLS }));
"""))
    assert "GET /api/admin/mcp-sources/s1" in res["all"], f"the source was never loaded: {res['all']}"
    # The group list is fetched too — a grant is stored as an id, and a chip
    # reading "g2" is not access an admin can check at a glance.
    assert "GET /api/admin/groups" in res["all"], "grants would render as raw ids"


def test_a_tool_turned_off_is_disabled_not_deleted():
    """Off has to take the tool out of the callable set — and nothing more.

    It used to DELETE the registration. The toggle reads "✓ On / Off", which
    is the vocabulary of a reversible switch, and the source's detail page
    offers exactly that for the same row; deleting instead threw away the
    exposed-name override, the description and the input schema, with no
    confirmation and no undo, for an admin doing the cautious thing after
    seeing a tool marked "writes".
    """
    res = _node(_edit_script(r"""
  fire('click', { 'data-mcp-tool': 'write_row' });
  for (let i = 0; i < 4; i++) await flush();
  fire('click', { id: 'mcp-save' });
  for (let i = 0; i < 16; i++) await flush();
  process.stdout.write(JSON.stringify({
    all: CALLS, href: window.location.href,
    body: (BODIES['PUT /api/admin/mcp-tools/s1__write'] || [])[0] || null,
  }));
"""))
    assert not [c for c in res["all"] if c.startswith("DELETE /api/admin/mcp-tools")], (
        f"turning a tool off still destroys its registration: {res['all']}"
    )
    assert res["body"] == {"enabled": False}, f"the tool was not disabled: {res['body']}"
    assert "PUT /api/admin/mcp-tools/s1__search" not in res["all"], "it touched a tool that was left on"
    assert res["href"] == "/admin/mcp-sources/s1", f"the save never finished: {res['all']}"


def _mixed_source_script(tail: str) -> str:
    """A source whose two tools disagree: one enabled, one deliberately not."""
    return (
        _HARNESS
        + _load(SHELL, APPS_PANEL, MCP)
        + r"""
installFetch({
  'GET /api/admin/groups': { status: 200, body: [{ id: 'g1', name: 'Analysts' }] },
  'GET /api/admin/mcp-sources/s1': { status: 200, body: {
    id: 's1', name: 'acme_crm', transport: 'http', url: 'https://mcp.example.com/sse',
    auth_method: '', auth_secret_env: '', scope: 'shared',
    tools: [
      { tool_id: 's1__search', original_name: 'search', mutating: false, enabled: true },
      { tool_id: 's1__danger', original_name: 'delete_customer', mutating: true, enabled: false },
    ],
    grants: [], partial_grants: [],
  } },
});
window.AgnesMcpBuilder.open({ mount, editSourceId: 's1' });
(async () => {
  for (let i = 0; i < 10; i++) await flush();
"""
        + tail
        + r"""
})();
"""
    )


def test_an_untouched_panel_does_not_rewrite_the_tool_rows():
    """The loader marked every returned tool enabled, so a tool an admin had
    deliberately switched off rendered "✓ On" — with a "writes" badge beside
    it, on the one that matters most. Opening and saving would then have
    silently re-enabled it."""
    res = _node(_mixed_source_script(r"""
  fire('click', { id: 'mcp-save' });
  for (let i = 0; i < 16; i++) await flush();
  process.stdout.write(JSON.stringify({
    tools: CALLS.filter((c) => c.indexOf('/api/admin/mcp-tools') >= 0),
  }));
"""))
    assert res["tools"] == [], f"opening and saving rewrote the tool rows: {res['tools']}"


def test_switching_a_disabled_tool_back_on_re_enables_it():
    """The other direction of the same toggle — and it must not create a
    second registration for a row that already exists."""
    res = _node(_mixed_source_script(r"""
  fire('click', { 'data-mcp-tool': 'delete_customer' });
  for (let i = 0; i < 4; i++) await flush();
  fire('click', { id: 'mcp-save' });
  for (let i = 0; i < 16; i++) await flush();
  process.stdout.write(JSON.stringify({
    body: (BODIES['PUT /api/admin/mcp-tools/s1__danger'] || [])[0] || null,
    posts: CALLS.filter((c) => c === 'POST /api/admin/mcp-tools').length,
  }));
"""))
    assert res["body"] == {"enabled": True}, f"it was not re-enabled: {res['body']}"
    assert res["posts"] == 0, "a registered-but-disabled tool was re-created instead of re-enabled"


def test_a_group_unpicked_is_revoked():
    res = _node(_edit_script(r"""
  fire('click', { 'data-mcp-unpick': 'g2' });
  for (let i = 0; i < 4; i++) await flush();
  fire('click', { id: 'mcp-save' });
  for (let i = 0; i < 16; i++) await flush();
  process.stdout.write(JSON.stringify({ all: CALLS }));
"""))
    assert "DELETE /api/admin/mcp-sources/s1/grants/g2" in res["all"], (
        f"unpicking a group left its access live: {res['all']}"
    )


def test_an_unchanged_edit_registers_nothing_twice():
    """Saving without changing anything must not re-register the tools that
    are already there — the create path's POST per tool would duplicate them."""
    res = _node(_edit_script(r"""
  fire('click', { id: 'mcp-save' });
  for (let i = 0; i < 16; i++) await flush();
  process.stdout.write(JSON.stringify({ all: CALLS }));
"""))
    assert "POST /api/admin/mcp-tools" not in res["all"], f"tools were re-registered: {res['all']}"
    assert "POST /api/admin/mcp-sources" not in res["all"], f"a second source was created: {res['all']}"
    assert "PUT /api/admin/mcp-sources/s1" in res["all"], "the source itself was never updated"


def test_editing_never_overwrites_the_draft_slot():
    """`persistDraft` is keyed on one localStorage entry shared with the create
    path — writing an opened source into it would replace whatever half-typed
    connection the admin had, and offer to resume one already registered."""
    src = MCP.read_text(encoding="utf-8")
    body = re.search(r"function persistDraft\(\) \{(.*?)\n  \}", src, re.S)
    assert body and "if (editing) return" in body.group(1), (
        "an edit is being parked as a draft"
    )
