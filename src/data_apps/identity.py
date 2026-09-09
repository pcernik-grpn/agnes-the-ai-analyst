"""Per-app *data identity* — whose grants a hosted app reads Agnes data with.

``owner`` (the default, and the only behaviour before this module existed):
the container's ``AGNES_TOKEN`` is the app owner's service PAT, so every
viewer sees whatever the owner may see — "sharing an app is an act of
publication" (``docs/superpowers/specs/2026-07-21-data-apps-design.md`` §8).

``viewer``: the ingress proxy additionally hands the container a short-lived,
server-signed viewer token on every request (``X-Agnes-Viewer-Token``); data
read with it is authorized as ``owner ∩ viewer`` and row-level access
policies bind to the viewer — see ``app/auth/data_app_viewer.py``.

The value lives in the Postgres-only ``data_apps.data_identity`` column (A3
PG-first ratchet: Alembic revision ``0113_data_apps_data_identity``, no
``src/db.py`` step). A DuckDB-backed row simply lacks the key, and
:func:`data_identity_of` reads that as ``owner`` — the ONE place that default
is spelled out, shared by the API serializer, the container-spec builder and
the proxy so they can never disagree about what an absent column means.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

DATA_IDENTITY_OWNER = "owner"
DATA_IDENTITY_VIEWER = "viewer"

#: Every value ``data_apps.data_identity`` may hold.
DATA_IDENTITIES: frozenset[str] = frozenset({DATA_IDENTITY_OWNER, DATA_IDENTITY_VIEWER})


def data_identity_of(row: Optional[Mapping[str, Any]]) -> str:
    """``'owner'`` | ``'viewer'`` for a ``data_apps`` row dict.

    Fail-closed to the status quo: an absent key (DuckDB backend, or a row
    dict built before the column existed), ``None``, or any value other than
    the literal ``'viewer'`` reads as ``'owner'``.
    """
    value = row.get("data_identity") if row else None
    return DATA_IDENTITY_VIEWER if value == DATA_IDENTITY_VIEWER else DATA_IDENTITY_OWNER


def viewer_mode(row: Optional[Mapping[str, Any]]) -> bool:
    """Whether this app reads Agnes data as its viewer (``data_identity='viewer'``)."""
    return data_identity_of(row) == DATA_IDENTITY_VIEWER
