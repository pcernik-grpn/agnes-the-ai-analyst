"""The delivery next-step — closing the funnel gap between "connected" and
"someone can actually see it".

An admin connects a source on /admin/data-sources (or registers a marketplace
on /admin/marketplaces), sees its content listed on that page, opens /library
and finds it empty. Nothing is broken: /library is grant-scoped, and governed
content only reaches a person once its tables are in a data package AND that
package is granted to a group they are in. Connecting the source does neither,
and until now no page said so or offered the next move.

What these pin:

  * the chain is state-aware, not a banner — the row appears for the step the
    source is ACTUALLY on (in no package → in no group → in an empty group)
    and disappears entirely once the source delivers, so a finished setup is
    never nagged;
  * it is never a lie: a source with nothing registered is left to the Tables
    cell that already carries that verb, and a `remote`-only source (reachable
    server-side without a package) is never told to bundle anything;
  * every state links the Simulate lens, the one honest answer to "did that
    work?" — and it is the PREVIEW lens (`?lens=simulate`), never "open the
    analyst page as yourself", which is a different question;
  * the step travels in the same dict the pipeline strip does, so the card's
    counts, its status word and its next step come from ONE read and can never
    disagree.
"""

from __future__ import annotations

from tests import _ds_page_source

import re
import uuid
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATES = _ROOT / "app" / "web" / "templates"
_DATA_SOURCES_TPL = _TEMPLATES / "admin_data_sources.html"
_MARKETPLACES_TPL = _TEMPLATES / "admin_marketplaces.html"

#: Where the Simulate lens lives. Hard-coded here on purpose: this is the
#: contract the affordance exists to honour, so a silent move of the lens
#: fails this suite rather than quietly dropping the verify step.
SIMULATE_HREF = "/admin/access?lens=simulate"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def source(seeded_app):
    """A connected source with one distributable table and nothing else —
    the exact state an admin is in one minute after the connect wizard."""
    from src.repositories import source_connections_repo, table_registry_repo

    conn_id = f"nextstep-{uuid.uuid4().hex[:8]}"
    table_id = f"nstbl-{uuid.uuid4().hex[:6]}"
    source_connections_repo().create(
        id=conn_id,
        name="Warehouse",
        source_type="keboola",
        config={"stack_url": "https://connection.example.com"},
    )
    table_registry_repo().register(
        id=table_id,
        name=f"orders_{table_id[-6:]}",
        source_type="keboola",
        bucket="in.c-main",
        source_table="orders",
        query_mode="local",
        connection_id=conn_id,
    )
    yield {"conn_id": conn_id, "table_id": table_id}
    try:
        table_registry_repo().unregister(table_id)
    finally:
        source_connections_repo().delete(conn_id)


def _next_step(conn_id: str) -> dict | None:
    from app.web.router import _source_pipelines

    return (_source_pipelines()[conn_id].get("feeds") or {}).get("next")


def _package_the_table(table_id: str, name: str = "Sales") -> str:
    from src.repositories import data_packages_repo

    pkg_id = data_packages_repo().create(
        name=name,
        slug=f"{name.lower()}-{uuid.uuid4().hex[:6]}",
        description=None,
        icon=None,
        color=None,
        created_by="admin@example.com",
    )
    data_packages_repo().add_table(pkg_id, table_id, added_by="admin@example.com")
    return pkg_id


def _grant_to_new_group(pkg_id: str, *, with_member: str | None = None) -> str:
    from src.repositories import (
        resource_grants_repo,
        user_group_members_repo,
        user_groups_repo,
    )

    group_id = user_groups_repo().create(
        name=f"Analysts {uuid.uuid4().hex[:6]}",
        created_by="admin@example.com",
    )["id"]
    resource_grants_repo().create(
        group_id=group_id,
        resource_type="data_package",
        resource_id=pkg_id,
        assigned_by="admin@example.com",
    )
    if with_member:
        # Seeded users are NOT auto-joined to any group (the sign-in path that
        # writes `Everyone` never runs under the fixture), so membership here
        # is always explicit.
        user_group_members_repo().add_member(
            user_id=with_member,
            group_id=group_id,
            source="manual",
            added_by="admin@example.com",
        )
    return group_id


class TestTheChainIsStateAware:
    """One row, four states — and one of them is "say nothing"."""

    def test_tables_in_no_package_ask_for_a_package(self, seeded_app, source):
        step = _next_step(source["conn_id"])
        assert step is not None, "a source whose tables reach nobody must offer the next step"
        assert step["key"] == "package"
        assert "package" in step["cta"].lower()
        # The destination is the pile it names, not the list of every table.
        assert step["href"].startswith("/admin/tables")

    def test_a_package_shared_with_nobody_asks_for_the_grant(self, seeded_app, source):
        _package_the_table(source["table_id"])
        step = _next_step(source["conn_id"])
        assert step is not None
        assert step["key"] == "share"
        assert "group" in step["cta"].lower()

    def test_a_grant_to_an_empty_group_asks_for_people(self, seeded_app, source):
        pkg_id = _package_the_table(source["table_id"])
        _grant_to_new_group(pkg_id)
        step = _next_step(source["conn_id"])
        assert step is not None
        assert step["key"] == "people"

    def test_a_delivering_source_is_not_nagged(self, seeded_app, source):
        """The bar the whole feature is judged on: once somebody can actually
        pull this source's data, the card says nothing extra."""
        pkg_id = _package_the_table(source["table_id"])
        _grant_to_new_group(pkg_id, with_member="analyst1")
        assert _next_step(source["conn_id"]) is None

    def test_a_source_with_no_tables_leaves_the_verb_to_the_tables_cell(self, seeded_app):
        """The strip's Tables cell already renders "Add the first tables →".
        A second copy of that verb on the same card is noise, not guidance."""
        from src.repositories import source_connections_repo

        conn_id = f"empty-{uuid.uuid4().hex[:8]}"
        source_connections_repo().create(
            id=conn_id,
            name="Nothing Registered",
            source_type="keboola",
            config={"stack_url": "https://connection.example.com"},
        )
        try:
            assert _next_step(conn_id) is None
        finally:
            source_connections_repo().delete(conn_id)

    def test_a_remote_only_source_is_never_told_to_bundle(self, seeded_app):
        """`remote` rows answer server-side without a package (the same fold
        /admin's gap card and the unpackaged tray use), so telling their admin
        to bundle them would be advice that fixes nothing."""
        from src.repositories import source_connections_repo, table_registry_repo

        conn_id = f"remote-{uuid.uuid4().hex[:8]}"
        table_id = f"rmtbl-{uuid.uuid4().hex[:6]}"
        source_connections_repo().create(
            id=conn_id,
            name="Live Warehouse",
            source_type="bigquery",
            config={"project_id": "example-warehouse"},
        )
        table_registry_repo().register(
            id=table_id,
            name=f"events_{table_id[-6:]}",
            source_type="bigquery",
            bucket="analytics",
            source_table="events",
            query_mode="remote",
            connection_id=conn_id,
        )
        try:
            assert _next_step(conn_id) is None
        finally:
            table_registry_repo().unregister(table_id)
            source_connections_repo().delete(conn_id)


class TestEveryStepCarriesTheHonestCheck:
    def test_each_state_links_the_simulate_lens(self, seeded_app, source):
        """ "Did that work?" is answered by previewing a person's Library, and
        nothing pointed at that lens from the page where the admin gets
        confused."""
        seen = [_next_step(source["conn_id"])]
        pkg_id = _package_the_table(source["table_id"])
        seen.append(_next_step(source["conn_id"]))
        _grant_to_new_group(pkg_id)
        seen.append(_next_step(source["conn_id"]))

        assert [s["key"] for s in seen] == ["package", "share", "people"]
        for s in seen:
            assert s["verify_href"] == SIMULATE_HREF
            assert s["verify_cta"]

    def test_the_verify_link_is_the_preview_not_the_analyst_page(self, seeded_app, source):
        """Two different questions, deliberately kept apart (see the comment
        on admin_package_detail.html's "Open analyst page"): arriving as
        YOURSELF is not previewing someone else's access."""
        step = _next_step(source["conn_id"])
        assert "lens=simulate" in step["verify_href"]
        assert not step["verify_href"].startswith("/catalog")
        assert "library" in step["verify_cta"].lower()

    def test_every_step_is_guidance_with_a_real_destination(self, seeded_app):
        from app.web.router import _source_next_step

        states = [
            ({"count": 1, "distributable": 1, "unpackaged": 1}, {"packages": 0, "groups": 0, "people": 0}),
            ({"count": 1, "distributable": 1, "unpackaged": 0}, {"packages": 1, "groups": 0, "people": 0}),
            ({"count": 1, "distributable": 1, "unpackaged": 0}, {"packages": 1, "groups": 1, "people": 0}),
        ]
        router_src = (_ROOT / "app" / "web" / "router.py").read_text(encoding="utf-8")
        routes = set(re.findall(r'@router\.get\("(/admin[^"]*)"', router_src))
        seen = []
        for tables, feeds in states:
            step = _source_next_step("keboola", tables, feeds)
            assert step is not None
            assert len(step["text"]) > 40, f"{step['key']} has no real sentence"
            assert step["cta"] and step["href"]
            for href in (step["href"], step["verify_href"]):
                assert href.split("?", 1)[0] in routes, f"dead link: {href}"
            seen.append(step["key"])
        assert seen == ["package", "share", "people"]

    def test_a_partly_bundled_source_still_asks_for_the_package(self, seeded_app):
        """The first rung counts UNPACKAGED distributable rows exactly, so a
        source whose remote rows are bundled and shared while its local ones
        are not is still caught — the coarser `feeds` counts alone would have
        read that as delivered."""
        from app.web.router import _source_next_step

        step = _source_next_step(
            "keboola",
            {"count": 5, "distributable": 4, "unpackaged": 3},
            {"packages": 1, "groups": 1, "people": 7},
        )
        assert step is not None and step["key"] == "package"
        assert "3 of this source's tables are" in step["text"]

    def test_a_wholly_unbundled_source_does_not_say_five_of_five(self, seeded_app):
        """On the common fresh-connection path every row is unpackaged, and
        "5 of 5" is a riddle where "This source's 5 tables" is a sentence."""
        from app.web.router import _source_next_step

        step = _source_next_step(
            "keboola",
            {"count": 5, "distributable": 5, "unpackaged": 5},
            {"packages": 0, "groups": 0, "people": 0},
        )
        assert step["text"].startswith("This source's 5 tables are in no data package")

    def test_everyone_counts_as_delivering(self, seeded_app):
        """`people == -1` is the "granted to Everyone" sentinel the strip
        already uses; reading it as a falsy zero would nag the one setup that
        reaches the most people."""
        from app.web.router import _source_next_step

        assert (
            _source_next_step(
                "keboola",
                {"count": 3, "distributable": 3, "unpackaged": 0},
                {"packages": 1, "groups": 1, "people": -1},
            )
            is None
        )

    def test_a_file_source_has_its_own_chain_and_is_left_alone(self, seeded_app):
        """SharePoint delivers through collections + groups, not packages —
        its card carries its own sharing rows, so the package chain must not
        speak over them."""
        from app.web.router import _source_next_step

        assert (
            _source_next_step(
                "sharepoint",
                {"count": 4, "distributable": 4, "unpackaged": 4},
                {"packages": 0, "groups": 0, "people": 0},
            )
            is None
        )


class TestTheCardRendersIt:
    def _page(self, seeded_app) -> str:
        return (
            seeded_app["client"]
            .get(
                "/admin/data-sources",
                headers=_auth(seeded_app["admin_token"]),
            )
            .text
        )

    def test_the_page_ships_the_renderer_and_its_style(self, seeded_app, source):
        body = self._page(seeded_app)
        assert "_nextStepHtml" in body
        assert ".ds-next" in body
        assert SIMULATE_HREF in body

    def test_the_repaint_keeps_the_row_true(self, seeded_app):
        """Every mutation on this page re-reads the strip and repaints in
        place. A next-step row the repaint does not touch would go on telling
        an admin to bundle tables they just bundled."""
        body = self._page(seeded_app)
        repaint = body[body.index("function _repaintSourceCards()") :]
        repaint = repaint[: repaint.index("\n}\n")]
        assert "_nextStepHtml" in repaint

    def test_the_row_is_drawn_between_the_strip_and_the_body(self, seeded_app):
        body = self._page(seeded_app)
        card = body[body.index('<article class="ds-src"') : body.index("async function loadConnections()")]
        assert card.index("_pipelineStripHtml(row)") < card.index("_nextStepHtml(row)")
        assert card.index("_nextStepHtml(row)") < card.index('class="ds-src__body"')


class TestNextStepHtml:
    """`_nextStepHtml` executed for real via node — the rendered markup, not
    just the server-side data shape. Same harness as
    `TestSharePointSourceCardRendering` in test_admin_data_sources_page.py."""

    @staticmethod
    def _extract_function(tpl: str, signature: str) -> str:
        start = tpl.index(signature)
        depth = 0
        started = False
        for i in range(start, len(tpl)):
            ch = tpl[i]
            if ch == "{":
                depth += 1
                started = True
            elif ch == "}":
                depth -= 1
                if started and depth == 0:
                    return tpl[start : i + 1]
        raise AssertionError(f"unbalanced braces extracting {signature!r}")

    def _run(self, pipelines: dict, row: dict | None = None) -> str:
        import json
        import subprocess
        import tempfile

        tpl = _ds_page_source.page_source()
        fns = "\n".join(
            self._extract_function(tpl, sig) for sig in ("function _esc(s) {", "function _nextStepHtml(row) {")
        )
        script = f"""
{fns}
const SOURCE_PIPELINES = {json.dumps(pipelines)};
const row = {json.dumps(row or {"id": "c1", "source_type": "keboola"})};
console.log(JSON.stringify({{ html: _nextStepHtml(row) }}));
"""
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
        return json.loads(proc.stdout)["html"]

    _STEP = {
        "key": "package",
        "text": "Nobody can see this data yet.",
        "cta": "Put them in a package",
        "href": "/admin/tables?unpackaged=1",
        "verify_cta": "Preview a person's Library",
        "verify_href": SIMULATE_HREF,
    }

    def test_a_step_renders_its_sentence_cta_and_verify_link(self):
        html = self._run({"c1": {"feeds": {"packages": 0, "groups": 0, "people": 0, "next": self._STEP}}})
        assert "ds-next" in html
        assert "Nobody can see this data yet." in html
        assert 'href="/admin/tables?unpackaged=1"' in html
        assert f'href="{SIMULATE_HREF}"' in html

    def test_no_step_renders_nothing_at_all(self):
        assert self._run({"c1": {"feeds": {"packages": 2, "groups": 1, "people": 4, "next": None}}}) == ""

    def test_a_source_the_strip_has_never_seen_renders_nothing(self):
        assert self._run({}) == ""

    def test_the_sentence_is_escaped_not_interpolated_raw(self):
        hostile = dict(self._STEP, text="<img src=x onerror=alert(1)>", cta="<b>go</b>")
        html = self._run({"c1": {"feeds": {"packages": 0, "groups": 0, "people": 0, "next": hostile}}})
        assert "<img" not in html
        assert "<b>go</b>" not in html


class TestMarketplacePluginsReachNobody:
    """The same funnel one page over: a synced marketplace lists its plugins
    on /admin/marketplaces while the served feed is grant-filtered, so an
    ungranted plugin is present, visible to the admin, and in nobody's
    Library."""

    def _page(self, seeded_app) -> str:
        return (
            seeded_app["client"]
            .get(
                "/admin/marketplaces",
                headers=_auth(seeded_app["admin_token"]),
            )
            .text
        )

    @pytest.fixture
    def plugin(self, seeded_app):
        from src.repositories import marketplace_plugins_repo, marketplace_registry_repo

        slug = f"mp{uuid.uuid4().hex[:8]}"
        marketplace_registry_repo().register(
            id=slug,
            name="Example Marketplace",
            url="https://git.example.com/org/repo.git",
            registered_by="admin@example.com",
        )
        marketplace_plugins_repo().replace_for_marketplace(
            slug,
            [{"name": "churn-analysis", "description": "Churn skills", "version": "1.0.0"}],
        )
        yield {"slug": slug, "resource_id": f"{slug}/churn-analysis"}
        try:
            marketplace_plugins_repo().clear_for_marketplace(slug)
        finally:
            marketplace_registry_repo().unregister(slug)

    @staticmethod
    def _grant(resource_id: str) -> None:
        from src.repositories import resource_grants_repo, user_groups_repo

        group_id = user_groups_repo().create(
            name=f"Plugin Users {uuid.uuid4().hex[:6]}",
            created_by="admin@example.com",
        )["id"]
        resource_grants_repo().create(
            group_id=group_id,
            resource_type="marketplace_plugin",
            resource_id=resource_id,
            assigned_by="admin@example.com",
        )

    def _grant_everything(self) -> None:
        from src.repositories import marketplace_plugins_repo

        for p in marketplace_plugins_repo().list_all():
            self._grant(f"{p['marketplace_id']}/{p['name']}")

    def test_an_ungranted_plugin_is_counted(self, seeded_app, plugin):
        from app.web.router import _marketplace_plugin_delivery

        delivery = _marketplace_plugin_delivery()
        assert delivery["ungranted"] >= 1
        assert plugin["slug"] in delivery["marketplaces"]

    def test_granting_it_drops_it_out_of_the_count(self, seeded_app, plugin):
        from app.web.router import _marketplace_plugin_delivery

        before = _marketplace_plugin_delivery()["ungranted"]
        self._grant(plugin["resource_id"])
        after = _marketplace_plugin_delivery()
        assert after["ungranted"] == before - 1
        assert plugin["slug"] not in after["marketplaces"]

    def test_the_page_shows_the_strip_while_something_is_ungranted(self, seeded_app, plugin):
        body = self._page(seeded_app)
        assert 'id="mp-ungranted-note"' in body
        assert SIMULATE_HREF.replace("&", "&amp;") in body or SIMULATE_HREF in body

    def test_a_fully_granted_instance_is_not_nagged(self, seeded_app, plugin):
        self._grant_everything()
        assert 'id="mp-ungranted-note"' not in self._page(seeded_app)

    def test_an_unreadable_repo_is_silence_not_a_500(self, seeded_app, monkeypatch):
        """The strip is chrome on a page an admin opens when something is
        already wrong — it degrades to saying nothing."""
        import src.repositories as repos
        from app.web.router import _marketplace_plugin_delivery

        def _boom():
            raise RuntimeError("repo unavailable")

        monkeypatch.setattr(repos, "marketplace_plugins_repo", _boom)
        assert _marketplace_plugin_delivery()["ungranted"] == 0
        resp = seeded_app["client"].get("/admin/marketplaces", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        assert 'id="mp-ungranted-note"' not in resp.text

    def test_a_disabled_plugin_is_not_counted(self, seeded_app, plugin):
        """An admin-disabled plugin is hidden from every served surface, so
        "nobody can see it" is the intended state, not a gap."""
        from app.web.router import _marketplace_plugin_delivery
        from src.repositories import marketplace_plugins_repo

        before = _marketplace_plugin_delivery()["ungranted"]
        marketplace_plugins_repo().set_admin_disabled(plugin["slug"], "churn-analysis", True)
        assert _marketplace_plugin_delivery()["ungranted"] == before - 1

    def test_the_strip_uses_the_shared_admin_vocabulary(self, seeded_app, plugin):
        """`.apg-strip--warn` (css/admin_page.css), the same object the
        package page's "Shared with nobody" band is — not a page-local
        banner."""
        body = self._page(seeded_app)
        at = body.index('id="mp-ungranted-note"')
        strip = body[at - 300 : at + 800]
        assert "apg-strip" in strip
        assert "apg-strip--warn" in strip


class TestTheNewCssIsTokenised:
    """The design system is binding for both templates this touches."""

    @pytest.mark.parametrize("tpl", [_DATA_SOURCES_TPL, _MARKETPLACES_TPL])
    def test_the_new_rules_use_tokens(self, tpl: Path):
        text = tpl.read_text(encoding="utf-8")
        blocks = re.findall(r"\.ds-next[^{]*\{([^}]*)\}", text)
        for block in blocks:
            assert not re.search(r"#[0-9a-fA-F]{3,6}\b", block), f"raw hex in a .ds-next rule: {block}"
            assert "var(--primary" not in block
