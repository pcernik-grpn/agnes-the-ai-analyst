"""Agnes Cowork setup bundle endpoints.

One-click Claude Code setup flow (no terminal needed):

  POST /api/user/cowork-bundle          — generate + return a ZIP bundle
  GET  /api/user/setup-tokens           — list active setup tokens (for UI revoke)
  DELETE /api/user/setup-tokens/{id}    — revoke a setup token
  POST /api/auth/exchange-setup-token   — exchange setup token → PAT (no auth required)

Bundle structure (unzipping creates a ready-to-open Claude Code workspace)::

  agnes-cowork-setup-<ts>/
  ├── agnes-bundle.json         ← setup token + server URL (visible to Claude tools)
  ├── setup.py                  ← pure stdlib fallback (no pip required)
  ├── .claude/
  │   ├── settings.json         ← SessionStart hook: ``agnes init --bundle .``
  │   ├── skills/
  │   │   ├── setup-cowork.md   ← /setup-cowork — guided onboarding
  │   │   ├── explore-data.md   ← /explore-data — catalog + describe + suggest
  │   │   ├── query-data.md     ← /query-data — schema-aware SQL workflow
  │   │   ├── new-skill.md      ← /new-skill — design + write a new skill
  │   │   └── <marketplace skills from user's RBAC-granted plugins>
  │   └── agents/
  │       └── <marketplace agents from user's RBAC-granted plugins>
  └── CLAUDE.md                 ← user-friendly instructions + agent guidance

User flow:
  1. Download ZIP from /me/ai-connector.
  2. Unzip the file.
  3. Open Claude Code → File → Open Folder → select the unzipped folder.
  4. The SessionStart hook fires ``agnes init --bundle .`` which exchanges the
     token, configures credentials, installs the pull/push hooks, and runs the
     first data pull — all automatically.

After the first successful ``agnes init``, ``install_claude_hooks`` removes the
``agnes init`` entry (matched via ``_OUR_COMMAND_MARKERS``) and replaces it with
the standard ``agnes pull`` / ``agnes push`` hooks, so the setup is truly one-time.

Security model:
- Setup tokens are short-lived (24 h), single-use, stored hashed (SHA-256).
- The raw token lives only in the ZIP bundle — never in server logs or DB.
- ``POST /api/auth/exchange-setup-token`` is the only unauthenticated endpoint;
  the setup token IS the auth credential for that one call.
- After exchange, the setup token is atomically marked used and cannot be
  reused (replay prevention).
- Max 5 active setup tokens per user — UI shows a warning above that limit.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import textwrap
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from typing import List

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.auth.dependencies import _get_db, get_current_user, require_session_token
from app.auth.jwt import create_access_token
from src.repositories import (
    access_token_repo,
    audit_repo,
    setup_tokens_repo,
    users_repo,
)

# ── routers ──────────────────────────────────────────────────────────────────

# User-scoped (auth required): bundle generation + token management
user_router = APIRouter(prefix="/api/user", tags=["cowork"])

# Auth-scoped (no auth): setup token exchange
auth_router = APIRouter(prefix="/api/auth", tags=["cowork"])

# Max active setup tokens per user before the UI shows a warning
_MAX_ACTIVE_TOKENS = 5

# Setup token TTL
_SETUP_TOKEN_TTL = timedelta(hours=24)


# ── helpers ───────────────────────────────────────────────────────────────────


def _audit(conn, actor: str, action: str, target: str, params=None):
    try:
        audit_repo().log(
            user_id=actor,
            action=action,
            resource=f"setup_token:{target}",
            params=params,
        )
    except Exception:
        pass


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _generate_setup_token() -> str:
    """Return a 67-char ``st_<64 url-safe base64 chars>`` token."""
    return "st_" + secrets.token_urlsafe(48)


# ── skill frontmatter helpers ─────────────────────────────────────────────────

_SKILL_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

# Names reserved for Agnes curated skills — marketplace content must never
# claim these so a plugin can't silently overwrite onboarding/workflow skills.
_CURATED_SKILL_NAMES: frozenset[str] = frozenset(
    {
        "setup-cowork",
        "explore-data",
        "query-data",
        "new-skill",
    }
)

# Names reserved for Agnes curated agents — same protection as curated skills,
# so a marketplace plugin can't shadow the bundled skill-router agent.
_CURATED_AGENT_NAMES: frozenset[str] = frozenset(
    {
        "skill-router",
    }
)


def _filter_skill_for_bundle(text: str, name: str) -> str:
    """Keep name/description/compatibility; drop Claude-Code-only keys.

    Name is always overridden by the caller-supplied ``name`` (the skill's
    folder name, de-duplicated across plugins) so the slash command is
    predictable regardless of what's in the source SKILL.md frontmatter.
    """
    m = _SKILL_FM_RE.match(text)
    if not m:
        return f"---\nname: {name}\ndescription: {name}\n---\n\n{text.lstrip()}"
    block, body = m.group(1), text[m.end() :]
    parsed: dict = {}
    for line in block.splitlines():
        if ":" in line and not line.startswith((" ", "\t")):
            k, _, v = line.partition(":")
            parsed[k.strip()] = v.strip().strip('"').strip("'")
    lines = [f"name: {name}"]
    if "description" in parsed:
        desc = parsed["description"].replace("<", "").replace(">", "").replace('"', "'")
        lines.append(f"description: {desc}")
    if "compatibility" in parsed:
        lines.append(f"compatibility: {parsed['compatibility']}")
    return "---\n" + "\n".join(lines) + "\n---\n" + body.lstrip("\n")


def _collect_marketplace_content(conn, user: dict) -> tuple[list[tuple[str, bytes]], list[tuple[str, bytes]]]:
    """Return (skills, agents) lists of (arcname, bytes) from the user's granted plugins.

    Skills land in ``.claude/skills/<name>/SKILL.md`` (directory format, with
    any supporting files preserved alongside); agents in
    ``.claude/agents/<name>.md`` (agents are flat by convention).
    Names are de-duplicated by prefixing the plugin's manifest_name when a
    collision would otherwise occur. Failures are silently swallowed — a
    missing marketplace is not a bundle error.
    """
    skills: list[tuple[str, bytes]] = []
    agents: list[tuple[str, bytes]] = []
    try:
        from src import marketplace_filter

        plugins = marketplace_filter.resolve_user_marketplace(conn, user)
    except Exception:
        return skills, agents

    seen_skill: set[str] = set(_CURATED_SKILL_NAMES)  # reserves curated names
    seen_agent: set[str] = set(_CURATED_AGENT_NAMES)  # reserves curated agents

    for plugin in plugins:
        plugin_dir = plugin.get("plugin_dir")
        if not plugin_dir or not plugin_dir.is_dir():
            continue
        prefix = plugin.get("manifest_name") or "plugin"

        skills_dir = plugin_dir / "skills"
        if skills_dir.is_dir():
            for folder in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
                skill_md = next(
                    (f for f in folder.iterdir() if f.name.lower() == "skill.md" and f.is_file()),
                    None,
                )
                if skill_md is None:
                    continue
                try:
                    raw = skill_md.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                name = folder.name
                if name in seen_skill:
                    name = f"{prefix}-{folder.name}"
                if name in seen_skill:
                    continue  # still collides after prefix — skip
                seen_skill.add(name)
                filtered = _filter_skill_for_bundle(raw, name)
                # Claude Code loads a skill as a DIRECTORY: the entrypoint must
                # be ``.claude/skills/<name>/SKILL.md`` (a flat ``<name>.md`` is
                # never picked up). Emit the rewritten SKILL.md plus every
                # supporting file (references/, assets/, ...) so the skill
                # arrives intact and Claude can auto-select it.
                skills.append((f".claude/skills/{name}/SKILL.md", filtered.encode("utf-8")))
                for extra in sorted(folder.rglob("*")):
                    if not extra.is_file() or extra == skill_md:
                        continue
                    if ".git" in extra.parts:
                        continue
                    try:
                        data = extra.read_bytes()
                    except OSError:
                        continue
                    rel = extra.relative_to(folder).as_posix()
                    skills.append((f".claude/skills/{name}/{rel}", data))

        agents_dir = plugin_dir / "agents"
        if agents_dir.is_dir():
            for agent_md in sorted(agents_dir.glob("*.md")):
                try:
                    raw = agent_md.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                name = agent_md.stem
                if name in seen_agent:
                    name = f"{prefix}-{agent_md.stem}"
                if name in seen_agent:
                    continue  # still collides after prefix — skip
                seen_agent.add(name)
                agents.append((f".claude/agents/{name}.md", raw.encode("utf-8")))

    return skills, agents


# ── bundle generation helpers ─────────────────────────────────────────────────


def _bundle_settings_json(server_url: str, access_token: str) -> str:  # noqa: ARG001
    """Return .claude/settings.json content for the setup bundle.

    Uses stdio transport (python3 mcp_server.py) — the bundled mcp_server.py
    is a pure-stdlib REST proxy that requires no Agnes installation and works
    inside the cowork VM on the very first session open.

    The SessionStart hook runs setup.py once to exchange the short-lived bundle
    token for a 90-day PAT and write credentials to ~/.config/agnes/.
    After that run setup.py deletes itself from the hook and replaces it with
    ``agnes pull``.
    """
    _init_cmd = "python3 setup.py 2>/dev/null || python setup.py 2>/dev/null || true"
    cfg = {
        "model": "sonnet",
        "permissions": {
            "allow": ["Read", "Bash", "Bash(agnes *)", "Grep", "Glob"],
        },
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": _init_cmd,
                        }
                    ]
                }
            ]
        },
        # stdio transport: mcp_server.py is a pure-stdlib REST proxy bundled
        # in the ZIP root. No Agnes package install needed — works on first
        # session open. Uses relative path so it resolves from the project root.
        "mcpServers": {
            "agnes": {
                "command": "python3",
                "args": ["mcp_server.py"],
            }
        },
    }
    return json.dumps(cfg, indent=2) + "\n"


def _bundle_mcp_server_py() -> str:
    """Return mcp_server.py — pure-stdlib stdio MCP proxy.

    No Agnes package install required. Reads credentials from agnes-bundle.json
    (pre-baked on first open, before setup.py fires) or ~/.config/agnes/token.json
    (after setup.py runs). Proxies every tool call to the Agnes REST API over
    HTTP — works inside the cowork VM as long as the Agnes server is reachable.

    Tools: server_info, catalog, schema, describe, query, skills.
    """
    return textwrap.dedent("""\
        #!/usr/bin/env python3
        \"\"\"Agnes MCP stdio proxy — pure stdlib, no install needed.

        Reads credentials from agnes-bundle.json or ~/.config/agnes/token.json,
        then implements the MCP protocol over stdio, forwarding each tool call
        to the Agnes REST API.
        \"\"\"
        from __future__ import annotations
        import json, os, pathlib, re, ssl, sys, urllib.error, urllib.request

        HERE       = pathlib.Path(__file__).resolve().parent
        CONFIG_DIR = pathlib.Path.home() / ".config" / "agnes"

        # Verified HTTPS that also works on Pythons without a usable system CA
        # store (notably macOS python.org builds -> CERTIFICATE_VERIFY_FAILED).
        # The Mozilla CA bundle ships next to this script as cacert.pem (and
        # setup.py copies it into ~/.config/agnes/). Falls back to the OS trust
        # store and honours SSL_CERT_FILE. Verification stays ON; the only
        # opt-out is the explicit AGNES_INSECURE_SKIP_TLS_VERIFY=1 escape hatch.
        def _ssl_context():
            if os.environ.get("AGNES_INSECURE_SKIP_TLS_VERIFY") == "1":
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                return ctx
            for _ca in (os.environ.get("SSL_CERT_FILE"),
                        str(HERE / "cacert.pem"),
                        str(CONFIG_DIR / "cacert.pem")):
                if _ca and pathlib.Path(_ca).exists():
                    try:
                        return ssl.create_default_context(cafile=_ca)
                    except Exception:
                        pass
            return ssl.create_default_context()

        _SSL_CTX = _ssl_context()

        # ── credentials ──────────────────────────────────────────────────────

        def _load_creds():
            \"\"\"Return (server_url, pat) or ('', '') if not found.\"\"\"
            # Pre-setup: pre-baked PAT in the bundle JSON
            bf = HERE / "agnes-bundle.json"
            if bf.exists():
                try:
                    b = json.loads(bf.read_text())
                    u, p = b.get("server_url", "").rstrip("/"), b.get("access_token", "")
                    if u and p:
                        return u, p
                except Exception:
                    pass
            # Post-setup: persistent credentials file in the project folder.
            # setup.py writes this to the Mac filesystem so it survives
            # cowork VM restarts even when ~/.config/agnes/ is on the VM.
            cf = HERE / ".agnes-creds.json"
            if cf.exists():
                try:
                    b = json.loads(cf.read_text())
                    u = b.get("server_url", "").rstrip("/")
                    p = b.get("access_token", "")
                    if u and p:
                        return u, p
                except Exception:
                    pass
            cfg = CONFIG_DIR / "config.yaml"
            tok = CONFIG_DIR / "token.json"
            if cfg.exists() and tok.exists():
                try:
                    m = re.search(r"server:\\s*(.+)", cfg.read_text())
                    u = m.group(1).strip() if m else ""
                    p = json.loads(tok.read_text()).get("access_token", "")
                    if u and p:
                        return u, p
                except Exception:
                    pass
            return "", ""

        # ── HTTP helper ───────────────────────────────────────────────────────

        def _api(method, path, server_url, pat, body=None, timeout=30):
            url = server_url.rstrip("/") + path
            hdrs = {"Authorization": f"Bearer {pat}", "Content-Type": "application/json"}
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            try:
                with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                try:
                    return {"error": json.loads(e.read()).get("detail", str(e))}
                except Exception:
                    return {"error": str(e)}
            except Exception as e:
                return {"error": str(e)}

        # ── MCP tool definitions ──────────────────────────────────────────────

        _TOOLS = [
            {"name": "server_info",
             "description": "Return Agnes server health and your account email. Run at session start to verify connectivity.",
             "inputSchema": {"type": "object", "properties": {}}},
            {"name": "catalog",
             "description": "List all tables available to you (RBAC-filtered). Always call this first.",
             "inputSchema": {"type": "object", "properties": {}}},
            {"name": "schema",
             "description": "Show column names, types, and SQL hints for a table.",
             "inputSchema": {"type": "object", "required": ["table_id"],
                             "properties": {"table_id": {"type": "string"}}}},
            {"name": "describe",
             "description": "Show schema plus sample rows for a table.",
             "inputSchema": {"type": "object", "required": ["table_id"],
                             "properties": {"table_id": {"type": "string"},
                                            "rows": {"type": "integer", "default": 5}}}},
            {"name": "query",
             "description": "Run SQL against Agnes data (server-side, all query_mode tables).",
             "inputSchema": {"type": "object", "required": ["sql"],
                             "properties": {"sql": {"type": "string"},
                                            "limit": {"type": "integer", "default": 1000}}}},
            {"name": "skills",
             "description": "List marketplace skills you can access, with full SKILL.md content.",
             "inputSchema": {"type": "object", "properties": {}}},
        ]

        # ── tool dispatch ─────────────────────────────────────────────────────

        def _call(name, args, server_url, pat):
            if name == "server_info":
                health = _api("GET", "/api/health", server_url, pat, timeout=5)
                access = _api("GET", "/api/me/effective-access", server_url, pat, timeout=5)
                return json.dumps({"server": health, "access": access, "server_url": server_url}, indent=2)
            elif name == "catalog":
                return json.dumps(_api("GET", "/api/v2/catalog", server_url, pat), indent=2)
            elif name == "schema":
                tid = args.get("table_id", "")
                return json.dumps(_api("GET", f"/api/v2/schema/{tid}", server_url, pat), indent=2)
            elif name == "describe":
                tid  = args.get("table_id", "")
                rows = int(args.get("rows", 5))
                sc = _api("GET", f"/api/v2/schema/{tid}", server_url, pat)
                sm = _api("GET", f"/api/v2/sample/{tid}?n={rows}", server_url, pat)
                return json.dumps({"schema": sc, "sample": sm}, indent=2)
            elif name == "query":
                sql   = args.get("sql", "")
                limit = int(args.get("limit", 1000))
                return json.dumps(_api("POST", "/api/query", server_url, pat,
                                       body={"sql": sql, "limit": limit}, timeout=60), indent=2)
            elif name == "skills":
                return json.dumps(_api("GET", "/api/v2/marketplace/skills", server_url, pat), indent=2)
            return json.dumps({"error": f"unknown tool: {name}"})

        # ── MCP stdio loop ────────────────────────────────────────────────────

        def _send(obj):
            sys.stdout.write(json.dumps(obj) + "\\n")
            sys.stdout.flush()

        server_url, pat = _load_creds()

        for _raw in sys.stdin:
            _raw = _raw.strip()
            if not _raw:
                continue
            try:
                msg = json.loads(_raw)
            except ValueError:
                continue
            m   = msg.get("method", "")
            mid = msg.get("id")
            if m == "initialize":
                _send({"jsonrpc": "2.0", "id": mid, "result": {
                    "protocolVersion": msg.get("params", {}).get("protocolVersion", "2024-11-05"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "Agnes", "version": "1.0"},
                }})
            elif m == "initialized":
                pass
            elif m == "ping":
                _send({"jsonrpc": "2.0", "id": mid, "result": {}})
            elif m == "tools/list":
                _send({"jsonrpc": "2.0", "id": mid, "result": {"tools": _TOOLS}})
            elif m == "tools/call":
                p    = msg.get("params", {})
                name = p.get("name", "")
                args = p.get("arguments", {})
                if not server_url or not pat:
                    _send({"jsonrpc": "2.0", "id": mid,
                           "error": {"code": -32000,
                                     "message": "Agnes credentials not found — run setup.py"}})
                else:
                    text = _call(name, args, server_url, pat)
                    _send({"jsonrpc": "2.0", "id": mid,
                           "result": {"content": [{"type": "text", "text": text}]}})
            elif mid is not None:
                _send({"jsonrpc": "2.0", "id": mid,
                       "error": {"code": -32601, "message": f"Method not found: {m}"}})
    """)


def _bundle_agnes_py() -> str:
    """Return agnes.py — pure-stdlib CLI for Agnes data access via Bash tool.

    Reads credentials from .agnes-creds.json (project folder, written by
    setup.py) or ~/.config/agnes/. Provides catalog/schema/describe/query
    commands that Claude can call via the Bash tool in the cowork session.

    This is the reliable fallback for cowork environments where the cowork
    VM does not load mcpServers from the project-level settings.json.
    """
    return textwrap.dedent("""\
        #!/usr/bin/env python3
        \"\"\"Agnes CLI — pure stdlib, no install needed.

        Usage:
          python3 agnes.py catalog
          python3 agnes.py schema <table_id>
          python3 agnes.py describe <table_id> [rows]
          python3 agnes.py query '<sql>'
          python3 agnes.py info
          python3 agnes.py skills
        \"\"\"
        from __future__ import annotations
        import json, os, pathlib, re, ssl, sys, urllib.error, urllib.request

        HERE       = pathlib.Path(__file__).resolve().parent
        CONFIG_DIR = pathlib.Path.home() / ".config" / "agnes"

        # Verified HTTPS that also works on Pythons without a usable system CA
        # store (notably macOS python.org builds -> CERTIFICATE_VERIFY_FAILED).
        # The Mozilla CA bundle ships next to this script as cacert.pem (and
        # setup.py copies it into ~/.config/agnes/). Falls back to the OS trust
        # store and honours SSL_CERT_FILE. Verification stays ON; the only
        # opt-out is the explicit AGNES_INSECURE_SKIP_TLS_VERIFY=1 escape hatch.
        def _ssl_context():
            if os.environ.get("AGNES_INSECURE_SKIP_TLS_VERIFY") == "1":
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                return ctx
            for _ca in (os.environ.get("SSL_CERT_FILE"),
                        str(HERE / "cacert.pem"),
                        str(CONFIG_DIR / "cacert.pem")):
                if _ca and pathlib.Path(_ca).exists():
                    try:
                        return ssl.create_default_context(cafile=_ca)
                    except Exception:
                        pass
            return ssl.create_default_context()

        _SSL_CTX = _ssl_context()

        def _load_creds():
            \"\"\"Return (server_url, pat) or raise SystemExit.\"\"\"
            # Persistent creds file written by setup.py — survives VM restarts
            cf = HERE / ".agnes-creds.json"
            if cf.exists():
                try:
                    b = json.loads(cf.read_text())
                    u = b.get("server_url", "").rstrip("/")
                    p = b.get("access_token", "")
                    if u and p:
                        return u, p
                except Exception:
                    pass
            # Pre-setup: pre-baked PAT in bundle JSON (before setup.py runs)
            bf = HERE / "agnes-bundle.json"
            if bf.exists():
                try:
                    b = json.loads(bf.read_text())
                    u = b.get("server_url", "").rstrip("/")
                    p = b.get("access_token", "")
                    if u and p:
                        return u, p
                except Exception:
                    pass
            # Fallback: ~/.config/agnes/ (may not persist in cowork VM)
            cfg = CONFIG_DIR / "config.yaml"
            tok = CONFIG_DIR / "token.json"
            if cfg.exists() and tok.exists():
                try:
                    m = re.search(r"server:\\s*(.+)", cfg.read_text())
                    u = m.group(1).strip() if m else ""
                    p = json.loads(tok.read_text()).get("access_token", "")
                    if u and p:
                        return u, p
                except Exception:
                    pass
            print("ERROR: Agnes credentials not found. Run setup.py first.")
            sys.exit(2)

        def _api(method, path, server_url, pat, body=None, timeout=30):
            url = server_url.rstrip("/") + path
            hdrs = {"Authorization": f"Bearer {pat}", "Content-Type": "application/json"}
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            try:
                with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                try:
                    return {"error": json.loads(e.read()).get("detail", str(e))}
                except Exception:
                    return {"error": str(e)}
            except Exception as e:
                return {"error": str(e)}

        def main():
            args = sys.argv[1:]
            if not args or args[0] in ("-h", "--help"):
                print("Usage: python3 agnes.py <command> [args]")
                print("Commands:")
                print("  catalog                  List all accessible tables")
                print("  schema <table_id>        Show columns and types")
                print("  describe <table_id> [n]  Schema + sample rows (default 5)")
                print("  query '<sql>'            Run SQL (server-side)")
                print("  info                     Check server connectivity")
                print("  skills                   List marketplace skills")
                return

            server_url, pat = _load_creds()
            cmd = args[0]

            if cmd in ("info", "server_info"):
                health = _api("GET", "/api/health", server_url, pat, timeout=5)
                access = _api("GET", "/api/me/effective-access", server_url, pat, timeout=5)
                print(json.dumps({"server": health, "access": access, "server_url": server_url}, indent=2))

            elif cmd == "catalog":
                print(json.dumps(_api("GET", "/api/v2/catalog", server_url, pat), indent=2))

            elif cmd == "schema":
                if len(args) < 2:
                    print("Usage: python3 agnes.py schema <table_id>")
                    sys.exit(1)
                print(json.dumps(
                    _api("GET", f"/api/v2/schema/{args[1]}", server_url, pat), indent=2
                ))

            elif cmd == "describe":
                if len(args) < 2:
                    print("Usage: python3 agnes.py describe <table_id> [rows]")
                    sys.exit(1)
                tid  = args[1]
                rows = int(args[2]) if len(args) > 2 else 5
                sc = _api("GET", f"/api/v2/schema/{tid}", server_url, pat)
                sm = _api("GET", f"/api/v2/sample/{tid}?n={rows}", server_url, pat)
                print(json.dumps({"schema": sc, "sample": sm}, indent=2))

            elif cmd == "query":
                if len(args) < 2:
                    print("Usage: python3 agnes.py query '<sql>'")
                    sys.exit(1)
                sql = " ".join(args[1:])
                print(json.dumps(
                    _api("POST", "/api/query", server_url, pat,
                         body={"sql": sql, "limit": 1000}, timeout=60), indent=2
                ))

            elif cmd == "skills":
                print(json.dumps(
                    _api("GET", "/api/v2/marketplace/skills", server_url, pat), indent=2
                ))

            else:
                print(f"Unknown command: {cmd}")
                print("Run `python3 agnes.py --help` for usage.")
                sys.exit(1)

        main()
    """)


def _bundle_setup_py(server_url: str) -> str:
    """Return setup.py content — pure stdlib, no pip required.

    The primary path (steps 1-4) is entirely file I/O — no network call.
    The bundle now contains a pre-baked PAT (``access_token``), so setup
    works even inside Claude Desktop's sandboxed Bash tool which blocks
    outbound HTTP to external servers.

    Step 5 (fetch server-rendered CLAUDE.md) and step 6 (agnes pull) are
    best-effort network calls that are silently skipped when unreachable —
    the hook will retry them on the next session open as a system process.

    Fallback flags for Terminal use (when access_token is absent):
        python setup.py --server-url https://agnes.example.com --token <PAT>
    """
    return textwrap.dedent("""\
        #!/usr/bin/env python3
        \"\"\"Agnes Cowork one-time setup — no external packages needed.\"\"\"
        from __future__ import annotations
        import json, os, pathlib, platform, ssl, subprocess, sys, urllib.error, urllib.request

        HERE = pathlib.Path(__file__).parent
        BUNDLE_FILE = HERE / "agnes-bundle.json"

        # Verified HTTPS that also works on Pythons without a usable system CA
        # store (notably macOS python.org builds -> CERTIFICATE_VERIFY_FAILED).
        def _ssl_context():
            if os.environ.get("AGNES_INSECURE_SKIP_TLS_VERIFY") == "1":
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                return ctx
            _config_dir = pathlib.Path.home() / ".config" / "agnes"
            for _ca in (os.environ.get("SSL_CERT_FILE"),
                        str(HERE / "cacert.pem"),
                        str(_config_dir / "cacert.pem")):
                if _ca and pathlib.Path(_ca).exists():
                    try:
                        return ssl.create_default_context(cafile=_ca)
                    except Exception:
                        pass
            return ssl.create_default_context()

        _SSL_CTX = _ssl_context()

        # ── CLI overrides (used from Terminal as fallback) ────────────────────
        _args = sys.argv[1:]
        def _flag(name):
            try: return _args[_args.index(name) + 1]
            except (ValueError, IndexError): return None

        _override_server = _flag("--server-url")
        _override_token  = _flag("--token")

        # Write a plaintext secret file with mode 0o600 (owner-only). The
        # chmod happens on a temp file in the same dir BEFORE the atomic
        # rename, so a reader never observes a world-readable transient.
        # Best-effort + Windows-safe: where fchmod/chmod isn't honored the
        # contents still land and native ACLs apply.
        def _write_secret(path, text):
            import tempfile
            _dir = path.parent
            _dir.mkdir(parents=True, exist_ok=True)
            _fd, _tmp = tempfile.mkstemp(prefix=".agnes-sec.", dir=str(_dir))
            _tmp_path = pathlib.Path(_tmp)
            try:
                try:
                    os.write(_fd, text.encode("utf-8"))
                    try:
                        os.fchmod(_fd, 0o600)
                    except (AttributeError, NotImplementedError, OSError):
                        pass
                finally:
                    os.close(_fd)
                os.replace(_tmp_path, path)
            except Exception:
                try:
                    _tmp_path.unlink()
                except Exception:
                    pass
                raise

        if not BUNDLE_FILE.exists():
            print("ERROR: agnes-bundle.json not found. Download a fresh bundle.")
            sys.exit(1)

        bundle = json.loads(BUNDLE_FILE.read_text())
        server_url = (_override_server or bundle["server_url"]).rstrip("/")

        # ── 1. Resolve PAT ────────────────────────────────────────────────────
        # Preferred path: exchange setup_token → 90-day PAT (long-lived).
        # Fallback: pre-baked 24h access_token (works offline / in sandbox).
        # The exchange is single-use; if it was already consumed on a prior
        # run, the fallback token takes over for that session.
        setup_token  = bundle.get("setup_token", "")
        pre_baked    = bundle.get("access_token", "")
        user_email   = bundle.get("user_email", "")

        pat = _override_token or ""

        if not pat and setup_token:
            try:
                req = urllib.request.Request(
                    f"{server_url}/api/auth/exchange-setup-token",
                    data=json.dumps({"setup_token": setup_token}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as r:
                    resp = json.loads(r.read())
                pat = resp.get("access_token", "")
                user_email = resp.get("user_email", user_email)
                if pat:
                    print("Agnes connected (90-day token).")
            except Exception:
                pass  # fall through to pre-baked token below

        if not pat:
            pat = pre_baked  # 24h fallback — works offline/sandboxed

        if not pat:
            print("ERROR: No token available. Download a fresh bundle.")
            sys.exit(2)

        print(f"Setting up Agnes Cowork for {user_email or server_url} ...")

        # 2. Save credentials — pure file I/O, no network needed.
        #    The Agnes CLI reads server URL from config.yaml (YAML, not JSON)
        #    and the token from token.json.  We write both formats for
        #    compatibility but config.yaml is authoritative for the CLI.
        config_dir = pathlib.Path.home() / ".config" / "agnes"
        config_dir.mkdir(parents=True, exist_ok=True)
        # config.yaml — read by the CLI (pyyaml); simple single-key YAML
        # written without the yaml library (stdlib-only constraint).
        (config_dir / "config.yaml").write_text(f"server: {server_url}\\n")
        # config.json — read by setup.py itself and some older tooling
        (config_dir / "config.json").write_text(
            json.dumps({"server": server_url}, indent=2)
        )
        # token.json holds a plaintext bearer token → 0o600 (owner-only).
        _write_secret(
            config_dir / "token.json",
            json.dumps({"access_token": pat}, indent=2),
        )
        # Also write to the project folder — this file is on the Mac filesystem,
        # so it persists across cowork VM restarts (unlike ~/.config/agnes/ which
        # lives in the VM's home directory and may be ephemeral). Same plaintext
        # token → 0o600.
        _write_secret(
            HERE / ".agnes-creds.json",
            json.dumps({"server_url": server_url, "access_token": pat}, indent=2),
        )
        print("Credentials saved.")

        # 3. Replace the one-time setup hook with a permanent pull hook.
        #    Keep stdio MCP (mcp_server.py) but switch to absolute path and
        #    ensure credentials are in place so it works after bundle cleanup.
        settings_path = HERE / ".claude" / "settings.json"
        if settings_path.exists():
            cfg = json.loads(settings_path.read_text())
            cfg.setdefault("hooks", {})
            cfg["hooks"]["SessionStart"] = [
                {"hooks": [{"type": "command", "command":
                    "agnes pull --quiet 2>/dev/null || true"
                }]},
            ]
            cfg["hooks"].pop("SessionEnd", None)
            cfg["mcpServers"] = {
                "agnes": {
                    "command": sys.executable,
                    "args": [str(HERE / "mcp_server.py")],
                }
            }
            settings_path.write_text(json.dumps(cfg, indent=2) + "\\n")
            print("Session hook installed (agnes pull on start).")

        # 3b. Register Agnes MCP (SSE) in Claude Desktop's global config.
        #
        # When running inside Claude Desktop's cowork VM, $HOME points to the
        # VM's ephemeral sandbox home — NOT the real Mac home. We derive the
        # Mac home from the project folder path (the project folder IS on the
        # Mac filesystem, so its path leaks the real username and home dir).
        #
        # Example: HERE = /Users/alice/Downloads/agnes-cowork-setup-…/
        #   → Mac home = /Users/alice
        #   → claude_desktop_config.json = /Users/alice/Library/Application
        #                                  Support/Claude/claude_desktop_config.json
        #
        # Writes are best-effort; failure falls through silently.
        # Returns list of (home_path, claude_desktop_config_path) tuples.
        def _claude_cfg_candidates():
            candidates = []
            _parts = list(HERE.parts)
            if len(_parts) >= 3 and _parts[1] == "Users":
                _mac_home = pathlib.Path("/") / _parts[1] / _parts[2]
                candidates.append((
                    _mac_home,
                    _mac_home / "Library" / "Application Support"
                    / "Claude" / "claude_desktop_config.json",
                ))
            if platform.system() == "Darwin":
                candidates.append((
                    pathlib.Path.home(),
                    pathlib.Path.home() / "Library" / "Application Support"
                    / "Claude" / "claude_desktop_config.json",
                ))
            elif platform.system() == "Windows":
                _appdata = os.environ.get("APPDATA", "")
                _userprofile = os.environ.get("USERPROFILE", "")
                if _appdata and _userprofile:
                    candidates.append((
                        pathlib.Path(_userprofile),
                        pathlib.Path(_appdata) / "Claude" / "claude_desktop_config.json",
                    ))
            elif platform.system() == "Linux":
                candidates.append((
                    pathlib.Path.home(),
                    pathlib.Path.home() / ".config" / "Claude" / "claude_desktop_config.json",
                ))
            seen = set()
            return [(h, c) for h, c in candidates
                    if not (str(c) in seen or seen.add(str(c)))]

        _registered = False
        _reg_errors = []
        for _home, _cfg_path in _claude_cfg_candidates():
            try:
                import shutil as _shutil
                # Copy mcp_server.py to a stable location outside the bundle
                # folder so it survives bundle deletion. Files extracted from
                # a downloaded ZIP carry com.apple.quarantine on macOS, which
                # blocks python3 from reading them — the copy + xattr -c strip
                # fixes that.
                _stable_dir = _home / ".config" / "agnes"
                _stable_mcp = _stable_dir / "mcp_server.py"
                _stable_dir.mkdir(parents=True, exist_ok=True)
                _shutil.copy2(str(HERE / "mcp_server.py"), str(_stable_mcp))
                _stable_mcp.chmod(0o644)
                if platform.system() == "Darwin":
                    import subprocess as _sp
                    _sp.run(["xattr", "-c", str(_stable_mcp)], capture_output=True)
                # Ship the CA bundle next to mcp_server.py so verified TLS works
                # on machines whose Python lacks a usable system CA store.
                _bundled_ca = HERE / "cacert.pem"
                if _bundled_ca.exists():
                    _shutil.copy2(str(_bundled_ca), str(_stable_dir / "cacert.pem"))
                    (_stable_dir / "cacert.pem").chmod(0o644)
                # Write credentials alongside the stable copy so mcp_server.py
                # can find them even after the bundle folder is deleted.
                # Plaintext token → 0o600.
                _write_secret(
                    _stable_dir / ".agnes-creds.json",
                    json.dumps({"server_url": server_url, "access_token": pat},
                               indent=2),
                )
                # claude_desktop_config.json supports stdio transport only —
                # "type": "sse" with headers is for claude.ai web, not Desktop.
                _desktop_cfg = {}
                if _cfg_path.exists():
                    try:
                        _desktop_cfg = json.loads(_cfg_path.read_text())
                    except Exception:
                        _desktop_cfg = {}
                _desktop_cfg.setdefault("mcpServers", {})
                _desktop_cfg["mcpServers"]["agnes"] = {
                    "command": "python3",
                    "args": [str(_stable_mcp)],
                }
                _cfg_path.parent.mkdir(parents=True, exist_ok=True)
                _cfg_path.write_text(json.dumps(_desktop_cfg, indent=2))
                print(f"Agnes MCP registered: {_cfg_path}")
                _registered = True
                break
            except Exception as _e:
                _reg_errors.append(f"  {_cfg_path}: {_e}")
                continue
        if not _registered and _reg_errors:
            print("WARNING: Agnes MCP registration failed for all candidates:")
            for _err in _reg_errors:
                print(_err)
            print("MCP tools won't load automatically — restart Claude Desktop manually after fixing the path.")
        if _registered:
            print("Restart Claude Desktop once to activate Agnes MCP tools.")
            if platform.system() == "Darwin":
                try:
                    _dlg = subprocess.run(
                        ["osascript", "-e",
                         "button returned of (display dialog "
                         "\\"Agnes MCP tools registered. "
                         "Restart Claude Desktop now to activate them?\\" "
                         "buttons {\\"Later\\", \\"Restart Now\\"} "
                         "default button \\"Restart Now\\" "
                         "with title \\"Agnes Cowork Setup\\")"],
                        capture_output=True, text=True, timeout=60,
                    )
                    if _dlg.stdout.strip() == "Restart Now":
                        subprocess.run(
                            ["osascript", "-e",
                             "tell application \\"Claude\\" to quit"],
                            capture_output=True,
                        )
                        import time as _t; _t.sleep(2)
                        subprocess.run(["open", "-a", "Claude"],
                                       capture_output=True)
                except Exception:
                    pass

        # 3c. Write MCP config to user-level ~/.claude/settings.json so the
        #     cowork VM's claude-code binary picks it up on the next session
        #     open — without requiring a full Claude Desktop restart.
        #     Project-level .claude/settings.json is also updated (step 3) but
        #     the cowork VM may not load project-level mcpServers; user-level
        #     settings are loaded regardless of which project is open.
        #     Use the stable ~/.config/agnes/mcp_server.py path when the MCP
        #     registration succeeded — outlasts any bundle folder deletion.
        _user_claude_dir = pathlib.Path.home() / ".claude"
        _user_claude_dir.mkdir(parents=True, exist_ok=True)
        _user_settings_path = _user_claude_dir / "settings.json"
        try:
            _user_cfg = {}
            if _user_settings_path.exists():
                try:
                    _user_cfg = json.loads(_user_settings_path.read_text())
                except Exception:
                    _user_cfg = {}
            _user_cfg.setdefault("mcpServers", {})
            _mcp_path = (
                str(_stable_mcp) if _registered
                else str(HERE / "mcp_server.py")
            )
            _user_cfg["mcpServers"]["agnes"] = {
                "command": sys.executable,
                "args": [_mcp_path],
            }
            _user_settings_path.write_text(json.dumps(_user_cfg, indent=2) + "\\n")
            print("Agnes MCP registered in ~/.claude/settings.json (user-level).")
        except Exception:
            pass  # best-effort; project-level settings.json is the fallback

        # 4. Delete bundle file — credentials are now in ~/.config/agnes/
        try:
            BUNDLE_FILE.unlink()
        except Exception:
            pass  # best-effort; file may already be gone

        # 5. Best-effort: fetch server-rendered CLAUDE.md (needs network).
        #    Skipped silently if unreachable — the existing CLAUDE.md stays.
        try:
            req2 = urllib.request.Request(
                f"{server_url}/api/welcome?server_url={server_url}",
                headers={"Authorization": f"Bearer {pat}"},
            )
            with urllib.request.urlopen(req2, timeout=10, context=_SSL_CTX) as r:
                welcome = json.loads(r.read())
            content = welcome.get("content", "")
            if content:
                (HERE / "CLAUDE.md").write_text(content, encoding="utf-8")
                print("CLAUDE.md updated with your Agnes context.")
        except Exception:
            pass  # best-effort; hooks handle the full pull on next session

        # 6. Best-effort: run agnes pull to pre-cache data for offline queries.
        try:
            import shutil as _shutil
            agnes_bin = _shutil.which("agnes") or ""
            if agnes_bin:
                result = subprocess.run(
                    [agnes_bin, "pull", "--quiet"],
                    capture_output=True, text=True, timeout=120,
                )
                if result.returncode == 0:
                    print("Initial data sync complete.")
        except Exception:
            pass  # best-effort; MCP pull tool handles it on demand

        print()
        print("Agnes Cowork ready!")
        print(f"  Server : {server_url}")
        print(f"  Account: {user_email}")
        print(f"  MCP    : starts automatically when you open this project")
        print()
        print("Ask: \\"What data do I have access to?\\"")
    """)


def _bundle_claude_md(server_url: str, user_email: str, expires_at: datetime) -> str:
    """Return CLAUDE.md content for the setup bundle.

    Primary path: Bash tool with `python3 agnes.py` — works immediately,
    no restart needed, works inside the cowork VM.
    Bonus path: MCP tools — may become available after Claude Desktop restart
    once setup.py has registered them in claude_desktop_config.json.
    """
    exp_str = expires_at.strftime("%Y-%m-%d %H:%M UTC")
    tokens_url = server_url.rstrip("/") + "/tokens"
    return textwrap.dedent(f"""\
        # Agnes Cowork workspace

        Agnes server: {server_url}  |  Account: {user_email}

        ## How Agnes works in Cowork

        Agnes is registered as an MCP server in Claude Desktop. Tools (`catalog`, `schema`,
        `query`, etc.) appear directly in your tool list — no Bash needed.

        **If tools are not yet in your list:** setup.py registered them at session start.
        Restart Claude Desktop once to load them. After that they persist in every session.

        ## At session start — do this immediately

        When this workspace opens, run this command via the Bash tool and show the results:

        ```
        python3 agnes.py catalog
        ```

        Then pick the most interesting table and run:

        ```
        python3 agnes.py describe <table_id>
        ```

        Show the user what data is available and suggest a concrete question to explore.
        Do NOT wait for the user to ask — do it proactively at the start of every session.

        ## Agnes tools

        Always use `python3 agnes.py <command>` via the Bash tool:

        | Command | What it does |
        |---------|-------------|
        | `python3 agnes.py catalog` | List all tables — **run this first** |
        | `python3 agnes.py schema <id>` | Column names and types |
        | `python3 agnes.py describe <id>` | Schema + sample rows |
        | `python3 agnes.py query '<sql>'` | Run SQL server-side |
        | `python3 agnes.py info` | Check connectivity |

        If Agnes MCP tools (`catalog`, `schema`, `query`) appear in your tool list,
        use those directly instead of the Bash commands above — they are faster.

        ## Bundle info

        Expires {exp_str}. Fresh bundle: {tokens_url}
    """)


def _bundle_skill_setup_cowork() -> str:
    """Return .claude/skills/setup-cowork/SKILL.md content for the bundle.

    Invoked by the user as /setup-cowork inside the cowork workspace.
    Guides Claude through: verify connectivity → show available tables →
    list marketplace skills → run a first query.
    """
    return textwrap.dedent("""\
        ---
        name: setup-cowork
        description: Guided Agnes Cowork setup — verify connection, explore your data, try a skill
        ---

        Run this flow when /setup-cowork is invoked.

        ## Step 1 — Check if Agnes MCP tools are available

        Look in your tool list for tools named `catalog`, `schema`, `describe`, `query`.

        **If Agnes MCP tools ARE in your tool list:**
        - Call `catalog()` to list tables
        - Call `describe(<best_table>)` on the most interesting one
        - Suggest one concrete question the user could ask
        - Tell the user Agnes is ready

        **If Agnes MCP tools are NOT in your tool list:**
        - Tell the user: "Agnes tools registered — restart Claude Desktop to activate them."
        - Explain: setup.py already wrote Agnes into Claude Desktop's config at session start.
          One restart loads the tools into every future session.
        - Do NOT tell the user to run terminal commands, install packages, or re-download the bundle.
        - Do NOT say Agnes is broken or unavailable — it just needs one restart.

        ## After restart — what the user gets

        Once Claude Desktop is restarted, Agnes tools (`catalog`, `schema`, `query`, etc.)
        are available in every session, including Cowork. No further setup needed.
    """)


def _bundle_cacert_pem() -> str:
    """Return the Mozilla CA bundle (certifi) shipped inside the Cowork ZIP.

    The generated mcp_server.py / agnes.py verify TLS against this file, so
    Cowork connects from any end-user machine — including macOS python.org
    builds that lack a usable system CA store (CERTIFICATE_VERIFY_FAILED) —
    without ever disabling certificate verification.
    """
    import certifi

    with open(certifi.where(), encoding="utf-8") as fh:
        return fh.read()


def _skill_explore_data() -> str:
    return textwrap.dedent("""\
        ---
        name: explore-data
        description: Explore Agnes data — list tables, examine the most interesting ones, suggest questions
        ---

        Run this flow when /explore-data is invoked.

        1. Call `catalog()` (or `python3 agnes.py catalog`) to list all available tables.
        2. Pick the 2–3 most interesting tables based on names and descriptions.
        3. Call `describe(<table_id>)` on each to see real sample values and column types.
        4. Based on what you see, suggest 3 concrete, specific questions the user could explore.
        5. Ask which question they want to pursue first, then run it.

        Be proactive — don't wait for the user to tell you what to look at. Lead with a finding.
    """)


def _skill_query_data() -> str:
    return textwrap.dedent("""\
        ---
        name: query-data
        description: Write and run SQL queries against Agnes data — guided workflow with schema lookup
        ---

        Run this flow when /query-data is invoked.

        1. Ask the user what they want to find out (one sentence is enough).
        2. Call `catalog()` to identify which tables are relevant.
        3. Call `schema(<table_id>)` for each relevant table — know the exact column names before writing SQL.
        4. Draft the SQL query. Show it to the user before running.
        5. Call `query(<sql>)` and present results clearly — table, key numbers, plain-language summary.
        6. Offer a follow-up: refine filter, add aggregation, compare periods, or export to CSV.

        SQL dialect notes:
        - Local / materialized tables → DuckDB SQL (`DATE '2026-01-01'`, `strftime`, window functions).
        - Remote tables (BigQuery) → BQ SQL (`DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)`, `REGEXP_CONTAINS`).
        - Check `query_mode` in catalog output to know which dialect to use.
    """)


def _skill_new_skill() -> str:
    return textwrap.dedent("""\
        ---
        name: new-skill
        description: Design and create a new skill for your Agnes Cowork workspace
        ---

        Run this flow when /new-skill is invoked.

        1. Ask the user: what should this skill do? What will they type to invoke it?
        2. Ask: what Agnes data or tools does it need? (tables, catalog, query, schema?)
        3. Draft a SKILL.md:
           - frontmatter: `name`, `description` (one line, no angle brackets or double quotes)
           - body: clear step-by-step instructions Claude follows when the skill is invoked
           - Keep the body focused — one workflow, not a swiss-army knife
        4. Show the draft. Refine until the user is happy.
        5. Write the file using the Bash tool. Claude Code loads a skill as a
           DIRECTORY, so the entrypoint must be `.claude/skills/<name>/SKILL.md`
           (a flat `<name>.md` is never picked up):
           ```
           python3 -c "
           import pathlib
           p = pathlib.Path('.claude/skills/<name>/SKILL.md')
           p.parent.mkdir(parents=True, exist_ok=True)
           p.write_text('''<content>''', encoding='utf-8')
           print('Created', p)
           "
           ```
        6. Tell the user: the new /<name> command is available immediately in this session.
           In Cowork, open a new session or re-upload the bundle to pick it up.

        To share the skill with the whole team, send the .md file to your Agnes admin
        for review and inclusion in the marketplace.
    """)


def _bundle_agent_skill_router() -> str:
    """Return .claude/agents/skill-router.md for the bundle.

    A lightweight subagent that inventories the workspace skills, picks the
    ones that fit the user's request, and activates them. Bundled because
    Cowork's Customize panel does not list workspace skills (a known Claude
    Code UI bug), so users often cannot see what is available to choose from.
    """
    return textwrap.dedent("""\
        ---
        name: skill-router
        description: Selects and activates the Agnes Cowork skills relevant to the current task. Use at the start of a task when it is unclear which skill applies, or when the user asks which skill to use.
        tools: Read, Glob, Bash, Skill
        ---

        You route a request to the right Agnes skill(s) and activate them. In
        Cowork the Customize - Skills panel does not list workspace skills (a
        known Claude Code UI bug), so users often cannot see what is available.
        Discover the skills yourself, pick the ones that fit, and run them.

        ## Steps

        1. Enumerate available skills. They live in `.claude/skills/`:
           - directory form: `.claude/skills/<name>/SKILL.md`
           - flat form:      `.claude/skills/<name>.md`
           Glob `.claude/skills/**/*.md`, then Read each file's frontmatter
           `name` + `description`. Build a short inventory.

        2. Match against the user's request. Compare the task to each skill's
           `description` and choose the smallest set that covers it — usually one
           skill, occasionally two complementary ones. The description must
           actually fit the goal; do not match on a keyword alone.

        3. Activate the chosen skill(s) with the Skill tool, in the order they
           should run. Pass along any argument the user supplied (e.g. a table
           name).

        4. If nothing fits, say so plainly and list the available skills with
           their one-line descriptions so the user can choose. Never invent a
           skill that is not in the inventory.

        ## Output

        State which skills you activated and why (one line each), then hand back
        so the activated skill's workflow can run. You are a router, not the
        destination — keep it short.
    """)


def _build_bundle_zip(
    server_url: str,
    setup_token: str,
    access_token: str,
    user_email: str,
    expires_at: datetime,
    folder_name: str,
    *,
    marketplace_skills: list[tuple[str, bytes]] | None = None,
    marketplace_agents: list[tuple[str, bytes]] | None = None,
) -> bytes:
    """Return a ZIP archive as bytes.

    Unzipping the archive creates a single top-level directory ``folder_name/``
    containing the workspace files. The user opens that directory in Claude Code
    and the ``SessionStart`` hook handles the rest.

    Structure::

        <folder_name>/
        ├── agnes-bundle.json         ← pre-baked PAT + setup token + server URL
        ├── setup.py                  ← one-time setup (writes .agnes-creds.json)
        ├── agnes.py                  ← pure-stdlib CLI; Claude calls via Bash tool
        ├── mcp_server.py             ← stdio MCP proxy (if cowork VM loads it)
        ├── cacert.pem                ← Mozilla CA bundle (verified TLS on macOS)
        ├── .claude/
        │   ├── settings.json         ← SessionStart hook + mcpServers config
        │   ├── skills/               ← directory-per-skill (<name>/SKILL.md)
        │   │   ├── setup-cowork/SKILL.md  ← /setup-cowork — guided onboarding
        │   │   ├── explore-data/SKILL.md  ← /explore-data — catalog + describe + suggest
        │   │   ├── query-data/SKILL.md    ← /query-data — schema-aware SQL workflow
        │   │   ├── new-skill/SKILL.md     ← /new-skill — design + write a new skill
        │   │   └── <marketplace skill>/SKILL.md (+ supporting files), per RBAC grants
        │   └── agents/
        │       ├── skill-router.md   ← selects + activates the right skill(s)
        │       └── <marketplace agents per user's RBAC grants>
        └── CLAUDE.md                 ← user + agent guidance (Bash-first)

    The ``access_token`` field in ``agnes-bundle.json`` is a short-lived PAT
    (same 24 h TTL as the bundle) so ``setup.py`` can save credentials without
    any outbound HTTP — works inside Claude Desktop's sandboxed Bash tool.
    ``setup_token`` is kept for the ``agnes init --bundle`` CLI path which runs
    outside the sandbox and does the full server-side exchange.
    """
    bundle_json = json.dumps(
        {
            "version": 1,
            "server_url": server_url,
            "setup_token": setup_token,
            "access_token": access_token,
            "user_email": user_email,
            "expires_at": expires_at.isoformat(),
        },
        indent=2,
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{folder_name}/agnes-bundle.json", bundle_json)
        zf.writestr(f"{folder_name}/setup.py", _bundle_setup_py(server_url))
        zf.writestr(f"{folder_name}/mcp_server.py", _bundle_mcp_server_py())
        zf.writestr(f"{folder_name}/agnes.py", _bundle_agnes_py())
        zf.writestr(f"{folder_name}/cacert.pem", _bundle_cacert_pem())
        zf.writestr(f"{folder_name}/.claude/settings.json", _bundle_settings_json(server_url, access_token))
        # Agnes curated skills — always present regardless of marketplace config.
        # Curated skills ship in directory format so Claude Code loads them:
        # the entrypoint must be `.claude/skills/<name>/SKILL.md`.
        zf.writestr(f"{folder_name}/.claude/skills/setup-cowork/SKILL.md", _bundle_skill_setup_cowork())
        zf.writestr(f"{folder_name}/.claude/skills/explore-data/SKILL.md", _skill_explore_data())
        zf.writestr(f"{folder_name}/.claude/skills/query-data/SKILL.md", _skill_query_data())
        zf.writestr(f"{folder_name}/.claude/skills/new-skill/SKILL.md", _skill_new_skill())
        # Agnes curated agents — the skill-router picks + activates the right
        # skill(s) for a task (Cowork's Customize panel can't list them).
        zf.writestr(f"{folder_name}/.claude/agents/skill-router.md", _bundle_agent_skill_router())
        # Marketplace skills + agents from the user's RBAC-granted plugins.
        for arcname, content in marketplace_skills or []:
            zf.writestr(f"{folder_name}/{arcname}", content)
        for arcname, content in marketplace_agents or []:
            zf.writestr(f"{folder_name}/{arcname}", content)
        zf.writestr(
            f"{folder_name}/CLAUDE.md",
            _bundle_claude_md(server_url, user_email, expires_at),
        )
    return buf.getvalue()


# ── request / response models ─────────────────────────────────────────────────


class ExchangeRequest(BaseModel):
    setup_token: str


class ExchangeResponse(BaseModel):
    access_token: str
    server_url: str
    user_email: str


class SetupTokenItem(BaseModel):
    id: str
    created_at: str
    expires_at: str


# ── endpoints ─────────────────────────────────────────────────────────────────
#
# All handlers below are plain ``def`` (not ``async def``): none contains an
# ``await`` — their bodies are blocking DB reads plus, for generate_bundle,
# reading every granted marketplace skill/agent off disk and building a ZIP
# in memory. Plain ``def`` makes FastAPI offload them to the anyio thread
# pool so a slow bundle build can't freeze the single uvicorn event loop
# (PR #188 convention; pinned by tests/test_event_loop_offload_guard.py).


@user_router.post(
    "/cowork-bundle",
    status_code=200,
)
def generate_bundle(
    request: Request,
    user: dict = Depends(require_session_token),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Generate a Cowork Setup Bundle ZIP for the calling user.

    Returns a ``application/zip`` file containing a setup script, a
    ``README.txt``, and a ```.agnes-bundle.json`` with a short-lived setup
    token embedded. The analyst unzips and runs ``./setup.sh``, which calls
    ``agnes init --bundle .`` to exchange the setup token for a PAT and
    bootstrap their workspace in one step.

    Rate-limited: max 5 active (unexpired, unused) setup tokens per user.

    Session-only (``require_session_token``, #1292): this route mints two
    durable follow-on credentials — a pre-baked PAT inline in the ZIP, and a
    setup token that ``POST /api/auth/exchange-setup-token`` (unauthenticated
    by design — the setup token IS the credential) trades for a fresh 90-day
    PAT. Gating it on ``get_current_user`` alone let a caller holding only a
    PAT mint a new 90-day one that outlives revoking the original — the same
    class of hole ``require_session_token`` already closes for ``POST
    /auth/tokens`` and agent-PAT issuance. ``require_session_token`` also
    rejects the ``X-StorageApi-Token`` header credential (PR #1288's narrower
    fix, now subsumed), the scheduler shared secret, and an agent PAT.
    """
    repo = setup_tokens_repo()

    active_count = repo.count_active_for_user(user["id"])
    if active_count >= _MAX_ACTIVE_TOKENS:
        raise HTTPException(
            status_code=400,
            detail={
                "kind": "too_many_setup_tokens",
                "hint": (
                    f"You have {active_count} active setup tokens. Revoke unused ones before generating a new bundle."
                ),
                "active_count": active_count,
            },
        )

    raw_token = _generate_setup_token()
    token_hash = _hash_token(raw_token)
    token_id = str(uuid.uuid4())
    expires_at = datetime.now(timezone.utc) + _SETUP_TOKEN_TTL
    server_url = (os.environ.get("AGNES_BASE_URL") or str(request.base_url)).rstrip("/")

    repo.create(
        id=token_id,
        user_id=user["id"],
        token_hash=token_hash,
        expires_at=expires_at,
    )

    # Pre-bake a short-lived PAT (same TTL as the bundle) so setup.py can
    # save credentials without any outbound HTTP — works inside Claude
    # Desktop's sandboxed Bash tool which blocks external network calls.
    import hashlib as _hl

    pat_id = str(uuid.uuid4())
    pat_jwt = create_access_token(
        user_id=user["id"],
        email=user.get("email", ""),
        token_id=pat_id,
        typ="pat",
        expires_delta=_SETUP_TOKEN_TTL,
        extra_claims={"scope": "cowork-bundle"},
    )
    access_token_repo().create(
        id=pat_id,
        user_id=user["id"],
        name="Agnes Cowork Setup (auto-generated)",
        token_hash=_hl.sha256(pat_jwt.encode()).hexdigest(),
        prefix=pat_id.replace("-", "")[:8],
        expires_at=expires_at,
        # v106: Cowork onboarding is an analyst surface — stack default.
        surface="stack",
    )

    _audit(conn, user["id"], "cowork_bundle.create", token_id, {"server_url": server_url})

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder_name = f"agnes-cowork-setup-{ts}"
    filename = f"{folder_name}.zip"

    mkt_skills, mkt_agents = _collect_marketplace_content(conn, user)
    zip_bytes = _build_bundle_zip(
        server_url=server_url,
        setup_token=raw_token,
        access_token=pat_jwt,
        user_email=user.get("email", ""),
        expires_at=expires_at,
        folder_name=folder_name,
        marketplace_skills=mkt_skills,
        marketplace_agents=mkt_agents,
    )

    return StreamingResponse(
        io.BytesIO(zip_bytes),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@user_router.get("/setup-tokens", response_model=List[SetupTokenItem])
def list_setup_tokens(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List the calling user's active (unexpired, unused) setup tokens."""
    rows = setup_tokens_repo().list_active_for_user(user["id"])
    return [
        SetupTokenItem(
            id=r["id"],
            created_at=str(r["created_at"]),
            expires_at=str(r["expires_at"]),
        )
        for r in rows
    ]


@user_router.delete("/setup-tokens/{token_id}", status_code=204)
def revoke_setup_token(
    token_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Revoke (delete) a setup token. Only the owner may revoke their own tokens."""
    repo = setup_tokens_repo()
    rows = repo.list_active_for_user(user["id"])
    owned = {r["id"] for r in rows}
    if token_id not in owned:
        raise HTTPException(status_code=404, detail="Setup token not found")
    repo.delete(token_id)
    _audit(conn, user["id"], "cowork_bundle.revoke", token_id)


@auth_router.post("/exchange-setup-token", response_model=ExchangeResponse)
def exchange_setup_token(
    payload: ExchangeRequest,
    request: Request,
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Exchange a one-time setup token for a regular PAT.

    This endpoint requires **no prior authentication** — the setup token
    embedded in the bundle IS the authentication credential.

    Security properties:
    - Token is matched by SHA-256 hash, never stored or logged in plaintext.
    - Single-use: atomically marked used on first consumption.
    - Short-lived: 24 h TTL enforced server-side regardless of client clock.
    - On success, the setup token is consumed and cannot be replayed.
    """
    if not payload.setup_token.startswith("st_"):
        raise HTTPException(status_code=400, detail="Invalid token format")

    token_hash = _hash_token(payload.setup_token)
    repo = setup_tokens_repo()
    row = repo.get_by_hash(token_hash)

    now = datetime.now(timezone.utc)

    if not row:
        raise HTTPException(status_code=401, detail="Invalid or expired setup token")

    if row["expires_at"] and row["expires_at"].replace(tzinfo=timezone.utc) < now:
        raise HTTPException(status_code=401, detail="Setup token has expired")

    if row["used_at"] is not None:
        raise HTTPException(status_code=401, detail="Setup token has already been used")

    # Atomically claim the token (prevents concurrent replay)
    claimed = repo.mark_used(row["id"])
    if not claimed:
        raise HTTPException(status_code=401, detail="Setup token has already been used")

    # Fetch the user the token belongs to
    user_row = users_repo().get_by_id(row["user_id"])
    if not user_row or user_row.get("deleted_at"):
        raise HTTPException(status_code=401, detail="Account not found")

    # Mint a PAT for the user (90-day default, scope "cowork")
    token_id = str(uuid.uuid4())
    expires_delta = timedelta(days=90)
    jwt_token = create_access_token(
        user_id=user_row["id"],
        email=user_row["email"],
        token_id=token_id,
        typ="pat",
        expires_delta=expires_delta,
        extra_claims={"scope": "cowork"},
    )
    import hashlib as _hl

    pat_hash = _hl.sha256(jwt_token.encode()).hexdigest()
    prefix = token_id.replace("-", "")[:8]
    expires_at = now + expires_delta
    access_token_repo().create(
        id=token_id,
        user_id=user_row["id"],
        name="Agnes Cowork (auto-generated)",
        token_hash=pat_hash,
        prefix=prefix,
        expires_at=expires_at,
        # v106: Cowork onboarding is an analyst surface — stack default.
        surface="stack",
    )

    _audit(conn, user_row["id"], "cowork_bundle.exchange", row["id"], {"pat_id": token_id})

    server_url = (os.environ.get("AGNES_BASE_URL") or str(request.base_url)).rstrip("/")
    return ExchangeResponse(
        access_token=jwt_token,
        server_url=server_url,
        user_email=user_row.get("email", ""),
    )
