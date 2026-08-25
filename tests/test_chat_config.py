from pathlib import Path

from app.chat.config import load_chat_config


def test_default_disabled(tmp_path: Path):
    yaml = tmp_path / "instance.yaml"
    yaml.write_text("instance_name: test\n")
    cfg = load_chat_config(yaml)
    assert cfg.enabled is False
    assert cfg.provider == "e2b"
    assert cfg.concurrency_per_user == 3
    assert cfg.idle_ttl_seconds == 1800
    assert cfg.per_tool_call_seconds == 90
    assert cfg.per_session_bq_scan_bytes == 20 * 1024**3
    assert cfg.daily_anthropic_spend_usd == 20.0
    assert cfg.e2b_template_id is None
    assert cfg.e2b_workspace_max_bytes == 100 * 1024 * 1024
    assert cfg.e2b_kill_on_ws_disconnect is True


def test_enabled_with_overrides(tmp_path: Path):
    yaml = tmp_path / "instance.yaml"
    yaml.write_text(
        "instance_name: test\n"
        "chat:\n"
        "  enabled: true\n"
        "  provider: e2b\n"
        "  e2b_template_id: agnes-chat\n"
        "  e2b_workspace_max_bytes: 52428800\n"
        "  e2b_kill_on_ws_disconnect: false\n"
        "  concurrency_per_user: 5\n"
        "  idle_ttl_seconds: 900\n"
    )
    cfg = load_chat_config(yaml)
    assert cfg.enabled is True
    assert cfg.provider == "e2b"
    assert cfg.e2b_template_id == "agnes-chat"
    assert cfg.e2b_workspace_max_bytes == 52428800
    assert cfg.e2b_kill_on_ws_disconnect is False
    assert cfg.concurrency_per_user == 5
    assert cfg.idle_ttl_seconds == 900


def test_docker_provider_defaults(tmp_path: Path):
    """`chat.docker_*` knobs are inert under the default e2b provider but must
    still carry usable defaults — an operator flipping `provider: docker`
    should get a working stack from the image tag alone."""
    yaml = tmp_path / "instance.yaml"
    yaml.write_text("instance_name: test\n")
    cfg = load_chat_config(yaml)
    assert cfg.docker_image == "agnes-chat-sandbox:latest"
    assert cfg.docker_network == "agnes-apps"
    assert cfg.docker_mem_limit == "2g"
    assert cfg.docker_cpus == 1.0
    assert cfg.docker_pids_limit == 512
    assert cfg.docker_egress_mode == "open"
    assert cfg.docker_max_total_sandboxes == 10


def test_docker_provider_overrides(tmp_path: Path):
    yaml = tmp_path / "instance.yaml"
    yaml.write_text(
        "chat:\n"
        "  enabled: true\n"
        "  provider: docker\n"
        "  docker_image: agnes-chat-sandbox:0.77.32\n"
        "  docker_network: agnes-chat\n"
        "  docker_mem_limit: 4g\n"
        "  docker_cpus: 2.5\n"
        "  docker_pids_limit: 256\n"
        "  docker_egress_mode: none\n"
        "  docker_max_total_sandboxes: 3\n"
    )
    cfg = load_chat_config(yaml)
    assert cfg.provider == "docker"
    assert cfg.docker_image == "agnes-chat-sandbox:0.77.32"
    assert cfg.docker_network == "agnes-chat"
    assert cfg.docker_mem_limit == "4g"
    assert cfg.docker_cpus == 2.5
    assert cfg.docker_pids_limit == 256
    assert cfg.docker_egress_mode == "none"
    assert cfg.docker_max_total_sandboxes == 3


def test_unknown_docker_egress_mode_normalizes_to_open(tmp_path: Path, caplog):
    # "allowlist" graduated to a real mode; "wide-open" stays a typo
    y = tmp_path / "instance.yaml"
    y.write_text("chat:\n  enabled: true\n  provider: docker\n  docker_egress_mode: wide-open\n")
    cfg = load_chat_config(y)
    assert cfg.docker_egress_mode == "open"
    assert "docker_egress_mode" in caplog.text


def test_blank_string_keys_fall_back_to_their_defaults(tmp_path: Path):
    """A key written with nothing after it parses to YAML null; the naive
    `str(raw.get(key, default))` then produced the string "None" — and for
    docker_egress_mode the *valid-looking* mode "none", silently cutting the
    sandbox off from the internet (the #1148 trap). Blank must mean default."""
    y = tmp_path / "instance.yaml"
    y.write_text(
        "chat:\n  enabled: true\n  provider:\n  harness:\n  docker_egress_mode:\n  on_detach:\n  llm:\n    auth:\n"
    )
    cfg = load_chat_config(y)
    assert cfg.provider == "e2b"
    assert cfg.harness == "claude-code"
    assert cfg.docker_egress_mode == "open"
    assert cfg.on_detach == "pause"
    assert cfg.llm_auth == "api_key"


def test_blank_numeric_and_bool_keys_fall_back_to_their_defaults(tmp_path: Path, caplog):
    """The numeric variant of the same trap: `int(raw.get(key, default))` on a
    key written with no value raised `int(None)` out of load_chat_config,
    turning one blank line into chat being disabled at boot; `bool(None)`
    silently flipped e2b_kill_on_ws_disconnect to False. Garbage values warn
    and fall back rather than aborting the load."""
    y = tmp_path / "instance.yaml"
    y.write_text(
        "chat:\n"
        "  enabled: true\n"
        "  docker_cpus:\n"
        "  docker_pids_limit:\n"
        "  docker_max_total_sandboxes:\n"
        "  concurrency_per_user:\n"
        "  detach_linger_seconds:\n"
        "  e2b_kill_on_ws_disconnect:\n"
        "  bootstrap_marketplace:\n"
        "  rate_messages_per_hour: not-a-number\n"
    )
    cfg = load_chat_config(y)
    assert cfg.docker_cpus == 1.0
    assert cfg.docker_pids_limit == 512
    assert cfg.docker_max_total_sandboxes == 10
    assert cfg.concurrency_per_user == 3
    assert cfg.detach_linger_seconds == 60
    assert cfg.idle_grace_seconds == 60
    assert cfg.e2b_kill_on_ws_disconnect is True
    assert cfg.bootstrap_marketplace is True  # blank yaml value → the (on) default
    assert cfg.rate_messages_per_hour == 100
    assert "rate_messages_per_hour" in caplog.text


def test_textual_kill_flag_drives_on_detach_and_echo_identically(tmp_path: Path):
    """Both readers of the deprecated kill flag must share one parser: with
    plain truthiness in _parse_on_detach, `"no"` was echoed as disabled while
    still switching on_detach to kill."""
    y = tmp_path / "instance.yaml"
    y.write_text('chat:\n  enabled: true\n  e2b_kill_on_ws_disconnect: "no"\n')
    cfg = load_chat_config(y)
    assert cfg.e2b_kill_on_ws_disconnect is False
    assert cfg.on_detach == "pause"

    y.write_text('chat:\n  enabled: true\n  e2b_kill_on_ws_disconnect: "yes"\n')
    cfg = load_chat_config(y)
    assert cfg.e2b_kill_on_ws_disconnect is True
    assert cfg.on_detach == "kill"


def test_legacy_sandbox_uid_knob_is_dropped(tmp_path: Path):
    """The deprecated sandbox_uid / require_isolation keys are silently
    ignored — the ChatConfig dataclass no longer exposes them and the
    loader doesn't trip on their presence in older instance.yaml files."""
    yaml = tmp_path / "instance.yaml"
    yaml.write_text(
        "chat:\n  enabled: true\n  e2b_template_id: agnes-chat\n  require_isolation: true\n  sandbox_uid: 1500\n"
    )
    cfg = load_chat_config(yaml)
    assert cfg.enabled is True
    assert not hasattr(cfg, "require_isolation")
    assert not hasattr(cfg, "sandbox_uid")


def test_detach_defaults():
    cfg = load_chat_config(Path("/nonexistent"))
    assert cfg.on_detach == "pause"
    assert cfg.detach_linger_seconds == 60
    assert cfg.idle_grace_seconds == 60
    assert cfg.paused_ttl_seconds == 7 * 24 * 3600


def test_idle_grace_seconds_defaults_to_detach_linger_seconds(tmp_path: Path):
    """Tier 1 grace window: when idle_grace_seconds is not set explicitly,
    it falls back to whatever detach_linger_seconds resolves to — an
    operator pinning only the legacy knob keeps working unmodified."""
    p = tmp_path / "instance.yaml"
    p.write_text("chat:\n  enabled: true\n  detach_linger_seconds: 45\n")
    cfg = load_chat_config(p)
    assert cfg.detach_linger_seconds == 45
    assert cfg.idle_grace_seconds == 45


def test_idle_grace_seconds_explicit_override(tmp_path: Path):
    p = tmp_path / "instance.yaml"
    p.write_text("chat:\n  enabled: true\n  detach_linger_seconds: 45\n  idle_grace_seconds: 120\n")
    cfg = load_chat_config(p)
    assert cfg.detach_linger_seconds == 45
    assert cfg.idle_grace_seconds == 120


def test_legacy_kill_knob_maps_to_on_detach_kill(tmp_path, caplog):
    p = tmp_path / "instance.yaml"
    p.write_text("chat:\n  enabled: true\n  e2b_kill_on_ws_disconnect: true\n")
    cfg = load_chat_config(p)
    assert cfg.on_detach == "kill"
    assert "deprecated" in caplog.text.lower()


def test_explicit_on_detach_wins_over_legacy_knob(tmp_path):
    p = tmp_path / "instance.yaml"
    p.write_text("chat:\n  enabled: true\n  e2b_kill_on_ws_disconnect: true\n  on_detach: pause\n")
    assert load_chat_config(p).on_detach == "pause"


def test_unknown_on_detach_normalizes_to_pause(tmp_path):
    p = tmp_path / "instance.yaml"
    p.write_text("chat:\n  enabled: true\n  on_detach: explode\n")
    assert load_chat_config(p).on_detach == "pause"


def test_egress_allow_out_parsed(tmp_path: Path):
    y = tmp_path / "instance.yaml"
    y.write_text("chat:\n  enabled: true\n  egress_allow_out:\n    - api.github.com\n")
    cfg = load_chat_config(y)
    assert cfg.egress_allow_out == ["api.github.com"]


def test_egress_allow_out_defaults_empty(tmp_path: Path):
    y = tmp_path / "instance.yaml"
    y.write_text("chat:\n  enabled: true\n")
    cfg = load_chat_config(y)
    assert cfg.egress_allow_out == []


# --- AGNES_CHAT_ENABLED env override (#1022 feature-flag canonicalization) ---


def test_env_var_enables_over_yaml_false(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGNES_CHAT_ENABLED", "1")
    y = tmp_path / "instance.yaml"
    y.write_text("chat:\n  enabled: false\n")
    assert load_chat_config(y).enabled is True


def test_env_var_disables_over_yaml_true(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGNES_CHAT_ENABLED", "0")
    y = tmp_path / "instance.yaml"
    y.write_text("chat:\n  enabled: true\n")
    assert load_chat_config(y).enabled is False


def test_env_var_applies_even_without_an_instance_yaml(monkeypatch):
    monkeypatch.setenv("AGNES_CHAT_ENABLED", "true")
    assert load_chat_config(Path("/nonexistent")).enabled is True


def test_no_env_var_falls_through_to_yaml(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("AGNES_CHAT_ENABLED", raising=False)
    y = tmp_path / "instance.yaml"
    y.write_text("chat:\n  enabled: true\n")
    assert load_chat_config(y).enabled is True


# --- AGNES_CHAT_PROVIDER env override (infra-pinned provider) ---------------


def test_provider_env_var_wins_over_yaml(tmp_path: Path, monkeypatch):
    """Infrastructure pins the provider via env (the customer-instance
    module's per-VM chat_provider field) — code-reviewed Terraform must beat
    the hand-edited instance.yaml overlay on the data disk."""
    monkeypatch.setenv("AGNES_CHAT_PROVIDER", "kai-agent")
    y = tmp_path / "instance.yaml"
    y.write_text("chat:\n  enabled: true\n  provider: e2b\n")
    assert load_chat_config(y).provider == "kai-agent"


def test_provider_env_var_applies_even_without_an_instance_yaml(monkeypatch):
    """A FRESH machine boots with no instance.yaml yet — the infra-pinned
    provider must apply there too, or first boot silently runs e2b."""
    monkeypatch.setenv("AGNES_CHAT_PROVIDER", "kai-agent")
    assert load_chat_config(Path("/nonexistent")).provider == "kai-agent"


def test_provider_blank_env_falls_through_to_yaml(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGNES_CHAT_PROVIDER", "  ")
    y = tmp_path / "instance.yaml"
    y.write_text("chat:\n  enabled: true\n  provider: docker\n")
    assert load_chat_config(y).provider == "docker"
    monkeypatch.delenv("AGNES_CHAT_PROVIDER")
    assert load_chat_config(y).provider == "docker"


def test_approvals_kill_switch_uses_the_shared_truthy_rule(tmp_path: Path):
    """`bool("false")` is True, so a plain truth test would read a quoted YAML
    value — or one produced by an env-substituted template — as "on" and leave
    approvals armed for an operator who asked for them off. Every boolean
    config value in Agnes goes through coerce_flag_value
    (docs/feature-flags.md) (Devin Review on #1157)."""
    for written, expected in (
        (None, True),
        ("false", False),
        ('"false"', False),
        ("off", False),
        ('"0"', False),
        ("true", True),
    ):
        yaml = tmp_path / "instance.yaml"
        body = "instance_name: test\nchat:\n  enabled: true\n"
        if written is not None:
            body += f"  approvals_enabled: {written}\n"
        yaml.write_text(body)
        assert load_chat_config(yaml).approvals_enabled is expected, written


def test_the_approvals_env_override_is_honoured(tmp_path: Path, monkeypatch):
    """The registry and docs advertise AGNES_CHAT_APPROVALS_ENABLED, and
    /admin/server-config resolves flags env-first — so reading only the YAML
    left the documented switch inert AND the admin panel reporting a value the
    running gate does not honour (Devin Review on #1157)."""
    from app.chat.config import load_chat_config

    yaml = tmp_path / "instance.yaml"
    yaml.write_text("instance_name: test\nchat:\n  enabled: true\n  approvals_enabled: true\n")

    monkeypatch.setenv("AGNES_CHAT_APPROVALS_ENABLED", "0")
    assert load_chat_config(yaml).approvals_enabled is False, "env must win over the yaml value"

    monkeypatch.setenv("AGNES_CHAT_APPROVALS_ENABLED", "true")
    yaml.write_text("instance_name: test\nchat:\n  enabled: true\n  approvals_enabled: false\n")
    assert load_chat_config(yaml).approvals_enabled is True

    monkeypatch.delenv("AGNES_CHAT_APPROVALS_ENABLED")
    assert load_chat_config(yaml).approvals_enabled is False, "…and the yaml stands when env is unset"


def test_no_doc_or_dockerfile_names_an_egress_mode_config_does_not_accept():
    """`_parse_docker_egress_mode` accepts exactly `open` | `none` | `allowlist`
    and silently falls back to `open` — fully open egress — for anything else,
    with one WARNING line as the only trace. An operator copying a value out of
    these docs that is not in the accepted set gets the OPPOSITE of what the
    doc told them to expect, with no error. (`docker_egress_mode: closed` was
    exactly this: it read as "closed", parsed as "open".) This walks the same
    files a copy-pasting operator would, rather than trusting them to match the
    parser by inspection."""
    import re

    from app.chat.config import _parse_docker_egress_mode

    accepted = set()
    for candidate in ("open", "none", "allowlist", "not-a-real-mode"):
        mode = _parse_docker_egress_mode({"docker_egress_mode": candidate})
        if mode == candidate:
            accepted.add(candidate)
    assert accepted == {"open", "none", "allowlist"}, "the accepted set drifted — update this test too"

    files = [
        Path("docs/cloud-chat.md"),
        Path("app/initial_workspace_default/docker-sandbox/Dockerfile"),
        Path("app/initial_workspace_default/e2b-template/Dockerfile"),
    ]
    pattern = re.compile(r"docker_egress_mode\s*:\s*([A-Za-z_-]+)")
    for f in files:
        text = f.read_text(encoding="utf-8")
        for value in pattern.findall(text):
            assert value in accepted, (
                f"{f} names `docker_egress_mode: {value}`, which "
                f"_parse_docker_egress_mode does not accept — it would silently "
                f"fall back to 'open' at runtime instead of doing what the doc says"
            )
