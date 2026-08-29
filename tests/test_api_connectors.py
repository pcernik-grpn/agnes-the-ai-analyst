"""Integration tests for the connectors API.

Covers:
  * GET /api/connectors/manifest auth gate (401 when no token)
  * 200 + bundled connectors when no IWT configured
  * source flag flips between iwt / bundled
  * GET /api/connectors/params returns shape with globals + per-connector
    blocks parsed from instance.yaml overlay
  * Auth-required (no anonymous access)
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client_with_admin(monkeypatch, tmp_path: Path):
    """Boot the FastAPI app against a temp DATA_DIR + bootstrap an admin
    user, return (client, token).
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # Disable LLM guardrails so the test boot doesn't warn about API keys.
    monkeypatch.setenv("AGNES_DISABLE_GUARDRAILS", "1")

    from app.main import app

    client = TestClient(app)
    resp = client.post(
        "/auth/bootstrap",
        json={
            "email": "admin@example.com",
            "name": "Admin",
            "password": "TestPass123!",
        },
    )
    if resp.status_code == 403:
        # Users already exist on a re-run — skip; admin tests do this on fresh DBs only.
        pytest.skip("admin already bootstrapped")
    assert resp.status_code == 200, resp.text
    return client, resp.json()["access_token"]


def _gws_creds(
    configured: bool = True,
    client_id: str = "123456789012-abc.apps.googleusercontent.com",
    project_id: str = "123456789012",
    secret: str = "GOCSPX-test-secret-value",
) -> dict:
    """Shape returned by app.instance_config.get_gws_oauth_credentials."""
    return {
        "client_id": client_id if configured else "",
        "client_secret": secret if configured else "",
        "project_id": project_id if configured else "",
        "oauthlib_insecure_transport": "1",
        "configured": configured,
    }


def test_manifest_requires_auth(client_with_admin):
    client, _token = client_with_admin
    resp = client.get("/api/connectors/manifest")
    # No Authorization header → 401 (FastAPI auth dependency rejects)
    assert resp.status_code in (401, 403)


def test_manifest_returns_bundled_when_no_iwt(client_with_admin):
    """Fresh install (no Initial Workspace Template configured) → manifest
    sources from the bundled seed inside the wheel. The bundle ships the
    three canonical connectors (asana, atlassian, gws).
    """
    client, token = client_with_admin
    resp = client.get(
        "/api/connectors/manifest",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["schema_version"] == 2
    assert body["source"] == "bundled"
    slugs = sorted(c["slug"] for c in body["connectors"])
    assert slugs == [
        "connector-asana",
        "connector-atlassian",
        "connector-gws",
    ]
    # Sanity-check fields make it through unmolested
    asana = next(c for c in body["connectors"] if c["slug"] == "connector-asana")
    assert asana["display_name"] == "Asana"
    assert asana["estimated_minutes"] > 0
    assert asana["vendor_url"].startswith("https://")


def test_manifest_exposes_required_flag(client_with_admin):
    """Schema v2: every entry carries `required`; the bundled connectors
    are all optional (pins the OSS default), and a required=True entry
    round-trips through _entry_to_meta.
    """
    client, token = client_with_admin
    resp = client.get(
        "/api/connectors/manifest",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert all(c["required"] is False for c in body["connectors"])

    from app.api.connectors import _entry_to_meta
    from src.connectors_manifest import ConnectorEntry

    meta = _entry_to_meta(
        ConnectorEntry(
            slug="connector-x",
            display_name="X",
            short_summary="s",
            estimated_minutes=1,
            required=True,
        )
    )
    assert meta.required is True


def test_params_empty_when_overlay_absent(client_with_admin, monkeypatch):
    """No `connectors:` section in instance.yaml → endpoint returns empty
    params + empty globals. `agnes init` treats this as "use defaults".

    GWS pinned to unconfigured so a developer's local env vars /
    instance.yaml can't inject the server-side connector-gws fallback
    and flake this test.
    """
    client, token = client_with_admin
    monkeypatch.setattr(
        "app.instance_config.get_gws_oauth_credentials",
        lambda: _gws_creds(configured=False),
    )
    resp = client.get(
        "/api/connectors/params",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["schema_version"] == 2
    assert body["params"] == {}
    assert body["globals"] == {}


def test_params_filters_overlay_to_manifest_allowlist(client_with_admin, monkeypatch, caplog):
    """Code review on PR #462: the per-tenant `connectors:` overlay was
    emitted verbatim, so an operator typo (`connector-atlasian:` instead
    of `connector-atlassian:`) would land in the analyst's `.env` as a
    junk slug, polluting it AND silently dropping the real connector's
    params. The manifest is the source of truth for "which slugs
    exist"; everything else is dropped + logged at WARNING.

    `globals:` bypasses the allowlist (it's not slug-scoped) — verify it
    still passes through unchanged.
    """
    client, token = client_with_admin

    # GWS pinned to unconfigured (same rationale as
    # test_params_empty_when_overlay_absent): the exact-equality assert
    # below would flake for a developer whose local env / instance.yaml
    # configures the shared GWS OAuth client.
    monkeypatch.setattr(
        "app.instance_config.get_gws_oauth_credentials",
        lambda: _gws_creds(configured=False),
    )

    # Synthesize the overlay shape `_load_current_instance_yaml` returns.
    # Three keys exercise the three branches: one valid manifest slug
    # (asana — should survive), one typo of a real slug (atlasian —
    # should drop), one completely unrelated key (random-junk — should
    # also drop). globals is non-slug-scoped — should always pass.
    overlay = {
        "connectors": {
            "globals": {"AGNES_INSTANCE_BRAND": "Acme"},
            "connector-asana": {"AGNES_ASANA_PAT_ENV": "AGNES_ASANA_PAT"},
            "connector-atlasian": {"ATLASSIAN_BASE_URL": "https://typo.example"},
            "random-junk": {"X": "Y"},
        },
    }
    monkeypatch.setattr(
        "app.api.admin._load_current_instance_yaml",
        lambda: overlay,
    )

    import logging

    with caplog.at_level(logging.WARNING, logger="app.api.connectors"):
        resp = client.get(
            "/api/connectors/params",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    body = resp.json()
    # Only the manifest-known slug survives. Operator typo + unrelated
    # key are silently dropped (silent FROM the analyst's perspective;
    # noisy in the server log — asserted below).
    assert body["params"] == {
        "connector-asana": {"AGNES_ASANA_PAT_ENV": "AGNES_ASANA_PAT"},
    }
    # globals bypass the allowlist.
    assert body["globals"] == {"AGNES_INSTANCE_BRAND": "Acme"}
    # Server-side warning names BOTH ignored slugs (sorted for stable
    # diagnostic output) so the operator can spot a typo in logs.
    warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("connector-atlasian" in m and "random-junk" in m for m in warnings), (
        f"expected single warning naming both ignored slugs; got {warnings}"
    )


def test_params_gws_server_credentials_injected(client_with_admin, monkeypatch):
    """Operator-provisioned GWS OAuth creds (server env vars / vault /
    `instance.gws` YAML — resolved by get_gws_oauth_credentials) must
    reach analysts through /api/connectors/params even when the
    `connectors:` overlay never mentions connector-gws. Regression test
    for the A1.2 refactor which retired the only consumer of
    get_gws_oauth_credentials, silently forcing every analyst into the
    manual GCP-project walkthrough (skill Branch B).
    """
    client, token = client_with_admin
    monkeypatch.setattr(
        "app.instance_config.get_gws_oauth_credentials",
        lambda: _gws_creds(),
    )
    resp = client.get(
        "/api/connectors/params",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["params"]["connector-gws"] == {
        "AGNES_GWS_CLIENT_ID": "123456789012-abc.apps.googleusercontent.com",
        "AGNES_GWS_PROJECT_ID": "123456789012",
        # The fallback ships the client-secret VALUE (a Desktop-app
        # OAuth client secret is an app identifier, not a user
        # credential) so the analyst-side skill can self-serve…
        "AGNES_GWS_CLIENT_SECRET": "GOCSPX-test-secret-value",
        # …and keeps the legacy pointer next to it for backward
        # compatibility with seed skills that read the pointer shape.
        "AGNES_GWS_CLIENT_SECRET_ENV": "AGNES_GWS_CLIENT_SECRET",
    }


def test_params_overlay_wins_over_server_gws(client_with_admin, monkeypatch):
    """The `connectors:` overlay stays authoritative: keys it sets win
    over the server-resolved GWS creds; keys it omits are backfilled.
    """
    client, token = client_with_admin
    monkeypatch.setattr(
        "app.instance_config.get_gws_oauth_credentials",
        lambda: _gws_creds(),
    )
    overlay = {
        "connectors": {
            "connector-gws": {
                "AGNES_GWS_CLIENT_ID": "999-overlay.apps.googleusercontent.com",
            },
        },
    }
    monkeypatch.setattr(
        "app.api.admin._load_current_instance_yaml",
        lambda: overlay,
    )
    resp = client.get(
        "/api/connectors/params",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    gws = resp.json()["params"]["connector-gws"]
    # Overlay key wins…
    assert gws["AGNES_GWS_CLIENT_ID"] == "999-overlay.apps.googleusercontent.com"
    # …server-resolved keys fill the gaps.
    assert gws["AGNES_GWS_PROJECT_ID"] == "123456789012"
    assert gws["AGNES_GWS_CLIENT_SECRET"] == "GOCSPX-test-secret-value"
    assert gws["AGNES_GWS_CLIENT_SECRET_ENV"] == "AGNES_GWS_CLIENT_SECRET"


def test_params_gws_not_injected_when_unconfigured(client_with_admin, monkeypatch):
    """Half-configured or absent operator creds (`configured=False`) →
    no connector-gws block appears; the seed skill falls back to its
    interactive branch instead of receiving empty strings.
    """
    client, token = client_with_admin
    monkeypatch.setattr(
        "app.instance_config.get_gws_oauth_credentials",
        lambda: _gws_creds(configured=False),
    )
    resp = client.get(
        "/api/connectors/params",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert "connector-gws" not in resp.json()["params"]


def test_bundled_seed_files_present():
    """The wheel-resident bundled seed must include the install-prompt
    template + the three connector SKILL.md files. This guards against
    a release that forgot to update src/_bundled_seed/ via
    scripts/sync_bundled_seed.sh.
    """
    from src.initial_workspace import bundled_seed_path

    bundle = bundled_seed_path()
    assert (bundle / "install-prompt" / "template.md.tmpl").is_file()
    for slug in ("connector-asana", "connector-atlassian", "connector-gws"):
        assert (bundle / "workspace" / ".claude" / "skills" / slug / "SKILL.md").is_file(), f"missing bundled {slug}"
    assert (bundle / ".source_ref").is_file()


# ---------------------------------------------------------------------------
# GET /api/connectors/{slug}/prompt — the on-demand connector setup prompt
# (the install prompt references it via `agnes connectors show <slug>`
# instead of inlining every SKILL.md body).
# ---------------------------------------------------------------------------


def test_prompt_requires_auth(client_with_admin):
    client, _token = client_with_admin
    resp = client.get("/api/connectors/connector-asana/prompt")
    assert resp.status_code in (401, 403)


def test_prompt_returns_bundled_body(client_with_admin):
    client, token = client_with_admin
    resp = client.get(
        "/api/connectors/connector-asana/prompt",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["slug"] == "connector-asana"
    assert body["display_name"] == "Asana"
    assert body["source"] == "bundled"
    # The body is the post-frontmatter SKILL.md prose — no YAML fence.
    assert not body["prompt"].lstrip().startswith("---")
    # Content locks that used to live on the inline renderer: the Asana
    # flow is PAT + REST, not the hosted MCP.
    assert "app.asana.com/api/1.0" in body["prompt"]
    # {instance_brand} is substituted server-side (default brand: Agnes).
    assert "{instance_brand}" not in body["prompt"]


def test_prompt_unknown_slug_404_hints_list(client_with_admin):
    """A slug outside the manifest is a registry miss with a next-step
    hint. (Traversal-shaped slugs can't even reach the handler: Starlette's
    `{slug}` converter is single-segment and URL-aware clients normalize
    `../` before sending — the handler-level gate is manifest equality,
    locked by `test_prompt_manifest_equality_is_the_gate`; the
    filesystem-level gate is locked by
    `test_body_loader_contains_traversal_shaped_slugs`.)"""
    client, token = client_with_admin
    resp = client.get(
        "/api/connectors/connector-nope/prompt",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert detail["kind"] == "unknown_connector"
    assert "agnes tools list" in detail["hint"]


def test_prompt_manifest_equality_is_the_gate(client_with_admin, monkeypatch):
    """The handler resolves a body ONLY for slugs the manifest emitted —
    the delete-the-check regression lock the old `../../` URL test could
    not provide (client-side URL normalization meant that request never
    reached the handler). A slug absent from the manifest 404s as
    `unknown_connector` BEFORE any body loading happens, even when a
    body would resolve for it."""
    calls: list[str] = []

    import app.api.connectors as mod

    def spy_load(slug):
        calls.append(slug)
        return ("SHOULD NEVER BE SERVED", "bundled")

    monkeypatch.setattr(mod, "load_connector_body", spy_load)
    client, token = client_with_admin
    resp = client.get(
        "/api/connectors/connector-not-in-manifest/prompt",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["kind"] == "unknown_connector"
    assert calls == []  # the body loader was never consulted
    assert "SHOULD NEVER BE SERVED" not in resp.text


def test_body_loader_contains_traversal_shaped_slugs(tmp_path):
    """Defense-in-depth below the manifest gate: even if a traversal-shaped
    slug reached `load_connector_body`, `resolve_seed_file`'s realpath
    containment (`_is_within`) refuses to read outside the seed roots.
    Exercised directly because the HTTP layer structurally can't deliver
    a slug containing `/` (single-segment route param)."""
    from src.connectors_manifest import load_connector_body

    # A real file outside the seed roots that would leak if containment
    # failed.
    outside = tmp_path / "SKILL.md"
    outside.write_text("SECRET", encoding="utf-8")
    up = "/".join([".."] * 12)
    assert load_connector_body(f"{up}{tmp_path}") is None
    assert load_connector_body("../../../../etc/passwd") is None
