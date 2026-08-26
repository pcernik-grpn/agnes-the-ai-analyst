"""The migration UI's "not yet available" set must equal the API's 501 set.

`/admin/database` is opened by an operator on a Postgres-backed instance —
`side_car`, `cloud` or `duckdb_quack` — and `duckdb` is an allowed transition
from every one of them (`src/db_state_machine.py`). The endpoint refuses it
with 501 alongside `duckdb_quack`, but the page only disabled the latter, so
the operator saw an ENABLED button labelled with the raw enum (`Migrate to
duckdb`), confirmed an irreversible-cutover warning, and got a 501.

Comparing the two sets rather than asserting a literal is what keeps this
honest when reverse-to-DuckDB support actually lands: the test then fails on
whichever side was not updated.
"""

from __future__ import annotations

import re
from pathlib import Path

_API = Path("app/api/db_state.py")
_JS = Path("app/web/static/js/admin/db_state.js")


def _api_501_targets() -> set[str]:
    src = _API.read_text()
    m = re.search(r"if payload\.target in \(([^)]*)\):", src)
    assert m, "the 501 guard in app/api/db_state.py changed shape"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def _ui_disabled_targets() -> set[str]:
    src = _JS.read_text()
    body = src[src.index("_isNotYetSupported(target) {") :]
    body = body[: body.index("},")]
    return set(re.findall(r"'([^']+)'", body))


def test_ui_disables_every_target_the_endpoint_501s():
    api, ui = _api_501_targets(), _ui_disabled_targets()
    assert ui == api, f"UI disables {sorted(ui)} but the endpoint 501s {sorted(api)}"


def test_no_disabled_target_reaches_the_button_label_fallback():
    """`_transitionLabel`'s fallback is `Migrate to ${target}` — the raw enum.
    Every target the UI disables must have a human label, or the page shows an
    internal identifier to an operator."""
    src = _JS.read_text()
    fn = src[src.index("_transitionLabel(backend, target) {") :]
    fn = fn[: fn.index("\n  },")]
    for target in _ui_disabled_targets():
        assert f"'{target}'" in fn, f"{target} falls through to the raw-enum label"


def test_the_legacy_card_disables_them_too():
    """The legacy `#db-state-card` branch renders its own buttons and wires its
    own clicks; a clickable disabled-target button there fails identically."""
    src = _JS.read_text()
    branch = src[src.index("const legacyEl = document.getElementById('db-state-card');") :]
    branch = branch[: branch.index("\n    }\n")]
    assert "b.disabled" in branch, "legacy buttons must carry the disabled attribute"
    assert "button[data-target]:not([disabled])" in branch, "legacy clicks must skip disabled buttons"


def test_the_confirm_dialog_does_not_claim_reverse_migration_is_impossible():
    """`src/db_state_machine.py` documents `SIDE_CAR / CLOUD -> DUCKDB` and
    CLAUDE.md retires the forward-only framing. "There is no path back" is
    wrong in principle, and a wrong absolute in a confirmation dialog talks an
    operator out of a supported migration."""
    src = _JS.read_text()
    assert "there is no path back to DuckDB" not in src
    assert "not implemented yet" in src
