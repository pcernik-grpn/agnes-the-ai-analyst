### Fixed
- **Extraction call records now say which document they were for.** The
  per-document `llm_calls` rows for `facts_extraction`/`facts_retry` now
  carry `subject_id` (the document's file id) — previously only the batch
  transport's own generation record did, so a sync-transport call was
  unattributable to a document from the ledger alone.
- **Vertex-hosted LLM calls (builder turns, corporate memory, OCR, ...) are
  now labelled `gcp.vertex_ai`, not `vertex`,** matching the chat broker and
  the fact-extraction stage, which already used the Google-Cloud label.
  `provider` on the call record and `gen_ai.system` on its span now agree
  across every Vertex call site.
- **`POST /api/admin/telemetry/ask` (the admin usage text-to-SQL assistant)
  is now traced.** Its LLM call previously bypassed observability entirely;
  it now runs under a new `admin_ask` workload (`purpose=telemetry_ask`)
  and is counted in `llm-cost`/`llm-calls`.

### Internal
- The static coverage guard (`tests/test_llm_coverage_guard.py`) now scans
  every module the design names plus the two remaining `connectors/llm`
  providers, and gained a second check that every `extract_json(` call site
  sets an `llm_context` — closing the exact gap the `admin_ask` fix above
  needed.
