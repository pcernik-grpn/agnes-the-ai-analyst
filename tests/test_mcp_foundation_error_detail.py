"""``_raise_for_status_with_detail`` — 4xx/5xx bodies must reach the model.

The generic ``raise_for_status()`` message ("Client error '400 Bad Request'
for url …") hides the JSON ``detail`` that tells the model how to fix the
call — e.g. ``invalid_category`` arrives with the list of valid categories,
and swallowing it turns a self-correctable mistake into a dead-end error
card. Every foundation tool routes through the helper instead.
"""

import httpx
import pytest

from app.api.mcp.foundation_tools import _raise_for_status_with_detail


def _resp(status: int, *, json_body=None, text: str = "") -> httpx.Response:
    req = httpx.Request("POST", "http://server/api/store/entities/from-markdown")
    if json_body is not None:
        return httpx.Response(status, json=json_body, request=req)
    return httpx.Response(status, text=text, request=req)


def test_success_status_does_not_raise():
    _raise_for_status_with_detail(_resp(201, json_body={"id": "x"}))


def test_dict_detail_lands_in_the_message():
    r = _resp(
        400,
        json_body={
            "detail": {
                "code": "invalid_category",
                "given": "Marketing",
                "valid": ["Data & Analytics", "Other"],
            }
        },
    )
    with pytest.raises(httpx.HTTPStatusError) as exc:
        _raise_for_status_with_detail(r)
    msg = str(exc.value)
    assert "400" in msg
    assert "invalid_category" in msg
    assert "Data & Analytics" in msg


def test_string_detail_lands_in_the_message():
    r = _resp(400, json_body={"detail": "invalid_name_format"})
    with pytest.raises(httpx.HTTPStatusError) as exc:
        _raise_for_status_with_detail(r)
    assert "invalid_name_format" in str(exc.value)


def test_non_json_body_falls_back_to_text():
    r = _resp(502, text="upstream exploded")
    with pytest.raises(httpx.HTTPStatusError) as exc:
        _raise_for_status_with_detail(r)
    assert "upstream exploded" in str(exc.value)


def test_json_body_without_detail_falls_back_to_raw_text():
    r = _resp(403, json_body={"error": "nope"})
    with pytest.raises(httpx.HTTPStatusError) as exc:
        _raise_for_status_with_detail(r)
    assert "nope" in str(exc.value)


def test_empty_body_keeps_a_clean_message():
    r = _resp(401)
    with pytest.raises(httpx.HTTPStatusError) as exc:
        _raise_for_status_with_detail(r)
    msg = str(exc.value)
    assert msg.endswith(str(r.request.url))
    assert "—" not in msg


def test_oversized_detail_is_truncated():
    r = _resp(400, json_body={"detail": "x" * 5000})
    with pytest.raises(httpx.HTTPStatusError) as exc:
        _raise_for_status_with_detail(r)
    assert len(str(exc.value)) < 1000
