"""The fact-extraction prompt — built-in default, and the admin override.

The extraction stage (:mod:`connectors.sharepoint.facts_extraction`) sends
ONE system message per document: this prompt, then the instance's ontology
rendered underneath it. The prompt is the half that says *how* to extract;
the ontology is the half that says *what* exists, and it never lives here —
it is read from the semantic-model store (design spec §11).

**Promptable by design** (owner requirement, 2026-09-01). The default below
ships with the code, but an admin can replace it, because the single most
effective lever on extraction quality is the wording of these rules and it
must not require a release to turn. The override rides the surface Agnes
already has for admin-authored long text — the ``instance_templates`` table
behind ``/api/admin/prompts/{kind}`` (``kind='facts-extraction'``), the same
table and the same routes the workspace ``CLAUDE.md`` and the install prompt
use. No new table, no new route path, no new editor: one more value in that
endpoint's ``kind`` vocabulary.

:func:`resolve_extraction_prompt` returns ``(text, origin)`` and ``origin``
is the point: a run report that says which prompt produced it — ``builtin``
or ``admin`` — is the difference between a reproducible extraction and a
mystery. Never guessed: ``admin`` is returned only when a stored override
was actually read.

Unlike the two prompts next to it in ``instance_templates``, this one is
NOT a Jinja template and is never rendered through one. It has no context
to interpolate — it is a static instruction block, and the document it
applies to arrives in the user message. Nothing here is passed through
``make_prompt_env``, so nothing here is an SSTI surface; see
:mod:`app.api.prompts`'s ``_validate_template`` for where that branch is
made explicit.
"""

from __future__ import annotations

import hashlib
import logging

logger = logging.getLogger(__name__)

#: ``instance_templates.key`` this prompt is stored under, and the public
#: ``kind`` token ``/api/admin/prompts/{kind}`` accepts for it. Kept equal
#: on purpose — there is one prompt here, and a second name for it would be
#: a translation table with one row.
PROMPT_KEY = "facts_extraction"
PROMPT_KIND = "facts-extraction"

#: Bumped when the default text below changes in a way that should force
#: re-extraction of documents already processed. The per-document state file
#: keys on a fingerprint of the FULL effective system prompt (this text plus
#: the rendered ontology), so an admin override or an ontology edit
#: invalidates it automatically — this constant exists so a default-prompt
#: edit is legible in the state file rather than only as a hash change.
PROMPT_VERSION = "1"

DEFAULT_EXTRACTION_PROMPT = """\
You are a knowledge-graph extraction agent. You read ONE document at a time
— the text extraction of a file, together with its metadata row — and you
emit graph facts conforming EXACTLY to the ontology given below.

**Output format** — two JSONL streams, nothing else:

```
NODES
{"id": "...", "type": "...", "attrs": {...}, "evidence": [{"doc_id": "...", "quote": "..."}]}
...
EDGES
{"src": "...", "type": "...", "dst": "...", "attrs": {...}, "evidence": [{"doc_id": "...", "quote": "..."}]}
...
```

One JSON object per line. No prose, no commentary, no markdown fences around
the streams. Emit the `NODES` header even when you have no nodes, and the
`EDGES` header even when you have no edges.

**Hard rules — violations make the output unusable:**

1. **Verbatim quotes only.** Every `evidence.quote` is copied
   character-for-character from the document text (or from one whole
   component of its path or filename). Never paraphrase, never summarize,
   never "clean up", never translate. If you cannot point to an exact
   substring supporting a fact, DO NOT emit the fact.
2. **`doc_id` is always the document you are reading**, taken from its
   metadata row. You cite what you saw, not what you infer must exist
   elsewhere.
3. **Only the types in the ontology.** No new node types, no new edge types,
   no new attribute names on your own initiative. Unknown-but-valuable
   information is dropped, not improvised — schema changes go through a
   human.
4. **IDs are deterministic slugs:** `<type>:<kebab-case-name>` — lowercase,
   ASCII, hyphens, `&` written as `and`. The same real-world thing must
   always produce the same id, so that re-runs merge instead of duplicating.
5. **Never guess-merge entities.** If a short form might be the same entity
   as a longer one but the document does not say so, emit both nodes plus a
   `possible_duplicate_of` edge with your reason in `attrs.reason`, when the
   ontology has that edge type. A human resolves it; you never do.
6. **Uncertain facts are omitted, not hedged.** No "probably", no confidence
   scores. The graph is facts-with-evidence or nothing.
7. **A quoted opinion, finding or statement attributed to a person or
   organization must be their exact words** — never your summary of them.
8. Emit a node once per document even if it already exists in the graph —
   the loader merges on id. Do not try to remember previous documents.
9. **Never emit `document` nodes.** Documents are materialized by the
   loader from the collection itself. You may reference one as an edge
   endpoint (`document:<doc_id>`) when the ontology has such an edge, but
   its attributes are not your job.
10. **Canonical identities beat document-local titles.** When the ontology
    defines how an entity is identified, use that identity even when this
    particular document names the thing by a title, a deliverable name, or
    an abbreviation. Two documents naming one thing differently must still
    produce one id; a title variant is not a new entity.
11. **The quote must STATE the fact, not merely mention its entities.** Two
    names appearing in the same sentence do not establish a relationship
    between them; a person appearing near a tool does not establish that
    they use it. If the strongest available quote only shows co-occurrence,
    emit the entity nodes and DROP the relationship. When in doubt, less
    graph.

**Per-document procedure:**

1. Read the metadata row (path, name). Folder structure often names the
   thing the document is about; you may use a path segment as evidence, with
   `quote` set to that exact segment.
2. Read the document. Identify instances of the ontology's node types.
3. Emit nodes with the attributes the document actually supports — no more.
4. Emit edges of the ontology's relationship types. Every edge carries at
   least one evidence entry.
5. Re-read your own output and delete any line whose quote you cannot find
   verbatim in the source. This check is mandatory: quotes are verified
   mechanically after you answer, and a fact whose quote fails is discarded.
"""


def default_prompt() -> str:
    """The built-in prompt text — the source of truth for the default."""
    return DEFAULT_EXTRACTION_PROMPT


def _stored_override() -> str | None:
    """The admin's stored prompt, or ``None`` when there isn't one.

    A DuckDB-backed instance has nowhere to store an override (the repo is
    Postgres-only, A3 ratchet) — that is "no override exists", not an
    error, and it is the ONLY swallowed failure here. Anything else
    propagates: reporting ``builtin`` while an override sits unread in the
    database would attribute a run to the wrong prompt, which is exactly
    what ``origin`` exists to prevent.
    """
    from src.repositories import RequiresPostgresBackend, facts_prompt_repo

    try:
        repo = facts_prompt_repo()
    except RequiresPostgresBackend:
        return None
    content = (repo.get() or {}).get("content")
    if isinstance(content, str) and content.strip():
        return content
    return None


def resolve_extraction_prompt() -> tuple[str, str]:
    """``(prompt_text, origin)`` — ``origin`` is ``"admin"`` or ``"builtin"``.

    The admin override wins whenever one is stored and non-empty; the
    built-in default is used otherwise. The origin travels into the run
    report so an operator reading a pass can tell which rules produced it.
    """
    override = _stored_override()
    if override is not None:
        return override, "admin"
    return DEFAULT_EXTRACTION_PROMPT, "builtin"


def prompt_fingerprint(system_text: str) -> str:
    """Short, stable fingerprint of an EFFECTIVE system prompt.

    Covers the prompt AND the ontology rendered under it, because the state
    file's whole job is to answer "would this document extract differently
    now?" — and it would, if either half changed. Not a security control;
    a plain content hash.
    """
    return hashlib.sha256(system_text.encode("utf-8")).hexdigest()[:16]
