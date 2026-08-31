#!/usr/bin/env python3
"""05 — the extraction seam (SharePoint corpus-extraction job).

Exercises the one piece of the pipeline this repo can trigger directly:
``POST /api/admin/sharepoint/connections/{id}/extract`` (TCRD-226,
``app/api/admin_sharepoint.py::trigger_extraction``), which enqueues the
``corpus-extraction`` job kind and hands the extraction producer this
instance's anonymize-scope map + HMAC key (docs/anonymization.md). Everything
upstream of "run the configured producer command" — the crawl, convert,
anonymize, extract stages themselves — lives outside this repo (spec §9,
§7.1) and is not exercised here; this script only proves Agnes's own seam:
does the job enqueue, does a worker pick it up, does it finish.

SKIPs cleanly (never fails) when:

  - ``extraction.enabled`` is off on the instance — nothing to trigger.
  - ``AGNES_E2E_SP_CONNECTION_ID`` is not set — nothing to trigger it FOR.
    Find one with ``GET /api/admin/source-connections`` (``source_type=
    "sharepoint"``) on the target instance.

Given both, triggers the extraction, then polls ``GET /api/jobs/{id}``
until it reaches a terminal state or ``AGNES_E2E_EXTRACT_TIMEOUT_SECONDS``
(default 120s) elapses. A real crawl can run far longer than that on a
sizeable corpus — a timeout here means "did not finish within this script's
patience", not "is broken"; re-run with a larger timeout or poll the job id
printed in the output manually.

An already-in-flight job for this connection (409 ``extraction_already_
running``) is treated as success-so-far: this script polls THAT job instead
of failing, which is what makes it safe to re-run without waiting for a
prior run to finish first.

Usage:
    AGNES_BASE_URL=... AGNES_ADMIN_TOKEN=... \\
    AGNES_E2E_SP_CONNECTION_ID=conn_... \\
    [AGNES_E2E_EXTRACT_TIMEOUT_SECONDS=300] \\
    python scripts/e2e/unstructured/05_extraction_seam.py
"""

from __future__ import annotations

import sys
import time

import httpx

from _common import Config, ConfigError, Report, client_for, fail_and_exit, short

_POLL_INTERVAL_SECONDS = 3.0
_TERMINAL_STATUSES = {"done", "failed"}


def _extraction_enabled(client: httpx.Client) -> tuple[bool, str]:
    resp = client.get("/api/admin/server-config")
    if resp.status_code != 200:
        return False, f"could not read GET /api/admin/server-config -> {resp.status_code}"
    flags = {f.get("name"): f for f in resp.json().get("feature_flags", [])}
    flag = flags.get("extraction")
    if flag is None:
        return False, "'extraction' flag missing from the feature_flags inventory (schema drift?)"
    return bool(flag.get("effective")), f"effective={flag.get('effective')!r} source={flag.get('source')!r}"


def _trigger(client: httpx.Client, connection_id: str) -> tuple[int, dict]:
    resp = client.post(f"/api/admin/sharepoint/connections/{connection_id}/extract")
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    return resp.status_code, body


def _poll_job(client: httpx.Client, job_id: str, timeout_seconds: int) -> tuple[str, dict]:
    deadline = time.monotonic() + timeout_seconds
    last: dict = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/api/jobs/{job_id}")
        if resp.status_code != 200:
            return "poll_error", {"status_code": resp.status_code, "body": resp.text}
        last = resp.json().get("job", {})
        if last.get("status") in _TERMINAL_STATUSES:
            return last["status"], last
        time.sleep(_POLL_INTERVAL_SECONDS)
    return "timeout", last


def main() -> int:
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        fail_and_exit(str(exc))
        return 1

    report = Report("05 — extraction seam")

    with client_for(cfg, token=cfg.admin_token) as client:
        enabled, detail = _extraction_enabled(client)
        if not enabled:
            report.skip("trigger + poll corpus-extraction", f"extraction.enabled is off ({detail})")
            report.print_table()
            return report.exit_code

        if not cfg.sp_connection_id:
            report.skip(
                "trigger + poll corpus-extraction",
                "extraction.enabled is ON, but AGNES_E2E_SP_CONNECTION_ID is not set. "
                "Find a connection id with GET /api/admin/source-connections (source_type=sharepoint).",
            )
            report.print_table()
            return report.exit_code

        status_code, body = _trigger(client, cfg.sp_connection_id)

        job_id: str | None = None
        if status_code == 202:
            job_id = body.get("job_id")
            report.record(
                "trigger corpus-extraction",
                job_id is not None,
                f"-> 202 job_id={job_id}",
            )
        elif status_code == 409 and body.get("error") == "extraction_already_running":
            job_id = body.get("job_id")
            report.record(
                "trigger corpus-extraction",
                job_id is not None,
                f"already running -> 409, polling existing job_id={job_id} instead",
            )
        else:
            report.record(
                "trigger corpus-extraction",
                False,
                f"-> {status_code} {short(body)}",
            )
            report.print_table()
            return report.exit_code

        if job_id is None:
            report.print_table()
            return report.exit_code

        final_status, job = _poll_job(client, job_id, cfg.extract_timeout_seconds)
        if final_status == "done":
            report.record(
                "corpus-extraction job completes",
                True,
                f"job_id={job_id} status=done payload={short(job.get('payload'))}",
            )
        elif final_status == "failed":
            report.record(
                "corpus-extraction job completes",
                False,
                f"job_id={job_id} status=failed error={short(job.get('error') or job)}",
            )
        elif final_status == "timeout":
            report.record(
                "corpus-extraction job completes",
                False,
                f"job_id={job_id} did not reach a terminal state within "
                f"{cfg.extract_timeout_seconds}s (last seen status={job.get('status')!r}) — "
                "a real crawl can run much longer than this script's patience; "
                f"check it manually: GET /api/jobs/{job_id}",
            )
        else:
            report.record("corpus-extraction job completes", False, f"job_id={job_id} poll error: {short(job)}")

    report.print_table()
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
