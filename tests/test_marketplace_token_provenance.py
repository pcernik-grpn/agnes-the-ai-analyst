"""TCRD-230: the marketplace token cell must say more than yes/no.

Every marketplace sync on an instance can run under one person's personal
PAT, and the UI showed only a dot — not which env var holds the secret,
who saved it, or when. A rotation or expiry then becomes an unattributable
fleet-wide sync failure.

The provenance is not new storage: token writes were already audited
(`marketplace.create` with `has_token`, `marketplace.update` with
`token: rotated/cleared`). This projects that trail into the list payload —
`token_env` (a variable NAME, never the value) plus `token_set_by` /
`token_set_at` derived from the newest token-writing audit row. Rows older
than the audit retention window degrade to the env name alone, honestly.
"""

from __future__ import annotations

from pathlib import Path


def _headers(seeded_app):
    return {"Authorization": f"Bearer {seeded_app['admin_token']}"}


def _create(client, headers, slug="provtest", token="ghp_prov_secret_123456"):
    r = client.post(
        "/api/marketplaces",
        headers=headers,
        json={
            "name": "Prov Test",
            "slug": slug,
            "url": f"https://example.com/{slug}.git",
            "token": token,
            "curator_name": "Curator",
            "curator_email": "c@example.com",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _entry(client, headers, slug):
    r = client.get("/api/marketplaces", headers=headers)
    assert r.status_code == 200
    return next(e for e in r.json() if e["id"] == slug)


class TestTokenProvenanceInThePayload:
    def test_create_with_token_carries_env_name_and_actor(self, seeded_app):
        client, headers = seeded_app["client"], _headers(seeded_app)
        _create(client, headers)

        e = _entry(client, headers, "provtest")
        assert e["has_token"] is True
        assert e["token_env"] == "AGNES_MARKETPLACE_PROVTEST_TOKEN", (
            "the env var NAME is what an operator needs to rotate the secret"
        )
        assert e["token_set_by"] == "admin@test.com", "who saved the token — from the marketplace.create audit row"
        assert e["token_set_at"], "when it was saved"
        # The secret itself must never appear anywhere in the payload.
        assert "ghp_prov_secret" not in str(e)

    def test_rotation_updates_the_provenance(self, seeded_app):
        client, headers = seeded_app["client"], _headers(seeded_app)
        _create(client, headers, slug="rotatest")
        first = _entry(client, headers, "rotatest")

        r = client.patch(
            "/api/marketplaces/rotatest",
            headers=headers,
            json={"token": "ghp_second_secret_654321"},
        )
        assert r.status_code == 200, r.text

        second = _entry(client, headers, "rotatest")
        assert second["token_set_at"] >= first["token_set_at"], (
            "a rotation is a new save — provenance must follow the newest write"
        )
        assert second["token_set_by"] == "admin@test.com"

    def test_no_token_means_no_provenance(self, seeded_app):
        client, headers = seeded_app["client"], _headers(seeded_app)
        r = client.post(
            "/api/marketplaces",
            headers=headers,
            json={
                "name": "Bare",
                "slug": "baretest",
                "url": "https://example.com/bare.git",
                "curator_name": "Curator",
                "curator_email": "c@example.com",
            },
        )
        assert r.status_code == 201, r.text
        e = _entry(client, headers, "baretest")
        assert e["has_token"] is False
        assert e["token_set_by"] is None
        assert e["token_set_at"] is None


class TestTokenCellRendersProvenance:
    def test_admin_table_shows_more_than_a_dot(self):
        template = Path("app/web/templates/admin_marketplaces.html").read_text(encoding="utf-8")
        assert "token_set_by" in template, "the token cell must surface who saved the PAT, not only that one exists"
        assert "token_env" in template
