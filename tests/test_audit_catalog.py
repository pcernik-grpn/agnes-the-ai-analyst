"""Guards for the audit action catalog (F0 — Task 1).

Two ratchets live here:
  1. every action string actually emitted via an audit writer must be
     registered in ``src.audit_events.CATALOG`` (or covered by a
     ``DYNAMIC_ACTION_PREFIXES`` prefix) — no silently-uncataloged actions.
  2. no NEW direct ``audit_repo().log(...)`` caller outside the repo layer —
     new code must go through ``src.audit_helpers.log_safe`` instead.
"""

import re
from pathlib import Path

from src.audit_events import CATALOG, is_cataloged

REPO = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("app", "src", "services", "cli")
# action="..." literals passed to audit writers; single- or double-quoted
ACTION_RE = re.compile(r"""action\s*=\s*(?:f?["'])([a-zA-Z0-9_.:{}-]+)["']""")


def _emitted_literals() -> set[str]:
    out: set[str] = set()
    for d in SCAN_DIRS:
        for p in (REPO / d).rglob("*.py"):
            text = p.read_text(encoding="utf-8", errors="replace")
            if not any(w in text for w in ("log_safe(", "write_audit(", "audit_repo(")):
                continue
            for m in ACTION_RE.finditer(text):
                lit = m.group(1)
                if "{" in lit:  # f-string with a dynamic tail — prefix rule covers it
                    continue
                out.add(lit)
    return out


def test_every_emitted_action_is_cataloged():
    missing = sorted(a for a in _emitted_literals() if not is_cataloged(a))
    assert not missing, (
        "Actions emitted but not registered in src/audit_events.py CATALOG "
        f"(register them, don't rename silently): {missing}"
    )


def test_catalog_categories_are_valid():
    for ev in CATALOG.values():
        assert ev.category in ("auth", "mutation", "read", "system"), ev.action


# ---------------------------------------------------------------------------
# Step 8: static emitter guard (ratchet) — no new direct audit_repo().log()
# callers outside the repo layer; new code must use log_safe instead.
# ---------------------------------------------------------------------------

KNOWN_DIRECT_LOG_CALLERS = frozenset(
    {
        # frozen pre-existing direct audit_repo().log() call sites (file paths);
        # new code must use src.audit_helpers.log_safe instead. Seeded from
        # the scan output on the integration base (2026-08-28, commit
        # 4cdf2cc8f) — do not add to this list; migrate to log_safe instead.
        "app/api/access.py",
        "app/api/activity.py",
        "app/api/admin.py",
        "app/api/admin_adoption.py",
        "app/api/admin_analytics.py",
        "app/api/admin_chat.py",
        "app/api/admin_datasource_secrets.py",
        "app/api/admin_mcp.py",
        "app/api/admin_reports.py",
        "app/api/admin_sessions.py",
        "app/api/admin_slack_secrets.py",
        "app/api/admin_sso.py",
        "app/api/admin_usage.py",
        "app/api/admin_usage_summary.py",
        "app/api/admin_user_sessions.py",
        "app/api/agent_memory.py",
        "app/api/agent_schedules.py",
        "app/api/agents_admin.py",
        "app/api/authoring_suggestions.py",
        "app/api/broker.py",
        "app/api/cli_auth.py",
        "app/api/cowork_bundle.py",
        "app/api/data.py",
        "app/api/data_apps.py",
        "app/api/data_packages.py",
        "app/api/facts.py",
        "app/api/initial_workspace.py",
        "app/api/jobs.py",
        "app/api/kai.py",
        "app/api/knowledge_digests.py",
        "app/api/knowledge_search.py",
        "app/api/marketplace.py",
        "app/api/marketplaces.py",
        "app/api/mcp_oauth_connect.py",
        "app/api/me.py",
        "app/api/memory.py",
        "app/api/memory_domain_suggestions.py",
        "app/api/memory_domains.py",
        "app/api/my_stack.py",
        "app/api/news.py",
        "app/api/query.py",
        "app/api/query_hybrid.py",
        "app/api/recipes.py",
        "app/api/scripts.py",
        "app/api/semantic_models.py",
        "app/api/share_requests_admin.py",
        "app/api/store.py",
        "app/api/sync.py",
        "app/api/tokens.py",
        "app/api/upload.py",
        "app/api/v2_catalog.py",
        "app/api/v2_sample.py",
        "app/api/v2_scan.py",
        "app/api/v2_schema.py",
        "app/auth/pat_resolver.py",
        "app/auth/providers/password.py",
        "app/auth/providers/sso.py",
        "app/auth/router.py",
        "app/chat/audit.py",
        "app/main.py",
        "cli/commands/admin_metrics.py",
        "services/slack_bot/binding.py",
    }
)


def test_no_new_direct_audit_log_callers():
    offenders = set()
    for d in SCAN_DIRS:
        for p in (REPO / d).rglob("*.py"):
            rel = str(p.relative_to(REPO))
            if rel.startswith("src/repositories/") or rel == "src/audit_helpers.py":
                continue
            if "audit_repo().log(" in p.read_text(encoding="utf-8", errors="replace"):
                offenders.add(rel)
    new = offenders - KNOWN_DIRECT_LOG_CALLERS
    removed = KNOWN_DIRECT_LOG_CALLERS - offenders
    assert not new, f"New direct audit_repo().log() callers — use log_safe: {sorted(new)}"
    assert not removed, f"Prune KNOWN_DIRECT_LOG_CALLERS, these migrated: {sorted(removed)}"
