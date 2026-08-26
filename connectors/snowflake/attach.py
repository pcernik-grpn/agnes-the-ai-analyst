"""Snowflake remote ATTACH helpers.

DuckDB's ``snowflake`` community extension connects to a Snowflake account
through a SECRET that carries ACCOUNT, USER, PASSWORD or KEY-PAIR, DATABASE
and WAREHOUSE. The non-secret coordinates are packed into the ``_remote_attach``
``url`` column so the orchestrator can gate credential egress with the host
allowlist before creating the SECRET and ATTACHing the catalog.
"""

from __future__ import annotations

import atexit
import base64
import binascii
import errno
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.orchestrator_security import escape_sql_string_literal, is_attach_host_allowed

logger = logging.getLogger(__name__)

SF_ALIAS = "sf"
SF_EXTENSION = "snowflake"
SF_TOKEN_ENV = "SNOWFLAKE_PASSWORD"

_ADBC_DRIVER_NAMES = (
    "libadbc_driver_snowflake.so",
    "libadbc_driver_snowflake.dylib",
    "adbc_driver_snowflake.dll",
)

_driver_missing_logged = False

# Account strings can include region/location separators and dots.
_SAFE_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# Database/warehouse/user/role are Snowflake identifiers; keep the regex
# linear-time and conservative.
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_private_key_write_lock = threading.Lock()


def _normalize_key_text(text: str) -> str:
    """Unescape literal newlines and normalize CRLF/CR to LF."""
    s = text.replace("\\r\\n", "\n").replace("\\r", "\n").replace("\\n", "\n")
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return s.strip()


def _load_private_key(data: bytes, passphrase: str | None = None):
    """Load an RSA private key from PEM or DER bytes, optionally decrypting it."""
    password = passphrase.encode() if passphrase else None

    if b"-----BEGIN" in data:
        try:
            key = serialization.load_pem_private_key(data, password=password)
        except TypeError as exc:
            msg = str(exc)
            if "Password was given but private key is not encrypted" in msg:
                key = serialization.load_pem_private_key(data, password=None)
            else:
                raise ValueError(msg) from exc
        except (ValueError, binascii.Error) as exc:
            raise ValueError(f"could not parse PEM private key: {exc}") from exc
    else:
        try:
            key = serialization.load_der_private_key(data, password=password)
        except TypeError as exc:
            msg = str(exc)
            if "Password was given but private key is not encrypted" in msg:
                key = serialization.load_der_private_key(data, password=None)
            else:
                raise ValueError(msg) from exc
        except (ValueError, binascii.Error) as exc:
            raise ValueError(f"could not parse DER private key: {exc}") from exc

    if not isinstance(key, rsa.RSAPrivateKey):
        raise TypeError(f"snowflake private key must be an RSA key, got {type(key).__name__}")
    return key


def _serialize_unencrypted_pkcs8_pem(key) -> str:
    """Return a PKCS#8 unencrypted PEM for the loaded key."""
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def _try_decode_base64_key(text: str) -> bytes:
    """Decode a base64-only key string, tolerating whitespace and URL-safe alphabet."""
    cleaned = re.sub(r"\s+", "", text)
    if not cleaned:
        raise ValueError("empty base64 key")
    for candidate in (cleaned, cleaned.replace("-", "+").replace("_", "/")):
        try:
            return base64.b64decode(candidate, validate=True)
        except (binascii.Error, ValueError):
            continue
    raise ValueError("not a valid base64 key")


def _snowflake_key_dir() -> Path:
    """Return a per-process, user-private directory for temporary PEM key files."""
    parent = Path(tempfile.gettempdir()) / "agnes-snowflake-keys"
    d = parent / f"pid-{os.getpid()}"
    parent.mkdir(parents=True, exist_ok=True)
    try:
        parent.chmod(0o700)
    except OSError:
        pass
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    return d


def _cleanup_snowflake_key_dir() -> None:
    d = Path(tempfile.gettempdir()) / "agnes-snowflake-keys" / f"pid-{os.getpid()}"
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


atexit.register(_cleanup_snowflake_key_dir)


def _safe_key_path_part(s: str) -> str:
    """Sanitize one filename component of a Snowflake temp-key path."""
    return re.sub(r"[^A-Za-z0-9_-]+", "_", s).strip("_")[:32] or "x"


def _private_key_file_path(private_key_pem: str, account: str, user: str, secret_name: str) -> Path:
    """Return a deterministic, per-process file path for a normalized PEM key."""
    digest = hashlib.sha256(f"{account}:{user}:{secret_name}:{private_key_pem}".encode()).hexdigest()[:24]
    return (
        _snowflake_key_dir()
        / f"{_safe_key_path_part(secret_name)}-{_safe_key_path_part(account)}-{_safe_key_path_part(user)}-{digest}.pem"
    )


def _write_private_key_pem(pem: str, path: Path) -> None:
    """Atomically write a PKCS#8 PEM to a private file, creating its directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    with _private_key_write_lock:
        try:
            tmp.write_text(pem, encoding="ascii")
            tmp.chmod(0o600)
            tmp.replace(path)
        except OSError:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            raise


def _duckdb_extension_dir() -> Path:
    """Return the DuckDB community-extension directory for this version/platform."""
    import platform as _platform

    import duckdb

    machine = _platform.machine().lower()
    system = sys.platform
    if system == "linux":
        base = "linux"
    elif system == "darwin":
        base = "osx"
    elif system.startswith("win"):
        base = "windows"
    else:
        base = system
    if machine in ("x86_64", "amd64"):
        arch = "amd64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    else:
        arch = machine
    return Path.home() / ".duckdb" / "extensions" / f"v{duckdb.__version__}" / f"{base}_{arch}"


def _find_adbc_driver_library() -> Path | None:
    """Locate the ADBC Snowflake shared library shipped with ``adbc-driver-snowflake``."""
    try:
        import adbc_driver_snowflake
    except ImportError:
        adbc_driver_snowflake = None

    if adbc_driver_snowflake is not None:
        pkg_dir = Path(adbc_driver_snowflake.__file__).parent
        for name in _ADBC_DRIVER_NAMES:
            candidate = pkg_dir / name
            if candidate.is_file():
                return candidate

    for p in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep):
        if not p:
            continue
        for name in _ADBC_DRIVER_NAMES:
            candidate = Path(p) / name
            if candidate.is_file():
                return candidate

    for p in ("/usr/local/lib", "/usr/lib"):
        for name in _ADBC_DRIVER_NAMES:
            candidate = Path(p) / name
            if candidate.is_file():
                return candidate
    return None


def install_snowflake_adbc_driver(*, missing_ok: bool = True) -> None:
    """Copy the ADBC Snowflake driver into DuckDB's extension directory.

    DuckDB's ``snowflake`` community extension looks for ``libadbc_driver_snowflake.*``
    in ``~/.duckdb/extensions/v<version>/<platform>/``. The ``adbc-driver-snowflake``
    Python package ships the matching shared library, but in its own site-packages
    directory, so this helper performs a one-time copy to the directory DuckDB
    actually searches. It is safe to call repeatedly: if the driver is already
    present it returns immediately.
    """
    ext_dir = _duckdb_extension_dir()
    for name in _ADBC_DRIVER_NAMES:
        if (ext_dir / name).is_file():
            return

    source = _find_adbc_driver_library()

    if source is None:
        if missing_ok:
            global _driver_missing_logged
            if not _driver_missing_logged:
                logger.warning(
                    "adbc_driver_snowflake not found; Snowflake extension may fail to load. "
                    "Install it (scripts/install-adbc-driver.sh) or set LD_LIBRARY_PATH."
                )
                _driver_missing_logged = True
            return
        raise RuntimeError(
            "ADBC Snowflake driver not found. Install it with scripts/install-adbc-driver.sh "
            "or 'uv pip install --python .venv/bin/python adbc-driver-snowflake'."
        )

    target = ext_dir / source.name
    ext_dir.mkdir(parents=True, exist_ok=True)
    tmp_target = ext_dir / f".{source.name}.{os.getpid()}.tmp"
    try:
        shutil.copy2(source, tmp_target)
        os.replace(tmp_target, target)
        logger.info("Copied ADBC Snowflake driver to %s", target)
    except OSError as exc:
        if tmp_target.is_file():
            tmp_target.unlink(missing_ok=True)
        logger.warning(
            "Could not stage ADBC Snowflake driver to %s: %s. "
            "DuckDB may still find the driver via LD_LIBRARY_PATH or system paths.",
            target,
            exc,
        )
        return


def _is_safe_segment(value: str, name: str) -> str:
    """Validate a URL path/query segment, raising ``ValueError`` if unsafe."""
    if not value or not _SAFE_SEGMENT_RE.match(value):
        raise ValueError(f"unsafe or empty snowflake {name}: {value!r}")
    return value


def _is_safe_account(value: str) -> str:
    if not value or not _SAFE_ACCOUNT_RE.match(value):
        raise ValueError(f"unsafe or empty snowflake account: {value!r}")
    return value


def build_remote_attach_url(
    account: str,
    database: str,
    warehouse: str,
    user: str,
    role: str = "",
) -> str:
    """Pack Snowflake connection coordinates into a single HTTPS URL.

    The account may already end with ``.snowflakecomputing.com``; otherwise
    the canonical host suffix is appended. Query parameters carry the
    database, warehouse, user and optional role. The password is intentionally
    NOT included here — it travels through the ``token_env`` column.
    """
    account = _is_safe_account(account.strip())
    database = _is_safe_segment(database.strip(), "database")
    warehouse = _is_safe_segment(warehouse.strip(), "warehouse")
    user = _is_safe_segment(user.strip(), "user")

    if account.lower().endswith(".snowflakecomputing.com"):
        host = account
    else:
        host = f"{account}.snowflakecomputing.com"

    query: dict[str, str] = {
        "database": database,
        "warehouse": warehouse,
        "user": user,
    }
    role = (role or "").strip()
    if role:
        query["role"] = _is_safe_segment(role, "role")

    return f"https://{host}?{urlencode(query)}"


def parse_remote_attach_url(url: str) -> dict[str, str]:
    """Reverse ``build_remote_attach_url`` for the orchestrator side."""
    parts = urlsplit((url or "").strip())
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError(
            f"snowflake _remote_attach url must be https://<account>.snowflakecomputing.com?...; got {url!r}"
        )

    host = parts.hostname
    if host.lower().endswith(".snowflakecomputing.com"):
        account = host[: -len(".snowflakecomputing.com")]
    else:
        account = host

    account = _is_safe_account(account)

    qs = parse_qs(parts.query, keep_blank_values=True)

    def _first(key: str) -> str:
        vals = qs.get(key, [])
        return vals[0] if vals else ""

    database = _is_safe_segment(_first("database"), "database")
    warehouse = _is_safe_segment(_first("warehouse"), "warehouse")
    user = _is_safe_segment(_first("user"), "user")
    role = _first("role").strip()
    if role:
        role = _is_safe_segment(role, "role")

    return {
        "account": account,
        "database": database,
        "warehouse": warehouse,
        "user": user,
        "role": role,
    }


def _looks_like_key_pair(token: str) -> bool:
    """Return True when ``token`` is a PEM, JSON-wrapped PEM, base64 DER key,
    or a filesystem path pointing at one of those."""
    t = (token or "").strip()
    if "-----BEGIN" in t and "-----END" in t:
        return True
    if t.startswith("{") and t.endswith("}"):
        try:
            data = json.loads(t)
        except json.JSONDecodeError:
            return False
        return isinstance(data, dict) and "private_key" in data

    # Heuristic for a pasted base64-only DER key (Snowflake keys are long RSA
    # keys; a short base64 password is left to the password path).
    cleaned = re.sub(r"\s+", "", t)
    if len(cleaned) >= 256:
        for candidate in (cleaned, cleaned.replace("-", "+").replace("_", "/")):
            try:
                decoded = base64.b64decode(candidate, validate=True)
                return decoded and decoded[0] == 0x30
            except (binascii.Error, ValueError):
                continue

    # A single-line value that resolves to an existing file is likely a
    # path to a PEM key (common in container/secret-file deployments).
    if "\n" not in t and len(t) < 4096:
        try:
            p = Path(t).expanduser()
            if p.is_file() and p.stat().st_size < 64 * 1024:
                return _looks_like_key_pair(p.read_text(encoding="utf-8", errors="strict"))
        # RuntimeError: ``Path("~nonuser").expanduser()`` raises it when the
        # user cannot be resolved (no such user / no home dir). This probe runs
        # for EVERY credential, passwords included, so a password starting with
        # '~' must not abort the caller — it is simply "not a key path".
        except (OSError, RuntimeError, UnicodeError, ValueError):
            pass
    return False


def _private_key_pem_and_passphrase(token: str, passphrase: str | None = None) -> tuple[str, str | None]:
    """Extract and normalize a private key into an unencrypted PKCS#8 PEM.

    The DuckDB Snowflake extension accepts an inline value or a file path via
    ``PRIVATE_KEY`` or ``PRIVATE_KEY_FILE``. We always write the normalized
    key to a private temp file and also pass it inline, so the extension can
    pick whichever option it recognizes and we are not exposed to version- or
    option-name differences.

    This function tolerates pasted PEMs with escaped ``\\n``/``\\r\\n``,
    Windows line endings, PKCS#1 ``RSA PRIVATE KEY`` blocks, encrypted keys,
    base64-only DER blobs, and filesystem paths pointing at any of the above.
    The optional passphrase is used to decrypt the key and is not forwarded
    to the generated SQL.
    """
    raw = (token or "").strip()
    if not raw:
        raise ValueError("empty snowflake private key")

    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"snowflake private key is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("snowflake private key JSON must be an object")
        raw = data.get("private_key") or ""
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("snowflake private key JSON missing 'private_key'")
        if not passphrase:
            passphrase = data.get("passphrase") or None

    if not raw.strip():
        raise ValueError("empty snowflake private key")

    # If the value is a single-line filesystem path (and not an inline PEM
    # with escaped \n), read the key from disk. This is common when the
    # credential is injected as a secret file mount.
    if "\n" not in raw and "-----BEGIN" not in raw and "-----END" not in raw and len(raw) < 4096:
        try:
            p = Path(raw).expanduser()
            if p.is_file() and p.stat().st_size < 64 * 1024:
                raw = p.read_text(encoding="utf-8", errors="strict")
        # RuntimeError: see _looks_like_key_pair — expanduser() raises it for an
        # unresolvable '~user' prefix, which is not an OSError/ValueError.
        except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
            # ENAMETOOLONG / EINVAL mean the value cannot be a filesystem name
            # at all, so it was never a path and this is not an error — fall
            # through and treat it as an inline key. A pasted base64 DER key is
            # exactly this case: base64's alphabet includes '/', so the value
            # splits into path components whose lengths depend on where those
            # '/' land, i.e. on the key's random bytes. Roughly one run in
            # twenty produces a component over NAME_MAX, which is why
            # test_attach_snowflake_key_pair_normalizes_pasted_keys was flaky
            # rather than reliably red. Every other failure (a real path that
            # cannot be read, an unresolvable '~user') still surfaces.
            if isinstance(exc, OSError) and exc.errno in (errno.ENAMETOOLONG, errno.EINVAL):
                pass
            else:
                # Neither the value nor the exception text may appear here.
                # `raw` is the credential itself (it is only reassigned to file
                # CONTENTS on the success branch above), and an OSError's own
                # str() ends with the offending filename — which in this branch
                # IS that same value. The message travels a long way: into
                # `init_extract`'s per-table errors, out through
                # `register-table`'s 500 body, and into `sync_state.error`,
                # which `GET /api/admin/registry`, /admin/sync and `agnes admin
                # list-tables` all render unredacted and without a TTL. The
                # exception class (plus errno symbol) distinguishes a bad mount
                # from a bad encoding without carrying the payload; the original
                # stays chained for a local traceback.
                _why = type(exc).__name__
                if isinstance(exc, OSError) and exc.errno is not None:
                    _why = f"{_why}/{errno.errorcode.get(exc.errno, exc.errno)}"
                raise ValueError(
                    "snowflake private key looked like a filesystem path but could not be read "
                    f"({_why}); check the file named by data_source.snowflake.private_key_env"
                ) from exc

    # Defense-in-depth: a key should never contain '$', but a pasted value
    # containing it could close a dollar-quoted SQL literal. Reject early.
    if "$" in raw:
        raise ValueError("snowflake private key contains the SQL dollar-quote tag; refusing to build a secret")

    text = _normalize_key_text(raw)

    if "-----BEGIN" in text and "-----END" in text:
        try:
            key = _load_private_key(text.encode(), passphrase)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"snowflake private key is not a valid PEM/DER key: {exc}") from exc
        return _serialize_unencrypted_pkcs8_pem(key), None

    try:
        der = _try_decode_base64_key(text)
    except ValueError as exc:
        raise ValueError("snowflake private key is not a PEM block or valid base64 key") from exc

    try:
        key = _load_private_key(der, passphrase)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"snowflake private key is not a valid PEM/DER key: {exc}") from exc

    return _serialize_unencrypted_pkcs8_pem(key), None


def _create_snowflake_secret_sql(
    secret_name: str,
    params: dict[str, str],
    token: str,
    passphrase: str | None = None,
) -> tuple[str, tuple[str, ...]]:
    """The ``CREATE OR REPLACE SECRET`` SQL, and the secret values it embedded.

    The second element is what makes the scrub in ``attach_snowflake`` correct
    rather than merely usually-correct. In the key-pair branch the statement
    does NOT carry ``token``: it carries
    ``_private_key_pem_and_passphrase(token, passphrase)``'s output, which
    *normalizes* the key to an unencrypted PKCS#8 PEM. Whenever the stored
    credential is PKCS#1, an encrypted key plus passphrase, a JSON wrapper with
    escaped newlines, or a filesystem path — i.e. most real key-pair
    deployments — the normalized PEM is not a substring of ``token``, so
    scrubbing ``token`` alone matches nothing and the whole private key rides
    out in the driver's echoed statement.

    Returning the embedded values instead of re-deriving them at the scrub site
    keeps the two from drifting: whatever this function puts into the SQL is
    exactly what gets redacted out of an error quoting it back.
    """
    role_sql = ""
    if params.get("role"):
        role_sql = f", ROLE '{escape_sql_string_literal(params['role'])}'"

    if _looks_like_key_pair(token):
        private_key_pem, _ = _private_key_pem_and_passphrase(token, passphrase)
        key_path = _private_key_file_path(
            private_key_pem,
            params["account"],
            params["user"],
            secret_name,
        )
        _write_private_key_pem(private_key_pem, key_path)
        # Set both PRIVATE_KEY (inline, dollar-quoted PKCS#8 PEM) and
        # PRIVATE_KEY_FILE (temp file). Different DuckDB/Snowflake-extension
        # builds prefer one or the other; the inline value guarantees that an
        # unrecognized PRIVATE_KEY_FILE option never leaves the driver with
        # an empty key value to parse.
        return (
            f"CREATE OR REPLACE SECRET {secret_name} ("
            f"TYPE snowflake, "
            f"ACCOUNT '{escape_sql_string_literal(params['account'])}', "
            f"USER '{escape_sql_string_literal(params['user'])}', "
            f"AUTH_TYPE 'key_pair', "
            f"PRIVATE_KEY $PK${private_key_pem}$PK$, "
            f"PRIVATE_KEY_FILE '{escape_sql_string_literal(str(key_path))}', "
            f"DATABASE '{escape_sql_string_literal(params['database'])}', "
            f"WAREHOUSE '{escape_sql_string_literal(params['warehouse'])}'"
            f"{role_sql})"
        ), (private_key_pem,)

    return (
        f"CREATE OR REPLACE SECRET {secret_name} ("
        f"TYPE snowflake, "
        f"ACCOUNT '{escape_sql_string_literal(params['account'])}', "
        f"USER '{escape_sql_string_literal(params['user'])}', "
        f"PASSWORD '{escape_sql_string_literal(token)}', "
        f"DATABASE '{escape_sql_string_literal(params['database'])}', "
        f"WAREHOUSE '{escape_sql_string_literal(params['warehouse'])}'"
        f"{role_sql})"
    ), (escape_sql_string_literal(token),)


# Secret-literal shapes `_create_snowflake_secret_sql` emits, terminated form
# first. Each is anchored on fixed delimiters with a single non-greedy span
# between them — linear-time over untrusted text, per the security playbook.
#
# The UNTERMINATED variants are not belt-and-braces, they are the ones that
# fire in practice. A DuckDB parser error quotes the offending statement back
# TRUNCATED ("LINE 1: " + a prefix), so the closing `$PK$` / `'` the terminated
# patterns need is exactly what the truncation cut off. Ordering matters: the
# terminated patterns run first so a complete literal is not over-redacted to
# end-of-message.
_SECRET_LITERAL_RES = (
    re.compile(r"PASSWORD\s*'[^']*'", re.I),
    re.compile(r"PRIVATE_KEY\s*\$PK\$.*?\$PK\$", re.I | re.S),
    re.compile(r"PRIVATE_KEY\s*\$PK\$[\s\S]*", re.I),
    re.compile(r"PASSWORD\s*'[^']*", re.I),
)


class SnowflakeAttachError(RuntimeError):
    """An ATTACH/SECRET failure whose message has had credential material removed."""


def _scrub_secret_material(message: str, *secrets: str | None) -> str:
    """Strip credential material from a driver error message.

    ``attach_snowflake`` EXECUTES a ``CREATE OR REPLACE SECRET … (PASSWORD '…')``
    / ``PRIVATE_KEY $PK$…$PK$`` statement, and DuckDB's parser-class errors quote
    the offending statement back — so on a build whose extension does not
    recognise one of those options, the raised error carries the Snowflake
    credential. Every caller then forwards it somewhere durable: the listing
    endpoint into a 502 body and a log line, the extract build into
    ``stats["errors"]`` → ``sync_state.error``, which the admin registry,
    /admin/sync and ``agnes admin list-tables`` render unredacted.

    Scrubs the known secret values first (exact substring, which also covers a
    driver reformatting the statement), then the literal shapes as a backstop
    for a value this call does not hold.

    Callers must pass what was actually EMBEDDED, not only what they were
    given: `_create_snowflake_secret_sql` normalizes a key-pair credential to
    unencrypted PKCS#8 before embedding it, so for a PKCS#1 / encrypted / JSON
    / path-shaped credential the two differ and the substring pass matches
    nothing. That is why it returns its embedded values.
    """
    out = message
    for secret in secrets:
        # Guard against a degenerate short value turning the message into noise.
        if secret and len(secret) >= 8:
            out = out.replace(secret, "<redacted>")
    for pattern in _SECRET_LITERAL_RES:
        out = pattern.sub("<redacted>", out)
    return out


def attach_snowflake(
    conn,
    *,
    alias: str,
    url: str,
    token: str,
    passphrase: str | None = None,
) -> None:
    """Install/load the Snowflake extension, create a SECRET, and ATTACH.

    ``url`` carries the non-secret coordinates; ``token`` is either the
    Snowflake password or a PEM / JSON-wrapped private key. Identifiers for
    ``alias`` and the derived SECRET name must be safe before this is called
    (the orchestrator validates the alias).
    """
    if not is_attach_host_allowed(url):
        raise ValueError(
            f"Snowflake host {url!r} is not in AGNES_REMOTE_ATTACH_HOST_ALLOWLIST; refusing to send credential"
        )

    params = parse_remote_attach_url(url)
    secret_name = f"sf_secret_{alias}"

    secret_sql, embedded_secrets = _create_snowflake_secret_sql(secret_name, params, token, passphrase)
    try:
        conn.execute(secret_sql)
        conn.execute(f"ATTACH '' AS {alias} (TYPE {SF_EXTENSION}, SECRET {secret_name}, READ_ONLY)")
    except Exception as exc:
        # Redact HERE, at the one place that holds the secret, so every caller
        # inherits the guarantee instead of each remembering to sanitize. Raised
        # `from None`: the original exception's own message is the thing being
        # withheld, and a chained cause would put it straight back into any
        # traceback that gets logged. The class name keeps it diagnosable.
        raise SnowflakeAttachError(
            f"snowflake ATTACH failed ({type(exc).__name__}): "
            # `embedded_secrets` — not just `token` — because the key-pair
            # branch embeds a NORMALIZED PKCS#8 PEM that need not be a
            # substring of the stored credential.
            + _scrub_secret_material(str(exc), token, passphrase, *embedded_secrets)
        ) from None
    logger.info("Attached Snowflake database %r as catalog %r", params["database"], alias)
