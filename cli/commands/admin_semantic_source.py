"""`agnes admin semantic-source` — DEPRECATED alias of `agnes admin semantic source`.

Block 6 of #1707: a semantic source is a *noun under* the semantic layer, not
a sibling group of it. ``add``/``list``/``sync`` keep their names under
``agnes admin semantic source``, which also gained ``rm``.

Kept, hidden, for one release; each command delegates to the same function the
new path runs.
"""

from __future__ import annotations

import typer

from cli.commands import admin_semantic
from cli.deprecation import deprecated_group_notice

admin_semantic_source_app = typer.Typer(help="(deprecated alias of `agnes admin semantic source`)")
admin_semantic_source_app.callback()(
    deprecated_group_notice("admin semantic-source", "admin semantic source")
)

for _name, _fn in (
    ("add", admin_semantic.add_source),
    ("list", admin_semantic.list_sources),
    ("sync", admin_semantic.sync_source),
):
    admin_semantic_source_app.command(_name, hidden=True)(_fn)
