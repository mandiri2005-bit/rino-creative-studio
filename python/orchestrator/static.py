# -*- coding: utf-8 -*-
"""
Project Dalang — orchestrator.static (WS-6, the *static* strategies).

This is the "dalang" (puppet-master) working from a FIXED plan: the number and
shape of the work units are known up-front, so we fan out a deterministic set of
workers and reduce their output. Two strategies live here:

  * cowork()           — ROLES fan-out for a single brief. One worker per role
                         (researcher / dramatist / fact-checker / stylist / ...),
                         all writing the SAME brief from a different angle, then a
                         manager SYNTHESIZE merge into one piece. Map (parallel) →
                         reduce (synthesize). This is the consolidation of the old
                         `cowork_llm_static.py` prototype.

  * narrate_chapters() — CHAPTERS map-reduce. One worker per chapter, each handed
                         the FULL outline (via the shared context's outline()) as
                         cross-chapter context plus its OWN anti-collision scope.
                         MAP in parallel (asyncio.gather / as_completed), sort by
                         chapter number, then an optional REDUCE polish pass. This
                         is the consolidation of `narrate_consistent.py` +
                         `narration_api.py`.

Everything here is built ON TOP of the WS-1/WS-4/WS-5 primitives — we do NOT
re-implement client/routing/coherence/assembly:

    build_shared_context  (WS-5)  -> ONE RAG retrieval + style guide + facts/scope
    compose               (WS-4)  -> cache-stable system prefix + per-chapter user turn
    Worker / run_worker   (WS-1)  -> never-raise async worker call (retry/backoff/timeout)
    synthesize            (WS-1)  -> manager merge/polish reduce

CONTRACT: nothing in this module raises into the caller. Workers return
never-raise markers ({"ok": False, ...}); a chapter that fails leaves a labelled
placeholder so the book still assembles and the gap is visible.

The per-chapter worker pipeline is EXACTLY (as WS-6 requires):

    build_shared_context(...)  --once per job-->
        for each chapter:  compose(...)  ->  run_worker(...)
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from typing import Any, Optional, Sequence

import chapter_heading_patterns as _chp

from .core import (
    Worker,
    run_worker,
    synthesize,
    route_model,
    WORKER_MODEL,
    MANAGER_MODEL,
    MAX_WORKERS,
    max_tokens_for,
)
from .context_builder import build_shared_context, SharedContext

# Narrative grammar is optional at this orchestration boundary for the same reason
# compose() is soft-imported below: standalone/minimal environments must still be able
# to import and run the degraded direct-prompt path. A missing or partially importable
# pakem cannot invent a contract, so fail closed to the fully legacy empty block.
try:  # pragma: no cover - normal path exercised through narrate_chapters
    from pakem import render_narrative_grammar, resolve_narrative_grammar
except Exception:  # noqa: BLE001 - keep module importable, matching compose fallback
    def resolve_narrative_grammar(_style: Optional[str]):
        return None

    def render_narrative_grammar(_contract: Any) -> str:
        return ""

# compose() lives in the pakem package (WS-4). Soft-import so that if pakem is
# somehow unavailable we still import (the functions then degrade rather than
# crash the module at import time).
try:  # pragma: no cover - exercised indirectly
    from pakem.assembler import compose, Chapter as _Chapter  # type: ignore
    _COMPOSE_OK = True
except Exception:  # noqa: BLE001 - keep module importable
    compose = None  # type: ignore
    _Chapter = None  # type: ignore
    _COMPOSE_OK = False

# narasi_gate (CC v3 R-FG4/5/6): deterministic post-generation gate — resolve/hedge/cut
# [VERIFY] flags, auto-correct known-bad claims, block bracket residue. Soft-import so
# the orchestrator stays importable standalone; gate degrades to identity when absent.
try:  # pragma: no cover
    import narasi_gate as _ngate  # type: ignore
except Exception:  # noqa: BLE001
    _ngate = None  # type: ignore


def _gate(text: str, lang: str = "en") -> str:
    """Per-chapter deterministic gate (identity when narasi_gate absent/disabled).
    vo_strip=False: [ANCHOR]/[BEAT] markers survive HERE so the counters (which run on
    the assembled book, before the terminal pass) can measure the anchor budget — the
    terminal _apply_v3_gates pass does the real marker strip."""
    if _ngate is None or not text:
        return text
    try:
        out, _rep = _ngate.gate_text(text, lang=lang, vo_strip=False)
    except TypeError:  # older gate signature
        out, _rep = _ngate.gate_text(text)
    return out

log = logging.getLogger("orchestrator.static")


def _build_narrative_authority_packet(ctx: SharedContext) -> Optional[dict[str, Any]]:
    """Freeze the exact outline/Bible authority the MAP is about to consume.

    This is in-process transit, not product output.  Hashes make the hand-off
    attributable without logging user prose; ``text`` is forwarded only to later
    text-mutating stages and is removed before persistence by ``narration_api``.
    """
    text = ctx.narrative_authority()
    if not text:
        return None
    outline = ctx.outline()
    bible = str(ctx.canonical_facts or "")
    execution = ctx.outline_packet_bundle()
    return {
        "text": text,
        "outline_sha256": hashlib.sha256(outline.encode("utf-8")).hexdigest(),
        "bible_sha256": hashlib.sha256(bible.encode("utf-8")).hexdigest(),
        "outline_packet_contract_version": str(execution["contract_version"]),
        "outline_packet_bundle_sha256": str(execution["bundle_sha256"]),
        # Private in-process payload.  Later structural repair needs the exact
        # chapter packet the worker saw; the whole authority object is stripped
        # before persistence by narration_api.
        "outline_packets_by_chapter": execution["packets_by_chapter"],
        "outline_packet_sha256_by_chapter": execution["packet_sha256_by_chapter"],
    }


# ===========================================================================
# Default ROLES for cowork() — each role writes the SAME brief from a different
# craft angle. The manager then SYNTHESIZES them into one piece. These mirror the
# intent of the old cowork prototype's fixed role list. Overridable per call.
# ===========================================================================
DEFAULT_COWORK_ROLES: tuple[dict[str, str], ...] = (
    {
        "name": "researcher",
        "system": (
            "You are the RESEARCHER. From the brief, surface the concrete spine: "
            "the verifiable facts, names, dates, numbers, causes and effects that "
            "the piece must rest on. Write a tight, fact-dense draft — accuracy "
            "over flourish. Mark anything you are unsure of with [VERIFY: ...]."
        ),
    },
    {
        "name": "dramatist",
        "system": (
            "You are the DRAMATIST. From the brief, write the same piece for "
            "MAXIMUM narrative pull: a cold open, scene-first staging, tension and "
            "turn, sensory anchors. Honour the facts but make them MOVE. No abstract "
            "throat-clearing — drop the reader into the moment."
        ),
    },
    {
        "name": "stylist",
        "system": (
            "You are the STYLIST. From the brief, write the same piece for VOICE: "
            "rhythm, cadence, fresh imagery, varied sentence length, zero cliché. "
            "Keep the facts; make every sentence earn its place. This draft sets "
            "the register the final piece should hold."
        ),
    },
    {
        "name": "skeptic",
        "system": (
            "You are the SKEPTIC / fact-checker. From the brief, write the same "
            "piece but interrogate every claim: name the counter-view, flag the "
            "overreach, hedge with honest epistemic distance where the evidence is "
            "thin. Truth first; do NOT invent specifics to sound confident."
        ),
    },
)


def _cowork_synthesis_task(brief: str, roles: Sequence[dict]) -> str:
    """The instruction handed to the manager for the cowork SYNTHESIZE reduce.
    Spells out that these parts are ANGLES on one brief, not separate pieces."""
    role_names = ", ".join(str(r.get("name", "writer")) for r in roles)
    return (
        "These parts are different craft angles on ONE brief, written in parallel "
        f"by specialists ({role_names}). Fuse them into a SINGLE finished piece: "
        "take the factual spine from the researcher, the momentum and staging from "
        "the dramatist, the voice and rhythm from the stylist, and the honesty and "
        "counter-views from the skeptic. Resolve every contradiction in favour of "
        "the most accurate claim, keep all concrete facts/names/dates, drop "
        "duplication, and hold one consistent register throughout.\n\n"
        f"THE BRIEF:\n{brief}"
    )


async def cowork(
    brief: str,
    *,
    roles: Optional[Sequence[dict]] = None,
    style: Optional[str] = None,
    language: str = "id",
    polish: str = "synthesize",
    worker_model: Optional[str] = None,
    manager_model: Optional[str] = None,
    worker_timeout: float = 120.0,
    manager_timeout: float = 180.0,
    telemetry_sink: Optional[Any] = None,
    max_workers: int = MAX_WORKERS,
) -> dict[str, Any]:
    """ROLES fan-out for a single brief → SYNTHESIZE merge.

    MAP: one worker per role, all writing the SAME `brief` from their craft angle,
    run IN PARALLEL (asyncio.gather, never-raise via run_worker).
    REDUCE: the manager SYNTHESIZES the role drafts into one finished piece.

    Never raises. Returns:
        {
          "ok": bool,                 # True if synthesis produced text
          "output": str | None,       # the final synthesized piece
          "roles": [ {name, ok, output, model, telemetry}, ... ],
          "model": str,               # manager model used for the reduce
          "strategy": "cowork",
        }
    """
    roles = list(roles or DEFAULT_COWORK_ROLES)
    if max_workers and len(roles) > max_workers:
        roles = roles[:max_workers]  # honour the worker cap

    w_model = worker_model or route_model(role="worker", style=style)
    m_model = manager_model or MANAGER_MODEL

    # --- MAP: one worker per role, in parallel ---------------------------
    async def _run_role(role: dict) -> dict[str, Any]:
        name = str(role.get("name", "writer"))
        worker = Worker(
            name=f"cowork:{name}",
            role="worker",
            phase="worker",
            model=w_model,
            style=style,
            system=str(role.get("system", "")),
            temperature=float(role.get("temperature", 0.7)),
            telemetry_sink=telemetry_sink,
        )
        res = await run_worker(worker, brief, timeout=worker_timeout, task_id=f"cowork:{name}")
        res["role_name"] = name
        return res

    results = await asyncio.gather(*(_run_role(r) for r in roles))
    role_summaries = [
        {
            "name": r.get("role_name", ""),
            "ok": bool(r.get("ok")),
            "output": r.get("output"),
            "model": r.get("model", ""),
            "telemetry": r.get("telemetry", {}),
        }
        for r in results
    ]

    usable = [r for r in results if r.get("ok") and r.get("output")]
    if not usable:
        log.warning("cowork: all %d role workers failed", len(roles))
        return {
            "ok": False, "output": None, "roles": role_summaries,
            "model": m_model, "strategy": "cowork",
            "error": "all_roles_failed",
        }

    # --- REDUCE: manager synthesize merge --------------------------------
    # polish="none" with a single usable role short-circuits the spend.
    if polish == "none" and len(usable) == 1:
        only = usable[0]
        return {
            "ok": True, "output": only["output"], "roles": role_summaries,
            "model": only.get("model", w_model), "strategy": "cowork",
        }

    synth = await synthesize(
        _cowork_synthesis_task(brief, roles),
        usable,
        role="synthesize",
        model=m_model,
        timeout=manager_timeout,
        telemetry_sink=telemetry_sink,
        task_id="cowork:synthesize",
    )
    return {
        "ok": bool(synth.get("ok")),
        "output": synth.get("output"),
        "roles": role_summaries,
        "model": synth.get("model", m_model),
        "strategy": "cowork",
        "error": synth.get("error") if not synth.get("ok") else None,
    }


# ===========================================================================
# narrate_chapters() — the chapter map-reduce. The headline static strategy.
# ===========================================================================
def _placeholder(ch: dict, no: int, reason: str) -> str:
    """A visible, non-crashing placeholder for a chapter whose worker failed.
    Keeps the book assembling and makes the gap auditable (matches the Node path's
    placeholder-on-error behaviour)."""
    title = str(ch.get("title", "") or "").strip() or f"Chapter {no + 1}"
    return f"[CHAPTER {no + 1} — \"{title}\" FAILED TO GENERATE: {reason}. RETRY THIS CHAPTER.]"


# ── CC v3 word-gate for the ⚡ engine (port of the classic Slice-3 gate). ──
# Floor = 0.9×word_target: an undershooting chapter is CONTINUED (never restarted) in
# bounded follow-up calls — the model extends ITS OWN draft from the last sentence, so
# length never comes from splitting (splitting = drift; continuation = drift-free).
# Truncation (finish_reason ∈ length/max_tokens) IS checked separately: it does NOT only
# bite above the 32k-token ceiling — the manager-routed styles (harari/academic/literary)
# run their WORKER on claude-sonnet-4-6, whose real ceiling is 8192 tokens ≈ 5650 words.
# A ~6000-word chapter there returns ~5650 words truncated mid-sentence, CLEARS the
# 0.9×6000=5400 floor, and would ship truncated-but-billed-complete. So we continue on
# truncation regardless of word count. Kill switch: NARASI_WORDGATE=0 (default ON).
_WORDGATE_RETRIES = int(os.environ.get("DALANG_MAX_CHAPTER_RETRIES", "2"))
_TRUNC_REASONS = ("length", "max_tokens", "max_output_tokens", "model_length")
# Deterministic backstop for a provider that mis-reports finish_reason: a polished blob whose
# last non-whitespace char isn't terminal punctuation (optionally a closing quote/bracket after
# one) reads as cut off mid-sentence, same shape as laozhang_api._narasi_revise_chunked's tail
# check on the sibling per-chapter revise pass. Also accepts an em-dash/en-dash/double-hyphen
# ending (audit-caught: a standard fiction device for interrupted dialogue/thought — "Wait, I
# need to tell you—" — was being misread as truncated, identically to genuine mid-word cutoff).
# Round-8 audit (2026-07-18, job funym2wo): guillemet-quoted dialogue («Sudah selesai.») and
# en-dash (–) endings — both established conventions elsewhere in this codebase (narasi_gate.py's
# _DIALOGUE_QUOTE_RX/_POV_DIALOGUE_STRIP_RX, narasi_counters.py's _DN_QUOTE_RX/_GLOSS_RX) — were
# missing from this regex's accepted set, causing false-positive rejections in _apply_word_gate
# (spurious "continue your chapter" retries) and _polish_reduce (a successful polish silently
# discarded). Mirrors the identical fix applied to laozhang_api.py's _REVISE_TAIL_UNTERMINATED_RX.
_TAIL_UNTERMINATED_RX = re.compile(r'(?:[.!?…]|—|–|--)["\'’”»\)\]]*\s*$')
# Ceiling companion to the 0.9x floor above: a chapter running 30%+ OVER word_target is a
# spec-adherence regression (confirmed 2026-07-15: a book shipped at 53,655 words against a
# ~40,000 target). Report-only — see the ceiling check inside _apply_word_gate below. Kept
# deliberately loose (30%) so it fires only on genuinely egregious overshoot, not routine
# variance — do NOT reuse this as the model-facing prompt ceiling (audit-caught 2026-07-16:
# an earlier patch did exactly that, which told the model a LOOSER ceiling than the assembler's
# own pre-existing 1.15x default — the opposite of fixing the overshoot).
_WORDGATE_CEILING_FACTOR = 1.3
# The actual ceiling STATED TO THE MODEL — tighter than the alarm threshold above by design;
# matches pakem.assembler.Chapter's own pre-existing 1.15x default so both code paths (the
# fallback prompt below and the compose() chapter dict) agree on one real ceiling.
_WORDGATE_PROMPT_CEILING_FACTOR = 1.15
# F6 case 5 (BRIEF-FOR-CODEX-2026-08-14-POST-CANARY-V9.md, fecd3dcb...): the ceiling
# check above was report-only ("never fails, never regenerates"). Bounded regeneration
# is now available behind its OWN default-OFF flag -- ADDITIVE, does not touch the
# report-only warning or the undershoot/floor path above. Trims DOWN to the tighter
# 1.15x prompt ceiling (never the loose 1.3x alarm factor -- see that constant's own
# comment on why reusing it here would be the identical mistake caught 2026-07-16).
_WORDGATE_CEILING_RETRIES = int(os.environ.get("NARASI_WORDGATE_CEILING_RETRIES", "1"))


def _wordgate_ceiling_enforce_on() -> bool:
    return str(os.environ.get("NARASI_WORDGATE_CEILING_ENFORCE", "0")).strip().lower() in (
        "1", "true", "yes", "on")


def _res_truncated(r: Any) -> bool:
    """True if a worker result's telemetry says the model hit its output ceiling. Handles
    both OpenAI ('length') and Anthropic/KIE ('max_tokens') stop reasons."""
    if not isinstance(r, dict):
        return False
    fr = str((r.get("telemetry") or {}).get("finish_reason") or "").strip().lower()
    return fr in _TRUNC_REASONS


def _wordgate_on() -> bool:
    return str(os.environ.get("NARASI_WORDGATE", "1")).strip().lower() not in ("0", "false", "no", "off")


def _scaled_timeout(base: float, word_target: int) -> float:
    """Per-chapter LLM timeout scaled to the word target so a big chapter (≤8k words
    admission cap) is not killed by the flat 120s default: est = words×1.45 tok/word ÷
    40 tok/s ×1.3 buffer (8k words → ~380s). Never below `base`; capped by
    NARASI_WORKER_TIMEOUT_MAX (default 900s, matching the classic chapter timeout).

    A19: when the aggregator-failover chain is ARMED, this outer per-chapter wait_for MUST
    outlive the whole chain budget — otherwise a HUNG rung (KIE black-hole; errors advance
    in sub-seconds, a hang does not) is killed by the outer timeout at ~120-380s and the
    chain restarts at rung 1 on retry, so LaoZhang/AtlasCloud are never reached. Floor the
    timeout to chain_budget + buffer so the whole walk can complete inside one attempt."""
    try:
        cap = float(os.environ.get("NARASI_WORKER_TIMEOUT_MAX", "900"))
        est = int(word_target) * 1.45 / 40.0 * 1.3
        t = max(float(base), min(cap, est))
        # NARASI_WORKER_KIE_FIRST (Rino 2026-07-15) puts a THIRD rung (kie) ahead of laozhang/
        # claude for this exact per-chapter call — same cold-hang exposure as the legacy
        # NARASI_FAILOVER_ENABLED chain, so it needs the identical budget floor. Checked
        # independently (not just as an addition to the condition above) since the two flags
        # are unrelated and either can be on alone.
        _failover_armed = (str(os.environ.get("NARASI_FAILOVER_ENABLED", "0")).strip().lower() in ("1", "true", "yes", "on")
                            or str(os.environ.get("NARASI_WORKER_KIE_FIRST", "0")).strip().lower() in ("1", "true", "yes", "on"))
        if _failover_armed:
            budget = float(os.environ.get("NARASI_FAILOVER_CHAIN_BUDGET", "840")) + 90.0
            t = max(t, budget)   # deliberately allowed to exceed `cap` — failover wants the wait
        return t
    except Exception:  # noqa: BLE001
        return float(base)


async def _apply_word_gate(res: dict, *, worker: Any, word_target: int,
                           timeout: float, task_id: str) -> dict:
    """Continue an undershooting chapter (< 0.9×target) up to _WORDGATE_RETRIES rounds.
    The continuation reuses the SAME worker (same cached system prefix: style rules +
    coherence contract) with the full model output ceiling, so a follow-up round can
    never itself be length-starved for targets under the admission cap.

    Also runs a CEILING check on the final word count (independent of the NARASI_WORDGATE
    on/off flag): if the chapter is 30%+ OVER word_target, logs a warning. By default this
    is pure defect-visibility and never mutates output — but behind NARASI_WORDGATE_
    CEILING_ENFORCE=1 (F6, default OFF) it also attempts a bounded condense-and-replace of
    `res["output"]` (see below); with that flag unset, the "never mutates" guarantee still
    holds exactly as before (adversarial-audit finding, 2026-08-14 night: this docstring's
    older unqualified "Never mutates output for this" predates that flag and was stale)."""
    if _wordgate_on() and res.get("ok") and res.get("output"):
        floor = int(int(word_target) * 0.9)
        text = str(res["output"])
        rounds = 0     # total ATTEMPTED rounds -- bounds the loop
        applied = 0     # rounds that actually appended text
        last = res  # the result whose truncation flag we track as the tail grows
        # Continue while the chapter is UNDER the floor, OR the last call was truncated by the
        # model's output ceiling, OR the text's own tail isn't terminated (deterministic
        # backstop for a provider that mis-reports finish_reason — audit-caught: this is the
        # ONLY completeness check on the original per-chapter generation path; the chunked-
        # revise and polish-reduce passes downstream already had it, generation itself did not).
        # A truncated chapter above the floor would otherwise ship mid-sentence. Bounded by
        # _WORDGATE_RETRIES either way.
        while (len(text.split()) < floor or _res_truncated(last)
               or not _TAIL_UNTERMINATED_RX.search(text)) and rounds < _WORDGATE_RETRIES:
            rounds += 1
            need = max(50, floor - len(text.split()))
            cont_task = (
                "You are continuing YOUR OWN chapter draft. The chapter so far:\n\n---\n"
                + text +
                "\n---\n\nCONTINUE the chapter EXACTLY from its last sentence — do NOT repeat or "
                "summarize anything already written, do NOT restart, do NOT add a new heading or "
                f"closing recap. Add at least {need} words, deepening the scene or argument already "
                "in progress, in the same language and register. Return ONLY the continuation text."
            )
            cres = await run_worker(worker, cont_task, timeout=timeout, task_id=f"{task_id}:cont{rounds}")
            add = (cres.get("output") or "").strip() if isinstance(cres, dict) else ""
            if not add:
                break
            text = text.rstrip() + "\n\n" + add
            last = cres
            applied += 1
        if applied:
            # applied, not rounds -- adversarial-audit follow-up (2026-08-14 night,
            # task_8ff0d0dc): a round that was ATTEMPTED but produced no usable
            # output (and broke the loop) must not inflate the reported count of
            # rounds that actually appended text. Same pattern as the sibling
            # trimmed/_trim_applied fix a few lines below in this same file.
            log.info("%s: word-gate continued %d round(s) → %d words (target %d, truncated_tail=%s)",
                     task_id, applied, len(text.split()), word_target, _res_truncated(last))
            res = {**res, "output": text, "continued": applied}

    try:
        if res.get("ok") and res.get("output"):
            _final_wc = len(str(res["output"]).split())
            _ceiling = int(int(word_target) * _WORDGATE_CEILING_FACTOR)
            if word_target and _final_wc > _ceiling:
                log.warning(
                    "%s: chapter word count %d exceeds ceiling %d (target %d words, +%.0f%% over target)",
                    task_id, _final_wc, _ceiling, word_target,
                    ((_final_wc / word_target) - 1.0) * 100.0)
                if _wordgate_ceiling_enforce_on():
                    _trim_original = str(res["output"])
                    _trim_text = _trim_original
                    _trim_floor = int(int(word_target) * 0.9)
                    _trim_target = int(int(word_target) * _WORDGATE_PROMPT_CEILING_FACTOR)
                    _trim_rounds = 0    # total ATTEMPTED rounds -- bounds the loop
                    _trim_applied = 0   # rounds that actually changed _trim_text
                    while (len(_trim_text.split()) > _trim_target
                           and _trim_rounds < _WORDGATE_CEILING_RETRIES):
                        _trim_rounds += 1
                        _excess = len(_trim_text.split()) - _trim_target
                        _trim_task = (
                            "You are condensing YOUR OWN chapter draft, which overshot its "
                            "target length. The chapter so far:\n\n---\n" + _trim_text +
                            "\n---\n\nReturn the SAME chapter, condensed by roughly "
                            f"{_excess} words -- tighten prose, cut redundant description or "
                            "repeated beats, but keep every plot event, all dialogue content, "
                            "and the chapter's heading and ending intact. Do NOT summarize "
                            "away any story beat. Return ONLY the condensed chapter, "
                            "complete, beginning at its heading line."
                        )
                        _tres = await run_worker(worker, _trim_task, timeout=timeout,
                                                 task_id=f"{task_id}:trim{_trim_rounds}")
                        _tout = (_tres.get("output") or "").strip() if isinstance(_tres, dict) else ""
                        if not _tout or _res_truncated(_tres):
                            log.warning(
                                "%s: word-ceiling trim round %d produced no usable output "
                                "(empty=%s truncated=%s) -- keeping the prior draft",
                                task_id, _trim_rounds, not _tout, _res_truncated(_tres))
                            break
                        if len(_tout.split()) < _trim_floor:
                            log.warning(
                                "%s: word-ceiling trim round %d undershot the floor (%d < %d) "
                                "-- discarding, keeping the prior draft",
                                task_id, _trim_rounds, len(_tout.split()), _trim_floor)
                            break
                        _trim_text = _tout
                        _trim_applied += 1
                    if _trim_text != _trim_original:
                        # _trim_applied, not _trim_rounds -- adversarial-audit finding
                        # (2026-08-14 night): a round that was ATTEMPTED but then
                        # discarded (empty/truncated/undershot-floor) must not inflate
                        # the reported count of rounds that actually changed the text.
                        log.info(
                            "%s: word-ceiling trimmed %d round(s) -> %d words "
                            "(target %d, ceiling %d)",
                            task_id, _trim_applied, len(_trim_text.split()), word_target, _ceiling)
                        res = {**res, "output": _trim_text, "trimmed": _trim_applied}
    except Exception as _wce:  # noqa: BLE001
        log.warning("%s: word-ceiling check failed (non-fatal): %s", task_id, _wce)

    return res


# ── Deterministic leak-scrub for outline-planning residue in generated prose. ──
# Confirmed defect (2026-07-15): a chapter shipped with literal "Scene N--..." planning
# lines embedded in the prose, plus a stray trailing "#" left after the real closing
# line. Both are pure regex/string ops on the chapter text — no LLM call, no flag (a
# defect scrub that never fires on clean output is safe to always run).
_SCENE_LEAK_RE = re.compile(r'^[ \t]*Scenes?[ \t]+\d+[ \t]*(?:—|-{1,2})[ \t]*.*$')
_TRAILING_HASH_RE = re.compile(r'(?:^|\s)#$')


def _scrub_chapter_leaks(text: str, *, task_id: str) -> str:
    """Always trims trailing whitespace from one chapter's generated text;
    additionally strips leaked outline-planning lines ('Scene 2--...') and a bare
    trailing hash if present. NOT byte-identical on clean input that merely has
    trailing whitespace/newlines — those are always stripped regardless of whether
    either leak pattern matched."""
    if not text:
        return text
    kept = []
    for ln in text.split("\n"):
        if _SCENE_LEAK_RE.match(ln):
            log.warning("%s: scrubbed leaked outline-planning line: %r", task_id, ln)
            continue
        kept.append(ln)
    scrubbed = "\n".join(kept).rstrip()
    if _TRAILING_HASH_RE.search(scrubbed):
        scrubbed = _TRAILING_HASH_RE.sub("", scrubbed).rstrip()
    return scrubbed


def _scrub_generation_markers(text: str, canon: Optional[Any], *,
                              task_id: str) -> tuple[str, int]:
    """F1 (BRIEF-FOR-CODEX-2026-08-14-POST-CANARY-V9.md), applied at its EARLIEST
    possible point (2026-08-15 re-audit REJECT finding): the instant a chapter
    worker's raw text exists, before it enters assembly, L2 extraction, a checkpoint,
    or L3. `render_canon_for_generation` should mean a worker never SEES an opaque
    canon id to begin with — this is the backstop for a provider that echoes
    structure it was never shown. Distinct from `_scrub_chapter_leaks` above (an
    unrelated, older defect class — leaked outline-planning residue, not canon ids).

    🔴 SCRUBS ONLY WHAT WAS ACTUALLY INJECTED — the caller passes `canon` only when it
       also injected that canon into this worker's prompt. The first version of this
       fix scrubbed whenever a canon merely EXISTED, which under `shadow` (where a
       canon is built for the projection report but nothing is ever injected) silently
       rewrote user-visible output. Shadow's one architectural promise is that it may
       never change what the user gets, and a bracket colliding with an id in text
       that was never shown the canon is a coincidence, not a leak. The call site
       gates on the mode; this signature makes a future caller unable to reintroduce
       the same bug by passing a canon it did not inject.

    Returns `(text, removed_count)` — the count is aggregated per job by
    `narrate_chapters` so the final telemetry reflects removals at EVERY seam, not
    just the last one. Never raises, matching every other best-effort scrub here."""
    if not text or canon is None:
        return text, 0
    try:
        import canon_lite as _cl
        scrubbed, n = _cl.scrub_bound_markers(text, canon)
        if n:
            log.warning("%s: scrubbed %d bound marker(s) from raw generation output",
                        task_id, n)
        return scrubbed, n
    except Exception as _se:  # noqa: BLE001 - a scrub bug must never break generation
        log.warning("%s: generation marker scrub failed (non-fatal): %s", task_id, _se)
        return text, 0


async def _write_chapter(
    *,
    ctx: SharedContext,
    ch: dict,
    no: int,
    total: int,
    style: Optional[str],
    language: str,
    mode: str,
    job_id: str,
    worker_model: str,
    timeout: float,
    telemetry_sink: Optional[Any],
    canon: Optional[Any] = None,
    canon_text: Optional[str] = None,
    canon_prompt_sha: Optional[str] = None,
    canon_sha256: Optional[str] = None,
    context_sha256: Optional[str] = None,
) -> dict[str, Any]:
    """Generate ONE chapter via the required pipeline:

        build_shared_context (already done once, passed in as ctx)
          -> compose()      (cache-stable prefix + this chapter's user turn)
          -> run_worker()   (never-raise)

    Returns a dict tagged with `no` so the MAP can be sorted back into book order.
    """
    # F1 (2026-08-15 re-audit round 2): scrub ONLY what this worker was actually shown.
    # `canon_text` is the injected prefix and exists under assist alone; shadow builds a
    # canon for its own projection report and injects nothing, and the no-compose
    # fallback path below injects nothing either. Deriving the scrub's canon from the
    # INJECTION rather than from "a canon object was passed" is what keeps shadow
    # byte-identical no matter what a caller hands in.
    _scrub_canon = canon if canon_text else None
    word_target = int(ch.get("word_target", ch.get("words", 800)) or 800)
    # Firm ceiling companion to word_target — stated in the prompt itself so the model
    # treats the target as a ceiling too, not just a floor (2026-07-15: a book shipped
    # at 53,655 words against a ~40,000 target). Deliberately tighter than the separate
    # _WORDGATE_CEILING_FACTOR alarm threshold used below — see that constant's comment.
    word_max = int(word_target * _WORDGATE_PROMPT_CEILING_FACTOR)
    # CC v3: scale the per-chapter timeout to the target so big chapters (≤8k words)
    # aren't killed by the flat default while small ones keep the tight bound.
    timeout = _scaled_timeout(timeout, word_target)
    _outline_execution = ctx.outline_packet_bundle()
    _outline_packet_key = str(no + 1)

    def _bind_outline_packet_identity(result: dict[str, Any]) -> None:
        # Bounded attribution only; exact packet prose stays in the private authority
        # envelope and dynamic user turn.
        result["outline_packet_contract_version"] = _outline_execution["contract_version"]
        result["outline_packet_sha256"] = _outline_execution["packet_sha256_by_chapter"][_outline_packet_key]
        result["outline_packet_bundle_sha256"] = _outline_execution["bundle_sha256"]

    if not _COMPOSE_OK or compose is None:
        # Assembler unavailable: degrade to a minimal direct prompt so the
        # strategy still functions (never crash the job).
        prompt = (
            f"{ctx.brief_block()}\n\n"
            "AUTHORITATIVE FULL OUTLINE (immutable; this wins every conflict):\n"
            f"{ctx.outline()}\n\n"
            f"THIS CHAPTER: write chapter {no + 1} of {total}: \"{ch.get('title','')}\". "
            f"Target ~{word_target} words. Do not exceed {word_max} words. "
            f"\n\nCHAPTER CONTINUITY CONTRACT:\n{ctx.scope_for(no)}\n\n"
            "RESPONSE SHAPE: Return ONLY the complete chapter body; do not include "
            "the chapter title or number, notes, analysis, an outline, or a fragment."
        )
        worker = Worker(
            name=f"ch{no + 1}", role="worker", phase="worker", model=worker_model,
            style=style, telemetry_sink=telemetry_sink,
        )
        res = await run_worker(worker, prompt, timeout=timeout, task_id=f"ch{no + 1}")
        res = await _apply_word_gate(res, worker=worker, word_target=word_target,
                                     timeout=timeout, task_id=f"ch{no + 1}")
        if res.get("output"):
            res["output"] = _scrub_chapter_leaks(res["output"], task_id=f"ch{no + 1}")
            res["output"], _n_scrubbed = _scrub_generation_markers(
                res["output"], _scrub_canon, task_id=f"ch{no + 1}")
            res["f1_markers_removed"] = _n_scrubbed
        res["no"] = no
        _bind_outline_packet_identity(res)
        return res

    # The whole point of WS-4/WS-5: outline + facts + style ride the CACHED prefix;
    # the per-chapter continuity contract + RAG passages land in the variable user turn.
    composed = compose(
        style=style or "creative non-fiction",
        language=language,
        mode=mode,
        outline=ctx.outline(),            # FULL outline — same for every chapter (cached)
        brief=ctx.brief_block(),          # facts + style guide + coherence RULES (cached)
        chapter={
            "id": str(ch.get("id", no + 1)),
            "title": str(ch.get("title", "") or ""),
            "summary": str(ch.get("summary", ch.get("description", "")) or ""),
            "index": no,
            "total": total,
            "word_target": word_target,
            # Propagate the same 1.3x ceiling used in the fallback prompt + the
            # _apply_word_gate WARN check — omitting these left Chapter.__post_init__
            # silently defaulting to its own unrelated 0.85x/1.15x figures, so the
            # "Do not exceed" instruction never reached the model on this (primary,
            # production) path. Audit-confirmed dead-code fix, 2026-07-16.
            "word_min": int(word_target * 0.9),
            "word_max": word_max,
        },
        chapter_scope=ctx.scope_for(no), # past/current/future contract (NOT cached)
        rag_passages=ctx.passages,        # retrieved ONCE in build_shared_context, reused
        job_id=job_id,
        model=worker_model,
    )

    # 🔴 THE CANON GOES ON THE CACHE-STABLE SYSTEM PREFIX, NOT THE USER TURN.
    #    Every chapter's system message then begins with the SAME bytes, which is
    #    both the contract ("one byte-identical mini-canon into every chapter
    #    worker") and what keeps prompt caching intact — a per-chapter prefix
    #    would defeat the cache and quietly multiply cost. The block is prepended
    #    verbatim: rendered once upstream, never rebuilt here, so no worker can
    #    produce a variant of it.
    _system = composed.messages[0]["content"]
    if canon_text:
        _system = f"{canon_text}\n{_system}"

    worker = Worker(
        name=f"ch{no + 1}",
        role="worker",
        phase="worker",
        model=worker_model,
        style=style,
        system=_system,
        telemetry_sink=telemetry_sink,
    )
    res = await run_worker(
        worker,
        composed.messages[1]["content"],
        timeout=timeout,
        task_id=f"ch{no + 1}",
    )
    res = await _apply_word_gate(res, worker=worker, word_target=word_target,
                                 timeout=timeout, task_id=f"ch{no + 1}")
    if res.get("output"):
        res["output"] = _scrub_chapter_leaks(res["output"], task_id=f"ch{no + 1}")
        res["output"], _n_scrubbed = _scrub_generation_markers(
            res["output"], _scrub_canon, task_id=f"ch{no + 1}")
        # Per-worker count, carried on this worker's OWN result dict. Deliberately not
        # a shared counter the fan-out mutates concurrently: `narrate_chapters` sums
        # these after the MAP, so the arithmetic never depends on scheduling order.
        res["f1_markers_removed"] = _n_scrubbed
    res["no"] = no
    res["cache_key"] = composed.cache_key
    _bind_outline_packet_identity(res)
    if canon_text:
        import canon_lite as _cl_h
        # 🔴 THE TWO MEASURED VALUES — recomputed here from what this worker was
        #    ACTUALLY handed, never copied from a parameter. Copying would make
        #    the downstream census a tautology: it would compare the dispatch
        #    value with itself and pass however badly the prefix had been mangled
        #    or the context moved.
        # 2026-08-15 re-audit REJECT finding: this field used to be named
        # `canon_prompt_sha256` — the SAME name the legacy meaning "hash of
        # render_canon()'s own (id-bearing, QC/L3-only) output" would suggest, even
        # though this hashes the GENERATION projection prefix instead
        # (`render_canon_for_generation`'s output). Renamed to say so explicitly;
        # `_seen` matches the sibling `context_sha256_seen` field two lines down —
        # both are "what THIS worker measured", not what was dispatched.
        res["canon_generation_prompt_sha256_seen"] = _cl_h.sha256_hex(
            _system[:len(canon_text)].encode("utf-8"))
        # The context digest AS THIS WORKER SAW IT. The post-MAP freeze verify()
        # compares start against end and so cannot see a mutation that was made
        # and undone while the fan-out was in flight; this can, because it is
        # sampled inside the window, once per chapter.
        res["context_sha256_seen"] = _cl_h.context_digest(ctx)
        # The three DISPATCHED values, carried so the census can prove the whole
        # binding was complete and identical for every chapter, and so a
        # checkpoint can be written against the binding it was really produced
        # under. These are echoes, not evidence — the two above are the evidence.
        res["canon_generation_prompt_expected"] = canon_prompt_sha
        res["canon_sha256"] = canon_sha256
        res["context_sha256"] = context_sha256
    return res


def _f5_unowned_ledger_terms(terms, *, topic: str = "", outline: str = "") -> list:
    """F5: keep only the lane-ledger terms that accepted authority does NOT own.

    🔴 THE SIGNATURE IS THE GUARANTEE. It takes the user's topic and the ACCEPTED
    outline — and deliberately nothing else. At Bible-pin the candidate bible has not
    been accepted yet; passing it in would make every term it just invented self-exempt
    and switch this enforcement off entirely. There is no `bible` parameter to pass by
    accident.

    Extracted as a named helper so the decision is reachable from a test without
    driving the whole bible-build path: the first version of this filter lived inline,
    and the end-to-end test written for it never reached the branch at all — it asserted
    "no enforcement call" in a run where enforcement never ran for an unrelated reason,
    and a mutation run caught it surviving.

    Canary v9 is the case: the accepted outline establishes `Tae-jun`, the bible
    correctly adopts it, and the lane ledger flags it only because OTHER stories in this
    lane overused it. Ordering a rewrite there corrupts this story's own canon."""
    from narasi_counters import (authority_owns_term as _owns,
                                 build_authority_ownership as _build)
    ownership = _build(topic=str(topic or ""), outline=str(outline or ""), bible="")
    kept = []
    for raw in (terms or []):
        if not raw:
            continue
        # `category:value` arrives intact; both halves reach the decision, and the
        # BARE value is what enforcement quotes back downstream.
        category, _, rest = str(raw).partition(":")
        value = rest if rest else str(raw)
        category = category.strip().lower() if rest else ""
        if not _owns(value, ownership, category=category):
            kept.append(value)
    return kept


def _is_fiction_style(style: Optional[str]) -> bool:
    """True ONLY for FICTION styles — the sole regime that should receive an INVENTED story
    bible. Nonfiction/history (natgeo, journalistic, true_crime, babad, sejarah) ground on
    RETRIEVED facts and must NEVER have facts fabricated, so they are excluded. Checks both
    the is_fiction flag and a fictional factual_regime ('fiction'/'fictional'). Note: the P1
    fiction specs (kdrama_serial/romance_contemporary/coming_of_age) only resolve to their
    real entry when DALANG_INFRA_FIXES=1 merges them; without that flag they fuzzy-fall to a
    nonfiction entry and this returns False — so the bible stays OFF, which is fail-safe.
    Soft: any import/lookup failure ⟹ False."""
    if not style:
        return False
    try:
        from pakem import resolve_style as _rs  # type: ignore
        entry = _rs(style) or {}
        if entry.get("is_fiction") is True:
            return True
        return str(entry.get("factual_regime") or "").strip().lower() in ("fiction", "fictional")
    except Exception:  # noqa: BLE001
        return False


# ===========================================================================
# C10 — no orphan chapter task may outlive the MAP.
# ===========================================================================
# Canon Lite P0 measured TWO child tasks surviving an outer cancellation: the
# `as_completed` loop below re-raised CancelledError and the remaining futures kept
# running, free to call a provider and write evidence after the job had already
# terminalized. C10 ("all tasks are awaited or drained before terminalization; no
# orphan provider work may mutate evidence later") forbids exactly that.
_MAP_DRAIN_TIMEOUT_S = 30.0


async def _drain_chapter_tasks(tasks: Sequence["asyncio.Future"], *, why: str) -> int:
    """Cancel every still-running chapter task and AWAIT it before leaving the MAP.

    The drain must survive OUR OWN cancellation. Once a cancel has been delivered to
    this coroutine, the next `await` can raise immediately — so a single
    `await asyncio.wait(...)` would abandon the very children it had just cancelled,
    which is the leak this closes. We therefore retry cancel + join until the children
    are genuinely finished. `_MAP_DRAIN_TIMEOUT_S` is a REPORTING/RE-CANCEL interval,
    never permission to return while a physical child remains alive.

    Returns the number of tasks that were in flight and are now finished. On the normal
    path nothing is pending and this is a no-op.
    """
    def _retrieve(done_tasks) -> None:
        for task in done_tasks:
            if task.cancelled():
                continue
            try:
                task.exception()
            except Exception:  # noqa: BLE001 - retrieval only
                pass

    # A child may have completed between the last `as_completed` yield and the outer
    # cancellation. It is no longer "pending", but its exception must still be
    # retrieved for C10's all-awaited-or-drained guarantee.
    _retrieve(t for t in tasks if t.done())
    original = {t for t in tasks if not t.done()}
    if not original:
        return 0
    pending = set(original)
    drain_was_cancelled = False
    while pending:
        for task in pending:
            task.cancel()
        try:
            done, pending = await asyncio.wait(
                pending, timeout=max(0.01, float(_MAP_DRAIN_TIMEOUT_S)))
        except asyncio.CancelledError:
            # Preserve our caller's cancellation, but only re-raise after every physical
            # child is gone. A repeated cancel merely causes another cancel+join round.
            drain_was_cancelled = True
            continue
        # Retrieve each finished child's exception so a chapter that died on its way
        # out doesn't surface later as an "exception was never retrieved" warning.
        _retrieve(done)
        if pending:
            log.error(
                "narrate_chapters: %d chapter task(s) still alive after %.0fs; "
                "continuing mandatory drain (%s)",
                len(pending), _MAP_DRAIN_TIMEOUT_S, why)
    log.info("narrate_chapters: drained %d in-flight chapter task(s) (%s)",
             len(original), why)
    if drain_was_cancelled:
        raise asyncio.CancelledError
    return len(original)


async def narrate_chapters(
    topic: str,
    chapters: Sequence[dict],
    *,
    style: Optional[str] = None,
    language: str = "id",
    mode: str = "text",
    tenant_id: Optional[str] = None,
    job_id: str = "",
    polish: str = "light",
    worker_model: Optional[str] = None,
    manager_model: Optional[str] = None,
    worker_timeout: float = 120.0,
    manager_timeout: float = 240.0,
    telemetry_sink: Optional[Any] = None,
    max_parallel: int = MAX_WORKERS,
    shared_context: Optional[SharedContext] = None,
    assist_activation_ready: Optional[bool] = None,
) -> dict[str, Any]:
    """Map-reduce a book: ONE worker per chapter, FULL outline as context.

    Pipeline (WS-6 contract):
      1. build_shared_context(topic, chapters, ...) ONCE  (one RAG retrieval).
      2. MAP — fan out one `_write_chapter` per chapter IN PARALLEL
         (asyncio.gather over a bounded semaphore; results consumed as they
         complete, then SORTED BY CHAPTER NUMBER).
      3. REDUCE — an optional manager POLISH pass over the assembled book
         (mode set by `polish` ∈ {none, light, heavy}).

    Never raises. A failed chapter yields a labelled placeholder (the book still
    assembles). Returns:
        {
          "ok": bool,
          "chapters": [ {no, id, title, content, ok, model, error}, ... ],  # book order
          "book": str,                 # chapters joined (polished if polish != none)
          "polished": bool,
          "rag_used": bool,
          "context": {...},            # ctx.as_dict() telemetry
          "strategy": "narrate_chapters",
        }
    """
    chapters = list(chapters or [])
    total = len(chapters)
    if total == 0:
        return {
            "ok": False, "chapters": [], "book": "", "polished": False,
            "rag_used": False, "context": {}, "strategy": "narrate_chapters",
            "error": "no_chapters",
        }

    # Resolve the mode before routing, RAG, Story Bible, or any other physical work.
    # C11 requires the flag-off path to import/call nothing from Canon Lite, hence this
    # literal closed gate rather than `canon_lite.resolve_mode()`.
    _cl_mode = str(os.environ.get("NARASI_CANON_LITE_MODE", "") or "").strip().lower()
    if _cl_mode not in ("shadow", "assist", "enforce"):
        _cl_mode = "off"
    if _cl_mode == "assist":
        # 🔴 ASSIST IS PER-TENANT. The variable is process-wide, so without this a
        #    flip to `assist` puts every job this worker touches on the repair path
        #    at once — queued ones included, which the drain gate never covered.
        #    An empty allowlist admits nobody, so the flag alone is a no-op.
        _cl_tid = str(tenant_id or "").strip()
        _cl_allow = {t.strip() for t in str(
            os.environ.get("NARASI_CANON_LITE_ASSIST_TENANTS", "") or "").split(",")
            if t.strip()}
        if not _cl_tid or _cl_tid not in _cl_allow:
            _cl_mode = "off"
        # ── ACTIVATION READINESS — the server's decision, and it must be PRESENT ────
        #
        # 🔴 FAIL CLOSED: only the exact object `True` keeps assist armed. Absent, None,
        #    a string, a typo, a truthy-looking payload value — every one of them means
        #    "no server decision reached this call", and a job that cannot prove it was
        #    cleared does not get to run assist. An earlier draft asked the opposite
        #    question (`== "off"`), which meant a decision that was never threaded — a
        #    renamed parameter, a dropped call-site argument, a caller that predates the
        #    preflight — read as consent. That is fail OPEN, and it fails open in exactly
        #    the situation this gate exists for: the wiring being wrong.
        #
        #    Still not an ENABLER. Raising `off` to `assist` remains impossible: the env
        #    gate above must independently say `assist` AND the tenant must be in the
        #    allowlist. `True` can only preserve what the environment already permits.
        #
        #    It exists because the env gate above answers "is this tenant in the cohort",
        #    which production job s0di2o1g proved is NOT the same question as "can assist
        #    actually run". That job passed this gate, armed injection, logged
        #    `assist injection armed` and an `assist census PASS` — and checked nothing,
        #    because the QC credential was absent. Arming injection on a deployment that
        #    cannot run a metered wave produces a canary that looks green.
        #
        # ⚠️ It arrives as an INTERNAL keyword-only argument, never off the request dict.
        #    A server decision parked on `req` looks like a payload field, and the next
        #    caller to build a request by hand would have silently omitted it.
        #
        #    The inline computation above is deliberately NOT replaced by
        #    canon_lite.resolve_effective_mode: C11 requires the flag-off path to import and
        #    call nothing from Canon Lite, and that constraint is why this duplication is
        #    sanctioned. The decision is threaded in as a plain bool for the same reason.
        if _cl_mode == "assist" and assist_activation_ready is not True:
            _cl_mode = "off"
    if _cl_mode == "enforce":
        # L3-ASSIST implements `assist`. `enforce` is a SEPARATE project, deferred
        # until L1/L2/L3 are live — so it keeps L1's refusal rather than silently
        # degrading to assist. Quietly running a weaker mode than the operator
        # configured spends money under guarantees that do not exist.
        log.error("canon lite: mode=enforce is unavailable in L3-ASSIST; job refused before work")
        return {
            "ok": False, "chapters": [], "book": "", "polished": False,
            "rag_used": False, "context": {}, "strategy": "narrate_chapters",
            "error": "canon_lite_mode_unavailable_enforce",
        }

    w_model = worker_model or route_model(role="worker", style=style)
    m_model = manager_model or MANAGER_MODEL
    # MODEL-TRACE (Rino: diagnose WORKER_MODEL vs NARASI_DEFAULT_MODEL). Which model the MAP
    # (per-chapter worker) will use, and where it came from: the explicit worker_model arg, the
    # WORKER_MODEL env, or route_model's per-style hint. Manager (polish/merge) model too.
    log.info("[narasi-model] PHASE=map worker=%s manager=%s | src: worker_model_arg=%r "
             "WORKER_MODEL_env=%r style=%r route_model=%s", w_model, m_model, worker_model,
             os.environ.get("WORKER_MODEL"), style, route_model(role="worker", style=style))

    # 1) ONE shared context for the whole job (one RAG retrieval, reused).
    ctx = shared_context or await build_shared_context(
        topic, chapters, tenant_id, style=style,
    )
    effective_style = style if style is not None else ctx.style
    ctx.style = effective_style
    ctx.narrative_grammar_block = render_narrative_grammar(
        resolve_narrative_grammar(effective_style)
    )

    # 1.5) STORY BIBLE — for EVERY multi-chapter book (Rino: "semua style WAJIB pake brief"),
    #   default ON (NARASI_STORY_BIBLE=0 disables). Before the parallel MAP, pin the piece's
    #   load-bearing specifics — names+ages, the central subject's fixed identity + counts, named
    #   locations, timeline, POV, the key reveal — in ONE manager call, and ride them into every
    #   worker via ctx.canonical_facts (cached prefix). Root-cause PREVENTION for the cross-chapter
    #   drift (the Beekeeper name-collapse Hartono→Suranto→Wiro; the star that is 3 objects) that
    #   the #53 critic otherwise only catches after generation. REGIME-AWARE: fiction DECIDES the
    #   invented specifics (facts_are_bible=True ⟹ "obey/invent-freely" framing); nonfiction/
    #   history PINS only what the premise gives and marks unknowns [VERIFY] — NEVER fabricates
    #   (facts_are_bible stays False ⟹ the [VERIFY] framing). Runs only when the facts slot is
    #   still EMPTY (RAG facts win). Never raises; on failure ctx is unchanged. Metered through
    #   the same telemetry_sink as the outline/chapters, so _settle bills it.
    # P0-B: does THIS job want the structured semantic envelope alongside the prose?
    #
    # 🔴 `assist` ONLY — NOT `shadow`. Requesting the envelope changes the Story Bible
    #    SYSTEM PROMPT and the user prompt (the suppression exception), grows the response
    #    with a fenced JSON block, and — because that fence is deliberately left in the
    #    returned text, matching `canon_registry`'s own precedent — changes
    #    `ctx.canonical_facts`, which every chapter worker receives as its pinned prefix.
    #    Shadow's governing invariant is that it "may observe, never prevent — §9: no
    #    user-visible change" (see `test_shadow_context_stays_writable` and the shadow
    #    branch below). Different prompt, different output tokens, different chapter input
    #    is a user-visible change by any reading. An earlier draft had this as
    #    `_cl_mode in ("shadow", "assist")`, which silently redefined what shadow means —
    #    exactly the kind of quiet scope creep the mode split exists to prevent.
    #
    # 🔴 FICTION ONLY, and the nonfiction case REFUSES rather than degrades (below). The
    #    envelope's entities/anchors/events come from an LLM inventing or restating the
    #    fact-sheet's own content. For FICTION that is legitimate: the bible DECIDES the
    #    invented specifics (`facts_are_bible=True`), so it IS the authority. For
    #    NONFICTION the regime is the opposite — the sheet pins only what the premise
    #    gives and marks unknowns `[VERIFY]`, never fabricating — so LLM-produced tuples
    #    would be UNGROUNDED claims about real people, dates and events, promoted to canon
    #    authority QC then enforces against. Until a provenance path exists that can prove
    #    `job_input`/`source_grounded` grounding, nonfiction gets no semantic authority.
    #
    # off-mode never requests it, so `structured_semantic` stays unpassed below, which is
    # what keeps the off-path call byte-identical to before P0-B existed.
    #
    # `_cl_semantic_source` is initialized here, UNCONDITIONALLY, so every path past this
    # point — Story Bible skipped entirely (RAG facts already present, flag off, single
    # chapter), the call raising, or a normal run — leaves it a defined `None` rather than
    # an unset name a later reference could NameError on.
    _cl_wants_semantic = (_cl_mode == "assist") and _is_fiction_style(style)
    _cl_semantic_source = None
    # Advisory `canon_registry` sidecar, threaded from the structured Story Bible response
    # to `narration_api`'s canon-diff via this function's result dict. `None` on every
    # non-structured path, where the registry still rides as a fence inside the prose and
    # the consumer's own regex scrape finds it exactly as before.
    _cl_canon_registry = None
    if (str(os.environ.get("NARASI_STORY_BIBLE", "1")).strip().lower() not in ("0", "false", "no", "off")
            and total >= 2 and not (ctx.canonical_facts or "").strip()):
        _fic = _is_fiction_style(style)
        try:
            from .dynamic import build_story_bible
            # ── BEST-OF-N SKELETON (NARASI_BIBLE_BEST_OF, default 0=OFF, round-5) ──
            # One roll samples the generator's MEDIAN skeleton; every craft ceiling the
            # 7-roll lens corpus names (keystone allocation, secondary arc, evidence
            # cost, bridge beat) is a SKELETON property decided at bible time. N
            # candidates in PARALLEL cost the same wall-clock as one (the bible is
            # the serial bottleneck), plus one ~5s cheap judge call. Winner flows into
            # the untouched pin → validator → enforce path. Any failure ⟹ candidate #1
            # (or the single-call path when N<2). Cost: N× bible tokens, only when set.
            _bo_n = 0
            if _fic:
                try:
                    _bo_n = max(0, min(3, int(str(os.environ.get("NARASI_BIBLE_BEST_OF", "0")).strip() or "0")))
                except Exception:  # noqa: BLE001
                    _bo_n = 0
            if _bo_n >= 2:
                _cands_raw = await asyncio.gather(
                    *[build_story_bible(
                        topic, list(ctx.chapters or chapters), is_fiction=_fic,
                        style=style, language=language,
                        manager_model=m_model, telemetry_sink=telemetry_sink,
                        narrative_grammar_block=ctx.narrative_grammar_block,
                        structured_semantic=_cl_wants_semantic)
                      for _ in range(_bo_n)],
                    return_exceptions=True)
                # P0-B + best-of interaction: each candidate carries ITS OWN envelope (the
                # same LLM response that produced its prose), never a different candidate's
                # — that pairing is what keeps the structured data "not an independent
                # authority" once a WINNER is picked below. `_cand_pairs` is None under
                # `structured_semantic=False` (off-mode): the old bare-string shape,
                # untouched.
                if _cl_wants_semantic:
                    import canon_lite_semantic_source as _bo_css
                    _bo_outline = list(ctx.chapters or chapters)
                    _shaped = [c for c in _cands_raw
                               if isinstance(c, tuple) and len(c) == 3
                               and isinstance(c[0], str) and c[0].strip()]
                    # 🔴 P0-B #4 — ASSIST MAY ONLY SELECT A CANDIDATE THAT CARRIES REAL
                    #    SEMANTIC AUTHORITY. A candidate whose envelope is absent, invalid
                    #    or empty cannot be repaired by winning: it will refuse at the
                    #    arming gate a few hundred lines below, AFTER the judge call and
                    #    after every chapter task has been planned. Worse, letting the
                    #    judge see it means a sourceless candidate can BEAT a valid one on
                    #    prose quality and turn a runnable job into a guaranteed refusal —
                    #    the two live `oehhe741` candidates were both sourceless, and the
                    #    judge happily picked #2.
                    #
                    #    "Valid" here means the SAME four things the arming gate will ask
                    #    later — type, self-hash, outline binding, bible binding — plus
                    #    non-emptiness. Checking only type+emptiness (an earlier version of
                    #    this filter) would still let a candidate through that the gate is
                    #    certain to reject, which is the whole failure this filter exists to
                    #    prevent: the judge would spend a call choosing between candidates,
                    #    one of which cannot possibly run.
                    #
                    #    `binds_bible(p[0])` pairs each envelope against ITS OWN prose —
                    #    candidate i's text, never the eventual winner's — which is what
                    #    makes a cross-paired or superseded extraction visible here.
                    def _bo_usable(p) -> bool:
                        src = p[1]
                        if not isinstance(src, _bo_css.CanonLiteSemanticSourceV1):
                            return False
                        try:
                            return bool(src.verify_sha256()
                                        and src.binds_outline(_bo_outline)
                                        and src.binds_bible(p[0])
                                        and src.has_any_semantic_content())
                        except Exception:  # noqa: BLE001 — an unusable candidate, not a crash
                            return False

                    _cand_pairs = [p for p in _shaped if _bo_usable(p)]
                    if len(_cand_pairs) != len(_shaped):
                        log.warning(
                            "bible best-of: %d of %d candidate(s) carried no usable semantic "
                            "source and are NOT eligible for assist selection "
                            "(error_code=bible_candidate_semantic_source_unusable)",
                            len(_shaped) - len(_cand_pairs), len(_shaped))
                    _cands = [p[0] for p in _cand_pairs]
                else:
                    _cand_pairs = None
                    _cands = [c for c in _cands_raw if isinstance(c, str) and c.strip()]
                _bible = _cands[0] if _cands else ""
                _cl_semantic_source = _cand_pairs[0][1] if _cand_pairs else None
                _cl_canon_registry = _cand_pairs[0][2] if _cand_pairs else None
                if len(_cands) >= 2:
                    try:
                        from laozhang_api import _narasi_cheap_call as _bj_call, _narasi_parse_json as _bj_parse
                        # ROUND-8 (user directive, 14-file evidence): "supporting characters thin"
                        # and "antagonist never appears" survived EVERY roll across three premises —
                        # they are ABSENCES born at the skeleton, so the doctrine-legal lever is the
                        # SELECTION rubric, not another generator instruction. Criteria 6-7 make the
                        # judge prefer skeletons that embody the antagonist and keep the cast alive.
                        _bj_sys = (
                            "You are judging candidate STORY BIBLES written for the same premise and "
                            "outline. Pick the one that will produce the strongest serialized melodrama. "
                            "Score each on: (1) KEYSTONE ALLOCATION — decisions, confessions, "
                            "evidence-discoveries and handovers pinned to named ON-PAGE scenes; "
                            "(2) SECONDARY ARC — a non-lead with a stated want and a turn; "
                            "(3) EVIDENCE COST — discoveries cost the finder something, no convenient "
                            "single-box finds; (4) SPINE — calendar/timeline complete and arithmetic-"
                            "consistent; (5) FRESHNESS — specific, non-generic names and beats; "
                            "(6) ANTAGONIST EMBODIED — the opposing power has at least one named "
                            "ON-PAGE scene (a confrontation, a deposition, an offer, a threat), not "
                            "only documents and verdicts about them; (7) CAST PERSISTENCE — every "
                            "named supporting character is given a RETURN appearance or a stated exit; "
                            "family members central to the premise (a mother, a mentor) get at least "
                            "one scene of their own, never introduced-then-forgotten. "
                            "When candidates score similarly on criteria 1-5, criteria 6-7 DECIDE "
                            "the winner — a skeleton that embodies its antagonist and keeps its "
                            "cast alive beats an otherwise-equal one that does not. Return "
                            "ONLY JSON: {\"winner\": <1-based index>, \"reason\": \"<one line>\"}.")
                        _bj_user = "\n\n".join(
                            f"===== CANDIDATE {i + 1} =====\n{c[:9000]}" for i, c in enumerate(_cands))
                        _bj_raw, _bj_cr = await _bj_call(_bj_sys, _bj_user, tenant_id=tenant_id,
                                                         user_id=None, job_uuid=None, json_mode=True)
                        _bj = _bj_parse(_bj_raw) if isinstance(_bj_raw, str) else (_bj_raw or {})
                        _w = int((_bj or {}).get("winner") or 1)
                        if 1 <= _w <= len(_cands):
                            _bible = _cands[_w - 1]
                            if _cand_pairs:
                                # The winner's OWN envelope and OWN registry — the three
                                # values came out of one response and must never be split
                                # across candidates.
                                _cl_semantic_source = _cand_pairs[_w - 1][1]
                                _cl_canon_registry = _cand_pairs[_w - 1][2]
                        log.info("bible best-of-%d: %d candidate(s), winner #%d — %s",
                                 _bo_n, len(_cands), _w, str((_bj or {}).get("reason") or "")[:120])
                    except Exception as _bje:  # noqa: BLE001 — judge is an enhancement
                        log.warning("bible best-of judge failed (non-fatal, candidate #1 kept): %s", _bje)
            elif _cl_wants_semantic:
                _bible, _cl_semantic_source, _cl_canon_registry = await build_story_bible(
                    topic, list(ctx.chapters or chapters), is_fiction=_fic,
                    style=style, language=language,
                    manager_model=m_model, telemetry_sink=telemetry_sink,
                    narrative_grammar_block=ctx.narrative_grammar_block,
                    structured_semantic=True,
                )
            else:
                _bible = await build_story_bible(
                    topic, list(ctx.chapters or chapters), is_fiction=_fic,
                    style=style, language=language,
                    manager_model=m_model, telemetry_sink=telemetry_sink,
                    narrative_grammar_block=ctx.narrative_grammar_block,
                )
            if _bible:
                ctx.canonical_facts = _bible
                ctx.facts_are_bible = _fic  # True ⟹ invent framing; False ⟹ [VERIFY] framing
                log.info("narrate_chapters: %s pinned (%d chars, style=%s, fiction=%s)",
                         "story bible" if _fic else "continuity sheet", len(_bible), style, _fic)
                # ROUND-10 ANTAGONIST SCENE ALLOCATION (NARASI_ANTAG_SCENE_CHECK, default
                # OFF): the opposing power stayed faceless 16/16 files even with the motive
                # pin (CRAFT_LEVERS_V2) live — pins buy MOTIVE, not SCENES; an unallocated
                # scene never gets written on a 2k-word canvas. Extract-and-check over the
                # outline: if no chapter gives the antagonist an on-page scene, ONE bounded
                # amendment call rewrites a single mid-book chapter summary. Never raises.
                try:
                    if (_fic and str(os.environ.get("NARASI_ANTAG_SCENE_CHECK", "0")).strip().lower() in ("1", "true", "yes", "on")):
                        from laozhang_api import _narasi_cheap_call as _as_call, _narasi_parse_json as _as_parse
                        _as_ch = list(ctx.chapters or chapters or [])
                        _as_outline = "\n".join(
                            f"{i + 1}. {str((c or {}).get('title') or '')} — {str((c or {}).get('summary') or '')[:220]}"
                            for i, c in enumerate(_as_ch) if isinstance(c, dict))
                        if _as_outline:
                            _as_sys = (
                                "You are checking a chapter outline against its story fact-sheet. Question: "
                                "does ANY chapter give the story's OPPOSING POWER (the antagonist person or "
                                "the responsible institution's named officer) an ON-PAGE scene — a "
                                "confrontation, deposition, offer, threat, or appearance — rather than "
                                "existing only in documents, verdicts and reports? Return ONLY JSON: "
                                "{\"allocated\": true|false, \"chapter\": <n or null>, \"antagonist\": \"<who>\"}")
                            _as_raw, _as_cr = await _as_call(_as_sys,
                                                             "OUTLINE:\n" + _as_outline + "\n\nFACT-SHEET (head):\n" + _bible[:3500],
                                                             tenant_id=tenant_id, user_id=None,
                                                             job_uuid=None, json_mode=True)
                            _as_d = _as_parse(_as_raw) if isinstance(_as_raw, str) else (_as_raw or {})
                            if isinstance(_as_d, dict) and _as_d.get("allocated") is False:
                                _as_depth = (
                                    " In that scene the antagonist must VOICE their own logic — "
                                    "rationalizing the wrong as procedure, duty, risk-management or "
                                    "necessity (e.g. 'the level was unoccupied; opening it would have "
                                    "endangered the crew') — a coherent, specific justification shown "
                                    "through what they SAY and DO, never cartoon malice and never a "
                                    "confession of guilt. Give them one human tell (composure, a flicker, "
                                    "a deflection). Make them a person with a position, not a name on a form."
                                    if os.environ.get("NARASI_ANTAG_DEPTH", "0").strip().lower() in ("1", "true", "yes", "on")
                                    else "")
                                _as_sys2 = (
                                    "The outline below never puts the opposing power on-page. Pick ONE chapter "
                                    "between the midpoint and the second-to-last, and rewrite ONLY its summary "
                                    "so it now contains one on-page scene with the antagonist "
                                    f"({str(_as_d.get('antagonist') or 'the responsible officer')[:60]}) — a "
                                    "confrontation, deposition, offer or threat that fits the existing beats; "
                                    "keep every other element of that summary." + _as_depth + " Return ONLY JSON: "
                                    "{\"chapter\": <n>, \"summary\": \"<the full rewritten summary>\"}")
                                _as_raw2, _as_cr2 = await _as_call(_as_sys2, _as_outline,
                                                                   tenant_id=tenant_id, user_id=None,
                                                                   job_uuid=None, json_mode=True)
                                _as_d2 = _as_parse(_as_raw2) if isinstance(_as_raw2, str) else (_as_raw2 or {})
                                try:
                                    _as_n = int((_as_d2 or {}).get("chapter") or 0)
                                    _as_sum = str((_as_d2 or {}).get("summary") or "").strip()
                                except Exception:  # noqa: BLE001
                                    _as_n, _as_sum = 0, ""
                                if 2 <= _as_n <= len(_as_ch) and len(_as_sum) > 60 and isinstance(_as_ch[_as_n - 1], dict):
                                    _as_ch[_as_n - 1]["summary"] = _as_sum
                                    if ctx.chapters:
                                        ctx.chapters = _as_ch
                                    # ROUND-11: the cheap check names the antagonist; the amend
                                    # call may target a DIFFERENT figure (roll-14 logged "Kang
                                    # Seong-ho" — the tragic father, not the corporate opponent).
                                    # Log both so the amendment is auditable.
                                    log.info("antag-scene check: no on-page antagonist scene — ch %d amended (check named: %s)",
                                             _as_n, str(_as_d.get("antagonist") or "?")[:50])
                                else:
                                    log.info("antag-scene check: amendment unusable — outline kept")
                            else:
                                log.info("antag-scene check: allocated=%s chapter=%s",
                                         (_as_d or {}).get("allocated"), (_as_d or {}).get("chapter"))
                except Exception as _ase:  # noqa: BLE001
                    log.warning("antag-scene check failed (non-fatal): %s", _ase)

                # ROUND-12 WARMTH SCENE ALLOCATION (NARASI_WARMTH_SCENE_CHECK, default OFF --
                # the sanctioned register exception, opened on request). warmth-bonding beats
                # read 0 across 17 files even with NARASI_KDRAMA_WARMTH_MOVE tightening the
                # gate: the checklist line is IGNORED because warmth is a SCENE, not a rule.
                # Same lever as the antagonist check -- post-bible extract-and-check over the
                # outline: if the two leads share no on-page warmth beat before the plot turns,
                # amend ONE early chapter's summary to carry one. Two-hander premises only.
                try:
                    if (_fic and str(os.environ.get("NARASI_WARMTH_SCENE_CHECK", "0")).strip().lower() in ("1", "true", "yes", "on")):
                        from laozhang_api import _narasi_cheap_call as _ws_call, _narasi_parse_json as _ws_parse
                        _ws_ch = list(ctx.chapters or chapters or [])
                        _ws_outline = "\n".join(
                            f"{i + 1}. {str((c or {}).get('title') or '')} -- {str((c or {}).get('summary') or '')[:220]}"
                            for i, c in enumerate(_ws_ch) if isinstance(c, dict))
                        if _ws_outline and len(_ws_ch) >= 4:
                            _ws_sys = (
                                "You are checking a chapter outline against its story fact-sheet for a "
                                "melodrama. Question: do the TWO LEADS share at least one ON-PAGE beat of "
                                "quiet warmth or bonding -- a shared meal, an unguarded moment, a small "
                                "kindness, ease between them -- BEFORE the central betrayal or plot turn, so "
                                "later rupture has an earned bond to break? A working partnership or shared "
                                "investigation alone does NOT count; it must be a moment of human closeness. "
                                "Return ONLY JSON: {\"allocated\": true|false, \"chapter\": <n or null>}")
                            _ws_raw, _ws_cr = await _ws_call(
                                _ws_sys, "OUTLINE:\n" + _ws_outline + "\n\nFACT-SHEET (head):\n" + _bible[:3500],
                                tenant_id=tenant_id, user_id=None, job_uuid=None, json_mode=True)
                            _ws_d = _ws_parse(_ws_raw) if isinstance(_ws_raw, str) else (_ws_raw or {})
                            if isinstance(_ws_d, dict) and _ws_d.get("allocated") is False:
                                _ws_sys2 = (
                                    "The outline gives the two leads no on-page warmth before the plot turns. "
                                    "Pick ONE chapter in the FIRST HALF and rewrite ONLY its summary so it now "
                                    "includes one small, understated bonding beat between the leads -- a shared "
                                    "meal, a quiet gesture, a moment of ease -- woven into the existing action, "
                                    "NOT a romance subplot and NOT displacing the chapter's plot work. Keep "
                                    "every other element. Return ONLY JSON: {\"chapter\": <n>, \"summary\": \"<full rewritten summary>\"}")
                                _ws_raw2, _ws_cr2 = await _ws_call(_ws_sys2, _ws_outline,
                                                                  tenant_id=tenant_id, user_id=None,
                                                                  job_uuid=None, json_mode=True)
                                _ws_d2 = _ws_parse(_ws_raw2) if isinstance(_ws_raw2, str) else (_ws_raw2 or {})
                                try:
                                    _ws_n = int((_ws_d2 or {}).get("chapter") or 0)
                                    _ws_sum = str((_ws_d2 or {}).get("summary") or "").strip()
                                except Exception:  # noqa: BLE001
                                    _ws_n, _ws_sum = 0, ""
                                _ws_half = max(2, len(_ws_ch) // 2)
                                if 1 <= _ws_n <= _ws_half and len(_ws_sum) > 60 and isinstance(_ws_ch[_ws_n - 1], dict):
                                    _ws_ch[_ws_n - 1]["summary"] = _ws_sum
                                    if ctx.chapters:
                                        ctx.chapters = _ws_ch
                                    log.info("warmth-scene check: no on-page bonding beat -- ch %d summary amended", _ws_n)
                                else:
                                    log.info("warmth-scene check: amendment unusable (ch %s) -- outline kept", _ws_n)
                            else:
                                log.info("warmth-scene check: allocated=%s chapter=%s",
                                         (_ws_d or {}).get("allocated"), (_ws_d or {}).get("chapter"))
                except Exception as _wse:  # noqa: BLE001
                    log.warning("warmth-scene check failed (non-fatal): %s", _wse)

                # ROUND-14 SUPPORTING-CAST DEPTH (NARASI_CAST_DEPTH, default OFF): all four lenses
                # scored supporting cast ~7.5 across 18 files even at 40k -- the prosecutor, the mother,
                # the witnesses stay FUNCTIONS. CRAFT_LEVERS pins a want in the bible, but a want with no
                # SCENE never reaches the page. Same allocation lever as antag/warmth: if no named
                # secondary gets a beat with a PERSONAL STAKE of their own, amend one chapter summary so
                # one does. Deepens a character the outline already has; never invents one. Fiction-only.
                try:
                    if (_fic and str(os.environ.get("NARASI_CAST_DEPTH", "0")).strip().lower() in ("1", "true", "yes", "on")):
                        from laozhang_api import _narasi_cheap_call as _cd_call, _narasi_parse_json as _cd_parse
                        _cd_ch = list(ctx.chapters or chapters or [])
                        _cd_outline = "\n".join(
                            f"{i + 1}. {str((c or {}).get('title') or '')} -- {str((c or {}).get('summary') or '')[:220]}"
                            for i, c in enumerate(_cd_ch) if isinstance(c, dict))
                        if _cd_outline and len(_cd_ch) >= 5:
                            _cd_sys = (
                                "You are checking a chapter outline against its story fact-sheet. Question: does "
                                "any NAMED SUPPORTING character (not the two leads, not the antagonist) -- a "
                                "prosecutor, a parent, a witness, a colleague -- get at least one on-page beat "
                                "driven by a PERSONAL STAKE of their OWN (a private reason they care, a cost they "
                                "carry, a hesitation, a mistake) rather than only delivering information or "
                                "procedure? Return ONLY JSON: {\"allocated\": true|false, \"chapter\": <n or null>, "
                                "\"who\": \"<name>\"}")
                            _cd_raw, _cd_cr = await _cd_call(
                                _cd_sys, "OUTLINE:\n" + _cd_outline + "\n\nFACT-SHEET (head):\n" + _bible[:3500],
                                tenant_id=tenant_id, user_id=None, job_uuid=None, json_mode=True)
                            _cd_d = _cd_parse(_cd_raw) if isinstance(_cd_raw, str) else (_cd_raw or {})
                            if isinstance(_cd_d, dict) and _cd_d.get("allocated") is False:
                                _cd_sys2 = (
                                    "The outline keeps its supporting cast as functions. Pick ONE named supporting "
                                    "character the outline ALREADY uses (not a lead, not the antagonist) and ONE "
                                    "chapter they appear in, and rewrite ONLY that chapter's summary so it gives "
                                    "that character a single beat with a personal stake of their own -- a private "
                                    "reason they care, a cost, a hesitation, a small human turn -- woven into the "
                                    "existing action, without a new subplot and without displacing the plot work. "
                                    "Keep every other element. Return ONLY JSON: {\"chapter\": <n>, "
                                    "\"summary\": \"<full rewritten summary>\"}")
                                _cd_raw2, _cd_cr2 = await _cd_call(_cd_sys2, _cd_outline,
                                                                  tenant_id=tenant_id, user_id=None,
                                                                  job_uuid=None, json_mode=True)
                                _cd_d2 = _cd_parse(_cd_raw2) if isinstance(_cd_raw2, str) else (_cd_raw2 or {})
                                try:
                                    _cd_n = int((_cd_d2 or {}).get("chapter") or 0)
                                    _cd_sum = str((_cd_d2 or {}).get("summary") or "").strip()
                                except Exception:  # noqa: BLE001
                                    _cd_n, _cd_sum = 0, ""
                                if 2 <= _cd_n <= len(_cd_ch) and len(_cd_sum) > 60 and isinstance(_cd_ch[_cd_n - 1], dict):
                                    _cd_ch[_cd_n - 1]["summary"] = _cd_sum
                                    if ctx.chapters:
                                        ctx.chapters = _cd_ch
                                    log.info("cast-depth check: no personal-stake beat -- ch %d amended (%s)",
                                             _cd_n, str(_cd_d.get("who") or "?")[:40])
                                else:
                                    log.info("cast-depth check: amendment unusable -- outline kept")
                            else:
                                log.info("cast-depth check: allocated=%s (%s)",
                                         (_cd_d or {}).get("allocated"), str((_cd_d or {}).get("who") or "?")[:40])
                except Exception as _cde:  # noqa: BLE001
                    log.warning("cast-depth check failed (non-fatal): %s", _cde)
                # LEDGER VALIDATOR at BIBLE time (NARASI_LEDGER_VALIDATOR, default OFF):
                # a ledger hit committed in the bible poisons every chapter (roll-3
                # 'eleven-month drought'), so scan the bible the moment it is pinned —
                # earliest, cheapest signal (pure regex, zero LLM). Report-only WARN;
                # never blocks or edits the bible; never raises. Fiction-only: the
                # lane ledger is a fiction-lane artifact.
                try:
                    if _fic and str(os.environ.get("NARASI_LEDGER_VALIDATOR", "0")).strip().lower() in ("1", "true", "yes", "on"):
                        import narasi_counters as _lnc
                        from pakem import resolve_style_key as _lrsk
                        _lrep = _lnc.ledger_hits_scan("", bible=_bible, style_key=_lrsk(style))
                        if _lrep.get("bible_hits"):
                            log.warning("ledger validator: %d BIBLE-level ledger hit(s) at pin time: %s",
                                        _lrep["bible_hits"],
                                        sorted({str(h.get("term")) for h in _lrep.get("hits") or []})[:10])
                            # LEDGER ENFORCE (NARASI_LEDGER_ENFORCE, default OFF): Law 1 of the
                            # 5-roll audit — a WARN nobody acts on is a dead ban. ONE bible
                            # re-roll with the violating terms quoted back; the re-roll is
                            # pinned ONLY if it strictly reduces bible-level hits (never
                            # trades sideways, never loops). Cost: one extra manager call,
                            # only on the hit path. Failure ⟹ original bible stands.
                            if str(os.environ.get("NARASI_LEDGER_ENFORCE", "0")).strip().lower() in ("1", "true", "yes", "on"):
                                try:
                                    # ORBIT-VALUE POLICY (round-6, NARASI_LEDGER_VALUE_DEMOTE): floor
                                    # numbers are VALUES, not named entities — SBF stood its whole
                                    # premise on banned floor 6 (Units 601/701, the composition
                                    # "Floor 6½") and enforcement would have asked a patch call to
                                    # rewrite the story's load-bearing address. Demote the floor
                                    # category to WARN-only; names/foods/numbers stay enforced.
                                    _vdem = str(os.environ.get("NARASI_LEDGER_VALUE_DEMOTE", "0")).strip().lower() in ("1", "true", "yes", "on")
                                    # F5: keep `category:value` INTACT here. Splitting
                                    # the category off before ownership let a value
                                    # owned in one category exempt the same value
                                    # reported under another.
                                    _terms_all = sorted({str(h.get("term", ""))
                                                         for h in _lrep.get("hits") or []
                                                         if h.get("where") == "bible"
                                                         and not (_vdem and str(h.get("term", "")).startswith("floor:"))})
                                    # PREMISE EXEMPTION (round-4, roll-6 lesson): a term the USER's
                                    # premise itself supplies (surname Han from 'Han Seo-jin') is not
                                    # a lane tic — the bible MUST use it, so it can never re-roll away.
                                    # Roll 6 paid a 160s re-roll chasing hits that included exactly
                                    # this class. Word-boundary match against the topic text.
                                    import re as _pre
                                    # ROUND-10: cross-language exemption (the injection path got this
                                    # in r8; the bible-level filter here kept chasing «11»/«19» on a
                                    # premise that wrote «sebelas» — roll-13 surgical burn).
                                    # F5: the ACCEPTED OUTLINE owns its terms too.
                                    # Filtering on the topic alone let canary v9's
                                    # `Tae-jun` — established by the outline, never
                                    # written verbatim in the topic — trigger a surgical
                                    # rewrite or a full re-roll of the bible that had
                                    # just correctly adopted it.
                                    # 🔴 `bible=""` ON PURPOSE: the candidate is not an
                                    # authority until it is pinned. Passing it here would
                                    # make every term it just invented self-exempt and
                                    # switch this enforcement off entirely.
                                    try:
                                        _f5outline = ctx.outline()
                                    except Exception:  # noqa: BLE001
                                        _f5outline = ""
                                    _terms = _f5_unowned_ledger_terms(
                                        _terms_all, topic=topic or "",
                                        outline=_f5outline)[:12]
                                    if not _terms:
                                        log.info("ledger-enforce: all %d bible hit(s) premise-supplied (%s) — re-roll skipped",
                                                 len(_terms_all), ", ".join(_terms_all[:6]))
                                    elif (str(os.environ.get("NARASI_LEDGER_ENFORCE_SURGICAL", "0")).strip().lower()
                                          in ("1", "true", "yes", "on")
                                          and _cl_wants_semantic and _cl_semantic_source is not None):
                                        # 🔴 P0-B #5 — CONTAINED BEFORE THE PROVIDER CALL, NOT AFTER IT.
                                        #    This patch rewrites the pinned bible in place, but the
                                        #    semantic source was hashed against the PRE-patch text, so
                                        #    applying it guarantees `binds_bible()` refuses the job with
                                        #    `..._bible_changed` — live job `oehhe741` had 18 bible-level
                                        #    hits and would have died here even with a perfect envelope.
                                        #
                                        #    The containment therefore sits AHEAD of `_sp_call`: an
                                        #    earlier version of this fix checked at the APPLY site, which
                                        #    still paid for a patch it was always going to throw away.
                                        #    Nobody should be billed for output whose only possible fate
                                        #    is being discarded.
                                        #
                                        #    Report-only is the containment, NOT re-hashing the old
                                        #    source against new text: the tuples were extracted from the
                                        #    unpatched prose, and stamping a fresh digest onto them would
                                        #    assert a provenance nobody verified — exactly what
                                        #    `binds_bible()` exists to catch. The reroll branch below is
                                        #    the one path allowed to replace the bible, because it
                                        #    carries a NEW envelope from the SAME response.
                                        #
                                        #    Cost: assist jobs keep the ledger hits this patch would have
                                        #    removed — a prose-quality regression bounded to the assist
                                        #    cohort, traded for a job that actually runs.
                                        log.info(
                                            "ledger-enforce SURGICAL: SKIPPED before any provider call — "
                                            "assist holds a bound semantic source for this bible, so a "
                                            "patch could only invalidate it (%d bible-level hit(s) left "
                                            "in place, error_code=surgical_contained_by_assist)",
                                            int(_lrep.get("bible_hits") or 0))
                                    elif str(os.environ.get("NARASI_LEDGER_ENFORCE_SURGICAL", "0")).strip().lower() in ("1", "true", "yes", "on"):
                                        # ROUND-5.1 SURGICAL RE-ROLL: two consecutive prod rolls burned a FULL
                                        # bible regeneration (203s, 234s) and both came back WORSE (10→13,
                                        # 16→28) — a fresh roll of the whole sheet re-samples every slot,
                                        # including the clean ones. The bible is a LINE-ORIENTED sheet, so
                                        # patch ONLY the offending lines: one cheap JSON call rewrites them
                                        # in place (~15s), exact-string splice, re-scan, accept only on
                                        # strict decrease. Any failure ⟹ original bible stands.
                                        try:
                                            import re as _sre
                                            from laozhang_api import _narasi_cheap_call as _sp_call, _narasi_parse_json as _sp_parse
                                            _bad_lines = []
                                            for _ln in _bible.split("\n"):
                                                if any(_sre.search(r"(?i)\b" + _sre.escape(t) + r"\b", _ln) for t in _terms):
                                                    _bad_lines.append(_ln)
                                                if len(_bad_lines) >= 14:
                                                    break
                                            if _bad_lines:
                                                _sp_sys = (
                                                    "You are patching single lines of a story fact-sheet. Each line below "
                                                    "contains one or more BANNED lane items: " + ", ".join(_terms) + ". "
                                                    "Rewrite EACH line replacing every banned item (and near-variants) with a "
                                                    "fresh, premise-appropriate invention; keep the line's structure, meaning "
                                                    "and everything else IDENTICAL. Return ONLY JSON: "
                                                    "{\"lines\": [{\"old\": \"<exact original line>\", \"new\": \"<patched line>\"}]}")
                                                # budget scales with len(_bad_lines): the flat 800 default truncated
                                                # mid-JSON at roll-13's 21 hits (SALVAGED only 1 patch pair) — each
                                                # line needs its full original echoed back + a full rewrite + JSON overhead.
                                                _sp_max_tokens = 300 + 350 * len(_bad_lines)
                                                _sp_raw, _sp_cr = await _sp_call(_sp_sys, "\n".join(_bad_lines),
                                                                                 tenant_id=tenant_id, user_id=None,
                                                                                 job_uuid=None, max_tokens=_sp_max_tokens,
                                                                                 json_mode=True)
                                                _sp = _sp_parse(_sp_raw) if isinstance(_sp_raw, str) else (_sp_raw or {})
                                                if isinstance(_sp, dict) and not (_sp.get("lines") or []):
                                                    _sp = None   # empty parse → try salvage below
                                                if not isinstance(_sp, dict):
                                                    # ROUND-10 SALVAGE (roll-13: head showed a VALID
                                                    # {"lines":[{"old":...}]} start — token-cap truncation
                                                    # again): recover individually balanced old/new pairs.
                                                    _sraw = str(_sp_raw or "")
                                                    _spairs = []
                                                    for _sm in _sre.finditer(r"\{", _sraw):
                                                        _st2 = _sm.start()
                                                        if not _sre.search(r"\"old\"", _sraw[_st2:_st2 + 80]):
                                                            continue
                                                        _d2, _in2, _e2 = 0, False, False
                                                        for _i2 in range(_st2, min(len(_sraw), _st2 + 3000)):
                                                            _c2 = _sraw[_i2]
                                                            if _in2:
                                                                if _e2:
                                                                    _e2 = False
                                                                elif _c2 == "\\":
                                                                    _e2 = True
                                                                elif _c2 == '"':
                                                                    _in2 = False
                                                            elif _c2 == '"':
                                                                _in2 = True
                                                            elif _c2 == "{":
                                                                _d2 += 1
                                                            elif _c2 == "}":
                                                                _d2 -= 1
                                                                if _d2 == 0:
                                                                    try:
                                                                        import json as _sjson
                                                                        _o2 = _sjson.loads(_sraw[_st2:_i2 + 1])
                                                                        if isinstance(_o2, dict) and _o2.get("old") and _o2.get("new"):
                                                                            _spairs.append(_o2)
                                                                    except Exception:  # noqa: BLE001
                                                                        pass
                                                                    break
                                                        if len(_spairs) >= 14:
                                                            break
                                                    if _spairs:
                                                        _sp = {"lines": _spairs}
                                                        log.info("ledger-enforce surgical: SALVAGED %d patch pair(s) from truncated response", len(_spairs))
                                                if not isinstance(_sp, dict):
                                                    # ROUND-6 (SBF roll): parse returned None and the
                                                    # .get() below crashed the whole surgical path —
                                                    # 18 poisoned bans sailed into MAP unpatched.
                                                    log.warning("ledger-enforce surgical: unparseable patch response (head: %s) — original bible stands",
                                                                str(_sp_raw)[:160].replace("\n", " "))
                                                    _sp = {}
                                                _bible2 = _bible
                                                _n_patched = 0
                                                for _pair in (_sp.get("lines") or []):
                                                    _old, _newl = str(_pair.get("old") or ""), str(_pair.get("new") or "")
                                                    if _old and _newl and _bible2.count(_old) == 1:
                                                        _bible2 = _bible2.replace(_old, _newl, 1)
                                                        _n_patched += 1
                                                _rep2 = (_lnc.ledger_hits_scan("", bible=_bible2, style_key=_lrsk(style))
                                                         if _n_patched else {})
                                                if _n_patched and int(_rep2.get("bible_hits") or 0) < int(_lrep.get("bible_hits") or 0):
                                                    # Containment for assist lives at the TOP of this
                                                    # branch (see `surgical_contained_by_assist`), so
                                                    # reaching here means no bound semantic source is at
                                                    # risk and the patch may be pinned as it always was.
                                                    ctx.canonical_facts = _bible2
                                                    log.info("ledger-enforce SURGICAL: %d line(s) patched — hits %d → %d, pinned",
                                                             _n_patched, _lrep.get("bible_hits"), _rep2.get("bible_hits") or 0)
                                                else:
                                                    log.info("ledger-enforce SURGICAL: no improvement (%d patched, hits %d → %s) — original kept",
                                                             _n_patched, _lrep.get("bible_hits"),
                                                             (_rep2.get("bible_hits") if _n_patched else "n/a"))
                                        except Exception as _spe:  # noqa: BLE001
                                            log.warning("ledger-enforce surgical failed (non-fatal): %s", _spe)
                                    else:
                                        # P0-B: the reroll REPLACES the pinned bible, so it
                                        # must carry its OWN semantic source from the SAME
                                        # response — the old source describes a document
                                        # that is about to stop being the canon's bible.
                                        # Pairing them here is what keeps `binds_bible()`
                                        # satisfiable after a legitimate reroll; without it
                                        # every reroll would (correctly, but wastefully)
                                        # force assist to refuse.
                                        _bible2_src = None
                                        _bible2_reg = None
                                        if _cl_wants_semantic:
                                            _bible2, _bible2_src, _bible2_reg = await build_story_bible(
                                                topic, list(ctx.chapters or chapters), is_fiction=_fic,
                                                style=style, language=language,
                                                manager_model=m_model, telemetry_sink=telemetry_sink,
                                                extra_negative=", ".join(_terms),
                                                narrative_grammar_block=ctx.narrative_grammar_block,
                                                structured_semantic=True)
                                        else:
                                            _bible2 = await build_story_bible(
                                                topic, list(ctx.chapters or chapters), is_fiction=_fic,
                                                style=style, language=language,
                                                manager_model=m_model, telemetry_sink=telemetry_sink,
                                                extra_negative=", ".join(_terms),
                                                narrative_grammar_block=ctx.narrative_grammar_block)
                                        _rep2 = (_lnc.ledger_hits_scan("", bible=_bible2, style_key=_lrsk(style))
                                                 if _bible2 else {})
                                        if _bible2 and int(_rep2.get("bible_hits") or 0) < int(_lrep.get("bible_hits") or 0):
                                            ctx.canonical_facts = _bible2
                                            # Pinned together, or not at all: a reroll whose
                                            # own envelope failed to parse leaves
                                            # `_cl_semantic_source = None`, and assist then
                                            # refuses rather than pairing new prose with the
                                            # superseded extraction. The registry sidecar
                                            # moves with them for the same reason — all three
                                            # came out of one response.
                                            _cl_semantic_source = _bible2_src
                                            if _cl_wants_semantic:
                                                _cl_canon_registry = _bible2_reg
                                            log.info("ledger-enforce: bible re-rolled — hits %d → %d, re-roll pinned",
                                                     _lrep.get("bible_hits"), _rep2.get("bible_hits") or 0)
                                        else:
                                            log.info("ledger-enforce: re-roll not better (hits %d → %s) — original kept",
                                                     _lrep.get("bible_hits"),
                                                     (_rep2.get("bible_hits") if _bible2 else "no bible"))
                                except Exception as _lee:  # noqa: BLE001
                                    log.warning("ledger-enforce re-roll failed (non-fatal): %s", _lee)
                except Exception as _lve:  # noqa: BLE001
                    log.warning("bible ledger scan failed (non-fatal): %s", _lve)
        except Exception as _be:  # noqa: BLE001
            log.warning("narrate_chapters: story bible generation failed (non-fatal): %s", _be)

    # ── CANON LITE L1 — shadow only (NARASI_CANON_LITE_MODE, default off) ───────────
    # Placed AFTER the Story Bible slot and BEFORE fan-out, which is the one point where
    # the accepted outline and the resolved configuration are both final (§5). It adds
    # NO provider call: the L1 canon is a deterministic projection of the outline plus
    # the resolved config, and the prose bible is advisory (hash only). That is how the
    # §11 "one steady-state canon call" ceiling is met — by spending zero — and how the
    # I09 constraint "do not stack a second serial planner" is satisfied.
    #
    # C11 is why the mode is compared as a literal string here instead of calling
    # canon_lite.resolve_mode(): flag-off must perform NO Canon Lite import at all. The
    # two gates are held equivalent by an explicit test over a shared input matrix, so
    # this duplication cannot drift unnoticed.
    _cl_freeze = None
    if _cl_mode == "shadow":
        # L1.1: honour a §9 parity mismatch recorded for this job upstream. Shadow still
        # runs the job unchanged, but the MEASUREMENT is ineligible — building a canon
        # here would drop a reading into the denominator that was taken under a
        # configuration already known to be inconsistent.
        #
        # Implemented by downgrading the local mode, so the existing guard below skips
        # the whole block: no canon, no provider call, no freeze. Raising instead would
        # land in the construction handler and be mislabelled `invalid` — ineligible is
        # not invalid, and the difference is the whole point of the status.
        try:
            import canon_lite as _cl_elig
            if _cl_elig.job_is_canon_ineligible():
                log.info("canon lite: %s",
                         _cl_elig.telemetry_digest(None, canon_status="skipped"))
                _cl_mode = "off"
        except Exception:  # noqa: BLE001
            log.warning("canon lite: eligibility check unavailable "
                        "code=eligibility_check_error")
    # 🔴 SHADOW AND ASSIST BUILD THE SAME CANON; ONLY WHAT THEY DO WITH IT DIFFERS.
    #    Shadow hashes and logs it. Assist additionally RENDERS it, injects the
    #    rendering into every chapter worker, and freezes the shared context HARD
    #    before fan-out. Sharing the construction is what makes the assist canon
    #    the same artifact shadow has been observing in production.
    #
    # 🔴 AND THEY FAIL DIFFERENTLY, WHICH IS THE WHOLE DIFFERENCE BETWEEN AN
    #    OBSERVER AND A GUARANTEE. Shadow degrades: it may never change what the
    #    user gets, so a failed build is logged and the job runs on. Assist may
    #    NOT degrade — a job that advertises "every chapter saw the same canon"
    #    and then quietly writes an uncanonical book has sold something it did not
    #    deliver, and has charged for it. Every arming failure below therefore
    #    returns a bounded non-success BEFORE the first chapter task exists, which
    #    is the last moment at which refusing is still free.
    _cl_canon = None
    _cl_status = "absent"
    _cl_canon_text: Optional[str] = None
    # TWO prompt hashes, deliberately distinct (2026-08-15 re-audit round 2). The
    # generation one identifies the prefix workers are actually injected with; the
    # canonical one is the LEGACY `canon_prompt_sha256` contract — the hash of
    # `render_canon()`, id-bearing, which QC/L3 still use. F1 changed the injection
    # from `render_canon()` to `render_canon_for_generation()` and, for one round,
    # kept publishing the result under the old field name — silently redefining what
    # that field meant. Keeping both, under names that say which is which, is what
    # lets the injection change without the older contract changing under anyone.
    _cl_gen_prompt_sha: Optional[str] = None
    _cl_canonical_prompt_sha: Optional[str] = None
    _cl_canon_sha: Optional[str] = None
    _cl_ctx_sha: Optional[str] = None
    _cl_assist = (_cl_mode == "assist")

    def _assist_refuse(code: str) -> dict[str, Any]:
        """Bounded non-success for assist, before any physical work. Never raises."""
        nonlocal _cl_freeze
        if _cl_freeze is not None:
            try:
                _cl_freeze.release()
            except Exception:  # noqa: BLE001 - a release bug must not mask the refusal
                log.warning("canon lite: freeze release failed during assist refusal")
            _cl_freeze = None
        log.error("canon lite: assist could not be armed (%s); job refused before work",
                  code)
        return {
            "ok": False, "chapters": [], "book": "", "polished": False,
            "rag_used": False, "context": {}, "strategy": "narrate_chapters",
            "error": code,
        }

    # 🔴 THE COMPOSER IS A PRECONDITION OF ASSIST, NOT A DEGRADATION OF IT.
    #    `_write_chapter` has a fallback for an unavailable assembler: it builds a
    #    minimal direct prompt and runs the worker anyway, so the strategy still
    #    functions. That fallback constructs its Worker with no `system=` at all, so
    #    there is no cache-stable prefix to prepend the canon to — every chapter
    #    would be written with no canon and report no hash, and the census would
    #    refuse the job only after the whole book had been generated.
    #
    #    Refusing here is NOT free, and the comment that used to say so was wrong:
    #    `build_shared_context` (one RAG retrieval) and the Story Bible manager call
    #    both run above this line and are already paid for. What it does save is the
    #    MAP itself — one provider call per chapter, the dominant cost of the job.
    #
    #    `_COMPOSE_OK` is decided at MODULE IMPORT, so unlike every other assist
    #    precondition this one could be checked at the top of the function, before
    #    any spend at all. It is left here so that Stage 1 changes no behaviour
    #    outside the canon path; hoisting it is a real improvement and a separate
    #    decision.
    if _cl_assist and (not _COMPOSE_OK or compose is None):
        return _assist_refuse("canon_lite_assist_composer_unavailable")

    if _cl_mode in ("shadow", "assist"):
        try:
            import canon_lite as _cl
            import canon_lite_semantic_source as _cl_semantic_source_module
            _cl_outline = list(ctx.chapters or chapters)
            _cl_cfg = _cl.build_job_config_snapshot(
                outline_chapters=_cl_outline,
                target_language=language,
                narration_style=style,
                # genre / subgenre / twist_variant_id are NOT resolvable on this path:
                # select_beatmap() runs only in the separate outline endpoint and its
                # twist_id is never read back here. They stay explicitly UNKNOWN rather
                # than being guessed (§6.1).
            )
            # Derived from real context state, never from a style guess.
            if getattr(ctx, "facts_are_bible", False):
                _cl_policy = "fiction_generated"
            elif getattr(ctx, "rag_used", False):
                _cl_policy = "source_grounded"
            else:
                _cl_policy = "unknown"

            # ── P0-B: validate the semantic source against the outline ABOUT TO BE USED ──
            #
            # 🔴 THIS IS THE MUTATION-AFTER-BINDING CHECK, AND IT MUST RUN HERE, NOT AT
            #    THE STORY BIBLE CALL SITE. `_cl_semantic_source.accepted_outline_content_sha256`
            #    was bound to whatever `ctx.chapters` looked like when the Story Bible call
            #    ran, several statements — and, on the best-of-N path, one LLM judge call —
            #    earlier. `_cl_outline` above is what is ABOUT to become the real canon's
            #    chapter identity. `binds_outline()` re-hashes `_cl_outline` right now and
            #    compares: a mismatch means the accepted outline moved in that window, and
            #    injecting the stale extraction would describe a book that is no longer the
            #    one being written. `verify_sha256()` catches the same class of problem from
            #    the other direction — the object's own content no longer matches its hash.
            #
            #    A cleared-cohort assist job that fails ANY of these three checks — absent,
            #    unbound, or genuinely empty (parsed fine but extracted nothing) — refuses
            #    below, before the canon (and therefore before the MAP) rather than silently
            #    building a structurally-present-but-semantically-empty canon: that exact
            #    shape is `has_semantic_authority() == False` while `canon_lite_l3` still
            #    reports `outcome=unchecked` as if something had been verified — the false
            #    canary this workstream exists to close, one layer up from P0-A's.
            _cl_semantic_valid = None
            _cl_semantic_reason = "canon_lite_assist_semantic_source_unavailable"
            # 🔴 `isinstance`, NOT `is not None` ALONE. `build_story_bible()`'s documented
            #    contract is "`None` or a validated `CanonLiteSemanticSourceV1`", but this
            #    block must stay correct even if that contract is ever violated by a future
            #    change — an untyped truthy value reaching `.verify_sha256()` would raise
            #    `AttributeError` INSIDE this try, which the outer `except` catches and
            #    reports as the unrelated, less precise `canon_lite_assist_canon_unavailable`.
            #    Checking the type here keeps the diagnosis honest about what actually failed.
            _cl_bible_now = getattr(ctx, "canonical_facts", "") or None
            if not _cl_wants_semantic and _cl_assist:
                # Assist asked for, but this job is not eligible to HAVE a semantic source
                # at all — today that means nonfiction (see `_cl_wants_semantic`). Refusing
                # is the point: silently running assist with empty tuples would be a canon
                # that passes every structural check and can verify nothing.
                _cl_semantic_reason = "canon_lite_assist_semantic_source_not_eligible"
            elif isinstance(_cl_semantic_source,
                            _cl_semantic_source_module.CanonLiteSemanticSourceV1):
                if not _cl_semantic_source.verify_sha256():
                    _cl_semantic_reason = "canon_lite_assist_semantic_source_unbound"
                elif not _cl_semantic_source.binds_outline(_cl_outline):
                    _cl_semantic_reason = "canon_lite_assist_semantic_source_unbound"
                elif not _cl_semantic_source.binds_bible(_cl_bible_now):
                    # The pinned bible is no longer the one these tuples came from — a
                    # surgical ledger patch rewrote it in place, or a reroll replaced it
                    # without carrying its own envelope. Distinct code from `unbound`: the
                    # outline is fine, the PROVENANCE is not.
                    _cl_semantic_reason = "canon_lite_assist_semantic_source_bible_changed"
                elif not _cl_semantic_source.has_any_semantic_content():
                    _cl_semantic_reason = "canon_lite_assist_semantic_source_empty"
                else:
                    _cl_semantic_valid, _cl_semantic_reason = _cl_semantic_source, None

            _cl_canon = _cl.build_canon_lite_v1(
                outline_chapters=_cl_outline,
                job_config=_cl_cfg,
                fact_source_policy=_cl_policy,
                advisory_bible_text=(getattr(ctx, "canonical_facts", "") or None),
                # SHADOW with an invalid/absent source proceeds on EMPTY tuples — same
                # "tolerate and log" discipline as an unavailable canon; only ASSIST
                # refuses (checked below, after this try/except, matching the existing
                # canon-unavailable check's own structure).
                entities=(_cl_semantic_valid.entities if _cl_semantic_valid else ()),
                anchors=(_cl_semantic_valid.anchors if _cl_semantic_valid else ()),
                one_time_events=(
                    _cl_semantic_valid.one_time_events if _cl_semantic_valid else ()),
            )
            _cl_status = "present"
        except Exception as _cle:  # noqa: BLE001 - shadow must never break a job
            _cl_canon, _cl_status = None, "invalid"
            _cl_semantic_valid = None
            _cl_semantic_reason = "canon_lite_assist_semantic_source_unavailable"
            # Fixed code only: even a dynamically named exception class can carry
            # attacker-controlled prose, so neither message nor type name is telemetry.
            log.warning(
                "canon lite: canon construction failed "
                "(error_code=canon_construction_error) — recorded as invalid, "
                "never as clean")
        # There is no assist without a canon. Shadow records `invalid` and carries
        # on; assist has nothing to inject and must not pretend otherwise.
        if _cl_assist and _cl_canon is None:
            return _assist_refuse("canon_lite_assist_canon_unavailable")
        # P0-B: a non-None canon is not enough for assist — it must carry REAL semantic
        # authority. `_cl_semantic_reason` is always a bounded literal from the fixed set
        # above; never provider/LLM text (§C12-style discipline, same as the L3 reason
        # codes this mirrors).
        if _cl_assist and _cl_semantic_valid is None:
            return _assist_refuse(_cl_semantic_reason)
        try:
            # C7/§8.1: absent or invalid canon is REPORTED as such. The digest is
            # hashes, counts and bounded labels only — no name, title, literal or prose.
            log.info("canon lite: %s",
                     _cl.telemetry_digest(_cl_canon, canon_status=_cl_status))
            # I08/C2: freeze the shared context for the whole fan-out. Soft in
            # shadow — a mutation is reported, not raised, because shadow may not
            # change user-visible behaviour. ASSIST turns prevention on: a worker
            # may READ the frozen snapshot and may not mutate shared context.
            _cl_freeze = _cl.SharedContextFreeze(ctx, hard=_cl_assist)

            # ASSIST: render ONCE, hash the rendering, and hand the whole binding
            # to every worker. `render_canon_for_generation` is byte-stable the same
            # way `render_canon` is — two canons with the same `canon_sha256` render
            # identically — which is what makes the hash of the RENDERING a sound
            # identity for the injected prefix. All of it is computed here, before
            # any task exists, so there is exactly one value of each and no worker
            # can produce its own.
            # F1 (BRIEF-FOR-CODEX-2026-08-14-POST-CANARY-V9.md): this is the
            # GENERATION injection every chapter worker reads as its prefix, not the
            # QC/L3 addressing render — `render_canon()` put `{"anchor_id":"anc3",...}`
            # into that prefix, and canary v9 showed a worker echo `[anc3]` straight
            # into delivered prose. A writer needs the literal, never the id;
            # `render_canon_for_generation` carries the former and omits the latter.
            # QC/L3 still call `render_canon()` directly (narration_api.py) — untouched.
            if _cl_assist:
                _cl_canon_text = _cl.render_canon_for_generation(_cl_canon)
                _cl_gen_prompt_sha = _cl.sha256_hex(_cl_canon_text.encode("utf-8"))
                # The LEGACY contract, preserved unchanged: the hash of the canonical,
                # id-bearing `render_canon()` — exactly what `canon_prompt_sha256`
                # meant at a6711e0 and still means. Only the hash is kept; the render
                # itself is prompt material that may never reach a log or payload.
                _cl_canonical_prompt_sha = _cl.sha256_hex(
                    _cl.render_canon(_cl_canon).encode("utf-8"))
                _cl_canon_sha = _cl_canon.canon_sha256
                # The freeze baseline IS the context identity: the digest of the
                # state every worker is about to read, taken at the instant it
                # stopped being able to change.
                _cl_ctx_sha = _cl_freeze.baseline_digest
                if not _cl_canon_text.strip():
                    # An empty rendering hashes and compares perfectly while
                    # injecting nothing — the one failure that would pass every
                    # downstream check by being consistently absent.
                    raise ValueError("assist canon rendering is empty")
                log.info("canon lite: assist injection armed canon_sha256=%s "
                         "generation_prompt_sha256=%s canonical_prompt_sha256=%s "
                         "context_sha256=%s",
                         _cl_canon_sha, _cl_gen_prompt_sha,
                         _cl_canonical_prompt_sha, _cl_ctx_sha)
        except Exception as _cle2:  # noqa: BLE001
            log.warning(
                "canon lite: shadow instrumentation skipped "
                "(error_code=shadow_instrumentation_error)")
            if _cl_assist:
                # Includes a failed freeze install: assist without a frozen context
                # is assist without its central claim.
                return _assist_refuse("canon_lite_assist_arming_failed")
            _cl_freeze = None

    # ── ASSIST: THE MAP READS THE FROZEN SNAPSHOT, NOT THE ORIGINAL ALIAS ────
    #
    # 🔴 FREEZING `ctx.chapters` IS NOT ENOUGH ON ITS OWN. The freeze replaces
    #    `ctx.chapters` with deep-frozen copies, but the fan-out below iterates the
    #    LOCAL `chapters` list, whose dicts are still the caller's original objects.
    #    Anyone holding that alias — the caller, or an earlier stage of this
    #    function — can mutate a chapter dict after the freeze and change the `ch`
    #    a worker is handed, with the frozen context showing nothing at all. The
    #    two have to be the same objects, so the MAP and the assembly below both
    #    switch to the frozen snapshot.
    #
    #    A length divergence means the canon was hashed over one outline while the
    #    book would be written from another — the binding would be a statement
    #    about a different book. There is no safe way to guess which is right, so
    #    it refuses.
    if _cl_assist:
        _frozen_chapters = list(ctx.chapters or ())
        if len(_frozen_chapters) != total:
            log.error("canon lite: outline diverged between the canon (%d chapters) "
                      "and the fan-out (%d) — the binding would describe a different "
                      "book", len(_frozen_chapters), total)
            return _assist_refuse("canon_lite_assist_outline_divergence")
        chapters = _frozen_chapters

    # Freeze the SAME accepted outline and winning Bible the MAP will read, after
    # every outline amendment and (under assist) after switching to the frozen
    # chapter snapshot.  Later polish/revise stages previously reconstructed no
    # authority at all, which let a final edit rename the protagonist, move beats
    # between chapters, and create time jumps even when every MAP prompt was correct.
    # This packet stays private and in-process; narration_api removes it before
    # persistence.  An outline-less strategy gets None and retains legacy prompts.
    _narrative_authority = _build_narrative_authority_packet(ctx)
    _narrative_authority_text = str(
        (_narrative_authority or {}).get("text") or "")
    if _narrative_authority:
        log.info(
            "narrate_chapters: narrative authority frozen outline_sha256=%s "
            "bible_sha256=%s outline_packet_contract=%s "
            "outline_packet_bundle_sha256=%s",
            _narrative_authority["outline_sha256"],
            _narrative_authority["bible_sha256"],
            _narrative_authority["outline_packet_contract_version"],
            _narrative_authority["outline_packet_bundle_sha256"],
        )

    # 2) MAP — bounded parallel fan-out. Semaphore caps concurrency at max_parallel
    #    so a 40-chapter book doesn't open 40 sockets at once.
    sem = asyncio.Semaphore(max(1, int(max_parallel or 1)))

    # ── BullMQ S2 (resume): with NARRATION_RESUME_ENABLED=1, chapters already
    # checkpointed in narasi_chapters are SKIPPED (a stalled-retry continues instead of
    # rewriting) and each completing chapter is checkpointed durably. Soft-import db;
    # any failure degrades to today's behavior. Default OFF (ship dormant).
    _resume_on = str(os.environ.get("NARRATION_RESUME_ENABLED", "0")).strip().lower() in ("1", "true", "yes", "on")
    _ckpt_uuid = None
    _pre: dict[int, str] = {}
    if _resume_on and tenant_id and job_id:
        try:
            import database as _db
            _row = await _db.get_job_by_external(tenant_id, job_id)
            _ckpt_uuid = (_row or {}).get("id")
            if _ckpt_uuid:
                _pre = {int(r["chapter_index"]): r["content"]
                        for r in await _db.get_narasi_chapter_contents(tenant_id, _ckpt_uuid)
                        if r.get("content")}
                if _pre:
                    log.info("narrate_chapters: resuming — %d/%d chapters checkpointed", len(_pre), total)

                # ── ASSIST: NEVER REUSE A CHECKPOINT ─────────────────────────
                #
                # 🔴 RESUMED TEXT IS TEXT SOME EARLIER RUN WROTE, under some canon
                #    and some context. Splicing it in yields a book that is
                #    canonical in the chapters that happened to be regenerated and
                #    not in the ones that were not — the exact defect assist exists
                #    to remove, and invisible afterwards because the output looks
                #    complete.
                #
                #    Stage 1 settles this the blunt way: regenerate everything.
                #    Deciding per chapter would need each checkpoint to carry its
                #    own durable binding, and there is nowhere to put one without a
                #    schema of its own — narration's chapter rows have no field for
                #    it, and borrowing an unrelated column would be overwritten at
                #    finalisation anyway. Regenerating costs money; the alternative
                #    costs the claim. A per-chapter binding is Stage 2's problem,
                #    alongside the chapter-text byte binding it already owns.
                if _cl_assist and _pre:
                    log.warning(
                        "canon lite: assist ignores %d checkpointed chapter(s) — a "
                        "checkpoint carries no canon binding, so it cannot be shown "
                        "to belong to this run; regenerating all of them", len(_pre))
                    _pre = {}
        except Exception as _re:  # noqa: BLE001
            log.warning("resume preload failed (non-fatal): %s", _re)
            _ckpt_uuid, _pre = None, {}

    async def _bounded(no: int, ch: dict) -> dict[str, Any]:
        if no in _pre:   # S2: checkpointed on a previous attempt — reuse, zero spend
            # Resumed content bypasses _write_chapter entirely, so it must get the same
            # leak scrub freshly-generated chapters get below — otherwise a checkpoint
            # captured before the scrub existed (or containing a leak some other way)
            # would ship un-scrubbed forever.
            # Under assist `_pre` is empty, so this branch is unreachable there and
            # no delivered chapter can escape the census by being "resumed".
            _resumed_output = _scrub_chapter_leaks(_pre[no], task_id=f"ch{no + 1}")
            return {"ok": True, "output": _resumed_output, "no": no,
                    "model": w_model, "resumed": True}
        async with sem:
            res = await _write_chapter(
                ctx=ctx, ch=ch, no=no, total=total,
                style=style, language=language, mode=mode, job_id=job_id,
                worker_model=w_model, timeout=worker_timeout,
                telemetry_sink=telemetry_sink,
                # F1: ASSIST ONLY. `_cl_canon` is non-None under shadow too (shadow
                # builds one for its own projection report), and an earlier version of
                # this line passed it unconditionally — which made the early scrub
                # rewrite user-visible output on shadow jobs, breaking the one promise
                # shadow makes. Only assist injects a canon into a generation prompt,
                # so only assist can leak one. `_write_chapter` derives its scrub from
                # `canon_text` as well, so this gate and that one both have to fail
                # before shadow could mutate anything.
                canon=(_cl_canon if _cl_assist else None),
                canon_text=_cl_canon_text, canon_prompt_sha=_cl_gen_prompt_sha,
                canon_sha256=_cl_canon_sha, context_sha256=_cl_ctx_sha,
            )
        if _resume_on and _ckpt_uuid and res.get("ok") and res.get("output"):
            try:
                import database as _db
                # Checkpoint the GATED text (markers stripped too): the classic stitch
                # falls back to these rows while the job is still in its gates phase, and
                # the Diponegoro user downloaded raw [VERIFY]/[BEAT] residue that way.
                # Whatever leaves this process durably must already be clean.
                _ck_txt = res["output"]
                if _ngate is not None:
                    try:
                        _ck_txt, _ckr = _ngate.gate_text(_ck_txt, lang=language)
                    except TypeError:
                        _ck_txt, _ckr = _ngate.gate_text(_ck_txt)
                await _db.save_narasi_chapter(
                    tenant_id, _ckpt_uuid, no, _ck_txt,
                    len(str(_ck_txt).split()), "", [])
            except Exception as _ce:  # noqa: BLE001
                log.warning("chapter checkpoint failed (non-fatal): %s", _ce)
        return res

    _t_map = time.monotonic()  # timing: chapter MAP phase (Rino 2026-07-06)
    _cl_fail_code: Optional[str] = None   # set by the assist census below
    _cl_bound_n = 0                       # chapters the census actually vouched for
    tasks = [asyncio.ensure_future(_bounded(i, ch)) for i, ch in enumerate(chapters)]

    # Consume as_completed (so a slow chapter doesn't block logging of fast ones),
    # then SORT BY CHAPTER NUMBER to restore book order — the map-reduce invariant.
    raw: list[dict[str, Any]] = []
    try:
        try:
            for fut in asyncio.as_completed(tasks):
                try:
                    raw.append(await fut)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    # _write_chapter is never-raise, but preserve the legacy belt+braces.
                    log.warning(
                        "narrate_chapters: a chapter task raised unexpectedly: %s", exc)
                    raw.append(
                        {"ok": False, "output": None, "error": str(exc), "no": -1})
        finally:
            # C10: on ANY exit that still has work in flight — outer cancellation, a
            # deadline, an unexpected raise — cancel and JOIN the survivors before
            # unwinding. On the normal path this is a zero-await no-op.
            await _drain_chapter_tasks(tasks, why="map exit")
        log.info("narrate_chapters: MAP done — %d chapters in %.1fs (max_parallel=%s)",
                 len(tasks), time.monotonic() - _t_map, max_parallel)
        _cl_freeze_ok = True
        if _cl_freeze is not None:
            # I08: the context handed to every worker must be the same object it was at
            # fan-out. Report-only in shadow (§9: shadow makes no user-visible change);
            # under assist it is one of the four conditions that fail the job.
            _cl_ok, _cl_codes = _cl_freeze.verify()
            if not _cl_ok:
                _cl_freeze_ok = False
                (log.error if _cl_assist else log.warning)(
                    "canon lite: shared-context freeze violation %s", list(_cl_codes))
            else:
                log.info("canon lite: shared-context freeze intact across MAP")

        # ── ASSIST: the post-MAP census. NOTHING HERE IS ADVISORY ────────────
        #
        # 🔴 THE HASHES ARE COLLECTED FROM THE WORKERS, NOT ASSERTED AT DISPATCH.
        #    Checking the value we sent proves only that we computed it once;
        #    each worker re-hashes the prefix it actually received and re-digests
        #    the context it actually read, so a prefix that was truncated,
        #    re-encoded or rebuilt per chapter — or a context that moved and moved
        #    back — shows up here.
        #
        # 🔴 AND A VIOLATION FAILS THE JOB. Logging it and returning success ships
        #    the defect: the book assembles, the credits settle, and the only trace
        #    that the guarantee did not hold is a line in a log nobody reads until
        #    a reader complains about continuity. Assist held for every delivered
        #    chapter, or this is not a successful job. There is no third outcome.
        #
        #    "Delivered" is the right denominator, not "attempted": a chapter that
        #    failed generation contributes no text to the book, and failing the
        #    whole job over it would change legacy partial-success behaviour for a
        #    reason that has nothing to do with the canon.
        if _cl_assist:
            _used = [r for r in raw if r.get("ok") and r.get("output")]
            _n_missing = sum(
                1 for r in _used
                if not r.get("canon_generation_prompt_sha256_seen")
                or not r.get("canon_sha256")
                or not r.get("context_sha256_seen"))
            _p_seen = {r.get("canon_generation_prompt_sha256_seen") for r in _used}
            _c_seen = {r.get("canon_sha256") for r in _used}
            _x_seen = {r.get("context_sha256_seen") for r in _used}
            for _s in (_p_seen, _c_seen, _x_seen):
                _s.discard(None)

            if not _cl_freeze_ok:
                _cl_fail_code = "canon_lite_assist_freeze_violation"
            elif _n_missing:
                # A chapter with no hash is not a chapter that passed — it is a
                # chapter nothing was measured on. Silence is not evidence.
                log.error("canon lite: %d of %d delivered chapter(s) carry NO canon "
                          "binding — assist cannot account for them",
                          _n_missing, len(_used))
                _cl_fail_code = "canon_lite_assist_binding_missing"
            # The census compares the GENERATION hash: that is what was injected and
            # what each worker re-measured off its own prefix. The canonical hash is
            # not part of this comparison — nothing injects it, so no worker sees it.
            elif (_p_seen - {_cl_gen_prompt_sha}) or (_c_seen - {_cl_canon_sha}):
                # Exact, and against the DISPATCHED value: "all workers agree with
                # each other" is satisfied by every worker being wrong the same way.
                log.error("canon lite: delivered chapters do not all carry the "
                          "dispatched canon (distinct prompt=%d canon=%d) — "
                          "assist guarantees do not hold", len(_p_seen), len(_c_seen))
                _cl_fail_code = "canon_lite_assist_binding_mismatch"
            elif _x_seen - {_cl_ctx_sha}:
                log.error("canon lite: a delivered chapter observed a shared context "
                          "that is not the frozen baseline (distinct=%d) — "
                          "assist guarantees do not hold", len(_x_seen))
                _cl_fail_code = "canon_lite_assist_context_mismatch"
            else:
                _cl_bound_n = len(_used)
                log.info("canon lite: assist census PASS — %d delivered chapter(s) "
                         "canon_sha256=%s generation_prompt_sha256=%s "
                         "context_sha256=%s",
                         _cl_bound_n, _cl_canon_sha, _cl_gen_prompt_sha, _cl_ctx_sha)
    finally:
        # Cancellation/error must not leave the context frozen for a retry or caller.
        if _cl_freeze is not None:
            _cl_freeze.release()
            _cl_freeze = None

    # The census verdict, acted on after the freeze is released so the caller gets a
    # normal context back either way. The chapters are already written and paid for —
    # that is unavoidable, the evidence only exists once the workers have reported —
    # but the job does not get to call itself successful on text whose canon binding
    # it cannot vouch for.
    if _cl_fail_code:
        log.error("canon lite: assist guarantees do not hold (%s); "
                  "job returns non-success rather than delivering unverified text",
                  _cl_fail_code)
        return {
            "ok": False, "chapters": [], "book": "", "polished": False,
            "rag_used": False, "context": {}, "strategy": "narrate_chapters",
            "error": _cl_fail_code,
        }

    raw.sort(key=lambda r: r.get("no", 0))

    chapter_records: list[dict[str, Any]] = []
    for i, r in enumerate(raw):
        no = r.get("no", i)
        ch = chapters[no] if 0 <= no < total else {}
        ok = bool(r.get("ok")) and bool(r.get("output"))
        # R-FG4/5/6 per-chapter gate on real output only — the failure placeholder IS a
        # deliberate bracket signal and must survive (the gate whitelists it anyway).
        content = _gate(r.get("output"), lang=language) if ok else _placeholder(ch, no, str(r.get("error", "unknown")))
        chapter_records.append({
            "no": no,
            "id": str(ch.get("id", no + 1)),
            "title": str(ch.get("title", "") or ""),
            "content": content,
            "ok": ok,
            "model": r.get("model", w_model),
            "error": None if ok else r.get("error", "unknown"),
            "cache_key": r.get("cache_key"),
        })

    n_ok = sum(1 for c in chapter_records if c["ok"])

    # F1: markers removed at GENERATION time, from chapters whose text is actually
    # DELIVERED. Summed here, from each worker's own reported count, AFTER the MAP has
    # been gathered and sorted — never a counter the concurrent fan-out mutates, so
    # the arithmetic cannot depend on scheduling order.
    #
    # 🔴 THE `ok`/`output` FILTER IS THE POINT, NOT A TIDY-UP. A failed worker's text
    #    is replaced wholesale by `_placeholder(...)` a few lines above and never
    #    reaches the reader; counting a scrub performed on prose that was then thrown
    #    away would inflate a number an operator reads as "this is what we had to
    #    clean out of the book". Same predicate the assist census uses for `_used`,
    #    for the same reason: delivered is the honest denominator.
    _f1_generation_removed = sum(
        int(r.get("f1_markers_removed") or 0)
        for r in raw if r.get("ok") and r.get("output"))

    # POST-MAP CHAPTER-BOUNDARY CONTINUITY CHECK (NARASI_CHAPTER_BOUNDARY_CHECK, default
    # OFF): chapters MAP in genuine parallel (asyncio.ensure_future, gathered via
    # as_completed above) — no chapter ever sees another chapter's ACTUAL generated prose,
    # only the static outline's summary (ctx.scope_for, built pre-MAP). Confirmed real-world
    # consequence: ch1 ended with a character taking a train alone at night and arriving;
    # ch2 opened re-staging the same inciting incident from scratch with a different
    # character taking a car in the morning. Making generation sequential would fight the
    # MAP-parallelism perf work — out of scope. CHEAP option instead: for each adjacent
    # chapter pair, ONE bounded cheap-model call judges whether N+1's opening contradicts or
    # redundantly re-stages something N's ending already resolved. REPORT-ONLY — logs a
    # WARNING, never revises (matches every other gate in this file). Bounded at exactly
    # total-1 calls (loop below), skipping pairs where either side failed to generate.
    # Never raises.
    # _bc_findings feeds the "chapter_boundary_report" key on this function's return
    # dict below (populated only when a "broken": true finding fires) — narration_api.py's
    # _r7_actuator_violations() reads it from there (same result-dict-plumbing pattern
    # numeric_ledger_report/domain_plausibility_report already use) to build an actionable
    # violation, gated behind NARASI_CHAPTER_BOUNDARY_ENFORCE. Stays [] (key omitted from the
    # log-only default) unless the ENFORCE flag route actually needs it.
    _bc_findings: list[dict] = []
    if str(os.environ.get("NARASI_CHAPTER_BOUNDARY_CHECK", "0")).strip().lower() in ("1", "true", "yes", "on"):
        try:
            if total >= 2:
                from laozhang_api import _narasi_cheap_call as _bc_call, _narasi_parse_json as _bc_parse
                _bc_sys = (
                    "You are checking two adjacent chapters of a narrative for a continuity "
                    "break at the seam between them. You are given the ENDING of chapter N and "
                    "the OPENING of chapter N+1. Question: does the OPENING of chapter N+1 "
                    "CONTRADICT or REDUNDANTLY RE-STAGE something the ENDING of chapter N already "
                    "resolved, OR skip a required causal/location/time bridge so it assumes an "
                    "off-page decision, journey, reconciliation, arrival, or large time jump? "
                    "Also mark broken when a bounded duration/deadline suddenly expires without "
                    "the adjacent prose accounting for the elapsed interval. Examples include "
                    "re-introducing an event that already happened with different details, or "
                    "jumping straight from considering a plan to executing it at a new location. "
                    "Return ONLY JSON: {\"broken\": true|false, "
                    "\"reason\": \"<one sentence, or empty if broken=false>\"}")
                # Number-keyed lookup, NOT positional list indexing: the exception handler
                # above can append a "no": -1 placeholder for a chapter task that raised,
                # and since chapter_records is sorted by "no", that -1 entry sorts to the
                # very front and shifts every subsequent record's LIST POSITION out of sync
                # with its actual chapter number — positional chapter_records[_bi]/[_bi+1]
                # access would then silently compare a non-adjacent pair (e.g. ch2 vs ch4,
                # skipping ch3) without ever raising. Keying by "no" sidesteps that entirely.
                _bc_by_no = {c["no"]: c for c in chapter_records if c.get("no", -1) >= 0}
                for _bi in range(total - 1):
                    _bc_a, _bc_b = _bc_by_no.get(_bi), _bc_by_no.get(_bi + 1)
                    if _bc_a is None or _bc_b is None:
                        continue   # missing chapter record (e.g. the -1 exception placeholder) — nothing adjacent to compare
                    if not (_bc_a.get("ok") and _bc_b.get("ok")):
                        continue   # a placeholder-failed chapter has nothing real to check
                    _bc_tail = " ".join((_bc_a.get("content") or "").split()[-300:])
                    _bc_head = " ".join((_bc_b.get("content") or "").split()[:300])
                    if not _bc_tail or not _bc_head:
                        continue
                    _bc_u = (f"CHAPTER {int(_bc_a.get('no', 0)) + 1} ENDING:\n{_bc_tail}\n\n"
                             f"CHAPTER {int(_bc_b.get('no', 0)) + 1} OPENING:\n{_bc_head}")
                    _bc_raw, _bc_cr = await _bc_call(_bc_sys, _bc_u, tenant_id=tenant_id,
                                                     user_id=None, job_uuid=None, json_mode=True)
                    _bc_d = _bc_parse(_bc_raw) if isinstance(_bc_raw, str) else (_bc_raw or {})
                    if isinstance(_bc_d, dict) and _bc_d.get("broken") is True:
                        _bc_a_no1 = int(_bc_a.get("no", 0)) + 1   # chapter A's 1-based number
                        _bc_b_no1 = int(_bc_b.get("no", 0)) + 1   # chapter B's 1-based number (the OPENING with the break)
                        log.warning(
                            "chapter-boundary check: ch %d -> ch %d looks like a continuity "
                            "break (%s)", _bc_a_no1, _bc_b_no1, str(_bc_d.get("reason") or "")[:200])
                        # Record the finding regardless of NARASI_CHAPTER_BOUNDARY_ENFORCE (that
                        # flag only controls whether narration_api.py converts it into a revise
                        # violation) — capturing it here is free and keeps this report-only.
                        _bc_findings.append({
                            "chapter_a": _bc_a_no1, "chapter_b": _bc_b_no1,
                            "head": _bc_head, "tail": _bc_tail,
                            "reason": str(_bc_d.get("reason") or "")[:200],
                        })
        except Exception as _bce:  # noqa: BLE001
            log.warning("chapter-boundary check failed (non-fatal): %s", _bce)

    # Assemble WITH a per-chapter heading so the result is classified per-bab while
    # the manager polish keeps the prose flowing. Heading uses the outline title
    # (1-based by position); chapters with no title get a bare "## Bab N".
    # Language-aware chapter label. Was hardcoded "Bab" (Indonesian) for EVERY language —
    # it shipped "## Bab N" into English books AND, because chapters see prior headings in
    # their context, taught the model to emit "## Bab N" mid-body (narasi-bed-of-orchid
    # part-2, 2026-07-06: a leaked "## Bab 4" fused into an English chapter). Source the
    # label from the canonical per-language map; fall back to "Chapter {n}".
    try:
        from laozhang_api import _narasi_header_labels as _nhl  # cycle-free at call time
        _chap_fmt = (_nhl(language) or {}).get("chapter", "Chapter {n}")
    except Exception:  # noqa: BLE001
        _chap_fmt = "Chapter {n}"
    def _chapter_md(c: dict[str, Any]) -> str:
        title = (c.get("title") or "").strip()
        try:  # F5: strip a leaked structural beat-label from the chapter title (cycle-free at call time)
            from laozhang_api import _strip_beat_label as _sbl
            title = _sbl(title)
        except Exception:  # noqa: BLE001
            pass
        try:
            label = _chap_fmt.format(n=int(c.get("no", 0)) + 1)
        except Exception:  # noqa: BLE001
            label = f"Chapter {int(c.get('no', 0)) + 1}"
        head = f"## {label}" + (f": {title}" if title else "")
        return f"{head}\n\n{(c.get('content') or '').strip()}"
    book = "\n\n".join(_chapter_md(c) for c in chapter_records)

    # 3) REDUCE — optional manager polish over the assembled book.
    _t_polish = time.monotonic()  # timing: POLISH phase (Rino 2026-07-06)
    # Floor manager_timeout the SAME way _scaled_timeout floors the per-chapter timeout: when a
    # failover chain is armed (legacy NARASI_FAILOVER_ENABLED, or NARASI_WORKER_KIE_FIRST now
    # that it also covers role=='manager' — Rino 2026-07-15), a hung early rung (cold KIE) must
    # not be killed by the flat 240s default before the chain can fall through to laozhang/
    # native. Same cold-KIE-hang class NARASI_BIBLE_SKIP_KIE and _scaled_timeout's own floor
    # exist to avoid, now reachable on the polish/manager path too. Applies to every internal
    # _polish_reduce call site (whole-book and each NARASI_POLISH_PARALLEL chunk alike) since
    # they all receive this same `timeout` value.
    _mgr_timeout = manager_timeout
    if (str(os.environ.get("NARASI_FAILOVER_ENABLED", "0")).strip().lower() in ("1", "true", "yes", "on")
            or str(os.environ.get("NARASI_WORKER_KIE_FIRST", "0")).strip().lower() in ("1", "true", "yes", "on")):
        _mgr_timeout = max(_mgr_timeout,
                           float(os.environ.get("NARASI_FAILOVER_CHAIN_BUDGET", "840")) + 90.0)
    polished_book, did_polish = await _polish_reduce(
        book=book,
        topic=topic,
        style=style,
        language=language,
        polish=polish,
        manager_model=m_model,
        timeout=_mgr_timeout,
        telemetry_sink=telemetry_sink,
        any_failures=(n_ok < total),
        authority_text=_narrative_authority_text,
    )
    # Print the EFFECTIVE polish model — _polish_reduce overrides manager_model
    # with NARASI_POLISH_MODEL when set, so `m_model` (passed-in) is misleading
    # (Rino 2026-07-06: log showed opus while env was sonnet). Mirror the same
    # env resolution here so the timing line is honest about opus-vs-sonnet.
    _eff_polish_model = (os.environ.get("NARASI_POLISH_MODEL") or "").strip() or m_model
    log.info("narrate_chapters: POLISH done in %.1fs (mode=%s, applied=%s, model=%s)",
             time.monotonic() - _t_polish, polish, did_polish, _eff_polish_model)
    # R-FG5 TERMINAL: the polish is the last LLM touch and can reintroduce brackets —
    # the deterministic gate must run AFTER it, so residue can never ship.
    polished_book = _gate(polished_book, lang=language)
    # POST-ASSEMBLY DEDUP GUARD (narasi round-16 postmortem, job nny3va3j): a chunked-polish
    # completion can echo/duplicate a chapter block under its own preserved heading. Regardless
    # of root cause, the final book must never carry two blocks for the same chapter number.
    polished_book, _n_dedup_dropped = _dedup_chapter_blocks(polished_book)
    if _n_dedup_dropped:
        log.warning("narrate_chapters: POST-ASSEMBLY dedup guard collapsed %d duplicate "
                    "chapter-heading block(s)", _n_dedup_dropped)

    return {
        "ok": n_ok > 0,
        "chapters": chapter_records,
        "book": polished_book,
        "raw_book": book,
        "polished": did_polish,
        "n_ok": n_ok,
        "n_total": total,
        "rag_used": ctx.rag_used,
        "context": ctx.as_dict(),
        "model": w_model,
        "manager_model": m_model,
        "strategy": "narrate_chapters",
        # Private in-process transit only. The exact accepted outline + winning
        # Bible must survive the MAP/REDUCE return boundary so narration_api's
        # diet and consistency revise cannot operate under a weaker contract.
        # _result_payload never includes it, and narration_api pops it before
        # chapter persistence/finalisation.
        **({"_narrative_authority": _narrative_authority}
           if _narrative_authority is not None else {}),
        # L2a in-process transit only. This key exists only while shadow genuinely ran;
        # flag-off returns the byte/shape-identical legacy dict (C11). narration_api
        # consumes and removes it after every text mutation and before persistence or the
        # bounded result payload. It is never serialized, logged, or sent to the user.
        # 🔴 ASSIST NEEDS THIS AS MUCH AS SHADOW DOES. Forwarding it for shadow
        #    only left the real assist path receiving `canon=None` at the L3 seam,
        #    where a canon-less report has no semantic authority, no violations and
        #    therefore nothing to repair: assist would have been a permanent,
        #    silent no-op in production while every seam test passed by handing the
        #    canon in directly. Still IN-PROCESS TRANSIT ONLY — narration_api pops
        #    both keys before persistence and before the bounded payload.
        **({"_canon_lite_canon": _cl_canon,
            "_canon_lite_canon_status": _cl_status}
           if _cl_mode in ("shadow", "assist") else {}),
        # F1: generation-time removal subtotal, ASSIST ONLY (nothing is scrubbed under
        # shadow/off, so the key would be a meaningless 0 there). narration_api folds
        # the L3-candidate and final-seam counts in beside it to publish
        # `f1_scrub_operations_count`. This subtotal alone is filtered to DELIVERED
        # chapters — see the sum above for why that filter is load-bearing here.
        **({"f1_generation_markers_removed": _f1_generation_removed}
           if _cl_assist else {}),
        # ASSIST ONLY — the durable canon binding of this job. Persisted through
        # `_result_payload`, so the binding survives even when chapter checkpoints
        # are off (NARRATION_RESUME_ENABLED=0) and nothing else would record what
        # this book was written against. Present only under assist: flag-off must
        # keep returning the shape-identical legacy dict (C11).
        # Hashes and one count — no name, title, literal or prose (C12/§10).
        # BOTH prompt identities, under names that say which is which (2026-08-15
        # re-audit round 2). `canon_prompt_sha256` keeps its ORIGINAL a6711e0 meaning:
        # the hash of the canonical, id-bearing `render_canon()`, which QC/L3 still
        # use. F1 changed what gets INJECTED (to the id-free generation projection)
        # and an earlier round published that new hash under the old field's name,
        # silently redefining a contract nobody was told had changed — then a later
        # round "fixed" it by deleting the old field outright, which is the same
        # break in the other direction. Both are published, neither is redefined.
        **({"canon_lite_binding": {
            "mode": "assist",
            "canon_sha256": _cl_canon_sha,
            "canon_prompt_sha256": _cl_canonical_prompt_sha,
            "canon_generation_prompt_sha256": _cl_gen_prompt_sha,
            "canon_generation_projection_version": _cl.GENERATION_PROJECTION_VERSION,
            "context_sha256": _cl_ctx_sha,
            "chapters_bound": _cl_bound_n,
        }} if _cl_assist else {}),
        # In-memory transit only (NOT persisted into the bounded _result_payload): the pinned
        # story bible, so the downstream consistency critic can diff each chapter against the
        # committed canon when NARASI_CANON_CONFORMANCE is on. as_dict() exposes only the char
        # count, so the raw text otherwise dies here.
        "canonical_facts": ctx.canonical_facts,
        # P0-B: the advisory `canon_registry` sidecar, threaded from the structured Story
        # Bible response to `narration_api`'s canon-diff. `None` on every non-structured
        # path, where the registry still rides as a fence inside `canonical_facts` and the
        # consumer's own regex finds it exactly as before — so this key ADDS a source, it
        # does not replace one. Without it the single-object contract would silently strip
        # the registry out of the prose and push the diff loop into its PAID LLM
        # re-extraction fallback.
        "canon_registry": _cl_canon_registry,
        "facts_are_bible": ctx.facts_are_bible,
        # NARASI_CHAPTER_BOUNDARY_CHECK's findings (see _bc_findings above) — [] unless the
        # flag is on AND at least one break fired. Same pattern numeric_ledger_report /
        # domain_plausibility_report use: narration_api.py's _r7_actuator_violations() reads
        # this key off `result` to build an actionable violation under NARASI_CHAPTER_
        # BOUNDARY_ENFORCE. Present unconditionally (even when empty) so downstream code can
        # always do a plain `.get("chapter_boundary_report")` without an extra existence check.
        "chapter_boundary_report": {"breaks": _bc_findings},
    }


# ===========================================================================
# Polish reducer — the 3-mode REDUCE (none / light / heavy).
# Shared by narrate_chapters() and the router. A standalone helper so the router
# can apply the same semantics to any strategy's output.
# ===========================================================================
_HEAVY_POLISH_PROCEDURE = """MANDATORY HEAVY-POLISH PROCEDURE
(The authoritative Outline and Story Bible remain the factual authority.)

1. FACT AUTHORITY
Preserve every fact fixed by the Outline and Story Bible. Do not invent plot events or choose a conflicting variant without authority.

2. BOUNDARY INSPECTION
Treat the final two paragraphs before each chapter heading and the first two paragraphs after it as one editable seam.

3. BRIDGE REPAIR
If the seam is broken, add at most one or two short paragraphs immediately before the next chapter heading. Show only the necessary cause or decision, location change, and elapsed time.

4. SEAM DEDUPLICATION
When adding a bridge, remove or compress equivalent transition setup after the heading. Preserve the first unique plot action.

5. EVIDENCE PROVENANCE
For every recurring evidence object, enforce one origin, hiding place, finder, acquisition event, and custody chain from the Story Bible EVIDENCE MAP. A legal challenge must match the acquisition actually depicted. If authority is silent, preserve the earliest on-page acquisition unless the Outline explicitly says otherwise.

6. HARD PRESERVATION
Preserve every unique plot event, action beat, scene outcome, and evidentiary fact. Retain at least 80% of the input word count. Do not move beats, repeat setup, create new scenes, or alter, remove, rename, renumber, translate, or move any chapter heading.

7. NO-OP WHEN CLEAN
If a boundary or evidence chain is already consistent, leave it unchanged."""


def _polish_instruction(mode: str, topic: str, language: str, *, is_chunk: bool = False):
    """Build (instruction, synthesize-role) for a polish pass. is_chunk swaps 'book'→'section'
    so a chunk pass does not think it is the whole book."""
    unit = "section of a multi-chapter narrative" if is_chunk else "multi-chapter narrative"
    ret = "edited section" if is_chunk else "edited book"
    if mode == "heavy":
        instruction = (
            f"You are the editor-in-chief doing a HEAVY final edit of a {unit} about \"{topic}\". "
            "Reconcile contradictions and remove cross-chapter repetition and re-introductions.\n\n"
            f"{_HEAVY_POLISH_PROCEDURE}\n\n"
            "FINAL OUTPUT\n"
            f"Return ONLY the {ret} in {language}, no notes.")
        return instruction, "synthesize"
    instruction = (
        f"You are the editor-in-chief doing a LIGHT final pass of a {unit} about \"{topic}\". "
        "ONLY smooth the seams between chapters, remove obvious cross-chapter repetition, and keep "
        "the register consistent. Do NOT rewrite content, do NOT change any fact, name, date or "
        "number, do NOT shorten the text. Keep every chapter-heading line (each begins with `## `) "
        "exactly as given — do not remove, rename, renumber, translate or move them, and never "
        f"write a new heading of your own. Return ONLY the lightly-edited {ret} in {language}.")
    return instruction, "polish"


def _polish_system(authority_text: str) -> str:
    """Put narrative authority at system priority without changing legacy prompts."""
    authority_text = str(authority_text or "").strip()
    if not authority_text:
        return ""
    return (
        "You are a meticulous senior editor. The narrative authority below is "
        "immutable. An editing request may improve prose only inside it; if an edit "
        "would conflict, preserve the original prose instead.\n\n"
        + authority_text
    )


# `chapter_heading_patterns.chapter_split_rx_for` (imported as `_chp` above) picks the right
# regex for a given book — see that module's docstring for the full "why", including the
# 2026-08-15 adversarial review that found the first version of this fix (a single always-
# broadened regex shared with `canon_lite_l2._CHAPTER_SPLIT_RX` by hand-copy, `(?i)` and `\b`
# both present, no two-phase selection) let a bare-word false match land inside an already-
# "## "-marked manuscript's body prose, leaking an orphaned fragment of a genuinely-duplicated
# chapter past `_dedup_chapter_blocks` below, and letting `_split_into_chunks` cut a real
# chapter's polish pass across two seam-blind chunks. `chapter_split_rx_for` structurally
# prevents both: the permissive fallback only ever runs on a book already proven to contain
# zero "## " markers, never as a supplement within one that does.

# AUDIT FIX (r16): the bare `(\d+)` capture collapsed "Chapter 3a"/"Chapter 3.5" into the
# SAME key as plain "Chapter 3" — a genuinely distinct chapter would then be silently DELETED
# as a false "duplicate". The negative lookahead rejects a match immediately followed by a
# lowercase letter or a decimal point, so "3a"/"3.5" no longer match at all (nums[i]=None,
# which the "fails safe" branch below always KEEPS) while plain "Chapter 3:"/"Chapter 3 —"
# still match normally.
#
# KNOWN GAP, not fixed here: still requires literal "## ", unlike
# `chapter_heading_patterns.BARE_WORD_RX`. Same deferred follow-up as
# canon_lite_l2._CH_HEADER_NUM_RX (different digit *position* per language, more than this
# pass's split-only scope). A part that only matched the split via a bare-word opener always
# parses as `None` here, and `None` is already the documented "fails safe, never a false
# duplicate" case below, so this is inert for such a manuscript, not silently wrong — dedup
# just does not fire on it yet.
_CH_HEADER_NUM_RX = re.compile(r"^##\s+\D*?(\d+)(?![a-z.])")


def _dedup_chapter_blocks(book: str) -> tuple[str, int]:
    """Defense-in-depth safety net (narasi round-16 postmortem, job nny3va3j): scan the final
    assembled/polished book for repeated '## <label> N' chapter headings and keep only the LAST
    occurrence of each chapter number, dropping earlier duplicate blocks — regardless of root
    cause (a bloated polish-chunk echo — see the upper-bound guard in _polish_one/_polish_reduce
    — a stale resume, or anything else). Splits on the same '^## ' boundary _split_into_chunks
    already trusts, so behavior stays consistent with the chunker. Preserves original relative
    order (a pure filter, never a re-sort), so a correctly-ordered book is byte-identical.
    _CH_HEADER_NUM_RX still requires literal "## " (see its own KNOWN GAP comment above), so a
    part that only split via the bare-word fallback always parses as None here — fails safe,
    never mistaken for a duplicate. Never raises; returns (book, 0) when every heading number
    is already unique or none are found."""
    try:
        parts = [p for p in _chp.chapter_split_rx_for(book).split(book) if p.strip()]
        if len(parts) <= 1:
            return book, 0
        nums: list[Optional[int]] = []
        for p in parts:
            m = _CH_HEADER_NUM_RX.match(p.split("\n", 1)[0].strip())
            nums.append(int(m.group(1)) if m else None)
        # AUDIT FIX (r16), then RE-VERIFIED and CORRECTED against the real manuscript: the
        # audit raised a valid hypothetical ("keep LAST" could retain a corrupted/truncated
        # duplicate appended AFTER a good original) and an initial fix switched to "prefer the
        # LONGEST occurrence" unconditionally — but empirically re-testing that against the
        # REAL bug (4 real duplicated chapters) showed it picks the WRONG (stale/duplicate,
        # EARLIER) occurrence in 3 of 4 cases, because two independent regenerations of the
        # same chapter differ in length by mere noise (a few dozen words either way — 3235 vs
        # 3217, or an exact tie), which carries no correctness signal. "Keep last" alone
        # correctly resolved all 4/4 real cases (the pipeline's later generation is the
        # authoritative one). Default back to "keep last"; ONLY override it when the last
        # occurrence is drastically shorter than the best earlier one (looks truncated, not
        # just ordinary length variance) — narrow protection for the audit's scenario without
        # being noise-sensitive for the actual observed failure mode.
        occurrences: dict[int, list[int]] = {}
        for i, n in enumerate(nums):
            if n is not None:
                occurrences.setdefault(n, []).append(i)
        keep_idx: dict[int, int] = {}
        for n, idxs in occurrences.items():
            last_i = idxs[-1]
            if len(idxs) == 1:
                keep_idx[n] = last_i
                continue
            best_i = max(idxs, key=lambda i: len(parts[i].split()))
            last_words = len(parts[last_i].split())
            best_words = len(parts[best_i].split())
            keep_idx[n] = best_i if last_words < best_words * 0.5 else last_i
        keep = [i for i, n in enumerate(nums) if n is None or keep_idx[n] == i]
        if len(keep) == len(parts):
            return book, 0        # nothing duplicated
        return "\n\n".join(parts[i].strip() for i in keep), len(parts) - len(keep)
    except Exception:  # noqa: BLE001 - a dedup bug must never break generation
        return book, 0


def _split_into_chunks(book: str, chunk_words: int):
    """Split the assembled book into chapter-aligned chunks each <= chunk_words words. Splits
    only at real chapter headings (`chapter_heading_patterns.chapter_split_rx_for` — "## " if
    the book has any, the bare-word fallback only if it has none; never both within one book,
    which is what keeps this "never mid-chapter" for a book that already uses "## "); a single
    chapter larger than chunk_words becomes its own oversized chunk. Returns [book] when there
    is nothing to split."""
    parts = [p for p in _chp.chapter_split_rx_for(book).split(book) if p.strip()]
    if len(parts) <= 1:
        return [book]
    chunks, cur, cur_w = [], [], 0
    for p in parts:
        w = len(p.split())
        if cur and cur_w + w > chunk_words:
            chunks.append("\n\n".join(c.strip() for c in cur))
            cur, cur_w = [], 0
        cur.append(p)
        cur_w += w
    if cur:
        chunks.append("\n\n".join(c.strip() for c in cur))
    return chunks


_POLISH_PREVIOUS_TAIL_WORDS = 300
_POLISH_TAIL_LEAK_WINDOW_WORDS = 20
_POLISH_BRIDGE_MAX_WORDS = 160
_POLISH_TAIL_LABEL = "READ-ONLY PREVIOUS-CHAPTER TAIL (context only; never reproduce)"


def _bounded_polish_tail(text: str, max_words: int = _POLISH_PREVIOUS_TAIL_WORDS) -> str:
    """Return the bounded tail of the previous chunk's final chapter only."""
    raw = str(text or "")
    parts = [p for p in _chp.chapter_split_rx_for(raw).split(raw) if p.strip()]
    final_chapter = parts[-1] if parts else raw
    words = final_chapter.split()
    return " ".join(words[-max(0, int(max_words)):]) if words and max_words > 0 else ""


def _polish_heading_records(text: str) -> tuple[tuple[str, int], ...]:
    """Return each detected heading line and its byte-preserving string offset, in order."""
    raw = str(text or "")
    records: list[tuple[str, int]] = []
    for match in _chp.chapter_split_rx_for(raw).finditer(raw):
        start = match.start()
        end = raw.find("\n", start)
        if end < 0:
            end = len(raw)
        records.append((raw[start:end], start))
    return tuple(records)


def _previous_tail_leaked(candidate: str, previous_tail: str, original: str = "") -> bool:
    """Detect a verbatim tail echo without a model/parser call.

    A 20-word sliding window is long enough to avoid ordinary phrase collisions while
    catching the harmful failure mode: the model copying READ-ONLY context into the bridge
    or editable chunk. Short tails are checked in full. Only occurrences added beyond the
    original chunk count as a leak, so an existing deliberate refrain does not waste the call.
    """
    candidate_text = " ".join(str(candidate or "").split()).casefold()
    original_text = " ".join(str(original or "").split()).casefold()
    if _POLISH_TAIL_LABEL.casefold() in candidate_text:
        return True
    tail_words = str(previous_tail or "").split()
    if not candidate_text or not tail_words:
        return False
    window = min(_POLISH_TAIL_LEAK_WINDOW_WORDS, len(tail_words))
    for start in range(len(tail_words) - window + 1):
        phrase = " ".join(tail_words[start:start + window]).casefold()
        if phrase and candidate_text.count(phrase) > original_text.count(phrase):
            return True
    return False


def _heavy_chunk_rejection_reason(
    original: str,
    candidate: str,
    previous_tail: str,
) -> Optional[str]:
    """Return a bounded reason when a HEAVY chunk candidate is unsafe to accept."""
    expected = _polish_heading_records(original)
    actual = _polish_heading_records(candidate)
    if tuple(line for line, _ in actual) != tuple(line for line, _ in expected):
        return "heading_sequence_changed"
    if not actual:
        return "heading_sequence_missing"

    bridge = candidate[:actual[0][1]].strip()
    if bridge:
        if not previous_tail:
            return "bridge_without_previous_context"
        paragraphs = [p for p in re.split(r"\n\s*\n", bridge) if p.strip()]
        if len(paragraphs) > 2 or len(bridge.split()) > _POLISH_BRIDGE_MAX_WORDS:
            return "bridge_prefix_too_large"
    if previous_tail and _previous_tail_leaked(candidate, previous_tail, original):
        return "previous_tail_leaked"
    return None


_HEAVY_INCOMING_SEAM_INSTRUCTION = (
    "\n\nINCOMING CROSS-CHUNK SEAM: the system message supplies a bounded READ-ONLY "
    "tail from the already-polished previous chapter. Inspect that tail against the first "
    "chapter opening already present in the editable section. Never quote, paraphrase, "
    "summarize, or reproduce the tail. If and only if the seam is broken, emit at most one "
    "or two short bridge paragraphs immediately BEFORE the editable section's first chapter "
    "heading, followed by the complete edited section. That optional prefix is appended to "
    "the previous chapter on rejoin; it must contain no heading. If the seam is already "
    "clear, begin directly with the unchanged first chapter heading and emit no prefix."
)


def _polish_retention_floor(input_words: int, min_retained_percent: int) -> int:
    """Return the accepted-output floor without changing legacy LIGHT rounding."""
    if min_retained_percent == 75:
        return int(input_words * 0.75)
    return (input_words * min_retained_percent + 99) // 100


async def _polish_one(text, *, instruction, role, model, timeout, telemetry_sink, task_id,
                      authority_text: str = "", previous_chapter_tail: str = "",
                      guard_heavy_chunk: bool = False, min_retained_percent: int = 75):
    """Polish ONE blob (whole book or a chunk) via synthesize. Returns (out, ok). Post-
    truncation guard (legacy LIGHT >=75%; HEAVY >=80%) keeps the original on a cut/degraded pass.
    Post-bloat guard (<=135% words) does the SAME for the opposite failure: neither a "light"
    (preserve length) nor a "heavy" (same-length reconciling edit) polish instruction should
    legitimately double a chunk's length — a materially LONGER output is exactly as anomalous as
    a shorter one and was previously accepted unconditionally. narasi round-16 postmortem (job
    nny3va3j): a long-input polish chunk degenerated on a long-context failure mode (the
    instruction requires preserving
    every "## Chapter N" heading exactly, and the model echoed/re-derived the section instead of
    only editing it), producing one completion with each "## Chapter N" heading TWICE — silently
    accepted, landing as a 14K-word divergent duplicate of Ch1-4 prepended to the real book.
    HEAVY chunk callers may additionally supply a read-only previous tail and require exact
    heading-sequence preservation; the tail never enters ``wrapped`` editable text. Never raises."""
    wrapped = [{"ok": True, "output": text, "model": model}]
    _t = time.monotonic()
    system_text = _polish_system(authority_text)
    if previous_chapter_tail:
        tail_block = (
            f"{_POLISH_TAIL_LABEL}:\n"
            "Use this only to judge the incoming seam. It is not editable manuscript text. "
            "Do not quote, paraphrase, summarize, or reproduce any of it in the response.\n"
            "--- BEGIN READ-ONLY TAIL ---\n"
            f"{previous_chapter_tail}\n"
            "--- END READ-ONLY TAIL ---"
        )
        # Keep narrative authority positionally last/highest, matching its own contract.
        system_text = f"{tail_block}\n\n{system_text}" if system_text else tail_block
    res = await synthesize(
        instruction, wrapped, role=role, model=model,
        system=system_text, timeout=timeout,
        telemetry_sink=telemetry_sink, task_id=task_id)
    _tel = res.get("telemetry") or {}
    log.info("_polish_reduce: %s role=%s model=%s served_by=%s ok=%s in %.1fs (tok_in=%s tok_out=%s)",
             task_id, role, model, _tel.get("provider") or "?", res.get("ok"),
             time.monotonic() - _t, _tel.get("tokens_in"), _tel.get("tokens_out"))
    if res.get("ok") and res.get("output"):
        out = str(res["output"])
        if guard_heavy_chunk:
            rejection = _heavy_chunk_rejection_reason(text, out, previous_chapter_tail)
            if rejection:
                log.warning("_polish_reduce: %s rejected (%s) — keeping original chunk",
                            task_id, rejection)
                return text, False
        _in_w, _out_w = len(text.split()), len(out.split())
        # A bridge is additive seam repair, not replacement manuscript. Exclude it from
        # the retention numerator so a large allowed prefix cannot mask a truncated
        # editable chunk. Keep the 135% ceiling on the COMPLETE output so the bridge gets
        # no free bloat allowance either.
        _retained_w = _out_w
        if guard_heavy_chunk:
            _records = _polish_heading_records(out)
            if _records:
                _retained_w = len(out[_records[0][1]:].split())
        # Preserve the legacy LIGHT rounding exactly. HEAVY uses a firm minimum:
        # ceiling(input * 80%) means each accepted integer word count is >=80%, while
        # an exact 80% remains admissible.
        _minimum_retained_w = _polish_retention_floor(
            _in_w, min_retained_percent
        )
        if _retained_w < _minimum_retained_w:
            log.warning("_polish_reduce: %s output %d words < %d%% of %d — discarding (truncation guard)",
                        task_id, _retained_w, min_retained_percent, _in_w)
            return text, False
        if _out_w > int(_in_w * 1.35):
            log.warning("_polish_reduce: %s output %d words > 135%% of %d — discarding (bloat/duplication guard)",
                        task_id, _out_w, _in_w)
            return text, False
        # A polish pass cut off mid-sentence by the token ceiling can clear the word-band checks
        # above (marginally short, not gutted) — catch it the same way _apply_word_gate already
        # does for the MAP phase: the provider's own stop reason, plus a deterministic check that
        # the blob's last non-whitespace isn't terminal punctuation.
        if _res_truncated(res):
            log.warning("_polish_reduce: %s finish_reason=%s — discarding (truncation guard)",
                        task_id, (_tel.get("finish_reason") or "?"))
            return text, False
        if not _TAIL_UNTERMINATED_RX.search(out):
            log.warning("_polish_reduce: %s output does not end in terminal punctuation — "
                        "discarding (mid-sentence truncation guard)", task_id)
            return text, False
        return out, True
    log.warning("_polish_reduce: %s failed (%s) — keeping unpolished", task_id, res.get("error"))
    return text, False


async def _polish_reduce(
    *,
    book: str,
    topic: str,
    style,
    language: str,
    polish: str,
    manager_model: str,
    timeout: float,
    telemetry_sink,
    any_failures: bool = False,
    authority_text: str = "",
):
    """3-mode polish reducer (none/light/heavy). When the assembled book exceeds the polish
    model's single-call OUTPUT ceiling, CHUNK it — split by chapter into <=NARASI_POLISH_CHUNK_WORDS
    groups, polish each, rejoin — instead of skipping, so a long book (up to NARASI_POLISH_MAX_WORDS,
    default the 40k generation cap) still gets a full pass. Cross-chunk boundaries are always
    chapter breaks. Each chunk has its own retention guard (HEAVY >=80%; LIGHT's legacy >=75%).
    Chunked HEAVY runs dependency-serial so each call can inspect the accepted preceding tail;
    this makes no extra provider calls but can increase latency. LIGHT keeps its existing
    parallel option. Never raises."""
    mode = (polish or "light").strip().lower()
    if mode == "none" or not book.strip():
        return book, False
    min_retained_percent = 80 if mode == "heavy" else 75
    if any_failures:
        log.info("_polish_reduce: skipping polish — book has failed-chapter placeholders")
        return book, False
    _env_polish = (os.environ.get("NARASI_POLISH_MODEL") or "").strip()
    if _env_polish:
        manager_model = _env_polish

    # Hard upper bound (chunking handles everything below it). Default = the 40k generation cap.
    _pmax = int(os.environ.get("NARASI_POLISH_MAX_WORDS", "40000"))
    _book_words = len(book.split())
    if _book_words > _pmax:
        log.info("_polish_reduce: skipping polish — book %d words > NARASI_POLISH_MAX_WORDS %d",
                 _book_words, _pmax)
        return book, False

    instruction, role = _polish_instruction(mode, topic, language)

    # Does the WHOLE book round-trip through the polish model's output ceiling in one call?
    _ceil = int(max_tokens_for(manager_model or "") or 0)
    _need = int(_book_words * 1.45 * 1.08)   # tokens to reproduce the book + slack
    if (not _ceil) or _need <= int(_ceil * 0.95):
        return await _polish_one(book, instruction=instruction, role=role, model=manager_model,
                                 timeout=timeout, telemetry_sink=telemetry_sink,
                                 task_id=f"polish:{mode}", authority_text=authority_text,
                                 min_retained_percent=min_retained_percent)

    # Too big for one call → CHUNK by chapter (default on; NARASI_POLISH_CHUNK=0 disables).
    if str(os.environ.get("NARASI_POLISH_CHUNK", "1")).strip().lower() not in ("0", "false", "no", "off"):
        _fit_words = int((_ceil * 0.95) / (1.45 * 1.08)) if _ceil else 18000   # words that fit one call
        # Cap the chunk to what ACTUALLY fits the ceiling — no floor. A floor would force
        # chunks bigger than a small-ceiling model can emit → every chunk truncates + is
        # discarded (guaranteed-wasted spend). Better: smaller chunks that each round-trip.
        chunk_words = min(int(os.environ.get("NARASI_POLISH_CHUNK_WORDS", "18000")), _fit_words)
        chunks = _split_into_chunks(book, chunk_words)
        if len(chunks) > 1:
            c_instr, c_role = _polish_instruction(mode, topic, language, is_chunk=True)
            polished, any_ok = [], False
            # ── PARALLEL LIGHT polish (NARASI_POLISH_PARALLEL>=2; default 0 = the serial loop below runs
            #    verbatim / byte-identical). LIGHT chunks are INDEPENDENT — each polishes its own chapter group,
            #    rejoined IN ORDER — so run them concurrently: N serial chunks (job rsmyws7s = 752s, of
            #    which 2 flaky chunks wasted ~458s) collapse to ~one wave (the slowest chunk). asyncio.gather
            #    preserves input order → identical rejoin. _polish_one NEVER raises (keeps its original
            #    chunk on truncation/timeout), so a bad chunk cannot abort the gather — its waste just
            #    OVERLAPS the good chunks instead of adding serially. HEAVY takes the dependency-serial
            #    branch below regardless of this flag. ──
            try:
                _pp = int(str(os.environ.get("NARASI_POLISH_PARALLEL", "0")).strip() or "0")
            except Exception:
                _pp = 0
            if mode == "heavy":
                # HEAVY chunks are intentionally dependency-serial: chunk N needs the
                # accepted output of chunk N-1 to inspect the real cross-chunk seam. This
                # keeps the provider-call count unchanged (one per chunk) but can increase
                # wall-clock latency versus NARASI_POLISH_PARALLEL; LIGHT retains the old
                # independent/parallel path below.
                log.info("_polish_reduce: HEAVY chunk seam repair uses %d serial call(s); "
                         "parallel=%d ignored for dependency ordering (latency may increase)",
                         len(chunks), _pp)
                for i, ch in enumerate(chunks):
                    previous_tail = _bounded_polish_tail(polished[-1]) if polished else ""
                    chunk_instruction = (
                        c_instr + _HEAVY_INCOMING_SEAM_INSTRUCTION
                        if previous_tail else c_instr
                    )
                    _o, _ok = await _polish_one(
                        ch, instruction=chunk_instruction, role=c_role, model=manager_model,
                        timeout=timeout, telemetry_sink=telemetry_sink,
                        task_id=f"polish:{mode}:chunk{i + 1}/{len(chunks)}",
                        authority_text=authority_text,
                        previous_chapter_tail=previous_tail,
                        guard_heavy_chunk=True,
                        min_retained_percent=min_retained_percent,
                    )
                    polished.append(_o)
                    any_ok = any_ok or _ok
            elif _pp >= 2:
                _psem = asyncio.Semaphore(_pp)

                async def _polish_chunk(_i, _ch):
                    async with _psem:
                        return await _polish_one(_ch, instruction=c_instr, role=c_role, model=manager_model,
                                                 timeout=timeout, telemetry_sink=telemetry_sink,
                                                 task_id=f"polish:{mode}:chunk{_i + 1}/{len(chunks)}",
                                                 authority_text=authority_text,
                                                 min_retained_percent=min_retained_percent)

                for _o, _ok in await asyncio.gather(*[_polish_chunk(i, ch) for i, ch in enumerate(chunks)]):
                    polished.append(_o)
                    any_ok = any_ok or _ok
            else:
                for i, ch in enumerate(chunks):
                    _o, _ok = await _polish_one(ch, instruction=c_instr, role=c_role, model=manager_model,
                                                timeout=timeout, telemetry_sink=telemetry_sink,
                                                task_id=f"polish:{mode}:chunk{i + 1}/{len(chunks)}",
                                                authority_text=authority_text,
                                                min_retained_percent=min_retained_percent)
                    polished.append(_o)
                    any_ok = any_ok or _ok
            rejoined = "\n\n".join(polished)
            # Align with the per-chunk retention/135% guards in _polish_one. The upper bound
            # is defense-in-depth (round-16 postmortem): each chunk is already capped
            # individually, so this should be structurally unreachable, but mirrors the same
            # symmetry in case a future change bypasses _polish_one's own check.
            _rejoined_w = len(rejoined.split())
            _minimum_rejoined_w = _polish_retention_floor(
                _book_words, min_retained_percent
            )
            if _rejoined_w < _minimum_rejoined_w:   # lost too much → keep original
                log.warning("_polish_reduce: chunked polish retained < %d%% words — "
                            "discarding, keeping original", min_retained_percent)
                return book, False
            if _rejoined_w > int(_book_words * 1.35):   # gained too much → keep original
                log.warning("_polish_reduce: chunked polish rejoin %d words > 135%% of %d — "
                            "discarding, keeping original (bloat/duplication guard)",
                            _rejoined_w, _book_words)
                return book, False
            log.info("_polish_reduce: chunked polish done — %d chunks (<=%d words each), applied=%s",
                     len(chunks), chunk_words, any_ok)
            return rejoined, any_ok

    # Chunking disabled or unsplittable (one giant chapter) → promote-or-skip fallback.
    _big = (os.environ.get("NARASI_POLISH_BIG_MODEL") or "").strip()
    _big_ceil = int(max_tokens_for(_big) or 0) if _big else 0
    if _big and _big_ceil and _need <= int(_big_ceil * 0.95):
        log.info("_polish_reduce: promoting to %s (ceiling %d) instead of skipping", _big, _big_ceil)
        return await _polish_one(book, instruction=instruction, role=role, model=_big,
                                 timeout=timeout, telemetry_sink=telemetry_sink,
                                 task_id=f"polish:{mode}", authority_text=authority_text,
                                 min_retained_percent=min_retained_percent)
    log.info("_polish_reduce: skipping polish — book needs ~%d tokens but %s ceiling is %d "
             "(no chunking, no big-model)", _need, manager_model, _ceil)
    return book, False


__all__ = [
    "cowork",
    "narrate_chapters",
    "DEFAULT_COWORK_ROLES",
    "_polish_reduce",
]
