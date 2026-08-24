"""Backend-parity test for the magic-link verify endpoint (#518 regression).

``POST /auth/email/verify`` consumes a ``reset_token`` through
``users_repo().consume_reset_token``. Before #518 the consume ran on a raw
DuckDB ``_get_db`` connection, so on a Postgres instance it read the frozen
DuckDB system file — the token written by ``send_magic_link`` (factory) lived
in PG, so verification never matched and login 401'd.

The repository contract is covered in ``test_users_contract.py``, but a
repository-only test can't catch a handler-wiring regression (the exact #518
failure mode). This seeds a user + reset_token THROUGH THE FACTORY (active
backend), calls the endpoint, and asserts a successful, single-use login on
BOTH backends.

Discriminator (pre-fix): duck PASS + pg FAIL => the backend-split bug.
"""

from __future__ import annotations

from datetime import datetime, timezone


def test_email_verify_consumes_token_on_both_backends(seeded_app_both, monkeypatch):
    from app.auth.token_hash import hash_token
    from src.repositories import users_repo

    # Email is opt-in-only by default (B6) — this test exercises the verify
    # endpoint's backend parity, not the default-offering policy, so name it
    # explicitly or the endpoint 404s before the parity assertion runs.
    monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "email,password")

    # Seed a fresh, unexpired magic-link token on the active backend, exactly
    # as send_magic_link would (reset_token + reset_token_created) — the token
    # is hashed at rest (audit M3), so seed the digest of the raw we submit.
    users_repo().update(
        id="admin1",
        reset_token=hash_token("magic-tok"),
        reset_token_created=datetime.now(timezone.utc),
    )

    client = seeded_app_both["client"]
    backend = seeded_app_both["backend"]

    r = client.post(
        "/auth/email/verify",
        json={"email": "admin@test.com", "token": "magic-tok"},
    )
    assert r.status_code == 200, (
        f"[{backend}] magic-link verify failed — backend-split: the consume "
        f"read the wrong backend (#518). body={r.text}"
    )
    assert r.json().get("access_token"), f"[{backend}] no access_token: {r.text}"

    # Single-use: the same token must not verify a second time.
    r2 = client.post(
        "/auth/email/verify",
        json={"email": "admin@test.com", "token": "magic-tok"},
    )
    assert r2.status_code == 401, f"[{backend}] consumed token verified twice: {r2.text}"
