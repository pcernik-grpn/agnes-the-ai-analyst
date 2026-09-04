"""``PATCH /api/admin/sharepoint/connections/{id}/extraction/crawl-config`` —
per-connection age filter override for ``extraction.crawl.min_modified``: a
190k-document backfill run can crawl only what changed on/after a cutoff
date instead of re-walking the whole corpus.

The resolver itself (``connectors.sharepoint.crawler.resolve_min_modified``
— an ISO date on the connection row, or unfiltered when absent/invalid) is
unit-tested in ``tests/test_sharepoint_crawler.py``; the crawler gate itself
(boundary rule, missing-timestamp handling, deleted-item exemption) lives
there too. This module covers the HTTP surface: RBAC, the write, the
resolved-value response shape, validation, and the audit row — the exact
sibling of ``tests/test_extraction_facts_config.py``.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests._admin_data_sources_source import read_admin_data_sources_source

BASE = "/api/admin/sharepoint/connections"
TEMPLATE = Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_data_sources.html"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _extract_block(text: str, opener: str) -> str:
    """The brace-balanced body of one declaration, from its signature —
    same helper `test_admin_data_sources_extraction.py` uses, except that the
    braces INSIDE the opener are netted out before the scan continues past
    it: a signature with a destructured parameter (`({ a, b } = {}) {`)
    closes a brace pair of its own before the body even opens, and counting
    from the opener's first character would stop right there."""
    start = text.index(opener)
    depth = opener.count("{") - opener.count("}")
    started = depth > 0
    for i in range(start + len(opener), len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
            started = True
        elif ch == "}":
            depth -= 1
            if started and depth == 0:
                return text[start : i + 1]
    raise AssertionError(f"unbalanced braces extracting {opener!r}")


def _run_node(script: str) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as f:
        f.write(script)
        path = f.name
    try:
        proc = subprocess.run(["node", path], capture_output=True, text=True)
    finally:
        Path(path).unlink(missing_ok=True)
    if proc.returncode == 127:
        pytest.skip("node unavailable")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


def _create_connection(client, token, *, name="corp-sharepoint-crawl-config"):
    resp = client.post(
        "/api/admin/source-connections",
        json={
            "name": name,
            "source_type": "sharepoint",
            "config": {"tenant_id": "tenant-1", "client_id": "client-1"},
        },
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


class TestAuthGating:
    def test_requires_auth(self, seeded_app):
        r = seeded_app["client"].patch(f"{BASE}/nope/extraction/crawl-config", json={"min_modified": "2023-12-31"})
        assert r.status_code == 401

    def test_requires_admin(self, seeded_app):
        token = seeded_app["analyst_token"]
        r = seeded_app["client"].patch(
            f"{BASE}/nope/extraction/crawl-config", json={"min_modified": "2023-12-31"}, headers=_auth(token)
        )
        assert r.status_code == 403


class TestUnknownConnection:
    def test_unknown_connection_is_404(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        r = client.patch(
            f"{BASE}/does-not-exist/extraction/crawl-config",
            json={"min_modified": "2023-12-31"},
            headers=_auth(token),
        )
        assert r.status_code == 404
        assert r.json()["detail"] == "connection_not_found"

    def test_non_sharepoint_connection_is_404(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        created = client.post(
            "/api/admin/source-connections",
            json={
                "name": "kbc-crawl-config",
                "source_type": "keboola",
                "config": {"stack_url": "https://connection.example.com"},
            },
            headers=_auth(token),
        )
        assert created.status_code == 201, created.text
        r = client.patch(
            f"{BASE}/{created.json()['id']}/extraction/crawl-config",
            json={"min_modified": "2023-12-31"},
            headers=_auth(token),
        )
        assert r.status_code == 404


class TestPatch:
    def test_setting_the_override_returns_it_as_the_resolved_source(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token)

        r = client.patch(
            f"{BASE}/{conn_id}/extraction/crawl-config", json={"min_modified": "2023-12-31"}, headers=_auth(token)
        )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["connection_id"] == conn_id
        assert body["min_modified"] == {"value": "2023-12-31", "source": "connection"}

    def test_the_override_is_actually_written_to_the_connection_row(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-crawl-config-write")

        client.patch(
            f"{BASE}/{conn_id}/extraction/crawl-config", json={"min_modified": "2023-12-31"}, headers=_auth(token)
        )

        from src.repositories import source_connections_repo

        row = source_connections_repo().get(conn_id)
        assert row["config"]["extraction"]["crawl"]["min_modified"] == "2023-12-31"
        # A shallow PATCH of `extraction` must not clobber sibling connect-
        # wizard config already on the row.
        assert row["config"]["tenant_id"] == "tenant-1"

    def test_a_null_min_modified_clears_a_previously_set_override(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-crawl-config-clear")
        client.patch(
            f"{BASE}/{conn_id}/extraction/crawl-config", json={"min_modified": "2023-12-31"}, headers=_auth(token)
        )

        r = client.patch(f"{BASE}/{conn_id}/extraction/crawl-config", json={}, headers=_auth(token))

        assert r.status_code == 200, r.text
        assert r.json()["min_modified"] == {"value": None, "source": "none"}

        from src.repositories import source_connections_repo

        row = source_connections_repo().get(conn_id)
        assert "crawl" not in (row["config"].get("extraction") or {})

    def test_an_invalid_min_modified_is_refused_with_400(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-crawl-config-invalid")

        r = client.patch(
            f"{BASE}/{conn_id}/extraction/crawl-config", json={"min_modified": "not-a-date"}, headers=_auth(token)
        )

        assert r.status_code == 400
        assert r.json()["detail"] == "invalid_min_modified"

    def test_works_regardless_of_sharepoint_enabled(self, seeded_app, monkeypatch):
        monkeypatch.delenv("AGNES_SHAREPOINT_ENABLED", raising=False)
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-crawl-config-disabled")

        r = client.patch(
            f"{BASE}/{conn_id}/extraction/crawl-config", json={"min_modified": "2023-12-31"}, headers=_auth(token)
        )

        assert r.status_code == 200


class TestCrawlSchedulePatch:
    """D.16 — ``schedule`` on the SAME PATCH endpoint, with a DIFFERENT
    omitted-vs-null contract than ``min_modified`` (``model_fields_set``,
    the ``…/facts-config`` pattern): omitted leaves ``schedule`` untouched,
    ``null`` clears it back to the instance default. ``min_modified`` itself
    keeps its ORIGINAL "omitted == cleared" contract unchanged — a caller
    that wants to touch one field without disturbing the other must resend
    the OTHER field's current value explicitly (the CLI and the source-card
    panel both do); this endpoint itself does not protect against a bare
    single-field body clobbering the sibling, same posture ``…/facts-
    config``'s own ``retry_mode``/``transport`` pair already has."""

    def test_setting_off_returns_it_resolved_with_no_next_run(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-crawl-schedule-off")

        r = client.patch(f"{BASE}/{conn_id}/extraction/crawl-config", json={"schedule": "off"}, headers=_auth(token))

        assert r.status_code == 200, r.text
        assert r.json()["schedule"] == {"value": "off", "source": "connection", "next_run_at": None}

    def test_setting_an_interval_is_written_and_resolved(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-crawl-schedule-interval")

        r = client.patch(
            f"{BASE}/{conn_id}/extraction/crawl-config", json={"schedule": "every 6h"}, headers=_auth(token)
        )

        assert r.status_code == 200, r.text
        schedule = r.json()["schedule"]
        assert schedule["value"] == "every 6h"
        assert schedule["source"] == "connection"
        # Never run before -> due immediately, so `next_due_at` resolves to
        # "now" rather than None (see `src.scheduler.next_due_at`).
        assert schedule["next_run_at"] is not None

        from src.repositories import source_connections_repo

        row = source_connections_repo().get(conn_id)
        assert row["config"]["extraction"]["crawl"]["schedule"] == "every 6h"

    def test_an_invalid_schedule_is_refused_with_400(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-crawl-schedule-invalid")

        r = client.patch(
            f"{BASE}/{conn_id}/extraction/crawl-config", json={"schedule": "sometimes"}, headers=_auth(token)
        )

        assert r.status_code == 400
        assert r.json()["detail"] == "invalid_crawl_schedule"

    def test_omitted_schedule_leaves_a_previously_set_one_untouched(self, seeded_app):
        """A ``min_modified``-only Save (the existing date-filter control)
        must not reset an already-configured ``schedule`` back to
        ``instance`` — the footgun the ``model_fields_set`` contract exists
        to avoid."""
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-crawl-schedule-persists")
        client.patch(f"{BASE}/{conn_id}/extraction/crawl-config", json={"schedule": "off"}, headers=_auth(token))

        r = client.patch(
            f"{BASE}/{conn_id}/extraction/crawl-config", json={"min_modified": "2023-12-31"}, headers=_auth(token)
        )

        assert r.status_code == 200, r.text
        assert r.json()["schedule"]["value"] == "off"

        from src.repositories import source_connections_repo

        row = source_connections_repo().get(conn_id)
        assert row["config"]["extraction"]["crawl"]["schedule"] == "off"

    def test_a_bare_schedule_only_body_still_clears_min_modified(self, seeded_app):
        """``min_modified`` keeps its ORIGINAL "omitted == cleared" contract
        even now that this endpoint has a second, independent field — same
        posture ``…/facts-config``'s own ``retry_mode`` keeps regardless of
        ``transport``/``provider`` (see that endpoint's docstring). A caller
        that wants to touch `schedule` alone WITHOUT wiping an existing date
        filter must resend the current `min_modified` value explicitly —
        that is the CLI's (`agnes admin sharepoint crawl-config`) and the
        source-card panel's job, not this endpoint's."""
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-crawl-schedule-bare")
        client.patch(
            f"{BASE}/{conn_id}/extraction/crawl-config", json={"min_modified": "2023-12-31"}, headers=_auth(token)
        )

        r = client.patch(
            f"{BASE}/{conn_id}/extraction/crawl-config", json={"schedule": "every 6h"}, headers=_auth(token)
        )

        assert r.status_code == 200, r.text
        assert r.json()["min_modified"] == {"value": None, "source": "none"}
        assert r.json()["schedule"]["value"] == "every 6h"

    def test_resending_the_current_min_modified_alongside_schedule_preserves_it(self, seeded_app):
        """The safe form of the call above — the way the source card panel
        and the CLI actually issue a schedule-only-intent save."""
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-crawl-schedule-preserves-mm")
        client.patch(
            f"{BASE}/{conn_id}/extraction/crawl-config", json={"min_modified": "2023-12-31"}, headers=_auth(token)
        )

        r = client.patch(
            f"{BASE}/{conn_id}/extraction/crawl-config",
            json={"min_modified": "2023-12-31", "schedule": "every 6h"},
            headers=_auth(token),
        )

        assert r.status_code == 200, r.text
        assert r.json()["min_modified"] == {"value": "2023-12-31", "source": "connection"}
        assert r.json()["schedule"]["value"] == "every 6h"

    def test_a_null_schedule_clears_it_back_to_the_instance_default(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-crawl-schedule-clear")
        client.patch(f"{BASE}/{conn_id}/extraction/crawl-config", json={"schedule": "every 6h"}, headers=_auth(token))

        r = client.patch(f"{BASE}/{conn_id}/extraction/crawl-config", json={"schedule": None}, headers=_auth(token))

        assert r.status_code == 200, r.text
        assert r.json()["schedule"]["value"] == "instance"

        from src.repositories import source_connections_repo

        row = source_connections_repo().get(conn_id)
        assert "schedule" not in (row["config"]["extraction"].get("crawl") or {})

    def test_unset_schedule_defaults_to_instance(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-crawl-schedule-default")

        r = client.patch(
            f"{BASE}/{conn_id}/extraction/crawl-config", json={"min_modified": "2023-12-31"}, headers=_auth(token)
        )

        assert r.status_code == 200, r.text
        assert r.json()["schedule"] == {"value": "instance", "source": "default", "next_run_at": None}


class TestAudit:
    def test_the_patch_is_audited_with_the_value_and_its_resolution(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-crawl-config-audit")

        r = client.patch(
            f"{BASE}/{conn_id}/extraction/crawl-config", json={"min_modified": "2023-12-31"}, headers=_auth(token)
        )
        assert r.status_code == 200

        import json

        from src.repositories import audit_repo

        rows, _ = audit_repo().query(action="extraction.min_modified_set", limit=50)
        matches = [row for row in rows if conn_id in (row.get("resource") or "")]
        assert matches, "the handler's own log_safe row is missing"
        assert matches[0]["user_id"] == "admin1"
        raw_params = matches[0]["params"]
        params = json.loads(raw_params) if isinstance(raw_params, str) else raw_params
        assert params["min_modified"] == "2023-12-31"
        assert params["resolved"] == "2023-12-31"
        assert params["source"] == "connection"


class TestConfigDrawerResolvedValue:
    """`GET .../extraction/config` (the drawer's own read) carries the
    resolved ``min_modified`` alongside the instance-level rows, so the
    Crawl filter panel can pre-fill its date input with the CURRENT
    override rather than opening blank."""

    def test_unset_by_default(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-crawl-config-drawer")
        r = client.get(f"{BASE}/{conn_id}/extraction/config", headers=_auth(token))
        assert r.status_code == 200, r.text
        assert r.json()["min_modified"] == {"value": None, "source": "none"}

    def test_reflects_a_set_override(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-crawl-config-drawer-set")
        client.patch(
            f"{BASE}/{conn_id}/extraction/crawl-config", json={"min_modified": "2023-12-31"}, headers=_auth(token)
        )
        r = client.get(f"{BASE}/{conn_id}/extraction/config", headers=_auth(token))
        assert r.json()["min_modified"] == {"value": "2023-12-31", "source": "connection"}

    def test_schedule_defaults_to_instance(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-crawl-schedule-drawer-default")
        r = client.get(f"{BASE}/{conn_id}/extraction/config", headers=_auth(token))
        assert r.status_code == 200, r.text
        assert r.json()["schedule"] == {"value": "instance", "source": "default", "next_run_at": None}

    def test_schedule_reflects_a_set_override(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        conn_id = _create_connection(client, token, name="corp-sharepoint-crawl-schedule-drawer-set")
        client.patch(f"{BASE}/{conn_id}/extraction/crawl-config", json={"schedule": "every 6h"}, headers=_auth(token))
        r = client.get(f"{BASE}/{conn_id}/extraction/config", headers=_auth(token))
        body = r.json()["schedule"]
        assert body["value"] == "every 6h"
        assert body["source"] == "connection"
        assert body["next_run_at"] is not None


class TestAdminUiWiring:
    """The `/admin/data-sources` page must actually ship the Crawl filter
    control, not just the API underneath it (the API alone is unusable by
    an admin without shell/API access)."""

    def test_the_page_ships_the_crawl_filter_panel(self, seeded_app):
        client, token = seeded_app["client"], seeded_app["admin_token"]
        resp = client.get("/admin/data-sources", headers=_auth(token))
        assert resp.status_code == 200
        # The panel's own JS moved into an extracted static asset (perf
        # follow-up, 2026-09-03) — `read_admin_data_sources_source()`
        # concatenates the template with every file extracted from it, so
        # this still proves the PAGE ships the capability, regardless of
        # which physical file the fragment now lives in.
        source = read_admin_data_sources_source()
        assert "crawlFilterSave" in source
        assert "crawlFilterClear" in source
        assert "extraction/crawl-config" in source
        # Cookie-session `/api/**` protection: the app-wide CsrfOriginMiddleware
        # origin check, not a form-embedded csrf token — same as the
        # sibling Stop button's own fetch call.
        assert 'credentials: "include"' in source

    def test_the_crawl_filter_lives_on_the_card_not_only_the_drawer(self, seeded_app):
        """Gap 2 (2026-09 live walkthrough): buried behind "View
        configuration" with nothing on the card hinting it exists. Moved,
        not duplicated — the drawer's own additive wrap onto
        `_extConfigHtml` (`_crawlFilterBaseConfigHtml`) is gone, so there is
        exactly one place this value can be set from, and it can never
        drift from a second copy."""
        client, token = seeded_app["client"], seeded_app["admin_token"]
        resp = client.get("/admin/data-sources", headers=_auth(token))
        assert resp.status_code == 200
        source = read_admin_data_sources_source()
        assert "_extRenderCrawlFilter" in source
        assert "_crawlFilterBaseConfigHtml" not in source


class TestCrawlFilterCardRendering:
    """The Crawl filter control (`_extRenderCrawlFilter`), rendered on the
    card next to "Facts policy" — same pattern
    `TestFactsPolicyControlRendering` uses in
    `test_admin_data_sources_extraction.py` for its sibling control."""

    def _run(self, body_js: str, *, row=None) -> dict:
        tpl = read_admin_data_sources_source()
        fns = "\n".join(
            _extract_block(tpl, sig) for sig in ("function _esc(s) {", "function _extRenderCrawlFilter(row) {")
        )
        row = row if row is not None else {"id": "sp1", "config": {}}
        script = f"""
{fns}
const row = {json.dumps(row)};
{body_js}
"""
        return _run_node(script)

    def test_prefills_the_date_input_from_the_connection_override(self):
        out = self._run(
            "console.log(JSON.stringify({ html: _extRenderCrawlFilter(row) }));",
            row={"id": "sp1", "config": {"extraction": {"crawl": {"min_modified": "2026-01-15"}}}},
        )
        html = out["html"]
        assert 'id="ds-sp-crawlfilter-date-sp1"' in html
        assert 'value="2026-01-15"' in html
        assert "Currently: files modified on/after 2026-01-15" in html

    def test_no_override_leaves_the_input_blank_and_says_no_filter(self):
        out = self._run("console.log(JSON.stringify({ html: _extRenderCrawlFilter(row) }));")
        html = out["html"]
        assert 'value=""' in html
        assert "no default filter" in html

    def test_save_and_clear_are_wired_to_this_connection(self):
        out = self._run(
            "console.log(JSON.stringify({ html: _extRenderCrawlFilter(row) }));",
            row={"id": "sp7", "config": {}},
        )
        html = out["html"]
        assert "crawlFilterSave('sp7')" in html
        assert "crawlFilterClear('sp7')" in html

    def test_an_untrusted_name_is_escaped_not_injected(self):
        out = self._run(
            "console.log(JSON.stringify({ html: _extRenderCrawlFilter(row) }));",
            row={"id": "sp1", "name": "<img src=x onerror=alert(1)>", "config": {}},
        )
        html = out["html"]
        assert "<img" not in html
        assert "&lt;img" in html


class TestCrawlFilterCardSave:
    """`crawlFilterSave`/`crawlFilterClear` — moved from the drawer's own
    copy onto the card, same PATCH endpoint, same "show the resolved value
    only after the server answers" discipline `saveSpFactsPolicy` uses for
    its sibling control (`TestFactsPolicySave`)."""

    def _run(self, *, action, input_value="", response_status=200, response_body=None, prior_config=None):
        tpl = read_admin_data_sources_source()
        fns = "\n".join(
            _extract_block(tpl, sig)
            for sig in (
                "async function _crawlConfigPatch(id, { minModified, schedule } = {}) {",
                "function crawlFilterSave(id) {",
                "function crawlFilterClear(id) {",
            )
        )
        response_body = response_body if response_body is not None else {}
        call = "crawlFilterSave('sp1');" if action == "save" else "crawlFilterClear('sp1');"
        script = f"""
const _elements = {{
  "ds-sp-crawlfilter-date-sp1": {{ value: {json.dumps(input_value)} }},
  "ds-sp-crawlfilter-status-sp1": {{ textContent: "" }},
}};
const document = {{ getElementById: (id) => _elements[id] || null }};
let _connections = [{{ id: "sp1", config: {json.dumps(prior_config or {})} }}];
const requests = [];
const toasts = [];
function encodeURIComponent(s) {{ return s; }}
function showToast(msg, ok) {{ toasts.push({{ msg, ok }}); }}
function detailMessage(body, fallback) {{ return (body && body.detail) || fallback; }}
async function fetch(url, opts) {{
  requests.push({{ url, method: opts.method, body: JSON.parse(opts.body) }});
  return {{
    ok: {str(response_status < 400).lower()},
    status: {response_status},
    json: async () => ({json.dumps(response_body)}),
  }};
}}

{fns}

(async () => {{
  {call}
  await new Promise((r) => setTimeout(r, 0));
  console.log(JSON.stringify({{
    requests,
    toasts,
    status: _elements["ds-sp-crawlfilter-status-sp1"].textContent,
    inputValue: _elements["ds-sp-crawlfilter-date-sp1"].value,
    conn: _connections[0],
  }}));
}})();
"""
        return _run_node(script)

    def test_save_with_no_date_sends_nothing_and_says_so(self):
        out = self._run(action="save", input_value="")
        assert out["requests"] == []
        assert out["toasts"][0]["ok"] is False
        assert "Pick a date first" in out["toasts"][0]["msg"]

    def test_save_sends_the_chosen_date_to_the_crawl_config_endpoint(self):
        out = self._run(action="save", input_value="2026-02-01")
        assert out["requests"][0]["url"] == "/api/admin/sharepoint/connections/sp1/extraction/crawl-config"
        assert out["requests"][0]["method"] == "PATCH"
        # The schedule control (D.16) shares this PATCH; an untouched control
        # is re-sent as its current value (here: none) — never omitted, since
        # the endpoint reads an omitted `min_modified` as "cleared".
        assert out["requests"][0]["body"] == {"min_modified": "2026-02-01", "schedule": None}

    def test_a_filter_save_resends_the_connections_own_schedule_untouched(self):
        out = self._run(
            action="save",
            input_value="2026-02-01",
            prior_config={"extraction": {"crawl": {"schedule": "every 6h"}}},
            response_body={
                "min_modified": {"value": "2026-02-01", "source": "connection"},
                "schedule": {"value": "every 6h", "source": "connection", "next_run_at": None},
            },
        )
        assert out["requests"][0]["body"] == {"min_modified": "2026-02-01", "schedule": "every 6h"}
        assert out["conn"]["config"]["extraction"]["crawl"] == {"min_modified": "2026-02-01", "schedule": "every 6h"}

    def test_clear_blanks_the_input_and_sends_null(self):
        out = self._run(action="clear", input_value="2026-02-01")
        assert out["requests"][0]["body"] == {"min_modified": None, "schedule": None}
        assert out["inputValue"] == ""

    def test_the_resolved_value_and_source_are_shown_after_save(self):
        out = self._run(
            action="save",
            input_value="2026-02-01",
            response_body={"min_modified": {"value": "2026-02-01", "source": "connection"}},
        )
        assert "2026-02-01" in out["status"]
        assert "default" in out["status"]

    def test_a_cleared_filter_reads_as_no_filter(self):
        out = self._run(
            action="clear",
            input_value="2026-02-01",
            response_body={"min_modified": {"value": None, "source": "none"}},
        )
        assert out["status"] == "Currently: no default filter — a scope without its own filter is crawled unfiltered."

    def test_a_failed_save_toasts_the_servers_reason_and_clears_the_status(self):
        out = self._run(
            action="save",
            input_value="not-really-a-date",
            response_status=400,
            response_body={"detail": "invalid_min_modified"},
        )
        assert out["status"] == ""
        assert out["toasts"][0]["ok"] is False
        assert "invalid_min_modified" in out["toasts"][0]["msg"]

    def test_a_successful_save_updates_the_in_memory_row_for_the_next_repaint(self):
        out = self._run(
            action="save",
            input_value="2026-03-01",
            prior_config={"extraction": {"crawl": {"min_modified": "2025-01-01"}}},
            response_body={"min_modified": {"value": "2026-03-01", "source": "connection"}},
        )
        assert out["conn"]["config"]["extraction"]["crawl"]["min_modified"] == "2026-03-01"

    def test_a_successful_clear_removes_the_override_from_the_in_memory_row(self):
        out = self._run(
            action="clear",
            input_value="2026-03-01",
            prior_config={"extraction": {"crawl": {"min_modified": "2026-03-01"}}},
            response_body={"min_modified": {"value": None, "source": "none"}},
        )
        assert "min_modified" not in out["conn"]["config"]["extraction"]["crawl"]
