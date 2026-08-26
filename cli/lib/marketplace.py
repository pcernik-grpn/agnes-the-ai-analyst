"""Shared constants for the Claude Code marketplace clone.

`agnes refresh-marketplace --bootstrap` clones the per-user filtered
marketplace repo to `~/.agnes/marketplace`, then registers that path with
Claude Code via `claude plugin marketplace add <path>`. The marketplace is
named "agnes" inside Claude Code's registry.

Every consumer of the clone reaches it through the constants below —
`agnes refresh-marketplace` (bootstrap + incremental pull), `agnes update`
and `agnes global` (staleness reconcile), and `agnes onboard`, which drives
the bootstrap during install. Keeping the path and the registry name here,
rather than re-spelling them per call site, is what stops a refresh from
reconciling a different directory than the one the bootstrap created.

The install prompt (`app/web/setup_instructions.py`) no longer spells the
clone path out: it delegates the whole marketplace stage to `agnes onboard`,
so the CLI is the only writer of this location.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

# Filesystem location of the marketplace clone. Owned end-to-end by
# `cli/commands/refresh_marketplace.py` — it creates the clone under
# `--bootstrap` and every later reconcile (`agnes update`, `agnes global`,
# `agnes onboard`) reads it back through this constant.
CLONE_DIR: Path = Path.home() / ".agnes" / "marketplace"

# The marketplace name as registered in Claude Code (`claude plugin
# marketplace list` shows this). Must match
# `app.marketplace_server.packager.MARKETPLACE_NAME`, which is what the
# server writes into the served `marketplace.json`.
MARKETPLACE_NAME: str = "agnes"


def configured_marketplace_host() -> Optional[str]:
    """The ``host[:port]`` the marketplace SHOULD be served from, or None.

    Resolution order mirrors ``_bootstrap_clone``'s URL derivation:
    ``AGNES_MARKETPLACE_URL`` env override, then ``AGNES_SERVER`` env, then the
    configured ``server`` in ``~/.config/agnes/config.yaml``. Deliberately does
    NOT fall back to a localhost default — callers must only treat the host as
    "known" when it is explicitly configured.

    Vendor-agnostic by construction: the host is always derived from the
    caller's own configuration, never hardcoded.
    """
    base = os.environ.get("AGNES_MARKETPLACE_URL", "").strip()
    if not base:
        # Lazy import keeps this module free of an import-time dependency on
        # cli.config (which pulls in more of the CLI surface).
        from cli.config import load_config

        base = os.environ.get("AGNES_SERVER") or load_config().get("server") or ""
    parsed = urlparse(base)
    if not parsed.scheme or not parsed.hostname:
        return None
    return parsed.netloc.split("@", 1)[-1]


def configured_marketplace_origin() -> Optional[str]:
    """``scheme://host[:port]`` for the configured marketplace, or None.

    Same resolution as ``configured_marketplace_host``, but keeps the scheme —
    git's credential config is keyed by scheme AND host
    (``credential.https://host.helper``), so the host alone can't scope a helper.

    Deriving the scope from configuration rather than from the clone's actual
    ``origin`` is deliberate: if origin has drifted to a different host (the case
    ``_origin_host_mismatch`` exists to detect), that host must NOT receive this
    workspace's PAT.
    """
    base = os.environ.get("AGNES_MARKETPLACE_URL", "").strip()
    if not base:
        from cli.config import load_config

        base = os.environ.get("AGNES_SERVER") or load_config().get("server") or ""
    parsed = urlparse(base)
    if not parsed.scheme or not parsed.hostname:
        return None
    return f"{parsed.scheme}://{parsed.netloc.split('@', 1)[-1]}"
