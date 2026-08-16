"""F4b — the repair-landing probe artifact.

🔴 THE DEFECT THIS PROBE EXISTS BECAUSE OF. The F4a probe keeps ONE `captured` slot for
   the raw response and its usage, so under a corrective retry response 2 overwrites
   response 1 and the artifact prices the LAST response as the cost of the run. Driven
   offline with two responses it reports 165 tokens where 330 were spent. It also has
   nowhere to record the only things F4b is about: the retry counters, whether a chapter
   was ACCEPTED, and whether the manuscript actually changed. So F4b has its own runner
   and its own schema, and the three F4a artifacts are neither rewritten nor reused as
   evidence for it.

🔴 A `dod` BLOCK, NOT A `passed` FLAG. Each landing requirement is reported separately:
   a reader must be able to see WHICH half failed, and a partial result must be
   impossible to read as a pass.
"""
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python/tools"))

import narasi_f4b_probe as probe  # noqa: E402

_HEX64 = 64

_TOP_KEYS = {
    "schema_version", "probe_kind", "recording_mode", "primary_response_observed",
    "executed_at_utc", "source", "provider", "delivery", "counts", "cost",
    "responses", "manuscript", "dod", "fault_injection",
}

_UNREADABLE_OP = json.dumps({
    "schema_version": "narasi_addressed_patch_v1",
    "operations": [{"op": None, "unit_id": "u002", "text": "Kandidat."}],
})
_GOOD = json.dumps({
    "schema_version": "narasi_addressed_patch_v1",
    "operations": [{"op": "replace", "unit_id": "u002",
                    "text": "Ia menyerahkan haknya di hadapan majelis sebelum "
                            "rekaman diputar."}],
})
#: 🔴 THE ADVERSARIAL CANDIDATE. It obeys the contract, passes the word band, the byte
#: band and the fidelity floor, and therefore LANDS — but it rewrites u003 as well as
#: the u002 the probe scripted. A repair-landing artifact that cannot tell this apart
#: from the intended single edit is not evidence of a bounded repair.
_TWO_UNIT_CHANGE = json.dumps({
    "schema_version": "narasi_addressed_patch_v1",
    "operations": [
        {"op": "replace", "unit_id": "u002",
         "text": "Ia menyerahkan haknya di hadapan majelis sebelum rekaman diputar."},
        {"op": "replace", "unit_id": "u003",
         "text": "Hakim lalu menyatakan rekaman itu tak dapat diterima."},
    ],
})


def _surrender_before_ruling(chapter):
    """The outline beat the probe's packet demands, evaluated on the unit table.

    Deliberately blunt and readable: the surrender must HAPPEN in some unit (a refusal
    does not count) and that unit must come before the one carrying the ruling."""
    import narasi_addressed_patch as ap

    units = [unit.text for unit in ap.segment_chapter(chapter).units]
    surrender = next((i for i, t in enumerate(units)
                      if "menyerahkan haknya" in t and "menolak" not in t), None)
    ruling = next((i for i, t in enumerate(units) if "tak dapat diterima" in t), None)
    return surrender is not None and ruling is not None and surrender < ruling


@pytest.fixture
def artifact(tmp_path):
    path = probe.run_probe(allow_network=False, out_dir=tmp_path,
                           now_utc="2026-08-15T00:00:00Z")
    return json.loads(path.read_text(encoding="utf-8")), path


# ── 🔴 the target must actually BE broken ──────────────────────────────────

def test_the_probe_target_genuinely_violates_the_beat_it_claims_to_repair():
    """🔴 THE PROBE WAS PROVING THE WRONG THING. Reused from F4a, the synthetic chapter
    ALREADY satisfied its own finding — surrender at u002 preceded the ruling at u003 —
    because F4a only ever needed a chapter that would provoke one call. The scripted
    "repair" then changed `haknya` to `seluruh haknya`: a cosmetic edit landing under
    the name of a structural one. F4b's target has to fail the beat BEFORE the repair
    and pass it after, or the artifact means nothing."""
    assert _surrender_before_ruling(probe.F4B_PROBE_CHAPTER) is False, (
        "the target must actually carry the defect the finding describes")

    repaired = probe.F4B_PROBE_CHAPTER.replace(
        "Ia menolak menyerahkan haknya dan menyimpan map itu kembali.",
        "Ia menyerahkan haknya di hadapan majelis sebelum rekaman diputar.")
    assert _surrender_before_ruling(repaired) is True


def test_the_f4a_target_would_not_have_proved_a_repair():
    """Kept as a standing explanation for why F4b does not reuse it."""
    import narasi_f4a_probe as f4a

    assert _surrender_before_ruling(f4a.PROBE_CHAPTER) is True, (
        "F4a's chapter already satisfies the beat — repairing it is unobservable")


def test_the_expected_edit_position_is_grounded_in_the_prompt(artifact):
    """`EXPECTED_CHANGED_POSITIONS` must follow from what the probe ASKED for, not from
    what the scripted response happened to touch. The finding, the fix and the packet
    all name u002 — index 1."""
    doc, _path = artifact
    assert probe.EXPECTED_CHANGED_POSITIONS == (1,)
    assert doc["manuscript"]["expected_changed_positions"] == [1]
    for text in (probe.F4B_PROBE_FINDING["evidence"], probe.F4B_PROBE_FINDING["fix"]):
        assert "u002" in text
    assert "[u002]" in probe.F4B_PROBE_PACKET["3"]
    assert "Do not touch any other unit." in probe.F4B_PROBE_FINDING["fix"]


def test_the_landed_repair_executes_the_beat(artifact):
    """The end-to-end claim, stated as the beat rather than as a diff: the run must
    take the chapter from violating the outline to satisfying it."""
    doc, _path = artifact
    assert doc["counts"]["chapters_accepted"] == 1
    assert doc["dod"]["met"] is True
    # The repaired text is deliberately not persisted, so the artifact proves the
    # change by hash; the beat itself is proved above on the same scripted repair.
    assert doc["manuscript"]["before_sha256"] != doc["manuscript"]["after_sha256"]


# ── shape ──────────────────────────────────────────────────────────────────

def test_the_artifact_has_exactly_the_declared_keys(artifact):
    doc, _path = artifact
    assert set(doc) == _TOP_KEYS
    assert doc["schema_version"] == probe.ARTIFACT_SCHEMA_VERSION
    assert doc["probe_kind"] == "targeted_non_delivery_repair_landing"
    assert set(doc["source"]) == {
        "head", "laozhang_api_sha256", "narasi_addressed_patch_sha256",
        "narasi_f4b_probe_sha256", "narasi_f4a_probe_sha256"}
    assert set(doc["provider"]) == {"phase", "model_alias", "resolved_model"}
    assert set(doc["delivery"]) == {"persisted", "returned_to_user"}
    assert set(doc["counts"]) == {
        "chapters_targeted", "chapters_attempted", "chapters_accepted",
        "provider_calls_reported", "provider_calls_observed",
        "adapter_invocations_observed", "responses_observed", "upstream_request_cap",
        "schema_retry_chapters", "schema_retry_accepted", "schema_retry_exhausted"}
    assert set(doc["cost"]) == {
        "prompt_tokens", "completion_tokens", "total_tokens",
        "estimated_usd_micros", "estimator", "usage_source",
        "responses_with_complete_usage", "credit_row_written"}
    assert set(doc["manuscript"]) == {
        "before_sha256", "after_sha256", "manuscript_changed", "heading_identical",
        "trailing_frame_identical", "unit_count_before", "unit_count_after",
        "unit_counts_match", "units_changed", "changed_unit_positions",
        "expected_changed_positions", "only_expected_units_changed",
        "untouched_units_identical", "all_original_bodies_present"}
    for item in doc["responses"]:
        assert set(item) == {
            "index", "prompt_tokens", "completion_tokens", "usage_metadata",
            "served_by", "served_model", "observation"}
    for item in doc["responses"]:
        assert item["usage_metadata"] in probe.USAGE_STATUSES
    assert doc["cost"]["usage_source"] in probe.USAGE_SOURCES
    assert set(doc["dod"]) == {"met", "checks"}


def test_the_schema_version_is_distinct_from_the_f4a_artifact(artifact):
    """🔴 A DIFFERENT SCHEMA AND A DIFFERENT FILENAME. Reusing F4a's name for a record
    with a different key set is exactly the ambiguity a schema version prevents, and a
    reader scanning `docs/audit/narasi/` must be able to tell the two apart by sight."""
    import narasi_f4a_probe as f4a

    doc, path = artifact
    assert doc["schema_version"] != f4a.ARTIFACT_SCHEMA_VERSION
    assert doc["schema_version"].startswith("narasi_f4b_probe_artifact_")
    assert path.name.startswith("f4b-repair-landing-probe-")
    assert "f4a" not in path.name


def test_every_enum_is_closed(artifact):
    doc, _path = artifact
    assert doc["recording_mode"] in probe.RECORDING_MODES
    for item in doc["responses"]:
        assert item["observation"]["validator_outcome"] in probe.VALIDATOR_OUTCOMES
        assert item["observation"]["operation_source"] in probe.OPERATION_SOURCES
        assert item["observation"]["subtype"] in probe.SUBTYPES
    assert doc["provider"]["phase"] == "canon_diff_revise"


def test_hashes_are_lowercase_hex_of_the_right_width(artifact):
    doc, _path = artifact
    hashes = [doc["source"]["laozhang_api_sha256"],
              doc["source"]["narasi_addressed_patch_sha256"],
              doc["manuscript"]["before_sha256"], doc["manuscript"]["after_sha256"]]
    hashes += [item["observation"]["token_sha256"] for item in doc["responses"]]
    for value in hashes:
        assert isinstance(value, str) and len(value) == _HEX64
        assert value == value.lower()
        assert all(c in "0123456789abcdef" for c in value)


# ── 🔴 the regression: usage is SUMMED, never last-write-wins ──────────────

def test_the_cost_is_the_sum_over_every_response_not_the_last_one(tmp_path):
    """🔴 THE EXACT DEFECT. Two responses, one usage slot: the F4a probe records the
    second and publishes it as the run's cost — 165 tokens against 330 actually spent,
    on a run whose own `provider_calls` says 2. An artifact that under-reports spend by
    half is worse than no artifact, because it looks like evidence."""
    path = probe.run_probe(allow_network=False, out_dir=tmp_path,
                           now_utc="2026-08-15T00:00:00Z",
                           offline_responses=[_UNREADABLE_OP, _UNREADABLE_OP])
    doc = json.loads(path.read_text(encoding="utf-8"))

    assert doc["counts"]["provider_calls_reported"] == 2
    assert doc["counts"]["provider_calls_observed"] == 2
    assert doc["counts"]["responses_observed"] == 2
    assert len(doc["responses"]) == 2
    assert doc["cost"]["prompt_tokens"] == 240
    assert doc["cost"]["completion_tokens"] == 90
    assert doc["cost"]["total_tokens"] == 330, (
        "165 means the second response overwrote the first instead of being added")
    assert doc["cost"]["total_tokens"] == sum(
        item["prompt_tokens"] + item["completion_tokens"] for item in doc["responses"])


def test_distinct_usages_are_added_rather_than_doubled(monkeypatch, tmp_path):
    """The sum must be a real sum. Two responses with DIFFERENT usage rule out a probe
    that simply multiplies one record by the call count."""
    import laozhang_api as lz

    served = [(_UNREADABLE_OP, 11, 1), (_GOOD, 7, 2)]

    def _create(**_kw):
        content, prompt, completion = served.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content),
                                     finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion))

    def _factory(*_a, **_k):
        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=_create)))
        client.with_options = lambda **_kw: client
        return client

    monkeypatch.setattr(lz, "make_narasi_client", _factory)
    monkeypatch.setattr(lz, "_narasi_revise_timeout", lambda *_a: 5.0)
    path = probe.run_probe(allow_network=True, out_dir=tmp_path,
                           now_utc="2026-08-15T00:00:00Z", model_alias="opus-4-6")
    doc = json.loads(path.read_text(encoding="utf-8"))

    assert [(r["prompt_tokens"], r["completion_tokens"]) for r in doc["responses"]] == [
        (11, 1), (7, 2)]
    assert doc["cost"]["prompt_tokens"] == 18
    assert doc["cost"]["completion_tokens"] == 3
    assert doc["cost"]["total_tokens"] == 21


# ── the landing claims F4a had no field for ────────────────────────────────

def test_a_landed_repair_reports_every_requirement_and_meets_the_dod(artifact):
    doc, _path = artifact
    counts, manuscript, dod = doc["counts"], doc["manuscript"], doc["dod"]

    assert counts["chapters_attempted"] == 1
    assert counts["provider_calls_reported"] == 2
    assert counts["provider_calls_observed"] == 2
    assert counts["chapters_accepted"] == 1
    assert (counts["schema_retry_chapters"], counts["schema_retry_accepted"],
            counts["schema_retry_exhausted"]) == (1, 1, 0)
    assert manuscript["before_sha256"] != manuscript["after_sha256"]
    assert manuscript["manuscript_changed"] is True
    assert manuscript["heading_identical"] is True
    assert manuscript["trailing_frame_identical"] is True
    assert manuscript["untouched_units_identical"] is True
    assert manuscript["only_expected_units_changed"] is True
    assert manuscript["unit_counts_match"] is True
    assert manuscript["units_changed"] == 1
    assert manuscript["changed_unit_positions"] == [1]
    assert manuscript["expected_changed_positions"] == [1]
    assert doc["cost"]["usage_source"] == "provider_reported"
    assert doc["cost"]["responses_with_complete_usage"] == 2
    assert dod["met"] is True
    assert all(dod["checks"].values())
    assert doc["cost"]["credit_row_written"] is False
    assert doc["delivery"] == {"persisted": False, "returned_to_user": False}


def test_a_run_where_nothing_lands_fails_the_dod_and_names_the_failures(tmp_path):
    """🔴 THE ARTIFACT MUST BE ABLE TO SAY NO. A probe that can only produce a pass is
    not evidence. Both responses break the contract, so two calls are spent, nothing
    lands, and the record must say exactly which requirements went unmet."""
    path = probe.run_probe(allow_network=False, out_dir=tmp_path,
                           now_utc="2026-08-15T00:00:00Z",
                           offline_responses=[_UNREADABLE_OP, _UNREADABLE_OP])
    doc = json.loads(path.read_text(encoding="utf-8"))

    assert doc["counts"]["chapters_accepted"] == 0
    assert doc["counts"]["schema_retry_exhausted"] == 1
    assert doc["manuscript"]["manuscript_changed"] is False
    assert doc["manuscript"]["before_sha256"] == doc["manuscript"]["after_sha256"]
    assert doc["dod"]["met"] is False
    failed = {k for k, v in doc["dod"]["checks"].items() if not v}
    assert failed == {"retry_accepted_once", "retry_not_exhausted",
                      "one_chapter_accepted", "manuscript_changed",
                      # nothing changed at all, so the scripted edit did not land
                      # either — the check reads the expectation, not the outcome
                      "only_expected_units_changed"}
    # The requirements that DID hold must still read as true — a blanket false would
    # tell a reader nothing about where the run actually broke.
    assert doc["dod"]["checks"]["two_physical_calls"] is True
    assert doc["dod"]["checks"]["usage_recorded_for_every_response"] is True


def test_usage_that_the_provider_never_reported_is_not_called_recorded(
        monkeypatch, tmp_path):
    """🔴 A COUNT OF RESPONSES IS NOT A COUNT OF USAGE. The check used to compare
    `responses == calls` and nothing else, so a provider that returned no usage
    metadata produced an artifact claiming complete cost coverage over ZERO tokens —
    two billable calls priced at nothing, DoD met. A zero that means "the provider told
    us nothing" and a zero that means "no tokens" are different facts about cost."""
    import laozhang_api as lz

    served = [_UNREADABLE_OP, _GOOD]

    def _create(**_kw):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=served.pop(0)),
                                     finish_reason="stop")],
            usage=None)                                 # the provider reported nothing

    def _factory(*_a, **_k):
        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=_create)))
        client.with_options = lambda **_kw: client
        return client

    monkeypatch.setattr(lz, "make_narasi_client", _factory)
    monkeypatch.setattr(lz, "_narasi_revise_timeout", lambda *_a: 5.0)
    path = probe.run_probe(allow_network=True, out_dir=tmp_path,
                           now_utc="2026-08-15T00:00:00Z", model_alias="opus-4-6")
    doc = json.loads(path.read_text(encoding="utf-8"))

    # The repair itself DID land — this is purely about what the cost block may claim.
    assert doc["counts"]["chapters_accepted"] == 1
    assert doc["counts"]["provider_calls_observed"] == 2
    assert doc["cost"]["total_tokens"] == 0
    assert doc["cost"]["usage_source"] == "absent"
    assert doc["cost"]["responses_with_complete_usage"] == 0
    assert all(item["usage_metadata"] == "absent" for item in doc["responses"])
    assert doc["dod"]["checks"]["usage_recorded_for_every_response"] is False
    assert doc["dod"]["met"] is False, (
        "an artifact may not publish complete cost coverage over zero tokens")


def test_partial_usage_is_named_incomplete_rather_than_rounded_up(
        monkeypatch, tmp_path):
    """One response with usage, one without: `incomplete`, never `provider_reported`."""
    import laozhang_api as lz

    served = [(_UNREADABLE_OP, SimpleNamespace(prompt_tokens=5, completion_tokens=2)),
              (_GOOD, None)]

    def _create(**_kw):
        content, usage = served.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content),
                                     finish_reason="stop")],
            usage=usage)

    def _factory(*_a, **_k):
        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=_create)))
        client.with_options = lambda **_kw: client
        return client

    monkeypatch.setattr(lz, "make_narasi_client", _factory)
    monkeypatch.setattr(lz, "_narasi_revise_timeout", lambda *_a: 5.0)
    path = probe.run_probe(allow_network=True, out_dir=tmp_path,
                           now_utc="2026-08-15T00:00:00Z", model_alias="opus-4-6")
    doc = json.loads(path.read_text(encoding="utf-8"))

    assert doc["cost"]["usage_source"] == "incomplete"
    assert doc["cost"]["responses_with_complete_usage"] == 1
    assert [item["usage_metadata"] for item in doc["responses"]] == \
        ["complete", "absent"]
    assert doc["cost"]["total_tokens"] == 7
    assert doc["dod"]["checks"]["usage_recorded_for_every_response"] is False
    assert doc["dod"]["met"] is False


def test_usage_missing_one_field_inside_a_response_is_partial_not_complete(
        monkeypatch, tmp_path):
    """🔴 "SOME USAGE" IS NOT "USAGE". The first fix asked for prompt tokens OR
    completion tokens, so a provider reporting only `prompt_tokens` produced
    `usage_source=provider_reported` and a met DoD over a cost missing every completion
    token. Completeness needs BOTH, and the half-present case needs its own name rather
    than promotion to the full one."""
    import laozhang_api as lz

    served = [_UNREADABLE_OP, _GOOD]

    def _create(**_kw):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=served.pop(0)),
                                     finish_reason="stop")],
            # prompt_tokens only — exactly the repro
            usage=SimpleNamespace(prompt_tokens=13))

    def _factory(*_a, **_k):
        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=_create)))
        client.with_options = lambda **_kw: client
        return client

    monkeypatch.setattr(lz, "make_narasi_client", _factory)
    monkeypatch.setattr(lz, "_narasi_revise_timeout", lambda *_a: 5.0)
    path = probe.run_probe(allow_network=True, out_dir=tmp_path,
                           now_utc="2026-08-15T00:00:00Z", model_alias="opus-4-6")
    doc = json.loads(path.read_text(encoding="utf-8"))

    assert doc["recording_mode"] == "live"
    assert doc["counts"]["chapters_accepted"] == 1, "the repair itself still landed"
    assert [item["usage_metadata"] for item in doc["responses"]] == \
        ["partial", "partial"]
    assert doc["cost"]["usage_source"] == "incomplete"
    assert doc["cost"]["responses_with_complete_usage"] == 0
    assert doc["cost"]["completion_tokens"] == 0
    assert doc["dod"]["checks"]["usage_recorded_for_every_response"] is False
    assert doc["dod"]["met"] is False, (
        "a cost missing every completion token is not complete coverage")


# ── the two-call bound belongs to the runner, not only the CLI ─────────────

@pytest.mark.parametrize("cap", [0, 3, 5, -1])
def test_run_probe_itself_refuses_a_cap_outside_the_authorised_bound(tmp_path, cap):
    """🔴 THE PERMISSION IS ENFORCED WHERE IT IS GRANTED. `main()` checking the cap only
    protects the command line: any caller reaching `run_probe()` directly — a test, a
    script, a future runner — could ask for five and get a met DoD over five billable
    calls. The function that installs the ledger is the one that must refuse."""
    with pytest.raises(ValueError, match="bounded at 2"):
        probe.run_probe(allow_network=False, out_dir=tmp_path,
                        now_utc="2026-08-15T00:00:00Z", upstream_cap=cap)
    assert not list(tmp_path.iterdir()), "a refused run must not write an artifact"


@pytest.mark.parametrize("cap", [1, 2])
def test_run_probe_accepts_the_two_caps_inside_the_bound(tmp_path, cap):
    path = probe.run_probe(allow_network=False, out_dir=tmp_path,
                           now_utc="2026-08-15T00:00:00Z", upstream_cap=cap)
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["counts"]["upstream_request_cap"] == cap
    assert doc["counts"]["provider_calls_observed"] <= cap


def test_a_candidate_that_rewrites_a_second_unit_fails_the_dod(tmp_path):
    """🔴 THE TAUTOLOGY THIS REPLACED. `untouched_units_identical` used to collect the
    positions that DIFFERED, call them `changed`, then compare the positions outside
    that set — equal by construction, `True` for every possible input. The candidate
    got to define the property it was being judged against.

    This candidate is a legal patch: it obeys the contract, clears the word band, the
    byte band and the fidelity floor, and LANDS. It also rewrites u003, which the probe
    never scripted. The comparison is now scoped to the EXPECTED position, so the
    second edit is visible and the DoD refuses the run."""
    path = probe.run_probe(allow_network=False, out_dir=tmp_path,
                           now_utc="2026-08-15T00:00:00Z",
                           offline_responses=[_UNREADABLE_OP, _TWO_UNIT_CHANGE])
    doc = json.loads(path.read_text(encoding="utf-8"))
    manuscript = doc["manuscript"]

    assert doc["counts"]["chapters_accepted"] == 1, (
        "the fixture must actually land, or it proves nothing about identity")
    assert manuscript["manuscript_changed"] is True
    assert manuscript["unit_counts_match"] is True
    assert manuscript["units_changed"] == 2
    assert manuscript["changed_unit_positions"] == [1, 2]
    assert manuscript["only_expected_units_changed"] is False
    assert manuscript["untouched_units_identical"] is False, (
        "position 2 was rewritten and was never in the expected edit")
    assert doc["dod"]["met"] is False
    failed = {k for k, v in doc["dod"]["checks"].items() if not v}
    assert failed == {"only_expected_units_changed", "untouched_units_identical"}


def test_the_cap_is_enforced_not_promised(tmp_path):
    """cap=1 refuses the retry above the transport: one call, nothing landed, and the
    DoD honestly not met. The cap is what holds the money line."""
    path = probe.run_probe(allow_network=False, out_dir=tmp_path,
                           now_utc="2026-08-15T00:00:00Z", upstream_cap=1,
                           offline_responses=[_UNREADABLE_OP, _GOOD])
    doc = json.loads(path.read_text(encoding="utf-8"))

    assert doc["counts"]["provider_calls_observed"] == 1
    assert doc["counts"]["responses_observed"] == 1
    assert doc["cost"]["total_tokens"] == 165, "only one response was ever received"
    assert doc["counts"]["chapters_accepted"] == 0
    assert doc["dod"]["checks"]["within_upstream_cap"] is True
    assert doc["dod"]["checks"]["two_physical_calls"] is False
    assert doc["dod"]["met"] is False


# ── it cannot launder itself into evidence it is not ───────────────────────

def test_an_offline_dry_run_can_never_claim_to_be_live(artifact):
    doc, path = artifact
    assert doc["recording_mode"] == "offline_dry_run"
    assert doc["primary_response_observed"] is False
    assert "offline-dry-run" in path.name


def test_a_paid_run_that_never_reached_the_provider_is_not_live_evidence(
        monkeypatch, tmp_path):
    import laozhang_api as lz

    def _dead_factory(*_a, **_k):
        raise RuntimeError("no credential")

    monkeypatch.setattr(lz, "make_narasi_client", _dead_factory)
    path = probe.run_probe(allow_network=True, out_dir=tmp_path,
                           now_utc="2026-08-15T00:00:00Z", model_alias="opus-4-6")
    doc = json.loads(path.read_text(encoding="utf-8"))

    assert doc["counts"]["provider_calls_observed"] == 0
    assert doc["counts"]["responses_observed"] == 0
    assert doc["recording_mode"] == "failed_probe"
    assert doc["primary_response_observed"] is False
    assert doc["dod"]["met"] is False
    assert "failed-probe" in path.name


def test_a_live_run_that_misses_the_dod_says_so_in_its_own_filename(
        monkeypatch, tmp_path):
    """A live record that did not land must not sit in the directory looking like the
    one that did."""
    import laozhang_api as lz

    def _create(**_kw):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=_UNREADABLE_OP),
                                     finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    def _factory(*_a, **_k):
        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=_create)))
        client.with_options = lambda **_kw: client
        return client

    monkeypatch.setattr(lz, "make_narasi_client", _factory)
    monkeypatch.setattr(lz, "_narasi_revise_timeout", lambda *_a: 5.0)
    path = probe.run_probe(allow_network=True, out_dir=tmp_path,
                           now_utc="2026-08-15T00:00:00Z", model_alias="opus-4-6")
    doc = json.loads(path.read_text(encoding="utf-8"))

    assert doc["recording_mode"] == "live"
    assert doc["dod"]["met"] is False
    assert "dod-not-met" in path.name


def test_the_artifact_never_carries_prose_or_raw_responses(tmp_path):
    """Non-delivery is a property of the RECORD too. Only hashes, enums, lengths and
    counts leave the process — never the chapter, never a candidate, never a prompt."""
    marker = "Ia menyerahkan seluruh haknya"
    path = probe.run_probe(allow_network=False, out_dir=tmp_path,
                           now_utc="2026-08-15T00:00:00Z")
    blob = path.read_text(encoding="utf-8")

    assert marker not in blob, "the repaired prose was written into the artifact"
    assert "Mira membawa" not in blob, "the source chapter leaked"
    assert "Kandidat." not in blob, "a rejected candidate leaked"
    assert "EXACT OUTLINE" not in blob, "the prompt packet leaked"


def test_an_artifact_is_a_record_and_cannot_be_silently_overwritten(tmp_path):
    probe.run_probe(allow_network=False, out_dir=tmp_path,
                    now_utc="2026-08-15T00:00:00Z")
    with pytest.raises(FileExistsError):
        probe.run_probe(allow_network=False, out_dir=tmp_path,
                        now_utc="2026-08-15T00:00:00Z")


# ── the CLI gates ──────────────────────────────────────────────────────────
#
# 🔴 EVERY GATE TEST MUST BE MUTANT-SAFE. A mutant that DELETES one of these gates lets
#    `main()` fall through into a real `run_probe(allow_network=True, ...)`: it would
#    reach for the network AND write a stray artifact into the repo's own
#    `docs/audit/narasi`. The suite's outbound-network guard would fire, and the
#    mutation harness rightly refuses to score a guard trip as a kill (INCONCLUSIVE) —
#    so the mutant survives unproven and the repo gets dirtied. Found exactly that way.
#    The seam is stubbed and the output redirected so a deleted gate fails as a plain
#    assertion instead.

@pytest.fixture
def sealed_cli(monkeypatch, tmp_path):
    """Neutralise the fall-through: no network reachable, nothing written to the repo."""
    import laozhang_api as lz

    def _create(**_kw):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=_GOOD),
                                     finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    def _factory(*_a, **_k):
        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=_create)))
        client.with_options = lambda **_kw: client
        return client

    monkeypatch.setattr(lz, "make_narasi_client", _factory)
    monkeypatch.setattr(lz, "_narasi_revise_timeout", lambda *_a: 5.0)
    return ["--out-dir", str(tmp_path)]


def test_network_mode_needs_the_explicit_confirmation_variable(
        monkeypatch, capsys, sealed_cli):
    monkeypatch.delenv("NARASI_F4B_PROBE_CONFIRM", raising=False)
    assert probe.main(["--allow-network", "--model", "opus-4-6"] + sealed_cli) == 2
    assert "NARASI_F4B_PROBE_CONFIRM=1" in capsys.readouterr().err


def test_network_mode_refuses_a_non_production_alias(monkeypatch, capsys, sealed_cli):
    monkeypatch.setenv("NARASI_F4B_PROBE_CONFIRM", "1")
    assert probe.main(["--allow-network", "--model", "test-model"] + sealed_cli) == 2
    assert "production --model" in capsys.readouterr().err


def test_the_upstream_cap_cannot_be_raised_above_two(monkeypatch, capsys, sealed_cli):
    """🔴 THE BOUND IS THE POINT. F4b is one call plus at most one corrective retry;
    a probe allowed to spend more is no longer the thing that was authorised."""
    monkeypatch.setenv("NARASI_F4B_PROBE_CONFIRM", "1")
    assert probe.main(["--allow-network", "--model", "opus-4-6",
                       "--max-upstream", "5"] + sealed_cli) == 2
    assert "bounded at 2" in capsys.readouterr().err


# ── the CLI must FAIL when the run did not demonstrate what it claims ──────

def test_the_cli_exits_zero_only_when_the_dod_was_met(tmp_path, capsys):
    """🔴 THE FILENAME IS NOT THE INTERFACE. A run that spent two billable calls and
    landed nothing wrote `-dod-not-met` into its name and then returned 0 — automation
    and an operator both read the exit code, and both were told it worked."""
    assert probe.main(["--out-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "dod.met=True" in out


def test_the_cli_fails_closed_when_the_dod_is_not_met(tmp_path, capsys):
    assert probe.main(["--out-dir", str(tmp_path), "--max-upstream", "1"]) == 1
    captured = capsys.readouterr()
    assert "dod.met=False" in captured.out
    assert "failed_checks=" in captured.out
    assert "definition of done was not met" in captured.err


def test_the_cli_refuses_a_bad_cap_cleanly_instead_of_crashing(tmp_path, capsys):
    """A runner bound is a REFUSAL, not a crash: a traceback reads as a broken tool and
    invites someone to work around it. One line, exit 2, same as every other gate."""
    assert probe.main(["--fault-injection", "--max-upstream", "1",
                       "--out-dir", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "refusing:" in err and "the retry is the thing under test" in err
    assert not list(tmp_path.iterdir())


def test_the_cli_fails_a_network_run_that_is_not_live_evidence(
        monkeypatch, tmp_path, capsys):
    import laozhang_api as lz

    def _dead_factory(*_a, **_k):
        raise RuntimeError("no credential")

    monkeypatch.setattr(lz, "make_narasi_client", _dead_factory)
    monkeypatch.setenv("NARASI_F4B_PROBE_CONFIRM", "1")
    assert probe.main(["--allow-network", "--model", "opus-4-6",
                       "--out-dir", str(tmp_path)]) == 1
    assert "not live evidence" in capsys.readouterr().err


# ══════════════════════════════════════════════════════════════════════════
# DETERMINISTIC FAULT INJECTION — the only accepted route to F4b `CLOSED`
#
# 🔴 WHY IT EXISTS. The live probe of 2026-08-15T17:16:24Z proved structural repair can
#    land, but the model answered correctly on the first call, so the corrective RETRY
#    never fired. Re-rolling until it misbehaves is stochastic and was refused; a probe
#    whose first response is SCRIPTED contradicts the DoD's `two_physical_calls`. What
#    is left is to keep BOTH calls real and make the defect deterministic: after the
#    first response has been received AND METERED, the probe — never production code —
#    replaces its candidate with the unreadable-`op` class F4a proved occurs naturally.
#
#    Six properties have to hold, and each is pinned below: injection happens after
#    receive+meter, it lives only in the probe, production source is untouched, both
#    calls are physical, the cap stays two, and the artifact can neither hide nor
#    mislabel the injection.
# ══════════════════════════════════════════════════════════════════════════

def _injected_run(tmp_path, *, responses=None, **kwargs):
    return json.loads(probe.run_probe(
        allow_network=False, out_dir=tmp_path, now_utc="2026-08-15T00:00:00Z",
        offline_responses=responses if responses is not None else [_GOOD, _GOOD],
        fault_injection=True, **kwargs).read_text(encoding="utf-8"))


# ── 1. the injection happens AFTER the response was received and metered ───

def test_the_injection_fires_only_after_the_first_response_was_metered(tmp_path):
    """🔴 THE ORDERING IS THE WHOLE CLAIM. If the candidate were swapped before
    metering, the artifact's cost would describe a response the engine never judged and
    the run would prove nothing about a REAL first call. The order is production's own
    (`_exchange` meters, then reads the candidate), so the recorded trace is evidence
    rather than assertion."""
    doc = _injected_run(tmp_path)
    fi = doc["fault_injection"]

    assert fi["applied"] is True
    assert fi["applied_after_metering"] is True
    seq = fi["event_sequence"]
    assert "metered:1" in seq and "injected:1" in seq
    assert seq.index("metered:1") < seq.index("injected:1"), seq
    # The SECOND response is metered after the injection — i.e. a real retry followed.
    assert "metered:2" in seq
    assert seq.index("injected:1") < seq.index("metered:2"), seq
    assert doc["cost"]["usage_source"] == "provider_reported"
    assert doc["cost"]["responses_with_complete_usage"] == 2


def test_the_first_response_is_recorded_as_the_provider_actually_answered_it(tmp_path):
    """🔴 THE INJECTION MUST NOT FALSIFY THE RECORD. `responses[0].observation`
    describes what the PROVIDER returned; here that is a well-formed patch, and the
    artifact says so. The injected payload appears only as its own hash, never as the
    model's answer."""
    doc = _injected_run(tmp_path)

    assert doc["responses"][0]["observation"]["validator_outcome"] == "accepted", (
        "the model's real first answer was valid and must be recorded as valid")
    assert doc["fault_injection"]["injected_payload_sha256"] == hashlib.sha256(
        probe._INJECTED_UNREADABLE.encode()).hexdigest()
    assert doc["fault_injection"]["class"] == "unknown_operation_unreadable"


# ── 2+3. the injection lives ONLY in the probe; production is untouched ────

def test_production_code_carries_no_trace_of_the_injection():
    """🔴 PRODUCTION MUST NOT KNOW A PROBE IS RUNNING. A repair engine with a test hook
    in it is a different engine from the one that serves customers, and every result
    obtained through that hook would describe the wrong system."""
    root = Path(__file__).resolve().parents[2]
    for name in ("python/laozhang_api.py", "python/narasi_addressed_patch.py"):
        source = (root / name).read_text(encoding="utf-8")
        # PRECISE markers only. A bare "inject" also matches production's own
        # "injects billing context" — a test that fails on an innocent English word
        # teaches people to weaken it, which is how a real hook eventually slips
        # through. Every marker here is unique to the probe.
        for marker in ("fault_injection", "_INJECTED_UNREADABLE", "narasi_f4b_probe",
                       "deterministic_fault_injection", "unknown_operation_unreadable"):
            assert marker not in source, f"{marker!r} leaked into {name}"


def test_the_injection_does_not_outlive_the_run(tmp_path):
    """The probe swaps two production seams and must hand both back. A leaked
    `_resp_content` wrapper would silently corrupt the next caller in this process."""
    import laozhang_api as lz

    before_usage, before_content = lz._log_narasi_usage, lz._resp_content
    _injected_run(tmp_path)
    assert lz._log_narasi_usage is before_usage
    assert lz._resp_content is before_content, "the injection seam outlived the probe"

    # ...and the restored reader is the real one: it does NOT return the payload.
    response = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content="halo"), finish_reason="stop")])
    assert lz._resp_content(response) == "halo"


# ── 4. BOTH calls are physical ─────────────────────────────────────────────

def test_both_calls_are_real_and_the_retry_is_the_second_of_them(tmp_path):
    doc = _injected_run(tmp_path)
    counts = doc["counts"]

    assert counts["provider_calls_reported"] == 2
    assert counts["provider_calls_observed"] == 2
    assert counts["responses_observed"] == 2
    assert len(doc["responses"]) == 2
    assert counts["chapters_attempted"] == 1, "still ONE chapter attempt"
    assert (counts["schema_retry_chapters"], counts["schema_retry_accepted"],
            counts["schema_retry_exhausted"]) == (1, 1, 0)
    assert doc["dod"]["checks"]["two_physical_calls"] is True


def test_the_injected_run_lands_the_repair_on_the_expected_unit_only(tmp_path):
    doc = _injected_run(tmp_path)
    m = doc["counts"], doc["manuscript"]
    counts, manuscript = m

    assert counts["chapters_accepted"] == 1
    assert manuscript["manuscript_changed"] is True
    assert manuscript["before_sha256"] != manuscript["after_sha256"]
    assert manuscript["changed_unit_positions"] == [1]
    assert manuscript["only_expected_units_changed"] is True
    assert manuscript["untouched_units_identical"] is True
    assert manuscript["heading_identical"] is True
    assert manuscript["trailing_frame_identical"] is True
    assert doc["delivery"] == {"persisted": False, "returned_to_user": False}
    assert doc["cost"]["credit_row_written"] is False
    assert doc["dod"]["met"] is True


# ── 5. the cap stays two ───────────────────────────────────────────────────

@pytest.mark.parametrize("cap", [1, 3, 5])
def test_fault_injection_refuses_any_cap_other_than_two(tmp_path, cap):
    """A cap of 1 would refuse the retry above the transport, so the run could only ever
    fail its own DoD — spending a billable call to prove nothing. Anything above 2 is
    outside the authorisation."""
    with pytest.raises(ValueError):
        probe.run_probe(allow_network=False, out_dir=tmp_path,
                        now_utc="2026-08-15T00:00:00Z", upstream_cap=cap,
                        fault_injection=True)
    assert not list(tmp_path.iterdir()), "a refused run must not write an artifact"


def test_the_injected_run_stays_inside_its_two_call_budget(tmp_path):
    doc = _injected_run(tmp_path)
    assert doc["counts"]["upstream_request_cap"] == 2
    assert doc["counts"]["provider_calls_observed"] <= 2
    assert doc["dod"]["checks"]["within_upstream_cap"] is True


# ── 6. the artifact can neither HIDE nor MISLABEL the injection ────────────

def test_an_injected_run_is_named_as_one_everywhere_a_reader_looks(tmp_path):
    """🔴 THE MOST MISLEADING ARTIFACT THIS WORKSTREAM COULD PRODUCE would be an
    injected run filed as a naturally occurring one. Kind, filename and block must all
    say so."""
    path = probe.run_probe(allow_network=False, out_dir=tmp_path,
                           now_utc="2026-08-15T00:00:00Z",
                           offline_responses=[_GOOD, _GOOD], fault_injection=True)
    doc = json.loads(path.read_text(encoding="utf-8"))

    assert doc["probe_kind"] == "deterministic_fault_injection"
    assert doc["probe_kind"] in probe.PROBE_KINDS
    assert path.name.startswith("f4b-fault-injection-probe-")
    assert "repair-landing" not in path.name
    assert doc["fault_injection"]["requested"] is True
    assert doc["fault_injection"]["applied"] is True


def test_a_normal_run_is_never_labelled_as_injected_and_carries_the_block_anyway(
        tmp_path):
    """The block is present in BOTH modes: an absent block would let a reader take
    silence for a clean run."""
    path = probe.run_probe(allow_network=False, out_dir=tmp_path,
                           now_utc="2026-08-15T00:00:00Z")
    doc = json.loads(path.read_text(encoding="utf-8"))

    assert doc["probe_kind"] == "targeted_non_delivery_repair_landing"
    assert path.name.startswith("f4b-repair-landing-probe-")
    assert set(doc["fault_injection"]) == {
        "requested", "applied", "class", "target_response_index",
        "applied_after_metering", "injected_payload_sha256", "event_sequence"}
    assert doc["fault_injection"]["requested"] is False
    assert doc["fault_injection"]["applied"] is False
    assert doc["fault_injection"]["class"] is None
    assert doc["fault_injection"]["injected_payload_sha256"] is None
    assert doc["dod"]["checks"]["no_fault_injection"] is True
    assert "fault_injection_applied" not in doc["dod"]["checks"]


def test_the_dod_of_an_injected_run_requires_the_injection_to_have_happened(tmp_path):
    """The mode-specific checks exist, and they are REQUIRED — not decoration."""
    doc = _injected_run(tmp_path)
    checks = doc["dod"]["checks"]

    for key in ("fault_injection_applied", "fault_injection_after_metering",
                "fault_injection_target_is_first_response"):
        assert checks[key] is True, key
    assert "no_fault_injection" not in checks
    assert doc["dod"]["met"] is True


def test_an_injection_that_could_not_happen_is_reported_as_not_applied(
        monkeypatch, tmp_path):
    """🔴 THE CHECK MUST BE ABLE TO READ FALSE. If the first call never returns a
    response there is nothing to inject into — and a run that still claimed
    `fault_injection_applied` would be asserting an event that did not occur, on an
    artifact whose whole purpose is to declare that event honestly."""
    import laozhang_api as lz

    def _create(**_kw):
        raise RuntimeError("provider exploded on the first call")

    def _factory(*_a, **_k):
        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=_create)))
        client.with_options = lambda **_kw: client
        return client

    monkeypatch.setattr(lz, "make_narasi_client", _factory)
    monkeypatch.setattr(lz, "_narasi_revise_timeout", lambda *_a: 5.0)
    path = probe.run_probe(allow_network=True, out_dir=tmp_path,
                           now_utc="2026-08-15T00:00:00Z", model_alias="opus-4-6",
                           fault_injection=True)
    doc = json.loads(path.read_text(encoding="utf-8"))

    assert doc["counts"]["responses_observed"] == 0
    assert doc["fault_injection"]["applied"] is False
    assert doc["fault_injection"]["requested"] is True, (
        "the run still ASKED for injection — that must stay on the record")
    assert doc["fault_injection"]["target_response_index"] is None
    assert doc["dod"]["checks"]["fault_injection_applied"] is False
    assert doc["dod"]["checks"]["fault_injection_after_metering"] is False
    assert doc["dod"]["met"] is False


def test_an_injected_run_whose_retry_does_not_land_still_fails_honestly(tmp_path):
    """The injection forces the retry; it does not force the retry to SUCCEED. If the
    second response is unusable the run must fail its DoD while still declaring the
    injection."""
    doc = _injected_run(tmp_path, responses=[_GOOD, _UNREADABLE_OP])

    assert doc["fault_injection"]["applied"] is True
    assert doc["counts"]["provider_calls_observed"] == 2
    assert doc["counts"]["chapters_accepted"] == 0
    assert doc["counts"]["schema_retry_exhausted"] == 1
    assert doc["manuscript"]["manuscript_changed"] is False
    assert doc["dod"]["met"] is False
    failed = {k for k, v in doc["dod"]["checks"].items() if not v}
    assert "fault_injection_applied" not in failed, "the injection itself DID happen"
    assert {"one_chapter_accepted", "manuscript_changed"} <= failed


def test_the_v2_schema_is_declared_and_distinct_from_the_live_v1_artifact(tmp_path):
    """The key set changed, so the version moved. The live v1 artifact from
    2026-08-15T17:16:24Z stays v1 and is not rewritten."""
    doc = _injected_run(tmp_path)
    assert doc["schema_version"] == "narasi_f4b_probe_artifact_v2"
    assert probe.ARTIFACT_SCHEMA_VERSION.endswith("_v2")

    live = (Path(__file__).resolve().parents[2] / "docs/audit/narasi"
            / "f4b-repair-landing-probe-2026-08-15T171624Z-dod-not-met.json")
    if live.exists():
        recorded = json.loads(live.read_text(encoding="utf-8"))
        assert recorded["schema_version"] == "narasi_f4b_probe_artifact_v1", (
            "the earlier live artifact must not be rewritten into the new schema")


# ── the structural-identity helper, exercised directly ─────────────────────

def test_untouched_unit_identity_reads_false_when_an_unexpected_unit_moves():
    """🔴 THE FIELD MUST BE CAPABLE OF READING FALSE ON ITS OWN. Scoping the comparison
    to the EXPECTED position is what gives it that capability: a mutant replacing the
    computation with the literal `True` is now distinguishable, which it was not while
    "untouched" meant "whatever the candidate did not touch"."""
    before = ("## Bab 3: Atap\n\nSatu kalimat pertama.\n\nDua kalimat kedua.\n\n"
              "Tiga kalimat ketiga.\n")
    expected_only = before.replace("Dua kalimat kedua.",
                                   "Dua kalimat kedua yang diperbaiki.")
    also_third = expected_only.replace("Tiga kalimat ketiga.", "Tiga kalimat DIRUSAK.")
    wrong_unit = before.replace("Satu kalimat pertama.", "Satu kalimat DIRUSAK.")

    ok = probe._structural_identity(before, expected_only)
    assert ok["manuscript_changed"] is True
    assert ok["unit_counts_match"] is True
    assert ok["units_changed"] == 1
    assert ok["changed_unit_positions"] == [1]
    assert ok["only_expected_units_changed"] is True
    assert ok["untouched_units_identical"] is True
    assert ok["heading_identical"] is True
    assert ok["trailing_frame_identical"] is True

    two = probe._structural_identity(before, also_third)
    assert two["units_changed"] == 2
    assert two["changed_unit_positions"] == [1, 2]
    assert two["only_expected_units_changed"] is False
    assert two["untouched_units_identical"] is False, (
        "a second, unscripted edit must not read as untouched")

    # The RIGHT number of edits in the WRONG place is still a failure.
    elsewhere = probe._structural_identity(before, wrong_unit)
    assert elsewhere["units_changed"] == 1
    assert elsewhere["changed_unit_positions"] == [0]
    assert elsewhere["only_expected_units_changed"] is False
    assert elsewhere["untouched_units_identical"] is False

    unchanged = probe._structural_identity(before, before)
    assert unchanged["manuscript_changed"] is False
    assert unchanged["before_sha256"] == unchanged["after_sha256"]
    assert unchanged["units_changed"] == 0
    assert unchanged["only_expected_units_changed"] is False


def test_a_topology_change_is_reported_as_a_count_mismatch_not_compared_positionally():
    """Insert/move make positional comparison meaningless, so it is refused rather than
    performed on misaligned indices."""
    before = ("## Bab 3: Atap\n\nSatu kalimat pertama.\n\nDua kalimat kedua.\n\n"
              "Tiga kalimat ketiga.\n")
    inserted = before.replace("Dua kalimat kedua.",
                              "Sisipan baru.\n\nDua kalimat kedua.")

    out = probe._structural_identity(before, inserted)
    assert out["unit_counts_match"] is False
    assert out["unit_count_before"] == 3 and out["unit_count_after"] == 4
    assert out["units_changed"] == 1
    assert out["only_expected_units_changed"] is False
    assert out["untouched_units_identical"] is False


def test_a_changed_heading_or_frame_is_reported_as_such():
    before = "## Bab 3: Atap\n\nSatu kalimat pertama.\n\nDua kalimat kedua.\n"
    assert probe._structural_identity(
        before, before.replace("## Bab 3: Atap", "## Bab 4: Atap")
    )["heading_identical"] is False
    assert probe._structural_identity(
        before, before.rstrip("\n"))["trailing_frame_identical"] is False
