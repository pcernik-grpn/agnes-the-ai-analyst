# Corporate Memory Governance — Design Document

> Reviewed by: Google Gemini, Claude Sonnet 4.5, OpenAI GPT-5.4
> Version: 2 (feedback incorporated from all three reviewers)

## Problem

Today's Corporate Memory is a democratic wiki: AI extracts knowledge, everyone
votes, each person picks what they want. This doesn't work for enterprise:

- **No authority** — CEO can't mandate "everyone must know this"
- **No quality gate** — AI output goes live without human review
- **Depends on user activity** — if nobody votes, nothing gets distributed
- **No explanation** — users don't know WHY they're getting specific knowledge
- **No audit trail** — no record of who decided what
- **No expiry** — knowledge goes stale silently

## Solution

Add a governance layer where administrators curate and control knowledge
distribution. The system becomes self-operating: AI extracts, admins approve,
mandatory items distribute automatically to all users.

Everything is configurable per instance — each client picks the governance
model that fits their organization.

This is **v1 admin curation** — a credible first step toward full enterprise
governance, with a clear path to audience targeting, attestation, and compliance
features in future versions.

---

## Three Governance Modes (configurable)

> **Status (#1573):** `distribution_mode` is enforced at the sync layer.
> `GET /api/memory/bundle` (both the JSON shape and the per-domain markdown
> `agnes pull` writes), plus the matching `memory_domains[].md5` in `GET
> /api/sync/manifest`, all filter through one shared rule
> (`app/api/memory.py::select_distributable_items`): required items always
> reach their target audience in every mode; approved (non-required) items
> are distributed only in `"hybrid"` mode, and then only to a caller who has
> personally upvoted them. `"mandatory_only"` and `"admin_curated"` ship
> required items only through this channel — matching "distribution is
> always admin-driven" below. What's still unimplemented is the *catalog
> UI* reacting to the mode (e.g. `mandatory_only` hiding the vote buttons
> entirely) — the browsing/voting experience described per-mode below is
> currently the same for every mode; only what actually gets *distributed*
> is mode-aware today.

### Mode 1: "mandatory_only"

CEO/admin has full control. Users receive what's mandated, nothing else.

```
AI extracts → Review queue → Admin approves/rejects → Mandatory items → target users
```

- Users see the knowledge catalog in webapp (read-only, no voting)
- Each mandatory item has an explanation ("Why this matters")
- Users cannot add or remove items from their rules
- Users CAN flag items for correction ("Report issue" button)

**Best for:** Compliance-heavy environments, small teams with strong leadership.

### Mode 2: "admin_curated"

Admin curates, users give feedback via voting (but votes don't distribute).

```
AI extracts → Review queue → Admin approves/rejects/mandates
                                    ↓
              Mandatory items → target users (automatic)
              Approved items → visible in catalog (users vote as feedback)
```

- Voting is a signal for admins: "people find this useful"
- Admin sees vote counts when deciding what to mandate
- Users see catalog with mandatory badge + vote buttons
- Distribution is always admin-driven

**Best for:** Medium-sized teams where admin wants user input but retains control.

### Mode 3: "hybrid" (default)

Two distribution channels: mandatory (admin) + optional (user choice).

```
AI extracts → Review queue → Admin approves/rejects/mandates
                                    ↓
              Mandatory items → target users (automatic)
              Approved items → catalog → users upvote → personal rules
```

- Mandatory items go to target audience (no opt-out)
- Approved items are available for individual opt-in via voting (like today)
- Users get mandatory + their personal picks
- Best of both worlds

**Best for:** Larger teams, diverse roles, balance of governance and autonomy.

---

## Approval Workflow (configurable)

### Option A: "review_queue" (default)

New items from AI go to a pending queue. Nothing reaches users until
an admin reviews it.

```
AI extraction → status: "pending" → Admin reviews → approve / reject / mandate
```

- Admin sees a queue of pending items in the webapp
- **Batch operations**: checkboxes + "Approve selected" / "Reject selected" buttons
- Can approve (visible in catalog), reject (hidden), or mandate (goes to target users)
- Can edit title/content before approving
- Can add "Why this matters" explanation for mandatory items
- Queue has filters: by category, by source user, by date, by AI confidence
- Keyboard shortcuts for fast review (j/k navigate, a/r/m = approve/reject/mandate)

### Option B: "auto_publish"

Items go live immediately (like today). Admin intervenes retroactively.

```
AI extraction → status: "approved" (auto) → Admin can veto or mandate later
```

- Less admin work, faster knowledge flow
- Risk: bad content visible until admin catches it
- Admin gets digest of new items (e.g., Telegram notification)
- Recommended only for trusted, small-team environments

### Option C: "threshold"

AI assigns a confidence score during extraction. High confidence = auto-publish,
low = review queue.

```
AI extraction → confidence > threshold? → auto-publish (approved)
                confidence ≤ threshold? → review queue (pending)
```

- Admin only reviews borderline items
- Reduces review burden while maintaining quality gate
- Threshold configurable via `corporate_memory.auto_publish_min_confidence`
  (default `0.80`) in instance.yaml
- Confidence score visible to admin in review queue (helps calibrate trust over time)
- **Implementation note**: the confidence score is computed in code from
  `(source_type, detection_type)` via `services/corporate_memory/confidence.py`,
  the same scorer the `user_verification` path already uses — the LLM is
  never trusted to self-report its own credibility (see
  `services/session_processors/verification.py`). For the CLAUDE.local.md
  collector every item in a run shares one score (the `claude_local_md`
  source's base confidence); there is no per-item LLM-reported score.

---

## Admin Role: Per-User Flag

No new role system. Existing `users:` section in instance.yaml gets a flag:

```yaml
users:
  ceo@company.com:
    display_name: "Jan Novák"
    km_admin: true
  lead@company.com:
    display_name: "Petra Dvořáková"
    km_admin: true           # multiple admins supported
  analyst@company.com:
    display_name: "Anna Kovářová"
    # no km_admin = regular user
```

**Multiple admins** are supported. Conflict resolution: last write wins with
audit trail. No locking — concurrent admin actions are recorded, the most
recent state is authoritative.

`km_admin: true` grants:
- Access to review queue in webapp
- Approve / reject / mandate buttons on items
- Batch operations (select multiple, act on all)
- Edit item content before publishing
- Add "Why this matters" explanation
- Set audience targeting for mandatory items
- View all items including pending and rejected
- View audit log
- Emergency revoke capability

Regular users see:
- Approved and mandatory items only
- Mandatory items highlighted with explanation
- Voting buttons (when governance mode allows)
- "Report issue" button on any item
- Their personal rules list

---

## Item Lifecycle

```
                    ┌─────────┐
   AI extracts ──→  │ PENDING │  (only admins see)
                    └────┬────┘
                         │
              ┌──────────┼──────────┐
              ↓          ↓          ↓
         ┌────────┐ ┌─────────┐ ┌──────────┐
         │APPROVED│ │MANDATORY│ │ REJECTED │
         └───┬────┘ └────┬────┘ └──────────┘
             │           │
             │      ┌────┴────┐
             │      ↓         ↓
             │  ┌───────┐ ┌───────┐
             │  │REVOKED│ │EXPIRED│
             │  └───────┘ └───────┘
             ↓
         catalog
         (opt-in)
```

**Statuses:**
- **pending** — new from AI, waiting for admin review
- **approved** — admin approved, visible in catalog, users can opt-in
- **mandatory** — admin mandated, distributed to target audience automatically
- **rejected** — admin rejected, not visible to anyone (kept for audit)
- **revoked** — was mandatory, emergency pulled by admin (removed from rules on next sync)
- **expired** — past its review date, moved to re-review queue

**Allowed transitions:**
- pending → approved / mandatory / rejected
- approved → mandatory (promote) / rejected (remove)
- mandatory → approved (demote to optional) / revoked (emergency pull)
- rejected → approved (reinstate)
- revoked → approved / mandatory (re-enable after fix)
- expired → approved / mandatory (re-confirmed) / rejected (retire)

**Edited items**: When an admin edits a mandatory item, the item enters
"needs_reapproval" state — it stays distributed but is flagged in admin
dashboard for review. This prevents silent content drift.

---

## Audience Targeting (v1: simple groups)

Mandatory items can target specific groups instead of all users:

```yaml
# In the admin UI when mandating:
audience: "all"                    # everyone (default)
audience: "group:finance"          # only finance team
audience: "group:engineering"      # only engineering
```

Groups are defined in instance.yaml:

```yaml
groups:
  finance:
    label: "Finance & Analytics"
    members: ["analyst1@co.com", "analyst2@co.com"]
  engineering:
    label: "Engineering"
    members: ["dev1@co.com", "dev2@co.com"]
```

This is intentionally simple for v1. Future versions can support
role-based targeting, department hierarchies, or LDAP/SSO group sync.

---

## Audit Log

Every admin action is recorded in an immutable append-only log:

```
/data/corporate-memory/audit.jsonl
```

Each line is a JSON object:
- `timestamp` — when the action happened
- `admin` — who performed it (email)
- `action` — what happened (approved, rejected, mandated, revoked, edited, etc.)
- `item_id` — which item
- `details` — action-specific (e.g., old status, new status, reason, audience)

The audit log is:
- **Append-only** — never edited or truncated (compliance requirement)
- **Separate from knowledge.json** — survives resets
- **Viewable by km_admins** in webapp (filterable by date, admin, action)
- **Exportable** as CSV for compliance reporting

---

## Knowledge Freshness & Expiry

### Review dates

When approving or mandating, admin can optionally set:
- `review_by` — date when item should be re-reviewed (default: 6 months)

Items past their `review_by` date:
- Status changes to **expired**
- Appear in admin's "Needs re-review" queue
- If mandatory: stay distributed until admin acts (no surprise removals)
- Admin can re-confirm (resets review date) or retire (reject)

### Stale detection

System flags items that may be stale:
- Source CLAUDE.local.md files were changed/removed but item wasn't re-extracted
- Item hasn't been re-confirmed in > 12 months (configurable)
- Multiple users flagged "Report issue" on the item

---

## Emergency Controls

### Emergency Revoke

Any km_admin can immediately revoke a mandatory item:
- Status changes to **revoked**
- Rules regenerated for all affected users immediately
- Item removed from `.claude_rules/` on next sync
- Audit log records: who revoked, when, why
- Revoked items visible in admin dashboard with "Revoked" badge

### User "Report Issue" Button

All users (not just admins) can flag any visible item:
- Button on every item in the catalog
- Opens text field for description ("Contains outdated info", "Incorrect SQL", etc.)
- Report goes to km_admins as notification
- Admin can review and act (edit, revoke, reject)
- Prevents situations where admin is unavailable and bad item stays

---

## What Changes for Each Actor

### For the Admin / CEO

**Review Queue (new webapp section)**
- List of pending items with AI-extracted content, category, source users
- **Batch operations**: checkboxes + "Approve selected" / "Reject selected"
- Keyboard shortcuts for fast review
- Filters: category, source user, date, AI confidence
- For each item: Approve / Reject / Mandate buttons
- Mandate requires: "Why this matters" text + audience selection
- Edit button to refine content before publishing
- Dashboard: pending count, approved count, mandatory count, expired count

**Audit & Reporting**
- Audit log viewer (filterable by date, admin, action)
- Export audit log as CSV
- Coverage stats: how many mandatory items, how many users have them

**Notifications**
- After AI collection: "N new items awaiting review"
- When user reports issue on an item
- When items reach their review date

### For the Regular User

**Knowledge Catalog (redesigned)**
- Mandatory items section at top, highlighted with distinct badge
- Each mandatory item shows "Why this matters" explanation from admin
- "Report issue" button on every item
- Below: approved items (browsable, searchable, filterable)
- Voting buttons visible when governance mode allows
- Clean, read-focused UI — no admin clutter

**Rules distribution**
- Mandatory items → automatically in `.claude/rules/` after next sync
- Optional items → user upvotes in hybrid mode (like today)
- Revoked items → automatically removed on next sync
- User doesn't need to do anything for mandatory knowledge — it just appears

### For the AI (Haiku)

Extraction logic stays the same with one addition for threshold mode:
- New optional field in CATALOG_SCHEMA: `confidence` (float 0-1)
- AI rates its confidence that each extracted item is valuable and accurate
- Used by threshold approval mode to auto-publish high-confidence items

---

## Migration from Current System

When upgrading from the current democratic wiki:

1. **Existing knowledge.json items** get `status: "approved"` (not pending —
   they already passed sensitivity check and may have votes)
2. **Existing votes** are preserved (work as before in hybrid/admin_curated modes)
3. **Existing rules** in `.claude_rules/` continue working
4. **No user disruption** — everything looks the same until admin starts curating
5. **Admin enables governance** by setting `distribution_mode` and `approval_mode`
   in instance.yaml — until then, system behaves exactly as today

The migration is **non-breaking and gradual**. An instance can run in legacy
mode indefinitely.

---

## Configuration in instance.yaml

```yaml
corporate_memory:
  # How knowledge reaches users
  # "mandatory_only" — admin controls everything, no user voting
  # "admin_curated" — admin controls, users vote as feedback signal
  # "hybrid" — mandatory from admin + optional from user voting (default)
  distribution_mode: "hybrid"

  # How new AI-extracted items enter the system
  # "review_queue" — nothing published without admin approval (default)
  # "auto_publish" — items go live immediately, admin intervenes retroactively
  # "threshold" — high-confidence auto-publish, low-confidence to review queue
  approval_mode: "review_queue"

  # For threshold mode: minimum AI confidence to auto-publish (0.0-1.0)
  # auto_confidence_threshold: 0.8

  # Default review period for approved/mandatory items (months)
  # Items past this date appear in "Needs re-review" queue
  review_period_months: 6

  # Notify km_admins about new pending items (requires Telegram or email)
  notify_on_new_items: true

# User groups for audience targeting
groups:
  finance:
    label: "Finance & Analytics"
    members: ["analyst1@company.com", "analyst2@company.com"]
  engineering:
    label: "Engineering"
    members: ["dev1@company.com", "dev2@company.com"]
```

---

## What We DON'T Change

- AI extraction logic (collector.py) — stays the same (except optional confidence field)
- Sensitivity filtering — stays the same
- Hash-based change detection — stays the same
- CLAUDE.local.md input mechanism — stays the same
- LLM connector (connectors/llm/) — just built, stays the same
- sync_data.sh mechanism — stays the same
- Timer scheduling — stays the same

We're adding a governance layer BETWEEN extraction and distribution.
The pipes stay the same, we're adding a valve.

---

## Implementation Phases

### Phase 1: Data Model + Audit + Admin API
- Add status, approved_by, mandatory_reason, audience, review_by fields to knowledge.json
- Create audit.jsonl (append-only log)
- New approval API endpoints in webapp (approve, reject, mandate, revoke, edit)
- Batch operations API (approve/reject multiple)
- km_admin flag in users config
- Collector writes new items as "pending" (when review_queue mode)
- Migration logic: existing items get status "approved"

### Phase 2: Admin UI — Review Queue
- Review queue page with batch operations
- Approve/reject/mandate buttons with keyboard shortcuts
- Filters: category, source user, date, confidence
- Edit before publish + "Why this matters" text field
- Audience selection (all / specific group)
- Audit log viewer

### Phase 3: User UI Redesign
- Mandatory items section at top with explanation
- "Report issue" button on all items
- Governance-mode-aware voting visibility
- Revoked items automatically hidden

### Phase 4: Automatic Distribution + Notifications
- Mandatory items → regenerate rules for target users
- Revoked items → remove from rules on next regeneration
- Notification to admins: new pending items, user-reported issues, expiring items
- Expired items → "Needs re-review" queue

### Phase 5: Configuration + Groups
- distribution_mode, approval_mode in instance.yaml
- Groups definition and audience targeting
- All three governance modes tested and working
- Threshold mode with AI confidence scoring

---

## Future Considerations (not in v1)

These were raised in review but are deferred for future versions:

- **Attestation / acknowledgment** — users confirm they read mandatory items
- **Coverage dashboard** — which users have synced, who's behind
- **LDAP/SSO group sync** — automatic group membership from corporate directory
- **Multi-reviewer approval** — 4-eyes rule for mandatory items
- **Version history** — full diff history for edited items
- **Contradiction detection** — flag when two mandatory items conflict
- **Data retention policy** — automatic purge of rejected items after N months
- **GDPR compliance** — right to deletion for personal data in extracted items
