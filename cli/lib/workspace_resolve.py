"""Anchored workspace resolution for data commands (spec §5.1).

Order: ``AGNES_LOCAL_DIR`` env (explicit override, always wins even when
the target is not workspace-shaped — the sandbox/runner contract) →
cwd, if workspace-shaped (preserves pre-existing behaviour for anyone
standing inside a workspace) → ``workspace_root`` config, if
workspace-shaped (the global fallback; a stale anchor degrades to None,
never to reads against a bogus path) → ``None``.

Deliberately DIFFERENT from ``cli/commands/update.py::_resolve_workspace``
(env → anchor → cwd-if-initialised): convergence must target the anchor
even when run from inside some other initialised folder, while data reads
prefer the workspace you are standing in. Pinned by
``tests/test_workspace_resolve.py::test_precedence_differs_from_update_resolver``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from cli.config import get_workspace_root


def is_workspace_shaped(p: Path) -> bool:
    """True when ``p`` looks like an Agnes workspace: the init sentinel,
    a local analytics DuckDB, or a parquet tree."""
    try:
        return (
            (p / ".claude" / "init-complete").exists()
            or (p / "user" / "duckdb" / "analytics.duckdb").exists()
            or (p / "server" / "parquet").is_dir()
        )
    except OSError:
        return False


def workspace_anchor() -> Optional[Path]:
    """The configured ``workspace_root`` anchor, resolved (raw, not
    shape-checked).

    Distinct from the anchor branch inside ``resolve_data_workspace()``
    (which additionally requires the anchor to still look workspace-shaped
    before trusting it for reads). This helper exists purely for
    DIVERGENCE LABELING (issue #1312) — comparing "what directory a command
    actually used" against "what `workspace_root` says my workspace is" —
    not for deciding whether to read from it. `agnes pull` and `agnes
    status` each need exactly this comparison; sharing one read here keeps
    a third caller from re-deriving it ad hoc (the drift #1312 flags).
    """
    root = get_workspace_root()
    if not root:
        return None
    return Path(root).resolve()


def resolve_data_workspace() -> Optional[Path]:
    # `if env_dir:` — an EMPTY `AGNES_LOCAL_DIR` falls through to the chain
    # below rather than meaning cwd, which the old inline
    # `Path(os.environ.get("AGNES_LOCAL_DIR", ".")).resolve()` did via
    # `Path("")`. Deliberate: an exported-but-empty override is a caller bug,
    # and silently reading it as "this directory" is how a data command ends
    # up pointed at a random repository. Audited against every producer in the
    # tree — nothing sets this variable outside tests, and every test sets a
    # non-empty value — so no runner reaches the changed branch
    # (Devin on #1184).
    env_dir = os.environ.get("AGNES_LOCAL_DIR")
    if env_dir:
        return Path(env_dir).resolve()
    cwd = Path.cwd()
    if is_workspace_shaped(cwd):
        return cwd.resolve()
    root = get_workspace_root()
    if root:
        anchor = Path(root)
        if is_workspace_shaped(anchor):
            return anchor.resolve()
    return None
