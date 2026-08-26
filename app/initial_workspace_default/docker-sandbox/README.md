# Agnes chat — Docker sandbox image

Every cloud-chat session with `chat.provider: docker` runs in a container built from
the `Dockerfile` here, created by the `apps-runner` sidecar (the only process
that holds the Docker socket).

Operator walkthrough — prerequisites, egress modes, pause/resume semantics:
[`docs/cloud-chat.md`](../../../docs/cloud-chat.md) →
*Docker provider (self-hosted)*.

## Build it (operator one-time setup)

```bash
# from the repo root, on the host whose Docker daemon runs the sandboxes
docker build -t agnes-chat-sandbox:latest app/initial_workspace_default/docker-sandbox
```

Then point the instance at it:

```yaml
chat:
  enabled: true
  provider: docker
  docker_image: "agnes-chat-sandbox:latest"
```

and give the sidecar permission to run that image:

```bash
CHAT_SANDBOX_IMAGE_PREFIX=agnes-chat-sandbox   # in .env, next to APPS_RUNNER_TOKEN
```

The image is **not** published to a registry by the Agnes release pipeline —
operators build it locally. It must exist
on the machine running the Docker daemon; the boot gate refuses to start chat
(with the build command in the log line) when it is missing.

## Tags are mutable — rebuild deliberately

`:latest` is a mutable tag: a rebuild is
picked up by the next sandbox spawn with no Agnes redeploy. Pin a version tag
(`agnes-chat-sandbox:0.77.32`) in `chat.docker_image` for production if you want
rollouts to be explicit. `agnes.chat-sandbox.contract` is a label carrying the
filesystem-contract version this image satisfies:

```bash
docker inspect -f '{{ index .Config.Labels "agnes.chat-sandbox.contract" }}' agnes-chat-sandbox:latest
```

## The filesystem contract

`ChatManager` pins the sandbox env, so the image has to provide:

| Contract | Why |
|---|---|
| writable `$HOME` = `/home/user` | the inner `claude` CLI writes `~/.claude/` on startup; an unwritable HOME surfaces as `Control request timeout: initialize` |
| `/work` exists and is writable | the per-session dir is bind-mounted here and is the runner's cwd |
| `pip install --no-deps` console scripts land on `/usr/local/bin` | `PATH` is pinned to `/usr/local/bin:/usr/bin:/bin`; this is how the staged `agnes` CLI becomes callable |
| non-root | `runner.py` runs the agent with `permission_mode=bypassPermissions`; the container boundary is the compensating control |

Those dirs are world-writable because the container runs as the **host uid that
owns the session directory** (bind mounts carry host ownership), which is
generally not the image's own `user`. See the Dockerfile comment for why that is
safe here.

## Updating the dependency pins

When `pyproject.toml` bumps `claude-agent-sdk` or another CLI runtime dep, edit
the `pip install` block here and rebuild.
