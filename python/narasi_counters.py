# ── narasi_counters — CC v4 §1: deterministic counter enforcement + language packs.
# "Models write; code counts." Every scan here is pure code (zero LLM calls); budgets are
# read from the style's style_spec (refactor doc: pipeline_rules × style_spec) so all
# styles inherit the MACHINERY without inheriting harari's numbers. A style with
# counters=None (everything except harari today) reports OFF; a language without the
# needed pattern class reports UNMEASURED — never PASS (refactor §5 guard).
#
# Counters implemented (v4 §1 + §4/§5):
#   citations   R-H2  — attribution-pattern matches; per-chapter distribution; budget +
#                       "varied" check (≥1 chapter zero).
#   aporia      R-H3  — phrase-regex; sentences within ±2 of an attribution are EXEMPT
#                       (legitimate dispute-notes); standalone closers count.
#   epithet     R-E3  — first mention may carry an epithet; later mentions with a fresh
#                       appositive/epithet flag (Hassig-introduced-4x fix).
#   word_budget R-E4  — manuscript total vs target ±10%.
#   comparative R-FG7 — superlative/comparative claims over quantities, REPORT-ONLY
#                       (no fact-ledger ranges yet to auto-hedge against).
#   thesis      R-H6  — UNMEASURED unless a thesis sentence is provided AND an embed
#                       fn is available (dalang_dedup.embed); cosine-sim > threshold.
from __future__ import annotations

import math
import os
import re
from typing import Any, Optional

__all__ = ["LANGUAGE_PACKS", "scan_manuscript", "surgical_prompt"]

# ── language packs (refactor §5) — only the genuinely language-dependent parts. ──
LANGUAGE_PACKS: dict[str, dict[str, Any]] = {
    "en": {
        "aporia": re.compile(
            r"(?i)\b(?:cannot\s+(?:say|resolve|separate|show|tell|adjudicate|clarify)"
            r"|remains\s+(?:unresolved|contested|genuinely\s+unresolved)"
            r"|no\s+source\s+(?:specifies|records)"
            r"|does\s+not\s+record|do(?:es)?\s+not\s+remember|does\s+not\s+annotate"
            r"|no\s+one\s+(?:recorded|was\s+counting)"
            r"|left\s+no\s+unmediated\s+record|resists\s+clean\s+resolution"
            r"|sits\s+beyond\s+recovery|belongs\s+to\s+silence)\b"),
        "attribution": re.compile(
            r"(?:\b(?:[Hh]istorian|[Ss]cholar|[Aa]rchaeologist|[Ee]thnohistorian|[Ee]conomic\s+historian|"
            r"[Dd]emographic\s+historian|[Mm]ilitary\s+historian|[Aa]nthropologist)\s+"
            r"(?P<name1>[A-Z][a-z]+(?:\s+[A-Z][a-zA-Z]+)+))"
            r"|(?:\b(?P<name2>[A-Z][a-z]+(?:\s+[A-Z][a-zA-Z]+)+)\s+"
            r"(?:argues?|argued|contends?|contended|notes?|noted|recorded|recalls?|recalled|"
            r"documented|estimated|has\s+challenged|has\s+argued|pushed\s+further|emphasizes?|suggests?)\b)"),
        "epithet": re.compile(r",\s+(?:an?|the)\s+[^,]{4,60},"),
        "homographs": ["read", "lead", "wound", "tear", "bass", "row"],
        "wpm": {"min": 140, "max": 155},
    },
    "id": {
        # ID-path fixes §4: functional pack, seeded from the Diponegoro manuscript
        # (the standard growth mechanic — expand from every real ID run).
        "aporia": re.compile(
            r"(?i)(?:sumber\s+tidak\s+mencatat|tidak\s+ada\s+catatan\s+(?:yang|tentang)"
            r"|sejarah\s+tidak\s+menyimpan|tak\s+ada\s+yang\s+tahu\s+pasti"
            r"|tidak\s+mencatat\s+dengan\s+pasti|tidak\s+seragam\s+dalam\s+berbagai\s+catatan"
            r"|belum\s+bisa\s+dijawab\s+oleh\s+dokumen\s+mana\s*pun"
            r"|tidak\s+ada\s+satu\s+penjelasan\s+yang\s+memadai"
            r"|tidak\s+cukup\s+untuk\s+(?:memisahkan|memastikan)"
            r"|tidak\s+akan\s+pernah\s+selesai\s+dijawab"
            r"|tidak\s+ada\s+metode\s+yang\s+bisa\s+memverifikasi"
            r"|bukti\s+arsip\s+tidak|yang\s+tidak\s+diperdebatkan\s+adalah)"),
        "attribution": re.compile(
            r"(?:\b(?:[Ss]ejarawan|[Aa]rkeolog|[Aa]ntropolog|[Pp]eneliti|[Ff]ilolog)\s+"
            r"(?P<name1>[A-Z][a-zA-Z.]*(?:\s+[A-Z][a-zA-Z.]+)+))"
            r"|(?:\b(?P<name2>[A-Z][a-zA-Z.]*(?:\s+[A-Z][a-zA-Z.]+)+)\s+"
            r"(?:berpendapat|mencatat|menunjukkan|menegaskan|memperkirakan|berargumen|"
            r"menekankan|melihat|mengakui|merekonstruksi|mengingatkan)\b)"),
        # ID appositive epithet: ", sejarawan Inggris yang …," after a name (R-E3)
        "epithet": re.compile(r",\s+(?:seorang\s+)?(?:sejarawan|arkeolog|filolog|peneliti|"
                              r"antropolog|pakar|ahli)\s+[^,]{4,70},"),
        "homographs": ["apel", "serang", "tahu", "bisa", "kali"],
        "wpm": {"min": 130, "max": 150},
        # §3: hedge vocabulary comes from the pack, NEVER hardcoded EN.
        "hedge_value": "sekitar {v}",
        "hedge_prose": "beberapa",
        # §2.4: standalone tokens from OTHER languages = placeholder leakage. Replacement
        # map (token → ID equivalent); tokens mapping to "" are cut outright.
        "foreign_tokens": {
            "several": "beberapa", "around": "sekitar", "roughly": "kira-kira",
            "approximately": "kurang lebih", "about": "sekitar", "some": "beberapa",
            "nearly": "hampir", "circa": "sekitar", "tbd": "",
        },
    },
}

# §3/§7 hedge + number-spellout helpers for the EN pack too (uniform interface).
LANGUAGE_PACKS["en"]["hedge_value"] = "around {v}"
LANGUAGE_PACKS["en"]["hedge_prose"] = "several"
LANGUAGE_PACKS["en"]["foreign_tokens"] = {}


def spell_number_id(n: int) -> str:
    """ID spellout for 0..999_999 (§4 number_spellout, video-path speakable numbers).
    1825 → 'seribu delapan ratus dua puluh lima'."""
    units = ["nol", "satu", "dua", "tiga", "empat", "lima", "enam", "tujuh", "delapan", "sembilan"]
    if n < 0 or n > 999_999:
        raise ValueError("out of spellout range")
    if n < 10:
        return units[n]
    if n == 10:
        return "sepuluh"
    if n == 11:
        return "sebelas"
    if n < 20:
        return units[n - 10] + " belas"
    if n < 100:
        rest = n % 10
        return units[n // 10] + " puluh" + (" " + units[rest] if rest else "")
    if n < 200:
        rest = n % 100
        return "seratus" + (" " + spell_number_id(rest) if rest else "")
    if n < 1000:
        rest = n % 100
        return units[n // 100] + " ratus" + (" " + spell_number_id(rest) if rest else "")
    if n < 2000:
        rest = n % 1000
        return "seribu" + (" " + spell_number_id(rest) if rest else "")
    rest = n % 1000
    return spell_number_id(n // 1000) + " ribu" + (" " + spell_number_id(rest) if rest else "")


LANGUAGE_PACKS["id"]["spell_number"] = spell_number_id


_NUM_TOKEN_RX = re.compile(r"(?<![\d.,\-/])\b(\d{1,6})\b(?![\d.,\-/%])")


def render_numbers_id(text: str) -> tuple[str, int]:
    """§7 number rendering, video path: standalone integers → speakable ID spellout
    ('1825' → 'seribu delapan ratus dua puluh lima'). UNIFORM — one pass owns this.
    Skips: markdown headings (## Bab N is structural), decimals/ranges/percent-symbol
    forms, digit-adjacent compounds, and anything above the spellout range."""
    if not text:
        return text, 0
    out_lines: list[str] = []
    n = 0
    for line in text.split("\n"):
        if line.lstrip().startswith(("#", ">", "|")):
            out_lines.append(line)
            continue

        def _sub(m: re.Match) -> str:
            nonlocal n
            try:
                v = int(m.group(1))
                if v > 999_999:
                    return m.group(0)
                n += 1
                return spell_number_id(v)
            except Exception:  # noqa: BLE001
                return m.group(0)

        out_lines.append(_NUM_TOKEN_RX.sub(_sub, line))
    return "\n".join(out_lines), n

_COMPARATIVE = re.compile(
    r"(?i)\b(?:larger|bigger|greater|smaller|older|richer|more\s+populous|taller)\s+than\s+[A-Z][a-zA-Z]+"
    r"|\b(?:the\s+(?:largest|biggest|greatest|richest|oldest|most\s+populous))\b")

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _sentences(text: str) -> list[str]:
    return [s for s in _SENT_SPLIT.split(text or "") if s.strip()]


def _chapters(text: str) -> list[str]:
    """Split on '## ' headings; the pre-heading preamble (header line) is dropped."""
    parts = re.split(r"(?m)^##\s+", text or "")
    return parts[1:] if len(parts) > 1 else [text or ""]


def _names_from(m: re.Match) -> Optional[str]:
    return m.groupdict().get("name1") or m.groupdict().get("name2")


def scan_manuscript(text: str, *, lang: str = "en", style_entry: Optional[dict] = None,
                    thesis: Optional[str] = None, word_target: Optional[int] = None,
                    embed_fn: Any = None) -> dict:
    """Full deterministic scan. Returns a report dict; every counter carries
    status ∈ {PASS, OVER, OFF, UNMEASURED} + the evidence (sentences/indices) the
    surgical rewrite needs. Never raises."""
    spec = ((style_entry or {}).get("style_spec") or {})
    budgets = spec.get("counters") or {}
    pack = LANGUAGE_PACKS.get((lang or "en").split("-")[0].lower())
    report: dict[str, Any] = {"lang": lang, "counters": {}}
    try:
        chapters = _chapters(text)
        sents = _sentences(text)

        # ── R-H2 citations ──
        cit_budget = budgets.get("citations_max")
        if pack is None or pack.get("attribution") is None:
            report["counters"]["citations"] = {"status": "UNMEASURED"}
            attrib_sent_idx: set[int] = set()
        else:
            rx = pack["attribution"]
            per_ch = [len(rx.findall(ch)) for ch in chapters]
            hits = [(i, s) for i, s in enumerate(sents) if rx.search(s)]
            attrib_sent_idx = {i for i, _ in hits}
            total = sum(per_ch)
            names: list[str] = []
            for m in rx.finditer(text):
                n = _names_from(m)
                if n:
                    names.append(n)
            status = "OFF" if cit_budget is None else ("OVER" if total > int(cit_budget) else "PASS")
            varied_ok = True
            if budgets.get("citations_distribution") == "varied" and per_ch:
                varied_ok = (0 in per_ch)
            report["counters"]["citations"] = {
                "status": status, "count": total, "budget": cit_budget,
                "per_chapter": per_ch, "varied_ok": varied_ok,
                "distinct_scholars": sorted(set(names)),
                "sentences": [s[:200] for _, s in hits],
            }

        # ── R-H3 aporia (±2-sentence attribution exemption) ──
        ap_budget = budgets.get("aporia_max")
        if pack is None or pack.get("aporia") is None:
            report["counters"]["aporia"] = {"status": "UNMEASURED"}
        else:
            rx = pack["aporia"]
            standalone = []
            for i, s in enumerate(sents):
                if not rx.search(s):
                    continue
                near_attrib = any((i + d) in attrib_sent_idx for d in (-2, -1, 0, 1, 2))
                if not near_attrib:
                    standalone.append((i, s))
            status = "OFF" if ap_budget is None else ("OVER" if len(standalone) > int(ap_budget) else "PASS")
            report["counters"]["aporia"] = {
                "status": status, "count": len(standalone), "budget": ap_budget,
                "sentences": [s[:200] for _, s in standalone],
            }

        # ── R-E3 epithet-once ──
        # A RE-introduction is "Full Name, an/the <epithet>," for a surname the manuscript
        # has already introduced (attribution match OR earlier full-name+epithet). Checked on
        # every sentence — not only attribution-verb sentences (Hassig-4x came as bare
        # appositives, no verb).
        if pack is None or pack.get("attribution") is None or pack.get("epithet") is None:
            report["counters"]["epithet"] = {"status": "UNMEASURED"}
        else:
            rx = pack["attribution"]
            reintro = re.compile(r"\b([A-Z][a-z]+\s+[A-Z][a-zA-Z]+),\s+(?:an?|the)\s+[^,]{4,60},")
            seen: set[str] = set()
            violations = []
            for i, s in enumerate(sents):
                hits_full = [m.group(1) for m in reintro.finditer(s)]
                for full in hits_full:
                    surname = full.split()[-1]
                    if surname in seen:
                        violations.append((i, s))
                # register names AFTER the violation check so first-mention epithets are legal
                for m in rx.finditer(s):
                    name = _names_from(m)
                    if name:
                        seen.add(name.split()[-1])
                for full in hits_full:
                    seen.add(full.split()[-1])
            report["counters"]["epithet"] = {
                "status": "OVER" if violations else "PASS",
                "count": len(violations),
                "sentences": [s[:200] for _, s in violations],
            }

        # ── R-E4 word budget (±10%) ──
        # UNDER and OVER are distinct: an UNDERSHOOTING manuscript must NEVER enter the
        # surgical DIET loop (dieting = shrinking → it can only make an already-short book
        # shorter, or no-op and re-loop, burning un-metered Opus). Undershoot is the
        # word-gate/continuation path's job (per-chapter). So word_budget is reported here
        # but excluded from `over_budget` below — length is not a "surgical density" fix.
        words = len((text or "").split())
        if word_target:
            lo, hi = int(word_target * 0.9), int(word_target * 1.1)
            _wb_status = "PASS" if lo <= words <= hi else ("UNDER" if words < lo else "OVER")
            report["counters"]["word_budget"] = {
                "status": _wb_status,
                "count": words, "target": int(word_target), "range": [lo, hi],
            }
        else:
            report["counters"]["word_budget"] = {"status": "OFF", "count": words}

        # ── R-FG7 comparative claims (report-only — no verified ranges yet) ──
        comps = [(i, s) for i, s in enumerate(sents) if _COMPARATIVE.search(s)]
        report["counters"]["comparative"] = {
            "status": "REPORT", "count": len(comps),
            "sentences": [s[:200] for _, s in comps],
        }

        # ── anchors (ID-path §6.2): deterministic counter, budget 3 — the original
        # narasi rule (9 shipped in the Diponegoro run). Counted PRE-strip, so this scan
        # must run before the terminal gate removes the [ANCHOR] tokens. ──
        anchor_budget = int(budgets.get("anchors_max", 3))
        anchor_lines = [ln.strip()[:200] for ln in (text or "").splitlines()
                        if re.match(r"(?i)\s*\[anchor", ln)]
        report["counters"]["anchors"] = {
            "status": "OVER" if len(anchor_lines) > anchor_budget else "PASS",
            "count": len(anchor_lines), "budget": anchor_budget,
            "sentences": anchor_lines[:12],
        }

        # ── scene-dedup (ID-path §6.1): cross-chapter near-verbatim sensory beats.
        # Deterministic: normalized sentences (≥6 words) sharing an 8-word shingle across
        # DIFFERENT chapters = a stamped template ("lumpur … roda … hingga ke poros" in
        # Bab 4 AND Bab 7). OVER when any pair found. ──
        dup_hits: list[str] = []
        if len(chapters) > 1:
            def _shingles(t: str) -> set:
                w = re.sub(r"[^\wàâéèêîôûáíóúäëïöü' -]", " ", t.lower()).split()
                return {" ".join(w[i:i + 8]) for i in range(max(0, len(w) - 7))}
            seen_sh: dict[str, int] = {}
            for ci, ch in enumerate(chapters):
                for s in _sentences(ch):
                    if len(s.split()) < 6:
                        continue
                    for sh in _shingles(s):
                        prev = seen_sh.get(sh)
                        if prev is not None and prev != ci:
                            dup_hits.append(s.strip()[:200])
                            break
                        seen_sh.setdefault(sh, ci)
        dup_hits = list(dict.fromkeys(dup_hits))
        report["counters"]["scene_dup"] = {
            "status": "OVER" if dup_hits else "PASS",
            "count": len(dup_hits), "sentences": dup_hits[:10],
        }

        # ── R-H6 thesis restatement (embed-based; UNMEASURED without thesis+embed) ──
        th_budget = budgets.get("thesis_restatement_max")
        if not thesis or embed_fn is None or th_budget is None:
            report["counters"]["thesis"] = {"status": "UNMEASURED" if th_budget is not None else "OFF"}
        else:
            try:
                tv = embed_fn(thesis)
                def _cos(a, b):
                    dot = sum(x * y for x, y in zip(a, b))
                    na = math.sqrt(sum(x * x for x in a)); nb = math.sqrt(sum(y * y for y in b))
                    return dot / (na * nb) if na and nb else 0.0
                thr = float(os.environ.get("NARASI_THESIS_SIM_THRESHOLD", "0.80"))
                restatements = []
                for i, s in enumerate(sents):
                    if len(s.split()) < 6:
                        continue
                    sv = embed_fn(s)
                    if sv and _cos(tv, sv) >= thr:
                        restatements.append((i, s))
                status = "OVER" if len(restatements) > int(th_budget) else "PASS"
                report["counters"]["thesis"] = {
                    "status": status, "count": len(restatements), "budget": th_budget,
                    "sentences": [s[:200] for _, s in restatements],
                }
            except Exception:  # noqa: BLE001
                report["counters"]["thesis"] = {"status": "UNMEASURED"}

        # word_budget is deliberately EXCLUDED from the diet trigger (see note above): a
        # too-long book isn't a density defect the surgical prompt can fix, and a too-short
        # one belongs to the word-gate. Only real editorial-density counters drive the loop.
        report["over_budget"] = [k for k, v in report["counters"].items()
                                 if v.get("status") == "OVER" and k != "word_budget"]
        return report
    except Exception:  # noqa: BLE001 — a broken scan must never break generation
        report["error"] = "scan_failed"
        report["over_budget"] = []
        return report


def surgical_prompt(report: dict, *, language: str = "English") -> str:
    """Build the SURGICAL rewrite instruction (v4 §1): list the exact offending sentences;
    forbid touching anything else. Full-chapter rewrites are banned in this loop."""
    parts = [
        "SURGICAL EDIT ONLY. Below is a manuscript followed by specific sentences that "
        "exceed editorial budgets. Fix ONLY the listed sentences — cut, merge, or convert "
        "them as instructed. Do NOT rewrite, rephrase, or touch ANY other sentence. Do NOT "
        "change headings, facts, names, dates, or numbers elsewhere. Return the FULL "
        f"manuscript in {language} with only those edits applied.",
    ]
    c = report.get("counters", {})
    if c.get("citations", {}).get("status") == "OVER":
        cit = c["citations"]
        parts.append(
            f"CITATIONS: {cit['count']} scholar attributions exceed the budget of {cit['budget']}. "
            "Keep only the citations doing real dispute-work; convert the weakest to unattributed "
            "prose (e.g. 'one line of scholarship argues…') or delete the sentence. Offending sentences:\n- "
            + "\n- ".join(cit.get("sentences", [])[:20]))
    if c.get("aporia", {}).get("status") == "OVER":
        ap = c["aporia"]
        parts.append(
            f"APORIA CLOSERS: {ap['count']} stand-alone 'the sources do not record…'-class closers "
            f"exceed the budget of {ap['budget']}. Keep the strongest {ap['budget']}; end the other "
            "paragraphs plainly (a concrete image or a flat statement). Offending sentences:\n- "
            + "\n- ".join(ap.get("sentences", [])[:12]))
    if c.get("epithet", {}).get("status") == "OVER":
        epv = c["epithet"]
        parts.append(
            "EPITHETS: these sentences re-introduce an already-introduced scholar with a fresh "
            "epithet. Strip the appositive; use the bare surname. Sentences:\n- "
            + "\n- ".join(epv.get("sentences", [])[:10]))
    if c.get("thesis", {}).get("status") == "OVER":
        th = c["thesis"]
        parts.append(
            f"THESIS RESTATEMENTS: {th['count']} restatements exceed the budget of {th['budget']}. "
            "Keep the strongest two; delete or sharpen the rest into NEW claims. Sentences:\n- "
            + "\n- ".join(th.get("sentences", [])[:10]))
    if c.get("anchors", {}).get("status") == "OVER":
        an = c["anchors"]
        parts.append(
            f"ANCHOR LINES: {an['count']} [ANCHOR] lines exceed the budget of {an['budget']}. "
            f"Keep only the {an['budget']} strongest anchors (delete the [ANCHOR] line entirely, "
            "including its text). Anchor lines:\n- " + "\n- ".join(an.get("sentences", [])[:12]))
    if c.get("scene_dup", {}).get("status") == "OVER":
        sd = c["scene_dup"]
        parts.append(
            "DUPLICATED SCENE BEATS: these sensory sentences repeat near-verbatim across "
            "chapters (a stamped template). Rewrite EACH duplicate with a DIFFERENT sensory "
            "register (rotate: cuaca, bau, suara, tekstur, cahaya) while keeping its factual "
            "content. Sentences:\n- " + "\n- ".join(sd.get("sentences", [])[:10]))
    return "\n\n".join(parts)
