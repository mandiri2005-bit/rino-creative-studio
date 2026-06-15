"""pakem.registry — the styles. ONE source of truth.

Each style entry holds:
  display_name        — human label
  aliases             — list of accepted input keys (substring/legacy/JS keys)
  is_fiction          — True => skip FACTUAL_INTEGRITY + skip RAG factual rules
  rag                 — {query_instruction, framing, min_quality, top_k}
                        (extracted from data/moat/gutenberg/style_rag_config.py)
  style_rules_core    — load-bearing rules SHIPPED at generation time
  style_rules_editor  — long-form rules used ONLY in an editor pass.
                        DO NOT ship at generation. "" when there is no
                        separate editor layer.

The long harari / narrative-nonfiction blocks are loaded verbatim from
pakem/_src/*.txt (sliced from python/laozhang_api.py) to avoid transcription
drift. Everything else is inline.
"""
from __future__ import annotations

import os

_SRC = os.path.join(os.path.dirname(__file__), "_src")


def _load(name: str) -> str:
    path = os.path.join(_SRC, name)
    with open(path, encoding="utf-8") as fh:
        return fh.read().rstrip() + "\n"


# Long blocks (verbatim slices from laozhang_api.py STYLE_RULES).
_HARARI_CORE = _load("harari_core.txt")      # PART A — generation prompt
_HARARI_EDITOR = _load("harari_editor.txt")  # PART B — editor pass (NOT shipped)
_NNF_FULL = _load("nnf_full.txt")            # full cinematic-history engine


# ---------------------------------------------------------------------------
# Short, load-bearing core rules (verbatim from STYLE_RULES, these styles ship
# their full block at generation — they are already compact).
# ---------------------------------------------------------------------------
_CREATIVE_NF = """STYLE: Creative Non-Fiction
= Techniques of fiction (concrete scenes, specific POV, sensory detail) applied to REAL FACTS.

STRUCTURE PER CHAPTER:
1. COLD OPEN -- One specific cinematic scene. Specific object, person, moment -- NOT abstract.
2. UNTOLD STORY -- The fact most people don't know. Specific data: %, dates, species names, site names.
3. SUDUT PANDANG -- At least one scene from a specific character's human POV.

FORBIDDEN: empty abstraction ("harapan", "keberanian", "cakrawala yang menari", "kita adalah kelanjutan mereka").
REQUIRED: Min 2 specific facts with numbers/dates per section. 1 concrete object/sensory detail per paragraph.
"""

_STORYTELLING = """STYLE: Storytelling -- Narrative Drama
= Story-first. Every historical fact must be delivered through SCENE and CHARACTER, not exposition.

STRUCTURE PER CHAPTER:
1. SCENE OPENER -- Drop into the middle of a moment. In medias res. Who, what, where -- in the first sentence.
2. CONFLICT/TENSION -- Every chapter needs a problem or stakes. What does someone want? What stands in the way?
3. DIALOGUE -- At least 2 lines of spoken dialogue per chapter. Ground it in specific context.
4. TURN -- A moment where something changes: a realization, a surprise, a decision.

FORBIDDEN: Passive summary of events. Telling emotion instead of showing. Generic descriptions.
REQUIRED: Named or clearly characterized figures. Cause-and-effect within scenes. Physical action.
"""

_BEDTIME = """STYLE: Bedtime Story -- Gentle, Soothing
= Warm narrator voice, gentle wonder, age-appropriate vocabulary. History as a lullaby.

STRUCTURE PER CHAPTER:
1. SOFT OPENING -- Begin with a peaceful image or a gentle question. No drama, no conflict.
2. SENSE OF WONDER -- Each chapter reveals one amazing thing in a way that feels like a gift, not a lesson.
3. COMFORTING CLOSE -- End each chapter with warmth. A sense that things turned out okay.

FORBIDDEN: Violence, conflict, darkness. Complex syntax. Academic jargon.
REQUIRED: Short sentences. Soft vocabulary. Metaphors from nature and everyday life. Second-person ("kamu") or inclusive "kita".
"""

_POV = """STYLE: POV -- First Person Immersive
= You ARE the historical figure. First person, present tense, immediate sensory experience.

STRUCTURE PER CHAPTER:
1. IMMEDIATE SENSORY OPENING -- First sentence places reader in a body, in a moment.
2. INNER MONOLOGUE -- Thoughts, fears, calculations.
3. SPECIFIC OBSERVATION -- What do I see/hear/smell/touch that reveals historical context?
4. DECISION OR ACTION -- The POV character does or decides something that moves history.

FORBIDDEN: Third person. Omniscient narrator intrusions. Modern sensibility projected onto ancient figure.
REQUIRED: Present tense throughout. Specific sensory details -- not abstract emotions.
"""

_NATGEO = """STYLE: National Geographic Documentary
= Science anchored in beauty. Every fact arrives inside a visual, environmental description.

STRUCTURE PER CHAPTER:
1. LANDSCAPE SHOT -- Open with the physical environment as it looks/feels/smells.
2. ZOOM TO SUBJECT -- From landscape to a specific creature, artifact, or human activity.
3. SCIENTIFIC EXPLANATION -- The "how does this work" in accessible, precise language.
4. CONSERVATION/SIGNIFICANCE FRAME -- Why does this matter today?

FORBIDDEN: Vague wonder without specificity. Human-centric framing that ignores ecology.
REQUIRED: Species names, geological terms, GPS-level location specificity. Present tense for ongoing phenomena.
"""

_YOUTUBE = """STYLE: YouTube -- Popular Science
= Hook in first sentence. Curiosity loops. Reframe what viewer thinks they know.

STRUCTURE PER CHAPTER:
1. HOOK -- First sentence must be a question, surprising fact, or counterintuitive claim.
2. SETUP THE MYSTERY -- What's the weird thing we're about to explain? Why should they keep watching?
3. EXPLAIN WITH ANALOGY -- One modern analogy per complex concept. Make the ancient feel familiar.
4. PAYOFF + REFRAME -- Answer the question, then add "...and here's what that means for you today."

FORBIDDEN: Academic tone. Passive voice. Long blocks without a hook or punchline.
REQUIRED: Short punchy sentences mixed with longer ones. Direct address ("kamu", "kalian"). At least one modern analogy.
"""

_JOURNALISTIC = """STYLE: Journalistic -- Long Form
= Report the past like a journalist covering a breaking story. Sources, scenes, quotes, stakes.

STRUCTURE PER CHAPTER:
1. LEAD -- The most important/surprising fact first. Then context.
2. NUT GRAF -- What is this chapter really about? Why does it matter?
3. SCENE + VOICE -- At least one reconstructed scene + one "quoted" source (archaeologist, record, oral tradition).
4. MULTIPLE ANGLES -- Show competing interpretations. What do scholars disagree about?

FORBIDDEN: Single narrative voice without tension. Unverified claims presented as fact.
REQUIRED: Attribution language ("menurut penelitian...", "arkeolog menemukan..."). Present tense for reconstruction. Specific numbers and sources.
"""

_LITERARY_ESSAY = """STYLE: Literary Essay
= Personal intellectual voice. Digressive. Thinking on the page, not presenting conclusions.

STRUCTURE PER CHAPTER:
1. PERSONAL/ASSOCIATIVE OPENING -- Start with an observation, memory, or cultural reference that connects obliquely.
2. DIGRESSION -- Follow one idea sideways before returning to the main thread.
3. COMPLEXITY -- Resist simple conclusions. Show what we don't know. Sit with the ambiguity.
4. RESONANT CLOSE -- End not with a conclusion but with a lingering image or open question.

FORBIDDEN: Thesis statements. Bullet-point logic. Authoritative declarations.
REQUIRED: First-person or intimate narrator voice. Cultural and literary references. Sentences that think out loud.
"""

_PODCAST = """STYLE: Podcast Narrative
= Written for the ear, not the eye. Conversational, signposted, built on spoken rhythm.

STRUCTURE PER CHAPTER:
1. CONVERSATIONAL HOOK -- Address the listener directly. Short sentence to catch attention.
2. SCENE -- Tell a short story in present tense, as if recounting to a friend.
3. EXPLANATION -- "Nah, inilah yang menarik..." -- signpost the insight clearly.
4. LISTENER TAKEAWAY -- End with "apa artinya ini?" for the listener's life or worldview.

FORBIDDEN: Complex nested sentences. Dense data without analogies. Visual-only descriptions.
REQUIRED: Short sentences (max 20 words each for key points). Signpost phrases. Rhythm that works read aloud.
"""

_ACADEMIC_POPULAR = """STYLE: Academic Popular (like Sapiens)
= Big claim -> evidence -> implication. Accessible language for complex ideas. Thought experiments.

STRUCTURE PER CHAPTER:
1. BOLD OPENING CLAIM -- State the argument plainly. No hedging.
2. EVIDENCE STACK -- 3-4 specific data points that support the claim. Studies, sites, percentages.
3. THOUGHT EXPERIMENT -- "Bayangkan jika..." -- use hypothetical to make abstract concrete.
4. IMPLICATION FOR TODAY -- Connect past to present human behavior, society, or culture.

FORBIDDEN: Jargon without definition. Evidence without interpretation. Hedging that kills momentum.
REQUIRED: Footnote-worthy specifics in accessible language. Comparative lens. One thought experiment per chapter.
"""

_CINEMATIC_VO = """STYLE: Cinematic Voiceover
= Written for a narrator's voice over moving images. Short. Punchy. Visual. Rhythmic.

STRUCTURE PER CHAPTER:
1. VISUAL ESTABLISHING LINE -- One sentence, one image. What is the camera seeing?
2. NARRATION IN SHORT BURSTS -- 2-4 sentence paragraphs max. Pause between images.
3. EMOTIONAL BEAT -- One moment of human connection. Keep it brief.
4. TITLE CARD CLOSE -- End with a short, quotable line. One sentence. Strikes like a title card.

FORBIDDEN: Long complex sentences. Explanatory exposition. Anything that can't be spoken in one breath.
REQUIRED: Present tense. Fragments allowed for rhythm. Powerful monosyllabic words where possible. Visual-first, emotion-second.
"""

# Compact core for the narrative-nonfiction engine. The 300-line full block
# (_NNF_FULL) is editor-grade craft reference, kept OUT of generation.
_NNF_CORE = """STYLE: Narrative Non-Fiction (Cinematic History)
= Sources: Erik Larson / Robert Caro / Sebastian Junger / Jon Krakauer / Hampton Sides.
= Netflix prestige-doc / PBS NOVA register. Plain prose output, NO production markers.

THE WRITER MUST DISAPPEAR BEHIND THE REALITY. Enter from ground level, not from above.

STRUCTURE PER CHAPTER:
1. SCENE BEFORE THESIS -- Every argument first appears as object, body, landscape, ritual, or action.
2. MATERIAL REALITY FIRST -- weight, distance, hunger, moisture, fatigue, wood, salt, smoke, mud, labor, weather.
3. INFORMATION DISCOVERED -- Embed evidence inside narrative flow; no visible "research shows..." exposition.
4. RESONANCE ENDING -- Close on physical residue or a forward-pulling image, not a summary.

FORBIDDEN: Self-aware narrator. Stacked aphorisms. Quotable-line stacking. Any bracketed production markup in output.
REQUIRED: Scene before thesis. Material reality first. Evidence tiering held inline. Plain prose -- no markers.
"""


# ---------------------------------------------------------------------------
# THE REGISTRY
# Canonical key -> entry. `aliases` are matched by the resolver (exact,
# normalized, then substring) so substring-style inputs like "creative
# non-fiction documentary" still resolve.
# ---------------------------------------------------------------------------
STYLES: dict[str, dict] = {
    "creative_nonfiction": {
        "display_name": "Creative Non-Fiction",
        "aliases": [
            "creative non-fiction", "creative nonfiction", "creative_nonfiction",
        ],
        "is_fiction": False,
        "rag": {
            "query_instruction": (
                "Retrieve a literary non-fiction passage that combines "
                "factual precision with vivid sensory detail and personal voice:"
            ),
            "framing": (
                "Study how the writer BLENDS fact with sensory detail and a strong "
                "narrative voice. Notice how they use specific details (numbers, "
                "names, places) while preserving the beauty of the prose."
            ),
            "min_quality": 3,
            "top_k": 4,
        },
        "style_rules_core": _CREATIVE_NF,
        "style_rules_editor": "",
    },

    "storytelling": {
        "display_name": "Storytelling",
        "aliases": ["storytelling", "story telling", "narrative drama", "epic"],
        "is_fiction": False,
        "rag": {
            "query_instruction": (
                "Retrieve a dramatic narrative passage with tension, "
                "character action, and vivid scene-setting:"
            ),
            "framing": (
                "Study how the writer builds TENSION and SCENE in the examples below. "
                "Notice the sentence rhythm, sensory detail, and moments of dramatic reversal."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": _STORYTELLING,
        "style_rules_editor": "",
    },

    "bedtime_story": {
        "display_name": "Bedtime Story",
        "aliases": ["bedtime story", "bedtime", "bedtime_story", "lullaby"],
        "is_fiction": False,
        "rag": {
            "query_instruction": (
                "Retrieve a gentle, lyrical, flowing passage with "
                "soothing rhythm and peaceful imagery:"
            ),
            "framing": (
                "Study the RHYTHM and GENTLENESS of the sentences in the examples below. "
                "Notice the flowing sentence length, soothing word choices, and how the "
                "writer creates a sense of safety and calm."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": _BEDTIME,
        "style_rules_editor": "",
    },

    "harari": {
        "display_name": "Harari / Big History",
        "aliases": ["harari", "big_history", "big history", "diamond", "jared diamond"],
        "is_fiction": False,
        "rag": {
            "query_instruction": (
                "Retrieve a passage that explains large-scale historical patterns, "
                "civilizational forces, or long-term causation across centuries:"
            ),
            "framing": (
                "Study how the writer CONNECTS the large scale (civilizations, centuries, "
                "patterns) with concrete, tangible detail. Notice the cause-and-effect "
                "arguments, analogies across time, and bold opening lines."
            ),
            "min_quality": 3,
            "top_k": 4,
        },
        # PART A — the load-bearing generation prompt (shipped).
        "style_rules_core": _HARARI_CORE,
        # PART B — editor pass. NEVER ship at generation (creates self-conscious prose).
        "style_rules_editor": _HARARI_EDITOR,
    },

    "pov": {
        "display_name": "POV — First Person Immersive",
        "aliases": ["pov", "pov_first_person", "first person", "first_person", "biography", "biographical"],
        "is_fiction": False,
        "rag": {
            "query_instruction": (
                "Retrieve a first-person memoir or journal passage with "
                "immediate sensory presence and personal voice:"
            ),
            "framing": (
                "Study how the writer creates PRESENCE and IMMERSION in the first-person "
                "point of view. Notice the sensory details, the internal monologue, and "
                "the intimacy of the narrative voice."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": _POV,
        "style_rules_editor": "",
    },

    "natgeo": {
        "display_name": "National Geographic Documentary",
        "aliases": ["national geographic", "natgeo", "national_geographic", "documentary", "discovery"],
        "is_fiction": False,
        "rag": {
            "query_instruction": (
                "Retrieve a passage rich in natural-world observation, precise "
                "scientific detail, and a strong sense of place:"
            ),
            "framing": (
                "Study how the writer anchors SCIENCE in BEAUTY — every fact arrives "
                "inside a visual or environmental description. Notice the precision of "
                "species names, geology, and location."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": _NATGEO,
        "style_rules_editor": "",
    },

    "youtube": {
        "display_name": "YouTube — Popular Science",
        "aliases": ["youtube", "youtube_popular_science", "popular_science", "popular science", "science"],
        "is_fiction": False,
        "rag": {
            "query_instruction": (
                "Retrieve a clear, engaging explanatory passage that makes "
                "complex ideas accessible with concrete examples and momentum:"
            ),
            "framing": (
                "Study how the writer EXPLAINS complex ideas with energy and clarity. "
                "Notice the opening line that grabs attention immediately, the easy-to-grasp "
                "analogies, and how they keep momentum without losing accuracy."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": _YOUTUBE,
        "style_rules_editor": "",
    },

    "journalistic": {
        "display_name": "Journalistic — Long Form",
        "aliases": ["journalistic", "long_form", "long form", "reportage"],
        "is_fiction": False,
        "rag": {
            "query_instruction": (
                "Retrieve an investigative or reportage passage that builds "
                "a case through accumulated evidence, quotes, and scene-setting:"
            ),
            "framing": (
                "Study how the writer BUILDS A CASE through accumulated evidence. Notice "
                "the structure of investigative paragraphs, the use of quotes and sources, "
                "and how they balance fact with narrative."
            ),
            "min_quality": 3,
            "top_k": 4,
        },
        "style_rules_core": _JOURNALISTIC,
        "style_rules_editor": "",
    },

    "literary_essay": {
        "display_name": "Literary Essay",
        "aliases": ["literary essay", "literary_essay", "essay", "philosophical", "reflective"],
        "is_fiction": False,
        "rag": {
            "query_instruction": (
                "Retrieve a reflective, intellectually rich prose passage "
                "that develops a complex argument with elegance and precision:"
            ),
            "framing": (
                "Study how the writer DEVELOPS AN ARGUMENT with prose elegance. Notice the "
                "complex yet clear sentence structure, the use of paradox and qualification, "
                "and the density of ideas per paragraph."
            ),
            "min_quality": 4,  # high bar — literary essay needs the best prose
            "top_k": 3,
        },
        "style_rules_core": _LITERARY_ESSAY,
        "style_rules_editor": "",
    },

    "podcast_narrative": {
        "display_name": "Podcast Narrative",
        "aliases": ["podcast narrative", "podcast_narrative", "podcast"],
        "is_fiction": False,
        "rag": {
            "query_instruction": (
                "Retrieve a conversational, direct-address passage that speaks to the "
                "reader, asks questions, and sounds like a host talking aloud:"
            ),
            "framing": (
                "Write this as a HOST TALKING DIRECTLY TO ONE LISTENER, not a documentary "
                "voice-over. Address the listener throughout — rhetorical questions, asides, "
                "'imagine this' moments. Relaxed spoken register: contractions, casual "
                "connectives, the rhythm of thinking out loud. Keep the conversational voice "
                "all the way to the end — do NOT drift into grand cinematic prose."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": _PODCAST,
        "style_rules_editor": "",
    },

    "academic_popular": {
        "display_name": "Academic Popular (Sapiens)",
        "aliases": ["academic popular", "academic_popular", "sapiens", "expository",
                    "finance", "economics", "business"],
        "is_fiction": False,
        "rag": {
            "query_instruction": (
                "Retrieve a passage that synthesizes scholarly knowledge "
                "into accessible prose, combining evidence with bold interpretive claims:"
            ),
            "framing": (
                "Study how the writer SYNTHESIZES scholarly knowledge into prose anyone can "
                "read. Notice the bold claims backed by specific evidence, how they use "
                "historical examples as illustration, and boldness of interpretation "
                "without losing rigor."
            ),
            "min_quality": 3,
            "top_k": 4,
        },
        "style_rules_core": _ACADEMIC_POPULAR,
        "style_rules_editor": "",
    },

    "cinematic_voiceover": {
        "display_name": "Cinematic Voiceover",
        "aliases": ["cinematic voiceover", "cinematic_voiceover", "cinematic", "voiceover"],
        "is_fiction": False,
        "rag": {
            "query_instruction": (
                "Retrieve a visually striking, high-contrast passage with "
                "punchy sentences, strong imagery, and dramatic forward momentum:"
            ),
            "framing": (
                "Study how the writer creates IMAGES THE READER CAN VISUALIZE with words. "
                "Notice the short punchy sentences, the dramatic contrast, and how they "
                "begin and end paragraphs with maximum force."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": _CINEMATIC_VO,
        "style_rules_editor": "",
    },

    "narrative_nonfiction": {
        "display_name": "Narrative Non-Fiction (Cinematic History)",
        "aliases": [
            "narrative non-fiction", "narrative nonfiction", "narrative_nonfiction",
            "narrative_non_fiction", "narrative_nonfiction_mystery", "investigative",
            "mystery", "suspense", "cinematic history",
        ],
        "is_fiction": False,
        "rag": {
            "query_instruction": (
                "Retrieve a passage that builds suspense through accumulated "
                "evidence, unanswered questions, and a sense of hidden truth:"
            ),
            "framing": (
                "This is NON-FICTION: an investigation of a REAL historical mystery, not an "
                "invented thriller. Build suspense the way the best true-history writers do — "
                "around genuine unanswered questions and real gaps in the record. Every name, "
                "place, date, and event must be real. Do NOT invent characters or fabricate "
                "a story to create mystery."
            ),
            "min_quality": 3,
            "top_k": 4,
        },
        # Compact, shippable engine; full 300-line block kept as editor reference.
        "style_rules_core": _NNF_CORE,
        "style_rules_editor": _NNF_FULL,
    },

    "fiction": {
        "display_name": "Fiction",
        "aliases": [
            "fiction", "fictional", "story", "short_story", "novel", "horror",
            "horror_story", "fairy_tale", "fairytale", "childrens_story", "children",
            "dongeng", "drama", "thriller", "fantasy", "scifi", "science_fiction",
            "sci-fi", "anime", "noir", "comedy",
        ],
        "is_fiction": True,
        "rag": {
            "query_instruction": (
                "Retrieve an imaginative, vivid fictional narrative passage:"
            ),
            "framing": (
                "This is FICTION — invent freely. You may create characters, places, events, "
                "and dialogue. Match the sub-genre implied by the topic (horror: dread; fairy "
                "tale: warmth and wonder; drama: human conflict; thriller: suspense and a "
                "turning reveal). Build immersive scenes with sensory detail, distinct "
                "characters, and momentum. The goal is a compelling story, not factual accuracy."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": (
            "STYLE: Fiction\n"
            "= Invent freely. Create characters, places, events, and dialogue.\n"
            "= Match the sub-genre implied by the topic (horror / fairy tale / drama / thriller).\n\n"
            "CRAFT:\n"
            "- Build immersive scenes with sensory detail and distinct characters.\n"
            "- Vary sentence length: short for momentum, long for depth.\n"
            "- Open IN the story — no preamble. End on a resonant final line.\n"
            "- Keep it age-appropriate when the topic calls for a children's story.\n"
        ),
        "style_rules_editor": "",
    },
}


# Default style when nothing resolves — matches the legacy get_style_rules
# fallback (creative non-fiction).
DEFAULT_STYLE = "creative_nonfiction"


__all__ = ["STYLES", "DEFAULT_STYLE"]
