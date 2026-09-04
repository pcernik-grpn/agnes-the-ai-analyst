"""Shared CLI renderer for HTTP error responses.

Two CLI paths surface BigQuery / guardrail / RBAC typed errors today:
- ``agnes query --remote`` (POST /api/query)
- ``agnes snapshot create`` / ``agnes schema`` etc. (cli.v2_client wrappers around v2 endpoints)

Both previously flattened the structured ``detail`` JSON to a
truncated single-line string, hiding the operator-facing hint that
explains how to fix ``USER_PROJECT_DENIED`` / cost-cap rejection /
unregistered ``bq.*`` paths. This module recognizes a few canonical
shapes and pretty-prints them; falls back to truncated form for
anything unrecognized so the renderer never makes a worse-than-
status-quo error message.

Closes #160 §4.7.
"""

from __future__ import annotations

import json
from textwrap import fill
from typing import Any


# Keys that hold long human-readable text — wrap separately so the line
# break is at a word boundary, not mid-key.
_WRAP_KEYS = ("hint", "suggestion")
# Keys to render first in the key/value block (when present); other keys
# follow in declaration order so a future server-side detail addition
# surfaces automatically without a renderer change.
_PRIORITY_KEYS = (
    "kind",
    "reason",
    "table",
    "path",
    "registered_as",
    "billing_project",
    "data_project",
    "scan_bytes",
    "limit_bytes",
    "tables",
    "current",
    "limit",
    "retry_after_seconds",
)

# Table access policies (design doc §16) — reason-keyed error dicts raised
# by every enforcement surface (`/api/query`, `/api/v2/sample`,
# `/api/v2/scan`, the MCP per-table tool). Several of these carry NO
# explanatory key at all on the wire — `policy_identity_unresolvable` is a
# bare ``{"reason": ...}`` at every current call site — so the actionable
# next step an LLM needs to stop retrying the same failure lives here, in
# the CLI, rather than depending on the server payload growing one. Keyed
# by `reason`; injected as a synthetic `hint` (see `_with_access_policy_hint`)
# so it rides the same wrapped-at-80-cols rendering as every other typed
# error's own `hint`/`suggestion`.
_ACCESS_POLICY_HINTS: dict[str, str] = {
    "policy_name_collision": (
        "Rename your CTE or subquery alias — it is spelled identically to a "
        "table this query reads through an access policy, and Agnes refuses "
        "to guess which occurrence you meant."
    ),
    "policy_identity_unresolvable": (
        "This table has a per-user access policy, and the current session "
        "has no single identity to bind it against (e.g. a shared co-drive "
        "session with several participants). Open the table in a solo "
        "session and retry."
    ),
    "policy_mapping_empty": (
        "This is a data problem, not a permissions problem — the access "
        "policy depends on a mapping table that is currently empty or was "
        "never synced. Contact your administrator to check its sync."
    ),
    "access_policy_protected_row": (
        "The table derived from this file carries an access policy, and a "
        "re-ingest would replace the table without it. Ask an administrator "
        "to clear the policy first — retrying the upload will keep failing."
    ),
    "policy_error": (
        "The access policy attached to this table failed to resolve or "
        "execute. Contact your administrator — the underlying engine error "
        "is deliberately withheld here (it can quote values from the "
        "policy body)."
    ),
}


def _with_access_policy_hint(detail: dict) -> dict:
    """Inject the §16 next-step text for a recognized access-policy
    ``reason`` when the server payload doesn't already carry its own
    ``hint``/``suggestion`` — several call sites send a bare
    ``{"reason": ...}`` with nothing else (see ``_ACCESS_POLICY_HINTS``).
    Returns ``detail`` unchanged for every other shape.
    """
    reason = detail.get("reason")
    if reason not in _ACCESS_POLICY_HINTS or detail.get("hint") or detail.get("suggestion"):
        return detail
    return {**detail, "hint": _ACCESS_POLICY_HINTS[reason]}


def render_error(status_code: int, body: Any) -> str:
    """Format an HTTP error body for stderr.

    Recognized shapes (pretty-printed):
    - ``{"detail": {"kind": str, ...}}`` — typed BqAccessError
    - ``{"detail": {"reason": str, ...}}`` — guardrail / RBAC dicts
    - ``{"detail": {"code": str, ...}}`` — store upload/update errors;
      ``code == "validation_failed"`` additionally renders every issue in
      the nested ``checks`` as one actionable line (never truncated — the
      historical flatten-and-truncate hid all but the first issue, forcing
      submitters into a fix-and-retry loop).
    Anything else: fallback ``f"HTTP {status_code}: {str(body)[:500]}"``.
    """
    detail = _detail_dict(body)
    if detail is not None and detail.get("code") == "validation_failed":
        return _format_validation_failed(status_code, detail)
    if detail is not None and ("kind" in detail or "reason" in detail or "code" in detail):
        return _format_dict(status_code, detail)
    if isinstance(body, dict) and isinstance(body.get("detail"), str):
        return _with_session_only_hint(f"HTTP {status_code}: {body['detail']}", body["detail"])
    text = str(body) if not isinstance(body, str) else body
    if len(text) > 500:
        text = text[:497] + "..."
    return f"HTTP {status_code}: {text}"


#: The server's refusals for a PAT (or service token) on a route that
#: requires interactive owner auth — `app/auth/dependencies.py::
#: require_session_token`. Matched on the stable prefix both share.
_SESSION_ONLY_MARKER = "requires an interactive session"


def _with_session_only_hint(rendered: str, detail: str) -> str:
    """Append a way forward to a session-only refusal.

    `agnes agent list/show/create/...` authenticate with the PAT that
    `agnes auth login` stores, and every `/api/v1/agents` management route
    rejects PATs on purpose (a PAT must not be able to mint another PAT or
    re-scope an agent). So those subcommands cannot succeed from a plain
    CLI login, and the bare server sentence reads as a CLI bug rather than
    a deliberate boundary. The runtime call `agnes agent ask` is
    unaffected — it is not a management route.
    """
    if _SESSION_ONLY_MARKER not in detail:
        return rendered
    try:
        from cli.config import get_server_url

        base = get_server_url().rstrip("/")
    except Exception:
        # Never let the error path raise a second error on top of the first.
        return rendered
    return (
        f"{rendered}\n"
        f"  hint: agent management is deliberately web-only — manage agents at "
        f"{base}/agents.\n"
        f"        From the CLI, `agnes agent ask <slug>` and `agnes chat <slug>` "
        f"work with your token."
    )


def _detail_dict(body: Any) -> dict | None:
    """Return ``body['detail']`` when it's a dict, else None."""
    if isinstance(body, dict):
        d = body.get("detail")
        if isinstance(d, dict):
            return d
    return None


def _format_dict(status_code: int, detail: dict) -> str:
    """Multi-line render of a recognized typed-error dict.

    When both `kind` and `reason` are present (e.g. quota rejections at
    `app/api/query.py` carry `{reason: "daily_byte_cap_exceeded",
    kind: "daily_bytes", ...}`), the label line shows only one — the
    other must still appear in the key/value section so its value isn't
    silently dropped. Devin Review iter #4 caught this.
    """
    detail = _with_access_policy_hint(detail)
    label_key = next(
        (k for k in ("kind", "reason", "code") if detail.get(k)),
        None,
    )
    label = detail.get(label_key) if label_key else "error"
    lines: list[str] = [f"Error: {label} (HTTP {status_code})"]

    # Only the key actually used in the label is hidden from the kv block.
    seen: set[str] = {label_key} if label_key else set()
    # Priority keys first. Filter only None — `_kv_line` already renders
    # empty strings as `(empty)`, which is the key diagnostic for
    # `billing_project: ""` in cross_project_forbidden errors. Earlier
    # `not in (None, "")` filter dropped exactly the field the operator
    # needs to see (Devin Review iter #6 on PR #168).
    for key in _PRIORITY_KEYS:
        if key in seen:
            continue
        if key in detail and detail[key] is not None:
            lines.append(_kv_line(key, detail[key]))
            seen.add(key)

    # Anything else not already shown and not a wrap key
    for key, value in detail.items():
        if key in seen or key in _WRAP_KEYS:
            continue
        if value is not None:
            lines.append(_kv_line(key, value))
            seen.add(key)

    # Wrap keys last — they're the long human-readable explanation
    for key in _WRAP_KEYS:
        if key in detail and detail[key]:
            wrapped = fill(
                str(detail[key]),
                width=80,
                initial_indent=f"  {key}: ",
                subsequent_indent="    ",
            )
            lines.append(wrapped)
    return "\n".join(lines)


def _kv_line(key: str, value: Any) -> str:
    """Format one ``  key: value`` line. Lists join with comma; dicts
    json-encode (rare but defensive)."""
    if isinstance(value, list):
        rendered = ", ".join(str(v) for v in value) if value else "(empty)"
    elif isinstance(value, dict):
        rendered = json.dumps(value, default=str)
    elif value == "":
        rendered = "(empty)"
    else:
        rendered = str(value)
    return f"  {key}: {rendered}"


def _format_validation_failed(status_code: int, detail: dict) -> str:
    """Render the store-upload 422 with one line per guardrail issue.

    Shape (see ``_reject_inline_or_continue`` in ``app/api/store.py``)::

        {"code": "validation_failed",
         "checks": {"manifest": {"status", "issues"},
                    "content":  {"status", "issues"},
                    "quality":  {"status", "issues"}}}

    Passing checks are omitted; every issue of every failing check is
    printed in full — file, field, code, then the hint wrapped at 80
    cols. No truncation: the whole point is that the submitter fixes
    everything in ONE round instead of replaying upload-fix-upload.
    """
    lines: list[str] = [f"Error: validation_failed (HTTP {status_code})"]
    checks = detail.get("checks")
    if not isinstance(checks, dict):
        return "\n".join(lines)
    for check_name, check in checks.items():
        if not isinstance(check, dict) or check.get("status") == "pass":
            continue
        issues = check.get("issues") or []
        lines.append(f"  {check_name}: {check.get('status', 'fail')} ({len(issues)} issue(s))")
        for issue in issues:
            if not isinstance(issue, dict):
                lines.append(f"    - {issue}")
                continue
            where = issue.get("file") or "<bundle>"
            field = issue.get("field")
            code = issue.get("code", "issue")
            head = f"    - {where}"
            if field:
                head += f" [{field}]"
            head += f": {code}"
            lines.append(head)
            hint = issue.get("hint")
            if hint:
                lines.append(
                    fill(
                        str(hint),
                        width=80,
                        initial_indent="        ",
                        subsequent_indent="        ",
                    )
                )
    return "\n".join(lines)
