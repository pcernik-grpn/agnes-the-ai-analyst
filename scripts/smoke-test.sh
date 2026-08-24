#!/usr/bin/env bash
# Agnes smoke test — verifies a running instance is functional.
# Usage: ./scripts/smoke-test.sh [host:port]
# Default: http://localhost:8000
set -euo pipefail

HOST="${1:-http://localhost:8000}"
PASS=0
FAIL=0
TOKEN=""

check() {
    local name="$1" ok="$2"
    if [ "$ok" = "true" ]; then
        echo "  PASS $name"
        PASS=$((PASS + 1))
    else
        echo "  FAIL $name"
        FAIL=$((FAIL + 1))
    fi
}

echo "Smoke test: $HOST"
echo "---"

# 1. Health check (minimal, unauthenticated)
HEALTH=$(curl -sf "$HOST/api/health" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])" 2>/dev/null || echo "unreachable")
if [ "$HEALTH" = "unreachable" ]; then
    echo "  FATAL: health=$HEALTH"
    exit 1
fi
check "health ($HEALTH)" "true"

# 1b. Unauthenticated DB-touching probe — exercises the system-DB path before
# any token is acquired. /api/health does NOT open system.duckdb (deliberate, so
# the LB probe stays cheap), so it can return 200 while every authed request
# 500s on permission/IO errors. /auth/email/request opens the users table to
# look up the email, which catches the foundryai-development class of
# regression (host-mounted /data root-owned, USER agnes can't open the DB).
# Accept anything in 200-499 — including 4xx for "email auth disabled" — but
# fail loudly on 5xx.
DB_PROBE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$HOST/auth/email/request" \
  -H "Content-Type: application/json" \
  -d '{"email":"smoke-probe@test.local"}' 2>/dev/null || echo "000")
case "$DB_PROBE" in
    5*|000) check "db-touching probe (HTTP $DB_PROBE — expected non-5xx)" "false" ;;
    *)      check "db-touching probe (HTTP $DB_PROBE)" "true" ;;
esac

# 2. Health detailed has version fields (requires auth, checked after bootstrap)

# 3. Bootstrap (only works on fresh DB; 403 means users exist)
BOOT_HTTP=$(curl -s -o /tmp/smoke_boot.json -w "%{http_code}" -X POST "$HOST/auth/bootstrap" \
  -H "Content-Type: application/json" \
  -d '{"email":"smoke@test.local","name":"Smoke Test","password":"SmokeTest123!"}' 2>/dev/null || echo "000")

if [ "$BOOT_HTTP" = "200" ]; then
    TOKEN=$(python3 -c "import json; print(json.load(open('/tmp/smoke_boot.json'))['access_token'])" 2>/dev/null || echo "")
    check "bootstrap (new admin)" "true"
elif [ "$BOOT_HTTP" = "403" ]; then
    # Users exist — operator must supply SMOKE_TOKEN to validate the authed
    # paths, otherwise the script would silently SKIP every regression.
    TOKEN="${SMOKE_TOKEN:-}"
    if [ -z "$TOKEN" ]; then
        check "bootstrap (users exist; SMOKE_TOKEN required to continue)" "false"
    else
        echo "  SKIP bootstrap (users exist; using SMOKE_TOKEN)"
    fi
else
    check "bootstrap (HTTP $BOOT_HTTP)" "false"
fi

# 2b. Health detailed (authenticated) — version fields
if [ -n "$TOKEN" ]; then
    HAS_VERSION=$(curl -sf "$HOST/api/health/detailed" \
      -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys,json
d=json.load(sys.stdin)
print('true' if 'version' in d and 'channel' in d and 'schema_version' in d else 'false')
" 2>/dev/null || echo "false")
    check "health detailed version fields" "$HAS_VERSION"
fi

# 2c. App-state backend is Postgres (A1: default for new installs). Reads
# the same use_pg()-derived verdict app/services/instance_doctor.py's
# app-state-backend check reports, via the support-bundle's
# schema.backend field ("postgres"/"duckdb" — see app/services/support_bundle.py).
# This is the release smoke gate's own positive assertion that A1's default
# actually boots on Postgres, independent of the "duckdb"/"backend"/
# "COMPOSE_FILE"/"postgres" string-grep audit that found no prior assumption
# to fix here.
if [ -n "$TOKEN" ]; then
    BACKEND=$(curl -sf "$HOST/api/admin/doctor/support" \
      -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys,json
d=json.load(sys.stdin)
print(d.get('schema',{}).get('backend','unknown'))
" 2>/dev/null || echo "unreachable")
    if [ "$BACKEND" = "postgres" ]; then
        check "app-state backend ($BACKEND)" "true"
    else
        check "app-state backend ($BACKEND, expected postgres)" "false"
    fi
else
    echo "  SKIP app-state backend (no token)"
fi

# 4. Query SELECT 1 (requires auth)
if [ -n "$TOKEN" ]; then
    QUERY_OK=$(curl -sf -X POST "$HOST/api/query" \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" \
      -d '{"sql":"SELECT 1 as test"}' | python3 -c "
import sys,json
d=json.load(sys.stdin)
print('true' if len(d.get('rows',[])) > 0 else 'false')
" 2>/dev/null || echo "false")
    check "query SELECT 1" "$QUERY_OK"
else
    echo "  SKIP query (no token)"
fi

# 5. Sync trigger
if [ -n "$TOKEN" ]; then
    SYNC_HTTP=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$HOST/api/sync/trigger" \
      -H "Authorization: Bearer $TOKEN" 2>/dev/null || echo "000")
    if [[ "$SYNC_HTTP" =~ ^(200|202)$ ]]; then
        check "sync trigger" "true"
    else
        check "sync trigger (HTTP $SYNC_HTTP)" "false"
    fi
else
    echo "  SKIP sync (no token)"
fi

# 6. Post-sync health (wait briefly)
sleep 5
if [ -n "$TOKEN" ]; then
    HEALTH2=$(curl -sf "$HOST/api/health/detailed" \
      -H "Authorization: Bearer $TOKEN" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])" 2>/dev/null || echo "unreachable")
else
    HEALTH2=$(curl -sf "$HOST/api/health" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])" 2>/dev/null || echo "unreachable")
fi
if [ "$HEALTH2" = "unhealthy" ] || [ "$HEALTH2" = "unreachable" ]; then
    check "post-sync health ($HEALTH2)" "false"
else
    check "post-sync health ($HEALTH2)" "true"
fi

# 7. Catalog endpoint (authenticated)
if [ -n "$TOKEN" ]; then
    CATALOG_HTTP=$(curl -s -o /tmp/smoke_catalog.json -w "%{http_code}" "$HOST/api/catalog" \
      -H "Authorization: Bearer $TOKEN" 2>/dev/null || echo "000")
    if [[ "$CATALOG_HTTP" =~ ^(200|404)$ ]]; then
        check "catalog endpoint (HTTP $CATALOG_HTTP)" "true"
    else
        check "catalog endpoint (HTTP $CATALOG_HTTP)" "false"
    fi
else
    echo "  SKIP catalog (no token)"
fi

# 8. Admin registry endpoint (authenticated)
# NOTE: was /api/admin/tables until that endpoint was renamed to
# /api/admin/registry; this assertion went stale and only surfaced when the
# auto-rollback workflow first fired (smoke test was failing for many
# releases without anyone noticing).
if [ -n "$TOKEN" ]; then
    TABLES_HTTP=$(curl -s -o /tmp/smoke_tables.json -w "%{http_code}" "$HOST/api/admin/registry" \
      -H "Authorization: Bearer $TOKEN" 2>/dev/null || echo "000")
    if [[ "$TABLES_HTTP" =~ ^(200|403)$ ]]; then
        check "admin registry endpoint (HTTP $TABLES_HTTP)" "true"
    else
        check "admin registry endpoint (HTTP $TABLES_HTTP)" "false"
    fi
else
    echo "  SKIP admin registry (no token)"
fi

# 9. Marketplace.zip endpoint (with PAT auth if available)
MARKETPLACE_PAT="${AGNES_PAT:-${SMOKE_PAT:-}}"
if [ -n "$MARKETPLACE_PAT" ]; then
    MARKET_HTTP=$(curl -s -o /tmp/smoke_marketplace.zip -w "%{http_code}" "$HOST/api/marketplace.zip" \
      -H "Authorization: Bearer $MARKETPLACE_PAT" 2>/dev/null || echo "000")
    if [[ "$MARKET_HTTP" =~ ^(200|304|404)$ ]]; then
        check "marketplace.zip (HTTP $MARKET_HTTP)" "true"
    else
        check "marketplace.zip (HTTP $MARKET_HTTP)" "false"
    fi
else
    echo "  SKIP marketplace.zip (no PAT — set AGNES_PAT or SMOKE_PAT to test)"
fi

# 10. Metrics endpoint (authenticated)
if [ -n "$TOKEN" ]; then
    METRICS_HTTP=$(curl -s -o /tmp/smoke_metrics.json -w "%{http_code}" "$HOST/api/metrics" \
      -H "Authorization: Bearer $TOKEN" 2>/dev/null || echo "000")
    if [[ "$METRICS_HTTP" =~ ^(200|404)$ ]]; then
        check "metrics endpoint (HTTP $METRICS_HTTP)" "true"
    else
        check "metrics endpoint (HTTP $METRICS_HTTP)" "false"
    fi
else
    echo "  SKIP metrics (no token)"
fi

# 11. /home renders, and the connector manifest is served (regression
#     guard for the install-prompt renderer in
#     app/web/setup_instructions.py). A blank /home — empty body,
#     renderer crash — is the worst-case failure here: the setup prompt
#     is the primary onboarding surface.
#
#     Contract note (0.83.86, "thin install prompt"): /home no longer
#     renders connector tiles or a finale roll-call of connector display
#     names. That work moved into `agnes onboard`, whose step 6 reads
#     GET /api/connectors/manifest and prints "Available connectors on
#     this instance:". The removal is deliberate and is pinned by
#     tests/test_web_home_page.py::test_connectors_section_removed_from_home,
#     which asserts `class="connector-tiles"` and
#     `data-section="connectors"` are ABSENT while "Asana" and "Google
#     Workspace" still appear in the page copy — i.e. exactly the
#     opposite of what this check used to require. This check was left
#     behind by that change and failed every release until it was
#     updated; it now asserts the surviving contract (the page renders
#     and still names the connector families) plus the endpoint the
#     coverage moved to, so nothing is merely deleted.
if [ -n "$TOKEN" ]; then
    HOME_BODY=$(curl -s "$HOST/home" \
      -H "Authorization: Bearer $TOKEN" \
      -b "access_token=$TOKEN" 2>/dev/null || echo "")
    HOME_OK="true"
    # Connector FAMILY names, as they appear in the page's own copy —
    # not the bundled skills' frontmatter display names, which the thin
    # prompt no longer renders. Use a here-string instead of a pipe:
    # `grep -q` closes stdin as soon as it matches, the upstream `echo`
    # dies of SIGPIPE, and `pipefail` then surfaces that as a false
    # negative for the substring check.
    for name in "Asana" "Google Workspace" "Atlassian"; do
        if ! grep -qF "$name" <<< "$HOME_BODY"; then
            HOME_OK="false"
            echo "  WARN /home body missing \"$name\" (page copy regression?)"
        fi
    done
    if [ -z "$HOME_BODY" ]; then
        HOME_OK="false"
        echo "  WARN /home body empty (renderer crash?)"
    fi
    check "/home renders" "$HOME_OK"

    # Where the connector roll-call moved: `agnes onboard` step 6 reads
    # this endpoint to tell the operator what the instance offers, so a
    # broken manifest is the modern shape of the failure the /home tile
    # check used to catch.
    CONN_HTTP=$(curl -s -o /tmp/smoke_connectors.json -w "%{http_code}" \
      "$HOST/api/connectors/manifest" \
      -H "Authorization: Bearer $TOKEN" 2>/dev/null || echo "000")
    CONN_OK="false"
    if [ "$CONN_HTTP" = "200" ]; then
        CONN_COUNT=$(python3 -c "
import json
try:
    b = json.load(open('/tmp/smoke_connectors.json'))
    c = b.get('connectors')
    print(len(c) if isinstance(c, list) else -1)
except Exception:
    print(-1)
" 2>/dev/null || echo "-1")
        if [ "$CONN_COUNT" -ge 1 ] 2>/dev/null; then
            CONN_OK="true"
        else
            echo "  WARN /api/connectors/manifest returned no connectors (bundled seed regression?)"
        fi
    else
        echo "  WARN /api/connectors/manifest HTTP $CONN_HTTP"
    fi
    check "connector manifest served (HTTP $CONN_HTTP)" "$CONN_OK"
else
    echo "  SKIP /home (no token)"
fi

# 12. POST /api/admin/initial-workspace/sync (no-op-friendly): the endpoint
#     400s with kind=not_configured on a fresh install (no IWT registered).
#     We don't try to register one — that would require a live PAT — but we
#     verify the contract returns the typed error rather than 500.
if [ -n "$TOKEN" ]; then
    IW_SYNC_HTTP=$(curl -s -o /tmp/smoke_iw_sync.json -w "%{http_code}" \
      -X POST "$HOST/api/admin/initial-workspace/sync" \
      -H "Authorization: Bearer $TOKEN" 2>/dev/null || echo "000")
    if [ "$IW_SYNC_HTTP" = "400" ]; then
        IW_KIND=$(python3 -c "
import sys, json
try:
    print(json.load(open('/tmp/smoke_iw_sync.json'))['detail']['kind'])
except Exception:
    print('')
" 2>/dev/null)
        if [ "$IW_KIND" = "not_configured" ]; then
            check "initial-workspace sync error contract" "true"
        else
            check "initial-workspace sync error contract (got kind=$IW_KIND)" "false"
        fi
    else
        check "initial-workspace sync error contract (HTTP $IW_SYNC_HTTP, expected 400)" "false"
    fi
else
    echo "  SKIP initial-workspace sync (no token)"
fi

# Results
echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
