# -*- coding: utf-8 -*-
"""
Project Dalang — orchestrator.dynamic (WS-6, the *dynamic* decomposer).

Where `orchestrator.static` works from a FIXED plan (known roles, known chapter
list), this module ASKS THE MANAGER MODEL to invent the plan when the shape of the
work is not known up-front. Two planners live here:

  * plan_subtasks()       — given an open-ended goal, ask the manager for a
                            VARIABLE list of 1..MAX_WORKERS subtasks as JSON, each
                            with {role/title, instruction}. This is what makes the
                            engine "dynamic": the decomposition adapts to the goal
                            instead of using a canned role list. This is the
                            consolidation of the old `cowork_llm-dynamic.py`.

  * outline_from_topic()  — given ONLY a topic (no chapter titles), ask the manager
                            for N chapter {title, summary, words} entries as JSON,
                            so a bare topic can feed `static.narrate_chapters`.

THE LOAD-BEARING REQUIREMENT (WS-6): the JSON parser must be ROBUST and must NEVER
crash. LLMs wrap JSON in ```json fences, add prose before/after, use single quotes,
trail commas, or return nothing usable. `_parse_json_loose` strips and repairs the
common failure modes; if it STILL can't parse, both planners FALL BACK to a
deterministic static plan so the pipeline always has something to run. A bad-JSON
manager response degrades the plan — it never takes the job down.

Built on WS-1 primitives only (Worker / run_worker / MANAGER_MODEL / MAX_WORKERS);
no network or DB of its own.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from .core import (
    Worker,
    run_worker,
    MANAGER_MODEL,
    MAX_WORKERS,
)

log = logging.getLogger("orchestrator.dynamic")


# ===========================================================================
# Robust JSON extraction — the never-crash heart of the dynamic planners.
# ===========================================================================
_FENCE_RE = re.compile(r"```(?:json|javascript|js)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def _strip_fences(text: str) -> str:
    """Return the contents of the first ```...``` code fence, else the text as-is."""
    m = _FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    return text.strip()


def _slice_to_brackets(text: str) -> str:
    """Slice from the first opening bracket to the last matching closing bracket.

    Prefers an array (`[ ... ]`) since both planners want a list; falls back to an
    object (`{ ... }`). This discards prose the model wrapped around the JSON.
    """
    starts = [i for i in (text.find("["), text.find("{")) if i != -1]
    if not starts:
        return text
    start = min(starts)
    open_ch = text[start]
    close_ch = "]" if open_ch == "[" else "}"
    end = text.rfind(close_ch)
    if end == -1 or end < start:
        return text
    return text[start:end + 1]


def _parse_json_loose(text: str) -> Optional[Any]:
    """Best-effort JSON parse. Returns the parsed object, or None if unrecoverable.

    Repairs the common LLM-JSON failure modes, in order:
      1. strip ```json fences and surrounding prose
      2. slice to the outermost [...] / {...}
      3. drop trailing commas before } or ]
      4. as a last resort, swap single quotes for double quotes
    NEVER raises — any failure returns None so the caller can fall back.
    """
    if not text or not text.strip():
        return None

    candidate = _slice_to_brackets(_strip_fences(text))

    attempts = [
        candidate,
        _TRAILING_COMMA_RE.sub(r"\1", candidate),
    ]
    # Last-ditch: single→double quotes (only if there are no double quotes already,
    # to avoid mangling apostrophes inside properly-quoted strings).
    if '"' not in candidate and "'" in candidate:
        attempts.append(
            _TRAILING_COMMA_RE.sub(r"\1", candidate.replace("'", '"'))
        )

    for attempt in attempts:
        try:
            return json.loads(attempt)
        except Exception:  # noqa: BLE001 - try the next repair
            continue
    log.info("dynamic: JSON parse failed across %d repair attempts", len(attempts))
    return None


def _clamp_workers(n: int, max_workers: int) -> int:
    """Clamp a requested worker count into [1, max_workers]."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 1
    return max(1, min(max_workers, n))


# ===========================================================================
# plan_subtasks() — decompose an open-ended goal into a variable subtask list.
# ===========================================================================
def _static_subtask_plan(goal: str, n: int, max_workers: int) -> list[dict]:
    """Deterministic fallback plan when the manager's JSON is unusable.

    Splits the goal into a small fixed set of complementary angles so the pipeline
    always has SOMETHING coherent to fan out. Sized to the requested/allowed count.
    """
    base = [
        {"role": "researcher",
         "instruction": f"Establish the factual spine for: {goal}. Surface concrete "
                        "facts, names, dates and causes; mark anything uncertain [VERIFY: ...]."},
        {"role": "writer",
         "instruction": f"Write the main narrative for: {goal}. Scene-first, momentum, "
                        "concrete imagery; honour the facts."},
        {"role": "stylist",
         "instruction": f"Refine voice and rhythm for: {goal}. Vary sentence length, "
                        "cut cliché and filler, hold one register."},
        {"role": "skeptic",
         "instruction": f"Stress-test the claims for: {goal}. Name counter-views, hedge "
                        "thin evidence honestly, do not invent specifics."},
        {"role": "synthesist",
         "instruction": f"Draft an integrative take on: {goal}, weaving the strongest "
                        "facts, drama and honesty into one coherent piece."},
        {"role": "closer",
         "instruction": f"Write a resonant close for: {goal} that lands the implication "
                        "without re-summarising."},
    ]
    k = _clamp_workers(n or 4, max_workers)
    return base[:k] if k <= len(base) else base + [
        {"role": f"angle_{i}",
         "instruction": f"Develop an additional distinct angle ({i}) on: {goal}."}
        for i in range(len(base) + 1, k + 1)
    ]


def _normalize_subtasks(parsed: Any, goal: str, max_workers: int) -> list[dict]:
    """Coerce a parsed manager response into a clean [{role, instruction}] list.

    Accepts a bare list, or an object with a "subtasks"/"tasks"/"plan" array.
    Drops malformed entries; clamps to max_workers. Returns [] if nothing usable
    (the caller then falls back to the static plan)."""
    items: Any = None
    if isinstance(parsed, list):
        items = parsed
    elif isinstance(parsed, dict):
        for key in ("subtasks", "tasks", "plan", "steps", "workers"):
            if isinstance(parsed.get(key), list):
                items = parsed[key]
                break
    if not isinstance(items, list):
        return []

    out: list[dict] = []
    for i, it in enumerate(items, 1):
        if isinstance(it, dict):
            role = str(it.get("role", it.get("title", it.get("name", f"task_{i}"))) or f"task_{i}").strip()
            instr = str(it.get("instruction", it.get("task", it.get("description", ""))) or "").strip()
        elif isinstance(it, str):
            role, instr = f"task_{i}", it.strip()
        else:
            continue
        if not instr:
            continue
        out.append({"role": role or f"task_{i}", "instruction": instr})
        if len(out) >= max_workers:
            break
    return out


_PLAN_SYSTEM = (
    "You are a planning manager that decomposes a creative/writing goal into a "
    "SMALL set of parallel subtasks for specialist writer-agents. Return ONLY JSON."
)


def _plan_prompt(goal: str, max_workers: int, hint_n: Optional[int]) -> str:
    n_clause = (
        f"Use about {hint_n} subtasks."
        if hint_n
        else f"Choose the RIGHT number of subtasks (between 1 and {max_workers}) for the goal — "
             "simple goals need fewer, complex ones need more."
    )
    return (
        f"GOAL:\n{goal}\n\n"
        f"Decompose this into parallel subtasks for specialist writers. {n_clause} "
        f"Never exceed {max_workers} subtasks. Each subtask must cover a DISTINCT "
        "angle (e.g. research/facts, narrative/scene, voice/style, skeptic/counter-view) "
        "so the parts complement rather than duplicate each other.\n\n"
        "Return ONLY a JSON array, no prose, no code fences, in EXACTLY this shape:\n"
        '[{"role": "short role name", "instruction": "what this writer should produce"}]'
    )


async def plan_subtasks(
    goal: str,
    *,
    n: Optional[int] = None,
    manager_model: Optional[str] = None,
    max_workers: int = MAX_WORKERS,
    timeout: float = 90.0,
    telemetry_sink: Optional[Any] = None,
) -> dict[str, Any]:
    """Ask the manager for a VARIABLE list of 1..max_workers subtasks (as JSON).

    Robust: parses loosely and, on ANY failure (call failed, empty, bad JSON, no
    usable entries), FALLS BACK to a deterministic static plan — never crashes.

    Returns:
        {
          "subtasks": [ {role, instruction}, ... ],   # length 1..max_workers
          "source": "manager" | "fallback",
          "model": <manager model>,
          "ok": True,                                  # always True: there's always a plan
        }
    """
    max_workers = max(1, int(max_workers or 1))
    m_model = manager_model or MANAGER_MODEL

    worker = Worker(
        name="planner:subtasks", role="manager", model=m_model,
        system=_PLAN_SYSTEM, temperature=0.3, telemetry_sink=telemetry_sink,
    )
    res = await run_worker(
        worker, _plan_prompt(goal, max_workers, n),
        timeout=timeout, task_id="planner:subtasks",
    )

    subtasks: list[dict] = []
    if res.get("ok") and res.get("output"):
        parsed = _parse_json_loose(res["output"])
        subtasks = _normalize_subtasks(parsed, goal, max_workers)

    if subtasks:
        return {"subtasks": subtasks, "source": "manager", "model": m_model, "ok": True}

    log.info("plan_subtasks: falling back to static plan (manager JSON unusable)")
    return {
        "subtasks": _static_subtask_plan(goal, n or 4, max_workers),
        "source": "fallback", "model": m_model, "ok": True,
    }


# ===========================================================================
# outline_from_topic() — invent N chapter titles when only a topic is given.
# ===========================================================================
def _static_outline(topic: str, n: int, words_per_chapter: int) -> list[dict]:
    """Deterministic fallback outline: N generically-titled chapters covering the
    topic from opening through development to close. Always produces a runnable
    outline so a bare topic never dead-ends."""
    n = max(1, int(n or 5))
    out: list[dict] = []
    for i in range(1, n + 1):
        if i == 1:
            title = f"Opening: Entering the world of {topic}"
            summary = f"Set the scene and hook the reader into {topic}."
        elif i == n:
            title = f"Closing: What {topic} leaves us with"
            summary = f"Land the payoff and lasting implication of {topic}."
        else:
            title = f"{topic} — part {i}"
            summary = f"Develop a distinct facet of {topic} (segment {i} of {n})."
        out.append({"id": i, "title": title, "summary": summary, "word_target": words_per_chapter})
    return out


def _normalize_outline(parsed: Any, topic: str, n: int, words_per_chapter: int) -> list[dict]:
    """Coerce a parsed manager response into a clean chapter list. Accepts a bare
    list or an object wrapping a "chapters"/"outline" array. Empty -> caller falls back."""
    items: Any = None
    if isinstance(parsed, list):
        items = parsed
    elif isinstance(parsed, dict):
        for key in ("chapters", "outline", "sections"):
            if isinstance(parsed.get(key), list):
                items = parsed[key]
                break
    if not isinstance(items, list):
        return []

    out: list[dict] = []
    for i, it in enumerate(items, 1):
        if isinstance(it, dict):
            title = str(it.get("title", it.get("name", "")) or "").strip()
            summary = str(it.get("summary", it.get("description", it.get("desc", ""))) or "").strip()
            words = it.get("word_target", it.get("words"))
        elif isinstance(it, str):
            title, summary, words = it.strip(), "", None
        else:
            continue
        if not title and not summary:
            continue
        try:
            wt = int(words) if words else words_per_chapter
        except (TypeError, ValueError):
            wt = words_per_chapter
        out.append({
            "id": i,
            "title": title or f"{topic} — part {i}",
            "summary": summary,
            "word_target": wt,
        })
    return out


_OUTLINE_SYSTEM = (
    "You are an editor who designs the chapter structure of a narrative book or "
    "long-form script from a single topic. Return ONLY JSON."
)


def _outline_prompt(topic: str, n: int, style: Optional[str], language: str,
                    words_per_chapter: int) -> str:
    style_clause = f" in a {style} style" if style else ""
    return (
        f"TOPIC:\n{topic}\n\n"
        f"Design a {n}-chapter outline{style_clause}. The chapters must progress "
        "logically (a hook that opens, development in the middle, a resonant close) "
        "and must NOT overlap — each chapter owns a distinct part of the story so "
        f"parallel writers won't repeat each other. Titles and summaries in {language}.\n\n"
        "Return ONLY a JSON array, no prose, no code fences, in EXACTLY this shape:\n"
        '[{"title": "chapter title", "summary": "1-2 sentences on what it covers", '
        f'"words": {words_per_chapter}}}]'
    )


async def outline_from_topic(
    topic: str,
    *,
    n_chapters: int = 5,
    style: Optional[str] = None,
    language: str = "id",
    words_per_chapter: int = 800,
    manager_model: Optional[str] = None,
    timeout: float = 90.0,
    telemetry_sink: Optional[Any] = None,
) -> dict[str, Any]:
    """Ask the manager for `n_chapters` chapter {title, summary, words} entries.

    Robust like plan_subtasks: on any failure FALLS BACK to a deterministic
    generic outline so a bare topic always yields a runnable chapter list.

    Returns:
        {
          "chapters": [ {id, title, summary, word_target}, ... ],
          "source": "manager" | "fallback",
          "model": <manager model>,
          "ok": True,
        }
    """
    n = max(1, int(n_chapters or 5))
    m_model = manager_model or MANAGER_MODEL

    worker = Worker(
        name="planner:outline", role="manager", model=m_model,
        system=_OUTLINE_SYSTEM, temperature=0.5, telemetry_sink=telemetry_sink,
    )
    res = await run_worker(
        worker, _outline_prompt(topic, n, style, language, words_per_chapter),
        timeout=timeout, task_id="planner:outline",
    )

    chapters: list[dict] = []
    if res.get("ok") and res.get("output"):
        parsed = _parse_json_loose(res["output"])
        chapters = _normalize_outline(parsed, topic, n, words_per_chapter)

    if chapters:
        return {"chapters": chapters, "source": "manager", "model": m_model, "ok": True}

    log.info("outline_from_topic: falling back to static outline (manager JSON unusable)")
    return {
        "chapters": _static_outline(topic, n, words_per_chapter),
        "source": "fallback", "model": m_model, "ok": True,
    }


__all__ = [
    "plan_subtasks",
    "outline_from_topic",
    "_parse_json_loose",
    "_static_subtask_plan",
    "_static_outline",
]
