#!/usr/bin/env python3
"""Deterministic verifier for the ``CONTRIBUTING.md`` sync-map rows CI does not guard.

Why this exists
---------------
The sync-map lists surfaces that must change together. Most rows already have a
mechanical guard in ``tests/`` — those are NOT reimplemented here:

===========================================  =========================================
sync-map row                                 existing guard
===========================================  =========================================
repo method parity (``X.py`` ↔ ``X_pg.py``)  ``tests/db_pg/test_repo_method_parity.py``
factory dispatch entry                       ``tests/test_repository_registry.py``
raw ``get_system_db()`` / direct repo ctor   ``tests/test_backend_split_guard.py``
Alembic ↔ ``src/db.py`` migration ladder     ``tests/test_db_schema_version.py``
``/api/*`` route has *some* auth             ``tests/test_route_auth_guard.py``
REST × CLI × MCP coverage                    ``tests/test_documentation_api_triple_surface.py``
MCP foundation-tool registration             ``tests/test_mcp_tool_parity.py``
===========================================  =========================================

What is left are the rows marked "CI guard? NO". They are enforced today by an
LLM reviewer at the end of the work — expensive, non-deterministic and merely
advisory. Every check below is mechanically decidable from the diff, so it
belongs in the edit loop instead:

* ``ResourceType`` member without a ``ResourceTypeSpec`` in ``RESOURCE_TYPES``
* user-visible change without a ``## [Unreleased]`` CHANGELOG bullet
* a NEW boolean scope flag in a CLI command (command-UX standard)
* ``query_mode='remote'`` in a connector without a ``_remote_attach`` row
* (WARN) a new entity-scoped endpoint carrying authn but no authz dependency

Usage::

    scripts/verify_syncmap.py                  # vs merge-base with origin/main
    scripts/verify_syncmap.py --base HEAD      # only uncommitted work
    scripts/verify_syncmap.py --json

Exit codes: ``0`` clean (WARN findings still print), ``1`` at least one BLOCKING
finding, ``2`` the verifier itself could not run (not a git repo, bad base ref).

Stdlib only, on purpose — it runs before/independently of the venv.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

BLOCKING = "BLOCKING"
WARN = "WARN"

REPO_ROOT = Path(__file__).resolve().parents[1]

RESOURCE_TYPES_PATH = "app/resource_types.py"
CHANGELOG_PATH = "CHANGELOG.md"

# Trees whose change is user-visible for CHANGELOG purposes. `tests/`, `docs/`,
# `scripts/`, `.claude/` and any Markdown are deliberately outside.
_VISIBLE_PREFIXES = (
    "app/",
    "cli/",
    "src/",
    "connectors/",
    "services/",
    "config/",
    "migrations/",
)

# Boolean scope flags. `--remote` / `--local` are FROZEN aliases on the commands
# that already carry them; a new command must use `--scope auto|local|server`.
_BANNED_SCOPE_FLAGS = {"--remote", "--local", "--server", "--server-side", "--local-only"}

# The third marker is a reviewed-and-confirmed comment for endpoints whose
# authorization deliberately lives in the repository layer (e.g. the facts
# read surface, spec §5: the resource is not nameable in the route template,
# so every read method filters by the caller inside the repo). Writing the
# marker IS the durable confirmation the WARN otherwise asks for on every PR.
_AUTHZ_MARKERS = ("require_admin", "require_resource_access", "authz: repository-enforced")

_ROUTE_DECORATOR_RE = re.compile(r"@\w+\.(get|post|put|patch|delete)\s*\(")
_VERSION_RE = re.compile(r'^version\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


@dataclass(frozen=True)
class Finding:
    """One sync-map violation.

    ``file:line`` is where the change landed; ``mirror`` is where the matching
    update is missing — the two-citation shape the review team already uses.
    """

    file: str
    line: int
    severity: str
    rule: str
    message: str
    mirror: str

    def render(self) -> str:
        return f"{self.file}:{self.line}: [{self.severity}] {self.rule}\n    {self.message}\n    mirror: {self.mirror}"


# ---------------------------------------------------------------------------
# diff plumbing
# ---------------------------------------------------------------------------


def parse_added_lines(diff_text: str) -> dict[str, list[tuple[int, str]]]:
    """Map ``path -> [(new_line_number, added_text), …]`` from a unified diff.

    Only ``+`` lines are returned, keyed by their line number in the NEW file so
    findings point at something the reader can open. Deleted files (``+++
    /dev/null``) never become keys.
    """
    added: dict[str, list[tuple[int, str]]] = {}
    path: str | None = None
    lineno = 0

    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            path = None if target == "/dev/null" else target.removeprefix("b/")
            continue
        if raw.startswith("@@"):
            # @@ -a,b +c,d @@
            match = re.search(r"\+(\d+)", raw)
            lineno = int(match.group(1)) if match else 0
            continue
        if path is None:
            continue
        if raw.startswith("+"):
            added.setdefault(path, []).append((lineno, raw[1:]))
            lineno += 1
        elif raw.startswith("-"):
            continue
        elif raw.startswith(" "):
            lineno += 1

    return added


# ---------------------------------------------------------------------------
# check: ResourceType ↔ ResourceTypeSpec
# ---------------------------------------------------------------------------


def check_resource_type_registry(source: str) -> list[Finding]:
    """Every ``ResourceType`` member must be a key in ``RESOURCE_TYPES``.

    Full sweep rather than diff-scoped: the module is small, the invariant is
    absolute, and a missing spec is a latent 500 on ``/admin/access`` whenever
    the type is granted.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    members: dict[str, int] = {}
    registered: set[str] = set()
    registry_line = 0

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ResourceType":
            for stmt in node.body:
                # `NAME = "value"` only — skip docstrings, methods, annotations.
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                    target = stmt.targets[0]
                    if isinstance(target, ast.Name) and not target.id.startswith("_"):
                        members[target.id] = stmt.lineno

        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {t.id for t in targets if isinstance(t, ast.Name)}
            if "RESOURCE_TYPES" not in names or not isinstance(node.value, ast.Dict):
                continue
            registry_line = node.lineno
            for key in node.value.keys:
                if (
                    isinstance(key, ast.Attribute)
                    and isinstance(key.value, ast.Name)
                    and key.value.id == "ResourceType"
                ):
                    registered.add(key.attr)

    return [
        Finding(
            file=RESOURCE_TYPES_PATH,
            line=line,
            severity=BLOCKING,
            rule="New ResourceType enum value → ResourceTypeSpec in RESOURCE_TYPES",
            message=(
                f"ResourceType.{name} is not registered — /admin/access cannot "
                f"project it and grants for it are unreachable."
            ),
            mirror=f"{RESOURCE_TYPES_PATH}:{registry_line} (RESOURCE_TYPES)",
        )
        for name, line in sorted(members.items(), key=lambda kv: kv[1])
        if name not in registered
    ]


# ---------------------------------------------------------------------------
# check: CHANGELOG [Unreleased] bullet
# ---------------------------------------------------------------------------


def unreleased_bullets(changelog: str) -> list[str]:
    """Normalised bullet lines under ``## [Unreleased]``.

    Normalisation (collapse whitespace after the marker) means re-indenting or
    rewrapping an existing bullet does not read as a new one.
    """
    bullets: list[str] = []
    inside = False

    for raw in changelog.splitlines():
        stripped = raw.strip()
        if stripped.startswith("## "):
            inside = stripped.startswith("## [Unreleased]")
            continue
        if not inside or not stripped:
            continue
        if stripped[0] not in "-*":
            continue
        text = stripped[1:].strip()
        if not text or set(stripped) <= {"-", "*", " "}:
            continue  # a `---` rule, not a bullet
        bullets.append("- " + " ".join(text.split()))

    return bullets


def _is_user_visible(path: str) -> bool:
    if path.endswith(".md"):
        return False
    if path.startswith("tests/"):
        return False
    return path.startswith(_VISIBLE_PREFIXES)


def check_changelog(
    *,
    base_changelog: str,
    head_changelog: str,
    changed_paths: list[str],
    version_bumped: bool,
) -> list[Finding]:
    """A user-visible change must add a ``## [Unreleased]`` bullet.

    Skipped on a release-cut (version bump), where ``[Unreleased]`` legitimately
    empties out as its content moves under the new version heading.
    """
    if version_bumped:
        return []
    if not any(_is_user_visible(p) for p in changed_paths):
        return []

    before = unreleased_bullets(base_changelog)
    after = unreleased_bullets(head_changelog)
    if [b for b in after if b not in before]:
        return []

    touched = sorted(p for p in changed_paths if _is_user_visible(p))
    return [
        Finding(
            file=CHANGELOG_PATH,
            line=1,
            severity=BLOCKING,
            rule="User-visible behavior change → `## [Unreleased]` bullet",
            message=(
                f"{len(touched)} user-visible file(s) changed (e.g. {touched[0]}) "
                f"but no new bullet appeared under [Unreleased]. Add one under "
                f"Added/Changed/Fixed/Removed/Internal — same PR, no follow-ups."
            ),
            mirror=f"{CHANGELOG_PATH} → ## [Unreleased]",
        )
    ]


# ---------------------------------------------------------------------------
# check: command-UX — no NEW boolean scope flag
# ---------------------------------------------------------------------------


def check_scope_flags(added: dict[str, list[tuple[int, str]]], sources: dict[str, str]) -> list[Finding]:
    """A new read/find command must express scope as ``--scope``, not a boolean.

    AST-based rather than line-based: this repo overwhelmingly wraps
    ``typer.Option`` across several lines (``cli/commands/search.py:27``,
    ``cli/commands/snapshot.py:199``), so the ``bool`` annotation and the flag
    literal sit on *different* physical lines. Matching both on one line let the
    dominant style walk straight through a BLOCKING check.

    Diff-scoped by the parameter's line span, so the frozen aliases already in
    the tree are not re-reported when an unrelated line in the file changes.
    """
    findings: list[Finding] = []

    for path, lines in added.items():
        if not path.startswith("cli/commands/") or not path.endswith(".py"):
            continue
        source = sources.get(path)
        if not source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue

        added_linenos = {lineno for lineno, _ in lines}

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for arg, default in _params_with_defaults(node):
                if not _mentions_bool(arg.annotation):
                    continue
                banned = sorted(_option_flag_literals(default) & _BANNED_SCOPE_FLAGS)
                if not banned:
                    continue
                span = range(arg.lineno, (getattr(default, "end_lineno", None) or arg.lineno) + 1)
                if not (set(span) & added_linenos):
                    continue
                flag = banned[0]
                findings.append(
                    Finding(
                        file=path,
                        line=arg.lineno,
                        severity=BLOCKING,
                        rule="New CLI read/find command → command-UX standard",
                        message=(
                            f"`{flag}` is a boolean scope flag. The standard is a "
                            f"single `--scope auto|local|server` with default "
                            f"auto/everywhere and a labeled result origin; "
                            f"`--remote`/`--local` are frozen aliases on existing "
                            f"commands only."
                        ),
                        mirror=(".claude/skills/agnes-conventions/references/command-ux.md"),
                    )
                )

    return findings


def _params_with_defaults(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[tuple[ast.arg, ast.expr]]:
    """Pair each parameter that has a default with that default expression.

    Positional defaults align to the TAIL of the argument list; keyword-only
    defaults align one-to-one and may be ``None`` (no default).
    """
    pairs: list[tuple[ast.arg, ast.expr]] = []

    positional = node.args.posonlyargs + node.args.args
    defaults = node.args.defaults
    if defaults:
        for arg, default in zip(positional[-len(defaults) :], defaults):
            pairs.append((arg, default))

    for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
        if default is not None:
            pairs.append((arg, default))

    return pairs


def _mentions_bool(annotation: ast.expr | None) -> bool:
    """True for ``bool``, ``bool | None``, ``Optional[bool]`` — anything whose
    annotation subtree names ``bool``."""
    if annotation is None:
        return False
    return any(isinstance(sub, ast.Name) and sub.id == "bool" for sub in ast.walk(annotation))


def _option_flag_literals(default: ast.expr) -> set[str]:
    """Flag strings passed to a ``typer.Option(...)`` / ``Option(...)`` call."""
    if not isinstance(default, ast.Call):
        return set()
    func = default.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if name not in ("Option", "Argument"):
        return set()
    return {
        arg.value
        for arg in default.args
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith("--")
    }


# ---------------------------------------------------------------------------
# check: query_mode='remote' ⇒ _remote_attach
# ---------------------------------------------------------------------------


def check_remote_attach(added: dict[str, list[tuple[int, str]]], *, repo_root: Path = REPO_ROOT) -> list[Finding]:
    """A connector gaining a ``remote`` table must publish ``_remote_attach``.

    Without that row the orchestrator cannot re-ATTACH the external DuckDB
    extension at query time, so every view over the source fails to resolve.
    """
    findings: list[Finding] = []
    seen: set[str] = set()

    for path, lines in added.items():
        parts = path.split("/")
        if len(parts) < 3 or parts[0] != "connectors":
            continue
        connector = parts[1]
        if connector in seen:
            continue

        hit = next(
            (
                (lineno, text)
                for lineno, text in lines
                if "query_mode" in text and ("'remote'" in text or '"remote"' in text)
            ),
            None,
        )
        if hit is None:
            continue

        connector_dir = repo_root / "connectors" / connector
        if _dir_mentions(connector_dir, "_remote_attach"):
            continue

        seen.add(connector)
        lineno, _ = hit
        findings.append(
            Finding(
                file=path,
                line=lineno,
                severity=BLOCKING,
                rule="`query_mode='remote'` table → `_remote_attach` row in extract.duckdb",
                message=(
                    f"connectors/{connector}/ declares a remote table but nothing "
                    f"in the connector mentions `_remote_attach` (alias, extension, "
                    f"url, token_env). The orchestrator cannot re-ATTACH the source "
                    f"at query time, so its views will not resolve."
                ),
                mirror=f"connectors/{connector}/ (extract_init or extractor)",
            )
        )

    return findings


def _dir_mentions(directory: Path, needle: str) -> bool:
    if not directory.is_dir():
        return False
    for candidate in directory.rglob("*"):
        if not candidate.is_file() or candidate.suffix in {".pyc", ".parquet", ".duckdb"}:
            continue
        try:
            if needle in candidate.read_text(encoding="utf-8", errors="ignore"):
                return True
        except OSError:
            continue
    return False


# ---------------------------------------------------------------------------
# check (WARN): entity-scoped endpoint carrying authn but no authz
# ---------------------------------------------------------------------------


def check_entity_scoped_authz(added: dict[str, list[tuple[int, str]]], sources: dict[str, str]) -> list[Finding]:
    """Complements ``tests/test_route_auth_guard.py``.

    That guard proves a route has *some* auth dependency; it cannot judge
    whether an entity-scoped route authorizes the specific entity. WARN, not
    BLOCKING — authorizing inside the handler body is a legitimate shape, and
    only a human/reviewer can tell "checked elsewhere" from "not checked".
    """
    findings: list[Finding] = []

    for path, lines in added.items():
        if not path.startswith("app/api/") or not path.endswith(".py"):
            continue
        source = sources.get(path)
        if not source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue

        added_linenos = {lineno for lineno, _ in lines}

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if decorator.lineno not in added_linenos:
                    continue
                route = _route_path(decorator)
                if route is None or "{" not in route:
                    continue
                segment = ast.get_source_segment(source, node) or ""
                if any(marker in segment for marker in _AUTHZ_MARKERS):
                    continue
                findings.append(
                    Finding(
                        file=path,
                        line=decorator.lineno,
                        severity=WARN,
                        rule="New entity-scoped endpoint → require_admin / require_resource_access",
                        message=(
                            f"`{route}` is entity-scoped but `{node.name}` carries no "
                            f"require_admin / require_resource_access. If the entity is "
                            f"authorized elsewhere this is fine — confirm it."
                        ),
                        mirror="app/auth/access.py",
                    )
                )

    return findings


def _route_path(decorator: ast.expr) -> str | None:
    if not isinstance(decorator, ast.Call):
        return None
    func = decorator.func
    if not isinstance(func, ast.Attribute) or func.attr not in {
        "get",
        "post",
        "put",
        "patch",
        "delete",
    }:
        return None
    if not decorator.args:
        return None
    first = decorator.args[0]
    return first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else None


# ---------------------------------------------------------------------------
# git glue
# ---------------------------------------------------------------------------


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _git_or_empty(*args: str) -> str:
    try:
        return _git(*args)
    except subprocess.CalledProcessError:
        return ""


def _resolve_base(explicit: str | None) -> str:
    if explicit:
        _git("rev-parse", "--verify", explicit)  # raises → exit 2 upstream
        return explicit
    merge_base = _git_or_empty("merge-base", "origin/main", "HEAD").strip()
    return merge_base or "HEAD"


def _untracked_files() -> list[str]:
    out = _git_or_empty("ls-files", "--others", "--exclude-standard")
    return [line for line in out.splitlines() if line.strip()]


def _added_from_untracked(paths: list[str]) -> dict[str, list[tuple[int, str]]]:
    """An untracked file is 100% added lines — the diff never mentions it."""
    added: dict[str, list[tuple[int, str]]] = {}
    for path in paths:
        candidate = REPO_ROOT / path
        if not candidate.is_file() or candidate.suffix not in {".py", ".md"}:
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        added[path] = list(enumerate(text.splitlines(), start=1))
    return added


def _version_bumped(base: str) -> bool:
    before = _VERSION_RE.search(_git_or_empty("show", f"{base}:pyproject.toml"))
    head_file = REPO_ROOT / "pyproject.toml"
    after = _VERSION_RE.search(head_file.read_text(encoding="utf-8")) if head_file.is_file() else None
    if before is None or after is None:
        return False
    return before.group(1) != after.group(1)


def collect_findings(base: str) -> list[Finding]:
    """Run every check against the working tree as of ``base``."""
    changed = [p for p in _git_or_empty("diff", "--name-only", base).splitlines() if p.strip()]
    untracked = _untracked_files()
    changed_paths = sorted(set(changed) | set(untracked))

    added = parse_added_lines(_git_or_empty("diff", "-U0", base))
    added.update(_added_from_untracked(untracked))

    sources: dict[str, str] = {}
    for path in added:
        candidate = REPO_ROOT / path
        if candidate.is_file() and candidate.suffix == ".py":
            sources[path] = candidate.read_text(encoding="utf-8", errors="ignore")

    resource_types_file = REPO_ROOT / RESOURCE_TYPES_PATH
    changelog_file = REPO_ROOT / CHANGELOG_PATH

    findings: list[Finding] = []
    if resource_types_file.is_file():
        findings += check_resource_type_registry(resource_types_file.read_text(encoding="utf-8"))
    if changelog_file.is_file():
        findings += check_changelog(
            base_changelog=_git_or_empty("show", f"{base}:{CHANGELOG_PATH}"),
            head_changelog=changelog_file.read_text(encoding="utf-8"),
            changed_paths=changed_paths,
            version_bumped=_version_bumped(base),
        )
    findings += check_scope_flags(added, sources)
    findings += check_remote_attach(added)
    findings += check_entity_scoped_authz(added, sources)

    order = {BLOCKING: 0, WARN: 1}
    return sorted(findings, key=lambda f: (order[f.severity], f.file, f.line))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the CONTRIBUTING.md sync-map rows CI does not guard.")
    parser.add_argument(
        "--base",
        default=None,
        help="Base ref (default: merge-base with origin/main). `--base HEAD` checks only uncommitted work.",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    args = parser.parse_args(argv)

    try:
        base = _resolve_base(args.base)
        findings = collect_findings(base)
    except subprocess.CalledProcessError as exc:
        print(f"verify-syncmap: git failed: {exc.stderr or exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"verify-syncmap: {exc}", file=sys.stderr)
        return 2

    blocking = [f for f in findings if f.severity == BLOCKING]

    if args.json:
        print(json.dumps({"base": base, "findings": [asdict(f) for f in findings]}, indent=2))
        return 1 if blocking else 0

    if not findings:
        print(f"verify-syncmap: clean (base {base})")
        return 0

    for finding in findings:
        print(finding.render())
        print()

    warns = len(findings) - len(blocking)
    print(f"verify-syncmap: {len(blocking)} blocking, {warns} warning (base {base})")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
