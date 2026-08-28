"""Postgres-only repository for ``user_external_identities`` (design 2026-08-28).

One external principal per Agnes user (``user_id`` is the PK). ``subject``
stores the validated token's ``oid`` (never the OIDC ``sub`` — see the model
docstring in ``src/models/sso.py``); ``tenant_id`` stores the token's ``tid``
GUID (never the admin-configured tenant string). Uniqueness is
``(provider_type, tenant_id, subject)``.

The binding *policy* (subject-hit beats email attach, stale-binding
replacement, same-tenant conflict refusal) lives in the ``sso`` auth
provider; this repo provides the primitives and reports uniqueness conflicts
as the typed :class:`IdentityLinkConflictError` so the provider can re-read
and decide.

PG-first ratchet (A3): brand-new app-state surface added after the freeze,
so there is no DuckDB sibling — see ``docs/migrations.md`` -> "Adding a
PG-only feature". Reach this repo only through
``src.repositories.user_external_identities_repo()``; on a DuckDB-backed
instance that factory call raises ``RequiresPostgresBackend`` (translated to
a ``501`` by the app-wide handler).
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class IdentityLinkConflictError(RuntimeError):
    """A ``link()`` hit a uniqueness conflict it was not allowed to resolve.

    Either the ``(provider_type, tenant_id, subject)`` key already belongs to
    another user, or (without ``replace_existing``) this user already holds
    an identity row. The caller re-reads by subject and applies the binding
    algorithm's race rule — this error never carries row data.
    """


class UserExternalIdentitiesPgRepository:
    """External identity bindings keyed on the validated token's claims."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get_by_user_id(self, user_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM user_external_identities WHERE user_id = :user_id"),
                    {"user_id": user_id},
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def get_by_subject(self, provider_type: str, tenant_id: str, subject: str) -> dict[str, Any] | None:
        """Lookup by the validated token's ``(tid, oid)`` — the login-time read."""
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text(
                        "SELECT * FROM user_external_identities "
                        "WHERE provider_type = :provider_type AND tenant_id = :tenant_id "
                        "AND subject = :subject"
                    ),
                    {"provider_type": provider_type, "tenant_id": tenant_id, "subject": subject},
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def link(
        self,
        *,
        user_id: str,
        provider_type: str,
        tenant_id: str,
        subject: str,
        email_at_link: str,
        replace_existing: bool = False,
    ) -> None:
        """Insert this user's identity row.

        ``replace_existing=True`` swaps a stale binding in place (tenant
        re-point — design: replacement rule): the row is rewritten with a
        fresh ``linked_at`` and ``last_login_at`` reset to NULL, because a
        replacement is a NEW binding. Any uniqueness conflict this call may
        not resolve raises :class:`IdentityLinkConflictError`.
        """
        params = {
            "user_id": user_id,
            "provider_type": provider_type,
            "tenant_id": tenant_id,
            "subject": subject,
            "email_at_link": email_at_link,
        }
        if replace_existing:
            stmt = (
                "INSERT INTO user_external_identities "
                "(user_id, provider_type, tenant_id, subject, email_at_link) "
                "VALUES (:user_id, :provider_type, :tenant_id, :subject, :email_at_link) "
                "ON CONFLICT (user_id) DO UPDATE SET "
                "provider_type = EXCLUDED.provider_type, "
                "tenant_id = EXCLUDED.tenant_id, "
                "subject = EXCLUDED.subject, "
                "email_at_link = EXCLUDED.email_at_link, "
                "linked_at = CURRENT_TIMESTAMP, "
                "last_login_at = NULL"
            )
        else:
            stmt = (
                "INSERT INTO user_external_identities "
                "(user_id, provider_type, tenant_id, subject, email_at_link) "
                "VALUES (:user_id, :provider_type, :tenant_id, :subject, :email_at_link)"
            )
        try:
            with self._engine.begin() as conn:
                conn.execute(sa.text(stmt), params)
        except sa.exc.IntegrityError as exc:
            raise IdentityLinkConflictError("external identity link conflicts with an existing binding") from exc

    def touch_last_login(self, user_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE user_external_identities SET last_login_at = CURRENT_TIMESTAMP WHERE user_id = :user_id"
                ),
                {"user_id": user_id},
            )

    def unlink(self, user_id: str) -> bool:
        """Hard-delete this user's identity row (admin unlink). ``False`` when absent."""
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text("DELETE FROM user_external_identities WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
        return result.rowcount > 0

    def list_page(self, *, limit: int, offset: int) -> list[dict[str, Any]]:
        """One page of linked identities, ``linked_at`` DESC.

        Deliberately no unbounded list method — one admin call must not
        serialize an entire tenant's links (design: identities pagination).
        """
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT * FROM user_external_identities "
                        "ORDER BY linked_at DESC, user_id "
                        "LIMIT :limit OFFSET :offset"
                    ),
                    {"limit": limit, "offset": offset},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def count(self) -> int:
        with self._engine.connect() as conn:
            n = conn.execute(sa.text("SELECT COUNT(*) FROM user_external_identities")).scalar()
        return int(n or 0)
