import pytest
from fastapi.testclient import TestClient


class FakeContainer:
    def __init__(self, name, status="running", attrs=None):
        self.name, self.status = name, status
        self.attrs = attrs or {}
        self.removed = self.paused = self.unpaused = False

    def remove(self, force=False):
        self.removed = True

    def pause(self):
        self.paused = True

    def unpause(self):
        self.unpaused = True

    def logs(self, tail=200):
        return b"hello\n"


class FakeVolumes:
    """Separate from `FakeDocker`'s container/network tracking — a real
    Docker client keeps these namespaces independent, and conflating them
    (as a single shared `by_name`/`create()` would) both misfiles the
    cache-volume's fixup container under `by_name` and pollutes
    `networks_created`."""

    def __init__(self):
        self.names: set[str] = set()

    def get(self, name):
        if name not in self.names:
            import docker.errors

            raise docker.errors.NotFound(name)
        return _FakeVolume(name)

    def create(self, name, **kw):
        self.names.add(name)
        return _FakeVolume(name)


class _FakeVolume:
    def __init__(self, name):
        self.name = name


class FakeDocker:
    def __init__(self):
        self.run_calls = []
        self.by_name = {}
        self.networks_created = set()
        self.raise_on_run = None
        self.containers = self
        self.networks = self
        self.volumes = FakeVolumes()

    # containers API
    def run(self, image, **kw):
        if self.raise_on_run is not None:
            raise self.raise_on_run
        self.run_calls.append((image, kw))
        name = kw.get("name")
        if name is None:
            # Anonymous, synchronous fixup container (e.g. the cache-volume
            # chown in `_ensure_cache_volume`) — nothing to track by name.
            return None
        c = FakeContainer(name)
        self.by_name[name] = c
        return c

    def get(self, name):
        if name not in self.by_name:
            import docker.errors

            raise docker.errors.NotFound(name)
        return self.by_name[name]

    def list(self, all=True, filters=None, names=None):
        # containers.list(all=True) vs. networks.list(names=[...]) share
        # this one method, same as the real client aliases both APIs to `self`.
        if names is not None:
            # Docker's `name` filter is a SUBSTRING match.
            return [n for n in self.networks_created if any(w in n for w in names)]
        if filters and "name" in filters:
            wanted = filters["name"]
            return [c for c in self.by_name.values() if c.name in wanted]
        return list(self.by_name.values())

    # networks API (idempotent ensure)
    def create(self, name, **kw):
        self.networks_created.add(name)
        return None


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("APPS_RUNNER_TOKEN", "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr")
    monkeypatch.setenv("APPS_RUNNER_IMAGE_PREFIX", "keboolapublic.azurecr.io/data-app-python-js")
    from services.apps_runner import api

    fake = FakeDocker()
    monkeypatch.setattr(api, "_docker", lambda: fake)
    return TestClient(api.app), fake, tmp_path


SPEC = lambda tmp: {
    "name": "agnes-dataapp-s",
    "image": "keboolapublic.azurecr.io/data-app-python-js:1.6.2",
    "labels": {"agnes.data-app": "app_1"},
    "network": "agnes-apps",
    "config_dir": str(tmp / "apps" / "s"),
    "cache_volume": "agnes-dataapp-cache-s",
    "mem_limit": "1g",
    "cpus": 1.0,
    "env": {"A": "1"},
    # Container hardening, as `src/data_apps/spec.py::build_container_spec`
    # emits it in production: caps/no-new-privileges/pids_limit always on,
    # read-only rootfs off (so no tmpfs mounts) — see that builder's
    # `_READ_ONLY_TMPFS` for why read-only is opt-in.
    "cap_drop": ["ALL"],
    "security_opt": ["no-new-privileges:true"],
    "pids_limit": 512,
    "read_only": False,
    "tmpfs": {},
}


def test_auth_required(client):
    c, _, tmp = client
    assert c.post("/apps/s/up", json={"spec": SPEC(tmp), "config_json": {}}).status_code == 401


def test_up_writes_config_and_runs(client):
    c, fake, tmp = client
    r = c.post(
        "/apps/s/up", headers={"X-Runner-Token": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"}, json={"spec": SPEC(tmp), "config_json": {"dataApp": {}}}
    )
    assert r.status_code == 200
    assert (tmp / "apps" / "s" / "config.json").exists()
    # run_calls[0] is the one-time cache-volume chown fixup (anonymous, no
    # "name" key); the named app container is the last call.
    image, kw = fake.run_calls[-1]
    assert kw["name"] == "agnes-dataapp-s"
    assert kw["detach"] is True
    assert fake.volumes.names == {"agnes-dataapp-cache-s"}


def test_up_uses_a_bounded_restart_policy(client):
    """Crash-loop guard: the app container must run under a bounded
    `on-failure` policy, never unbounded `unless-stopped`. The upstream
    entrypoint is not idempotent (it `git clone`s into `/app` unconditionally,
    so any restart onto a non-empty `/app` dies), so a data app that fails its
    first boot would otherwise be restarted forever — externally dead, burning
    CPU, and never settling into a state reap-idle can reconcile to `error`."""
    c, fake, tmp = client
    r = c.post(
        "/apps/s/up", headers={"X-Runner-Token": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"}, json={"spec": SPEC(tmp), "config_json": {"dataApp": {}}}
    )
    assert r.status_code == 200
    _, kw = fake.run_calls[-1]
    policy = kw["restart_policy"]
    assert policy["Name"] == "on-failure"
    assert policy.get("MaximumRetryCount", 0) >= 1


class TestContainerHardening:
    """Defense-in-depth options threaded from the spec into the real
    docker-py `containers.run` call — never applied to the chat-sandbox path
    (`/sandboxes/*`, see `tests/test_apps_runner_sandboxes.py`), which
    legitimately needs broader write access for agent-authored code."""

    def test_up_applies_cap_drop_and_no_new_privileges(self, client):
        c, fake, tmp = client
        c.post(
            "/apps/s/up", headers={"X-Runner-Token": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"}, json={"spec": SPEC(tmp), "config_json": {"dataApp": {}}}
        )
        _, kw = fake.run_calls[-1]
        assert kw["cap_drop"] == ["ALL"]
        assert kw["security_opt"] == ["no-new-privileges:true"]

    def test_up_applies_pids_limit(self, client):
        c, fake, tmp = client
        c.post(
            "/apps/s/up", headers={"X-Runner-Token": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"}, json={"spec": SPEC(tmp), "config_json": {"dataApp": {}}}
        )
        _, kw = fake.run_calls[-1]
        assert kw["pids_limit"] == 512

    def test_up_leaves_rootfs_writable_by_default(self, client):
        """The read-only rootfs is opt-in: its tmpfs list is unverified
        against the shipped nginx+supervisord runtime image, so the default
        spec must not mount a read-only rootfs and must add no tmpfs."""
        c, fake, tmp = client
        c.post(
            "/apps/s/up", headers={"X-Runner-Token": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"}, json={"spec": SPEC(tmp), "config_json": {"dataApp": {}}}
        )
        _, kw = fake.run_calls[-1]
        assert kw["read_only"] is False
        assert kw["tmpfs"] == {}

    def test_up_honors_an_operator_enabled_read_only_rootfs(self, client):
        """When an operator turns `data_apps.container_read_only` on, the
        spec's `read_only` + `tmpfs` reach docker-py unchanged."""
        c, fake, tmp = client
        spec = SPEC(tmp) | {"read_only": True, "tmpfs": {"/tmp": "", "/app": ""}}
        c.post("/apps/s/up", headers={"X-Runner-Token": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"}, json={"spec": spec, "config_json": {"dataApp": {}}})
        _, kw = fake.run_calls[-1]
        assert kw["read_only"] is True
        assert kw["tmpfs"] == {"/tmp": "", "/app": ""}

    def test_up_hardens_a_spec_from_an_older_app_process(self, client):
        """Mid-upgrade the `app` process can be older than this sidecar and
        mint a spec with no hardening keys at all. That must still land
        hardened (and NOT KeyError), with the rootfs left writable."""
        c, fake, tmp = client
        spec = {
            k: v
            for k, v in SPEC(tmp).items()
            if k not in ("cap_drop", "security_opt", "pids_limit", "read_only", "tmpfs")
        }
        r = c.post("/apps/s/up", headers={"X-Runner-Token": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"}, json={"spec": spec, "config_json": {"dataApp": {}}})
        assert r.status_code == 200
        _, kw = fake.run_calls[-1]
        assert kw["cap_drop"] == ["ALL"]
        assert kw["security_opt"] == ["no-new-privileges:true"]
        assert kw["pids_limit"] == 512
        assert kw["read_only"] is False


def test_up_rejects_foreign_image(client):
    c, _, tmp = client
    spec = SPEC(tmp) | {"image": "evil/image:1"}
    r = c.post("/apps/s/up", headers={"X-Runner-Token": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"}, json={"spec": spec, "config_json": {}})
    assert r.status_code == 400
    assert r.json()["detail"] == "image_not_allowed"


def test_stop_and_status(client):
    c, fake, tmp = client
    c.post("/apps/s/up", headers={"X-Runner-Token": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"}, json={"spec": SPEC(tmp), "config_json": {}})
    r = c.post("/apps/s/stop", headers={"X-Runner-Token": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"}, json={"mode": "recreate"})
    assert r.status_code == 200
    assert fake.by_name["agnes-dataapp-s"].removed


def test_up_twice_removes_old_container_and_reruns(client):
    c, fake, tmp = client
    c.post("/apps/s/up", headers={"X-Runner-Token": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"}, json={"spec": SPEC(tmp), "config_json": {}})
    first = fake.by_name["agnes-dataapp-s"]
    r = c.post("/apps/s/up", headers={"X-Runner-Token": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"}, json={"spec": SPEC(tmp), "config_json": {}})
    assert r.status_code == 200
    assert first.removed
    named_runs = [kw for _, kw in fake.run_calls if kw.get("name")]
    assert len(named_runs) == 2
    # the network — and the cache volume + its chown fixup — are created
    # once (idempotent), not once per `up`: 2 named app-container runs + 1
    # anonymous chown fixup = 3 total `run()` calls.
    assert len(fake.run_calls) == 3
    assert fake.networks_created == {"agnes-apps"}
    assert fake.volumes.names == {"agnes-dataapp-cache-s"}


def test_resume_unpauses_container(client):
    c, fake, _ = client
    fake.by_name["agnes-dataapp-s"] = FakeContainer("agnes-dataapp-s", status="paused")
    r = c.post("/apps/s/resume", headers={"X-Runner-Token": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"})
    assert r.status_code == 200
    assert r.json() == {"status": "running"}
    assert fake.by_name["agnes-dataapp-s"].unpaused


def test_resume_absent_is_404(client):
    c, _, _ = client
    r = c.post("/apps/s/resume", headers={"X-Runner-Token": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"})
    assert r.status_code == 404


def test_logs_returns_decoded_string(client):
    c, fake, _ = client
    fake.by_name["agnes-dataapp-s"] = FakeContainer("agnes-dataapp-s")
    r = c.get("/apps/s/logs", headers={"X-Runner-Token": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"})
    assert r.status_code == 200
    assert r.json() == {"logs": "hello\n"}


def test_logs_absent_is_404(client):
    c, _, _ = client
    r = c.get("/apps/s/logs", headers={"X-Runner-Token": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"})
    assert r.status_code == 404


def test_list_apps_filters_dataapp_names(client):
    c, fake, _ = client
    fake.by_name["agnes-dataapp-a"] = FakeContainer("agnes-dataapp-a")
    fake.by_name["agnes-dataapp-b"] = FakeContainer("agnes-dataapp-b", status="paused")
    fake.by_name["some-other-container"] = FakeContainer("some-other-container")
    r = c.get("/apps", headers={"X-Runner-Token": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"})
    assert r.status_code == 200
    names = {row["name"] for row in r.json()["apps"]}
    assert names == {"agnes-dataapp-a", "agnes-dataapp-b"}


def test_status_paused(client):
    c, fake, _ = client
    fake.by_name["agnes-dataapp-s"] = FakeContainer("agnes-dataapp-s", status="paused")
    r = c.get("/apps/s/status", headers={"X-Runner-Token": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"})
    assert r.status_code == 200
    assert r.json() == {"container": "paused", "ready": False}


def test_status_maps_exited_to_stopped(client):
    c, fake, _ = client
    fake.by_name["agnes-dataapp-s"] = FakeContainer("agnes-dataapp-s", status="exited")
    r = c.get("/apps/s/status", headers={"X-Runner-Token": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"})
    assert r.status_code == 200
    assert r.json() == {"container": "stopped", "ready": False}


def test_up_maps_image_not_found(client):
    c, fake, tmp = client
    import docker.errors

    fake.raise_on_run = docker.errors.ImageNotFound("no such image")
    r = c.post("/apps/s/up", headers={"X-Runner-Token": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"}, json={"spec": SPEC(tmp), "config_json": {}})
    assert r.status_code == 400
    assert r.json()["detail"] == "image_not_found"


def test_up_maps_docker_api_error(client):
    c, fake, tmp = client
    import docker.errors

    fake.raise_on_run = docker.errors.APIError("daemon unavailable")
    r = c.post("/apps/s/up", headers={"X-Runner-Token": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"}, json={"spec": SPEC(tmp), "config_json": {}})
    assert r.status_code == 502
    assert r.json()["detail"].startswith("docker_error:")


# ---------------------------------------------------------------------------
# _resolve_host_path — DinD bind-mount source translation
# ---------------------------------------------------------------------------
#
# In production apps-runner itself runs as a container whose own `/data` is
# the named volume `data` (see docker-compose.yml). Docker resolves a bind
# mount's *source* against the daemon's host namespace, not the caller's, so
# bind-mounting a path from apps-runner's own mount namespace as another
# container's bind source silently resolves to an empty, unrelated host
# directory instead of the config.json this process just wrote. These tests
# use the fake self-container's mount `Destination` set to `tmp` (the
# fixture's own tmp_path) rather than the real `/data` — that's an arbitrary
# choice standing in for whatever this container's config-dir ancestor mount
# happens to be; the resolution logic doesn't care what the literal
# destination string is, only that it's a prefix of the path being resolved.


def test_up_resolves_config_mount_via_dind(client, monkeypatch):
    c, fake, tmp = client
    import socket

    monkeypatch.setattr(socket, "gethostname", lambda: "runner123")
    fake.by_name["runner123"] = FakeContainer(
        "runner123",
        attrs={"Mounts": [{"Destination": str(tmp), "Source": "/var/lib/docker/volumes/proj_data/_data"}]},
    )
    r = c.post("/apps/s/up", headers={"X-Runner-Token": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"}, json={"spec": SPEC(tmp), "config_json": {}})
    assert r.status_code == 200
    _, kw = fake.run_calls[-1]
    bind_sources = list(kw["volumes"].keys())
    assert "/var/lib/docker/volumes/proj_data/_data/apps/s" in bind_sources
    assert str(tmp / "apps" / "s") not in bind_sources


def test_up_keeps_container_path_when_not_containerized(client, monkeypatch):
    """apps-runner's own container isn't found by the Docker daemon (bare
    host/dev/E2E process talking to the same daemon) — the config bind
    source stays the raw container_path, matching pre-fix behavior."""
    c, fake, tmp = client
    import socket

    monkeypatch.setattr(socket, "gethostname", lambda: "not-a-container")
    r = c.post("/apps/s/up", headers={"X-Runner-Token": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"}, json={"spec": SPEC(tmp), "config_json": {}})
    assert r.status_code == 200
    _, kw = fake.run_calls[-1]
    assert str(tmp / "apps" / "s") in kw["volumes"]


def test_up_creates_the_apps_network_despite_a_substring_named_leftover(client):
    """Docker's `name` network filter is a SUBSTRING match, so a host that has
    run chat's allowlist egress mode (which leaves `agnes-apps-internal`)
    would answer the lookup for `agnes-apps` with that other network — and the
    app's own network would never be created, failing every run afterwards."""
    c, fake, tmp = client
    fake.networks_created.add("agnes-apps-internal")

    r = c.post("/apps/s/up", headers={"X-Runner-Token": "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr"}, json={"spec": SPEC(tmp), "config_json": {}})

    assert r.status_code == 200
    assert "agnes-apps" in fake.networks_created


def test_docker_client_gets_a_pull_sized_timeout(monkeypatch):
    """docker-py defaults every daemon call to a 60 s HTTP timeout.

    `containers.run` pulls the image inline when it is missing locally, so on
    a host that has never run a data app that 60 s covers fetching ~1.3 GB.
    When it expires the pull is torn down, the retried `create` raises
    ImageNotFound, and the sidecar answers 400 `image_not_found` — blaming a
    missing image for what was a truncated download. The client must allow a
    pull-sized budget, and it must be tunable per deployment.
    """
    import docker

    import services.apps_runner.api as api

    seen = {}

    def _from_env(**kwargs):
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(docker, "from_env", _from_env)

    api._docker()
    assert seen.get("timeout", 60) >= 300, f"pull-sized budget required; got {seen.get('timeout')}"

    seen.clear()
    monkeypatch.setenv("APPS_RUNNER_DOCKER_TIMEOUT", "900")
    api._docker()
    assert seen.get("timeout") == 900
