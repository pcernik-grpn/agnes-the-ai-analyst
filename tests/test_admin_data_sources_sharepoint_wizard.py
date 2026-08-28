"""The SharePoint connect wizard on /admin/data-sources (spec 2026-08-27
§13.2): a source *type* on the existing picker, three steps
(connect -> scope -> share) in its own drawer — no new nav item, no new
page. Depth here is page-shell markers the JS hangs off + the verbatim
copy the spec pins; the endpoints themselves are covered in
tests/test_admin_sharepoint.py."""

from __future__ import annotations


def _page(seeded_app) -> str:
    c = seeded_app["client"]
    return c.get(
        "/admin/data-sources",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
    ).text


class TestSharePointIsASourceType:
    def test_picker_offers_sharepoint_alongside_the_others(self, seeded_app):
        body = _page(seeded_app)
        assert 'data-wsrc="sharepoint"' in body
        for src in ("keboola", "bigquery", "csv", "jira"):
            assert f'data-wsrc="{src}"' in body

    def test_no_new_nav_item(self, seeded_app):
        """Reached only from the existing /admin/data-sources picker — no
        sidebar/nav link anywhere else points at a SharePoint-only page."""
        c = seeded_app["client"]
        nav = c.get("/dashboard", headers={"Authorization": f"Bearer {seeded_app['admin_token']}"}).text
        assert "sharepoint" not in nav.lower()


class TestThreeStepDrawer:
    def test_drawer_has_exactly_connect_scope_share(self, seeded_app):
        body = _page(seeded_app)
        assert 'id="sp-wizard-overlay"' in body
        assert 'data-spw-step="1"' in body and ">Connect<" in body
        assert 'data-spw-step="2"' in body and ">Scope<" in body
        assert 'data-spw-step="3"' in body and ">Share<" in body
        # The table wizard's fourth ("Bundle") step does not appear in this drawer.
        sp_drawer = body.split('id="sp-wizard-overlay"', 1)[1].split("</script>", 1)[0]
        assert "Bundle" not in sp_drawer

    def test_step1_fields_are_tenant_and_client_id(self, seeded_app):
        """Exact foreign values as FIELDS, never through conversation."""
        body = _page(seeded_app)
        assert 'id="spw-tenant"' in body
        assert 'id="spw-client"' in body
        assert "Tenant ID" in body
        assert "Client (application) ID" in body

    def test_step1_certificate_choice_vault_or_env(self, seeded_app):
        body = _page(seeded_app)
        assert 'id="spw-cert-pem"' in body  # upload-to-vault path
        assert 'id="spw-cert-env-name"' in body  # server env-name path
        assert "never echoed back" in body

    def test_step2_has_anonymize_column_and_the_verbatim_note(self, seeded_app):
        body = _page(seeded_app)
        assert "anonymize" in body
        # The exact note text the spec requires (§13.2).
        assert "original file is not copied" in body
        assert "stores the extracted markdown" in body
        assert "open in the source" in body.lower()

    def test_step3_has_group_badges_and_no_group_warning(self, seeded_app):
        body = _page(seeded_app)
        assert "indexed but invisible" in body
        assert 'id="spw-share-rows"' in body

    def test_step3_has_corpus_map_download(self, seeded_app):
        body = _page(seeded_app)
        assert 'id="spw-corpus-map-link"' in body
        assert "corpus-map" in body
