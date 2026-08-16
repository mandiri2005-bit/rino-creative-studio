"""F4a — PHYSICAL provider calls counted independently of chapter attempts.

BRIEF-FOR-CODEX-2026-08-14-POST-CANARY-V9.md §F4a (`fecd3dcb…20b5`).

F4a is OBSERVABILITY, not an actuator. Nothing here makes structural repair land; it
makes the cost of trying to land it countable.

🔴 WHY `attempted` COULD NOT KEEP DOING THIS JOB. `chapters_attempted` counts CHAPTERS
   the lane decided to work on. A physical provider call is a different quantity and
   the two only coincide while every chapter costs exactly one call. The moment a
   bounded retry exists (F4b's territory), one attempted chapter can cost two calls —
   and every consumer of the old number silently undercounts: the structural summary
   under-reports spend, and, worse, the legacy lane's shared budget is debited one slot
   for two calls actually made, handing legacy headroom that was already spent.

   The two numbers also diverge in the other direction, which is why the counter cannot
   simply be derived: a chapter refused for a missing outline packet, an unresolved
   locator or a segmentation failure is never attempted AND never billed, while a
   client factory that raises before `.create` is entered is an attempt that costs
   nothing. Only an increment placed on the last line before control enters `.create`
   tells the truth in all four directions.
"""
import asyncio
import json
import time

import pytest
from types import SimpleNamespace

import laozhang_api as lz
import narasi_addressed_patch as ap

from test_narasi_addressed_patch import CHAPTER, _payload, plain_client  # noqa: E402


_ACCEPTED_PATCH = _payload({
    "op": "replace",
    "unit_id": "u002",
    "text": "Ia menyerahkan seluruh haknya sebelum rekaman diputar.",
})
#: `delete_paragraph` is outside the closed four-verb vocabulary -> unknown_operation.
_UNKNOWN_OP_PATCH = _payload({"op": "delete_paragraph", "unit_id": "u001"})

_FINDING = {
    "severity": "high", "type": "outline_beat_order", "chapter": 3,
    "evidence": "Surrender must precede the ruling.",
    "fix": "Restore the outlined order.",
}


def _install(monkeypatch, *, create, client_factory=None):
    """Wire the provider seam. `create` is the `.create` callable itself, so a test can
    make the CALL raise (a physical call that happened) as distinct from making the
    CLIENT raise (no physical call at all)."""
    calls = {"create_entered": 0, "factory_entered": 0}

    def _create(**kwargs):
        calls["create_entered"] += 1
        return create(**kwargs)

    def _factory(*_a, **_k):
        calls["factory_entered"] += 1
        if client_factory is not None:
            return client_factory()
        return plain_client(SimpleNamespace(create=_create))

    async def _usage(*_a, **_k):
        return 1

    monkeypatch.setattr(lz, "make_narasi_client", _factory)
    monkeypatch.setattr(lz, "_log_narasi_usage", _usage)
    monkeypatch.setattr(lz, "_narasi_revise_timeout", lambda *_a: 2.0)
    return calls


def _ok_response(content):
    def _create(**_kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=content), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))
    return _create


def _run(monkeypatch, *, chapter=CHAPTER, findings=None, packets=None):
    return asyncio.run(lz._narasi_structural_patch_revise(
        chapter, findings or [_FINDING], "storytelling", "id", "test-model",
        tenant_id="t", user_id="u", job_uuid=None,
        authority_text="AUTHORITY",
        outline_packets={"3": "EXACT PACKET"} if packets is None else packets,
    ))


# ── the four quadrants of attempted-vs-billed ──────────────────────────────

def test_unknown_operation_is_one_attempt_and_two_physical_calls(monkeypatch):
    """The canary v9 shape: the call was made and paid for, the patch was refused.

    🔴 THIS IS THE DIVERGENCE F4a's COUNTER WAS BUILT FOR, now real rather than
    hypothetical. `unknown_operation` is a broken response contract, so F4b spends one
    corrective call on it: ONE attempted chapter, TWO physical calls. Every consumer
    that derived calls from attempts would report 1 here and under-bill by half — and
    would hand the legacy lane a slot the structural lane had already spent."""
    seam = _install(monkeypatch, create=_ok_response(_UNKNOWN_OP_PATCH))
    revised, _credits, stats = _run(monkeypatch)

    assert revised == CHAPTER, "a refused patch must leave the chapter untouched"
    assert stats["targeted"] == 1
    assert stats["attempted"] == 1
    assert stats["provider_calls"] == 2
    assert stats["accepted"] == 0
    assert stats["rejected_reason_counts"] == {"unknown_operation": 1}
    assert seam["create_entered"] == 2


def test_an_accepted_patch_is_one_attempt_and_one_physical_call(monkeypatch):
    seam = _install(monkeypatch, create=_ok_response(_ACCEPTED_PATCH))
    revised, _credits, stats = _run(monkeypatch)

    assert "seluruh haknya" in revised and revised != CHAPTER
    assert stats["attempted"] == 1
    assert stats["provider_calls"] == 1
    assert stats["accepted"] == 1
    assert seam["create_entered"] == 1


@pytest.mark.parametrize("packets", [{}, {"9": "WRONG CHAPTER"}])
def test_a_missing_outline_packet_costs_no_physical_call(monkeypatch, packets):
    seam = _install(monkeypatch, create=_ok_response(_ACCEPTED_PATCH))
    revised, credits, stats = _run(monkeypatch, packets=packets)

    assert revised == CHAPTER and credits == 0
    assert stats["targeted"] == 1
    assert stats["attempted"] == 0
    assert stats["provider_calls"] == 0
    assert stats["not_attempted_reason_counts"] == {"outline_packet_missing": 1}
    assert seam["create_entered"] == 0


def test_a_segmentation_failure_costs_no_physical_call(monkeypatch):
    seam = _install(monkeypatch, create=_ok_response(_ACCEPTED_PATCH))

    def _boom(_text):
        raise ap.PatchValidationError("segmentation_failure")

    monkeypatch.setattr(ap, "segment_chapter", _boom)
    revised, credits, stats = _run(monkeypatch)

    assert revised == CHAPTER and credits == 0
    assert stats["attempted"] == 0
    assert stats["provider_calls"] == 0
    assert stats["not_attempted_reason_counts"] == {"segmentation_failure": 1}
    assert seam["create_entered"] == 0


def test_an_unresolved_locator_costs_no_physical_call(monkeypatch):
    seam = _install(monkeypatch, create=_ok_response(_ACCEPTED_PATCH))
    finding = {"severity": "high", "type": "outline_missing_beat",
               "evidence": "No manuscript quote or chapter locator.", "fix": "Restore it."}
    revised, credits, stats = _run(monkeypatch, findings=[finding])

    assert revised == CHAPTER and credits == 0
    assert stats["targeted"] == 0
    assert stats["attempted"] == 0
    assert stats["provider_calls"] == 0
    assert stats["unresolved_locator_count"] == 1
    assert seam["create_entered"] == 0


# ── the two edges that make the counter non-derivable ───────────────────────

def test_a_call_that_raises_after_being_entered_is_still_billed(monkeypatch):
    """🔴 THE COST DIRECTION THAT MATTERS. Control entered `.create` — the request went
    out and the provider may well have charged for it. A rejection that silently
    un-counts the call would under-report real spend."""
    def _raises(**_kwargs):
        raise RuntimeError("provider exploded mid-call")

    seam = _install(monkeypatch, create=_raises)
    revised, _credits, stats = _run(monkeypatch)

    assert revised == CHAPTER
    assert stats["attempted"] == 1
    assert stats["provider_calls"] == 1, "a call that was entered must stay counted"
    assert stats["accepted"] == 0
    assert stats["rejected_reason_counts"] == {"provider_error": 1}
    assert seam["create_entered"] == 1


def test_a_client_that_fails_before_create_is_not_billed(monkeypatch):
    """The mirror case. The factory raised, `.create` was never entered, nothing left
    the process — an attempt with no physical call. Counting it would inflate spend and
    would wrongly debit the legacy lane's shared budget."""
    def _bad_factory():
        raise RuntimeError("no credential for this model")

    seam = _install(monkeypatch, create=_ok_response(_ACCEPTED_PATCH),
                    client_factory=_bad_factory)
    revised, _credits, stats = _run(monkeypatch)

    assert revised == CHAPTER
    assert stats["attempted"] == 1
    assert stats["provider_calls"] == 0, (
        "the client factory raised before `.create` was entered — no request was made")
    assert stats["accepted"] == 0
    assert seam["create_entered"] == 0
    assert seam["factory_entered"] == 1


# ── the summary must be able to REPRESENT a retry ──────────────────────────

def test_the_summary_can_represent_two_calls_for_one_attempted_chapter():
    """Nothing in the lane emits this yet — F4b owns the retry. The point is that the
    schema does not FLATTEN it: a summary that cannot express 2 calls on 1 attempt
    would force the future retry to lie the moment it is built."""
    summary = lz._narasi_structural_patch_summary(
        structural_violations=1, targeted=1, attempted=1, provider_calls=2, accepted=0,
        not_attempted_reason_counts={}, manuscript_changed=False)

    assert summary["schema_version"] == "structural_patch_summary_v3"
    assert summary["chapters_attempted"] == 1
    assert summary["provider_calls"] == 2
    assert summary["chapters_accepted"] == 0
    assert summary["status"] == "attempted_no_accept"


def test_the_summary_refuses_calls_without_attempts():
    """`attempted == 0` and a nonzero call count cannot both be true: a chapter that was
    never attempted never reached the provider."""
    with pytest.raises(AssertionError):
        lz._narasi_structural_patch_summary(
            structural_violations=1, targeted=1, attempted=0, provider_calls=1,
            accepted=0, not_attempted_reason_counts={"attempt_cap": 1})


def test_the_summary_refuses_more_accepts_than_calls():
    with pytest.raises(AssertionError):
        lz._narasi_structural_patch_summary(
            structural_violations=1, targeted=1, attempted=1, provider_calls=0,
            accepted=1, not_attempted_reason_counts={}, manuscript_changed=True)


def test_the_summary_is_exactly_the_v3_shape():
    summary = lz._narasi_structural_patch_summary()
    assert summary == {
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


# ── the two ways a physical call was still being LOST ──────────────────────

def test_an_attempt_abandoned_before_dispatch_is_forbidden_to_dispatch(monkeypatch):
    """🔴 THE TIMEOUT RACE, CLOSED RATHER THAN NARROWED. `wait_for` stops WAITING; it
    cannot stop the thread. The first fix let the worker signal that it had entered
    `.create` and waited a bounded grace for that signal — a factory slower than the
    grace still dispatched afterwards and the billed call was recorded as zero
    (reproduced: grace 20ms, factory 80ms -> entered 1, counted 0). Shrinking a window
    is not closing it.

    The slot decides atomically instead: whoever takes the lock first wins. Here the
    timeout wins, so the worker is FORBIDDEN to dispatch — no request, no cost, and
    the zero in the ledger is true rather than lucky."""
    entered = {"n": 0}

    def _slow_factory(*_a, **_k):
        time.sleep(0.25)                      # still resolving when the timeout fires

        def _create(**_kw):
            entered["n"] += 1
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content=_ACCEPTED_PATCH),
                    finish_reason="stop")],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))
        return plain_client(SimpleNamespace(create=_create))

    async def _usage(*_a, **_k):
        return 0

    monkeypatch.setattr(lz, "make_narasi_client", _slow_factory)
    monkeypatch.setattr(lz, "_log_narasi_usage", _usage)
    monkeypatch.setattr(lz, "_narasi_revise_timeout", lambda *_a: 0.02)

    _revised, _credits, stats = _run(monkeypatch)
    time.sleep(0.5)                           # let the abandoned worker finish

    assert entered["n"] == 0, (
        "the worker dispatched after the coroutine had already written it off — the "
        "ledger and the provider now disagree")
    assert stats["attempted"] == 1
    assert stats["provider_calls"] == 0


def test_a_call_already_dispatched_when_the_timeout_fires_is_still_counted(monkeypatch):
    """The other side of the same lock, and the one that costs money: the worker won
    the race, the request is on the wire, and the coroutine gives up afterwards. The
    provider will bill it, so the ledger must carry it."""
    entered = {"n": 0}

    def _factory(*_a, **_k):
        def _create(**_kw):
            entered["n"] += 1                 # dispatched, THEN slow
            time.sleep(0.25)
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content=_ACCEPTED_PATCH),
                    finish_reason="stop")],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))
        return plain_client(SimpleNamespace(create=_create))

    async def _usage(*_a, **_k):
        return 0

    monkeypatch.setattr(lz, "make_narasi_client", _factory)
    monkeypatch.setattr(lz, "_log_narasi_usage", _usage)
    monkeypatch.setattr(lz, "_narasi_revise_timeout", lambda *_a: 0.05)

    _revised, _credits, stats = _run(monkeypatch)

    assert entered["n"] == 1, "the fixture must dispatch before the timeout"
    assert stats["attempted"] == 1
    assert stats["provider_calls"] == 1


# ── the counter must sit at the UPSTREAM boundary, not the adapter's ───────

def _failover_chain(*rungs):
    """(name, proto, endpoint, key, model_id) — the shape `_narasi_failover_chain`
    returns."""
    return [(name, "anthropic", f"https://{name}.invalid/v1/messages", "k", "m1")
            for name in rungs]


def test_a_failover_retry_counts_every_upstream_request(monkeypatch):
    """🔴 THE BOUNDARY DEFECT. One outer `.create()` is NOT one physical call:
    `_NarasiFailoverClient` walks a cheapest-first chain and retries each rung, and
    every one of those is a billable upstream request. Counting the adapter invocation
    reported 1 where two requests had gone out — and that number debits the shared
    repair budget, so failover silently bought legacy headroom it had already spent."""
    upstream = {"n": 0}

    def _transport(_endpoint, _key, _model_id, _messages, _max_tokens, _timeout,
                   temperature=None, _reserve=None):
        # Mirrors the real helper: reserve at the transport line, then send.
        if _reserve is not None and not _reserve():
            raise lz._NarasiUpstreamRefused("upstream refused")
        upstream["n"] += 1
        if upstream["n"] == 1:
            raise RuntimeError("first rung is down")
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=_ACCEPTED_PATCH), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    monkeypatch.setattr(lz, "_anthropic_messages_create", _transport)
    monkeypatch.setattr(lz, "_narasi_failover_chain",
                        lambda *_a, **_k: _failover_chain("rung_a", "rung_b"))
    monkeypatch.setattr(lz, "make_narasi_client",
                        lambda *_a, **_k: lz._NarasiFailoverClient(
                            model="m1", role="", phase="canon_diff_revise"))

    async def _usage(*_a, **_k):
        return 0

    monkeypatch.setattr(lz, "_log_narasi_usage", _usage)
    monkeypatch.setattr(lz, "_narasi_revise_timeout", lambda *_a: 20.0)

    revised, _credits, stats = _run(monkeypatch)

    assert upstream["n"] == 2, "the fixture must actually exercise a second rung"
    assert stats["attempted"] == 1, "still ONE chapter attempt"
    assert stats["provider_calls"] == 2, (
        "two upstream requests were issued and billed; reporting 1 measures adapter "
        "invocations, not physical provider calls")
    assert stats["accepted"] == 1 and revised != CHAPTER


def test_every_rung_failing_still_publishes_the_calls_it_made(monkeypatch):
    """All rungs down: nothing lands, but the requests were still made and billed."""
    upstream = {"n": 0}

    def _transport(*_a, _reserve=None, **_k):
        if _reserve is not None and not _reserve():
            raise lz._NarasiUpstreamRefused("upstream refused")
        upstream["n"] += 1
        raise RuntimeError("rung down")

    monkeypatch.setattr(lz, "_anthropic_messages_create", _transport)
    monkeypatch.setattr(lz, "_narasi_failover_chain",
                        lambda *_a, **_k: _failover_chain("rung_a", "rung_b"))
    monkeypatch.setattr(lz, "make_narasi_client",
                        lambda *_a, **_k: lz._NarasiFailoverClient(
                            model="m1", role="", phase="canon_diff_revise"))

    async def _usage(*_a, **_k):
        return 0

    monkeypatch.setattr(lz, "_log_narasi_usage", _usage)
    monkeypatch.setattr(lz, "_narasi_revise_timeout", lambda *_a: 20.0)

    revised, _credits, stats = _run(monkeypatch)

    assert upstream["n"] >= 2
    assert revised == CHAPTER
    assert stats["accepted"] == 0
    assert stats["provider_calls"] == upstream["n"], (
        "a run that landed nothing still spent every request it made")


def test_a_failover_that_dies_while_being_built_costs_nothing(monkeypatch):
    """🔴 THE INVENTED CALL. `provider_calls` used to be `max(sink, outer_invocations)`,
    treating an adapter invocation as a floor. But an invocation is not evidence of a
    request: a chain that fails while being CONSTRUCTED touches no transport at all,
    and the floor then reported one physical call against zero real ones — a number
    that also debits the shared repair budget."""
    upstream = {"n": 0}

    def _transport(*_a, _reserve=None, **_k):
        upstream["n"] += 1
        raise AssertionError("no transport may be reached in this test")

    def _explode(*_a, **_k):
        raise RuntimeError("chain could not be built")

    monkeypatch.setattr(lz, "_anthropic_messages_create", _transport)
    monkeypatch.setattr(lz, "_narasi_failover_chain", _explode)
    monkeypatch.setattr(lz, "make_narasi_client",
                        lambda *_a, **_k: lz._NarasiFailoverClient(
                            model="m1", role="", phase="canon_diff_revise"))

    async def _usage(*_a, **_k):
        return 0

    monkeypatch.setattr(lz, "_log_narasi_usage", _usage)
    monkeypatch.setattr(lz, "_narasi_revise_timeout", lambda *_a: 20.0)

    revised, _credits, stats = _run(monkeypatch)

    assert upstream["n"] == 0, "the fixture must not reach a transport"
    assert revised == CHAPTER
    assert stats["attempted"] == 1, "the chapter WAS attempted"
    assert stats["provider_calls"] == 0, (
        "no request left the process; reporting one invents a billable call")


def test_a_local_client_failure_inside_a_rung_is_not_counted(monkeypatch):
    """The same rule one level down: the increment sits AFTER every local step. A rung
    whose client constructor raises has sent nothing."""
    monkeypatch.setattr(lz, "_narasi_failover_chain", lambda *_a, **_k: [
        ("plain", "openai", "https://plain.invalid/v1", "k", "m1")])

    def _bad_openai(*_a, **_k):
        raise RuntimeError("client could not be constructed")

    monkeypatch.setattr(lz, "OpenAI", _bad_openai)
    monkeypatch.setattr(lz, "make_narasi_client",
                        lambda *_a, **_k: lz._NarasiFailoverClient(
                            model="m1", role="", phase="canon_diff_revise"))

    async def _usage(*_a, **_k):
        return 0

    monkeypatch.setattr(lz, "_log_narasi_usage", _usage)
    monkeypatch.setattr(lz, "_narasi_revise_timeout", lambda *_a: 20.0)

    _revised, _credits, stats = _run(monkeypatch)

    assert stats["attempted"] == 1
    assert stats["provider_calls"] == 0


def test_a_failover_retry_is_forbidden_once_the_caller_has_given_up(monkeypatch):
    """🔴 THE LATE RETRY. The atomic slot stops a worker that has not started; it says
    nothing about a failover walk already underway. After the caller's `wait_for`
    expires and its stats are frozen, the rung loop would fire its NEXT retry anyway —
    billed, and counted by nobody. The loop now checks the abandonment flag before
    every rung attempt: the request in flight finishes and is counted, the next one is
    refused."""
    upstream = {"n": 0}

    def _transport(*_a, _reserve=None, **_k):
        if _reserve is not None and not _reserve():
            raise lz._NarasiUpstreamRefused("upstream refused")
        upstream["n"] += 1
        time.sleep(0.25)                      # outlives the caller's timeout
        raise RuntimeError("rung down")

    monkeypatch.setattr(lz, "_anthropic_messages_create", _transport)
    monkeypatch.setattr(lz, "_narasi_failover_chain",
                        lambda *_a, **_k: _failover_chain("rung_a", "rung_b"))
    monkeypatch.setattr(lz, "make_narasi_client",
                        lambda *_a, **_k: lz._NarasiFailoverClient(
                            model="m1", role="", phase="canon_diff_revise"))

    async def _usage(*_a, **_k):
        return 0

    monkeypatch.setattr(lz, "_log_narasi_usage", _usage)
    monkeypatch.setattr(lz, "_narasi_revise_timeout", lambda *_a: 0.05)

    _revised, _credits, stats = _run(monkeypatch)
    time.sleep(0.8)                           # let the abandoned walk run its course

    assert upstream["n"] == 1, (
        "a second upstream request was issued after the caller had already frozen its "
        "stats — billed and unaccounted")
    assert stats["attempted"] == 1


def test_the_legacy_lane_is_never_billed_to_the_structural_sink(monkeypatch):
    """The sink is scoped to ONE structural run. The dispatcher runs the legacy lane
    immediately afterwards through the same failover client, so a leaked sink would
    charge legacy's upstream requests to the structural summary."""
    ledger_before = lz._NARASI_UPSTREAM_LEDGER.get()
    _install(monkeypatch, create=_ok_response(_ACCEPTED_PATCH))
    _run(monkeypatch)
    assert lz._NARASI_UPSTREAM_LEDGER.get() is ledger_before, (
        "the upstream ledger outlived the structural run")


def test_a_fault_after_the_call_keeps_the_accounting(monkeypatch):
    """🔴 THE ZERO-FALLBACK. An unexpected exception AFTER a successful provider call
    used to propagate out of the structural lane; the dispatcher then replaced the whole
    stats block with zeros. The published summary said `not_targeted` — no targeting, no
    attempt, no call — while a real, billed call had already gone out, and the legacy
    lane was handed the full shared budget on top."""
    monkeypatch.setattr(ap, "apply_addressed_patch",
                        lambda *_a, **_k: (_ for _ in ()).throw(TypeError("post-call fault")))
    seam = _install(monkeypatch, create=_ok_response(_ACCEPTED_PATCH))
    critique = {"violations": [dict(_FINDING)]}
    revised, _credits = asyncio.run(lz._narasi_consistency_revise(
        CHAPTER, critique, "storytelling", "id", model="test-model",
        tenant_id="t", user_id="u", job_uuid=None,
        authority_text="AUTHORITY", outline_packets={"3": "EXACT PACKET"}))

    assert seam["create_entered"] == 1
    assert revised == CHAPTER, "a faulted repair must leave the manuscript alone"
    summary = critique["structural_patch"]
    assert summary["provider_calls"] == 1, "the billed call vanished from the summary"
    assert summary["chapters_attempted"] == 1
    assert summary["chapters_targeted"] == 1
    assert summary["status"] != "not_targeted", (
        "the chapter WAS targeted and WAS attempted — reporting not_targeted is false")


# ── the field must survive to BOTH persisted delivery paths ────────────────

def test_classic_path_carries_provider_calls_to_the_persisted_payload(monkeypatch):
    """End to end on the classic route with only the PROVIDER faked: real
    `_narasi_structural_patch_revise`, real dispatcher, real summary builder, real copy
    helper, real `_result_payload`."""
    import narration_api as na

    _install(monkeypatch, create=_ok_response(_ACCEPTED_PATCH))
    critique = {"violations": [dict(_FINDING)]}
    revised, _credits = asyncio.run(lz._narasi_consistency_revise(
        CHAPTER, critique, "storytelling", "id", model="test-model",
        tenant_id="t", user_id="u", job_uuid=None,
        authority_text="AUTHORITY", outline_packets={"3": "EXACT PACKET"}))

    assert "seluruh haknya" in revised
    summary = critique["structural_patch"]
    assert summary["schema_version"] == "structural_patch_summary_v3"
    assert summary["provider_calls"] == 1
    assert summary["chapters_accepted"] == 1

    result = {"book": revised}
    lz._narasi_copy_structural_patch_summary(result, critique)
    payload = na._result_payload(result)
    assert payload["structural_patch"]["schema_version"] == "structural_patch_summary_v3"
    assert payload["structural_patch"]["provider_calls"] == 1


def test_narration_gate_path_carries_provider_calls_to_the_persisted_payload(monkeypatch):
    """The OTHER route. `_apply_v3_gates` hands the revise a LOCAL request dict and
    copies back only what it names — the same seam where F2's block was being dropped.
    Driven through the real gate function and the real `_result_payload`."""
    import narration_api as na

    summary = lz._narasi_structural_patch_summary(
        structural_violations=1, targeted=1, attempted=1, provider_calls=2,
        accepted=0, not_attempted_reason_counts={}, manuscript_changed=False)

    async def _fake_revise(full_text, critique, *_a, **_k):
        critique["structural_patch"] = dict(summary)
        return full_text, 0

    async def _no_provider(system, user, **_k):
        return "{}", None

    for flag in ("NARASI_REGISTER_GATE", "NARASI_CANON_DIFF", "NARASI_THREAD_TRACKER",
                 "NARASI_CRITIQUE_ENABLED", "NARASI_CRITIQUE_REVISE",
                 "NARASI_F6_ENABLED",
                 "NARASI_CANON_REGISTRY_EXTRACT"):
        monkeypatch.setenv(flag, "0")
    # The module-level alias can be stale after test_middleware re-imports
    # laozhang_api; patch what narration_api's lazy import will actually resolve.
    import importlib
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

    assert result["structural_patch"]["schema_version"] == "structural_patch_summary_v3"
    assert result["structural_patch"]["provider_calls"] == 2
    payload = na._result_payload(result)
    assert payload["structural_patch"]["provider_calls"] == 2


# ── the shared repair budget must be debited with physical calls ───────────

@pytest.mark.parametrize("parallel", [0, 2])
def test_the_legacy_budget_is_debited_with_physical_calls_not_attempts(monkeypatch, parallel):
    """🔴 THE DEFECT F4a's COUNTER EXISTS TO PREVENT. The structural lane and the legacy
    lane share ONE `2 * max_chapters` budget. Debiting it with `attempted` under-reports
    the moment a bounded structural retry exists: the stub below spends TWO physical
    calls on ONE attempted chapter, so legacy must find 6 of the 8 slots left, not 7.

    Observable difference — 6 calls over 3 chapters (physical debit, correct) versus
    7 calls over 4 chapters (attempt debit, the bug)."""
    monkeypatch.setenv("NARASI_REVISE_MAX_CHAPTERS", "4")
    monkeypatch.setenv("NARASI_REVISE_PARALLEL", str(parallel))
    book = "".join(
        f"## Bab {n}: Judul {n}\n\nKalimat salah unik{n} tetap lengkap.\n\n"
        for n in range(1, 9))

    async def _structural(*_a, **_k):
        return book, 0, {
            "targeted": 1, "attempted": 1, "provider_calls": 2, "accepted": 0,
            "schema_retry_chapters": 1, "schema_retry_accepted": 0,
            "schema_retry_exhausted": 1,
            "not_attempted_reason_counts": {},
            "rejected_reason_counts": {"response_not_json": 1},
            "unresolved_locator_count": 0,
            "owned_chapter_numbers": {5},
        }

    legacy_calls = []

    class _LegacyCompletions:
        def create(self, **kwargs):
            legacy_calls.append(kwargs)
            marker = "[CHAPTER — return the corrected version, unchanged except for the fixes]\n"
            chapter = kwargs["messages"][-1]["content"].split(marker, 1)[1]
            return SimpleNamespace(   # echo => no-op => both attempts spent per chapter
                choices=[SimpleNamespace(
                    message=SimpleNamespace(
                        content=chapter.split("\n\n[CORRECTION]\n", 1)[0]),
                    finish_reason="stop")],
                usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0))

    async def _usage(*_a, **_k):
        return 0

    monkeypatch.setattr(lz, "_narasi_structural_patch_revise", _structural)
    monkeypatch.setattr(lz, "make_narasi_client", lambda *_a, **_k: SimpleNamespace(
        chat=SimpleNamespace(completions=_LegacyCompletions())))
    monkeypatch.setattr(lz, "_log_narasi_usage", _usage)

    critique = {"violations": (
        [{"severity": "high", "type": "outline_beat_order", "chapter": 5}]
        + [{"severity": "high", "type": "timeline",
            "evidence": f'"Kalimat salah unik{n} tetap lengkap."'}
           for n in range(1, 5)])}
    revised, _credits = asyncio.run(lz._narasi_consistency_revise(
        book, critique, "storytelling", "id", model="test-model",
        tenant_id="t", user_id="u", job_uuid=None,
        authority_text="AUTHORITY", outline_packets={}))

    assert revised == book, "every legacy candidate was a no-op"
    assert len(legacy_calls) == 6, (
        "structural spent 2 physical calls, so 6 of the shared 8 remain; 7 means the "
        "budget was debited with `attempted` and legacy was handed a spent slot")
    assert critique["legacy_revise"]["provider_calls"] == 6
    assert critique["legacy_revise"]["chapters_attempted"] == 3
    assert critique["structural_patch"]["provider_calls"] == 2
    assert critique["structural_patch"]["chapters_attempted"] == 1


# ── the raw model token must never escape ──────────────────────────────────

def test_the_raw_operation_token_never_reaches_logs_or_the_summary(monkeypatch, caplog):
    """A unique sentinel, so a leak cannot hide behind a plausible-looking word."""
    sentinel = "zqx_raw_verb_sentinel_8f3a"
    _install(monkeypatch, create=_ok_response(
        _payload({"op": sentinel, "unit_id": "u001"})))
    caplog.set_level("DEBUG")
    revised, _credits, stats = _run(monkeypatch)

    summary = lz._narasi_structural_patch_summary(
        structural_violations=1, targeted=stats["targeted"],
        attempted=stats["attempted"], provider_calls=stats["provider_calls"],
        accepted=stats["accepted"],
        schema_retry_chapters=stats["schema_retry_chapters"],
        schema_retry_accepted=stats["schema_retry_accepted"],
        schema_retry_exhausted=stats["schema_retry_exhausted"],
        not_attempted_reason_counts=stats["not_attempted_reason_counts"],
        manuscript_changed=False)

    assert stats["rejected_reason_counts"] == {"unknown_operation": 1}
    # Two calls: F4b answered the broken contract once. The sentinel must not have
    # ridden along into the corrective prompt either — pinned in the F4b suite.
    assert stats["provider_calls"] == 2
    for blob in (caplog.text, json.dumps(summary), repr(stats), revised):
        assert sentinel not in blob, "the raw model verb escaped its bounded subtype"
    assert "subtype=other" in caplog.text


def test_the_plain_structural_client_disables_sdk_retries(monkeypatch):
    """🔴 ONE RESERVATION MUST MEAN ONE REQUEST. `make_client()` builds an OpenAI client
    without `max_retries=0`, and the SDK default is 2 — so a single reserved,
    single-counted plain-client call could quietly become three HTTP attempts, and the
    probe's "at most one billable request" would simply be untrue."""
    seen = {"opts": []}

    class _Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(
                create=_ok_response(_ACCEPTED_PATCH)))

        def with_options(self, **kw):
            seen["opts"].append(kw)
            return self

    async def _usage(*_a, **_k):
        return 0

    monkeypatch.setattr(lz, "make_narasi_client", lambda *_a, **_k: _Client())
    monkeypatch.setattr(lz, "_log_narasi_usage", _usage)
    monkeypatch.setattr(lz, "_narasi_revise_timeout", lambda *_a: 2.0)

    _revised, _credits, stats = _run(monkeypatch)

    assert seen["opts"] == [{"max_retries": 0}], (
        "the plain structural call must disable the SDK's own silent retries")
    assert stats["provider_calls"] == 1


# ── the reservation INSIDE each helper, exercised for real ──────────────────
#
# 🔴 EVERY failover test above stubs the helper, so none of them executes a single line
#    of the helper's own body — the reservation that now lives at each helper's true
#    transport line was invisible to all of them (two mutants survived and said so).
#    These call the real helpers directly with a refusing reserver: no network is
#    reachable because the refusal is raised BEFORE any client or socket is touched,
#    which is precisely the property under test.

def test_the_anthropic_helper_refuses_at_its_own_transport_line(monkeypatch):
    """The connection class is stubbed so REMOVING the guard cannot reach the network:
    a mutant that deletes it must fail this test as a plain assertion failure, not as
    the suite's outbound-network guard firing (which the mutation harness rightly
    refuses to score as a kill)."""
    refused = {"asked": 0}
    opened = {"n": 0}

    class _Conn:
        def __init__(self, *_a, **_k):
            opened["n"] += 1

        def request(self, *_a, **_k):
            raise AssertionError("transport must not be reached after a refusal")

        def close(self):
            pass

    import http.client as _httpc
    monkeypatch.setattr(_httpc, "HTTPSConnection", _Conn)

    def _reserve():
        refused["asked"] += 1
        return False

    with pytest.raises(lz._NarasiUpstreamRefused):
        lz._anthropic_messages_create(
            "https://unreachable.invalid/v1/messages", "k", "m1",
            [{"role": "user", "content": "x"}], 16, 1.0, _reserve=_reserve)
    assert refused["asked"] == 1


def test_the_vertex_helper_refuses_after_its_own_client_preflight(monkeypatch):
    """Vertex builds its client and config BEFORE dispatching on a raw thread. The
    reservation has to sit inside that thread, at `generate_content` — this is the
    branch the audit reproduced as `_genai_client() → None` reporting two phantom
    calls, so the guard must be exercised for real, not through a stubbed helper."""
    sent = {"n": 0}
    refused = {"asked": 0}

    class _Models:
        def generate_content(self, **_kw):
            sent["n"] += 1
            raise AssertionError("transport must not be reached after a refusal")

    monkeypatch.setattr(lz, "_genai_client",
                        lambda *_a, **_k: SimpleNamespace(models=_Models()))

    def _reserve():
        refused["asked"] += 1
        return False

    with pytest.raises(Exception):
        lz._vertex_gemini_create("m1", [{"role": "user", "content": "x"}], 16, 1.0,
                                 _reserve=_reserve)
    assert refused["asked"] == 1, "the vertex transport line is unguarded"
    assert sent["n"] == 0


def test_the_fal_helper_refuses_at_submit_not_at_polling(monkeypatch):
    """FAL's billable event is the SUBMIT; the later status polls are not requests."""
    posted = {"n": 0}
    refused = {"asked": 0}
    monkeypatch.setenv("FAL_API_KEY", "k")

    class _Requests:
        @staticmethod
        def post(*_a, **_k):
            posted["n"] += 1
            raise AssertionError("submit must not happen after a refusal")

    monkeypatch.setattr(lz, "_requests", _Requests, raising=False)

    def _reserve():
        refused["asked"] += 1
        return False

    with pytest.raises(Exception):
        lz._fal_llm_create("m1", [{"role": "user", "content": "x"}], 16, 1.0,
                           _reserve=_reserve)
    assert refused["asked"] == 1, "the fal submit line is unguarded"
    assert posted["n"] == 0


