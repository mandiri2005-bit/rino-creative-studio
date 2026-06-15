# -*- coding: utf-8 -*-
"""
video_segmenter.py — Step 6 / Workstream F-1: the **scene segmenter**, the
central controller of the video-assembly engine.

WHY THIS EXISTS
---------------
The whole video pipeline is a straight line with one data structure flowing
through it: the *scene*. This module is the only thing that creates scenes.
Everything downstream — the parallel dispatch engine (Step 6b), the FFmpeg
stitcher (Step 6c), the visual-mode / fit gate (Step 6d), the master clock
(Step 6e) — reads scene objects it never produces. Build this first, because
nothing else exists without its output.

THE FORMULA (the contract)
--------------------------
Documentary narration is paced at **130 words per minute**. From a target
duration in minutes, everything cascades:

    target_words    = round(minutes * 130)
    scene_count     = max(2, round(target_words / 45))
    words_per_scene = round(target_words / scene_count)
    seconds_per_scene = words_per_scene / 130 * 60

Dispatch and UI fall out of scene_count:

    scene_count <= 10  →  full-parallel dispatch, scene-card progress UI
    scene_count >  10  →  batched dispatch (10 at a time), progress-bar UI

Planning credits scale per scene by quality tier (Fast x2 / HD x5 / HD+ x8).
This is only the *up-front estimate* shown to the user; the real charge at
dispatch time comes from credit_catalog.py against measured seconds/chars.

The `--all-durations` CLI output must match the Duration Presets table from the
video-assembly roadmap. See `duration_table()` / `verify_contract()`.

    NOTE on the source table: every preset row follows credits = scenes x
    {2,5,8} EXCEPT the 1-minute HD/HD+ cells, which the roadmap renders as
    18/28. That is a typo in the doc — 3 scenes x 5 = 15 and x 8 = 24, which is
    what this module emits (and what every other row confirms). The per-scene
    multiplier is the real contract; the table is a rendering of it.

TWO NARRATION MODES
-------------------
    Mode A (topic-first)    — the caller generates fresh narration to
                              `params.target_words` (use `build_generation_prompt`),
                              then segments it into exactly `scene_count` scenes.
    Mode B (narration-first)— the caller already has narration. We NEVER truncate
                              it; instead we derive the scene count from its true
                              word count and report the real length back.

THE VISUAL PROMPT
-----------------
Each scene gets a cinematography prompt built from subject + setting + action.
If spaCy + an `xx_*`/`en_*` model is installed it is used for NER and sentence
segmentation; otherwise a dependency-free heuristic fallback runs, so this
module imports and runs anywhere (tests, CLI, a bare container) with no model
download. The narration style maps to a cinematographic tone via `STYLE_TONE`.

This module is pure, synchronous, and has no I/O or network calls — it is safe
to import from either backend and trivial to unit-test. Run it directly:

    python video_segmenter.py --all-durations          # print the contract table
    python video_segmenter.py --minutes 3 --tier hd     # params for a 3-min video
    python video_segmenter.py --text "<narration>"      # Mode B segment a script
    python video_segmenter.py --self-test               # assert the contract
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import Optional

# ══════════════════════════════════════════════════════════════════════════════
# Constants — the formula's tunables
# ══════════════════════════════════════════════════════════════════════════════
WORDS_PER_MINUTE = 130          # documentary narration pace
WORDS_PER_SCENE = 45            # scene_count = round(target_words / WORDS_PER_SCENE)
MIN_SCENES = 2                  # a video is at least two scenes (so there's a cut)
BATCH_SIZE = 10                 # full-parallel <= BATCH_SIZE scenes, batched above
PROGRESS_CARD_LIMIT = 10        # scene cards <= this many scenes, progress bar above
MAX_VISUAL_PROMPT_CHARS = 240   # cap the cinematography prompt for the image API
MAX_VISUAL_PROMPT_WORDS = 40

# Per-scene planning credits by quality tier (the up-front estimate; real metering
# is credit_catalog at dispatch). Keys match the duration-preset table columns.
TIER_CREDITS_PER_SCENE: dict[str, int] = {"fast": 2, "hd": 5, "hd_plus": 8}
_TIER_ALIASES = {
    "fast": "fast",
    "hd": "hd",
    "hd+": "hd_plus", "hdplus": "hd_plus", "hd_plus": "hd_plus", "hdp": "hd_plus",
}

# Per-model clip config — the menu the eligibility gate (Step 6d) reads. The
# segmenter precomputes each scene's fit against the *chosen* model so the gate
# is a cheap lookup, not a recompute. Ceilings are version-specific and moving,
# so they live in config, never hardcoded in logic.
CLIP_MODELS: dict[str, dict] = {
    "veo3":   {"max_s": 8,  "allowed": [4, 6, 8],   "gate": 0.85},   # ~6.8s eligible
    "kling3": {"max_s": 15, "allowed": [5, 10, 15],  "gate": 0.85},   # ~12.7s eligible
}
DEFAULT_CLIP_MODEL = "veo3"

# Narration style → cinematographic tone. Keys cover both canonical RAG style
# names (style_rag_config.py) and the user-facing display names. Normalised
# (lowercased, spaces/dashes → underscores) before lookup; unknown → default.
STYLE_TONE: dict[str, str] = {
    "storytelling":            "warm dramatic lighting, intimate character framing, shallow depth of field",
    "bedtime_story":           "soft golden-hour glow, gentle pastel palette, dreamlike soft focus",
    "creative_nonfiction":     "naturalistic cinematic light, textured realism, considered composition",
    "creative_non_fiction":    "naturalistic cinematic light, textured realism, considered composition",
    "big_history":             "epic sweeping vista, deep-time grandeur, cool expansive palette",
    "harari":                  "epic sweeping vista, deep-time grandeur, cool expansive palette",
    "pov_first_person":        "intimate first-person perspective, eye-level handheld, personal detail",
    "biography":               "intimate first-person perspective, eye-level handheld, personal detail",
    "natgeo":                  "crisp natural-light documentary realism, rich field colour, observational framing",
    "documentary":             "crisp natural-light documentary realism, rich field colour, observational framing",
    "youtube_popular_science": "clean bright explanatory lighting, vivid diagrammatic clarity",
    "science":                 "clean bright explanatory lighting, vivid diagrammatic clarity",
    "academic_popular":        "measured editorial lighting, restrained palette, authoritative composition",
    "finance":                 "measured editorial lighting, restrained palette, authoritative composition",
    "economics":               "measured editorial lighting, restrained palette, authoritative composition",
    "business":                "measured editorial lighting, restrained palette, authoritative composition",
    "literary_essay":          "contemplative muted tones, painterly stillness, generous negative space",
    "philosophical":           "contemplative muted tones, painterly stillness, generous negative space",
}
DEFAULT_TONE = "cinematic natural lighting, documentary realism, balanced composition"

# Framing by scene position in the sequence.
_POSITION_FRAMING = {
    "opening": "establishing wide shot",
    "middle":  "medium cinematic shot",
    "closing": "resolving wide shot, slow push-out",
}


# ══════════════════════════════════════════════════════════════════════════════
# Data structures — the scene is the atom every later stage operates on
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class VideoParams:
    """The plan for one video, derived from a target duration. This is the object
    the UI, the dispatcher and the credit hold all read."""
    minutes: float
    target_words: int
    scene_count: int
    words_per_scene: int
    seconds_per_scene: float
    batch_size: int
    dispatch_mode: str          # "full_parallel" | "batch"
    batch_plan: list[int]       # e.g. [10, 4] — sizes of each dispatch batch
    progress_ui: str            # "cards" | "bar"
    tier: str                   # "fast" | "hd" | "hd_plus"
    credits: int                # planning estimate = scene_count * per-scene rate
    credits_by_tier: dict[str, int] = field(default_factory=dict)


@dataclass
class Scene:
    """One scene — the unit every downstream worker generates and the stitcher
    fuses. `audio_url` / `clip_url` are the empty slots the parallel workers
    (Step 6b) fill in; the segmenter leaves them None."""
    number: int                 # 1-indexed scene number
    text: str                   # the narration spoken over this scene
    word_count: int
    est_seconds: float          # word-count estimate; ffprobe measures truth later
    position: str               # "opening" | "middle" | "closing"
    visual_prompt: str          # cinematography prompt for the image/clip model
    clip_eligible: bool         # est_seconds fits the chosen model's ceiling x gate
    suggested_clip_seconds: Optional[int]  # smallest allowed clip >= est_seconds, else None
    audio_url: Optional[str] = None        # filled by the audio worker (Step 6b)
    clip_url: Optional[str] = None         # filled by the visual worker (Step 6b)


@dataclass
class SegmentResult:
    """The full output: the plan plus the scenes. For Mode B, `actual_*` reflect
    the existing narration's true length and `truncated` is always False."""
    mode: str                   # "A" | "B"
    params: VideoParams
    scenes: list[Scene]
    actual_words: int
    actual_minutes: float
    truncated: bool             # invariant: always False — Mode B never truncates
    note: str

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "params": asdict(self.params),
            "scenes": [asdict(s) for s in self.scenes],
            "actual_words": self.actual_words,
            "actual_minutes": self.actual_minutes,
            "truncated": self.truncated,
            "note": self.note,
        }


# ══════════════════════════════════════════════════════════════════════════════
# Normalisation helpers
# ══════════════════════════════════════════════════════════════════════════════
def _normalize_tier(tier: str) -> str:
    t = (tier or "hd").strip().lower().replace(" ", "_")
    return _TIER_ALIASES.get(t, "hd" if t not in TIER_CREDITS_PER_SCENE else t)


def _normalize_style(style: str) -> str:
    return (style or "").strip().lower().replace("-", "_").replace(" ", "_")


def _normalize_clip_model(model: str) -> str:
    m = (model or "").strip().lower()
    if m in CLIP_MODELS:
        return m
    # accept "veo"/"veo-3.1" → veo3, "kling"/"kling-3.0" → kling3
    if m.startswith("veo"):
        return "veo3"
    if m.startswith("kling"):
        return "kling3"
    return DEFAULT_CLIP_MODEL


# ══════════════════════════════════════════════════════════════════════════════
# The core formula
# ══════════════════════════════════════════════════════════════════════════════
def scene_count_for_words(words: int, words_per_scene: int = WORDS_PER_SCENE) -> int:
    """Words → scene count, floored at MIN_SCENES. round() is banker's rounding in
    Python, but it reproduces every preset row exactly (verified in tests).
    `words_per_scene` is overridden smaller for clip-led videos (see
    words_per_scene_for) so scenes are short enough to fit a real clip."""
    wps = max(1, int(words_per_scene or WORDS_PER_SCENE))
    return max(MIN_SCENES, round(max(0, words) / wps))


def words_per_scene_for(visual_mode: str, clip_model: str = DEFAULT_CLIP_MODEL) -> int:
    """Scene length target in words. Image-led videos use long scenes (fewer,
    cheaper). Clip-led videos (full_clips / hybrid) MUST cut scenes down to the
    clip length — otherwise every scene overshoots the model's ~6.8s (Veo) /
    ~12.7s (Kling) eligibility ceiling and silently degrades to an image, which
    is exactly why 'Semua klip' was producing stills. Returns words such that a
    scene's narration fits a real clip with the 0.85 gate's margin."""
    mode = (visual_mode or "").lower().replace("-", "_")
    if mode not in ("full_clips", "hybrid"):
        return WORDS_PER_SCENE
    cfg = CLIP_MODELS[_normalize_clip_model(clip_model)]
    ceiling = cfg["max_s"] * cfg["gate"]                      # eligible seconds
    target = max((a for a in cfg["allowed"] if a <= ceiling), default=min(cfg["allowed"]))
    return max(6, round(target * WORDS_PER_MINUTE / 60))


def tier_credits(scene_count: int) -> dict[str, int]:
    """Planning credits for every tier, given a scene count."""
    return {tier: scene_count * rate for tier, rate in TIER_CREDITS_PER_SCENE.items()}


def _batch_plan(scene_count: int, batch_size: int = BATCH_SIZE) -> list[int]:
    """How many scenes per dispatch batch. [scene_count] when full-parallel."""
    if scene_count <= batch_size:
        return [scene_count]
    full, rem = divmod(scene_count, batch_size)
    plan = [batch_size] * full
    if rem:
        plan.append(rem)
    return plan


def calculate_video_params(minutes: float, tier: str = "hd",
                           visual_mode: str = "full_images",
                           clip_model: str = DEFAULT_CLIP_MODEL) -> VideoParams:
    """The central formula. From a target duration, derive everything downstream
    reads: scene count, words per scene, dispatch mode, batch plan, progress UI,
    and the planning credit estimate. When `visual_mode` wants clips the scenes
    are sized DOWN to the clip length so they're actually clip-eligible."""
    if minutes is None or minutes <= 0:
        raise ValueError("minutes must be > 0")
    tier = _normalize_tier(tier)
    target_words = round(minutes * WORDS_PER_MINUTE)
    scene_count = scene_count_for_words(target_words, words_per_scene_for(visual_mode, clip_model))
    words_per_scene = max(1, round(target_words / scene_count))
    seconds_per_scene = round(words_per_scene / WORDS_PER_MINUTE * 60, 2)
    by_tier = tier_credits(scene_count)
    return VideoParams(
        minutes=float(minutes),
        target_words=target_words,
        scene_count=scene_count,
        words_per_scene=words_per_scene,
        seconds_per_scene=seconds_per_scene,
        batch_size=BATCH_SIZE,
        dispatch_mode="full_parallel" if scene_count <= BATCH_SIZE else "batch",
        batch_plan=_batch_plan(scene_count),
        progress_ui="cards" if scene_count <= PROGRESS_CARD_LIMIT else "bar",
        tier=tier,
        credits=by_tier[tier],
        credits_by_tier=by_tier,
    )


def estimate_seconds(words: int) -> float:
    """Narration seconds estimate from word count — the only timing available
    before TTS runs. `est_s = words / 130 * 60`."""
    return round(max(0, words) / WORDS_PER_MINUTE * 60, 2)


# ══════════════════════════════════════════════════════════════════════════════
# Clip eligibility (forward-looking — the Step 6d gate reads these)
# ══════════════════════════════════════════════════════════════════════════════
def clip_fits(est_s: float, clip_model: str = DEFAULT_CLIP_MODEL) -> bool:
    """Hard fit gate: a scene is clip-eligible only if its estimated narration
    fits the chosen model's ceiling x gate (the 15% margin absorbs word→TTS
    drift). Veo3 ≈ 6.8s, Kling3 ≈ 12.7s."""
    cfg = CLIP_MODELS[_normalize_clip_model(clip_model)]
    return est_s <= cfg["max_s"] * cfg["gate"]


def suggested_clip_length(est_s: float, clip_model: str = DEFAULT_CLIP_MODEL) -> Optional[int]:
    """The smallest allowed clip length on the chosen model that is >= the
    estimated narration. None if the scene doesn't fit (→ it becomes an image).
    NOTE: this is the fit-driven floor; the merit bump (high-importance scenes
    request one step longer) is the Step 6d ranker's job, kept separate here."""
    if not clip_fits(est_s, clip_model):
        return None
    cfg = CLIP_MODELS[_normalize_clip_model(clip_model)]
    for length in sorted(cfg["allowed"]):
        if length >= est_s:
            return length
    return cfg["allowed"][-1] if cfg["allowed"] else None


def _bump_clip_length(suggested: Optional[int], clip_model: str) -> Optional[int]:
    """Nudge a clip one allowed step longer (high-merit scenes earn more motion).
    Eligibility is about FIT; requested length is about EMPHASIS — kept separate."""
    if suggested is None:
        return None
    allowed = sorted(CLIP_MODELS[_normalize_clip_model(clip_model)]["allowed"])
    for length in allowed:
        if length > suggested:
            return length
    return suggested


# ══════════════════════════════════════════════════════════════════════════════
# The DECIDE stage (Step 6d) — full-clips / full-images / hybrid
# ══════════════════════════════════════════════════════════════════════════════
VISUAL_MODES = ("full_clips", "full_images", "hybrid")


def _heuristic_merit(scenes: list[dict]) -> list[float]:
    """Deterministic cinematic-merit fallback when no LLM scores are supplied:
    short, punchy, action-bearing scenes earn motion; long narration-led ones
    don't. Mirrors the aesthetic the /chat/once ranker is asked to produce."""
    scores = []
    n = len(scenes)
    for i, s in enumerate(scenes):
        wc = int(s.get("word_count") or 0)
        score = 50.0
        score += max(-25.0, min(30.0, 45 - wc))         # shorter ⇒ more clip-worthy
        vp = (s.get("visual_prompt") or "").lower()
        if any(v in vp for v in ("run", "move", "fly", "rush", "berlari", "bergerak", "melaju")):
            score += 12.0
        if s.get("position") in ("opening", "closing"):
            score += 6.0                                  # bookends benefit from motion
        scores.append(score)
    return scores


def decide_visual_modes(scenes: list[dict], visual_mode: str = "hybrid",
                        clip_model: str = DEFAULT_CLIP_MODEL, clip_ratio: float = 0.3,
                        merit_scores: Optional[list[float]] = None) -> list[dict]:
    """Assign each scene a final visual `kind` (clip | image) — the layer between
    segmentation and dispatch. Built CONSTRAINT-THEN-RANK:

      1. Fit gate (hard): a scene is clip-eligible only if its narration fits the
         chosen model's ceiling × 0.85. Non-fitting scenes are images, always.
      2. Mode:
         - full_images : every scene → image (no constraint).
         - full_clips  : every fit-eligible scene → clip; the rest → image.
         - hybrid      : among fit-eligible scenes, the top `clip_ratio` of ALL
                         scenes (by merit) → clip; everyone else → image.
      3. Requested length: clips snap to the smallest allowed ≥ est; high-merit
         hybrid picks are bumped one step longer.

    `merit_scores` (aligned to scenes, higher = more clip-worthy) is injected so
    this stays pure/testable; the route supplies /chat/once scores, else the
    deterministic heuristic runs. Returns NEW scene dicts.
    """
    mode = (visual_mode or "hybrid").lower().replace("-", "_")
    cm = _normalize_clip_model(clip_model)
    out = []
    for s in scenes:
        s = dict(s)
        est = s.get("est_seconds")
        try:
            est = float(est) if est is not None and est != "" else None
        except (TypeError, ValueError):
            est = None
        if est is None:
            est = estimate_seconds(int(s.get("word_count") or 0))
        s["est_seconds"] = est
        s["clip_eligible"] = clip_fits(est, cm)
        s["suggested_clip_seconds"] = suggested_clip_length(est, cm)
        out.append(s)

    if mode == "full_images":
        for s in out:
            s["kind"] = "image"
            s["suggested_clip_seconds"] = None
        return out

    if mode == "full_clips":
        # The user EXPLICITLY asked for all clips — honor it. A scene that slightly
        # overshoots the fit gate still becomes a clip (request the longest allowed
        # length; the stitcher trims/pads to the measured audio). The worker's
        # clip→image fallback covers any that genuinely fail to generate.
        longest = max(CLIP_MODELS[cm]["allowed"])
        for s in out:
            s["kind"] = "clip"
            if not s["suggested_clip_seconds"]:
                s["suggested_clip_seconds"] = longest
        return out

    # hybrid: rank the fit-eligible scenes, promote the top share to clips
    eligible = [i for i, s in enumerate(out) if s["clip_eligible"]]
    ratio = max(0.0, min(1.0, float(clip_ratio)))
    k = min(len(eligible), round(len(out) * ratio))
    scores = merit_scores if merit_scores is not None else _heuristic_merit(out)
    # stable: rank eligible by score desc, tie-break by original order
    ranked = sorted(eligible, key=lambda i: (-float(scores[i]), i))
    chosen = set(ranked[:k])
    for i, s in enumerate(out):
        if i in chosen:
            s["kind"] = "clip"
            s["suggested_clip_seconds"] = _bump_clip_length(s["suggested_clip_seconds"], cm)
        else:
            s["kind"] = "image"
    return out


# ══════════════════════════════════════════════════════════════════════════════
# Sentence segmentation + balanced partitioning
# ══════════════════════════════════════════════════════════════════════════════
_NLP = None
_NLP_TRIED = False
# split after a sentence terminator (incl. Indonesian/ellipsis) + optional closing
# quote/bracket, followed by whitespace. Dependency-free fallback when no spaCy.
_SENT_RE = re.compile(r'(?<=[.!?…])["”’\'\)\]]*\s+')


def _get_nlp():
    """Load a spaCy pipeline once if available; otherwise stay None forever.
    Prefer the multilingual `xx_ent_wiki_sm` (covers Indonesian), then English."""
    global _NLP, _NLP_TRIED
    if _NLP_TRIED:
        return _NLP
    _NLP_TRIED = True
    try:
        import spacy  # type: ignore
        for model in ("xx_ent_wiki_sm", "en_core_web_sm"):
            try:
                _NLP = spacy.load(model)
                break
            except Exception:
                continue
        if _NLP is not None and "sentencizer" not in _NLP.pipe_names and not _NLP.has_pipe("parser"):
            _NLP.add_pipe("sentencizer")
    except Exception:
        _NLP = None
    return _NLP


def _split_sentences(text: str) -> list[str]:
    """Split narration into sentences. Uses spaCy if present, else a regex that
    handles `. ! ? …` plus trailing quotes/brackets."""
    text = (text or "").strip()
    if not text:
        return []
    nlp = _get_nlp()
    if nlp is not None:
        try:
            sents = [s.text.strip() for s in nlp(text).sents if s.text.strip()]
            if sents:
                return sents
        except Exception:
            pass
    parts = [p.strip() for p in _SENT_RE.split(text) if p.strip()]
    return parts or [text]


def _word_count(text: str) -> int:
    return len(text.split())


def _chunk_by_words(text: str, k: int) -> list[str]:
    """Fallback when there are fewer sentences than scenes: split the raw words
    into exactly k contiguous, balanced chunks."""
    words = text.split()
    n = len(words)
    k = max(1, min(k, n)) if n else 1
    if n == 0:
        return [""]
    base, extra = divmod(n, k)
    chunks, i = [], 0
    for g in range(k):
        size = base + (1 if g < extra else 0)
        chunks.append(" ".join(words[i:i + size]))
        i += size
    return chunks


def _partition_sentences(sentences: list[str], k: int) -> list[str]:
    """Partition contiguous sentences into exactly k balanced groups (each
    non-empty), targeting equal word counts. Deterministic; keeps sentence
    boundaries intact so a scene never starts mid-sentence."""
    n = len(sentences)
    if n == 0:
        return []
    k = max(1, min(k, n))
    words = [max(1, _word_count(s)) for s in sentences]
    total = sum(words)
    groups: list[str] = []
    start = 0
    consumed = 0
    for g in range(k):
        target = (g + 1) * total / k          # cumulative word boundary for group g
        end = start
        cur = 0
        groups_left_after = k - g - 1          # groups still to open after this one
        while end < n:
            cur += words[end]
            end += 1
            # close once we've passed the proportional boundary AND still leave
            # at least one sentence for every remaining group...
            if (consumed + cur) >= target and (n - end) >= groups_left_after:
                break
            # ...or close early if we'd otherwise starve the remaining groups.
            if (n - end) <= groups_left_after:
                break
        groups.append(" ".join(sentences[start:end]).strip())
        consumed += cur
        start = end
    if start < n:  # safety: fold any remainder into the last group
        groups[-1] = (groups[-1] + " " + " ".join(sentences[start:])).strip()
    return groups


# ══════════════════════════════════════════════════════════════════════════════
# Visual-prompt construction (spaCy-optional NER + heuristic fallback)
# ══════════════════════════════════════════════════════════════════════════════
_STOPWORDS = {
    # tiny ID+EN stoplist for the heuristic subject/action picks
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "at", "by",
    "for", "with", "as", "is", "was", "were", "are", "be", "been", "it", "its",
    "this", "that", "these", "those", "from", "into", "yang", "dan", "di", "ke",
    "dari", "itu", "ini", "para", "pada", "untuk", "dengan", "adalah", "sebuah",
    "seorang", "akan", "tidak", "juga", "atau", "mereka", "kita", "kami",
}
_PREP_SETTING = re.compile(
    r'\b(?:di|ke|dari|in|on|at|inside|within|across|near|atop|beneath)\s+'
    r'((?:[a-z]+\s+){0,1}(?:[A-Z][\w’\'-]+(?:\s+[A-Z][\w’\'-]+)*|[a-z][\w’\'-]+))',
)
_VERB_HINT = re.compile(
    r'\b(\w+(?:ed|ing|kan|nya))\b|\b(mem\w+|ber\w+|meng\w+|men\w+)\b', re.UNICODE
)


def _extract_entities(text: str) -> dict[str, str]:
    """Pull subject / setting / action from a scene's text. spaCy NER + POS when
    available; otherwise a dependency-free heuristic. Always returns the three
    keys (values may be '')."""
    text = (text or "").strip()
    if not text:
        return {"subject": "", "setting": "", "action": ""}

    nlp = _get_nlp()
    if nlp is not None:
        try:
            doc = nlp(text)
            persons, places = [], []
            for ent in doc.ents:
                if ent.label_ in ("PERSON", "PER", "ORG"):
                    persons.append(ent.text)
                elif ent.label_ in ("GPE", "LOC", "FAC"):
                    places.append(ent.text)
            action = ""
            for tok in doc:
                if getattr(tok, "pos_", "") == "VERB":
                    action = tok.lemma_ or tok.text
                    break
            subject = persons[0] if persons else _heuristic_subject(text)
            setting = places[0] if places else _heuristic_setting(text)
            return {
                "subject": subject,
                "setting": setting,
                "action": action or _heuristic_action(text),
            }
        except Exception:
            pass

    return {
        "subject": _heuristic_subject(text),
        "setting": _heuristic_setting(text),
        "action": _heuristic_action(text),
    }


def _heuristic_subject(text: str) -> str:
    """Prefer a multi-word Proper-noun phrase; else the first content noun-ish
    word in the first sentence."""
    first = _split_sentences(text)[0] if text else ""
    caps = re.findall(r'\b([A-Z][\w’\'-]+(?:\s+[A-Z][\w’\'-]+){0,2})\b', first)
    if caps:
        return caps[0]
    for w in first.split():
        cw = re.sub(r'[^\w’\'-]', '', w)
        if cw and cw.lower() not in _STOPWORDS and len(cw) > 2:
            return cw
    return ""


def _heuristic_setting(text: str) -> str:
    m = _PREP_SETTING.search(text)
    return m.group(1).strip() if m else ""


def _heuristic_action(text: str) -> str:
    m = _VERB_HINT.search(text.lower())
    if m:
        return next((g for g in m.groups() if g), "")
    return ""


# Visual ART STYLE → a render-style suffix appended to every scene's image/clip
# prompt. Distinct from `style` (the NARRATION tone via STYLE_TONE). The empty/
# unknown key leaves the prompt as-is (the model's default look).
VISUAL_STYLE: dict[str, str] = {
    "cinematic":      "cinematic film still, dramatic lighting, shallow depth of field, color-graded, 35mm",
    "photorealistic": "photorealistic, ultra-detailed, natural lighting, high dynamic range",
    "caricature":     "caricature illustration, exaggerated playful features, bold lines, vibrant",
    "comic":          "comic book art, bold ink outlines, halftone shading, dynamic",
    "manga":          "black-and-white manga style, screentones, expressive linework",
    "anime":          "anime style, cel shading, vivid colors, expressive, studio-quality",
    "watercolor":     "watercolor painting, soft washes, bleeding pigments, paper texture",
    "oil_painting":   "oil painting, visible brushstrokes, rich impasto, classical lighting",
    "gouache":        "gouache illustration, matte opaque colors, flat painterly shapes",
    "charcoal":       "charcoal sketch, smudged shading, dramatic monochrome, hand-drawn",
    "pencil_sketch":  "detailed pencil sketch, fine hatching, graphite, hand-drawn",
    "line_art":       "clean line art, single-weight outlines, minimal, flat",
    "3d_render":      "3D render, octane, soft global illumination, subsurface scattering, detailed",
    "pixar":          "stylized 3D animation, soft rounded forms, warm cinematic light, family-film",
    "claymation":     "claymation, handmade clay texture, stop-motion, tactile",
    "low_poly":       "low-poly 3D, faceted geometry, flat-shaded, minimal palette",
    "pixel_art":      "pixel art, 16-bit, crisp dithering, limited palette",
    "isometric":      "isometric illustration, clean vector, soft shadows, 2.5D",
    "flat_vector":    "flat vector illustration, bold shapes, minimal gradients, modern",
    "pop_art":        "pop art, Ben-Day dots, bold saturated colors, high contrast",
    "noir":           "film noir, high-contrast black and white, dramatic shadows, moody",
    "cyberpunk":      "cyberpunk, neon-lit, rain-slick streets, holographic, high-tech dystopia",
    "steampunk":      "steampunk, brass and gears, Victorian, warm sepia, intricate machinery",
    "vaporwave":      "vaporwave, pastel neon, retro 80s, glitch, dreamy",
    "ukiyo_e":        "ukiyo-e woodblock print, flat color, bold outlines, Japanese classical",
    "impressionist":  "impressionist painting, loose dappled brushwork, luminous color",
    "storybook":      "children's storybook illustration, whimsical, soft warm palette, hand-painted",
    "papercut":       "layered papercut art, soft drop shadows, tactile cut-paper depth",
    "fantasy_art":    "epic fantasy concept art, dramatic, painterly, rich detail",
    "minimalist":     "minimalist, negative space, simple shapes, restrained palette",
}


def build_visual_prompt(text: str, style: str = "", position: str = "middle",
                        visual_style: str = "") -> str:
    """Compose a cinematography prompt: subject + action + setting, coloured by
    the narration style's tone and the scene's position framing, then an optional
    ART-STYLE suffix (caricature/comic/cinematic/…). Capped for the image/clip API."""
    ents = _extract_entities(text)
    tone = STYLE_TONE.get(_normalize_style(style), DEFAULT_TONE)
    framing = _POSITION_FRAMING.get(position, _POSITION_FRAMING["middle"])

    core_bits = [b for b in (ents["subject"], ents["action"], ents["setting"]) if b]
    if core_bits:
        core = ", ".join(core_bits)
    else:
        # nothing extracted (very short / abstract line) → summarise the opening
        core = " ".join(text.split()[:8])

    # art style goes right after the core so the cap (which trims from the end)
    # never drops it — the look matters more than the tail framing words.
    vstyle = VISUAL_STYLE.get(_normalize_style(visual_style)) if visual_style else None
    head = f"{core}, {vstyle}" if vstyle else core
    return _cap_prompt(f"{head} — {framing}, {tone}")


def _cap_prompt(prompt: str) -> str:
    prompt = re.sub(r'\s+', ' ', prompt).strip(" ,—-")
    words = prompt.split()
    if len(words) > MAX_VISUAL_PROMPT_WORDS:
        prompt = " ".join(words[:MAX_VISUAL_PROMPT_WORDS])
    if len(prompt) > MAX_VISUAL_PROMPT_CHARS:
        prompt = prompt[:MAX_VISUAL_PROMPT_CHARS].rsplit(" ", 1)[0]
    return prompt


def _position_for(index: int, count: int) -> str:
    if index == 0:
        return "opening"
    if index == count - 1:
        return "closing"
    return "middle"


# Output-language code → the name the narration model is told to write in. Covers
# major Indonesian regional languages (the product's "Suara Lokal" moat) + a few
# globals. Unknown codes default to Bahasa Indonesia.
LANGUAGE_NAMES: dict[str, str] = {
    "id": "Bahasa Indonesia", "jv": "Bahasa Jawa (Javanese)", "su": "Bahasa Sunda (Sundanese)",
    "min": "Bahasa Minang", "ban": "Bahasa Bali (Balinese)", "bug": "Bahasa Bugis",
    "btk": "Bahasa Batak", "ms": "Bahasa Melayu", "en": "English", "ar": "Arabic",
    "zh": "Chinese (Mandarin)", "ja": "Japanese", "ko": "Korean",
}


# ══════════════════════════════════════════════════════════════════════════════
# Mode A — generation prompt (the caller runs the LLM; the segmenter stays pure)
# ══════════════════════════════════════════════════════════════════════════════
def build_generation_prompt(topic: str, target_words: int, style: str = "",
                            language: str = "id") -> str:
    """Mode A: instruct the narration model to write ~`target_words` of narration
    on `topic`. The caller feeds this to the existing narration generator
    (which layers STYLE_RULES on top); the returned text is then passed to
    `segment()`. Kept here so the word-count target lives with the formula."""
    lang = LANGUAGE_NAMES.get((language or "id").strip().lower(), "Bahasa Indonesia")
    style_clause = f" in the '{style}' style" if style else ""
    return (
        f"Write documentary voiceover narration{style_clause} about: {topic}.\n"
        f"Language: {lang}. Target length: about {target_words} words "
        f"(~{target_words / WORDS_PER_MINUTE:.1f} minutes at {WORDS_PER_MINUTE} wpm).\n"
        f"Write flowing spoken narration in complete sentences — no headings, no "
        f"scene labels, no stage directions. Pace it for the ear."
    )


# ══════════════════════════════════════════════════════════════════════════════
# The segmenter — narration → scene objects
# ══════════════════════════════════════════════════════════════════════════════
def segment(text: str, *, mode: str = "B", minutes: Optional[float] = None,
            style: str = "", clip_model: str = DEFAULT_CLIP_MODEL,
            tier: str = "hd", visual_mode: str = "full_images",
            visual_style: str = "") -> SegmentResult:
    """Cut narration into timed scene objects.

    Mode A: pass the freshly generated narration and the `minutes` it targeted;
            we segment into exactly `scene_count` scenes.
    Mode B: pass existing narration (minutes optional); we NEVER truncate — the
            scene count is derived from the text's true word count and the real
            length is reported back.
    """
    mode = (mode or "B").strip().upper()
    text = (text or "").strip()
    actual_words = _word_count(text)
    actual_minutes = round(actual_words / WORDS_PER_MINUTE, 2)

    if mode == "A":
        if minutes is None or minutes <= 0:
            raise ValueError("Mode A requires the target `minutes`")
        params = calculate_video_params(minutes, tier, visual_mode, clip_model)
        note = (f"Mode A: targeted {params.target_words} words "
                f"(~{minutes:.1f} min); generated {actual_words} words.")
    else:
        # Mode B: the existing narration IS the truth. Plan from its real length.
        eff_minutes = actual_minutes if actual_words else (minutes or 0)
        params = calculate_video_params(max(eff_minutes, 1 / WORDS_PER_MINUTE), tier,
                                        visual_mode, clip_model)
        # re-anchor the reported target on the real text, never on a request
        params.target_words = actual_words
        params.minutes = actual_minutes
        params.words_per_scene = max(1, round(actual_words / params.scene_count)) if actual_words else 0
        params.seconds_per_scene = round(params.words_per_scene / WORDS_PER_MINUTE * 60, 2)
        note = (f"Mode B: existing narration is {actual_words} words "
                f"(~{actual_minutes:.1f} min) → {params.scene_count} scenes. "
                f"Not truncated.")

    scenes = _build_scenes(text, params.scene_count, style, clip_model, visual_style)
    # a scene partition can yield fewer groups than asked only when the text has
    # fewer sentences/words than scenes — keep params honest if so.
    if scenes and len(scenes) != params.scene_count:
        params.scene_count = len(scenes)
        params.batch_plan = _batch_plan(params.scene_count)
        params.dispatch_mode = "full_parallel" if params.scene_count <= BATCH_SIZE else "batch"
        params.progress_ui = "cards" if params.scene_count <= PROGRESS_CARD_LIMIT else "bar"
        params.credits_by_tier = tier_credits(params.scene_count)
        params.credits = params.credits_by_tier[params.tier]

    return SegmentResult(
        mode="A" if mode == "A" else "B",
        params=params,
        scenes=scenes,
        actual_words=actual_words,
        actual_minutes=actual_minutes,
        truncated=False,
        note=note,
    )


def _build_scenes(text: str, scene_count: int, style: str,
                  clip_model: str, visual_style: str = "") -> list[Scene]:
    if not text:
        return []
    sentences = _split_sentences(text)
    if len(sentences) >= scene_count:
        chunks = _partition_sentences(sentences, scene_count)
    else:
        chunks = _chunk_by_words(text, scene_count)
    chunks = [c for c in chunks if c.strip()]

    scenes: list[Scene] = []
    count = len(chunks)
    for i, chunk in enumerate(chunks):
        wc = _word_count(chunk)
        est_s = estimate_seconds(wc)
        position = _position_for(i, count)
        scenes.append(Scene(
            number=i + 1,
            text=chunk,
            word_count=wc,
            est_seconds=est_s,
            position=position,
            visual_prompt=build_visual_prompt(chunk, style, position, visual_style),
            clip_eligible=clip_fits(est_s, clip_model),
            suggested_clip_seconds=suggested_clip_length(est_s, clip_model),
        ))
    return scenes


# ══════════════════════════════════════════════════════════════════════════════
# The Duration Presets contract — `--all-durations`
# ══════════════════════════════════════════════════════════════════════════════
# Canonical preset durations from the video-assembly roadmap (in minutes).
PRESET_MINUTES = [0.5, 1, 2, 3, 5, 10, 15]
PRESET_LABELS = {0.5: "30 sec", 1: "1 min", 2: "2 min", 3: "3 min",
                 5: "5 min", 10: "10 min", 15: "15 min"}

# The published table, as the contract to verify against. credits are the
# formula's (scenes x {2,5,8}); the doc's 1-min HD/HD+ cells (18/28) are a typo.
_EXPECTED_PRESETS = {
    #  minutes: (words, scenes)
    0.5: (65, 2),
    1:   (130, 3),
    2:   (260, 6),
    3:   (390, 9),
    5:   (650, 14),
    10:  (1300, 29),
    15:  (1950, 43),
}


def duration_table() -> list[dict]:
    """Build the duration-presets table the UI and the dispatcher share."""
    rows = []
    for m in PRESET_MINUTES:
        p = calculate_video_params(m, "hd")
        rows.append({
            "label": PRESET_LABELS[m],
            "minutes": m,
            "words": p.target_words,
            "scenes": p.scene_count,
            "dispatch": "Full parallel" if p.dispatch_mode == "full_parallel"
                        else "Batch " + "+".join(str(b) for b in p.batch_plan),
            "progress_ui": "scene cards" if p.progress_ui == "cards" else "progress bar",
            "credits": p.credits_by_tier,   # {fast, hd, hd_plus}
        })
    return rows


def verify_contract() -> list[str]:
    """Assert the formula reproduces the published presets. Returns a list of
    mismatch strings (empty == contract holds)."""
    problems = []
    for m, (exp_words, exp_scenes) in _EXPECTED_PRESETS.items():
        p = calculate_video_params(m, "hd")
        if p.target_words != exp_words:
            problems.append(f"{PRESET_LABELS[m]}: words {p.target_words} != {exp_words}")
        if p.scene_count != exp_scenes:
            problems.append(f"{PRESET_LABELS[m]}: scenes {p.scene_count} != {exp_scenes}")
        # credits must equal scenes x {2,5,8}
        exp_credits = {t: exp_scenes * r for t, r in TIER_CREDITS_PER_SCENE.items()}
        if p.credits_by_tier != exp_credits:
            problems.append(f"{PRESET_LABELS[m]}: credits {p.credits_by_tier} != {exp_credits}")
    return problems


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════
def _print_all_durations() -> None:
    rows = duration_table()
    print(f"{'Duration':10} {'Words':>6} {'Scenes':>7} {'Dispatch':16} "
          f"{'Progress UI':14} {'Credits F/HD/HD+':>18}")
    print("-" * 78)
    for r in rows:
        c = r["credits"]
        credits = f"{c['fast']}/{c['hd']}/{c['hd_plus']}"
        print(f"{r['label']:10} {r['words']:>6} {r['scenes']:>7} {r['dispatch']:16} "
              f"{r['progress_ui']:14} {credits:>18}")
    print(f"\nNER backend: {'spaCy' if _get_nlp() is not None else 'heuristic fallback (spaCy not installed)'}")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Step 6 scene segmenter — the central controller of the video engine.")
    ap.add_argument("--all-durations", action="store_true",
                    help="print the Duration Presets contract table and exit")
    ap.add_argument("--self-test", action="store_true",
                    help="verify the formula reproduces the published presets")
    ap.add_argument("--minutes", type=float, help="target duration in minutes")
    ap.add_argument("--tier", default="hd", help="fast | hd | hd_plus (default hd)")
    ap.add_argument("--text", help="Mode B: segment this existing narration")
    ap.add_argument("--mode", default="B", help="A (topic-first) or B (narration-first)")
    ap.add_argument("--style", default="", help="narration style → cinematography tone")
    ap.add_argument("--clip-model", default=DEFAULT_CLIP_MODEL, help="veo3 | kling3")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args(argv)

    if args.self_test:
        problems = verify_contract()
        if problems:
            print("CONTRACT VIOLATIONS:")
            for p in problems:
                print("  -", p)
            return 1
        print("OK — formula reproduces all published presets (scenes x {2,5,8}).")
        return 0

    if args.all_durations:
        if args.json:
            print(json.dumps(duration_table(), indent=2, ensure_ascii=False))
        else:
            _print_all_durations()
        return 0

    if args.text is not None:
        result = segment(args.text, mode=args.mode, minutes=args.minutes,
                         style=args.style, clip_model=args.clip_model, tier=args.tier)
        if args.json:
            print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        else:
            print(result.note)
            print(f"params: {result.params.scene_count} scenes, "
                  f"dispatch={result.params.dispatch_mode}, ui={result.params.progress_ui}, "
                  f"credits({result.params.tier})={result.params.credits}\n")
            for s in result.scenes:
                flag = f"clip<={s.suggested_clip_seconds}s" if s.clip_eligible else "image"
                print(f"  [{s.number:>2}] {s.position:7} {s.word_count:>3}w "
                      f"{s.est_seconds:>5.1f}s  {flag:11} | {s.visual_prompt}")
        return 0

    if args.minutes is not None:
        p = calculate_video_params(args.minutes, args.tier)
        print(json.dumps(asdict(p), indent=2, ensure_ascii=False) if args.json else (
            f"{args.minutes} min → {p.target_words} words, {p.scene_count} scenes "
            f"(~{p.words_per_scene}w/{p.seconds_per_scene}s each)\n"
            f"dispatch={p.dispatch_mode} {p.batch_plan}  ui={p.progress_ui}\n"
            f"credits: fast={p.credits_by_tier['fast']} hd={p.credits_by_tier['hd']} "
            f"hd_plus={p.credits_by_tier['hd_plus']}"))
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
