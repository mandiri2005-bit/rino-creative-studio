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
        # Renamed from "Harari / Big History" (Rino 2026-07-04): descriptive public label,
        # no living-person name in the paid-product UI (registry-expansion doc note #3).
        # Internal key + aliases keep "harari" so existing jobs/localStorage resolve.
        "display_name": "Big History",
        "aliases": ["harari", "big_history", "big history", "diamond", "jared diamond"],
        "is_fiction": False,
        # R-H10 register spec (CC v3): the calibration gate counts required_moves and
        # bans source-branded diction. Mirrors narasi_gate.BANNED_DICTION for harari.
        "register_spec": {
            "required_moves": ["scale_shift", "contingency_reveal"],
            "banned_tells": ["imagined order", "operating system of belief",
                             "shared fiction", "universal fiction", "collective fiction"],
        },
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
        "display_name": "Academic Popular",
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


# ── Dual-path medium layer (CC dual-path doc §1) — every style's NATIVE medium. ──
# ear = anchor born in narration/VO; page = anchor born in written prose. The selector in
# build_style_block compares this with the job's output target (video|book): native match →
# light normalization only; mismatch → R-VO (page→ear) or R-PAGE (ear→book) transform block.
# Applied programmatically so the 14 existing entry bodies stay untouched (Rino-approved).
# Data, not code: if a style's transform output feels wrong, FLIP the mapping first.
_MEDIUM_ORIGIN = {
    # ear-native (anchor = narration)
    "storytelling": "ear", "bedtime_story": "ear", "pov": "ear", "natgeo": "ear",
    "youtube": "ear", "podcast_narrative": "ear", "cinematic_voiceover": "ear",
    # page-native (anchor = written prose)
    "creative_nonfiction": "page", "harari": "page", "journalistic": "page",
    "literary_essay": "page", "academic_popular": "page",
    "narrative_nonfiction": "page", "fiction": "page",
}
for _k, _m in _MEDIUM_ORIGIN.items():
    if _k in STYLES:
        STYLES[_k].setdefault("medium_origin", _m)

# ── P1 expansion wave (pakem-style-registry-expansion.md) — isolated module so the
# core 14 stay reviewable; a broken/absent P1 file must never take down the registry. ──
try:  # pragma: no cover - additive
    from .registry_p1 import P1_STYLES
    for _k, _v in P1_STYLES.items():
        STYLES.setdefault(_k, _v)
except Exception:  # noqa: BLE001
    P1_STYLES = {}

# P2/P3 wave — same isolation contract as P1.
try:  # pragma: no cover - additive
    from .registry_p23 import P23_STYLES
    for _k, _v in P23_STYLES.items():
        STYLES.setdefault(_k, _v)
except Exception:  # noqa: BLE001
    P23_STYLES = {}


# ── Signature moves — the one-line hover copy per style (UI tooltip; served via
# /narration/styles → "moves"). P1 values verbatim from pakem-style-registry-expansion.md;
# core-14 written to match. Applied programmatically so entry bodies stay untouched.
_SIGNATURE_MOVES = {
    # core 14
    "creative_nonfiction":  "Fiction techniques on real facts — concrete scenes, specific POV, sensory detail",
    "storytelling":         "Dramatic narrative arc, tension and reversal, vivid scene-setting",
    "bedtime_story":        "Gentle rhythmic prose, soothing repetition, soft landing ending",
    "harari":               "Macro-zoom scale shifts, institutions revealed as human inventions, deep time against concrete detail",
    "pov":                  "First-person immersion, present-tense sensory experience, interiority",
    "natgeo":               "Reverent observational wonder, precise natural detail, patient pacing",
    "youtube":              "Hook-first opening, punchy explainers, curiosity gaps with payoffs",
    "journalistic":         "Sourced reporting voice, scene-led long form, evidence and attribution",
    "literary_essay":       "Idea-driven reflection, elegant digression, argument as narrative",
    "podcast_narrative":    "Host intimacy, episodic beats, audible signposting",
    "academic_popular":     "Big-idea synthesis, accessible authority, structured argument",
    "cinematic_voiceover":  "Sparse evocative lines, image-led pacing, trailer gravitas",
    "narrative_nonfiction": "Novelistic reconstruction of real events, character-driven chapters, cinematic history",
    "fiction":              "Invented characters and scenes, genre-matched craft, immersive sensory detail",
    # P1 wave (verbatim from the registry-expansion doc)
    "true_crime_procedural": "Detached timeline voice, evidence unveiled in sequence, no speculation until the end, dates as beats",
    "true_crime_host":       "First-person doubt, interviews paraphrased, unresolved threads owned",
    "internet_mystery":      "Digital-forensics framing, timestamps/usernames as clues, escalating rabbit hole, agnostic verdict",
    "existential_science":   "Direct second-person address, scale shocks (atoms→galaxies), optimistic-nihilist closer",
    "systems_logistics":     "How-infrastructure-works, dry wit, economics of mundane things, maps-in-prose",
    "tech_rise_fall":        "Calm measured arc: garage → peak → hubris → collapse, archival quotes, inflection-point beats",
    "elegiac_ruins":         "What was it like to watch your world end — civilization arc, present-day ruins framing, mournful awe",
    "conversational_epic":   "Rhetorical questions, 'imagine you're standing there', tangents acknowledged, visceral hypotheticals",
    "countdown_listicle":    "Ranked segments, snappy per-item hook + payoff, teaser for #1",
    "cosmic_poetic":         "Wonder + humility, humanity-from-orbit perspective, cosmic register in original words",
    "counterintuitive_thesis": "Anecdote → study → reveal loop, 'we've been thinking about X wrong', named-character openers",
    "folklore_creepy":       "Calm cadence over dark material, historical anecdote → universal fear",
    "sleep_story_adult":     "Second-person slow descent, sensory softness, deliberately anticlimactic, sentences that lengthen",
    "epic_fantasy_prologue": "'The world is changed' preamble, mythic exposition, artifact/prophecy framing",
    "trailer_voice":         "Punchy fragments, escalating stakes in 60–90 seconds — built for Shorts/promos",
    "gothic_cosmic_horror":  "Unreliable senses, dread by implication, forbidden-knowledge arc, archaic diction",
    "internet_horror":       "'This happened to me', escalating rules-based dread, ambiguous ending",
    "warm_omniscient":       "Gentle authoritative reflection, humanity-affirming closers, patient pacing",
    "dongeng_nusantara":     "'Pada zaman dahulu kala…', tokoh binatang/rakyat, pesan moral penutup eksplisit",
    "horor_viral_indonesia": "Thread-style orang pertama ('gue'), setting KKN/kampung, pantangan dilanggar, slow-burn penasaran",
    "legenda_asal_usul":     "Etiological arc (kenapa gunung/danau ini ada), kutukan/sumpah, nama tempat sebagai payoff",
    "first_principles":      "Build from zero, homemade analogies, delight in not-knowing",
    "stoic_daily":           "Aphoristic, imperative mood, ancient quote → modern application, memento mori beats",
    "motivational_grind":    "Confrontational second person, obstacle-as-gift, cadence builds to a charge",
    "business_case":         "How X built Y — founder arc, decision points as cliffhangers, numbers as drama",
}
for _k, _sm in _SIGNATURE_MOVES.items():
    if _k in STYLES:
        STYLES[_k].setdefault("signature_moves", _sm)
# Styles without a hand-written hover line (P2/P3 wave) synthesize one from their
# register_spec.required_moves so the picker popup is never empty.
for _k, _e in STYLES.items():
    if not _e.get("signature_moves"):
        _mv = ((_e.get("register_spec") or {}).get("required_moves")) or []
        if _mv:
            _e["signature_moves"] = ", ".join(str(m).replace("_", " ") for m in _mv[:4])


# ═══ SCHEMA v2 (refactor: pipeline_rules × style_spec — cc-instruksi-refactor doc) ═══
# Two orthogonal per-style axes applied programmatically (entry bodies untouched):
#   category        A Dokumenter/YouTube · B Suara Penulis · C Audio-first ·
#                   D Sinematik/Genre · E Nusantara · F Edukasi/Motivasi  (UI grouping)
#   factual_regime  strict | hybrid | fictional (refactor §4 defaults; job-overridable)
# plus style_spec counters/ratios for the deterministic counter engine (v4 §1) —
# ONLY harari is hand-tuned (refactor §3, values frozen = round-4 behavior); other
# styles get counters=None ⟹ the engine reports UNMEASURED/off, tuned on first
# production use (refactor §8: don't fill 74 specs blind).
_CATEGORY = {
    # core 14
    "creative_nonfiction": "B", "storytelling": "D", "bedtime_story": "C", "harari": "B",
    "pov": "D", "natgeo": "A", "youtube": "A", "journalistic": "A", "literary_essay": "B",
    "podcast_narrative": "C", "academic_popular": "B", "cinematic_voiceover": "D",
    "narrative_nonfiction": "B", "fiction": "D",
    # P1
    "true_crime_procedural": "A", "true_crime_host": "A", "internet_mystery": "A",
    "existential_science": "A", "systems_logistics": "A", "tech_rise_fall": "A",
    "elegiac_ruins": "A", "conversational_epic": "A", "countdown_listicle": "A",
    "cosmic_poetic": "B", "counterintuitive_thesis": "B",
    "folklore_creepy": "C", "sleep_story_adult": "C",
    "epic_fantasy_prologue": "D", "trailer_voice": "D", "gothic_cosmic_horror": "D",
    "internet_horror": "D", "warm_omniscient": "D",
    "dongeng_nusantara": "E", "horor_viral_indonesia": "E", "legenda_asal_usul": "E",
    "first_principles": "F", "stoic_daily": "F", "motivational_grind": "F", "business_case": "F",
}
_FACTUAL_REGIME = {
    # refactor §4: core 14 default strict, exceptions:
    "fiction": "fictional", "bedtime_story": "fictional", "storytelling": "fictional",
    "pov": "hybrid",
    # P1 exceptions (A/B/F strict by default; D fictional; C sleep fictional; E per doc)
    "sleep_story_adult": "fictional",
    "epic_fantasy_prologue": "fictional", "trailer_voice": "fictional",
    "gothic_cosmic_horror": "fictional", "internet_horror": "fictional",
    "warm_omniscient": "strict",
    "dongeng_nusantara": "fictional", "horor_viral_indonesia": "hybrid",
    "legenda_asal_usul": "hybrid",
}
for _k, _e in STYLES.items():
    _e.setdefault("category", _CATEGORY.get(_k, "B"))
    _e.setdefault("factual_regime", _FACTUAL_REGIME.get(_k, "strict"))

# harari style_spec — MIGRATED VALUES, frozen (refactor §3; regression = round-4 outcomes).
# v4 §2: the scale-shift taxonomy makes the TEMPORAL zoom explicitly required (round 4
# satisfied spatial/systemic but flinched from temporal); placement = the chapter with
# the largest unexplained causal claim. banned_tells += "is just a story we tell".
STYLES["harari"]["register_spec"] = {
    "required_moves": [
        "scale_shift_temporal",            # ≥1 REQUIRED — deep-time reframe as vertigo
        "contingent_institution_reveal",   # systemic zoom (credited, round-4 strength)
        "thesis_image",                    # outline-nominated concrete image
    ],
    "banned_tells": ["imagined order", "operating system of belief", "shared fiction",
                     "universal fiction", "collective fiction", "is just a story we tell"],
}
STYLES["harari"]["style_spec"] = {
    "counters": {"citations_max": 5, "citations_distribution": "varied",
                 "aporia_max": 3, "thesis_restatement_max": 2, "triplet_per_1000w": 4},
    "ratios": {"scene_min_pct": 40},
    "positive_exemplars": ["round4_ch8_gold_drowning", "round4_ch2_requerimiento",
                           "round4_closing_line"],
}

# ironic_moral_fable style_spec — PIPELINE-SPEC v1 §4, style_spec instance (P2 tier;
# base entry lives in registry_p23.py). Aurelius-run learnings: Rino flagged 1-dim
# greed-only characters and Dutch moral-summary tells; the register_spec below adds
# protagonist_psychological_layer + contingent_institution_reveal + a capped
# narrator_moral_aside; the style_spec counters cap moral asides / direct address and
# hold repetition/bureaucracy ratio ≤0.15 (10-15% cut target). Fables are page-native:
# medium_origin OVERRIDES the shipped "ear" to "book" per §4 (output_support=book+video).
# factual_regime=fictional (invented parables). positive_exemplars=[] — first production
# use; seed on next review pass.
STYLES["ironic_moral_fable"]["medium_origin"] = "book"
STYLES["ironic_moral_fable"]["output_support"] = ["book", "video"]
STYLES["ironic_moral_fable"]["factual_regime"] = "fictional"
STYLES["ironic_moral_fable"]["tts_risk"] = "medium"
STYLES["ironic_moral_fable"]["aliases"] = [
    "Ironic Moral Fable", "Fable Satir Ironis", "Fable Satiris",
    "ironic moral fable", "ironic_moral_fable", "rod serling", "serling",
    "twilight zone", "twist fable", "moral twist tale",
]
STYLES["ironic_moral_fable"]["register_spec"] = {
    "required_moves": [
        "protagonist_psychological_layer",   # ≥1 REQUIRED — luka/ketakutan/motif; forbids 1-dim greed-only characters
        "contingent_institution_reveal",     # ≥1 REQUIRED — absurd system has a specific mechanism, not vague
        "narrator_moral_aside",              # capped ≤3; over-explaining kills satire
    ],
    "banned_tells": [
        "Het is een bescheiden les",         # Dutch — Rino flagged literal moral-summary tell
        "Sta ons toe u voor te stellen",     # Dutch narrator-intro cliche
        "The lesson is simple",              # English equivalent
        "Notice that",                       # English narrator-explain
    ],
}
STYLES["ironic_moral_fable"]["style_spec"] = {
    "counters": {"moral_asides_max": 3, "narrator_direct_address_max": 4,
                 "merk_op_max": 3},
    "ratios": {"repetition_bureaucracy_ratio_max": 0.15},
    "positive_exemplars": [],
}
# DALANG_MORALISTE_CALIBRATION: recalibrated caps for moraliste register.
# Baseline (moral_asides_max=3) false-blocked a real 9.0/10 Dutch narasi
# (Goedgemutst/EthiClean) with 10 aphoristic asides — moraliste voice USES
# asides as a genre marker; strict cap punishes correct style. Raise to 8;
# add narrator_opening_formula_budget=3 (audit found 6/6 formulaic openers,
# R4 in the review); add final_image_required=True (audit R5).
import os as _os_moraliste
if _os_moraliste.environ.get("DALANG_MORALISTE_CALIBRATION") == "1":
    STYLES["ironic_moral_fable"]["style_spec"]["counters"]["moral_asides_max"] = 8
    STYLES["ironic_moral_fable"]["style_spec"]["counters"]["narrator_opening_formula_budget"] = 3
    STYLES["ironic_moral_fable"]["style_spec"]["final_image_required"] = True


# ── category-level register/style defaults (P0 rollout, Rino 2026-07-05)
# PIPELINE-SPEC v1 §4 said "hand-tune on first production use", but 13 CORE styles
# were running with ZERO explicit gates (register_spec/style_spec empty), meaning
# the deterministic counter engine had nothing to measure against and the narrative
# guard had no banned-tell / required-move floor. This is the minimum-viable
# coverage — category-level defaults per the design_rationale — so every CORE
# style has SOME gate the pipeline can enforce. Per-style hand-tuning (like
# harari's frozen values at L684-699 and ironic_moral_fable's overrides at
# L710-737) happens as production observation surfaces category-inadequate cases.
#
# Semantics — pure setdefault: any style that already has a non-empty
# register_spec (harari) or style_spec (harari, ironic_moral_fable) is UNTOUCHED.
# Empty holes fill from the style's category. Missing category → default "B".
CATEGORY_DEFAULTS: dict[str, dict[str, dict]] = {
    # A — documentary / explainer (natgeo, youtube, journalistic, + P1 A-tier)
    "A": {
        "register_spec": {
            "required_moves": [
                "source_or_provenance_anchor",
                "concrete_number_or_date",
                "mechanism_reveal",
            ],
            "banned_tells": [
                "scientists say", "studies have shown", "it is widely believed",
                "many experts agree", "throughout history", "since the dawn of time",
            ],
        },
        "style_spec": {
            "counters": {
                "citations_max": 6, "same_source_max": 3,
                "rhetorical_question_max": 2, "aside_max": 2,
                "direct_address_max": 3, "hedge_max": 4, "superlative_max": 3,
            },
        },
    },
    # B — essayistic (creative_nonfiction, literary_essay, academic_popular,
    # narrative_nonfiction, harari-class). harari's own register_spec/style_spec
    # is set explicitly above; setdefault won't touch it.
    "B": {
        "register_spec": {
            "required_moves": [
                "thesis_image",
                "scale_shift_temporal",
                "contingent_institution_reveal",
            ],
            "banned_tells": [
                "at the end of the day", "it is important to note",
                "in a very real sense", "the question then becomes",
                "one might argue", "as we shall see",
            ],
        },
        "style_spec": {
            "counters": {
                "citations_max": 5, "same_scholar_max": 4, "debate_pairing_max": 2,
                "aporia_max": 3, "anchors_max": 3, "triplet_per_1000w": 4,
                "rhetorical_question_max": 3, "aside_max": 3,
                "direct_address_max": 2, "moral_gloss_max": 1,
            },
        },
    },
    # C — audio-first, low-stakes (bedtime_story, podcast_narrative,
    # + P1 folklore_creepy / sleep_story_adult)
    "C": {
        "register_spec": {
            "required_moves": [
                "sensory_breath_anchor",
                "gentle_return_or_refrain",
            ],
            "banned_tells": [
                "let us begin", "close your eyes and imagine", "take a deep breath",
                "in this episode", "welcome back listeners", "so without further ado",
            ],
        },
        "style_spec": {
            "counters": {
                "exclamation_max": 1, "rhetorical_question_max": 2, "aside_max": 2,
                "direct_address_max": 6, "abstract_noun_per_1000w": 8,
                "sentence_len_avg_max_words": 18, "adverb_ly_per_1000w": 12,
            },
        },
    },
    # D — dramatic / scene-first (storytelling, pov, cinematic_voiceover, fiction,
    # + P1 epic_fantasy_prologue / trailer_voice / gothic_cosmic_horror /
    # internet_horror / warm_omniscient)
    "D": {
        "register_spec": {
            "required_moves": [
                "in_scene_sensory_detail",
                "character_specific_gesture_or_speech",
                "pov_anchor",
            ],
            "banned_tells": [
                "little did he know", "meanwhile back at", "as fate would have it",
                "with a heavy heart", "in that moment he realized",
                "and so it was that",
            ],
        },
        "style_spec": {
            "counters": {
                "aside_max": 1, "direct_address_max": 1, "moral_gloss_max": 0,
                "adverb_ly_per_1000w": 10, "epithet_repeat_max": 2,
                "interior_thought_per_1000w": 6, "flashback_max": 2,
                "adjective_stack_max": 2,
            },
        },
    },
}

# Apply defaults: setdefault semantics — NEVER overwrite existing per-style values.
# harari (register_spec + style_spec both set at L684-699) is fully protected.
# ironic_moral_fable (register_spec + style_spec both set at L719-737) is fully
# protected. Any future per-style override placed above this block is likewise safe.
# Styles with a partial spec (e.g. register_spec set but style_spec empty) get the
# missing half filled from their category; the set half is preserved.
for _key, _entry in STYLES.items():
    _cat = _entry.get("category", "B")
    _defaults = CATEGORY_DEFAULTS.get(_cat, CATEGORY_DEFAULTS["B"])
    _rs = _entry.setdefault("register_spec", {})
    _rs.setdefault(
        "required_moves",
        list(_defaults["register_spec"]["required_moves"]),
    )
    _rs.setdefault(
        "banned_tells",
        list(_defaults["register_spec"]["banned_tells"]),
    )
    _ss = _entry.setdefault("style_spec", {})
    _ss.setdefault(
        "counters",
        dict(_defaults["style_spec"]["counters"]),
    )
    _ss.setdefault("positive_exemplars", [])


# ── sample prompts (picker UX): two register-matched topic exemplars per style, shown
# as the Title/Theme placeholder when the style is selected. Nusantara styles sample in
# Bahasa. Served via styles_catalog() → /narration/styles → FE placeholder. Programmatic
# (setdefault) like _CATEGORY so the 75 body entries stay untouched.
_SAMPLE_PROMPTS: dict[str, str] = {
    # A — documentary / explainer
    "natgeo": "e.g. The secret life of the Sumatran rainforest canopy\nor: Okavango — the river that never reaches the sea",
    "youtube": "e.g. Why the Sahara was green 6,000 years ago\nor: The physics hiding inside a soap bubble",
    "journalistic": "e.g. How one container ship blocked 12% of world trade\nor: The rise and fall of the Concorde",
    "true_crime_procedural": "e.g. The disappearance of the Sodder children, 1945\nor: The Isdal Woman — Norway's coldest case",
    "true_crime_host": "e.g. D.B. Cooper — the hijacker who vanished mid-air\nor: The Somerton Man, found on a quiet beach",
    "internet_mystery": "e.g. Cicada 3301 — the internet's hardest puzzle\nor: The Max Headroom broadcast intrusion",
    "existential_science": "e.g. The heat death of the universe\nor: What happens if the Gulf Stream stops",
    "systems_logistics": "e.g. How a banana reaches your table in 14 days\nor: The invisible machine that runs global shipping",
    "tech_rise_fall": "e.g. Nokia — from world domination to fire sale\nor: The billion-dollar collapse of Theranos",
    "elegiac_ruins": "e.g. Angkor — the city the jungle took back\nor: Detroit's abandoned Packard plant",
    "conversational_epic": "e.g. The entire fall of Rome, over coffee\nor: How Genghis Khan actually happened",
    "countdown_listicle": "e.g. 7 lost cities we've actually found\nor: 5 inventions that arrived too early",
    "question_driven_explainer": "e.g. Why do we dream?\nor: Why can't we just print more money?",
    "sports_mythic": "e.g. The Miracle on Ice, 1980\nor: Senna at Monaco — the lap of the gods",
    "archival_elegiac": "e.g. The last voices of the Titanic survivors\nor: Letters home from the Somme, 1916",
    "collage_history": "e.g. 1969 — the year everything happened at once\nor: Berlin, November 1989, hour by hour",
    "intimate_nature": "e.g. A year in the life of one oak tree\nor: The octopus in the tide pool",
    "ecstatic_doom_nature": "e.g. Toba — the eruption that nearly ended us\nor: The wolf that rewired Yellowstone",
    "embedded_gritty": "e.g. Three weeks on a Bering Sea crab boat\nor: Night shift in a big-city ER",
    # B — literary / essay
    "creative_nonfiction": "e.g. The night the lighthouse keeper didn't come home\nor: A history of my grandmother's kitchen",
    "harari": "e.g. Salt — the mineral that built and broke empires\nor: How wheat domesticated humans",
    "literary_essay": "e.g. On waiting rooms\nor: The quiet tyranny of the to-do list",
    "academic_popular": "e.g. How the Black Death rewired Europe's economy\nor: The cognitive revolution, 70,000 years ago",
    "narrative_nonfiction": "e.g. The five days of Dunkirk\nor: Shackleton's open-boat voyage to South Georgia",
    "cosmic_poetic": "e.g. We are the universe looking at itself\nor: Every atom in you was forged in a dying star",
    "counterintuitive_thesis": "e.g. The Middle Ages were more innovative than the Renaissance\nor: Traffic jams are a sign your city works",
    "witty_wonder": "e.g. The surprisingly dramatic life of the potato\nor: A brief history of the pause button",
    "character_driven_systems": "e.g. The trucker who accidentally invented the container age\nor: How one Dutch engineer drained a sea",
    "braided_thriller_history": "e.g. Two men racing to crack the Enigma\nor: The parallel hunts for the atomic bomb",
    "comic_science": "e.g. Your gut bacteria are running the show\nor: The absurd physics of cats landing on their feet",
    "clinical_compassion": "e.g. The last outbreak of smallpox\nor: Anatomy of a heart transplant, hour by hour",
    "world_weary_travel": "e.g. Overnight train to Ulaanbaatar\nor: The last ferry out of Tangier",
    "literary_true_crime": "e.g. The quiet town that hid a serial poisoner\nor: A vanishing on the Appalachian Trail",
    "grand_sweep_history": "e.g. The Silk Road — 1,500 years in ten chapters\nor: The age of sail, from carrack to clipper",
    "literary_reportage": "e.g. The last cassette factory on Earth\nor: A season inside a dying coal town",
    "polyphonic_testimony": "e.g. Chernobyl, in the voices of those who stayed\nor: The 1965 blackout, told by nine strangers",
    "gonzo": "e.g. 72 hours inside a Las Vegas pawn shop\nor: Riding shotgun with storm chasers in Tornado Alley",
    # C — calm / audio-first
    "bedtime_story": "e.g. The lighthouse cat who counted stars\nor: A sleepy village where the clocks run slow",
    "podcast_narrative": "e.g. Episode one — the map that shouldn't exist\nor: A small-town mystery in three acts",
    "folklore_creepy": "e.g. The thing that knocks twice in Appalachia\nor: Why you don't whistle at night in the forest",
    "sleep_story_adult": "e.g. A slow night train through the winter Alps\nor: The bookshop at the end of the rain",
    "sound_led_wonder": "e.g. What the deep ocean sounds like\nor: The hum of a glacier, melting",
    "act_structure_personal": "e.g. The summer I worked the fire lookout\nor: How I lost and found my father's watch",
    "guided_meditation": "e.g. A walk through a bamboo forest at dawn\nor: Letting the day settle like sand in water",
    # D — fiction / cinematic
    "storytelling": "e.g. The ferryman who rowed a king in disguise\nor: A thief who stole only memories",
    "pov": "e.g. I am the last lighthouse keeper on the Atlantic\nor: You wake as the only engineer on a drifting station",
    "cinematic_voiceover": "e.g. Dawn over a city that doesn't know it's the last day\nor: One take — the heist begins at midnight",
    "fiction": "e.g. A cartographer who maps places that don't exist yet\nor: The village where everyone shares one dream",
    "epic_fantasy_prologue": "e.g. Before the war of the nine crowns\nor: The forging of the blade that ended a god",
    "trailer_voice": "e.g. One city. One night. No way out.\nor: This summer… the ocean fights back",
    "gothic_cosmic_horror": "e.g. The lighthouse log ends mid-sentence\nor: Something in the ice has been counting",
    "internet_horror": "e.g. My smart doorbell keeps ringing — from inside\nor: I moderate a forum that shouldn't exist",
    "warm_omniscient": "e.g. The small kindnesses of a very ordinary street\nor: A postman who knew everyone's secrets",
    "film_noir_vo": "e.g. She walked in with trouble and a fake name\nor: The city sweats at 2 a.m. — and so do I",
    "ironic_moral_fable": "e.g. The king who taxed the rain\nor: A town that outsourced its conscience",
    "whispered_existential": "e.g. What the mirror keeps when you leave\nor: The hour between night and morning",
    "magical_realism": "e.g. The year it rained letters from the dead\nor: A grandmother who folds time into her batik",
    "fairy_tale_classic": "e.g. The miller's daughter and the winter king\nor: Three brothers and a door in the mountain",
    "mythic_epic": "e.g. The song of the first fire\nor: How the sea was given its salt",
    "epistolary": "e.g. Letters between a soldier and a lighthouse keeper\nor: The diary of the last speaker of a dying language",
    "second_person_adventure": "e.g. You inherit a shop that sells forgotten things\nor: You have one match, and the night is long",
    "mockumentary_deadpan": "e.g. Inside the fierce world of competitive snail racing\nor: The office that runs the moon",
    # E — Nusantara (sample in Bahasa)
    "dongeng_nusantara": "cth. Asal mula Danau Toba\natau: Si Kancil dan raja hutan yang sombong",
    "horor_viral_indonesia": "cth. KKN di desa yang tak ada di peta\natau: Penunggu lantai 4 kosan lama",
    "legenda_asal_usul": "cth. Asal-usul nama Banyuwangi\natau: Legenda Gunung Tangkuban Perahu",
    "babad_hikayat": "cth. Babad runtuhnya Majapahit\natau: Hikayat pelayaran ke negeri atas angin",
    "pewayangan_ki_dalang": "cth. Lakon Gatotkaca gugur di Kurusetra\natau: Semar mbangun kahyangan",
    # F — teaching / oratory
    "first_principles": "e.g. Money, rebuilt from barter up\nor: Flight, explained from a falling leaf",
    "stoic_daily": "e.g. On things not in our control\nor: The obstacle is the way — a morning meditation",
    "motivational_grind": "e.g. Nobody is coming to save you — good\nor: The 4 a.m. advantage",
    "business_case": "e.g. How Toyota out-built Detroit\nor: The day Netflix killed its own DVD business",
    "eli5": "e.g. Why is the sky blue?\nor: How does a plane stay up?",
    "socratic_dialogue": "e.g. Is a hot dog a sandwich — and why it matters\nor: What makes a country rich?",
    "commencement_wisdom": "e.g. Advice to my 22-year-old self\nor: The unglamorous secret of good work",
    "oratory_anaphora": "e.g. We build, we break, we build again\nor: This is the hour of the small and stubborn",
}
for _k, _e in STYLES.items():
    _e.setdefault("sample_prompt", _SAMPLE_PROMPTS.get(_k, ""))


# Default style when nothing resolves — matches the legacy get_style_rules
# fallback (creative non-fiction).
DEFAULT_STYLE = "creative_nonfiction"


__all__ = ["STYLES", "DEFAULT_STYLE"]
