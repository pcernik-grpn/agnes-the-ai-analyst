"""apps-runner — the only process holding the Docker socket.

Deliberately dumb: no registry access, no RBAC, no policy. The Agnes app
decides *what* should run; this sidecar only translates to Docker calls.
Bound on the internal compose network only; token-gated.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import socket
from pathlib import Path

from fastapi import Body, FastAPI, Header, HTTPException

from services.apps_runner.audit_report import report_event

logger = logging.getLogger(__name__)

app = FastAPI(title="agnes-apps-runner", docs_url=None, redoc_url=None)


# docker-py defaults every daemon call to a 60 s HTTP timeout. That is fine
# for the calls this sidecar mostly makes (inspect/stop/logs) but not for
# `containers.run`, which pulls the image inline when it is missing locally:
# the runtime image is ~1.3 GB, so on a host that has never run a data app
# those 60 s have to cover the whole fetch. When they ran out the daemon tore
# the pull down ("failed to cleanup extract-…", "Error deleting lease") and
# docker-py's retried `create` raised ImageNotFound — so the sidecar reported
# a missing image for what was really a truncated download. Pre-pulling the
# runtime image (startup script + agnes-auto-upgrade) is the actual fix; this
# is the backstop, tunable because link speed is a per-deployment fact.
_DOCKER_TIMEOUT_ENV = "APPS_RUNNER_DOCKER_TIMEOUT"
_DOCKER_TIMEOUT_DEFAULT = 600


def _docker():
    import docker

    try:
        timeout = int(os.environ.get(_DOCKER_TIMEOUT_ENV, "") or _DOCKER_TIMEOUT_DEFAULT)
    except ValueError:
        timeout = _DOCKER_TIMEOUT_DEFAULT
    return docker.from_env(timeout=timeout)


def _check_token(x_runner_token: str | None) -> None:
    expected = os.environ.get("APPS_RUNNER_TOKEN", "")
    if not expected or x_runner_token != expected:
        raise HTTPException(status_code=401, detail="bad_runner_token")


def _safe_report(action: str, params: dict) -> None:
    """Belt-and-braces on top of ``report_event``'s own internal swallow: a
    container action (start/stop/resume) must never fail because reporting
    it did, even if a future change to ``report_event`` reintroduces a raise."""
    try:
        report_event(action, params)
    except Exception:
        logger.warning("apps-runner: report_event raised for %r; container action unaffected", action, exc_info=True)


def _container(name: str):
    import docker.errors

    try:
        return _docker().containers.get(name)
    except docker.errors.NotFound:
        return None


# The upstream runtime image runs its entire entrypoint as a fixed non-root
# `app` user (uid/gid 1000) — it never runs as, or elevates to, root.
_CACHE_VOLUME_OWNER = "1000:1000"


def _ensure_cache_volume(client, spec: dict) -> None:
    """Create the per-app cache volume if missing, and chown it to the
    runtime image's non-root ``app`` user.

    A fresh Docker named volume is created empty and root-owned. Nothing is
    baked into the image at ``/home/app/.cache`` for Docker to inherit
    ownership from on first mount (there's no directory there at all), and
    the upstream entrypoint never runs as root — so on a brand-new volume
    `uv sync` (and any other cache-writing setup step) fails with
    `Permission denied`. Fixed up once, at creation, via a throwaway
    container using the *same* allowlisted runtime image (no new image
    dependency, no change to the app container's own non-root ``user``).
    """
    import docker.errors

    name = spec["cache_volume"]
    try:
        client.volumes.get(name)
        return  # already exists — already fixed up on a prior `up()`
    except docker.errors.NotFound:
        pass
    client.volumes.create(name)
    client.containers.run(
        spec["image"],
        entrypoint=["chown", "-R", _CACHE_VOLUME_OWNER, "/cache"],
        user="0:0",
        volumes={name: {"bind": "/cache", "mode": "rw"}},
        remove=True,
    )


def _resolve_host_path(container_path: str) -> str:
    """Translate a path inside *this* apps-runner container to the path the
    Docker daemon must use as a bind-mount *source* for a new container.

    Docker resolves bind-mount sources against the daemon's host filesystem
    namespace, never the calling container's — but in production apps-runner
    is itself a container whose own ``/data`` is the named volume ``data``
    (see docker-compose.yml), so a path like ``/data/apps/<slug>`` computed
    inside this process is meaningless to the daemon as a bind source and
    silently resolves to an empty, unrelated directory.

    Finds *this* container (Docker sets the container hostname to its own
    id; ``HOSTNAME`` carries the same value) via the daemon, and walks its
    ``Mounts`` for the entry whose ``Destination`` is the longest prefix of
    ``container_path`` — that mount's ``Source`` is what the daemon
    understands (a named-volume mountpoint or a host directory, either
    way). Returns ``container_path`` unchanged when this process isn't
    itself a container the daemon knows about (bare host/dev/E2E process
    talking to the same daemon) — the historical, already-correct behavior
    for that case.
    """
    import docker.errors

    hostname = os.environ.get("HOSTNAME") or socket.gethostname()
    try:
        self_container = _docker().containers.get(hostname)
    except (docker.errors.NotFound, docker.errors.APIError):
        return container_path

    best_dest = ""
    best_source = ""
    for mount in self_container.attrs.get("Mounts", []) or []:
        dest = (mount.get("Destination") or "").rstrip("/")
        if not dest:
            continue
        if container_path == dest or container_path.startswith(dest + "/"):
            if len(dest) > len(best_dest):
                best_dest, best_source = dest, mount.get("Source") or ""

    if not best_dest:
        return container_path
    return best_source + container_path[len(best_dest) :]


def _docker_errors(fn):
    """Map Docker SDK / transport errors to structured HTTP responses.

    ``docker.errors.ImageNotFound`` -> 400 ``image_not_found`` (bad spec, the
    caller's fault). Anything else that means "couldn't talk to Docker" —
    ``docker.errors.APIError`` (covers ``NotFound`` races too),
    ``docker.errors.DockerException``, or a raw
    ``requests.exceptions.ConnectionError`` from the transport — becomes a
    502 ``docker_error: <message>``. ``HTTPException`` raised deliberately by
    the handler (401/400/404) passes through unchanged.

    Async handlers (the chat-sandbox attach stream) get an async wrapper so
    FastAPI still awaits them — a sync wrapper would return the coroutine
    object instead of running it.
    """
    import asyncio

    def _map(exc: Exception):
        import docker.errors
        import requests.exceptions

        if isinstance(exc, docker.errors.ImageNotFound):
            return HTTPException(status_code=400, detail="image_not_found")
        if isinstance(
            exc, (docker.errors.APIError, docker.errors.DockerException, requests.exceptions.ConnectionError)
        ):
            return HTTPException(status_code=502, detail=f"docker_error: {exc}")
        return None

    if asyncio.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def awrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:
                mapped = _map(exc)
                if mapped is not None:
                    raise mapped from exc
                raise

        return awrapper

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            mapped = _map(exc)
            if mapped is not None:
                raise mapped from exc
            raise

    return wrapper


@app.post("/apps/{slug}/up")
@_docker_errors
def up(slug: str, payload: dict = Body(...), x_runner_token: str | None = Header(default=None)):
    """Start (or replace) the container for ``slug``.

    ``spec["ports"]`` is an optional Docker port-publish mapping (e.g.
    ``{"8888/tcp": 18888}``, the same shape `docker-py`'s
    ``containers.run(ports=...)`` accepts) — a test-only escape hatch so
    ``tests/test_data_apps_e2e_docker.py`` can reach the runtime container's
    nginx directly from the host without standing up the ingress proxy.
    Production specs (`src/data_apps/spec.py::build_container_spec`) never
    set this key; apps are reached exclusively through the ingress proxy.
    """
    _check_token(x_runner_token)
    spec, config_json = payload["spec"], payload["config_json"]
    prefix = os.environ.get("APPS_RUNNER_IMAGE_PREFIX", "")
    if not prefix or not str(spec["image"]).startswith(prefix + ":"):
        raise HTTPException(status_code=400, detail="image_not_allowed")
    cfg_dir = Path(spec["config_dir"])
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(json.dumps(config_json, indent=2))
    client = _docker()
    # Exact name only: Docker's `name` filter is a SUBSTRING match, so asking
    # for `agnes-apps` also matches `agnes-apps-internal` and the network the
    # app actually needs would never be created — every run then fails on an
    # unknown network. Same hazard the sandbox path hit (Devin Review on #1148).
    if not [n for n in client.networks.list(names=[spec["network"]]) if getattr(n, "name", n) == spec["network"]]:
        client.networks.create(spec["network"], driver="bridge")
    _ensure_cache_volume(client, spec)
    old = _container(spec["name"])
    if old is not None:
        old.remove(force=True)
    client.containers.run(
        spec["image"],
        name=spec["name"],
        detach=True,
        labels=spec["labels"],
        network=spec["network"],
        environment=spec["env"],
        mem_limit=spec["mem_limit"],
        nano_cpus=int(float(spec["cpus"]) * 1e9),
        # Defense-in-depth, built by `src/data_apps/spec.py::build_container_spec`:
        # no Linux capabilities, no privilege escalation, and a fork-bomb
        # ceiling. Read with `.get()` and secure fallbacks so a spec minted by
        # an older `app` process mid-upgrade still gets the hardening rather
        # than a KeyError. Never applied to the chat-sandbox path
        # (`services/apps_runner/sandbox_api.py::sandbox_up`), which
        # legitimately needs broader privileges for agent-authored code.
        cap_drop=spec.get("cap_drop") or ["ALL"],
        security_opt=spec.get("security_opt") or ["no-new-privileges:true"],
        pids_limit=int(spec.get("pids_limit") or 512),
        # A read-only rootfs is opt-in and OFF by default — its tmpfs list is
        # unverified against the shipped nginx+supervisord image (see
        # `_READ_ONLY_TMPFS` in the spec builder), so it is the operator's
        # switch, not a default. Absent from the spec means off.
        read_only=bool(spec.get("read_only", False)),
        tmpfs=spec.get("tmpfs") or {},
        ports=spec.get("ports"),
        volumes={
            _resolve_host_path(str(cfg_dir)): {"bind": "/data", "mode": "rw"},
            spec["cache_volume"]: {"bind": "/home/app/.cache", "mode": "rw"},
        },
        # Bounded on-failure, NOT unbounded unless-stopped. The upstream
        # runtime entrypoint is not idempotent — it `git clone`s into `/app`
        # unconditionally, so any restart onto a non-empty `/app` dies with
        # "destination path already exists". Under `unless-stopped` that is an
        # infinite crash loop: the app is externally dead (nginx never
        # listens), it burns CPU forever, and nothing surfaces the failure.
        # After MaximumRetryCount the daemon gives up, the container settles as
        # `exited` (→ `status()` reports "stopped"), and the reap-idle
        # reconcile scan flips the row to `error`. Trade-off: a healthy
        # container is no longer auto-restarted across a daemon/VM reboot — it
        # survives the reboot as `exited`, so the reconcile scan marks its row
        # `error` and the app needs an explicit redeploy; the next request does
        # NOT rebuild it, because the ingress proxy only wakes `sleeping` rows
        # and renders `error` without re-checking. `unless-stopped` was no
        # better across a reboot: the daemon restarted the container straight
        # into the non-idempotent clone above, so the app came back
        # crash-looping rather than serving. Reconciling a dead container to
        # `sleeping` instead would restore wake-on-request self-healing, but it
        # would also hide a genuine crash loop behind a silent wake-retry
        # cycle; surfacing the failure is the deliberate choice here.
        restart_policy={"Name": "on-failure", "MaximumRetryCount": 3},
    )
    _safe_report("data_app.container_up", {"slug": slug})
    return {"status": "started"}


@app.post("/apps/{slug}/stop")
@_docker_errors
def stop(slug: str, payload: dict = Body(...), x_runner_token: str | None = Header(default=None)):
    _check_token(x_runner_token)
    c = _container(f"agnes-dataapp-{slug}")
    if c is None:
        return {"status": "absent"}
    if payload.get("mode") == "pause":
        c.pause()
        _safe_report("data_app.container_stop", {"slug": slug, "mode": "pause"})
        return {"status": "paused"}
    c.remove(force=True)
    _safe_report("data_app.container_stop", {"slug": slug, "mode": "removed"})
    return {"status": "removed"}


@app.post("/apps/{slug}/resume")
@_docker_errors
def resume(slug: str, x_runner_token: str | None = Header(default=None)):
    _check_token(x_runner_token)
    c = _container(f"agnes-dataapp-{slug}")
    if c is None:
        raise HTTPException(status_code=404, detail="absent")
    c.unpause()
    _safe_report("data_app.container_resume", {"slug": slug})
    return {"status": "running"}


@app.get("/apps/{slug}/status")
@_docker_errors
def status(slug: str, x_runner_token: str | None = Header(default=None)):
    """Container status contract — exactly one of:

    ``"running" | "paused" | "stopped" | "absent"``

    Any other Docker-reported state (``exited``, ``created``, ``restarting``,
    ``dead``, ...) for a container that still exists is folded into
    ``"stopped"``; ``ready`` is only ever true for ``"running"``.
    """
    _check_token(x_runner_token)
    c = _container(f"agnes-dataapp-{slug}")
    if c is None:
        return {"container": "absent", "ready": False}
    if c.status == "paused":
        state = "paused"
    elif c.status == "running":
        state = "running"
    else:
        state = "stopped"
    ready = False
    if state == "running":
        try:
            with socket.create_connection((f"agnes-dataapp-{slug}", 8888), timeout=2):
                ready = True
        except OSError:
            ready = False
    return {"container": state, "ready": ready}


@app.get("/apps/{slug}/logs")
@_docker_errors
def logs(slug: str, tail: int = 200, x_runner_token: str | None = Header(default=None)):
    _check_token(x_runner_token)
    c = _container(f"agnes-dataapp-{slug}")
    if c is None:
        raise HTTPException(status_code=404, detail="absent")
    return {"logs": c.logs(tail=tail).decode("utf-8", errors="replace")}


@app.get("/apps")
@_docker_errors
def list_apps(x_runner_token: str | None = Header(default=None)):
    _check_token(x_runner_token)
    rows = [
        {"name": c.name, "status": c.status}
        for c in _docker().containers.list(all=True)
        if c.name.startswith("agnes-dataapp-")
    ]
    return {"apps": rows}


# The chat-sandbox half of the socket API (`/sandboxes/*`, used by the chat
# gateway's DockerSandboxProvider). Imported at the bottom, after the helpers
# above exist: sandbox_api reaches back into this module lazily for
# `_docker`/`_check_token`/`_container`/`_resolve_host_path` so both halves
# share one Docker seam (and one monkeypatch point in tests).
from services.apps_runner.sandbox_api import router as _sandbox_router

app.include_router(_sandbox_router)
