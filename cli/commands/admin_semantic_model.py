"""`agnes admin semantic-model` — DEPRECATED alias of `agnes admin semantic`.

Block 6 of #1707 collapsed five semantic-layer command groups into two. This
group's commands now live in one of two places:

  - document CRUD          → ``agnes admin semantic <cmd>``
    (``list``/``show``/``import``/``delete``/``detach``/``reattach``/
    ``link-package``/``unlink-package``)
  - the two that were never admin operations → ``agnes semantic-model <cmd>``
    (``export`` reads the public, resource-gated endpoint; ``validate``
    schema-checks a local file with no server and no token at all)

Kept, hidden, for one release: an old invocation still runs, prints one line
on stderr naming its new path, and delegates to the SAME function the new
path runs — so this file can never drift from what it forwards to. Delete it
once callers have caught up.
"""

from __future__ import annotations

import typer

from cli.commands import admin_semantic, semantic_model
from cli.deprecation import deprecated_alias, deprecated_group_notice

admin_semantic_model_app = typer.Typer(help="(deprecated alias of `agnes admin semantic`)")
admin_semantic_model_app.callback()(
    deprecated_group_notice("admin semantic-model", "admin semantic")
)

# Same name on both sides — only the group moved.
for _name, _fn in (
    ("list", admin_semantic.list_models),
    ("show", admin_semantic.show_model),
    ("import", admin_semantic.import_model),
    ("delete", admin_semantic.delete_model),
    ("detach", admin_semantic.detach_model),
    ("reattach", admin_semantic.reattach_model),
    ("link-package", admin_semantic.link_package),
    ("unlink-package", admin_semantic.unlink_package),
):
    admin_semantic_model_app.command(_name, hidden=True)(_fn)

# These two left the admin tier entirely, so the group-level notice above
# would name the wrong destination — each carries its own.
for _name, _new, _fn in (
    ("export", "semantic-model export", semantic_model.export_model),
    ("validate", "semantic-model validate", semantic_model.validate_document),
):
    deprecated_alias(
        admin_semantic_model_app,
        name=_name,
        old=f"admin semantic-model {_name}",
        new=_new,
        fn=_fn,
    )
