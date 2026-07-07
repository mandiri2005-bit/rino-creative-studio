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
import os
import re
from typing import Any, Optional

from .core import (
    Worker,
    run_worker,
    MANAGER_MODEL,
    WORKER_MODEL,
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
        "parallel writers won't repeat each other. Order the chapters in CHRONOLOGICAL "
        "sequence — no chapter set earlier in time than the one before it (no rewinding to "
        "an earlier event as a whole chapter). And make sure every EXTERNAL stake the story "
        "opens — a deadline, a debt, a threat, a search — is RESOLVED by the final chapters, "
        "never left dangling. Make the STRUCTURE original: build it from THIS topic's specific "
        "hook, NOT as a re-skin of a famous novel/film with the details swapped, and NOT as the "
        "over-used \"a buried document/record exposes a hidden past crime\" shape — if it drifts "
        "there, choose a different engine. And IF the premise poses a DISTINCTIVE hook — a "
        "recurring image, an anomaly, a specific mystery the opening raises and frames as a "
        "question to answer — make sure a specific late chapter DELIVERS its answer on the page, "
        "rather than letting a generic sub-plot (a fraud, a buried record) resolve while the hook "
        "hangs. A premise with no such posed puzzle owes no answer-chapter — do not invent one. "
        f"Titles and summaries in {language}.\n\n"
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


# ===========================================================================
# STORY BIBLE — pin the load-bearing facts BEFORE the parallel chapter MAP.
# Root cause of the batch's cross-chapter contradictions (the star that is a red
# giant in ch1 and a supernova remnant in ch6; the genealogy that flips clan
# between ch2 and ch3; the workshop that changes cities): N chapter-writers run in
# PARALLEL and none sees the others' drafts, while the outline entries are only a
# title + a 1-2 sentence summary — so each writer INVENTS the specifics (the
# object's identity, the names, the places, the dates) independently. This one
# manager call decides those specifics ONCE and rides them into every worker via
# ctx.canonical_facts (the cached SYSTEM prefix), so all chapters inherit the same
# world. Prevention that complements the post-hoc #53 consistency critic.
# ===========================================================================
# FICTION variant — the writers INVENT the world, so the bible DECIDES the specifics once.
_STORY_BIBLE_SYSTEM_FICTION = (
    "You are the STORY BIBLE editor for a multi-chapter FICTION narrative. N writers will each "
    "draft ONE chapter of this story IN PARALLEL, and none of them can see the others' drafts. "
    "Produce the single canonical fact-sheet they must ALL obey so their chapters do not "
    "contradict each other. DECIDE and FIX the load-bearing specifics now, once, so every writer "
    "inherits the same world.\n\n"
    "Pin ONLY facts that must stay constant across the whole book:\n"
    "1. CHARACTERS — each named person: EXACT name spelling, BIRTH YEAR / age, hometown or origin, "
    "parents & lineage (biological vs adopted, where the plot turns on it), role, one-line "
    "identity, key relationships. Fix ALL of these, not just the name. TWO failure modes: (a) the "
    "cast MUTATES — one grandfather becoming three names, one child becoming four; (b) even when "
    "names hold, ATTRIBUTES drift — a birth year that is a clue splitting into two values, a "
    "father's name and city changing between chapters, 'biological' quietly becoming 'adopted'. "
    "One person, one FIXED set of facts, in every chapter. NAME DIVERSITY: choose DISTINCT, "
    "fresh names and vary them across the cast — avoid the over-used defaults an LLM reaches for "
    "first (for Korean settings do NOT default to Min-woo / Seo / Park / Ji-min / Mi-young; apply "
    "the same 'avoid the obvious default' rule to any culture), and make sure no two characters "
    "share a starting syllable or rhyme, so the reader never confuses them.\n"
    "2. CENTRAL SUBJECT — the ONE object / place / phenomenon the whole story turns on. Fix its "
    "identity and EVERY measurable attribute (type, size, age, distance, material, provenance, "
    "COUNT) so it is unmistakably the SAME thing in chapter 1 and the last chapter. If the "
    "premise implies science/domain facts, make them internally correct and mutually consistent.\n"
    "3. SETTING — every named location, pinned once (country, city, building, floor). One place, "
    "one name, one spelling.\n"
    "4. TIMELINE & QUANTITIES — the season, total elapsed span, dated offsets (\"3 years since "
    "X\"), and any COUNTS (how many colonies/chapters/people). Keep them arithmetically "
    "consistent and MONOTONIC — a duration cannot shrink as the story moves forward. Also lay "
    "out a FORWARD CHAPTER TIMELINE: place each chapter, in outline order, at its point in time "
    "(Ch1 earliest → last chapter latest) so no chapter is set earlier than the one before it — "
    "the story must not step backward between consecutive chapters.\n"
    "5. POV & NARRATION — who narrates and whose head we are in (per chapter if it rotates), "
    "first vs third person, and tense. Fix this so no chapter silently switches viewpoint.\n"
    "6. KEY FACTS / REVEAL — the plot facts that must not drift (who did what to whom, the "
    "secret, the diagnosis, the twist). Choose a SINGLE version of each reveal and state it once. "
    "If the premise is a MYSTERY / thriller / has a twist: COMMIT the true solution NOW (the real "
    "identity, what actually happened, the kinship/blood facts) and make every clue, document, "
    "date, and birth year the chapters plant CONSISTENT with that solution — the reader assembles "
    "the answer, so a middle-act clue that the ending contradicts (e.g. investigating a biological "
    "lineage the finale reveals never existed) breaks the whole book. Clues derive FROM the "
    "committed answer; they are never improvised against it.\n"
    "7. OPEN THREADS TO RESOLVE — list every EXTERNAL stake the premise raises (a deadline, a "
    "debt, a foreclosure, a lawsuit, a threat, a missing person, a corporate/legal reckoning). "
    "Each one MUST be paid off — resolved on the page by the final chapters, never dropped or "
    "left off-screen. Note, per thread, roughly which late chapter delivers its outcome.\n"
    "8. SIGNATURE HOOK & ITS PAYOFF — name the ONE distinctive device, mystery, image, or "
    "phenomenon that makes THIS premise original: the specific thing the opening chapters pose "
    "with weight and promise the reader an answer to (an anomaly, a recurring motif, a question "
    "the premise ITSELF raises — NOT a generic sub-plot). This is DIFFERENT from the external "
    "stakes in #7. Commit (a) what the hook is, (b) its diegetic ANSWER delivered ON THE PAGE, and "
    "(c) which late chapter delivers it. The hook is LOAD-BEARING: the story's more familiar "
    "machinery (a fraud, a buried document, a family secret, a legal reckoning) must SERVE this "
    "hook, never REPLACE it — a signature mystery set up with portent then abandoned while a stock "
    "sub-plot resolves is the single worst failure. If the premise genuinely has no distinctive "
    "device, pin instead the central DRAMATIC QUESTION the opening raises and the chapter that "
    "answers it.\n\n"
    "Rules: DECIDE concrete values even where the outline is vague — that is the entire point, "
    "pin them. Do NOT write prose, plot beats, or chapter content. Keep it tight — a numbered "
    "fact-sheet under the headings above, one fact per line, no preamble."
)

# NONFICTION / HYBRID variant — factual content: PIN only, NEVER fabricate. This is the
# fabrication-safety guarantee (review finding): a real place/person/date must not have an
# invented provenance canonized into the shared prefix.
_STORY_BIBLE_SYSTEM_NONFICTION = (
    "You are the CONTINUITY SHEET editor for a multi-chapter NONFICTION / factual piece. N "
    "writers will each draft ONE chapter IN PARALLEL, none seeing the others'. Produce the single "
    "continuity sheet they must ALL obey so the chapters stay consistent about WHO and WHAT the "
    "piece is about.\n\n"
    "CRITICAL — this is factual content. You may ONLY pin facts the premise/topic states or "
    "clearly implies. Do NOT invent names, dates, numbers, statistics, quotes, or events that "
    "are not given. Where a specific is needed but not established, write \"[VERIFY: ...]\" so an "
    "editor fills it — NEVER fabricate it, and NEVER invent a history or provenance for a real "
    "place or person.\n\n"
    "Pin, from the premise only:\n"
    "1. SUBJECT / PEOPLE — the real subject(s) and figures named, with exact names/spellings and "
    "roles; keep them consistent (no renaming, no invented middle names).\n"
    "2. CENTRAL SUBJECT — the one thing the piece is about; its GIVEN attributes only.\n"
    "3. SETTING — named real places, spelled consistently; do not invent their backstory.\n"
    "4. TIMELINE — the ordering/era the premise establishes; unknown dates → [VERIFY].\n"
    "5. POV & NARRATION — the narrating stance and tense to hold throughout.\n"
    "6. KEY CLAIMS — the through-line the premise sets; anything not given → [VERIFY], not invented.\n\n"
    "Rules: consistency, not invention. Do NOT write prose. Numbered sheet, one item per line, no preamble."
)


def _story_bible_prompt(topic: str, outline: list[dict], language: str, is_fiction: bool) -> str:
    ol_lines = []
    for i, c in enumerate(outline or []):
        cid = c.get("id", i + 1) if isinstance(c, dict) else i + 1
        title = str((c.get("title", "") if isinstance(c, dict) else "") or "").strip()
        summ = str((c.get("summary", c.get("description", "")) if isinstance(c, dict) else "") or "").strip()
        ol_lines.append(f"- Ch{cid}: {title}" + (f" — {summ}" if summ else ""))
    ol = "\n".join(ol_lines) if ol_lines else "(no outline)"
    label = "STORY BIBLE" if is_fiction else "CONTINUITY SHEET"
    heads = ("CHARACTERS, CENTRAL SUBJECT, SETTING, TIMELINE & QUANTITIES, POV & NARRATION, "
             "KEY FACTS, OPEN THREADS, SIGNATURE HOOK & PAYOFF"
             if is_fiction else
             "SUBJECT/PEOPLE, CENTRAL SUBJECT, SETTING, TIMELINE, POV & NARRATION, KEY CLAIMS")
    return (
        f"TOPIC / PREMISE:\n{topic}\n\n"
        f"CHAPTER OUTLINE ({len(outline or [])} chapters):\n{ol}\n\n"
        f"Write the {label} in {language}. Output ONLY the numbered sheet under the headings "
        f"({heads}). One item per line. No preamble, no prose, no chapter text."
    )


async def build_story_bible(
    topic: str,
    outline: list[dict],
    *,
    is_fiction: bool = True,
    style: Optional[str] = None,
    language: str = "id",
    manager_model: Optional[str] = None,
    timeout: Optional[float] = None,
    telemetry_sink: Optional[Any] = None,
) -> str:
    """ONE manager call → a canonical fact-sheet pinning the piece's load-bearing specifics
    (names, the central subject's fixed identity, locations, timeline, POV, key reveal) so
    parallel chapter-writers cannot contradict each other.

    is_fiction toggles the regime: FICTION decides/invents the specifics; NONFICTION pins only
    what the premise gives and marks unknowns [VERIFY] (never fabricates — fabrication-safety).

    MODEL FAILOVER (not just a longer timeout): the bible is load-bearing — no bible ⟹ cross-chapter
    drift (the "The Names They Left" 7.0: the manager model timed out at 120s on a degraded provider
    window, so it generated with NO bible). A healthy bible measured ~90s on the manager model, only
    ~30s under the wall. Rather than one longer attempt (which just stalls longer on a hung provider),
    keep the per-attempt timeout at NARASI_BIBLE_TIMEOUT (120s) and FAIL OVER to the fast failover
    WORKER model (NARASI_BIBLE_FALLBACK_MODEL, default WORKER_MODEL = gemini-2.5-flash) that is far
    likelier to finish inside the window. A bible from a fast model >> no bible. Note this is MODEL
    failover; the aggregator/provider failover (KIE→LaoZhang→AtlasCloud, same model) already runs
    INSIDE each attempt via make_narasi_client. Set NARASI_BIBLE_FALLBACK_MODEL="" to disable the fallback.

    Robust like outline_from_topic: NEVER raises. Returns "" on any failure, so the caller
    simply proceeds without a bible (prior behavior).
    """
    if not (topic and outline):
        return ""
    _to = float(timeout if timeout is not None else os.environ.get("NARASI_BIBLE_TIMEOUT", "120"))
    system = _STORY_BIBLE_SYSTEM_FICTION if is_fiction else _STORY_BIBLE_SYSTEM_NONFICTION
    # SECONDARY WANT (9.0->9.x craft lever) — pin ONE want/friction per recurring NAMED secondary so
    # they read as people, not plot-functions. FICTION-only (never invent wants for real people) and
    # flag-gated NARASI_CRAFT_LEVERS (default OFF — SAME flag as PROSE VARIATION, so one flip turns
    # both #1+#2 ON): no-op on deploy, and a no-op on two-hander premises (no recurring secondaries to
    # deepen). Lives in the fact-sheet, which already bans prose/subplots, so the want stays a compact
    # pinned attribute — worst case = mild noise or ignored.
    if is_fiction and os.environ.get("NARASI_CRAFT_LEVERS", "0").strip().lower() in ("1", "true", "yes", "on"):
        system = system + (
            "\n\nADDENDUM to heading 1 (CHARACTERS) — SECONDARY DEPTH: for each NAMED secondary who "
            "RECURS (not a one-scene walk-on) — the friend, the parent, the colleague, the rival — pin "
            "ONE concrete WANT or moral friction of their OWN: something they are after, resist, fear, "
            "or are quietly wrong about, INDEPENDENT of serving the protagonist's plot. A secondary who "
            "exists only to hand the protagonist information reads as a function, not a person. One want "
            "per secondary, ONE line, drawn from who they already are — do NOT invent new characters, "
            "add subplots, or give a walk-on a backstory; this only DEEPENS secondaries the outline "
            "already requires. If the premise is a two-hander with no recurring secondary, add nothing.")
    prompt = _story_bible_prompt(topic, outline, language, is_fiction)
    _primary  = manager_model or MANAGER_MODEL
    _fallback = (os.environ.get("NARASI_BIBLE_FALLBACK_MODEL", WORKER_MODEL) or "").strip()
    _chain = [_primary] + ([_fallback] if (_fallback and _fallback != _primary) else [])
    for _i, _mdl in enumerate(_chain):
        worker = Worker(
            name="planner:bible", role="manager", model=_mdl,
            system=system, temperature=0.3, telemetry_sink=telemetry_sink,
        )
        res = await run_worker(worker, prompt, timeout=_to, task_id="planner:bible")
        if res.get("ok") and str(res.get("output") or "").strip():
            if _i > 0:
                log.info("build_story_bible: primary timed out — bible via fallback model %s", _mdl)
            return str(res["output"]).strip()
        log.info("build_story_bible: model %s returned no usable bible (attempt %d/%d)%s",
                 _mdl, _i + 1, len(_chain),
                 " — failing over" if _i + 1 < len(_chain) else " — proceeding without one")
    return ""


__all__ = [
    "plan_subtasks",
    "outline_from_topic",
    "build_story_bible",
    "_parse_json_loose",
    "_static_subtask_plan",
    "_static_outline",
]
