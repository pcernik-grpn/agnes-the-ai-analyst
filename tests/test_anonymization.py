"""Contract tests for the built-in anonymizer (`src/anonymization.py`).

The properties under test are the ones design spec §9.2 makes load-bearing:
one entity is one pseudonym (within a document and across documents), a
different key is a disjoint token space, emails are stable tokens, URLs are
collapsed and never identity-bearing, and the markdown around all of it
survives.
"""

from __future__ import annotations

import re

import pytest

from src.anonymization import (
    AnonymizeResult,
    Entity,
    RegexDetector,
    anonymize_markdown,
    normalize,
)

KEY = b"0123456789abcdef0123456789abcdef"
OTHER_KEY = b"fedcba9876543210fedcba9876543210"

PERSON_TOKEN = re.compile(r"PERSON_[0-9a-f]{6}")
COMPANY_TOKEN = re.compile(r"COMPANY_[0-9a-f]{6}")
EMAIL_TOKEN = re.compile(r"EMAIL_[0-9a-f]{6}")


def fixed_detector(entities: list[tuple[str, str]]):
    """A stub Detector returning exactly these entities, whatever the text.

    Used where the property under test is the pseudonym scheme itself rather
    than the default detector's recall.
    """

    def detect(_text: str) -> list[Entity]:
        return [Entity(text=text, kind=kind) for text, kind in entities]  # type: ignore[arg-type]

    return detect


# ---------------------------------------------------------------------------
# Czech inflection — the gate this mechanism exists for
# ---------------------------------------------------------------------------

# Replacement is verbatim and case-sensitive, so one Czech name arrives as
# several detected strings.
CZECH_DOC_ONE = "Novák podepsal smlouvu ve středu."
CZECH_DOC_TWO = "S Nováka jsme mluvili v úterý a Novákovi jsme poslali fakturu."


def test_czech_inflected_forms_collapse_to_one_pseudonym():
    result = anonymize_markdown(
        CZECH_DOC_TWO,
        key=KEY,
        detector=fixed_detector([("Nováka", "person"), ("Novákovi", "person")]),
    )
    assert len(set(PERSON_TOKEN.findall(result.text))) == 1, result.text
    assert "Novák" not in result.text
    assert result.replaced == 2


def test_one_person_keeps_one_pseudonym_across_two_documents():
    # Three spellings between two documents, one person: the token a
    # downstream consumer joins on has to be the same in both.
    first = anonymize_markdown(CZECH_DOC_ONE, key=KEY, detector=fixed_detector([("Novák", "person")]))
    second = anonymize_markdown(
        CZECH_DOC_TWO,
        key=KEY,
        detector=fixed_detector([("Nováka", "person"), ("Novákovi", "person")]),
    )
    assert set(PERSON_TOKEN.findall(first.text)) == set(PERSON_TOKEN.findall(second.text))


def test_czech_inflection_end_to_end_with_the_default_detector():
    # Same property, but letting RegexDetector find the names itself. Each
    # document spells the full name at least once, which is what the default
    # detector needs to anchor its inflected-variant pass.
    doc_one = "Jan Novák podepsal smlouvu. S Nováka jsme mluvili v úterý."
    doc_two = "Novákovi jsme poslali fakturu. Jan Novák ji schválil."

    first = anonymize_markdown(doc_one, key=KEY)
    second = anonymize_markdown(doc_two, key=KEY)

    assert len(set(PERSON_TOKEN.findall(first.text))) == 1, first.text
    assert len(set(PERSON_TOKEN.findall(second.text))) == 1, second.text
    assert set(PERSON_TOKEN.findall(first.text)) == set(PERSON_TOKEN.findall(second.text))
    assert "Novák" not in first.text and "Novák" not in second.text


def test_unrelated_names_do_not_share_a_pseudonym():
    result = anonymize_markdown(
        "Novák a Dvořák podepsali.",
        key=KEY,
        detector=fixed_detector([("Novák", "person"), ("Dvořák", "person")]),
    )
    assert len(set(PERSON_TOKEN.findall(result.text))) == 2, result.text


def test_two_full_names_sharing_a_surname_stay_two_people():
    result = anonymize_markdown(
        "Jan Novák a Petr Novák podepsali.",
        key=KEY,
        detector=fixed_detector([("Jan Novák", "person"), ("Petr Novák", "person")]),
    )
    assert len(set(PERSON_TOKEN.findall(result.text))) == 2, result.text


# ---------------------------------------------------------------------------
# determinism, stability, key rotation
# ---------------------------------------------------------------------------


def test_the_same_entity_gets_the_same_pseudonym_everywhere_in_a_document():
    result = anonymize_markdown(
        "Keboola invoiced us; Keboola then called.",
        key=KEY,
        detector=fixed_detector([("Keboola", "company")]),
    )
    tokens = COMPANY_TOKEN.findall(result.text)
    assert len(tokens) == 2 and len(set(tokens)) == 1
    assert result.replaced == 2


def test_anonymization_is_deterministic_across_runs():
    text = "# Notes\n\nJan Novák wrote to jan@example.com about https://example.com/x."
    first = anonymize_markdown(text, key=KEY)
    second = anonymize_markdown(text, key=KEY)
    assert first == second
    assert isinstance(first, AnonymizeResult)


def test_pseudonyms_ignore_case_and_internal_whitespace():
    spaced = anonymize_markdown("Jan  Novak signed.", key=KEY, detector=fixed_detector([("Jan  Novak", "person")]))
    plain = anonymize_markdown("JAN NOVAK signed.", key=KEY, detector=fixed_detector([("JAN NOVAK", "person")]))
    assert set(PERSON_TOKEN.findall(spaced.text)) == set(PERSON_TOKEN.findall(plain.text))


def test_key_rotation_produces_disjoint_pseudonyms():
    text = "Jan Novak works at Keboola. Mail jan@example.com."
    entities = fixed_detector([("Jan Novak", "person"), ("Keboola", "company")])
    ours = anonymize_markdown(text, key=KEY, detector=entities)
    theirs = anonymize_markdown(text, key=OTHER_KEY, detector=entities)

    assert set(PERSON_TOKEN.findall(ours.text)).isdisjoint(PERSON_TOKEN.findall(theirs.text))
    assert set(COMPANY_TOKEN.findall(ours.text)).isdisjoint(COMPANY_TOKEN.findall(theirs.text))
    assert set(EMAIL_TOKEN.findall(ours.text)).isdisjoint(EMAIL_TOKEN.findall(theirs.text))


def test_person_wins_a_kind_conflict():
    result = anonymize_markdown(
        "Ford signed the contract.",
        key=KEY,
        detector=fixed_detector([("Ford", "company"), ("Ford", "person")]),
    )
    assert PERSON_TOKEN.search(result.text)
    assert not COMPANY_TOKEN.search(result.text)


# ---------------------------------------------------------------------------
# emails and URLs
# ---------------------------------------------------------------------------


def test_emails_become_stable_pseudonyms():
    text = "Write to jan.novak@example.com, or again to JAN.NOVAK@Example.com, or to someone.else@example.org."
    result = anonymize_markdown(text, key=KEY, detector=fixed_detector([]))
    tokens = EMAIL_TOKEN.findall(result.text)
    assert len(tokens) == 3
    assert len(set(tokens)) == 2, result.text  # the two spellings are one address
    assert "@" not in result.text
    assert result.replaced == 3


def test_email_pseudonyms_are_stable_across_documents():
    one = anonymize_markdown("Contact: a@example.com", key=KEY, detector=fixed_detector([]))
    two = anonymize_markdown("Cc a@example.com again", key=KEY, detector=fixed_detector([]))
    assert EMAIL_TOKEN.findall(one.text) == EMAIL_TOKEN.findall(two.text)


def test_urls_collapse_and_are_never_identity_bearing():
    text = (
        "See https://acme.example.com/cases/3979-4561 and www.acme.example.com and https://other.example.org/pricing."
    )
    result = anonymize_markdown(text, key=KEY, detector=fixed_detector([]))
    assert result.text.count("**URL**") == 3
    assert "acme" not in result.text and "3979" not in result.text
    # Every URL collapses to the SAME marker — two documents citing one host
    # must not join on it.
    assert len(set(re.findall(r"\*\*URL\*\*", result.text))) == 1


def test_url_keeps_trailing_sentence_punctuation():
    result = anonymize_markdown("See https://example.com/x.", key=KEY, detector=fixed_detector([]))
    assert result.text == "See **URL**."


def test_a_url_carrying_an_at_sign_is_not_eaten_by_the_email_pass():
    result = anonymize_markdown("Open https://user@example.com/path now.", key=KEY, detector=fixed_detector([]))
    assert result.text == "Open **URL** now."


# ---------------------------------------------------------------------------
# markdown structure
# ---------------------------------------------------------------------------


def test_markdown_structure_survives():
    text = (
        "# Zápis z jednání\n"
        "\n"
        "## Účastníci\n"
        "\n"
        "- **Jan Novák** (Acme Trading s.r.o.)\n"
        "- kontakt: jan.novak@example.com\n"
        "\n"
        "Podrobnosti jsou na [našem webu](https://acme.example.com/zapis).\n"
        "\n"
        "| Položka | Částka |\n"
        "| --- | --- |\n"
        "| Licence | 1000 |\n"
    )
    result = anonymize_markdown(text, key=KEY)

    lines = result.text.splitlines()
    assert lines[0] == "# Zápis z jednání"
    assert "## Účastníci" in lines
    assert any(line.startswith("- **PERSON_") for line in lines), result.text
    assert "[našem webu](**URL**)" in result.text
    assert "| Položka | Částka |" in result.text
    assert "| --- | --- |" in result.text
    assert "| Licence | 1000 |" in result.text
    # and the sensitive parts are actually gone
    assert "Novák" not in result.text
    assert "jan.novak@example.com" not in result.text
    assert "acme.example.com" not in result.text


# ---------------------------------------------------------------------------
# the default detector
# ---------------------------------------------------------------------------


def test_regex_detector_finds_a_company_by_its_legal_form():
    entities = RegexDetector()("Fakturu vystavila Acme Trading s.r.o. dne 3. 4.")
    assert Entity(text="Acme Trading s.r.o.", kind="company") in entities


def test_regex_detector_finds_a_titled_single_token_name():
    entities = RegexDetector()("Schůzku vedl Ing. Novák.")
    assert Entity(text="Novák", kind="person") in entities


def test_regex_detector_ignores_a_sentence_initial_preposition():
    # "S Nováka" must not be read as a two-word name.
    entities = RegexDetector()("Jan Novák přišel. S Nováka jsme mluvili.")
    texts = {entity.text for entity in entities}
    assert "S Nováka" not in texts
    assert "Jan Novák" in texts
    assert "Nováka" in texts


def test_regex_detector_does_not_rewrite_its_own_pseudonyms():
    # Two pseudonyms side by side must not look like a two-token name run.
    once = anonymize_markdown("Jan Novák and Petra Svobodová met.", key=KEY)
    twice = anonymize_markdown(once.text, key=KEY)
    assert twice.text == once.text
    assert twice.replaced == 0


# ---------------------------------------------------------------------------
# key handling
# ---------------------------------------------------------------------------


def test_empty_key_is_refused():
    with pytest.raises(ValueError):
        anonymize_markdown("Anything.", key=b"")


def test_a_str_key_is_refused():
    with pytest.raises(TypeError):
        anonymize_markdown("Anything.", key="not-bytes")  # type: ignore[arg-type]


def test_the_key_never_appears_in_the_output():
    key = b"SUPER-SECRET-KEY-VALUE"
    result = anonymize_markdown("Jan Novák wrote to jan@example.com.", key=key)
    assert "SUPER-SECRET" not in result.text
    assert key.decode() not in result.text


def test_the_key_never_appears_in_the_error_message():
    key = b"SUPER-SECRET-KEY-VALUE"
    with pytest.raises(TypeError) as excinfo:
        anonymize_markdown("Anything.", key=key.decode())  # type: ignore[arg-type]
    assert "SUPER-SECRET" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# the detector boundary
# ---------------------------------------------------------------------------


def test_a_failing_detector_propagates_rather_than_degrading():
    # A detector that cannot run must NOT be swallowed into a regex-only or
    # entity-free pass: that would hand back a document reporting a redaction
    # nobody performed. The pipeline has no exception handling, by design.
    class DetectionUnavailable(RuntimeError):
        pass

    def broken(_text: str) -> list[Entity]:
        raise DetectionUnavailable("model unreachable")

    with pytest.raises(DetectionUnavailable):
        anonymize_markdown("Jan Novák podepsal.", key=KEY, detector=broken)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_normalize_folds_case_and_collapses_whitespace():
    assert normalize("  Jan\n Novák ") == "jan novák"


def test_nothing_detected_leaves_the_text_untouched():
    text = "Nothing to see here.\n"
    result = anonymize_markdown(text, key=KEY, detector=fixed_detector([]))
    assert result == AnonymizeResult(text=text, replaced=0)


@pytest.mark.parametrize(
    "hostile",
    [
        "a" * 5000 + "@",
        "a" * 5000 + "@" + "b" * 5000,
        "https://" + "a" * 5000,
        "www." + "." * 5000,
        ("Novák " * 2000),
    ],
)
def test_pathological_input_stays_fast(hostile: str):
    # Security playbook §5: every regex here runs over untrusted document text.
    import time

    start = time.monotonic()
    anonymize_markdown(hostile, key=KEY)
    assert time.monotonic() - start < 2.0, "anonymizer took too long — not linear-time"
