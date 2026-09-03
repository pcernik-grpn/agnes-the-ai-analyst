"""``app.instance_config.get_collections_search_max_chunks`` (#2151).

Mirrors the monkeypatched-``get_value`` pattern in
``tests/test_retention_prune.py``'s ``TestRetentionDaysConfig``.
"""

from __future__ import annotations


def test_default_is_25000(monkeypatch):
    import app.instance_config as ic

    monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: default)
    assert ic.get_collections_search_max_chunks() == 25000


def test_reads_configured_value(monkeypatch):
    import app.instance_config as ic

    def _get_value(*keys, default=None):
        return 5000 if keys == ("collections", "search_max_chunks") else default

    monkeypatch.setattr(ic, "get_value", _get_value)
    assert ic.get_collections_search_max_chunks() == 5000


def test_non_positive_value_clamped_to_one(monkeypatch):
    import app.instance_config as ic

    monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: -10)
    assert ic.get_collections_search_max_chunks() == 1

    monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: 0)
    assert ic.get_collections_search_max_chunks() == 1


def test_invalid_value_falls_back_to_default(monkeypatch):
    import app.instance_config as ic

    monkeypatch.setattr(ic, "get_value", lambda *keys, default=None: "garbage")
    assert ic.get_collections_search_max_chunks() == 25000
