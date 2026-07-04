# ── pakem/registry_p23.py — P2/P3 expansion wave (pakem-style-registry-expansion.md).
# GENERATED from the 6-category draft workflow (2026-07-04), then integrated. Isolated
# like registry_p1 (merged via STYLES.setdefault). Entries carry the FULL schema v2:
# medium_origin, tier, tts_risk, output_support, factual_regime, category, register_spec.

# === A — shared bases ===
# === A-docyoutube — P2/P3 wave ===
# --- A. Dokumenter & YouTube-native (all medium_origin=ear, factual_regime=strict,
#     category A, is_fiction=False) --------------------------------------------

# COUSIN RULE: Intimate Nature + Ecstatic Doom Nature are variants of the
# NATURE-DOC family — kin of the core-14 `natgeo` pakem in registry.py
# ("reverent observational wonder, precise natural detail, patient pacing").
# Shared family base below + per-entry delta, mirroring _P1_HORROR_BASE.
_P23_NATURE_DOC_BASE = """STYLE FAMILY: Nature Documentary -- the living world observed with total attention. Behavior is the plot; precision is the wonder.
SHARED RULES (family-wide):
- ARRIVAL FIRST -- open by placing the listener in the habitat: light, weather, season, scale. The world before its actors.
- ONE PROTAGONIST -- follow one animal (or one group) and reveal the whole ecosystem through its stakes.
- BEHAVIOR AS PLOT -- hunts, courtship, migration, rearing: let observed behavior carry the narrative arc. No invented events.
- PRESENT TENSE, always -- this is happening now, before the listener's eyes.
- EXACTNESS -- name species, distances, temperatures, timespans precisely; accuracy is where the awe comes from.
- WIDER WEB -- pull back at least once to the system: predator and prey, climate, interdependence.
FORBIDDEN (family-wide): Invented animal thoughts or dialogue. Motives anthropomorphized beyond what behavior shows.
"""

# === B-authorial — shared bases ===
# === B-authorial (P2/P3 wave) — shared bases ===
# Cousin rule (expansion doc note #1): family base + per-entry delta.

_P23_COMIC_NONFIC_BASE = """STYLE FAMILY: Comic Nonfiction -- real facts delivered funny. The humor never replaces the fact; it is how the fact lands.
FAMILY RULES:
- Every joke must sit on a TRUE, checkable fact; the funnier the line, the more solid the sourcing beneath it.
- Fold asides INTO the prose as one-sentence spoken-style parentheticals that comment on the fact just given, then return.
- Play the narrator as the least impressive person in the room: the experts are the straight men; the narrator supplies the wonder and the flinching.
- Land the chapter's final beat on sincere awe, not a punchline.
FORBIDDEN (family-wide): Mocking the subject or the experts. Jokes that require distrusting the facts. Humor by exaggerating the science.
"""

_P23_NOVELISTIC_NONFIC_BASE = """STYLE FAMILY: Novelistic Nonfiction -- real events built with fiction craft. Scenes, not summary; every detail traceable to the record.
FAMILY RULES (cousins of Narrative Non-Fiction):
- Reconstruct in SCENES: weather, rooms, gestures, spoken words -- all drawn from documents, letters, interviews, testimony. Invent nothing.
- Grant interiority only where a diary, letter, or interview supports it; otherwise render thought as observable behavior.
- Plant ordinary details early that will matter terribly later; the reader's dread comes from knowing more than the people on the page.
- Withhold authorial judgment; let the assembled record convict or absolve.
FORBIDDEN (family-wide): Invented dialogue or thoughts no source supports. Editorializing adjectives ("evil", "tragic") doing work the scenes should do.
"""

    # === B-authorial (P2/P3) ===

# === C-audio — shared bases ===
# === C-audio (P2 wave) — no shared family base: three distinct styles, cousin rule N/A ===

# === D-cinematic — shared bases ===
# === D-cinematic — P2/P3 wave — shared bases ===
# --- D. Sinematik & Genre (cousin rule: Fairy Tale + Mythic Epic share oral-tale base) ---

_P23_ORAL_TALE_BASE = """STYLE FAMILY: Oral Tale -- fiction shaped by telling aloud. Formula, repetition, and archetype carry the story; the teller's voice is the medium.
ORAL RULES (family-wide):
- FORMULA FRAMES -- open and close on a traditional-sounding formula; the frame signals "this is a tale," set apart from the present world.
- REPETITION IS STRUCTURE -- repeat key phrasings verbatim at structural beats; a listening audience navigates by the echo.
- ARCHETYPE OVER INTERIORITY -- define characters by role, deed, and fixed epithet, never by psychological analysis.
- INVENT ALL NAMES -- kingdoms, heroes, forests, gods: coin your own; borrow from no existing canon.
FORBIDDEN (family-wide): Modern idiom, irony, or self-aware commentary. Inner monologue dissecting a character's psyche.
"""

# === D-cinematic — P2/P3 entries (splice inside the styles dict) ===

# === E — shared bases ===
# === E-nusantara (P2 wave) ===
# Cousin rule considered: Babad (page/hybrid, scribe chronicle) and Pewayangan
# (ear/fictional, performance narration) are siblings by category only, not
# variants of one family -- no shared _P23_ base; each carries full rules.

# === F — shared bases ===
# === F-edu — shared bases (module level) ===
# --- Spoken Address family base (cousin rule: Commencement Wisdom + Oratory — Anaphora) ---
_P23_SPEECH_BASE = """STYLE FAMILY: Spoken Address -- words written for a voice in front of a gathered audience. The page is only a score; the performance is the text.
DELIVERY RULES (family-wide):
- WRITE FOR BREATH -- every sentence sayable in one breath; rhythm is tested aloud, not by eye.
- ADDRESS THE ROOM -- sustained direct "you"; the audience is present, gathered, and spoken to, never described.
- STRUCTURE MUST BE AUDIBLE -- mark every turn with sound (a returning phrase, a question, a shift in sentence length); formatting does not exist for a listener.
FORBIDDEN (family-wide): Page furniture -- headings, bullet lists, parentheticals, footnotes. Anything a first-pass listener cannot catch.
"""

    # === F-edu (Edukasi & Motivasi) — P2/P3 wave ===

P23_STYLES = {
    # === A ===

"question_driven_explainer": {
    "display_name": "Question-Driven Explainer",
    "aliases": [
        "question-driven explainer", "question driven explainer",
        "question_driven_explainer", "vox", "vox style", "explainer",
        "news explainer", "policy explainer",
    ],
    "is_fiction": False,
    "medium_origin": "ear",
    "tier": "P2",
    "tts_risk": False,
    "output_support": "both",
    "factual_regime": "strict",
    "category": "A",
    "rag": {
        "query_instruction": (
            "Retrieve a data-driven explainer passage that unpacks why an "
            "everyday price, rule, or behavior works the way it does:"
        ),
        "framing": (
            "Study how the writer turns one concrete question into a systems "
            "explanation. Notice how each step is carried by a sourced number "
            "made tangible, and how the piece lands on a concrete takeaway."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": """STYLE: Question-Driven Explainer
= One everyday puzzle unpacked with data until a system becomes visible. Curiosity in, policy out.

STRUCTURE:
1. PUZZLE OPEN -- Begin with a concrete, slightly absurd question from ordinary life ("Why does insulin cost ten times more here?").
2. FAILED FIRST ANSWER -- Offer the obvious explanation, then show with one data point why it cannot be the whole story.
3. DATA BEATS -- Walk the real mechanism in steps, each step carried by one number, one comparison, or one chart described in prose.
4. THE PIVOT -- Midway, reframe: the real story is the system behind the price or rule, not the thing itself. Say so in your own words.
5. POLICY TAKEAWAY -- Close with what could change and who has the power to change it -- concrete, not utopian.

VOICE: Bright, fast, friendly, present tense -- a sharp friend who brought the receipts. Second-person address welcome.
SIGNATURE MOVES:
- Make every abstract number tangible with a comparison the listener can hold ("that's three months of rent").
- Attribute every statistic to its source in the same breath as the number.
- Voice the listener's likely objection out loud, then answer it with data, not assertion.
FORBIDDEN: Outrage without data. Ending on a shrug -- always land the takeaway.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "puzzle_open", "failed_first_answer", "system_pivot",
            "policy_takeaway",
        ],
        "banned_tells": [
            "here's the thing",
            "let me explain",
            "so how did we get here",
        ],
    },
},

"sports_mythic": {
    "display_name": "Sports Mythic",
    "aliases": [
        "sports mythic", "sports_mythic", "30 for 30", "thirty for thirty",
        "sports documentary", "sports tragedy", "sports legend",
    ],
    "is_fiction": False,
    "medium_origin": "ear",
    "tier": "P2",
    "tts_risk": False,
    "output_support": "both",
    "factual_regime": "strict",
    "category": "A",
    "rag": {
        "query_instruction": (
            "Retrieve a factual sports narrative about an athlete's rise, "
            "defining moment, and aftermath:"
        ),
        "framing": (
            "Study how the writer builds an athlete into a tragic or redemptive "
            "hero using exact scores, dates, and records. Notice how one game or "
            "one play is slowed down to carry the whole arc."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": """STYLE: Sports Mythic
= Sport told as Greek tragedy. The game is fate; the athlete is a hero with one flaw the ending already knows.

STRUCTURE:
1. REFRAME COLD OPEN -- Open on a hypothetical that flips what the listener thinks they know about this story -- your own phrasing, never the stock line.
2. THE ASCENT -- Build the athlete or team from obscurity in concrete detail: the gym, the block, the bad knee, the doubters.
3. THE MOMENT -- Slow one game, one play, one night to near-freeze. This is the fulcrum the whole piece balances on.
4. THE TURN -- Triumph curdles or redemption lands: hubris, injury, scandal, or grace. Fate collects what it is owed.
5. LEGACY CODA -- End years later: what the moment means now, to the athlete and to everyone who watched it live.

VOICE: Reverent, muscular, deliberate. Past tense for the arc; present tense inside The Moment. Every stat is a plot point, never trivia.
SIGNATURE MOVES:
- Treat the game as fate: plant the ending in small early details and let the listener feel it coming.
- Give scores, times, and records exactly -- precision is the mythmaker's proof.
- Let rivals, coaches, and witnesses speak (attributed or paraphrased) like a chorus around the hero.
FORBIDDEN: Highlight-reel listing of achievements. Inventing locker-room scenes no source reports.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "reframe_cold_open", "the_moment_slowdown", "fate_foreshadow",
            "legacy_coda",
        ],
        "banned_tells": [
            "what if i told you",
            "30 for 30",
        ],
    },
},

"archival_elegiac": {
    "display_name": "Archival Elegiac",
    "aliases": [
        "archival elegiac", "archival_elegiac", "ken burns",
        "letters and diaries", "archival documentary", "sepia history",
    ],
    "is_fiction": False,
    "medium_origin": "ear",
    "tier": "P2",
    "tts_risk": False,
    "output_support": "both",
    "factual_regime": "strict",
    "category": "A",
    "rag": {
        "query_instruction": (
            "Retrieve a historical passage built on letters, diaries, or "
            "firsthand documents from ordinary people:"
        ),
        "framing": (
            "Study how quoted documents carry the emotion while the narrator "
            "stays restrained. Notice how one individual's record becomes the "
            "lens on the larger calamity."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": """STYLE: Archival Elegiac
= History mourned through its documents. Letters and diaries read aloud; one ordinary life stands for the whole calamity.

STRUCTURE:
1. STILL IMAGE OPEN -- Begin on a photograph, a field, a dated artifact, described slowly, the way a camera would pan across it.
2. THE INDIVIDUAL LENS -- Choose one ordinary person from the record -- a private, a nurse, a farmer -- and let the great event arrive through their days.
3. DOCUMENTS SPEAK -- Quote letters, diaries, and dispatches at length, naming writer, recipient, and date BEFORE the words.
4. THE WIDE SHOT -- Periodically pull back to the sweep -- numbers, maps, movements -- then return to the person.
5. ELEGIAC CLOSE -- End on what became of them, in one restrained paragraph, and let the silence afterward do the grieving.

VOICE: Measured, warm, sorrowful third person, past tense throughout. Sentences with the patience of a slow pan -- never hurried, never shrill.
SIGNATURE MOVES:
- Read the documents "aloud": introduce each with author and date, and let the period language stand unmodernized.
- Anchor emotion in objects -- a mended coat, an unsent letter -- rather than in adjectives.
- Mark time by seasons and harvests as much as by battles and proclamations.
FORBIDDEN: Melodrama the documents do not contain. Modern slang inside or around quoted material.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "still_image_open", "individual_lens", "documents_read_aloud",
            "elegiac_close",
        ],
        "banned_tells": [
            "a nation torn asunder",
            "lost to history",
        ],
    },
},

"collage_history": {
    "display_name": "Collage History",
    "aliases": [
        "collage history", "collage_history", "adam curtis",
        "hypernormalisation", "ironic montage history", "systems history",
    ],
    "is_fiction": False,
    "medium_origin": "ear",
    "tier": "P3",
    "tts_risk": False,
    "output_support": "both",
    "factual_regime": "strict",
    "category": "A",
    "rag": {
        "query_instruction": (
            "Retrieve a passage on recent political, economic, or technological "
            "history where a confident system produced unintended consequences:"
        ),
        "framing": (
            "Study how the writer cuts between storylines and decades, and how "
            "ironic juxtaposition replaces commentary. Notice the flat "
            "declarative delivery and the clearly dated jumps."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": """STYLE: Collage History
= Recent history as ironic montage. Confident systems built on a dream; a strange rupture; consequences nobody intended.

STRUCTURE:
1. CONFIDENT WORLD -- Open on an era sure of itself: name the system (finance, tech, politics) and the dream it sold, in flat declarative sentences.
2. THE RUPTURE -- Pivot abruptly to a strange, specific, dated event, and state plainly that it changed everything -- your own phrasing, never the stock line.
3. THREAD-JUMPING -- Cut between two or three storylines (a politician, an engineer, an idea) across decades. Trust the listener to hold the threads.
4. UNINTENDED CONSEQUENCE -- Show the fix becoming the new trap: the machinery built to control produced exactly the chaos it feared.
5. COLD CLOSE -- End in the present, still inside the system, with the question of who is dreaming whom left open.

VOICE: Flat, declarative, hypnotically calm third person in simple past. Short assertive sentences. The irony lives in the cut, never in the tone.
SIGNATURE MOVES:
- Juxtapose without commentary: place the utopian claim beside the grubby outcome and simply move on.
- Date every jump aloud ("In 1971...", "By the mid-nineties...") -- the collage must stay navigable by ear.
- Attribute each era's beliefs to named thinkers and institutions, then show the belief failing in practice.
FORBIDDEN: Explaining the irony after showing it. Conspiracy framing -- systems fail through incentives, not puppet-masters.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "confident_world_open", "strange_rupture", "thread_jump",
            "unintended_consequence",
        ],
        "banned_tells": [
            "but then something strange happened",
            "this is a story about how",
            "it was a fantasy",
        ],
    },
},

"intimate_nature": {
    "display_name": "Intimate Nature",
    "aliases": [
        "intimate nature", "intimate_nature", "attenborough",
        "david attenborough", "bbc earth", "planet earth",
        "hushed nature documentary",
    ],
    "is_fiction": False,
    "medium_origin": "ear",
    "tier": "P2",
    "tts_risk": False,
    "output_support": "both",
    "factual_regime": "strict",
    "category": "A",
    "rag": {
        "query_instruction": (
            "Retrieve a closely observed factual account of animal behavior "
            "in its natural habitat:"
        ),
        "framing": (
            "Study how precisely observed behavior becomes narrative. Notice "
            "the patient present-tense pacing and how the animal earns "
            "individuality through attention, never through invention."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": _P23_NATURE_DOC_BASE + """VARIANT: Intimate Nature (hushed close-observation register):
- VOICE: Hushed, close, unhurried -- narrate as if crouched a few metres away, unwilling to disturb the animal.
- Slow the pacing to the animal's own tempo: let one small behavior -- a first flight, a grooming, a lesson -- fill a whole passage.
- Choose reverence over spectacle: the wonder is that this happens at all, daily, unseen, and the listener is privileged to watch.
- Grant individuality through attention, not names: "the female", "her second chick", "the old male".
- Close on quiet continuity: night falls, the season turns, life goes on without us.
FORBIDDEN: Doom voiceover. Loud superlatives. Peril inflated beyond what observation honestly supports.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "habitat_arrival", "animal_protagonist", "tempo_of_the_animal",
            "quiet_continuity_close",
        ],
        "banned_tells": [
            "nowhere else on earth",
            "for the very first time",
        ],
    },
},

"ecstatic_doom_nature": {
    "display_name": "Ecstatic Doom Nature",
    "aliases": [
        "ecstatic doom nature", "ecstatic_doom_nature", "herzog",
        "werner herzog", "grizzly man", "existential nature",
        "nature monotone",
    ],
    "is_fiction": False,
    "medium_origin": "ear",
    "tier": "P3",
    "tts_risk": False,
    "output_support": "both",
    "factual_regime": "strict",
    "category": "A",
    "rag": {
        "query_instruction": (
            "Retrieve a factual nature or wilderness passage where the natural "
            "world appears violent, chaotic, or indifferent:"
        ),
        "framing": (
            "Study how accurate natural detail can carry existential weight. "
            "Notice where observation tips into philosophical reflection and "
            "how beauty and dread are held in the same sentence."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": _P23_NATURE_DOC_BASE + """VARIANT: Ecstatic Doom Nature (existential-monotone register):
- VOICE: Flat, deliberate, formally composed monotone. Long declarative sentences delivered without warmth -- the calm makes the content more disturbing.
- See nature as indifferent chaos: describe the same jungle or ice the intimate variant would, then state plainly that it does not care.
- Interrupt observation with uncomfortable philosophical asides -- on death, futility, the absurdity of the observer -- then return to the animal as if nothing happened.
- Find the grotesque inside the beautiful and say so without apology: the parasite in the orchid, the frenzy beneath the birdsong.
- Close on an unresolved metaphysical note, never on comfort.
FORBIDDEN: Cheerfulness or reassurance. The hushed reverence of the intimate variant -- awe here is dread spoken aloud.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "indifference_stated", "philosophical_aside", "grotesque_in_beauty",
            "unresolved_close",
        ],
        "banned_tells": [
            "overwhelming indifference of nature",
            "chaos, hostility, and murder",
            "ecstatic truth",
        ],
    },
},

"embedded_gritty": {
    "display_name": "Embedded Gritty Feature",
    "aliases": [
        "embedded gritty feature", "embedded_gritty", "vice", "vice style",
        "embedded reporting", "on the ground reporting", "gritty feature",
    ],
    "is_fiction": False,
    "medium_origin": "ear",
    "tier": "P3",
    "tts_risk": False,
    "output_support": "both",
    "factual_regime": "strict",
    "category": "A",
    "rag": {
        "query_instruction": (
            "Retrieve a first-person reported feature from inside a dangerous, "
            "illicit, or closed-off world:"
        ),
        "framing": (
            "Study how the reporter's presence and negotiated access shape the "
            "story. Notice the sensory street-level detail, the subjects quoted "
            "in their own words, and the refusal to resolve neatly."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": """STYLE: Embedded Gritty Feature
= A reporter inside the story, telling you what it smelled like. Access is the scoop; discomfort is the proof.

STRUCTURE:
1. IN MEDIAS RES -- Open already inside: the checkpoint, the squat, the lab, the back room. First person, present tense, mid-scene.
2. HOW WE GOT IN -- Briefly own the access: who agreed to talk, what was negotiated, what cannot be shown. The door is part of the story.
3. GROUND SCENES -- Move through two or three encounters, each built from sensory grit: sounds, smells, broken objects, what people wore and said.
4. THE WIDER RACKET -- Zoom out once to the system that produces this place -- economics, policy, supply chain -- in plain, unofficial language.
5. UNRESOLVED EXIT -- Leave the way reporters actually leave: abruptly, with the problem intact and one image that will not shake off.

VOICE: First person, present tense, plainspoken and a little raw. Short sentences under pressure. React honestly -- unease, absurdity -- without becoming the hero.
SIGNATURE MOVES:
- Report the texture a news desk cannot: street prices, slang translated, the waiting, the paperwork, the boredom between dangers.
- Quote subjects in their own words, attributed, self-justifications included -- let the listener judge.
- Register danger with understatement: state the fact of the risk and keep moving.
FORBIDDEN: War-tourism swagger; the reporter is a witness, not the hero. Sanitizing subjects into monsters or saints.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "in_medias_res_open", "access_owned", "sensory_grit",
            "unresolved_exit",
        ],
        "banned_tells": [
            "vice news",
            "we got exclusive access",
        ],
    },
},

    # === B-authorial ===

    "witty_wonder": {
        "display_name": "Witty Wonder",
        "aliases": [
            "witty wonder", "witty_wonder", "bryson", "bill bryson",
            "funny trivia nonfiction",
        ],
        "is_fiction": False,
        "medium_origin": "page",
        "tier": "P2",
        "tts_risk": True,
        "output_support": "both",
        "factual_regime": "strict",
        "category": "B",
        "rag": {
            "query_instruction": (
                "Retrieve a humorous nonfiction passage that delivers dense "
                "factual trivia through a self-deprecating amateur narrator:"
            ),
            "framing": (
                "Study how the humor rides ON TOP of verifiable facts: the "
                "narrator's own bafflement licenses the tour, and asides fold "
                "into the prose in one sentence before returning to the fact. "
                "Notice large numbers translated into homely comparisons."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": _P23_COMIC_NONFIC_BASE + """VARIANT: Witty Wonder (amiable-amateur register)
STRUCTURE:
1. HAPLESS ENTRANCE -- Open with the narrator's own ignorance or mishap with the subject; the confession licenses the tour.
2. TRIVIA AVALANCHE -- Stack improbable-but-true facts in rising order of absurdity; give each one beat of reaction before the next lands.
3. THE ECCENTRICS -- Introduce the forgotten scientists and obsessives behind the facts, each with one absurd, documented biographical detail.
4. WONDER PAYOFF -- Step back and be sincerely astonished that any of it is true, works, or was ever discovered at all.
VOICE: First person; past tense for anecdotes, present tense for facts; the register of a well-read friend who cannot quite believe what he is telling you.
SIGNATURE MOVES:
- Convert every large number into a domestic comparison (teaspoons, garden sheds, a queue around the block).
- Self-deprecate about comprehension, never about effort: the narrator is baffled but diligent.
- Give every historical figure exactly one ridiculous true detail, then treat their work with respect.
FORBIDDEN: Cynicism about the subject. Footnote formatting -- asides live inside the prose. Asides outnumbering facts.
""",
        "style_rules_editor": "",
        "register_spec": {
            "required_moves": [
                "hapless_entrance",
                "trivia_avalanche",
                "domestic_comparison",
                "wonder_payoff",
            ],
            "banned_tells": [
                "a short history of nearly everything",
                "notes from a small island",
                "i come from des moines",
            ],
        },
    },

    "character_driven_systems": {
        "display_name": "Character-Driven Systems",
        "aliases": [
            "character-driven systems", "character driven systems",
            "character_driven_systems", "michael lewis", "maverick lens",
        ],
        "is_fiction": False,
        "medium_origin": "page",
        "tier": "P2",
        "tts_risk": False,
        "output_support": "both",
        "factual_regime": "strict",
        "category": "B",
        "rag": {
            "query_instruction": (
                "Retrieve a narrative passage that explains a complex system, "
                "market, or institution through one insider character's "
                "decisions and bets:"
            ),
            "framing": (
                "Study how every mechanism of the system arrives ATTACHED to a "
                "scene where the character needs it -- jargon translated the "
                "moment it appears. Notice the dramatic irony: the reader "
                "learns what the maverick knows while the institutions stay blind."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": """STYLE: Character-Driven Systems
= A complex system explained through the one stubborn insider who saw it was broken. The character is the syllabus.

STRUCTURE PER CHAPTER:
1. MISFIT INTRO -- Open on the maverick in a scene that shows the exact trait letting them see what others cannot; state what everyone else believes.
2. THE SYSTEM AS SEEN -- Teach the system only as the character collides with it; every mechanism arrives attached to a decision they must make.
3. THE BET -- The character acts against consensus; render the stakes in hard numbers.
4. THE RECKONING -- The system proves them right or wrong; say what the outcome exposes about the system, not just the person.

VOICE: Third person, wry, fascinated; past tense for scenes, present tense for how the system works. A brilliant explainer hiding inside a character writer.
SIGNATURE MOVES:
- Never explain a mechanism in the abstract: translate each piece of jargon within one sentence, using an analogy from the character's own world.
- Milk dramatic irony: state plainly what the smart money believed and exactly why it was wrong.
- Render incentives as motive -- show who was paid not to see the problem.
- Keep a scorecard: track the character's wager in recurring, dated numbers.
FORBIDDEN: Explainer sections detached from any character. Sainting the maverick -- their flaws must stay load-bearing.
""",
        "style_rules_editor": "",
        "register_spec": {
            "required_moves": [
                "maverick_lens",
                "jargon_via_character",
                "consensus_wrong_reveal",
                "numbered_stakes",
            ],
            "banned_tells": [
                "moneyball",
                "the big short",
                "liar's poker",
                "flash boys",
            ],
        },
    },

    "braided_thriller_history": {
        "display_name": "Braided Thriller History",
        "aliases": [
            "braided thriller history", "braided_thriller_history",
            "erik larson", "larson", "dual timeline history",
        ],
        "is_fiction": False,
        "medium_origin": "page",
        "tier": "P2",
        "tts_risk": False,
        "output_support": "both",
        "factual_regime": "strict",
        "category": "B",
        "rag": {
            "query_instruction": (
                "Retrieve a documented historical narrative that intercuts two "
                "storylines converging toward a single real event:"
            ),
            "framing": (
                "Study the braid mechanics: how each strand's chapter ends on "
                "an unresolved documented moment, how dates and clock-times "
                "work as suspense hardware, and how the intervals tighten as "
                "the two strands approach their collision."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": _P23_NOVELISTIC_NONFIC_BASE + """VARIANT: Braided Thriller History (dual-strand register)
STRUCTURE:
1. TWO OPENINGS -- Introduce two documented strands in separate scenes: one building toward light (a fair, a voyage, an invention), one moving in dark parallel (a killer, a storm, a torpedo). Date-stamp both.
2. THE BRAID -- Alternate strands chapter by chapter; each handoff cuts on an unresolved, documented moment.
3. TIGHTENING -- Shorten the alternation and the time-gaps as the strands near each other; the reader feels the distance closing before the characters do.
4. CONVERGENCE -- The collision scene, rendered minute by minute from the record.
5. AFTERMATH CODA -- One quiet closing passage on what became of each strand's people.
VOICE: Third person, past tense, cinematic pacing; dates and clock-times spoken as beats of pressure.
SIGNATURE MOVES:
- End a strand's chapter mid-motion; resolve it only in that strand's NEXT chapter, never immediately.
- Cross-echo: let one image or object from one strand recur in the other before they meet.
- Keep the dark strand cold and procedural, the light strand warm and ambitious -- the contrast is the engine.
FORBIDDEN: Revealing the convergence early. Leaving a strand unattended for more than two chapters.
""",
        "style_rules_editor": "",
        "register_spec": {
            "required_moves": [
                "dual_timeline_braid",
                "handoff_cliffhanger",
                "interval_tightening",
                "strand_convergence",
            ],
            "banned_tells": [
                "the devil in the white city",
                "dead wake",
                "in the garden of beasts",
            ],
        },
    },

    "comic_science": {
        "display_name": "Comic Science",
        "aliases": [
            "comic science", "comic_science", "mary roach",
            "funny science", "gross science",
        ],
        "is_fiction": False,
        "medium_origin": "page",
        "tier": "P3",
        "tts_risk": True,
        "output_support": "both",
        "factual_regime": "strict",
        "category": "B",
        "rag": {
            "query_instruction": (
                "Retrieve a factual science passage that investigates a taboo "
                "or bodily question with deadpan humor and real research detail:"
            ),
            "framing": (
                "Study the deadpan engine: appalling material described with "
                "lab-report precision, experts answering gross questions in "
                "ordinary tones, and the narrator's squeamishness logged as "
                "data. The flatter the delivery, the funnier the fact."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": _P23_COMIC_NONFIC_BASE + """VARIANT: Comic Science (deadpan-investigator register)
STRUCTURE:
1. IMPOLITE QUESTION -- Open with the question nobody asks in polite company, asked with complete earnestness.
2. FIELD VISIT -- Go where the answer lives (lab, morgue, test facility, archive) and report the visit deadpan, procedure by procedure.
3. THE EXPERTS -- Let scientists answer gross questions matter-of-factly; the comedy is the mismatch between subject and tone, never a wink.
4. ANSWER PLUS RESIDUE -- Answer the opening question plainly, then admit the new, weirder question the answer has raised.
VOICE: First person, deadpan; shameless curiosity, zero smirk; the flatter the description of the appalling, the funnier it gets.
SIGNATURE MOVES:
- Describe taboo material (cadavers, digestion, decay) with lab-report precision -- exact verbs, exact quantities, no euphemism.
- Put the narrator's own squeamishness on the record as one more data point, then proceed anyway.
- Quote experts saying extraordinary things in ordinary tones and let the quote sit unadorned.
FORBIDDEN: Euphemism. Gross-out beats with no fact attached.
""",
        "style_rules_editor": "",
        "register_spec": {
            "required_moves": [
                "impolite_question_open",
                "deadpan_field_visit",
                "expert_mismatch_quote",
                "answer_plus_residue",
            ],
            "banned_tells": [
                "the curious lives of human cadavers",
                "adventures on the alimentary canal",
                "the curious science of life in the void",
            ],
        },
    },

    "clinical_compassion": {
        "display_name": "Clinical Compassion",
        "aliases": [
            "clinical compassion", "clinical_compassion", "oliver sacks",
            "sacks", "case study portrait",
        ],
        "is_fiction": False,
        "medium_origin": "page",
        "tier": "P2",
        "tts_risk": False,
        "output_support": "both",
        "factual_regime": "strict",
        "category": "B",
        "rag": {
            "query_instruction": (
                "Retrieve a medical or psychological case study told as a "
                "compassionate human portrait with precise clinical detail:"
            ),
            "framing": (
                "Study how the case moves SYMPTOM to PERSON to MEANING: the "
                "condition rendered first as lived experience, then named with "
                "clinical exactness, then opened into what it reveals about "
                "every mind. Notice dignity doing the work sentiment usually does."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": """STYLE: Clinical Compassion
= Case study as human portrait. Medical precision held in warmth; the condition reveals the person, never replaces them.

STRUCTURE PER CASE:
1. PRESENTING MOMENT -- Open in a scene where the symptom shows itself in daily life -- a street, a kitchen, a concert -- not in a chart.
2. THE CLINICAL PICTURE -- Name the condition precisely; give the mechanism briefly; define each term at first use.
3. THE PERSON BENEATH -- Biography, work, loves, humor; how they adapt, compensate, and build a life around or inside the condition.
4. THE MEANING TURN -- What this way of being reveals about how all minds construct their worlds; close on the patient's own dignity, ideally their own recorded words.

VOICE: First-person physician-narrator, humble and curious; past tense for encounters, present tense for reflection; exactness without coldness.
SIGNATURE MOVES:
- Write the symptom from inside as experience before naming it from outside as diagnosis.
- Let the patient surprise you on the page; record your own corrected assumption explicitly.
- Treat adaptation as achievement: catalogue what remains and what grows, not only what is lost.
- Allow ONE short philosophical widening per case -- from this mind to mind itself -- and make it earned.
FORBIDDEN: The patient as specimen, marvel, or freak. Miracle-cure arcs. Pity in any sentence.
""",
        "style_rules_editor": "",
        "register_spec": {
            "required_moves": [
                "symptom_lived_first",
                "person_beneath_diagnosis",
                "adaptation_as_achievement",
                "meaning_turn",
            ],
            "banned_tells": [
                "the man who mistook his wife for a hat",
                "an anthropologist on mars",
                "the island of the colorblind",
            ],
        },
    },

    "world_weary_travel": {
        "display_name": "World-Weary Travel",
        "aliases": [
            "world-weary travel", "world weary travel", "world_weary_travel",
            "bourdain", "anthony bourdain", "food travel",
        ],
        "is_fiction": False,
        "medium_origin": "ear",
        "tier": "P2",
        "tts_risk": False,
        "output_support": "both",
        "factual_regime": "strict",
        "category": "B",
        "rag": {
            "query_instruction": (
                "Retrieve a travel narration passage where food, cooks, and "
                "eating carry the culture, history, and politics of a place:"
            ),
            "framing": (
                "Study how one dish becomes the door into a place, how the "
                "people who cook carry the history lesson, and how every "
                "sentimental swell gets undercut within a line or two. Notice "
                "the spoken rhythm -- this voice is written to be said aloud."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": """STYLE: World-Weary Travel
= Place through appetite. A traveled, scarred narrator who trusts the food and the cooks more than the brochure.

STRUCTURE:
1. ARRIVAL, NO ROMANCE -- Land mid-motion: heat, noise, smell, traffic. No postcard establishing shot.
2. THE TABLE -- Get to the food fast; one dish becomes the door into the place.
3. THE PEOPLE WHO FEED YOU -- Cooks, vendors, hosts as the protagonists; their personal history carries the region's history.
4. THE UNCOMFORTABLE TRUTH -- Politics, war, poverty, or memory enter through the meal. Do not resolve them.
5. GRACE NOTE CLOSE -- Earned tenderness, undercut once so it never goes soft.

VOICE: First person, spoken rhythm -- short muscular sentences with sudden lyric swells; irreverent grit without gratuitous shock. Write every line to be said aloud.
SIGNATURE MOVES:
- Describe food with hunger, not refinement: texture, fat, smoke, heat -- never menu-speak.
- Take the anti-tourist stance: mock your own outsiderness before you mock anything local.
- Let one meal carry the history lesson: who eats this, who used to, and what that says.
- Undercut every sentimental swell within a line or two.
FORBIDDEN: Listicle traveltainment ("top five things to eat in..."). Exoticizing the locals. Menu adjectives ("delectable", "mouthwatering").
""",
        "style_rules_editor": "",
        "register_spec": {
            "required_moves": [
                "arrival_no_romance",
                "dish_as_door",
                "cooks_as_protagonists",
                "sentiment_undercut",
            ],
            "banned_tells": [
                "kitchen confidential",
                "parts unknown",
                "your body is not a temple",
            ],
        },
    },

    "literary_true_crime": {
        "display_name": "Literary True Crime",
        "aliases": [
            "literary true crime", "literary_true_crime", "capote",
            "truman capote", "in cold blood style", "nonfiction novel",
        ],
        "is_fiction": False,
        "medium_origin": "page",
        "tier": "P2",
        "tts_risk": False,
        "output_support": "both",
        "factual_regime": "strict",
        "category": "B",
        "rag": {
            "query_instruction": (
                "Retrieve a documented crime narrative reconstructed "
                "novelistically, with scenes, interiority, and sourced detail:"
            ),
            "framing": (
                "Study how dread is built from ordinary documented detail -- "
                "the unlocked door, the radio left on -- and how the "
                "perpetrators receive full human interiority from the record "
                "without a syllable of absolution. The calmer the prose, the "
                "worse the knowledge."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": _P23_NOVELISTIC_NONFIC_BASE + """VARIANT: Literary True Crime (quiet-dread register)
STRUCTURE:
1. THE PLACE BEFORE -- Open on the town and the victims in full ordinary life, unaware; land, weather, and routine rendered with novelistic patience.
2. CONVERGING PATHS -- Alternate the victims' last ordinary days with the perpetrators' approach, both tracks in the same calm register.
3. THE ACT, OBLIQUE -- Render the crime through aftermath, discovery, and testimony -- never as choreographed spectacle.
4. THE LONG AFTER -- Investigation, capture, trial, sentence; the community's changed weather is the final measure of the crime.
VOICE: Third person, unhurried, exact; a cool composed surface over deep feeling; sentences never breathless.
SIGNATURE MOVES:
- Grant the perpetrators full documented interiority -- childhood, grievance, self-justification -- without one syllable of absolution.
- Build dread from ordinary detail: the coffee cups, the unlocked door, the dog that did not bark.
- Slow the prose down as the horror approaches; calm is the instrument of dread.
FORBIDDEN: Cliffhanger theatrics. Gore choreography. The narrator's own shock intruding on the record.
""",
        "style_rules_editor": "",
        "register_spec": {
            "required_moves": [
                "place_before_portrait",
                "converging_paths",
                "oblique_act",
                "ordinary_detail_dread",
            ],
            "banned_tells": [
                "in cold blood",
                "the nonfiction novel",
            ],
        },
    },

    "grand_sweep_history": {
        "display_name": "Grand Sweep History",
        "aliases": [
            "grand sweep history", "grand_sweep_history", "tuchman",
            "barbara tuchman", "panoramic history",
        ],
        "is_fiction": False,
        "medium_origin": "page",
        "tier": "P3",
        "tts_risk": False,
        "output_support": "both",
        "factual_regime": "strict",
        "category": "B",
        "rag": {
            "query_instruction": (
                "Retrieve an authoritative historical passage that compresses "
                "an era into confident panoramic prose anchored by vivid "
                "particulars:"
            ),
            "framing": (
                "Study the altitude control: a decade dispatched in a "
                "paragraph, then a full page on one telling afternoon. Notice "
                "judgments delivered as flat declaratives -- no hedging -- and "
                "every abstraction pinned to a named person, price, or date."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": """STYLE: Grand Sweep History
= The confident historian at altitude. Epochs compressed into elegant paragraphs; judgment delivered without apology.

STRUCTURE:
1. PORTENT OPENING -- Open on one grand scene that contains the age in miniature (a funeral, a parade, a court ceremony), rendered in full ceremonial detail.
2. THE FORCES -- Lay out the era's pressures -- dynastic, economic, ideological -- in panoramic paragraphs, each anchored by one vivid particular.
3. THE FOLLY IN MOTION -- Narrate decisions as they compound; let the reader watch rulers ignore what the page has already shown them.
4. THE VERDICT -- Close with explicit judgment: what this age got wrong and what it cost. Own the verdict.

VOICE: Third person, past tense, formal but never stiff; long balanced sentences broken by one cutting short one when judgment falls; wit dry and aristocratic.
SIGNATURE MOVES:
- Control altitude: dispatch a decade in a paragraph, then spend a page on one telling afternoon.
- Pin every abstraction to a named person, a document, a price, or a date.
- Deliver judgments as flat declaratives -- no "perhaps", no "arguably" -- supported by the record already shown.
- Deploy the telling particular: one detail of dress, menu, or protocol that betrays a civilization's state of mind.
FORBIDDEN: Hedge-words on judgments. Sociological jargon. Bloodless summary with no scene beneath it.
""",
        "style_rules_editor": "",
        "register_spec": {
            "required_moves": [
                "portent_opening",
                "epoch_in_paragraph",
                "telling_particular",
                "owned_verdict",
            ],
            "banned_tells": [
                "the guns of august",
                "a distant mirror",
                "the march of folly",
            ],
        },
    },

    "literary_reportage": {
        "display_name": "Literary Reportage",
        "aliases": [
            "literary reportage", "literary_reportage", "kapuscinski",
            "ryszard kapuscinski", "kapuściński", "correspondent poetics",
        ],
        "is_fiction": False,
        "medium_origin": "page",
        "tier": "P3",
        "tts_risk": False,
        "output_support": "both",
        "factual_regime": "strict",
        "category": "B",
        "rag": {
            "query_instruction": (
                "Retrieve a first-person reportage passage where one small "
                "concrete scene reveals how a political system or regime works:"
            ),
            "framing": (
                "Study how a checkpoint, a queue, or a piece of palace "
                "furniture is read as a text about power. Notice the sparse "
                "witness-narrator, rumor reported AS rumor, and the thesis "
                "never stated -- only built from concrete scenes."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": """STYLE: Literary Reportage
= The foreign correspondent as poet. One small scene made to hold an entire regime.

STRUCTURE:
1. THE SMALL SCENE -- Open on something minor and concrete: a checkpoint, a waiting room, a broken elevator, a cup of tea going cold.
2. THE WIDENING -- Read the scene as a text: what this queue, this ritual, this fear says about how power works here.
3. DISPATCHES -- Move through the country in short sensory fragments -- heat, dust, rumor, radio static -- each fragment carrying exactly one observation about the system.
4. THE HUMAN LEDGER -- End with the ordinary people who must live inside the machinery; their smallest gestures are the true record.

VOICE: First person but sparse -- a witness, not a hero; present tense for standing-there scenes, past tense for history; plain sentences, images doing the argument's work.
SIGNATURE MOVES:
- Let objects testify: what the palace furniture, the ration cards, the portraits on the walls reveal about the ruler.
- Report rumor AS rumor -- what people whisper is data about fear, and must be flagged as unverified.
- Render waiting, queues, and silence as political facts with causes.
- Never state the thesis; build it from three concrete scenes and trust the reader to assemble it.
FORBIDDEN: War-zone bravado. Statistics without a human standing beside them. Naming the thesis outright.
""",
        "style_rules_editor": "",
        "register_spec": {
            "required_moves": [
                "small_scene_open",
                "object_testimony",
                "rumor_as_rumor",
                "unstated_thesis",
            ],
            "banned_tells": [
                "shah of shahs",
                "the soccer war",
                "travels with herodotus",
            ],
        },
    },

    "polyphonic_testimony": {
        "display_name": "Polyphonic Testimony",
        "aliases": [
            "polyphonic testimony", "polyphonic_testimony", "alexievich",
            "svetlana alexievich", "oral history chorus", "witness chorus",
        ],
        "is_fiction": False,
        "medium_origin": "page",
        "tier": "P3",
        "tts_risk": True,
        "output_support": "both",
        "factual_regime": "strict",
        "category": "B",
        "rag": {
            "query_instruction": (
                "Retrieve first-person witness testimony about a historical "
                "event with raw, unpolished speech texture:"
            ),
            "framing": (
                "Study how the author nearly disappears: witnesses carry the "
                "register in their own broken syntax, repetitions, and sudden "
                "intimacies. Notice the montage logic -- voices ordered for "
                "collision, contradictions left standing, no narrator verdict."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": """STYLE: Polyphonic Testimony
= History as a chorus of witnesses. The voices speak; the author almost disappears.

STRUCTURE:
1. FRAME IN A WHISPER -- A thin authorial note: what was asked, where, when. Then step back.
2. THE VOICES -- First-person testimonies in sequence, each a monologue headed only by the speaker's name or role and age. Each voice keeps its own diction, obsessions, and pauses.
3. MONTAGE LOGIC -- Order voices for collision: official memory against private memory, a believer beside a doubter, a large event beside a domestic detail.
4. LAST VOICE LOW -- Close on the quietest testimony, unresolved. No summary.

VOICE: The narrator speaks only in fragments between testimonies -- a question asked, a room described in two lines. The witnesses carry the register.
SIGNATURE MOVES:
- Differentiate voices by vocabulary and rhythm: a nurse, a soldier, and a child of the event must be tellable apart blind.
- Keep speech texture: false starts, trailing sentences, repetitions, sudden intimacies ("I have never told anyone this").
- Let witnesses contradict each other and leave the contradiction standing on the page.
- Prefer domestic detail over spectacle: what they cooked, wore, buried, kept.
FORBIDDEN: A narrator who explains what the testimony means. Smoothing witness speech into essay prose. Witnesses the record does not support.
""",
        "style_rules_editor": "",
        "register_spec": {
            "required_moves": [
                "whisper_frame",
                "voice_montage",
                "speech_texture_kept",
                "contradiction_left_standing",
            ],
            "banned_tells": [
                "the unwomanly face of war",
                "voices from chernobyl",
                "secondhand time",
                "zinky boys",
            ],
        },
    },

    "gonzo": {
        "display_name": "Gonzo",
        "aliases": [
            "gonzo", "gonzo journalism", "hunter s thompson",
            "hunter thompson", "hunter s. thompson",
        ],
        "is_fiction": False,
        "medium_origin": "page",
        "tier": "P3",
        "tts_risk": True,
        "output_support": "both",
        "factual_regime": "strict",
        "category": "B",
        "rag": {
            "query_instruction": (
                "Retrieve a first-person journalistic passage where the "
                "reporter's own unraveling experience becomes the story:"
            ),
            "framing": (
                "Study how the failure to cover the assignment becomes the "
                "coverage: escalating personal chaos with the real subject "
                "flickering through it, then one dead-sober paragraph of "
                "insight that says the true thing better than straight "
                "reporting could."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": """STYLE: Gonzo
= The assignment eaten alive by the reporter. Subjective, excessive first-person journalism where the breakdown of the coverage IS the coverage.

STRUCTURE:
1. THE ASSIGNMENT -- State the ostensible story in one straight sentence. It is the last straight sentence.
2. THE DERAILMENT -- The pursuit goes immediately and personally wrong; logistics, paranoia, and appetite take over the reporting.
3. ESCALATION LADDER -- Each scene wilder than the last: confrontations, misjudgments, fevered interior monologue; keep the real subject flickering through the chaos.
4. THE SAVAGE CLARITY -- Mid-mayhem, drop one dead-sober paragraph of insight that says the true thing about the subject better than straight coverage could.
5. WRECKAGE CLOSE -- The story ends unfiled or unrecognizable; the narrator files the wreckage instead.

VOICE: First person, manic present or breathless past; long careening sentences that crash into short ones; hyperbole as an instrument; self-lacerating, never self-glorifying.
SIGNATURE MOVES:
- Make the reporter's failure to get the story the story's spine.
- Escalate concretely: each excess must top the previous scene's, on the page, in ascending order.
- Swing the register from rage to lyricism inside a single paragraph.
- Aim the savagery at power and at the narrator; never punch down.
FORBIDDEN: Detached objectivity. Glamorizing the excess as cool -- it must visibly cost. Substance-use how-to detail.
""",
        "style_rules_editor": "",
        "register_spec": {
            "required_moves": [
                "straight_sentence_assignment",
                "reporter_becomes_story",
                "escalation_ladder",
                "savage_clarity_beat",
            ],
            "banned_tells": [
                "fear and loathing",
                "buy the ticket, take the ride",
                "when the going gets weird, the weird turn pro",
                "bat country",
            ],
        },
    },

    # === C-audio ===

"sound_led_wonder": {
    "display_name": "Sound-Led Wonder",
    "aliases": [
        "sound led wonder", "sound_led_wonder", "sound-led wonder",
        "radiolab", "radio lab", "dialogic wonder", "wonder explainer",
    ],
    "is_fiction": False,
    "medium_origin": "ear",
    "tier": "P2",
    "tts_risk": False,
    "output_support": "both",
    "factual_regime": "strict",
    "category": "C",
    "rag": {
        "query_instruction": (
            "Retrieve a passage that works out a scientific or complex idea "
            "conversationally, with voiced doubts, corrections, and layered "
            "re-explanation:"
        ),
        "framing": (
            "Study how understanding is BUILT in front of the reader — the wrong "
            "first pass, the backtrack, the one-layer-at-a-time rebuild. Notice how "
            "confusion is confessed and treated as fuel, not hidden."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": """STYLE: Sound-Led Wonder
= A curious mind thinking out loud: gets the idea wrong, backs up, rebuilds it in layers until confusion turns into delight.

STRUCTURE PER CHAPTER:
1. NAIVE QUESTION -- Open on a small, almost childish question and take it completely seriously.
2. FIRST PASS, THEN CRACK -- Give the obvious answer... then break it: find the one detail that doesn't fit and say so out loud.
3. LAYERED REBUILD -- Rebuild the idea one layer at a time, each layer triggered by a new fact, objection, or paraphrased voice.
4. WONDER LANDING -- End the moment it clicks: one clean sentence of earned awe, bigger than the question that opened.

VOICE: First person, present tense -- the discovery is happening NOW, not being reported after the fact. Register drifts between "I", "we", and "you" as the listener is pulled in.
SIGNATURE MOVES:
- Interrupt yourself mid-explanation when a hole appears: stop, send the listener back ("hold on -- back up a step"), and re-tell that step right.
- Stage other perspectives as short paraphrased voices ("a physicist would tell you..."), then argue with them on the record.
- Confess confusion plainly and name exactly WHAT doesn't make sense before resolving it -- doubt is the engine, not a flaw.
- Add exactly ONE new element per pass; recap the stack in miniature before stacking the next layer.

FORBIDDEN: A lecture that arrives pre-understood -- if the narrator never doubts, it is the wrong style.
FORBIDDEN: Withholding the answer for false suspense; the pleasure is watching understanding assemble, not a twist.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "naive_question_open",
            "self_interrupt_backtrack",
            "layered_rebuild",
            "confessed_confusion",
        ],
        "banned_tells": [
            "this is radiolab",
            "i'm jad abumrad",
        ],
    },
},

"act_structure_personal": {
    "display_name": "Act-Structure Personal",
    "aliases": [
        "act structure personal", "act_structure_personal", "act structure",
        "this american life", "ira glass", "radio essay", "personal acts story",
    ],
    "is_fiction": False,
    "medium_origin": "ear",
    "tier": "P2",
    "tts_risk": False,
    "output_support": "both",
    "factual_regime": "strict",
    "category": "C",
    "rag": {
        "query_instruction": (
            "Retrieve a first-person narrative essay or radio story where an "
            "ordinary personal event quietly opens onto a larger theme:"
        ),
        "framing": (
            "Study the architecture: theme stated up front, then discrete acts, each "
            "angling at it through one person's story. Notice how mundane detail "
            "accumulates until it turns profound WITHOUT the narrator announcing it."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": """STYLE: Act-Structure Personal
= Radio-essay architecture: a stated theme, then acts -- ordinary people's stories told plainly until they open into quiet profundity.

STRUCTURE:
1. THEME PROLOGUE -- Open with one small concrete story or scene, then step out and name, in plain words, the theme it points to.
2. ACTS -- Divide the body into two or three acts, announced simply ("Act One. The Substitute."). Each act is one person's story angled at the theme from a different side.
3. THE TURN -- Inside each act, let mundane details accumulate until one of them quietly turns out to mean something larger. Never announce the turn; let it land.
4. SOFT CODA -- Return to the opening person or image for a few closing lines. End understated, one size smaller than the feeling.

VOICE: First-person host, conversational and unhurried. Present tense for the framing, past tense for the stories. Curious, warm, never ironic at a subject's expense.
SIGNATURE MOVES:
- Frame each act with gentle host reasoning: what drew you to this story, what you expected, what actually surprised you.
- Paraphrase your subjects most of the way, then drop ONE short direct quote where their exact words beat anything you could write.
- Keep the diction radically plain; profundity comes from placement and timing, never from vocabulary.
- Ask one honest question per act that the story never fully answers -- and admit that it doesn't.

FORBIDDEN: Cynicism or mockery toward ordinary people; the register is tender curiosity.
FORBIDDEN: Stating the moral outright in the coda -- it gestures, it never explains.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "theme_prologue",
            "act_divisions",
            "mundane_to_profound_turn",
            "understated_coda",
        ],
        "banned_tells": [
            "i'm ira glass",
            "each week on our program we choose a theme",
            "stay with us",
        ],
    },
},

"guided_meditation": {
    "display_name": "Guided Meditation",
    "aliases": [
        "guided meditation", "guided_meditation", "meditation",
        "meditation script", "headspace", "mindfulness", "body scan",
    ],
    "is_fiction": False,
    "medium_origin": "ear",
    "tier": "P2",
    "tts_risk": False,
    "output_support": "video_only",
    "factual_regime": "fictional",
    "category": "C",
    "rag": {
        "query_instruction": (
            "Retrieve a calm, present-tense guided relaxation or body-awareness "
            "passage with slow pacing and simple physical anchors:"
        ),
        "framing": (
            "Study the pacing above all: one short instruction per breath, silence "
            "built in between, attention always anchored to something physically "
            "felt. Notice the invitational grammar -- nothing is ever commanded."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": """STYLE: Guided Meditation
= A voice that breathes with the listener. Every line is paced to one exhale; the script is an instrument for calm, not a text to be read.

STRUCTURE PER SESSION:
1. SETTLE -- Invite the listener to arrive: posture, softening the gaze or closing the eyes, one deliberate breath. Mark [pause] after each instruction.
2. BREATH ANCHOR -- Guide attention to the breath for several cycles. One short line per breath, each followed by [pause] or [silence 3s].
3. BODY SWEEP or SINGLE FOCUS -- Move attention slowly through the body, one region per line -- or hold one anchor: weight, warmth, sound. Never rushed.
4. RELEASE + RETURN -- Widen attention back out, offer one small closing kindness, then bring the listener gently back -- or let the final lines dissolve into [silence].

VOICE: Second person, present tense ONLY -- "you notice", never "you will notice". Low, warm, even; no enthusiasm, no urgency, no cleverness.
SIGNATURE MOVES:
- Write in breath units: each line speakable in ONE easy exhale (roughly five to ten words), then mark [pause].
- Anchor every instruction in something physically felt NOW: the weight of the hands, air at the nostrils, the belly rising. Nothing abstract for more than a line.
- Score the silence explicitly with [pause] and [silence Ns] markers -- silence is part of the script, and its share GROWS as the session deepens.
- Treat wandering attention as normal: name it kindly, guide it back, never frame it as failure.

FORBIDDEN: Story, plot, exciting imagery, or any claim about health outcomes -- this is attention, not medicine.
FORBIDDEN: Command register ("you must", "don't") -- every instruction is an invitation ("allowing", "if you like", "when you're ready").
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "breath_unit_lines",
            "present_tense_body_anchor",
            "pause_silence_markers",
            "gentle_return_close",
        ],
        "banned_tells": [
            "welcome to headspace",
            "take a nice big deep breath",
        ],
    },
},

    # === D-cinematic ===

"film_noir_vo": {
    "display_name": "Film Noir Voiceover",
    "aliases": [
        "film noir vo", "film_noir_vo", "film noir", "noir",
        "noir voiceover", "hardboiled", "hardboiled detective",
    ],
    "is_fiction": True,
    "medium_origin": "ear",
    "tier": "P2",
    "tts_risk": False,
    "output_support": "both",
    "factual_regime": "fictional",
    "category": "D",
    "rag": {
        "query_instruction": (
            "Retrieve a hardboiled first-person crime narration passage with "
            "cynical wit, urban atmosphere, and world-weary reflection:"
        ),
        "framing": (
            "Study how the narrator's wry, self-incriminating voice colors every "
            "fact. Notice the freshly minted cynical similes, the rain-and-neon "
            "atmosphere, and how doom is admitted from the first line."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": """STYLE: Film Noir Voiceover
= Hardboiled first-person narration over a city that never dries out. World-weary, wisecracking, already doomed.

STRUCTURE:
1. AFTERMATH OPEN -- Start after the damage is done: the narrator looks back at the moment the trouble walked in.
2. THE HOOK -- A client, a favor, a face in a doorway; the narrator knows better and takes the job anyway. Say so.
3. DESCENT -- Each lead peels back another layer of rot; every answer costs something. Let the city close in.
4. RUEFUL CLOSE -- End on what it cost. No redemption, no lesson -- a lit cigarette's worth of resignation.

VOICE: First person, past tense, wry and worn. Short declaratives, cut with one long tired sentence when the memory hurts.
SIGNATURE MOVES:
- Mint one fresh cynical simile per scene ("she smiled like a foreclosure notice") -- never a stock noir line.
- Paint in rain, neon, smoke, and shadow -- but earn each; weather is mood, not wallpaper.
- Let the narrator judge himself hardest: he saw the double-cross coming and walked in anyway.
- Play the femme-fatale beat as danger admitted, not a body described.

FORBIDDEN: Sincerity without irony. Verbatim lines, names, or places from classic noir films.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "aftermath_open", "fresh_cynical_simile", "rain_neon_palette",
            "rueful_close",
        ],
        "banned_tells": [
            "of all the gin joints", "the stuff that dreams are made of",
            "forget it, jake, it's chinatown", "it was a dark and stormy night",
        ],
    },
},

"ironic_moral_fable": {
    "display_name": "Ironic Moral Fable",
    "aliases": [
        "ironic moral fable", "ironic_moral_fable", "rod serling", "serling",
        "twilight zone", "twist fable", "moral twist tale",
    ],
    "is_fiction": True,
    "medium_origin": "ear",
    "tier": "P2",
    "tts_risk": False,
    "output_support": "both",
    "factual_regime": "fictional",
    "category": "D",
    "rag": {
        "query_instruction": (
            "Retrieve a short parable or twist tale where an ordinary person's "
            "flaw meets one impossible element and earns an ironic reckoning:"
        ),
        "framing": (
            "Study how the frame narration presents the protagonist like an "
            "exhibit and lets irony do the moralizing. Notice the single "
            "fantastic element and the twist that inverts the character's "
            "own stated wish."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": """STYLE: Ironic Moral Fable
= Anthology-television parable. A measured host presents one ordinary person, one impossible twist, and a moral served dry.

STRUCTURE:
1. HOST'S PORTRAIT -- Introduce the protagonist like an exhibit, in your OWN framing words: name, station, one flaw, one desire.
2. ORDINARY WORLD, ONE CRACK -- Establish the mundane routine, then admit a single impossible element without fanfare.
3. THE TURN -- The flaw and the impossible element meet; the protagonist gets exactly what they asked for, in the way they least wanted.
4. DRY MORAL CLOSE -- The host returns to deliver the lesson in two or three sentences, ironic and unsmiling, aimed just past the audience.

VOICE: Host bookends = formal, precise, present tense, faintly amused. Story body = close third person, past tense, plain.
SIGNATURE MOVES:
- Frame the tale with bookend host monologues; the host knows the ending and lets it show only in word choice.
- Make the twist an inversion of the protagonist's stated wish -- ironic justice, never random cruelty.
- Keep the impossible element singular: one device, one visitor, one rule. Everything else stays stubbornly normal.
- Let the moral indict the flaw, not the magic: the machinery only gave the character room to be themselves.

FORBIDDEN: Explaining the mechanism of the twist. More than one supernatural element. Signature phrases from classic anthology television.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "host_bookends", "single_impossible_element",
            "ironic_inversion_twist", "dry_moral_close",
        ],
        "banned_tells": [
            "submitted for your approval", "the twilight zone",
            "picture if you will", "you are traveling through another dimension",
        ],
    },
},

"whispered_existential": {
    "display_name": "Whispered Existential",
    "aliases": [
        "whispered existential", "whispered_existential", "malick",
        "terrence malick", "voiceover prayer", "existential whisper",
    ],
    "is_fiction": True,
    "medium_origin": "ear",
    "tier": "P3",
    "tts_risk": False,
    "output_support": "both",
    "factual_regime": "fictional",
    "category": "D",
    "rag": {
        "query_instruction": (
            "Retrieve a lyrical interior monologue of fragmented, unanswerable "
            "questions set against nature imagery and memory:"
        ),
        "framing": (
            "Study how meaning accrues through association rather than argument "
            "-- questions, images, silence. Notice the hushed address to an "
            "absent 'you' and how one concrete image answers what logic cannot."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": """STYLE: Whispered Existential
= Voiceover as prayer. Fragmented interior questions drift over images of nature and memory, hushed, addressed to someone absent.

STRUCTURE:
1. QUESTION INTO THE DARK -- Open on an unanswerable question, whispered to a "you" who may be God, the dead, or the world itself.
2. DRIFT -- Move by association, not plot: a remembered touch, light through leaves, a wound seen from very far inside.
3. TURN TOWARD GRACE -- Let one small concrete image answer what argument cannot -- provisionally, wordlessly.
4. OPEN CLOSE -- End on a question or an unfinished phrase. Nothing resolves; the light just changes.

VOICE: First person, present tense, hushed and halting. Fragments outnumber full sentences. Every line must survive being whispered.
SIGNATURE MOVES:
- Ask questions that skip logic ("Why do we let go? Where were you?") -- at most two per section, never answered directly.
- Set human pain against indifferent nature imagery: rivers, wheat, birds -- the world continuing regardless.
- Address the absent "you" throughout; the entire narration is one side of a prayer.
- Let sentences trail off... begin again... circle the same wound with new words.

FORBIDDEN: Plot exposition or scene-setting logistics. Irony, jokes, or a confident thesis. Verbatim lines from art-film voiceovers.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "whispered_question_open", "associative_drift",
            "nature_counterpoint", "unresolved_close",
        ],
        "banned_tells": [
            "the way of nature and the way of grace",
            "what's this war in the heart of nature",
        ],
    },
},

"magical_realism": {
    "display_name": "Magical Realism",
    "aliases": [
        "magical realism", "magical_realism", "magic realism", "marquez",
        "gabriel garcia marquez", "realismo magico",
    ],
    "is_fiction": True,
    "medium_origin": "page",
    "tier": "P3",
    "tts_risk": False,
    "output_support": "both",
    "factual_regime": "fictional",
    "category": "D",
    "rag": {
        "query_instruction": (
            "Retrieve a passage of generational saga prose where impossible "
            "events are narrated matter-of-factly amid rich sensory detail:"
        ),
        "framing": (
            "Study the inverted astonishment: miracles reported flatly, ordinary "
            "objects described with wonder. Notice generational time, recurring "
            "names, and the domestic consequences of the marvelous."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": """STYLE: Magical Realism
= Miracles reported in the same breath as laundry. Generational saga prose where the impossible is mundane and the mundane is astonishing.

STRUCTURE:
1. TIME-FOLDED OPEN -- Begin with a sentence of your OWN construction that folds two moments together: a future event remembered from a past afternoon.
2. GENERATIONAL WEAVE -- Move by family time: inherited names, repeated fates, a house or town aging alongside its people.
3. THE MIRACLE, DEADPAN -- When the impossible arrives (a levitation, a rain that lasts years, an unaging visitor), state it flatly and turn at once to its practical consequences.
4. CYCLICAL CLOSE -- End where a descendant repeats, fulfills, or finally breaks the pattern the opening promised.

VOICE: Third-person omniscient, past tense, long sensuous sentences dense with objects, smells, and heat. Written for the page; let clauses bloom.
SIGNATURE MOVES:
- Give the marvelous NO extra adjectives -- and give the ordinary (ice, soap, an orange) the language of wonder. Invert the astonishment.
- Measure time in generations and repetitions; let a name recur until fate reads as a family heirloom.
- Anchor every marvel in bureaucratic or domestic consequence: the priest complains, the neighbors charge admission.
- Saturate the senses -- every page should smell of something.

FORBIDDEN: Characters marveling at the magic or explaining it. Names, towns, or lines from existing magical-realist novels.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "time_folded_open", "deadpan_miracle", "generational_time",
            "sensory_abundance",
        ],
        "banned_tells": [
            "many years later, as he faced the firing squad", "macondo",
            "one hundred years of solitude",
        ],
    },
},

"fairy_tale_classic": {
    "display_name": "Fairy Tale Classic",
    "aliases": [
        "fairy tale classic", "fairy_tale_classic", "fairy tale", "grimm",
        "brothers grimm", "andersen", "hans christian andersen",
    ],
    "is_fiction": True,
    "medium_origin": "ear",
    "tier": "P2",
    "tts_risk": False,
    "output_support": "both",
    "factual_regime": "fictional",
    "category": "D",
    "rag": {
        "query_instruction": (
            "Retrieve a classic folk or fairy tale with a formula opening, "
            "threefold repetition, and a transformation ending:"
        ),
        "framing": (
            "Study the tale's bare, confident architecture: formula frames, the "
            "rule of three, conditions set and then broken. Notice how absolute "
            "rewards and punishments land without psychological explanation."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": _P23_ORAL_TALE_BASE + """VARIANT: Fairy Tale Classic (Grimm-to-Andersen register)
STRUCTURE:
1. DISTANCING OPEN -- A once-upon-a-time formula (own wording welcome) sets the tale long ago and far away, then names the lack: a childless miller, a lost crown, a hungry winter.
2. RULE OF THREE -- Three tasks, three visitors, or three nights; the first two fail or deceive, the third turns the tale.
3. TRANSFORMATION -- The reversal is physical and visible: beast to prince, rags to gold, pride to stone. Cruelty and reward are both allowed to be absolute.
4. MORAL CLOSE -- Seal the tale with a closing formula and, if a moral is drawn, one plain sentence -- sweet or sharp, never preachy.
VOICE: Third-person teller, past tense, simple clear sentences a child can follow and an adult can feel. Speak dark turns plainly, without gloating.
SIGNATURE MOVES:
- Keep magic rule-bound: every gift has a condition, and every condition will be broken.
- Reward kindness shown to the small and strange (the crone, the talking fish); punish greed with poetic symmetry.
- Repeat the charm or rhyme verbatim at each of the three beats.
FORBIDDEN: Psychological realism or moral ambiguity. Branded phrases, characters, or plots from famous published fairy tales.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "distancing_formula_open", "rule_of_three",
            "visible_transformation", "moral_close",
        ],
        "banned_tells": [
            "mirror, mirror, on the wall", "let down your hair",
            "what big eyes you have",
        ],
    },
},

"mythic_epic": {
    "display_name": "Mythic Epic",
    "aliases": [
        "mythic epic", "mythic_epic", "homeric", "homer", "epic poem",
        "iliad", "odyssey", "heroic epic",
    ],
    "is_fiction": True,
    "medium_origin": "ear",
    "tier": "P3",
    "tts_risk": False,
    "output_support": "both",
    "factual_regime": "fictional",
    "category": "D",
    "rag": {
        "query_instruction": (
            "Retrieve an elevated epic narration of heroic deeds with an "
            "invocation, fixed epithets, and rhythmic catalogues:"
        ),
        "framing": (
            "Study the oral machinery: repeated epithets, rolling catalogues, "
            "extended similes. Notice how ceremony and rhythm turn violence and "
            "grief into memory built for recitation."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": _P23_ORAL_TALE_BASE + """VARIANT: Mythic Epic (Homeric oral-formula register)
STRUCTURE:
1. INVOCATION -- Open by calling on a higher power -- muse, ancestor, the deep -- to speak THROUGH the teller, naming the deed and the hero in the same breath. Own wording, never the classical formula.
2. IN MEDIAS RES -- Enter the story mid-crisis; let earlier events arrive later as a tale told within the tale.
3. DEEDS AND TRIALS -- Battles, voyages, and councils in elevated cadence; gods or fates intervene with motives of their own.
4. HOMECOMING OR PYRE -- Close on rest earned or glory paid for: the hero returns, or the fire takes them, and the teller marks what will be remembered.
VOICE: Third-person bard, past tense, long rolling lines built for chanting -- rhythmic, ceremonious, never stiff.
SIGNATURE MOVES:
- Fix an invented epithet to each major figure ("storm-bred", "the twice-crowned") and repeat it at every entrance.
- Catalogue at least once: ships, clans, gifts, or the dead -- names rolled in rhythm until quantity becomes awe.
- Render violence with physical exactness, and grant even the fallen a lineage: name their father, their land, their unfinished field.
- Let similes run long: a warrior falls the way a pine falls, and the pine gets three lines.
FORBIDDEN: Understatement or slang inside the high register. Epithets, heroes, or lines lifted from classical epics.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "invocation_open", "fixed_epithets", "catalogue_beat",
            "extended_simile",
        ],
        "banned_tells": [
            "sing, o muse", "rosy-fingered dawn", "wine-dark sea",
            "swift-footed",
        ],
    },
},

"epistolary": {
    "display_name": "Epistolary — Letters & Diaries",
    "aliases": [
        "epistolary", "letters and diaries", "diary novel", "letters novel",
        "dracula", "dracula-style", "found documents",
    ],
    "is_fiction": True,
    "medium_origin": "page",
    "tier": "P3",
    "tts_risk": False,
    "output_support": "both",
    "factual_regime": "fictional",
    "category": "D",
    "rag": {
        "query_instruction": (
            "Retrieve a story told through dated letters or diary entries from "
            "multiple first-person correspondents:"
        ),
        "framing": (
            "Study how each dated document advances the plot while exposing its "
            "writer's blind spot. Notice the dramatic irony assembled BETWEEN "
            "entries and the work done by dates and the gaps between them."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": """STYLE: Epistolary
= A story assembled from documents. Letters, diary entries, and telegrams -- each dated, each partial -- add up to a truth no single writer can see.

STRUCTURE:
1. DOCUMENT OPEN -- Begin with a dated entry from one correspondent, mid-life and specific: a journey, an arrival, a small unease noted in passing.
2. ALTERNATING VOICES -- Rotate two to four writers with distinct registers; each entry advances the story AND reveals its writer's blind spot.
3. CONVERGENCE -- The documents begin answering one another: a warning sent too late, two entries describing one night from opposite sides.
4. FINAL ENTRY -- Close on the last document: a note that breaks off, a letter never sent, or a survivor's postscript dated long after.

VOICE: Every entry in its writer's own first-person voice and period diction; past tense within entries. No voice outside the documents.
SIGNATURE MOVES:
- Head EVERY entry with writer, form, and date ("From the diary of --, 3 May"); the dates are the skeleton -- keep them consistent and let the gaps between them speak.
- Engineer dramatic irony: let the reader assemble a danger the writers cannot see from inside their single entries.
- Keep each writer partial: confident where they are wrong, dismissive of what will destroy them.
- Interrupt at least one entry mid-thought; what stopped the pen matters more than what it wrote.

FORBIDDEN: An omniscient narrator smoothing between documents. Names, characters, or lines from famous epistolary novels.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "dated_document_headers", "alternating_partial_voices",
            "cross_entry_irony", "broken_final_entry",
        ],
        "banned_tells": [
            "children of the night, what music they make", "count dracula",
        ],
    },
},

"second_person_adventure": {
    "display_name": "Second-Person Adventure",
    "aliases": [
        "second person adventure", "second_person_adventure", "cyoa",
        "choose your own adventure", "interactive fiction", "you are the hero",
    ],
    "is_fiction": True,
    "medium_origin": "page",
    "tier": "P3",
    "tts_risk": False,
    "output_support": "both",
    "factual_regime": "fictional",
    "category": "D",
    "rag": {
        "query_instruction": (
            "Retrieve a second-person present-tense adventure passage that "
            "immerses 'you' in urgent scenes ending at decision points:"
        ),
        "framing": (
            "Study how second person plus present tense manufactures urgency and "
            "ownership. Notice scenes ending on forked choices framed as actions, "
            "and consequences that honor earlier decisions."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": """STYLE: Second-Person Adventure
= You are the hero. Present-tense interactive-fiction prose that puts the reader inside the story and hands them the choices.

STRUCTURE:
1. COLD IMMERSION -- Drop "you" straight into a situation with a goal and a ticking constraint: the torch is dying, the train leaves at nine.
2. EXPLORE AND ESCALATE -- Short scenes, each ending at a decision point; every choice narrows the path and raises the cost.
3. CONSEQUENCE BEATS -- Honor earlier choices: the rope you left behind, the stranger you trusted, returns to pay off.
4. EARNED ENDING -- Close on the outcome those choices built -- triumph, escape, or a fitting doom -- addressed to "you" to the last line.

VOICE: Second person, present tense, urgent and concrete. Short sentences in danger; slightly longer when it is safe to breathe.
SIGNATURE MOVES:
- Give "you" senses first, judgment second: cold air on your neck before any explanation of where you are.
- End scenes on forked decisions framed as actions, not menus ("You can force the lock, or follow the light below").
- Track a small inventory in prose -- what your hands carry becomes plot.
- Never describe "you" in a mirror; the reader supplies the face.

FORBIDDEN: Slipping into first or third person. Choices without consequence. Branded interactive-fiction phrasing or page-number mechanics.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "cold_second_person_open", "forked_decision_beats",
            "choice_consequence_payoff",
        ],
        "banned_tells": [
            "choose your own adventure", "turn to page",
        ],
    },
},

"mockumentary_deadpan": {
    "display_name": "Mockumentary Deadpan",
    "aliases": [
        "mockumentary deadpan", "mockumentary_deadpan", "mockumentary",
        "fake documentary", "spinal tap", "this is spinal tap",
    ],
    "is_fiction": True,
    "medium_origin": "ear",
    "tier": "P3",
    "tts_risk": True,
    "output_support": "both",
    "factual_regime": "fictional",
    "category": "D",
    "rag": {
        "query_instruction": (
            "Retrieve a deadpan documentary-style narration that treats a "
            "trivial or absurd subject with complete solemnity:"
        ),
        "framing": (
            "Study the gap between gravitas and content -- the comedy is never "
            "acknowledged. Notice the invented expert voices, overqualified "
            "titles, false-precision statistics, and the unwavering straight face."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": """STYLE: Mockumentary Deadpan
= A perfectly serious documentary about a perfectly absurd subject. The comedy lives in the gap between gravitas and content.

STRUCTURE:
1. GRAVE THESIS OPEN -- A measured narrator introduces the subject -- a village cheese-rolling dynasty, the world's third-best mime -- with the solemnity of a war documentary.
2. RISE ARC WITH EXPERTS -- Trace the subject's history through cut-in "interview" quotes from invented experts and insiders, each introduced with an overqualified title.
3. THE SETBACK -- Treat a trivial disaster (a mislabeled trophy, a feud over folding chairs) as the tragedy the participants believe it is.
4. UNEARNED UPLIFT CLOSE -- End on swelling documentary sincerity about legacy and dreams, played completely straight over the tiny subject.

VOICE: Narrator = calm, authoritative documentary register, present tense. Interviewees = first-person quotes, self-serious, blind to their own absurdity.
SIGNATURE MOVES:
- Never wink. The narrator believes every word; the comedy is the subject, not the tone.
- Escalate credentials absurdly but deliver them dry ("chair of competitive whistling studies, retired").
- Let interviewees contradict each other and the narrator's stated facts; move on without comment.
- Deploy statistics with false precision ("attendance has grown eleven percent since the incident").

FORBIDDEN: Jokes the narrator seems aware of. Sarcasm markers or laughing at the subject. Quotes or characters from famous mockumentaries.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "grave_thesis_open", "invented_expert_cutins",
            "trivial_tragedy_beat", "played_straight_close",
        ],
        "banned_tells": [
            "these go to eleven",
            "it's such a fine line between stupid and clever",
            "that's what she said",
        ],
    },
},

    # === E ===

"babad_hikayat": {
    "display_name": "Babad / Hikayat — Archaic Court Chronicle",
    "aliases": [
        "babad", "hikayat", "babad hikayat", "babad_hikayat",
        "babad / hikayat", "babad tanah jawi", "kronik keraton",
        "court chronicle", "kronik kerajaan", "sejarah keraton",
    ],
    "is_fiction": False,
    "medium_origin": "page",
    "tier": "P2",
    "tts_risk": False,
    "output_support": "both",
    "factual_regime": "hybrid",
    "category": "E",
    "rag": {
        "query_instruction": (
            "Retrieve a court-chronicle or hikayat passage in smooth archaic "
            "prose that records royal genealogy, successions, and omens "
            "surrounding a dynasty:"
        ),
        "framing": (
            "Study the scribe's reverent, unhurried register: genealogy as "
            "the foundation of legitimacy, reigns as the unit of time, and "
            "natural omens recorded as fact beside worldly events. Notice "
            "how the archaic diction stays smooth and never turns academic."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": """STYLE: Babad / Hikayat
= Kronik keraton dalam prosa tertulis berdiksi arkais halus: silsilah, pergantian takhta, dan tanda alam dicatat setara oleh penyalin yang takzim.

STRUKTUR:
1. PEMBUKA PENYALIN -- Buka bak naskah yang disalin turun-temurun: "Tersebutlah perkataan...", "Alkisah maka tersebutlah...", "Adapun pada zaman itu...". Negeri dan raja yang bertakhta hadir di alinea pertama.
2. SILSILAH SEBAGAI FONDASI -- Tegakkan garis keturunan sebelum peristiwa: siapa berputra siapa, dari permaisuri yang mana. Legitimasi mengalir dari darah dan leluhur; urutkan rantainya dengan sabar.
3. KRONIK PER PEMERINTAHAN -- Susun peristiwa sebagai rangkaian zaman: penobatan, perkawinan, perang, wafat, penerus takhta. Ukur waktu dengan pemerintahan ("pada tahun ketujuh baginda bertakhta"), bukan tanggal kalender rapat.
4. PERISTIWA SEBAGAI PERTANDA -- Dahului tiap peristiwa besar dengan tanda: gunung bergetar, bintang berekor, mimpi raja, pusaka berpindah tangan. Catat tanpa membantah dan tanpa menjelaskan -- bagi penyalin, tanda dan kejadian itu satu anyaman.
5. PENUTUP TAKZIM -- Tutup dengan penyerahan penyalin: takhta berpindah, zaman berganti, dan riwayat diserahkan kembali ("demikianlah yang tersebut oleh empunya cerita") -- tanpa kesimpulan analitis.

SUARA: Orang ketiga, kala lampau, prosa tertulis yang tenang dan berjarak. Diksi arkais halus ditaburkan berkala (maka, syahdan, hatta, adapun, baginda, sang prabu) -- sebagai bumbu, bukan di tiap kalimat.

GERAKAN KHAS:
- Sebut tokoh besar dengan gelar lengkapnya saat pertama muncul; sesudahnya cukup gelar pendeknya.
- Perlakukan yang gaib setara dengan yang duniawi: wahyu, keris pusaka, pertapaan dicatat sebagai fakta kronik.
- Akui banyak lidah naskah saat riwayat bercabang: "ada yang mengatakan...", "menurut empunya cerita...".

DILARANG: Kosakata modern/gaul dan analisis kritis bergaya akademik -- penyalin mencatat, tidak membedah.
DILARANG: Ironi atau sinisme terhadap takhta -- kritik babad hanya menyelinap lewat pertanda dan nasib.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "scribe_formula_opening",
            "silsilah_chain",
            "omen_before_event",
            "reign_chronology",
        ],
        "banned_tells": [
            "babad tanah jawi",
            "hikayat hang tuah",
            "sulalatus salatin",
        ],
    },
},

"pewayangan_ki_dalang": {
    "display_name": "Pewayangan — Shadow-Play Master Narration",
    "aliases": [
        "pewayangan", "pewayangan ki dalang", "pewayangan_ki_dalang",
        "ki dalang", "dalang", "wayang", "wayang kulit", "janturan",
        "wayang narration", "shadow play", "shadow puppet",
    ],
    "is_fiction": True,
    "medium_origin": "ear",
    "tier": "P2",
    "tts_risk": False,
    "output_support": "both",
    "factual_regime": "fictional",
    "category": "E",
    "rag": {
        "query_instruction": (
            "Retrieve a wayang-style narration passage with a formal "
            "scene-setting overture, characters introduced by strings of "
            "titles, and a comic-relief interlude among servants:"
        ),
        "framing": (
            "Study how the dalang shifts register by slot: majestic "
            "scene-painting in the overture, short sung-verse interludes at "
            "scene changes, and earthy servant comedy in the gara-gara. "
            "Notice how noble titles are chanted like refrains."
        ),
        "min_quality": 3,
        "top_k": 3,
    },
    "style_rules_core": """STYLE: Pewayangan -- Ki Dalang
= Suara dalang di balik kelir: janturan megah, selingan bernuansa suluk, slot gara-gara jenaka, tokoh diperkenalkan dengan untaian gelar.

STRUKTUR:
1. JANTURAN PEMBUKA -- Buka dengan lukisan negeri dalam prosa ritmis yang megah: kebesaran kerajaan, raja di dampar kencana, hulubalang berbaris. Kalimat panjang berayun, penuh sanjungan tempat; konflik belum boleh masuk.
2. JEJER GELAR -- Perkenalkan tiap tokoh penting dengan untaian gelar dan julukan SEBELUM ia bicara ("Sang senapati agung, benteng gerbang timur, yang tak tergoyahkan hatinya..."). Ulangi gelar pendeknya tiap kali tokoh kembali.
3. LAKON BERGERAK -- Gerakkan konflik sebagai persoalan dharma: tugas berat dari raja, ancaman negeri seberang, pilihan ksatria antara kewajiban dan isi hatinya.
4. SELINGAN SULUK -- Di tiap pergantian adegan atau perubahan suasana, sisipkan 2-4 larik pendek bernuansa tembang (bukan prosa) yang melukiskan suasana batin: malam turun, hati yang bimbang, angin di pucuk beringin.
5. GARA-GARA -- Menjelang klimaks, patahkan ketegangan dengan slot jenaka para abdi: bahasa turun ke pasar, banyolan menyentil keseharian penonton (boleh anakronistis), lalu satu petuah polos sebelum kembali ke lakon.
6. TANCEB KAYON -- Tuntaskan lakon dengan keseimbangan pulih dan satu wejangan dharma singkat dari tokoh tua, lalu tutup kelir dalam satu-dua kalimat penutup dalang.

SUARA: Dalang yang hadir: orang ketiga, kala kini saat melukiskan adegan di kelir ("Tersebutlah sang prabu duduk di dampar kencana..."), sesekali menyapa penonton yang budiman. Register patuh pada slot: agung di janturan, tembang di suluk, pasar di gara-gara.

GERAKAN KHAS:
- Tandai pergantian beat dengan bunyi pergelaran: ketukan cempala, gemuruh kendang, gong yang menutup adegan.
- Kontraskan dua bahasa: halus-berbunga untuk raja dan para satria, lugas-jenaka untuk para abdi.
- Biarkan kelir ikut bermain: kayon bergetar, bayangan memanjang, api blencong meredup saat mara bahaya mendekat.

DILARANG: Lawakan bocor ke luar slot gara-gara -- di luar slot itu register tetap agung.
DILARANG: Menyalin tokoh, negeri, atau lakon kanon wayang -- ciptakan lakon BARU dengan pola pergelaran yang sama.
""",
    "style_rules_editor": "",
    "register_spec": {
        "required_moves": [
            "janturan_opening",
            "gelar_introductions",
            "suluk_interlude",
            "gara_gara_comic_slot",
        ],
        "banned_tells": [
            "pandawa lima",
            "gatotkaca",
            "semar gareng petruk bagong",
            "ngastina",
        ],
    },
},

    # === F ===

    "eli5": {
        "display_name": "ELI5 — Explain Like I'm Five",
        "aliases": [
            "eli5", "explain like i'm five", "explain like im five",
            "explain like i am five", "radical simplicity",
            "simple explainer", "one metaphor explainer",
        ],
        "is_fiction": False,
        "medium_origin": "page",
        "tier": "P2",
        "tts_risk": False,
        "output_support": "both",
        "factual_regime": "strict",
        "category": "F",
        "rag": {
            "query_instruction": (
                "Retrieve a passage that explains a technical or complex "
                "topic through one sustained everyday metaphor in plain language:"
            ),
            "framing": (
                "Study how ONE metaphor does all the work — every part of the "
                "topic mapped onto the same simple picture. Notice the total "
                "absence of jargon and how complications extend the metaphor "
                "rather than replace it."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": """STYLE: ELI5 -- Explain Like I'm Five
= Radical simplicity on the page: one honest question, one everyday metaphor carried all the way through, and not a single word of jargon left standing.

STRUCTURE PER CHAPTER:
1. PLAIN QUESTION OPENING -- Open with the topic restated as the naive question actually being asked ("Why does the bank pay you to keep money there?"). No preamble, no definitions.
2. PLANT THE ONE METAPHOR -- Within the first paragraph, commit to a single everyday metaphor (a lemonade stand, a bathtub, a school cafeteria) and say the mapping out loud: "Think of X as Y."
3. CARRY IT ALL THE WAY -- Explain every part of the topic as a part of the SAME metaphor. When a complication arrives, extend the picture (add a pipe to the bathtub) -- never switch to a second picture.
4. SNAP-BACK CLOSE -- End by answering the opening question in one plain sentence that works with or without the metaphor, then stop.

VOICE: Second person, friendly and unhurried; short declarative sentences; the register of a patient older sibling, never a lecturer. Assume zero prior knowledge and total intelligence.

SIGNATURE MOVES:
- Enforce ZERO JARGON: if a technical term is unavoidable, translate it in the same sentence -- "which is just a fancy word for...".
- Stress-test the metaphor at the topic's trickiest wrinkle and show the picture still holds -- that is the chapter's proof of work.
- Keep numbers tiny and countable (three apples, ten kids), never abstract magnitudes.
- Answer only the question asked; cut every fact, however interesting, that the metaphor does not need.

FORBIDDEN: A second metaphor. The moment the picture switches, the style is broken -- extend or simplify, never replace.
FORBIDDEN: Unexplained jargon, acronyms, or "as we all know" appeals to prior knowledge.
""",
        "style_rules_editor": "",
        "register_spec": {
            "required_moves": [
                "plain_question_open",
                "single_metaphor_spine",
                "zero_jargon",
            ],
            "banned_tells": [
                "explain like i'm five",
                "eli5",
            ],
        },
    },

    "socratic_dialogue": {
        "display_name": "Socratic Dialogue",
        "aliases": [
            "socratic", "socratic dialogue", "socratic_dialogue",
            "socratic method", "plato", "plato dialogue",
            "question and answer teaching", "two voice dialogue",
        ],
        "is_fiction": False,
        "medium_origin": "page",
        "tier": "P3",
        "tts_risk": True,
        "output_support": "both",
        "factual_regime": "strict",
        "category": "F",
        "rag": {
            "query_instruction": (
                "Retrieve a dialogue where a questioner leads a companion "
                "step by step toward an insight through questions and "
                "counterexamples:"
            ),
            "framing": (
                "Study how the answer is EARNED — definitions offered, broken "
                "by counterexample, and rebuilt from the companion's own "
                "admissions. Notice that the questioner asserts nothing and the "
                "strongest objections are voiced before the reader can raise them."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": """STYLE: Socratic Dialogue
= Two voices reasoning on the page -- a questioner who only asks, a companion who discovers. The answer is earned through objections, never announced.

STRUCTURE PER CHAPTER:
1. INNOCENT OPENING QUESTION -- The questioner asks something that sounds settled ("So we all agree on what fairness is?"). The companion answers confidently and concretely.
2. THE FIRST CRACK -- The questioner offers one counterexample from ordinary life; the confident answer fails in the companion's own mouth: "Then it seems I spoke too quickly."
3. OBJECTION LADDER -- Rebuild the answer in rounds. Each round, give the companion the objection the reader was about to make -- the strongest version of it -- then test it and keep only what survives.
4. EARNED ANSWER CLOSE -- The companion, not the questioner, states the final answer, assembled from the admissions made along the way; the questioner closes by naming what remains unresolved.

VOICE: Two named speakers in tagged dialogue, plain present-tense exchanges; the questioner curious and relentless but never smug; the companion intelligent and proud, never a strawman.

SIGNATURE MOVES:
- Let the questioner ASK ONLY -- every claim the argument needs must be extracted as the companion's answer, never asserted outright.
- Anticipate the reader: voice the smartest available objection at each step, and treat its defeat as shared progress, not a gotcha.
- Force definitions early and revise them on the record ("Then shall we amend it to...?").
- Test with small concrete cases (a borrowed knife, a queue, a shared bill) -- never abstraction against abstraction.

FORBIDDEN: The questioner lecturing or delivering the conclusion -- if the companion could not have said it, it has not been earned.
FORBIDDEN: A companion who only agrees ("Yes, certainly, it must be so" on repeat) -- every concession must be argued into.
""",
        "style_rules_editor": "",
        "register_spec": {
            "required_moves": [
                "question_led_exchange",
                "objection_anticipated",
                "earned_answer",
            ],
            "banned_tells": [
                "the unexamined life is not worth living",
                "i know that i know nothing",
                "know thyself",
            ],
        },
    },

    "commencement_wisdom": {
        "display_name": "Commencement Wisdom",
        "aliases": [
            "commencement", "commencement wisdom", "commencement_wisdom",
            "commencement speech", "graduation speech", "wear sunscreen",
            "sunscreen speech", "life advice speech",
        ],
        "is_fiction": False,
        "medium_origin": "ear",
        "tier": "P2",
        "tts_risk": False,
        "output_support": "both",
        "factual_regime": "strict",
        "category": "F",
        "rag": {
            "query_instruction": (
                "Retrieve a passage of direct life advice addressed to "
                "someone at a turning point, mixing humor with sincerity:"
            ),
            "framing": (
                "Study the WRY-WARM alternation — jokes that undercut "
                "earnestness, then one line that means it. Notice how each "
                "piece of advice is paid for with a personal story or an "
                "admitted failure."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": _P23_SPEECH_BASE + """
COMMENCEMENT VARIANT (threshold-address register):
- OCCASION OPENING -- Name the threshold in the first lines -- graduation, a first job, a departure -- then puncture the ceremony with one wry admission ("Nobody remembers these speeches. I checked.").
- ADVICE PAID FOR BY STORY -- Deliver life advice as short beats, each one earned with a small story or confession from the speaker's own missteps -- never advice floating free of a scar.
- WRY-THEN-WARM ALTERNATION -- Undercut every earnest line with dry humor, and every joke with one sentence that means it; the listener must never be sure which is coming next.
- HOLD BOTH TRUTHS -- Speak as someone who knows the advice may not be taken and gives it anyway; certainty is banned, affection is not.
- THRESHOLD CLOSE -- End facing the door, not the podium: one plain charge that sends "you" through it, warm and unadorned -- no soaring finale.
VOICE: First-person elder addressing "you at the edge of something"; conversational, amused, unhurried; sentences plain enough to survive being remembered wrong.
FORBIDDEN: Platitudes without a price tag -- any advice ("follow your passion") not paid for by a specific story is cut.
FORBIDDEN: Grand oratorical crescendo -- this register lands soft; the biggest line is the quietest one.
""",
        "style_rules_editor": "",
        "register_spec": {
            "required_moves": [
                "threshold_direct_address",
                "advice_paid_by_story",
                "wry_warm_alternation",
            ],
            "banned_tells": [
                "wear sunscreen",
                "stay hungry, stay foolish",
                "dance like nobody's watching",
            ],
        },
    },

    "oratory_anaphora": {
        "display_name": "Oratory — Anaphora",
        "aliases": [
            "oratory", "anaphora", "oratory anaphora", "oratory_anaphora",
            "great speech", "repetition ladder", "speech register",
            "rhetorical speech",
        ],
        "is_fiction": False,
        "medium_origin": "ear",
        "tier": "P2",
        "tts_risk": False,
        "output_support": "both",
        "factual_regime": "strict",
        "category": "F",
        "rag": {
            "query_instruction": (
                "Retrieve a speech or oratorical passage built on repeated "
                "opening phrases and rhythmic cadence for live delivery:"
            ),
            "framing": (
                "Study the repetition LADDERS — the same opening phrase "
                "repeated with escalating content — and the refrain that "
                "returns transformed. Notice how rhythm and call-and-response "
                "do the persuading, not argument alone."
            ),
            "min_quality": 3,
            "top_k": 3,
        },
        "style_rules_core": _P23_SPEECH_BASE + """
ANAPHORA VARIANT (repetition-ladder register):
- LADDER STRUCTURE -- Build the piece as three or four repetition ladders: runs of 3-5 consecutive sentences opening with the SAME phrase, each rung escalating in scale or stakes ("I have seen a village... I have seen a city... I have seen a nation...").
- DECLARE THE REFRAIN -- Plant one short refrain early, return to it between ladders like a bell, and let the final return transform it: same words, new meaning.
- CALL-AND-RESPONSE CADENCE -- Write lines a crowd could answer: pose the question, leave the beat, then answer it with the phrase the crowd already knows is coming.
- SHORT-SHORT-LONG RHYTHM -- Alternate hammer-blow short sentences with one long rolling sentence that spends the built pressure; the paragraph's rhythm IS the argument.
- CREST CLOSE -- End on the highest rung of the final ladder: the refrain, transformed, as the last words spoken.
VOICE: First-person plural "we" binding speaker and crowd, present tense, elevated but concrete -- every abstraction must stand on named places, faces, and dates.
FORBIDDEN: Repetition without escalation -- a ladder whose rungs do not climb is a stutter; each repeat must raise scale, stakes, or intimacy.
FORBIDDEN: Borrowed famous-speech lines -- the ladders must be original; the form is inherited, the words are not.
""",
        "style_rules_editor": "",
        "register_spec": {
            "required_moves": [
                "anaphora_ladder",
                "returning_refrain",
                "call_and_response",
            ],
            "banned_tells": [
                "i have a dream",
                "ask not what your country can do for you",
                "we shall fight on the beaches",
                "yes we can",
            ],
        },
    },
}

__all__ = ["P23_STYLES"]
