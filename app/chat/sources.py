"""The `sources` block: what an answer claims, checked against what it ran.

The product promises, on the first card of the onboarding tour and again in the
chat greeting, that Agnes "always" says where an answer came from. A prompt rule
alone cannot make that true — a prompt is a request to a model, and the failure
mode is silent: an answer with no attribution looks exactly like an answer that
needed none.

So the workspace prompt asks for a machine-readable trailer instead of a
sentence, and this module turns it into something the reader can check:

    ```sources
    table: hr_headcount
    metric: headcount/active
    assumption: active employees only | origin: user | why: you asked about "the team"
    assumption: contractors excluded | origin: definition | why: headcount/active counts employees only
    ```

Two things follow from that being parseable rather than prose:

- **Absence becomes visible.** An answer that reports a figure and declares
  nothing renders as "no source declared" rather than as an ordinary answer.
- **A claim can be wrong, and we can say so.** The turn's tool calls are the
  record of what the agent actually ran. A `table:` no tool call touched is
  reported to the reader as unverified. This is the part that separates
  "the prompt asks for it" from "the answer is accountable for it".

An ``assumption:`` is the one claim nothing can check, so it carries its own
accountability instead (TCRD-289 — a reader looking at six ``assumes …``
chips could not tell where any of them came from, or why). Each line names
its **origin** from a closed vocabulary (:data:`ASSUMPTION_ORIGINS`: the
user's question, a definition, a gap in the data, or the analyst's own
judgment) and a one-sentence **why**, as ``| origin: … | why: …`` segments
after the statement. Both are parsed here and rendered next to the
statement; a line without them is still a valid assumption — it renders as
"origin not stated", which is the same visible-absence rule the block
itself rests on.

What this deliberately is NOT: enforcement in the sense of refusing the answer.
That would need a second pass over the model on every turn, and it would trade
a visible gap for an invisible cost. The verdict is advisory and always
rendered — including when it is unflattering.

**The verdict is computed, never stored.** Both call sites already hold the
content and the tool calls together (`assistant_message` in
`app/chat/manager.py`, and `GET /sessions/{id}/messages` in `app/api/chat.py`),
so there is no column to add, no migration ladder to keep in step, and no
DuckDB/Postgres parity surface. A recomputed verdict also follows the code:
improving the matcher improves every historical message, which storing it at
write time would not.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

#: The fenced block the workspace prompt asks for. Tolerant on purpose — a
#: model writing ```` ```sources ```` with trailing spaces, or with the CRLF a
#: Windows-side paste can introduce, still parses. Non-greedy so an answer that
#: (wrongly) carries two blocks yields the first rather than swallowing the
#: prose between them.
#: Opening fence only. The BODY is located with `str.find`, never with a
#: non-greedy regex body: `` ```sources…(.*?)``` `` rescans to end-of-string
#: for every unterminated opening fence, so cost was O(occurrences x length)
#: over model output — which `verdict()` now walks on every assistant message
#: of every history read. The repo's rule is that regexes over untrusted text
#: stay linear; `find` is linear by construction and needs no reasoning about
#: backtracking. (Devin Review on this PR.)
_OPEN_RE = re.compile(r"```sources[ \t]*\r?\n", re.IGNORECASE)
_CLOSE = "```"

#: The prompt's SECOND wire-format trailer: ```next_actions, suggested
#: follow-up prompts the web client lifts into one-click buttons
#: (``extractNextActions`` in ``chat.js``). It lives here, next to the
#: sources fence, because the push sinks strip both in one breath — a sink
#: with no buttons must not end every answer in a fenced block of wire
#: format. Same tolerant shape, same linear-scan rules as `_OPEN_RE`.
_NEXT_ACTIONS_OPEN_RE = re.compile(r"```next_actions[ \t]*\r?\n", re.IGNORECASE)

#: One claim per line: `kind: ref`. Anything else in the block is ignored
#: rather than treated as an error — a stray blank line or a comment must not
#: cost the reader the whole block.
_CLAIM_RE = re.compile(r"^\s*(table|metric|glossary|document|assumption)\s*:\s*(.+?)\s*$", re.IGNORECASE)

#: Claim kinds that name something the agent should have touched, and can
#: therefore be checked. `assumption` is free text about the analyst's own
#: choices — there is nothing to check it against, and pretending otherwise
#: would render every honest assumption as "unverified".
#:
#: ``document`` joined the two SQL-shaped kinds because the vocabulary having
#: no word for a file is what pushed citations into ``assumption:``. An answer
#: built on the fact graph rests on neither a table nor a metric, so its only
#: legal slot was the one kind nothing checks: observed in the wild as five
#: ``assumption:`` lines carrying PDF filenames and engagement ids, each
#: badged "origin not stated" (no origin in :data:`ASSUMPTION_ORIGINS` means
#: "a source document"), under a row reading "Sources — none declared" —
#: true by this module's own definition and absurd above five named files.
#: The last of those lines said it outright: *"No SQL tables queried — this
#: answer is sourced entirely from the document fact graph"*, a model
#: reporting a schema mismatch through the only channel it had.
#:
#: ``glossary`` joined for the same reason one kind later (#2258): a governed
#: business term the answer leaned on had no word either, so its only legal
#: slot was ``assumption: … | origin: definition`` — a citation filed as a
#: caveat about method, which is precisely what the prompt warns against on
#: the line above it. A term is evidence.
#:
#: Its read tier is its OWN, and the difference matters downstream rather
#: than here: ``GET /api/glossary*`` serves any authenticated caller with no
#: per-resource narrowing (business vocabulary, not data), while ``GET
#: /api/metrics`` drops every metric bound to a table outside the caller's
#: stack. Nothing in this module reads either registry — verification is
#: containment over the turn's own tool calls — so the verdict is outside
#: that split by construction, and the client is where it has to be honoured
#: (a glossary chip opens the glossary tab, never the filtered metrics one;
#: see ``_claimHref`` in ``chat.js``).
VERIFIABLE_KINDS = frozenset({"table", "metric", "glossary", "document"})

#: Floor on a needle this module DERIVES from a claim (a document's basename,
#: its extensionless stem) rather than one the answer wrote. Peeling is meant
#: as latitude for a citation spelled differently from the tool output — not
#: as a rubber stamp, and below a few characters it becomes one: `document:
#: docs/` peels to a basename of ``""``, which is a substring of every
#: haystack ever built, and `document: a.pdf` peels to ``"a"``, which is a
#: substring of essentially any serialized tool call. Both verified against
#: turns that touched nothing.
#:
#: That is the one direction this module must not err in. Accepting too much
#: is elsewhere the safe failure (see :func:`_tool_call_haystack`) precisely
#: because a false "verified" costs the reader nothing they did not already
#: have — but a needle that matches everything is not leniency, it is the
#: badge asserting a check that never happened. The ref the answer actually
#: wrote is still matched in full at any length; only the guesses have a
#: floor. Four characters: shorter than that and a filename fragment is
#: likelier to appear inside unrelated JSON than to be the document's name.
_MIN_DERIVED_NEEDLE = 4

#: The separator a model puts between a document's name and its own gloss on
#: it — ``document: Foo.pptx — 4-week assessment, week 1``. The prompt asks
#: for the filename alone and this is not that, but the WHOLE line arrives as
#: the ref, so the citation matched nothing and the reader was told a real
#: document was unverified for the sin of being described. The head is peeled
#: like a directory prefix: same latitude, same floor, same direction of
#: error. Spaces on both sides are required, so the hyphen inside `4-week` or
#: `Week-2-Deliverable.pdf` is not a separator; a pipe is not one either,
#: because `Q3 | Q4 review.pdf` is a filename this parser already accepts.
_DESCRIPTION_SEP_RE = re.compile(r"\s+[—–-]{1,2}\s+")

#: Runs of anything that is not a letter or a digit, for :func:`_normalize`.
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

#: Where an assumption came from — the closed vocabulary the workspace prompt
#: asks for on an ``assumption:`` line's ``origin:`` segment. Closed on
#: purpose: the UI turns each value into a badge with fixed copy
#: (``_ASSUMPTION_ORIGIN`` in ``chat.js``), and a free-text origin would put
#: the model's phrasing where the reader expects a category.
#:
#: - ``user``       — the question said or implied it.
#: - ``definition`` — a metric definition, semantic model, glossary term or
#:                    knowledge-base document says so.
#: - ``data``       — the data forced it: the column does not exist or the
#:                    value is missing, so a proxy or a subset stood in.
#: - ``judgment``   — the analyst's own choice, with nothing behind it.
#:
#: Anything else normalizes to ``None`` — "origin not stated" — rather than
#: to a guess. A model that went off-vocabulary is told so by the badge, the
#: same way a table nothing queried is told so by "unverified".
ASSUMPTION_ORIGINS: tuple[str, ...] = ("user", "definition", "data", "judgment")

#: Tolerated spellings, lowercased. Kept short: an alias table that grows
#: with every model quirk stops being a vocabulary.
_ORIGIN_ALIASES: dict[str, str] = {
    "user": "user",
    "question": "user",
    "asked": "user",
    "request": "user",
    "definition": "definition",
    "defined": "definition",
    "model": "definition",
    "semantic": "definition",
    "semantic model": "definition",
    "metric": "definition",
    "glossary": "definition",
    "knowledge": "definition",
    "knowledge base": "definition",
    "docs": "definition",
    "documentation": "definition",
    "data": "data",
    "schema": "data",
    "missing": "data",
    "missing data": "data",
    "proxy": "data",
    "judgment": "judgment",
    "judgement": "judgment",
    "own": "judgment",
    "my own": "judgment",
    "analyst": "judgment",
    "default": "judgment",
    "choice": "judgment",
    "guess": "judgment",
    "inference": "judgment",
}

#: The keyed segments an assumption line may carry after its statement,
#: ``| key: value``. Two keys, a few tolerated spellings each.
_SEGMENT_KEYS: dict[str, str] = {
    "origin": "origin",
    "from": "origin",
    "source": "origin",
    "why": "why",
    "because": "why",
    "reason": "why",
    "rationale": "why",
}
#: Compiled FROM ``_SEGMENT_KEYS`` so the accepted spellings live in one place
#: — a key added to the map above is recognized here without a second edit
#: (Copilot review on #2047). Greedy body, stripped in code: no non-greedy
#: pattern over model output (see the note on `_OPEN_RE`).
_SEGMENT_RE = re.compile(
    r"^\s*(" + "|".join(re.escape(k) for k in _SEGMENT_KEYS) + r")\s*:(.*)$",
    re.IGNORECASE,
)


def normalize_origin(raw: Optional[str]) -> Optional[str]:
    """One of :data:`ASSUMPTION_ORIGINS`, or ``None`` for anything else."""
    if not raw:
        return None
    key = " ".join(raw.strip().strip("`'\"").lower().split())
    return _ORIGIN_ALIASES.get(key)


def _split_assumption(ref: str) -> tuple[str, Optional[str], Optional[str]]:
    """``(statement, origin, why)`` out of one assumption line's body.

    The statement is everything up to the first ``|`` segment that carries
    a recognized key. A ``|`` inside prose is kept where it was: an
    un-keyed segment continues whatever came before it — the statement, or
    the ``why:`` a model wrote a pipe into — instead of being dropped or
    mis-filed. ``origin`` is normalized (see :func:`normalize_origin`); an
    empty ``why:`` is no rationale at all.
    """
    parts = ref.split("|")
    buckets: dict[str, list[str]] = {"statement": [parts[0]], "origin": [], "why": []}
    current = "statement"
    for seg in parts[1:]:
        m = _SEGMENT_RE.match(seg)
        if m:
            current = _SEGMENT_KEYS[m.group(1).lower()]
            buckets[current] = [m.group(2)]
        else:
            buckets[current].append(seg)
    statement = "|".join(buckets["statement"]).strip()
    origin = normalize_origin("|".join(buckets["origin"])) if buckets["origin"] else None
    why = "|".join(buckets["why"]).strip().strip("`").strip() or None
    return statement, origin, why


@dataclass(frozen=True)
class SourceClaim:
    kind: str  # "table" | "metric" | "glossary" | "document" | "assumption"
    ref: str
    #: None for kinds that carry nothing to check (see VERIFIABLE_KINDS).
    verified: Optional[bool] = None
    #: ``assumption`` only: one of ASSUMPTION_ORIGINS, or None when the line
    #: did not say (or said something outside the vocabulary).
    origin: Optional[str] = None
    #: ``assumption`` only: the one-sentence rationale, or None.
    why: Optional[str] = None


@dataclass(frozen=True)
class SourcesVerdict:
    #: A `sources` block was present at all.
    declared: bool
    claims: list[SourceClaim] = field(default_factory=list)

    @property
    def unverified(self) -> list[SourceClaim]:
        return [c for c in self.claims if c.verified is False]

    def to_dict(self) -> dict[str, Any]:
        return {
            "declared": self.declared,
            "claims": [_claim_to_dict(c) for c in self.claims],
        }


def _claim_to_dict(c: SourceClaim) -> dict[str, Any]:
    """``origin``/``why`` ride only on assumptions — a table or metric claim
    keeps the three-key shape it always had on the wire."""
    d: dict[str, Any] = {"kind": c.kind, "ref": c.ref, "verified": c.verified}
    if c.kind == "assumption":
        d["origin"] = c.origin
        d["why"] = c.why
    return d


def _first_block_span(content: str, open_re: re.Pattern[str] = _OPEN_RE):
    """``(start, body_start, body_end, end)`` of the first complete block.

    ``None`` when there is no opening fence, or when an opening fence is never
    closed — an unterminated block is not a block, and treating it as one
    would let a truncated answer swallow everything after it.
    """
    m = open_re.search(content)
    if not m:
        return None
    body_start = m.end()
    body_end = content.find(_CLOSE, body_start)
    if body_end == -1:
        return None
    return (m.start(), body_start, body_end, body_end + len(_CLOSE))


def extract_block(content: str) -> Optional[str]:
    """The raw body of the first `sources` block, or None."""
    if not content:
        return None
    span = _first_block_span(content)
    return content[span[1] : span[2]] if span else None


def strip_block(content: str) -> str:
    """The answer without its machine-readable ``sources`` fence.

    For the sinks that cannot render the block as anything better than a code
    block. The web client strips it too (``stripSourcesFence`` in
    ``chat.js``) and draws chips from the server's verdict instead; a push
    sink has no chips, so where the answer used to end in a readable
    ``Sources:`` line it would now end in a fenced block of machinery.

    Deliberately NOT applied before persistence, and not to the AG-UI/SSE
    surface. The verdict is *derived* rather than stored — `GET
    /sessions/{id}/messages` re-parses it out of the saved content — so
    stripping it on the way to the database would silently drop the chips on
    every reload. And a programmatic consumer of the agent API is exactly the
    caller the machine-readable form exists for. (Devin Review on this PR.)
    """
    if not content:
        return content
    return _strip_blocks(content, _OPEN_RE)


def strip_next_actions_block(content: str) -> str:
    """The answer without its ``next_actions`` trailer.

    The web client lifts this trailer into one-click follow-up buttons
    (``extractNextActions`` in ``chat.js``); a push sink has no buttons wired
    to it, so the raw fence would sit under every answer as a code block of
    wire format. Same persistence rule as ``strip_block``: never applied
    before the database — the web client re-extracts the buttons from the
    saved content on every history reload.

    Deliberately NOT applied to the agent API either — ``POST
    /api/v1/agents/{slug}/responses`` (sync and its ``agent_response``
    worker-job/poll form) and the AG-UI session stream return the answer
    verbatim: a programmatic consumer is exactly the caller the
    machine-readable trailer exists for, the same line ``strip_block``
    draws for the sources fence. (Outbound webhooks carry a notification,
    never the answer — see ``app/chat/webhook_delivery.py`` — so nothing
    leaks there.) The one human-facing AG-UI client, ``agnes chat``, strips
    at render time on its own (``cli/commands/chat.py``).
    """
    if not content:
        return content
    return _strip_blocks(content, _NEXT_ACTIONS_OPEN_RE)


def _strip_blocks(content: str, open_re: re.Pattern[str]) -> str:
    out = content
    while True:
        span = _first_block_span(out, open_re)
        if span is None:
            return out.rstrip()
        start, _body_start, _body_end, end = span
        out = out[:start] + out[end:]


def parse_claims(block_body: str) -> list[SourceClaim]:
    """Claims in the order written, deduplicated on (kind, ref).

    Order is the agent's own — the first table named is usually the one the
    figure came from — and duplicates are dropped because a model listing the
    same table twice should not show the reader the same chip twice.
    """
    seen: set[tuple[str, str]] = set()
    out: list[SourceClaim] = []
    for line in block_body.splitlines():
        m = _CLAIM_RE.match(line)
        if not m:
            continue
        kind = m.group(1).lower()
        ref = m.group(2).strip()
        # Strip the backticks a model reaches for out of markdown habit; the
        # ref is compared against tool-call text, where it appears bare.
        ref = ref.strip("`").strip()
        origin = why = None
        if kind == "assumption":
            ref, origin, why = _split_assumption(ref)
        if not ref:
            continue
        # Deduplicated on the statement alone: the same assumption written
        # twice with two rationales is still one assumption, and the reader
        # gets the first.
        key = (kind, ref)
        if key in seen:
            continue
        seen.add(key)
        out.append(SourceClaim(kind=kind, ref=ref, origin=origin, why=why))
    return out


def _tool_call_haystack(tool_calls: Optional[Iterable[Any]]) -> str:
    """Everything the turn's tool calls said, as one lowercase string.

    Deliberately crude. The alternative — parsing SQL out of each call and
    resolving table identifiers — would be precise about `agnes query` and
    blind to every other route to the same data (`agnes describe`, a snapshot,
    an MCP tool, a `--remote` push-down). Substring containment over the whole
    serialized call is wrong in the harmless direction: it can accept a table
    the agent only mentioned, and it does not invent a match that is not there.
    A false "verified" leaves the reader where they are today; a false
    "unverified" would teach them to ignore the badge.
    """
    if not tool_calls:
        return ""
    parts: list[str] = []
    for call in tool_calls:
        if isinstance(call, str):
            parts.append(call)
            continue
        try:
            parts.append(json.dumps(call, default=str))
        except (TypeError, ValueError):
            parts.append(str(call))
    return "\n".join(parts).lower()


def _normalize(text: str) -> str:
    """Lowercased, with every run of non-alphanumerics collapsed to one ``_``.

    A filename reaches the model twice, spelled differently each time. The web
    client REWRITES a dropped file's name before uploading it
    (``safeUploadName`` in ``chat.js``: spaces and accents to underscores, a
    stamp before the extension) because the server's filename rule is a
    rejection, not a sanitizer — while the chat bubble, and therefore the
    answer, keeps saying the name the user dropped. Matching
    ``AI Opportunity Assessment.pptx`` against the stored
    ``AI_Opportunity_Assessment-20260904T151500-1.pptx`` is not latitude
    toward a sloppy citation; the rewrite between them is ours.

    Collapsing rather than deleting keeps the separator that stops two words
    running together into a third, and every needle built this way still goes
    through :data:`_MIN_DERIVED_NEEDLE` — punctuation-insensitivity must not
    hand back the rubber stamp the floor exists to refuse.
    """
    return _NON_ALNUM_RE.sub("_", text.lower())


def _document_needles(ref: str) -> list[str]:
    """Every spelling of ``ref`` we accept as naming the same document.

    A document is cited by whatever the agent saw it called, and the tools do
    not agree with each other on that: `fact_claims` names the file, a
    collections listing carries a path in front of it, a distillate may drop
    the extension, and the model may append a description of its own. So a
    directory prefix, a trailing extension and a description tail are peeled
    in turn — same latitude, and the same direction of error, as the metric
    case. Peeling is order-dependent (head first, then basename, then its
    stem) so ``collections/x/a.pdf — the roadmap`` reaches ``a``.

    Every DERIVED needle goes through :data:`_MIN_DERIVED_NEEDLE`; the ref the
    answer actually wrote is returned whatever its length.
    """
    cands = {ref}
    head = _DESCRIPTION_SEP_RE.split(ref, 1)[0].strip()
    if head:
        cands.add(head)
    for cand in list(cands):
        base = cand.rsplit("/", 1)[-1]
        cands.update((base, base.rsplit(".", 1)[0]))
    return [c for c in cands if c == ref or len(c) >= _MIN_DERIVED_NEEDLE]


def verify(
    claims: list[SourceClaim],
    tool_calls: Optional[Iterable[Any]],
    tool_results: Optional[Iterable[Any]] = None,
) -> list[SourceClaim]:
    """Judge each checkable claim against the record of what the turn ran.

    Two records, not one, and which claim gets which is the substance here.

    A `table:` or a `metric:` names an INPUT — the agent wrote that name into
    the SQL or the catalog lookup — so the arguments are the whole record, and
    widening them would break the check: ``agnes catalog`` RETURNS every table
    id the caller can see, which would verify any table an answer cared to
    name off the back of one listing call.

    A `document:` names an OUTPUT. No document tool takes a filename argument
    (`fact_search` takes a query, `fact_claims` a subject id, a collections
    listing nothing at all), so the file is named for the first time in the
    tool's own result. Checked against arguments alone — which is what
    shipped — a document cited by filename could not verify on any turn, at
    any peeling: six correct citations under six amber badges, teaching the
    reader that the badge means nothing. So the results reach this check too,
    for `document:` alone, and only where the name could not be anywhere else.
    """
    haystack = _tool_call_haystack(tool_calls)
    # Built once, and only if something asks for it: a result is the whole of
    # a tool's output (a query's rows, a fact dump), this runs on every
    # assistant message of every history read, and a turn with no document
    # claim never needs it.
    document_haystack: Optional[tuple[str, str]] = None
    out: list[SourceClaim] = []
    for c in claims:
        if c.kind not in VERIFIABLE_KINDS:
            out.append(c)
            continue
        ref = c.ref.lower()
        # A metric id is written `family/name` in the catalog and may be cited
        # either way round; accept the bare name too so a correct citation is
        # not reported as unverified over punctuation.
        needles = [ref]
        if c.kind == "metric" and "/" in ref:
            needles.append(ref.rsplit("/", 1)[-1])
        # A `glossary:` ref gets NO derived needle, deliberately — it is the
        # one kind whose ref has no structure to peel. A metric id is a
        # namespaced `family/name` and a document is a path, so their tails
        # are the same identifier written shorter; a term is prose, where a
        # slash or a dot is punctuation inside the name ("bookings/billings"
        # is one term). Splitting it would not be latitude, it would invent a
        # needle the answer never wrote and verify the citation against a
        # turn that looked up something else.
        hay = haystack
        if c.kind == "document":
            needles = _document_needles(ref)
            if document_haystack is None:
                widened = "\n".join(p for p in (haystack, _tool_call_haystack(tool_results)) if p)
                document_haystack = (widened, _normalize(widened))
            hay, normalized_hay = document_haystack
            verified = any(n in hay for n in needles) or any(
                len(n) >= _MIN_DERIVED_NEEDLE and n in normalized_hay for n in map(_normalize, needles)
            )
        else:
            verified = any(n in hay for n in needles)
        out.append(SourceClaim(kind=c.kind, ref=c.ref, verified=verified))
    return out


def verdict(
    content: str,
    tool_calls: Optional[Iterable[Any]] = None,
    tool_results: Optional[Iterable[Any]] = None,
) -> SourcesVerdict:
    """The whole pass: parse the block, check what can be checked.

    An answer with no block yields `declared=False` and no claims — which the
    UI renders only when the turn looks like it reported something (see
    `chat.js`), because "no source declared" under "hello" would be noise.
    """
    body = extract_block(content)
    if body is None:
        return SourcesVerdict(declared=False)
    return SourcesVerdict(declared=True, claims=verify(parse_claims(body), tool_calls, tool_results))
