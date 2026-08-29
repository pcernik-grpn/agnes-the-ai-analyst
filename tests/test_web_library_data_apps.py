"""tests/test_web_library_data_apps.py — the Library's Data apps band.

Rail-only by construction (the /library route serves library_legacy.html to
topnav before the sections pipeline runs); these tests pin the rail render.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_app(slug="revenue-dash", name="Revenue dashboard", owner_id="admin1"):
    from src.db import get_system_db
    from src.repositories.data_apps import DataAppsRepository

    conn = get_system_db()
    try:
        DataAppsRepository(conn).create(
            slug=slug,
            name=name,
            owner_user_id=owner_id,
            description="Streamlit revenue overview",
        )
    finally:
        conn.close()


def test_band_lists_visible_app_under_rail(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "1")
    _seed_app()
    c = seeded_app["client"]
    resp = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    body = resp.text
    # Inside the Artefacts band (the caller's files + agent outputs + apps),
    # not a band of its own — the Type facet still says "Data app" (rows
    # keep type_key=data_app).
    assert 'data-lib-sec="files"' in body
    assert ">Artefacts<" in body
    assert 'data-lib-sec="data_app"' not in body
    assert 'href="/apps/detail/revenue-dash"' in body
    assert "Revenue dashboard" in body
    assert 'data-type="data_app"' in body


def test_band_absent_when_feature_disabled(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    monkeypatch.delenv("AGNES_DATA_APPS_ENABLED", raising=False)
    _seed_app(slug="hidden-app", name="Hidden app")
    c = seeded_app["client"]
    resp = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    assert 'href="/apps/detail/hidden-app"' not in resp.text


def test_each_app_renders_exactly_one_library_row(seeded_app, monkeypatch):
    """One app, one row. Two listing blocks in ``library_page`` each appended
    a row for the same apps (one id-keyed, one slug-keyed, both
    ``type_key="data_app"``), so every visible app rendered twice in the same
    band with doubled counts (Devin review on PR #1278). ``data-href`` is
    emitted exactly once per row, so its count is the row count."""
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "1")
    _seed_app(slug="once-app", name="Once App")
    c = seeded_app["client"]
    body = c.get("/library", headers=_auth(seeded_app["admin_token"])).text
    assert body.count('data-href="/apps/detail/once-app"') == 1


def test_non_owner_without_grant_sees_no_app_row(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "1")
    _seed_app(slug="private-app", name="Private app", owner_id="admin1")
    c = seeded_app["client"]
    resp = c.get("/library", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    assert 'href="/apps/detail/private-app"' not in resp.text


def test_soon_badge_gone_from_files_band(seeded_app, monkeypatch):
    """The badge promised data apps would land in Files; with the Data apps
    band real, the promise is kept and the badge retires. The Files band only
    renders when the caller has a file/collection, so seed one — without it
    this test is vacuously green on the unfixed code."""
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    c = seeded_app["client"]
    r = c.post("/api/collections", json={"name": "Badge probe"}, headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 201, r.text
    resp = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    body = resp.text
    assert "Badge probe" in body  # the Files band really rendered
    assert "Data apps coming soon" not in body


def test_app_rows_trail_the_files_inside_artefacts(seeded_app, monkeypatch):
    """Sub-kinds stay grouped inside the Artefacts band: folders, then loose
    files, then data apps — not interleaved by recency."""
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "1")
    _seed_app(slug="order-app", name="Ordering app")
    c = seeded_app["client"]
    r = c.post("/api/collections", json={"name": "Order probe"}, headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 201, r.text
    body = c.get("/library", headers=_auth(seeded_app["admin_token"])).text
    sec_at = body.index('data-lib-sec="files"')
    assert body.index("Order probe", sec_at) < body.index('href="/apps/detail/order-app"', sec_at)


def test_admin_sees_only_own_and_granted_apps(seeded_app, monkeypatch):
    """The Library's contract is no admin god-mode: an admin's Library lists
    what THEY have. The API's ``_can_view`` short-circuits on Admin — reusing
    it here listed every user's private app in the admin's Library (Devin
    review on PR #1278). The instance-wide inventory stays on the API/CLI
    list and the admin surfaces."""
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "1")
    _seed_app(slug="analyst-private", name="Analyst private app", owner_id="analyst1")
    c = seeded_app["client"]
    body = c.get("/library", headers=_auth(seeded_app["admin_token"])).text
    assert "Analyst private app" not in body
    # The analyst (owner) still sees it.
    body = c.get("/library", headers=_auth(seeded_app["analyst_token"])).text
    assert "Analyst private app" in body


def test_app_row_sharing_badge_is_not_the_store_explainer(seeded_app, monkeypatch):
    """An owner's app row must not wear the store-entity sharing explainer
    (its dialog describes Store approval, which does not govern apps). Since
    the Devin follow-ups on PR #1272, the badge is the owner-share control
    wired to the slug-keyed grant — the same dialog every other owner-held
    kind uses (`data-share`/`data-share-type`, backed by `_OWNER_RESOLVERS`)."""
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "1")
    _seed_app(slug="badge-probe", name="Badge probe app")
    c = seeded_app["client"]
    body = c.get("/library", headers=_auth(seeded_app["admin_token"])).text
    row_at = body.index("Badge probe app")
    row = body[row_at - 2000 : row_at + 3000]
    assert 'data-share-info="badge-probe"' not in row
    assert 'data-share-type="data_app"' in row
    assert 'data-share="badge-probe"' in row


def test_memory_rows_survive_a_count_failure(seeded_app, monkeypatch):
    """Counts unknown must not read as counts zero: when the grouped count
    query fails, granted memory domains still render (without counts) instead
    of being hidden by the empty-domain rule (Devin review on PR #1278)."""
    import tests.test_web_library_memory_band as band

    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    dom = band._make_domain("lib-cnt-fail", "Lib CountFail")
    band._make_item("lib_cntfail_1", "Note", dom)
    band._grant_domain("Everyone", dom, users=["analyst1"])

    from src.repositories.memory_domains import MemoryDomainsRepository

    def _boom(self):
        raise RuntimeError("simulated count failure")

    monkeypatch.setattr(MemoryDomainsRepository, "count_items_by_domain", _boom)
    c = seeded_app["client"]
    resp = c.get("/library", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    assert "Lib CountFail" in resp.text
