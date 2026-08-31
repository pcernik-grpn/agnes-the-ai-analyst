"""``connectors.sharepoint.graph_client`` — item permissions + transitive
group members (spec 2026-08-28 §6 link 1), the two Graph readers the ACL
mirroring sync (2026-08-30 plan, Task 1) is built on.

No live network: the ``_http_client()`` seam is monkeypatched to an
``httpx.AsyncClient`` wired to ``httpx.MockTransport``, same idiom as
``tests/test_sharepoint_graph_client.py`` / ``tests/test_teams_sigverify.py``.
"""

from __future__ import annotations

import httpx
import pytest

from connectors.sharepoint import graph_client


def _transport(pages: dict[str, dict]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        key = str(request.url)
        for frag, body in pages.items():
            if frag in key:
                return httpx.Response(200, json=body)
        return httpx.Response(404, json={"error": {"message": "no page"}})

    return httpx.MockTransport(handler)


@pytest.mark.anyio
async def test_list_item_permissions_follows_next_link(monkeypatch):
    page2_url = "https://graph.microsoft.com/v1.0/drives/d1/items/root/permissions?$skiptoken=x"
    pages = {
        "/drives/d1/items/root/permissions?$skiptoken=x": {"value": [{"id": "p2", "roles": ["read"]}]},
        "/drives/d1/items/root/permissions": {
            "value": [{"id": "p1", "roles": ["read"]}],
            "@odata.nextLink": page2_url,
        },
    }
    monkeypatch.setattr(graph_client, "_http_client", lambda: httpx.AsyncClient(transport=_transport(pages)))
    perms = await graph_client.list_item_permissions("tok", "d1", "root")
    assert [p["id"] for p in perms] == ["p1", "p2"]


@pytest.mark.anyio
async def test_transitive_members_keeps_users_only(monkeypatch):
    pages = {
        "/groups/g-123/transitiveMembers": {
            "value": [
                {
                    "@odata.type": "#microsoft.graph.user",
                    "id": "u1",
                    "mail": "Alice.Novak@example.com",
                    "userPrincipalName": "Alice.Novak@example.com",
                },
                {"@odata.type": "#microsoft.graph.group", "id": "nested"},
            ]
        },
    }
    monkeypatch.setattr(graph_client, "_http_client", lambda: httpx.AsyncClient(transport=_transport(pages)))
    members = await graph_client.list_group_transitive_members("tok", "g-123")
    assert [m["id"] for m in members] == ["u1"]
