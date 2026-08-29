"""Per-instance deploy freeze — pause the VM's auto-upgrade tick without SSH.

An auto-upgrade once landed in the middle of a customer demo; the mitigation
was "ping the operator an hour ahead", a human lock (TCRD-238). The freeze is
a marker file on the shared state disk — ``{DATA_DIR}/state/upgrade-freeze-until``
holding a UTC epoch — which the host-side ``agnes-auto-upgrade.sh`` tick
consults before pulling (host ``STATE_DIR`` and container ``{DATA_DIR}/state``
are the same disk, the same channel ``instance.yaml`` already rides).

Deliberately a file, not a DB row: the consumer is a bash cron tick on the
host that has no DB access, and the freeze must keep holding even while the
app containers are being recreated — which is exactly when it matters.

Bounded at 72 hours so a typo cannot silently disable upgrades for a month;
the marker's absence or expiry is the resting state.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.auth.access import require_admin
from src.audit_helpers import log_safe

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin/upgrade-freeze", tags=["admin"])

MAX_FREEZE_HOURS = 72


def _marker_path() -> Path:
    return Path(os.environ.get("DATA_DIR", "data")) / "state" / "upgrade-freeze-until"


def _read_until() -> Optional[int]:
    """The marker's epoch, or None when absent/expired/garbage.

    Garbage reads as no-freeze on purpose — same fail-open rule the host
    script applies, so both readers of the file agree on its meaning.
    """
    try:
        raw = _marker_path().read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        logger.exception("upgrade-freeze marker unreadable; treating as no freeze")
        return None
    if not raw.isdigit():
        logger.warning("upgrade-freeze marker holds non-numeric %r; treating as no freeze", raw[:40])
        return None
    until = int(raw)
    return until if until > time.time() else None


class FreezeRequest(BaseModel):
    hours: int = Field(ge=1, le=MAX_FREEZE_HOURS)


class FreezeStatus(BaseModel):
    active: bool
    until: Optional[str] = None
    until_epoch: Optional[int] = None


def _status() -> FreezeStatus:
    until = _read_until()
    if until is None:
        return FreezeStatus(active=False)
    from datetime import datetime, timezone

    return FreezeStatus(
        active=True,
        until=datetime.fromtimestamp(until, tz=timezone.utc).isoformat(),
        until_epoch=until,
    )


@router.get("", response_model=FreezeStatus)
async def get_freeze(user: dict = Depends(require_admin)):
    """Whether an upgrade freeze is currently in force, and until when."""
    return _status()


@router.post("", response_model=FreezeStatus, status_code=201)
async def set_freeze(body: FreezeRequest, user: dict = Depends(require_admin)):
    """Freeze auto-upgrades for ``hours`` from now (1–72).

    Setting a new freeze replaces any existing one — the freeze window is
    absolute, not additive, so repeated clicks don't stack.
    """
    until = int(time.time()) + body.hours * 3600
    marker = _marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{until}\n", encoding="utf-8")
    log_safe(
        user_id=user.get("id"),
        action="upgrade_freeze.set",
        resource="instance",
        params={"hours": body.hours, "until_epoch": until},
        result="success",
        client_kind="web",
    )
    return _status()


@router.delete("", status_code=204)
async def lift_freeze(user: dict = Depends(require_admin)):
    """Lift the freeze immediately (idempotent). 204 per the API design
    rules — the post-delete state is a GET away and always "no freeze"."""
    marker = _marker_path()
    try:
        marker.unlink()
        lifted = True
    except FileNotFoundError:
        lifted = False
    log_safe(
        user_id=user.get("id"),
        action="upgrade_freeze.lift",
        resource="instance",
        params={"was_active": lifted},
        result="success",
        client_kind="web",
    )
