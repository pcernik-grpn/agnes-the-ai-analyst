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

Substitutions are applied in this order, and the order is a contract:

1. **URLs** collapse to a fixed ``**URL**`` marker. A URL names a host, not a
   party, so it is deliberately *not* identity-bearing: two documents citing
   the same address must not thereby join into one subject.
2. **Email addresses** become ``EMAIL_<hmac>``. An address is the strongest
   join key a person has, so it earns a stable token rather than a marker —
   and replacing the whole address as one unit is what stops a local part
   (usually a person's name) surviving next to a redacted domain.
3. **Deterministic identifier tiers**, each individually toggleable
   (:class:`DeterministicRules`) and each defaulting to ON: IBANs
   (``IBAN_<hmac>``), national ids (``ID_<hmac>``), phone numbers
   (``PHONE_<hmac>``) — in that order, because the wider shape has to win.
   A grouped IBAN (``CZ65 0800 0000 1920 0014 5399``) contains a run that
   reads as an international phone number to the phone tier, so the IBAN
   tier consumes it first; the other order shreds the IBAN into a redacted
   fragment plus a surviving one.
4. **Admin-supplied custom terms** become ``TERM_<hmac>`` — the operator's
   own vocabulary (a project codename, an internal system, a partner name)
   no general detector can be expected to know. Literal text only; see
   :func:`compile_custom_terms` for why config may not carry a regex.
5. **Entities** (persons, companies) become ``PERSON_<hmac>`` /
   ``COMPANY_<hmac>``.

Steps 3–5 run after 1–2 on purpose: an email's local part is often a name or
a number, and collapsing the whole address first means the later tiers never
see — and never half-rewrite — its pieces.

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
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal

__all__ = [
    "AnonymizeResult",
    "CustomTermError",
    "DEFAULT_RULES",
    "Detector",
    "DeterministicRules",
    "Entity",
    "EntityKind",
    "PLACEHOLDER_PREFIXES",
    "PLACEHOLDER_WORDS",
    "PSEUDONYM_DIGEST_CHARS",
    "PSEUDONYM_PREFIXES",
    "RegexDetector",
    "anonymize_markdown",
    "compile_custom_terms",
    "normalize",
    "pseudonym",
    "rules_from_config",
]

PERSON: Literal["person"] = "person"
COMPANY: Literal["company"] = "company"

#: Kinds produced by the deterministic tiers. Not part of ``EntityKind``:
#: a *detector* never reports these — they are decided from characters
#: alone, before any detector runs.
EMAIL = "email"
PHONE = "phone"
IBAN = "iban"
NATIONAL_ID = "national_id"
TERM = "term"
URL = "url"

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
    #: Total substitutions made — URL collapses + every token kind below.
    #: Counts *occurrences*, not distinct entities.
    replaced: int
    #: Occurrences per kind (``"url"``, ``"email"``, ``"iban"``,
    #: ``"national_id"``, ``"phone"``, ``"term"``, ``"person"``,
    #: ``"company"``). Only kinds that actually fired appear. Same accounting
    #: as ``replaced``, split — which is what a preview surface renders and
    #: what a crawl report can total without re-deriving anything.
    counts: dict[str, int] = field(default_factory=dict)
    #: The distinct pseudonyms this document produced, as ``(kind,
    #: pseudonym)`` sorted pairs. Deliberately NOT the values behind them:
    #: this is the one field a preview endpoint may echo back to a caller,
    #: so it must be incapable of carrying an original. URLs have no
    #: pseudonym (they collapse to a fixed marker) and are absent here,
    #: though they are counted above.
    pseudonyms: list[tuple[str, str]] = field(default_factory=list)


# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

#: URLs are collapsed, never tokenized — see the module docstring.
URL_PLACEHOLDER = "**URL**"

#: The word a pseudonym starts with, before its ``_<digest>`` tail. Every
#: kind this module can substitute has an entry — the placeholder screen,
#: the reserved-word list and the "already anonymized" recognizer are all
#: derived from this ONE mapping, so a new tier cannot be added without its
#: token becoming un-rewritable in the same edit.
PSEUDONYM_PREFIXES: dict[str, str] = {
    PERSON: "PERSON",
    COMPANY: "COMPANY",
    EMAIL: "EMAIL",
    PHONE: "PHONE",
    IBAN: "IBAN",
    NATIONAL_ID: "ID",
    TERM: "TERM",
}

#: The bare prefixes a pseudonym can start with — ``{"PERSON", …, "TERM"}``.
PLACEHOLDER_PREFIXES: frozenset[str] = frozenset(PSEUDONYM_PREFIXES.values())

#: Every word this module inserts, prefixes plus the URL marker's word. The
#: screen ``src/anonymization_ner.py`` keeps on its own side must agree with
#: this set (``tests/test_anonymization.py`` pins that they do).
PLACEHOLDER_WORDS: frozenset[str] = PLACEHOLDER_PREFIXES | {"URL"}

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
_RESERVED_WORDS = PLACEHOLDER_WORDS

#: An already-inserted pseudonym, so a second pass can recognize (and skip)
#: it. Derived from :data:`PLACEHOLDER_PREFIXES` rather than spelled out, so
#: the re-anonymization no-op property extends to a new tier automatically.
_PSEUDONYM_TOKEN_RE = re.compile(
    r"^(?:%s)_[0-9a-f]{%d}$" % ("|".join(sorted(PLACEHOLDER_PREFIXES)), PSEUDONYM_DIGEST_CHARS)
)

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
# deterministic identifier tiers
#
# The default detector's own docstring lists "phone numbers, birth numbers /
# national IDs, IBANs" among what it cannot find, because none of them is a
# NAME — they are decided from characters alone. That makes them the wrong
# job for a detector and the right job for a shape pass, which is what this
# block is: three tiers that run before entity substitution, each
# individually toggleable, each ON by default because it is cheap,
# deterministic, and guards a high-severity identifier.
#
# Every regex here is written the same defensive way as the URL/email ones
# above (security playbook §5): a candidate is matched by ONE unambiguous
# bounded run, then *validated* in Python. Packing the validation into the
# pattern is what makes a phone/IBAN regex backtrack, and a document is
# untrusted input.
# --------------------------------------------------------------------------

#: Separators that may appear inside a written phone number or IBAN. NBSP
#: is in the list because it is what a word processor inserts between the
#: groups of a phone number, and a detector that only knew ASCII space
#: would miss exactly the documents that came out of one.
_NUMBER_SEPARATORS = " \u00a0\u202f.-"

#: An international phone candidate: a ``+`` or ``00`` prefix followed by a
#: single bounded run of digits, separators and parentheses. One greedy
#: character class, so there is nothing for the engine to backtrack over;
#: :func:`_phone_canonical` decides whether the run is really a number.
_PHONE_INTERNATIONAL_RE = re.compile(r"(?<![\w+])(?:\+|00)[\d ()\u00a0\u202f.\-]{6,28}")

#: The Czech national form, written in the 3-3-3 grouping that is the only
#: reason it is safely distinguishable from an order or account number: a
#: bare nine-digit run is NOT matched (see the module's limits in
#: ``docs/anonymization.md``). Leading digit 2–9 — no Czech subscriber
#: number starts with 0 or 1.
_PHONE_CZ_RE = re.compile(r"(?<![\w+])[2-9]\d{2}[ \u00a0\u202f.\-]\d{3}[ \u00a0\u202f.\-]\d{3}(?![\d\-])")

#: Fewest / most digits an international number may carry. E.164 caps a
#: subscriber number at 15 digits including the country code; below 8 the
#: run is a version string, a range, or an amount far more often than it is
#: a number anyone could dial.
_PHONE_MIN_DIGITS = 8
_PHONE_MAX_DIGITS = 15

#: IBAN, in its two written forms, as two branches that cannot both match
#: at one position: solid (no separators at all) or grouped in fours. The
#: grouped branch's separator is MANDATORY inside the repetition and its
#: group size is fixed, so there is exactly one way to split any candidate.
_IBAN_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?:"
    r"[A-Z]{2}\d{2}[A-Za-z0-9]{11,30}"
    r"|"
    r"[A-Z]{2}\d{2}(?:[ \u00a0\u202f][A-Za-z0-9]{4}){2,7}(?:[ \u00a0\u202f][A-Za-z0-9]{1,3})?"
    r")"
    r"(?![A-Za-z0-9])"
)

#: Czech birth number (rodné číslo): ``YYMMDD/XXX`` (pre-1954, three-digit
#: serial) or ``YYMMDD/XXXX`` (four digits, mod-11 checked). The slash form
#: is matched on its shape; the slash-LESS ten-digit form is matched only
#: when it passes the checksum, because ten consecutive digits is also what
#: a Czech bank account number looks like.
_NATIONAL_ID_SLASHED_RE = re.compile(r"(?<![\w/])(\d{6})\s?/\s?(\d{3,4})(?![\w/])")
_NATIONAL_ID_SOLID_RE = re.compile(r"(?<![\w/])\d{10}(?![\w/])")


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def _phone_canonical(candidate: str) -> str | None:
    """The E.164-ish form a phone candidate hashes under, or ``None``.

    ``None`` means "this run is not a phone number" and the text is left
    exactly as it was — the validation the regex deliberately does not do.

    Canonicalization is what makes ``+420 776 041 900`` and
    ``+420776041900`` one token. It does NOT unify a national form with an
    international one (``776 041 900`` vs ``+420 776 041 900``): that would
    require assuming a default country, and an anonymizer that guesses a
    country prefix produces a pseudonym that is wrong in the one corpus
    where it matters. Two tokens for one phone is a documented limit, not a
    silent one.
    """
    digits = _digits(candidate)
    if not digits:
        return None
    if candidate.lstrip().startswith("+"):
        national = digits
    elif digits.startswith("00"):
        national = digits[2:]
    else:
        # A bare national run — only the 3-3-3 Czech branch reaches here, and
        # it is fixed at nine digits by its own pattern. Canonicalized as it
        # was written, with no country prefix invented for it.
        return digits
    if not _PHONE_MIN_DIGITS <= len(national) <= _PHONE_MAX_DIGITS:
        return None
    return "+" + national


def _iban_checksum_ok(candidate: str) -> bool:
    """ISO 13616 mod-97: rearrange, letters → digits, remainder must be 1.

    Required rather than advisory. ``[A-Z]{2}\\d{2}`` plus a dozen
    alphanumerics is a shape a product code or a document reference can
    also wear, and a mod-97 check costs nothing to run. The cost is on the
    other side and is documented: an OCR-mangled or typo'd account number
    fails the check and is therefore NOT redacted.
    """
    compact = "".join(candidate.split()).upper()
    if not (15 <= len(compact) <= 34) or not compact[:2].isalpha() or not compact[2:4].isdigit():
        return False
    rearranged = compact[4:] + compact[:4]
    total = 0
    for char in rearranged:
        if char.isdigit():
            total = (total * 10 + int(char)) % 97
        elif "A" <= char <= "Z":
            total = (total * 100 + (ord(char) - 55)) % 97
        else:
            return False
    return total == 1


#: How many trailing groups a candidate may shed before it is abandoned.
#: A grouped IBAN written in prose runs straight into the next word, and
#: the "four alphanumerics after a space" shape a real group has is one an
#: ordinary word also has ("... 5399 they said"). Rather than teach the
#: pattern about words, the checksum decides: shed the last group and ask
#: again, at most this many times.
_IBAN_MAX_SHED_GROUPS = 3


def _iban_prefix(candidate: str) -> str | None:
    """The longest leading part of ``candidate`` that is a valid IBAN.

    ``None`` when no prefix validates, which is the ordinary answer for a
    product code or document reference wearing the same shape.
    """
    if _iban_checksum_ok(candidate):
        return candidate
    separators = [m.start() for m in re.finditer(r"[ \u00a0\u202f]", candidate)]
    for cut in reversed(separators[-_IBAN_MAX_SHED_GROUPS:]):
        head = candidate[:cut]
        if _iban_checksum_ok(head):
            return head
    return None


#: Month field of a Czech birth number. +50 marks a woman; +20 and +70 are
#: the exhausted-range offsets in use since 2004.
_RC_MONTH_OFFSETS = (0, 20, 50, 70)


def _national_id_canonical(digits: str) -> str | None:
    """The canonical form of a Czech birth number, or ``None`` if it is not
    one. Canonical = the digits alone, so ``760419/0343`` and
    ``7604190343`` are one token.

    Two independent checks, both required: the date field has to be a real
    date under one of the four month offsets, and a ten-digit number has to
    satisfy the mod-11 rule (with the pre-1985 remainder-10 allowance, which
    was legal and is still on documents held today).
    """
    if len(digits) not in (9, 10) or not digits.isdigit():
        return None
    month = int(digits[2:4])
    if not any(1 <= month - offset <= 12 for offset in _RC_MONTH_OFFSETS):
        return None
    day = int(digits[4:6])
    if not 1 <= day <= 31:
        return None
    if len(digits) == 10:
        remainder = int(digits[:9]) % 11
        if remainder == 10:
            # Legacy allowance: numbers issued before 1985 could end in 0
            # where the modulus said 10.
            if digits[9] != "0":
                return None
        elif remainder != int(digits[9]):
            return None
    return digits


# --------------------------------------------------------------------------
# admin-supplied custom terms
# --------------------------------------------------------------------------


class CustomTermError(ValueError):
    """A configured ``extraction.anonymization.custom_terms`` entry is not
    usable. Raised, never skipped: silently dropping a term an operator
    added would leave them believing something is redacted that is not,
    which is the single failure mode this whole module exists to avoid.
    """


#: Punctuation a literal term may contain. Everything else that is not a
#: letter or a digit is refused — which is what turns "config may not carry
#: a regex" from a hope into a check. `.` is a LITERAL dot here, not "any
#: character"; the error message says so.
_CUSTOM_TERM_PUNCTUATION = frozenset(" .,-_'’&/@#:+")

#: A term shorter than this redacts far more than an operator intends (a
#: one-character term matches somewhere in almost every document), and one
#: longer than this is a sentence rather than an identifier.
_CUSTOM_TERM_MIN_CHARS = 2
_CUSTOM_TERM_MAX_CHARS = 128

#: How many terms one instance may configure. The compiled alternation is
#: walked at every position of every document, so its size is a per-crawl
#: cost an operator should have to opt into deliberately rather than
#: discover.
_CUSTOM_TERM_MAX_COUNT = 200

#: Word characters a trailing ``*`` may stand for. Bounded on purpose: an
#: unbounded quantifier over untrusted text is the ReDoS shape the security
#: playbook §5 forbids, and no identifier this is for has a 64-character
#: tail.
_CUSTOM_TERM_WILDCARD_CHARS = 64


def _validate_custom_term(raw: object, *, index: int) -> tuple[str, bool]:
    """One configured term → ``(literal, has_wildcard)``, or raise.

    The rejection messages name the term, the offending character, and what
    the field actually accepts, because the operator reading them is
    looking at a preview panel or a failed crawl and needs to know which of
    their entries to fix.
    """
    where = f"extraction.anonymization.custom_terms[{index}]"
    if not isinstance(raw, str):
        raise CustomTermError(f"{where}: expected a string, got {type(raw).__name__}")
    term = raw.strip()
    wildcard = term.endswith("*")
    if wildcard:
        term = term[:-1].strip()
    if len(term) < _CUSTOM_TERM_MIN_CHARS:
        raise CustomTermError(
            f"{where}: {raw!r} is too short — a custom term needs at least "
            f"{_CUSTOM_TERM_MIN_CHARS} characters before any trailing '*', or it redacts "
            "most of the document"
        )
    if len(term) > _CUSTOM_TERM_MAX_CHARS:
        raise CustomTermError(f"{where}: longer than {_CUSTOM_TERM_MAX_CHARS} characters")
    for char in term:
        if char.isalnum() or char in _CUSTOM_TERM_PUNCTUATION:
            continue
        raise CustomTermError(
            f"{where}: {raw!r} contains {char!r}, which custom_terms does not accept. "
            "Entries are LITERAL text (a '.' matches a dot, not any character), optionally "
            "with one trailing '*' meaning 'followed by more word characters'. Regular "
            "expressions are refused on purpose — a pattern from config runs over every "
            "document of every crawl, where one badly written quantifier is a denial of "
            "service."
        )
    if term.upper() in PLACEHOLDER_WORDS or _PSEUDONYM_TOKEN_RE.match(term):
        raise CustomTermError(
            f"{where}: {raw!r} is a redaction placeholder this anonymizer inserts itself. "
            "Redacting it again would rewrite an already-anonymized document."
        )
    return term, wildcard


def compile_custom_terms(terms: Sequence[object]) -> re.Pattern[str] | None:
    """Compile configured literal terms into one linear-time alternation.

    ``None`` when there is nothing to match. Longest term first, so a term
    that is a prefix of another can never win over it — the same
    longest-first single-alternation discipline the entity pass uses.

    Matching is case-insensitive (an operator writing ``Projekt Fénix``
    means the ALL-CAPS heading too) and word-bounded, and every term is
    ``re.escape``d, so the compiled pattern contains no operator-supplied
    metacharacter at all. A trailing ``*`` becomes a BOUNDED ``\\w{0,N}``
    run rather than an unbounded quantifier.
    """
    if not terms:
        return None
    if all(isinstance(term, str) for term in terms):
        # The ordinary path: a config-resolved tuple of strings, compiled
        # once per distinct term list rather than once per document. A crawl
        # runs this for every file of a corpus, and building a 200-branch
        # alternation ten thousand times is pure waste.
        return _compiled_custom_terms(tuple(terms))  # type: ignore[arg-type]
    return _compile_custom_terms_uncached(tuple(terms))


@lru_cache(maxsize=8)
def _compiled_custom_terms(terms: tuple[str, ...]) -> re.Pattern[str] | None:
    return _compile_custom_terms_uncached(terms)


def _compile_custom_terms_uncached(terms: tuple[object, ...]) -> re.Pattern[str] | None:
    if len(terms) > _CUSTOM_TERM_MAX_COUNT:
        raise CustomTermError(
            f"extraction.anonymization.custom_terms: {len(terms)} entries exceeds the "
            f"{_CUSTOM_TERM_MAX_COUNT}-term ceiling"
        )
    compiled: list[tuple[str, bool]] = []
    for index, raw in enumerate(terms):
        compiled.append(_validate_custom_term(raw, index=index))
    if not compiled:
        return None
    compiled.sort(key=lambda pair: len(pair[0]), reverse=True)
    branches = [
        re.escape(literal) + (r"\w{0,%d}" % _CUSTOM_TERM_WILDCARD_CHARS if wildcard else "")
        for literal, wildcard in compiled
    ]
    return re.compile(r"(?<!\w)(?:" + "|".join(branches) + r")(?!\w)", re.IGNORECASE)


@dataclass(frozen=True)
class DeterministicRules:
    """Which deterministic tiers run, and what extra literals they redact.

    All three built-in tiers default to **ON**. They are deterministic (no
    model, no network, no per-document cost), they are exact by
    construction (a validated IBAN is an IBAN), and each one guards an
    identifier whose disclosure is more severe than a name's — a phone
    number, a bank account, a national id. An operator turning one off is
    making a considered trade for a corpus where that shape is noise; an
    operator who never touches the block gets the safe answer.
    """

    phones: bool = True
    ibans: bool = True
    national_ids: bool = True
    #: Compiled from ``extraction.anonymization.custom_terms``. Kept as the
    #: raw strings rather than a compiled pattern so this stays a plain,
    #: comparable, hashable value; compilation happens per call, where the
    #: validation error can be reported to whoever asked.
    custom_terms: tuple[str, ...] = ()


#: What an instance that configures nothing gets.
DEFAULT_RULES = DeterministicRules()


def rules_from_config() -> DeterministicRules:
    """Read ``extraction.anonymization.detect`` + ``custom_terms``.

    Lives here, not in the caller, for one reason: the crawl calls
    ``anonymize_markdown`` with a key and a detector and nothing else, so a
    tier toggle resolved in the caller would apply to whichever caller had
    been taught about it. Resolving it at the point of substitution means
    the crawl, the preview endpoint and any future caller are configured by
    the same keys automatically.

    A missing config package or an unreadable overlay yields the defaults —
    tiers ON, no custom terms — which is the safe direction: an instance
    whose config cannot be read must not silently redact LESS. A malformed
    ``custom_terms`` entry is the one thing that raises rather than
    defaulting, because it is a positive instruction that could not be
    honored (see :class:`CustomTermError`).
    """
    try:
        from app.instance_config import get_value
    except Exception:  # noqa: BLE001 — no config package: built-in defaults
        return DEFAULT_RULES

    def _flag(name: str) -> bool:
        try:
            value = get_value("extraction", "anonymization", "detect", name, default=True)
        except Exception:  # noqa: BLE001 — unreadable config: safe default
            return True
        return True if value is None else bool(value)

    try:
        raw_terms = get_value("extraction", "anonymization", "custom_terms", default=None)
    except Exception:  # noqa: BLE001
        raw_terms = None
    if raw_terms is None:
        terms: tuple[str, ...] = ()
    elif isinstance(raw_terms, (list, tuple)):
        terms = tuple(str(term) for term in raw_terms)
    else:
        raise CustomTermError(
            f"extraction.anonymization.custom_terms: expected a list of strings, got {type(raw_terms).__name__}"
        )

    return DeterministicRules(
        phones=_flag("phones"),
        ibans=_flag("ibans"),
        national_ids=_flag("national_ids"),
        custom_terms=terms,
    )


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

    The surname rule is also where an ambiguous bare surname has to be
    refused rather than guessed at: when a bare form ("smith") inflects
    against the surname of *more than one* full name that do not share a
    first token ("john smith" **and** "mary smith"), it is impossible to say
    which person it names, so it joins **neither** — it keeps its own group
    (and its own pseudonym) instead of picking a side. Prefer a missed join
    over a wrong one: merging "john smith" and "mary smith" into one
    pseudonym would make two people look like one, which is a strictly worse
    failure than the bare mention getting an unlinked pseudonym of its own.

    Grouping is transitive (union-find), so the outer pair of a chain need not
    itself satisfy the rule — except at the ambiguity check above, which looks
    at the direct pairing, not the transitive closure, precisely so it can
    catch a bridge before it is built.

    A group hashes under its whole-string longest common prefix when that is
    still ``ALIAS_MIN_PREFIX`` characters or more, otherwise (a group that
    mixes a full name with a bare surname) under its *stem* prefix. This is
    what makes the token stable across documents: a document spelling
    "Jan Novák ... Nováka" and a document spelling only "Nováka ... Novákovi"
    both canonicalize to "novák".

    Known and accepted:

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
        # A bare surname can inflection-match more than one full name in the
        # same bucket ("smith" against both "john smith" and "mary smith").
        # Collect every full name each bare form matches BEFORE unioning
        # anything, so the ambiguity check below sees the whole picture
        # rather than unioning the first match and never getting a chance to
        # notice the second.
        bare_matches: dict[str, list[str]] = {}
        for index, left in enumerate(bucket):
            for right in bucket[index + 1 :]:
                # Exactly one side must be a bare surname; two full names never
                # merge on a shared surname alone.
                if (" " in left) == (" " in right):
                    continue
                if not _inflection_pair(_stem(left), _stem(right)):
                    continue
                bare, full = (left, right) if " " not in left else (right, left)
                bare_matches.setdefault(bare, []).append(full)

        for bare, fulls in bare_matches.items():
            # The full names one bare surname matches are the SAME person
            # only when they agree on a first token — an inflected spelling
            # of one full name never disagrees with itself there. Two (or
            # more) different first tokens means two different people share
            # this surname, and the bare mention cannot say which one it is:
            # leave it — and them — unmerged rather than guess.
            first_tokens = {full.split(" ", 1)[0] for full in fulls}
            if len(first_tokens) > 1:
                continue
            for full in fulls:
                union(bare, full)

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


#: Ordinary horizontal whitespace inside a name run: an ASCII space and the
#: NBSP a word processor inserts between groups (the same separator set the
#: phone/IBAN tiers use).
_RUN_HORIZONTAL_WS = "  "


def _is_run_gap(gap: str, *, extra: str = "") -> bool:
    """True when ``gap`` only keeps a capitalized run visually apart — never
    when it separates two runs on either side of a paragraph.

    A single line break (plus surrounding horizontal whitespace) counts as
    part of the run: PDF/DOCX→markdown conversion wraps lines constantly, so
    a name split across one of those wraps ("Northwind\\nLogistics Holding
    a.s.") is the ordinary case, not an edge case. TWO OR MORE line breaks is
    a blank line — a genuine paragraph boundary in markdown — and is
    deliberately NOT treated as part of a run: a capitalized word ending one
    paragraph and one starting the next are not one name.

    ``extra`` adds caller-specific characters to tolerate (the company pass
    also allows a comma in the gap, e.g. "Acme, Inc.").
    """
    if gap.count("\n") > 1:
        return False
    return gap.strip(_RUN_HORIZONTAL_WS + "\n" + extra) == ""


def _adjacent(text: str, left: _Word, right: _Word) -> bool:
    """True when two words form one run — see :func:`_is_run_gap`.

    An abbreviating full stop is tolerated after an honorific or a
    single-letter initial ("Ing. Novák", "J. Novák"), and nowhere else:
    tolerating it in general would splice the last word of one sentence onto
    the first word of the next.
    """
    gap = text[left.end : right.start]
    if gap == "":
        return False
    if _is_run_gap(gap):
        return True
    abbreviating = left.text.casefold() in _HONORIFICS or len(left.text) == 1
    return abbreviating and gap.startswith(".") and _is_run_gap(gap[1:])


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
    * **Names split across markup** — bold/italic markers or a soft hyphen
      inside a name break the run. A *single* line break does not (see
      :func:`_is_run_gap`) — PDF/DOCX→markdown conversion wraps lines
      constantly, so tolerating one wrap is what keeps an ordinary wrapped
      name from leaking half-redacted; a blank line (two or more breaks)
      still ends the run, because that is a paragraph boundary.
    * **Identifiers that are not names but identify anyway**: addresses,
      account numbers in national (non-IBAN) format, case numbers, VAT ids,
      passport numbers. None are detected HERE. Phone numbers, IBANs and
      Czech birth numbers are handled — but by the deterministic tiers that
      run before this detector (:class:`DeterministicRules`), not by it: a
      shape decidable from characters alone is the wrong job for a name
      heuristic. If your corpus carries any of the shapes still on the list
      above, this detector is not sufficient on its own.
    * **Non-Latin scripts** are effectively untouched, as is any language
      whose orthography does not capitalize proper nouns.

    What it will over-detect (the safer direction, but still wrong):

    * **Place, product, and organization names** written as two capitalized
      words become persons — "New York", "Prague Castle", "Data Apps".
    * **Title-Cased headings** become a person run.

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
                if not _is_run_gap(text[word.end : gap_end], extra=","):
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


def _count_real_substitutions(
    pattern: re.Pattern[str],
    replace: Callable[[re.Match[str]], str],
    text: str,
) -> tuple[str, int]:
    """``pattern.subn`` that counts only the matches actually rewritten.

    The validated tiers deliberately return the matched text unchanged when
    validation fails (a run that looked like an IBAN and is not), and
    ``re.subn`` would count that as a substitution. A counter that inflates
    on the rejects is worse than no counter: the whole point of the preview
    surface is that the number next to a kind is what was really redacted.
    """
    replacements = 0

    def _wrapper(match: re.Match[str]) -> str:
        nonlocal replacements
        out = replace(match)
        if out != match.group(0):
            replacements += 1
        return out

    return pattern.sub(_wrapper, text), replacements


def anonymize_markdown(
    text: str,
    *,
    key: bytes,
    detector: Detector | None = None,
    rules: DeterministicRules | None = None,
) -> AnonymizeResult:
    """Anonymize one markdown document under this instance's pseudonym key.

    URLs collapse to ``**URL**``; email addresses, the deterministic
    identifier tiers, the operator's custom terms and the detected
    person/company entities become stable ``EMAIL_<hmac>`` / ``IBAN_<hmac>``
    / ``ID_<hmac>`` / ``PHONE_<hmac>`` / ``TERM_<hmac>`` / ``PERSON_<hmac>``
    / ``COMPANY_<hmac>`` pseudonyms — in the order the module docstring
    fixes. The same value under the same key always yields the same token, in
    every document and on every run; a different key yields a disjoint token
    space.

    Running this over its own output is a **no-op**: every token it inserts
    is screened out of every later pass (:data:`PLACEHOLDER_WORDS`,
    :data:`_PSEUDONYM_TOKEN_RE`), so a document that is anonymized twice —
    by a re-crawl, a retry, or an admin pasting an anonymized sample into
    the preview panel — is unchanged the second time.

    Markdown structure is preserved: only matched spans are rewritten, so
    headings, emphasis, lists, tables and link syntax survive verbatim (a
    link's *target* is collapsed to ``**URL**``, its label is not).

    :param text: the document, as markdown or plain text.
    :param key: this instance's HMAC key. Never logged, never embedded in the
        output, never included in an error message. Empty keys are refused.
    :param detector: entity detection override; defaults to
        :class:`RegexDetector`. Read that class's docstring for what the
        default will and will not find.
    :param rules: which deterministic tiers run and what custom terms they
        redact. ``None`` — the default, and what the crawl passes — resolves
        them from instance config via :func:`rules_from_config`; pass an
        explicit :class:`DeterministicRules` to pin them.
    :raises ValueError: if ``key`` is empty.
    :raises TypeError: if ``key`` is not bytes.
    :raises CustomTermError: if configured custom terms are unusable.
    """
    if isinstance(key, str):
        raise TypeError("anonymization key must be bytes, not str")
    if not isinstance(key, bytes | bytearray):
        raise TypeError("anonymization key must be bytes")
    if not key:
        raise ValueError("anonymization key must not be empty")
    key = bytes(key)
    rules = rules_from_config() if rules is None else rules

    counts: dict[str, int] = {}
    minted: dict[str, str] = {}

    def _mint(kind: str, canonical: str) -> str:
        token = pseudonym(key, kind, canonical)
        minted[token] = kind
        return token

    def _record(kind: str, count: int) -> None:
        if count:
            counts[kind] = counts.get(kind, 0) + count

    # Shape detectors first — nothing about them depends on what a detector
    # reported. URLs precede emails, because a URL may carry an "@" in its
    # userinfo or path and the email pass would otherwise eat part of one.
    def _replace_url(match: re.Match[str]) -> str:
        whole = match.group(0)
        return URL_PLACEHOLDER + whole[len(_trim_url(whole)) :]

    text, url_count = _URL_RE.subn(_replace_url, text)
    _record(URL, url_count)

    def _replace_email(match: re.Match[str]) -> str:
        return _mint(EMAIL, normalize(match.group(0)))

    text, email_count = _EMAIL_RE.subn(_replace_email, text)
    _record(EMAIL, email_count)

    # IBAN before the id and phone tiers — see the module docstring: a
    # grouped IBAN carries a run the phone tier would otherwise claim.
    if rules.ibans:

        def _replace_iban(match: re.Match[str]) -> str:
            whole = match.group(0)
            candidate = _iban_prefix(whole)
            if candidate is None:
                return whole
            return _mint(IBAN, "".join(candidate.split()).upper()) + whole[len(candidate) :]

        text, iban_count = _count_real_substitutions(_IBAN_RE, _replace_iban, text)
        _record(IBAN, iban_count)

    if rules.national_ids:

        def _replace_slashed_id(match: re.Match[str]) -> str:
            canonical = _national_id_canonical(match.group(1) + match.group(2))
            return match.group(0) if canonical is None else _mint(NATIONAL_ID, canonical)

        def _replace_solid_id(match: re.Match[str]) -> str:
            canonical = _national_id_canonical(match.group(0))
            return match.group(0) if canonical is None else _mint(NATIONAL_ID, canonical)

        text, slashed = _count_real_substitutions(_NATIONAL_ID_SLASHED_RE, _replace_slashed_id, text)
        text, solid = _count_real_substitutions(_NATIONAL_ID_SOLID_RE, _replace_solid_id, text)
        _record(NATIONAL_ID, slashed + solid)

    if rules.phones:

        def _replace_phone(match: re.Match[str]) -> str:
            whole = match.group(0)
            trimmed = whole.rstrip(_NUMBER_SEPARATORS + ")")
            canonical = _phone_canonical(trimmed)
            if canonical is None:
                return whole
            return _mint(PHONE, canonical) + whole[len(trimmed) :]

        text, international = _count_real_substitutions(_PHONE_INTERNATIONAL_RE, _replace_phone, text)
        text, national = _count_real_substitutions(_PHONE_CZ_RE, _replace_phone, text)
        _record(PHONE, international + national)

    term_pattern = compile_custom_terms(rules.custom_terms)
    if term_pattern is not None:
        text, term_count = term_pattern.subn(lambda m: _mint(TERM, normalize(m.group(0))), text)
        _record(TERM, term_count)

    entities = (detector or _DEFAULT_DETECTOR)(text)
    entity_pseudonyms = _resolve_pseudonyms(entities, key=key)
    if entity_pseudonyms:
        kinds_by_text = _entity_kinds(entities)

        # One pass over a single alternation, longest entity first, so a
        # shorter entity can never match inside a pseudonym a longer one just
        # produced: the replacement text is never rescanned.
        ordered = sorted(entity_pseudonyms, key=len, reverse=True)
        pattern = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(entity) for entity in ordered) + r")(?!\w)")

        def _replace_entity(match: re.Match[str]) -> str:
            found = match.group(0)
            token = entity_pseudonyms[found]
            # Bookkeeping only — the token itself was already resolved above,
            # because alias unification needs the whole document's entity set
            # before it can say what any one form hashes under.
            kind = kinds_by_text.get(found, PERSON)
            minted[token] = kind
            _record(kind, 1)
            return token

        text = pattern.sub(_replace_entity, text)

    return AnonymizeResult(
        text=text,
        replaced=sum(counts.values()),
        counts=counts,
        pseudonyms=sorted({(kind, token) for token, kind in minted.items()}),
    )
