"""B-03: deterministic post-mutation language re-scan -- pure coordinator.

Builds one bounded, prose-free report tracking language-consistency evidence across the
four real manuscript mutation boundaries (post_map, post_polish, post_revise, final),
binding to the two existing deterministic language mechanisms without altering either:

  - narasi_gate.language_consistency_scan        (sentence-level, function-word + script leak)
  - narasi_counters.language_consistency_word_scan (word-level, Indonesian-particle leak)

This module never reads os.environ, never imports a provider/network/database client, never
calls scan_manuscript()/gate_text() (which would activate unrelated counters), and never
mutates the candidate text it is given. Every function here is a pure function of its
arguments; the caller (python/orchestrator/static.py, python/narration_api.py) owns flag
checks, logging, and result-dict wiring. B-03 is independent of B-01/B-02: it never reads
their reports as evidence and produces its own separate key, `post_mutation_language_rescan`.

Rework 3 (Codex adversarial re-audit, 2026-07-24): Rework 2's sealed `_StageObservation` used
a module-level `_CONSTRUCTION_SENTINEL` object plus a plain `__setattr__`/`__delattr__`
override as its "immutability" and "provenance" story. An independent adversarial validation
(read-only, 7 probes) confirmed this was insufficient on two separate axes:

  1. `object.__setattr__(entry, "candidate_sha256", forged)` (and the same for `status`, or any
     other field) bypasses a plain `__setattr__` method override entirely -- Python only routes
     normal attribute assignment (`entry.x = y`) through the type's own `__setattr__`;
     `object.__setattr__` calls the base implementation directly. Once an entry existed, EVERY
     field on it could be silently rewritten post-construction, and `build_report()` had no way
     to detect this: candidate_sha256 was checked only for hex-format, and status was trusted
     as a literal with no re-derivation from the stored sentence_scan/word_scan evidence.
  2. `_CONSTRUCTION_SENTINEL` was an ordinary importable module global (Python's leading
     underscore is a naming convention, not access control): `from narasi_language_rescan
     import _CONSTRUCTION_SENTINEL` plus a direct `_StageObservation(_CONSTRUCTION_SENTINEL,
     stage=..., candidate_sha256=FORGED, status="clean", ...)` call bypassed
     `_build_applicable`'s honest hash/status derivation entirely while still satisfying
     `build_report`'s only gate, `type(entry) is _StageObservation`.

Rework 3's redesign replaces BOTH of these with mechanisms that do not share the same failure
mode:

  - Verifier-owned provenance, not caller-visible state: `_StageObservation`, the minting
    functions, the WeakKeyDictionary provenance registry, and the per-process HMAC secret used
    to seal every mint all live inside ONE closure (`_make_stage_observation_module`, below).
    Only `scan_stage`/`not_applicable_stage`/`incomplete_stage` (this module's real public
    surface, each with its own input validation) and `_StageObservation` (needed by tests to
    attempt forgery against it directly) are returned as module-level names.
    `_build_applicable`/`_build_incomplete`/`_build_not_applicable`/`_mint` are NOT
    module-level names at all -- there is no `narasi_language_rescan._build_applicable` to
    import, unlike the sentinel before it.
  - Read-only properties, not a `__setattr__` override: every public field
    (`stage`/`target_language`/`candidate_sha256`/`applicability`/`status`/`sentence_scan`/
    `word_scan`/`errors`/`reason`) is a `property` with no setter, backed by a private
    `_raw_*` slot. A property with no setter is a data descriptor, and Python's attribute-set
    machinery (used by BOTH `entry.x = y` AND `object.__setattr__(entry, "x", y)`) checks for a
    data descriptor on the type before ever touching instance storage -- so `object.__setattr__`
    raises the identical `AttributeError` a normal assignment would, closing exactly the P1/P2
    bypass (empirically verified: `object.__setattr__(instance, "<property name>", value)` on a
    no-setter property raises "property '<name>' of '<Class>' object has no setter"). The
    private `_raw_*` slots underneath are still ordinary settable slots (Python offers no way to
    make a slot itself un-writable while the class remains an ordinary, introspectable object),
    which is exactly why provenance verification -- not attribute protection -- is the real
    trust boundary; see below.
  - A closure-sealed provenance check as the actual trust boundary. Every mint
    (`_build_applicable`/`_build_incomplete`/`_build_not_applicable`, reachable only from
    `scan_stage`/`not_applicable_stage`/`incomplete_stage` inside the SAME closure) computes an
    HMAC-SHA256 digest over a canonical JSON serialization of every one of the observation's
    nine fields, keyed by a `secrets.token_bytes(32)` secret generated once per process, and
    records `{observation: digest}` in a `weakref.WeakKeyDictionary`. Neither the secret nor the
    registry is assigned to a module-level name (see the "Threat model" section below for
    exactly what this does and does not mean). `_verify_stage_observation_provenance` (the only
    closure function returned for this purpose, since it can only ANSWER true/false through the
    module's normal API) recomputes that same digest from the observation's CURRENT field values
    and compares with `hmac.compare_digest`. This closes P1/P2/P4/P6 at once: a forged raw slot (via
    `object.__setattr__` on the private name, or via `object.__new__` + manual slot assignment
    entirely bypassing the class's properties) changes what gets recomputed, so it no longer
    matches the digest sealed at mint time; an object never minted through this module's own
    closure was never sealed at all, so its lookup in the registry returns nothing and it is
    rejected outright, regardless of how "real" (`type(x) is _StageObservation`) it looks. The
    registry is a `WeakKeyDictionary` (not a plain dict keyed by `id()`, which would risk a
    stale entry validating an unrelated object after the original is garbage-collected and its
    id reused) -- `_StageObservation.__slots__` includes `"__weakref__"` for exactly this reason.
  - Deep-copy-on-read for every mutable field. `sentence_scan`/`word_scan` (dicts) and `errors`
    (a list) are returned as a fresh `copy.deepcopy()`/`list()` from their properties, from
    `to_dict()`, and from the dict-like `__getitem__`/`get()` convenience methods (which route
    through the SAME properties via `getattr`) -- never the live stored reference. Mutating any
    value returned by ANY of these accessors can therefore never alter the observation itself,
    closing the pre-build nested-mutation class of attack (Rework 2's `copy.deepcopy()` inside
    `build_report()` only protected the ALREADY-BUILT report from later caller mutation; it did
    nothing to stop a caller from mutating an entry's nested evidence BEFORE `build_report()`
    ever saw it, since `sentence_scan`/`word_scan` were previously returned as live references).
  - Independent semantic re-derivation, not merely a trusted status literal.
    `_revalidate_stage_entry` now calls `_expected_finding_status(sentence_scan, word_scan)` --
    the SAME finding/clean derivation `_build_applicable` uses internally, re-run here against
    the (already-normalized) evidence -- and rejects any entry whose stored `status` disagrees.
    This is deliberately independent of (not merely implied by) the provenance seal: even if the
    seal mechanism were somehow compromised or a future bug in `_build_applicable` itself sealed
    a self-inconsistent observation, this check alone would still catch the mismatch, because it
    never trusts the stored `status` field at all -- it only trusts what the normalized evidence
    itself says.

Target-language validation is centralized in one shared `valid_target_language()`, delegating
to `continuity.lifecycle.canonicalize_target_language` (this repository's own established "one
true target_language authority") and requiring the value to already equal its own canonical
form -- missing, blank, whitespace-only, wrong-case, or otherwise noncanonical values are all
rejected, never silently normalized. Every construction path (`scan_stage`, `not_applicable_stage`)
uses this same validator, and `build_report` requires a `not_applicable` entry's `target_language`
to exactly equal the report's own, not merely be type-correct.

Exact-type discipline throughout: every dict this module reads (a scanner's own return value) is
accepted only when `type(x) is dict` -- never `isinstance`, which would also accept a hostile
subclass whose `__eq__`/`__hash__`/`.items()` could misbehave. A malformed or hostile-shaped
return from either scanner, or a malformed/forged/unregistered stage entry, becomes
`status="incomplete"`/`coverage="incomplete"` with a bounded error code; it is never silently
reported as `clean`, and reading it never crashes `build_report()`.

Threat model (Codex Rework 4 audit, 2026-07-24 -- stated explicitly, not implied): the
provenance seal, the closure scoping, and the read-only properties defend against ORDINARY
caller-visible tampering through this module's own supported surface -- reference mutation,
`entry.x = y`, `object.__setattr__` on a documented property name or a guessed/discovered
`_raw_*` slot name, and direct construction attempts via `_StageObservation(...)` or
`object.__new__(_StageObservation)` followed by manual slot assignment. They do NOT, and cannot,
defend against arbitrary in-process Python code with reflection or monkeypatching capability:
`inspect.getclosurevars()` (or equivalent frame/cell introspection) can recover a closure's free
variables, including this module's HMAC secret and provenance registry, from a running
`_StageObservation`-related function object; and any in-process code can reassign
`narasi_language_rescan._verify_stage_observation_provenance` (or any other module attribute) to
a stand-in of its choosing. No pure-Python construct -- closures, properties, slots, or
otherwise -- can prevent this, because Python does not offer a privilege boundary within a single
process. This module's actual, defensible claim is narrower and is the one that matters for its
real callers (`python/narration_api.py`, `python/orchestrator/static.py`, and this module's own
test suite): a caller using the ordinary, documented API cannot forge a "clean"/"finding" report,
and the accidental or buggy paths that Rework 1-3 each found were previously exploitable close.
Defending against a fully malicious, arbitrary-code-execution actor already running inside the
same Python process is out of scope -- that actor could rewrite this module's bytecode directly.

Rework 4 (Codex P1 finding, 2026-07-24): every equality/membership/set-construction operation
reachable with a raw, not-yet-validated value (a caller's `stage` argument, a scanner's raw
return value and its keys/values) is now exact-type-guarded BEFORE the operation that would
invoke a hostile object's `__eq__`/`__hash__` -- `scan_stage`/`not_applicable_stage`/
`incomplete_stage`'s own `stage` parameter, and `_normalize_word_scan`'s `status` field, were the
two confirmed crash sites; `_revalidate_stage_entry` and `build_report`'s error-labeling are
guarded the same way as defense in depth, even though a legitimately-minted entry can only ever
carry a plain `str` there. `_normalize_sentence_scan`/`_normalize_word_scan`/
`_revalidate_stage_entry` additionally run their entire body inside a backstop try/except: no
finite set of explicit guards can enumerate every hostile-object interaction (e.g. a dict key
whose `__hash__` succeeds once during the scanner's own dict construction but raises on a later
re-hash inside this module's own set-comprehension checks), so any unexpected exception there
must degrade to the same bounded incomplete/rejection path, never crash the caller.

Rework 4 (Codex P2 finding, 2026-07-24): `scan_stage()` (the production entry point) no longer
accepts `sentence_scanner`/`word_scanner` override parameters at all -- Rework 1-3 accepted them
as optional keywords defaulting to the real scanners, which meant this module could not prove a
production call site hadn't (by bug, or by a careless future edit) substituted a fake scanner and
produced a fabricated "clean" report that was otherwise correctly sealed and provenance-verified.
Test code that needs a controlled stand-in scanner must use `_scan_stage_test_seam()` (this
module's test-only entry point, see its own docstring); this module's own test suite proves via
static (AST) inspection that neither `python/narration_api.py` nor `python/orchestrator/static.py`
ever calls `_scan_stage_test_seam` -- they can only reach the real scanners.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import secrets
import weakref
from typing import Any, Callable, Optional

SCHEMA_VERSION = 1
STAGE_ORDER = ("post_map", "post_polish", "post_revise", "final")
_STAGE_INDEX = {s: i for i, s in enumerate(STAGE_ORDER)}
_ALWAYS_APPLICABLE_STAGES = frozenset({"post_revise", "final"})
_MAX_SAMPLES = 10
_MAX_SAMPLE_LEN = 200
_MAX_OTHER_LANGS = 20


def valid_target_language(value: Any) -> bool:
    """THE single shared canonical-language validator used by every construction path in this
    module. Delegates to `continuity.lifecycle.canonicalize_target_language` -- this
    repository's own established target-language authority -- and requires `value` to already
    equal ITS OWN canonical form (round-trip equality), never merely be coercible to one:
    missing, non-str, empty, whitespace-only, surrounding-whitespace, or wrong-case values are
    all rejected outright rather than silently normalized. Never raises."""
    if type(value) is not str:
        return False
    try:
        from continuity.lifecycle import canonicalize_target_language
        return canonicalize_target_language(value) == value
    except Exception:  # noqa: BLE001 - any failure here means "not valid", never a raise
        return False


def candidate_sha256(candidate_text: Any) -> Optional[str]:
    """Exact SHA-256 hex digest of `candidate_text`, or None for any non-`str` input or a
    UTF-8 encode failure. Never raises."""
    if type(candidate_text) is not str:
        return None
    try:
        return hashlib.sha256(candidate_text.encode("utf-8", errors="strict")).hexdigest()
    except Exception:  # noqa: BLE001 - a hash failure must become None, never a raise
        return None


def _is_hash64(value: Any) -> bool:
    return (type(value) is str and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


_SENTENCE_SCAN_KEYS = frozenset({"applies", "hits", "samples", "other_langs"})
_WORD_SCAN_KEYS = frozenset({"status", "count", "samples"})
_WORD_SCAN_SAMPLE_KEYS = frozenset({"term", "snippet"})


def _normalize_sentence_scan(raw: Any) -> Optional[dict]:
    """Closes narasi_gate.language_consistency_scan's return into a bounded, exact-typed,
    exact-SCHEMA shape, or None if the return does not match the documented contract exactly.
    Enforces the scanner's own documented invariants: `applies` False requires `hits==0`,
    `samples==[]`, `other_langs=={}`; `hits` must be `>= len(samples)`; EVERY `other_langs`
    value must be a POSITIVE int; and `hits==0` requires `other_langs=={}`. Never trusts a
    scanner return blindly.

    Rework 4 (Codex P1 finding, 2026-07-24): every equality/membership/set-construction
    operation below is now exact-type-guarded FIRST (so a hostile object's `__eq__`/`__hash__`
    is never invoked at all), but as a final backstop the ENTIRE body also runs inside a single
    try/except: a hostile object can still reach a raw scanner return in ways no finite set of
    explicit guards can fully enumerate (e.g. a dict key whose `__hash__` succeeds once during
    the scanner's own dict construction but raises on a later re-hash here) -- ANY such
    exception must degrade to None (the caller's own bounded incomplete/error-code path), never
    escape and crash `scan_stage()`/`build_report()`."""
    try:
        if type(raw) is not dict:
            return None
        captured = list(raw.items())
        if {k for k, _ in captured} != _SENTENCE_SCAN_KEYS:
            return None
        closed = dict(captured)
        applies = closed["applies"]
        hits = closed["hits"]
        samples = closed["samples"]
        other_langs = closed["other_langs"]
        if type(applies) is not bool or type(hits) is not int or type(hits) is bool or hits < 0:
            return None
        if type(samples) is not list or type(other_langs) is not dict:
            return None
        closed_samples = []
        for s in list(samples)[:_MAX_SAMPLES]:
            if type(s) is not str:
                return None
            closed_samples.append(s[:_MAX_SAMPLE_LEN])
        closed_other: dict[str, int] = {}
        for k, v in list(other_langs.items())[:_MAX_OTHER_LANGS]:
            if type(k) is not str or type(v) is not int or type(v) is bool or v <= 0:
                return None
            closed_other[k] = v
        if not applies:
            if hits != 0 or closed_samples or closed_other:
                return None  # applies=False can never carry evidence -- self-contradictory
        if hits == 0 and closed_other:
            return None  # zero total hits cannot have a nonempty per-language breakdown
        if hits < len(closed_samples):
            return None  # samples is a truncation of the hit list, never larger than hits
        return {"applies": applies, "hits": hits, "samples": closed_samples, "other_langs": closed_other}
    except Exception:  # noqa: BLE001 - any hostile-input crash here must become None, never raise
        return None


def _normalize_word_scan(raw: Any) -> Optional[dict]:
    """Closes narasi_counters.language_consistency_word_scan's return into a bounded,
    exact-typed, exact-schema shape, or None on any shape mismatch (see
    _normalize_sentence_scan). Enforces: `status="PASS"` requires `count==0` and
    `samples==[]`; `status="FLAG"` requires `count>=1`; `count` must always be `>=
    len(samples)`. Same Rework 4 exact-type-first-plus-final-backstop discipline as
    `_normalize_sentence_scan` (see its docstring)."""
    try:
        if type(raw) is not dict:
            return None
        captured = list(raw.items())
        if {k for k, _ in captured} != _WORD_SCAN_KEYS:
            return None
        closed = dict(captured)
        status = closed["status"]
        count = closed["count"]
        samples = closed["samples"]
        if type(status) is not str or status not in ("PASS", "FLAG"):
            return None
        if type(count) is not int or type(count) is bool or count < 0 or type(samples) is not list:
            return None
        closed_samples = []
        for s in list(samples)[:_MAX_SAMPLES]:
            if type(s) is not dict:
                return None
            s_captured = list(s.items())
            if {k for k, _ in s_captured} != _WORD_SCAN_SAMPLE_KEYS:
                return None
            s_closed = dict(s_captured)
            term = s_closed["term"]
            snippet = s_closed["snippet"]
            if type(term) is not str or type(snippet) is not str:
                return None
            closed_samples.append({"term": term[:80], "snippet": snippet[:100]})
        if status == "PASS" and (count != 0 or closed_samples):
            return None  # PASS can never carry evidence -- self-contradictory
        if status == "FLAG" and count < 1:
            return None  # FLAG with zero hits is self-contradictory
        if count < len(closed_samples):
            return None  # samples is a truncation of the hit count, never larger than count
        return {"status": status, "count": count, "samples": closed_samples}
    except Exception:  # noqa: BLE001 - any hostile-input crash here must become None, never raise
        return None


def _expected_finding_status(sentence_scan: dict, word_scan: dict) -> str:
    """Independently derives the expected clean/finding verdict from ALREADY-NORMALIZED
    evidence -- the exact same formula the minting logic uses internally, re-run here so
    `_revalidate_stage_entry` never merely trusts a stored `status` literal at face value
    (Rework 3, Codex P2/P3/P6 findings, 2026-07-24). A pure function of its two arguments;
    never raises, never reads any module state."""
    finding = bool(sentence_scan["applies"] and sentence_scan["hits"] > 0) \
        or word_scan["status"] == "FLAG"
    return "finding" if finding else "clean"


def _make_stage_observation_module():
    """Factory returning the sealed-observation machinery as one closure. The WeakKeyDictionary
    provenance registry and the per-process HMAC secret used to seal every mint live in this
    function's local scope and are not assigned to a module-level name -- ordinary module-level
    access (`from narasi_language_rescan import ...`, or reading `narasi_language_rescan.<name>`)
    cannot reach them, and `_mint`/`_build_applicable`/`_build_incomplete`/`_build_not_applicable`
    (the only functions that can produce a sealed `_StageObservation`) are themselves
    closure-local, not module attributes. See the module docstring's "Threat model" section for
    what this does and does NOT defend against -- closure scoping is not a defense against
    arbitrary in-process reflection (`inspect.getclosurevars()` and similar can still reach
    closure cells) or monkeypatching; it defends against the ORDINARY module-attribute-access
    surface a caller or test would use.

    Returned for module-level assignment: `_StageObservation` (the class; needed so tests can
    attempt forgery directly against it and confirm it fails), `scan_stage` (production entry
    point, no scanner-override parameters), `_scan_stage_test_seam` (test-only entry point that
    DOES accept scanner overrides -- see its own docstring for why this is a separate function
    rather than an optional parameter on `scan_stage`), `not_applicable_stage`, `incomplete_stage`
    (this module's remaining public surface, each with its own independent input validation
    before ever reaching the minting functions), and `_verify_stage_observation_provenance` (a
    verification-only function that can only answer true/false and can neither mint an
    observation nor reveal the secret/registry).
    """
    _secret = secrets.token_bytes(32)
    _provenance: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
    _PUBLIC_FIELDS = (
        "stage", "target_language", "candidate_sha256", "applicability", "status",
        "sentence_scan", "word_scan", "errors", "reason",
    )

    def _canonical_bytes(stage: Any, target_language: Any, candidate_sha256_: Any,
                         applicability: Any, status: Any, sentence_scan: Any, word_scan: Any,
                         errors: Any, reason: Any) -> bytes:
        payload = {
            "stage": stage, "target_language": target_language,
            "candidate_sha256": candidate_sha256_, "applicability": applicability,
            "status": status, "sentence_scan": sentence_scan, "word_scan": word_scan,
            "errors": list(errors), "reason": reason,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")

    def _seal(*fields: Any) -> bytes:
        return hmac.new(_secret, _canonical_bytes(*fields), hashlib.sha256).digest()

    class _StageObservation:
        """Sealed stage observation. Cannot be constructed via its own `__init__` under any
        circumstances -- the only legitimate instances come from this module's closure-private
        `_mint()`, reachable exclusively through `scan_stage`/`not_applicable_stage`/
        `incomplete_stage`. Every public field is a read-only `property` backed by a private
        `_raw_*` slot: a no-setter property is a data descriptor, and Python's attribute-set
        machinery -- invoked identically by `entry.x = y` AND `object.__setattr__(entry, "x", y)`
        -- checks for a data descriptor on the type before touching instance storage, so BOTH
        raise the same `AttributeError`. `sentence_scan`/`word_scan`/`errors` (and `to_dict()`/
        `__getitem__`/`get()`, which route through these same properties) always return a fresh
        `copy.deepcopy()`/`list()` -- never the live stored reference -- so no value obtained
        from ANY accessor can ever be mutated back into the observation. None of this is the
        actual trust boundary, though: the private `_raw_*` slots are still ordinary settable
        slots (there is no way to prevent `object.__setattr__(entry, "_raw_status", x)` from
        writing into an exposed class's own slot storage), which is exactly why
        `_verify_stage_observation_provenance` (in the enclosing closure) -- not attribute
        protection -- is what `build_report()` ultimately relies on: it independently reseals
        every CURRENT field value and compares against what was sealed at mint time, so even a
        forged raw slot is caught."""

        __slots__ = (
            "__weakref__", "_raw_stage", "_raw_target_language", "_raw_candidate_sha256",
            "_raw_applicability", "_raw_status", "_raw_sentence_scan", "_raw_word_scan",
            "_raw_errors", "_raw_reason",
        )

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise TypeError(
                "_StageObservation cannot be constructed directly -- use "
                "scan_stage()/not_applicable_stage()/incomplete_stage()")

        @property
        def stage(self) -> str:
            return self._raw_stage

        @property
        def target_language(self) -> Optional[str]:
            return self._raw_target_language

        @property
        def candidate_sha256(self) -> Optional[str]:
            return self._raw_candidate_sha256

        @property
        def applicability(self) -> str:
            return self._raw_applicability

        @property
        def status(self) -> str:
            return self._raw_status

        @property
        def sentence_scan(self) -> Optional[dict]:
            return copy.deepcopy(self._raw_sentence_scan) if self._raw_sentence_scan is not None else None

        @property
        def word_scan(self) -> Optional[dict]:
            return copy.deepcopy(self._raw_word_scan) if self._raw_word_scan is not None else None

        @property
        def errors(self) -> list:
            return list(self._raw_errors)

        @property
        def reason(self) -> str:
            return self._raw_reason

        def to_dict(self) -> dict:
            return {
                "stage": self.stage, "target_language": self.target_language,
                "candidate_sha256": self.candidate_sha256, "applicability": self.applicability,
                "status": self.status, "sentence_scan": self.sentence_scan,
                "word_scan": self.word_scan, "errors": self.errors, "reason": self.reason,
            }

        def __getitem__(self, key: str) -> Any:
            if key not in _PUBLIC_FIELDS:
                raise KeyError(key)
            return getattr(self, key)

        def get(self, key: str, default: Any = None) -> Any:
            return getattr(self, key) if key in _PUBLIC_FIELDS else default

        def __contains__(self, key: str) -> bool:
            return key in _PUBLIC_FIELDS

    def _mint(stage: str, target_language: Optional[str], candidate_sha256_: Optional[str],
               applicability: str, status: str, sentence_scan: Optional[dict],
               word_scan: Optional[dict], errors: list, reason: str) -> "_StageObservation":
        obs = object.__new__(_StageObservation)
        object.__setattr__(obs, "_raw_stage", stage)
        object.__setattr__(obs, "_raw_target_language", target_language)
        object.__setattr__(obs, "_raw_candidate_sha256", candidate_sha256_)
        object.__setattr__(obs, "_raw_applicability", applicability)
        object.__setattr__(obs, "_raw_status", status)
        object.__setattr__(obs, "_raw_sentence_scan", sentence_scan)
        object.__setattr__(obs, "_raw_word_scan", word_scan)
        object.__setattr__(obs, "_raw_errors", list(errors))
        object.__setattr__(obs, "_raw_reason", reason)
        _provenance[obs] = _seal(stage, target_language, candidate_sha256_, applicability,
                                  status, sentence_scan, word_scan, list(errors), reason)
        return obs

    def _build_applicable(*, stage: str, target_language: str, candidate_text: str,
                           sentence_scan: dict, word_scan: dict) -> "_StageObservation":
        """The ONLY path that can produce a "clean"/"finding" observation. `candidate_sha256`
        is computed HERE, directly from `candidate_text` -- never accepted as a parameter from
        any caller, so it can never be a forged/stale value. Not reachable from outside this
        closure. Uses the SAME `_expected_finding_status` formula `_revalidate_stage_entry`
        re-derives later, rather than a separately-maintained inline copy, so the two can never
        drift apart (Rework 4 cleanup)."""
        digest = candidate_sha256(candidate_text)
        finding = _expected_finding_status(sentence_scan, word_scan) == "finding"
        return _mint(stage, target_language, digest, "applicable",
                     "finding" if finding else "clean", sentence_scan, word_scan, [], "")

    def _build_incomplete(*, stage: str, target_language: Optional[str],
                           candidate_sha256_: Optional[str], sentence_scan: Optional[dict],
                           word_scan: Optional[dict], errors: list) -> "_StageObservation":
        return _mint(stage, target_language, candidate_sha256_, "applicable", "incomplete",
                     sentence_scan, word_scan, errors, "")

    def _build_not_applicable(*, stage: str, target_language: str,
                               reason: str) -> "_StageObservation":
        return _mint(stage, target_language, None, "not_applicable", "not_applicable",
                     None, None, [], reason)

    def _verify_provenance(obs: Any) -> bool:
        """The actual trust boundary: True iff `obs` is a genuine `_StageObservation` AND its
        current field values (read via the raw slots, not the deep-copying properties -- there
        is nothing to alias here since this only reads, never returns, the values) still match
        the digest sealed by `_mint()` at construction time. False for: anything not of this
        exact type; a genuine-looking instance built by any means other than this closure's own
        `_mint()` (never registered, so the lookup misses entirely); a genuine, properly-minted
        instance whose raw slots were subsequently altered by `object.__setattr__` on the
        private name (the resealed digest no longer matches); and -- Rework 3 hardening -- an
        instance whose raw slots were replaced with a hostile value that raises when the reseal
        computation tries to serialize it (e.g. a dict subclass with a poisoned `.items()`,
        reachable via `object.__setattr__` on a raw slot exactly like the hash/status forgeries
        above). Reseal computation is never trusted to complete without raising: any exception
        there means "cannot verify", which must be treated as False, never let escape and crash
        `build_report()`."""
        if type(obs) is not _StageObservation:
            return False
        sealed = _provenance.get(obs)
        if sealed is None:
            return False
        try:
            current = _seal(obs._raw_stage, obs._raw_target_language, obs._raw_candidate_sha256,
                             obs._raw_applicability, obs._raw_status, obs._raw_sentence_scan,
                             obs._raw_word_scan, obs._raw_errors, obs._raw_reason)
        except Exception:  # noqa: BLE001 - a hostile/corrupted raw slot must fail verification,
            return False    # never crash build_report()
        return hmac.compare_digest(sealed, current)

    def _scan_stage_impl(*, candidate_text: Any, target_language: Any, stage: Any,
                         sentence_scanner: Optional[Callable],
                         word_scanner: Optional[Callable]) -> "_StageObservation":
        """Shared implementation for `scan_stage()` (production, always real scanners) and
        `_scan_stage_test_seam()` (test-only, accepts scanner overrides) -- see both callers'
        own docstrings for why the override capability is not on the production entry point.

        Rework 4 (Codex P1 finding, 2026-07-24): `stage` is exact-type-checked BEFORE the
        `not in STAGE_ORDER` membership test -- `in`/`not in` on a tuple calls `__eq__` against
        every member, so a hostile `stage` object with a poisoned `__eq__` used to crash this
        function outright before Rework 4. Never raises: any scanner exception, import failure,
        or malformed scanner return becomes `status="incomplete"` with a bounded error code --
        incomplete must never collapse into clean. Returns a `_StageObservation`, not a dict;
        `build_report` is the only place a plain-dict view is produced."""
        if type(stage) is not str or stage not in STAGE_ORDER:
            return _build_incomplete(
                stage=stage if type(stage) is str else "unknown", target_language=None,
                candidate_sha256_=None, sentence_scan=None, word_scan=None,
                errors=["STAGE_ID_INVALID"])
        if type(candidate_text) is not str:
            return _build_incomplete(
                stage=stage, target_language=None, candidate_sha256_=None,
                sentence_scan=None, word_scan=None, errors=["CANDIDATE_TYPE_INVALID"])
        if not valid_target_language(target_language):
            return _build_incomplete(
                stage=stage, target_language=None, candidate_sha256_=candidate_sha256(candidate_text),
                sentence_scan=None, word_scan=None, errors=["TARGET_LANGUAGE_INVALID"])
        digest = candidate_sha256(candidate_text)
        if digest is None:
            return _build_incomplete(
                stage=stage, target_language=target_language, candidate_sha256_=None,
                sentence_scan=None, word_scan=None, errors=["CANDIDATE_HASH_FAILED"])

        _sentence_fn = sentence_scanner
        _word_fn = word_scanner
        if _sentence_fn is None or _word_fn is None:
            try:
                if _sentence_fn is None:
                    import narasi_gate as _ng
                    _sentence_fn = _ng.language_consistency_scan
                if _word_fn is None:
                    import narasi_counters as _nc
                    _word_fn = _nc.language_consistency_word_scan
            except Exception:  # noqa: BLE001 - an import failure must become incomplete
                return _build_incomplete(
                    stage=stage, target_language=target_language, candidate_sha256_=digest,
                    sentence_scan=None, word_scan=None, errors=["SCANNER_IMPORT_FAILED"])

        try:
            raw_sentence = _sentence_fn(candidate_text, target_language)
        except Exception:  # noqa: BLE001 - a scanner exception must become incomplete, never raise
            raw_sentence = None
        try:
            raw_word = _word_fn(candidate_text, target_language)
        except Exception:  # noqa: BLE001
            raw_word = None

        sentence_scan = _normalize_sentence_scan(raw_sentence)
        word_scan = _normalize_word_scan(raw_word)
        errors = []
        if sentence_scan is None:
            errors.append("SENTENCE_SCAN_UNAVAILABLE")
        if word_scan is None:
            errors.append("WORD_SCAN_UNAVAILABLE")
        if errors:
            return _build_incomplete(
                stage=stage, target_language=target_language, candidate_sha256_=digest,
                sentence_scan=sentence_scan, word_scan=word_scan, errors=errors)

        return _build_applicable(
            stage=stage, target_language=target_language, candidate_text=candidate_text,
            sentence_scan=sentence_scan, word_scan=word_scan)

    def scan_stage(*, candidate_text: Any, target_language: Any, stage: Any) -> "_StageObservation":
        """Builds ONE sealed stage observation for `stage`, ALWAYS using the two real
        deterministic scanners (`narasi_gate.language_consistency_scan` /
        `narasi_counters.language_consistency_word_scan`). This is the ONLY scan entry point
        production call sites (`python/narration_api.py`, `python/orchestrator/static.py`) may
        use, and its signature has no scanner-override parameters at all -- Rework 3 accepted
        `sentence_scanner`/`word_scanner` here as optional keywords, which meant a production
        hook (by bug or by a future careless edit) COULD have silently substituted a fake
        scanner and produced a fabricated "clean" report that was otherwise correctly sealed and
        provenance-verified. Codex's Rework 4 P2 finding requires this capability be physically
        absent from the production entry point, not merely undocumented. Test code that needs a
        controlled stand-in scanner must use `_scan_stage_test_seam()` instead (see its own
        docstring) -- an AST-based test proves neither production file ever calls it."""
        return _scan_stage_impl(candidate_text=candidate_text, target_language=target_language,
                                 stage=stage, sentence_scanner=None, word_scanner=None)

    def _scan_stage_test_seam(*, candidate_text: Any, target_language: Any, stage: Any,
                               sentence_scanner: Optional[Callable] = None,
                               word_scanner: Optional[Callable] = None) -> "_StageObservation":
        """TEST-ONLY dependency seam: identical to `scan_stage()` except it accepts
        `sentence_scanner`/`word_scanner` overrides (each falling back to the real scanner when
        omitted). This function exists SOLELY so this module's own test suite can inject a
        controlled stand-in scanner to exercise malformed/hostile/exception-raising scanner
        returns without needing the real `narasi_gate`/`narasi_counters` modules to misbehave.
        Production code must never call this -- use `scan_stage()`, which has no override
        parameters at all."""
        return _scan_stage_impl(candidate_text=candidate_text, target_language=target_language,
                                 stage=stage, sentence_scanner=sentence_scanner,
                                 word_scanner=word_scanner)

    def not_applicable_stage(stage: Any, target_language: Any,
                              reason: str) -> Optional["_StageObservation"]:
        """A legitimate, explicit not_applicable stage observation -- used ONLY for
        post_map/post_polish when the caller has independently, positively classified this job as
        never reaching narrate_chapters's map-reduce boundary. Returns None (never a placeholder
        observation) when `stage` is post_revise/final (which can never be not_applicable) or when
        `target_language` fails the same shared `valid_target_language` check every other
        construction path uses -- a caller must NEVER seed a not_applicable claim in an invalid or
        noncanonical language; if it cannot supply one, the stage should stay genuinely absent
        (surfacing truthfully as STAGE_MISSING) rather than fabricate one. Rework 4: `stage` is
        exact-type-checked before every membership test, same rationale as `scan_stage`."""
        if type(stage) is not str or stage not in STAGE_ORDER or stage in _ALWAYS_APPLICABLE_STAGES:
            return None
        if not valid_target_language(target_language):
            return None
        clean_reason = reason[:100] if type(reason) is str and reason.strip() else "unspecified"
        return _build_not_applicable(stage=stage, target_language=target_language, reason=clean_reason)

    def incomplete_stage(stage: Any, target_language: Any, error_code: str) -> "_StageObservation":
        """A bounded incomplete stage observation for a hook's OWN internal failure (e.g. an
        exception raised by the hook's bookkeeping code, independent of `scan_stage` itself) --
        used so a hook failure lands a real observation rather than leaving the stage silently
        absent. `target_language` is stored only when it independently passes
        `valid_target_language`; otherwise None. Rework 4: `stage` is exact-type-checked before
        the membership test, same rationale as `scan_stage`."""
        lang = target_language if valid_target_language(target_language) else None
        code = error_code if type(error_code) is str and error_code else "HOOK_FAILED"
        return _build_incomplete(
            stage=stage if type(stage) is str and stage in STAGE_ORDER else "unknown",
            target_language=lang, candidate_sha256_=None, sentence_scan=None, word_scan=None,
            errors=[code])

    return (_StageObservation, scan_stage, _scan_stage_test_seam, not_applicable_stage,
            incomplete_stage, _verify_provenance)


(_StageObservation, scan_stage, _scan_stage_test_seam, not_applicable_stage, incomplete_stage,
 _verify_stage_observation_provenance) = _make_stage_observation_module()


def _revalidate_stage_entry(entry: Any, *, report_target_language: str) -> Optional[dict]:
    """Independently re-validates ONE stage observation's provenance AND cross-report
    invariants, and serializes it to a plain dict -- the SOLE trust boundary for entering the
    final report. The first and non-negotiable gate is `type(entry) is _StageObservation`; the
    second, equally non-negotiable, is `_verify_stage_observation_provenance(entry)` -- an
    object that is the right TYPE but was never sealed by this module's own closure (or was
    sealed, then had a raw slot altered afterward via `object.__setattr__`) is rejected here,
    before any of its fields are trusted for anything else.

    Beyond provenance: target_language must be present and EXACTLY equal to
    `report_target_language` for BOTH "applicable" and "not_applicable" entries; post_revise/
    final can never be not_applicable; every remaining field is re-checked via the same
    scanner-shape normalizers `scan_stage` itself uses, as defense in depth; and -- new in
    Rework 3 -- for a "clean"/"finding" entry, the stored `status` MUST exactly equal
    `_expected_finding_status(sentence_scan, word_scan)` recomputed from the (already
    independently normalized) evidence. This is deliberately independent of the provenance
    check above: even if a future bug in the minting logic itself sealed a self-inconsistent
    observation, this would still catch it, because it never trusts the stored `status` literal
    at all.

    Rework 4 (Codex audit-completeness instruction, 2026-07-24): every membership/equality
    check below is exact-type-guarded first. This is defense in depth, not the primary defense
    -- a legitimately-minted entry can only ever carry a plain `str` in these fields (`scan_stage`
    itself enforces that before minting), so these fields are not attacker-reachable through the
    supported API surface. They ARE reachable through direct forgery
    (`object.__new__(_StageObservation)` + `object.__setattr__` on a raw slot, bypassing minting
    entirely), which already fails the provenance check above and is rejected before any of
    these lines run -- but the exact-type guards, plus the function-wide backstop below, mean
    that even a forged entry that somehow passed provenance can never crash this function."""
    try:
        if type(entry) is not _StageObservation:
            return None
        if not _verify_stage_observation_provenance(entry):
            return None
        stage = entry.stage
        if type(stage) is not str or stage not in STAGE_ORDER:
            return None
        if type(entry.applicability) is not str or entry.applicability not in ("applicable", "not_applicable"):
            return None
        if type(entry.status) is not str or entry.status not in ("clean", "finding", "incomplete", "not_applicable"):
            return None
        errors = entry.errors
        if type(errors) is not list or any(type(e) is not str for e in errors):
            return None
        if type(entry.reason) is not str:
            return None

        return _revalidate_stage_entry_impl(entry, stage=stage, errors=errors,
                                             report_target_language=report_target_language)
    except Exception:  # noqa: BLE001 - any hostile/unexpected value anywhere below must
        return None      # degrade to rejection, never crash build_report()


def _revalidate_stage_entry_impl(entry: Any, *, stage: str, errors: list,
                                  report_target_language: str) -> Optional[dict]:
    """The exact-type-confirmed continuation of `_revalidate_stage_entry` -- split out only so
    the try/except backstop above wraps the type checks AND this remaining logic in one place
    without a second, redundant try/except. Not reachable from outside this module and not
    meaningful to call directly (it assumes its caller's checks already ran)."""
    if entry.status in ("clean", "finding", "not_applicable"):
        # every non-incomplete status REQUIRES a present, exactly-matching target_language --
        # this is the SAME requirement for not_applicable as for applicable entries.
        if type(entry.target_language) is not str or entry.target_language != report_target_language:
            return None
    elif entry.target_language is not None:
        # an "incomplete" entry MAY carry a language (when only the scanner step failed) --
        # if it does, it must still agree with the report.
        if entry.target_language != report_target_language:
            return None

    if entry.applicability == "not_applicable":
        if stage in _ALWAYS_APPLICABLE_STAGES:
            return None
        if entry.status != "not_applicable":
            return None
        if (entry.candidate_sha256 is not None or entry.sentence_scan is not None
                or entry.word_scan is not None or errors):
            return None
        if not entry.reason:
            return None
        return entry.to_dict()

    # applicability == "applicable"
    if entry.reason != "":
        return None
    if entry.status == "incomplete":
        if not errors:
            return None
        if entry.candidate_sha256 is not None and not _is_hash64(entry.candidate_sha256):
            return None
        if entry.sentence_scan is not None and _normalize_sentence_scan(entry.sentence_scan) is None:
            return None
        if entry.word_scan is not None and _normalize_word_scan(entry.word_scan) is None:
            return None
        return entry.to_dict()

    if entry.status not in ("clean", "finding"):
        return None
    if errors:
        return None
    if not _is_hash64(entry.candidate_sha256):
        return None
    if entry.sentence_scan is None or entry.word_scan is None:
        return None
    normalized_sentence = _normalize_sentence_scan(entry.sentence_scan)
    normalized_word = _normalize_word_scan(entry.word_scan)
    if normalized_sentence is None or normalized_word is None:
        return None
    if entry.status != _expected_finding_status(normalized_sentence, normalized_word):
        return None
    return entry.to_dict()


def build_report(target_language: Any, stage_entries: Any) -> dict:
    """Assembles the final closed `post_mutation_language_rescan` report from an ordered list
    of stage observations. This is the SOLE trust boundary: every entry is independently
    re-validated (provenance first, then semantic and cross-report invariants) via
    `_revalidate_stage_entry`; nothing is accepted on the strength of merely looking right.
    Never mutates its input -- takes one atomic `list(stage_entries)` copy before iterating.

    Rejects (marks incomplete) any of:
    - anything that is not a genuine, PROVENANCE-VERIFIED `_StageObservation` (a plain dict, an
      object of the right type but never minted by this module's own closure, or a genuinely
      minted observation whose raw fields were altered after the fact, are all rejected
      outright);
    - a stage entry whose own `target_language` disagrees with the report-level one -- for
      BOTH `applicable` and `not_applicable` entries;
    - a "clean"/"finding" entry whose stored `status` disagrees with what its own normalized
      evidence independently implies;
    - a report missing any of the four `STAGE_ORDER` stages entirely;
    - `post_revise` or `final` ever being `not_applicable`;
    - stage entries appearing out of `STAGE_ORDER`'s relative order;
    - a duplicate stage id;
    - any entry that fails full-schema/invariant re-validation.

    Coverage is only "complete" once all four stages are truthfully accounted for, in order,
    each independently re-validated. `status="finding"` takes priority whenever any accepted
    stage found something; otherwise `status` is "clean" only if coverage is "complete"."""
    if not valid_target_language(target_language):
        return {
            "schema_version": SCHEMA_VERSION, "target_language": None,
            "coverage": "incomplete", "status": "incomplete", "stages": [],
            "finding_count": 0, "errors": ["TARGET_LANGUAGE_INVALID"],
        }
    if type(stage_entries) is not list:
        return {
            "schema_version": SCHEMA_VERSION, "target_language": target_language,
            "coverage": "incomplete", "status": "incomplete", "stages": [],
            "finding_count": 0, "errors": ["STAGE_ENTRIES_TYPE_INVALID"],
        }
    entries = list(stage_entries)

    stages: list = []
    coverage = "complete"
    overall_errors: list[str] = []
    finding_count = 0
    any_finding = False
    seen_stage_ids: set = set()
    max_seen_index = -1

    def _add_error(code: str) -> None:
        if code not in overall_errors:
            overall_errors.append(code)

    for raw_entry in entries:
        # Rework 4: this whole error-code-selection block only ever picks a DIAGNOSTIC label
        # for an entry `_revalidate_stage_entry` already rejected -- it never decides
        # acceptance. But `raw_entry.stage`/`.applicability`/`.target_language` are plain
        # property reads with no validation of their own, so a directly-forged entry (which
        # bypassed minting and therefore ALSO failed provenance above) could still carry a
        # hostile value into a bare `in`/`==` here. Exact-type-guard every check, and wrap the
        # whole block in a backstop try/except: worst case is a less-specific error code, never
        # a crash.
        try:
            raw_stage = raw_entry.stage if type(raw_entry) is _StageObservation else None
        except Exception:  # noqa: BLE001
            raw_stage = None

        validated = _revalidate_stage_entry(raw_entry, report_target_language=target_language)
        if validated is None:
            coverage = "incomplete"
            try:
                if type(raw_entry) is not _StageObservation:
                    _add_error("STAGE_ENTRY_NOT_SEALED")
                elif not _verify_stage_observation_provenance(raw_entry):
                    _add_error("STAGE_ENTRY_PROVENANCE_INVALID")
                elif type(raw_stage) is not str or raw_stage not in STAGE_ORDER:
                    _add_error("STAGE_ID_INVALID")
                elif (type(raw_entry.applicability) is str
                        and raw_entry.applicability != "not_applicable"
                        and type(raw_entry.target_language) is str
                        and raw_entry.target_language != target_language):
                    _add_error("TARGET_LANGUAGE_CONFLICT")
                else:
                    _add_error("STAGE_ENTRY_INVALID")
            except Exception:  # noqa: BLE001 - a hostile field must never crash error labeling
                _add_error("STAGE_ENTRY_INVALID")
            continue

        entry_stage = validated["stage"]
        if entry_stage in seen_stage_ids:
            coverage = "incomplete"
            _add_error("STAGE_DUPLICATE")
            continue
        entry_index = _STAGE_INDEX[entry_stage]
        if entry_index < max_seen_index:
            coverage = "incomplete"
            _add_error("STAGE_ORDER_INVALID")
        max_seen_index = max(max_seen_index, entry_index)
        seen_stage_ids.add(entry_stage)

        stages.append(copy.deepcopy(validated))
        entry_status = validated["status"]
        if entry_status == "finding":
            any_finding = True
            finding_count += 1
        elif entry_status == "incomplete":
            coverage = "incomplete"

    missing_stages = [s for s in STAGE_ORDER if s not in seen_stage_ids]
    if missing_stages:
        coverage = "incomplete"
        _add_error("STAGE_MISSING")

    overall_status = "finding" if any_finding else ("clean" if coverage == "complete" else "incomplete")
    return {
        "schema_version": SCHEMA_VERSION, "target_language": target_language,
        "coverage": coverage, "status": overall_status, "stages": stages,
        "finding_count": finding_count, "errors": overall_errors,
    }
