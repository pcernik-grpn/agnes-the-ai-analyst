#!/usr/bin/env python3
"""03 — visibility probes (S-probe style, over HTTP).

Reuses the shapes of ``tests/db_pg/test_facts_read_pg.py`` (S1/S2/S3/S6) and
the audience-tag machinery in ``tests/db_pg/test_facts_audience.py`` —
proven at the repository layer there, proven here end-to-end against a live
instance's actual REST surface with two real personas' bearer tokens.

Two tiers:

1. A **baseline** 404-parity check that needs only the two persona tokens:
   a nonexistent subject id 404s identically for both personas (no existence
   oracle) — this always runs when ``AGNES_E2E_TOKEN_A``/``_B`` are set.

2. **Full plant-based probes** — additionally needs ``AGNES_E2E_GROUP_A``,
   the id of an Agnes group persona A belongs to and persona B does NOT
   (same "fixture uploader is never the probed caller" discipline the pytest
   suite enforces — see the module docstring of
   ``tests/db_pg/test_facts_read_pg.py``). This script plants, with the
   admin token:

   - two collections: ``col_a`` (granted to that group — A can read it, B
     cannot) and ``col_b`` (granted to nobody);
   - one "engagement" fact with THREE evidence items on one node: a public
     claim in ``col_a`` (no attrs), a claim in ``col_b`` carrying
     ``attrs.price`` (S2 — the attribute oracle), and a claim in ``col_a``
     tagged ``evidence[].audience="e2e-tier"`` — col_a is registered as a
     "tiered" collection (spec §4.1-4.3) with that class mapped to the same
     group, so this evidence readable-by-collection-grant is ALSO
     audience-gated;
   - two "person" facts, both with their own readable claim in ``col_a``,
     joined by a ``knows`` edge whose ONLY claim lives in ``col_b`` (S3 — an
     edge's visibility is never inferred from its endpoints').

   Then asserts, calling the REST API as A and B (never as admin — admin
   god-mode would make every one of these pass vacuously):

   - B's ``search`` for the planted engagement returns nothing (S1: an
     ungranted collection contributes nothing, in any form);
   - B's ``claims()`` on the real (but ungranted) engagement subject 404s,
     identically to a genuinely nonexistent id (S6, using a REAL secret
     subject rather than only a synthetic one);
   - A sees the engagement, but its projected ``attrs`` never carries
     ``price``, and filtering ``search`` by ``price`` returns no match even
     though A IS granted a claim on this very subject (S2);
   - A's ``claims()`` response for the engagement never contains the string
     ``"price"`` anywhere (attrs OR quotes);
   - A's ``neighbors()`` from person-A never returns the ``knows`` edge or
     person-B as a node (S3).

   What this does NOT prove: per-tier separation among two callers who BOTH
   hold the ``col_a`` grant but differ only in audience class — that needs a
   third persona/group this pack's env vars don't provide, so it is out of
   scope here (see ``docs/superpowers/specs/2026-08-27-fact-graph-over-
   collections-design.md`` §15.6's "deliberately untested" convention). What
   IS proven: the audience-tagged evidence field round-trips through ingest
   and is never exposed to a caller (B) who holds no grant at all.

Idempotent: every planted id carries a fresh random suffix; cleanup runs in
a ``finally`` block regardless of outcome.

Usage:
    AGNES_BASE_URL=... AGNES_ADMIN_TOKEN=... \\
    AGNES_E2E_TOKEN_A=... AGNES_E2E_TOKEN_B=... AGNES_E2E_GROUP_A=... \\
    python scripts/e2e/unstructured/03_visibility_probes.py
"""

from __future__ import annotations

import sys
import time

import httpx

from _common import Config, ConfigError, Report, client_for, fail_and_exit, short, unique_suffix

_POLL_INTERVAL_SECONDS = 2.0
_INDEX_TIMEOUT_SECONDS = 60.0


def _upload_text_file(client: httpx.Client, collection_id: str, *, path: str, content: bytes) -> str:
    resp = client.post(
        f"/api/collections/{collection_id}/files",
        files={"files": (path.rsplit("/", 1)[-1], content, "text/markdown")},
        data={"paths": path},
    )
    resp.raise_for_status()
    return resp.json()[0]["file_id"]


def _list_files(client: httpx.Client, collection_id: str) -> list[dict]:
    """Tolerates either a bare list or a ``{"files": [...]}`` envelope —
    ``GET /api/collections/{id}/files``'s exact wrapper shape isn't this
    script's concern, only the ``file_id``/``processing_status`` fields on
    each row are."""
    resp = client.get(f"/api/collections/{collection_id}/files")
    resp.raise_for_status()
    body = resp.json()
    return body.get("files", []) if isinstance(body, dict) else body


def _wait_indexed(client: httpx.Client, collection_id: str, file_ids: list[str]) -> tuple[bool, str]:
    deadline = time.monotonic() + _INDEX_TIMEOUT_SECONDS
    remaining = set(file_ids)
    last_statuses: dict[str, str] = {}
    while time.monotonic() < deadline and remaining:
        by_id = {f["file_id"]: f for f in _list_files(client, collection_id)}
        for fid in list(remaining):
            row = by_id.get(fid)
            if row is None:
                continue
            last_statuses[fid] = row.get("processing_status", "")
            if row.get("processing_status") == "indexed":
                remaining.discard(fid)
            elif row.get("processing_status") == "rejected":
                return False, f"file {fid} rejected: {row.get('processing_detail')}"
        if remaining:
            time.sleep(_POLL_INTERVAL_SECONDS)
    if remaining:
        return False, f"timed out waiting for indexing; last statuses={last_statuses}"
    return True, "all files indexed"


def _create_collection(client: httpx.Client, name: str) -> str:
    resp = client.post("/api/collections", json={"name": name})
    resp.raise_for_status()
    return resp.json()["id"]


def _grant(client: httpx.Client, *, group_id: str, collection_id: str) -> None:
    resp = client.post(
        "/api/admin/grants",
        json={"group_id": group_id, "resource_type": "collection", "resource_id": collection_id},
    )
    if resp.status_code not in (201, 409):  # 409 = already granted, fine (idempotent re-run)
        resp.raise_for_status()


def _create_tiered_sharepoint_connection(client: httpx.Client, *, name: str, collection_id: str, group_id: str) -> str:
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
                        "display_path": "E2E Visibility Probe",
                        "anonymize": False,
                        "collection_id": collection_id,
                        "audience_classes": [{"name": "e2e-tier", "group_ids": [group_id]}],
                    }
                ],
            },
        },
    )
    resp.raise_for_status()
    return resp.json()["id"]


def _cleanup(admin_client: httpx.Client, *, connection_id: str | None, collection_ids: list[str]) -> None:
    if connection_id:
        try:
            admin_client.delete(f"/api/admin/source-connections/{connection_id}")
        except httpx.HTTPError:
            pass
    for cid in collection_ids:
        try:
            admin_client.delete(f"/api/collections/{cid}")
        except httpx.HTTPError:
            pass


def _baseline_404_parity(report: Report, cfg: Config) -> None:
    ghost_id = f"f_e2e_ghost_{unique_suffix()}"
    responses: dict[str, httpx.Response] = {}
    for label, token in (("A", cfg.token_a), ("B", cfg.token_b)):
        with client_for(cfg, token=token) as client:
            try:
                responses[label] = client.get(f"/api/facts/{ghost_id}/claims")
            except httpx.HTTPError as exc:
                report.record(
                    f"baseline: {label} 404s on a nonexistent subject", False, f"transport error: {short(exc)}"
                )
                return
    a_ok = responses["A"].status_code == 404
    b_ok = responses["B"].status_code == 404
    report.record(
        "baseline: A 404s on a nonexistent subject",
        a_ok,
        f"GET /api/facts/{ghost_id}/claims -> {responses['A'].status_code}",
    )
    report.record(
        "baseline: B 404s on a nonexistent subject",
        b_ok,
        f"GET /api/facts/{ghost_id}/claims -> {responses['B'].status_code}",
    )
    report.record(
        "baseline: A and B get an identical 404 shape",
        a_ok and b_ok and responses["A"].text == responses["B"].text,
        f"bodies equal={responses['A'].text == responses['B'].text!r}",
    )


def main() -> int:
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        fail_and_exit(str(exc))
        return 1

    report = Report("03 — visibility probes")

    if not cfg.token_a or not cfg.token_b:
        report.skip("all probes", "AGNES_E2E_TOKEN_A and AGNES_E2E_TOKEN_B are both required for this script")
        report.print_table()
        return report.exit_code

    with client_for(cfg, token=cfg.admin_token) as admin_client:
        probe = admin_client.get("/api/facts/facets")
        if probe.status_code == 404:
            report.skip("all probes", "facts.enabled is off on this instance (GET /api/facts/facets -> 404)")
            report.print_table()
            return report.exit_code

    _baseline_404_parity(report, cfg)

    if not cfg.group_a:
        report.skip(
            "full plant-based probes (S1/S2/S3)",
            "AGNES_E2E_GROUP_A not set — need a group id persona A belongs to (and persona B does not) "
            "to grant a throwaway collection to. See README.md.",
        )
        report.print_table()
        return report.exit_code

    suffix = unique_suffix()
    connection_id: str | None = None
    col_a = col_b = None

    with client_for(cfg, token=cfg.admin_token) as admin_client:
        try:
            col_a = _create_collection(admin_client, f"e2e-visibility-a-{suffix}")
            col_b = _create_collection(admin_client, f"e2e-visibility-b-{suffix}")
            _grant(admin_client, group_id=cfg.group_a, collection_id=col_a)

            path_public = f"e2e/{suffix}/plant-public.md"
            path_audience = f"e2e/{suffix}/plant-audience.md"
            path_secret = f"e2e/{suffix}/plant-secret.md"
            file_public = _upload_text_file(admin_client, col_a, path=path_public, content=b"public placeholder")
            file_audience = _upload_text_file(admin_client, col_a, path=path_audience, content=b"tiered placeholder")
            file_secret = _upload_text_file(admin_client, col_b, path=path_secret, content=b"secret placeholder")

            indexed_ok, indexed_detail = _wait_indexed(admin_client, col_a, [file_public, file_audience])
            if indexed_ok:
                indexed_ok_b, detail_b = _wait_indexed(admin_client, col_b, [file_secret])
                indexed_ok = indexed_ok and indexed_ok_b
                indexed_detail = detail_b if not indexed_ok_b else indexed_detail
            if not report.record("setup: planted files finished indexing", indexed_ok, short(indexed_detail)):
                report.skip("S1/S2/S3 assertions", "setup did not finish indexing — see the failed check above")
                report.print_table()
                return report.exit_code

            connection_id = _create_tiered_sharepoint_connection(
                admin_client, name=f"e2e-visibility-sp-{suffix}", collection_id=col_a, group_id=cfg.group_a
            )

            engagement_id = f"organization:e2e-engagement-{suffix}"
            person_a_id = f"person:e2e-person-a-{suffix}"
            person_b_id = f"person:e2e-person-b-{suffix}"
            doc_public, doc_audience, doc_secret = f"doc-pub-{suffix}", f"doc-aud-{suffix}", f"doc-sec-{suffix}"

            ingest_body = {
                "documents": [
                    {"doc_id": doc_public, "corpus_id": col_a, "path": path_public},
                    {"doc_id": doc_audience, "corpus_id": col_a, "path": path_audience},
                    {"doc_id": doc_secret, "corpus_id": col_b, "path": path_secret},
                ],
                "nodes": [
                    {
                        "id": engagement_id,
                        "type": "organization",
                        "attrs": {},
                        "evidence": [
                            {"doc_id": doc_public, "quote": path_public},
                            {"doc_id": doc_secret, "quote": path_secret, "attrs": {"price": 999888}},
                            {"doc_id": doc_audience, "quote": path_audience, "audience": "e2e-tier"},
                        ],
                    },
                    {
                        "id": person_a_id,
                        "type": "person",
                        "attrs": {},
                        "evidence": [{"doc_id": doc_public, "quote": path_public}],
                    },
                    {
                        "id": person_b_id,
                        "type": "person",
                        "attrs": {},
                        "evidence": [{"doc_id": doc_public, "quote": path_public}],
                    },
                ],
                "edges": [
                    {
                        "src": person_a_id,
                        "type": "knows",
                        "dst": person_b_id,
                        "attrs": {},
                        "evidence": [{"doc_id": doc_secret, "quote": path_secret}],
                    }
                ],
            }
            ingest_resp = admin_client.post("/api/facts/ingest", json=ingest_body)
            ingest_ok = ingest_resp.status_code == 200 and not ingest_resp.json().get("claims_rejected")
            if not report.record(
                "setup: ingest wrote every planted claim",
                ingest_ok,
                f"-> {ingest_resp.status_code} {short(ingest_resp.text)}",
            ):
                report.skip("S1/S2/S3 assertions", "ingest did not fully succeed — see the failed check above")
                report.print_table()
                return report.exit_code

            engagement_fact_id: str | None = None
            search_resp = admin_client.post(
                "/api/facts/search", json={"type": "organization", "q": engagement_id.split(":", 1)[1]}
            )
            subjects = search_resp.json().get("subjects", []) if search_resp.status_code == 200 else []
            if subjects:
                engagement_fact_id = subjects[0]["id"]
            if not report.record(
                "setup: resolved the engagement's opaque subject id (as admin)",
                engagement_fact_id is not None,
                f"search -> {search_resp.status_code} subjects={len(subjects)}",
            ):
                report.skip("S1/S2/S3 assertions", "could not resolve the planted subject id")
                report.print_table()
                return report.exit_code

            with client_for(cfg, token=cfg.token_b) as b_client:
                b_search = b_client.post(
                    "/api/facts/search", json={"type": "organization", "q": engagement_id.split(":", 1)[1]}
                )
                report.record(
                    "S1: B's search sees nothing of the ungranted engagement",
                    b_search.status_code == 200 and b_search.json().get("subjects") == [],
                    f"-> {b_search.status_code} subjects={short(b_search.text)}",
                )

                b_ghost = b_client.get(f"/api/facts/f_e2e_ghost_{unique_suffix()}/claims")
                b_secret = b_client.get(f"/api/facts/{engagement_fact_id}/claims")
                report.record(
                    "S6: B gets 404-parity between the ghost id and the real (ungranted) subject",
                    b_ghost.status_code == 404 and b_secret.status_code == 404 and b_ghost.text == b_secret.text,
                    f"ghost -> {b_ghost.status_code} {short(b_ghost.text)}; "
                    f"real -> {b_secret.status_code} {short(b_secret.text)}",
                )

            with client_for(cfg, token=cfg.token_a) as a_client:
                a_search = a_client.post(
                    "/api/facts/search", json={"type": "organization", "q": engagement_id.split(":", 1)[1]}
                )
                a_subjects = a_search.json().get("subjects", []) if a_search.status_code == 200 else []
                a_sees_it = any(s["id"] == engagement_fact_id for s in a_subjects)
                no_price_in_attrs = a_sees_it and "price" not in next(
                    s["attrs"] for s in a_subjects if s["id"] == engagement_fact_id
                )
                report.record(
                    "S2: A sees the engagement without a leaked `price` attr",
                    a_sees_it and no_price_in_attrs,
                    f"-> {a_search.status_code} subjects={short(a_search.text)}",
                )

                a_filtered = a_client.post(
                    "/api/facts/search", json={"type": "organization", "filters": {"price": 999888}}
                )
                report.record(
                    "S2: A's search filtered on the hidden `price` value returns no match",
                    a_filtered.status_code == 200 and a_filtered.json().get("subjects") == [],
                    f"-> {a_filtered.status_code} {short(a_filtered.text)}",
                )

                a_claims = a_client.get(f"/api/facts/{engagement_fact_id}/claims")
                report.record(
                    "S2/audience: A's claims() response never contains the hidden price value",
                    a_claims.status_code == 200 and "999888" not in a_claims.text and "price" not in a_claims.text,
                    f"-> {a_claims.status_code} claim_count={len(a_claims.json().get('claims', [])) if a_claims.status_code == 200 else 'n/a'}",
                )

                a_search_person = a_client.post(
                    "/api/facts/search", json={"type": "person", "q": person_a_id.split(":", 1)[1]}
                )
                a_person_subjects = (
                    a_search_person.json().get("subjects", []) if a_search_person.status_code == 200 else []
                )
                person_a_fact_id = next((s["id"] for s in a_person_subjects if s["id"]), None)
                if not report.record(
                    "setup: resolved person-A's opaque subject id (as A)",
                    person_a_fact_id is not None,
                    f"-> {a_search_person.status_code} {short(a_search_person.text)}",
                ):
                    report.print_table()
                    return report.exit_code

                a_neighbors = a_client.post("/api/facts/neighbors", json={"subject_id": person_a_fact_id})
                neighbors_body = a_neighbors.json() if a_neighbors.status_code == 200 else {}
                edges = neighbors_body.get("edges", [])
                node_ids = {n["id"] for n in neighbors_body.get("nodes", [])}
                report.record(
                    "S3: A's neighbors() never returns the edge whose only claim is unreadable",
                    a_neighbors.status_code == 200 and edges == [],
                    f"-> {a_neighbors.status_code} edges={short(edges)}",
                )
                report.record(
                    "S3: A's neighbors() never reveals person-B as a neighbor via that edge",
                    a_neighbors.status_code == 200 and not any(person_b_id.split(":", 1)[1] in nid for nid in node_ids),
                    f"node_ids={short(node_ids)}",
                )
        finally:
            _cleanup(admin_client, connection_id=connection_id, collection_ids=[c for c in (col_a, col_b) if c])

    report.print_table()
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
