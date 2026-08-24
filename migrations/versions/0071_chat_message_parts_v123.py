"""chat_messages.parts — the assistant turn's ordered shape

Mirrors DuckDB ``_v122_to_v123``.

An assistant turn is prose → tool call → prose explaining what came back, but
the row recorded ``content`` (one flattened string) plus ``tool_calls`` (a
positionless list). The interleaving therefore existed only in the live frame
order and was gone by the time anything read the row back: a reloaded
conversation showed every tool block appended under the whole answer, and a
replayed tool card could show only a name — no outcome, no result — because
the row evidenced neither (#1504).

``parts`` stores the sequence instead: ``[{type:'text'|'tool', …}]``, where a
tool entry carries its own ``state``/``result``/``is_error``. Shape and
rationale live in ``app/chat/message_parts.py``; it mirrors what
``apps/kai-agent`` persists for the same reason.

JSONB rather than JSON: the whole array is read and written as one value, and
JSONB is what every other structured column on this backend uses.

Additive and nullable, with NO backfill — the ordering a historical row lost
is not recoverable from ``content`` + ``tool_calls`` (the positions are simply
not in the data), and guessing would place tool cards where they never ran.
Pre-v123 rows keep rendering via the ``tool_calls`` fallback the client
retains.
"""

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0071_chat_message_parts_v123"
down_revision: Union[str, None] = "0070_builder_scope_v122"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_messages",
        sa.Column("parts", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_messages", "parts")
