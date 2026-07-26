"""
A-05a Safe Regression Fixtures.

Minimal, synthetic fixture corpus for confirmed continuity-defect shapes. This is a
REWORK: the prior submission covered only 4 classes; this inventory was rebuilt from
the Execution Plan CLEAN (Sections 2-4, 9-10, 12-13, the A-04/A-05a/A-05b master-table
row), the Permanent Continuity Solution (Section 3's confirmed reference-manuscript
evidence, Section 5, Section 22.3-22.4, Sections 30/32), PIPELINE-SPEC.md (Sections 1-2, 8), and
direct source comments -- 13 confirmed defect classes total:

  - audition_date_contradiction        (Permanent Continuity Solution Section 3.1)
  - duplicate_inciting_incident        (Section 22.3 item 3; Execution Plan Section 4 #1/#10)
  - entity_attribute_gender_flip       (narration_api.py NARASI_ENTITY_ATTR_CHECK, gender kind)
  - entity_attribute_relation_flip     (narration_api.py NARASI_ENTITY_ATTR_CHECK, relation kind)
  - entity_attribute_title_flip        (narration_api.py:2280 kind enum — gender|title|age|relation)
  - entity_attribute_age_fork          (narasi_arithmetic.py:774-806)
  - duplicate_full_name_collision      (narration_api.py _name_uniqueness_scan)
  - chapter_heading_fusion             (Permanent Continuity Solution Section 3.3; commit 5637e72)
  - planning_text_leak                 (orchestrator/static.py _scrub_chapter_leaks)
  - english_chapter_heading_mismatch   (Execution Plan CLEAN Section 3 line 57; main.jsx:4119)
  - language_leak_indonesian           (Permanent Continuity Solution Section 3.2)
  - trailing_hash_leak                 (orchestrator/static.py:417-428)
  - elapsed_span_fork                  (narasi_arithmetic.py:819-867)

Every fixture is 100% synthetic -- invented character names ("Larasati", "Bimo",
"Made", "Sri Wulandari") and invented plot beats. No production manuscript text, job
IDs, tenant IDs, or user data appear anywhere in this file or in
tests/narasi_gates/fixtures/.

No network, provider, or model call is ever made by this test file: the LLM-based
mechanisms (entity-attribute check, canon-fork classifier) are exercised either via
source-schema introspection only, or with the network-calling helper explicitly
monkeypatched to a canned, offline response. Where a prior round's fixture only proved
response-WIRING (mocked classifier output -> position mapping) rather than that the
input genuinely contains the claimed defect, this rework adds a SEPARATE, deterministic,
test-local invariant check that inspects the fixture's own text/structure directly and
is fully independent of any mocked model response (see TestAuditionDateContradiction's
day-count extractor and TestDuplicateIncitingIncidentScope's event-signature check).
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES_DIR = HERE / "fixtures"
MANIFEST_PATH = HERE / "MANIFEST.json"

REQUIRED_DEFECT_CLASSES = {
    "audition_date_contradiction",
    "duplicate_inciting_incident",
    "entity_attribute_gender_flip",
    "entity_attribute_relation_flip",
    "entity_attribute_title_flip",
    "entity_attribute_age_fork",
    "elapsed_span_fork",
    "duplicate_full_name_collision",
    "chapter_heading_fusion",
    "planning_text_leak",
    "trailing_hash_leak",
    "english_chapter_heading_mismatch",
    "language_leak_indonesian",
}

# ---------------------------------------------------------------------------
# Independent canonical source-of-truth, deliberately OUTSIDE MANIFEST.json and
# OUTSIDE the fixture files themselves. This is a hardcoded Python literal, part
# of this test file's own source -- it is not derived from, and never reads,
# either the manifest or the fixtures. This is the anchor that closes the
# self-referential-binding gap: a manifest and a fixture can be tampered
# CONSISTENTLY with each other (same wrong source_reference in both places,
# hash recomputed to match) and no cross-check between those two documents
# alone can ever detect that, because they were made to agree by construction.
# Comparing against this THIRD, independent map is what makes the tamper
# detectable -- an attacker would additionally have to edit this test file's
# own source to survive re-review, a categorically different (and far more
# visible, in-diff) act than editing a data file.
# ---------------------------------------------------------------------------
_CANONICAL_SOURCE_BINDINGS: dict[str, dict[str, str]] = {
    "AUDDATE-001": {
        "source_reference": "Permanent Continuity Solution Section 3.1 (Timeline contradiction); Section 22.3 golden-fixture item 1",
        "mechanism_id": "_narasi_classify_canon_items",
    },
    "DUPINC-001": {
        "source_reference": "Permanent Continuity Solution Section 22.3 (golden-fixture item 3); Execution Plan CLEAN Section 3 line 49 and Section 4 root cause #1 and #10; Section 9 coverage matrix row 'Duplicate new occurrence'",
        "mechanism_id": "SharedContext.scope_for",
    },
    "ENTATTR-001": {
        "source_reference": "narration_api.py NARASI_ENTITY_ATTR_CHECK code comment, 2026-07-19 confirmed-miss note (gender kind)",
        "mechanism_id": "NARASI_ENTITY_ATTR_CHECK",
    },
    "ENTREL-001": {
        "source_reference": "narration_api.py NARASI_ENTITY_ATTR_CHECK code comment, 2026-07-15 confirmed-miss note (relation kind)",
        "mechanism_id": "NARASI_ENTITY_ATTR_CHECK",
    },
    "ENTTITLE-001": {
        "source_reference": "narration_api.py:2280 (kind enum: gender|title|age|relation); narration_api.py:2270-2278 (title/rank drift description)",
        "mechanism_id": "NARASI_ENTITY_ATTR_CHECK",
    },
    "AGE-001": {
        "source_reference": "narasi_arithmetic.py:774-806 (scan_same_entity_age_fork, NARASI_AGE_LEDGER); narration_api.py:2270 (entity-attribute check: 'age' drift kind)",
        "mechanism_id": "scan_same_entity_age_fork",
    },
    "ELAPSE-001": {
        "source_reference": "narasi_arithmetic.py:819-867 (scan_elapsed_span_consistency, NARASI_AGE_LEDGER)",
        "mechanism_id": "scan_elapsed_span_consistency",
    },
    "NAMEDUP-001": {
        "source_reference": "narration_api.py _name_uniqueness_scan code comment, 2026-07-15 confirmed-miss note",
        "mechanism_id": "_name_uniqueness_scan",
    },
    "HEADFUSE-001": {
        "source_reference": "Permanent Continuity Solution Section 3.3 (Structural heading corruption); narasi_gate.py chapter_heading_repair, confirmed fix commit 5637e72",
        "mechanism_id": "chapter_heading_repair",
    },
    "PLANLEAK-001": {
        "source_reference": "orchestrator/static.py _scrub_chapter_leaks function and SharedContext.outline()/scope_for() 'Leak fix (2026-07)' comments",
        "mechanism_id": "_scrub_chapter_leaks",
    },
    "TRAILHASH-001": {
        "source_reference": "orchestrator/static.py:417-428 (_scrub_chapter_leaks trailing-hash case, confirmed 2026-07-15)",
        "mechanism_id": "_scrub_chapter_leaks",
    },
    "CHNMISMATCH-001": {
        "source_reference": "Execution Plan CLEAN Section 3 line 57; main.jsx:4119 (getBabKey regex /Bab\\s+(\\d+)/i)",
        "mechanism_id": "frontend_getBabKey_regex",
    },
    "LANGLEAK-001": {
        "source_reference": "Permanent Continuity Solution Section 3.2 (Target-language leakage); Execution Plan CLEAN Section 9 coverage matrix row 'Target-language leakage'",
        "mechanism_id": "language_consistency_scan",
    },
}


def _validate_source_binding(fixture_id: str, source_reference: str, mechanism_id: str) -> bool:
    """Pure function, zero I/O: compares a claimed (source_reference, mechanism_id)
    pair against the independent canonical map above. Returns False for an unknown
    fixture_id or any mismatch -- never raises, never consults the manifest or a
    fixture file itself (that would defeat the entire point of independence)."""
    canonical = _CANONICAL_SOURCE_BINDINGS.get(fixture_id)
    if canonical is None:
        return False
    return (canonical["source_reference"] == source_reference
            and canonical["mechanism_id"] == mechanism_id)


# uji ketahanan / privacy robustness: patterns that must never appear in a fixture.
_FORBIDDEN_PATTERNS = [
    re.compile(r"\bjob_[0-9a-fA-F-]{8,}\b"),
    re.compile(r"\btenant_[0-9a-fA-F-]{8,}\b"),
    re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
    re.compile(r"Love on the Wrong Pitch", re.IGNORECASE),  # real reference manuscript name
]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _load_fixture(rel_path: str) -> dict:
    return json.loads((HERE / rel_path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Manifest completeness
# ---------------------------------------------------------------------------
class TestManifestCompleteness:
    def test_manifest_file_exists_and_parses(self):
        assert MANIFEST_PATH.is_file()
        manifest = _load_manifest()
        assert isinstance(manifest.get("fixtures"), list)
        assert len(manifest["fixtures"]) > 0

    def test_every_fixture_file_on_disk_is_listed_and_vice_versa(self):
        manifest = _load_manifest()
        listed = {entry["file"] for entry in manifest["fixtures"]}
        on_disk = {f"fixtures/{p.name}" for p in FIXTURES_DIR.glob("*.json")}
        assert listed == on_disk

    def test_every_manifest_entry_has_required_fields(self):
        manifest = _load_manifest()
        required_keys = ("id", "file", "defect_class", "classification",
                          "source_reference", "mechanism_id", "expected_mechanism", "sha256")
        for entry in manifest["fixtures"]:
            for key in required_keys:
                assert key in entry and entry[key], f"{entry.get('id')} missing/empty {key!r}"

    def test_every_fixture_sha256_matches_disk(self):
        manifest = _load_manifest()
        for entry in manifest["fixtures"]:
            path = HERE / entry["file"]
            assert path.is_file(), f"manifest lists missing file {entry['file']}"
            assert _sha256(path) == entry["sha256"], f"hash drift for {entry['file']}"

    def test_all_required_defect_classes_present(self):
        """Not restricted to a fixed subset -- every confirmed defect shape found in the
        authoritative inventory (ledger + solution doc + source comments) must appear."""
        manifest = _load_manifest()
        classes = {entry["defect_class"] for entry in manifest["fixtures"]}
        assert REQUIRED_DEFECT_CLASSES <= classes

    def test_no_defect_class_is_duplicated_across_fixtures(self):
        manifest = _load_manifest()
        classes = [entry["defect_class"] for entry in manifest["fixtures"]]
        assert len(classes) == len(set(classes))

    def test_every_fixture_classified_synthetic_or_deidentified(self):
        manifest = _load_manifest()
        for entry in manifest["fixtures"]:
            assert entry["classification"] in ("synthetic", "deidentified"), entry["id"]

    def test_stable_ids_are_unique(self):
        manifest = _load_manifest()
        ids = [entry["id"] for entry in manifest["fixtures"]]
        assert len(ids) == len(set(ids))

    def test_manifest_id_matches_fixture_files_own_id(self):
        """Cross-check id/defect_class/classification/source_reference/mechanism_id
        between the manifest entry and the fixture file's own declared fields (not just
        the SHA-256 of the bytes -- the manifest's DESCRIPTIVE fields must agree with
        the fixture's own, not just point at a file that happens to hash-match).
        EQUALITY check, not merely nonempty — a BOGUS source_reference that differs
        from the fixture's own must fail this assertion."""
        manifest = _load_manifest()
        for entry in manifest["fixtures"]:
            fixture = _load_fixture(entry["file"])
            assert fixture["id"] == entry["id"]
            assert fixture["defect_class"] == entry["defect_class"]
            assert fixture["classification"] == entry["classification"]
            assert fixture.get("source_reference") == entry.get("source_reference"), \
                f"{entry['id']} source_reference mismatch: fixture={fixture.get('source_reference')!r} vs manifest={entry.get('source_reference')!r}"
            assert fixture.get("mechanism_id") == entry.get("mechanism_id"), \
                f"{entry['id']} mechanism_id mismatch: fixture={fixture.get('mechanism_id')!r} vs manifest={entry.get('mechanism_id')!r}"

    def test_manifest_has_no_extra_untracked_fixture_id(self):
        manifest = _load_manifest()
        ids_from_manifest = {entry["id"] for entry in manifest["fixtures"]}
        ids_from_disk = set()
        for path in FIXTURES_DIR.glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            ids_from_disk.add(data["id"])
        assert ids_from_manifest == ids_from_disk

    def test_single_byte_fixture_tamper_breaks_the_recorded_hash(self):
        """Sanity check only (NOT the full tamper defense -- see
        TestCanonicalSourceBindingIndependentOfManifest for the combined-tamper
        negative control): an UNCOORDINATED single-byte mutation of a fixture,
        with the manifest's recorded hash left untouched, is caught by the plain
        hash-match check. This does not defend against a tamper that updates the
        manifest's hash to match -- that class of attack is defeated separately,
        by comparison against the independent canonical map, not by this hash
        check (which only ever compares two documents that could be tampered
        together)."""
        manifest = _load_manifest()
        entry = manifest["fixtures"][0]
        path = HERE / entry["file"]
        original_bytes = path.read_bytes()
        original_hash = hashlib.sha256(original_bytes).hexdigest()
        assert original_hash == entry["sha256"], \
            f"canonical hash binding broken for {entry['id']}: {original_hash} != {entry['sha256']}"
        mutated = original_bytes.replace(
            b'"source_reference": "', b'"source_reference": "X', 1)
        assert mutated != original_bytes, "mutation had no effect"
        mutated_hash = hashlib.sha256(mutated).hexdigest()
        assert mutated_hash != entry["sha256"], \
            f"single-byte tamper not detected: {mutated_hash} still equals {entry['sha256']}"


# ---------------------------------------------------------------------------
# Canonical source-binding: independent of the manifest/fixture pair itself
# ---------------------------------------------------------------------------
class TestCanonicalSourceBindingIndependentOfManifest:
    """Closes the self-referential-binding gap: a manifest and its fixture file can
    be tampered CONSISTENTLY (same wrong source_reference in both, hash recomputed
    to match) and no cross-check comparing only those two documents can ever detect
    it, because they were made to agree with each other by construction. Every test
    here validates against _CANONICAL_SOURCE_BINDINGS -- a hardcoded literal in THIS
    file, never read from manifest.json or any fixtures/*.json file."""

    def test_every_manifest_entry_matches_the_independent_canonical_map(self):
        manifest = _load_manifest()
        for entry in manifest["fixtures"]:
            assert _validate_source_binding(
                entry["id"], entry.get("source_reference", ""), entry.get("mechanism_id", "")
            ), f"{entry['id']} manifest entry does not match the independent canonical binding"

    def test_every_fixture_file_matches_the_independent_canonical_map(self):
        manifest = _load_manifest()
        for entry in manifest["fixtures"]:
            fixture = _load_fixture(entry["file"])
            assert _validate_source_binding(
                fixture["id"], fixture.get("source_reference", ""), fixture.get("mechanism_id", "")
            ), f"{fixture['id']} fixture file does not match the independent canonical binding"

    def test_canonical_map_has_no_gap_against_the_manifest(self):
        """The independent map itself must not silently omit a fixture -- an
        unlisted id would make the two tests above vacuously pass nothing for it."""
        manifest = _load_manifest()
        ids_in_manifest = {entry["id"] for entry in manifest["fixtures"]}
        assert ids_in_manifest <= set(_CANONICAL_SOURCE_BINDINGS)
        assert set(_CANONICAL_SOURCE_BINDINGS) <= ids_in_manifest

    def test_consistent_tamper_of_fixture_and_manifest_together_is_still_rejected(self):
        """THE negative control this class exists for: simulate an attacker who
        controls BOTH the fixture file and the manifest entry, changes
        source_reference to the SAME wrong value in both, and recomputes the
        fixture's SHA-256 so the plain hash-match check (test_every_fixture_
        sha256_matches_disk) would stay green throughout. No disk I/O is used to
        simulate this -- the tamper is modeled entirely in memory, exactly
        mirroring what test_every_manifest_entry_matches_the_independent_canonical_
        map and test_every_fixture_file_matches_the_independent_canonical_map do
        against the real files, so this proves the SAME validator call that passes
        on real data would fail on tampered data claiming to be real.
        """
        manifest = _load_manifest()
        entry = dict(manifest["fixtures"][0])  # real entry, copied
        real_fixture = _load_fixture(entry["file"])
        tampered_fixture = dict(real_fixture)

        # Attacker changes source_reference to a plausible-looking but WRONG value,
        # consistently, in both documents (this is exactly Codex's P1 scenario).
        bogus_reference = "Permanent Continuity Solution Section 99.9 (fabricated)"
        tampered_fixture["source_reference"] = bogus_reference
        entry["source_reference"] = bogus_reference

        # Recompute what the fixture's bytes/hash WOULD be after this edit, proving
        # the plain hash-match check alone cannot catch a fully consistent tamper.
        tampered_bytes = json.dumps(tampered_fixture, indent=2).encode("utf-8") + b"\n"
        entry["sha256"] = hashlib.sha256(tampered_bytes).hexdigest()

        # The consistent tamper agrees with itself perfectly -- id/defect_class/
        # classification/sha256 all still line up between the two tampered documents.
        assert tampered_fixture["id"] == entry["id"]
        assert tampered_fixture["source_reference"] == entry["source_reference"]
        assert hashlib.sha256(tampered_bytes).hexdigest() == entry["sha256"]

        # The independent canonical map was never touched -- it still says what it
        # always said. The SAME validator that passed on real data now fails.
        assert _validate_source_binding(
            entry["id"], entry["source_reference"], entry.get("mechanism_id", "")
        ) is False, "combined fixture+manifest+hash tamper was NOT caught -- binding is still self-referential"

        # Sanity: confirm the untampered real entry still validates fine, so this
        # test is exercising a genuine before/after contrast, not a broken map.
        assert _validate_source_binding(
            entry["id"], real_fixture["source_reference"], real_fixture.get("mechanism_id", "")
        ) is True

    def test_mechanism_id_alone_tampered_is_also_rejected(self):
        """A narrower variant of the same attack: only mechanism_id is changed
        (source_reference left correct). Proves the validator checks BOTH fields,
        not just source_reference."""
        manifest = _load_manifest()
        entry = manifest["fixtures"][0]
        assert _validate_source_binding(
            entry["id"], entry["source_reference"], "some_other_function_entirely"
        ) is False

    def test_unknown_fixture_id_is_rejected_not_vacuously_true(self):
        assert _validate_source_binding("NOT-A-REAL-ID-999", "anything", "anything") is False


# ---------------------------------------------------------------------------
# Privacy review (uji ketahanan)
# ---------------------------------------------------------------------------
class TestPrivacyScan:
    """No production manuscript text, job/tenant IDs, or other real-user data
    anywhere in the fixture corpus or manifest."""

    def test_no_forbidden_identifier_pattern_in_any_fixture(self):
        for path in FIXTURES_DIR.glob("*.json"):
            raw = path.read_text(encoding="utf-8")
            for rx in _FORBIDDEN_PATTERNS:
                assert not rx.search(raw), f"forbidden pattern {rx.pattern!r} found in {path.name}"

    def test_no_forbidden_identifier_pattern_in_manifest(self):
        raw = MANIFEST_PATH.read_text(encoding="utf-8")
        for rx in _FORBIDDEN_PATTERNS:
            assert not rx.search(raw)

    def test_every_fixture_declares_a_recognized_classification(self):
        for path in FIXTURES_DIR.glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            assert data.get("classification") in ("synthetic", "deidentified"), path.name


# ---------------------------------------------------------------------------
# 1) language_leak_indonesian -- real, deterministic gate
# ---------------------------------------------------------------------------
class TestLanguageLeakGate:
    def _fixture(self) -> dict:
        return _load_fixture("fixtures/language_leak_indonesian.json")

    def test_positive_fixture_triggers_the_real_scanner(self):
        import narasi_gate
        fx = self._fixture()
        result = narasi_gate.language_consistency_scan(fx["input_text"], lang=fx["target_language"])
        assert result["applies"] is True
        assert result["hits"] >= fx["expected_verdict"]["hits_at_least"]
        assert fx["expected_verdict"]["other_langs_contains"] in result["other_langs"]

    def test_negative_control_does_not_trigger(self):
        import narasi_gate
        fx = self._fixture()
        result = narasi_gate.language_consistency_scan(fx["negative_control_text"], lang=fx["target_language"])
        assert result["applies"] is True
        assert result["hits"] == fx["negative_control_expected"]["hits"]

    def test_edge_case_empty_text_never_raises(self):
        import narasi_gate
        result = narasi_gate.language_consistency_scan("", lang="en")
        assert result == {"applies": False, "hits": 0, "samples": [], "other_langs": {}}

    def test_edge_case_unseeded_language_with_clean_latin_text_reports_not_applicable(self):
        import narasi_gate
        result = narasi_gate.language_consistency_scan("Plain clean text with no leak at all.", lang="zz")
        assert result["applies"] is False


# ---------------------------------------------------------------------------
# 2) entity_attribute_gender_flip -- LLM-based gate, offline schema-shape only
# ---------------------------------------------------------------------------
class TestEntityAttributeGenderFlipShape:
    def _fixture(self) -> dict:
        return _load_fixture("fixtures/entity_attribute_gender_flip.json")

    def test_fixture_text_contains_the_contradicting_usages(self):
        fx = self._fixture()
        drift = fx["expected_drift"]
        ch_text = fx["input_chapters"][str(drift["chapter"])]
        assert drift["quote"] in ch_text
        assert "she" in fx["input_chapters"]["2"].lower()
        assert "a man" in ch_text.lower()

    def test_negative_control_has_no_contradiction(self):
        fx = self._fixture()
        neg = fx["negative_control_chapters"]
        assert "she" in neg["2"].lower()
        assert "a man" not in neg["5"].lower()

    def test_fixture_drift_schema_matches_live_extraction_contract(self):
        """Cross-checks the fixture's expected_drift keys against the REAL required
        JSON shape documented in narration_api.py's entity-attribute continuity
        checker prompt. Schema introspection only -- no model is called."""
        import narration_api
        src = inspect.getsource(narration_api)
        i = src.index("entity-attribute continuity checker")
        schema_region = src[i:i + 2500]
        fx = self._fixture()
        for key in fx["expected_drift"]:
            assert f'\\"{key}\\"' in schema_region, f"schema no longer documents key {key!r}"
        assert fx["expected_drift"]["kind"] == "gender"
        assert '"gender|title|' in schema_region  # kind enum still includes gender


# ---------------------------------------------------------------------------
# 3) entity_attribute_relation_flip -- LLM-based gate, offline schema-shape only
# ---------------------------------------------------------------------------
class TestEntityAttributeRelationFlipShape:
    def _fixture(self) -> dict:
        return _load_fixture("fixtures/entity_attribute_relation_flip.json")

    def test_fixture_text_contains_the_contradicting_relation_terms(self):
        fx = self._fixture()
        drift = fx["expected_drift"]
        ch_text = fx["input_chapters"][str(drift["chapter"])]
        assert drift["quote"] in ch_text
        assert "daughter" in fx["input_chapters"]["3"].lower()
        assert "son" in ch_text.lower()
        assert "daughter" not in ch_text.lower()  # genuinely flipped, not both present

    def test_negative_control_has_no_contradiction(self):
        fx = self._fixture()
        neg = fx["negative_control_chapters"]
        assert "daughter" in neg["3"].lower()
        assert "daughter" in neg["6"].lower()
        assert "son" not in neg["6"].lower()

    def test_fixture_drift_schema_matches_live_extraction_contract(self):
        import narration_api
        src = inspect.getsource(narration_api)
        i = src.index("entity-attribute continuity checker")
        schema_region = src[i:i + 2500]
        fx = self._fixture()
        for key in fx["expected_drift"]:
            assert f'\\"{key}\\"' in schema_region, f"schema no longer documents key {key!r}"
        assert fx["expected_drift"]["kind"] == "relation"
        assert "relation" in schema_region  # kind enum still includes relation
        assert "relation slot" in schema_region or "relation descriptor" in schema_region


# ---------------------------------------------------------------------------
# 3b) entity_attribute_title_flip -- LLM-based gate, offline schema-shape only
# ---------------------------------------------------------------------------
class TestEntityAttributeTitleFlipShape:
    def _fixture(self) -> dict:
        return _load_fixture("fixtures/entity_attribute_title_flip.json")

    def test_fixture_text_contains_the_contradicting_title_terms(self):
        fx = self._fixture()
        drift = fx["expected_drift"]
        ch_text = fx["input_chapters"][str(drift["chapter"])]
        assert drift["quote"] in ch_text
        assert "head of marketing" in fx["input_chapters"]["4"].lower()
        assert "assistant" in ch_text.lower()
        assert "head of marketing" not in ch_text.lower()

    def test_negative_control_has_no_contradiction(self):
        fx = self._fixture()
        neg = fx["negative_control_chapters"]
        assert "head of marketing" in neg["4"].lower()
        assert "head of marketing" in neg["8"].lower()
        assert "assistant" not in neg["8"].lower()

    def test_fixture_drift_schema_matches_live_extraction_contract(self):
        import narration_api
        src = inspect.getsource(narration_api)
        i = src.index("entity-attribute continuity checker")
        schema_region = src[i:i + 2500]
        fx = self._fixture()
        for key in fx["expected_drift"]:
            assert f'\\"{key}\\"' in schema_region, f"schema no longer documents key {key!r}"
        assert fx["expected_drift"]["kind"] == "title"
        assert '"gender|title|' in schema_region  # kind enum still includes title


# ---------------------------------------------------------------------------
# 13) duplicate_full_name_collision -- real, deterministic gate
# ---------------------------------------------------------------------------
class TestDuplicateFullNameCollisionGate:
    def _fixture(self) -> dict:
        return _load_fixture("fixtures/duplicate_full_name_collision.json")

    def test_positive_fixture_triggers_the_real_scanner(self):
        import narration_api
        fx = self._fixture()
        result = narration_api._name_uniqueness_scan(fx["input_text"])
        assert len(result) >= fx["expected_verdict"]["collision_count_at_least"]
        assert result[0]["name"] == fx["expected_verdict"]["collision_name"]

    def test_negative_control_does_not_trigger(self):
        import narration_api
        fx = self._fixture()
        result = narration_api._name_uniqueness_scan(fx["negative_control_text"])
        assert result == []

    def test_edge_case_empty_text_never_raises(self):
        import narration_api
        assert narration_api._name_uniqueness_scan("") == []

    def test_edge_case_single_introduction_never_fires(self):
        import narration_api
        result = narration_api._name_uniqueness_scan(
            "The story opens on a woman named Sri Wulandari, the head coordinator.")
        assert result == []


# ---------------------------------------------------------------------------
# 13) chapter_heading_fusion -- real, deterministic gate
# ---------------------------------------------------------------------------
class TestChapterHeadingFusionGate:
    def _fixture(self) -> dict:
        return _load_fixture("fixtures/chapter_heading_fusion.json")

    def test_positive_fixture_triggers_the_real_repair(self):
        import narasi_gate
        fx = self._fixture()
        out, n = narasi_gate.chapter_heading_repair(fx["input_text"], lang="en")
        assert n == fx["expected_verdict"]["n_repairs"]
        assert "\n\n## Chapter 4:" in out
        assert "air.## Chapter 4" not in out

    def test_negative_control_is_byte_identical(self):
        import narasi_gate
        fx = self._fixture()
        out, n = narasi_gate.chapter_heading_repair(fx["negative_control_text"], lang="en")
        assert n == fx["negative_control_expected"]["n_repairs"]
        assert out == fx["negative_control_text"]

    def test_edge_case_empty_text_never_raises(self):
        import narasi_gate
        out, n = narasi_gate.chapter_heading_repair("", lang="en")
        assert n == 0
        assert out == ""


# ---------------------------------------------------------------------------
# 13) planning_text_leak -- real, deterministic gate
# ---------------------------------------------------------------------------
class TestPlanningTextLeakGate:
    def _fixture(self) -> dict:
        return _load_fixture("fixtures/planning_text_leak.json")

    def test_positive_fixture_leaked_line_is_scrubbed(self):
        from orchestrator.static import _scrub_chapter_leaks
        fx = self._fixture()
        scrubbed = _scrub_chapter_leaks(fx["input_text"], task_id="test-ch1")
        assert fx["expected_verdict"]["leaked_line_text"] not in scrubbed
        assert "Larasati stood at the microphone" in scrubbed
        assert "The room held its breath" in scrubbed

    def test_negative_control_is_unchanged(self):
        from orchestrator.static import _scrub_chapter_leaks
        fx = self._fixture()
        scrubbed = _scrub_chapter_leaks(fx["negative_control_text"], task_id="test-ch1")
        assert scrubbed == fx["negative_control_text"]

    def test_edge_case_empty_text_never_raises(self):
        from orchestrator.static import _scrub_chapter_leaks
        assert _scrub_chapter_leaks("", task_id="test") == ""


# ---------------------------------------------------------------------------
# 13) english_chapter_heading_mismatch -- frontend regex property, Python-expressed
# ---------------------------------------------------------------------------
class TestEnglishChapterHeadingMismatch:
    _FRONTEND_BAB_RX = re.compile(r"Bab\s+(\d+)", re.IGNORECASE)

    def _fixture(self) -> dict:
        return _load_fixture("fixtures/english_chapter_heading_mismatch.json")

    def test_frontend_regex_fails_to_match_english_chapter_heading(self):
        fx = self._fixture()
        assert self._FRONTEND_BAB_RX.search(fx["positive_title"]) is None

    def test_frontend_regex_matches_indonesian_bab_heading(self):
        fx = self._fixture()
        m = self._FRONTEND_BAB_RX.search(fx["negative_control_title"])
        assert m is not None
        assert m.group(1) == fx["expected_verdict"]["negative_control_captured_number"]

    def test_regex_pattern_in_fixture_matches_the_confirmed_source_pattern(self):
        """Cross-check the fixture's declared frontend_regex_source string against the
        pattern actually used in this test (both must describe the SAME confirmed
        pattern, not just happen to produce the same behavior)."""
        fx = self._fixture()
        assert fx["frontend_regex_source"] == "/Bab\\s+(\\d+)/i"


# ---------------------------------------------------------------------------
# 13) duplicate_inciting_incident -- confirmed-absent detector; mitigation +
#    deterministic event-identity invariant (INPUT-based, not just scope_for()
#    isolation which holds regardless of whether the events actually collide)
# ---------------------------------------------------------------------------
def _event_signature(summary: str, character: str) -> str:
    """Test-local, deterministic: strips the POV character's own name out of a chapter
    summary so the REMAINING templated event description can be compared across
    chapters. Two chapters describing the SAME underlying event (just from a different
    character's POV) collapse to the same signature; two chapters describing DIFFERENT
    events do not. This is independent of scope_for()'s isolation behavior, which is
    invariant regardless of whether the underlying events actually collide."""
    return summary.replace(character, "<CHAR>")


class TestDuplicateIncitingIncidentScope:
    def _fixture(self) -> dict:
        return _load_fixture("fixtures/duplicate_inciting_incident.json")

    def _ctx(self, chapters):
        from orchestrator.context_builder import SharedContext
        return SharedContext(chapters=chapters)

    def test_positive_fixture_chapters_share_the_same_event_signature(self):
        """INPUT-based invariant: proves the two chapters in the positive fixture
        genuinely describe the SAME underlying event, independent of scope_for()."""
        fx = self._fixture()
        ch = fx["input_chapters"]
        sig0 = _event_signature(ch[0]["summary"], ch[0]["character"])
        sig1 = _event_signature(ch[1]["summary"], ch[1]["character"])
        assert sig0 == sig1
        assert fx["expected_event_identity"]["input_chapters_share_same_event_signature"] is True

    def test_negative_control_chapters_have_different_event_signatures(self):
        """INPUT-based invariant: proves the negative control's two chapters describe
        genuinely DIFFERENT events -- if this fixture were tampered to make them the
        same event, this assertion (not scope_for()'s isolation behavior) would fail."""
        fx = self._fixture()
        ch = fx["negative_control_chapters"]
        sig0 = _event_signature(ch[0]["summary"], ch[0]["character"])
        sig1 = _event_signature(ch[1]["summary"], ch[1]["character"])
        assert sig0 != sig1
        assert fx["expected_event_identity"]["negative_control_chapters_have_different_event_signatures"] is True

    def test_neither_scope_leaks_the_sibling_summary(self):
        fx = self._fixture()
        ctx = self._ctx(fx["input_chapters"])
        scope0 = ctx.scope_for(0)
        scope1 = ctx.scope_for(1)
        summary0 = fx["input_chapters"][0]["summary"]
        summary1 = fx["input_chapters"][1]["summary"]
        assert summary1 not in scope0
        assert summary0 not in scope1

    def test_both_scopes_name_the_sibling_title_as_not_owned(self):
        fx = self._fixture()
        ctx = self._ctx(fx["input_chapters"])
        scope0 = ctx.scope_for(0)
        scope1 = ctx.scope_for(1)
        title0 = fx["input_chapters"][0]["title"]
        title1 = fx["input_chapters"][1]["title"]
        assert title1 in scope0
        assert title0 in scope1

    def test_confirmed_gap_documented_not_silently_claimed_covered(self):
        fx = self._fixture()
        assert fx["expected_gate"] == "NO_DETECTOR_TODAY"

    def test_edge_case_single_chapter_scope_has_no_sibling_section(self):
        ctx = self._ctx([{"title": "Solo", "summary": "..."}])
        scope = ctx.scope_for(0)
        assert "You do NOT own" not in scope

    def test_edge_case_out_of_range_index_returns_empty_string(self):
        fx = self._fixture()
        ctx = self._ctx(fx["input_chapters"])
        assert ctx.scope_for(99) == ""
        assert ctx.scope_for(-1) == ""


# ---------------------------------------------------------------------------
# 13) audition_date_contradiction -- LLM-based classifier, offline-stubbed wiring
#    PLUS a deterministic, test-local day-count extractor that independently
#    proves the fixture's own text contains (or does not contain) a real
#    contradiction, decoupled from the mocked classifier response.
# ---------------------------------------------------------------------------
_ID_NUM_WORDS = {"satu": 1, "dua": 2, "tiga": 3, "empat": 4, "lima": 5, "enam": 6, "tujuh": 7}
_DAYS_LAGI_RX = re.compile(
    r"\b(\d+|satu|dua|tiga|empat|lima|enam|tujuh)\s+hari\s+lagi\b", re.IGNORECASE)
_BESOK_RX = re.compile(r"\bbesok\b", re.IGNORECASE)


def _extract_days_until(text: str):
    """Test-local, deterministic Indonesian time-to-event-distance extractor. Not a
    product function -- exists only so this test suite can independently verify a
    fixture's own text contains a genuine day-count contradiction (or does not),
    fully decoupled from any mocked classifier response."""
    m = _DAYS_LAGI_RX.search(text)
    if m:
        tok = m.group(1).lower()
        return int(tok) if tok.isdigit() else _ID_NUM_WORDS[tok]
    if _BESOK_RX.search(text):
        return 1
    return None


class TestAuditionDateContradictionClassifierWiring:
    """Every model-call test in this class monkeypatches the network-calling helper
    with a canned, offline response. No live model call, no network access, anywhere.
    The day-count tests below are fully independent of any mock."""

    def _fixture(self) -> dict:
        return _load_fixture("fixtures/audition_date_contradiction.json")

    def test_positive_fixture_excerpts_have_different_day_counts_independent_of_model(self):
        """INPUT-based invariant, decoupled from the mocked classifier: proves the two
        chapter excerpts genuinely describe different time-to-event distances."""
        fx = self._fixture()
        days_a = _extract_days_until(fx["chapter_a_excerpt"])
        days_b = _extract_days_until(fx["chapter_b_excerpt"])
        assert days_a is not None and days_b is not None
        assert days_a != days_b
        assert fx["expected_day_count_invariant"]["chapter_a_and_chapter_b_days_until_differ"] is True

    def test_negative_control_excerpts_have_the_same_day_count_independent_of_model(self):
        fx = self._fixture()
        days_a = _extract_days_until(fx["negative_control_chapter_a_excerpt"])
        days_b = _extract_days_until(fx["negative_control_chapter_b_excerpt"])
        assert days_a is not None and days_b is not None
        assert days_a == days_b
        assert fx["expected_day_count_invariant"]["negative_control_days_until_are_equal"] is True

    def test_item_shape_matches_classifier_input_contract(self):
        fx = self._fixture()
        for item in fx["input_items"]:
            assert isinstance(item, dict)
            assert item.get("signal")

    def test_stubbed_continuity_classification_returns_expected_position_set(self, monkeypatch):
        import laozhang_api
        import narration_api
        fx = self._fixture()

        async def fake_cheap_call(system, user, *, tenant_id, user_id, job_uuid=None, **kw):
            return json.dumps({"items": [{"i": 0, "class": fx["expected_classification"]}]}), 0

        monkeypatch.setattr(laozhang_api, "_narasi_cheap_call", fake_cheap_call)
        result = asyncio.run(narration_api._narasi_classify_canon_items(
            fx["input_items"], tenant_id="t", user_id="u", job_uuid="j"))
        assert result == {0}

    def test_reveal_classification_is_excluded_from_the_returned_set(self, monkeypatch):
        import laozhang_api
        import narration_api
        fx = self._fixture()

        async def fake_cheap_call(system, user, *, tenant_id, user_id, job_uuid=None, **kw):
            return json.dumps({"items": [{"i": 0, "class": "reveal"}]}), 0

        monkeypatch.setattr(laozhang_api, "_narasi_cheap_call", fake_cheap_call)
        result = asyncio.run(narration_api._narasi_classify_canon_items(
            fx["input_items"], tenant_id="t", user_id="u", job_uuid="j"))
        assert result == set()

    def test_edge_case_broken_model_response_fails_safe(self, monkeypatch):
        import laozhang_api
        import narration_api
        fx = self._fixture()

        async def fake_cheap_call(system, user, *, tenant_id, user_id, job_uuid=None, **kw):
            return "not json at all", 0

        monkeypatch.setattr(laozhang_api, "_narasi_cheap_call", fake_cheap_call)
        result = asyncio.run(narration_api._narasi_classify_canon_items(
            fx["input_items"], tenant_id="t", user_id="u", job_uuid="j"))
        assert result == set()  # fail-safe: never crashes, never over-classifies

    def test_edge_case_empty_items_short_circuits_without_any_call(self, monkeypatch):
        import laozhang_api
        import narration_api

        def boom(*_a, **_k):
            raise AssertionError("must not be called for empty items")

        monkeypatch.setattr(laozhang_api, "_narasi_cheap_call", boom)
        result = asyncio.run(narration_api._narasi_classify_canon_items(
            [], tenant_id="t", user_id="u", job_uuid="j"))
        assert result == set()


# ---------------------------------------------------------------------------
# 13) trailing_hash_leak — real, deterministic gate (same _scrub_chapter_leaks
#     function as PLANLEAK-001, exercises the trailing-hash branch separately)
# ---------------------------------------------------------------------------
class TestTrailingHashLeakGate:
    def _fixture(self) -> dict:
        return _load_fixture("fixtures/trailing_hash_leak.json")

    def test_positive_fixture_trailing_hash_is_scrubbed(self):
        from orchestrator.static import _scrub_chapter_leaks
        fx = self._fixture()
        scrubbed = _scrub_chapter_leaks(fx["input_text"], task_id="test-th")
        assert fx["expected_verdict"]["trailing_hash_removed"] is True
        assert scrubbed == fx["expected_verdict"]["substantive_text_preserved"]

    def test_negative_control_is_unchanged(self):
        from orchestrator.static import _scrub_chapter_leaks
        fx = self._fixture()
        scrubbed = _scrub_chapter_leaks(fx["negative_control_text"], task_id="test-th")
        assert scrubbed == fx["negative_control_text"]
        assert fx["negative_control_expected"]["text_unchanged"] is True

    def test_trailing_hash_only_removes_hash_not_body(self):
        """Prove the trailing-hash scrub preserves all substantive text and only removes
        the trailing '#' marker — it does not damage a mid-text '#' that appears
        elsewhere in the prose."""
        from orchestrator.static import _scrub_chapter_leaks
        text_with_inline_hash = "She paused. # The mark meant nothing to her. #"
        scrubbed = _scrub_chapter_leaks(text_with_inline_hash, task_id="test-th")
        assert scrubbed == "She paused. # The mark meant nothing to her."


# ---------------------------------------------------------------------------
# 13) elapsed_span_fork — real, deterministic arithmetic gate (narasi_arithmetic)
# ---------------------------------------------------------------------------
class TestElapsedSpanForkGate:
    def _fixture(self) -> dict:
        return _load_fixture("fixtures/elapsed_span_fork.json")

    def test_positive_fixture_triggers_the_real_scanner(self):
        import narasi_arithmetic
        fx = self._fixture()
        result = narasi_arithmetic.scan_age_ledger(fx["input_text"])
        assert result["status"] == fx["expected_verdict"]["status"]
        assert result["count"] >= fx["expected_verdict"]["count_at_least"]
        assert any(f["anchor"] == fx["expected_verdict"]["anchor"] for f in result["findings"])

    def test_negative_control_passes(self):
        import narasi_arithmetic
        fx = self._fixture()
        result = narasi_arithmetic.scan_age_ledger(fx["negative_control_text"])
        assert result["status"] == fx["negative_control_expected"]["status"]
        assert result["count"] == fx["negative_control_expected"]["count"]

    def test_edge_case_empty_text_never_raises(self):
        import narasi_arithmetic
        result = narasi_arithmetic.scan_age_ledger("")
        assert result["status"] == "PASS"
        assert result["count"] == 0


# ---------------------------------------------------------------------------
# 13) entity_attribute_age_fork — real, deterministic arithmetic gate
#     (narasi_arithmetic.scan_same_entity_age_fork)
# ---------------------------------------------------------------------------
class TestEntityAttributeAgeForkGate:
    def _fixture(self) -> dict:
        return _load_fixture("fixtures/entity_attribute_age_fork.json")

    def test_positive_fixture_triggers_the_real_scanner(self):
        import narasi_arithmetic
        fx = self._fixture()
        result = narasi_arithmetic.scan_age_ledger(fx["input_text"])
        assert result["status"] == fx["expected_verdict"]["status"]
        assert result["count"] >= fx["expected_verdict"]["count_at_least"]
        findings = result["findings"]
        assert any(f["kind"] == fx["expected_verdict"]["kind"] for f in findings)
        assert any(f["subject"] == fx["expected_verdict"]["subject"] for f in findings)

    def test_negative_control_passes(self):
        import narasi_arithmetic
        fx = self._fixture()
        result = narasi_arithmetic.scan_age_ledger(fx["negative_control_text"])
        assert result["status"] == fx["negative_control_expected"]["status"]
        assert result["count"] == fx["negative_control_expected"]["count"]

    def test_edge_case_empty_text_never_raises(self):
        import narasi_arithmetic
        result = narasi_arithmetic.scan_age_ledger("")
        assert result["status"] == "PASS"
        assert result["count"] == 0
