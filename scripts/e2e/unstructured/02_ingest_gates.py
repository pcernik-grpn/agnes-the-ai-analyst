#!/usr/bin/env python3
"""02 — ingest gates: audience format + anonymize-fail-closed.

Creates one throwaway collection and one throwaway SharePoint connection
(never a real Graph credential — the gates below never call out to
SharePoint), then proves the three ``POST /api/facts/ingest`` enforcement
gates documented in ``app/api/facts.py`` and ``docs/anonymization.md``:

  (a) an evidence item with a malformed ``audience`` tag is refused whole-
      batch, 422 ``invalid_audience`` (spec §4.2, Task 10) — BEFORE either
      DB lookup below ever runs.
  (b) a batch documenting a corpus whose connect-wizard scope is
      ``anonymize=true`` is refused, 403 ``anonymization_not_declared``,
      when the batch's own ``anonymization`` block omits that corpus (spec
      §9.2 hardening).
  (c) the SAME batch, with the declaration added, clears the gate — proven
      by the response NOT being the 403 from (b). What happens after the
      gate (whether the ingest itself fully succeeds) depends on the
      instance's backend and is reported for visibility, but is not what
      this script is checking — see the note printed at (c).

Mirrors the HTTP-level assertions in ``tests/test_api_facts_ingest.py``
(``test_ingest_refuses_anonymize_marked_corpus_without_declaration`` /
``test_ingest_accepts_when_declaration_covers_the_corpus``) and the format
gate in ``app/api/facts.py::_validate_evidence_audience`` — run here over a
live instance's actual HTTP surface instead of an in-process TestClient.

"Corpus rows" in this script's scope means the ``documents[]`` entries on
the ingest wire itself (spec §7.0's ``make_row`` shape) — it does not upload
real file content or exercise the verbatim gate; that full write path (real
file upload -> chunking -> content-grounded claim) is covered by probe 03,
which needs it for the visibility assertions anyway.

Idempotent: every resource this script creates is named with a fresh random
suffix and deleted in a ``finally`` block, so a failed run never blocks the
next one and re-running never collides with a prior run's leftovers.

Usage:
    AGNES_BASE_URL=... AGNES_ADMIN_TOKEN=... \\
    python scripts/e2e/unstructured/02_ingest_gates.py
"""

from __future__ import annotations

import sys

import httpx

from _common import Config, ConfigError, Report, client_for, fail_and_exit, short, unique_suffix


def _create_collection(client: httpx.Client, name: str) -> str:
    resp = client.post("/api/collections", json={"name": name})
    resp.raise_for_status()
    return resp.json()["id"]


def _create_sharepoint_connection(client: httpx.Client, *, name: str, corpus_id: str) -> str:
    resp = client.post(
        "/api/admin/source-connections",
        json={
            "name": name,
            "source_type": "sharepoint",
            "config": {
                "tenant_id": "e2e-tenant",
                "client_id": "e2e-client",
                "scopes": [
                    {
                        "source_scope_id": f"e2e-scope-{unique_suffix()}",
                        "display_path": "E2E Ingest Gates",
                        "anonymize": True,
                        "collection_id": corpus_id,
                    }
                ],
            },
        },
    )
    resp.raise_for_status()
    return resp.json()["id"]


def _cleanup(admin_client: httpx.Client, *, connection_id: str | None, collection_id: str | None) -> None:
    if connection_id:
        try:
            admin_client.delete(f"/api/admin/source-connections/{connection_id}")
        except httpx.HTTPError:
            pass
    if collection_id:
        try:
            admin_client.delete(f"/api/collections/{collection_id}")
        except httpx.HTTPError:
            pass


def main() -> int:
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        fail_and_exit(str(exc))
        return 1

    report = Report("02 — ingest gates")
    suffix = unique_suffix()
    connection_id: str | None = None
    collection_id: str | None = None

    with client_for(cfg, token=cfg.admin_token) as admin_client:
        # ------------------------------------------------------------------
        # Setup: does the facts flag even let us in? A 404 here means the
        # whole /api/facts* router is off — nothing below is testable.
        # ------------------------------------------------------------------
        probe = admin_client.post("/api/facts/ingest", json={})
        if probe.status_code == 404:
            report.skip(
                "gate (a) invalid audience -> 422",
                "facts.enabled is off on this instance (POST /api/facts/ingest -> 404) — see 01's flag check",
            )
            report.skip("gate (b) undeclared anonymize-marked corpus -> 403", "facts.enabled is off")
            report.skip("gate (c) declared anonymize-marked corpus clears the gate", "facts.enabled is off")
            report.print_table()
            return report.exit_code

        try:
            collection_id = _create_collection(admin_client, f"e2e-ingest-gates-{suffix}")
            connection_id = _create_sharepoint_connection(
                admin_client, name=f"e2e-ingest-gates-sp-{suffix}", corpus_id=collection_id
            )

            # -- (a) invalid audience format --------------------------------
            resp_a = admin_client.post(
                "/api/facts/ingest",
                json={
                    "nodes": [
                        {
                            "id": f"organization:e2e-audience-check-{suffix}",
                            "type": "organization",
                            "attrs": {},
                            "evidence": [
                                {
                                    "doc_id": "e2e-audience-doc",
                                    "quote": "irrelevant — the format gate runs before doc resolution",
                                    "audience": "Not A Valid Tag!!",
                                }
                            ],
                        }
                    ]
                },
            )
            detail_a = (
                resp_a.json().get("detail")
                if resp_a.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            report.record(
                "gate (a) invalid audience -> 422",
                resp_a.status_code == 422
                and isinstance(detail_a, dict)
                and detail_a.get("reason") == "invalid_audience",
                f"-> {resp_a.status_code} detail={short(detail_a)}",
            )

            # -- (b) anonymize-marked corpus, no declaration -----------------
            doc_id_b = f"e2e-doc-{suffix}-b"
            resp_b = admin_client.post(
                "/api/facts/ingest",
                json={"documents": [{"doc_id": doc_id_b, "corpus_id": collection_id, "path": f"e2e/{suffix}-b.md"}]},
            )
            detail_b = resp_b.json().get("detail") if resp_b.status_code == 403 else {}
            gate_b_ok = (
                resp_b.status_code == 403
                and isinstance(detail_b, dict)
                and detail_b.get("reason") == "anonymization_not_declared"
                and collection_id in (detail_b.get("corpus_ids") or [])
            )
            report.record(
                "gate (b) undeclared anonymize-marked corpus -> 403",
                gate_b_ok,
                f"-> {resp_b.status_code} detail={short(detail_b or resp_b.text)}",
            )

            # -- (c) same shape, WITH the declaration ------------------------
            doc_id_c = f"e2e-doc-{suffix}-c"
            resp_c = admin_client.post(
                "/api/facts/ingest",
                json={
                    "documents": [{"doc_id": doc_id_c, "corpus_id": collection_id, "path": f"e2e/{suffix}-c.md"}],
                    "anonymization": {"declared": True, "scopes": {collection_id: {"docs_anonymized": 1}}},
                },
            )
            wrongly_blocked = resp_c.status_code == 403 and (
                isinstance(resp_c.json().get("detail"), dict)
                and resp_c.json()["detail"].get("reason") == "anonymization_not_declared"
            )
            report.record(
                "gate (c) declared anonymize-marked corpus clears the gate",
                not wrongly_blocked,
                f"-> {resp_c.status_code} body={short(resp_c.text)} "
                "(any status other than the (b) 403 proves the gate let it through; "
                "what happens next depends on the backend/full write path, not checked here)",
            )
        finally:
            _cleanup(admin_client, connection_id=connection_id, collection_id=collection_id)

    report.print_table()
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
