#!/usr/bin/env python3
"""Persona x resource leak sweep against a live instance.

Why this exists
---------------
Grant configuration is reviewable in one place (``/admin/access``, and the
``effective-access`` diagnostic behind it), and a review of that graph can
come back clean while a persona still reads something they should not — the
graph describes intent, the read surfaces enforce it, and the two are
different code paths. Every incident of this shape has looked the same in
hindsight: someone widened a grant (or a default) for one surface, and
nobody re-checked what the other surfaces then answered.

So this sweep does not read the grant graph. It presents each persona's own
credential to the real read surfaces and records what actually comes back:

* ``GET  /api/v2/catalog``          which tables the persona is offered
* ``POST /api/query``               whether a table NOT expected is refused
                                    (the enforcement probe — a table absent
                                    from the catalog but queryable is the
                                    exact bug a catalog-only check misses)
* ``GET  /api/collections``         which collections are listed
* ``GET  /api/collections/search``  whether canary text surfaces in content
* ``GET  /api/knowledge/search``    the same question on the other index
* ``GET  /api/v1/agents``           for an agent-PAT persona

Each persona declares what it SHOULD reach; anything else that answers is a
finding. Run it after every grant change — that is the point, not a one-off
audit.

It also asks the server, with each persona's OWN credential
(``GET /api/me/effective-access``), what it claims that persona can read,
and diffs the claim against what the persona actually got. A disagreement
is its own finding: the admin view of that same payload is what an operator
reads during an incident, so a mismatch means the view they trust is not
describing the enforcement path. No admin credential is needed for any of
this.

Usage::

    scripts/leak_matrix.py --config leak-matrix.yaml
    scripts/leak_matrix.py --config leak-matrix.yaml --json out.json
    scripts/leak_matrix.py --config leak-matrix.yaml --fail-on-leak   # CI mode

Exit codes: ``0`` no findings, ``1`` at least one LEAK/DENIED-WRONGLY finding
(only when ``--fail-on-leak``; otherwise findings print and exit stays 0),
``2`` the sweep itself could not run (bad config, unreachable instance).

Config (YAML or JSON) — see ``docs/leak-matrix.md`` for the annotated
version::

    base_url: https://<host>
    canaries:
      - text: <distinctive string from a restricted document>
        visible_to: [analyst-with-package]
    personas:
      - name: wrong-domain-user
        token: env:LEAK_TOKEN_WRONGDOMAIN
        expect_tables: []                 # [] = must reach nothing
        expect_collections: []
      - name: analyst-with-package
        token: env:LEAK_TOKEN_ANALYST
        expect_tables: [orders, customers]
        expect_collections: [sow-library]
      - name: rfp-agent
        kind: agent_pat                   # agent PATs only authenticate
        token: env:LEAK_TOKEN_AGENT       # /api/v1/* — probes adjust
        expect_tables: [orders]

A token value of ``env:NAME`` is read from the environment, so no secret
lands in the config file or in this process's argv.

Never widens anything: every request is a GET, or a POST /api/query whose
body is a ``SELECT ... LIMIT 1``. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The repo's single SQL-identifier quoter. Table names in the probe below come
# from a catalog RESPONSE — data, from this script's point of view — so they get
# the same treatment as any other untrusted identifier
# (``tests/test_security_audit_20260805.py`` enforces this repo-wide). The
# module has no imports of its own, so depending on it costs the script
# nothing: it stays stdlib-only, it just has to be run from the checkout.
from src.sql_ident import quote_ident  # noqa: E402

LEAK = "LEAK"
WRONGLY_DENIED = "WRONGLY-DENIED"
DISAGREEMENT = "DISAGREEMENT"
GAP = "GAP"

_SEVERITY_ORDER = {LEAK: 0, DISAGREEMENT: 1, WRONGLY_DENIED: 2, GAP: 3}

# An agent PAT is rejected on every other prefix by construction
# (`app/auth/pat_resolver.py::_AGENT_PAT_ALLOWED_PREFIXES`), so probing the
# analyst surfaces with one would report authentication failures as denials
# and read as "no leak" for the wrong reason.
_AGENT_PAT_PREFIXES = ("/api/v1/",)


@dataclass
class Finding:
    severity: str
    persona: str
    surface: str
    detail: str
    resource: str = ""

    def line(self) -> str:
        where = f"{self.surface}" + (f" [{self.resource}]" if self.resource else "")
        return f"[{self.severity}] {self.persona} -> {where}: {self.detail}"


@dataclass
class Persona:
    name: str
    token: Optional[str]
    kind: str = "user"
    expect_tables: list = field(default_factory=list)
    expect_collections: list = field(default_factory=list)
    #: Filled by the sweep — what the persona actually reached.
    saw_tables: list = field(default_factory=list)
    saw_collections: list = field(default_factory=list)
    queryable: list = field(default_factory=list)
    skipped: list = field(default_factory=list)


class Client:
    """Minimal JSON HTTP client. Returns (status, parsed_body_or_text)."""

    def __init__(self, base_url: str, *, insecure: bool = False, timeout: int = 30):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self._ctx = None
        if insecure:
            self._ctx = ssl.create_default_context()
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE

    def call(self, method: str, path: str, token: Optional[str], body: Any = None):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if token:
            # Header, never a query parameter — a token in a URL lands in
            # access logs and proxy caches.
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, _maybe_json(raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            return e.code, _maybe_json(raw)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return 0, {"transport_error": str(e)}


def _maybe_json(raw: str):
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _resolve_secret(value: Optional[str], *, what: str) -> Optional[str]:
    """``env:NAME`` -> the environment's value; a literal passes through.

    A missing env var returns ``None`` rather than raising: a persona whose
    credential is unavailable becomes a reported GAP, which is the honest
    outcome — silently sweeping without it would print an empty row that
    reads exactly like "nothing leaked".
    """
    if not value:
        return None
    if value.startswith("env:"):
        name = value[4:]
        got = os.environ.get(name)
        if not got:
            print(f"note: {what} reads env:{name}, which is unset", file=sys.stderr)
        return got
    return value


def load_config(path: str) -> dict:
    text = open(path, encoding="utf-8").read()
    try:
        return json.loads(text)
    except ValueError:
        pass
    try:
        import yaml  # type: ignore
    except ImportError:
        raise SystemExit(
            f"{path} is not JSON and PyYAML is not installed — either install PyYAML or write the config as JSON"
        )
    parsed = yaml.safe_load(text)
    if not isinstance(parsed, dict):
        raise SystemExit(f"{path}: expected a mapping at the top level")
    return parsed


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def _table_ids(catalog_body: Any) -> list:
    """Table ids out of a /api/v2/catalog body, tolerant of shape drift."""
    if not isinstance(catalog_body, dict):
        return []
    rows = catalog_body.get("tables") or catalog_body.get("items") or []
    out = []
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict):
            ident = r.get("id") or r.get("name")
            if ident:
                out.append(str(ident))
    return sorted(out)


def _collection_ids(body: Any) -> list:
    if not isinstance(body, dict):
        return []
    rows = body.get("items") or []
    out = []
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict):
            ident = r.get("id") or r.get("slug")
            if ident:
                out.append(str(ident))
    return sorted(out)


def sweep_persona(client: Client, p: Persona, canaries: list, findings: list) -> None:
    if not p.token:
        findings.append(Finding(GAP, p.name, "-", "no credential available — this persona was NOT swept"))
        return

    agent_only = p.kind == "agent_pat"

    def reachable(path: str) -> bool:
        return (not agent_only) or path.startswith(_AGENT_PAT_PREFIXES)

    # --- catalog: what is OFFERED -----------------------------------------
    if reachable("/api/v2/catalog"):
        status, body = client.call("GET", "/api/v2/catalog", p.token)
        if status == 200:
            p.saw_tables = _table_ids(body)
            for t in p.saw_tables:
                if t not in p.expect_tables and not _is_internal(t):
                    findings.append(Finding(LEAK, p.name, "GET /api/v2/catalog", "offered an unexpected table", t))
            for t in p.expect_tables:
                if t not in p.saw_tables:
                    findings.append(
                        Finding(WRONGLY_DENIED, p.name, "GET /api/v2/catalog", "expected table not offered", t)
                    )
        elif status in (401, 403):
            findings.append(
                Finding(GAP, p.name, "GET /api/v2/catalog", f"credential refused ({status}) — probe inconclusive")
            )
        else:
            findings.append(Finding(GAP, p.name, "GET /api/v2/catalog", f"unexpected status {status}: {_short(body)}"))
    else:
        p.skipped.append("GET /api/v2/catalog (agent PAT: wrong surface by design)")

    # --- query: what is ENFORCED ------------------------------------------
    # The catalog is a projection; this is the authorization boundary. A
    # table missing from the catalog but queryable is precisely the gap a
    # catalog-only sweep reports as clean.
    if reachable("/api/query"):
        for table in sorted(set(p.saw_tables) | set(p.expect_tables)):
            if _is_internal(table):
                continue
            sql = f"SELECT * FROM {quote_ident(table)} LIMIT 1"
            status, body = client.call("POST", "/api/query", p.token, {"sql": sql})
            allowed = status == 200
            if allowed:
                p.queryable.append(table)
            if allowed and table not in p.expect_tables:
                findings.append(Finding(LEAK, p.name, "POST /api/query", "queried a table it must not reach", table))
            if not allowed and table in p.expect_tables and status in (401, 403):
                findings.append(
                    Finding(WRONGLY_DENIED, p.name, "POST /api/query", f"refused ({status}) an expected table", table)
                )
    else:
        p.skipped.append("POST /api/query (agent PAT: wrong surface by design)")

    # --- collections: listing + content ------------------------------------
    if reachable("/api/collections"):
        status, body = client.call("GET", "/api/collections", p.token)
        if status == 200:
            p.saw_collections = _collection_ids(body)
            for c in p.saw_collections:
                if c not in p.expect_collections:
                    findings.append(Finding(LEAK, p.name, "GET /api/collections", "listed an unexpected collection", c))
            for c in p.expect_collections:
                if c not in p.saw_collections:
                    findings.append(
                        Finding(WRONGLY_DENIED, p.name, "GET /api/collections", "expected collection not listed", c)
                    )
        elif status not in (401, 403):
            findings.append(Finding(GAP, p.name, "GET /api/collections", f"unexpected status {status}: {_short(body)}"))

        # Canary text: listing a collection and reading its CONTENT are
        # separate gates, so a redaction that only hides the row still has
        # to be probed through search.
        for canary in canaries:
            text = canary.get("text")
            if not text:
                continue
            may_see = p.name in (canary.get("visible_to") or [])
            for surface, path in (
                ("GET /api/collections/search", "/api/collections/search"),
                ("GET /api/knowledge/search", "/api/knowledge/search"),
            ):
                q = urllib.parse.urlencode({"q": text, "k": 5})
                status, body = client.call("GET", f"{path}?{q}", p.token)
                if status != 200:
                    continue
                hits = _hit_count(body)
                if hits and not may_see:
                    findings.append(Finding(LEAK, p.name, surface, f"canary text surfaced in {hits} hit(s)", text))
                elif not hits and may_see:
                    findings.append(
                        Finding(WRONGLY_DENIED, p.name, surface, "canary text not found though expected", text)
                    )

    # --- agents (agent-PAT persona) ---------------------------------------
    if agent_only:
        status, body = client.call("GET", "/api/v1/agents", p.token)
        if status not in (200, 403):
            findings.append(Finding(GAP, p.name, "GET /api/v1/agents", f"unexpected status {status}: {_short(body)}"))


def _hit_count(body: Any) -> int:
    if isinstance(body, dict):
        for key in ("results", "items", "hits"):
            val = body.get(key)
            if isinstance(val, list):
                return len(val)
    return 0


def _is_internal(table_id: str) -> bool:
    """Internal data-source tables are implicitly readable by every
    authenticated user with row-level filtering, so they are not findings."""
    return table_id.startswith("agnes_")


def _short(body: Any, limit: int = 160) -> str:
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    return text[:limit] + ("…" if len(text) > limit else "")


# ---------------------------------------------------------------------------
# Cross-check: what the server CLAIMS vs what the persona GOT
# ---------------------------------------------------------------------------


def cross_check(client: Client, p: Persona, findings: list) -> None:
    """Diff what the server SAYS this persona can read against what it did.

    Uses ``GET /api/me/effective-access`` — the persona asks with its own
    credential, so no admin token is needed and the answer honours the
    credential's own read surface (a ``surface='stack'`` PAT audits itself
    as filtered, exactly as a live query with it would be). The admin view
    of the same payload is what an operator reads during an incident, so a
    disagreement here means that view is not describing the enforcement
    path — worth reporting on its own, separately from a leak.
    """
    status, claim = client.call("GET", "/api/me/effective-access", p.token)
    if status != 200 or not isinstance(claim, dict):
        findings.append(
            Finding(GAP, p.name, "GET /api/me/effective-access", f"self-audit unavailable (status {status})")
        )
        return
    claimed = {str(t.get("table_id")) for t in (claim.get("tables") or []) if isinstance(t, dict) and t.get("table_id")}
    for t in sorted(set(p.queryable) - claimed - {x for x in p.queryable if _is_internal(x)}):
        findings.append(
            Finding(
                DISAGREEMENT,
                p.name,
                "effective-access vs POST /api/query",
                "queried a table the self-audit view does not list — the view an operator reads "
                "during an incident is not describing the enforcement path",
                t,
            )
        )
    for t in sorted(claimed - set(p.queryable)):
        findings.append(
            Finding(
                DISAGREEMENT,
                p.name,
                "effective-access vs POST /api/query",
                "self-audit lists a table the persona could not actually query",
                t,
            )
        )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def render(personas: list, findings: list) -> str:
    out = []
    out.append("Persona x resource sweep")
    out.append("=" * 72)
    for p in personas:
        out.append("")
        cred = "no credential" if not p.token else f"kind={p.kind}"
        out.append(f"{p.name}  ({cred})")
        if not p.token:
            out.append("  NOT SWEPT — see the GAP finding below")
            continue
        out.append(f"  tables offered   : {', '.join(p.saw_tables) or '(none)'}")
        out.append(f"  tables queryable : {', '.join(p.queryable) or '(none)'}")
        out.append(f"  tables expected  : {', '.join(p.expect_tables) or '(none)'}")
        out.append(f"  collections      : {', '.join(p.saw_collections) or '(none)'}")
        for s in p.skipped:
            out.append(f"  not probed       : {s}")

    out.append("")
    out.append("Findings")
    out.append("-" * 72)
    if not findings:
        out.append("none — every persona reached exactly what it declared")
    else:
        for f in sorted(findings, key=lambda f: (_SEVERITY_ORDER.get(f.severity, 9), f.persona)):
            out.append("  " + f.line())
    leaks = sum(1 for f in findings if f.severity in (LEAK, DISAGREEMENT))
    gaps = sum(1 for f in findings if f.severity == GAP)
    out.append("")
    out.append(f"summary: {leaks} leak/disagreement, {gaps} coverage gap, {len(findings)} finding(s) total")
    if gaps:
        out.append("NOTE: coverage gaps mean part of the matrix did not run — this is not a clean result.")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", required=True, help="YAML or JSON persona config")
    ap.add_argument("--json", dest="json_out", help="also write the findings as JSON to this path")
    ap.add_argument(
        "--fail-on-leak",
        action="store_true",
        help="exit 1 when a leak or disagreement is found (CI mode)",
    )
    ap.add_argument("--insecure", action="store_true", help="skip TLS verification (staging with a self-signed cert)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    base_url = cfg.get("base_url")
    if not base_url:
        raise SystemExit("config: base_url is required")

    client = Client(base_url, insecure=args.insecure)
    status, _ = client.call("GET", "/api/health", None)
    if status == 0:
        raise SystemExit(f"cannot reach {base_url} — is the host right and are you on the network?")

    personas = []
    for raw in cfg.get("personas") or []:
        name = raw.get("name")
        if not name:
            raise SystemExit("config: every persona needs a name")
        personas.append(
            Persona(
                name=name,
                token=_resolve_secret(raw.get("token"), what=f"persona {name}"),
                kind=raw.get("kind", "user"),
                expect_tables=list(raw.get("expect_tables") or []),
                expect_collections=list(raw.get("expect_collections") or []),
            )
        )
    if not personas:
        raise SystemExit("config: no personas declared — nothing to sweep")

    canaries = cfg.get("canaries") or []
    findings: list = []
    for p in personas:
        sweep_persona(client, p, canaries, findings)

    for p in personas:
        # Agent PATs cannot reach /api/me/* (wrong surface by design), and a
        # persona with no credential was never swept.
        if p.token and p.kind != "agent_pat":
            cross_check(client, p, findings)

    print(render(personas, findings))

    if args.json_out:
        payload = {
            "base_url": base_url,
            "personas": [
                {
                    "name": p.name,
                    "kind": p.kind,
                    "swept": bool(p.token),
                    "tables_offered": p.saw_tables,
                    "tables_queryable": p.queryable,
                    "tables_expected": p.expect_tables,
                    "collections": p.saw_collections,
                    "not_probed": p.skipped,
                }
                for p in personas
            ],
            "findings": [
                {
                    "severity": f.severity,
                    "persona": f.persona,
                    "surface": f.surface,
                    "resource": f.resource,
                    "detail": f.detail,
                }
                for f in findings
            ],
        }
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        print(f"\nJSON written to {args.json_out}")

    if args.fail_on_leak and any(f.severity in (LEAK, DISAGREEMENT) for f in findings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
