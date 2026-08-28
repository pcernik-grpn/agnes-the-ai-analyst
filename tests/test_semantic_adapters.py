import pytest

from src.semantic.adapters import UnknownAdapter, get_adapter


def test_native_adapter_returns_documents_untouched():
    text = "version: '0.2.0.dev0'\nsemantic_model:\n  - name: retail\n"
    out = get_adapter("native").extract({"documents": [text]})
    assert out == [text], "the adapter must not re-serialize; byte-identical or bust"


def test_unknown_adapter_names_the_available_ones():
    with pytest.raises(UnknownAdapter) as exc:
        get_adapter("nope")
    assert "native" in str(exc.value)


def test_every_connector_adapter_is_mirrored_in_the_coverage_map():
    """`SEMANTIC_ADAPTER_BY_SOURCE_TYPE` is a hand-maintained mirror of the
    adapter registry, and a source type absent from it reports its semantic
    column as ``not_applicable`` — "no adapter exists for this source type"
    — which is a lie the moment one does. That is exactly how the Databricks
    metric-view adapter shipped registered, wired into the connect wizard,
    and still reported as having no adapter at all.

    ``native`` is the one legitimate absence: it serves the git/upload
    transports, which are not bound to a `source_connections` row. Anything
    else new has to be added to the map — or added to this exemption with a
    reason, deliberately.
    """
    from src.semantic.adapters import _REGISTRY
    from src.semantic.coverage import SEMANTIC_ADAPTER_BY_SOURCE_TYPE

    not_connection_backed = {"native"}
    registered = set(_REGISTRY) - not_connection_backed
    mapped = set(SEMANTIC_ADAPTER_BY_SOURCE_TYPE.values())

    assert registered - mapped == set(), (
        f"adapter(s) {sorted(registered - mapped)} are registered but named by no source type in "
        "SEMANTIC_ADAPTER_BY_SOURCE_TYPE (src/semantic/coverage.py) — every connection whose "
        "type they serve will report semantic coverage as 'not_applicable'"
    )
    assert mapped - registered == set(), (
        f"SEMANTIC_ADAPTER_BY_SOURCE_TYPE names adapter(s) {sorted(mapped - registered)} that are "
        "not registered in src/semantic/adapters — those connections' semantic column would "
        "score against an adapter that cannot run"
    )
