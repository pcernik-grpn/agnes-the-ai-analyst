"""Measurement harness for the fact-graph evaluation (workbook v0.2, EQ0/EQ3/EQ9).

See `docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md`
section 14 for the workbook contract this package encodes, and section 15.3
for the acceptance tests (EQ0, EQ3, EQ9) it exists to satisfy.

Modules:
    prompts   -- loads the frozen ten-prompt set (sha256-pinned, refuses a
                 locally-edited copy).
    records   -- the machine-readable run record ("the run is the artifact")
                 and its on-disk layout under `runs/<round>/<arm>/`.
    arms      -- one executor per workbook arm (A0 API, A4 Agnes, A1/A2/A3
                 manual-transcript import).
    run_eval  -- the CLI driver: `run` executes a round from a YAML config;
                 `import-transcript` ingests an operator-pasted manual run.
    grade     -- blind grading sheets, composite scoring, the governance
                 gate, mean/spread, and LLM-assisted grading.
    decision  -- the five pre-registered decision thresholds (workbook
                 Decision sheet, spec Sec. 14.3) computed from graded rows.
    metrics   -- EQ3 (precision/recall per fact type) and EQ9 (entity-
                 resolution cluster purity, conflict rate, orphan rate)
                 against the facts REST API.
"""
