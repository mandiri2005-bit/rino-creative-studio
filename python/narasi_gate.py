# ── narasi_gate — CC INSTRUCTION v3 (R-FG4/R-FG5/R-FG6/R-FG9): deterministic post-generation
# gates for the narasi engines. NO LLM calls, NO network, stdlib only — importable from
# both laozhang_api.py and the orchestrator without cycles.
#
#   R-FG9  unbracketed numeric-placeholder residue: R-FG6 only sees text that lived inside
#          a [VERIFY: ...] wrapper. When a model skips the [VERIFY] discipline entirely and
#          writes "ongeveer 3?" or "sebanyak {N} orang" or "[TBD]" as bare prose, R-FG5's
#          directive-scan doesn't fire (no VERIFY/TODO/FIXME/CITE/STYLE token) and the
#          number-guess-plus-hedge ships. R-FG9 deterministically catches bare '3?', '{N}',
#          '[X]', '<TBD>', 'XX', 'TBD', 'TKTK' etc. and CUTS the enclosing sentence — safer
#          than emitting a hedge on a value we have zero confidence in. Aurelius Bab 4:
#          "De regenbelasting had het voorgaande kwartaal ongeveer 3? opgebracht".
#   R-FG6  resolve→hedge→cut: a flagged number ([VERIFY: ...]) has exactly three legal
#          exits — hedged value ("around 1547"), hedge-into-prose ("several times"), or
#          cut. A raw [VERIFY] is NEVER a legal output.
#   R-FG5  terminal bracket-block scan: after every rewrite (incl. the ⚡ polish), a
#          deterministic regex pass strips any surviving directive token so a manuscript
#          with [VERIFY]/TODO/FIXME residue can never ship. Whitelisted: VO performance
#          markers ([pause]/[beat]/[silence]/[ANCHOR]), the failed-chapter placeholder,
#          the "> **Gaya:** ..." metadata header (no brackets, untouched by design).
#   R-FG4  known_bad_claims: a per-project store of previously-flagged wrong claims
#          (seeded: siege of Tenochtitlan = 93 days — 75/79/80-day variants shipped
#          THREE times). Deterministic regex check auto-corrects (action=replace) or
#          flags (action=flag) on every gated text; also exported as a KNOWN CORRECTIONS
#          prompt block so generation is prevented, not just patched.
#
# Kill switch: NARASI_TERMINAL_GATE=0 disables the whole module (default ON — these are
# blocking bugs per the v3 spec, not style preferences).
from __future__ import annotations

import contextvars
import os
import re
import unicodedata
from typing import Any, Optional

# alt_history (regime-precedence spec §3): canon-contradiction enforcement OFF for the
# job — known-bad REPLACE actions and the KNOWN CORRECTIONS prompt block are suppressed
# so deliberate departures ("what if Cortés drowned at the Noche Triste") survive.
# ContextVar (not a module global) so concurrent worker jobs can't race each other.
_ALT_HISTORY: contextvars.ContextVar[bool] = contextvars.ContextVar("narasi_alt_history", default=False)


def set_alt_history(on: bool) -> None:
    _ALT_HISTORY.set(bool(on))

__all__ = [
    "gate_enabled", "gate_text", "resolve_flags", "apply_known_bad",
    "terminal_scan", "known_corrections_prompt", "set_db_claims", "BANNED_DICTION",
    "unplaced_numeric_placeholder_scan",
]


def gate_enabled() -> bool:
    return str(os.environ.get("NARASI_TERMINAL_GATE", "1")).strip().lower() not in ("0", "false", "no", "off")


# ── R-FG4 seed — in-process fallback so the gate works even before/without the DB
# table (narasi_known_bad_claims, migration 0057). Rows loaded from the DB are merged
# in via set_db_claims() at job start (best-effort).
#   pattern       must contain a named group (?P<bad>...) — the span that gets replaced
#                 (action=replace) or reported (action=flag).
#   action        "replace" → substitute the <bad> span with `correct`;
#                 "flag"    → never mutate, only report (logic errors can't be regexed
#                             safely into truth — they go to the report + the KNOWN
#                             CORRECTIONS prompt block).
KNOWN_BAD_SEED: list[dict[str, Any]] = [
    {
        # Siege of Tenochtitlan duration — shipped as "75/79/80 days" three times.
        # Context-scoped (same sentence must mention the siege/demolition/Tenochtitlan)
        # so an unrelated legitimate "75 days" in another narration is never touched.
        "name": "tenochtitlan-siege-93-days",
        "pattern": r"(?is)(?:siege|demolition|tenochtitlan|blockade)[^.!?\n]{0,200}?"
                   r"(?P<bad>(?:seventy[-\s]?five|seventy[-\s]?nine|75|79|80)[-\s]*(?:days?|hari))\b",
        "correct": "93 days",
        "action": "replace",
        "source": "human flag x3 (R-FG4 seed); siege = 93 days, May 22 - Aug 13, 1521",
    },
    {
        # Reverse order: the number precedes the siege mention in the sentence.
        "name": "tenochtitlan-siege-93-days-rev",
        "pattern": r"(?is)\b(?P<bad>(?:seventy[-\s]?five|seventy[-\s]?nine|75|79|80)[-\s]*(?:days?|hari))"
                   r"[^.!?\n]{0,200}?(?:siege|tenochtitlan)",
        "correct": "93 days",
        "action": "replace",
        "source": "human flag x3 (R-FG4 seed)",
    },
    {
        # Smallpox-immunity logic error: Indigenous allies (Tlaxcalans etc.) framed as
        # having Old World exposure/immunity. Flag-only — a logic claim can't be safely
        # regex-rewritten; prevention lives in the KNOWN CORRECTIONS prompt block.
        "name": "tlaxcalan-immunity-error",
        "pattern": r"(?is)(?P<bad>\b(?:tlaxcal\w+|totonac\w*|indigenous|native)\b[^.!?\n]{0,120}?"
                   r"(?:immun\w+|survived\s+childhood\s+smallpox|prior\s+exposure|old[-\s]world\s+exposure))",
        "correct": ("Indigenous allies (Tlaxcalans, Totonacs, etc.) had NO Old World disease exposure "
                    "or immunity — only Europeans with prior exposure did. Smallpox struck Indigenous "
                    "populations broadly; Tenochtitlan's fall was decisive because of density, timing, "
                    "leadership deaths, and siege pressure."),
        "action": "flag",
        "source": "backend-rules fix #2 (R-FG4 seed)",
    },
]

# Per-JOB, not per-process: the project-scoped known_bad rows are set at job start and
# read during that job's generation. Under the narration-worker (concurrency=4) and the
# 128-thread executor MANY jobs share ONE process, so a module global would let job B's
# set_db_claims() wipe job A's rules mid-scan (cross-project canon bleed, incl. a
# wrong-project "hard fact" baked into A's cached prompt). ContextVar isolates each job:
# set_db_claims runs before the job's gen task spawns, children inherit the context, and
# asyncio.to_thread copies it — mirrors the _ALT_HISTORY fix above.
_DB_CLAIMS: contextvars.ContextVar[list] = contextvars.ContextVar("narasi_db_claims", default=[])
# Hard cap on the apply_known_bad replace loop — belt-and-braces against a self-matching
# correction (a 'replace' whose corrected value re-matches its own pattern would otherwise
# spin forever and wedge the worker with no exception for gate_text to catch).
_MAX_REPLACE_ITERS = 200


def set_db_claims(rows: list[dict[str, Any]]) -> None:
    """Load DB known_bad_claims rows into THIS job's context (best-effort, called at job
    start). Rows need: pattern (with (?P<bad>...)), correct, action."""
    ok = []
    for r in rows or []:
        try:
            pat = r.get("bad_pattern") or r.get("pattern") or ""
            if not pat or "(?P<bad>" not in pat:
                continue
            re.compile(pat)   # reject broken patterns at load, not at gate time
            ok.append({
                "name": r.get("name") or "db-claim",
                "pattern": pat,
                "correct": r.get("correct_value") or r.get("correct") or "",
                "action": (r.get("action") or "replace").strip().lower(),
                "source": r.get("source") or "db",
            })
        except Exception:
            continue
    _DB_CLAIMS.set(ok)


def _claims() -> list[dict[str, Any]]:
    return KNOWN_BAD_SEED + list(_DB_CLAIMS.get())


def apply_known_bad(text: str) -> tuple[str, list[dict[str, Any]]]:
    """R-FG4 check. Returns (corrected_text, report_rows). action=replace substitutes the
    <bad> span with the correct value; action=flag only reports (never mutates)."""
    report: list[dict[str, Any]] = []
    if not text:
        return text, report
    if _ALT_HISTORY.get():
        return text, [{"claim": "alt_history", "action": "skipped",
                       "note": "canon-contradiction enforcement OFF for this job"}]
    for c in _claims():
        try:
            rx = re.compile(c["pattern"])
        except Exception:
            continue
        # Iterate manually so we replace ONLY the named <bad> span, keeping the context.
        # `pos` advances past each replacement so the corrected value can NEVER be re-scanned
        # (prevents an infinite loop when a correction re-matches its own pattern); the hard
        # iteration cap is a second backstop.
        pos = 0
        iters = 0
        while iters < _MAX_REPLACE_ITERS:
            iters += 1
            m = rx.search(text, pos)
            if not m or not m.group("bad"):
                break
            if c["action"] == "replace" and c.get("correct"):
                s, e = m.span("bad")
                text = text[:s] + c["correct"] + text[e:]
                report.append({"claim": c["name"], "action": "corrected",
                               "from": m.group("bad")[:80], "to": c["correct"][:80]})
                pos = s + len(c["correct"])   # skip past the insert — no re-scan of it
                continue   # re-search from pos: there may be more occurrences
            report.append({"claim": c["name"], "action": "flagged",
                           "span": m.group("bad")[:160], "note": c.get("correct", "")[:200]})
            break          # flag-only: report once per claim, never loop
    return text, report


# ── R-FG6: [VERIFY]/TODO/FIXME resolution — three legal exits, never a raw bracket. ──
# Optional preceding qualifier is consumed so "at least [VERIFY: number] times" reads
# "several times", and "in [VERIFY: 1547]" reads "around 1547" (not "in around 1547").
# ID-path fixes §3: hedge vocabulary comes from the LANGUAGE PACK, never hardcoded EN —
# the Diponegoro run shipped "sekitar several" and "tahun around 1830" because this pass
# enforced with the wrong language's templates. And the hedge pass may NEVER delete or
# replace a value: a bracket carrying ANY substantive text keeps that text (hedged).
_VERIFY_RX = re.compile(
    r"(?i)(?P<prep>\b(?:in|at|of|by|on)\s+)?(?P<qual>\b(?:at\s+least|exactly|precisely|approximately)\s+)?"
    r"\[\s*VERIFY\b[:\-]?\s*(?P<inner>[^\]]*)\]")
_CUT_RX = re.compile(r"(?i)\[\s*(?:TODO|FIXME|CITE|STYLE)\b[^\]]*\]")
_HEDGES = ("roughly", "about", "around", "approximately", "some", "circa", "nearly",
           "sekitar", "kurang lebih", "kira-kira", "hampir")
# generic bracket fillers that carry NO information — the only case where prose-hedge
# (which discards the inner text) is legal. Anything else = a value → keep it.
# R-FG9: extended with the same placeholder tokens that the unbracketed scanner catches,
# so a variant appearing INSIDE a [VERIFY: ...] wrapper (e.g., "[VERIFY: 3?]",
# "[VERIFY: {N}]", "[VERIFY: TBD]") is treated as empty/generic and cut per R-FG6 Exit 2.
_GENERIC_INNER = {"", "number", "value", "amount", "date", "year", "angka", "jumlah",
                  "tahun", "nilai", "n", "x", "?", "tbd", "...",
                  # R-FG9 additions
                  "tba", "tk", "tktk", "xx", "xxx", "xxxx",
                  "{n}", "{}", "{x}", "{0}",
                  "[x]", "[n]", "[?]", "[tbd]",
                  "<n>", "<x>", "<tbd>",
                  "3?", "todo"}
# an inner STARTING with these is a META-REQUEST ("jumlah surat yang disita dalam arsip
# KITLV"), not a value — wrapping it with a hedge would ship the request as prose.
_GENERIC_PREFIX_RX = re.compile(
    r"(?i)^(?:jumlah|berapa|angka|nilai|tanggal|number\s+of|how\s+many|amount\s+of|"
    r"the\s+number|the\s+exact|exact\s+(?:number|date|figure))\b")

# ── R-FG6b (sample-23): descriptive-slot placeholders ──────────────────────────
# A [VERIFY: <inner>] whose inner is an UNFILLED INSTRUCTION ("judul lagu yang relevan
# untuk konteks remaja Indonesia") rather than a value-in-words ("dua hari perjalanan")
# must NOT be hedge-wrapped — the ID pipeline shipped "Kamu denger sekitar judul lagu
# yang relevan…" (a raw slot marker) because Exit 1b treated the instruction as a value.
# Signal: NO number-word/digit AND a meta-instruction marker ("yang relevan/sesuai/…",
# "untuk konteks"). Such a sentence is unsalvageable (its object was never written) →
# drop the whole sentence. NARROW by design (only the clearest instruction markers) so a
# real name-slot ("[VERIFY: nama lengkap Sultan Ageng]") is NOT swept up.
_NUMBER_WORD_RX = re.compile(
    r"(?i)\b(?:satu|dua|tiga|empat|lima|enam|tujuh|delapan|sembilan|sepuluh|"
    r"puluh|ratus|ribu|juta|miliar|milyar|triliun|belas|lusin|kodi|"
    r"persen|perseratus|setengah|separuh|seperempat|paruh|"
    r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"dozen|hundred|thousand|million|billion|percent|half|quarter)\b")
_DESC_SLOT_INNER_RX = re.compile(
    r"(?i)(?:\byang\s+(?:relevan|sesuai|tepat|cocok|mengena|pas|dibutuhkan|diperlukan|"
    r"bersangkutan|dimaksud|mewakili|menggambarkan|mencerminkan)\b"
    r"|\b(?:untuk|dalam|sesuai(?:\s+dengan)?)\s+konteks\b)")


def _is_descriptive_slot(inner: str) -> bool:
    """True when a [VERIFY] inner is an unfilled instruction (no number-word/digit + a
    meta-instruction marker), not a value-in-words. Conservative: name/value slots pass."""
    if not inner:
        return False
    if any(c.isdigit() for c in inner) or _NUMBER_WORD_RX.search(inner):
        return False
    return bool(_DESC_SLOT_INNER_RX.search(inner))


def _strip_descriptive_slot_sentences(text: str) -> tuple[str, int]:
    """Remove whole sentences containing a [VERIFY: <descriptive slot>] (R-FG6b). The
    sentence is unsalvageable without the unwritten slot content. Paragraph-aware; a
    paragraph with no sentence terminator is treated as one unit."""
    if "[" not in text:
        return text, 0
    cut = 0
    out_paras: list[str] = []
    for para in text.split("\n"):
        sents = re.split(r"(?<=[.!?…])\s+", para)
        kept: list[str] = []
        for s in sents:
            if any(_is_descriptive_slot((m.group("inner") or "").strip())
                   for m in _VERIFY_RX.finditer(s)):
                cut += 1
                continue
            kept.append(s)
        out_paras.append(" ".join(kept))
    return "\n".join(out_paras), cut


def _hedge_pack(lang: str) -> tuple[str, str, bool]:
    """(hedge_value_template, hedge_prose_word, measured) from the language pack.
    measured=False → the pack has no hedge vocab for this language: per refactor §5 the
    pass must NOT enforce with another language's templates (UNMEASURED, keep values)."""
    try:
        from narasi_counters import LANGUAGE_PACKS
        pack = LANGUAGE_PACKS.get((lang or "en").split("-")[0].lower()) or {}
        hv, hp = pack.get("hedge_value"), pack.get("hedge_prose")
        if hv and hp:
            return hv, hp, True
    except Exception:  # noqa: BLE001
        pass
    return "around {v}", "several", (lang or "en").split("-")[0].lower() == "en"


def resolve_flags(text: str, lang: str = "en") -> tuple[str, dict[str, int]]:
    """Resolve every [VERIFY: ...] via hedge-value / hedge-prose / cut; cut TODO/FIXME.
    Localized (§3): hedge words come from the language pack; a value inside a bracket is
    NEVER deleted. Unmeasured language → brackets are unwrapped verbatim (keep inner)."""
    stats = {"hedged_value": 0, "hedged_prose": 0, "cut": 0, "unwrapped_unmeasured": 0,
             "exact_known_good": 0, "idempotent_skip": 0}
    if not text:
        return text, stats
    # R-FG6b: drop unsalvageable descriptive-slot sentences before hedging (gated so
    # flag-OFF stays byte-identical). Prevents "sekitar judul lagu yang relevan…".
    if _INFRA_FIXES_ON():
        text, _slot_cut = _strip_descriptive_slot_sentences(text)
        if _slot_cut:
            stats["descriptive_slot_sentence_cut"] = _slot_cut
    hv_tpl, hp_word, measured = _hedge_pack(lang)

    # Hedge IDEMPOTENCY guard (Diponegoro-2 review): the PROSE before the bracket often
    # already carries a qualifier ("lebih dari …", "hanya …", "rata-rata …") — stacking
    # our hedge on top shipped "lebih dari sekitar sekitar empat puluh persen". If the
    # ~28 chars before the bracket end with a qualifier, emit the BARE value.
    _PRE_QUAL_RX = re.compile(
        r"(?i)(?:lebih\s+dari|kurang\s+dari|hanya|sekitar|kurang\s+lebih|kira-kira|hampir|"
        r"nyaris|rata-rata|setidaknya|paling\s+tidak|mencapai|melebihi|menjelang|"
        r"di\s+atas|di\s+bawah|"
        r"more\s+than|less\s+than|only|about|around|roughly|approximately|nearly|"
        r"at\s+least|up\s+to|as\s+many\s+as|averaging)\s*$")

    # R-FG8 class-gating (round-2 §1): a DATE is a world-claim, not an estimate —
    # "sekitar 1825" is the tell that class-gating is absent. Pure year / day-month-year /
    # month-year tokens go on the never-hedge whitelist unconditionally.
    # bare-number branch restricted to plausible YEARS (1000-2099) — "200 prajurit" is a
    # quantity (hedge once), "1825" is a date (never hedge). The word branches require a
    # MONTH NAME — "12 days"/"40 km" are durations/measures, not dates.
    _MONTH = (r"(?:jan(?:uari)?|feb(?:ruari)?|mar(?:et|ch)?|apr(?:il)?|mei|may|jun[ie]?|"
              r"jul[iy]?|agustus|aug(?:ust)?|sep(?:tember)?|okt(?:ober)?|oct(?:ober)?|"
              r"nov(?:ember)?|des(?:ember)?|dec(?:ember)?)")
    _DATE_TOKEN_RX = re.compile(
        r"(?i)^(?:(?:1\d{3}|20\d{2})|\d{1,2}\s+" + _MONTH + r"\s+(?:1\d{3}|20\d{2})|"
        + _MONTH + r"\s+(?:1\d{3}|20\d{2})|\d{1,2}\s+" + _MONTH + r")$")

    def _pre_qualified(m: re.Match) -> bool:
        seg = m.string[max(0, m.start() - 28):m.start()]
        return bool(_PRE_QUAL_RX.search(seg))

    def _known_good_ctx(m: re.Match) -> bool:
        """R-FG8 epistemic class: a bracket whose SENTENCE matches a known_good pattern
        is a VERIFIED world fact — render EXACT, never hedged ('sekitar 1825' on a
        certain date was the Diponegoro-2 tell). Uses the per-job known_good context."""
        try:
            from narasi_factscan import _known_good
            s = m.string
            a = max(0, m.start() - 170)
            b = min(len(s), m.end() + 170)
            seg = s[a:b]
            # trim to the containing sentence-ish segment
            cut = max(seg.rfind(". ", 0, m.start() - a), seg.rfind("\n", 0, m.start() - a))
            if cut > 0:
                seg = seg[cut + 1:]
            return _known_good(seg)
        except Exception:  # noqa: BLE001
            return False

    def _strip_inner_hedge(inner: str) -> str:
        low = inner.lower()
        for h in sorted(_HEDGES, key=len, reverse=True):
            if low.startswith(h):
                return inner[len(h):].lstrip()
        return inner

    def _whole_sentence_bracket(m: re.Match) -> bool:
        s = m.string
        # strip only spaces/tabs — a NEWLINE is a sentence boundary and must survive
        before = s[max(0, m.start() - 4):m.start()].rstrip(" \t")
        after = s[m.end():m.end() + 4].lstrip(" \t")
        return (not before or before[-1] in ".!?…\n") and (not after or after[0] in ".!?…\n")

    def _verify_sub(m: re.Match) -> str:
        inner = (m.group("inner") or "").strip()
        if not measured:
            # §3.2: no hedge vocab for this language → do not enforce; strip the bracket,
            # KEEP the inner text untouched (never let the wrong language's words in).
            stats["unwrapped_unmeasured"] += 1
            prep = m.group("prep") or ""
            qual = m.group("qual") or ""
            return (prep + qual + inner).strip()
        generic = inner.lower() in _GENERIC_INNER or bool(_GENERIC_PREFIX_RX.match(inner))
        if not generic:
            # R-FG8: verified world fact → EXACT (no hedge at all, drop inner's own hedge).
            if _known_good_ctx(m):
                stats["exact_known_good"] += 1
                return _strip_inner_hedge(inner)
            # R-FG8 class-gating: a bare date/year token is a world-claim — NEVER hedged
            # (a date is not an estimate), cached or not.
            if _DATE_TOKEN_RX.match(_strip_inner_hedge(inner)):
                stats["exact_known_good"] += 1
                return _strip_inner_hedge(inner)
            # idempotency: prose already qualified this value → bare value, no new hedge.
            if _pre_qualified(m):
                stats["idempotent_skip"] += 1
                return _strip_inner_hedge(inner)
        if any(ch.isdigit() for ch in inner):
            # Exit 1 — a numeric value exists: emit it hedged (localized). Start from an
            # existing hedge word ("roughly 60-80 km") else hedge-wrap from the first digit.
            low = inner.lower()
            start = None
            for h in _HEDGES:
                i = low.find(h)
                if i >= 0 and (start is None or i < start):
                    start = i
            if start is not None:
                val = inner[start:].strip()
            else:
                di = next(i for i, ch in enumerate(inner) if ch.isdigit())
                val = hv_tpl.format(v=inner[di:].strip())
            stats["hedged_value"] += 1
            # A numeric hedge replaces the preposition ("in [VERIFY: 1547]" → "around 1547").
            return val
        if not generic:
            # Exit 1b (§3.1) — NON-numeric but substantive inner ("dua hari perjalanan"):
            # a value in words. NEVER delete it — hedge-wrap the text itself. If the inner
            # ALREADY starts with a hedge word ("sekitar empat puluh persen"), keep it
            # as-is — wrapping again shipped "sekitar sekitar…" in the Diponegoro run.
            stats["hedged_value"] += 1
            low_i = inner.lower()
            if any(low_i.startswith(h) for h in _HEDGES) or low_i.startswith(hp_word):
                return inner
            return hv_tpl.format(v=inner)
        # Exit 2 — genuinely empty/generic. A bracket standing as its OWN sentence would
        # leave a naked prose fragment ("beberapa.") — CUT it instead (the third legal
        # R-FG6 exit; Diponegoro-2 shipped "sekitar jumlah surat yang disita…" this way).
        if _whole_sentence_bracket(m):
            stats["cut"] += 1
            return ""
        stats["hedged_prose"] += 1
        prep = m.group("prep") or ""
        return (prep + hp_word).strip() if prep else hp_word

    text = _VERIFY_RX.sub(_verify_sub, text)
    text, n = _CUT_RX.subn("", text)
    stats["cut"] += n
    # tidy: collapse doubled spaces / space-before-punctuation the substitutions leave,
    # and orphaned punctuation from whole-sentence cuts
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" +([,.;:!?])", r"\1", text)
    text = re.sub(r"(?m)^[ \t]*[.!?…]+[ \t]*$\n?", "", text)
    # belt-and-braces: any double hedge the model itself wrote ("sekitar sekitar")
    text = re.sub(r"(?i)\b(sekitar|kira-kira|kurang lebih|hampir|around|roughly|about|"
                  r"approximately)\s+\1\b", r"\1", text)
    return text, stats


# ── R-FG5: terminal scan + deterministic strip of survivors. ──
# ID-path fixes §2: NO raw marker token ever ships (the Diponegoro run shipped 35, incl.
# lowercase [beat]). [ANCHOR] strips its TOKEN but KEEPS the line's text (anchors are
# protected lines, not disposable); [BEAT]/[pause]/[silence] strip clean — they already
# sit on their own paragraph, so the break they signal survives as layout. The only
# whitelisted bracket is the failed-chapter placeholder (an explicit signal, not residue).
_WHITELIST_RX = re.compile(r"(?i)^\[CHAPTER \d+ [—-].*FAILED TO GENERATE.*\]$")
_DIRECTIVE_RX = re.compile(r"(?i)\[[^\]]*(?:VERIFY|TODO|FIXME|CITE:|STYLE:)[^\]]*\]|\[\s*\]")
_MARKER_ANCHOR_RX = re.compile(r"(?i)\[\s*anchor[^\]]*\]\s*")
_MARKER_BREAK_RX = re.compile(r"(?i)[ \t]*\[\s*(?:beat|pause|silence)[^\]]*\][ \t]*")


_DEHEDGE_RX = re.compile(r"(?i)\b(sekitar|kira-kira|kurang lebih|around|roughly|about|"
                         r"approximately|circa)\s+")


def dehedge_known_good(text: str) -> tuple[str, int]:
    """R-FG8 render discipline: a CACHE-VERIFIED fact renders EXACT — 'sekitar 1825' on a
    known_good date is epistemically wrong (Diponegoro-2: hedges glued to certain dates
    all over). For every known_good pattern match, drop a hedge word IMMEDIATELY before
    the matched span. Only fires on cache-verified facts; everything else keeps its hedge."""
    if not text:
        return text, 0
    try:
        from narasi_factscan import _KNOWN_GOOD
        pats = _KNOWN_GOOD.get()
    except Exception:  # noqa: BLE001
        return text, 0
    n = 0
    _inner_rx = re.compile(_DEHEDGE_RX.pattern + r"(?=\d)")
    for p in pats or []:
        # right-to-left so earlier offsets stay valid after each splice
        for m in sorted(p.finditer(text), key=lambda x: -x.start()):
            s0, e0 = m.start(), m.end()
            # (a) hedge word immediately BEFORE the verified span
            seg_start = max(0, s0 - 18)
            pre = text[seg_start:s0]
            mh = _DEHEDGE_RX.search(pre)
            if mh and mh.end() == len(pre):
                text = text[:seg_start + mh.start()] + text[seg_start + mh.end():]
                n += 1
                continue
            # (b) hedge INSIDE the span glued to a digit ("pahlawan nasional pada
            # sekitar 1973" where the whole clause is the verified pattern)
            span = text[s0:e0]
            new_span, k = _inner_rx.subn("", span)
            if k:
                text = text[:s0] + new_span + text[e0:]
                n += k
    if n:
        text = re.sub(r"[ \t]{2,}", " ", text)
    return text, n


def strip_world_date_hedge(text: str, lang: str = "en") -> tuple[str, list[dict[str, Any]]]:
    """SPEC §3.4.2: world-documented values are NEVER hedged. If the pack defines a
    `world_date_hedge_rx` (fr: matches "vers le 14 mai 1643" class), strip the hedge
    word — the date itself stays. Flags reported for the audit."""
    if not text:
        return text, []
    try:
        from narasi_counters import LANGUAGE_PACKS
        pack = LANGUAGE_PACKS.get((lang or "en").split("-")[0].lower())
    except Exception:  # noqa: BLE001
        pack = None
    if not pack or not pack.get("world_date_hedge_rx"):
        return text, []
    rx = pack["world_date_hedge_rx"]
    report: list[dict[str, Any]] = []
    # right-to-left so offsets stay valid
    for m in sorted(rx.finditer(text), key=lambda x: -x.start()):
        hedge = m.group("hedge")
        date = m.group("date")
        # Replace the whole match with just the date span (drops the hedge word + space)
        text = text[:m.start()] + date + text[m.end():]
        report.append({"claim": "world_date_hedge_strip",
                       "action": "stripped",
                       "from": m.group(0)[:120],
                       "to": date,
                       "note": f"§3.4.2: world-documented date not hedged (dropped '{hedge}')"})
    return text, report


def strip_markers(text: str) -> tuple[str, int]:
    """§2.2/§2.3: remove every VO marker token, case-insensitive. Anchor TEXT survives;
    break markers vanish (their paragraph break already exists in the layout)."""
    if not text:
        return text, 0
    n = 0
    out, k = _MARKER_ANCHOR_RX.subn("", text)
    n += k
    out, k = _MARKER_BREAK_RX.subn("", out)
    n += k
    if n:
        out = re.sub(r"[ \t]{2,}", " ", out)
        out = re.sub(r"\n{4,}", "\n\n\n", out)
    return out, n


def foreign_token_scan(text: str, lang: str = "en") -> tuple[str, list[str]]:
    """§2.4: per-language blocklist of placeholder/hedge tokens from OTHER languages.
    A standalone EN token inside ID output ("sekitar several") = placeholder leakage —
    replaced with the pack's equivalent (or cut), reported like a bracket."""
    if not text:
        return text, []
    try:
        from narasi_counters import LANGUAGE_PACKS
        toks = (LANGUAGE_PACKS.get((lang or "en").split("-")[0].lower()) or {}).get("foreign_tokens") or {}
    except Exception:  # noqa: BLE001
        toks = {}
    if not toks:
        return text, []
    hits: list[str] = []

    def _sub(m: re.Match) -> str:
        w = m.group(0)
        hits.append(w)
        rep = toks.get(w.lower(), "")
        return rep

    rx = re.compile(r"(?i)\b(" + "|".join(re.escape(t) for t in toks) + r")\b")
    out = rx.sub(_sub, text)
    if hits:
        out = re.sub(r"[ \t]{2,}", " ", out)
        out = re.sub(r" +([,.;:!?])", r"\1", out)
    return out, hits


# ── language-consistency (report-only) ─────────────────────────────────────────
# Catch a run of text in a language OTHER than the job language — draft-residue leak,
# e.g. an Indonesian sentence ("Kenapa aku merasa lega?") inside an all-English
# manuscript. foreign_token_scan only substitutes a tiny per-language hedge-word
# blocklist (and is a no-op for EN); it does NOT see a whole leaked sentence. This is a
# REPORT-ONLY detector (writes report flags, never rewrites/blocks) and is honestly
# UNMEASURED for languages without a seeded function-word set.
_LANG_FUNCTION_WORDS: dict[str, set] = {
    "en": {"the", "and", "of", "to", "a", "in", "that", "it", "was", "for", "with", "as",
           "his", "her", "she", "he", "they", "you", "why", "did", "is", "are", "not",
           "but", "on", "at", "this", "from", "had", "have", "were", "would"},
    "id": {"yang", "dan", "di", "ke", "dari", "itu", "ini", "dengan", "untuk", "tidak",
           "aku", "kamu", "dia", "saya", "kenapa", "mengapa", "merasa", "adalah", "akan",
           "sudah", "pada", "juga", "karena", "atau", "saja", "sedang", "masih", "lega"},
    "es": {"el", "la", "de", "que", "y", "los", "las", "un", "una", "por", "con", "para",
           "no", "se", "su", "al", "del", "como", "pero", "más", "porque", "cuando"},
    "fr": {"le", "la", "les", "de", "des", "un", "une", "et", "que", "qui", "dans", "pour",
           "pas", "ne", "je", "il", "elle", "est", "son", "sa", "avec", "mais", "parce"},
    "de": {"der", "die", "das", "und", "den", "dem", "ein", "eine", "zu", "mit", "nicht",
           "ich", "sie", "er", "ist", "war", "für", "auf", "aber", "weil", "auch"},
    "pt": {"o", "a", "os", "as", "de", "que", "e", "um", "uma", "por", "para", "não", "se",
           "com", "mais", "porque", "como", "mas", "seu", "sua", "quando"},
}
# Latin-script job languages (for the non-Latin-script leak pass).
_LATIN_JOB_LANGS = {"en", "id", "es", "fr", "de", "pt", "nl", "jv", "su", "min", "ms",
                    "it", "pl", "cs", "hr", "sr", "sl", "mk", "lt", "lv", "et", "hu", "ro",
                    "tl", "vi"}
_LC_WORD_RX = re.compile(r"[A-Za-zÀ-ÿ]+")


def _nonlatin_run(s: str) -> int:
    """Longest run of consecutive non-Latin, non-common LETTER characters (Cyrillic,
    CJK, Arabic, Hangul, Devanagari, Thai …). Digits/punct/spaces break the run."""
    best = run = 0
    for ch in s:
        if not ch.isalpha():
            run = 0
            continue
        try:
            name = unicodedata.name(ch)
        except ValueError:
            run = 0
            continue
        # LATIN letters are fine for a Latin-script target; anything else is a leak.
        if name.startswith("LATIN"):
            run = 0
        else:
            run += 1
            best = max(best, run)
    return best


def language_consistency_scan(text: str, lang: str = "en") -> dict:
    """Report-only: flag sentences reading as a DIFFERENT language than the job `lang`.
    (a) function-word pass: a sentence with >=3 tokens in some OTHER seeded language's
        function-word set and 0 in the job language's; (b) script pass: for a Latin-script
        job, a run of >=4 consecutive non-Latin letters. Never raises; applies=False when
        neither pass can measure (no seeded set for `lang` and no script leak)."""
    res: dict[str, Any] = {"applies": False, "hits": 0, "samples": [], "other_langs": {}}
    if not text:
        return res
    try:
        base = (lang or "en").split("-")[0].lower()
        job_fw = _LANG_FUNCTION_WORDS.get(base)
        is_latin_job = base in _LATIN_JOB_LANGS
        # applies when we can run at least one pass
        res["applies"] = (job_fw is not None) or is_latin_job
        if not res["applies"]:
            return res
        hits: list[str] = []
        others: dict[str, int] = {}
        for s in _SENT_SPLIT_RX.split(text):
            s = s.strip()
            if not s or len(s) < 8:
                continue
            flagged = False
            # (a) function-word leak
            if job_fw is not None:
                toks = {w.lower() for w in _LC_WORD_RX.findall(s)}
                if len(toks) >= 3 and not (toks & job_fw):
                    for other, fw in _LANG_FUNCTION_WORDS.items():
                        if other == base:
                            continue
                        if len(toks & fw) >= 3:
                            others[other] = others.get(other, 0) + 1
                            flagged = True
                            break
            # (b) non-Latin-script leak into a Latin-script target
            if not flagged and is_latin_job and _nonlatin_run(s) >= 4:
                others["non_latin"] = others.get("non_latin", 0) + 1
                flagged = True
            if flagged and s not in hits:
                hits.append(s[:200])
        res["hits"] = len(hits)
        res["samples"] = hits[:10]
        res["other_langs"] = others
        return res
    except Exception:  # noqa: BLE001 — a broken scan must never break generation
        return {"applies": False, "hits": 0, "samples": [], "other_langs": {}}


# Diponegoro-2: manuscript-structure references leaking into prose ("reputasi taktisnya
# telah dibangun dalam bab-bab sebelumnya pertempuran") — meta-leak, report-only.
# Round-2 §7 extends the phrase list (prompt bleed, not authored prose).
_META_LEAK_RX = re.compile(r"(?i)\b(?:bab(?:-bab)?|chapters?)\s+"
                           r"(?:sebelumnya|berikutnya|selanjutnya|di\s+atas|earlier|previous|later|above)\b"
                           r"|\bseperti\s+disebutkan\b|\bsebagaimana\s+(?:dibahas|diuraikan)\b"
                           r"|\bpada\s+bagian\s+(?:sebelumnya|berikut)\b")

# Round-2 §2 terminal patterns — the [VERIFY] leak wearing prose:
# (a) placeholder-prose: "[hedge] <quantity-noun> …" with NO numeral in the sentence =
#     an unresolved value description ("sekitar jumlah surat yang disita…") → CUT.
_PLACEHOLDER_PROSE_RX = re.compile(
    r"(?i)\b(?:sekitar|kira-kira|kurang\s+lebih|beberapa|around|roughly|about|some|several)\s+"
    r"(?:jumlah|angka|banyaknya|nilai|total|the\s+number\s+of|the\s+amount\s+of)\b")
# (b) broken-substitution: stray pronoun immediately before a proper name — "Mereka Louw
#     dan De Klerck", "Ia Smissaert" — the corpse of a substitution that ate the verb.
_BROKEN_SUB_RX = re.compile(r"\b(?:Mereka|Ia|Dia|They|He|She)\s+[A-Z][a-z]+\s+(?:dan|de|van|und|and)\b")


def placeholder_prose_scan(text: str) -> tuple[str, list[str]]:
    """Cut sentences that describe a quantity with a hedge but carry NO numeral (§2a).
    Returns (text_without_them, cut_sentences)."""
    if not text or not _PLACEHOLDER_PROSE_RX.search(text):
        return text, []
    cut: list[str] = []
    out_parts: list[str] = []
    for para in text.split("\n"):
        if not _PLACEHOLDER_PROSE_RX.search(para):
            out_parts.append(para)
            continue
        sents = re.split(r"(?<=[.!?])\s+", para)
        kept = []
        for s in sents:
            if _PLACEHOLDER_PROSE_RX.search(s) and not any(ch.isdigit() for ch in s) \
                    and len(s.split()) <= 28:
                cut.append(s.strip()[:160])
                continue
            kept.append(s)
        out_parts.append(" ".join(kept))
    return "\n".join(out_parts), cut


# R-FG9: unbracketed numeric-placeholder residue — the [VERIFY] discipline was skipped
# entirely, so R-FG6 never wrapped this and R-FG5's directive-scan can't see it either.
# Aurelius Bab 4 shipped "ongeveer 3? opgebracht" (guess + question-mark hedge). Also
# catches template holes ({N}, [X], <TBD>) and journalism/editorial 'to come' markers
# (TBD, TBA, TK, TKTK, XX+) that the model shipped as bare prose.
_UNPLACED_NUM_PLACEHOLDER_RX = re.compile(
    r"(?<![\w.])"                              # left boundary — not mid-token / not part of a decimal
    r"(?:"
    r"\d+\s*\?"                                # bare '3?' or '42 ?' — model wrote a guess then hedged with '?'
    r"|\d+\s*\(\s*\?\s*\)"                     # '3(?)' academic-uncertainty variant
    r"|\{\s*[Nn0-9x]?\s*\}"                    # '{N}', '{}', '{n}', '{0}' template holes
    r"|\[\s*(?:X|N|n|x|\?|TBD|tbd|number|angka|jumlah)\s*\]"  # '[X]', '[TBD]', '[?]', '[jumlah]'
    r"|<\s*(?:X|N|n|x|TBD|number|angka|jumlah)\s*>"           # '<N>', '<TBD>' angle-bracket holes
    r"|\bXX+\b"                                # 'XX', 'XXXX' as a stand-in
    r"|\b(?:TBD|TBA|TK|TKTK)\b"                # journalism/editorial 'to come' markers
    r")"
    r"(?![\w.])"                               # right boundary
)


def unplaced_numeric_placeholder_scan(text: str) -> tuple[str, list[str]]:
    """R-FG9: catch UNBRACKETED numeric-placeholder residue that R-FG6 misses.
    A model that writes 'ongeveer 3?' or 'sebanyak {N} orang' shipped an unfilled
    guess — the [VERIFY] discipline was skipped, so the terminal bracket scan
    can't see it. Cut the enclosing sentence (safer than emitting a hedge on a
    number we have zero confidence in) and report every hit."""
    if not text:
        return text, []
    hits: list[str] = []
    if not _UNPLACED_NUM_PLACEHOLDER_RX.search(text):
        return text, []
    out_parts: list[str] = []
    for para in text.split("\n"):
        if not _UNPLACED_NUM_PLACEHOLDER_RX.search(para):
            out_parts.append(para)
            continue
        sents = re.split(r"(?<=[.!?])\s+", para)
        kept = []
        for s in sents:
            m = _UNPLACED_NUM_PLACEHOLDER_RX.search(s)
            if m:
                hits.append(s.strip()[:200] + f" [hit: {m.group(0)!r}]")
                continue  # drop the sentence — safer than hedging a zero-confidence number
            kept.append(s)
        out_parts.append(" ".join(kept))
    out = "\n".join(out_parts)
    # tidy: orphaned punctuation from the whole-sentence cuts
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"(?m)^[ \t]*[.!?…]+[ \t]*$\n?", "", out)
    return out, hits


def broken_substitution_scan(text: str) -> list[str]:
    """§2b: flag substitution corpses (report-only — the repair needs an editor)."""
    hits = []
    for m in _BROKEN_SUB_RX.finditer(text or ""):
        a = max(0, m.start() - 30)
        hits.append(text[a:m.end() + 40].replace("\n", " ").strip()[:120])
    return hits[:8]


def meta_leak_scan(text: str) -> list[str]:
    """Sentences that reference the manuscript's own structure (report-only)."""
    hits = []
    for m in _META_LEAK_RX.finditer(text or ""):
        a = max(0, m.start() - 60)
        hits.append(text[a:m.end() + 40].replace("\n", " ").strip()[:140])
    return hits[:8]


def terminal_scan(text: str) -> list[str]:
    """Return surviving directive tokens (post-resolution). Empty list = clean."""
    hits = []
    for m in _DIRECTIVE_RX.finditer(text or ""):
        tok = m.group(0)
        if not _WHITELIST_RX.match(tok):
            hits.append(tok[:80])
    return hits


def _MORALISTE_CALIBRATION_ON() -> bool:
    return os.environ.get("DALANG_MORALISTE_CALIBRATION") == "1"


def _INFRA_FIXES_ON() -> bool:
    """Phase 1 corpus-audit infra bundle (patches W + O flavor A/B/C + JJJ + JJ + UUUUU).
    All gated OFF by default; set DALANG_INFRA_FIXES=1 on the python service to activate.
    Flag-OFF is byte-identical to prior behavior.

    Sources: cross-15-sample audit 2026-07-05 surfaced 5 config/infra bugs that live
    at the pipeline boundary rather than the LLM: metadata `Output: book` mislabel for
    VO scripts (UUUUU, CROSS-7-SAMPLE), CJK word-count tokenizer (JJ, CROSS-2-SAMPLE),
    chapter-title double-prefix in 3 flavors (patch O A/B/C, CROSS-5-LANG), markdown-
    syntax leak in chapter titles (JJJ), and back-to-back duplicate-token typos (W)."""
    return os.environ.get("DALANG_INFRA_FIXES") == "1"


# W repeat_token_scan (Phase 1 patch): cross-language back-to-back duplicate-word detector.
# Detects `os os ombros`-style typos where the same token appears twice consecutively.
# Unicode-word-boundary aware; case-insensitive; requires ≥2 chars to skip pronoun collisions
# ('la la', 'na na', 'ya ya' — legit interjections). Deterministic, cross-lang.
_REPEAT_TOKEN_RX = re.compile(r"(?iu)\b(\w{2,})\s+\1\b")


def repeat_token_scan(text: str) -> tuple[int, list[str]]:
    """R-FG-W: back-to-back duplicate-token count + up to 10 sample matches.
    Empty text → (0, []). Deterministic; unicode-aware."""
    if not text:
        return 0, []
    hits: list[str] = []
    count = 0
    for m in _REPEAT_TOKEN_RX.finditer(text):
        count += 1
        if len(hits) < 10:
            hits.append(m.group(0)[:60])
    return count, hits


# ──────────────────────────────────────────────────────────────────────────
# Phase 3 (2026-07-05) — cross-validated R-FG counters, gated on DALANG_INFRA_FIXES=1.
# 5 patches from cross-15-sample corpus audit. All scanners are report-only heuristics —
# they surface stats into gate_text's report["flags"] but never block or rewrite text.
# Downstream editors/dashboards can turn stats into actionable UI later.
# ──────────────────────────────────────────────────────────────────────────

# Patch M — R-FG11 style-conditional threshold table.
# Cross-4-style validated. Different genres have DIFFERENT natural rates of narrator-
# opening-formula saturation. Moraliste at 4/4 samples with ratio=1.0 rated 8.9-9.2 —
# saturated openers are genre convention. Babad at 6/6 chronicle-formula openers is
# canonically correct. Pewayangan (dalang-narrated) typically 0.0-0.5. Cinematic
# voiceover (present-tense atmospheric) typically 0.0-0.15. Everything else defaults
# to 0.5. Extends the moraliste bundle's fixed-0.5 comparison (line 749 above).
_R_FG11_STYLE_THRESHOLDS: dict[str, float] = {
    # moraliste family — extends the moraliste calibration bundle default 0.5 → 0.83
    "ironic_moral_fable": 0.83, "moraliste": 0.83, "moralist_fable": 0.83,
    # babad_hikayat family — chronicle-opener saturation is genre-conventional
    "babad_hikayat": 1.0, "babad": 1.0, "hikayat": 1.0, "chronicle": 1.0,
    # pewayangan family — dalang narrator uses varied openers, not saturated formulas
    "pewayangan_dalang": 0.5, "pewayangan": 0.5, "wayang": 0.5,
    # cinematic voiceover family — atmospheric-scene openers, minimal formula
    "cinematic_voiceover": 0.15, "cinematic_voice_over": 0.15, "voiceover": 0.15,
    "documentary_voiceover": 0.15,
}


def _r_fg11_threshold(style: Optional[str]) -> float:
    """Look up per-style R-FG11 ratio_max. None/unknown style → 0.5 default."""
    if not style:
        return 0.5
    return _R_FG11_STYLE_THRESHOLDS.get(str(style).strip().lower(), 0.5)


# Patch J — source_note_density counter. Per-style thresholds for factual regimes.
# Cross-9-style validated. A citation-lite factual chapter often signals unhedged
# invention. Regex patterns per language capture the common attribution shapes.
_SOURCE_NOTE_PATTERNS: dict[str, str] = {
    "en": r"\b(?:according\s+to|as\s+(?:noted|reported|argued)\s+by|per\s+the|"
          r"cited\s+in|records?\s+of|argued\s+that|documented\s+by|"
          r"[A-Z][a-z]+\s*\(\d{4}\)|(?:\d{4}[a-z]?))\b",
    "id": r"(?:menurut\s+|catatan\s+|dalam\s+catatan|berdasarkan\s+|"
          r"seperti\s+dicatat|(?:sejarawan|arkeolog|antropolog|peneliti)\s+"
          r"[A-Z][a-z]+|\d{4}[a-z]?)",
    "es": r"(?:seg[uú]n\s+|de\s+acuerdo\s+con|registrado\s+por|"
          r"[A-Z][a-z]+\s+(?:sostiene|argumenta|se[ñn]ala)|\d{4}[a-z]?)",
    "fr": r"(?:selon\s+|d[’']apr[èe]s\s+|comme\s+l[’']a\s+not[eé]|"
          r"[A-Z][a-z]+\s+(?:soutient|argue|note)|\d{4}[a-z]?)",
    "de": r"(?:laut\s+|nach\s+|wie\s+.+?\s+festh[äa]lt|"
          r"[A-Z][a-z]+\s+(?:argumentiert|schreibt|notiert)|\d{4}[a-z]?)",
    "pt": r"(?:segundo\s+|de\s+acordo\s+com|conforme\s+registrado|"
          r"[A-Z][a-z]+\s+(?:argumenta|nota|documenta)|\d{4}[a-z]?)",
    "nl": r"(?:volgens\s+|zoals\s+.+?\s+opmerkt|"
          r"[A-Z][a-z]+\s+(?:betoogt|noteert|documenteert)|\d{4}[a-z]?)",
    "ja": r"(?:によれば|によると|が指摘するように|"
          r"[A-Z][a-z]+\s*\(\d{4}\)|\d{4}年)",
    "ko": r"(?:에\s*따르면|가\s+지적하듯이|이\s+주장하듯이|"
          r"[A-Z][a-z]+\s*\(\d{4}\)|\d{4}년)",
    "zh": r"(?:根据|据|指出|论证|文献|"
          r"[A-Z][a-z]+\s*\(\d{4}\)|\d{4}年)",
    "vi": r"(?:theo\s+|nh(ư|u)\s+.+?\s+(l(ưu|uu)\s+ý|ghi\s+nh(ậ|a)n)|"
          r"[A-Z][a-z]+\s+(?:l(ậ|a)p\s+lu(ậ|a)n)|\d{4})",
    "ar": r"(?:وفقاً\s+ل|بحسب|كما\s+يذكر|"
          r"[A-Z][a-z]+\s*\(\d{4}\)|\d{4})",
}

# Per-style density thresholds (mentions per 1000 words). Corpus signals:
# sample-9 Salt heavy (6.35), sample-10 Chicken mild (4.03), sample-16 Wheat missing.
_J_STYLE_DENSITY_MIN: dict[str, float] = {
    "creative_non_fiction": 2.0, "cnf": 2.0,
    "natgeo_documentary": 3.0, "natgeo": 3.0, "national_geographic": 3.0,
    "big_history": 3.0, "harari": 3.0,
    "journalistic_long_form": 2.0, "journalistic": 2.0,
    "youtube_popular_science": 1.5, "popular_science": 1.5,
    "literary_essay": 1.0,
    "academic_popular": 1.5,
    # explicit ZERO for non-factual + mythic regimes — patch J does not apply
    "ironic_moral_fable": 0.0, "moraliste": 0.0,
    "babad_hikayat": 0.0, "pewayangan_dalang": 0.0,
    "cinematic_voiceover": 0.0,
    "storytelling": 0.0, "bedtime_story": 0.0,
    "pov_first_person_immersive": 0.0,
}


def source_note_density_scan(text: str, lang: str = "en",
                              style: Optional[str] = None
                              ) -> tuple[int, float, bool]:
    """R-FG-J: count source-note/attribution markers + rate per 1000 words.
    Returns (count, rate_per_1000_words, under_threshold).
    under_threshold=True only when a per-style minimum applies and rate falls below.
    Empty text or unknown-language language → (0, 0.0, False)."""
    if not text:
        return 0, 0.0, False
    code = str(lang or "en").strip().lower().replace("_", "-").split("-", 1)[0]
    pat = _SOURCE_NOTE_PATTERNS.get(code)
    if not pat:
        return 0, 0.0, False
    try:
        rx = re.compile(pat, re.IGNORECASE | re.UNICODE)
    except re.error:
        return 0, 0.0, False
    count = sum(1 for _ in rx.finditer(text))
    n_words = max(1, len(text.split()))
    rate = 1000.0 * count / n_words
    style_key = (style or "").strip().lower()
    threshold = _J_STYLE_DENSITY_MIN.get(style_key, 0.0)
    under = threshold > 0 and rate < threshold
    return count, rate, under


# Patch LL — factual_ending_concrete_return.
# Cross-9-style validated for factual regimes. The final chapter's last 300 chars
# should include a sensory noun or physical object (Flannan lamp / Okavango elephant
# footprint / Salt tubulus / Chicken sediment). Report present/absent; only apply
# to factual regime styles.
_SENSORY_ANCHOR_PATTERNS: dict[str, str] = {
    "en": r"\b(?:light|lamp|stone|bone|rain|dust|salt|blood|hand|foot|"
          r"river|forest|sea|water|earth|sky|wind|fire|sun|moon|"
          r"skin|breath|voice|silence|smell|taste|touch)\b",
    "id": r"\b(?:cahaya|batu|tulang|hujan|debu|garam|darah|tangan|kaki|"
          r"sungai|hutan|laut|air|tanah|langit|angin|api|matahari|bulan|"
          r"kulit|nafas|suara|keheningan|bau|rasa|sentuhan)\b",
    "es": r"\b(?:luz|piedra|hueso|lluvia|polvo|sal|sangre|mano|pie|"
          r"r[ií]o|bosque|mar|agua|tierra|cielo|viento|fuego|sol|luna)\b",
    "fr": r"\b(?:lumi[èe]re|pierre|os|pluie|poussi[èe]re|sel|sang|main|pied|"
          r"rivi[èe]re|for[êe]t|mer|eau|terre|ciel|vent|feu|soleil|lune)\b",
    "de": r"\b(?:Licht|Stein|Knochen|Regen|Staub|Salz|Blut|Hand|Fu[ßs]|"
          r"Fluss|Wald|Meer|Wasser|Erde|Himmel|Wind|Feuer|Sonne|Mond)\b",
    "pt": r"\b(?:luz|pedra|osso|chuva|poeira|sal|sangue|m[ãa]o|p[ée]|"
          r"rio|floresta|mar|[áa]gua|terra|c[ée]u|vento|fogo|sol|lua)\b",
    "nl": r"\b(?:licht|steen|been|regen|stof|zout|bloed|hand|voet|"
          r"rivier|bos|zee|water|aarde|hemel|wind|vuur|zon|maan)\b",
    "ja": r"(?:光|石|骨|雨|塵|塩|血|手|足|川|森|海|水|土|空|風|火|太陽|月)",
    "ko": r"(?:빛|돌|뼈|비|먼지|소금|피|손|발|강|숲|바다|물|땅|하늘|바람|불|해|달)",
    "zh": r"(?:光|石|骨|雨|尘|盐|血|手|脚|河|森|海|水|土|天|风|火|阳|月)",
    "vi": r"\b(?:ánh sáng|đá|xương|mưa|bụi|muối|máu|tay|chân|"
          r"sông|rừng|biển|nước|đất|trời|gió|lửa|mặt trời|mặt trăng)\b",
    "ar": r"(?:ضوء|حجر|عظم|مطر|غبار|ملح|دم|يد|قدم|نهر|غابة|بحر|ماء|أرض|سماء)",
}

_FACTUAL_STYLES: frozenset = frozenset([
    "creative_non_fiction", "cnf",
    "natgeo_documentary", "natgeo", "national_geographic",
    "big_history", "harari",
    "journalistic_long_form", "journalistic",
    "youtube_popular_science", "popular_science",
    "literary_essay", "academic_popular",
])


def factual_ending_concrete_return_scan(text: str, lang: str = "en",
                                         style: Optional[str] = None,
                                         tail_chars: int = 400
                                         ) -> tuple[bool, bool, list[str]]:
    """R-FG-LL: for factual-regime styles, check whether the narrative's final
    tail_chars contains a concrete sensory noun. Returns (applies, present, matches).
    - applies=False when style is not in the factual set (patch not relevant)
    - present=True when at least one sensory anchor matches the tail
    - matches: up to 5 sample matches (for reporter)"""
    style_key = (style or "").strip().lower()
    if style_key not in _FACTUAL_STYLES:
        return False, True, []
    if not text:
        return True, False, []
    code = str(lang or "en").strip().lower().replace("_", "-").split("-", 1)[0]
    pat = _SENSORY_ANCHOR_PATTERNS.get(code)
    if not pat:
        return True, True, []   # no pattern for language → assume present (avoid false-negative)
    try:
        rx = re.compile(pat, re.IGNORECASE | re.UNICODE)
    except re.error:
        return True, True, []
    tail = text[-max(200, tail_chars):]
    matches: list[str] = []
    for m in rx.finditer(tail):
        matches.append(m.group(0)[:40])
        if len(matches) >= 5:
            break
    return True, bool(matches), matches


# Patch HH — human_anchor scanner. Cross-4-genre (documentary + journalistic +
# cinematic voiceover + chronicle). Detects named individuals — capitalized 1-3
# word proper nouns. Heuristic: Latin-script only; unicode-word-boundary aware.
# Reports count + first-8 unique names. Downstream can compute "recurring across
# chapters" from split.
_HUMAN_NAME_RX = re.compile(
    r"(?u)\b([A-ZÀ-Ý][A-Za-zÀ-ÿ.'\-]+(?:\s+[A-ZÀ-Ý][A-Za-zÀ-ÿ.'\-]+){0,2})\b"
)
_HUMAN_NAME_STOPWORDS: frozenset = frozenset([
    # Common lead-of-sentence words that would otherwise match the regex.
    "The", "A", "An", "In", "On", "At", "From", "To", "By", "For", "With",
    "But", "And", "Or", "Yet", "So", "As", "Of", "This", "That", "These",
    "Chapter", "Bab", "Kapitel", "Chapitre", "Cap", "Hoofdstuk", "Kabanata",
])


def human_anchor_scan(text: str, min_repeats: int = 2
                      ) -> tuple[int, list[str], int]:
    """R-FG-HH: count unique capitalized name-like tokens + list first N + count
    those appearing ≥min_repeats. Returns (unique_count, sample_names, recurring_count).
    Empty text → (0, [], 0). Heuristic; Latin-script narasi only."""
    if not text:
        return 0, [], 0
    seen: dict[str, int] = {}
    for m in _HUMAN_NAME_RX.finditer(text):
        name = m.group(1).strip()
        first = name.split(" ", 1)[0]
        if first in _HUMAN_NAME_STOPWORDS:
            continue
        # Skip single-word entries that are all-caps (likely acronyms) or too short.
        if len(name) < 3:
            continue
        seen[name] = seen.get(name, 0) + 1
    unique = len(seen)
    recurring = sum(1 for c in seen.values() if c >= min_repeats)
    sample = sorted(seen.keys(), key=lambda k: -seen[k])[:8]
    return unique, sample, recurring


# Patch IIIIII — entity_consistency_pass (shared front-end + report-only back-end).
# Cross-15-lens-2 samples. Audit refined the reviewer's original "single gate covers
# fiction + nonfiction" claim: the gate needs a SHARED entity-extraction front-end
# (same for both regimes) plus a GENRE-CONDITIONAL back-end (external lookup for
# nonfiction, internal-continuity for fiction). This scanner ships the shared front-
# end + internal-continuity check; external verification defers to Phase 4+.
def entity_consistency_scan(text: str) -> dict[str, Any]:
    """R-FG-IIIIII: extract capitalized entities from Bab 1-2 and Bab 3+ (using the
    chapter splitter). Report:
        early_entities: set of names appearing in Bab 1-2
        late_entities: set of names appearing in Bab 3+
        abandoned: early_entities - late_entities (Mode-B1 fiction character-abandonment
                    OR Mode-B1 nonfiction proper-noun-drift signal)
        introduced_late: late_entities - early_entities (new characters late in book)
    Heuristic; Latin-script narasi only; empty text → all-empty dict."""
    if not text:
        return {
            "early_entities_count": 0,
            "late_entities_count": 0,
            "abandoned_sample": [],
            "abandoned_count": 0,
            "introduced_late_sample": [],
            "introduced_late_count": 0,
        }
    # Split on chapter headings using the existing splitter.
    parts = _CHAPTER_SPLIT_RX.split(text)
    chapters = parts[1:] if len(parts) > 1 else parts
    early_text = "\n".join(chapters[:2])
    late_text = "\n".join(chapters[2:]) if len(chapters) > 2 else ""
    def _extract(t: str) -> set[str]:
        names: set[str] = set()
        for m in _HUMAN_NAME_RX.finditer(t):
            n = m.group(1).strip()
            first = n.split(" ", 1)[0]
            if first in _HUMAN_NAME_STOPWORDS or len(n) < 3:
                continue
            names.add(n)
        return names
    early = _extract(early_text)
    late = _extract(late_text)
    abandoned = sorted(early - late)[:8]
    introduced_late = sorted(late - early)[:8]
    return {
        "early_entities_count": len(early),
        "late_entities_count": len(late),
        "abandoned_sample": abandoned,
        "abandoned_count": len(early - late),
        "introduced_late_sample": introduced_late,
        "introduced_late_count": len(late - early),
    }


# ── Track A (Rino 2026-07-06, corpus sample-21/22) — two report-only scanners for the
# pipeline DEFAULTS that survived prompt-injection + substring bans across every rewrite:
# (1) glossary/kamus tic, (2) POV grammatical-persona drift. PATTERN-based, so they
# generalize across domains (SMA/kuliah/kerja) where a literal banned_tell list could not.

# Glossary gloss: an ACRONYM or short Title-case term IMMEDIATELY followed by an
# in-line definition via em-dash / en-dash / comma. Catches the FORM, not a term list:
#   "MPLS — Masa Pengenalan Lingkungan Sekolah"
#   "Ospek, orientasi studi dan pengenalan kampus"
#   "SKS — Satuan Kredit Semester"
#   "Kecerdasan emosional — kemampuan buat mengenali..."
# Heuristic: an ALL-CAPS acronym (2-6 letters) OR a 1-2 word Capitalised term, then a
# dash/comma, then >=3 lowercase-started words (the definition body). Latin-script.
_GLOSSARY_ACRONYM_RX = re.compile(
    r"\b([A-Z]{2,6})\b\s*[—–-]\s*([A-Z][a-z]+(?:\s+\w+){2,})"
)
_GLOSSARY_TERM_RX = re.compile(
    r"\b([A-Z][a-z]+(?:\s+[a-z]+)?)\s*[—–,]\s*((?:kemampuan|kegiatan|singkatan|istilah|"
    r"orientasi|jalur|ujian|ruang|proses|masa|zona|sistem|satuan|"
    # sample-23: 'Remaja — fase perkembangan…', 'Pacaran — hubungan romantis…' slipped
    r"fase|hubungan|kondisi|bentuk|pola|tahap|rentang|fenomena|keadaan|periode)"
    r"\b[\w\s,]{6,})"
)
# English inline gloss (sample-24 kdrama): 'nunchi — that distinctly Korean radar',
# 'gapjil — the abuse of hierarchical power', 'kibun — the emotional weather'. The tell is
# a term + em-dash + a definitional lead-noun, OR '— that <distinctly/untranslatable>'.
_GLOSSARY_TERM_EN_RX = re.compile(
    r"\b([A-Za-z][a-z]{2,})\s*[—–]\s*(?:"
    r"(?:that|the|a|an)\s+(?:\w+\s+){0,3}?(?:radar|weather|registry|abuse|practice|custom|"
    r"tradition|concept|art|skill|sense|shorthand|climate|hierarchy|ritual|etiquette|"
    r"notion|term|word)\b"
    r"|that\s+(?:distinctly|uniquely|untranslatable|so-called|peculiarly|characteristically)\b"
    r")"
)


def glossary_definition_scan(text: str) -> dict[str, Any]:
    """Report-only: detect inline dictionary/kamus glosses of common terms — the tic
    that survived every rewrite because banned_tells are literal substrings and each
    new domain (SMA vs kuliah) uses different acronyms. Returns count + up-to-8 samples.
    Native readers do not need MPLS/Ospek/SKS/UTS defined; the gloss breaks the register."""
    if not text:
        return {"glossary_hits": 0, "glossary_samples": []}
    samples: list[str] = []
    for rx in (_GLOSSARY_ACRONYM_RX, _GLOSSARY_TERM_RX, _GLOSSARY_TERM_EN_RX):
        for m in rx.finditer(text):
            frag = m.group(0).strip()
            if frag not in samples:
                samples.append(frag)
    return {"glossary_hits": len(samples), "glossary_samples": samples[:8]}


# POV persona markers — first-person singular subject pronouns vs a third-person
# proper-name subject at sentence start. We count per chapter and flag a book that
# MIXES personas across chapters (the Bab-2-reverts-to-3rd-person failure mode).
_POV_FIRST_RX = re.compile(r"(?<![\wÀ-ÿ])(aku|gue|gua|saya|ku)(?![\wÀ-ÿ])", re.IGNORECASE)
# English 1st-person: standalone capital "I" (+ contractions). Case-SENSITIVE so it does
# not match every 'i'. (bed-of-orchid part-2: Ch5 slipped to 1st-person in a 2nd-person book.)
_POV_FIRST_EN_RX = re.compile(r"(?<![\w'])(?:I|I'm|I've|I'll|I'd)(?![\w'])")
# 2nd-person narrative: "you/your" as a frequent subject (the Orchid pieces' register). A
# little dialogue-"you" noise is fine — the per-chapter dominant vote absorbs it.
_POV_SECOND_RX = re.compile(r"(?<![\w'])(?:[Yy]ou|[Yy]our|[Yy]ou're|[Yy]ourself|[Yy]ourselves)(?![\w'])")
# 3rd-person: a Capitalised name as the subject at the START of a sentence, followed by
# a lowercase verb (heuristic for "Arya mendengar", "Raka duduk"). Excludes dialogue.
_POV_THIRD_RX = re.compile(r"(?m)^\s*([A-Z][a-z]{2,})\s+([a-z]{3,})")
# Quoted-dialogue span — stripped before the persona tally so dialogue "you"/"I" doesn't
# swamp the NARRATIVE person (a heavy-dialogue 3rd-person book would otherwise false-read
# as 2nd/mixed — The Version He Loved dual-3rd regression, 2026-07-07).
_POV_DIALOGUE_STRIP_RX = re.compile(r"[\"“”«»][^\"“”«»]{0,400}?[\"“”«»]")


def pov_persona_drift_scan(text: str) -> dict[str, Any]:
    """Report-only (Track A): the fiction analogue of entity_consistency — lock ONE
    grammatical persona and scan for drift. Per chapter, tally first-person markers
    (aku/gue/saya/ku) vs third-person 'Name+verb' sentence openers, label the chapter's
    dominant persona, and flag when chapters DISAGREE (e.g. Bab 2 in 3rd-person while the
    rest is 1st-person — the SMA-2/Kuliah failure mode). Heuristic; Latin-script."""
    if not text:
        return {"chapters": 0, "personas": [], "drift": False, "dominant": None}
    parts = _CHAPTER_SPLIT_RX.split(text)
    chapters = parts[1:] if len(parts) > 1 else parts
    personas: list[str] = []
    for ch in chapters:
        if not ch or not ch.strip():
            continue
        # Strip quoted dialogue first — count the NARRATIVE person only, so a 3rd-person
        # book with heavy dialogue "you" isn't misread as 2nd-person (allows legit
        # break-delimited dual-POV like The Version He Loved to pass).
        cc = _POV_DIALOGUE_STRIP_RX.sub(" ", ch)
        # Tri-state: first (aku/gue/… + English "I"), second ("you"), third (Name+verb).
        first = len(_POV_FIRST_RX.findall(cc)) + len(_POV_FIRST_EN_RX.findall(cc))
        second = len(_POV_SECOND_RX.findall(cc))
        third = len(_POV_THIRD_RX.findall(cc))
        # ASYMMETRIC classification (dialogue already stripped, so these are narration):
        #  • 1st-person "I" is DEFINITIVE — a real 3rd-person narrator never narrates in "I",
        #    so substantial "I" ⟹ first, even when the narrator names a character ("Julian
        #    stands" trips the weak Name+verb 3rd heuristic). Fixes Long-and-Winding-Road.
        #  • 2nd-person "you" only wins if it clearly beats the Name+verb count (≥2×), so a
        #    gnomic "you" ("the way you know where north is") in a 3rd-person book doesn't
        #    flip it. Fixes The Version He Loved (dual-3rd with gnomic 'you').
        if first >= 3 and first >= second:
            personas.append("first")
        elif second >= 3 and second >= first and second >= max(1, third * 2):
            personas.append("second")
        elif third > 0:
            personas.append("third")
        elif first or second:
            personas.append("first" if first >= second else "second")
        else:
            personas.append("none")
    voted = [p for p in personas if p in ("first", "second", "third")]
    dominant = max(set(voted), key=voted.count) if voted else None
    # drift = >1 distinct dominant persona across chapters (e.g. a 2nd-person book with one
    # 1st-person chapter — bed-of-orchid part-2 Ch5), OR any single chapter reads 'mixed'.
    distinct = set(voted)
    drift = len(distinct) > 1 or ("mixed" in personas)
    return {
        "chapters": len(personas),
        "personas": personas,
        "dominant": dominant,
        "drift": bool(drift),
    }


# ── R-FG6b / Track A (sample-23): placeholder-leak + fiction stat-citation scans ──
# placeholder_leak_scan: residual "sekitar/kira-kira/kurang lebih + <non-number phrase>"
# in the FINAL text — a raw slot marker that R-FG6b's sentence-strip did not catch (e.g.
# a direct-generation hedge, not a [VERIFY] bracket). Report-only telemetry: tells us
# whether the root fix was sufficient or a direct-gen path also leaks.
_PLACEHOLDER_LEAK_RX = re.compile(
    r"(?i)\b(?:sekitar|kira-kira|kurang\s+lebih|kurleb)\s+"
    r"((?:yang\s+(?:relevan|sesuai|tepat|cocok)|(?:untuk|dalam)\s+konteks|"
    r"(?:judul|nama|contoh|daftar|jenis|kutipan|lirik|survei|studi|penelitian)\s+\w+)"
    r"[\w\s]{0,40})")


def placeholder_leak_scan(text: str) -> dict[str, Any]:
    """Report-only: detect a hedged descriptive-slot residue ("sekitar judul lagu yang
    relevan…", "sekitar survei psikologi…") that reached the final text. Not a value
    (no number follows the hedge) — an unfilled instruction the model never resolved."""
    if not text:
        return {"placeholder_leak_hits": 0, "placeholder_leak_samples": []}
    samples: list[str] = []
    for m in _PLACEHOLDER_LEAK_RX.finditer(text):
        frag = re.sub(r"\s+", " ", m.group(0).strip())[:80]
        if frag not in samples:
            samples.append(frag)
        if len(samples) >= 8:
            break
    return {"placeholder_leak_hits": len(samples), "placeholder_leak_samples": samples}


# fiction_stat_citation_scan: in a FICTION style, ANY statistic-citation pattern is a
# REGISTER violation (essay-voice leaking into story), NOT a fact to verify. Report-only;
# no verification/retrieval. The nonfiction fact-scan is unchanged and stays nonfiction-
# only — fiction is not fact-checked; its stat-CITATIONS are flagged for removal.
_FICTION_STYLES: frozenset = frozenset([
    "remaja_coming_of_age", "coming_of_age",
    "romance_contemporary", "romance",
    "kdrama_serial", "kdrama",
    "ironic_moral_fable", "moraliste",
    "storytelling", "bedtime_story", "pov_first_person_immersive",
])
_FICTION_STAT_RX = re.compile(
    r"(?i)(?:"
    r"\bmenurut\s+(?:\w+\s+){0,3}?(?:survei|penelitian|studi|riset|data|laporan|"
    r"jurnal|kajian|statistik)\b"
    r"|\b(?:survei|penelitian|studi|riset|statistik|data)\s+(?:\w+\s+){0,4}?"
    r"(?:menunjukkan|mencatat|menyebut(?:kan)?|membuktikan|memperkirakan|mengungkap)\b"
    r"|\b[Dd]ata\s+[A-Z][A-Za-z]{1,12}\b"
    r"|\b\d{1,3}\s*(?:persen|%)"
    r"|(?:\b(?:satu|dua|tiga|empat|lima|enam|tujuh|delapan|sembilan|sepuluh|puluh|"
    r"ratus|seratus)\b\s+){1,5}persen\b"
    r"|\b(?:satu|dua|tiga|empat|lima|enam|tujuh|delapan|sembilan)\s+dari\s+"
    r"(?:sepuluh|lima|empat|tiga|dua|\d+)\b"
    # English (sample-24 kdrama_serial): the same stat/data intrusions break fiction
    r"|\b\d{1,3}(?:\.\d+)?\s*(?:percent|per\s*cent|%)\b"
    r"|\baccording\s+to\s+(?:\w+\s+){0,3}?(?:a\s+|the\s+)?(?:survey|study|studies|research|"
    r"data|report|statistics|census|poll)\b"
    r"|\b(?:survey|study|studies|research|statistics|data|report|census|poll)\s+"
    r"(?:\w+\s+){0,3}?(?:shows?|found|finds?|reports?|suggests?|indicates?|reveals?|"
    r"estimates?|records?)\b"
    r"|\bthe\s+statistic\s+is\s+real\b|\bthe\s+math\s+is\s+not\s+subtle\b"
    r"|\bthe\s+numbers?\s+(?:don'?t\s+lie|speak\s+for|are\s+clear)\b"
    r"|\bthe\s+average\s+(?:\w+\s+){0,3}?(?:spends?|is|takes?|lasts?|earns?|works?)\b"
    r"|\b(?:one|two|three|four|five|six|seven|eight|nine|\d+)\s+(?:in|out\s+of)\s+"
    r"(?:ten|five|four|three|two|\d+)\b"
    r"|\b(?:hundred|thousand|million|billion)\s+(?:\w+\s+){0,2}?(?:visitors|people|users|"
    r"viewers|adults|teens|teenagers|couples|residents|citizens|respondents)\b"
    r")")


def fiction_stat_citation_scan(text: str, style: Optional[str] = None) -> dict[str, Any]:
    """Report-only (Track A): in a fiction-regime style, flag statistic-citations
    ('menurut survei…', 'Data KPAI…', '72 persen', 'tujuh dari sepuluh remaja') as a
    register violation. applies=False for non-fiction styles (scan skipped)."""
    style_key = (style or "").strip().lower()
    if style_key not in _FICTION_STYLES:
        return {"applies": False, "fiction_stat_hits": 0, "fiction_stat_samples": []}
    if not text:
        return {"applies": True, "fiction_stat_hits": 0, "fiction_stat_samples": []}
    samples: list[str] = []
    for m in _FICTION_STAT_RX.finditer(text):
        frag = re.sub(r"\s+", " ", m.group(0).strip())[:60]
        if frag not in samples:
            samples.append(frag)
        if len(samples) >= 8:
            break
    return {"applies": True, "fiction_stat_hits": len(samples),
            "fiction_stat_samples": samples}


# Genre self-reference (sample-24 + Flower of Evil): the narrator steps OUTSIDE the story —
# calls it 'the drama', addresses the audience ('you already know how this works'), or slips
# a meta-cinematic aside ('the camera lingers'). Cousin of the stat-coda. Report-only.
_GENRE_SELF_REF_RX = re.compile(
    r"(?i)(?:"
    r"\bthe\s+drama\s+(?:shoots?|cuts?|frames?|lingers?|shows?|opens?|gives?|likes?|would)\b"
    r"|\bthis\s+(?:drama|episode)\b"
    r"|\byou\s+already\s+know\s+(?:how|what|the|this)\b"
    r"|\bwe\s+(?:all\s+)?know\s+how\s+this\s+(?:works|goes|ends)\b"
    r"|\bevery\s+(?:good\s+)?(?:drama|romance|story)\s+(?:knows|has|needs|does)\b"
    r"|\bthe\s+(?:camera|screen|scene|frame)\s+(?:shoots?|cuts?|lingers?|holds?|pans?|frames?)\b"
    r"|\bcue\s+the\s+\w+"
    # Indonesian
    r"|\bkamu\s+(?:udah|sudah)\s+tau\s+(?:gimana|bagaimana|kok|caranya)\b"
    r"|\b(?:kayak|seperti)\s+(?:di\s+)?(?:sinetron|drama\s+korea|drakor)\b"
    r")"
)


def genre_self_reference_scan(text: str) -> dict[str, Any]:
    """Report-only (Track A): narrator stepping outside the fiction — calling the story
    'the drama', addressing the audience, or a meta-cinematic aside. sample-24 + Flower of
    Evil both broke on this. Cheap regex; wired for fiction regimes in gate_text."""
    if not text:
        return {"genre_self_ref_hits": 0, "genre_self_ref_samples": []}
    samples: list[str] = []
    for m in _GENRE_SELF_REF_RX.finditer(text):
        frag = re.sub(r"\s+", " ", m.group(0).strip())[:60]
        if frag not in samples:
            samples.append(frag)
        if len(samples) >= 8:
            break
    return {"genre_self_ref_hits": len(samples), "genre_self_ref_samples": samples}


# Aphorism-density scan (corpus sample-25). remaja register_spec caps aphorisms at ~1
# per 2 chapters (~3 for a 6-chapter book); sample-25 shipped ~10 and lens-1 explicitly
# flagged it. Signal = an ISOLATED short paragraph (a single-sentence maxim on its own
# line) using generalizing/abstract form ("X adalah Y", "tidak semua", "kadang X berarti")
# and NOT anchored to a scene (no dialogue quote, no character+action verb). Report-only.
_APHORISM_RX = re.compile(
    r"(?i)(?:"
    # "X adalah <abstract-noun>" — "Kenyamanan adalah cara paling sopan…"
    r"\b\w+\s+adalah\s+(?:cara|jenis|bentuk|bukti|hal|momen|salah\s+satu|proses|"
    r"kebiasaan|kondisi|rencana|kalimat|jawaban|pertanyaan|yang)\b"
    # "tidak semua X" / "not all X"
    r"|\b(?:tidak|nggak|gak|tak)\s+(?:semua|selalu)\s+\w+"
    r"|\bnot\s+all\s+\w+"
    # "kadang X berarti" / "sometimes X means"
    r"|\bkadang\s+\w+\s+berarti\b"
    r"|\bsometimes\s+\w+\s+means\b"
    # "yang paling X adalah/bukan/hanya…" / "the most X is/are…"
    r"|\byang\s+paling\s+\w+\s+(?:adalah|justru|bukan|hanya|cuma)\b"
    r"|\bthe\s+most\s+\w+\s+(?:is|are)\b"
    # "means to/nothing/everything/the …"
    r"|\bmeans?\s+(?:to|nothing|everything|the)\b"
    # Twin negation: "aku tidak pernah X. aku hanya (tidak pernah) Y."
    r"|\b(?:aku|dia|kau|ia|kita|kami)\s+(?:tidak|nggak|gak|tak)\s+pernah\s+\w+"
    r"[^.!?]{0,60}[.!?]\s+"
    r"(?:aku|dia|kau|ia|kita|kami)?\s*hanya\s+(?:tidak|nggak|gak|tak)?\s*(?:pernah\s+)?\w+"
    # "Yang tersisa/hilang/paling…" as paragraph opener
    r"|(?:^|\.\s+)yang\s+(?:tersisa|paling|jujur|hilang|penting|nyata|dalam)\b"
    # "X tidak datang tiba-tiba" / "sampai (ia|tiba-tiba) (menjadi|tidak…)" — sample-25 patterns
    r"|\btidak\s+datang\s+tiba-tiba\b"
    r"|\bsampai\s+(?:tiba-tiba|ia|dia|itu)\s+(?:menjadi|tidak|jadi|hilang|selesai|habis|berubah)\b"
    # "X bukan Y" as paragraph opener with hanya/melainkan follow-through
    r"|(?:^|\n)\s*\w+\s+bukan\b[^.!?\n]{0,80}[.!?]\s*(?:hanya|cuma|melainkan|itu\s+cuma)\b"
    # English literary-maxim forms (narasi-3 "Long and Winding Road", 2026-07-06):
    # short isolated gnomic sentences the id-centric branches above never matched
    # ("Cowardice has two hands.", "Ambition is just loneliness with a schedule.",
    # "Some walls hold better when you stop pretending they're yours alone.").
    r"|\bwe\s+always\s+\w+"
    r"|\b\w+\s+is\s+(?:the\s+only|just|not\s+the\s+same\s+as|always\s+the|never\s+the|only\s+ever)\b"
    r"|\bit\s+takes\s+\w+\s+\w+\s+to\b"
    r"|\bsome\s+\w+\s+(?:hold|holds|are|is|do|does|keep|keeps|remember|remembers|"
    r"last|lasts|stay|stays|need|die|grow|grows|carry|know|knows|leave|break|fall|"
    r"come|go|matter|remain|survive|forget|built|make|makes|mean|means|refuse|"
    r"refuses|wait)\b"
    r"|\b\w+\s+has\s+(?:two|three|four|no|only\s+one|its\s+own)\s+\w+"
    r"|\b(?:silence|debt|ambition|cowardice|grief|love|memory|shame|hope|fear|"
    r"truth|the\s+truth)\s+(?:is|has|does|never|always|only|grows|costs|keeps|waits)\b"
    r")"
)
# Quote characters (dialogue), including curly quotes and Indonesian-typographic pairs
_DIALOGUE_QUOTE_RX = re.compile(r"[\"“”«»]")
# A character-name + action verb (scene anchor) — "Rendra bilang", "Dia mengangguk", "he said"
_CHARACTER_ACTION_RX = re.compile(
    r"\b(?:[A-Z][a-z]{2,}|[Aa]ku|[Ii]a|[Dd]ia|[Mm]ereka|[Kk]ami|[Kk]ita|[Gg]ue|[Gg]ua|"
    r"[Ll]u|[Ee]lu|[Ss]aya|[Kk]au|[Hh]e|[Ss]he|[Tt]hey|[Ww]e|[II])\s+"
    r"(?:said|asked|replied|thought|walked|sat|stood|looked|took|opened|closed|smiled|"
    r"nodded|shook|turned|leaned|whispered|shouted|kata|tanya|bilang|jawab|balas|lihat|"
    r"liat|duduk|berdiri|senyum|angguk|tersenyum|menoleh|melihat|mengangguk|berkata|"
    r"menjawab|bertanya|memandang|melangkah|mundur|maju|kirim|naruh|ketuk|geser|nulis|"
    r"buka|tutup|ambil|kasih|beli|jalan|pulang|masuk|keluar|tarik|tunggu|nunggu|dengar)\b"
)


def aphorism_density_scan(text: str, style: Optional[str] = None) -> dict[str, Any]:
    """Report-only (Track A, corpus sample-25): count ISOLATED aphoristic paragraphs —
    short single-sentence maxims that sit on their own line, use generalizing form, and
    don't anchor to scene (no dialogue, no character+action). Fiction-regime only (the
    non-fiction essays legitimately state maxims). Sample-25 (persahabatan SMA memudar,
    coming_of_age) shipped ~10 in 6 chapters; register_spec caps at ~3.
    Returns rate_per_10_chapters so a value is comparable across book lengths."""
    style_key = (style or "").strip().lower()
    if style_key not in _FICTION_STYLES:
        return {"applies": False, "aphorism_hits": 0, "aphorism_samples": [],
                "aphorism_rate_per_10_chapters": 0.0}
    if not text:
        return {"applies": True, "aphorism_hits": 0, "aphorism_samples": [],
                "aphorism_rate_per_10_chapters": 0.0}
    paras = re.split(r"\n\s*\n", text)
    samples: list[str] = []
    for p in paras:
        p = p.strip()
        if not p or len(p) < 30 or len(p) > 260:
            continue
        # skip chapter headers + metadata / list lines
        if re.match(r"(?i)^(?:bab|chapter|hoofdstuk|cap[íi]tulo|kapitel|chapitre|gaya|"
                    r"style|output|bahasa|language)\b", p):
            continue
        if _DIALOGUE_QUOTE_RX.search(p):
            continue
        if _CHARACTER_ACTION_RX.search(p):
            continue
        sents = re.split(r"[.!?…]\s+", p)
        if len(sents) > 3:
            continue
        if not _APHORISM_RX.search(p):
            continue
        samples.append(p if len(p) <= 120 else p[:117] + "…")
        if len(samples) >= 20:
            break
    # rate: hits per 10 chapters (uses _CHAPTER_SPLIT_RX from below in the file).
    n_ch = 0
    try:
        parts = _CHAPTER_SPLIT_RX.split(text)
        n_ch = max(0, len(parts) - 1)
    except Exception:  # noqa: BLE001
        pass
    n_ch = n_ch or 6   # default assumption for a book with no detected chapter marks
    rate = round(10.0 * len(samples) / n_ch, 2)
    return {"applies": True, "aphorism_hits": len(samples),
            "aphorism_samples": samples[:10],
            "aphorism_rate_per_10_chapters": rate}


# Phonetic-collision scan (corpus sample-25 lens-#4). Detects titled proper-noun pairs
# that are DIFFERENT characters but PHONETICALLY confusable — in TTS output a listener
# may hear them as the same entity drifting. Sample-25 surfaced "Pak Harto" (Fisika,
# Bab 1) vs "Pak Hendra" (Matematika, Bab 3): different subjects, both teachers, but
# Pak H+cluster reads as drift. Signal = same honorific title + same first letter after
# title + length within 1. Narrow by design (high precision). Fiction only — nonfiction
# may legitimately name similar historical figures (Louis XIII/XIV in one passage).
_TITLE_HONORIFIC_RX = re.compile(
    r"\b(Pak|Bu|Mas|Mbak|Kak|Om|Tante|Bapak|Ibu|Ustadz|Ustadzah|Kiai|"
    r"Mr\.?|Mrs\.?|Ms\.?|Miss|Dr\.?|Sir|Lady|Prof\.?|"
    r"Ajusshi|Ajumma|Oppa|Unni|Sunbae|Hoobae|Eomma|Appa|Halmoni|Harabeoji)"
    r"\s+([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})?)"
)


def phonetic_collision_scan(text: str, style: Optional[str] = None) -> dict[str, Any]:
    """Report-only (Track A, corpus sample-25 lens-#4): flag titled proper-noun pairs
    that share honorific + first letter + similar length — VO listeners hear them as
    the same character drifting. Fiction only. Narrow: catches Pak Harto ~ Pak Hendra
    (the corpus case); does NOT touch nonfiction (may legitimately name Louis XIII/XIV)
    and does NOT scan bare names (higher false-positive risk — start narrow, broaden if
    corpus demands)."""
    style_key = (style or "").strip().lower()
    if style_key not in _FICTION_STYLES:
        return {"applies": False, "phonetic_collision_hits": 0,
                "phonetic_collision_pairs": []}
    if not text:
        return {"applies": True, "phonetic_collision_hits": 0,
                "phonetic_collision_pairs": []}
    by_title: dict = {}
    for m in _TITLE_HONORIFIC_RX.finditer(text):
        title = m.group(1).strip(".").lower()
        name = m.group(2).strip()
        # first-word only, so "Pak Harto Setya Wibowo" and "Pak Harto" collapse
        name_key = name.split()[0]
        by_title.setdefault(title, set()).add(name_key)
    pairs: list = []
    seen = set()
    for title, names in by_title.items():
        distinct = sorted(names)
        if len(distinct) < 2:
            continue
        for i in range(len(distinct)):
            a = distinct[i]
            for j in range(i + 1, len(distinct)):
                b = distinct[j]
                key = (title, a, b)
                if key in seen:
                    continue
                seen.add(key)
                if a[0].lower() != b[0].lower():
                    continue
                if abs(len(a) - len(b)) > 1:
                    continue
                pairs.append(f"{title.capitalize()} {a} / {title.capitalize()} {b}")
                if len(pairs) >= 8:
                    break
            if len(pairs) >= 8:
                break
        if len(pairs) >= 8:
            break
    return {"applies": True, "phonetic_collision_hits": len(pairs),
            "phonetic_collision_pairs": pairs}


# ── Duplicate-sentence + lexical merge-corruption scanners (narasi-3 "Long and
#    Winding Road" lens synthesis, 2026-07-06). Both report-only, style-agnostic,
#    dictionary-free. They surface two classes the aphorism/entity scanners
#    structurally cannot see:
#      (1) a whole sentence repeated verbatim, or a long contiguous phrase recurring
#          at distant positions (L53==L87 "Debt is the only thing…"; the reused
#          simile "like a stone dropped into still water" L137/L189).
#      (2) a lexical merge-corruption — a short standalone-word prefix fused to a
#          non-word remainder ("of white" → "ofite" L343), which voices as a nonsense
#          token in TTS.
_SENT_SPLIT_RX = re.compile(r"[.!?…]+[\s\"“”'’)]*\s+|\n+")
_DUP_WORD_RX = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")


def _normalize_sentence(s: str) -> str:
    return " ".join(_DUP_WORD_RX.findall(s.lower()))


def duplicate_sentence_scan(text: str, ngram: int = 7) -> dict[str, Any]:
    """Report-only: verbatim whole-sentence repeats (>=5 words) + long contiguous
    n-gram repeats at distant positions. Short refrains / epizeuxis (<5-word
    sentences, <ngram-word phrases) are intentionally NOT flagged so a deliberate
    bookend ("The corridor is narrow.") survives."""
    if not text:
        return {"duplicate_sentence_hits": 0, "duplicate_sentence_samples": []}
    samples: list[str] = []
    sent_norms: list[str] = []
    raw_sents = [s.strip() for s in _SENT_SPLIT_RX.split(text) if s.strip()]
    counts: dict[str, int] = {}
    first: dict[str, str] = {}
    for s in raw_sents:
        n = _normalize_sentence(s)
        if len(n.split()) < 5:
            continue
        counts[n] = counts.get(n, 0) + 1
        first.setdefault(n, s)
    for n, c in counts.items():
        if c >= 2:
            sent_norms.append(n)
            samples.append(f"×{c} verbatim: “{first[n][:80]}”")
    words = _DUP_WORD_RX.findall(text.lower())
    positions: dict[str, list[int]] = {}
    for i in range(len(words) - ngram + 1):
        gram = " ".join(words[i:i + ngram])
        positions.setdefault(gram, []).append(i)
    for gram, pos in positions.items():
        if len(pos) < 2 or pos[-1] - pos[0] < ngram * 2:
            continue
        if any(gram in n for n in sent_norms):   # already reported as a verbatim dup
            continue
        samples.append(f"×{len(pos)} phrase: “{gram[:80]}”")
        if len(samples) >= 10:
            break
    return {"duplicate_sentence_hits": len(samples),
            "duplicate_sentence_samples": samples[:10]}


# ── Chapter-heading REPAIR (narasi-bed-of-orchid part-2, 2026-07-06). Deterministic +
#    SAFE mutation — only touches heading-shaped spans, a clean book is byte-identical.
#    Fixes two things the line-anchored _retrofit_legacy_chapter_labels misses:
#      (1) a heading FUSED mid-line (the model bled "## Bab 4: ..." onto a sentence, because
#          static.py used to emit "## Bab N" for every language and that taught the model the
#          format) → split it onto its own line;
#      (2) a WRONG-LANGUAGE chapter word ("## Bab 4" in an English book) → relabel to lang.
#    Unlike a report-only scanner this REPAIRS, so the reader-visible defect never ships. ──
_CH_WORDS = "Bab|Chapter|Cap[íi]tulo|Kapitel|Chapitre|Hoofdstuk|Capitolo"
_FUSED_HEADING_RX = re.compile(r"(?<=\S)([ \t]*)(#{1,4}[ \t]*(?:" + _CH_WORDS + r")[ \t]*\d+)")
_CH_HEADING_LINE_RX = re.compile(r"(?im)^([ \t]*#{1,4}[ \t]*)(" + _CH_WORDS + r")([ \t]*)(\d+)(.*)$")
_LANG_CH_WORD = {"id": "Bab", "ms": "Bab", "jv": "Bab", "su": "Bab", "en": "Chapter",
                 "es": "Capítulo", "fr": "Chapitre", "de": "Kapitel", "pt": "Capítulo",
                 "nl": "Hoofdstuk", "it": "Capitolo"}
_CH_WORD_SET = frozenset(("Bab", "Chapter", "Capítulo", "Capitulo", "Kapitel",
                          "Chapitre", "Hoofdstuk", "Capitolo"))


def chapter_heading_repair(text: str, lang: str = "en") -> tuple[str, int]:
    """Split fused chapter headings onto their own line + relabel a wrong-language chapter
    word to `lang`. Returns (repaired_text, n_repairs). Never raises."""
    if not text:
        return text, 0
    n = [0]

    def _split(m):
        n[0] += 1
        return "\n\n" + m.group(2)
    text = _FUSED_HEADING_RX.sub(_split, text)
    code = str(lang or "en").strip().lower().replace("_", "-").split("-", 1)[0]
    want = _LANG_CH_WORD.get(code)
    if want:
        def _relabel(m):
            cur = m.group(2)
            if cur != want and cur in _CH_WORD_SET:
                n[0] += 1
                return f"{m.group(1)}{want}{m.group(3)}{m.group(4)}{m.group(5)}"
            return m.group(0)
        text = _CH_HEADING_LINE_RX.sub(_relabel, text)
    return text, n[0]


# ── '-ite' merge-corruption detector (narasi-3 'ofite'←'of white'; bed-of-orchid part-2
#    'mostlyite'←'mostly laterite'; kintsugi 'powderedite'←'powdered [material]'). REPORT-only
#    by necessity: the dropped letters are UNRECOVERABLE, so the token can't be deterministically
#    repaired — but it CAN be caught precisely. Signal: a token ending in 'ite' that is NOT a
#    real English -ite word (checked against a bundled 3k-word -ite dictionary). This subsumes
#    the earlier prefix-restricted heuristic, which caught 'ofite'/'mostlyite' (function/adverb
#    prefixes) but MISSED 'powderedite' (content-word prefix 'powdered' — not enumerable). The
#    dictionary excludes every real -ite word (granite/opposite/favorite/graphite/website/
#    expedite/laterite/hematite …) so those never flag. Proper nouns are skipped (case check)
#    to avoid FP on names/places. The frozenset _MERGE_ITE_PREFIXES remains as the graceful
#    fallback if the bundled gz can't be loaded at import (path/deploy safety). ──
_MERGE_ITE_PREFIXES = frozenset((
    "of", "to", "in", "on", "at", "as", "is", "it", "or", "an", "be", "no", "so", "we",
    "he", "up", "by", "and", "the", "all", "my", "mostly", "only", "really", "simply",
    "nearly", "partly", "just", "almost", "fully", "barely", "hardly", "truly", "merely",
    "purely", "largely", "mainly", "half", "quite"))
_ITE_TOKEN_RX = re.compile(r"\b([a-z]{2,})ite\b", re.I)


def _load_ite_words() -> set[str] | None:
    """Bundled real-'-ite'-word dictionary (words ending in 'ite', len>=5). None ⟹ load
    failed ⟹ merge_fusion_scan degrades to the prefix-restricted heuristic. Fail-safe: any
    exception (missing file, bad gzip) returns None rather than raising at import."""
    try:
        import gzip
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ite_words.txt.gz")
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            words = {ln.strip() for ln in fh if ln.strip()}
        return words or None
    except Exception:
        return None


_ITE_REAL_WORDS = _load_ite_words()


def merge_fusion_scan(text: str) -> dict[str, Any]:
    """Report-only: catch the '-ite' letter-loss fusion class precisely (can't auto-repair —
    the dropped letters are gone). Flags 'ofite'/'mostlyite'/'powderedite'; ignores every real
    -ite word via the bundled dictionary. Falls back to the function-word prefix set if the
    dictionary is unavailable."""
    if not text:
        return {"merge_fusion_hits": 0, "merge_fusion_samples": []}
    samples: list[str] = []
    seen: set[str] = set()
    for m in _ITE_TOKEN_RX.finditer(text):
        raw = m.group(0)
        if raw[:1].isupper():          # skip proper nouns (names/places) → no FP
            continue
        pre = m.group(1).lower()
        tok = pre + "ite"
        if tok in seen:
            continue
        if _ITE_REAL_WORDS is not None:
            if len(tok) < 5 or tok in _ITE_REAL_WORDS:   # real -ite word ⟹ clean
                continue
        elif pre not in _MERGE_ITE_PREFIXES:             # fallback: prefix heuristic
            continue
        seen.add(tok)
        samples.append(f"{tok} (letter-loss fusion — not a real '-ite' word)")
        if len(samples) >= 8:
            break
    return {"merge_fusion_hits": len(samples), "merge_fusion_samples": samples}


# ── English instruction-residue / unresolved-placeholder scanner (narasi "The Version He
#    Loved", 2026-07-07). The generator hides unfilled template slots behind hedge-words —
#    "around appropriate Joseon-era genre painter", "around 8-12 billion", "around Jeongjo or
#    Sunjo", "from arrival or from today", "[name of district]". These survive the fill step
#    and are ship-blockers for book output (2 of 7 corpus pieces hit them). Report-only,
#    high-precision: verified 0 FP on the clean literary pieces. The Indonesian cousin
#    (sekitar/kira-kira + slot) is placeholder_leak_scan; this is the English pattern.
#    Widened 2026-07-15 (Tier 1a): the adjective/noun lists above missed two confirmed prod
#    leaks — "the around major Seoul daily with investigative desk, specify name newspaper"
#    and "around address held by the Seoul Family Court...". Added: (a) around+adjective
#    (major/primary/nearby/etc, no article in between — "around a major X" stays unflagged,
#    only the ungrammatical article-less "around major X" fires) + a wider noun tail incl.
#    address/location/publication/outlet/newspaper; (b) bare "around address/residence/
#    location" (no adjective — the hedge sits directly on the slot noun, same shape as
#    "around 8-12 billion"); (c) "specify name/title/date/..." — a bare instruction verb
#    immediately touching its (unresolved) object noun. Verified 0 FP against "she looked
#    around the room" / "revolved around major office politics" / "around a major heist".
#    NOTE (as originally written 2026-07-15): pattern (a)'s gap between the adjective and
#    the noun tail was capped at 4 words with no comma in the separator class, so it did
#    NOT actually reach across "major Seoul daily with investigative desk, specify name
#    newspaper" (7 filler words incl. a comma) — pattern (c) below was the one that caught
#    that leak string, via its own "specify name" match, purely by coincidence. Corrected
#    2026-07-16 (adversarial audit, Tier 1b): pattern (a)'s gap widened to {0,10} words and
#    its separator class widened to cross a comma, so it now reaches this leak directly
#    (see below). Patterns (b)/(c) were ALSO tightened same pass — the bare "around
#    address/residence/location" and "specify name/title/..." shapes fired on ordinary
#    prose ("around residence halls", "specify address and date of birth"); they now only
#    match the leaked shape (hedge noun at a clause boundary — end-of-clause punctuation,
#    "held", "by", or end of string), not any bare occurrence of the noun. ──
_INSTRUCTION_RESIDUE_PATS = [
    re.compile(r"\baround\s+(?:appropriate|relevant|suitable|the\s+relevant|a\s+suitable|an?\s+appropriate)\b", re.I),
    re.compile(r"\b(?:appropriate|relevant|suitable)\s+(?:\w+[-\s]){0,4}?(?:painter|artist|period|era|name|names|"
               r"district|neighbou?rhood|figure|place|region|dynasty|master|value|amount|price|character|song|city|street)\b", re.I),
    re.compile(r"\baround\s+\d+\s*[-–—]\s*\d+\b"),
    re.compile(r"\b(?:around|during|circa|about)\s+[A-Z][a-z]+\s+or\s+[A-Z][a-z]+\b"),
    # Article-less only (2026-07-16, caught while regression-testing the pattern-(a) split
    # above): the optional "(?:the\s+|an?\s+)?" article let this match ordinary grammatical
    # English ("the name of the street") identically to the actual leak shape ("name of
    # district" — a raw unfilled slot marker, which by definition has no article). Same
    # article-less-is-the-tell design already used for the "around major X" patterns above.
    re.compile(r"\bname\s+of\s+(?:district|neighbou?rhood|painter|artist|place|character|person|"
               r"figure|city|street|region|company|brand|song|dynasty)\b", re.I),
    re.compile(r"\[[^\]\n]{2,50}\]"),
    re.compile(r"\b(?:TODO|FIXME|PLACEHOLDER|TBD)\b"),
    re.compile(r"\bfrom\s+\w+\s+or\s+from\s+\w+\b", re.I),
    # (a) SPLIT 2026-07-16 (2nd adversarial audit pass): the prior single pattern widened
    # its gap to {0,10}+comma-crossing to reach the confirmed leak ("...major Seoul daily
    # with investigative desk, specify name newspaper"), but that same wide gap then
    # false-positived on ordinary prose whenever ANY of ~20 generic nouns (city/street/
    # name/price/place/etc — common words with no special connection to a hedge) turned up
    # within 10 words/a comma of an "around <adjective>" opener ("walked around major
    # landmarks in the old quarter, sketching the buildings on every street" -> false hit
    # via "street"). Fix: two patterns instead of one. (a-narrow) keeps the ORIGINAL tight
    # {0,4}-word, no-comma gap for the generic noun tail — these words are too common to
    # risk a wide reach. (a-wide) keeps the {0,10}+comma-crossing gap, but ONLY for the
    # narrow, leak-specific noun tail (address/location/publication/outlet/newspaper) that
    # motivated the widening in the first place — uncommon enough in this shape that a
    # wider reach doesn't pick up unrelated prose.
    re.compile(r"\baround\s+(?:major|primary|leading|prominent|nearby|large|well-known|well\s+known|"
               r"small|minor|popular|respected|top)\s+(?:\w+[-\s]){0,4}?(?:painter|artist|period|era|"
               r"name|names|district|neighbou?rhood|figure|place|region|dynasty|master|value|amount|"
               r"price|character|song|city|street)\b", re.I),
    re.compile(r"\baround\s+(?:major|primary|leading|prominent|nearby|large|well-known|well\s+known|"
               r"small|minor|popular|respected|top)\s+(?:\w+[-,\s]+){0,10}?(?:address|location|"
               r"publication|outlet|newspaper)\b", re.I),
    # (b)/(c) tightened 2026-07-16 (adversarial audit): a bare "around address/residence/
    # location" or "specify name/title/..." fires on ordinary prose ("around residence
    # halls", "around location scouting for...", "specify address and date of birth",
    # "specify name and title on the form") — real collocations, not leaked hedges. The
    # leaked shape has the slot noun sitting alone at a clause boundary ("around address
    # held by...", "...desk, specify name" — no sensible word follows). Require the noun
    # be followed by end-of-clause: whitespace+punctuation, "held", "by", or end of string.
    # RETIGHTENED 2026-07-16 (2nd adversarial audit pass): a comma/semicolon after the
    # slot noun is exactly the shape of ordinary enumeration/list phrasing ("specify name,
    # rank, and serial number", "around location; the trail went cold") — dropped from the
    # clause-end class, keeping only unambiguous sentence-enders (. ! ?), end-of-string,
    # or the "held"/"by" continuation (neither leak string needs a comma/semicolon to match).
    re.compile(r"\baround\s+(?:address|residence|location)\b(?=\s*(?:[.!?]|$)|\s+(?:held|by)\b)", re.I),
    re.compile(r"\bspecify\s+(?:name|title|date|address|location|number|amount|price|value)\b"
               r"(?=\s*(?:[.!?]|$)|\s+(?:held|by)\b)", re.I),
]


def instruction_residue_scan(text: str) -> dict[str, Any]:
    """Report-only: unresolved English template slots hiding behind hedge-words. High-
    precision — a legit approximate ('around ten minutes') is NOT flagged; only slot-shaped
    residue (instruction adjective + slot noun, digit-range hedge, unresolved Proper-or-
    Proper / either-or, name-of-X, bracket, TODO)."""
    if not text:
        return {"instruction_residue_hits": 0, "instruction_residue_samples": []}
    samples: list[str] = []
    seen: set[str] = set()
    for rx in _INSTRUCTION_RESIDUE_PATS:
        for m in rx.finditer(text):
            frag = re.sub(r"\s+", " ", m.group(0)).strip()
            key = frag.lower()
            if key in seen:
                continue
            seen.add(key)
            samples.append(frag[:80])
            if len(samples) >= 10:
                return {"instruction_residue_hits": len(samples),
                        "instruction_residue_samples": samples}
    return {"instruction_residue_hits": len(samples), "instruction_residue_samples": samples}


def _unattributed_voice_scan_on() -> bool:
    """NARASI_UNATTRIBUTED_VOICE_SCAN=1 arms unattributed_voice_scan below. Default OFF
    ⟹ scan never runs ⟹ report/stats byte-identical, matching every other gate in this
    file (see e.g. _refrain_scan_on in narasi_counters.py for the same convention)."""
    return os.environ.get("NARASI_UNATTRIBUTED_VOICE_SCAN", "0").strip().lower() in ("1", "true", "yes", "on")


# ── Unattributed collective-voice leak (narasi manuscript S2-Th-6, review flag,
#    2026-07-16). A chapter closed on an unquoted, unattributed second-person/first-
#    person-plural COLLECTIVE-voice sentence that breaks otherwise-strict close-third
#    POV with no clear speaker — "You call it administrative efficiency; we call it an
#    eviction with a cleaner pen." — reading like a stray manifesto/pull-quote fragment
#    leaking into prose rather than a deliberate character line; never happens elsewhere
#    in that manuscript. Report-only, deterministic, modeled stylistically on
#    narasi_counters._unattributed_expert_scan (never-raise, PASS/FLAG dict, capped
#    samples). Conservative by design — false positives on ordinary narration are worse
#    than missing some cases — so a sentence only flags when ALL of: (a) it carries no
#    quotation-mark character at all (any quote is treated as possibly-attributed
#    dialogue and skipped, even if the tag sits outside the quoted span); (b) it uses a
#    collective you/your/we/our pronoun; AND (c) it has a rhetorical antithesis shape —
#    opens with "You/We call it…" / "You/We say…" framing AND the OPPOSING pronoun side
#    also appears later in the sentence, OR a semicolon- or vs/versus-joined clause pairs
#    a you/your side against a we/our side where BOTH clauses share the same rhetorical
#    framing verb ("call it"/"say"). Plain second-person POV narration ("You could see
#    the harbor from the ridge"), quoted dialogue ("You know what I mean," she said), a
#    bare framing opener with no opposing clause ("We say the harvest was good this
#    year"), and an ordinary paired-observation sentence that merely mixes pronoun
#    families without a shared framing verb ("You could see the boats; we watched from
#    the porch") all carry no such antithesis shape and are NOT flagged (2026-07-16,
#    tightened after an adversarial audit caught all three as false positives in the
#    first version). Gated behind NARASI_UNATTRIBUTED_VOICE_SCAN (default OFF). ──
# QUOTE_CHARS retightened 2026-07-16 (adversarial audit): the plain ASCII/curly apostrophe
# was matching mid-word contractions/possessives ("it's", "you're"), silently exempting
# most contraction-bearing target sentences as "possibly-attributed dialogue" even though
# no actual dialogue quote was present. Only treat ' / ‘ / ’ as a quote DELIMITER — not
# flanked by a word character on both sides — leaving genuine 'quoted' dialogue exempted
# while contractions/possessives no longer are.
_UV_QUOTE_CHARS_RX = re.compile(r"[\"“”]|(?<!\w)[\'‘’]|[\'‘’](?!\w)")
_UV_COLLECTIVE_PRON_RX = re.compile(r"\b(?:you|your|we|our)\b", re.I)
_UV_FRAMING_RX = re.compile(r"^(?:you|we)\s+(?:call\s+it|say)\b", re.I)
_UV_YOU_SIDE_RX = re.compile(r"\b(?:you|your)\b", re.I)
_UV_WE_SIDE_RX = re.compile(r"\b(?:we|our)\b", re.I)
_UV_VERSUS_RX = re.compile(r"\bversus\b|\bvs\.?\b", re.I)
# Shared rhetorical-frame verb required on BOTH sides of a semicolon/versus split before
# treating mixed you/we pronouns as an antithesis (added 2026-07-16, adversarial audit) —
# see the two false-positive classes documented in unattributed_voice_scan's docstring.
_UV_ANTITHESIS_VERB_RX = re.compile(r"\bcall(?:s|ing|ed)?\s+it\b|\bsay(?:s|ing)?\b|\bsaid\b", re.I)


def unattributed_voice_scan(text: str) -> dict:
    """Report-only: an unattributed, unquoted collective-voice (you/we) sentence with a
    rhetorical antithesis shape — a stray manifesto/pull-quote fragment breaking close-
    third POV. status FLAG/PASS. Never raises."""
    out = {"status": "PASS", "count": 0, "samples": []}
    try:
        hits: list[str] = []
        for para in (text or "").split("\n"):
            for s in re.split(r"(?<=[.!?])\s+", para):
                s = s.strip()
                if not s or not _UV_COLLECTIVE_PRON_RX.search(s):
                    continue
                if _UV_QUOTE_CHARS_RX.search(s):
                    continue  # a real quote char — possibly-attributed dialogue, skip
                antithesis = False
                fm = _UV_FRAMING_RX.match(s)
                if fm:
                    # Opener matched ("You call it…"/"We say…") — only an antithesis if
                    # the OPPOSING pronoun side also appears later in the sentence; a bare
                    # opener with nothing to contrast ("We say the harvest was good this
                    # year") must NOT flag (audit-caught false positive, 2026-07-16).
                    rest = s[fm.end():]
                    opener_is_you = s[:3].lower().startswith("you")
                    antithesis = bool(
                        _UV_WE_SIDE_RX.search(rest) if opener_is_you
                        else _UV_YOU_SIDE_RX.search(rest)
                    )
                if not antithesis:
                    parts = None
                    if ";" in s:
                        parts = s.split(";", 1)
                    elif _UV_VERSUS_RX.search(s):
                        parts = _UV_VERSUS_RX.split(s, 1)
                    if parts and len(parts) >= 2:
                        left, right = parts[0], parts[1]
                        # Require a shared rhetorical framing verb on BOTH clauses, not
                        # just bare you/we pronoun presence on each side — an ordinary
                        # paired-observation sentence ("You could see the boats gliding
                        # by; we watched from the porch") merely mixes pronoun families
                        # without any rhetorical contrast (audit-caught, 2026-07-16).
                        if _UV_ANTITHESIS_VERB_RX.search(left) and _UV_ANTITHESIS_VERB_RX.search(right) and (
                            (_UV_YOU_SIDE_RX.search(left) and _UV_WE_SIDE_RX.search(right)) or
                            (_UV_WE_SIDE_RX.search(left) and _UV_YOU_SIDE_RX.search(right))
                        ):
                            antithesis = True
                if antithesis:
                    hits.append(s[:200])
        out["count"] = len(hits)
        if hits:
            out["status"] = "FLAG"
            out["samples"] = hits[:10]
        return out
    except Exception:  # noqa: BLE001
        return {"status": "PASS", "count": 0, "samples": []}


# ── Number-format consistency (narasi "The Version He Loved", 2026-07-07). Report-only. A
#    single piece rendered the SAME 42-billion figure three ways — "forty-two billion won"
#    (spelled), "8-12 billion" (digits), "₩42 billion" (symbol). Scoped to LARGE-MAGNITUDE
#    currency (billion/million/trillion) so ordinary small numbers ("three works", "nine
#    hours") never trip it; flags only when ≥2 render styles co-occur. ──
_NUM_WORD = (r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
             r"fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|"
             r"fifty|sixty|seventy|eighty|ninety|hundred)")
_NUM_MAG = r"(?:billion|million|trillion)"
_NUM_STYLE_RX = {
    "spelled": re.compile(r"\b" + _NUM_WORD + r"(?:[-\s]" + _NUM_WORD + r")*\s+" + _NUM_MAG + r"\b", re.I),
    "digits":  re.compile(r"(?<![\w$₩€£¥])\d[\d,.]*(?:\s*[-–—]\s*\d[\d,.]*)?\s+" + _NUM_MAG + r"\b", re.I),
    "symbol":  re.compile(r"[₩$€£¥]\s?\d[\d,.]*\s*" + _NUM_MAG + r"?\b"),
}


def number_format_scan(text: str) -> dict[str, Any]:
    """Report-only: flag when a piece mixes ≥2 large-magnitude/currency render styles
    (spelled / digits / currency-symbol) for the same class of figure."""
    if not text:
        return {"number_format_styles": 0, "number_format_samples": []}
    found: dict[str, str] = {}
    for label, rx in _NUM_STYLE_RX.items():
        m = rx.search(text)
        if m:
            found[label] = re.sub(r"\s+", " ", m.group(0).strip())[:40]
    if len(found) < 2:
        return {"number_format_styles": 0, "number_format_samples": []}
    return {"number_format_styles": len(found),
            "number_format_samples": [f"{k}: {v}" for k, v in found.items()]}


# ── Korean-ceramics fact-check (narasi "The Version He Loved" lens, 2026-07-07). Report-only,
#    high-precision domain list for the K-drama / Korean-art lane. "celadon moon jar" is a
#    category error the expert protagonist would never make — a moon jar (달항아리) is white
#    porcelain (baekja); celadon (청자) is Goryeo green-glazed ware. Only clear cross-ware
#    contradictions fire; a correct piece is untouched. ──
_CERAMICS_ERRORS = [
    (re.compile(r"celadon\s+moon\s+jar", re.I),
     "moon jar (달항아리) is white porcelain (baekja), not celadon (Goryeo green ware)"),
    (re.compile(r"(?:goryeo|gory[oŏ])\s+moon\s+jar", re.I),
     "moon jars are Joseon-era white porcelain, not Goryeo"),
    (re.compile(r"blue[-\s]and[-\s]white\s+celadon", re.I),
     "blue-and-white is porcelain (cheonghwa baekja), not celadon"),
    (re.compile(r"celadon\s+(?:baekja|white\s+porcelain)", re.I),
     "celadon and white porcelain (baekja) are different wares"),
    (re.compile(r"white[-\s]porcelain\s+celadon", re.I),
     "white porcelain and celadon are different wares"),
]


def ceramics_factcheck_scan(text: str) -> dict[str, Any]:
    """Report-only: flag clear Korean-ceramics category errors (celadon vs white porcelain
    vs blue-and-white; Goryeo vs Joseon moon jar)."""
    if not text:
        return {"ceramics_error_hits": 0, "ceramics_error_samples": []}
    hits: list[str] = []
    for rx, why in _CERAMICS_ERRORS:
        m = rx.search(text)
        if m:
            frag = re.sub(r"\s+", " ", m.group(0).strip())
            hits.append(f"“{frag}” — {why}")
    return {"ceramics_error_hits": len(hits), "ceramics_error_samples": hits[:5]}


# R-FG11 narrator-opening formulas: canned rhetorical openers per language.
# Extend by adding new lang keys; each value is a list of anchored regex patterns.
_NARRATOR_OPENING_FORMULAS: dict[str, list[str]] = {
    "nl": [r"^\s*Sta ons toe\b", r"^\s*Beschouw,?\s+als u wilt\b"],
    "en": [r"^\s*Allow (us|me) to introduce\b", r"^\s*Consider,?\s+if you will\b"],
}

# Chapter-heading splitter (permissive multi-lang): Dutch, English, Indonesian,
# Spanish/Portuguese, German, French, CJK numbered chapter markers.
_CHAPTER_SPLIT_RX = re.compile(
    r"(?im)^\s*(?:Hoofdstuk|Chapter|Bab|Cap[íi]tulo|Kapitel|Chapitre|"
    r"第\s*\S+\s*章|제\s*\S+\s*장)\b[^\n]*$"
)


def narrator_opening_ratio_scan(text: str, lang: str = "en",
                                ratio_max: float = 0.5) -> tuple[float, int, int]:
    """R-FG11: fraction of chapters whose first prose line matches a canned
    narrator-opening formula for `lang`. Returns (ratio, matched, total).
    Empty text / unknown lang / zero chapters → (0.0, 0, 0)."""
    if not text:
        return 0.0, 0, 0
    formulas = _NARRATOR_OPENING_FORMULAS.get(lang) or []
    if not formulas:
        return 0.0, 0, 0
    try:
        compiled = [re.compile(p) for p in formulas]
    except re.error:
        return 0.0, 0, 0
    # Split on chapter headings; drop the pre-first-heading preamble if any
    # headings exist, otherwise treat the whole text as one chapter.
    parts = _CHAPTER_SPLIT_RX.split(text)
    if len(parts) > 1:
        chapters = parts[1:]
    else:
        chapters = parts
    matched = 0
    total = 0
    for ch in chapters:
        # First non-empty line of the chapter body.
        first_line = ""
        for ln in (ch or "").splitlines():
            if ln.strip():
                first_line = ln
                break
        if not first_line:
            continue
        total += 1
        if any(rx.match(first_line) for rx in compiled):
            matched += 1
    if total == 0:
        return 0.0, 0, 0
    return (matched / total), matched, total


def gate_text(text: str, lang: str = "en", mode: str = "book", *,
              vo_strip: bool = True, style: Optional[str] = None
              ) -> tuple[str, dict[str, Any]]:
    """Full deterministic gate: known-bad correct → resolve flags (localized) → marker
    strip → foreign-token scan → terminal scan → strip survivors. Never raises; returns
    the original text on any internal error.

    vo_strip=False (per-chapter calls): keep [ANCHOR]/[BEAT] markers so the DOWNSTREAM
    counters can still measure the anchor budget on the assembled book — the terminal
    _apply_v3_gates pass (which runs AFTER the counters) does the actual marker strip.

    style (Phase 3, kwarg-only, backward-compatible default None): pakem style key used
    by the Phase 3 R-FG counters for per-style thresholds. When callers omit style, the
    Phase 3 scanners fall back to genre-agnostic defaults (0.5 R-FG11 threshold,
    zero factual-density minimum, factual-ending scan skipped)."""
    report: dict[str, Any] = {"known_bad": [], "flags": {}, "stripped": 0,
                              "markers_stripped": 0, "foreign_tokens": [],
                              "enabled": gate_enabled()}
    if not text or not gate_enabled():
        return text, report
    try:
        out, kb = apply_known_bad(text)
        out, stats = resolve_flags(out, lang=lang)
        # Chapter-heading REPAIR (bed-of-orchid part-2): unconditional + safe — splits a
        # heading fused mid-line + relabels a wrong-language chapter word. A clean book is
        # untouched. Runs always (not flag-gated) so the reader-visible defect never ships.
        out, _chr = chapter_heading_repair(out, lang=lang)
        stats["chapter_heading_repairs"] = _chr
        out, n_dehedged = dehedge_known_good(out)
        stats["dehedged_known_good"] = n_dehedged
        out, placeholder_cut = placeholder_prose_scan(out)
        stats["placeholder_prose_cut"] = len(placeholder_cut)
        out, unplaced_num_hits = unplaced_numeric_placeholder_scan(out)
        stats["unplaced_numeric_placeholder_cut"] = len(unplaced_num_hits)
        n_markers = 0
        if vo_strip:
            out, n_markers = strip_markers(out)
        out, foreign = foreign_token_scan(out, lang=lang)
        if _MORALISTE_CALIBRATION_ON():
            _r, _m, _t = narrator_opening_ratio_scan(out, lang=lang)
            stats["narrator_opening_ratio"] = _r
            stats["narrator_opening_matched"] = _m
            stats["narrator_opening_total"] = _t
            # Patch M (Phase 3, DALANG_INFRA_FIXES): style-conditional R-FG11 threshold.
            # When Phase 3 is off, retain the moraliste bundle's fixed 0.5 comparison so
            # the byte-identical flag-OFF invariant holds. When Phase 3 is on, look up
            # per-style threshold (moraliste=0.83, babad=1.0, pewayangan=0.5, cinematic
            # voiceover=0.15, unknown=0.5).
            _r_fg11_max = _r_fg11_threshold(style) if _INFRA_FIXES_ON() else 0.5
            stats["r_fg11_threshold"] = _r_fg11_max
            if _r > _r_fg11_max:
                stats["r_fg11_violation"] = True
        if _INFRA_FIXES_ON():
            _rt_count, _rt_samples = repeat_token_scan(out)
            stats["repeat_token_count"] = _rt_count
            stats["repeat_token_samples"] = _rt_samples
            if _rt_count > 2:
                # Corpus signal: sample-6 Vale (PT) 'os os ombros', sample-16 Wheat
                # near-verbatim Larsen re-intro Ch3+Ch5. Threshold conservative — a
                # legitimate refrain ('há tempo, há tempo, há tempo') will typically
                # trip 1-2 hits per narasi and stay under.
                stats["r_fg_w_violation"] = True
            # Patch J — source_note_density: report count + rate + under-threshold flag.
            _j_count, _j_rate, _j_under = source_note_density_scan(out, lang=lang, style=style)
            stats["source_note_count"] = _j_count
            stats["source_note_rate_per_1000w"] = round(_j_rate, 2)
            if _j_under:
                # Report-only — a downstream editor/dashboard can decide to surface this.
                stats["r_fg_j_violation"] = True
            # Patch LL — factual_ending_concrete_return: applies to factual regime only.
            _ll_applies, _ll_present, _ll_matches = factual_ending_concrete_return_scan(
                out, lang=lang, style=style
            )
            if _ll_applies:
                stats["factual_ending_present"] = _ll_present
                stats["factual_ending_matches"] = _ll_matches
                if not _ll_present:
                    stats["r_fg_ll_violation"] = True
            # Patch HH — human_anchor: unique named individuals + recurring across text.
            _hh_unique, _hh_sample, _hh_recurring = human_anchor_scan(out)
            stats["human_anchor_unique"] = _hh_unique
            stats["human_anchor_sample"] = _hh_sample
            stats["human_anchor_recurring"] = _hh_recurring
            # Patch IIIIII — entity_consistency_pass: shared front-end + report-only
            # internal-continuity check. External verification (nonfiction) defers.
            _ec = entity_consistency_scan(out)
            stats["entity_consistency"] = _ec
            # Track A (report-only) — glossary/kamus gloss detector (pattern-based,
            # domain-general) + POV grammatical-persona drift. Both surfaced by the
            # 3-romance corpus (sample-21/22) as pipeline defaults that survived the
            # prompt-injection banned_tells.
            _gl = glossary_definition_scan(out)
            stats["glossary_hits"] = _gl["glossary_hits"]
            stats["glossary_samples"] = _gl["glossary_samples"]
            _pv = pov_persona_drift_scan(out)
            stats["pov_persona"] = _pv
            if _pv.get("drift"):
                stats["pov_drift_flag"] = True
            # A.2 (sample-23): residual descriptive-slot hedge leak (report-only) — a
            # safety net that measures whether the R-FG6b sentence-strip caught the
            # placeholder, or a direct-generation path also leaks "sekitar <slot>".
            _pl = placeholder_leak_scan(out)
            stats["placeholder_leak_hits"] = _pl["placeholder_leak_hits"]
            stats["placeholder_leak_samples"] = _pl["placeholder_leak_samples"]
            if _pl["placeholder_leak_hits"]:
                stats["placeholder_leak_flag"] = True
            # C′ (sample-23): fiction stat-citation = REGISTER violation (report-only,
            # NOT a fact-check). Fiction styles only; nonfiction fact-scan is untouched.
            _fs = fiction_stat_citation_scan(out, style=style)
            if _fs.get("applies"):
                stats["fiction_stat_hits"] = _fs["fiction_stat_hits"]
                stats["fiction_stat_samples"] = _fs["fiction_stat_samples"]
                if _fs["fiction_stat_hits"]:
                    stats["fiction_stat_flag"] = True
                # Genre self-reference — same fiction-regime scope as the stat scan.
                _gsr = genre_self_reference_scan(out)
                stats["genre_self_ref_hits"] = _gsr["genre_self_ref_hits"]
                stats["genre_self_ref_samples"] = _gsr["genre_self_ref_samples"]
                if _gsr["genre_self_ref_hits"]:
                    stats["genre_self_ref_flag"] = True
                # Aphorism density (sample-25) — closes the register_spec counter loop
                # (aphorism_density_max_per_2_chapters ≈ 1). Flag when rate is high.
                _ap = aphorism_density_scan(out, style=style)
                if _ap.get("applies"):
                    stats["aphorism_hits"] = _ap["aphorism_hits"]
                    stats["aphorism_samples"] = _ap["aphorism_samples"]
                    stats["aphorism_rate_per_10_chapters"] = _ap["aphorism_rate_per_10_chapters"]
                    # Cap = ~1 per 2 chapters = 5 per 10; flag when materially over.
                    # VIDEO output is read aloud (VO must breathe), so it warrants a
                    # TIGHTER cap than book/text — two convergent lens reviews flagged
                    # aphorism density as high FOR VIDEO specifically. `mode` is already
                    # plumbed here (gate_text arg); report-only, no text change.
                    _ap_cap = 4.0 if str(mode).strip().lower() == "video" else 6.0
                    stats["aphorism_cap_per_10_chapters"] = _ap_cap
                    if _ap["aphorism_rate_per_10_chapters"] > _ap_cap:
                        stats["aphorism_flag"] = True
                # Phonetic-collision (sample-25 lens-#4): titled proper-noun pairs that
                # would drift in TTS output. Fiction-regime only, report-only.
                _pc = phonetic_collision_scan(out, style=style)
                if _pc.get("applies"):
                    stats["phonetic_collision_hits"] = _pc["phonetic_collision_hits"]
                    stats["phonetic_collision_pairs"] = _pc["phonetic_collision_pairs"]
                    if _pc["phonetic_collision_hits"]:
                        stats["phonetic_collision_flag"] = True
            # Duplicate-sentence + merge-token (narasi-3 lens synthesis, 2026-07-06).
            # Style-agnostic report-only — run for every gated text, not just fiction:
            # verbatim/near-dup repeats and lexical merge-corruptions ('ofite') break
            # any register (and TTS) regardless of genre.
            _ds = duplicate_sentence_scan(out)
            stats["duplicate_sentence_hits"] = _ds["duplicate_sentence_hits"]
            stats["duplicate_sentence_samples"] = _ds["duplicate_sentence_samples"]
            if _ds["duplicate_sentence_hits"]:
                stats["duplicate_sentence_flag"] = True
            # Language-consistency (report-only, style-agnostic): a sentence in a language
            # OTHER than `lang` (draft-residue leak, e.g. an Indonesian line in an English
            # book). Never rewrites/blocks — surfaces a flag a dashboard/editor can act on.
            _lc = language_consistency_scan(out, lang=lang)
            if _lc.get("applies"):
                stats["language_leak_hits"] = _lc["hits"]
                stats["language_leak_samples"] = _lc["samples"]
                stats["language_leak_langs"] = _lc["other_langs"]
                if _lc["hits"]:
                    stats["language_leak_flag"] = True
            _mt = merge_fusion_scan(out)
            stats["merge_fusion_hits"] = _mt["merge_fusion_hits"]
            stats["merge_fusion_samples"] = _mt["merge_fusion_samples"]
            if _mt["merge_fusion_hits"]:
                stats["merge_fusion_flag"] = True
            # Unresolved English template slots ("around appropriate …", "around 8-12
            # billion", "[name of district]") — ship-blockers for book output.
            _ir = instruction_residue_scan(out)
            stats["instruction_residue_hits"] = _ir["instruction_residue_hits"]
            stats["instruction_residue_samples"] = _ir["instruction_residue_samples"]
            if _ir["instruction_residue_hits"]:
                stats["instruction_residue_flag"] = True
            # Unattributed collective-voice leak (manuscript S2-Th-6 review flag,
            # 2026-07-16) — report-only, gated (NARASI_UNATTRIBUTED_VOICE_SCAN,
            # default OFF ⟹ block skipped ⟹ stats byte-identical).
            if _unattributed_voice_scan_on():
                _uv = unattributed_voice_scan(out)
                stats["unattributed_voice_hits"] = _uv["count"]
                stats["unattributed_voice_samples"] = _uv["samples"]
                if _uv["count"]:
                    stats["unattributed_voice_flag"] = True
            # Number-format consistency + Korean-ceramics fact-check (TVHL lens).
            _nf = number_format_scan(out)
            stats["number_format_styles"] = _nf["number_format_styles"]
            stats["number_format_samples"] = _nf["number_format_samples"]
            if _nf["number_format_styles"]:
                stats["number_format_flag"] = True
            _cx = ceramics_factcheck_scan(out)
            stats["ceramics_error_hits"] = _cx["ceramics_error_hits"]
            stats["ceramics_error_samples"] = _cx["ceramics_error_samples"]
            if _cx["ceramics_error_hits"]:
                stats["ceramics_error_flag"] = True
        survivors = terminal_scan(out)
        if survivors:
            # Never ship a directive bracket: deterministic last-resort strip.
            out = _DIRECTIVE_RX.sub(lambda m: "" if not _WHITELIST_RX.match(m.group(0)) else m.group(0), out)
            out = re.sub(r"[ \t]{2,}", " ", out)
            out = re.sub(r" +([,.;:!?])", r"\1", out)
        report.update({"known_bad": kb, "flags": stats, "stripped": len(survivors),
                       "markers_stripped": n_markers, "foreign_tokens": foreign[:20],
                       "meta_leak": meta_leak_scan(out),
                       "placeholder_prose": placeholder_cut,
                       "unplaced_numeric_placeholder": unplaced_num_hits,
                       "broken_substitution": broken_substitution_scan(out)})
        return out, report
    except Exception:
        return text, report


def known_corrections_prompt() -> str:
    """KNOWN CORRECTIONS block for the generation prompts (prevention, not patching).
    Injected into the shared system prefix so every worker sees the same hard facts."""
    if _ALT_HISTORY.get():
        return ""     # alt_history: departures from canon are the point
    lines = []
    seen = set()
    for c in _claims():
        note = (c.get("correct") or "").strip()
        if not note or note in seen:
            continue
        seen.add(note)
        if c["action"] == "replace":
            lines.append(f"- The siege of Tenochtitlan lasted 93 days (May 22 - August 13, 1521). "
                         f"NEVER write 75, 79, or 80 days." if c["name"].startswith("tenochtitlan")
                         else f"- {note}")
        else:
            lines.append(f"- {note}")
    # R-FG9 prompt-side prevention: teach the model the ONE legal channel for uncertainty
    # (the [VERIFY: ...] wrapper) so it never ships bare '3?' or '{N}' as prose. Emitted
    # unconditionally — this is a hard emission rule, not a project-scoped canon claim.
    lines.append("- Never emit a number followed by ? or a bare {N}/[X]/<TBD> — "
                 "use [VERIFY: …] so the gate can hedge or cut.")
    if not lines:
        return ""
    return "KNOWN CORRECTIONS (hard facts — never contradict these):\n" + "\n".join(dict.fromkeys(lines))


# ── R-H10 helper: banned source-branded diction (the anti-pastiche tells). Deterministic
# count used by the register scorecard; the LLM half (scale-shift / contingency-reveal)
# lives with the critic. ──
BANNED_DICTION = (
    # singular forms substring-match their plurals — do not list both
    "imagined order", "operating system of belief",
    "shared fiction", "universal fiction", "collective fiction",
    "gold-standard economy",   # anachronism (backend-rules fix #4)
)


def banned_diction_hits(text: str) -> list[str]:
    low = (text or "").lower()
    return [p for p in BANNED_DICTION if p in low]
