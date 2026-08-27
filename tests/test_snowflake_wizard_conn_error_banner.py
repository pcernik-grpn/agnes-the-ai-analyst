"""#1616: the Snowflake add-data wizard's step 2 rendered a broken
connection's 502 `detail` as a bare line of body text — no icon/banner
styling, and the rest of the step (Reload tables, manual table-entry rows,
"Continue with selected tables") stayed fully interactive even though
nothing past step 1 can succeed until the credential is fixed. The only way
out was to notice the buried sentence and manually re-open step 1.

Verified against `app/web/templates/admin_data_sources.html` — the same
template-level-assertion style already used by
`tests/test_keboola_wizard_mode_and_sync_trigger.py` and
`tests/test_snowflake_discovery.py::test_the_snowflake_picker_prepares_its_status_lines_before_registering`.
There is no JS/template test runner in this repo (no jest/playwright config),
so these assertions pin behaviour at the source-text level rather than by
executing the script.
"""

from __future__ import annotations

from pathlib import Path


def _template_text() -> str:
    tpl = Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_data_sources.html"
    return tpl.read_text(encoding="utf-8")


def _function_body(tpl: str, signature: str) -> str:
    start = tpl.index(signature)
    end = tpl.index("\nfunction ", start + len(signature))
    return tpl[start:end]


def test_error_banner_markup_exists_with_fix_connection_button():
    """The banner needs its own error-toned container (not the plain-text
    `.ds-wizard-error` used elsewhere) plus a "Fix connection" action."""
    tpl = _template_text()
    assert 'id="ds-sf-conn-error"' in tpl
    assert 'id="ds-sf-conn-error-fix"' in tpl
    fix_btn = tpl[tpl.index('id="ds-sf-conn-error-fix"') - 200 : tpl.index('id="ds-sf-conn-error-fix"') + 200]
    assert "Fix connection" in fix_btn


def test_fix_connection_button_navigates_back_to_step_1():
    """No new page/route — it reuses the wizard's own step navigation, the
    same primitive the steps-strip back button already uses."""
    tpl = _template_text()
    body = _function_body(tpl, "function _renderSfRowsEditor(")
    idx = body.index('id="ds-sf-conn-error-fix"')
    window = body[idx:]
    assert "_setWizStep(1)" in window


def test_load_sf_catalog_sets_conn_error_state_on_failure_with_server_message():
    tpl = _template_text()
    body = _function_body(tpl, "async function _loadSfCatalog(")
    assert "_setSfConnErrorState(true" in body
    # The banner must show the server's classified `detail`/`message`, the
    # same lookup the old plain-text error element used.
    assert "body.detail" in body and "body.message" in body


def test_load_sf_catalog_clears_conn_error_state_on_a_fresh_attempt():
    """Reload (or the auto-load on step entry) must reset the error banner
    and the disabled controls before the new request lands — otherwise a
    successful retry after "Fix connection" would stay stuck disabled."""
    tpl = _template_text()
    body = _function_body(tpl, "async function _loadSfCatalog(")
    assert "_setSfConnErrorState(false)" in body


def test_conn_error_state_disables_reload_and_manual_entry_and_register():
    """Nothing past step 1 can work while the connection can't authenticate
    — Reload tables, the manual schema/table rows (+ "Add another table"),
    and the footer's register / register-only buttons must all go inert."""
    tpl = _template_text()
    body = _function_body(tpl, "function _setSfConnErrorState(")
    assert "ds-sf-browse" in body  # Reload tables button
    assert "ds-wizard-register-btn" in body  # Continue with selected tables
    assert "ds-wizard-finish-early-btn" in body  # Register only & finish
    assert "disabled" in body
    # The register-mode select's branded dropdown button — the native select
    # alone does not stop clicks on the paper-theme overlay button.
    assert "ds-sf-picker-mode-dd-btn" in body


def test_pointing_the_wizard_at_a_source_releases_the_conn_error_lock():
    """Regression (Devin Review on #1635): two of the controls the lock
    disables — `ds-wizard-register-btn` / `ds-wizard-finish-early-btn` — live
    in the drawer's SHARED sticky foot, reused by the Keboola / BigQuery /
    Databricks step 2. Only `_loadSfCatalog` ever cleared the lock, and only
    on the Snowflake path, while `openWizard`/`closeWizard` reset neither the
    buttons' `disabled` flags nor `_sfConnErrorActive` — so one failed
    Snowflake listing left "Continue with selected tables" and "Register only
    & finish" dead for every other source until a full page reload.

    `_setWizSource` is the funnel every wizard entry goes through
    (`openWizard` ends with it, the connector picker calls it, the
    steps-strip's return to step 1 calls it), so releasing the lock there
    covers reopen AND switching source inside an already-open wizard.
    """
    tpl = _template_text()
    body = _function_body(tpl, "function _setWizSource(")
    assert "_setSfConnErrorState(false)" in body, (
        "_setWizSource must release the Snowflake connection-error lock — it "
        "disables step-2 footer buttons shared with every other source"
    )


def test_open_wizard_routes_through_the_source_funnel_that_clears_the_lock():
    """The release above is only load-bearing for a REOPEN because
    `openWizard` ends by pointing the wizard at a source. Pin that call so a
    refactor cannot drop it and silently restore the stuck-buttons bug.
    """
    tpl = _template_text()
    body = _function_body(tpl, "function openWizard(")
    assert "_setWizSource(" in body


def test_conn_error_banner_reuses_the_design_system_danger_tokens():
    """Proper error styling, not the plain highlighted-line-of-text look the
    issue called out — same danger color tokens the rest of the page already
    uses (`.ds-src__health.is-err`, `.ds-conn-test-result.fail`, ...)."""
    tpl = _template_text()
    css_start = tpl.index(".ds-sf-conn-error")
    css_block = tpl[css_start : css_start + 400]
    assert "var(--ds-accent-danger" in css_block
