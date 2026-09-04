"""Task 15 — admin UI: the Access column, the policy editor modal, the
inline interlock warning, the preview call, and policy history on
/admin/tables (table access policies design doc §13, §13.1).

The per-table listing renders entirely client-side
(``_renderFlatTableRows`` fetches ``/api/admin/registry`` and builds
``<tr>``s in JS — see ``loadAdminTablesLayout``), so — like every other
admin_tables UI test in this suite (``test_admin_tables_tab_ui.py``,
``test_admin_tables_warmup_ui.py``, ``test_admin_tables_ui_materialized.py``)
— these are structural: assert the served HTML (the inline ``<script>``
ships verbatim) carries the renderer, the three Access-column states, the
modal's DOM, and the inline-error wiring. A full click-through needs a
headless browser this suite doesn't run.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_tables.html"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _template_text() -> str:
    """The template carries a couple of stray NUL bytes (pre-existing,
    unrelated to this change) that trip a plain ``read_text`` — strip them
    the same way the diff-panel review that found this used ``grep -a``."""
    return TEMPLATE_PATH.read_bytes().replace(b"\x00", b"").decode("utf-8")


def test_access_column_header_present(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    assert r.status_code == 200, r.text
    assert "'<th>Access</th>'" in r.text


def test_access_column_renders_three_states(seeded_app):
    """``renderAccessPolicyChip`` emits: (1) a muted "+ Add policy" chip for
    a table that could carry a policy but doesn't, (2) a muted "not
    available — distributed" for a table that isn't eligible (not
    remote/server_only), and (3) a tinted "Policy" chip for a table that
    carries one."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert "function renderAccessPolicyChip" in body
    assert "access-chip--none" in body
    assert "access-chip--unavailable" in body
    assert "access-chip--active" in body
    assert "not available — distributed" in body
    assert ">Policy</button>" in body


def test_eligible_table_without_a_policy_gets_a_labelled_affordance(seeded_app):
    """Item 1 of the #1979 setup-flow review: the eligible-but-no-policy
    state used to render as a bare, unlabelled "—" that was clickable and
    looked inert. It now carries a label and an explanatory title, and it
    still opens the SAME editor (``openAccessPolicyModal``)."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert ">+ Add policy</button>" in body
    assert "This table is eligible for a row-level access policy — click to add one" in body
    # The bare dash is gone as a BUTTON label (the internal-table "—" stays
    # a non-interactive <span>, which is a different, correct state).
    assert ">—</button>" not in body
    # Still the one modal all four states open.
    assert "openAccessPolicyModal" in body


def test_eligible_no_policy_chip_hints_at_an_unpackaged_table(seeded_app):
    """Item 6: a policy on a table no data package carries guards data
    nobody can reach, so the chip trails a quiet link into the same
    ``?unpackaged=1`` assign flow the /admin/data-packages banner opens."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert "access-chip-hint" in body
    assert "not in any package yet" in body
    assert "/admin/tables?unpackaged=1" in body
    # Membership comes from the already-server-rendered delivery map — no
    # new field and no new endpoint.
    assert "TABLE_DELIVERY[String(t.id || '')]" in body


def test_ineligible_access_chip_label_is_unchanged(seeded_app):
    """The labelled eligible state must not have disturbed the ineligible
    one: same label, same explanatory title, same modal."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert ">not available — distributed</button>" in body
    assert "table can carry an access policy — click to see the fix." in body


def test_access_column_omits_the_unwired_mapping_warn_state(seeded_app):
    """The design doc names a fourth "Policy · check" warn chip for an
    empty/stale mapping table (§15.1). No surface exposes that signal yet
    (would need server-side SQL-reference parsing + mapping sync-state
    cross-reference), so it is deliberately NOT implemented — this pins
    that as a documented decision, not a silent gap."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert "access-chip--warn" not in body
    assert "TODO: wire once that signal exists" in body


def test_internal_tables_get_a_fixed_non_interactive_access_chip(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert "access-chip--fixed" in body
    assert "Internal tables use their own built-in row scoping" in body


def test_access_policy_modal_is_a_plain_textarea_no_syntax_highlighting(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert 'id="accessPolicyModal"' in body
    assert '<textarea class="form-textarea" id="apSql"' in body
    assert '<textarea class="form-textarea" id="apNote"' in body
    for forbidden in ("codemirror", "CodeMirror", "monaco-editor", "ace-builds", "ace.js"):
        assert forbidden not in body, f"unexpected syntax-highlighting dependency: {forbidden}"


def test_access_policy_modal_shows_the_variable_vocabulary_and_prefill(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert "$user_email" in body
    assert "$user_id" in body
    assert "$user_groups" in body
    assert "Use table as base" in body
    assert "function apUseTableAsBase" in body
    assert "'SELECT * FROM '" in body


def test_access_policy_note_is_required_before_save(seeded_app):
    """The API 422s a policy attach with no note (Task 14); the modal
    surfaces that requirement inline before even calling the API."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert 'id="apNote"' in body
    assert "async function apSavePolicy" in body
    assert "access_policy_note is required" in body


def test_access_policy_save_and_clear_wired_to_registry_put(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert "async function apClearPolicy" in body
    assert 'id="apClearBtn"' in body
    assert "/api/admin/registry/" in body
    assert "access_policy_sql" in body
    assert "confirmModal(" in body


def test_access_policy_inline_interlock_warning_mirrors_the_bq_pattern(seeded_app):
    """Deliverable 3: the interlock warning is computed and shown BEFORE
    save — the same pattern as ``onEditBqAccessModeChange`` — and it names
    the fix (set server_only, or query_mode='remote')."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert 'id="apInterlockWarning"' in body
    assert "function _apRenderInterlockWarning" in body
    assert "onEditBqAccessModeChange" in body  # explicit cross-reference in the comment
    assert "server_only=true" in body
    assert "query_mode='remote'" in body


def test_access_policy_save_failure_renders_inline_not_as_a_toast(seeded_app):
    """A rejected save (the §16 ``reason: detail`` error contract) must
    render inline in the modal's ``apSaveError`` slot, not the 4s
    auto-hide ``showToast`` other saves use."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert 'id="apSaveError"' in body
    assert "function _apShowSaveError" in body
    assert "function _apHideSaveError" in body
    # Both the PUT-rejected branch and the network-error branch route
    # through the inline renderer, not showToast(..., 'error').
    assert body.count("_apShowSaveError(") >= 4


def test_access_policy_preview_is_wired_to_the_preview_endpoint(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert "async function apRunPreview" in body
    assert "/policy/preview" in body
    assert "as_user" in body
    assert "as_groups" in body
    assert "rows_visible" in body
    assert "rows_total" in body


def test_access_policy_preview_is_single_persona_with_a_documented_todo(seeded_app):
    """v1 ships single-persona preview; the full persona -> rows -> columns
    matrix (§13.1: union coverage + pairwise overlap across every distinct
    group-set) is explicitly deferred, not silently missing."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert "TODO: once that primitive exists" in body
    assert "union coverage" in body
    assert "pairwise overlap" in body


def test_access_policy_history_reads_the_existing_activity_endpoint(seeded_app):
    """History reuses ``GET /api/admin/activity`` (resource + action_prefix
    filters) rather than a new endpoint — zero backend plumbing added."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert "async function _apLoadHistory" in body
    assert 'id="apHistorySection"' in body
    assert "/api/admin/activity?resource=" in body
    # #1430 gave a policy write its own audit actions, so the fallback asks
    # for those first and only then re-scans the generic `update_table`
    # rows an instance wrote before they existed.
    assert "'access_policy.'" in body
    assert "row.action === 'access_policy.set'" in body
    assert "'update_table'" in body


def test_access_policy_history_cleared_detection_survives_audit_redaction(seeded_app):
    """#1979 redacted ``access_policy_sql`` out of ``update_table`` audit
    params (`app/api/admin.py::_SECRET_FIELDS`) — the value is now always
    the literal string ``"***"`` (set) or ``"<empty>"`` (cleared/absent),
    never ``null``/``""``. A JS falsiness check (``!params.access_policy_sql``)
    would treat ``"<empty>"`` as truthy and misreport every clear as an
    update, so the "cleared the policy" row must key off that literal
    sentinel instead."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert "params.access_policy_sql === '<empty>'" in body
    assert "!params.access_policy_sql" not in body


def test_builder_scaffold_renders_when_flag_on(seeded_app):
    """Task 4 (access-policy-builder-ux plan): the modal's default tab is a
    no-SQL Builder — a column-list mount fed by ``GET .../policy/columns``
    — with today's textarea demoted to an "Advanced SQL" tab. Both tabs
    ship in the same static HTML; JS toggles which panel is visible."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert 'id="apBuilder"' in body
    assert 'id="apColList"' in body
    assert 'data-ap-tab="builder"' in body and 'data-ap-tab="sql"' in body


def test_inline_eligibility_and_mapping_controls_render(seeded_app):
    """Task 5: the interlock warning's former dead-end sentence ("set
    server_only first") becomes an inline fix-it action, and a separate
    switch lets an admin mark a table policy_mapping=true (referenceable
    from other policies' SQL) without dropping to the CLI."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert 'id="apMakeServerOnly"' in body
    assert 'id="apMappingToggle"' in body


def test_mapping_toggle_is_visually_separated_from_the_row_scope_section(seeded_app):
    """The mapping toggle answers a different question ("can other tables'
    policies read through this one") than the row-rule/mask builder right
    below it ("who sees which rows of THIS table") — stacked with identical
    styling and no separator, it reads as one setting. It must carry its
    own heading, a divider before the Builder tabs, and copy that says it
    does not change this table's own row visibility."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert "Use as input for other tables' policies" in body
    assert "does not change who sees which" in body
    mapping_idx = body.index("ap-mapping-section")
    divider_idx = body.index('<hr style="border:0; border-top:1px solid var(--ds-border)')
    tabs_idx = body.index('data-ap-tab="builder"')
    assert mapping_idx < divider_idx < tabs_idx


def test_registered_table_row_wires_the_access_chip_to_the_modal(seeded_app):
    """A registered table's row calls ``openAccessPolicyModal(t)`` with the
    full registry row as payload, so the modal can prefill from
    access_policy_sql/_note without a second round-trip."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    c.post(
        "/api/admin/register-table",
        headers=_auth(token),
        json={
            "name": "ap_ui_orders",
            "source_type": "keboola",
            "bucket": "in.c-sales",
            "source_table": "orders",
            "query_mode": "local",
        },
    )
    r = c.get("/admin/tables", headers=_auth(token))
    assert "openAccessPolicyModal(" in r.text


def test_row_rule_builder_scaffold_renders_above_the_column_list(seeded_app):
    """access-policy-builder-ux Slice 2, Task A: a "Who sees which rows"
    section — the row-rule repeater — sits above ``#apColList`` inside the
    Builder tab, with an add-rule control and an AND/OR combine toggle."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert 'id="apBuilder"' in body
    assert 'id="apRowRules"' in body
    assert 'id="apRowCombine"' in body
    assert "Who sees which rows" in body
    assert "_apAddRowRule()" in body
    # The row-rule section is ABOVE the column list in document order.
    assert body.index('id="apRowRules"') < body.index('id="apColList"')


def test_row_rule_builder_ops_cover_the_compiler_vocabulary(seeded_app):
    """Every ``row_rules`` op the compiler
    (``src/access_policy_compile.py``) understands must be reachable from
    the builder — a caller-group check, self-owned-row checks, and literal
    eq/in — so the no-SQL path never needs to fall back to Advanced SQL for
    the common row-scoping cases."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    for op in ("in_caller_groups", "eq_caller_email", "eq_caller_id", "'eq'", "'in'"):
        assert op in body, f"missing row-rule op wiring: {op}"


def test_row_rule_builder_feeds_the_existing_compile_call(seeded_app):
    """The row-rule state feeds the SAME ``_apCompileNow()`` POST Slice 1
    already wires up — the compiler stays the only SQL generator, and the
    hard-coded ``row_rules: []`` placeholder from Slice 1 is gone."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert "async function _apCompileNow" in body
    assert "row_rules: []" not in body
    assert "_apAssembleRowRules()" in body
    assert "function _apRenderRowRules" in body


def test_row_rule_controls_respect_the_eligibility_interlock(seeded_app):
    """A distributed table's row-rule controls must be disabled the same
    way its mask selects already are (``_apIsEligible``) — a row filter is
    just as pointless as a mask on a table `agnes pull` can route around."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert "_apIsEligible(_apTable)" in body
    # The row-rule renderer computes its own disabled flag the same way
    # _apRenderColList does.
    assert body.count("!_apIsEligible(_apTable)") >= 2


def test_row_rule_column_picker_has_a_filter_input(seeded_app):
    """#1979 follow-up (admin setup-flow review, item 4): the row rule's
    column dropdown is a flat alphabetical list with no type-to-filter — on
    a table with 100+ columns the admin scrolls by hand. Matches the
    register-table wizard's "Filter tables…" box (``_register_table_form.
    html`` / ``register_table_form.js``): a text input, filtered live, that
    never changes the picker's selected value."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert 'class="ap-rr-col-filter"' in body
    assert "Filter columns…" in body
    assert 'oninput="_apFilterRowRuleColumnMenu(this)"' in body
    assert "function _apFilterRowRuleColumnMenu" in body
    # Skipped alongside the paired custom dropdown for a disabled (ineligible)
    # row — same interlock the column/operator dropdowns already respect.
    assert "(disabled ? '' : '<input type=\"text\" class=\"ap-rr-col-filter\"" in body


def test_row_rule_column_filter_matches_case_insensitively_and_never_writes_state(seeded_app):
    """The filter function only toggles ``.ds-dropdown-menu-item`` visibility
    — it must never touch ``_apRowRules``, ``_apColumns``, or set a
    ``<select>``'s value, or the picker's value contract with the compiler
    would drift out from under a keystroke."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    start = body.index("function _apFilterRowRuleColumnMenu")
    end = body.index("\n    }\n", start)
    fn = body[start : end + len("\n    }\n")]
    assert ".toLowerCase()" in fn
    assert "_apMatchesColumnFilter" in fn
    assert "_apRowRules" not in fn
    assert ".value =" not in fn
    assert "hidden" in fn  # hides/shows menu items and the empty-state message


def test_column_mask_list_has_a_matching_filter_input(seeded_app):
    """The mask/column list below the row rules is sourced from the same
    ``_apColumns`` fetch — give it the same "Filter columns…" box rather
    than leaving one of the two column pickers unfiltered."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert 'id="apColFilter"' in body
    assert 'class="ap-col-filter"' in body
    assert "Filter columns…" in body
    assert 'oninput="_apRenderColList()"' in body
    assert "function _apMatchesColumnFilter" in body
    # Case-insensitive substring on name and (when known) type.
    fn = body[body.index("function _apMatchesColumnFilter") : body.index("function _apColumnFilterValue")]
    assert ".toLowerCase()" in fn
    assert "indexOf(search)" in fn
    # An empty result set says so instead of silently rendering nothing.
    assert "No columns match your filter." in body


def test_column_filter_never_reaches_the_compiled_policy_spec(seeded_app):
    """Filtering only narrows what ``_apRenderColList``/the row-rule menu
    render — the compile request still walks the full, unfiltered state
    (``_apMaskState`` and ``_apRowRules``), so a column hidden by an active
    filter is never silently dropped from a saved policy."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    compile_start = body.index("async function _apCompileNow")
    compile_end = body.index("\n    }\n", compile_start)
    compile_fn = body[compile_start:compile_end]
    assert "row_rules: _apAssembleRowRules()" in compile_fn
    assert "column_masks: _apMaskState" in compile_fn
    assert "apColFilter" not in compile_fn
    assert "_apFilterRowRuleColumnMenu" not in compile_fn


def test_preview_shows_before_after_on_the_raw_sample(seeded_app):
    """access-policy-builder-ux Slice 2, Task B: the preview renders every
    ``base_sample_rows`` row — struck-through when the policy drops it,
    diffed cell-by-cell when a visible column's value changed, and struck
    in the header with an em-dash body for a hidden column."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert "base_sample_rows" in body
    assert "function _apMatchPreviewRows" in body
    assert "ap-preview-row--dropped" in body
    assert "ap-preview-diff-raw" in body
    assert "ap-preview-hidden-cell" in body
    # The transient result grid still uses the product's ONE table class.
    assert 'class="data-table"' in body


def test_make_server_only_button_recovers_from_a_failed_attempt(seeded_app):
    """``apMakeServerOnly()`` disables its own button and relabels it
    "Setting…" before the PUT. Only the SUCCESS path re-renders the
    warning (which recreates the button), so both failure paths — a
    rejected PUT and a network error — must put the button back the way
    ``apSavePolicy()``/``apClearPolicy()`` do in their ``finally``, or the
    admin is left staring at a permanently disabled control with no way to
    retry."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    fn = body[body.index("async function apMakeServerOnly") : body.index("async function apToggleMapping")]
    assert "} finally {" in fn
    assert "btn.disabled = false" in fn


def test_builder_surfaces_compile_warnings(seeded_app):
    """``policy/compile`` returns ``warnings`` (unknown column dropped, and
    the "this policy filters nothing" no-op warning). Discarding them lets
    an admin save a policy that quietly does nothing — so the builder
    renders them, and renders them as text (never innerHTML with server
    strings)."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert 'id="apCompileWarnings"' in body
    assert "function _apRenderCompileWarnings" in body
    assert "body.warnings" in body
    fn = body[body.index("function _apRenderCompileWarnings") : body.index("function _apShowSaveError")]
    assert "textContent" in fn
    assert "innerHTML" not in fn


def test_save_confirms_before_storing_a_policy_that_filters_or_masks_nothing(seeded_app):
    """A policy with no row rule and no column mask compiles to a bare
    `SELECT * FROM t` — it saves successfully (masking-only or
    filtering-only policies are legitimate, so this must never be a hard
    block) but must not go through silently: `apSavePolicy()` asks for
    explicit confirmation first, mirroring the same no-op condition
    `compile_policy()` itself warns about (no WHERE, no EXCLUDE, no CASE)."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert "function _apSqlFiltersOrMasksNothing" in body
    fn = body[body.index("async function apSavePolicy") : body.index("async function apClearPolicy")]
    assert "_apSqlFiltersOrMasksNothing(sql)" in fn
    assert "confirmModal(" in fn
    assert "Save anyway" in fn


def test_the_feature_flag_notice_has_exactly_one_implementation(seeded_app, monkeypatch):
    """#1430 and #1979 both grew a "say the flag is off up front" notice and
    they were reconciled onto ONE: the server-rendered ``{% if not
    access_policies_enabled %}`` banner plus the ``disabled`` Save button
    (pinned by ``test_flag_off_shows_a_notice_and_disables_save`` below).
    The JS twin -- an ``ACCESS_POLICIES_ENABLED`` constant read from a
    ``data-access-policies-enabled`` body attribute, feeding a second
    ``#apFeatureDisabledWarning`` div and a client-side branch in
    ``apSavePolicy()`` -- must not come back: two notices on one modal read
    as two different rules, and the disabled button is the stronger gate
    (the JS twin left Save clickable and only explained the refusal after
    the click).
    """
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    body = c.get("/admin/tables", headers=_auth(token)).text

    assert 'id="apFeatureDisabledWarning"' not in body
    assert "_apRenderFeatureDisabledWarning" not in body
    # (the notice's own prose names the env var, so pin the JS symbol)
    assert "var ACCESS_POLICIES_ENABLED" not in body
    assert "!ACCESS_POLICIES_ENABLED" not in body
    assert "data-access-policies-enabled" not in body
    # ... and the surviving one is still there, with the flag off.
    assert 'id="apFlagDisabledNotice"' in body

    monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
    assert 'id="apFlagDisabledNotice"' not in c.get("/admin/tables", headers=_auth(token)).text


def test_preview_all_groups_button_is_wired_to_the_new_endpoint(seeded_app):
    """review-plan P1.4: a "Preview all groups" action next to the
    single-persona preview sweeps every real group through the same policy
    in one call, so a CASE with a missing ELSE branch shows up as an
    unexpected group seeing everything instead of requiring the admin to
    run the single-persona preview once per group by hand."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert "async function apRunPreviewAllGroups" in body
    assert "/policy/preview-groups" in body
    assert 'id="apPreviewGroupsResult"' in body
    assert "Preview all groups" in body
    assert "function _apRenderPreviewGroupsResult" in body


def test_preview_renderers_surface_the_mapping_warning(seeded_app):
    """review plan P2.6: both preview renderers must check
    ``body.mapping_warning`` and show it as text (server-supplied string,
    so ``textContent``-safe rendering via ``escapeHtml``, never raw
    ``innerHTML``) instead of rendering a misleading rows/columns result
    when a referenced ``policy_mapping`` table is empty or never synced."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    single = body[body.index("function _apRenderPreviewResult") : body.index("async function _apLoadHistory")]
    assert "body.mapping_warning" in single
    assert "escapeHtml(body.mapping_warning)" in single
    groups = body[body.index("function _apRenderPreviewGroupsResult") : body.index("function _apMatchPreviewRows")]
    assert "body.mapping_warning" in groups
    assert "escapeHtml(body.mapping_warning)" in groups


def test_preview_only_diffs_samples_the_server_says_are_comparable(seeded_app):
    """The before/after diff pairs raw rows against policied rows, which is
    only meaningful when both samples cover the same source rows. The
    preview response carries ``base_sample_comparable``; when it is false
    the renderer must fall back to the policied-only view with a note
    rather than inventing dropped rows."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert "base_sample_comparable" in body
    assert "ap-preview-note" in body


def test_a_failed_compile_does_not_lock_the_advanced_sql_tab(seeded_app):
    """The compile-error block exists to stop a SAVE of stale builder output:
    after a failed compile `#apSql` still holds the previous compile (or the
    saved policy), and saving that would silently disagree with what the
    builder shows.

    It must not become a lock on the tab the surrounding comments describe as a
    standalone escape hatch. A deterministic failure —
    `policy_builder_schema_unavailable` for a table whose schema cannot be read,
    or a spec that hides every column — fails identically on every retry, so no
    builder interaction can clear the flag and the admin could not save even SQL
    they typed themselves. Hand-editing the box takes ownership of its contents,
    which is what clears it."""
    c = seeded_app["client"]
    r = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"]))
    body = r.text
    assert 'oninput="apSqlEdited()"' in body, "the SQL box does not report a manual edit"
    fn = body[body.index("function apSqlEdited") : body.index("async function apSavePolicy")]
    assert "_apCompileError = null" in fn
    assert "_apHideSaveError()" in fn
    # ...and the message names the way out, rather than only the blocked action.
    save = body[body.index("async function apSavePolicy") :]
    save = save[: save.index("var sql = document.getElementById('apSql')")]
    assert "Advanced SQL tab" in save


def test_an_unreadable_schema_is_not_reported_as_an_empty_table(seeded_app):
    """`GET .../policy/columns` answers 200 with an empty list both when a table
    genuinely has no columns and when the DESCRIBE failed — the ordinary outcome
    for a remote row, whose external catalog is not attached on the connection
    that read uses. "No columns found" sends the admin looking for a data
    problem; the endpoint now says which it was."""
    c = seeded_app["client"]
    r = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"]))
    body = r.text
    assert "body.schema_available !== false" in body
    render = body[body.index("function _apRenderColList") :]
    render = render[: render.index("var disabled = ")]
    assert "_apSchemaAvailable" in render
    assert "Advanced SQL tab" in render, "the empty state does not point at the way to write the policy"


def test_the_one_click_starter_sql_clears_the_compile_block_like_typing_does(seeded_app):
    """`apUseTableAsBase()` writes `#apSql` programmatically, and a programmatic
    `.value` assignment does not fire `input` — so the textarea's `oninput` hook
    never runs.

    Without an explicit call the compile block set by an earlier failure outlives
    text the admin deliberately put in the box, and the save keeps being refused
    until they type one extra character. For the deterministic failures the
    escape hatch exists for (unreadable schema, a spec hiding every column) no
    builder interaction can clear the flag at all, so the button would hand the
    admin starter SQL they cannot save."""
    c = seeded_app["client"]
    body = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text

    fn = body[body.index("function apUseTableAsBase") :]
    fn = fn[: fn.index("function apSwitchTab")]
    assert "apSqlEdited();" in fn, (
        "apUseTableAsBase replaces the SQL box without reporting the edit -- "
        "a stale compile block would survive and keep refusing the save"
    )


def test_opening_the_sql_tab_runs_a_queued_compile_instead_of_dropping_it(seeded_app):
    """The compile is debounced ~250ms and `#apSql` is written only by
    `_apCompileNow`. Cancelling the timer when the Advanced SQL tab opens throws
    away any builder change made inside that window: it never reaches the box,
    no compile error is set to block the save, and the admin then stores a
    policy that silently omits their last edit.

    Flushing runs it instead, so the box handed over matches what the builder
    shows. The in-flight request is deliberately left to land — the admin has
    not typed yet, and their first keystroke cancels it through `apSqlEdited`,
    with `_apCompileSeq` discarding a superseded response."""
    c = seeded_app["client"]
    body = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text

    tab = body[body.index("function apSwitchTab") :]
    tab = tab[: tab.index("var _AP_MASK_LABELS")]
    assert "_apFlushPendingCompile();" in tab, "switching to the SQL tab must flush the queued compile, not discard it"
    assert "_apCancelPendingCompile();" not in tab, (
        "cancelling here drops a builder change made inside the debounce window"
    )

    flush = body[body.index("function _apFlushPendingCompile") :]
    flush = flush[: flush.index("function _apScheduleCompile")]
    assert "clearTimeout(_apCompileTimer)" in flush
    assert "_apCompileNow();" in flush, "the queued compile must actually run"


# ── K1-sweep finding 4 (#1979): surface access_policies.enabled in the modal ──
#
# The flag only gates ATTACHING a policy (``PUT /registry/{id}``'s
# ``access_policy_sql`` setter) — enforcement of an already-saved policy
# always runs, and the read-only authoring endpoints (``policy/columns``,
# ``policy/compile``, ``policy/preview``) are never gated (see their own
# docstrings in ``app/api/admin.py``). Before this, the modal opened
# regardless and only the server-side save 422'd — friction, not a dead
# end, but discoverable only after typing a policy. These tests pin the
# notice + disabled Save button that make the flag state visible up front.


def test_flag_off_shows_a_notice_and_disables_save(seeded_app):
    """Default test env carries no ``AGNES_ACCESS_POLICIES_ENABLED`` — same
    as this instance's own default (off) — so the notice must render and
    the Save button must be disabled without any extra setup."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert 'id="apFlagDisabledNotice"' in body
    assert "attaching or editing a policy is disabled" in body
    assert "enforcement of any already-saved policy keeps running" in body
    assert "/admin/server-config" in body
    assert "AGNES_ACCESS_POLICIES_ENABLED" in body
    assert 'id="apSaveBtn" onclick="apSavePolicy()" disabled' in body


def test_flag_off_leaves_the_editor_and_preview_usable(seeded_app):
    """Only Save is blocked — the textarea stays readable/editable (so an
    admin can still view an existing policy) and the Preview button is not
    disabled, matching the backend (``policy/preview`` is not gated)."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert '<textarea class="form-textarea" id="apSql"' in body
    assert "apSql" in body and "disabled" not in body[body.index('id="apSql"') : body.index('id="apSql"') + 200]
    assert 'onclick="apRunPreview()">Preview</button>' in body


def test_flag_off_the_access_chip_still_opens_the_modal(seeded_app):
    """The notice must be discoverable — the Access-column chip flow keeps
    calling ``openAccessPolicyModal`` regardless of the flag; the modal
    itself decides what to show, not the chip."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert "function renderAccessPolicyChip" in body
    assert "openAccessPolicyModal(" in body


def test_flag_on_is_zero_visual_change(seeded_app, monkeypatch):
    """With the flag on, neither the notice nor the disabled attribute may
    render — this is the "zero visual change" contract."""
    monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "1")
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text
    assert 'id="apFlagDisabledNotice"' not in body
    assert 'id="apSaveBtn" onclick="apSavePolicy()">Save policy</button>' in body


def test_preview_renders_the_transpiled_block_when_present(seeded_app):
    """K1-sweep finding 3 (#1979): a remote table on a transpiling engine
    runs the TRANSPILED body on a live read, not the DuckDB text in
    ``#apSql`` — ``_apRenderPreviewResult`` must show it, collapsed by
    default (secondary to the row/column preview above), read-only."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    body = r.text

    render = body[body.index("function _apRenderPreviewResult") :]
    render = render[: render.index("function _apLoadHistory")]
    assert "body.transpiled" in render
    assert "ap-preview-transpiled" in render
    assert "<details" in render and "<summary>" in render
    assert "Transpiled for " in render
    # Read-only text, escaped like every other server-controlled string
    # rendered into this modal — never innerHTML'd raw.
    assert "escapeHtml(body.transpiled.dialect)" in render
    assert "escapeHtml(body.transpiled.relation_sql)" in render


# ── #1979 K1-sweep finding 1: restore a policy version ────────────────


def test_history_prefers_the_revision_store_over_the_audit_trail(seeded_app):
    """The panel now reads ``GET .../policy/revisions`` first — the only
    source that carries the SQL BODY of each saved state, which is what
    "Restore" needs. The audit trail cannot serve it: #1979 redacted
    ``access_policy_sql`` out of ``update_table`` params."""
    c = seeded_app["client"]
    body = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
    assert "/policy/revisions?limit=" in body
    assert "async function _apLoadHistory" in body
    assert "function _apRenderRevisions" in body

    loader = body[body.index("async function _apLoadHistory") :]
    loader = loader[: loader.index("async function _apLoadHistoryFromActivity")]
    assert "/policy/revisions" in loader
    assert "_apLoadHistoryFromActivity" in loader, "the audit-derived history must remain the fallback"


def test_history_falls_back_to_the_audit_trail_when_there_is_no_revision_store(seeded_app):
    """``access_policy_revisions`` is PG-only (A3), so a DuckDB-backed
    instance answers a typed 501. The panel must degrade to the read-only
    audit-derived history it always had — not to an empty section, which
    would read as "this policy was never edited"."""
    c = seeded_app["client"]
    body = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
    assert "async function _apLoadHistoryFromActivity" in body
    assert "/api/admin/activity?resource=" in body

    fallback = body[body.index("async function _apLoadHistoryFromActivity") :]
    fallback = fallback[: fallback.index("function _apParseAuditParams")]
    # The fallback rows carry no Restore button: without a stored body there
    # is nothing to restore, and a button that cannot work is worse than none.
    assert "apRestoreRevision(" not in fallback


def test_restore_button_fills_the_editor_and_never_saves_by_itself(seeded_app):
    """Restore is not a write. It loads the revision into the SQL + note
    boxes and hands it back to the admin, so the ordinary Save runs every
    validation and interlock a fresh policy pays for (distribution
    interlock, mandatory note, static validation, live probe)."""
    c = seeded_app["client"]
    body = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
    assert "function apRestoreRevision" in body
    assert ">Restore</button>" in body

    fn = body[body.index("function apRestoreRevision") :]
    fn = fn[: fn.index("function _apShowRestoreNotice")]
    assert "document.getElementById('apSql').value" in fn
    assert "document.getElementById('apNote').value" in fn
    # No write of any kind from the restore path itself.
    assert "fetch(" not in fn, "restore must not call the API — the admin's Save does"
    assert "apSavePolicy(" not in fn, "restore must not auto-save; the editor/save flow is the point"
    # The restored body lands on the tab that shows it, and the stale-builder
    # guard is cleared the same way a hand edit clears it.
    assert "apSwitchTab('sql')" in fn
    assert "apSqlEdited()" in fn


def test_restore_announces_that_nothing_is_saved_yet(seeded_app):
    """An admin who clicks Restore and closes the modal must not believe the
    old policy is back. The notice says the editor is loaded and nothing has
    changed until Save."""
    c = seeded_app["client"]
    body = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
    assert 'id="apRestoreNotice"' in body
    assert "function _apShowRestoreNotice" in body
    assert "function _apHideRestoreNotice" in body
    assert "Nothing has changed yet" in body
    assert "Save policy" in body


def test_a_cleared_revision_renders_as_such_and_offers_no_restore(seeded_app):
    """A revision whose SQL is NULL is the moment protection was REMOVED.
    It belongs in the history (it is the most important row in it), but
    "restore" on it would mean re-clearing — which the Clear button already
    does, explicitly and with a confirmation."""
    c = seeded_app["client"]
    body = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
    renderer = body[body.index("function _apRenderRevisions") :]
    renderer = renderer[: renderer.index("function apRestoreRevision")]
    assert "rev.cleared" in renderer
    assert "cleared the policy" in renderer
    assert "if (!rev.cleared)" in renderer, "the Restore button is conditional on a body existing"


def test_history_rows_show_a_peek_at_the_stored_sql(seeded_app):
    """Who/when/note alone cannot tell two edits apart. The row shows the
    head of the stored body — truncated, because the panel is a chooser, not
    a viewer; the editor is where the full body goes."""
    c = seeded_app["client"]
    body = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
    assert "function _apSqlPeek" in body
    assert "ap-history-sql" in body
    # Every interpolation into the row HTML is escaped — saved_by and the
    # note are admin-authored free text.
    renderer = body[body.index("function _apRenderRevisions") :]
    renderer = renderer[: renderer.index("function apRestoreRevision")]
    assert "escapeHtml(_apSqlPeek(" in renderer
    assert "escapeHtml(who)" in renderer


def test_a_truncated_history_says_so(seeded_app):
    """The endpoint returns an untruncated ``count`` alongside the capped
    list, so ten of thirty-four never renders as the whole history."""
    c = seeded_app["client"]
    body = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
    renderer = body[body.index("function _apRenderRevisions") :]
    renderer = renderer[: renderer.index("function apRestoreRevision")]
    assert "body.count" in renderer
    assert "most recent of" in renderer


def test_new_history_styles_are_tokenized_and_not_inline(seeded_app):
    """Design-system contract: the new rows style through classes in the
    page's CSS block using --ds-* tokens, never inline style attributes and
    never raw colours."""
    c = seeded_app["client"]
    body = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
    for cls in (".ap-history-sql", ".ap-history-restore", ".ap-history-meta"):
        assert cls + " {" in body, f"missing CSS rule for {cls}"
    block = body[body.index(".ap-history-sql {") :]
    block = block[: block.index(".ap-history-restore {")]
    assert "var(--ds-" in block
    assert "#" not in block, "raw hex colour in the new history styles"

    renderer = body[body.index("function _apRenderRevisions") :]
    renderer = renderer[: renderer.index("function apRestoreRevision")]
    assert "style=" not in renderer, "new history rows must not carry inline styles"


# ── #1979 K1-sweep finding 2 (MonikaFeigler): a diff between revisions ────
#
# The panel already listed who/when/note + a body peek + Restore. That is a
# CHOOSER, not a way to tell what actually changed between two saves — this
# section adds a per-revision line diff so an admin doesn't have to load two
# versions into the editor and eyeball them.


def test_history_panel_ships_a_self_contained_line_diff(seeded_app):
    """No external diff library — a policy body is a handful of SQL lines,
    not a file worth Myers' bookkeeping. The functions live inline, next to
    the renderer that calls them."""
    c = seeded_app["client"]
    body = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
    assert "function _apDiffLines" in body
    assert "function _apRenderDiffBody" in body
    assert "function _apRenderRevisionDiff" in body
    for lib in ("diff-match-patch", "jsdiff", "diff.js", "Diff.diffLines"):
        assert lib not in body, f"unexpected external diff dependency: {lib}"


def test_diff_is_bounded_so_a_pasted_wall_of_sql_cannot_hang_the_panel(seeded_app):
    """The LCS table is O(n*m) time AND space — a per-side cap keeps opening
    the history panel cheap even if a policy body is pasted from somewhere
    unbounded."""
    c = seeded_app["client"]
    body = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
    assert "_AP_DIFF_MAX_LINES" in body
    diff_fn = body[body.index("function _apDiffLines") :]
    diff_fn = diff_fn[: diff_fn.index("function _apRenderDiffBody")]
    assert "_AP_DIFF_MAX_LINES" in diff_fn
    assert "return null" in diff_fn


def test_diff_renders_a_collapsed_details_block_like_the_transpiled_preview(seeded_app):
    """Same idiom as ``.ap-preview-transpiled`` (f2fd242e6): collapsed by
    default, opened only when the peek isn't enough to tell two edits apart."""
    c = seeded_app["client"]
    body = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
    render_fn = body[body.index("function _apRenderRevisionDiff") :]
    render_fn = render_fn[: render_fn.index("function _apRenderRevisions(body)")]
    assert "<details" in render_fn and "<summary>" in render_fn
    assert "ap-history-diff-details" in render_fn
    assert "Diff vs previous" not in render_fn, "the label is a parameter, not hardcoded here"
    assert "escapeHtml(label)" in render_fn


def test_diff_escapes_both_the_removed_and_added_lines(seeded_app):
    """Every line inserted into the diff body — from either side — goes
    through ``escapeHtml`` before it reaches the page. A policy body is
    admin-authored SQL, not trusted markup."""
    c = seeded_app["client"]
    body = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
    diff_body_fn = body[body.index("function _apRenderDiffBody") :]
    diff_body_fn = diff_body_fn[: diff_body_fn.index("function _apMappingLabel")]
    assert "escapeHtml(prefix + op.text)" in diff_body_fn, (
        "the single escapeHtml call must cover both add ('+') and del ('-') lines, "
        "since op.text comes from either the old or the new side"
    )


def test_note_only_change_says_so_instead_of_an_empty_diff(seeded_app):
    """Re-saving the same SQL with a clarifying note must not render an
    empty diff block, which would read as a bug rather than as "no SQL
    change"."""
    c = seeded_app["client"]
    body = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
    render_fn = body[body.index("function _apRenderRevisionDiff") :]
    render_fn = render_fn[: render_fn.index("function _apRenderRevisions(body)")]
    assert "SQL unchanged" in render_fn and "note changed" in render_fn
    assert "oldSql === newSql" in render_fn


def test_cleared_revision_diffs_as_all_lines_removed(seeded_app):
    """A cleared revision's SQL is NULL. Diffed against its predecessor it
    must show as every line removed, not silently treated as unchanged."""
    c = seeded_app["client"]
    body = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
    render_fn = body[body.index("function _apRenderRevisionDiff") :]
    render_fn = render_fn[: render_fn.index("function _apRenderRevisions(body)")]
    assert "!older.cleared" in render_fn
    assert "!newer.cleared" in render_fn


def test_oldest_revision_in_the_window_says_first_recorded_or_diffs_against_empty(seeded_app):
    """The oldest revision the (capped) list carries has no older neighbour
    IN THAT LIST. When the store truly holds nothing before it, say so
    plainly rather than rendering a diff against nothing; when ``count``
    says there is more history than this page shows, diff against an empty
    baseline instead of silently dropping the block."""
    c = seeded_app["client"]
    body = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
    renderer = body[body.index("function _apRenderRevisions(body)") :]
    renderer = renderer[: renderer.index("function apRestoreRevision")]
    assert "First recorded version." in renderer
    assert "_apRenderRevisionDiff(null, rev, 'Diff vs previous')" in renderer
    assert "body.count > revisions.length" in renderer


def test_newest_revision_also_diffs_against_the_currently_stored_policy(seeded_app):
    """The newest saved revision and the live registry row are USUALLY
    identical (one save writes both in the same transaction) — this only
    renders when they diverge, per idx === 0."""
    c = seeded_app["client"]
    body = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
    renderer = body[body.index("function _apRenderRevisions(body)") :]
    renderer = renderer[: renderer.index("function apRestoreRevision")]
    assert "idx === 0" in renderer
    assert "Diff vs current" in renderer
    assert "_apTable.access_policy_sql" in renderer
    assert "_apTable.policy_mapping" in renderer


def test_policy_mapping_toggle_renders_as_a_one_line_flag_change(seeded_app):
    """``policy_mapping`` is a boolean, not a line-diffable body — it earns
    its own sentence rather than hiding inside (or being silently dropped
    from) the SQL diff."""
    c = seeded_app["client"]
    body = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
    assert "function _apMappingChangeLine" in body
    mapping_fn = body[body.index("function _apMappingChangeLine") :]
    mapping_fn = mapping_fn[: mapping_fn.index("function _apRenderRevisionDiff")]
    assert "Policy mapping:" in mapping_fn
    assert "ap-history-mapping-change" in mapping_fn
    assert "escapeHtml(_apMappingLabel(" in mapping_fn


def test_diff_styles_use_ds_tokens_not_raw_hex(seeded_app):
    """Design-system contract, same shape as the existing history-row
    styling test: classes only, --ds-* tokens only, no raw hex."""
    c = seeded_app["client"]
    body = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
    for cls in (".ap-diff-add", ".ap-diff-del", ".ap-history-diff", ".ap-history-mapping-change"):
        assert cls + " {" in body, f"missing CSS rule for {cls}"
    block = body[body.index(".ap-history-diff-details {") :]
    block = block[: block.index(".ap-history-first {")]
    assert "var(--ds-" in block
    assert "#" not in block, "raw hex colour in the new diff styles"
    # The two semantic colours per the design-system playbook's status
    # vocabulary — never a hand-picked green/red.
    assert "--ds-accent-success" in block
    assert "--ds-accent-danger" in block


def test_audit_fallback_notes_diffing_is_unavailable_on_a_501(seeded_app):
    """``access_policy_revisions`` is PG-only (A3). A DuckDB-backed instance
    gets a typed 501 from the revisions endpoint, and the panel degrades to
    the audit-derived, read-only list — which cannot diff, because
    ``audit_log.params`` never carried a body. Say so once, not silently."""
    c = seeded_app["client"]
    body = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
    loader = body[body.index("async function _apLoadHistory(") :]
    loader = loader[: loader.index("function _apSqlPeek")]
    assert "revisionsUnavailable" in loader
    assert "rr.status === 501" in loader

    fallback = body[body.index("async function _apLoadHistoryFromActivity") :]
    fallback = fallback[: fallback.index("function _apParseAuditParams")]
    assert "revisionsUnavailable" in fallback
    assert "cannot store" in fallback
    assert "ap-history-nodiff" in fallback
    assert "apRestoreRevision(" not in fallback, "the fallback still carries no restore/diff action"


class TestDiffAlgorithmUnderNode:
    """Runs the SHIPPED ``_apDiffLines`` under node rather than restating
    its rules in Python — a Python transcription would pass whatever the
    rules happen to be, which is exactly the failure mode this class exists
    to catch (same rationale as ``test_preview_error_names_the_reason.py``).
    """

    @staticmethod
    def _extract_diff_snippet() -> str:
        text = _template_text()
        start = text.index("var _AP_DIFF_MAX_LINES")
        end = text.index("function _apRenderRevisions(body)")
        return text[start:end]

    def _run(self, expression: str):
        node = shutil.which("node")
        if not node:
            pytest.skip("node not available")
        # `escapeHtml` in the shipped template goes through the DOM
        # (`document.createElement('div').textContent = ...; .innerHTML`).
        # This stub reproduces exactly what that round-trip does for plain
        # text (escape &, <, >) — nothing about the DIFF LOGIC under test is
        # reimplemented here, only the browser API it calls into.
        shim = (
            "var document = { createElement: function() { "
            "  var v = ''; return { set textContent(s) { v = String(s)"
            ".replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }, "
            "  get innerHTML() { return v; } }; } };\n"
        )
        script = shim + self._extract_diff_snippet() + "\nprocess.stdout.write(JSON.stringify(" + expression + "));\n"
        out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=False)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)

    def test_a_changed_line_is_one_del_and_one_add(self):
        ops = self._run("_apDiffLines('SELECT 1', 'SELECT 2')")
        assert ops == [{"type": "del", "text": "SELECT 1"}, {"type": "add", "text": "SELECT 2"}]

    def test_a_shared_prefix_line_survives_as_same(self):
        ops = self._run("_apDiffLines('A\\nB', 'A\\nC')")
        assert ops[0] == {"type": "same", "text": "A"}
        assert {"type": "del", "text": "B"} in ops
        assert {"type": "add", "text": "C"} in ops

    def test_identical_text_has_no_add_or_del(self):
        ops = self._run("_apDiffLines('SAME', 'SAME')")
        assert ops == [{"type": "same", "text": "SAME"}]

    def test_clearing_a_policy_diffs_as_every_line_removed_with_no_stray_add(self):
        """Regression: `''.split('\\n')` is `['']`, not `[]` — an earlier
        version of this diffed a cleared policy as "every line removed PLUS
        one blank line added", which is wrong."""
        ops = self._run("_apDiffLines('SELECT 1\\nWHERE x = 1', '')")
        assert all(op["type"] == "del" for op in ops)
        assert [op["text"] for op in ops] == ["SELECT 1", "WHERE x = 1"]

    def test_oversized_input_returns_null_rather_than_diffing(self):
        big = "\\n".join(f"line{i}" for i in range(500))
        result = self._run(f"_apDiffLines('{big}', '{big}x')")
        assert result is None


# ── #1979 reviewer finding 1: the Builder tab must not describe a policy it
# cannot represent ────────────────────────────────────────────────────────
#
# The Builder starts empty on every open (there is no reverse-compiler from
# stored SQL back into rules), so a policy authored on the Advanced SQL tab
# used to render as "No row rules — every caller sees every row" while a real
# restrictive policy was being enforced. Authorship is TRACKED
# (`_apSqlFromBuilder`), never re-derived by parsing SQL.


def test_builder_says_when_a_policy_cannot_be_shown_as_rules(seeded_app):
    """A stored policy the builder did not author renders an explicit
    "cannot be shown as rules" block in the Builder tab, with a control that
    hands the admin to the Advanced SQL tab — not the empty-rules copy."""
    c = seeded_app["client"]
    body = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
    assert 'id="apNotRepresentableNotice"' in body
    assert "function _apRenderRepresentationNotice" in body
    fn = body[body.index("function _apRenderRepresentationNotice") : body.index("function _apAddRowRule")]
    assert "cannot be shown as rules" in fn
    # A stored policy is "enforced as written"; SQL typed into the box but not
    # saved yet is not enforced at all, and must not claim to be.
    assert "It is enforced as" in fn
    assert "It is what Save policy will store" in fn
    assert "Advanced SQL" in fn
    assert "apSwitchTab(\\'sql\\')" in fn or 'apSwitchTab(\\"sql\\")' in fn


def test_the_not_representable_notice_uses_the_existing_warn_block(seeded_app):
    """Mirrors #apFlagDisabledNotice / #apInterlockWarning — the same
    design-system warn block, no inline colours, no new component."""
    c = seeded_app["client"]
    body = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
    at = body.index('id="apNotRepresentableNotice"')
    tag = body[body.rindex("<div", 0, at) : body.index(">", at) + 1]
    assert 'class="form-hint form-hint--warn"' in tag, tag
    assert "color" not in tag and "background" not in tag, f"the notice must style through the shared class: {tag}"

    fn = body[body.index("function _apRenderRepresentationNotice") : body.index("function _apAddRowRule")]
    assert "style=" not in fn, "the notice body must not carry inline styles"


def test_builder_authorship_is_tracked_not_reparsed(seeded_app):
    """No SQL→rules parser: the modal remembers whether a compile put the
    current body in the box. Open-with-a-stored-policy = not authored here;
    a successful compile = authored here; a hand edit = not authored here."""
    c = seeded_app["client"]
    body = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
    assert "function _apBuilderDescribesSql" in body

    opener = body[body.index("function openAccessPolicyModal") : body.index("function closeAccessPolicyModal")]
    assert "_apStoredSqlAtOpen" in opener
    assert "_apSqlFromBuilder = !_apStoredSqlAtOpen" in opener
    assert "_apRenderRepresentationNotice()" in opener

    compile_fn = body[body.index("async function _apCompileNow") : body.index("function _apRenderCompileWarnings")]
    assert "_apSqlFromBuilder = true" in compile_fn

    edited = body[body.index("function apSqlEdited") : body.index("function _apSqlFiltersOrMasksNothing")]
    assert "_apSqlFromBuilder = false" in edited
    assert "_apRenderRepresentationNotice()" in edited


def test_empty_rules_copy_is_suppressed_when_the_builder_cannot_show_the_policy(seeded_app):
    """"No row rules — every caller sees every row" is only true when these
    rules ARE the policy; the notice replaces it otherwise."""
    c = seeded_app["client"]
    body = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
    renderer = body[body.index("function _apRenderRowRules") : body.index("function _apAddRowRule")]
    assert "No row rules — every caller sees every row." in renderer
    head = renderer[: renderer.index("No row rules")]
    assert "_apBuilderDescribesSql()" in head, "the empty-rules copy must be gated on the builder describing the body"


def test_save_skips_the_filters_nothing_gate_when_re_saving_an_untouched_policy(seeded_app):
    """#1430's "filters nothing → Save anyway?" gate judges what the admin
    authored. Re-saving, verbatim, a stored policy the builder never authored
    (the #1979 case) must not be second-guessed by it."""
    c = seeded_app["client"]
    body = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
    assert "function _apSqlIsUntouchedStoredPolicy" in body
    fn = body[body.index("async function apSavePolicy") : body.index("async function apClearPolicy")]
    assert "if (sql && !_apSqlIsUntouchedStoredPolicy() && _apSqlFiltersOrMasksNothing(sql))" in fn


def test_save_confirms_before_builder_rules_replace_a_hand_written_policy(seeded_app):
    """The one destructive path: the admin adds a rule while a SQL-authored
    policy is stored, and saving would overwrite that SQL with the rules."""
    c = seeded_app["client"]
    body = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
    assert "function _apBuilderWouldReplaceStoredSql" in body
    fn = body[body.index("async function apSavePolicy") : body.index("async function apClearPolicy")]
    assert "_apBuilderWouldReplaceStoredSql()" in fn
    assert "REPLACE the SQL policy" in fn
    assert "confirmModal(" in fn
    assert "Replace policy" in fn
    # The replace question is asked BEFORE the no-op nudge: it is the
    # destructive one.
    assert fn.index("_apBuilderWouldReplaceStoredSql()") < fn.index("_apSqlFiltersOrMasksNothing(sql)")


def test_builder_warns_inline_before_rules_replace_a_stored_sql_policy(seeded_app):
    """Not only at save time — the Builder tab says so while the admin is
    still editing, in the same warn block."""
    c = seeded_app["client"]
    body = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
    fn = body[body.index("function _apRenderRepresentationNotice") : body.index("function _apAddRowRule")]
    assert "_apBuilderWouldReplaceStoredSql()" in fn
    assert "REPLACE the SQL policy" in fn


def test_switching_back_to_the_builder_refreshes_the_representation_state(seeded_app):
    c = seeded_app["client"]
    body = c.get("/admin/tables", headers=_auth(seeded_app["admin_token"])).text
    fn = body[body.index("function apSwitchTab") : body.index("var _AP_MASK_LABELS")]
    assert "_apRenderRepresentationNotice()" in fn
