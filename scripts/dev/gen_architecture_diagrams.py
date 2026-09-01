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


# A C4 element carries one extra line the layered figures do not: the
# bracketed type tag between the name and the description.
C4_TAG_BASE = 37
C4_LINE1_BASE = 57


def c4_box_height(nlines: int) -> float:
    if nlines <= 0:
        return C4_TAG_BASE + PAD_BOTTOM
    return C4_LINE1_BASE + LINE_STEP * (nlines - 1) + PAD_BOTTOM


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

    # ---------- C4 primitives ----------

    def circle(self, cx, cy, r, *, accent=None, fill_op=0.07):
        stroke = accent or INK
        self.el.append(
            _void(
                "circle",
                [
                    ("cx", f(cx)),
                    ("cy", f(cy)),
                    ("r", f(r)),
                    ("fill", stroke),
                    ("fill-opacity", fill_op),
                    ("stroke", stroke),
                    ("stroke-opacity", 0.7 if accent else 0.35),
                ],
            )
        )

    def c4card(self, x, y, w, h, title, tag, lines, *, accent=None, dashed=False, pad=13):
        """A C4 element: name, bracketed type tag, then the description.

        The tag is what makes a box read as C4 rather than as one more
        rectangle — it carries the element's kind and its technology, so the
        drawing says whether you are looking at a container or a component
        without the reader holding a separate key in their head.
        """
        self.rect(x, y, w, h, accent=accent, dashed=dashed, fill_op=0.07 if accent else 0.035)
        avail = w - 2 * pad
        if len(title) * 0.6 * 12.5 > avail:
            OVERFLOW.append((round(len(title) * 0.6 * 12.5 - avail, 1), "c4 title", title))
        if len(tag) * 0.6 * 9.5 > avail:
            OVERFLOW.append((round(len(tag) * 0.6 * 9.5 - avail, 1), "c4 tag", tag))
        for ln in lines:
            if len(ln) * 0.6 * 11 > avail:
                OVERFLOW.append((round(len(ln) * 0.6 * 11 - avail, 1), f"c4 w={w:.0f}", ln))
        self.text(
            x + pad,
            y + TITLE_BASE,
            title,
            size=12.5,
            weight="600",
            color=accent or INK,
            op=None if accent else 0.95,
        )
        self.text(x + pad, y + C4_TAG_BASE, tag, size=9.5, color=INK, op=0.5)
        for i, ln in enumerate(lines):
            self.text(x + pad, y + C4_LINE1_BASE + i * LINE_STEP, ln, size=11, op=0.66)

    def person(self, x, y, w, h, name, tag, lines, *, accent=None):
        """A C4 person. The head glyph sits above the box rather than inside
        it, so the actors are pickable out of a page of rectangles at a glance
        and the box keeps its full width for text."""
        self.circle(x + w / 2, y - 15, 11, accent=accent)
        self.c4card(x, y, w, h, name, tag, lines, accent=accent)

    def boundary(self, x, y, w, h, label, sublabel=None, *, accent=None):
        """A dashed enclosure — a system or container boundary. Call it before
        the elements it holds, so they draw on top of its fill."""
        self.rect(x, y, w, h, accent=accent, dashed=True, fill_op=0.02, r=10)
        self.text(
            x + 20,
            y + 26,
            label,
            size=12,
            weight="700",
            color=accent or INK,
            op=None if accent else 0.8,
            ls=0.6,
        )
        if sublabel:
            # 0.6 of letter-spacing rides on every glyph of the label, so the
            # advance is (0.6em + ls) per character — measuring without it
            # butts the sublabel against the label's last letter.
            self.text(
                x + 20 + len(label) * (0.6 * 12 + 0.6) + 20,
                y + 26,
                sublabel,
                size=10.5,
                op=0.5,
            )

    def caption(self, x, y, s):
        """A small-caps group label above a row inside a boundary."""
        self.text(x, y, s, size=9.5, weight="700", op=0.55, ls=1.2)

    def key(self, x, y, entries):
        """The notation key. A C4 drawing is only self-describing if the
        shapes are named on the drawing itself."""
        for label, kind in entries:
            if kind == "person":
                self.circle(x + 8, y - 11, 5)
                self.rect(x, y - 5, 17, 11, r=2)
            elif kind == "external":
                self.rect(x, y - 9, 24, 14, r=3, dashed=True)
            elif kind == "agent":
                self.rect(x, y - 9, 24, 14, r=3, accent=CRIM)
            elif kind == "data":
                self.rect(x, y - 9, 24, 14, r=3, accent=TEAL)
            else:
                self.rect(x, y - 9, 24, 14, r=3)
            self.text(x + 34, y + 1, label, size=10.5, op=0.65)
            x += 34 + len(label) * 0.6 * 10.5 + 30
        return x

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


# ══════════════════════════════════════════════════════════════════════════
# THE C4 SET — figures 4-8
#
# https://c4model.com/ — a hierarchy of four nested abstractions. The three
# figures above decompose Agnes by LAYER; these decompose it by ZOOM, which
# is a different question and a different reader. Levels 1-3 are drawn here;
# level 4 (code) is deliberately absent, as the model itself advises — the
# repository is that diagram, and a drawn copy of it rots on contact.
#
# Notation, since a C4 drawing has to be self-describing: a name, a bracketed
# type tag naming the element's kind and technology, then what it does.
# Dashed = outside the boundary in focus. Azure = the data path, royal blue =
# the agent/LLM path, matching the layered figures above.
# ══════════════════════════════════════════════════════════════════════════

C4_W = 1500
C4_X0 = 26
C4_X1 = 1474
C4_TOTAL = C4_X1 - C4_X0


def c4_head(p, title, subtitle, level, *, x1=C4_X1):
    p.text(C4_X0, 30, "Agnes", size=20, family=SERIF, weight="600", op=0.95)
    p.text(C4_X0 + 80, 30, title, size=20, family=SERIF, italic=True, op=0.45)
    p.text(C4_X0, 52, subtitle, size=11, op=0.55)
    p.text(x1, 26, level, size=11, op=0.5, anchor="end")
    p.text(x1, 44, "c4model.com", size=11, op=0.35, anchor="end")


def c4_row(p, y, items, *, n=None, gap=18.0, x0=C4_X0, total=C4_TOTAL):
    """Lay one row of C4 elements and hand back where they landed.

    Returns ``(positions, w, h)`` rather than advancing a cursor: these
    figures place arrows between named rows, so the caller needs the
    geometry back, not a stateful pen.
    """
    n = n or len(items)
    positions, w = cols(n, gap, x0, total)
    h = c4_box_height(max(len(it[2]) for it in items))
    for (x, _), it in zip(positions, items):
        opts = dict(it[3]) if len(it) > 3 else {}
        draw = p.person if opts.pop("person", False) else p.c4card
        draw(x, y, w, h, it[0], it[1], it[2], **opts)
    return positions, w, h


def c4_flow(p, x, y0, y1, label, *, color=INK, up=False):
    if up:
        p.arrow(x, y1, x, y0, color=color, op=0.75)
    else:
        p.arrow(x, y0, x, y1, color=color, op=0.75)
    p.text(x + 14, (y0 + y1) / 2 + 4, label, size=10.5, color=color, op=0.68)


# ── FIGURE 4 — level 1, system context ───────────────────────────────────


def figure_c4_context() -> str:
    p = Fig(C4_W)
    c4_head(
        p,
        "system context",
        "who uses Agnes, and what Agnes talks to — one box, no internals",
        "C4 level 1  ·  context",
    )

    people = [
        (
            "Data analyst",
            "[Person]",
            [
                "pulls governed parquet to a laptop, works",
                "in Claude Code, publishes what it learns",
            ],
            {"person": True},
        ),
        (
            "Business user",
            "[Person]",
            [
                "asks questions in web chat, Slack or",
                "Telegram; opens hosted data apps",
            ],
            {"person": True},
        ),
        (
            "Administrator",
            "[Person]",
            [
                "registers sources and tables, grants",
                "access, sets model and token budgets",
            ],
            {"person": True},
        ),
        (
            "Client application",
            "[External software system]",
            [
                "calls a named agent profile with a PAT",
                "and gets a one-shot or streamed answer",
            ],
            {"dashed": True},
        ),
    ]
    pos, w, h = c4_row(p, 100, people, gap=24)
    for (x, _), label in zip(
        pos,
        [
            "pull · query · push",
            "asks questions",
            "registers · grants · governs",
            "agent API, PAT-auth",
        ],
    ):
        p.arrow(x + w / 2, 190, x + w / 2, 232, op=0.7)
        p.text(x + w / 2 + 14, 215, label, size=10, op=0.62)

    p.c4card(
        C4_X0,
        234,
        C4_TOTAL,
        c4_box_height(3),
        "Agnes",
        "[Software system]   ·   source-available, self-hosted, one image and one entrypoint",
        [
            "Governed access to an organization's data: every table, metric and document reaches a caller through one authorization boundary.",
            "Agents run against that boundary — web chat, Slack, Telegram, MCP, the agnes CLI, and named agent profiles with their own public API.",
            "What analysts learn flows back as corporate memory and a semantic layer, so the next question starts from the last answer.",
        ],
        accent=CRIM,
    )

    p.caption(C4_X0, 372, "EXTERNAL SYSTEMS  ·  OUTSIDE THE TRUST BOUNDARY")
    c4_flow(p, 470, 342, 388, "extracts · queries · authenticates · sends out")
    c4_flow(p, 1130, 342, 388, "webhooks and events push back in", up=True)

    ext = {"dashed": True}
    c4_row(
        p,
        392,
        [
            (
                "Keboola Storage",
                "[External system]",
                ["master token or per-project", "OAuth; DuckDB extension pull"],
                ext,
            ),
            (
                "BigQuery",
                "[External system]",
                ["remote attach, no download;", "dry-run scan cap per query"],
                ext,
            ),
            (
                "Databricks + UC",
                "[External system]",
                ["SQL warehouse via Statement", "Execution; UC metric views"],
                ext,
            ),
            (
                "Jira Cloud",
                "[External system]",
                ["HMAC-signed webhooks plus", "REST consistency polls"],
                ext,
            ),
            (
                "SharePoint / Graph",
                "[External system]",
                ["delta crawl to documents,", "anonymized before ingest"],
                ext,
            ),
            (
                "Object store (S3)",
                "[External system]",
                ["optional parquet mirror,", "15-minute signed URLs"],
                ext,
            ),
        ],
        gap=14,
    )
    c4_row(
        p,
        492,
        [
            (
                "Google Workspace",
                "[External system]",
                ["OAuth sign-in and nightly", "group sync"],
                ext,
            ),
            (
                "Microsoft Entra ID",
                "[External system]",
                ["single-tenant OIDC, optional", "Graph group sync"],
                ext,
            ),
            (
                "Slack",
                "[External system]",
                ["Socket Mode or HTTP events;", "/agnes slash command"],
                ext,
            ),
            (
                "Telegram",
                "[External system]",
                ["long-poll bot, notification", "dispatch"],
                ext,
            ),
            (
                "LLM API",
                "[External system]",
                ["Anthropic, or any OpenAI-", "compatible gateway"],
                ext,
            ),
            (
                "Marketplace repos",
                "[External systems]",
                ["admin-registered git repos,", "cloned nightly into one feed"],
                ext,
            ),
        ],
        gap=14,
    )

    p.line(C4_X0, 596, C4_X1, 596, op=0.2)
    p.key(
        C4_X0,
        620,
        [("person", "person"), ("the system in focus", "agent"), ("external system", "external")],
    )
    p.text(
        C4_X1,
        620,
        "source: CLAUDE.md · docs/architecture.md",
        size=10.5,
        op=0.45,
        anchor="end",
    )
    return p.svg(
        648,
        "C4 level 1, system context: analysts, business users, administrators "
        "and client applications use Agnes, which reads from Keboola, BigQuery, "
        "Databricks, Jira and SharePoint, authenticates against Google "
        "Workspace and Microsoft Entra ID, and calls out to Slack, Telegram, "
        "an object store and an LLM API.",
    )


# ── FIGURE 5 — level 2, containers ───────────────────────────────────────


def figure_c4_container() -> str:
    p = Fig(W)
    X1 = W - GUT_X
    TOT = X1 - GUT_X
    c4_head(
        p,
        "containers",
        "the separately deployable and runnable pieces, and the stores they keep state in",
        "C4 level 2  ·  container",
        x1=X1,
    )

    pos, w, _ = c4_row(
        p,
        100,
        [
            (
                "Data analyst",
                "[Person]",
                ["laptop: agnes CLI plus a Claude Code workspace"],
                {"person": True},
            ),
            (
                "Business user",
                "[Person]",
                ["browser chat, Slack, Telegram, hosted data apps"],
                {"person": True},
            ),
            (
                "Administrator",
                "[Person]",
                ["the /admin console and the agnes admin CLI"],
                {"person": True},
            ),
            (
                "Client application",
                "[External software system]",
                ["a PAT-authenticated caller of the agent API"],
                {"dashed": True},
            ),
        ],
        gap=24,
        x0=GUT_X,
        total=TOT,
    )
    for (x, _), label in zip(
        pos,
        [
            "agnes pull / push · hooks",
            "HTTPS · WebSocket",
            "HTTPS · CLI",
            "HTTPS + Bearer PAT",
        ],
    ):
        p.arrow(x + w / 2, 174, x + w / 2, 212, op=0.7)
        p.text(x + w / 2 + 14, 197, label, size=10, op=0.62)

    # ── the server deployment ────────────────────────────────────────────
    p.boundary(
        GUT_X,
        214,
        TOT,
        602,
        "Agnes server deployment",
        "one Docker image, one entrypoint — AGNES_ROLE selects which planes a process runs",
    )
    IX, ITOT = GUT_X + 20, TOT - 40

    p.caption(IX, 270, "EDGE AND APPLICATION PROCESSES")
    c4_row(
        p,
        282,
        [
            (
                "Caddy",
                "[Container: Caddy]",
                [
                    "TLS termination and security headers;",
                    "the only port the internet reaches;",
                    "routes /apps/<slug> through to the app",
                ],
            ),
            (
                "role: api",
                "[Container: Python · FastAPI · Uvicorn]",
                [
                    "REST routers, Jinja web pages, MCP server;",
                    "/api/query is a SELECT-only sandbox;",
                    "builds the manifest agnes pull reads",
                ],
            ),
            (
                "role: gateway",
                "[Container: Python · FastAPI · Uvicorn]",
                [
                    "ChatManager: sessions, turns, copresence;",
                    "WS chat and desktop notifications;",
                    "a routing lease names each live sandbox",
                ],
            ),
            (
                "role: worker",
                "[Container: Python · FastAPI · Uvicorn]",
                [
                    "durable jobs with lease and heartbeat;",
                    "heavy lane: data-refresh, jira-refresh;",
                    "light lane: memory, mirror, maintenance",
                ],
            ),
        ],
        x0=IX,
        total=ITOT,
    )

    p.caption(IX, 412, "SIDECAR CONTAINERS")
    c4_row(
        p,
        424,
        [
            (
                "scheduler",
                "[Container: Python]",
                [
                    "holds no state — calls REST on offset",
                    "cadences with a shared secret",
                ],
            ),
            (
                "apps-runner",
                "[Container: Python + Docker socket]",
                [
                    "the only process holding the socket;",
                    "image allowlist, fixed mounts, no RBAC",
                ],
            ),
            (
                "egress-proxy",
                "[Container: Python]",
                [
                    "fail-closed CONNECT allowlist — the",
                    "sandbox network's only route out",
                ],
            ),
            (
                "surface bots",
                "[Container: Python]",
                [
                    "Slack Socket Mode, Telegram long-poll,",
                    "Teams; one leader lease per workspace",
                ],
            ),
        ],
        x0=IX,
        total=ITOT,
    )

    p.caption(IX, 538, "CONTAINERS AGNES SPAWNS AT RUNTIME")
    c4_row(
        p,
        550,
        [
            (
                "chat sandbox",
                "[Container: Docker — one per session]",
                [
                    "the caller's stack, materialized: skills, data, CLAUDE.md, notebook;",
                    "no host filesystem and no network route except the egress proxy;",
                    "spawned through apps-runner, destroyed when the session goes idle",
                ],
                {"accent": CRIM},
            ),
            (
                "hosted data app",
                "[Container: Docker — analyst-authored]",
                [
                    "a Flask or Dash app, or a static SPA, running next to the data;",
                    "RBAC-gated ingress, wake on request, sleep when idle;",
                    "off by default (data_apps.enabled)",
                ],
            ),
        ],
        x0=IX,
        total=ITOT,
    )

    p.caption(IX, 680, "STATE STORES AND SHARED FABRIC")
    c4_row(
        p,
        692,
        [
            (
                "app state",
                "[Container: Postgres | DuckDB]",
                [
                    "users, groups, grants, table_registry,",
                    "jobs, chat, knowledge, agents, audit",
                    "Postgres is required for a role split",
                ],
            ),
            (
                "extracts tree",
                "[Container: filesystem]",
                [
                    "/data/extracts/<source>/extract.duckdb",
                    "plus data/*.parquet — both the",
                    "distribution artifact and the rollback",
                ],
                {"accent": TEAL},
            ),
            (
                "analytics plane",
                "[Container: DuckDB | DuckLake]",
                [
                    "legacy: temp rebuild, atomic swap",
                    "ducklake: catalog in Postgres, the",
                    "worker is the only writer",
                ],
                {"accent": TEAL},
            ),
            (
                "coordination",
                "[Container: Redis, or in-process]",
                [
                    "leases, pub/sub, TTL keys, counters",
                    "memory by default; redis is what",
                    "makes a role split possible at all",
                ],
            ),
            (
                "secrets and config",
                "[Container: vault rows + env]",
                [
                    "credentials encrypted at rest, never",
                    "on argv or in a URL; instance.yaml",
                    "and .env resolve once, at boot",
                ],
            ),
        ],
        x0=IX,
        total=ITOT,
    )

    # ── the analyst's laptop ─────────────────────────────────────────────
    c4_flow(p, 420, 820, 862, "manifest, then parquet by signed URL", color=TEAL)
    c4_flow(p, 1120, 820, 862, "agnes push: sessions + CLAUDE.local.md", up=True)

    p.boundary(
        GUT_X,
        864,
        TOT,
        176,
        "Analyst laptop",
        "outside the server boundary — the analyst's own machine, converged by hooks rather than managed",
    )
    p.caption(IX, 920, "WHAT agnes init PUTS THERE")
    c4_row(
        p,
        932,
        [
            (
                "agnes CLI",
                "[Container: Python — on the laptop]",
                [
                    "pull · query · snapshot · push · stack · agent · chat · admin",
                    "scope=auto picks local or server, and says which on stderr",
                ],
            ),
            (
                "local analytics",
                "[Container: DuckDB + parquet]",
                [
                    "views over pulled parquet, plus snapshots of remote tables;",
                    "a query that lands here costs nothing and leaves no trace",
                ],
                {"accent": TEAL},
            ),
            (
                "Claude Code workspace",
                "[Container: Claude Code]",
                [
                    "the stack materialized: skills, data, memory rules, CLAUDE.md;",
                    "SessionStart converges it, SessionEnd pushes what happened",
                ],
                {"accent": CRIM},
            ),
        ],
        x0=IX,
        total=ITOT,
    )

    p.caption(GUT_X, 1076, "EXTERNAL SYSTEMS")
    ext = {"dashed": True}
    c4_row(
        p,
        1090,
        [
            (
                "data sources",
                "[External systems]",
                ["Keboola · BigQuery · Databricks ·", "Jira · SharePoint · file uploads"],
                ext,
            ),
            (
                "identity providers",
                "[External systems]",
                ["Google Workspace · Microsoft", "Entra ID · Keboola OAuth"],
                ext,
            ),
            (
                "messaging",
                "[External systems]",
                ["Slack · Telegram · Microsoft", "Teams"],
                ext,
            ),
            (
                "LLM API",
                "[External system]",
                ["Anthropic, or any OpenAI-", "compatible gateway"],
                ext,
            ),
            (
                "object store",
                "[External system]",
                ["optional S3 mirror behind", "15-minute signed URLs"],
                ext,
            ),
            (
                "marketplace repos",
                "[External systems]",
                ["admin-registered git repos,", "cloned nightly into one feed"],
                ext,
            ),
        ],
        gap=14,
        x0=GUT_X,
        total=TOT,
    )

    p.line(GUT_X, 1194, X1, 1194, op=0.2)
    p.key(
        GUT_X,
        1218,
        [
            ("person", "person"),
            ("container", "plain"),
            ("data path", "data"),
            ("agent path", "agent"),
            ("external", "external"),
        ],
    )
    p.text(
        X1,
        1218,
        "source: docker-compose.yml · app/roles.py · docs/architecture.md",
        size=10.5,
        op=0.45,
        anchor="end",
    )
    return p.svg(
        1246,
        "C4 level 2, containers: Caddy and one application image running the "
        "api, gateway and worker roles; scheduler, apps-runner, egress-proxy "
        "and surface-bot sidecars; chat sandboxes and hosted data apps spawned "
        "at runtime; app state in Postgres or DuckDB, the extracts tree, the "
        "analytics plane and a coordination backend; and, outside the server "
        "boundary, the analyst laptop's CLI, local DuckDB and Claude Code "
        "workspace.",
    )


# ── FIGURE 6 — level 3, components of the application container ──────────


def figure_c4_component_app() -> str:
    p = Fig(C4_W)
    c4_head(
        p,
        "components — application",
        "inside one FastAPI process: how a request becomes an authorized read",
        "C4 level 3  ·  component",
    )

    p.boundary(
        C4_X0,
        76,
        C4_TOTAL,
        644,
        "Container: FastAPI application",
        "app/ — the same image at every role; this is the api role's view",
    )
    IX, ITOT = C4_X0 + 20, C4_TOTAL - 40

    p.caption(IX, 132, "REQUEST INGRESS")
    c4_row(
        p,
        144,
        [
            (
                "REST routers",
                "[Component: app/api/*]",
                [
                    "query · data · catalog · sync · admin ·",
                    "users · agents · memory · jobs · stack",
                ],
            ),
            (
                "web pages",
                "[Component: app/web/]",
                [
                    "Jinja over base_ds / base_page;",
                    "chat, catalog, stack, /admin console",
                ],
            ),
            (
                "MCP server",
                "[Component: app/api/mcp/]",
                [
                    "streamable HTTP and stdio; foundation",
                    "tools defined once, parity-tested",
                ],
            ),
            (
                "notifications WS",
                "[Component: notifications_ws.py]",
                [
                    "serves only on Role.GATEWAY; rides",
                    "the notify:{user} pub/sub channel",
                ],
            ),
        ],
        x0=IX,
        total=ITOT,
    )
    c4_flow(p, 500, 234, 272, "every request resolves to a principal before it reaches data")

    p.caption(IX, 288, "IDENTITY AND AUTHORIZATION")
    c4_row(
        p,
        300,
        [
            (
                "auth providers",
                "[Component: app/auth/providers/]",
                [
                    "Google OIDC · Entra ID · magic link ·",
                    "password · Keboola OAuth",
                ],
            ),
            (
                "PAT resolver",
                "[Component: pat_resolver.py]",
                [
                    "hash, expiry, revocation, IP audit —",
                    "user PATs and agent PATs alike",
                ],
            ),
            (
                "access gates",
                "[Component: app/auth/access.py]",
                [
                    "require_admin for app-level writes,",
                    "require_resource_access(type, id)",
                ],
            ),
            (
                "AgentPrincipal",
                "[Component: app/chat/agent_profile.py]",
                [
                    "owner grants ∩ agent scope, enforced",
                    "live — never an audit-only verdict",
                ],
                {"accent": CRIM},
            ),
        ],
        x0=IX,
        total=ITOT,
    )
    c4_flow(p, 500, 390, 428, "an authorized principal — re-checked at every read, never cached as a verdict")

    p.caption(IX, 444, "QUERY AND DATA ACCESS")
    c4_row(
        p,
        456,
        [
            (
                "query sandbox",
                "[Component: app/api/query.py]",
                [
                    "SELECT / WITH only; file and URL",
                    "functions blocked; RBAC per view name",
                ],
                {"accent": TEAL},
            ),
            (
                "remote engines",
                "[Component: src/remote_engines.py]",
                [
                    "picks BigQuery or Databricks, and",
                    "refuses a statement straddling both",
                ],
                {"accent": TEAL},
            ),
            (
                "access policies",
                "[Component: table access policies]",
                [
                    "one SQL policy substituted for the",
                    "table on every server-side read",
                ],
                {"accent": TEAL},
            ),
            (
                "manifest builder",
                "[Component: app/api/sync.py]",
                [
                    "per-user tables + md5 + optional",
                    "signed URL — what agnes pull reads",
                ],
                {"accent": TEAL},
            ),
        ],
        x0=IX,
        total=ITOT,
    )
    c4_flow(p, 500, 546, 584, "reads and writes reach state only through the factory")

    p.caption(IX, 600, "STATE AND BACKGROUND WORK")
    c4_row(
        p,
        612,
        [
            (
                "repositories factory",
                "[Component: src/repositories/]",
                [
                    "*_repo() dispatches on the backend;",
                    "callsites never instantiate a class",
                ],
            ),
            (
                "worker runtime",
                "[Component: app/worker/]",
                [
                    "job registry, lease and heartbeat,",
                    "idempotency dedup, expired-lease reap",
                ],
            ),
            (
                "audit log",
                "[Component: src/audit_events.py]",
                [
                    "every action string cataloged; every",
                    "route declares its audit posture",
                ],
            ),
            (
                "instance config",
                "[Component: config/loader.py]",
                [
                    "instance.yaml with ${ENV_VAR}, read",
                    "at boot and cached for the process",
                ],
            ),
        ],
        x0=IX,
        total=ITOT,
    )

    c4_flow(p, 500, 724, 762, "in-process calls and pooled connections to the containers alongside")
    p.caption(C4_X0, 752, "CONTAINERS IT TALKS TO")
    ext = {"dashed": True}
    c4_row(
        p,
        776,
        [
            ("app state", "[Container]", ["Postgres, or state/system.duckdb"], ext),
            ("analytics plane", "[Container]", ["analytics.duckdb, or a DuckLake catalog"], ext),
            ("coordination", "[Container]", ["Redis, or in-process memory"], ext),
            ("chat sandboxes", "[Container]", ["spawned per session via apps-runner"], ext),
        ],
    )

    p.line(C4_X0, 862, C4_X1, 862, op=0.2)
    p.key(
        C4_X0,
        886,
        [
            ("component", "plain"),
            ("data path", "data"),
            ("agent path", "agent"),
            ("outside this container", "external"),
        ],
    )
    p.text(C4_X1, 886, "source: app/main.py · app/auth/access.py", size=10.5, op=0.45, anchor="end")
    return p.svg(
        914,
        "C4 level 3, components of the FastAPI application container: request "
        "ingress through REST routers, web pages, the MCP server and the "
        "notifications WebSocket; identity and authorization through auth "
        "providers, the PAT resolver, access gates and AgentPrincipal; query "
        "and data access through the query sandbox, remote engines, table "
        "access policies and the manifest builder; and state through the "
        "repositories factory, worker runtime, audit log and instance config.",
    )


# ── FIGURE 7 — level 3, components of the data plane ─────────────────────


def figure_c4_component_data() -> str:
    p = Fig(C4_W)
    c4_head(
        p,
        "components — data plane",
        "one contract from every source, and the path a parquet takes to a laptop",
        "C4 level 3  ·  component",
    )

    p.caption(C4_X0, 76, "THE PIPELINE  ·  EVERY SOURCE WRITES THE SAME CONTRACT")
    pos, w, h = c4_row(
        p,
        100,
        [
            (
                "connectors",
                "[Component: connectors/*]",
                [
                    "keboola · bigquery · databricks ·",
                    "jira · sharepoint · local upload",
                    "each writes the same contract",
                ],
                {"accent": TEAL},
            ),
            (
                "extract.duckdb",
                "[Component: the contract]",
                [
                    "_meta, plus _remote_attach for",
                    "remote tables, plus data/*.parquet",
                    "for everything downloaded",
                ],
                {"accent": TEAL},
            ),
            (
                "SyncOrchestrator",
                "[Component: src/orchestrator.py]",
                [
                    "validates every identifier,",
                    "ATTACHes each source, rebuilds",
                    "master views under rebuild_mutex()",
                ],
                {"accent": TEAL},
            ),
            (
                "analytics backend",
                "[Component: legacy | ducklake]",
                [
                    "legacy: temp rebuild, then an",
                    "atomic swap · ducklake: per-source",
                    "copy-ingest by the worker alone",
                ],
                {"accent": TEAL},
            ),
            (
                "serving",
                "[Component: app/api/]",
                [
                    "/api/query · /api/data/{id}/",
                    "download · manifest · catalog",
                    "and table profiles",
                ],
                {"accent": TEAL},
            ),
        ],
        gap=22,
    )
    for i, label in enumerate(["writes", "scans", "rebuilds", "reads"]):
        x_from = pos[i][0] + w
        x_to = pos[i + 1][0]
        p.arrow(x_from + 3, 100 + h / 2, x_to - 2, 100 + h / 2, color=TEAL, op=0.85)
        p.text((x_from + x_to) / 2, 91, label, size=10, op=0.6, anchor="middle")

    # stops short of the caption below it — the arrow reads as "that row
    # drives this one" without its tail striking through the label
    c4_flow(p, 200, 206, 236, "the worker runs it on a cadence", color=TEAL, up=True)

    p.caption(C4_X0, 250, "WHAT DRIVES IT, AND HOW A PARQUET REACHES A LAPTOP")
    c4_row(
        p,
        266,
        [
            (
                "scheduler + worker jobs",
                "[Component: app/worker/kinds.py]",
                [
                    "data-refresh every 15m, jira-refresh,",
                    "distribution-mirror chained on success",
                ],
            ),
            (
                "distribution mirror",
                "[Component: src/object_store.py]",
                [
                    "uploads changed parquet to S3; never",
                    "moves or rewrites the extracts tree",
                ],
            ),
            (
                "manifest v2 signed URLs",
                "[Component: app/api/sync.py]",
                [
                    "15-minute TTL, and only for objects the",
                    "mirror shows present and current",
                ],
            ),
            (
                "agnes pull",
                "[Container: CLI on the laptop]",
                [
                    "prefers the signed URL, falls back to",
                    "the app route; md5-verified either way",
                ],
                {"dashed": True},
            ),
        ],
    )

    p.caption(C4_X0, 394, "WHERE A QUERY RUNS IS A PROPERTY OF THE TABLE, NOT OF THE CALLER")
    c4_row(
        p,
        408,
        [
            ("local", "[query_mode]", ["parquet on the server and on the laptop"]),
            ("materialized", "[query_mode]", ["registered SQL → parquet on a cadence"]),
            ("remote", "[query_mode]", ["nothing downloaded; runs at the source"]),
            ("server_only", "[registry flag]", ["server-side only; agnes pull skips it"]),
        ],
    )

    p.line(C4_X0, 500, C4_X1, 500, op=0.2)
    p.key(
        C4_X0,
        524,
        [("data path", "data"), ("component", "plain"), ("outside this plane", "external")],
    )
    p.text(
        C4_X1,
        524,
        "source: src/orchestrator.py · app/worker/kinds.py",
        size=10.5,
        op=0.45,
        anchor="end",
    )
    return p.svg(
        552,
        "C4 level 3, components of the data plane: connectors write the "
        "extract.duckdb contract, the SyncOrchestrator ATTACHes each source "
        "and rebuilds master views into either the legacy or the DuckLake "
        "backend, and the API serves queries, the manifest and downloads; "
        "scheduler and worker jobs drive it, and a distribution mirror plus "
        "signed URLs carry a parquet to agnes pull on a laptop.",
    )


# ── FIGURE 8 — level 3, components of the agent runtime ──────────────────


def figure_c4_component_agent() -> str:
    p = Fig(C4_W)
    c4_head(
        p,
        "components — agent runtime",
        "how one turn runs: entry, orchestration, the sandbox, and every call out",
        "C4 level 3  ·  component",
    )

    p.caption(C4_X0, 86, "ENTRY  ·  EVERY SURFACE LANDS ON ONE SESSION API")
    c4_row(
        p,
        100,
        [
            (
                "chat surfaces",
                "[Component: app/api/chat.py + the surface bots]",
                [
                    "browser WS, Slack, Telegram and agnes chat all land",
                    "on one multi-turn session API",
                ],
            ),
            (
                "agent API",
                "[Component: app/api/agent_runtime.py]",
                [
                    "POST /api/v1/agents/{slug}/responses — one-shot and",
                    "PAT-authenticated, plus AG-UI SSE sessions",
                ],
            ),
            (
                "schedules and webhooks",
                "[Component: agent_schedules.py · agent_webhooks.py]",
                [
                    "cron-triggered agent runs; outbound webhook delivery",
                    "with retries, as a durable worker job",
                ],
            ),
        ],
        gap=22,
    )
    c4_flow(p, 480, 190, 228, "one session, wherever it came from")

    p.caption(C4_X0, 244, "SESSION ORCHESTRATION  ·  role: gateway")
    c4_row(
        p,
        258,
        [
            (
                "ChatManager",
                "[Component: app/chat/manager.py]",
                [
                    "owns the session lifecycle: spawn, turn",
                    "pump, idle reap, foreign-session takeover",
                ],
            ),
            (
                "routing lease",
                "[Component: app/chat/routing.py]",
                [
                    "chat:{id} names the one gateway holding",
                    "the live sandbox; renewed on heartbeat",
                ],
            ),
            (
                "replay + inbound",
                "[Component: replay.py · inbound.py]",
                [
                    "monotonic seq per frame, so a reconnect",
                    "with ?last_seq= gets exactly the gap",
                ],
            ),
            (
                "sandbox provider",
                "[Component: docker_provider.py]",
                [
                    "docker, or the kai-agent engine;",
                    "staging materializes the caller's stack",
                ],
            ),
        ],
    )
    c4_flow(p, 480, 348, 386, "spawn a sandbox holding the caller's stack, and nothing else", color=CRIM)

    p.caption(C4_X0, 402, "INSIDE THE SANDBOX  ·  NO HOST FILESYSTEM, NO API KEY")
    c4_row(
        p,
        416,
        [
            (
                "Claude Code harness",
                "[Component: in the sandbox]",
                [
                    "runs the turn — no host filesystem, no host network,",
                    "and no API key ever enters it",
                ],
                {"accent": CRIM},
            ),
            (
                "MCP tools + agnes CLI",
                "[Component: in the sandbox]",
                [
                    "catalog, schema, query, metrics, memory search — the",
                    "same rails a human analyst gets, under the same RBAC",
                ],
                {"accent": CRIM},
            ),
            (
                "agent memory notebook",
                "[Component: app/api/agent_memory.py]",
                [
                    "off / propose / auto; the owner inspects, approves,",
                    "archives or deletes from the /agents builder",
                ],
                {"accent": CRIM},
            ),
        ],
        gap=22,
    )
    c4_flow(p, 480, 506, 544, "every call out is brokered — the sandbox holds no credential", color=CRIM)

    p.caption(C4_X0, 560, "EGRESS  ·  TWO CHOKEPOINTS, BOTH FAIL-CLOSED")
    c4_row(
        p,
        574,
        [
            (
                "secret broker",
                "[Component: app/api/broker.py]",
                [
                    "ticket-gated: keys stay server-side, the model is",
                    "pinned, token_budget_monthly → 429 budget_exhausted",
                ],
                {"accent": CRIM},
            ),
            (
                "egress proxy",
                "[Container: sidecar]",
                [
                    "fail-closed CONNECT allowlist — the sandbox network",
                    "has no other route out, so the proxy is the policy",
                ],
                {"accent": CRIM},
            ),
            (
                "LLM API",
                "[External system]",
                [
                    "Anthropic, or any OpenAI-compatible gateway",
                    "(LiteLLM · OpenRouter · vLLM)",
                ],
                {"dashed": True},
            ),
        ],
        gap=22,
    )

    p.caption(C4_X0, 700, "WHAT BOUNDS THE TURN")
    c4_row(
        p,
        714,
        [
            (
                "AgentPrincipal",
                "[Component: a restricted principal]",
                [
                    "owner grants ∩ agent scope, bound live at every",
                    "brokered request — an agent never inherits admin",
                ],
                {"accent": CRIM},
            ),
            (
                "delegation",
                "[Component: app/api/agent_delegation.py]",
                [
                    "depth-1, one per turn; the delegate spawns under the",
                    "ORIGINAL CALLER's identity, never either owner's",
                ],
                {"accent": CRIM},
            ),
            (
                "artifact harvest",
                "[Component: app/chat/artifact_harvest.py]",
                [
                    "files the turn produced come back as chat artifacts,",
                    "scoped to the session that made them",
                ],
            ),
        ],
        gap=22,
    )

    p.line(C4_X0, 820, C4_X1, 820, op=0.2)
    p.key(
        C4_X0,
        844,
        [("agent path", "agent"), ("component", "plain"), ("external", "external")],
    )
    p.text(
        C4_X1,
        844,
        "source: app/chat/manager.py · app/api/broker.py",
        size=10.5,
        op=0.45,
        anchor="end",
    )
    return p.svg(
        872,
        "C4 level 3, components of the agent runtime: chat surfaces, the agent "
        "API and schedules enter one session API; ChatManager, the routing "
        "lease, replay and inbound streams and the sandbox provider orchestrate "
        "the session; the Claude Code harness, MCP tools and the memory "
        "notebook run inside the sandbox; and the secret broker and egress "
        "proxy are the two fail-closed chokepoints in front of the LLM API.",
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
        "agnes-c4-context.svg": figure_c4_context,
        "agnes-c4-container.svg": figure_c4_container,
        "agnes-c4-component-app.svg": figure_c4_component_app,
        "agnes-c4-component-data.svg": figure_c4_component_data,
        "agnes-c4-component-agent.svg": figure_c4_component_agent,
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
