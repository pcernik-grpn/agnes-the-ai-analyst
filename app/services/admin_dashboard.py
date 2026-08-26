"""Resolution layer for the `/admin` dashboard signals declared in
`app/web/admin_signals.py`.

Two things live here that the spec list deliberately does not:

**Isolation.** Every resolver runs inside its own try/except. `/admin` is the
page an admin lands on when something is already wrong, so it is exactly the
page that must not 500 because one repo raised. A failing resolver degrades to
a single row rendered as "unavailable" and the other eight still render.

**Cost control.** Zone 1 resolvers are COUNT-shaped and run inline during the
page render. Zone 2 resolvers read `sync_history` and `usage_events` — the
tables that grow without bound on a busy instance — so they are fetched
after first paint via `GET /api/admin/dashboard/signals` and memoised behind a
short process-local TTL. The TTL is what stops a tab left open on `/admin`
(or three admins during an incident) from turning a dashboard into a load
source; it is deliberately short enough that an admin who fixes a failing sync
and refreshes sees it clear within the minute.

The cache is per-process and unsynchronised across replicas on purpose: it
memoises a read-only rollup, so the worst case of a cold replica is one extra
aggregate query, not an inconsistency anyone can observe.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from app.web.admin_signals import (
    ADMIN_SIGNALS,
    ZONE_NEEDS_FIXING,
    ZONE_NEEDS_YOU,
    Signal,
    SignalSpec,
    signals_for_zone,
)

logger = logging.getLogger(__name__)

# Zone 2 only. Zone 1 is cheap and always fresh — an approval queue that keeps
# showing a submission the admin just approved is worse than one extra COUNT.
_ZONE_FIXING_TTL_SECONDS = 60


@dataclass(frozen=True)
class ResolvedSignal:
    """A spec plus its outcome, ready to render.

    ``signal is None and not failed`` means "nothing to report" — the row is
    dropped by `resolve_zone`. ``failed`` means the resolver raised; the row
    survives so the admin knows a check is broken rather than clear.
    """

    key: str
    title: str
    zone: str
    severity: str
    signal: Optional[Signal]
    failed: bool = False

    @property
    def count(self) -> int:
        return self.signal.count if self.signal else 0

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "zone": self.zone,
            "severity": self.severity,
            "failed": self.failed,
            "count": self.count,
            "href": self.signal.href if self.signal else None,
            "blurb": self.signal.blurb if self.signal else "Could not be checked.",
        }


def _resolve_one(spec: SignalSpec) -> Optional[ResolvedSignal]:
    try:
        signal = spec.resolve()
    except Exception:
        # Never let one bad signal take the admin's home page with it.
        logger.warning("admin dashboard signal %r failed to resolve", spec.key, exc_info=True)
        return ResolvedSignal(
            key=spec.key,
            title=spec.title,
            zone=spec.zone,
            severity="warn",
            signal=None,
            failed=True,
        )
    if signal is None:
        return None
    return ResolvedSignal(
        key=spec.key,
        title=spec.title,
        zone=spec.zone,
        severity=spec.severity,
        signal=signal,
    )


def resolve_zone(zone: str) -> list[ResolvedSignal]:
    """Every signal in *zone* that has something to say, in declaration order.

    Clear signals are dropped entirely (rule 1 in `admin_signals`), so an
    empty list is the "nothing needs your attention" state and the caller
    renders it as such.
    """
    out = []
    for spec in signals_for_zone(zone):
        resolved = _resolve_one(spec)
        if resolved is not None:
            out.append(resolved)
    return out


def resolve_needs_you() -> list[ResolvedSignal]:
    """Zone 1, resolved inline during the `/admin` render."""
    return resolve_zone(ZONE_NEEDS_YOU)


# --- Zone 2 cache -----------------------------------------------------------

_cache_lock = threading.Lock()
_cache_value: Optional[list[ResolvedSignal]] = None
_cache_at: float = 0.0


def resolve_needs_fixing(*, force: bool = False) -> list[ResolvedSignal]:
    """Zone 2, memoised for `_ZONE_FIXING_TTL_SECONDS`.

    The lock is held across resolution so a burst of concurrent requests on a
    cold cache produces ONE pass over the audit/history tables rather than one
    per caller — the stampede is the whole reason this is cached.
    """
    global _cache_value, _cache_at
    now = time.monotonic()
    with _cache_lock:
        if not force and _cache_value is not None and (now - _cache_at) < _ZONE_FIXING_TTL_SECONDS:
            return _cache_value
        _cache_value = resolve_zone(ZONE_NEEDS_FIXING)
        _cache_at = time.monotonic()
        return _cache_value


def invalidate_cache() -> None:
    """Drop the Zone-2 cache. Used by tests, which must not inherit a rollup
    computed against a previous fixture's data."""
    global _cache_value, _cache_at
    with _cache_lock:
        _cache_value = None
        _cache_at = 0.0


def signal_keys() -> list[str]:
    """Every declared key — used by the guard test and for debugging."""
    return [s.key for s in ADMIN_SIGNALS]


# ---------------------------------------------------------------------------
# Journey — the guided setup chain on `/admin`.
#
# A different contract from the signal zones above, which is why these are NOT
# SignalSpecs: rule 1 there is "zero renders nothing", while this chain always
# has something to say — the healthy state ("every table is in a package") is
# information, not noise, because it is People → Data → Access read end to end.
#
# ONE list, not two. This layer shipped as a four-step setup path ABOVE three
# gap cards, which stated the same facts twice in different words: the path's
# "Share with people — grant packages to groups" sat directly above a card
# reading "4 packages are shared with nobody", and an admin had to work out
# that they were the same sentence. The path also retired itself for good once
# its four stages were done, so the guidance vanished exactly when a second
# admin inherited the instance. Now: six ordered steps, each carrying its own
# counts AND its own gap line, and the panel graduates to a one-line summary
# it can be reopened from instead of disappearing.
#
# Each step answers three questions in this order — what state it is in, WHY it
# exists at all (the dependency the product otherwise leaves invisible), and
# where the work is done. The "why" is the difference between a checklist and
# onboarding, and it is the same rule the analyst-side journey follows
# (`static/js/chat_onboarding.js`'s STEP_META).
#
# Same isolation rule as the zones: each AREA resolves inside its own
# try/except and every step that reads a failed area degrades to a `failed`
# marker, never a 500 — `/admin` is the page an admin opens when something is
# already wrong. Areas (not steps) are the unit of isolation so that the
# numbers shared by several steps are read once and can never disagree with
# each other mid-page.
# ---------------------------------------------------------------------------

# `query_mode` values whose parquet is distributed to analysts via data
# packages (`agnes pull`). Blank/NULL reads as `local`, the same fold the
# manifest and the distribution gate use (see `_assert_distributable` in
# app/api/data.py). `remote` tables are reachable without a package
# (server-side execution), so they are deliberately NOT counted as
# "unreachable" when unpackaged.
_DISTRIBUTABLE_QUERY_MODES = ("", "local", "materialized")


def _is_distributable(row: dict) -> bool:
    return (row.get("query_mode") or "") in _DISTRIBUTABLE_QUERY_MODES


def _data_package_grants() -> list[dict]:
    from src.repositories import resource_grants_repo

    return resource_grants_repo().list_all(resource_type="data_package")


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


# --- Areas: the three reads the six steps are composed from ----------------


def _area_data() -> dict:
    """Sources, registered tables, packages, and the tables that reach nobody."""
    from src.repositories import (
        data_packages_repo,
        source_connections_repo,
        table_registry_repo,
    )

    tables = [t for t in table_registry_repo().list_all() if (t.get("source_type") or "") != "internal"]
    packages_repo = data_packages_repo()
    member_ids = packages_repo.list_member_ids_bulk()
    packaged: set[str] = set()
    for ids in member_ids.values():
        packaged.update(ids)
    return {
        "sources": len(source_connections_repo().list()),
        "tables": len(tables),
        "packages": len(packages_repo.list()),
        "packages_with_tables": sum(1 for ids in member_ids.values() if ids),
        "unpackaged": sum(1 for t in tables if _is_distributable(t) and t["id"] not in packaged),
    }


def _area_people() -> dict:
    """Accounts, groups, and the people no grant reaches.

    ``uncovered`` is the People→Access link of the chain: an account in no
    group that holds a data-package grant can sign in and see nothing.
    """
    from src.repositories import (
        user_group_members_repo,
        user_groups_repo,
        users_repo,
    )

    groups = user_groups_repo().list_all()
    active_users = [u for u in users_repo().list_all() if u.get("active", True)]
    granted_group_ids = {g["group_id"] for g in _data_package_grants()}

    everyone_ids = {g["id"] for g in groups if g.get("is_system") and g.get("name") == "Everyone"}
    admin_ids = {g["id"] for g in groups if g.get("is_system") and g.get("name") == "Admin"}

    members = user_group_members_repo()
    if granted_group_ids & everyone_ids:
        # Auto-membership: a grant to Everyone covers every account.
        uncovered = 0
        covered_count = len(active_users)
    else:
        covered: set[str] = set()
        for gid in granted_group_ids | admin_ids:  # admins have god-mode
            # `list_members_for_group` joins users — `id` is the USER id.
            covered.update(m["id"] for m in members.list_members_for_group(gid))
        covered_count = sum(1 for u in active_users if u["id"] in covered)
        uncovered = len(active_users) - covered_count

    return {
        "people": len(active_users),
        "groups": len(groups),
        "covered": covered_count,
        "uncovered": uncovered,
    }


def _area_access() -> dict:
    """Packages, the ones a group can actually reach, and the grants doing it."""
    from src.repositories import data_packages_repo

    packages = data_packages_repo().list()
    grants = _data_package_grants()
    granted_ids = {g["resource_id"] for g in grants}
    shared = sum(1 for p in packages if p["id"] in granted_ids)
    return {
        "packages": len(packages),
        "shared": shared,
        "unshared": len(packages) - shared,
        "grants": len(grants),
        "groups_granted": len({g["group_id"] for g in grants}),
    }


def _safe(label: str, resolver) -> Optional[dict]:
    """Run an area resolver, or log and return None. One broken backend must
    degrade the steps that read it, never the page."""
    try:
        return resolver()
    except Exception:
        logger.exception("admin journey: area %r failed to resolve", label)
        return None


# --- The six steps ---------------------------------------------------------
#
# `_step` builds one row. `detail` is the state of the world in a sentence
# ("11 tables registered"), `why` is why the step exists at all, and `health`
# is the gap this step owns — the number that says the step is done but not
# yet TRUE ("4 packages are shared with nobody").


def _step(
    key: str,
    title: str,
    *,
    done: bool,
    detail: str,
    why: str,
    href: str,
    cta: str,
    area: Optional[dict],
    done_cta: Optional[str] = None,
    done_href: Optional[str] = None,
    facts: Optional[list] = None,
    health: Optional[dict] = None,
    aside: Optional[dict] = None,
) -> dict:
    failed = area is None
    return {
        "key": key,
        "title": title,
        # A step whose area is unreadable is neither done nor a safe place to
        # send someone: it renders as "could not be checked".
        "done": bool(done) and not failed,
        "failed": failed,
        "detail": detail if not failed else "Could not be checked — see the server log.",
        "why": why,
        "facts": facts or [],
        "health": health,
        "href": href,
        # `cta` is the verb while the step is open ("Connect a source"); once
        # it is done that verb is a lie about what the click will do, so a done
        # row shows the maintenance label instead ("Add another source").
        "cta": cta,
        "done_cta": done_cta or cta,
        # …and when the maintenance verb names a DIFFERENT place, the click has
        # to follow it: "Bundle tables" belongs on the Tables lens, "Manage
        # packages" on the Packages workspace. Sharing one href made the done
        # row's label a promise the click broke — the same defect the verify
        # step had with "Simulate a person".
        "done_href": done_href or href,
        "aside": aside,
    }


def _build_steps(data: Optional[dict], people: Optional[dict], access: Optional[dict]) -> list[dict]:
    d = data or {}
    p = people or {}
    a = access or {}

    unpackaged = d.get("unpackaged", 0)
    uncovered = p.get("uncovered", 0)
    unshared = a.get("unshared", 0)

    steps = [
        _step(
            "connect",
            "Connect a source",
            area=data,
            done=bool(d.get("sources")) or bool(d.get("tables")),
            detail=(
                f"{d.get('sources', 0)} {_plural(d.get('sources', 0), 'source is', 'sources are')} connected."
                if d.get("sources")
                else "Keboola, BigQuery, Jira, or files you upload."
            ),
            why=(
                "A connection is this instance's credential for reading a system you already run. "
                "One connection can carry hundreds of tables, and you can add more sources later."
            ),
            facts=[{"n": d.get("sources", 0), "label": _plural(d.get("sources", 0), "source", "sources")}],
            href="/admin/data-sources?add=1",
            cta="Connect a source",
            done_cta="Add another source",
        ),
        _step(
            "tables",
            "Choose the tables",
            area=data,
            done=bool(d.get("tables")),
            detail=(
                f"{d.get('tables', 0)} {_plural(d.get('tables', 0), 'table is', 'tables are')} registered."
                if d.get("tables")
                else "Nothing registered yet."
            ),
            why=(
                "A source can hold thousands of tables; only the ones you register here are managed — "
                "which is what keeps sync time, storage, and the analyst's catalog under control."
            ),
            facts=[{"n": d.get("tables", 0), "label": _plural(d.get("tables", 0), "table", "tables")}],
            href="/admin/data-sources?add=1",
            cta="Choose tables",
            done_cta="Add more tables",
            aside={"href": "/admin/tables", "label": "See every registered table"},
        ),
        _step(
            "package",
            "Bundle tables into packages",
            area=data,
            done=bool(d.get("packages_with_tables")),
            detail=(
                f"{d.get('packages_with_tables', 0)} "
                f"{_plural(d.get('packages_with_tables', 0), 'package holds', 'packages hold')} tables."
                if d.get("packages_with_tables")
                else "No package holds a table yet."
            ),
            why=(
                "A data package is the unit people receive — you grant packages, not tables. "
                "A table in no package can't be shared and can't be pulled, which is the dependency "
                "most instances discover late."
            ),
            facts=[
                {"n": d.get("packages", 0), "label": _plural(d.get("packages", 0), "package", "packages")},
            ],
            health=(
                {
                    "level": "warn",
                    "text": (
                        f"{unpackaged} {_plural(unpackaged, 'table is', 'tables are')} in no package — "
                        f"{_plural(unpackaged, 'it', 'they')} cannot reach anyone"
                    ),
                    "href": "/admin/tables",
                }
                if unpackaged
                else {"level": "ok", "text": "Every distributable table is in a package."}
            ),
            href="/admin/tables",
            cta="Bundle tables",
            done_cta="Manage packages",
            # Bundling happens on the Tables lens (that is where the tables
            # are); managing the packages themselves is the Packages
            # workspace's whole job.
            done_href="/admin/data-packages",
        ),
        _step(
            "people",
            "Get your people in",
            area=people,
            # One account is the admin who is reading this page. The step is
            # about the OTHER people — an instance nobody else can sign into
            # has not finished setup, however healthy its data is.
            done=p.get("people", 0) > 1,
            detail=(
                f"{p.get('people', 0)} {_plural(p.get('people', 0), 'person', 'people')} · "
                f"{p.get('groups', 0)} {_plural(p.get('groups', 0), 'group', 'groups')}."
                if p.get("people")
                else "Only your own account so far."
            ),
            why=(
                "Accounts appear when people sign in through your identity provider. "
                "Grants attach to GROUPS, not to people, so a group you grant once keeps working "
                "for everyone who joins it later."
            ),
            facts=[
                {"n": p.get("people", 0), "label": _plural(p.get("people", 0), "person", "people")},
                {"n": p.get("groups", 0), "label": _plural(p.get("groups", 0), "group", "groups")},
            ],
            health=(
                {
                    "level": "warn",
                    "text": (
                        f"{uncovered} {_plural(uncovered, 'person is', 'people are')} in no group "
                        "that holds a data package"
                    ),
                    "href": "/admin/access",
                }
                if uncovered
                else {"level": "ok", "text": "Everyone with an account can reach at least one package."}
            ),
            href="/admin/users",
            cta="Invite people",
            done_cta="Manage people",
            aside={"href": "/admin/access", "label": "Manage groups"},
        ),
        _step(
            "share",
            "Share packages with groups",
            area=access,
            done=bool(a.get("grants")),
            detail=(
                f"{a.get('shared', 0)} of {a.get('packages', 0)} "
                f"{_plural(a.get('packages', 0), 'package', 'packages')} shared with "
                f"{a.get('groups_granted', 0)} {_plural(a.get('groups_granted', 0), 'group', 'groups')}."
                if a.get("grants")
                else "No package is shared yet."
            ),
            why=(
                "This is the step that makes data reachable. Optional means the package shows in "
                "their Library and they add it themselves; Automatic lands in their workspace on the "
                "next sync — either way their next pull downloads the data."
            ),
            facts=[
                {"n": a.get("shared", 0), "label": "shared"},
                {"n": a.get("packages", 0), "label": "packages"},
            ],
            health=(
                {
                    "level": "warn",
                    "text": (
                        f"{unshared} {_plural(unshared, 'package is', 'packages are')} shared with nobody "
                        "— invisible to analysts"
                    ),
                    "href": "/admin/data-packages",
                }
                if unshared
                else {"level": "ok", "text": "Every package is shared with at least one group."}
            ),
            href="/admin/data-packages",
            cta="Share a package",
            done_cta="Manage sharing",
            aside={"href": "/admin/access", "label": "Edit access by group"},
        ),
    ]

    # Step six is the chain read back to front: it is done only when nothing
    # earlier is merely "done" while still broken. It is deliberately the one
    # step whose completion an admin cannot tick by visiting a page — "did my
    # change actually reach anyone?" is the question the product could not
    # answer at all before.
    chain_ok = all(s["done"] for s in steps) and not (unpackaged or uncovered or unshared)
    reach = min(p.get("covered", 0), p.get("people", 0))
    steps.append(
        _step(
            "verify",
            "Check what they'll see",
            # It reads all three areas, so any one of them being unreadable
            # makes this row a "could not be checked", never a green tick.
            area=(access if None not in (data, people, access) else None),
            done=chain_ok,
            # Reports what HAS reached people even while the step is open —
            # "nothing has reached anyone" would be a plain falsehood on an
            # instance whose only unfinished step is "invite your team".
            detail=(
                f"{reach} {_plural(reach, 'person', 'people')} can pull "
                f"{a.get('shared', 0)} {_plural(a.get('shared', 0), 'package', 'packages')}"
                f"{'.' if chain_ok else ' so far.'}"
                if reach and a.get("shared")
                else "Nothing has reached anyone yet."
            ),
            why=(
                "Simulate a person to see the instance exactly as their groups leave it — the packages, "
                "tools and library they get — before they tell you something is missing."
            ),
            facts=[{"n": reach, "label": "reached"}],
            # The Simulate LENS, not the Access workspace: Simulate is a real
            # URL (`?lens=simulate`), and a CTA that says "Simulate a person"
            # while landing on the grant editor is the one broken promise this
            # step cannot afford — seeing it as they see it is its whole job.
            href="/admin/access?lens=simulate",
            cta="Simulate a person",
        )
    )
    return steps


def resolve_journey() -> dict:
    """The guided setup chain for `/admin` — every count from existing repo
    reads, resolved inline (they are all COUNT/short-list shaped).

    Returns ``{"setup": {...}}`` where ``setup`` carries the ordered steps,
    the progress fraction, and a one-line summary for the graduated state.
    ``setup`` is None only when the whole build raised; individual steps whose
    area is unreadable survive as ``failed`` so a broken check never reads as
    a healthy chain.
    """
    try:
        data = _safe("data", _area_data)
        people = _safe("people", _area_people)
        access = _safe("access", _area_access)
        steps = _build_steps(data, people, access)

        # The first not-done, not-failed step is where the CTA belongs; later
        # steps stay CTA-less so the panel reads as a sequence rather than six
        # simultaneous alarms.
        current = next((s for s in steps if not s["done"] and not s["failed"]), None)
        for i, s in enumerate(steps, start=1):
            s["n"] = i
            s["current"] = s is current
            s["state"] = "done" if s["done"] else ("current" if s["current"] else "later")

        done_count = sum(1 for s in steps if s["done"])
        d, p, a = data or {}, people or {}, access or {}
        setup = {
            "steps": steps,
            "complete": done_count == len(steps),
            "done_count": done_count,
            "total": len(steps),
            "summary": (
                f"{p.get('people', 0)} {_plural(p.get('people', 0), 'person', 'people')} · "
                f"{d.get('tables', 0)} {_plural(d.get('tables', 0), 'table', 'tables')} in "
                f"{d.get('packages', 0)} {_plural(d.get('packages', 0), 'package', 'packages')} · "
                f"{a.get('shared', 0)} shared"
            ),
        }
    except Exception:
        logger.exception("admin journey: setup chain failed to resolve")
        setup = None
    return {"setup": setup}
