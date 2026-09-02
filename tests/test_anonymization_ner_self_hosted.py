"""Self-hosted detector endpoint: config resolution, both allowlist gates, the
configurable output ceiling, and the corroboration gate that closes the
empty-array fail-open class."""

import pytest

from src import anonymization_ner as ner
from src.anonymization_ner import DetectionUnavailable


@pytest.fixture
def cfg(monkeypatch):
    """Drive ``extraction.anonymization.llm.*`` without an instance.yaml."""
    values: dict[tuple, object] = {}

    def fake_get_value(*path, default=None):
        return values.get(tuple(path), default)

    import app.instance_config as ic

    monkeypatch.setattr(ic, "get_value", fake_get_value)

    def setter(**kw):
        for name, value in kw.items():
            values[("extraction", "anonymization", "llm", name)] = value

    return setter


ALLOWED_HOST = "llm.internal:8443"
BASE_URL = f"https://{ALLOWED_HOST}"


def _allow_host(monkeypatch, hosts=ALLOWED_HOST):
    monkeypatch.setenv("AGNES_ANONYMIZATION_LLM_HOST_ALLOWLIST", hosts)


# --- resolution -------------------------------------------------------------


def test_no_base_url_means_no_self_hosted_endpoint(cfg):
    cfg(model="qwen3.5-9b")
    assert ner.self_hosted_endpoint() is None


def test_resolves_when_host_allowlisted(cfg, monkeypatch):
    _allow_host(monkeypatch)
    monkeypatch.setenv("AGNES_ANONYMIZATION_LLM_API_KEY", "sk-local")
    cfg(base_url=BASE_URL, model="qwen3.5-9b")

    endpoint = ner.self_hosted_endpoint()

    assert endpoint is not None
    assert endpoint.base_url == BASE_URL
    assert endpoint.model == "qwen3.5-9b"
    assert endpoint.api_key == "sk-local"


def test_model_is_used_verbatim_not_through_resolve_model_tier(cfg, monkeypatch):
    """A non-``claude-*`` id must survive; ``resolve_model_tier`` would raise."""
    _allow_host(monkeypatch)
    cfg(base_url=BASE_URL, model="qwen3.5-9b")
    assert ner.default_model() == "qwen3.5-9b"


def test_missing_model_is_an_error_not_a_guess(cfg, monkeypatch):
    _allow_host(monkeypatch)
    cfg(base_url=BASE_URL, model="")
    with pytest.raises(DetectionUnavailable, match="model is empty"):
        ner.self_hosted_endpoint()


# --- gate 1: host allowlist (default-closed) --------------------------------


def test_unset_host_allowlist_refuses_every_base_url(cfg, monkeypatch):
    monkeypatch.delenv("AGNES_ANONYMIZATION_LLM_HOST_ALLOWLIST", raising=False)
    cfg(base_url=BASE_URL, model="qwen3.5-9b")
    with pytest.raises(DetectionUnavailable, match="HOST_ALLOWLIST"):
        ner.self_hosted_endpoint()


def test_unlisted_host_is_refused(cfg, monkeypatch):
    _allow_host(monkeypatch)
    cfg(base_url="https://attacker.example/v1", model="qwen3.5-9b")
    with pytest.raises(DetectionUnavailable, match="HOST_ALLOWLIST"):
        ner.self_hosted_endpoint()


def test_bare_host_entry_matches_a_url_carrying_a_port(cfg, monkeypatch):
    _allow_host(monkeypatch, hosts="llm.internal")
    cfg(base_url=BASE_URL, model="qwen3.5-9b")
    assert ner.self_hosted_endpoint() is not None


@pytest.mark.parametrize(
    "entry, url, allowed",
    [
        ("llm.internal:443", "https://llm.internal", True),   # scheme default port
        ("llm.internal:80", "http://llm.internal", True),
        ("llm.internal:443", "http://llm.internal", False),   # other scheme's default
        ("llm.internal:8443", "https://llm.internal:8443", True),
        ("llm.internal", "https://llm.internal:8443", True),  # bare entry, any port
        ("llm.internal:8443", "https://llm.internal:9999", False),  # the pin holds
        ("llm.internal:443", "https://attacker.example", False),
    ],
)
def test_host_matching_handles_default_ports_without_discarding_a_pin(cfg, monkeypatch, entry, url, allowed):
    """An entry naming a port pins it; a url omitting a DEFAULT port still
    matches an entry that spells it out."""
    _allow_host(monkeypatch, hosts=entry)
    cfg(base_url=url, model="qwen3.5-9b")
    if allowed:
        assert ner.self_hosted_endpoint() is not None
    else:
        with pytest.raises(DetectionUnavailable, match="HOST_ALLOWLIST"):
            ner.self_hosted_endpoint()


# --- gate 2: api_key_env allowlist ------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["AGNES_VAULT_KEY", "AGNES_ANONYMIZATION_HMAC_KEY", "SHAREPOINT_CERT_PRIVATE_KEY", "ANTHROPIC_API_KEY"],
)
def test_config_cannot_name_a_secret_from_another_trust_class(cfg, monkeypatch, name):
    """`extraction` is admin-writable, so the NAME is attacker-reachable."""
    _allow_host(monkeypatch)
    monkeypatch.setenv(name, "super-secret")
    cfg(base_url=BASE_URL, model="qwen3.5-9b", api_key_env=name)
    with pytest.raises(DetectionUnavailable, match="not an allowed"):
        ner.self_hosted_endpoint()


def test_endpoint_without_auth_is_legitimate(cfg, monkeypatch):
    _allow_host(monkeypatch)
    monkeypatch.delenv("AGNES_ANONYMIZATION_LLM_API_KEY", raising=False)
    cfg(base_url=BASE_URL, model="qwen3.5-9b")
    endpoint = ner.self_hosted_endpoint()
    assert endpoint is not None and endpoint.api_key == ""


# --- the output ceiling -----------------------------------------------------


def test_ceiling_defaults_when_unconfigured(cfg):
    assert ner.configured_max_output_tokens() == ner.DEFAULT_MAX_OUTPUT_TOKENS


def test_ceiling_is_configurable(cfg):
    cfg(max_output_tokens=16384)
    assert ner.configured_max_output_tokens() == 16384


def test_the_documented_zero_default_does_not_warn(cfg, caplog):
    """`instance.yaml.example` tells operators to write `0` for "use the
    default" — warning about the prescribed value on every construction is
    noise that teaches readers to skip the line that matters."""
    cfg(max_output_tokens=0)
    with caplog.at_level("WARNING"):
        assert ner.configured_max_output_tokens() == ner.DEFAULT_MAX_OUTPUT_TOKENS
    assert "max_output_tokens" not in caplog.text


def test_a_genuinely_out_of_range_ceiling_does_warn(cfg, caplog):
    cfg(max_output_tokens=10)
    with caplog.at_level("WARNING"):
        assert ner.configured_max_output_tokens() == ner.DEFAULT_MAX_OUTPUT_TOKENS
    assert "max_output_tokens" in caplog.text


@pytest.mark.parametrize("bad", [10, -1, 10**9, "sixteen thousand", None])
def test_out_of_range_ceiling_falls_back_rather_than_crashing_a_crawl(cfg, bad):
    cfg(max_output_tokens=bad)
    assert ner.configured_max_output_tokens() == ner.DEFAULT_MAX_OUTPUT_TOKENS


def test_explicit_ceiling_beats_config(cfg):
    cfg(max_output_tokens=16384)
    assert ner.LLMDetector(model="m", client=object(), max_output_tokens=999).max_output_tokens == 999


# --- the corroboration gate (the fail-open class) ---------------------------


class _Entity:
    def __init__(self, text, kind="person"):
        self.text, self.kind = text, kind


class _FakeLLM:
    """Minimal stand-in for `LLMDetector`'s per-chunk contract.

    One chunk unless `by_chunk` is given, in which case each (chunk, entities)
    pair is returned as-is — which is how a partial refusal is expressed.
    """

    def __init__(self, out=(), by_chunk=None):
        self._out, self._by_chunk = list(out), by_chunk

    def detect_by_chunk(self, markdown):
        return list(self._by_chunk) if self._by_chunk is not None else [(markdown, list(self._out))]


def _hybrid(monkeypatch, regex_out, llm_out, high_conf=None):
    """Hybrid detector over a STUB regex tier.

    Only for cases about the gate's own logic. Anything asserting what the
    gate does to a real document must use :func:`_real_hybrid` — a stub that
    returns `[]` where the real tier returns six heading hits is exactly how
    the first version of this gate shipped a corpus-eating bug unnoticed.
    """

    class _Stub:
        def __call__(self, _t):
            return list(regex_out)

        def high_confidence(self, _t):
            return list(regex_out if high_conf is None else high_conf)

    monkeypatch.setattr(ner, "_resolve_regex_detector", lambda: _Stub())
    return ner.hybrid_detector(_FakeLLM(llm_out))


def _real_hybrid(llm_out=(), by_chunk=None):
    """Hybrid detector over the REAL ``RegexDetector``."""
    return ner.hybrid_detector(_FakeLLM(llm_out, by_chunk))


def test_empty_llm_result_is_refused_when_regex_is_high_confidence(monkeypatch):
    """The measured gpt-oss-20b refusal: `[]` on a 'Confidential' document."""
    detect = _hybrid(monkeypatch, [_Entity("Ing. Tomas Kovar")], [])
    with pytest.raises(DetectionUnavailable, match="high-confidence"):
        detect("# Performance Review - Confidential\nIng. Tomas Kovar ...")


def test_llm_finding_something_is_never_refused(monkeypatch):
    detect = _hybrid(monkeypatch, [_Entity("Jan Novak")], [_Entity("Novakovi")])
    assert {e.text for e in detect("Jan Novak, Novakovi")} == {"Jan Novak", "Novakovi"}


def test_llm_may_legitimately_add_what_regex_missed(monkeypatch):
    detect = _hybrid(monkeypatch, [], [_Entity("Novak")])
    assert [e.text for e in detect("Novak podepsal smlouvu.")] == ["Novak"]


# --- what the gate must NOT do, against the real detector -------------------
#
# `RegexDetector` pass 2 fires on any two adjacent capitalized tokens, so in
# Markdown every Title-Cased heading is a `person` hit. Gating on "regex found
# anything" dropped these; that is a fail-CLOSED bug that eats a corpus of
# release notes and policies, and no stub can show it.

NAME_FREE_DOCUMENTS = {
    "release notes": "# Release Notes\n\n## Bug Fixes\nFixed a crash in the Data Warehouse export.\n",
    "meeting agenda": "# Weekly Standup\n\n- Sprint Review\n- Open Questions\n- Next Steps\n",
    "plain log": "Server rebooted at 02:15. No further action.",
    "policy doc": (
        "# Information Security Policy\n\nAll laptops must use Full Disk Encryption. "
        "Contact the Service Desk.\n"
    ),
    "title-cased report": (
        "# Quarterly Report\n\n## Executive Summary\n\nRevenue grew in the Prague Office "
        "region. See the Data Warehouse dashboard for the Monthly Recurring Revenue breakdown.\n"
    ),
}


@pytest.mark.parametrize("label", sorted(NAME_FREE_DOCUMENTS))
def test_a_name_free_document_is_never_dropped(label):
    """An honest `[]` on a document with no names must be believed."""
    detect = _real_hybrid()
    detect(NAME_FREE_DOCUMENTS[label])  # must not raise


def test_the_real_regex_tier_does_produce_hits_on_those_documents():
    """Pins WHY the test above is not vacuous: the naive corroborator fires."""
    from src.anonymization import RegexDetector

    tier = RegexDetector()
    noisy = {k: len(tier(v)) for k, v in NAME_FREE_DOCUMENTS.items() if tier(v)}
    assert noisy, "if this is empty the gate is no longer being exercised"
    assert all(not tier.high_confidence(v) for v in NAME_FREE_DOCUMENTS.values())


def test_a_real_honorific_document_is_still_refused():
    """The gate has to keep working against the real tier, not just a stub."""
    detect = _real_hybrid()
    with pytest.raises(DetectionUnavailable, match="high-confidence"):
        detect("# Zapis\n\nJednani vedl Ing. Tomas Kovar a zapsal jej.\n")


def test_a_real_legal_form_company_is_still_refused():
    detect = _real_hybrid()
    with pytest.raises(DetectionUnavailable, match="high-confidence"):
        detect("# Smlouva\n\nDodavatelem je Nexbyte Technologies SE.\n")


def test_a_refusal_in_ONE_chunk_is_not_hidden_by_names_in_another():
    """A split document's union is non-empty as soon as any chunk answers, so
    corroboration has to run per chunk or a refusal rides in on a neighbour."""
    answered = "Ing. Tomas Kovar led the review."
    refused = "Ing. Jana Dvorakova signed the contract."
    detect = _real_hybrid(by_chunk=[(answered, [_Entity("Tomas Kovar")]), (refused, [])])
    with pytest.raises(DetectionUnavailable, match="chunk"):
        detect(answered + "\n\n" + refused)


def test_every_chunk_answering_is_accepted():
    a, b = "Ing. Tomas Kovar led it.", "Ing. Jana Dvorakova signed it."
    detect = _real_hybrid(by_chunk=[(a, [_Entity("Tomas Kovar")]), (b, [_Entity("Jana Dvorakova")])])
    assert {e.text for e in detect(a + "\n\n" + b)} >= {"Tomas Kovar", "Jana Dvorakova"}


def test_a_name_free_chunk_beside_an_answered_one_is_fine():
    a, b = "Ing. Tomas Kovar led it.", "# Release Notes\n\n## Bug Fixes\nFixed the export."
    detect = _real_hybrid(by_chunk=[(a, [_Entity("Tomas Kovar")]), (b, [])])
    detect(a + "\n\n" + b)  # must not raise


@pytest.mark.parametrize("url", ["https://llm.internal:notaport", "https://llm.internal:99999"])
def test_a_malformed_port_fails_closed_instead_of_raising(cfg, monkeypatch, url):
    """`urlparse().port` raises on a bad port. Propagating that gave the admin
    preview a 500 instead of its 502, and aborted a crawl before its
    per-document fail-closed handling could run."""
    _allow_host(monkeypatch, hosts="llm.internal")
    cfg(base_url=url, model="qwen3.5-9b")
    with pytest.raises(DetectionUnavailable, match="HOST_ALLOWLIST"):
        ner.self_hosted_endpoint()


def test_detect_by_chunk_counts_entities_for_the_hybrid_path():
    """`hybrid_detector` consumes `detect_by_chunk`, so the counters have to
    live there — otherwise every redacted document reports zero entities."""
    detector = ner.LLMDetector(model="m", client=object())
    detector._detect_chunk = lambda chunk: [_Entity("Jan Novak")]
    detector.detect_by_chunk("Jan Novak signed it.")
    assert detector.last_usage["entities"] == 1
    assert detector.total_usage["entities"] == 1


def test_entities_are_deduplicated_across_chunks_not_double_counted():
    detector = ner.LLMDetector(model="m", client=object())
    detector._detect_by_chunk = lambda chunks: [("a", [_Entity("Jan Novak")]), ("b", [_Entity("Jan Novak")])]
    detector.detect_by_chunk("long enough to split, notionally")
    assert detector.last_usage["entities"] == 1


def test_cumulative_usage_is_updated_under_the_lock():
    """`total_usage` is mutated while `_usage_lock` is held.

    Asserted structurally, not by racing: on CPython the GIL makes a lost
    update on `dict[k] += int` very hard to provoke — measured here at 400,000
    unlocked increments across 8 threads with a 1us switch interval, zero
    updates lost. So a "run it concurrently and compare the total" test passes
    whether or not the lock exists, which makes it worthless as a guard. The
    lock is still correct: `+=` on a dict item is LOAD/ADD/STORE with no
    atomicity guarantee from the language, and a free-threaded build removes
    the accident that currently hides it. This test pins the thing that is
    actually decidable — that the update happens inside the critical section.
    """

    class _WatchedLock:
        def __init__(self):
            self.held = False
            self.updates_outside = 0

        def __enter__(self):
            self.held = True
            return self

        def __exit__(self, *_exc):
            self.held = False
            return False

    class _Watched(dict):
        def __init__(self, lock, *a):
            super().__init__(*a)
            self._lock = lock

        def __setitem__(self, key, value):
            if not self._lock.held:
                self._lock.updates_outside += 1
            super().__setitem__(key, value)

    detector = ner.LLMDetector(model="m", client=object())
    lock = _WatchedLock()
    detector._usage_lock = lock
    detector.total_usage = _Watched(lock, detector.total_usage)
    detector._detect_chunk = lambda chunk: [_Entity("Jan Novak")]

    detector.detect_by_chunk("Jan Novak signed it.")

    assert detector.total_usage["entities"] == 1
    assert lock.updates_outside == 0, "a cumulative counter was written outside the lock"


def test_cumulative_usage_totals_are_exact_across_threads():
    """Smoke test, not a race detector — see the note above for why a passing
    result here proves nothing about the lock. It does catch a total that is
    wrong for a non-concurrency reason, such as double-counting."""
    import threading

    detector = ner.LLMDetector(model="m", client=object())
    detector._detect_chunk = lambda chunk: [_Entity(f"Name {chunk[-1]}")]
    start = threading.Barrier(8)

    def one(i):
        start.wait()
        for _ in range(25):
            detector.detect_by_chunk(f"a document {i}")

    threads = [threading.Thread(target=one, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert detector.total_usage["entities"] == 8 * 25, detector.total_usage
    assert detector.total_usage["chunks"] == 8 * 25, detector.total_usage
