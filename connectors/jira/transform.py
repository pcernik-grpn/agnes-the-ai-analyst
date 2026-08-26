"""
Transform raw Jira JSON data into clean Parquet format for analysis.

Extracts key fields from Jira issues including custom fields used by support team.
Converts Atlassian Document Format (ADF) to plain text.
"""

import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from connectors.jira.service import organization_detail_fields, refresh_fields
from src.parquet_publish import atomic_publish

logger = logging.getLogger(__name__)

# Parquet write options applied to every monthly chunk.
# ZSTD offers better compression ratio than Snappy with comparable latency.
# write_statistics=True enables column-level min/max stats used by DuckDB's
# query planner for predicate push-down.  write_page_index=True adds a
# per-column page index (data-page-level statistics) that further narrows
# I/O when filtering on high-cardinality string columns such as issue_key.
PARQUET_WRITE_OPTIONS: dict = {
    "compression": "zstd",
    "write_statistics": True,
    "write_page_index": True,
}

# Hive partition directory name prefix used for all tables.
HIVE_PARTITION_PREFIX = "month"


# Custom field mapping (ID -> human readable name)
# Verified against Jira field configuration (Feb 2026)
CUSTOM_FIELD_NAMES = {
    "customfield_10156": "participants",  # List of users watching/participating
    "customfield_10002": "organizations",  # Organizations
    "customfield_10010": "request_type_info",  # Service Desk request type details
    "customfield_10004": "severity",  # Severity level
    "customfield_10365": "spam",  # Spam flag
    "customfield_10157": "satisfaction",  # Customer satisfaction (was: sla_info)
    "customfield_10323": "triage",  # Triage multi-select (was: team_tier)
    "customfield_10330": "context",  # Context field (was: root_cause)
    "customfield_10325": "custom_url",  # Custom URL (was: resolution_summary)
    "customfield_10350": "slack_link",  # Slack link (was: customer_type)
    "customfield_10475": "email_address",  # Email address (was: context)
    "customfield_10511": "configuration_item",  # Configuration item (was: categories)
    "customfield_10676": "technical_issue_category",  # Technical issue category (was: satisfaction_rating)
    "customfield_10328": "first_response_time",  # SLA: first response time (new)
    "customfield_10161": "time_to_resolution",  # SLA: time to resolution (new)
    "customfield_11831": "l3_team",  # L3 team assignment (new)
}

# Explicit schema definitions for consistent types across monthly chunks
# This prevents DuckDB union errors when some months have all-NULL columns
ISSUES_SCHEMA = {
    "issue_key": "string",
    "issue_id": "string",
    "issue_url": "string",
    "summary": "string",
    "description": "string",
    "issue_type": "string",
    "status": "string",
    "status_category": "string",
    "priority": "string",
    "resolution": "string",
    "project_key": "string",
    "project_name": "string",
    "creator_email": "string",
    "creator_name": "string",
    "reporter_email": "string",
    "reporter_name": "string",
    "assignee_email": "string",
    "assignee_name": "string",
    "created_at": "datetime64[ns, UTC]",
    "updated_at": "datetime64[ns, UTC]",
    "resolved_at": "datetime64[ns, UTC]",
    "due_date": "string",
    "labels": "string",
    "attachment_count": "Int64",
    "comment_count": "Int64",
    "issuelink_count": "Int64",
    "request_type": "string",
    "request_status": "string",
    "severity": "string",
    "triage": "string",
    "configuration_item": "string",
    "participants": "string",
    "organizations": "string",
    # Organization *ids* alongside the names. The names in `organizations` are a
    # point-in-time capture that drifts whenever an organization is renamed, so they
    # cannot be joined on reliably; the ids are stable and are the join key into the
    # `organizations` table. Kept as a JSON array because the Jira field is
    # multi-valued — taking only the first would silently drop organizations.
    "organization_ids": "string",
    "spam": "string",
    "context": "string",
    "custom_url": "string",
    "slack_link": "string",
    "technical_issue_category": "string",
    "email_address": "string",
    "satisfaction": "Int64",
    "l3_team": "string",
    "_synced_at": "string",
    "_raw_file": "string",
}


REFRESH_COLLISION_PREFIX = "cf_"


def resolved_refresh_columns() -> list[tuple[str, str]]:
    """``[(field_id, column), ...]`` for the configured refresh fields, collision-safe.

    The column is the operator's alias (or field id) — kept clean, matching the
    existing custom-field columns. If it would collide with a built-in
    ``ISSUES_SCHEMA`` column it is prefixed with ``REFRESH_COLLISION_PREFIX`` so a
    refresh field can never overwrite a built-in value. A column already used by an
    earlier entry is skipped. Single source of truth for both ``transform_issue``
    and ``issues_schema`` so they never drift.
    """
    reserved = set(ISSUES_SCHEMA)
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for field_id, column in refresh_fields():
        if column in reserved:
            safe = f"{REFRESH_COLLISION_PREFIX}{column}"
            logger.warning(
                "Jira refresh field %s: column %r collides with a built-in issues column; using %r instead",
                field_id,
                column,
                safe,
            )
            column = safe
        if column in seen:
            logger.warning(
                "Jira refresh field %s: column %r already used by another refresh field; skipping",
                field_id,
                column,
            )
            continue
        seen.add(column)
        out.append((field_id, column))
    return out


def issues_schema() -> dict:
    """ISSUES_SCHEMA extended with one string column per configured refresh field.

    The operator's ``JIRA_REFRESH_FIELDS`` columns are appended (as JSON-text
    strings) so ``apply_schema`` keeps them. Resolved at call time (the field list
    comes from the env) and used everywhere the issues parquet is written, so the
    generic columns survive both batch and incremental writes.
    """
    schema = dict(ISSUES_SCHEMA)
    for _, column in resolved_refresh_columns():
        schema.setdefault(column, "string")
    return schema


# Current-state dimension: one row per JSM organization, keyed by the id that
# `issues.organization_ids` carries. Deliberately NOT month-partitioned like the
# event tables — an organization has one current name and one current set of detail
# values, not a history, so it is written as a single parquet.
ORGANIZATIONS_SCHEMA = {
    "org_id": "string",
    "name": "string",
    "_synced_at": "string",
}


def organizations_schema() -> dict:
    """ORGANIZATIONS_SCHEMA extended with one string column per configured detail.

    Mirrors ``issues_schema()``: the operator's ``JIRA_ORG_DETAIL_FIELDS`` columns are
    appended so ``apply_schema`` keeps them. Resolved at call time, and used
    everywhere the organizations parquet is written.
    """
    schema = dict(ORGANIZATIONS_SCHEMA)
    for _, column in organization_detail_fields():
        schema.setdefault(column, "string")
    return schema


def _detail_value(detail: dict) -> str | None:
    """The scalar value of one organization detail, across both CSM response shapes.

    ``GET /organization/{id}`` returns ``{"values": ["ACC-1"]}`` while the batched
    profile endpoints return ``{"value": {"type": "TEXT", "text": ["ACC-1"]}}``. Both
    are arrays; a detail field holds a single value in practice, so the first entry is
    taken and an empty array reads as unset.
    """
    values = detail.get("values")
    if values is None:
        value = detail.get("value")
        values = value.get("text") if isinstance(value, dict) else None
    if not isinstance(values, list) or not values or values[0] is None:
        return None
    return str(values[0])


def extract_organization_details(raw_org: dict) -> dict[str, str | None]:
    """``{column: value}`` for each configured organization detail.

    Each configured key is matched against a detail's ``id`` first and its ``name``
    second. The id is what survives a rename of the detail field — the label is not —
    so it is always preferred when present. The name fallback exists because only
    ``GET /organization/{id}`` returns detail ids at all: the batched
    ``POST /organization/profile/fetch`` and ``GET /organization/details`` responses
    carry ``name`` alone. That also means the id is an observed property of one
    endpoint rather than a documented guarantee, so the fallback is load-bearing, not
    decorative.

    A configured detail that the organization does not carry yields ``None`` rather
    than a missing key, so every row has the same columns.
    """
    configured = organization_detail_fields()
    if not configured:
        return {}

    details = raw_org.get("details")
    details = details if isinstance(details, list) else []

    by_id: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for detail in details:
        if not isinstance(detail, dict):
            continue
        detail_id = detail.get("id")
        if detail_id is not None:
            by_id.setdefault(str(detail_id), detail)
        detail_name = detail.get("name")
        if detail_name is not None:
            by_name.setdefault(str(detail_name), detail)

    out: dict[str, str | None] = {}
    for key, column in configured:
        detail = by_id.get(key) or by_name.get(key)
        out[column] = _detail_value(detail) if detail else None
    return out


def transform_organization(raw_org: dict) -> dict:
    """Transform one raw CSM organization into a flat ``organizations`` row."""
    org_id = raw_org.get("id")
    record: dict[str, Any] = {
        "org_id": None if org_id is None else str(org_id),
        "name": raw_org.get("name"),
        "_synced_at": datetime.now(UTC).isoformat(),
    }
    record.update(extract_organization_details(raw_org))
    return record


COMMENTS_SCHEMA = {
    "comment_id": "string",
    "issue_key": "string",
    "author_email": "string",
    "author_name": "string",
    "body": "string",
    "created_at": "datetime64[ns, UTC]",
    "updated_at": "datetime64[ns, UTC]",
    "update_author_email": "string",
    # Three-valued on purpose: True (customer-facing), False (internal note),
    # NULL (neither signal present). See `_comment_public_visibility`.
    "public_visibility": "bool",
}

ATTACHMENTS_SCHEMA = {
    "attachment_id": "string",
    "issue_key": "string",
    "filename": "string",
    "local_path": "string",
    "hierarchical_path": "string",
    "size_bytes": "Int64",
    "mime_type": "string",
    "author_email": "string",
    "created_at": "datetime64[ns, UTC]",
    "content_url": "string",
    "thumbnail_url": "string",
}

CHANGELOG_SCHEMA = {
    "change_id": "string",
    "issue_key": "string",
    "author_email": "string",
    "author_name": "string",
    "field_name": "string",
    "field_type": "string",
    "from_value": "string",
    "to_value": "string",
    "changed_at": "datetime64[ns, UTC]",
}

ISSUELINKS_SCHEMA = {
    "issue_key": "string",
    "link_id": "string",
    "link_type": "string",
    "direction": "string",
    "linked_issue_key": "string",
    "linked_issue_summary": "string",
    "linked_issue_status": "string",
    "linked_issue_priority": "string",
}

REMOTE_LINKS_SCHEMA = {
    "issue_key": "string",
    "remote_link_id": "string",
    "url": "string",
    "title": "string",
    "application_name": "string",
    "application_type": "string",
}


def get_pyarrow_schema(schema_dict: dict) -> pa.Schema:
    """Convert schema dict to PyArrow schema for consistent Parquet types."""
    pa_fields = []
    for col, dtype in schema_dict.items():
        if dtype == "string":
            pa_fields.append(pa.field(col, pa.string()))
        elif dtype.startswith("datetime64"):
            pa_fields.append(pa.field(col, pa.timestamp("us", tz="UTC")))
        elif dtype == "Int64":
            pa_fields.append(pa.field(col, pa.int64()))
        elif dtype == "bool":
            pa_fields.append(pa.field(col, pa.bool_()))
        else:
            pa_fields.append(pa.field(col, pa.string()))
    return pa.schema(pa_fields)


def apply_schema(df: pd.DataFrame, schema: dict) -> pa.Table:
    """
    Apply explicit schema to DataFrame and return PyArrow Table.

    This ensures all monthly chunks have the same column types,
    preventing DuckDB union errors when querying with glob patterns.
    """
    # Ensure all schema columns exist
    for col in schema:
        if col not in df.columns:
            df[col] = None

    # Convert types
    for col, dtype in schema.items():
        if dtype == "string":
            # Convert to string, keeping None as None
            df[col] = df[col].apply(lambda x: str(x) if x is not None and pd.notna(x) else None)
        elif dtype.startswith("datetime64"):
            df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")
        elif dtype == "Int64":
            df[col] = pd.to_numeric(df[col], errors="coerce")
        elif dtype == "bool":
            # Nullable `boolean`, never numpy `bool`: the column is genuinely
            # three-valued and numpy bool cannot hold NA. `astype` also RAISES
            # on a string rather than coercing it, which is the property worth
            # having — a miscoerced flag fails the write loudly instead of
            # landing as a confident, wrong boolean.
            df[col] = df[col].astype("boolean")

    # Reorder columns to match schema
    df = df[[col for col in schema]]

    # Convert to PyArrow with explicit schema
    pa_schema = get_pyarrow_schema(schema)
    return pa.Table.from_pandas(df, schema=pa_schema, preserve_index=False)


#: ADF smart-link nodes. All three carry their target in ``attrs`` and have NO
#: text child at all, so a text-node-only walk emitted nothing for them and a
#: comment whose whole body is one smart link stored as ``''``.
_ADF_CARD_TYPES = frozenset({"inlineCard", "blockCard", "embedCard"})


def _nonempty_str(value: object) -> str | None:
    """``value`` stripped, or ``None`` if it is not a non-blank string.

    One definition of "a usable URL string", because every ADF attribute this
    module reads comes from third-party JSON and may be absent, null or mistyped.
    """
    return stripped if isinstance(value, str) and (stripped := value.strip()) else None


#: Query/fragment parameter names whose VALUE is a credential. Two tiers because
#: the vocabulary is not standardised: a substring match catches every vendor
#: spelling built around a known word (``access_token``, ``refresh_token``,
#: ``mfaManagementToken``, ``X-Amz-Signature``), while short or ambiguous names
#: have to be matched whole or they would fire on innocent params — ``sig``
#: as a substring would redact ``design``, ``auth`` would redact ``author``.
_SECRET_PARAM_SUBSTRINGS = ("token", "jwt", "secret", "password", "passwd", "signature", "credential", "apikey")
_SECRET_PARAM_NAMES = frozenset({"tok", "sig", "key", "pin", "auth", "code", "sdata"})

#: Deliberately a word, not an ellipsis: it survives a CSV round-trip, greps
#: cleanly, and reads as a decision rather than as truncated data.
_REDACTED_VALUE = "REDACTED"

#: Splits on ``&``/``;`` while KEEPING the separators, so a redacted URL differs
#: from the original only in the values removed. One character class, no
#: alternation to backtrack over — linear on adversarial input (security
#: playbook rule 5).
_PARAM_SPLIT_RE = re.compile(r"([&;])")


#: Separators vendors sprinkle through parameter names. Stripping them before the
#: substring match is what makes one entry cover `api_key`, `api-key` and
#: `apiKey`. Only the substring tier is normalised: the exact tier must keep
#: seeing `api_key` as a different name from `key`.
_PARAM_NAME_SEPARATORS = str.maketrans("", "", "_-.")


def _is_secret_param(name: str) -> bool:
    lowered = name.lower()
    if lowered in _SECRET_PARAM_NAMES:
        return True
    squashed = lowered.translate(_PARAM_NAME_SEPARATORS)
    return any(word in squashed for word in _SECRET_PARAM_SUBSTRINGS)


def _redact_param_values(blob: str) -> str:
    """``blob`` (a query string or fragment) with credential values removed."""
    pieces = _PARAM_SPLIT_RE.split(blob)
    for index in range(0, len(pieces), 2):  # odd indices are the separators
        name, equals, _value = pieces[index].partition("=")
        if equals and _is_secret_param(name):
            pieces[index] = f"{name}={_REDACTED_VALUE}"
    return "".join(pieces)


def _redact_url_secrets(url: str) -> str:
    """``url`` with any credential-bearing parameter value replaced.

    Inlining a href verbatim is the whole point of this renderer, but Jira hands
    out URLs that carry bearer credentials in the query string — a Service Desk
    ``unsubscribe?jwt=…`` link is a signed token authorising an action, and these
    columns are distributed to analyst laptops as parquet and read by agents. So
    the URL is kept whole and identifiable and only the *values* go. Everything
    else — path, scheme, ordering, separators, non-secret params — is verbatim.

    The fragment is covered too, because OAuth's implicit flow puts the token
    there rather than in the query.
    """
    head, hash_sep, fragment = url.partition("#")
    base, query_sep, query = head.partition("?")

    out = base
    if query_sep:
        out += query_sep + _redact_param_values(query)
    if hash_sep:
        out += hash_sep + _redact_param_values(fragment)
    return out


def _adf_card_url(node: dict) -> str | None:
    """Target of a smart-link node, or ``None`` if it carries none.

    A blockCard resolved by the Smart Links service can arrive with an embedded
    JSON-LD object instead of a flat ``url`` — same target, different spelling.
    """
    attrs = node.get("attrs")
    if not isinstance(attrs, dict):
        return None
    data = attrs.get("data")
    if not isinstance(data, dict):
        data = {}
    for candidate in (attrs.get("url"), data.get("url"), data.get("@id")):
        if url := _nonempty_str(candidate):
            return _redact_url_secrets(url)
    return None


def _adf_link_href(node: dict) -> str | None:
    """Href of the ``link`` mark on a text node, or ``None`` if it has none."""
    marks = node.get("marks")
    if not isinstance(marks, list):
        return None
    for mark in marks:
        if not isinstance(mark, dict) or mark.get("type") != "link":
            continue
        attrs = mark.get("attrs")
        if isinstance(attrs, dict) and (href := _nonempty_str(attrs.get("href"))):
            return href
    return None


def _without_inferred_scheme(target: str) -> str:
    """``target`` without a leading ``http://`` / ``https://``.

    Two prefixes rather than a URL parser on purpose: the question is only
    whether Jira's autolinker prepended a scheme to text the author typed, and
    ``http://SKILL.md`` is not a URL anyone wants normalised. Only the FIRST
    scheme goes, so a doubled ``https://http://x`` keeps its second one.
    """
    for scheme in ("https://", "http://"):
        if target.startswith(scheme):
            return target[len(scheme) :]
    return target


def _render_adf_link(text: str, href: str) -> str:
    """Render one link-marked text node so the href survives the flattening.

    The anchor text alone is what a text-node walk kept, and for a labelled link
    ("DMD-1943" pointing at a Linear issue) that discards the only copy of the
    target. ``label (href)`` keeps both and still reads as a sentence.

    An autolink is the exception — Jira marks up a URL the author typed by using
    it as its own anchor text, so the parenthesised copy would be pure noise. Two
    spellings of the same link count as one:

    * the href differs only by a trailing slash: emit the href, one copy of a URL
      that is already whole either way;
    * the href is the anchor text plus a scheme Jira inferred: emit the anchor
      text. The scheme is the autolinker's guess, not authored content, and Jira
      guesses freely — it links a bare ``SKILL.md`` to ``http://SKILL.md``.
      Emitting the href there would rewrite a word into a URL that never existed,
      and the text already carries every character of the target.

    Every URL leaves through ``_redact_url_secrets``, but the comparisons above
    run on the RAW href on purpose: redacting first would make an autolinked
    ``?token=`` URL stop matching its own anchor text, and this would emit
    ``<raw url> (<redacted url>)`` — printing the credential it just removed.
    """
    label = text.strip()
    if not label:
        return _redact_url_secrets(href)
    bare_label = label.rstrip("/")
    if bare_label == href.rstrip("/"):
        return _redact_url_secrets(href)
    if bare_label == _without_inferred_scheme(href).rstrip("/"):
        # This branch fires only when the label IS the target, so the label is a
        # URL and earns the same redaction rather than an exemption.
        return _redact_url_secrets(label)
    return f"{label} ({_redact_url_secrets(href)})"


def _join_adf_parts(parts: list[str]) -> str:
    """Join fragments, dropping the ones that render to nothing.

    Only fragment *edges* are stripped, never a ``codeBlock``'s interior.
    """
    return " ".join([stripped for part in parts if (stripped := part.strip())])


def extract_text_from_adf(node: dict | list | None) -> str:
    """
    Extract plain text from Atlassian Document Format (ADF) content.

    ADF is a nested JSON structure used by Jira for rich text.
    This function recursively extracts all text content.

    URLs are inlined rather than dropped: a smart-link node keeps its target in
    ``attrs``, a ``link`` mark keeps its in ``marks[].attrs.href``, and a walk
    over ``text`` nodes alone saw neither. ``_adf_card_url`` and
    ``_render_adf_link`` carry the per-construct rules.
    """
    if node is None:
        return ""

    if isinstance(node, str):
        return node

    if isinstance(node, list):
        return _join_adf_parts([extract_text_from_adf(item) for item in node])

    if not isinstance(node, dict):
        return ""

    # Get text from this node
    text_parts = []

    # Smart links: no text child, target lives in attrs. Emitted as a bare token
    # in the sentence flow, taking the spacing a word in that position would get.
    if node.get("type") in _ADF_CARD_TYPES:
        card_url = _adf_card_url(node)
        if card_url:
            text_parts.append(card_url)

    # Direct text content. A missing key and an explicit null both fall out of
    # the isinstance guard, so it stands in for the old `"text" in node` too.
    text = node.get("text")
    if isinstance(text, str):
        href = _adf_link_href(node)
        text_parts.append(_render_adf_link(text, href) if href else text)

    # Recursive content
    if "content" in node:
        text_parts.append(extract_text_from_adf(node["content"]))

    return _join_adf_parts(text_parts)


def extract_user_info(user: dict | None) -> dict:
    """Extract key user information from Jira user object."""
    if not user:
        return {"email": None, "name": None, "account_id": None}

    return {
        "email": user.get("emailAddress"),
        "name": user.get("displayName"),
        "account_id": user.get("accountId"),
    }


def extract_option_value(field: Any) -> str | None:
    """Extract value from Jira option field (select/radio)."""
    if field is None:
        return None
    if isinstance(field, dict):
        return field.get("value") or field.get("name")
    return str(field)


def extract_option_list(field: Any) -> list[str]:
    """Extract values from Jira multi-select field."""
    if not field or not isinstance(field, list):
        return []
    values = (extract_option_value(item) for item in field if item)
    # An option dict carrying neither `value` nor `name` resolves to None; skip
    # it rather than landing a JSON `null` inside the serialized array.
    return [value for value in values if value is not None]


def extract_organization_ids(field: Any) -> list[str]:
    """Ids from the Jira organizations field, in payload order.

    The field is multi-valued and each entry carries ``id``, ``uuid`` and ``name``;
    only the id is taken, as the stable join key into the ``organizations`` table.
    Entries with no id are skipped rather than emitting a null, so the array holds
    only usable keys. Ids are stringified because the column is text and Jira has
    returned them both quoted and bare depending on the endpoint.
    """
    if not field or not isinstance(field, list):
        return []
    out: list[str] = []
    for item in field:
        if not isinstance(item, dict):
            continue
        org_id = item.get("id")
        if org_id is None:
            continue
        out.append(str(org_id))
    return out


def parse_datetime(dt_str: str | None) -> datetime | None:
    """Parse Jira datetime string to datetime object."""
    if not dt_str:
        return None
    try:
        # Jira format: "2026-02-03T12:06:52.829+0100"
        # Remove milliseconds and parse
        dt_str = re.sub(r"\.\d+", "", dt_str)
        return datetime.fromisoformat(dt_str)
    except (ValueError, TypeError):
        return None


def transform_issue(raw_issue: dict) -> dict:
    """
    Transform a single raw Jira issue into clean format.

    Returns a flat dictionary suitable for DataFrame conversion.
    """
    fields = raw_issue.get("fields", {})

    # Extract user info
    creator = extract_user_info(fields.get("creator"))
    reporter = extract_user_info(fields.get("reporter"))
    assignee = extract_user_info(fields.get("assignee"))

    # Extract request type info from Service Desk field
    request_type_info = fields.get("customfield_10010", {}) or {}
    request_type = request_type_info.get("requestType", {}) or {}
    current_status = request_type_info.get("currentStatus", {}) or {}

    # Build clean record
    record = {
        # Core identifiers
        "issue_key": raw_issue.get("key"),
        "issue_id": raw_issue.get("id"),
        "issue_url": f"https://{os.environ.get('JIRA_DOMAIN', 'your-org.atlassian.net')}/browse/{raw_issue.get('key')}",
        # Standard fields
        "summary": fields.get("summary"),
        "description": extract_text_from_adf(fields.get("description")),
        "issue_type": fields.get("issuetype", {}).get("name") if fields.get("issuetype") else None,
        "status": fields.get("status", {}).get("name") if fields.get("status") else None,
        "status_category": fields.get("status", {}).get("statusCategory", {}).get("name")
        if fields.get("status")
        else None,
        "priority": fields.get("priority", {}).get("name") if fields.get("priority") else None,
        "resolution": fields.get("resolution", {}).get("name") if fields.get("resolution") else None,
        # Project
        "project_key": fields.get("project", {}).get("key") if fields.get("project") else None,
        "project_name": fields.get("project", {}).get("name") if fields.get("project") else None,
        # People
        "creator_email": creator["email"],
        "creator_name": creator["name"],
        "reporter_email": reporter["email"],
        "reporter_name": reporter["name"],
        "assignee_email": assignee["email"],
        "assignee_name": assignee["name"],
        # Dates
        "created_at": parse_datetime(fields.get("created")),
        "updated_at": parse_datetime(fields.get("updated")),
        "resolved_at": parse_datetime(fields.get("resolutiondate")),
        "due_date": fields.get("duedate"),
        # Arrays as JSON strings for Parquet compatibility
        "labels": json.dumps(fields.get("labels", [])),
        # Counts
        "attachment_count": len(fields.get("attachment", [])),
        "comment_count": fields.get("comment", {}).get("total", 0),
        "issuelink_count": len(fields.get("issuelinks", [])),
        # Service Desk specific
        "request_type": request_type.get("name"),
        "request_status": current_status.get("status"),
        # Custom fields (verified against Jira field configuration Feb 2026)
        "severity": extract_option_value(fields.get("customfield_10004")),
        "triage": json.dumps(extract_option_list(fields.get("customfield_10323"))),
        "configuration_item": json.dumps(extract_option_list(fields.get("customfield_10511"))),
        "participants": json.dumps(
            [extract_user_info(u).get("email") for u in (fields.get("customfield_10156") or [])]
        ),
        "organizations": json.dumps(extract_option_list(fields.get("customfield_10002"))),
        "organization_ids": json.dumps(extract_organization_ids(fields.get("customfield_10002"))),
        "spam": extract_option_value(fields.get("customfield_10365")),
        "context": extract_text_from_adf(fields.get("customfield_10330")) or None,
        "custom_url": fields.get("customfield_10325"),
        "slack_link": extract_option_value(fields.get("customfield_10350")),
        "technical_issue_category": extract_option_value(fields.get("customfield_10676")),
        "email_address": extract_option_value(fields.get("customfield_10475")),
        "satisfaction": fields.get("customfield_10157", {}).get("rating")
        if isinstance(fields.get("customfield_10157"), dict)
        else None,
        "l3_team": extract_option_value(fields.get("customfield_11831")),
        # Metadata
        "_synced_at": raw_issue.get("_synced_at"),
        "_raw_file": None,  # Will be set by caller
    }

    # Generic operator-configured fields, refreshed onto the ticket. Each is stored
    # as JSON text in its own column on `issues` (column name from refresh_fields()).
    for field_id, column in resolved_refresh_columns():
        value = fields.get(field_id)
        record[column] = json.dumps(value, default=str) if value is not None else None

    return record


# JSM stores a comment's customer-facing/internal state in the
# `sd.public.comment` entity property; `jsdPublic` is the platform API's
# documented read-only projection of it, and the only signal worth reading
# here — it rides along on the comments embedded in a plain `GET /issue/{key}`
# (no `expand`, no second request), so this transform stays pure and the fetch
# layer is untouched. Audited live: present as a JSON boolean on every comment
# observed — a project-wide sweep of each issue's newest-20 comment window
# (112,859 comments, 2022-2026; that is what `search/jql?fields=comment`
# actually embeds) plus full page-throughs of the longest threads covering the
# older tails the sweep cannot see (697/697, including every comment past the
# 100-comment embed and 2022-era thread heads). Zero string-typed and zero
# explicit-null values anywhere.
#
# The entity property is deliberately NOT consulted. Reading it would need
# `expand=properties`, and any payload carrying the property carries
# `jsdPublic` too (120/120 where both could appear), so a property branch is
# unreachable by construction rather than merely unused. If you ever do add
# `expand=properties`, know that `value.internal` is NOT consistently typed —
# the same instance stores both a JSON boolean and the string "false" — so
# coerce it by content. A plain `bool()` reads any non-empty string as truthy
# and would silently flip a public comment to internal.
#
# `jsdPublic` is a Jira CLOUD PLATFORM field, not a JSM-licensed one. Live
# verification (2026-08-19) found it serialized as a boolean on 100% of comments
# on two JSM-UNLICENSED Cloud sites as well as a licensed one, and Atlassian's
# v2/v3 OpenAPI schema says as much: it "defaults to true ... when the site
# doesn't use Jira Service Desk". Licensing gates the VALUE (`false` occurs only
# in service-desk projects), never the key's presence. Jira Data Center/Server
# lacks the field entirely — and cannot run this connector at all, which
# hardcodes `/rest/api/3`. So a NULL here is an anomaly worth a WARNING on every
# deployment this code can reach.
#
# Reports of `jsdPublic` being wrong (JSDCLOUD-7997, -8275, -6050) concern
# WEBHOOK payloads and WRITE attempts. This connector reads GET refetches on
# every path but one: `process_webhook_event`'s fetch-failure fallback
# (service.py) transforms the webhook body's own comments when the refetch fails
# and the embedded thread is length-complete. Whether Cloud webhook bodies
# serialize the flag is the one shape nobody has sampled — which is why the
# incremental write layer no longer depends on the answer: it carries a stored
# same-version value forward rather than letting an unflagged comment null one
# (`incremental_transform._carry_forward_public_visibility`).


def _comment_public_visibility(comment: dict) -> bool | None:
    """Is this comment customer-facing? ``None`` unless the flag is a real boolean.

    Deliberately never defaults, and deliberately trusts nothing but a JSON
    boolean. ``bool()`` reads any non-empty string as truthy — ``bool("false")``
    is ``True``, the exact miscoercion documented above for ``value.internal``,
    which this same instance demonstrably stores as the string ``"false"`` — so
    a mistyped flag resolves to NULL (and is counted in the caller's WARNING)
    instead of landing as a confidently wrong ``true``. A missing flag is
    genuinely unknown, and a boolean column that is confidently wrong is
    strictly worse than one that admits the gap — nothing downstream can
    distinguish a defaulted ``true`` from an observed one. A sibling ingest of
    this same field defaulted instead: it holds zero NULLs across 120k rows,
    and spot-checking it against live Jira finds 25-30% of every pre-2025 month
    wrong, every error in the same direction (stored public, actually
    internal).
    """
    jsd_public = comment.get("jsdPublic")
    return jsd_public if isinstance(jsd_public, bool) else None


def transform_comments(
    raw_issue: dict, *, preserve_on_incomplete: bool = True, warn_unresolved: bool = True
) -> list[dict] | None:
    """Extract and transform comments from an issue.

    Args:
        raw_issue: raw Jira issue JSON.
        preserve_on_incomplete: whether the ``_comments_incomplete`` marker
            should suppress the (known-truncated) embedded list. Defaults to
            True, which is the correct behaviour for the INCREMENTAL path
            only. Full-rebuild callers pass False — see below.
        warn_unresolved: whether comments lacking a boolean ``jsdPublic``
            are logged. Callers running this transform on a throwaway pass
            (the batch grouping pass in ``incremental_transform.transform_issues``,
            which rebuilds the same payloads under the month lock right after)
            pass False so the same gap is not logged twice per issue per cycle.

    Returns:
      - list[dict]: transformed comment records. May be empty — the issue
        legitimately has no comments.
      - None: only when ``preserve_on_incomplete`` is True and
        ``_comments_incomplete`` is set on ``raw_issue``, meaning
        ``complete_issue_comments`` (connectors/jira/service.py) hit a
        page-fetch failure mid-pagination and ``fields.comment.comments``
        is a KNOWN-TRUNCATED subset of the full thread. This is the same
        overlay-absent contract as ``transform_remote_links``: callers that
        upsert onto an issue-scoped delete-then-insert store (the
        incremental comments parquet) MUST treat ``None`` as "skip the
        upsert, preserve existing rows" — otherwise a transient pagination
        failure on a refetch would overwrite a previously-complete stored
        comment thread with a known-incomplete one.

    The preserve semantics only make sense where there are existing rows to
    preserve. ``transform_all`` rebuilds the monthly parquets from scratch,
    so suppressing there means the issue contributes ZERO comment rows —
    strictly worse than writing the partially-fetched list the JSON already
    carries. Batch/full-rebuild callers therefore pass
    ``preserve_on_incomplete=False`` and get the partial list.
    """
    if preserve_on_incomplete and comments_are_incomplete(raw_issue):
        return None

    issue_key = raw_issue.get("key")
    fields = raw_issue.get("fields", {})
    comments_data = fields.get("comment", {})
    comments = comments_data.get("comments", [])

    records = []
    unresolved = 0
    for comment in comments:
        author = extract_user_info(comment.get("author"))
        update_author = extract_user_info(comment.get("updateAuthor"))
        public_visibility = _comment_public_visibility(comment)
        if public_visibility is None:
            unresolved += 1

        records.append(
            {
                "comment_id": comment.get("id"),
                "issue_key": issue_key,
                "author_email": author["email"],
                "author_name": author["name"],
                "body": extract_text_from_adf(comment.get("body")),
                "created_at": parse_datetime(comment.get("created")),
                "updated_at": parse_datetime(comment.get("updated")),
                "update_author_email": update_author["email"],
                "public_visibility": public_visibility,
            }
        )

    if unresolved and warn_unresolved:
        # Counted, not defaulted — the operator needs to see the size of the gap
        # rather than have it disappear into a plausible-looking `true`. On every
        # deployment this connector can run against, `jsdPublic` is a platform
        # field present on every comment, so a non-zero count here is a genuine
        # anomaly and this is the only signal of it.
        #
        # Worded as what ARRIVED, not as what is stored: this count is taken
        # before the incremental write layer gets a chance to carry a stored
        # same-version value forward (`incremental_transform._comment_records`),
        # so the number of rows that actually end up NULL may be lower. "No
        # boolean jsdPublic" covers all three unresolved shapes — absent,
        # explicit null, and mistyped.
        logger.warning(
            "Jira issue %s: %d of %d comments arrived without a boolean jsdPublic — public_visibility is NULL "
            "here; the incremental write layer may still carry a stored same-version value forward",
            issue_key,
            unresolved,
            len(comments),
        )

    return records


def transform_attachments(raw_issue: dict, attachments_dir: Path | None = None) -> list[dict]:
    """Extract and transform attachments from an issue."""
    issue_key = raw_issue.get("key")
    fields = raw_issue.get("fields", {})
    attachments = fields.get("attachment", [])

    records = []
    for att in attachments:
        author = extract_user_info(att.get("author"))
        att_id = att.get("id")
        filename = att.get("filename")

        # Check if local file exists
        local_path = None
        if attachments_dir and issue_key:
            expected_path = attachments_dir / issue_key / f"{att_id}_{filename}"
            if expected_path.exists():
                local_path = str(expected_path)

        records.append(
            {
                "attachment_id": att_id,
                "issue_key": issue_key,
                "filename": filename,
                "local_path": local_path,
                "size_bytes": att.get("size"),
                "mime_type": att.get("mimeType"),
                "author_email": author["email"],
                "created_at": parse_datetime(att.get("created")),
                "content_url": att.get("content"),
                "thumbnail_url": att.get("thumbnail"),
            }
        )

    return records


def transform_changelog(raw_issue: dict) -> list[dict] | None:
    """Extract and transform changelog entries from an issue.

    Returns:
      - list[dict]: fresh records to upsert. May be empty — ``{"histories": []}``
        is a successful fetch confirming the issue has no history right now.
      - None: the ``changelog`` key was absent entirely, so this payload carries
        no fresh history AT ALL. Callers that upsert onto an issue-scoped
        delete-then-insert store MUST treat ``None`` as "skip the upsert, preserve
        existing rows" — the identical contract ``transform_remote_links``
        carries, and for the identical reason.

    The key shape is set by the writers: ``JiraService.fetch_issue`` and the
    backfill both request ``expand=renderedFields,changelog``, so a successful
    fetch always carries the key. The one writer that does not is
    ``process_webhook_event``'s fetch-failure fallback, whose payload is the
    event body Jira embedded — and Jira has never embedded a changelog there. So
    every such save wrote zero changelog rows over the issue's stored history,
    deleting it, on a path that runs precisely when a refetch has already failed.
    """
    issue_key = raw_issue.get("key")
    # Absent and explicit-null both mean "no fresh data", mirroring
    # `transform_remote_links` — absent is the writers' contract, explicit null
    # the defensive case where a JSON edit stored one.
    changelog = raw_issue.get("changelog")
    if changelog is None:
        return None
    histories = changelog.get("histories", [])

    records = []
    for history in histories:
        author = extract_user_info(history.get("author"))
        changed_at = parse_datetime(history.get("created"))

        for item in history.get("items", []):
            records.append(
                {
                    "change_id": history.get("id"),
                    "issue_key": issue_key,
                    "author_email": author["email"],
                    "author_name": author["name"],
                    "field_name": item.get("field"),
                    "field_type": item.get("fieldtype"),
                    "from_value": item.get("fromString"),
                    "to_value": item.get("toString"),
                    "changed_at": changed_at,
                }
            )

    return records


def transform_issuelinks(raw_issue: dict) -> list[dict]:
    """Extract and transform issue links from an issue."""
    issue_key = raw_issue.get("key")
    fields = raw_issue.get("fields", {})
    issuelinks = fields.get("issuelinks", [])

    records = []
    for link in issuelinks:
        link_type = link.get("type", {})
        link_type_name = link_type.get("name", "")

        # Each link has either inwardIssue or outwardIssue
        if "inwardIssue" in link:
            linked = link["inwardIssue"]
            direction = "inward"
        elif "outwardIssue" in link:
            linked = link["outwardIssue"]
            direction = "outward"
        else:
            continue

        linked_fields = linked.get("fields", {})
        records.append(
            {
                "issue_key": issue_key,
                "link_id": link.get("id"),
                "link_type": link_type_name,
                "direction": direction,
                "linked_issue_key": linked.get("key"),
                "linked_issue_summary": linked_fields.get("summary"),
                "linked_issue_status": linked_fields.get("status", {}).get("name")
                if linked_fields.get("status")
                else None,
                "linked_issue_priority": linked_fields.get("priority", {}).get("name")
                if linked_fields.get("priority")
                else None,
            }
        )

    return records


def transform_remote_links(raw_issue: dict) -> list[dict] | None:
    """Extract and transform remote links from an issue.

    Returns:
      - list[dict]: fresh records to upsert into parquet. May be empty,
        meaning the issue legitimately has no remote links right now
        (HTTP 200 with [] or HTTP 404 from the fetch).
      - None: the _remote_links key was absent from raw_issue, which
        signals that save_issue (or the equivalent backfill writer)
        could not refresh remote links — typically a 401/403/5xx from
        the Jira API. Callers MUST treat None as "skip the upsert";
        overwriting with [] would delete existing parquet rows for
        this issue.

    The key shape is set by the writers (JiraService.save_issue,
    JiraBackfiller, backfill_remote_links): present means the fetch
    succeeded (200 or 404), absent means the fetch raised.
    """
    issue_key = raw_issue.get("key")
    # Treat both absent key and explicit None as the "no fresh data" signal —
    # absent is the contract from save_issue/backfill writers, None is the
    # defensive case where a JSON edit or older buggy code stored an explicit
    # null (would otherwise blow up on `for rl in None`).
    remote_links = raw_issue.get("_remote_links")
    if remote_links is None:
        return None

    records = []
    for rl in remote_links:
        obj = rl.get("object", {})
        app = rl.get("application", {})
        records.append(
            {
                "issue_key": issue_key,
                "remote_link_id": str(rl.get("id", "")),
                "url": obj.get("url"),
                "title": obj.get("title"),
                "application_name": app.get("name"),
                "application_type": app.get("type"),
            }
        )

    return records


def write_parquet_atomic(table: pa.Table, dest: Path) -> Path:
    """Publish *table* at *dest* so no reader can observe a partial parquet.

    This is not a cosmetic nicety. A direct ``pq.write_table(table, dest)``
    leaves a footerless file when the process dies midway (deploy, OOM,
    restart), and an unreadable partition does not stay a read error: the next
    read-modify-write treats it as EMPTY — ``load_parquet_month`` logs a warning
    and returns ``None``, ``upsert_dataframe`` then keeps only the incoming
    record — so the whole month is republished with a single row. The SLA poller
    revisits only tickets whose ``status_category != 'Done'``, a small minority
    of any month, so the rest never come back without an operator re-running the
    batch transform.

    The actual publish mechanism — per-process temp, ``chmod 0644``,
    ``os.replace``, cleanup-on-failure-not-``finally`` — moved to
    `src.parquet_publish.atomic_publish` (#1359), shared by every other
    extract-layout writer; see that module's docstring for the full mechanism
    and the #1274 / #203 incidents behind it. This function keeps its name and
    two-argument shape so its callers (`write_hive_parquet`,
    `connectors/jira/organizations.py`, `connectors/jira/incremental_transform.py`)
    and tests are undisturbed, and keeps Jira's own `PARQUET_WRITE_OPTIONS`
    (ZSTD + page index + statistics) exactly as they were — the shared helper
    has no opinion on compression, so moving to it does not silently
    re-compress Jira's partitions to whatever another caller uses.
    """
    dest = Path(dest)
    with atomic_publish(dest) as tmp_dest:
        pq.write_table(table, tmp_dest, **PARQUET_WRITE_OPTIONS)
    return dest


def write_hive_parquet(table: pa.Table, table_dir: Path, month_key: str) -> Path:
    """Write a PyArrow table to the hive-partitioned layout.

    Creates ``table_dir/month=<month_key>/data.parquet`` with ZSTD compression
    and column statistics enabled.  Returns the path to the written file.

    Published via :func:`write_parquet_atomic` — the extract views glob this
    directory on every query, including mid-write.
    """
    hive_dir = table_dir / f"{HIVE_PARTITION_PREFIX}={month_key}"
    return write_parquet_atomic(table, hive_dir / "data.parquet")


def get_month_key(dt: datetime | None) -> str:
    """Get month key (YYYY-MM) from datetime, defaulting to current month."""
    if dt is None:
        dt = datetime.now(UTC)
    return dt.strftime("%Y-%m")


def get_attachment_path(issue_key: str, attachment_id: str, filename: str) -> str:
    """
    Generate hierarchical attachment path.

    SUPPORT-14991 -> 14/991/54908_files.zip
    """
    # Extract number from issue key (e.g., "SUPPORT-14991" -> "14991")
    match = re.search(r"(\d+)$", issue_key)
    if not match:
        return f"other/{issue_key}/{attachment_id}_{filename}"

    num = match.group(1)
    # Split into prefix (thousands) and suffix (rest)
    prefix = num[:-3] if len(num) > 3 else "0"
    suffix = num[-3:] if len(num) >= 3 else num

    return f"{prefix}/{suffix}/{attachment_id}_{filename}"


def is_deleted(raw_issue: dict) -> bool:
    """True when a deletion webhook has marked this issue's stored JSON.

    `service.py` handles an issue-deleted webhook by stamping `_deleted_at`
    into the JSON and removing the issue's parquet rows; the file itself is
    kept for audit. Every consumer that republishes from raw JSON must honor
    the marker: deletion webhooks fire once and the consistency check skips
    marked JSONs, so a resurrected row would never be re-deleted and would be
    invisible to the checker.

    ``.get``, not ``in``: an explicit ``null`` counts as not-deleted, matching
    the pre-existing reader in the consistency check; this helper is what
    keeps the next one from drifting.
    """
    return bool(raw_issue.get("_deleted_at"))


def comments_are_incomplete(raw_issue: dict) -> bool:
    """True when this issue's embedded comment list is a KNOWN-TRUNCATED subset.

    `complete_issue_comments` (service.py) sets the ``_comments_incomplete``
    marker after a page-fetch failure mid-pagination. This helper is the single
    source for the marker's semantics: `transform_comments` consults it for its
    preserve-on-incomplete contract, and payload builders test it directly
    instead of running the whole comment transform a second time just to see
    whether it answers ``None``.
    """
    return raw_issue.get("_comments_incomplete") is True


def transform_all(
    raw_dir: Path,
    output_dir: Path,
    attachments_dir: Path | None = None,
) -> dict[str, int]:
    """
    Transform all raw Jira JSON files into monthly Parquet chunks.

    Output structure (hive-partitioned layout):
        output_dir/
        ├── issues/
        │   ├── month=2025-01/
        │   │   └── data.parquet
        │   └── month=2026-02/
        │       └── data.parquet
        ├── comments/
        │   └── month=YYYY-MM/data.parquet  ...
        ├── changelog/
        │   └── ...
        ├── attachments/
        │   └── ...  (metadata only)
        └── attachments_files/
            └── 14/991/54908_files.zip  (hierarchical)

    Args:
        raw_dir: Directory containing raw JSON files (issues/*.json)
        output_dir: Directory for output Parquet files
        attachments_dir: Directory containing downloaded attachments

    Returns:
        Dict with counts of records per table
    """
    issues_dir = raw_dir / "issues"
    if not issues_dir.exists():
        logger.error(f"Issues directory not found: {issues_dir}")
        return {}

    # Collect records grouped by month (based on issue created_at)
    issues_by_month: dict[str, list] = {}
    comments_by_month: dict[str, list] = {}
    attachments_by_month: dict[str, list] = {}
    changelog_by_month: dict[str, list] = {}
    issuelinks_by_month: dict[str, list] = {}
    remote_links_by_month: dict[str, list] = {}

    # Process each issue file
    json_files = list(issues_dir.glob("*.json"))
    logger.info(f"Processing {len(json_files)} issue files...")

    deleted_by_month: dict[str, int] = {}
    for json_file in json_files:
        try:
            with open(json_file) as f:
                raw_issue = json.load(f)

            # A rebuild republishing deleted issues would resurrect rows nothing
            # ever re-deletes — the full contract lives on `is_deleted`. Tracked
            # by month so the all-deleted case below can be called out.
            if is_deleted(raw_issue):
                month_key = get_month_key(parse_datetime(raw_issue.get("fields", {}).get("created")))
                deleted_by_month[month_key] = deleted_by_month.get(month_key, 0) + 1
                continue

            # Transform issue
            issue_record = transform_issue(raw_issue)
            issue_record["_raw_file"] = json_file.name

            # Determine month key based on issue creation date
            month_key = get_month_key(issue_record.get("created_at"))

            # Add to month bucket
            if month_key not in issues_by_month:
                issues_by_month[month_key] = []
                comments_by_month[month_key] = []
                attachments_by_month[month_key] = []
                changelog_by_month[month_key] = []
                issuelinks_by_month[month_key] = []
                remote_links_by_month[month_key] = []

            issues_by_month[month_key].append(issue_record)

            # Transform related data (all go to same month as parent issue)
            # This is a full rebuild from raw JSON — nothing is being preserved,
            # so an issue marked `_comments_incomplete` still contributes the
            # partially-fetched comments its JSON carries. Dropping them would
            # write ZERO rows for that issue, which is strictly worse than a
            # short thread; the preserve semantics belong to the incremental
            # delete-then-insert path (incremental_transform.py) alone.
            comment_records = transform_comments(raw_issue, preserve_on_incomplete=False) or []
            comments_by_month[month_key].extend(comment_records)

            # Transform attachments with hierarchical paths
            issue_key = raw_issue.get("key", "unknown")
            for att_record in transform_attachments(raw_issue, attachments_dir):
                # Update local_path to hierarchical structure
                if att_record.get("local_path"):
                    att_record["hierarchical_path"] = get_attachment_path(
                        issue_key, att_record["attachment_id"], att_record["filename"]
                    )
                attachments_by_month[month_key].append(att_record)

            changelog_records = transform_changelog(raw_issue)
            if changelog_records is not None:
                changelog_by_month[month_key].extend(changelog_records)
            # else: same story as `_remote_links` below — the payload carries no
            # changelog overlay. A rebuild has nothing to preserve, so the issue
            # simply contributes no changelog rows. The guard is not cosmetic:
            # `extend(None)` raises, and the per-file handler below is a blind
            # `except`, so an unguarded version would drop EVERY table's rows for
            # that issue while the run still reported success.
            issuelinks_by_month[month_key].extend(transform_issuelinks(raw_issue))
            rl_records = transform_remote_links(raw_issue)
            if rl_records is not None:
                remote_links_by_month[month_key].extend(rl_records)
            # else: _remote_links overlay was skipped (fetch failure). The batch
            # rebuild writes monthly parquets from scratch, so this issue simply
            # contributes no rows to the rebuild — it doesn't "preserve" anything.
            # A re-run after the outage clears will repopulate. The incremental
            # path (incremental_transform.py) is what genuinely preserves
            # existing rows; batch mode is full-rebuild and not the hot path.

        # Blind on purpose: per-file fault isolation — one malformed JSON must
        # not sink the whole rebuild, and every skip is logged with its filename.
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error processing {json_file}: {e}")

    if deleted_by_month:
        logger.info(f"Skipped {sum(deleted_by_month.values())} issue(s) marked _deleted_at")
    # A month whose issues are since ALL deleted never reaches the write loop, so
    # whatever is on disk for it — including a corrupt partition an operator came
    # here to repair — is left untouched. Rewriting it is not an option (the only
    # rows we could publish are deleted issues'), so say what the remedy is
    # instead of exiting 0 and letting "repaired" be assumed.
    for month_key in sorted(set(deleted_by_month) - set(issues_by_month)):
        logger.warning(
            f"month={month_key}: all {deleted_by_month[month_key]} issue(s) are deleted; "
            f"existing partitions are left untouched. If you are repairing a corrupt "
            f"partition in this month, remove the file instead — every row it held "
            f"belongs to a deleted issue."
        )

    # Create output directories
    (output_dir / "issues").mkdir(parents=True, exist_ok=True)
    (output_dir / "comments").mkdir(parents=True, exist_ok=True)
    (output_dir / "attachments").mkdir(parents=True, exist_ok=True)
    (output_dir / "changelog").mkdir(parents=True, exist_ok=True)
    (output_dir / "issuelinks").mkdir(parents=True, exist_ok=True)
    (output_dir / "remote_links").mkdir(parents=True, exist_ok=True)

    # Save to monthly Parquet files
    counts = {"issues": 0, "comments": 0, "attachments": 0, "changelog": 0, "issuelinks": 0, "remote_links": 0}

    for month_key in sorted(issues_by_month.keys()):
        # Issues
        if issues_by_month[month_key]:
            table = apply_schema(pd.DataFrame(issues_by_month[month_key]), issues_schema())
            write_hive_parquet(table, output_dir / "issues", month_key)
            counts["issues"] += table.num_rows
            logger.info(f"Saved {table.num_rows} issues to issues/month={month_key}/data.parquet")

        # Comments
        if comments_by_month[month_key]:
            table = apply_schema(pd.DataFrame(comments_by_month[month_key]), COMMENTS_SCHEMA)
            write_hive_parquet(table, output_dir / "comments", month_key)
            counts["comments"] += table.num_rows
            logger.info(f"Saved {table.num_rows} comments to comments/month={month_key}/data.parquet")

        # Attachments (metadata)
        if attachments_by_month[month_key]:
            table = apply_schema(pd.DataFrame(attachments_by_month[month_key]), ATTACHMENTS_SCHEMA)
            write_hive_parquet(table, output_dir / "attachments", month_key)
            counts["attachments"] += table.num_rows
            logger.info(f"Saved {table.num_rows} attachments to attachments/month={month_key}/data.parquet")

        # Changelog
        if changelog_by_month[month_key]:
            table = apply_schema(pd.DataFrame(changelog_by_month[month_key]), CHANGELOG_SCHEMA)
            write_hive_parquet(table, output_dir / "changelog", month_key)
            counts["changelog"] += table.num_rows
            logger.info(f"Saved {table.num_rows} changelog entries to changelog/month={month_key}/data.parquet")

        # Issue links
        if issuelinks_by_month[month_key]:
            table = apply_schema(pd.DataFrame(issuelinks_by_month[month_key]), ISSUELINKS_SCHEMA)
            write_hive_parquet(table, output_dir / "issuelinks", month_key)
            counts["issuelinks"] += table.num_rows
            logger.info(f"Saved {table.num_rows} issue links to issuelinks/month={month_key}/data.parquet")

        # Remote links
        if remote_links_by_month[month_key]:
            table = apply_schema(pd.DataFrame(remote_links_by_month[month_key]), REMOTE_LINKS_SCHEMA)
            write_hive_parquet(table, output_dir / "remote_links", month_key)
            counts["remote_links"] += table.num_rows
            logger.info(f"Saved {table.num_rows} remote links to remote_links/month={month_key}/data.parquet")

    logger.info(f"Created monthly chunks for {len(issues_by_month)} months")
    return counts


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Transform raw Jira JSON to Parquet")
    parser.add_argument("--raw-dir", type=Path, required=True, help="Directory with raw JSON files")
    parser.add_argument("--output-dir", type=Path, required=True, help="Output directory for Parquet files")
    parser.add_argument("--attachments-dir", type=Path, help="Directory with downloaded attachments")

    args = parser.parse_args()

    counts = transform_all(
        raw_dir=args.raw_dir,
        output_dir=args.output_dir,
        attachments_dir=args.attachments_dir,
    )

    print(f"\nTransformation complete: {counts}")
