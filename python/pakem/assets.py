"""pakem.assets — shared narration assets, defined ONCE.

These are the cross-cutting blocks that used to live (duplicated) in
python/laozhang_api.py, backend/server.js, and
data/moat/gutenberg/rag_narration.py. The pakem is the single source of
truth: every generator (Python /narasi, the Node Google path, the RAG
builder) should pull these from here instead of re-declaring them.

Pure data + functions. No network, no imports of the app.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# FACTUAL INTEGRITY — canonical version, lifted verbatim from
# data/moat/gutenberg/rag_narration.py:550-567 (the most careful copy).
# Ship this ONLY for non-fiction styles (is_fiction == False).
# ---------------------------------------------------------------------------
FACTUAL_INTEGRITY = """FACTUAL INTEGRITY (this is NON-FICTION — a documentary, not a story):
- This narration must be TRUE. Never invent names, places, dates, numbers,
  quotes, or events. A fabricated detail in a documentary is a serious failure,
  even if it sounds convincing.
- Use concrete, specific facts (real names, dates, figures) ONLY when you are
  confident they are real. Specificity makes good documentary — but invented
  specificity is the worst outcome. When unsure of an exact detail, stay general
  rather than fabricate ("in the late 13th century" is fine; a made-up exact date
  is not).
- If solid information on the topic is genuinely limited, do NOT fill the gap with
  fiction. Instead, acknowledge the uncertainty naturally, woven into the
  narration the way real documentaries do — e.g. "what happened next, the records
  do not say" — never as a preamble or disclaimer, and never breaking the
  narration's voice.
- Real historical figures MAY be voiced or dramatized (e.g. a first-person account
  from a real person at a real moment), but their world — the facts around them —
  must remain accurate. Dramatizing a real perspective is allowed; inventing fake
  history is not."""


# ---------------------------------------------------------------------------
# CRAFT RULES — generic "write to be heard" rules shared by every style.
# Derived from rag_narration.py CRAFT/DOCUMENTARY rules + the laozhang prompt
# tail. Style-specific structure lives in the registry; this is the floor.
# ---------------------------------------------------------------------------
CRAFT_RULES = """CRAFT RULES:
- Write to be HEARD, not read — every sentence must feel alive when spoken aloud.
- Vary sentence length: short sentences for momentum, long sentences for depth.
- No headings, no bullet points, no meta-commentary.
- Open immediately with a strong first sentence — no preamble.
- End with a weighty closing line, not a mere summary.
- Do NOT include the chapter title or number in the output.
- Return ONLY the chapter body text. No markdown, no stage directions."""


# ---------------------------------------------------------------------------
# LANGUAGE DIRECTIVE — the most careful version (from laozhang_api.py:4520-4522
# combined with the rag_narration.py:569-573 anti-mirroring clause).
# ---------------------------------------------------------------------------
def LANGUAGE_DIRECTIVE(lang_label: str) -> str:
    """Return the OUTPUT LANGUAGE directive for a resolved language label.

    Use the resolver (resolve_language) to turn a code like "id" into the
    label "Bahasa Indonesia" before calling this.
    """
    return (
        f"OUTPUT LANGUAGE: {lang_label}. "
        f"Write the ENTIRE chapter ONLY in {lang_label}. "
        f"Any references/context above may be in another language — study their "
        f"content, but do NOT mirror or carry their wording into your output. "
        f"Do not leave a foreign word in simply because it appeared in a reference "
        f"or because translating felt awkward; produce the chapter fully in "
        f"{lang_label}."
    )


# ---------------------------------------------------------------------------
# GENERATION PREAMBLE — injected at the TOP of every chapter prompt.
# Lifted from laozhang_api.py:get_generation_preamble (4032-4045).
# ---------------------------------------------------------------------------
def GENERATION_PREAMBLE(video_mode: bool = False) -> str:
    """Top-of-prompt preamble. VO mode signals 'write for ears from word one'."""
    if video_mode:
        return (
            "CRITICAL: You are writing DOCUMENTARY NARRATION — text that will be "
            "spoken aloud by a narrator, heard once, and felt immediately. "
            "Do NOT write written prose and convert it. "
            "Write each sentence as spoken language at formal/literary register. "
            "Apply [ANCHOR] and [BEAT] markers as instructed in the VO rules below.\n\n"
        )
    return ""


# ---------------------------------------------------------------------------
# VIDEO MODIFIER — VO delivery engineering. Appended AFTER style rules when
# video_mode=True so the delivery layer wraps the content layer.
# Lifted verbatim from laozhang_api.py VIDEO_SCRIPT_MODIFIER (3913-4013).
# ---------------------------------------------------------------------------
VIDEO_MODIFIER = """
=== VO GENERATION MODE — WRITE FOR EARS, NOT EYES ===

This chapter is being written DIRECTLY as documentary narration.
Do not write prose first and convert later.
Write each sentence as if a narrator is about to speak it aloud — once, without re-read.

=======================================================================
LAYER 1 — CONTENT (style content rules — already in style guide above)
These are active. What you say must follow the style framework.
This layer is not repeated here. Apply it silently.
=======================================================================

=======================================================================
LAYER 2 — DELIVERY (VO ENGINEERING — how you say it)
These rules govern sentence construction, rhythm, and breath architecture.
=======================================================================

── RULE 1: ONE IDEA PER BREATH ──
A narrator breathes every 15–20 words.
Any clause over 25 words must become two sentences.
Do NOT lower the register — shorten the breath unit, not the vocabulary.

── RULE 2: SENTENCE HIERARCHY — MANDATORY RATIO ──
For every 2–3 profound/aphoristic sentences, insert 1 of the following:
  PLAIN: simple declarative, no metaphor, just fact. Resets the ear.
  BREATHING: one observation, present tense or slow rhythm, atmospheric.
  OBSERVATIONAL: concrete sensory detail — what you SEE or HEAR, not what it MEANS.

Max ONE aphoristic/quotable sentence per paragraph.
Never two consecutive profound sentences. The second one cancels the first.

── RULE 3: RHYTHM VARIATION ──
Vary length deliberately: LONG → SHORT → LONG → SHORT → VERY SHORT.
The very short sentence (3–7 words) is the detonator. Place it after buildup.

── RULE 4: [BEAT] MARKERS ──
After every major revelation or emotional peak: insert [BEAT].
The sentence before [BEAT] must be SHORT — the landing strip, not the runway.
[BEAT] signals: pause here, let image carry, cut to visual.

── RULE 5: ANCHOR LINES ──
Every chapter must have 3–5 ANCHOR lines.
Criteria: under 12 words, standalone quotable, paradox or reversal structure, emotionally irreversible.
Mark each with [ANCHOR].
Anchor lines are VERBATIM — do not edit them during revision. Build everything else around them.

── RULE 6: PROPER NOUN GLOSS ──
A listener cannot pause to google. They hear it once.
For every proper noun or technical term on first mention: add an inline micro-gloss of 2–5 words.

── RULE 7: PARAGRAPH SIZE ──
Maximum 4–5 sentences per paragraph for VO.
White space is pacing. Use it.
After a data-dense paragraph: mandatory 1–2 sentence atmospheric reset before continuing.

── RULE 8: FORBIDDEN IN VO ──
- Complex nested clauses with 3+ subordinate levels
- Abstract tangents that cannot be visualized by the listener
- Two aphorisms in the same paragraph
- Anchor echoing the paragraph before it
- Dropping register to conversational when breaking long sentences
"""


__all__ = [
    "FACTUAL_INTEGRITY",
    "CRAFT_RULES",
    "LANGUAGE_DIRECTIVE",
    "GENERATION_PREAMBLE",
    "VIDEO_MODIFIER",
]
