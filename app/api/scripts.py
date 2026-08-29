"""Script management and execution endpoints."""

import logging
import os
import subprocess
import sys
import tempfile
import uuid
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, field_validator
from typing import Optional

import duckdb

from app.auth.access import require_admin
from app.auth.dependencies import _get_db
from src.audit_helpers import log_safe
from src.scheduler import is_valid_schedule, is_table_due

from src.repositories import (
    audit_repo,
    notifications_script_repo,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/scripts", tags=["scripts"])

SCRIPT_TIMEOUT = int(os.environ.get("SCRIPT_TIMEOUT", "300"))  # 5 min default
SCRIPT_MAX_OUTPUT = int(os.environ.get("SCRIPT_MAX_OUTPUT", "65536"))  # 64KB


class DeployScriptRequest(BaseModel):
    name: str
    source: str
    schedule: Optional[str] = None

    @field_validator("schedule", mode="before")
    @classmethod
    def _validate_schedule(cls, v):
        if v in (None, ""):
            return None
        # Pure-whitespace strings ("   ") fall through to is_valid_schedule
        # and reject — same convention as RegisterTableRequest.sync_schedule.
        # We do NOT silently normalise whitespace to None; surfacing the
        # caller's mistake at register time beats persisting an unusable value.
        if not is_valid_schedule(v):
            raise ValueError(f"schedule must be 'every Nm' / 'every Nh' / 'daily HH:MM[,HH:MM,...]', got {v!r}")
        return v


class RunScriptRequest(BaseModel):
    name: Optional[str] = None
    source: Optional[str] = None


class ScriptResponse(BaseModel):
    id: str
    name: str
    schedule: Optional[str]
    owner: Optional[str]


@router.get("")
async def list_scripts(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List deployed scripts. Admin-only."""
    repo = notifications_script_repo()
    scripts = repo.list_all()
    return {"scripts": scripts, "count": len(scripts)}


@router.post("/deploy", status_code=201)
async def deploy_script(
    request: DeployScriptRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Deploy a Python script to be run on the server (optionally on schedule). Admin-only.

    Validates the source against the safety blocklist BEFORE persisting —
    closes the Devin claim-fail-retry loop where a script with blocked
    patterns would land in script_registry, fail every scheduler tick, and
    re-claim itself perpetually.
    """
    _validate_script_source(request.source)
    repo = notifications_script_repo()
    script_id = str(uuid.uuid4())
    repo.deploy(
        id=script_id,
        name=request.name,
        owner=user["id"],
        schedule=request.schedule,
        source=request.source,
    )
    # Content never: the script SOURCE never enters the audit record.
    log_safe(
        user_id=user["id"],
        action="script.deploy",
        resource=f"script:{script_id}",
        params={"name": request.name, "schedule": request.schedule},
    )
    return ScriptResponse(
        id=script_id,
        name=request.name,
        schedule=request.schedule,
        owner=user["id"],
    )


@router.post("/{script_id}/run")
async def run_deployed_script(
    script_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Run a deployed script by ID. Admin-only."""
    repo = notifications_script_repo()
    script = repo.get(script_id)
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    result = _execute_script(script["source"], script["name"])
    log_safe(
        user_id=user["id"],
        action="script.run",
        resource=f"script:{script_id}",
        params={"name": script["name"], "adhoc": False},
        result="success" if result.get("exit_code") == 0 else "error",
    )
    return result


@router.post("/run")
async def run_adhoc_script(
    request: RunScriptRequest,
    user: dict = Depends(require_admin),
):
    """Run an ad-hoc Python script (not deployed). Admin-only."""
    if not request.source:
        raise HTTPException(status_code=400, detail="Script source required")
    name = request.name or "adhoc"
    result = _execute_script(request.source, name)
    # Content never: the script SOURCE never enters the audit record.
    log_safe(
        user_id=user["id"],
        action="script.run",
        resource=f"script:{name}",
        params={"name": name, "adhoc": True},
        result="success" if result.get("exit_code") == 0 else "error",
    )
    return result


@router.delete("/{script_id}", status_code=204)
async def undeploy_script(
    script_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = notifications_script_repo()
    script = repo.get(script_id)
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    repo.undeploy(script_id)
    log_safe(
        user_id=user["id"],
        action="script.delete",
        resource=f"script:{script_id}",
        params={"name": script.get("name")},
    )


@router.post("/run-due")
async def run_due_scripts(
    background_tasks: BackgroundTasks,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Run every deployed script whose ``schedule`` says it is due.

    Iterates ``script_registry``, skips rows without a schedule (those run
    only via explicit POST /{id}/run), evaluates ``is_table_due(schedule,
    last_run)``, and atomically claims each due row via
    ``ScriptRepository.claim_for_run``. Execution is queued as a
    ``BackgroundTask`` so the response returns immediately — the sidecar
    must not block waiting on a long-running script.

    Concurrency: ``claim_for_run`` flips ``last_status`` to ``'running'``
    inside the same UPDATE; a script already in that state is skipped on
    subsequent ticks until the BackgroundTask writes a terminal status via
    ``record_run_result``. There is no max-runtime detection in this PR —
    if a BackgroundTask crashes without writing a terminal status, the
    script stays stuck in ``'running'`` until an operator clears it
    manually (``UPDATE script_registry SET last_status = NULL WHERE id =
    ?``). Documenting this as an accepted v0 limitation; revisit if it
    bites in practice.
    """
    repo = notifications_script_repo()
    claimed: list[str] = []
    for script in repo.list_all():
        schedule = script.get("schedule")
        if not schedule:
            continue
        last_run = script.get("last_run")
        last_run_iso = last_run.isoformat() if last_run else None
        if not is_table_due(schedule, last_run_iso):
            continue
        if not repo.claim_for_run(script["id"]):
            # Lost the race / already running — next tick will retry.
            continue
        claimed.append(script["id"])
        background_tasks.add_task(
            _run_claimed_script,
            script_id=script["id"],
            source=script["source"],
            name=script["name"],
        )
    scripts_run_count = len(claimed)
    try:
        # audit_repo() is factory-routed (honors use_pg()) and opens its own
        # backend connection — no system-DB handle needed here, and opening one
        # on Postgres is forbidden (would create a stale system.duckdb).
        audit_repo().log(
            user_id=user.get("id"),
            action="script_runner.tick",
            params={"scripts_run": scripts_run_count, "scripts_failed": 0},
            result="success",
            client_kind="scheduler",
        )
    except Exception:
        logger.exception("audit_log write failed for script_runner.tick; continuing")
    return {"claimed": claimed, "count": scripts_run_count}


def _run_claimed_script(script_id: str, source: str, name: str) -> None:
    """Execute a previously-claimed script and write the terminal status.

    Runs in a FastAPI BackgroundTask, so it owns its own DB connection
    (the request-scoped conn is already gone by the time this fires).
    ``_execute_script`` only raises on safety-check violations — runtime
    failures (non-zero exit code, ``subprocess.TimeoutExpired`` → exit -1)
    are returned in the result dict, so we must inspect ``exit_code`` to
    decide success vs failure rather than treating "no exception" as
    success. Any exception still writes 'failure' and re-raises so the BG
    handler surfaces the traceback in logs.
    """
    # notifications_script_repo() is factory-routed (honors use_pg()) and opens
    # its own backend connection — no system-DB handle needed here, and opening
    # one on Postgres is forbidden (would create a stale system.duckdb).
    bg_repo = notifications_script_repo()
    try:
        result = _execute_script(source, name)
        status = "success" if result.get("exit_code", 1) == 0 else "failure"
        bg_repo.record_run_result(script_id, status=status)
    except Exception:
        bg_repo.record_run_result(script_id, status="failure")
        raise


def _validate_script_source(source: str) -> None:
    """Reject scripts containing blocked imports / patterns.

    Raises HTTPException(400) on any violation. Called from BOTH the deploy
    endpoint (so bad scripts never land in script_registry — closes the
    Devin claim-fail-retry loop where the scheduler would re-claim and
    re-fail a deployed-but-unrunnable script every tick) and from
    ``_execute_script`` as defense-in-depth.
    """
    blocked_patterns = [
        # Direct imports of dangerous modules
        "import subprocess",
        "from subprocess",
        "import shutil",
        "from shutil",
        "import ctypes",
        "from ctypes",
        "import importlib",
        "from importlib",
        "import socket",
        "from socket",
        "import requests",
        "from requests",
        "import httpx",
        "from httpx",
        "import urllib",
        "from urllib",
        "import http",
        "from http",
        # Dynamic import bypasses
        "__import__",
        "importlib",
        # Code execution bypasses
        "exec(",
        "eval(",
        "compile(",
        # OS-level access
        "import os",
        "from os",
        "import sys",
        "from sys",
        "import signal",
        "from signal",
        # File access bypasses
        "open(",
        "pathlib",
        # Dangerous builtins
        "globals()",
        "locals()",
        "getattr(",
        "setattr(",
        "delattr(",
        "breakpoint(",
        # Introspection-chain dunders that can pivot to RCE.
        # `__init__`/`__getattribute__` deliberately omitted: substring
        # match would flag every `def __init__(self):`.
        "__subclasses__",
        "__globals__",
        "__class__",
        "__base__",
        "__bases__",
        "__mro__",
        "__dict__",
        "__code__",
        "__builtins__",
    ]
    import ast

    BLOCKED_MODULES = {
        "os",
        "sys",
        "subprocess",
        "shutil",
        "ctypes",
        "importlib",
        "socket",
        "requests",
        "httpx",
        "urllib",
        "http",
        "signal",
        "pathlib",
        "builtins",
        # never-legit-in-analytics modules that reach os-level exec/spawn or the
        # browser. Marginal hardening only — the denylist is bypassable by design
        # (e.g. `import duckdb`), so real isolation is the actual fix (see the
        # _execute_script docstring).
        "posix",
        "pty",
        "nt",
        "multiprocessing",
        "webbrowser",
    }
    BLOCKED_FUNCTIONS = {
        "exec",
        "eval",
        "compile",
        "open",
        "globals",
        "locals",
        "getattr",
        "setattr",
        "delattr",
        "breakpoint",
        "__import__",
        "vars",
    }

    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise HTTPException(status_code=400, detail=f"Script syntax error: {e}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in BLOCKED_MODULES:
                    raise HTTPException(status_code=400, detail=f"Blocked import: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in BLOCKED_MODULES:
                raise HTTPException(status_code=400, detail=f"Blocked import: {node.module}")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in BLOCKED_FUNCTIONS:
                raise HTTPException(status_code=400, detail=f"Blocked function: {node.func.id}")

    source_lower = source.lower()
    for pattern in blocked_patterns:
        if pattern.lower() in source_lower:
            raise HTTPException(
                status_code=400,
                detail=f"Script contains disallowed pattern: {pattern.split('(')[0].strip()}",
            )


def _execute_script(source: str, name: str) -> dict:
    """Execute an admin-authored Python script in a subprocess.

    SECURITY: this runs ADMIN-AUTHORED code with the Agnes server process's own
    privileges — it is NOT a security sandbox. ``_validate_script_source`` is a
    bypassable source denylist (defense-in-depth only): ``sys.executable``
    resolves its venv site-packages from the interpreter location regardless of
    the scrubbed env below, so any installed package (e.g. ``duckdb``, which
    alone gives filesystem read/write + network) stays importable, and the child
    runs as the server uid with no container/seccomp/namespace isolation. The
    actual control is that every scripts endpoint is ``require_admin``
    (god-mode) gated. Running deployed scripts inside a real isolation boundary
    (microVM/container, dropped caps, scrubbed uid) is a tracked follow-up.

    Re-runs ``_validate_script_source`` (the deploy endpoint already validates)
    so a registry write that bypassed the deploy contract is still rejected
    before we spawn.
    """
    _validate_script_source(source)
    data_dir = os.environ.get("DATA_DIR", "./data")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(source)
        f.flush()
        script_path = f.name

    try:
        result = subprocess.run(
            [sys.executable, script_path],
            capture_output=True,
            text=True,
            timeout=SCRIPT_TIMEOUT,
            env={
                "PATH": "/usr/bin:/usr/local/bin",
                "DATA_DIR": data_dir,
                "HOME": "/tmp",
                # NOTE: excluding VIRTUAL_ENV / PYTHONPATH does NOT isolate the
                # interpreter — sys.executable locates its site-packages from
                # pyvenv.cfg, not these vars. Kept only to avoid leaking
                # unrelated env into the child; it is not a security boundary.
            },
            cwd="/tmp",  # restrict working directory
        )
        stdout = result.stdout[:SCRIPT_MAX_OUTPUT]
        stderr = result.stderr[:SCRIPT_MAX_OUTPUT]
        return {
            "name": name,
            "exit_code": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": len(result.stdout) > SCRIPT_MAX_OUTPUT or len(result.stderr) > SCRIPT_MAX_OUTPUT,
        }
    except subprocess.TimeoutExpired:
        return {
            "name": name,
            "exit_code": -1,
            "stdout": "",
            "stderr": f"Script timed out after {SCRIPT_TIMEOUT}s",
            "truncated": False,
        }
    finally:
        os.unlink(script_path)
