# One Agent Model (C1+C2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Trunk-plan note:** This is the first Track C plan of the remediation
> program (`2026-08-24-agnes-remediation-program.md`). It covers C1 (one
> agent API) and C2 (agent-owned authority + shared-agent runtime +
> caller-bound policies + per-caller attribution). C5–C7 (picker UI,
> builder role/approval, @delegation) are separate follow-up plans.

**Goal:** One agent vocabulary (`/api/v1`), one authority model (the agent's
own grants, not an intersection with its owner), and a runtime in which a
user an agent was shared with can actually run it — with row policies bound
to the caller, and usage attributed to the caller.

**Architecture:** Fold the builder's `/api/agents` adapter into the `/api/v1`
vocabulary and delete it (LD3: no deprecation window). Make `agent_scope`
the agent's grant store (no new table): add `granted_by`, gate who may write
data-authority items, and replace `compute_agent_intersection` with
`resolve_agent_authority` that no longer consults the *owner's* grants at
run time. Open the four owner-scoped runtime resolution sites to
`owned ∪ granted-runnable`. Bind `$user_*` policy variables to the CALLER.

**Tech Stack:** FastAPI, Postgres (Alembic-only migrations — REQUIRES A3
merged), existing `agent_scope`/`AgentPrincipal`/broker seams, pytest.

**Spec:** `docs/superpowers/plans/2026-08-24-agnes-remediation-program.md`
§Track C locked contract + §5 product design (Agents + Corporate Brain).

## Ground truth this plan stands on (verified 2026-08-26, main@1fc7ca3b8)

What CHANGED since the 2026-08 audit (do not re-fix):
- **Builder scope is enforced** (#1520): `/api/agents` create/update maps
  knowledge/plugins onto real `agent_scope` rows, all four `*_mode` axes
  `'selected'` fail-closed (`app/api/agents.py:235-319,606-616,673-689`;
  backfill `0070_builder_agent_scope_v122`).
- **`data_package` is a valid scope item** with live member-table expansion
  (`app/api/agents_admin.py:75-77`, `src/agent_scope_intersection.py:69-79,137`).
- **Chat storage is unified** — both surfaces ride one ChatManager +
  `chat_session_repo()`/`chat_message_repo()`; the remaining split is
  protocol (WS/ticket at `app/api/chat.py:196-210` vs SSE/AG-UI at
  `app/api/agent_sessions.py:290`) and router duplication.
- **Chat default provider is the embedded kai engine**
  (`app/chat/kai_engine_provider.py`; `docker` provider remains). The broker
  seam survives (per-turn tickets → `POST /api/broker/anthropic`, model pin
  + budget enforced). Workspace materialization has TWO delivery contracts:
  bind-mount (docker) and tarball-overlay (kai). Kai keeps its own PG
  conversation store; session ids are UUIDs under kai.

What is UNCHANGED (the gaps this plan closes):
- Runtime resolution is owner-scoped everywhere: `app/api/agent_runtime.py:168`
  (slug), `app/api/chat.py:141-162`, `app/api/agent_sessions.py:188`, CLI as
  a pure v1 caller. A `ResourceType.AGENT` grant conveys builder-read only
  (`app/api/agents.py:5-17`); no runtime consumer exists.
- Authority = `owner grants ∩ agent_scope`
  (`src/agent_scope_intersection.py`), and `src/access_policy.py:358-360`
  binds the OWNER's identity for `AgentPrincipal`.
- Two agent APIs over one table: `app/api/agents.py:52-53` vs
  `app/api/agents_admin.py:51-52` (both in `app/main.py:2737,2771`).
- Schema at v124 / Alembic head `0072_sync_state_id_v124`; `slack_channel`
  routing rows live INSIDE `agent_scope` (one non-deleted agent per channel,
  `app/api/agents_admin.py:63-74,483-525`) — no table reshape may break this.

## Deviation from the master plan (recorded here)

The master plan sketched a NEW `agent_grants` table. Rejected during
detailed planning: `agent_scope` already IS the per-agent grant store —
it holds `data_package` with live expansion, participates in enforcement,
carries the Slack routing rows, and has a working builder writer. A second
table would duplicate the concept (disease N2) and force a double
migration. Instead `agent_scope` gains a `granted_by` column and a
write-gate; the master plan's intent (LD2) is preserved.

## Decision point D-C2 (owner-approved default; can be overridden)

LD2's letter says the owner disappears from the authority equation
entirely. Pure scope-only authority has one security hole during C2 (before
C6's builder-role/approval exists): any user can create an agent and
self-grant data scope, so scope-only authority would let them mint
authority they don't have. Chosen staging, strictly-safe at every step:

- **Admin-granted items** (`granted_by` is an admin): unconditioned agent
  authority — pure LD2.
- **Self-service items** (`granted_by` = a non-admin, i.e. today's builder
  flow): enforced as `item ∩ granter's CURRENT access` — the same
  intersection as today, but keyed to the *granter*, not "the owner".
- C6 migrates self-service into the builder-role + approval queue, after
  which the intersection path retires and pure LD2 holds everywhere.

Migration backfills `granted_by := owner_user_id` for existing rows, so
cutover changes NOTHING for existing agents (their behavior stays exactly
today's intersection) while newly admin-granted items immediately get the
shared-agent semantics.

## Global Constraints

- Inherits the master program's Global Constraints (CHANGELOG same-PR,
  vendor-neutral, draft-PR-first, verify_syncmap, review loop, no merging).
- **Blocked on A3 merged** (PG-first ratchet): every migration here is
  Alembic-only, no `src/db.py` ladder step, no `_pg.py` sibling for new
  repo methods... but NOTE: `agents`/`agent_scope` repos are EXISTING
  dual-backend pairs — methods **modified** on them stay dual-backend
  (frozen ≠ abandoned); only genuinely NEW stores may be PG-only. Expect
  most of C2 to be dual-touching because it edits existing repos.
- BREAKING changes allowed with migration + `**BREAKING**` bullets (LD3).
- The plan's execution rewrites contracts pinned by the test inventory in
  the final section — updating those tests to the new contract is in-scope
  work, not test-weakening; every updated test must re-pin the NEW contract
  with the same strength.
- Reserved for this plan (parallel work must avoid): `app/api/agents*.py`,
  `app/api/agent_runtime.py`, `app/api/agent_sessions.py`,
  `app/api/chat.py`, `src/agent_scope_intersection.py`,
  `src/access_policy.py`, `app/api/broker*.py`.

---

## Stage C1 — one agent API (2 PRs)

### Task C1.1: v1 absorbs the builder's needs

**Files:** Modify `app/api/agents_admin.py` (add the builder-shape read/write
projections v1 lacks — inventory first: diff `app/api/agents.py`'s
request/response models against v1; known deltas: `instructions`/`tone`/
`greeting` naming, opaque `knowledge`/`plugins` JSON round-trip, template
prefill endpoints if any live builder-side), keep URL space `/api/v1/agents*`.
**Test:** new `tests/test_v1_builder_parity.py` — for every builder-page
operation (list incl. shared, get, create, patch, delete, share-state read),
an equivalent v1 call exists and round-trips the builder payload.

- [ ] Inventory diff (paste into PR body) → failing tests per missing
  operation → implement as v1 additions (same gates as existing v1 routes;
  reads accept user PATs per the B4 dependency, mutations session-only) →
  green → `make update-openapi-snapshot` → commit.

### Task C1.2: builder UI re-pointed; `/api/agents` deleted

**Files:** `app/web/templates/agents.html` (+ its JS) → call v1 endpoints;
Delete `app/api/agents.py` + its registration in `app/main.py:2737`; update
`tests/test_documentation_api_triple_surface.py` (drop the two-registries
exemption — its reason is now satisfied); rewrite the management-contract
tests (inventory below) to v1; CHANGELOG `**BREAKING**` (API `/api/agents`
removed; v1 is the only agent API).

- [ ] Re-point UI (template/JS only, no UX redesign — that's C5) → delete
  router → fix every import/test → full targeted battery green → commit.
  Acceptance: `grep -r "api/agents\b"` (non-v1) finds nothing live.

## Stage C2 — agent-owned authority + shared runtime (4 PRs, in order)

### Task C2.1: `granted_by` + write-gate

**Files:** Alembic `00XX_agent_scope_granted_by_v12X` (column
`granted_by TEXT NULL` + backfill `granted_by := agents.owner_user_id`;
Alembic-only per A3 — but `agent_scope` writes go through existing repo
methods: extend BOTH `src/repositories/agents*.py` writers to persist
`granted_by` (existing-pair rule)); `app/api/agents_admin.py:PUT scope` +
`app/api/agents.py`-successor v1 builder writes: every scope write records
the writer; **write-gate**: data-authority item_types
(`table|data_package|collection|connection`) — a non-admin writer may only
grant items they can currently access (validate at write time via the same
access checks the intersection uses today); admins unrestricted;
`plugin|memory_domain|slack_channel` keep today's rules.
**Interfaces — produces:** every `agent_scope` row carries a non-null
`granted_by` after migration; writers enforce the gate.
**Tests:** contract test both backends (column + backfill), write-gate
matrix (admin grants anything; non-admin grants own-accessible → 200,
non-accessible → 403 `scope_item_not_accessible`), Slack-channel invariant
untouched.

### Task C2.2: `resolve_agent_authority` replaces the owner intersection

**Files:** `src/agent_scope_intersection.py` → new module or in-place
rewrite exposing `resolve_agent_authority(agent_id) -> AgentAuthority`:
union over scope rows of — admin-granted: item expanded unconditionally;
self-granted: item ∩ granter's CURRENT access (reuse the existing
owner-side machinery, parameterized by granter id). Callers
(`app/auth/pat_resolver.py` principal build, chat spawn, MCP seams, scope
snapshots) switch to it. Delete the "owner grants" concept from the
runtime path.
**Tests:** rewrite `tests/test_agent_scope_intersection.py` +
`test_agent_scope_e2e.py` + `test_agent_scope_seams.py` to the new
semantics; REQUIRED new cases: (a) admin-granted package reaches the agent
even though the owner has NO grant on it (kills the audit's
package-invisible bug class for admin-built agents — must FAIL pre-change);
(b) self-granted item stops resolving when the granter loses access;
(c) snapshot parity (`agent_scope_snapshots` reflects the new resolution).

### Task C2.3: shared-agent runtime + caller binding

**Files:** resolution sites open to `owned ∪ runnable`: new repo read
`agents_repo().get_runnable_by_slug(user_id, slug)` (owned OR
`ResourceType.AGENT` grant via caller's groups — reuse
`app/resource_types.py:860`'s reader; existing-pair: both backends) wired
into `app/api/agent_runtime.py:151-183`, `app/api/chat.py:141-162`,
`app/api/agent_sessions.py:188`, and the v1 list gains
`runnable=true` filter (CLI `agnes chat` picker data source).
`AgentPrincipal` gains `caller_user_id/caller_email` (kept alongside owner
fields); `src/access_policy.py:350-360` binds `$user_*` to the CALLER for
AgentPrincipal. Memory notebooks / PAT issuance / mutation rights stay
OWNER-only (a runnable grant is run+read, never manage).
**Kai/docker note:** both providers receive the materialized profile the
same way as today (bind-mount vs tarball) — this task changes WHO may
start a session and WHOSE identity policies bind, not the delivery
(`app/chat/agent_profile.py` untouched except principal fields flowing in).
**Tests:** the master plan's program-level E2E story becomes a real test —
admin builds agent + grants a package (C2.1/C2.2), shares to a group; a
NON-owner group member creates a session via BOTH surfaces (v1 sessions +
web chat route) and gets rows filtered by THEIR policy identity (access
policy with `$user_email`); owner-only mutations still 403 for the grantee.
Must fail (404 agent_not_found) before this task. Update
`test_chat_session_as_agent.py`, `test_agent_sessions_api.py`,
`test_agent_principal.py`, `test_agent_session_principal.py`.

### Task C2.4: per-caller usage attribution

**Files:** `llm_usage` gains `caller_user_id` (Alembic-only migration —
usage rows; verify which repo writes them: `app/api/broker.py:946-1002`
batcher + `broker_agent_policy.py` ledger — thread the principal's caller
through the ticket → broker path incl. the kai ticket mint
(`app/api/kai.py`)); `GET /api/v1/agents/{slug}/usage` reports a per-caller
breakdown for the owner/admin.
**Tests:** broker policy tests extended — two different callers on one
shared agent produce distinguishable usage rows; budget still enforced at
the AGENT level (unchanged).

---

## Execution & sequencing

1. C1.1 → C1.2 → C2.1 → C2.2 → C2.3 → C2.4, one PR each, review loop per
   program rules; C2.2 and C2.3 are the security-critical reviews (rbac
   reviewer must get the explicit questions: no widening at migration
   cutover; grantee cannot manage; caller binding cannot be spoofed via
   the kai engine's session JWT path).
2. Each PR rewrites its slice of the pinned-test inventory (below);
   updated tests must fail against pre-change code where the contract
   genuinely flips (evidence in PR body).
3. After C2 lands: C5 (picker UI per the product design), C6 (builder
   role + approval — retires the self-service intersection), C7
   (@delegation) get their own plans.

## Pinned-test inventory to rewrite (from the 2026-08-26 scout)

Management/builder: `test_agents_management_api.py`,
`test_agent_builder_scope_contract.py`, `test_agents_schema.py`,
`test_agent_v1b_schema.py`, `db_pg/test_agents_contract.py`, template/slug
tests. Scope/principal: `test_agent_scope_e2e.py`,
`test_agent_scope_intersection.py`, `test_agent_scope_seams.py`,
`test_agent_scope_mcp.py`, `test_agent_principal.py`,
`test_agent_session_principal.py`, `test_broker_agent_policy.py`,
`test_agent_pat.py`, `test_agent_reads_on_pat.py`. Runtime/sessions:
`test_agent_responses_api.py`, `test_agent_sessions_api.py`,
`test_agent_sse*.py`, `test_agent_usage_api.py`,
`test_agents_preview_is_not_a_chat.py`. Chat: `test_chat_api.py`,
`test_chat_agent_binding.py`, `test_chat_session_as_agent.py`,
`test_agent_profile_spawn.py`. CLI/sharing: `test_cli_agent.py`,
`test_cli_chat.py`, `test_web_library_sharing.py`, `test_web_nav_agents.py`.

## Self-review notes

- The plan deliberately does NOT touch: chat protocol unification (WS vs
  SSE — its own later slice), workspace delivery contracts, the kai
  engine's own store, picker UI, approval flows, @delegation.
- Slack-channel scope rows: no reshape; C2.1's column-add is additive.
- If A3 slips, C2.1's migration falls back to the dual-ladder rule — do
  not start C2 before that is decided.
