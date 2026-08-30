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

What the detector recognizes (the two shapes every current writer uses):

* ``NAME = {**base, "KEY": value}`` — a dict literal with a ``**`` unpack
  plus an explicit literal-string key (``confirm_scope`` / ``remove_scope``'s
  shape).
* ``NAME["KEY"] = value`` — a subscript assignment with a literal-string key,
  on a local variable later passed as ``config=`` (``_record_extraction_
  dispatch``'s shape).

Both are scoped to the SAME local variable name that flows into a
``config=`` keyword in an ``....update(...)`` call within the SAME function
— not a blind, function-wide scan. That distinction is load-bearing: this
file also assigns into OTHER dicts that are not the connection's ``config``
at all (a response dict's ``result["hint"] = ...``, a scope row's own
``existing["anonymize"] = ...`` nested a level below the top-level ``config``
key). A naive "any literal-key assignment anywhere in the file" scan flags
those too — false positives that would make the ratchet cry wolf on every
routine change and get muted. Variable-scoping the match to what actually
reaches ``config=`` is what keeps it precise.

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
    """Every literal-string key assigned into the local variable a function
    in ``tree`` passes as ``config=`` to a ``....update(...)`` call — see the
    module docstring for the exact two shapes recognized and why the match is
    scoped per-function to that one variable name."""
    keys: set[str] = set()
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

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
