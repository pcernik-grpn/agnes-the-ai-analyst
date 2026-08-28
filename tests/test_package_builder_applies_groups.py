"""The package builder's group proposals have to reach the panel.

Found by a review bot on PR #1679 and confirmed: `applyPatch` ticked groups by
querying an attribute nothing in the codebase emits — a group row is
`[data-group-id]` wrapping an unlabelled checkbox. The loop matched nothing,
silently.

Two further links in the same chain, each enough on its own to lose the
proposal:

  * group rows are hydrated LAZILY, only when the admin opens the access
    disclosure, so in create mode — the builder's own mode — no rows exist when
    a patch arrives, right selector or not;
  * `sendTurn` reported `groups` from `st.grantsOriginal`, the edit-mode
    baseline of already-saved grants. It is empty in create mode, so the
    conversation never saw what was ticked and kept re-proposing groups the
    admin had already accepted.

`chosenGrants()` — what Save reads — was correct throughout, which is why the
break was invisible: tick a box by hand and the grant is created; let the
builder propose it and nothing happens.

The behavioural test runs the SHIPPED bytes: the drawer builds itself through
`innerHTML`, which a node shim cannot parse and there is no jsdom here, so the
whole module cannot be driven the way `test_builder_save_is_resumable.py`
drives the MCP builder. Transcribing the branch into the harness instead would
measure the transcription — the exact failure this branch already hit once, two
node tests green against broken code. So the branch is SLICED OUT of
`package_drawer.js` and executed as-is: change the selector back and the test
goes red, because it is running that line.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "app" / "web" / "static" / "js" / "components"
DRAWER = JS / "package_drawer.js"

_GROUPS_BRANCH = "if (Array.isArray(patch.groups) && patch.groups.length) {"


def _run(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _strip_comments(src: str) -> str:
    """JS source with `//` and `/* */` removed, quotes respected.

    An assertion about what the CODE does must not be satisfiable — or broken —
    by a comment. The note inside `applyPatch` explains the dead selector by
    name, so a blanket `"..." not in src` would fail on the very explanation of
    the fix.
    """
    out: list[str] = []
    i, n = 0, len(src)
    quote: str | None = None
    while i < n:
        c = src[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "\"'`":
            quote = c
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            end = src.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _groups_branch(src: str) -> str:
    """The `patch.groups` branch of `applyPatch`, verbatim, braces balanced."""
    start = src.index(_GROUPS_BRANCH)
    body = src.index("{", start)
    depth, i, n = 0, body, len(src)
    quote: str | None = None
    while i < n:
        c = src[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "\"'`":
            quote = c
        elif c == "/" and src[i + 1 : i + 2] == "/":
            i = src.find("\n", i)
            continue
        elif c == "/" and src[i + 1 : i + 2] == "*":
            i = src.index("*/", i + 2) + 2
            continue
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[body + 1 : i]
        i += 1
    raise AssertionError("unbalanced braces in applyPatch's groups branch")


def test_the_dead_selector_is_gone() -> None:
    """The narrowest statement of the bug: nothing emits `data-pdw-group`, so
    any query for it matches nothing. Cheap to assert and it is what regressed.

    Asserted against the code with comments stripped — the note explaining this
    history names the attribute, and an explanation must not fail its own test.
    """
    code = _strip_comments(DRAWER.read_text(encoding="utf-8"))
    assert "data-pdw-group" not in code, (
        "applyPatch queries an attribute nothing renders — group rows are [data-group-id]"
    )
    assert "querySelectorAll('[data-group-id]')" in code


def test_apply_patch_waits_for_the_rows_before_ticking() -> None:
    """Create mode has no group rows until `hydrateGroups` lands, so ticking
    has to be chained to it rather than run inline."""
    code = _strip_comments(DRAWER.read_text(encoding="utf-8"))
    assert "Promise.resolve(hydrateGroups()).then(" in code, (
        "applyPatch must wait for the rows; in create mode they do not exist when the patch arrives"
    )
    assert "if (st.groupsLoaded) return Promise.resolve();" in code, (
        "hydrateGroups must return a promise for that chain to mean anything"
    )


def test_the_turn_reports_the_groups_on_screen_not_the_saved_baseline() -> None:
    code = _strip_comments(DRAWER.read_text(encoding="utf-8"))
    assert "groups: chosenGrants().map(" in code, (
        "sendTurn sent grantsOriginal (the edit-mode baseline, empty in create "
        "mode), so the conversation never saw what was ticked"
    )


def test_a_group_id_holding_a_quote_cannot_break_the_lookup() -> None:
    """Group ids are server data. Interpolating one into a selector is how a
    name with a quote in it becomes a thrown exception mid-patch."""
    code = _strip_comments(DRAWER.read_text(encoding="utf-8"))
    assert "'[data-group-id=\"' +" not in code, (
        "a group id is interpolated into a selector — compare the attribute "
        "instead, there is no CSS.escape to lean on here"
    )


def test_the_shipped_branch_ticks_the_proposed_group_and_opens_the_disclosure() -> None:
    """The behaviour, on the real bytes: a patch proposing a group ends with
    that group's checkbox checked and the access disclosure open.

    `hydrateGroups` is the seam — it stands in for the fetch + render, and
    installs the rows the server would have rendered AFTER the patch arrives,
    which is the create-mode condition the bug depended on.
    """
    branch = _groups_branch(DRAWER.read_text(encoding="utf-8"))
    script = (
        r"""
function el(extra) {
  return Object.assign({
    checked: false, open: false,
    getAttribute: () => null,
    querySelector: () => null, querySelectorAll: () => [],
  }, extra || {});
}
// A group row as hydrateGroups renders it, reduced to what applyPatch reads.
function groupRow(id) {
  const box = el({ checked: false });
  const row = el({
    getAttribute: (k) => (k === 'data-group-id' ? id : null),
    querySelector: (sel) => (sel === 'input[type="checkbox"]' ? box : null),
  });
  row._box = box;
  return row;
}
const rows = [];
const els = {
  groups: el({ querySelectorAll: (sel) => (sel === '[data-group-id]' ? rows : []) }),
  access: el({ open: false }),
};
// Lazy on purpose: no rows exist when the patch arrives, as in create mode.
let hydrated = 0;
function hydrateGroups() {
  hydrated += 1;
  return Promise.resolve().then(() => {
    rows.push(groupRow('g-sales'), groupRow('g-finance'));
  });
}
const patch = { groups: ['g-sales', 'g-sales'] };  // duplicate: must tick once
(async () => {
"""
        + branch
        + r"""
  for (let i = 0; i < 6; i++) await new Promise((r) => setTimeout(r, 0));
  process.stdout.write(JSON.stringify({
    hydrated,
    checked: rows.filter((r) => r._box.checked).map((r) => r.getAttribute('data-group-id')),
    disclosureOpen: els.access.open,
  }));
})();
"""
    )
    res = _run(script)
    assert res["hydrated"] == 1, "the rows were never fetched — the patch had nothing to tick"
    assert res["checked"] == ["g-sales"], f"the proposed group was not ticked (or the wrong one was): {res['checked']}"
    assert res["disclosureOpen"] is True, "ticked silently inside a collapsed disclosure"


def test_a_patch_proposing_nothing_new_leaves_the_disclosure_shut() -> None:
    """The other half of the same rule: opening the access section is how the
    panel says "who can reach this just changed". A patch that changes nothing
    must not say it."""
    branch = _groups_branch(DRAWER.read_text(encoding="utf-8"))
    script = (
        r"""
function el(extra) {
  return Object.assign({
    checked: false, open: false,
    getAttribute: () => null,
    querySelector: () => null, querySelectorAll: () => [],
  }, extra || {});
}
const box = el({ checked: true });   // already ticked by the admin
const row = el({
  getAttribute: (k) => (k === 'data-group-id' ? 'g-sales' : null),
  querySelector: (sel) => (sel === 'input[type="checkbox"]' ? box : null),
});
const rows = [row];
const els = {
  groups: el({ querySelectorAll: (sel) => (sel === '[data-group-id]' ? rows : []) }),
  access: el({ open: false }),
};
function hydrateGroups() { return Promise.resolve(); }
const patch = { groups: ['g-sales'] };
(async () => {
"""
        + branch
        + r"""
  for (let i = 0; i < 6; i++) await new Promise((r) => setTimeout(r, 0));
  process.stdout.write(JSON.stringify({ disclosureOpen: els.access.open }));
})();
"""
    )
    assert _run(script)["disclosureOpen"] is False, "the disclosure opened for a patch that ticked nothing"
