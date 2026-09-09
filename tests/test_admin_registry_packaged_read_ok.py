"""``GET /api/admin/registry`` says whether its ``packaged`` stamps are real.

The endpoint stamps ``packaged`` on every row from one bulk data-package
membership read. When that read fails it logs and stamps ``False`` everywhere
— which, read naively, claims that every table is orphaned. A composed reader
(the ``admin_access_picture`` MCP tool) needs to tell that state apart from a
registry that is genuinely unpackaged, so the response carries
``packaged_read_ok``: ``True`` on a successful read, ``False`` when the stamps
are a fallback and must not be trusted.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_registry_reports_a_successful_membership_read(seeded_app):
    resp = seeded_app["client"].get("/api/admin/registry", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    assert resp.json()["packaged_read_ok"] is True


def test_registry_flags_a_failed_membership_read_instead_of_pretending(seeded_app, monkeypatch):
    import src.repositories as repos

    class _Broken:
        def list_member_ids_bulk(self):
            raise RuntimeError("membership store unavailable")

    monkeypatch.setattr(repos, "data_packages_repo", lambda *a, **k: _Broken())

    resp = seeded_app["client"].get("/api/admin/registry", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["packaged_read_ok"] is False
    # The per-row fallback is unchanged — existing consumers keep their shape.
    assert all(t["packaged"] is False for t in body["tables"])
