# ── narasi_entities — ID-path fixes §5: scholar entity resolution (FG3, both directions).
# Round 4 taught MERGE detection (two Cooks = two people); the Diponegoro run teaches
# SPLIT detection: one person fragmented into many ("M.C. Ricklefs" / invented "Merijn
# Ricklefs" / "Merle Ricklefs" — Bab 3 staged him as his own opponent). Deterministic,
# stdlib-only, no LLM: table-driven variant unification + self-debate flag + wrong-domain
# downgrade + surname-cluster report. The table is the seed of the per-project scholar
# registry (a keyed view of known_good_claims, epistemic_class='attribution').
from __future__ import annotations

import re
from typing import Any

__all__ = ["entity_pass", "SCHOLAR_TABLE"]

# Seed per §5.1. canonical = the ONE name the manuscript uses (first mention may carry
# the ONE epithet; after that, bare surname per R-E3). variants list is matched
# case-sensitively as whole phrases.
SCHOLAR_TABLE: list[dict[str, Any]] = [
    {
        "canonical": "Peter Carey",
        "surname": "Carey",
        "variants": [],   # canonical + bare surname only; the failure was epithet drift
        "domain": "sejarawan Inggris (Oxford), arsip Yogyakarta & Perang Jawa",
        "epithet": "sejarawan Inggris yang menghabiskan sekitar empat dekade meneliti arsip Yogyakarta",
        # epithet-canon substitutions applied INSIDE this scholar's appositives (the
        # Diponegoro run drifted tiga-dekade/empat-dekade/puluhan-tahun across 10 intros)
        "epithet_fixes": [(r"(?i)(?:lebih\s+dari\s+)?tiga\s+dekade", "sekitar empat dekade"),
                          (r"(?i)puluhan\s+tahun", "sekitar empat dekade")],
    },
    {
        "canonical": "M.C. Ricklefs",
        "surname": "Ricklefs",
        # "Merijn" is an INVENTED first name (split-person evidence); Merle (Calvin) is real.
        "variants": ["Merijn Ricklefs", "Merle Calvin Ricklefs", "Merle Ricklefs",
                     "M. C. Ricklefs"],
        "domain": "sejarawan Australia, islamisasi Jawa",
        "epithet": "sejarawan Australia yang menekuni sejarah islamisasi Jawa",
        "epithet_fixes": [],
    },
]

# §5.3 wrong-domain: a real person attributed OUTSIDE their field is downgraded to an
# anonymous plural — "Meriam/Miriam Budiardjo" (ilmuwan politik) as babad philologist.
WRONG_DOMAIN: list[dict[str, str]] = [
    {
        # optional role prefix consumed so "filolog Meriam Budiardjo mengingatkan…"
        # reads "sejumlah filolog mengingatkan…"
        "pattern": r"(?:(?:seorang\s+)?(?:filolog|sejarawan|peneliti)\s+)?M[ei]riam\s+Budiardjo",
        "replacement": "sejumlah filolog",
        "note": "Miriam Budiardjo = ilmuwan politik, bukan filolog babad (wrong-domain attribution)",
    },
]

# adversative markers that stage a counter-argument (self-debate detection §5.2)
_ADVERSATIVE_RX = re.compile(r"(?i)\b(?:namun|tetapi|akan\s+tetapi|sebaliknya|meskipun\s+demikian|"
                             r"tidak\s+semua\s+sarjana|however|but|by\s+contrast|yet)\b")
_CHAPTER_SPLIT = re.compile(r"(?m)^##\s+")


def entity_pass(text: str) -> tuple[str, dict[str, Any]]:
    """Deterministic §5 pass. Returns (corrected_text, report).
    1. Variant unification: every table variant → canonical (split-person repair).
    2. Wrong-domain downgrade: table-listed misattributions → anonymous plural.
    3. Self-debate flag: one canonical entity on BOTH sides of an adversative within a
       chapter → conflict (reported; the diet/editor pass resolves the prose).
    4. Surname-cluster report: same surname under MULTIPLE full-name spellings NOT in
       the table → needs-review (search disambiguation is the manual/verify follow-up).
    Never raises — on any error the original text returns unmodified."""
    report: dict[str, Any] = {"unified": [], "wrong_domain": [], "self_debate": [],
                              "surname_clusters": []}
    if not text:
        return text, report
    try:
        out = text
        # 1 — variant unification (longest variants first so subsets don't pre-empt)
        for sch in SCHOLAR_TABLE:
            for var in sorted(sch.get("variants") or [], key=len, reverse=True):
                if var and var in out and var != sch["canonical"]:
                    out = out.replace(var, sch["canonical"])
                    report["unified"].append({"from": var, "to": sch["canonical"]})

        # 1b — R-E3 first-mention discipline (§5.4): the FIRST full-name mention keeps its
        # appositive epithet (canon-fixed); EVERY later mention collapses to the bare
        # surname with the appositive dropped — Carey was introduced 10× with drifting
        # epithets in the Diponegoro run.
        for sch in SCHOLAR_TABLE:
            full = sch["canonical"]
            sn = sch["surname"]
            if full not in out:
                continue
            first_idx = out.index(full)
            first_end = first_idx + len(full)
            # canon-fix the FIRST mention's appositive in place ("tiga dekade" drift)
            m_app = re.match(r",\s+[^,]{4,110},", out[first_end:])
            if m_app:
                app = m_app.group(0)
                fixed_app = app
                for pat, rep in (sch.get("epithet_fixes") or []):
                    fixed_app = re.sub(pat, rep, fixed_app)
                if fixed_app != app:
                    out = out[:first_end] + fixed_app + out[first_end + len(app):]
                    report.setdefault("epithet_fixed", []).append(
                        {"scholar": full, "from": app[:80], "to": fixed_app[:80]})
            # collapse every LATER full-name mention (+ its appositive) to bare surname
            head = out[:first_end]
            tail = out[first_end:]
            tail, k = re.subn(
                re.escape(full) + r"(?:,\s+(?:seorang\s+)?sejarawan[^,]{0,110},)?",
                sn, tail)
            if k:
                out = head + tail
                report.setdefault("collapsed", []).append({"scholar": full, "count": k})

        # 2 — wrong-domain downgrade
        for wd in WRONG_DOMAIN:
            rx = re.compile(wd["pattern"])
            out, k = rx.subn(wd["replacement"], out)
            if k:
                report["wrong_domain"].append({"replaced": wd["pattern"], "with": wd["replacement"],
                                               "count": k, "note": wd["note"]})

        # 3 — self-debate: same canonical surname on both sides of an adversative,
        # inside one chapter
        for ch in _CHAPTER_SPLIT.split(out):
            for sch in SCHOLAR_TABLE:
                sn = sch["surname"]
                idxs = [m.start() for m in re.finditer(rf"\b{re.escape(sn)}\b", ch)]
                if len(idxs) < 2:
                    continue
                for a, b in zip(idxs, idxs[1:]):
                    between = ch[a:b]
                    if _ADVERSATIVE_RX.search(between):
                        snippet = ch[max(0, a - 40):min(len(ch), b + 60)].replace("\n", " ")
                        report["self_debate"].append({"scholar": sch["canonical"],
                                                      "snippet": snippet[:220]})
                        break

        # 4 — surname clusters not covered by the table (report-only)
        known_surnames = {s["surname"] for s in SCHOLAR_TABLE}
        fullnames = set(re.findall(r"\b([A-Z][a-zA-Z.]+(?:\s+[A-Z][a-zA-Z.]+)+)\b", out))
        by_surname: dict[str, set] = {}
        for fn in fullnames:
            sn = fn.split()[-1]
            by_surname.setdefault(sn, set()).add(fn)
        for sn, names in by_surname.items():
            if len(names) > 1 and sn not in known_surnames:
                report["surname_clusters"].append({"surname": sn, "variants": sorted(names)[:5]})

        return out, report
    except Exception:  # noqa: BLE001 — a broken pass must never break generation
        return text, report
