"""Storage-independent core of the semantic-layer query validator.

Design: docs/superpowers/specs/2026-08-14-semantic-layer-ui-and-agent-parity-design.md
(section 3, "Query validator"). Mirrors the upstream vendor assistant's
``validate_semantic_query`` tool contract — same input/output shape, same
honesty about its limits.

This module operates on a plain ``document`` dict — the parsed form of an
Ossie-style semantic-model document (docs/superpowers/specs/
2026-08-13-open-semantic-layer-contract-design.md), i.e. what will become a
``semantic_models.document_json`` row once the storage slice lands. It has
**no** DB access, no HTTP, and no import from ``app.`` or ``src.repositories``
— every surface (REST/CLI/MCP) wraps these functions rather than duplicating
the logic.

Expected document shape (fields this module reads; anything else is ignored)::

    {
        "name": str, "description": str,
        "datasets": [
            {"name": str, "source": str, "primary_key": [...],
             "ai_context": {...}, "fields": [{"name": str, ...}, ...]},
            ...
        ],
        "metrics": [
            {"name": str, "dataset": str,
             "expression": {"dialects": [{"dialect": str, "expression": str}, ...]}},
            ...
        ],
        "relationships": [
            {"name": str, "from": <dataset name>, "to": <dataset name>, ...},
            ...
        ],
        "glossary": [{"term": str, "definition": str, "see_also": [...]}, ...],
        "custom_extensions": [{"vendor_name": str, "data": "<json string>"}, ...],
    }

LIMITATIONS (carried into every surface that wraps this module):

- Detection (``detect_used_objects``) is best-effort, case-insensitive string
  matching of declared names against the raw SQL text — **not SQL parsing**.
  A dataset/metric/column whose name is a common word, or a query that
  references objects only through an unrelated alias or a view several joins
  removed, can produce false negatives; a name that happens to appear in a
  comment or a string literal can produce a false positive.
- Constraint checking (``evaluate_constraints``) can only verify rules whose
  ``constraint_type`` this module recognizes as checkable from the raw SQL
  text alone (currently just ``"required_filter"``, checked as text presence —
  see ``_STATICALLY_CHECKABLE_CONSTRAINT_TYPES``). Anything else — most business
  rules are about the query's *result*, not its text (e.g. a value-range rule
  like ``"value >= 0"``) — degrades to a ``post_execution_checks`` entry
  rather than a guess in either direction.
- ``locally_executable`` only inspects whether a *used* metric declares an
  expression for the target engine (or an ``ANSI_SQL`` fallback -- the
  universally accepted baseline, any target engine; see ``check_dialects``);
  it says nothing about whether the composed SQL is otherwise valid.

The expected-object payload shape referenced above (``{type, name}``) is this
module's own provisional convention. The constraint payload is NOT: the core
schema leaves constraints homeless (they ride ``custom_extensions``), so that
shape belongs to Agnes, and its key for the rule kind is ``constraint_type``
— what ``connectors/keboola/semantic_ossie.py`` composes, what
``src/semantic/projection.py`` reads into
``metric_definitions.validation.rules[]``, and what
``agnes catalog --metrics --show`` renders from there. This module used to
read ``type`` instead, its own provisional name for the same field, so no
imported constraint was ever statically checkable; one name now spans the
whole chain rather than each end accepting both.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Custom_extensions[].vendor_name under which Agnes-specific constraint data
# travels (contract spec, "Background: Apache Ossie" -> custom_extensions).
# The contract spec doesn't literally pin this string; "agnes" is this
# module's working choice, consistent with existing lowercase source/vendor
# tags elsewhere in the codebase (e.g. metric_definitions.source values).
AGNES_VENDOR_NAME = "agnes"

# Constraint ``constraint_type`` values this module can check against the raw
# SQL text (a "requires this text to appear somewhere" rule). Everything else
# needs the query's *result* to evaluate (e.g. a value-range rule) and always
# degrades to a post_execution_checks entry -- never a guessed violation.
_STATICALLY_CHECKABLE_CONSTRAINT_TYPES = frozenset({"required_filter"})

# ANSI_SQL is executable on EVERY target engine, not just DuckDB: the
# contract spec's dialect-handling rule states it for DuckDB ("Read the
# DuckDB expression when the document offers one; otherwise ANSI SQL, which
# DuckDB accepts"), and ``_mixed_dialect_warning`` already composes by the
# same universality -- executability and composability must agree on it
# (Devin Review on PR #1319). ``check_dialects`` unions it into every
# target's usable set.
_UNIVERSAL_DIALECT = "ansi_sql"

# Disclosure of the LIMITATIONS above, carried in the wire result so every
# surface that wraps ``validate_query`` (the REST endpoint, the ``/api/query``
# soft-enforce advisory, the CLI, MCP) says the same thing rather than each
# inventing -- or omitting -- its own caveat. Issue #1707 finding A18: the
# REST endpoint used to ship ``used_datasets``/``used_metrics`` with no such
# disclosure at all, so a heuristic name-match read as a confirmed fact.
DETECTION_NOTE_LONG = (
    "Datasets and metrics were detected by a best-effort text match on their declared names, not by parsing "
    "the SQL — a column or alias that shares a name matches too. Treat this as a prompt to check, not a proof."
)


def _normalize_for_presence(text: str) -> str:
    """Normalize SQL-ish text for the required_filter presence check:
    lowercase, strip double quotes (SQL identifier quoting), drop ALL
    whitespace.

    The check asks "does the rule's text appear in the query?", and spacing
    (`region='EU'` vs `region = 'EU'`) or identifier quoting (`"region"` vs
    `region` -- the same name in SQL) must not turn a query that genuinely
    applies the filter into an error-severity violation (Devin Review on PR
    #1319, rounds 5-6). Double quotes are stripped rather than aliased to
    single quotes: aliasing turned `WHERE "region" = 'EU'` into
    `'region'='eu'`, which the rule `region='eu'` then failed to match. A
    double-quoted *string literal* (nonstandard SQL) loses its quotes too --
    acceptable under this check's best-effort contract. Single quotes are
    kept: they delimit literals, and the trailing quote is exactly what stops
    `region='eu'` from matching inside `region='europe'`. Dropping whitespace
    entirely is safe here because both sides get the same treatment and the
    rule's characters must still appear in order.
    """
    return "".join(text.split()).lower().replace('"', "")


def _word_present(text: str, ident: str) -> bool:
    """Case-insensitive whole-identifier presence check.

    ``ident`` comes from imported document content (untrusted), so it is
    ``re.escape``-d before compiling -- the pattern is then a literal string
    with two fixed-width lookarounds, which is linear-time regardless of
    ``ident``'s content (no nested quantifiers, no backtracking blowup).
    """
    ident = (ident or "").strip()
    if not ident or not text:
        return False
    pattern = r"(?<![A-Za-z0-9_])" + re.escape(ident) + r"(?![A-Za-z0-9_])"
    return re.search(pattern, text, re.IGNORECASE) is not None


def detect_used_objects(sql: str, document: dict[str, Any]) -> dict[str, list[str]]:
    """Heuristic, case-insensitive match of dataset names/table ids/column
    names/metric names declared in ``document`` against ``sql``'s text.

    Best-effort string matching by declared contract -- not SQL parsing (see
    the module LIMITATIONS section). Returns ``used_datasets`` (a dataset
    counts as used if its name, its physical ``source``, or any of its
    ``fields[]`` names is present), ``used_metrics`` (by metric name), and
    ``matched_relationships`` (a relationship matches when both its ``from``
    and ``to`` datasets are used).
    """
    text = sql or ""
    document = document if isinstance(document, dict) else {}

    used_datasets: list[str] = []
    for dataset in document.get("datasets") or []:
        if not isinstance(dataset, dict):
            continue
        name = dataset.get("name")
        if not name:
            continue
        candidates: list[str] = [str(name)]
        source = dataset.get("source")
        if source:
            source = str(source)
            candidates.append(source)
            last_segment = source.rsplit(".", 1)[-1]
            if last_segment:
                candidates.append(last_segment)

        matched = any(_word_present(text, c) for c in candidates)
        if not matched:
            for field in dataset.get("fields") or []:
                if isinstance(field, dict) and field.get("name") and _word_present(text, str(field["name"])):
                    matched = True
                    break
        if matched:
            used_datasets.append(str(name))

    used_metrics: list[str] = []
    for metric in document.get("metrics") or []:
        if isinstance(metric, dict) and metric.get("name") and _word_present(text, str(metric["name"])):
            used_metrics.append(str(metric["name"]))

    # Same case-insensitive join rationale as evaluate_constraints (Devin
    # Review on PR #1319, round 4): from/to and dataset names are both
    # imported text and may disagree on case.
    used_dataset_set = {str(d).casefold() for d in used_datasets}
    matched_relationships: list[str] = []
    for relationship in document.get("relationships") or []:
        if not isinstance(relationship, dict):
            continue
        name = relationship.get("name")
        if not name:
            continue
        from_dataset = str(relationship.get("from") or "").casefold()
        to_dataset = str(relationship.get("to") or "").casefold()
        if from_dataset in used_dataset_set and to_dataset in used_dataset_set:
            matched_relationships.append(str(name))

    return {
        "used_datasets": used_datasets,
        "used_metrics": used_metrics,
        "matched_relationships": matched_relationships,
    }


def extract_constraints(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Read constraints from ``document["custom_extensions"]`` under
    ``AGNES_VENDOR_NAME``. ``data`` may be a JSON string or already-parsed
    JSON (dict/list) -- storage layers legitimately hand back either shape.
    Tolerates an absent/malformed extension entirely -- returns ``[]`` rather
    than raising, for every shape of bad input (missing key, wrong type,
    non-JSON string ``data``, missing/wrong-typed ``constraints``).

    Each returned constraint is normalized to
    ``{"name", "constraint_type", "rule", "severity", "metrics"}`` --
    ``severity`` and ``constraint_type`` are imported document text and are
    stored casefolded
    (``"ERROR"`` must drive ``valid=False`` exactly like ``"error"``, and
    ``"Required_Filter"`` must reach the static check); ``severity`` defaults
    to ``"warning"`` unless it reads as ``"error"``/``"warning"``, so a
    malformed/absent severity can never accidentally drive ``valid=False``
    downstream.
    """
    if not isinstance(document, dict):
        return []
    extensions = document.get("custom_extensions")
    if not isinstance(extensions, list):
        return []

    constraints: list[dict[str, Any]] = []
    for entry in extensions:
        if not isinstance(entry, dict):
            continue
        # vendor_name is imported document text: casefold like every other
        # name comparison in this module, or a model tagged "Agnes"/"AGNES"
        # silently drops ALL its constraints -- fail-open (Devin Review on
        # PR #1319, round 7).
        vendor = entry.get("vendor_name")
        if not isinstance(vendor, str) or vendor.casefold() != AGNES_VENDOR_NAME:
            continue
        raw = entry.get("data")
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except ValueError:
                continue
        elif isinstance(raw, (dict, list)):
            # Already-parsed JSON: once storage keeps document_json as a
            # parsed tree, ``data`` arrives as an object, not a stringified
            # blob. Same payload, so read it directly rather than silently
            # dropping the constraints (Devin Review on PR #1319).
            parsed = raw
        else:
            continue

        if isinstance(parsed, dict):
            items = parsed.get("constraints")
        elif isinstance(parsed, list):
            items = parsed
        else:
            items = None
        if not isinstance(items, list):
            continue

        for item in items:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            metrics = item.get("metrics")
            if isinstance(metrics, str):
                metrics = [metrics]
            elif not isinstance(metrics, list):
                metrics = []
            # severity/constraint_type come from imported text and are compared (and
            # stored) casefolded, like every other comparison over document
            # text in this module: an exact-case test would silently downgrade
            # an "ERROR" constraint to a warning -- it could then never set
            # valid=False -- and hide a "Required_Filter" from the static
            # check (Devin Review on PR #1319, round 6).
            severity = item.get("severity")
            severity = severity.casefold() if isinstance(severity, str) else ""
            if severity not in ("error", "warning"):
                severity = "warning"
            # `constraint_type`, not `type`: the AGNES constraint payload has
            # no schema of its own, so its shape is fixed by the only producer
            # (`connectors/keboola/semantic_ossie.py::_compose_constraint`) and
            # the consumer that stores it into
            # `metric_definitions.validation` (`src.semantic.projection
            # ._constraints_for`) -- both `constraint_type`. Reading `type`
            # here meant no Keboola-composed constraint was ever statically
            # checkable: each degraded to `post_execution_checks` and the
            # validator could never return `valid=False` for a real imported
            # model. One key end to end beats each end accepting both.
            constraint_type = item.get("constraint_type")
            if isinstance(constraint_type, str):
                constraint_type = constraint_type.casefold()
            constraints.append(
                {
                    "name": str(item["name"]),
                    "constraint_type": constraint_type,
                    "rule": item.get("rule"),
                    "severity": severity,
                    "metrics": [str(m) for m in metrics if m],
                }
            )
    return constraints


def evaluate_constraints(
    constraints: list[dict[str, Any]],
    used_metrics: list[str],
    sql: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Evaluate ``constraints`` (as returned by ``extract_constraints``)
    against the metrics actually detected as used and the raw SQL text.

    A constraint whose ``metrics`` don't overlap ``used_metrics`` is not on a
    used metric and is skipped entirely -- it shows up in neither list. That
    includes a constraint with an empty/absent ``metrics`` list: the
    provisional constraint convention has no model-wide scope, so such an
    entry is never evaluated (deliberate, not an oversight). A
    constraint on a used metric whose ``constraint_type`` is not statically checkable
    (see ``_STATICALLY_CHECKABLE_CONSTRAINT_TYPES``) degrades to a
    ``post_execution_checks`` entry, never a guessed violation. Only a
    checkable rule that actually fails becomes a ``violations`` entry;
    ``severity == "error"`` there is what drives ``valid=False`` upstream.
    """
    # Case-insensitive join, like every other name comparison over imported
    # text in this module: an exact-case match would silently drop an
    # error-severity constraint whose metrics[] spelling differs from the
    # metric declaration — fail-open (Devin Review on PR #1319, round 4).
    used = {str(m).casefold() for m in (used_metrics or [])}
    normalized_text = _normalize_for_presence(sql or "")

    violations: list[dict[str, Any]] = []
    post_execution_checks: list[dict[str, Any]] = []

    for constraint in constraints or []:
        if not isinstance(constraint, dict):
            continue
        applicable_metrics = [m for m in (constraint.get("metrics") or []) if str(m).casefold() in used]
        if not applicable_metrics:
            continue

        # ``severity``, like ``constraint_type`` below, may come from a hand-built
        # constraint and is compared casefolded downstream (``valid`` keys off
        # exactly "error") -- normalize it the same way extract_constraints
        # does, or a hand-built "ERROR" silently degrades to advisory.
        severity = constraint.get("severity")
        severity = severity.casefold() if isinstance(severity, str) else ""
        if severity not in ("error", "warning"):
            severity = "warning"

        entry = {
            "name": constraint.get("name"),
            "constraint_type": constraint.get("constraint_type"),
            "rule": constraint.get("rule"),
            "severity": severity,
            "metrics": applicable_metrics,
        }

        # extract_constraints already stores ``constraint_type`` casefolded,
        # but this is public API and callers may hand-build constraints --
        # casefold again so "Required_Filter" reaches the static check either
        # way.
        constraint_type = constraint.get("constraint_type")
        if isinstance(constraint_type, str):
            constraint_type = constraint_type.casefold()
        # A non-string rule (imported documents are untrusted; a structured
        # rule value is legal input) can never appear verbatim in SQL text, so
        # stringifying it would manufacture a violation -- "never a guess in
        # either direction" means it degrades to a post-execution check
        # instead (Devin Review on PR #1319, round 7).
        rule = constraint.get("rule")
        if constraint_type not in _STATICALLY_CHECKABLE_CONSTRAINT_TYPES or not isinstance(rule, str) or not rule:
            post_execution_checks.append({**entry, "reason": "rule cannot be checked before executing the query"})
            continue

        rule_text = _normalize_for_presence(rule)
        if rule_text not in normalized_text:
            violations.append({**entry, "reason": f"required filter not found in the query: {constraint['rule']}"})

    return violations, post_execution_checks


def _declared_dialects(metric: dict[str, Any]) -> list[str]:
    # `expression` is imported, untrusted data: a plain-string expression (or
    # any non-dict shape) means "no declared dialects", never an error.
    expression = metric.get("expression")
    if not isinstance(expression, dict):
        return []
    dialects = expression.get("dialects") or []
    if not isinstance(dialects, list):
        return []
    # An entry must carry BOTH a label and an expression body: a label-only
    # entry has no fragment to compose, so counting it would claim the metric
    # runs on that engine when nothing can be built for it — and, for the
    # target engine, would hold locally_executable at True with nothing behind
    # it (Devin Review on PR #1319, round 7).
    return [
        str(d["dialect"])
        for d in dialects
        if isinstance(d, dict) and d.get("dialect") and str(d.get("expression") or "").strip()
    ]


def _canonical_dialect(dialect: str) -> str:
    return dialect.strip().lower()


def _used_metric_dialect_pairs(document: dict[str, Any], used_metrics: list[str]) -> list[tuple[str, list[str]]]:
    """Per *used* metric in ``document``, ``(declared name, dialect labels)``
    (raw display form; the label list may be empty for a metric with no
    expressions).

    The name is carried alongside the labels so a caller can say WHICH metric
    is unexecutable instead of naming every metric the statement touched --
    the bool alone cannot distinguish them.

    The name join is casefolded on both sides, like every other name
    comparison in this module: ``check_dialects`` is public API and its
    ``used_metrics`` may be caller-supplied, so an exact-case join would
    yield "no declared dialects" for a case-variant name -- which reads as
    ``locally_executable=True``, a fail-open (Devin Review on PR #1319,
    round 6).
    """
    used = {str(m).casefold() for m in (used_metrics or [])}
    pairs: list[tuple[str, list[str]]] = []
    for metric in document.get("metrics") or []:
        if not isinstance(metric, dict) or not metric.get("name"):
            continue
        if str(metric["name"]).casefold() not in used:
            continue
        pairs.append((str(metric["name"]), _declared_dialects(metric)))
    return pairs


def _used_metric_dialects(document: dict[str, Any], used_metrics: list[str]) -> list[list[str]]:
    """The labels-only projection of ``_used_metric_dialect_pairs`` -- what
    ``_mixed_dialect_warning`` consumes."""
    return [dialects for _name, dialects in _used_metric_dialect_pairs(document, used_metrics)]


def _declares_unusable_expression(metric: dict[str, Any]) -> bool:
    """True when a metric declares ``dialects[]`` entries but not one of them
    carries an expression body.

    Such a metric composes on NO engine, so it must not read the same as one
    that declares no expression block at all (which stays unflagged — it may
    be defined elsewhere). Without this, dropping label-only entries would
    turn "declares an engine we cannot use" into silence, flipping
    ``locally_executable`` from False to True (Devin Review on PR #1327).
    """
    expression = metric.get("expression")
    if not isinstance(expression, dict):
        return False
    dialects = expression.get("dialects")
    if not isinstance(dialects, list) or not dialects:
        return False
    return not _declared_dialects(metric)


def _mixed_dialect_warning(declared: list[str], metric_dialect_lists: list[list[str]]) -> str | None:
    """Warn only when the used metrics share no dialect to compose in.

    A metric's declared dialects are ALTERNATIVES for one expression, not
    requirements, so a metric constrains the composition only through the
    set it declares -- and a metric declaring nothing, or only ``ANSI_SQL``
    (universally composable per the contract spec's dialect-handling rule),
    constrains it not at all. The warning fires when the constraining
    metrics' dialect sets have no common member. Labels come from untrusted
    imported text and are compared case-insensitively -- ``"DUCKDB"`` and
    ``"duckdb"`` are one dialect (Devin Review on PR #1319, both points).
    """
    # A metric whose alternatives INCLUDE the universal dialect composes with
    # anything through that variant, so it constrains nothing — dropping only
    # the universal member while keeping the rest would falsely lock it to
    # its engine-specific variants (Devin Review on PR #1319, round 3).
    canonical_sets = [{_canonical_dialect(d) for d in dialects} for dialects in metric_dialect_lists]
    constraining = [s for s in canonical_sets if s and _UNIVERSAL_DIALECT not in s]
    if not constraining or set.intersection(*constraining):
        return None
    non_ansi = sorted((d for d in declared if _canonical_dialect(d) != _UNIVERSAL_DIALECT), key=str.lower)
    return (
        "Used metrics declare mixed SQL dialects (" + ", ".join(non_ansi) + "); "
        "composing their expressions into one query may not run as written."
    )


def check_dialects(
    document: dict[str, Any],
    used_metrics: list[str],
    target_engine: str = "duckdb",
) -> dict[str, Any]:
    """Dialects declared by the *used* metrics in ``document``, a mixed-dialect
    warning (only when those metrics share no composable dialect -- see
    ``_mixed_dialect_warning``), and whether every used metric is executable
    on ``target_engine``.

    ``sql_dialects`` is de-duplicated case-insensitively (first-seen display
    form wins). ``locally_executable`` is ``False`` when a used metric
    declares expressions but none of them target ``target_engine`` or
    ``ANSI_SQL`` (usable on any target -- see ``_UNIVERSAL_DIALECT``), and
    also when it declares ``dialects[]`` entries of which none carries an
    expression body (nothing composes anywhere). A metric with no expression
    block at all is not flagged here -- nothing to conflict with.

    ``not_executable_metrics`` names the metrics that drove
    ``locally_executable`` to ``False``, in declaration order. A single bool
    forces a caller who wants to warn about it to name EVERY used metric --
    turning one unusable metric into an accusation against all of them --
    so the names travel with the flag. Empty whenever
    ``locally_executable`` is ``True``.
    """
    document = document if isinstance(document, dict) else {}
    target = (target_engine or "duckdb").strip().lower()
    usable = frozenset({target, _UNIVERSAL_DIALECT})

    pairs = _used_metric_dialect_pairs(document, used_metrics)
    per_metric = [dialects for _name, dialects in pairs]

    declared: list[str] = []
    seen: set[str] = set()
    not_executable: list[str] = []
    for name, metric_dialects in pairs:
        for dialect in metric_dialects:
            key = _canonical_dialect(dialect)
            if key not in seen:
                seen.add(key)
                declared.append(dialect)
        if metric_dialects and not any(_canonical_dialect(d) in usable for d in metric_dialects):
            not_executable.append(name)

    # A used metric that declares dialect entries none of which carry a body
    # composes nowhere; it reaches this point with an EMPTY dialect list, so
    # the loop above cannot see it (Devin Review on PR #1327). Every such
    # metric is collected, not just the first: the loop no longer only flips
    # a bool, it builds the offender list the caller names out loud.
    used = {str(m).casefold() for m in (used_metrics or [])}
    for metric in document.get("metrics") or []:
        if not isinstance(metric, dict) or not metric.get("name"):
            continue
        if (
            str(metric["name"]).casefold() in used
            and _declares_unusable_expression(metric)
            and str(metric["name"]) not in not_executable
        ):
            not_executable.append(str(metric["name"]))

    return {
        "sql_dialects": declared,
        "mixed_dialect_warning": _mixed_dialect_warning(declared, per_metric),
        "locally_executable": not not_executable,
        "not_executable_metrics": not_executable,
    }


def _build_summary(
    *,
    used_datasets: list[str],
    used_metrics: list[str],
    violations: list[dict[str, Any]],
    post_execution_checks: list[dict[str, Any]],
    locally_executable: bool,
    mixed_dialect_warning: str | None,
) -> str:
    # Written from the violations list, not the pass/fail flag: a
    # warning-severity violation keeps valid=True but must still be named
    # here, never summarized as "no constraint violations detected"
    # (Devin Review on PR #1319).
    error_count = sum(1 for v in violations if v.get("severity") == "error")
    warning_count = len(violations) - error_count

    # Every branch keeps the detection read-out, and the counts must agree
    # with len(violations) -- an error must not hide the advisory count nor
    # drop the dataset/metric sentence (Devin Review on PR #1319, round 8).
    base = f"Query references {len(used_datasets)} dataset(s) and {len(used_metrics)} metric(s) from the semantic layer"
    parts: list[str] = []
    if error_count:
        counts = f"{error_count} blocking"
        if warning_count:
            counts += f" and {warning_count} advisory"
        parts.append(f"{base}; {counts} constraint violation(s) found -- see 'violations'.")
    elif warning_count:
        parts.append(f"{base}; {warning_count} advisory constraint violation(s) detected -- see 'violations'.")
    else:
        parts.append(f"{base}; no constraint violations detected.")
    if post_execution_checks:
        parts.append(
            f"{len(post_execution_checks)} constraint(s) cannot be checked before execution -- "
            "see 'post_execution_checks'."
        )
    if not locally_executable:
        parts.append("One or more used metrics have no expression for the local engine and are not locally executable.")
    if mixed_dialect_warning:
        parts.append(mixed_dialect_warning)
    return " ".join(parts)


def _diff_expected(
    expected: list[dict[str, Any]],
    used_datasets: list[str],
    used_metrics: list[str],
    matched_relationships: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Diff the caller's ``expected`` objects against the detected ones,
    returning ``(matched, missing, unexpected)`` entries of ``{type, name}``.

    ``unexpected`` is scoped to the object TYPES the caller actually
    enumerated in ``expected``: a type with no ``expected`` entry means the
    caller expressed no expectation for it, so its detections are not
    reported as unexpected -- ``expected=[one dataset]`` must not flag every
    detected metric and relationship. The vendor contract doesn't pin this
    either way; scoping to the enumerated types is the least-surprise
    reading (Devin Review on PR #1319, round 6).
    """
    detected_lists: dict[str, list[str]] = {
        "dataset": used_datasets,
        "metric": used_metrics,
        "relationship": matched_relationships,
    }
    # Case-insensitive join, same rationale as every other name join in this
    # module: expected names come from an agent's tool call, detected names
    # from imported text, and a capitalisation difference must not report one
    # object as both missing and unexpected (Devin Review on PR #1319,
    # round 5). Output entries keep each side's original spelling.
    detected_sets = {etype: {str(n).casefold() for n in names} for etype, names in detected_lists.items()}
    expected_names: dict[str, set[str]] = {etype: set() for etype in detected_lists}

    matched: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for item in expected or []:
        if not isinstance(item, dict):
            continue
        etype, name = item.get("type"), item.get("name")
        if not etype or not name:
            continue
        # The type key casefolds like the names on both sides: an expectation
        # typed "Dataset" must be checked, not silently skipped as an unknown
        # type (Devin Review on PR #1319, round 7). Output entries keep the
        # caller's spelling.
        etype_key = str(etype).casefold()
        if etype_key not in detected_sets:
            continue
        expected_names[etype_key].add(str(name).casefold())
        entry = {"type": etype, "name": name}
        (matched if str(name).casefold() in detected_sets[etype_key] else missing).append(entry)

    unexpected: list[dict[str, Any]] = []
    for etype, names in detected_lists.items():
        if not expected_names[etype]:
            continue  # caller expressed no expectation for this type -- see docstring
        for name in names:
            if str(name).casefold() not in expected_names[etype]:
                unexpected.append({"type": etype, "name": name})

    return matched, missing, unexpected


def validate_query(
    sql: str,
    documents: list[dict[str, Any]],
    expected: list[dict[str, Any]] | None = None,
    target_engine: str = "duckdb",
) -> dict[str, Any]:
    """Compose the functions above into the vendor-shape validation result.

    ``documents`` is a list of semantic-model documents (a query may span more
    than one model); every list below is the union across all of them,
    de-duplicated, in first-seen order. An empty ``documents`` list (no
    semantic model available) degrades gracefully to an all-clear result
    rather than raising -- the actual "only offer this when the instance has
    >=1 valid model" gate is a wiring-level concern for the surfaces that wrap
    this module, not this pure core.
    """
    documents = [d for d in (documents or []) if isinstance(d, dict)]

    used_datasets: list[str] = []
    used_metrics: list[str] = []
    matched_relationships: list[str] = []
    declared_dialects: list[str] = []
    seen_dialects: set[str] = set()
    metric_dialect_lists: list[list[str]] = []
    locally_executable = True
    not_executable_metrics: list[str] = []
    violations: list[dict[str, Any]] = []
    post_execution_checks: list[dict[str, Any]] = []

    # Constraints and dialects are scoped to the document that declares them:
    # each document is evaluated against ITS OWN detected metrics, never a
    # name-keyed pool across models — a constraint (or a dialect declaration)
    # in model A must not fire on a same-named metric that only model B
    # defines (Devin Review on PR #1319). The result lists are still unions
    # across documents, de-duplicated in first-seen order (case-insensitively
    # for dialect labels); the mixed-dialect warning is computed over the
    # per-metric dialect sets of every used metric across all documents.
    for document in documents:
        detected = detect_used_objects(sql, document)
        for name in detected["used_datasets"]:
            if name not in used_datasets:
                used_datasets.append(name)
        for name in detected["used_metrics"]:
            if name not in used_metrics:
                used_metrics.append(name)
        for name in detected["matched_relationships"]:
            if name not in matched_relationships:
                matched_relationships.append(name)

        dialect_info = check_dialects(document, detected["used_metrics"], target_engine)
        for dialect in dialect_info["sql_dialects"]:
            key = _canonical_dialect(dialect)
            if key not in seen_dialects:
                seen_dialects.add(key)
                declared_dialects.append(dialect)
        if not dialect_info["locally_executable"]:
            locally_executable = False
        # Union across documents, de-duplicated in first-seen order like every
        # other list here. Same-named metrics in two documents are one name to
        # the caller, who is going to print it.
        for name in dialect_info.get("not_executable_metrics") or []:
            if name not in not_executable_metrics:
                not_executable_metrics.append(name)
        metric_dialect_lists.extend(_used_metric_dialects(document, detected["used_metrics"]))

        document_violations, document_checks = evaluate_constraints(
            extract_constraints(document), detected["used_metrics"], sql
        )
        violations.extend(document_violations)
        post_execution_checks.extend(document_checks)
    valid = not any(v.get("severity") == "error" for v in violations)
    mixed_dialect_warning = _mixed_dialect_warning(declared_dialects, metric_dialect_lists)

    summary = _build_summary(
        used_datasets=used_datasets,
        used_metrics=used_metrics,
        violations=violations,
        post_execution_checks=post_execution_checks,
        locally_executable=locally_executable,
        mixed_dialect_warning=mixed_dialect_warning,
    )

    result: dict[str, Any] = {
        "valid": valid,
        "used_datasets": used_datasets,
        "used_metrics": used_metrics,
        "matched_relationships": matched_relationships,
        "violations": violations,
        "post_execution_checks": post_execution_checks,
        "sql_dialects": declared_dialects,
        # Machine-readable twin of the summary's dialect sentence — the spec's
        # output contract lists the warning next to sql_dialects, and agents
        # should not have to parse prose (Devin Review on PR #1319, round 3).
        "mixed_dialect_warning": mixed_dialect_warning,
        "locally_executable": locally_executable,
        # Which used metrics drove ``locally_executable`` to False -- so a
        # consumer warns about those, not about every metric the statement
        # happened to mention. Empty when ``locally_executable`` is True.
        "not_executable_metrics": not_executable_metrics,
        "summary": summary,
        # Issue #1707 finding A18: every caller of this function ships
        # ``used_datasets``/``used_metrics`` as if they were confirmed facts;
        # without this, a heuristic hit (e.g. a WHERE clause on a column named
        # ``status`` matching a dataset that merely shares the word) reads as
        # a proven touch. Set here, once, so it travels with the result
        # regardless of which surface (REST endpoint, ``/api/query`` advisory,
        # CLI, MCP) forwards it.
        "detection": DETECTION_NOTE_LONG,
    }

    if expected is not None:
        matched_expected, missing_expected, unexpected_detected = _diff_expected(
            expected, used_datasets, used_metrics, matched_relationships
        )
        result["matched_expected_objects"] = matched_expected
        result["missing_expected_objects"] = missing_expected
        result["unexpected_detected_objects"] = unexpected_detected

    return result


__all__ = [
    "AGNES_VENDOR_NAME",
    "DETECTION_NOTE_LONG",
    "check_dialects",
    "detect_used_objects",
    "evaluate_constraints",
    "extract_constraints",
    "validate_query",
]
