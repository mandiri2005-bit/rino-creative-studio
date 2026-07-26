"""B-04a: Review/One-Shot language contract and localized report-token tables -- pure module.

Separates two concepts the Review and One-Shot endpoints previously conflated:

  - manuscript_language: authority is exclusively the existing B-07 lineage resolution
    (_narasi_resolve_lineage / _narasi_lineage_language in laozhang_api.py). This module
    never derives, infers, or overrides it -- it only renders an already-resolved value
    into deterministic prompt text.
  - report_language: a new, separate, exact `id`/`en` value controlling only report/
    checklist prose, report section labels, and bounded Review/Auto-Fix UI labels.

This module never reads os.environ, never imports FastAPI/db/network/logging, and exposes
no test-only seam or injectable global. laozhang_api.py owns environment-flag parsing
(the existing `_flag_on` helper) and HTTP 422 translation of the `ReviewContractError`
codes raised here. Every public function performs exact-type-first validation before any
equality, membership, iteration, string conversion, hashing, or attacker-controlled dunder
method touches a caller-supplied value.

Rework 9 (Codex supersession of Rework 8): PART_TWO_MARKER below is a fixed GLOBAL
machine delimiter, never selected or derived from report_language, manuscript_language,
UI locale, persona prose, or provider output -- Wimba is a global multilingual product,
and a structural protocol byte sequence must be identical for every language. Rework 8's
id/en marker MAP ("PER BAB" vs "PER CHAPTER") is the superseded design; do not reintroduce
it. Localized REPORT prose/labels (report_tokens below) are unrelated and unaffected --
only the machine delimiter itself is language-neutral.
"""
from types import MappingProxyType

REVIEW_CONTRACT_VERSION = "b04a.review-language-chapter-key.v1"

SUPPORTED_REPORT_LANGUAGES = ("id", "en")

# ASCII horizontal/vertical whitespace only -- deliberately narrower than str.strip()'s
# Unicode-aware default, so a non-ASCII whitespace character (e.g. U+00A0 NBSP) is treated
# as part of the token rather than silently trimmed away.
_ASCII_WHITESPACE = " \t\n\r\x0b\x0c"

_REPORT_TOKENS = MappingProxyType({
    "id": MappingProxyType({
        "chapter_label": "Bab",
        "score": "Skor",
        "key_weaknesses": "Kelemahan Utama",
        "recommendations": "Rekomendasi",
        "no_significant_issues": "Tidak ada kelemahan signifikan.",
        "final_verdict": "Verdict Akhir",
    }),
    "en": MappingProxyType({
        "chapter_label": "Chapter",
        "score": "Score",
        "key_weaknesses": "Key Weaknesses",
        "recommendations": "Recommendations",
        "no_significant_issues": "No significant weaknesses.",
        "final_verdict": "Final Verdict",
    }),
})


# Rework 9 (Codex supersession): ONE fixed, global, language-NEUTRAL machine delimiter --
# never a per-language mapping, never looked up by report_language/manuscript_language.
# The frontend (narasiReviewContract.mjs) exports the byte-identical constant; a pure
# cross-runtime test asserts the two never drift. A plain module-level constant (not a
# function taking a language argument) makes language-dependent selection structurally
# impossible, not merely undesirable.
PART_TWO_MARKER = "---WIMBA_REVIEW_CHAPTERS_START:v1---"


class ReviewContractError(Exception):
    """Typed, stable-code error. `.code` is one of a small closed vocabulary and never
    embeds the raw offending value (so a caller can safely surface `.code` to a client)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def report_tokens(report_language):
    """Return the immutable localized-token mapping for an already-validated report_language."""
    if type(report_language) is not str or report_language not in _REPORT_TOKENS:
        raise ReviewContractError("REPORT_LANGUAGE_UNSUPPORTED")
    return _REPORT_TOKENS[report_language]


def normalize_report_language(raw):
    """Normalize a caller-supplied report_language value to exact 'id' or 'en'.

    None, or a string that is blank after trimming ASCII whitespace, means "unspecified"
    and defaults to 'id'. Any non-str type (bool, int, float, bytes, list, tuple, set,
    mapping, or a str subclass/duck type) is rejected by its exact type before any
    equality, membership, or lowering touches it -- so a hostile __eq__/__str__/__hash__
    on a non-str object is never invoked.
    """
    if raw is None:
        return "id"
    if type(raw) is not str:
        raise ReviewContractError("REPORT_LANGUAGE_TYPE_INVALID")
    trimmed = raw.strip(_ASCII_WHITESPACE)
    if trimmed == "":
        return "id"
    lowered = trimmed.lower()
    if lowered not in SUPPORTED_REPORT_LANGUAGES:
        raise ReviewContractError("REPORT_LANGUAGE_UNSUPPORTED")
    return lowered


def _coerce_manuscript_language_display(manuscript_language):
    """manuscript_language is already-resolved B-07 authority (or None for an unresolved
    standalone request). Never validates or re-derives it -- only renders it as display
    text, defaulting to a bounded literal when absent."""
    if manuscript_language is None:
        return "unspecified"
    if type(manuscript_language) is not str:
        raise ReviewContractError("MANUSCRIPT_LANGUAGE_TYPE_INVALID")
    return manuscript_language if manuscript_language else "unspecified"


def build_review_language_block(manuscript_language, report_language):
    """Deterministic final language-contract block appended to the Review persona system
    prompt. Explicitly supersedes only earlier output-language/localized-label/chapter-
    label instructions; never touches editorial content rules.

    Rework 9 (Codex supersession of Rework 8): the Part-2 structural boundary marker is
    the fixed, language-NEUTRAL PART_TWO_MARKER constant -- identical regardless of
    REPORT_LANGUAGE or MANUSCRIPT_LANGUAGE. It is appended here as an invariant machine
    delimiter instruction, distinct from the localized report tokens below."""
    tokens = report_tokens(report_language)
    manuscript_display = _coerce_manuscript_language_display(manuscript_language)
    lines = (
        "",
        "---",
        "FINAL LANGUAGE CONTRACT (supersedes any earlier output-language, localized-label, "
        "or chapter-label instruction above):",
        "MANUSCRIPT_LANGUAGE: " + manuscript_display,
        "REPORT_LANGUAGE: " + report_language,
        "Report prose and report section labels MUST use REPORT_LANGUAGE.",
        "Quoted manuscript evidence MUST remain byte-for-byte in MANUSCRIPT_LANGUAGE.",
        "Proposed replacement prose MUST remain in MANUSCRIPT_LANGUAGE.",
        "The original chapter heading label and title MUST be preserved exactly as given; "
        "do not translate or rename it.",
        "Use ONLY these localized report tokens (do not invent alternates):",
        "- chapter label: \"" + tokens["chapter_label"] + "\"",
        "- score: \"" + tokens["score"] + "\"",
        "- key weaknesses: \"" + tokens["key_weaknesses"] + "\"",
        "- recommendations: \"" + tokens["recommendations"] + "\"",
        "- no significant issues: \"" + tokens["no_significant_issues"] + "\"",
        "- final verdict: \"" + tokens["final_verdict"] + "\"",
        "The line below is a FIXED, LANGUAGE-NEUTRAL MACHINE DELIMITER -- not manuscript "
        "prose, not a translatable label, and never selected by REPORT_LANGUAGE or "
        "MANUSCRIPT_LANGUAGE. It MUST appear verbatim, character-for-character, on its "
        "own line, exactly once, immediately before the first per-chapter section, "
        "identical in every language: do not translate, localize, paraphrase, prefix, "
        "suffix, duplicate, or emit it inline with prose. It overrides any differently-"
        "worded Part 2 boundary marker instruction given earlier in this prompt:",
        PART_TWO_MARKER,
        "Machine JSON keys and internal delimiters remain stable English identifiers and "
        "must not be translated.",
        "---",
    )
    return "\n".join(lines)


def build_oneshot_language_instruction(manuscript_language, report_language):
    """Deterministic localized instruction block for the non-VO One-Shot Fix path. Keeps
    ---FIXED_BOOK_START---/---FIXED_BOOK_END--- and original chapter headings language-
    neutral/byte-exact regardless of report_language."""
    tokens = report_tokens(report_language)
    manuscript_display = _coerce_manuscript_language_display(manuscript_language)
    lines = (
        "",
        "LANGUAGE CONTRACT:",
        "- Scan/checklist prose must use REPORT_LANGUAGE = " + report_language + ".",
        "- The fixed manuscript body must remain in MANUSCRIPT_LANGUAGE = "
        + manuscript_display + ".",
        "- Preserve every original Markdown chapter heading byte-for-byte; never rename "
        "\"Chapter N\" to \"Bab N\" or the reverse.",
        "- Never translate manuscript prose merely because the report language differs.",
        "- Keep ---FIXED_BOOK_START--- and ---FIXED_BOOK_END--- exactly as written; these "
        "delimiters are language-neutral and must not be localized.",
        "- Checklist headings may localize using: chapter label \""
        + tokens["chapter_label"] + "\", score \"" + tokens["score"]
        + "\", key weaknesses \"" + tokens["key_weaknesses"] + "\", recommendations \""
        + tokens["recommendations"] + "\", no significant issues \""
        + tokens["no_significant_issues"] + "\", final verdict \""
        + tokens["final_verdict"] + "\" -- but the parser delimiters above may not localize.",
        "",
    )
    return "\n".join(lines)


def response_capability_fields(report_language):
    """Immutable {report_language, review_contract_version} mapping to merge into a
    response/result/metadata dict only when the backend flag is on. Never includes prose
    or any field beyond these two."""
    if type(report_language) is not str or report_language not in _REPORT_TOKENS:
        raise ReviewContractError("REPORT_LANGUAGE_UNSUPPORTED")
    return MappingProxyType({
        "report_language": report_language,
        "review_contract_version": REVIEW_CONTRACT_VERSION,
    })
