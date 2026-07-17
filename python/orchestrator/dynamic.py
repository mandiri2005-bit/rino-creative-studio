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
        name="planner:subtasks", role="manager", phase="plan_subtasks", model=m_model,
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
    # CLOSER-FLOOR (flag NARASI_CLOSER_FLOOR, default OFF): models routinely taper the CLOSING
    # chapter's word_target well below the requested per-chapter scalar; _normalize_outline honors it
    # (line ~320) → the word-gate floor (0.9×target, static.py) shrinks with it → a ~1/3-length closer
    # ships (Giza Ch8 529 vs ~1330 median; 4-script systemic). When ON, floor ONLY the LAST chapter's
    # target to the requested scalar so the gate lifts an under-written closer, leaving intentionally
    # short openers/mid-chapters untouched. OFF ⟹ byte-identical.
    if out and os.environ.get("NARASI_CLOSER_FLOOR", "0").strip().lower() in ("1", "true", "yes", "on"):
        try:
            out[-1]["word_target"] = max(int(out[-1].get("word_target") or words_per_chapter),
                                         int(words_per_chapter))
        except Exception:  # noqa: BLE001
            pass
    return out


def _normalize_reveals(parsed: Any) -> list[dict]:
    """Extract a well-formed "reveals" ledger from a parsed outline response (Flag
    NARASI_OUTLINE_REVEALS). Pure — no LLM, no I/O — and completely INDEPENDENT of
    _normalize_outline: this function reads the SAME `parsed` object but only ever looks
    at the separate "reveals" key, so it can NEVER affect the chapters extraction above,
    in either direction.

    Fail-safe by construction: returns [] on ANY malformation — parsed is not a dict,
    "reveals" is absent or not a list, an item is not a dict, an item is missing any of
    id/secret/chapter/characters_involved/method, "chapter" is not a plain int (bool
    excluded — bool is a Python int subclass but not a valid chapter number here), or
    "characters_involved" is not a list. This covers "flag off" too: when
    NARASI_OUTLINE_REVEALS is OFF, _outline_prompt never asks for a "reveals" key, so
    `parsed` (a bare list, or a dict with no "reveals" key) hits the `not isinstance(parsed,
    dict)` or `not isinstance(items, list)` guard below and returns [] — a no-op. NEVER
    raises: the whole body is wrapped so a single malformed item cannot take down the call.
    """
    try:
        if not isinstance(parsed, dict):
            return []
        items = parsed.get("reveals")
        if not isinstance(items, list):
            return []
        out: list[dict] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            if not all(k in it for k in ("id", "secret", "chapter", "characters_involved", "method")):
                continue
            rid, secret, chapter, chars, method = (
                it.get("id"), it.get("secret"), it.get("chapter"),
                it.get("characters_involved"), it.get("method"),
            )
            if not (isinstance(secret, str) and secret.strip()):
                continue
            if not isinstance(chapter, int) or isinstance(chapter, bool):
                continue
            if not isinstance(chars, list):
                continue
            if not (isinstance(method, str) and method.strip()):
                continue
            chars_clean = [str(c).strip() for c in chars if str(c or "").strip()]
            out.append({
                "id": str(rid).strip() if rid not in (None, "") else f"r{len(out) + 1}",
                "secret": secret.strip(),
                "chapter": chapter,
                "characters_involved": chars_clean,
                "method": method.strip(),
            })
        return out
    except Exception:  # noqa: BLE001 — reveals extraction must NEVER affect chapters
        return []


# ===========================================================================
# FLAG NARASI_OUTLINE_FIDELITY (default OFF, fiction only — same _ofic gating pattern as
# NARASI_OUTLINE_MANDATES / NARASI_OUTLINE_REVEALS above). Root cause (confirmed on a real
# manuscript, "The Trash Project" pitch): the outline prompt's "Make the STRUCTURE original"
# instruction — a deliberate, intentional copyright-mitigation guard against near-verbatim
# reproduction of a synopsis a user might paste from an existing copyrighted work — fires
# UNIFORMLY regardless of how much structure the user's own topic already supplies. On a
# topic with an extremely detailed chapter-by-chapter breakdown, that uniform "make it
# original" pressure caused the outline to INVERT a stated character's backstory (a victim
# who reported toxic waste leaks -> the generated version instead had him take a corporate
# bribe and stay silent, flipping the story's moral premise) and invent a major subplot
# (hidden-heir/DNA-paternity/corporate-succession) with zero basis anywhere in the input.
# This flag scales the guidance to the input's own detail level instead of applying the same
# "invent freely" pressure no matter how detailed the source already is.
# ===========================================================================
_TOPIC_DETAIL_HEADER_RE = re.compile(
    r"^[ \t]*(?:episode|chapter|bab)\s*\d+\s*[:.]",
    re.IGNORECASE | re.MULTILINE,
)


def _classify_topic_detail(topic: str) -> str:
    """Pure, deterministic, NO LLM — classifies how much structural detail the caller's
    topic input already supplies (Flag NARASI_OUTLINE_FIDELITY). Cheap: one regex pass plus
    a word count, no I/O, no network.

    Returns one of "minimal" / "moderate" / "detailed":

      "detailed"  — the topic contains 3 or more per-chapter/episode headers (a line
                    starting with "Episode"/"Chapter"/"Bab", case-insensitive, then a
                    number, then ':' or '.') where EACH qualifying header is followed by
                    a real synopsis — at least ~15 words of body text before the next
                    header or the end of the string. A bare list of episode TITLES with
                    no synopsis under any of them does not qualify as any one header's
                    body is too short, so it never crosses the well-populated count.
                    Structure matters more than raw length here: this can fire even on a
                    short-ish topic, as long as it is genuinely broken down per chapter.
      "moderate"  — no qualifying per-chapter breakdown, but the topic is long (over
                    roughly 250 words) — e.g. a logline plus character descriptions with
                    no chapter-by-chapter map.
      "minimal"   — short title or logline only (the common case today).

    Never raises: any failure (unexpected input shape, regex edge case) falls back to
    "minimal" — the SAME "invent freely" instruction the flag would otherwise leave
    dominant by default, so a classification failure degrades to today's behavior, never
    to an over-constrained one.
    """
    try:
        text = topic or ""
        word_count = len(text.split())

        matches = list(_TOPIC_DETAIL_HEADER_RE.finditer(text))
        well_populated = 0
        for i, m in enumerate(matches):
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            if len(text[start:end].split()) >= 15:
                well_populated += 1

        if well_populated >= 3:
            return "detailed"
        if word_count > 250:
            return "moderate"
        return "minimal"
    except Exception:  # noqa: BLE001 — classification is an enhancement, never blocks the outline
        return "minimal"


# The three instruction blocks Flag NARASI_OUTLINE_FIDELITY selects between. Each is a single
# sentence-fragment meant to be appended inline next to the existing anti-overlap instruction
# in _outline_prompt (leading space, no trailing space) — NOT a restructuring of the prompt.
_FIDELITY_MINIMAL = (
    " The topic above is a short title or logline with no per-chapter breakdown — invent "
    "plot, character, and subplot freely wherever the topic is silent. The one exception: if "
    "the topic states a specific fact about a character's backstory, motivation, or moral "
    "standing (their name, role, or a stated action — e.g. \"fired for reporting a "
    "violation\"), that fact stays fixed — do not invert it, and do not bolt on an unstated "
    "trait that contradicts it in spirit (e.g. giving that same character a separate hidden "
    "guilt or silence of his own). The same rule covers institutional figures the topic "
    "mentions (a hospital, a company, an official) — do not invent a cover-up or concealment "
    "motive for them either."
)

_FIDELITY_MODERATE = (
    " The topic above states some character traits, premise facts, and named entities — "
    "preserve every one of those exactly as given, including backstory, motivation, and "
    "moral standing, while plot and chapter structure may be freely invented to fill in "
    "whatever the topic does not specify. Preservation covers the spirit as well as the "
    "letter: do not bolt on an unstated guilt, hidden motive, or concealment beat that "
    "undercuts a character's established throughline without literally contradicting a "
    "stated line. The same rule covers institutional figures the topic mentions (a hospital, "
    "a company, an official) — do not invent a cover-up or concealment motive for them when "
    "the topic frames their action as an honest mistake or leaves it unstated."
)

_FIDELITY_DETAILED = (
    " The topic above already provides a detailed chapter-by-chapter or episode-by-episode "
    "breakdown. Your job is to ADAPT this into the requested chapter structure, preserving "
    "every stated plot beat, character arc, character backstory and motivation, and "
    "specified structural choice (for example a public versus private climax, or a "
    "character's stated moral status) exactly as given. You may combine, resequence, or add "
    "connective, sensory, or dialogue detail between stated beats for pacing. You must NOT "
    "invent new major subplots that have no basis anywhere in the input, and you must NOT "
    "alter a named character's stated backstory, motivation, or moral standing (for example, "
    "if the input states a character was fired for a specific reason, do not change this to "
    "the character having been complicit, bribed, or otherwise morally different) unless the "
    "input itself is genuinely ambiguous or silent on that specific point. This also applies "
    "to unnamed or minor institutional figures (a hospital, a company, an official) the topic "
    "mentions — do not assign them a hidden or deliberate motive (a cover-up, bad faith, "
    "concealment) the input does not state, even if doing so would seem dramatically "
    "convenient."
)


_OUTLINE_SYSTEM = (
    "You are an editor who designs the chapter structure of a narrative book or "
    "long-form script from a single topic. Return ONLY JSON."
)


def _outline_prompt(topic: str, n: int, style: Optional[str], language: str,
                    words_per_chapter: int, topic_detail: Optional[str] = None) -> str:
    style_clause = f" in a {style} style" if style else ""
    # ROUND-5 STRUCTURAL MANDATES (NARASI_OUTLINE_MANDATES, default OFF, fiction only).
    # Seven rolls x four lenses converged on ONE diagnosis: the remaining defects are
    # ABSENCES (a delivery scene never allocated, a reconciliation bridge missing, a
    # secondary with no arc, evidence that costs nothing) — and scenes are BORN here,
    # in the outline. Every downstream gate (bible pins, counters, critic, revise)
    # can only protect what the outline allocated; the revise pass is explicitly
    # forbidden from inserting scenes. OFF ⟹ prompt byte-identical.
    _mand = ""
    try:
        from .static import _is_fiction_style as _ofis
        _ofic = bool(_ofis(style)) if style else False
    except Exception:  # noqa: BLE001 — mandates are an enhancement
        _ofic = False
    if _ofic and os.environ.get("NARASI_OUTLINE_MANDATES", "0").strip().lower() in ("1", "true", "yes", "on"):
        _mand = (
            "\n\nSTRUCTURAL MANDATES (fiction):\n"
            "1. KEYSTONE SLOTS — every decision, confession, evidence-DISCOVERY, or handover the "
            "plot depends on gets its OWN scene, named as a plain prose sentence inside a chapter "
            "summary (e.g. 'the postmaster delivers the held letters and the brother reads the "
            "first one on-page' — NOT a labelled 'Scene N —' or 'ChN:' line). A keystone implied "
            "between chapters is a structural defect.\n"
            "2. PAYOFF OWNERS — any hook a chapter opens (a delayed envelope, a deadline, a "
            "threat) names the LATER chapter that pays it, written into that chapter's summary.\n"
            "3. ONE SECONDARY ARC — one non-lead character gets a stated want and an on-page "
            "turn spanning at least two chapters.\n"
            "4. BRIDGE BEAT — if the leads rupture, allocate the beat that turns the wounded "
            "party back (what they learn or receive that changes their mind) BEFORE any reunion "
            "scene; a reunion without its bridge reads unearned.\n"
            "5. EVIDENCE COSTS — each major discovery costs the finder something on-page (time, "
            "risk, a relationship, an admission) — never a convenient box that happens to hold "
            "everything.\n"
            "6. PRESENT-TENSE STAKES — if the story raises a LIVE physical danger in its "
            "present (an incoming storm, a flood risk), the lead ACTS on it on-page (a warning "
            "issued, an evacuation started) — the protagonist may not ignore the same class of "
            "danger the backstory punished someone for ignoring.\n"
            "Write every summary as ordinary prose sentences (what happens), never as a "
            "numbered/labelled scene list — the summary is a planning aid, not text to be "
            "copied.\n\n")
        # r5.2 TITLE BANS: chapter titles are born HERE, before the bible, so the lane
        # ledger's title findings never reached them ('The Weight of Clear Skies'
        # VERBATIM across two rolls; 'weight' in titles 7/9). Data-driven, fail-open.
        try:
            import json as _tj
            _tpath = os.path.join(os.path.dirname(__file__), "..", "pakem", "lane_ledger.json")
            with open(_tpath, encoding="utf-8") as _tf:
                _tlane = (_tj.load(_tf) or {}).get((style or "").strip()) or {}
            _ttok = [str(x) for x in (_tlane.get("banned_title_tokens") or [])][:14]
            if _ttok:
                _mand += ("CHAPTER TITLES: previous books in this lane overused these title "
                          "words/frames — do NOT use any of them in any chapter title: "
                          + ", ".join(_ttok) + ". Invent fresh title shapes.\n\n")
        except Exception:  # noqa: BLE001 — titles enhancement never blocks the outline
            pass
    # REVEAL LEDGER (flag NARASI_OUTLINE_REVEALS, default OFF, fiction only — same gating
    # convention as NARASI_OUTLINE_MANDATES above). Root cause (confirmed on a real
    # manuscript): the single-shot outline call independently invents the SAME secret as a
    # "fresh" reveal in two different chapters (Ch3 and Ch5 both staging one confession as
    # new) because nothing forces the model to commit each secret to exactly one chapter, and
    # nothing records the reveals in a form two DIFFERENT phrasings of the same fact can later
    # be compared against. Ask for a parallel "reveals" ledger, in neutral OMNISCIENT phrasing
    # (not each character's own in-scene wording) so a downstream dedup check (Flag
    # NARASI_REVEAL_DEDUP_CHECK) can compare reveals across chapters for paraphrase-blindness.
    # OFF ⟹ prompt/schema byte-identical (bare chapters array, unchanged).
    _reveals_clause = ""
    _reveals_schema = ""
    if _ofic and os.environ.get("NARASI_OUTLINE_REVEALS", "0").strip().lower() in ("1", "true", "yes", "on"):
        _reveals_clause = (
            "\n\nREVEAL LEDGER: separately from the chapter list, enumerate every "
            "PLOT-CRITICAL secret, confession, or reveal in this story — anything a "
            "character conceals that the plot later discloses. Commit EACH one to exactly "
            "ONE chapter as its first-reveal — the chapter where it is FIRST disclosed on "
            "the page — and never let the same underlying secret be staged as a fresh "
            "revelation in more than one chapter. Describe each secret in NEUTRAL, "
            "OMNISCIENT-NARRATOR phrasing (the objective underlying fact, e.g. 'the will "
            "names the daughter as sole heir'), NOT in any character's own in-scene wording "
            "or euphemism — this is what lets two different phrasings of the SAME fact be "
            "recognized later as one reveal, not two.\n"
        )
        _reveals_schema = (
            ', "reveals": [{"id": "r1", "secret": "neutral omniscient-POV description of the '
            'underlying fact", "chapter": 1, "characters_involved": ["name1", "name2"], '
            '"method": "how it is revealed"}]'
        )
    if _reveals_schema:
        _return_block = (
            "Return ONLY a JSON object, no prose, no code fences, in EXACTLY this shape:\n"
            '{"chapters": [{"title": "chapter title", "summary": "1-2 sentences on what it '
            f'covers", "words": {words_per_chapter}}}]'
            + _reveals_schema + "}"
        )
    else:
        _return_block = (
            "Return ONLY a JSON array, no prose, no code fences, in EXACTLY this shape:\n"
            '[{"title": "chapter title", "summary": "1-2 sentences on what it covers", '
            f'"words": {words_per_chapter}}}]'
        )
    # FIDELITY CLAUSE (flag NARASI_OUTLINE_FIDELITY, default OFF, fiction only — same _ofic
    # gate as _mand/_reveals_clause above). Independent of both: reads only `topic_detail`
    # (precomputed once by the caller via _classify_topic_detail, see outline_from_topic) or,
    # if not supplied, classifies inline as a self-contained fallback. Selects exactly ONE of
    # the three instruction blocks and appends it inline next to the existing anti-overlap
    # sentence below — it does not touch _mand, _reveals_clause, or _return_block in any way.
    # OFF ⟹ _fidelity_clause == "" ⟹ prompt byte-identical (including byte-identical to Flags
    # 1/2's own additions — this clause is spliced in below, not entangled with theirs).
    _fidelity_clause = ""
    # ANTI-TROPE / FIDELITY CONFLICT (same flag+gate as _fidelity_clause above): the
    # unconditional "avoid the buried-document/hidden-crime shape" instruction below is a
    # deliberate copyright-mitigation guard, but at topic_detail=="detailed" it directly
    # fights _FIDELITY_DETAILED's "preserve every stated plot beat... do NOT invent new
    # major subplots" instruction — confirmed on a real manuscript ("The Trash Project")
    # where this exact tension inverted a stated character's backstory. At that tier,
    # fidelity to the user's own given structure should dominate — the same principle the
    # flag's own minimal tier already applies at the OPPOSITE extreme (full creative
    # invention licensed there). So the anti-trope sentence is suppressed ONLY when the
    # flag is on AND topic_detail=="detailed"; every other combination (flag off, or flag
    # on at "moderate"/"minimal") keeps it, byte-identical to before this fix.
    _suppress_antitrope = False
    if _ofic and os.environ.get("NARASI_OUTLINE_FIDELITY", "0").strip().lower() in ("1", "true", "yes", "on"):
        _detail = topic_detail if topic_detail in ("minimal", "moderate", "detailed") else _classify_topic_detail(topic)
        if _detail == "detailed":
            _fidelity_clause = _FIDELITY_DETAILED
            _suppress_antitrope = True
        elif _detail == "moderate":
            _fidelity_clause = _FIDELITY_MODERATE
        else:
            _fidelity_clause = _FIDELITY_MINIMAL
    _antitrope_clause = "" if _suppress_antitrope else (
        " Make the STRUCTURE original: build it from THIS topic's specific "
        "hook, NOT as a re-skin of a famous novel/film with the details swapped, and NOT as the "
        "over-used \"a buried document/record exposes a hidden past crime\" shape — if it drifts "
        "there, choose a different engine."
    )
    return (
        f"TOPIC:\n{topic}\n\n"
        f"Design a {n}-chapter outline{style_clause}. The chapters must progress "
        "logically (a hook that opens, development in the middle, a resonant close) "
        "and must NOT overlap — each chapter owns a distinct part of the story so "
        "parallel writers won't repeat each other."
        + _fidelity_clause +
        " Order the chapters in CHRONOLOGICAL "
        "sequence — no chapter set earlier in time than the one before it (no rewinding to "
        "an earlier event as a whole chapter). And make sure every EXTERNAL stake the story "
        "opens — a deadline, a debt, a threat, a search — is RESOLVED by the final chapters, "
        "never left dangling."
        + _antitrope_clause +
        " And IF the premise poses a DISTINCTIVE hook — a "
        "recurring image, an anomaly, a specific mystery the opening raises and frames as a "
        "question to answer — make sure a specific late chapter DELIVERS its answer on the page, "
        "rather than letting a generic sub-plot (a fraud, a buried record) resolve while the hook "
        "hangs. A premise with no such posed puzzle owes no answer-chapter — do not invent one. "
        f"Titles and summaries in {language}.\n\n"
        + _mand + _reveals_clause + _return_block
    )


def _title_ban_hits(chapters: list, style) -> list:
    """ROUND-6 (#6): banned_title_tokens reached the OUTLINE PROMPT in r5.2 and the
    very next roll used 'The Weight of a Withdrawn Warning' anyway — Law 1 again:
    an instruction without a checker is a suggestion. Deterministic check of
    outline titles against the lane's banned tokens; empty for non-lane styles.
    Never raises."""
    try:
        import json as _tj
        import os as _tos
        from pakem import resolve_style_key as _trsk
        _tkey = _trsk(style) if style else None
        if not _tkey:
            return []
        _tpath = _tos.path.join(_tos.path.dirname(__file__), "..", "pakem", "lane_ledger.json")
        with open(_tpath, encoding="utf-8") as _tf:
            _tlane = (_tj.load(_tf) or {}).get(_tkey) or {}
        _ttok = [str(x).lower() for x in (_tlane.get("banned_title_tokens") or [])][:14]
        if not _ttok:
            return []
        hits = []
        for ch in chapters or []:
            _tt = str((ch or {}).get("title") or "").lower()
            for tok in _ttok:
                if tok and tok in _tt:
                    hits.append(f"'{(ch or {}).get('title')}' contains banned token '{tok}'")
                    break
        return hits
    except Exception:  # noqa: BLE001
        return []


# ===========================================================================
# FLAG NARASI_REVEAL_DEDUP_CHECK (default OFF; only acts when Flag NARASI_OUTLINE_REVEALS
# is ALSO on and produced 2+ reveals). Catches the paraphrase-blind half of the same
# duplicate-reveal bug the ledger records: two reveals in DIFFERENT chapters that share a
# character and, on closer (LLM) inspection, turn out to describe the SAME underlying
# secret despite different wording (a hidden file in one reveal vs a corporate payoff in
# another, both actually the same confession).
# ===========================================================================
_NAME_NORM_RE = re.compile(r"[\s\-_]+")


def _norm_char_name(name: str) -> str:
    """Pure, deterministic — casefold + collapse whitespace/'-'/'_' so trivial LLM
    formatting variance between two reveal entries for the SAME character (e.g.
    "Do-hyun" vs "Do Hyun", or "  Mira " vs "mira") doesn't defeat the exact-match set
    intersection in _candidate_reveal_pairs below. Deliberately NOT fuzzy/Levenshtein —
    that is a different, larger problem; this only closes the casing/spacing gap. Never
    raises: any failure falls back to the original string (worst case, back to today's
    exact-match behavior for that one name)."""
    try:
        return _NAME_NORM_RE.sub(" ", str(name or "")).strip().casefold()
    except Exception:  # noqa: BLE001
        return str(name or "")


def _candidate_reveal_pairs(reveals: list) -> list[tuple[dict, dict]]:
    """Pure, deterministic, NO LLM — the cost-saving pre-filter for the dedup check.

    Returns only pairs of reveals that (a) sit in DIFFERENT chapters, and (b) share at
    least one name in characters_involved (compared via _norm_char_name so casing/
    spacing variance doesn't silently defeat the match) — a same-chapter pair can't be a
    cross-chapter duplicate reveal, and two reveals about disjoint characters are
    exceedingly unlikely to be the same secret. Fewer than 2 reveals, or no qualifying
    pair, -> []. This is the COMMON case: most outlines have no overlapping-cast
    cross-chapter reveals, so this filter alone keeps the LLM call in Flag 2 step 2 from
    ever firing. Never raises."""
    try:
        if not isinstance(reveals, list) or len(reveals) < 2:
            return []
        pairs: list[tuple[dict, dict]] = []
        for i in range(len(reveals)):
            a = reveals[i]
            if not isinstance(a, dict):
                continue
            a_chars = {_norm_char_name(x) for x in (a.get("characters_involved") or [])}
            for j in range(i + 1, len(reveals)):
                b = reveals[j]
                if not isinstance(b, dict):
                    continue
                if a.get("chapter") == b.get("chapter"):
                    continue
                b_chars = {_norm_char_name(x) for x in (b.get("characters_involved") or [])}
                if a_chars & b_chars:
                    pairs.append((a, b))
        return pairs
    except Exception:  # noqa: BLE001
        return []


_REVEAL_DEDUP_SYS = (
    "You are checking a fiction outline's REVEAL LEDGER for accidental duplicates: two "
    "reveals that were phrased differently but actually stage the SAME underlying secret "
    "as a fresh revelation TWICE, once in each of two different chapters (e.g. 'a hidden "
    "file surfaces' in one chapter and 'a corporate payoff comes to light' in another, "
    "when both are really the same confession described two ways). Some pairs below are "
    "innocuous — genuinely different secrets that merely happen to share a character. "
    "Judge ONLY whether the SECRET ITSELF is the same underlying fact, never by shared "
    "characters or surface wording alone. Do NOT flag a pair where the earlier chapter "
    "only hints, suspects, or partially reveals the fact and the later chapter is the "
    "first FULL, CONFIRMED disclosure — that is intentional dramatic structure (a "
    "foreshadow-then-confirm), not a duplicate. Only flag when BOTH chapters "
    "independently stage a full, confirmed reveal of the same fact as if it were new "
    "information. Return ONLY JSON: {\"duplicates\": [{\"pair\": "
    "<1-based index into the numbered list below>, \"keep_chapter\": <int, the earlier/"
    "canonical chapter that keeps the reveal>, \"demote_chapter\": <int, the later chapter "
    "that must stop re-staging it as new>}]}. If no pair is a true duplicate, return "
    "{\"duplicates\": []}."
)

_REVEAL_AMEND_SYS = (
    "You are editing ONE chapter summary in a fiction outline. This chapter's summary "
    "currently re-stages a secret as a FRESH reveal, but that secret was already revealed "
    "earlier in the story, in an earlier chapter. Rewrite the summary so this chapter "
    "treats the fact as ALREADY KNOWN by this point — it may reference the fact or its "
    "consequences, but must NOT re-stage the disclosure itself as new information to the "
    "reader or characters. Keep every other plot beat in the summary; change only how this "
    "one fact is handled. Do not delete, shorten, or invent unrelated content, and do not "
    "mention that this is an edit. Return ONLY JSON: {\"summary\": \"<the full rewritten "
    "chapter summary>\"}"
)


async def _reveal_dedup_amend(chapters: list, reveals: list, *, tenant_id=None, job_uuid=None,
                              telemetry_sink=None) -> list:
    """FLAG NARASI_REVEAL_DEDUP_CHECK step 2-3: given the chapters list and the Flag-1
    reveals ledger, find and fix genuine cross-chapter duplicate reveals.

    Step 1 (pre-filter): _candidate_reveal_pairs (pure, no LLM). If it returns no pairs —
    the common case — this returns `chapters` UNCHANGED with ZERO LLM calls.

    Step 2 (check): if there ARE candidate pairs, ONE holistic cheap LLM call
    (_narasi_cheap_call) presents ALL candidate pairs together and asks which (if any) are
    true duplicates despite different wording.

    Step 3 (amend): for each CONFIRMED duplicate, ONE bounded keep/demote amend call
    (mirrors static.py's extract-and-check-then-amend shape, e.g. the antag-scene /
    warmth-scene / cast-depth checks) rewrites ONLY the demoted/later chapter's summary to
    treat the fact as already known — never deletes a chapter, never re-rolls the outline,
    never auto-merges beyond that one summary edit. Bounded to at most 5 amends per call.

    BILLING (telemetry_sink, default None — same param outline_from_topic's own manager
    call already receives via its Worker/run_worker): outline_from_topic's Worker call
    reaches the job's real settlement total AUTOMATICALLY, by invoking telemetry_sink as
    a Callable[[CallTelemetry], None] (see core.py's run_worker -> _emit). The two
    _narasi_cheap_call invocations below are NOT routed through run_worker/_emit, so they
    never reach that path on their own — that gap is the fix here. When the caller's
    telemetry_sink happens to expose a duck-typed `.credits` running-total attribute (the
    same "job-level running total" convention laozhang_api._log_narasi_usage's own
    docstring documents, and the exact convention narration_api.py's _UsageSink /
    charge.settle(credits_actual=sink.credits) already settles from — see narration_api.py
    GATES-phase sites, e.g. "if sink is not None and _dpcc: sink.credits += int(_dpcc)"),
    fold each call's cost into it directly and drop that call's own usage_logs credits to 0
    (credit_row=False) so the cost is counted exactly once, at settlement, not twice.
    telemetry_sink IS a real, credits-bearing accumulator in the one production call path
    this function is reachable from (narration_api.py's _ChapterCheckboxSink wraps the real
    _UsageSink and proxies `.credits` straight through to it, fixed 2026-07-17 — previously
    it was a bare closure with no `.credits`, which made this fold a permanent no-op). When
    telemetry_sink is None or genuinely lacks `.credits` (BYOK, metering-disabled, or a
    future caller with no accounting), this still degrades gracefully: credit_row stays
    True as a fallback, keeping the cost visible in usage_logs exactly as before rather
    than silently disappearing. Deciding credit_row once, before either call, avoids a call
    landing with credit_row=False on the hope of an accumulation that then turns out to be
    impossible.

    Returns a NEW list (chapters is never mutated in place). Never raises: any failure at
    any step returns `chapters` unchanged."""
    try:
        pairs = _candidate_reveal_pairs(reveals)
        if not pairs:
            return chapters
        from laozhang_api import _narasi_cheap_call as _rd_call, _narasi_parse_json as _rd_parse

        # Decided ONCE, before any call: can this telemetry_sink actually absorb a direct
        # credit accumulation? See the docstring BILLING section above.
        _sink_can_absorb = telemetry_sink is not None and hasattr(telemetry_sink, "credits")
        _credit_row = not _sink_can_absorb

        def _fold_credits(cr) -> None:
            if not (_sink_can_absorb and cr):
                return
            try:
                telemetry_sink.credits += cr
            except Exception as _sce:  # noqa: BLE001 — sink accumulation must never break the dedup check
                log.warning("reveal-dedup: telemetry sink credit accumulation failed (non-fatal): %s", _sce)

        def _fmt(r: dict) -> str:
            return (f"ch{r.get('chapter')}: \"{r.get('secret')}\" "
                    f"(characters: {', '.join(r.get('characters_involved') or [])}; "
                    f"method: {r.get('method')})")

        _user = "\n\n".join(
            f"PAIR {i + 1}:\n  A ({_fmt(a)})\n  B ({_fmt(b)})"
            for i, (a, b) in enumerate(pairs)
        )
        _raw, _cr = await _rd_call(
            _REVEAL_DEDUP_SYS, _user, tenant_id=tenant_id, user_id=None,
            job_uuid=job_uuid, json_mode=True, credit_row=_credit_row,
        )
        _fold_credits(_cr)
        _parsed = _rd_parse(_raw) if isinstance(_raw, str) else (_raw or {})
        dupes = (_parsed or {}).get("duplicates") if isinstance(_parsed, dict) else None
        if not isinstance(dupes, list) or not dupes:
            log.info("reveal-dedup check: %d candidate pair(s), 0 confirmed duplicate(s)", len(pairs))
            return chapters

        out = [dict(c) if isinstance(c, dict) else c for c in chapters]
        out_by_id = {c.get("id"): idx for idx, c in enumerate(out) if isinstance(c, dict)}
        amended = 0
        for d in dupes[:5]:
            if not isinstance(d, dict):
                continue
            try:
                pair_idx = int(d.get("pair") or 0)
                demote_ch = int(d.get("demote_chapter"))
                keep_ch = int(d.get("keep_chapter"))
            except (TypeError, ValueError):
                continue
            if not (1 <= pair_idx <= len(pairs)) or demote_ch == keep_ch:
                continue
            a, b = pairs[pair_idx - 1]
            valid_chapters = {a.get("chapter"), b.get("chapter")}
            if demote_ch not in valid_chapters or keep_ch not in valid_chapters:
                continue
            # A correct duplicate-reveal fix always demotes the LATER occurrence (the
            # earlier one is the legitimate first-disclosure) — never the earlier one. A
            # judge response that picks the earlier chapter as demote_chapter is an
            # invalid/unusable verdict for this pair: skip it rather than apply a
            # backwards edit.
            if demote_ch <= keep_ch:
                log.warning(
                    "reveal-dedup: pair %d judge picked demote_chapter=%d <= keep_chapter=%d "
                    "(must demote the LATER chapter) — skipping amend for this pair",
                    pair_idx, demote_ch, keep_ch)
                continue
            idx = out_by_id.get(demote_ch)
            if idx is None or not isinstance(out[idx], dict) or not out[idx].get("summary"):
                continue
            secret = a.get("secret") if a.get("chapter") == keep_ch else b.get("secret")
            _amend_user = (
                f"THE FACT (already known as of chapter {keep_ch}): {secret}\n\n"
                f"CHAPTER {demote_ch} CURRENT SUMMARY:\n{out[idx].get('summary')}"
            )
            _araw, _acr = await _rd_call(
                _REVEAL_AMEND_SYS, _amend_user, tenant_id=tenant_id, user_id=None,
                job_uuid=job_uuid, json_mode=True, credit_row=_credit_row,
            )
            _fold_credits(_acr)
            _ad = _rd_parse(_araw) if isinstance(_araw, str) else (_araw or {})
            new_summary = str((_ad or {}).get("summary") or "").strip()
            if len(new_summary) > 40:
                out[idx] = {**out[idx], "summary": new_summary}
                amended += 1
                log.info("reveal-dedup: pair %d confirmed duplicate — ch%d demoted (kept ch%d)",
                         pair_idx, demote_ch, keep_ch)
            else:
                log.info("reveal-dedup: amend unusable for ch%d — chapter summary kept", demote_ch)
        log.info("reveal-dedup check: %d candidate pair(s), %d confirmed, %d amended",
                 len(pairs), len(dupes), amended)
        return out
    except Exception as _rde:  # noqa: BLE001 — dedup check must never block the outline
        log.warning("reveal-dedup check failed (non-fatal): %s", _rde)
        return chapters


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
    tenant_id: Optional[str] = None,
    job_uuid: Optional[str] = None,
) -> dict[str, Any]:
    """Ask the manager for `n_chapters` chapter {title, summary, words} entries.

    Robust like plan_subtasks: on any failure FALLS BACK to a deterministic
    generic outline so a bare topic always yields a runnable chapter list.

    tenant_id/job_uuid (both optional, default None): forwarded ONLY to the Flag-2
    NARASI_REVEAL_DEDUP_CHECK cheap-call credit logging below — this function's LLM outline
    call itself does not need them. Threaded through so the dedup check's usage_logs rows
    attribute to the right tenant/job; None is safe (the cheap call still runs, just with no
    tenant/job attribution) and preserves the flags-off, byte-identical-caller contract.

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

    # Fiction gate shared by ALL THREE flags below — same convention as NARASI_OUTLINE_MANDATES
    # / _outline_prompt's own _ofic check. Soft: any import/lookup failure -> False (flags OFF).
    try:
        from .static import _is_fiction_style as _otfis
        _is_fic = bool(_otfis(style)) if style else False
    except Exception:  # noqa: BLE001 — gating is an enhancement, never blocks the outline
        _is_fic = False

    # FLAG NARASI_OUTLINE_FIDELITY: classify the topic's own structural detail level ONCE per
    # outline request (pure/cheap — no LLM), so BOTH the initial _outline_prompt call below AND
    # the title-ban reroll's call further down (same topic, unchanged) select the SAME
    # instruction block instead of reclassifying redundantly. Computed unconditionally (it's
    # cheap and side-effect-free); _outline_prompt only USES it when the flag+fiction gate
    # passes, so this is a no-op when the flag is off.
    _topic_detail = _classify_topic_detail(topic)

    worker = Worker(
        name="planner:outline", role="manager", phase="outline", model=m_model,
        system=_OUTLINE_SYSTEM, temperature=0.5, telemetry_sink=telemetry_sink,
    )
    res = await run_worker(
        worker, _outline_prompt(topic, n, style, language, words_per_chapter, topic_detail=_topic_detail),
        timeout=timeout, task_id="planner:outline",
    )

    chapters: list[dict] = []
    # FLAG NARASI_OUTLINE_REVEALS reveals ledger (see _normalize_reveals docstring for the
    # fail-safe contract): extracted from the SAME `parsed` object as chapters, via a fully
    # separate key/function, so a malformed or absent reveals block can NEVER affect
    # `chapters` above — the two extractions cannot interact.
    reveals: list[dict] = []
    if res.get("ok") and res.get("output"):
        parsed = _parse_json_loose(res["output"])
        chapters = _normalize_outline(parsed, topic, n, words_per_chapter)
        reveals = _normalize_reveals(parsed)

    # ROUND-6 TITLE-BAN ENFORCE (NARASI_TITLE_BAN_ENFORCE, default OFF): verify the
    # outline's titles against the lane's banned tokens; on a hit, ONE re-roll with
    # the rejection quoted back, accepted only if it strictly reduces hits. The r5.2
    # prompt-side ban alone was violated on its first live roll ('Weight' 8/10 files).
    if (chapters and os.environ.get("NARASI_TITLE_BAN_ENFORCE", "0").strip().lower() in ("1", "true", "yes", "on")):
        try:
            _tbh = _title_ban_hits(chapters, style)
            if _tbh:
                log.info("title-ban enforce: %d banned title(s) in outline (%s) — one re-roll",
                         len(_tbh), _tbh[:2])
                _tbres = await run_worker(
                    worker,
                    _outline_prompt(topic, n, style, language, words_per_chapter, topic_detail=_topic_detail)
                    + ("\n\nPREVIOUS ATTEMPT REJECTED — these chapter titles used banned title "
                       "words: " + "; ".join(_tbh[:4]) + ". Regenerate the SAME outline structure "
                       "with completely different, fresh title shapes that avoid every banned "
                       "title word listed above."),
                    timeout=timeout, task_id="planner:outline-titlefix",
                )
                if _tbres.get("ok") and _tbres.get("output"):
                    _tbp = _parse_json_loose(_tbres["output"])
                    _tbch = _normalize_outline(_tbp, topic, n, words_per_chapter)
                    if _tbch and len(_title_ban_hits(_tbch, style)) < len(_tbh):
                        chapters = _tbch
                        # Re-derive reveals from THIS SAME re-roll response — not left stale
                        # against the discarded chapters, not left pointing at chapter shapes
                        # (titles/order) that just changed. _normalize_reveals is malformation-
                        # safe on its own, so this is a plain re-extraction, never a hazard.
                        reveals = _normalize_reveals(_tbp)
                        log.info("title-ban enforce: re-roll accepted (%d → %d banned title(s))",
                                 len(_tbh), len(_title_ban_hits(_tbch, style)))
                    else:
                        log.info("title-ban enforce: re-roll no better — original outline kept")
                        # reveals intentionally left untouched: it already matches the ORIGINAL
                        # (kept) chapters, since the re-roll was rejected.
        except Exception as _tbe:  # noqa: BLE001
            log.warning("title-ban enforce failed (non-fatal): %s", _tbe)

    # FLAG NARASI_REVEAL_DEDUP_CHECK (default OFF; only does anything when Flag 1 is ALSO on
    # and produced 2+ reveals — both conditions checked here AND inside _reveal_dedup_amend's
    # pre-filter). Runs BEFORE build_story_bible is ever invoked downstream: the router
    # (_run_topic_to_book) calls outline_from_topic, THEN passes the returned chapters into
    # narrate_chapters -> build_story_bible, so a demoted duplicate is fixed in the outline
    # before the bible (and every parallel chapter writer) ever sees it.
    if (_is_fic and len(reveals) >= 2
            and os.environ.get("NARASI_REVEAL_DEDUP_CHECK", "0").strip().lower() in ("1", "true", "yes", "on")):
        chapters = await _reveal_dedup_amend(
            chapters, reveals, tenant_id=tenant_id, job_uuid=job_uuid,
            telemetry_sink=telemetry_sink,
        )

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
    # CONTINUITY PINS (NARASI_CONTINUITY_PINS, default OFF): the SYSTEM addendum adds headings 9-13;
    # mirror them here because this prompt says "Output ONLY the numbered sheet under the headings
    # (...)" — an unlisted heading risks being suppressed. OFF ⟹ prompt byte-identical.
    if is_fiction and os.environ.get("NARASI_CONTINUITY_PINS", "0").strip().lower() in ("1", "true", "yes", "on"):
        heads += (", CAUSALITY POLICY, CALENDAR SPINE, SIGNATURE-PROP CUSTODY, NAMING CONVENTIONS, "
                  "ENTITY INTRODUCTIONS & FINALE CAST, PROPERTY & LEVERAGE, GEOGRAPHY POLICY, "
                  "AFTERMATH COMMIT, COUNTERPOINT NUMBERS, EVIDENCE CHAIN & CUSTODY, CONTIGUOUS SCENES, "
                  "EVIDENCE MAP")
    # CANON REGISTRY suppression fix (round-4): the SYSTEM addendum asks for a fenced
    # canon_registry JSON block, but THIS prompt says "Output ONLY the numbered sheet …
    # One item per line. No preamble, no prose" — the JSON block is neither a numbered
    # heading nor one-item-per-line, so the model obeyed the stricter instruction and
    # dropped it (rolls 5-6: canon-diff skipped, roll 6 head-dump: "no registry-shaped
    # block found"). Mirror the block here as an explicit exception, same flag.
    _reg_tail = ""
    if is_fiction and os.environ.get("NARASI_CANON_REGISTRY", "0").strip().lower() in ("1", "true", "yes", "on"):
        _reg_tail = (" EXCEPTION: after the last numbered heading, ALSO output the fenced "
                     "```json canon_registry block specified above — it is part of the "
                     "required output, not prose.")
    return (
        f"TOPIC / PREMISE:\n{topic}\n\n"
        f"CHAPTER OUTLINE ({len(outline or [])} chapters):\n{ol}\n\n"
        f"Write the {label} in {language}. Output ONLY the numbered sheet under the headings "
        f"({heads}). One item per line. No preamble, no prose, no chapter text." + _reg_tail
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
    extra_negative: Optional[str] = None,
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
    # ROUND-14 BIBLE SCALE (NARASI_BIBLE_SCALE, default OFF): the fact-sheet was a fixed
    # ~10k chars whether it drove 10 chapters or 16 — roll-16 (40k/16) shared a 10-chapter-
    # sized bible across 16 chapters, so the extra chapters invented their own specifics
    # (cast renamed, floor/date forks) and the critic scored 4.0. Tell the bible the chapter
    # count and require it to pin a NAMED, LOCKED set proportional to the book length so the
    # later chapters inherit facts instead of improvising. FICTION-only; append-only.
    if is_fiction and os.environ.get("NARASI_BIBLE_SCALE", "0").strip().lower() in ("1", "true", "yes", "on"):
        _bs_n = max(1, len(outline or []))
        system = system + (
            f"\n\nADDENDUM — SCALE TO {_bs_n} CHAPTERS: this fact-sheet must anchor a "
            f"{_bs_n}-chapter book. Pin EVERY load-bearing specific ONCE, here, so no later "
            "chapter has to invent one: the FULL NAMED CAST (every character the plot names — "
            "leads, family, the antagonist/officer, each witness/victim named on the page), each "
            "with a fixed full name and role; every KEY DATE, QUANTITY and PLACE; and the exact "
            "value of any fact stated more than once. A longer book needs MORE pinned names and "
            "facts, not a longer prose sheet — keep it a terse locked list. Any name or number a "
            "chapter needs that is NOT pinned here is a fork waiting to happen.")
    # ROUND-14 PREMISE NAME LOCK (NARASI_PREMISE_NAME_LOCK, default OFF): the brief NAMES its
    # leads (e.g. "Lee Seo-an", "Lee Hae-won"); roll-16 renamed them (Song/Do-yoon/Hwang) because
    # best-of-3 reinvented the cast. Extract the premise-supplied full names and order the bible
    # to USE THEM VERBATIM for those roles. Deterministic name grab (Latin given-surname shapes,
    # incl. hyphenated Korean given names); append-only; no-op when the brief names no one.
    if is_fiction and os.environ.get("NARASI_PREMISE_NAME_LOCK", "0").strip().lower() in ("1", "true", "yes", "on"):
        try:
            import re as _pnl_re
            # ROUND-14 audit: the given-surname shape also matches hyphenated English
            # adjectives/places ("New York-based", "Han River-side"). A romanized Korean
            # given name never ends in a common English word — drop those tails.
            _pnl_stop = {"based", "adjacent", "side", "style", "related", "driven", "level",
                         "owned", "run", "led", "backed", "facing", "bound", "wide", "term",
                         "old", "new", "free", "born", "made", "year", "story", "life",
                         "class", "scale", "wing", "bank", "front", "born", "long"}
            _pnl = []
            for _m in _pnl_re.finditer(r"\b([A-Z][a-z]+)\s+([A-Z][a-z]+(?:-[a-z]+)+)\b", str(topic or "")):
                _full = _m.group(0)
                if _m.group(2).rsplit("-", 1)[-1].lower() in _pnl_stop:
                    continue
                if _full not in _pnl:
                    _pnl.append(_full)
            _pnl = _pnl[:8]
            if _pnl:
                system = system + (
                    "\n\nADDENDUM — PREMISE NAME LOCK: the brief names these characters — use each "
                    "EXACTLY as written for that same role, never a re-invented substitute: "
                    + ", ".join(_pnl) + ". You may add fresh names ONLY for characters the brief "
                    "leaves unnamed.")
        except Exception:  # noqa: BLE001
            pass
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
    # ANTAGONIST LOGIC + REVEAL ETHICS (two depth levers — TWO independent lens reviews of the same 9.0
    # piece both named these as the gap between 9 and 9.5: a flat-motive antagonist, and a climax that
    # re-victimizes the wronged). FICTION-only, flag-gated NARASI_CRAFT_LEVERS_V2 (default OFF, SEPARATE
    # from NARASI_CRAFT_LEVERS so each bundle A/B-tests on its own). Both are BIBLE-level pins (once,
    # upfront — no per-chapter bloat), soft/conditional ("add nothing" when the premise doesn't call for
    # them), and phrased anti-backfire: motive is SHOWN not monologued; the reveal rule is a constraint,
    # not a softening. Worst case = a compact ignored pin.
    if is_fiction and os.environ.get("NARASI_CRAFT_LEVERS_V2", "0").strip().lower() in ("1", "true", "yes", "on"):
        system = system + (
            "\n\nADDENDUM to heading 1 (CHARACTERS) — ANTAGONIST LOGIC: IF the premise's central "
            "opposition is a specific PERSON acting from personal will, pin the ONE internally coherent "
            "reason they act — which may be a genuine moral logic (it feels, to THEM, like correction, "
            "duty, or fairness) OR a simple appetite they do not bother to dress up (greed, power, "
            "self-preservation, cruelty). What matters is that it is COHERENT and specific to THIS "
            "person, NOT that it be sympathetic; avoid only motiveless cartoon malice. Do NOT default "
            "every antagonist to 'believes they are the good guy' — vary the register (self-deception, "
            "cold appetite, wounded grievance, ideology, indifference) and pick the one TRUE to this "
            "premise, even if unflattering. Pin it as what the antagonist WANTS and DOES, phrased so a "
            "chapter-writer dramatizes it through action — never as a creed the character would recite. "
            "One line, drawn from who they already are in the outline; do NOT invent new wounds, "
            "backstory, or scenes. If the opposition is a person merely ENFORCING a system / policy / "
            "institution, or the conflict is systemic, internal, or a two-hander, add nothing — and do "
            "NOT manufacture a personal wound for a conflict the premise intends as impersonal."
            "\n\nADDENDUM to headings 6-7 (SOLUTION / OPEN THREADS) — REVEAL WITHOUT RE-HARM: apply ONLY "
            "when the premise clearly turns on the antagonist WEAPONIZING an INNOCENT third party's "
            "PRIVATE secret. In that case the protagonist's climactic reveal must NOT re-enact that harm "
            "— do not have the hero publicly expose those same private secrets, re-victimizing the "
            "wronged in the name of justice; let each wronged party choose what of their own truth to "
            "make public. EXCEPTION: when the premise's own justice IS public exposure of the "
            "ANTAGONIST'S OWN wrongdoing (a corruption exposé, a whistleblower arc, holding a wrongdoer "
            "publicly accountable), that reckoning proceeds IN FULL — this rule shields only innocent "
            "third parties' private secrets, NEVER the antagonist's culpable conduct. It is a constraint "
            "on the SOLUTION's mechanics only; do NOT let it surface as a character speech, lesson, or "
            "moral statement in the chapters. If in ANY doubt, or if the reckoning targets the "
            "antagonist's own conduct, add nothing."
            "\n\nBOTH V2 addenda are SECONDARY to headings 1-8: never let satisfying them shorten or "
            "weaken the #8 SIGNATURE HOOK payoff commitment.")
    # MID-ARC BEAT (setup/payoff completeness lever — recurring across the corpus: a planted
    # thread present thematically but resolved in the FINALE with no dramatized MIDDLE, e.g. a
    # falsified-ledger sub-plot that jumps from Ch2 plant to Ch8 payoff with a hollow center).
    # FICTION-only, flag-gated NARASI_MIDARC_BEATS (default OFF, SEPARATE flag so it A/B-tests on
    # its own — a "dramatize every thread" push can fight deliberate restraint, so it ships inert).
    # Extends heading 7: per thread, also name ONE mid-arc chapter that dramatizes it on-screen.
    if is_fiction and os.environ.get("NARASI_MIDARC_BEATS", "0").strip().lower() in ("1", "true", "yes", "on"):
        system = system + (
            "\n\nADDENDUM to heading 7 (OPEN THREADS TO RESOLVE) — MID-ARC BEAT: for each open "
            "thread, in addition to the late chapter that delivers its OUTCOME, also name ONE "
            "MID-ARC chapter where the thread is DRAMATIZED ON-SCREEN through character action or a "
            "concrete scene (not merely referenced, recapped, or alluded to), so its final-chapter "
            "payoff is EARNED rather than reported. One chapter per thread, ONE line; this only pins "
            "WHERE an already-required thread earns its middle — do NOT invent new threads, scenes, "
            "or subplots, and do NOT force a beat onto a thread the premise intends to keep quiet or "
            "off-screen. SECONDARY to heading 8: never let a mid-arc beat crowd out the SIGNATURE "
            "HOOK payoff.")
    # CONTINUITY PINS (flag NARASI_CONTINUITY_PINS, default OFF): five defect classes from the kdrama
    # job (eky9gcge) that headings 1-8 and the canon_registry do NOT pin: (a) the central symbolic
    # causation drifted between literal-magical and coincidence readings; (b) chapter months/durations
    # FROZE ("nine months without rain" verbatim in late chapters) — heading 4 pins ORDER, not an
    # absolute calendar; (c) a signature prop (umbrella class) teleported between holders; (d) Korean
    # naming errors (a married woman taking her husband's surname; family members swapping surnames);
    # (e) the finale named characters with no earlier on-page introduction. All five are BIBLE-level
    # pins (once, upfront — no per-chapter machinery), additive headings 9-13 in the style of 1-8,
    # each with an explicit "write 'none'" escape so they no-op on premises lacking the feature.
    # OFF ⟹ bible byte-identical. Pair with the heads extension in _story_bible_prompt (same flag).
    # ROUND 2 (same flag, additive — jobs eky9gcge/cpn9kjg7/ibtgb7zg, 3 prod rolls of one premise):
    # five MORE bible-level classes — flashback date drift (Aug 13 vs 14, 3/3 rolls), property
    # lease/parcel/eviction/deed instrument forks, mid-book entities used-as-known (Roh Gwang-su),
    # real-geo errors (Mokpo '90km northeast' of Haenam; actually ~35km NNW), and aftermath/casualty
    # numbers never committed. Round 2 extends heading 10 (FLASHBACK ANCHOR), broadens 13 (role tag
    # at FIRST mention for ANY plot-force entity, not just finale), and adds 14 PROPERTY & LEVERAGE,
    # 15 GEOGRAPHY POLICY, 16 AFTERMATH COMMIT. ROUND 3 adds 17 COUNTERPOINT NUMBERS (the
    # deliberate two-gauge divergence 112/287 vs 291 that the critic misread as a canon fork —
    # roll-5 score-4.0 anomaly); canon-diff and critic check #0 consume it as a sanction list.
    # ROUND 4 adds 18 EVIDENCE CHAIN & CUSTODY (roll-6 lens#2: casualty log + bank transfer
    # appeared with zero discovery process) and 19 CONTIGUOUS SCENES (roll-6 S1: the Room-204
    # climax staged twice with contradictory seats/transport). Headings 9-19 = eleven sections.
    if is_fiction and os.environ.get("NARASI_CONTINUITY_PINS", "0").strip().lower() in ("1", "true", "yes", "on"):
        system = system + (
            "\n\nADDITIONAL HEADINGS — continue the numbered fact-sheet with these twelve sections:\n"
            "9. CAUSALITY POLICY — if the premise has a central SYMBOLIC or seemingly supernatural "
            "causation (e.g. 'the rain returns when the truth is told'), COMMIT its metaphysics NOW "
            "as exactly ONE of: LITERAL-MAGICAL (the world really works this way), AMBIGUOUS-BY-DESIGN "
            "(the text must never confirm or deny — pin the mundane coincidence that preserves both "
            "readings), or BELIEVED-NOT-REAL (characters read meaning into it; the narration knows "
            "better). Every chapter renders cause and effect under the SAME policy. Also pin WHICH "
            "character voices the story's thesis about this causation ON THE PAGE, and in which "
            "chapter — the thesis belongs to ONE voice, never improvised per chapter. If the "
            "symbolic causation is itself the #8 SIGNATURE HOOK, the pinned policy (including its "
            "mundane-coincidence rendering, for AMBIGUOUS-BY-DESIGN) IS the hook's committed payoff "
            "— do not pin a second, conflicting answer. If the premise has no symbolic causation, "
            "write 'none'.\n"
            "10. CALENDAR SPINE (extends heading 4) — give EVERY chapter an ABSOLUTE calendar anchor, "
            "one line per chapter ('Ch3 = late March, early spring'; for books over 12 chapters, "
            "anchor only the chapters where the month or season CHANGES), months advancing MONOTONICALLY "
            "(never repeating or stepping back unless a flashback is explicitly pinned as one). Any "
            "stated DURATION ('nine months without rain', 'three years since X') must be RE-DERIVED "
            "from this spine at each chapter's own anchor — a writer may NEVER copy a duration "
            "verbatim from an earlier chapter, because a frozen duration contradicts a moving "
            "calendar. FLASHBACK ANCHOR: pin every BACKSTORY event any chapter revisits (the "
            "flood night, the accident, the signing) to ONE absolute date-and-time line "
            "('FLOOD NIGHT = 13 Aug, 20:30'); every retrospective reference — dates, weekdays, "
            "hours, 'X years ago' spans — must DERIVE from that pinned line, and no flashback "
            "may restate the anchor with a different date or hour. If the story runs a DAY "
            "COUNTER (a drought count, a vigil count), pin its EPOCH as an explicit line "
            "('DROUGHT EPOCH = 3 June 2019; every day-count in every chapter derives from "
            "THIS date') — counters must NEVER be re-derived from the protagonist's arrival "
            "or any other event, and a chapter may never reuse an earlier chapter's count "
            "after story time has advanced (roll-9 class: the arrival-day number reappearing "
            "fifty days later).\n"
            "11. SIGNATURE-PROP CUSTODY — for each SIGNATURE OBJECT the story's imagery leans on "
            "(the umbrella, the letter, the ring — at most 3), pin a CUSTODY CHAIN: one line per "
            "custody CHANGE (who hands it to whom, in which chapter, via what ON-PAGE handover) "
            "plus the final holder. An object may not appear in a character's hands without a "
            "pinned handover putting it there. If the premise has no signature object, write "
            "'none'.\n"
            "12. NAMING CONVENTIONS — pin the setting culture's naming rules and APPLY them to "
            "heading 1's cast: in Korean settings, married women KEEP their maiden surname (a wife "
            "sharing her husband's surname is an ERROR unless the premise explicitly pins it) and "
            "children take the father's surname — so no two family members share a surname except "
            "where that rule or a pinned canon fact creates it. Give each family a one-line surname "
            "map ('husband Kang, wife Yoon (maiden), children Kang') so writers cannot improvise.\n"
            "13. ENTITY INTRODUCTIONS & FINALE CAST — ANY named character OR company that exerts "
            "plot force anywhere in the book (acts, decides, threatens, buys, signs) gets a "
            "one-phrase ROLE TAG at its FIRST on-page mention ('Roh Gwang-su, Han-gang's "
            "land-acquisition director'); for each mid-book entrant, pin WHICH chapter introduces "
            "them. Walk-ons exerting no plot force stay NAMELESS. The final chapters may NOT name "
            "any character who lacks an earlier ON-PAGE introduction: list the characters "
            "permitted to appear or be named in the last two chapters (drawn from heading 1); if "
            "the finale needs someone new, add them to an earlier chapter's cast NOW.\n"
            "14. PROPERTY & LEVERAGE — for EACH property or asset the plot puts pressure on (the "
            "building, the land, the shop — at most 3), pin ONE line: the holder's status as "
            "exactly one word, OWNER or TENANT, and the ONE legal instrument used against it (an "
            "eviction notice, a compulsory-purchase order, a foreclosure on the deed, a "
            "terminated lease). Every chapter uses THAT status and THAT instrument: an OWNER "
            "cannot be evicted under a lease, a TENANT cannot have a deed seized — never mix "
            "instruments that contradict the pinned status. If the status legitimately CHANGES "
            "ON-PAGE (a sale, a signed transfer, a foreclosure), pin the ONE chapter where it "
            "changes; before that chapter every writer uses the original status. If ownership is "
            "SHARED or inherited (an estate partition, a disputed house), ALSO pin each "
            "claimant's LEGAL BASIS in a few words ('widow — registered marriage', 'brother — "
            "intestate heir', 'fiancée — NO standing unless a will/marriage is pinned') — no "
            "character may assert a share on the page without a pinned basis. If no property "
            "is leveraged, write 'none'.\n"
            "15. GEOGRAPHY POLICY — commit NOW to exactly ONE of: REAL-TOWN (the setting is a "
            "real place; pin the few place-to-place facts chapters may use — distances, bearings, "
            "travel times, neighboring towns — and every pinned fact must be REAL-WORLD CORRECT; "
            "if you are not CERTAIN of a real fact, omit it, and writers may not invent any "
            "others) or FICTIONAL-TOWN (an invented town, freely mapped — optionally 'in the "
            "manner of' a real region, but never named as a real town). DEFAULT to "
            "FICTIONAL-TOWN unless the premise itself NAMES a real town. NEVER a real town "
            "with invented geography. The SAME rule governs INSTITUTIONS and HISTORY: a REAL "
            "company, agency, or conglomerate may NEVER be the story's wrongdoer — invent a "
            "fictional firm 'in the manner of' one; and never present an invented historical "
            "episode of a real institution as documented fact.\n"
            "16. AFTERMATH COMMIT — pin the consequence beats the ending must land: what happens "
            "to the antagonist, the company, and the case or investigation AFTER the climax (one "
            "line each), plus the KEY PUBLIC NUMBERS the in-world record would state (casualty "
            "count, sentence, settlement) as EXACT values that every chapter touching the "
            "aftermath must reuse verbatim. If the premise ends before any aftermath exists, "
            "write 'none'.\n"
            "17. COUNTERPOINT NUMBERS — if the plot deliberately keeps TWO versions of the same "
            "measurement or count alive (an official record vs a private measurement, a cover "
            "story's figure vs the truth), pin each PAIR on one line: the two exact values, who "
            "holds each, and the chapter where the gap is revealed ('official rain total 112mm "
            "(agency record) vs 291mm (private gauge), gap revealed Ch6'). These pairs are "
            "SANCTIONED DIVERGENCES: continuity tools treat the two values as ONE designed fact, "
            "never a contradiction — and writers must never average, reconcile, or 'correct' one "
            "toward the other. If the premise has no counterpoint pair, write 'none'.\n"
            "18. EVIDENCE CHAIN & CUSTODY — for EVERY evidence item that accuses or convicts "
            "anyone (an altered report, a bank transfer, a suppressed casualty list, a phone "
            "log), pin ONE line: WHO finds it, WHERE it survived the intervening years and WHY it "
            "survived (a carbon copy believed destroyed; a registrar who quietly kept it), who "
            "can AUTHENTICATE it, and the chapter where its DISCOVERY happens ON-PAGE. No "
            "convicting document may simply exist at the moment it is needed: if the climax uses "
            "it, an earlier chapter must dramatize the finding. For any falsified COUNT, also pin "
            "the concealment mechanism (how 14 dead became an official 3: which categories — "
            "missing at sea, unrelated landslide, departed migrant workers — absorbed the "
            "difference). For any named culprit, pin the ONE document or act that ties THEM "
            "specifically to the crime. If no such evidence exists, write 'none'.\n"
            "19. CONTIGUOUS SCENES — if consecutive chapters share ONE continuous scene, session, "
            "or gathering (a hearing spanning two chapters), pin it: 'Ch6→Ch7 = ONE continuous "
            "session, Room 204, same day'. The LATER chapter RESUMES mid-scene: it may NOT "
            "re-introduce the room, re-seat the cast, restate arrivals, transport, or dates, or "
            "refer to the earlier half as a separate past event. Pin each attendee's seat/position "
            "and travel mode ONCE, here, and every chapter touching the scene reuses them "
            "verbatim. If no scene spans chapters, write 'none'.\n"
            "20. EVIDENCE MAP — for EVERY physical evidence object (a negative, a tape, a "
            "letter bundle, an altered report): ONE line each — WHAT it is, WHO created it, "
            "the ONE place it has been hidden all these years, WHO finds it, in WHICH "
            "chapter, and each custody move after that. Two objects may NEVER swap hiding "
            "places, finders, or discovery chapters; if the story holds both a document and "
            "a recording, give each its own line and keep them distinct in every chapter "
            "that touches them. If no evidence objects exist, write 'none'.\n"
            "These sections are SECONDARY to headings 1-8: never let them shorten or weaken the #8 "
            "SIGNATURE HOOK payoff commitment.")
    # CANON REGISTRY (Phase 2a) — emit a MACHINE-CHECKABLE twin of the prose fact-sheet so a later
    # per-chapter diff (Phase 2b) can catch canon-forks (one load-bearing fact rendered two ways,
    # e.g. the river pusaran victim/age fork). FICTION-only, flag-gated NARASI_CANON_REGISTRY (default
    # OFF → not emitted → bible byte-identical). Same env-gated addendum pattern as the levers above.
    if is_fiction and os.environ.get("NARASI_CANON_REGISTRY", "0").strip().lower() in ("1", "true", "yes", "on"):
        system = system + (
            "\n\nADDENDUM to heading 6 (KEY FACTS / REVEAL) — CANON REGISTRY: AFTER the prose "
            "fact-sheet, output a fenced ```json code block labelled canon_registry serializing ONLY "
            "the LOAD-BEARING facts as machine-checkable rows. Shape: {\"events\":[{\"id\":\"<slug>\","
            "\"summary\":\"<short>\",\"when\":{\"actor_age\":<int|null>,\"anchor\":\"<slug>\"},"
            "\"participants\":{\"<role>\":\"<entity_id>\"},\"key_action\":\"<slug>\",\"moral_load\":"
            "\"<one line: why the plot turns on this>\",\"false_versions\":[{\"claim\":\"<what a "
            "character wrongly believes or tells>\",\"corrected_in_chapter\":<n>}],\"chapters\":[<n>]}],"
            "\"entities\":[{\"id\":\"<slug>\",\"name\":\"<str>\",\"kinship\":{\"<rel>\":\"<entity_id>\"},"
            "\"knowledge\":[{\"fact_id\":\"<slug>\",\"knows\":\"<what>\",\"since_chapter\":<n>}]}],"
            "\"timeline\":[{\"id\":\"<slug>\",\"order\":<int>}],\"kinship\":[{\"a\":\"<id>\",\"b\":"
            "\"<id>\",\"relation\":\"<str>\"}]}. RULES: pin ONLY facts a chapter's plot turns on — an "
            "event enters ONLY if it carries a moral_load; hard caps <=8 events, <=15 entities, <=12 "
            "timeline anchors, so you TRIAGE, not dump. Use canonical TOKENS/ints (victim=entity_id, "
            "age=int), NEVER prose descriptors (so 'dusk' vs 'evening' cannot fork). If the story "
            "legitimately has a character believe or tell a FALSE version that a later chapter "
            "corrects, record it under that event's false_versions with corrected_in_chapter, so a "
            "pre-correction rendering is treated as LEGAL, not a fork. This JSON is machine-only; it "
            "does NOT replace the prose fact-sheet above.")
    # ANTI-HOMOGENIZATION (flag NARASI_ANTI_HOMOGENIZATION, default OFF): a cross-roll audit (fallen-angel
    # ↔ Lumi, same kdrama lane) exposed a lane-default TIC-LEXICON + a repeated CLIMAX SKELETON colonizing
    # the bible/outline (not the prose): opening timestamps ending :14, the 11th floor, cold-coffee-as-
    # opening-beat, tteokbokki-as-intimacy-food, the 'X do not Y' aphorism at a villain's defeat, the
    # number-11 motif, and the 'protagonist destroys the asset mid-vote' resolution. Steer AWAY at the
    # bible so the perturbation lands before the prose. FICTION-only. OFF ⟹ bible byte-identical.
    if is_fiction and os.environ.get("NARASI_ANTI_HOMOGENIZATION", "0").strip().lower() in ("1", "true", "yes", "on"):
        system = system + (
            "\n\nADDENDUM (ALL headings) — ANTI-SAMENESS: this lane has overused a set of DEFAULT tics; "
            "you MUST avoid them and pick fresh, specific choices particular to THIS premise. BANNED "
            "lane-defaults: (1) an opening timestamp ending in ':14' (6:14/7:14/2:14) — use any other "
            "minute; (2) setting the office/key scene on the ELEVENTH floor — pick a different floor; "
            "(3) 'cold coffee' as the opening character beat; (4) tteokbokki as the intimacy/comfort food "
            "— vary the dish; (5) the '[X] do not [Y]' aphorism at a villain's defeat ('Boards do not "
            "gasp' / 'men like him do not'); (6) leaning on the number-11 / eleven-minute motif. AND "
            "diversify the CLIMAX RESOLUTION: do NOT default to 'the protagonist destroys or renders the "
            "asset worthless in the middle of a shareholder/board vote' — choose a DIFFERENT resolution "
            "shape (win-by-exposure, win-by-outmaneuver, lose-but-intact, refuse-the-game, a cost paid "
            "elsewhere). The world's specifics — place, hour, object, food, floor, and how the climax "
            "resolves — must be chosen for this story, not inherited from the lane's habit.")
    # ANTI-HOMOGENIZATION V2 (flag NARASI_ANTI_HOMOG_V2, default OFF — separate so it A/B's on its own):
    # the Archivist round-3 cross-roll audit showed the v1 LEXICON bans work but the deepest tics are
    # BEAT-SLOTS portable across vocabulary (food-portioning-as-love survived tteokbokki→tin-cups→
    # persimmon-thirds; the repertory cast Hae-rin/Ha-rin + Ok; the favorite number 41), and the
    # "Kenapa" home-language bleed recurred with a reproducible trigger (introspective-question beat
    # fills "her own language" with the BRIEF's language — Rino's synopses are Indonesian). Since the
    # fact sheet rides into EVERY chapter worker as the pinned prefix, rules EMITTED IN the sheet reach
    # the prose. OFF ⟹ bible byte-identical.
    if is_fiction and os.environ.get("NARASI_ANTI_HOMOG_V2", "0").strip().lower() in ("1", "true", "yes", "on"):
        system = system + (
            "\n\nADDENDUM (ALL headings) — BEAT-SLOT & CAST VARIATION: (A) BANNED repertory: do not "
            "name characters 'Hae-rin', 'Ha-rin', 'Ok Jae-heon', 'Ok Hye-ran' or near-variants (this "
            "lane has reused that cast); invent fresh names. Do not use 41/forty-one as a count or "
            "spec (sacks, leaves, gigabytes, pages — a lane fingerprint); pick other values. (B) The "
            "lane's recurring BEAT-SLOTS are habits, not requirements — (i) food-portioning-as-love "
            "(splitting/giving the larger share), (ii) an honorific/rank-term dropped or repurposed as "
            "intimacy, (iii) the italicized interior question, (iv) the aphoristic two-sentence cold "
            "open. For EACH: either execute it in a fresh, premise-specific way or SKIP it, and never "
            "use more than TWO of them in their lane-default form; PIN in the fact sheet this story's "
            "chosen intimacy gesture and opening mode so every chapter follows the same fresh choice. "
            "(C) LANGUAGE LOCK — emit this as the fact sheet's FINAL line, verbatim rule: 'LANGUAGE "
            "LOCK: every character's dialogue and interior thought is rendered fully in the "
            "manuscript's language, or fully in the character's own in-world language — NEVER in the "
            "language of this production brief, and NEVER as a hybrid. If a single foreign-language "
            "interjection is used for flavor, it must stand ALONE as an interjection (followed by its "
            "own sentence), never spliced into the syntax of another language's sentence.'")
    # LANE LEDGER (flag NARASI_LANE_LEDGER, default OFF): the cross-roll USED-NAMES/NUMBERS ledger.
    # Static in-prompt ban-lists proved WHACK-A-MOLE (V2 banned the Lumi/Archivist repertory
    # Hae-rin/Ok — the next roll drew from the fallen-angel repertory instead: Gyeom/Chae-rin/
    # Hyeon-jae/Baek; the [C/H]a(e)-ri[n/m] female-name shape hit 4/4 rolls, :14 hit ×7). The fix is a
    # single GROWING per-lane ledger (pakem/lane_ledger.json — curated JSON per the corpus policy;
    # raw data persists per-job in counter_report.homogenization_tics, and — for the "overused_phrases"
    # key below — counter_report.style_saturation once NARASI_STYLE_SATURATION_SCAN is curated in the
    # same way) injected as a NEGATIVE
    # constraint at bible-time. Fail-open everywhere: missing file / unknown style / bad JSON ⟹ no
    # paragraph, bible unchanged. OFF ⟹ byte-identical.
    if is_fiction and os.environ.get("NARASI_LANE_LEDGER", "0").strip().lower() in ("1", "true", "yes", "on"):
        try:
            import json as _lj
            _lpath = os.path.join(os.path.dirname(__file__), "..", "pakem", "lane_ledger.json")
            with open(_lpath, encoding="utf-8") as _lf:
                _ledger = _lj.load(_lf) or {}
            _lane = _ledger.get((style or "").strip()) or {}
            # isinstance guard: a style of '_comment'/'_seeded_from' (metadata STRING keys, and style is
            # user-supplied) would make _lane a truthy str and AttributeError inside _fmt — caught by the
            # outer except either way, but cheaper to never enter.
            if isinstance(_lane, dict) and _lane:
                def _fmt(key):
                    v = _lane.get(key) or []
                    return ", ".join(str(x) for x in v[:40])
                system = system + (
                    "\n\nADDENDUM (ALL headings) — LANE LEDGER, NEGATIVE CONSTRAINTS: previous stories "
                    "in this exact lane have ALREADY USED the following; a binge viewer will notice the "
                    "repetition, so do NOT reuse or near-vary any of them. Already-used GIVEN NAMES: "
                    + _fmt("given_names") + ". Banned NAME SHAPES: " + _fmt("name_stems") + ". Overused "
                    "SURNAMES (vary away from these): " + _fmt("overused_surnames") + ". Already-used "
                    "clock minutes: " + _fmt("timestamp_minutes") + " — pick other minutes. Already-used "
                    "small numbers for counts/specs: " + _fmt("small_numbers") + " — pick other values. "
                    "Already-used comfort/intimacy foods: " + _fmt("foods") + " — choose a different "
                    "dish. Already-used building floors: " + _fmt("floors")
                    + ((". Already-used DISTRICTS/PLACES (pick different neighborhoods): " + _fmt("places"))
                       if _lane.get("places") else "")
                    + ". BANNED verbatim "
                    "phrases (never reproduce these lines): " + _fmt("verbatim_phrases") + ". Overused "
                    "BEATS (execute differently or SKIP): " + _fmt("recycled_beats") + ". Overused STYLE "
                    "PHRASES (word/phrase crutches this lane leans on — vary your prose, do not lean on "
                    "these): " + _fmt("overused_phrases") + ". Invent fresh, "
                    "premise-specific choices for every one of these slots.")
        except Exception:  # noqa: BLE001 — ledger is an enhancement; its absence must never block a bible
            pass
    # ENFORCEMENT RE-ROLL (round-3, caller-driven via NARASI_LEDGER_ENFORCE): the first
    # draft's bible-level ledger hits, quoted back so the retry knows exactly what to
    # replace. None (default) ⟹ byte-identical prompt.
    if extra_negative:
        system = system + (
            "\n\nENFORCEMENT RE-ROLL — your previous draft violated the lane ledger by using: "
            + str(extra_negative)[:600] +
            ". Regenerate the bible WITHOUT these items or near-variants of them; replace each "
            "with a fresh, premise-specific invention. Every other requirement above still applies.")
    prompt = _story_bible_prompt(topic, outline, language, is_fiction)
    # The bible BLOCKS the whole job before any chapter starts, so it must be FAST + RELIABLE. Opus is the
    # FIRST heavy call of the job (cold KIE connection) and is flaky/slow for it: when it works ~106s, else a
    # full NARASI_BIBLE_TIMEOUT waste (~300s) that ALSO double-bills the abandoned LaoZhang opus (a 502 to us
    # while LaoZhang finishes + bills it server-side). Sonnet is fast (~89s), reliable, and strong enough for
    # a fact-sheet. Default the bible to sonnet; NARASI_BIBLE_MODEL overrides (set claude-opus-4-6 to force
    # opus and accept the latency/cost). Chapters keep their own worker_model (opus) — they run warm + fine.
    _primary  = (os.environ.get("NARASI_BIBLE_MODEL", "claude-sonnet-4-6") or "").strip() or (manager_model or MANAGER_MODEL)
    # Last-resort net if the bible model itself fails on every provider (rare for sonnet): a fast, KIE-free
    # Vertex model so the job still completes. Env-overridable.
    _fallback = (os.environ.get("NARASI_BIBLE_FALLBACK_MODEL", "gemini-2.5-flash") or "").strip()
    _chain = [_primary] + ([_fallback] if (_fallback and _fallback != _primary) else [])
    # ROUND-8 (roll-11 bible-less disaster): prod pinned BOTH chain slots to opus-family
    # models and BOTH timed out at 300s — the fast-model fallback this docstring promises
    # never ran, and 10 chapters went out with no canon (two load-bearing forks). A
    # LAST-RESORT slot that env config cannot accidentally remove: default WORKER_MODEL,
    # disable with NARASI_BIBLE_LAST_RESORT=0.
    if str(os.environ.get("NARASI_BIBLE_LAST_RESORT", "1")).strip().lower() in ("1", "true", "yes", "on"):
        _lr = os.environ.get("NARASI_BIBLE_LAST_RESORT_MODEL", "").strip() or WORKER_MODEL
        if _lr and _lr not in _chain:
            _chain.append(_lr)
    # Right-size the bible: it is a numbered fact-sheet (~2-4k tokens), NOT a book. Leaving max_tokens unset
    # made it inherit the opus 128k ceiling → a heavy, slow non-streaming KIE request that outran its timeout
    # and hung the read. Cap it (env-tunable). The bible uses the SAME global per-rung failover timing as
    # chapters (which are reliable) — no bible-specific short-rung override — plus the wall-clock deadline
    # now enforced in _anthropic_messages_create, so a slow rung aborts to the fallback instead of hanging.
    _bib_max = max(1000, int(os.environ.get("NARASI_BIBLE_MAX_TOKENS", "12000")))
    _TRUNC_FINISH = ("length", "max_tokens", "max_output_tokens", "model_length")
    # NARASI_BIBLE_SKIP_KIE=1 → bible attempts start the opus failover chain at rung 2 (LaoZhang).
    # The bible is the job's FIRST heavy call and cold-KIE non-streaming reads hang it; the chain's
    # rung timing (280s × NARASI_RUNG_ATTEMPTS=2 = 560s for KIE alone) is chapter-calibrated (900s
    # window) and exceeds the WHOLE bible window (NARASI_BIBLE_TIMEOUT), so a hung KIE rung eats the
    # full bible timeout before rung 2 is ever tried. Guarded import: laozhang_api may be
    # unimportable in standalone orchestrator use — then this is a silent no-op (that fallback path
    # has no KIE rung to skip anyway). Default OFF = byte-identical behavior.
    _skip_var = None
    if os.environ.get("NARASI_BIBLE_SKIP_KIE", "0").strip().lower() in ("1", "true", "yes", "on"):
        try:
            from laozhang_api import _narasi_skip_kie as _skip_var  # type: ignore
        except Exception:  # noqa: BLE001
            _skip_var = None
    # Confirmed live this session (billing evidence): with NARASI_BIBLE_LAOZHANG set to a model
    # id, the laozhang rung's served model gets rewritten IN PLACE regardless of which _chain
    # entry this attempt actually requested — so attempt 2 (meant to fail over to a DIFFERENT,
    # faster model) can silently re-run the EXACT SAME model attempt 1 already tried and failed,
    # double-billing for a "fallback" that never actually fires. Look the override up ONCE,
    # up front, so the loop below can detect and skip that specific waste. Guarded import:
    # laozhang_api may be unimportable in standalone orchestrator use — then this is a silent
    # no-op (None), same as the _skip_var import above.
    _lz_override = None
    try:
        from laozhang_api import _narasi_phase_laozhang_override  # type: ignore
        _lz_override = _narasi_phase_laozhang_override("bible")
    except Exception:  # noqa: BLE001
        _lz_override = None
    # ROUND-25 (adversarial audit of round-24's fix above): each _chain entry is NOT a single
    # laozhang call — build_story_bible's Worker/run_worker path fans each model id out into a
    # FULL multi-rung failover chain (e.g. kie+laozhang+atlascloud for opus-family models).
    # Skipping the WHOLE attempt via `continue` below whenever the laozhang leg is doomed also
    # discards every OTHER rung in that attempt (kie/claude_native/atlascloud/vertex), which
    # serve the correctly-requested model and are completely unaffected by the override. That's
    # only safe when laozhang is confirmed to be the SOLE rung for "bible" (every sibling
    # provider explicitly force-excluded) — so require that confirmation too before skipping.
    # Guarded import, same pattern as _lz_override above; False is the safe default (assume a
    # sibling rung might exist, so do NOT skip).
    _lz_sole_rung = False
    try:
        from laozhang_api import _narasi_phase_laozhang_sole_rung  # type: ignore
        _lz_sole_rung = _narasi_phase_laozhang_sole_rung("bible")
    except Exception:  # noqa: BLE001
        _lz_sole_rung = False
    for _i, _mdl in enumerate(_chain):
        # If NARASI_BIBLE_LAOZHANG pins the laozhang rung to a model already tried earlier in
        # THIS chain, this attempt would just repeat that already-failed call verbatim rather
        # than the different, presumably-more-reliable model it was requesting — skip it rather
        # than wastefully repeating (and double-billing) a doomed call. No new flag needed: this
        # only changes behavior in a scenario that is ALREADY broken (a fallback that silently
        # isn't one), and it can only make that scenario fail faster/cheaper, never worse. Also
        # require _lz_sole_rung: without it, skipping the whole attempt could discard a
        # legitimate, unaffected sibling rung (kie/claude_native/atlascloud/vertex) that might
        # have succeeded — the exact bug an adversarial audit caught in round 25.
        if _i > 0 and _lz_override and _lz_override in _chain[:_i] and _lz_sole_rung:
            log.warning(
                "build_story_bible: NARASI_BIBLE_LAOZHANG pins the laozhang rung to model %s "
                "regardless of what this attempt (%d/%d, requested %s) asked for — %s was "
                "already tried and failed earlier in this chain, so this attempt would only "
                "repeat that same doomed call and double-bill it. Skipping.",
                _lz_override, _i + 1, len(_chain), _mdl, _lz_override)
            continue
        worker = Worker(
            name="planner:bible", role="manager", phase="bible", model=_mdl,
            system=system, temperature=0.3, max_tokens=_bib_max, telemetry_sink=telemetry_sink,
        )
        _skip_tok = _skip_var.set(True) if _skip_var is not None else None
        try:
            res = await run_worker(worker, prompt, timeout=_to, task_id="planner:bible")
        finally:
            # Reset is REQUIRED: chapter tasks created later copy this context — a leaked
            # True would strip KIE from every chapter of the job.
            if _skip_tok is not None:
                _skip_var.reset(_skip_tok)
        _fin = str(((res.get("telemetry") or {}).get("finish_reason")) or "").lower()
        _truncated = _fin in _TRUNC_FINISH
        if res.get("ok") and str(res.get("output") or "").strip() and not _truncated:
            if _i > 0:
                log.info("build_story_bible: primary failed — bible via fallback model %s", _mdl)
            return str(res["output"]).strip()
        if _truncated:
            # A bible cut off at the token cap is INCOMPLETE — every parallel chapter would inherit a
            # partial fact-sheet. Reject it and fail over (or proceed with none) rather than poison the book.
            log.warning("build_story_bible: model %s bible TRUNCATED at cap=%d (finish=%s) — failing over",
                        _mdl, _bib_max, _fin)
        else:
            log.info("build_story_bible: model %s returned no usable bible (attempt %d/%d)%s",
                     _mdl, _i + 1, len(_chain),
                     " — failing over" if _i + 1 < len(_chain) else " — proceeding without one")
    log.warning("BIBLE-LESS ROLL: all %d bible attempt(s) failed — %d parallel chapters will "
                "run with NO canonical facts (fork risk HIGH; see roll-11 postmortem)",
                len(_chain), len(outline or []))
    return ""


__all__ = [
    "plan_subtasks",
    "outline_from_topic",
    "build_story_bible",
    "_parse_json_loose",
    "_static_subtask_plan",
    "_static_outline",
]
