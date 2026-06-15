# -*- coding: utf-8 -*-
"""
Project Dalang — orchestrator.router (WS-6, the dispatcher / "dalang").

This is the single front door of the orchestration engine. `generate_narration(req)`
inspects the SHAPE of the request and dispatches to the right strategy in
`orchestrator.static` / `orchestrator.dynamic`, then applies a 3-mode polish reduce.
Every chapter worker, regardless of scenario, flows through the WS-6 pipeline:

        build_shared_context  ->  compose()  ->  run_worker

(That pipeline lives inside `static.narrate_chapters` / `static._write_chapter`;
the router just chooses WHICH strategy feeds it.)

===========================================================================
THE A–E DECISION TABLE  (request shape  ->  strategy)
===========================================================================
The router classifies a request by what it CONTAINS, in priority order A..E:

  ┌────┬───────────────────────────────────┬──────────────────────────────────────────────┐
  │ Sc │ Request shape (what's present)    │ Strategy                                       │
  ├────┼───────────────────────────────────┼──────────────────────────────────────────────┤
  │ A  │ topic + explicit chapter list     │ STATIC chapter map-reduce directly.            │
  │    │ (titles/outline already given)    │ static.narrate_chapters(topic, chapters).      │
  │    │                                   │ -> the common "user already has an outline".   │
  ├────┼───────────────────────────────────┼──────────────────────────────────────────────┤
  │ B  │ topic ONLY (no titles), wants a   │ DYNAMIC outline THEN static map.               │
  │    │ multi-chapter book/script         │ dynamic.outline_from_topic -> narrate_chapters.│
  ├────┼───────────────────────────────────┼──────────────────────────────────────────────┤
  │ C  │ a single brief to render (one     │ STATIC roles fan-out + synthesize.             │
  │    │ piece, not a chaptered book)      │ static.cowork(brief)  (researcher/dramatist/   │
  │    │                                   │ stylist/skeptic -> manager synthesize).        │
  ├────┼───────────────────────────────────┼──────────────────────────────────────────────┤
  │ D  │ open-ended goal (no titles, no    │ FULLY DYNAMIC plan.                            │
  │    │ brief, no obvious chapter intent) │ dynamic.plan_subtasks -> parallel workers ->   │
  │    │                                   │ synthesize. The decomposition is invented.     │
  ├────┼───────────────────────────────────┼──────────────────────────────────────────────┤
  │ E  │ a single SHORT ask (tiny word     │ ONE call, NO orchestrator.                     │
  │    │ target / explicit single=true)    │ a single run_worker — no fan-out, no manager.  │
  └────┴───────────────────────────────────┴──────────────────────────────────────────────┘

Classification priority: E (single/short) is checked FIRST as an escape hatch (no
point orchestrating a one-paragraph ask); then A (explicit chapters) > B (topic +
multi-chapter intent) > C (brief) > D (everything else, open-ended).

===========================================================================
ENV OVERRIDES (read at call time so deployments can re-route live)
===========================================================================
  * ORCH_MODE = auto | static | dynamic   (default auto = use the A–E table)
        - "static":  force the static lane — a topic-only request gets a STATIC
          fallback outline (no manager outline call); never uses plan_subtasks.
        - "dynamic": force the dynamic lane — even an explicit-chapters request is
          treated as a goal and decomposed via plan_subtasks.
  * POLISH   = none | light | heavy        (default light) — the REDUCE mode.
  * MAX_WORKERS = <int>                     (default from core.MAX_WORKERS = 6)
  * RAG      = on | off                     (default on) — off skips RAG retrieval
        in build_shared_context (pass empty chapters' shared context with no passages).

A per-request `req` field of the same (lowercased) name overrides the env, which
overrides the default — so callers can pin behaviour without touching the env.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from .core import MAX_WORKERS as _CORE_MAX_WORKERS, MANAGER_MODEL, route_model, Worker, run_worker
from . import static as _static
from . import dynamic as _dynamic
from .context_builder import build_shared_context

log = logging.getLogger("orchestrator.router")


# ===========================================================================
# Settings resolution — req field > env var > default. Read at call time.
# ===========================================================================
def _resolve_setting(req: dict, key: str, env: str, default: str,
                     allowed: Optional[set[str]] = None) -> str:
    """Resolve a string setting: req[key] (lowercased) > os.environ[env] > default.
    If `allowed` is given, an out-of-range value falls back to `default`."""
    val = req.get(key)
    if val is None or str(val).strip() == "":
        val = os.environ.get(env)
    if val is None or str(val).strip() == "":
        val = default
    val = str(val).strip().lower()
    if allowed is not None and val not in allowed:
        log.info("router: %s=%r not in %s — using default %r", key, val, allowed, default)
        return default
    return val


def _resolve_max_workers(req: dict) -> int:
    raw = req.get("max_workers") or os.environ.get("MAX_WORKERS")
    if raw is None or str(raw).strip() == "":
        return _CORE_MAX_WORKERS
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return _CORE_MAX_WORKERS


class _Settings:
    """Resolved, validated per-request settings (one place so every scenario agrees)."""
    __slots__ = ("orch_mode", "polish", "max_workers", "rag_on")

    def __init__(self, req: dict):
        self.orch_mode = _resolve_setting(
            req, "orch_mode", "ORCH_MODE", "auto", {"auto", "static", "dynamic"})
        self.polish = _resolve_setting(
            req, "polish", "POLISH", "light", {"none", "light", "heavy"})
        self.max_workers = _resolve_max_workers(req)
        self.rag_on = _resolve_setting(req, "rag", "RAG", "on", {"on", "off"}) == "on"

    def as_dict(self) -> dict[str, Any]:
        return {
            "orch_mode": self.orch_mode, "polish": self.polish,
            "max_workers": self.max_workers, "rag_on": self.rag_on,
        }


# ===========================================================================
# Request-shape classification — the A–E decision.
# ===========================================================================
_SINGLE_SHORT_WORDS = 350  # at/below this single-piece word target, prefer scenario E


def _has_chapters(req: dict) -> Optional[list]:
    """Return an explicit chapter list if the request carries one (scenario A)."""
    for key in ("chapters", "outline", "titles"):
        v = req.get(key)
        if isinstance(v, list) and v:
            return v
    return None


def _wants_multichapter(req: dict) -> bool:
    """True if a topic-only request still implies a multi-chapter book/script
    (scenario B) rather than a single open-ended piece (scenario D)."""
    if req.get("multi_chapter") is True:
        return True
    nc = req.get("n_chapters") or req.get("num_chapters")
    try:
        if nc and int(nc) > 1:
            return True
    except (TypeError, ValueError):
        pass
    # A book/script kind with a topic implies chapters.
    kind = str(req.get("kind", req.get("type", "")) or "").strip().lower()
    return kind in {"book", "script", "narration", "chapters", "series"}


def _normalize_titles(chapters: list) -> list[dict]:
    """Coerce an explicit chapter/title list into the dict shape the static map
    expects ({id, title, summary, word_target}). Accepts strings or dicts."""
    out: list[dict] = []
    for i, ch in enumerate(chapters, 1):
        if isinstance(ch, dict):
            out.append({
                "id": ch.get("id", i),
                "title": str(ch.get("title", ch.get("name", "")) or "").strip(),
                "summary": str(ch.get("summary", ch.get("description", "")) or "").strip(),
                "word_target": int(ch.get("word_target", ch.get("words", 800)) or 800),
            })
        else:
            out.append({"id": i, "title": str(ch).strip(), "summary": "", "word_target": 800})
    return out


def classify(req: dict, settings: "_Settings") -> str:
    """Return the scenario letter A..E for a request given resolved settings.

    Priority: E (single/short escape hatch) > A (explicit chapters) >
              B (topic + multi-chapter intent) > C (brief) > D (open-ended).
    ORCH_MODE coerces: 'static' demotes B->B-static (handled in dispatch) and
    forces D->C-ish via cowork; 'dynamic' promotes A/B/C toward D (plan_subtasks).
    """
    topic = str(req.get("topic", "") or "").strip()
    brief = str(req.get("brief", "") or "").strip()
    goal = str(req.get("goal", req.get("prompt", "")) or "").strip()

    # --- E: explicit single / short single-piece ask -> one call, no orchestrator.
    if req.get("single") is True:
        return "E"
    word_target = req.get("word_target") or req.get("words")
    if not _has_chapters(req) and not _wants_multichapter(req):
        try:
            if word_target and int(word_target) <= _SINGLE_SHORT_WORDS:
                return "E"
        except (TypeError, ValueError):
            pass

    # --- forced dynamic: treat anything as an open-ended goal to decompose.
    if settings.orch_mode == "dynamic":
        # but a real chapter list is still best served as a chaptered book.
        return "A" if _has_chapters(req) else ("B" if (topic and _wants_multichapter(req)) else "D")

    # --- A: explicit chapter list present.
    if _has_chapters(req):
        return "A"

    # --- B: topic only, multi-chapter intent.
    if topic and _wants_multichapter(req):
        return "B"

    # --- C: a brief to render as one piece.
    if brief:
        return "C"

    # --- B (looser): a bare topic with no single/brief signal defaults to a book.
    if topic:
        return "B"

    # --- D: open-ended goal / prompt with nothing more specific.
    if goal:
        return "D"

    # Nothing usable -> treat the whole req as a goal string if any text exists.
    return "D"


# ===========================================================================
# generate_narration — the async front door.
# ===========================================================================
async def generate_narration(req: dict) -> dict[str, Any]:
    """Dispatch a narration request to the right strategy and return a unified result.

    `req` keys (all optional; the SHAPE drives routing — see the A–E table):
      topic            : str  — book/script subject (scenarios A/B).
      chapters/outline : list — explicit chapter list/titles (scenario A).
      titles           : list — alias for an explicit chapter list.
      brief            : str  — a single brief to render as one piece (scenario C).
      goal/prompt      : str  — open-ended goal to decompose (scenario D).
      n_chapters       : int  — desired chapter count for scenario B.
      word_target/words: int  — single-piece word target (scenario E threshold).
      single           : bool — force scenario E (one call, no orchestrator).
      style, language, mode, tenant_id, job_id : narration params (passed through).
      orch_mode/polish/max_workers/rag : per-request overrides of the env settings.

    NEVER raises. Returns at minimum:
        {"ok": bool, "scenario": "A".."E", "strategy": str, "settings": {...}, ...}
    plus the scenario-specific payload (book/chapters or output).
    """
    req = dict(req or {})
    settings = _Settings(req)

    style = req.get("style")
    language = str(req.get("language", "id") or "id")
    mode = str(req.get("mode", "text") or "text")
    tenant_id = req.get("tenant_id")
    job_id = str(req.get("job_id", "") or "")
    telemetry_sink = req.get("telemetry_sink")

    scenario = classify(req, settings)
    log.info("router: scenario=%s settings=%s", scenario, settings.as_dict())

    base = {"scenario": scenario, "settings": settings.as_dict()}

    try:
        if scenario == "E":
            result = await _run_single(req, style=style, telemetry_sink=telemetry_sink)

        elif scenario == "A":
            chapters = _normalize_titles(_has_chapters(req) or [])
            result = await _run_chaptered(
                req, chapters, settings, style=style, language=language, mode=mode,
                tenant_id=tenant_id, job_id=job_id, telemetry_sink=telemetry_sink,
            )

        elif scenario == "B":
            result = await _run_topic_to_book(
                req, settings, style=style, language=language, mode=mode,
                tenant_id=tenant_id, job_id=job_id, telemetry_sink=telemetry_sink,
            )

        elif scenario == "C":
            result = await _static.cowork(
                str(req.get("brief", "") or ""),
                style=style, language=language,
                polish=("none" if settings.polish == "none" else "synthesize"),
                worker_model=req.get("worker_model"),
                manager_model=req.get("manager_model"),
                telemetry_sink=telemetry_sink,
                max_workers=settings.max_workers,
            )

        else:  # scenario == "D"
            result = await _run_dynamic_goal(
                req, settings, style=style, language=language,
                telemetry_sink=telemetry_sink,
            )
    except Exception as exc:  # noqa: BLE001 - router must never raise into the caller
        log.exception("router: scenario %s failed unexpectedly", scenario)
        return {**base, "ok": False, "strategy": "error", "error": f"{type(exc).__name__}: {exc}",
                "output": None}

    result = dict(result or {})
    result.update(base)
    result.setdefault("ok", False)
    return result


# ---------------------------------------------------------------------------
# Scenario E — single short call, no orchestrator.
# ---------------------------------------------------------------------------
async def _run_single(req: dict, *, style: Optional[str],
                      telemetry_sink: Optional[Any]) -> dict[str, Any]:
    """One run_worker call. No fan-out, no manager, no shared context."""
    prompt = str(
        req.get("brief") or req.get("goal") or req.get("prompt") or req.get("topic") or ""
    ).strip()
    language = str(req.get("language", "id") or "id")
    word_target = req.get("word_target") or req.get("words")
    ask = prompt
    if word_target:
        ask = f"{prompt}\n\nWrite about {word_target} words. Output language: {language}."
    worker = Worker(
        name="single", role="worker",
        model=req.get("worker_model") or route_model(role="worker", style=style),
        style=style, telemetry_sink=telemetry_sink,
    )
    res = await run_worker(worker, ask, timeout=float(req.get("timeout", 120.0)), task_id="single")
    return {
        "ok": bool(res.get("ok")), "output": res.get("output"),
        "model": res.get("model"), "strategy": "single",
        "error": res.get("error") if not res.get("ok") else None,
    }


# ---------------------------------------------------------------------------
# Scenario A — explicit chapters -> static chapter map.
# ---------------------------------------------------------------------------
async def _run_chaptered(req: dict, chapters: list[dict], settings: "_Settings", *,
                         style, language, mode, tenant_id, job_id,
                         telemetry_sink) -> dict[str, Any]:
    # RAG off: build a shared context with no retrieval by handing narrate_chapters
    # a pre-built (empty-RAG) context. We do this by setting RAG_PREFER nothing and
    # passing shared_context explicitly when rag is off.
    shared = None
    if not settings.rag_on:
        shared = await _no_rag_context(req.get("topic", ""), chapters, tenant_id, style)
    return await _static.narrate_chapters(
        str(req.get("topic", "") or ""), chapters,
        style=style, language=language, mode=mode, tenant_id=tenant_id, job_id=job_id,
        polish=settings.polish,
        worker_model=req.get("worker_model"), manager_model=req.get("manager_model"),
        telemetry_sink=telemetry_sink, max_parallel=settings.max_workers,
        shared_context=shared,
    )


# ---------------------------------------------------------------------------
# Scenario B — topic only -> dynamic outline (or static fallback) -> static map.
# ---------------------------------------------------------------------------
async def _run_topic_to_book(req: dict, settings: "_Settings", *,
                             style, language, mode, tenant_id, job_id,
                             telemetry_sink) -> dict[str, Any]:
    topic = str(req.get("topic", "") or "").strip()
    n_chapters = req.get("n_chapters") or req.get("num_chapters") or 5
    try:
        n_chapters = max(1, int(n_chapters))
    except (TypeError, ValueError):
        n_chapters = 5
    words_per = int(req.get("words_per_chapter", req.get("word_target", 800)) or 800)

    if settings.orch_mode == "static":
        # Forced static: no manager outline call — use the deterministic outline.
        outline_res = {
            "chapters": _dynamic._static_outline(topic, n_chapters, words_per),
            "source": "fallback-forced-static",
        }
    else:
        outline_res = await _dynamic.outline_from_topic(
            topic, n_chapters=n_chapters, style=style, language=language,
            words_per_chapter=words_per, manager_model=req.get("manager_model"),
            telemetry_sink=telemetry_sink,
        )

    chapters = outline_res["chapters"]
    book = await _run_chaptered(
        req, chapters, settings, style=style, language=language, mode=mode,
        tenant_id=tenant_id, job_id=job_id, telemetry_sink=telemetry_sink,
    )
    book["outline_source"] = outline_res.get("source")
    book["outline"] = chapters
    book["strategy"] = "topic_to_book"
    return book


# ---------------------------------------------------------------------------
# Scenario D — open-ended goal -> dynamic plan_subtasks -> parallel -> synthesize.
# ---------------------------------------------------------------------------
async def _run_dynamic_goal(req: dict, settings: "_Settings", *,
                            style, language, telemetry_sink) -> dict[str, Any]:
    import asyncio  # local: keep module import light
    goal = str(req.get("goal") or req.get("prompt") or req.get("topic") or req.get("brief") or "").strip()

    plan = await _dynamic.plan_subtasks(
        goal, n=req.get("n_workers"), manager_model=req.get("manager_model"),
        max_workers=settings.max_workers, telemetry_sink=telemetry_sink,
    )
    subtasks = plan["subtasks"]
    w_model = req.get("worker_model") or route_model(role="worker", style=style)

    async def _run_one(st: dict) -> dict[str, Any]:
        worker = Worker(
            name=f"dyn:{st.get('role','task')}", role="worker", model=w_model,
            style=style, system=f"You are the {st.get('role','specialist')}.",
            telemetry_sink=telemetry_sink,
        )
        out = f"GOAL:\n{goal}\n\nYOUR SUBTASK:\n{st.get('instruction','')}\n\nOutput language: {language}."
        r = await run_worker(worker, out, timeout=float(req.get("timeout", 120.0)),
                             task_id=f"dyn:{st.get('role','task')}")
        r["role_name"] = st.get("role", "task")
        return r

    results = await asyncio.gather(*(_run_one(st) for st in subtasks))
    usable = [r for r in results if r.get("ok") and r.get("output")]

    parts_summary = [
        {"name": r.get("role_name", ""), "ok": bool(r.get("ok")),
         "output": r.get("output"), "model": r.get("model", "")}
        for r in results
    ]
    if not usable:
        return {"ok": False, "output": None, "strategy": "dynamic_goal",
                "plan_source": plan.get("source"), "subtasks": parts_summary,
                "error": "all_subtasks_failed"}

    if settings.polish == "none" and len(usable) == 1:
        only = usable[0]
        return {"ok": True, "output": only["output"], "strategy": "dynamic_goal",
                "plan_source": plan.get("source"), "subtasks": parts_summary,
                "model": only.get("model")}

    from .core import synthesize
    synth = await synthesize(
        f"Combine these parts into ONE coherent piece that fulfils the goal:\n{goal}",
        usable, role="synthesize", model=req.get("manager_model") or MANAGER_MODEL,
        telemetry_sink=telemetry_sink, task_id="dyn:synthesize",
    )
    return {
        "ok": bool(synth.get("ok")), "output": synth.get("output"),
        "strategy": "dynamic_goal", "plan_source": plan.get("source"),
        "subtasks": parts_summary, "model": synth.get("model"),
        "error": synth.get("error") if not synth.get("ok") else None,
    }


# ---------------------------------------------------------------------------
# RAG-off helper — build a shared context WITHOUT retrieval (RAG=off override).
# ---------------------------------------------------------------------------
async def _no_rag_context(topic: str, chapters: list[dict], tenant_id, style):
    """Build a SharedContext with the style guide loaded but NO RAG passages.

    We get this by calling build_shared_context with an empty topic, which short-
    circuits the single retrieval (it only retrieves when ctx.topic is truthy), then
    restoring the real topic on the returned context so outline()/scope_for() still
    describe the right book. Never raises.
    """
    ctx = await build_shared_context("", chapters, tenant_id, style=style)
    ctx.topic = str(topic or "").strip()
    ctx.rag_used = False
    return ctx


__all__ = [
    "generate_narration",
    "classify",
]
