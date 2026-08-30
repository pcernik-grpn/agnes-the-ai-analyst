"""TCRD-221: content granted to nobody must be visible as a problem.

A plugin ingested but granted to no group is invisible to every non-admin —
and the admin gets no signal that it exists yet reaches nobody. Ingesting
content and granting it to no one has no legitimate steady state, so the
`/admin` dashboard's "needs you" zone owes the admin a row for it.

The live cost that motivated this: a merged, ingested plugin "missing" from
the Library ate ~50 minutes of a demo walkthrough before anyone found the
unticked grant.
"""

from __future__ import annotations

from pathlib import Path


def _bootstrap(tmp_path: Path, monkeypatch):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb

    conn = _open_duckdb(str(tmp_path / "system.duckdb"))
    _ensure_schema(conn)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr("src.repositories.get_system_db", lambda: conn)
    return conn


def _seed_marketplace_with_plugin(name="orphan-plugin", *, admin_disabled=False):
    from src.repositories import marketplace_plugins_repo, marketplace_registry_repo

    marketplace_registry_repo().register(id="mp1", name="MP One", url="https://example.test/mp1.git")
    marketplace_plugins_repo().replace_for_marketplace(
        "mp1",
        [{"name": name, "version": "1.0", "description": "d"}],
    )
    if admin_disabled:
        marketplace_plugins_repo().set_admin_disabled("mp1", name, True)


class TestUngrantedPluginsSignal:
    def test_an_ungranted_plugin_raises_the_signal(self, tmp_path, monkeypatch):
        conn = _bootstrap(tmp_path, monkeypatch)
        _seed_marketplace_with_plugin()

        from app.web.admin_signals import _resolve_ungranted_plugins

        sig = _resolve_ungranted_plugins()
        assert sig is not None, "a plugin granted to no group must raise the signal"
        assert sig.count == 1
        assert sig.href == "/admin/access"
        conn.close()

    def test_a_granted_plugin_is_clear(self, tmp_path, monkeypatch):
        conn = _bootstrap(tmp_path, monkeypatch)
        _seed_marketplace_with_plugin()

        from src.repositories import resource_grants_repo, user_groups_repo

        group = user_groups_repo().get_by_name("Everyone")
        assert group is not None, "system groups are seeded by _ensure_schema"
        resource_grants_repo().ensure_grant(
            group_id=group["id"],
            resource_type="marketplace_plugin",
            resource_id="mp1/orphan-plugin",
        )

        from app.web.admin_signals import _resolve_ungranted_plugins

        assert _resolve_ungranted_plugins() is None, "rule 1: zero renders nothing — a granted plugin must not count"
        conn.close()

    def test_disabled_and_system_plugins_do_not_count(self, tmp_path, monkeypatch):
        """A disabled plugin is deliberately hidden (not a mistake), and a
        system plugin's grants are materialized for every group by
        mark_system — neither is 'content reaching nobody'."""
        conn = _bootstrap(tmp_path, monkeypatch)
        _seed_marketplace_with_plugin("hidden-one", admin_disabled=True)

        from app.web.admin_signals import _resolve_ungranted_plugins

        assert _resolve_ungranted_plugins() is None
        conn.close()

    def test_signal_is_registered_in_needs_you(self):
        from app.web.admin_signals import ZONE_NEEDS_YOU, signals_for_zone

        keys = [s.key for s in signals_for_zone(ZONE_NEEDS_YOU)]
        assert "ungranted_plugins" in keys


class TestAccessPageShowsTheOrphan:
    def test_access_page_marks_resources_granted_to_nobody(self):
        """Same guarantee, a better surface.

        The badge used to ride each row of the group view — where a resource
        nobody holds can never appear, because that view shows one group's
        holdings and a thing nobody holds is in no group's. It is a
        first-class state of the BUNDLE view now, the reading that can
        actually see it: those bundles are collected behind their own line,
        counted by kind, and each says so on its own row.

        It still derives from the grants payload the controls read, which is
        the part that matters — a second source could disagree with them.
        """
        template = Path("app/web/templates/admin_access.html").read_text(encoding="utf-8")
        assert "granted to nobody" in template
        assert "Granted to nobody:" in template          # the collected line
        assert "is-nobody" in template                   # …and the row's own state

        # Scoped to the bundle renderer: `const rows = []` appears in the
        # group view too, and that one says nothing about this state.
        held_block = template.split("function renderBundles()")[1].split("const nobodyLine")[0]
        assert "overview.grants" in held_block, (
            "the nobody state must derive from the grants payload the controls "
            "read, not a second data source that could disagree with them"
        )
