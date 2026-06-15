# pakem — old → new style mapping

The pakem (`python/pakem/`) is the single source of truth for narration styles.
It consolidates style rules + language + RAG config that previously lived
(duplicated) in four places. This table maps the old keys to the new canonical
registry keys so callers can migrate.

## Sources consolidated

| Old location | What lived there |
|---|---|
| `python/laozhang_api.py` `STYLE_RULES` (2849–3911) | 12 style rule blocks |
| `python/laozhang_api.py` `get_style_rules()` (4016) | substring style match |
| `python/laozhang_api.py` `_RAG_STYLE_LEGACY` / `_rag_style()` (669–693) | rag alias map |
| `python/laozhang_api.py` `_NARASI_LANG_NAMES` / `_resolve_narasi_lang()` (4126–4142) | language table |
| `python/laozhang_api.py` `VIDEO_SCRIPT_MODIFIER` / `get_generation_preamble()` (3913–4045) | VO assets |
| `backend/server.js` `NARASI_STYLE_RULES_JS` / `_getStyleRulesJS()` (790–982) | JS style rules (14 keys, adds `pov`, `national geographic`) |
| `backend/server.js` `_NARASI_LANG_NAMES` / `resolveLang()` (48–61) | JS language table (identical) |
| `data/moat/gutenberg/style_rag_config.py` `STYLE_RAG_CONFIG` / `_ALIASES` | per-style RAG query_instruction, framing, min_quality, top_k |
| `data/moat/gutenberg/rag_narration.py` FACTUAL INTEGRITY block (550–567) | factual integrity (canonical copy) |

## Style key mapping

| Old / input key(s) | → Canonical pakem key | is_fiction |
|---|---|---|
| `creative non-fiction`, `creative nonfiction`, `creative_nonfiction` | `creative_nonfiction` *(DEFAULT)* | no |
| `storytelling`, `narrative drama`, `epic` | `storytelling` | no |
| `bedtime story`, `bedtime`, `bedtime_story` | `bedtime_story` | no |
| `harari`, `big_history`, `big history`, `diamond`, `jared diamond` | `harari` | no |
| `pov`, `pov_first_person`, `first person`, `biography`, `biographical` | `pov` | no |
| `national geographic`, `natgeo`, `national_geographic`, `documentary`, `discovery` | `natgeo` | no |
| `youtube`, `youtube_popular_science`, `popular_science`, `science` | `youtube` | no |
| `journalistic`, `long_form`, `long form`, `reportage` | `journalistic` | no |
| `literary essay`, `literary_essay`, `essay`, `philosophical`, `reflective` | `literary_essay` | no |
| `podcast narrative`, `podcast_narrative`, `podcast` | `podcast_narrative` | no |
| `academic popular`, `academic_popular`, `sapiens`, `expository`, `finance`, `economics`, `business` | `academic_popular` | no |
| `cinematic voiceover`, `cinematic_voiceover`, `cinematic`, `voiceover` | `cinematic_voiceover` | no |
| `narrative non-fiction`, `narrative_nonfiction`, `narrative_nonfiction_mystery`, `investigative`, `mystery`, `suspense`, `cinematic history` | `narrative_nonfiction` | no |
| `fiction`, `story`, `novel`, `horror`, `fairy_tale`, `dongeng`, `drama`, `thriller`, `fantasy`, `scifi`, `sci-fi`, `anime`, `noir`, `comedy` | `fiction` | **yes** |

Unknown / empty input → `creative_nonfiction` (the default), matching the old
`get_style_rules()` fallback.

## Resolution semantics (replaces substring match + 3 alias maps)

`pakem.resolve_style_key(input)`:
1. exact normalized alias/key match
2. substring match (longest matching alias wins — preserves the old
   `get_style_rules` behaviour where `"creative non-fiction documentary"`
   matched `"creative non-fiction"`; the longest-alias rule prevents `pov`
   from eating `popular science`)
3. fall back to `creative_nonfiction`

## CORE vs EDITOR rules

- `style_rules_core` — load-bearing rules **shipped at generation**. Returned by
  `build_style_block(style, video_mode)` (the pakem replacement for
  `get_style_rules`). Appends `VIDEO_MODIFIER` when `video_mode=True`.
- `style_rules_editor` — long-form rules used **only in a separate editor pass**.
  Returned by `get_editor_block(style)`. **Never shipped at generation.**
  - `harari`: core = PART A (generation prompt); editor = PART B (editor pass).
    The original `STYLE_RULES["harari"]` shipped BOTH at generation, which the
    source itself warns "creates self-conscious prose." pakem fixes this.
  - `narrative_nonfiction`: core = compact shippable engine; editor = the full
    ~300-line cinematic-history craft reference.

## Shared assets (defined ONCE in `pakem/assets.py`)

`FACTUAL_INTEGRITY`, `CRAFT_RULES`, `LANGUAGE_DIRECTIVE(lang_label)`,
`GENERATION_PREAMBLE(video_mode)`, `VIDEO_MODIFIER`. Ship `FACTUAL_INTEGRITY`
only for non-fiction styles (`is_fiction == False`).

## Language mapping

`pakem.resolve_language(code)` uses the single `LANGUAGE_NAMES` table (20
languages, identical to both old copies). `id` → `Bahasa Indonesia`; unknown
label passes through trimmed; empty → `Bahasa Indonesia`.
