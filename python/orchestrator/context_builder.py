# -*- coding: utf-8 -*-
"""
Project Dalang — orchestrator.context_builder (WS-5, coherence layer).

THE PROBLEM THIS SOLVES
-----------------------
WS-1 (`orchestrator.core`) can fan a book out to many `run_worker` calls that write
chapters IN PARALLEL. Parallel writers that never see each other produce four
classic failure modes:

  1. REPETITION       — two chapters tell the same anecdote / use the same hook.
  2. RE-INTRODUCTION  — chapter 5 re-introduces a person/term chapter 2 already set up.
  3. TONE DRIFT       — each worker drifts to its own register; the book feels stitched.
  4. CONTRADICTION    — worker A says "1453", worker B says "1454"; facts diverge.

The fix is a single SHARED CONTEXT computed ONCE per job and handed to every worker:
the same canonical facts, the same style guide, and an explicit per-worker SCOPE that
tells each writer what it does NOT own (so it doesn't wander into a neighbour's lane).

HOW IT FEEDS `compose()` (pakem.assembler, WS-4)
------------------------------------------------
`compose()` already splits a prompt into a CACHEABLE system prefix (static across the
job) and a VARIABLE user block (per chapter). SharedContext is built to slot into both:

  * canonical_facts + style_guide + RULES  -> the cached SYSTEM prefix
        feed via `compose(brief=ctx.brief_block())`  (rides the byte-stable prefix that
        is paid for once per job, exactly like the narrative brief WS-4 already caches).
  * scope_for(no) + RAG passages           -> the per-chapter USER turn
        feed via `compose(prev_tail=ctx.scope_for(no), rag_passages=ctx.passages)`.

CRITICAL: RAG retrieval happens EXACTLY ONCE per job (`build_shared_context`), not once
per chapter. The retrieved passages + extracted canonical facts are reused for every
chapter, which is both cheaper and the whole point — every worker grounds on the SAME
facts, killing the contradiction failure mode at the source.

This module is additive and import-safe: it REUSES the real `get_narration_context`
when the moat package is importable (same path `laozhang_api` uses) and degrades to an
empty-but-valid context otherwise. It never requires redis/asyncpg/fastapi to import.

ROUTER-INTEGRATION EXAMPLE (how a /narration runtime wires it together)
-----------------------------------------------------------------------
    from orchestrator.context_builder import build_shared_context
    from orchestrator.core import Worker, run_worker, synthesize
    from pakem.assembler import compose

    async def run_book(topic, chapters, style, language, tenant_id, job_id):
        # 1) ONE RAG retrieval + brand lookup for the WHOLE job.
        ctx = await build_shared_context(topic, chapters, tenant_id, style=style)

        # 2) Fan out: every worker shares the cached prefix (facts+style+RULES via
        #    brief_block) and gets its OWN anti-collision scope in the user turn.
        async def write(ch):
            composed = compose(
                style=style, language=language, job_id=job_id,
                outline=ctx.outline(),                 # whole-book structure (static)
                brief=ctx.brief_block(),               # facts + style_guide + RULES (cached)
                chapter=ch,
                prev_tail=ctx.scope_for(ch["index"]),  # "you do NOT own ..." (per chapter)
                rag_passages=ctx.passages,             # retrieved ONCE, reused
            )
            return await run_worker(
                Worker(name=f"ch{ch['index']}", style=style,
                       system=composed.messages[0]["content"]),
                composed.messages[1]["content"],
            )

        results = await asyncio.gather(*(write(c) for c in chapters))
        # 3) Optional manager pass to smooth seams (orchestrator.synthesize).
        return results
"""
from __future__ import annotations

import os
import re
import sys
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger("orchestrator.context")


# ===========================================================================
# RAG retrieval — reuse the REAL get_narration_context, exactly as laozhang_api
# does (`from moat.gutenberg.rag_narration import get_narration_context`). On the
# host dev box `moat` lives under data/, so we add that to sys.path first (the
# rag_narration module itself does the same kind of path insertion). If the moat
# package or Qdrant is simply unavailable we degrade to a valid empty context —
# the orchestrator must stay importable everywhere (host 3.14, Docker 3.11).
# ===========================================================================
_RAG_OK = False
_get_narration_context = None  # type: ignore[assignment]
_get_style_config = None       # type: ignore[assignment]


def _ensure_moat_on_path() -> None:
    """Best-effort: put the repo's data/ dir on sys.path so `import moat` works
    on a host checkout (in Docker prod, /app already has moat on the path)."""
    here = os.path.dirname(os.path.abspath(__file__))           # python/orchestrator
    repo = os.path.dirname(os.path.dirname(here))               # repo root
    for cand in (os.path.join(repo, "data"), os.path.join(repo, "python")):
        if os.path.isdir(cand) and cand not in sys.path:
            sys.path.insert(0, cand)


try:  # pragma: no cover - depends on environment deps (Qdrant / moat presence)
    _ensure_moat_on_path()
    from moat.gutenberg.rag_narration import get_narration_context as _get_narration_context  # type: ignore
    try:
        from moat.gutenberg.style_rag_config import get_style_config as _get_style_config  # type: ignore
    except Exception:  # noqa: BLE001 - style config is optional, RAG still works
        _get_style_config = None  # type: ignore
    _RAG_OK = True
    log.debug("context_builder: reusing moat.gutenberg.rag_narration.get_narration_context")
except Exception as _rag_imp_err:  # noqa: BLE001 - intentional broad fallback
    log.info(
        "context_builder: RAG not importable (%s); shared context will carry no "
        "retrieved passages (canonical_facts stays empty, prompts still build).",
        _rag_imp_err.__class__.__name__,
    )

# RAG master-roadmap Phase 1: global kill switch (default OFF). RAG underperforms
# standard right now (eval gate 8.52 vs 6.52), so retrieval is skipped until the
# Step 5→6→7 fix lands. Re-enable per-env with RAG_ENABLED=true.
_RAG_ENABLED = os.environ.get("RAG_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
if _RAG_OK and not _RAG_ENABLED:
    log.info("context_builder: RAG importable but DISABLED (RAG_ENABLED=false) — skipping retrieval.")


# Legacy/UI style names -> the rag-corpus style key the retriever filters on. Mirrors
# the intent of laozhang_api._RAG_STYLE_LEGACY / _rag_style: broad genres return None
# (skip the Qdrant style filter), specific styles map to their corpus key.
_RAG_STYLE_LEGACY: dict[str, Optional[str]] = {
    "biography": "pov_first_person",
    "documentary": "natgeo",
    "science": "youtube_popular_science",
    "narrative non-fiction": None,   # broad genre — no style filter
    "narrative nonfiction": None,
    "harari": "big_history",
    "academic popular": "academic_popular",
    "creative non-fiction": "creative_nonfiction",
    "creative nonfiction": "creative_nonfiction",
}


def _rag_style(style: Optional[str]) -> Optional[str]:
    """Resolve a user/legacy style to its rag-corpus style key (or None to skip
    the Qdrant style filter for broad genres). Falls back to passing it through."""
    if not style:
        return None
    s = style.strip().lower()
    if s in _RAG_STYLE_LEGACY:
        return _RAG_STYLE_LEGACY[s]
    return s


# ===========================================================================
# The four anti-collision RULES. Injected verbatim into the cached system prefix
# so EVERY parallel worker reads the same contract. Each rule targets one of the
# four parallel-writing failure modes by name.
# ===========================================================================
COHERENCE_RULES = """COHERENCE CONTRACT (you are ONE writer among several working in parallel on the SAME book — obey this contract so the chapters fuse into one voice):

1. NO REPETITION. Do NOT re-tell an anecdote, re-use a hook/opening device, or repeat an example that belongs to another chapter. Each scene, image, and turn of phrase appears in this book exactly once — and it is not yours unless it is in YOUR scope below.

2. NO RE-INTRODUCTION. Any person, place, term, or concept named in CANONICAL FACTS has ALREADY been introduced earlier in the book. Refer to it as already-known (no "a man named…", no first-time definitions). Introduce ONLY what is genuinely new to your scope.

3. NO TONE DRIFT. Hold the exact register, person, and tense fixed in STYLE GUIDE for every sentence. Do not relax into a different voice as the chapter goes on. When unsure how something should sound, match the STYLE GUIDE, not your own default.

4. NO CONTRADICTION. State a name, date, number, place, or quote ONLY if it appears in CANONICAL FACTS below (or is uncontroversial common knowledge you are certain of). If you need a specific fact that is NOT in CANONICAL FACTS, do NOT invent it — write a literal placeholder "[VERIFY: what you need]" and keep the narration flowing around it. A placeholder is always better than a fabricated fact that contradicts another chapter.
"""


# Safe default style guide when no tenant brand profile exists. Keeps every
# parallel worker on one register so chapters fuse (kills TONE DRIFT by default).
DEFAULT_STYLE_GUIDE = (
    "Voice: confident, warm, and precise — a master storyteller who respects the "
    "reader's intelligence. Person/tense: keep whatever the chosen style fixes; do "
    "not switch person or tense mid-book. Sentences: vary length for rhythm; favour "
    "concrete nouns and strong verbs over abstraction. Avoid cliché, filler, hedging "
    "throat-clearing, and meta-commentary about the writing itself. Consistent "
    "terminology: once a thing is named, keep using that name across the whole book."
)


# ===========================================================================
# SharedContext — built ONCE per job, handed to every worker.
# ===========================================================================
@dataclass
class SharedContext:
    """The single source of cross-chapter truth for one narration job.

    Built once by `build_shared_context` and read by every parallel worker:
      * canonical_facts -> goes into the cached SYSTEM prefix (so all workers
        ground on the SAME facts — kills contradiction & re-introduction).
      * style_guide     -> goes into the cached SYSTEM prefix (one register for
        the whole book — kills tone drift).
      * scope_for(no)   -> goes into the per-chapter USER turn (tells each worker
        what it does NOT own — kills repetition & lane-crossing).
    """
    topic: str = ""
    chapters: list[dict] = field(default_factory=list)
    style_guide: str = DEFAULT_STYLE_GUIDE
    # Human-readable facts block ready to drop into the prompt (deduped, numbered).
    canonical_facts: str = ""
    # Retrieved RAG passages (the raw dicts) + the pre-formatted context_text, so
    # the assembler can take either. Retrieved ONCE; reused for every chapter.
    passages: list[dict] = field(default_factory=list)
    context_text: str = ""
    sources: list[str] = field(default_factory=list)
    style: Optional[str] = None
    rag_used: bool = False

    # -- outline ----------------------------------------------------------
    def outline(self) -> str:
        """Render the WHOLE-book outline as a deterministic numbered list.

        Identical for every chapter of the job, so it rides the cacheable prefix
        (assembler treats a string outline as already-rendered). Gives each worker
        the map of the whole book — necessary context for not re-introducing or
        repeating what other chapters cover.
        """
        if not self.chapters:
            return ""
        lines: list[str] = []
        for i, ch in enumerate(self.chapters, 1):
            title = str(ch.get("title", "") or "").strip()
            desc = str(ch.get("summary", ch.get("description", "")) or "").strip()
            head = f"{i}. {title}" if title else f"{i}."
            lines.append(head + (f" — {desc}" if desc else ""))
        return "\n".join(lines)

    # -- per-worker scope -------------------------------------------------
    def scope_for(self, no: int) -> str:
        """Tell worker for chapter index `no` (0-based) what it OWNS and, crucially,
        what it does NOT own — the neighbouring chapters whose material it must not
        poach or re-introduce. This is the anti-collision instruction; it goes in
        the per-chapter USER turn (it differs per chapter, so it is NOT cacheable).
        """
        n = len(self.chapters)
        if not self.chapters or no < 0 or no >= n:
            return ""
        mine = self.chapters[no]
        mine_title = str(mine.get("title", "") or "").strip() or f"Chapter {no + 1}"
        mine_desc = str(mine.get("summary", mine.get("description", "")) or "").strip()

        lines = [
            f"YOUR SCOPE — you are writing ONLY chapter {no + 1} of {n}: \"{mine_title}\".",
        ]
        if mine_desc:
            lines.append(f"What this chapter covers: {mine_desc}")

        # What you do NOT own: every OTHER chapter, named so this worker can steer clear.
        not_owned: list[str] = []
        for i, ch in enumerate(self.chapters):
            if i == no:
                continue
            t = str(ch.get("title", "") or "").strip() or f"Chapter {i + 1}"
            where = "earlier" if i < no else "later"
            not_owned.append(f"  - Chapter {i + 1} ({where}): \"{t}\" — NOT yours.")
        if not_owned:
            lines.append(
                "You do NOT own these chapters. Do not tell their stories, re-use their "
                "hooks, or re-introduce what they establish:"
            )
            lines.extend(not_owned)

        # Position-aware continuity nudge (no overlap with neighbours).
        if no == 0:
            lines.append(
                "You open the book: set the voice and hook, but leave room — later "
                "chapters build on you, so do not pre-empt their material."
            )
        elif no == n - 1:
            lines.append(
                "You close the book: assume everything above is already told; land the "
                "payoff without re-summarising prior chapters."
            )
        else:
            lines.append(
                "You are a middle chapter: pick up cleanly from what precedes you and "
                "hand off cleanly to what follows — no recap, no foreshadowing another "
                "chapter's reveal."
            )
        return "\n".join(lines)

    # -- cached-prefix payload -------------------------------------------
    def brief_block(self) -> str:
        """The job-static block that rides the CACHED system prefix: the coherence
        RULES + STYLE GUIDE + CANONICAL FACTS. Feed this straight into
        `compose(brief=ctx.brief_block())` — it is identical for every chapter, so
        WS-4's prefix cache pays for it once per job.
        """
        parts = [COHERENCE_RULES.rstrip()]
        if self.style_guide and self.style_guide.strip():
            parts.append("STYLE GUIDE (hold this register for the entire book):\n"
                         + self.style_guide.strip())
        if self.canonical_facts and self.canonical_facts.strip():
            parts.append(
                "CANONICAL FACTS (the ONLY names/dates/numbers/quotes you may state as "
                "fact — anything else, write \"[VERIFY: ...]\"):\n"
                + self.canonical_facts.strip()
            )
        else:
            parts.append(
                "CANONICAL FACTS: (none retrieved for this job — state NO specific "
                "names, dates, numbers, or quotes as fact; where you need one, write "
                "\"[VERIFY: ...]\" so an editor can fill it in.)"
            )
        return "\n\n".join(parts).strip()

    def as_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "n_chapters": len(self.chapters),
            "style": self.style,
            "rag_used": self.rag_used,
            "n_passages": len(self.passages),
            "n_sources": len(self.sources),
            "canonical_facts_chars": len(self.canonical_facts),
            "style_guide_chars": len(self.style_guide),
        }


# ===========================================================================
# Canonical-fact extraction — turn retrieved passages into a deduped facts block.
# We DON'T spend an LLM call here (build_shared_context must be cheap and never
# raise): we lift the concrete groundables — sources + sentences carrying a
# date/number/proper-noun-quote — so workers have a shared factual spine. A later
# WS can swap this for an LLM "fact sheet" pass; the interface stays the same.
# ===========================================================================
_YEAR_RE = re.compile(r"\b(?:1[0-9]{3}|20[0-9]{2}|[1-9][0-9]{0,3}\s?(?:BC|BCE|AD|CE|SM|M))\b")
_NUM_RE = re.compile(r"\b\d[\d.,]*\b")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _looks_groundable(sentence: str) -> bool:
    """A sentence is worth keeping as a candidate fact if it anchors something
    concrete: a year/era, a number, or a quoted phrase. Cheap, conservative."""
    if _YEAR_RE.search(sentence):
        return True
    if '"' in sentence or "“" in sentence:
        return True
    # A bare number that isn't just a reference index.
    if _NUM_RE.search(sentence) and len(sentence) > 25:
        return True
    return False


def extract_canonical_facts(passages: list[dict], sources: list[str],
                            max_facts: int = 12) -> str:
    """Build a deduped, numbered CANONICAL FACTS block from retrieved passages.

    Conservative by design: we surface sources (so workers cite the same canon)
    and a handful of groundable sentences. Empty string if nothing groundable —
    the brief_block() then instructs workers to write "[VERIFY: ...]" instead of
    inventing, which is the contradiction-killing default.
    """
    facts: list[str] = []
    seen: set[str] = set()

    for p in passages or []:
        if not isinstance(p, dict):
            continue
        text = str(p.get("text", p.get("content", "")) or "").strip()
        if not text:
            continue
        for sent in _SENT_SPLIT.split(text):
            sent = sent.strip().strip("…").strip()
            if len(sent) < 25 or len(sent) > 320:
                continue
            if not _looks_groundable(sent):
                continue
            key = re.sub(r"\s+", " ", sent.lower())[:140]
            if key in seen:
                continue
            seen.add(key)
            facts.append(sent)
            if len(facts) >= max_facts:
                break
        if len(facts) >= max_facts:
            break

    lines: list[str] = []
    if sources:
        # Dedup-preserving order.
        uniq_src = list(dict.fromkeys(s for s in sources if s and s.strip()))
        if uniq_src:
            lines.append("Reference sources for this book: " + "; ".join(uniq_src[:8]) + ".")
    for i, f in enumerate(facts, 1):
        lines.append(f"{i}. {f}")
    return "\n".join(lines).strip()


# ===========================================================================
# Tenant brand profile (style_guide) lookup — OPTIONAL.
# No brand-profile table exists in this repo yet (only `tenants`). We probe for
# one defensively so the moment a brand_profiles table/column lands, style guides
# light up automatically — with ZERO change here and no breakage if it never does.
# ===========================================================================
async def _load_style_guide(tenant_id: Optional[str]) -> str:
    """Return the tenant's brand style guide if such a table exists, else the safe
    default. Never raises — a DB miss / no-table / no-asyncpg just yields the default.
    """
    if not tenant_id:
        return DEFAULT_STYLE_GUIDE
    try:  # pragma: no cover - requires DB; degrades cleanly without one
        import database as _db  # type: ignore
    except Exception:  # noqa: BLE001 - DB layer not importable on host
        return DEFAULT_STYLE_GUIDE

    # Probe known-plausible shapes for a brand style guide. The first that returns a
    # non-empty value wins; any SQL error (e.g. relation does not exist) is swallowed
    # and we fall through to the next candidate, then to the default.
    _candidates = (
        ("SELECT style_guide FROM brand_profiles WHERE tenant_id=$1", True),
        ("SELECT brand_voice FROM brand_profiles WHERE tenant_id=$1", True),
        ("SELECT style_guide FROM tenants WHERE id=$1", False),
    )
    for sql, tenant_scoped in _candidates:
        try:
            fetchval = getattr(_db, "_q_fetchval", None)
            if fetchval is None:
                break
            uid = getattr(_db, "_uid", lambda v: v)(tenant_id)
            val = await fetchval(sql, uid, tenant=str(tenant_id) if tenant_scoped else None)
            if val and str(val).strip():
                log.debug("context_builder: loaded brand style_guide for tenant=%s", tenant_id)
                return str(val).strip()
        except Exception:  # noqa: BLE001 - missing table/column/pool -> try next
            continue
    return DEFAULT_STYLE_GUIDE


# ===========================================================================
# build_shared_context — the ONE call per job.
# ===========================================================================
async def build_shared_context(
    topic: str,
    chapters: list[dict],
    tenant_id: Optional[str] = None,
    *,
    style: Optional[str] = None,
    top_k: int = 6,
    min_quality: int = 3,
    rag_timeout: float = 30.0,
) -> SharedContext:
    """Build the per-job SharedContext: ONE RAG retrieval + the brand style guide.

    Runs RAG retrieval EXACTLY ONCE for the whole book (not per chapter), honouring
    RAG_PREFER_SOURCE (e.g. "rino") and min_quality exactly like the production
    `/rag/context` path, then distills the passages into a canonical_facts block.
    Loads the tenant's brand style_guide if a brand table exists, else a safe
    default. Never raises — on any failure it returns a valid empty-ish context so
    prompts still build (workers then ground only on common knowledge + [VERIFY]).

    Args:
      topic:      the book topic (the RAG query seed).
      chapters:   list of chapter dicts ({title, summary/description, ...}). Order
                  is the book order; scope_for() uses the index into this list.
      tenant_id:  tenant for the brand style-guide lookup (optional).
      style:      narration style; mapped to the rag-corpus style filter.
      top_k:      passages to retrieve for the whole job.
      min_quality:corpus quality floor (matches /rag/context default of 3).
      rag_timeout:hard ceiling on the single retrieval so a slow Qdrant can't hang
                  the whole job.
    """
    chapters = list(chapters or [])
    ctx = SharedContext(
        topic=str(topic or "").strip(),
        chapters=chapters,
        style=style,
    )

    # --- brand style guide (independent of RAG; both kicked off, then awaited) ---
    style_guide_task = asyncio.ensure_future(_load_style_guide(tenant_id))

    # --- ONE RAG retrieval for the whole job ---
    rag_task: Optional[asyncio.Future] = None
    if _RAG_OK and _RAG_ENABLED and _get_narration_context is not None and ctx.topic:
        rag_style = _rag_style(style)
        # Per-style retrieval params, mirroring /rag/context: style_filter /
        # structure_filter / min_quality / query_instruction come from style_rag_config
        # when available; otherwise sensible defaults.
        cfg: dict[str, Any] = {
            "style_filter": rag_style,
            "structure_filter": None,
            "min_quality": min_quality,
            "query_instruction": None,
        }
        if _get_style_config is not None and rag_style is not None:
            try:
                _c = _get_style_config(rag_style) or {}
                cfg["style_filter"] = _c.get("style_filter", rag_style)
                cfg["structure_filter"] = _c.get("structure_filter")
                cfg["min_quality"] = _c.get("min_quality", min_quality)
                cfg["query_instruction"] = _c.get("query_instruction")
            except Exception:  # noqa: BLE001 - style config optional
                pass

        async def _retrieve() -> dict:
            # The deployed get_narration_context signature varies across moat
            # versions (some lack structure / query_instruction / prefer_source).
            # Pass ONLY the kwargs it actually accepts, so a richer-than-available
            # call never raises TypeError and silently disables RAG.
            candidate = {
                "topic": ctx.topic,
                "style": cfg["style_filter"],
                "structure": cfg["structure_filter"],
                "min_quality": cfg["min_quality"],
                "top_k": top_k,
                "query_instruction": cfg["query_instruction"],
                "prefer_source": os.environ.get("RAG_PREFER_SOURCE") or None,
            }
            try:
                import inspect
                accepted = set(inspect.signature(_get_narration_context).parameters)
                candidate = {k: v for k, v in candidate.items() if k in accepted}
            except (TypeError, ValueError):  # signature not introspectable → drop the optional extras
                candidate.pop("query_instruction", None)
                candidate.pop("prefer_source", None)
            return await _get_narration_context(**candidate)

        rag_task = asyncio.ensure_future(_retrieve())

    # Await RAG (bounded), then settle facts.
    if rag_task is not None:
        try:
            rag_res = await asyncio.wait_for(rag_task, timeout=rag_timeout)
            if isinstance(rag_res, dict):
                ctx.passages = list(rag_res.get("passages", []) or [])
                ctx.context_text = str(rag_res.get("context_text", "") or "")
                ctx.sources = list(rag_res.get("sources", []) or [])
                ctx.rag_used = bool(ctx.passages)
                ctx.canonical_facts = extract_canonical_facts(ctx.passages, ctx.sources)
        except asyncio.TimeoutError:
            log.warning("context_builder: RAG retrieval timed out after %ss — "
                        "continuing without passages", rag_timeout)
            rag_task.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - RAG must never break the job
            log.warning("context_builder: RAG retrieval failed (%s) — continuing "
                        "without passages", exc)

    # Await the brand style guide (never raises).
    try:
        ctx.style_guide = await style_guide_task or DEFAULT_STYLE_GUIDE
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        ctx.style_guide = DEFAULT_STYLE_GUIDE

    log.info("context_builder: shared context ready %s", ctx.as_dict())
    return ctx


__all__ = [
    "SharedContext",
    "build_shared_context",
    "extract_canonical_facts",
    "COHERENCE_RULES",
    "DEFAULT_STYLE_GUIDE",
]
