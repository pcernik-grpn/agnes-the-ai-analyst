"""Contract tests against the *installed* docker SDK.

Pins the docker SDK surface the chat stack depends on: the chat
sandbox's Docker calls are mocked everywhere else in the suite, so a `docker`
bump that renames a kwarg or changes a return shape would stay green here and
fail only on a live spawn. These assertions run with no daemon and no network —
they only introspect the SDK.

Every pin below is a call shape in `services/apps_runner/sandbox_api.py` (and,
for the two `/apps/*`-shared helpers, `services/apps_runner/api.py`); keep them
in sync if those callsites change.
"""

import inspect

import pytest


def test_from_env_is_the_client_factory():
    """`api._docker()` is `docker.from_env()` for both halves of the sidecar."""
    import docker

    assert callable(docker.from_env)


@pytest.mark.parametrize(
    "kwarg",
    ["stdin_open", "tty", "command", "working_dir", "environment", "labels", "user", "name", "detach"],
)
def test_containers_run_accepts_the_create_kwargs_the_sandbox_spec_sets(kwarg):
    """`sandbox_up` passes these to `containers.run`; docker-py validates them
    against this list and raises `create_unexpected_kwargs_error` otherwise.

    `stdin_open` is the load-bearing one: without it the runner gets EOF on
    stdin immediately and no chat message ever reaches the agent.
    """
    from docker.models.containers import RUN_CREATE_KWARGS

    assert kwarg in RUN_CREATE_KWARGS, f"docker-py no longer accepts containers.run(..., {kwarg}=...)"


@pytest.mark.parametrize(
    "kwarg",
    ["mem_limit", "nano_cpus", "pids_limit", "cap_drop", "security_opt", "extra_hosts", "init"],
)
def test_containers_run_accepts_the_hardening_kwargs(kwarg):
    """The D7 container hardening — dropping any of these silently would widen
    the sandbox boundary, so pin them."""
    from docker.models.containers import RUN_HOST_CONFIG_KWARGS

    assert kwarg in RUN_HOST_CONFIG_KWARGS, f"docker-py no longer accepts containers.run(..., {kwarg}=...)"


def test_run_kwargs_translate_into_the_expected_host_config():
    """End-to-end (daemon-free) check that the spec `sandbox_up` builds produces
    the binds / limits / hardening we intend — not just accepted kwargs."""
    from docker.models.containers import _create_container_args

    args = _create_container_args(
        {
            "version": "1.44",
            "image": "agnes-chat-sandbox:dev",
            "name": "agnes-chatsbx-x-1",
            "detach": True,
            "stdin_open": True,
            "tty": False,
            "command": ["python3", "/work/runner.py"],
            "working_dir": "/work",
            "environment": {"AGNES_SESSION_ID": "x"},
            "labels": {"agnes.chat-sandbox": "1"},
            "network": "agnes-apps",
            "volumes": {"/host/sessions/x": {"bind": "/work", "mode": "rw"}},
            "mem_limit": "2g",
            "nano_cpus": 1_000_000_000,
            "pids_limit": 512,
            "user": "999:999",
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "extra_hosts": {"host.docker.internal": "host-gateway"},
        }
    )
    host_config = args["host_config"]
    assert host_config["Binds"] == ["/host/sessions/x:/work:rw"]
    assert host_config["PidsLimit"] == 512
    assert host_config["CapDrop"] == ["ALL"]
    assert host_config["SecurityOpt"] == ["no-new-privileges:true"]
    assert host_config["ExtraHosts"] == ["host.docker.internal:host-gateway"]
    assert host_config["NanoCpus"] == 1_000_000_000
    assert args["stdin_open"] is True
    # `network` becomes a networking_config rather than a create kwarg — the
    # sandbox must land on the Agnes bridge, not the default one.
    assert "networking_config" in args


def test_attach_socket_exposes_the_held_response_for_buffered_reads():
    """`sandbox_stream` opens the hijacked connection via `attach_socket`
    (holding the socket directly is what lets teardown unblock the reader
    thread) and reads frames through `sock._response.raw._fp.fp` — the
    BufferedReader that http.client's header parse read ahead into; raw-socket
    reads lose replayed frames sitting in that buffer. Pin the `params` kwarg
    and docker-py's `_response` GC-guard assignment the buffered path rides."""
    from docker import APIClient

    params = inspect.signature(APIClient.attach_socket).parameters
    assert "params" in params, "docker APIClient.attach_socket lost `params`"
    src = inspect.getsource(APIClient._get_raw_response_socket)
    assert "sock._response = response" in src, (
        "docker-py no longer parks the HTTP response on the attach socket — "
        "sandbox_stream's buffered-reader path depends on that reference"
    )


@pytest.mark.parametrize(
    "method",
    ["attach_socket", "put_archive", "get_archive", "pause", "unpause", "stop", "remove"],
)
def test_container_keeps_the_methods_the_sandbox_api_calls(method):
    from docker.models.containers import Container

    assert hasattr(Container, method), f"docker Container lost `{method}` — services/apps_runner calls it"


def test_networks_create_accepts_internal():
    """`docker_egress_mode: none` creates an `internal` bridge (no route off
    the host) — without this kwarg that mode would silently allow egress."""
    from docker import APIClient

    params = inspect.signature(APIClient.create_network).parameters
    assert "internal" in params and "driver" in params


@pytest.mark.parametrize("name", ["ImageNotFound", "NotFound", "APIError", "DockerException"])
def test_docker_error_types_the_sidecar_maps_still_exist(name):
    """`api._docker_errors` maps these to 400/502 and `_container` catches
    NotFound; a rename would turn a clean 404 into an unhandled 500."""
    import docker.errors

    assert hasattr(docker.errors, name)
