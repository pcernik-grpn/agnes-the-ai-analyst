"""Repository for column metadata."""

from datetime import datetime, timezone
from typing import Any, Optional, List, Dict

import duckdb

from src.repositories._orchestration_mixins import ColumnMetadataImportMixin


class ColumnMetadataRepository(ColumnMetadataImportMixin):
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def save(
        self,
        table_id: str,
        column_name: str,
        basetype: Optional[str] = None,
        description: Optional[str] = None,
        confidence: str = "manual",
        source: str = "manual",
        source_ref: Optional[str] = None,
    ) -> dict:
        """Insert or update column metadata. Returns the saved record.

        ``source_ref`` is accepted for signature parity with the Postgres
        sibling but not persisted here: the DuckDB app-state schema is
        frozen (A3 PG-first ratchet, see ``docs/migrations.md`` -> "Adding a
        PG-only feature") and never gained a ``column_metadata.source_ref``
        column. The capability lands on Postgres only.
        """
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO column_metadata (table_id, column_name, basetype, description, confidence, source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (table_id, column_name) DO UPDATE SET
                basetype = excluded.basetype,
                description = excluded.description,
                confidence = excluded.confidence,
                source = excluded.source,
                updated_at = excluded.updated_at""",
            [table_id, column_name, basetype, description, confidence, source, now],
        )
        return self.get(table_id, column_name)

    def get(self, table_id: str, column_name: str) -> Optional[Dict[str, Any]]:
        """Select by composite PK. Returns None if not found."""
        result = self.conn.execute(
            "SELECT * FROM column_metadata WHERE table_id = ? AND column_name = ?",
            [table_id, column_name],
        ).fetchone()
        if not result:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, result))

    def list_for_table(self, table_id: str) -> List[Dict[str, Any]]:
        """Select all columns for a table, ordered by column_name."""
        results = self.conn.execute(
            "SELECT * FROM column_metadata WHERE table_id = ? ORDER BY column_name",
            [table_id],
        ).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def delete(self, table_id: str, column_name: str) -> bool:
        """Delete column metadata. Returns True if a row was deleted."""
        before = self.conn.execute(
            "SELECT COUNT(*) FROM column_metadata WHERE table_id = ? AND column_name = ?",
            [table_id, column_name],
        ).fetchone()[0]
        if before == 0:
            return False
        self.conn.execute(
            "DELETE FROM column_metadata WHERE table_id = ? AND column_name = ?",
            [table_id, column_name],
        )
        return True

    # import_proposal() lives in ColumnMetadataImportMixin — shared with the
    # PG repo so it can't drift (uses only self.save()).
