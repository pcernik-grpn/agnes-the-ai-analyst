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
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "app" / "web" / "static" / "js" / "components"
SHELL = JS / "builder_shell.js"
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
global.window = { location: { href: '' }, document: global.document };
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
    CALLS.push(method + ' ' + url);
    const entry = responses[method + ' ' + url];
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
        + _load(SHELL, MCP)
        + r"""
installFetch({
  'GET /api/admin/groups': { status: 200, body: [{ id: 'g1', name: 'Analysts' }] },
  // The create succeeds and hands back an id. Keyed exactly, so the
  // builder's own opening turn (POST .../builder/turn) cannot consume it.
  'POST /api/admin/mcp-sources': [{ status: 201, body: { id: 'src-1' } }],
  // ...and the FIRST grant attempt fails. The retry's grant is unqueued, so
  // it answers 200 — the resumed save must then finish.
  'POST /api/admin/mcp-sources/src-1/grants': [{ status: 500, body: { detail: 'boom' } }],
});
window.AgnesMcpBuilder.open({ mount });
(async () => {
  await flush();
  // Type a name and a url, the two things canSave() needs.
  fire('input', { 'data-mcp-field': 'name', value: 'CRM' });
  fire('input', { 'data-mcp-field': 'url', value: 'https://mcp.example.com/sse' });
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
        + _load(SHELL, MCP)
        + r"""
installFetch({
  'GET /api/admin/groups': { status: 200, body: [{ id: 'g1', name: 'Analysts' }] },
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


# ── Linked apps: 409 is done, and one failure does not hide the rest ─────────


def _linked_script(grant_responses: str, tail: str) -> str:
    """Drive the linked-apps builder as far as Save.

    The apps come from the fetch step (a Keboola MCP source + its lister tool),
    so the harness answers those three reads before Save has anything to grant.
    """
    return (
        _HARNESS
        + _load(SHELL, LINKED)
        + r"""
installFetch({
  'GET /api/admin/mcp-sources': { status: 200, body: [
    { id: 's1', name: 'keboola', url: 'https://connection.example.com/mcp', enabled: true },
  ] },
  // One lister tool, so `loadTools` picks it without the admin choosing.
  'GET /api/admin/mcp-tools?source_id=s1': { status: 200, body: [
    { id: 't1', tool_id: 't1', source_id: 's1', name: 'list_data_apps', enabled: true },
  ] },
  'GET /api/admin/groups': { status: 200, body: [
    { id: 'g1', name: 'Analysts' }, { id: 'g2', name: 'Finance' },
  ] },
  'GET /api/data-apps?kind=linked&source=s1': { status: 200, body: { apps: [
    { id: 'app-a', name: 'Churn' }, { id: 'app-b', name: 'Revenue' },
  ] } },
"""
        + grant_responses
        + r"""
});
window.AgnesLinkedAppsBuilder.open({ mount });
(async () => {
  await flush();
  // Pick the source (which loads and auto-picks its one lister tool), then
  // fetch the catalogue — every app comes back chosen by default.
  fire('click', { 'data-la-source': 's1' });
  for (let i = 0; i < 8; i++) await flush();
  fire('click', { 'data-la-fetch': '1' });
  for (let i = 0; i < 12; i++) await flush();
"""
        + tail
        + r"""
})();
"""
    )


def test_linked_apps_treats_an_existing_grant_as_done():
    """Every pair 409s — the apps are already granted to those groups. That is
    the state the admin asked for, so Save must complete."""
    script = _linked_script(
        r"""  'POST /api/admin/grants': [
    { status: 409, body: { detail: 'Grant already exists for this group/resource_type/resource_id' } },
    { status: 409, body: { detail: 'Grant already exists for this group/resource_type/resource_id' } },
  ],""",
        r"""
  // Open the picker before picking: `toggleGroup` resolves the id against the
  // loaded group list, and the picker is what loads it.
  fire('click', { 'data-la-openpick': '1' });
  for (let i = 0; i < 8; i++) await flush();
  fire('click', { 'data-la-pick': 'g1' });
  for (let i = 0; i < 4; i++) await flush();
  fire('click', { id: 'la-save' });
  for (let i = 0; i < 12; i++) await flush();
  process.stdout.write(JSON.stringify({
    href: window.location.href,
    grants: CALLS.filter((c) => c === 'POST /api/admin/grants').length,
    all: CALLS,
  }));
""",
    )
    res = _node(script)
    assert res["href"] == "/library?kind=data_app", (
        f"409s were read as failures: href={res['href']!r}, calls={res['all']}"
    )


def test_linked_apps_retry_only_repeats_what_is_still_missing():
    """One pair fails for real. The save must report which, keep the admin on
    the page, and — on retry — reach the success path once the 409s from the
    first attempt are counted as done."""
    script = _linked_script(
        r"""  'POST /api/admin/grants': [
    { status: 200, body: {} },
    { status: 500, body: { detail: 'boom' } },
    { status: 409, body: { detail: 'Grant already exists' } },
    { status: 200, body: {} },
  ],""",
        r"""
  // Open the picker before picking: `toggleGroup` resolves the id against the
  // loaded group list, and the picker is what loads it.
  fire('click', { 'data-la-openpick': '1' });
  for (let i = 0; i < 8; i++) await flush();
  fire('click', { 'data-la-pick': 'g1' });
  for (let i = 0; i < 4; i++) await flush();
  fire('click', { id: 'la-save' });
  for (let i = 0; i < 12; i++) await flush();
  const hrefAfterPartial = window.location.href;
  fire('click', { id: 'la-save' });
  for (let i = 0; i < 12; i++) await flush();
  process.stdout.write(JSON.stringify({
    hrefAfterPartial,
    href: window.location.href,
    grants: CALLS.filter((c) => c === 'POST /api/admin/grants').length,
    all: CALLS,
  }));
""",
    )
    res = _node(script)
    assert res["hrefAfterPartial"] == "", "a partial failure navigated away, so the admin never saw which pairs failed"
    assert res["href"] == "/library?kind=data_app", (
        f"the retry could not finish: href={res['href']!r}, calls={res['all']}"
    )
