"""Ratchet: every key ``app/api/admin_sharepoint.py`` writes into a SharePoint
connection's ``config`` must be listed in ``SHAREPOINT_SERVER_WRITTEN_CONFIG_KEYS``,
so ``app/api/admin_source_connections.py::update_connection`` (the generic,
wholesale-``config``-replacing editor) carries it forward instead of silently
erasing it.

Why this exists: the SAME bug landed twice in one day (2026-08-29) before this
ratchet did. First ``config.scopes`` (the connect wizard's confirmed-scope
rows, including the anonymize-in-front pipeline's only opt-in) was found to be
wiped by an ordinary edit through the generic connection editor — the editor's
form never renders that field, so an unrelated field on the SAME endpoint
silently erased it. That was fixed with an explicit carry-forward. Hours
later, TCRD-226 added a SECOND server-written key, ``config.extraction`` (the
in-Agnes extraction schedule's own dispatch bookkeeping), to the exact same
``config`` column — and the fix for the first key did not, by itself, cover
the second: a literal tuple has to be remembered by every future author, and
nothing failed until an operator's edit silently reset the second key too. A
hand-maintained list is a trap with a longer fuse, not a fix.

This test closes that gap MECHANICALLY rather than by review discipline: it
statically scans ``app/api/admin_sharepoint.py`` for the actual write sites —
every literal-string key a function in that file assigns into the LOCAL
variable it later passes as ``config=`` to a ``....update(...)`` call — and
fails when that set disagrees with ``SHAREPOINT_SERVER_WRITTEN_CONFIG_KEYS``,
in EITHER direction (a real writer key missing from the list is the erasure
risk; a stale list entry with no real writer is dead weight worth noticing
too). The failure message names the exact fix.

What the detector recognizes (the four shapes current writers use):

* ``NAME = {**base, "KEY": value}`` — a dict literal with a ``**`` unpack
  plus an explicit literal-string key (``confirm_scope`` / ``remove_scope``'s
  shape).
* ``NAME["KEY"] = value`` — a subscript assignment with a literal-string key,
  on a local variable later passed as ``config=`` to a ``....update(...)``
  call.
* ``....config_patch(id, {"KEY": value, ...})`` — a literal dict passed
  DIRECTLY as the patch argument (positional or ``patch=``), no intermediate
  variable to track: ``config_patch`` re-reads the row fresh and merges this
  dict's top-level keys itself, so there is no separate ``config=``-bound
  local the first two shapes rely on.
* ``....merge_extraction(id, {...})`` — ANY call to this method is a write
  of the literal key ``"extraction"``, unconditionally: unlike
  ``config_patch``, the target key is not one of the call's own arguments
  at all, it is the method's own contract ("merge into ``config.
  extraction``"). ``_record_extraction_dispatch``'s current shape, since
  2026-09-07 — first moved off a whole-``config`` ``update()`` onto
  ``config_patch`` (closing one race), then onto this ONE-transaction
  nested merge (closing the race THAT left open — a separate ``get()``
  then ``config_patch()`` call pair still had a window for a concurrent
  ``request_stop`` to land in between).

The first two are scoped to the SAME local variable name that flows into a
``config=`` keyword in an ``....update(...)`` call within the SAME function
— not a blind, function-wide scan. That distinction is load-bearing: this
file also assigns into OTHER dicts that are not the connection's ``config``
at all (a response dict's ``result["hint"] = ...``, a scope row's own
``existing["anonymize"] = ...`` nested a level below the top-level ``config``
key). A naive "any literal-key assignment anywhere in the file" scan flags
those too — false positives that would make the ratchet cry wolf on every
routine change and get muted. Variable-scoping the match to what actually
reaches ``config=`` is what keeps it precise. The third shape needs no such
scoping: ``config_patch``'s own contract is "patch the top-level ``config``
of the row named by the id argument", so any literal-keyed dict passed to it
IS a config write by construction, whichever call site.

Deliberately scoped to ONE file, not a repo-wide scan: ``source_connections
.config`` also carries genuinely different, Keboola-only server-written keys
written by OTHER modules (``project_id``/``project_name`` in
``app/api/admin_source_connections.py::_record_project_identity``,
``pat_master_unobtainable`` in ``app/auth/keboola_provisioning.py``), each
with their OWN, separate carry-forward branch keyed on
``source_type == "keboola"``. A repo-wide version of this same scan would
need to group writers by ``source_type`` before comparing against ANY one
allowlist, which is a materially harder, more failure-prone problem this
pass does not attempt — scoping to the one file where SharePoint's keys are
actually defined (and where the coordinator's fix landed both times) is
precise where a broader scan would only be approximately right.

Known limitation, stated honestly rather than hidden: the per-function scope
this detector tracks does not distinguish a NESTED function from its
enclosing one (``ast.walk`` sees both), so a hypothetical future nested
closure could in principle misattribute a key across that boundary. No
function in this file is nested today, and the failure mode if one ever were
added incorrectly is a MISSING-key false negative on a pattern this codebase
does not currently use — not a false positive silently blocking real changes,
which is the risk this test is actually built to avoid.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ADMIN_SHAREPOINT_PATH = REPO_ROOT / "app" / "api" / "admin_sharepoint.py"


def config_writer_keys(tree: ast.Module) -> set[str]:
    """Every literal-string key written into a SharePoint connection's
    ``config`` by a function in ``tree`` — into the local variable it
    passes as ``config=`` to a ``....update(...)`` call, directly as a
    literal dict passed to ``....config_patch(...)``, or (unconditionally)
    ``"extraction"`` for any ``....merge_extraction(...)`` call. See the
    module docstring for the exact four shapes recognized and why the
    first two are scoped per-function to one variable name while the last
    two need no such scoping."""
    keys: set[str] = set()
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        # ....config_patch(id, {"KEY": value, ...}) — the literal dict IS
        # the config write, wherever it appears; no config=-bound local to
        # track first.
        for node in ast.walk(func):
            if not (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "config_patch"
            ):
                continue
            patch_dicts = [arg for arg in node.args if isinstance(arg, ast.Dict)]
            patch_dicts += [kw.value for kw in node.keywords if kw.arg == "patch" and isinstance(kw.value, ast.Dict)]
            for patch_dict in patch_dicts:
                for k in patch_dict.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        keys.add(k.value)

        # ....merge_extraction(id, {...}) — the method's own contract IS
        # "merge these keys into config.extraction", so any call to it is
        # a write of the literal key "extraction" by construction, whatever
        # its second argument looks like (unlike config_patch, there is no
        # dict-literal shape to inspect — the target key isn't an argument
        # at all, it's baked into the method name).
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "merge_extraction"
            ):
                keys.add("extraction")

        config_vars: set[str] = set()
        for node in ast.walk(func):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "update"):
                continue
            for kw in node.keywords:
                if kw.arg == "config" and isinstance(kw.value, ast.Name):
                    config_vars.add(kw.value.id)
        if not config_vars:
            continue

        for node in ast.walk(func):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                # NAME = {**base, "KEY": value}
                if isinstance(target, ast.Name) and target.id in config_vars and isinstance(node.value, ast.Dict):
                    for k in node.value.keys:
                        if isinstance(k, ast.Constant) and isinstance(k.value, str):
                            keys.add(k.value)
                # NAME["KEY"] = value
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id in config_vars
                    and isinstance(target.slice, ast.Constant)
                    and isinstance(target.slice.value, str)
                ):
                    keys.add(target.slice.value)
    return keys


# ---------------------------------------------------------------------------
# the ratchet
# ---------------------------------------------------------------------------


def test_admin_sharepoint_config_writes_match_the_declared_ratchet():
    from app.api.admin_sharepoint import SHAREPOINT_SERVER_WRITTEN_CONFIG_KEYS

    tree = ast.parse(ADMIN_SHAREPOINT_PATH.read_text())
    found = config_writer_keys(tree)
    declared = set(SHAREPOINT_SERVER_WRITTEN_CONFIG_KEYS)

    missing = sorted(found - declared)
    assert not missing, (
        f"app/api/admin_sharepoint.py writes {missing} into a SharePoint connection's "
        "config, but SHAREPOINT_SERVER_WRITTEN_CONFIG_KEYS (same file) does not list "
        f"{'it' if len(missing) == 1 else 'them'}. Add "
        f"{'it' if len(missing) == 1 else 'each'} to that tuple in the SAME change, "
        "or app/api/admin_source_connections.py::update_connection's generic editor "
        "will silently erase it on the next ordinary connection edit — this is the "
        "exact bug config.scopes and config.extraction both hit on 2026-08-29."
    )

    stale = sorted(declared - found)
    assert not stale, (
        f"SHAREPOINT_SERVER_WRITTEN_CONFIG_KEYS lists {stale}, but no writer in "
        "app/api/admin_sharepoint.py assigns it into config anymore (per this test's "
        "detector — see its module docstring for what it recognizes before deleting "
        "the entry). Remove the stale entry, or fix the writer if it is still there "
        "but no longer matches one of the two recognized shapes."
    )


# ---------------------------------------------------------------------------
# meta-tests: prove the detector actually catches violations, and does not
# cry wolf on the shapes that are NOT a config write.
# ---------------------------------------------------------------------------


def test_detector_flags_a_planted_dict_literal_violation():
    """A new writer using the `{**base, "KEY": value}` shape must be
    flagged — guards against the detector silently matching nothing."""
    tree = ast.parse(
        "def confirm_widget(row):\n"
        "    new_config = {**(row.get('config') or {}), 'widget_secret': 'x'}\n"
        "    source_connections_repo().update(row['id'], config=new_config)\n"
    )
    assert config_writer_keys(tree) == {"widget_secret"}, "detector failed to flag a planted dict-literal write"


def test_detector_flags_a_planted_subscript_violation():
    """A new writer using the `NAME["KEY"] = value` shape must be flagged —
    the _record_extraction_dispatch shape."""
    tree = ast.parse(
        "def record_widget(row):\n"
        "    config = dict(row.get('config') or {})\n"
        "    config['widget'] = {'last_run_at': 'x'}\n"
        "    source_connections_repo().update(row['id'], config=config)\n"
    )
    assert config_writer_keys(tree) == {"widget"}, "detector failed to flag a planted subscript write"


def test_detector_flags_a_planted_config_patch_violation():
    """A new writer using the `....config_patch(id, {"KEY": value})` shape
    must be flagged — `_record_extraction_dispatch`'s CURRENT shape
    (2026-09-07: switched from `update()` to `config_patch()` so a stale
    caller-held snapshot can no longer clobber a concurrent write)."""
    tree = ast.parse(
        "def record_widget(row):\n"
        "    patch = {'last_run_at': 'x'}\n"
        "    source_connections_repo().config_patch(row['id'], {'widget': patch})\n"
    )
    assert config_writer_keys(tree) == {"widget"}, "detector failed to flag a planted config_patch write"


def test_detector_ignores_a_config_patch_call_with_no_literal_dict():
    """A `config_patch` call passed a variable (not an inline dict literal)
    carries no statically-visible key and must not be flagged — nothing in
    this file does this today, but the detector should not crash or
    hallucinate a key on it either."""
    tree = ast.parse("def record_widget(row, patch):\n    source_connections_repo().config_patch(row['id'], patch)\n")
    assert config_writer_keys(tree) == set(), "a non-literal config_patch argument was wrongly flagged"


def test_detector_flags_a_planted_merge_extraction_call():
    """A new writer calling `....merge_extraction(...)` must be flagged as
    an "extraction" write — `_record_extraction_dispatch`'s CURRENT shape
    (2026-09-07: the one-transaction nested merge that closed the race a
    separate `get()`/`config_patch()` pair still left open)."""
    tree = ast.parse(
        "def record_widget(row):\n"
        "    patch = {'last_run_at': 'x'}\n"
        "    source_connections_repo().merge_extraction(row['id'], patch)\n"
    )
    assert config_writer_keys(tree) == {"extraction"}, "detector failed to flag a planted merge_extraction call"


def test_detector_ignores_an_unrelated_dict_with_no_config_update_call():
    """A function that assigns a literal key into SOME dict, but never calls
    `....update(config=...)` at all, must not be flagged — e.g. building a
    plain response dict."""
    tree = ast.parse("def browse():\n    result = {}\n    result['hint'] = 'narrow the search'\n    return result\n")
    assert config_writer_keys(tree) == set(), "an unrelated dict with no config= call was wrongly flagged"


def test_detector_ignores_a_sibling_dict_in_the_same_function():
    """This is the real false-positive shape `confirm_scope`/`remove_scope`
    have today: a NESTED scope-row dict (`existing`) gets its own literal-key
    assignments in the SAME function that also writes `config` — only the
    `config`-bound variable's keys may be flagged, never the sibling's."""
    tree = ast.parse(
        "def confirm_scope(row, existing, scopes):\n"
        "    existing['display_path'] = 'Contracts'\n"
        "    existing['anonymize'] = True\n"
        "    new_config = {**(row.get('config') or {}), 'scopes': scopes}\n"
        "    source_connections_repo().update(row['id'], config=new_config)\n"
    )
    assert config_writer_keys(tree) == {"scopes"}, "a sibling dict's own keys leaked into the config-write set"


def test_detector_ignores_the_factory_shape_used_elsewhere():
    """The generic editor's own carry-forward (`update_connection`) writes a
    VARIABLE key (`config[_sp_key] = ...`), never a literal one — must not be
    mistaken for a new hardcoded write."""
    tree = ast.parse(
        "def update_connection(config, old_config, keys):\n"
        "    for key in keys:\n"
        "        if key not in config:\n"
        "            config = {**config, key: old_config[key]}\n"
        "    repo.update('id', config=config)\n"
    )
    assert config_writer_keys(tree) == set(), "a variable-keyed carry-forward was wrongly flagged as a literal write"


def test_the_real_file_is_covered_by_the_ratchet_module():
    """Sanity: the ratchet test above is actually pointed at the real file,
    not an empty/misresolved path (would otherwise pass vacuously forever)."""
    assert ADMIN_SHAREPOINT_PATH.exists()
    text = ADMIN_SHAREPOINT_PATH.read_text()
    assert "SHAREPOINT_SERVER_WRITTEN_CONFIG_KEYS" in text
    assert "_record_extraction_dispatch" in text
