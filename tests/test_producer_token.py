"""Tests for the producer-scoped callback credential (`app.auth.producer_token`).

Mirrors `tests/test_auth_scheduler_token.py`'s fixture shape — this is the
credential that REPLACES forwarding the scheduler shared secret to the
corpus-extraction producer subprocess.
"""

from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _jwt_env(monkeypatch):
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")


def _fake_request(method: str, path: str, path_params: dict) -> MagicMock:
    request = MagicMock()
    request.method = method
    request.url.path = path
    request.path_params = path_params
    return request


class TestMintProducerToken:
    def test_round_trips_claims(self):
        from app.auth.jwt import verify_token
        from app.auth.producer_token import mint_producer_token

        token = mint_producer_token(
            connection_id="conn1",
            collection_ids=["col-b", "col-a"],
            ttl_seconds=3600,
        )
        payload = verify_token(token)
        assert payload is not None
        assert payload["typ"] == "producer"
        assert payload["connection_id"] == "conn1"
        # Sorted, deduplicated.
        assert payload["collection_ids"] == ["col-a", "col-b"]

    def test_dedupes_collection_ids(self):
        from app.auth.jwt import verify_token
        from app.auth.producer_token import mint_producer_token

        token = mint_producer_token(
            connection_id="conn1",
            collection_ids=["col-a", "col-a", "col-b"],
            ttl_seconds=3600,
        )
        payload = verify_token(token)
        assert payload["collection_ids"] == ["col-a", "col-b"]

    def test_expires_after_ttl(self, monkeypatch):
        import datetime

        from app.auth.jwt import verify_token
        from app.auth.producer_token import mint_producer_token

        token = mint_producer_token(connection_id="conn1", collection_ids=[], ttl_seconds=1)
        assert verify_token(token) is not None

        # Freeze time far enough in the future that the token has expired —
        # patch datetime.now inside app.auth.jwt so verify_token sees it.
        real_datetime = datetime.datetime

        class _FrozenDatetime(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return real_datetime.now(tz) + datetime.timedelta(hours=1)

        import jwt as pyjwt

        # PyJWT checks exp against wall clock internally; simplest robust
        # check here is that a token minted with ttl_seconds=-1 is already
        # expired at verify time (no need to mock time at all).
        expired_token = mint_producer_token(connection_id="conn1", collection_ids=[], ttl_seconds=-1)
        assert verify_token(expired_token) is None
        # sanity: pyjwt import used only to document why (ExpiredSignatureError)
        assert pyjwt.ExpiredSignatureError is not None


class TestResolveProducerPrincipal:
    _ALLOWED = [
        ("GET", "/api/admin/sharepoint/connections/conn1/corpus-map", {"connection_id": "conn1"}),
        ("GET", "/api/admin/sharepoint/connections/conn1/scopes", {"connection_id": "conn1"}),
        ("POST", "/api/collections/col-a/files", {"collection_id": "col-a"}),
        ("POST", "/api/facts/ingest", {}),
        ("GET", "/api/facts/corrections", {}),
    ]

    @pytest.mark.parametrize("method,path,path_params", _ALLOWED)
    def test_allowed_surface_resolves_a_principal(self, method, path, path_params):
        from app.auth.producer_token import mint_producer_token, resolve_producer_principal
        from app.auth.session_principal import ProducerPrincipal

        token = mint_producer_token(connection_id="conn1", collection_ids=["col-a", "col-b"], ttl_seconds=3600)
        request = _fake_request(method, path, path_params)

        principal = resolve_producer_principal(token, request)

        assert isinstance(principal, ProducerPrincipal)
        assert principal.connection_id == "conn1"
        assert principal.collection_ids == frozenset({"col-a", "col-b"})
        assert principal.jti

    def test_off_surface_request_raises_403(self):
        from fastapi import HTTPException

        from app.auth.producer_token import mint_producer_token, resolve_producer_principal

        token = mint_producer_token(connection_id="conn1", collection_ids=["col-a"], ttl_seconds=3600)
        # A facts read route no gate protects beyond get_current_user —
        # exactly the over-wide surface this allowlist exists to close.
        request = _fake_request("POST", "/api/facts/search", {})

        with pytest.raises(HTTPException) as exc_info:
            resolve_producer_principal(token, request)
        assert exc_info.value.status_code == 403

    def test_wrong_method_on_an_otherwise_allowed_path_is_off_surface(self):
        from fastapi import HTTPException

        from app.auth.producer_token import mint_producer_token, resolve_producer_principal

        token = mint_producer_token(connection_id="conn1", collection_ids=[], ttl_seconds=3600)
        request = _fake_request("DELETE", "/api/facts/ingest", {})

        with pytest.raises(HTTPException) as exc_info:
            resolve_producer_principal(token, request)
        assert exc_info.value.status_code == 403

    def test_no_request_context_is_off_surface(self):
        from fastapi import HTTPException

        from app.auth.producer_token import mint_producer_token, resolve_producer_principal

        token = mint_producer_token(connection_id="conn1", collection_ids=[], ttl_seconds=3600)

        with pytest.raises(HTTPException) as exc_info:
            resolve_producer_principal(token, None)
        assert exc_info.value.status_code == 403

    def test_not_a_producer_token_returns_none(self):
        """A normal session JWT must fall through (None), not raise —
        `get_current_user` relies on this to continue its own chain."""
        from app.auth.jwt import create_access_token
        from app.auth.producer_token import resolve_producer_principal

        session_token = create_access_token(user_id="u1", email="a@b.com")
        request = _fake_request("POST", "/api/facts/ingest", {})

        assert resolve_producer_principal(session_token, request) is None

    def test_garbage_token_returns_none(self):
        from app.auth.producer_token import resolve_producer_principal

        request = _fake_request("POST", "/api/facts/ingest", {})
        assert resolve_producer_principal("not-a-jwt-at-all", request) is None

    def test_expired_producer_token_returns_none_not_403(self):
        """An expired producer token must fall through to the normal 401
        chain (`user_not_found`/`invalid_token`) rather than 403 — it is
        indistinguishable, post-expiry, from "not a producer token"."""
        from app.auth.producer_token import mint_producer_token, resolve_producer_principal

        token = mint_producer_token(connection_id="conn1", collection_ids=[], ttl_seconds=-1)
        request = _fake_request("POST", "/api/facts/ingest", {})

        assert resolve_producer_principal(token, request) is None

    def test_malformed_claims_return_none(self):
        """A producer-typed JWT missing connection_id/collection_ids (should
        never happen from `mint_producer_token`, but a forged/hand-built
        token might) is treated as not-a-producer-token, never crashes."""
        from app.auth.jwt import create_access_token
        from app.auth.producer_token import resolve_producer_principal

        token = create_access_token(user_id="", email="", typ="producer")
        request = _fake_request("POST", "/api/facts/ingest", {})

        assert resolve_producer_principal(token, request) is None


class TestPrincipalTypesSeamIsTotal:
    """``PRINCIPAL_TYPES`` conflates "not a full user dict" with "read its
    ``intersection``". Five call sites ask the first and then do the second,
    so every member must carry the attribute or those become an
    ``AttributeError`` (a 500) instead of a clean deny."""

    def test_every_principal_type_exposes_an_intersection(self):
        import dataclasses

        from app.auth.session_principal import PRINCIPAL_TYPES

        missing = [
            t.__name__ for t in PRINCIPAL_TYPES if "intersection" not in {f.name for f in dataclasses.fields(t)}
        ]
        assert not missing, (
            f"{missing} are in PRINCIPAL_TYPES but carry no `intersection` — "
            "src.rbac.get_accessible_ids and four sibling sites branch on "
            "PRINCIPAL_TYPES and then read it, so such a member 500s there."
        )

    def test_a_producer_principal_denies_through_the_generic_grant_helpers(self):
        from app.auth.access import can_access_session
        from app.auth.session_principal import ProducerPrincipal
        from src.rbac import get_accessible_ids

        principal = ProducerPrincipal(
            connection_id="conn-1",
            collection_ids=frozenset({"col-a"}),
            jti="jti-1",
        )

        # Denied even for a collection its OWN token names: the generic
        # grant-table primitives are not where a producer's authority lives
        # (that is the surface allowlist + each endpoint's own scope check),
        # so they must answer "nothing", never crash and never widen.
        assert get_accessible_ids(principal, "collection") == frozenset()
        assert can_access_session(principal, "collection", "col-a") is False
