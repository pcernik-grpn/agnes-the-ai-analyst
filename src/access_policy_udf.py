"""The DuckDB-only scalar functions an access-policy body may call.

Exactly one today: ``agnes_hmac(VARCHAR) -> VARCHAR``, the keyed pseudonym
behind the builder's ``pseudonymize_keyed`` column mask. It returns the
lowercase hex HMAC-SHA256 of its argument under **this instance's**
anonymization key (``src/anonymization_key.py``), and ``NULL`` for ``NULL``.

Why a function at all, when ``md5(col)`` already exists
------------------------------------------------------
``md5`` is a pseudonym, not a mask: an unsalted digest over a low-entropy
domain (an email address, a short account id) is reversible by dictionary in
minutes, so the mask's only real guarantee is "the plaintext is not printed".
Keying the digest keeps everything ``md5`` bought -- the value is stable, so it
still joins across tables on this instance -- and takes the dictionary away
from anyone who does not hold the key. It is the same key, and the same
argument, as the anonymize-in-front pipeline's ``PERSON_<hmac(key, ...)>``
substitution: two instances never produce the same pseudonym for the same
person, so pseudonyms cannot correlate across tenants.

Where the key lives, and where it must never appear
---------------------------------------------------
Only in a Python closure, resolved once per process and cached. Deliberately
NOT in a table, a DuckDB ``SET`` variable, a session setting or the policy body
-- every one of those is readable back through the very connection the function
runs on, and the policy body is stored in the registry and rendered in the
admin UI. ``duckdb_functions()`` reports the function's NAME, never its Python
closure, which is what makes registration safe on a connection an analyst's own
SQL also runs on.

Resolution is lazy: :func:`register_policy_udfs` never touches the key, so
opening a connection is never what provisions an instance's key (that would
mint one on every install that merely serves a request). The first actual call
resolves it; a resolution failure raises, so a policy that cannot compute its
pseudonym fails the read rather than returning the plaintext it was masking.

DuckDB-only, by construction
----------------------------
There is no counterpart on BigQuery/Databricks, and there must not be one: the
key must never travel to a remote engine, and a same-named remote function
would silently pseudonymize under a key Agnes does not control. Three layers
keep it here -- the save-time validator refuses it for a ``query_mode='remote'``
table (``policy_function_duckdb_only``), the resolver's remote transpile
helpers refuse a body that references it (defence in depth for a row edited
straight into the database), and ``/api/query`` refuses it in caller-authored
SQL (otherwise any analyst could hash a candidate value on the same connection
and match it against the masked column -- the dictionary attack the key exists
to prevent).

Stdlib-only at import time on purpose: ``src/db.py`` imports this module, and
that import graph is walked by the CLI on every command.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: The keyed-pseudonym function's SQL name.
POLICY_HMAC_FUNCTION = "agnes_hmac"

#: Every name :func:`register_policy_udfs` registers -- the set the query
#: guard reserves from caller-authored SQL. One entry today; a second would
#: automatically inherit both the reservation and the remote refusal.
POLICY_UDF_NAMES: frozenset[str] = frozenset({POLICY_HMAC_FUNCTION})

_key_lock = threading.Lock()
_cached_key: Optional[bytes] = None


def reset_key_cache() -> None:
    """Drop the cached instance key.

    For tests (and, in principle, a process that has just been told its key
    changed). Production never calls this: the key is write-once per instance
    and a rotation would orphan every pseudonym already written -- see
    ``src/anonymization_key.py``'s module docstring on why there is no
    ``rotate()`` anywhere in this codebase.
    """
    global _cached_key
    with _key_lock:
        _cached_key = None


def _instance_key() -> bytes:
    """This instance's HMAC key, resolved at most once per process.

    Only a SUCCESSFUL resolution is cached: a transient vault outage must not
    poison the process into failing forever, and must never be answered with a
    fallback or empty key (``resolve_or_provision_key`` raises rather than
    inventing one).
    """
    global _cached_key
    key = _cached_key
    if key is not None:
        return key
    with _key_lock:
        if _cached_key is None:
            # Imported at call time, not module scope: this module is on
            # src/db.py's import path and must stay stdlib-only up front,
            # and the late binding is also what lets a test substitute the
            # resolver.
            from src.anonymization_key import resolve_or_provision_key

            _cached_key = resolve_or_provision_key()
        return _cached_key


def _hmac_hex(value: Any) -> Optional[str]:
    """The UDF body: lowercase hex HMAC-SHA256 of ``value`` under the key.

    ``NULL`` in, ``NULL`` out -- a pseudonym must not invent a real-looking
    value where there is none (the same edge the partial masks handle with an
    explicit ``IS NULL`` arm). The empty string is a value like any other and
    is hashed.
    """
    if value is None:
        return None
    try:
        key = _instance_key()
    except Exception as exc:
        # Fail CLOSED and say why without quoting anything: the caller sees a
        # failed query, never the plaintext this mask exists to hide. The
        # key's own resolution error (which names env vars, never key
        # material) goes to the log for the operator.
        logger.error("%s cannot run: this instance's anonymization key did not resolve: %s", POLICY_HMAC_FUNCTION, exc)
        raise RuntimeError(
            f"{POLICY_HMAC_FUNCTION}: this instance's anonymization key could not be resolved, "
            "so the keyed pseudonym cannot be computed"
        ) from exc
    return hmac.new(key, str(value).encode("utf-8"), hashlib.sha256).hexdigest()


#: DuckDB's own wording for "this name is taken on this connection"
#: (``NotImplementedException: A function by the name of 'x' is already
#: created``). Matched only as a fast path -- the authoritative check is the
#: catalog lookup below, which runs whenever the message does not match.
_ALREADY_CREATED_MARKER = "already created"


def register_policy_udfs(conn: Any) -> None:
    """Register the policy-only scalar functions on ``conn``. Idempotent.

    Called from every place that opens a connection a policy body can execute
    on: ``src.db.get_analytics_db_readonly`` (the choke point for /api/query,
    /api/mcp/query-table, the effective-access counts and the save-time
    ``probe_policy``), ``src.db.get_analytics_db``, and the throwaway
    ``:memory:`` connections ``/api/v2/sample`` and ``/api/v2/scan`` build over
    a parquet.

    Cheap by design (~0.3 ms against a ~4 ms connection open) and it resolves
    nothing: the key is fetched on first CALL, not here.

    Never raises. DuckDB refuses to create a function twice on one connection,
    and a registration is shared BOTH ways with that connection's cursors
    (verified: registering on a cursor makes the parent and every sibling
    cursor resolve it), so a repeat call on a long-lived handle -- the DuckLake
    reader hands out one cursor per request off a single attach -- lands in the
    "already there" branch, recognized from the engine's own message so the
    common repeat costs no catalog query at all.

    A registration that genuinely fails is logged and swallowed rather than
    propagated: taking down every read on a connection because ONE mask kind
    cannot be served would trade a fail-closed policy error for an outage. A
    policy that then calls the function fails closed on its own -- DuckDB
    errors on the unknown name, and a policy that fails to execute denies.
    """
    try:
        conn.create_function(POLICY_HMAC_FUNCTION, _hmac_hex, ["VARCHAR"], "VARCHAR")
    except Exception as exc:
        if _ALREADY_CREATED_MARKER in str(exc).lower() or _is_registered(conn):
            return
        logger.warning("could not register the %s policy function on this connection: %s", POLICY_HMAC_FUNCTION, exc)


def _is_registered(conn: Any) -> bool:
    """Whether ``conn`` already resolves the function (registrations are shared
    between a connection and its cursors, in both directions)."""
    try:
        row = conn.execute(
            "SELECT count(*) FROM duckdb_functions() WHERE function_name = ?",
            [POLICY_HMAC_FUNCTION],
        ).fetchone()
        return bool(row and row[0])
    except Exception:
        return False


def references_policy_udf(sql: str) -> Optional[str]:
    """The policy-only function ``sql`` calls, or ``None``.

    Name-based on the parsed tree -- the same shape (and the same reason) as
    ``app/api/query.py``'s SQL-string-table-function guard: the call is always
    a function node carrying the literal name, quoted (``"agnes_hmac"(x)``) or
    not, while an identifier that merely starts with the name
    (``agnes_hmac_note``) is left alone.

    Fails CLOSED on SQL sqlglot cannot parse: every caller uses this to REFUSE
    something, so over-matching on an unparseable body is the safe direction.
    """
    try:
        import sqlglot
        from sqlglot import exp
    except Exception:  # pragma: no cover - sqlglot is a hard dependency
        return _text_scan(sql)

    try:
        statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except Exception:
        return _text_scan(sql)
    if not statements:
        return _text_scan(sql)

    for statement in statements:
        for node in statement.find_all(exp.Func):
            raw = getattr(node, "this", None)
            if isinstance(raw, str):
                name = raw
            else:
                name = getattr(raw, "name", "") or ""
            if isinstance(name, str) and name.lower() in POLICY_UDF_NAMES:
                return name.lower()
    return None


def _text_scan(sql: str) -> Optional[str]:
    lowered = (sql or "").lower()
    for name in sorted(POLICY_UDF_NAMES):
        if name in lowered:
            return name
    return None
