"""Deterministic, per-instance anonymization of markdown documents.

This is the built-in implementation of the anonymize-in-front pipeline's
substitution step described in
``docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md``
§9.2, and configured per the operator guide in ``docs/anonymization.md``
(``AGNES_ANONYMIZATION_HMAC_KEY`` / the ``extraction.anonymization`` block).

The replacement mechanics (single-alternation, longest-first, one-pass
substitution; shape detectors before entity substitution; "person wins a kind
conflict"; the alias-unification prefix heuristic) are ported from
``doc_quantization`` (Apache-2.0, ``doc_quant/redactor.py``).

What it produces
----------------

Three kinds of substitution, applied in this order:

1. **URLs** collapse to a fixed ``**URL**`` marker. A URL names a host, not a
   party, so it is deliberately *not* identity-bearing: two documents citing
   the same address must not thereby join into one subject.
2. **Email addresses** become ``EMAIL_<hmac>``. An address is the strongest
   join key a person has, so it earns a stable token rather than a marker —
   and replacing the whole address as one unit is what stops a local part
   (usually a person's name) surviving next to a redacted domain.
3. **Entities** (persons, companies) become ``PERSON_<hmac>`` /
   ``COMPANY_<hmac>``.

``<hmac>`` is the first six hex characters of
``hmac_sha256(key, normalize(entity))``. Six hex characters is 24 bits: wide
enough that the handful of entities in one document do not collide in
practice, short enough that the anonymized text stays readable.

Why HMAC and not a fixed marker: a fixed marker collapses every person in a
document onto one node once facts are extracted from the anonymized copy.
Why per-instance and not global: two instances with different keys produce
disjoint token spaces, so the same person can never be correlated across
tenants by their pseudonym.

The key is a secret. It is never logged, never embedded in the output, and
never included in an exception message.

Threat-model note (security playbook §5): every regex here runs over
untrusted document text and is written to be linear-time — bounded
repetitions over disjoint character classes, no ambiguous alternation.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "AnonymizeResult",
    "Detector",
    "Entity",
    "EntityKind",
    "RegexDetector",
    "anonymize_markdown",
    "normalize",
    "pseudonym",
]

PERSON: Literal["person"] = "person"
COMPANY: Literal["company"] = "company"

EntityKind = Literal["person", "company"]


@dataclass(frozen=True)
class Entity:
    """One detected entity: the exact substring, and what kind it is.

    ``text`` must be a verbatim substring of the document — substitution is
    literal and case-sensitive, so a detector that normalizes or lemmatizes
    before reporting would produce entities that never match.
    """

    text: str
    kind: EntityKind


#: A detector maps a document to the entities it contains. The default is
#: :class:`RegexDetector`; an LLM/NER-backed detector plugs in here unchanged.
Detector = Callable[[str], list[Entity]]


@dataclass
class AnonymizeResult:
    """The anonymized document plus how many substitutions produced it."""

    text: str
    #: Total substitutions made — URL collapses + email tokens + entity tokens.
    #: Counts *occurrences*, not distinct entities.
    replaced: int


# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

#: URLs are collapsed, never tokenized — see the module docstring.
URL_PLACEHOLDER = "**URL**"

#: The word a pseudonym starts with, before its ``_<digest>`` tail.
PSEUDONYM_PREFIXES: dict[str, str] = {PERSON: "PERSON", COMPANY: "COMPANY", "email": "EMAIL"}

#: How much of the HMAC digest is kept.
PSEUDONYM_DIGEST_CHARS = 6

#: Alias unification (see :func:`_alias_canonicals`): two detected forms are
#: one entity when they share at least ``ALIAS_MIN_PREFIX`` leading characters
#: and each tail past that prefix is at most ``ALIAS_MAX_SUFFIX`` characters —
#: the shape of a Czech case ending ("Novák" / "Nováka" / "Novákovi").
ALIAS_MIN_PREFIX = 4
ALIAS_MAX_SUFFIX = 3

#: Bare words that may never be treated as an entity, because rewriting an
#: already-inserted placeholder into another placeholder helps nobody.
_RESERVED_WORDS = frozenset({"PERSON", "COMPANY", "EMAIL", "URL"})

#: An already-inserted pseudonym, so a second pass can recognize (and skip) it.
_PSEUDONYM_TOKEN_RE = re.compile(r"^(?:PERSON|COMPANY|EMAIL)_[0-9a-f]{%d}$" % PSEUDONYM_DIGEST_CHARS)

# Pragmatic rather than RFC-complete: it matches what documents actually
# contain. Linear-time by construction — "@" is disjoint from the local-part
# class, every label is anchored by a literal ".", and the label repetition is
# bounded, so the engine can never explore an unbounded number of splits.
_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+\-])"
    r"[A-Za-z0-9._%+\-]{1,64}"
    r"@"
    r"(?:[A-Za-z0-9\-]{1,63}\.){1,8}"
    r"[A-Za-z]{2,24}"
    r"(?![A-Za-z0-9\-])"
)

# A URL identifies as much as a name does: the host names the company and the
# path often carries an account or case identifier. Both the scheme form and
# the bare "www." form are matched, up to the first whitespace or bracket.
# Linear-time: the two alternation branches start with disjoint characters
# ("h" / "w") and the tail is a single bounded character class.
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"'()\[\]]{1,2000}")

#: Sentence punctuation a URL at the end of a sentence would otherwise swallow.
_URL_TRAILING_PUNCTUATION = ".,;:!?"


# --------------------------------------------------------------------------
# shape detectors (deterministic, detector-independent)
# --------------------------------------------------------------------------


def _trim_url(candidate: str) -> str:
    """Drop sentence punctuation a URL match picked up at its end."""
    return candidate.rstrip(_URL_TRAILING_PUNCTUATION)


def normalize(text: str) -> str:
    """The form an entity is hashed under: case- and whitespace-insensitive.

    Casefolded rather than lowercased, so non-English alphabets fold the way
    their own rules say they should, and internal whitespace collapsed, so a
    name broken across a line break hashes like the same name written inline
    in another document.

    Note this is only half of "normalization": the other half is
    document-scoped alias unification (:func:`_alias_canonicals`), which maps
    the inflected forms of one name onto a single canonical string *before*
    that string is hashed.
    """
    return " ".join(text.split()).casefold()


def pseudonym(key: bytes, kind: str, canonical: str) -> str:
    """Return e.g. ``PERSON_1a2b3c`` for one canonical, normalized form.

    HMAC rather than a plain hash: the key is what makes the token
    unguessable, and ``sha256(key + name)`` is exactly the construction HMAC
    exists to replace. The same ``(key, canonical)`` always gives the same
    token — across documents and across runs, which is what lets a downstream
    consumer join two mentions of one entity — and a different key gives an
    unrelated token.
    """
    digest = hmac.new(key, canonical.encode("utf-8"), hashlib.sha256)
    return f"{PSEUDONYM_PREFIXES[kind]}_{digest.hexdigest()[:PSEUDONYM_DIGEST_CHARS]}"


# --------------------------------------------------------------------------
# alias unification
# --------------------------------------------------------------------------


def _common_prefix_length(left: str, right: str) -> int:
    """How many leading characters two strings share."""
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


def _inflection_pair(left: str, right: str) -> bool:
    """True when two forms differ only by what looks like a case ending."""
    shared = _common_prefix_length(left, right)
    if shared < ALIAS_MIN_PREFIX:
        return False
    return len(left) - shared <= ALIAS_MAX_SUFFIX and len(right) - shared <= ALIAS_MAX_SUFFIX


def _stem(name: str) -> str:
    """The last whitespace-separated token — the surname, for a person name."""
    parts = name.split()
    return parts[-1] if parts else name


def _lcp_all(values: Sequence[str]) -> str:
    """The longest prefix shared by every value."""
    if not values:
        return ""
    shared = values[0]
    for value in values[1:]:
        shared = shared[: _common_prefix_length(shared, value)]
        if not shared:
            break
    return shared


def _alias_canonicals(names: Iterable[str], *, kind: str) -> dict[str, str]:
    """Map each normalized name to the form its whole alias group hashes under.

    Substitution is verbatim and case-sensitive, so an inflecting language
    reports one person as several strings: "Novák", "Nováka" and "Novákovi"
    are three detected entities, and without unification they become three
    pseudonyms — three people in whatever graph is built downstream.

    The heuristic is deliberately the cheapest defensible one — no lemmatizer,
    no model, no wordlist. Two normalized forms join the same group when:

    * **whole-string rule** — they share at least ``ALIAS_MIN_PREFIX`` leading
      characters and each tail past that prefix is at most
      ``ALIAS_MAX_SUFFIX`` characters ("novák" ~ "nováka" ~ "novákovi"); or
    * **surname rule** (persons only) — exactly one of the two is a bare
      single-token form and their *last* tokens satisfy the rule above, so
      "jan novák" and "nováka" join. Requiring one side to be a bare surname
      is what keeps "jan novák" and "petr novák" apart: two full names never
      merge on the surname alone.

    Grouping is transitive (union-find), so the outer pair of a chain need not
    itself satisfy the rule.

    A group hashes under its whole-string longest common prefix when that is
    still ``ALIAS_MIN_PREFIX`` characters or more, otherwise (a group that
    mixes a full name with a bare surname) under its *stem* prefix. This is
    what makes the token stable across documents: a document spelling
    "Jan Novák ... Nováka" and a document spelling only "Nováka ... Novákovi"
    both canonicalize to "novák".

    Known and accepted, in both directions:

    * two genuinely different people who share a surname *and* are both
      mentioned by bare surname merge into one pseudonym;
    * a document that only ever spells the full name ("Jan Novák", never an
      inflected bare surname) canonicalizes to "jan novák" and therefore does
      *not* join a document that only ever spells "Novákovi". Cross-document
      identity from purely local information is not solvable here; this is the
      residual case, not a bug.

    Candidate pairs are bucketed by their first ``ALIAS_MIN_PREFIX``
    characters, which is exact (the rule requires that prefix to be equal) and
    keeps the comparison from being quadratic in the whole document.
    """
    unique = sorted(set(names))
    parent = {name: name for name in unique}

    def find(name: str) -> str:
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    def union(left: str, right: str) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    whole_buckets: dict[str, list[str]] = {}
    stem_buckets: dict[str, list[str]] = {}
    for name in unique:
        if len(name) >= ALIAS_MIN_PREFIX:
            whole_buckets.setdefault(name[:ALIAS_MIN_PREFIX], []).append(name)
        if kind == PERSON:
            stem = _stem(name)
            if len(stem) >= ALIAS_MIN_PREFIX:
                stem_buckets.setdefault(stem[:ALIAS_MIN_PREFIX], []).append(name)

    for bucket in whole_buckets.values():
        for index, left in enumerate(bucket):
            for right in bucket[index + 1 :]:
                if _inflection_pair(left, right):
                    union(left, right)

    for bucket in stem_buckets.values():
        for index, left in enumerate(bucket):
            for right in bucket[index + 1 :]:
                # Exactly one side must be a bare surname; two full names never
                # merge on a shared surname alone.
                if (" " in left) == (" " in right):
                    continue
                if _inflection_pair(_stem(left), _stem(right)):
                    union(left, right)

    groups: dict[str, list[str]] = {}
    for name in unique:
        groups.setdefault(find(name), []).append(name)

    canonicals: dict[str, str] = {}
    for group in groups.values():
        whole = _lcp_all(group)
        if len(whole) >= ALIAS_MIN_PREFIX:
            canonical = whole
        else:
            stem_prefix = _lcp_all([_stem(name) for name in group])
            canonical = stem_prefix if len(stem_prefix) >= ALIAS_MIN_PREFIX else group[0]
        for name in group:
            canonicals[name] = canonical
    return canonicals


# --------------------------------------------------------------------------
# the default detector
# --------------------------------------------------------------------------

# A word: starts with a letter, continues with letters/digits/underscore/
# apostrophe/hyphen. Linear-time (one bounded-free class with no following
# ambiguity).
_WORD_RE = re.compile(r"[^\W\d_][\w'’\-]*")

#: Legal-form markers that make a preceding capitalized run a company with
#: high confidence. Case-SENSITIVE on purpose: matching "se"/"ab"/"co"
#: case-insensitively would fire on ordinary Czech and English words.
_COMPANY_SUFFIX_RE = re.compile(
    r"(?<![\w.])(?:"
    r"spol\.\s?s\s?r\.\s?o\.|s\.\s?r\.\s?o\.|a\.\s?s\.|k\.\s?s\.|v\.\s?o\.\s?s\.|"
    r"o\.\s?p\.\s?s\.|z\.\s?s\.|"
    r"Inc\.|Inc|Ltd\.|Ltd|LLC|L\.L\.C\.|GmbH|AG|SE|N\.V\.|B\.V\.|S\.A\.|S\.p\.A\.|"
    r"Oy|AB|A/S|PLC|plc|Corp\.|Corp|Co\."
    r")(?!\w)"
)

#: Titles that make the capitalized run right after them a person name even
#: when it is a single token.
_HONORIFICS = frozenset(
    {
        "ing",
        "mgr",
        "bc",
        "mudr",
        "judr",
        "rndr",
        "phdr",
        "paeddr",
        "dr",
        "doc",
        "prof",
        "pan",
        "pana",
        "panu",
        "pane",
        "paní",
        "pani",
        "mr",
        "mrs",
        "ms",
        "miss",
        "sir",
    }
)

#: Words whose capitalization at the start of a sentence is explained by
#: grammar, not by being a name. Only consulted for a sentence-initial token.
_SENTENCE_INITIAL_STOPWORDS = frozenset(
    {
        # Czech
        "a",
        "ale",
        "ani",
        "asi",
        "az",
        "až",
        "byl",
        "byla",
        "bylo",
        "dne",
        "do",
        "je",
        "jsme",
        "jsou",
        "k",
        "ke",
        "na",
        "nad",
        "o",
        "od",
        "po",
        "pod",
        "pro",
        "před",
        "při",
        "s",
        "se",
        "tato",
        "tento",
        "toto",
        "u",
        "v",
        "ve",
        "však",
        "z",
        "za",
        "ze",
        # English
        "a",
        "an",
        "and",
        "as",
        "at",
        "but",
        "by",
        "for",
        "from",
        "he",
        "her",
        "his",
        "in",
        "it",
        "its",
        "of",
        "on",
        "or",
        "she",
        "the",
        "their",
        "they",
        "this",
        "to",
        "we",
        "with",
    }
)

#: Minimum length of a token that may take part in a capitalized name run.
#: Drops the sentence-initial one-letter Czech prepositions ("S Nováka") that
#: would otherwise look like the first half of a two-word name.
_MIN_NAME_TOKEN_CHARS = 2

#: Longest capitalized run taken as one company name before its legal form.
_MAX_COMPANY_RUN = 4


@dataclass(frozen=True)
class _Word:
    text: str
    start: int
    end: int


def _words(text: str) -> list[_Word]:
    return [_Word(m.group(0), m.start(), m.end()) for m in _WORD_RE.finditer(text)]


def _is_sentence_initial(text: str, start: int) -> bool:
    """True when the character run before ``start`` only explains capitalization.

    That is: the start of the document, the start of a line, or the end of a
    previous sentence — plus the markdown furniture ("#", "-", "*", ">", "|")
    that can sit between a line start and its first word.
    """
    index = start - 1
    while index >= 0 and text[index] in " \t":
        index -= 1
    if index < 0:
        return True
    char = text[index]
    if char in ".!?\n\r":
        return True
    if char in "#-*>|":
        # Markdown furniture only counts when nothing but furniture and
        # whitespace separates it from the line start.
        line_start = text.rfind("\n", 0, index) + 1
        return set(text[line_start : index + 1]) <= set("#-*>| \t0123456789.")
    return False


def _is_reserved(word: str) -> bool:
    return word in _RESERVED_WORDS or bool(_PSEUDONYM_TOKEN_RE.match(word))


def _adjacent(text: str, left: _Word, right: _Word) -> bool:
    """True when two words form one run — only spaces separate them.

    An abbreviating full stop is tolerated after an honorific or a
    single-letter initial ("Ing. Novák", "J. Novák"), and nowhere else:
    tolerating it in general would splice the last word of one sentence onto
    the first word of the next.
    """
    gap = text[left.end : right.start]
    if gap == "":
        return False
    if gap.strip("  ") == "":
        return True
    abbreviating = left.text.casefold() in _HONORIFICS or len(left.text) == 1
    return abbreviating and gap.startswith(".") and gap[1:].strip("  ") == ""


class RegexDetector:
    """Rule-based entity detection — the default, deliberately conservative.

    **This is a heuristic, not a recognizer.** It runs in three passes:

    1. **Companies** — a run of capitalized words immediately followed by a
       legal-form marker (``s.r.o.``, ``a.s.``, ``Inc.``, ``GmbH``, …). High
       precision; the marker is what carries the confidence.
    2. **Persons** — a capitalized run introduced by an honorific
       (``Ing.``, ``Dr.``, ``pan``, ``Mr.``, …), or any run of two or more
       adjacent capitalized tokens of at least two characters each. A
       sentence-initial token that is a grammatical stopword is dropped from
       the front of a run, because its capitalization is explained by the
       sentence rather than by being a name.
    3. **Inflected variants** — any remaining capitalized token that looks
       like a case-inflected form of a surname already found in pass 2
       ("Nováka", "Novákovi" once "Jan Novák" is known). This is what gives an
       inflecting language usable recall without opening pass 2 to every
       capitalized word in the document.

    What it will miss — read this before trusting it on real documents:

    * **A person mentioned only by bare surname, never with a first name or a
      title.** Pass 2 needs two tokens or an honorific, and pass 3 needs a
      surname from pass 2 to anchor on. "Novák podepsal smlouvu." in a
      document that never spells his first name is not detected at all.
    * **Lowercase, ALL-CAPS and OCR-mangled names.** Detection keys on an
      initial capital followed by lowercase-ish text.
    * **Single-token company names without a legal form** — "Keboola",
      "Anthropic", "Acme" standing alone are indistinguishable from any other
      capitalized word and are not detected.
    * **Companies whose legal form is not in the list**, or is written in a
      way the list does not cover.
    * **Names split across markup** — bold/italic markers, a soft hyphen or a
      line break inside a name break the run.
    * **Identifiers that are not names but identify anyway**: phone numbers,
      birth numbers / national IDs, IBANs, account numbers, addresses, case
      numbers, VAT ids. None are detected. If your corpus contains them,
      this detector is not sufficient on its own.
    * **Non-Latin scripts** are effectively untouched, as is any language
      whose orthography does not capitalize proper nouns.

    What it will over-detect (the safer direction, but still wrong):

    * **Place, product, and organization names** written as two capitalized
      words become persons — "New York", "Prague Castle", "Data Apps".
    * **Title-Cased headings** become a person run.
    * Two different people who share a surname and are both referred to by
      bare surname collapse into one pseudonym (see
      :func:`_alias_canonicals`).

    The interface exists so this can be replaced: any
    ``Callable[[str], list[Entity]]`` — an NER model, an LLM pass, a curated
    gazetteer — drops into ``anonymize_markdown(..., detector=...)`` without
    touching the substitution machinery. For a corpus where a missed name is
    a real disclosure, use one.
    """

    def __call__(self, text: str) -> list[Entity]:
        words = _words(text)
        entities: list[Entity] = []
        covered: list[tuple[int, int]] = []

        for entity, span in self._companies(text, words):
            entities.append(entity)
            covered.append(span)

        person_spans: list[tuple[int, int]] = []
        for entity, span in self._persons(text, words, covered):
            entities.append(entity)
            person_spans.append(span)

        covered.extend(person_spans)
        entities.extend(self._inflected_variants(words, entities, covered))
        return entities

    # -- pass 1 ------------------------------------------------------------

    def _companies(self, text: str, words: list[_Word]) -> list[tuple[Entity, tuple[int, int]]]:
        found: list[tuple[Entity, tuple[int, int]]] = []
        for marker in _COMPANY_SUFFIX_RE.finditer(text):
            preceding = [word for word in words if word.end <= marker.start()]
            run: list[_Word] = []
            for word in reversed(preceding):
                if len(run) >= _MAX_COMPANY_RUN:
                    break
                anchor = run[0] if run else None
                gap_end = anchor.start if anchor else marker.start()
                if text[word.end : gap_end].strip("  ,") != "":
                    break
                if not word.text[:1].isupper() or _is_reserved(word.text):
                    break
                run.insert(0, word)
            if not run:
                continue
            start, end = run[0].start, marker.end()
            found.append((Entity(text=text[start:end], kind=COMPANY), (start, end)))
        return found

    # -- pass 2 ------------------------------------------------------------

    def _persons(
        self,
        text: str,
        words: list[_Word],
        covered: list[tuple[int, int]],
    ) -> list[tuple[Entity, tuple[int, int]]]:
        found: list[tuple[Entity, tuple[int, int]]] = []
        index = 0
        while index < len(words):
            word = words[index]
            if not word.text[:1].isupper() or _overlaps(word, covered):
                index += 1
                continue
            run = [word]
            cursor = index + 1
            while (
                cursor < len(words)
                and words[cursor].text[:1].isupper()
                and _adjacent(text, words[cursor - 1], words[cursor])
                and not _overlaps(words[cursor], covered)
            ):
                run.append(words[cursor])
                cursor += 1

            trimmed, honorific = self._trim_run(text, run)
            if trimmed and (honorific or len(trimmed) >= 2):
                start, end = trimmed[0].start, trimmed[-1].end
                found.append((Entity(text=text[start:end], kind=PERSON), (start, end)))
            index = cursor
        return found

    def _trim_run(self, text: str, run: list[_Word]) -> tuple[list[_Word], bool]:
        """Drop leading honorifics/stopwords/initials; report whether a title led."""
        honorific = False
        start = 0
        while start < len(run):
            token = run[start].text
            folded = token.casefold()
            if folded in _HONORIFICS:
                honorific = True
                start += 1
                continue
            if len(token) < _MIN_NAME_TOKEN_CHARS:
                start += 1
                continue
            if start == 0 and folded in _SENTENCE_INITIAL_STOPWORDS and _is_sentence_initial(text, run[0].start):
                start += 1
                continue
            break
        # Truncate (never filter) at a placeholder word: dropping one from the
        # middle would leave a span that still covers it, and the entity text
        # would then contain an already-inserted pseudonym.
        trimmed: list[_Word] = []
        for word in run[start:]:
            if _is_reserved(word.text):
                break
            trimmed.append(word)
        return trimmed, honorific

    # -- pass 3 ------------------------------------------------------------

    def _inflected_variants(
        self,
        words: list[_Word],
        entities: list[Entity],
        covered: list[tuple[int, int]],
    ) -> list[Entity]:
        stems = {_stem(normalize(entity.text)) for entity in entities if entity.kind == PERSON}
        stems = {stem for stem in stems if len(stem) >= ALIAS_MIN_PREFIX}
        if not stems:
            return []
        found: list[Entity] = []
        seen: set[str] = set()
        for word in words:
            if not word.text[:1].isupper() or _overlaps(word, covered) or _is_reserved(word.text):
                continue
            if word.text in seen:
                continue
            folded = normalize(word.text)
            if any(_inflection_pair(folded, stem) for stem in stems):
                seen.add(word.text)
                found.append(Entity(text=word.text, kind=PERSON))
        return found


def _overlaps(word: _Word, spans: list[tuple[int, int]]) -> bool:
    return any(start < word.end and word.start < end for start, end in spans)


_DEFAULT_DETECTOR: Detector = RegexDetector()


# --------------------------------------------------------------------------
# substitution
# --------------------------------------------------------------------------


def _entity_kinds(entities: Iterable[Entity]) -> dict[str, str]:
    """Map each usable entity string to its winning kind, first seen first.

    The same string may be reported as a person by one pass and a company by
    another; **person wins**, because leaking a personal name is the more
    sensitive failure (the ``doc_quant`` precedent).
    """
    kinds: dict[str, str] = {}
    for entity in entities:
        text = entity.text
        if not text or not text.strip():
            continue
        if entity.kind not in (PERSON, COMPANY):
            continue
        if _is_reserved(text.strip()):
            continue
        if kinds.get(text) == PERSON:
            continue
        kinds[text] = entity.kind
    return kinds


def _resolve_pseudonyms(entities: Iterable[Entity], *, key: bytes) -> dict[str, str]:
    """Map each unique entity string to the pseudonym that replaces it."""
    kinds = _entity_kinds(entities)
    if not kinds:
        return {}
    normalized = {text: normalize(text) for text in kinds}
    canonicals: dict[str, dict[str, str]] = {
        kind: _alias_canonicals(
            [normalized[text] for text, resolved in kinds.items() if resolved == kind],
            kind=kind,
        )
        for kind in (PERSON, COMPANY)
    }
    return {text: pseudonym(key, kind, canonicals[kind][normalized[text]]) for text, kind in kinds.items()}


def anonymize_markdown(
    text: str,
    *,
    key: bytes,
    detector: Detector | None = None,
) -> AnonymizeResult:
    """Anonymize one markdown document under this instance's pseudonym key.

    URLs collapse to ``**URL**``; email addresses and detected person/company
    entities become stable ``EMAIL_<hmac>`` / ``PERSON_<hmac>`` /
    ``COMPANY_<hmac>`` pseudonyms. The same entity under the same key always
    yields the same token, in every document and on every run; a different key
    yields a disjoint token space.

    Markdown structure is preserved: only matched spans are rewritten, so
    headings, emphasis, lists, tables and link syntax survive verbatim (a
    link's *target* is collapsed to ``**URL**``, its label is not).

    :param text: the document, as markdown or plain text.
    :param key: this instance's HMAC key. Never logged, never embedded in the
        output, never included in an error message. Empty keys are refused.
    :param detector: entity detection override; defaults to
        :class:`RegexDetector`. Read that class's docstring for what the
        default will and will not find.
    :raises ValueError: if ``key`` is empty.
    :raises TypeError: if ``key`` is not bytes.
    """
    if isinstance(key, str):
        raise TypeError("anonymization key must be bytes, not str")
    if not isinstance(key, bytes | bytearray):
        raise TypeError("anonymization key must be bytes")
    if not key:
        raise ValueError("anonymization key must not be empty")
    key = bytes(key)

    replaced = 0

    # Shape detectors first — nothing about them depends on what a detector
    # reported. URLs precede emails, because a URL may carry an "@" in its
    # userinfo or path and the email pass would otherwise eat part of one.
    def _replace_url(match: re.Match[str]) -> str:
        whole = match.group(0)
        return URL_PLACEHOLDER + whole[len(_trim_url(whole)) :]

    text, url_count = _URL_RE.subn(_replace_url, text)
    replaced += url_count

    def _replace_email(match: re.Match[str]) -> str:
        return pseudonym(key, "email", normalize(match.group(0)))

    text, email_count = _EMAIL_RE.subn(_replace_email, text)
    replaced += email_count

    entities = (detector or _DEFAULT_DETECTOR)(text)
    pseudonyms = _resolve_pseudonyms(entities, key=key)
    if not pseudonyms:
        return AnonymizeResult(text=text, replaced=replaced)

    # One pass over a single alternation, longest entity first, so a shorter
    # entity can never match inside a pseudonym a longer one just produced:
    # the replacement text is never rescanned.
    ordered = sorted(pseudonyms, key=len, reverse=True)
    pattern = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(entity) for entity in ordered) + r")(?!\w)")
    text, entity_count = pattern.subn(lambda match: pseudonyms[match.group(0)], text)
    replaced += entity_count

    return AnonymizeResult(text=text, replaced=replaced)
