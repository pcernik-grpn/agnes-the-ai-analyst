from src.data_apps.spec import SLUG_RE, build_config_json, build_container_spec

APP = {
    "id": "app_abc",
    "slug": "sales",
    "repo_mode": "internal",
    "repo_url": "",
    "repo_branch": "main",
    "runtime_tag": "",
    "mem_limit": "",
    "cpu_limit": "",
    "env": '{"FOO": "bar"}',
    "sleep_mode": "recreate",
}
DEFAULTS = {
    "runtime_image": "keboolapublic.azurecr.io/data-app-python-js:1.6.2_python-3.13_node-24",
    "default_mem_limit": "1g",
    "default_cpus": 1.0,
}


def test_slug_re():
    assert SLUG_RE.match("sales-dash")
    assert not SLUG_RE.match("Sales")
    assert not SLUG_RE.match("-x")


def test_config_json_internal_repo_embeds_token():
    cfg = build_config_json(
        APP,
        secrets={"DB_PASSWORD": "s3"},
        clone_url="http://app:8000/data-apps.git/sales",
        clone_token="PATPAT",
        service_token="SERVICE",
        viewer_secret="VIEWER",
    )
    git = cfg["dataApp"]["git"]
    # The token is embedded into the repository URL: the runtime image only
    # adds credentials to HTTPS clone URLs, never plain HTTP, so Agnes's
    # internal http://app:8000 backend needs the creds pre-embedded or the
    # container clone prompts for a username and crash-loops.
    assert git["repository"] == "http://agnes:PATPAT@app:8000/data-apps.git/sales"
    assert git["branch"] == "agnes-live"
    assert git["username"] == "agnes"
    assert git["#password"] == "PATPAT"
    # secrets: caller-provided + injected platform vars
    assert cfg["dataApp"]["secrets"]["#DB_PASSWORD"] == "s3"
    # NOT the clone token: `AGNES_TOKEN` is the app's RUNTIME credential for
    # the Agnes API. This assertion used to read "PATPAT" and so encoded the
    # very swap that left every hosted app unable to read data.
    assert cfg["dataApp"]["secrets"]["AGNES_TOKEN"] == "SERVICE"
    assert "input" not in cfg  # Data Loader never configured on this platform


def test_config_json_draft_uses_pinned_branch():
    from src.data_apps.spec import build_config_json

    row = {"repo_mode": "internal", "is_draft": True, "draft_branch": "init", "slug": "d--init"}
    cfg = build_config_json(
        row, secrets={}, clone_url="http://app:8000/data-apps.git/d", clone_token="PAT", service_token="SERVICE", viewer_secret="VIEWER"
    )
    assert cfg["dataApp"]["git"]["branch"] == "init"
    assert cfg["dataApp"]["git"]["repository"].endswith("/data-apps.git/d")
    assert cfg["dataApp"]["git"]["#password"] == "PAT"


def test_config_json_prod_still_agnes_live():
    from src.data_apps.spec import build_config_json

    row = {"repo_mode": "internal", "slug": "d"}
    cfg = build_config_json(
        row, secrets={}, clone_url="http://x/data-apps.git/d", clone_token="PAT", service_token="SERVICE", viewer_secret="VIEWER"
    )
    assert cfg["dataApp"]["git"]["branch"] == "agnes-live"


def test_container_spec_defaults_and_overrides():
    spec = build_container_spec(APP, defaults=DEFAULTS, data_dir="/data")
    assert spec["name"] == "agnes-dataapp-sales"
    assert spec["image"] == DEFAULTS["runtime_image"]
    assert spec["mem_limit"] == "1g"
    assert spec["network"] == "agnes-apps"
    assert spec["labels"] == {"agnes.data-app": "app_abc"}
    assert spec["cache_volume"] == "agnes-dataapp-cache-sales"
    assert spec["env"]["AGNES_URL"] == "http://app:8000"
    assert spec["env"]["FOO"] == "bar"
    assert "DATA_LOADER_API_URL" not in spec["env"]
    # `ports` is a test-only escape hatch the apps-runner API accepts (see
    # services/apps_runner/api.py::up) so tests/test_data_apps_e2e_docker.py
    # can reach the runtime container directly without the ingress proxy.
    # Production specs must never set it — apps are reached exclusively
    # through the proxy.
    assert "ports" not in spec


class TestContainerHardening:
    """Defense-in-depth for an internet-facing web server (never applied to
    the chat-sandbox path — see `services/apps_runner/sandbox_api.py`, which
    legitimately needs broader write access for agent-authored code).

    Note `DEFAULTS` above carries none of the hardening keys — the builder
    must supply its own defaults, because an instance.yaml written before
    these knobs existed omits them entirely."""

    def test_defaults_are_hardened(self):
        spec = build_container_spec(APP, defaults=DEFAULTS, data_dir="/data")
        assert spec["cap_drop"] == ["ALL"]
        assert spec["security_opt"] == ["no-new-privileges:true"]
        assert spec["pids_limit"] == 512

    def test_read_only_rootfs_is_off_by_default_with_no_tmpfs(self):
        """The read-only rootfs is deliberately opt-in: the tmpfs list it
        needs is unverified against the shipped runtime image, whose nginx +
        supervisord write to /var/run, /var/log and /var/cache/nginx. So the
        default spec leaves the rootfs writable AND mounts no tmpfs — a tmpfs
        over /app would shadow the entrypoint's clone target for no benefit
        while the rootfs is writable anyway."""
        spec = build_container_spec(APP, defaults=DEFAULTS, data_dir="/data")
        assert spec["read_only"] is False
        assert spec["tmpfs"] == {}

    def test_operator_can_enable_read_only_and_override_pids_limit(self):
        defaults = {**DEFAULTS, "container_read_only": True, "container_pids_limit": 128}
        spec = build_container_spec(APP, defaults=defaults, data_dir="/data")
        assert spec["pids_limit"] == 128
        assert spec["read_only"] is True
        # Only in the opt-in case is scratch space mounted: /tmp for general
        # scratch, /app because the entrypoint clones the repo + installs
        # deps there on every boot. Known-incomplete for the shipped image —
        # whoever enables this extends the list from what actually fails.
        assert spec["tmpfs"] == {"/tmp": "", "/app": ""}

    def test_hardening_without_a_knob_stays_on_regardless(self):
        defaults = {**DEFAULTS, "container_read_only": False, "container_pids_limit": 0}
        spec = build_container_spec(APP, defaults=defaults, data_dir="/data")
        assert spec["cap_drop"] == ["ALL"]
        assert spec["security_opt"] == ["no-new-privileges:true"]
        # 0/None is not "unlimited" — it falls back to the module default
        # rather than handing a compromised app an unbounded process budget.
        assert spec["pids_limit"] == 512


def test_config_json_external_repo():
    app_external = {
        "id": "app_ext",
        "slug": "custom-app",
        "repo_mode": "external",
        "repo_url": "https://github.com/user/repo.git",
        "repo_branch": "feature-x",
        "runtime_tag": "",
        "mem_limit": "",
        "cpu_limit": "",
        "env": "{}",
    }
    cfg = build_config_json(app_external, secrets={}, clone_url="", clone_token="", service_token="SERVICE", viewer_secret="VIEWER")
    git = cfg["dataApp"]["git"]
    assert git["repository"] == "https://github.com/user/repo.git"
    assert git["branch"] == "feature-x"
    assert "username" not in git
    assert "#password" not in git


def test_container_spec_malformed_env_json():
    app_bad_env = APP.copy()
    app_bad_env["env"] = '{"invalid": json}'
    try:
        build_container_spec(app_bad_env, defaults=DEFAULTS, data_dir="/data")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "invalid env JSON" in str(exc)
        assert "sales" in str(exc)


def test_container_spec_malformed_cpu_limit():
    app_bad_cpu = APP.copy()
    app_bad_cpu["cpu_limit"] = "not-a-number"
    try:
        build_container_spec(app_bad_cpu, defaults=DEFAULTS, data_dir="/data")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "invalid cpu_limit" in str(exc)
        assert "sales" in str(exc)


def test_config_json_embeds_percent_encoded_token():
    # A token with URL-significant characters must be percent-encoded in the
    # embedded repository URL so the container's `git clone` parses it.
    from src.data_apps.spec import build_config_json

    row = {"repo_mode": "internal", "slug": "s"}
    cfg = build_config_json(
        row, secrets={}, clone_url="http://app:8000/data-apps.git/s", clone_token="a/b@c:d", service_token="SERVICE", viewer_secret="VIEWER"
    )
    assert cfg["dataApp"]["git"]["repository"] == "http://agnes:a%2Fb%40c%3Ad@app:8000/data-apps.git/s"
    assert cfg["dataApp"]["git"]["#password"] == "a/b@c:d"  # raw token still in the field


def test_config_json_does_not_double_embed_credentials():
    from src.data_apps.spec import build_config_json

    row = {"repo_mode": "internal", "slug": "s"}
    cfg = build_config_json(
        row,
        secrets={},
        clone_url="http://agnes:existing@app:8000/data-apps.git/s",
        clone_token="NEW",
        service_token="SERVICE",
        viewer_secret="VIEWER",
    )
    assert cfg["dataApp"]["git"]["repository"] == "http://agnes:existing@app:8000/data-apps.git/s"


def test_config_json_external_repo_repository_untouched():
    from src.data_apps.spec import build_config_json

    row = {"repo_mode": "external", "repo_url": "https://github.com/org/repo", "repo_branch": "main", "slug": "s"}
    cfg = build_config_json(row, secrets={}, clone_url="ignored", clone_token="PAT", service_token="SERVICE", viewer_secret="VIEWER")
    # External repos keep their own URL + branch; no token embedding, no username field.
    assert cfg["dataApp"]["git"] == {"repository": "https://github.com/org/repo", "branch": "main"}


def test_the_runtime_credential_is_the_service_token_not_the_clone_token():
    """Devin Review on #1239: one parameter was serving two different roles.

    `AGNES_TOKEN` is what the running app calls the Agnes API with. When the
    deploy path was corrected to pass the git-scoped clone token (so the first
    clone would stop failing), the shared parameter carried it into
    `AGNES_TOKEN` too — and `data-app-git:<slug>` is admitted by exactly one
    surface, the internal git backend. Every hosted app therefore started
    healthy and was refused by every data endpoint it called, which is
    invisible from the outside: the container is up and the app renders.
    """
    from src.data_apps.spec import build_config_json

    row = {"repo_mode": "internal", "slug": "s"}
    cfg = build_config_json(
        row,
        secrets={},
        clone_url="http://app:8000/data-apps.git/s",
        clone_token="GIT-SCOPED",
        service_token="SERVICE-SCOPED",
        viewer_secret="VIEWER-SECRET",
    )
    secrets = cfg["dataApp"]["secrets"]
    assert secrets["AGNES_TOKEN"] == "SERVICE-SCOPED", "the app cannot read Agnes data with a clone token"
    assert cfg["dataApp"]["git"]["#password"] == "GIT-SCOPED", "the clone still needs the git-scoped one"
    # The third credential — the per-app key the container verifies the
    # proxy's `X-Agnes-Viewer` assertion with. Never the service token, never
    # the clone token.
    assert secrets["AGNES_VIEWER_SECRET"] == "VIEWER-SECRET"


def test_viewer_secret_is_required_not_defaulted():
    """A forgotten kwarg must fail loudly at the call site, not ship a
    container that cannot verify any viewer assertion."""
    import pytest

    from src.data_apps.spec import build_config_json

    with pytest.raises(TypeError):
        build_config_json({"repo_mode": "internal", "slug": "s"}, secrets={}, clone_url="x", clone_token="c", service_token="s")  # type: ignore[call-arg]


def test_container_spec_carries_slug_and_data_identity_env():
    """`AGNES_APP_SLUG` (for the assertion's `aud` check) and
    `AGNES_DATA_IDENTITY` are platform-owned and written AFTER the user's env
    merge, so an app-authored value can never override them. A row without
    the PG-only column reads `owner`."""
    spec = build_container_spec(
        {**APP, "env": '{"AGNES_APP_SLUG": "spoofed", "AGNES_DATA_IDENTITY": "viewer"}'},
        defaults=DEFAULTS,
        data_dir="/data",
    )
    assert spec["env"]["AGNES_APP_SLUG"] == "sales"
    assert spec["env"]["AGNES_DATA_IDENTITY"] == "owner"
    viewer_spec = build_container_spec({**APP, "data_identity": "viewer"}, defaults=DEFAULTS, data_dir="/data")
    assert viewer_spec["env"]["AGNES_DATA_IDENTITY"] == "viewer"


def test_the_deploy_path_passes_the_service_token():
    """Source-level: the two credentials exist in `_deploy` and are not swapped."""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1] / "app" / "api" / "data_apps.py").read_text(encoding="utf-8")
    call = src[src.index("config_json = build_config_json(") :][:1400]
    assert "clone_token=git_token" in call
    assert "service_token=jwt_token" in call
    # The viewer secret derives from the SAME new token id the row now carries.
    assert "viewer_secret=derive_viewer_secret(slug, new_token_id)" in call
