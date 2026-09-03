"""Pure data shaping for "split one large SharePoint site into N parallel
crawl connections" — the packing algorithm that decides which top-level
folders go in which group, with NO Graph I/O and NO app-state access, so it
can be tested without a mocked Graph client or a database.

The endpoints that call this live in ``app.api.admin_sharepoint``
(``GET …/split-plan`` for a read-only preview, ``POST …/splits`` to actually
create the sibling connections — see that module's docstrings for the full
contract) and reuse the exact same clone + bulk-scope-add primitives
``POST …/clone`` and ``POST …/scopes/bulk`` already use (PR #2095) — this
module is only the piece those two didn't have: deciding the split itself.
"""

from __future__ import annotations

from typing import Any, Dict, List


def pack_folders_into_groups(folders: List[Dict[str, Any]], n: int) -> List[Dict[str, Any]]:
    """Greedy longest-processing-time-first bin packing: sort folders by
    document count descending, then repeatedly place the next folder into
    whichever group currently holds the fewest documents (classic
    multiprocessor-scheduling greedy — not optimal in the worst case, but
    within a small constant factor of it, and simple enough to explain to
    an admin reading the preview).

    A folder with ``documents == 0`` (unreadable count, or a genuinely
    empty folder — the caller never distinguishes the two, see
    ``app.api.admin_sharepoint``) is packed exactly the same way as any
    other folder: it is NEVER dropped, only likely bunched with other small
    folders into whichever group is currently lightest.

    Returns exactly ``n`` groups, each ``{"folders": [...], "documents":
    total}`` — folder dicts are passed through unchanged (whatever keys the
    caller gave them), so this function stays agnostic to whether a folder
    also carries an ``id``, a Graph ``web_url``, or neither. Naming a group
    (``"<source name> — part i/n"``) is the caller's job, not this
    function's — see :func:`format_group_name`.

    ``n`` must be >= 1; folders may be empty (returns ``n`` empty groups).
    """
    if n < 1:
        raise ValueError("n must be >= 1")
    groups: List[Dict[str, Any]] = [{"folders": [], "documents": 0} for _ in range(n)]
    ordered = sorted(folders, key=lambda f: f.get("documents") or 0, reverse=True)
    for folder in ordered:
        target = min(groups, key=lambda g: g["documents"])
        target["folders"].append(folder)
        target["documents"] += folder.get("documents") or 0
    return groups


def format_group_name(source_name: str, index: int, n: int) -> str:
    """The clone name a split group gets — ``"<source name> — part i/n"``,
    1-indexed. Shared between the preview (so the admin sees the exact name
    the apply call will use) and the apply endpoint itself (both the
    creation call and the idempotency check that looks for a prior split
    under these same names)."""
    return f"{source_name} — part {index}/{n}"
