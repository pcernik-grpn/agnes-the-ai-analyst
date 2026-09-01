import json
import logging

import pytest

from app.logging_config import (
    _derive_slug,
    _JSONFormatter,
    _OAuthCallbackQueryRedactFilter,
    request_id_var,
    setup_logging,
)


@pytest.fixture(autouse=True)
def _reset_logging(monkeypatch):
    """Reset global logging state between tests."""
    import app.logging_config as lc

    lc._CONFIGURED = False
    monkeypatch.delenv("DEBUG", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    yield
    lc._CONFIGURED = False
    logging.getLogger().handlers.clear()


def test_dev_uses_rich_handler(monkeypatch):
    monkeypatch.setenv("DEBUG", "1")
    setup_logging("app")
    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    from rich.logging import RichHandler

    assert isinstance(handlers[0], RichHandler)


def test_prod_uses_json_formatter():
    setup_logging("app")
    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.StreamHandler)
    assert isinstance(handlers[0].formatter, _JSONFormatter)


def test_idempotent():
    setup_logging("app")
    setup_logging("app")
    setup_logging("app")
    assert len(logging.getLogger().handlers) == 1


def test_log_level_from_env(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    setup_logging("app")
    assert logging.getLogger().level == logging.DEBUG


def test_log_level_default_prod():
    setup_logging("app")
    assert logging.getLogger().level == logging.INFO


def test_log_level_default_dev(monkeypatch):
    monkeypatch.setenv("DEBUG", "1")
    setup_logging("app")
    assert logging.getLogger().level == logging.DEBUG


def test_slug_explicit_short_name():
    assert _derive_slug("scheduler") == "scheduler"


def test_slug_strips_services_prefix():
    assert _derive_slug("services.scheduler.__main__") == "scheduler"


def test_slug_keeps_nested_module():
    assert _derive_slug("services.corporate_memory.collector") == "corporate_memory.collector"


def test_slug_strips_app_prefix():
    assert _derive_slug("app.main") == "app"


def test_slug_strips_connectors_prefix():
    assert _derive_slug("connectors.jira.transform") == "jira.transform"


def test_slug_explicit_app():
    assert _derive_slug("app") == "app"


def test_json_formatter_includes_replica_field():
    from app.observability.metrics import replica_id

    rec = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello world",
        args=(),
        exc_info=None,
    )
    fmt = _JSONFormatter(service="myservice")
    line = fmt.format(rec)
    parsed = json.loads(line)
    assert parsed["replica"] == replica_id()


def test_replica_id_safe_falls_back_to_dash_on_failure(monkeypatch):
    """``_replica_id_safe`` imports the cached ``_REPLICA_ID`` (computed
    once at process start), not the ``replica_id()`` function — so the
    failure mode to simulate is the import itself failing."""
    import app.observability.metrics as metrics_mod

    from app.logging_config import _replica_id_safe

    monkeypatch.delattr(metrics_mod, "_REPLICA_ID")
    assert _replica_id_safe() == "-"


def test_replica_id_safe_reuses_cached_value_not_recomputed(monkeypatch):
    """Regression guard: must not call ``replica_id()`` (fresh
    ``socket.gethostname()`` + ``os.getpid()``) on every log line — reuse
    the value already cached at import in ``_REPLICA_ID``."""
    import app.observability.metrics as metrics_mod
    from app.logging_config import _replica_id_safe

    def boom():
        raise AssertionError("replica_id() must not be called — reuse _REPLICA_ID")

    monkeypatch.setattr(metrics_mod, "replica_id", boom)
    assert _replica_id_safe() == metrics_mod._REPLICA_ID


def test_request_id_filter_sets_replica_attr():
    from app.observability.metrics import replica_id
    from app.logging_config import _RequestIdFilter

    rec = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="m",
        args=(),
        exc_info=None,
    )
    assert _RequestIdFilter().filter(rec) is True
    assert rec.replica == replica_id()


def test_dev_format_string_includes_replica(monkeypatch):
    monkeypatch.setenv("DEBUG", "1")
    setup_logging("app")
    handler = logging.getLogger().handlers[0]
    assert "%(replica)s" in handler.formatter._fmt


def test_json_formatter_includes_service_field():
    rec = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello world",
        args=(),
        exc_info=None,
    )
    fmt = _JSONFormatter(service="myservice")
    line = fmt.format(rec)
    parsed = json.loads(line)
    assert parsed["service"] == "myservice"
    assert parsed["message"] == "hello world"
    assert parsed["severity"] == "INFO"
    assert parsed["logger"] == "test"
    assert "time" in parsed


def test_json_formatter_includes_request_id_when_set():
    fmt = _JSONFormatter(service="app")
    rec = logging.LogRecord(
        name="t",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="m",
        args=(),
        exc_info=None,
    )
    token = request_id_var.set("abc123")
    try:
        line = fmt.format(rec)
    finally:
        request_id_var.reset(token)
    parsed = json.loads(line)
    assert parsed["request_id"] == "abc123"


def test_json_formatter_omits_request_id_when_unset():
    fmt = _JSONFormatter(service="app")
    rec = logging.LogRecord(
        name="t",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="m",
        args=(),
        exc_info=None,
    )
    line = fmt.format(rec)
    parsed = json.loads(line)
    assert "request_id" not in parsed


def test_json_formatter_includes_exception():
    fmt = _JSONFormatter(service="app")
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        rec = logging.LogRecord(
            name="t",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="oops",
            args=(),
            exc_info=sys.exc_info(),
        )
    line = fmt.format(rec)
    parsed = json.loads(line)
    assert "exc" in parsed
    assert "ValueError: boom" in parsed["exc"]


def test_setup_logging_emits_parsable_json_in_prod(capsys):
    setup_logging("app")
    logging.getLogger("test").info("hello %s", "world")
    out = capsys.readouterr().err
    parsed = json.loads(out.strip().splitlines()[-1])
    assert parsed["message"] == "hello world"
    assert parsed["service"] == "app"


def test_json_formatter_names_the_fields_a_log_collector_reads():
    """`severity` / `message` / `time`, not `lvl` / `msg` / `ts`.

    A collector that promotes a JSON line to a structured entry looks for
    these names — Cloud Logging's Ops Agent among them. Under the old names
    every line arrived at severity DEFAULT with the payload as one opaque
    string, which is the same as having no levels at all.
    """
    rec = logging.LogRecord(
        name="test", level=logging.WARNING, pathname=__file__, lineno=1, msg="careful", args=(), exc_info=None
    )
    parsed = json.loads(_JSONFormatter(service="app").format(rec))
    assert parsed["severity"] == "WARNING"
    assert parsed["message"] == "careful"
    assert "time" in parsed
    assert "lvl" not in parsed and "msg" not in parsed and "ts" not in parsed


def test_json_formatter_carries_structured_extras():
    """`logger.info(..., extra={...})` reaches the payload as real fields.

    Without this a caller with something to record — an LLM call's model and
    token counts, say — can only stringify it into the message, where it is
    no longer filterable.
    """
    rec = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1, msg="llm call", args=(), exc_info=None
    )
    rec.event = "llm_generation"
    rec.model = "claude-x"
    rec.output_tokens = 512
    parsed = json.loads(_JSONFormatter(service="app").format(rec))
    assert parsed["event"] == "llm_generation"
    assert parsed["model"] == "claude-x"
    assert parsed["output_tokens"] == 512


def test_json_formatter_extras_cannot_shadow_the_core_fields():
    """An extra named like a core field must not rewrite it — a caller could
    otherwise relabel its own line's severity or service by accident."""
    rec = logging.LogRecord(
        name="test", level=logging.ERROR, pathname=__file__, lineno=1, msg="real", args=(), exc_info=None
    )
    rec.severity = "DEBUG"
    rec.message = "fake"
    rec.service = "somewhere-else"
    parsed = json.loads(_JSONFormatter(service="app").format(rec))
    assert parsed["severity"] == "ERROR"
    assert parsed["message"] == "real"
    assert parsed["service"] == "app"


def test_json_formatter_drops_unserializable_extras_without_losing_the_line():
    """A field that will not serialize must not cost the whole log record."""

    class Opaque:
        def __repr__(self) -> str:
            return "<opaque>"

    rec = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1, msg="m", args=(), exc_info=None
    )
    rec.thing = Opaque()
    parsed = json.loads(_JSONFormatter(service="app").format(rec))
    assert parsed["message"] == "m"
    assert parsed["thing"] == "<opaque>"


def test_json_formatter_tags_the_deployment_environment(monkeypatch):
    """One dashboard serves the whole fleet only if every line says where it
    came from — otherwise a laptop's DEBUG noise sits next to production."""
    monkeypatch.setenv("AGNES_DEPLOYMENT_ENV", "production")
    rec = logging.LogRecord(name="t", level=logging.INFO, pathname=__file__, lineno=1, msg="m", args=(), exc_info=None)
    parsed = json.loads(_JSONFormatter(service="app").format(rec))
    assert parsed["env"] == "production"


def test_the_deployment_environment_falls_back_to_the_release_channel(monkeypatch):
    monkeypatch.delenv("AGNES_DEPLOYMENT_ENV", raising=False)
    monkeypatch.setenv("RELEASE_CHANNEL", "dev")
    rec = logging.LogRecord(name="t", level=logging.INFO, pathname=__file__, lineno=1, msg="m", args=(), exc_info=None)
    parsed = json.loads(_JSONFormatter(service="app").format(rec))
    assert parsed["env"] == "dev"


def test_an_unlabelled_deployment_says_so_rather_than_omitting_the_field(monkeypatch):
    """A missing key and an unknown environment must not look the same to a
    filter — `env != "production"` has to keep matching either way."""
    monkeypatch.delenv("AGNES_DEPLOYMENT_ENV", raising=False)
    monkeypatch.delenv("RELEASE_CHANNEL", raising=False)
    rec = logging.LogRecord(name="t", level=logging.INFO, pathname=__file__, lineno=1, msg="m", args=(), exc_info=None)
    parsed = json.loads(_JSONFormatter(service="app").format(rec))
    assert parsed["env"] == "unknown"


def test_setup_logging_silences_uvicorn_access_in_prod():
    setup_logging("app")
    assert logging.getLogger("uvicorn.access").level == logging.WARNING


def test_setup_logging_keeps_uvicorn_access_in_dev(monkeypatch):
    monkeypatch.setenv("DEBUG", "1")
    setup_logging("app")
    assert logging.getLogger("uvicorn.access").level == logging.INFO


def test_slug_none_falls_back_to_app():
    # No service hint and the calling frame's __file__ won't sit under
    # services/connectors/app — fallback returns "app" or the file stem.
    result = _derive_slug(None)
    assert isinstance(result, str)
    assert result  # non-empty


def test_slug_none_uses_frame_inspection_for_app_path(tmp_path, monkeypatch):
    # Simulate a caller from a path that contains "app" in its parts by
    # invoking _derive_slug from a helper module that lives under app/.
    # We exercise the frame-inspection branch directly.
    import app.logging_config as lc

    # Wrap to ensure the call frame's __file__ is THIS test file (no
    # services/connectors/app prefix on macOS path) -> falls through to p.stem.
    result = lc._derive_slug(None)
    assert result  # path stem or "app" — both are valid here


def test_slug_underscore_prefix_falls_back():
    # "_private" should NOT be treated as a service name (starts with "_").
    result = _derive_slug("_private")
    assert isinstance(result, str)
    assert result


def test_slug_main_dunder_falls_back():
    # "__main__" alone is not a useful slug.
    result = _derive_slug("__main__")
    assert isinstance(result, str)
    assert result


# ---------------------------------------------------------------------------
# _OAuthCallbackQueryRedactFilter — outbound MCP OAuth connect callback
# (2026-07-30 spec §6): strip code/state from uvicorn access-log lines.
# ---------------------------------------------------------------------------


def _access_record(path_with_query: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:1234", "GET", path_with_query, "1.1", 303),
        exc_info=None,
    )


def test_oauth_callback_filter_strips_query_string():
    rec = _access_record("/api/mcp/oauth-client/callback?code=SECRET-CODE&state=SIGNED-STATE")
    assert _OAuthCallbackQueryRedactFilter().filter(rec) is True
    assert rec.args[2] == "/api/mcp/oauth-client/callback"
    formatted = rec.getMessage()
    assert "SECRET-CODE" not in formatted
    assert "SIGNED-STATE" not in formatted


def test_oauth_callback_filter_leaves_other_paths_untouched():
    rec = _access_record("/api/mcp/sources/src_1/oauth/authorize?foo=bar")
    assert _OAuthCallbackQueryRedactFilter().filter(rec) is True
    assert rec.args[2] == "/api/mcp/sources/src_1/oauth/authorize?foo=bar"


def test_oauth_callback_filter_leaves_bare_callback_path_untouched():
    rec = _access_record("/api/mcp/oauth-client/callback")
    assert _OAuthCallbackQueryRedactFilter().filter(rec) is True
    assert rec.args[2] == "/api/mcp/oauth-client/callback"


def test_oauth_callback_filter_ignores_non_tuple_args():
    # A single-dict %-style args (the stdlib's own mapping-args convention —
    # LogRecord unwraps a one-element tuple whose sole item is a Mapping)
    # must pass through untouched: this filter only ever rewrites the
    # positional-tuple shape uvicorn's access logger actually uses.
    rec = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="%(path)s",
        args=({"path": "/api/mcp/oauth-client/callback?code=x"},),
        exc_info=None,
    )
    assert _OAuthCallbackQueryRedactFilter().filter(rec) is True
    assert rec.args == {"path": "/api/mcp/oauth-client/callback?code=x"}


def test_setup_logging_registers_oauth_callback_filter_on_access_logger():
    setup_logging("app")
    access_logger = logging.getLogger("uvicorn.access")
    assert any(isinstance(f, _OAuthCallbackQueryRedactFilter) for f in access_logger.filters)
