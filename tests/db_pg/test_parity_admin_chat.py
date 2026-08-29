"""Backend-parity tests for the admin_chat cluster (app/api/admin_chat.py).

Seeds chat sessions through the backend-aware factory (chat_session_repo())
so the row lands in whichever backend is active, then exercises the
admin-chat endpoints via seeded_app_both — once on DuckDB, once on Postgres.

Discriminator (general): a route that reads persisted session state through
the factory returns the seeded session on BOTH backends; a route that reads
through a raw DuckDB connection returns it on DuckDB but a stale/empty result
on Postgres, so the [pg] parametrization fails.

Cluster-specific harness note
-----------------------------
The admin_chat read endpoints (``/tail-ticket``, the tail WS) read persisted
session state via ``app.state.chat_repo``, which is populated in the FastAPI
*lifespan* startup (``app/main.py`` CHAT-INIT block), NOT at ``create_app()``
time. ``TestClient(app)`` does not enter the lifespan unless used as a context
manager. The default ``seeded_app_both`` harness builds ``TestClient(app)``
without ``with``, so ``app.state.chat_repo`` is ``None`` → every read 503s
``chat_disabled`` on BOTH backends (a harness artifact, not a product signal).

These tests therefore enter the lifespan explicitly with ``with client as c:``
so ``app.state.chat_repo`` is wired before the endpoint is called — only then
is the read path exercised and the backends comparable.
"""
from __future__ import annotations


def _auth(seeded_app_both, who="admin"):
    return {"Authorization": f"Bearer {seeded_app_both[f'{who}_token']}"}


def _seed_session(title="parity probe"):
    """Create a chat session through the factory (PG or DuckDB)."""
    from app.chat.types import Surface
    from src.repositories import chat_session_repo

    return chat_session_repo().create_session(
        user_email="analyst@test.com",
        surface=Surface.WEB,
        title=title,
    )


# ---------------------------------------------------------------------------
# Factory-roundtrip control: prove the seed lands in the active backend and is
# readable back through the factory on BOTH engines. If this fails on pg the
# test itself (seed signature / wiring) is wrong, not the product.
# ---------------------------------------------------------------------------

def test_factory_roundtrip_reads_seeded_session(seeded_app_both):
    from src.repositories import chat_session_repo

    sess = _seed_session(title="roundtrip")
    fetched = chat_session_repo().get_session(sess.id)
    assert fetched is not None, (
        f"[{seeded_app_both['backend']}] factory get_session lost the seeded row"
    )
    assert fetched.id == sess.id
    assert fetched.user_email == "analyst@test.com"


# ---------------------------------------------------------------------------
# POST /admin/chat/{chat_id}/tail-ticket (POST since the broker admin-read
# hardening: minting a ticket is a state change — see app/api/admin_chat.py)
# Reads via app.state.chat_repo.get_session(); mints a ticket if the session
# exists (200), else 404. The read discriminator for the cluster.
# Must enter the lifespan so app.state.chat_repo is wired (see module docstring).
# ---------------------------------------------------------------------------

def test_tail_ticket_finds_seeded_session(seeded_app_both):
    sess = _seed_session()
    client = seeded_app_both["client"]
    with client as c:
        r = c.post(
            f"/admin/chat/{sess.id}/tail-ticket",
            headers=_auth(seeded_app_both),
        )
    assert r.status_code == 200, (
        f"[{seeded_app_both['backend']}] tail-ticket should find seeded session "
        f"{sess.id} but got {r.status_code}: {r.text}"
    )
    body = r.json()
    assert body.get("ticket"), body
    assert sess.id in body.get("ws_url", ""), body


def test_tail_ticket_missing_session_is_404(seeded_app_both):
    """Negative control: an unseeded id 404s on both backends."""
    client = seeded_app_both["client"]
    with client as c:
        r = c.post(
            "/admin/chat/chat_does_not_exist/tail-ticket",
            headers=_auth(seeded_app_both),
        )
    assert r.status_code == 404, (
        f"[{seeded_app_both['backend']}] expected 404 for unknown session, "
        f"got {r.status_code}: {r.text}"
    )


# ---------------------------------------------------------------------------
# GET /admin/chat  (Accept: application/json)
# list_active reads in-memory chat_manager.list_live(), NOT persisted state,
# so it is backend-agnostic by construction. Asserted here only to pin that
# the route is reachable + admin-gated identically on both backends (it must
# NOT regress into a persisted-state read without a factory route).
# ---------------------------------------------------------------------------

def test_list_active_reachable_both_backends(seeded_app_both):
    client = seeded_app_both["client"]
    with client as c:
        r = c.get(
            "/admin/chat",
            headers={**_auth(seeded_app_both), "Accept": "application/json"},
        )
    assert r.status_code == 200, (
        f"[{seeded_app_both['backend']}] /admin/chat should be reachable: "
        f"{r.status_code} {r.text}"
    )
    assert "sessions" in r.json(), r.json()
