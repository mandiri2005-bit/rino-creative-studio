// video/visualPrompt/validate.mjs — validator for LLM-generated non-WB visual prompt
// output.  Mirrors the shape and design of whiteboard/plan/validate.mjs:
//   • returns { ok, errors, warnings } — never throws, pure (unit-testable)
//   • FATAL errors (ok=false) → caller must fall back to build_visual_prompt regex
//   • warnings → log only, render with the prompt anyway
//
// THE CHASTELEIN BUG (what this validator prevents)
// ─────────────────────────────────────────────────
// When an LLM generates a visual_prompt for a non-WB scene it sometimes injects
// character names from a _different_ scene's brief (e.g. "Chastelein" appearing in
// a scene whose narration never mentions him).  The prompt then goes to an image
// model which dutifully renders that character, breaking continuity.  Rule:
//   every proper noun in visual_prompt MUST also appear in narration_text
//   (cross-checked via extractProperNouns on both sides).
//
// JSON shape validated:
//   { visual_prompt: string (200-600 chars)  ← REQUIRED
//     characters:    string[]                ← optional
//     setting:       string                  ← optional
//     mood:          string                  ← optional }
//
// Fallback (exported):
//   If validation fails the caller should invoke buildVisualPromptFallback(scene)
//   which delegates to the Python-side build_visual_prompt regex path (via the
//   visualPrompt field already on the scene object — produced by /video/segment).

// ── constants (tunable via env, no hard re-deploy needed) ──────────────────────
const MIN_CHARS = Number(process.env.VP_MIN_CHARS)  || 200;
const MAX_CHARS = Number(process.env.VP_MAX_CHARS)  || 600;

// Titles / honorifics that aren't character names (don't trip the cross-check).
const TITLE_WORDS = new Set([
  "mr", "mrs", "ms", "dr", "prof", "rev", "lord", "lady", "sir", "dame",
  "van", "de", "den", "von", "bin", "binti", "ibn", "st",
]);

// Common English/Indonesian vocabulary that legitimately appears capitalised at the
// start of a sentence or as adjectives / common nouns but is NOT a proper noun.
// Goal: reduce false positives in the Chastelein cross-check without building a
// full language model.  Add words here when you observe them triggering false
// fallbacks in production (console.warn "[visualPrompt] LLM output invalid").
const COMMON_VOCAB = new Set([
  // English determiners / prepositions / conjunctions
  "the", "a", "an", "in", "on", "at", "by", "for", "with", "as", "and",
  "but", "or", "nor", "yet", "so", "its", "his", "her", "their", "our",
  // English cinematography / scene-description adjectives and nouns that often
  // appear capitalised (start of prompt sentence or after colon)
  "wide", "close", "long", "medium", "extreme", "overhead", "aerial",
  "slow", "fast", "steady", "handheld", "static",
  "warm", "cool", "bright", "dark", "soft", "harsh", "golden", "silver",
  "amber", "blue", "green", "red", "white", "black", "grey", "gray",
  "misty", "foggy", "dusty", "hazy", "lush", "dense", "arid",
  "cinematic", "dramatic", "sweeping", "intimate", "atmospheric",
  "establishing", "tracking", "dolly", "crane", "tilt", "pan",
  // English common nouns that appear sentence-initial in prompts
  "fishing", "farming", "trading", "walking", "running", "standing", "sitting",
  "morning", "afternoon", "evening", "night", "dawn", "dusk", "sunset", "sunrise",
  "market", "village", "city", "town", "river", "forest", "mountain", "field",
  "birds", "trees", "clouds", "waves", "light", "shadow", "mist", "smoke",
  "people", "man", "woman", "child", "figure", "vendor", "soldier", "worker",
  "street", "road", "path", "building", "house", "temple", "palace", "bridge",
  "boats", "ships", "water", "sky", "earth", "land", "sea", "ocean", "hill",
  "smoke", "fire", "rain", "sun", "moon", "stars", "wind", "dust", "sand",
  "two", "three", "four", "five", "several", "many", "few", "some", "all",
  // Indonesian stopwords / connectives that occasionally appear capitalised
  "yang", "dan", "di", "ke", "dari", "itu", "ini", "para", "pada",
  "untuk", "dengan", "adalah", "sebuah", "seorang", "akan", "tidak",
  "juga", "atau", "mereka", "kita", "kami", "ada", "setelah", "banyak",
  "terlihat", "tampak", "terbang", "bergerak", "berlari", "berdiri",
  "cahaya", "bayangan", "langit", "tanah", "hutan", "gunung", "sungai",
]);

// ── helpers ───────────────────────────────────────────────────────────────────

/**
 * Extract proper nouns from text: words that start with an upper-case letter,
 * are ≥ 3 chars, and are not honorifics or common vocabulary.
 *
 * Returns a Set<string> of lowercased tokens for case-insensitive comparison.
 *
 * Design intent — avoid FALSE POSITIVES more than false negatives:
 *   • A missed stray name yields a suboptimal prompt (acceptable).
 *   • A false positive triggers an unnecessary fallback to the regex path
 *     (wastes the LLM output; we want to avoid this).
 * The COMMON_VOCAB list is the primary lever; extend it as needed in prod.
 */
export function extractProperNouns(text) {
  if (!text || typeof text !== "string") return new Set();
  const tokens = text.match(/\p{Lu}\p{L}+/gu) || [];
  const out = new Set();
  for (const t of tokens) {
    const lc = t.toLowerCase();
    if (lc.length >= 3 && !TITLE_WORDS.has(lc) && !COMMON_VOCAB.has(lc)) {
      out.add(lc);
    }
  }
  return out;
}

/**
 * Given the set of proper nouns in visual_prompt and the set allowed by
 * narration_text + setting (the "universe"), return an array of nouns that
 * appear in the prompt but are absent from the universe.
 */
function strayNouns(promptNouns, universeNouns) {
  const strays = [];
  for (const n of promptNouns) {
    if (!universeNouns.has(n)) strays.push(n);
  }
  return strays;
}

// ── main export ───────────────────────────────────────────────────────────────

/**
 * Validate an LLM-generated visual prompt output object.
 *
 * @param {object}  output        — the raw LLM JSON (may be null/undefined)
 * @param {object}  scene         — the scene it was generated for; must include
 *                                  `narration_text` (string).
 * @param {object}  [opts]
 * @param {string}  [opts.sceneId]  — for logging context only
 * @returns {{ ok: boolean, errors: string[], warnings: string[] }}
 *
 * ok=false  → caller MUST fall back to build_visual_prompt regex.
 * ok=true   → output is safe to use; check warnings for monitoring.
 */
export function validateVisualPromptOutput(output, scene, { sceneId = "?" } = {}) {
  const errors   = [];   // FATAL  → caller falls back to regex
  const warnings = [];   // NON-FATAL → log only

  const narration = (typeof scene?.narration_text === "string") ? scene.narration_text : "";

  // ── 1. shape guard ────────────────────────────────────────────────────────
  if (!output || typeof output !== "object" || Array.isArray(output)) {
    errors.push("output is not an object");
    return { ok: false, errors, warnings };
  }

  // ── 2. visual_prompt: required, string, length in [MIN_CHARS, MAX_CHARS] ─
  const vp = output.visual_prompt;
  if (typeof vp !== "string" || !vp.trim()) {
    errors.push("visual_prompt is missing or empty");
  } else {
    const len = vp.trim().length;
    if (len < MIN_CHARS) {
      errors.push(`visual_prompt too short (${len} chars, min ${MIN_CHARS}) — lazy generation`);
    }
    if (len > MAX_CHARS) {
      // Over-long is non-fatal: image models truncate anyway; the extra is just wasted tokens.
      warnings.push(`visual_prompt too long (${len} chars, max ${MAX_CHARS}) — trimmed by image model`);
    }
  }

  // ── 3. Chastelein check ───────────────────────────────────────────────────
  // Proper nouns in visual_prompt must be a subset of proper nouns in narration_text
  // PLUS the optional setting field (setting names like "Batavia", "Nusantara" are fine).
  if (typeof vp === "string" && vp.trim()) {
    const promptNouns  = extractProperNouns(vp);
    const narratNouns  = extractProperNouns(narration);
    const settingNouns = extractProperNouns(typeof output.setting === "string" ? output.setting : "");
    // also allow any noun that appears literally in the narration (catches mixed-case)
    const narratLower  = narration.toLowerCase();
    const universe     = new Set([...narratNouns, ...settingNouns]);
    // keep only strays that are truly absent from the narration as a substring too
    const strays = strayNouns(promptNouns, universe).filter((n) => !narratLower.includes(n));
    if (strays.length) {
      errors.push(
        `visual_prompt contains proper noun(s) not in narration_text: ${strays.join(", ")} ` +
        `(Chastelein bug — name injected from brief, not this scene)`
      );
    }
  }

  // ── 4. characters array ───────────────────────────────────────────────────
  if (output.characters !== undefined) {
    if (!Array.isArray(output.characters)) {
      warnings.push("characters is not an array — ignored");
    } else {
      const narratLower = narration.toLowerCase();
      for (const entry of output.characters) {
        if (typeof entry !== "string") {
          warnings.push(`characters: non-string entry ${JSON.stringify(entry)} — skipped`);
          continue;
        }
        // Each declared character must actually appear in the narration text
        // (case-insensitive substring match; keeps the rule simple and robust).
        if (entry.trim() && !narratLower.includes(entry.trim().toLowerCase())) {
          errors.push(
            `characters entry "${entry}" not found in narration_text — ` +
            `character from brief injected into wrong scene`
          );
        }
      }
    }
  }

  // ── 5. optional string fields (setting, mood) ─────────────────────────────
  if (output.setting !== undefined && typeof output.setting !== "string") {
    warnings.push(`setting is not a string (got ${typeof output.setting}) — ignored`);
  }
  if (output.mood !== undefined && typeof output.mood !== "string") {
    warnings.push(`mood is not a string (got ${typeof output.mood}) — ignored`);
  }

  // ── 6. unknown extra keys ─────────────────────────────────────────────────
  const KNOWN = new Set(["visual_prompt", "characters", "setting", "mood"]);
  const extra = Object.keys(output).filter((k) => !KNOWN.has(k));
  if (extra.length) {
    warnings.push(`unknown fields in output: ${extra.join(", ")} — ignored`);
  }

  return { ok: errors.length === 0, errors, warnings };
}

// ── fallback ──────────────────────────────────────────────────────────────────

/**
 * Fallback: return the scene's existing visual_prompt string (set by Python's
 * build_visual_prompt regex path during /video/segment).  Call this when
 * validateVisualPromptOutput returns ok=false.
 *
 * Emits a console.warn so the caller's logs show WHY a fallback was used
 * without requiring the caller to repeat the error list.
 *
 * @param {object} scene        — scene object with .visualPrompt or .visual_prompt
 * @param {string[]} errors     — error list from validateVisualPromptOutput
 * @param {string}   [sceneId]
 * @returns {string}  the fallback prompt (may be empty if segment data is missing)
 */
export function buildVisualPromptFallback(scene, errors, sceneId = "?") {
  const fallback = scene?.visualPrompt || scene?.visual_prompt || "";
  console.warn(
    `[visualPrompt ${sceneId}] LLM output invalid → regex fallback. ` +
    `Errors: ${(errors || []).join("; ")}`
  );
  return fallback;
}
