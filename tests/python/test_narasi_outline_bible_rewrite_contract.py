"""Provider-free witnesses for outline/Bible authority after the chapter MAP."""
from __future__ import annotations

import asyncio
import ast
import hashlib
import inspect
import json
import sys
from types import SimpleNamespace

import laozhang_api as lz
import narration_api as na
from orchestrator import dynamic
from orchestrator import static
from orchestrator.context_builder import SharedContext
from narasi_outline_packet import OUTLINE_PACKET_CONTRACT_VERSION, outline_packet_bundle


def _ctx() -> SharedContext:
    return SharedContext(
        topic="rooftop contract",
        chapters=[
            {"title": "Return", "summary": "Kang Tae-jun returns as Lee Jin-woo."},
            {"title": "Reveal", "summary": "Cha Eun-soo proves the theft in chapter two."},
            {"title": "Board", "summary": "Cha Min-jae faces the final board confrontation."},
        ],
        canonical_facts=(
            "Kang Tae-jun is the protagonist. Lee Jin-woo is only his alias. "
            "Cha Eun-soo is his former lover."
        ),
        facts_are_bible=True,
        style="storytelling",
    )


def _authority() -> str:
    return _ctx().narrative_authority()


def test_authority_packet_binds_the_exact_final_outline_and_bible():
    ctx = _ctx()
    packet = static._build_narrative_authority_packet(ctx)

    assert packet is not None
    assert packet["text"] == ctx.narrative_authority()
    assert packet["outline_sha256"] == hashlib.sha256(
        ctx.outline().encode("utf-8")).hexdigest()
    assert packet["bible_sha256"] == hashlib.sha256(
        ctx.canonical_facts.encode("utf-8")).hexdigest()
    execution = outline_packet_bundle(ctx.chapters)
    assert packet["outline_packet_contract_version"] == OUTLINE_PACKET_CONTRACT_VERSION
    assert packet["outline_packet_bundle_sha256"] == execution["bundle_sha256"]
    assert packet["outline_packets_by_chapter"] == execution["packets_by_chapter"]
    assert packet["outline_packet_sha256_by_chapter"] == execution["packet_sha256_by_chapter"]
    assert packet["text"].index("SUBORDINATE STORY BIBLE") \
        < packet["text"].index("AUTHORITATIVE FULL OUTLINE (highest authority")
    assert packet["text"].rstrip().endswith(ctx.outline())
    assert "execute the outlined beats in their written order" in packet["text"]
    assert "causal, location, and elapsed-time handoff" in packet["text"]
    assert "advance that clock visibly" in packet["text"]
    for chapter in ctx.chapters:
        assert chapter["summary"] in packet["text"]


def test_missing_bible_does_not_ban_facts_explicitly_owned_by_the_outline():
    ctx = _ctx()
    ctx.canonical_facts = ""
    ctx.facts_are_bible = False
    brief = ctx.brief_block()

    assert "explicitly stated in the AUTHORITATIVE FULL OUTLINE remain allowed" in brief
    assert "Outside those outlined facts, state NO additional specific names" in brief


def test_story_bible_locks_names_found_in_outline_even_when_topic_is_generic(monkeypatch):
    ctx = _ctx()
    assert dynamic._outline_name_lock_names(ctx.topic, ctx.chapters) == (
        "Kang Tae-jun", "Lee Jin-woo", "Cha Eun-soo", "Cha Min-jae")
    assert "NAME DIVERSITY applies ONLY" in dynamic._STORY_BIBLE_SYSTEM_FICTION
    assert "never a fresher substitute" in dynamic._STORY_BIBLE_SYSTEM_FICTION

    captured: dict = {}

    async def fake_run_worker(worker, prompt, **kwargs):
        captured["system"] = worker.system
        captured["prompt"] = prompt
        return {
            "ok": True,
            "output": (
                "1. CHARACTERS — Kang Tae-jun / Lee Jin-woo; "
                "Cha Eun-soo; Cha Min-jae."
            ),
            "telemetry": {"finish_reason": "stop"},
        }

    monkeypatch.setenv("NARASI_BIBLE_MODEL", "test-model")
    monkeypatch.setenv("NARASI_BIBLE_FALLBACK_MODEL", "")
    monkeypatch.setenv("NARASI_BIBLE_LAST_RESORT", "0")
    monkeypatch.setattr(dynamic, "run_worker", fake_run_worker)

    bible = asyncio.run(dynamic.build_story_bible(
        ctx.topic, ctx.chapters, is_fiction=True, language="en"))
    assert bible.startswith("1. CHARACTERS — Kang Tae-jun")
    assert "ADDENDUM — OUTLINE NAME LOCK" in captured["system"]
    assert "Kang Tae-jun, Lee Jin-woo, Cha Eun-soo, Cha Min-jae" in captured["system"]


def test_story_bible_rejects_a_name_substituting_attempt_and_fails_over(monkeypatch):
    ctx = _ctx()
    attempted: list[str] = []

    async def fake_run_worker(worker, prompt, **kwargs):
        attempted.append(worker.model)
        output = (
            "1. CHARACTERS — Kim Do-hyun replaces the named protagonist."
            if worker.model == "primary-model" else
            "1. CHARACTERS — Kang Tae-jun / Lee Jin-woo; Cha Eun-soo; Cha Min-jae."
        )
        return {
            "ok": True, "output": output,
            "telemetry": {"finish_reason": "stop"},
        }

    monkeypatch.setenv("NARASI_BIBLE_MODEL", "primary-model")
    monkeypatch.setenv("NARASI_BIBLE_FALLBACK_MODEL", "fallback-model")
    monkeypatch.setenv("NARASI_BIBLE_LAST_RESORT", "0")
    monkeypatch.setattr(dynamic, "run_worker", fake_run_worker)

    bible = asyncio.run(dynamic.build_story_bible(
        ctx.topic, ctx.chapters, is_fiction=True, language="en"))

    assert attempted == ["primary-model", "fallback-model"]
    assert "Kang Tae-jun" in bible
    assert "Kim Do-hyun" not in bible


def test_polish_puts_authority_in_the_system_turn_and_legacy_stays_empty(monkeypatch):
    captured: list[dict] = []

    async def fake_synthesize(task, results, **kwargs):
        captured.append({"task": task, **kwargs})
        return {"ok": True, "output": results[0]["output"], "telemetry": {}}

    monkeypatch.setattr(static, "synthesize", fake_synthesize)
    chapter = "## Chapter 1: Return\n\nKang Tae-jun came home."
    common = dict(
        text=chapter, instruction="Light edit.", role="polish", model="test-model",
        timeout=2, telemetry_sink=None, task_id="polish-witness")

    out, ok = asyncio.run(static._polish_one(
        **common, authority_text=_authority()))
    assert (out, ok) == (chapter, True)
    assert captured[-1]["system"].endswith(_authority())
    assert "immutable" in captured[-1]["system"]

    asyncio.run(static._polish_one(**common))
    assert captured[-1]["system"] == ""


def test_whole_and_chunked_polish_forward_the_same_authority(monkeypatch):
    seen: list[str] = []

    async def fake_polish_one(text, **kwargs):
        seen.append(kwargs.get("authority_text", ""))
        return text, True

    monkeypatch.setattr(static, "_polish_one", fake_polish_one)
    monkeypatch.setattr(static, "max_tokens_for", lambda _model: 100000)
    one = "## Chapter 1: Return\n\n" + "word " * 30 + "end."
    asyncio.run(static._polish_reduce(
        book=one, topic="x", style="storytelling", language="en", polish="light",
        manager_model="test-model", timeout=2, telemetry_sink=None,
        authority_text=_authority()))
    assert seen == [_authority()]

    seen.clear()
    monkeypatch.setattr(static, "max_tokens_for", lambda _model: 100)
    monkeypatch.setenv("NARASI_POLISH_CHUNK", "1")
    monkeypatch.setenv("NARASI_POLISH_CHUNK_WORDS", "60")
    two = (
        "## Chapter 1: Return\n\n" + "alpha " * 75 + "end.\n\n"
        "## Chapter 2: Reveal\n\n" + "beta " * 75 + "end."
    )
    asyncio.run(static._polish_reduce(
        book=two, topic="x", style="storytelling", language="en", polish="light",
        manager_model="test-model", timeout=2, telemetry_sink=None,
        authority_text=_authority()))
    assert seen == [_authority(), _authority()]


def test_narrate_chapters_freezes_one_packet_for_map_reduce_and_downstream(monkeypatch):
    ctx = _ctx()
    captured: dict = {}

    async def fake_write(*, no, **kwargs):
        return {
            "ok": True, "output": f"Chapter body {no + 1}.", "no": no,
            "model": "test-model",
        }

    async def fake_polish_reduce(*, book, authority_text, **kwargs):
        captured["authority_text"] = authority_text
        return book, False

    monkeypatch.setenv("NARASI_STORY_BIBLE", "0")
    monkeypatch.setenv("NARASI_CANON_LITE_MODE", "off")
    monkeypatch.setattr(static, "_write_chapter", fake_write)
    monkeypatch.setattr(static, "_polish_reduce", fake_polish_reduce)

    result = asyncio.run(static.narrate_chapters(
        ctx.topic, ctx.chapters, style="storytelling", language="en",
        polish="light", worker_model="test-model", manager_model="test-model",
        shared_context=ctx, max_parallel=3))

    assert result["ok"] is True
    assert captured["authority_text"] == _authority()
    assert result["_narrative_authority"] \
        == static._build_narrative_authority_packet(ctx)


def test_diet_builder_is_authority_bound_without_changing_legacy_shape():
    legacy = na._diet_rewrite_messages("EDIT\n\nMANUSCRIPT:\ntext", "")
    assert legacy == [{"role": "user", "content": "EDIT\n\nMANUSCRIPT:\ntext"}]

    bound = na._diet_rewrite_messages("EDIT\n\nMANUSCRIPT:\ntext", _authority())
    assert [m["role"] for m in bound] == ["system", "user"]
    assert bound[0]["content"].endswith(_authority())
    assert "move a beat between chapters" in bound[0]["content"]


def test_gate_forwards_the_private_authority_to_consistency_revise(monkeypatch):
    captured: dict = {}
    # Several legacy endpoint tests reload laozhang_api during the full-tree run.
    # Patch the module currently registered for imports, not this file's possibly
    # stale collection-time reference, or _apply_v3_gates will reach the real client.
    live_lz = sys.modules.get("laozhang_api", lz)

    async def critique(*args, **kwargs):
        captured["critic_authority_text"] = kwargs.get("authority_text")
        return ({"violations": [{
            "type": "timeline", "severity": "high", "evidence": '"Alpha"',
            "description": "timeline", "fix": "Correct it.", "chapter": 1,
        }]}, 0)

    async def revise(full_text, *args, **kwargs):
        captured.update(kwargs)
        return full_text, 0

    async def cheap(*args, **kwargs):
        return "", 0

    monkeypatch.setenv("NARASI_DIET_MAX_LOOPS", "0")
    monkeypatch.setattr(live_lz, "_narasi_critique_enabled", lambda: True)
    monkeypatch.setattr(live_lz, "_narasi_critique_revise_enabled", lambda: True)
    monkeypatch.setattr(live_lz, "NARASI_CRITIQUE_MIN_CHAPTERS", 1)
    monkeypatch.setattr(live_lz, "_narasi_consistency_critique", critique)
    monkeypatch.setattr(live_lz, "_narasi_consistency_revise", revise)
    monkeypatch.setattr(live_lz, "_narasi_cheap_call", cheap)

    book = "\n\n".join(
        f"## Chapter {i}: Title\n\nAlpha sentence {i}." for i in range(1, 4))
    packet = static._build_narrative_authority_packet(_ctx())
    result = {
        "ok": True, "book": book,
        "chapters": [{"no": i, "content": f"Alpha sentence {i + 1}."}
                     for i in range(3)],
        "canonical_facts": _ctx().canonical_facts,
        "_narrative_authority": packet,
    }
    body = {
        "style": "storytelling", "language": "en", "model": "test-model",
        "chapters": _ctx().chapters,
    }
    asyncio.run(na._apply_v3_gates(
        result, body, tenant_id="t", user_id="u", job_uuid=None,
        sink=None, job_id="authority-gate"))

    assert captured["critic_authority_text"] == _authority()
    assert captured["authority_text"] == _authority()
    assert captured["outline_packets"] == packet["outline_packets_by_chapter"]


def test_authority_aware_critic_contract_names_the_canary_failure_classes():
    legacy = lz._consistency_critic_sys(
        is_fiction=True, canon_aware=False, epistemic_check=False)
    bound = lz._consistency_critic_sys(
        is_fiction=True, canon_aware=False, epistemic_check=False,
        authority_aware=True)

    for token in (
        "outline_beat_order",
        "outline_missing_beat",
        "chapter_boundary_break",
        "story_clock_progression",
    ):
        assert token not in legacy
        assert token in bound
    assert "include `chapter` as the 1-based chapter number" in bound
    assert "unanswered invitation is not" in bound
    assert "Merely saying 'days passed'" in bound


def test_private_authority_never_enters_the_durable_payload():
    packet = static._build_narrative_authority_packet(_ctx())
    result = {"book": "## Chapter 1\n\nText.", "_narrative_authority": packet}

    assert na._narrative_authority_text(result) == _authority()
    payload = na._result_payload(result)
    assert "_narrative_authority" not in payload
    assert "Kang Tae-jun" not in json.dumps(payload)


def test_structural_patch_summary_persists_only_when_revise_published_it():
    summary = lz._narasi_structural_patch_summary()
    with_summary = na._result_payload({"book": "Text.", "structural_patch": summary})
    without_summary = na._result_payload({"book": "Text."})

    assert with_summary["structural_patch"] == summary
    assert "structural_patch" not in without_summary


def test_classic_job_result_uses_the_same_top_level_structural_summary_path():
    summary = lz._narasi_structural_patch_summary(
        structural_violations=1, targeted=1, attempted=1, accepted=0)
    result = {"markdown": "Text."}
    critique = {"violations": [], "structural_patch": summary}
    lz._narasi_copy_structural_patch_summary(result, critique)
    assert result["structural_patch"] is summary


class _FakeCompletions:
    def __init__(self, output: str, captured: list[dict]):
        self.output = output
        self.captured = captured

    def create(self, **kwargs):
        self.captured.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=self.output), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0),
        )


def _install_revise_client(monkeypatch, output: str, captured: list[dict]):
    completions = _FakeCompletions(output, captured)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(lz, "make_narasi_client", lambda *args, **kwargs: client)

    async def no_usage(*args, **kwargs):
        return 0

    monkeypatch.setattr(lz, "_log_narasi_usage", no_usage)
    monkeypatch.setattr(lz, "_narasi_revise_timeout", lambda *args, **kwargs: 2.0)


def test_whole_book_and_chunked_revise_receive_the_same_authority(monkeypatch):
    monkeypatch.setenv("NARASI_REVISE_CHUNKED", "0")
    monkeypatch.setenv("NARASI_REVISE_CHUNKED_AUTO_WORDS", "0")
    book = (
        "## Chapter 1: Return\nAlpha sentence.\n\n"
        "## Chapter 2: Reveal\nBeta sentence."
    )
    captured: list[dict] = []
    _install_revise_client(monkeypatch, book, captured)
    revised, _ = asyncio.run(lz._narasi_consistency_revise(
        book, {"violations": [{
            "severity": "high", "type": "timeline", "evidence": '"Alpha"',
            "fix": "Correct it.",
        }]}, "storytelling", "en", model="test-model",
        tenant_id="t", user_id="u", job_uuid=None,
        authority_text=_authority()))
    assert revised == book
    assert captured[-1]["messages"][0]["content"].endswith(_authority())

    captured.clear()
    first = "## Chapter 1: Return\nAlpha sentence."
    _install_revise_client(monkeypatch, first, captured)
    monkeypatch.setenv("NARASI_REVISE_PARALLEL", "0")
    revised, _ = asyncio.run(lz._narasi_revise_chunked(
        book, [{
            "severity": "high", "type": "timeline",
            "evidence": '"Alpha sentence."', "fix": "Correct it.",
        }], "storytelling", "en", "test-model",
        tenant_id="t", user_id="u", job_uuid=None,
        authority_text=_authority()))
    assert revised == book
    # The chapter lane no longer ENDS on the authority: the response-shape contract closes
    # the prompt, because leaving the outline as the last thing read produced the inserted
    # bridge alone instead of the chapter (prod pk6ci880: 530/540/828 words -> 48/54/72).
    # The authority must still be present, with NOTHING between it and that closing block.
    _chunk_sys = captured[-1]["messages"][0]["content"]
    assert _authority() in _chunk_sys
    _after_authority = _chunk_sys.split(_authority(), 1)[1]
    assert _after_authority.startswith("\n\nRESPONSE SHAPE")
    assert _after_authority.rstrip().endswith(
        "never a template for your reply's length or register.")


def test_authority_structural_finding_forces_addressed_patch_repair(monkeypatch):
    monkeypatch.setenv("NARASI_REVISE_CHUNKED", "0")
    monkeypatch.setenv("NARASI_REVISE_CHUNKED_AUTO_WORDS", "0")
    captured: dict = {}

    async def fake_patch(full_text, violations, *args, **kwargs):
        captured["full_text"] = full_text
        captured["violations"] = violations
        captured.update(kwargs)
        return full_text, 0, {
            "targeted": 1, "attempted": 0, "accepted": 0,
            "not_attempted_reason_counts": {"outline_packet_missing": 1},
            "owned_chapter_numbers": {2},
        }

    monkeypatch.setattr(lz, "_narasi_structural_patch_revise", fake_patch)
    book = (
        "## Chapter 1: Return\nAlpha sentence.\n\n"
        "## Chapter 2: Reveal\nBeta sentence."
    )
    finding = {
        "severity": "high",
        "type": "outline_beat_order",
        "chapter": 2,
        "evidence": "the rights surrender precedes the family audit",
        "fix": "Restore the chapter-two outline order.",
    }
    revised, credits = asyncio.run(lz._narasi_consistency_revise(
        book, {"violations": [finding]}, "storytelling", "en",
        model="test-model", tenant_id="t", user_id="u", job_uuid=None,
        authority_text=_authority()))

    assert revised == book
    assert credits == 0
    assert captured["violations"] == [finding]
    assert captured["authority_text"] == _authority()


def test_authority_structural_patch_failure_never_falls_back_to_whole_book(monkeypatch):
    monkeypatch.setenv("NARASI_REVISE_CHUNKED", "0")
    monkeypatch.setenv("NARASI_REVISE_CHUNKED_AUTO_WORDS", "0")

    async def broken_patch(*args, **kwargs):
        raise RuntimeError("unsupported heading shape")

    # A RAISING tripwire cannot witness this on its own: the whole-book path wraps its
    # provider call in `except Exception`, and AssertionError is an Exception — so the
    # guard's own alarm is swallowed and that path then returns (full_text, 0), which is
    # byte-identical to the guarded outcome. Both trailing asserts therefore pass with or
    # without the production guard. Record entry in a flag SET BEFORE the raise, which no
    # exception handler can undo.
    entered = {"whole_book": False}

    def forbidden_client(*args, **kwargs):
        entered["whole_book"] = True
        raise AssertionError("authority repair must not enter whole-book rewrite")

    monkeypatch.setattr(lz, "_narasi_structural_patch_revise", broken_patch)
    monkeypatch.setattr(lz, "make_narasi_client", forbidden_client)
    book = (
        "## Chapter 1: Return\nAlpha sentence.\n\n"
        "## Chapter 2: Reveal\nBeta sentence."
    )
    revised, credits = asyncio.run(lz._narasi_consistency_revise(
        book, {"violations": [{
            "severity": "high",
            "type": "chapter_boundary_break",
            "chapter": 2,
            "evidence": "the journey happened off-page",
            "fix": "Add the causal and location bridge.",
        }]}, "storytelling", "en", model="test-model",
        tenant_id="t", user_id="u", job_uuid=None,
        authority_text=_authority()))

    assert entered["whole_book"] is False
    assert revised == book
    assert credits == 0


def test_authority_bound_whole_book_revise_rejects_restructure_and_replacement(monkeypatch):
    monkeypatch.setenv("NARASI_REVISE_CHUNKED", "0")
    monkeypatch.setenv("NARASI_REVISE_CHUNKED_AUTO_WORDS", "0")
    body1 = " ".join(f"alpha{i}" for i in range(70)) + "."
    body2 = " ".join(f"beta{i}" for i in range(70)) + "."
    book = f"## Chapter 1: Return\n{body1}\n\n## Chapter 2: Reveal\n{body2}"
    violation = {"violations": [{
        "severity": "high", "type": "timeline", "evidence": '"alpha1"',
        "fix": "Correct it.",
    }]}

    captured: list[dict] = []
    wrong_heading = book.replace("## Chapter 2: Reveal", "## Chapter 9: Wrong")
    _install_revise_client(monkeypatch, wrong_heading, captured)
    revised, _ = asyncio.run(lz._narasi_consistency_revise(
        book, violation, "storytelling", "en", model="test-model",
        tenant_id="t", user_id="u", job_uuid=None,
        authority_text=_authority()))
    assert revised == book

    replacement = (
        "## Chapter 1: Return\n" + "omega " * 70 + "end.\n\n"
        "## Chapter 2: Reveal\n" + "sigma " * 70 + "end."
    )
    captured.clear()
    _install_revise_client(monkeypatch, replacement, captured)
    revised, _ = asyncio.run(lz._narasi_consistency_revise(
        book, violation, "storytelling", "en", model="test-model",
        tenant_id="t", user_id="u", job_uuid=None,
        authority_text=_authority()))
    assert revised == book


def test_classic_final_revise_is_wired_to_its_exact_outline_authority():
    outline = (
        "1. Return — Kang Tae-jun returns as Lee Jin-woo.\n"
        "2. Reveal — Cha Eun-soo proves the theft."
    )
    brief = "Kang Tae-jun is the protagonist; Lee Jin-woo is only his alias."
    authority = lz._narasi_classic_narrative_authority(outline, brief)

    assert authority.rstrip().endswith(outline)
    assert authority.index("SUBORDINATE NARRATIVE BRIEF") \
        < authority.index("AUTHORITATIVE FULL OUTLINE (highest authority")
    assert lz._narasi_classic_narrative_authority("", brief) == ""
    assert "execute outlined beats in their written order" in authority
    assert "causal/location/time handoff" in authority
    assert "unaccounted-for time jump" in authority

    # AST-scoped call-site witness: the active Classic whole-draft path must pass
    # the frozen authority rather than falling through the helper's default "".
    tree = ast.parse(inspect.getsource(lz._narasi_generate_impl))
    revise_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_narasi_consistency_revise"
    ]
    assert len(revise_calls) == 1
    authority_kw = next(
        (kw.value for kw in revise_calls[0].keywords
         if kw.arg == "authority_text"), None)
    assert isinstance(authority_kw, ast.Name)
    assert authority_kw.id == "_narrative_authority"
    packets_kw = next(
        (kw.value for kw in revise_calls[0].keywords
         if kw.arg == "outline_packets"), None)
    assert isinstance(packets_kw, ast.Name)
    assert packets_kw.id == "_classic_outline_packets"

    critique_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_narasi_consistency_critique"
    ]
    assert len(critique_calls) == 1
    critic_authority_kw = next(
        (kw.value for kw in critique_calls[0].keywords
         if kw.arg == "authority_text"), None)
    assert isinstance(critic_authority_kw, ast.Name)
    assert critic_authority_kw.id == "_narrative_authority"


def test_boundary_detector_and_repair_cover_unbridged_time_progression():
    detector = inspect.getsource(static.narrate_chapters)
    actuator = inspect.getsource(na._r7_actuator_violations)

    assert "skip a required causal/location/time bridge" in detector
    assert "bounded duration/deadline suddenly expires" in detector
    assert "make the causal decision, location" in actuator
    assert "Preserve this " in actuator and "chapter's outlined beat order" in actuator


def test_numeric_ledger_ignores_unit_only_duration_payload():
    malformed = [{
        "name": "co-habitation agreement duration",
        "intentional_contrast": False,
        "values": [
            {"value": "30", "chapter": 1},
            {"value": "hours", "chapter": 3},
        ],
    }]
    assert na._numeric_drifts(malformed) == []

    actual_conflict = [{
        **malformed[0],
        "values": [
            {"value": "30", "chapter": 1},
            {"value": "20", "chapter": 3},
        ],
    }]
    assert len(na._numeric_drifts(actual_conflict)) == 1


def test_classic_final_revise_rejects_same_length_total_replacement(monkeypatch):
    monkeypatch.setenv("NARASI_REVISE_CHUNKED", "0")
    monkeypatch.setenv("NARASI_REVISE_CHUNKED_AUTO_WORDS", "0")
    body1 = " ".join(f"alpha{i}" for i in range(70)) + "."
    body2 = " ".join(f"beta{i}" for i in range(70)) + "."
    book = f"## Chapter 1: Return\n{body1}\n\n## Chapter 2: Reveal\n{body2}"
    replacement = (
        "## Chapter 9: Totally Different\n" + "omega " * 70 + "end.\n\n"
        "## Chapter 10: Also Different\n" + "sigma " * 70 + "end."
    )
    captured: list[dict] = []
    _install_revise_client(monkeypatch, replacement, captured)
    authority = lz._narasi_classic_narrative_authority(
        "1. Return — Kang Tae-jun returns.\n2. Reveal — the theft is proven.",
        "Kang Tae-jun is the protagonist.")

    # Counterfactual witness for the exact production gap Claude found: the old
    # call shape accepted this full swap because its word count cleared the 90% gate.
    unbound, _ = asyncio.run(lz._narasi_consistency_revise(
        book, {"violations": [{
            "severity": "high", "type": "timeline", "evidence": '"alpha1"',
            "fix": "Correct it.",
        }]}, "storytelling", "en", model="test-model",
        tenant_id="t", user_id="u", job_uuid=None))
    assert unbound == replacement

    captured.clear()
    _install_revise_client(monkeypatch, replacement, captured)
    revised, _ = asyncio.run(lz._narasi_consistency_revise(
        book, {"violations": [{
            "severity": "high", "type": "timeline", "evidence": '"alpha1"',
            "fix": "Correct it.",
        }]}, "storytelling", "en", model="test-model",
        tenant_id="t", user_id="u", job_uuid=None,
        authority_text=authority))

    assert revised == book
    assert captured[-1]["messages"][0]["content"].endswith(authority)


def test_chapter_shape_tail_is_inert_without_authority_and_names_the_failure():
    # Legacy bytes must not move: no bound authority -> no tail at all.
    assert lz._revise_chapter_shape_tail("", 100, 140) == ""
    assert lz._revise_chapter_shape_tail("   ", 100, 140) == ""

    tail = lz._revise_chapter_shape_tail(_authority(), 477, 742)
    # Governs FORM only — it must not appear to outrank the narrative authority, or it
    # would contradict _revise_authority_suffix's precedence contract one line above it.
    assert "governs the FORM of your reply only" in tail
    assert "still outranks every content decision" in tail
    # The concrete band travels with it, and the observed failure mode is named outright.
    assert "477-742 words" in tail
    for banned_shape in ("only the bridge or passage you added",
                         "only the sentences you changed",
                         "a diff, a summary, or an outline entry"):
        assert banned_shape in tail


def test_both_chapter_prompt_sites_close_with_the_shape_tail():
    # The parallel and serial chapter lanes build byte-identical system prompts. A rule
    # added to one door and missed at the other is this workstream's most repeated defect,
    # so pin that BOTH append the tail, and that each does so AFTER the authority suffix.
    src = inspect.getsource(lz._narasi_revise_chunked)
    authority_calls = [ln for ln in src.splitlines()
                       if "_sys += _revise_authority_suffix(" in ln]
    shape_calls = [ln for ln in src.splitlines()
                   if "_sys += _revise_chapter_shape_tail(" in ln]
    assert len(authority_calls) == 2
    assert len(shape_calls) == 2
    # Order matters: the shape tail is only the LAST thing read if it follows the authority.
    assert src.index("_revise_authority_suffix(") < src.index("_revise_chapter_shape_tail(")
    for a, s in zip(authority_calls, shape_calls):
        assert a.rstrip().startswith(" " * (len(s) - len(s.lstrip())))
