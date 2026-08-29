"""Keboola Metastore -> Apache Ossie document adapter.

`connectors/keboola/semantic_layer.py` flattens the Metastore's six object
types (`semantic-model`, `-dataset`, `-metric`, `-constraint`,
`-relationship`, `-glossary`) straight into `metric_definitions` /
`glossary_terms`, keeping only what those two flat tables have columns for.
This module composes the SAME six object types into a canonical Ossie
document instead — one document per `semantic-model`, so a project with more
than one model (already handled by the flat importer, see
`_sync_one_source`'s "EVERY model, not just the first" comment) keeps every
model as its own document rather than being collapsed into one.

An adapter's only job is to return documents as text (see
`src/semantic/adapters/__init__.py`); it never writes to `semantic_models`,
`metric_definitions`, `glossary_terms` or `column_metadata` itself.

Wire shapes below are the live-verified ones from
`docs/superpowers/specs/2026-07-15-keboola-semantic-layer-importer-design.md`
and `docs/superpowers/specs/2026-07-17-keboola-relationship-metrics-design.md`
— EXCEPT `semantic-model.sqlDialect`, which no prior spec or code path in
this repo has ever read or verified against a live project. It is used here
because composing a dialect-tagged expression requires *some* dialect, and
`semantic-model` is the only object type scoped to the whole model rather
than one dataset/metric — but it should be confirmed against a live
Metastore response before this adapter is relied on in production.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import yaml

from connectors.keboola.semantic_layer import parse_on_clause
from src.semantic.document_validation import SPEC_VERSION
from src.sql_ident import quote_ident

logger = logging.getLogger(__name__)

# The vendor name every custom_extensions entry this adapter writes is filed
# under (schema/osi-schema.json's `Vendor` def accepts any string).
_AGNES_VENDOR = "AGNES"

_ITEM_TYPES = (
    "semantic-dataset",
    "semantic-metric",
    "semantic-constraint",
    "semantic-relationship",
    "semantic-glossary",
)

# Keboola's own column-type vocabulary (verified: connectors/keboola/client.py
# KEBOOLA_TO_PYARROW_TYPES, used for Storage API columns) mapped onto Ossie's
# DataType enum. `semantic-dataset.fields[].type` has never itself been read
# by any existing code path, so this reuses the closest verified vocabulary
# rather than inventing a new one — not a confirmed 1:1 match. Anything not
# listed is omitted (`datatype` is optional; the schema's own guidance is to
# omit rather than guess).
_DATATYPE_MAP = {
    "STRING": "String",
    "VARCHAR": "String",
    "TEXT": "String",
    "INTEGER": "Integer",
    "BIGINT": "Integer",
    "NUMERIC": "Decimal",
    "DECIMAL": "Decimal",
    "FLOAT": "Float",
    "DOUBLE": "Float",
    "BOOLEAN": "Boolean",
    "DATE": "Date",
    "TIMESTAMP": "DateTime",
    "TIMESTAMP_NTZ": "DateTime",
    "TIMESTAMP_TZ": "DateTimeTz",
}

# `semantic-dataset.fields[].role` has no verified vocabulary either (see
# module docstring). Any role whose value contains "time", plus the literal
# "date", is treated as the temporal-role marker Ossie's `dimension.is_time`
# exists for; anything else leaves `is_time` unset (the schema's own
# datatype-based default then applies).
_TIME_ROLE_NEEDLE = "time"


def _custom_extension(payload: Dict[str, Any]) -> Dict[str, str]:
    return {"vendor_name": _AGNES_VENDOR, "data": json.dumps(payload)}


def _identity(item: Dict[str, Any]) -> Dict[str, Any]:
    """The upstream object's identity, for the AGNES extension.

    Nothing else in a composed document says which Metastore object an entry
    came from: names are the only handle, and a rename upstream is
    indistinguishable from a delete plus a create. This is the one fact that
    cannot be recovered later — every sync rewrites the document, so a
    document composed without it has lost it for good — which is why it lands
    now rather than with the write-back that will consume it.

    ``meta`` (the revision) is included only when upstream actually sends it;
    whether it rides along on the list endpoint or only on a detail fetch is
    not settled, and a fabricated null would be indistinguishable from a real
    empty revision.
    """
    identity: Dict[str, Any] = {}
    item_id = item.get("id")
    if item_id:
        identity["metastore_id"] = item_id
    meta = item.get("meta")
    if meta:
        identity["metastore_revision"] = meta
    return identity


def _resolve_dialect(raw: Optional[str]) -> str:
    """Normalize a Keboola project's declared SQL dialect into the tag every
    expression composed for its model carries.

    Missing entirely -> ``ANSI_SQL``: this mirrors the flat importer's
    pre-existing, implicit assumption (its composed SQL, e.g.
    ``SELECT SUM("amount") FROM "orders" AS t``, was always emitted with no
    dialect tracking at all).

    Declared but outside the vendored Ossie ``Dialect`` enum (``ANSI_SQL``,
    ``SNOWFLAKE``, ``MDX``, ``TABLEAU``, ``DATABRICKS``, ``MAQL``,
    ``BIGQUERY``) is passed through UPPERCASED as-is rather than silently
    substituted — Redshift, a real Keboola backend, has no Ossie counterpart
    in the pinned schema version, and relabeling a Redshift fragment as
    ``ANSI_SQL`` would let it be spliced into a DuckDB query as if it were
    portable SQL, exactly what ``src/semantic/dialect.py``'s resolution
    exists to prevent. The importer already treats a document that fails
    schema validation as ``status='invalid'`` (recorded, not fatal), so an
    unmappable dialect surfaces as a visible validation error instead of a
    silent wrong answer.
    """
    if raw is None or not str(raw).strip():
        return "ANSI_SQL"
    return str(raw).strip().upper()


def _resolve_datatype(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    return _DATATYPE_MAP.get(str(raw).strip().upper())


def _is_time_role(role: Optional[str]) -> bool:
    r = (role or "").strip().lower()
    return r == "date" or _TIME_ROLE_NEEDLE in r


def _compose_ai_context(ai_block: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    synonyms = list(ai_block.get("synonyms") or [])
    # AIContext.instructions is a single string; Keboola's `hints` and
    # `warnings` are both lists, so they are folded into one string rather
    # than dropped for lack of an array-typed slot.
    instruction_lines = list(ai_block.get("hints") or []) + list(ai_block.get("warnings") or [])
    out: Dict[str, Any] = {}
    if synonyms:
        out["synonyms"] = synonyms
    if instruction_lines:
        out["instructions"] = "\n".join(instruction_lines)
    return out or None


def _compose_field(field: Dict[str, Any], dialect: str) -> Dict[str, Any]:
    name = field.get("name") or ""
    out: Dict[str, Any] = {
        "name": name,
        # Field.expression is REQUIRED by the vendored schema, but a plain
        # Keboola column has no expression of its own — this synthesizes a
        # trivial pass-through (the quoted column name) tagged with the
        # model's dialect so the field is representable at all.
        "expression": {"dialects": [{"dialect": dialect, "expression": quote_ident(name)}]},
    }
    description = field.get("description")
    if description:
        out["description"] = description
    datatype = _resolve_datatype(field.get("type"))
    if datatype:
        out["datatype"] = datatype
    if _is_time_role(field.get("role")):
        out["dimension"] = {"is_time": True}
    return out


def _compose_dataset(item: Dict[str, Any], dialect: str) -> tuple[Dict[str, Any], str]:
    attrs = item.get("attributes") or {}
    table_id = attrs.get("tableId") or ""
    name = attrs.get("name") or table_id or "dataset"
    out: Dict[str, Any] = {"name": name, "source": table_id or name}

    description = attrs.get("description")
    if description:
        out["description"] = description

    primary_key = attrs.get("primaryKey") or []
    if primary_key:
        out["primary_key"] = list(primary_key)

    fields = attrs.get("fields") or []
    if fields:
        out["fields"] = [_compose_field(f, dialect) for f in fields]

    ai_block = attrs.get("ai") or {}
    ai_context = _compose_ai_context(ai_block)
    if ai_context:
        out["ai_context"] = ai_context

    # Neither `ai.keywords` nor `grain` has a first-class slot on the
    # vendored Dataset schema (`additionalProperties: false` rules out
    # adding either as a top-level key) — both ride custom_extensions rather
    # than being dropped. `grain` is a real, populated field the flat
    # importer surfaces today (`semantic-dataset.grain` -> `metric_definitions
    # .grain`); the Ossie schema simply has no dataset-level granularity
    # concept as of the pinned version.
    dataset_extension: Dict[str, Any] = _identity(item)
    keywords = list(ai_block.get("keywords") or [])
    if keywords:
        dataset_extension["keywords"] = keywords
    grain = attrs.get("grain")
    if grain:
        dataset_extension["grain"] = grain
    if dataset_extension:
        out["custom_extensions"] = [_custom_extension(dataset_extension)]

    return out, table_id


def _compose_relationship(item: Dict[str, Any], dataset_name_by_table_id: Dict[str, str], index: int) -> Dict[str, Any]:
    attrs = item.get("attributes") or {}
    name = attrs.get("name") or f"relationship_{index}"
    from_id = attrs.get("from") or ""
    to_id = attrs.get("to") or ""
    on = attrs.get("on") or ""

    parsed = parse_on_clause(on)
    if parsed is not None:
        _from_alias, from_col, _to_alias, to_col = parsed
        # NOTE: which side of the `on` equality belongs to the `from` table
        # vs. the `to` table is NOT determinable from the on-clause alone —
        # `connectors/keboola/semantic_layer.py::resolve_join_aliases` only
        # resolves it by cross-checking real column names against Agnes's
        # own table_registry/column_metadata, data this adapter deliberately
        # does not depend on (it must compose a document for a project that
        # hasn't registered any tables in Agnes yet). The first parsed column
        # is kept as `from_columns`, the second as `to_columns`, positionally
        # — this may attribute a column to the wrong side. The raw `on`
        # string rides along in `custom_extensions` so a consumer can correct
        # it without the original data being lost.
        from_columns, to_columns = [from_col], [to_col]
    else:
        # Doesn't match Keboola's live-verified on-clause shape. Ossie
        # requires a non-empty column list on both sides, so the raw string
        # stands in for it rather than the relationship being dropped.
        from_columns, to_columns = [on], [on]

    return {
        "name": name,
        "from": dataset_name_by_table_id.get(from_id, from_id),
        "to": dataset_name_by_table_id.get(to_id, to_id),
        "from_columns": from_columns,
        "to_columns": to_columns,
        # Carry the RAW tableIds (not just the dataset names used for from/to
        # above) plus the on-clause and type, losslessly. This is what lets the
        # flat-table projector rebuild the exact `relationship_lookup` the
        # legacy JOIN composer used — keyed by tableId, resolving the on-clause
        # against the registry — without having to reverse the name mapping or
        # re-parse anything. Composed here because the adapter is the only
        # place that still has the tableIds; consumed at projection time, where
        # the registry and column metadata needed to finish the JOIN live.
        "custom_extensions": [
            _custom_extension(
                {"on": on, "type": attrs.get("type"), "from_table": from_id, "to_table": to_id, **_identity(item)}
            )
        ],
    }


def _compose_metric(item: Dict[str, Any], dialect: str) -> Optional[Dict[str, Any]]:
    attrs = item.get("attributes") or {}
    name = attrs.get("name")
    if not name:
        return None
    expression = attrs.get("sql") or ""
    out: Dict[str, Any] = {
        "name": name,
        "expression": {"dialects": [{"dialect": dialect, "expression": expression}]},
    }
    description = attrs.get("description")
    if description:
        out["description"] = description
    metric_extension: Dict[str, Any] = _identity(item)
    dataset_table_id = attrs.get("dataset")
    if dataset_table_id:
        metric_extension["dataset"] = dataset_table_id
    if metric_extension:
        out["custom_extensions"] = [_custom_extension(metric_extension)]
    return out


def _compose_constraint(item: Dict[str, Any]) -> Dict[str, Any]:
    attrs = item.get("attributes") or {}
    return {
        "name": attrs.get("name"),
        "constraint_type": attrs.get("constraintType"),
        "rule": attrs.get("rule"),
        "metrics": list(attrs.get("metrics") or []),
        "severity": attrs.get("severity"),
        **_identity(item),
    }


def _compose_glossary_entry(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    attrs = item.get("attributes") or {}
    term = attrs.get("term")
    if not term:
        return None
    return {
        "term": term,
        "definition": attrs.get("definition"),
        "see_also": list(attrs.get("seeAlso") or []),
        **_identity(item),
    }


def compose_document(model_item: Dict[str, Any], model_items: Dict[str, List[dict]]) -> Optional[str]:
    """Compose one model's Metastore objects into an Ossie document (YAML
    text), or ``None`` when the model has no datasets at all.

    ``datasets`` has ``minItems: 1`` in the vendored schema — a model that
    hasn't registered a single ``semantic-dataset`` yet has nothing valid to
    compose, so it is skipped here rather than shipping a document the
    schema is guaranteed to reject.
    """
    attrs = model_item.get("attributes") or {}
    name = attrs.get("name") or model_item.get("id") or ""
    dialect = _resolve_dialect(attrs.get("sqlDialect"))

    dataset_name_by_table_id: Dict[str, str] = {}
    datasets: List[Dict[str, Any]] = []
    for dataset_item in model_items.get("semantic-dataset", []):
        dataset, table_id = _compose_dataset(dataset_item, dialect)
        datasets.append(dataset)
        if table_id:
            dataset_name_by_table_id[table_id] = dataset["name"]

    if not datasets:
        logger.warning(
            "Keboola Ossie adapter: model %r has no semantic-dataset entries; skipping "
            "(the vendored schema requires at least one dataset per model)",
            name,
        )
        return None

    semantic_model: Dict[str, Any] = {"name": name, "datasets": datasets}

    description = attrs.get("description")
    if description:
        semantic_model["description"] = description

    relationships = [
        _compose_relationship(item, dataset_name_by_table_id, index)
        for index, item in enumerate(model_items.get("semantic-relationship", []), start=1)
    ]
    if relationships:
        semantic_model["relationships"] = relationships

    metrics = [m for m in (_compose_metric(item, dialect) for item in model_items.get("semantic-metric", [])) if m]
    if metrics:
        semantic_model["metrics"] = metrics

    # Neither `semantic-constraint` (project-wide rules keyed by metric name,
    # not scoped to one dataset) nor `semantic-glossary` (conceptual
    # definitions with no per-metric or per-dataset home) has a first-class
    # slot in the Ossie schema — both ride the model's own custom_extensions
    # rather than being dropped, same as `ai.keywords` rides a dataset's.
    agnes_payload: Dict[str, Any] = _identity(model_item)
    constraints = [_compose_constraint(item) for item in model_items.get("semantic-constraint", [])]
    if constraints:
        agnes_payload["constraints"] = constraints
    glossary = [g for g in (_compose_glossary_entry(item) for item in model_items.get("semantic-glossary", [])) if g]
    if glossary:
        agnes_payload["glossary"] = glossary
    if agnes_payload:
        semantic_model["custom_extensions"] = [_custom_extension(agnes_payload)]

    document = {"version": SPEC_VERSION, "semantic_model": [semantic_model]}
    return yaml.safe_dump(document, sort_keys=False)


class KeboolaMetastoreAdapter:
    """Fetches a Keboola project's Metastore and composes one Ossie document
    per ``semantic-model``.

    ``config`` carries either the credentials directly (``{"url", "token"}``
    — what ``sync_semantic_layer`` passes for the Keboola-login-triggered
    sync) or, for a registered ``semantic_sources`` row, only SCOPE:
    ``{"connection_id": ...}`` or ``{"legacy_credentials": true}``, resolved
    per sync by ``connectors.keboola.semantic_layer
    .resolve_semantic_source_credentials``. A stored source row must never
    become a second place a Storage token is kept, which is the same rule the
    Snowflake and Databricks adapters follow.

    Resolving from scope also runs the preflight the legacy sync ran before
    touching a row — master-token check and project-binding check — so a
    misconfigured connection fails the sync instead of importing another
    project's semantic layer under this one's provenance.

    This adapter owns its own Metastore fetch (a fresh ``MetastoreClient``,
    independent of any fetch ``connectors/keboola/semantic_layer.py`` has
    already done for the flat-table sync) so it stays a self-contained
    "hand it connection config, get documents back" adapter like every other
    one — the cost is one extra round-trip per sync.
    """

    def extract(self, config: Dict[str, Any]) -> List[str]:
        # Imported locally, not at module level: every existing Keboola
        # semantic-layer test patches `connectors.keboola.metastore_client
        # .MetastoreClient` at ITS OWN defining module (see
        # `_sync_one_source`, which imports it the same way for the same
        # reason) — a module-level import here would bind an independent
        # reference at import time that a test's `patch()` on the defining
        # module can never reach, silently making this adapter fire a real
        # HTTP request in what every caller believes is a fully mocked test.
        from connectors.keboola.metastore_client import MetastoreClient

        url = config.get("url")
        token = config.get("token")
        if not url or not token:
            if not (config.get("connection_id") or config.get("legacy_credentials")):
                raise ValueError(
                    "KeboolaMetastoreAdapter requires config['url'] + config['token'], or a registered "
                    "source's scope (config['connection_id'] or config['legacy_credentials'])"
                )
            # Local import for the same reason as MetastoreClient above, and
            # because connectors.keboola.semantic_layer imports this module's
            # adapter in turn.
            from connectors.keboola.semantic_layer import resolve_semantic_source_credentials

            url, token = resolve_semantic_source_credentials(config)

        metastore = MetastoreClient(url=url, token=token)
        models = metastore.list_items("semantic-model")
        models_by_id = {m["id"]: m for m in models if m.get("id")}
        # Sorted, not API order — mirrors `_sync_one_source`'s own rationale:
        # Metastore list order is not a documented guarantee, so a flapping
        # order would silently reorder the returned documents between runs.
        model_uuids = sorted(models_by_id)

        documents: List[str] = []
        for model_uuid in model_uuids:
            model_items = {t: metastore.list_items(t, model_uuid) for t in _ITEM_TYPES}
            text = compose_document(models_by_id[model_uuid], model_items)
            if text is not None:
                documents.append(text)
        return documents
