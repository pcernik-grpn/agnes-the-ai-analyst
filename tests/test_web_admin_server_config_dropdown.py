"""`/admin/server-config`'s `.ds-dropdown` conversion, held to the same bar as
its ~9 predecessors.

Every other page that swapped a native `<select>` for the branded component
shipped a markup test (`test_web_admin_mcp_sources_dropdown.py`,
`..._sessions_dropdown.py`, `test_web_activity_center_dropdown.py`,
`test_user_management.py`, …). This one converts a page whose fields are built
client-side from an async fetch, which is the case the load-time bootstrap
cannot reach — so the wiring has more moving parts, not fewer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_TPL = Path("app/web/templates/admin_server_config.html")
_JS = Path("app/web/static/js/components/ds_dropdown.js")


@pytest.fixture(scope="module")
def tpl() -> str:
    return _TPL.read_text()


def test_the_component_assets_are_loaded(tpl):
    assert "css/ds_dropdown.css" in tpl, "the stylesheet belongs in head_extra"
    assert "js/components/ds_dropdown.js" in tpl


def test_the_markup_pairs_a_native_select_to_the_custom_host(tpl):
    """The native `<select>` stays in the DOM and remains the value carrier —
    it is what existing `change` listeners and the save path read."""
    assert 'class="ds-dropdown-native"' in tpl
    assert 'data-ds-dropdown-target="${fieldId}"' in tpl


def test_client_built_hosts_are_initialized_explicitly(tpl):
    """`bootstrapAll` runs at DOMContentLoaded; these fields do not exist
    until `loadConfig` resolves, so they need the exported hook."""
    assert "dsDropdownInit" in tpl


def test_the_visible_control_carries_an_accessible_name(tpl):
    """The field's `<label for>` points at the NATIVE select, which
    `paper-skin.css` hides on the default theme — so without a name span the
    button announces only its current value ("true") with no field identity.
    Same shape `_components.html`'s `dropdown` macro uses for `aria_label`."""
    assert 'class="ds-dropdown-name"' in tpl, "no visually-hidden field-name span"
    assert "aria-labelledby=" in tpl, "the button must concatenate name + value"


def test_init_installs_no_per_host_document_listeners():
    """`renderAll()` re-inits every dropdown on every save, and `init` has no
    teardown — so a `document.addEventListener` inside it added two permanent
    listeners per dropdown per save (40+ fields on this page) and pinned the
    detached DOM they closed over. Esc and outside-click belong at module
    scope, resolving the open menu at event time."""
    src = _JS.read_text()
    body = src[src.index("function init(host)") : src.index("function bootstrapAll")]
    # The module-scope handlers live after init() in source order, so slice to
    # the registry declaration rather than to bootstrapAll.
    body = body[: body.index("const _closers")]
    assert "document.addEventListener(" not in body, (
        "init() must not add document-level listeners per host — "
        "admin_server_config.html re-inits them on every save"
    )
    assert "_closers.set(host, close)" in body, "the host's closer must be registered instead"
