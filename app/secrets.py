"""Auto-generate and persist secrets that survive container restarts."""

import logging
import os
import secrets
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _state_dir() -> Path:
    """Return path to writable state directory.

    STATE_DIR env var takes precedence; otherwise defaults to
    ${DATA_DIR}/state for backward compatibility with deployments
    that nest state under the data disk. See docs/state-dir.md.
    """
    state = os.environ.get("STATE_DIR", "")
    if state:
        return Path(state)
    return Path(os.environ.get("DATA_DIR", "./data")) / "state"


# Module-level lock guarding read-modify-write of `.env_overlay`. Without it,
# two admins clicking "Save" on /admin/marketplaces (or /admin/server-config
# Initial Workspace section) in the same second can race on the same file:
# both read [X, Y], one writes [X, Y, A], the other writes [X, Y, B] and
# silently clobbers A. The lock is process-local; we rely on the app being
# the sole writer to `${STATE_DIR}/.env_overlay` (no out-of-process tools
# touch it). Reused (below) to serialize the vault write path too — not for
# DB atomicity (each vault write is a single-row upsert, already atomic) but
# to keep the os.environ mutation + publish ordered the same way the file
# path orders its read-merge-write, for one process writing multiple keys.
_overlay_lock = threading.Lock()

#: Coordination-backend pub/sub channel: published (with the bare env var
#: name as the message) every time a vault-mode ``persist_overlay_token``
#: call changes a key, so every other api/worker/gateway replica can re-read
#: that one key from the vault and refresh its own ``os.environ`` without a
#: restart. See ``app/main.py``'s lifespan subscribe/unsubscribe and
#: ``_state_checkpoint_loop`` (belt-and-braces periodic sweep).
OVERLAY_CHANGED_CHANNEL = "env-overlay-changed"

#: Vault key namespace for overlay tokens inside the ``system_secrets``
#: table (shared with Slack bot tokens / datasource secrets under their own
#: unnamespaced keys — see app/secrets_vault.py). Namespacing avoids
#: colliding with those other consumers of the same table.
_OVERLAY_VAULT_PREFIX = "env_overlay/"

# One-time-per-process warning guard for the keyless (no AGNES_VAULT_KEY)
# fallback path, so a busy admin session doesn't spam the log once per save.
_warned_vault_unusable = False


def _overlay_vault_key(env_name: str) -> str:
    return f"{_OVERLAY_VAULT_PREFIX}{env_name}"


def persist_overlay_token(env_name: str, value: Optional[str]) -> None:
    """Persist a secret env var so it survives restarts, and update ``os.environ``.

    Single shared helper for every code path that writes a secret this way
    (today: marketplaces PATs, the initial-workspace template PAT, and the
    Anthropic chat key). ``value=None`` or ``value=""`` removes
    the key; a non-empty value writes/replaces it.

    Two storage backends, chosen by whether the control-plane vault is
    usable (``AGNES_VAULT_KEY`` configured — see
    ``app.secrets_vault.vault_key_configured``):

    * **Vault mode** (production / multi-process): the token is written to
      the ``system_secrets`` vault table (namespaced ``env_overlay/<name>``,
      Fernet-encrypted at rest — see ``app/secrets_vault.py``) and an
      ``env-overlay-changed`` event is published on the coordination backend
      so every other process re-reads that key (see ``app/main.py``'s
      subscriber + the periodic belt-and-braces sweep in
      ``_state_checkpoint_loop``). FLUSHALL story: if the pub/sub event is
      lost (e.g. a Redis FLUSHALL, or a replica that was briefly
      disconnected), the affected process serves a stale value until the
      next periodic re-read (≤ ``AGNES_STATE_CHECKPOINT_INTERVAL_S``,
      default 300s) or its next restart — acceptable because these tokens
      change rarely (an admin rotating a PAT), not on a hot path.
    * **Keyless / S-tier mode** (``AGNES_VAULT_KEY`` unset): unchanged
      legacy behavior — read-merge-write into ``${STATE_DIR}/.env_overlay``
      under ``_overlay_lock``, plus a one-time-per-process warning that
      cross-process reload isn't available in this mode (there's only ever
      one process here, so there's nothing to synchronize).

    Path resolution for the file mode matches ``app/main.py``'s startup-time
    read; without this alignment, PATs persisted under the flat-mount layout
    (``STATE_DIR=/data-state``) would land at ``/data/state/.env_overlay``
    while the app reads from ``/data-state/.env_overlay``, silently
    dropping the token on the next restart.
    """
    from app.secrets_vault import vault_key_configured

    if vault_key_configured():
        _persist_overlay_token_vault(env_name, value)
        return

    global _warned_vault_unusable
    if not _warned_vault_unusable:
        logger.warning(
            "AGNES_VAULT_KEY is not configured; persisting %s to the legacy "
            "'.env_overlay' file instead of the control-plane vault. This is "
            "expected for a single-process/keyless (S-tier) install; a "
            "multi-process deployment should set AGNES_VAULT_KEY so overlay "
            "tokens replicate via the vault instead.",
            env_name,
        )
        _warned_vault_unusable = True
    _persist_overlay_token_file(env_name, value)


def _persist_overlay_token_file(env_name: str, value: Optional[str]) -> None:
    """Legacy file-backed path — see ``persist_overlay_token``."""
    overlay_path = _state_dir() / ".env_overlay"

    with _overlay_lock:
        overlay_path.parent.mkdir(parents=True, exist_ok=True)

        existing: dict[str, str] = {}
        if overlay_path.exists():
            for line in overlay_path.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    existing[k.strip()] = v.strip()

        if value:
            existing[env_name] = value
            os.environ[env_name] = value
        else:
            existing.pop(env_name, None)
            os.environ.pop(env_name, None)

        overlay_path.write_text("\n".join(f"{k}={v}" for k, v in existing.items()) + ("\n" if existing else ""))
        try:
            overlay_path.chmod(0o600)
        except OSError:
            pass


def _persist_overlay_token_vault(env_name: str, value: Optional[str]) -> None:
    """Vault-backed path — see ``persist_overlay_token``."""
    from src.repositories import system_secrets_repo

    repo = system_secrets_repo()
    key = _overlay_vault_key(env_name)

    with _overlay_lock:
        if value:
            repo.upsert(key, value)
            os.environ[env_name] = value
        else:
            repo.delete(key)
            os.environ.pop(env_name, None)

    try:
        from app.coordination.factory import coordination

        coordination().publish(OVERLAY_CHANGED_CHANNEL, env_name)
    except Exception:
        # Non-fatal: this process already applied the change to its own
        # os.environ above. A lost/failed publish just means other replicas
        # rely on the periodic re-read (see persist_overlay_token's FLUSHALL
        # note) instead of the immediate event.
        logger.exception("env-overlay-changed publish failed for %s (non-fatal)", env_name)


def reapply_overlay_token_from_vault(env_name: str) -> None:
    """Re-read ``env_name``'s current vault value and apply it to ``os.environ``.

    Used by (1) the cross-process ``env-overlay-changed`` subscriber — one
    key, event-driven — and (2) as the per-key step inside
    ``reapply_all_overlay_tokens_from_vault`` (boot load + periodic sweep).

    A vault row that no longer exists (the token was cleared on another
    replica) removes ``env_name`` from THIS process's ``os.environ`` too,
    mirroring ``persist_overlay_token``'s own delete-on-empty semantics.
    """
    from src.repositories import system_secrets_repo

    value = system_secrets_repo().get(_overlay_vault_key(env_name))
    if value:
        os.environ[env_name] = value
    else:
        os.environ.pop(env_name, None)


def reapply_all_overlay_tokens_from_vault() -> None:
    """Belt-and-braces full sweep of every ``env_overlay/*`` vault row.

    No-ops when the vault isn't usable (keyless/S-tier mode — nothing was
    ever written there). Otherwise applied:

    * once at boot, AFTER the legacy ``.env_overlay`` file load in
      ``app.main.create_app`` — the vault wins over the file on a conflict
      (see that call site's comment for the precedence rationale);
    * every tick of the periodic state-checkpoint loop
      (``app.main._state_checkpoint_loop``) as a belt-and-braces catch-all
      for any ``env-overlay-changed`` event a replica missed (see the
      FLUSHALL note on ``persist_overlay_token``).

    Each key is applied independently (log-and-continue) so one bad row
    (decrypt failure after a key rotation, transient DB hiccup) doesn't
    block every other overlay token from refreshing.
    """
    from app.secrets_vault import vault_key_configured

    if not vault_key_configured():
        return

    from src.repositories import system_secrets_repo

    try:
        keys = system_secrets_repo().list_names_with_prefix(_OVERLAY_VAULT_PREFIX)
    except Exception:
        logger.exception("listing env_overlay/* vault keys failed (non-fatal)")
        return

    for key in keys:
        env_name = key[len(_OVERLAY_VAULT_PREFIX) :]
        try:
            reapply_overlay_token_from_vault(env_name)
        except Exception:
            logger.exception("vault overlay re-read failed for %s (non-fatal)", env_name)


def _load_or_generate(env_var: str, file_name: str) -> str:
    """Load secret from env var, or from file, or generate and persist."""
    val = os.environ.get(env_var, "")
    if val:
        return val
    secret_path = _state_dir() / file_name
    if secret_path.exists():
        val = secret_path.read_text().strip()
        if val:
            return val
        logger.warning("Secret file %s is empty, regenerating", secret_path)
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    val = secrets.token_hex(32)
    secret_path.write_text(val)
    try:
        secret_path.chmod(0o600)
    except OSError:
        pass  # chmod not supported on all platforms (e.g., Windows)
    logger.info(
        "Auto-generated %s -> %s (set %s in .env to use a fixed value)",
        file_name,
        secret_path,
        env_var,
    )
    return val


def get_jwt_secret() -> str:
    """Get JWT secret key from env, file, or auto-generate."""
    return _load_or_generate("JWT_SECRET_KEY", ".jwt_secret")


def get_session_secret() -> str:
    """Get session secret from env, file, or auto-generate."""
    return _load_or_generate("SESSION_SECRET", ".session_secret")


# --- OAuth client_secret encryption at rest (#869) -------------------------
# The MCP OAuth SDK authenticates clients by comparing the presented secret
# against the value our provider's get_client() returns, so a one-way hash
# can't be verified inside the SDK's client-auth path. Instead we encrypt the
# client_secret at rest (reversible with an app-held key) so a DB/backup leak
# no longer exposes usable client secrets in plaintext; get_client decrypts to
# the raw value the SDK still compares by equality.
_CLIENT_SECRET_ENC_PREFIX = "enc:v1:"
_fernet_lock = threading.Lock()
_fernet_cached = None


def _client_secret_fernet():
    """Lazily build the Fernet used for client_secret encryption.

    The key is derived (sha256) from an app-managed secret so any key string —
    the auto-generated hex or an operator's ``AGNES_OAUTH_ENC_KEY`` override —
    yields a valid 32-byte Fernet key. Cached; the derivation runs once.

    Durability: the auto-generated key lives in ``${STATE_DIR}/.oauth_enc_key``.
    It is **irreplaceable** state, exactly like ``.jwt_secret`` /
    ``.session_secret`` — if STATE_DIR is not persisted across container
    recreation (or the key is rotated), previously-encrypted ``client_secret``s
    can no longer be decrypted and those OAuth clients must re-register
    (fail-closed, see ``decrypt_client_secret``). This adds no new deployment
    burden beyond the STATE_DIR persistence the other secret files already
    require — see docs/state-dir.md.
    """
    global _fernet_cached
    if _fernet_cached is not None:
        return _fernet_cached
    with _fernet_lock:
        if _fernet_cached is None:
            import base64
            import hashlib
            from cryptography.fernet import Fernet

            key_material = _load_or_generate("AGNES_OAUTH_ENC_KEY", ".oauth_enc_key")
            fernet_key = base64.urlsafe_b64encode(hashlib.sha256(key_material.encode()).digest())
            _fernet_cached = Fernet(fernet_key)
    return _fernet_cached


def encrypt_client_secret(raw: Optional[str]) -> Optional[str]:
    """Encrypt an OAuth client_secret for storage. Idempotent and total:
    returns ``None``/empty unchanged, and returns an already-encrypted value
    (``enc:v1:`` prefix) untouched so re-persisting a decrypted-then-stored
    row never double-encrypts."""
    if not raw or raw.startswith(_CLIENT_SECRET_ENC_PREFIX):
        return raw
    token = _client_secret_fernet().encrypt(raw.encode()).decode()
    return _CLIENT_SECRET_ENC_PREFIX + token


def decrypt_client_secret(stored: Optional[str]) -> Optional[str]:
    """Decrypt a stored client_secret back to the raw value.

    Legacy rows written before encryption (no ``enc:v1:`` prefix) are returned
    verbatim, so existing clients keep working until their next re-registration
    re-encrypts them. If decryption fails (key rotated/corrupt), we fail
    **closed** — return an unmatchable sentinel so the SDK's constant-time
    secret comparison rejects the client, rather than returning ``None`` (which
    the SDK treats as "no secret required" and would let the client in)."""
    if not stored or not stored.startswith(_CLIENT_SECRET_ENC_PREFIX):
        return stored
    token = stored[len(_CLIENT_SECRET_ENC_PREFIX):]
    try:
        return _client_secret_fernet().decrypt(token.encode()).decode()
    except Exception:
        logger.error(
            "OAuth client_secret decryption failed (key rotated or ciphertext "
            "corrupt); failing client authentication closed"
        )
        return "\x00invalid-decrypt-" + secrets.token_hex(16)
