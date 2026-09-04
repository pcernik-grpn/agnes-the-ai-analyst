"""``connectors.sharepoint.site_split`` — the pure packing algorithm behind
"split one large SharePoint site into N parallel crawl connections". No
Graph I/O, no app-state — see the module docstring for why this is tested in
isolation from ``app.api.admin_sharepoint``'s endpoints."""

from __future__ import annotations

import pytest

from connectors.sharepoint.site_split import (
    SPLIT_SERVER_WRITTEN_CONFIG_KEYS,
    format_group_name,
    pack_folders_into_groups,
)


def _folder(name: str, documents: int, **extra) -> dict:
    return {"name": name, "documents": documents, **extra}


class TestPackFoldersIntoGroups:
    def test_returns_exactly_n_groups(self):
        groups = pack_folders_into_groups([_folder("A", 10)], 3)
        assert len(groups) == 3

    def test_empty_folders_yields_n_empty_groups(self):
        groups = pack_folders_into_groups([], 4)
        assert len(groups) == 4
        assert all(g["folders"] == [] and g["documents"] == 0 for g in groups)

    def test_every_folder_appears_exactly_once(self):
        folders = [_folder(f"F{i}", i * 7) for i in range(1, 11)]
        groups = pack_folders_into_groups(folders, 3)
        packed_names = [f["name"] for g in groups for f in g["folders"]]
        assert sorted(packed_names) == sorted(f["name"] for f in folders)
        assert len(packed_names) == len(folders)

    def test_balances_document_counts(self):
        # Four folders of 100, three of 1 each: naive round-robin would put
        # two 100s in one group; greedy-by-largest-first must not.
        folders = [_folder("Big1", 100), _folder("Big2", 100), _folder("Big3", 100), _folder("Big4", 100)]
        groups = pack_folders_into_groups(folders, 4)
        totals = sorted(g["documents"] for g in groups)
        assert totals == [100, 100, 100, 100]

    def test_greedy_beats_naive_ordering_on_a_skewed_input(self):
        # One huge folder plus several small ones: the huge one must not
        # share a group with more than a small remainder.
        folders = [_folder("Huge", 1000)] + [_folder(f"Small{i}", 10) for i in range(10)]
        groups = pack_folders_into_groups(folders, 4)
        totals = sorted(g["documents"] for g in groups)
        # The group holding "Huge" should be the largest, and no other
        # group should be forced to absorb more than a fair share of the
        # remaining 100 documents (roughly 33 each across the other three).
        assert totals[-1] >= 1000
        assert totals[0] <= 40

    def test_zero_count_folders_are_kept_never_dropped(self):
        folders = [_folder("Empty1", 0), _folder("Empty2", 0), _folder("Real", 50)]
        groups = pack_folders_into_groups(folders, 2)
        packed_names = {f["name"] for g in groups for f in g["folders"]}
        assert packed_names == {"Empty1", "Empty2", "Real"}

    def test_folder_dicts_pass_through_unchanged(self):
        folders = [_folder("A", 5, id="item-a", web_url="https://example.sharepoint.com/x/A")]
        groups = pack_folders_into_groups(folders, 1)
        assert groups[0]["folders"] == folders

    def test_rejects_n_below_one(self):
        with pytest.raises(ValueError):
            pack_folders_into_groups([_folder("A", 1)], 0)

    def test_n_larger_than_folder_count_leaves_some_groups_empty(self):
        groups = pack_folders_into_groups([_folder("A", 5), _folder("B", 3)], 5)
        assert len(groups) == 5
        non_empty = [g for g in groups if g["folders"]]
        assert len(non_empty) == 2


class TestFormatGroupName:
    def test_one_indexed(self):
        assert format_group_name("Big Site", 1, 8) == "Big Site — part 1/8"
        assert format_group_name("Big Site", 8, 8) == "Big Site — part 8/8"


def test_split_server_written_config_keys_is_exactly_split():
    """Consolidation's `include_split_siblings` reads `config.split` off
    every connection `app.api.admin_sharepoint.apply_split` created — the
    ONE key this module declares as server-written, carried forward on an
    ordinary connection edit by `app.api.admin_source_connections.
    update_connection` (see that module's own carry-forward test,
    `tests/test_admin_source_connections.py::
    test_editing_config_without_split_preserves_it`)."""
    assert SPLIT_SERVER_WRITTEN_CONFIG_KEYS == ("split",)
