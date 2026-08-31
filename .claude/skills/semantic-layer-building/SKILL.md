---
name: semantic-layer-building
description: Author and maintain an Agnes semantic-layer document (datasets, metrics, relationships, constraints, glossary) — modeling rules, the payload shapes, and how to keep a layer honest over time. Use when creating a new semantic model, adding or editing an object in one, or investigating why a layer looks stale/incomplete.
---

# Semantic-layer building

Agnes stores a project's business meaning as one whole Apache Ossie document
per semantic model — not a pile of independently-editable rows. Everything
in this skill assumes that: you read/write a *document*, then Agnes
validates and stores it as a unit.

The standing rule this skill exists to support — also carried in the
generated workspace `CLAUDE.md`'s "Semantic layer" section and in
`docs/semantic-layer.md`: **the semantic layer is the authoritative source of
business meaning — prefer it over inferring definitions from table/column
names, and reuse a declared metric's SQL rather than inventing a
calculation.**

## Before you touch a document

1. **Look at what already exists.** Don't guess whether a dataset or metric
   is already modeled:
   ```
   agnes semantic-model context dataset            # every dataset, compact
   agnes semantic-model context metric              # every metric, compact
   agnes semantic-model context metric --id revenue # one metric, full detail
   ```
   Explicit `--id` gives you the FULL object (every attribute); omitting it
   gives you every object of that type compactly (name + short summary) —
   start compact, then drill into the one you need.

2. **Read the schema before writing a new object**, don't remember it:
   ```
   agnes semantic-model schema dataset metric relationship
   ```
   This is served straight from the vendored, pinned Apache Ossie JSON
   Schema (`src/semantic/schema/osi-schema.json`, exposed via
   `src/semantic/document_validation.py::get_schema_defs`) — the exact
   contract your document will be validated against, not a paraphrase.

3. **Check for a canonical metric before inventing one.** `agnes catalog
   --metrics --show <category>/<name>` reads a metric this layer already
   projected into `metric_definitions`; adapt its SQL rather than writing a
   fresh calculation over the same data.

See `references/modeling-rules.md` for how to decide dataset vs. metric,
naming conventions, grain, and when something belongs in a constraint rather
than a description.

## The payload shapes

`references/payloads.md` is the interchange format reference — the exact
shape of a `dataset`, `metric`, `relationship`, and the Agnes-specific
constraint extension, with a worked example of each. Read it before hand-
authoring a document or editing one you exported:

```
agnes semantic-model export <slug> -o model.yaml   # any user with access
agnes semantic-model apply model.yaml              # any user: applied if you
                                                   # are an admin, otherwise
                                                   # queued for review
agnes admin semantic import model.yaml             # admin: direct import
```

## Validate before you save anything

Agnes never stores a half-valid document. Validate locally — no server, no
token — before importing:

```
agnes semantic-model validate <path-to-document.yaml>
```

This runs the same central `validate()` the server runs on import/edit
(`src.semantic.document_validation.validate_document`), against the same
vendored schema `agnes semantic-model schema` describes above. Fix every
reported error; a document that doesn't validate is refused, not stored
`status='invalid'` (that status is reserved for imports from a registered
source that later fails validation, not for authoring here).

Once a document is stored, sanity-check a query you intend to run against it
before running it:

```
agnes semantic-model validate-query "SELECT SUM(revenue) FROM orders"
```

This is best-effort text matching, not SQL parsing — see the command's own
output for what it can and cannot check.

## Maintaining a layer over time

A semantic layer rots the same way any other artifact does: objects go
stale, datasets stop binding to a registered table, imported metrics quietly
fail to land. `references/maintenance.md` covers the audit loop — what to
check, how often, and which commands surface drift against the table
registry.

## Scope note (read/author, not a bundled write tool)

This skill's actions ride the existing surfaces, all of them in the any-user
`agnes semantic-model` group: `search`/`show`/`export` to find and read a
document, `validate` to schema-check one offline, `context`/`schema`/
`validate-query` (the read-parity tools), and **`apply`** — the write path,
which is not admin-only: an admin's document goes live, anyone else's is
queued for admin moderation and the command says which happened. Only
`agnes admin semantic import` (direct import, bypassing moderation) and the
rest of `agnes admin semantic …` need an admin.

There is no bundled script here that mutates a document for you — validation
is always a call to the central `validate()` via the CLI above, never a local
re-implementation, so a document you hand-author is checked against the exact
same rules the server enforces.
