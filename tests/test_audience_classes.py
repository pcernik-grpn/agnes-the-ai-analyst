"""``src.audience_classes`` — Slice 3 (2026-08-30 sharepoint-acl-mirroring
plan, Task 9; spec §4.2, §7 step 6): caller-side audience-class resolution.

Repo-level (no ``create_app``): a fresh DuckDB system db is bootstrapped per
test, mirroring ``tests/test_admin_signals_ungranted_plugins.py``'s
``_bootstrap`` idiom. A "tiered" collection is seeded directly as a
``source_connections`` row (``source_type='sharepoint'``, ``config.scopes``
carrying ``audience_classes``) — the exact shape
``app/api/admin_sharepoint.py::confirm_scope`` persists (see
``tests/test_admin_sharepoint.py::TestAudienceClassMap`` for the HTTP-level
round-trip of that same shape) — since :func:`audience_classes_for_caller`
only needs the collection id to key the result, not a real ``file_corpora``
row.

The HTTP-level agent-scope pin validation
(``PUT /api/v1/agents/{id}/scope``'s ``audience_class`` field) is exercised
separately at the bottom of this file via the ``shared_app`` fixture, since
that is genuinely an API surface (typed 400s, omitted-vs-empty semantics).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient


def _bootstrap(tmp_path, monkeypatch):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb

    conn = _open_duckdb(str(tmp_path / "system.duckdb"))
    _ensure_schema(conn)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr("src.repositories.get_system_db", lambda: conn)
    return conn


def _make_group(name: str) -> str:
    from src.repositories import user_groups_repo

    return user_groups_repo().create(name=name)["id"]


def _make_user(user_id: str, email: str) -> None:
    from src.repositories import users_repo

    users_repo().create(id=user_id, email=email, name=email)


def _add_member(user_id: str, group_id: str, source: str = "admin") -> None:
    from src.repositories import user_group_members_repo

    user_group_members_repo().add_member(user_id, group_id, source=source, added_by="test")


def _seed_tiered_collection(collection_id: str, classes: list[tuple[str, list[str]]]) -> None:
    """One SharePoint connection with a single confirmed, tiered scope —
    the same ``config.scopes`` shape ``app/api/admin_sharepoint.py::
    confirm_scope`` writes (Task 8), minimal enough for
    :func:`src.audience_classes.audience_class_map` to read back.
    ``classes`` is ordered most-privileged first, matching the wizard's
    persisted privilege ranking.
    """
    from src.repositories import source_connections_repo

    source_connections_repo().create(
        id=uuid.uuid4().hex,
        name=f"conn-{collection_id}",
        source_type="sharepoint",
        config={
            "scopes": [
                {
                    "source_scope_id": f"drive:{collection_id}",
                    "display_path": "Aud",
                    "collection_id": collection_id,
                    "audience_classes": [{"name": name, "group_ids": gids} for name, gids in classes],
                }
            ]
        },
    )


def _seed_plain_collection(collection_id: str) -> None:
    from src.repositories import source_connections_repo

    source_connections_repo().create(
        id=uuid.uuid4().hex,
        name=f"plain-{collection_id}",
        source_type="sharepoint",
        config={
            "scopes": [
                {
                    "source_scope_id": f"drive:{collection_id}",
                    "display_path": "Plain",
                    "collection_id": collection_id,
                }
            ]
        },
    )


# ---------------------------------------------------------------------------
# audience_classes_for_caller — plain users + admin
# ---------------------------------------------------------------------------


class TestPlainUserResolution:
    def test_user_in_top_class(self, tmp_path, monkeypatch):
        _bootstrap(tmp_path, monkeypatch)
        full = _make_group("full-group")
        _make_user("u1", "u1@test.com")
        _add_member("u1", full)
        _seed_tiered_collection("col1", [("full", [full]), ("redacted", [])])

        from src.audience_classes import audience_classes_for_caller

        out = audience_classes_for_caller({"id": "u1"}, ["col1"])
        assert out == {"col1": frozenset({"full"})}

    def test_user_in_no_class_is_present_but_empty(self, tmp_path, monkeypatch):
        _bootstrap(tmp_path, monkeypatch)
        full = _make_group("full-group2")
        _make_user("u2", "u2@test.com")
        _seed_tiered_collection("col2", [("full", [full])])

        from src.audience_classes import audience_classes_for_caller

        out = audience_classes_for_caller({"id": "u2"}, ["col2"])
        # PRESENT (not omitted) with an empty frozenset — decided and
        # documented in src.audience_classes.audience_classes_for_caller's
        # own docstring; kept consistent with Task 10's :audience_pairs
        # binding, which reads the same either way.
        assert out == {"col2": frozenset()}

    def test_admin_gets_every_class(self, tmp_path, monkeypatch):
        conn = _bootstrap(tmp_path, monkeypatch)
        from src.db import SYSTEM_ADMIN_GROUP
        from src.repositories import user_group_members_repo, user_groups_repo

        admin_gid = user_groups_repo().get_by_name(SYSTEM_ADMIN_GROUP)["id"]
        _make_user("admin1", "admin1@test.com")
        user_group_members_repo().add_member("admin1", admin_gid, source="system_seed")
        full = _make_group("full-group3")
        redacted = _make_group("redacted-group3")
        _seed_tiered_collection("col3", [("full", [full]), ("redacted", [redacted])])

        from src.audience_classes import audience_classes_for_caller

        out = audience_classes_for_caller({"id": "admin1"}, ["col3"])
        assert out == {"col3": frozenset({"full", "redacted"})}
        conn.close()

    def test_non_tiered_collection_is_omitted(self, tmp_path, monkeypatch):
        _bootstrap(tmp_path, monkeypatch)
        _make_user("u3", "u3@test.com")
        _seed_plain_collection("col-plain")

        from src.audience_classes import audience_classes_for_caller

        out = audience_classes_for_caller({"id": "u3"}, ["col-plain"])
        assert out == {}

    def test_mixed_tiered_and_non_tiered(self, tmp_path, monkeypatch):
        _bootstrap(tmp_path, monkeypatch)
        full = _make_group("full-group4")
        _make_user("u4", "u4@test.com")
        _add_member("u4", full)
        _seed_tiered_collection("col-tiered", [("full", [full])])
        _seed_plain_collection("col-plain2")

        from src.audience_classes import audience_classes_for_caller

        out = audience_classes_for_caller({"id": "u4"}, ["col-tiered", "col-plain2"])
        assert out == {"col-tiered": frozenset({"full"})}


class TestTopClassFor:
    def test_tiered_returns_most_privileged(self, tmp_path, monkeypatch):
        _bootstrap(tmp_path, monkeypatch)
        _seed_tiered_collection("col5", [("full", []), ("redacted", [])])

        from src.audience_classes import top_class_for

        assert top_class_for("col5") == "full"

    def test_non_tiered_or_unknown_is_none(self, tmp_path, monkeypatch):
        _bootstrap(tmp_path, monkeypatch)
        _seed_plain_collection("col6")

        from src.audience_classes import top_class_for

        assert top_class_for("col6") is None
        assert top_class_for("does-not-exist") is None


# ---------------------------------------------------------------------------
# audience_classes_for_caller — restricted Principal (AgentPrincipal)
# ---------------------------------------------------------------------------


def _agent_principal(agent_id: str, owner_user_id: str):
    from app.auth.session_principal import AgentPrincipal

    return AgentPrincipal(
        session_id="s1",
        agent_id=agent_id,
        owner_user_id=owner_user_id,
        owner_email="owner@test.com",
        intersection={},
    )


def _create_agent(agent_id: str, owner_user_id: str) -> None:
    from src.repositories import agents_repo

    agents_repo().create(id=agent_id, owner_user_id=owner_user_id, name="a", slug=agent_id)


def _set_pin(agent_id: str, pins: dict[str, str]) -> None:
    """Write raw ``audience_class_pin`` rows directly — bypasses the API
    validator on purpose, since these tests exercise the READ/resolution
    side, not the write-time validation (covered below in
    ``TestAgentScopePinValidation``)."""
    from src.agent_scope_intersection import AUDIENCE_CLASS_PIN_ITEM_TYPE
    from src.repositories import agents_repo

    items = [(AUDIENCE_CLASS_PIN_ITEM_TYPE, f"{cid}:{cls}") for cid, cls in pins.items()]
    agents_repo().set_scope(agent_id, items)


class TestAgentPrincipalResolution:
    def test_default_is_least_privileged(self, tmp_path, monkeypatch):
        _bootstrap(tmp_path, monkeypatch)
        _make_user("owner1", "owner1@test.com")
        _create_agent("agent1", "owner1")
        _seed_tiered_collection("col7", [("full", []), ("redacted", [])])

        from src.audience_classes import audience_classes_for_caller

        caller = _agent_principal("agent1", "owner1")
        out = audience_classes_for_caller(caller, ["col7"])
        assert out == {"col7": frozenset({"redacted"})}

    def test_pin_honored_when_owner_holds_the_class(self, tmp_path, monkeypatch):
        _bootstrap(tmp_path, monkeypatch)
        full = _make_group("full-group8")
        _make_user("owner2", "owner2@test.com")
        _add_member("owner2", full)
        _create_agent("agent2", "owner2")
        _seed_tiered_collection("col8", [("full", [full]), ("redacted", [])])
        _set_pin("agent2", {"col8": "full"})

        from src.audience_classes import audience_classes_for_caller

        caller = _agent_principal("agent2", "owner2")
        out = audience_classes_for_caller(caller, ["col8"])
        assert out == {"col8": frozenset({"full"})}

    def test_pin_honored_when_owner_holds_a_better_class(self, tmp_path, monkeypatch):
        """Three tiers: owner holds the TOP class, pin asks for the MIDDLE
        one — still honored (holds "that class or better")."""
        _bootstrap(tmp_path, monkeypatch)
        top = _make_group("top-group9")
        _make_user("owner3", "owner3@test.com")
        _add_member("owner3", top)
        _create_agent("agent3", "owner3")
        _seed_tiered_collection("col9", [("full", [top]), ("partial", []), ("redacted", [])])
        _set_pin("agent3", {"col9": "partial"})

        from src.audience_classes import audience_classes_for_caller

        caller = _agent_principal("agent3", "owner3")
        out = audience_classes_for_caller(caller, ["col9"])
        assert out == {"col9": frozenset({"partial"})}

    def test_pin_degrades_silently_when_owner_lacks_the_class(self, tmp_path, monkeypatch):
        _bootstrap(tmp_path, monkeypatch)
        redacted = _make_group("redacted-group10")
        _make_user("owner4", "owner4@test.com")
        _add_member("owner4", redacted)  # owner holds only the LEAST class
        _create_agent("agent4", "owner4")
        _seed_tiered_collection("col10", [("full", []), ("redacted", [redacted])])
        _set_pin("agent4", {"col10": "full"})  # pin asks for MORE than owner holds

        from src.audience_classes import audience_classes_for_caller

        caller = _agent_principal("agent4", "owner4")
        out = audience_classes_for_caller(caller, ["col10"])
        assert out == {"col10": frozenset({"redacted"})}

    def test_pin_degrades_when_owner_holds_no_class_at_all(self, tmp_path, monkeypatch):
        _bootstrap(tmp_path, monkeypatch)
        _make_user("owner5", "owner5@test.com")
        _create_agent("agent5", "owner5")
        _seed_tiered_collection("col11", [("full", []), ("redacted", [])])
        _set_pin("agent5", {"col11": "full"})

        from src.audience_classes import audience_classes_for_caller

        caller = _agent_principal("agent5", "owner5")
        out = audience_classes_for_caller(caller, ["col11"])
        assert out == {"col11": frozenset({"redacted"})}

    def test_pin_referencing_a_removed_class_degrades(self, tmp_path, monkeypatch):
        _bootstrap(tmp_path, monkeypatch)
        full = _make_group("full-group12")
        _make_user("owner6", "owner6@test.com")
        _add_member("owner6", full)
        _create_agent("agent6", "owner6")
        # Pin references a class name no longer configured on the collection.
        _seed_tiered_collection("col12", [("full", [full]), ("redacted", [])])
        _set_pin("agent6", {"col12": "archived"})

        from src.audience_classes import audience_classes_for_caller

        caller = _agent_principal("agent6", "owner6")
        out = audience_classes_for_caller(caller, ["col12"])
        assert out == {"col12": frozenset({"redacted"})}

    def test_non_tiered_collection_omitted_for_agent_principal(self, tmp_path, monkeypatch):
        _bootstrap(tmp_path, monkeypatch)
        _make_user("owner7", "owner7@test.com")
        _create_agent("agent7", "owner7")
        _seed_plain_collection("col13")

        from src.audience_classes import audience_classes_for_caller

        caller = _agent_principal("agent7", "owner7")
        out = audience_classes_for_caller(caller, ["col13"])
        assert out == {}


# ---------------------------------------------------------------------------
# audience_class_pins (src.agent_scope_intersection) — read-side reader
# ---------------------------------------------------------------------------


class TestAudienceClassPinsReader:
    def test_reads_back_stored_pins(self, tmp_path, monkeypatch):
        _bootstrap(tmp_path, monkeypatch)
        _make_user("owner8", "owner8@test.com")
        _create_agent("agent8", "owner8")
        _set_pin("agent8", {"colA": "full", "colB": "redacted"})

        from src.agent_scope_intersection import audience_class_pins

        assert audience_class_pins("agent8") == {"colA": "full", "colB": "redacted"}

    def test_no_pins_is_empty_dict(self, tmp_path, monkeypatch):
        _bootstrap(tmp_path, monkeypatch)
        _make_user("owner9", "owner9@test.com")
        _create_agent("agent9", "owner9")

        from src.agent_scope_intersection import audience_class_pins

        assert audience_class_pins("agent9") == {}

    def test_missing_agent_id_is_empty_dict(self, tmp_path, monkeypatch):
        _bootstrap(tmp_path, monkeypatch)

        from src.agent_scope_intersection import audience_class_pins

        assert audience_class_pins("") == {}


# ---------------------------------------------------------------------------
# HTTP — PUT /api/v1/agents/{id}/scope's `audience_class` field
# ---------------------------------------------------------------------------


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def pin_env(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")

    from app.auth.jwt import create_access_token
    from src.db import get_system_db
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="pinowner", email="pinowner@test.com", name="Owner")
    conn.close()

    client = TestClient(shared_app)
    token = create_access_token("pinowner", "pinowner@test.com")
    agent_id = client.post(
        "/api/v1/agents", json={"name": "pin-agent", "slug": "pin-agent"}, headers=_auth(token)
    ).json()["id"]
    return {"client": client, "token": token, "agent_id": agent_id}


class TestAgentScopePinValidation:
    def test_unknown_collection_id_rejected(self, pin_env):
        r = pin_env["client"].put(
            f"/api/v1/agents/{pin_env['agent_id']}/scope",
            json={"audience_class": {"does-not-exist": "full"}},
            headers=_auth(pin_env["token"]),
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["code"] == "invalid_audience_class_collection"

    def test_unknown_class_name_rejected(self, pin_env, monkeypatch):
        _seed_tiered_collection("http-col1", [("full", [])])
        r = pin_env["client"].put(
            f"/api/v1/agents/{pin_env['agent_id']}/scope",
            json={"audience_class": {"http-col1": "does-not-exist"}},
            headers=_auth(pin_env["token"]),
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["code"] == "invalid_audience_class_name"

    def test_valid_pin_round_trips(self, pin_env):
        _seed_tiered_collection("http-col2", [("full", []), ("redacted", [])])
        r = pin_env["client"].put(
            f"/api/v1/agents/{pin_env['agent_id']}/scope",
            json={"audience_class": {"http-col2": "redacted"}},
            headers=_auth(pin_env["token"]),
        )
        assert r.status_code == 200, r.text
        assert r.json()["audience_class"] == {"http-col2": "redacted"}
        assert all(item["item_type"] != "audience_class_pin" for item in r.json()["items"])

        from src.agent_scope_intersection import audience_class_pins

        assert audience_class_pins(pin_env["agent_id"]) == {"http-col2": "redacted"}

    def test_omitting_the_field_preserves_the_existing_pin(self, pin_env):
        _seed_tiered_collection("http-col3", [("full", []), ("redacted", [])])
        pin_env["client"].put(
            f"/api/v1/agents/{pin_env['agent_id']}/scope",
            json={"audience_class": {"http-col3": "full"}},
            headers=_auth(pin_env["token"]),
        )
        # A follow-up PUT that says nothing about audience_class (e.g. only
        # changing plain scope items) must not wipe the stored pin.
        r = pin_env["client"].put(
            f"/api/v1/agents/{pin_env['agent_id']}/scope",
            json={"items": []},
            headers=_auth(pin_env["token"]),
        )
        assert r.status_code == 200, r.text
        assert r.json()["audience_class"] == {"http-col3": "full"}

    def test_empty_dict_clears_the_pin(self, pin_env):
        _seed_tiered_collection("http-col4", [("full", []), ("redacted", [])])
        pin_env["client"].put(
            f"/api/v1/agents/{pin_env['agent_id']}/scope",
            json={"audience_class": {"http-col4": "full"}},
            headers=_auth(pin_env["token"]),
        )
        r = pin_env["client"].put(
            f"/api/v1/agents/{pin_env['agent_id']}/scope",
            json={"audience_class": {}},
            headers=_auth(pin_env["token"]),
        )
        assert r.status_code == 200, r.text
        assert r.json()["audience_class"] == {}

        from src.agent_scope_intersection import audience_class_pins

        assert audience_class_pins(pin_env["agent_id"]) == {}
