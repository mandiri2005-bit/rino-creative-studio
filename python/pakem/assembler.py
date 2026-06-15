# -*- coding: utf-8 -*-
"""pakem.assembler — the cache-aware narration prompt assembler (Project Dalang WS-4).

This is the ONE place that turns (style, language, mode, outline, brief, chapter,
prev_tail, rag_passages) into the chat `messages` we send upstream. It deliberately
inverts the ordering used by the legacy chapter assembly in
`laozhang_api._narasi_generate_impl` (per the repo map, that path PREPENDS the
per-chapter rag_context + prev_context to the prompt, so the variable part sits at
the FRONT). Here the STATIC, reusable material goes FIRST (in a `system` block) and
the per-chapter VARIABLE material goes LAST (in a `user` block).

WHY (the whole point of WS-4):
  * A stable, byte-for-byte-identical prefix across every chapter of a job lets the
    upstream provider serve a prompt PREFIX CACHE. With Anthropic models we mark the
    system block with `cache_control` (ephemeral); with OpenAI-style providers the
    implicit automatic prefix cache kicks in for the identical leading bytes. Either
    way the long, expensive style/factual/outline preamble is paid for once.
  * Even when the laozhang proxy ignores cache hints entirely, putting the static
    content first is a NO-REGRET reorder: it never hurts quality and sets us up to
    benefit the moment caching is honoured. The dynamic, chapter-specific scope lands
    closest to the generation boundary, which is also where models attend hardest.

The static prefix is composed PURELY from pakem (WS-2): GENERATION_PREAMBLE +
style_rules_core (+ VIDEO_MODIFIER in video mode) + FACTUAL_INTEGRITY (non-fiction
only) + LANGUAGE_DIRECTIVE + the NARRATIVE BRIEF + the FULL outline + the per-style
RAG framing. It depends only on (style, language, mode, brief, outline) — NOT on the
chapter index, prev_tail, or retrieved passages — so it is identical for every chapter
of a job. `compose()` asserts this invariant indirectly; `smoke_assembler.py` asserts
it directly (byte-for-byte across N chapters).

Pure-ish module: imports only pakem (pure data) and orchestrator telemetry/token
helpers (which themselves fall back to a self-contained client when laozhang_api is
absent). No network, no DB. Safe to import anywhere.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence, Union

from . import (
    PAKEM_VERSION,
    FACTUAL_INTEGRITY,
    GENERATION_PREAMBLE,
    LANGUAGE_DIRECTIVE,
    build_style_block,
    resolve_language,
    resolve_style,
)

# Reuse WS-1's model-aware token ceiling + cost estimator + telemetry record so the
# assembler's budgeter agrees with the orchestrator that actually makes the calls.
# Soft-import: the assembler must still build prompts even if orchestrator can't load
# (e.g. running the pakem package in pure isolation for an eval).
try:  # pragma: no cover - exercised indirectly
    from orchestrator import (  # type: ignore
        CallTelemetry,
        TelemetrySink,
        estimate_cost,
        max_tokens_for,
    )
    _ORCH_OK = True
except Exception:  # noqa: BLE001 - assembler works standalone
    _ORCH_OK = False
    CallTelemetry = None  # type: ignore[assignment]
    TelemetrySink = None  # type: ignore[assignment]

    def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:  # type: ignore[misc]
        return 0.0

    # Minimal model-token fallback mirroring laozhang_api.DEFAULT_MAX_TOKENS.
    _FALLBACK_MAX_TOKENS = 16384

    def max_tokens_for(model: str) -> int:  # type: ignore[misc]
        return _FALLBACK_MAX_TOKENS


# ---------------------------------------------------------------------------
# Token heuristics — no tiktoken dependency. We mirror the codebase's own
# WORDS_TO_TOKENS_NARASI = 1.5 (laozhang_api.py:2833) for the word path and a
# chars/4 floor for non-Latin scripts, then take the larger of the two so we
# never UNDER-count a budget (under-counting would let prev_tail blow the window).
# ---------------------------------------------------------------------------
WORDS_TO_TOKENS = 1.5
_CHARS_PER_TOKEN = 4.0

# Reserve for the per-chapter scope text + RAG passages that share the user block
# with prev_tail. prev_tail is trimmed to (budget - this reserve) so the dynamic
# block as a whole stays under the input budget. Tunable; deliberately generous.
_USER_OVERHEAD_TOKENS = 600


def estimate_tokens(text: str) -> int:
    """Heuristic token count for a string. Conservative (rounds UP).

    max(word-based, char-based) so neither a Latin word-heavy text nor a CJK
    char-heavy text is undercounted. Used only for budgeting prev_tail; the real
    token accounting comes from the provider's usage object via CallTelemetry.
    """
    if not text:
        return 0
    words = len(text.split())
    by_words = int(words * WORDS_TO_TOKENS + 0.9999)
    by_chars = int(len(text) / _CHARS_PER_TOKEN + 0.9999)
    return max(by_words, by_chars, 1)


def trim_to_token_budget(text: str, max_tokens: int, *, keep: str = "tail") -> str:
    """Trim `text` so its estimated tokens fit `max_tokens`.

    For prev_tail ("story so far") the END is what matters most — the immediately
    preceding chapter — so we keep the TAIL by default and drop from the front,
    snapping to a paragraph/sentence boundary so we never cut mid-word. Returns the
    text unchanged if it already fits or if max_tokens <= 0 disables trimming-by-budget.
    """
    if max_tokens <= 0 or not text:
        return text
    if estimate_tokens(text) <= max_tokens:
        return text

    # Convert the token budget back to an approximate char budget (use the looser
    # chars/token so we keep as much as fits) then snap to a clean boundary.
    char_budget = int(max_tokens * _CHARS_PER_TOKEN)
    if char_budget <= 0:
        return ""

    if keep == "tail":
        cut = text[-char_budget:]
        # Snap forward to the start of the next paragraph, else next sentence,
        # else next word — so the kept fragment begins cleanly.
        for sep in ("\n\n", "\n", ". ", " "):
            idx = cut.find(sep)
            if idx != -1 and idx < len(cut) - 1:
                cut = cut[idx + len(sep):]
                break
        return cut.lstrip()
    # keep == "head"
    cut = text[:char_budget]
    for sep in ("\n\n", "\n", ". ", " "):
        idx = cut.rfind(sep)
        if idx != -1:
            cut = cut[:idx]
            break
    return cut.rstrip()


# ---------------------------------------------------------------------------
# Inputs — light dataclasses so callers don't juggle loose dicts. Both accept a
# plain dict too (see _as_chapter / _as_outline) for ergonomics from the runtime.
# ---------------------------------------------------------------------------
@dataclass
class Chapter:
    """The chapter currently being generated (the VARIABLE scope)."""
    id: str = ""
    title: str = ""
    summary: str = ""
    index: int = 0          # 0-based position in the book
    total: int = 0          # total chapters in the book
    word_target: int = 800
    word_min: int = 0
    word_max: int = 0

    def __post_init__(self) -> None:
        if not self.word_min:
            self.word_min = int(self.word_target * 0.85)
        if not self.word_max:
            self.word_max = int(self.word_target * 1.15)


def _as_chapter(chapter: Union["Chapter", dict, None]) -> Chapter:
    if isinstance(chapter, Chapter):
        return chapter
    if not chapter:
        return Chapter()
    d = dict(chapter)
    return Chapter(
        id=str(d.get("id", "") or ""),
        title=str(d.get("title", "") or ""),
        summary=str(d.get("summary", d.get("description", "")) or ""),
        index=int(d.get("index", 0) or 0),
        total=int(d.get("total", 0) or 0),
        word_target=int(d.get("word_target", d.get("words", 800)) or 800),
        word_min=int(d.get("word_min", 0) or 0),
        word_max=int(d.get("word_max", 0) or 0),
    )


def _outline_text(outline: Any) -> str:
    """Render the FULL outline into a stable string for the static prefix.

    Accepts a pre-rendered string (returned as-is), or a list of chapter dicts
    (rendered as a deterministic numbered list — order preserved, no per-chapter
    state leaks in, so the rendering is identical across chapters of a job).
    """
    if not outline:
        return ""
    if isinstance(outline, str):
        return outline.strip()
    lines = []
    for i, ch in enumerate(outline, 1):
        if isinstance(ch, dict):
            t = str(ch.get("title", "") or "").strip()
            d = str(ch.get("summary", ch.get("description", "")) or "").strip()
            w = ch.get("word_target", ch.get("words"))
            head = f"{i}. {t}" if t else f"{i}."
            if w:
                head += f" ({w} words)"
            lines.append(head + (f" — {d}" if d else ""))
        else:
            lines.append(f"{i}. {str(ch).strip()}")
    return "\n".join(lines)


def _passages_text(rag_passages: Any) -> str:
    """Render retrieved RAG passages into the DYNAMIC block.

    Accepts a pre-formatted string (e.g. rag_narration's `context_text`, returned
    as-is) or a list of passage dicts/strings. Passages are per-chapter retrieval,
    so they belong in the variable user block, NOT the cacheable prefix.
    """
    if not rag_passages:
        return ""
    if isinstance(rag_passages, str):
        return rag_passages.strip()
    chunks = []
    for i, p in enumerate(rag_passages, 1):
        if isinstance(p, dict):
            src = str(p.get("source", p.get("title", "")) or "").strip()
            body = str(p.get("text", p.get("content", p.get("passage", ""))) or "").strip()
            if not body:
                continue
            head = f"[REF {i}{(' — ' + src) if src else ''}]"
            chunks.append(f"{head}\n{body}")
        else:
            s = str(p).strip()
            if s:
                chunks.append(f"[REF {i}]\n{s}")
    return "\n\n".join(chunks)


# ---------------------------------------------------------------------------
# Static prefix builder — the CACHEABLE block. Depends ONLY on style/language/
# mode/brief/outline. MUST NOT reference chapter index, prev_tail, or passages.
# ---------------------------------------------------------------------------
def build_static_prefix(
    *,
    style: Union[str, dict],
    language: str,
    video_mode: bool,
    brief: str,
    outline: Any,
) -> str:
    """Compose the byte-stable system prefix shared by every chapter of a job.

    Order (top → bottom):
      1. GENERATION_PREAMBLE(video_mode)        — 'write for ears' signal in VO mode
      2. style_rules_core (+ VIDEO_MODIFIER)    — the load-bearing craft rules
      3. FACTUAL_INTEGRITY                      — non-fiction ONLY (skipped for fiction)
      4. LANGUAGE_DIRECTIVE(lang_label)         — output-language lock + anti-mirroring
      5. RAG FRAMING                            — per-style 'how to use references'
      6. NARRATIVE BRIEF                        — the book's tone/voice/arc (static)
      7. FULL OUTLINE                           — the whole book's structure (static)
    """
    entry = style if isinstance(style, dict) else resolve_style(style)
    lang_label = resolve_language(language)

    parts: list[str] = []

    preamble = GENERATION_PREAMBLE(video_mode)
    if preamble:
        parts.append(preamble.rstrip())

    parts.append(build_style_block(entry, video_mode=video_mode).rstrip())

    if not entry.get("is_fiction", False):
        parts.append(FACTUAL_INTEGRITY.rstrip())

    parts.append(LANGUAGE_DIRECTIVE(lang_label).rstrip())

    framing = (entry.get("rag") or {}).get("framing", "")
    if framing:
        parts.append("HOW TO USE REFERENCES:\n" + framing.strip())

    if brief and brief.strip():
        parts.append("NARRATIVE BRIEF:\n" + brief.strip())

    outline_str = _outline_text(outline)
    if outline_str:
        parts.append("FULL OUTLINE (the whole book — for cross-chapter consistency):\n" + outline_str)

    # Single newline-join with one blank line between blocks → deterministic bytes.
    return "\n\n".join(p for p in parts if p).strip() + "\n"


# ---------------------------------------------------------------------------
# Dynamic block builder — the VARIABLE user message. Changes every chapter.
# ---------------------------------------------------------------------------
def build_dynamic_block(
    *,
    language: str,
    chapter: Chapter,
    prev_tail: str,
    rag_passages: Any,
) -> str:
    """Compose the per-chapter user message: scope + story-so-far + passages.

    Order (top → bottom):
      1. RETRIEVED REFERENCES   — this chapter's RAG passages (per-chapter retrieval)
      2. STORY SO FAR           — trimmed tail of prior chapters (continuity)
      3. THIS CHAPTER           — title/summary/word target — the actual ask, LAST
    """
    lang_label = resolve_language(language)
    parts: list[str] = []

    passages = _passages_text(rag_passages)
    if passages:
        parts.append(
            "RETRIEVED REFERENCES (context only — study, do not copy or mirror wording):\n"
            + passages
        )

    if prev_tail and prev_tail.strip():
        parts.append("STORY SO FAR (the immediately preceding narration — continue from here, do not repeat it):\n" + prev_tail.strip())

    pos = ""
    if chapter.total:
        pos = f" (chapter {chapter.index + 1} of {chapter.total})"
    scope = [f"THIS CHAPTER{pos}:"]
    if chapter.title:
        scope.append(f"  Title: {chapter.title}")
    if chapter.summary:
        scope.append(f"  Summary: {chapter.summary}")
    scope.append(
        f"  Target: {chapter.word_target} words "
        f"(range {chapter.word_min}–{chapter.word_max})."
    )
    scope.append(
        f"Write EXACTLY about {chapter.word_target} words in {lang_label}. "
        f"Do NOT include the chapter title or number. Return ONLY the chapter body text."
    )
    parts.append("\n".join(scope))

    return "\n\n".join(parts).strip() + "\n"


# ---------------------------------------------------------------------------
# Cache key — PAKEM_VERSION + style + language + job_id. Identifies a stable
# prefix family. Bumping PAKEM_VERSION (any style/asset change) invalidates it.
# ---------------------------------------------------------------------------
def cache_key(*, style: Union[str, dict], language: str, job_id: str) -> str:
    """Deterministic cache key for the static prefix of a job.

    Same job + same style + same language + same pakem version => same key =>
    same prefix bytes => prefix-cache hit. We resolve style/language to their
    canonical forms first so 'creative non-fiction' and a pre-resolved entry map
    to the same key.
    """
    entry = style if isinstance(style, dict) else resolve_style(style)
    skey = entry.get("key") or entry.get("display_name") or str(style)
    lang_label = resolve_language(language)
    raw = f"{PAKEM_VERSION}|{skey}|{lang_label}|{job_id or ''}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"pakem:{PAKEM_VERSION}:{skey}:{digest}"


# ---------------------------------------------------------------------------
# The composed result.
# ---------------------------------------------------------------------------
@dataclass
class Composed:
    """Output of compose(): provider-ready messages + budgeter/telemetry metadata."""
    messages: list[dict[str, Any]]
    static_prefix: str
    dynamic_block: str
    cache_key: str
    model: str
    max_tokens: int                 # output ceiling for this call (budgeter)
    prefix_tokens: int              # estimated tokens in the cacheable prefix
    dynamic_tokens: int             # estimated tokens in the variable block
    input_tokens: int               # estimated total input tokens
    prev_tail_trimmed: bool         # True if prev_tail was cut to fit its budget
    style_key: str
    language_label: str

    def telemetry(self, *, role: str = "worker", task_id: str = "") -> Any:
        """Pre-fill a WS-1 CallTelemetry with this call's input accounting.

        The caller settles tokens_out/cost after the provider responds. Returns
        None if orchestrator isn't importable (telemetry is best-effort).
        """
        if not _ORCH_OK or CallTelemetry is None:  # pragma: no cover
            return None
        return CallTelemetry(
            model=self.model,
            role=role,
            tokens_in=self.input_tokens,
            task_id=task_id or self.cache_key,
        )

    def settle(self, sink: Optional[Any], telem: Any, *,
               tokens_out: int, latency_ms: int = 0,
               finish_reason: str = "", ok: bool = True) -> Any:
        """Finalize a CallTelemetry after the call and emit it to a sink.

        Estimates cost from (input_tokens + tokens_out) via WS-1's estimator and
        invokes the sink (which may write usage_logs). Never raises.
        """
        if telem is None:
            return None
        try:
            telem.tokens_out = int(tokens_out or 0)
            telem.ok = ok
            telem.latency_ms = int(latency_ms or 0)
            telem.finish_reason = finish_reason or ""
            telem.cost_usd = estimate_cost(self.model, telem.tokens_in, telem.tokens_out)
            if sink is not None:
                sink(telem)
        except Exception:  # noqa: BLE001 - telemetry never breaks generation
            pass
        return telem


# ---------------------------------------------------------------------------
# compose() — the public entry point.
# ---------------------------------------------------------------------------
def compose(
    style: Union[str, dict],
    language: str = "id",
    mode: str = "text",
    outline: Any = None,
    brief: str = "",
    chapter: Union[Chapter, dict, None] = None,
    prev_tail: str = "",
    rag_passages: Any = None,
    *,
    job_id: str = "",
    model: Optional[str] = None,
    prev_tail_token_budget: Optional[int] = None,
    cache_control: bool = True,
) -> Composed:
    """Compose provider-ready chat messages with a cache-stable system prefix.

    Returns a `Composed` whose `.messages` is exactly:
        [
          {"role": "system", "content": <STATIC cacheable prefix>,
           # Anthropic-style cache hint on the LAST content block (added when
           # cache_control=True). OpenAI-style providers ignore it and benefit
           # from the implicit identical-prefix cache instead.
           "cache_control": {"type": "ephemeral"}},
          {"role": "user", "content": <DYNAMIC: passages + story-so-far + scope>},
        ]

    Args:
      style:        raw user/legacy style string OR a resolved pakem entry dict.
      language:     language code ("id") or label; resolved via pakem.
      mode:         "video"/"vo"/"voiceover" => VO mode (preamble + VIDEO_MODIFIER);
                    anything else => plain text mode.
      outline:      FULL outline — pre-rendered string OR list of chapter dicts.
      brief:        the book's narrative brief (static across chapters).
      chapter:      the chapter to generate (Chapter or dict).
      prev_tail:    'story so far' — tail of prior chapters; trimmed to budget.
      rag_passages: this chapter's retrieved passages (string or list).
      job_id:       job id — part of the cache key so each job has its own family.
      model:        model alias to budget against (defaults to env worker model).
      prev_tail_token_budget: explicit token cap for prev_tail. If None, derived
                    from the model's output ceiling (so input ~ output budget,
                    minus the user-block overhead reserve).
      cache_control: attach the Anthropic ephemeral cache hint to the system block.
    """
    entry = style if isinstance(style, dict) else resolve_style(style)
    style_key = entry.get("key", "") or ""
    lang_label = resolve_language(language)
    video_mode = str(mode or "").strip().lower() in {"video", "vo", "voiceover", "voice"}
    ch = _as_chapter(chapter)

    # Resolve model + output ceiling (budgeter). Default to env worker model.
    resolved_model = model or _default_model()
    out_ceiling = max_tokens_for(resolved_model)

    # Static prefix — identical for every chapter of this (style, language, mode,
    # brief, outline). This is the byte-stable cacheable region.
    static_prefix = build_static_prefix(
        style=entry,
        language=language,
        video_mode=video_mode,
        brief=brief,
        outline=outline,
    )

    # --- Budgeter: trim prev_tail so the dynamic block fits the input budget. ---
    if prev_tail_token_budget is None:
        # Give the variable block roughly the model's output ceiling worth of
        # input headroom, minus a reserve for the chapter scope + RAG passages.
        passages_tokens = estimate_tokens(_passages_text(rag_passages))
        budget = max(0, out_ceiling - _USER_OVERHEAD_TOKENS - passages_tokens)
    else:
        budget = max(0, int(prev_tail_token_budget))

    trimmed_tail = trim_to_token_budget(prev_tail or "", budget, keep="tail")
    # Trimmed iff the budgeter actually dropped content (len shrank) — robust to
    # whitespace-only stripping which is not a meaningful trim.
    prev_tail_trimmed = bool(prev_tail) and len(trimmed_tail) < len((prev_tail or "").strip())

    dynamic_block = build_dynamic_block(
        language=language,
        chapter=ch,
        prev_tail=trimmed_tail,
        rag_passages=rag_passages,
    )

    system_msg: dict[str, Any] = {"role": "system", "content": static_prefix}
    if cache_control:
        # Anthropic-style: hint the proxy/provider to cache the system prefix.
        # Harmless to OpenAI-style providers (extra key ignored).
        system_msg["cache_control"] = {"type": "ephemeral"}

    messages = [system_msg, {"role": "user", "content": dynamic_block}]

    prefix_tokens = estimate_tokens(static_prefix)
    dynamic_tokens = estimate_tokens(dynamic_block)

    return Composed(
        messages=messages,
        static_prefix=static_prefix,
        dynamic_block=dynamic_block,
        cache_key=cache_key(style=entry, language=language, job_id=job_id),
        model=resolved_model,
        max_tokens=out_ceiling,
        prefix_tokens=prefix_tokens,
        dynamic_tokens=dynamic_tokens,
        input_tokens=prefix_tokens + dynamic_tokens,
        prev_tail_trimmed=prev_tail_trimmed,
        style_key=style_key,
        language_label=lang_label,
    )


def _default_model() -> str:
    """Env worker model, matching orchestrator's WORKER_MODEL default."""
    try:  # pragma: no cover
        from orchestrator import WORKER_MODEL  # type: ignore
        return WORKER_MODEL
    except Exception:  # noqa: BLE001
        import os
        return os.environ.get("WORKER_MODEL", "gemini-2.5-flash")


__all__ = [
    "Chapter",
    "Composed",
    "compose",
    "build_static_prefix",
    "build_dynamic_block",
    "cache_key",
    "estimate_tokens",
    "trim_to_token_budget",
    "WORDS_TO_TOKENS",
]
