"""B2: `/admin/data-sources` wizard — Keboola table-step mode choice + a
post-registration sync trigger.

Two gaps, verified against `app/web/templates/admin_data_sources.html`:

1. The per-row Keboola mode select (`data-kb-mode`) offered only
   `materialized` / `remote` ("live") — no `local` (Storage API direct
   extract), even though the register endpoint and `_buildKeboolaPayload` in
   the `/admin/tables` modal both support it.
2. The wizard never called `POST /api/sync/trigger` after registering
   tables — a newly-registered `local`/`materialized` row sat unsynced until
   the next scheduled tick.

Template-level assertions (no headless browser) — the same style already
used by `tests/test_admin_data_sources_page.py::TestWizardRegisterPayloadContract`.
"""

from __future__ import annotations

from pathlib import Path


def _template_text() -> str:
    tpl = Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_data_sources.html"
    return tpl.read_text(encoding="utf-8")


def _picker_row(tpl: str) -> str:
    return tpl.split('data-table-bare="${_esc(bare)}"', 1)[1].split("ds-bucket-group", 1)[0]


def test_keboola_mode_select_offers_local_alongside_materialized_and_remote():
    """The per-row select must offer all three query_modes Keboola supports
    end-to-end: materialized (default), remote (live), and local (direct
    extract via the Storage API — no server-scheduled copy, incremental
    support). Pre-fix it offered only materialized/remote."""
    tpl = _template_text()
    picker_row = _picker_row(tpl)
    assert "data-kb-mode" in picker_row
    assert '<option value="materialized" selected>' in picker_row
    assert '<option value="remote">' in picker_row
    assert '<option value="local">' in picker_row


def test_shared_sync_trigger_helper_posts_to_the_trigger_endpoint():
    """`_triggerWizardSync` is the one place a `POST /api/sync/trigger`
    fire-and-forget call may live — both finish paths below call it rather
    than duplicating the fetch."""
    tpl = _template_text()
    start = tpl.index("function _triggerWizardSync(")
    end = tpl.index("\nfunction ", start + len("function _triggerWizardSync("))
    body = tpl[start:end]

    assert '"/api/sync/trigger"' in body
    assert 'method: "POST"' in body


def test_wizard_done_slab_triggers_a_sync():
    """The wizard's finish step (`_showDoneSlab`, reached via Share&finish or
    Skip sharing) must call the shared sync-trigger helper so newly-registered
    tables don't wait for the next scheduled tick, and show a visible
    'Sync started' note in the success state."""
    tpl = _template_text()
    start = tpl.index("function _showDoneSlab(")
    end = tpl.index("\nfunction ", start + len("function _showDoneSlab("))
    body = tpl[start:end]

    assert "_triggerWizardSync();" in body
    assert "Sync started" in body


def test_wizard_early_finish_also_triggers_a_sync():
    """ "Register only & finish" (the step-2 escape hatch) is a second,
    mutually-exclusive way to end the wizard — it must trigger a sync too,
    not just the Share-step path."""
    tpl = _template_text()
    start = tpl.index('document.getElementById("ds-wizard-finish-early-btn").addEventListener(')
    end = tpl.index("\n});", start) + len("\n});")
    body = tpl[start:end]

    assert "_triggerWizardSync();" in body
