"""Structural lint for the Structurizr DSL model under ``docs/c4/``.

This is NOT the Structurizr compiler — it does not check DSL grammar, and a
file that passes here can still fail to render. What it does check is the one
failure mode a hand-edited multi-file model actually accumulates, and the one
the platform C4 approach doc names as the reason for using a DSL at all:
**dangling references** — a relationship or a view naming an element nobody
defined. Those are invisible in a diff and only surface when someone tries to
open the diagrams.

It also resolves every ``!include`` (so a typo'd path fails here rather than at
render time), checks brace balance, and flags a tag filtered in a view that no
element carries — usually a rename that missed one side.

``docs/c4/`` is the source of truth for the five ``agnes-c4-*.svg`` figures;
``scripts/dev/render_c4.sh`` turns it into them through the real Structurizr
parser and PlantUML. That script is the gate that matters, but it needs a
container runtime. This lint covers the same failure class in milliseconds, so
CI and the pre-commit loop catch a broken model without pulling two images.

Usage:  python3 scripts/validate_c4.py [docs/c4/workspace.dsl]
"""

from __future__ import annotations

import pathlib
import re
import sys

DEF_RE = re.compile(r'^\s*([A-Za-z][\w-]*)\s*=\s*(person|softwareSystem|container|component)\s+"')
REL_RE = re.compile(r'^\s*([A-Za-z][\w-]*)\s*->\s*([A-Za-z][\w-]*)\s*(?:"|$)')
VIEW_RE = re.compile(r'^\s*(systemContext|container|component|dynamic)\s+([A-Za-z][\w-]*)\s+"')
INCLUDE_EL_RE = re.compile(r"^\s*include\s+([A-Za-z][\w-]*)\s*$")
TAGS_RE = re.compile(r"^\s*tags\s+(.+)$")
TAGFILTER_RE = re.compile(r"element\.tag==([\w-]+)")
INCLUDE_RE = re.compile(r"^\s*!include\s+(\S+)\s*$")

# Identifiers the DSL itself defines; never declared in our files.
BUILTIN = {"this"}


def expand(path: pathlib.Path, seen: list[tuple[pathlib.Path, int, str]]) -> None:
    """Depth-first !include expansion, recording (file, lineno, text) per line."""
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        m = INCLUDE_RE.match(line)
        if m:
            target = (path.parent / m.group(1)).resolve()
            if not target.is_file():
                print(f"  {path}:{n}: !include target does not exist: {m.group(1)}")
                sys.exit(1)
            expand(target, seen)
        else:
            seen.append((path, n, line))


def main(argv: list[str]) -> int:
    root = pathlib.Path(argv[1] if len(argv) > 1 else "docs/c4/workspace.dsl").resolve()
    if not root.is_file():
        print(f"error: {root} not found", file=sys.stderr)
        return 2

    lines: list[tuple[pathlib.Path, int, str]] = []
    expand(root, lines)

    defined: dict[str, tuple[pathlib.Path, int]] = {}
    tags: set[str] = set()
    for f, n, line in lines:
        m = DEF_RE.match(line)
        if m:
            name = m.group(1)
            if name in defined:
                prev_f, prev_n = defined[name]
                print(f"  {f}:{n}: '{name}' redefined (first at {prev_f}:{prev_n})")
                return 1
            defined[name] = (f, n)
        m = TAGS_RE.match(line)
        if m:
            tags.update(re.findall(r'"([^"]+)"', m.group(1)))

    known = set(defined) | BUILTIN
    problems: list[str] = []

    for f, n, line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        m = REL_RE.match(line)
        if m:
            for ident in m.groups():
                if ident not in known:
                    problems.append(f"  {f}:{n}: relationship names undefined element '{ident}'")
        m = VIEW_RE.match(line)
        if m and m.group(2) not in known:
            problems.append(f"  {f}:{n}: view is scoped to undefined element '{m.group(2)}'")
        m = INCLUDE_EL_RE.match(line)
        if m and m.group(1) not in known:
            problems.append(f"  {f}:{n}: view includes undefined element '{m.group(1)}'")
        for tag in TAGFILTER_RE.findall(line):
            if tag not in tags:
                problems.append(f"  {f}:{n}: view filters on tag '{tag}' that no element carries")

    depth = 0
    for f, n, line in lines:
        code = re.sub(r'"[^"]*"', "", re.sub(r"#.*$", "", line))
        depth += code.count("{") - code.count("}")
        if depth < 0:
            problems.append(f"  {f}:{n}: unbalanced closing brace")
            break
    if depth > 0:
        problems.append(f"  {depth} unclosed brace(s) at end of model")

    if problems:
        print(f"c4: {len(problems)} problem(s)\n" + "\n".join(problems))
        return 1

    kinds: dict[str, int] = {}
    for f, n, line in lines:
        m = DEF_RE.match(line)
        if m:
            kinds[m.group(2)] = kinds.get(m.group(2), 0) + 1
    rels = sum(1 for _, _, line in lines if REL_RE.match(line) and not line.strip().startswith("#"))
    summary = ", ".join(f"{v} {k}" for k, v in sorted(kinds.items()))
    print(f"c4: clean — {summary}; {rels} relationships; {len(tags)} tags")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
