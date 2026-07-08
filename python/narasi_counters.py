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
        "epithet": re.compile(r",\s+(?:an?|the)\s+[^,]{4,140},"),
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
            r"|bukti\s+arsip\s+tidak|yang\s+tidak\s+diperdebatkan\s+adalah"
            # round-2 manuscript additions (the growth mechanic):
            r"|(?:bukti|sumber)\s+yang\s+tersedia\s+tidak\s+memungkinkan"
            r"|tidak\s+memungkinkan\s+(?:kita\s+)?(?:memisahkan|memastikan|merekonstruksi)"
            r"|tidak\s+cukup\s+untuk\s+merekonstruksi)"),
        # Three shapes seen in real ID manuscripts: "sejarawan NAMA", "Menurut NAMA",
        # and "NAMA[, aposisi panjang,] VERBA" — the dominant Diponegoro form puts a full
        # appositive BETWEEN name and verb, so the optional `(?:,[^,]{4,120},)?` bridge is
        # load-bearing. Single-word surnames count too ("Carey berargumen").
        "attribution": re.compile(
            r"(?:\b(?:[Ss]ejarawan|[Aa]rkeolog|[Aa]ntropolog|[Pp]eneliti|[Ff]ilolog)\s+"
            r"(?P<name1>[A-Z][a-zA-Z.]*(?:\s+[A-Z][a-zA-Z.]+)+))"
            r"|(?:\b[Mm]enurut\s+(?:pembacaan\s+|catatan\s+|analisis\s+)?"
            r"(?P<name3>[A-Z][a-zA-Z.]+(?:\s+[A-Z][a-zA-Z.]+)?))"
            r"|(?:\b(?P<name2>[A-Z][a-zA-Z.]+(?:\s+[A-Z][a-zA-Z.]+)*)(?:,\s+[^,]{4,120},)?\s+"
            r"(?:berpendapat|mencatat|menunjukkan|menegaskan|memperkirakan|berargumen|"
            r"menekankan|melihat|mengakui|merekonstruksi|mengingatkan|menawarkan|"
            r"membantah|menyebutnya|mengidentifikasi)\b)"),
        # ID appositive epithet: ", sejarawan Inggris yang …," after a name (R-E3)
        # ID historian appositives run long ("sejarawan Universitas London yang
        # mendokumentasikan ekonomi Minangkabau pra-kolonial," ~78 chars). Cap 140 covers
        # realistic prose without accepting a whole sentence between commas.
        "epithet": re.compile(r",\s+(?:seorang\s+)?(?:sejarawan|arkeolog|filolog|peneliti|"
                              r"antropolog|pakar|ahli)\s+[^,]{4,140},"),
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

# ── ID spelled-quantity detector: ID prose spells numbers ("dua ratus prajurit",
# "tiga puluh ribu kilometer persegi"), so a digits-only sweep is BLIND on the ID path —
# the Diponegoro run's invented statistics sailed through unflagged. Feeds factscan. ──
LANGUAGE_PACKS["id"]["spelled_quantity"] = re.compile(
    r"(?i)\b(?:se)?(?:satu|dua|tiga|empat|lima|enam|tujuh|delapan|sembilan|sepuluh|sebelas|"
    r"puluh|belas|ratus|ribu|juta)"
    r"(?:[-\s](?:satu|dua|tiga|empat|lima|enam|tujuh|delapan|sembilan|puluh|belas|ratus|ribu|juta))*"
    r"\s+(?:orang|prajurit|tentara|serdadu|jiwa|tahun|hari|bulan|jilid|halaman|gulden|"
    r"kilometer|meter|mdpl|kaki|hektar|ton|pos|kapal|kuda|persen|dekade|abad|kali)\b")

# ── multi-language packs (Rino 2026-07-04: "consider other lang as well"). Every served
# language gets AT MINIMUM hedge vocab + a foreign-token blocklist, so the FG6 resolve
# pass is localized everywhere and EN placeholder tokens can never leak into any non-EN
# manuscript. Latin-script languages with clear conventions also get starter
# attribution/aporia patterns (counters measure); the rest stay honestly UNMEASURED for
# those counters (visible in the §1 manifest) until seeded from real manuscripts. ──
_EN_LEAK = ("several", "around", "roughly", "approximately", "about", "some",
            "nearly", "circa", "tbd")


def _leak_map(*repls: str) -> dict:
    """Zip the EN leak tokens against per-language replacements ('' = cut)."""
    return dict(zip(_EN_LEAK, repls))


_EXTRA_PACKS: dict[str, dict[str, Any]] = {
    "ms": {
        "hedge_value": "kira-kira {v}", "hedge_prose": "beberapa",
        "foreign_tokens": _leak_map("beberapa", "sekitar", "kira-kira", "kurang lebih",
                                    "sekitar", "beberapa", "hampir", "sekitar", ""),
        "aporia": re.compile(r"(?i)(?:sumber\s+tidak\s+mencatat|tiada\s+catatan|"
                             r"tidak\s+dapat\s+dipastikan|tidak\s+diketahui\s+dengan\s+pasti)"),
        "attribution": re.compile(
            r"(?:\b(?:[Ss]ejarawan|[Aa]hli\s+sejarah|[Pp]enyelidik)\s+"
            r"(?P<name1>[A-Z][a-zA-Z.]*(?:\s+[A-Z][a-zA-Z.]+)+))"
            r"|(?:\b(?P<name2>[A-Z][a-zA-Z.]*(?:\s+[A-Z][a-zA-Z.]+)+)\s+"
            r"(?:berhujah|mencatat|menegaskan|berpendapat|menganggarkan)\b)"),
    },
    "jv": {
        "hedge_value": "watara {v}", "hedge_prose": "sawetara",
        "foreign_tokens": _leak_map("sawetara", "watara", "kira-kira", "kirang langkung",
                                    "watara", "sawetara", "meh", "watara", ""),
        "aporia": re.compile(r"(?i)(?:ora\s+ana\s+cathetan|sumber\s+ora\s+nyathet|"
                             r"ora\s+bisa\s+dipesthekake)"),
    },
    "su": {
        "hedge_value": "kira-kira {v}", "hedge_prose": "sababaraha",
        "foreign_tokens": _leak_map("sababaraha", "kira-kira", "kira-kira", "kurang leuwih",
                                    "kira-kira", "sababaraha", "ampir", "kira-kira", ""),
    },
    "ban": {
        "hedge_value": "kirang langkung {v}", "hedge_prose": "makudang",
        "foreign_tokens": _leak_map("makudang", "kirang langkung", "kira-kira", "kirang langkung",
                                    "kirang langkung", "makudang", "meh", "kirang langkung", ""),
    },
    "min": {
        "hedge_value": "sakitar {v}", "hedge_prose": "babarapo",
        "foreign_tokens": _leak_map("babarapo", "sakitar", "kiro-kiro", "kurang labiah",
                                    "sakitar", "babarapo", "ampia", "sakitar", ""),
    },
    "es": {
        "hedge_value": "alrededor de {v}", "hedge_prose": "varios",
        "foreign_tokens": _leak_map("varios", "alrededor de", "aproximadamente", "aproximadamente",
                                    "cerca de", "algunos", "casi", "hacia", ""),
        "aporia": re.compile(r"(?i)(?:las\s+fuentes\s+no\s+registran|no\s+hay\s+constancia|"
                             r"no\s+se\s+puede\s+determinar|no\s+queda\s+registro)"),
        "attribution": re.compile(
            r"(?:\b(?:[Ee]l\s+)?(?:[Hh]istoriador|[Aa]rque[oó]log[oa]|[Ii]nvestigador)\s+"
            r"(?P<name1>[A-Z][a-zA-Z.]*(?:\s+[A-Z][a-zA-Z.]+)+))"
            r"|(?:\b(?P<name2>[A-Z][a-zA-Z.]*(?:\s+[A-Z][a-zA-Z.]+)+)\s+"
            r"(?:sostiene|argumenta|se[ñn]ala|documenta|registra|estima)\b)"),
    },
    "fr": {
        "hedge_value": "environ {v}", "hedge_prose": "plusieurs",
        "foreign_tokens": _leak_map("plusieurs", "environ", "environ", "approximativement",
                                    "environ", "quelques", "près de", "vers", ""),
        "aporia": re.compile(r"(?i)(?:les\s+sources\s+ne\s+mentionnent\s+pas|aucune\s+trace|"
                             r"impossible\s+[àa]\s+d[ée]terminer|nul\s+ne\s+sait)"),
        "attribution": re.compile(
            r"(?:\b(?:[Ll]'historien(?:ne)?|[Ll]'arch[ée]ologue)\s+"
            r"(?P<name1>[A-Z][a-zA-Z.]*(?:\s+[A-Z][a-zA-Z.]+)+))"
            r"|(?:\b(?P<name2>[A-Z][a-zA-Z.]*(?:\s+[A-Z][a-zA-Z.]+)+)\s+"
            r"(?:soutient|note|souligne|documente|estime|rappelle)\b)"),
        # §12.3 Louis XIV: "vraisemblablement" is a LEGITIMATE hedge for genuinely-
        # uncertain dates (mariage secret ~1683). Kept in hedge_prose_alt so the
        # gate can recognize it as a hedge marker without confusing it with a
        # bare-value hedge.
        "hedge_prose_alt": ("vraisemblablement", "sans doute", "probablement", "à peu près"),
        # §3.4.2 world-date hedge guard: "vers" prepended to a famous exact date
        # (Louis XIII †14 mai 1643) violates the "world-documented values are NEVER
        # hedged" invariant. The gate strips the hedge on any date that matches
        # WORLD_DATE_RX and is inside a KNOWN_GOOD or the FR world-dates seed.
        "world_date_hedge_rx": re.compile(
            r"(?i)\b(?P<hedge>vers|environ|autour\s+de|aux\s+alentours\s+de)\s+"
            r"(?P<date>le\s+\d{1,2}(?:er)?\s+"
            r"(?:janvier|f[ée]vrier|mars|avril|mai|juin|juillet|ao[uû]t|septembre|"
            r"octobre|novembre|d[ée]cembre)\s+\d{4})"),
    },
    "de": {
        "hedge_value": "etwa {v}", "hedge_prose": "mehrere",
        # "circa" is NATIVE German — not in the blocklist.
        "foreign_tokens": {"several": "mehrere", "around": "etwa", "roughly": "ungefähr",
                           "approximately": "annähernd", "about": "etwa", "some": "einige",
                           "nearly": "fast", "tbd": ""},
        "aporia": re.compile(r"(?i)(?:die\s+Quellen\s+verzeichnen\s+nicht|keine\s+Aufzeichnung|"
                             r"l[äa]sst\s+sich\s+nicht\s+feststellen)"),
        "attribution": re.compile(
            r"(?:\b(?:[Dd]er\s+)?(?:[Hh]istoriker(?:in)?|[Aa]rch[äa]ologe)\s+"
            r"(?P<name1>[A-Z][a-zA-Z.]*(?:\s+[A-Z][a-zA-Z.]+)+))"
            r"|(?:\b(?P<name2>[A-Z][a-zA-Z.]*(?:\s+[A-Z][a-zA-Z.]+)+)\s+"
            r"(?:argumentiert|betont|dokumentiert|sch[äa]tzt|vermerkt)\b)"),
    },
    "pt": {
        "hedge_value": "cerca de {v}", "hedge_prose": "vários",
        "foreign_tokens": _leak_map("vários", "cerca de", "aproximadamente", "aproximadamente",
                                    "cerca de", "alguns", "quase", "por volta de", ""),
        "aporia": re.compile(r"(?i)(?:as\s+fontes\s+n[ãa]o\s+registram|n[ãa]o\s+h[áa]\s+registro|"
                             r"n[ãa]o\s+se\s+pode\s+determinar)"),
        "attribution": re.compile(
            r"(?:\b(?:[Oo]\s+)?(?:[Hh]istoriador(?:a)?|[Aa]rque[óo]log[oa])\s+"
            r"(?P<name1>[A-Z][a-zA-Z.]*(?:\s+[A-Z][a-zA-Z.]+)+))"
            r"|(?:\b(?P<name2>[A-Z][a-zA-Z.]*(?:\s+[A-Z][a-zA-Z.]+)+)\s+"
            r"(?:argumenta|observa|registra|documenta|estima)\b)"),
    },
    "nl": {
        "hedge_value": "ongeveer {v}", "hedge_prose": "verscheidene",
        # native_hedge_whitelist below moves the "circa" exclusion from comment → data.
        "foreign_tokens": {"several": "verscheidene", "around": "ongeveer", "roughly": "ruwweg",
                           "approximately": "bij benadering", "about": "ongeveer",
                           "some": "enkele", "nearly": "bijna", "tbd": ""},
        "aporia": re.compile(r"(?i)(?:de\s+bronnen\s+vermelden\s+niet|geen\s+verslag|"
                             r"valt\s+niet\s+vast\s+te\s+stellen)"),
        # DALANG_MORALISTE_CALIBRATION additions. Consumers must guard reads on the flag.
        "native_hedge_whitelist": {"enkele", "circa"},  # resolves 'enkele' double-classification bug + moves 'circa' from comment to data
        "tech_loan_tokens": {"feature": "functie", "dashboard": "overzicht",
                             "update": "actualisering", "user": "gebruiker",
                             "subscription": "abonnement"},
    },
    "vi": {
        "hedge_value": "khoảng {v}", "hedge_prose": "vài",
        "foreign_tokens": _leak_map("vài", "khoảng", "khoảng", "xấp xỉ", "khoảng",
                                    "một vài", "gần", "khoảng", ""),
    },
    "tl": {
        "hedge_value": "humigit-kumulang {v}", "hedge_prose": "ilang",
        "foreign_tokens": _leak_map("ilang", "mga", "humigit-kumulang", "humigit-kumulang",
                                    "mga", "ilang", "halos", "noong mga", ""),
    },
    "ar": {
        "hedge_value": "حوالي {v}", "hedge_prose": "عدة",
        "foreign_tokens": _leak_map("عدة", "حوالي", "تقريبًا", "تقريبًا", "نحو", "بعض",
                                    "قرابة", "نحو", ""),
    },
    "zh": {
        "hedge_value": "大约{v}", "hedge_prose": "若干",
        "foreign_tokens": _leak_map("若干", "大约", "大致", "大约", "约", "一些", "将近", "约", ""),
    },
    "ja": {
        "hedge_value": "およそ{v}", "hedge_prose": "いくつか",
        "foreign_tokens": _leak_map("いくつか", "およそ", "おおよそ", "約", "約", "一部",
                                    "近く", "約", ""),
    },
    "ko": {
        "hedge_value": "약 {v}", "hedge_prose": "여러",
        "foreign_tokens": _leak_map("여러", "약", "대략", "약", "약", "일부", "거의", "약", ""),
    },
    "hi": {
        "hedge_value": "लगभग {v}", "hedge_prose": "कई",
        "foreign_tokens": _leak_map("कई", "लगभग", "मोटे तौर पर", "लगभग", "लगभग", "कुछ",
                                    "लगभग", "लगभग", ""),
    },
    "th": {
        "hedge_value": "ประมาณ {v}", "hedge_prose": "หลาย",
        "foreign_tokens": _leak_map("หลาย", "ประมาณ", "ราว ๆ", "ประมาณ", "ราว", "บางส่วน",
                                    "เกือบ", "ราว", ""),
    },
    "ru": {
        "hedge_value": ["около {v}", "примерно {v}", "приблизительно {v}",
                        "порядка {v}", "почти {v}"],
        "hedge_prose": ["несколько", "много", "немного", "некоторые",
                        "множество", "ряд"],
        "foreign_tokens": ["about", "roughly", "approximately", "several",
                           "many", "some", "few", "around"],
        "aporia_phrases": ["неясно, действительно ли", "невозможно установить",
                           "источники расходятся", "точно неизвестно",
                           "остаётся под вопросом", "данные противоречивы"],
        "attribution_patterns": ["писал", "утверждал", "отмечал", "подчёркивал",
                                 "реконструировал", "доказывал"],
    },
    "pl": {
        "hedge_value": ["około {v}", "mniej więcej {v}", "w przybliżeniu {v}",
                        "blisko {v}", "prawie {v}"],
        "hedge_prose": ["kilka", "wiele", "trochę", "niektóre", "parę", "sporo"],
        "foreign_tokens": ["about", "roughly", "approximately", "several",
                           "many", "some", "few", "around"],
        "aporia_phrases": ["nie jest jasne, czy", "nie można ustalić",
                           "źródła są rozbieżne", "pozostaje niepewne",
                           "brak pewności co do", "dane są sprzeczne"],
        "attribution_patterns": ["pisał", "twierdził", "zauważył", "podkreślał",
                                 "zrekonstruował", "argumentował"],
    },
    "uk": {
        "hedge_value": ["близько {v}", "приблизно {v}", "орієнтовно {v}",
                        "порядку {v}", "майже {v}"],
        "hedge_prose": ["кілька", "багато", "трохи", "деякі", "чимало", "низка"],
        "foreign_tokens": ["about", "roughly", "approximately", "several",
                           "many", "some", "few", "around"],
        "aporia_phrases": ["незрозуміло, чи", "неможливо встановити",
                           "джерела розходяться", "залишається невідомим",
                           "точно не відомо", "дані суперечливі"],
        "attribution_patterns": ["писав", "стверджував", "зазначав", "підкреслював",
                                 "реконструював", "доводив"],
    },
    "cs": {
        "hedge_value": ["přibližně {v}", "asi {v}", "kolem {v}", "zhruba {v}",
                        "téměř {v}"],
        "hedge_prose": ["několik", "mnoho", "trochu", "některé", "pár", "řada"],
        "foreign_tokens": ["about", "roughly", "approximately", "several",
                           "many", "some", "few", "around"],
        "aporia_phrases": ["není jasné, zda", "nelze určit",
                           "prameny se rozcházejí", "zůstává nejisté",
                           "není známo", "údaje si protiřečí"],
        "attribution_patterns": ["psal", "tvrdil", "poznamenal", "zdůraznil",
                                 "rekonstruoval", "dokládal"],
    },
    "sk": {
        "hedge_value": ["približne {v}", "asi {v}", "okolo {v}", "zhruba {v}",
                        "takmer {v}"],
        "hedge_prose": ["niekoľko", "mnoho", "trochu", "niektoré", "zopár", "rad"],
        "foreign_tokens": ["about", "roughly", "approximately", "several",
                           "many", "some", "few", "around"],
        "aporia_phrases": ["nie je jasné, či", "nemožno určiť",
                           "pramene sa rozchádzajú", "zostáva neisté",
                           "nie je známe", "údaje si protirečia"],
        "attribution_patterns": ["písal", "tvrdil", "poznamenal", "zdôraznil",
                                 "rekonštruoval", "dokladal"],
    },
    "bg": {
        "hedge_value": ["около {v}", "приблизително {v}", "горе-долу {v}",
                        "някъде към {v}", "почти {v}"],
        "hedge_prose": ["няколко", "много", "малко", "някои", "доста", "неколцина"],
        "foreign_tokens": ["около", "приблизително", "някои", "about",
                           "roughly", "approximately", "some", "several"],
        "aporia_phrases": ["не е ясно дали", "не може да се установи",
                           "източниците се разминават", "остава неясно",
                           "липсват достатъчно данни", "трудно е да се каже"],
        "attribution_patterns": ["написа", "твърди", "отбеляза", "подчерта",
                                 "възстанови", "изтъкна", "посочи"],
    },
    "sr": {
        "hedge_value": ["око {v}", "отприлике {v}", "приближно {v}",
                        "негде око {v}", "скоро {v}"],
        "hedge_prose": ["неколико", "много", "мало", "неки", "подоста", "понеки"],
        "foreign_tokens": ["око", "отприлике", "неки", "about",
                           "roughly", "approximately", "some", "several"],
        "aporia_phrases": ["није јасно да ли", "не може се утврдити",
                           "извори се не слажу", "остаје нејасно",
                           "нема довољно података", "тешко је рећи"],
        "attribution_patterns": ["написао је", "тврдио је", "приметио је",
                                 "нагласио је", "реконструисао је", "истакао је",
                                 "указао је"],
    },
    "hr": {
        "hedge_value": ["oko {v}", "otprilike {v}", "približno {v}",
                        "negdje oko {v}", "skoro {v}"],
        "hedge_prose": ["nekoliko", "mnogo", "malo", "neki", "podosta", "poneki"],
        "foreign_tokens": ["oko", "otprilike", "neki", "about",
                           "roughly", "approximately", "some", "several"],
        "aporia_phrases": ["nije jasno je li", "ne može se utvrditi",
                           "izvori se ne slažu", "ostaje nejasno",
                           "nema dovoljno podataka", "teško je reći"],
        "attribution_patterns": ["napisao je", "tvrdio je", "primijetio je",
                                 "naglasio je", "rekonstruirao je", "istaknuo je",
                                 "ukazao je"],
    },
    "sl": {
        "hedge_value": ["okoli {v}", "približno {v}", "okrog {v}",
                        "nekje okoli {v}", "skoraj {v}"],
        "hedge_prose": ["nekaj", "veliko", "malo", "nekateri", "precej", "peščica"],
        "foreign_tokens": ["okoli", "približno", "nekateri", "about",
                           "roughly", "approximately", "some", "several"],
        "aporia_phrases": ["ni jasno, ali", "ni mogoče ugotoviti",
                           "viri si nasprotujejo", "ostaja nejasno",
                           "ni dovolj podatkov", "težko je reči"],
        "attribution_patterns": ["napisal je", "trdil je", "opazil je",
                                 "poudaril je", "rekonstruiral je", "izpostavil je",
                                 "opozoril je"],
    },
    "mk": {
        "hedge_value": ["околу {v}", "приближно {v}", "отприлика {v}",
                        "некаде околу {v}", "скоро {v}"],
        "hedge_prose": ["неколку", "многу", "малку", "некои", "доста",
                        "неколкумина"],
        "foreign_tokens": ["околу", "приближно", "некои", "about",
                           "roughly", "approximately", "some", "several"],
        "aporia_phrases": ["не е јасно дали", "не може да се утврди",
                           "изворите не се согласуваат", "останува нејасно",
                           "нема доволно податоци", "тешко е да се каже"],
        "attribution_patterns": ["напиша", "тврдеше", "забележа", "нагласи",
                                 "реконструираше", "истакна", "укажа"],
    },
    "lt": {
        "hedge_value": ["apie {v}", "maždaug {v}", "beveik {v}",
                        "apytiksliai {v}", "apie {v} ar daugiau"],
        "hedge_prose": ["keli", "keletas", "daug", "kai kurie", "nemažai",
                        "šiek tiek"],
        "foreign_tokens": ["apie", "maždaug", "beveik", "about",
                           "roughly", "approximately", "several", "some"],
        "aporia_phrases": ["neaišku, ar", "negalima nustatyti",
                           "šaltiniai nesutaria", "lieka neaišku",
                           "duomenų nepakanka", "tikslus skaičius nežinomas"],
        "attribution_patterns": ["rašė", "teigė", "pažymėjo", "pabrėžė",
                                 "argumentavo", "rekonstravo"],
    },
    "lv": {
        "hedge_value": ["apmēram {v}", "aptuveni {v}", "gandrīz {v}", "ap {v}",
                        "vairāk nekā {v}"],
        "hedge_prose": ["daži", "vairāki", "daudzi", "nedaudz", "daļa", "kāds"],
        "foreign_tokens": ["apmēram", "aptuveni", "gandrīz", "about",
                           "roughly", "approximately", "several", "some"],
        "aporia_phrases": ["nav skaidrs, vai", "nevar noteikt",
                           "avoti nesakrīt", "paliek neskaidrs",
                           "trūkst datu", "precīzs skaits nav zināms"],
        "attribution_patterns": ["rakstīja", "apgalvoja", "atzīmēja", "uzsvēra",
                                 "argumentēja", "rekonstruēja"],
    },
    "et": {
        "hedge_value": ["umbes {v}", "ligikaudu {v}", "peaaegu {v}", "ligi {v}",
                        "üle {v}"],
        "hedge_prose": ["mõned", "mitmed", "paljud", "veidi", "osa", "vähesed"],
        "foreign_tokens": ["umbes", "ligikaudu", "peaaegu", "about",
                           "roughly", "approximately", "several", "some"],
        "aporia_phrases": ["pole selge, kas", "ei ole võimalik kindlaks teha",
                           "allikad ei ole ühel meelel", "jääb ebaselgeks",
                           "andmeid napib", "täpne arv on teadmata"],
        "attribution_patterns": ["kirjutas", "väitis", "märkis", "rõhutas",
                                 "argumenteeris", "rekonstrueeris"],
    },
    "hu": {
        "hedge_value": ["körülbelül {v}", "nagyjából {v}", "megközelítőleg {v}",
                        "mintegy {v}", "hozzávetőleg {v}"],
        "hedge_prose": ["néhány", "több", "sok", "kevés", "egyesek", "számos"],
        "foreign_tokens": ["körülbelül", "nagyjából", "megközelítőleg", "about",
                           "roughly", "approximately", "several", "some"],
        "aporia_phrases": ["nem világos, hogy", "nem állapítható meg",
                           "a források nem egyeznek", "bizonytalan marad",
                           "hiányoznak az adatok", "a pontos szám ismeretlen"],
        "attribution_patterns": ["írta", "állította", "megjegyezte",
                                 "hangsúlyozta", "érvelt", "rekonstruálta"],
    },
    "ro": {
        "hedge_value": ["aproximativ {v}", "circa {v}", "în jur de {v}",
                        "cam {v}", "aproape {v}"],
        "hedge_prose": ["câțiva", "mai mulți", "mulți", "puțini", "unii",
                        "câteva"],
        "foreign_tokens": ["aproximativ", "circa", "în jur de", "about",
                           "roughly", "approximately", "several", "some"],
        "aporia_phrases": ["nu este clar dacă", "nu se poate stabili",
                           "sursele nu sunt de acord", "rămâne neclar",
                           "lipsesc datele", "numărul exact este necunoscut"],
        "attribution_patterns": ["a scris", "a susținut", "a notat",
                                 "a subliniat", "a argumentat", "a reconstituit"],
    },
    "sv": {
        "hedge_value": ["ungefär {v}", "cirka {v}", "omkring {v}", "runt {v}",
                        "uppskattningsvis {v}"],
        "hedge_prose": ["flera", "många", "några", "ett fåtal", "en del",
                        "diverse"],
        "foreign_tokens": ["about", "roughly", "approximately", "around",
                           "several", "many", "some", "few"],
        "aporia_phrases": ["det är oklart om", "det går inte att fastställa",
                           "källorna är oense", "det är osäkert huruvida",
                           "uppgifterna varierar", "det saknas belägg för"],
        "attribution_patterns": ["skrev", "hävdade", "påpekade", "betonade",
                                 "noterade", "framhöll"],
    },
    "no": {
        "hedge_value": ["omtrent {v}", "cirka {v}", "rundt {v}", "omkring {v}",
                        "anslagsvis {v}"],
        "hedge_prose": ["flere", "mange", "noen", "et fåtall", "en del",
                        "diverse"],
        "foreign_tokens": ["about", "roughly", "approximately", "around",
                           "several", "many", "some", "few"],
        "aporia_phrases": ["det er uklart om", "det lar seg ikke fastslå",
                           "kildene er uenige", "det er usikkert hvorvidt",
                           "opplysningene varierer", "det mangler belegg for"],
        "attribution_patterns": ["skrev", "hevdet", "påpekte", "understreket",
                                 "bemerket", "framholdt"],
    },
    "da": {
        "hedge_value": ["omkring {v}", "cirka {v}", "omtrent {v}",
                        "rundt regnet {v}", "skønsmæssigt {v}"],
        "hedge_prose": ["flere", "mange", "nogle", "et fåtal", "en del",
                        "diverse"],
        "foreign_tokens": ["about", "roughly", "approximately", "around",
                           "several", "many", "some", "few"],
        "aporia_phrases": ["det er uklart om", "det kan ikke fastslås",
                           "kilderne er uenige", "det er usikkert hvorvidt",
                           "oplysningerne varierer", "der mangler belæg for"],
        "attribution_patterns": ["skrev", "hævdede", "påpegede", "understregede",
                                 "bemærkede", "fremhævede"],
    },
    "fi": {
        "hedge_value": ["noin {v}", "suunnilleen {v}", "arviolta {v}",
                        "likimain {v}", "karkeasti {v}"],
        "hedge_prose": ["useita", "monia", "joitakin", "muutamia", "jokunen",
                        "eräitä"],
        "foreign_tokens": ["about", "roughly", "approximately", "around",
                           "several", "many", "some", "few"],
        "aporia_phrases": ["on epäselvää onko", "ei voida määrittää",
                           "lähteet ovat eri mieltä", "on epävarmaa",
                           "tiedot vaihtelevat", "asiasta ei ole näyttöä"],
        "attribution_patterns": ["kirjoitti", "väitti", "huomautti", "korosti",
                                 "totesi", "painotti"],
    },
    "is": {
        "hedge_value": ["um það bil {v}", "u.þ.b. {v}", "í kringum {v}",
                        "nálægt {v}", "áætlað {v}"],
        "hedge_prose": ["nokkrir", "margir", "sumir", "fáeinir", "ýmsir",
                        "einhverjir"],
        "foreign_tokens": ["about", "roughly", "approximately", "around",
                           "several", "many", "some", "few"],
        "aporia_phrases": ["óljóst er hvort", "ekki verður staðfest",
                           "heimildir eru ósammála", "óvíst er hvort",
                           "upplýsingar eru misvísandi", "skortur er á heimildum"],
        "attribution_patterns": ["skrifaði", "hélt fram", "benti á",
                                 "lagði áherslu á", "tók fram", "áréttaði"],
    },
    "el": {
        "hedge_value": ["περίπου {v}", "γύρω στα {v}", "σχεδόν {v}",
                        "κατά προσέγγιση {v}", "πάνω από {v}"],
        "hedge_prose": ["μερικοί", "αρκετοί", "πολλοί", "λίγοι", "κάποιοι",
                        "ορισμένοι"],
        "foreign_tokens": ["about", "roughly", "approximately", "several",
                           "many", "some", "περίπου", "σχεδόν"],
        "aporia_phrases": ["δεν είναι σαφές αν", "δεν μπορεί να προσδιοριστεί",
                           "οι πηγές διαφωνούν", "παραμένει αβέβαιο",
                           "δεν υπάρχει συναίνεση", "είναι αμφισβητούμενο"],
        "attribution_patterns": ["έγραψε", "υποστήριξε", "σημείωσε", "τόνισε",
                                 "ανασυνέθεσε", "επισήμανε"],
    },
    "ga": {
        "hedge_value": ["thart ar {v}", "timpeall {v}", "beagnach {v}",
                        "níos mó ná {v}", "os cionn {v}"],
        "hedge_prose": ["roinnt", "cúpla", "go leor", "beagán", "roinnt mhaith",
                        "mórán"],
        "foreign_tokens": ["about", "roughly", "approximately", "several",
                           "many", "some", "thart", "timpeall"],
        "aporia_phrases": ["ní léir an", "ní féidir a chinneadh",
                           "níl na foinsí ar aon fhocal", "tá sé fós éiginnte",
                           "níl aon chomhaontú ann", "tá sé conspóideach"],
        "attribution_patterns": ["scríobh", "d'áitigh", "thug faoi deara",
                                 "chuir béim ar", "d'athchruthaigh", "léirigh"],
    },
    "cy": {
        "hedge_value": ["tua {v}", "oddeutu {v}", "bron {v}", "yn agos at {v}",
                        "dros {v}"],
        "hedge_prose": ["ychydig", "nifer", "sawl", "llawer", "rhai", "peth"],
        "foreign_tokens": ["about", "roughly", "approximately", "several",
                           "many", "some", "tua", "oddeutu"],
        "aporia_phrases": ["nid yw'n glir a", "ni ellir penderfynu",
                           "mae'r ffynonellau'n anghytuno", "mae'n aros yn ansicr",
                           "nid oes consensws", "mae'n destun dadl"],
        "attribution_patterns": ["ysgrifennodd", "dadleuodd", "nododd",
                                 "pwysleisiodd", "ail-lunio", "sylwodd"],
    },
    "mt": {
        "hedge_value": ["madwar {v}", "kważi {v}", "bejn wieħed u ieħor {v}",
                        "aktar minn {v}", "iktar minn {v}"],
        "hedge_prose": ["ftit", "diversi", "ħafna", "xi", "numru", "bosta"],
        "foreign_tokens": ["about", "roughly", "approximately", "several",
                           "many", "some", "madwar", "kważi"],
        "aporia_phrases": ["mhux ċar jekk", "ma jistax jiġi determinat",
                           "is-sorsi ma jaqblux", "għadu inċert",
                           "m'hemm l-ebda kunsens", "huwa kkontestat"],
        "attribution_patterns": ["kiteb", "argumenta", "nnota", "enfasizza",
                                 "irrikostrwixxa", "innutat"],
    },
    "sq": {
        "hedge_value": ["rreth {v}", "afërsisht {v}", "përafërsisht {v}",
                        "gati {v}", "më shumë se {v}"],
        "hedge_prose": ["disa", "shumë", "ca", "pak", "të tjerë", "një numër"],
        "foreign_tokens": ["about", "roughly", "approximately", "several",
                           "many", "some", "rreth", "afërsisht"],
        "aporia_phrases": ["nuk është e qartë nëse", "nuk mund të përcaktohet",
                           "burimet nuk pajtohen", "mbetet e pasigurt",
                           "nuk ka konsensus", "është e kontestuar"],
        "attribution_patterns": ["shkroi", "argumentoi", "vuri në dukje",
                                 "theksoi", "rindërtoi", "vërejti"],
    },

# --- v3 non-Latin + missing tier (51 langs, af, am, az, bn, bo, dz, eo, fa, gu, ha, haw, he, hy, ig, ka, kk, km, kn, ku, ky, la, lo, mi, ml, mn, mr, my, ne, nn, ny, or, pa, prs, ps, rw, sd, si, sm, sn, so, sw, ta, te, tr, tt, ug, ur, uz, xh, yo, zu) ---
    "bn": {
        "hedge_value": "প্রায় {v}",
        "hedge_prose": "বেশ কয়েকটি",
        "foreign_tokens": {
        "about": "প্রায়",
        "roughly": "মোটামুটি",
        "approximately": "আনুমানিক",
        "several": "বেশ কয়েকটি",
        "many": "অনেক",
        "some": "কিছু",
        "few": "কয়েকটি",
        "around": "প্রায়",
        },
        "aporia": re.compile(r"(?i)(?:সূত্রে উল্লেখ নেই|কোনো নথিভুক্ত তথ্য পাওয়া যায়নি|নির্দিষ্টভাবে জানা যায়নি|নিশ্চিতভাবে নির্ধারণ করা সম্ভব নয়|তথ্যসূত্রে এ বিষয়ে কিছু বলা হয়নি)"),
    },
    "ta": {
        "hedge_value": "சுமார் {v}",
        "hedge_prose": "சில",
        "foreign_tokens": {
        "about": "சுமார்",
        "roughly": "தோராயமாக",
        "approximately": "ஏறத்தாழ",
        "several": "பல",
        "many": "பல",
        "some": "சில",
        "few": "ஒரு சில",
        "around": "சுமார்",
        },
        "aporia": re.compile(r"(?i)(?:ஆதாரங்கள் குறிப்பிடவில்லை|பதிவு எதுவும் இல்லை|உறுதிப்படுத்த முடியவில்லை|தகவல் கிடைக்கவில்லை|சான்றுகள் இல்லை)"),
    },
    "te": {
        "hedge_value": "సుమారు {v}",
        "hedge_prose": "కొన్ని",
        "foreign_tokens": {
        "about": "సుమారు",
        "roughly": "సుమారుగా",
        "approximately": "దాదాపు",
        "several": "పలు",
        "many": "అనేక",
        "some": "కొన్ని",
        "few": "కొద్ది",
        "around": "సుమారు",
        },
        "aporia": re.compile(r"(?i)(?:మూలాధారాలలో పేర్కొనబడలేదు|ఎటువంటి రికార్డు లేదు|నిర్ధారించలేము|ఆధారాలు అందుబాటులో లేవు|సమాచారం లభ్యం కాలేదు)"),
    },
    "mr": {
        "hedge_value": "सुमारे {v}",
        "hedge_prose": "काही",
        "foreign_tokens": {
        "about": "सुमारे",
        "roughly": "अंदाजे",
        "approximately": "अंदाजे",
        "several": "अनेक",
        "many": "बरेच",
        "some": "काही",
        "few": "थोडे",
        "around": "सुमारे",
        },
        "aporia": re.compile(r"(?i)(?:स्रोतांमध्ये नमूद नाही|याबाबत कोणतीही नोंद नाही|निश्चितपणे सांगता येत नाही|उपलब्ध माहितीत आढळत नाही|याची पुष्टी होऊ शकत नाही)"),
    },
    "gu": {
        "hedge_value": "આશરે {v}",
        "hedge_prose": "કેટલાક",
        "foreign_tokens": {
        "about": "આશરે",
        "roughly": "લગભગ",
        "approximately": "અંદાજે",
        "several": "કેટલાક",
        "many": "ઘણા",
        "some": "કેટલાક",
        "few": "થોડા",
        "around": "આસપાસ",
        },
        "aporia": re.compile(r"(?i)(?:સ્રોતોમાં ઉલ્લેખ નથી|કોઈ નોંધ ઉપલબ્ધ નથી|નિશ્ચિતપણે કહી શકાય તેમ નથી|માહિતી ઉપલબ્ધ નથી|આ અંગે કોઈ પુષ્ટિ નથી)"),
    },
    "kn": {
        "hedge_value": "ಸುಮಾರು {v}",
        "hedge_prose": "ಹಲವಾರು",
        "foreign_tokens": {
        "about": "ಸುಮಾರು",
        "roughly": "ಅಂದಾಜು",
        "approximately": "ಸರಿಸುಮಾರು",
        "several": "ಹಲವಾರು",
        "many": "ಹಲವು",
        "some": "ಕೆಲವು",
        "few": "ಕೆಲವೇ",
        "around": "ಸುಮಾರು",
        },
        "aporia": re.compile(r"(?i)(?:ಮೂಲಗಳಲ್ಲಿ ಉಲ್ಲೇಖವಿಲ್ಲ|ದಾಖಲೆ ಲಭ್ಯವಿಲ್ಲ|ಖಚಿತಪಡಿಸಲು ಸಾಧ್ಯವಿಲ್ಲ|ಈ ಬಗ್ಗೆ ಮಾಹಿತಿ ಇಲ್ಲ|ಸ್ಪಷ್ಟ ವಿವರ ದೊರೆತಿಲ್ಲ)"),
    },
    "ml": {
        "hedge_value": "ഏകദേശം {v}",
        "hedge_prose": "ചില",
        "foreign_tokens": {
        "about": "ഏകദേശം",
        "roughly": "ഏകദേശം",
        "approximately": "ഏകദേശം",
        "several": "പലതും",
        "many": "പല",
        "some": "ചില",
        "few": "കുറച്ച്",
        "around": "ഏകദേശം",
        },
        "aporia": re.compile(r"(?i)(?:ഉറവിടങ്ങളിൽ പരാമർശിച്ചിട്ടില്ല|രേഖകളൊന്നും ലഭ്യമല്ല|നിർണ്ണയിക്കാൻ കഴിയില്ല|വ്യക്തമായ വിവരമില്ല|സ്ഥിരീകരിക്കാൻ സാധിച്ചിട്ടില്ല)"),
    },
    "pa": {
        "hedge_value": "ਲਗਭਗ {v}",
        "hedge_prose": "ਕਈ",
        "foreign_tokens": {
        "about": "ਲਗਭਗ",
        "roughly": "ਤਕਰੀਬਨ",
        "approximately": "ਲਗਭਗ",
        "several": "ਕਈ",
        "many": "ਬਹੁਤ ਸਾਰੇ",
        "some": "ਕੁਝ",
        "few": "ਕੁਝ ਕੁ",
        "around": "ਲਗਭਗ",
        },
        "aporia": re.compile(r"(?i)(?:ਸਰੋਤਾਂ ਵਿੱਚ ਇਸ ਦਾ ਜ਼ਿਕਰ ਨਹੀਂ ਹੈ|ਕੋਈ ਰਿਕਾਰਡ ਉਪਲਬਧ ਨਹੀਂ ਹੈ|ਇਹ ਨਿਰਧਾਰਤ ਨਹੀਂ ਕੀਤਾ ਜਾ ਸਕਦਾ|ਸਰੋਤ ਇਸ ਬਾਰੇ ਚੁੱਪ ਹਨ|ਜਾਣਕਾਰੀ ਉਪਲਬਧ ਨਹੀਂ ਹੈ|ਪੁਸ਼ਟੀ ਨਹੀਂ ਹੋ ਸਕੀ)"),
    },
    "or": {
        "hedge_value": "ପ୍ରାୟ {v}",
        "hedge_prose": "କେତେକ",
        "foreign_tokens": {
        "about": "ପ୍ରାୟ",
        "roughly": "ପ୍ରାୟତଃ",
        "approximately": "ଆନୁମାନିକ",
        "several": "କେତେକ",
        "many": "ଅନେକ",
        "some": "କିଛି",
        "few": "ଅଳ୍ପ",
        "around": "ପ୍ରାୟ",
        },
        "aporia": re.compile(r"(?i)(?:ଉତ୍ସରେ ଉଲ୍ଲେଖ ନାହିଁ|କୌଣସି ରେକର୍ଡ ନାହିଁ|ନିର୍ଣ୍ଣୟ କରାଯାଇପାରିବ ନାହିଁ|ତଥ୍ୟ ଉପଲବ୍ଧ ନାହିଁ|ସୂତ୍ରରେ କୁହାଯାଇନାହିଁ|ସ୍ପଷ୍ଟ ନୁହେଁ)"),
    },
    "si": {
        "hedge_value": "දළ වශයෙන් {v}",
        "hedge_prose": "කිහිපයක්",
        "foreign_tokens": {
        "about": "දළ වශයෙන්",
        "roughly": "දළ වශයෙන්",
        "approximately": "ආසන්න වශයෙන්",
        "several": "කිහිපයක්",
        "many": "බොහෝ",
        "some": "සමහර",
        "few": "ස්වල්පයක්",
        "around": "පමණ",
        },
        "aporia": re.compile(r"(?i)(?:මූලාශ්‍රවල සඳහන් නොවේ|වාර්තා නොමැත|නිශ්චය කළ නොහැක|තොරතුරු නොමැත|සනාථ කළ නොහැක|ලේඛනගත කර නොමැත)"),
    },
    "ne": {
        "hedge_value": "लगभग {v}",
        "hedge_prose": "केही",
        "foreign_tokens": {
        "about": "लगभग",
        "roughly": "करिब",
        "approximately": "अनुमानित रूपमा",
        "several": "थुप्रै",
        "many": "धेरै",
        "some": "केही",
        "few": "थोरै",
        "around": "करिब",
        },
        "aporia": re.compile(r"(?i)(?:स्रोतहरूमा उल्लेख छैन|कुनै अभिलेख छैन|यकिन गर्न सकिँदैन|प्रमाणित तथ्य उपलब्ध छैन|स्पष्ट जानकारी पाइँदैन)"),
    },
    "sd": {
        "hedge_value": "لڳ ڀڳ {v}",
        "hedge_prose": "ڪيترائي",
        "foreign_tokens": {
        "about": "لڳ ڀڳ",
        "roughly": "اٽڪل",
        "approximately": "تقريباً",
        "several": "ڪيترائي",
        "many": "گھڻا",
        "some": "ڪجھ",
        "few": "ٿورا",
        "around": "لڳ ڀڳ",
        },
        "aporia": re.compile(r"(?i)(?:ذريعن ۾ ذڪر ناهي|ڪوبه رڪارڊ موجود ناهي|طئي نه ٿي سگهيو|معلوم ٿي نه سگهيو|ذريعا خاموش آهن|ڪا به تصديق موجود ناهي)"),
    },
    "he": {
        "hedge_value": "כ-{v}",
        "hedge_prose": "כמה",
        "foreign_tokens": {
        "about": "כ-",
        "roughly": "בערך",
        "approximately": "בקירוב",
        "several": "כמה",
        "many": "רבים",
        "some": "מספר",
        "few": "מעטים",
        "around": "סביב",
        },
        "aporia": re.compile(r"(?i)(?:המקורות אינם מציינים|אין תיעוד לכך|לא ניתן לקבוע|אין מידע זמין|לא נמצא תיעוד|הפרטים אינם ידועים)"),
    },
    "fa": {
        "hedge_value": "حدود {v}",
        "hedge_prose": "چندین",
        "foreign_tokens": {
        "about": "حدود",
        "roughly": "تقریباً",
        "approximately": "تقریباً",
        "several": "چندین",
        "many": "بسیاری",
        "some": "برخی",
        "few": "چند",
        "around": "حدود",
        },
        "aporia": re.compile(r"(?i)(?:منابع در این باره چیزی نمی‌گویند|هیچ سندی در این زمینه موجود نیست|قابل تعیین نیست|اطلاعات موثقی در دست نیست|در منابع ذکری از آن نشده است|نمی‌توان با قطعیت گفت)"),
    },
    "ur": {
        "hedge_value": "تقریباً {v}",
        "hedge_prose": "کئی",
        "foreign_tokens": {
        "about": "تقریباً",
        "roughly": "تقریباً",
        "approximately": "تقریباً",
        "several": "کئی",
        "many": "بہت سے",
        "some": "کچھ",
        "few": "چند",
        "around": "لگ بھگ",
        },
        "aporia": re.compile(r"(?i)(?:ذرائع اس بارے میں خاموش ہیں|کوئی ریکارڈ دستیاب نہیں|اس کا تعین ممکن نہیں|معلومات دستیاب نہیں ہیں|ذرائع میں اس کا ذکر نہیں ملتا)"),
    },
    "ps": {
        "hedge_value": "شاوخوا {v}",
        "hedge_prose": "څو",
        "foreign_tokens": {
        "about": "شاوخوا",
        "roughly": "نږدې",
        "approximately": "تقریباً",
        "several": "څو",
        "many": "ډېر",
        "some": "ځینې",
        "few": "لږ",
        "around": "شاوخوا",
        },
        "aporia": re.compile(r"(?i)(?:سرچينې پدې اړه څه نه وايي|کوم ثبت شوی مالومات نشته|دا نه شي ټاکل کېدی|په دې اړه معلومات شتون نلري|سرچينې خاموشې دي|دقيق شمېر نه دی معلوم)"),
    },
    "ku": {
        "hedge_value": "نزیکەی {v}",
        "hedge_prose": "چەند",
        "foreign_tokens": {
        "about": "نزیکەی",
        "roughly": "بە نزیکەیی",
        "approximately": "نزیکەی",
        "several": "چەند",
        "many": "زۆر",
        "some": "هەندێک",
        "few": "کەم",
        "around": "نزیکەی",
        },
        "aporia": re.compile(r"(?i)(?:سەرچاوەکان ئاماژەیان پێ نەکردووە|هیچ تۆمارێک بەردەست نییە|ناتوانرێت دیاری بکرێت|زانیاری لەبەردەستدا نییە|بەڵگەیەک نەدۆزراوەتەوە)"),
    },
    "tr": {
        "hedge_value": "yaklaşık {v}",
        "hedge_prose": "birkaç",
        "foreign_tokens": {
        "about": "yaklaşık",
        "roughly": "kabaca",
        "approximately": "yaklaşık olarak",
        "several": "birkaç",
        "many": "birçok",
        "some": "bazı",
        "few": "az sayıda",
        "around": "civarında",
        },
        "aporia": re.compile(r"(?i)(?:kaynaklar bunu belirtmiyor|kayıt bulunmamaktadır|kesin olarak tespit edilememektedir|mevcut kaynaklarda yer almamaktadır|bu konuda bilgi bulunmamaktadır)"),
    },
    "az": {
        "hedge_value": "təxminən {v}",
        "hedge_prose": "bir neçə",
        "foreign_tokens": {
        "about": "təxminən",
        "roughly": "təxminən",
        "approximately": "təxminən",
        "several": "bir neçə",
        "many": "bir çox",
        "some": "bəzi",
        "few": "az sayda",
        "around": "təxminən",
        },
        "aporia": re.compile(r"(?i)(?:mənbələr bu barədə məlumat vermir|heç bir qeyd yoxdur|dəqiq müəyyən edilə bilmir|mənbələrdə göstərilmir|məlumat mövcud deyil)"),
    },
    "kk": {
        "hedge_value": "шамамен {v}",
        "hedge_prose": "бірнеше",
        "foreign_tokens": {
        "about": "шамамен",
        "roughly": "шамамен",
        "approximately": "шамамен",
        "several": "бірнеше",
        "many": "көптеген",
        "some": "кейбір",
        "few": "бірлі-жарым",
        "around": "шамамен",
        },
        "aporia": re.compile(r"(?i)(?:дереккөздерде айтылмаған|нақты дерек жоқ|деректер расталмаған|анықтау мүмкін емес|мәлімет келтірілмеген|жазба сақталмаған)"),
    },
    "uz": {
        "hedge_value": "taxminan {v}",
        "hedge_prose": "bir nechta",
        "foreign_tokens": {
        "about": "taxminan",
        "roughly": "taxminan",
        "approximately": "taxminan",
        "several": "bir nechta",
        "many": "ko'plab",
        "some": "ba'zi",
        "few": "bir necha",
        "around": "taxminan",
        },
        "aporia": re.compile(r"(?i)(?:manbalarda qayd etilmagan|aniq ma'lumot yo'q|hech qanday yozuv topilmadi|aniqlab bo'lmaydi|manbalar bu haqda jim|ma'lumot mavjud emas)"),
    },
    "ky": {
        "hedge_value": "болжол менен {v}",
        "hedge_prose": "бир нече",
        "foreign_tokens": {
        "about": "болжол менен",
        "roughly": "болжол менен",
        "approximately": "болжолдуу",
        "several": "бир нече",
        "many": "көптөгөн",
        "some": "айрым",
        "few": "аз сандагы",
        "around": "болжол менен",
        },
        "aporia": re.compile(r"(?i)(?:булактарда айтылган эмес|маалымат жок|так аныктоо мүмкүн эмес|документтерде белгиленбеген|тастыкталган маалымат жок|белгисиз бойдон калууда)"),
    },
    "tt": {
        "hedge_value": "якынча {v}",
        "hedge_prose": "берничә",
        "foreign_tokens": {
        "about": "якынча",
        "roughly": "якынча",
        "approximately": "якынча",
        "several": "берничә",
        "many": "күп",
        "some": "кайбер",
        "few": "аз",
        "around": "тирәсендә",
        },
        "aporia": re.compile(r"(?i)(?:чыганакларда әйтелми|мәгълүмат юк|төгәл билгеле түгел|чыганаклар бу турыда сөйләми|билгеләп булмый|мәгълүматлар табылмады)"),
    },
    "ug": {
        "hedge_value": "تەخمىنەن {v}",
        "hedge_prose": "بىرقانچە",
        "foreign_tokens": {
        "about": "تەخمىنەن",
        "roughly": "تەخمىنەن",
        "approximately": "تەخمىنەن",
        "several": "بىرقانچە",
        "many": "نۇرغۇن",
        "some": "بەزى",
        "few": "ئاز",
        "around": "ئەتراپىدا",
        },
        "aporia": re.compile(r"(?i)(?:مەنبەلەردە بۇ ھەقتە مەلۇمات يوق|خاتىرە تېپىلمىدى|ئېنىقلاش مۇمكىن ئەمەس|مەلۇمات مەۋجۇت ئەمەس|بۇ توغرىسىدا ھېچقانداق ئۇچۇر يوق)"),
    },
    "ka": {
        "hedge_value": "დაახლოებით {v}",
        "hedge_prose": "რამდენიმე",
        "foreign_tokens": {
        "about": "დაახლოებით",
        "roughly": "დაახლოებით",
        "approximately": "დაახლოებით",
        "several": "რამდენიმე",
        "many": "მრავალი",
        "some": "ზოგიერთი",
        "few": "მცირერიცხოვანი",
        "around": "დაახლოებით",
        },
        "aporia": re.compile(r"(?i)(?:წყაროები ამის შესახებ არაფერს ამბობენ|ჩანაწერი არ არსებობს|ვერ დგინდება|ინფორმაცია არ არის დაფიქსირებული|წყაროებში მონაცემები არ მოიპოვება|ზუსტი მონაცემები უცნობია)"),
    },
    "hy": {
        "hedge_value": "մոտավորապես {v}",
        "hedge_prose": "մի քանի",
        "foreign_tokens": {
        "about": "մոտավորապես",
        "roughly": "մոտավորապես",
        "approximately": "մոտավորապես",
        "several": "մի քանի",
        "many": "բազմաթիվ",
        "some": "որոշ",
        "few": "մի քանի",
        "around": "շուրջ",
        },
        "aporia": re.compile(r"(?i)(?:աղբյուրները չեն նշում|տվյալներ չկան|հնարավոր չէ հաստատել|տեղեկություններ չեն պահպանվել|ստույգ հայտնի չէ)"),
    },
    "mn": {
        "hedge_value": "ойролцоогоор {v}",
        "hedge_prose": "хэд хэдэн",
        "foreign_tokens": {
        "about": "ойролцоогоор",
        "roughly": "барагцаагаар",
        "approximately": "ойролцоогоор",
        "several": "хэд хэдэн",
        "many": "олон",
        "some": "зарим",
        "few": "цөөн",
        "around": "орчим",
        },
        "aporia": re.compile(r"(?i)(?:эх сурвалжид дурдаагүй|тодорхой мэдээлэл алга|тогтоох боломжгүй|баримт бичигт тэмдэглэгдээгүй|нотлох баримт байхгүй)"),
    },
    "bo": {
        "hedge_value": "ཕལ་ཆེར་ {v}",
        "hedge_prose": "འགའ་ཤས་",
        "foreign_tokens": {
        "about": "ཕལ་ཆེར་",
        "roughly": "ཕལ་ཆེར་",
        "approximately": "ཉེ་བར་",
        "several": "འགའ་ཤས་",
        "many": "མང་པོ་",
        "some": "ཁ་ཤས་",
        "few": "ཉུང་ཤས་",
        "around": "ཙམ་",
        },
        "aporia": re.compile(r"(?i)(?:འབྱུང་ཁུངས་ཀྱིས་གསལ་བཤད་མ་བྱས།|ཡིག་ཆ་གང་ཡང་མི་འདུག|ཐག་གཅོད་བྱེད་མི་ཐུབ།|གསལ་པོ་མི་འདུག|རྒྱུ་མཚན་མ་རྙེད།)"),
    },
    "dz": {
        "hedge_value": "ཉེ་བར་ {v}",
        "hedge_prose": "ལ་ལུ་",
        "foreign_tokens": {
        "about": "ཉེ་བར་",
        "roughly": "ཧ་ལམ་",
        "approximately": "ཕལ་ཆེར་",
        "several": "ལ་ལུ་",
        "many": "མང་རབས་",
        "some": "འགའ་ཤས་",
        "few": "ཉུང་ཤས་",
        "around": "ཉེ་འཁོར་",
        },
        "aporia": re.compile(r"(?i)(?:འབྱུང་ཁུངས་ཚུ་ནང་ལུ་བཀོད་དེ་མིན་འདུག|གནས་ཚུལ་ག་ནི་ཡང་མིན་འདུག|གསལ་བཀོད་འབད་མི་ཚུགས་པས|ཞིབ་ཕྲའི་གནས་ཚུལ་མེད|ཐག་བཅད་འབད་མི་ཚུགས་པས)"),
    },
    "my": {
        "hedge_value": "ခန့် {v}",
        "hedge_prose": "အနည်းငယ်",
        "foreign_tokens": {
        "about": "ခန့်",
        "roughly": "ခန့်မှန်းခြေအားဖြင့်",
        "approximately": "ခန့်မှန်းခြေ",
        "several": "အနည်းငယ်",
        "many": "များစွာ",
        "some": "အချို့",
        "few": "အနည်းငယ်",
        "around": "ခန့်",
        },
        "aporia": re.compile(r"(?i)(?:အရင်းအမြစ်များတွင် ဖော်ပြထားခြင်း မရှိပါ|မှတ်တမ်း မရှိပါ|အတည်ပြု၍ မရနိုင်ပါ|ရှင်းလင်းစွာ မသိရသေးပါ|အသေးစိတ် အချက်အလက် မရရှိသေးပါ)"),
    },
    "km": {
        "hedge_value": "ប្រហែល {v}",
        "hedge_prose": "ជាច្រើន",
        "foreign_tokens": {
        "about": "ប្រហែល",
        "roughly": "ប្រហែលជា",
        "approximately": "ប្រមាណ",
        "several": "ជាច្រើន",
        "many": "ច្រើន",
        "some": "មួយចំនួន",
        "few": "បន្តិចបន្តួច",
        "around": "ប្រហែល",
        },
        "aporia": re.compile(r"(?i)(?:ប្រភពមិនបានបញ្ជាក់|គ្មានកំណត់ត្រា|មិនអាចកំណត់បាន|ព័ត៌មានមិនមាន|ឯកសារមិនបានរៀបរាប់|មិនមានទិន្នន័យបញ្ជាក់)"),
    },
    "lo": {
        "hedge_value": "ປະມານ {v}",
        "hedge_prose": "ຫຼາຍ",
        "foreign_tokens": {
        "about": "ປະມານ",
        "roughly": "ປະມານ",
        "approximately": "ປະມານ",
        "several": "ຫຼາຍ",
        "many": "ຫຼວງຫຼາຍ",
        "some": "ບາງ",
        "few": "ຈຳນວນໜ້ອຍ",
        "around": "ປະມານ",
        },
        "aporia": re.compile(r"(?i)(?:ແຫຼ່ງຂໍ້ມູນບໍ່ໄດ້ລະບຸໄວ້|ບໍ່ມີບັນທຶກ|ບໍ່ສາມາດລະບຸໄດ້|ຍັງບໍ່ທັນມີຂໍ້ມູນຢືນຢັນ|ບໍ່ປາກົດໃນເອກະສານ)"),
    },
    "sw": {
        "hedge_value": "takriban {v}",
        "hedge_prose": "kadhaa",
        "foreign_tokens": {
        "about": "takriban",
        "roughly": "takriban",
        "approximately": "takriban",
        "several": "kadhaa",
        "many": "nyingi",
        "some": "baadhi",
        "few": "chache",
        "around": "takriban",
        },
        "aporia": re.compile(r"(?i)(?:vyanzo havisemi|hakuna kumbukumbu|haiwezi kubainika|hakuna taarifa zilizothibitishwa|haijulikani kwa uhakika|hakuna rekodi inayopatikana)"),
    },
    "am": {
        "hedge_value": "በግምት {v}",
        "hedge_prose": "በርካታ",
        "foreign_tokens": {
        "about": "ገደማ",
        "roughly": "በግምት",
        "approximately": "በግምት",
        "several": "በርካታ",
        "many": "ብዙ",
        "some": "አንዳንድ",
        "few": "ጥቂት",
        "around": "ገደማ",
        },
        "aporia": re.compile(r"(?i)(?:ምንጮቹ አይገልጹም|ማረጋገጥ አይቻልም|ምንም መዝገብ የለም|መረጃው አልተገኘም|በምንጮቹ ውስጥ አልተጠቀሰም|በእርግጠኝነት ማወቅ አይቻልም)"),
    },
    "ha": {
        "hedge_value": "kusan {v}",
        "hedge_prose": "da dama",
        "foreign_tokens": {
        "about": "kusan",
        "roughly": "kusan",
        "approximately": "kimanin",
        "several": "da dama",
        "many": "da yawa",
        "some": "wasu",
        "few": "kaɗan",
        "around": "kusan",
        },
        "aporia": re.compile(r"(?i)(?:majiyoyi ba su bayyana ba|babu tabbataccen bayani|ba a rubuta ba|ba a iya tantancewa ba|babu wani tarihi da ke tabbatar da hakan|bayanin bai fito fili ba)"),
    },
    "yo": {
        "hedge_value": "nǹkan bí {v}",
        "hedge_prose": "ọ̀pọ̀lọpọ̀",
        "foreign_tokens": {
        "about": "nǹkan bí",
        "roughly": "nǹkan bí",
        "approximately": "nǹkan bí",
        "several": "ọ̀pọ̀lọpọ̀",
        "many": "ọ̀pọ̀",
        "some": "díẹ̀",
        "few": "díẹ̀",
        "around": "nǹkan bí",
        },
        "aporia": re.compile(r"(?i)(?:àwọn orísun kò sọ|kò sí àkọsílẹ̀|a kò lè mọ̀ dájú|kò ṣe é fìdí rẹ̀ múlẹ̀|kò sí ẹ̀rí kankan|kò tí ì hàn kedere)"),
    },
    "ig": {
        "hedge_value": "ihe dị ka {v}",
        "hedge_prose": "ọtụtụ",
        "foreign_tokens": {
        "about": "ihe dị ka",
        "roughly": "ihe ruru",
        "approximately": "ihe dị ka",
        "several": "ọtụtụ",
        "many": "ọtụtụ",
        "some": "ụfọdụ",
        "few": "ole na ole",
        "around": "ihe dị ka",
        },
        "aporia": re.compile(r"(?i)(?:isi mmalite ekwughị|enweghị ndekọ|enweghị ike ịchọpụta|akụkọ ndị a ekwughị ya|ozi ọ bụla adịghị)"),
    },
    "zu": {
        "hedge_value": "cishe {v}",
        "hedge_prose": "ezimbalwa",
        "foreign_tokens": {
        "about": "cishe",
        "roughly": "cishe",
        "approximately": "cishe",
        "several": "ezimbalwa",
        "many": "eziningi",
        "some": "ezinye",
        "few": "ezimbalwa",
        "around": "cishe",
        },
        "aporia": re.compile(r"(?i)(?:imithombo ayikushongo lokhu|akukho mbhalo otholakalayo|akunakunqunywa|alukho ulwazi olutholakalayo|imininingwane ayitholakali|akuqinisekisiwe emithonjeni)"),
    },
    "xh": {
        "hedge_value": "malunga ne-{v}",
        "hedge_prose": "ezimbalwa",
        "foreign_tokens": {
        "about": "malunga ne",
        "roughly": "phantse",
        "approximately": "malunga ne",
        "several": "ezimbalwa",
        "many": "ezininzi",
        "some": "ezithile",
        "few": "ezimbalwa",
        "around": "malunga ne",
        },
        "aporia": re.compile(r"(?i)(?:imithombo ayitsho|akukho ngxelo|akunakumiselwa|ayaziwa|akukho lwazi lufumanekayo|ayichazwanga)"),
    },
    "af": {
        "hedge_value": "ongeveer {v}",
        "hedge_prose": "verskeie",
        "foreign_tokens": {
        "about": "ongeveer",
        "roughly": "ongeveer",
        "approximately": "ongeveer",
        "several": "verskeie",
        "many": "baie",
        "some": "sommige",
        "few": "enkele",
        "around": "ongeveer",
        },
        "aporia": re.compile(r"(?i)(?:die bronne meld nie|geen rekord bestaan nie|dit kan nie vasgestel word nie|die bronne swyg hieroor|daar is geen inligting beskikbaar nie|dit bly onbekend)"),
    },
    "so": {
        "hedge_value": "qiyaastii {v}",
        "hedge_prose": "dhowr",
        "foreign_tokens": {
        "about": "ku dhawaad",
        "roughly": "qiyaastii",
        "approximately": "qiyaas ahaan",
        "several": "dhowr",
        "many": "badan",
        "some": "qaar",
        "few": "yar",
        "around": "ku dhawaad",
        },
        "aporia": re.compile(r"(?i)(?:ilaha ma sheegin|diiwaan ma jiro|lama xaqiijin karo|macluumaad lama helin|wax caddayn ah lama hayo|ilaha ma xusin)"),
    },
    "rw": {
        "hedge_value": "hafi ya {v}",
        "hedge_prose": "bimwe muri byo",
        "foreign_tokens": {
        "about": "hafi ya",
        "roughly": "nko",
        "approximately": "hafi ya",
        "several": "byinshi",
        "many": "byinshi",
        "some": "bimwe",
        "few": "bike",
        "around": "hafi ya",
        },
        "aporia": re.compile(r"(?i)(?:inkomoko ntizibivuga|nta nyandiko ibihamya|ntibishobora kumenyekana|amakuru ntaboneka|nta gihamya kirahari)"),
    },
    "sn": {
        "hedge_value": "anenge {v}",
        "hedge_prose": "akati wandei",
        "foreign_tokens": {
        "about": "anenge",
        "roughly": "anenge",
        "approximately": "anosvika",
        "several": "akati wandei",
        "many": "akawanda",
        "some": "mamwe",
        "few": "mashoma",
        "around": "anenge",
        },
        "aporia": re.compile(r"(?i)(?:zvinyorwa hazvitauri|hapana chinyorwa|hazvigoni kuzivikanwa|hazvina kutaurwa|hapana ruzivo rwakawanikwa|zvakadzikama hazvizivikanwe)"),
    },
    "ny": {
        "hedge_value": "pafupifupi {v}",
        "hedge_prose": "zingapo",
        "foreign_tokens": {
        "about": "pafupifupi",
        "roughly": "pafupifupi",
        "approximately": "pafupifupi",
        "several": "zingapo",
        "many": "zambiri",
        "some": "zina",
        "few": "zochepa",
        "around": "pafupifupi",
        },
        "aporia": re.compile(r"(?i)(?:magwero sanena|palibe cholembedwa|sizingatheke kudziwa|palibe umboni|sizinatchulidwe|sizikudziwika)"),
    },
    "mi": {
        "hedge_value": "tata ki te {v}",
        "hedge_prose": "ētahi",
        "foreign_tokens": {
        "about": "tata ki",
        "roughly": "tata ki",
        "approximately": "tata ki",
        "several": "ētahi",
        "many": "he maha",
        "some": "ētahi",
        "few": "he torutoru",
        "around": "tata ki",
        },
        "aporia": re.compile(r"(?i)(?:kāore ngā puna kōrero e whakaatu|kāore he rēkoata|kāore e taea te whakatau|kāore i tuhia ki ngā puna|kāore he mōhiohio e wātea ana)"),
    },
    "sm": {
        "hedge_value": "pe tusa ma {v}",
        "hedge_prose": "nisi",
        "foreign_tokens": {
        "about": "pe tusa ma",
        "roughly": "pe tusa",
        "approximately": "pe tusa ma",
        "several": "nisi",
        "many": "tele",
        "some": "nisi",
        "few": "nai",
        "around": "pe tusa ma",
        },
        "aporia": re.compile(r"(?i)(?:e lē o taʻua e puna faamatalaga|e leai se faamaumauga|e lē mafai ona faamautinoaina|e leai se faamatalaga o maua|e lē o iloa mai puna faamatalaga)"),
    },
    "haw": {
        "hedge_value": "ma kahi o {v}",
        "hedge_prose": "kekahi mau",
        "foreign_tokens": {
        "about": "ma kahi o",
        "roughly": "ma kahi o",
        "approximately": "kokoke i",
        "several": "kekahi mau",
        "many": "he nui",
        "some": "kekahi",
        "few": "kakaikahi",
        "around": "ma kahi o",
        },
        "aporia": re.compile(r"(?i)(?:ʻaʻole ʻōlelo nā kumu|ʻaʻohe moʻolelo i mālama ʻia|ʻaʻole hiki ke hoʻoholo ʻia|ʻaʻole i hōʻike ʻia ma nā kumu|ʻaʻohe ʻike i loaʻa)"),
    },
    "prs": {
        "hedge_value": "تقریباً {v}",
        "hedge_prose": "چندین",
        "foreign_tokens": {
        "about": "تقریباً",
        "roughly": "تخمیناً",
        "approximately": "تقریباً",
        "several": "چندین",
        "many": "بسیاری",
        "some": "برخی",
        "few": "اندکی",
        "around": "در حدود",
        },
        "aporia": re.compile(r"(?i)(?:منابع در این باره چیزی نمی‌گویند|هیچ سندی در دست نیست|نمی‌توان با قطعیت تعیین کرد|اطلاعات موثقی موجود نیست|در منابع ثبت نشده است)"),
    },
    "nn": {
        "hedge_value": "om lag {v}",
        "hedge_prose": "fleire",
        "foreign_tokens": {
        "about": "om lag",
        "roughly": "grovt sett",
        "approximately": "tilnærma",
        "several": "fleire",
        "many": "mange",
        "some": "nokre",
        "few": "få",
        "around": "kring",
        },
        "aporia": re.compile(r"(?i)(?:kjeldene seier ikkje|det finst ikkje opplysningar om dette|ingen dokumentasjon føreligg|kan ikkje fastslåast|uklart ut frå kjeldene|ikkje kjent)"),
    },
    "eo": {
        "hedge_value": "proksimume {v}",
        "hedge_prose": "kelkaj",
        "foreign_tokens": {
        "about": "proksimume",
        "roughly": "malglate",
        "approximately": "proksimume",
        "several": "kelkaj",
        "many": "multaj",
        "some": "iuj",
        "few": "malmultaj",
        "around": "ĉirkaŭ",
        },
        "aporia": re.compile(r"(?i)(?:la fontoj ne diras|neniu registro ekzistas|ne eblas determini|la fontoj silentas pri tio|informoj ne haveblas|ne estas dokumentite)"),
    },
    "la": {
        "hedge_value": "circiter {v}",
        "hedge_prose": "nonnulli",
        "foreign_tokens": {
        "about": "circiter",
        "roughly": "fere",
        "approximately": "circiter",
        "several": "nonnulli",
        "many": "multi",
        "some": "aliqui",
        "few": "pauci",
        "around": "circa",
        },
        "aporia": re.compile(r"(?i)(?:fontes non tradunt|nullum exstat testimonium|definiri non potest|incertum manet|nihil in fontibus reperitur|auctores silent)"),
    },
}
for _lk, _lp in _EXTRA_PACKS.items():
    LANGUAGE_PACKS.setdefault(_lk, {})
    for _pk, _pv in _lp.items():
        LANGUAGE_PACKS[_lk].setdefault(_pk, _pv)


# ── PROVISIONAL language packs (batch 1, 2026-07-05). ───────────────────────────
# Structured seed data (aporia_phrases / attribution_patterns / hedge_templates /
# wpm / foreign_placeholder_blocklist / provisional flag). Compiled into the
# existing pack schema below via `_install_provisional`, then registered with
# `setdefault` so any hand-tuned entry above (en, id, plus the _EXTRA_PACKS
# languages already merged) survives untouched.
#
# CONSUMERS (spec refs):
#   - hedge_policy §3.4.4       → hedge_value / hedge_prose
#   - deterministic_counters §3.6 → aporia / attribution regex
#   - terminal_scan §3.5        → foreign_tokens blocklist
#   - TTS wpm                    → wpm dict (min/max derived from normal±)
#
# Missing-lang stays UNMEASURED (spec §3.4.4): that path is for langs below the
# coverage floor, NOT seeded entries with empty pattern lists. Seeded langs get
# real (if provisional) entries so counters don't degrade to UNMEASURED here.

_PROVISIONAL_SEEDS: dict[str, dict[str, Any]] = {
    "ban": {
        "aporia_phrases": ["nenten wenten sumber", "durung wenten cihna",
                           "tan kacatet ring babad"],
        "attribution_patterns": ["nyuratang", "maosang", "nyihnayang", "negesang"],
        "hedge_templates": ["sawetara", "kirang langkung", "watara"],
        "homographs": [],
        "wpm": {"slow": 130, "normal": 160, "fast": 190},
        "foreign_placeholder_blocklist": ["several", "around", "approximately",
                                          "roughly", "some", "about"],
        "provisional": True,
    },
    "min": {
        "aporia_phrases": ["indak ado sumber", "alun tacatek",
                           "indak tacatek dek sajarah"],
        "attribution_patterns": ["manulih", "mancatek", "manaruangkan", "mangecek"],
        "hedge_templates": ["lebiah kurang", "kiro-kiro", "sakitar"],
        "homographs": [],
        "wpm": {"slow": 130, "normal": 160, "fast": 190},
        "foreign_placeholder_blocklist": ["several", "around", "approximately",
                                          "roughly", "some", "about"],
        "provisional": True,
    },
    "ceb": {
        "aporia_phrases": ["walay tinubdan nga nag-ingon", "wala matala",
                           "dili masulbad"],
        "attribution_patterns": ["nagsulat", "nag-ingon", "nagpasabot", "nagbanabana"],
        "hedge_templates": ["mga", "banabana", "duol sa"],
        "homographs": [],
        "wpm": {"slow": 130, "normal": 160, "fast": 190},
        "foreign_placeholder_blocklist": ["several", "around", "approximately",
                                          "roughly", "some", "about"],
        "provisional": True,
    },
    "ilo": {
        "aporia_phrases": ["awan ti pagtaudan", "saan a nailanad",
                           "di pay narisut"],
        "attribution_patterns": ["insurat", "kinuna", "impatalged", "ninamnama"],
        "hedge_templates": ["agarup", "aginggana", "manipud"],
        "homographs": [],
        "wpm": {"slow": 130, "normal": 160, "fast": 190},
        "foreign_placeholder_blocklist": ["several", "around", "approximately",
                                          "roughly", "some", "about"],
        "provisional": True,
    },
    "war": {
        "aporia_phrases": ["waray tinikangan", "waray nahisurat",
                           "diri masabtan"],
        "attribution_patterns": ["nagsurat", "nagsiring", "nagpakita", "nagbanabana"],
        "hedge_templates": ["mga", "banabana", "harani"],
        "homographs": [],
        "wpm": {"slow": 130, "normal": 160, "fast": 190},
        "foreign_placeholder_blocklist": ["several", "around", "approximately",
                                          "roughly", "some", "about"],
        "provisional": True,
    },
    "cbk": {
        "aporia_phrases": ["no hay fuente", "no ta anota", "no puede resolve"],
        "attribution_patterns": ["ya escribi", "ya habla", "ya nota", "ya calcula"],
        "hedge_templates": ["mas o menos", "cerca de", "casi"],
        "homographs": [],
        "wpm": {"slow": 130, "normal": 160, "fast": 190},
        "foreign_placeholder_blocklist": ["several", "around", "approximately",
                                          "roughly", "some", "about"],
        "provisional": True,
    },
    "sk": {
        "aporia_phrases": ["nezachovalo sa", "pramene neuvádzajú",
                           "nemožno určiť"],
        "attribution_patterns": ["napísal", "tvrdí", "poznamenal", "odhaduje"],
        "hedge_templates": ["približne", "asi", "okolo"],
        "homographs": [],
        "wpm": {"slow": 130, "normal": 160, "fast": 190},
        "foreign_placeholder_blocklist": ["several", "around", "approximately",
                                          "roughly", "some", "about"],
        "provisional": True,
    },
    "hr": {
        "aporia_phrases": ["izvori ne bilježe", "ostaje neriješeno",
                           "nije zabilježeno"],
        "attribution_patterns": ["napisao je", "tvrdi", "primjećuje", "procjenjuje"],
        "hedge_templates": ["približno", "otprilike", "oko"],
        "homographs": [],
        "wpm": {"slow": 130, "normal": 160, "fast": 190},
        "foreign_placeholder_blocklist": ["several", "around", "approximately",
                                          "roughly", "some", "about"],
        "provisional": True,
    },
    "sl": {
        "aporia_phrases": ["viri ne poročajo", "ostaja nerešeno",
                           "ni zabeleženo"],
        "attribution_patterns": ["je zapisal", "trdi", "opaža", "ocenjuje"],
        "hedge_templates": ["približno", "okoli", "skoraj"],
        "homographs": [],
        "wpm": {"slow": 130, "normal": 160, "fast": 190},
        "foreign_placeholder_blocklist": ["several", "around", "approximately",
                                          "roughly", "some", "about"],
        "provisional": True,
    },
    "lt": {
        "aporia_phrases": ["šaltiniai nenurodo", "lieka neišspręsta",
                           "nėra užfiksuota"],
        "attribution_patterns": ["rašė", "teigia", "pažymėjo", "vertino"],
        "hedge_templates": ["apie", "maždaug", "beveik"],
        "homographs": [],
        "wpm": {"slow": 130, "normal": 160, "fast": 190},
        "foreign_placeholder_blocklist": ["several", "around", "approximately",
                                          "roughly", "some", "about"],
        "provisional": True,
    },
    "lv": {
        "aporia_phrases": ["avoti neatzīmē", "paliek neatrisināts",
                           "nav fiksēts"],
        "attribution_patterns": ["rakstīja", "apgalvo", "atzīmēja", "novērtēja"],
        "hedge_templates": ["aptuveni", "apmēram", "gandrīz"],
        "homographs": [],
        "wpm": {"slow": 130, "normal": 160, "fast": 190},
        "foreign_placeholder_blocklist": ["several", "around", "approximately",
                                          "roughly", "some", "about"],
        "provisional": True,
    },
    "et": {
        "aporia_phrases": ["allikad ei märgi", "jääb lahtiseks",
                           "pole talletatud"],
        "attribution_patterns": ["kirjutas", "väidab", "märkis", "hindas"],
        "hedge_templates": ["umbes", "ligikaudu", "peaaegu"],
        "homographs": [],
        "wpm": {"slow": 130, "normal": 160, "fast": 190},
        "foreign_placeholder_blocklist": ["several", "around", "approximately",
                                          "roughly", "some", "about"],
        "provisional": True,
    },
    "is": {
        "aporia_phrases": ["heimildir geta ekki", "óleyst spurning",
                           "ekki skráð"],
        "attribution_patterns": ["skrifaði", "heldur fram", "benti á", "áætlaði"],
        "hedge_templates": ["um það bil", "nálægt", "næstum"],
        "homographs": [],
        "wpm": {"slow": 130, "normal": 160, "fast": 190},
        "foreign_placeholder_blocklist": ["several", "around", "approximately",
                                          "roughly", "some", "about"],
        "provisional": True,
    },
    "ga": {
        "aporia_phrases": ["níl foinse ann", "fágtha gan réiteach",
                           "níor taifeadadh"],
        "attribution_patterns": ["scríobh", "áitíonn", "thug faoi deara", "mheas"],
        "hedge_templates": ["timpeall", "beagnach", "thart ar"],
        "homographs": [],
        "wpm": {"slow": 130, "normal": 160, "fast": 190},
        "foreign_placeholder_blocklist": ["several", "around", "approximately",
                                          "roughly", "some", "about"],
        "provisional": True,
    },
    "cy": {
        "aporia_phrases": ["nid oes ffynhonnell", "heb ei ddatrys",
                           "nid yw wedi'i gofnodi"],
        "attribution_patterns": ["ysgrifennodd", "dadleua", "nododd",
                                 "amcangyfrifodd"],
        "hedge_templates": ["tua", "bron", "oddeutu"],
        "homographs": [],
        "wpm": {"slow": 130, "normal": 160, "fast": 190},
        "foreign_placeholder_blocklist": ["several", "around", "approximately",
                                          "roughly", "some", "about"],
        "provisional": True,
    },
    "mt": {
        "aporia_phrases": ["l-ebda sors", "għadha mhux solvuta",
                           "mhux irreġistrat"],
        "attribution_patterns": ["kiteb", "isostni", "innota", "stima"],
        "hedge_templates": ["madwar", "kważi", "bejn wieħed u ieħor"],
        "homographs": [],
        "wpm": {"slow": 130, "normal": 160, "fast": 190},
        "foreign_placeholder_blocklist": ["several", "around", "approximately",
                                          "roughly", "some", "about"],
        "provisional": True,
    },
    "eu": {
        "aporia_phrases": ["iturriek ez dute", "argitu gabe", "ez da jaso"],
        "attribution_patterns": ["idatzi zuen", "dio", "nabarmendu zuen",
                                 "kalkulatu zuen"],
        "hedge_templates": ["gutxi gorabehera", "inguru", "ia"],
        "homographs": [],
        "wpm": {"slow": 130, "normal": 160, "fast": 190},
        "foreign_placeholder_blocklist": ["several", "around", "approximately",
                                          "roughly", "some", "about"],
        "provisional": True,
    },
    "ca": {
        "aporia_phrases": ["cap font indica", "resta sense resoldre", "no consta"],
        "attribution_patterns": ["va escriure", "argumenta", "va assenyalar",
                                 "va estimar"],
        "hedge_templates": ["aproximadament", "gairebé", "vora"],
        "homographs": [],
        "wpm": {"slow": 130, "normal": 160, "fast": 190},
        "foreign_placeholder_blocklist": ["several", "around", "approximately",
                                          "roughly", "some", "about"],
        "provisional": True,
    },
    "gl": {
        "aporia_phrases": ["ningunha fonte indica", "queda sen resolver",
                           "non consta"],
        "attribution_patterns": ["escribiu", "argumenta", "sinalou", "estimou"],
        "hedge_templates": ["aproximadamente", "case", "preto de"],
        "homographs": [],
        "wpm": {"slow": 130, "normal": 160, "fast": 190},
        "foreign_placeholder_blocklist": ["several", "around", "approximately",
                                          "roughly", "some", "about"],
        "provisional": True,
    },
}


def _install_provisional(seeds: dict[str, dict[str, Any]]) -> None:
    """Compile provisional structured seeds into the existing pack schema.
    setdefault semantics: any pre-existing key (en, id, or an _EXTRA_PACKS lang)
    is preserved verbatim — provisional data NEVER overrides curated entries."""
    for lk, seed in seeds.items():
        pack = LANGUAGE_PACKS.setdefault(lk, {})
        # aporia — union of phrase alternates (case-insensitive)
        if pack.get("aporia") is None and seed.get("aporia_phrases"):
            alts = "|".join(re.escape(p) for p in seed["aporia_phrases"] if p)
            if alts:
                pack.setdefault("aporia", re.compile(r"(?i)(?:" + alts + r")"))
        # attribution — NAME followed by any listed verb; also plain verb match
        if pack.get("attribution") is None and seed.get("attribution_patterns"):
            verbs = "|".join(re.escape(v) for v in seed["attribution_patterns"] if v)
            if verbs:
                pack.setdefault(
                    "attribution",
                    re.compile(
                        r"(?:\b(?P<name2>[A-Z][a-zA-Z.]+(?:\s+[A-Z][a-zA-Z.]+)*)\s+"
                        r"(?:" + verbs + r")\b)"
                    ),
                )
        # hedge_value / hedge_prose — first template = value form, second = prose
        tmpls = seed.get("hedge_templates") or []
        if pack.get("hedge_value") is None and tmpls:
            pack.setdefault("hedge_value", tmpls[0] + " {v}")
        if pack.get("hedge_prose") is None and tmpls:
            pack.setdefault("hedge_prose", tmpls[-1])
        # foreign_tokens — every blocklist entry mapped to "" (cut outright);
        # provisional packs have no vetted replacements yet.
        if pack.get("foreign_tokens") is None and seed.get("foreign_placeholder_blocklist"):
            pack.setdefault("foreign_tokens",
                            {t: "" for t in seed["foreign_placeholder_blocklist"]})
        # wpm — derive TTS min/max from normal ±15% (matches existing en/id shape)
        wpm = seed.get("wpm") or {}
        if pack.get("wpm") is None and wpm.get("normal"):
            _n = int(wpm["normal"])
            pack.setdefault("wpm", {"min": int(_n * 0.85), "max": int(_n * 1.15)})
        # homographs list (empty for provisional; consumers tolerate empty)
        if pack.get("homographs") is None:
            pack.setdefault("homographs", list(seed.get("homographs") or []))
        # keep the raw seed accessible for consumers that want the structured form
        pack.setdefault("provisional", bool(seed.get("provisional", True)))
        pack.setdefault("_seed", seed)


_install_provisional(_PROVISIONAL_SEEDS)


# ── Tier classification (Rino 2026-07-05). Consumers that need to distinguish
# native-quality from PROVISIONAL data (e.g. surface a "provisional coverage"
# badge in the UI, or gate a stricter counter) can read this constant rather
# than probing individual packs. `top20` = curated packs with real
# attribution/aporia regex + native hedge; `next40` = provisional seeds shipped
# in this batch; `tail40` = remaining served langs that stay UNMEASURED for
# counters until seeded.
LANGUAGE_PACK_TIER: dict[str, list[str]] = {
    "top20": ["en", "id", "ms", "jv", "su", "es", "fr", "de", "pt", "nl",
              "vi", "tl", "ar", "zh", "ja", "ko", "hi", "th"],
    "next40": sorted(_PROVISIONAL_SEEDS.keys()),
    "tail40": [],  # reserved for the next seeding batch
}


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


# Standalone integer, NOT part of a thousands group / decimal / range / percent. The
# trailing guard excludes a following digit or %, or a .,-/ that JOINS another digit
# ("1,825", "3,5", "1825-1830") — but NOT a plain sentence comma/period ("tahun 1825,"
# / "wafat 1855."), which the earlier over-broad guard wrongly skipped, leaving years as
# digits in video output.
_NUM_TOKEN_RX = re.compile(r"(?<![\d.,\-/])\b(\d{1,6})\b(?![\d%]|[.,\-/]\d)")


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


# Content-word stopset for the opening-motif scan (en+id common words length>=4 that
# carry no motif signal). Length<4 words are already dropped by the crude content filter.
_OPENING_STOP = {
    "that", "this", "were", "been", "have", "with", "then", "when", "what", "from",
    "they", "them", "their", "would", "could", "into", "over", "onto", "upon", "than",
    "also", "only", "just", "even", "such", "each", "very", "more", "most", "some",
    "here", "there", "will", "shall", "must", "does", "done", "same", "about", "after",
    "before", "again", "still",
    "yang", "dari", "untuk", "dengan", "tidak", "adalah", "akan", "sudah", "pada", "juga",
    "karena", "atau", "masih", "sedang", "dalam", "telah", "bahwa", "yaitu", "namun",
}
_OPENING_WORD_RX = re.compile(r"[A-Za-zÀ-ÿ]+")


def _opening_motif_scan(chapters: list[str]) -> dict:
    """Report-only: flag an atmospheric OPENING MOTIF reused across >=3 chapters (e.g.
    'rain/basement/radiator' opening most chapters). Generalizes the ID-only weather-opener
    regex to any content word, language-agnostic. Uses status 'FLAG' (NEVER 'OVER') so it
    can NEVER enter over_budget / trigger the diet loop. Never raises."""
    out = {"status": "PASS", "count": 0, "motifs": {}, "sentences": []}
    try:
        if len(chapters) < 3:
            return out
        by_word: dict[str, set] = {}
        word_sents: dict[str, list] = {}
        for ci, ch in enumerate(chapters):
            sents = _sentences(ch)
            if not sents:
                continue
            # opening = first sentence; strip heading glue (take the last line of the blob)
            opening = sents[0].strip().split("\n")[-1].strip()
            toks = [w.lower() for w in _OPENING_WORD_RX.findall(opening)]
            content = [w for w in toks if len(w) >= 4 and w not in _OPENING_STOP][:8]
            for w in set(content):
                by_word.setdefault(w, set()).add(ci)
                word_sents.setdefault(w, []).append(opening[:200])
        motifs = {w: len(chs) for w, chs in by_word.items() if len(chs) >= 3}
        if motifs:
            out["status"] = "FLAG"
            out["count"] = len(motifs)
            out["motifs"] = motifs
            seen_s: list[str] = []
            for w in motifs:
                for s in word_sents.get(w, [])[:2]:
                    if s not in seen_s:
                        seen_s.append(s)
            out["sentences"] = seen_s[:10]
        return out
    except Exception:  # noqa: BLE001 — a broken scan must never break generation
        return {"status": "PASS", "count": 0, "motifs": {}, "sentences": []}


def _names_from(m: re.Match) -> Optional[str]:
    gd = m.groupdict()
    return gd.get("name1") or gd.get("name2") or gd.get("name3")


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

        # Density budgets (citations/aporia/anchors/thesis) were calibrated on a
        # ~single-chapter Sapiens-length piece; a full 10-chapter book that keeps the same
        # whole-book budget forces a wasteful, LLM-imperfect diet loop that can't (and
        # shouldn't) gut a long-form piece's real scholarly texture (Rino 2026-07-04:
        # "scale budget by length"). Scale the budget with the manuscript's actual word
        # count against a baseline; epithet (0-tolerance re-intro) and scene_dup (not a
        # budget) are NOT scaled. Baseline configurable; scale never drops below the base.
        _mswords = len((text or "").split())
        _baseline = max(500, int(os.environ.get("NARASI_BUDGET_BASELINE_WORDS", "1500")))

        def _scaled(base):
            if base is None:
                return None
            return max(int(base), math.ceil(int(base) * max(1.0, _mswords / _baseline)))

        report["budget_scale"] = {"words": _mswords, "baseline": _baseline,
                                  "factor": round(max(1.0, _mswords / _baseline), 2)}

        # ── R-H2 citations ──
        cit_budget = _scaled(budgets.get("citations_max"))
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
        ap_budget = _scaled(budgets.get("aporia_max"))
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
            # Round-2 §5: a REPEATED aporia phrase is worse than the budget exceeded —
            # "Bukti yang tersedia tidak memungkinkan…" ×4 near-verbatim is a template,
            # not natural aporia. Same 4-gram in ≥2 aporia sentences → OVER regardless
            # of budget, with the repeated phrase named for the surgical prompt.
            repeated: list[str] = []
            if len(standalone) >= 2:
                seen_g: dict[str, int] = {}
                for _, s in standalone:
                    w = re.sub(r"[^\w' -]", " ", s.lower()).split()
                    grams = {" ".join(w[i:i + 4]) for i in range(max(0, len(w) - 3))}
                    for g in grams:
                        seen_g[g] = seen_g.get(g, 0) + 1
                repeated = [g for g, c in seen_g.items() if c >= 2][:5]
                if repeated and status != "OFF":
                    status = "OVER"
            report["counters"]["aporia"] = {
                "status": status, "count": len(standalone), "budget": ap_budget,
                "repeated_template": repeated,
                "sentences": [s[:200] for _, s in standalone],
            }

        # ── R-E3 epithet-once (SPEC v1 §3.6: WITHIN-DOCUMENT, TABLE-INDEPENDENT) ──
        # A RE-introduction is "Full Name, <epithet appositive>," for a surname the
        # manuscript has ALREADY introduced. Regression on the Aceh run: Reid ×5 with
        # full epithet on the ID path — because the old reintro regex was hardcoded
        # English "an/the". Now the appositive detector is the language pack's own
        # `epithet` regex, so any language whose pack defines it enforces R-E3 without
        # any per-project scholar table.
        if pack is None or pack.get("attribution") is None or pack.get("epithet") is None:
            report["counters"]["epithet"] = {"status": "UNMEASURED"}
        else:
            rx = pack["attribution"]
            epi_rx = pack["epithet"]   # pack's language-local appositive pattern
            # Match "Full Name" IMMEDIATELY followed by the pack-defined appositive.
            _full_name_rx = re.compile(r"\b([A-Z][a-zA-Z.]+(?:\s+[A-Z][a-zA-Z.]+)+)")
            seen: set[str] = set()
            violations = []
            for i, s in enumerate(sents):
                hits_full: list[str] = []
                for m in _full_name_rx.finditer(s):
                    tail = s[m.end():m.end() + 130]
                    epi_m = epi_rx.match(tail)
                    if epi_m:
                        hits_full.append(m.group(1))
                for full in hits_full:
                    surname = full.split()[-1]
                    if surname in seen:
                        violations.append((i, s))
                # register AFTER the check so a first-mention epithet is legal
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

        # ── same_scholar_max (SPEC §3.6, Aceh review): one scholar ≤4 total sentence
        # mentions (Carey ×10 / Reid ×5 class). Independent of R-E3 (which fires on
        # re-introductions with epithet); this fires on RAW sentence-count regardless
        # of whether an epithet accompanies the mention.
        if pack is not None and pack.get("attribution") is not None:
            rx = pack["attribution"]
            per_scholar: dict[str, int] = {}
            per_scholar_sents: dict[str, list] = {}
            for i, s in enumerate(sents):
                for m in rx.finditer(s):
                    n = _names_from(m)
                    if not n:
                        continue
                    surname = n.split()[-1]
                    per_scholar[surname] = per_scholar.get(surname, 0) + 1
                    per_scholar_sents.setdefault(surname, []).append(s[:180])
            ss_max = int(budgets.get("same_scholar_max", 4))
            over_sch = {s_: c for s_, c in per_scholar.items() if c > ss_max}
            report["counters"]["same_scholar"] = {
                "status": "OVER" if over_sch else "PASS",
                "budget": ss_max,
                "over": [{"scholar": s_, "count": c,
                          "sentences": per_scholar_sents.get(s_, [])[:6]}
                         for s_, c in over_sch.items()],
            }

            # ── debate_pairing_variance (SPEC §3.6, Aceh review): the SAME two scholars
            # may be staged against each other in ≤2 chapters. "Reid-vs-van 't Veer every
            # chapter" is a formula tell independent of citation count. A "pairing" is
            # two distinct scholar surnames co-occurring within one chapter.
            ch_pairs: dict[frozenset, set] = {}
            for ci, ch in enumerate(chapters):
                surnames_here: set[str] = set()
                for m in rx.finditer(ch):
                    n = _names_from(m)
                    if n:
                        surnames_here.add(n.split()[-1])
                if len(surnames_here) >= 2:
                    surnames_list = list(surnames_here)
                    for a in range(len(surnames_list)):
                        for b in range(a + 1, len(surnames_list)):
                            key = frozenset((surnames_list[a], surnames_list[b]))
                            ch_pairs.setdefault(key, set()).add(ci)
            dp_max = int(budgets.get("debate_pairing_max", 2))
            over_pairs = [(list(k), sorted(v)) for k, v in ch_pairs.items()
                          if len(v) > dp_max]
            report["counters"]["debate_pairing"] = {
                "status": "OVER" if over_pairs else "PASS",
                "budget": dp_max,
                "over": [{"scholars": p[0], "chapters": [c + 1 for c in p[1]]}
                         for p in over_pairs[:6]],
            }
        else:
            report["counters"]["same_scholar"] = {"status": "UNMEASURED"}
            report["counters"]["debate_pairing"] = {"status": "UNMEASURED"}

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
        # anchors are ABSOLUTE (the original narasi rule: 3 per manuscript, any length) —
        # deliberately NOT length-scaled; an anchor's power comes from scarcity.
        anchor_budget = int(budgets.get("anchors_max", 3))
        # count EVERY [ANCHOR] token (leading OR trailing — the Diponegoro run used both
        # "…badai. [ANCHOR]" and "[ANCHOR] Bagi yang tertindas…"); samples = their lines
        anchor_lines = [ln.strip()[:200] for ln in (text or "").splitlines()
                        if re.search(r"(?i)\[\s*anchor", ln)]
        report["counters"]["anchors"] = {
            "status": "OVER" if len(anchor_lines) > anchor_budget else "PASS",
            "count": len(anchor_lines), "budget": anchor_budget,
            "sentences": anchor_lines[:12],
        }

        # ── scene-dedup (ID-path §6.1): cross-chapter near-verbatim sensory beats.
        # Two deterministic nets: (a) 6-word shingles (true near-verbatim), (b) 4-word
        # shingles where EVERY word is ≥4 chars — catches the stamped template phrase
        # ("turun hampir setiap sore", "roda gerobak hingga poros") without firing on
        # function-word runs. OVER when any cross-chapter hit. ──
        dup_hits: list[str] = []
        if len(chapters) > 1:
            # spelled numbers/dates repeat legitimately across chapters ("tahun seribu
            # delapan ratus dua puluh lima") — a shingle dominated by number words is a
            # DATE, not a stamped scene template. Skip those.
            _NUMWORDS = {"satu", "dua", "tiga", "empat", "lima", "enam", "tujuh", "delapan",
                         "sembilan", "sepuluh", "sebelas", "puluh", "belas", "ratus", "ribu",
                         "juta", "seribu", "seratus", "tahun", "one", "two", "three", "four",
                         "five", "six", "seven", "eight", "nine", "ten", "hundred", "thousand"}

            # SPEC v1 §3.9 synonym-swap dedup: paraphrase-resistant scene detection —
            # "roda gerobak hingga poros" and "roda gerobak hingga as" share a template
            # even though the shingles differ. Canonicalize synonym groups so both forms
            # collapse to the same shingle. Grow the map from real reviews.
            _SYNONYM_CANON = {
                "poros": "as", "gandar": "as",             # mechanical axle
                "lumpur": "lumpur", "berlumpur": "lumpur",  # mud family
                "hujan": "hujan", "gerimis": "hujan",       # rain family
                "cermin": "cermin", "ceureumen": "cermin",  # Aceh loan
            }

            def _canon_word(w: str) -> str:
                return _SYNONYM_CANON.get(w, w)

            def _shingles(t: str) -> set:
                w0 = re.sub(r"[^\wàâéèêîôûáíóúäëïöü' -]", " ", t.lower()).split()
                w = [_canon_word(x) for x in w0]
                out = set()
                for i in range(max(0, len(w) - 5)):
                    six = w[i:i + 6]
                    if sum(1 for x in six if x in _NUMWORDS) < 3:
                        out.add(" ".join(six))
                for i in range(max(0, len(w) - 3)):
                    quad = w[i:i + 4]
                    if all(len(x) >= 4 for x in quad) and \
                       sum(1 for x in quad if x in _NUMWORDS) < 2:
                        out.add(" ".join(quad))
                return out
            seen_sh: dict[str, int] = {}
            for ci, ch in enumerate(chapters):
                for s in _sentences(ch):
                    if len(s.split()) < 5:
                        continue
                    for sh in _shingles(s):
                        prev = seen_sh.get(sh)
                        if prev is not None and prev != ci:
                            dup_hits.append(s.strip()[:200])
                            break
                        seen_sh.setdefault(sh, ci)
        # Weather-opener template (Diponegoro-1 AND -2 both stamped it): sentences that
        # OPEN with the same weather noun in ≥3 different chapters ("Hujan …" in Bab 4,
        # 5, 6, 7) are a formula even when the wording paraphrases past the shingles.
        if len(chapters) > 2:
            _wx = re.compile(r"(?i)^(hujan|udara|angin|kabut|debu|gerimis|matahari)\b")
            _wx_by: dict[str, set] = {}
            _wx_sents: dict[str, list] = {}
            for ci, ch in enumerate(chapters):
                for s in _sentences(ch):
                    # heading-glue: the chapter's first "sentence" carries the heading
                    # line — match the LAST line's start, not the glued blob's
                    s_clean = s.strip().split("\n")[-1].strip()
                    mwx = _wx.match(s_clean)
                    if mwx:
                        k = mwx.group(1).lower()
                        _wx_by.setdefault(k, set()).add(ci)
                        _wx_sents.setdefault(k, []).append(s_clean[:200])
            for k, chs in _wx_by.items():
                if len(chs) >= 3:
                    dup_hits.extend(_wx_sents[k][:4])

        dup_hits = list(dict.fromkeys(dup_hits))
        # floor: a stamped template shows up as a CLUSTER of repeats — 1-2 incidental
        # cross-chapter phrase matches in a long book are coincidence, not a formula, and
        # shouldn't drive the expensive diet loop. Threshold scales gently with length.
        _dup_floor = max(3, math.ceil(_mswords / 2000))
        report["counters"]["scene_dup"] = {
            "status": "OVER" if len(dup_hits) >= _dup_floor else "PASS",
            "count": len(dup_hits), "floor": _dup_floor, "sentences": dup_hits[:10],
        }

        # Opening-motif (report-only): same atmospheric opening word across >=3 chapters,
        # generalizing the ID-only weather-opener above to any language/content word.
        # status='FLAG' (never 'OVER') so it is EXCLUDED from over_budget and can NEVER
        # drive the diet loop — a repetitive-opening pattern is a generation-prompt fix,
        # not something the surgical sentence-editor can repair.
        report["counters"]["opening_motif"] = _opening_motif_scan(chapters)

        # ── anchor-voice (§6.3): a first-person [ANCHOR] inside third-person narration
        # reads as invented testimony ("Hutan adalah benteng kami…"). Skipped when the
        # whole manuscript is first-person narration (POV styles) — detected by pronoun
        # density, language-pack driven. ──
        _fp_rx = (pack or {}).get("first_person")
        if _fp_rx is None:
            _fp_rx = re.compile(r"(?i)\b(?:kami|kita|aku)\b") if (lang or "").startswith("id") \
                else re.compile(r"\b(?:we|our|us|I|my)\b")
        voice_hits: list[str] = []
        try:
            body_fp = len(_fp_rx.findall(text or ""))
            per_100_sent = body_fp / max(1, len(sents)) * 100
            # POV detector: only treat as first-person narration if BOTH the ratio is
            # high (>50%) AND we have enough absolute hits (>=5) — a third-person book
            # with a single "kami" anchor break in a short excerpt used to sail through
            # as "POV" because 1/3 sentences trip a 25% threshold.
            first_person_narration = per_100_sent > 50 and body_fp >= 5
            if not first_person_narration:
                # (a) explicit [ANCHOR]-tagged lines (marker still present)
                if anchor_lines:
                    voice_hits.extend(ln for ln in anchor_lines if _fp_rx.search(ln))
                # Round-3 review: the model NOW ships without brackets, but the
                # first-person voice-break survives ("Hutan adalah benteng kami…" as a
                # standalone paragraph). Detect the CONTENT shape: a short (≤20 word)
                # single-sentence paragraph that reads like a quotable line and carries
                # a first-person plural pronoun in a third-person book. Same failure
                # class, no marker required.
                for para in (text or "").split("\n\n"):
                    p = para.strip()
                    if not p or len(p.split()) > 20 or "\n" in p:
                        continue
                    if p.startswith(("#", ">", "- ", "* ", "|")):
                        continue
                    # single-sentence (has a terminator; not just a heading fragment)
                    if not re.search(r"[.!?…]\s*$", p):
                        continue
                    if _fp_rx.search(p) and p not in voice_hits:
                        voice_hits.append(p[:200])
        except Exception:  # noqa: BLE001
            pass
        # Grammar-broken anchor detector (round-3 review §craft): an anchor line where
        # the second clause opens with "maka + noun + relative-pronoun" is missing its
        # verb ("…, maka kepalsuan sebuah jabat tangan yang menyelesaikannya"). Report-
        # only — needs an editor pass.
        # "…, maka <noun-phrase> yang <verb+suffix>" is a nominal predicate that lost its
        # finite verb ("…gagal menundukkannya, maka kepalsuan sebuah jabat tangan yang
        # menyelesaikannya"). Missing-verb signature: after "maka/dan" comes a NP → yang
        # → verb-with-clitic, with no other finite verb between.
        _BROKEN_ANCHOR_RX = re.compile(
            r",\s+(?:maka|dan)\s+(?:\w+\s+){1,5}yang\s+[a-zA-Z]+(?:nya|kannya|kanya)\b")
        broken_grammar: list[str] = []
        # Scan short standalone paragraphs (anchor-shape) AND explicit anchor lines.
        _short_paras = []
        for para in (text or "").split("\n\n"):
            p = para.strip()
            if p and len(p.split()) <= 25 and "\n" not in p \
                    and not p.startswith(("#", ">", "- ", "* ", "|")) \
                    and re.search(r"[.!?…]\s*$", p):
                _short_paras.append(p)
        for cand in (anchor_lines + voice_hits + _short_paras):
            if _BROKEN_ANCHOR_RX.search(cand) and cand[:180] not in broken_grammar:
                broken_grammar.append(cand[:180])
        report["counters"]["anchor_voice"] = {
            "status": "OVER" if voice_hits else "PASS",
            "count": len(voice_hits), "sentences": voice_hits[:6],
            "broken_grammar": broken_grammar[:4],
        }

        # ── R-H6 thesis restatement (embed-based; UNMEASURED without thesis+embed) ──
        th_budget = _scaled(budgets.get("thesis_restatement_max"))
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
        _tmpl = (" REPEATED TEMPLATE detected (" + "; ".join(ap["repeated_template"]) +
                 ") — a repeated aporia phrase is WORSE than the budget: every kept aporia "
                 "must use a DIFFERENT construction.") if ap.get("repeated_template") else ""
        parts.append(
            f"APORIA CLOSERS: {ap['count']} stand-alone 'the sources do not record…'-class closers "
            f"exceed the budget of {ap['budget']}.{_tmpl} Keep the strongest {ap['budget']}; end the other "
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
    if c.get("anchor_voice", {}).get("status") == "OVER":
        av = c["anchor_voice"]
        parts.append(
            "ANCHOR VOICE: these [ANCHOR] lines use first-person voice inside third-person "
            "narration — they read as invented testimony. Rewrite each to the narrator's POV, "
            "or attribute it explicitly to a documented source, or mark it in-text as an "
            "imagined voice. Lines:\n- " + "\n- ".join(av.get("sentences", [])[:6]))
    if c.get("same_scholar", {}).get("status") == "OVER":
        ss = c["same_scholar"]
        _who = "; ".join(f"{o['scholar']} ×{o['count']}" for o in ss.get("over", [])[:3])
        parts.append(
            f"SAME SCHOLAR OVER-REPRESENTED: {_who} exceed the budget of {ss['budget']} sentence "
            "mentions per scholar. Keep the strongest instances; replace the rest with adjacent "
            "attribution names (or fold into unattributed prose). One scholar cited across every "
            "chapter is a formula tell.")
    if c.get("debate_pairing", {}).get("status") == "OVER":
        dp = c["debate_pairing"]
        _pairs = "; ".join(f"{'/'.join(o['scholars'])} in bab {o['chapters']}"
                            for o in dp.get("over", [])[:3])
        parts.append(
            f"DEBATE PAIRING VARIANCE: {_pairs}. The same two scholars staged against each "
            f"other in >{dp['budget']} chapters is a formula. Vary the pairing — introduce a "
            "different second voice, or fold one side into unattributed synthesis.")
    return "\n\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────
# Phase 2 (2026-07-05) — Nusantara language_pack augments, gated on DALANG_INFRA_FIXES=1.
# Seeded from corpus samples 17 (Babad Tanah Jawi, id) + 19 (Cepot Membangun Kahyangan, su)
# + 18 (Black Death, tl). Additions extend existing packs via setdefault; existing keys are
# never overwritten. Consumers of nusantara_vocab/wayang_vocab/filipino_historical_terms
# must guard reads on _PHASE2_ON() — flag-OFF preserves prior behavior byte-identically.
# ─────────────────────────────────────────────────────────────────────────

import os as _os_phase2_lp

def _PHASE2_ON() -> bool:
    """Same env knob as narasi_gate._INFRA_FIXES_ON. One flip activates the whole
    corpus-audit bundle across gate + counters + pakem."""
    return _os_phase2_lp.environ.get("DALANG_INFRA_FIXES") == "1"


# id — Nusantara chronicle vocabulary + classical Islamic closing formulas.
# Seeded from Babad Tanah Jawi audit (sample-17, lens-2). Extends existing id pack.
_ID_NUSANTARA_VOCAB = {
    "chronicle_registers": (
        "keraton", "pendapa", "cacah", "pusaka", "wahyu", "wahyu keprabon",
        "silsilah", "susuhunan", "kanjeng", "kanjeng ratu", "kanjeng sunan",
        "baginda", "sang prabu", "adipati", "abdi dalem", "kraman",
        "empunya cerita", "menurut empunya cerita", "menurut sesetengah riwayat",
    ),
    "chronicle_openers": (
        "Adapun", "Syahdan", "Hatta", "Alkisah",
        "Maka tersebutlah", "Tersebutlah pula", "Ada yang mengatakan",
    ),
    "classical_closing_formulas": (
        "Wallahu a'lam bissawab",
        "Wa Allahu a'lamu bishawab",
        "Wassalam",
    ),
    "wayang_dewa_names": (
        "Batara Guru", "Batara Narada", "Batara Brama", "Batara Wisnu",
        "Batara Indra", "Sang Hyang Ismaya", "Dewi Sri", "Dewi Ratih",
        "Cingkarabala", "Balaupata",   # canonical Suralaya gatekeepers per patch MMMM
    ),
    "chronicle_natural_signs": (
        "bintang berekor", "gunung bergemuruh", "gunung meletus",
        "laut selatan bergelora", "gerhana matahari", "gerhana bulan",
        "sungai berubah keruh tanpa hujan",
    ),
}


# su — wayang vocabulary + Sundanese chronicle-opener purity.
# Seeded from Cepot Membangun Kahyangan audit (sample-19, lens-2). Chronicle-opener
# purity note is patch LLLL: canonical Sundanese uses "Kocap"/"Kacaturkeun" for scene-
# opening rather than the BI-import "Tersebutlah"/"Kacatur". Advisory only.
_SU_WAYANG_VOCAB = {
    "chronicle_openers_native": (
        "Kocap",              # canonical Sundanese chronicle-opener
        "Kacaturkeun",        # canonical Sundanese chronicle-opener
    ),
    "chronicle_openers_bi_import_advisory": (
        # Advisory: these are BI-imports; purist reader may flag. NEVER hard-blocked.
        "Tersebutlah",
        "Kacatur",
    ),
    "stagecraft_elements": (
        "kayon", "blencong", "kelir", "cempala",
        "gong", "kendang", "suling", "rebab",
    ),
    "dalang_direct_address_openers": (
        "Héh, dulur-dulur anu budiman",
        "Simkuring rék nanyakeun",
        "Kitu, dulur-dulur nu budiman",
    ),
    "punakawan_sunda": (
        "Semar", "Cepot", "Astrajingga",   # Cepot = Astrajingga
        "Dawala", "Garéng",
    ),
    "verse_forms": (
        "kidung", "tembang", "pantun", "sisindiran", "pupuh",
    ),
}


# tl — Filipino precolonial labor terms + religious/feudal vocabulary.
# Seeded from Black Death audit (sample-18, lens-1) which flagged 'alipin' misapplied to
# European medieval serf. Precolonial Filipino labor conditions are distinct from European
# feudal — distinguishing them at the language_pack level lets narasi flag context-mismatched
# usage without hard-blocking historically valid uses.
_TL_FILIPINO_HISTORICAL_TERMS = {
    "labor_terms_precolonial": {
        "alipin": "chattel-slave (NOT equivalent to European medieval serf)",
        "aliping_lupa": "serf-tied-to-land (closer to European serf)",
        "aliping_saguiguilir": "household-servant (indoor domestic labor)",
        "aliping_namamahay": "sharecropper (owed labor but kept own house)",
        "maharlika": "freeman/noble (distinct from timawa)",
        "timawa": "freeman (below maharlika)",
    },
    "religious_feudal_terms": (
        "datu", "raja", "sultan",
        "bahay-na-bato", "sacop", "barangay",
        "encomienda", "polo", "tribute", "vandala",
    ),
    "historical_openers": (
        "Noong unang panahon",        # long ago
        "Sa panahon ng",               # in the era of
        "Ayon sa mga kronika",         # according to the chronicles
    ),
    "chronicle_attribution": (
        "ayon sa kasaysayan",
        "sang-ayon sa mga kronika",
        "sabi ng mga matatanda",
    ),
}


if _PHASE2_ON():
    # Env-gated activation. setdefault semantics — a hand-tuned id/su/tl entry added
    # later takes precedence over the seed, and existing keys are never overwritten.
    LANGUAGE_PACKS.setdefault("id", {}).setdefault("nusantara_vocab", _ID_NUSANTARA_VOCAB)
    LANGUAGE_PACKS.setdefault("su", {}).setdefault("wayang_vocab", _SU_WAYANG_VOCAB)
    LANGUAGE_PACKS.setdefault("tl", {}).setdefault("filipino_historical_terms",
                                                    _TL_FILIPINO_HISTORICAL_TERMS)
