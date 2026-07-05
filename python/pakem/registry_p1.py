# ── pakem/registry_p1.py — P1 expansion wave (pakem-style-registry-expansion.md).
# GENERATED from the 6-category draft workflow (2026-07-04), then integrated.
# Isolated from the core 14 (registry.py merges via STYLES.setdefault) so a problem
# here can never take down the base registry. Schema per entry mirrors registry.py
# + dual-path fields: medium_origin (ear|page), tier, tts_risk, output_support,
# register_spec {required_moves, banned_tells} for the R-H10 gate.

# === A-docyoutube — shared bases ===
# --- A. Dokumenter & YouTube-native (all medium_origin=ear, tier P1) -------

_P1_TRUE_CRIME_BASE = """STYLE: True Crime
= Real crimes reconstructed for the ear. The facts are the drama; restraint is the register.

SHARED STRUCTURE:
1. COLD OPEN -- One concrete pre-crime moment: a place, a date, an ordinary detail that will matter later.
2. TIMELINE SPINE -- Build the account on a chronology. Speak dates and times plainly; they are the beats.
3. EVIDENCE IN SEQUENCE -- Reveal facts in the order investigators uncovered them, one at a time.
4. VERDICT LAST -- Withhold interpretation until the evidence is fully on the table.

SHARED VOICE:
- Past tense for events; present tense only for what remains unknown today.
- Name victims with dignity. Never linger on gore for effect.
- Attribution BEFORE every claim: police report, court record, witness -- say the source first.

FORBIDDEN: Gratuitous gore. Jokes. Invented dialogue or inner thoughts no record supports.
"""

# === D-cinematic — shared bases ===
# --- D. Sinematik & Genre — shared family base (cousin rule, doc note #1) ---
_P1_HORROR_BASE = """STYLE FAMILY: Horror -- dread by implication. The horror is never shown in full; the reader assembles it from wrongness.
DREAD RULES (family-wide):
- IMPLY, never reveal -- describe effects, absences, and aftermath; never the entity in full light.
- ESCALATE WRONGNESS -- each scene breaks one more rule of normal reality; shrink the gaps between breaks.
- GROUND THE UNCANNY -- anchor every impossible detail to a mundane sensory one (a smell, a clock, a doorframe).
FORBIDDEN (family-wide): Jump-scare exclamations. Explaining the horror's mechanics. Gore as a substitute for dread.
"""

P1_STYLES = {
    # === A-docyoutube ===

"true_crime_procedural": {
    "display_name": "True Crime — Procedural",
    "aliases": [
        "true crime procedural", "true_crime_procedural", "casefile",
        "case file", "procedural true crime", "true crime case file",
    ],
    "is_fiction": False,
    "medium_origin": "ear",
    "tier": "P1",
    "tts_risk": False,
    "output_support": "both",
    "rag": {
        "query_instruction": (
            "Retrieve a factual crime or investigation account told "
            "chronologically with dates, evidence, and sourced detail:"
        ),
        "framing": (
            "Study how the writer sequences EVIDENCE and DATES without "
            "editorializing. Notice the flat, sourced delivery and how "
            "interpretation is withheld until the facts are complete."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": _P1_TRUE_CRIME_BASE + """
PROCEDURAL VARIANT (detached case-file register):
- VOICE: Third person, detached, even-toned -- a case file read aloud, never a performer telling a tale.
- Open each section on a spoken date or time stamp ("On the morning of June third...").
- State physical evidence with flat exactness: distances, durations, item descriptions -- no adjectives of feeling.
- Confine ALL speculation to one clearly-marked final section; before it, report only what the record shows.
- Close on the case status today -- solved, cold, or pending -- in one plain sentence, no flourish.
FORBIDDEN: First-person intrusion. Emotional adjectives attached to evidence. Cliffhanger theatrics between sections.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "date_beat_open", "evidence_in_sequence", "withheld_verdict",
        ],
        "banned_tells": [
            "listener discretion is advised",
            "our episodes deal with serious and often distressing incidents",
        ],
    },
},

"true_crime_host": {
    "display_name": "True Crime — Host Investigation",
    "aliases": [
        "true crime host investigation", "true_crime_host", "serial",
        "host investigation", "investigative true crime", "host-led true crime",
    ],
    "is_fiction": False,
    "medium_origin": "ear",
    "tier": "P1",
    "tts_risk": False,
    "output_support": "both",
    "rag": {
        "query_instruction": (
            "Retrieve a first-person investigative account where the "
            "narrator reasons through uncertain evidence:"
        ),
        "framing": (
            "Study how the narrator thinks OUT LOUD: doubts voiced, leads "
            "followed, contradictions owned. Notice how paraphrased testimony "
            "and unresolved threads build intimacy and trust."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": _P1_TRUE_CRIME_BASE + """
HOST-INVESTIGATION VARIANT (first-person doubt register):
- VOICE: First person, present tense for the investigating -- the listener watches you think, now.
- Keep circling back to the one detail that will not resolve; admit the obsession in your own words.
- Paraphrase interviews instead of quoting at length ("She told me she never saw the car"), then react to them.
- Weigh both readings of every ambiguous fact aloud; change your mind on the record when the evidence turns.
- Own the loose ends at the close: name exactly what you still cannot answer and why it matters.
FORBIDDEN: Detached case-file monotone. False certainty. Hiding the narrator's doubts to seem authoritative.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "first_person_doubt", "circling_detail", "owned_loose_end",
        ],
        "banned_tells": [
            "i keep coming back to",
            "one story told week by week",
            "next time, on serial",
        ],
    },
},

"internet_mystery": {
    "display_name": "Internet Mystery Deep-Dive",
    "aliases": [
        "internet mystery", "internet_mystery", "internet mystery deep-dive",
        "lemmino", "barely sociable", "deep dive", "unsolved internet mystery",
    ],
    "is_fiction": False,
    "medium_origin": "ear",
    "tier": "P1",
    "tts_risk": False,
    "output_support": "both",
    "rag": {
        "query_instruction": (
            "Retrieve an investigative passage that reconstructs events "
            "from documents, records, or digital traces:"
        ),
        "framing": (
            "Study how the writer treats artifacts (posts, records, timestamps) "
            "as EVIDENCE and escalates from one clue to the next. Notice how "
            "confirmed fact is kept separate from speculation."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": """STYLE: Internet Mystery Deep-Dive
= Digital forensics narrated calmly. The internet is the crime scene; posts, timestamps, and usernames are the evidence.

STRUCTURE:
1. ARTIFACT OPEN -- Begin with one concrete digital artifact -- a video, a post, an account -- described plainly, with its date.
2. ESCALATING RABBIT HOLE -- Each section digs one layer deeper: what was found, who found it, what it opened up.
3. THEORY LEDGER -- Present competing explanations as they emerged in the community, each with its strongest evidence.
4. AGNOSTIC VERDICT -- Weigh the theories, rank their plausibility, and refuse to crown a winner unless the evidence forces it.

VOICE: Calm, methodical, third person. The excitement lives in the findings, never in the delivery.
SIGNATURE MOVES:
- Speak timestamps and usernames as precisely as a detective cites case numbers.
- Mark every shift between confirmed fact, community consensus, and pure speculation -- out loud, every time.
- Let dead ends stay dead: report the lead that went nowhere. It buys trust.
FORBIDDEN: Declaring the mystery solved without proof. Horror-stinger dramatics or creepypasta framing.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "digital_artifact_open", "escalating_layers",
            "fact_speculation_split", "agnostic_verdict",
        ],
        "banned_tells": [
            "down the rabbit hole",
            "we may never know",
        ],
    },
},

"existential_science": {
    "display_name": "Existential Science",
    "aliases": [
        "existential science", "existential_science", "kurzgesagt",
        "in a nutshell", "second person science", "existential science explainer",
    ],
    "is_fiction": False,
    "medium_origin": "ear",
    "tier": "P1",
    "tts_risk": False,
    "output_support": "both",
    "rag": {
        "query_instruction": (
            "Retrieve a science passage that connects vast cosmic or "
            "microscopic scales to the reader's own life:"
        ),
        "framing": (
            "Study how the writer moves between SCALES and addresses the reader "
            "directly. Notice how enormous numbers are made physical and how "
            "bleak facts are turned toward wonder or agency."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": """STYLE: Existential Science -- Second Person
= Science as an existential experience. Speak directly to "you", swing between atom and galaxy, land on earned hope.

STRUCTURE:
1. DIRECT-ADDRESS HOOK -- The first line puts "you" inside the question ("Right now, trillions of cells inside you...").
2. SCALE LADDER -- Walk the idea up or down orders of magnitude: atom, cell, city, planet, galaxy. Make each rung concrete.
3. THE UNSETTLING TRUTH -- State the bleak implication honestly. Do not soften it yet.
4. EARNED-HOPE CLOSER -- Turn the bleakness into agency or wonder in the final beats, in your own words -- never a stock slogan.

VOICE: Second person throughout. Present tense. Short declarative sentences that build like steps.
SIGNATURE MOVES:
- One scale shock per chapter: a comparison so large or small it produces vertigo ("if the sun were a grain of sand...").
- Convert every big number into time, distance, or bodies. Never leave it abstract.
- Touch the listener's own existence at least once: their atoms, their lifespan, their place in the chain.
FORBIDDEN: Third-person lecture voice. Despair without the turn. Jargon without a picture attached to it.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "second_person_address", "scale_shock", "earned_hope_closer",
        ],
        "banned_tells": [
            "optimistic nihilism",
            "in a nutshell",
        ],
    },
},

"systems_logistics": {
    "display_name": "Systems & Logistics",
    "aliases": [
        "systems and logistics", "systems_logistics", "wendover",
        "reallifelore", "logistics", "infrastructure explainer",
    ],
    "is_fiction": False,
    "medium_origin": "ear",
    "tier": "P1",
    "tts_risk": False,
    "output_support": "both",
    "rag": {
        "query_instruction": (
            "Retrieve a passage explaining how an economic system, supply "
            "chain, or piece of infrastructure actually works:"
        ),
        "framing": (
            "Study how the writer traces the hidden SYSTEM behind something "
            "ordinary. Notice the unit economics, the geography given as spoken "
            "routes, and the dry understated wit."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": """STYLE: Systems & Logistics
= How the boring machinery of the world actually works. Dry wit, hard numbers, the economics of mundane things.

STRUCTURE:
1. MUNDANE HOOK -- Open on an ordinary object or route and make it strange ("Every banana in this store crossed an ocean").
2. SYSTEM REVEAL -- Trace the hidden network behind it: who moves what, where, and why it is set up this way.
3. ECONOMICS BEAT -- Follow the money: cost per unit, margins, incentives. The system exists because the math works.
4. CONSTRAINT PAYOFF -- Close on the bottleneck or trade-off that explains the whole design.

VOICE: Third person, present tense, measured. Amused but never mocking; the wit lives in the framing, not in punchlines.
SIGNATURE MOVES:
- Maps in prose: give geography as spoken routes ("from the port at Rotterdam, up the Rhine, into Basel").
- One concrete unit-economics beat per chapter -- a price, a distance, a time -- rounded for the ear.
- One dry aside per section, delivered deadpan and moved past immediately.
FORBIDDEN: Breathless hype. Calling anything "fascinating" or "incredible" instead of showing why it is.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "mundane_hook", "unit_economics", "map_in_prose", "dry_aside",
        ],
        "banned_tells": [
            "the logistics of",
            "this video was made possible by",
        ],
    },
},

"tech_rise_fall": {
    "display_name": "Tech & Business Rise and Fall",
    "aliases": [
        "tech rise and fall", "tech_rise_fall", "coldfusion", "cold fusion",
        "business rise and fall", "rise and fall", "corporate collapse",
    ],
    "is_fiction": False,
    "medium_origin": "ear",
    "tier": "P1",
    "tts_risk": False,
    "output_support": "both",
    "rag": {
        "query_instruction": (
            "Retrieve a business or technology narrative charting a "
            "company's rise, turning point, and decline:"
        ),
        "framing": (
            "Study how the writer paces an ARC with dates and numbers as beats. "
            "Notice archival quotes used as pivots and the calm register that "
            "never moralizes over the collapse."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": """STYLE: Tech & Business Rise and Fall
= A company's whole arc told calmly: garage, ascent, peak, hubris, collapse. Measured narration over the archival record.

STRUCTURE:
1. ORIGIN SCENE -- Open small and specific: the garage, the dorm room, the first product, the founding date.
2. ASCENT WITH RECEIPTS -- Chart the rise through numbers and dates: revenue, users, headlines -- each one a beat.
3. INFLECTION POINT -- Mark the exact decision or moment where the fall began. Name it plainly.
4. HUBRIS TO COLLAPSE -- Let the same traits that built the company destroy it. Show the mirror, don't announce it.
5. QUIET AFTERMATH -- Close on what remains today: the brand, the patents, the lesson, the ruins of the valuation.

VOICE: Calm, even, documentary-neutral. The drama is in the facts; never raise the narration's pulse.
SIGNATURE MOVES:
- Archival quotes as pivots: a founder's boast or an internal memo, attributed BEFORE it is quoted -- then let it hang.
- Date-stamp every act break ("By 2007...", "Three years later...").
- Pair each peak metric with its later collapse metric: the same number, spoken twice.
FORBIDDEN: Moralizing lectures. Mockery of the fallen. Hindsight dressed up as your own foresight.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "origin_scene", "dated_ascent", "inflection_point",
            "peak_collapse_mirror",
        ],
        "banned_tells": [
            "you're watching coldfusion",
            "how the mighty have fallen",
        ],
    },
},

"elegiac_ruins": {
    "display_name": "Elegiac Ruins",
    "aliases": [
        "elegiac ruins", "elegiac_ruins", "fall of civilizations",
        "paul cooper", "ruins", "lost civilizations", "civilization collapse",
    ],
    "is_fiction": False,
    "medium_origin": "ear",
    "tier": "P1",
    "tts_risk": False,
    "output_support": "both",
    "rag": {
        "query_instruction": (
            "Retrieve a passage about a vanished civilization, ruin, or "
            "lost city told with mournful, evocative detail:"
        ),
        "framing": (
            "Study how the writer moves between present-day RUINS and the "
            "living world they once were. Notice the sensory resurrection of "
            "daily life and the elegiac, unhurried cadence."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": """STYLE: Elegiac Ruins
= The death of a civilization told with mournful awe. Present-day ruins open the door; the living city walks through it.

STRUCTURE:
1. RUINS FRAME -- Open in the present tense at the site today: wind, stone, silence, visitors. Establish what is left.
2. RESURRECTION -- Rebuild the living civilization in full sensory detail: markets, temples, harvests, daily bread.
3. THE ARC -- Trace rise, strain, and unraveling as one continuous story. Collapse is a process, not an event.
4. WITNESS MOMENT -- Give the fall one human pair of eyes: an inhabitant watching their world end. Ask, in your own words, what they must have felt.
5. RETURN TO STONE -- Close back at the ruins, changed by what the listener now knows.

VOICE: Slow, formal, mournful but never sentimental. Long flowing sentences are allowed; this style earns them.
SIGNATURE MOVES:
- Address the gulf of time directly: how long ago, how much is lost, how little survives.
- Quote surviving laments and chronicles where they exist, attributed before the quote.
- Hold awe and grief in the same breath: describe every marvel already knowing its end.
FORBIDDEN: Listicle pacing. Blaming the collapse on a single villain. Modern political point-scoring.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "present_ruins_open", "living_city_resurrection",
            "witness_moment", "return_to_ruins",
        ],
        "banned_tells": [
            "what was it like to watch your world end",
            "lost to the sands of time",
        ],
    },
},

"conversational_epic": {
    "display_name": "Conversational Epic History",
    "aliases": [
        "conversational epic history", "conversational_epic", "dan carlin",
        "hardcore history", "epic history", "conversational history",
    ],
    "is_fiction": False,
    "medium_origin": "ear",
    "tier": "P1",
    "tts_risk": False,
    "output_support": "both",
    "rag": {
        "query_instruction": (
            "Retrieve a dramatic history passage with strong narrative "
            "momentum and visceral human-scale detail:"
        ),
        "framing": (
            "Study how the writer makes epic events FELT at human scale. Notice "
            "the rhetorical questions, the direct invitations to imagine being "
            "there, and sources quoted with open enthusiasm."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": """STYLE: Conversational Epic History
= A brilliant friend telling you history for hours. Huge events, visceral hypotheticals, tangents owned out loud.

STRUCTURE:
1. ORBIT QUESTION -- Begin with the rhetorical question the whole piece will circle ("What does a society do when...?").
2. PLUNGE -- Drop the listener into a scene at human scale: put THEM there -- the smell, the noise, the fear.
3. ESCALATION -- Widen from the scene to the epoch, stacking stakes, but keep returning to what it felt like.
4. OPEN-ENDED CLOSE -- End on the question that remains, not on a tidy answer.

VOICE: First-person telling laced with second-person invitations. Present tense for scenes. Conversational, digressive, urgent.
SIGNATURE MOVES:
- Visceral hypotheticals: make the listener inhabit the ordeal ("picture yourself in that line, knowing what comes next").
- Own your tangents out loud ("...and we'll come back to that") -- then actually come back to it.
- Quote historians and primary sources by name, marveling at them like a fan sharing a find.
- Use rhetorical questions as gear-shifts between sections -- but never two in a row.
FORBIDDEN: Academic detachment. Tidy moral summaries. Pretending certainty where the sources conflict.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "orbit_question", "you_are_there", "owned_tangent",
            "visceral_hypothetical",
        ],
        "banned_tells": [
            "hardcore history",
            "what if i told you",
        ],
    },
},

"countdown_listicle": {
    "display_name": "Countdown / Listicle",
    "aliases": [
        "countdown", "listicle", "countdown_listicle", "watchmojo",
        "top 10", "top ten", "ranked list", "count down",
    ],
    "is_fiction": False,
    "medium_origin": "ear",
    "tier": "P1",
    "tts_risk": False,
    "output_support": "both",
    "rag": {
        "query_instruction": (
            "Retrieve a punchy, high-momentum passage built from short "
            "self-contained segments with strong hooks:"
        ),
        "framing": (
            "Study how each segment HOOKS instantly and pays off fast. Notice "
            "the rhythm of short sentences and how anticipation is engineered "
            "toward a final reveal."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": """STYLE: Countdown / Listicle
= Ranked entertainment. N items, descending to number one, every item a self-contained hook-and-payoff.

STRUCTURE:
1. PREMISE + RULES -- One breath: what is being ranked and by what criteria. Then move.
2. RANKED SEGMENTS -- Count DOWN. Announce each rank crisply ("Number seven."), then hook, context, payoff -- done.
3. RISING STAKES -- Each entry must top the last: a bigger claim, a better fact, a wilder detail.
4. TEASE NUMBER ONE -- Before the final entry, one line of deliberate delay to spike anticipation.
5. NUMBER-ONE PAYOFF -- The best material lives here. Land it, then a one-line outro. No trailing summary.

VOICE: High-energy, punchy, present tense. Short sentences. Every item earns its rank out loud.
SIGNATURE MOVES:
- Hook in the FIRST sentence of every segment: a stat, a paradox, or a wait-what fact.
- Justify placement: say why this item ranks above the previous one.
- Keep segments symmetric in length -- the rhythm is the product.
FORBIDDEN: Meandering intros. Entries without a payoff line. Filler padding ("this one speaks for itself").
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "ranked_countdown", "per_item_hook_payoff", "number_one_tease",
        ],
        "banned_tells": [
            "welcome to watchmojo",
            "don't forget to like and subscribe",
            "honorable mentions",
        ],
    },
},

    # === B-authorial ===
    "cosmic_poetic": {
        "display_name": "Cosmic Poetic",
        "aliases": [
            "cosmic poetic", "cosmic_poetic", "sagan", "carl sagan", "cosmos",
        ],
        "is_fiction": False,
        "medium_origin": "page",
        "tier": "P1",
        "tts_risk": False,
        "output_support": "both",
        "rag": {
            "query_instruction": (
                "Retrieve a lyrical science passage that moves between cosmic "
                "scale and human meaning, grounded in precise astronomical or "
                "physical detail:"
            ),
            "framing": (
                "Study how the writer EARNS wonder through precision -- real "
                "magnitudes, real physics -- then turns the cosmic scale back "
                "toward human meaning and humility. Notice the long flowing "
                "sentences broken by one short declarative for awe."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": """STYLE: Cosmic Poetic
= Science as reverence. The vast made intimate, the intimate made vast. Wonder earned through precision, delivered with humility.

STRUCTURE PER CHAPTER:
1. SMALL OBJECT OPENING -- Begin with one humble, concrete thing (a grain of sand, a candle flame, a shoreline at dusk) before any cosmic claim.
2. THE ZOOM -- Pull back stepwise from that object to planetary, stellar, or deep-time scale. Each step states a real magnitude.
3. THE TURN HOME -- Bring the cosmic scale back to human meaning: our fragility, our kinship with the universe, our shared origin.
4. HUMILITY CLOSE -- End on what we do not yet know, framed as invitation, never as defeat.

VOICE: First-person plural "we" -- humanity as one crew on one small world. Present tense for cosmic facts. Long flowing sentences broken by one short declarative for awe.
SIGNATURE MOVES:
- Deliver at least one scale comparison per chapter that dwarfs human experience (the age of the universe against a human life; atoms in a body against stars in a galaxy).
- Frame humanity from orbit at least once: how our borders, wars, and vanities look from a great distance.
- Express the humans-as-the-universe-observing-itself idea in FRESH words each time -- never quote the famous formulations.
- Treat the scientific method with tenderness: doubt, error, and correction rendered as heroic acts.

FORBIDDEN: Recycled catchphrases from the source exemplar. Mysticism that contradicts evidence. Cold fact-recitation without the turn to meaning.
REQUIRED: Real numbers and real physics beneath every poetic line. One humility beat ("we may be wrong") per chapter.
""",
        "style_rules_editor": "",
        "register_spec": {
            "required_moves": [
                "small_object_zoom",
                "scale_comparison",
                "orbit_perspective",
                "humility_close",
            ],
            "banned_tells": [
                "billions and billions",
                "pale blue dot",
                "star-stuff",
                "the cosmos is all that is or ever was or ever will be",
            ],
        },
    },

    "counterintuitive_thesis": {
        "display_name": "Counterintuitive Thesis",
        "aliases": [
            "counterintuitive thesis", "counterintuitive_thesis",
            "counterintuitive", "gladwell", "malcolm gladwell",
        ],
        "is_fiction": False,
        "medium_origin": "page",
        "tier": "P1",
        "tts_risk": False,
        "output_support": "both",
        "rag": {
            "query_instruction": (
                "Retrieve a narrative passage that opens on a specific named "
                "person and builds toward a surprising, evidence-backed "
                "general claim:"
            ),
            "framing": (
                "Study how the writer moves from ANECDOTE to STUDY to REVEAL. "
                "Notice how the obvious explanation is deliberately built up "
                "so the research can overturn it, and how the opening story "
                "returns at the end meaning the opposite of what it first seemed."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": """STYLE: Counterintuitive Thesis
= The confident essayist who proves the world works opposite to how you think. Story-driven social science with a reveal engine.

STRUCTURE PER CHAPTER:
1. NAMED-CHARACTER OPENER -- Begin with a specific person doing a specific thing on a specific day. Full name, place, one mundane detail. No thesis yet.
2. THE PUZZLE -- Show what is strange about this person's story. Let the reader silently form the obvious explanation.
3. THE STUDY -- Introduce research that demolishes the obvious explanation. Name the researcher, the institution, the finding, one striking number.
4. THE REVEAL -- State the counterintuitive rule the story and study together prove: we have been thinking about this backwards.
5. THE ECHO -- Re-run the opening anecdote through the new lens; the same facts now mean the opposite.

VOICE: Confident present tense for arguments, past tense for anecdotes. Direct implication of the reader ("you would assume..."). Conversational authority -- a brilliant dinner guest, never a lecturer.
SIGNATURE MOVES:
- Alternate anecdote -> study -> reveal in a loop; never stack two studies without a story between them.
- Build the wrong explanation on purpose so the reveal has something to break.
- Give minor characters one vivid humanizing detail (what they wore, what they ordered, how they laughed).
- Coin at most ONE fresh label for the chapter's phenomenon and reuse it as a refrain.

FORBIDDEN: Recycled pop-psychology brand phrases. Opening with the thesis. Hedging the reveal into mush.
REQUIRED: At least one named real study or dataset per chapter. The explicit flip: the conclusion must invert a belief stated earlier in the chapter.
""",
        "style_rules_editor": "",
        "register_spec": {
            "required_moves": [
                "named_character_opener",
                "anecdote_study_reveal",
                "belief_inversion",
                "anecdote_echo",
            ],
            "banned_tells": [
                "the tipping point",
                "the 10,000-hour rule",
                "thin-slicing",
                "the law of the few",
            ],
        },
    },

    # === C-audio ===
    "folklore_creepy": {
        "display_name": "Folklore — Measured Creepy",
        "aliases": [
            "folklore creepy", "folklore_creepy", "measured creepy",
            "dark folklore", "folklore horror", "lore", "mahnke", "aaron mahnke",
        ],
        "is_fiction": False,
        "medium_origin": "ear",
        "tier": "P1",
        "tts_risk": False,
        "output_support": "both",
        "rag": {
            "query_instruction": (
                "Retrieve a passage that recounts dark folklore, legend, or a "
                "historical mystery in a calm, restrained, documentary tone:"
            ),
            "framing": (
                "Study how the writer stays CALM while the material darkens — the even "
                "cadence, the understated delivery of disturbing facts, and how one specific "
                "local anecdote opens out into a universal human fear."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": """STYLE: Folklore -- Measured Creepy
= Calm, unhurried voice narrating dark material. The horror comes from restraint, never from the telling.

STRUCTURE PER CHAPTER:
1. ORDINARY DOORWAY -- Open with something mundane and safe: a town, an object, a custom. Let the reader settle before anything is wrong.
2. HISTORICAL ANECDOTE -- One documented case with names, dates, places. Tell it plainly, in sequence, like local history.
3. THE WIDENING -- Step back from the case to the pattern: other villages, other centuries, the same fear wearing different clothes.
4. UNIVERSAL TURN -- Close by naming, in your own words, the human fear this folklore was built to hold. Quiet, not loud.

VOICE: Third person. Past tense for the cases, present tense for reflection. Even, museum-guide register -- the narrator NEVER gets scared.
SIGNATURE MOVES:
- Understate every horror. "The child was never found" beats any adjective. Let the fact land, then leave silence after it.
- Deliver dark details in exactly the same calm cadence as the mundane ones. No exclamation marks, no breathless build.
- Treat folklore as evidence of what people FEARED, not as claims about what is real. Stay agnostic on the supernatural.
- End sections one beat early: a single short sentence, then stop.

FORBIDDEN: Jump-scare phrasing, gore lingered on, or the narrator declaring things "terrifying"/"horrifying" -- restraint IS the style.
FORBIDDEN: A verdict on whether the ghost, creature, or curse was real.
""",
        "style_rules_editor": "",
        "register_spec": {
            "required_moves": [
                "ordinary_doorway_open",
                "historical_anecdote",
                "understated_horror",
                "universal_fear_turn",
            ],
            "banned_tells": [
                "the scariest monsters are the ones we make",
                "the truth is more frightening than fiction",
            ],
        },
    },

    "sleep_story_adult": {
        "display_name": "Sleep Story — Adult",
        "aliases": [
            "sleep story", "sleep_story", "sleep story adult", "sleep_story_adult",
            "adult sleep story", "sleep", "calm", "calm app",
        ],
        "is_fiction": False,
        "medium_origin": "ear",
        "tier": "P1",
        "tts_risk": False,
        "output_support": "both",
        "rag": {
            "query_instruction": (
                "Retrieve a gentle, slow, second-person descriptive passage with "
                "soothing sensory detail and no plot tension:"
            ),
            "framing": (
                "Study how the writer SLOWS the reader down — the sentence rhythm that "
                "lengthens as the passage goes on, the soft sensory palette, and how the "
                "description keeps drifting forward without ever raising stakes."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": """STYLE: Sleep Story -- Adult
= Second-person descent into drowsiness. A story engineered to be abandoned halfway. Nothing happens, beautifully.

STRUCTURE PER CHAPTER:
1. ARRIVAL -- Place "you" somewhere safe and softly luxurious within the first lines: a night train, a lakeside cabin, an old library after closing.
2. SLOW DESCENT -- Move through the place one soft detail at a time. Make each paragraph slightly slower and lower-stakes than the one before.
3. ANTICLIMAX BY DESIGN -- Let questions dissolve instead of resolving. If a door appears, it opens onto another quiet room. No twist, ever.
4. FADE CLOSE -- End mid-calm, not at a destination. The final lines must work even if the listener never hears them.

VOICE: Second person ("you"), present tense throughout. Warm, low, adult register -- unhurried, never childish, never instructional.
SIGNATURE MOVES:
- LENGTHEN the sentences as the chapter goes on: begin short and simple, end in long, gently coiling clauses that carry no new information.
- Keep the sensory palette soft: warmth, weight, fabric, rain on glass, distant sound. Nothing sharp, sudden, cold, or bright.
- Cycle soothing motifs (the lamp, the rain, your breathing) on a slow return -- repetition here is a feature, not a flaw.
- Strip all urgency: no "suddenly", no deadlines, no unanswered danger, no reason to stay awake.

FORBIDDEN: Plot tension, cliffhangers, conflict, or any question the listener must stay awake to answer.
FORBIDDEN: Kids-bedtime register ("little one", closing morals) and meditation-app instructions ("now breathe in") -- this is a story, not a lesson or an exercise.
""",
        "style_rules_editor": "",
        "register_spec": {
            "required_moves": [
                "second_person_address",
                "slow_descent",
                "lengthening_sentences",
                "anticlimax_close",
            ],
            "banned_tells": [
                "welcome to calm",
                "nowhere to go, nothing to do",
            ],
        },
    },

    # === D-cinematic ===

"epic_fantasy_prologue": {
    "display_name": "Epic Fantasy Prologue",
    "aliases": [
        "epic fantasy prologue", "epic_fantasy", "fantasy prologue",
        "mythic prologue", "lotr", "lord of the rings",
    ],
    "is_fiction": True,
    "medium_origin": "ear",
    "tier": "P1",
    "tts_risk": False,
    "output_support": "both",
    "rag": {
        "query_instruction": (
            "Retrieve a mythic prologue or legend-opening passage that "
            "compresses ages of invented history into sweeping, elevated narration:"
        ),
        "framing": (
            "Study how the writer compresses DEEP TIME into a few paragraphs and "
            "hangs an entire history on one object or prophecy. Notice the elevated "
            "cadence and how invented proper nouns carry the weight of ages."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": """STYLE: Epic Fantasy Prologue
= Mythic scene-setting voiceover. An ancient witness recounts how the world came to its present peril.

STRUCTURE:
1. PREAMBLE -- Declare, in your OWN words, that an age has ended and the world stands altered. Never quote any franchise line.
2. DEEP-TIME EXPOSITION -- Compress ages into paragraphs: forging, flourishing, betrayal, fall. Give each era one vivid emblem (a crown, a burning city, a buried blade).
3. ARTIFACT / PROPHECY FRAME -- Hang the entire history on one object or foretelling whose fate drives the present story.
4. THRESHOLD CLOSE -- End where the story begins: the artifact resurfaces, the prophecy stirs, the burden passes to unlikely hands.

VOICE: Omniscient ancient narrator. Past tense for the ages; shift to present tense only in the final beat. Elevated but SPEAKABLE -- written for the ear.
SIGNATURE MOVES:
- Invent proper nouns for kingdoms, ages, and artifacts; let the names carry the weight of history.
- Roll the ages in parallel cadence ("For an age... and for an age after...") -- parallelism of your own making.
- Deliver loss as inevitability: name what was forgotten, what slept, what waited.

FORBIDDEN: Modern idiom or irony. Any verbatim name, place, or line from an existing fantasy franchise.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "world_changed_preamble", "deep_time_compression", "artifact_prophecy_frame",
        ],
        "banned_tells": [
            "the world is changed", "history became legend, legend became myth",
            "one ring to rule them all", "middle-earth",
        ],
    },
},

"trailer_voice": {
    "display_name": "Trailer Voice",
    "aliases": [
        "trailer voice", "trailer_voice", "trailer", "movie trailer",
        "epic trailer", "promo voice",
    ],
    "is_fiction": False,
    "medium_origin": "ear",
    "tier": "P1",
    "tts_risk": False,
    "output_support": "video_only",
    "rag": {
        "query_instruction": (
            "Retrieve a passage of short, punchy, high-stakes lines with "
            "escalating rhythm and dramatic sentence fragments:"
        ),
        "framing": (
            "Study the RHYTHM of escalation -- how each short line raises the stakes "
            "and cuts everything that does not. Notice fragments doing the work of "
            "full sentences and the quotable final line."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": """STYLE: Trailer Voice
= Movie-trailer narration for 60-90 second promos. Punchy fragments, escalating stakes, a title-card ending.

STRUCTURE:
1. COLD HOOK -- One line that plants the ordinary world or the hero. A fragment is allowed.
2. DISRUPTION -- The turn that shatters the ordinary ("But..." / "Until..."). Name the stakes plainly.
3. ESCALATION LADDER -- Three to five beats, each shorter and higher-stakes than the last, until everything is on the line.
4. TITLE-CARD CLOSE -- Final line lands like the title card: five words or fewer, quotable, spoken into silence.

VOICE: Omniscient tease or direct second person. Present tense ONLY. Gravel-and-drums register: every line must survive being spoken over a bass hit.
SIGNATURE MOVES:
- Write in fragments. One image per line. Cut any clause that does not raise the stakes.
- Lead with punch verbs: "Run." "Fight." "Remember."
- Withhold: tease the premise, never explain the plot.
- The phrase "in a world" may appear AT MOST once per script, and never as the opening line.

FORBIDDEN: Full plot summary. Paragraphs longer than two sentences. More than one rhetorical question per script.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "fragment_cadence", "stakes_escalation", "title_card_close",
        ],
        "banned_tells": ["in a world"],
    },
},

"gothic_cosmic_horror": {
    "display_name": "Gothic / Cosmic Horror",
    "aliases": [
        "gothic horror", "cosmic horror", "gothic_cosmic_horror", "gothic",
        "lovecraft", "lovecraftian", "poe", "eldritch horror",
    ],
    "is_fiction": True,
    "medium_origin": "page",
    "tier": "P1",
    "tts_risk": False,
    "output_support": "both",
    "rag": {
        "query_instruction": (
            "Retrieve a gothic or cosmic horror passage where dread builds through "
            "implication, unreliable perception, and archaic formal diction:"
        ),
        "framing": (
            "Study how dread accumulates WITHOUT the horror ever being shown -- the "
            "narrator doubts their own senses while the prose stays formal, archaic, "
            "and controlled. Notice how documents and scholarly hedging make the "
            "impossible feel corroborated."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": _P1_HORROR_BASE + """VARIANT: Gothic / Cosmic Horror (Poe-to-Lovecraft register)
STRUCTURE:
1. CONFESSION OPEN -- A learned first-person narrator sets down an account they dread to write, hinting the tale has already ruined them.
2. FORBIDDEN-KNOWLEDGE ARC -- Curiosity -> inquiry -> the archive, ruin, or manuscript -> a truth that should have stayed buried.
3. UNRAVELING CLOSE -- Certainty collapses; the account ends at the threshold of what was finally seen, described only by its shadow.
VOICE: First person, past tense, formal ARCHAIC diction -- long periodic sentences, Latinate vocabulary, scholarly hedges ("I cannot say with surety").
SIGNATURE MOVES:
- Make the narrator's SENSES unreliable: they doubt the lamplight, the angles of a room, their own memory -- and confess the doubt.
- Treat documents as dread objects: letters, parish records, ship logs that corroborate the impossible.
- Render scale as horror: geometry, aeons, cosmic indifference -- humanity as a footnote in something older.
FORBIDDEN: Named entities, books, or places from existing horror canons. Contemporary slang inside the archaic register.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "unreliable_senses", "forbidden_knowledge_arc",
            "archaic_diction", "dread_by_implication",
        ],
        "banned_tells": [
            "cthulhu", "necronomicon", "non-euclidean", "nevermore",
        ],
    },
},

"internet_horror": {
    "display_name": "Internet Horror — First Person",
    "aliases": [
        "internet horror", "internet horror first-person", "internet_horror",
        "nosleep", "r/nosleep", "creepypasta", "first person horror",
    ],
    "is_fiction": True,
    "medium_origin": "page",
    "tier": "P1",
    "tts_risk": False,
    "output_support": "both",
    "rag": {
        "query_instruction": (
            "Retrieve a first-person confessional horror passage told in plain "
            "contemporary prose, where ordinary rules or routines turn menacing:"
        ),
        "framing": (
            "Study how a casual, believable first-person voice makes the impossible "
            "feel documented. Notice the flat statement of strange rules, the mundane "
            "setting, and how the ending refuses to resolve."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": _P1_HORROR_BASE + """VARIANT: Internet Horror -- First Person (forum-testimony register)
STRUCTURE:
1. TESTIMONY OPEN -- State plainly, in your OWN words, that this really happened to the narrator -- and why they are only telling it now.
2. RULES-BASED DREAD -- A list of arbitrary rules arrives early (a job, a landlord, a note). Dread = each rule's purpose revealing itself when broken.
3. AMBIGUOUS ENDING -- Resolve nothing. The narrator survives changed, still notices one small wrong thing, and stops on an unanswered question.
VOICE: First person, past tense sliding into present at the end. Casual contemporary register -- contractions, asides, self-doubt ("I know how this sounds").
SIGNATURE MOVES:
- Number the rules and state them flat; break them one by one, out of order.
- Keep the setting mundane and employed: night shift, house-sit, delivery route -- and the pay was a little too good.
- Corroborate with texture: timestamps, screenshots described, a manager who stops answering.
FORBIDDEN: Neat explanations or defeated monsters. Creatures, sites, or lore borrowed from famous internet horror stories.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "testimony_open", "rules_list_dread", "ambiguous_ending",
        ],
        "banned_tells": [
            "slenderman", "the backrooms", "jeff the killer", "smile dog",
        ],
    },
},

"warm_omniscient": {
    "display_name": "Warm Omniscient",
    "aliases": [
        "warm omniscient", "warm_omniscient", "gentle narrator",
        "warm narrator", "freeman", "morgan freeman",
    ],
    "is_fiction": False,
    "medium_origin": "ear",
    "tier": "P1",
    "tts_risk": False,
    "output_support": "both",
    "rag": {
        "query_instruction": (
            "Retrieve a warm, reflective narration passage where a gentle omniscient "
            "voice draws universal meaning from one small human moment:"
        ),
        "framing": (
            "Study the PATIENCE of the voice -- unhurried sentences, gentle authority, "
            "affection for its subjects. Notice how the passage closes on an earned, "
            "humanity-affirming note rather than a lesson."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": """STYLE: Warm Omniscient
= Gentle authoritative narration -- a wise, unhurried voice that regards humanity with patient affection.

STRUCTURE:
1. QUIET OPENING -- Begin small and human: one person, one morning, one ordinary act, observed with tenderness.
2. WIDENING ARC -- Pivot from the small scene to the larger truth it carries; let the view rise without losing the person.
3. HUMANITY-AFFIRMING CLOSE -- End on earned warmth: what this says about who we are, delivered softly, never preached.

VOICE: Third-person omniscient, present tense preferred. Measured, PATIENT pacing -- medium-length sentences with room to breathe; no rush, no hype.
SIGNATURE MOVES:
- Speak of "we" and "us" at the pivot points -- include the listener in the species, not in a lecture.
- Deliver hard facts gently: acknowledge darkness plainly, then find the human light beside it without denying the dark.
- Place one deliberate pause-beat per section: a short sentence that lands, and rests. Like this one.
- Wry, kind humor allowed once per chapter -- a smile, never a joke at anyone's expense.

FORBIDDEN: Cynicism, sarcasm, urgency. Motivational-poster platitudes ("everything happens for a reason").
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "small_to_universal_pivot", "gentle_authority", "humanity_affirming_close",
        ],
        "banned_tells": [
            "hope is a good thing", "get busy living or get busy dying",
        ],
    },
},

    # === E-nusantara ===
    "dongeng_nusantara": {
        "display_name": "Dongeng Nusantara — Archipelago Folk Tale",
        "aliases": [
            "dongeng", "dongeng nusantara", "dongeng_nusantara", "dongeng rakyat",
            "cerita rakyat", "fabel", "kancil", "indonesian folk tale",
        ],
        "is_fiction": True,
        "medium_origin": "ear",
        "tier": "P1",
        "tts_risk": False,
        "output_support": "both",
        "rag": {
            "query_instruction": (
                "Retrieve a folk-tale passage with a formulaic once-upon-a-time "
                "opening, talking animals or humble village characters, and an "
                "explicitly stated closing moral:"
            ),
            "framing": (
                "Study the oral storyteller cadence: short breathing sentences, "
                "repetition in threes, and how the tale earns its spoken moral. "
                "Notice how one dominant trait (greed, cunning, honesty) drives "
                "the character's fate."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": """STYLE: Dongeng Nusantara
= Suara pendongeng rakyat: lisan, hangat, berirama; tokoh binatang/rakyat kecil; moral diucapkan terang-terangan di akhir.

STRUKTUR:
1. PEMBUKA FORMULA -- Buka dengan formula lisan: "Pada zaman dahulu kala...", "Alkisah...", atau "Konon, di sebuah desa...". Tempat dan tokoh muncul dalam dua kalimat pertama.
2. SATU SIFAT DOMINAN -- Protagonis = binatang yang bisa bicara atau rakyat kecil (petani, nelayan, janda miskin). Beri SATU sifat penentu nasib: cerdik, serakah, jujur, sombong.
3. POLA TIGA -- Bangun konflik lewat pengulangan tiga (tiga ujian, tiga tipu daya, tiga hari). Ulangi frasa kunci hampir kata per kata di setiap putaran -- pengulangan adalah fitur lisan, bukan bug.
4. AKIBAT SETIMPAL -- Nasib lahir dari sifat: yang serakah kehilangan segalanya, yang jujur diganjar. Rantai sebab-akibat harus bisa diikuti anak kecil.
5. MORAL PENUTUP EKSPLISIT -- Tutup dengan pesan moral yang DIUCAPKAN langsung dalam 1-2 kalimat: "Sejak saat itu... Itulah sebabnya kita tidak boleh serakah."

SUARA: Orang ketiga, kala lampau, register pendongeng yang hangat dan sabar. Kalimat pendek yang enak dibacakan keras; sesekali sapa pendengar ("Nah,...", "Coba tebak...").

GERAKAN KHAS:
- Beri dialog tokoh dengan panggilan khas dongeng ("Hai, Sahabatku...", "Wahai Pak Tani...").
- Selipkan onomatope dan bunyi alam sebagai bumbu adegan (gemericik, kokok ayam, "BRUK!").
- Pakai benda dunia desa: ketupat, sawah, sumur, pasar, lesung -- bukan benda modern.

DILARANG: Kekerasan grafis, kosakata modern/asing (handphone, kantor), dan sinisme -- dunia dongeng itu polos dan adil.
DILARANG: Moral yang hanya tersirat -- pesan penutup WAJIB diucapkan eksplisit.
""",
        "style_rules_editor": "",
        "register_spec": {
            "required_moves": [
                "formula_opening",
                "animal_folk_protagonist",
                "rule_of_three",
                "explicit_closing_moral",
            ],
            "banned_tells": [
                "si kancil anak nakal",
                "suka mencuri ketimun",
                "bawang merah bawang putih",
                "timun mas",
            ],
        },
    },

    "horor_viral_indonesia": {
        "display_name": "Horor Viral Indonesia — Viral Horror Thread",
        "aliases": [
            "horor viral", "horor viral indonesia", "horor_viral_indonesia",
            "horror viral", "horor thread", "thread horor", "simpleman",
            "kkn di desa penari", "kkn", "creepypasta indonesia", "horor kampung",
        ],
        "is_fiction": True,
        "medium_origin": "page",
        "tier": "P1",
        "tts_risk": False,
        "output_support": "both",
        "rag": {
            "query_instruction": (
                "Retrieve a first-person horror passage told casually as a true "
                "personal experience, where dread escalates after a local warning "
                "or taboo is ignored:"
            ),
            "framing": (
                "Study how the narrator's plain, confiding voice makes the "
                "supernatural credible. Notice the slow escalation -- mundane "
                "detail first, the stated rule, the violation, then consequences "
                "arriving in stages with false lulls between them."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": """STYLE: Horor Viral Indonesia
= Horor gaya thread viral: orang pertama "gue", diklaim kisah nyata, setting KKN/kampung, pantangan dilanggar, dread menetes pelan.

STRUKTUR:
1. PEMBUKA KESAKSIAN -- Buka seperti membuka thread: klaim pengalaman nyata + penyamaran identitas ("Ini kejadian waktu gue KKN. Nama orang dan desa gue samarkan."). Janjikan cerita panjang.
2. DUNIA NORMAL DULU -- Habiskan bagian awal untuk hal biasa: perjalanan, posko, warga ramah, jadwal program. Selipkan satu-dua kejanggalan kecil TANPA menjelaskannya.
3. PANTANGAN DISEBUT SPESIFIK -- Tokoh tua setempat (juru kunci, pak kades, mbah) mengucapkan larangan yang konkret dan bisa dilanggar: jangan lewat jalan itu selepas magrib, jangan ambil apa pun dari tempat itu.
4. PELANGGARAN + ESKALASI MENETES -- Seseorang melanggar, sering tanpa sadar atau karena hal sepele. Naikkan gangguan bertahap: bunyi -> bau -> mimpi -> sosok. SATU gangguan per beat; beri jeda "aman" palsu di antaranya.
5. PUNCAK MAHAL + GANTUNG -- Klimaks singkat dengan harga nyata (kesurupan, sakit tak wajar, seseorang berubah selamanya). Tutup menggantung: satu pertanyaan tak terjawab + pengakuan narator masih memikirkannya sampai sekarang.

SUARA: Orang pertama "gue/kami", kala lampau, bahasa tutur santai khas thread (boleh "sumpah", "anjir", "gue merinding") -- TANPA format platform: tanpa username, timestamp, atau "RT". Narator bukan pemberani: dia menawar logika ("mungkin cuma kucing") dan baru percaya setelah terlambat.

GERAKAN KHAS:
- Taburkan detail duniawi yang terasa bisa diverifikasi (sinyal hilang, merek kopi sachet, jadwal ronda) untuk menopang klaim "kisah nyata".
- Buat warga lokal tahu lebih banyak daripada yang mereka katakan; jawaban mereka pendek dan mengelak.
- Saat takut memuncak, patahkan kalimat jadi pendek-pendek; biarkan satu paragraf hanya berisi "Gue diem. Lama."

DILARANG: Jump-scare instan tanpa penantian, dan penjelasan tuntas tentang wujud/asal makhluknya -- misteri harus tersisa.
DILARANG: Bahasa baku kaku, dan menyalin nama tempat/tokoh dari kisah viral yang sudah ada.
""",
        "style_rules_editor": "",
        "register_spec": {
            "required_moves": [
                "gue_first_person",
                "stated_pantangan",
                "slow_burn_escalation",
                "unresolved_ending",
            ],
            "banned_tells": [
                "kkn di desa penari",
                "desa penari",
                "badarawuhi",
            ],
        },
    },

    "legenda_asal_usul": {
        "display_name": "Legenda Asal-Usul — Origin Legend",
        "aliases": [
            "legenda", "legenda asal usul", "legenda_asal_usul", "legenda asal-usul",
            "asal usul", "asal-usul", "cerita asal usul", "legenda rakyat",
            "sangkuriang", "malin kundang", "origin legend",
        ],
        "is_fiction": True,
        "medium_origin": "ear",
        "tier": "P1",
        "tts_risk": False,
        "output_support": "both",
        "rag": {
            "query_instruction": (
                "Retrieve a legend or origin-myth passage that explains how a "
                "place, landmark, or natural feature came to exist:"
            ),
            "framing": (
                "Study the etiological arc: human desire, a broken oath or taboo, "
                "a spoken curse, then transformation -- and how the landscape "
                "itself becomes the story's proof. Notice the tragic inevitability "
                "and the place-name held back as the final payoff."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": """STYLE: Legenda Asal-Usul
= Legenda etiologis: kenapa gunung/danau/batu ini ada. Arc tragis, kutukan/sumpah sebagai engsel, nama tempat sebagai payoff penutup.

STRUKTUR:
1. PEMBUKA DUNIA LAMA -- Buka dengan formula lisan bernuansa tempat: "Dahulu kala, di tanah yang kini kita kenal...". JANGAN bocorkan nama tempatnya di awal -- simpan sebagai payoff.
2. TOKOH BERHASRAT -- Manusia dengan keinginan kuat yang bisa dipahami: ibu menanti anak pulang, pemuda jatuh cinta, anak ingin kaya. Hasrat itu bahan bakar cerita sekaligus benih bencana.
3. PELANGGARAN JANJI/TABU -- Tokoh melanggar yang sakral: janji diingkari, ibu disangkal, syarat mustahil dicurangi. Tunjukkan momen pelanggaran sebagai ADEGAN, bukan ringkasan.
4. KUTUKAN/SUMPAH DIUCAPKAN -- Pihak yang terluka mengucapkan kutukan DALAM DIALOG LANGSUNG, dengan kata-kata bertuah yang berirama. Buat alam ikut bereaksi: langit menghitam, angin berhenti, gemuruh.
5. TRANSFORMASI + NAMA SEBAGAI PAYOFF -- Orang/benda menjelma fitur alam. Kalimat penutup mengunci etimologi: "Itulah sebabnya sampai hari ini tempat itu dinamai..." dan jejaknya "masih dapat dilihat sampai sekarang."

SUARA: Orang ketiga, kala lampau, register pendongeng lisan yang lebih muram dan agung daripada dongeng anak. Boleh diksi arkais secukupnya (konon, syahdan, tatkala) -- jangan berlebihan.

GERAKAN KHAS:
- Jahit geografi sebagai bukti: bentuk bukit, warna air, jumlah mata air -- fitur alam nyata lahir dari akibat cerita.
- Bangun ironi tragis: pendengar melihat bencana datang sebelum tokohnya sadar.
- Buat kutukan setimpal dan tak bisa ditarik; penyesalan datang SETELAH semuanya terlambat.

DILARANG: Akhir bahagia yang membatalkan kutukan, dan moral eksplisit gaya dongeng anak -- biarkan tragedi dan nama tempat yang bicara.
DILARANG: Menyalin tokoh/alur legenda kanon yang sudah ada -- ciptakan legenda BARU dengan pola yang sama.
""",
        "style_rules_editor": "",
        "register_spec": {
            "required_moves": [
                "broken_oath_or_taboo",
                "spoken_curse",
                "transformation_to_landscape",
                "place_name_payoff",
            ],
            "banned_tells": [
                "malin kundang",
                "sangkuriang",
                "tangkuban perahu",
                "dikutuk menjadi batu",
            ],
        },
    },

    # === F-edu (Edukasi & Motivasi) ===
    "first_principles": {
        "display_name": "First-Principles Teaching",
        "aliases": [
            "first principles", "first_principles", "first-principles",
            "first principles teaching", "feynman", "feynman teaching",
            "build from zero",
        ],
        "is_fiction": False,
        "medium_origin": "ear",
        "tier": "P1",
        "tts_risk": False,
        "output_support": "both",
        "rag": {
            "query_instruction": (
                "Retrieve a passage where a teacher explains a complex concept "
                "from first principles using simple, everyday analogies:"
            ),
            "framing": (
                "Study how the writer REBUILDS the idea from zero — one step at a "
                "time, each step anchored to something the reader already knows. "
                "Notice where they admit uncertainty and treat it as the exciting part."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": """STYLE: First-Principles Teaching
= A brilliant teacher rebuilding an idea from zero, out loud, with homemade analogies and open delight in what nobody knows.

STRUCTURE PER CHAPTER:
1. STRIP-THE-JARGON OPENING -- Restate the topic as a plain question a curious twelve-year-old could ask. Throw the textbook definition away.
2. BUILD FROM ZERO -- Start from something the listener already knows (a ball, water, a hot cup, a spring) and add ONE idea per step. Never skip a step.
3. TEST THE PICTURE -- Push the analogy until it breaks, and say out loud where it breaks: "Now here the picture stops working -- and that's the interesting part."
4. HONEST CLOSE -- End on the live edge of knowledge: what nobody can explain yet, framed as a pleasure, not a failure.

VOICE: First person "I" thinking aloud plus direct "you"; present tense; informal, playful, precise. Written for the ear -- short spoken sentences, audible self-corrections allowed ("no, wait -- simpler than that").

SIGNATURE MOVES:
- Invent HOMEMADE analogies from kitchens, streets, toys -- never stock textbook analogies (no solar-system atom).
- Ask "but WHY does that happen?" at least twice per chapter, going one layer deeper each time.
- Admit ignorance with visible delight: "and the honest answer is nobody knows -- isn't that wonderful?"
- Make numbers felt before named ("a wire thinner than a hair") -- quantity as sensation first, figure second.

FORBIDDEN: Argument from authority ("scientists say", "it is well established") -- every claim must be rebuilt, not cited.
FORBIDDEN: Unexplained jargon -- a technical term may appear only AFTER its meaning was built from plain parts.
""",
        "style_rules_editor": "",
        "register_spec": {
            "required_moves": [
                "build_from_zero",
                "homemade_analogy",
                "delight_in_not_knowing",
            ],
            "banned_tells": [
                "surely you're joking",
                "the pleasure of finding things out",
                "what i cannot create, i do not understand",
            ],
        },
    },

    "stoic_daily": {
        "display_name": "Stoic Daily",
        "aliases": [
            "stoic", "stoic daily", "stoic_daily", "stoicism", "daily stoic",
            "aurelius", "marcus aurelius", "seneca", "epictetus",
            "ryan holiday", "memento mori",
        ],
        "is_fiction": False,
        "medium_origin": "page",
        "tier": "P1",
        "tts_risk": False,
        "output_support": "both",
        "rag": {
            "query_instruction": (
                "Retrieve an aphoristic, meditative passage on discipline, "
                "mortality, or accepting what cannot be controlled:"
            ),
            "framing": (
                "Study the COMPRESSION — short, self-contained sentences that give "
                "instruction rather than argument. Notice how the writer moves from "
                "a timeless principle to a concrete daily action."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": """STYLE: Stoic Daily
= Aphoristic page meditation: an ancient line, a modern application, an order you give yourself. Calm, stern, mortal.

STRUCTURE PER CHAPTER:
1. ANCIENT ANCHOR OPENING -- Open with one short quotation or close paraphrase from a Stoic (Marcus Aurelius, Seneca, Epictetus), attributed, then stop. Let it sit alone.
2. TRANSLATE TO TODAY -- One concrete modern scene where the line bites: a rude email, a missed promotion, traffic, a diagnosis.
3. THE INSTRUCTION -- Turn the insight into imperatives addressed to the reader: "Do the work. Expect nothing. Begin again."
4. MEMENTO MORI CLOSE -- End by setting the day against death or lost time -- quiet, factual, never morbid theatrics.

VOICE: Second-person imperative dominates; present tense; plain, compressed, unhurried register. Sentences short enough to be carved. Paragraphs of 2-4 sentences, each able to stand alone.

SIGNATURE MOVES:
- Write at least one aphorism per section that survives out of context.
- Divide, explicitly, what is in the reader's control from what is not -- then dismiss the second half in a single sentence.
- Reframe the obstacle as material: the annoyance IS the training.
- Give no comfort without a demand attached.

FORBIDDEN: Motivational hype, exclamation marks, "you've got this" cheer -- the register is stern calm, not a pep talk.
FORBIDDEN: Hedging qualifiers ("maybe", "perhaps try", "you might consider") -- the mode is instruction, not suggestion.
""",
        "style_rules_editor": "",
        "register_spec": {
            "required_moves": [
                "imperative_aphorism",
                "ancient_quote_modern_application",
                "memento_mori_beat",
            ],
            "banned_tells": [
                "the obstacle is the way",
                "ego is the enemy",
                "stillness is the key",
            ],
        },
    },

    "motivational_grind": {
        "display_name": "Motivational Grind",
        "aliases": [
            "motivational", "motivation", "grind", "motivational grind",
            "motivational_grind", "goggins", "hard motivation", "no excuses",
            "discipline motivation",
        ],
        "is_fiction": False,
        "medium_origin": "ear",
        "tier": "P1",
        "tts_risk": False,
        "output_support": "both",
        "rag": {
            "query_instruction": (
                "Retrieve a forceful passage of direct address about hardship, "
                "discipline, and overcoming through relentless effort:"
            ),
            "framing": (
                "Study the CADENCE — how sentence length shortens as intensity "
                "rises, and how the writer confronts the reader directly. Notice "
                "the physical, concrete images of effort over abstract success-talk."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": """STYLE: Motivational Grind
= A drill voice in your ear: confrontational second person, no sympathy, cadence that builds from a mutter to a charge.

STRUCTURE PER CHAPTER:
1. CALL-OUT OPENING -- First line accuses the listener of the exact comfort they are hiding in: "You hit snooze again. You know you did."
2. STRIP THE EXCUSE -- Name the excuse, repeat it back in the listener's own words, then dismantle it with one hard fact or one cornering question.
3. OBSTACLE AS GIFT -- Reframe the pain point as the exact training the listener ordered: the rejection, the 4 a.m. dark, the doubters -- fuel, all of it.
4. THE CHARGE -- Close with stacked short imperatives that accelerate: sentences get shorter, verbs harder, until the final line is one to three words.

VOICE: Second person "you", present tense, spoken register. Sentence rhythm IS the argument: long build, short strike. Repetition is deliberate -- repeat the key phrase three times with rising force.

SIGNATURE MOVES:
- Ask questions that corner, not comfort: "What did you do with the last hour? Exactly."
- Hammer contrast pairs on a drumbeat: "They sleep. You work. They talk. You count reps."
- Plant one concrete physical image of effort per section -- sweat on the floor, taped hands, the last rep nobody sees.
- Address the listener's inner negotiator directly and overrule it mid-sentence.

FORBIDDEN: Self-pity, apology, or softening frames ("it's okay to rest") -- rest exists only as earned, never offered.
FORBIDDEN: Abstract success-talk without a named physical action the listener must take today.
""",
        "style_rules_editor": "",
        "register_spec": {
            "required_moves": [
                "confrontational_second_person",
                "obstacle_as_gift",
                "cadence_build",
            ],
            "banned_tells": [
                "stay hard",
                "who's gonna carry the boats",
                "taking souls",
                "callous your mind",
            ],
        },
    },

    "business_case": {
        "display_name": "Business Case Narrative",
        "aliases": [
            "business case", "business_case", "business case narrative",
            "acquired", "founder story", "how x built y", "business story",
            "startup story", "rise of a company",
        ],
        "is_fiction": False,
        "medium_origin": "ear",
        "tier": "P1",
        "tts_risk": False,
        "output_support": "both",
        "rag": {
            "query_instruction": (
                "Retrieve a narrative passage about a business, venture, or "
                "leader facing a high-stakes decision, with concrete figures:"
            ),
            "framing": (
                "Study how the writer turns DECISIONS into drama — stakes laid out "
                "before the outcome is revealed, and numbers delivered as reveals. "
                "Notice how character choices, not market forces, drive the story."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": """STYLE: Business Case Narrative
= "How X built Y" told as drama: founder arc, decision points as cliffhangers, numbers delivered like plot twists. Podcast-native.

STRUCTURE PER CHAPTER:
1. STAKES-FIRST OPENING -- Open at the moment before a decision that could kill the company: the bank balance, the deadline, the term sheet on the table.
2. REWIND THE ARC -- Then earn that moment: where the founder started, the insight everyone laughed at, the early grind in a specific room (garage, dorm, back office).
3. DECISION POINTS AS CLIFFHANGERS -- Frame each strategic choice as a live fork: lay out both options and their cost, let the listener choose, THEN reveal what they did and what it cost.
4. LEDGER CLOSE -- End on a number that reframes everything ("the division they almost sold now earns more than the whole company did then") plus ONE transferable lesson, stated once, plainly.

VOICE: Enthusiast-narrator "we/you" spoken register -- a smart friend who did the homework. Present tense at decision points, past tense in the rewind.

SIGNATURE MOVES:
- Treat numbers as DRAMA: set the expectation first, then land the figure as a reveal, rounded for the ear ("not ten million. A hundred million.").
- Name real people making real calls -- the deciding meeting, who said no, who signed anyway.
- Track ONE recurring metric across the chapter (users, margin, cash) so its final value lands as a payoff.
- Signpost the pivot audibly: "And this is the decision that changes everything."

FORBIDDEN: MBA abstraction ("synergies", "leveraging core competencies") without a concrete event attached.
FORBIDDEN: Hero worship -- every triumph is priced with what it cost or nearly cost.
""",
        "style_rules_editor": "",
        "register_spec": {
            "required_moves": [
                "founder_arc",
                "decision_cliffhanger",
                "numbers_as_drama",
            ],
            "banned_tells": [
                "welcome to acquired",
                "the podcast about great technology companies",
            ],
        },
    },

    # ── SPEC v1 §12.3/§13 — style_spec instance #2. Distinct from harari:
    # scene-open + woven closing-aphorism + "what the standard account omits" turn;
    # no forced temporal-zoom; over-analysis breaking scene is a banned_tell; the
    # omits-turn is capped as a near-duplicate. Confirmed live on the Louis XIV run.
    "creative_non_fiction": {
        "display_name": "Creative Non-Fiction",
        "aliases": [
            "creative non-fiction", "creative_non_fiction", "cnf",
            "literary non-fiction", "literary nonfiction", "narrative non-fiction",
            "narrative nonfiction", "book-length essay",
        ],
        "is_fiction": False,
        "medium_origin": "page",
        "tier": "P1",
        "tts_risk": False,
        "output_support": "book",
        "rag": {
            "query_instruction": (
                "Retrieve a literary non-fiction passage that opens on a scene with "
                "sensory grounding and closes on a woven aphorism — subordination "
                "intact, no signposting, exact figures rendered as book-digits:"
            ),
            "framing": (
                "Study how the writer opens each chapter at ground level (scene, "
                "object, moment) before analysis, weaves a closing aphorism into the "
                "final paragraph, and — at most twice per piece — turns to what the "
                "standard account omits. Notice that the register stays formal without "
                "arcane vocabulary."
            ),
            "min_quality": 4,
            "top_k": 3,
        },
        "style_rules_core": """STYLE: Creative Non-Fiction
= Book-length literary non-fiction: scene-first, argument-second, aphorism at close.

STRUCTURE PER CHAPTER:
1. SCENE OPEN -- Every chapter opens at ground level: a person, a place, an object, a moment. No statistic is the first move.
2. SUBORDINATED PROSE -- Paragraphs run long, subordination intact; short punctuating sentences appear as drama, not as default rhythm.
3. WOVEN CLOSING APHORISM -- The closing paragraph earns ONE aphorism woven into the scene; never as a standalone maxim.
4. OMITS-TURN (AT MOST TWICE) -- "What the standard account omits…" or its equivalent may appear at most TWO times in the whole piece; more = near-duplicate rewrite.

VOICE: Third person omniscient with close-focus periods. Formal literary register without arcane vocabulary. Present tense for scene, past tense for consequence.

SIGNATURE MOVES:
- Choose sensory detail for scene-opening, never for erudition display.
- Use short punctuation-sentences ("He was forty-nine.") between longer periods for cadence.
- Render numbers as digits (357 miroirs, 30 000 workers), never spelled out on the book path.
- Attribute contested figures once; do not repeat the attribution in every paragraph.

FORBIDDEN: Over-analysis breaking the scene mid-paragraph (analyze after scene closes).
FORBIDDEN: "Historians disagree" / "some argue" hedges used as a rhetorical crutch — commit or drop.
FORBIDDEN: Signposting ("in this chapter I will show…"). The book path never signposts.
""",
        "style_rules_editor": "",
        "register_spec": {
            "required_moves": [
                "scene_open",
                "woven_closing_aphorism",
            ],
            "banned_tells": [
                "in this chapter",
                "as we will see",
                "historians debate whether",
                "some argue that",
                "what is remarkable is",
            ],
            "counters": {
                "omits_turn_max": 2,
                "aphorism_density_target": 1,  # ~1 per chapter
                "citations_max": 3,
                "same_scholar_max": 3,
                "temporal_zoom_required": False,  # explicit anti-harari signal
            },
        },
    },
}


# ─────────────────────────────────────────────────────────────────────────
# Phase 2 (2026-07-05) — Nusantara style_spec seeds, gated on DALANG_INFRA_FIXES=1.
# Seeded from corpus samples 17 (Babad Tanah Jawi, id) + 19 (Cepot Membangun Kahyangan, su).
# Both introduce factual_regime values distinct from the existing 'factual'/'fictional' pair:
#   - babad_hikayat: 'mythic_history' — historical events + legendary silsilah + wahyu hybrid.
#     Cross-judge validated (n=2 reviewers) on Babad Tanah Jawi lens-1 8.9 + lens-2 8.5.
#   - pewayangan_dalang: 'mythic_narrative' — wayang cosmology with dalang direct-address,
#     kayon/blencong/kelir stagecraft, punakawan tradition. Cepot lens-1 9.0 + lens-2 8.5.
# ─────────────────────────────────────────────────────────────────────────

import os as _os_phase2

def _PHASE2_ON() -> bool:
    """Same env knob as narasi_gate._INFRA_FIXES_ON — one flip activates the whole
    corpus-audit bundle across gate + counters + pakem. Flag-OFF preserves prior
    behavior; the two new styles fall through to CATEGORY_DEFAULTS as unknown-style."""
    return _os_phase2.environ.get("DALANG_INFRA_FIXES") == "1"


_BABAD_HIKAYAT_SPEC = {
    "display_name": "Babad / Hikayat — Archaic Court Chronicle",
    "aliases": [
        "babad", "hikayat", "babad_hikayat", "babad-hikayat",
        "chronicle", "court chronicle", "kronik istana",
        "silsilah", "babad tanah jawi",
    ],
    "is_fiction": False,
    "category": "E",
    "medium_origin": "page",
    "tier": "P1",
    "tts_risk": False,
    "output_support": "both",
    "factual_regime": "mythic_history",
    "rag": {
        "query_instruction": (
            "Retrieve a court-chronicle passage in babad/hikayat register, "
            "written by an anonymous 'empunya cerita' who reports without adjudicating "
            "between mundane history and legendary wahyu:"
        ),
        "framing": (
            "Study how the chronicler frames historical events (rulers, wars, dynastic "
            "successions) alongside supernatural signs (bintang berekor, gunung "
            "bergemuruh, wahyu keprabon) without ever claiming truth for either — the "
            "narrator only says 'tersebutlah' / 'menurut empunya cerita' / 'ada yang "
            "mengatakan'. Notice the classical Islamic closing formula 'Wallahu a'lam "
            "bissawab' and the silsilah threading Adam → prophets → dewa → kings."
        ),
    },
    "style_rules_book": """STYLE: Babad / Hikayat — Archaic Court Chronicle
= Historical events retold in archaic Malay/Javanese chronicle register. The narrator
= is anonymous 'empunya cerita' — reports without adjudicating between mundane fact
= and mythic wahyu. Legendary silsilah, wahyu keprabon, and natural signs (bintang
= berekor, gunung bergemuruh) sit alongside dated succession events without hierarchy.

CHRONICLE OPENERS (idiomatic — vary across chapters):
- Adapun / Syahdan / Hatta / Alkisah / Maka tersebutlah / Tersebutlah pula
- Menurut empunya cerita / Menurut sesetengah riwayat / Ada yang mengatakan

FACTUAL REGIME: mythic_history (NEW, distinct from factual/fictional).
- Historical claims (dates, dynasties, wars, taxes) MAY be dated and named — but do
  NOT require external source-note density; chronicle-attribution 'menurut empunya
  cerita' or 'catatan penyalin' is a valid substitute for citation.
- Legendary/wahyu elements (Dewa turun ke bumi, wahyu keprabon, silsilah dari Adam)
  are reported flat, without hedging or rationalist scrubbing.
- Natural signs (bintang berekor, gunung meletus, laut selatan bergelora) sit BESIDE
  royal succession events, not below them. Both are 'apa yang tercatat'.

CLASSICAL CLOSING FORMULA (mandatory on the final chapter):
- "Wallahu a'lam bissawab" — Islamic 'God knows best' — signals epistemic humility
  and closes the chronicle cycle.

VOICE: Third-person impersonal chronicler ('empunya cerita' / 'penyalin'). NEVER
first-person ('saya'). NEVER modern-political vocabulary (demokrasi, republik,
parlemen) — these break the timeless-mythological register. Use archaic honorifics
(baginda / sang prabu / kanjeng ratu / susuhunan).

SIGNATURE MOVES:
- 6/6 chapters may open with a chronicle-formula opener (patch M ratio=1.0 for babad —
  genre convention, NOT flagged as narrator-formula-saturation like moraliste).
- Silsilah threading: prophets → dewa → kings, unbroken chain established Bab 1.
- Aphoristic pronouncements ~5/1000 words permitted (higher than most factual styles).
- Small-human anchor: at least 1 named commoner or vignette PER chapter, not only royals.

FORBIDDEN:
- Modern political vocabulary (demokrasi, republik, konstitusi, parlemen, konsensus).
- First-person narrator voice.
- Explicit hedging of legendary elements ('ini mungkin mitos' / 'kemungkinan legenda').
- Framing that reads the chronicle as folk-tale rather than as testimony.
- AI-hedge placeholders like 'sekitar X atau tokoh Y lain' — commit to the silsilah OR
  use classical hedge 'ada yang mengatakan'.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "chronicle_opener_per_chapter",
            "classical_closing_formula",
            "silsilah_threading",
        ],
        "banned_tells": [
            "demokrasi", "republik", "parlemen", "konstitusi",
            "sekitar X atau tokoh Y lain",
            "kemungkinan besar mitos",
            "ini adalah legenda belaka",
        ],
        "counters": {
            "narrator_opening_ratio_max": 1.0,
            "small_human_anchor_min_per_chapter": 1,
            "aphorism_density_target": 5,
            "modern_political_terms_max": 0,
            "silsilah_present_ch1": True,
            "classical_closing_present_ch_last": True,
        },
    },
}


_PEWAYANGAN_DALANG_SPEC = {
    "display_name": "Pewayangan — Shadow-Play Master Narration",
    "aliases": [
        "pewayangan", "pewayangan_dalang", "pewayangan-dalang", "wayang",
        "shadow play", "dalang narration", "punakawan tradition",
        "wayang kulit", "wayang purwa",
    ],
    "is_fiction": True,
    "category": "E",
    "medium_origin": "ear",
    "tier": "P1",
    "tts_risk": False,
    "output_support": "both",
    "factual_regime": "mythic_narrative",
    "rag": {
        "query_instruction": (
            "Retrieve a wayang-tradition passage narrated by a dalang, blending "
            "Mahabharata dewa (Batara Guru, Narada, Brama, Dorna) with Nusantara "
            "punakawan (Semar, Cepot, Dawala, Garéng in Sunda; Petruk, Bagong in Jawa):"
        ),
        "framing": (
            "Study how the dalang alternates scene-narration with direct address to "
            "'para peraga' / 'dulur-dulur nu budiman', invoking stagecraft elements "
            "(kayon dioyagkeun, blencong hurung, kelir geunjleung, cempala ditakol). "
            "Notice the siloka structure — teachings arrive through parable, not "
            "explicit sermon; the pak Dalang trusts the eunteung to speak for itself."
        ),
    },
    "style_rules_book": """STYLE: Pewayangan — Shadow-Play Master Narration
= Wayang cosmology retold by a dalang. Mahabharata dewa (Batara Guru, Narada,
= Brama, Dorna) share the cosmos with Nusantara punakawan (Semar/Cepot/Dawala/
= Garéng in Sundanese, Semar/Petruk/Bagong in Javanese). The dalang narrates
= scenes, quotes dialog, AND directly addresses the audience — three registers
= interleaved.

FACTUAL REGIME: mythic_narrative (NEW — pure myth, distinct from mythic_history
babad. Wayang is not chronicle; it is theater about the cosmos).

STAGECRAFT ELEMENTS (must be present as sensory anchors, not decoration):
- Kayon (tree-of-life puppet) — dioyagkeun / ditancebkeun / condong
- Blencong (oil lamp behind the kelir) — hurung / meredong / ngiceupan
- Kelir (screen) — geunjleung ku angin / kabuka / ditutup
- Cempala (wooden knocker) — ditakol tilu kali (opening/climax/closing)
- Gong / kendang / suling — as scene-transition punctuation

DALANG DIRECT-ADDRESS (genre-canonical, use sparingly at climax + closing):
- "Héh, dulur-dulur anu budiman, simkuring rék nanyakeun hiji hal…"
- "Kitu, dulur-dulur nu budiman. Lalakon ieu sanés dongéng kosong."
- "Anu daék muka ceuli leuwih agung tibatan anu ngan ukur muka mahkota."

SIGNATURE MOVES:
- Embedded verse forms (kidung / tembang / pantun / sisindiran) between prose
  paragraphs — 3-4 line stanzas that punctuate scene transitions.
- Aphoristic siloka at scene close, ~6-7 per 1000 words (highest permitted density
  across Nusantara tier — corpus sample-19 measured ~6.4/1000).
- Pupuh-like cadence with 8-11 syllable line rhythm on prose paragraphs.

REGISTER MIXING (patch AAAA — audit-flagged risk):
- Wayang is TIMELESS-MYTHOLOGICAL register. Modern political vocabulary
  (demokrasi, republik, parlemen, konstitusi, HAM) BREAKS the register.
- Anachronism via Cepot is a valid CANONICAL move in comedy Cepot register
  (Asep Sunandar's Giri Harja tradition uses topical/anachronistic Cepot) —
  but ONLY in explicitly comedic passages, never in khusyuk-liris passages.

SILOKA OVER EXPLICIT TEACHING (patch CCCC — audit signal):
- Wayang teaches through parable + eunteung; the audience learns by watching
  the wayang be itself, not by being told the lesson.
- Bab 6 (tanceb kayon / closing) may deliver piwulang, but must be SILOKA —
  parabolic, indirect. AVOID full-khotbah restating the whole lesson.

PUNAKAWAN TRADITION:
- Semar (patriarch, titisan Sang Hyang Ismaya) — quiet wisdom.
- Cepot / Astrajingga (Sunda) OR Petruk (Jawa) — the punakawan who speaks truth
  to power through humor. Requires 3+ humor beats per 6 chapters (audit signal:
  Cepot without humor beats reads as too somber for the tradition).
- Dawala / Bagong — the loyal follower.

CANONICAL ROLE ASSIGNMENTS (patch MMMM):
- Suralaya gatekeeper: Cingkarabala + Balaupata (raksasa kembar), NOT Dorna.
- Patih Astinapura: Sengkuni.
- Guru Kurawa+Pandawa: Dorna.

CROSSOVER: Mahabharata dewa + Nusantara punakawan is CANONICAL — this is the
Sundanese/Javanese synthesis, not a mixing error.

FORBIDDEN:
- Modern political vocabulary in khusyuk-liris passages.
- Cepot/Petruk role assigned to non-canonical positions.
- Full-khotbah closing (dalang restates the lesson explicitly instead of siloka).
- Mystical elements introduced without setup (Bab 4 climax powers should be
  seeded Bab 1-3 with capability-boundary hints — patch KKKK).
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "stagecraft_elements",
            "dalang_direct_address_climax",
            "siloka_closing",
            "embedded_verse_form",
            "punakawan_humor_beats",
        ],
        "banned_tells": [
            "demokrasi", "republik", "parlemen", "konstitusi", "HAM",
            "sistem pemerintahan modern",
            "berdasarkan konstitusi",
        ],
        "counters": {
            "narrator_opening_ratio_max": 0.5,
            "stagecraft_element_min": 4,
            "dalang_direct_address_uses": [1, 3],
            "embedded_verse_min": 3,
            "punakawan_humor_beats_min": 3,
            "aphorism_density_target": 6,
            "modern_political_terms_max": 0,
            "mystical_element_setup_required": True,
        },
    },
}


_KDRAMA_SERIAL_SPEC = {
    "display_name": "K-Drama Serial — Melodramatic Episode Arc",
    "aliases": [
        "kdrama", "k-drama", "k_drama", "kdrama_serial", "kdrama-serial",
        "korean_drama", "korean drama", "korean serial", "dorama",
        "hallyu drama", "korean tv drama", "kdrama episode",
    ],
    "is_fiction": True,
    "category": "D",
    "medium_origin": "screen",
    "tier": "P1",
    "tts_risk": False,
    "output_support": "both",
    "factual_regime": "fiction",
    "rag": {
        "query_instruction": (
            "Retrieve a K-drama serial-episode passage in present-tense close-third, "
            "alternating between 2-4 principals, with cold-open shock, three-beat middle, "
            "and last-frame cliffhanger freeze — hallyu-era contemporary Korean setting:"
        ),
        "framing": (
            "Study how the K-drama narrator lets each chapter carry the weight of one "
            "hourly episode: the cold-open lands on a shock image (a hand releasing an "
            "umbrella, a phone lighting up at 3am, a wrist grabbed at the elevator "
            "closing), the middle unfolds in three beats — public collision, private "
            "aftermath, quiet meal — and the last frame freezes on a reveal that would "
            "make an audience wait a week. Notice how weather (rain, first snow, dawn "
            "blue over the Han), light (streetlamp yellow through pojangmacha vinyl, "
            "hospital fluorescents), and food (ramyeon at 2am, tteokbokki after a "
            "breakup, kimbap in the rain) do emotional work that dialogue cannot. "
            "Honorific-shifts (banmal-drop from a sunbae, unnie / oppa softening) mark "
            "threshold moments. The second lead is never a prop — they get real "
            "interior monologue in question-form ('Kenapa dia menoleh saat namaku "
            "disebut?') even when destined to lose."
        ),
    },
    "style_rules_book": """STYLE: K-Drama Serial — Melodramatic Episode Arc
= Contemporary Korean serialized-episode fiction. Every chapter is one hourly
= episode arc: cold-open shock (1-2 pages that would open episode-1-scene-1) →
= three-beat middle (public collision → private aftermath → quiet meal or
= two-hander) → last-frame cliffhanger reveal (freeze the frame, cue the OST).
= Present-tense close-third, alternating between 2-4 principals across the run.
= Melodrama is EARNED through sensory specificity, not withheld through irony.
= The register commits INTO feeling.

EPISODE ARC (mandatory shape per chapter):
- COLD OPEN: open on a shock image that would make a viewer stop scrolling —
  a wrist grabbed at closing elevator doors, an umbrella tilted over the wrong
  head, a phone lighting up at 3am with a name that should not be there, a
  bowl of ramyeon set down between two people who have not spoken in a year.
  Do NOT ease in. Do NOT open on weather-as-mood-setter without a human beat
  inside the same paragraph.
- THREE-BEAT MIDDLE: public collision (a scene the world can see: office,
  wedding hall, hospital corridor, gallery opening) → private aftermath (one
  principal alone, or two principals cornered) → quiet meal or two-hander
  (ramyeon, tteokbokki, soju + anju, convenience-store bench).
- LAST-FRAME FREEZE: end on a reveal, a look held one beat too long, a text
  message that arrives mid-sentence, a name spoken by the wrong voice. The
  chapter ends BEFORE the reaction. Cue the OST. Roll credits.

POV DISTRIBUTION (structural rule, not stylistic preference):
- Alternating close-third between 2-4 principals across the book. Each chapter
  anchors to ONE POV; POV switches happen at chapter boundaries, not mid-scene.
- The SECOND LEAD is not a prop. They get real emotional interiority — at least
  one interior-monologue beat every two chapters, minimum 3 beats per book.
  When they lose, the loss must be felt. Second-lead syndrome exists because
  K-drama writers wrote them with dignity. Match that.

INTERIOR MONOLOGUE (register-defining):
- Question-form: "Kenapa dia bilang begitu? Kenapa tangannya bergetar tadi?
  Apa yang tidak dia katakan?"
- Rendered in italics OR clear voice-break — never buried inside neutral
  narration.
- Memory intrudes as unmarked tense-shift ("Even now, she remembers the smell
  of the raincoat he gave her." / "Bahkan sekarang, dia masih ingat bau jaket
  hujan itu."). NO sectioned FLASHBACK headers. NO "FIVE YEARS EARLIER" cards.
  Memory is a tense-shift inside the same paragraph.

SENSORY GRAMMAR (weather + light + food, always concrete):
- Weather: rain hitting a car window, first snow that stops traffic, dawn blue
  over the Han, humidity before a summer storm. Weather is emotional weather.
- Light: streetlamp yellow through pojangmacha vinyl, hospital fluorescents
  buzzing at 4am, the blue glow of a phone screen in a dark bedroom, dawn blue
  through hanok paper doors.
- Food: ramyeon at 2am is grief, tteokbokki after a breakup is defiance, kimbap
  in the rain is love, soju is confession, hotteok is childhood, banchan set
  down without a word is forgiveness. Meals CARRY emotional beats; they are
  not decoration. AT LEAST ONE meal-as-emotional-scene per book.

CULTURE-SPECIFIC BEATS (load-bearing, not decoration):
- HONORIFIC-SHIFT threshold: a sunbae drops banmal, a hyung-nim softens to
  hyung, an oppa becomes a name. The shift IS the emotional beat. Mark it in
  narration ("She had never heard him call her by her name alone before.").
- WRIST-GRAB: the hand closes around the wrist mid-turn — arrival, prevention,
  claim. Not a flirt. A stopped-motion beat.
- UMBRELLA-SHARE: one umbrella, two people, one shoulder wet. The person
  holding the umbrella tilts it TOWARD the other. That is the whole scene.
- PIGGYBACK: after drinking, after collapse, after a fever. Physical trust
  that the character would never admit to in daylight.
- HAND-OVER-HEAD in sudden rain — a jacket, a bag, a bare hand as shelter.
- At least ONE of {honorific_shift, wrist_grab, umbrella_share, piggyback,
  hand_over_head} per book, embedded as a threshold beat.

CLASS FRICTION (jaebeol vs seomin — texture, NEVER moral binary):
- The chaebol world (marbled foyers, elevator keycards, hospital VIP wards,
  Cheongdam-dong penthouses, private tutors, family lawyers who arrive faster
  than paramedics) is real pressure, not caricature. Its inhabitants love and
  suffer and fail.
- The seomin world (반지하 half-basement apartments, night-shift convenience
  stores, subway last-train, ramyeon budgets, mothers who work three jobs) is
  dignity under weight, not virtue-porn poverty tourism.
- Class friction shows in objects (whose phone, whose coat, whose car pulls
  up), in space (who owns the room they are standing in), in language (who
  can afford politeness). Not in speeches about inequality.
- NEVER: "chaebol heir with a heart of gold" as sole characterization. The
  jaebeol lead is compromised by their world, and the seomin lead is not
  ennobled by lack.

MELODRAMA COMMITMENT (register-defining):
- K-drama commits INTO feeling. Rain scenes are ALLOWED to be operatic. The
  OST swell is EARNED, not undercut. A character crying in a car parked
  outside a hospital is not embarrassing prose — it is the register.
- Withhold nothing through ironic detachment. If a scene would make a viewer
  cry, WRITE it to make a reader cry. Specificity is the earning, not
  restraint.

FORBIDDEN:
- Generic Western romcom beats (meet-cute → misunderstanding → grand-gesture
  apology). K-drama arcs are longer, colder, and more patient.
- Physical-description-as-characterization ("doe-eyed," "porcelain skin,"
  "raven hair," "impossibly tall"). Describe what a character DOES in a
  space, not what they look like on a poster.
- Ironic detachment / smirking narrator. K-drama does not wink at itself.
- Self-referential meta ("like a K-drama," "binge-worthy," "Netflix Korea").
  The story does not know it is a K-drama.
- Exoticizing English glosses ("kimbap, a Korean rice roll," "oppa, meaning
  older brother"). Trust the reader OR trust the context. Gloss only when the
  narrative demands it (a non-Korean character asking).
- "As they say in Korea…" narrator-tourism voice.
- Sectioned FLASHBACK / FIVE YEARS EARLIER headers. Memory is a tense-shift.
- Second lead reduced to obstacle-prop with no interior beat. If the second
  lead gets zero interior monologue in the entire book, the register has
  failed.
- The "chaebol heir with a heart of gold" as sole characterization for the
  jaebeol principal.
- Aegyo written as cringe-observation. If a character does aegyo, the
  narration commits to it as tenderness, not as ironic distance.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "cold_open_shock_per_chapter",
            "end_of_chapter_cliffhanger_freeze",
            "interior_monologue_question_beat_per_chapter",
            "meal_as_emotional_scene_min_per_book",
            "honorific_shift_or_physical_contact_threshold_per_book",
            "second_lead_interior_beat_min",
            "weather_or_light_sensory_anchor_per_chapter",
        ],
        "banned_tells": [
            "doe-eyed",
            "porcelain skin",
            "raven hair",
            "as they say in Korea",
            "like a K-drama",
            "binge-worthy",
            "meet-cute",
            "FIVE YEARS EARLIER",
            "chaebol heir with a heart of gold",
            "oppa, meaning",
            "kimbap, a Korean rice roll",
            "impossibly tall",
        ],
        "counters": {
            "cold_open_per_chapter": True,
            "episode_cliffhanger_min_per_chapter": 1,
            "interior_monologue_beat_per_chapter": 1,
            "interior_monologue_question_form_min_per_chapter": 1,
            "meal_as_emotion_min": 1,
            "honorific_shift_present": True,
            "physical_contact_threshold_min_per_book": 1,
            "second_lead_pov_min_beats": 3,
            "second_lead_interior_min_per_2_chapters": 1,
            "principal_pov_count_range": [2, 4],
            "pov_switch_at_chapter_boundary_only": True,
            "weather_or_light_anchor_min_per_chapter": 1,
            "sectioned_flashback_headers_max": 0,
            "meta_self_reference_max": 0,
            "physical_description_as_characterization_max": 0,
            "class_friction_moral_binary_max": 0,
            "narrator_opening_ratio_max": 0.5,
            "aphorism_density_target": 1,
        },
    },
}


_ROMANCE_CONTEMPORARY_SPEC = {
    "display_name": "Contemporary Romance — Literary Register",
    "aliases": [
        "romance", "contemporary_romance", "romance_contemporary",
        "contemporary-romance", "romance-contemporary",
        "love story", "love_story", "romance novel", "romance_novel",
        "contemporary_love", "contemporary love",
        "sally rooney", "sally_rooney", "emily henry", "emily_henry",
        "casey mcquiston", "ocean vuong tender",
        "literary romance", "literary_romance",
    ],
    "is_fiction": True,
    "category": "D",
    "medium_origin": "page",
    "tier": "P1",
    "tts_risk": False,
    "output_support": "both",
    "factual_regime": "fiction",
    "rag": {
        "query_instruction": (
            "Retrieve a contemporary literary-romance passage in close-3rd POV "
            "alternating between two leads, where interiority runs BENEATH the "
            "dialogue and physical proximity shortens by measured units across "
            "the book:"
        ),
        "framing": (
            "Study how the two leads' POV chapters carry distinct sentence-rhythms "
            "(one long-serpentine when interior, the other short-punch when "
            "defended), how each spoken line is shadowed by a four-line unspoken "
            "paragraph the reader hears through free-indirect voice, and how "
            "bodies are noticed as single specific details (a wrist tendon, the "
            "seam of a T-shirt at the collarbone) rather than inventoried. Notice "
            "the proximity grammar — the leads move from across-the-room in the "
            "opening chapter to sharing one bed by the closing chapter, and every "
            "chapter measurably shortens the space between them. Notice how "
            "sexual tension is felt at word-level (verb choice, sentence landing "
            "on the wrong word) rather than signposted with 'she wanted him.'"
        ),
    },
    "style_rules_book": """STYLE: Contemporary Romance — Literary Register
= Two leads, close-3rd POV alternating chapter-by-chapter, whose distance closes
= by measured units across the book. The prose lives in the space between what
= is said and what runs beneath. Bodies are noticed one detail at a time. Silence
= is a beat, not a gap. The reader knows the two are moving toward each other
= before either lead admits it — but the narrative NEVER signposts this in
= advance; it earns it, chapter by chapter, at word-level.

POV GRAMMAR (structural, non-negotiable):
- Close-3rd, past-tense-close-3rd OR present tense — CHOOSE ONE and hold across
  the whole book. Do not drift.
- Chapters alternate between the two leads. Each lead's POV chapters carry a
  distinct sentence-rhythm: one long-serpentine when interior (Marianne-adjacent);
  the other short-punch when defended (Connell-adjacent). The reader can identify
  whose chapter it is from paragraph 1 by rhythm alone, without a name.
- No omniscient third party summarizes their relationship from outside. The
  reader knows only what each lead knows, plus what free-indirect voice leaks.

INTERIOR COUNTER-MELODY (register-defining move):
- Every dialogue-heavy scene carries a running interior beneath the said lines.
- Rule of thumb: for every three spoken beats, there is at least one interior
  paragraph — what she wanted to say instead, what he registered but did not
  respond to, the four lines she edited out before speaking.
- The counter-melody uses free-indirect voice — no italics, no "she thought,"
  just voice drifting between narrator and lead.

PROXIMITY GRAMMAR (arc-level move):
- Physical distance between the two leads shortens by measurable units across
  the book. A rough scaffold — vary the beats but not the arc:
  ch1: across the room at a party / on opposite sides of a shared workspace.
  ch2-3: same table, not yet touching. First accidental touch.
  ch4-5: next to. Shoulder-to-shoulder on a couch, in a cab, on a curb.
  ch6-7: touching on purpose. A hand at the small of the back, a knee.
  ch8+: alone in one room, one bed, one silence.
- The arc must be TRACKABLE — a reader plotting proximity per chapter should
  see monotonic shortening (small oscillations permitted, no full reversals).

BODY DESCRIPTION — SINGLE NOTICED DETAIL:
- Per scene, ONE specific body detail noticed by the POV lead. Not an inventory.
- Wrist tendon under a rolled sleeve. The ridge of a collarbone under a T-shirt.
- A scar on a knuckle. The way his jaw sets on a hard vowel. Not eye color, not
  hair color, not height, not chest, not "chiseled anything." One thing, once.
- The detail is chosen because THIS lead notices it — it characterizes the
  looker, not the looked-at.

SEXUAL TENSION — WORD-LEVEL, NOT SIGNPOSTED:
- Tension is felt in verb choice (he set the glass down instead of put), in
  sentences ending on the wrong word, in the paragraph that does not answer
  the question the previous paragraph asked.
- FORBIDDEN mode: "She wanted him." "She had never wanted anyone this much."
  "Electricity coursed through her." The reader must feel the tension without
  the narrator naming it.

SILENCE AS BEAT:
- Silence is written as a paragraph, not a dash. "He didn't say anything. She
  watched the ice re-form on the glass." Silence CARRIES weight — it is the
  interior-counter-melody's rest.

DIALOGUE:
- Snappy, contemporary, register-appropriate to the world (Dublin postgrad
  reads different from Portland pottery studio reads different from Jakarta
  expat sunset bar). Register-specific vocabulary — a barista in Kemang says
  "iced Americano" and "kabur dulu," not generic-American coffee-shop dialogue.
- Comma splices are permitted as intimacy-marker in interior paragraphs — they
  are the sentence structure of a lead too close to their own thought to punctuate.

CULTURAL SPECIFICITY (anchor the world):
- Real named place. Not "the city" — Brooklyn co-op bar / Dublin postgrad
  library / Portland pottery studio / Jakarta rooftop off Casablanca. Real
  coffee shop names, real train lines, real weather months. The world is
  specific because the leads live IN it, not on a stage set for them.

THE ONE INTERNAL-SHIFT SCENE:
- Exactly one scene per book (typically 2/3 through) where NOTHING external
  happens — they wait for a train that comes on time, they wash dishes, they
  sit on a fire escape — and everything internal shifts. The reader closes
  the chapter knowing the arc has just turned, without a single external event.

FORBIDDEN (register-breaks):
- Bodice-ripper register in any form: heaving, throbbing, member (as
  euphemism), silken tresses, molten core, molten heat.
- Purple-prose color inventory: "his piercing blue eyes," "her emerald-green
  eyes," "raven-black hair" — anything that reads like a character sheet.
- Coup-de-foudre signposted in advance: "The moment their eyes met, she
  knew." "From the second he walked in, everything changed." The reader
  discovers the arc; the narrator does not announce it.
- "But he was different." "Unlike any man she'd ever met." "He was
  everything she never knew she needed." — the standard-genre tell.
- Third-party observer voiceover: a side character telling one lead "you
  two clearly love each other" / "anyone can see it." The reader does the
  seeing; no side character does it for them.
- Dialogue-only scenes with no interior counter-melody — flat surface, no
  beneath.
- Setting-decoration prose: paragraphs describing the coffee shop, the
  autumn light, the exposed brick, instead of describing the person opposite.
- "Her heart skipped a beat" / "electricity coursed through her" / "her
  stomach did a little flip" — narrated-body-reaction shorthand for
  attraction. Show the noticed detail; do not narrate the physiology.
- Full-figure body inventory: "tall, dark, and handsome" / "long legs and a
  killer smile" / any three-adjective run on a body.
- Insta-love without proximity work: the leads confess love in ch2 with no
  proximity-grammar arc earned. Love, if it lands, lands late and earned.
- Register-drift across POV: both leads sounding identical in interior. The
  rhythm distinction MUST hold.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "pov_alternation_between_two_leads",
            "interior_counter_melody_beneath_dialogue",
            "proximity_grammar_shortening_arc",
            "single_noticed_detail_body_description",
            "internal_shift_scene_no_external_event",
            "cultural_specificity_named_place",
        ],
        "banned_tells": [
            "heaving", "throbbing", "silken tresses", "molten core",
            "molten heat", "piercing blue eyes", "emerald green eyes",
            "raven-black hair",
            "the moment their eyes met",
            "but he was different",
            "unlike any man she'd ever met",
            "everything she never knew she needed",
            "her heart skipped a beat",
            "electricity coursed through",
            "stomach did a little flip",
            "melted into his arms",
            "tall, dark, and handsome",
            "you two clearly love each other",
            "anyone can see it",
            "she wanted him more than",
        ],
        "counters": {
            "pov_alternation_min_per_book": 4,
            "pov_leads_exact": 2,
            "interior_counter_melody_min_per_dialogue_scene": 1,
            "proximity_grammar_present": True,
            "proximity_shortening_monotonic": True,
            "single_noticed_detail_per_scene_min": 1,
            "single_noticed_detail_per_scene_max": 2,
            "full_body_inventory_max": 0,
            "purple_prose_tells_max": 0,
            "coup_de_foudre_signposted_max": 0,
            "third_party_observer_voiceover_max": 0,
            "narrated_body_reaction_shorthand_max": 0,
            "internal_shift_scene_present": True,
            "single_setting_over_inventory_ratio_max": 0.25,
            "cultural_specificity_named_place_min": 1,
            "tense_consistency_required": True,
            "pov_rhythm_distinct_between_leads": True,
            "silence_as_paragraph_min_per_book": 3,
            "comma_splice_permitted_in_interior": True,
            "insta_love_max_chapters": 0,
        },
    },
}


_REMAJA_COMING_OF_AGE_SPEC = {
    "display_name": "Coming of Age",
    "aliases": [
        "remaja", "coming_of_age", "coming-of-age", "ya", "young_adult",
        "novel_remaja", "teen_lit", "tumbuh_dewasa", "remaja_indonesia",
        "remaja_coming_of_age", "remaja-coming-of-age",
    ],
    "is_fiction": True,
    "category": "D",
    "medium_origin": "page",
    "tier": "P1",
    "tts_risk": False,
    "output_support": "both",
    "factual_regime": "fiction",
    "rag": {
        "query_instruction": (
            "Retrieve an Indonesian young-adult / novel-remaja passage narrated "
            "close-first-person by a teenage 'aku' or 'gue', with sentence-level "
            "trilingual code-switch (Indonesian baku + gaul + English micro-"
            "borrowing) anchored in specific school-social geography:"
        ),
        "framing": (
            "Study how the teen narrator interleaves formal Indonesian, gaul slang, "
            "and English micro-borrowings inside a single paragraph without register "
            "collapse ('aku tuh sebenernya udah tau, tapi denial mode masih aktif'). "
            "Notice the school-geography specificity — kantin belakang, lorong depan "
            "lab, halte busway, warung Padang samping kos — never generic 'school'. "
            "Notice the obsessive re-reading beat: WhatsApp typing dots, IG story "
            "timing, seen-tapi-belum-dibalas. Notice the music-anchor scene — a "
            "specific song lyric at a specific moment, quoted with attribution. "
            "Notice that the narrator is CONSTITUTIVELY teen — no adult reflection "
            "overwriting, no 'zaman sekarang tuh anak muda' diagnostic voice."
        ),
    },
    "style_rules_book": """STYLE: Remaja — Indonesian YA / Coming-of-Age
= Close-first-person Indonesian teen narrator (aku / gue), sometimes in journal/diary
= form. Voice is CONSTITUTIVELY adolescent — never a grown-up looking back and
= diagnosing youth. Emotional grammar runs on obsessive re-reading of small signals
= (WhatsApp typing dots, IG story timing, seen-tapi-nggak-dibalas). School-social
= geography and family-geography are load-bearing, not decoration.

POV LOCK:
- First-person aku/gue OR diary/journal address ('Dear diary', 'Kamu tau nggak, aku
  tuh…', unaddressed monologue). Third-person close is permitted only if the
  narrator's interior stays teen-locked without exception.
- The narrator CAN be nostalgic-teen (writing tonight about last week) but is
  NEVER grown-up-looking-back-with-macro-wisdom. No 'setelah dewasa aku baru
  paham', no 'sebagai orang tua sekarang aku ngerti', no 20-years-later frame.

TRILINGUAL CODE-SWITCH (register-defining):
- Sentence-level or intra-paragraph switch across three registers:
  (1) Indonesian baku — for framing, quoted authority, narration spine.
  (2) Bahasa gaul — for interior voice ('tuh', 'sih', 'kok', 'banget', 'gue',
      'elo', 'anjir', 'yaudah', 'gapapa', 'baper', 'bucin', 'santuy', 'gaskeun',
      'y'ampun', 'literally', 'random banget').
  (3) English micro-borrowing — one to three words, not sentences ('denial mode
      aktif', 'red flag', 'green flag', 'main character energy', 'situationship',
      'ghosting', 'overthinking', 'insecure', 'closure').
- ≥2 code-switch beats per chapter minimum. Density calibrated to sound like
  2020s Indonesian teens, NOT 2010s Twitter kids and NOT expat-school English-
  first speakers.
- Slang must be GENERATIONALLY LOCKED to 2020s. Forbidden throwbacks: 'jayus',
  'garing', 'kece badai', 'ciyus miapah', 'kepo abis' (reads 2010s), 'nyokap
  bokap gue keren' (90s ABG), 'boyband korea favoritku SS501' (dated K-anchor).

SCHOOL-SOCIAL GEOGRAPHY (mandatory anchor):
- Every chapter names ≥1 specific school-social OR family-social location:
  kantin (belakang / depan / atas), lorong (depan lab bio, samping ruang guru,
  antara kelas XI IPA 3 dan XI IPS 1), kelas (dengan nomor / jurusan), OSIS,
  pramuka, ekskul (rohis, paskibra, teater, basket, mading, PMR), warung
  (Yu Sum, Bu Ijah, warung Padang sebelah kos), halte busway, stasiun (Rangkas,
  Tanah Abang, Manggarai, Sudirman), kos, kontrakan, rumah nenek.
- Generic 'sekolah' / 'rumah' without micro-geography is a tell — flagged.
- Jakarta/Bandung/Yogya specific (ganjil-genap, Rangkas last train, Malioboro,
  Braga, TIM, Blok M, Kopi Kenangan, Janji Jiwa, Fore) is welcomed but not
  required; small-kota geography (warung depan sekolah, alun-alun, terminal
  angkot) is equally valid.

FAMILY-GEOGRAPHY:
- Family texture is economic + emotional, not abstract. Name the configuration:
  mama-papa, single parent (mama aja / papa aja), tinggal sama nenek, kos jauh
  dari rumah, ngekos sendiri di Depok, pulang kampung tiap lebaran. Name the
  money texture without moralizing: uang jajan pas-pasan, transfer bulanan
  telat, minta tambahan malu.

OBSESSIVE RE-READING BEAT (mandatory ≥1 per chapter):
- The narrator returns to a small signal and re-reads it: WhatsApp typing dots
  ('titik-titik itu muncul, hilang, muncul lagi'), IG story timing ('dia
  upload jam 11.47, aku tau karena aku ngecek dua menit sebelumnya'), 'seen'
  tapi belum dibalas, DM tone shift, dia follow lagi mantannya, notif yang
  di-mute, spam story yang cuma buat satu orang.
- This beat carries interiority — it IS the coming-of-age texture. Skipping
  it makes the register read as generic fiction.

MUSIC-ANCHOR (mandatory ≥1 per book):
- At least one scene anchored to a specific song at a specific moment.
- Lyric may be quoted (1-2 lines) with attribution ('lagu Fiersa Besari,
  Waktu yang Salah', 'Hindia — Evaluasi', 'Nadin Amizah — Rumpang', 'Feby
  Putri — Halu', 'Juicy Luicy — Terlanjur Mencinta').
- Playlist references (Spotify liked, replay tengah malam, headset satu
  telinga di kereta) are register-canonical.

THRESHOLD MOMENTS (choose ≥1 per book):
- First ojol solo. First konser (Joyland, We The Fest, Synchronize).
- First pacaran resmi. First LDR (Jakarta-Bandung / Jakarta-Jogja / dalam-luar
  kota). First patah hati. First UN result / first SBMPTN result / first ditolak
  PTN. First ngajakin pulang malem tanpa izin. First aku-bukan-anak-kecil-lagi
  moment dengan orang tua.

VOICE INTERIOR ADDRESS:
- Narrator sometimes addresses self ('bego lu, ngapain di-reply lagi'), sometimes
  an imagined 'kamu' (the crush, the mantan, the future self), sometimes nobody
  ('yaudah, jalan aja dulu'). Rotate — don't sit only in one mode.

STRUCTURAL:
- Chapters can be diary-dated, mixtape-titled, WhatsApp-thread-titled, or plain.
- 6/6 chapters may open with an interior beat (not a scene-setter) — high POV
  saturation is genre-canonical for remaja.

FORBIDDEN:
- Translated-Anglo-YA voice: 'I stared at my hands', 'She was different', 'Life
  was hard', 'The pain was real.' English-first prosody with Indonesian words on
  top is a fail state.
- Adult-narrator overwrite: 'zaman sekarang tuh anak muda…', 'kalau dipikir-pikir
  sekarang setelah dewasa…', 'sebagai orang tua saya paham…', '20 tahun kemudian
  aku baru sadar…'. If the narrator diagnoses generational patterns, the POV
  broke.
- Fantasy-remaja Wattpad tropes: 'Chairman muda tampan', 'CEO ganteng jatuh
  cinta padaku', harem-jatuh-cinta-tiga-cowok-sekaligus, arranged-marriage-with-
  billionaire. That's a different sub-genre; not this register.
- Generic school setting: 'aku duduk di kelas', 'kami di kantin' without
  specifying WHICH kelas / WHICH kantin / WHICH lorong.
- Aegyo / kawaii texture mistranslated into Indonesian ('pipiku menggembung
  imut', 'aku memasang wajah puppy-eyes'). That's kdrama register, not remaja.
- English-heavy code-switch that reads as expat-school kid ('Guys, so today
  was totally exhausting, I literally can't').
- Purple prose ('air mataku berlinang bagai sungai', 'hatiku hancur berkeping-
  keping', 'langit menangis bersamaku').
- Generational anachronism in slang (2010s Twitter / 90s ABG / dated K-pop
  anchor as CURRENT-favorite).
- Explicit lesson-delivery at chapter close ('dari situ aku belajar bahwa
  hidup itu…'). Remaja teaches through the moment, not a moral tag.
- Third-person omniscient with adult reflection.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "code_switch_trilingual_per_chapter",
            "school_or_family_geography_anchor_per_chapter",
            "music_anchor_scene_per_book",
            "obsessive_re_reading_beat_per_chapter",
            "first_person_teen_voice_constitutive",
        ],
        "banned_tells": [
            "zaman sekarang tuh anak muda",
            "kalau dipikir-pikir sekarang setelah dewasa",
            "sebagai orang tua saya paham",
            "20 tahun kemudian aku baru sadar",
            "air mataku berlinang bagai sungai",
            "hatiku hancur berkeping-keping",
            "langit menangis bersamaku",
            "chairman muda tampan",
            "ceo ganteng jatuh cinta padaku",
            "pipiku menggembung imut",
            "puppy-eyes",
            "I stared at my hands",
            "She was different",
            "Life was hard",
            "guys so today was totally exhausting",
            "jayus", "ciyus miapah", "kece badai",
            "dari situ aku belajar bahwa hidup",
        ],
        "counters": {
            "code_switch_min_per_chapter": 2,
            "school_or_family_geography_anchor_per_chapter": 1,
            "music_anchor_present_per_book": True,
            "obsessive_re_reading_beat_min_per_chapter": 1,
            "first_person_teen_voice_ratio_min": 0.95,
            "adult_reflection_tells_max": 0,
            "wattpad_ceo_tropes_max": 0,
            "generic_school_setting_max": 0,
            "purple_prose_tells_max": 0,
            "generational_anachronism_slang_max": 0,
            "narrator_opening_ratio_max": 1.0,
            "explicit_lesson_delivery_max": 0,
        },
    },
}


if _PHASE2_ON():
    P1_STYLES["babad_hikayat"] = _BABAD_HIKAYAT_SPEC
    P1_STYLES["pewayangan_dalang"] = _PEWAYANGAN_DALANG_SPEC
    P1_STYLES["kdrama_serial"] = _KDRAMA_SERIAL_SPEC
    P1_STYLES["romance_contemporary"] = _ROMANCE_CONTEMPORARY_SPEC
    P1_STYLES["remaja_coming_of_age"] = _REMAJA_COMING_OF_AGE_SPEC


__all__ = ["P1_STYLES"]
