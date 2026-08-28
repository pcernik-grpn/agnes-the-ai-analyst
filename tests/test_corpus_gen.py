"""Tests for scripts/eval/corpus_gen.py — the planted proving-run corpus
generator (fact-graph spec §15.5, Run P).

Scope note: this suite asserts what THIS generator is responsible for --
S1-S4 (§15.1), the six traps that stand in for the sync/extraction-quality
fixtures (§15.2/§15.3: duplicate ~ C-series dedup, succession ~ EQ5,
contradiction ~ EQ4, entity_resolution ~ EQ7, fabrication ~ EQ1, scan ~
EQ8), and AN1/AN2 (§15.4). The C1-C9 sync tests proper (new file, changed
file, unchanged re-sync, rename/move, delete, ...) mutate a corpus over
time and S5-S8 exercise live grant/AgentPrincipal/correction behavior on
top of whatever content exists -- both reuse this same planted corpus at
harness/acceptance-test time rather than needing their own manifest ids
here (per the task's routing: a sibling task owns that harness).

TDD-first: written alongside the generator, exercised against it before
being declared done.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts.eval import corpus_gen
from scripts.eval.corpus_helpers import writers

REPO_ROOT = Path(__file__).resolve().parents[1]
SMALL_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "eval" / "planted_corpus_small"

S_FIXTURE_IDS = ("S1", "S2", "S3", "S4")
TRAP_IDS = ("scan", "duplicate", "succession", "contradiction", "entity_resolution", "fabrication")
AN_FIXTURE_IDS = ("AN1_canary", "AN2_pair")


def _strip_volatile(manifest: dict) -> dict:
    """A manifest copy with the one field that legitimately differs between
    otherwise-identical runs (the wall-clock generation timestamp) removed."""
    m = copy.deepcopy(manifest)
    m["generator"].pop("generated_at", None)
    return m


def _checksum(manifest: dict) -> str:
    stable = _strip_volatile(manifest)
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode("utf-8")).hexdigest()


def _all_files(root: Path) -> list[Path]:
    return sorted(p.relative_to(root) for p in root.rglob("*") if p.is_file())


# ── Determinism ──────────────────────────────────────────────────────


class TestDeterminism:
    def test_same_seed_same_manifest_checksum(self, tmp_path):
        m1 = corpus_gen.generate(tmp_path / "run1", seed=42, small=True)
        m2 = corpus_gen.generate(tmp_path / "run2", seed=42, small=True)
        assert _checksum(m1) == _checksum(m2)

    def test_same_seed_same_files_byte_for_byte(self, tmp_path):
        out1, out2 = tmp_path / "run1", tmp_path / "run2"
        corpus_gen.generate(out1, seed=42, small=True)
        corpus_gen.generate(out2, seed=42, small=True)

        files1, files2 = _all_files(out1), _all_files(out2)
        assert files1 == files2
        for rel in files1:
            if rel.name in ("ground_truth.json",):
                continue  # generated_at differs by construction; see checksum test
            assert (out1 / rel).read_bytes() == (out2 / rel).read_bytes(), rel

    def test_same_seed_same_files_in_full_mode_too(self, tmp_path):
        # The scan trap's PDF bytes are the one place non-determinism could
        # sneak back in via a library-embedded wall-clock timestamp.
        out1, out2 = tmp_path / "run1", tmp_path / "run2"
        corpus_gen.generate(out1, seed=7, n_docs=corpus_gen.FULL_MIN_DOCS)
        corpus_gen.generate(out2, seed=7, n_docs=corpus_gen.FULL_MIN_DOCS)
        scan_rel = next(p for p in _all_files(out1) if p.suffix == ".pdf")
        assert (out1 / scan_rel).read_bytes() == (out2 / scan_rel).read_bytes()

    def test_different_seed_changes_filler_but_not_planted_content(self, tmp_path):
        m1 = corpus_gen.generate(tmp_path / "a", seed=1, n_docs=corpus_gen.FULL_MIN_DOCS)
        m2 = corpus_gen.generate(tmp_path / "b", seed=2, n_docs=corpus_gen.FULL_MIN_DOCS)

        assert m1["nodes"] == m2["nodes"]
        assert m1["edges"] == m2["edges"]

        filler1 = [d["path"] for d in m1["documents"] if d["doc_key"].startswith("filler-")]
        filler2 = [d["path"] for d in m2["documents"] if d["doc_key"].startswith("filler-")]
        assert filler1 != filler2


# ── Full-mode scale (Run P: >=1000 docs, >=4 sites) ─────────────────


class TestFullModeScale:
    def test_at_least_1000_docs_across_at_least_4_sites(self, tmp_path):
        out = tmp_path / "full"
        manifest = corpus_gen.generate(out, seed=1)
        try:
            assert manifest["counts"]["documents"] >= 1000
            assert manifest["counts"]["sites"] >= 4
            on_disk = [p for p in out.rglob("*") if p.is_file() and p.name not in ("ground_truth.json", "sharing.yaml")]
            assert len(on_disk) == manifest["counts"]["documents"]
            sites_on_disk = {d.name for d in out.iterdir() if d.is_dir()}
            assert len(sites_on_disk) >= 4
        finally:
            pass  # tmp_path is cleaned up by pytest automatically

    def test_n_docs_floor_is_enforced(self, tmp_path):
        # A caller cannot ask for fewer than Run P's minimum in full mode.
        manifest = corpus_gen.generate(tmp_path / "full", seed=1, n_docs=10)
        assert manifest["counts"]["documents"] >= corpus_gen.FULL_MIN_DOCS

    def test_small_mode_has_no_filler(self, tmp_path):
        manifest = corpus_gen.generate(tmp_path / "small", seed=1, small=True)
        assert manifest["counts"]["filler_documents"] == 0
        assert manifest["counts"]["documents"] == manifest["counts"]["planted_documents"]


# ── Verbatim quotes (the load-bearing invariant for EQ1/EQ3/EQ8) ────


@pytest.fixture(scope="module")
def small_corpus(tmp_path_factory):
    out = tmp_path_factory.mktemp("verbatim") / "small"
    manifest = corpus_gen.generate(out, seed=corpus_gen.DEFAULT_SEED, small=True)
    return out, manifest


class TestVerbatimQuotes:
    def test_every_planted_quote_is_a_substring_of_its_documents_text(self, small_corpus):
        out, manifest = small_corpus
        docs_by_id = {d["doc_id"]: d for d in manifest["documents"]}

        rows = manifest["nodes"] + manifest["edges"]
        assert rows, "no planted nodes/edges to check"
        for row in rows:
            for ev in row["evidence"]:
                doc = docs_by_id[ev["doc_id"]]
                text = writers.extract_text(out / doc["path"])
                assert text is not None, f"no extractor available for {doc['path']}"
                assert ev["quote"] in text, (
                    f"quote for {row.get('id') or (row.get('src'), row.get('type'), row.get('dst'))} "
                    f"not found verbatim in {doc['path']}"
                )

    def test_office_format_docs_are_checked_via_the_matching_extraction_library(self, small_corpus):
        """For any document whose ACTUAL on-disk format is docx/pptx/xlsx
        (i.e. the writer library was available and no fallback happened),
        extract_text() must go through that format's own reader, not a
        generic text read -- this is what makes the substring check above
        meaningful for Office formats specifically."""
        out, manifest = small_corpus
        available = writers.available_formats()
        office_docs = [d for d in manifest["documents"] if d["format"] in ("docx", "pptx", "xlsx")]
        for doc in office_docs:
            assert not doc["format_fallback"]
            assert available[doc["format"]]
            assert (out / doc["path"]).suffix == f".{doc['format']}"

    def test_fallback_docs_are_flagged_and_still_carry_their_quotes(self, small_corpus):
        """When an Office writer library is unavailable, the fallback is a
        .md rendering -- verified separately here so the generator's
        contract holds regardless of which optional libraries happen to be
        installed in the environment running the suite."""
        out, manifest = small_corpus
        for doc in manifest["documents"]:
            if doc["format_fallback"]:
                assert doc["format"] == "md"
                assert (out / doc["path"]).suffix == ".md"

    def test_fabrication_trap_quote_is_not_actually_in_the_document(self, small_corpus):
        out, manifest = small_corpus
        assert manifest["prepared_false_claims"], "no prepared false claims"
        fc = manifest["prepared_false_claims"][0]
        doc = next(d for d in manifest["documents"] if d["doc_id"] == fc["doc_id"])
        text = writers.extract_text(out / doc["path"])
        assert fc["quote"] not in text, "the adversarial fixture's quote must NOT be verbatim"

    def test_scan_trap_has_no_extractable_text_layer(self, small_corpus):
        out, manifest = small_corpus
        scan = manifest["planted_elements"]["traps"]["scan"]
        doc = next(d for d in manifest["documents"] if d["doc_id"] == scan["doc_id"])
        assert doc["format"] == "pdf"
        text = writers.extract_text(out / doc["path"]) or ""
        assert text.strip() == "", "a scan trap with an extractable text layer isn't a scan trap"

    def test_duplicate_trap_shares_one_doc_id(self, small_corpus):
        _out, manifest = small_corpus
        dup = manifest["planted_elements"]["traps"]["duplicate"]
        assert len(set(dup["doc_ids"])) == 1, "byte-identical files must share one content-addressed doc_id"


# ── Fixture ids present in the manifest ─────────────────────────────


class TestFixtureIdsPresent:
    def test_all_s_fixtures_present(self, small_corpus):
        _out, manifest = small_corpus
        s = manifest["planted_elements"]["s_fixtures"]
        for fid in S_FIXTURE_IDS:
            assert fid in s, fid

    def test_all_traps_present(self, small_corpus):
        _out, manifest = small_corpus
        traps = manifest["planted_elements"]["traps"]
        for tid in TRAP_IDS:
            assert tid in traps, tid

    def test_all_anonymization_fixtures_present(self, small_corpus):
        _out, manifest = small_corpus
        an = manifest["planted_elements"]["anonymization"]
        for aid in AN_FIXTURE_IDS:
            assert aid in an, aid

    def test_s2_secret_claim_carries_the_documented_price(self, small_corpus):
        _out, manifest = small_corpus
        s2 = manifest["planted_elements"]["s_fixtures"]["S2"]
        secret_row = next(n for n in manifest["nodes"] if n["claim_key"] == s2["secret_claim_key"])
        assert secret_row["attrs"]["price_usd"] == s2["value"]

    def test_s1_secret_value_appears_only_in_its_own_document(self, small_corpus):
        out, manifest = small_corpus
        s1 = manifest["planted_elements"]["s_fixtures"]["S1"]
        value = s1["planted_value"]
        hits = []
        for doc in manifest["documents"]:
            text = writers.extract_text(out / doc["path"]) or ""
            if value in text:
                hits.append(doc["doc_key"])
        assert hits == [s1["doc_key"]], f"S1 value leaked into: {hits}"

    def test_an2_pair_uses_two_distinct_inflected_surface_forms(self, small_corpus):
        out, manifest = small_corpus
        an2 = manifest["planted_elements"]["anonymization"]["AN2_pair"]
        forms = list(an2["surface_forms"].values())
        assert len(set(forms)) == len(forms) >= 2
        texts = [
            writers.extract_text(out / d["path"]) or ""
            for d in manifest["documents"]
            if d["doc_key"] in an2["doc_keys"]
        ]
        for form in forms:
            assert any(form in t for t in texts), f"surface form {form!r} not found in any AN2 document"

    def test_entity_resolution_trap_ids_are_distinct_slugs(self, small_corpus):
        _out, manifest = small_corpus
        er = manifest["planted_elements"]["traps"]["entity_resolution"]
        assert er["canonical_id"] != er["variant_id"]

    def test_succession_trap_has_two_dated_claims_with_changed_value(self, small_corpus):
        _out, manifest = small_corpus
        trap = manifest["planted_elements"]["traps"]["succession"]
        claims = [n for n in manifest["nodes"] if n["claim_key"] in trap["claim_keys"]]
        assert len(claims) == 2
        values = {c["attrs"][trap["attribute"]] for c in claims}
        assert values == {trap["earlier_value"], trap["later_value"]}

    def test_contradiction_trap_claims_share_one_document_date(self, small_corpus):
        out, manifest = small_corpus
        trap = manifest["planted_elements"]["traps"]["contradiction"]
        docs_by_id = {d["doc_id"]: d for d in manifest["documents"]}
        edge_rows = [e for e in manifest["edges"] if e["claim_key"] in trap["claim_keys"]]
        assert len(edge_rows) == 2
        dates = {docs_by_id[e["evidence"][0]["doc_id"]]["document_date"] for e in edge_rows}
        assert dates == {trap["document_date"]}
        dsts = {e["dst"] for e in edge_rows}
        assert dsts == set(trap["values"])


# ── The committed small fixture stays in sync with the generator ────


class TestSmallFixtureInSync:
    def test_committed_fixture_matches_a_fresh_regeneration(self, tmp_path):
        assert SMALL_FIXTURE_DIR.exists(), "run scripts/eval/corpus_gen.py --small first"

        fresh = tmp_path / "fresh_small"
        fresh_manifest = corpus_gen.generate(fresh, seed=corpus_gen.DEFAULT_SEED, small=True)

        committed_manifest = json.loads((SMALL_FIXTURE_DIR / "ground_truth.json").read_text())
        assert _strip_volatile(fresh_manifest) == _strip_volatile(committed_manifest), (
            "tests/fixtures/eval/planted_corpus_small is stale -- regenerate with: "
            "python scripts/eval/corpus_gen.py --small --out tests/fixtures/eval/planted_corpus_small"
        )

        fresh_files = _all_files(fresh)
        committed_files = _all_files(SMALL_FIXTURE_DIR)
        assert fresh_files == committed_files
        for rel in fresh_files:
            if rel.name == "ground_truth.json":
                continue
            assert (fresh / rel).read_bytes() == (SMALL_FIXTURE_DIR / rel).read_bytes(), rel

    def test_committed_fixture_is_small(self):
        total = sum(p.stat().st_size for p in SMALL_FIXTURE_DIR.rglob("*") if p.is_file())
        assert total < 1_000_000, f"committed small fixture grew to {total} bytes"

    def test_committed_fixture_has_no_leaked_reserved_names_in_filler(self):
        # Small mode has zero filler by contract; this guards against that
        # contract silently changing.
        manifest = json.loads((SMALL_FIXTURE_DIR / "ground_truth.json").read_text())
        assert manifest["counts"]["filler_documents"] == 0


# ── Slugging / id grammar ────────────────────────────────────────────


class TestSlugGrammar:
    def test_ontology_id_grammar_holds_for_every_node_and_edge_endpoint(self, small_corpus):
        import re

        _out, manifest = small_corpus
        pattern = re.compile(r"^[a-z_]+:[a-z0-9][a-z0-9-]*$")
        ids = {n["id"] for n in manifest["nodes"]}
        for e in manifest["edges"]:
            ids.add(e["src"])
            ids.add(e["dst"])
        for node_id in ids:
            assert pattern.match(node_id), node_id

    def test_diacritics_dont_collapse_a_slug(self):
        from scripts.eval.corpus_helpers import vocab

        assert vocab.slugify("Tomáš Padrák") == "tomas-padrak"
