# ── narasi_gate — CC INSTRUCTION v3 (R-FG4/R-FG5/R-FG6): deterministic post-generation
# gates for the narasi engines. NO LLM calls, NO network, stdlib only — importable from
# both laozhang_api.py and the orchestrator without cycles.
#
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
_GENERIC_INNER = {"", "number", "value", "amount", "date", "year", "angka", "jumlah",
                  "tahun", "nilai", "n", "x", "?", "tbd", "..."}
# an inner STARTING with these is a META-REQUEST ("jumlah surat yang disita dalam arsip
# KITLV"), not a value — wrapping it with a hedge would ship the request as prose.
_GENERIC_PREFIX_RX = re.compile(
    r"(?i)^(?:jumlah|berapa|angka|nilai|tanggal|number\s+of|how\s+many|amount\s+of|"
    r"the\s+number|the\s+exact|exact\s+(?:number|date|figure))\b")


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
    stats = {"hedged_value": 0, "hedged_prose": 0, "cut": 0, "unwrapped_unmeasured": 0}
    if not text:
        return text, stats
    hv_tpl, hp_word, measured = _hedge_pack(lang)

    def _verify_sub(m: re.Match) -> str:
        inner = (m.group("inner") or "").strip()
        if not measured:
            # §3.2: no hedge vocab for this language → do not enforce; strip the bracket,
            # KEEP the inner text untouched (never let the wrong language's words in).
            stats["unwrapped_unmeasured"] += 1
            prep = m.group("prep") or ""
            qual = m.group("qual") or ""
            return (prep + qual + inner).strip()
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
        if inner.lower() not in _GENERIC_INNER and not _GENERIC_PREFIX_RX.match(inner):
            # Exit 1b (§3.1) — NON-numeric but substantive inner ("dua hari perjalanan"):
            # a value in words. NEVER delete it — hedge-wrap the text itself. If the inner
            # ALREADY starts with a hedge word ("sekitar empat puluh persen"), keep it
            # as-is — wrapping again shipped "sekitar sekitar…" in the Diponegoro run.
            stats["hedged_value"] += 1
            low_i = inner.lower()
            if any(low_i.startswith(h) for h in _HEDGES) or low_i.startswith(hp_word):
                return inner
            return hv_tpl.format(v=inner)
        # Exit 2 — genuinely empty/generic: hedge into prose (localized), qualifier consumed.
        stats["hedged_prose"] += 1
        prep = m.group("prep") or ""
        return (prep + hp_word).strip() if prep else hp_word

    text = _VERIFY_RX.sub(_verify_sub, text)
    text, n = _CUT_RX.subn("", text)
    stats["cut"] += n
    # tidy: collapse doubled spaces / space-before-punctuation the substitutions leave
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" +([,.;:!?])", r"\1", text)
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


def terminal_scan(text: str) -> list[str]:
    """Return surviving directive tokens (post-resolution). Empty list = clean."""
    hits = []
    for m in _DIRECTIVE_RX.finditer(text or ""):
        tok = m.group(0)
        if not _WHITELIST_RX.match(tok):
            hits.append(tok[:80])
    return hits


def gate_text(text: str, lang: str = "en", mode: str = "book", *,
              vo_strip: bool = True) -> tuple[str, dict[str, Any]]:
    """Full deterministic gate: known-bad correct → resolve flags (localized) → marker
    strip → foreign-token scan → terminal scan → strip survivors. Never raises; returns
    the original text on any internal error.

    vo_strip=False (per-chapter calls): keep [ANCHOR]/[BEAT] markers so the DOWNSTREAM
    counters can still measure the anchor budget on the assembled book — the terminal
    _apply_v3_gates pass (which runs AFTER the counters) does the actual marker strip."""
    report: dict[str, Any] = {"known_bad": [], "flags": {}, "stripped": 0,
                              "markers_stripped": 0, "foreign_tokens": [],
                              "enabled": gate_enabled()}
    if not text or not gate_enabled():
        return text, report
    try:
        out, kb = apply_known_bad(text)
        out, stats = resolve_flags(out, lang=lang)
        n_markers = 0
        if vo_strip:
            out, n_markers = strip_markers(out)
        out, foreign = foreign_token_scan(out, lang=lang)
        survivors = terminal_scan(out)
        if survivors:
            # Never ship a directive bracket: deterministic last-resort strip.
            out = _DIRECTIVE_RX.sub(lambda m: "" if not _WHITELIST_RX.match(m.group(0)) else m.group(0), out)
            out = re.sub(r"[ \t]{2,}", " ", out)
            out = re.sub(r" +([,.;:!?])", r"\1", out)
        report.update({"known_bad": kb, "flags": stats, "stripped": len(survivors),
                       "markers_stripped": n_markers, "foreign_tokens": foreign[:20]})
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
