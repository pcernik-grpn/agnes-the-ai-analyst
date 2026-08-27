---
name: ontology-building
description: Author the ontology a fact graph is extracted against — which entity types and relationship types exist, what is an attribute instead, and how to test it on a sample before freezing. Use when setting up knowledge extraction over a document collection, adding a type to an existing ontology, or diagnosing why extraction misses things people expect it to find.
---

# Ontology building

An ontology is the contract between two things that must never disagree: what
the extraction pass is **allowed to produce**, and what the answering agent
knows how to **ask for**. One document drives both, so a type that is not in
the ontology can neither be extracted nor queried — it simply does not exist.

This is not the semantic layer. The semantic layer (`semantic-layer-building`)
describes **tables** so an agent writes correct SQL. An ontology describes
**the world** — the kinds of things your documents talk about. In Agnes they
meet: the ontology is stored *as* a semantic model, where entity types become
datasets and relationship types become relationships. Author the ontology
here; that skill covers the document mechanics.

## The one economic fact that shapes every decision

**Adding an attribute is free. Adding a type is not.**

A new attribute on an existing type is picked up on the next document that
happens to be processed — nothing is re-read. A new *entity or relationship
type* means the extraction pass never looked for it, so every document must
be read again: a full LLM pass over the whole corpus, priced per document.

Everything below follows from that asymmetry. Get the type set right while
the corpus is small and a mistake costs an afternoon; after the first full
run it costs the corpus.

## Starting from one you already have

Most ontologies are not written from nothing — there is a YAML, a schema, a
diagram, or a previous project's type list. Import it rather than retyping
it: a hand-copied type set drops things silently, and prose about a file you
are holding adds a transcription step for no gain.

Importing is a **translation**, and the interesting output is what fails to
translate. Expect three kinds of leftover, and handle each deliberately:

- **Rules with no field to live in.** "Every edge must carry evidence" is a
  constraint, not a type. It belongs in the model's `ai_context`, where the
  answering agent will actually read it.
- **Structures the target format expresses differently.** A parent/child
  hierarchy that was a plain attribute usually becomes a relationship from a
  type to itself. Confirm it rather than dropping it — hierarchies are what
  ambiguity questions ("show me our manufacturing work") traverse.
- **Types carried over out of habit.** An imported set is not automatically
  the right set. Run it against a sample before accepting it, and apply the
  same test as for a new type: does it appear in a question anyone asks?

An import fills the draft. It is not a save, and it is not an endorsement —
review it exactly as you would review a proposal.

## Order of work

1. **Load a real sample first.** Five to ten documents, chosen to be
   *different from each other* — one index/spreadsheet, one long prose
   document, one deck, one edge case (a scan, a template, something
   half-filled-in). Never author from memory of what the documents "probably"
   contain; the recurring nouns are rarely the ones people name in a meeting.
2. **Name the questions before the types.** Write down five questions the
   organization actually asks. Each type you propose must appear in at least
   one of them. A type nobody asks about is cost with no return.
3. **Draft types, then test on the sample.** Run the extraction nanečisto
   (dry) and read the output next to the source text. The valuable half is
   what *didn't* get captured — see below.
4. **Iterate on the sample, not the corpus.** Two or three rounds is normal.
5. **Freeze, then run.** Record the freeze — the date and the type set — so
   a later "can we just add X?" conversation starts from the real cost.

## Deciding: type, attribute, or nothing

Ask in this order.

**Is it asked about on its own?** "Which engagements did Kohlberg sponsor?"
means sponsor is a **type**. "How long did the Myers engagement run?" means
duration is an **attribute** of engagement — nobody asks "list all durations".

**Does it connect two things?** If it only makes sense as *A relates to B*,
it is a **relationship type**, not an entity. `worked_on` connects a person
and an engagement; it is not a thing.

**Would it have its own attributes?** A type usually carries more than a
name. If the only thing you would ever store is the name itself, it is
probably a value on something else. (Countervailing case: it is a type
anyway when many things must point at the *same* one and you need them to
join — an industry with only a name still earns typehood because it is how
you group clients.)

**Does it survive outside this document?** A client exists across hundreds of
documents; "the Q3 revision" exists in one. The first is a type, the second
is an attribute or nothing.

**Would you filter or group by it?** If yes, type. If it is only ever read
back as part of an answer, attribute.

### The three mistakes that cost the most

- **Types that are really attributes.** Symptom: the type has exactly one
  field, the name, and nothing ever points at it. Cost: bloated graph, slower
  extraction, no gain.
- **Types nobody asks about**, added because they seemed natural. Every one
  makes every future document more expensive to process.
- **Missing the type that carries the answer.** The expensive one, because it
  is only discovered after the full run — which is exactly what the sample
  pass exists to prevent.

## Relationship types

Keep them **few and directional**. Prefer `owned_by` (client → sponsor) over
a generic `related_to` with a role attribute: a specific type is queryable,
a generic one pushes the distinction into free text where no query reaches it.

Name them as the *verb from source to target*, so a path reads as a sentence:
`person —worked_on→ engagement —for_client→ client —owned_by→ sponsor`.

Do not model a relationship that a foreign key on the entity already
expresses, unless the relationship itself carries evidence or attributes —
which, in a fact graph, it usually does.

## Evidence is not optional

Every fact carries the document it came from and the **verbatim** sentence
that supports it. This is what separates a fact graph from a summary, and it
is why the graph can be trusted at all:

- a fact with no evidence is dropped, not stored;
- a quote that does not appear in the source document is dropped — the check
  is mechanical, not a model's opinion;
- the quote is stored **exactly**, never paraphrased or tidied.

When you add a type, state where its evidence comes from. If you cannot name
a sentence that would prove an instance of it, the type is an inference, not
a fact, and it does not belong in the ontology.

## Reading the sample output

Put the source sentence and the extracted facts side by side, and look at
three things in this order:

1. **What propagated wrongly** — a value in the wrong field, a client
   recorded as a sponsor. Usually a naming problem in the ontology: two types
   whose descriptions do not draw the line clearly enough.
2. **What was dropped** — the sentence held something and nothing came out.
   This is the cheapest signal you will ever get about a missing type or
   attribute. Fix it now.
3. **What was invented** — a fact whose quote does not support it. The
   verbatim gate should have caught it; if it did not, tighten the type's
   description rather than the prompt.

Do not grade on volume. More facts per document is not better; more
*answerable questions* is.

## Conflicts are a result, not a failure

Two documents will disagree — an index says one sponsor, a deck says another.
Correct behaviour is to keep **both**, each with its evidence, and route the
conflict to a human. An ontology that forces a single value per attribute
turns a disagreement into a silent wrong answer, which is worse than a
visible one.

## Freezing

Before the first full run, confirm:

- every type appears in at least one of the written questions;
- every type has a stated evidence source;
- the sample pass produced no drops you care about;
- someone who knows the domain has read the type list — not the JSON, the
  list of nouns and verbs;
- the cost of the full run is known and approved by whoever pays it.

After the freeze, attribute additions continue freely. A new type is a
re-extraction: price it, schedule it, and batch several together rather than
adding them one at a time.
