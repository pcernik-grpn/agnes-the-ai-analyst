"""
Keboola Storage API Client

Wrapper for Keboola Storage API with these functions:
1. Authentication using Storage API token
2. Export tables to CSV (with incremental sync support)
3. Get table metadata (schema, columns, data types)
4. Cache metadata for faster repeated use

Uses official kbcstorage Python client.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta

import pyarrow as pa
import requests
from kbcstorage.client import Client

from dataclasses import dataclass, field


@dataclass
class WhereFilter:
    """Keboola where filter for export operations."""

    column: str
    operator: str = "eq"
    values: list = field(default_factory=list)


logger = logging.getLogger(__name__)


# Mapping Keboola data types to pandas dtypes
# Keboola uses Snowflake data types
KEBOOLA_TO_PANDAS_TYPES = {
    "STRING": "object",
    "VARCHAR": "object",
    "TEXT": "object",
    "INTEGER": "Int64",  # Nullable integer
    "BIGINT": "Int64",
    "NUMERIC": "float64",
    "DECIMAL": "float64",
    "FLOAT": "float64",
    "DOUBLE": "float64",
    "BOOLEAN": "boolean",  # Nullable boolean - handles empty strings
    "DATE": "datetime64[ns]",  # Pandas datetime
    "TIMESTAMP": "datetime64[ns]",
    "TIMESTAMP_NTZ": "datetime64[ns]",  # Without timezone
    "TIMESTAMP_TZ": "datetime64[ns]",  # With timezone
}

# Mapping Keboola data types to PyArrow types
# Used for explicit schema enforcement when writing Parquet files
KEBOOLA_TO_PYARROW_TYPES = {
    "STRING": pa.string(),
    "VARCHAR": pa.string(),
    "TEXT": pa.string(),
    "INTEGER": pa.int64(),
    "BIGINT": pa.int64(),
    "NUMERIC": pa.float64(),
    "DECIMAL": pa.float64(),
    "FLOAT": pa.float64(),
    "DOUBLE": pa.float64(),
    "BOOLEAN": pa.bool_(),
    "DATE": pa.date32(),
    "TIMESTAMP": pa.timestamp("us"),
    "TIMESTAMP_NTZ": pa.timestamp("us"),
    "TIMESTAMP_TZ": pa.timestamp("us", tz="UTC"),
}


class KeboolaClient:
    """
    Wrapper for Keboola Storage API.

    Provides high-level methods for working with Keboola tables:
    - Export tables to CSV
    - Get metadata (schema, columns)
    - Incremental sync with changedSince parameter
    """

    def __init__(self, token: Optional[str] = None, url: Optional[str] = None):
        """
        Initialize Keboola client.

        Args:
            token: Storage API token. If None, loads from configuration.
            url: Stack URL. If None, loads from configuration.
        """
        if token:
            self.token = token
        else:
            from app.datasource_secrets import datasource_secret

            self.token = datasource_secret("KEBOOLA_STORAGE_TOKEN") or ""
        self.url = url or os.environ.get("KEBOOLA_STACK_URL", "")

        if not self.token:
            raise ValueError("Keboola Storage token not set. Set KEBOOLA_STORAGE_TOKEN in .env file.")

        # Initialize kbcstorage client
        self.client = Client(self.url, self.token)

        # Metadata cache
        self.metadata_cache: Dict[str, Dict[str, Any]] = {}
        metadata_dir = Path(os.environ.get("DATA_DIR", "./data")) / "metadata"
        metadata_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_cache_path = metadata_dir / "table_metadata.json"

        # Load cache from disk if exists
        self._load_metadata_cache()

        logger.info(f"Keboola client initialized: {self.url}")

    def _load_metadata_cache(self):
        """
        Load metadata cache from disk.

        Cache is stored in JSON format in data/metadata/table_metadata.json
        """
        if self.metadata_cache_path.exists():
            try:
                with open(self.metadata_cache_path, "r") as f:
                    self.metadata_cache = json.load(f)
                logger.info(f"Metadata cache loaded: {len(self.metadata_cache)} tables")
            except Exception as e:
                logger.warning(f"Error loading metadata cache: {e}")
                self.metadata_cache = {}

    def _save_metadata_cache(self):
        """
        Save metadata cache to disk.
        """
        try:
            self.metadata_cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.metadata_cache_path, "w") as f:
                json.dump(self.metadata_cache, f, indent=2)
            logger.debug("Metadata cache saved")
        except Exception as e:
            logger.warning(f"Error saving metadata cache: {e}")

    def get_table_metadata(self, table_id: str, use_cache: bool = True, cache_ttl_hours: int = 24) -> Dict[str, Any]:
        """
        Get table metadata from Keboola Storage API.

        Metadata includes:
        - Table name
        - Bucket
        - Columns (names and data types)
        - Primary key
        - Row count
        - Size in bytes
        - Last change timestamp

        Args:
            table_id: Full table ID (e.g., "in.c-sfdc.company")
            use_cache: Use cache if available
            cache_ttl_hours: Cache TTL in hours (default 24h)

        Returns:
            Dictionary with metadata

        Raises:
            Exception: If unable to get metadata from API
        """
        # Check cache
        if use_cache and table_id in self.metadata_cache:
            cached = self.metadata_cache[table_id]
            cached_time = datetime.fromisoformat(cached.get("_cached_at", "2000-01-01"))
            cache_age = datetime.now() - cached_time

            if cache_age < timedelta(hours=cache_ttl_hours):
                logger.debug(f"Using metadata cache for {table_id}")
                return cached

        # Get metadata from API
        logger.info(f"Fetching metadata for table: {table_id}")

        try:
            # Keboola API call
            table_detail = self.client.tables.detail(table_id)

            # Get column metadata - for alias tables it's in sourceTable.columnMetadata
            column_metadata = table_detail.get("columnMetadata", {})
            if not column_metadata and table_detail.get("isAlias"):
                # For alias tables, get metadata from source table
                source_table = table_detail.get("sourceTable", {})
                column_metadata = source_table.get("columnMetadata", {})

            # Extract relevant metadata
            metadata = {
                "table_id": table_id,
                "name": table_detail.get("name"),
                "bucket": table_detail.get("bucket", {}).get("id"),
                "columns": table_detail.get("columns", []),
                "column_metadata": column_metadata,
                "primary_key": table_detail.get("primaryKey", []),
                "row_count": table_detail.get("rowsCount", 0),
                "data_size_bytes": table_detail.get("dataSizeBytes", 0),
                "last_import_date": table_detail.get("lastImportDate"),
                "last_change_date": table_detail.get("lastChangeDate"),
                "is_alias": table_detail.get("isAlias", False),
                "_cached_at": datetime.now().isoformat(),
            }

            # Save to cache
            self.metadata_cache[table_id] = metadata
            self._save_metadata_cache()

            return metadata

        except Exception as e:
            logger.error(f"Error getting metadata for {table_id}: {e}")
            raise

    def _resolve_keboola_type(self, col_meta_list: Any) -> str:
        """
        Resolve Keboola data type from column metadata using provider cascade.

        Provider priority: user > ai-metadata-enrichment > snowflake-transformation > storage
        Falls back to STRING if no type found.

        Args:
            col_meta_list: Column metadata from Keboola API (list or dict)

        Returns:
            Keboola type (STRING, INTEGER, DATE, etc.)
        """
        # Provider priority: user > ai-metadata-enrichment > snowflake-transformation > storage
        PROVIDER_PRIORITY = ["user", "ai-metadata-enrichment", "keboola.snowflake-transformation", "storage"]

        kbc_type = "STRING"  # default

        if isinstance(col_meta_list, list):
            # Group basetype by provider
            basetype_by_provider = {}
            for meta_entry in col_meta_list:
                provider = meta_entry.get("provider", "")
                key = meta_entry.get("key", "")
                value = meta_entry.get("value", "")

                if key == "KBC.datatype.basetype":
                    basetype_by_provider[provider] = value.upper()
                elif key == "KBC.datatype.type" and provider not in basetype_by_provider:
                    basetype_by_provider[provider] = value.upper()

            # Apply cascade: first matching provider wins
            for provider in PROVIDER_PRIORITY:
                if provider in basetype_by_provider:
                    kbc_type = basetype_by_provider[provider]
                    break

        elif isinstance(col_meta_list, dict):
            # Fallback for old format (dict with direct type key)
            kbc_type = col_meta_list.get("type", "STRING")

        return kbc_type

    def get_pandas_dtypes(self, table_id: str) -> Dict[str, str]:
        """
        Get column to pandas dtype mapping for given table.

        Uses metadata from Keboola API and converts Keboola data types
        to pandas dtypes according to KEBOOLA_TO_PANDAS_TYPES mapping.

        Args:
            table_id: Full table ID

        Returns:
            Dictionary {column_name: pandas_dtype}
        """
        metadata = self.get_table_metadata(table_id)
        column_metadata = metadata.get("column_metadata", {})

        dtypes = {}
        for col_name, col_meta_list in column_metadata.items():
            # Resolve Keboola type using provider cascade
            kbc_type = self._resolve_keboola_type(col_meta_list)

            # Convert to pandas dtype
            pandas_type = KEBOOLA_TO_PANDAS_TYPES.get(kbc_type, "object")
            dtypes[col_name] = pandas_type

        # If metadata not available, default to object for all columns
        if not dtypes:
            columns = metadata.get("columns", [])
            dtypes = {col: "object" for col in columns}
            logger.warning(f"Column metadata not available for {table_id}, using 'object' for all columns")

        return dtypes

    def get_date_columns(self, table_id: str) -> List[str]:
        """
        Get list of DATE-only columns (without time component) for given table.

        These columns should be stored as PyArrow DATE32 instead of TIMESTAMP.

        Args:
            table_id: Full table ID

        Returns:
            List of column names that have DATE type in Keboola
        """
        metadata = self.get_table_metadata(table_id)
        column_metadata = metadata.get("column_metadata", {})

        date_columns = []
        for col_name, col_meta_list in column_metadata.items():
            # Resolve Keboola type using provider cascade
            kbc_type = self._resolve_keboola_type(col_meta_list)

            # Add to list if it's a DATE type (not TIMESTAMP)
            if kbc_type == "DATE":
                date_columns.append(col_name)

        return date_columns

    def get_pyarrow_schema(self, table_id: str) -> Optional[pa.Schema]:
        """
        Get PyArrow schema for given table from Keboola metadata.

        Builds explicit schema from column metadata using provider cascade.
        This ensures columns with all-NULL values get correct type (not null type).

        Args:
            table_id: Full table ID

        Returns:
            PyArrow schema or None if metadata unavailable
        """
        metadata = self.get_table_metadata(table_id)
        column_metadata = metadata.get("column_metadata", {})

        # If no column metadata, return None (graceful fallback)
        if not column_metadata:
            logger.warning(f"Column metadata not available for {table_id}, PyArrow schema will not be applied")
            return None

        fields = []
        for col_name, col_meta_list in column_metadata.items():
            # Resolve Keboola type using provider cascade
            kbc_type = self._resolve_keboola_type(col_meta_list)

            # Convert to PyArrow type
            pa_type = KEBOOLA_TO_PYARROW_TYPES.get(kbc_type, pa.string())
            fields.append(pa.field(col_name, pa_type))

        return pa.schema(fields)

    def _convert_to_unix_timestamp(self, timestamp_str: str) -> str:
        """
        Convert ISO timestamp to Unix timestamp string.

        Keboola API's changedSince parameter expects Unix timestamp.

        Args:
            timestamp_str: ISO timestamp (e.g., "2026-01-20T00:00:00") or Unix timestamp

        Returns:
            Unix timestamp as string
        """
        # If already numeric (Unix timestamp), return as-is
        if timestamp_str.isdigit():
            return timestamp_str

        # Parse ISO timestamp and convert to Unix
        try:
            dt = datetime.fromisoformat(timestamp_str)
            return str(int(dt.timestamp()))
        except ValueError:
            # If parsing fails, return original (let API handle the error)
            logger.warning(f"Could not parse timestamp: {timestamp_str}")
            return timestamp_str

    def export_table(
        self,
        table_id: str,
        output_path: Path,
        changed_since: Optional[str] = None,
        changed_until: Optional[str] = None,
        columns: Optional[List[str]] = None,
        where_filters: Optional[List[WhereFilter]] = None,
    ) -> Dict[str, Any]:
        """
        Export table from Keboola to CSV file.

        Supports incremental export using changedSince parameter.
        Supports row filtering using whereFilters parameter.

        Args:
            table_id: Full table ID (e.g., "in.c-sfdc.company")
            output_path: Path where to save CSV file
            changed_since: Timestamp for incremental export. Accepts both:
                          - ISO format (e.g., "2026-01-20T00:00:00") - will be converted
                          - Unix timestamp (e.g., "1737331200")
                          Downloads only rows changed after this timestamp.
            changed_until: Timestamp for limiting export window. Accepts both:
                          - ISO format (e.g., "2026-01-20T00:00:00") - will be converted
                          - Unix timestamp (e.g., "1737331200")
                          Downloads only rows changed before this timestamp.
                          Used with changed_since for chunked initial loads.
            columns: List of columns to export. If None, exports all.
            where_filters: List of WhereFilter objects for filtering rows on server side.

        Returns:
            Dictionary with export information:
            - exported_rows: Number of exported rows
            - file_size_bytes: Size of CSV file
            - export_time: Export timestamp

        Raises:
            Exception: If export fails
        """
        # Convert ISO timestamp to Unix if provided
        if changed_since:
            changed_since = self._convert_to_unix_timestamp(changed_since)
        if changed_until:
            changed_until = self._convert_to_unix_timestamp(changed_until)

        # If where_filters are specified, use direct API call (without changedSince)
        if where_filters:
            return self._export_table_with_filters(
                table_id=table_id, output_path=output_path, where_filters=where_filters, columns=columns
            )

        logger.info(
            f"Exporting table {table_id} -> {output_path} "
            f"(changedSince: {changed_since or 'None'}, changedUntil: {changed_until or 'None'})"
        )

        # Ensure output folder exists
        output_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            # Keboola API call - export table to folder
            # NOTE: export_to_file takes path_name as FOLDER, not file!
            # Creates file as {path_name}/{table_name} (or .csv.gz if gzipped)
            # For sliced tables, also creates slice files in CWD (!), so we change CWD to temp_dir
            temp_dir = output_path.parent

            # Change CWD to temp_dir - Keboola SDK downloads sliced files to CWD
            original_cwd = Path.cwd()
            os.chdir(temp_dir)

            try:
                # API: export_to_file(table_id, path_name, limit, file_format, changed_since, changed_until, columns, ...)
                self.client.tables.export_to_file(
                    table_id=table_id,
                    path_name=str(temp_dir),
                    changed_since=changed_since,  # For incremental sync
                    changed_until=changed_until,  # For chunked initial load
                    columns=columns,  # Optionally - select only some columns
                    is_gzip=False,  # Don't want gzip compressed CSV
                )
            finally:
                os.chdir(original_cwd)

            # Keboola creates file as {temp_dir}/{table_name} (WITHOUT extension!)
            # We need to rename it to our output_path
            table_name = table_id.split(".")[-1]  # Extract table name from ID
            actual_file = temp_dir / table_name  # WITHOUT .csv extension!

            if actual_file.exists():
                # Rename to desired name (with .csv extension)
                actual_file.rename(output_path)
            else:
                raise FileNotFoundError(f"Keboola didn't create expected file: {actual_file}")

            # Clean up slice files (*.csv_*_*_*.csv) that Keboola creates for large tables
            for slice_file in temp_dir.glob("*.csv_*_*_*.csv"):
                slice_file.unlink()
            for slice_file in temp_dir.glob("*.csv.gz_*"):
                slice_file.unlink()

            # Get info about exported file
            file_size = output_path.stat().st_size

            # Count rows (quick way using wc -l would be better for large files,
            # but for consistency we use Python)
            with open(output_path, "r") as f:
                # First line is header, rest are data
                row_count = sum(1 for _ in f) - 1

            export_info = {
                "table_id": table_id,
                "exported_rows": row_count,
                "file_size_bytes": file_size,
                "export_time": datetime.now().isoformat(),
                "changed_since": changed_since,
                "changed_until": changed_until,
                "output_path": str(output_path),
            }

            logger.info(f"Export complete: {row_count} rows, {file_size / 1024 / 1024:.2f} MB")

            return export_info

        except Exception as e:
            logger.error(f"Error exporting table {table_id}: {e}")
            # Delete partially downloaded file
            if output_path.exists():
                output_path.unlink()
            raise

    def _export_table_with_filters(
        self, table_id: str, output_path: Path, where_filters: List[WhereFilter], columns: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Export table with whereFilters using direct API call.

        Uses async export endpoint which supports whereFilters parameter.

        Args:
            table_id: Full table ID
            output_path: Path where to save CSV file
            where_filters: List of WhereFilter objects
            columns: List of columns to export (optional)

        Returns:
            Dictionary with export information
        """
        filters_desc = ", ".join(f"{f.column} {f.operator} {f.values}" for f in where_filters)
        logger.info(f"Exporting table {table_id} -> {output_path} (filters: {filters_desc})")

        # Ensure output folder exists
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Build request payload
        payload = {}

        # Add whereFilters
        for i, wf in enumerate(where_filters):
            payload[f"whereFilters[{i}][column]"] = wf.column
            payload[f"whereFilters[{i}][operator]"] = wf.operator
            for j, val in enumerate(wf.values):
                payload[f"whereFilters[{i}][values][{j}]"] = val
            logger.info(f"  Filter: {wf.column} {wf.operator} {wf.values}")

        # Add columns if specified
        if columns:
            for i, col in enumerate(columns):
                payload[f"columns[{i}]"] = col

        headers = {"X-StorageApi-Token": self.token}

        try:
            # 1. Start async export job
            export_url = f"{self.url}/v2/storage/tables/{table_id}/export-async"
            logger.debug(f"Starting async export: {export_url}")

            response = requests.post(export_url, headers=headers, data=payload)
            response.raise_for_status()

            job_data = response.json()
            job_id = job_data.get("id")

            if not job_id:
                raise ValueError(f"No job ID in response: {job_data}")

            logger.debug(f"Export job started: {job_id}")

            # 2. Poll for job completion
            job_url = f"{self.url}/v2/storage/jobs/{job_id}"
            max_wait = 600  # 10 minutes
            poll_interval = 2  # seconds
            elapsed = 0

            while elapsed < max_wait:
                time.sleep(poll_interval)
                elapsed += poll_interval

                job_response = requests.get(job_url, headers=headers)
                job_response.raise_for_status()
                job_status = job_response.json()

                status = job_status.get("status")
                logger.debug(f"Job {job_id} status: {status}")

                if status == "success":
                    break
                elif status == "error":
                    error_msg = job_status.get("error", {}).get("message", "Unknown error")
                    raise Exception(f"Export job failed: {error_msg}")
                elif status in ("waiting", "processing"):
                    continue
                else:
                    raise Exception(f"Unknown job status: {status}")
            else:
                raise TimeoutError(f"Export job timed out after {max_wait} seconds")

            # 3. Download the file
            file_info = job_status.get("results", {}).get("file", {})
            file_id = file_info.get("id")

            if not file_id:
                raise ValueError(f"No file ID in job results: {job_status}")

            # Get file download URL with federation token for GCS credentials
            file_url = f"{self.url}/v2/storage/files/{file_id}?federationToken=1"
            file_response = requests.get(file_url, headers=headers)
            file_response.raise_for_status()
            file_data = file_response.json()

            download_url = file_data.get("url")
            if not download_url:
                raise ValueError(f"No download URL in file data: {file_data}")

            # Extract GCS credentials if available (for sliced files)
            gcs_credentials = file_data.get("gcsCredentials", {})
            gcs_access_token = gcs_credentials.get("access_token")

            # Check if file is sliced (multiple parts)
            is_sliced = file_data.get("isSliced", False)

            if is_sliced:
                # Download manifest to get list of slice URLs
                logger.debug("Downloading sliced file manifest")
                manifest_response = requests.get(download_url)
                manifest_response.raise_for_status()
                manifest = manifest_response.json()

                # Download all slices and combine
                slice_entries = manifest.get("entries", [])
                logger.debug(f"Found {len(slice_entries)} slices")

                # Get column names from table metadata (sliced files don't have headers!)
                table_metadata = self.get_table_metadata(table_id)
                column_names = table_metadata.get("columns", [])
                if not column_names:
                    raise ValueError(f"No columns found in metadata for {table_id}")

                # Create CSV header line
                header_line = ",".join(f'"{col}"' for col in column_names) + "\n"

                with open(output_path, "wb") as outfile:
                    # Write header first (sliced files don't contain headers)
                    outfile.write(header_line.encode("utf-8"))

                    for i, entry in enumerate(slice_entries):
                        slice_url = entry.get("url")
                        if not slice_url:
                            continue

                        logger.debug(f"Downloading slice {i + 1}/{len(slice_entries)}")

                        # Every backend's scheme rewrite lives in the Storage
                        # API client's dispatch, not here. This path grew its
                        # own chain once and it silently lacked an azure://
                        # arm long after the other one gained it, so the two
                        # must not diverge again.
                        from connectors.keboola.storage_api import KeboolaStorageClient

                        slice_url, extra_headers = KeboolaStorageClient._prepare_slice_request(
                            slice_url,
                            i,
                            gcs_token=gcs_access_token,
                            abs_credentials=file_data.get("absCredentials") or {},
                            s3_context=KeboolaStorageClient._s3_context(file_data),
                        )
                        slice_headers = dict(extra_headers or {})

                        slice_response = requests.get(slice_url, headers=slice_headers)
                        slice_response.raise_for_status()

                        # Check if slice is gzipped
                        if slice_url.endswith(".gz"):
                            import gzip
                            import io

                            with gzip.GzipFile(fileobj=io.BytesIO(slice_response.content)) as gz:
                                content = gz.read()
                        else:
                            content = slice_response.content

                        # Write slice content (no header skipping - slices don't have headers)
                        outfile.write(content)

                        # Ensure newline at end of slice if missing
                        if content and not content.endswith(b"\n"):
                            outfile.write(b"\n")
            else:
                # Single file download
                logger.debug(f"Downloading from: {download_url}")
                download_response = requests.get(download_url, stream=True)
                download_response.raise_for_status()

                # Check if gzipped
                content_encoding = download_response.headers.get("Content-Encoding", "")
                is_gzip = "gzip" in content_encoding.lower() or download_url.endswith(".gz")

                if is_gzip:
                    import gzip
                    import io

                    compressed_data = download_response.content
                    with gzip.GzipFile(fileobj=io.BytesIO(compressed_data)) as gz:
                        with open(output_path, "wb") as f:
                            f.write(gz.read())
                else:
                    with open(output_path, "wb") as f:
                        for chunk in download_response.iter_content(chunk_size=8192):
                            f.write(chunk)

            # Get info about exported file
            file_size = output_path.stat().st_size

            # Count rows
            with open(output_path, "r") as f:
                row_count = sum(1 for _ in f) - 1  # -1 for header

            export_info = {
                "table_id": table_id,
                "exported_rows": row_count,
                "file_size_bytes": file_size,
                "export_time": datetime.now().isoformat(),
                "where_filters": [
                    {"column": wf.column, "operator": wf.operator, "values": wf.values} for wf in where_filters
                ],
                "output_path": str(output_path),
            }

            logger.info(f"Export complete: {row_count} rows, {file_size / 1024 / 1024:.2f} MB")

            return export_info

        except Exception as e:
            logger.error(f"Error exporting table {table_id}: {e}")
            if output_path.exists():
                output_path.unlink()
            raise

    def discover_all_tables(self) -> List[Dict[str, Any]]:
        """List all available tables in the Keboola project.

        Tries tables.list(include=["columns","buckets"]) first.
        Falls back to per-bucket listing if that fails.

        Returns:
            Normalized list of table dicts.
        """
        logger.info("Discovering all tables in Keboola project...")

        try:
            raw_tables = self.client.tables.list(include="columns,buckets")
        except Exception as e:
            logger.warning(f"tables.list() failed ({e}), falling back to per-bucket listing")
            raw_tables = []
            for bucket in self.client.buckets.list():
                bucket_id = bucket["id"]
                try:
                    bucket_tables = self.client.buckets.list_tables(bucket_id, include="columns")
                    for t in bucket_tables:
                        t.setdefault("bucket", bucket)
                    raw_tables.extend(bucket_tables)
                except Exception as be:
                    logger.warning(f"Could not list tables in bucket {bucket_id}: {be}")

        result = []
        for t in raw_tables:
            bucket = t.get("bucket", {})
            result.append(
                {
                    "id": t.get("id", ""),
                    "name": t.get("name", ""),
                    "bucket_id": bucket.get("id", ""),
                    "bucket_name": bucket.get("name", bucket.get("id", "")),
                    "columns": t.get("columns", []),
                    "row_count": t.get("rowsCount", 0),
                    "size_bytes": t.get("dataSizeBytes", 0),
                    "primary_key": t.get("primaryKey", []),
                    "last_change": t.get("lastChangeDate"),
                    "last_import": t.get("lastImportDate"),
                }
            )

        logger.info(f"Discovered {len(result)} tables")
        return result

    def test_connection(self) -> bool:
        """
        Test connection to Keboola API.

        Returns:
            True if connection works, False otherwise
        """
        try:
            # Try to get list of buckets as connection test
            # (verification methods differ between kbcstorage versions)
            buckets = self.client.buckets.list()
            logger.info(f"Connection to Keboola OK. Found {len(buckets)} buckets.")
            return True
        except Exception as e:
            logger.error(f"Error testing connection: {e}")
            return False


def create_client() -> KeboolaClient:
    """
    Factory function to create Keboola client.

    Uses environment variables for token and URL.

    Returns:
        KeboolaClient instance
    """
    return KeboolaClient()


if __name__ == "__main__":
    # Test Keboola client
    print("🔌 Testing Keboola client...")

    try:
        # Create client
        client = create_client()

        # Test connection
        print("\n1️⃣ Testing connection...")
        if client.test_connection():
            print("   ✅ Connection works!")
        else:
            print("   ❌ Connection failed!")
            exit(1)

        # Test metadata with discovered tables
        print("\n2️⃣ Testing metadata...")
        tables = client.discover_all_tables()
        if tables:
            test_table_id = tables[0].get("id", tables[0].get("name", ""))
            print(f"   Testing table: {test_table_id}")

            metadata = client.get_table_metadata(test_table_id)
            print("   ✅ Metadata loaded:")
            print(f"      Columns: {len(metadata.get('columns', []))}")
            print(f"      Rows: {metadata.get('row_count', 0):,}")
            print(f"      Size: {metadata.get('data_size_bytes', 0) / 1024 / 1024:.2f} MB")

            # Test dtypes
            dtypes = client.get_pandas_dtypes(test_table_id)
            print("      Pandas dtypes:")
            for col, dtype in list(dtypes.items())[:5]:
                print(f"         {col}: {dtype}")
            if len(dtypes) > 5:
                print(f"         ... and {len(dtypes) - 5} more")

        print("\n✅ All tests passed!")

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback

        traceback.print_exc()
