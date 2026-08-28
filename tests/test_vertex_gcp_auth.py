"""Unit tests for ``app/auth/vertex_gcp.py`` — Google access tokens for the
chat broker's Vertex AI mode. Mirrors ``tests/test_wif_auth.py``'s posture:
no network, google.auth monkeypatched at the seam.
"""

from __future__ import annotations

import google.auth
import pytest

from app.auth import vertex_gcp


class _FakeCreds:
    def __init__(self, *, valid: bool = True, token: str | None = "tok-1", refresh_exc: Exception | None = None):
        self.valid = valid
        self.token = token
        self.refresh_calls = 0
        self._refresh_exc = refresh_exc

    def refresh(self, _request):
        self.refresh_calls += 1
        if self._refresh_exc is not None:
            raise self._refresh_exc
        self.valid = True
        if self.token is None:
            return
        self.token = f"tok-refreshed-{self.refresh_calls}"


@pytest.fixture(autouse=True)
def _clean_cache():
    vertex_gcp.clear_token_cache()
    yield
    vertex_gcp.clear_token_cache()


def _patch_default(monkeypatch, creds):
    calls = {"n": 0}

    def fake_default(scopes=None):
        calls["n"] += 1
        assert scopes == ["https://www.googleapis.com/auth/cloud-platform"]
        return creds, "proj-x"

    monkeypatch.setattr(google.auth, "default", fake_default)
    return calls


def test_token_from_valid_credentials(monkeypatch):
    creds = _FakeCreds(valid=True, token="tok-abc")
    _patch_default(monkeypatch, creds)
    assert vertex_gcp.get_vertex_access_token() == "tok-abc"
    assert creds.refresh_calls == 0


def test_expired_credentials_are_refreshed(monkeypatch):
    creds = _FakeCreds(valid=False)
    _patch_default(monkeypatch, creds)
    assert vertex_gcp.get_vertex_access_token() == "tok-refreshed-1"
    assert creds.refresh_calls == 1


def test_credentials_object_is_cached_across_calls(monkeypatch):
    creds = _FakeCreds(valid=True, token="tok-abc")
    calls = _patch_default(monkeypatch, creds)
    vertex_gcp.get_vertex_access_token()
    vertex_gcp.get_vertex_access_token()
    assert calls["n"] == 1


def test_clear_token_cache_forces_re_resolution(monkeypatch):
    creds = _FakeCreds(valid=True, token="tok-abc")
    calls = _patch_default(monkeypatch, creds)
    vertex_gcp.get_vertex_access_token()
    vertex_gcp.clear_token_cache()
    vertex_gcp.get_vertex_access_token()
    assert calls["n"] == 2


def test_unresolvable_credentials_raise_actionable_error(monkeypatch):
    def fake_default(scopes=None):
        raise RuntimeError("no ADC anywhere")

    monkeypatch.setattr(google.auth, "default", fake_default)
    with pytest.raises(vertex_gcp.VertexAuthError) as exc:
        vertex_gcp.get_vertex_access_token()
    msg = str(exc.value)
    assert "GOOGLE_APPLICATION_CREDENTIALS" in msg
    assert "gcloud auth application-default login" in msg


def test_refresh_failure_raises_and_drops_cache(monkeypatch):
    bad = _FakeCreds(valid=False, refresh_exc=RuntimeError("SA key revoked"))
    calls = _patch_default(monkeypatch, bad)
    with pytest.raises(vertex_gcp.VertexAuthError, match="refresh failed"):
        vertex_gcp.get_vertex_access_token()
    # Cache dropped: the next call re-resolves instead of reusing the dead object.
    good = _FakeCreds(valid=True, token="tok-new")
    _patch_default(monkeypatch, good)
    assert vertex_gcp.get_vertex_access_token() == "tok-new"
    assert calls["n"] == 1


def test_refresh_without_token_raises(monkeypatch):
    creds = _FakeCreds(valid=False, token=None)
    _patch_default(monkeypatch, creds)
    with pytest.raises(vertex_gcp.VertexAuthError, match="no access token"):
        vertex_gcp.get_vertex_access_token()


def test_credentials_resolvable_ok_is_memoized(monkeypatch):
    creds = _FakeCreds(valid=True)
    calls = _patch_default(monkeypatch, creds)
    ok1, detail1 = vertex_gcp.credentials_resolvable()
    ok2, _ = vertex_gcp.credentials_resolvable()
    assert ok1 and ok2
    assert "resolvable" in detail1
    assert calls["n"] == 1  # second call served from the memo


def test_credentials_resolvable_failure_is_not_memoized(monkeypatch):
    attempts = {"n": 0}

    def flaky_default(scopes=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("not configured yet")
        return _FakeCreds(valid=True), "proj-x"

    monkeypatch.setattr(google.auth, "default", flaky_default)
    ok1, detail1 = vertex_gcp.credentials_resolvable()
    assert not ok1
    assert "Google ADC credentials unavailable" in detail1
    ok2, _ = vertex_gcp.credentials_resolvable()
    assert ok2  # the operator fixed it; no stale negative memo
    assert attempts["n"] == 2
