# Entity resolution

Adapted from the producer pipeline's two-pass design (`skills/kg-builder-agent.md`,
`skills/reconcile-agent.md`, `docs/agent-passes.md`): extraction is
per-document and stateless on purpose, so cross-document identity is
explicitly a SEPARATE pass's job, run afterward over the whole graph. Mixing
the two jobs into one agent is the mistake this split avoids -- a
per-document agent that also tries to resolve identity across documents it
has never seen produces silent, unauditable merges.

## Pass 1 -- extraction: never guess-merge

Reading one document at a time, the extraction agent:

- assigns deterministic slug ids (`type:kebab-case-name`) so the same
  real-world thing produces the same id when the document makes the
  identity explicit;
- **never merges on its own initiative when identity is not explicit.** If
  one mention *might* be the same entity as another but the document does
  not say so, it emits a separate node plus a `possible_duplicate_of` edge
  carrying its reasoning -- a human resolves it, the agent never does;
- omits a fact rather than hedging it: no "probably", no confidence scores.
  A fact graph is evidence-backed facts or nothing;
- requires the quote to STATE the fact, not merely mention the entities
  near each other. A document naming a PE firm somewhere near a company
  name does not by itself establish ownership; a person's name appearing
  near a tool's name does not by itself establish that the person has that
  skill. When the strongest available quote only shows co-occurrence, the
  entities are still emitted but the relationship is dropped -- less graph
  is the correct choice over an invented edge.

## Pass 2 -- reconciliation: judgment over the whole graph

The reconciliation pass runs after extraction, over candidate groups that
deterministic screening (slug similarity, same-type near-duplicate names,
same-client engagement variants) flagged as possibly the same real-world
thing. For each group it returns exactly one of three decisions:

- **merge** -- the nodes denote the same real-world entity; identity is
  clear beyond reasonable doubt from the evidence, names, and graph context
  (e.g. name variants of one company, or the same engagement seen from
  documents with and without its SOW date). The canonical id preference
  order: the deterministic converter's id over an agent-invented one; the id
  carrying the SOW date; the fuller name in the attributes.
- **split** -- plausibly the same thing, but identity is NOT established by
  the evidence. Both nodes stay, connected by a `possible_duplicate_of`
  edge, for a human to decide. **When in doubt between merge and split,
  split** -- an under-merge is visible and repairable; an over-merge is
  silent.
- **distinct** -- screening was wrong; these are different things, with a
  stated reason.

## Conflicts are not an identity question

If two sources assert incompatible facts about the same entity (one
document says a client has one sponsor, another says a different one), that
is never resolved by merging or by picking a side. Both facts stay, each
with its own evidence, and the disagreement is surfaced for a human review
-- resolving it silently would turn a real disagreement into a wrong answer
that looks confident.

## Never merge on name similarity alone

Context must corroborate an identity call: a shared client, a shared
engagement, shared documents, or compatible attributes. Contradictory
attributes between two candidates that are each independently evidenced
(different SOW dates, different sponsors) forbid a merge outright -- that
is either a split or a conflict, never a merge.
