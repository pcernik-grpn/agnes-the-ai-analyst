"""TCRD-227: "Reset to default" must not destroy an override on one click.

The button issued the DELETE immediately. On the 2026-08-28 walkthrough the
customer's read was verbatim: "když klikneš na reset to default, tak všechno
je v píči" — one click replaces a tuned install/workspace prompt with the
shipped default, no confirmation, nothing recoverable.

Template-contract test in the house style: the reset handler must go through
`window.confirmModal` (the design-system confirm every other destructive
admin action uses — see admin_package_detail.html) before calling the API.
"""

from __future__ import annotations

from pathlib import Path


def test_reset_handler_confirms_before_deleting():
    text = Path("app/web/templates/admin_prompts.html").read_text(encoding="utf-8")
    handler = text.split('data-action="reset"]\').addEventListener')[1].split("card.querySelector")[0]
    assert "confirmModal" in handler, (
        "the reset handler must confirm before the DELETE — one click "
        "currently destroys the operator's override irrecoverably"
    )
    assert handler.index("confirmModal") < handler.index("api("), "the confirm must come before the API call, not after"
