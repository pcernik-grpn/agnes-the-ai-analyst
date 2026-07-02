# tests/test_v2_scan_estimate.py
import asyncio
import importlib
from unittest.mock import MagicMock
import pytest
from fastapi import HTTPException


@pytest.fixture
def reload_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db_module
    importlib.reload(db_module)
    yield db_module


def _seed(conn):
    _ensure_admin1(conn)
    from src.repositories.table_registry import TableRegistryRepository
    TableRegistryRepository(conn).register(
        id="bq_view", name="bq_view", source_type="bigquery",
        bucket="ds", source_table="bq_view", query_mode="remote",
    )


def _ensure_admin1(conn):
    """Seed an admin user with id='admin1' + Admin group membership so
    {"id": "admin1", ...} dicts pass the can_access admin shortcut."""
    from src.db import SYSTEM_ADMIN_GROUP
    from src.repositories.users import UserRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    if UserRepository(conn).get_by_id('admin1') is None:
        UserRepository(conn).create(id='admin1', email='admin1@test.com', name='Admin')
    admin_gid = conn.execute(
        'SELECT id FROM user_groups WHERE name = ?', [SYSTEM_ADMIN_GROUP]
    ).fetchone()
    if admin_gid:
        UserGroupMembersRepository(conn).add_member(
            'admin1', admin_gid[0], source='system_seed',
        )


def _bq(billing="billing-proj", data="data-proj"):
    """Build a BqAccess wired to default factories. For tests that monkeypatch
    `_bq_dry_run_bytes` whole, the inner factories are never called."""
    from connectors.bigquery.access import BqAccess, BqProjects
    return BqAccess(BqProjects(billing=billing, data=data))


class TestScanEstimate:
    def test_returns_scan_bytes_for_bq(self, reload_db, monkeypatch):
        from app.api import v2_scan
        monkeypatch.setattr(
            v2_scan, "_bq_dry_run_bytes",
            lambda bq, sql, **kw: 4_400_000_000,
        )
        # Stub the schema fetch the validator uses
        monkeypatch.setattr(
            v2_scan, "_resolve_schema",
            lambda *a, **kw: {"event_date": "DATE", "country_code": "STRING"},
        )

        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            req = {
                "table_id": "bq_view",
                "select": ["event_date", "country_code"],
                "where": "event_date > DATE '2026-01-01'",
                "limit": 1000000,
            }
            data = v2_scan.estimate(conn, user, req, bq=_bq(data="proj"))
        finally:
            conn.close()
        assert data["estimated_scan_bytes"] == 4_400_000_000
        assert "estimated_result_rows" in data
        assert "bq_cost_estimate_usd" in data


class TestBqAccessErrors:
    """Issue #134: structured 502/400 translation on BQ errors in dry-run path.

    These tests exercise the REAL translation path through `BqAccess` +
    `translate_bq_error` by injecting a MagicMock BQ client via the
    `bq_access` fixture. That's the production path — Phase 1 monkeypatches
    of `_bq_dry_run_bytes` whole would skip the translation logic and only
    test the outer wrap (which has been removed in Phase 2)."""

    def test_scan_estimate_returns_502_on_bq_forbidden_serviceusage(self, reload_db, bq_access):
        """When the BQ client raises Forbidden mentioning serviceusage, the
        endpoint must translate to HTTP 502 with `cross_project_forbidden`
        and a hint that mentions billing_project."""
        from app.api import v2_scan
        from google.api_core.exceptions import Forbidden

        mock_client = MagicMock()
        mock_client.query.side_effect = Forbidden(
            "Permission denied: serviceusage.services.use on project foo"
        )
        bq = bq_access(client=mock_client, billing="billing-proj", data="data-proj")

        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            req = {
                "table_id": "bq_view",
                "select": ["event_date", "country_code"],
                "where": "event_date > DATE '2026-01-01'",
                "limit": 1000,
            }
            # Stub schema since build_schema queries BQ INFORMATION_SCHEMA
            from unittest.mock import patch
            with patch.object(
                v2_scan, "_resolve_schema",
                lambda *a, **kw: {"event_date": "DATE", "country_code": "STRING"},
            ):
                with pytest.raises(HTTPException) as exc_info:
                    (
                        v2_scan.scan_estimate_endpoint(raw=req, user=user, conn=conn, bq=bq)
                    )
        finally:
            conn.close()

        assert exc_info.value.status_code == 502
        detail = exc_info.value.detail
        assert isinstance(detail, dict)
        assert detail["error"] == "cross_project_forbidden"
        assert "hint" in detail["details"]
        assert "billing_project" in detail["details"]["hint"].lower()

    def test_scan_estimate_returns_502_on_bq_forbidden_non_serviceusage(self, reload_db, bq_access):
        """A Forbidden that is NOT about serviceusage (e.g. dataset-level ACL)
        still becomes a 502, but with `bq_forbidden`."""
        from app.api import v2_scan
        from google.api_core.exceptions import Forbidden

        mock_client = MagicMock()
        mock_client.query.side_effect = Forbidden(
            "Access Denied: Table foo.bar.baz: User does not have permission"
        )
        bq = bq_access(client=mock_client, billing="billing-proj", data="data-proj")

        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            req = {
                "table_id": "bq_view",
                "select": ["event_date"],
            }
            from unittest.mock import patch
            with patch.object(
                v2_scan, "_resolve_schema",
                lambda *a, **kw: {"event_date": "DATE", "country_code": "STRING"},
            ):
                with pytest.raises(HTTPException) as exc_info:
                    (
                        v2_scan.scan_estimate_endpoint(raw=req, user=user, conn=conn, bq=bq)
                    )
        finally:
            conn.close()

        assert exc_info.value.status_code == 502
        assert exc_info.value.detail["error"] == "bq_forbidden"

    def test_scan_estimate_returns_400_on_bq_bad_request(self, reload_db, bq_access):
        """`/scan/estimate` SQL is user-derived (built from req.select/where/order_by),
        so a BQ BadRequest must surface as HTTP 400 with `bq_bad_request`."""
        from app.api import v2_scan
        from google.api_core.exceptions import BadRequest

        mock_client = MagicMock()
        mock_client.query.side_effect = BadRequest(
            "Syntax error: unexpected token at line 1, column 5"
        )
        bq = bq_access(client=mock_client, billing="billing-proj", data="data-proj")

        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            req = {
                "table_id": "bq_view",
                "select": ["event_date"],
            }
            from unittest.mock import patch
            with patch.object(
                v2_scan, "_resolve_schema",
                lambda *a, **kw: {"event_date": "DATE", "country_code": "STRING"},
            ):
                with pytest.raises(HTTPException) as exc_info:
                    (
                        v2_scan.scan_estimate_endpoint(raw=req, user=user, conn=conn, bq=bq)
                    )
        finally:
            conn.close()

        assert exc_info.value.status_code == 400
        detail = exc_info.value.detail
        assert isinstance(detail, dict)
        assert detail["error"] == "bq_bad_request"
        assert "Syntax error" in detail["message"]
