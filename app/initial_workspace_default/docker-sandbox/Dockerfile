# Agnes chat sandbox image. Runs claude-agent-sdk + the agnes CLI inside a
# local Docker container.
#
# Build (from the repo root):
#   docker build -t agnes-chat-sandbox:latest app/initial_workspace_default/docker-sandbox
# then set `chat.docker_image` in instance.yaml. See README.md here and
# docs/cloud-chat.md → "Docker provider (self-hosted)".
#
# Base: `python:3.13-slim`. What the runner actually depends on is a Debian
# userland with python3 + pip installing into /usr/local, which this gives
# directly.
#
# NOT baked in (deliberate trade-offs):
#   - `app/chat/runner.py` — staged per spawn (as a plain host write into the
#     bind-mounted session dir, so runner changes need no image rebuild).
#   - the `agnes` CLI wheel — staged per spawn from the server's own build so
#     the in-sandbox CLI version always matches the running server exactly.
#   - the user workspace — bind-mounted, not copied.
#   - firewall rules — egress policy is the Docker network
#     (`chat.docker_egress_mode`) plus the workspace PreToolUse hook.
FROM python:3.13-slim

# Marks the filesystem contract below. Bump when the contract changes so an
# operator can tell a stale image from a current one:
#   docker inspect -f '{{ index .Config.Labels "agnes.chat-sandbox.contract" }}' agnes-chat-sandbox:latest
# v2: added `matplotlib` to the pip install block below (charts) — an image
# still reporting "1" predates that dependency and cannot draw a chart.
LABEL agnes.chat-sandbox.contract="2"

# Node.js is required by claude-agent-sdk: it shells out to the
# `@anthropic-ai/claude-code` CLI for its MCP handshake, and without that
# binary the SDK fails with "Control request timeout: initialize".
# git: the optional marketplace bootstrap (`agnes refresh-marketplace
# --bootstrap`) clones plugin repos.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg git \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

# claude-agent-sdk + the agnes CLI's *runtime dependency closure* — NOT the
# agnes package itself (staged per spawn, see above). cli/main.py imports every
# command module eagerly, so a missing dep breaks the whole `agnes` binary, not
# one subcommand. Keep in sync with the CLI-relevant entries of
# [project.dependencies] in pyproject.toml.
#
# `matplotlib` is the one entry here that the CLI never imports. It is baked in
# because the agent is asked to draw charts and had no way to obtain it: under
# `chat.docker_egress_mode: none`
# an in-sandbox `pip install matplotlib` cannot reach PyPI at all, and even on
# an open egress it is a slow download inside every session that wants a plot.
# Without it the agent improvises — the reported failure was an SVG written to
# `/tmp` and a user told to open a path that does not exist on their machine.
# The workspace CLAUDE.md pairs this with the delivery half of the rule: a
# chart is returned as inline SVG in the reply, never as a file.
RUN pip install --no-cache-dir \
    "claude-agent-sdk>=0.2.87,<0.3.0" \
    "duckdb>=1.5.2" \
    "httpx>=0.27.0" \
    "pyyaml>=6.0" \
    "PyJWT>=2.13.0" \
    "typer>=0.12.0" \
    "rich>=13.0.0" \
    "filelock>=3.13,<4" \
    "pyarrow>=12.0.0" \
    "pandas>=2.0.0" \
    "numpy>=1.24.0" \
    "sqlglot>=30.0.0" \
    "matplotlib>=3.8"

# --- Filesystem contract ----------------------------------------------------
# ChatManager pins the sandbox env (app/chat/manager.py::_spawn_runner):
#   HOME=/home/user            must exist and be writable
#   PATH=/usr/local/bin:/usr/bin:/bin
#   cwd  /work                 the bind-mounted session dir
# and the runner installs the staged wheel with `pip install --no-deps
# --break-system-packages` (no --user), so the console script has to land in
# /usr/local/bin.
#
# The world-writable modes are what make that work for ANY uid: the provider
# runs the container as the uid owning the bind-mounted session dir on the host
# (docker's standard answer to bind-mount ownership), which is almost never
# 1000. Everything here is a single-process, cap-dropped,
# no-new-privileges container with no setuid binaries and no root process to
# escalate to, so a writable /usr/local buys an attacker nothing they don't
# already have as the agent user.
RUN useradd --create-home --shell /bin/bash --uid 1000 user \
    && mkdir -p /work \
    && chmod 0777 /work /home/user \
    && chmod -R a+rwX "$(python3 -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" /usr/local/bin

# Fallback only — the provider always passes an explicit non-root `user`.
USER user
WORKDIR /work
ENV HOME=/home/user \
    PYTHONUNBUFFERED=1

# No ENTRYPOINT/CMD: the container's command is the runner argv the manager
# builds (`python3 /work/runner.py --session-id …`), passed at create time.
