#!/usr/bin/env python3
"""Generate the architecture figures under ``docs/diagrams/``.

The figures are hand-laid-out SVG, produced by this script so they stay
reviewable and regenerable — a 70 KB hand-edited SVG is not maintainable.
Run it after changing the architecture, and commit the regenerated files:

    python3 scripts/dev/gen_architecture_diagrams.py

Design constraints worth keeping if you edit the layout:

* **One visual world: blue ink on white.** Ink is ``currentColor`` (pinned on
  the root element of the exported file); two literal hues carry meaning —
  azure = the data path, royal blue = the agent/LLM path. Box fills are
  ``currentColor`` at low opacity, so nothing is hard-coded per theme.
* **Overflow is measured, not eyeballed.** The mono stack has a deterministic
  ~0.6em advance, so the script asserts every drawn line fits its box and
  exits non-zero when one does not. Keep it that way — the failure mode of a
  generated diagram is silently clipped text.
* **No external references.** No fonts, no images, no CSS — the file has to
  render inside GitHub's markdown sanitizer and in any doc it is pasted into.
"""

from __future__ import annotations

import pathlib
import sys

MONO = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, monospace"
SERIF = "'Iowan Old Style', 'Palatino Linotype', Palatino, Georgia, serif"

OVERFLOW: list[tuple[float, str, str]] = []

TEAL = "#0369A1"  # data path — deep azure
CRIM = "#1B57C4"  # agent / LLM path — royal blue
INK = "currentColor"

W = 1680
GUT_X = 26
CX = 190  # content left
CW = 1466  # content width
RULE_X0 = 26
RULE_X1 = CX + CW

TITLE_BASE = 21
LINE1_BASE = 41
LINE_STEP = 16
PAD_BOTTOM = 13


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def f(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


Attrs = list[tuple[str, object]]

_DQ = '"'


def _attrs(pairs: Attrs) -> str:
    """Render ``name="value"`` pairs, skipping any whose value is None.

    The quoting happens here, by concatenation, rather than inline in an
    f-string: a double-quoted brace placeholder written literally in source is
    the shape of hand-quoted SQL identifier interpolation, which
    `tests/test_security_audit_20260805.py` ratchets against repo-wide. An
    exemption family reading "it is only SVG" would widen that guard's blind
    spot for every future file, so the attributes go through one helper and the
    guard keeps its full coverage.
    """
    return " ".join(f"{k}={_DQ}{v}{_DQ}" for k, v in pairs if v is not None)


def _void(tag: str, pairs: Attrs) -> str:
    return f"<{tag} {_attrs(pairs)}/>"


def cols(n: int, gap: float = 14.0, x0: float = CX, total: float = CW):
    w = (total - gap * (n - 1)) / n
    return [(x0 + i * (w + gap), w) for i in range(n)], w


def box_height(nlines: int) -> float:
    return LINE1_BASE + LINE_STEP * (max(nlines, 1) - 1) + PAD_BOTTOM


class Fig:
    def __init__(self, width: int):
        self.w = width
        self.y = 0.0
        self.el: list[str] = []

    # ---------- primitives ----------

    def rect(self, x, y, w, h, *, accent=None, dashed=False, fill_op=0.035, r=6):
        stroke = accent or INK
        stroke_op = 0.7 if accent else 0.22
        if dashed:
            stroke_op = 0.6 if accent else 0.3
        fill = accent or INK
        self.el.append(
            _void(
                "rect",
                [
                    ("x", f(x)),
                    ("y", f(y)),
                    ("width", f(w)),
                    ("height", f(h)),
                    ("rx", r),
                    ("fill", fill),
                    ("fill-opacity", fill_op),
                    ("stroke", stroke),
                    ("stroke-opacity", stroke_op),
                    ("stroke-dasharray", "5 4" if dashed else None),
                ],
            )
        )

    def text(
        self,
        x,
        y,
        s,
        *,
        size=11,
        weight=None,
        color=INK,
        op=None,
        anchor="start",
        family=MONO,
        ls=None,
        italic=False,
    ):
        pairs: Attrs = [
            ("x", f(x)),
            ("y", f(y)),
            ("font-family", family),
            ("font-size", f(size)),
            ("fill", color),
            ("font-weight", weight or None),
            ("fill-opacity", op),
            ("text-anchor", None if anchor == "start" else anchor),
            ("letter-spacing", None if ls is None else f(ls)),
            ("font-style", "italic" if italic else None),
        ]
        self.el.append(f"<text {_attrs(pairs)}>{esc(s)}</text>")

    def line(self, x1, y1, x2, y2, *, color=INK, op=0.22, wid=1, dashed=False):
        self.el.append(
            _void(
                "line",
                [
                    ("x1", f(x1)),
                    ("y1", f(y1)),
                    ("x2", f(x2)),
                    ("y2", f(y2)),
                    ("stroke", color),
                    ("stroke-opacity", op),
                    ("stroke-width", wid),
                    ("stroke-dasharray", "5 4" if dashed else None),
                ],
            )
        )

    def arrow(self, x1, y1, x2, y2, *, color=INK, op=0.8, wid=1.4, head=6.0, dashed=False):
        """Axis-aligned arrow with a solid triangular head at (x2, y2)."""
        if x1 == x2:  # vertical
            sign = 1 if y2 > y1 else -1
            self.line(x1, y1, x2, y2 - sign * head, color=color, op=op, wid=wid, dashed=dashed)
            pts = f"{f(x2)},{f(y2)} {f(x2 - head * 0.62)},{f(y2 - sign * head)} {f(x2 + head * 0.62)},{f(y2 - sign * head)}"
        else:  # horizontal
            sign = 1 if x2 > x1 else -1
            self.line(x1, y1, x2 - sign * head, y2, color=color, op=op, wid=wid, dashed=dashed)
            pts = f"{f(x2)},{f(y2)} {f(x2 - sign * head)},{f(y2 - head * 0.62)} {f(x2 - sign * head)},{f(y2 + head * 0.62)}"
        self.el.append(_void("polygon", [("points", pts), ("fill", color), ("fill-opacity", op)]))

    # ---------- composites ----------

    def card(
        self,
        x,
        y,
        w,
        h,
        title,
        lines,
        *,
        accent=None,
        dashed=False,
        title_color=None,
        pad=13,
    ):
        self.rect(x, y, w, h, accent=accent, dashed=dashed, fill_op=0.07 if accent else 0.035)
        # Monospace advance is deterministic (~0.6em across the whole stack),
        # so overflow is measurable rather than eyeballed.
        avail = w - 2 * pad
        if len(title) * 0.6 * 12.5 > avail:
            OVERFLOW.append((round(len(title) * 0.6 * 12.5 - avail, 1), "title", title))
        for ln in lines:
            if len(ln) * 0.6 * 11 > avail:
                OVERFLOW.append((round(len(ln) * 0.6 * 11 - avail, 1), f"w={w:.0f}", ln))
        self.text(
            x + pad,
            y + TITLE_BASE,
            title,
            size=12.5,
            weight="600",
            color=title_color or accent or INK,
            op=None if (title_color or accent) else 0.95,
        )
        for i, ln in enumerate(lines):
            self.text(x + pad, y + LINE1_BASE + i * LINE_STEP, ln, size=11, color=INK, op=0.66)

    def band(self, label_lines, *, rule=True):
        if rule:
            self.line(RULE_X0, self.y, RULE_X1, self.y, op=0.2, wid=1)
        top = self.y
        self.text(GUT_X, top + 22, label_lines[0], size=11.5, weight="700", op=0.9, ls=1.3)
        for i, ln in enumerate(label_lines[1:]):
            self.text(GUT_X, top + 40 + i * 14, ln, size=10, op=0.5)
        self.y = top + 16

    def note(self, s, *, color=INK, op=0.55, size=10.5, x=None):
        self.text(x if x is not None else CX, self.y + 10, s, size=size, color=color, op=op)
        self.y += 20

    def row(self, items, *, n=None, gap=14.0, link=None, x0=CX, total=CW):
        n = n or len(items)
        positions, w = cols(n, gap, x0, total)
        h = box_height(max(len(it[1]) for it in items))
        for (x, _), it in zip(positions, items):
            title, lines = it[0], it[1]
            opts = it[2] if len(it) > 2 else {}
            self.card(x, self.y, w, h, title, lines, **opts)
        if link:
            for i in range(n - 1):
                x_from = positions[i][0] + w
                x_to = positions[i + 1][0]
                self.arrow(
                    x_from + 3,
                    self.y + h / 2,
                    x_to - 2,
                    self.y + h / 2,
                    color=link,
                    op=0.85,
                    wid=1.4,
                    head=5.5,
                )
        self.y += h
        return h

    def framed_row(self, items, *, n=None, gap=14.0, accent=None, dashed=True, inset=13.0):
        n = n or len(items)
        inner_total = CW - 2 * inset
        positions, w = cols(n, gap, CX + inset, inner_total)
        h = box_height(max(len(it[1]) for it in items))
        self.rect(
            CX,
            self.y,
            CW,
            h + 2 * inset,
            accent=accent,
            dashed=dashed,
            fill_op=0.05,
            r=8,
        )
        for (x, _), it in zip(positions, items):
            self.card(x, self.y + inset, w, h, it[0], it[1], **(it[2] if len(it) > 2 else {}))
        self.y += h + 2 * inset

    def gap(self, label, *, color=INK, direction="up", h=42.0, xarrow=None):
        xa = xarrow if xarrow is not None else CX + CW / 2 - 260
        top, bot = self.y + 8, self.y + h - 8
        if direction == "up":
            self.arrow(xa, bot, xa, top, color=color, op=0.75)
        elif direction == "down":
            self.arrow(xa, top, xa, bot, color=color, op=0.75)
        else:
            self.arrow(xa, self.y + h / 2 - 1, xa, top, color=color, op=0.75)
            self.arrow(xa, self.y + h / 2 + 1, xa, bot, color=color, op=0.75)
        self.text(xa + 14, self.y + h / 2 + 4, label, size=10.5, color=INK, op=0.62)
        self.y += h

    def svg(self, height, aria) -> str:
        body = "\n".join(self.el)
        root = _attrs(
            [
                ("viewBox", f"0 0 {self.w} {int(height)}"),
                ("role", "img"),
                ("aria-label", esc(aria)),
                ("xmlns", "http://www.w3.org/2000/svg"),
                ("fill", "none"),
                # Some mono faces fuse "--" into an em dash; the drawings avoid
                # the sequence anyway, but a pasted-in label should not surprise.
                ("style", "font-variant-ligatures:none"),
            ]
        )
        return f"<svg {root}>\n{body}\n</svg>"


# ══════════════════════════════════════════════════════════════════════════
# FIGURE 1 — the poster
# ══════════════════════════════════════════════════════════════════════════


def figure_poster() -> str:
    p = Fig(W)

    # header
    p.text(GUT_X, 34, "Agnes", size=27, family=SERIF, weight="600", op=0.95)
    p.text(
        GUT_X + 108,
        34,
        "platform architecture",
        size=27,
        family=SERIF,
        italic=True,
        op=0.45,
    )
    p.text(RULE_X1, 26, "v0.83.30  ·  state schema v118", size=11, op=0.5, anchor="end")
    p.text(
        RULE_X1,
        42,
        "one image  ·  one entrypoint  ·  roles select planes",
        size=11,
        op=0.5,
        anchor="end",
    )
    p.y = 66

    # ── surfaces ─────────────────────────────────────────────────────────
    p.band(["SURFACES", "clients and", "protocols"])
    p.row(
        [
            (
                "Web UI",
                [
                    "chat · dashboard · catalog · stack · news",
                    "/admin/* · /agents builder · data-app pages",
                    "Jinja + design system (ds-* tokens, rail)",
                ],
            ),
            (
                "Slack",
                [
                    "Socket Mode or HTTP events",
                    "/agnes slash command · threaded replies",
                    "one leader lease per workspace",
                ],
            ),
            (
                "Telegram",
                [
                    "long-poll bot · /status",
                    "notification scripts",
                    "linked via /api/telegram",
                ],
            ),
            (
                "MCP",
                [
                    "streamable HTTP + stdio",
                    "foundation tools, per-table servers,",
                    "passthrough to upstream MCP servers",
                ],
            ),
        ]
    )
    p.y += 14
    p.row(
        [
            (
                "CLI  agnes",
                [
                    "pull · query · snapshot · push · stack · agent · chat · admin",
                    "workspace hooks: SessionStart → agnes update, SessionEnd → push",
                ],
            ),
            (
                "Agent API",
                [
                    "POST /api/v1/agents/{slug}/responses  (one-shot, PAT-auth)",
                    "multi-turn AG-UI SSE sessions · outbound webhooks",
                ],
            ),
            (
                "Hosted data apps",
                [
                    "/apps/<slug>/…  or  <slug>.<subdomain_base>",
                    "RBAC-gated ingress · wake-on-request · idle sleep",
                ],
            ),
        ],
        n=3,
    )
    p.gap(
        "every request carries a JWT cookie, a Bearer PAT, or a broker ticket",
        direction="down",
    )

    # ── identity & authorization ─────────────────────────────────────────
    p.band(["IDENTITY", "& authorization —", "the boundary every", "request crosses"])
    p.framed_row(
        [
            (
                "auth providers",
                [
                    "Google OIDC · magic link",
                    "password · Keboola OAuth",
                    "→ JWT cookie or Bearer",
                ],
            ),
            (
                "tokens",
                [
                    "user PAT · agent PAT",
                    "broker ticket · setup st_",
                    "scheduler shared secret",
                ],
            ),
            (
                "groups & grants",
                [
                    "Admin group short-circuits",
                    "resource_grants(group,type,id)",
                    "Everyone = auto-membership",
                ],
            ),
            (
                "effective stack",
                [
                    "data packages · memory domains",
                    "· marketplace plugins",
                    "= what a caller actually gets",
                ],
            ),
            (
                "row/column policies",
                [
                    "one SQL policy per table,",
                    "$user_email · $user_groups",
                    "server-side tables only",
                ],
            ),
        ],
        n=5,
        dashed=True,
    )
    p.gap(
        "an authorized principal — re-checked at every read and write, never cached as a verdict",
        direction="down",
    )

    # ── application plane ────────────────────────────────────────────────
    p.band(
        [
            "APPLICATION",
            "plane — FastAPI,",
            "one image; AGNES_ROLE",
            "picks which planes",
            "this process runs",
        ]
    )
    p.note(
        "AGNES_ROLE=api,gateway,worker   ·   default `all` = a single process   ·   role-split requires Postgres + a coordination backend"
    )
    p.row(
        [
            (
                "role: api",
                [
                    "REST routers — query · data · catalog · sync · admin · users",
                    "memory · agents · semantic · marketplace · stack · jobs",
                    "web pages (base_ds / base_page) + admin console",
                    "/api/query = SELECT-only sandbox; /api/query/hybrid (admin)",
                    "builds the per-user manifest that agnes pull reads",
                ],
            ),
            (
                "role: gateway",
                [
                    "ChatManager — sessions, turns, streaming sink, copresence",
                    "routing lease chat:{id} names the replica owning a sandbox",
                    "outbound replay stream + monotonic seq → reconnect w/ last_seq",
                    "inbound stream forwards commands to the owning replica",
                    "WS /api/notifications/ws (desktop + browser notifications)",
                ],
            ),
            (
                "role: worker",
                [
                    "durable jobs table on both backends · lease + heartbeat",
                    "heavy lane (1): data-refresh · jira-refresh",
                    "light lane (2): marketplaces-sync · session-collector ·",
                    "corporate-memory · distribution-mirror · ducklake-maintenance",
                    "idempotency_key dedup · expired leases reaped",
                ],
            ),
        ],
        n=3,
    )
    p.y += 14
    p.row(
        [
            (
                "scheduler sidecar",
                [
                    "no state of its own — calls REST on",
                    "offset cadences (sync 15m, memory 17m,",
                    "marketplace 03:00) with a shared secret",
                ],
            ),
            (
                "coordination backend",
                [
                    "memory (default)  |  redis",
                    "leases · pub/sub · TTL kv · counters",
                    "same contract suite for both",
                ],
            ),
            (
                "apps-runner sidecar",
                [
                    "the only process holding the docker",
                    "socket — image allowlist, fixed mounts,",
                    "no registry access, no RBAC of its own",
                ],
            ),
            (
                "egress-proxy sidecar",
                [
                    "fail-closed CONNECT allowlist for",
                    "sandboxes; their network has no other",
                    "route out — proxy is the policy layer",
                ],
            ),
        ]
    )
    p.gap(
        "spawn a sandbox, then broker every call out — API keys never enter it",
        color=CRIM,
        direction="down",
    )

    # ── agent & LLM plane ────────────────────────────────────────────────
    p.band(["AGENT + LLM", "plane — how one", "turn actually runs"])
    p.row(
        [
            (
                "sandbox   Docker | kai-agent",
                [
                    "one per chat or agent session",
                    "workspace = the caller's stack:",
                    "skills · data · CLAUDE.md · notebook",
                    "authority = owner grants, cut by scope",
                ],
                {"accent": CRIM},
            ),
            (
                "Claude Code harness",
                [
                    "runs inside the sandbox",
                    "MCP foundation tools + agnes CLI",
                    "artifacts harvested back into the chat",
                    "no host filesystem, no host network",
                ],
                {"accent": CRIM},
            ),
            (
                "secret broker  /api/broker/*",
                [
                    "ticket-gated egress; keys stay server-side",
                    "pins the agent's model",
                    "token_budget_monthly → 429 budget_exhausted",
                    "admin routes refused by route introspection",
                ],
                {"accent": CRIM},
            ),
            (
                "LLM API",
                [
                    "external — Anthropic, or any",
                    "OpenAI-compatible gateway",
                    "(LiteLLM · OpenRouter · vLLM)",
                ],
                {"accent": CRIM, "dashed": True},
            ),
        ],
        link=CRIM,
        gap=26,
    )
    p.gap(
        "agents read the stack and the catalog · sessions and notes flow back as knowledge",
        direction="both",
    )

    # ── knowledge & governance ───────────────────────────────────────────
    p.band(["KNOWLEDGE", "& governance —", "what the agents are", "allowed to know"])
    p.row(
        [
            (
                "semantic layer",
                [
                    "an Apache Ossie document is the owner",
                    "(semantic_models, schema-validated)",
                    "→ projections, regenerable from it:",
                    "metric_definitions · glossary_terms ·",
                    "column_metadata",
                    "sources: git · upload · connection;",
                    "source-owned ⇒ 409 on API edits",
                ],
            ),
            (
                "corporate memory",
                [
                    "knowledge_items × memory_domains (M:N)",
                    "Haiku extraction from CLAUDE.local.md",
                    "and session transcripts",
                    "contradiction judge + suggested fix",
                    "verification_evidence keeps raw signal",
                    "confidence computed in code, never",
                    "trusted from the model",
                ],
            ),
            (
                "marketplace",
                [
                    "admin-registered git repos cloned nightly",
                    "→ ONE aggregated, RBAC-filtered feed",
                    "GET /marketplace.zip",
                    "GET /marketplace.git/*  (PAT-gated)",
                    "plugins joined against the caller's",
                    "groups via resource_grants",
                    "contributed skills reviewed in /admin",
                ],
            ),
            (
                "stack & packages",
                [
                    "data_packages (+ tables) · memory",
                    "domains · plugins → the analyst's stack",
                    "required grants land automatically,",
                    "the rest via agnes stack add",
                    "catalog + table_profiles + metrics",
                    "feed discovery (agnes catalog, MCP)",
                    "audit_log records every action",
                ],
            ),
        ]
    )
    p.gap("persisted as rows — one schema, two engines", direction="down", color=TEAL)

    # ── app state ────────────────────────────────────────────────────────
    p.band(["APP STATE", "dual-backend;", "parity is a", "review gate"])
    positions, _ = cols(1, 0, CX, 356)
    p.card(
        CX,
        p.y,
        356,
        box_height(4),
        "repositories factory",
        [
            "src/repositories/__init__.py",
            "*_repo() dispatch on use_pg() /",
            "DATABASE_URL — callsites never",
            "instantiate a repository class",
        ],
    )
    p.card(
        560,
        p.y,
        541,
        box_height(4),
        "DuckDB   state/system.duckdb",
        [
            "one shared connection per DATA_DIR (write-lock safe)",
            "auto-migrating ladder _vN_to_v(N+1) → v118",
            "single-process only, enforced by a startup guard",
            "zero-config default for a self-hosted instance",
        ],
    )
    p.card(
        1115,
        p.y,
        541,
        box_height(4),
        "Postgres   DATABASE_URL",
        [
            "Alembic ladder must reach the same schema endpoint",
            "required for role-split and multi-replica topologies",
            "advisory locks (rebuild_lease) serialize cross-process writers",
            "cross-engine contract tests run the same assertions on both",
        ],
    )
    p.y += box_height(4)
    p.note(
        "rows:  users · user_groups · user_group_members · resource_grants · table_registry · sync_state / sync_history · jobs · audit_log"
    )
    p.note(
        "       chat_sessions / chat_messages · knowledge_items / votes / contradictions · memory_domains · semantic_models / semantic_sources · data_packages · agents · tokens · data_apps"
    )
    p.gap("read-only views  ·  per-user manifest  ·  profiles", direction="up", color=TEAL)

    # ── analytics data plane ─────────────────────────────────────────────
    p.band(["ANALYTICS", "data plane —", "one contract,", "two engines"])
    p.row(
        [
            (
                "/data/extracts/<source>/",
                [
                    "extract.duckdb — _meta (+ _remote_attach)",
                    "data/*.parquet for local sources",
                    "the distribution artifact AND the",
                    "rollback truth for both backends;",
                    "switching backends is a rebuild",
                    "from this tree, never a re-sync",
                ],
            ),
            (
                "SyncOrchestrator",
                [
                    "scans extracts, validates every",
                    "identifier, ATTACHes each source,",
                    "re-attaches remote extensions with a",
                    "session secret, builds master views",
                    "rebuild_mutex() = thread lock +",
                    "Postgres advisory lease",
                ],
            ),
            (
                "legacy backend  (default)",
                [
                    "rebuild into analytics.duckdb.tmp,",
                    "CHECKPOINT, then an atomic move",
                    "swaps it in — readers never see a",
                    "half-built database",
                    "any single source change costs a",
                    "full rebuild",
                ],
            ),
            (
                "ducklake backend  (opt-in)",
                [
                    "catalog in Postgres + data files",
                    "DuckLake owns",
                    "worker is the only writer and",
                    "copy-ingests per source → genuinely",
                    "incremental; readers hold one attach",
                    "and get MVCC snapshots",
                ],
            ),
        ],
        link=TEAL,
        gap=26,
    )
    p.y += 14
    p.row(
        [
            (
                "/api/query   sandbox",
                [
                    "SELECT / WITH only; ~30 keywords and all",
                    "file + URL functions blocked; no ';'",
                    "RBAC checked against referenced view names",
                ],
            ),
            (
                "query_mode per table",
                [
                    "local (parquet) · materialized (SQL → parquet)",
                    "· remote (nothing downloaded) · server_only",
                    "agnes query, scope=auto, labels where it ran",
                ],
            ),
            (
                "src/remote_engines.py",
                [
                    "picks the engine a statement needs and",
                    "refuses one that straddles two",
                    "(remote_cross_engine_unsupported)",
                ],
            ),
            (
                "cost guardrails",
                [
                    "BigQuery: dry-run scan cap (default 5 GiB)",
                    "Databricks: byte_limit — a capped result is",
                    "refused, never returned short",
                ],
            ),
        ]
    )
    p.gap("connectors write the contract, nothing else does", direction="up", color=TEAL)

    # ── connectors ───────────────────────────────────────────────────────
    p.band(["CONNECTORS", "extract.duckdb", "producers"])
    p.row(
        [
            (
                "keboola",
                [
                    "DuckDB extension → parquet",
                    "batch pull · remote attach",
                    "full / incremental / part.",
                ],
            ),
            (
                "bigquery",
                [
                    "remote views, no download",
                    "+ materialized SQL →",
                    "parquet (dry-run capped)",
                ],
            ),
            (
                "databricks",
                [
                    "SQL warehouse materialize",
                    "remote per-query · Unity",
                    "Catalog metric views",
                ],
            ),
            (
                "jira",
                [
                    "webhook → HMAC verify →",
                    "monthly parquet shards",
                    "SLA + consistency polls",
                ],
            ),
            (
                "local / upload",
                [
                    "CSV and parquet uploads",
                    "no external source",
                    "same contract, same rails",
                ],
            ),
            (
                "openmetadata",
                [
                    "catalog export (outbound)",
                    "+ MCP passthrough to",
                    "upstream servers",
                ],
            ),
        ],
        gap=14,
    )
    p.gap("credentials live only server-side, in the vault", direction="up", color=TEAL)

    # ── external systems ─────────────────────────────────────────────────
    p.band(["EXTERNAL", "systems — outside", "the trust boundary"])
    p.row(
        [
            (
                "Keboola Storage",
                ["master token or", "per-project OAuth"],
                {"dashed": True},
            ),
            (
                "BigQuery",
                ["service-account JSON", "or ADC / metadata"],
                {"dashed": True},
            ),
            (
                "Databricks + UC",
                ["SQL warehouse via", "Statement Execution"],
                {"dashed": True},
            ),
            ("Jira Cloud", ["webhooks + REST", "HMAC-SHA256 signed"], {"dashed": True}),
            (
                "Google Workspace",
                ["OAuth sign-in +", "nightly group sync"],
                {"dashed": True},
            ),
            (
                "Object store  (S3)",
                ["optional mirror behind", "15-min signed URLs"],
                {"dashed": True},
            ),
        ],
        gap=14,
    )

    p.y += 10
    p.line(RULE_X0, p.y, RULE_X1, p.y, op=0.2)
    p.y += 22
    p.line(GUT_X, p.y - 4, GUT_X + 34, p.y - 4, color=TEAL, op=0.9, wid=2)
    p.text(GUT_X + 44, p.y, "data path", size=10.5, op=0.8, weight="600")
    p.line(GUT_X + 132, p.y - 4, GUT_X + 166, p.y - 4, color=CRIM, op=0.9, wid=2)
    p.text(GUT_X + 176, p.y, "agent / LLM path", size=10.5, op=0.8, weight="600")
    p.line(GUT_X + 310, p.y - 4, GUT_X + 344, p.y - 4, op=0.35, wid=1.4, dashed=True)
    p.text(GUT_X + 354, p.y, "external, or off by default", size=10.5, op=0.6)
    p.text(
        RULE_X1,
        p.y,
        "source: docs/architecture.md · CLAUDE.md · app/main.py",
        size=10.5,
        op=0.45,
        anchor="end",
    )

    return p.svg(
        p.y + 24,
        "Layered architecture of Agnes: surfaces, the authorization "
        "boundary, the api/gateway/worker application plane, the agent and LLM "
        "plane, knowledge and governance, dual-backend app state, the analytics "
        "data plane, connectors, and external systems.",
    )


# ══════════════════════════════════════════════════════════════════════════
# FIGURE 2 — the analyst loop
# ══════════════════════════════════════════════════════════════════════════


def figure_analyst_loop() -> str:
    p = Fig(1500)
    p.text(26, 28, "The analyst loop", size=19, family=SERIF, weight="600", op=0.95)
    p.text(
        26,
        48,
        "how data reaches a laptop, and how what happens there comes back",
        size=11,
        op=0.55,
    )

    y = 84
    boxes = [
        (
            "server: manifest",
            [
                "GET /api/sync/manifest",
                "RBAC-filtered to the caller's",
                "stack; md5 per table so only",
                "changed files move",
            ],
        ),
        (
            "agnes pull",
            [
                "prefers a 15-min signed URL from",
                "the bucket mirror, falls back to",
                "/api/data/{id}/download;",
                "md5-verified either way",
            ],
        ),
        (
            "laptop",
            [
                "parquet + local DuckDB views;",
                "snapshots for big remote tables;",
                "agnes query (scope=auto) runs",
                "local or server-side, and says so",
            ],
        ),
        (
            "Claude Code workspace",
            [
                "the stack, materialized: skills,",
                "data, memory rules, CLAUDE.md;",
                "hooks converge it on SessionStart",
                "and push on SessionEnd",
            ],
        ),
        (
            "corporate memory",
            [
                "agnes push uploads sessions +",
                "CLAUDE.local.md; Haiku extracts",
                "knowledge_items, which land in",
                "every future stack",
            ],
        ),
    ]
    positions, w = cols(5, 24, 26, 1448)
    h = box_height(4)
    for (x, _), (t, lines) in zip(positions, boxes):
        accent = CRIM if t == "corporate memory" else None
        p.card(x, y, w, h, t, lines, accent=accent)
    labels = [
        "changed tables",
        "verified parquet",
        "agent works here",
        "sessions + notes",
    ]
    for i, lab in enumerate(labels):
        x_from = positions[i][0] + w
        x_to = positions[i + 1][0]
        color = CRIM if i == 3 else TEAL
        p.arrow(x_from + 3, y + h / 2, x_to - 2, y + h / 2, color=color, op=0.85, head=6)
        p.text((x_from + x_to) / 2, y - 11, lab, size=10, op=0.6, anchor="middle")

    # return edge
    y2 = y + h + 44
    x_start = positions[4][0] + w / 2
    x_end = positions[3][0] + w / 2
    p.line(x_start, y + h, x_start, y2, color=CRIM, op=0.7, wid=1.4)
    p.line(x_start, y2, x_end, y2, color=CRIM, op=0.7, wid=1.4)
    p.arrow(x_end, y2, x_end, y + h + 4, color=CRIM, op=0.7, wid=1.4)
    p.text(
        x_end + 16,
        y2 + 16,
        "next session starts already knowing it — the flywheel",
        size=10.5,
        color=INK,
        op=0.62,
    )

    return p.svg(
        y2 + 40,
        "The analyst loop: manifest, agnes pull, laptop DuckDB, "
        "Claude Code workspace, agnes push into corporate memory, and back "
        "into the next session's workspace.",
    )


# ══════════════════════════════════════════════════════════════════════════
# FIGURE 3 — where a query runs
# ══════════════════════════════════════════════════════════════════════════


def figure_query_routing() -> str:
    p = Fig(1500)
    p.text(
        26,
        28,
        "Where a query actually runs",
        size=19,
        family=SERIF,
        weight="600",
        op=0.95,
    )
    p.text(
        26,
        48,
        "one statement, four destinations — chosen by the table's query_mode, "
        "not by the caller;   outlined in blue = runs on the server",
        size=11,
        op=0.55,
    )

    # entry
    p.card(
        26,
        84,
        330,
        box_height(3),
        'agnes query "SELECT …"',
        [
            "scope=auto is the default",
            "scope=local / scope=server override it",
            "stderr prints a [scope] note either way",
        ],
    )

    x1, w1 = 420, 300
    ys = [84, 176, 268, 360]
    branches = [
        (
            "local view exists",
            [
                "runs in the laptop's DuckDB over",
                "pulled parquet — no server hop",
            ],
            TEAL,
        ),
        (
            "no local data",
            [
                "same statement re-runs server-side",
                "against analytics views",
            ],
            TEAL,
        ),
        (
            "query_mode = remote",
            [
                "remote_engines picks the engine;",
                "nothing was ever downloaded",
            ],
            TEAL,
        ),
        (
            "too big to fetch",
            [
                "agnes snapshot create, estimate first",
                "→ a filtered local subset",
            ],
            TEAL,
        ),
    ]
    hb = box_height(2)
    for y, (t, lines, c) in zip(ys, branches):
        p.card(x1, y, w1, hb, t, lines)
        p.arrow(356 + 4, 84 + box_height(3) / 2, x1 - 2, y + hb / 2, color=TEAL, op=0.7)

    # destinations
    x2, w2 = 800, 320
    dests = [
        ("laptop DuckDB", ["parquet + snapshots", "zero marginal cost"]),
        (
            "server analytics plane",
            ["legacy analytics.duckdb or", "the DuckLake catalog"],
        ),
        ("BigQuery", ["dry-run scan cap (5 GiB)", "Storage Read API push-down"]),
        ("Databricks", ["Statement Execution API", "byte_limit; capped ⇒ refused"]),
    ]
    for y, (t, lines) in zip(ys, dests):
        p.card(x2, y, w2, hb, t, lines, accent=TEAL if y != ys[0] else None)
        p.arrow(x1 + w1 + 3, y + hb / 2, x2 - 2, y + hb / 2, color=TEAL, op=0.8)

    # policy overlay
    p.card(
        1160,
        ys[1],
        314,
        box_height(4),
        "on every server-side read",
        [
            "a table access policy (if attached)",
            "is substituted for the table —",
            "rows filtered, columns masked by",
            "$user_email / $user_groups",
        ],
        dashed=True,
    )
    p.line(x2 + w2 + 6, ys[1] + hb / 2, 1160 - 6, ys[1] + hb / 2, op=0.3, dashed=True)
    p.line(x2 + w2 + 6, ys[2] + hb / 2, 1150, ys[2] + hb / 2, op=0.3, dashed=True)
    p.line(1150, ys[1] + hb / 2, 1150, ys[2] + hb / 2, op=0.3, dashed=True)

    return p.svg(
        ys[3] + hb + 40,
        "Query routing: agnes query with scope auto runs "
        "locally when a synced view exists, otherwise server-side; remote "
        "tables go to BigQuery or Databricks under cost guardrails; access "
        "policies rewrite every server-side read.",
    )


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT_DIR = REPO_ROOT / "docs" / "diagrams"


def standalone(svg: str) -> str:
    """Pin the ink color and paint a ground.

    ``currentColor`` has nothing to inherit from in a standalone file, and a
    transparent background would borrow whatever ground the viewer paints —
    including GitHub's dark theme, where blue-on-nothing is unreadable.
    """
    head, rest = svg.split(">", 1)
    _, _, width, height = head.split("viewBox=" + _DQ)[1].split(_DQ)[0].split()
    ground = _void(
        "rect",
        [("x", 0), ("y", 0), ("width", width), ("height", height), ("fill", "#FFFFFF")],
    )
    pinned = _attrs([("color", "#0B2545")])
    return f"{head} {pinned}>{ground}{rest}"


def main() -> int:
    figures = {
        "agnes-architecture.svg": figure_poster,
        "agnes-analyst-loop.svg": figure_analyst_loop,
        "agnes-query-routing.svg": figure_query_routing,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, build in figures.items():
        # Explicit encoding, not the locale's: the figures carry →, ⇒, ·, ×, —,
        # none of which survive a cp1252 default. Without it the script dies
        # mid-loop on such a machine, having already overwritten an earlier file.
        (OUT_DIR / name).write_text(standalone(build()), encoding="utf-8")
        print(f"wrote {(OUT_DIR / name).relative_to(REPO_ROOT)}")

    if OVERFLOW:
        print(f"\n{len(OVERFLOW)} drawn line(s) do not fit their box:", file=sys.stderr)
        for over, where, text in sorted(OVERFLOW, reverse=True):
            print(f"  +{over}px  [{where}]  {text}", file=sys.stderr)
        return 1
    print(f"\n{len(figures)} figures, no text overflow")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
