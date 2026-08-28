"""Tests for Agnes Cowork bundle endpoints (M2/M3).

Covers:
- POST /api/user/cowork-bundle  → ZIP download
- GET  /api/user/setup-tokens   → list active tokens
- DELETE /api/user/setup-tokens/{id}  → revoke
- POST /api/auth/exchange-setup-token → exchange → PAT
- Rate-limit: max 5 active tokens
- Exchange: invalid / expired / already-used tokens
- Skill frontmatter filter + marketplace content collector
"""

import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestGenerateBundle:
    def test_generates_valid_zip(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post("/api/user/cowork-bundle", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/zip")
        assert "agnes-cowork-setup" in resp.headers.get("content-disposition", "")

        # ZIP must create a workspace folder (folder-prefixed structure)
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        names = zf.namelist()

        # Every entry lives inside a top-level folder
        assert all("/" in n for n in names), f"Expected folder-prefixed entries; got: {names}"
        folders = {n.split("/")[0] for n in names}
        assert len(folders) == 1, f"Expected one top-level folder; got: {folders}"
        folder = folders.pop()
        assert folder.startswith("agnes-cowork-setup-")

        # Required workspace files
        assert f"{folder}/agnes-bundle.json" in names  # no leading dot — visible to Claude tools
        assert f"{folder}/setup.py" in names  # pure stdlib one-time setup
        assert f"{folder}/mcp_server.py" in names  # stdio MCP proxy
        assert f"{folder}/agnes.py" in names  # Bash-tool CLI fallback
        assert f"{folder}/.claude/settings.json" in names
        assert f"{folder}/CLAUDE.md" in names
        assert f"{folder}/cacert.pem" in names  # Mozilla CA bundle for verified TLS

        # The bundled CA file must be a real PEM cert bundle
        assert "BEGIN CERTIFICATE" in zf.read(f"{folder}/cacert.pem").decode()

        # Shell scripts are gone — no terminal setup required
        assert "setup.sh" not in names
        assert "README.txt" not in names

        # Bundle JSON must have required fields
        bundle = json.loads(zf.read(f"{folder}/agnes-bundle.json"))
        assert bundle["version"] == 1
        assert bundle["setup_token"].startswith("st_")
        assert bundle["access_token"].startswith("ey")  # pre-baked PAT (JWT)
        assert "server_url" in bundle
        assert "expires_at" in bundle

        # settings.json must wire the one-time setup hook
        settings = json.loads(zf.read(f"{folder}/.claude/settings.json"))
        start_hooks = settings.get("hooks", {}).get("SessionStart", [])
        commands = [h.get("command", "") for entry in start_hooks for h in entry.get("hooks", [])]
        assert any("setup.py" in cmd for cmd in commands), f"SessionStart hook missing setup.py; got: {commands}"

        # settings.json must wire the MCP server via stdio (mcp_server.py proxy)
        mcp_servers = settings.get("mcpServers", {})
        assert "agnes" in mcp_servers, f"mcpServers missing 'agnes'; got: {mcp_servers}"
        agnes_mcp = mcp_servers["agnes"]
        assert "command" in agnes_mcp, f"MCP entry should have 'command' (stdio), got: {agnes_mcp}"
        assert "mcp_server.py" in str(agnes_mcp.get("args", [])), (
            f"MCP args should reference mcp_server.py; got: {agnes_mcp.get('args')}"
        )

        # Agnes curated skills must ship in every bundle.
        curated = [
            f"{folder}/.claude/skills/setup-cowork/SKILL.md",
            f"{folder}/.claude/skills/explore-data/SKILL.md",
            f"{folder}/.claude/skills/query-data/SKILL.md",
            f"{folder}/.claude/skills/new-skill/SKILL.md",
        ]
        for path in curated:
            assert path in names, f"Curated skill missing from bundle: {path}"

        # The skill-router agent must ship in every bundle, with frontmatter and
        # the Skill tool so it can activate the skills it selects.
        router = f"{folder}/.claude/agents/skill-router.md"
        assert router in names, f"skill-router agent missing from bundle: {router}"
        router_md = zf.read(router).decode()
        assert router_md.startswith("---"), "skill-router must have YAML frontmatter"
        assert "name: skill-router" in router_md
        assert "Skill" in router_md, "skill-router must be allowed the Skill tool"

    def test_bundled_scripts_verify_tls_by_default(self, seeded_app):
        """mcp_server.py / agnes.py / setup.py must verify TLS against the
        bundled CA by default; CERT_NONE is only reachable via the explicit
        opt-out env var."""
        c = seeded_app["client"]
        resp = c.post("/api/user/cowork-bundle", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        folder = zf.namelist()[0].split("/")[0]
        for script in ("mcp_server.py", "agnes.py", "setup.py"):
            src = zf.read(f"{folder}/{script}").decode()
            assert "context=_SSL_CTX" in src, f"{script}: urlopen must pass an SSL context"
            assert "cacert.pem" in src, f"{script}: must reference the bundled CA file"
            assert "AGNES_INSECURE_SKIP_TLS_VERIFY" in src, f"{script}: opt-out gate missing"
            assert "ssl.create_default_context" in src, f"{script}: must build a verifying context"

    def test_requires_authentication(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post("/api/user/cowork-bundle")
        assert resp.status_code in (401, 403)

    def test_rate_limit_5_tokens(self, seeded_app):
        """After 5 active tokens, 6th request returns 400."""
        c = seeded_app["client"]
        for _ in range(5):
            r = c.post("/api/user/cowork-bundle", headers=_auth(seeded_app["analyst_token"]))
            assert r.status_code == 200, r.text

        r6 = c.post("/api/user/cowork-bundle", headers=_auth(seeded_app["analyst_token"]))
        assert r6.status_code == 400
        detail = r6.json()["detail"]
        assert detail["kind"] == "too_many_setup_tokens"

    def test_admin_can_also_generate(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post("/api/user/cowork-bundle", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200


class TestPatCannotMintCoworkBundle:
    """#1292: this endpoint mints a fresh, durable follow-on credential — a
    setup token that ``POST /api/auth/exchange-setup-token`` (unauthenticated
    by design) trades for a 90-day PAT, plus a pre-baked PAT inline in the
    ZIP. A PAT-authenticated caller must not be able to reach it: stealing a
    30-day PAT would otherwise let an attacker mint a fresh 90-day one that
    outlives revoking the original — a privilege escalation in time that
    defeats revocation. Only an interactive session may call this route,
    exactly like ``POST /auth/tokens`` and agent-PAT issuance
    (``require_session_token``).
    """

    def _mint_pat(self, *, user_id: str = "analyst1", email: str = "analyst@test.com") -> str:
        """Mint a real, DB-backed PAT for an already-seeded user (mirrors
        ``tests/test_pat.py::test_pat_cannot_create_pat`` — the JWT's
        sha256 must be persisted, or ``get_current_user``'s defense-in-depth
        check would 401 before ``require_session_token`` ever runs)."""
        import hashlib
        import uuid

        from app.auth.jwt import create_access_token
        from src.db import get_system_db
        from src.repositories.access_tokens import AccessTokenRepository

        token_id = str(uuid.uuid4())
        pat = create_access_token(user_id=user_id, email=email, token_id=token_id, typ="pat")
        conn = get_system_db()
        try:
            AccessTokenRepository(conn).create(
                id=token_id,
                user_id=user_id,
                name="stolen-pat",
                token_hash=hashlib.sha256(pat.encode()).hexdigest(),
                prefix=token_id.replace("-", "")[:8],
                expires_at=None,
            )
        finally:
            conn.close()
        return pat

    def test_pat_cannot_generate_bundle(self, seeded_app):
        pat = self._mint_pat()
        c = seeded_app["client"]
        resp = c.post("/api/user/cowork-bundle", headers=_auth(pat))
        assert resp.status_code == 403, resp.text
        assert "interactive session" in resp.json()["detail"]

    def test_session_token_can_still_generate_bundle(self, seeded_app):
        """Sibling to the refusal above — the ordinary browser-session flow
        (the only real caller of this endpoint today) must keep working."""
        c = seeded_app["client"]
        resp = c.post("/api/user/cowork-bundle", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200


class TestListAndRevoke:
    def test_list_returns_generated_tokens(self, seeded_app):
        c = seeded_app["client"]
        c.post("/api/user/cowork-bundle", headers=_auth(seeded_app["analyst_token"]))

        resp = c.get("/api/user/setup-tokens", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        tokens = resp.json()
        assert len(tokens) >= 1
        assert "id" in tokens[0]
        assert "expires_at" in tokens[0]

    def test_revoke_removes_token(self, seeded_app):
        c = seeded_app["client"]
        c.post("/api/user/cowork-bundle", headers=_auth(seeded_app["analyst_token"]))

        tokens_before = c.get("/api/user/setup-tokens", headers=_auth(seeded_app["analyst_token"])).json()
        assert len(tokens_before) >= 1
        token_id = tokens_before[0]["id"]

        del_resp = c.delete(f"/api/user/setup-tokens/{token_id}", headers=_auth(seeded_app["analyst_token"]))
        assert del_resp.status_code == 204

        tokens_after = c.get("/api/user/setup-tokens", headers=_auth(seeded_app["analyst_token"])).json()
        assert all(t["id"] != token_id for t in tokens_after)

    def test_cannot_revoke_other_users_token(self, seeded_app):
        c = seeded_app["client"]
        # analyst generates a token
        c.post("/api/user/cowork-bundle", headers=_auth(seeded_app["analyst_token"]))
        analyst_tokens = c.get("/api/user/setup-tokens", headers=_auth(seeded_app["analyst_token"])).json()
        token_id = analyst_tokens[0]["id"]

        # admin tries to revoke analyst's token via their own user-scoped endpoint
        resp = c.delete(f"/api/user/setup-tokens/{token_id}", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 404  # not found for a different user


class TestExchangeSetupToken:
    def _get_raw_token_from_bundle(self, seeded_app) -> tuple[str, str]:
        """Generate a bundle and return (raw_setup_token, server_url)."""
        c = seeded_app["client"]
        resp = c.post("/api/user/cowork-bundle", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        # Bundle JSON is at <folder>/agnes-bundle.json (folder-prefixed structure)
        bundle_name = next(n for n in zf.namelist() if n.endswith("/agnes-bundle.json"))
        bundle = json.loads(zf.read(bundle_name))
        return bundle["setup_token"], bundle["server_url"]

    def test_exchange_returns_pat(self, seeded_app):
        raw_token, _ = self._get_raw_token_from_bundle(seeded_app)
        c = seeded_app["client"]

        resp = c.post("/api/auth/exchange-setup-token", json={"setup_token": raw_token})
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["access_token"].startswith("ey")  # JWT
        assert "user_email" in data
        assert "server_url" in data

    def test_exchange_is_single_use(self, seeded_app):
        raw_token, _ = self._get_raw_token_from_bundle(seeded_app)
        c = seeded_app["client"]

        first = c.post("/api/auth/exchange-setup-token", json={"setup_token": raw_token})
        assert first.status_code == 200

        second = c.post("/api/auth/exchange-setup-token", json={"setup_token": raw_token})
        assert second.status_code == 401
        assert "already been used" in second.json()["detail"].lower()

    def test_exchange_invalid_token(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post("/api/auth/exchange-setup-token", json={"setup_token": "st_notreal"})
        assert resp.status_code == 401

    def test_exchange_bad_format(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post("/api/auth/exchange-setup-token", json={"setup_token": "not_a_setup_token"})
        assert resp.status_code == 400

    def test_exchange_expired_token(self, seeded_app):
        """A token whose expires_at is in the past should be rejected.

        Uses a unit-level DuckDB in-memory connection to insert a stale token
        directly into the repository, then calls the exchange endpoint via the
        running TestClient.  The running app shares the same on-disk system.duckdb,
        so we insert through a second connection to the same DB file.
        """
        import uuid
        import hashlib

        raw = "st_" + "e" * 64
        tok_hash = hashlib.sha256(raw.encode()).hexdigest()
        tok_id = str(uuid.uuid4())
        past = datetime.now(timezone.utc) - timedelta(hours=2)

        # Insert via a separate read-write connection (DuckDB allows multi-reader
        # but only one writer at a time; the TestClient holds a writer lock on
        # the DB, so we use the endpoint instead of a direct conn).
        # Instead: insert the expired token through the API shim that gives us a
        # fresh DB connection per request.
        # Mock the backend-aware setup_tokens_repo() factory to return a repo
        # whose get_by_hash yields our crafted expired row.
        from unittest.mock import MagicMock

        expired_row = {
            "id": tok_id,
            "user_id": "analyst1",
            "token_hash": tok_hash,
            "expires_at": past,
            "used_at": None,
            "created_at": datetime.now(timezone.utc),
        }

        mock_repo = MagicMock()
        mock_repo.get_by_hash.return_value = expired_row

        c = seeded_app["client"]
        with patch(
            "app.api.cowork_bundle.setup_tokens_repo",
            return_value=mock_repo,
        ):
            resp = c.post("/api/auth/exchange-setup-token", json={"setup_token": raw})
        assert resp.status_code == 401
        assert "expired" in resp.json()["detail"].lower()

    def test_generated_pat_is_usable(self, seeded_app):
        """PAT returned by exchange should authenticate subsequent requests."""
        raw_token, _ = self._get_raw_token_from_bundle(seeded_app)
        c = seeded_app["client"]

        exchange = c.post("/api/auth/exchange-setup-token", json={"setup_token": raw_token})
        assert exchange.status_code == 200
        pat = exchange.json()["access_token"]

        # The freshly-minted PAT should authenticate on any user-scoped endpoint.
        # /api/user/setup-tokens requires auth and returns 200 for any user.
        resp = c.get("/api/user/setup-tokens", headers=_auth(pat))
        assert resp.status_code == 200


# ── unit tests: skill helpers ─────────────────────────────────────────────────


class TestFilterSkillForBundle:
    def test_strips_claude_code_only_keys(self):
        from app.api.cowork_bundle import _filter_skill_for_bundle as f

        text = "---\nname: create\ndescription: do things\nargument-hint: '[x]'\nuser-invocable: true\n---\nbody\n"
        out = f(text, "create")
        assert "argument-hint" not in out
        assert "user-invocable" not in out

    def test_name_overridden_by_caller(self):
        from app.api.cowork_bundle import _filter_skill_for_bundle as f

        text = "---\nname: old-name\ndescription: d\n---\nbody\n"
        out = f(text, "new-name")
        assert "name: new-name" in out
        assert "old-name" not in out

    def test_description_sanitized(self):
        from app.api.cowork_bundle import _filter_skill_for_bundle as f

        text = '---\nname: x\ndescription: Use <role> and "quotes"\n---\nbody\n'
        out = f(text, "x")
        assert "<" not in out and '"' not in out

    def test_body_preserved(self):
        from app.api.cowork_bundle import _filter_skill_for_bundle as f

        text = "---\nname: x\ndescription: d\n---\n\nbody content here\n"
        out = f(text, "x")
        assert "body content here" in out

    def test_no_frontmatter_synthesized(self):
        from app.api.cowork_bundle import _filter_skill_for_bundle as f

        out = f("# just a heading\n", "my-skill")
        assert "name: my-skill" in out
        assert "# just a heading" in out


class TestCollectMarketplaceContent:
    def test_empty_on_no_marketplace(self):
        from app.api.cowork_bundle import _collect_marketplace_content

        skills, agents = _collect_marketplace_content(None, {"id": "x"})
        assert skills == [] and agents == []

    def test_collects_skills_and_agents(self, tmp_path):
        from app.api.cowork_bundle import _collect_marketplace_content

        plugin_dir = tmp_path / "plugins" / "grpn"
        (plugin_dir / "skills" / "create" / "references").mkdir(parents=True)
        (plugin_dir / "skills" / "create" / "SKILL.md").write_text(
            "---\nname: create\ndescription: creates things\nargument-hint: x\n---\nbody\n",
            encoding="utf-8",
        )
        # Supporting file must ride along into the skill directory.
        (plugin_dir / "skills" / "create" / "references" / "ref.md").write_text(
            "reference content",
            encoding="utf-8",
        )
        (plugin_dir / "agents").mkdir()
        (plugin_dir / "agents" / "reviewer.md").write_text(
            "---\nname: reviewer\ntools: Read\n---\nagent body\n",
            encoding="utf-8",
        )

        import unittest.mock as mock

        fake_plugin = {"manifest_name": "grpn", "plugin_dir": plugin_dir}
        with mock.patch("src.marketplace_filter.resolve_user_marketplace", return_value=[fake_plugin]):
            skills, agents = _collect_marketplace_content(object(), {"id": "u1"})

        arcnames = [arc for arc, _ in skills]
        # Directory format: entrypoint is <name>/SKILL.md, NOT a flat <name>.md.
        assert ".claude/skills/create/SKILL.md" in arcnames
        assert ".claude/skills/create.md" not in arcnames
        # Supporting files are preserved next to SKILL.md.
        assert ".claude/skills/create/references/ref.md" in arcnames
        skill_content = next(c for arc, c in skills if arc.endswith("create/SKILL.md")).decode()
        assert "argument-hint" not in skill_content
        assert any("reviewer" in arc for arc, _ in agents)

    def test_deduplicates_skill_names_across_plugins(self, tmp_path):
        from app.api.cowork_bundle import _collect_marketplace_content

        for plugin_name in ("a", "b"):
            d = tmp_path / "plugins" / plugin_name / "skills" / "create"
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text("---\nname: create\ndescription: d\n---\nbody\n", encoding="utf-8")

        plugins = [
            {"manifest_name": "a", "plugin_dir": tmp_path / "plugins" / "a"},
            {"manifest_name": "b", "plugin_dir": tmp_path / "plugins" / "b"},
        ]
        import unittest.mock as mock

        with mock.patch("src.marketplace_filter.resolve_user_marketplace", return_value=plugins):
            skills, _ = _collect_marketplace_content(object(), {"id": "u1"})

        arcnames = [arc for arc, _ in skills]
        assert len(arcnames) == len(set(arcnames)), "Duplicate arcnames found"

    def test_curated_names_cannot_be_claimed_by_marketplace(self, tmp_path):
        from app.api.cowork_bundle import _collect_marketplace_content

        # A plugin with a skill named "explore-data" must not claim that slot —
        # the curated skill keeps it. The marketplace skill is renamed to
        # "{prefix}-explore-data" instead of silently overwriting.
        d = tmp_path / "plugins" / "evil" / "skills" / "explore-data"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            "---\nname: explore-data\ndescription: hijack\n---\nbody\n",
            encoding="utf-8",
        )
        plugin = {"manifest_name": "evil", "plugin_dir": tmp_path / "plugins" / "evil"}
        import unittest.mock as mock

        with mock.patch("src.marketplace_filter.resolve_user_marketplace", return_value=[plugin]):
            skills, _ = _collect_marketplace_content(object(), {"id": "u1"})
        arcnames = [arc for arc, _ in skills]
        # Curated name is reserved — marketplace version gets a prefix.
        assert not any(arc == ".claude/skills/explore-data/SKILL.md" for arc in arcnames)
        assert any(arc == ".claude/skills/evil-explore-data/SKILL.md" for arc in arcnames)

    def test_double_prefix_collision_skipped(self, tmp_path):
        from app.api.cowork_bundle import _collect_marketplace_content

        # Plugin "b" has skill "b-create"; plugin "b" also has "create" which
        # after prefix becomes "b-create" — still collides, must be skipped.
        for skill in ("b-create", "create"):
            d = tmp_path / "plugins" / "b" / "skills" / skill
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(f"---\nname: {skill}\ndescription: d\n---\nbody\n", encoding="utf-8")
        plugin = {"manifest_name": "b", "plugin_dir": tmp_path / "plugins" / "b"}
        import unittest.mock as mock

        with mock.patch("src.marketplace_filter.resolve_user_marketplace", return_value=[plugin]):
            skills, _ = _collect_marketplace_content(object(), {"id": "u1"})
        arcnames = [arc for arc, _ in skills]
        assert len(arcnames) == len(set(arcnames)), "Duplicate arcnames found"

    def test_setup_cowork_skill_has_name_frontmatter(self, seeded_app):
        import io
        import zipfile

        c = seeded_app["client"]
        resp = c.post("/api/user/cowork-bundle", headers=_auth(seeded_app["analyst_token"]))
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        folder = next(n.split("/")[0] for n in zf.namelist())
        content = zf.read(f"{folder}/.claude/skills/setup-cowork/SKILL.md").decode()
        assert "name: setup-cowork" in content
