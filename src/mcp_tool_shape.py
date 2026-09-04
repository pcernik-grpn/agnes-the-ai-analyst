"""What an MCP tool's NAME and SCHEMA say about it, in one place.

An upstream MCP server is free to publish no annotations at all, so two
questions about a tool get answered from its name and its input schema rather
than from anything it declared:

  * **Is it write-shaped?** A leading verb like ``create_`` / ``delete_`` /
    ``deploy_``. A guess, and named as one — it is used to REFUSE a nomination
    (a lister that would be invoked automatically), never to grant a tool extra
    trust. ``readOnlyHint`` deliberately plays no part: it is a tri-state, most
    servers send nothing, and registration records that as ``mutating=True``,
    so trusting it would refuse every tool on exactly the servers these
    features exist for.

  * **Can it answer a no-argument call?** A schema with ``required`` entries
    cannot, and that is a fact rather than a guess.

The definition lives here because three callers need the same answer and no two
of them may drift: ``app/api/admin_mcp.py`` (refusing a bad lister
designation, #2154), ``scripts/repair_mcp_materialize_debris.py`` (finding the
rows the old heuristic left behind, #2251), and
``app/web/static/js/components/linked_apps_panel.js``, which cannot import
Python and carries the same list as ``WRITE_VERB`` — the client ranks with it
and the server refuses with it, so a UI that nominated what the endpoint then
rejected would be the drift this module exists to prevent.
``tests/test_mcp_tool_shape.py`` pins the two lists to each other.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

#: Verbs that make a tool name write-shaped. Kept character-for-character in
#: step with ``WRITE_VERB`` in
#: ``app/web/static/js/components/linked_apps_panel.js``.
WRITE_VERB = re.compile(
    r"^(create|delete|remove|drop|deploy|modify|update|patch|set|add|put|post|write|"
    r"rename|move|copy|start|stop|restart|enable|disable|install|uninstall|run|"
    r"execute|trigger|publish|unpublish|share|revoke|grant|import|upload)(_|$)"
)


def is_write_shaped(name: str) -> bool:
    """Does ``name`` lead with a verb that changes things upstream?

    A judgment about a string, not a fact about the tool: a server may well
    publish a read-only ``run_report``. Use it to refuse an automatic
    invocation, never to permit one.
    """
    return bool(WRITE_VERB.match(str(name or "").lower()))


def required_arguments(input_schema: Optional[Dict[str, Any]]) -> List[str]:
    """The arguments ``input_schema`` demands, or ``[]``.

    Tolerant on purpose — the schema is upstream JSON that reached us through a
    registry column, so a non-dict schema or a ``required`` that is not a list
    of strings is treated as "demands nothing" rather than raised on.
    """
    if not isinstance(input_schema, dict):
        return []
    required = input_schema.get("required")
    if not isinstance(required, list):
        return []
    return [str(r) for r in required if isinstance(r, (str, int, float))]


def tool_name(tool: Optional[Dict[str, Any]]) -> str:
    """The upstream name of a ``tool_registry`` row, falling back to the
    exposed one. ``original_name`` is what the server was asked for, so it is
    what a name-shape question is about."""
    if not tool:
        return ""
    return str(tool.get("original_name") or tool.get("exposed_name") or "")
