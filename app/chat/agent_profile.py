"""DB-sourced agent persona profile + spawn-time scope snapshot (Task 7).

Bridges the owner-scoped `agents` repository (`src/repositories/agents.py`,
v96 schema) into the existing spawn-time `ChatProfile` mechanism
(`app/chat/profiles.py`). Two independent pieces:

1. ``build_profile`` turns an agent row into a dynamic ``ChatProfile`` —
   the same frozen dataclass the static authoring-agent profiles use — so
   ``ChatManager._spawn_live`` can materialize a persona `CLAUDE.md` +
   read-only knowledge skill into the session workdir exactly like it does
   for the hand-authored profiles in ``app/chat/profiles.py``. Returns
   ``None`` when the agent has no (non-whitespace) ``system_prompt`` — the
   seeded default agent always falls in this bucket, so a web chat session
   bound to it keeps today's generic data-analyst rails bit-for-bit.

2. ``compute_effective_scope`` / ``record_snapshot`` compute the agent's
   effective scope (which plugins/connections/tables/memory domains it is
   allowed to touch, per its four `*_mode` columns) and persist it as an
   audit row via ``agents_repo().record_scope_snapshot``.

   **This snapshot is an audit trail, not the enforcement mechanism — but as
   of V1d it now describes what IS actually enforced.** V1a shipped this
   module computing + recording the scope with no live seam honoring it
   (a gap closed in V1d, not V1b as originally planned — see
   ``docs/superpowers/specs/2026-07-25-agent-scope-live-enforcement-design.md``).
   Live enforcement lives elsewhere: the broker mints an ``agent_session``
   JWT for a narrowing agent (``app/api/broker.py::_mint_identity_jwt``),
   the resolver turns it into an ``AgentPrincipal`` whose ``intersection`` is
   ``resolve_agent_authority(agent_id)`` — the agent's OWN resolved
   authority per ``agent_scope.granted_by`` (C2.2's D-C2 resolution;
   ``src/agent_scope_intersection.py``, recomputed live per request) — and
   the tables/marketplace/MCP seams honor that principal directly. This
   module's snapshot and that live resolution are computed from the same
   inputs (``agent_row`` + ``agent_scope`` rows) via the same mode→type
   mapping, so they cannot drift out of agreement — see
   ``tests/test_agent_scope_e2e.py::test_audit_snapshot_matches_enforced_intersection``.

``record_snapshot`` must never raise into the spawn path: a scope-snapshot
write failure is an audit-trail gap, not a reason to fail the chat spawn
the user is waiting on. All internals are wrapped in try/except with
``logger.exception`` so a repo error (or a malformed agent row) is logged
and swallowed.

**Snapshot growth note:** ``record_snapshot`` skips the write entirely for
the default agent when all four scope modes are ``'all'`` — the seeded
default agent's baseline shape. That row's effective scope is fully
derivable from its ``*_mode`` columns alone (``{"plugins": "all", ...}``),
so persisting one identical snapshot per web-chat spawn would otherwise
accrue an unbounded, redundant `agent_scope_snapshots` row per session for
every single user — O(spawns), not O(scope changes) — with zero audit
value. Any deviation from that all-'all' shape (a non-default agent, or a
defensively-possible 'selected' mode on a default row) still gets a row,
since that *does* carry information worth auditing.

3. ``materialize_memories`` (V1c Task 3) writes an agent's active memories
   into the session workdir *before* spawn — the same host-dir-then-
   uploaded seam ``build_profile``'s persona takes (``ChatManager.
   _spawn_live`` calls it right before ``_spawn_runner``, which is what
   actually uploads ``session_dir`` into the remote sandbox). This is the
   read side of agent memory; the write side is the remember tool (V1c
   Task 4).

   **Precedence note (important):** ``agent_memories_repo().list_active``
   returns memories newest-first, and ``select_in_budget`` consumes them
   in that order, packing as many as fit under ``_MEMORY_BUDGET_CHARS``.
   A memory's "active" status therefore does NOT guarantee it is actually
   "in effect" for a given spawn — if the active set exceeds the budget,
   older active memories (and, in principle, a just-approved one sitting
   behind enough older-but-still-active content) are silently shadowed for
   that spawn. ``select_in_budget`` returns both halves precisely so a
   management surface (V1c Task 5) can show admins which active memories
   are in-budget vs shadowed, instead of that distinction only being
   visible by reading generated sandbox files.

``materialize_memories`` must never raise into the spawn path, for the
same reason as ``record_snapshot`` — see below.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.chat.profiles import ChatProfile

logger = logging.getLogger(__name__)

# ~6000 tokens at a conservative 4 chars/token — the active-memory budget
# materialized into a spawned session's workdir. See the module docstring's
# "Precedence note" for what happens when the active set exceeds this.
_MEMORY_BUDGET_CHARS = 6000 * 4

#: Platform data-access floor appended to every persona.
#:
#: A persona REPLACES the workspace ``CLAUDE.md`` in the session workdir
#: (``WorkdirManager._materialize_profile`` writes it as a real file instead
#: of symlinking the workspace one) — that replacement is what makes an
#: agent an agent rather than a themed analyst, and it stays. What nobody
#: authoring a persona expects is that the analyst *data rails* go with it:
#: the sandbox still has the ``agnes`` CLI on PATH, the Agnes MCP server,
#: and every passthrough tool the agent's scope exposes, but nothing left
#: tells it that the organization's data lives behind ``agnes catalog``.
#: The observed failure is an agent that goes hunting through whatever other
#: MCP servers it can see and has to be told "use Agnes" by hand — on every
#: surface that spawns a sandbox (web chat, Slack, ``agnes chat``, and the
#: one-shot agent API, where there is no human in the loop to say it).
#:
#: So a persona overrides the rails' *tone and task*, never their existence.
#: Deliberately short: a pointer to the discovery commands, not a copy of
#: the analyst prompt (``config/claude_md_template.txt``), which would
#: drown the persona it is appended to. Depth stays one command away.
#:
#: Appended, not prepended, so the authored opening ("You are ...") keeps
#: the first word on the agent's identity.
DATA_ACCESS_RAILS = """

---

## Data access (Agnes platform)

Agnes adds this section to every agent; it holds regardless of the persona
above.

Your organization's data lives in Agnes and is reachable **only** through
the `agnes` CLI and the equivalent Agnes MCP tools. Do not look for it in
other MCP servers, in local files, or in your training data.

Before answering anything that depends on that data:

```
agnes catalog                 # which tables exist — the source of truth
agnes schema <table>          # columns + types, in the right SQL dialect
agnes describe <table> -n 5   # sample rows (local + materialized tables)
agnes query "SELECT ..."      # run the query
```

- Never enumerate tables, columns, or metrics from memory. The catalog
  changes as admins register, migrate, and drop entries, and it is filtered
  to what *you* are allowed to see.
- Before computing a business metric, read its canonical definition:
  `agnes catalog --metrics`, then
  `agnes catalog --metrics --show <category>/<name>`. Never invent one.
- An empty catalog means you hold no data grants — say so plainly instead
  of substituting another source.
- Full querying protocol (remote tables, snapshots, scan-cost limits):
  `agnes skills show agnes-data-querying`.
"""

#: Fact-graph rails appended after :data:`DATA_ACCESS_RAILS` — ONLY when the
#: `facts` feature switch is on for this instance.
#:
#: `config/claude_md_template.txt` carries the equivalent
#: "Facts — entity and relationship questions" section, but that template IS
#: the ``CLAUDE.md`` a persona replaces (see :data:`DATA_ACCESS_RAILS`'s own
#: docstring for why the replacement itself is correct and stays). Without
#: this, a persona'd agent silently lost the ONLY text that ever told it to
#: reach for `fact_search`/`fact_neighbors`/`fact_claims` on a who/what/
#: relationship question — it fell back to `agnes catalog` + SQL, found no
#: table shaped like the answer, and reported no data instead of walking the
#: fact graph, while an agent with no persona (the default rails, unaltered)
#: answered the same question correctly. That divergence is what made the
#: product look random rather than genuinely lacking the data.
#:
#: Gated on the switch alone, deliberately not on the caller's actual fact
#: read access: unlike ``DATA_ACCESS_RAILS`` — which just names a CLI, not a
#: caller-specific slice of it — checking readability here would mean a
#: repository call on every persona spawn for a fact that only teaches WHEN
#: to try the tools; the tools (`facts_repo()`) already enforce per-caller
#: visibility on every call regardless of what this text says. Same
#: reasoning `src/claude_md.py::_facts_enabled` documents for the default
#: template's own gate — this mirrors it rather than diverging.
FACTS_ACCESS_RAILS = """

---

## Facts — entity and relationship questions

Agnes also extracts typed facts (people, clients, organizations, and the
relationships between them) into a queryable graph. For a question about
**who, what, which entity, or how things relate** — "who owns X", "how do
these two people connect", "which clients per industry" — reach for the
fact tools FIRST, before writing SQL or searching documents by keyword:
`fact_search` -> `fact_neighbors` / `fact_edges` -> `fact_claims` (the same
calls as `agnes facts search|neighbors|edges|claims` on the CLI). For a
question about ALL relationships of one type ("which organizations own
which companies", "which industries are our clients in"), read the
relationship names from `fact_type_map` and call `fact_edges` ONCE with
`include_claims=1` — not `fact_neighbors` once per entity.

```
agnes facts search <type> [query] [--filter key=value]   # find subjects by type, an optional name, and/or attrs
agnes facts neighbors <subject_id>                        # traverse relationships from ONE subject (depth <= 2)
agnes facts edges <edge_type> [--extend <type>] [--claims 1]  # EVERY relationship of one type, both ends, cited
agnes facts claims <subject_id>                           # the evidencing quotes (newest first) for one subject
```

Cite every fact you use — `--claims`/`include_claims` gives you the quote
inline, `agnes facts claims` gives the rest; name the quote and its source
document in your answer. Facts are filtered
server-side to what YOU can read; a search returning nothing may exist
outside your access, it is not evidence the fact is absent — fall back to
`agnes collections search` rather than inventing an answer or refusing
outright.
"""

#: File-delivery rails appended after :data:`DATA_ACCESS_RAILS`, ungated.
#:
#: Same failure shape as :data:`FACTS_ACCESS_RAILS`, one section over:
#: `config/claude_md_template.txt` carries the "Files you produce" section
#: that names ``outputs/`` as the ONE directory a deliverable can reach the
#: user from — and a persona REPLACES that template wholesale. So a persona'd
#: agent asked for a ``.docx`` had no text telling it where to write one, and
#: none of the "never disclaim the handover" rule either; the observed
#: behavior (#1975) was an agent writing to ``/tmp`` or a skill directory and
#: then telling the user that this sandbox has no download channel and they
#: should copy markdown into their own editor — while the chat's Files panel
#: sat beside the conversation waiting for a file that was never written
#: anywhere it could see.
#:
#: Ungated on purpose, unlike the facts rails: ``outputs/`` is not a feature
#: switch but the platform's one delivery convention, shared by the chat
#: files channel (``app/api/chat_session_files.py``) and the agent API's
#: artifact harvest (``app/chat/artifact_harvest.py``, which scans exactly
#: ``{workdir}/outputs``). Every surface that spawns a persona'd agent honors
#: it, so there is no instance where this text would steer an agent at
#: something that does not exist.
#:
#: The closing paragraph is not padding: an agent that overclaims ("click the
#: download button below") is the mirror-image bug, and it is the likelier
#: one once the agent knows a panel exists at all. Only web chat draws the
#: file beside the conversation; a Slack thread carries a Continue-on-web link
#: to the same session's drawer, and an `api` session (``agnes chat``, the
#: one-shot agent API) has its ``outputs/`` harvested to
#: ``GET /api/v1/sessions/{id}/artifacts`` — reachable on every surface, shown
#: inline on exactly one. So this text says where to WRITE and never how the
#: reader will get it, which is the only claim true everywhere; a
#: surface-conditional block would have to drop the never-disclaim rule on
#: Slack and the CLI, and that rule is the actual defect in #1975.
FILE_DELIVERY_RAILS = """

---

## Files you produce

Agnes adds this section to every agent; it holds regardless of the persona
above.

A **document** — a `.docx`, a `.pptx`, an `.xlsx`, a PDF, a CSV export —
reaches the user as a file, and where you write it decides whether it reaches
them at all: write it to **`outputs/`**, relative to your working directory,
under a descriptive filename. Create the directory if it isn't there.

`outputs/` is the one directory Agnes collects deliverables from. A file
written anywhere else stays in this sandbox: `.claude/` (skill directories
included), `/tmp`, or a bare filename in the working directory are all
invisible — however well the file itself rendered. A skill whose scaffolds
live in `.claude/skills/<name>/` must still write its *output* to `outputs/`.

```
outputs/q3-revenue.xlsx      <- they get this
.claude/skills/deck/out.pptx <- they never see it
```

A **chart** is the one exception: it belongs inside your reply as inline SVG,
not in `outputs/` — a picture the user has to open in another window is not an
answer.

**Never disclaim the handover.** Writing the file to `outputs/` is your whole
part of it. Do not tell the user that this filesystem is not their machine,
that you cannot produce a downloadable file, that there is no way to hand one
over here, that they need Claude Code or the Agnes CLI to fetch it, or that
they should copy your answer into their own editor instead — each of those is
false, and each sends the reader hunting for a problem that does not exist.

Do not overclaim in the other direction either. How the file then reaches the
reader is the surface's job, not yours, and it differs: web chat lists it
beside the conversation, other surfaces collect it and hand it over their own
way. You cannot see which, so do not promise a download button, do not say
where to click, and do not claim it is already in front of them. Name what you
wrote and where: "The deck is ready as `outputs/q3-review.pptx`" is the whole
job — true on every surface, and it needs no caveat on either side.
"""


#: Provenance rails appended after :data:`FILE_DELIVERY_RAILS`, ungated.
#:
#: Third instance of the same gap: the "Say where every number came from"
#: section of `config/claude_md_template.txt` is the ONLY text that asks for
#: the ```sources trailer — the `table:` claims the server checks against the
#: turn's tool calls, and the `assumption:` lines whose `origin:`/`why:`
#: segments the chat renders as badges and rationale (TCRD-289) — and a
#: persona REPLACES that template wholesale. So a persona'd agent's figures
#: rendered as "Sources — none declared" and its assumptions never reached
#: the reader at all, while the default agent, one prompt over, was held to
#: the product's promise on every answer. (Devin Review on #2047.)
#:
#: Ungated for the same reason the file rails are: the renderer, the verdict
#: and the push-sink strip (`app/chat/sources.py`) run on every surface a
#: persona'd agent can answer from, so there is no instance where this text
#: steers an agent at machinery that does not exist. The origin vocabulary is
#: the server's (`ASSUMPTION_ORIGINS`), pinned by
#: `tests/test_chat_sources_verdict.py` alongside the template's copy — one
#: contract, three carriers, none allowed to drift.
PROVENANCE_RAILS = """

---

## Say where every number came from

Agnes adds this section to every agent; it holds regardless of the persona
above.

An answer that reports a figure ends with a fenced `sources` block — one
claim per line, the LAST thing in the reply:

    ```sources
    table: hr_headcount
    metric: headcount/active
    glossary: Full-time equivalent
    document: 2026_Workforce_Plan.pdf
    assumption: active employees only | origin: user | why: you asked about "the team"
    assumption: contractors excluded | origin: definition | why: headcount/active counts employees only
    ```

- `table:` — the registry id (as `agnes catalog` gives it) of every table the
  figure was computed from. `metric:` — the canonical metric id, when you
  adapted one. `glossary:` — every governed business term whose definition the
  answer leaned on, as `agnes glossary` gives it. `document:` — every document
  or fact-graph subject the answer rests on: the filename as the fact tools
  give it, or the subject id you passed to `agnes facts claims`. Each is
  checked against the tools you actually ran; naming a table you did not
  query, a term you did not look up, or a file you did not open, is worse
  than naming none. An answer read entirely out of documents declares
  `document:` lines and no `table:` — that is a complete answer to "where did
  this come from", not a gap.
- `assumption:` — anything the number depends on that you chose rather than
  read, one per line, and never a source: a file you read is a `document:`
  and a term you looked up is a `glossary:`, and filing either here shows the
  reader your evidence among your guesses. Always with two more segments
  separated by ` | `:
  `origin:` is ONE word from `user` (the question said or implied it),
  `definition` (a metric definition, a semantic model or a document in the
  knowledge base says so), `data` (the data forced it: a missing column or
  value, so a proxy or a subset stood in), `judgment` (your own choice, with
  nothing behind it); `why:` is one short sentence a reader could check. An
  assumption without them is shown to the reader as "origin not stated".

The chat lifts this block out of your reply and renders it as provenance next
to the answer; it never appears as text. Never report a number whose origin
you cannot name — when no tool call backs a figure, say so in the answer.
"""


def _facts_rails_enabled() -> bool:
    """Whether this instance has the `facts` feature switched on — the sole
    gate for appending :data:`FACTS_ACCESS_RAILS`. See that constant's
    docstring for why this stays switch-only with no RBAC narrowing."""
    from app.instance_config import feature_enabled

    return feature_enabled("facts", "enabled", env_var="AGNES_FACTS_ENABLED", default=False)


def _semantic_layer_section(user_email: str | None) -> str:
    """The same minimal semantic-layer pointer the sandbox/CLAUDE.md path
    gets (``src.claude_md``'s "## Semantic layer" section), condensed for a
    persona: slug + description + the model author's own truncated
    ``ai_context.instructions``, no full metric/glossary dump — a named
    agent profile REPLACES the workspace CLAUDE.md the same way a persona
    replaces it natively (see :data:`DATA_ACCESS_RAILS`), so without this a
    named agent (persona web chat, Slack, ``agnes chat``, the one-shot agent
    API) got zero semantic context while the sandbox path always has.

    Empty string — never raises, never appended — when ``user_email`` is
    unset, the user can't be resolved, no readable model exists, or
    anything else goes wrong: this is a nice-to-have addition to a persona
    prompt, not a reason to fail a spawn the user is waiting on.

    Opens its own DuckDB connection when the active backend is DuckDB
    (Postgres reads need none) — the same ``conn = None if use_pg() else
    get_system_db()`` pattern ``app/main.py``'s workspace-prompt renderer
    uses. This module otherwise opens no connection of its own, so this
    call is why it carries a ``get_system_db()`` grandfather entry in
    ``tests/test_backend_split_guard.py``.
    """
    if not user_email:
        return ""
    conn = None
    try:
        from src.claude_md import _semantic_layer_models
        from src.repositories import use_pg, users_repo

        u = users_repo().get_by_email(user_email)
        if not u:
            return ""
        if not use_pg():
            from src.db import get_system_db

            conn = get_system_db()
        models = _semantic_layer_models(conn, user=u)
        if not models:
            return ""
        lines = [
            "\n---\n\n"
            "## Semantic layer\n\n"
            "This instance has at least one semantic model you can read — a "
            "structured document of datasets, metrics, relationships, and "
            "constraints that is the authoritative source of business "
            "meaning here. Prefer its definitions over inferring meaning "
            "from table or column names.\n\n"
            "Registered models:\n"
        ]
        for m in models:
            line = f"- `{m['slug']}`"
            if m.get("description"):
                line += f" — {m['description']}"
            lines.append(line + "\n")
            if m.get("instructions"):
                lines.append(f"  - Model author's note about this data: {m['instructions']}\n")
        # Discovery guidance is deliberately economy-first. An agent that
        # finds the layer without it issues one lookup per object, and every
        # one of those payloads then sits in the conversation for the rest of
        # the session — reading the layer is cheap, reading it one object at a
        # time and re-reading it later is not.
        lines.append(
            "\nDiscover more in ONE call, then work from what you read:\n"
            "- `agnes semantic-model context dataset metric relationship` — "
            "the whole layer, compact (or the MCP `get_semantic_context` tool "
            "with the same list).\n"
            "- `agnes semantic-model context metric --id <a> --id <b>` — full "
            "detail for several objects at once. A call per object multiplies "
            "both round trips and the context every later turn carries, and a "
            "definition you have already read is still valid.\n"
            "- Always check a query against the layer first with `agnes "
            'semantic-model validate-query "<SQL>"` — it catches a constraint '
            "violation (an excluded order state, a wrong grain) and a dialect "
            "mismatch before the query hands you a confidently wrong number.\n"
        )
        return "".join(lines)
    except Exception:
        logger.exception("semantic layer section unavailable for agent persona (user=%s)", user_email)
        return ""
    finally:
        if conn is not None:
            conn.close()


# agents.<field>_mode -> (scope key, agent_scope.item_type)
_MODE_FIELD_TO_SCOPE = {
    "plugins_mode": ("plugins", "plugin"),
    "connections_mode": ("connections", "connection"),
    "tables_mode": ("tables", "table"),
    "memory_mode": ("memory_domains", "memory_domain"),
}


def _context_skill(agent_row: dict, *, advertise_memory_write: bool = True) -> str:
    """Render the small read-only SKILL.md describing this agent's identity.

    States the agent's name/description and that its capability is scoped
    by the owner's config. This skill body is descriptive text materialized
    into the sandbox, not the enforcement mechanism (that is the live
    ``AgentPrincipal`` intersection at the broker/RBAC seams — see the
    module docstring); it stays deliberately generic rather than
    enumerating the agent's actual scope, so a stale/forged copy sitting in
    an already-spawned sandbox can never claim more than the live seams
    would allow anyway.

    Also advertises the "remember" write tool (V1c Task 4,
    `POST /api/v1/sessions/{id}/memories`) — but ONLY when this agent's
    `memory_write_mode` is not `'off'` AND the caller left
    ``advertise_memory_write`` on. The endpoint enforces the mode
    regardless of what this text says (a stale/forged skill body can never
    grant a write `off` denies), but a well-behaved agent should never even
    attempt a call it knows is disabled — and telling an `off` agent about a
    tool it cannot use would just invite a wasted/failed call.
    ``advertise_memory_write=False`` is for sandboxes that have no channel
    to the endpoint at all (the embedded kai-agent engine: no ``agnes-api``
    broker scope and no ``$AGNES_SERVER``/``$AGNES_SESSION_ID`` env), where
    the curl recipe below would only ever fail.

    Includes a concrete curl invocation against `$AGNES_SERVER` +
    `$AGNES_SESSION_ID` — the two env vars `app/chat/runner.py` sets in the
    sandbox (`AGNES_SERVER` rewritten to the loopback relay, per
    `_spawn_runner`; `AGNES_SESSION_ID` forwarded as-is) — since a bare route
    path with no host or id source isn't actually callable from in-sandbox.
    There is no `agnes` CLI subcommand for this by design (M2).
    """
    name = agent_row.get("name") or agent_row.get("slug") or "agent"
    description = (agent_row.get("description") or "").strip()
    slug = agent_row.get("slug") or "agent"
    body_description = description or f"Context for the '{name}' agent."
    lines = [
        "---\n",
        "name: agnes-agent-context\n",
        f"description: Identity and scope context for the '{name}' agent "
        f"({slug}) — use when you need to know who you are and what you're "
        "allowed to touch.\n",
        "---\n\n",
        f"# {name}\n\n",
        f"{body_description}\n\n",
        "This agent's capability (which plugins, connections, tables, and "
        "memory domains it may use) is scoped by its owner's configuration "
        "in Agnes, not by this file.\n",
    ]
    # The remember endpoint lives on the flag-guarded agent_memory router, so
    # with agent profiles disabled every call would 403 — treat flag-off the
    # same as memory_write_mode='off': don't advertise a tool the sandbox
    # cannot use (the router guard still enforces regardless of this text).
    from app.instance_config import get_agent_profiles_enabled

    memory_write_mode = agent_row.get("memory_write_mode") or "propose"
    if advertise_memory_write and memory_write_mode != "off" and get_agent_profiles_enabled():
        lines.append(
            "\n## Remember\n\n"
            "You can save a durable note to your own memory notebook by "
            "calling `POST /api/v1/sessions/{session_id}/memories` with "
            '`{"content": "..."}`, using this session\'s own id. '
            + (
                "Writes are reviewed by your owner before they become active (status starts `pending`)."
                if memory_write_mode == "propose"
                else "Writes take effect immediately (status `active`)."
            )
            + "\n\n"
            "Concretely, in this sandbox: the server host is `$AGNES_SERVER` "
            "and your own session id is `$AGNES_SESSION_ID` (both already set "
            "in your environment). For example:\n\n"
            "```bash\n"
            'curl -X POST "$AGNES_SERVER/api/v1/sessions/$AGNES_SESSION_ID/memories" \\\n'
            "  -H 'content-type: application/json' \\\n"
            '  -d \'{"content": "..."}\'\n'
            "```\n"
        )
    return "".join(lines)


def build_profile(
    agent_row: dict, *, advertise_memory_write: bool = True, user_email: str | None = None
) -> ChatProfile | None:
    """Build a dynamic ``ChatProfile`` from an ``agents`` row.

    Returns ``None`` when ``system_prompt`` is empty/whitespace-only — the
    caller (``ChatManager._spawn_live``) must keep today's behavior in that
    case (no profile / the static ``self._session_profiles`` lookup, if
    any), so the default agent (always an empty prompt) never changes web
    chat's generic rails.

    The returned ``claude_md`` is the authored persona followed by
    :data:`DATA_ACCESS_RAILS` — see that constant for why a persona must
    never be able to silently drop the platform's data-access floor — then,
    when the `facts` feature switch is on, :data:`FACTS_ACCESS_RAILS` after
    it (see that constant for why a persona needs its own copy of the
    fact-tool guidance too), then :data:`FILE_DELIVERY_RAILS` (the
    ``outputs/`` handover convention, ungated — see that constant), then
    :data:`PROVENANCE_RAILS` (the ```sources trailer with the assumption
    origin/why contract, ungated — see that constant), and finally
    :func:`_semantic_layer_section`.
    The early return above means this only ever applies where a persona
    actually replaces the workspace prompt; an agent with no persona keeps
    the full symlinked rails — including the template's own facts and
    semantic-layer sections — and is untouched.

    ``advertise_memory_write`` is threaded to :func:`_context_skill` — pass
    ``False`` when the profile is materialized for a sandbox with no channel
    to the remember endpoint (the embedded kai-agent engine's workspace
    tarball, ``app/api/kai.py``).

    ``user_email`` threads into :func:`_semantic_layer_section` for the
    RBAC-filtered model summary — omit it (or pass ``None``) where the
    caller has no session identity handy; the persona still builds, just
    without that section, exactly as before this parameter existed.
    """
    system_prompt = (agent_row.get("system_prompt") or "").strip()
    if not system_prompt:
        return None
    slug = agent_row.get("slug") or agent_row.get("id") or "agent"
    claude_md = system_prompt + DATA_ACCESS_RAILS
    if _facts_rails_enabled():
        claude_md += FACTS_ACCESS_RAILS
    claude_md += FILE_DELIVERY_RAILS
    claude_md += PROVENANCE_RAILS
    claude_md += _semantic_layer_section(user_email)
    return ChatProfile(
        slug=f"agent-{slug}",
        claude_md=claude_md,
        skill_name="agnes-agent-context",
        skill_body=_context_skill(agent_row, advertise_memory_write=advertise_memory_write),
    )


def compute_effective_scope(agent_row: dict, scope_items: list[dict]) -> dict:
    """Map an agent's four scope modes + its ``agent_scope`` rows into
    ``{"plugins": [...] | "all", "connections": [...] | "all",
    "tables": [...] | "all", "memory_domains": [...] | "all"}``.

    ``mode == 'all'`` -> the literal string ``"all"``. ``mode ==
    'selected'`` -> the sorted list of ``item_id`` for that item_type drawn
    from ``scope_items`` (each a ``{"item_type": ..., "item_id": ...}``
    dict, e.g. from ``agents_repo().get_scope``).
    """
    by_type: dict[str, list[str]] = {}
    for item in scope_items:
        item_type = item.get("item_type")
        item_id = item.get("item_id")
        if item_type is None or item_id is None:
            continue
        by_type.setdefault(item_type, []).append(item_id)

    effective: dict[str, Any] = {}
    for mode_field, (scope_key, item_type) in _MODE_FIELD_TO_SCOPE.items():
        mode = agent_row.get(mode_field) or "all"
        if mode == "selected":
            effective[scope_key] = sorted(by_type.get(item_type, []))
        elif mode == "all":
            effective[scope_key] = "all"
        else:
            # Fail CLOSED (treat as empty selection), matching live
            # enforcement (src/agent_scope_intersection.py). The audit
            # view must never disagree with what is actually enforced —
            # an unrecognized mode value means something upstream (a bad
            # migration, a hand-edited row, a future mode this code
            # doesn't know about yet) put the agent in a state this
            # function doesn't understand, and reporting "all" here would
            # make the audit trail lie about a spawn that was in fact
            # denied everything for that scope key.
            logger.warning(
                "agent %s has unrecognized %s=%r — treating as empty (fail closed)",
                agent_row.get("id"),
                mode_field,
                mode,
            )
            effective[scope_key] = []
    return effective


def _is_default_all_scope(agent_row: dict) -> bool:
    """True for the (only) default-agent shape that carries no audit
    information: ``is_default`` truthy and every ``*_mode`` column is
    ``'all'``. See the module docstring's snapshot-growth note."""
    if not agent_row.get("is_default"):
        return False
    return all(agent_row.get(mode_field) == "all" for mode_field in _MODE_FIELD_TO_SCOPE)


def record_snapshot(session_id: str, agent_row: dict) -> None:
    """Compute + persist an audit-only scope snapshot for this spawn.

    Skips the write for the default agent when its scope is fully-'all'
    (see the module docstring's snapshot-growth note) — that case is
    intentionally not an audit gap, just nothing worth recording.

    Never raises — see the module docstring. A failure here (repo error,
    coordination hiccup, malformed row) is logged and swallowed so it can
    never take down the chat spawn that is waiting on this call.
    """
    try:
        if _is_default_all_scope(agent_row):
            return

        from src.repositories import agents_repo

        agent_id = agent_row["id"]
        repo = agents_repo()
        scope_items = repo.get_scope(agent_id)
        effective_scope = compute_effective_scope(agent_row, scope_items)
        repo.record_scope_snapshot(
            id=str(uuid4()),
            session_id=session_id,
            agent_id=agent_id,
            effective_scope=json.dumps(effective_scope, sort_keys=True),
        )
    except Exception:
        logger.exception(
            "agent scope snapshot failed for session %s — spawn continues without an audit row",
            session_id,
        )


def select_in_budget(memories: list[dict], max_chars: int) -> tuple[list[dict], list[dict]]:
    """Split ``memories`` — assumed already newest-first, the order
    ``agent_memories_repo().list_active`` returns — into ``(in_budget,
    shadowed)`` by a cumulative character budget on each memory's
    ``content``.

    Newest-first precedence: memories are consumed in list order: the
    earliest ones that fit land in ``in_budget``; everything after the
    budget is exhausted lands in ``shadowed``, regardless of how relevant a
    later (older) memory might be. See the module docstring's "Precedence
    note" — this is what makes "active" not synonymous with "in effect".
    Reused by both ``materialize_memories`` and the memory-management
    surface (V1c Task 5), which needs to render the same split for admins.
    """
    in_budget: list[dict] = []
    shadowed: list[dict] = []
    used = 0
    for memory in memories:
        content = memory.get("content") or ""
        length = len(content)
        if used + length <= max_chars:
            in_budget.append(memory)
            used += length
        else:
            shadowed.append(memory)
    return in_budget, shadowed


def _memory_date(memory: dict) -> str:
    """Best-effort ``YYYY-MM-DD`` from a memory's ``created_at``, which may
    arrive as a ``datetime``, a ``date``, an ISO string, or (defensively)
    be missing — DuckDB and Postgres don't guarantee the same Python type
    back from the repo layer."""
    value = memory.get("created_at")
    if not value:
        return "unknown-date"
    text = str(value)
    return text[:10] if text else "unknown-date"


def _rendered_memories_with_count(agent_row: dict) -> tuple[str | None, int]:
    """``(document, count)`` for this agent's in-budget active memories —
    ``(None, 0)`` when there is nothing to write. Shared never-raises core of
    :func:`render_memories` and :func:`materialize_memories`."""
    agent_id = agent_row.get("id")
    try:
        if not agent_id:
            return None, 0

        from src.repositories import agent_memories_repo

        memories = agent_memories_repo().list_active(agent_id)
        if not memories:
            return None, 0

        in_budget, _shadowed = select_in_budget(memories, _MEMORY_BUDGET_CHARS)
        if not in_budget:
            return None, 0

        lines = ["# Agent memory\n\n"]
        for memory in in_budget:
            content = (memory.get("content") or "").strip()
            lines.append(f"- **{_memory_date(memory)}** — {content}\n")
        return "".join(lines), len(in_budget)
    except Exception:
        logger.exception(
            "agent memory render failed for agent_id=%s — continuing without memories",
            agent_id,
        )
        return None, 0


def render_memories(agent_row: dict) -> str | None:
    """Render this agent's in-budget active memories as the ``agent-memory.md``
    document, or ``None`` when there is nothing to write.

    The single renderer behind both delivery shapes: the native session
    workdir (:func:`materialize_memories` writes it to
    ``.claude/agent-memory.md``) and the embedded engine's workspace tarball
    (``app/api/kai.py`` packs the same bytes at the same arcname), so the two
    sandboxes cannot drift in what an agent remembers.

    Same never-raises posture as :func:`materialize_memories`: any failure
    (repo error, malformed row) is logged and answered with ``None``.
    """
    return _rendered_memories_with_count(agent_row)[0]


def materialize_memories(agent_row: dict, session_dir: Path) -> int:
    """Write this agent's active memories into the session workdir.

    Called from ``ChatManager._spawn_live`` at the same pre-spawn seam as
    ``build_profile`` — before the provider delivers ``session_dir``
    into the (remote) sandbox. A file written after spawn
    would never reach the agent; see the module docstring.

    Renders via the shared memory renderer (newest-first, capped to
    ``_MEMORY_BUDGET_CHARS`` via ``select_in_budget``) and writes the result
    to ``session_dir / ".claude" / "agent-memory.md"``. No active memories
    (or nothing fits the budget) -> no file is written, returns ``0``.

    This is the read side of agent memory; the write side is the remember
    tool (V1c Task 4).

    Never raises into spawn — mirrors ``record_snapshot``: any failure
    (repo error, malformed row, disk error) is logged via
    ``logger.exception`` and swallowed, so a memory-materialization bug can
    never block the chat spawn the user is waiting on.
    """
    try:
        rendered, count = _rendered_memories_with_count(agent_row)
        if rendered is None:
            return 0

        claude_dir = session_dir / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        (claude_dir / "agent-memory.md").write_text(rendered, encoding="utf-8")
        return count
    except Exception:
        logger.exception(
            "agent memory materialization failed for agent_id=%s — spawn continues without memories",
            agent_row.get("id"),
        )
        return 0
