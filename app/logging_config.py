"""Centralized logging configuration for FastAPI app and background services.

Each entrypoint (app/main.py, services/*/__main__.py or top-level script)
calls setup_logging(__name__) once. Library modules just do
`logger = logging.getLogger(__name__)` — they NEVER call setup_logging.

Dev (DEBUG=1): rich.logging.RichHandler with color, tracebacks, links.
Prod: stdlib StreamHandler with a JSON formatter to stderr — one object
per line, keyed `severity`/`message`/`time` so a log collector can promote it
to a structured entry instead of a string.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)

_CONFIGURED = False


class _RequestIdFilter(logging.Filter):
    """Inject the current request_id ContextVar and this process's replica
    id into every LogRecord (the latter consumed by the dev/rich text
    format string; the JSON formatter reads :func:`_replica_id_safe`
    directly instead — see its own docstring)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get() or "-"
        record.replica = _replica_id_safe()
        return True


def _replica_id_safe() -> str:
    """Best-effort replica id (``hostname:pid``) for cross-replica log
    correlation — spec §3.7 "replica id on every line".

    Lazily imports :data:`app.observability.metrics._REPLICA_ID` (computed
    once at import, hostname/pid never change after process start) so this
    low-level module (imported very early by every entrypoint, before the
    FastAPI app or the observability package may be fully wired up) carries
    no import-time coupling to it, and falls back to ``"-"`` if the import
    ever fails for any reason — a metrics/logging seam must never break
    logging itself. Reuses the cached value rather than calling
    ``replica_id()`` (a fresh ``socket.gethostname()`` + ``os.getpid()`` on
    every invocation) — this runs on every log line.
    """
    try:
        from app.observability.metrics import _REPLICA_ID

        return _REPLICA_ID
    except Exception:
        return "-"


class _OAuthCallbackQueryRedactFilter(logging.Filter):
    """Strip the query string from uvicorn access-log lines for the outbound
    MCP OAuth connect callback (2026-07-30 outbound MCP OAuth sources spec
    §6) — that query string carries ``code`` (a single-use authorization
    code) and ``state`` (opaque but still worth not persisting verbatim in
    logs). No such log-redaction control exists elsewhere in this module
    before this filter; it's new code, not an existing assumption.

    Uvicorn's h11/httptools protocols log the access line as
    ``'%s - "%s %s HTTP/%s" %d'`` with
    ``record.args = (client_addr, method, path_with_query_string,
    http_version, status)`` — this rewrites ``args[2]`` in place, dropping
    everything from ``?`` onward, whenever the path (query stripped) is the
    callback route. Any other shape of ``record.args`` (a different uvicorn
    version, a non-access logger) is left untouched.
    """

    CALLBACK_PATH = "/api/mcp/oauth-client/callback"

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            path = args[2].split("?", 1)[0]
            if path == self.CALLBACK_PATH:
                new_args = list(args)
                new_args[2] = path
                record.args = tuple(new_args)
        return True


def setup_logging(service: str | None = None, level: str | None = None) -> None:
    """Configure root logger. Idempotent.

    Pass ``__name__`` (preferred) or an explicit short slug like ``"app"``.
    Multiple calls are no-ops.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    debug = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")
    lvl = (level or os.environ.get("LOG_LEVEL") or ("DEBUG" if debug else "INFO")).upper()
    slug = _derive_slug(service)

    if debug:
        from rich.console import Console
        from rich.logging import RichHandler

        handler: logging.Handler = RichHandler(
            console=Console(stderr=True, force_terminal=True),
            rich_tracebacks=True,
            tracebacks_show_locals=False,
            show_time=True,
            show_path=True,
            markup=False,
        )
        handler.setFormatter(logging.Formatter("[%(replica)s] [%(request_id)s] [%(name)s] %(message)s"))
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(_JSONFormatter(service=slug))

    handler.addFilter(_RequestIdFilter())
    logging.basicConfig(level=lvl, handlers=[handler], force=True)
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.setLevel(logging.INFO if debug else logging.WARNING)
    access_logger.addFilter(_OAuthCallbackQueryRedactFilter())
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _CONFIGURED = True


def _derive_slug(service: str | None) -> str:
    """Turn module name (``__name__``) or override into readable service slug.

    Examples:
        _derive_slug("app")                                  -> "app"
        _derive_slug("services.scheduler.__main__")          -> "scheduler"
        _derive_slug("services.corporate_memory.collector")  -> "corporate_memory.collector"
        _derive_slug("connectors.jira.transform")            -> "jira.transform"
    """
    if service and not service.startswith("_") and service != "__main__":
        s = service.removeprefix("services.").removeprefix("connectors.").removeprefix("app.")
        s = s.removesuffix(".__main__").removesuffix(".main")
        if s in ("", "main", "__main__"):
            return "app"
        return s

    try:
        frame = sys._getframe(2)
        path = frame.f_globals.get("__file__")
        if path:
            p = Path(path)
            for top in ("services", "connectors", "app"):
                if top in p.parts:
                    i = p.parts.index(top) + 1
                    rest = p.parts[i:]
                    name = ".".join([*rest[:-1], p.stem])
                    return name.removesuffix(".__main__").removesuffix(".main") or top
            return p.stem
    except Exception:
        pass
    return "app"


#: LogRecord attributes the stdlib sets itself, plus the two `_RequestIdFilter`
#: injects — everything else on a record came from a caller's ``extra={...}``
#: and is promoted to a real field.
_RESERVED_RECORD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
        # injected by _RequestIdFilter for the dev/rich format string
        "replica",
        "request_id",
    }
)


def _deployment_env() -> str:
    """Which deployment this line came from, for a fleet-wide log view.

    Read per record rather than cached: the operator scripts rewrite
    ``/opt/agnes/.env`` between container recreates, and a value frozen at
    import would keep labelling a rolled-forward instance with the old one.
    Always a string — a filter must not have to distinguish "unlabelled"
    from "field absent".
    """
    for var in ("AGNES_DEPLOYMENT_ENV", "RELEASE_CHANNEL"):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    return "unknown"


class _JSONFormatter(logging.Formatter):
    """One JSON object per line, named the way a log collector reads them.

    ``severity`` / ``message`` / ``time`` rather than this project's older
    ``lvl`` / ``msg`` / ``ts``: a collector that promotes a JSON line to a
    structured entry looks for the former (Cloud Logging's Ops Agent among
    them), and under the latter every line arrives at its default severity
    with the whole payload as one opaque string — the same as having no
    levels at all.
    """

    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "severity": record.levelname,
            "logger": record.name,
            "service": self.service,
            "env": _deployment_env(),
            "replica": _replica_id_safe(),
            "message": record.getMessage(),
        }
        rid = request_id_var.get()
        if rid:
            payload["request_id"] = rid
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # `extra={...}` lands as attributes on the record; promote them so a
        # caller's numbers stay filterable instead of being stringified into
        # the message. Core fields win — an extra must not be able to relabel
        # its own line's severity or service.
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_ATTRS or key.startswith("_") or key in payload:
                continue
            payload[key] = value
        return json.dumps(payload, default=str)
