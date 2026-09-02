"""GET /api/admin/reports/marketplace-digest — consolidated daily/weekly digest."""
import uuid
from datetime import datetime, date, timezone, timedelta

ANCHOR = date(2026, 6, 20)
PREV = ANCHOR - timedelta(days=1)


def _ts(d: date, hour: int = 10) -> datetime:
    return datetime(d.year, d.month, d.day, hour, tzinfo=timezone.utc)


def _seed(conn):
    # --- usage_events: anchor day busier than the day before ----------------
    def ev(d, user, tool, is_err):
        conn.execute(
            """INSERT INTO usage_events
               (id, session_id, session_file, username, event_type, tool_name,
                is_error, source, occurred_at, processor_version)
               VALUES (?, ?, ?, ?, 'tool_use', ?, ?, ?, ?, 1)""",
            [str(uuid.uuid4()), f"sess-{uuid.uuid4()}", f"{user}/x.jsonl", user,
             tool, is_err, "curated", _ts(d)],
        )

    # anchor: 3 users, 10 events, 1 error
    for user in ("alice", "bob", "carol"):
        for tool in ("Bash", "Read", "Edit"):
            ev(ANCHOR, user, tool, False)
    ev(ANCHOR, "alice", "Write", True)  # 10th event, the error
    # prev day: 2 users, 5 events, 0 errors
    for user in ("alice", "bob"):
        for tool in ("Bash", "Read"):
            ev(PREV, user, tool, False)
    ev(PREV, "alice", "Edit", False)

    # --- sessions -----------------------------------------------------------
    for d, sid in ((ANCHOR, "s1"), (ANCHOR, "s2"), (PREV, "s3")):
        conn.execute(
            """INSERT INTO usage_session_summary
               (session_file, session_id, username, started_at, processor_version)
               VALUES (?, ?, 'alice', ?, 1)""",
            [f"{sid}.jsonl", sid, _ts(d)],
        )

    # --- marketplace item daily rollups -------------------------------------
    def item(d, source, type_, name, count, du, err):
        conn.execute(
            """INSERT INTO usage_marketplace_item_daily
               (day, source, type, parent_plugin, name, count, distinct_users, error_count)
               VALUES (?, ?, ?, '', ?, ?, ?, ?)""",
            [d, source, type_, name, count, du, err],
        )

    item(ANCHOR, "curated", "plugin", "product-analyzer", 8, 3, 1)
    item(ANCHOR, "flea", "agent", "data-bot", 4, 2, 0)
    item(PREV, "curated", "plugin", "product-analyzer", 4, 2, 0)  # rising (8 > 4)
    item(PREV, "curated", "skill", "old-skill", 6, 2, 0)          # falling (0 < 6)

    # --- marketplace registry + plugins -------------------------------------
    def reg(mid, name, curator, last_synced, last_error):
        conn.execute(
            """INSERT INTO marketplace_registry
               (id, name, url, curator_name, last_synced_at, last_error)
               VALUES (?, ?, 'https://example.com/repo.git', ?, ?, ?)""",
            [mid, name, curator, last_synced, last_error],
        )

    now = datetime.now(timezone.utc)
    reg("curated-product", "Product", "Blanka", now - timedelta(hours=2), None)   # ok
    reg("curated-stale", "Marketing", "Juraj", now - timedelta(days=5), None)     # stale
    reg("curated-err", "Sales", "Carmen", now - timedelta(hours=1), "clone failed")  # error

    # Built-in marketplace: seeded locally, never git-synced (null last_synced),
    # is_builtin=TRUE. Must NOT be flagged stale, and its plugins must NOT show
    # up as zero-usage curated content.
    conn.execute(
        """INSERT INTO marketplace_registry
           (id, name, url, curator_name, last_synced_at, last_error, is_builtin)
           VALUES ('agnes-builtin', 'Built-in', '', NULL, NULL, NULL, TRUE)"""
    )

    def plug(mid, name):
        conn.execute(
            "INSERT INTO marketplace_plugins (marketplace_id, name) VALUES (?, ?)",
            [mid, name],
        )

    plug("curated-product", "product-analyzer")  # used → not in zero_usage
    plug("curated-product", "unused-skill")       # zero usage → listed
    plug("agnes-builtin", "welcome")              # built-in → excluded from zero_usage
    conn.execute(                                  # admin-disabled → excluded
        "INSERT INTO marketplace_plugins (marketplace_id, name, admin_disabled) "
        "VALUES ('curated-product', 'disabled-skill', TRUE)"
    )

    # Reaches every account automatically → its installs are provisioning,
    # not adoption. `marketplace_plugins.is_system` said this until 0098;
    # it is now a required grant held by the carrier group, which is the one
    # spelling both backends share (see `reports._NOT_SYSTEM`).
    plug("curated-product", "platform-core")
    everyone_id = conn.execute(
        "SELECT id FROM user_groups WHERE name = 'Everyone' AND is_system"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO resource_grants (id, group_id, resource_type, resource_id, requirement) "
        "VALUES ('rg-platform-core', ?, 'marketplace_plugin', "
        "        'curated-product/platform-core', 'required')",
        [everyone_id],
    )

    # --- installs (anchor day) ----------------------------------------------
    conn.execute(
        "INSERT INTO user_plugin_optouts (user_id, marketplace_id, plugin_name, opted_out_at) "
        "VALUES ('u1', 'curated-product', 'product-analyzer', ?)", [_ts(ANCHOR)])
    conn.execute(
        "INSERT INTO user_plugin_optouts (user_id, marketplace_id, plugin_name, opted_out_at) "
        "VALUES ('u2', 'curated-product', 'product-analyzer', ?)", [_ts(ANCHOR)])
    conn.execute(
        "INSERT INTO user_store_installs (user_id, entity_id, installed_at) "
        "VALUES ('u1', 'ent-1', ?)", [_ts(ANCHOR)])
    # A system-plugin rollout to 3 users on the same day. This is what made the
    # KPI row self-contradictory: it must not reach new_installs.
    for uid in ("u1", "u3", "u4"):
        conn.execute(
            "INSERT INTO user_plugin_optouts (user_id, marketplace_id, plugin_name, opted_out_at) "
            "VALUES (?, 'curated-product', 'platform-core', ?)", [uid, _ts(ANCHOR)])


def _get(client, headers, period):
    return client.get(
        f"/api/admin/reports/marketplace-digest?period={period}&date={ANCHOR.isoformat()}",
        headers=headers,
    )


def test_daily_digest_shape_and_kpis(seeded_app, admin_user):
    from src.db import get_system_db
    conn = get_system_db()
    _seed(conn)
    conn.close()

    resp = _get(seeded_app["client"], admin_user, "daily")
    assert resp.status_code == 200
    data = resp.json()

    # top-level contract
    for key in ("meta", "headline_kpis", "trend_series", "by_source", "top_items",
                "rising", "falling", "failures", "installs", "zero_usage",
                "marketplace_health"):
        assert key in data, f"missing {key}"

    assert data["meta"]["report_type"] == "daily"
    assert data["meta"]["period_start"] == ANCHOR.isoformat()
    assert len(data["trend_series"]) == 14
    # the series must carry the same exclusion as the headline, or the chart
    # and the KPI can disagree. 2 curated + 1 flea, not the 3 system rows.
    anchor_day = next(d for d in data["trend_series"] if d["day"] == ANCHOR.isoformat())
    assert anchor_day["installs"] == 3

    k = data["headline_kpis"]
    assert k["active_users"] == {"value": 3, "prev": 2, "delta_pct": 50.0}
    assert k["invocations"] == {"value": 10, "prev": 5, "delta_pct": 100.0}
    assert k["errors"]["value"] == 1
    assert k["sessions"]["value"] == 2 and k["sessions"]["prev"] == 1
    assert k["new_installs"]["value"] == 3  # 2 curated + 1 flea; the 3-user
    # system-plugin rollout on the same day is provisioning, not adoption, and
    # is reported separately instead of inflating the headline figure.
    assert k["system_installs"]["value"] == 3
    # People-unit figure, the one comparable to active_users: u1 (curated+flea)
    # and u2. u3/u4 only received the system plugin, so they are not installers.
    assert k["distinct_installers"]["value"] == 2
    assert data["installs"]["total"] == 3
    assert data["installs"]["system_total"] == 3
    # the per-plugin breakdown agrees with the headline — no system plugin in it
    assert [i["name"] for i in data["installs"]["curated"]] == ["product-analyzer"]


def test_daily_movers_failures_and_zero_usage(seeded_app, admin_user):
    from src.db import get_system_db
    conn = get_system_db()
    _seed(conn)
    conn.close()

    data = _get(seeded_app["client"], admin_user, "daily").json()

    # A plugin everyone gets automatically DOES surface in zero_usage now.
    # It used to be excluded, on the grounds that a mandatory plugin cannot
    # be "not landing" — but that exclusion rode `is_system`, and a granted
    # plugin nobody invokes is a report-worthy finding rather than a
    # false positive. Its INSTALL rows are still classified as provisioning
    # (`_NOT_SYSTEM`), which is the part that kept the KPI row honest.
    assert "platform-core" in [z["name"] for z in data["zero_usage"]]

    # top_items ranked, product-analyzer leads with 8 invocations
    assert data["top_items"][0]["name"] == "product-analyzer"
    assert data["top_items"][0]["rank"] == 1
    assert data["top_items"][0]["invocations"] == 8
    # daily spans one day, so the per-day rollup distinct is exact
    assert data["top_items"][0]["distinct_users"] == 3

    # rising: product-analyzer 8 vs 4 → +100%
    assert any(i["name"] == "product-analyzer" and i["delta_pct"] == 100.0
               for i in data["rising"])
    # rising must also surface brand-new items (no comparison base → delta null):
    # data-bot appears only on the anchor day, so prev=0.
    new_movers = [i for i in data["rising"] if i["name"] == "data-bot"]
    assert new_movers and new_movers[0]["delta_pct"] is None
    # falling: old-skill dropped from 6 to 0
    assert any(i["name"] == "old-skill" for i in data["falling"])
    # failures: product-analyzer had 1 error
    assert any(f["name"] == "product-analyzer" and f["errors"] == 1
               for f in data["failures"])
    # zero_usage: unused-skill listed; used + built-in plugins excluded
    zero_names = {z["name"] for z in data["zero_usage"]}
    assert "unused-skill" in zero_names
    assert "product-analyzer" not in zero_names
    assert "welcome" not in zero_names        # built-in plugin must not be flagged
    assert "disabled-skill" not in zero_names  # admin-disabled can't land → excluded

    # marketplace_health statuses derived correctly
    health = {h["id"]: h for h in data["marketplace_health"]}
    assert health["curated-product"]["sync_status"] == "ok"
    assert health["curated-stale"]["sync_status"] == "stale"
    assert health["curated-err"]["sync_status"] == "error"
    # built-in is never git-synced (null last_synced) but is healthy, not stale
    assert health["agnes-builtin"]["sync_status"] == "ok"
    # product-analyzer + unused-skill + disabled-skill + platform-core
    # (count is the catalog size)
    assert health["curated-product"]["plugin_count"] == 4


def test_weekly_digest_window(seeded_app, admin_user):
    from src.db import get_system_db
    conn = get_system_db()
    _seed(conn)
    conn.close()

    data = _get(seeded_app["client"], admin_user, "weekly").json()
    assert data["meta"]["report_type"] == "weekly"
    assert len(data["trend_series"]) == 30
    # weekly primary spans anchor-6..anchor, so both seeded days are included
    assert data["headline_kpis"]["invocations"]["value"] == 15  # 10 + 5
    # P2: weekly never sums per-day distincts (that overcounts multi-day users)
    # and has no window-aligned true-distinct source, so per-item distinct_users
    # is reported as null rather than an inflated or window-misaligned number.
    assert data["top_items"], "expected weekly top_items"
    assert all(i["distinct_users"] is None for i in data["top_items"])


def test_zero_usage_parent_plugin_attribution(seeded_app, admin_user):
    """Child skill/agent usage marks its `parent_plugin` as used (kept OUT of
    zero_usage), but the child item's own name must NOT suppress an unrelated
    plugin that happens to share that name. Pins the business rule at
    `app/api/admin_reports.py` (used_curated derivation) so a future edit can't
    silently flag an active parent or clear an unrelated same-named plugin."""
    from src.db import get_system_db
    conn = get_system_db()

    # A curated (non-builtin) marketplace so both plugins resolve as curated
    # content eligible for zero_usage.
    conn.execute(
        """INSERT INTO marketplace_registry
           (id, name, url, curator_name, last_synced_at, last_error)
           VALUES ('curated-pp', 'PP', 'https://example.com/pp.git', 'Dana', ?, NULL)""",
        [datetime.now(timezone.utc) - timedelta(hours=1)],
    )
    # `parent-only-plugin` is never invoked directly — only its child skill is.
    # `child-skill` is an UNRELATED plugin that merely shares the child's name.
    for name in ("parent-only-plugin", "child-skill"):
        conn.execute(
            "INSERT INTO marketplace_plugins (marketplace_id, name) VALUES ('curated-pp', ?)",
            [name],
        )
    # One curated child-skill invocation on the anchor day, parent_plugin set,
    # type='skill' (NOT 'plugin').
    conn.execute(
        """INSERT INTO usage_marketplace_item_daily
           (day, source, type, parent_plugin, name, count, distinct_users, error_count)
           VALUES (?, 'curated', 'skill', 'parent-only-plugin', 'child-skill', 3, 1, 0)""",
        [ANCHOR],
    )
    conn.close()

    data = _get(seeded_app["client"], admin_user, "daily").json()
    zero_names = {z["name"] for z in data["zero_usage"]}
    # parent counted as used via its child's parent_plugin → excluded
    assert "parent-only-plugin" not in zero_names
    # the unrelated plugin sharing the child's NAME is not suppressed → listed
    assert "child-skill" in zero_names


def test_digest_survives_audit_failure(seeded_app, admin_user, monkeypatch):
    """Audit logging is best-effort: a failing `audit_repo().log()` must not turn
    a transient audit outage into a report outage for the headless pipeline.
    Forces the burst-suppression gate open so the audit write is actually
    attempted, then makes it raise."""
    class _BoomAudit:
        def log(self, *args, **kwargs):
            raise RuntimeError("audit backend down")

    monkeypatch.setattr("app.api.admin_reports._should_audit", lambda *a, **k: True)
    monkeypatch.setattr("app.api.admin_reports.audit_repo", lambda: _BoomAudit())

    resp = _get(seeded_app["client"], admin_user, "daily")
    assert resp.status_code == 200


def test_digest_admin_only(seeded_app, analyst_user):
    resp = _get(seeded_app["client"], analyst_user, "daily")
    assert resp.status_code in (401, 403)


def test_digest_period_validation(seeded_app, admin_user):
    resp = seeded_app["client"].get(
        "/api/admin/reports/marketplace-digest?period=bogus", headers=admin_user)
    assert resp.status_code == 422


def test_digest_bad_date(seeded_app, admin_user):
    resp = seeded_app["client"].get(
        "/api/admin/reports/marketplace-digest?period=daily&date=not-a-date",
        headers=admin_user)
    assert resp.status_code == 422
