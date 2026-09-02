# Fact extraction — where the money actually goes

**Status:** investigation and proposal. **Nothing implemented, nothing
recommended for implementation without the owner reading this first.** No
code in this PR beyond this document.

**Scope:** the LLM stage of the built-in document pipeline
(`connectors/sharepoint/facts_extraction.py`) — the only stage that spends
model tokens per document. Two levers were asked for; the measurement found
a third and a fourth that outrank one of them, and disproved a premise of
the fifth.

Related: [fact-graph over Collections](2026-08-27-fact-graph-over-collections-design.md)
§8 (the verbatim gate), [extraction observability](2026-08-31-extraction-observability-ui-design.md).

---

## 1. Measured baseline

One live pass, default model (Haiku-class), no batching, concurrency 3:

| | |
|---|---|
| documents shipped | 187 |
| model calls | 347 |
| input tokens | 4,695,740 |
| output tokens | 1,089,419 |
| cache read / cache write tokens | 0 / 0 |

At list price ($1/MTok in, $5/MTok out):

| | |
|---|---:|
| input | $4.70 |
| output | $5.45 |
| **total** | **$10.14** |
| **per document** | **$0.0542** |
| input per document | 25,111 tok |
| output per document | 5,826 tok |
| **calls per document** | **1.856** |

Output is the larger half of the bill. That is the headline the earlier cost
model missed.

### 1.1 Against the shipped estimate

`config/instance.yaml.example` (the `extraction.facts.enabled` note) claims
"$0.011 for a typical ~5k-token document … $0.020 when the corrective
verbatim retry fires, ~$0.045 for a 30k-token one", and adds "the prompt +
ontology prefix is cached".

Reality: $0.0542 per document — above even the 30k-token figure — and the
prefix is cached zero times (§6). Solving the run's arithmetic for the
average user message gives **~11,500 tokens of document text per call**,
i.e. the corpus sits between the note's two bracket cases, so the 5.4×
overshoot is not "the documents were bigger than assumed". It is the retry
rate (§2) plus an unpriced input inflation (§4).

Two of that note's sentences are now known-false and should be corrected
whenever this area is next touched: the cached-prefix claim, and the
per-document figures.

---

## 2. Lever 1a — the corrective retry fires on 85.6% of documents

This is the dominant cost, and nobody had priced it.

```
347 calls − 187 documents = 160 retries   →   85.6% of shipped documents
```

Every document costs at least one call; `extract_one` adds at most one more
(`connectors/sharepoint/facts_extraction.py:1099`). Usage is only recorded
on a *successful* response (`_Extractor._record`), so transport-level retries
inside `_Extractor.call` do not inflate the call count. 160 of 187 documents
therefore went through the corrective verbatim retry.

**The retry re-sends the entire document.** `_retry_message`
(`facts_extraction.py:714`) is `base_user_message + <listing of failures>` —
a fresh single-turn request carrying the full ~11.5k-token document text
again, plus the same system prompt.

What that costs, bracketed by how much the retry re-emits (the one thing the
run report cannot tell us — see §8):

| assumption about retry output | first-pass output/doc | cost of the retry |
|---|---:|---:|
| retry re-emits everything | 3,140 tok | **$4.70 — 46.4% of the bill** |
| retry re-emits ~40% | 4,340 tok | **$3.58 — 35.3% of the bill** |
| retry re-emits ~20% | 4,975 tok | **$2.99 — 29.4% of the bill** |

**The corrective retry is 29–46% of this run's bill.** Compacting the wire
format (§3) cannot reach that; making the first attempt pass can.

And the retry is not a clean win even when it fires: whatever it fails to
rescue is dropped and counted in `facts_quotes_dropped`. A high retry rate
is simultaneously a cost problem and a recall problem.

### 2.1 Why the first attempt fails — three structural causes, none of them "the model is sloppy"

**(a) The gate is byte-exact, with no normalization at all.**
`src/repositories/facts_pg.py:340` states it outright: "No normalization
anywhere: matches the chunk-text comparison exactly (an NFC/NFD quote fails
identically on both sides)." Over a corpus converted from PDF/DOCX/PPTX,
that means a curly apostrophe against a straight one, NFC against NFD, a
non-breaking space, a soft hyphen, a doubled space, or a mid-sentence line
break in the converted markdown each turns an *honest* quote into a
verbatim failure. The model is reproducing what it read; the comparison is
stricter than the rendering.

**(b) A quote may not cross a chunk boundary, and the model cannot see the
boundaries.** The model is sent the concatenation —
`_document_text` returns `"\n\n".join(chunks)` (`facts_extraction.py:791`) —
but `quote_is_verbatim` tests membership **per chunk**
(`facts_extraction.py:513`). Chunks are 3,200 characters
(`src/ingest/chunking.py:17`), so a ~46k-character document has ~16 chunks
and ~15 internal boundaries. Every boundary is an invisible landmine: a
sentence straddling one is verbatim in the document the model was shown and
absent from every chunk. The 400-char overlap rescues some of these by
accident, not by design.

**(c) The retry cannot fix either class.** It shows the model the failing
quote and asks for "an exact substring". For a normalization mismatch the
model re-emits the same characters it still reads; for a boundary-spanning
quote there is no signal about where the boundary is. Both classes therefore
pay for a second full-document call and then get dropped anyway.

### 2.2 The proposal — repair deterministically, in process, at zero tokens

Before the corrective retry fires, try to *snap* each failing quote to real
document bytes: normalize both the quote and each chunk (Unicode NFC,
collapse whitespace runs, fold the common punctuation confusables), look for
a unique match, and if one is found, replace the quote with the exact
substring of the chunk that produced it.

This does **not** weaken the gate. The gate stays byte-exact; the citation is
corrected to point at bytes that genuinely exist, so the shipped evidence is
strictly more accurate than what the model emitted. Anything that does not
match uniquely falls through to the existing retry, and anything the retry
does not rescue is dropped exactly as today.

Same pass should also decide (b): either check the quote against the joined
text the model was actually shown, or hand the model the de-overlapped
document and check against that — see §4, which wants the same change for a
different reason.

Expected effect: the retry rate falls to whatever fraction of failures are
genuine fabrications rather than transcription artifacts. **How that splits
is the single most valuable unknown in this document** (§8), and one
instrumented pass settles it at no extra model cost: log the first N failing
quotes alongside their nearest normalized match.

---

## 3. Lever 1b — output wire format

Asked directly: how much of the 5,826 output tokens per document is
mandatory evidence, and how much is syntax?

Measured on a representative five-fact block in the exact shape the prompt
mandates (node `{id, type, attrs, evidence:[{doc_id, quote}]}`, edge
`{src, type, dst, attrs, evidence:[…]}`), with a quote mix of 45/120/240
characters:

| component | chars | share |
|---|---:|---:|
| verbatim quotes | 765 | 45.7% — **mandatory floor** |
| ids, types, attribute values | 280 | 16.7% — the facts themselves |
| restated `doc_id` | 125 | 7.5% — **constant; the caller already knows it** |
| JSON keys and punctuation | 504 | 30.1% — pure syntax |
| **removable without losing a fact** | **629** | **37.6%** |

Two candidate changes:

- **Drop `doc_id` from evidence and let the caller inject it.** The prompt
  itself says it is always the document being read (rule 2), and
  `_Work.doc_id` already holds it on the main thread. Worth **11.6%** of
  output characters. Cheapest possible change: one prompt line, one line in
  `extract_one`.
- **Replace JSONL with a tab-delimited line format** (`N⇥id⇥attrs⇥quote`,
  `E⇥src⇥type⇥dst⇥attrs⇥quote`). Worth **34.5%** of output characters on
  this mix, and it raises the quote's share of the output from 45.7% to
  69.8%.

Sensitivity — the answer depends entirely on how long real quotes are, which
this run's report does not record:

| average quote length | quote share of output | compact-format saving |
|---:|---:|---:|
| 40 chars | 18.0% | 52.0% |
| 80 chars | 30.5% | 44.1% |
| 160 chars | 46.8% | 33.8% |
| 300 chars | 62.2% | 24.0% |
| 400 chars | 68.7% | 19.8% |

What a saving is worth here: 10% off output = $0.54 (5.4% of the bill); 25%
= $1.36 (13.4%); 34% = $1.85 (18.3%).

**Two honest caveats.** These are *character* counts, not tokens — JSON
punctuation tokenizes worse than prose, so the structural share in tokens is
at least this large; the figures are a floor. And a format change means
rewriting `parse_streams`, its tolerance for stray prose, and the retry
listing — against a model that has seen far more JSON than any bespoke
delimited format, so it trades a known-good parse rate for tokens. **Do not
do this before §2 lands**: if the retry rate falls, the same percentage is
worth roughly half as many dollars, and a format change made under a broken
retry rate would be measured against the wrong baseline.

### 3.1 `max_tokens` truncation is currently invisible

`DEFAULT_MAX_OUTPUT_TOKENS = 16_000` (`facts_extraction.py:106`), against
~3,100–5,000 observed output tokens per call, so the cap is unlikely to bite
today. But the run report cannot tell us: `docs_truncated` counts
**input**-side truncation at `DEFAULT_MAX_DOC_CHARS = 120_000`, nothing
else. `_reply_text` concatenates text blocks and never inspects
`stop_reason`, so a response cut off at `max_tokens` is parsed as if
complete — the severed final line raises the parse-error count by one and
every fact before it ships normally. A one-line counter on
`stop_reason == "max_tokens"` would close a silent-data-loss hole that costs
nothing to add. Not a cost lever; an honesty lever, and it belongs in the
same diff as any output-format work.

---

## 4. The unpriced lever nobody asked about — 14.3% of every document is duplicated

Chunking is 3,200 characters with 400 characters of overlap
(`src/ingest/chunking.py:17-18`). `_document_text` then rebuilds the model's
view by joining those chunks. The overlap is therefore **sent to the model
twice**: every 2,800 characters of real document arrives as 3,200
characters of prompt.

```
inflation = 3200/2800 − 1 = 14.3% extra input characters
≈ 1,389 tok per call  ×  347 calls  =  482k tokens  =  $0.48  =  4.8% of the bill
```

That 14.3% is a **ceiling, not the corpus figure**. `src/ingest/chunking.py`
windows each structured element separately, and `_window` returns an element
shorter than `target_chars` whole — with no overlap at all. A deck or a table
of short elements therefore pays nothing here, and the real saving lies
somewhere between zero and the number above, depending on how much of the
corpus is long-form prose. Measuring it is one query over `corpus_chunks`
(what share of chunks are full-length windows) and should precede the work,
not follow it.

Bigger than the prompt-caching win (§6), and a pure deletion — the model
sees the same sentences twice, which is at best noise and at worst a nudge
toward duplicate facts.

The catch is that the overlap is quietly doing gate work: a quote spanning a
boundary can land wholly inside the next chunk's 400-char head. So this is
the *same decision* as §2.1(b) — pick one document representation, send that,
and check quotes against it. Fixing them together is one change; fixing
either alone is a regression risk for the other.

---

## 5. Lever 2 — Batch API

### 5.1 What it buys

50% off **every token in the request, including cache reads and writes** —
the discounts stack. On this run: **$5.07**. The workload is a textbook fit
— offline, embarrassingly parallel, no user waiting, no mid-call tool loop.

Limits that matter here: 100,000 requests **or 256 MB** per batch. At ~46 KB
of user message per document the byte cap binds first — roughly **5,500
documents per batch**, so 187 documents is one batch (~8.6 MB) and a
100k-document corpus is ~19. Most batches finish within an hour; 24 hours is
the expiry, not an SLA. Results are readable for 29 days and arrive in
**any order** — they must be keyed by `custom_id`, never by position.

### 5.2 What it costs in complexity — plainly

The current path is `_plan()` → `ThreadPoolExecutor.submit(extract_one, …)`
→ drain in submission order → `_BatchShipper` → `facts_ingest` →
`save_state`. Batching changes the *shape* of the run, not a parameter:

**(a) The corrective retry becomes a second batch.** There is no mid-batch
repair. The pass becomes: submit → poll → collect → verbatim-check all →
submit a *retry batch* → poll → collect → merge. Two poll cycles, up to 48
hours worst case, and two accounting phases. This is the strongest argument
for doing §2 first: if the deterministic repair drops the retry rate, the
second batch is often empty and this complexity mostly evaporates.

**(b) The deadline stops meaning anything, and the lane slot becomes the
real collision.** `_Deadline` (`connectors/sharepoint/crawler.py:1565`)
bounds one process's wall clock; a batch run's wall clock belongs to the
provider. `run_facts_extraction` would have to become resumable across
process restarts: persist `{batch_id, submitted_at, doc_ids}` into the
per-connection state file, return, and let a later tick poll. **This is the
real cost of the lever** — a state machine, not a code path.

The job lease is *not* the obstacle it looks like: `corpus-extraction` holds
a 300 s lease (`app/worker/kinds.py:246`) renewed by a heartbeat every 100 s
(`app/worker/runtime.py:430-441`), and the lease is deliberately a *liveness*
ceiling rather than a duration ceiling — a handler may run for hours as long
as its process lives. Two other things are:

- `extraction.timeout_s`, default **3600 s**, admin-editable to a maximum of
  **86,400 s** (`app/api/admin.py:521-522`). That ceiling is exactly the
  Batch API's 24-hour expiry, so a blocking poll loop has no margin at all.
- The extraction lane runs **one slot by default**, and is configurable to
  1–8 (`_extraction_concurrency()`, `app/worker/runtime.py`, resolved from
  `AGNES_EXTRACTION_CONCURRENCY` over `extraction.concurrency` and clamped;
  a worker restart is required to change it). A handler parked on a poll
  loop holds one slot for the whole batch window, so at the default it
  blocks every *other* connection's crawl for up to 24 hours — and at a
  configured N it takes out 1/N of the fleet's extraction capacity for the
  same period, which is a smaller version of the same problem rather than a
  different one. Submit-and-return is therefore not an optimization here;
  it is mandatory. Raising the slot count is not an alternative to it.

**(c) Per-document idempotency needs one new state.** `is_up_to_date` and the
content-hash keying are otherwise untouched — the plan walk computes them
identically. But the state entry must be written *after collection*, not
after submission, and a document must never be re-submitted while a batch
containing it is in flight or the run pays for it twice. That means a third
status beside `done` / `skipped-*`: `in-batch`, carrying the batch id. One
new invariant; a real one.

**(d) Nothing ships incrementally.** `batches.results()` streams, but only
after the batch has **ended** — no partial delivery while it processes. The
25-document flush in `_BatchShipper` still works; every document simply
lands at the end. Durability actually *improves*, provided the batch id is
persisted: a crash mid-collect re-reads results that stay available 29 days,
whereas today a crash loses the in-flight calls outright.

**(e) The operator loses live progress — but there is almost none to lose.**
This is the one place batching costs less than it looks. `_RunRecorder.
checkpoint()` is only ever called from the crawl loop and always writes
`phase="crawl"` (`connectors/sharepoint/crawler.py:510-513`);
`facts_extraction.py` never touches `extraction_runs` at all. The facts pass
runs *after* the crawl's last checkpoint, so for its entire duration
`checkpoint_at` is frozen — and `_derived_outcome` re-labels a `running` row
as **`stalled`** once `checkpoint_at` is older than `_STALL_AFTER_S = 1800`
(`app/api/admin_extraction.py:101,182-214`). **A facts pass longer than 30
minutes already renders in the admin card as stalled while it is working
normally.** Under batching the only signal would be `batch.request_counts`
(processing / succeeded / errored) — which is strictly more than an operator
gets today. Either way the report needs a "submitted, awaiting results"
state, and that is the observability surface's problem, not this module's.

**(f) Caching and batching partly conflict** — cache hits inside a concurrent
batch are best-effort. Moot today (§6 — nothing caches at all), but it means
the two levers do not simply multiply.

### 5.3 Where the crossover is

Not at a dollar figure. **At the point where the corpus stops fitting in one
run's wall clock** — because that is when the resumable state machine (5.2b)
has to be built regardless, and once it exists, batching is nearly free to
add on top.

Rough sizing from this run: 347 calls at concurrency 3 is on the order of
30 seconds of wall clock per document. Extrapolating:

| corpus | in-process, concurrency 3 | in-process, concurrency 16 | batch saving/pass |
|---:|---|---|---:|
| 187 docs | ~1.6 h | ~20 min | ~$5 |
| ~1,900 docs | ~16 h | ~3 h | ~$51 |
| ~19,000 docs | ~6.6 days | ~30 h | ~$507 |

Put that against `extraction.timeout_s`, whose **default is 3600 s** and whose
admin-editable ceiling is 86,400 s: at the shipped default, even this
187-document pass is already at the edge of one run's budget at concurrency
3, and the pass survives only because it is resumable across runs via the
per-document state file. So the wall-clock pressure arrives far earlier than
the dollar pressure — which is an argument for raising `concurrency` and for
fixing the deadline defect in §9 long before it is an argument for batching.

**Recommendation: not worth it at this corpus size. Do not build it now.**
At ~10× it is still the wrong thing to build *before* §2, which costs far
less and yields more. At ~100× the in-process pass no longer completes in
any sane run window, the state machine becomes mandatory, and batching
should be adopted in that same change. Note also that §2 and §3 shrink the
bill that batching then halves — sequencing them first makes the batch lever
smaller, which is an argument for the sequence, not against it.

---

## 6. The settled item, re-checked — and the threshold claim is wrong

The premise was: caching is configured correctly but never engages because
the rendered system prompt is 6,779 chars ≈ 1,883 tokens and the model's
minimum cacheable length is 2,048 — "~165 tokens under".

**The conclusion holds; the number does not.** Verified against the Anthropic
prompt-caching reference: the minimum cacheable prefix is model-dependent and
**not monotonic across generations** — 512 tokens on the newest models, 1,024
on several mid-generation ones, 2,048 on a third group, and **4,096 on the
Haiku 4.5 tier this stage defaults to.** 2,048 is a real threshold, but for a
different set of models.

So the shortfall is **~2,213 tokens, not ~165** — the prefix is less than half
the length it needs to be. Two independent renderings confirm the diagnosis
is robust to the exact token estimate: the reported live figure is ~1,883
tokens, and rendering the shipped default prompt against the repo's own
22-type ontology fixture (`tests/fixtures/eval/ontology.yaml`, translated
through `scripts/ontology/import_ontology.py`) gives 8,838 characters ≈ 2,450
tokens. **Both are below 4,096**, so both cache counters reading exactly zero
is fully explained either way.

What it is worth, and why "nearly free" is the wrong description:

- The system prefix is billed on every call: 347 × 1,883 = **653k tokens,
  13.9% of input, $0.65**.
- If it were cacheable as-is: **saves $0.59 — 5.8% of the bill.**
- Grown past 4,096 and cached: **saves $0.50 — 5.0% of the bill** (the bigger
  prefix costs 0.1× on every read, which eats part of the win).

Getting there means more than doubling the system prompt — with content, not
padding. The natural candidate does double duty: **worked examples**. Two or
three fully-formed NODES/EDGES blocks showing a correct quote, a correct id
slug, and a correctly-dropped co-occurrence would add roughly the needed
length *and* attack the first-attempt failure rate (§2), which is worth 6×
more. Padding for its own sake is not worth doing.

### 6.1 A second, larger caching finding the premise missed

Caching is currently applied to the **system block only**
(`facts_extraction.py:639`). The retry re-sends system + the entire base user
message, byte-identical to the first request's prefix. **A second cache
breakpoint at the end of the base user message would make the retry's input a
cache read.**

| | tokens |
|---|---:|
| base prefix billed today (187 writes + 160 full re-sends) | 4,647,718 |
| with a user-turn breakpoint (187 writes @1.25×, 160 reads @0.1×) | 3,345,152 |
| **saving** | **$1.30 — 12.8% of the bill** |

Twice the whole system-prompt caching win, with **no prompt change and no
format change** — one extra `cache_control` marker (the request uses 1 of its
4 allowed breakpoints). The combined prefix is ~13.4k tokens, far past 4,096,
so unlike the system block it caches on this model today. The retry follows
its first call within seconds, well inside the 5-minute TTL.

**But it is contingent on the retry rate.** The 1.25× write is paid on every
document; the 0.1× read is only collected on retried ones. Break-even is at a
**27.8%** retry rate:

| retry rate | net |
|---:|---:|
| 85.6% (today) | +$1.30 |
| 50% | +$0.50 |
| 30% | +$0.05 |
| 15% | −$0.29 |

So this is worth doing **now**, and worth **re-measuring immediately after
§2 lands** — if the repair pass drives the retry rate below ~28%, this
breakpoint turns into a net loss and should come back out. Two levers that
must be measured together, not stacked blindly.

---

## 7. Recommendation, ordered by value per effort

| # | Change | Worth | Effort | Note |
|---|---|---:|---|---|
| 0 | **An A/B seam: a `doc_ids`-scoped trigger + render the cost already computed** (§7.1) | $0 directly | Small | Prerequisite for measuring 1, 6 and 7. Today a prompt change costs a full crawl to evaluate. |
| 1 | **Deterministic quote repair before the retry** (§2.2) | 29–46% of the bill, plus recall | Medium — one function, careful tests | Also fixes a silent recall loss. The only lever that touches the dominant cost. |
| 2 | **Second cache breakpoint at the end of the base user message** (§6.1) | 12.8% | Trivial — one marker | Do first (it is free), re-measure after #1. Contingent on retry rate >27.8%. |
| 3 | **De-duplicate the chunk overlap** in what the model is sent (§4) | 4.8% | Small, but coupled to the boundary decision in #1 | Do in the same diff as #1. |
| 4 | **`stop_reason == "max_tokens"` counter** (§3.1) | $0 | Trivial | Not cost — closes a silent-truncation hole. |
| 5 | **Drop `doc_id` from evidence** (§3) | ~11.6% of output ≈ 5.9% of bill | Trivial — one prompt line, one injection | Safe standalone. |
| 6 | **Worked examples in the prompt** (§6) | up to 5.0%, plus retry-rate effect | Medium — prompt work needs A/B on a live pass | Only worth it if it clears 4,096 tokens *and* helps #1. |
| 7 | **Replace JSONL with a delimited format** (§3) | 20–52% of output, quote-length dependent | Large — new parser, new retry listing, parse-rate risk | Re-measure after #1; do not do it on today's baseline. |
| 8 | **Batch API** (§5) | 50% of whatever remains | Large — a resumable state machine | **Not now.** Adopt when the corpus outgrows one run's wall clock (~10k+ documents). |
| 9 | **Correct the cost note** in `config/instance.yaml.example` (§1.1) | honesty | Trivial | Two shipped sentences are known-false. |

Items 1–5 are additive and plausibly take the run from $0.054/document to
somewhere near $0.025–0.030 without touching the model, the ontology, or a
single fact.

### 7.1 Prerequisite — none of this can be measured today

Every item above says "measure it". Right now you cannot, cheaply:

- **There is no way to run fact extraction without a full crawl.** The only
  production caller of `run_facts_extraction` is the crawl itself
  (`connectors/sharepoint/crawler.py:2932`). No admin route, no CLI command,
  no script reaches it. Testing a prompt or format change means paying the
  entire crawl → convert → ingest pass first.
- **The documented cheap path does not exist as a surface.** The `doc_ids`
  parameter (`facts_extraction.py:1177`) is documented as "the single-document
  path an operator uses to test a prompt change without re-running a corpus"
  and has **zero non-test callers** — it is reachable only from a Python REPL
  on the box.
- ~~**The cost is computed and then thrown away in the UI.**~~ **Already
  shipped** — this investigation was written against a stale reading of the
  template. `facts_usage` carries `estimated_cost_usd` via
  `src.llm_pricing.cost_usd` (`facts_extraction.py`), is persisted into
  `extraction_runs.usage`, and the admin run line **renders it**
  (`app/web/templates/admin_data_sources.html`, `c0e2fabc4`, 2026-09-01):
  `· not priced` is the fallback for a stage that carries no such field, not
  the display for one that does. Nothing to do here; the A/B seam below needs
  only the `doc_ids`-scoped trigger.
- **An interrupted run loses its facts spend entirely.** On the crash/stop
  path `stopped_usage` carries only `ner` and `ocr`
  (`connectors/sharepoint/crawler.py:2960-2975`) — a facts pass that dies
  mid-way contributes no usage to the row at all, so the money is spent and
  unrecorded.

**A small A/B seam — a `doc_ids`-scoped trigger, the cost display already
being live — is arguably item 0.** It is not a cost lever itself,
but it is what turns items 1, 6 and 7 from guesses into measurements, and it
is the difference between one cheap instrumented pass (§8) and another full
corpus run.

---

## 8. What I could not determine

- **The retry's output volume relative to the first attempt.** This sets
  whether the retry is 29% or 46% of the bill. The run report records
  `facts_retries` (a document count) but no per-call token split, so the
  three-row bracket in §2 is the honest answer. One counter — retry output
  tokens, separately from first-attempt output tokens — closes it.
- **The split between transcription-artifact failures and genuine
  fabrications.** This decides whether §2.2 removes most of the retries or a
  quarter of them, and it is the highest-value unknown here. Settled by one
  instrumented pass at no extra model cost.
- **Average quote length and facts per document** (`nodes_emitted` /
  `edges_emitted` are in the report; I did not have the report itself). These
  set §3's saving anywhere between 20% and 52% of output. The §3 sensitivity
  table brackets it; the report resolves it exactly.
- **Exact token counts.** No API credential is available in this environment,
  so `messages.count_tokens` could not be run; token figures are derived from
  the run's own arithmetic and from the calibration implied by the reported
  6,779 chars ≈ 1,883 tokens. Every conclusion here is robust to that ratio —
  in particular §6's, since both plausible renderings sit below 4,096.
- **The live instance's actual ontology.** Measurements used the repo's own
  22-type fixture, which renders 4,686 characters of ontology against the
  live prompt's implied ~2,600. A larger ontology moves §6 closer to the
  threshold and inflates every call's prefix.
- **Wall clock per document.** §5.3's crossover table is extrapolated from
  concurrency and call count, not from a measured duration. The per-document
  `seconds` field in the state file would replace the estimate.

---

## 9. Adjacent defect found while reading (not a cost lever)

`connectors/sharepoint/facts_extraction.py:1448`:

```python
if deadline is not None and getattr(deadline, "expired", False):
```

`_Deadline.expired` is a **method** (`crawler.py:1565`), so this `getattr`
returns a bound method — always truthy. Verified at runtime: a fresh
`_Deadline(3600)` reports `expired() -> False` while
`bool(getattr(d, "expired", False))` is `True`.

The chained production path is the only caller
(`crawler.py:2932`, `maybe_run_facts_extraction(connection, deadline=deadline)`)
and it always passes a `_Deadline`. As written, that pass breaks out of the
plan loop **before submitting its first document** and reports
`interrupted: "timeout"` with `docs_extracted: 0`. No test covers it —
`expired` does not appear anywhere in `tests/test_facts_extraction.py`.

The fix is one character class (`deadline.expired()`, or make `expired` a
property) plus a test. Deliberately not made here — this document changes no
code. It is noted because §5.2(b) proposes rebuilding this exact deadline
handling, and that work should start from a version that runs.
