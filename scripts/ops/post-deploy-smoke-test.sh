#!/usr/bin/env bash
# Post-deploy smoke test — run on the prod VM after an image upgrade or a
# first deploy (docs/ONBOARDING.md step 8).
#
# Usage: ./scripts/ops/post-deploy-smoke-test.sh [AGNES_URL] [AGNES_PAT]
#   or:  AGNES_URL=https://agnes.example.com AGNES_PAT=xxx ./scripts/ops/post-deploy-smoke-test.sh
#
# Three layers, cheapest first:
#   1. Public API checks — health, DB schema, CLI wheel. No token needed.
#   2. Authenticated API checks — query/catalog/marketplace, plus the
#      new-instance doctor (POST /api/admin/doctor/new-instance): login-door,
#      email-delivery, chat-grant, agent-scope, app-state-backend, branding.
#      Needs an admin bearer: $AGNES_PAT, or (on a VM) SCHEDULER_API_TOKEN
#      read from $AGNES_OPT_DIR/.env.
#   3. Host-side consistency checks — COMPOSE_FILE ↔ instance.yaml backend,
#      TLS predicate agreement. Run only when $AGNES_OPT_DIR/.env exists
#      (i.e. on a deployed VM); skipped elsewhere. These catch states the
#      API cannot see: a sidecar-Postgres instance whose .env would drop
#      postgres on the next auto-upgrade tick, and a certs directory that
#      flips the state-applier's TLS predicate without starting caddy.
#
# Optional env:
#   AGNES_OPT_DIR    install dir (default /opt/agnes; overridable for tests)
#   DOCTOR_EMAIL_TO  have the doctor send a real test email to this address
set -euo pipefail

AGNES_URL="${1:-${AGNES_URL:-http://localhost:8000}}"
AGNES_PAT="${2:-${AGNES_PAT:-}}"
OPT_DIR="${AGNES_OPT_DIR:-/opt/agnes}"
ENV_FILE="$OPT_DIR/.env"
PASS=0
FAIL=0
WARN=0

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

warn() {
    echo "  WARN $1"
    WARN=$((WARN + 1))
}

# Line-wise .env read — mirrors _env_get in agnes-auto-upgrade.sh (never
# `. .env`: values may contain spaces/semicolons that would execute).
env_get() {
    local line
    line=$(grep -m1 -E "^${1}=" "$ENV_FILE" 2>/dev/null || true)
    [ -z "$line" ] && return 0
    line="${line#*=}"
    line="${line%\"}"
    line="${line#\"}"
    printf '%s' "$line"
}

echo "Post-deploy smoke test: $AGNES_URL"
echo "---"

# 1. Health check
HEALTH=$(curl -sf "$AGNES_URL/api/health" 2>/dev/null || echo "")
if [ -z "$HEALTH" ]; then
    check "health endpoint" "false"
else
    STATUS=$(echo "$HEALTH" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','unknown'))" 2>/dev/null || echo "parse-error")
    if [[ "$STATUS" =~ ^(ok|healthy)$ ]]; then
        check "health ($STATUS)" "true"
    else
        check "health ($STATUS)" "false"
    fi
fi

# 2. DB schema version
DB_SCHEMA=$(echo "$HEALTH" | python3 -c "import sys,json; print(json.load(sys.stdin).get('db_schema','unknown'))" 2>/dev/null || echo "unknown")
if [ "$DB_SCHEMA" = "ok" ]; then
    check "db schema version" "true"
elif [ "$DB_SCHEMA" = "unknown" ]; then
    # Fallback: check /api/version for schema_version field
    VERSION_INFO=$(curl -sf "$AGNES_URL/api/version" 2>/dev/null || echo "")
    if [ -n "$VERSION_INFO" ]; then
        check "db schema (version endpoint only)" "true"
    else
        check "db schema version" "false"
    fi
else
    check "db schema ($DB_SCHEMA)" "false"
fi

# 3. CLI wheel is served — the onboarding guide's install path
#    (`curl $AGNES_URL/cli/install.sh | bash`) downloads /cli/download; a 404
#    here means every analyst install fails. (Official images build the wheel
#    at image-build; only a dev image that skipped `uv build` legitimately 404s.)
WHEEL_HTTP=$(curl -s -o /dev/null -w "%{http_code}" "$AGNES_URL/cli/download" 2>/dev/null || echo "000")
if [ "$WHEEL_HTTP" = "200" ]; then
    check "cli wheel (/cli/download)" "true"
else
    check "cli wheel (/cli/download HTTP $WHEEL_HTTP — analyst installs from the onboarding guide will fail)" "false"
fi

# 4. Query SELECT 1 (requires PAT)
if [ -n "$AGNES_PAT" ]; then
    QUERY_OK=$(curl -sf -X POST "$AGNES_URL/api/query" \
      -H "Authorization: Bearer $AGNES_PAT" \
      -H "Content-Type: application/json" \
      -d '{"sql":"SELECT 1 as test"}' | python3 -c "
import sys,json
d=json.load(sys.stdin)
print('true' if len(d.get('rows',[])) > 0 else 'false')
" 2>/dev/null || echo "false")
    check "query SELECT 1" "$QUERY_OK"
else
    echo "  SKIP query (no PAT)"
fi

# 5. Catalog endpoint (requires PAT)
if [ -n "$AGNES_PAT" ]; then
    CATALOG_HTTP=$(curl -s -o /dev/null -w "%{http_code}" "$AGNES_URL/api/catalog" \
      -H "Authorization: Bearer $AGNES_PAT" 2>/dev/null || echo "000")
    if [[ "$CATALOG_HTTP" =~ ^(200|404)$ ]]; then
        check "catalog endpoint (HTTP $CATALOG_HTTP)" "true"
    else
        check "catalog endpoint (HTTP $CATALOG_HTTP)" "false"
    fi
else
    echo "  SKIP catalog (no PAT)"
fi

# 6. Marketplace.zip (requires PAT)
if [ -n "$AGNES_PAT" ]; then
    MARKET_HTTP=$(curl -s -o /dev/null -w "%{http_code}" "$AGNES_URL/api/marketplace.zip" \
      -H "Authorization: Bearer $AGNES_PAT" 2>/dev/null || echo "000")
    if [[ "$MARKET_HTTP" =~ ^(200|204|304|404)$ ]]; then
        check "marketplace.zip (HTTP $MARKET_HTTP)" "true"
    else
        check "marketplace.zip (HTTP $MARKET_HTTP)" "false"
    fi
else
    echo "  SKIP marketplace.zip (no PAT)"
fi

# 7. New-instance doctor — the server-side deploy gate. Each of its checks
#    caught a silent real-deployment failure: no usable login door, email
#    that 200s but never delivers, chat invisible without its grant, agents
#    with an empty owner-grants ∩ scope, branding not reaching the login page.
#    Admin bearer: explicit AGNES_PAT first; on a VM fall back to the
#    scheduler service token (admin for /api/admin/*).
DOCTOR_TOKEN="$AGNES_PAT"
DOCTOR_TOKEN_SRC="AGNES_PAT"
if [ -z "$DOCTOR_TOKEN" ] && [ -f "$ENV_FILE" ]; then
    DOCTOR_TOKEN=$(env_get SCHEDULER_API_TOKEN)
    DOCTOR_TOKEN_SRC="SCHEDULER_API_TOKEN from $ENV_FILE"
fi
if [ -n "$DOCTOR_TOKEN" ]; then
    DOCTOR_BODY='{}'
    if [ -n "${DOCTOR_EMAIL_TO:-}" ]; then
        # json.dumps, not printf: a quote/backslash in the value would malform
        # a hand-interpolated payload and fail the check opaquely.
        DOCTOR_BODY=$(python3 -c 'import json, sys; print(json.dumps({"email_to": sys.argv[1]}))' "$DOCTOR_EMAIL_TO")
    fi
    DOCTOR_JSON=$(curl -sf -X POST "$AGNES_URL/api/admin/doctor/new-instance" \
      -H "Authorization: Bearer $DOCTOR_TOKEN" \
      -H "Content-Type: application/json" \
      -d "$DOCTOR_BODY" 2>/dev/null || echo "")
    if [ -z "$DOCTOR_JSON" ]; then
        check "doctor endpoint (POST /api/admin/doctor/new-instance with $DOCTOR_TOKEN_SRC)" "false"
    else
        DOCTOR_LINES=$(echo "$DOCTOR_JSON" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for c in d.get('checks', []):
    detail = ' '.join(str(c.get('detail', '')).split())
    print('%s\t%s\t%s' % (c.get('status', '?'), c.get('name', '?'), detail))
" 2>/dev/null || echo "")
        if [ -z "$DOCTOR_LINES" ]; then
            check "doctor response parse" "false"
        else
            while IFS=$'\t' read -r d_status d_name d_detail; do
                [ -z "$d_name" ] && continue
                case "$d_status" in
                    ok)      check "doctor:$d_name — $d_detail" "true" ;;
                    error)   check "doctor:$d_name — $d_detail" "false" ;;
                    warning) warn "doctor:$d_name — $d_detail" ;;
                    *)       echo "  INFO doctor:$d_name — $d_detail" ;;
                esac
            done <<< "$DOCTOR_LINES"
        fi
    fi
else
    echo "  SKIP doctor (no admin token — pass AGNES_PAT, or run on the VM where $ENV_FILE holds SCHEDULER_API_TOKEN)"
fi

# --- Host-side consistency checks (VM only) --------------------------------
if [ -f "$ENV_FILE" ]; then
    echo "--- host checks ($OPT_DIR) ---"
    STATE_DIR=$(env_get STATE_DIR)
    STATE_DIR="${STATE_DIR:-/data/state}"
    COMPOSE_FILE_VAL=$(env_get COMPOSE_FILE)
    INSTANCE_YAML="$STATE_DIR/instance.yaml"

    # 8. COMPOSE_FILE ↔ instance.yaml database.backend.
    #    The startup script derives COMPOSE_FILE from instance.yaml on boot,
    #    but a live DuckDB→Postgres migration (agnes-state-applier) updates
    #    only instance.yaml — auto-upgrade then re-runs `docker compose up`
    #    from the stale .env and silently drops the postgres sidecar. The
    #    backend read below deliberately mirrors the startup script's own
    #    (first `backend:` line wins) — the point is to predict what the
    #    writer of COMPOSE_FILE would do, not to parse YAML better than it.
    BACKEND=""
    if [ -f "$INSTANCE_YAML" ]; then
        BACKEND=$(sed -n 's/^[[:space:]]*backend:[[:space:]]*//p' "$INSTANCE_YAML" 2>/dev/null | head -1 | tr -d '"' || true)
    fi
    case "$BACKEND" in
        side_car|side_car_in_progress)
            case ":$COMPOSE_FILE_VAL:" in
                *":docker-compose.postgres.yml:"*)
                    check "compose: backend=$BACKEND and COMPOSE_FILE carries the postgres overlay" "true"
                    ;;
                *)
                    check "compose: instance.yaml says database.backend=$BACKEND but COMPOSE_FILE in $ENV_FILE lacks docker-compose.postgres.yml — the next auto-upgrade tick recreates the stack WITHOUT postgres. Fix: append docker-compose.postgres.yml:docker-compose.postgres-host-mount.yml to COMPOSE_FILE (or reboot: the startup script rewrites .env from instance.yaml)" "false"
                    ;;
            esac
            ;;
        *)
            check "compose: backend=${BACKEND:-duckdb} — postgres overlay not required" "true"
            ;;
    esac
    # The applier's own target flag should agree with the persisted backend.
    TARGET_FLAG=$(cat "$STATE_DIR/db-state-target.flag" 2>/dev/null || true)
    if [ -n "$TARGET_FLAG" ]; then
        if [ "$BACKEND" = "side_car" ] && [ "$TARGET_FLAG" != "side-car-enabled" ]; then
            warn "compose: backend=side_car but db-state-target.flag says '$TARGET_FLAG' — applier and instance.yaml disagree"
        elif [ "$BACKEND" != "side_car" ] && [ "$BACKEND" != "side_car_in_progress" ] && [ "$TARGET_FLAG" = "side-car-enabled" ]; then
            warn "compose: db-state-target.flag says side-car-enabled but instance.yaml backend is '${BACKEND:-duckdb}' — applier and instance.yaml disagree"
        fi
    fi

    # 9. TLS predicate agreement. Three scripts decide "is TLS on" three ways:
    #      startup:      TLS_MODE=caddy && DOMAIN set        (config)
    #      auto-upgrade: certs/fullchain.pem && privkey.pem  (cert files)
    #      state-applier: -d $STATE_DIR/certs                (bare directory!)
    #    The dangerous disagreement is a certs dir that exists WITHOUT both
    #    cert files: the applier then applies docker-compose.tls.yml (which
    #    resets the app's :8000 ports) without ever starting caddy — the
    #    instance goes unreachable on both ports.
    TLS_MODE=$(env_get TLS_MODE)
    DOMAIN=$(env_get DOMAIN)
    CERTS_DIR="$STATE_DIR/certs"
    CERTS_OK="false"
    if [ -s "$CERTS_DIR/fullchain.pem" ] && [ -s "$CERTS_DIR/privkey.pem" ]; then
        CERTS_OK="true"
    fi
    TLS_STATE="tls_mode=${TLS_MODE:-<unset>} domain=${DOMAIN:-<unset>} certs=$CERTS_OK"
    # An ACME instance (tls_mode=caddy with a DOMAIN) is the one shape where an
    # empty certs dir is normal rather than dangerous: Let's Encrypt certs live
    # in caddy's own volume and $CERTS_DIR is just the read-only bind mount, so
    # it exists and stays empty for the whole life of a healthy instance. The
    # applier does add the tls overlay off that bare directory, but it restarts
    # only `app scheduler` with --no-deps, so the caddy the startup script
    # brought up under --profile tls keeps running and keeps terminating TLS --
    # closing plain :8000 is then the intended outcome, not an outage. Failing
    # here would exit 1 on precisely the instances this gate is run against.
    ACME_SHAPE="false"
    if [ "$TLS_MODE" = "caddy" ] && [ -n "$DOMAIN" ]; then
        ACME_SHAPE="true"
    fi
    if [ -d "$CERTS_DIR" ] && [ "$CERTS_OK" = "false" ] && [ "$ACME_SHAPE" = "false" ]; then
        check "tls: $CERTS_DIR exists but fullchain.pem/privkey.pem are missing or empty — the state-applier's next run applies docker-compose.tls.yml (app ports closed) WITHOUT starting caddy, taking the instance offline. Install both certs or remove the directory" "false"
    elif [ "$TLS_MODE" = "caddy" ] && [ -z "$DOMAIN" ]; then
        check "tls: tls_mode=caddy but DOMAIN is empty — the tls profile never starts ($TLS_STATE)" "false"
    else
        check "tls: predicates agree ($TLS_STATE)" "true"
    fi
    # Caddy-with-LetsEncrypt keeps its certs in caddy's own volume, so
    # CERTS_OK=false is normal there — but then nothing closes plain :8000
    # unless the tls overlay is in COMPOSE_FILE or a firewall does it.
    if [ "$ACME_SHAPE" = "true" ] && [ "$CERTS_OK" = "false" ]; then
        case ":$COMPOSE_FILE_VAL:" in
            *":docker-compose.tls.yml:"*) : ;;
            *)
                warn "tls: caddy terminates TLS but docker-compose.tls.yml is not in COMPOSE_FILE — plain :8000 stays open on the app container; close it via firewall or append the overlay"
                ;;
        esac
    fi
else
    echo "  SKIP host checks (no $ENV_FILE — not a deployed VM)"
fi

# Results
echo ""
echo "Results: $PASS passed, $WARN warnings, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
