#!/usr/bin/env python3
"""
Jira Data Consistency Monitoring Script

Validates data integrity by comparing three sources:
1. Jira API (ground truth)
2. Raw JSON files (/data/src_data/raw/jira/issues/*.json)
3. Parquet files (/data/src_data/parquet/jira/issues/*.parquet)

Automatically backfills small gaps (1-10 issues), alerts on large ones (11+).
Runs every 30 minutes via systemd timer to detect webhook losses and transform failures.

Usage:
    # Dry run (check only, no fixes)
    python -m connectors.jira.scripts.consistency_check --dry-run --max-age-days 7

    # Auto-fix mode (default)
    python -m connectors.jira.scripts.consistency_check --auto-fix --max-age-days 30

    # Weekly deep check (full history)
    python -m connectors.jira.scripts.consistency_check --auto-fix --max-age-days 365

Environment variables (loaded from .env):
    JIRA_DOMAIN - Jira Cloud domain (e.g., your-org.atlassian.net)
    JIRA_EMAIL - Email for API authentication
    JIRA_API_TOKEN - API token from Atlassian
    JIRA_DATA_DIR - Directory for storing data (default: /data/src_data/raw/jira)
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Try to import DuckDB for Parquet queries
try:
    import duckdb

    HAS_DUCKDB = True
except ImportError:
    HAS_DUCKDB = False
    logging.warning("DuckDB not available, Parquet validation will be skipped")

from app.logging_config import setup_logging

setup_logging(__name__)
logger = logging.getLogger(__name__)


@dataclass
class Config:
    """Configuration loaded from environment."""

    jira_domain: str
    jira_email: str
    jira_api_token: str
    raw_dir: Path
    parquet_dir: Path
    repo_dir: Path
    venv_python: Path

    @classmethod
    def from_env(cls) -> "Config":
        """Load configuration from environment variables."""
        # Try to load .env file from common locations.
        # Customer-specific install paths (e.g. /opt/<deployment>/.env) can be
        # injected via the AGNES_ENV_FILE env var without editing this list.
        env_paths = [
            Path(os.environ["AGNES_ENV_FILE"]) if os.environ.get("AGNES_ENV_FILE") else None,
            Path.cwd() / ".env",
            Path(__file__).parent.parent / ".env",
        ]
        env_paths = [p for p in env_paths if p is not None]
        for env_path in env_paths:
            if env_path.exists():
                load_dotenv(env_path)
                logger.info(f"Loaded environment from {env_path}")
                break

        # Validate required variables
        required = ["JIRA_DOMAIN", "JIRA_EMAIL", "JIRA_API_TOKEN"]
        missing = [var for var in required if not os.environ.get(var)]
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

        raw_dir = Path(os.environ.get("JIRA_DATA_DIR", "/data/src_data/raw/jira"))
        parquet_dir = Path(os.environ.get("JIRA_PARQUET_DIR", "/data/src_data/parquet/jira"))
        # REPO_DIR / VENV_PYTHON have no sensible OSS default — operators
        # must export them when running this script outside an editable
        # checkout.
        repo_dir = Path(os.environ.get("REPO_DIR", str(Path(__file__).resolve().parents[3])))
        venv_python = Path(os.environ.get("VENV_PYTHON", sys.executable))

        return cls(
            jira_domain=os.environ["JIRA_DOMAIN"],
            jira_email=os.environ["JIRA_EMAIL"],
            jira_api_token=os.environ["JIRA_API_TOKEN"],
            raw_dir=raw_dir,
            parquet_dir=parquet_dir,
            repo_dir=repo_dir,
            venv_python=venv_python,
        )


class JiraConsistencyChecker:
    """Checks data consistency across Jira API, JSON, and Parquet."""

    # Grace period for new issues (avoid false positives from timing windows)
    GRACE_PERIOD_MINUTES = 5

    # Thresholds for auto-backfill
    AUTO_FIX_THRESHOLD = 20  # Auto-fix if ≤20 issues missing
    WARNING_THRESHOLD = 5  # Log WARNING if >5 issues

    # Jira API limits
    MAX_RESULTS_PER_PAGE = 100

    # Sentinel reported in `parquet_read_failed` when the Parquet scan could
    # not run at all (as opposed to individual files failing to read). Not a
    # path, and deliberately shaped so it cannot collide with one, because the
    # distinction it carries is "we never looked" rather than "this file is
    # broken" — the run must not then report a corpus-sized gap it never
    # measured.
    PARQUET_SCAN_UNAVAILABLE = "<parquet-scan-unavailable: duckdb not installed>"

    def __init__(self, config: Config):
        self.config = config
        self.base_url = f"https://{config.jira_domain}/rest/api/3"
        self.auth = (config.jira_email, config.jira_api_token)

        # Statistics
        self.stats = {
            "jira_api_count": 0,
            "raw_json_count": 0,
            "parquet_count": 0,
            "missing_in_json": [],
            "missing_in_parquet": [],
            "deleted_in_jira": 0,
            "auto_backfilled": [],
            "transformed": [],
            "backfill_failed": [],
            "transform_failed": [],
            "parquet_read_failed": [],
        }

    def fetch_jira_keys(self, max_age_days: int = 30) -> set[str]:
        """
        Query Jira API for all issue keys via JQL search.

        Args:
            max_age_days: Only fetch issues created in last N days

        Returns:
            Set of issue keys from Jira API (ground truth)
        """
        # Calculate cutoff date with grace period
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        grace_cutoff = datetime.now(timezone.utc) - timedelta(minutes=self.GRACE_PERIOD_MINUTES)

        # JQL: fetch issues created after cutoff, but not too recent (grace period)
        jira_project = os.environ.get("JIRA_PROJECT", "")
        project_clause = f'project = "{jira_project}" AND ' if jira_project else ""
        jql = (
            f"{project_clause}"
            f'created >= "{cutoff.strftime("%Y-%m-%d")}" '
            f'AND created <= "{grace_cutoff.strftime("%Y-%m-%d %H:%M")}"'
        )

        logger.info(f"Querying Jira API with JQL: {jql}")

        issue_keys = set()
        next_page_token = None

        while True:
            try:
                result = self._search_issues(jql, next_page_token)
                issues = result.get("issues", [])

                if not issues:
                    break

                for issue in issues:
                    issue_keys.add(issue["key"])

                # Check for next page
                next_page_token = result.get("nextPageToken")
                if not next_page_token:
                    break

                # Respect rate limits
                time.sleep(0.1)

            except Exception as e:
                logger.error(f"Error querying Jira API: {e}")
                raise

        self.stats["jira_api_count"] = len(issue_keys)
        logger.info(f"Found {len(issue_keys)} issues from Jira API")
        return issue_keys

    def _search_issues(self, jql: str, next_page_token: str | None = None) -> dict:
        """Execute JQL search with pagination."""
        url = f"{self.base_url}/search/jql"
        payload = {
            "jql": jql,
            "maxResults": self.MAX_RESULTS_PER_PAGE,
            "fields": ["key"],  # Only need keys
        }

        if next_page_token:
            payload["nextPageToken"] = next_page_token

        with httpx.Client(timeout=60) as client:
            response = client.post(
                url,
                auth=self.auth,
                json=payload,
                headers={"Content-Type": "application/json"},
            )

        if response.status_code != 200:
            raise RuntimeError(f"Jira search failed: {response.status_code} - {response.text[:200]}")

        return response.json()

    def scan_json_keys(self) -> set[str]:
        """
        List issue keys from raw JSON files.

        Excludes deleted issues (those with _deleted_at field).

        Returns:
            Set of issue keys from JSON files
        """
        issues_dir = self.config.raw_dir / "issues"
        if not issues_dir.exists():
            logger.warning(f"Issues directory not found: {issues_dir}")
            return set()

        issue_keys = set()

        for json_file in issues_dir.glob("*.json"):
            try:
                with open(json_file) as f:
                    data = json.load(f)

                # Skip deleted issues
                if data.get("_deleted_at"):
                    continue

                issue_key = data.get("key")
                if issue_key:
                    issue_keys.add(issue_key)
                else:
                    logger.warning(f"JSON file missing 'key' field: {json_file}")

            except Exception as e:
                logger.warning(f"Error reading {json_file}: {e}")

        self.stats["raw_json_count"] = len(issue_keys)
        logger.info(f"Found {len(issue_keys)} issues in raw JSON")
        return issue_keys

    def scan_parquet_keys(self, month: str | None = None) -> tuple[set[str], list[str]]:
        """
        Query Parquet files for distinct issue keys using DuckDB.

        Reads each file individually — mirrors ``find_open_issues`` in
        ``scripts/poll_sla.py`` — so a single corrupt partition costs only
        its own rows. The old combined ``read_parquet([...])`` over every
        file at once meant one bad file failed the WHOLE read, and the
        caller could not tell "no Parquet files exist" apart from "some
        Parquet files could not be read": both returned an empty set.

        Args:
            month: Optional month filter (e.g., "2026-02")

        Returns:
            Tuple of (issue keys found, paths that failed to read). An empty
            keys set together with a non-empty failure list means the scan
            could not read some/all Parquet data — callers must NOT treat
            that the same as a genuinely empty corpus.

            The failure list holds file paths, except for one sentinel:
            `PARQUET_SCAN_UNAVAILABLE`, reported when the scan could not run
            at all (no DuckDB) and there are therefore no paths to name. A
            caller seeing it knows the whole Parquet side is unmeasured, not
            that some files are broken — `run_check` uses it to suppress a
            corpus-sized "missing in Parquet" gap it never actually observed.

            "No Parquet directory" is NOT a failure: the scan ran and found
            nothing, which on a never-transformed instance is a real gap.
        """
        if not HAS_DUCKDB:
            # NOT `return set(), []` — that is the "genuinely empty corpus"
            # answer, and this is the opposite: the scan could not run at all.
            # Conflating them is the exact bug this function's contract exists
            # to prevent, one level up from the per-file case: with no keys
            # read, EVERY issue looks missing from Parquet, which then reports
            # a corpus-sized gap nobody measured (Devin review).
            logger.error(
                "DuckDB is not available — Parquet validation cannot run. Reporting a scan "
                "failure rather than an empty corpus, so the run cannot claim success."
            )
            return set(), [self.PARQUET_SCAN_UNAVAILABLE]

        issues_dir = self.config.parquet_dir / "issues"
        if not issues_dir.exists():
            # Deliberately the empty-corpus answer, unlike the branch above:
            # here the scan DID run and found nothing to read. On a fresh
            # instance that has raw JSON but has never been transformed, the
            # whole-corpus Parquet gap is real, and the over-threshold
            # "manual review required" path is the correct response — it
            # mirrors what `missing_in_json` has always done at that size.
            logger.warning(f"Parquet directory not found: {issues_dir}")
            return set(), []

        # Collect parquet files across both the flat (<YYYY-MM>.parquet) and
        # hive (month=<YYYY-MM>/data.parquet) layouts.
        if month:
            candidates = [
                issues_dir / f"{month}.parquet",
                issues_dir / f"month={month}" / "data.parquet",
            ]
            files = [p for p in candidates if p.exists()]
        else:
            files = sorted(issues_dir.rglob("*.parquet"))

        if not files:
            return set(), []

        con = duckdb.connect()
        try:
            con.execute("SET GLOBAL TimeZone='UTC'")
        except duckdb.Error:
            pass

        issue_keys: set[str] = set()
        failed_files: list[str] = []
        for pf in files:
            quoted_path = "'" + str(pf).replace("'", "''") + "'"
            try:
                result = con.execute(f"SELECT DISTINCT issue_key FROM read_parquet({quoted_path})").fetchall()
                issue_keys.update(row[0] for row in result)
            except Exception as e:
                failed_files.append(str(pf))
                logger.error(f"Failed to read {pf} — its issues will appear as missing in Parquet: {e}")

        self.stats["parquet_count"] = len(issue_keys)
        logger.info(f"Found {len(issue_keys)} issues in Parquet files")
        if failed_files:
            logger.error(
                f"Could not read {len(failed_files)} of {len(files)} Parquet file(s): {', '.join(failed_files)}"
            )
        return issue_keys, failed_files

    def detect_discrepancies(
        self,
        jira_keys: set[str],
        json_keys: set[str],
        parquet_keys: set[str],
    ) -> dict:
        """
        Compare three sources and identify gaps.

        Args:
            jira_keys: Issue keys from Jira API
            json_keys: Issue keys from JSON files
            parquet_keys: Issue keys from Parquet files

        Returns:
            Dict with discrepancy lists
        """
        missing_in_json = sorted(jira_keys - json_keys)
        missing_in_parquet = sorted(json_keys - parquet_keys)
        deleted_in_jira = len(json_keys - jira_keys)

        self.stats["missing_in_json"] = missing_in_json
        self.stats["missing_in_parquet"] = missing_in_parquet
        self.stats["deleted_in_jira"] = deleted_in_jira

        logger.info("Discrepancies detected:")
        logger.info(f"  Missing in JSON (webhook loss): {len(missing_in_json)}")
        logger.info(f"  Missing in Parquet (transform lag): {len(missing_in_parquet)}")
        logger.info(f"  Deleted in Jira (expected): {deleted_in_jira}")

        return {
            "missing_in_json": missing_in_json,
            "missing_in_parquet": missing_in_parquet,
            "deleted_in_jira": deleted_in_jira,
        }

    def backfill_issues(self, issue_keys: list[str]) -> tuple[list[str], list[str]]:
        """
        Backfill specific issues using jira_backfill.py script.

        Args:
            issue_keys: List of issue keys to backfill

        Returns:
            Tuple of (successful_keys, failed_keys)
        """
        if not issue_keys:
            return [], []

        logger.info(f"Backfilling {len(issue_keys)} issues: {', '.join(issue_keys)}")

        # Build command for targeted backfill (force re-download to fix corrupted files)
        cmd = [
            str(self.config.venv_python),
            str(self.config.repo_dir / "connectors" / "jira" / "scripts" / "backfill.py"),
            "--issue-keys",
            ",".join(issue_keys),
            "--no-skip-existing",  # Force re-download even if files exist
        ]

        try:
            result = subprocess.run(
                cmd,
                cwd=self.config.repo_dir,
                capture_output=True,
                text=True,
                timeout=600,  # 10 minutes timeout
            )

            if result.returncode == 0:
                logger.info(f"Backfill completed successfully for {len(issue_keys)} issues")
                return issue_keys, []
            else:
                logger.error(f"Backfill failed: {result.stderr}")
                return [], issue_keys

        except subprocess.TimeoutExpired:
            logger.error("Backfill timed out after 10 minutes")
            return [], issue_keys
        except Exception as e:
            logger.error(f"Error running backfill: {e}")
            return [], issue_keys

    def _enqueue_jira_refresh(self) -> None:
        """Ask for one coalesced `jira-refresh` after this run's parquet writes.

        Mirrors the webhook path in `connectors/jira/service.py`, including its
        follow-up: the dedup matches `status IN ('queued', 'running')`, so
        collapsing onto a refresh that is already RUNNING is not enough — that job
        may have read the parquet before these writes landed, and nothing else
        would recover them.

        Non-fatal by design. This module runs as a standalone script and from a
        systemd timer as well as from the admin endpoint, and the checker's real
        work — backfilled JSON and rewritten parquet — is durable before this runs.
        """
        try:
            from app.job_correlation import stamp_request_id
            from src.repositories import jobs_repo

            result = jobs_repo().enqueue("jira-refresh", stamp_request_id({}), idempotency_key="jira-refresh")
            if result.get("status") == "running":
                jobs_repo().enqueue("jira-refresh", stamp_request_id({}), idempotency_key="jira-refresh-followup")
        except Exception as enqueue_err:
            logger.warning(f"Could not enqueue jira-refresh after the consistency check: {enqueue_err}")

    def transform_issues(self, issue_keys: list[str]) -> tuple[list[str], list[str]]:
        """
        Trigger incremental Parquet transform for specific issues.

        Args:
            issue_keys: List of issue keys to transform

        Returns:
            Tuple of (successful_keys, failed_keys)
        """
        if not issue_keys:
            return [], []

        logger.info(f"Transforming {len(issue_keys)} issues to Parquet")

        successful = []
        failed = []

        for issue_key in issue_keys:
            try:
                # Use subprocess to call transform module (ensures correct Python path)
                cmd = [
                    str(self.config.venv_python),
                    "-m",
                    "connectors.jira.incremental_transform",
                    issue_key,
                    "--raw-dir",
                    str(self.config.raw_dir),
                    "--output-dir",
                    str(self.config.parquet_dir),
                ]

                result = subprocess.run(
                    cmd,
                    cwd=self.config.repo_dir,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )

                if result.returncode == 0:
                    successful.append(issue_key)
                else:
                    logger.error(f"Transform failed for {issue_key}: {result.stderr}")
                    failed.append(issue_key)

            except subprocess.TimeoutExpired:
                logger.error(f"Transform timed out for {issue_key}")
                failed.append(issue_key)
            except Exception as e:
                logger.error(f"Transform error for {issue_key}: {e}")
                failed.append(issue_key)

        if successful:
            logger.info(f"Transformed {len(successful)} issues successfully")
        if failed:
            logger.warning(f"Transform failed for {len(failed)} issues: {', '.join(failed)}")

        return successful, failed

    def get_alert_level(self, missing_count: int) -> str:
        """Determine alert level based on missing count."""
        if missing_count == 0:
            return "INFO"
        elif missing_count <= self.WARNING_THRESHOLD:
            return "INFO"
        elif missing_count <= self.AUTO_FIX_THRESHOLD:
            return "WARNING"
        else:
            return "ERROR"

    def run_check(
        self,
        max_age_days: int = 30,
        auto_fix: bool = True,
        dry_run: bool = False,
    ) -> dict:
        """
        Run consistency check and optionally auto-fix discrepancies.

        Args:
            max_age_days: Check issues created in last N days
            auto_fix: Enable automatic backfill for small gaps
            dry_run: Only check, don't fix anything

        Returns:
            Statistics dict with results
        """
        start_time = time.time()
        logger.info("=" * 60)
        logger.info("Starting Jira consistency check")
        logger.info(f"Max age: {max_age_days} days")
        logger.info(f"Auto-fix: {auto_fix}")
        logger.info(f"Dry run: {dry_run}")
        logger.info("=" * 60)

        # Fetch data from all three sources
        try:
            jira_keys = self.fetch_jira_keys(max_age_days)
            json_keys = self.scan_json_keys()
            parquet_keys, parquet_read_failed = self.scan_parquet_keys()
            self.stats["parquet_read_failed"] = parquet_read_failed
        except Exception as e:
            logger.error(f"Error fetching data: {e}")
            return {"status": "error", "error": str(e)}

        # Detect discrepancies
        discrepancies = self.detect_discrepancies(jira_keys, json_keys, parquet_keys)

        missing_in_json = discrepancies["missing_in_json"]
        missing_in_parquet = discrepancies["missing_in_parquet"]

        # If the Parquet scan never ran, every issue trivially "missing in
        # Parquet" is an artifact of not having looked, not a measurement.
        # Drop it rather than report it: left in, it drives a corpus-sized
        # number into `missing_in_parquet`, logs "exceeds threshold, manual
        # review required" about a gap nobody observed, and scores the alert
        # on it. The run still fails loudly — `parquet_read_failed` carries
        # the sentinel and forces ERROR/partial_success below — but it now
        # says "the Parquet scan could not run", which is true and points at
        # the actual fix (install DuckDB), instead of naming N issues that
        # may be perfectly fine (Devin review).
        if self.PARQUET_SCAN_UNAVAILABLE in self.stats["parquet_read_failed"]:
            missing_in_parquet = []
            discrepancies["missing_in_parquet"] = []
            self.stats["missing_in_parquet"] = []

        # Determine if auto-fix should run
        should_fix = (
            auto_fix and not dry_run and len(missing_in_json) > 0 and len(missing_in_json) <= self.AUTO_FIX_THRESHOLD
        )

        # Apply fixes if needed
        if should_fix:
            logger.info(f"Auto-fixing {len(missing_in_json)} missing issues")

            # Backfill missing JSON files
            backfilled, backfill_failed = self.backfill_issues(missing_in_json)
            self.stats["auto_backfilled"] = backfilled
            self.stats["backfill_failed"] = backfill_failed

            # Transform to Parquet (for issues that were backfilled successfully)
            if backfilled:
                transformed, transform_failed = self.transform_issues(backfilled)
                self.stats["transformed"].extend(transformed)
                self.stats["transform_failed"] = transform_failed

        elif len(missing_in_json) > self.AUTO_FIX_THRESHOLD:
            logger.error(
                f"Found {len(missing_in_json)} missing issues - exceeds threshold "
                f"({self.AUTO_FIX_THRESHOLD}), manual review required"
            )

        # Fix Parquet transform lag (always safe to re-transform) — but only up
        # to the same threshold as missing_in_json. A lag in the thousands is
        # not a lag, it's a broken read (see scan_parquet_keys): shelling out
        # once per key for that many issues is what turned one corrupt Parquet
        # file into an unbounded re-transform loop running every 30 minutes,
        # forever.
        if auto_fix and not dry_run and missing_in_parquet:
            if len(missing_in_parquet) <= self.AUTO_FIX_THRESHOLD:
                logger.info(f"Transforming {len(missing_in_parquet)} issues with Parquet lag")
                transformed, transform_failed = self.transform_issues(missing_in_parquet)
                self.stats["transformed"].extend(transformed)
                self.stats["transform_failed"].extend(transform_failed)
            else:
                logger.error(
                    f"Found {len(missing_in_parquet)} issues missing in Parquet - exceeds threshold "
                    f"({self.AUTO_FIX_THRESHOLD}), manual review required"
                )

        # One coalesced rebuild for whatever the fixes just wrote. `transform_issues`
        # shells out to `connectors.jira.incremental_transform`, which writes parquet
        # and nothing else — the `_meta` refresh, and the `CREATE OR REPLACE VIEW`
        # inside it, live in the `jira-refresh` job now. Nothing on this path used to
        # announce anything, which was invisible while the transform refreshed `_meta`
        # inline. Without it, a run where this checker is the ONLY writer leaves the
        # catalog's row/size numbers stale until some unrelated webhook fires — and
        # "the only writer" is precisely a webhook outage, i.e. when this checker
        # matters most.
        # Gate on what a fix actually WROTE, not on the gap merely existing. The
        # threshold above means a large Parquet gap transforms nothing, and the
        # old `auto_fix and missing_in_parquet` condition still fired on it — so
        # an unreadable or badly lagging partition queued a no-op `jira-refresh`
        # (plus a `jira-refresh-followup` when one was already running) every 30
        # minutes, forever. That is the same silent churn this change set out to
        # stop, in a new place (Devin review).
        if not dry_run and (self.stats["auto_backfilled"] or self.stats["transformed"]):
            self._enqueue_jira_refresh()

        # Calculate final stats
        duration = time.time() - start_time

        # Score the larger of the two gaps. This used to score missing_in_json
        # only, so a Parquet-side gap — including one inflated to corpus size
        # by an unreadable Parquet file — never moved this needle.
        #
        # The Parquet side is scored on what this run did NOT repair, not on the
        # gap as first observed: a routine lag that the checker fully transforms
        # is a healthy self-healing run and must stay INFO, or every such run
        # becomes noise for anything alerting on `alert_level != INFO` (Devin
        # review). `missing_in_json` keeps its original pre-fix scoring — that
        # semantic predates this change and is not ours to redefine here.
        # Set difference, not a subtraction of counts: `transformed` accumulates
        # from BOTH fix branches (backfilled JSON keys as well as Parquet-lag
        # keys), and those sets need not be subsets of `missing_in_parquet` — a
        # count-based subtraction could go negative and mask a real gap.
        unrepaired_in_parquet = len(set(missing_in_parquet) - set(self.stats["transformed"]))
        alert_level = self.get_alert_level(max(len(missing_in_json), unrepaired_in_parquet))

        # A read failure or a failed fix attempt is a broken pipeline, not a
        # size-scored gap — it must never hide behind a small missing-issue
        # count just because nothing (successfully) got fixed.
        if self.stats["parquet_read_failed"]:
            alert_level = "ERROR"
        elif self.stats["transform_failed"] and alert_level == "INFO":
            alert_level = "WARNING"

        status = "success"
        if self.stats["backfill_failed"] or self.stats["transform_failed"] or self.stats["parquet_read_failed"]:
            status = "partial_success"

        # Build report
        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "check_type": "incremental" if max_age_days <= 90 else "deep",
            "max_age_days": max_age_days,
            "duration_seconds": round(duration, 2),
            "counts": {
                "jira_api": self.stats["jira_api_count"],
                "raw_json": self.stats["raw_json_count"],
                "parquet": self.stats["parquet_count"],
            },
            "discrepancies": {
                "missing_in_json": missing_in_json,
                "missing_in_parquet": missing_in_parquet,
                "deleted_in_jira": self.stats["deleted_in_jira"],
                "parquet_read_failed": self.stats["parquet_read_failed"],
            },
            "actions": {
                "auto_backfilled": self.stats["auto_backfilled"],
                "backfill_failed": self.stats["backfill_failed"],
                "transform_failed": self.stats["transform_failed"],
            },
            "status": status,
            "alert_level": alert_level,
        }

        # Save report to file
        report_path = self.config.raw_dir / "_consistency_report.json"
        try:
            # Atomic write using temp file
            fd, temp_path = tempfile.mkstemp(
                dir=report_path.parent,
                prefix=".consistency_report_",
                suffix=".json.tmp",
            )
            os.fchmod(fd, 0o660)  # Restore group rw so deploy can access via ACL
            with os.fdopen(fd, "w") as f:
                json.dump(report, f, indent=2)
            os.replace(temp_path, report_path)
            logger.info(f"Report saved to {report_path}")
        except Exception as e:
            logger.error(f"Failed to save report: {e}")

        # Log summary
        logger.info("=" * 60)
        logger.info("Consistency check completed")
        logger.info(f"Status: {status}")
        logger.info(f"Alert level: {alert_level}")
        logger.info(f"Duration: {duration:.1f}s")
        if missing_in_json:
            logger.info(f"Missing in JSON: {len(missing_in_json)} - {', '.join(missing_in_json[:10])}")
        if self.stats["auto_backfilled"]:
            logger.info(f"Auto-backfilled: {len(self.stats['auto_backfilled'])}")
        if self.stats["backfill_failed"]:
            logger.error(f"Backfill failed: {len(self.stats['backfill_failed'])}")
        if self.stats["parquet_read_failed"]:
            logger.error(f"Parquet read failed: {len(self.stats['parquet_read_failed'])} file(s)")
        logger.info("=" * 60)

        return report


def main():
    parser = argparse.ArgumentParser(
        description="Jira data consistency monitoring",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=30,
        help="Check issues created in last N days (default: 30)",
    )
    parser.add_argument(
        "--auto-fix",
        action="store_true",
        default=True,
        help="Enable automatic backfill for small gaps (default: True)",
    )
    parser.add_argument(
        "--no-auto-fix",
        action="store_false",
        dest="auto_fix",
        help="Disable automatic backfill (only report discrepancies)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only check, don't fix anything",
    )

    args = parser.parse_args()

    try:
        config = Config.from_env()
        checker = JiraConsistencyChecker(config)
        report = checker.run_check(
            max_age_days=args.max_age_days,
            auto_fix=args.auto_fix,
            dry_run=args.dry_run,
        )

        # Exit with error code if there are unfixed issues
        if report.get("alert_level") == "ERROR":
            sys.exit(1)
        elif report.get("status") != "success":
            sys.exit(1)

    except Exception as e:
        logger.error(f"Consistency check failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
