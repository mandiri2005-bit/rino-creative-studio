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
from typing import Any, Optional, Sequence

from .core import (
    Worker,
    run_worker,
    synthesize,
    route_model,
    WORKER_MODEL,
    MANAGER_MODEL,
    MAX_WORKERS,
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


def _gate(text: str) -> str:
    """Per-chapter deterministic gate (identity when narasi_gate absent/disabled)."""
    if _ngate is None or not text:
        return text
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
# Truncation (finish_reason=length) needs no separate check here: it only bites above
# ~22k words/chapter (32k-token output ceiling), unreachable under the 8k admission cap —
# any truncated chapter is therefore also under the word floor and gets continued anyway.
# Kill switch: NARASI_WORDGATE=0 (default ON).
_WORDGATE_RETRIES = int(os.environ.get("DALANG_MAX_CHAPTER_RETRIES", "2"))


def _wordgate_on() -> bool:
    return str(os.environ.get("NARASI_WORDGATE", "1")).strip().lower() not in ("0", "false", "no", "off")


def _scaled_timeout(base: float, word_target: int) -> float:
    """Per-chapter LLM timeout scaled to the word target so a big chapter (≤8k words
    admission cap) is not killed by the flat 120s default: est = words×1.45 tok/word ÷
    40 tok/s ×1.3 buffer (8k words → ~380s). Never below `base`; capped by
    NARASI_WORKER_TIMEOUT_MAX (default 900s, matching the classic chapter timeout)."""
    try:
        cap = float(os.environ.get("NARASI_WORKER_TIMEOUT_MAX", "900"))
        est = int(word_target) * 1.45 / 40.0 * 1.3
        return max(float(base), min(cap, est))
    except Exception:  # noqa: BLE001
        return float(base)


async def _apply_word_gate(res: dict, *, worker: Any, word_target: int,
                           timeout: float, task_id: str) -> dict:
    """Continue an undershooting chapter (< 0.9×target) up to _WORDGATE_RETRIES rounds.
    The continuation reuses the SAME worker (same cached system prefix: style rules +
    coherence contract) with the full model output ceiling, so a follow-up round can
    never itself be length-starved for targets under the admission cap."""
    if not _wordgate_on() or not res.get("ok") or not res.get("output"):
        return res
    floor = int(int(word_target) * 0.9)
    text = str(res["output"])
    rounds = 0
    while len(text.split()) < floor and rounds < _WORDGATE_RETRIES:
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
    if rounds:
        log.info("%s: word-gate continued %d round(s) → %d words (target %d)",
                 task_id, rounds, len(text.split()), word_target)
        res = {**res, "output": text, "continued": rounds}
    return res


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
    # CC v3: scale the per-chapter timeout to the target so big chapters (≤8k words)
    # aren't killed by the flat default while small ones keep the tight bound.
    timeout = _scaled_timeout(timeout, word_target)

    if not _COMPOSE_OK or compose is None:
        # Assembler unavailable: degrade to a minimal direct prompt so the
        # strategy still functions (never crash the job).
        prompt = (
            f"{ctx.brief_block()}\n\n{ctx.scope_for(no)}\n\n"
            f"Write chapter {no + 1} of {total}: \"{ch.get('title','')}\". "
            f"Target ~{word_target} words. Return ONLY the chapter body."
        )
        worker = Worker(
            name=f"ch{no + 1}", role="worker", model=worker_model,
            style=style, telemetry_sink=telemetry_sink,
        )
        res = await run_worker(worker, prompt, timeout=timeout, task_id=f"ch{no + 1}")
        res = await _apply_word_gate(res, worker=worker, word_target=word_target,
                                     timeout=timeout, task_id=f"ch{no + 1}")
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
        },
        prev_tail=ctx.scope_for(no),      # anti-collision scope (per chapter, NOT cached)
        rag_passages=ctx.passages,        # retrieved ONCE in build_shared_context, reused
        job_id=job_id,
        model=worker_model,
    )

    worker = Worker(
        name=f"ch{no + 1}",
        role="worker",
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
    res["no"] = no
    res["cache_key"] = composed.cache_key
    return res


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

    # 1) ONE shared context for the whole job (one RAG retrieval, reused).
    ctx = shared_context or await build_shared_context(
        topic, chapters, tenant_id, style=style,
    )

    # 2) MAP — bounded parallel fan-out. Semaphore caps concurrency at max_parallel
    #    so a 40-chapter book doesn't open 40 sockets at once.
    sem = asyncio.Semaphore(max(1, int(max_parallel or 1)))

    async def _bounded(no: int, ch: dict) -> dict[str, Any]:
        async with sem:
            return await _write_chapter(
                ctx=ctx, ch=ch, no=no, total=total,
                style=style, language=language, mode=mode, job_id=job_id,
                worker_model=w_model, timeout=worker_timeout,
                telemetry_sink=telemetry_sink,
            )

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

    raw.sort(key=lambda r: r.get("no", 0))

    chapter_records: list[dict[str, Any]] = []
    for i, r in enumerate(raw):
        no = r.get("no", i)
        ch = chapters[no] if 0 <= no < total else {}
        ok = bool(r.get("ok")) and bool(r.get("output"))
        # R-FG4/5/6 per-chapter gate on real output only — the failure placeholder IS a
        # deliberate bracket signal and must survive (the gate whitelists it anyway).
        content = _gate(r.get("output")) if ok else _placeholder(ch, no, str(r.get("error", "unknown")))
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
    # Assemble WITH a per-chapter heading so the result is classified per-bab while
    # the manager polish keeps the prose flowing. Heading uses the outline title
    # (1-based by position); chapters with no title get a bare "## Bab N".
    def _chapter_md(c: dict[str, Any]) -> str:
        title = (c.get("title") or "").strip()
        head = f"## Bab {int(c.get('no', 0)) + 1}" + (f": {title}" if title else "")
        return f"{head}\n\n{(c.get('content') or '').strip()}"
    book = "\n\n".join(_chapter_md(c) for c in chapter_records)

    # 3) REDUCE — optional manager polish over the assembled book.
    polished_book, did_polish = await _polish_reduce(
        book=book,
        topic=topic,
        style=style,
        language=language,
        polish=polish,
        manager_model=m_model,
        timeout=manager_timeout,
        telemetry_sink=telemetry_sink,
        any_failures=(n_ok < total),
    )
    # R-FG5 TERMINAL: the polish is the last LLM touch and can reintroduce brackets —
    # the deterministic gate must run AFTER it, so residue can never ship.
    polished_book = _gate(polished_book)

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
    }


# ===========================================================================
# Polish reducer — the 3-mode REDUCE (none / light / heavy).
# Shared by narrate_chapters() and the router. A standalone helper so the router
# can apply the same semantics to any strategy's output.
# ===========================================================================
async def _polish_reduce(
    *,
    book: str,
    topic: str,
    style: Optional[str],
    language: str,
    polish: str,
    manager_model: str,
    timeout: float,
    telemetry_sink: Optional[Any],
    any_failures: bool = False,
) -> tuple[str, bool]:
    """Apply the 3-mode polish reducer to an assembled book.

    Modes:
      * "none"  — return the book unchanged (no manager spend).
      * "light" — DEFAULT. One cheap-but-careful pass: smooth seams between
        chapters, kill cross-chapter repetition, lock the register — WITHOUT
        rewriting content or touching facts. Uses synthesize(role="polish").
      * "heavy" — a stronger editorial pass (synthesize role="merge" semantics:
        reconcile contradictions, remove duplication, tighten throughout) while
        still preserving every concrete fact/name/date/number.

    If any chapter failed, we SKIP polishing (placeholders would confuse the
    editor and waste spend) and return the book with its visible [CHAPTER … FAILED]
    markers intact. Never raises.
    """
    mode = (polish or "light").strip().lower()
    if mode == "none" or not book.strip():
        return book, False
    if any_failures:
        log.info("_polish_reduce: skipping polish — book has failed-chapter placeholders")
        return book, False

    # NARASI_POLISH_MODEL (env, python svc): run the polish/merge pass on a cheaper
    # editor-tier model than the worker (polish is editing, not writing — it doesn't
    # need the full worker model). Unset = today's behavior (manager_model = worker).
    # Restores the cost lever the removed Model-Manager picker used to provide,
    # server-side per the server-driven-model direction (Rino 2026-07-04).
    _env_polish = (os.environ.get("NARASI_POLISH_MODEL") or "").strip()
    if _env_polish:
        manager_model = _env_polish

    # CC v3 anti-truncate guard (pre): the polish returns the WHOLE book in one call, so a
    # book beyond the manager's output ceiling (~32k tokens ≈ 22k words) would come back
    # silently truncated and REPLACE the full text. Skip polish for big books instead.
    _pmax = int(os.environ.get("NARASI_POLISH_MAX_WORDS", "15000"))
    _book_words = len(book.split())
    if _book_words > _pmax:
        log.info("_polish_reduce: skipping polish — book %d words > NARASI_POLISH_MAX_WORDS %d "
                 "(output-ceiling truncation risk)", _book_words, _pmax)
        return book, False

    if mode == "heavy":
        instruction = (
            "You are the editor-in-chief doing a HEAVY final edit of a multi-chapter "
            f"narrative about \"{topic}\". Reconcile any contradictions, remove "
            "cross-chapter repetition and re-introductions, tighten flabby passages, "
            "and hold ONE consistent voice and tense for the whole book. PRESERVE "
            "every concrete fact, name, date, number and quote exactly. Keep every "
            "`## Bab N: ...` heading line exactly as given — do not remove, rename, "
            "renumber or move them. Between and within those sections, make the "
            "narration read as ONE seamless, continuous flow: smooth every transition "
            "so nothing reads as an abrupt break. Return ONLY the "
            f"edited book in {language}, no notes or preamble."
        )
        role = "merge"
    else:  # "light" (default) and any unknown value
        instruction = (
            "You are the editor-in-chief doing a LIGHT final pass of a multi-chapter "
            f"narrative about \"{topic}\". ONLY smooth the seams between chapters, "
            "remove obvious cross-chapter repetition, and keep the register "
            "consistent. Do NOT rewrite content, do NOT change any fact, name, date "
            "or number, do NOT shorten the book. Keep every `## Bab N: ...` heading "
            "line exactly as given (do not remove, rename, renumber or move them). "
            "Make the narration flow as ONE seamless, uninterrupted piece — smooth "
            "the transitions between sections so nothing reads as an abrupt break. "
            f"Return ONLY the lightly-edited book in {language}."
        )
        role = "polish"

    # synthesize() expects a list of worker-result dicts; wrap the whole book as one.
    wrapped = [{"ok": True, "output": book, "model": manager_model}]
    res = await synthesize(
        instruction,
        wrapped,
        role=role,
        model=manager_model,
        timeout=timeout,
        telemetry_sink=telemetry_sink,
        task_id=f"polish:{mode}",
    )
    if res.get("ok") and res.get("output"):
        # CC v3 anti-truncate guard (post): a truncated polish still returns ok=True. If the
        # "polished" book lost >25% of its words, it was cut by the output ceiling (or the
        # editor over-deleted) — discard it and keep the full original.
        _out = str(res["output"])
        if len(_out.split()) < int(_book_words * 0.75):
            log.warning("_polish_reduce: polished output %d words < 75%% of book %d — "
                        "discarding polish (truncation guard)", len(_out.split()), _book_words)
            return book, False
        return _out, True
    # Polish failed — return the unpolished book rather than nothing.
    log.warning("_polish_reduce: polish pass failed (%s) — returning unpolished book",
                res.get("error"))
    return book, False


__all__ = [
    "cowork",
    "narrate_chapters",
    "DEFAULT_COWORK_ROLES",
    "_polish_reduce",
]
