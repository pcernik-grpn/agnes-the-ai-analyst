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
    CustomTermError,
    DeterministicRules,
    Entity,
    RegexDetector,
    anonymize_markdown,
    compile_custom_terms,
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


# ---------------------------------------------------------------------------
# Deterministic identifier tiers (phones, IBANs, national ids)
#
# What these pin is precision as much as recall. Each tier's whole claim is
# "decidable from characters alone", so a test that only proved it catches
# the identifier — and never that it leaves an invoice number alone — would
# be passing a detector that redacts the corpus into uselessness.
# ---------------------------------------------------------------------------

PHONE_TOKEN = re.compile(r"PHONE_[0-9a-f]{6}")
IBAN_TOKEN = re.compile(r"IBAN_[0-9a-f]{6}")
ID_TOKEN = re.compile(r"ID_[0-9a-f]{6}")
TERM_TOKEN = re.compile(r"TERM_[0-9a-f]{6}")

# A valid Czech IBAN (mod-97 checks out) in both written forms.
IBAN_GROUPED = "CZ65 0800 0000 1920 0014 5399"
IBAN_SOLID = "CZ6508000000192000145399"
# A valid Czech birth number (date field + mod-11 both pass).
RC_SLASHED = "760419/0341"
RC_SOLID = "7604190341"

NO_DETECTOR = fixed_detector([])


def anon(text: str, *, key: bytes = KEY, **rule_kwargs) -> AnonymizeResult:
    """Anonymize with the entity detector silenced and rules PINNED.

    Explicit rules, never the config-resolved default: these tests are about
    the tiers themselves, and a test whose behaviour depended on whatever
    `instance.yaml` the runner happened to have would be testing the
    environment.
    """
    return anonymize_markdown(
        text,
        key=key,
        detector=NO_DETECTOR,
        rules=DeterministicRules(**rule_kwargs),
    )


@pytest.mark.parametrize(
    "written",
    [
        "+420 776 041 900",
        "+420776041900",
        "+420-776-041-900",
        "00420 776 041 900",
        "+1 (555) 123-4567",
        "+44 20 7123 4567",
        "776 041 900",
        "776.041.900",
    ],
)
def test_phone_numbers_are_tokenized(written: str):
    result = anon(f"Volejte na {written}, prosím.")
    assert written not in result.text
    assert PHONE_TOKEN.search(result.text)
    assert result.counts["phone"] == 1


def test_the_two_ways_of_writing_one_international_number_share_a_token():
    result = anon("+420 776 041 900 a +420776041900")
    tokens = set(PHONE_TOKEN.findall(result.text))
    assert len(tokens) == 1, "one number written two ways must be one pseudonym"


def test_a_national_and_an_international_form_are_two_tokens():
    # A documented limit, pinned so it cannot change silently: unifying them
    # would mean assuming a default country.
    result = anon("776 041 900 a +420 776 041 900")
    assert len(set(PHONE_TOKEN.findall(result.text))) == 2


@pytest.mark.parametrize(
    "not_a_phone",
    [
        "Faktura 2026001234 byla uhrazena.",
        "Objednávka 900123456 je vyřízena.",
        "Verze 1.2.3 vyšla včera.",
        "Rozsah 100-200 kusů.",
    ],
)
def test_ordinary_numbers_are_not_phone_numbers(not_a_phone: str):
    assert anon(not_a_phone).text == not_a_phone


def test_phone_keeps_trailing_sentence_punctuation():
    result = anon("Volejte +420 776 041 900.")
    assert result.text.endswith(".")


@pytest.mark.parametrize("written", [IBAN_GROUPED, IBAN_SOLID])
def test_ibans_are_tokenized(written: str):
    result = anon(f"Platbu pošlete na {written} do pátku.")
    assert written not in result.text
    assert IBAN_TOKEN.search(result.text)
    assert result.counts["iban"] == 1


def test_grouped_and_solid_iban_share_a_token():
    result = anon(f"{IBAN_GROUPED} = {IBAN_SOLID}")
    assert len(set(IBAN_TOKEN.findall(result.text))) == 1


def test_an_iban_runs_into_the_next_word_without_eating_it():
    # The grouped shape ("four alphanumerics after a space") is also the
    # shape of an ordinary word, so the checksum — not the pattern — has to
    # decide where the account number ends.
    result = anon(f"Účet {IBAN_GROUPED} they said dnes.")
    assert "they said dnes." in result.text
    assert IBAN_TOKEN.search(result.text)


def test_an_iban_shaped_string_that_fails_the_checksum_is_left_alone():
    # The other half of the mod-97 trade, pinned deliberately: a product code
    # is not redacted, and (documented) neither is a mangled account number.
    fake = "AB12 CDEF GHIJ KLMN OPQR"
    assert anon(f"Kód {fake} zde.").text == f"Kód {fake} zde."


def test_the_phone_tier_never_shreds_a_grouped_iban():
    # The ordering contract: IBAN consumes the whole account number before
    # the phone tier can claim the "0000 1920 0014 5399" run inside it.
    result = anon(f"IBAN {IBAN_GROUPED}.")
    assert "phone" not in result.counts
    assert result.counts["iban"] == 1


@pytest.mark.parametrize("written", [RC_SLASHED, "760419 / 0341", RC_SOLID])
def test_czech_birth_numbers_are_tokenized(written: str):
    result = anon(f"Rodné číslo {written} je v žádosti.")
    assert "760419" not in result.text
    assert ID_TOKEN.search(result.text)
    assert result.counts["national_id"] == 1


def test_slashed_and_solid_birth_numbers_share_a_token():
    result = anon(f"{RC_SLASHED} = {RC_SOLID}")
    assert len(set(ID_TOKEN.findall(result.text))) == 1


@pytest.mark.parametrize(
    "not_an_id",
    [
        "Účet 7604190342 je jiný.",  # ten digits, mod-11 fails
        "Číslo 769919/0341 neexistuje.",  # month 99 is not a month
        "Kód 776041900 je devítimístný.",  # nine digits, no checksum to check
    ],
)
def test_numbers_that_are_not_birth_numbers_are_left_alone(not_an_id: str):
    assert anon(not_an_id).text == not_an_id


@pytest.mark.parametrize(
    ("off", "text", "kind"),
    [
        ({"phones": False}, "+420 776 041 900", "phone"),
        # `phones` off too: with the IBAN tier disabled, the digit run inside
        # a grouped account number is no longer claimed by anything ahead of
        # the phone tier. Pinned as its own test below rather than hidden.
        ({"ibans": False, "phones": False}, IBAN_GROUPED, "iban"),
        ({"national_ids": False}, RC_SLASHED, "national_id"),
    ],
)
def test_each_tier_can_be_turned_off_individually(off: dict, text: str, kind: str):
    assert anon(text, **off).text == text
    assert kind not in anon(text, **off).counts
    # ...and with the default rules the same text IS redacted.
    assert anon(text).counts[kind] == 1


def test_turning_the_iban_tier_off_hands_its_digits_to_the_phone_tier():
    # Known and accepted: the tiers are ordered so the wider shape wins, and
    # removing the wider one exposes the narrower match underneath. It errs
    # toward MORE redaction, never less — the safe direction — but it is
    # surprising enough to pin as documented behaviour rather than leave for
    # someone to discover.
    result = anon(IBAN_GROUPED, ibans=False)
    assert PHONE_TOKEN.search(result.text)
    assert "iban" not in result.counts


def test_the_tiers_default_to_on():
    from src.anonymization import DEFAULT_RULES

    assert (DEFAULT_RULES.phones, DEFAULT_RULES.ibans, DEFAULT_RULES.national_ids) == (True, True, True)
    assert DEFAULT_RULES.custom_terms == ()


# ---------------------------------------------------------------------------
# Custom terms
# ---------------------------------------------------------------------------


def test_a_custom_term_is_tokenized():
    result = anon("Projekt Fénix začal v květnu.", custom_terms=("Projekt Fénix",))
    assert "Fénix" not in result.text
    assert TERM_TOKEN.search(result.text)
    assert result.counts["term"] == 1


def test_a_custom_term_matches_regardless_of_case_and_keeps_one_token():
    result = anon("Projekt Fénix a PROJEKT FÉNIX", custom_terms=("projekt fénix",))
    assert len(set(TERM_TOKEN.findall(result.text))) == 1
    assert result.counts["term"] == 2


def test_a_custom_term_respects_word_boundaries():
    result = anon("Acme a Acmerica", custom_terms=("Acme",))
    assert "Acmerica" in result.text
    assert result.counts["term"] == 1


def test_a_trailing_wildcard_covers_the_identifier_family():
    result = anon("ACME-1234 a ACME-9 a ACME", custom_terms=("ACME-*",))
    assert "ACME-1234" not in result.text and "ACME-9" not in result.text
    # The bare "ACME" is NOT covered: the literal part is "ACME-".
    assert result.text.endswith("ACME")
    assert len(set(TERM_TOKEN.findall(result.text))) == 2


def test_a_longer_term_wins_over_a_shorter_one_it_contains():
    result = anon("Projekt Fénix", custom_terms=("Fénix", "Projekt Fénix"))
    assert result.counts["term"] == 1


@pytest.mark.parametrize(
    "unsafe",
    [
        r"\d{3}",
        "(foo|bar)",
        "^start",
        "end$",
        "a.*b",
        "[A-Z]+",
        "x{2,}",
    ],
)
def test_regex_from_config_is_refused(unsafe: str):
    # Security playbook §5: a pattern from config runs over every document of
    # every crawl. It is refused BY NAME, never quietly treated as a literal
    # that happens to match nothing.
    with pytest.raises(CustomTermError) as excinfo:
        compile_custom_terms([unsafe])
    assert "custom_terms" in str(excinfo.value)
    assert repr(unsafe) in str(excinfo.value)


@pytest.mark.parametrize(
    "rejected",
    ["a", "", "   ", "*", "x" * 129],
)
def test_terms_that_would_redact_too_much_or_too_little_are_refused(rejected: str):
    with pytest.raises(CustomTermError):
        compile_custom_terms([rejected])


def test_a_placeholder_word_may_not_be_a_custom_term():
    for word in ("PERSON", "EMAIL", "TERM", "URL", "ID"):
        with pytest.raises(CustomTermError):
            compile_custom_terms([word])


def test_a_non_string_term_is_refused_by_position():
    with pytest.raises(CustomTermError) as excinfo:
        compile_custom_terms(["fine", 42])
    assert "custom_terms[1]" in str(excinfo.value)


def test_too_many_terms_are_refused():
    with pytest.raises(CustomTermError):
        compile_custom_terms([f"term{i:04d}" for i in range(201)])


def test_no_terms_compiles_to_nothing():
    assert compile_custom_terms([]) is None


def test_dots_in_a_term_are_literal_dots():
    pattern = compile_custom_terms(["a.s.co"])
    assert pattern is not None
    assert pattern.search("a.s.co") is not None
    assert pattern.search("axsxco") is None


# ---------------------------------------------------------------------------
# Ordering, accounting and the re-anonymization no-op
# ---------------------------------------------------------------------------

MIXED_DOC = (
    "Kontakt: jan.novak@example.com, https://example.com/a.\n"
    f"Tel +420 776 041 900, účet {IBAN_GROUPED}, RČ {RC_SLASHED}.\n"
    "Projekt Fénix vede Ing. Jan Novák.\n"
)


def test_every_tier_fires_and_is_counted_separately():
    result = anonymize_markdown(
        MIXED_DOC,
        key=KEY,
        rules=DeterministicRules(custom_terms=("Projekt Fénix",)),
    )
    assert result.counts["url"] == 1
    assert result.counts["email"] == 1
    assert result.counts["iban"] == 1
    assert result.counts["national_id"] == 1
    assert result.counts["phone"] == 1
    assert result.counts["term"] == 1
    assert result.replaced == sum(result.counts.values())


def test_re_anonymizing_the_output_changes_nothing():
    # The property the whole placeholder screen exists for: a re-crawl, a
    # retry, or an admin pasting an already-anonymized sample into the
    # preview panel must be a no-op, not a second layer of tokens.
    rules = DeterministicRules(custom_terms=("Projekt Fénix",))
    once = anonymize_markdown(MIXED_DOC, key=KEY, rules=rules)
    twice = anonymize_markdown(once.text, key=KEY, rules=rules)
    assert twice.text == once.text
    assert twice.replaced == 0
    assert twice.counts == {}


def test_the_result_reports_pseudonyms_and_never_their_originals():
    result = anonymize_markdown(
        MIXED_DOC,
        key=KEY,
        rules=DeterministicRules(custom_terms=("Projekt Fénix",)),
    )
    kinds = {kind for kind, _ in result.pseudonyms}
    assert {"email", "iban", "national_id", "phone", "term"} <= kinds
    # URLs collapse to a marker, so they are counted but have no pseudonym.
    assert "url" not in kinds
    for _, token in result.pseudonyms:
        assert token in result.text
        assert re.fullmatch(r"[A-Z]+_[0-9a-f]{6}", token)


def test_the_new_prefixes_are_screened_out_of_the_entity_pass():
    # A pseudonym must never be re-detected as a name by the entity tier.
    detector = fixed_detector([("PHONE_1a2b3c", "person"), ("IBAN_1a2b3c", "company")])
    text = "PHONE_1a2b3c a IBAN_1a2b3c."
    assert anonymize_markdown(text, key=KEY, detector=detector).text == text


def test_the_ner_placeholder_screen_knows_every_prefix_this_module_inserts():
    # Two independent screens by design (see anonymization_ner's comment) —
    # which only works while they say the same thing. This is the pin that
    # makes a new tier here fail loudly there rather than leak a token the
    # model may report as a name.
    from src.anonymization import PLACEHOLDER_WORDS
    from src.anonymization_ner import _PLACEHOLDER_BARE, _PLACEHOLDER_RE

    assert _PLACEHOLDER_BARE == PLACEHOLDER_WORDS
    for prefix in PLACEHOLDER_WORDS - {"URL"}:
        assert _PLACEHOLDER_RE.match(f"{prefix}_1a2b3c"), f"NER screen misses {prefix}_ tokens"


@pytest.mark.parametrize(
    "hostile",
    [
        "+" + "1" * 5000,
        "0" * 5000,
        ("CZ65 " + "0800 " * 2000),
        ("760419/0341 " * 2000),
        ("+420 776 041 900 " * 2000),
    ],
)
def test_pathological_identifier_input_stays_fast(hostile: str):
    # Security playbook §5, for the new tiers: every one of them is a single
    # bounded run plus a Python-side validator, precisely so there is nothing
    # for the engine to backtrack over.
    import time

    start = time.monotonic()
    anonymize_markdown(hostile, key=KEY, detector=NO_DETECTOR, rules=DeterministicRules())
    assert time.monotonic() - start < 2.0, "identifier tiers took too long — not linear-time"


def test_custom_terms_matching_stays_fast_with_a_full_list():
    import time

    terms = tuple(f"kodove-jmeno-{i:04d}" for i in range(200))
    hostile = "kodove-jmeno-0199 " * 2000
    start = time.monotonic()
    anonymize_markdown(hostile, key=KEY, detector=NO_DETECTOR, rules=DeterministicRules(custom_terms=terms))
    assert time.monotonic() - start < 3.0, "custom-term alternation took too long"


# ---------------------------------------------------------------------------
# Config resolution
#
# `anonymize_markdown` resolves these itself rather than making every caller
# pass them, which is what lets the crawl pick up a new toggle without being
# taught about it. That convenience is only safe while the defaults are the
# safe ones, so this is what pins them.
# ---------------------------------------------------------------------------


def _stub_get_value(overrides: dict):
    def get_value(*keys, default=None):
        return overrides.get(tuple(keys), default)

    return get_value


def test_config_resolution_defaults_every_tier_on(monkeypatch):
    import app.instance_config as instance_config
    from src.anonymization import rules_from_config

    monkeypatch.setattr(instance_config, "get_value", _stub_get_value({}))
    rules = rules_from_config()
    assert (rules.phones, rules.ibans, rules.national_ids) == (True, True, True)
    assert rules.custom_terms == ()


def test_config_resolution_reads_each_toggle(monkeypatch):
    import app.instance_config as instance_config
    from src.anonymization import rules_from_config

    monkeypatch.setattr(
        instance_config,
        "get_value",
        _stub_get_value(
            {
                ("extraction", "anonymization", "detect", "phones"): False,
                ("extraction", "anonymization", "detect", "ibans"): True,
                ("extraction", "anonymization", "detect", "national_ids"): False,
                ("extraction", "anonymization", "custom_terms"): ["Projekt Fénix"],
            }
        ),
    )
    rules = rules_from_config()
    assert (rules.phones, rules.ibans, rules.national_ids) == (False, True, False)
    assert rules.custom_terms == ("Projekt Fénix",)


def test_an_unreadable_config_still_redacts(monkeypatch):
    # The safe direction: an instance whose config cannot be read must not
    # silently redact LESS than one whose config is fine.
    import app.instance_config as instance_config
    from src.anonymization import rules_from_config

    def explode(*_keys, **_kw):
        raise RuntimeError("overlay unreadable")

    monkeypatch.setattr(instance_config, "get_value", explode)
    rules = rules_from_config()
    assert (rules.phones, rules.ibans, rules.national_ids) == (True, True, True)


def test_custom_terms_that_is_not_a_list_is_refused(monkeypatch):
    import app.instance_config as instance_config
    from src.anonymization import rules_from_config

    monkeypatch.setattr(
        instance_config,
        "get_value",
        _stub_get_value({("extraction", "anonymization", "custom_terms"): "Projekt Fénix"}),
    )
    with pytest.raises(CustomTermError):
        rules_from_config()
