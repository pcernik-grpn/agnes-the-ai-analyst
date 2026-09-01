#!/usr/bin/env python3
"""01 — instance smoke check (read-only).

Proves the basics every other script in this pack depends on: the instance
is reachable and healthy, ``AGNES_ADMIN_TOKEN`` actually authenticates as an
admin (and an absent/garbage token is actually refused — auth that always
says yes is worse than no auth), and reports the live state of the two
feature flags the rest of the pack cares about: ``facts`` (gates the whole
``/api/facts*`` surface, see ``app/auth/access.py::require_facts_enabled``)
and ``extraction`` (gates the SharePoint ``corpus-extraction`` job, see
``app/api/admin_sharepoint.py``).

Nothing here mutates instance state. Safe to run at any time, including
against a production-shaped instance, though this pack as a whole is meant
for a dev/staging box (02-04 create and delete throwaway collections and
source connections).

Usage:
    AGNES_BASE_URL=https://dev.example.internal \\
    AGNES_ADMIN_TOKEN=... \\
    python scripts/e2e/unstructured/01_smoke_instance.py
"""

from __future__ import annotations

import sys

import httpx

from _common import Config, ConfigError, Report, client_for, fail_and_exit, short


def _check_health(report: Report, cfg: Config) -> None:
    try:
        with client_for(cfg) as client:
            resp = client.get("/api/health")
    except httpx.HTTPError as exc:
        report.record("instance reachable", False, f"{cfg.base_url}/api/health: {short(exc)}")
        report.record("db schema healthy", False, "skipped — instance unreachable")
        return

    report.record(
        "instance reachable",
        resp.status_code == 200,
        f"GET /api/health -> {resp.status_code}",
    )
    if resp.status_code != 200:
        report.record("db schema healthy", False, "skipped — /api/health did not return 200")
        return

    body = resp.json()
    status = body.get("status")
    db_schema = body.get("db_schema")
    report.record(
        "db schema healthy",
        status == "ok",
        f"status={status!r} db_schema={db_schema!r} vault_key_configured={body.get('vault_key_configured')!r}",
    )


def _check_auth(report: Report, cfg: Config) -> tuple[dict | None, bool]:
    """Returns (server_config_body, admin_ok) — the body is reused by the
    feature-flag checks below so they don't cost a second round trip."""
    with client_for(cfg) as anon_client:
        try:
            anon_resp = anon_client.get("/api/admin/server-config")
        except httpx.HTTPError as exc:
            report.record("anonymous access is refused", False, f"transport error: {short(exc)}")
            anon_resp = None
    if anon_resp is not None:
        report.record(
            "anonymous access is refused",
            anon_resp.status_code == 401,
            f"GET /api/admin/server-config (no token) -> {anon_resp.status_code} (want 401)",
        )

    with client_for(cfg, token="not-a-real-token-e2e-smoke") as bogus_client:
        try:
            bogus_resp = bogus_client.get("/api/admin/server-config")
        except httpx.HTTPError as exc:
            report.record("bogus token is refused", False, f"transport error: {short(exc)}")
            bogus_resp = None
    if bogus_resp is not None:
        report.record(
            "bogus token is refused",
            bogus_resp.status_code == 401,
            f"GET /api/admin/server-config (garbage bearer) -> {bogus_resp.status_code} (want 401)",
        )

    with client_for(cfg, token=cfg.admin_token) as admin_client:
        try:
            admin_resp = admin_client.get("/api/admin/server-config")
        except httpx.HTTPError as exc:
            report.record("AGNES_ADMIN_TOKEN authenticates as admin", False, f"transport error: {short(exc)}")
            return None, False

    admin_ok = report.record(
        "AGNES_ADMIN_TOKEN authenticates as admin",
        admin_resp.status_code == 200,
        f"GET /api/admin/server-config -> {admin_resp.status_code} (want 200)",
    )
    return (admin_resp.json() if admin_ok else None), admin_ok


def _check_persona_tokens(report: Report, cfg: Config) -> None:
    for label, token in (("A", cfg.token_a), ("B", cfg.token_b)):
        name = f"persona {label} token authenticates"
        if not token:
            report.skip(name, f"AGNES_E2E_TOKEN_{label} not set — probes 03/04 that need it will also skip")
            continue
        with client_for(cfg, token=token) as client:
            try:
                resp = client.get("/api/collections")
            except httpx.HTTPError as exc:
                report.record(name, False, f"transport error: {short(exc)}")
                continue
        report.record(name, resp.status_code == 200, f"GET /api/collections -> {resp.status_code} (want 200)")


def _check_feature_flag(report: Report, server_config: dict | None, flag_name: str, label: str) -> None:
    check_name = f"{label} feature flag state"
    if server_config is None:
        report.record(check_name, False, "skipped — could not read GET /api/admin/server-config")
        return
    flags = {f.get("name"): f for f in server_config.get("feature_flags", [])}
    flag = flags.get(flag_name)
    if flag is None:
        report.record(check_name, False, f"'{flag_name}' is not in the feature_flags inventory (schema drift?)")
        return
    report.record(
        check_name,
        True,
        f"effective={flag.get('effective')!r} source={flag.get('source')!r} default={flag.get('default')!r}",
    )


def main() -> int:
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        fail_and_exit(str(exc))
        return 1  # unreachable — fail_and_exit exits — kept for type-checkers

    report = Report("01 — instance smoke")
    _check_health(report, cfg)
    server_config, _admin_ok = _check_auth(report, cfg)
    _check_persona_tokens(report, cfg)
    _check_feature_flag(report, server_config, "facts", "facts")
    _check_feature_flag(report, server_config, "extraction", "extraction")

    report.print_table()
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
