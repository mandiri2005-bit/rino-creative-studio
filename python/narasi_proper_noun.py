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

__all__ = ["detect_entities", "verify_pass", "phantom_name_scan", "ENTITY_TABLE"]

# ── Unified entity table. Kind ∈ {person, institution, place, treaty}. Seeded from
# what shipped in narasi_entities.SCHOLAR_TABLE/WRONG_DOMAIN; new entries land here,
# not in the person-only structure. Extend by adding rows here or (Phase 2) loading from
# the `narasi_proper_nouns` DB table (mig 0067 — deferred).
ENTITY_TABLE: list[dict[str, Any]] = [
    # ── persons (scholars) ──
    # SPEC §3.2 affiliation field: `institution` is load-bearing (Hadler-Berkeley class:
    # real scholar at wrong university ships silently otherwise). When set, verify_pass
    # flags any wrong-institution attribution in the manuscript.
    {"kind": "person",
     "canonical": "Peter Carey", "surname": "Carey",
     "variants": [],
     "domain": "sejarawan Inggris (Oxford), arsip Yogyakarta & Perang Jawa",
     "institution": "Oxford",
     "institution_alt": ["University of Oxford", "Oxford University", "Trinity College Oxford"],
     "institution_wrong": [],
     "epithet": "sejarawan Inggris yang menghabiskan sekitar empat dekade meneliti arsip Yogyakarta",
     "epithet_fixes": [(r"(?i)(?:lebih\s+dari\s+)?tiga\s+dekade", "sekitar empat dekade"),
                       (r"(?i)puluhan\s+tahun", "sekitar empat dekade")]},
    {"kind": "person",
     "canonical": "M.C. Ricklefs", "surname": "Ricklefs",
     "variants": ["Merijn Ricklefs", "Merle Calvin Ricklefs", "Merle Ricklefs",
                  "M. C. Ricklefs"],
     "domain": "sejarawan Australia, islamisasi Jawa",
     "institution": "Australian National University",
     "institution_alt": ["ANU", "Monash University", "Australian National"],
     "institution_wrong": [],
     "epithet": "sejarawan Australia yang menekuni sejarah islamisasi Jawa",
     "epithet_fixes": []},
    {"kind": "person",
     "canonical": "Hendrik Merkus de Kock", "surname": "De Kock",
     "variants": ["Hendrik Merkus de Groot van Amstel de Kock",
                  "Hendrik Merkus, Baron de Kock",
                  "Hendrik Merkus Baron de Kock"],
     "domain": "letnan gubernur-jenderal Hindia Belanda, Benteng Stelsel",
     "epithet": "", "epithet_fixes": []},
    # ── §12.1 Bonjol seed: Hadler affiliation LOAD-BEARING (real scholar wrong uni). ──
    {"kind": "person",
     "canonical": "Jeffrey Hadler", "surname": "Hadler",
     "variants": [],
     "domain": "sejarawan Minangkabau/Padri (Cornell 2008, Muslims and Matriarchs)",
     "institution": "UC Berkeley",
     "institution_alt": ["University of California Berkeley", "University of California, Berkeley",
                          "Berkeley", "UC Berkeley History"],
     "institution_wrong": ["Universitas Virginia", "University of Virginia", "UVA",
                            "Virginia", "Univ of Virginia"],
     "epithet": "sejarawan Minangkabau di UC Berkeley yang menulis Muslims and Matriarchs",
     "epithet_fixes": []},
    {"kind": "person",
     "canonical": "Christine Dobbin", "surname": "Dobbin",
     "variants": [],
     "domain": "ekonomi pra-kolonial Minangkabau + revivalisme Islam (Curzon 1983)",
     "institution": "Australian National University",
     "institution_alt": ["ANU"],
     "institution_wrong": [],
     "epithet": "sejarawan yang mendokumentasikan ekonomi pra-kolonial Minangkabau",
     "epithet_fixes": []},
    # ── wrong-subject (person + institution) — canonical is the ANONYMIZED replacement ──
    {"kind": "person_wrong_domain",
     "canonical": "sejumlah filolog",
     "pattern": r"(?:(?:seorang\s+)?(?:filolog|sejarawan|peneliti)\s+)?M[ei]riam\s+Budiardjo",
     "note": "Miriam Budiardjo = ilmuwan politik, bukan filolog babad"},
    {"kind": "person_wrong_domain",
     "canonical": "sejumlah sejarawan",
     "pattern": r"(?:(?:seorang\s+)?(?:filolog|sejarawan|peneliti|arkeolog)\s+)?Meriel\s+Buiskool",
     "note": "Meriel Buiskool = fabricated Java-War scholar"},
    {"kind": "person_wrong_domain",
     "canonical": "sejumlah peneliti",
     "pattern": r"(?:(?:seorang\s+)?(?:filolog|sejarawan|peneliti|arkeolog)\s+)?M[uo]rkalala\s+Firman",
     "note": "Murkalala Firman = fabricated Minangkabau scholar (Bonjol review, §12.1)"},
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
                              "merge_candidates": [],
                              # SPEC §3.2 affiliation field — Hadler-Berkeley class.
                              "institution_mismatch": []}
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

        # 1d — SPEC §3.2 affiliation-mismatch flag. For every person with a defined
        # `institution` field, scan the manuscript for that name paired with a WRONG
        # institution (from institution_wrong list, or any string that isn't in
        # institution + institution_alt) within a 240-char window. Real scholar +
        # wrong university ships silently otherwise (Hadler-Virginia class). Flag only,
        # never auto-correct — reviewer/spot-fixer resolves.
        for r in persons:
            canon = r.get("canonical")
            correct = r.get("institution")
            if not (canon and correct):
                continue
            correct_all = {correct.lower()} | {a.lower() for a in (r.get("institution_alt") or [])}
            wrongs = [w for w in (r.get("institution_wrong") or [])]
            surname = r.get("surname") or canon.split()[-1]
            # Search for the person mention + a nearby institution mention in the SAME
            # sentence (bounded by sentence terminators or paragraph breaks). Prior wider
            # window admitted the next scholar's institution as a false positive.
            for m in re.finditer(r"\b" + re.escape(canon) + r"\b|\b" + re.escape(surname) + r"\b",
                                  out):
                # find sentence-start (nearest .!?/newline before the name, or 0)
                lo = m.start()
                while lo > 0 and out[lo - 1] not in ".!?\n":
                    lo -= 1
                # find sentence-end (nearest .!?/newline after the name, or end)
                hi = m.end()
                while hi < len(out) and out[hi] not in ".!?\n":
                    hi += 1
                window = out[lo:hi]
                # Case A: explicit wrong-institution listed
                for w in wrongs:
                    if re.search(r"\b" + re.escape(w) + r"\b", window, re.IGNORECASE):
                        report["institution_mismatch"].append(
                            {"scholar": canon, "wrong": w, "correct": correct,
                             "context": window.strip()[:200]})
                        break
                else:
                    # Case B: any Uni/College-style token near the name that is NOT
                    # in the correct set. Detect institution-of-attribution shape:
                    # "di X University" / "of X University" / "Universitas X" / "at X".
                    inst_m = re.search(
                        r"(?:(?:di|dari|at|of|from|based\s+at)\s+"
                        r"(?:the\s+)?)((?:University\s+of\s+[A-Z][a-zA-Z]+"
                        r"|(?:UC|University\s+of\s+California)[,\s]+[A-Z][a-zA-Z]+"
                        r"|Universitas\s+[A-Z][a-zA-Z]+"
                        r"|[A-Z][a-zA-Z]+\s+University"
                        r"|Berkeley|Oxford|Cambridge|Yale|Harvard|Princeton))",
                        window)
                    if inst_m:
                        cand = inst_m.group(1).strip().lower()
                        if not any(c in cand or cand in c for c in correct_all):
                            report["institution_mismatch"].append(
                                {"scholar": canon, "wrong": inst_m.group(1),
                                 "correct": correct, "context": window.strip()[:200]})

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


# ── PHANTOM-NAME scan (report-only) — story-bible bleed class (kdrama eky9gcge:
# "Shim Ro-ha" appeared ONCE, in the finale, with presupposition phrasing). A PERSON
# full name whose FIRST mention falls in the final `tail_frac` of the text with
# <= `max_mentions` total mentions is flagged. Mentions are counted per name COMPONENT
# (full form + bare given-name/surname, hyphen/apostrophe-aware for Korean names), so a
# character properly introduced earlier under a short or variant form is EXCLUDED (its
# earliest component mention is early — and the caller runs this on POST-verify_pass
# text, so table-driven variant unification has already collapsed listed aliases).
# Components shared by >=2 distinct full names (family surnames: "Shim" in "Shim Ro-ha"
# + "Shim Min-jun") are ignored when a distinctive component exists, so a frequent
# family name cannot mask a phantom sibling. `bible` (canonical_facts / story bible) is
# ANNOTATION-ONLY: in_bible=True distinguishes bible-cast bleed from pure hallucination
# — bible presence does NOT exclude, because the defect IS a bible name leaking into
# the manuscript without an on-page introduction. status 'FLAG'/'PASS' only (NEVER
# 'OVER' — same contract as narasi_counters._opening_motif_scan: can never enter
# over_budget or drive the diet loop). Detection only — never edits text. Never raises.
_PHANTOM_NAME_RX = re.compile(
    r"\b([A-Z][a-zA-Z]+(?:[-'’][A-Za-z]+)?"
    r"(?:\s+[A-Z][a-zA-Z]+(?:[-'’][A-Za-z]+)?)+)")


def phantom_name_scan(text: str, *, bible: str = "", tail_frac: float = 0.25,
                      max_mentions: int = 2) -> dict:
    """Report-only phantom-name detection. Returns
    {"status": "PASS"|"FLAG", "count": int, "tail_frac": float, "max_mentions": int,
     "names": [{name, first_index, first_frac, mentions, in_bible, sentence}]}."""
    out: dict[str, Any] = {"status": "PASS", "count": 0,
                           "tail_frac": tail_frac, "max_mentions": max_mentions,
                           "names": []}
    try:
        if not text or len(text) < 400:
            return out
        n = len(text)
        cut = int(n * (1.0 - tail_frac))
        # 1 — candidate full names (earliest index of the FULL form). The greedy regex
        # swallows a leading capitalized sentence-opener ('Lalu Shim Ro-ha', 'Ketika
        # Yu Na') — STRIP stoplisted edge tokens instead of rejecting the whole
        # candidate, or ordinary Indonesian prose hides exactly the phantom class this
        # hunts (review finding, reproduced).
        first_full: dict[str, int] = {}
        for m in _PHANTOM_NAME_RX.finditer(text):
            tok = m.group(1)
            parts = tok.split()
            while parts and parts[0] in _STOP:
                parts = parts[1:]
            while parts and parts[-1] in _STOP:
                parts = parts[:-1]
            if len(parts) < 2:
                continue
            tok2 = " ".join(parts)
            _off = tok.find(tok2)
            first_full.setdefault(tok2, m.start() + (_off if _off >= 0 else 0))
            # A remaining non-stop opener can still prefix ('Akhirnya Shim Ro-ha'
            # where 'Akhirnya' recurs early): also register the trailing bigram as
            # its own candidate so the real name gets probed independently.
            if len(parts) >= 3:
                _bg = " ".join(parts[-2:])
                _bo = tok.find(_bg)
                first_full.setdefault(_bg, m.start() + (_bo if _bo >= 0 else 0))
        if not first_full:
            return out
        # 2 — component ownership map (a component in >=2 distinct full names is a
        # shared family name, not a distinctive handle)
        owners: dict[str, set] = {}
        for tok in first_full:
            for p in tok.split():
                if len(p) >= 3 and p not in _STOP:
                    owners.setdefault(p, set()).add(tok)
        hits: list[dict] = []
        for tok, fidx in first_full.items():
            if fidx < cut:
                continue                  # full form already appears before the tail
            comps = [p for p in tok.split() if len(p) >= 3 and p not in _STOP]
            if not comps:
                # Short-token names ('Yu Na', 'Bo Ra'): fall back to a 2-char floor
                # rather than skipping — two-syllable Korean given names romanized as
                # separate short tokens are a realistic phantom class here.
                comps = [p for p in tok.split() if len(p) >= 2 and p not in _STOP]
            distinctive = [p for p in comps if len(owners.get(p) or ()) == 1]
            probe = distinctive or comps or [tok]
            mentions = 0
            first_idx = fidx
            for p in probe:
                ms = [mm.start() for mm in re.finditer(r"\b" + re.escape(p) + r"\b", text)]
                if ms:
                    mentions = max(mentions, len(ms))
                    first_idx = min(first_idx, ms[0])
            if first_idx < cut or mentions > max_mentions:
                continue                  # introduced earlier, or genuinely recurring
            in_bible = bool(bible) and any(
                re.search(r"\b" + re.escape(p) + r"\b", bible) for p in [tok] + probe)
            hits.append({"name": tok, "first_index": first_idx,
                         "first_frac": round(first_idx / n, 3), "mentions": mentions,
                         "in_bible": in_bible,
                         "sentence": _snip(text, fidx, fidx + len(tok))})
        if hits:
            out["status"] = "FLAG"
            out["count"] = len(hits)
            out["names"] = sorted(hits, key=lambda h: h["first_index"])[:8]
        return out
    except Exception:  # noqa: BLE001 — a broken scan must never break generation
        return {"status": "PASS", "count": 0, "tail_frac": tail_frac,
                "max_mentions": max_mentions, "names": []}
