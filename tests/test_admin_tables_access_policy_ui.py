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


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_access_column_header_present(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/admin/tables", headers=_auth(token))
    assert r.status_code == 200, r.text
    assert "'<th>Access</th>'" in r.text


def test_access_column_renders_three_states(seeded_app):
    """``renderAccessPolicyChip`` emits: (1) a plain "—" for a table that
    could carry a policy but doesn't, (2) a muted "not available —
    distributed" for a table that isn't eligible (not remote/server_only),
    and (3) a tinted "Policy" chip for a table that carries one."""
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
    assert "action_prefix=update_table" in body


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
