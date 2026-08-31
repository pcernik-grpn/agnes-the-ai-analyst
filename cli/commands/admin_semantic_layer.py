"""`agnes admin semantic-layer` — DEPRECATED alias of `agnes admin semantic`.

Block 6 of #1707. The group held exactly one command, ``coverage``, and its
name was the problem: two OTHER reports also called themselves coverage
(``agnes admin semantic coverage`` and ``… coverage tables``), while this one
answered a different question from either — it predicts, live against a
connected Keboola project's Metastore, how much of that project's semantic
layer *would* import. It is now ``agnes admin semantic keboola-import``,
which says so.

Kept, hidden, for one release; it delegates to the same function the new path
runs.
"""

from __future__ import annotations

import typer

from cli.commands import admin_semantic
from cli.deprecation import deprecated_group_notice

admin_semantic_layer_app = typer.Typer(help="(deprecated alias of `agnes admin semantic keboola-import`)")
admin_semantic_layer_app.callback()(
    deprecated_group_notice(
        "admin semantic-layer",
        "admin semantic",
        # The command was renamed as well as moved.
        overrides={"coverage": "admin semantic keboola-import"},
    )
)

admin_semantic_layer_app.command("coverage", hidden=True)(admin_semantic.keboola_import)
