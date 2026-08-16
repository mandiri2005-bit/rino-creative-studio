"""F4b — ONE bounded corrective retry, and structural repair that actually LANDS.

BRIEF-FOR-CODEX-2026-08-14-POST-CANARY-V9.md §F4b (`fecd3dcb…20b5`) +
BRIEF-KONSOLIDASI-V5-FINAL-2026-08-14-CONTINUITY-V8.md (`953e1dc2…2c98`).

🔴 WHY THE NORMALIZER WAS NOT THE ANSWER. `_normalize_verb_as_key_operation` unwraps a
   response that put the verb in the WRAPPING KEY. It is grounded in a real probe and it
   stays. But three consecutive live probes (2026-08-15) came back
   `validator_outcome=unknown_operation`, `operation_source=unreadable`,
   `token_length=0` — an `op` that did not read as a non-empty string, which the
   normalizer does not cover. That is an equivalence CLASS, not an identified shape:
   null, a missing key, an integer, a list and a nested object all produce that same
   bounded record, and the telemetry cannot tell them apart by design. Widening the
   validator to chase an unidentified member would be guessing, and every guess
   permanently enlarges the vocabulary the four closed verbs exist to keep small. So
   F4b answers a broken response CONTRACT the only way that needs no guess: restate the
   contract and ask exactly once more — which repairs every member of the class alike.

🔴 WHAT SEPARATES A RETRY FROM SHOPPING FOR A VERDICT. Only failures to produce a
   well-formed patch are retryable. `word_band`, `byte_band`, `fidelity_below_minimum`,
   `ineffective`, `unknown_id`, `operation_overlap`, `move_cycle`, `move_unsatisfiable`
   describe a patch that was UNDERSTOOD and judged unfit — asking again buys a second
   opinion, not a corrected shape, and a guard you may re-roll is not a guard.
"""
import asyncio
import json

import pytest
from types import SimpleNamespace

import laozhang_api as lz

from test_narasi_addressed_patch import CHAPTER, _payload, plain_client  # noqa: E402


# ── fixtures: the first-response shapes that reproduce the live failure ─────

#: A REPRESENTATIVE fixture for the live observation — NOT "the live shape".
#:
#: 🔴 WHAT THE ARTIFACTS ACTUALLY PROVE, AND NO MORE. The three live probes recorded
#: `unknown_operation` / `operation_source=unreadable` / `subtype=other` /
#: `token_length=0` / hash = SHA-256 of the empty string. That says only that `op` did
#: not read as a non-empty string. `null`, a missing key, an integer, a list and a
#: nested object all satisfy it identically, and the telemetry is bounded by design so
#: it cannot distinguish them. Calling this "the live shape" would claim an
#: identification the evidence does not support — the same overclaim the whole F4a
#: telemetry design exists to refuse. It is one member of the observed equivalence
#: class, chosen because it needs no invented raw provider string;
#: `test_the_whole_unreadable_class_looks_identical_to_the_telemetry` pins that the
#: other members are indistinguishable from it, and that the retry repairs them all.
_OP_NULL_PATCH = _payload({"op": None, "unit_id": "u002", "text": "Kandidat."})
#: The rest of the equivalence class, spelled out rather than assumed.
_UNREADABLE_OP_VARIANTS = {
    "null": _payload({"op": None, "unit_id": "u002", "text": "Kandidat."}),
    "missing": _payload({"unit_id": "u002", "text": "Kandidat."}),
    "integer": _payload({"op": 7, "unit_id": "u002", "text": "Kandidat."}),
    "list": _payload({"op": [], "unit_id": "u002", "text": "Kandidat."}),
    "object": _payload({"op": {}, "unit_id": "u002", "text": "Kandidat."}),
    "empty_string": _payload({"op": "", "unit_id": "u002", "text": "Kandidat."}),
}
_NOT_JSON = "Ini bukan JSON, ini bab lengkap."
_BAD_SHAPE_PATCH = _payload(
    {"op": "replace", "unit_id": "u002", "text": "Kandidat lengkap.", "extra": True})

_GOOD_REPLACE = _payload({
    "op": "replace", "unit_id": "u002",
    "text": "Ia menyerahkan seluruh haknya sebelum rekaman diputar."})
_GOOD_INSERT_BEFORE = _payload({
    "op": "insert_before", "anchor_id": "u003",
    "text": "Rekaman itu tiba lebih dulu."})
_GOOD_INSERT_AFTER = _payload({
    "op": "insert_after", "anchor_id": "u001",
    "text": "Berkas itu sudah lengkap."})
_GOOD_MOVE = _payload({"op": "move", "unit_id": "u004", "before_id": "u003"})

_FINDING = {
    "severity": "high", "type": "outline_beat_order", "chapter": 3,
    "evidence": "Surrender must precede the ruling.",
    "fix": "Restore the outlined order.",
}


class _EmptyChoices:
    """An error-as-200: HTTP 200, real token usage, no usable choices. The provider has
    already charged for it — this is the shape that makes "was a response received?" and
    "is the response usable?" two DIFFERENT questions."""

    def __init__(self, prompt_tokens, completion_tokens):
        self.choices = []
        self.usage = SimpleNamespace(prompt_tokens=prompt_tokens,
                                     completion_tokens=completion_tokens)


def _install(monkeypatch, responses, *, credits_each=1, client=None):
    """Serve `responses` in order, one per physical `.create`. Records every call's
    kwargs so a test can read what actually went upstream — the corrective prompt is
    server-authored, so its contents are an assertion target, not a black box."""
    seam = {"calls": [], "usage_metered": 0, "tokens_metered": 0}
    served = list(responses)

    def _create(**kwargs):
        seam["calls"].append(kwargs)
        if not served:
            raise AssertionError(
                f"the lane made call #{len(seam['calls'])}; the stub has no response "
                "left, so more calls were issued than this test allows")
        content = served.pop(0)
        if isinstance(content, Exception):
            raise content
        if isinstance(content, _EmptyChoices):
            return content
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=content), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    def _factory(*_a, **_k):
        return client if client is not None else plain_client(
            SimpleNamespace(create=_create))

    async def _usage(_t, _u, _m, response, **_k):
        usage = getattr(response, "usage", None)
        seam["usage_metered"] += 1
        seam["tokens_metered"] += (int(getattr(usage, "prompt_tokens", 0) or 0)
                                   + int(getattr(usage, "completion_tokens", 0) or 0))
        return credits_each

    monkeypatch.setattr(lz, "make_narasi_client", _factory)
    monkeypatch.setattr(lz, "_log_narasi_usage", _usage)
    monkeypatch.setattr(lz, "_narasi_revise_timeout", lambda *_a: 5.0)
    return seam


def _run(monkeypatch, *, chapter=CHAPTER, findings=None, packets=None):
    return asyncio.run(lz._narasi_structural_patch_revise(
        chapter, findings or [_FINDING], "storytelling", "id", "test-model",
        tenant_id="t", user_id="u", job_uuid=None,
        authority_text="AUTHORITY",
        outline_packets={"3": "EXACT PACKET"} if packets is None else packets,
    ))


def _user_of(call):
    return call["messages"][-1]["content"]


# ── 1-3. a broken response CONTRACT is repaired by the one retry ───────────

@pytest.mark.parametrize(
    ("first", "initial_reason"),
    [
        (_OP_NULL_PATCH, "unknown_operation"),
        (_NOT_JSON, "response_not_json"),
        (_BAD_SHAPE_PATCH, "operation_shape"),
    ],
    ids=["unknown_operation", "response_not_json", "operation_shape"],
)
def test_a_broken_contract_is_repaired_by_exactly_one_corrective_call(
        monkeypatch, first, initial_reason):
    seam = _install(monkeypatch, [first, _GOOD_REPLACE])
    revised, credits, stats = _run(monkeypatch)

    assert revised != CHAPTER, "the repair did not land"
    assert "seluruh haknya" in revised
    assert len(seam["calls"]) == 2
    assert stats["attempted"] == 1
    assert stats["provider_calls"] == 2
    assert stats["accepted"] == 1
    assert stats["schema_retry_chapters"] == 1
    assert stats["schema_retry_accepted"] == 1
    assert stats["schema_retry_exhausted"] == 0
    assert credits == 2, "both responses must be metered"
    # A repaired chapter is not a rejected chapter. The initial reason belongs to a
    # candidate, and `rejected_reason_counts` counts CHAPTERS.
    assert stats["rejected_reason_counts"] == {}
    assert initial_reason in _user_of(seam["calls"][1]), (
        "the corrective prompt must name the bounded reason it is answering")


@pytest.mark.parametrize("variant", sorted(_UNREADABLE_OP_VARIANTS))
def test_the_whole_unreadable_class_looks_identical_to_the_telemetry(variant):
    """🔴 THE FIXTURE IS REPRESENTATIVE, NOT IDENTIFIED. Every member of this class
    produces the SAME bounded observation the live artifacts recorded — subtype
    `other`, `token_length` 0, hash of the empty string — so the artifacts cannot tell
    them apart and neither may we. Pinning the class is the honest claim; naming one
    member as "the live shape" would be an identification the evidence never made."""
    import hashlib

    import narasi_addressed_patch as ap

    segmented = ap.segment_chapter(CHAPTER)
    with pytest.raises(ap.PatchValidationError) as exc:
        ap.apply_addressed_patch(segmented, _UNREADABLE_OP_VARIANTS[variant])

    assert exc.value.code == "unknown_operation"
    assert ap.bounded_operation_subtype(exc.value.received) == "other"
    token = exc.value.received if isinstance(exc.value.received, str) else ""
    assert len(token) == 0, "the live observation was token_length=0"
    assert hashlib.sha256(token.encode()).hexdigest() == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"), (
        "the artifacts recorded the SHA-256 of the empty string")


@pytest.mark.parametrize("variant", sorted(_UNREADABLE_OP_VARIANTS))
def test_the_retry_repairs_every_member_of_the_unreadable_class(monkeypatch, variant):
    """F4b does not depend on knowing WHICH member arrived — restating the contract
    answers all of them, which is exactly why no guess about the shape was needed."""
    seam = _install(monkeypatch, [_UNREADABLE_OP_VARIANTS[variant], _GOOD_REPLACE])
    revised, credits, stats = _run(monkeypatch)

    assert len(seam["calls"]) == 2
    assert stats["schema_retry_accepted"] == 1
    assert stats["accepted"] == 1
    assert credits == 2
    assert revised != CHAPTER and "seluruh haknya" in revised


# ── 4. a valid first answer must not buy a call it does not need ───────────

def test_a_valid_first_response_costs_one_call_and_no_retry(monkeypatch):
    seam = _install(monkeypatch, [_GOOD_REPLACE])
    revised, credits, stats = _run(monkeypatch)

    assert revised != CHAPTER
    assert len(seam["calls"]) == 1
    assert stats["provider_calls"] == 1
    assert stats["accepted"] == 1
    assert stats["schema_retry_chapters"] == 0
    assert stats["schema_retry_accepted"] == 0
    assert stats["schema_retry_exhausted"] == 0
    assert credits == 1


# ── 5. the bound is ONE, not "until it works" ──────────────────────────────

def test_the_retry_is_capped_at_one_even_when_a_third_answer_would_land(monkeypatch):
    """🔴 THE RUNAWAY. A retry loop that keeps going "until valid" turns one stubborn
    chapter into an unbounded bill. The stub holds a THIRD, perfectly good response —
    it must never be reached, and the chapter must survive byte-identical."""
    seam = _install(monkeypatch, [_OP_NULL_PATCH, _NOT_JSON, _GOOD_REPLACE])
    revised, credits, stats = _run(monkeypatch)

    assert len(seam["calls"]) == 2, "a third call means the bound is not a bound"
    assert revised == CHAPTER, "nothing landed, so the chapter must be untouched"
    assert stats["provider_calls"] == 2
    assert stats["accepted"] == 0
    assert stats["schema_retry_chapters"] == 1
    assert stats["schema_retry_accepted"] == 0
    assert stats["schema_retry_exhausted"] == 1
    assert credits == 2
    # The chapter's FINAL verdict is the second candidate's — that is what the chapter
    # ended up being, and it is recorded exactly once.
    assert stats["rejected_reason_counts"] == {"response_not_json": 1}


# ── 6. a retry the ledger refuses must not invent a call ───────────────────

def test_a_retry_refused_by_the_cap_creates_no_phantom_call(monkeypatch):
    """The probe's cap is enforced by the same object production counts with. When it
    refuses the retry, the refusal happens ABOVE the transport: no request, no cost,
    and the chapter keeps the INITIAL schema reason rather than being labelled with a
    provider verdict that was never returned."""
    seam = _install(monkeypatch, [_OP_NULL_PATCH, _GOOD_REPLACE])
    token = lz._NARASI_UPSTREAM_CAP.set(1)
    try:
        revised, credits, stats = _run(monkeypatch)
    finally:
        lz._NARASI_UPSTREAM_CAP.reset(token)

    assert len(seam["calls"]) == 1, "the capped retry reached the provider anyway"
    assert stats["provider_calls"] == 1
    assert credits == 1, "an unsent retry must not be metered"
    assert revised == CHAPTER
    assert stats["accepted"] == 0
    assert stats["schema_retry_chapters"] == 1
    assert stats["schema_retry_exhausted"] == 1
    assert stats["rejected_reason_counts"] == {"unknown_operation": 1}, (
        "a refused retry must not relabel the chapter as a provider failure")


# ── 7-8. the second response is metered, and the counters say so ───────────

def test_an_empty_second_response_that_carried_usage_is_still_metered(monkeypatch):
    """🔴 A RESPONSE THAT ARRIVED IS BILLABLE, USABLE OR NOT. The provider's error-as-200
    is HTTP 200 with real token usage and no choices — it has already been charged for.
    Checking `choices` BEFORE metering made that response free in our ledger while the
    provider billed it, and F4b doubled the exposure: the retry is a second chance to
    hit exactly this shape, and it did so on a call `provider_calls` was already
    counting. "Was a response received?" and "is the response usable?" are two
    different questions and must be answered in that order."""
    seam = _install(monkeypatch, [_OP_NULL_PATCH, _EmptyChoices(11, 1)])
    revised, credits, stats = _run(monkeypatch)

    assert len(seam["calls"]) == 2
    assert stats["provider_calls"] == 2
    assert seam["usage_metered"] == 2, (
        "the second response arrived and was billed upstream; metering only the first "
        "under-reports real spend on a call the ledger already counted")
    assert seam["tokens_metered"] == 2 + 12, "both responses' usage must be metered"
    assert credits == 2
    assert revised == CHAPTER
    assert stats["rejected_reason_counts"] == {"unknown_operation": 1}, (
        "no second candidate was produced, so the chapter keeps its initial verdict")
    assert stats["schema_retry_chapters"] == 1
    assert stats["schema_retry_exhausted"] == 1


def test_an_empty_FIRST_response_that_carried_usage_is_still_metered(monkeypatch):
    """The same rule on the first call, so the fix cannot be retry-specific."""
    seam = _install(monkeypatch, [_EmptyChoices(9, 3)])
    revised, credits, stats = _run(monkeypatch)

    assert len(seam["calls"]) == 1
    assert stats["provider_calls"] == 1
    assert seam["usage_metered"] == 1
    assert seam["tokens_metered"] == 12
    assert credits == 1
    assert revised == CHAPTER
    assert stats["rejected_reason_counts"] == {"response_empty": 1}
    assert stats["schema_retry_chapters"] == 0, (
        "an empty response is a transport fault, not a schema fault — no retry")


def test_the_second_response_is_metered_and_the_counters_are_exact(monkeypatch):
    seam = _install(monkeypatch, [_OP_NULL_PATCH, _GOOD_REPLACE], credits_each=7)
    revised, credits, stats = _run(monkeypatch)

    assert seam["usage_metered"] == 2, "the retry's usage was never metered"
    assert credits == 14
    assert (stats["attempted"], stats["provider_calls"], stats["accepted"]) == (1, 2, 1)
    assert revised != CHAPTER


# ── 9. the SHARED repair budget is debited with both physical calls ────────

@pytest.mark.parametrize("parallel", [0, 2])
def test_the_legacy_budget_is_debited_with_the_retrys_call_too(monkeypatch, parallel):
    """🔴 THE COST DEFECT F4a's COUNTER PREDICTED, NOW DRIVEN BY THE REAL RETRY. The
    structural and legacy lanes share ONE `2 * max_chapters` budget. Here the real
    structural lane spends TWO physical calls on ONE chapter, so legacy must find 6 of
    the 8 slots left — 7 would mean the budget was debited with `attempted` and legacy
    was handed a slot the structural lane had already spent."""
    monkeypatch.setenv("NARASI_REVISE_MAX_CHAPTERS", "4")
    monkeypatch.setenv("NARASI_REVISE_PARALLEL", str(parallel))
    book = "".join(
        f"## Bab {n}: Judul {n}\n\nKalimat salah unik{n} tetap lengkap.\n\n"
        for n in range(1, 9))
    marker = "[CHAPTER — return the corrected version, unchanged except for the fixes]\n"
    seen = {"structural": 0, "legacy": 0}

    def _create(**kwargs):
        user = kwargs["messages"][-1]["content"]
        if marker in user:                        # the LEGACY lane's own prompt shape
            seen["legacy"] += 1
            chapter = user.split(marker, 1)[1].split("\n\n[CORRECTION]\n", 1)[0]
            content = chapter                     # echo => no-op => spends both slots
        else:
            seen["structural"] += 1
            content = _OP_NULL_PATCH              # never lands: 2 calls, 0 accepted
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=content), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0))

    async def _usage(*_a, **_k):
        return 0

    monkeypatch.setattr(lz, "make_narasi_client",
                        lambda *_a, **_k: plain_client(SimpleNamespace(create=_create)))
    monkeypatch.setattr(lz, "_log_narasi_usage", _usage)

    critique = {"violations": (
        [{"severity": "high", "type": "outline_beat_order", "chapter": 5}]
        + [{"severity": "high", "type": "timeline",
            "evidence": f'"Kalimat salah unik{n} tetap lengkap."'}
           for n in range(1, 5)])}
    revised, _credits = asyncio.run(lz._narasi_consistency_revise(
        book, critique, "storytelling", "id", model="test-model",
        tenant_id="t", user_id="u", job_uuid=None,
        authority_text="AUTHORITY", outline_packets={"5": "EXACT PACKET"}))

    assert revised == book, "every legacy candidate was a no-op"
    assert seen["structural"] == 2, "the structural lane must have retried once"
    assert seen["legacy"] == 6, (
        "structural spent 2 physical calls, so 6 of the shared 8 remain; 7 means the "
        "retry's call was invisible to the shared budget")
    assert critique["structural_patch"]["provider_calls"] == 2
    assert critique["structural_patch"]["chapters_attempted"] == 1
    assert critique["structural_patch"]["schema_retry_chapters"] == 1
    assert critique["legacy_revise"]["provider_calls"] == 6


# ── 10. nothing model-authored may ride back out ───────────────────────────

def test_the_rejected_response_never_reaches_the_retry_prompt_or_anything_else(
        monkeypatch, caplog):
    """🔴 THE LEAK THE CORRECTIVE PROMPT COULD HAVE OPENED. Echoing "here is what you
    sent me" is the obvious way to write a corrective prompt, and it puts unvalidated
    model output back on the wire, into request logs, and into anything built from
    them. Only the bounded reason code — a closed, server-owned string — crosses back."""
    sentinel = "zqx_first_candidate_sentinel_4b7e"
    seam = _install(monkeypatch, [
        _payload({"op": sentinel, "unit_id": "u002", "text": f"{sentinel} prosa."}),
        _GOOD_REPLACE,
    ])
    caplog.set_level("DEBUG")
    revised, _credits, stats = _run(monkeypatch)

    assert len(seam["calls"]) == 2 and stats["accepted"] == 1
    summary = lz._narasi_structural_patch_summary(
        structural_violations=1, targeted=stats["targeted"],
        attempted=stats["attempted"], provider_calls=stats["provider_calls"],
        accepted=stats["accepted"],
        schema_retry_chapters=stats["schema_retry_chapters"],
        schema_retry_accepted=stats["schema_retry_accepted"],
        schema_retry_exhausted=stats["schema_retry_exhausted"],
        not_attempted_reason_counts=stats["not_attempted_reason_counts"],
        manuscript_changed=True)

    corrective = json.dumps(seam["calls"][1], sort_keys=True, default=str)
    for blob in (corrective, caplog.text, json.dumps(summary), repr(stats), revised):
        assert sentinel not in blob, "the rejected candidate escaped its bounded reason"
    assert "unknown_operation" in corrective, (
        "the bounded reason code is the ONLY thing that may cross back")


# ── 11. one chapter, one final verdict ─────────────────────────────────────

def test_a_failed_retry_records_exactly_one_final_rejection(monkeypatch):
    _install(monkeypatch, [_OP_NULL_PATCH, _OP_NULL_PATCH])
    _revised, _credits, stats = _run(monkeypatch)

    assert stats["rejected_reason_counts"] == {"unknown_operation": 1}, (
        "two rejected candidates are still ONE rejected chapter")
    assert sum(stats["rejected_reason_counts"].values()) == 1


def test_a_successful_retry_records_no_rejection_at_all(monkeypatch):
    _install(monkeypatch, [_OP_NULL_PATCH, _GOOD_REPLACE])
    _revised, _credits, stats = _run(monkeypatch)

    assert stats["rejected_reason_counts"] == {}, (
        "a chapter that was repaired is not a rejected chapter")


# ── the final verdict must say where it CAME FROM, not invent an ordinal ───

def test_the_final_verdict_names_its_source_and_never_fabricates_an_attempt_number(
        monkeypatch, caplog):
    """🔴 `attempt=2` WAS A LIE ON EXACTLY THE PATH THAT MATTERS. When the ledger
    refuses the retry above the transport there IS no second candidate — the code says
    so and keeps the initial reason — yet the log still printed `attempt=2`, describing
    a provider answer that was never received. The mirror case was just as wrong: a
    fault while judging the SECOND candidate printed `attempt=1`. An ordinal cannot
    express "which artifact produced this verdict", so it is replaced by one."""
    _install(monkeypatch, [_OP_NULL_PATCH, _GOOD_REPLACE])
    caplog.set_level("WARNING")
    token = lz._NARASI_UPSTREAM_CAP.set(1)
    try:
        _revised, _credits, stats = _run(monkeypatch)
    finally:
        lz._NARASI_UPSTREAM_CAP.reset(token)

    assert stats["rejected_reason_counts"] == {"unknown_operation": 1}
    assert "final_source=initial_schema" in caplog.text
    assert "attempt=2" not in caplog.text, (
        "no second candidate existed; naming one is a fabricated provider verdict")


@pytest.mark.parametrize(
    ("responses", "expected_source", "expected_reason"),
    [
        # no retry at all: the first candidate was judged unfit on its merits
        ([_payload({"op": "replace", "unit_id": "u999", "text": "Valid."})],
         "first_candidate", "unknown_id"),
        # the first call never produced a candidate
        ([RuntimeError("provider exploded")], "first_exchange", "provider_error"),
        # the retry DID answer, and its candidate was judged unfit
        ([_OP_NULL_PATCH,
          _payload({"op": "replace", "unit_id": "u999", "text": "Valid."})],
         "retry_candidate", "unknown_id"),
        # the retry answered with another broken contract — still its own verdict
        ([_OP_NULL_PATCH, _NOT_JSON], "retry_candidate", "response_not_json"),
    ],
    ids=["first_candidate", "first_exchange", "retry_candidate", "retry_candidate_schema"],
)
def test_every_rejection_path_reports_the_artifact_that_produced_it(
        monkeypatch, caplog, responses, expected_source, expected_reason):
    _install(monkeypatch, responses)
    caplog.set_level("WARNING")
    _revised, _credits, stats = _run(monkeypatch)

    assert stats["rejected_reason_counts"] == {expected_reason: 1}
    assert f"final_source={expected_source}" in caplog.text
    assert f"reason={expected_reason}" in caplog.text


def test_a_post_call_fault_reports_itself_as_the_source(monkeypatch, caplog):
    """The mirror of the cap case: this used to print `attempt=1` even when the fault
    happened while judging the SECOND candidate."""
    import narasi_addressed_patch as ap

    calls = {"n": 0}
    real_apply = ap.apply_addressed_patch

    def _apply(segmented, raw):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_apply(segmented, raw)
        raise TypeError("post-call fault on the retry's candidate")

    _install(monkeypatch, [_OP_NULL_PATCH, _GOOD_REPLACE])
    monkeypatch.setattr(ap, "apply_addressed_patch", _apply)
    caplog.set_level("WARNING")
    _revised, _credits, stats = _run(monkeypatch)

    assert stats["rejected_reason_counts"] == {"internal_error": 1}
    assert "final_source=internal_error" in caplog.text
    assert "attempt=1" not in caplog.text


def test_the_final_source_vocabulary_is_closed():
    """Bounded, server-owned, and pinned — so a new branch cannot quietly invent a
    source string that flows into a log line nobody can enumerate."""
    assert lz._STRUCTURAL_FINAL_SOURCES == frozenset({
        "first_exchange", "first_candidate", "initial_schema", "retry_candidate",
        "internal_error",
    })


# ── 12. the retry addresses the ORIGINAL table ─────────────────────────────

def test_the_corrective_prompt_carries_the_original_unit_table_not_the_candidate(
        monkeypatch):
    """🔴 THE ADDRESS-DRIFT HAZARD. If the retry were prompted with the chapter as the
    first candidate proposed it, a malformed candidate would silently redefine the
    `uNNN` addresses its own replacement is then validated against. The retry reuses
    the ORIGINAL user block verbatim: same table, same packet, same directives."""
    import narasi_addressed_patch as ap

    seam = _install(monkeypatch, [
        _payload({"op": None, "unit_id": "u002",
                  "text": "Prosa kandidat yang tidak boleh menjadi alamat."}),
        _GOOD_REPLACE,
    ])
    _revised, _credits, _stats = _run(monkeypatch)

    first_user, retry_user = _user_of(seam["calls"][0]), _user_of(seam["calls"][1])
    segmented = ap.segment_chapter(CHAPTER)
    assert segmented.annotated() in retry_user, "the original address table is gone"
    assert retry_user.startswith(first_user), (
        "the corrective prompt must EXTEND the original block, not replace it")
    assert "EXACT PACKET" in retry_user
    assert "Restore the outlined order." in retry_user
    assert "Prosa kandidat yang tidak boleh menjadi alamat." not in retry_user
    for unit in segmented.units:
        assert unit.text in retry_user


def test_the_corrective_prompt_restates_the_closed_contract(monkeypatch):
    seam = _install(monkeypatch, [_NOT_JSON, _GOOD_REPLACE])
    _run(monkeypatch)

    retry_user = _user_of(seam["calls"][1])
    for verb in ("insert_before", "insert_after", "replace", "move"):
        assert verb in retry_user
    for rule in ("no verb-as-wrapper-key", "no aliases", "no delete",
                 "no markdown fence", "no prose outside JSON",
                 "addresses must come from the original table"):
        assert rule in retry_user
    assert '"schema_version": "narasi_addressed_patch_v1"' in retry_user


def test_the_retry_is_deterministic(monkeypatch):
    """A warm retry is a second lottery ticket on the same failure distribution. The
    corrective call asks for a corrected SHAPE, so it runs at temperature 0.0."""
    seam = _install(monkeypatch, [_NOT_JSON, _GOOD_REPLACE])
    _run(monkeypatch)

    assert seam["calls"][0]["temperature"] == 0.1
    assert seam["calls"][1]["temperature"] == 0.0


# ── 13-15. the repaired chapter is byte-exact everywhere it was not touched ─

@pytest.mark.parametrize(
    ("good", "probe"),
    [
        (_GOOD_REPLACE, "seluruh haknya"),
        (_GOOD_INSERT_BEFORE, "Rekaman itu tiba lebih dulu."),
        (_GOOD_INSERT_AFTER, "Berkas itu sudah lengkap."),
        (_GOOD_MOVE, None),
    ],
    ids=["replace", "insert_before", "insert_after", "move"],
)
def test_every_operation_lands_end_to_end_through_the_retry(monkeypatch, good, probe):
    import narasi_addressed_patch as ap

    seam = _install(monkeypatch, [_OP_NULL_PATCH, good])
    revised, _credits, stats = _run(monkeypatch)

    assert stats["accepted"] == 1 and stats["schema_retry_accepted"] == 1
    assert len(seam["calls"]) == 2
    assert revised != CHAPTER
    if probe is not None:
        assert probe in revised
    else:                                       # move: same bodies, new order
        assert revised.index("Deposisinya") < revised.index("Hakim lalu")

    before, after = ap.segment_chapter(CHAPTER), ap.segment_chapter(revised)
    # The heading and the trailing frame are identical, byte for byte.
    assert revised.splitlines()[0] == CHAPTER.splitlines()[0]
    assert revised[len(revised.rstrip()):] == CHAPTER[len(CHAPTER.rstrip()):]
    assert before.heading == after.heading
    # Every ORIGINAL body still appears verbatim except the one deliberately replaced.
    for unit in before.units:
        if good is _GOOD_REPLACE and unit.unit_id == "u002":
            continue
        assert unit.text in revised, f"{unit.unit_id} was not preserved byte-exactly"


def test_the_landed_repair_changes_the_hash_and_nothing_else(monkeypatch):
    """The controlled first-invalid → second-valid proof, stated as hashes."""
    import hashlib

    import narasi_addressed_patch as ap

    seam = _install(monkeypatch, [_OP_NULL_PATCH, _GOOD_REPLACE])
    revised, credits, stats = _run(monkeypatch)

    before_sha = hashlib.sha256(CHAPTER.encode()).hexdigest()
    after_sha = hashlib.sha256(revised.encode()).hexdigest()
    assert before_sha != after_sha, "the manuscript did not actually change"
    assert (stats["attempted"], stats["provider_calls"], stats["accepted"]) == (1, 2, 1)
    assert (stats["schema_retry_chapters"], stats["schema_retry_accepted"],
            stats["schema_retry_exhausted"]) == (1, 1, 0)
    assert credits == 2
    assert len(seam["calls"]) == 2

    original_units = [u.text for u in ap.segment_chapter(CHAPTER).units]
    patched_units = [u.text for u in ap.segment_chapter(revised).units]
    assert len(original_units) == len(patched_units)
    assert original_units[0] == patched_units[0]
    assert original_units[2:] == patched_units[2:]
    assert original_units[1] != patched_units[1]


# ── 16. a SEMANTIC verdict is never re-rolled ──────────────────────────────

_LONG_WORD = "x" * 60


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        # a patch that reproduces the original exactly
        (_payload({"op": "replace", "unit_id": "u002",
                   "text": "Ia menyerahkan haknya sebelum rekaman diputar."}),
         "ineffective"),
        (_payload(*(
            {"op": "replace", "unit_id": f"u{i:03d}",
             "text": " ".join(f"panjang{i}_{w}" for w in range(40)) + "."}
            for i in range(1, 5))), "word_band"),
        # same word COUNT, far too many bytes -> byte_band, which sits after word_band
        (_payload({"op": "replace", "unit_id": "u001",
                   "text": " ".join([_LONG_WORD] * 7) + " akhir."}), "byte_band"),
        (_payload(*(
            {"op": "replace", "unit_id": f"u{i:03d}",
             "text": " ".join(f"asing{i}_{w}" for w in range(
                 len(CHAPTER.split("\n\n")[i].split()))) + "."}
            for i in range(1, 5))), "fidelity_below_minimum"),
        (_payload({"op": "replace", "unit_id": "u999", "text": "Valid."}), "unknown_id"),
        (_payload({"op": "replace", "unit_id": "u002", "text": "Pertama."},
                  {"op": "replace", "unit_id": "u002", "text": "Kedua."}),
         "operation_overlap"),
        (_payload({"op": "move", "unit_id": "u001", "before_id": "u002"},
                  {"op": "move", "unit_id": "u002", "before_id": "u001"}), "move_cycle"),
        (_payload({"op": "move", "unit_id": "u002", "after_id": "u003"},
                  {"op": "move", "unit_id": "u003", "after_id": "u004"}),
         "move_unsatisfiable"),
        (_payload({"op": "move", "unit_id": "u001", "before_id": "u001"}),
         "self_reference"),
        (_payload({"op": "insert_before", "anchor_id": "u002", "text": "Pertama."},
                  {"op": "insert_after", "anchor_id": "u002", "text": "Kedua."}),
         "ambiguous_insert_order"),
        (_payload({"op": "replace", "unit_id": "u002", "text": "   "}), "prose_empty"),
        (_payload({"op": "replace", "unit_id": "u002",
                   "text": "Here is the revision."}), "prose_wrapper"),
        (_payload({"op": "replace", "unit_id": "u002",
                   "text": "fragmen tanpa tanda akhir"}), "prose_unterminated"),
        (_payload({"op": "replace", "unit_id": "u002",
                   "text": "## Bab 9: Rusak."}), "prose_contains_heading"),
    ],
    ids=["ineffective", "word_band", "byte_band", "fidelity_below_minimum",
         "unknown_id", "operation_overlap", "move_cycle", "move_unsatisfiable",
         "self_reference", "ambiguous_insert_order", "prose_empty", "prose_wrapper",
         "prose_unterminated", "prose_contains_heading"],
)
def test_a_semantic_rejection_never_buys_a_second_call(monkeypatch, raw, reason):
    """🔴 THE BOUNDARY THAT KEEPS A GUARD A GUARD. Every case here is a patch the
    validator UNDERSTOOD and then refused, or prose that broke a content rule. A retry
    would be asking the same judge for a nicer answer — and a word band, a byte band or
    a fidelity floor you may re-roll has stopped being a floor. One call, one verdict."""
    seam = _install(monkeypatch, [raw, _GOOD_REPLACE])
    revised, credits, stats = _run(monkeypatch)

    assert len(seam["calls"]) == 1, (
        f"{reason} bought a corrective call it must never get")
    assert revised == CHAPTER
    assert credits == 1
    assert stats["provider_calls"] == 1
    assert stats["accepted"] == 0
    assert stats["schema_retry_chapters"] == 0
    assert stats["schema_retry_exhausted"] == 0
    assert stats["rejected_reason_counts"] == {reason: 1}


def test_the_retryable_set_is_exactly_the_response_contract_failures():
    """Pinned as a set, so widening it is a deliberate edit rather than a side effect."""
    assert lz._STRUCTURAL_SCHEMA_RETRYABLE == frozenset({
        "response_not_json", "schema_shape", "schema_version", "operations_type",
        "operations_empty", "operations_cap", "operation_type", "operation_shape",
        "unknown_operation",
    })
    for semantic in ("unknown_id", "self_reference", "operation_overlap",
                     "ambiguous_insert_order", "move_cycle", "move_unsatisfiable",
                     "prose_empty", "prose_wrapper", "prose_unterminated",
                     "prose_contains_heading", "ineffective", "word_band",
                     "byte_band", "fidelity_below_minimum", "heading_changed",
                     "heading_sequence", "output_unterminated", "provider_error",
                     "response_empty", "metering_error", "internal_error"):
        assert semantic not in lz._STRUCTURAL_SCHEMA_RETRYABLE, semantic


# ── 17. Phase 0: a client whose SDK retries cannot be disabled is refused ──

def test_a_client_that_cannot_disable_sdk_retries_is_refused_before_any_transport(
        monkeypatch):
    """🔴 THE FAIL-OPEN, CLOSED. This used to be `except Exception: pass` — so the one
    case the guard exists for, a client whose retries CANNOT be switched off, was also
    the one case it waved through, leaving the SDK free to make three HTTP attempts
    behind a single reservation. The attempt is now refused above the reservation: no
    transport, no phantom call, chapter preserved."""
    entered = {"n": 0}

    class _Stubborn:
        def __init__(self):
            def _create(**_kw):
                entered["n"] += 1
                raise AssertionError("no request may be dispatched on this client")
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=_create))

        def with_options(self, **_kw):
            raise TypeError("this client does not accept max_retries")

    seam = _install(monkeypatch, [_GOOD_REPLACE], client=_Stubborn())
    revised, credits, stats = _run(monkeypatch)

    assert entered["n"] == 0, "a request went out on a client with SDK retries active"
    assert seam["usage_metered"] == 0
    assert revised == CHAPTER
    assert credits == 0
    assert stats["attempted"] == 1, "the chapter WAS attempted"
    assert stats["provider_calls"] == 0, "nothing left the process"
    assert stats["accepted"] == 0
    assert stats["rejected_reason_counts"] == {"provider_error": 1}


# ── 18. the v3 summary, exactly, and on BOTH persisted delivery paths ──────

def test_the_summary_is_exactly_the_v3_shape():
    assert lz._narasi_structural_patch_summary() == {
        "schema_version": "structural_patch_summary_v3",
        "status": "not_targeted",
        "structural_violations": 0,
        "chapters_targeted": 0,
        "chapters_attempted": 0,
        "provider_calls": 0,
        "chapters_accepted": 0,
        "schema_retry_chapters": 0,
        "schema_retry_accepted": 0,
        "schema_retry_exhausted": 0,
        "not_attempted_reason_counts": {},
        "deferred_nonstructural_total": 0,
        "deferred_nonstructural_by_chapter": [],
    }


def test_classic_path_carries_the_retry_counters_to_the_persisted_payload(monkeypatch):
    """End to end on the classic route with only the PROVIDER faked: real structural
    lane, real dispatcher, real summary builder, real copy helper, real payload."""
    import narration_api as na

    _install(monkeypatch, [_OP_NULL_PATCH, _GOOD_REPLACE])
    critique = {"violations": [dict(_FINDING)]}
    revised, _credits = asyncio.run(lz._narasi_consistency_revise(
        CHAPTER, critique, "storytelling", "id", model="test-model",
        tenant_id="t", user_id="u", job_uuid=None,
        authority_text="AUTHORITY", outline_packets={"3": "EXACT PACKET"}))

    assert "seluruh haknya" in revised
    summary = critique["structural_patch"]
    assert summary["schema_version"] == "structural_patch_summary_v3"
    assert summary["provider_calls"] == 2
    assert summary["chapters_attempted"] == 1
    assert summary["chapters_accepted"] == 1
    assert summary["schema_retry_chapters"] == 1
    assert summary["schema_retry_accepted"] == 1
    assert summary["schema_retry_exhausted"] == 0

    result = {"book": revised}
    lz._narasi_copy_structural_patch_summary(result, critique)
    payload = na._result_payload(result)
    assert payload["structural_patch"]["schema_version"] == "structural_patch_summary_v3"
    assert payload["structural_patch"]["schema_retry_accepted"] == 1


def test_narration_gate_path_carries_the_retry_counters_to_the_persisted_payload(
        monkeypatch):
    """The OTHER route. `_apply_v3_gates` hands the revise a LOCAL request dict and
    copies back only what it names — the same seam where an earlier block was dropped."""
    import importlib

    import narration_api as na

    summary = lz._narasi_structural_patch_summary(
        structural_violations=1, targeted=1, attempted=1, provider_calls=2, accepted=1,
        schema_retry_chapters=1, schema_retry_accepted=1, schema_retry_exhausted=0,
        not_attempted_reason_counts={}, manuscript_changed=True)

    async def _fake_revise(full_text, critique, *_a, **_k):
        critique["structural_patch"] = dict(summary)
        return full_text, 0

    async def _no_provider(system, user, **_k):
        return "{}", None

    for flag in ("NARASI_REGISTER_GATE", "NARASI_CANON_DIFF", "NARASI_THREAD_TRACKER",
                 "NARASI_CRITIQUE_ENABLED", "NARASI_CRITIQUE_REVISE",
                 "NARASI_CANON_REGISTRY_EXTRACT"):
        monkeypatch.setenv(flag, "0")
    live = importlib.import_module("laozhang_api")
    monkeypatch.setattr(live, "_narasi_cheap_call", _no_provider)
    monkeypatch.setattr(live, "_narasi_consistency_revise", _fake_revise)
    monkeypatch.setattr(na, "_v3g_dedup_violations",
                        lambda _v: [{"severity": "high", "type": "outline_beat_order",
                                     "chapter": 3, "evidence": "apa pun"}])

    result = {"ok": True, "book": "## Bab 1\n\nIsi bab.\n",
              "chapters": [{"id": 1, "title": "Bab 1", "text": "Isi bab."}]}
    asyncio.run(na._apply_v3_gates(result, {"chapters": result["chapters"]},
                                   tenant_id=None, user_id=None, job_uuid=None))

    assert result["structural_patch"]["schema_retry_accepted"] == 1
    payload = na._result_payload(result)
    assert payload["structural_patch"]["schema_retry_chapters"] == 1
    assert payload["structural_patch"]["provider_calls"] == 2


# ── 19. the counters cannot describe a success that did not happen ─────────

def test_a_retry_accepted_over_an_unchanged_manuscript_is_refused():
    """🔴 THE FALSE SUCCESS THIS WHOLE TASK EXISTS TO PREVENT. F4b is only finished when
    repair LANDS, so a summary claiming an accepted retry over a manuscript that never
    changed must be impossible to build, not merely unlikely.

    Enforced by COMPOSITION, on purpose: `schema_retry_accepted <= chapters_accepted`
    makes a nonzero retry-accept imply a nonzero accept, which the accepted-without-
    change invariant then refuses. A third check spelling the rule out again could
    never fire — a mutation run proved it survived, shielded by its twin — so both
    halves are mutated separately (prove_f4b 12 and 12b) instead."""
    with pytest.raises(AssertionError):
        lz._narasi_structural_patch_summary(
            structural_violations=1, targeted=1, attempted=1, provider_calls=2,
            accepted=1, schema_retry_chapters=1, schema_retry_accepted=1,
            schema_retry_exhausted=0, not_attempted_reason_counts={},
            manuscript_changed=False)


@pytest.mark.parametrize(
    "kwargs",
    [
        # more retries than chapters attempted
        dict(targeted=1, attempted=1, provider_calls=2, accepted=0,
             schema_retry_chapters=2, schema_retry_accepted=0,
             schema_retry_exhausted=2),
        # accepted retries exceed the chapters that entered the path
        dict(targeted=1, attempted=1, provider_calls=2, accepted=1,
             schema_retry_chapters=0, schema_retry_accepted=1,
             schema_retry_exhausted=0),
        # the partition does not close
        dict(targeted=1, attempted=1, provider_calls=2, accepted=0,
             schema_retry_chapters=1, schema_retry_accepted=0,
             schema_retry_exhausted=0),
        # a retry-accepted chapter that is not among the accepted chapters
        dict(targeted=1, attempted=1, provider_calls=2, accepted=0,
             schema_retry_chapters=1, schema_retry_accepted=1,
             schema_retry_exhausted=0),
        # negative
        dict(targeted=1, attempted=1, provider_calls=2, accepted=0,
             schema_retry_chapters=0, schema_retry_accepted=0,
             schema_retry_exhausted=-1),
    ],
)
def test_the_retry_counters_refuse_impossible_arithmetic(kwargs):
    with pytest.raises(AssertionError):
        lz._narasi_structural_patch_summary(
            structural_violations=1, not_attempted_reason_counts={},
            manuscript_changed=True, **kwargs)


def test_the_counters_survive_a_post_call_fault_without_breaking_the_partition(
        monkeypatch):
    """A fault after the retry must still leave the chapter counted as having LEFT the
    retry path, or the summary's own partition invariant would refuse to build the
    block and the whole run's accounting would be replaced with zeros."""
    import narasi_addressed_patch as ap

    calls = {"n": 0}
    real_apply = ap.apply_addressed_patch

    def _apply(segmented, raw):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_apply(segmented, raw)     # raises unknown_operation
        raise TypeError("post-call fault on the retry's candidate")

    _install(monkeypatch, [_OP_NULL_PATCH, _GOOD_REPLACE])
    monkeypatch.setattr(ap, "apply_addressed_patch", _apply)
    revised, _credits, stats = _run(monkeypatch)

    assert revised == CHAPTER
    assert stats["provider_calls"] == 2
    assert stats["accepted"] == 0
    assert stats["schema_retry_chapters"] == 1
    assert stats["schema_retry_exhausted"] == 1
    assert stats["rejected_reason_counts"] == {"internal_error": 1}
    # The proof that it closes: the real builder accepts these numbers.
    summary = lz._narasi_structural_patch_summary(
        structural_violations=1, targeted=stats["targeted"],
        attempted=stats["attempted"], provider_calls=stats["provider_calls"],
        accepted=stats["accepted"],
        schema_retry_chapters=stats["schema_retry_chapters"],
        schema_retry_accepted=stats["schema_retry_accepted"],
        schema_retry_exhausted=stats["schema_retry_exhausted"],
        not_attempted_reason_counts=stats["not_attempted_reason_counts"],
        manuscript_changed=False)
    assert summary["status"] == "attempted_no_accept"
