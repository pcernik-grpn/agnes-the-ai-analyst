"""Snowflake semantic views -> Apache Ossie document adapter.

Snowflake semantic views are first-class catalog objects (``CREATE SEMANTIC
VIEW``) declaring logical tables, relationships, dimensions, facts and
metrics. This module reads them through the ``snowflake`` DuckDB community
extension's pass-through function — ``snowflake_query('<sql>', '<secret>')``
— and composes one Ossie document per semantic view.

Why pass-through rather than the ATTACHed catalog the rest of this connector
uses: ``SHOW SEMANTIC VIEWS`` / ``DESCRIBE SEMANTIC VIEW`` are Snowflake DDL
commands, not table scans, and everything else in this connector runs
DuckDB-flavour SQL where only the scan is pushed down (see
``connectors/snowflake/extractor.py``). The credential path is unchanged:
``attach_snowflake`` still gates egress on the host allowlist and creates the
same SECRET, and this module only adds a second way to use it.

Expressions are tagged ``SNOWFLAKE``, which is deliberate and load-bearing:
``src/semantic/dialect.py`` will refuse to splice a SNOWFLAKE fragment into a
local DuckDB query. Importing these documents makes Snowflake's definitions
*readable* (catalog, metric SQL, lineage, AI instructions); it does not make
them *runnable* locally, and relabelling them ANSI_SQL to paper over that
would hand the analyst a query that parses and silently means something else.

An adapter's only job is to return documents as text (see
``src/semantic/adapters/__init__.py``); it never writes to ``semantic_models``,
``metric_definitions``, ``glossary_terms`` or ``column_metadata`` itself.

Wire shapes are the documented contract of ``SHOW SEMANTIC VIEWS`` and
``DESCRIBE SEMANTIC VIEW`` (object_kind / object_name / parent_entity /
property / property_value). They have NOT been verified against a live
Snowflake account from this repo — the composer is pure and fully tested, the
fetch half is pinned to the documented columns by name rather than position so
an upstream column insertion cannot silently shift the mapping.
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

import yaml

from src.identifier_validation import validate_quoted_identifier
from src.orchestrator_security import escape_sql_string_literal
from src.semantic.document_validation import SPEC_VERSION
from src.sql_ident import quote_ident

logger = logging.getLogger(__name__)

_AGNES_VENDOR = "AGNES"

# Every expression composed here is Snowflake-flavour. See module docstring.
_DIALECT = "SNOWFLAKE"

_DESCRIBE_COLUMNS = ("object_kind", "object_name", "parent_entity", "property", "property_value")

_FIELD_KINDS = ("DIMENSION", "FACT")
_METRIC_KINDS = ("METRIC", "DERIVED_METRIC")

# "" is the semantic view itself (object_kind NULL). EXTENSION is undocumented
# but real — see `_parse_extension`.
_KNOWN_KINDS = frozenset(
    ("", "TABLE", "RELATIONSHIP", "EXTENSION", "CUSTOM_INSTRUCTIONS", "AI_VERIFIED_QUERY")
    + _FIELD_KINDS
    + _METRIC_KINDS
)

# Snowflake's type vocabulary mapped onto Ossie's DataType enum. NUMBER is
# resolved by scale (Snowflake's bare NUMBER is NUMBER(38,0), an integer);
# anything unlisted is omitted rather than guessed, per the schema's own
# guidance that `datatype` is optional.
_DATATYPE_MAP = {
    "VARCHAR": "String",
    "CHAR": "String",
    "CHARACTER": "String",
    "STRING": "String",
    "TEXT": "String",
    "INT": "Integer",
    "INTEGER": "Integer",
    "BIGINT": "Integer",
    "SMALLINT": "Integer",
    "TINYINT": "Integer",
    "BYTEINT": "Integer",
    "FLOAT": "Float",
    "FLOAT4": "Float",
    "FLOAT8": "Float",
    "DOUBLE": "Float",
    "DOUBLE PRECISION": "Float",
    "REAL": "Float",
    "BOOLEAN": "Boolean",
    "DATE": "Date",
    "TIME": "Time",
    "DATETIME": "DateTime",
    "TIMESTAMP": "DateTime",
    "TIMESTAMP_NTZ": "DateTime",
    "TIMESTAMP_LTZ": "DateTimeTz",
    "TIMESTAMP_TZ": "DateTimeTz",
    "BINARY": "Opaque",
    "VARBINARY": "Opaque",
    "VARIANT": "Opaque",
    "OBJECT": "Opaque",
    "ARRAY": "Opaque",
    "GEOGRAPHY": "Opaque",
    "GEOMETRY": "Opaque",
}

_NUMERIC_NAMES = ("NUMBER", "DECIMAL", "NUMERIC")

# Linear-time: one bounded group, no nesting or alternation over the same span.
_TYPE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_ ]*)(?:\(([^)]*)\))?$")


def _custom_extension(payload: Dict[str, Any]) -> Dict[str, str]:
    return {"vendor_name": _AGNES_VENDOR, "data": json.dumps(payload)}


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _split_columns(raw: Any) -> List[str]:
    """Split a key list — ``ORDER_ID``, ``(A, B)``, ``[A, B]`` — into names.

    Snowflake renders composite keys with the parenthesised form; a single
    key comes through bare. Both are accepted rather than assuming one.
    """
    text = _text(raw).strip("()[] ")
    return [part.strip().strip('"').strip() for part in text.split(",") if part.strip().strip('"').strip()]


def _resolve_datatype(raw: Any) -> Optional[str]:
    text = _text(raw).upper()
    if not text:
        return None
    match = _TYPE_RE.match(text)
    if not match:
        return None
    base = match.group(1).strip()
    args = match.group(2)
    if base in _NUMERIC_NAMES:
        scale = 0
        if args:
            parts = [p.strip() for p in args.split(",")]
            if len(parts) > 1 and parts[1].isdigit():
                scale = int(parts[1])
        return "Decimal" if scale > 0 else "Integer"
    return _DATATYPE_MAP.get(base)


def rows_to_dicts(rows: Iterable[Sequence[Any]], description: Sequence[Sequence[Any]]) -> List[Dict[str, Any]]:
    """Key raw cursor rows by COLUMN NAME, not position.

    DuckDB hands back whatever column order Snowflake sent. Reading by name
    means a column inserted upstream shifts nothing; reading by position
    would silently remap every value one slot over.
    """
    names = [str(col[0]).lower() for col in description or []]
    out: List[Dict[str, Any]] = []
    for row in rows:
        by_name = dict(zip(names, row))
        out.append({key: by_name.get(key) for key in _DESCRIBE_COLUMNS})
    return out


def _require_ident(value: str, context: str) -> str:
    value = _text(value)
    if not value or not validate_quoted_identifier(value, context):
        raise ValueError(f"snowflake semantic layer: unsafe {context} {value!r}")
    return value


def build_show_sql(*, database: str, schema: Optional[str], like: Optional[str]) -> str:
    """``SHOW SEMANTIC VIEWS [LIKE ...] IN {DATABASE|SCHEMA} ...``.

    The LIKE pattern is operator-supplied free text, so it is escaped as a
    string literal; the scope identifiers are validated and quoted.
    """
    database = _require_ident(database, "snowflake database")
    sql = "SHOW SEMANTIC VIEWS"
    if like:
        sql += f" LIKE '{escape_sql_string_literal(str(like))}'"
    if schema:
        schema = _require_ident(schema, "snowflake schema")
        return f"{sql} IN SCHEMA {quote_ident(database)}.{quote_ident(schema)}"
    return f"{sql} IN DATABASE {quote_ident(database)}"


def build_describe_sql(view: Mapping[str, Any]) -> str:
    database = _require_ident(view.get("database_name"), "snowflake database")
    schema = _require_ident(view.get("schema_name"), "snowflake schema")
    name = _require_ident(view.get("name"), "snowflake semantic view")
    return f"DESCRIBE SEMANTIC VIEW {quote_ident(database)}.{quote_ident(schema)}.{quote_ident(name)}"


def _fqn(view: Mapping[str, Any]) -> str:
    parts = [_text(view.get("database_name")), _text(view.get("schema_name")), _text(view.get("name"))]
    return ".".join(p for p in parts if p)


def _group(rows: Iterable[Mapping[str, Any]], *, context: str = "") -> Dict[tuple, Dict[str, Any]]:
    """Collapse the property-per-row shape into one property dict per object.

    Two rows carrying the same property with DIFFERENT values would otherwise
    resolve last-write-wins, and DESCRIBE row order is not a contract — so the
    winner would not even be stable between runs. Keep the first and say so.
    """
    grouped: Dict[tuple, Dict[str, Any]] = {}
    for row in rows:
        key = (_text(row.get("object_kind")).upper(), _text(row.get("object_name")), _text(row.get("parent_entity")))
        prop = _text(row.get("property")).upper()
        value = row.get("property_value")
        bucket = grouped.setdefault(key, {})
        if prop in bucket:
            if bucket[prop] != value:
                logger.warning(
                    "Snowflake semantic adapter: %s%r declares %s twice with different values; keeping the first",
                    f"{context}: " if context else "",
                    key[1] or key[0],
                    prop,
                )
            continue
        bucket[prop] = value
    return grouped


def _parse_extension(raw: Any) -> Optional[Dict[str, Any]]:
    """Parse an ``EXTENSION`` row's JSON payload, or ``None``.

    ``EXTENSION`` is not in the documented object_kind vocabulary but a live
    account emits one (name ``CA``, Cortex Analyst) carrying the declared time
    dimensions and every relationship's join_type — data that appears nowhere
    else in the DESCRIBE output. Upstream JSON is not something to stake a
    whole document on, so a malformed payload costs its annotations and
    nothing more.
    """
    text = _text(raw)
    if not text:
        return None
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        logger.warning("Snowflake semantic adapter: EXTENSION payload is not valid JSON; annotations dropped")
        return None
    return payload if isinstance(payload, dict) else None


def _compose_dataset(name: str, props: Mapping[str, Any]) -> Dict[str, Any]:
    base = [
        _text(props.get("BASE_TABLE_DATABASE_NAME")),
        _text(props.get("BASE_TABLE_SCHEMA_NAME")),
        _text(props.get("BASE_TABLE_NAME")),
    ]
    source = ".".join(p for p in base if p)
    out: Dict[str, Any] = {"name": name, "source": source or name}

    comment = _text(props.get("COMMENT"))
    if comment:
        out["description"] = comment

    primary_key = _split_columns(props.get("PRIMARY_KEY"))
    if primary_key:
        out["primary_key"] = primary_key

    synonyms = _split_columns(props.get("SYNONYMS"))
    if synonyms:
        out["ai_context"] = {"synonyms": synonyms}

    extension: Dict[str, Any] = {}
    definition = _text(props.get("DEFINITION"))
    if definition:
        # No first-class slot on the Ossie Dataset (additionalProperties:
        # false) — carried rather than dropped.
        extension["definition"] = definition
    if extension:
        out["custom_extensions"] = [_custom_extension(extension)]
    return out


def _compose_field(name: str, kind: str, props: Mapping[str, Any], *, is_time: bool = False) -> Dict[str, Any]:
    expression = _text(props.get("EXPRESSION")) or quote_ident(name)
    out: Dict[str, Any] = {
        "name": name,
        "expression": {"dialects": [{"dialect": _DIALECT, "expression": expression}]},
    }
    comment = _text(props.get("COMMENT"))
    if comment:
        out["description"] = comment
    datatype = _resolve_datatype(props.get("DATA_TYPE"))
    if datatype:
        out["datatype"] = datatype

    # `object_kind` is the fact/dimension distinction, which Ossie's Field has
    # no slot for and which a consumer needs to know an aggregate from an
    # attribute. `access_modifier` is Snowflake's PRIVATE marker: a private
    # fact is queryable only through the metrics built on it, so dropping the
    # label would present it as ordinary public surface.
    if is_time:
        # Snowflake declared this a time dimension. The schema's own default
        # infers `is_time` from a temporal datatype, which is not the same
        # thing: a year-grain Integer is a time dimension too, and only the
        # declaration says so.
        out["dimension"] = {"is_time": True}

    extension: Dict[str, Any] = {"object_kind": kind}
    access = _text(props.get("ACCESS_MODIFIER"))
    if access and access.upper() != "PUBLIC":
        # PUBLIC is the default and a live view stamps it on every single
        # field; restating it there would bury the PRIVATE ones that matter.
        extension["access_modifier"] = access
    out["custom_extensions"] = [_custom_extension(extension)]
    return out


def _compose_metric(name: str, kind: str, table: str, props: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    expression = _text(props.get("EXPRESSION"))
    if not expression:
        # Expression is required by the schema and there is nothing sane to
        # synthesize for a metric — a quoted name would be a plain column.
        logger.warning("Snowflake semantic adapter: metric %r has no EXPRESSION; skipping", name)
        return None
    out: Dict[str, Any] = {
        "name": name,
        "expression": {"dialects": [{"dialect": _DIALECT, "expression": expression}]},
    }
    comment = _text(props.get("COMMENT"))
    if comment:
        out["description"] = comment
    datatype = _resolve_datatype(props.get("DATA_TYPE"))
    if datatype:
        out["datatype"] = datatype

    extension: Dict[str, Any] = {"object_kind": kind}
    if table:
        extension["table"] = table
    access = _text(props.get("ACCESS_MODIFIER"))
    if access and access.upper() != "PUBLIC":
        extension["access_modifier"] = access
    out["custom_extensions"] = [_custom_extension(extension)]
    return out


def _compose_relationship(
    name: str, props: Mapping[str, Any], index: int, *, join_type: str = ""
) -> Optional[Dict[str, Any]]:
    from_table = _text(props.get("TABLE"))
    to_table = _text(props.get("REF_TABLE"))
    from_columns = _split_columns(props.get("FOREIGN_KEY"))
    to_columns = _split_columns(props.get("REF_KEY"))
    if not (from_table and to_table and from_columns and to_columns):
        # All four are required by the Ossie schema; a partial relationship
        # would fail validation for the whole document.
        logger.warning(
            "Snowflake semantic adapter: relationship %r is missing TABLE/REF_TABLE/FOREIGN_KEY/REF_KEY; skipping",
            name,
        )
        return None
    out: Dict[str, Any] = {
        "name": name or f"relationship_{index}",
        "from": from_table,
        "to": to_table,
        "from_columns": from_columns,
        "to_columns": to_columns,
    }
    if join_type:
        # Ossie's Relationship has no join-type slot, and DESCRIBE carries this
        # nowhere else — an inner join and a left join are different questions,
        # so it rides custom_extensions rather than being dropped.
        out["custom_extensions"] = [_custom_extension({"join_type": join_type})]
    return out


def compose_document(view: Mapping[str, Any], rows: Iterable[Mapping[str, Any]]) -> Optional[str]:
    """Compose one semantic view's DESCRIBE rows into an Ossie document.

    Returns ``None`` when the view declares no logical tables: Ossie requires
    at least one dataset, so emitting anything here would guarantee a
    validation failure the operator then has to diagnose.
    """
    fqn = _fqn(view)
    grouped = _group(rows, context=fqn)

    # Extensions first, in their own pass: they annotate fields and
    # relationships composed below, and DESCRIBE row order is not a contract.
    extensions: Dict[str, Any] = {}
    for (kind, name, _parent), props in grouped.items():
        if kind == "EXTENSION":
            payload = _parse_extension(props.get("VALUE"))
            if payload is not None:
                extensions[name or f"extension_{len(extensions) + 1}"] = payload

    time_dimensions: Dict[str, set] = {}
    join_types: Dict[str, str] = {}
    for payload in extensions.values():
        for table in payload.get("tables") or []:
            if not isinstance(table, dict):
                continue
            names = {
                _text(d.get("name"))
                for d in (table.get("time_dimensions") or [])
                if isinstance(d, dict) and d.get("name")
            }
            if names:
                time_dimensions.setdefault(_text(table.get("name")), set()).update(names)
        for relationship in payload.get("relationships") or []:
            if isinstance(relationship, dict) and relationship.get("name") and relationship.get("join_type"):
                join_types[_text(relationship["name"])] = _text(relationship["join_type"])

    datasets: Dict[str, Dict[str, Any]] = {}
    fields_by_table: Dict[str, List[Dict[str, Any]]] = {}
    metrics: List[Dict[str, Any]] = []
    relationships: List[Dict[str, Any]] = []
    view_comment = ""
    custom_instructions: Dict[str, Any] = {}
    verified_queries: Dict[str, Dict[str, Any]] = {}

    for (kind, name, parent), props in grouped.items():
        if kind not in _KNOWN_KINDS:
            # A live account already returned one object_kind the SQL
            # reference does not document (EXTENSION). The next one must leave
            # a trail rather than vanishing into a document that looks whole.
            logger.warning(
                "Snowflake semantic adapter: %r declares unrecognized object_kind %r (%r); dropped",
                fqn,
                kind,
                name or parent or "",
            )
            continue
        if kind == "TABLE" and name:
            datasets[name] = _compose_dataset(name, props)
        elif kind in _FIELD_KINDS and name:
            table = parent or _text(props.get("TABLE"))
            fields_by_table.setdefault(table, []).append(
                _compose_field(name, kind, props, is_time=name in time_dimensions.get(table, ()))
            )
        elif kind in _METRIC_KINDS and name:
            metric = _compose_metric(name, kind, parent or _text(props.get("TABLE")), props)
            if metric:
                metrics.append(metric)
        elif kind == "RELATIONSHIP" and name:
            relationship = _compose_relationship(
                name, props, len(relationships) + 1, join_type=join_types.get(name, "")
            )
            if relationship:
                relationships.append(relationship)
        elif kind == "EXTENSION":
            continue  # already collected above
        elif kind == "CUSTOM_INSTRUCTIONS":
            custom_instructions.update({k: v for k, v in props.items() if v is not None})
        elif kind == "AI_VERIFIED_QUERY":
            verified_queries[name or f"query_{len(verified_queries) + 1}"] = {
                k: v for k, v in props.items() if v is not None
            }
        elif not kind:
            view_comment = _text(props.get("COMMENT")) or view_comment
        else:
            # A recognized kind with no object_name has nowhere to land.
            logger.warning("Snowflake semantic adapter: %r has a %s row with no object_name; dropped", fqn, kind)

    if not datasets:
        logger.warning(
            "Snowflake semantic adapter: semantic view %r declares no logical tables; skipping "
            "(the vendored schema requires at least one dataset per model)",
            fqn,
        )
        return None

    for table, fields in fields_by_table.items():
        if table in datasets:
            datasets[table]["fields"] = fields
        else:
            # A dimension whose parent table is absent from the DESCRIBE
            # output has nowhere to live; say so rather than dropping it in
            # silence.
            logger.warning(
                "Snowflake semantic adapter: %r declares %d field(s) on unknown logical table %r; dropped",
                fqn,
                len(fields),
                table,
            )

    # Fully qualified, not the bare view name: the importer keys storage on the
    # model name and collapses duplicates, so two same-named views in different
    # schemas would silently overwrite each other.
    semantic_model: Dict[str, Any] = {"name": fqn, "datasets": list(datasets.values())}

    description = view_comment or _text(view.get("comment"))
    if description:
        semantic_model["description"] = description
    if relationships:
        semantic_model["relationships"] = relationships
    if metrics:
        semantic_model["metrics"] = metrics
    if custom_instructions:
        # Snowflake's own AI instructions for this view — the closest thing
        # upstream has to `ai_context`, kept verbatim.
        semantic_model["ai_context"] = {"instructions": "\n".join(_text(v) for v in custom_instructions.values())}

    extension: Dict[str, Any] = {"semantic_view": fqn}
    if custom_instructions:
        extension["custom_instructions"] = custom_instructions
    if verified_queries:
        extension["ai_verified_queries"] = verified_queries
    if extensions:
        # Carried whole, not just the two keys read above: this is the only
        # copy, and the payload's shape is upstream's to change.
        extension["extensions"] = extensions
    semantic_model["custom_extensions"] = [_custom_extension(extension)]

    document = {"version": SPEC_VERSION, "semantic_model": [semantic_model]}
    return yaml.safe_dump(document, sort_keys=False)


class SnowflakeSemanticAdapter:
    """Reads Snowflake semantic views and composes one Ossie document each.

    ``config`` carries only SCOPE — ``{"database", "schema", "like"}``, all
    optional. Credentials are never taken from it: they resolve from the
    instance's Snowflake connection exactly as every other Snowflake code path
    does, so a semantic source row never becomes a second place a warehouse
    credential is stored.
    """

    def unconfigured_reason(self, config: Dict[str, Any]) -> Optional[str]:
        """The optional pre-flight hook (``src/semantic/adapters/__init__.py``):
        ``None`` when this instance has a Snowflake account to read, a reason
        when it does not.

        Not a hypothetical twin of the Databricks one: the
        /admin/data-sources wizard registers a ``snowflake_semantic`` source
        the moment an admin connects Snowflake with the opt-in ticked, so a
        later deconfiguration (credentials rotated out, connection removed)
        leaves a source that can never sync again — importing it would raise
        :meth:`extract`'s "not configured" on every sweep, forever. The sweep
        asks this first and skips the row instead, without touching it.

        Deliberately the same zero-argument ``resolve_snowflake_settings()``
        call :meth:`extract` makes, so this answers "yes" exactly when that
        one would raise — a pre-check that resolved settings differently
        could skip a source that would have synced, or let one through that
        cannot.
        """
        # Imported at call time for the same reason `extract` does it below.
        from connectors.snowflake.settings import resolve_snowflake_settings

        if resolve_snowflake_settings():
            return None
        return (
            "Snowflake is not configured on this instance (a registered Snowflake connection — "
            "Admin → Data sources — or the legacy data_source.snowflake.* config, plus its "
            "SNOWFLAKE_PASSWORD / private-key env or vault secret); skipping this source until "
            "it is configured again"
        )

    def extract(self, config: Dict[str, Any]) -> List[str]:
        # Imported at call time, not module scope, so a test patching the
        # defining module reaches this lookup (same reason as the Keboola
        # adapter's local import of MetastoreClient).
        from connectors.snowflake.settings import resolve_snowflake_settings

        settings = resolve_snowflake_settings()
        if not settings:
            # Still fatal HERE: a manual `POST /semantic-sources/{id}/sync` an
            # admin asked for must say why it did nothing, and the pre-flight
            # hook above is only consulted by the scheduled sweep.
            raise RuntimeError(
                "Snowflake is not configured (data_source.snowflake.* + SNOWFLAKE_PASSWORD "
                "env/vault secret); refusing to sync semantic views"
            )

        database = _text(config.get("database")) or _text(settings.get("database"))
        schema = _text(config.get("schema")) or None
        like = config.get("like") or None

        show_sql = build_show_sql(database=database, schema=schema, like=like)

        with tempfile.TemporaryDirectory(prefix="agnes-sf-semantic-") as tmp:
            # `_connect` opens the scratch DuckDB BEFORE it loads the
            # extension and attaches, and either of those can raise — a
            # disallowed host, a bad key, a missing driver. Connecting outside
            # the try would leak that handle and leave the TemporaryDirectory
            # deleting the file out from under it. Same shape as
            # `connectors/snowflake/extractor.py::materialize_query`.
            conn = None
            try:
                conn = self._connect(Path(tmp) / "scratch.duckdb", settings)
                views = self._pass_through(conn, show_sql)
                documents: List[str] = []
                for view in views:
                    try:
                        describe_sql = build_describe_sql(view)
                    except ValueError as exc:
                        logger.warning("Snowflake semantic adapter: %s; skipping view", exc)
                        continue
                    rows = rows_to_dicts(*self._pass_through_raw(conn, describe_sql))
                    text = compose_document(view, rows)
                    if text is not None:
                        documents.append(text)
                return documents
            finally:
                if conn is not None:
                    conn.close()

    def _connect(self, path: Path, settings: Dict[str, Any]):
        from connectors.snowflake.attach import (
            SF_ALIAS,
            SF_EXTENSION,
            attach_snowflake,
            build_remote_attach_url,
            install_snowflake_adbc_driver,
        )
        from src.duckdb_conn import _open_duckdb

        url = build_remote_attach_url(
            settings["account"],
            settings["database"],
            settings["warehouse"],
            settings["user"],
            settings.get("role") or "",
        )
        conn = _open_duckdb(str(path), read_only=False)
        try:
            install_snowflake_adbc_driver()
            conn.execute(f"INSTALL {SF_EXTENSION} FROM community")
            conn.execute(f"LOAD {SF_EXTENSION}")
            # attach_snowflake owns the host-allowlist gate and the SECRET; the
            # pass-through calls below reference that same SECRET by name.
            attach_snowflake(
                conn,
                alias=SF_ALIAS,
                url=url,
                token=settings.get("password") or settings.get("private_key"),
                passphrase=settings.get("private_key_passphrase"),
            )
        except Exception:
            # The handle exists only in this frame until it is returned, so the
            # caller's `finally` cannot reach it — a disallowed host or a
            # missing driver would leak it on every scheduled sync attempt.
            conn.close()
            raise
        return conn

    def _pass_through_raw(self, conn, sql: str):
        from connectors.snowflake.attach import SF_ALIAS

        secret = f"sf_secret_{SF_ALIAS}"
        cursor = conn.execute(f"SELECT * FROM snowflake_query('{escape_sql_string_literal(sql)}', '{secret}')")
        return cursor.fetchall(), cursor.description

    def _pass_through(self, conn, sql: str) -> List[Dict[str, Any]]:
        rows, description = self._pass_through_raw(conn, sql)
        names = [str(col[0]).lower() for col in description or []]
        return [dict(zip(names, row)) for row in rows]
