"""SQLAlchemy model behind "I know about this check, and it is deliberate".

PG-ONLY (A3 PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend discipline"):
this table landed after the DuckDB app-state backend was frozen, so there is no
``src/db.py`` ladder step and no DuckDB repository sibling. See
``docs/migrations.md`` -> "Adding a PG-only feature (post-A3)".

Its own module rather than a row in ``src/models/semantic.py``, for the same
reason ``semantic_feedback.py`` is: the tables there (``semantic_models``,
``semantic_sources``) are pre-A3 and still have a DuckDB half, and mixing a
PG-only table into that file would make the next author guess which half of it
the freeze applies to.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional, Tuple

from sqlalchemy import DateTime, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base

#: The three things a mute can name, as the admin types them. A scope is a
#: string rather than two nullable columns on purpose: health checks are
#: identified by a scope string end to end (report, API, CLI, MCP), and two
#: columns would need a third to say which combination was meant.
MUTE_SCOPE_FORMS: Tuple[str, ...] = (
    "source:<source_id>",
    "domain:<domain>",
    "source:<source_id>:domain:<domain>",
)

#: Long enough for a UUID source id plus the longest domain name, short enough
#: that a pasted paragraph is refused rather than indexed.
MAX_SCOPE_LENGTH = 200

#: Each half of a scope. Deliberately excludes ``:`` (the separator, so a
#: source id containing one could never round-trip) and whitespace (a scope
#: that differs from the report's own key only by a space silences nothing).
_SCOPE_PART = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")


def parse_mute_scope(scope: str) -> Tuple[Optional[str], Optional[str]]:
    """Split a scope into ``(source_id, domain)``; raise ``ValueError`` if it
    names neither.

    Validation lives here, next to the column, because a malformed scope is the
    one failure mode this feature cannot tolerate quietly: the row would sit in
    the muted list looking like a silenced check while the check it meant to
    silence carries on firing. Better a ``400`` at the moment of muting.

    Note what is NOT validated: whether ``domain`` names a check that exists.
    The coverage report scores six domains today and F4.2's health checks will
    add their own names; a closed vocabulary pinned here would refuse the next
    wave's check names, so the surfaces offer the known domains as a picker
    (see the Mute tab and ``agnes semantic-model mute --help``) and the grammar
    only guarantees the SHAPE.
    """
    raw = (scope or "").strip()
    if not raw:
        raise ValueError(f"scope is required — one of {', '.join(MUTE_SCOPE_FORMS)}")
    if len(raw) > MAX_SCOPE_LENGTH:
        raise ValueError(f"scope is longer than {MAX_SCOPE_LENGTH} characters")

    parts = raw.split(":")
    if len(parts) == 2 and parts[0] == "source":
        source_id, domain = parts[1], None
    elif len(parts) == 2 and parts[0] == "domain":
        source_id, domain = None, parts[1]
    elif len(parts) == 4 and parts[0] == "source" and parts[2] == "domain":
        source_id, domain = parts[1], parts[3]
    else:
        raise ValueError(f"{raw!r} is not a mute scope — expected one of {', '.join(MUTE_SCOPE_FORMS)}")

    for value in (source_id, domain):
        if value is not None and not _SCOPE_PART.match(value):
            raise ValueError(f"{raw!r} is not a mute scope — expected one of {', '.join(MUTE_SCOPE_FORMS)}")
    return source_id, domain


class SemanticHealthMute(Base):
    """One admin's standing "yes, I know" about a semantic-layer check.

    Turning a check off is a legitimate act — an admin who has read the finding,
    decided it is expected, and does not want it shouting on every page load is
    not doing anything wrong. Doing it ANONYMOUSLY is: a check that simply stops
    appearing leaves the next reader unable to tell "fixed" from "hidden".

    Hence the shape. ``muted_by`` and ``muted_at`` are not audit decoration —
    they are the columns that make this a signature, and every surface that
    reads a mute reads them with it. ``reason`` is nullable because forcing a
    sentence produces "n/a", not insight; the forms ask for one anyway.

    ``expires_at`` NULL means "until somebody unmutes it": a gap tracked in next
    quarter's work should not have to be re-affirmed weekly to stay quiet. A
    dated mute is the other honest option — say when you expect to have fixed
    it, and let the check come back on its own if you have not.
    """

    __tablename__ = "semantic_health_mutes"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # `source:<id>` | `domain:<domain>` | `source:<id>:domain:<domain>`.
    # Parsed by `parse_mute_scope` above; stored verbatim, so the string the
    # admin muted is the string the health roll-up matches on.
    scope: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    muted_by: Mapped[str] = mapped_column(String, nullable=False)
    muted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    # NULL = permanent until unmuted.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # The hot read is "is anything muting this check", once per health
        # roll-up and once per page view of the Mute tab.
        Index("idx_semantic_health_mutes_scope", "scope"),
    )
