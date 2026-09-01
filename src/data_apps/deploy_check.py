"""Deploy-time exposure scan for hosted data apps (warn-first, #1946).

**Static match, not proof.** Like `src/store_guardrails/static_scan.py`,
this is substring/regex pattern matching over the target commit's tree, not
a JS/Python interpreter — it is signal for a human (or an operator's
`data_apps.deploy_checks: block` policy) to look twice, not a security
boundary. An author willing to obfuscate a call, or route a value through a
helper this module can't trace, trivially slips past it; the payoff is
catching the common, unintentional mistake (a static file server rooted at
the project checkout, the whole environment echoed into a response) before
it ships, not detecting a determined adversary.

``check_tree`` is the pure, unit-testable core: a mapping of repo-relative
path -> decoded text in, a :class:`CheckReport` out, never raising.
``check_git_ref`` is the thin git-reading wrapper `app/api/data_apps.py`
calls, via ``src.data_apps.git_repos.read_tree``.

v1 scope, deliberately narrow (revisit once the false-positive rate is
known from real deploys):

- DA005 only ever matches the literal ``AGNES_TOKEN`` — per-app secret
  names are NOT scanned (arbitrary secret values are far noisier to match
  safely). ``secret_names`` is still accepted on both entry points so a v2
  that does scan them is an additive change to the rule body only.
- No persistence of findings, no admin/UI surface, no schema/migration —
  a finding lives for exactly one deploy request's response + audit row.
"""

from __future__ import annotations

import logging
import re
from typing import Callable, Mapping, Optional, Sequence, TypedDict

logger = logging.getLogger(__name__)


class Finding(TypedDict):
    """One deploy-check finding — a rule id, where it fired, and why."""

    rule_id: str  # "DA001".."DA006"
    severity: str  # "warn" | "info"
    message: str
    file: str
    line: int
    snippet: str
    doc_url: str


class CheckReport(TypedDict):
    """Result of scanning one commit's tree."""

    status: str  # "pass" | "warn" | "skipped"
    findings: list[Finding]
    rules_run: list[str]
    files_scanned: int
    skipped: Optional[str]


# Per-file cap — mirrors the idea (and, not by accident, the number) of
# `store_guardrails/static_scan.py`'s `_MAX_FILE_BYTES`. A huge generated
# bundle is never where a hand-written exposure bug lives.
_MAX_FILE_BYTES = 256 * 1024

_SKIP_DIR_NAMES = {"node_modules", "dist", "build"}

_JS_EXTS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
_CODE_EXTS = _JS_EXTS + (".py",)

_ALL_RULE_IDS: tuple[str, ...] = ("DA001", "DA002", "DA003", "DA004", "DA005", "DA006")


def _finding(rule_id: str, severity: str, message: str, *, path: str, lineno: int, line: str) -> Finding:
    return Finding(
        rule_id=rule_id,
        severity=severity,
        message=message,
        file=path,
        line=lineno,
        snippet=line.strip()[:200],
        doc_url=f"/docs/architecture.md#{rule_id.lower()}",
    )


def _ext(path: str) -> str:
    dot = path.rfind(".")
    slash = path.rfind("/")
    return path[dot:].lower() if dot > slash else ""


def _is_skipped_path(path: str) -> bool:
    parts = path.split("/")
    if any(part in _SKIP_DIR_NAMES for part in parts[:-1]):
        return True
    basename = parts[-1]
    return basename == "package-lock.json" or basename.endswith(".min.js")


def _is_nginx_conf(path: str) -> bool:
    return path.startswith("keboola-config/nginx/") and path.endswith(".conf")


# ---------------------------------------------------------------------------
# Shared, paren/bracket-depth-aware text helpers (single-line only — this is
# deliberately not a JS/Python parser, see the module docstring).
# ---------------------------------------------------------------------------


def _scan_value(line: str, start: int) -> str:
    """Text from `start` to the next top-level `,`/closing bracket.

    Tracks `([{`/`)]}` depth so a nested call in the value itself — e.g.
    `os.getcwd()` as a kwarg value, or `path.join(a, b)` as a positional
    arg — doesn't truncate at its OWN inner punctuation.
    """
    depth = 0
    n = len(line)
    i = start
    while i < n:
        ch = line[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif ch == "," and depth == 0:
            break
        i += 1
    return line[start:i]


def _split_top_level_args(text: str) -> list[str]:
    """Split a call's argument-list text on top-level commas."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


# ---------------------------------------------------------------------------
# DA001 — static root exposure (Node)
# ---------------------------------------------------------------------------

_DA001_CALL_RE = re.compile(r"(?:express\.static|serveStatic)\s*\(")
_DA001_FASTIFY_RE = re.compile(r"fastify\.register\([^)]*static", re.IGNORECASE)
_DA001_IDENTIFIER_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_DA001_PATH_CALL_RE = re.compile(r"path\.(?:join|resolve)\s*\(")
_DA001_ANCHORS = {"__dirname", "process.cwd()"}
_DA001_ROOT_MARKERS = {".", "/", "/app", ".."}


def _resolve_same_file_identifier(raw_arg: str, lines: list[str]) -> str:
    """`const X = <expr>;` single-assignment lookup for a bare identifier
    argument — e.g. the scaffold's `express.static(distDir)`. Anything that
    isn't a bare identifier (a call, a member access, a literal) is
    returned unchanged; only ONE same-file assignment is ever consulted, no
    cross-file/import resolution (see module docstring: no interpreter)."""
    if not _DA001_IDENTIFIER_RE.match(raw_arg):
        return raw_arg
    pattern = re.compile(r"^\s*(?:const|let|var)\s+" + re.escape(raw_arg) + r"\s*=\s*(.+?);?\s*$")
    for line in lines:
        m = pattern.match(line)
        if m:
            return m.group(1).strip()
    return raw_arg


def _classify_static_root_arg(expr: str) -> bool:
    """True ("safe") when `expr` resolves to a specifically-named
    subdirectory rather than the project root, an ancestor of it, or the
    bare current/working directory.

    The decisive signal is the LAST path segment of the (possibly
    `path.join`/`path.resolve`-wrapped) expression: ending on a plain name
    scopes the served root to exactly that folder no matter how many `..`
    hops preceded it (that's what makes the scaffold's own
    `path.resolve(__dirname, "..", "..", "dist")` safe — it always serves
    `dist/`, never an ancestor's whole tree); ending on `..`, `.`, `/`,
    `/app`, or being the bare anchor alone serves an entire directory
    verbatim, which is the actual exposure.
    """
    expr = expr.strip()
    if not expr:
        return False

    m = _DA001_PATH_CALL_RE.match(expr)
    if m:
        if not expr.endswith(")"):
            return False  # can't confidently parse a multi-line/malformed call
        tokens = _split_top_level_args(expr[m.end() : -1])
    else:
        tokens = [expr]

    if tokens and tokens[0] in _DA001_ANCHORS:
        tokens = tokens[1:]
    if not tokens:
        return False  # the anchor alone (or nothing left) — serves it whole

    last = tokens[-1].strip("'\"`")
    return last not in _DA001_ROOT_MARKERS


def _check_da001(path: str, lines: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for lineno, line in enumerate(lines, start=1):
        m = _DA001_CALL_RE.search(line)
        if m:
            open_paren = line.index("(", m.start())
            raw_arg = _scan_value(line, open_paren + 1).strip()
            expr = _resolve_same_file_identifier(raw_arg, lines)
            if not _classify_static_root_arg(expr):
                findings.append(
                    _finding(
                        "DA001",
                        "warn",
                        f"Static file server root ({raw_arg!r}) resolves at or above the "
                        "project root — scope it to a specific built-assets subdirectory.",
                        path=path,
                        lineno=lineno,
                        line=line,
                    )
                )
        elif _DA001_FASTIFY_RE.search(line):
            findings.append(
                _finding(
                    "DA001",
                    "warn",
                    "fastify.register(...) registers a static plugin — verify its `root` "
                    "option is a specific built-assets subdirectory, not the project root.",
                    path=path,
                    lineno=lineno,
                    line=line,
                )
            )
    return findings


# ---------------------------------------------------------------------------
# DA002 — static root exposure (Python)
# ---------------------------------------------------------------------------

_DA002_STATIC_FOLDER_RE = re.compile(r"app\.static_folder\s*=\s*(.+?)\s*$")
_DA002_BAD_VALUES = {".", "os.getcwd()", "Path(__file__).parent", "/app", ".."}


def _da002_bad_value(raw: str) -> bool:
    return raw.strip().strip("'\"").rstrip(",").strip() in _DA002_BAD_VALUES


def _check_da002(path: str, lines: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for lineno, line in enumerate(lines, start=1):
        value: Optional[str] = None
        idx = line.find("StaticFiles(")
        if idx != -1:
            kw_idx = line.find("directory=", idx)
            if kw_idx != -1:
                value = _scan_value(line, kw_idx + len("directory="))
        if value is None:
            idx = line.find("send_from_directory(")
            if idx != -1:
                value = _scan_value(line, idx + len("send_from_directory("))
        if value is None:
            m = _DA002_STATIC_FOLDER_RE.search(line)
            if m:
                value = m.group(1)
        if value is not None and _da002_bad_value(value):
            findings.append(
                _finding(
                    "DA002",
                    "warn",
                    f"Static file directory ({value.strip()!r}) resolves to the project root "
                    "or the whole working directory — scope it to a built-assets subdirectory.",
                    path=path,
                    lineno=lineno,
                    line=line,
                )
            )
    return findings


# ---------------------------------------------------------------------------
# DA003 — nginx root/alias
# ---------------------------------------------------------------------------

_DA003_DIRECTIVE_RE = re.compile(r"^\s*(root|alias)\s+(.+?);?\s*$")
_DA003_AUTOINDEX_RE = re.compile(r"^\s*autoindex\s+on\s*;?\s*$")
_DA003_SINGLE_SEGMENT_RE = re.compile(r"^/[^/]+$")


def _da003_bad_target(raw_target: str) -> bool:
    value = raw_target.strip().rstrip(";").strip()
    if value in ("/", "/app"):
        return True
    return bool(_DA003_SINGLE_SEGMENT_RE.match(value))  # "/xxx" — no nested subdirectory


def _check_da003(path: str, lines: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for lineno, line in enumerate(lines, start=1):
        m = _DA003_DIRECTIVE_RE.match(line)
        if m and _da003_bad_target(m.group(2)):
            findings.append(
                _finding(
                    "DA003",
                    "warn",
                    f"nginx `{m.group(1)}` serves {m.group(2).strip()!r} — scope it to a named "
                    "subdirectory of built assets.",
                    path=path,
                    lineno=lineno,
                    line=line,
                )
            )
        elif _DA003_AUTOINDEX_RE.match(line):
            findings.append(
                _finding(
                    "DA003",
                    "warn",
                    "nginx `autoindex on` lists directory contents to any visitor.",
                    path=path,
                    lineno=lineno,
                    line=line,
                )
            )
    return findings


# ---------------------------------------------------------------------------
# DA004 — whole-environment serialization
# ---------------------------------------------------------------------------

_DA004_JS_PATTERNS = (
    re.compile(r"res\.(?:json|send)\(\s*process\.env"),
    re.compile(r"JSON\.stringify\(\s*process\.env"),
    re.compile(r"\{\s*\.\.\.\s*process\.env"),
)
_DA004_PY_PATTERNS = (
    re.compile(r"jsonify\(\s*dict\(\s*os\.environ\s*\)\s*\)"),
    re.compile(r"return\s+dict\(\s*os\.environ\s*\)"),
    re.compile(r"return\s+str\(\s*os\.environ\s*\)"),
)


def _check_da004(path: str, lines: list[str]) -> list[Finding]:
    patterns = _DA004_JS_PATTERNS if _ext(path) in _JS_EXTS else _DA004_PY_PATTERNS
    findings: list[Finding] = []
    for lineno, line in enumerate(lines, start=1):
        if any(rx.search(line) for rx in patterns):
            findings.append(
                _finding(
                    "DA004",
                    "warn",
                    "The whole process environment appears to be serialized into a response "
                    "— every injected secret (AGNES_TOKEN included) would leak.",
                    path=path,
                    lineno=lineno,
                    line=line,
                )
            )
    return findings


# ---------------------------------------------------------------------------
# DA005 — injected-credential echo (v1: AGNES_TOKEN only, see module docstring)
# ---------------------------------------------------------------------------

# JS/Python sinks are kept separate rather than one combined alternation:
# `return {` is Flask's bare-dict-response idiom (`return {...}, 200`) — in
# a JS/TS file `return {...}` is an ordinary function return with nothing to
# do with an HTTP response (Express answers via `res.*`, never `return`), so
# applying it there would flag the scaffold's own `authHeaders()` helper for
# building an OUTBOUND request header out of AGNES_TOKEN.
_DA005_JS_SINK_RE = re.compile(r"res\.(?:json|send|write)\(")
_DA005_PY_SINK_RE = re.compile(r"jsonify\(|return\s*\{")


def _check_da005(path: str, lines: list[str]) -> list[Finding]:
    sink_re = _DA005_JS_SINK_RE if _ext(path) in _JS_EXTS else _DA005_PY_SINK_RE
    findings: list[Finding] = []
    for lineno, line in enumerate(lines, start=1):
        if "AGNES_TOKEN" in line and sink_re.search(line):
            findings.append(
                _finding(
                    "DA005",
                    "warn",
                    "AGNES_TOKEN appears on a line with a response call — verify the injected "
                    "service token is never echoed back to a caller.",
                    path=path,
                    lineno=lineno,
                    line=line,
                )
            )
    return findings


# ---------------------------------------------------------------------------
# DA006 — debug mode (info)
# ---------------------------------------------------------------------------

_DA006_PATTERNS = (
    re.compile(r"app\.run\([^)]*\bdebug\s*=\s*True"),
    re.compile(r"\bFLASK_DEBUG\b"),
    re.compile(r"res\.(?:json|send)\(\s*(?:err|error)\s*\)"),
)


def _check_da006(path: str, lines: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for lineno, line in enumerate(lines, start=1):
        if any(rx.search(line) for rx in _DA006_PATTERNS):
            findings.append(
                _finding(
                    "DA006",
                    "info",
                    "Debug mode or a raw-error response detected — avoid shipping a verbose "
                    "stack trace or the framework debugger to production.",
                    path=path,
                    lineno=lineno,
                    line=line,
                )
            )
    return findings


_RuleChecker = Callable[[str, list[str]], list[Finding]]
_RulePredicate = Callable[[str, str], bool]
_RULE_TABLE: tuple[tuple[str, _RulePredicate, _RuleChecker], ...] = (
    ("DA001", lambda ext, path: ext in _JS_EXTS, _check_da001),
    ("DA002", lambda ext, path: ext == ".py", _check_da002),
    ("DA003", lambda ext, path: _is_nginx_conf(path), _check_da003),
    ("DA004", lambda ext, path: ext in _CODE_EXTS, _check_da004),
    ("DA005", lambda ext, path: ext in _CODE_EXTS, _check_da005),
    ("DA006", lambda ext, path: ext in _CODE_EXTS, _check_da006),
)


def skipped_report(reason: str) -> CheckReport:
    """A `CheckReport` for "there was nothing to scan" — an external repo
    (source never reaches Agnes) today; a future unscannable-tree case
    later. Exported so `app/api/data_apps.py` builds the exact same shape
    for its own skip branches rather than hand-rolling the dict."""
    return CheckReport(status="skipped", findings=[], rules_run=[], files_scanned=0, skipped=reason)


def check_tree(files: Mapping[str, str], *, secret_names: Sequence[str] = ()) -> CheckReport:
    """Scan already-read file contents. Pure, never raises — an unexpected
    failure degrades to a `skipped` report rather than 500ing the deploy
    request it's called from.

    `secret_names` is accepted (v2 additivity, see module docstring) and
    currently unused by every rule.
    """
    del secret_names  # v1: DA005 only ever matches the literal AGNES_TOKEN
    try:
        findings: list[Finding] = []
        files_scanned = 0
        for path in sorted(files):
            content = files[path]
            if _is_skipped_path(path) or len(content.encode("utf-8", errors="ignore")) > _MAX_FILE_BYTES:
                continue
            files_scanned += 1
            ext = _ext(path)
            lines = content.splitlines()
            for rule_id, predicate, checker in _RULE_TABLE:
                if not predicate(ext, path):
                    continue
                try:
                    findings.extend(checker(path, lines))
                except Exception:
                    logger.warning("deploy_check rule %s failed on %s", rule_id, path, exc_info=True)
        return CheckReport(
            status="warn" if findings else "pass",
            findings=findings,
            rules_run=list(_ALL_RULE_IDS),
            files_scanned=files_scanned,
            skipped=None,
        )
    except Exception:
        logger.warning("deploy_check.check_tree failed", exc_info=True)
        return skipped_report("internal_error")


def check_git_ref(slug: str, ref: str, *, secret_names: Sequence[str] = ()) -> CheckReport:
    """`check_tree` over the tree at `ref` in app `slug`'s bare repo."""
    from src.data_apps.git_repos import read_tree

    try:
        files = read_tree(slug, ref, max_bytes=_MAX_FILE_BYTES)
    except Exception:
        logger.warning("deploy_check.check_git_ref: read_tree failed for %s@%s", slug, ref, exc_info=True)
        return skipped_report("read_error")
    return check_tree(files, secret_names=secret_names)
