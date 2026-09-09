"""``observability.conversation_export`` config reader -- the push sink's
endpoint is an outbound request carrying a secret header, so the reader
refuses every URL shape a request forgery could steer it to (review
finding: SSRF via the export destination)."""

from __future__ import annotations

import pytest


@pytest.fixture
def load(tmp_path, monkeypatch):
    def _load(yaml_text: str):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
        state = tmp_path / "state"
        state.mkdir(exist_ok=True)
        (state / "instance.yaml").write_text(yaml_text)
        import app.instance_config as mod

        mod.reset_cache()
        try:
            return mod.get_conversation_export_config()
        finally:
            mod.reset_cache()

    return _load


def _yaml(endpoint: str) -> str:
    return f"observability:\n  conversation_export:\n    endpoint: {endpoint!r}\n"


def test_https_endpoint_to_a_named_host_is_accepted(load):
    cfg = load(_yaml("https://collector.example.com/ingest"))
    assert cfg is not None
    assert cfg["endpoint"] == "https://collector.example.com/ingest"


@pytest.mark.parametrize("endpoint", ["http://localhost:4318/ingest", "http://127.0.0.1/x", "http://[::1]:8080/x"])
def test_plain_http_is_accepted_only_for_the_loopback_host(load, endpoint):
    assert load(_yaml(endpoint)) is not None


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://collector.example.com/ingest",  # plain http to a real host
        "https://user:secret@collector.example.com/ingest",  # credentials in the netloc
        "ftp://collector.example.com/ingest",  # not http(s)
        "file:///etc/passwd",
        "gopher://169.254.169.254/",
        "https:///ingest",  # no host at all
        "collector.example.com/ingest",  # no scheme
        "https://[bad",  # unparseable
    ],
)
def test_every_other_shape_disables_the_sink(load, endpoint, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="app.instance_config"):
        assert load(_yaml(endpoint)) is None
    assert any("push sink stays off" in r.getMessage() for r in caplog.records)


def test_blank_endpoint_is_off_without_a_warning(load, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="app.instance_config"):
        assert load(_yaml("")) is None
    assert not any("push sink stays off" in r.getMessage() for r in caplog.records)
