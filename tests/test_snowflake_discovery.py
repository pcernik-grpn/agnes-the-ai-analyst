"""Snowflake table discovery — the browse half of "add a Snowflake table".

Snowflake was the only source type whose wizard step had no discovery: two
free-text inputs, nothing validated against the account, and the registry id
auto-composed as ``schema + "_" + table``. Paste a name that already carries
its schema prefix and you get ``gold_gold_bi_supply_demand`` pointing at a
table that does not exist — which then sits in the registry forever, because
nothing retries a remote-extract rebuild except a re-save.

These tests pin the primitive the picker is built on. The Snowflake round-trip
is stubbed: the live attach path is exercised by tests/test_snowflake_connector.py.
"""

from unittest.mock import MagicMock

import pytest

from connectors.snowflake import discovery

SF_SETTINGS = {
    "account": "acct-123",
    "user": "SVC",
    "database": "PROD",
    "warehouse": "WH",
    "role": "",
    "auth_type": "password",
    "password": "s3cret",
    "token_env": "SNOWFLAKE_PASSWORD",
}

_ROWS = [
    ("GOLD", "BI_DEMAND", "BASE TABLE"),
    ("GOLD", "BI_SUPPLY_AVAILABILITY", "BASE TABLE"),
    ("GOLD", "V_LATEST", "VIEW"),
    ("STAGING", "RAW_EVENTS", "BASE TABLE"),
]


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(discovery, "resolve_snowflake_settings", lambda: SF_SETTINGS.copy())
    return SF_SETTINGS


@pytest.fixture
def fake_conn():
    """Stand-in for the DuckDB session the attach happens on."""
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = _ROWS
    return conn


def _patch_session(monkeypatch, conn, attach=None):
    monkeypatch.setattr(discovery, "_open_duckdb", lambda *a, **k: conn)
    monkeypatch.setattr(discovery, "attach_snowflake", attach or MagicMock())
    monkeypatch.setattr(discovery, "install_snowflake_adbc_driver", MagicMock())


def test_list_tables_groups_by_schema(configured, fake_conn, monkeypatch):
    _patch_session(monkeypatch, fake_conn)

    out = discovery.list_tables()

    assert out["database"] == "PROD"
    assert [s["name"] for s in out["schemas"]] == ["GOLD", "STAGING"]
    gold = out["schemas"][0]
    assert gold["tables"] == [
        {"name": "BI_DEMAND", "table_type": "BASE TABLE"},
        {"name": "BI_SUPPLY_AVAILABILITY", "table_type": "BASE TABLE"},
        {"name": "V_LATEST", "table_type": "VIEW"},
    ]


def test_list_tables_excludes_information_schema(configured, fake_conn, monkeypatch):
    """The account's own metadata schema is noise in a picker — and offering it
    for registration would produce rows nobody wants."""
    _patch_session(monkeypatch, fake_conn)

    discovery.list_tables()

    sql = " ".join(str(c) for c in fake_conn.execute.call_args_list)
    assert "INFORMATION_SCHEMA" in sql.upper()


def test_list_tables_returns_none_when_not_configured(monkeypatch):
    """No Snowflake block / no credential is a "nothing to browse" answer, not a
    crash — the caller turns it into a 400 with the setup hint."""
    monkeypatch.setattr(discovery, "resolve_snowflake_settings", lambda: None)

    assert discovery.list_tables() is None


def test_list_tables_refuses_host_outside_allowlist(configured, fake_conn, monkeypatch):
    """Same egress gate the extract build applies: the credential must not be
    shipped to a host the operator did not allow."""
    _patch_session(monkeypatch, fake_conn)
    monkeypatch.setattr(discovery, "is_attach_host_allowed", lambda url: False)

    with pytest.raises(ValueError, match="ALLOWLIST"):
        discovery.list_tables()


def test_list_tables_closes_session_on_failure(configured, monkeypatch):
    """A failed catalog query must not leak the DuckDB handle (and with it the
    attached Snowflake session)."""
    conn = MagicMock()
    conn.execute.side_effect = RuntimeError("boom")
    _patch_session(monkeypatch, conn)

    with pytest.raises(RuntimeError):
        discovery.list_tables()
    conn.close.assert_called_once()


# ---- the endpoint the picker calls ----------------------------------------

_LISTING = {
    "database": "PROD",
    "schemas": [{"name": "GOLD", "tables": [{"name": "BI_DEMAND", "table_type": "BASE TABLE"}]}],
}


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_endpoint_returns_grouped_listing(seeded_app, monkeypatch):
    monkeypatch.setattr("connectors.snowflake.discovery.list_tables", lambda schema=None: _LISTING)
    c = seeded_app["client"]

    resp = c.get("/api/admin/data-sources/snowflake/tables", headers=_auth(seeded_app["admin_token"]))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source_type"] == "snowflake"
    assert body["database"] == "PROD"
    assert body["schemas"][0]["tables"][0]["name"] == "BI_DEMAND"


def test_endpoint_passes_schema_filter_through(seeded_app, monkeypatch):
    seen = {}

    def _listing(schema=None):
        seen["schema"] = schema
        return _LISTING

    monkeypatch.setattr("connectors.snowflake.discovery.list_tables", _listing)
    c = seeded_app["client"]

    resp = c.get(
        "/api/admin/data-sources/snowflake/tables?schema=gold",
        headers=_auth(seeded_app["admin_token"]),
    )

    assert resp.status_code == 200, resp.text
    assert seen["schema"] == "gold"


def test_endpoint_requires_admin(seeded_app, monkeypatch):
    """Browsing an account's catalog is reconnaissance; it stays admin-only."""
    monkeypatch.setattr("connectors.snowflake.discovery.list_tables", lambda schema=None: _LISTING)
    c = seeded_app["client"]

    resp = c.get("/api/admin/data-sources/snowflake/tables")

    assert resp.status_code in (401, 403), resp.text


def test_endpoint_rejects_unbrowsable_source_type(seeded_app):
    """Keboola lists per connection — this route must not pretend to serve it."""
    c = seeded_app["client"]

    resp = c.get("/api/admin/data-sources/keboola/tables", headers=_auth(seeded_app["admin_token"]))

    assert resp.status_code == 400, resp.text
    assert "source-connections" in resp.json()["detail"]


def test_endpoint_explains_unconfigured_snowflake(seeded_app, monkeypatch):
    """`None` from the connector is a setup problem — say what to set, rather
    than answering 200 with an empty account."""
    monkeypatch.setattr("connectors.snowflake.discovery.list_tables", lambda schema=None: None)
    c = seeded_app["client"]

    resp = c.get("/api/admin/data-sources/snowflake/tables", headers=_auth(seeded_app["admin_token"]))

    assert resp.status_code == 400, resp.text
    assert "not configured" in resp.json()["detail"]


def test_endpoint_maps_driver_failure_to_502_not_empty(seeded_app, monkeypatch):
    """An empty listing would read as "the account has no tables" — the worse lie."""

    def _boom(schema=None):
        raise RuntimeError("ADBC driver not found")

    monkeypatch.setattr("connectors.snowflake.discovery.list_tables", _boom)
    c = seeded_app["client"]

    resp = c.get("/api/admin/data-sources/snowflake/tables", headers=_auth(seeded_app["admin_token"]))

    assert resp.status_code == 502, resp.text
    assert "ADBC driver not found" in resp.json()["detail"]


def test_endpoint_maps_allowlist_refusal_to_400(seeded_app, monkeypatch):
    """A host outside the egress allowlist is operator misconfiguration, not an
    upstream fault — 400, so the wizard does not offer a pointless retry."""

    def _refuse(schema=None):
        raise ValueError("host is not in AGNES_REMOTE_ATTACH_HOST_ALLOWLIST")

    monkeypatch.setattr("connectors.snowflake.discovery.list_tables", _refuse)
    c = seeded_app["client"]

    resp = c.get("/api/admin/data-sources/snowflake/tables", headers=_auth(seeded_app["admin_token"]))

    assert resp.status_code == 400, resp.text
    assert "ALLOWLIST" in resp.json()["detail"]


# ---- credential must not ride out on a driver error -----------------------


def test_attach_failure_does_not_leak_the_credential(monkeypatch):
    """A failing ATTACH must not carry the secret into the caller's message.

    `attach_snowflake` builds and EXECUTES `CREATE OR REPLACE SECRET … (PASSWORD
    '<token>')` / `PRIVATE_KEY $PK$…$PK$`. DuckDB's parser-class errors echo the
    offending statement text, so on a build whose extension does not recognise
    one of those options the raised error carries the Snowflake credential —
    and every caller forwards it somewhere durable: this endpoint into a 502
    `detail` and a server log line, and the extract build into
    `stats["errors"]` → `sync_state.error`, which the admin registry, /admin/sync
    and `agnes admin list-tables` all render unredacted.

    The Keboola listing endpoint already redacts before answering 502
    (`client._redact(exc)`); this pins the same guarantee at the Snowflake source,
    so every caller inherits it rather than each having to remember.
    """
    from unittest.mock import MagicMock

    from connectors.snowflake.attach import attach_snowflake

    token = "sUperSecretPassw0rd-not-in-any-message"

    conn = MagicMock()

    def _explode(sql, *a, **k):
        # Shape of a DuckDB parser error: it quotes the statement back.
        raise RuntimeError(f'Parser Error: syntax error at or near "PASSWORD"\nLINE 1: {sql}')

    conn.execute.side_effect = _explode

    with pytest.raises(Exception) as exc_info:
        attach_snowflake(
            conn,
            alias="sf",
            url="https://acct-123.snowflakecomputing.com?database=PROD&warehouse=WH&user=SVC",
            token=token,
        )

    msg = str(exc_info.value)
    assert token not in msg, msg
    assert "PASSWORD '" not in msg, msg
    # Still has to be actionable.
    assert "snowflake" in msg.lower(), msg


def test_attach_failure_does_not_leak_a_normalized_private_key(monkeypatch):
    """The key-pair arm, which the password test above cannot reach.

    `_create_snowflake_secret_sql` does not embed `token` — it embeds
    `_private_key_pem_and_passphrase(token, passphrase)`, which NORMALIZES the
    key to an unencrypted PKCS#8 PEM. Whenever the stored credential is PKCS#1,
    an encrypted key + passphrase, a JSON wrapper with escaped newlines, or a
    filesystem path (i.e. most real key-pair deployments), the embedded PEM is
    not a substring of `token`, so scrubbing `token` matches nothing.

    The only remaining guard was the `PRIVATE_KEY $PK$ … $PK$` regex, and that
    needs the CLOSING delimiter — which is exactly what a truncated DuckDB
    parser echo ("LINE 1: " + a prefix) does not have. The whole private key
    then rode out into the 502 body, the server log, and `sync_state.error`.
    """
    from unittest.mock import MagicMock

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from connectors.snowflake.attach import attach_snowflake, _private_key_pem_and_passphrase

    monkeypatch.delenv("AGNES_REMOTE_ATTACH_HOST_ALLOWLIST", raising=False)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pkcs1 = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    normalized, _ = _private_key_pem_and_passphrase(pkcs1)
    assert normalized != pkcs1, "precondition: the embedded PEM differs from the stored credential"
    material = normalized.strip().splitlines()[1]

    conn = MagicMock()
    conn.execute.side_effect = lambda sql, *a, **k: (_ for _ in ()).throw(
        # Truncated echo: the closing $PK$ never arrives.
        RuntimeError('Parser Error: syntax error at or near "PRIVATE_KEY_FILE"\nLINE 1: ' + sql[:900])
    )

    with pytest.raises(Exception) as exc_info:
        attach_snowflake(
            conn,
            alias="sf",
            url="https://acct-123.snowflakecomputing.com?database=PROD&warehouse=WH&user=SVC",
            token=pkcs1,
        )

    msg = str(exc_info.value)
    assert "-----BEGIN PRIVATE KEY-----" not in msg, msg[:400]
    assert material not in msg, msg[:400]
    assert "snowflake" in msg.lower(), msg


def test_attach_failure_does_not_leak_a_password_truncated_mid_value(monkeypatch):
    """The same open-delimiter hole on the PASSWORD arm. The substring pass
    usually saves it there (the password IS passed verbatim), so this pins the
    backstop itself rather than relying on that coincidence."""
    from unittest.mock import MagicMock

    from connectors.snowflake.attach import _scrub_secret_material

    truncated = "CREATE OR REPLACE SECRET s (TYPE snowflake, USER 'U', PASSWORD 'Tr0ub4dor-and"
    # Nothing passed as a known value: this is the backstop path, for a secret
    # the scrub site does not hold.
    out = _scrub_secret_material(truncated)
    assert "Tr0ub4dor" not in out, out


def test_the_snowflake_picker_prepares_its_status_lines_before_registering():
    """Per-row "registering…/✗ failed" is the only account of what happened,
    and with more than one schema every group renders collapsed
    (`openByDefault = schemas.length === 1`). Step 2's handler returns silently
    on zero successes on the assumption those statuses are visible — so a
    wholly-failed registration reads as a dead button unless the picker is
    prepared first. `registerSelected` already does this for the Keboola
    picker; `_registerSfRows` did not.
    """
    from pathlib import Path

    html = Path("app/web/templates/admin_data_sources.html").read_text()
    fn = html[html.index("async function _registerSfRows()") :]
    fn = fn[: fn.index("\n}\n")]
    assert "_setBucketOpen(group, true)" in fn, "checked groups must be opened before the status writes"
    assert "_filterPicker(" in fn, "an active filter would hide the rows being written to"
    assert "_syncPicker(" in fn, "counts must stay honest after the programmatic open"
