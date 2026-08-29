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
        replace_of: tuple[str, str, str] | None = None,
    ) -> None:
        """Insert this user's identity row.

        ``replace_of=(provider_type, tenant_id, subject)`` swaps a stale
        binding in place (tenant re-point — design: replacement rule) as a
        compare-and-swap against the OBSERVED stale identity: the row is
        rewritten — fresh ``linked_at``, ``last_login_at`` reset to NULL,
        because a replacement is a NEW binding — only if it still carries
        exactly that identity. A concurrent login that already replaced it
        makes the swap miss (rowcount 0), which raises
        :class:`IdentityLinkConflictError` so the caller re-reads and applies
        the race rule instead of silently clobbering the winner. Any
        uniqueness conflict raises the same error.
        """
        params = {
            "user_id": user_id,
            "provider_type": provider_type,
            "tenant_id": tenant_id,
            "subject": subject,
            "email_at_link": email_at_link,
        }
        try:
            with self._engine.begin() as conn:
                if replace_of is not None:
                    params.update(
                        {
                            "old_provider_type": replace_of[0],
                            "old_tenant_id": replace_of[1],
                            "old_subject": replace_of[2],
                        }
                    )
                    result = conn.execute(
                        sa.text(
                            "UPDATE user_external_identities SET "
                            "provider_type = :provider_type, "
                            "tenant_id = :tenant_id, "
                            "subject = :subject, "
                            "email_at_link = :email_at_link, "
                            "linked_at = CURRENT_TIMESTAMP, "
                            "last_login_at = NULL "
                            "WHERE user_id = :user_id "
                            "AND provider_type = :old_provider_type "
                            "AND tenant_id = :old_tenant_id "
                            "AND subject = :old_subject"
                        ),
                        params,
                    )
                    if result.rowcount == 0:
                        raise IdentityLinkConflictError("stale-binding replacement lost a concurrent update")
                else:
                    conn.execute(
                        sa.text(
                            "INSERT INTO user_external_identities "
                            "(user_id, provider_type, tenant_id, subject, email_at_link) "
                            "VALUES (:user_id, :provider_type, :tenant_id, :subject, :email_at_link)"
                        ),
                        params,
                    )
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
        """One page of linked identities, ``linked_at`` DESC, with the bound
        user's CURRENT email joined in (``email`` — ``email_at_link`` stays
        the drift-forensics snapshot).

        Deliberately no unbounded list method — one admin call must not
        serialize an entire tenant's links (design: identities pagination).
        """
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT uei.*, u.email AS email "
                        "FROM user_external_identities uei "
                        "JOIN users u ON u.id = uei.user_id "
                        "ORDER BY uei.linked_at DESC, uei.user_id "
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
