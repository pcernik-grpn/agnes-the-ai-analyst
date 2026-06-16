"""
OpenMetadata REST API Client

Low-level HTTP wrapper for OpenMetadata REST API with these functions:
1. Authentication using JWT bearer token
2. Get table metadata (description, columns, tags, owners)
3. Get metrics (for Phase 2)
4. Proper error handling and logging
"""

import json
import logging
from typing import Dict, List, Optional, Any

import httpx


logger = logging.getLogger(__name__)


class OpenMetadataClient:
    """
    HTTP client for OpenMetadata REST API.

    Provides methods for querying table metadata:
    - get_table(fqn) -> table metadata with columns, owners, tags
    - get_metrics() -> list of available business metrics
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: int = 30,
        verify: bool | str = True,
    ):
        """
        Initialize OpenMetadata API client.

        Args:
            base_url: Base URL of OpenMetadata instance (e.g., "https://catalog.example.com")
            token: JWT bearer token for authentication
            timeout: HTTP request timeout in seconds
            verify: TLS verification — True (default), False to disable
                (e.g., for self-signed certificates on internal CAs), or a
                path to a CA bundle. The previous version hardcoded False
                globally and suppressed warnings — both removed in #89.
                Operators with self-signed certs should pass an explicit
                ``verify=False`` or a CA bundle path from their config.
        """
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
            verify=verify,
        )

    def get_table(self, fqn: str) -> Dict[str, Any]:
        """
        Fetch table metadata from OpenMetadata.

        Args:
            fqn: Fully qualified name (e.g., "bigquery.project.dataset.table")

        Returns:
            Dictionary with table metadata including:
            - id, name, fullyQualifiedName
            - description
            - columns: list of column dicts with name, dataType, description
            - tags: list of tag dicts
            - owners: list of owner dicts with name, email
            - extension: custom metadata (e.g., tier)

        Raises:
            httpx.HTTPStatusError: If request fails (non-2xx status)
        """
        url = f"/api/v1/tables/name/{fqn}"
        params = {
            "fields": "columns,owners,tags,extension",
            "include": "all",
        }

        response = self._client.get(url, params=params)
        response.raise_for_status()

        return response.json()

    def get_metrics(self, limit: int = 100, fields: str = "tags,owners") -> List[Dict[str, Any]]:
        """
        Fetch list of available metrics from OpenMetadata.

        Args:
            limit: Maximum number of metrics to return
            fields: Comma-separated list of fields to include (e.g., "tags,owners")

        Returns:
            List of metric dictionaries with:
            - id, name, fullyQualifiedName
            - description
            - metricExpression: metric calculation SQL/formula
            - owners, tags (when requested via fields)
        """
        params = {
            "limit": limit,
            "fields": fields,
        }

        response = self._client.get("/api/v1/metrics", params=params)
        response.raise_for_status()

        data = response.json()
        return data.get("data", [])

    def get_metric_by_fqn(self, fqn: str, fields: str = "tags,owners") -> Dict[str, Any]:
        """
        Fetch a specific metric by FQN from OpenMetadata.

        Args:
            fqn: Fully qualified name (e.g., "Active2 Customers")
            fields: Comma-separated list of fields to include

        Returns:
            Dictionary with metric metadata:
            - id, name, fullyQualifiedName
            - description, metricExpression
            - owners, tags (when requested via fields)

        Raises:
            httpx.HTTPStatusError: If request fails (non-2xx status)
        """
        url = f"/api/v1/metrics/name/{fqn}"
        params = {"fields": fields}

        response = self._client.get(url, params=params)
        response.raise_for_status()

        return response.json()

    def search_by_data_product(
        self,
        data_product_name: str,
        entity_type: str = "",
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """
        Search for entities belonging to a data product.

        Uses OpenMetadata search API to fetch entities, then filters client-side
        by data product membership (queryFilter is unreliable for dataProducts field).

        Args:
            data_product_name: Name of the data product (e.g., "AnalyticsDataModel")
            entity_type: Filter by entity type (e.g., "metric", "table"). Empty = all types.
            limit: Maximum number of results to fetch before filtering

        Returns:
            List of entity dictionaries that belong to the data product
        """
        # Use type-specific index for efficiency (queryFilter is ignored by API,
        # so we filter client-side by dataProducts field)
        index_map = {
            "metric": "metric_search_index",
            "table": "table_search_index",
        }
        index = index_map.get(entity_type, "all")

        params = {
            "q": "*",
            "index": index,
            "size": limit,
        }

        response = self._client.get("/api/v1/search/query", params=params)
        response.raise_for_status()

        data = response.json()
        hits = data.get("hits", {}).get("hits", [])
        all_entities = [hit.get("_source", {}) for hit in hits]

        # Client-side filter: only entities that belong to the data product
        filtered = [
            entity for entity in all_entities
            if any(
                dp.get("name") == data_product_name
                for dp in entity.get("dataProducts", [])
            )
        ]

        logger.info(
            f"Data product '{data_product_name}' ({entity_type or 'all'}): "
            f"{len(filtered)}/{len(all_entities)} entities matched"
        )
        return filtered

    def search_data_products_by_tag(
        self, tag_fqn: str, limit: int = 200
    ) -> List[Dict[str, Any]]:
        """Reverse-search data products carrying a classification tag.

        Uses the search index's queryFilter on ``tags.tagFQN`` (reliable for the
        tag field, unlike the dataProducts membership filter). Returns the raw
        ``_source`` dicts.
        """
        query_filter = json.dumps(
            {"query": {"bool": {"must": [{"term": {"tags.tagFQN": tag_fqn}}]}}}
        )
        response = self._client.get(
            "/api/v1/search/query",
            params={
                "q": "*",
                "index": "data_product_search_index",
                "size": limit,
                "query_filter": query_filter,
            },
        )
        response.raise_for_status()
        hits = response.json().get("hits", {}).get("hits", [])
        return [hit.get("_source", {}) for hit in hits]

    def search_tables_by_data_product(
        self, dp_fqn: str, limit: int = 200
    ) -> List[Dict[str, Any]]:
        """Reverse-search the BigQuery tables linked to a data product.

        A data product's ``assets`` array is not reliably populated, so membership
        is read from the table search index by ``dataProducts.fullyQualifiedName``.
        Filtered to the ``bigquery`` service so duplicate non-BQ index entries
        (e.g. an upstream extract) don't double-count.
        """
        query_filter = json.dumps(
            {"query": {"bool": {"must": [
                {"term": {"dataProducts.fullyQualifiedName": dp_fqn}}]}}}
        )
        response = self._client.get(
            "/api/v1/search/query",
            params={
                "q": "*",
                "index": "table_search_index",
                "size": limit,
                "query_filter": query_filter,
            },
        )
        response.raise_for_status()
        hits = response.json().get("hits", {}).get("hits", [])
        sources = [hit.get("_source", {}) for hit in hits]
        return [t for t in sources if str(t.get("serviceType", "")).lower() == "bigquery"]

    def close(self):
        """Close HTTP client session."""
        self._client.close()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
