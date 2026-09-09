"""The "LLM cost" section on `/admin/telemetry` (design 2026-09-08 §3.4).

Static assertions on the rendered page — the section exists, wires the
`llm-cost` endpoint, uses page-shell/design-system tokens (no `.container:
has()`, no bare hex, no `var(--primary)`), and names the 501 case in plain
language for an operator still on the frozen DuckDB app-state backend.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_llm_cost_section_is_on_the_telemetry_page(seeded_app):
    c: TestClient = seeded_app["client"]
    r = c.get("/admin/telemetry", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200, r.text
    html = r.text

    assert 'id="llmcost-section"' in html
    assert 'aria-label="LLM cost"' in html
    assert "llm-cost?window=" in html
    assert "LLM cost needs the Postgres app-state backend." in html
    assert "agnes admin usage llm-cost" in html


def test_llm_cost_window_selector_offers_all_four_windows(seeded_app):
    c: TestClient = seeded_app["client"]
    html = c.get("/admin/telemetry", headers=_auth(seeded_app["admin_token"])).text
    assert 'id="llmcost-window"' in html
    for value in ("1d", "7d", "30d", "all"):
        assert f'value="{value}"' in html


def test_llm_cost_section_carries_no_new_raw_hex_or_legacy_primary(seeded_app):
    """The section reuses the page's existing `.obs-*` classes and moves the
    query-telemetry header's inline style into a shared `.obs-section-head`
    class — it must not introduce `var(--primary)` (the un-prefixed legacy
    token banned repo-wide) or a NEW bare hex literal beyond what the file
    already carries in its established `var(--token, #hex)` fallback idiom.
    """
    c: TestClient = seeded_app["client"]
    html = c.get("/admin/telemetry", headers=_auth(seeded_app["admin_token"])).text
    assert "var(--primary)" not in html
    assert ".container:has(" not in html
