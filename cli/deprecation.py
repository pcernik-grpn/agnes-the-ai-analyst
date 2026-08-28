"""Keeping a renamed CLI path alive for one release.

A rename that simply deletes the old spelling breaks every script, alias and
piece of muscle memory that used it, and the error the user gets ("No such
command") does not say what to type instead. A rename that keeps the old
spelling *visible* teaches two names for one thing forever.

So a renamed path becomes a HIDDEN alias: it still runs, `--help` stops
advertising it, and every invocation says once — on **stderr** — what the new
path is. stderr matters: the notice must never land in the middle of a
`--json` payload someone is piping into `jq`.

Two shapes, because Typer offers two:

* :func:`deprecated_group_notice` — a whole group moved (`agnes admin
  semantic-model …` → `agnes admin semantic …`). Attach it as the alias
  group's callback; Click runs a group callback before the subcommand, so one
  line covers every command under it.
* :func:`deprecated_alias` — a single command moved, or moved *and* renamed,
  inside a group that itself is not deprecated (`agnes semantic-model health`
  → `agnes admin semantic health`).

Both reuse the ORIGINAL command function, so an alias can never drift from
what it delegates to.
"""

from __future__ import annotations

import functools
from typing import Any, Callable

import typer


def deprecation_notice(old: str, new: str) -> None:
    """Print the one line that names the replacement, on stderr."""
    typer.echo(
        f"Deprecated: `agnes {old}` is now `agnes {new}`. "
        "The old path still works this release and will be removed in a later one.",
        err=True,
    )


def deprecated_group_notice(old: str, new: str) -> Callable[[], None]:
    """Build a Typer group callback that announces the group's new home.

    Register it with ``@alias_app.callback()``; it fires once per invocation,
    before whichever subcommand was asked for.
    """

    def _notice() -> None:
        deprecation_notice(old, new)

    _notice.__doc__ = f"(deprecated alias of `agnes {new}`)"
    return _notice


def deprecated_alias(
    alias_app: typer.Typer,
    *,
    name: str,
    old: str,
    new: str,
    fn: Callable[..., Any],
) -> None:
    """Register ``fn`` on ``alias_app`` as a hidden, deprecated command ``name``.

    The wrapper takes ``*args, **kwargs`` but carries ``fn``'s signature
    through :func:`functools.wraps` — Typer builds the click parameters from
    ``inspect.signature``, which follows ``__wrapped__``, so the alias accepts
    exactly the arguments the real command does with no duplicated declaration
    to keep in sync.
    """

    @functools.wraps(fn)
    def _aliased(*args: Any, **kwargs: Any) -> Any:
        deprecation_notice(old, new)
        return fn(*args, **kwargs)

    alias_app.command(name, hidden=True)(_aliased)
