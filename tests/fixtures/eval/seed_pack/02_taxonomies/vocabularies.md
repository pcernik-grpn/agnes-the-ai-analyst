# Taxonomies and closed vocabularies

Derived from `ontology.yaml`'s `enum` constraints and from lessons recorded
in the extraction skill. These are the closed value sets an extraction
agent (or a person reading an answer) should recognize as the full set --
if a document seems to need a value outside one of these lists, that is a
signal to raise it as an ontology change, not to invent a new value quietly.

## Closed enums (from ontology.yaml)

- `engagement.status`: proposed, active, completed, unknown
- `sponsor.type`: private_equity, corporate_parent, other
- `skill.category`: tool, domain, method
- `document.doc_type`: sow, deliverable, notes, email, feedback, other
- `finding.finding_type`: headline, takeaway, lesson, client_feedback, risk

## Industry hierarchy -- open, not yet a real taxonomy

`industry.name` is deliberately kept coarse (e.g. "manufacturing"), and
`industry.parent` is an optional broader term on the same node type. As
written this is a plain string attribute, not a structural reference to
another `industry` node -- there is no controlled list of parent terms and
no guarantee two documents spell the same parent the same way. Treat any
`industry.parent` value as informal until a human confirms the intended
hierarchy (the ontology-import script that produced this pack's sibling
semantic model flags every `parent`-shaped attribute for exactly this
reason: a parent/child relationship expressed as a plain value is a
translation guess, not a verified structure).

## Canonicalization: the same real thing, named differently across documents

The extraction skill records one concrete, load-bearing rule: canonical
identities beat document-local titles. An engagement's identity should be
built from the client and the SOW date when both are knowable, even when
the document itself only gives a deliverable title -- a title describes the
work product, not the engagement's identity. The skill's own worked example:
a single service offering has been observed under at least three different
proposal-title spellings across different documents ("AI Value Backlog /
Execution Roadmap", "AI Opportunity Assessment + Roadmap", and the short
form "AI Value Backlog") -- all three name the SAME `service_offering` node,
not three different ones. Client name spelling also varies across materials
that are unambiguously about the same company (spacing and punctuation
differences); when a document is explicitly about one company, those
variants normalize to one client, not several.

This is a naming-canonicalization rule, not a merge-across-different-things
rule -- see `03_extraction/entity_resolution.md` for when two DIFFERENT
document mentions may or may not be the same real-world entity at all.
