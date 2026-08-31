"""Packaging regression tests — guard against silent prod-vs-dev dep drift.

`anthropic` and `openai` are imported (lazily, at first API use) by
`connectors/llm/anthropic_provider.py` and `connectors/llm/openai_compat.py`.
Those modules run in production from `services/corporate_memory` and
`services/verification_detector`. If the SDKs slip back into
`[project.optional-dependencies].dev` the Dockerfile (which only installs
core deps) hits `ModuleNotFoundError` at the first LLM call. See #176.
"""

from __future__ import annotations

from pathlib import Path


def _read_pyproject() -> dict:
    """Load pyproject.toml from the repo root."""
    try:
        import tomllib  # py3.11+
    except ImportError:  # pragma: no cover
        import tomli as tomllib  # type: ignore

    root = Path(__file__).resolve().parent.parent
    with (root / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)


def test_anthropic_is_a_core_dependency():
    """anthropic must live in [project].dependencies, not [dev].

    Production code (connectors/llm/anthropic_provider.py) imports the SDK
    at first API use. Demoting it to dev resurrects the #176 failure as a
    ModuleNotFoundError on the first LLM call.
    """
    cfg = _read_pyproject()
    core = cfg["project"]["dependencies"]
    assert any(dep.startswith("anthropic") for dep in core), "anthropic must be in [project].dependencies — see #176"


def test_openai_is_a_core_dependency():
    """openai must live in [project].dependencies, not [dev]."""
    cfg = _read_pyproject()
    core = cfg["project"]["dependencies"]
    assert any(dep.startswith("openai") for dep in core), "openai must be in [project].dependencies — see #176"


def test_anthropic_not_in_optional_dev_extras():
    """Belt-and-suspenders: dev extras should not double-list anthropic."""
    cfg = _read_pyproject()
    dev = cfg["project"].get("optional-dependencies", {}).get("dev", [])
    assert not any(dep.startswith("anthropic") for dep in dev), (
        "anthropic should not be duplicated in [dev] — keep it core-only"
    )


def test_openai_not_in_optional_dev_extras():
    """Belt-and-suspenders: dev extras should not double-list openai."""
    cfg = _read_pyproject()
    dev = cfg["project"].get("optional-dependencies", {}).get("dev", [])
    assert not any(dep.startswith("openai") for dep in dev), (
        "openai should not be duplicated in [dev] — keep it core-only"
    )


def test_llm_provider_modules_import_cleanly():
    """A fresh interpreter with only core deps installed must import the
    LLM provider modules without ImportError. This is the actual behavior
    that breaks the scheduler container when anthropic/openai are dev-only.
    """
    # Just importing here proves the deps resolve in the active env. The
    # pyproject.toml assertions above keep the contract going forward.
    # The provider modules defer their SDK imports to first API use, so
    # importing them alone no longer exercises the SDKs — import the SDKs
    # explicitly to keep that guarantee.
    import importlib

    for mod in (
        "connectors.llm.anthropic_provider",
        "connectors.llm.openai_compat",
        "connectors.llm.factory",
        "anthropic",
        "openai",
    ):
        importlib.import_module(mod)


def test_jsonschema_is_a_core_dependency():
    """jsonschema must live in [project].dependencies, not [dev].

    app/chat/structured_output.py imports jsonschema unconditionally at
    module scope, and is itself imported unconditionally by
    app/api/agent_runtime.py and app/api/agent_sessions.py, which app/main.py
    imports at module scope. Demoting jsonschema back to dev-only reproduces
    the #176 boot-loop bug class for this dependency.
    """
    cfg = _read_pyproject()
    core = cfg["project"]["dependencies"]
    assert any(dep.startswith("jsonschema") for dep in core), (
        "jsonschema must be in [project].dependencies — production import in app/chat/structured_output.py"
    )


def test_jsonschema_not_in_optional_dev_extras():
    """Belt-and-suspenders: dev extras should not double-list jsonschema."""
    cfg = _read_pyproject()
    dev = cfg["project"].get("optional-dependencies", {}).get("dev", [])
    assert not any(dep.startswith("jsonschema") for dep in dev), (
        "jsonschema should not be duplicated in [dev] — keep it core-only"
    )


def test_structured_output_module_imports_cleanly():
    """A fresh interpreter with only core deps installed must import
    app.chat.structured_output without ImportError — the concrete failure
    mode when jsonschema is dev-only is a ModuleNotFoundError at app boot
    (app/main.py -> app/api/agent_runtime.py -> this module).
    """
    import importlib

    importlib.import_module("app.chat.structured_output")


# ---------------------------------------------------------------------------
# CLI wheel slimming — server-only deps must live in [server], not core.
#
# The CLI wheel is `uv build --wheel` of [project.dependencies] alone
# (Dockerfile installs `.[server,...]` on top for server processes). A dep
# that only `app/`, `services/`, or `connectors/` code needs — a web
# framework, a Postgres driver, a BigQuery/gRPC SDK, the chat runner's agent
# SDK — bloats every analyst's `uv tool install` for no reason and, worse,
# can drag in a transitive pin (see the kbcstorage/urllib3 story above) that
# conflicts with a CLI-only resolver context. Mirrors the #176 lesson this
# module already guards, in the opposite direction: some deps must NOT be
# core.
# ---------------------------------------------------------------------------

# Representative sample of the heavy, server-only deps moved out of core.
# Not exhaustive — the point is to catch a regression where one of these
# creeps back into [project.dependencies], not to duplicate the full
# pyproject.toml dependency list here.
_SERVER_ONLY_SAMPLE = ("claude-agent-sdk", "fastapi", "sqlalchemy", "psycopg", "google-cloud-bigquery")

# The dependencies that MUST remain core even though the import graph looks
# server-heavy at a glance — see the #176 rationale above (anthropic/openai)
# and the corresponding jsonschema tests above.
_CORE_ANCHORS = ("anthropic", "openai", "jsonschema")


# Modules an analyst's CLI-wheel install genuinely does not have: they are
# pulled by `[server]` (or by nothing at all — `requests` is not even declared
# there; it arrives transitively behind the Keboola connector). Blocking them
# in a subprocess reproduces that install exactly, which is the only way to
# catch this class of break from a dev checkout where everything is present.
_ABSENT_ON_A_CLI_ONLY_INSTALL = ("requests", "fastapi", "sqlalchemy", "psycopg")

_CLI_IMPORT_PROBE = """
import sys
from importlib.abc import MetaPathFinder

BLOCKED = {blocked!r}


class _Blocker(MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        root = name.split(".", 1)[0]
        if root in BLOCKED:
            raise ImportError("No module named " + repr(name) + " (absent on a CLI-only install)")
        return None


sys.meta_path.insert(0, _Blocker())
import cli.main  # noqa: F401

# The command tree must also BUILD, not merely import: Typer resolves every
# option's metadata when the Click command is assembled, so a lazy-looking
# import can still fire on first invocation.
import typer.main

typer.main.get_command(cli.main.app)
print("ok")
"""


def test_cli_imports_without_the_server_extra():
    """`agnes` must import with core deps alone — no `[server]`, no `requests`.

    The CLI wheel is `[project.dependencies]` only, so a module-level import
    anywhere under `cli/` that reaches server-side code breaks EVERY command,
    including ones with nothing to do with the feature that added it. This
    fired for real: `cli/commands/admin_semantic.py` imported
    `src.semantic.adapters` at module scope to build one line of `--adapter`
    help, and that module eagerly imported all four connector adapters —
    dragging in `requests` and failing `agnes` at import on an analyst
    install. The fix (`_BUILTIN_ADAPTERS` as data, implementations imported on
    demand) is what keeps the generated help AND a clean import; this guard is
    what stops the next one.

    Run in a subprocess: `cli.main` is already imported by the time most
    tests run, so an in-process block would prove nothing.
    """
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    probe = _CLI_IMPORT_PROBE.format(blocked=set(_ABSENT_ON_A_CLI_ONLY_INSTALL))
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (
        "`agnes` does not import with core dependencies alone — an import under cli/ reaches "
        f"a server-only module.\\n{result.stderr[-2500:]}"
    )


def test_server_only_deps_are_declared_in_server_extra():
    """Each dependency in the server-only sample must appear in
    [project.optional-dependencies].server."""
    cfg = _read_pyproject()
    server = cfg["project"]["optional-dependencies"]["server"]
    for name in _SERVER_ONLY_SAMPLE:
        assert any(dep.split("[")[0].split(">")[0].split("=")[0].split("<")[0].strip() == name for dep in server), (
            f"{name} must be declared in [project.optional-dependencies].server"
        )


def test_server_only_deps_are_absent_from_core():
    """None of the server-only sample may leak back into
    [project].dependencies — that's the wheel-bloat regression this guard
    exists to catch."""
    cfg = _read_pyproject()
    core = cfg["project"]["dependencies"]
    core_names = {dep.split("[")[0].split(">")[0].split("=")[0].split("<")[0].strip() for dep in core}
    for name in _SERVER_ONLY_SAMPLE:
        assert name not in core_names, f"{name} leaked into [project].dependencies — the CLI wheel must not ship it"


def test_core_anchors_remain_core_despite_the_server_split():
    """anthropic/openai/jsonschema look server-heavy but must stay core —
    see the #176 rationale and the dedicated tests above. This guard fails
    loudly if a future re-tier accidentally sweeps them into [server]
    alongside the genuinely server-only deps."""
    cfg = _read_pyproject()
    core = cfg["project"]["dependencies"]
    core_names = {dep.split("[")[0].split(">")[0].split("=")[0].split("<")[0].strip() for dep in core}
    for name in _CORE_ANCHORS:
        assert name in core_names, f"{name} must stay in [project].dependencies"
