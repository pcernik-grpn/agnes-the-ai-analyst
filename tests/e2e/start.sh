#!/usr/bin/env bash
# start.sh — entrypoint for the E2E Agnes container.
#
# Sequence:
#   1. Drop the test instance.yaml into /data/state so config/loader.py
#      picks it up.
#   2. Seed the analytics DuckDB with the SQL fixtures under sample-data/.
#   3. Boot uvicorn.
#
# There is no iptables / nsjail step here — chat sandboxes are sibling
# docker containers spawned (and hardened: cap_drop ALL, no-new-privileges,
# pids/mem/cpu limits) by the apps-runner sidecar, not by this container.

set -euo pipefail

echo "[start.sh] staging instance.yaml"
mkdir -p /data/state /data/marketplaces /data/analytics
cp /app/tests/e2e/instance.yaml.e2e /data/state/instance.yaml

echo "[start.sh] loading sample data into analytics DuckDB"
/opt/venv/bin/python /app/tests/e2e/load-sample-data.py

echo "[start.sh] starting uvicorn on 0.0.0.0:8000"
exec /opt/venv/bin/uvicorn app.main:app \
    --workers 1 \
    --host 0.0.0.0 \
    --port 8000
