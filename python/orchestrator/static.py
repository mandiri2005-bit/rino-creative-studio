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
import logging
import os
import re
import time
from typing import Any, Optional, Sequence

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

    Also runs a report-only CEILING check on the final word count (independent of the
    NARASI_WORDGATE on/off flag — this is pure defect-visibility, not a new gate): if the
    chapter is 30%+ OVER word_target, logs a warning. Never mutates output for this."""
    if _wordgate_on() and res.get("ok") and res.get("output"):
        floor = int(int(word_target) * 0.9)
        text = str(res["output"])
        rounds = 0
        last = res  # the result whose truncation flag we track as the tail grows
        # Continue while the chapter is UNDER the floor OR the last call was truncated by the
        # model's output ceiling (a truncated chapter above the floor would otherwise ship
        # mid-sentence). Bounded by _WORDGATE_RETRIES either way.
        while (len(text.split()) < floor or _res_truncated(last)) and rounds < _WORDGATE_RETRIES:
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
        if rounds:
            log.info("%s: word-gate continued %d round(s) → %d words (target %d, truncated_tail=%s)",
                     task_id, rounds, len(text.split()), word_target, _res_truncated(last))
            res = {**res, "output": text, "continued": rounds}

    try:
        if res.get("ok") and res.get("output"):
            _final_wc = len(str(res["output"]).split())
            _ceiling = int(int(word_target) * _WORDGATE_CEILING_FACTOR)
            if word_target and _final_wc > _ceiling:
                log.warning(
                    "%s: chapter word count %d exceeds ceiling %d (target %d words, +%.0f%% over target)",
                    task_id, _final_wc, _ceiling, word_target,
                    ((_final_wc / word_target) - 1.0) * 100.0)
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
) -> dict[str, Any]:
    """Generate ONE chapter via the required pipeline:

        build_shared_context (already done once, passed in as ctx)
          -> compose()      (cache-stable prefix + this chapter's user turn)
          -> run_worker()   (never-raise)

    Returns a dict tagged with `no` so the MAP can be sorted back into book order.
    """
    word_target = int(ch.get("word_target", ch.get("words", 800)) or 800)
    # Firm ceiling companion to word_target — stated in the prompt itself so the model
    # treats the target as a ceiling too, not just a floor (2026-07-15: a book shipped
    # at 53,655 words against a ~40,000 target). Deliberately tighter than the separate
    # _WORDGATE_CEILING_FACTOR alarm threshold used below — see that constant's comment.
    word_max = int(word_target * _WORDGATE_PROMPT_CEILING_FACTOR)
    # CC v3: scale the per-chapter timeout to the target so big chapters (≤8k words)
    # aren't killed by the flat default while small ones keep the tight bound.
    timeout = _scaled_timeout(timeout, word_target)

    if not _COMPOSE_OK or compose is None:
        # Assembler unavailable: degrade to a minimal direct prompt so the
        # strategy still functions (never crash the job).
        prompt = (
            f"{ctx.brief_block()}\n\n{ctx.scope_for(no)}\n\n"
            f"Write chapter {no + 1} of {total}: \"{ch.get('title','')}\". "
            f"Target ~{word_target} words. Do not exceed {word_max} words. "
            "Return ONLY the chapter body."
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
        res["no"] = no
        return res

    # The whole point of WS-4/WS-5: outline + facts + style ride the CACHED prefix;
    # the per-chapter scope + RAG passages land in the variable user turn.
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
        prev_tail=ctx.scope_for(no),      # anti-collision scope (per chapter, NOT cached)
        rag_passages=ctx.passages,        # retrieved ONCE in build_shared_context, reused
        job_id=job_id,
        model=worker_model,
    )

    worker = Worker(
        name=f"ch{no + 1}",
        role="worker",
        phase="worker",
        model=worker_model,
        style=style,
        system=composed.messages[0]["content"],
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
    res["no"] = no
    res["cache_key"] = composed.cache_key
    return res


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
                        manager_model=m_model, telemetry_sink=telemetry_sink)
                      for _ in range(_bo_n)],
                    return_exceptions=True)
                _cands = [c for c in _cands_raw if isinstance(c, str) and c.strip()]
                _bible = _cands[0] if _cands else ""
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
                        log.info("bible best-of-%d: %d candidate(s), winner #%d — %s",
                                 _bo_n, len(_cands), _w, str((_bj or {}).get("reason") or "")[:120])
                    except Exception as _bje:  # noqa: BLE001 — judge is an enhancement
                        log.warning("bible best-of judge failed (non-fatal, candidate #1 kept): %s", _bje)
            else:
                _bible = await build_story_bible(
                    topic, list(ctx.chapters or chapters), is_fiction=_fic,
                    style=style, language=language,
                    manager_model=m_model, telemetry_sink=telemetry_sink,
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
                                    _terms_all = sorted({str(h.get("term", "")).split(":", 1)[-1]
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
                                    from narasi_counters import premise_term_in_topic as _ptt
                                    _terms = [t for t in _terms_all
                                              if t and not _ptt(t, topic or "")][:12]
                                    if not _terms:
                                        log.info("ledger-enforce: all %d bible hit(s) premise-supplied (%s) — re-roll skipped",
                                                 len(_terms_all), ", ".join(_terms_all[:6]))
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
                                        _bible2 = await build_story_bible(
                                            topic, list(ctx.chapters or chapters), is_fiction=_fic,
                                            style=style, language=language,
                                            manager_model=m_model, telemetry_sink=telemetry_sink,
                                            extra_negative=", ".join(_terms))
                                        _rep2 = (_lnc.ledger_hits_scan("", bible=_bible2, style_key=_lrsk(style))
                                                 if _bible2 else {})
                                        if _bible2 and int(_rep2.get("bible_hits") or 0) < int(_lrep.get("bible_hits") or 0):
                                            ctx.canonical_facts = _bible2
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
        except Exception as _re:  # noqa: BLE001
            log.warning("resume preload failed (non-fatal): %s", _re)
            _ckpt_uuid, _pre = None, {}

    async def _bounded(no: int, ch: dict) -> dict[str, Any]:
        if no in _pre:   # S2: checkpointed on a previous attempt — reuse, zero spend
            # Resumed content bypasses _write_chapter entirely, so it must get the same
            # leak scrub freshly-generated chapters get below — otherwise a checkpoint
            # captured before the scrub existed (or containing a leak some other way)
            # would ship un-scrubbed forever.
            _resumed_output = _scrub_chapter_leaks(_pre[no], task_id=f"ch{no + 1}")
            return {"ok": True, "output": _resumed_output, "no": no, "model": w_model, "resumed": True}
        async with sem:
            res = await _write_chapter(
                ctx=ctx, ch=ch, no=no, total=total,
                style=style, language=language, mode=mode, job_id=job_id,
                worker_model=w_model, timeout=worker_timeout,
                telemetry_sink=telemetry_sink,
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
    tasks = [asyncio.ensure_future(_bounded(i, ch)) for i, ch in enumerate(chapters)]

    # Consume as_completed (so a slow chapter doesn't block logging of fast ones),
    # then SORT BY CHAPTER NUMBER to restore book order — the map-reduce invariant.
    raw: list[dict[str, Any]] = []
    for fut in asyncio.as_completed(tasks):
        try:
            raw.append(await fut)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - _write_chapter is never-raise, but belt+braces
            log.warning("narrate_chapters: a chapter task raised unexpectedly: %s", exc)
            raw.append({"ok": False, "output": None, "error": str(exc), "no": -1})
    log.info("narrate_chapters: MAP done — %d chapters in %.1fs (max_parallel=%s)",
             len(tasks), time.monotonic() - _t_map, max_parallel)

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
    if str(os.environ.get("NARASI_CHAPTER_BOUNDARY_CHECK", "0")).strip().lower() in ("1", "true", "yes", "on"):
        try:
            if total >= 2:
                from laozhang_api import _narasi_cheap_call as _bc_call, _narasi_parse_json as _bc_parse
                _bc_sys = (
                    "You are checking two adjacent chapters of a narrative for a continuity "
                    "break at the seam between them. You are given the ENDING of chapter N and "
                    "the OPENING of chapter N+1. Question: does the OPENING of chapter N+1 "
                    "CONTRADICT or REDUNDANTLY RE-STAGE something the ENDING of chapter N already "
                    "resolved — e.g. re-introducing an event, journey, decision or arrival that "
                    "already happened, with different details (different character, different "
                    "means, different time of day)? Return ONLY JSON: {\"broken\": true|false, "
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
                        log.warning(
                            "chapter-boundary check: ch %d -> ch %d looks like a continuity "
                            "break (%s)", int(_bc_a.get("no", 0)) + 1, int(_bc_b.get("no", 0)) + 1,
                            str(_bc_d.get("reason") or "")[:200])
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
        # In-memory transit only (NOT persisted into the bounded _result_payload): the pinned
        # story bible, so the downstream consistency critic can diff each chapter against the
        # committed canon when NARASI_CANON_CONFORMANCE is on. as_dict() exposes only the char
        # count, so the raw text otherwise dies here.
        "canonical_facts": ctx.canonical_facts,
        "facts_are_bible": ctx.facts_are_bible,
    }


# ===========================================================================
# Polish reducer — the 3-mode REDUCE (none / light / heavy).
# Shared by narrate_chapters() and the router. A standalone helper so the router
# can apply the same semantics to any strategy's output.
# ===========================================================================
def _polish_instruction(mode: str, topic: str, language: str, *, is_chunk: bool = False):
    """Build (instruction, synthesize-role) for a polish pass. is_chunk swaps 'book'→'section'
    so a chunk pass does not think it is the whole book."""
    unit = "section of a multi-chapter narrative" if is_chunk else "multi-chapter narrative"
    ret = "edited section" if is_chunk else "edited book"
    if mode == "heavy":
        instruction = (
            f"You are the editor-in-chief doing a HEAVY final edit of a {unit} about \"{topic}\". "
            "Reconcile any contradictions, remove cross-chapter repetition and re-introductions, "
            "tighten flabby passages, and hold ONE consistent voice and tense throughout. PRESERVE "
            "every concrete fact, name, date, number and quote exactly. Keep every chapter-heading "
            "line (each begins with `## `) exactly as given — do not remove, rename, renumber, "
            "translate or move them, and never write a new heading of your own. Make the narration "
            f"read as ONE seamless, continuous flow. Return ONLY the {ret} in {language}, no notes.")
        return instruction, "synthesize"
    instruction = (
        f"You are the editor-in-chief doing a LIGHT final pass of a {unit} about \"{topic}\". "
        "ONLY smooth the seams between chapters, remove obvious cross-chapter repetition, and keep "
        "the register consistent. Do NOT rewrite content, do NOT change any fact, name, date or "
        "number, do NOT shorten the text. Keep every chapter-heading line (each begins with `## `) "
        "exactly as given — do not remove, rename, renumber, translate or move them, and never "
        f"write a new heading of your own. Return ONLY the lightly-edited {ret} in {language}.")
    return instruction, "polish"


_POLISH_CHAPTER_SPLIT_RX = re.compile(r"(?m)(?=^## )")
# AUDIT FIX (r16): the bare `(\d+)` capture collapsed "Chapter 3a"/"Chapter 3.5" into the
# SAME key as plain "Chapter 3" — a genuinely distinct chapter would then be silently DELETED
# as a false "duplicate". The negative lookahead rejects a match immediately followed by a
# lowercase letter or a decimal point, so "3a"/"3.5" no longer match at all (nums[i]=None,
# which the "fails safe" branch below always KEEPS) while plain "Chapter 3:"/"Chapter 3 —"
# still match normally.
_CH_HEADER_NUM_RX = re.compile(r"^##\s+\D*?(\d+)(?![a-z.])")


def _dedup_chapter_blocks(book: str) -> tuple[str, int]:
    """Defense-in-depth safety net (narasi round-16 postmortem, job nny3va3j): scan the final
    assembled/polished book for repeated '## <label> N' chapter headings and keep only the LAST
    occurrence of each chapter number, dropping earlier duplicate blocks — regardless of root
    cause (a bloated polish-chunk echo — see the upper-bound guard in _polish_one/_polish_reduce
    — a stale resume, or anything else). Splits on the same '^## ' boundary _split_into_chunks
    already trusts, so behavior stays consistent with the chunker. Preserves original relative
    order (a pure filter, never a re-sort), so a correctly-ordered book is byte-identical.
    _CH_HEADER_NUM_RX matches a leading non-digit run then a digit run, which covers every
    language template in laozhang_api._NARASI_HEADER_LABELS ("Chapter {n}", "Bab {n}", "第{n}章",
    "{n}장", ...) since they all interpolate a plain digit run into the heading; a part with no
    matching digit (None) is always kept (fails safe — never mistaken for a duplicate). Never
    raises; returns (book, 0) when every heading number is already unique or none are found."""
    try:
        parts = [p for p in _POLISH_CHAPTER_SPLIT_RX.split(book) if p.strip()]
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
    ONLY at '## ' chapter headings (never mid-chapter); a single chapter larger than chunk_words
    becomes its own oversized chunk. Returns [book] when there is nothing to split."""
    parts = [p for p in _POLISH_CHAPTER_SPLIT_RX.split(book) if p.strip()]
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


async def _polish_one(text, *, instruction, role, model, timeout, telemetry_sink, task_id):
    """Polish ONE blob (whole book or a chunk) via synthesize. Returns (out, ok). Post-
    truncation guard (>=75% words) keeps the original on a cut/degraded pass. Post-bloat guard
    (<=135% words) does the SAME for the opposite failure: neither a "light" (preserve length)
    nor a "heavy" (same-length reconciling edit) polish instruction should legitimately double a
    chunk's length — a materially LONGER output is exactly as anomalous as a shorter one and was
    previously accepted unconditionally. narasi round-16 postmortem (job nny3va3j): a long-input
    polish chunk degenerated on a long-context failure mode (the instruction requires preserving
    every "## Chapter N" heading exactly, and the model echoed/re-derived the section instead of
    only editing it), producing one completion with each "## Chapter N" heading TWICE — silently
    accepted, landing as a 14K-word divergent duplicate of Ch1-4 prepended to the real book.
    Never raises."""
    wrapped = [{"ok": True, "output": text, "model": model}]
    _t = time.monotonic()
    res = await synthesize(instruction, wrapped, role=role, model=model, timeout=timeout,
                           telemetry_sink=telemetry_sink, task_id=task_id)
    _tel = res.get("telemetry") or {}
    log.info("_polish_reduce: %s role=%s model=%s served_by=%s ok=%s in %.1fs (tok_in=%s tok_out=%s)",
             task_id, role, model, _tel.get("provider") or "?", res.get("ok"),
             time.monotonic() - _t, _tel.get("tokens_in"), _tel.get("tokens_out"))
    if res.get("ok") and res.get("output"):
        out = str(res["output"])
        _in_w, _out_w = len(text.split()), len(out.split())
        if _out_w < int(_in_w * 0.75):
            log.warning("_polish_reduce: %s output %d words < 75%% of %d — discarding (truncation guard)",
                        task_id, _out_w, _in_w)
            return text, False
        if _out_w > int(_in_w * 1.35):
            log.warning("_polish_reduce: %s output %d words > 135%% of %d — discarding (bloat/duplication guard)",
                        task_id, _out_w, _in_w)
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
):
    """3-mode polish reducer (none/light/heavy). When the assembled book exceeds the polish
    model's single-call OUTPUT ceiling, CHUNK it — split by chapter into <=NARASI_POLISH_CHUNK_WORDS
    groups, polish each, rejoin — instead of skipping, so a long book (up to NARASI_POLISH_MAX_WORDS,
    default the 40k generation cap) still gets a full pass. Cross-chunk boundaries are always
    chapter breaks. Each chunk has its own >=75%-word truncation guard. Never raises."""
    mode = (polish or "light").strip().lower()
    if mode == "none" or not book.strip():
        return book, False
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
                                 timeout=timeout, telemetry_sink=telemetry_sink, task_id=f"polish:{mode}")

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
            # ── PARALLEL polish (NARASI_POLISH_PARALLEL>=2; default 0 = the serial loop below runs
            #    verbatim / byte-identical). Chunks are INDEPENDENT — each polishes its own chapter group,
            #    rejoined IN ORDER — so run them concurrently: N serial chunks (job rsmyws7s = 752s, of
            #    which 2 flaky chunks wasted ~458s) collapse to ~one wave (the slowest chunk). asyncio.gather
            #    preserves input order → identical rejoin. _polish_one NEVER raises (keeps its original
            #    chunk on truncation/timeout), so a bad chunk cannot abort the gather — its waste just
            #    OVERLAPS the good chunks instead of adding serially. ──
            try:
                _pp = int(str(os.environ.get("NARASI_POLISH_PARALLEL", "0")).strip() or "0")
            except Exception:
                _pp = 0
            if _pp >= 2:
                _psem = asyncio.Semaphore(_pp)

                async def _polish_chunk(_i, _ch):
                    async with _psem:
                        return await _polish_one(_ch, instruction=c_instr, role=c_role, model=manager_model,
                                                 timeout=timeout, telemetry_sink=telemetry_sink,
                                                 task_id=f"polish:{mode}:chunk{_i + 1}/{len(chunks)}")

                for _o, _ok in await asyncio.gather(*[_polish_chunk(i, ch) for i, ch in enumerate(chunks)]):
                    polished.append(_o)
                    any_ok = any_ok or _ok
            else:
                for i, ch in enumerate(chunks):
                    _o, _ok = await _polish_one(ch, instruction=c_instr, role=c_role, model=manager_model,
                                                timeout=timeout, telemetry_sink=telemetry_sink,
                                                task_id=f"polish:{mode}:chunk{i + 1}/{len(chunks)}")
                    polished.append(_o)
                    any_ok = any_ok or _ok
            rejoined = "\n\n".join(polished)
            # Align with the per-chunk 75%/135% guards in _polish_one: heavy mode legitimately
            # compresses, so a rejoin in [75%,85%) is real editing, not truncation — an 85%
            # floor would nuke a valid heavy polish that every chunk already accepted. The
            # upper bound is defense-in-depth (round-16 postmortem): each chunk is already
            # capped individually, so this should be structurally unreachable, but mirrors the
            # same symmetry in case a future change bypasses _polish_one's own check.
            _rejoined_w = len(rejoined.split())
            if _rejoined_w < int(_book_words * 0.75):   # lost too much → keep original
                log.warning("_polish_reduce: chunked polish lost >25%% words — discarding, keeping original")
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
                                 timeout=timeout, telemetry_sink=telemetry_sink, task_id=f"polish:{mode}")
    log.info("_polish_reduce: skipping polish — book needs ~%d tokens but %s ceiling is %d "
             "(no chunking, no big-model)", _need, manager_model, _ceil)
    return book, False


__all__ = [
    "cowork",
    "narrate_chapters",
    "DEFAULT_COWORK_ROLES",
    "_polish_reduce",
]
