# Head-to-head R1 — operator runbook

**Date:** 2026-08-31
**Status:** runbook, not a result — no R1 round has been executed yet.
**Anchor:** TCRD-187 "Head-to-head eval: G1/G2 + full prompt set per workbook v0.2".
**Harness:** `scripts/eval/` (verified green against `0.94.0`, HEAD `fd6c13d`).
**Predecessor:** [`2026-08-28-run-p-planted.md`](2026-08-28-run-p-planted.md) — the
planted proving run. Run P established that the substrate is sound (S-probes,
id stability, edge recall/precision). It is **not** a head-to-head and produces
none of the five decision thresholds.

## 0. The constraint that shapes the whole round

Read this before scheduling anything.

The decision sheet has five pre-registered thresholds
(`tests/fixtures/eval/workbook_v0_2/thresholds.yaml`, frozen 2026-08-27). The
harness can drive **two** of the five arms headlessly:

| Arm | What it is | Driven by |
|---|---|---|
| A0 | bare model, no tools — the hallucination floor | `run_eval.py run` (Messages API) |
| A1 | model + enterprise file-store connector | **operator, by hand** |
| A2 | competing assistant + same connector | **operator, by hand** |
| A3 | model + seed pack (same context, no platform) | **operator, by hand** |
| A4 | Agnes, per persona | `run_eval.py run` (chat surface) |

A1/A2/A3 run inside external product UIs the harness cannot drive
(`scripts/eval/arms.py` module docstring). The operator runs each prompt there
and pastes the transcript back via `run_eval.py import-transcript`, which
normalizes it into the same `RunRecord` shape an API-driven arm produces.

**Consequence:** thresholds #1 (A4 vs A1/A2), #2 (A4 vs A3) and #4
(tokens vs *best baseline arm*) all depend on hand-run arms. A round of only
A0 + A4 returns overall verdict `incomplete` — by design, not by bug
(`scripts/eval/decision.py`, `Decision!F12`). Threshold #2 is labelled THE
DECIDING TEST in the frozen sheet, and it is one of the manual ones.

So: **either budget the manual transcript work, or accept that R1 answers
nothing the program was built to answer.** This is the scheduling decision
TCRD-187 needs made before it can start, and it is why the ticket cannot be
picked up as a one-afternoon task.

## 1. Run volume

Ten frozen prompts (`X1 P1 P2 T1 T2 A1 L1 N1 G1 G2`), three runs each —
variance is a result, not noise (`Framework!B30`); runs are sequential, never
deduped or cached.

| Arm | Runs | How |
|---|---|---|
| A0 | 10 × 3 = **30** | automated |
| A4 | 10 × 3 × 2 personas = **60** | automated |
| A1, A2, A3 | 10 × 3 × 3 = **90** | **manual transcripts** |

The 90 manual runs are the schedule. Split them across operators by arm, not by
prompt, so one person stays in one product UI.

G1 and G2 are the two prompts the design predicts Agnes wins (relational /
entity-resolution questions); simple lookups may legitimately go to the
baselines. Do not drop the other eight — the composite is what the thresholds
compare, and cherry-picking the two favourable prompts invalidates the round.

## 2. Preconditions

1. **A live instance** with the fact flag on, the corpus ingested, and the
   ontology imported — the Run P setup, re-pointed at the head-to-head corpus.
2. **Persona accounts and tokens.** Two personas, distinct group membership, so
   the access rows (AC1–AC3) mean something. The run-config stores only the env
   var *name* holding each token, never the value.
3. **An eval agent slug** reachable at `POST /api/v1/agents/{slug}/responses`.
4. **Thresholds untouched.** `thresholds.yaml` was frozen before results
   existed. Editing it after a round moves the bar to wherever the results
   landed — the one thing that makes the whole exercise worthless.

## 3. Execute

```bash
cp scripts/eval/example_run_config.yaml runs/r1.yaml
# edit: round: R1, base_url, agent_slug, persona token env var names

export ANTHROPIC_API_KEY=...
export AGNES_EVAL_TOKEN_PRINCIPAL=...
export AGNES_EVAL_TOKEN_ASSOCIATE=...

# automated arms
python -m scripts.eval.run_eval run --config runs/r1.yaml --arm A0
python -m scripts.eval.run_eval run --config runs/r1.yaml --arm A4

# each manual run, once per arm/prompt/run index
python -m scripts.eval.run_eval import-transcript \
    --round R1 --arm A3 --prompt G1 --run 1 \
    --transcript-file /path/to/transcript.txt \
    --input-tokens N --output-tokens N
```

Records land at `<output_dir>/<round>/<arm>/<prompt>_<run#>.json`. Capture token
counts for the manual arms as you go — threshold #4 is a token comparison and
cannot be reconstructed after the UI session is closed.

`--prompt` restricts to one prompt id, useful for re-running a single failed
transport attempt. A failed run is still an artifact: keep it.

## 4. Grade and decide

Grading is blind — `build_grading_sheet` assigns a nonce per row so the grader
cannot see which arm produced an answer. Score into the sheet, then
`ingest_scores` → `summarize_round` → `decision`.

Two workbook cell bugs are deliberately **not** reproduced by
`scripts/eval/decision.py` (see its module docstring): `Access!C18` counts a
column that never holds the value it tests and always evaluates to 0, and
`Decision!E10` compares the wrong quantity in the wrong direction. The module
implements the sheet's own row labels and the spec prose instead. If a result
is ever reconciled against the spreadsheet by hand, expect these two cells to
disagree — the module is right.

## 5. Reporting

Commit a run report next to this file, in Run P's shape: what ran in order,
per-step machine-readable artifacts, the five thresholds with Actual vs
threshold, and the overall verdict. Record `incomplete` honestly if the manual
arms did not happen — an `incomplete` round reported as a pass is worse than no
round at all.

The pre-registered sheet names an outcome nobody plans for: if thresholds #1
and #3 pass but #2 fails, the honest reading is that context engineering is the
value and the platform is optional. That is a legitimate, sellable finding, and
finding it here is far cheaper than finding it in month four. Report it as a
result, not as a failure to be re-run until it moves.
