"""Fixture world, agent loop and scoring for the semantic-layer eval.

Split out of ``tests/e2e/test_semantic_layer_eval.py`` for one reason: every
part of the eval except the API call itself has to be testable without an API
key. ``tests/test_semantic_eval_harness.py`` runs in the ordinary suite and
drives the loading, prompt rendering, scoring and aggregation below over canned
transcripts — so a broken comparison fails in seconds on every PR instead of
silently reporting 0 % on the one job that costs money.

What the eval measures
----------------------

The same question set is asked twice against a real LLM:

* the **baseline** arm — an instance with no semantic layer at all. The
  workspace ``CLAUDE.md`` renders with ``semantic_layer.has_models = False``
  (so the "## Semantic layer" section, including the "ask, don't guess" rule,
  is absent) and the semantic tools answer "nothing registered here".
* the **semantic** arm — the same instance with one model registered. The
  section renders, and the semantic tools serve
  :data:`SEMANTIC_MODEL`.

Both arms see the identical tool *surface* (the ``agnes`` CLI exists on every
install regardless of whether a model is registered); what differs is the
prompt section and whether the layer has anything to say. That is the honest
simulation of the two instances being compared, and it is why the eval is a
statement about the layer rather than about one sentence of prompt.

Scoring is deliberately tool-call-level, never text-level: a table counts as
reached only when it appears in an actual tool call's arguments, and a metric
counts as used only when its canonical definition was actually fetched. An
answer that name-drops ``revenue/net_revenue`` in prose while computing its own
``SUM(gross_amount)`` scores zero — that is precisely the failure the semantic
layer exists to prevent, so the eval must not credit it.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import duckdb
import yaml


QUESTIONS_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "semantic_eval_questions.yaml"
_TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "config" / "claude_md_template.txt"

# Pinned rather than "whatever is newest": the eval's two arms are only
# comparable to each other when both ran on the same model, and a rate recorded
# in CI is only meaningful next to the model that produced it. Overridable for
# a one-off run against a different model.
EVAL_MODEL = os.environ.get("AGNES_EVAL_MODEL", "claude-haiku-4-5-20251001")

# An agent that has not answered in this many assistant turns is looping; the
# transcript is scored as-is rather than paying for more.
MAX_TURNS = 8


# ---------------------------------------------------------------------------
# The fixture world
# ---------------------------------------------------------------------------

CATALOG: list[dict[str, Any]] = [
    {
        "id": "orders",
        "query_mode": "local",
        "source_type": "local",
        "description": "One row per placed order. `status` is one of 'completed', 'pending', 'cancelled'. Money columns are in minor units.",
    },
    {
        "id": "order_refunds",
        "query_mode": "local",
        "source_type": "local",
        "description": "One row per refund issued against an order. An order may have several.",
    },
    {
        "id": "customers",
        "query_mode": "local",
        "source_type": "local",
        "description": "One row per customer account, with signup date, country and commercial segment.",
    },
    {
        "id": "web_sessions",
        "query_mode": "local",
        "source_type": "local",
        "description": "One row per website visit. Deliberately outside the semantic model — the eval's control for a table the layer does not cover.",
    },
]

SCHEMAS: dict[str, list[dict[str, str]]] = {
    "orders": [
        {"name": "order_id", "type": "VARCHAR"},
        {"name": "customer_id", "type": "VARCHAR"},
        {"name": "order_date", "type": "DATE"},
        {"name": "status", "type": "VARCHAR"},
        {"name": "gross_amount", "type": "BIGINT"},
        {"name": "discount_amount", "type": "BIGINT"},
        {"name": "country", "type": "VARCHAR"},
    ],
    "order_refunds": [
        {"name": "refund_id", "type": "VARCHAR"},
        {"name": "order_id", "type": "VARCHAR"},
        {"name": "refund_date", "type": "DATE"},
        {"name": "refund_amount", "type": "BIGINT"},
    ],
    "customers": [
        {"name": "customer_id", "type": "VARCHAR"},
        {"name": "signup_date", "type": "DATE"},
        {"name": "country", "type": "VARCHAR"},
        {"name": "segment", "type": "VARCHAR"},
    ],
    "web_sessions": [
        {"name": "session_id", "type": "VARCHAR"},
        {"name": "session_date", "type": "DATE"},
        {"name": "country", "type": "VARCHAR"},
        {"name": "source", "type": "VARCHAR"},
    ],
}

# The declared metrics. Each carries the business decisions that make it a
# metric rather than a measure — which is exactly what an agent cannot guess.
METRICS: dict[str, dict[str, Any]] = {
    "revenue/net_revenue": {
        "id": "revenue/net_revenue",
        "name": "Net revenue",
        "description": (
            "Gross order value minus discounts minus everything refunded against those "
            "orders. Cancelled orders never count, pending ones do."
        ),
        "sql": (
            "SELECT SUM(o.gross_amount - o.discount_amount) - COALESCE(SUM(r.refund_amount), 0) AS net_revenue\n"
            "FROM orders o\n"
            "LEFT JOIN order_refunds r ON r.order_id = o.order_id\n"
            "WHERE o.status <> 'cancelled'"
        ),
        "notes": "Refunds are attributed to the ORDER's date, not the refund's own date.",
    },
    "revenue/gross_revenue": {
        "id": "revenue/gross_revenue",
        "name": "Gross revenue",
        "description": "Order value before discounts and before refunds. Cancelled orders excluded.",
        "sql": "SELECT SUM(gross_amount) AS gross_revenue FROM orders WHERE status <> 'cancelled'",
        "notes": "Never net of refunds — use revenue/net_revenue for that.",
    },
    "revenue/average_order_value": {
        "id": "revenue/average_order_value",
        "name": "Average order value",
        "description": "Net revenue divided by the number of completed orders. Pending orders are excluded from the denominator.",
        "sql": (
            "SELECT (SUM(gross_amount - discount_amount)) / NULLIF(COUNT(*), 0) AS average_order_value\n"
            "FROM orders WHERE status = 'completed'"
        ),
        "notes": "Denominator is completed orders only, which is why it differs from a naive AVG().",
    },
    "customers/active_customers": {
        "id": "customers/active_customers",
        "name": "Active customers",
        "description": "Customers with at least one completed order in the last 90 days.",
        "sql": (
            "SELECT COUNT(DISTINCT c.customer_id) AS active_customers\n"
            "FROM customers c\n"
            "JOIN orders o ON o.customer_id = c.customer_id\n"
            "WHERE o.status = 'completed' AND o.order_date >= CURRENT_DATE - INTERVAL 90 DAY"
        ),
        "notes": "90 days, not a calendar quarter. A customer who only ever cancelled is not active.",
    },
}

GLOSSARY: dict[str, str] = {
    "completed order": "An order whose status is 'completed'. Pending orders are not yet revenue-recognised; cancelled ones never were.",
    "active customer": "A customer with at least one completed order in the last 90 days.",
    "refund window": "Refunds are accepted for 60 days after the order date.",
}

RELATIONSHIPS: list[dict[str, str]] = [
    {"from": "orders.order_id", "to": "order_refunds.order_id", "type": "one_to_many"},
    {"from": "customers.customer_id", "to": "orders.customer_id", "type": "one_to_many"},
]

DATASETS: list[dict[str, Any]] = [
    {"name": "orders", "table": "orders", "grain": "one row per order"},
    {"name": "order_refunds", "table": "order_refunds", "grain": "one row per refund"},
    {"name": "customers", "table": "customers", "grain": "one row per customer"},
]

SEMANTIC_MODEL: dict[str, Any] = {
    "datasets": DATASETS,
    "metrics": list(METRICS.values()),
    "relationships": RELATIONSHIPS,
    "glossary": [{"term": t, "definition": d} for t, d in GLOSSARY.items()],
}

# Deterministic seed data. Dates are relative to CURRENT_DATE so "the last 90
# days" style questions return rows whenever the eval runs.
SEED_SQL = """
CREATE TABLE orders AS
SELECT
    'ord_' || LPAD(CAST(i AS VARCHAR), 4, '0')          AS order_id,
    'cus_' || LPAD(CAST(i % 40 AS VARCHAR), 3, '0')     AS customer_id,
    CURRENT_DATE - CAST(i % 120 AS INTEGER)             AS order_date,
    CASE WHEN i % 11 = 0 THEN 'cancelled'
         WHEN i % 7 = 0 THEN 'pending'
         ELSE 'completed' END                           AS status,
    CAST(1500 + (i * 37) % 9000 AS BIGINT)              AS gross_amount,
    CAST((i * 13) % 400 AS BIGINT)                      AS discount_amount,
    CASE WHEN i % 4 = 0 THEN 'CZ'
         WHEN i % 4 = 1 THEN 'DE'
         WHEN i % 4 = 2 THEN 'PL'
         ELSE 'AT' END                                  AS country
FROM range(1, 241) t(i);

CREATE TABLE order_refunds AS
SELECT
    'ref_' || LPAD(CAST(i AS VARCHAR), 4, '0')          AS refund_id,
    'ord_' || LPAD(CAST(i * 5 AS VARCHAR), 4, '0')      AS order_id,
    CURRENT_DATE - CAST((i * 5) % 120 AS INTEGER) + 3   AS refund_date,
    CAST(200 + (i * 29) % 1200 AS BIGINT)               AS refund_amount
FROM range(1, 41) t(i);

CREATE TABLE customers AS
SELECT
    'cus_' || LPAD(CAST(i AS VARCHAR), 3, '0')          AS customer_id,
    CURRENT_DATE - CAST(200 + i AS INTEGER)             AS signup_date,
    CASE WHEN i % 4 = 0 THEN 'CZ'
         WHEN i % 4 = 1 THEN 'DE'
         WHEN i % 4 = 2 THEN 'PL'
         ELSE 'AT' END                                  AS country,
    CASE WHEN i % 3 = 0 THEN 'enterprise'
         WHEN i % 3 = 1 THEN 'smb'
         ELSE 'self_serve' END                          AS segment
FROM range(0, 40) t(i);

CREATE TABLE web_sessions AS
SELECT
    'ses_' || LPAD(CAST(i AS VARCHAR), 4, '0')          AS session_id,
    CURRENT_DATE - CAST(i % 30 AS INTEGER)              AS session_date,
    CASE WHEN i % 4 = 0 THEN 'CZ'
         WHEN i % 4 = 1 THEN 'DE'
         WHEN i % 4 = 2 THEN 'PL'
         ELSE 'AT' END                                  AS country,
    CASE WHEN i % 2 = 0 THEN 'organic' ELSE 'paid' END  AS source
FROM range(1, 601) t(i);
"""

TABLE_NAMES: tuple[str, ...] = tuple(row["id"] for row in CATALOG)


# ---------------------------------------------------------------------------
# Question set
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvalQuestion:
    id: str
    category: str
    question: str
    tables: tuple[str, ...]
    metrics: tuple[str, ...]
    must_ask_for_clarification: bool


def load_questions(path: Path | str | None = None) -> list[EvalQuestion]:
    """Parse the question set and check it against the fixture world.

    A question expecting a table or metric the world does not define can never
    pass, and scoring it 0 would look like a model failure rather than the
    fixture bug it is — so it raises here instead.
    """
    src = Path(path) if path is not None else QUESTIONS_PATH
    raw = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
    rows = raw.get("questions") or []
    if not rows:
        raise ValueError(f"{src} defines no questions")

    out: list[EvalQuestion] = []
    seen: set[str] = set()
    for row in rows:
        qid = str(row.get("id") or "").strip()
        if not qid:
            raise ValueError(f"{src}: every question needs an id")
        if qid in seen:
            raise ValueError(f"{src}: duplicate question id {qid!r}")
        seen.add(qid)
        expects = row.get("expects") or {}
        tables = tuple(expects.get("tables") or ())
        metrics = tuple(expects.get("metrics") or ())
        unknown_tables = [t for t in tables if t not in TABLE_NAMES]
        if unknown_tables:
            raise ValueError(f"{src}: question {qid!r} expects tables not in the fixture world: {unknown_tables}")
        unknown_metrics = [m for m in metrics if m not in METRICS]
        if unknown_metrics:
            raise ValueError(f"{src}: question {qid!r} expects metrics not in the fixture world: {unknown_metrics}")
        must_ask = bool(expects.get("must_ask_for_clarification"))
        if must_ask and (tables or metrics):
            raise ValueError(
                f"{src}: question {qid!r} both demands clarification and expects tables/metrics — "
                "an agent cannot pass both halves, pick one"
            )
        out.append(
            EvalQuestion(
                id=qid,
                category=str(row.get("category") or "uncategorised"),
                question=str(row["question"]).strip(),
                tables=tables,
                metrics=metrics,
                must_ask_for_clarification=must_ask,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Workspace prompt — the one variable under test
# ---------------------------------------------------------------------------


def render_workspace_prompt(*, has_models: bool) -> str:
    """Render ``config/claude_md_template.txt`` as the two arms see it.

    Renders the shipped template directly rather than through
    ``src.claude_md.render_claude_md`` so the eval needs no database: the only
    context value the eval varies is ``semantic_layer.has_models``, and a real
    render is the point — the arms must differ by exactly what a real instance
    with and without a registered model would differ by. Jinja's
    ``StrictUndefined`` (see ``src.prompt_render.make_prompt_env``) makes this
    self-checking: a context key the template starts using and this function
    does not supply raises here rather than silently rendering an empty arm.
    """
    from src.prompt_render import make_prompt_env

    context: dict[str, Any] = {
        "instance": {"name": "Agnes Eval", "subtitle": ""},
        "server": {"url": "https://agnes.example.com", "hostname": "agnes.example.com"},
        "sync_interval": "1h",
        "data_source": {"type": "local", "source_types": ["local"]},
        "tables": [
            {
                "name": row["id"],
                "description": row["description"],
                "query_mode": row["query_mode"],
                "source_type": row["source_type"],
            }
            for row in CATALOG
        ],
        "metrics": {
            "count": len(METRICS) if has_models else 0,
            "categories": sorted({m.split("/")[0] for m in METRICS}) if has_models else [],
        },
        "semantic_layer": {"has_models": has_models},
        "marketplaces": [],
        "user": {
            "id": "u-eval",
            "email": "eval@example.com",
            "name": "Eval",
            "is_admin": False,
            "groups": ["Everyone"],
        },
        "now": "2026-01-01T00:00:00+00:00",
        "today": "2026-01-01",
        "is_sandbox": True,
    }
    env = make_prompt_env()
    return env.from_string(_TEMPLATE_PATH.read_text(encoding="utf-8")).render(**context)


# ---------------------------------------------------------------------------
# Tool surface — the `agnes` CLI as the model sees it
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "agnes_catalog",
        "description": "List the registered tables you can query (mirrors `agnes catalog`).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "agnes_schema",
        "description": "Columns and types of one registered table (mirrors `agnes schema <table>`).",
        "input_schema": {
            "type": "object",
            "properties": {"table": {"type": "string"}},
            "required": ["table"],
        },
    },
    {
        "name": "agnes_catalog_metrics",
        "description": "List the canonical business-metric definitions registered on this instance (mirrors `agnes catalog --metrics`).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "agnes_metric_show",
        "description": (
            "Read one canonical metric's SQL and business rules "
            "(mirrors `agnes catalog --metrics --show <category>/<name>`)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"metric_id": {"type": "string"}},
            "required": ["metric_id"],
        },
    },
    {
        "name": "agnes_semantic_model_context",
        "description": (
            "Read this instance's semantic model (mirrors `agnes semantic-model context <type>`). "
            "object_type is one of dataset, metric, relationship, glossary."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "object_type": {"type": "string"},
                "id": {"type": "string"},
            },
            "required": ["object_type"],
        },
    },
    {
        "name": "agnes_query",
        "description": 'Run read-only DuckDB SQL against the synced tables (mirrors `agnes query "<SQL>"`).',
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
        },
    },
]

_NO_LAYER = "No semantic model is registered on this instance."

# The workspace prompt tells the agent to run `agnes catalog` in a shell; here
# those commands are tools. Without saying so the model narrates the commands it
# *would* run and calls nothing — which would depress both arms equally and make
# the eval measure the harness instead of the layer. This note is therefore
# appended verbatim to BOTH arms: it is scaffolding, never a variable.
HARNESS_NOTE = (
    "You are running inside an evaluation harness, not a terminal. The `agnes` CLI "
    "described above is exposed to you as tools, one per command: `agnes catalog` is "
    "`agnes_catalog`, `agnes schema <table>` is `agnes_schema`, `agnes catalog --metrics` "
    "is `agnes_catalog_metrics`, `agnes catalog --metrics --show <id>` is "
    "`agnes_metric_show`, `agnes semantic-model context <type>` is "
    '`agnes_semantic_model_context`, and `agnes query "<SQL>"` is `agnes_query`. Call the '
    "tools rather than printing shell commands, and do not conclude a command is "
    "unavailable because there is no shell. Every other instruction above still applies."
)

# The model picks its own word for the object type ("metrics", "glossary_terms",
# …). Normalising is not leniency for its own sake: the eval must not score a
# correct decision as a miss because of a plural.
_OBJECT_TYPE_ALIASES = {
    "datasets": "dataset",
    "metrics": "metric",
    "relationships": "relationship",
    "glossary_term": "glossary",
    "glossary_terms": "glossary",
    "term": "glossary",
    "terms": "glossary",
}


def normalize_object_type(raw: str | None) -> str:
    key = str(raw or "").strip().lower()
    return _OBJECT_TYPE_ALIASES.get(key, key)


class FixtureTools:
    """Executes the tool surface above against an in-memory fixture warehouse.

    ``has_models`` switches the semantic half off exactly the way an instance
    with no registered model behaves: the commands still exist, they just have
    nothing to return.
    """

    def __init__(self, *, has_models: bool) -> None:
        self.has_models = has_models
        self._conn = duckdb.connect(":memory:")
        self._conn.execute(SEED_SQL)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "FixtureTools":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def call(self, name: str, args: dict[str, Any]) -> str:
        handler: Callable[[dict[str, Any]], Any] | None = {
            "agnes_catalog": self._catalog,
            "agnes_schema": self._schema,
            "agnes_catalog_metrics": self._metrics,
            "agnes_metric_show": self._metric_show,
            "agnes_semantic_model_context": self._semantic_context,
            "agnes_query": self._query,
        }.get(name)
        if handler is None:
            return f"error: unknown command {name!r}"
        try:
            return json.dumps(handler(args), default=str)
        except Exception as exc:  # surfaced to the model, same as a CLI error
            return f"error: {exc}"

    # -- handlers ------------------------------------------------------------

    def _catalog(self, _args: dict[str, Any]) -> Any:
        return {"tables": CATALOG}

    def _schema(self, args: dict[str, Any]) -> Any:
        table = str(args.get("table") or "")
        if table not in SCHEMAS:
            return {"error": f"table {table!r} is not registered — run agnes_catalog"}
        return {"table": table, "columns": SCHEMAS[table]}

    def _metrics(self, _args: dict[str, Any]) -> Any:
        if not self.has_models:
            return {"metrics": [], "note": _NO_LAYER}
        return {"metrics": [{"id": m["id"], "name": m["name"]} for m in METRICS.values()]}

    def _metric_show(self, args: dict[str, Any]) -> Any:
        metric_id = str(args.get("metric_id") or "")
        if not self.has_models:
            return {"error": _NO_LAYER}
        metric = METRICS.get(metric_id)
        if metric is None:
            return {"error": f"no metric {metric_id!r} is defined — run agnes_catalog_metrics"}
        return metric

    def _semantic_context(self, args: dict[str, Any]) -> Any:
        if not self.has_models:
            return {"error": _NO_LAYER}
        object_type = normalize_object_type(args.get("object_type"))
        wanted = args.get("id")
        bucket: Sequence[Any] = {
            "dataset": DATASETS,
            "metric": list(METRICS.values()),
            "relationship": RELATIONSHIPS,
            "glossary": SEMANTIC_MODEL["glossary"],
        }.get(object_type, [])
        if not bucket:
            return {"error": f"unknown object type {object_type!r} — try dataset, metric, relationship, glossary"}
        if wanted:
            hits = [o for o in bucket if wanted in (o.get("id"), o.get("name"), o.get("term"))]
            return {object_type: hits} if hits else {"error": f"no {object_type} named {wanted!r} in this model"}
        return {object_type: bucket}

    def _query(self, args: dict[str, Any]) -> Any:
        sql = str(args.get("sql") or "")
        cur = self._conn.execute(sql)
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(50)
        return {"columns": cols, "rows": [list(r) for r in rows]}


# ---------------------------------------------------------------------------
# Transcripts + scoring
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any]


@dataclass
class Transcript:
    """What one question's run produced. Deliberately small — everything the
    scorer is allowed to look at, and nothing else.

    ``final_text`` is every piece of assistant prose from the run, not only the
    last turn: an agent that says "churn isn't defined here" while thinking and
    then hedges into an answer has still said the true thing, and the
    ``computed_an_aggregate`` half of the undefined-term check is what catches
    the hedge.
    """

    question_id: str
    arm: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    final_text: str = ""
    error: str | None = None


# Phrases that count as "the agent declined to guess". Two families: naming the
# term as undefined, and putting the question back to the user. Both are
# acceptable outcomes of the "ask, don't guess" rule — the failure it targets is
# answering anyway.
_CLARIFICATION_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(not|isn't|is not|no)\b[^.\n]{0,40}\bdefin(ed|ition)\b", re.I),
    re.compile(r"\bno (canonical |registered |declared )?(metric|definition|glossary)\b", re.I),
    re.compile(r"\bnot (in|covered by|part of) (the |this )?(semantic (layer|model)|catalog|glossary)\b", re.I),
    re.compile(r"\b(could|can) you (clarify|specify|tell me)\b", re.I),
    re.compile(r"\bwhat do you mean by\b", re.I),
    re.compile(r"\bhow (do|would) you (define|want)\b", re.I),
    re.compile(r"\bwhich .{0,60}\bdo you mean\b", re.I),
    re.compile(r"\bI don'?t have (a|any) .{0,40}\bdefinition\b", re.I),
)

_AGGREGATE_SQL = re.compile(r"\b(sum|count|avg|median|min|max)\s*\(", re.I)


def asked_for_clarification(text: str) -> bool:
    """True when the answer declines to invent a definition."""
    return any(p.search(text or "") for p in _CLARIFICATION_MARKERS)


def computed_an_aggregate(tool_calls: Iterable[ToolCall]) -> bool:
    """True when the run actually computed a figure.

    Paired with :func:`asked_for_clarification` for the undefined-term
    questions: an agent that asks the user what "churn" means *and* quietly
    reports its own number has not followed the rule, it has hedged.
    """
    return any(
        call.name == "agnes_query" and _AGGREGATE_SQL.search(str(call.args.get("sql") or "")) for call in tool_calls
    )


def tables_reached(tool_calls: Iterable[ToolCall]) -> set[str]:
    """Fixture tables named in an actual tool call's arguments.

    Prose in the final answer is never consulted — see the module docstring.
    """
    found: set[str] = set()
    for call in tool_calls:
        if call.name == "agnes_schema":
            table = str(call.args.get("table") or "")
            if table in TABLE_NAMES:
                found.add(table)
        elif call.name == "agnes_query":
            sql = str(call.args.get("sql") or "")
            for name in TABLE_NAMES:
                if re.search(rf"\b{re.escape(name)}\b", sql, re.I):
                    found.add(name)
    return found


def metrics_looked_up(tool_calls: Iterable[ToolCall]) -> set[str]:
    """Canonical metric ids whose definition the run actually fetched."""
    found: set[str] = set()
    for call in tool_calls:
        if call.name == "agnes_metric_show":
            metric_id = str(call.args.get("metric_id") or "")
            if metric_id in METRICS:
                found.add(metric_id)
        elif call.name == "agnes_semantic_model_context":
            object_type = normalize_object_type(call.args.get("object_type"))
            wanted = str(call.args.get("id") or "")
            if object_type != "metric":
                continue
            if wanted in METRICS:
                found.add(wanted)
            elif not wanted:
                # `context metric` with no --id returns every metric, so the
                # definitions really were in front of the agent.
                found.update(METRICS)
    return found


@dataclass
class Outcome:
    question_id: str
    category: str
    arm: str
    passed: bool
    reasons: list[str] = field(default_factory=list)


def score(question: EvalQuestion, transcript: Transcript) -> Outcome:
    """Score one run against one question's `expects`."""
    reasons: list[str] = []
    if transcript.error:
        return Outcome(question.id, question.category, transcript.arm, False, [f"run failed: {transcript.error}"])

    if question.must_ask_for_clarification:
        if not asked_for_clarification(transcript.final_text):
            reasons.append("answered an undefined term instead of asking or saying it is undefined")
        if computed_an_aggregate(transcript.tool_calls):
            reasons.append("computed a figure for a term the layer does not define")
        return Outcome(question.id, question.category, transcript.arm, not reasons, reasons)

    reached = tables_reached(transcript.tool_calls)
    missing_tables = [t for t in question.tables if t not in reached]
    if missing_tables:
        reasons.append(
            f"never reached {', '.join(missing_tables)} in a tool call (reached: {sorted(reached) or 'nothing'})"
        )

    used = metrics_looked_up(transcript.tool_calls)
    missing_metrics = [m for m in question.metrics if m not in used]
    if missing_metrics:
        reasons.append(f"computed without reading the canonical definition of {', '.join(missing_metrics)}")

    if not transcript.tool_calls:
        reasons.append("answered from memory — no tool call at all")

    return Outcome(question.id, question.category, transcript.arm, not reasons, reasons)


@dataclass
class ArmResult:
    arm: str
    outcomes: list[Outcome]

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def passed(self) -> int:
        return sum(1 for o in self.outcomes if o.passed)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0


def compare(baseline: ArmResult, semantic: ArmResult) -> dict[str, Any]:
    """Baseline vs. semantic, plus the per-question regressions.

    ``regressions`` is the list that matters when the gate is green but the
    layer is still doing harm: questions the agent got right WITHOUT the
    semantic layer and wrong with it — e.g. an over-cautious agent that starts
    asking for clarification about a plain row count.
    """
    base_by_id = {o.question_id: o for o in baseline.outcomes}
    regressions = [
        o.question_id
        for o in semantic.outcomes
        if not o.passed and base_by_id.get(o.question_id) is not None and base_by_id[o.question_id].passed
    ]
    return {
        "baseline_rate": baseline.pass_rate,
        "semantic_rate": semantic.pass_rate,
        "improvement": semantic.pass_rate - baseline.pass_rate,
        "regressions": regressions,
    }


def format_report(baseline: ArmResult, semantic: ArmResult) -> str:
    """Human-readable per-question table for the CI log."""
    cmp_ = compare(baseline, semantic)
    base_by_id = {o.question_id: o for o in baseline.outcomes}
    lines = [
        "",
        f"semantic-layer eval — model={EVAL_MODEL}",
        f"  baseline : {baseline.passed}/{baseline.total} ({baseline.pass_rate:.0%})",
        f"  semantic : {semantic.passed}/{semantic.total} ({semantic.pass_rate:.0%})",
        f"  delta    : {cmp_['improvement']:+.0%}",
        "",
        f"  {'question':<32}{'base':<7}{'sem':<7}why the semantic arm failed",
        f"  {'-' * 78}",
    ]
    for outcome in semantic.outcomes:
        base = base_by_id.get(outcome.question_id)
        lines.append(
            f"  {outcome.question_id:<32}"
            f"{('PASS' if base and base.passed else 'FAIL'):<7}"
            f"{('PASS' if outcome.passed else 'FAIL'):<7}"
            f"{'; '.join(outcome.reasons)}"
        )
    if cmp_["regressions"]:
        lines.append("")
        lines.append(f"  REGRESSIONS (passed without the layer, failed with it): {', '.join(cmp_['regressions'])}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The live-LLM half
# ---------------------------------------------------------------------------


def run_question(client: Any, question: EvalQuestion, *, has_models: bool, model: str = EVAL_MODEL) -> Transcript:
    """Ask one question through a real LLM and return what it did.

    The system prompt is the rendered workspace ``CLAUDE.md`` for the arm — the
    same document a real analyst's Claude Code session is given — and the tools
    are the fixture stand-ins for the `agnes` CLI. Cheap by construction: the
    prompt is cached across the whole run, and the loop stops at the first turn
    that produces text without asking for another tool.
    """
    arm = "semantic" if has_models else "baseline"
    transcript = Transcript(question_id=question.id, arm=arm)
    system = [
        {
            "type": "text",
            "text": render_workspace_prompt(has_models=has_models),
            "cache_control": {"type": "ephemeral"},
        },
        # After the cache boundary, so the (large) workspace prompt is what gets
        # cached across the run and this short note is re-sent each time.
        {"type": "text", "text": HARNESS_NOTE},
    ]
    messages: list[dict[str, Any]] = [{"role": "user", "content": question.question}]

    with FixtureTools(has_models=has_models) as tools:
        try:
            for _ in range(MAX_TURNS):
                response = client.messages.create(
                    model=model,
                    max_tokens=2048,
                    system=system,
                    tools=TOOL_SCHEMAS,
                    messages=messages,
                )
                assistant_content: list[dict[str, Any]] = []
                tool_results: list[dict[str, Any]] = []
                for block in response.content:
                    if getattr(block, "type", None) == "text":
                        transcript.final_text += block.text
                        assistant_content.append({"type": "text", "text": block.text})
                    elif getattr(block, "type", None) == "tool_use":
                        args = dict(block.input or {})
                        transcript.tool_calls.append(ToolCall(name=block.name, args=args))
                        assistant_content.append(
                            {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
                        )
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": tools.call(block.name, args)[:8000],
                            }
                        )
                if not assistant_content:
                    break
                messages.append({"role": "assistant", "content": assistant_content})
                if response.stop_reason != "tool_use":
                    break
                messages.append({"role": "user", "content": tool_results})
        except Exception as exc:  # a failed call is a failed answer, not a crashed eval
            transcript.error = f"{type(exc).__name__}: {exc}"
    return transcript


def run_arm(client: Any, questions: Sequence[EvalQuestion], *, has_models: bool, model: str = EVAL_MODEL) -> ArmResult:
    arm = "semantic" if has_models else "baseline"
    outcomes = [score(q, run_question(client, q, has_models=has_models, model=model)) for q in questions]
    return ArmResult(arm=arm, outcomes=outcomes)
