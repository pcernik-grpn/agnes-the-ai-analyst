"""REST-layer tests for the fact graph over Collections write surface
(build order step 4 of
docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md):
``POST /api/facts/ingest``, ``PUT/DELETE /api/facts/corrections/{kind}/{id}``,
``GET /api/facts/corrections``.

DuckDB backend (the default ``seeded_app`` fixture) — proves the HTTP
wiring: the router-level feature-flag gate, the admin/scheduler-token auth
gate, request validation shape, and the PG-only typed-501 fail-clean shape.
Every assertion here resolves BEFORE the PG-only ``facts_repo()`` call, so
none of it needs a real Postgres backend. Batch-cap enforcement,
itemization, corrections CRUD round-trips, the verbatim gate, the
collections-delete sweep hook, and the real upload -> ingest -> search
end-to-end path all need a genuine Postgres backend (``pg_engine`` is a
``tests/db_pg/`` fixture, not visible here) and live in
``tests/db_pg/test_facts_ingest_pg.py`` instead — same split
``tests/test_api_facts.py`` / ``tests/db_pg/test_facts_read_pg.py`` use for
the read surface.
"""

from __future__ import annotations

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def facts_client(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
    return seeded_app


# ---------------------------------------------------------------------------
# flag gate — the whole router (including the new routes) disappears when off.
# ---------------------------------------------------------------------------


def test_ingest_404s_when_flag_off(seeded_app):
    r = seeded_app["client"].post("/api/facts/ingest", json={}, headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 404


def test_corrections_get_404s_when_flag_off(seeded_app):
    r = seeded_app["client"].get("/api/facts/corrections", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 404


def test_corrections_put_404s_when_flag_off(seeded_app):
    r = seeded_app["client"].put(
        "/api/facts/corrections/fact/f_x",
        json={"verdict": "wrong", "reason": "x"},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# auth — anon 401, non-admin PAT 403, admin/scheduler pass the gate.
# ---------------------------------------------------------------------------


def test_ingest_requires_authentication(facts_client):
    r = facts_client["client"].post("/api/facts/ingest", json={})
    assert r.status_code == 401


def test_ingest_requires_admin_not_just_any_caller(facts_client):
    r = facts_client["client"].post("/api/facts/ingest", json={}, headers=_auth(facts_client["analyst_token"]))
    assert r.status_code == 403


def test_corrections_crud_requires_admin(facts_client):
    headers = _auth(facts_client["analyst_token"])
    put = facts_client["client"].put(
        "/api/facts/corrections/fact/f_x", json={"verdict": "wrong", "reason": "x"}, headers=headers
    )
    assert put.status_code == 403
    delete = facts_client["client"].delete("/api/facts/corrections/fact/f_x", headers=headers)
    assert delete.status_code == 403
    get = facts_client["client"].get("/api/facts/corrections", headers=headers)
    assert get.status_code == 403


def test_corrections_get_anon_401(facts_client):
    r = facts_client["client"].get("/api/facts/corrections")
    assert r.status_code == 401


def test_scheduler_token_passes_the_admin_gate_on_ingest(facts_client, monkeypatch):
    """The scheduler shared-secret resolves to the synthetic Admin-group
    user through get_current_user (app/auth/scheduler_token.py) — same
    dual-accept pattern app/api/jobs.py documents. Proven by NOT getting
    401/403; DuckDB backend still 501s past the gate (facts_repo() is
    PG-only), which is the assertion that the gate itself was cleared."""
    secret = "x" * 40
    monkeypatch.setenv("SCHEDULER_API_TOKEN", secret)
    r = facts_client["client"].post("/api/facts/ingest", json={}, headers=_auth(secret))
    assert r.status_code == 501, r.text
    assert r.json()["error"] == "requires_postgres_backend"


def test_scheduler_token_passes_the_admin_gate_on_corrections_export(facts_client, monkeypatch):
    secret = "y" * 40
    monkeypatch.setenv("SCHEDULER_API_TOKEN", secret)
    r = facts_client["client"].get("/api/facts/corrections", headers=_auth(secret))
    assert r.status_code == 501, r.text


# ---------------------------------------------------------------------------
# PG-only fail-clean on DuckDB.
# ---------------------------------------------------------------------------


def test_ingest_fails_clean_on_duckdb(facts_client):
    r = facts_client["client"].post("/api/facts/ingest", json={}, headers=_auth(facts_client["admin_token"]))
    assert r.status_code == 501, r.text
    assert r.json()["error"] == "requires_postgres_backend"


def test_corrections_put_fails_clean_on_duckdb(facts_client):
    r = facts_client["client"].put(
        "/api/facts/corrections/fact/f_x",
        json={"verdict": "wrong", "reason": "hallucinated"},
        headers=_auth(facts_client["admin_token"]),
    )
    assert r.status_code == 501, r.text


# ---------------------------------------------------------------------------
# request validation shape (runs before the PG-only repo is ever reached).
# ---------------------------------------------------------------------------


def test_corrections_put_invalid_verdict_is_422(facts_client):
    r = facts_client["client"].put(
        "/api/facts/corrections/fact/f_x",
        json={"verdict": "maybe", "reason": "x"},
        headers=_auth(facts_client["admin_token"]),
    )
    assert r.status_code == 422


def test_corrections_put_invalid_subject_kind_is_422(facts_client):
    r = facts_client["client"].put(
        "/api/facts/corrections/widget/f_x",
        json={"verdict": "wrong", "reason": "x"},
        headers=_auth(facts_client["admin_token"]),
    )
    assert r.status_code == 422


def test_corrections_put_empty_reason_is_422(facts_client):
    r = facts_client["client"].put(
        "/api/facts/corrections/fact/f_x",
        json={"verdict": "wrong", "reason": ""},
        headers=_auth(facts_client["admin_token"]),
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/facts/ingest-runs — persisted run reports (spec §7.2/§13.2), the
# source card's data. Same DuckDB-side split as every other route on this
# router: flag/auth/fail-clean here, real reads in
# tests/db_pg/test_facts_ingest_runs_pg.py + the E2E extension in
# tests/db_pg/test_facts_ingest_pg.py.
# ---------------------------------------------------------------------------


def test_ingest_runs_404s_when_flag_off(seeded_app):
    r = seeded_app["client"].get("/api/facts/ingest-runs", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 404


def test_ingest_runs_anon_401(facts_client):
    r = facts_client["client"].get("/api/facts/ingest-runs")
    assert r.status_code == 401


def test_ingest_runs_requires_admin_not_just_any_caller(facts_client):
    r = facts_client["client"].get("/api/facts/ingest-runs", headers=_auth(facts_client["analyst_token"]))
    assert r.status_code == 403


def test_ingest_runs_fails_clean_on_duckdb(facts_client):
    r = facts_client["client"].get("/api/facts/ingest-runs", headers=_auth(facts_client["admin_token"]))
    assert r.status_code == 501, r.text
    assert r.json()["error"] == "requires_postgres_backend"


def test_ingest_runs_invalid_limit_is_422(facts_client):
    r = facts_client["client"].get(
        "/api/facts/ingest-runs", params={"limit": 0}, headers=_auth(facts_client["admin_token"])
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# `anonymization` block on POST /api/facts/ingest (spec §9.2) — additive,
# optional, validated BEFORE the PG-only repo call, so a malformed block
# 422s cleanly even on the DuckDB backend.
# ---------------------------------------------------------------------------


def test_ingest_malformed_anonymization_declared_is_422(facts_client):
    r = facts_client["client"].post(
        "/api/facts/ingest",
        json={"anonymization": {"declared": "not-a-bool"}},
        headers=_auth(facts_client["admin_token"]),
    )
    assert r.status_code == 422


def test_ingest_malformed_anonymization_scope_count_is_422(facts_client):
    r = facts_client["client"].post(
        "/api/facts/ingest",
        json={"anonymization": {"scopes": {"col_a": {"docs_anonymized": -1}}}},
        headers=_auth(facts_client["admin_token"]),
    )
    assert r.status_code == 422


def test_ingest_omitted_anonymization_is_fine_pre_pg(facts_client):
    """No `anonymization` key at all — the field is optional; the request
    still validates and only fails past the gate at the PG-only repo call,
    same as every other bare ingest request against the DuckDB backend."""
    r = facts_client["client"].post("/api/facts/ingest", json={}, headers=_auth(facts_client["admin_token"]))
    assert r.status_code == 501, r.text


# ---------------------------------------------------------------------------
# `llm_usage` block on POST /api/facts/ingest (cost-visibility) — additive,
# optional, strictly typed (same posture as `anonymization` above), validated
# BEFORE the PG-only repo call, so a malformed block 422s cleanly even on the
# DuckDB backend.
# ---------------------------------------------------------------------------


def test_ingest_malformed_llm_usage_negative_token_count_is_422(facts_client):
    r = facts_client["client"].post(
        "/api/facts/ingest",
        json={"llm_usage": {"input_tokens": -5}},
        headers=_auth(facts_client["admin_token"]),
    )
    assert r.status_code == 422


def test_ingest_malformed_llm_usage_wall_seconds_wrong_type_is_422(facts_client):
    r = facts_client["client"].post(
        "/api/facts/ingest",
        json={"llm_usage": {"wall_seconds": "not-a-number"}},
        headers=_auth(facts_client["admin_token"]),
    )
    assert r.status_code == 422


def test_ingest_malformed_llm_usage_models_wrong_type_is_422(facts_client):
    r = facts_client["client"].post(
        "/api/facts/ingest",
        json={"llm_usage": {"models": "claude-sonnet-4"}},  # must be a list, not a bare string
        headers=_auth(facts_client["admin_token"]),
    )
    assert r.status_code == 422


def test_ingest_omitted_llm_usage_is_fine_pre_pg(facts_client):
    """No `llm_usage` key at all — the field is optional; the request still
    validates and only fails past the gate at the PG-only repo call, same as
    every other bare ingest request against the DuckDB backend."""
    r = facts_client["client"].post("/api/facts/ingest", json={}, headers=_auth(facts_client["admin_token"]))
    assert r.status_code == 501, r.text


def test_ingest_well_formed_llm_usage_is_fine_pre_pg(facts_client):
    r = facts_client["client"].post(
        "/api/facts/ingest",
        json={
            "llm_usage": {
                "input_tokens": 1000,
                "output_tokens": 200,
                "cache_read_input_tokens": 50,
                "cache_creation_input_tokens": 5,
                "models": ["claude-sonnet-4"],
                "documents": 3,
                "wall_seconds": 4.2,
            }
        },
        headers=_auth(facts_client["admin_token"]),
    )
    assert r.status_code == 501, r.text  # cleared validation; fails clean past it, same as every other body


# ---------------------------------------------------------------------------
# anonymize-fail-closed gate: refuse a batch that carries claims for a
# corpus whose SharePoint scope is anonymize-marked unless the batch's own
# `anonymization` block declares it. Runs BEFORE the PG-only `facts_repo()`
# call, keyed off `source_connections` (a DuckDB<->PG frozen pair) — so the
# whole gate is provable on the DuckDB backend without a real Postgres.
# ---------------------------------------------------------------------------


def _create_sharepoint_connection(client, admin_token, *, name: str, corpus_id: str, anonymize: bool = True) -> None:
    r = client.post(
        "/api/admin/source-connections",
        json={
            "name": name,
            "source_type": "sharepoint",
            "config": {
                "tenant_id": "tenant-1",
                "client_id": "client-1",
                "scopes": [
                    {
                        "source_scope_id": "site1!drive1",
                        "display_path": "Contracts",
                        "anonymize": anonymize,
                        "collection_id": corpus_id,
                    }
                ],
            },
        },
        headers=_auth(admin_token),
    )
    assert r.status_code == 201, r.text


def test_ingest_refuses_anonymize_marked_corpus_without_declaration(facts_client):
    """The report's exact failure mode: a corpus whose SharePoint scope is
    anonymize=true, ingested with no `anonymization` block at all — refused
    with a typed reason, itemizing the offending corpus, before the PG-only
    repo (and therefore any write) is ever reached."""
    client, token = facts_client["client"], facts_client["admin_token"]
    _create_sharepoint_connection(client, token, name="sp-gate-1", corpus_id="col_marked")

    r = client.post(
        "/api/facts/ingest",
        json={"documents": [{"doc_id": "d1", "corpus_id": "col_marked", "path": "f.md"}]},
        headers=_auth(token),
    )
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert detail["reason"] == "anonymization_not_declared"
    assert detail["corpus_ids"] == ["col_marked"]


def test_ingest_accepts_when_declaration_covers_the_corpus(facts_client):
    """Same shape, but the batch declares the corpus — the gate must let it
    through to the PG-only repo call (proven by the 501, not a 403)."""
    client, token = facts_client["client"], facts_client["admin_token"]
    _create_sharepoint_connection(client, token, name="sp-gate-2", corpus_id="col_declared")

    r = client.post(
        "/api/facts/ingest",
        json={
            "documents": [{"doc_id": "d1", "corpus_id": "col_declared", "path": "f.md"}],
            "anonymization": {"declared": True, "scopes": {"col_declared": {"docs_anonymized": 1}}},
        },
        headers=_auth(token),
    )
    assert r.status_code == 501, r.text
    assert r.json()["error"] == "requires_postgres_backend"


def test_ingest_unmarked_corpus_is_unaffected(facts_client):
    """No SharePoint connection at all marks this corpus — the gate must be
    a no-op (proven by the 501 falling through unchanged, exactly like the
    pre-existing `test_ingest_omitted_anonymization_is_fine_pre_pg`)."""
    client, token = facts_client["client"], facts_client["admin_token"]
    r = client.post(
        "/api/facts/ingest",
        json={"documents": [{"doc_id": "d1", "corpus_id": "col_unmarked", "path": "f.md"}]},
        headers=_auth(token),
    )
    assert r.status_code == 501, r.text


def test_ingest_lookup_failure_refuses_never_falls_through_to_accept(facts_client, monkeypatch):
    """A broken `source_connections` lookup must not be read as "nothing is
    marked" — that would silently accept plaintext into a corpus this
    instance cannot prove is safe. Fails closed: refused, not 501/200."""
    client, token = facts_client["client"], facts_client["admin_token"]

    def _boom(*args, **kwargs):
        raise RuntimeError("connections table unreadable")

    monkeypatch.setattr("src.repositories.source_connections_repo", _boom)

    r = client.post(
        "/api/facts/ingest",
        json={"documents": [{"doc_id": "d1", "corpus_id": "col_x", "path": "f.md"}]},
        headers=_auth(token),
    )
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["reason"] == "anonymization_check_unavailable"


# ---------------------------------------------------------------------------
# Producer-scoped callback credential (`app.auth.producer_token`) — the
# corpus-extraction producer's own narrow credential now accepted here
# alongside admin/scheduler (replaces forwarding the scheduler shared
# secret, see app/worker/kinds.py::_agnes_producer_callback_env). `/ingest`
# additionally scope-checks every document's `corpus_id` against the
# token's own `collection_ids`; `/corrections` is accepted unfiltered (see
# `list_corrections`'s own docstring for why a cheap filter isn't possible
# yet).
# ---------------------------------------------------------------------------


def _producer_token(collection_ids=(), connection_id="conn1") -> str:
    from app.auth.producer_token import mint_producer_token

    return mint_producer_token(connection_id=connection_id, collection_ids=list(collection_ids), ttl_seconds=3600)


def test_ingest_producer_token_passes_the_gate_then_501s_on_duckdb(facts_client):
    """Proven the same way the scheduler-token test above is: NOT
    401/403 — the DuckDB-backed `facts_repo()` 501 is what's left past
    the gate."""
    token = _producer_token(["col_a"])
    r = facts_client["client"].post(
        "/api/facts/ingest",
        json={"documents": [{"doc_id": "d1", "corpus_id": "col_a", "path": "f.md"}]},
        headers=_auth(token),
    )
    assert r.status_code == 501, r.text
    assert r.json()["error"] == "requires_postgres_backend"


def test_ingest_rejects_a_document_outside_the_producer_tokens_scope(facts_client):
    """Authorization-level gate (TCRD-...): a producer scoped to col_a must
    never write into col_b just because it can reach this endpoint at
    all — itemized 403, independent of the anonymize gate above."""
    token = _producer_token(["col_a"])
    r = facts_client["client"].post(
        "/api/facts/ingest",
        json={"documents": [{"doc_id": "d1", "corpus_id": "col_b", "path": "f.md"}]},
        headers=_auth(token),
    )
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert detail["reason"] == "producer_corpus_out_of_scope"
    assert detail["corpus_ids"] == ["col_b"]


def test_ingest_rejects_a_mixed_batch_itemizing_only_the_out_of_scope_ids(facts_client):
    token = _producer_token(["col_a"])
    r = facts_client["client"].post(
        "/api/facts/ingest",
        json={
            "documents": [
                {"doc_id": "d1", "corpus_id": "col_a", "path": "f1.md"},
                {"doc_id": "d2", "corpus_id": "col_b", "path": "f2.md"},
                {"doc_id": "d3", "corpus_id": "col_c", "path": "f3.md"},
            ]
        },
        headers=_auth(token),
    )
    assert r.status_code == 403, r.text
    assert sorted(r.json()["detail"]["corpus_ids"]) == ["col_b", "col_c"]


def test_ingest_rejects_a_producer_batch_that_declares_no_corpus(facts_client):
    """The `documents[]`-only scope check is bypassable on its own.

    `FactsPgRepository.ingest_batch`'s doc_id ladder falls back to an
    UNRESTRICTED, instance-wide scan (`_resolve_doc` tier 3b) precisely when
    `documents[]` declared no `(doc_id, corpus_id)` pair — the documented
    "documents may be omitted when every doc_id already resolves" replay flow
    (spec §7.2). So a producer scoped to col_a could send `documents: []` plus
    node evidence naming a doc_id that lives in col_b, and its claims would
    anchor onto col_b's file. Nothing in the batch carries a `corpus_id` for
    the per-document check to look at, so it passes.

    403 rather than the 501 that means "past the gate" — which is what this
    same request returned before the fix.
    """
    token = _producer_token(["col_a"])
    r = facts_client["client"].post(
        "/api/facts/ingest",
        json={
            "documents": [],
            "nodes": [{"id": "n1", "type": "client", "evidence": [{"doc_id": "d_in_col_b", "quote": "q"}]}],
        },
        headers=_auth(token),
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["reason"] == "producer_batch_declares_no_corpus"


def test_ingest_rejects_a_producer_full_documents_replace_with_no_corpus(facts_client):
    """`full_documents` is replace mode — it DELETES the listed documents'
    existing claims — and it is a bare list of doc_ids with no corpus_id
    anywhere, so the same undeclared-corpus batch is a cross-collection
    DELETE, not just a write."""
    token = _producer_token(["col_a"])
    r = facts_client["client"].post(
        "/api/facts/ingest",
        json={"documents": [], "full_documents": ["d_in_col_b"]},
        headers=_auth(token),
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["reason"] == "producer_batch_declares_no_corpus"


def test_ingest_allows_a_producer_batch_that_declares_an_in_scope_corpus(facts_client):
    """Declaring one in-scope `(doc_id, corpus_id)` pair is enough: from then
    on `declared_corpus_ids` is a subset of the token's own scope (the
    per-document check has already refused any other corpus_id), so tiers 1-3a
    cannot resolve outside it and the batch proceeds — 501, i.e. past the
    gate, not 403."""
    token = _producer_token(["col_a"])
    r = facts_client["client"].post(
        "/api/facts/ingest",
        json={
            "documents": [{"doc_id": "d1", "corpus_id": "col_a", "path": "f.md"}],
            "nodes": [{"id": "n1", "type": "client", "evidence": [{"doc_id": "d1", "quote": "q"}]}],
        },
        headers=_auth(token),
    )
    assert r.status_code == 501, r.text


def test_ingest_undeclared_corpus_replay_still_works_for_an_admin(facts_client):
    """The documents-omitted replay flow is the repository's documented
    behaviour and dozens of existing callers depend on it. The new refusal is
    scoped to a ProducerPrincipal only — an admin sending the same shape is
    unaffected (501 past the gate, never 403)."""
    r = facts_client["client"].post(
        "/api/facts/ingest",
        json={
            "documents": [],
            "nodes": [{"id": "n1", "type": "client", "evidence": [{"doc_id": "d_anywhere", "quote": "q"}]}],
        },
        headers=_auth(facts_client["admin_token"]),
    )
    assert r.status_code == 501, r.text


def test_ingest_producer_batch_with_no_doc_ids_at_all_is_not_refused(facts_client):
    """Nothing to resolve, nothing to escape: a batch that references no
    doc_id anywhere cannot reach the unrestricted scan, so the new gate must
    not fire on it (over-refusing would break a legitimate empty/no-op call)."""
    token = _producer_token(["col_a"])
    r = facts_client["client"].post(
        "/api/facts/ingest",
        json={"documents": [], "nodes": [{"id": "n1", "type": "client"}]},
        headers=_auth(token),
    )
    assert r.status_code != 403, r.text


def test_ingest_producer_scope_gate_is_a_noop_for_admin(facts_client):
    """An admin (or the scheduler token) has no `collection_ids` claim to
    check against — unaffected by the new gate, unchanged 501 past it."""
    r = facts_client["client"].post(
        "/api/facts/ingest",
        json={"documents": [{"doc_id": "d1", "corpus_id": "col_anything", "path": "f.md"}]},
        headers=_auth(facts_client["admin_token"]),
    )
    assert r.status_code == 501, r.text


def test_corrections_get_accepts_a_producer_token(facts_client):
    """No collection_ids scoping possible for this route today (documented
    TODO on `list_corrections`) — accepted, then 501s on DuckDB exactly
    like the scheduler-token/admin cases."""
    token = _producer_token(["col_a"])
    r = facts_client["client"].get("/api/facts/corrections", headers=_auth(token))
    assert r.status_code == 501, r.text
    assert r.json()["error"] == "requires_postgres_backend"


def test_correction_write_routes_still_reject_a_producer_token(facts_client):
    """The producer's scoped credential is off-surface for the
    corrections WRITE routes entirely (not on `_PRODUCER_ALLOWED_SURFACE`
    at all) — refused before this router's own `require_admin` gate ever
    runs."""
    token = _producer_token(["col_a"])
    put = facts_client["client"].put(
        "/api/facts/corrections/fact/f_x", json={"verdict": "wrong", "reason": "x"}, headers=_auth(token)
    )
    assert put.status_code == 403
    delete = facts_client["client"].delete("/api/facts/corrections/fact/f_x", headers=_auth(token))
    assert delete.status_code == 403


def test_ingest_runs_still_rejects_a_producer_token(facts_client):
    """`GET /api/facts/ingest-runs` is not one of the producer's five
    allowed endpoints — off-surface 403, even though it sits on the same
    `require_admin` family as `/ingest`."""
    token = _producer_token(["col_a"])
    r = facts_client["client"].get("/api/facts/ingest-runs", headers=_auth(token))
    assert r.status_code == 403


def test_read_surface_still_rejects_a_producer_token(facts_client):
    """The generic `Depends(get_current_user)` read routes (search/
    neighbors/claims/facets) have no further gate of their own — exactly
    the over-wide surface the fixed allowlist in
    `app.auth.producer_token` exists to close."""
    token = _producer_token(["col_a"])
    r = facts_client["client"].post("/api/facts/search", json={"type": "client"}, headers=_auth(token))
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# SharePoint source-ACL ingest gate (2026-08-31 plan, Task 5 —
# connectors/sharepoint/ingest_gate.py) — independent of, and checked
# alongside, the anonymize-fail-closed and producer-scope gates above. Direct
# repo writes (never the wizard's HTTP endpoint) so the fixture can seed
# `excluded_subtrees`/`access_mode`, which `_create_sharepoint_connection`
# above does not exercise.
# ---------------------------------------------------------------------------


def _seed_sharepoint_scope_with_exclusion(corpus_id: str, *, connection_id: str) -> None:
    from src.repositories import source_connections_repo

    source_connections_repo().create(
        id=connection_id,
        name=f"SP Facts Gate {connection_id}",
        source_type="sharepoint",
        config={
            "tenant_id": "tenant-1",
            "client_id": "client-1",
            "scopes": [
                {
                    "source_scope_id": "root-1",
                    "display_path": "Site/Documents",
                    "anonymize": False,
                    "collection_id": corpus_id,
                    "drive_id": "d1",
                    "access_mode": "mirrored",
                    "excluded_subtrees": [
                        {
                            "item_id": "X",
                            "path": "Secret",
                            "rel_path": "Secret",
                            "kind": "folder",
                            "detected_at": "2026-08-31T00:00:00+00:00",
                        }
                    ],
                }
            ],
        },
    )


def test_ingest_refuses_a_document_under_an_excluded_sharepoint_subtree(facts_client, monkeypatch):
    """The fact-graph ingest gate independently enforces the same
    SharePoint source-ACL exclusions the upload endpoint does — a producer
    that batches claims straight from crawl metadata cannot land them for
    content Agnes has already excluded, even if it skipped the upload."""
    monkeypatch.setenv("AGNES_ACL_MIRRORING_ENABLED", "true")
    client, token = facts_client["client"], facts_client["admin_token"]
    _seed_sharepoint_scope_with_exclusion("col_gate_1", connection_id="conn-facts-gate-1")

    r = client.post(
        "/api/facts/ingest",
        json={"documents": [{"doc_id": "d1", "corpus_id": "col_gate_1", "path": "Secret/contract.docx"}]},
        headers=_auth(token),
    )
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "source_acl_excluded_documents"
    assert detail["items"] == [{"doc_id": "d1", "reason": "source_acl_excluded"}]


def test_ingest_allows_a_document_outside_the_excluded_subtree(facts_client, monkeypatch):
    monkeypatch.setenv("AGNES_ACL_MIRRORING_ENABLED", "true")
    client, token = facts_client["client"], facts_client["admin_token"]
    _seed_sharepoint_scope_with_exclusion("col_gate_2", connection_id="conn-facts-gate-2")

    r = client.post(
        "/api/facts/ingest",
        json={"documents": [{"doc_id": "d1", "corpus_id": "col_gate_2", "path": "open/notes.md"}]},
        headers=_auth(token),
    )
    assert r.status_code == 501, r.text  # cleared this gate; PG-only repo fails clean past it
    assert r.json()["error"] == "requires_postgres_backend"


def test_ingest_source_acl_gate_is_a_noop_when_acl_mirroring_is_off(facts_client):
    """Same excluded-subtree config, but the feature flag stays off — the
    gate must be a strict no-op (proven by the 501 falling through
    unchanged, exactly like the pre-existing anonymize/producer no-op
    tests above)."""
    client, token = facts_client["client"], facts_client["admin_token"]
    _seed_sharepoint_scope_with_exclusion("col_gate_3", connection_id="conn-facts-gate-3")

    r = client.post(
        "/api/facts/ingest",
        json={"documents": [{"doc_id": "d1", "corpus_id": "col_gate_3", "path": "Secret/contract.docx"}]},
        headers=_auth(token),
    )
    assert r.status_code == 501, r.text
