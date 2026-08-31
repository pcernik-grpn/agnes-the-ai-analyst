# Arm A3 seed pack

This directory is the "context engineering" pack for evaluation arm A3
(`docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md`,
Sec 14, Framework sheet: "the ontology/taxonomies/entity-resolution rules as
plain context" -- "THE CONTROL THAT MATTERS"). A3 is Claude plus the M365/
SharePoint connector plus this pack loaded as project context; A4 is Agnes
plus this pack plus the knowledge graph. Both arms use the SAME words on
purpose -- the whole point of the five-arm design is to isolate platform
value from context-engineering value, and that isolation only holds if the
context each arm reads is identical.

## Contents

- `01_ontology/` -- the producer's ontology.yaml (Northwind Star / TCRD-185,
  v0.2.0) plus a plain-language rationale for why each node/edge type
  exists.
- `02_taxonomies/` -- the closed vocabularies (enums) and canonicalization
  rules the ontology and the extraction skill define.
- `03_extraction/entity_resolution.md` -- how the same real-world thing
  seen from different documents is (and is not) merged into one entity.
- `04_semantic/definitions_and_metrics.md` -- plain-English definitions of
  the entity/relationship types, plus how the graded eval prompts turn into
  a concrete question against this vocabulary.

## Rule for the person running the eval

**Give this pack to the A3 arm VERBATIM.** Do not summarize it, do not
trim it, do not "clean it up" for the prompt window. A3 exists to answer
one question honestly -- does typed extraction over a knowledge graph (A4)
beat the SAME domain knowledge handed to Claude as plain context (A3) -- and
that comparison is void if A3's context differs from what steps 1-4 of the
fact-graph build actually encode. If the pack changes (a new node type, a
corrected taxonomy entry), re-copy it here and re-run A3 before comparing
against a new A4 result.

This pack was assembled per Sec 11 / Sec 16 step 1 of the fact-graph spec:
"the ontology/taxonomies/ER rules as plain context (~1 day to assemble)".
The packaging owner and the exact moment A3 first runs are open items (O4)
tracked in the spec, Sec 17.
