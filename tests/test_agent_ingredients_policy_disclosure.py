"""Design doc §12 (shared agents + access policies) — the agent builder's
"tables in / selectable for an agent's scope" surfaces must disclose an
access policy before the owner grounds an agent in it.

``app.services.agent_ingredients.policy_disclosure_for_knowledge`` is the
one function both the picker's candidate list and an attached-knowledge
read go through (``knowledge_sources_for``, ``app.api.agents_admin.
_serialize`` via ``_policied_tables_disclosure``). These tests exercise it
directly, with stubbed repos — the endpoint-level wiring is covered in
``tests/test_agents_management_api_policy_disclosure.py``.
"""

from __future__ import annotations

from app.services.agent_ingredients import knowledge_sources_for, policy_disclosure_for_knowledge

_OWNER = {"id": "owner1", "email": "owner@example.com"}


class _PkgRepoStub:
    def __init__(self, tables_by_pkg):
        self._tables_by_pkg = tables_by_pkg

    def list_tables(self, pkg_id):
        return self._tables_by_pkg.get(pkg_id, [])


class _TableRegistryStub:
    def __init__(self, rows_by_id):
        self._rows_by_id = rows_by_id

    def get(self, table_id):
        return self._rows_by_id.get(table_id)


def _patch_repos(monkeypatch, *, tables_by_pkg, rows_by_id, diagnosis=None):
    import src.repositories as repos

    monkeypatch.setattr(repos, "data_packages_repo", lambda: _PkgRepoStub(tables_by_pkg))
    monkeypatch.setattr(repos, "table_registry_repo", lambda: _TableRegistryStub(rows_by_id))
    if diagnosis is not None:
        import app.api.access as access_mod

        monkeypatch.setattr(access_mod, "table_policy_diagnosis", lambda row, principal: diagnosis)


class TestPolicyDisclosureForKnowledge:
    def test_policied_member_table_is_reported(self, monkeypatch):
        _patch_repos(
            monkeypatch,
            tables_by_pkg={"pkg1": [{"id": "t1", "name": "Orders"}]},
            rows_by_id={"t1": {"id": "t1", "name": "Orders", "access_policy_sql": "SELECT * FROM orders"}},
            diagnosis={"applies": True, "rows_visible": 5, "reason": "ok", "note": None},
        )
        out = policy_disclosure_for_knowledge(["pkg1"], _OWNER)
        assert out == [
            {
                "table_id": "t1",
                "name": "Orders",
                "access_policy": True,
                "policy": {"applies": True, "rows_visible": 5, "reason": "ok", "note": None},
            }
        ]

    def test_unpolicied_table_is_absent(self, monkeypatch):
        _patch_repos(
            monkeypatch,
            tables_by_pkg={"pkg1": [{"id": "t1", "name": "Orders"}]},
            rows_by_id={"t1": {"id": "t1", "name": "Orders", "access_policy_sql": None}},
        )
        assert policy_disclosure_for_knowledge(["pkg1"], _OWNER) == []

    def test_non_package_id_yields_nothing_without_raising(self, monkeypatch):
        """A memory_domain/collection id in `knowledge` resolves to zero
        member tables — `list_tables` on a non-package id is just an empty
        join, never an error."""
        _patch_repos(monkeypatch, tables_by_pkg={}, rows_by_id={})
        assert policy_disclosure_for_knowledge(["mem_domain_1"], _OWNER) == []

    def test_empty_knowledge_short_circuits(self, monkeypatch):
        calls = []
        import src.repositories as repos

        monkeypatch.setattr(
            repos,
            "data_packages_repo",
            lambda: calls.append("called") or _PkgRepoStub({}),
        )
        assert policy_disclosure_for_knowledge([], _OWNER) == []
        assert calls == [], "must not touch the repo factory at all for an empty declaration"

    def test_dedupes_the_same_table_reached_via_two_packages(self, monkeypatch):
        _patch_repos(
            monkeypatch,
            tables_by_pkg={
                "pkg1": [{"id": "t1", "name": "Orders"}],
                "pkg2": [{"id": "t1", "name": "Orders"}],
            },
            rows_by_id={"t1": {"id": "t1", "name": "Orders", "access_policy_sql": "SELECT 1"}},
            diagnosis={"applies": True, "rows_visible": 1, "reason": "ok", "note": None},
        )
        out = policy_disclosure_for_knowledge(["pkg1", "pkg2"], _OWNER)
        assert len(out) == 1

    def test_a_list_tables_failure_degrades_to_no_disclosure_for_that_id(self, monkeypatch):
        class _Boom:
            def list_tables(self, pkg_id):
                raise RuntimeError("registry unavailable")

        import src.repositories as repos

        monkeypatch.setattr(repos, "data_packages_repo", lambda: _Boom())
        monkeypatch.setattr(repos, "table_registry_repo", lambda: _TableRegistryStub({}))
        assert policy_disclosure_for_knowledge(["pkg1"], _OWNER) == []


class TestKnowledgeSourcesForCarriesPolicyFlag:
    def _stub_stack(self, monkeypatch, entries):
        from app.services import stack_resolver

        monkeypatch.setattr(
            stack_resolver.StackResolver, "stack", lambda self, uid, rt: entries if rt.value == "data_package" else []
        )
        monkeypatch.setattr("app.auth.access.accessible_collection_ids", lambda user: None)
        import src.repositories as repos

        monkeypatch.setattr(repos, "file_corpora_repo", lambda: type("R", (), {"list_all": staticmethod(list)})())
        monkeypatch.setattr(repos, "memory_domains_repo", lambda: type("R", (), {})())

    def test_data_entry_with_a_policied_table_is_flagged(self, monkeypatch):
        entry = type("Entry", (), {"id": "pkg1", "name": "Sales Pack", "description": "d"})()
        self._stub_stack(monkeypatch, [entry])
        _patch_repos(
            monkeypatch,
            tables_by_pkg={"pkg1": [{"id": "t1", "name": "Orders"}]},
            rows_by_id={"t1": {"id": "t1", "name": "Orders", "access_policy_sql": "SELECT 1"}},
            diagnosis={"applies": True, "rows_visible": 3, "reason": "ok", "note": None},
        )
        rows = [r for r in knowledge_sources_for({"id": "u1"}) if r["kind"] == "data"]
        assert len(rows) == 1
        assert rows[0]["access_policy"] is True
        assert rows[0]["policied_tables"][0]["table_id"] == "t1"

    def test_data_entry_without_a_policy_is_not_flagged(self, monkeypatch):
        entry = type("Entry", (), {"id": "pkg1", "name": "Sales Pack", "description": "d"})()
        self._stub_stack(monkeypatch, [entry])
        _patch_repos(
            monkeypatch,
            tables_by_pkg={"pkg1": [{"id": "t1", "name": "Orders"}]},
            rows_by_id={"t1": {"id": "t1", "name": "Orders", "access_policy_sql": None}},
        )
        rows = [r for r in knowledge_sources_for({"id": "u1"}) if r["kind"] == "data"]
        assert len(rows) == 1
        assert rows[0]["access_policy"] is False
        assert rows[0]["policied_tables"] == []
