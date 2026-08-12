"""L3-ASSIST Stage 1 — WIRING.

What Stage 1 claims, and therefore what this file has to make falsifiable:

  * every chapter worker receives EXACTLY the same canon bytes and hash;
  * the shared context is materialised and FROZEN before any worker starts, and
    the freeze reaches NESTED containers, not just attribute rebinding;
  * a mutation attempted during the MAP is refused, and is invisible to every
    worker that runs after it;
  * MAP stays genuinely parallel — the freeze must not serialise the fan-out;
  * the dispatched canon hash is the hash of the dispatched bytes;
  * `assist` FAILS CLOSED: an arming failure refuses the job before the first
    worker exists, and a census failure refuses it before delivery;
  * a resumed chapter is bound by its persisted canon/context or regenerated;
  * `off` and `shadow` jobs are behaviourally unchanged.

🔴 EVERY ASSERTION HERE READS WHAT THE WORKER WAS HANDED, never what the
   orchestrator intended to hand it. Checking the value we computed proves only
   that we computed it once — the interesting failures (a prefix rebuilt per
   chapter, truncated, re-encoded, or quietly dropped for one worker) are
   visible only from the receiving end.

🔴 AND EVERY GUARANTEE HAS A NEGATIVE CONTROL. A test that only ever sees the
   healthy path cannot distinguish "the guard held" from "the guard is absent
   and nothing challenged it". Each gate below is therefore paired with an
   injected failure that must turn the job non-successful — the paired test is
   the one that proves the passing test was measuring anything.
"""
import asyncio
import hashlib
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import canon_lite as cl                                    # noqa: E402
import canon_lite_semantic_source as css                   # noqa: E402
from orchestrator import dynamic as dyn                     # noqa: E402
from orchestrator import static as st                      # noqa: E402
from orchestrator.context_builder import SharedContext     # noqa: E402


async def _fake_story_bible_with_semantics(topic, outline, *, is_fiction=True, style=None,
                                           language="id", manager_model=None, timeout=None,
                                           telemetry_sink=None, extra_negative=None,
                                           structured_semantic=False):
    """P0-B test double for `orchestrator.dynamic.build_story_bible` — no LLM call, no
    delay, returns INSTANTLY.

    🔴 WHY THIS FILE NEEDS IT NOW. Assist requires REAL semantic authority to arm (P0-B):
       `has_semantic_authority()` — and therefore this file's whole `assist` fixture —
       needs at least one entity/anchor/one_time_event, which only a Story Bible call can
       produce. This file predates P0-B and deliberately disables the real Story Bible
       call (`NARASI_STORY_BIBLE=0`) to stay fast and focused on FREEZE MECHANICS, not
       Story Bible content — this double keeps that focus: it returns a MINIMAL valid
       envelope built from whatever outline it is called with (so it binds correctly for
       any `n`), never touches a provider, and returns before any `await` could yield to
       the scheduler — so it cannot change the MAP-phase concurrency this file measures
       (`rec.peak`): it is one sequential, pre-MAP await that completes before the MAP's
       `asyncio.gather` is even created, exactly like the real call it replaces.
    """
    text = "1. CHARACTERS\nTest Entity (test double bible, no LLM called)."
    if not structured_semantic:
        return text
    # P0-B round 2: the envelope binds the EXACT bible response it came from, so the double
    # must name its own `text` — a source that cannot say which bible produced it is refused.
    source = css.build_semantic_source_v1(
        outline_chapters=outline, bible_text=text,
        entities=(cl.CanonEntityV1(entity_id="ent1", canonical_name="Test Entity",
                                   aliases=(), alias_source="none"),),
    )
    # THREE values under `structured_semantic`: prose, envelope, and the advisory
    # `canon_registry` sidecar (`None` here — this double has no registry to carry).
    return text, source, None


def _outline(n=3):
    return [{"id": i + 1, "title": f"Bab {i + 1}", "summary": f"ringkasan {i + 1}",
             "word_target": 800} for i in range(n)]


class _Recorder:
    """A chapter worker stub that records what it was ACTUALLY handed.

    It reports the same two MEASURED values the real `_write_chapter` reports —
    the hash of the canon bytes it received and the digest of the context it
    read — because the post-MAP census consumes them. A stub that skipped them
    would make every assist job fail as unaccountable, which is the correct
    behaviour of the census and a useless test harness.
    """

    def __init__(self, mutate_ctx=False, *, bind=True):
        self.calls = []
        self.peak = 0
        self.live = 0
        self.mutate_attempts = []
        self._mutate_ctx = mutate_ctx
        self._bind = bind

    def _report(self, kw):
        """The binding a real worker would report for what it was handed."""
        if not self._bind or not kw.get("canon_text"):
            return {}
        return {
            "canon_prompt_sha256": cl.sha256_hex(kw["canon_text"].encode("utf-8")),
            "context_sha256_seen": cl.context_digest(kw["ctx"]),
            "canon_sha256": kw.get("canon_sha256"),
            "context_sha256": kw.get("context_sha256"),
        }

    async def __call__(self, **kw):
        self.live += 1
        self.peak = max(self.peak, self.live)
        try:
            # Let every sibling start before any finishes, so `peak` reflects real
            # concurrency rather than the scheduler happening to run us serially.
            await asyncio.sleep(0.02)
            if self._mutate_ctx:
                # A worker trying to write to shared context AFTER the freeze.
                try:
                    kw["ctx"].topic = "mutated-by-worker"
                    self.mutate_attempts.append(None)          # no exception: BAD
                except Exception as exc:                       # noqa: BLE001
                    self.mutate_attempts.append(type(exc).__name__)
            self.calls.append({
                "no": kw["no"],
                "canon_text": kw.get("canon_text"),
                "canon_prompt_sha": kw.get("canon_prompt_sha"),
                "canon_sha256": kw.get("canon_sha256"),
                "context_sha256": kw.get("context_sha256"),
                "ctx_topic": kw["ctx"].topic,
                "ctx_ch0_title": (kw["ctx"].chapters[0]["title"]
                                  if kw["ctx"].chapters else None),
                "ctx_n_chapters": len(kw["ctx"].chapters or []),
            })
            return {"ok": True, "output": f"teks {kw['no']}", "no": kw["no"],
                    "model": "m", **self._report(kw)}
        finally:
            self.live -= 1


#: Assist is per-tenant now, so every control that exercises it has to run AS a
#: tenant that is on the allowlist. That is not test scaffolding around the gate —
#: it is the gate, and a control that forgot it would silently be testing `off`.
CANARY_TENANT = "t-canary"


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    # P0-B: was "0" (Story Bible disabled) before P0-B existed, when this file's freeze
    # mechanics had no dependency on it. Assist now REQUIRES semantic authority to arm, and
    # semantic authority comes only from a Story Bible call — so it must run, but only
    # against the test double below, never a real provider: `_fake_story_bible_with_semantics`
    # is instant and makes no network call at all, so it stays inert under the outbound
    # guard rather than tripping it.
    monkeypatch.setenv("NARASI_STORY_BIBLE", "1")
    monkeypatch.setattr(dyn, "build_story_bible", _fake_story_bible_with_semantics)
    # P0-B round 2 gates structured extraction to FICTION (nonfiction gets no LLM-derived
    # semantic authority until a grounding provenance path exists). This file passes no
    # style at all, and `_is_fiction_style(None)` is False — without this override every
    # assist row here would take the nonfiction refusal path and stop testing freeze
    # mechanics entirely. The nonfiction refusal itself is covered in
    # test_canon_lite_p0b_semantic_source.py, which is where it belongs.
    monkeypatch.setattr(st, "_is_fiction_style", lambda style: True)
    monkeypatch.setenv("NARASI_CANON_LITE_ASSIST_TENANTS", CANARY_TENANT)
    monkeypatch.delenv("NARASI_CANON_LITE_MODE", raising=False)
    monkeypatch.delenv("NARRATION_RESUME_ENABLED", raising=False)
    original = st._write_chapter
    yield
    st._write_chapter = original
    # 🔴 `_run` SETS THE MODE THROUGH `os.environ` DIRECTLY, SO MONKEYPATCH CANNOT
    #    UNDO IT. `delenv` at setup only records whatever the PREVIOUS test left
    #    behind and faithfully restores that at teardown, so the last mode this
    #    file selects survives the file and reconfigures every test module that
    #    sorts after it — canon lite then arms inside suites that never asked for
    #    it. Clearing it here is what keeps this file's blast radius equal to this
    #    file.
    os.environ.pop("NARASI_CANON_LITE_MODE", None)


async def _run(mode, n=4, *, stub=None, max_parallel=4, **kw):
    if mode is None:
        os.environ.pop("NARASI_CANON_LITE_MODE", None)
    else:
        os.environ["NARASI_CANON_LITE_MODE"] = mode
    rec = stub or _Recorder()
    st._write_chapter = rec
    chapters = _outline(n)
    kw.setdefault("tenant_id", CANARY_TENANT)
    # The server's activation decision must now be PRESENT for assist to stay armed:
    # narrate_chapters fails closed on anything that is not exactly True, so an absent or
    # mis-threaded decision disarms rather than being read as consent. These rows are about
    # what assist DOES once it has been cleared, so they state the clearance explicitly.
    #
    # ⚠️ This is NOT the fail-closed check being switched off. The negative controls — absent,
    #    None, "on", "off", 1, a forged payload field — live in
    #    test_canon_lite_l3_assist_activation.py and drive this same call WITHOUT the kwarg.
    kw.setdefault("assist_activation_ready", True)
    res = await st.narrate_chapters(
        "topik", chapters, polish="none", max_parallel=max_parallel,
        shared_context=SharedContext(topic="topik", chapters=chapters), **kw)
    return res, rec


# ── 1. one canon, byte-identical, to every worker ───────────────────────────
def test_every_worker_receives_identical_canon_bytes_and_hash():
    res, rec = asyncio.run(_run("assist", n=4))
    assert res.get("ok"), res
    assert len(rec.calls) == 4, rec.calls

    texts = {c["canon_text"] for c in rec.calls}
    shas = {c["canon_prompt_sha"] for c in rec.calls}
    assert len(texts) == 1, f"workers received {len(texts)} DIFFERENT canon renderings"
    assert len(shas) == 1, f"workers received {len(shas)} different canon hashes"

    canon_text = texts.pop()
    assert canon_text, "assist ran with NO canon injected — the mode is inert"
    # Non-vacuity: it really is the canon, not an empty string that trivially matches.
    assert canon_text.startswith("CANON "), canon_text[:40]

    # 🔴 THE DISPATCHED HASH IS THE HASH OF THE DISPATCHED BYTES. A hash carried
    #    alongside the text but computed from something else would satisfy every
    #    "all workers agree" check above and still be wrong.
    assert shas.pop() == hashlib.sha256(canon_text.encode("utf-8")).hexdigest()

    # The full binding reached every worker, not just the prompt hash.
    assert len({c["canon_sha256"] for c in rec.calls}) == 1
    assert len({c["context_sha256"] for c in rec.calls}) == 1
    assert all(c["canon_sha256"] and c["context_sha256"] for c in rec.calls)


def test_the_persisted_payload_gains_the_key_ONLY_when_assist_ran():
    """🔴 THE PERSISTENCE HALF, TESTED WHERE IT ACTUALLY HAPPENS.

    `narrate_chapters` returning a binding is not persistence — the durable row
    is whatever `_result_payload` builds. Listing the key beside the others there
    would stamp `canon_lite_binding: null` onto every off/shadow job's persisted
    payload, changing the shape of jobs that never ran assist. Flag-off has to
    stay byte-for-byte what it was, and `None` is not absent.
    """
    import narration_api as na

    binding = {"mode": "assist", "canon_sha256": "a" * 64,
               "canon_prompt_sha256": "b" * 64, "context_sha256": "c" * 64,
               "chapters_bound": 3}
    with_assist = na._result_payload({"book": "isi", "canon_lite_binding": binding})
    without = na._result_payload({"book": "isi"})
    explicit_none = na._result_payload({"book": "isi", "canon_lite_binding": None})

    assert with_assist["canon_lite_binding"] == binding
    assert "canon_lite_binding" not in without, \
        "a non-assist job's persisted payload grew an assist key"
    assert "canon_lite_binding" not in explicit_none
    # And the rest of the payload is untouched by the addition.
    assert set(with_assist) - set(without) == {"canon_lite_binding"}
    assert without == explicit_none


def test_binding_is_reported_on_the_result_for_persistence():
    """The binding has to OUTLIVE the run, or nothing can be reconciled later."""
    res, _ = asyncio.run(_run("assist", n=3))
    binding = res.get("canon_lite_binding")
    assert binding, "assist returned no durable binding"
    assert binding["mode"] == "assist"
    assert binding["chapters_bound"] == 3
    for key in ("canon_sha256", "canon_prompt_sha256", "context_sha256"):
        assert len(binding[key]) == 64, (key, binding[key])
    # C12/§10: hashes and counts only — no prose may ride to persistence here.
    assert set(binding) == {"mode", "canon_sha256", "canon_prompt_sha256",
                            "context_sha256", "chapters_bound"}


@pytest.mark.parametrize("mode", [None, "shadow"])
def test_non_assist_jobs_carry_no_binding(mode):
    res, _ = asyncio.run(_run(mode, n=2))
    assert res.get("ok"), res
    assert "canon_lite_binding" not in res, \
        f"mode={mode} emitted an assist-only key; flag-off must stay shape-identical"


# ── 2. frozen BEFORE the first worker, and the freeze holds ─────────────────
def test_shared_context_is_hard_frozen_before_any_worker_starts():
    rec = _Recorder(mutate_ctx=True)
    res, rec = asyncio.run(_run("assist", n=3, stub=rec))
    assert res.get("ok"), res
    assert len(rec.mutate_attempts) == 3

    # 🔴 EVERY worker's attempt must have RAISED. One `None` means one worker
    #    found the context writable, which is the whole property failing for the
    #    one chapter nobody would have looked at.
    assert all(a == "CanonFrozenError" for a in rec.mutate_attempts), rec.mutate_attempts

    # ...and no worker observed a mutated value, including the ones that ran
    # after the attempt.
    assert {c["ctx_topic"] for c in rec.calls} == {"topik"}


def test_nested_container_mutation_is_refused_not_merely_detected():
    """🔴 THE GAP AN ATTRIBUTE GUARD CANNOT SEE.

    `ctx.chapters = []` raises because `__setattr__` is guarded. `ctx.chapters[0]
    ["title"] = ...` and `ctx.chapters.append(...)` never touch `ctx` at all, so
    the attribute guard is blind to them. Detected-only, they change the input of
    every worker that has not started yet and are reported after the whole book
    has been written. Each mutation below must RAISE.
    """
    seen = {}

    class _Nested(_Recorder):
        async def __call__(self, **kw):
            if kw["no"] == 0:
                ctx = kw["ctx"]
                for label, fn in (
                    ("item_assign", lambda: ctx.chapters[0].__setitem__("title", "hijack")),
                    ("append", lambda: ctx.chapters.append({"id": 99, "title": "extra"})),
                    ("pop", lambda: ctx.chapters.pop()),
                    ("clear", lambda: ctx.chapters.clear()),
                    ("sort", lambda: ctx.chapters.sort(key=lambda c: c["id"])),
                    ("dict_update", lambda: ctx.chapters[0].update({"title": "hijack"})),
                    ("dict_del", lambda: ctx.chapters[0].pop("title", None)),
                ):
                    try:
                        fn()
                        seen[label] = None            # no exception: BAD
                    except Exception as exc:          # noqa: BLE001
                        seen[label] = type(exc).__name__
            return await super().__call__(**kw)

    res, rec = asyncio.run(_run("assist", n=3, stub=_Nested()))
    assert res.get("ok"), res
    assert seen, "the mutating worker never ran"
    assert all(v == "CanonFrozenError" for v in seen.values()), seen
    # The state itself is untouched — a guard that raises but mutates anyway
    # would satisfy the assertion above.
    assert {c["ctx_ch0_title"] for c in rec.calls} == {"Bab 1"}
    assert {c["ctx_n_chapters"] for c in rec.calls} == {3}


def test_one_workers_mutation_is_invisible_to_the_next_worker():
    """🔴 THE NEGATIVE CONTROL FOR CROSS-WORKER CONTAMINATION.

    Ordering is forced with an Event rather than by serialising the MAP: the
    claim is that a LATER worker cannot observe an EARLIER worker's write, and
    that claim is only tested if one demonstrably ran after the other while the
    fan-out was still genuinely parallel.
    """
    mutated = asyncio.Event()
    observations = []

    class _Racer(_Recorder):
        async def __call__(self, **kw):
            ctx = kw["ctx"]
            if kw["no"] == 0:
                for fn in (lambda: ctx.chapters[0].__setitem__("title", "hijack"),
                           lambda: setattr(ctx, "topic", "hijack"),
                           lambda: ctx.chapters.append({"id": 99, "title": "extra"})):
                    try:
                        fn()
                    except Exception:      # noqa: BLE001 - expected
                        pass
                mutated.set()
            else:
                # Every other worker reads only AFTER the mutation was attempted.
                await asyncio.wait_for(mutated.wait(), timeout=5)
                observations.append((ctx.topic, ctx.chapters[0]["title"],
                                     len(ctx.chapters)))
            return await super().__call__(**kw)

    n = 4
    res, rec = asyncio.run(_run("assist", n=n, stub=_Racer(), max_parallel=n))
    assert res.get("ok"), res
    assert len(observations) == n - 1, observations
    # The pristine reading: original topic, original title, original chapter count.
    assert set(observations) == {("topik", "Bab 1", n)}, \
        f"a later worker observed an earlier worker's mutation: {observations}"


def test_the_original_chapter_alias_cannot_reach_the_workers():
    """🔴 FREEZING `ctx.chapters` IS NOT THE SAME AS FREEZING WHAT THE MAP READS.

    The freeze replaces `ctx.chapters` with deep-frozen copies, but the fan-out
    iterates the chapter list the CALLER passed in, whose dicts are still the
    caller's original mutable objects. Mutating one of those after the freeze
    changes the `ch` a worker is handed while the frozen context shows nothing —
    the guard holds and the guarantee still fails. This control mutates the
    caller's own objects, from inside the MAP, and then reads what the workers
    were handed.
    """
    chapters = _outline(4)
    ctx = SharedContext(topic="topik", chapters=chapters)
    hijacked = asyncio.Event()
    seen = []

    class _Alias(_Recorder):
        async def __call__(self, **kw):
            if kw["no"] == 0:
                # The alias the freeze does not own. These MUST succeed — the
                # test is worthless if the mutation silently no-ops.
                chapters[1]["title"] = "hijacked"
                chapters[1]["summary"] = "hijacked"
                chapters.append({"id": 99, "title": "extra"})
                hijacked.set()
            else:
                await asyncio.wait_for(hijacked.wait(), timeout=5)
                seen.append((kw["no"], kw["ch"]["title"], kw["ch"]["summary"]))
            return await super().__call__(**kw)

    os.environ["NARASI_CANON_LITE_MODE"] = "assist"
    st._write_chapter = _Alias()
    res = asyncio.run(st.narrate_chapters(
        "topik", chapters, polish="none", max_parallel=4, shared_context=ctx,
        tenant_id=CANARY_TENANT, assist_activation_ready=True))

    assert res.get("ok"), res
    # The alias really was mutated — otherwise this proves nothing.
    assert chapters[1]["title"] == "hijacked"
    assert len(chapters) == 5
    # ...and no worker was handed any of it.
    assert len(seen) == 3, seen
    assert ("Bab 2", "ringkasan 2") in {(t, s) for _, t, s in seen}, seen
    assert all(t != "hijacked" and s != "hijacked" for _, t, s in seen), (
        f"a worker was handed a chapter mutated through the original alias: {seen}")


def test_assist_refuses_when_the_outline_diverges_from_the_canon():
    """The canon is hashed over `ctx.chapters`; the book is written from the
    chapter list. If they disagree, the binding is a statement about a different
    book, and there is no safe way to guess which one is right."""
    ctx_chapters = _outline(3)
    map_chapters = _outline(4)
    os.environ["NARASI_CANON_LITE_MODE"] = "assist"
    rec = _Recorder()
    st._write_chapter = rec
    res = asyncio.run(st.narrate_chapters(
        "topik", map_chapters, polish="none", max_parallel=4,
        shared_context=SharedContext(topic="topik", chapters=ctx_chapters),
        tenant_id=CANARY_TENANT, assist_activation_ready=True))

    assert res.get("ok") is False, res
    assert res.get("error") == "canon_lite_assist_outline_divergence"
    assert rec.calls == [], "assist spent worker calls on a divergent outline"


def test_post_freeze_mutation_cannot_alter_worker_input():
    """The negative control for the freeze: try to move the ground under the MAP."""
    class _Mutator(_Recorder):
        async def __call__(self, **kw):
            if kw["no"] == 0:
                for attr, value in (("topic", "hijacked"), ("chapters", [])):
                    try:
                        setattr(kw["ctx"], attr, value)
                    except Exception:      # noqa: BLE001 - expected
                        pass
            return await super().__call__(**kw)

    res, rec = asyncio.run(_run("assist", n=4, stub=_Mutator()))
    assert res.get("ok"), res
    assert {c["ctx_topic"] for c in rec.calls} == {"topik"}, "a worker saw a mutated context"
    assert len({c["canon_text"] for c in rec.calls}) == 1, \
        "the canon handed to later workers changed after a mutation attempt"


def test_context_is_restored_to_a_mutable_object_after_the_run():
    """The freeze is for the fan-out, not for the caller's object forever."""
    chapters = _outline(3)
    ctx = SharedContext(topic="topik", chapters=chapters)
    os.environ["NARASI_CANON_LITE_MODE"] = "assist"
    st._write_chapter = _Recorder()
    res = asyncio.run(st.narrate_chapters(
        "topik", chapters, polish="none", max_parallel=2, shared_context=ctx,
        tenant_id=CANARY_TENANT, assist_activation_ready=True))
    assert res.get("ok"), res
    ctx.topic = "writable again"              # must not raise
    ctx.chapters.append({"id": 4, "title": "Bab 4"})
    assert type(ctx).__name__ == "SharedContext"
    assert type(ctx.chapters) is list


# ── 3. the freeze must not cost parallelism ────────────────────────────────
def test_map_stays_genuinely_parallel_under_assist():
    res, rec = asyncio.run(_run("assist", n=4, max_parallel=4))
    assert res.get("ok"), res
    assert rec.peak > 1, (
        f"chapter execution serialised under assist (peak concurrency {rec.peak}). "
        "Freezing the context must not turn the MAP into a loop.")


# ── 4. off / shadow are behaviourally unchanged ────────────────────────────
@pytest.mark.parametrize("mode", [None, "shadow"])
def test_non_assist_jobs_receive_no_injection(mode):
    res, rec = asyncio.run(_run(mode, n=3))
    assert res.get("ok"), res
    assert all(c["canon_text"] is None for c in rec.calls), \
        f"mode={mode} injected a canon; only assist may change worker input"
    assert all(c["canon_prompt_sha"] is None for c in rec.calls)


def test_shadow_context_stays_writable():
    """Shadow may observe, never prevent — §9: no user-visible change."""
    rec = _Recorder(mutate_ctx=True)
    res, rec = asyncio.run(_run("shadow", n=2, stub=rec))
    assert res.get("ok"), res
    assert all(a is None for a in rec.mutate_attempts), (
        f"shadow raised on a context write ({rec.mutate_attempts}) — that is assist "
        "behaviour leaking into a mode that must stay observational")


def test_shadow_nested_containers_stay_writable():
    """The deep freeze is assist-only too. Shadow reports; it never blocks."""
    outcome = {}

    class _Nested(_Recorder):
        async def __call__(self, **kw):
            if kw["no"] == 0:
                try:
                    kw["ctx"].chapters[0]["title"] = "shadow-write"
                    outcome["raised"] = None
                except Exception as exc:      # noqa: BLE001
                    outcome["raised"] = type(exc).__name__
            return await super().__call__(**kw)

    res, _ = asyncio.run(_run("shadow", n=2, stub=_Nested()))
    assert res.get("ok"), res
    assert outcome["raised"] is None, (
        f"shadow refused a nested write ({outcome['raised']}) — prevention leaked "
        "into an observational mode")


# ── 5. enforce stays deferred ──────────────────────────────────────────────
def test_enforce_is_still_refused_before_any_work():
    res, rec = asyncio.run(_run("enforce", n=3))
    assert res.get("ok") is False
    assert res.get("error") == "canon_lite_mode_unavailable_enforce"
    assert rec.calls == [], "enforce spent worker calls before refusing"


# ── 6. GATE 1 — assist fails CLOSED when it cannot be armed ────────────────
#
# 🔴 THE FAILURE MUST LAND BEFORE THE FIRST WORKER EXISTS. Falling through to a
#    normal generation would produce a book with no canon in it while the job
#    still reports success and still charges — a silently uncanonical book is
#    exactly the outcome assist was bought to prevent, and it is undetectable
#    afterwards because the output looks completely normal.

@pytest.mark.parametrize("attr,boom,code", [
    ("build_canon_lite_v1", True, "canon_lite_assist_canon_unavailable"),
    ("render_canon", True, "canon_lite_assist_arming_failed"),
    ("SharedContextFreeze", True, "canon_lite_assist_arming_failed"),
])
def test_assist_refuses_before_any_worker_when_arming_fails(monkeypatch, attr, boom, code):
    def _raise(*_a, **_k):
        raise RuntimeError("injected arming failure")

    monkeypatch.setattr(cl, attr, _raise if boom else None)
    res, rec = asyncio.run(_run("assist", n=3))
    assert res.get("ok") is False, res
    assert res.get("error") == code, res
    assert rec.calls == [], (
        f"assist spent {len(rec.calls)} worker call(s) before refusing — the refusal "
        "has to be free, and after the first dispatch it no longer is")
    assert res.get("book") == "" and res.get("chapters") == []


def test_assist_refuses_before_any_worker_when_the_composer_is_unavailable(monkeypatch):
    """🔴 THE DEGRADATION PATH THAT SPENDS THE WHOLE BOOK BEFORE FAILING.

    `_write_chapter` falls back to a minimal direct prompt when the assembler is
    missing, so the strategy keeps working — but that fallback has no system
    prefix to prepend a canon to. Every chapter would be written with no canon,
    report no hash, and the census would refuse the job only after the entire
    book had been generated and paid for. Under assist the assembler is a
    precondition, checked while refusing is still free.
    """
    monkeypatch.setattr(st, "_COMPOSE_OK", False)
    res, rec = asyncio.run(_run("assist", n=3))
    assert res.get("ok") is False, res
    assert res.get("error") == "canon_lite_assist_composer_unavailable"
    assert rec.calls == [], (
        f"assist ran {len(rec.calls)} uncanonical chapter(s) before refusing — the "
        "whole point is that the refusal costs nothing")


def test_shadow_still_runs_without_the_composer(monkeypatch):
    """The fallback stays available to every mode that makes no canon promise."""
    monkeypatch.setattr(st, "_COMPOSE_OK", False)
    res, rec = asyncio.run(_run("shadow", n=3))
    assert res.get("ok"), res
    assert len(rec.calls) == 3, "shadow lost the assembler fallback"


def test_assist_refuses_when_the_rendering_is_empty(monkeypatch):
    """🔴 THE FAILURE THAT PASSES EVERY DOWNSTREAM CHECK BY BEING CONSISTENT.

    An empty rendering is injected identically into every worker and hashes
    identically everywhere, so the census agrees with itself perfectly while no
    canon reached anyone. Consistency is not the property; presence is.
    """
    monkeypatch.setattr(cl, "render_canon", lambda _c: "")
    res, rec = asyncio.run(_run("assist", n=3))
    assert res.get("ok") is False, res
    assert res.get("error") == "canon_lite_assist_arming_failed"
    assert rec.calls == []


@pytest.mark.parametrize("attr", ["build_canon_lite_v1", "render_canon",
                                  "SharedContextFreeze"])
def test_shadow_still_degrades_where_assist_refuses(monkeypatch, attr):
    """The behavioural split, stated as its own control.

    Same injected failure, opposite required outcome: shadow may never change
    what the user receives, so it logs and delivers. Testing only the assist half
    would leave "assist fails closed" indistinguishable from "the whole feature
    fails closed", which would be a production regression for every shadow job.
    """
    def _raise(*_a, **_k):
        raise RuntimeError("injected arming failure")

    monkeypatch.setattr(cl, attr, _raise)
    res, rec = asyncio.run(_run("shadow", n=3))
    assert res.get("ok"), res
    assert len(rec.calls) == 3, "shadow dropped chapters over an observation failure"
    assert all(c["canon_text"] is None for c in rec.calls)


# ── 7. GATE 3 — the census FAILS THE JOB, it does not just log ─────────────
#
# 🔴 EACH CASE BELOW WOULD PREVIOUSLY HAVE PRODUCED A DELIVERED BOOK PLUS A LOG
#    LINE. That is the defect: the book assembles, the credits settle, and the
#    only record that the guarantee did not hold is a line nobody reads until a
#    reader complains about continuity months later.

def test_missing_worker_hash_fails_the_job():
    """A chapter with no hash is not a chapter that passed — it is a chapter
    nothing was measured on. Silence must not read as assent."""
    res, rec = asyncio.run(_run("assist", n=3, stub=_Recorder(bind=False)))
    assert res.get("ok") is False, res
    assert res.get("error") == "canon_lite_assist_binding_missing"
    assert res.get("book") == ""


def test_one_chapter_with_a_foreign_canon_hash_fails_the_job():
    class _OneOdd(_Recorder):
        def _report(self, kw):
            rep = super()._report(kw)
            if kw["no"] == 1:
                rep["canon_prompt_sha256"] = "f" * 64
            return rep

    res, _ = asyncio.run(_run("assist", n=4, stub=_OneOdd()))
    assert res.get("ok") is False, res
    assert res.get("error") == "canon_lite_assist_binding_mismatch"


def test_all_chapters_agreeing_on_the_WRONG_canon_still_fails():
    """🔴 UNANIMITY IS NOT CORRECTNESS.

    Every worker reporting the same hash satisfies "they all got the same
    thing" while every one of them got the wrong thing. The census is exact
    against the DISPATCHED value for precisely this case.
    """
    class _AllOdd(_Recorder):
        def _report(self, kw):
            rep = super()._report(kw)
            rep["canon_prompt_sha256"] = "a" * 64
            return rep

    res, _ = asyncio.run(_run("assist", n=4, stub=_AllOdd()))
    assert res.get("ok") is False, res
    assert res.get("error") == "canon_lite_assist_binding_mismatch"


def test_a_worker_seeing_a_foreign_context_fails_the_job():
    """Mutate-and-restore is invisible to a start-vs-end digest comparison; it is
    not invisible to a digest sampled inside the window, once per chapter."""
    class _OddCtx(_Recorder):
        def _report(self, kw):
            rep = super()._report(kw)
            if kw["no"] == 2:
                rep["context_sha256_seen"] = "b" * 64
            return rep

    res, _ = asyncio.run(_run("assist", n=4, stub=_OddCtx()))
    assert res.get("ok") is False, res
    assert res.get("error") == "canon_lite_assist_context_mismatch"


def test_freeze_violation_fails_the_job(monkeypatch):
    monkeypatch.setattr(
        cl.SharedContextFreeze, "verify",
        lambda self: (False, ("shared_context_mutated_after_freeze",)))
    res, _ = asyncio.run(_run("assist", n=3))
    assert res.get("ok") is False, res
    assert res.get("error") == "canon_lite_assist_freeze_violation"


def test_shadow_survives_every_condition_that_fails_assist(monkeypatch):
    """The same violations, in shadow, must still deliver the book."""
    monkeypatch.setattr(
        cl.SharedContextFreeze, "verify",
        lambda self: (False, ("shared_context_mutated_after_freeze",)))
    res, rec = asyncio.run(_run("shadow", n=3, stub=_Recorder(bind=False)))
    assert res.get("ok"), res
    assert len(rec.calls) == 3


def test_a_failed_chapter_does_not_fail_the_whole_assist_job():
    """The census denominator is DELIVERED chapters, not attempted ones.

    A chapter that failed generation contributes no text to the book; failing the
    job over it would change legacy partial-success behaviour for a reason that
    has nothing to do with the canon.
    """
    class _OneFails(_Recorder):
        async def __call__(self, **kw):
            res = await super().__call__(**kw)
            if kw["no"] == 1:
                return {"ok": False, "output": None, "no": kw["no"],
                        "error": "provider timeout"}
            return res

    res, _ = asyncio.run(_run("assist", n=4, stub=_OneFails()))
    assert res.get("ok"), res
    assert res["canon_lite_binding"]["chapters_bound"] == 3


# ── 8. GATE 4 — a resumed chapter is BOUND or it is regenerated ────────────
#
# 🔴 RESUMED TEXT IS TEXT SOMEONE ELSE'S RUN WROTE. Under a different canon or a
#    different context it makes the book canonical in the chapters that happened
#    to be regenerated and not in the ones that were not — invisible afterwards,
#    because the output looks complete.

def _fake_db(rows, saved):
    mod = types.ModuleType("database")

    async def get_job_by_external(_tenant, _external):
        return {"id": "00000000-0000-0000-0000-0000000000aa"}

    async def get_narasi_chapter_contents(_tenant, _uuid):
        return list(rows)

    async def save_narasi_chapter(_tenant, _uuid, index, content, wc,
                                  source_prompt, retrieved, *a, **k):
        saved.append({"index": index, "content": content,
                      "source_prompt": source_prompt})

    mod.get_job_by_external = get_job_by_external
    mod.get_narasi_chapter_contents = get_narasi_chapter_contents
    mod.save_narasi_chapter = save_narasi_chapter
    return mod


def test_assist_never_reuses_a_checkpoint(monkeypatch):
    """🔴 A CHECKPOINT CANNOT PROVE WHICH RUN IT BELONGS TO.

    Nothing durable ties a checkpointed chapter to the canon it was written
    under, so under assist there is no reading of a checkpoint that establishes
    it is canonical for THIS run. Reusing one would make the book canonical in
    the chapters that happened to be regenerated and not in the others —
    undetectable afterwards, because the output looks complete. Stage 1 settles
    it by regenerating everything.
    """
    saved = []
    rows = [{"chapter_index": 0, "content": "teks checkpoint", "source_prompt": ""},
            {"chapter_index": 1, "content": "teks checkpoint 2", "source_prompt": ""}]
    monkeypatch.setitem(sys.modules, "database", _fake_db(rows, saved))
    monkeypatch.setenv("NARRATION_RESUME_ENABLED", "1")

    res, rec = asyncio.run(_run("assist", n=3, tenant_id=CANARY_TENANT, job_id="j-1"))
    assert res.get("ok"), res
    assert {c["no"] for c in rec.calls} == {0, 1, 2}, \
        "assist reused a checkpoint it cannot show belongs to this run"
    assert "teks checkpoint" not in res["book"]
    # Every delivered chapter was written by this run, so the census covers the
    # whole book — no chapter reaches the reader unaccounted for.
    assert res["canon_lite_binding"]["chapters_bound"] == 3


def test_assist_writes_no_binding_into_the_chapter_capture_column(monkeypatch):
    """`source_prompt` is a capture column, not binding metadata.

    It is also overwritten with "" by `_persist_chapters` at finalisation, so
    anything written there during the MAP would not survive the job that wrote
    it — a persistence claim that quietly evaporates on success.
    """
    saved = []
    monkeypatch.setitem(sys.modules, "database", _fake_db([], saved))
    monkeypatch.setenv("NARRATION_RESUME_ENABLED", "1")

    res, _ = asyncio.run(_run("assist", n=3, tenant_id=CANARY_TENANT, job_id="j-1"))
    assert res.get("ok"), res
    assert len(saved) == 3, saved
    assert {row["source_prompt"] for row in saved} == {""}, \
        "assist wrote binding metadata into a capture column"


def test_non_assist_resume_is_unchanged(monkeypatch):
    """Off/shadow resume exactly as before — the checkpoint is reused."""
    saved = []
    rows = [{"chapter_index": 0, "content": "teks checkpoint", "source_prompt": ""}]
    monkeypatch.setitem(sys.modules, "database", _fake_db(rows, saved))
    monkeypatch.setenv("NARRATION_RESUME_ENABLED", "1")

    res, rec = asyncio.run(_run("shadow", n=3, tenant_id=CANARY_TENANT, job_id="j-1"))
    assert res.get("ok"), res
    assert 0 not in {c["no"] for c in rec.calls}, \
        "shadow stopped reusing a checkpoint — assist behaviour leaked"
    assert "teks checkpoint" in res["book"]
    assert {row["source_prompt"] for row in saved} == {""}


# ── 9. the REAL worker really does prepend the canon to the system prefix ──
def test_real_write_chapter_prepends_canon_and_hashes_delivered_bytes(monkeypatch):
    """🔴 EVERY OTHER TEST HERE STUBS `_write_chapter`, SO NONE OF THEM PROVES
       THE PREFIX IS ACTUALLY ASSEMBLED. This one calls the real function and
       reads the `Worker.system` string that would have gone to the provider.
    """
    captured = {}

    async def _fake_run_worker(worker, user_turn, **_kw):
        captured["system"] = worker.system
        return {"ok": True, "output": "isi bab", "model": "m"}

    async def _passthrough_gate(res, **_kw):
        return res

    monkeypatch.setattr(st, "run_worker", _fake_run_worker)
    monkeypatch.setattr(st, "_apply_word_gate", _passthrough_gate)

    chapters = _outline(2)
    ctx = SharedContext(topic="topik", chapters=chapters)
    canon_text = "CANON v1\nbab 1: sesuatu\n"
    prompt_sha = cl.sha256_hex(canon_text.encode("utf-8"))

    # 🔴 THE DISPATCHED VALUES FOR THE TWO *MEASURED* FIELDS ARE DELIBERATELY
    #    WRONG. If `_write_chapter` ever copies a parameter instead of
    #    recomputing from what it delivered, the census downstream becomes a
    #    comparison of a value with itself — it would then pass no matter how
    #    badly the prefix or the context had been mangled. Handing it a lie is
    #    the only way to tell measuring apart from echoing.
    res = asyncio.run(st._write_chapter(
        ctx=ctx, ch=chapters[0], no=0, total=2, style="creative non-fiction",
        language="id", mode="text", job_id="j-1", worker_model="m",
        timeout=5.0, telemetry_sink=None,
        canon_text=canon_text, canon_prompt_sha="9" * 64,
        canon_sha256="c" * 64, context_sha256="d" * 64))

    assert res.get("ok"), res
    system = captured.get("system")
    assert system, "the worker was never given a system prefix"
    # On the SYSTEM prefix (cache-stable), at the very front, verbatim.
    assert system.startswith(canon_text), system[:120]
    # The reported hash is the hash of the bytes actually delivered — NOT the
    # value it was handed.
    assert res["canon_prompt_sha256"] == prompt_sha
    assert res["canon_prompt_sha256"] != "9" * 64, \
        "the prompt hash was copied from the parameter, not recomputed"
    assert res["canon_prompt_sha256"] == \
        hashlib.sha256(system[:len(canon_text)].encode("utf-8")).hexdigest()
    # Same for the context digest: measured on the object it actually read.
    assert res["context_sha256_seen"] == cl.context_digest(ctx)
    assert res["context_sha256_seen"] != "d" * 64, \
        "the context digest was copied from the parameter, not recomputed"
    # The dispatched values are still carried through, unaltered, for the census.
    assert res["canon_prompt_expected"] == "9" * 64
    assert res["canon_sha256"] == "c" * 64
    assert res["context_sha256"] == "d" * 64


def test_real_write_chapter_reports_nothing_when_no_canon_is_given(monkeypatch):
    """off/shadow must not gain canon keys on the result dict."""
    async def _fake_run_worker(worker, user_turn, **_kw):
        return {"ok": True, "output": "isi bab", "model": "m"}

    async def _passthrough_gate(res, **_kw):
        return res

    monkeypatch.setattr(st, "run_worker", _fake_run_worker)
    monkeypatch.setattr(st, "_apply_word_gate", _passthrough_gate)

    chapters = _outline(2)
    res = asyncio.run(st._write_chapter(
        ctx=SharedContext(topic="topik", chapters=chapters), ch=chapters[0], no=0,
        total=2, style="creative non-fiction", language="id", mode="text",
        job_id="j-1", worker_model="m", timeout=5.0, telemetry_sink=None))
    for key in ("canon_prompt_sha256", "context_sha256_seen", "canon_sha256",
                "context_sha256"):
        assert key not in res, key


# ── 12. the canon reaches the L3 seam on the REAL path ──────────────────────
@pytest.mark.parametrize("mode", ["shadow", "assist"])
def test_the_real_job_carries_the_canon_to_the_downstream_seam(mode):
    """🔴 EXERCISED THROUGH `narrate_chapters`, NOT BY HANDING THE CANON IN.

    The L3 repair seam reads its canon off this transit key. Forwarding it for
    shadow only left assist receiving `canon=None`, and a canon-less report has no
    semantic authority, no violations and nothing to repair — assist would have
    been a permanent silent no-op in production. Every seam test passed anyway,
    because they all passed the canon directly. Only a test that runs the real
    function can see the difference.
    """
    res, _ = asyncio.run(_run(mode, n=3))
    assert res.get("ok"), res
    assert isinstance(res.get("_canon_lite_canon"), cl.CanonLiteV1), \
        f"mode={mode} produced no canon for the downstream seam"
    assert res.get("_canon_lite_canon_status") == "present"


def test_the_canon_transit_key_never_survives_to_persistence():
    """It is in-process transit. narration_api pops it before the durable payload,
    and that pop must be unconditional — the key exists for two modes now."""
    import narration_api as na
    res, _ = asyncio.run(_run("assist", n=2))
    assert "_canon_lite_canon" in res
    payload = na._result_payload(res)
    assert "_canon_lite_canon" not in payload
    assert "_canon_lite_canon" not in repr(payload)


# ── 13. the tenant allowlist, through the REAL job path ─────────────────────
#
# 🔴 WITHOUT THIS GATE THERE IS NO SUCH THING AS "ONE COHORT". The mode variable
#    is process-wide: setting it to `assist` puts every job the worker picks up on
#    the repair path in one step, including jobs already WAITING in the queue —
#    which the pre-deploy drain gate never covered, because it proves zero ACTIVE.

def test_the_allowlisted_tenant_gets_assist_through_the_real_job():
    res, rec = asyncio.run(_run("assist", n=3, tenant_id=CANARY_TENANT))
    assert res.get("ok"), res
    assert all(c["canon_text"] for c in rec.calls), "the canary got no canon"
    assert res.get("canon_lite_binding", {}).get("mode") == "assist"
    assert isinstance(res.get("_canon_lite_canon"), cl.CanonLiteV1)


@pytest.mark.parametrize("tenant", ["t-someone-else", "", None,
                                    "T-CANARY", " t-canary-2 "])
def test_every_other_tenant_stays_on_the_legacy_path(tenant):
    """🔴 NOT MERELY "no repair" — the LEGACY path, indistinguishable from flag-off.

    A non-canary job under a global `assist` must look exactly like a job on a
    worker that had never heard of L3: no canon injected, no binding on the
    result, no transit key, no L3 payload. Anything less means the blast radius of
    activating one cohort is still the whole fleet, just quieter.
    """
    res, rec = asyncio.run(_run("assist", n=3, tenant_id=tenant))
    assert res.get("ok"), res
    assert rec.calls, "no chapters ran at all"
    assert all(c["canon_text"] is None for c in rec.calls), \
        f"tenant {tenant!r} was injected with a canon"
    assert all(c["canon_prompt_sha"] is None for c in rec.calls)
    assert "canon_lite_binding" not in res
    assert "_canon_lite_canon" not in res
    assert "canon_lite_l3" not in res


def test_a_missing_allowlist_puts_even_the_canary_on_the_legacy_path(monkeypatch):
    """The operator error that would otherwise activate the fleet in one keystroke."""
    monkeypatch.delenv("NARASI_CANON_LITE_ASSIST_TENANTS", raising=False)
    res, rec = asyncio.run(_run("assist", n=3, tenant_id=CANARY_TENANT))
    assert res.get("ok"), res
    assert all(c["canon_text"] is None for c in rec.calls)
    assert "canon_lite_binding" not in res


def test_shadow_is_not_gated_by_the_allowlist(monkeypatch):
    """The allowlist gates `assist` ONLY. Shadow is observational and already runs
    fleet-wide in production; narrowing it here would be an unrequested behaviour
    change to a mode nobody asked to touch."""
    monkeypatch.delenv("NARASI_CANON_LITE_ASSIST_TENANTS", raising=False)
    res, _ = asyncio.run(_run("shadow", n=2, tenant_id="t-anyone"))
    assert res.get("ok"), res
    assert isinstance(res.get("_canon_lite_canon"), cl.CanonLiteV1), \
        "shadow stopped building a canon for a non-allowlisted tenant"
