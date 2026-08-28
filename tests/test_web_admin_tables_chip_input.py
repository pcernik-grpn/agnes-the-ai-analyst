"""/admin/tables — Data Packages chip-input wiring (Task 8.8).

Smoke-level: assert the chip-input host element appears inside an EDIT
modal and points at the right source endpoint. Full chip-input +
form-submit wiring needs Playwright; documented as a follow-up.

D4 — one registration flow: the register modals this test originally
checked (`#registerBqModal` et al.) are gone, and the shared register
drawer (`_register_table_form.html`) does NOT carry a Data Packages
chip-input — it never actually forwarded `package_ids` into the register
call in the first place (see the comment this file's assertions used to
sit next to: "NOT wired to forward `package_ids` to /api/admin/tables in
this pass"), so dropping the decorative, non-functional field is not a
capability loss. Package assignment during onboarding/registration stays
a documented follow-up; an admin still attaches packages post-registration
via the package admin UI, the CLI, or an edit modal's chip-input, which
this test still covers.
"""


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_admin_tables_renders_chip_input_for_data_packages(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.get("/admin/tables", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.text
    # Chip-input mount + source endpoint inside the BQ EDIT modal
    # (unchanged by D4).
    assert 'data-source-url="/api/admin/data-packages"' in body
    assert 'data-allow-create="true"' in body
    assert 'data-name="bq_edit_package_ids"' in body
    # chip-input.js loaded via admin_tables.html's extra_scripts block.
    assert "/static/js/components/chip-input.js" in body
    # The shared register drawer has no chip-input host of its own.
    assert 'data-name="bq_package_ids"' not in body
