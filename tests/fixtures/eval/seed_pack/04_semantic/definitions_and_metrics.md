# Definitions and metrics

Plain-English definitions of the ontology's entity and relationship types
(from `ontology.yaml`'s own `description` fields), plus how the graded
evaluation prompts turn into a concrete traversal of this vocabulary --
adapted from the producer repo's `docs/ONTOLOGY.md` "proof" table. This is
the glossary a person (or an LLM given only this pack, per arm A3) needs to
answer the same questions a graph traversal would answer structurally.

## Entity definitions

- **person** -- anyone named in the corpus: staff on either side of an
  engagement, or a sponsor contact.
- **engagement** -- one unit of delivered or contracted work (a statement of
  work, a project). Carries dates, price, status, and a short summary.
- **client** -- the company the work was performed for.
- **sponsor** -- the private-equity firm or corporate parent that sourced or
  owns the client. Not always named directly next to the client -- it is
  frequently a separate fact that must be joined in.
- **industry** -- a coarse sector label (e.g. "manufacturing"), kept
  deliberately imprecise because disambiguation questions need groupings,
  not fine-grained precision.
- **service_offering** -- the kind of work performed (e.g. a named
  assessment or roadmap product), used to filter engagements by type and to
  disambiguate an otherwise-vague question about "our work in X".
- **skill** -- a capability a person demonstrably has (a tool, a domain, or
  a method). The proof that a specific person has a specific skill lives on
  the `has_skill` edge, not on the skill node itself, because a skill node
  is shared across every person who has it.
- **tool** -- a named technology, either used to deliver an engagement or
  run by a client as part of its own stack (see the two edge types below).
- **document** -- a file in the corpus; the citation target every fact
  traces back to.
- **finding** -- a stated outcome tied to an engagement: a headline result,
  a takeaway, a lesson learned, a client's own feedback (verbatim, in their
  words), or a risk.

## Relationship definitions

- `worked_on` (person -> engagement) -- who worked on what, with a role and
  a period.
- `for_client` (engagement -> client) -- which client an engagement was for.
- `owned_by` (client -> sponsor) -- which sponsor owns/sourced a client.
- `of_type` (engagement -> service_offering) -- which kind of work an
  engagement was.
- `in_industry` (client -> industry) -- which sector a client is in.
- `has_skill` (person -> skill) -- a demonstrated capability, evidenced on
  the edge itself.
- `used_tool` (engagement -> tool) -- delivery tooling used ON an
  engagement.
- `uses_technology` (client -> tool) -- the CLIENT's own technology stack,
  distinct from delivery tooling above.
- `has_finding` (engagement -> finding) -- an outcome tied to an engagement.
- `part_of` (document -> engagement) -- which engagement a document belongs
  to.
- `evidenced_by` (any -> document) -- the materialized citation chain: which
  document supports a given fact.
- `possible_duplicate_of` (any -> any) -- an entity-resolution escape hatch;
  never auto-merged, always a human review item (see
  `03_extraction/entity_resolution.md`).

## How the graded questions turn into a traversal

These are the concrete shapes the two graph-decider evaluation prompts
resolve to -- the same shape whether answered by a real graph traversal (A4)
or worked out by hand against this glossary (A3):

- **Aggregation question** ("which sponsor drove the most engagements, and
  what is the de-duplicated list of every industry touched?"): follow
  `person -> worked_on -> engagement -> for_client -> client -> owned_by ->
  sponsor`, group by sponsor and count engagements; separately follow
  `client -> in_industry -> industry` and de-duplicate. Filtering by date
  means constraining on `engagement.start_date`/`sow_date`; filtering to
  private-equity sponsors means constraining on `sponsor.type`.
- **Entity-resolution question** ("is company A the same real-world thing as
  company B?"): this is answered by the reconciliation pass's judgment
  (`03_extraction/entity_resolution.md`), not by a lookup -- the honest
  answer may be "yes, a parent with several operating subsidiaries", which
  is a structural relationship, not a yes/no fact.
- **Precedent-matching question** (closest prior engagement for a company of
  a given size, ownership, and industry): rank candidate engagements by
  `client.revenue_usd_estimate`, `client.ownership`, and industry proximity
  (via `industry.parent`, informally -- see
  `02_taxonomies/vocabularies.md`).
- **Verbatim-quote question** (client feedback "in their words"): the
  `finding.text` value where `finding_type = client_feedback` is required
  by the ontology itself to be the client's exact words, cited to the
  document it came from.
- **Ambiguity question** ("show me our work in X"): when `industry.parent`
  fans out to several child industries and service offerings, the honest
  answer names the ambiguity and asks which slice is meant, rather than
  guessing one.

Nothing here is a numeric financial formula -- this ontology's "metrics" are
counts, de-duplications, and rankings over typed, evidenced facts, not a
business-metrics layer. A structured/semantic-layer question (e.g.
utilization by business unit) is out of scope for this ontology by design;
it belongs to a separate structured data source, not the graph.
