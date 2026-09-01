#!/usr/bin/env python3
"""04 — anonymization probe (AN1-style).

Spec §15.4 AN1: "a unique planted name appears nowhere in Agnes: facts,
claim attrs, quotes, corpus_chunks.text, /raw, /preview, search, audit
log. Passes by construction once §9's order holds — so a failure means the
pipeline order broke, which is what the test is for."

**What this DOES verify (Agnes-side surfaces):** given a planted string that
should never appear in this instance because it was supposed to be redacted
before it ever reached Agnes, this script searches every reader surface
Agnes exposes and asserts the literal string is absent from each response
body:

  - ``POST /api/facts/search`` (unfiltered — ``q`` matches every alias)
  - a bounded sample of ``GET /api/facts/{id}/claims`` across facts search
    for each type in ``type-map`` (quotes + attrs — the string could hide
    in either)
  - ``GET /api/collections/search`` (hybrid lexical/semantic search)
  - optionally, ``GET /api/collections/{id}/files/{file_id}/preview`` and
    ``.../raw`` for every file in one named collection
      (``AGNES_E2E_ANON_COLLECTION_ID``) — the collection backing the
      anonymize-marked scope the planted string is supposed to be absent
      from.

**What this does NOT verify:** whether the producer's anonymizer actually
ran, or ran correctly, on the source content — that pipeline stage
(``source -> crawl -> convert -> anonymize -> Agnes``, docs/anonymization.md)
is entirely external to this repo (see design spec §9). A PASS here means
"the planted string is not visible anywhere Agnes can be asked to show it",
which is the guarantee docs/anonymization.md's "Current limits" section
describes: Agnes cannot independently verify content was anonymized, so a
FAIL here is a real, actionable finding (a leak reached Agnes's own
surfaces), but a PASS is not proof the upstream pipeline is correct — only
that Agnes did not make an already-anonymized batch un-anonymized again.

Read-only. Uses the admin token throughout (broadest visibility — the point
is to catch a leak ANYWHERE the instance can show it, not to probe RBAC;
that is script 03's job).

Usage:
    AGNES_BASE_URL=... AGNES_ADMIN_TOKEN=... \\
    AGNES_E2E_PLANTED="Some Never-Should-Appear Name" \\
    [AGNES_E2E_ANON_COLLECTION_ID=col_...] \\
    python scripts/e2e/unstructured/04_anonymization_probe.py
"""

from __future__ import annotations

import sys

import httpx

from _common import Config, ConfigError, Report, client_for, fail_and_exit, short


def _contains(haystack: str, needle: str) -> bool:
    return needle.lower() in haystack.lower()


def _check_facts_search(report: Report, client: httpx.Client, planted: str) -> None:
    resp = client.post("/api/facts/search", json={"q": planted, "limit": 100})
    if resp.status_code == 200:
        leaked = bool(resp.json().get("subjects"))
        report.record(
            "facts search: no alias matches the planted string",
            not leaked,
            f"-> {resp.status_code} subjects={len(resp.json().get('subjects', []))}",
        )
    else:
        report.record(
            "facts search: no alias matches the planted string", False, f"-> {resp.status_code} {short(resp.text)}"
        )


def _check_claims_sample(report: Report, client: httpx.Client, planted: str, sample_limit: int) -> None:
    type_map_resp = client.get("/api/facts/type-map")
    if type_map_resp.status_code != 200:
        report.record(
            "facts claims (bounded sample): no leak in quotes/attrs",
            False,
            f"could not read type-map -> {type_map_resp.status_code}",
        )
        return

    types = [t["type"] for t in type_map_resp.json().get("types", [])]
    subject_ids: list[str] = []
    for t in types:
        if len(subject_ids) >= sample_limit:
            break
        search_resp = client.post("/api/facts/search", json={"type": t, "limit": min(100, sample_limit)})
        if search_resp.status_code == 200:
            subject_ids.extend(s["id"] for s in search_resp.json().get("subjects", []))
    subject_ids = subject_ids[:sample_limit]

    leaked_in: list[str] = []
    checked = 0
    for sid in subject_ids:
        claims_resp = client.get(f"/api/facts/{sid}/claims")
        if claims_resp.status_code != 200:
            continue
        checked += 1
        if _contains(claims_resp.text, planted):
            leaked_in.append(sid)

    report.record(
        f"facts claims (bounded sample of {checked}/{len(subject_ids)}): no leak in quotes/attrs",
        not leaked_in,
        f"leaked_in={short(leaked_in)}"
        if leaked_in
        else f"sampled {checked} subjects across {len(types)} types (cap={sample_limit}) — not exhaustive",
    )


def _check_collections_search(report: Report, client: httpx.Client, planted: str) -> None:
    resp = client.get("/api/collections/search", params={"q": planted, "k": 50})
    if resp.status_code != 200:
        report.record("collections search: no leak in results", False, f"-> {resp.status_code} {short(resp.text)}")
        return
    leaked = _contains(resp.text, planted)
    report.record(
        "collections search: no leak in results",
        not leaked,
        f"-> {resp.status_code} results={len(resp.json().get('results', []))}"
        + (" (planted string literally present in the response body)" if leaked else ""),
    )


def _check_document_surfaces(report: Report, client: httpx.Client, planted: str, collection_id: str) -> None:
    files_resp = client.get(f"/api/collections/{collection_id}/files")
    if files_resp.status_code != 200:
        report.record(
            "document surfaces: could not list files in AGNES_E2E_ANON_COLLECTION_ID",
            False,
            f"-> {files_resp.status_code} {short(files_resp.text)}",
        )
        return
    rows = files_resp.json()
    rows = rows.get("files", rows) if isinstance(rows, dict) else rows

    leaked_preview: list[str] = []
    leaked_raw: list[str] = []
    checked = 0
    for row in rows:
        file_id = row.get("file_id") or row.get("id")
        if not file_id:
            continue
        checked += 1
        preview_resp = client.get(f"/api/collections/{collection_id}/files/{file_id}/preview")
        if preview_resp.status_code == 200 and _contains(preview_resp.text, planted):
            leaked_preview.append(file_id)
        if preview_resp.status_code == 200 and preview_resp.json().get("raw_url"):
            raw_resp = client.get(f"/api/collections/{collection_id}/files/{file_id}/raw")
            if raw_resp.status_code == 200 and planted.encode("utf-8", errors="ignore") in raw_resp.content:
                leaked_raw.append(file_id)

    report.record(
        f"document preview ({checked} files): no leak",
        not leaked_preview,
        f"leaked_in={short(leaked_preview)}" if leaked_preview else f"checked {checked} files",
    )
    report.record(
        f"document raw ({checked} files): no leak",
        not leaked_raw,
        f"leaked_in={short(leaked_raw)}" if leaked_raw else f"checked {checked} inline-media files",
    )


def main() -> int:
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        fail_and_exit(str(exc))
        return 1

    report = Report("04 — anonymization probe (AN1-style)")
    report.note(
        "Content anonymization itself happens upstream, in the producer pipeline "
        "(source -> crawl -> convert -> anonymize -> Agnes; see docs/anonymization.md). "
        "This script verifies Agnes-SIDE surfaces only: that a supplied planted string "
        "which should already be gone before ingestion is not re-exposed by anything "
        "Agnes itself serves. It cannot verify the producer's anonymizer ran at all."
    )

    if not cfg.planted:
        report.skip(
            "all checks",
            "AGNES_E2E_PLANTED not set — nothing to search for. "
            "Set it to a string known to have existed in the pre-anonymization source content.",
        )
        report.print_table()
        return report.exit_code

    with client_for(cfg, token=cfg.admin_token) as client:
        facets_probe = client.get("/api/facts/facets")
        facts_enabled = facets_probe.status_code != 404
        if facts_enabled:
            _check_facts_search(report, client, cfg.planted)
            _check_claims_sample(report, client, cfg.planted, cfg.anon_sample_limit)
        else:
            report.skip("facts search: no alias matches the planted string", "facts.enabled is off on this instance")
            report.skip(
                "facts claims (bounded sample): no leak in quotes/attrs", "facts.enabled is off on this instance"
            )

        _check_collections_search(report, client, cfg.planted)

        if cfg.anon_collection_id:
            _check_document_surfaces(report, client, cfg.planted, cfg.anon_collection_id)
        else:
            report.skip(
                "document preview/raw surfaces",
                "AGNES_E2E_ANON_COLLECTION_ID not set — cannot walk a specific collection's files. "
                "Set it to the collection id backing the anonymize-marked scope.",
            )

    report.print_table()
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
