# Multi-stage: `base` builds the whole app image exactly as before; `app`
# (the default/last stage, so a plain `docker build .` — CI and the release
# workflow — is completely unaffected) is `base` verbatim; `worker` is an
# ADDITIONAL stage for the extraction worker lane (spec §7.5 / §16 step 7 of
# docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md),
# built only when explicitly requested via `--target worker` (see the
# `extraction-worker` service in docker-compose.yml). Never built by
# default, so it costs nothing to every existing deployment.
FROM python:3.13-slim AS base

# libreoffice-{core,writer,calc,impress} (headless, --no-install-recommends
# keeps out the GUI/help-browser/Java deps) + fonts-liberation (metric-
# compatible with the common Office fonts, so a headless conversion doesn't
# fall back to box glyphs) back the legacy Office/OpenDocument pre-converter
# in src/ingest/convert.py (.doc/.rtf/.odt/.ppt/.odp/.xls/.ods →
# docx/pptx/xlsx via `soffice --headless`, then the existing markitdown
# route). Same apt layer as curl/git so the image gains one cache-friendly
# layer, not two.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl git \
        libreoffice-core libreoffice-writer libreoffice-calc libreoffice-impress \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ARG AGNES_VERSION=dev
ARG RELEASE_CHANNEL=dev
ARG AGNES_COMMIT_SHA=unknown
ARG AGNES_TAG=unknown
ENV AGNES_VERSION=${AGNES_VERSION}
ENV RELEASE_CHANNEL=${RELEASE_CHANNEL}
ENV AGNES_COMMIT_SHA=${AGNES_COMMIT_SHA}
ENV AGNES_TAG=${AGNES_TAG}

# Memory-allocator tuning. BigQuery/DuckDB query churn produces large transient
# native allocations; glibc's default per-CPU arenas (≈8×cores) retain freed
# memory and never return it to the OS, and on a host with Transparent Huge
# Pages = always each retained region is backed by a 2 MiB huge page — so RSS
# ratchets up to the largest concurrent working set and stays there, eventually
# tripping the cgroup OOM killer on data-source-heavy instances. Capping arenas
# to 2 and lowering the trim threshold (128 KiB) forces glibc to release freed
# memory back to the kernel. Negligible CPU cost for this I/O-bound workload
# (DuckDB manages its own buffer pool). Host-side THP=madvise is the companion
# mitigation, applied in infra/modules/customer-instance/startup-script.sh.tpl.
ENV MALLOC_ARENA_MAX=2
ENV MALLOC_TRIM_THRESHOLD_=131072

WORKDIR /app

COPY . .

# Bake every host-side artifact at /opt/agnes-host/ — the contract path
# VM startup uses to extract files via `docker create` + `docker cp`
# instead of curling from raw.githubusercontent.com/main. Pins host
# artifacts to AGNES_TAG the same way the app is already pinned —
# eliminates the split-brain where the immutable image runs against
# arbitrary main-branch compose files / bash scripts.
#
# Includes:
#   - agnes-auto-upgrade.sh — host cron driver (5-min digest poll)
#   - agnes-tls-rotate.sh — host cron driver (daily corp-PKI cert refetch)
#   - tls-fetch.sh — generic URL fetcher (sm:// gs:// https:// file://)
#   - agnes-state-applier.{sh,service,timer} — DB backend state machine
#     (applies compose lifecycle changes when /data/state/db-state-target.flag changes)
#   - scripts/ops/agnes-compose-file.sh — the single COMPOSE_FILE resolver,
#     SOURCED (not executed) by agnes-auto-upgrade.sh and
#     agnes-state-applier.sh. Kept under its scripts/ops/ sub-path because
#     both of them source it as "$COMPOSE_DIR/scripts/ops/…", and the
#     startup script's recursive `docker cp /opt/agnes-host/.` preserves
#     that shape. Without it in the image, a fresh VM has no resolver until
#     agnes-auto-upgrade.sh's GitHub fetch lands one — so on an
#     egress-restricted host the state machine and the upgrade job would
#     both be dead permanently.
#   - post-deploy-smoke-test.sh — deploy gate (docs/ONBOARDING.md step 8):
#     public API + new-instance doctor + host-side consistency checks
#   - docker-compose.{yml,prod.yml,host-mount.yml,tls.yml} — host runtime
#   - docker-compose.gcp-logging.yml — opt-out gcplogs overlay (removed by
#     startup-script.sh.tpl when enable_gcp_logging=false; see its own header)
#   - Caddyfile — TLS reverse proxy config
#   - static/maintenance.html — Caddy's handle_errors 502/503 fallback page
#
# Why a copy out of /app instead of pointing at /app directly:
#   /app is owned by uid 999 (USER agnes below); /opt/agnes-host is
#   root-owned, mode 0755 across the board, stable path that won't
#   shift if /app structure refactors. Stable contract for `docker cp`
#   consumers.
RUN mkdir -p /opt/agnes-host/static /opt/agnes-host/scripts/ops && \
    cp /app/scripts/ops/agnes-compose-file.sh \
       /app/scripts/ops/agnes-chat-sandbox-image.sh \
       /opt/agnes-host/scripts/ops/ && \
    cp /app/scripts/ops/agnes-auto-upgrade.sh \
       /app/scripts/ops/agnes-tls-rotate.sh \
       /app/scripts/ops/agnes-state-applier.sh \
       /app/scripts/ops/agnes-state-applier.service \
       /app/scripts/ops/agnes-state-applier.timer \
       /app/scripts/ops/agnes-state-applier-bootstrap.service \
       /app/scripts/ops/post-deploy-smoke-test.sh \
       /app/scripts/tls-fetch.sh \
       /opt/agnes-host/ && \
    cp /app/docker-compose.yml /app/docker-compose.prod.yml \
       /app/docker-compose.host-mount.yml /app/docker-compose.tls.yml \
       /app/docker-compose.postgres.yml \
       /app/docker-compose.postgres-host-mount.yml \
       /app/docker-compose.gcp-logging.yml \
       /app/Caddyfile /app/deploy/caddy/Caddyfile.apps-subdomain /opt/agnes-host/ && \
    cp /app/static/maintenance.html /opt/agnes-host/static/ && \
    chmod 0755 /opt/agnes-host/agnes-auto-upgrade.sh \
              /opt/agnes-host/scripts/ops/agnes-chat-sandbox-image.sh \
              /opt/agnes-host/agnes-tls-rotate.sh \
              /opt/agnes-host/agnes-state-applier.sh \
              /opt/agnes-host/post-deploy-smoke-test.sh \
              /opt/agnes-host/tls-fetch.sh && \
    chmod 0644 /opt/agnes-host/agnes-state-applier.service \
              /opt/agnes-host/agnes-state-applier.timer \
              /opt/agnes-host/agnes-state-applier-bootstrap.service \
              /opt/agnes-host/docker-compose.yml \
              /opt/agnes-host/docker-compose.prod.yml \
              /opt/agnes-host/docker-compose.host-mount.yml \
              /opt/agnes-host/docker-compose.tls.yml \
              /opt/agnes-host/docker-compose.postgres.yml \
              /opt/agnes-host/docker-compose.postgres-host-mount.yml \
              /opt/agnes-host/docker-compose.gcp-logging.yml \
              /opt/agnes-host/Caddyfile \
              /opt/agnes-host/scripts/ops/agnes-compose-file.sh \
              /opt/agnes-host/static/maintenance.html

# Build wheel artifact (served at /cli/download)
RUN uv build --wheel --out-dir /app/dist

# Install production dependencies from pyproject.toml. The `[server]` extra
# pulls in connectors-only deps (kbcstorage) that the CLI wheel deliberately
# omits; `[slack-socket]` adds slack_sdk so the optional Slack Socket Mode
# inbound transport works out-of-the-box in the server image (HTTP-only
# deployments simply never enable it; the import stays lazy + fail-closed).
# See [project.optional-dependencies] in pyproject.toml.
#
# EXTRA_EXTRAS appends optional extras to the SAME install, for image variants
# that need them. It is empty by default on purpose: `[docling]` and
# `[embeddings]` each pull torch, which adds gigabytes to every VM's disk and
# image pull — a cost the whole fleet would carry for a capability only some
# instances use. Build the rich variant explicitly instead:
#
#   docker build --build-arg EXTRA_EXTRAS=",docling,embeddings" .
#
# `.github/workflows/image-rich.yml` does exactly that and publishes it under
# a `-rich` tag suffix, so an instance opts in by pointing its image_tag at
# that tag. Note the leading comma — the value is concatenated inside the
# bracket list.
ARG EXTRA_EXTRAS=""
# `extraction` is part of the DEFAULT image since the built-in SharePoint
# pipeline became the only pipeline: its backends (markitdown, pypdfium2)
# are megabytes, not the gigabytes that keep docling/embeddings opt-in, and
# an image that can serve the connector but not convert a document would
# turn every containerized deploy's first crawl into a typed refusal an
# operator can do nothing about without a rebuild.
RUN uv pip install --system --no-cache ".[server,slack-socket,telegram,extraction${EXTRA_EXTRAS}]"

# Run as non-root user for container hardening (C13).
# uid/gid pinned to 999 so host-side chown in startup-script.sh.tpl can match
# without parsing /etc/passwd inside the image. Changing this number breaks
# every existing PD-backed deploy until the operator re-chowns /data.
RUN useradd --system --uid 999 --create-home --shell /usr/sbin/nologin agnes && \
    mkdir -p /data && chown -R agnes:agnes /data && \
    chown -R agnes:agnes /app
USER agnes

# Pre-stage the ADBC Snowflake driver in DuckDB's extension directory so the
# ``snowflake`` community extension can load without a runtime network fetch.
RUN /app/scripts/install-adbc-driver.sh /app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]

# ---------------------------------------------------------------------------
# worker — extraction worker lane image (spec §7.5 / §16 step 7)
# ---------------------------------------------------------------------------
# Byte-for-byte the same image as `base`/`app` above. The extraction
# pipeline is IN-REPO (owner decision 2026-08-31 — connectors/sharepoint/
# crawler.py, one pipeline, no dual modes), so there is nothing left to bake
# in: the former `EXTRACTION_PRODUCER_INSTALL` build-arg, which existed only
# to install an operator's external producer command, was removed with the
# external mode itself.
#
# The stage is kept because `docker-compose.yml`'s `extraction-worker`
# service names it as its build target, and because the split is where an
# operator would add lane-specific runtime deps if they ever needed them.
# The entrypoint is IDENTICAL to `app` — one-image, one-entrypoint still
# holds; `AGNES_ROLE=worker` + `AGNES_WORKER_LANES=extraction` (set by the
# compose service, not baked into the image) select the behavior at runtime,
# not a different CMD here.
#
# The converter backends the lane needs (markitdown, pypdfium2) come from
# the `extraction` optional extra, which the default install above now
# bakes in — every image built from this file can convert documents. The
# `libreoffice-*` packages above cover the legacy Office/OpenDocument
# pre-conversion step the same way. The typed "not installed" refusal
# (`409 extraction_dependencies_missing` / `MissingConversionDependency`)
# remains for bare-metal installs that skipped the extra or the apt packages.
FROM base AS worker

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]

# ---------------------------------------------------------------------------
# app — the default target (MUST stay last: `docker build .` with no
# `--target` builds whichever stage is last in this file, and both CI
# (`docker build -t data-analyst:test .`) and the release workflow
# (`docker/build-push-action@v7`, no `target:`) rely on that being this
# stage, unchanged from before `worker` existed).
# ---------------------------------------------------------------------------
FROM base AS app
