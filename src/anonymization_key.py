"""The per-instance anonymization HMAC key — resolve it, or provision it.

Design spec §9.2 (2026-08-27 fact-graph-over-collections) requires the
anonymize-in-front pipeline's entity substitution to be a per-instance HMAC
pseudonym (``PERSON_<hmac(key, ...)>``, ``COMPANY_<hmac(...)>``, …), never a
fixed marker: a fixed marker collapses every person in a document into one
node once facts are extracted, and a key shared across instances would let
the same person correlate across tenants.

Until owner decision 2026-09-01 that key was **operator-minted only**: an
admin had to generate a random value, set it as an env var on the server,
and point ``extraction.anonymization.hmac_key_env`` at that name. An
instance without that setup could never run an anonymize-marked scope — the
job failed closed, forever, with no in-product way out. This module makes
the key zero-ops: Agnes generates and stores it itself, encrypted, the first
time one is actually needed.

Precedence (strict, and in this order)
--------------------------------------
1. **Operator env path.** The admin-configurable env var NAME
   (``extraction.anonymization.hmac_key_env``, default
   ``AGNES_ANONYMIZATION_HMAC_KEY``) — gated by
   :func:`src.orchestrator_security.is_producer_key_env_allowed` BEFORE the
   value is read, exactly as before. An operator who mints their own key
   always wins; auto-provisioning must never quietly shadow it.
2. **The vault-stored instance key.** One reserved row
   (:data:`VAULT_SECRET_NAME`) in the existing ``system_secrets`` vault,
   Fernet-encrypted at rest by ``app/secrets_vault.py`` — the single
   existing owner of secret encryption in this codebase. No new crypto.
3. **Provision one.** When the vault is usable and no key exists yet:
   ``secrets.token_bytes(32)`` (rendered as hex, matching the value
   ``docs/anonymization.md`` tells an operator to generate by hand), stored
   through that same vault, one cataloged audit row
   (``anonymization.key_provisioned``), and a log line carrying the key's
   *fingerprint* only.
4. **Fail closed.** Vault unusable and no env key — the pre-existing
   behavior, with an error that now names BOTH fixes.

Why there is no rotate/regenerate path here
-------------------------------------------
Rotating this key rewrites every pseudonym: ``PERSON_<hmac(k1, "Ada")>`` and
``PERSON_<hmac(k2, "Ada")>`` are different nodes, so every already-ingested
document, claim and edge silently stops referring to the same person as
anything ingested afterwards. Design spec §9.2 flags this; §1 is explicit
that nothing retroactively re-anonymizes an already-ingested corpus. A key
that can be lost or regenerated *by accident* is therefore a data-integrity
bug, not an inconvenience — so provisioning here is **write-once**:

* an existing stored key is never overwritten (a second provisioning call
  returns the stored one, whoever wrote it);
* the insert is a single atomic ``INSERT … ON CONFLICT DO NOTHING``, so two
  workers provisioning concurrently converge on ONE key rather than one
  clobbering the other's;
* there is no ``rotate()``/``regenerate()`` in this module at all.

A real rotation feature has to be designed with the alias-rewrite (or
dual-key read) story it implies. It is deliberately not smuggled in as a
side effect of making the key zero-ops.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Default env var NAME for an operator-minted key. Kept here (and
#: re-exported from ``app.worker.kinds``) so the allowlist gate and the
#: default have exactly one definition.
ANONYMIZATION_HMAC_KEY_ENV_DEFAULT = "AGNES_ANONYMIZATION_HMAC_KEY"

#: Reserved, well-known ``system_secrets`` row name for the Agnes-provisioned
#: instance key. Namespaced (``anonymization/``) so it can never collide with
#: the other consumers of that table — the flat datasource/Slack secret names
#: and ``app.secrets``'s ``env_overlay/`` namespace.
VAULT_SECRET_NAME = "anonymization/hmac_key"

#: Stored-payload version. The row holds JSON, not a bare key, so the
#: provisioning timestamp travels with the key (the vault repo exposes no
#: ``get_updated_at`` for this scope, and ``updated_at`` would in any case be
#: a weaker claim than "this is when the key came into existence").
_PAYLOAD_VERSION = 1

# Guards the read-check-generate-insert sequence WITHIN one process. The
# cross-process guarantee comes from the atomic insert-if-absent below; this
# lock only stops two threads of the same worker each generating a candidate
# and both writing an audit row for a key only one of them stored.
_provision_lock = threading.Lock()


class AnonymizationKeyError(RuntimeError):
    """At least one selected scope is ``anonymize=true`` but no per-instance
    HMAC key resolves (design spec §9.2's pseudonym scheme —
    ``PERSON_<hmac(key, ...)>`` etc., never a fixed marker). Raised rather
    than silently omitting the key: falling back to a shared or absent key
    defeats the "tokens never correlate across tenants" guarantee the key
    exists for, so the job fails clean instead of running with a weaker
    guarantee than the wizard promised."""


def fingerprint(key: bytes) -> str:
    """Short, non-reversible identifier for a key — ``sha256(key)[:12]``.

    The ONLY form of the key that may appear in a log line, an audit row, or
    an admin UI. Twelve hex characters is enough for an operator to tell
    "the key this worker used" from "the key that one used" without being a
    useful oracle for the key itself.
    """
    return hashlib.sha256(key).hexdigest()[:12]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_env_name() -> str:
    """The configured (or default) env var NAME, allowlist-checked.

    Raises :class:`AnonymizationKeyError` when the configured name is not on
    :func:`src.orchestrator_security.is_producer_key_env_allowed`'s list.
    The name is admin-writable config, so without this gate an admin could
    point ``hmac_key_env`` at an unrelated instance secret
    (``ANTHROPIC_API_KEY``, ``JWT_SECRET_KEY``, …) and have it used as if it
    were the anonymization key.

    Deliberately uses ``is_producer_key_env_allowed`` — a SEPARATE, narrower
    allowlist from ``is_token_env_allowed`` (the connector-ATTACH
    ``token_env`` gate). Sharing the certificate's allowlist would
    additionally make this key a legal ``token_env`` for a connector-written
    ``_remote_attach`` row, letting a malicious connector exfiltrate the
    resolved key via ``ATTACH … TOKEN`` to a connector-chosen URL (RBAC
    review, 2026-08-28). See ``_PRODUCER_KEY_ENVS``'s docstring in
    ``src/orchestrator_security.py`` for the full trust-boundary argument.

    This gate fires BEFORE any vault lookup or provisioning: a misconfigured
    name is an error to surface, never a reason to quietly fall through to a
    generated key.
    """
    from app.instance_config import get_value
    from src.orchestrator_security import is_producer_key_env_allowed

    name = str(get_value("extraction", "anonymization", "hmac_key_env", default="") or "").strip()
    name = name or ANONYMIZATION_HMAC_KEY_ENV_DEFAULT

    if not is_producer_key_env_allowed(name):
        raise AnonymizationKeyError(
            f"extraction.anonymization.hmac_key_env={name!r} is not an allowed anonymization "
            f"key variable. Use the default name, {ANONYMIZATION_HMAC_KEY_ENV_DEFAULT}, or leave "
            "hmac_key_env empty."
        )
    return name


def _vault_usable() -> bool:
    """True iff a stable ``AGNES_VAULT_KEY`` is configured.

    Deliberately :func:`app.secrets_vault.vault_key_configured` and NOT
    ``can_store_secrets()``: the latter also allows the LOCAL_DEV_MODE
    ephemeral key, under which a provisioned key is re-generated on every
    restart. For most secrets that is a harmless dev convenience; for this
    one it would silently orphan every pseudonym already written — exactly
    the accidental key loss this module's write-once discipline exists to
    prevent. A keyless dev instance therefore keeps the old fail-closed
    behavior and the message that names both fixes.
    """
    try:
        from app.secrets_vault import vault_key_configured

        return bool(vault_key_configured())
    except Exception:  # pragma: no cover - import/config failure is "unusable"
        logger.exception("vault availability check failed; treating the vault as unusable")
        return False


def _secrets_repo() -> Any:
    from src.repositories import system_secrets_repo

    return system_secrets_repo()


def _decode_payload(raw: Optional[str]) -> Optional[dict]:
    """Parse a stored vault payload into ``{"key": hex, "provisioned_at": iso}``.

    Total: a missing row, an undecryptable one (``get`` already returns
    ``None`` for that), or a malformed payload all read as "no stored key".
    A malformed payload is logged, never repaired and never overwritten —
    overwriting is exactly the accident this module refuses to make.
    """
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        logger.error(
            "%s holds a malformed anonymization-key payload — refusing to overwrite it. "
            "Restore the vault row (or its AGNES_VAULT_KEY) rather than regenerating: a new "
            "key orphans every pseudonym already written.",
            VAULT_SECRET_NAME,
        )
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("key"), str) or not payload["key"]:
        logger.error(
            "%s holds an anonymization-key payload with no usable key — refusing to overwrite it.",
            VAULT_SECRET_NAME,
        )
        return None
    return payload


def _read_stored() -> Optional[dict]:
    """The stored instance key payload, or ``None``. Never raises."""
    try:
        return _decode_payload(_secrets_repo().get(VAULT_SECRET_NAME))
    except Exception:
        logger.exception("reading the stored anonymization key failed; treating it as absent")
        return None


def _audit_provisioned(fp: str, provisioned_at: str) -> None:
    from src.audit_helpers import log_safe

    log_safe(
        user_id=None,
        action="anonymization.key_provisioned",
        resource=f"anonymization_key:{VAULT_SECRET_NAME}",
        params={"fingerprint": fp, "provisioned_at": provisioned_at},
        result="success",
        client_kind="system",
    )


def _provision() -> dict:
    """Generate + store the instance key, or return the one that already exists.

    Race-safe by construction on both state backends: the write is a single
    ``INSERT … ON CONFLICT (name) DO NOTHING`` (``insert_if_absent``), and
    the value this function returns is always the **read-back** row, never
    the locally generated candidate. So when two workers provision at the
    same instant, exactly one insert takes effect and both return that same
    key — nothing is clobbered, and neither caller walks away with a key the
    store does not hold.
    """
    with _provision_lock:
        existing = _read_stored()
        if existing is not None:
            return existing

        candidate = secrets.token_bytes(32).hex()
        payload = {
            "version": _PAYLOAD_VERSION,
            "key": candidate,
            "provisioned_at": _now_iso(),
        }
        repo = _secrets_repo()
        created = repo.insert_if_absent(VAULT_SECRET_NAME, json.dumps(payload))

        stored = _read_stored()
        if stored is None:
            # Two ways to land here, and BOTH must fail rather than force a
            # write: the store rejected the insert, or a row already exists
            # that this process cannot read (AGNES_VAULT_KEY changed since it
            # was written, or the payload is malformed). Overwriting the
            # second case would destroy the real key — the exact accident the
            # write-once discipline exists to prevent, so the fix is to
            # restore the vault key, never to regenerate.
            raise AnonymizationKeyError(
                f"No usable per-instance anonymization key after provisioning ({VAULT_SECRET_NAME}). "
                "Either the vault write did not take effect, or a key is already stored that this "
                "process cannot decrypt — check that AGNES_VAULT_KEY is the same value the key was "
                "stored under. Refusing to overwrite it: a replacement key orphans every pseudonym "
                "already written."
            )

        fp = fingerprint(stored["key"].encode("utf-8"))
        if created and stored.get("key") == candidate:
            logger.info(
                "Generated this instance's anonymization HMAC key (fingerprint %s) and stored it "
                "encrypted in the vault as %s. It is never rotated automatically: a new key "
                "orphans every pseudonym already written.",
                fp,
                VAULT_SECRET_NAME,
            )
            _audit_provisioned(fp, str(stored.get("provisioned_at") or ""))
        else:
            logger.info(
                "Another process provisioned this instance's anonymization HMAC key first "
                "(fingerprint %s); using the stored key.",
                fp,
            )
        return stored


def resolve_or_provision_key() -> bytes:
    """This instance's anonymization HMAC key, provisioning one if needed.

    Precedence is strict — see the module docstring:
    operator env > stored vault key > freshly provisioned vault key >
    fail closed.

    Returns the key material as bytes (the UTF-8 encoding of the key
    string), so ``resolve_or_provision_key() == key_str.encode("utf-8")``
    for both sources and the fingerprint is comparable across them.

    Raises :class:`AnonymizationKeyError` — never returns a fallback or an
    empty key — when the configured env NAME is not allowlisted, or when no
    key can be resolved and none can be stored.
    """
    env_name = _resolve_env_name()

    value = os.environ.get(env_name)
    if value:
        return value.encode("utf-8")

    if not _vault_usable():
        raise AnonymizationKeyError(
            f"{env_name} is not set on the server and no per-instance anonymization key is "
            "stored, so it cannot be resolved. At least one selected scope is marked "
            "anonymize=true. Two fixes, either is enough: set AGNES_VAULT_KEY on the server "
            "and Agnes generates, stores and reuses the key itself (no further setup), or "
            f"mint one yourself and set {env_name} (see docs/anonymization.md). Otherwise "
            "unmark the scope in the connect wizard."
        )

    stored = _read_stored()
    if stored is None:
        stored = _provision()
    return str(stored["key"]).encode("utf-8")


def key_status() -> dict:
    """Read-only state of this instance's anonymization key, for admin UI.

    ``{"configured": bool, "source": "env" | "vault" | None,
    "fingerprint": str | None, "provisioned_at": str | None}``

    * ``source`` mirrors :func:`resolve_or_provision_key`'s precedence — an
      operator-set env var reads ``"env"`` even when a vault key also exists,
      because that is the key an actual run would use.
    * ``fingerprint`` is ``sha256(key)[:12]`` — the key value itself is never
      returned by this function, by design.
    * ``provisioned_at`` is set only for a vault-provisioned key (an
      operator-minted env key has no provisioning event Agnes knows about).

    **Never provisions** and **never raises** — a status read must not be
    what creates an instance's key (that belongs to a run that actually
    needs one), and a misconfigured env NAME must render as "not
    configured", not as a 500 on the page that would explain it.
    """
    unset = {"configured": False, "source": None, "fingerprint": None, "provisioned_at": None}

    try:
        env_name = _resolve_env_name()
    except AnonymizationKeyError:
        return dict(unset)
    except Exception:  # pragma: no cover - defensive: config read failure
        logger.exception("anonymization key_status: env name resolution failed")
        return dict(unset)

    value = os.environ.get(env_name)
    if value:
        return {
            "configured": True,
            "source": "env",
            "fingerprint": fingerprint(value.encode("utf-8")),
            "provisioned_at": None,
        }

    if not _vault_usable():
        return dict(unset)

    stored = _read_stored()
    if stored is None:
        return dict(unset)

    return {
        "configured": True,
        "source": "vault",
        "fingerprint": fingerprint(str(stored["key"]).encode("utf-8")),
        "provisioned_at": (str(stored["provisioned_at"]) if stored.get("provisioned_at") else None),
    }
