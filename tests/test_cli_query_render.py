"""CLI commands route BQ-typed errors through the shared renderer.

Two CLI paths surface BQ errors today:
- `agnes query --remote` (cli/commands/query.py:_query_remote → /api/query)
- `agnes snapshot create` / `agnes schema` / etc. (cli/v2_client.V2ClientError → v2 endpoints)

After the refactor they all call cli.error_render.render_error so analyst
output is consistent and structured. Closes part of #160 §4.7.3.

The third assertion below covers `RemoteQueryError.details`, which is now
exercised only via the server-side `/api/query/hybrid` endpoint
(`app/api/query_hybrid.py`) — the client CLI no longer wraps it.
"""

from __future__ import annotations


def test_v2_client_error_uses_renderer():
    """`V2ClientError.__str__` calls render_error so any caller of the v2
    HTTP helpers (api_get_json/api_post_json/api_post_arrow) automatically
    surfaces typed BQ errors as structured output."""
    from cli.v2_client import V2ClientError

    body = {
        "detail": {
            "kind": "cross_project_forbidden",
            "message": "USER_PROJECT_DENIED",
            "hint": "Set data_source.bigquery.billing_project",
        }
    }
    err = V2ClientError(status_code=502, body=body)
    out = str(err)
    # Old form: `HTTP 502: {'detail': {'kind': ...}}` (single line).
    # New form: multi-line structured.
    assert "\n" in out, f"V2ClientError must use multi-line renderer; got {out!r}"
    assert "cross_project_forbidden" in out
    assert "billing_project" in out


def test_v2_client_error_drops_truncation_for_dicts():
    """The OLD `message=str(body)[:200]` truncation hid the structured
    `hint` field for any reasonably-sized error dict. The new renderer
    must NOT pre-truncate dict bodies."""
    from cli.v2_client import V2ClientError

    body = {
        "detail": {
            "kind": "bq_forbidden",
            "billing_project": "x" * 200,  # padding to push past old 200-char limit
            "data_project": "y" * 200,
            "hint": "MUST_REACH_THIS_HINT_IN_OUTPUT",
        }
    }
    err = V2ClientError(status_code=502, body=body)
    out = str(err)
    assert "MUST_REACH_THIS_HINT_IN_OUTPUT" in out, "renderer must not pre-truncate dict bodies past the hint field"


def test_render_error_keeps_a_column_not_found_hint_intact():
    """``agnes query --remote`` hits the shared renderer's plain-string
    branch for a `/api/query` 400 -- unlike the typed dict shapes above,
    a query error is a flat string (`"Query error: ...\\n\\nHint: ..."`,
    built server-side by `app/api/query.py` via `src.query_error_hints`).
    The renderer must not truncate or otherwise mangle it: this is what
    an analyst running `agnes query --remote` actually reads on their
    terminal, one of the three surfaces (REST/CLI/MCP) that share this
    single server-built message."""
    from cli.error_render import render_error

    detail = (
        'Query error: Binder Error: Table "s" does not have a column named "X"\n\n'
        "Hint: `orders` (aliased `s`) has no column named 'X'. Run `schema orders` "
        "(CLI) or the `schema` MCP tool to see its real columns before retrying."
    )
    out = render_error(400, {"detail": detail})
    assert out == f"HTTP 400: {detail}"


def test_remote_query_error_carries_details():
    """`RemoteQueryError` carries a structured `details` field. Verify the
    type's surface so `app/api/query_hybrid.py` (the only remaining caller)
    can rely on it."""
    from src.remote_query import RemoteQueryError

    err = RemoteQueryError(
        error_type="cross_project_forbidden",
        message="USER_PROJECT_DENIED",
        details={"billing_project": "", "data_project": "prj"},
    )
    assert err.details == {"billing_project": "", "data_project": "prj"}
    assert err.error_type == "cross_project_forbidden"
