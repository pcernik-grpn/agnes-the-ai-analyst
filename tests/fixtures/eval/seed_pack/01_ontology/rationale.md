# Ontology rationale

Machine-readable source of truth: `ontology.yaml` in this same directory
(Cuesta Star knowledge-graph ontology, TCRD-185, v0.2.0, draft-for-agreement,
2026-08-26). This file explains *why* each type is there -- adapted from the
producer repo's own `docs/ONTOLOGY.md`.

## Design rule

The ontology is derived backwards from the evaluation prompts (Shan Wang's
scoring workbook v0.2), because those are the questions the system is graded
on. The two graph-decider prompts are G1 (which sponsor drove the most
engagements, plus a de-duplicated list of every industry touched -- an
aggregation) and G2 (is one company the same real-world thing as another --
entity resolution). Every type below earns its place by being load-bearing
for at least one graded prompt; nothing else got in.

## The shape

```
person --worked_on--> engagement --for_client--> client --owned_by--> sponsor
  |                      |   |   |                   |
  |                      |   |   +-of_type--> service_offering
  |                      |   +-------used_tool--> tool <--uses_technology-- client
  |                      |                              +-in_industry--> industry
  |                      +--has_finding--> finding
  +--has_skill--> skill                      ^
                                              |
document --part_of--> engagement    (everything) --evidenced_by--> document
```

Evidence is not optional: every edge carries a list of (document, verbatim
quote) pairs. This is a direct lesson from an early prototype run where the
extraction agent paraphrased quotes instead of copying them verbatim, which
the ontology's own author describes as "potentially skewing and compounding
errors when accessing via the relation chain." Verbatim-or-nothing.

## Why each type exists (not obvious from the name alone)

- **sponsor** -- not in the originating ticket's sketch, but required
  because two graded prompts ask for the sponsor directly ("sponsors worked
  with", "tell me the sponsor") and that is not derivable from `client`
  alone.
- **service_offering** -- added because one prompt filters on it ("sell-side
  IT due diligence"), an ambiguity-handling prompt cannot disambiguate
  without it, and an RFP-drafting prompt needs to assemble a proposal of one
  offering type.
- **uses_technology** (client -> tool) -- added in v0.2, found by writing the
  eval questions: a precedent-matching prompt needed "which clients run
  [some tool]", and the existing `used_tool` edge only covers delivery
  tooling used *on* an engagement, not the client's own technology stack.
  This is the concrete illustration of the ontology's own change policy:
  attribute additions are free, but this was a new edge TYPE, so it bumped
  the minor version and is scoped for re-extraction of already-processed
  documents.
- **document** is the citation target: every fact traces back to one, and
  answers hand the original back via its source path. A ticket-sketch
  "deliverable" is a `document` with `doc_type: deliverable` -- one type, not
  two.
- **finding** covers "outcome" from the originating sketch, but typed
  (headline / takeaway / lesson / client_feedback / risk) because different
  graded prompts need different subsets -- a headline finding, a set of
  takeaways, or a client's own words verbatim.

One deliberate deviation from the originating ticket's sketch: it draws
`skill --evidenced_by--> document` directly, but a `skill` node is *shared*
across people (e.g. many people may have "NetSuite" as a skill) -- the proof
that *this particular person* has the skill belongs on the `has_skill` edge,
not on the skill node itself.

## Why the type set freezes

Re-passes over the corpus, not corpus size, are the cost driver: a full-site
re-index is a full LLM pass over every document. Attribute additions are
free (picked up on the next document that happens to be processed); a new
node or edge TYPE means every document must be read again. So the type set
is agreed and frozen before the first full run, and confirmed again before
any A3/A4 evaluation round that depends on it.
