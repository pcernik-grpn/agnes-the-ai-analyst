"""Ratchet: no function-scoped fixture may build its own FastAPI app.

``create_app()`` measured ~430 ms — roughly 90 ``include_router`` calls plus
middleware wiring. A *function-scoped* fixture that calls it pays that per
test, which for most API tests is far more than the test itself costs.

The suite already builds the app once per session (``_shared_seeded_app``)
and hands it out via the ``shared_app`` / ``seeded_app`` fixtures, but that
only helps files that actually use them. Before this guard existed, 74 files
and 1229 tests had drifted back to rolling their own: a random 60-file sample
spent 83% of its wall-clock in fixture setup, ~71% of that in per-test
``create_app()``. Converting those files took the same 1455 tests from 104 s
to 59 s while *halving* the worker count.

The drift is silent — a new file that copies the shape of an old one is never
slower in isolation, only in aggregate — so it needs a guard rather than a
convention.

To fix a failure here: request ``shared_app`` (or ``seeded_app`` if you also
want seeded users + role tokens) instead of calling ``create_app()``. If the
fixture genuinely needs a private app instance, use ``seeded_app_fresh`` or
add it to ``ALLOWED`` below WITH a comment saying why.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

TESTS_ROOT = Path(__file__).resolve().parent

# (path relative to tests/, fixture name) -> why a private app is required.
ALLOWED: dict[tuple[str, str], str] = {
    ("conftest.py", "seeded_app_fresh"): (
        "the documented escape hatch: tests that run the ASGI lifespan themselves "
        "need their own StreamableHTTPSessionManager, which may be entered once "
        "per app instance"
    ),
    ("db_pg/conftest.py", "seeded_app_both"): (
        "parametrized over the DuckDB/Postgres state backend; the app must be "
        "built after the backend env is set for that param"
    ),
}


def _function_scoped_fixtures_building_an_app(path: Path):
    """Yield names of function-scoped fixtures in ``path`` that call create_app()."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        is_fixture = False
        scope = "function"
        for decorator in node.decorator_list:
            text = ast.unparse(decorator)
            if "fixture" not in text:
                continue
            is_fixture = True
            for candidate in ("session", "module", "package", "class"):
                if f"'{candidate}'" in text or f'"{candidate}"' in text:
                    scope = candidate
        if not is_fixture or scope != "function":
            continue

        # Skip the docstring: files legitimately *describe* create_app() in prose
        # (tests/test_worker_kinds.py did, and tripped an earlier draft of this).
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        if "create_app()" in "\n".join(ast.unparse(stmt) for stmt in body):
            yield node.name


def test_no_function_scoped_fixture_builds_its_own_app():
    offenders: list[str] = []
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        rel = path.relative_to(TESTS_ROOT).as_posix()
        for name in _function_scoped_fixtures_building_an_app(path):
            if (rel, name) not in ALLOWED:
                offenders.append(f"tests/{rel}::{name}")

    assert not offenders, (
        "These function-scoped fixtures call create_app() per test (~430 ms each):\n"
        + "\n".join(f"  - {item}" for item in offenders)
        + "\n\nRequest the session-shared `shared_app` fixture instead (or "
        "`seeded_app` for seeded users + role tokens). If a private app really "
        "is required, use `seeded_app_fresh` or add an entry to ALLOWED in "
        f"{Path(__file__).name} explaining why."
    )


@pytest.mark.parametrize(("rel", "name"), sorted(ALLOWED))
def test_allowlist_entries_still_exist(rel: str, name: str):
    """An allowlist that outlives its fixture silently permits future drift."""
    path = TESTS_ROOT / rel
    assert path.exists(), f"allowlisted file no longer exists: tests/{rel}"
    assert name in set(_function_scoped_fixtures_building_an_app(path)), (
        f"tests/{rel}::{name} is allowlisted but no longer a function-scoped "
        "fixture calling create_app() — drop the ALLOWED entry."
    )
