#!/usr/bin/env bash
# Bootstraps a read-only monitoring role for the Datadog Agent in every
# Postgres side-car of this host and renders the agent's postgres check config.
#
# Runs as root from agnes-datadog-pg-role.timer rather than from the startup
# script, for two reasons: the agent block installs BEFORE docker compose has
# converged (so no side-car exists yet at that point), and a side-car whose
# volume is recreated loses the role — a timer re-converges, a boot-time step
# would not until the next reboot.
#
# The password is generated once, kept root-only in /data/state, fed to psql on
# stdin and substituted into the check config through bash parameter expansion.
# It never appears on a command line, in the environment, or in Terraform state.
#
# Safe to re-run. Always exits 0: a monitoring bootstrap must never mark a
# systemd unit failed and thereby trip the very alerting it is setting up.
set -uo pipefail

PW_FILE=/data/state/datadog-pg-password
TPL=/etc/datadog-agent/agnes-postgres.yaml.tpl
OUT=/etc/datadog-agent/conf.d/postgres.d/conf.yaml

warn() { logger -t agnes-datadog-pg-role -p user.warning "$1" 2>/dev/null || true; }

[ -d /etc/datadog-agent ] || exit 0
command -v docker >/dev/null 2>&1 || exit 0

if [ ! -s "$PW_FILE" ]; then
    install -d -m 0700 "$(dirname "$PW_FILE")" 2>/dev/null || true
    if ! ( umask 077; openssl rand -hex 24 > "$PW_FILE" ) 2>/dev/null; then
        ( umask 077; od -An -tx1 -N24 /dev/urandom | tr -d ' \n' > "$PW_FILE" ) 2>/dev/null || true
    fi
fi
if [ ! -s "$PW_FILE" ]; then
    warn "could not generate a Datadog Postgres password — check config not rendered"
    exit 0
fi
PW=$(cat "$PW_FILE")

for cid in $(docker ps -q 2>/dev/null); do
    image=$(docker inspect -f '{{.Config.Image}}' "$cid" 2>/dev/null) || continue
    case "$image" in
        postgres:* | */postgres:*) ;;
        *) continue ;;
    esac
    # The superuser differs per side-car (agnes / kai), so read it off the
    # container rather than hardcoding either.
    user=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$cid" 2>/dev/null \
        | sed -n 's/^POSTGRES_USER=//p' | head -1)
    user=${user:-postgres}
    docker exec -i "$cid" psql -v ON_ERROR_STOP=1 -U "$user" -d postgres >/dev/null 2>&1 <<SQL || warn "pg_monitor role bootstrap failed in container $cid ($image)"
DO \$do\$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'datadog') THEN
        CREATE ROLE datadog LOGIN;
    END IF;
END \$do\$;
ALTER ROLE datadog WITH LOGIN PASSWORD '$PW';
GRANT pg_monitor TO datadog;
SQL
done

if [ -f "$TPL" ]; then
    install -d -o dd-agent -g dd-agent -m 0750 "$(dirname "$OUT")" 2>/dev/null || true
    tmp=$(mktemp) || exit 0
    chmod 0600 "$tmp" 2>/dev/null || true
    rendered=$(cat "$TPL")
    printf '%s\n' "${rendered//@@DD_PG_PASSWORD@@/$PW}" > "$tmp"
    if ! cmp -s "$tmp" "$OUT"; then
        if install -o dd-agent -g dd-agent -m 0640 "$tmp" "$OUT" 2>/dev/null; then
            systemctl restart datadog-agent >/dev/null 2>&1 || warn "could not restart datadog-agent after rendering the postgres check"
        else
            warn "could not install the rendered postgres check config"
        fi
    fi
    rm -f "$tmp"
fi

exit 0
