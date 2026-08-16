"""F2 — legacy revise must not count a byte-identical rewrite as `revised`.

BRIEF-FOR-CODEX-2026-08-14-POST-CANARY-V9.md §F2. `_narasi_revise_chunked`'s accept
gate (serial ~laozhang_api.py:9696, parallel's `_revise_one_part` ~:9469) checked word
band / heading / wrapper / fidelity but never compared the candidate against the
ORIGINAL part — a provider that echoed the chapter back unchanged passed every check
(word band identical, fidelity 1.0) and was counted `revised`, `changed=True`. The
patch core (`narasi_addressed_patch.py`) already rejects an identical output as
`ineffective`; legacy did not — the sixth occurrence this session of a rule enforced
at one door and absent at another.

Fix: a no-op candidate now feeds the SAME corrective-retry the loop already runs for
word-band/truncation rejections (one extra attempt, reusing the existing 2-attempt
structure, not a new one) — and if attempt 2 is STILL byte-identical, the chapter is
rejected exactly like any other failed rewrite: original text kept, not counted revised.
"""

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

import laozhang_api as lz
import narration_api as na


def _book(n=1):
    return "".join(
        f"## Bab {i}: Judul {i}\n\nKalimat asli unik{i} tetap lengkap.\n\n"
        for i in range(1, n + 1)
    )


def _violations(n=1):
    return [{"severity": "high", "type": "timeline",
             "evidence": f'"Kalimat asli unik{i} tetap lengkap."'} for i in range(1, n + 1)]


def _client(*, responses):
    """`responses` is an iterator of full-chapter strings the provider returns, one per
    call, in order (extra calls beyond the iterator repeat the last value).

    An entry may also be: `None` -> echo THIS chapter back verbatim (a no-op); a
    callable -> called with this chapter's body, its return value is the response
    (order-independent, which the parallel lane needs since its call order is not
    deterministic); an Exception instance -> raised, exercising the provider-failure
    path (the call is still recorded, because the physical call WAS made)."""
    calls = []
    _responses = list(responses)

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            marker = "[CHAPTER — return the corrected version, unchanged except for the fixes]\n"
            chapter = kwargs["messages"][-1]["content"].split(marker, 1)[1]
            chapter = chapter.split("\n\n[CORRECTION]\n", 1)[0]
            idx = min(len(calls) - 1, len(_responses) - 1)
            output = _responses[idx] if _responses[idx] is not None else chapter
            if isinstance(output, BaseException):
                raise output
            if callable(output):
                output = output(chapter)
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content=output), finish_reason="stop")],
                usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0),
            )

    return SimpleNamespace(chat=SimpleNamespace(completions=_Completions())), calls


async def _log_usage(*_a, **_k):
    return 1


@pytest.mark.parametrize("parallel", [0, 2])
def test_byte_identical_first_attempt_is_not_counted_revised(monkeypatch, parallel):
    monkeypatch.setenv("NARASI_REVISE_PARALLEL", str(parallel))
    book = _book(1)
    original_chapter = book  # single chapter == whole book here
    # provider echoes the chapter back UNCHANGED on both attempts
    client, calls = _client(responses=[None, None])
    monkeypatch.setattr(lz, "make_narasi_client", lambda *_a, **_k: client)
    monkeypatch.setattr(lz, "_log_narasi_usage", _log_usage)

    result, _credits = asyncio.run(lz._narasi_revise_chunked(
        book, _violations(1), "storytelling", "id", "test-model",
        tenant_id="t", user_id="u", job_uuid=None))

    assert result == original_chapter, "no-op on both attempts must keep ORIGINAL bytes"
    assert len(calls) == 2, "must have retried once before giving up (not accepted on attempt 1)"


@pytest.mark.parametrize("parallel", [0, 2])
def test_noop_then_real_change_on_retry_is_accepted(monkeypatch, parallel):
    monkeypatch.setenv("NARASI_REVISE_PARALLEL", str(parallel))
    book = _book(1)
    changed = book.replace("asli", "diperbaiki", 1)
    # attempt 1: echoes back unchanged (no-op) → must retry
    # attempt 2: makes the requested change → must be accepted
    client, calls = _client(responses=[None, changed])
    monkeypatch.setattr(lz, "make_narasi_client", lambda *_a, **_k: client)
    monkeypatch.setattr(lz, "_log_narasi_usage", _log_usage)

    result, _credits = asyncio.run(lz._narasi_revise_chunked(
        book, _violations(1), "storytelling", "id", "test-model",
        tenant_id="t", user_id="u", job_uuid=None))

    assert "diperbaiki" in result, "the real change on attempt 2 must land"
    assert result != book
    assert len(calls) == 2


@pytest.mark.parametrize("parallel", [0, 2])
def test_serial_and_parallel_agree_on_noop_semantics(monkeypatch, parallel):
    """Same input, same provider script, run through whichever lane the env selects —
    both lanes must reach the identical verdict (this is the F2 DoD's explicit
    'serial dan parallel identik semantiknya')."""
    monkeypatch.setenv("NARASI_REVISE_PARALLEL", str(parallel))
    book = _book(1)
    client, calls = _client(responses=[None, None])
    monkeypatch.setattr(lz, "make_narasi_client", lambda *_a, **_k: client)
    monkeypatch.setattr(lz, "_log_narasi_usage", _log_usage)

    result, _credits = asyncio.run(lz._narasi_revise_chunked(
        book, _violations(1), "storytelling", "id", "test-model",
        tenant_id="t", user_id="u", job_uuid=None))
    assert result == book


def test_trailing_whitespace_reframe_is_not_a_fake_change(monkeypatch):
    """A candidate that differs from the original ONLY by trailing whitespace the
    server itself re-appends (`_trail`) must still be treated as a no-op — the
    'byte-identical' check has to compare what actually lands in the assembled book,
    not a naively-stripped intermediate that would let whitespace churn masquerade
    as a real edit."""
    monkeypatch.setenv("NARASI_REVISE_PARALLEL", "0")
    book = "## Bab 1: Judul 1\n\nKalimat asli unik1 tetap lengkap.\n\n"
    # provider returns the identical body (rstripped is what the code compares against
    # its own _body variable) — the server re-attaches book's own original trailing
    # bytes regardless, so this must still read as no-op.
    client, calls = _client(responses=[book.strip(), book.strip()])
    monkeypatch.setattr(lz, "make_narasi_client", lambda *_a, **_k: client)
    monkeypatch.setattr(lz, "_log_narasi_usage", _log_usage)

    result, _credits = asyncio.run(lz._narasi_revise_chunked(
        book, _violations(1), "storytelling", "id", "test-model",
        tenant_id="t", user_id="u", job_uuid=None))
    assert result == book
    assert len(calls) == 2


# ─────────────────────────────────────────────────────────────────────────────
# F2 §4-§5 — the ACCOUNTING contract (BRIEF-F2-LEGACY-NOOP-2026-08-15.md).
#
# Until this block existed the lane had exactly ONE observable: "did the book
# change?". On the two-no-op path that byte is identical whether the lane
# classified the chapter `unresolved`, classified it `ineffective`, or never
# attempted it at all — so the third golden row below was literally unwritable.
# Every row here is a DISTINCT (calls, validated, changed, ineffective,
# unresolved) tuple, which is what makes those states separable at all.
# ─────────────────────────────────────────────────────────────────────────────

_LANES = [(0, "serial"), (2, "parallel")]


def _fix(chapter):
    """A minimal, in-band, heading-preserving real repair of whichever chapter
    the provider was handed (order-independent — the parallel lane's call order
    is not deterministic, so a fixed response string cannot be used)."""
    return chapter.replace("asli", "diperbaiki", 1)


class _ProviderDown(Exception):
    pass


def _live(module_name):
    """The module object that lazily-importing production code will ACTUALLY see.

    🔴 `test_middleware.py` deletes every `laozhang_api` entry from `sys.modules` and
       re-imports it, so this file's module-level alias can be a STALE object by the
       time a full-tree run reaches here. `narration_api` imports its helpers lazily
       (`from laozhang_api import ...` inside the function), which resolves through
       `sys.modules` — so patching the stale alias misses silently, production takes
       the real path, and the test is red ONLY in a full-tree run. Cost: one bisect.
       Any test that patches `laozhang_api` and then crosses into another module must
       resolve the module at call time."""
    import importlib
    return importlib.import_module(module_name)


def _run(monkeypatch, book, viols, *, parallel, responses, max_ch=None, spent=0):
    monkeypatch.setenv("NARASI_REVISE_PARALLEL", str(parallel))
    if max_ch is not None:
        monkeypatch.setenv("NARASI_REVISE_MAX_CHAPTERS", str(max_ch))
    client, calls = _client(responses=responses)
    monkeypatch.setattr(lz, "make_narasi_client", lambda *_a, **_k: client)
    monkeypatch.setattr(lz, "_log_narasi_usage", _log_usage)
    sink = {}
    result, _credits = asyncio.run(lz._narasi_revise_chunked(
        book, viols, "storytelling", "id", "test-model",
        tenant_id="t", user_id="u", job_uuid=None,
        attempts_already_spent=spent, stats_out=sink))
    return result, sink, calls


def _noop_then_fix(delay=0.0, barrier_width=0):
    """Per-CHAPTER script: the first sight of a chapter echoes it back (no-op), the
    second repairs it. Keyed on the chapter body rather than the global call index, so
    it behaves identically however the lanes interleave.

    🔴 A ZERO-LATENCY DOUBLE CANNOT SEE A SCHEDULING DEFECT. Measured: with an instant
    fake provider the parallel lane SERIALISES — chapter 1's first attempt resolves
    before chapter 2's task is ever stepped (traced through `asyncio.to_thread`), so
    every lane produced the identical answer and a real starvation bug passed. `delay`
    holds each FIRST attempt long enough that the loop must step the sibling tasks,
    which is the only condition under which admission order matters.

    `barrier_width` is the stronger form: it holds every first attempt until that many
    are in flight together, so concurrency is proven rather than hoped for. Use it only
    where the lane is guaranteed to run concurrently — a width that never fills raises
    through the provider seam (loud and attributable) instead of hanging the suite."""
    seen = set()
    lock = threading.Lock()
    barrier = threading.Barrier(barrier_width) if barrier_width else None

    def responder(chapter):
        with lock:
            first = chapter not in seen
            if first:
                seen.add(chapter)
        if not first:
            return _fix(chapter)
        if barrier is not None:
            barrier.wait(timeout=10)
        elif delay:
            time.sleep(delay)
        return chapter

    return responder


def _expect(*, lane, targeted, attempted, calls, validated, changed,
            ineffective, unresolved):
    """EXACT dict equality, deliberately: it pins the schema, pins every counter,
    pins `violations_verified_resolved` at 0, and simultaneously enforces C12 (no
    prose / evidence / chapter text / id / raw exception can be added later
    without this failing)."""
    return {
        "schema_version": "legacy_revise_stats_v1",
        "lane": lane,
        "chapters_targeted": targeted,
        "chapters_attempted": attempted,
        "provider_calls": calls,
        "candidates_validated": validated,
        "chapters_changed": changed,
        "violations_verified_resolved": 0,
        "ineffective_count": ineffective,
        "unresolved_count": unresolved,
    }


@pytest.mark.parametrize("parallel,lane", _LANES)
def test_stats_row1_first_attempt_valid_change(monkeypatch, parallel, lane):
    book = _book(1)
    result, stats, calls = _run(monkeypatch, book, _violations(1),
                                parallel=parallel, responses=[_fix])
    assert "diperbaiki" in result and result != book
    assert len(calls) == 1
    assert stats == _expect(lane=lane, targeted=1, attempted=1, calls=1,
                            validated=1, changed=1, ineffective=0, unresolved=0)


@pytest.mark.parametrize("parallel,lane", _LANES)
def test_stats_row2_noop_then_valid_change(monkeypatch, parallel, lane):
    book = _book(1)
    result, stats, calls = _run(monkeypatch, book, _violations(1),
                                parallel=parallel, responses=[None, _fix])
    assert "diperbaiki" in result, "the accepted retry must land in the manuscript"
    assert result != book
    assert len(calls) == 2
    assert stats == _expect(lane=lane, targeted=1, attempted=1, calls=2,
                            validated=2, changed=1, ineffective=1, unresolved=0)


@pytest.mark.parametrize("parallel,lane", _LANES)
def test_stats_row3_two_noops_are_unresolved_not_revised(monkeypatch, parallel, lane):
    """The row that was unwritable before stats existed: two no-ops must be
    reported as one ATTEMPTED, one UNRESOLVED, two INEFFECTIVE, zero CHANGED —
    a state the manuscript bytes alone cannot distinguish from 'never tried'."""
    book = _book(1)
    result, stats, calls = _run(monkeypatch, book, _violations(1),
                                parallel=parallel, responses=[None, None])
    assert result == book, "both attempts no-op → EXACT original bytes"
    assert len(calls) == 2
    assert stats == _expect(lane=lane, targeted=1, attempted=1, calls=2,
                            validated=2, changed=0, ineffective=2, unresolved=1)


@pytest.mark.parametrize("parallel,lane", _LANES)
def test_stats_row4_provider_exception(monkeypatch, parallel, lane):
    """A raised provider call is a PHYSICAL call (it costs) but never reaches
    acceptance validation — calls 1, validated 0, and no corrective retry."""
    book = _book(1)
    result, stats, calls = _run(monkeypatch, book, _violations(1),
                                parallel=parallel, responses=[_ProviderDown("down")])
    assert result == book
    assert len(calls) == 1
    assert stats == _expect(lane=lane, targeted=1, attempted=1, calls=1,
                            validated=0, changed=0, ineffective=0, unresolved=1)


@pytest.mark.parametrize("parallel,lane", _LANES)
def test_stats_row5_unmapped_finding_targets_nothing(monkeypatch, parallel, lane):
    book = _book(1)
    unmapped = [{"severity": "high", "type": "timeline",
                 "evidence": '"Kalimat ini tidak pernah ada di dalam buku."'}]
    result, stats, calls = _run(monkeypatch, book, unmapped,
                                parallel=parallel, responses=[_fix])
    assert result == book
    assert calls == [], "an unmapped finding must never reach the provider"
    assert stats == _expect(lane=lane, targeted=0, attempted=0, calls=0,
                            validated=0, changed=0, ineffective=0, unresolved=0)


@pytest.mark.parametrize("parallel,lane", _LANES)
def test_stats_row6_trailing_frame_only_difference_is_ineffective(monkeypatch, parallel, lane):
    """The candidate differs from the stored part ONLY in the trailing bytes the
    server itself re-appends. Compared AFTER reconstruction it is a no-op; compared
    against the raw part it would look like a real edit and be billed as `changed`."""
    book = "## Bab 1: Judul 1\n\nKalimat asli unik1 tetap lengkap.\n\n"
    assert book != book.strip(), "the fixture must actually carry a trailing frame"
    result, stats, calls = _run(monkeypatch, book, _violations(1),
                                parallel=parallel,
                                responses=[book.strip(), book.strip()])
    assert result == book
    assert len(calls) == 2, "a trailing-frame-only echo must be retried, not accepted"
    assert stats == _expect(lane=lane, targeted=1, attempted=1, calls=2,
                            validated=2, changed=0, ineffective=2, unresolved=1)


def test_serial_and_parallel_agree_on_manuscript_and_stats(monkeypatch):
    """§7 DoD: same input, same provider script → identical manuscript bytes AND
    identical accounting. Only `lane` may differ."""
    book = _book(3)
    viols = _violations(3)
    ser_text, ser_stats, ser_calls = _run(monkeypatch, book, viols,
                                          parallel=0, responses=[None, _fix])
    par_text, par_stats, par_calls = _run(monkeypatch, book, viols,
                                          parallel=2, responses=[None, _fix])
    assert ser_text == par_text
    assert len(ser_calls) == len(par_calls)
    assert ser_stats.pop("lane") == "serial"
    assert par_stats.pop("lane") == "parallel"
    assert ser_stats == par_stats


@pytest.mark.parametrize("parallel,lane", _LANES)
def test_budget_all_first_attempts_noop_never_exceeds_two_times_max(monkeypatch, parallel, lane):
    """⚠ COST. With MAX=4 and every first attempt a no-op, BOTH lanes must stop at
    8 physical calls. The parallel lane used to skip its cap entirely whenever no
    earlier lane had spent attempts (`_attempts_already_spent > 0`), so pure-legacy
    parallel fired its per-part corrective retry on BOTH waves = 16 calls — a 2x
    cost divergence from serial on the SAME input."""
    book = _book(8)
    result, stats, calls = _run(monkeypatch, book, _violations(8),
                                parallel=parallel, responses=[None, None], max_ch=4)
    assert result == book
    assert len(calls) == 8, f"{lane}: 2*MAX physical calls is the hard ceiling"
    assert stats == _expect(lane=lane, targeted=8, attempted=4, calls=8,
                            validated=8, changed=0, ineffective=8, unresolved=4)


@pytest.mark.parametrize("parallel,lane", _LANES)
def test_budget_changed_chapters_capped_at_max(monkeypatch, parallel, lane):
    book = _book(8)
    result, stats, calls = _run(monkeypatch, book, _violations(8),
                                parallel=parallel, responses=[_fix], max_ch=4)
    assert result.count("diperbaiki") == 4, "success cap is MAX chapters, not 8"
    assert len(calls) == 4
    assert stats == _expect(lane=lane, targeted=8, attempted=4, calls=4,
                            validated=4, changed=4, ineffective=0, unresolved=0)


@pytest.mark.parametrize("parallel", [0, 2])
def test_public_two_value_return_is_unchanged_without_stats_out(monkeypatch, parallel):
    """`stats_out` is optional and keyword-only; every existing caller keeps the
    exact `(new_text, total_cr)` contract."""
    monkeypatch.setenv("NARASI_REVISE_PARALLEL", str(parallel))
    book = _book(1)
    client, _calls = _client(responses=[_fix])
    monkeypatch.setattr(lz, "make_narasi_client", lambda *_a, **_k: client)
    monkeypatch.setattr(lz, "_log_narasi_usage", _log_usage)

    returned = asyncio.run(lz._narasi_revise_chunked(
        book, _violations(1), "storytelling", "id", "test-model",
        tenant_id="t", user_id="u", job_uuid=None))

    assert isinstance(returned, tuple) and len(returned) == 2
    text, credits = returned
    assert "diperbaiki" in text
    assert isinstance(credits, int)


def test_dispatcher_publishes_the_block_and_it_reaches_the_result_payload(monkeypatch):
    monkeypatch.setenv("NARASI_REVISE_CHUNKED", "1")
    monkeypatch.setenv("NARASI_REVISE_PARALLEL", "0")
    monkeypatch.delenv("NARASI_REVISE_MAX_CHAPTERS", raising=False)
    book = _book(1)
    client, _calls = _client(responses=[_fix])
    monkeypatch.setattr(lz, "make_narasi_client", lambda *_a, **_k: client)
    monkeypatch.setattr(lz, "_log_narasi_usage", _log_usage)
    critique = {"violations": _violations(1)}

    text, _cr = asyncio.run(lz._narasi_consistency_revise(
        book, critique, "storytelling", "id", model="test-model",
        tenant_id="t", user_id="u", job_uuid=None))

    assert "diperbaiki" in text
    assert critique["legacy_revise"] == _expect(
        lane="serial", targeted=1, attempted=1, calls=1, validated=1,
        changed=1, ineffective=0, unresolved=0)

    result = {}
    lz._narasi_copy_legacy_revise_stats(result, critique)
    assert result["legacy_revise"] == critique["legacy_revise"]


# ─────────────────────────────────────────────────────────────────────────────
# F2 round 2 — the two closure defects Rino's review found.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("parallel", [0, 4])
def test_shared_headroom_never_starves_the_corrective_retry(monkeypatch, parallel):
    """🔴 [HIGH] Equal cost caps are NOT equal semantics.

    MAX=4 with the structural lane already 4 calls deep leaves 4 physical calls.
    Serial spends them depth-first: chapter 1 no-op→retry→repair, chapter 2 the same,
    chapters 3-4 refused by budget — 4 calls, 2 chapters repaired. The parallel lane
    admitted breadth-first: all four workers cleared the gate (4,5,6,7 < 8) before any
    of them had a verdict, all four came back no-op, and all four corrective retries
    then hit 8 >= 8 — 4 calls, ZERO repaired. The hard cap held while the mechanism
    that makes a no-op recoverable was silently switched off, in exactly the mixed
    structural+legacy shape production runs.

    Note this is NOT an asyncio race (nothing awaits between the budget test and the
    slot take) — it is breadth-first ADMISSION starving depth-first retry, and it only
    bites once an earlier lane has shrunk the headroom below 2 slots per admitted
    chapter. At `parallel=2` on this same input the two admitted workers DO get their
    retries, which is precisely why the original parity test (parallel=2, spent=0)
    could not see it — nor could a zero-latency double, which serialises the lane
    outright (see `_noop_then_fix`). Both discriminators are required: a first-attempt
    delay AND a concurrency level that admits more chapters than the headroom can
    retry. Measured on the unfixed lane: parallel=4 repairs 0, serial and parallel=2
    repair 2."""
    book = _book(4)
    result, stats, calls = _run(monkeypatch, book, _violations(4),
                                parallel=parallel, responses=[_noop_then_fix(delay=0.05)],
                                max_ch=4, spent=4)
    assert len(calls) == 4, "the shared 2*MAX cap must still hold"
    assert result.count("diperbaiki") == 2, (
        "two chapters must actually be repaired — a lane that spends the whole "
        "headroom on first attempts repairs nothing")
    stats.pop("lane")
    assert stats == {k: v for k, v in _expect(
        lane="ignored", targeted=4, attempted=2, calls=4, validated=4,
        changed=2, ineffective=2, unresolved=0).items() if k != "lane"}


def test_parallel_lane_still_retries_when_first_attempts_are_genuinely_concurrent(monkeypatch):
    """The companion to the test above: at FULL headroom the parallel lane must keep
    its concurrency AND still land every corrective retry. The barrier holds all four
    first attempts in flight simultaneously, so this fails if the fix for the starvation
    defect were to serialise the lane unconditionally."""
    book = _book(4)
    result, stats, calls = _run(monkeypatch, book, _violations(4),
                                parallel=4, responses=[_noop_then_fix(barrier_width=4)],
                                max_ch=4, spent=0)
    assert result.count("diperbaiki") == 4
    assert len(calls) == 8
    assert stats == _expect(lane="parallel", targeted=4, attempted=4, calls=8,
                            validated=8, changed=4, ineffective=4, unresolved=0)


def test_result_payload_persists_the_legacy_block(monkeypatch):
    """🔴 [MEDIUM] `_result_payload` is an explicit allowlist — a new `result` key is
    not carried by default, so the block was built, published, and then dropped before
    the durable jobs row."""
    block = lz._narasi_legacy_revise_stats(
        lane="serial", targeted=1, attempted=1, provider_calls=2,
        candidates_validated=2, chapters_changed=1, ineffective=1, unresolved=0)
    payload = na._result_payload({"book": "isi", "legacy_revise": block})
    assert payload["legacy_revise"] == block
    assert "legacy_revise" not in na._result_payload({"book": "isi"}), (
        "absent must stay absent — no null key on jobs that never revised")
    assert "legacy_revise" not in na._result_payload(
        {"book": "isi", "legacy_revise": None})


def test_v3_gates_copy_the_legacy_block_onto_the_result(monkeypatch):
    """🔴 [MEDIUM] The narration/L3 route hands `_narasi_consistency_revise` a LOCAL
    request dict and copies only what it names back onto `result`; it named
    `structural_patch` alone, so the F2 block never reached the persisted job. Driven
    through the real `_apply_v3_gates` seam — an AST witness cannot prove delivery."""
    block = lz._narasi_legacy_revise_stats(
        lane="serial", targeted=2, attempted=2, provider_calls=3,
        candidates_validated=3, chapters_changed=1, ineffective=1, unresolved=1)

    async def _fake_revise(full_text, critique, *_a, **_k):
        critique["legacy_revise"] = dict(block)
        return full_text, 0

    async def _no_provider(system, user, **_k):
        return "{}", None

    # 🔴 PIN THE FOUR DETECTORS OFF EXPLICITLY rather than inheriting whatever the suite
    #    left in os.environ. Two of them default ON, and in a FULL-TREE run one took a
    #    branch it never takes in isolation: real provider calls, a book the gates had
    #    rewritten, and this test red only when run with the whole suite. The subject
    #    here is the COPY at the end of the merged revise, not the detectors — the branch
    #    is forced through the module-level dedup they all funnel into, so switching them
    #    off makes the test hermetic without weakening what it proves.
    for _flag in ("NARASI_REGISTER_GATE", "NARASI_CANON_DIFF", "NARASI_THREAD_TRACKER",
                  "NARASI_CRITIQUE_ENABLED", "NARASI_CRITIQUE_REVISE",
                  "NARASI_F6_ENABLED",
                  "NARASI_CANON_REGISTRY_EXTRACT"):
        monkeypatch.setenv(_flag, "0")
    # Belt and braces: if a detector still reaches for a provider, fail loudly here
    # rather than through the suite's outbound-network guard at teardown.
    # Patched on the LIVE module — see `_live`; the module-level alias is stale after
    # test_middleware.py re-imports laozhang_api, and these two seams are reached
    # through narration_api's lazy imports, not through this file's alias.
    _lz = _live("laozhang_api")
    monkeypatch.setattr(_lz, "_narasi_cheap_call", _no_provider)
    monkeypatch.setattr(_lz, "_narasi_consistency_revise", _fake_revise)
    # Force the merged-revise branch: the four detectors are nested, but the dedup they
    # all funnel through is module-level and is what decides whether a revise runs.
    monkeypatch.setattr(na, "_v3g_dedup_violations",
                        lambda _v: [{"severity": "high", "type": "timeline",
                                     "evidence": '"apa pun"'}])
    result = {
        "ok": True,
        "book": "## Bab 1\n\nIsi bab.\n",
        "chapters": [{"id": 1, "title": "Bab 1", "text": "Isi bab."}],
    }
    asyncio.run(na._apply_v3_gates(result, {"chapters": result["chapters"]},
                                   tenant_id=None, user_id=None, job_uuid=None))

    assert result.get("legacy_revise") == block, (
        "the gate route must carry the block back onto `result`, or it is lost "
        "before _result_payload ever sees it")
    assert na._result_payload(result)["legacy_revise"] == block


def test_every_dispatcher_route_into_the_legacy_lane_asks_for_the_stats():
    """The dispatcher reaches `_narasi_revise_chunked` from TWO routes: pure-legacy and
    mixed structural+legacy. Only the first is cheap to drive end-to-end, so the second
    is pinned by an AST call-site witness (same technique as the `_narrative_authority`
    witness in test_narasi_outline_bible_rewrite_contract.py) rather than left to a
    string match — a route that silently stops asking for the block would otherwise
    report nothing and look exactly like a run where the legacy lane never fired."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(lz._narasi_consistency_revise))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_narasi_revise_chunked"
    ]
    assert len(calls) == 2, "expected exactly the pure-legacy and mixed routes"
    for call in calls:
        assert any(kw.arg == "stats_out" for kw in call.keywords), \
            "every route into the legacy lane must request the accounting block"

    published = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_narasi_publish_legacy_revise_stats"
    ]
    assert len(published) == 2, "both routes must publish what they collected"


def test_a_changed_chapter_is_never_reported_as_a_verified_resolution(monkeypatch):
    """F6 owns post-repair verification. `chapters_changed` is repair DELIVERY;
    aliasing it into `violations_verified_resolved` would report an unproven
    outcome as proven — the exact counter-naming failure F1 was punished for."""
    book = _book(1)
    _result, stats, _calls = _run(monkeypatch, book, _violations(1),
                                  parallel=0, responses=[_fix])
    assert stats["chapters_changed"] == 1
    assert stats["violations_verified_resolved"] == 0


def test_telemetry_block_carries_no_free_text(monkeypatch):
    """C12: bounded counters only — no prose, evidence, chapter text, id, or raw
    exception may ride along."""
    book = _book(2)
    _result, stats, _calls = _run(monkeypatch, book, _violations(2),
                                  parallel=0, responses=[_ProviderDown("boom")])
    assert set(stats) == {
        "schema_version", "lane", "chapters_targeted", "chapters_attempted",
        "provider_calls", "candidates_validated", "chapters_changed",
        "violations_verified_resolved", "ineffective_count", "unresolved_count"}
    assert stats["schema_version"] == "legacy_revise_stats_v1"
    assert stats["lane"] == "serial"
    for key, value in stats.items():
        if key in ("schema_version", "lane"):
            continue
        assert type(value) is int, f"{key} must be a bounded integer counter"
    assert "boom" not in str(stats)
