# ── narasi_proper_noun — SPEC v1 §3.2: unified proper_noun_verify pass.
# One pass over PERSONS + INSTITUTIONS + PLACES + TREATIES. Merges what used to live in
# narasi_entities (scholars) with the institution class in factscan, plus adds place-name
# extraction and treaty title/body consistency. All edits from table or search — never
# from generation (models "repair" names by elaborating).
#
# Contract:
#   detect_entities(text) → dict of typed sets (persons | institutions | places | treaties)
#                          with sentence + position tuples
#   verify_pass(text, table_rows, title, mode) → (patched_text, report):
#      1. variant unification (person + institution + treaty) via table
#      2. R-E3 first-mention epithet / later bare-surname (persons only, table-independent
#         via language pack's `epithet` regex — inherited from narasi_counters logic)
#      3. wrong-subject downgrade-to-anonymous (persons + institutions), table-driven
#      4. title/body treaty-name consistency ("Traktat London" header vs "Traktat Sumatra"
#         body — Aceh review Bab 2 defect)
#      5. self-debate flag when ≥2 distinct name-forms of the same person are staged
#         across an adversative (never on a single canonical name)
#      6. surname/name-cluster report (per-sentence, stoplisted; the entity_pass v1
#         cluster noise is fixed here)
#      7. merge-detection HOOK (two Cooks = two people) — reports same-surname pairs
#         appearing with DISTINCT domains/dates so an operator or the verify pass can
#         disambiguate. Detection only — never automatic split.
from __future__ import annotations

import re
from typing import Any, Optional

__all__ = ["detect_entities", "verify_pass", "ENTITY_TABLE"]

# ── Unified entity table. Kind ∈ {person, institution, place, treaty}. Seeded from
# what shipped in narasi_entities.SCHOLAR_TABLE/WRONG_DOMAIN; new entries land here,
# not in the person-only structure. Extend by adding rows here or (Phase 2) loading from
# the `narasi_proper_nouns` DB table (mig 0067 — deferred).
ENTITY_TABLE: list[dict[str, Any]] = [
    # ── persons (scholars) ──
    {"kind": "person",
     "canonical": "Peter Carey", "surname": "Carey",
     "variants": [],
     "domain": "sejarawan Inggris (Oxford), arsip Yogyakarta & Perang Jawa",
     "epithet": "sejarawan Inggris yang menghabiskan sekitar empat dekade meneliti arsip Yogyakarta",
     "epithet_fixes": [(r"(?i)(?:lebih\s+dari\s+)?tiga\s+dekade", "sekitar empat dekade"),
                       (r"(?i)puluhan\s+tahun", "sekitar empat dekade")]},
    {"kind": "person",
     "canonical": "M.C. Ricklefs", "surname": "Ricklefs",
     "variants": ["Merijn Ricklefs", "Merle Calvin Ricklefs", "Merle Ricklefs",
                  "M. C. Ricklefs"],
     "domain": "sejarawan Australia, islamisasi Jawa",
     "epithet": "sejarawan Australia yang menekuni sejarah islamisasi Jawa",
     "epithet_fixes": []},
    {"kind": "person",
     "canonical": "Hendrik Merkus de Kock", "surname": "De Kock",
     "variants": ["Hendrik Merkus de Groot van Amstel de Kock",
                  "Hendrik Merkus, Baron de Kock",
                  "Hendrik Merkus Baron de Kock"],
     "domain": "letnan gubernur-jenderal Hindia Belanda, Benteng Stelsel",
     "epithet": "", "epithet_fixes": []},
    # ── wrong-subject (person + institution) — canonical is the ANONYMIZED replacement ──
    {"kind": "person_wrong_domain",
     "canonical": "sejumlah filolog",
     "pattern": r"(?:(?:seorang\s+)?(?:filolog|sejarawan|peneliti)\s+)?M[ei]riam\s+Budiardjo",
     "note": "Miriam Budiardjo = ilmuwan politik, bukan filolog babad"},
    {"kind": "person_wrong_domain",
     "canonical": "sejumlah sejarawan",
     "pattern": r"(?:(?:seorang\s+)?(?:filolog|sejarawan|peneliti|arkeolog)\s+)?Meriel\s+Buiskool",
     "note": "Meriel Buiskool = fabricated Java-War scholar"},
    # ── treaties (title/body consistency: Bab-header vs body) ──
    {"kind": "treaty",
     "canonical": "Traktat Sumatra 1871",
     "surname": "Sumatra 1871",
     "variants": ["Traktat London 1824 (dalam konteks Aceh)"],
     "domain": "perjanjian Belanda-Inggris tentang Sumatra (bukan London 1824)",
     "note": "kelas Aceh Bab-2: judul 'Traktat London' padahal body benar 'Traktat Sumatra'"},
]

# ── deterministic detectors (kept small; the language pack owns the appositive form) ──
_PERSON_FULLNAME_RX = re.compile(r"\b([A-Z][a-zA-Z.]+(?:\s+[A-Z][a-zA-Z.]+)+)")
_INSTITUTION_NOUNS = (r"(?:[Kk]as|[Dd]ewan|[Kk]antor|[Kk]omisi|[Ll]embaga|[Bb]adan|"
                      r"[Mm]ajelis|[Bb]enteng|[Ss]istem|[Uu]ndang|[Rr]esolusi|"
                      r"[Aa]turan|[Kk]eputusan)")
_INSTITUTION_RX = re.compile(
    r"\b(" + _INSTITUTION_NOUNS + r"\s+[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)\b")
_PLACE_RX = re.compile(
    r"\b(?:di|ke|dari|menuju|at|to|from|in|near)\s+"
    r"(?:kota\s+|kabupaten\s+|provinsi\s+|desa\s+|kampung\s+)?"
    r"([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)\b")
_TREATY_RX = re.compile(
    r"\b((?:[Tt]raktat|[Pp]erjanjian|[Tt]reaty|[Uu]ndang[- ][Uu]ndang)\s+"
    r"[A-Z][a-zA-Z]+(?:\s+\d{4})?)\b")
# Adversative markers for self-debate (Indonesian + English)
_ADVERSATIVE_RX = re.compile(r"(?i)\b(?:namun|tetapi|akan\s+tetapi|sebaliknya|"
                             r"meskipun\s+demikian|tidak\s+semua\s+sarjana|"
                             r"however|but|by\s+contrast|yet)\b")
_CHAPTER_SPLIT = re.compile(r"(?m)^##\s+")
# Function-word stoplist for cluster reporting (person-cluster false-positive filter)
_STOP = {"Yang", "Tetapi", "Pada", "Dalam", "Di", "Ketika", "Tidak", "Sang", "Lalu",
         "Dan", "Mereka", "Namun", "Setelah", "Sebelum", "Bab", "Jawa", "Belanda",
         "Perang", "Tanah", "Fort", "The", "But", "When", "After", "Babad", "Pasukan",
         "Koalisi", "Ratu", "Kota", "Gunung", "Sungai", "Aceh", "Kuta", "Traktat"}


def detect_entities(text: str) -> dict[str, list]:
    """Return typed detections: persons (full-name+following-appositive), institutions,
    places (after a preposition), treaties. Each entry: {token, sentence, index}."""
    out: dict[str, list] = {"persons": [], "institutions": [], "places": [], "treaties": []}
    if not text:
        return out
    for m in _PERSON_FULLNAME_RX.finditer(text):
        tok = m.group(1)
        parts = tok.split()
        if parts[0] in _STOP or parts[-1] in _STOP:
            continue
        out["persons"].append({"token": tok, "sentence": _snip(text, m.start(), m.end()),
                                "index": m.start()})
    for m in _INSTITUTION_RX.finditer(text):
        out["institutions"].append({"token": m.group(1),
                                     "sentence": _snip(text, m.start(), m.end()),
                                     "index": m.start()})
    for m in _PLACE_RX.finditer(text):
        tok = m.group(1)
        if tok in _STOP:
            continue
        out["places"].append({"token": tok,
                               "sentence": _snip(text, m.start(), m.end()),
                               "index": m.start()})
    for m in _TREATY_RX.finditer(text):
        out["treaties"].append({"token": m.group(1),
                                 "sentence": _snip(text, m.start(), m.end()),
                                 "index": m.start()})
    return out


def _snip(text: str, s: int, e: int, before: int = 40, after: int = 100) -> str:
    return text[max(0, s - before):min(len(text), e + after)].replace("\n", " ").strip()[:200]


def _table_independent_r_e3(text: str, lang: str, report: dict) -> str:
    """SPEC v1 §3.6 R-E3 within-doc, TABLE-INDEPENDENT: after the FIRST full-name +
    appositive introduction of ANY person (whether or not they're in ENTITY_TABLE),
    every LATER `Full Name, <appositive>,` collapses to bare surname. The Christine-
    Dobbin regression on the Imam-Bonjol live run: 4 different epithets on 4 mentions
    because Dobbin wasn't in the table. Now the pack's own epithet regex is enough.

    We keep the FIRST introduction verbatim (its epithet establishes the person); every
    subsequent `Full Name, <epithet…>,` swap becomes just the surname. Full-name-only
    (no comma-epithet) later mentions are untouched — they're already legal."""
    try:
        from narasi_counters import LANGUAGE_PACKS
        pack = LANGUAGE_PACKS.get((lang or "id").split("-")[0].lower())
    except Exception:  # noqa: BLE001
        pack = None
    if not pack or not pack.get("epithet"):
        return text
    epi_rx = pack["epithet"]
    full_rx = re.compile(r"\b([A-Z][a-zA-Z.]+(?:\s+[A-Z][a-zA-Z.]+)+)")
    seen_first: set[str] = set()
    out_parts: list[str] = []
    pos = 0
    for m in full_rx.finditer(text):
        full = m.group(1)
        parts = full.split()
        if parts[0] in _STOP or parts[-1] in _STOP:
            continue
        surname = parts[-1]
        tail = text[m.end():m.end() + 200]
        epi_m = epi_rx.match(tail)
        if not epi_m:
            continue
        if surname not in seen_first:
            # first full+epithet mention — keep verbatim
            seen_first.add(surname)
            continue
        # RE-introduction: keep the surname only; drop the full name AND its appositive
        drop_end = m.end() + epi_m.end()
        # (append text since last cursor, then surname, then jump past drop_end)
        out_parts.append(text[pos:m.start()])
        out_parts.append(surname)
        pos = drop_end
        report.setdefault("collapsed_table_independent", []).append(
            {"scholar": full, "kept": surname})
    if pos == 0:
        return text
    out_parts.append(text[pos:])
    return "".join(out_parts)


def verify_pass(text: str, *, title: str = "", lang: str = "id",
                extra_rows: Optional[list] = None) -> tuple[str, dict]:
    """One-shot unified proper_noun_verify. Returns (patched_text, report).
    Report shape:
      {
        unified: [{from, to}],
        collapsed: [{scholar, count}],
        epithet_fixed: [{scholar, from, to}],
        wrong_domain: [{replaced, with, note, count}],
        self_debate: [{scholar, snippet}],
        title_body_conflict: [{title, body, note}],
        cluster: [{surname, variants}],
        merge_candidates: [{surname, forms, distinct_indices}],
      }
    Never raises; on error returns (original, {}).
    Rows = ENTITY_TABLE + caller-supplied `extra_rows` (per-project scholars/institutions
    loaded from DB in Phase-2; today only used by tests)."""
    report: dict[str, Any] = {"unified": [], "collapsed": [], "epithet_fixed": [],
                              "wrong_domain": [], "self_debate": [],
                              "title_body_conflict": [], "cluster": [],
                              "merge_candidates": []}
    if not text:
        return text, report
    try:
        rows = list(ENTITY_TABLE) + list(extra_rows or [])
        persons = [r for r in rows if r.get("kind") == "person"]
        wrong = [r for r in rows if r.get("kind") in ("person_wrong_domain",
                                                       "institution_wrong_domain")]
        treaties = [r for r in rows if r.get("kind") == "treaty"]
        out = text

        # 1 — variant unification (persons + treaties; longest-first).
        for r in persons + treaties:
            for var in sorted(r.get("variants") or [], key=len, reverse=True):
                if var and var in out and var != r["canonical"]:
                    out = out.replace(var, r["canonical"])
                    report["unified"].append({"from": var, "to": r["canonical"]})

        # 1b — R-E3 collapse for persons: first mention keeps its epithet (canon-fixed),
        # every later full-name+epithet appositive collapses to bare surname.
        for r in persons:
            full = r["canonical"]
            sn = r["surname"]
            if full not in out:
                continue
            first_idx = out.index(full)
            first_end = first_idx + len(full)
            m_app = re.match(r",\s+[^,]{4,110},", out[first_end:])
            if m_app:
                app = m_app.group(0)
                fixed_app = app
                for pat, rep in (r.get("epithet_fixes") or []):
                    fixed_app = re.sub(pat, rep, fixed_app)
                if fixed_app != app:
                    out = out[:first_end] + fixed_app + out[first_end + len(app):]
                    report["epithet_fixed"].append({"scholar": full,
                                                     "from": app[:80], "to": fixed_app[:80]})
            head = out[:first_end]
            tail = out[first_end:]
            tail, k = re.subn(
                re.escape(full) + r"(?:,\s+(?:seorang\s+)?sejarawan[^,]{0,110},)?",
                sn, tail)
            if k:
                out = head + tail
                report["collapsed"].append({"scholar": full, "count": k})

        # 1c — TABLE-INDEPENDENT R-E3: collapse epithet re-intros for scholars not in
        # ENTITY_TABLE (Christine Dobbin ×4 on the Imam-Bonjol run). Must run AFTER
        # variant unification (so re-intros of canonical names collapse) and AFTER
        # table-driven collapse (so no double-processing on table-listed scholars).
        out = _table_independent_r_e3(out, lang, report)

        # 2 — wrong-subject downgrade (persons + institutions).
        for w in wrong:
            rx = re.compile(w["pattern"])
            out, k = rx.subn(w["canonical"], out)
            if k:
                report["wrong_domain"].append({"replaced": w["pattern"],
                                                "with": w["canonical"],
                                                "count": k, "note": w.get("note", "")})

        # 3 — title/body treaty-name consistency. If the manuscript's TITLE (or Bab-N
        # heading) mentions "Traktat X" but the body uses "Traktat Y", report both.
        _headings = re.findall(r"(?m)^##\s+.*", out)
        body_treaties = set(m.group(1) for m in _TREATY_RX.finditer(out))
        head_treaties = set()
        for h in [title] + _headings:
            for m in _TREATY_RX.finditer(h):
                head_treaties.add(m.group(1))
        for ht in head_treaties:
            for bt in body_treaties:
                if ht != bt and _treaty_key(ht) == _treaty_key(bt):
                    # different specific but same generic kind (both "Traktat X")
                    report["title_body_conflict"].append(
                        {"title": ht, "body": bt,
                         "note": "chapter/document heading names one treaty; body names another"})

        # 4 — self-debate flag (persons, requires ≥2 different name-forms).
        for r in persons:
            forms = [f for f in ([r["canonical"]] + (r.get("variants") or []))
                     if f and f in text]
            if len(forms) < 2:
                continue
            for ch in _CHAPTER_SPLIT.split(text):
                idxs = []
                for f in forms:
                    idxs += [(m.start(), f) for m in re.finditer(re.escape(f), ch)]
                idxs.sort()
                for (a, fa), (b, fb) in zip(idxs, idxs[1:]):
                    if fa != fb and _ADVERSATIVE_RX.search(ch[a:b]):
                        snippet = ch[max(0, a - 40):min(len(ch), b + 60)].replace("\n", " ")
                        report["self_debate"].append({"scholar": r["canonical"],
                                                       "snippet": snippet[:220]})
                        break

        # 5 — cluster report (per-sentence, stoplisted). Same code as entity_pass v1
        # but shared here so the OLD entity_pass can retire.
        known_surnames = {r["surname"] for r in persons}
        fullnames: set[str] = set()
        for sent in re.split(r"(?<=[.!?])\s+|\n+", out):
            for fn in re.findall(r"\b([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)+)\b", sent):
                parts = fn.split()
                if parts[0] in _STOP or parts[-1] in _STOP:
                    continue
                fullnames.add(fn)
        by_surname: dict[str, set] = {}
        for fn in fullnames:
            sn = fn.split()[-1]
            by_surname.setdefault(sn, set()).add(fn)
        for sn, names in by_surname.items():
            if len(names) > 1 and sn not in known_surnames:
                report["cluster"].append({"surname": sn, "variants": sorted(names)[:5]})
            # 6 — merge-detection HOOK: same surname, distinct first-name letters, ≥2
            # widely-spaced mentions → candidate for two-people-with-same-surname (the
            # two-Cooks class). Reports; never splits automatically.
            if len(names) >= 2 and sn in known_surnames:
                report["merge_candidates"].append({"surname": sn,
                                                    "forms": sorted(names)[:4],
                                                    "note": "same surname, ≥2 full-name forms — verify same-vs-different-person"})

        return out, report
    except Exception:  # noqa: BLE001 — a broken pass must never break generation
        return text, report


def _treaty_key(tok: str) -> str:
    """Normalize a treaty token to its generic kind for title/body comparison
    ('Traktat Sumatra 1871' → 'traktat', 'Perjanjian Giyanti' → 'perjanjian')."""
    parts = tok.lower().split()
    return parts[0] if parts else tok.lower()
