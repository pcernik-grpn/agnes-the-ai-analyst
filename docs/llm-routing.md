# Plan: Modular LLM Routing for Corporate Memory

> Reviewed by: Claude Opus (author), Google Gemini, Claude Sonnet, OpenAI GPT-5.4
> Feedback incorporated from all three external reviewers.

## Context

Corporate Memory is a feature that reads team members' local notes (CLAUDE.local.md),
sends them to a small AI model (Claude Haiku) for knowledge extraction, and builds
a shared knowledge base. Currently it's hardwired to call Anthropic's API directly.

Different clients deploying this platform use different AI providers:

| Client profile | AI Provider | Why |
|----------------|------------|-----|
| Enterprise with central AI gateway | LiteLLM proxy | Cost control, audit, policy enforcement |
| Single-vendor deployment | Direct Anthropic | Simplest setup, one provider to operate |
| Multi-model deployments | OpenRouter | Multi-model access, cost optimization |
| GCP-native stack (Claude) | Google Vertex AI | Claude via GCP capacity/billing, keyless (ADC) |
| GCP-native stack (Gemini) | Google Gemini | Existing Google Cloud relationship |

**Problem**: The code only works with Anthropic. Adding a second client means duplicating
or rewriting the AI calling logic.

**Solution**: Extract the AI calling logic into a modular connector that each instance
configures for its own provider. The connector lives in the open-source repo (code),
the configuration lives in the private instance repo (config).

## Design Principles

### 1. Structured Extraction, Not General AI

This connector has one job: send a prompt, get back structured JSON.
It is NOT a general-purpose AI chat interface. The naming and interface reflect this:
`StructuredExtractor` (not "LLMProvider"), `extract_json()` (not "chat" or "generate").

This keeps the scope tight and the interface honest. If we need general AI capabilities
later, we build a separate abstraction.

### 2. Instance Config Drives Provider Selection

Each deployment configures its AI provider in `instance.yaml` (the same file that
already configures authentication, branding, data sources, and catalog integration).
Secrets use `${ENV_VAR}` references, resolved at load time by the existing config loader.

The open-source code never knows which provider it's talking to. It receives a configured
extractor and calls `extract_json()`.

### 3. Backward Compatibility

Existing deployments using `ai.anthropic_api_key` in their config continue to work
without changes. The factory recognizes the legacy config shape and creates the
appropriate provider automatically. No migration step required for existing instances.

### 4. Structured Output Strategy (Configurable)

Not all AI providers support JSON schema enforcement equally. The connector supports
three levels, but the operator controls which are allowed:

1. **JSON Schema mode** — provider enforces the exact schema (best quality)
2. **JSON Object mode** — provider guarantees valid JSON but no schema (good quality)
3. **Prompt-based JSON** — instructions in the prompt ask for JSON (acceptable quality)

By default, all three layers are available as progressive fallback. But the operator
can restrict this in config:

```yaml
ai:
  provider: "openai_compat"
  # --- Structured output quality control ---
  # AI models can return JSON in three ways, each with different reliability:
  #
  # Layer 1 - "json_schema" (best):
  #   The provider enforces an exact schema. Every field, type, and structure
  #   is guaranteed. Available on: Anthropic, OpenAI, Claude via LiteLLM.
  #
  # Layer 2 - "json_object" (good):
  #   The provider guarantees valid JSON, but does not enforce a specific schema.
  #   Fields may be missing or have wrong types. Available on most providers.
  #
  # Layer 3 - "prompt" (acceptable):
  #   The AI is asked to respond in JSON via instructions in the prompt.
  #   No technical enforcement — the model may still return invalid JSON.
  #   Works everywhere, but least reliable.
  #
  # "strict" = only Layer 1. Fail if provider doesn't support json_schema.
  #            Use when data quality is non-negotiable.
  # "json"   = Layer 1, fall back to Layer 2. No prompt-based fallback.
  #            Good balance of quality and compatibility.
  # "auto"   = All three layers as progressive fallback. Maximum compatibility.
  #            Use when you'd rather get imperfect data than no data.
  structured_output: "strict"
```

When set to `"strict"`, the connector will NOT fall back to weaker strategies.
If the provider doesn't support JSON schema, the extraction fails with a clear error.
This is the right choice when data quality is non-negotiable.

### 5. Fail-Safe by Default

- Missing config → Corporate Memory logs a warning and skips AI extraction (doesn't crash)
- AI call fails → item marked as "unsafe" (conservative, nothing leaks)
- Truncated response → detected and retried once
- Auth error → fails fast with clear message (don't retry forever)
- Rate limit → waits and retries with backoff

### 6. Zero Secrets in Logs

The connector NEVER logs:
- API keys, tokens, or any secret values
- Prompt content (may contain user notes with sensitive data)
- Response content (may contain extracted knowledge before sensitivity check)
- Full URLs with query parameters (may contain tokens)

What IS logged:
- Provider type and model name
- Sanitized base URL (scheme + host only, no path/query)
- Structured output strategy selected
- Call duration (latency)
- Error classification (auth/rate_limit/timeout/format — never the error body)
- Whether fallback was triggered

This is a hard rule, not a guideline. API keys and user content must never
appear in logs, stdout, stderr, or error messages propagated to callers.

## Architecture

### Where the code lives

```
OSS Repo (open-source, shared):
  connectors/llm/              ← NEW: AI provider abstraction
    base.py                       Interface definition
    anthropic_provider.py         Direct Anthropic API
    openai_compat.py              Any OpenAI-compatible proxy
    factory.py                    Creates the right provider from config

  services/corporate_memory/
    collector.py               ← MODIFIED: uses connector instead of direct API calls

Instance Repo (private, per-client):
  config/instance.yaml         ← MODIFIED: new ai: section
  .env                         ← MODIFIED: new LLM_API_KEY secret
```

### How it flows

```
instance.yaml (ai: section)
       ↓
  Config loader resolves ${ENV_VAR} secrets
       ↓
  Factory reads provider type, creates extractor
       ↓
  Corporate Memory calls extractor.extract_json(prompt, schema)
       ↓
  Extractor routes to the right API:
    ├─ Anthropic SDK  → api.anthropic.com/v1/messages
    ├─ AnthropicVertex SDK → {region}-aiplatform.googleapis.com (Google ADC, no key)
    └─ OpenAI SDK     → litellm.example.com/v1/chat/completions
                         openrouter.ai/v1/chat/completions
                         any OpenAI-compatible endpoint
```

### Config examples

**Claude on Google Vertex AI (keyless — Google ADC):**
```yaml
ai:
  provider: "vertex"
  vertex:
    project_id: "my-gcp-project"   # or env ANTHROPIC_VERTEX_PROJECT_ID
    region: "global"               # or a specific region; env CLOUD_ML_REGION
  model: "claude-haiku-4-5-20251001"   # first-party ids accepted; dated ids
                                       # translate to the @-form automatically
```
`structured_output` is ignored for vertex — like the anthropic provider it
uses native `json_schema` output, so the semantics match exactly. No
`api_key`: credentials come from Application Default Credentials
(GOOGLE_APPLICATION_CREDENTIALS → gcloud ADC → GCE/GKE attached service
account); requires the `anthropic[vertex]` extra (bundled in this repo's
dependencies).

**OpenAI-compatible proxy (LiteLLM, OpenRouter, Azure OpenAI, ...):**
```yaml
ai:
  provider: "openai_compat"
  base_url: "https://litellm.example.com"
  api_key: "${LLM_API_KEY}"
  model: "claude-haiku-4-5-20251001"
```

**Single-vendor deployment (direct Anthropic):**
```yaml
ai:
  provider: "anthropic"
  api_key: "${ANTHROPIC_API_KEY}"
  model: "claude-haiku-4-5-20251001"
```

**Legacy (existing deployments, no changes needed):**
```yaml
ai:
  anthropic_api_key: "${ANTHROPIC_API_KEY}"
```

## What We're Improving

### A. From Hardwired to Pluggable

**Before**: One provider, baked into the code. Changing provider = changing code.
**After**: Provider is a config choice. Switching from Anthropic to LiteLLM to OpenRouter
is a YAML change + secret rotation. No code touches needed.

### B. From Fragile to Resilient

**Before**: API error = entire collection run fails. No retries.
**After**:
- Transient errors (rate limits, timeouts, network) → automatic retry with backoff
- Permanent errors (bad API key, unsupported model) → fail fast, clear error message
- Truncated responses (model hit token limit) → detected, retried with note
- Model refuses request → logged, item skipped safely

### C. From All-or-Nothing to Progressive Degradation

**Before**: Structured output works or it doesn't. Binary.
**After**: Three fallback layers (schema → json_object → prompt-based). The connector
adapts to what each provider actually supports instead of assuming capabilities.

### D. From Silent to Observable

**Before**: No visibility into what the AI extraction does.
**After**:
- Which provider/model is being used (logged at startup)
- Which structured output strategy was selected (logged once)
- How long each call takes (logged per call)
- Whether fallback was triggered (logged as warning)
- Clear error classification in logs

### E. From Coupled to Separated

**Before**: AI provider choice is an engineering decision embedded in code.
**After**: AI provider choice is an operations decision in instance config.
Each client controls their own provider, model, and API gateway independently.

## Error Handling Strategy

| Error Type | What Happens | Why |
|-----------|-------------|-----|
| Missing `ai:` config | Corporate Memory skips AI extraction, logs warning | Don't crash the whole service |
| Invalid API key | Fail fast, log error, skip collection run | Don't waste retries on permanent failure |
| Rate limit (429) | Wait + retry with exponential backoff (3 attempts) | Transient, will resolve |
| Network timeout | Retry once, then fail | Might be transient |
| Truncated response | Detect via finish_reason, retry once | Model hit token limit |
| Model refusal | Log, mark item as unsafe | Conservative: don't share uncertain content |
| Invalid JSON response | Log, mark item as unsafe | Better to skip than crash |
| Structured output unsupported | Fall back to json_object, then prompt-based | Adapt to provider capabilities |

## Scope Boundaries

**In scope (v1):**
- Anthropic direct provider (existing behavior, tested)
- OpenAI-compatible proxy provider (LiteLLM, verified against a production proxy deployment)
- Backward compatibility with existing `ai.anthropic_api_key` config
- Three-layer structured output fallback
- Custom error hierarchy (auth / rate limit / timeout / format)
- Retry with backoff for transient errors
- Corporate Memory collector integration

**In scope since the Vertex provider landed:**
- Claude on Google Vertex AI (`provider: vertex`) — VertexExtractor subclasses
  AnthropicExtractor, so the whole retry/structured-output loop is shared.

### Which call sites honor `ai.provider: vertex` (TCRD-242 sweep)

Every consumer that builds an extractor through `connectors/llm/factory.py`
(`create_extractor`, `create_extractor_from_env_or_config`,
`vertex_config_or_none` + `create_vertex_extractor`) is Vertex-capable with no
further code changes — that's the whole point of routing through the factory
instead of constructing `anthropic.Anthropic(...)` directly. As of this sweep
that covers: `services/corporate_memory/collector.py` and its downstream
callers (`tagger.py`, `entities.py`, `contradiction.py`, `governance.py`,
`confidence.py` — all take an injected `StructuredExtractor`),
`services/verification_detector` (via `build_verification_processor`),
`src/store_guardrails/` (guardrails LLM review — see `llm_review.py` /
`craft_review.py` / `runner.py::default_api_key_loader`),
`src/table_autodoc.py` + `cli/commands/admin_autodoc.py`,
`src/knowledge_digests.py`, `app/api/admin_usage.py` (`/admin/usage/ask`),
and the admin builder endpoints (`app/api/package_builder.py`,
`app/api/mcp_builder.py`, `app/api/entity_builder.py`,
`app/api/agent_builder.py`, `app/api/memory.py`). `src/ingest/vision.py`
(Tier-2 image OCR) and the offline eval harness's `--llm-assist` grading pass
(`scripts/eval/grade.py::llm_assist_grade`) build a raw `AnthropicVertex`
client via `vertex_config_or_none()` + `connectors.llm.vertex_provider
.create_vertex_client()` instead — same fallback, no `StructuredExtractor`
involved because both need free-form `messages.create()` rather than
`extract_json()`.

**Interactive chat and the agent-as-API runtime use a *separate* config
surface** — `chat.llm.provider: vertex` / `chat.llm.vertex.*`, not `ai:` —
documented in `docs/cloud-chat.md` under *LLM provider: Google Vertex AI*.
That path already covers `app/chat/auto_title.py` (auto-title generation),
`app/chat/readiness.py`'s admin diagnostics probe, and
`POST /api/v1/agents/{slug}/responses` (the agent-as-API runtime spawns a
headless chat session through the same `app/api/broker.py` forwarding path
live chat uses, so pinned-model and token-budget enforcement already apply to
a Vertex-shaped model path there too). Two config blocks exist because they
gate genuinely different surfaces (server-side structured extraction vs. the
sandbox's interactive LLM traffic) — this sweep did not introduce a third.

**Deliberately excluded:** `scripts/eval/arms.py::AnthropicArm` (eval arm A0)
calls the first-party Anthropic API directly, on purpose — it measures the
"bare Claude, no tools" baseline the vendor's own hosted API produces,
independent of Agnes's provider routing; routing it through Vertex would
change what the baseline measures.

**Ops note before flipping a production instance to Vertex:** verify Claude
model availability in the target region's Vertex AI Model Garden (not every
region carries every model, and availability lags first-party release day),
and verify prompt-caching behavior/pricing parity for the specific model —
Vertex's cache semantics have historically trailed the first-party API and
should not be assumed identical without a live check.

**Explicitly NOT in scope (future):**
- Azure OpenAI, OpenRouter, Gemini — listed as "untested" until verified per-provider
- General-purpose AI chat/generation interface
- Streaming responses
- Multi-turn conversations
- Token usage tracking / cost monitoring (v2 consideration)
- Provider capability auto-detection at startup

## Testing Strategy

### Unit Tests (connector internals)
- Factory creates correct provider from each config shape
- Factory handles legacy `ai.anthropic_api_key` config
- Missing/invalid config raises clear errors
- Each provider formats API calls correctly (mocked SDK)
- Structured output fallback chain works
- Error classification (auth vs rate limit vs timeout)

### Integration Tests (Corporate Memory behavior)
- Full collection run with mocked provider
- Skip when no files changed (hash check)
- Preserve existing item IDs across runs
- Sensitivity check runs only on new items
- Fail-closed on sensitivity check errors
- user_hashes.json written only after successful processing
- Graceful degradation when `ai:` config is missing

### Manual Verification (before production)
- Dry-run against the production LiteLLM proxy deployment
- Verify structured output works through proxy
- Verify sensitivity check works through proxy
- Full collection produces valid knowledge.json

## Deployment

The connector ships with the platform and its SDKs are declared dependencies
(`pyproject.toml`), so enabling a provider on an instance is configuration
only — no code change and no manual install:

1. Add the `ai:` section to `config/instance.yaml` (see the config examples
   above; `config/instance.yaml.example` carries the annotated template)
2. Set the matching secret in `.env` — `ANTHROPIC_API_KEY` for the direct
   provider, `LLM_API_KEY` for an OpenAI-compatible proxy
   (both documented in `config/.env.template`)
3. Restart the service and verify with a dry run:
   `python -m services.corporate_memory.collector --dry-run`

**Rollback**: revert the `ai:` section. The legacy `ai.anthropic_api_key`
config shape is still honored, so an instance that never moved off it keeps
working unchanged.

## Risk Assessment

| Risk | Level | Mitigation |
|------|-------|-----------|
| LiteLLM structured output translation | Medium | Three-layer fallback + manual verification before deploy |
| Config migration breaks existing instances | Low | Backward compat shim for legacy config shape |
| New `openai` dependency conflicts | Low | Standard package, declared in `pyproject.toml` |
| Corporate Memory regression | Medium | Expanded behavior tests covering all current logic |
