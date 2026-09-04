"""Postgres-only repository for ``extraction_conditions`` — fleet-level
provider-refusal conditions the built-in facts-extraction stage cannot
retry its way past (TCRD-296 synthesis F.25, gaps #25/#48).

PG-first ratchet (A3): brand-new app-state surface added after the freeze,
so there is no DuckDB sibling — see ``docs/migrations.md`` -> "Adding a
PG-only feature". Reach this repo only through
``src.repositories.extraction_conditions_repo()``; on a DuckDB-backed
instance that factory call raises ``RequiresPostgresBackend``. Every caller
in ``connectors/sharepoint/facts_extraction.py`` wraps it in a broad
``except Exception`` and treats the failure as "no condition tracked here"
— a broken read/write on this table must never itself fail a pass or block
an enqueue, only an actually-persisted condition should. See that module's
"Provider-limit classification" section for the write side.

Every write here is best-effort observability with a real operational
consequence attached (it suppresses a streamed re-enqueue), never the
pass's own correctness: a pass that hit a provider refusal has already
decided its own ``interrupted_reason`` before this repo is even touched.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Engine

_ACTIVE = "cleared_at IS NULL"
_ORDER = "ORDER BY last_seen DESC, id DESC"


class ExtractionConditionsPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def record(
        self,
        *,
        reason: str,
        provider: str,
        model: Optional[str],
        region: Optional[str],
        message: str,
        retry_after_s: Optional[int],
    ) -> Dict[str, Any]:
        """Record one provider-limit hit — refreshes the matching ACTIVE
        row (same ``provider``/``model``/``region``/``reason``) when one
        already exists, otherwise inserts a new one. Returns the stored row.

        No DB-level uniqueness enforced: writes here are rare (only on a
        provider refusal) and effectively single-writer in practice (one
        extraction worker at a time per connection), so a benign duplicate
        row from a genuine race is an acceptable outcome rather than one
        worth a partial-unique-index's complexity — both rows would report
        the same condition, and either clears the same way.
        """
        model_key = model or ""
        region_key = region or ""
        with self._engine.begin() as conn:
            existing = (
                conn.execute(
                    sa.text(
                        "SELECT id FROM extraction_conditions WHERE provider = :provider AND model = :model "
                        f"AND region = :region AND reason = :reason AND {_ACTIVE}"
                    ),
                    {"provider": provider, "model": model_key, "region": region_key, "reason": reason},
                )
                .mappings()
                .first()
            )
            if existing:
                condition_id = str(existing["id"])
                conn.execute(
                    sa.text(
                        "UPDATE extraction_conditions SET message = :message, retry_after_s = :retry_after_s, "
                        "last_seen = current_timestamp WHERE id = :id"
                    ),
                    {"id": condition_id, "message": message, "retry_after_s": retry_after_s},
                )
            else:
                condition_id = f"ecnd_{uuid4().hex[:16]}"
                conn.execute(
                    sa.text(
                        "INSERT INTO extraction_conditions "
                        "(id, kind, reason, provider, model, region, message, retry_after_s, first_seen, last_seen) "
                        "VALUES (:id, 'provider_limit', :reason, :provider, :model, :region, :message, "
                        ":retry_after_s, current_timestamp, current_timestamp)"
                    ),
                    {
                        "id": condition_id,
                        "reason": reason,
                        "provider": provider,
                        "model": model_key,
                        "region": region_key,
                        "message": message,
                        "retry_after_s": retry_after_s,
                    },
                )
        row = self.get(condition_id)
        assert row is not None  # just written in the same engine
        return row

    def get(self, condition_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(sa.text("SELECT * FROM extraction_conditions WHERE id = :id"), {"id": condition_id})
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def list_active(self) -> List[Dict[str, Any]]:
        """Every condition still active — the fleet banner's read, and what
        ``streamed_pass_suppressed_by_provider_limit`` filters by cooldown."""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(sa.text(f"SELECT * FROM extraction_conditions WHERE {_ACTIVE} {_ORDER}")).mappings().all()
            )
        return [dict(r) for r in rows]

    def clear_for_provider(self, provider: str) -> int:
        """Mark every active condition for ``provider`` cleared — called
        once a pass for that provider completes without hitting one.
        Returns how many rows were cleared (0 is the common case: nothing
        was active)."""
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text(
                    f"UPDATE extraction_conditions SET cleared_at = current_timestamp WHERE provider = :provider AND {_ACTIVE}"
                ),
                {"provider": provider},
            )
        return int(result.rowcount or 0)

    def clear(self, condition_id: str) -> bool:
        """Manually clear one condition by id — the admin "dismiss" action,
        if a surface ever needs one beyond the automatic clear-on-success
        path. ``False`` when nothing matched (already cleared, or unknown
        id)."""
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text(
                    f"UPDATE extraction_conditions SET cleared_at = current_timestamp WHERE id = :id AND {_ACTIVE}"
                ),
                {"id": condition_id},
            )
        return bool(result.rowcount)
