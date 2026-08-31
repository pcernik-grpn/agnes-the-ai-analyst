# SharePoint ACL Mirroring (all slices) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. In this repo the plan is executed by `/agnes-build` (decomposer → agnes-builder worktrees → integrator → /agnes-review).

**Goal:** Mirror source-system (SharePoint) permissions into Agnes — groups, memberships, collection grants, broken-inheritance exclusions, and index-time audience variants — implementing all four slices of the design spec with the Q7 fork resolved at runtime by a policy switch.

**Architecture:** A third source-segregated sync writer (the Google-nightly pattern): a worker job reads scope-root role assignments from Graph, resolves principals to Agnes groups (`entra:<oid>`, `sp-direct:<scope>`) and users (case-insensitive email), and writes ordinary `user_group_members` + `resource_grants` rows tagged with a system sentinel — zero enforcement changes for reachability. Granularity rides an Agnes-side subtree sweep (Graph `hasUniqueRoleAssignments` probes) whose exclusion list is handed to the external producer via the existing subprocess seam. Audience variants add one nullable `claims.audience` column (PG-only Alembic), one AND-term inside `facts_pg`'s single visibility helper, and a per-scope `{class → groups}` mapping; the Q7 MUST-NOT/SHOULD-NOT fork is a config enum `acl_sync.guarantee_mode` (default `must_not`, the fail-closed reading).

**Tech Stack:** FastAPI, httpx (`httpx.MockTransport` for tests), Microsoft Graph app-only, Postgres (Alembic) + frozen DuckDB↔PG pair for `user_group_members`, worker job-queue (LIGHT lane), pytest with `shared_app`/`seeded_app` fixtures.

**Spec:** `docs/superpowers/specs/2026-08-28-sharepoint-acl-mirroring-design.md` (this branch carries the spec commit; also PR #1743). Parent: `docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md`.

## Global Constraints

- **PG-first ratchet (A3):** no new DuckDB app-state repo, no new `src/db.py` schema step. New schema = Alembic revision only (next free: `0086_`). `user_group_members` is a maintained frozen pair — its new method lands in BOTH `src/repositories/user_group_members.py` and `_pg.py` + contract test in the same task.
- **Sentinel constants (single source):** `ACL_SYNC_SENTINEL = "system:sharepoint-acl-sync"` (grants `assigned_by`, groups `created_by`), `ACL_SYNC_SOURCE = "sharepoint_sync"` (membership `source`) — defined once in `connectors/sharepoint/acl_sync.py`, imported everywhere else.
- **Group naming:** Entra groups → `entra:<group-oid>`; direct user assignments → one synthetic group per scope `sp-direct:<source_scope_id>`. Display name is presentation; `user_groups.name` is the canonical key.
- **Identity join:** `users_repo().get_by_email_ci(...)` ONLY (never `get_by_email`). Unmatched principals grant nobody and are counted (fail closed). The sync never creates user accounts.
- **Enforcement placement:** reachability stays `accessible_collection_ids` → `claims.corpus_id = ANY(:readable)`; the audience selector is ONE added AND-term inside `facts_pg`'s existing visibility helpers. No new enforcement path anywhere. 404-never-403.
- **Q7 fork = config:** `acl_sync.guarantee_mode: "must_not" | "should_not"`, default `"must_not"`. must_not ⇒ subtree default-exclude is mandatory (no include-anyway override), stale grants suspended past `acl_sync.max_stale_hours` (default 72), untagged claims in a tiered scope are admin-only. should_not ⇒ advisory override allowed, stale-with-warning, untagged = unrestricted-within-collection.
- **Feature flag:** everything dark behind `acl_mirroring.enabled` (default **false**) — `Switch` in `app/switches.py` + `docs/feature-flags.md` + `config/instance.yaml.example`, read via `feature_enabled("acl_mirroring", "enabled", env_var="AGNES_ACL_MIRRORING_ENABLED")`.
- **Audit:** every new action string registered in `src/audit_events.py` CATALOG; every new route declared in `src/audit_posture.py`; writes via `log_safe`.
- **Builder obligations:** new routes → PG smoke `COVERED_ROUTES`; CHANGELOG bullet under `## [Unreleased]`; vendor-agnostic content only (no customer names/tenants); no AI attribution in commits.
- **Tests:** never `create_app()` per test — use `shared_app`/`seeded_app` + `monkeypatch.setenv("DATA_DIR", str(tmp_path))`. Graph calls mocked with `httpx.MockTransport` (idiom in `tests/test_admin_sharepoint.py`).
- **Judgment calls made here (flag in PR body):** (1) nightly job is one sweep-all job iterating connections (per-connection failure isolation) rather than one enqueued job per connection; (2) most-privileged-variant dedup happens in Python projection after the SQL visibility filter (visibility itself is SQL, before LIMIT); (3) under must_not, untagged claims in a tiered corpus are visible to admins only — the runtime approximation of "re-index before enable"; (4) webhook acceleration (spec §5.2 v1.1) is external-producer work, out of this build.

---

### Task 1: Graph client — item permissions + transitive members

**Files:**
- Modify: `connectors/sharepoint/graph_client.py` (append after `probe_unique_permissions`)
- Test: `tests/test_sharepoint_graph_client_acl.py` (new)

**Interfaces:**
- Consumes: existing `_graph_get(access_token, path, params=...)`, `SharePointGraphError`.
- Produces:
  - `async def list_item_permissions(access_token: str, drive_id: str, item_id: str) -> List[Dict[str, Any]]` — raw Graph `permission` objects, all pages.
  - `async def list_group_transitive_members(access_token: str, group_id: str) -> List[Dict[str, Any]]` — user members only (`@odata.type == "#microsoft.graph.user"`), each `{id, mail, userPrincipalName}`, all pages.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sharepoint_graph_client_acl.py
import httpx
import pytest

from connectors.sharepoint import graph_client


def _transport(pages: dict[str, dict]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        key = str(request.url)
        for frag, body in pages.items():
            if frag in key:
                return httpx.Response(200, json=body)
        return httpx.Response(404, json={"error": {"message": "no page"}})
    return httpx.MockTransport(handler)


@pytest.mark.anyio
async def test_list_item_permissions_follows_next_link(monkeypatch):
    page2_url = "https://graph.microsoft.com/v1.0/drives/d1/items/root/permissions?$skiptoken=x"
    pages = {
        "/drives/d1/items/root/permissions?$skiptoken=x": {
            "value": [{"id": "p2", "roles": ["read"]}]},
        "/drives/d1/items/root/permissions": {
            "value": [{"id": "p1", "roles": ["read"]}],
            "@odata.nextLink": page2_url},
    }
    monkeypatch.setattr(graph_client, "_http_client", lambda: httpx.AsyncClient(transport=_transport(pages)))
    perms = await graph_client.list_item_permissions("tok", "d1", "root")
    assert [p["id"] for p in perms] == ["p1", "p2"]


@pytest.mark.anyio
async def test_transitive_members_keeps_users_only(monkeypatch):
    pages = {
        "/groups/g-123/transitiveMembers": {"value": [
            {"@odata.type": "#microsoft.graph.user", "id": "u1",
             "mail": "Alice.Novak@example.com", "userPrincipalName": "Alice.Novak@example.com"},
            {"@odata.type": "#microsoft.graph.group", "id": "nested"},
        ]},
    }
    monkeypatch.setattr(graph_client, "_http_client", lambda: httpx.AsyncClient(transport=_transport(pages)))
    members = await graph_client.list_group_transitive_members("tok", "g-123")
    assert [m["id"] for m in members] == ["u1"]
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_sharepoint_graph_client_acl.py -v` → FAIL (`AttributeError: list_item_permissions`).

- [ ] **Step 3: Implement**

```python
async def _graph_get_all_pages(access_token: str, path: str, *, params=None) -> List[Dict[str, Any]]:
    """Collect ``value`` across @odata.nextLink pages (permissions and
    transitiveMembers both page; a single-page read costs nothing extra)."""
    items: List[Dict[str, Any]] = []
    body = await _graph_get(access_token, path, params=params)
    while True:
        items.extend(body.get("value") or [])
        next_link = body.get("@odata.nextLink")
        if not next_link:
            return items
        # nextLink is absolute and already carries the query string.
        body = await _graph_get(access_token, next_link.split("/v1.0", 1)[-1])


async def list_item_permissions(access_token: str, drive_id: str, item_id: str) -> List[Dict[str, Any]]:
    """Role assignments on one drive item (the scope root). App-only requires
    Sites.FullControl.All or a per-site Sites.Selected full-control role —
    surface Graph 403 as SharePointGraphError, never swallow (the source
    card shows the typed failure)."""
    return await _graph_get_all_pages(access_token, f"/drives/{drive_id}/items/{item_id}/permissions")


async def list_group_transitive_members(access_token: str, group_id: str) -> List[Dict[str, Any]]:
    """Transitive USER members of an Entra group — nested groups are
    flattened by Graph itself; non-user directory objects are dropped here.
    Requires GroupMember.Read.All (NOT included in Sites.FullControl.All)."""
    rows = await _graph_get_all_pages(
        access_token,
        f"/groups/{group_id}/transitiveMembers",
        params={"$select": "id,mail,userPrincipalName", "$top": "999"},
    )
    return [r for r in rows if r.get("@odata.type") == "#microsoft.graph.user"]
```

Match `_graph_get`'s real URL-building convention when appending (read the function first; if it prefixes `https://graph.microsoft.com/v1.0`, pass the relative path exactly as the existing `list_*` helpers do).

- [ ] **Step 4: Run to verify pass** — `pytest tests/test_sharepoint_graph_client_acl.py -v` → PASS.
- [ ] **Step 5: Commit** — `git add connectors/sharepoint/graph_client.py tests/test_sharepoint_graph_client_acl.py && git commit -m "feat(sharepoint): Graph permissions + transitiveMembers readers"`

---

### Task 2: Frozen pair — `replace_group_members_for_source`

**Files:**
- Modify: `src/repositories/user_group_members.py` (after `replace_synced_groups`)
- Modify: `src/repositories/user_group_members_pg.py` (same position)
- Test: `tests/db_pg/test_user_group_members_contract.py` (extend)

**Interfaces:**
- Produces: `def replace_group_members_for_source(self, group_id: str, user_ids: List[str], source: str, added_by: str) -> None` on both backends — the group-oriented transpose of `replace_synced_groups`: DELETE this group's rows WHERE source=?, INSERT one row per user, `ON CONFLICT (user_id, group_id) DO NOTHING`, single transaction, same write-write-conflict retry loop as `replace_synced_groups` (DuckDB side).

- [ ] **Step 1: Extend the contract test (failing)**

```python
# in tests/db_pg/test_user_group_members_contract.py — same parametrized
# both-backends fixture the existing assertions use; follow the file's idiom.
def test_replace_group_members_for_source_segregation(repo, seeded_users, seeded_group):
    g = seeded_group
    u1, u2, u3 = seeded_users[:3]
    repo.add_member(u1, g, source="admin", added_by="admin-user")
    repo.replace_group_members_for_source(g, [u1, u2], source="sharepoint_sync", added_by="system:sharepoint-acl-sync")
    # u1 admin row survives; u1 gains no duplicate (ON CONFLICT), u2 added.
    members = {m["user_id"] for m in repo.list_members_for_group(g)}
    assert members == {u1, u2}
    # replacement removes only its own source's rows
    repo.replace_group_members_for_source(g, [u3], source="sharepoint_sync", added_by="system:sharepoint-acl-sync")
    members = {m["user_id"] for m in repo.list_members_for_group(g)}
    assert members == {u1, u3}  # u1 kept via admin source, u2 gone, u3 added
```

- [ ] **Step 2: Run both backends** — `pytest tests/db_pg/test_user_group_members_contract.py -v -k replace_group_members` → FAIL (method missing).

- [ ] **Step 3: Implement — DuckDB sibling**

```python
def replace_group_members_for_source(
    self, group_id: str, user_ids: List[str], source: str, added_by: str
) -> None:
    """Authoritative refresh of this GROUP's ``source``-tagged membership —
    the group-oriented transpose of :meth:`replace_synced_groups` for
    resource-driven syncs (one connection sweep computes one group's full
    member set; the user-oriented primitive would clobber concurrent
    connections' rows for shared users). Same source-segregation invariant,
    same single-transaction atomicity, same conflict-retry loop."""
    last_err: Optional[duckdb.Error] = None
    for attempt in range(_SYNC_CONFLICT_RETRIES):
        try:
            self.conn.execute("BEGIN")
            self.conn.execute(
                "DELETE FROM user_group_members WHERE group_id = ? AND source = ?",
                [group_id, source],
            )
            for user_id in user_ids:
                self.conn.execute(
                    """INSERT INTO user_group_members
                       (user_id, group_id, source, added_by)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT (user_id, group_id) DO NOTHING""",
                    [user_id, group_id, source, added_by],
                )
            self.conn.execute("COMMIT")
            return
        except duckdb.TransactionException as e:
            self._safe_rollback()
            last_err = e
            time.sleep(_SYNC_CONFLICT_BACKOFF_S * (attempt + 1))
        except Exception:
            self._safe_rollback()
            raise
    if last_err is not None:
        raise last_err
```

PG sibling: same semantics inside `self._engine.begin()` (mirror `replace_synced_groups`'s PG body — swap the DELETE predicate to `group_id = :g AND source = :s`).

- [ ] **Step 4: Run both backends to pass** — same command → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(rbac): group-oriented synced-membership replace (frozen-pair, both backends)"`

---

### Task 3: ACL classification + naming (pure engine)

**Files:**
- Create: `connectors/sharepoint/acl_sync.py`
- Test: `tests/test_sharepoint_acl_classify.py` (new)

**Interfaces:**
- Produces (imported by Tasks 4, 5):
  - `ACL_SYNC_SENTINEL = "system:sharepoint-acl-sync"`, `ACL_SYNC_SOURCE = "sharepoint_sync"`
  - `def entra_group_name(oid: str) -> str` → `f"entra:{oid}"`
  - `def direct_group_name(source_scope_id: str) -> str` → `f"sp-direct:{source_scope_id}"`
  - `@dataclass Classified: entra_group_oids: list[str]; direct_user_emails: list[str]; unhonored: list[dict]` (each unhonored: `{"kind": "...", "detail": "..."}`)
  - `def classify_permissions(perms: list[dict]) -> Classified` — the spec §8.2 table.

- [ ] **Step 1: Failing tests (one per §8.2 row)**

```python
# tests/test_sharepoint_acl_classify.py
from connectors.sharepoint.acl_sync import classify_permissions

def _perm(**grantee):
    return {"id": "p", "roles": ["read"], **grantee}

def test_user_and_group_honored():
    c = classify_permissions([
        _perm(grantedToV2={"user": {"id": "u", "email": "a@example.com"}}),
        _perm(grantedToV2={"group": {"id": "g-123"}}),
    ])
    assert c.direct_user_emails == ["a@example.com"]
    assert c.entra_group_oids == ["g-123"]
    assert c.unhonored == []

def test_out_rows_fail_closed_and_counted():
    c = classify_permissions([
        _perm(grantedToV2={"siteGroup": {"id": "3", "displayName": "Members"}}),
        _perm(link={"scope": "organization"}, grantedToIdentitiesV2=[]),
        _perm(link={"scope": "anonymous"}),
        _perm(grantedToV2={"user": {"id": "x", "userPrincipalName": "guest_ext#EXT#@t.example"}}),
        _perm(grantedToV2={"application": {"id": "app1"}}),
        _perm(grantedToV2={"user": {"id": "no-mail"}}),  # email-less principal
    ])
    assert c.entra_group_oids == [] and c.direct_user_emails == []
    assert len(c.unhonored) == 6
    kinds = {u["kind"] for u in c.unhonored}
    assert kinds == {"site_group", "org_link", "anonymous_link", "external_guest", "application", "no_email"}

def test_specific_people_link_grantees_are_out():
    c = classify_permissions([
        _perm(link={"scope": "users"},
              grantedToIdentitiesV2=[{"user": {"id": "u9", "email": "b@example.com"}}]),
    ])
    assert c.direct_user_emails == []
    assert c.unhonored[0]["kind"] == "sharing_link"
```

- [ ] **Step 2: Run** — `pytest tests/test_sharepoint_acl_classify.py -v` → FAIL (module missing).

- [ ] **Step 3: Implement** — module docstring names the spec (§2, §8.2) and the sentinel-segregation contract. Classification: a permission with `link` → `sharing_link`/`org_link`/`anonymous_link` by `link.scope`; `grantedToV2.siteGroup` → `site_group`; `grantedToV2.application` → `application`; `grantedToV2.user` with UPN containing `#EXT#` → `external_guest`; user without `email`/`mail`/UPN-that-looks-like-email → `no_email`; user with email → direct; `grantedToV2.group` → entra oid. Email preference order: `email`, then `mail`, then `userPrincipalName` (a UPN is usually the primary SMTP; the case-insensitive join in Task 4 absorbs case drift). Dedupe oids/emails preserving order.

- [ ] **Step 4: Run to pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(sharepoint): ACL permission classification engine (§8.2)"`

---

### Task 4: `sharepoint-acl-sync` worker job + scheduler + audit actions

**Files:**
- Modify: `connectors/sharepoint/acl_sync.py` (add the sync body — keeps kinds.py thin)
- Modify: `app/worker/kinds.py` (register kind; thin `_run_sharepoint_acl_sync` delegating to the module)
- Modify: `services/scheduler/__main__.py` (`_ENQUEUE_BODIES["sharepoint-acl"] = {"kind": "sharepoint-acl-sync", "idempotency_key": "sharepoint-acl-sync"}` + a daily JOBS row following `jira-org-refresh`'s pattern)
- Modify: `src/audit_events.py` (CATALOG entries)
- Test: `tests/test_sharepoint_acl_sync.py` (new)

**Interfaces:**
- Consumes: Task 1 (`list_item_permissions`, `list_group_transitive_members`), Task 2 (`replace_group_members_for_source`), Task 3 (classify + names + sentinels), existing `resolve_sharepoint_settings`, `get_app_token`, `user_groups_repo().ensure(...)`, `resource_grants_repo().ensure_grant/list_all/delete`, `users_repo().get_by_email_ci`, `source_connections_repo()`.
- Produces:
  - `def run_acl_sync(payload: dict) -> dict` in `acl_sync.py` — `payload = {"connection_id": str | None}`; `None` sweeps every `source_type='sharepoint'` connection that has at least one `access_mode='mirrored'` scope. Returns `{"connections": N, "scopes": M, "matched": X, "unmatched": Y, "errors": [...]}`.
  - Job kind name: `"sharepoint-acl-sync"`, LIGHT lane, `retry_in_seconds=300`.
  - New audit actions (register in CATALOG, mutation kind unless noted): `sharepoint_acl.sync_completed`, `sharepoint_acl.sync_failed`, `sharepoint_acl.grant_added`, `sharepoint_acl.grant_removed`, `sharepoint_acl.membership_replaced`, `sharepoint_acl.principal_unmatched`, `sharepoint_acl.grants_suspended`, `sharepoint_acl.sync_triggered`.
  - Last-run block written into the connection's `config["acl_sync_last_run"]`: `{"at": iso, "ok": bool, "matched": int, "unmatched": int, "unhonored": [...], "grant_deltas": {collection_id: {"added": [...], "removed": [...]}}, "stale_scopes": [...], "error": str | None}`.

**Sync body (per connection, per mirrored scope):**
1. Resolve settings + app token (typed failures → run marked failed for that connection, audit `sync_failed`, continue with next connection).
2. `list_item_permissions(token, scope["drive_id"], scope["source_scope_id"])` → `classify_permissions`. (Read the scope row first — confirm the stored keys carry the drive id; the wizard's `confirm_scope` stores `source_scope_id`; the drive id lives on the connection config / scope row per `browse_tree`'s storage — use whatever key the existing extraction dispatch uses to address the item.)
3. Target groups: each entra oid → `user_groups_repo().ensure(name=entra_group_name(oid), created_by=ACL_SYNC_SENTINEL, description="Mirrored from Entra group <oid> by SharePoint ACL sync")`; direct emails (if any) → `ensure(direct_group_name(scope_id), ...)`.
4. Membership: entra groups → `list_group_transitive_members` → emails → `get_by_email_ci` per email (unmatched → counted, audit `principal_unmatched` once per run with count detail, never per-user spam); write via `replace_group_members_for_source(group_id, matched_user_ids, source=ACL_SYNC_SOURCE, added_by=ACL_SYNC_SENTINEL)`. **Fail-soft:** a `SharePointGraphError` during ONE group's expansion keeps that group's previous rows (skip the replace), flags the scope stale in the run report.
5. Grants: target = the scope's honored groups; current = `[g for g in grants.list_all(resource_type=ResourceType.COLLECTION.value) if g["resource_id"] == collection_id and g["assigned_by"] == ACL_SYNC_SENTINEL]`. Add missing via `ensure_grant(..., assigned_by=ACL_SYNC_SENTINEL)` (audit `grant_added` each), delete leftovers (audit `grant_removed` each). **Order adds before removes** so the scope never passes through a granted-to-nobody window it didn't already have.
6. Staleness (guarantee_mode): compute hours since last successful run per connection; under `must_not`, if a run FAILS and staleness exceeds `acl_sync.max_stale_hours` (config int, default 72), delete all sentinel grants for the connection's mirrored scopes (audit `grants_suspended` with counts) — the next successful sync rewrites them. Under `should_not`, never suspend; the last-run block carries `stale_scopes`.
7. Feature gate: `feature_enabled("acl_mirroring", "enabled", env_var="AGNES_ACL_MIRRORING_ENABLED")` is checked first; disabled → return `{"skipped": "acl_mirroring disabled"}` (the scheduler enqueues unconditionally, harmless — same pattern as `ducklake-maintenance`).

- [ ] **Step 1: Failing tests** — build a connection row + two scopes (one `mirrored`, one `manual`) via the repos directly under `monkeypatch.setenv("DATA_DIR", tmp_path)`; monkeypatch `graph_client.list_item_permissions` / `list_group_transitive_members` / `get_app_token` with fakes (no transport needed at this layer). Assert, in separate tests:
  - mirrored scope gets `entra:<oid>` group + memberships (only users whose CI-email matched) + sentinel grant; `manual` scope untouched;
  - an admin-assigned grant on the same collection survives reconciliation; a stale sentinel grant is deleted;
  - re-run idempotent (no duplicate rows, no delta audit entries);
  - one group's expansion failure keeps its previous membership (fail-soft) and marks the run/scope stale;
  - mixed-case UPN matches a lowercase login (fixture user `alice.novak@example.com`, Graph returns `Alice.Novak@EXAMPLE.com`);
  - `must_not` + stale-past-cap ⇒ sentinel grants gone; `should_not` ⇒ they persist;
  - flag off ⇒ no-op.
- [ ] **Step 2: Run** — `pytest tests/test_sharepoint_acl_sync.py -v` → FAIL.
- [ ] **Step 3: Implement** `run_acl_sync` + kind registration + scheduler row + CATALOG entries. The kinds.py handler is 5 lines: import, call, return (module docstring entry follows the other kinds' comment style).
- [ ] **Step 4: Run to pass**; also `pytest tests/test_audit_catalog.py -q` (new actions registered correctly).
- [ ] **Step 5: Commit** — `git commit -m "feat(sharepoint): ACL mirroring sync job (nightly + on-demand), audited"`

---

### Task 5: Wizard/API — `access_mode`, sync-now, mirrored rows read-only

**Files:**
- Modify: `app/api/admin_sharepoint.py` (ConfirmScopeBody, `_scope_out`, `confirm_scope`, new route)
- Modify: `app/api/access.py` (`_guard_google_managed` → generalized sync-managed guard)
- Modify: `src/audit_posture.py` (new route posture)
- Modify: `tests/db_pg/` PG smoke `COVERED_ROUTES` (find via `grep -rn "COVERED_ROUTES" tests/`)
- Test: extend `tests/test_admin_sharepoint.py` + `tests/test_access_api.py` (or the file that exercises the google guard — locate by `grep -rln google_managed_readonly tests/`)

**Interfaces:**
- `ConfirmScopeBody` gains `access_mode: Literal["manual", "mirrored"] = "manual"`; `confirm_scope` persists it on the scope row (update-in-place on re-confirm, like `anonymize`); switching `mirrored → manual` deletes the sync's own grants for that scope (sentinel + collection-scoped) and converts nothing.
- `_scope_out` adds `access_mode` (default `"manual"` when absent) and, when present on the connection, `acl_sync_last_run` summary fields (`matched`, `unmatched`, `at`, `ok`, `stale`).
- `confirm_scope`'s revocation loop learns the sentinel skip:

```python
for grant in grants.list_all(resource_type=ResourceType.COLLECTION.value):
    if grant.get("resource_id") != collection_id or grant.get("group_id") in wanted:
        continue
    if (grant.get("assigned_by") or "") == ACL_SYNC_SENTINEL:
        continue  # mirrored row: manual unticking never deletes it (it would
                  # resurrect next sync); "stop mirroring" is access_mode.
    grants.delete(grant["id"])
```

- New route: `POST /api/admin/sharepoint/connections/{connection_id}/acl-sync` (`Depends(require_admin)`) — 409 `{"error": "feature_disabled"}` when the flag is off; otherwise enqueue `{"kind": "sharepoint-acl-sync", "payload": {"connection_id": ...}, "idempotency_key": f"sharepoint-acl-sync:{connection_id}"}` through the same jobs-repo enqueue the extraction trigger uses (mirror `trigger_extraction`'s enqueue mechanics); audit `sharepoint_acl.sync_triggered`; posture row `"POST /api/admin/sharepoint/connections/{connection_id}/acl-sync": "sharepoint_acl.sync_triggered"`.
- `app/api/access.py`: add

```python
_SYNC_MANAGED_SENTINELS = {
    "system:google-sync": ("google_managed_readonly", "Google Workspace", "admin.google.com"),
    "system:sharepoint-acl-sync": ("sharepoint_managed_readonly", "SharePoint ACL sync", "the source system"),
}
```

and extend the guard so membership/rename/delete mutations on a group whose `created_by` matches any sentinel raise the corresponding 409 (keep `_guard_google_managed` name as a thin wrapper if call sites are many — smallest diff wins).

- [ ] **Step 1: Failing tests** — (a) confirm_scope with `access_mode="mirrored"` round-trips through `_scope_out`; (b) confirm_scope with `group_ids=[]` deletes an admin grant but NOT a sentinel grant on the same collection; (c) `POST .../acl-sync` returns 202-shape with flag on, 409 with flag off, 403 for non-admin; (d) mutating a group with `created_by="system:sharepoint-acl-sync"` → 409 `sharepoint_managed_readonly`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the touched files + `pytest tests/test_audit_route_posture.py tests/test_audit_read_posture.py -q` + the PG smoke guard test named by `COVERED_ROUTES`.
- [ ] **Step 5: Commit** — `git commit -m "feat(sharepoint): per-scope access_mode, sync-now endpoint, mirrored rows read-only"`

---

### Task 6: Feature flag + config keys

**Files:**
- Modify: `app/switches.py` (three `Switch` entries)
- Modify: `docs/feature-flags.md`, `config/instance.yaml.example`
- Test: the switches contract test (locate via `grep -rln "SWITCHES" tests/ | head`) usually needs no change beyond the entries being well-formed; run it.

**Interfaces (exact entries):**

```python
Switch(
    name="acl_mirroring",
    config_keys=("acl_mirroring", "enabled"),
    env_var="AGNES_ACL_MIRRORING_ENABLED",
    kind="bool", default=False, effect="live", category="product", editable=True,
    description=(
        "SharePoint ACL mirroring: the sharepoint-acl-sync job, per-scope "
        "access_mode='mirrored', and the admin sync-now endpoint. OFF by default — "
        "turning it on changes nothing until a scope opts into mirroring."
    ),
),
Switch(
    name="acl_guarantee_mode",
    config_keys=("acl_sync", "guarantee_mode"),
    env_var="AGNES_ACL_GUARANTEE_MODE",
    kind="enum", options=("must_not", "should_not"),
    default="must_not", effect="live", category="product", editable=True,
    description=(
        "Cross-audience-leak posture (design Q7). must_not = fail closed: "
        "broken-inheritance subtrees are always excluded, mirrored grants are "
        "suspended past acl_sync.max_stale_hours, and untagged claims in an "
        "audience-tiered scope are admin-only. should_not = best effort: advisory "
        "overrides allowed, stale grants persist with warnings, untagged claims "
        "stay unrestricted within their collection."
    ),
),
Switch(
    name="acl_max_stale_hours",
    config_keys=("acl_sync", "max_stale_hours"),
    env_var="AGNES_ACL_MAX_STALE_HOURS",
    kind="int", default=72, effect="live", category="product", editable=True,
    description=(
        "must_not mode only: hours a failed ACL sync may leave mirrored grants "
        "standing before they are suspended (deleted until the next successful "
        "sync rewrites them)."
    ),
),
```

(Check the `Switch` dataclass's `kind` vocabulary first — if `"enum"`/`"int"` aren't existing kinds, use the nearest existing kind and `on_invalid="default"`; the options tuple exists.)

- [ ] Steps: add entries → run the switches/feature-flag guard tests → update the two docs files (feature-flags table row each; instance.yaml.example gets a commented `acl_mirroring:`/`acl_sync:` block) → commit `feat(config): acl_mirroring flag + guarantee-mode keys`.

---

### Task 7: Slice 2 — subtree sweep job + producer exclusion handoff

**Files:**
- Modify: `connectors/sharepoint/acl_sync.py` (`run_subtree_sweep(payload)`)
- Modify: `app/worker/kinds.py` (kind `sharepoint-subtree-sweep`, LIGHT lane, lease sized like the module's size-derived-lease notes — `AGNES_SP_SWEEP_LEASE_S` env with a 4 h default; `retry_in_seconds=None` — an operator looks at a failed multi-hour sweep, same rationale as corpus-extraction)
- Modify: `services/scheduler/__main__.py` (weekly row — follow how existing non-daily schedules are expressed in `build_jobs()`; if the scheduler only does daily, run the row daily and gate the sweep body on `config["acl_sweep_last_full"]` older than `acl_sync.sweep_interval_days`, default 7)
- Modify: `app/api/admin_sharepoint.py::_scope_out` (advisory counts) — and `_run_corpus_extraction` in `app/worker/kinds.py` (env handoff)
- Test: `tests/test_sharepoint_subtree_sweep.py` (new)

**Interfaces:**
- `run_subtree_sweep(payload)` walks each mirrored scope's folder tree via existing `list_item_children` + `probe_unique_permissions` (already `$batch`-based, 20 per batch), collecting `{"item_id", "path", "detected_at"}` per broken-inheritance subtree root; **does not descend into a detected subtree** (its children are excluded wholesale). Writes `scope["excluded_subtrees"] = [...]` + `config["acl_sweep_last_full"]` timestamp + request/429 counters into the run block (cost observed, not assumed).
- `_scope_out` adds `excluded_subtree_count` and `excluded_subtrees` (id + path) — the advisory surface's data.
- `_run_corpus_extraction` env handoff: when the connection has any `excluded_subtrees`, add `AGNES_SP_EXCLUDED_SUBTREE_IDS=json.dumps({scope_id: [item_ids]})` to the child env next to the anonymize vars. **Producer-side honoring is external-repo work** — the env var is the contract; say so in the module docstring and CHANGELOG bullet.
- Under `should_not` only: a scope may carry `include_excluded_subtrees: true` (admin set via confirm_scope body field `include_excluded_subtrees: bool = False`) — audit-logged override (`sharepoint_acl.subtree_override`, add to CATALOG); under `must_not` the field is refused with 409 `{"error": "must_not_forbids_subtree_override"}`.

- [ ] **Step 1: Failing tests** — fake `list_item_children`/`probe_unique_permissions` (tree: root → A(unique) → A/child, B(clean) → B/C(unique)); assert exclusions = [A, B/C], A/child never probed; `_scope_out` counts; env handoff present when exclusions exist and absent otherwise; override 409 under must_not / accepted + audited under should_not.
- [ ] **Step 2–4:** red → implement → green (`pytest tests/test_sharepoint_subtree_sweep.py tests/test_admin_sharepoint.py -q`).
- [ ] **Step 5: Commit** — `git commit -m "feat(sharepoint): broken-inheritance subtree sweep + producer exclusion handoff"`

---

### Task 8: Slice 4a — per-scope audience-class mapping

**Files:**
- Modify: `app/api/admin_sharepoint.py` (ConfirmScopeBody + `_scope_out` + validation)
- Test: extend `tests/test_admin_sharepoint.py`

**Interfaces:**
- Scope row gains `audience_classes: [{"name": str, "group_ids": [str]}]` — **ordered most-privileged first** (the order IS the privilege ranking; Task 11's projection dedup consumes it). `ConfirmScopeBody.audience_classes: Optional[List[AudienceClassIn]] = None` (`AudienceClassIn(BaseModel): name: str (min_length=1, pattern r"^[a-z0-9_-]{1,64}$"); group_ids: List[str]`), omitted = untouched, `[]` = clear (mirrors `group_ids` semantics).
- Validation: every `group_ids` entry must exist (400 `invalid_group_id`, reuse the existing check); class names unique per scope (400 `duplicate_audience_class`).
- A scope with non-empty `audience_classes` is **tiered**; `_scope_out` exposes `audience_classes` and `tiered: bool`.
- Produces for Tasks 9/11: helper in `app/api/admin_sharepoint.py` is NOT the read path — the runtime read is `def audience_class_map() -> dict[str, list[tuple[str, frozenset[str]]]]` in a new small module `src/audience_classes.py`: `{collection_id: [(class_name, frozenset(group_ids)), ...]}` built by scanning sharepoint connections' scopes (ordered). Cache per-request like other config reads (plain function; callers are repo-level and infrequent).

- [ ] Steps: failing test (round-trip, validation errors, tiered flag) → implement → green → commit `feat(sharepoint): per-scope audience-class mapping (wizard data, ordered by privilege)`.

---

### Task 9: Slice 3 — caller audience classes (users + AgentPrincipal)

**Files:**
- Create: `src/audience_classes.py` (from Task 8's interface) + `def audience_classes_for_caller(caller, collection_ids: Iterable[str]) -> dict[str, frozenset[str]]`
- Modify: `src/agent_scope_intersection.py` (+ agent scope pin), `app/api/agents*` scope validation (locate the agent-scope declaration validator via `grep -rn "'selected'" app/api/agents*.py src/agent_scope_intersection.py`)
- Test: `tests/test_audience_classes.py` (new)

**Interfaces:**
- For a plain user: caller's classes per tiered collection = every class whose `group_ids` intersects the user's group memberships (`user_group_members` via the existing repo — same live read `accessible_collection_ids` does); admins get all classes (god-mode consistency).
- For `PRINCIPAL_TYPES` (AgentPrincipal): default = **least-privileged class only** of each tiered collection in the intersection (last entry of the ordered list). An agent scope may pin `audience_class: {collection_id: class_name}`; the pin is honored only if the OWNER holds that class or better at request time (checked live, like the collection intersection itself) — otherwise it silently degrades to least-privileged (fail toward redacted, never toward privileged).
- Produces for Task 11: `audience_classes_for_caller` is the single entry point `facts_pg` binds parameters from.

- [ ] Steps: failing tests (user in top class; user in no class; admin; principal default = least privileged; principal pin honored iff owner qualifies) → implement → green → commit `feat(facts): caller audience-class resolution, agent least-privilege default`.

---

### Task 10: Slice 4b — `claims.audience` migration + predicate + ingest (MIGRATION — integrator serializes this last)

**Files:**
- Create: `migrations/versions/0086_claims_audience.py`
- Modify: `src/repositories/facts_pg.py` (visibility helpers + projection dedup)
- Modify: `app/api/facts.py` (`FactsIngestRequest` evidence items gain `audience: Optional[str] = None`; pass through to the claims write)
- Test: `tests/db_pg/test_facts_audience.py` (new; PG-marked like the other facts tests)

**Interfaces:**
- Migration: `op.add_column("claims", sa.Column("audience", sa.Text(), nullable=True))` + `op.create_index("idx_claims_corpus_audience", "claims", ["corpus_id", "audience"])`; downgrade drops both.
- Predicate — extend `_visibility_predicate` (same single place; admin stays `TRUE`):

```python
@staticmethod
def _visibility_predicate(column: str, is_admin: bool) -> str:
    if is_admin:
        return "TRUE"
    # Reachability (outer gate) AND audience selector (inner, §4.2):
    #   untagged claim: readable unless its corpus is tiered under must_not
    #   tagged claim:   caller must hold that (corpus, class) pair
    return (
        f"({column} = ANY(:readable) AND ("
        "  (audience IS NULL AND NOT (" + column + " = ANY(:tiered_hidden)))"
        "  OR (" + column + " || ':' || audience) = ANY(:audience_pairs)"
        "))"
    )
```

  Every read method that binds `:readable` now also binds `:tiered_hidden` (list of tiered corpus ids when `guarantee_mode == "must_not"`, else `[]`) and `:audience_pairs` (`[f"{corpus_id}:{cls}" for corpus_id, classes in audience_classes_for_caller(...) for cls in classes]`). Both computed once next to `_readable_ids` — add `def _audience_context(caller, readable) -> tuple[list[str], list[str]]` beside it so no method builds these ad hoc. **Column qualification:** the helper receives `column="c.corpus_id"` — the `audience` references must use the same alias (`c.audience`); thread the alias, don't hardcode.
- Projection dedup (most-privileged pick, §4.2): after rows return, group by `(fact_id, corpus_file_id, edge_id)`; among groups with >1 visible variant keep the one whose class ranks highest in the Task 8 ordering (untagged ranks below all classes); one helper `def _pick_most_privileged(rows, class_rank: dict[str, int]) -> rows` used by `search`/`claims` result assembly.
- Ingest: `evidence: [{doc_id, quote, audience?}]` — validate `audience` against the same `^[a-z0-9_-]{1,64}$` pattern; stored verbatim on the claim row. No re-index machinery in this task: under must_not, pre-variant (untagged) claims in a tiered scope are already hidden from non-admins by the predicate — that IS the "re-index before enable" enforcement, stated in the module docstring.

- [ ] **Step 1: Failing tests** — seed two users (top class holder via group, no-class user), one tiered + one plain collection, claims: untagged-in-plain, untagged-in-tiered, `audience='full'`, `audience='redacted'` variants of one `(fact, file)` pair. Assert:
  - plain collection unchanged for everyone (regression);
  - must_not: no-class user sees nothing from tiered scope; top-class user sees the `full` variant only (dedup picked it); admin sees everything;
  - should_not: untagged-in-tiered visible to both; no-class user sees untagged only;
  - S2 extension: attrs projection never includes a variant the caller can't read (assert on the projected attrs of the mixed pair);
  - ingest round-trip stores `audience`, rejects `audience="BAD NAME"` with 422.
- [ ] **Step 2: Run** — `pytest tests/db_pg/test_facts_audience.py -v` → FAIL.
- [ ] **Step 3: Implement** (migration → predicate + context helper → dedup → ingest field).
- [ ] **Step 4: Run** new file + the whole existing facts suite (`pytest tests/db_pg/ -k facts -q`) — the predicate change must not move any existing assertion.
- [ ] **Step 5: Commit** — `git commit -m "feat(facts): audience-variant selector — claims.audience, one predicate AND-term, privileged-pick projection"`

---

### Task 11: Slice 4c — chunk/raw/preview alignment

**Files:**
- Modify: `app/api/collections.py` (`/files/{file_id}/raw`, the text-preview path, and the chunk-backed search — locate all three via the file's module docstring index)
- Test: extend the collections tests file that exercises `/raw` (locate via `grep -rln "files/.*raw" tests/`)

**Interfaces:**
- Rule (fail toward redacted, §9 "chunks must not leak what claims withhold"): for a **tiered** collection (Task 8's `tiered` via `src/audience_classes.py`), raw bytes, text preview, and chunk snippets are served only to callers holding the **most-privileged** class of that collection (admins always). Everyone else gets the existing "no preview available" shape for preview/chunks and 404 for `/raw` (the same status a non-readable file already returns — no new error vocabulary, no 403 oracle). Non-tiered collections: unchanged.
- Implementation: one helper in `app/api/collections.py` — `def _document_text_visible(caller, collection_id) -> bool` — used at the three read sites; it consults `audience_classes_for_caller` + the class ordering. Document the O5 boundary in the helper docstring: per-audience *document derivations* are the parent spec's O5; until they exist, top-class-only is the honest stand-in.

- [ ] Steps: failing tests (tiered scope: top-class caller gets raw/preview; other caller gets 404/no-preview; plain scope regression-unchanged) → implement → green → commit `feat(collections): tiered scopes serve document text to top audience class only`.

---

### Task 12: Phase-ACL test suite + CHANGELOG + sync-map row

**Files:**
- Create: `tests/test_sharepoint_acl_phase.py`
- Modify: `CHANGELOG.md` (`## [Unreleased]` → Added), `CONTRIBUTING.md` (sync-map row)
- Test: itself.

**Interfaces (the parent spec §15.1 pre-declared tests, unit-scale):**
- **S1-source:** sharing set in SharePoint only — seed a user with NO Agnes-side grant; fake Graph returns their group on the scope root; run `run_acl_sync`; assert a facts search as that user now returns the seeded tiered-agnostic claim from the scope's collection (uses the real repos + `facts_repo`, mock Graph only).
- **S7-source:** revocation latency — after S1 state, fake Graph drops the group; re-run sync; assert the same search returns nothing AND `audit_log` carries `sharepoint_acl.grant_removed` with a timestamp (the "measured" number in unit scale = revocation is enforced at next completed sync; assert the run report's timestamps are recorded).
- **C9:** permission-only change — change ONLY the fake permissions payload between two runs (no content ingest in between); assert the grant delta is applied (this pins "the nightly permission re-read is the floor" — content delta machinery is not involved anywhere in the test).
- CHANGELOG bullet (one, Added): `SharePoint ACL mirroring (all slices, behind acl_mirroring.enabled, default off): per-scope mirrored grants via a third source-segregated sync writer, broken-inheritance subtree exclusion sweep, per-scope audience classes with index-time claim variants, and a guarantee-mode switch (must_not/should_not) deciding the fail-closed posture.`
- CONTRIBUTING.md sync-map row (the §9 rule): `Any new surface emitting a claim, quote, or claim-derived attr ⇒ must read through src/repositories/facts_pg.py's visibility helpers (_visibility_predicate/_audience_context) — a PR adding one without touching that module is a blocking review finding.`

- [ ] Steps: write the three tests failing where they can fail (S1 red until Tasks 1–5 merge in the integration branch; in /agnes-build this task depends on all prior) → green → docs edits → commit `test(sharepoint): Phase-ACL suite (S1-source, S7-source, C9) + changelog + sync-map row`.

---

## Task dependency / coupling notes (for the decomposer)

- Independent starts: T1, T2, T3, T6, T8.
- T4 needs T1+T2+T3. T5 needs T3 (+T4's enqueue only at test level — mock the jobs repo). T7 needs T1+T3 and **shares merge magnets with T4** (`app/worker/kinds.py`, `services/scheduler/__main__.py`, `src/audit_events.py`) — couple T4+T7 into one worktree, or integrate T7 after T4.
- T9 needs T8. T10 needs T9 (binds its outputs) and is the **single migration task — serialize last**. T11 needs T8+T9 (+T10's tiered semantics for tests). T12 needs everything.
- Merge magnets to watch: `app/api/admin_sharepoint.py` (T5, T7, T8 — sequence them), `app/switches.py` (T6 alone), `src/audit_events.py` (T4, T7), `src/audit_posture.py` (T5).

## Self-review (done while writing)

- Spec coverage: §2 (T2–T5), §3 (T7), §4 (T8–T11), §5 (T4, T6), §6 link 1 (T1, T7), §7 links 3–4 (T9), §8.2 (T3), §8.3/§9 (T11, T12), §10 (producer handoff is env-contract only, T7), §11 obligations (each task + T12), §1 Q7 fork (T6 switch + forked behavior in T4/T7/T10/T11). Not built, by design: webhook acceleration (external producer), site-group resolver (§8.2 "out"), O5 N-derivation store (T11 stand-in documented), second credential slot (decision 2 — existing cert, typed 403 surfaces on the source card).
- Type consistency: sentinel/source constants only from `connectors/sharepoint/acl_sync.py`; `replace_group_members_for_source(group_id, user_ids, source, added_by)` used identically in T2/T4; `audience_classes_for_caller` produced in T9, consumed in T10/T11; class ordering produced in T8, consumed in T10 dedup and T11 top-class rule.
