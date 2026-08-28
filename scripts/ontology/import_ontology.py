#!/usr/bin/env python3
"""Translate a producer's ontology.yaml into an Agnes semantic-model
(Apache Ossie) document -- fact-graph build-order step 1
(docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md
Sec 11 / Sec 16). The same content this script emits IS the arm-A3 seed
pack (Sec 14): node/edge types translate structurally, guidance that has no
Ossie field folds into ai_context, so nothing the ontology says is lost --
only relocated, and every relocation is named in the leftover report.

This module is a pure TRANSLATION: it knows nothing about any particular
ontology's vocabulary. Every entity/relationship/attribute name it emits
comes from the input document; the only names literal in this file's own
source are structural conventions of the ontology.yaml format itself
(node_types, edge_types, attrs, parent, synonyms, ...), never a customer's
node/edge/attribute values.

Mapping (spec Sec 11):
  - node_types.TYPE          -> datasets      (attrs -> fields)
  - edge_types.TYPE          -> relationships
  - a string attribute literally named parent (a hierarchy encoded as a
    plain value rather than a structural reference) -> an additional
    self-relationship, reported for human confirmation (the skill's
    "structures the target format expresses differently" case)
  - a relationship whose src or dst is the wildcard marker -> emitted as-is
    (the schema does not constrain from/to to a known dataset name) and
    reported for human confirmation -- it cannot resolve to one concrete
    dataset without a producer-specific rule this script must not encode
  - TYPE.synonyms             -> glossary entries (model custom_extensions)
  - every other top-level key (purpose, conventions, change_policy,
    out_of_scope, serialization, ...) -> folded into the model's
    ai_context.instructions, verbatim, and named in the leftover report

The Ossie schema requires an expression on every field and from_columns /
to_columns on every relationship; the ontology format has no equivalent
concept (attributes are typeless key/value, edges reference an opaque node
id). This script synthesizes the schema-required minimum -- a quoted
pass-through expression for fields, an id-keyed join for relationships --
the same mechanical placeholder pattern the Keboola/Snowflake Ossie adapters
use for the same reason (see connectors/keboola/semantic_ossie.py). It is
not a claim that the data is queryable yet: build-order step 2 (the
Postgres facts/edges schema) is what makes that true.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.semantic.document_validation import SPEC_VERSION, validate_document  # noqa: E402
from src.sql_ident import quote_ident  # noqa: E402

_AGNES_VENDOR = "AGNES"

_DIALECT = "ANSI_SQL"

_DATATYPE_MAP = dict(
    string="String",
    number="Decimal",
    integer="Integer",
    boolean="Boolean",
    date="Date",
    datetime="DateTime",
)

_HIERARCHY_ATTR_NAMES = ("parent",)

_WILDCARD_ENDPOINT = "*"

_STRUCTURAL_TOP_LEVEL_KEYS = ("node_types", "edge_types", "version", "date", "status")


def _custom_extension(payload: dict[str, Any]) -> dict[str, str]:
    data = dict()
    data["vendor_name"] = _AGNES_VENDOR
    data["data"] = json.dumps(payload, sort_keys=True)
    return data


def _pass_through_expression(name: str) -> dict[str, Any]:
    dialect_expression = dict()
    dialect_expression["dialect"] = _DIALECT
    dialect_expression["expression"] = quote_ident(name)
    expression = dict()
    expression["dialects"] = [dialect_expression]
    return expression


@dataclass
class TranslationReport:
    """What the translation could not place structurally, or placed by
    inference rather than an explicit source rule -- printed to stdout so an
    operator reviews every judgment call before the document is used."""

    node_type_count: int = 0
    edge_type_count: int = 0
    ai_context_keys: list = field(default_factory=list)
    self_relationships: list = field(default_factory=list)
    wildcard_relationships: list = field(default_factory=list)
    glossary_terms: list = field(default_factory=list)

    def render(self) -> str:
        lines = [
            "Ontology translation report",
            f"  node types  -> datasets:      {self.node_type_count}",
            f"  edge types  -> relationships: {self.edge_type_count}",
        ]
        if self.ai_context_keys:
            lines.append("")
            lines.append(
                "  Guidance with no structural Ossie field, folded into ai_context.instructions -- review it there:"
            )
            for key in self.ai_context_keys:
                lines.append(f"    - {key}")
        if self.self_relationships:
            lines.append("")
            lines.append(
                "  Hierarchies inferred from a plain attribute, emitted as a "
                "self-relationship -- CONFIRM these reflect the intended structure:"
            )
            for note in self.self_relationships:
                lines.append(f"    - {note}")
        if self.wildcard_relationships:
            lines.append("")
            lines.append(
                "  Relationships with a wildcard src or dst -- emitted "
                "literally, CONFIRM the intended concrete endpoint(s):"
            )
            for name in self.wildcard_relationships:
                lines.append(f"    - {name}")
        if self.glossary_terms:
            lines.append("")
            lines.append("  Synonyms translated into glossary entries:")
            for term in self.glossary_terms:
                lines.append(f"    - {term}")
        return "\n".join(lines)


def _flatten_text(path: str, value: Any, lines: list[str]) -> None:
    """Recursively render any nested dict/list/scalar as path-colon-value
    lines, stripping only the value's own outer whitespace -- every leaf
    string survives as a substring of the rendered text, which is what
    makes a rule like "every edge must carry evidence" retrievable verbatim
    rather than paraphrased into a summary."""
    if isinstance(value, dict):
        for key, sub_value in value.items():
            next_path = f"{path}.{key}" if path else str(key)
            _flatten_text(next_path, sub_value, lines)
    elif isinstance(value, list):
        for index, sub_value in enumerate(value):
            if isinstance(sub_value, (dict, list)):
                _flatten_text(f"{path}[{index}]", sub_value, lines)
            else:
                lines.append(f"{path}: {str(sub_value).strip()}")
    elif value is None:
        return
    else:
        lines.append(f"{path}: {str(value).strip()}")


def _compose_ai_context(ontology: dict[str, Any], report: TranslationReport) -> Optional[dict[str, Any]]:
    empty_values = (None, "", [], dict())
    lines: list[str] = []
    for key, value in ontology.items():
        if key in _STRUCTURAL_TOP_LEVEL_KEYS:
            continue
        skip = False
        for empty_value in empty_values:
            if value == empty_value:
                skip = True
        if skip:
            continue
        _flatten_text(key, value, lines)
        report.ai_context_keys.append(key)
    if not lines:
        return None
    ai_context = dict()
    ai_context["instructions"] = "\n".join(lines)
    return ai_context


def _compose_field(attr_name: str, attr_spec: dict[str, Any]) -> dict[str, Any]:
    out = dict()
    out["name"] = attr_name
    out["expression"] = _pass_through_expression(attr_name)

    raw_type = attr_spec.get("type")
    datatype = _DATATYPE_MAP.get(str(raw_type).strip().lower()) if raw_type else None
    if datatype:
        out["datatype"] = datatype

    extension = dict()
    if raw_type and not datatype:
        extension["ontology_type"] = raw_type
    if attr_spec.get("required"):
        extension["required"] = True
    if "enum" in attr_spec:
        extension["enum"] = list(attr_spec["enum"])
    if extension:
        out["custom_extensions"] = [_custom_extension(extension)]
    return out


def _hierarchy_attrs(attrs: dict[str, Any]) -> list[str]:
    found = []
    for name, spec in attrs.items():
        spec = spec or dict()
        attr_type = str(spec.get("type", "")).strip().lower()
        if name.lower() in _HIERARCHY_ATTR_NAMES and attr_type == "string":
            found.append(name)
    return found


def _compose_dataset(
    type_name: str, type_spec: dict[str, Any]
) -> tuple[dict[str, Any], list[str], Optional[dict[str, Any]]]:
    attrs = type_spec.get("attrs") or dict()
    fields = [_compose_field(name, spec or dict()) for name, spec in attrs.items()]

    node_type_extension = dict()
    node_type_extension["ontology_node_type"] = True

    dataset = dict()
    dataset["name"] = type_name
    # The document contract (spec Sec 3) backs every node type by the same
    # facts table filtered on `type`; step 1 has no schema to point at yet,
    # so `source` names the ontology's own node-type identifier rather than
    # a physical table that does not exist until step 2.
    dataset["source"] = f"ontology_node_type:{type_name}"
    dataset["custom_extensions"] = [_custom_extension(node_type_extension)]

    description = type_spec.get("description")
    if description:
        dataset["description"] = str(description).strip()
    if fields:
        dataset["fields"] = fields

    synonyms = type_spec.get("synonyms")
    glossary_entry = None
    if synonyms:
        glossary_entry = dict()
        glossary_entry["term"] = type_name
        glossary_entry["definition"] = str(description).strip() if description else ""
        glossary_entry["see_also"] = list(synonyms)

    return dataset, _hierarchy_attrs(attrs), glossary_entry


def _compose_relationship(edge_name: str, edge_spec: dict[str, Any], report: TranslationReport) -> dict[str, Any]:
    src = str(edge_spec.get("src", "") or "")
    dst = str(edge_spec.get("dst", "") or "")
    attrs = edge_spec.get("attrs") or dict()

    relationship = dict()
    relationship["name"] = edge_name
    # Ossie's schema key is literally `from`, a Python keyword -- set via
    # item assignment rather than a dict(from=...) keyword argument.
    relationship["from"] = src
    relationship["to"] = dst
    # Node ids are opaque (spec Sec 3, "facts.id is opaque"); there is no
    # real foreign-key column to name, so `id` stands in as the
    # schema-required join key on both sides, same placeholder pattern as
    # the field expression above.
    relationship["from_columns"] = ["id"]
    relationship["to_columns"] = ["id"]

    description = edge_spec.get("description")
    if description:
        relationship["ai_context"] = str(description).strip()
    if attrs:
        edge_attrs_extension = dict()
        edge_attrs_extension["edge_attrs"] = attrs
        relationship["custom_extensions"] = [_custom_extension(edge_attrs_extension)]

    if src == _WILDCARD_ENDPOINT or dst == _WILDCARD_ENDPOINT:
        report.wildcard_relationships.append(edge_name)
    return relationship


def _compose_self_relationship(type_name: str, attr_name: str, report: TranslationReport) -> dict[str, Any]:
    name = f"{type_name}_{attr_name}_hierarchy"
    note = (
        f"{type_name}.{attr_name} -> self-relationship "
        + repr(name)
        + " -- CONFIRM this matches the intended hierarchy"
    )
    report.self_relationships.append(note)

    extension = dict()
    extension["inferred_from_attribute"] = attr_name
    extension["kind"] = "hierarchy"

    relationship = dict()
    relationship["name"] = name
    relationship["from"] = type_name
    relationship["to"] = type_name
    relationship["from_columns"] = [attr_name]
    relationship["to_columns"] = ["name"]
    relationship["custom_extensions"] = [_custom_extension(extension)]
    return relationship


def translate_ontology(ontology: dict[str, Any]) -> tuple[str, TranslationReport]:
    """Translate a parsed ontology.yaml document into an Ossie semantic-model
    document (YAML text) plus a report of every leftover the translation
    could not place structurally.

    Callers should use the returned text as-is (write it to disk, POST it) --
    never re-serialize it through a YAML dumper again, same "never re-dump"
    contract every semantic-source adapter follows
    (src/semantic/adapters/__init__.py)."""
    report = TranslationReport()

    node_types = ontology.get("node_types") or dict()
    edge_types = ontology.get("edge_types") or dict()
    report.node_type_count = len(node_types)
    report.edge_type_count = len(edge_types)

    datasets: list[dict[str, Any]] = []
    hierarchy_relationships: list[dict[str, Any]] = []
    glossary: list[dict[str, Any]] = []
    for type_name in sorted(node_types):
        dataset, hierarchy_attrs, glossary_entry = _compose_dataset(type_name, node_types[type_name] or dict())
        datasets.append(dataset)
        for attr_name in hierarchy_attrs:
            hierarchy_relationships.append(_compose_self_relationship(type_name, attr_name, report))
        if glossary_entry:
            glossary.append(glossary_entry)
            report.glossary_terms.append(type_name)

    relationships: list[dict[str, Any]] = [
        _compose_relationship(edge_name, edge_types[edge_name] or dict(), report) for edge_name in sorted(edge_types)
    ]
    relationships.extend(hierarchy_relationships)

    model_name = ontology.get("name") or "ontology"
    semantic_model: dict[str, Any] = dict()
    semantic_model["name"] = model_name
    semantic_model["datasets"] = datasets
    if relationships:
        semantic_model["relationships"] = relationships

    ai_context = _compose_ai_context(ontology, report)
    if ai_context:
        semantic_model["ai_context"] = ai_context

    if glossary:
        glossary_extension = dict()
        glossary_extension["glossary"] = glossary
        semantic_model["custom_extensions"] = [_custom_extension(glossary_extension)]

    document: dict[str, Any] = dict()
    document["version"] = SPEC_VERSION
    document["semantic_model"] = [semantic_model]
    text = yaml.safe_dump(document, sort_keys=False)
    return text, report


def _post_to_server(document_text: str, server: str, token: str, description: Optional[str]) -> dict[str, Any]:
    import httpx

    payload = dict()
    payload["document"] = document_text
    if description:
        payload["description"] = description

    url = server.rstrip("/") + "/api/admin/semantic-models"
    headers = dict()
    headers["Authorization"] = "Bearer " + token
    response = httpx.post(url, json=payload, headers=headers, timeout=30.0)
    if response.status_code not in (200, 201):
        raise RuntimeError(f"server returned {response.status_code}: {response.text}")
    return response.json()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Translate a producer's ontology.yaml into an Agnes semantic-model (Ossie) document"
    )
    parser.add_argument("ontology", type=Path, help="Path to the producer's ontology.yaml")
    parser.add_argument("--out", type=Path, help="Write the translated Ossie document to this path")
    parser.add_argument(
        "--server",
        help="Post the translated document to POST server/api/admin/semantic-models (requires --token)",
    )
    parser.add_argument("--token", help="Admin PAT bearer token for --server")
    parser.add_argument("--description", help="Optional description to store with the model")
    args = parser.parse_args(argv)

    if not args.ontology.exists():
        print(f"error: ontology file not found: {args.ontology}", file=sys.stderr)
        return 2
    if args.server and not args.token:
        print("error: --server requires --token", file=sys.stderr)
        return 2

    ontology = yaml.safe_load(args.ontology.read_text())
    if not isinstance(ontology, dict):
        print("error: ontology file does not parse to a mapping", file=sys.stderr)
        return 1

    document_text, report = translate_ontology(ontology)

    result = validate_document(document_text)
    if not result.ok:
        print("Translated document failed schema validation -- refusing to write/post it:", file=sys.stderr)
        for error in result.errors:
            print(f"  {error}", file=sys.stderr)
        return 1

    print(report.render())
    print()

    if args.server:
        try:
            body = _post_to_server(document_text, args.server, args.token, args.description)
        except Exception as exc:  # noqa: BLE001 -- surfaced to the operator, not swallowed
            print(f"error: failed to import into {args.server}: {exc}", file=sys.stderr)
            return 1
        model_id = body.get("id")
        slug = body.get("slug")
        print(f"Imported semantic model id={model_id} slug={slug}")

    if args.out:
        args.out.write_text(document_text)
        print(f"Wrote {args.out}")

    if not args.server and not args.out:
        print(document_text)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
