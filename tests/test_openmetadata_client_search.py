"""Reverse-search methods for catalog-import: tag -> data products, dp -> tables."""
from unittest.mock import patch, MagicMock

from connectors.openmetadata.client import OpenMetadataClient


def _client_returning(hits):
    """An OpenMetadataClient whose _client.get returns a search response with `hits`."""
    with patch("connectors.openmetadata.client.httpx.Client") as cls:
        inst = MagicMock()
        cls.return_value = inst
        resp = MagicMock()
        resp.json.return_value = {"hits": {"hits": hits}}
        inst.get.return_value = resp
        client = OpenMetadataClient(base_url="https://om", token="t")
    return client, inst


def test_search_data_products_by_tag():
    client, inst = _client_returning([
        {"_source": {"name": "OrderAttribution", "displayName": "Order Attribution",
                     "domain": {"name": "Marketing"}}}])
    dps = client.search_data_products_by_tag("AIReady.Curated")
    assert dps[0]["name"] == "OrderAttribution"
    params = inst.get.call_args.kwargs["params"]
    assert params["index"] == "data_product_search_index"
    assert "AIReady.Curated" in params["query_filter"]
    assert "tags.tagFQN" in params["query_filter"]


def test_search_tables_by_data_product_filters_to_bigquery():
    client, inst = _client_returning([
        {"_source": {"fullyQualifiedName": "bigquery.p.marketing.order_attribution_mkt",
                     "name": "order_attribution_mkt", "serviceType": "BigQuery"}},
        {"_source": {"fullyQualifiedName": "keboola.x.y.dup",
                     "name": "dup", "serviceType": "Keboola"}}])
    tables = client.search_tables_by_data_product("OrderAttribution")
    assert [t["name"] for t in tables] == ["order_attribution_mkt"]  # keboola dup dropped
    params = inst.get.call_args.kwargs["params"]
    assert params["index"] == "table_search_index"
    assert "dataProducts.fullyQualifiedName" in params["query_filter"]
    assert "OrderAttribution" in params["query_filter"]
