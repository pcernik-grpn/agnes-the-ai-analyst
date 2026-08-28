# Workbook v0.2 fixtures

Content extracted verbatim from `eval_scoring_workbook_v0.2.xlsx` (owner Shan
Wang, **FROZEN 2026-08-27** — see the workbook's README sheet STATUS line:
"prompt set, weights, and decision thresholds locked ... Do not edit prompt
text, weights, or thresholds below without versioning as v0.3 and re-grading
R0 under the new version").

**Scope-note waiver.** This directory deliberately contains customer-specific
material (prompt text naming real client/engagement folders, PE sponsors,
personas) under the same 2026-08-27 owner decision recorded in
`docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md`'s
header scope note and repeated at its build-order step 5: "It lives in
`scripts/eval/` with fixtures (the frozen prompt strings, the planted ground
truth) under `tests/fixtures/eval/` — committed under the header's scope-note
waiver." If this repository ever returns to public distribution, this
directory must be scrubbed or moved to a private repo as a whole.

## Files

- `prompts.yaml` — the ten frozen prompts (Prompts sheet: id, category,
  service line, verbatim prompt text, what it tests, why baselines should
  struggle, traps to score against, key elements expected — the v0.2
  replacement for a literal gold answer, source folder reference, and
  build metadata).
- `frozen_manifest.json` — sha256 of `prompts.yaml` as committed.
  `scripts/eval/prompts.py::load_prompts` recomputes this hash at load
  time and refuses to run on a mismatch, so a locally-edited prompt string
  cannot silently drift from what was graded.
- `rubric.yaml` — the seven 0/1/2-scored dimensions (Rubric sheet): weight,
  the question each dimension asks, and the three anchor texts (2/1/0),
  plus the composite formula, the governance-gate override, and the
  worked example used to hand-verify `scripts/eval/grade.py`'s composite
  math.
- `thresholds.yaml` — the five pre-registered decision thresholds
  (Decision sheet) plus the Access sheet's two personas and three
  access-test rows (AC1–AC3) that feed threshold #5.
- `token_methods.md` — the per-arm token-counting methods (README sheet
  cell C24) plus the Framework sheet's token-visibility fallback table,
  for an operator running `import-transcript` to follow when supplying
  token counts for the manual arms (A1/A2/A3).

## What is NOT here

The A3 seed pack (ontology + taxonomies + entity-resolution rules + semantic
definitions) already exists under `tests/fixtures/eval/seed_pack/` and
`tests/fixtures/eval/ontology.yaml` from an earlier build step — not
duplicated here. The planted ground-truth manifest schema for EQ3/EQ9 lives
at `tests/fixtures/eval/ground_truth.schema.json`; conforming manifest
instances are emitted by a sibling task's corpus generator, not committed
here.
