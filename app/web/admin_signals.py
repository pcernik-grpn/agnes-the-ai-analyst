"""Canonical inventory for the `/admin` dashboard's signal rows.

`/admin` used to be a grid of links — a second copy of the sidebar
(`admin_nav.py`) rendered next to the first, answering "where do I go" for a
question the sidebar already answers better. This module is what replaced it:
the dashboard now answers **"what needs me?"**, and every row on it is
declared here.

Single source of truth, in the same idiom as `admin_nav.py` and
`app/resource_types.py`'s `ResourceTypeSpec`, so `tests/test_admin_signals.py`
can import it directly and assert the invariants below hold for every entry.

Two zones, in strict priority order:

    ZONE_NEEDS_YOU     decisions only — a human must approve, reject, or
                       verify something. Cheap COUNT-shaped resolvers;
                       resolved inline during the `/admin` render.
    ZONE_NEEDS_FIXING  breakage, not decisions — something is failing and
                       wants an operator, not a verdict. Resolved
                       out-of-band by `GET /api/admin/dashboard/signals`
                       after first paint (see `app/services/admin_dashboard.py`),
                       because these read audit/history tables that are the
                       largest in the instance.

Three rules hold for every spec here. They are what keep the dashboard worth
reading six months from now, and each is enforced by a guard test:

1. **Zero renders nothing.** A resolver returns ``None`` — not ``Signal(0)``
   — when there is nothing to do, or when the feature backing it is disabled
   on this instance. A dashboard showing twelve zeros trains admins to stop
   looking at it; the empty state is a one-line "nothing needs your
   attention", which is the state you *want* to be in.

2. **The count and its destination come from one constant.** Where a row
   links into a filtered queue, the filter in the href is built from the same
   module-level tuple the count queried (see `_SUBMISSION_REVIEW_STATUSES`).
   Hand-typing the status list twice is how a dashboard starts disagreeing
   with the page it links to, and nobody notices for a quarter.

3. **A row must land somewhere the work can actually be done.** Every `href`
   points at the page that owns the action — the dashboard routes, it never
   rebuilds a queue. This rule is why memory-domain suggestions are NOT here
   despite having a complete admin API
   (`/api/admin/memory-domain-suggestions`, incl. `count-pending`, `approve`,
   `reject`): no admin page renders that queue, so a row for it would show a
   number and then strand the caller. It belongs here the day the review UI
   ships.

Resolvers are also individually isolated at call time (see
`app/services/admin_dashboard.py`): one raising resolver degrades to a single
"unavailable" row, it never 500s the admin's home page.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

ZONE_NEEDS_YOU = "needs_you"
ZONE_NEEDS_FIXING = "needs_fixing"

ZONES: tuple[str, ...] = (ZONE_NEEDS_YOU, ZONE_NEEDS_FIXING)

# No ZONE_LABELS map here on purpose: the two headings are written into
# `admin_hub.html` next to the copy that explains them, and a constant read by
# exactly one template is indirection without a payer.

# Rendering tone. 'action' = a decision is waiting (neutral — an approval
# queue with items in it is normal operation, not a problem); 'warn' /
# 'error' = something is degraded or broken.
SEVERITIES: tuple[str, ...] = ("action", "warn", "error")

# --- shared thresholds ------------------------------------------------------
# Windows the operator agreed to: failed syncs in the last 24h, marketplaces
# with an error or no successful sync in 48h, error rate over the last 7d.
SYNC_WINDOW_HOURS = 24
STALE_MARKETPLACE_SYNC_HOURS = 48  # matches app/api/admin_reports.py
ERROR_RATE_WINDOW_DAYS = 7
# A tool is "erroring" past this share of its invocations. Below it, a couple
# of failures in a busy tool is noise, not a signal worth an admin's morning.
ERROR_RATE_THRESHOLD = 0.10
# Ignore tools with almost no traffic — 1 error out of 2 calls is 50% and
# means nothing.
ERROR_RATE_MIN_INVOCATIONS = 20

# The verdict set that the /admin/store/submissions "Needs review" chip uses.
# Rule 2: the count below and the `?status=` in its href are both built from
# this tuple, so the number on the dashboard always equals the row count on
# the page it opens. `blocked_inline` (the Rescan-flow state) MUST stay in —
# dropping it silently undercounts.
_SUBMISSION_REVIEW_STATUSES: tuple[str, ...] = (
    "blocked_inline",
    "blocked_llm",
    "review_error",
)


@dataclass(frozen=True)
class Signal:
    """A resolved row. ``count`` is always > 0 — see rule 1."""

    count: int
    href: str
    blurb: str


@dataclass(frozen=True)
class SignalSpec:
    key: str
    title: str
    zone: str
    severity: str
    resolve: Callable[[], Optional[Signal]]


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Zone 1 — Needs you (decisions)
# ---------------------------------------------------------------------------


def _resolve_store_verification() -> Optional[Signal]:
    from app.instance_config import get_store_moderation_enabled, get_store_verification_enabled
    from src.repositories import store_entities_repo

    if not get_store_verification_enabled():
        # Feature off on this instance — not "zero waiting", but "not a thing
        # here". Either way the row must not render (rule 1).
        return None
    # The hub this card links to is hidden by default, and it is where the
    # Verify / Request changes actions are reached from. A count pointing at
    # a redirect is worse than no card — same posture as
    # `_resolve_studio_suggestions`.
    if not get_store_moderation_enabled():
        return None
    # limit=1: we want the repo's real total, not the rows.
    _, total = store_entities_repo().list(verification_state=["requested"], limit=1)
    if not total:
        return None
    return Signal(
        count=total,
        href="/admin/store",
        blurb=f"{_plural(total, 'author has', 'authors have')} requested verification.",
    )


def _resolve_store_submissions() -> Optional[Signal]:
    from src.repositories import store_submissions_repo

    _, total = store_submissions_repo().list_for_admin(
        status=list(_SUBMISSION_REVIEW_STATUSES),
        limit=1,
    )
    if not total:
        return None
    # Rule 2 — same tuple drives the count and the destination's filter.
    href = "/admin/store/submissions?status=" + ",".join(_SUBMISSION_REVIEW_STATUSES)
    return Signal(
        count=total,
        href=href,
        blurb=f"{_plural(total, 'submission needs', 'submissions need')} review before publishing.",
    )


def _resolve_agent_share_requests() -> Optional[Signal]:
    """Track C6 — agent-sharing approval queue. PG-only (A3 ratchet): on a
    DuckDB-backed instance the feature simply doesn't exist here (not
    "broken"), so this returns `None` BEFORE calling the repo rather than
    letting `RequiresPostgresBackend` degrade the row to "could not be
    checked" forever — same posture as `_resolve_studio_suggestions`'s
    feature-flag early-return."""
    from app.instance_config import get_store_moderation_enabled
    from src.repositories import share_requests_repo, use_pg

    if not use_pg():
        return None
    # `/admin/store` is the ONLY page that renders this queue, and it is hidden
    # by default — so with the hub off there is nowhere for this card to send
    # anyone. Checked before the count, like the flag guards above.
    if not get_store_moderation_enabled():
        return None
    _, total = share_requests_repo().list_for_admin(status=["pending"], limit=1)
    if not total:
        return None
    return Signal(
        count=total,
        href="/admin/store",
        blurb=f"agent {_plural(total, 'share needs', 'shares need')} approval.",
    )


def _resolve_memory_items() -> Optional[Signal]:
    from src.repositories import knowledge_repo

    total = knowledge_repo().count_items(statuses=["pending"])
    if not total:
        return None
    return Signal(
        count=total,
        href="/admin/corporate-memory",
        blurb=f"corporate-memory {_plural(total, 'item is', 'items are')} awaiting review.",
    )


def _resolve_studio_suggestions() -> Optional[Signal]:
    from app.instance_config import get_studio_enabled
    from src.repositories import authoring_suggestions_repo

    # The queue page reads this same flag and redirects home when it is off
    # (Studio is off by DEFAULT since the admin cleanup), so a card here would
    # be a count nobody can act on pointing at a redirect. Checked before the
    # count, not after: on a disabled instance there is nothing to ask.
    if not get_studio_enabled():
        return None

    total = authoring_suggestions_repo().count_pending()
    if not total:
        return None
    return Signal(
        count=total,
        href="/admin/studio/suggestions",
        blurb=f"authoring {_plural(total, 'suggestion is', 'suggestions are')} waiting to be replayed.",
    )


def _resolve_ungranted_plugins() -> Optional[Signal]:
    """Plugins ingested but granted to no group — "indexed but invisible".

    Ingesting content and granting it to nobody has no legitimate steady
    state: every non-admin sees an absence, and nothing anywhere says why
    (TCRD-221 — this exact silence ate ~50 minutes of a live walkthrough).
    Admin-disabled plugins don't count (deliberately hidden, not a mistake)
    and neither do system plugins (``mark_system`` materializes their grant
    for every group, so they cannot be orphaned without also tripping the
    disabled path)."""
    from src.repositories import marketplace_plugins_repo, resource_grants_repo

    granted = {g["resource_id"] for g in resource_grants_repo().list_all(resource_type="marketplace_plugin")}
    orphans = [
        p
        for p in marketplace_plugins_repo().list_all()
        if not p.get("admin_disabled")
        and not p.get("is_system")
        and f"{p['marketplace_id']}/{p['name']}" not in granted
    ]
    if not orphans:
        return None
    return Signal(
        count=len(orphans),
        href="/admin/access",
        blurb=f"{_plural(len(orphans), 'plugin is', 'plugins are')} granted to no group — nobody can see "
        + (f"{orphans[0]['name']}." if len(orphans) == 1 else "them."),
    )


def _resolve_store_lint() -> Optional[Signal]:
    from src.repositories import store_lint_repo

    # Dismissed findings are excluded by default — an admin who waved one off
    # should not be shown it again every morning.
    findings = store_lint_repo().all_latest_findings(include_dismissed=False)
    total = len(findings)
    if not total:
        return None
    entities = len({f.get("entity_id") for f in findings})
    return Signal(
        count=total,
        href="/admin/store/lint",
        blurb=f"open lint {_plural(total, 'finding', 'findings')} across "
        f"{entities} {_plural(entities, 'entity', 'entities')}.",
    )


# ---------------------------------------------------------------------------
# Zone 2 — Needs fixing (breakage)
# ---------------------------------------------------------------------------


def _resolve_vault_key_missing() -> Optional[Signal]:
    """No vault key — this instance cannot connect any data source at all.

    `#ds-add-btn` on /admin/data-sources is `disabled` without
    `AGNES_VAULT_KEY`, so the whole data path is shut, and nothing on /admin
    said so: step 1 of the setup chain linked straight to the page that
    refuses. Docker and GCP deploys mint the key on boot, so this fires on
    the manual install path — the one QUICKSTART addresses.

    Uses `can_store_secrets()`, the same predicate the write path guards on,
    rather than `vault_key_configured()` — the narrower question answers
    False in LOCAL_DEV_MODE where the write would in fact succeed, and a
    dashboard error on a working dev instance is noise.
    """
    from app.secrets_vault import can_store_secrets

    if can_store_secrets():
        return None
    return Signal(
        count=1,
        href="/admin/data-sources",
        blurb="no vault key is set, so no data source can be connected. Set AGNES_VAULT_KEY and restart.",
    )


def _resolve_sources_without_credential() -> Optional[Signal]:
    """Connections that exist but hold no credential.

    A connection whose token failed validation is still a row, and every
    other surface counted rows: the setup chain called it "connected" and the
    wizard offered to go straight to its tables. Derived exactly as the
    source card derives `has_secret`, so the two cannot disagree.
    """
    from src.repositories import connection_secrets_repo, source_connections_repo

    secrets = connection_secrets_repo()
    broken = 0
    for row in source_connections_repo().list():
        if (row.get("token_env") or "").strip():
            continue
        try:
            if not secrets.has(row["id"]):
                broken += 1
        except Exception:  # noqa: BLE001
            # Cannot prove it works — say so rather than claim health.
            broken += 1
    if not broken:
        return None
    return Signal(
        count=broken,
        href="/admin/data-sources",
        blurb=(
            f"{_plural(broken, 'source has', 'sources have')} no credential stored — "
            f"{_plural(broken, 'it', 'they')} cannot read any table."
        ),
    )


def _resolve_sync_failures() -> Optional[Signal]:
    from src.repositories import sync_state_repo

    counts = sync_state_repo().status_counts_since(_now() - timedelta(hours=SYNC_WINDOW_HOURS))
    # Anything that isn't 'ok' is a failure — same fold the Activity Center's
    # health pulse uses (app/api/activity.py::_compute_health), so the two
    # surfaces can never disagree about what "failing" means.
    fail = sum(c for s, c in counts.items() if s and s != "ok")
    if not fail:
        return None
    return Signal(
        count=fail,
        href="/admin/sync",
        blurb=f"sync {_plural(fail, 'run', 'runs')} failed in the last {SYNC_WINDOW_HOURS}h.",
    )


def _resolve_marketplace_sync() -> Optional[Signal]:
    from src.repositories import marketplace_registry_repo

    cutoff = _now() - timedelta(hours=STALE_MARKETPLACE_SYNC_HOURS)
    broken = 0
    # list_non_builtin: a bundled row never syncs (no git remote), so its
    # NULL last_synced_at would read as "broken" forever on every instance
    # — the same permanent-red wound TCRD-219 closes in the admin table.
    for row in marketplace_registry_repo().list_non_builtin():
        if row.get("last_error"):
            broken += 1
            continue
        synced_at = row.get("last_synced_at")
        if synced_at is None:
            # Registered but never synced once.
            broken += 1
            continue
        if synced_at.tzinfo is None:
            synced_at = synced_at.replace(tzinfo=timezone.utc)
        if synced_at < cutoff:
            broken += 1
    if not broken:
        return None
    return Signal(
        count=broken,
        href="/admin/marketplaces",
        blurb=f"curated {_plural(broken, 'marketplace has', 'marketplaces have')} an error or "
        f"no sync in {STALE_MARKETPLACE_SYNC_HOURS}h.",
    )


# NOT a signal: dead-letter jobs (`jobs_repo().list(status="failed")`).
#
# Rule 3 rules them out for now, the same way it rules out memory-domain
# suggestions. There is no admin page over the `jobs` table. The nearest
# candidate — /admin/activity filtered to `resource_prefix=job:` — looks right
# and is not: those audit rows are admin-TRIGGERED scheduler runs written by
# `app/api/admin.py` (`job:session-collector`, `job:knowledge-digests`, …),
# a different thing from the durable work-queue rows that exhaust their
# retries. A row counting one and linking to the other would send an admin to
# a page where the items it named do not appear, which is worse than not
# mentioning them: it spends the trust the dashboard needs to be worth
# opening. Add this the day the queue has a surface.


def _resolve_error_rate() -> Optional[Signal]:
    from src.repositories import usage_repo

    cutoff = _now() - timedelta(days=ERROR_RATE_WINDOW_DAYS)
    hot = [
        r
        for r in usage_repo().summary_error_rate(cutoff, limit=25)
        if r["invocations"] >= ERROR_RATE_MIN_INVOCATIONS and r["rate"] >= ERROR_RATE_THRESHOLD
    ]
    if not hot:
        return None
    worst = max(hot, key=lambda r: r["rate"])
    pct = round(worst["rate"] * 100)
    return Signal(
        count=len(hot),
        href="/admin/telemetry",
        blurb=f"{_plural(len(hot), 'tool is', 'tools are')} erroring above "
        f"{round(ERROR_RATE_THRESHOLD * 100)}% — worst is {worst['tool_name']} at {pct}%.",
    )


ADMIN_SIGNALS: list[SignalSpec] = [
    # --- Needs you ---------------------------------------------------------
    SignalSpec(
        key="store_verification",
        title="Verification requests",
        zone=ZONE_NEEDS_YOU,
        severity="action",
        resolve=_resolve_store_verification,
    ),
    SignalSpec(
        key="store_submissions",
        title="Submissions to review",
        zone=ZONE_NEEDS_YOU,
        severity="action",
        resolve=_resolve_store_submissions,
    ),
    SignalSpec(
        key="agent_share_requests",
        title="Agent shares to approve",
        zone=ZONE_NEEDS_YOU,
        severity="action",
        resolve=_resolve_agent_share_requests,
    ),
    SignalSpec(
        key="memory_items",
        title="Memory items pending",
        zone=ZONE_NEEDS_YOU,
        severity="action",
        resolve=_resolve_memory_items,
    ),
    SignalSpec(
        key="studio_suggestions",
        title="Studio suggestions",
        zone=ZONE_NEEDS_YOU,
        severity="action",
        resolve=_resolve_studio_suggestions,
    ),
    SignalSpec(
        key="ungranted_plugins",
        title="Plugins nobody can see",
        zone=ZONE_NEEDS_YOU,
        severity="warn",
        resolve=_resolve_ungranted_plugins,
    ),
    SignalSpec(
        key="store_lint",
        title="Open lint findings",
        zone=ZONE_NEEDS_YOU,
        severity="warn",
        resolve=_resolve_store_lint,
    ),
    # --- Needs fixing ------------------------------------------------------
    # Configuration health comes FIRST in this zone. The dashboard had eight
    # signals — five content-moderation, three operational — and none about
    # broken configuration, so "Nothing needs your attention" sat above a
    # credential-less connection and a source that had never synced.
    # `_resolve_sync_failures` cannot cover it: it counts sync RUNS with a
    # non-ok status, and a source that never ran produces none.
    SignalSpec(
        key="vault_key_missing",
        title="Vault key not set",
        zone=ZONE_NEEDS_FIXING,
        severity="error",
        resolve=_resolve_vault_key_missing,
    ),
    SignalSpec(
        key="sources_without_credential",
        title="Sources without a credential",
        zone=ZONE_NEEDS_FIXING,
        severity="error",
        resolve=_resolve_sources_without_credential,
    ),
    SignalSpec(
        key="sync_failures",
        title="Failed syncs",
        zone=ZONE_NEEDS_FIXING,
        severity="error",
        resolve=_resolve_sync_failures,
    ),
    SignalSpec(
        key="marketplace_sync",
        title="Marketplace sync",
        zone=ZONE_NEEDS_FIXING,
        severity="error",
        resolve=_resolve_marketplace_sync,
    ),
    SignalSpec(
        key="error_rate",
        title="Tool error rate",
        zone=ZONE_NEEDS_FIXING,
        severity="warn",
        resolve=_resolve_error_rate,
    ),
]


def signals_for_zone(zone: str) -> list[SignalSpec]:
    """Specs in *zone*, in declaration order (which is display order)."""
    return [s for s in ADMIN_SIGNALS if s.zone == zone]
