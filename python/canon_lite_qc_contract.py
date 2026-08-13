"""Canon Lite QC provider CONTRACT — names and the ratified route, nothing else.

🔴 WHY THIS MODULE EXISTS. The activation gate must be able to answer "is the QC provider
   credential present, and is the route still the ratified one" WITHOUT importing the
   provider. Refusing to build the provider is the entire point of that gate, so a gate that
   imports `canon_lite_qc_provider` to read its constants has already done the thing it is
   supposed to prevent.

   The first attempt duplicated the strings in the meter module and leaned on a drift test to
   notice divergence. That is backwards: it makes correctness depend on a test being written,
   kept, and run, when it can instead be structural. Both the meter and the provider import
   from here, so there is exactly ONE definition and drift is impossible rather than merely
   detectable.

⚠️ HARD CONSTRAINTS on this module — it is imported by a gate that may run when Canon Lite is
   OFF, so it must stay inert:
     · no OpenAI/provider SDK import
     · no client construction
     · no secret READS (it names the variable; it never resolves its value)
     · no I/O of any kind, no network, no filesystem, no environment access
   Names are not secrets. Keep it to literals.
"""

# The dedicated QC credential. NOT interchangeable with the Vertex credentials that serve
# chapters: the incident on 2026-08-11 had working Vertex OAuth and still could not run a
# metered wave, because this variable was absent on narration-worker.
QC_API_KEY_ENV = "CANON_LITE_QC_PROVIDER_API_KEY"

# Optional override. When set it must MATCH the ratified route exactly — a silently
# repointed QC endpoint bills somewhere else under guarantees ratified for this one.
QC_BASE_URL_ENV = "CANON_LITE_QC_PROVIDER_BASE_URL"

# The ratified route: Google's Gemini OpenAI-compatible endpoint.
QC_PROVIDER_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

# The ratified upstream model for QC extraction.
#
# 🔴 RAISED FROM `gemini-2.5-flash-lite` 2026-08-13, AFTER TWO CANARIES MEASURED NOTHING.
#    The QC evidence contract asks for something narrow and unforgiving: a `quote` copied
#    character-for-character from `chapter_text`, the bare name or literal ALONE for
#    entity/literal claims, never widened to disambiguate — plus, when that quote repeats,
#    a `context` that is itself verbatim, occurs exactly once, and contains the quote
#    exactly once. Two nested exact strings, both byte-perfect.
#
#    Flash-Lite did not satisfy it in two canaries (`4wq7ntq3`, `gjpmhjzs`), each leaving
#    2 of 3 chapters unmeasured on `quote_not_found`.
#
#    ⚠️ THAT IS NOT A PROVEN CAPABILITY VERDICT, AND AN EARLIER DRAFT OF THIS COMMENT
#       CLAIMED IT WAS. `attempt` was not serialized at the time, so attempts 2 and 3
#       carried identical bytes at temperature 0.0 — the model was asked TWO distinct
#       questions, not three, and the third answer could not have differed. Failed
#       output is also deliberately not retained, so which part missed — quote,
#       context, escaping, or one claim type — was never observed.
#
#    This change is therefore a CONTROLLED EXPERIMENT, not a fix for a diagnosed cause.
#    It is worth running because Google positions Flash-Lite for cheap simple extraction
#    while this contract is neither, and because `flash` already satisfies an equally
#    strict structured contract on the Story Bible path (2/2 candidates, both canaries).
#    A larger model is the next lever, NOT the first — and only after three genuinely
#    distinct requests have failed.
#
# ⚠️ `QC_PRICING` MOVES WITH THIS LINE, ALWAYS. The rate record is frozen with its own
#    `pricing_version` precisely so a model change cannot land without one; a model billed
#    at another model's rate is a silent accounting fault, not a rounding error.
QC_MODEL_UPSTREAM = "gemini-2.5-flash"

# Closed adapter-failure vocabulary. This lives in the inert contract module so the
# provider can validate what it raises and the provider-agnostic extractor can preserve
# that code without importing (and therefore constructing) the provider module on a host
# that is not permitted to meter. No response text or exception message is ever a code.
QC_PROVIDER_CODES = frozenset({
    "qc_provider_http_error",
    "qc_provider_timeout",
    "qc_provider_empty_response",
    "qc_provider_unparseable",
    "qc_provider_schema_violation",
    "qc_provider_model_mismatch",
    "qc_provider_usage_missing",
    "qc_provider_route_mismatch",
})

# Closed feedback carried from one failed logical extraction attempt to the next. These
# values are prompt material, never durable artefact fields and never free-form provider
# text. They make a temperature-zero retry materially different while keeping the request
# contract auditable and bounded.
QC_RETRY_REASON_NONE = "none"
QC_RETRY_REASON_QUOTE_NOT_FOUND = "quote_not_found"
QC_RETRY_REASON_QUOTE_AMBIGUOUS = "quote_ambiguous"
QC_RETRY_REASON_SCHEMA_INVALID = "schema_invalid"
QC_RETRY_REASON_PROVIDER_UNPARSEABLE = "provider_unparseable"
QC_RETRY_REASON_CODES = (
    QC_RETRY_REASON_NONE,
    QC_RETRY_REASON_QUOTE_NOT_FOUND,
    QC_RETRY_REASON_QUOTE_AMBIGUOUS,
    QC_RETRY_REASON_SCHEMA_INVALID,
    QC_RETRY_REASON_PROVIDER_UNPARSEABLE,
)

__all__ = [
    "QC_API_KEY_ENV",
    "QC_BASE_URL_ENV",
    "QC_PROVIDER_BASE_URL",
    "QC_MODEL_UPSTREAM",
    "QC_PROVIDER_CODES",
    "QC_RETRY_REASON_NONE",
    "QC_RETRY_REASON_QUOTE_NOT_FOUND",
    "QC_RETRY_REASON_QUOTE_AMBIGUOUS",
    "QC_RETRY_REASON_SCHEMA_INVALID",
    "QC_RETRY_REASON_PROVIDER_UNPARSEABLE",
    "QC_RETRY_REASON_CODES",
]
