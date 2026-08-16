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


# ── structural-refrain scanners (report-only, FLAG-never-OVER) ──────────────────
# Three refrains that recur every chapter and read as the engine "asserting" rather
# than moving (lens synthesis over river + Banda): (1) thesis over-restatement, (2)
# epistemic-hedge boilerplate (nonfiction), (3) closing-tableau sameness. All gated by
# ONE flag NARASI_REFRAIN_SCAN (default OFF → none run → byte-identical). status is only
# ever FLAG/PASS/OFF, NEVER 'OVER', so they can never enter over_budget / drive the diet
# loop. Each never raises.
def _refrain_scan_on() -> bool:
    return os.environ.get("NARASI_REFRAIN_SCAN", "0").strip().lower() in ("1", "true", "yes", "on")


def _regloss_budget_on() -> bool:
    """NARASI_REGLOSS_BUDGET=1 promotes the reglossing scan from FLAG-only to a BUDGETED
    counter (cap: style_spec.counters.regloss_max, env NARASI_REGLOSS_MAX fallback).
    Default OFF => FLAG-only behavior byte-identical."""
    return os.environ.get("NARASI_REGLOSS_BUDGET", "0").strip().lower() in ("1", "true", "yes", "on")


def _anchor_budget_on() -> bool:
    """NARASI_ANCHOR_BUDGET=1 arms the anchor/aphorism hard budget: capped RULE 5
    wording (pakem), capped outline vo_note, and the budgeted floating-aphorism
    counter below (cap: style_spec.counters.aphorisms_max, env NARASI_APHORISMS_MAX
    fallback). Default OFF => scan output byte-identical."""
    return os.environ.get("NARASI_ANCHOR_BUDGET", "0").strip().lower() in ("1", "true", "yes", "on")


def _cos_sim(a, b) -> float:
    """Cosine of two embedding vectors; 0.0 on any degeneracy. Never raises."""
    try:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a)); nb = math.sqrt(sum(y * y for y in b))
        return (dot / (na * nb)) if (na and nb) else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


_TITLECASE_RX = re.compile(r"^[A-ZÀ-Þ][a-zà-ÿ]+$")
_NUMERALISH_RX = re.compile(r"\b\d{3,}\b|\d%|\b\d{4}\b")


def _is_thesis_candidate(s: str) -> bool:
    """An abstract declarative CLAIM — not dialogue, a question, a heading, or a
    numeral/proper-noun-dominated fact sentence (the main false-positive defense)."""
    s = (s or "").strip()
    if not s:
        return False
    wc = len(s.split())
    if wc < 9 or wc > 45:
        return False
    if s[-1] in ("?", "!") or s[0] in ("#", ">", "-", "*", "|", '"', "“", "‘", "'"):
        return False
    toks = s.split()
    tc = sum(1 for w in toks if _TITLECASE_RX.match(w))
    if toks and tc / len(toks) > 0.40:   # proper-noun-dominated → likely a fact sentence
        return False
    if _NUMERALISH_RX.search(s):          # year/percent/big number → factual, not thesis
        return False
    return True


def _thesis_cluster_scan(chapters: list[str], *, embed_fn, cap: int) -> dict:
    """Report-only, THESIS-AGNOSTIC (no supplied thesis string): the manuscript RESTATES one
    controlling idea across many chapters (near-paraphrase). Cluster abstract declarative
    candidates by embedding cosine; FLAG when the largest cluster spanning >=N chapters exceeds
    `cap`. Near-VERBATIM clusters are downgraded (intentional chorus, not over-restatement).
    status 'FLAG'/'PASS'/'OFF' (never 'OVER'). Never raises. embed_fn None → 'OFF'."""
    out = {"status": "OFF", "count": 0, "cap": cap, "chapters": 0, "sentences": []}
    try:
        if embed_fn is None or len(chapters) < 3:
            return out
        _sim = float(os.environ.get("NARASI_THESIS_CLUSTER_SIM", "0.82"))
        _min_ch = int(os.environ.get("NARASI_THESIS_CLUSTER_MIN_CHAPTERS", "3"))
        _min_cand = int(os.environ.get("NARASI_THESIS_CLUSTER_MIN_CANDIDATES", "12"))
        _maxc = int(os.environ.get("NARASI_THESIS_CLUSTER_MAX_CANDIDATES", "160"))
        cands = []
        for ci, ch in enumerate(chapters):
            for s in _sentences(ch):
                s2 = s.strip().split("\n")[-1].strip()   # drop heading glue on 1st sentence
                if _is_thesis_candidate(s2):
                    cands.append((ci, s2))
        out["status"] = "PASS"
        if len(cands) < _min_cand:
            return out                                    # insufficient data
        cands = cands[:_maxc]
        vecs = [embed_fn(s) or None for _ci, s in cands]
        clusters = []                                     # [{members:[i...], chs:set()}]
        for i, (ci, _s) in enumerate(cands):
            if not vecs[i]:
                continue
            placed = False
            for cl in clusters:
                if _cos_sim(vecs[i], vecs[cl["members"][0]]) >= _sim:
                    cl["members"].append(i); cl["chs"].add(ci); placed = True; break
            if not placed:
                clusters.append({"members": [i], "chs": {ci}})
        if not clusters:
            return out
        big = max(clusters, key=lambda c: len(c["members"]))
        span, size = len(big["chs"]), len(big["members"])
        if span < _min_ch or size <= cap:
            return out
        try:                                              # refrain downgrade (verbatim chorus)
            from dalang_dedup import ngram_jaccard as _nj
            reps = [cands[m][1] for m in big["members"][:6]]
            pairs = [(reps[a], reps[b]) for a in range(len(reps)) for b in range(a + 1, len(reps))]
            if pairs and (sum(_nj(x, y) for x, y in pairs) / len(pairs)) >= 0.60:
                return out
        except Exception:  # noqa: BLE001
            pass
        out.update(status="FLAG", count=size, chapters=span,
                   sentences=[cands[m][1][:200] for m in big["members"][:8]])
        return out
    except Exception:  # noqa: BLE001
        return {"status": "PASS", "count": 0, "cap": cap, "chapters": 0, "sentences": []}


_HEDGE_CUES = (
    "sebagian sejarawan", "sebagian ahli", "sebagian peneliti", "sebagian lain",
    "sejarawan berbeda pendapat", "para sejarawan berbeda", "bukti dokumenter",
    "bukti yang tersisa", "bukti arkeologis", "belum cukup untuk", "tidak menyelesaikan",
    "belum bisa menuntaskan", "belum sepenuhnya didamaikan", "masih diperdebatkan",
    "angka pastinya", "yang bisa dipastikan", "yang dapat dipastikan", "perdebatan ini belum",
    "some historians", "other historians", "the evidence does not", "still debated",
)


def _hedge_density_scan(chapters: list[str]) -> dict:
    """Report-only: epistemic-hedge BOILERPLATE — the same 'some historians X / others Y /
    the evidence doesn't settle it / what is certain is simpler' move repeated across chapters.
    Each instance is good (anti-fabrication); saturation is tonal drag. Counts chapters carrying
    a hedge cue; FLAG when in > threshold chapters. status FLAG/PASS. Never raises."""
    out = {"status": "PASS", "count": 0, "n_chapters": len(chapters), "threshold": 0, "samples": []}
    try:
        if len(chapters) < 3:
            return out
        hits = [ci for ci, ch in enumerate(chapters)
                if any(cue in (ch or "").lower() for cue in _HEDGE_CUES)]
        thr = int(os.environ.get("NARASI_HEDGE_MAX_CHAPTERS", "0")) or max(4, math.ceil(len(chapters) * 0.6))
        out["count"] = len(hits); out["threshold"] = thr
        if len(hits) > thr:
            out["status"] = "FLAG"; out["samples"] = [str(ci + 1) for ci in hits]
        return out
    except Exception:  # noqa: BLE001
        return {"status": "PASS", "count": 0, "n_chapters": len(chapters), "threshold": 0, "samples": []}


def _closing_tableau_scan(chapters: list[str]) -> dict:
    """Report-only: chapters ENDING on the same sensory tableau (dusk+rain+scent+bird). Mirror
    of _opening_motif_scan on the LAST sentence of each chapter. status FLAG/PASS. Never raises."""
    out = {"status": "PASS", "count": 0, "motifs": {}, "sentences": []}
    try:
        if len(chapters) < 3:
            return out
        by_word: dict[str, set] = {}; word_sents: dict[str, list] = {}
        for ci, ch in enumerate(chapters):
            ss = _sentences(ch)
            if not ss:
                continue
            closing = ss[-1].strip()
            toks = [w.lower() for w in _OPENING_WORD_RX.findall(closing)]
            content = [w for w in toks if len(w) >= 4 and w not in _OPENING_STOP][:8]
            for w in set(content):
                by_word.setdefault(w, set()).add(ci)
                word_sents.setdefault(w, []).append(closing[:200])
        motifs = {w: len(chs) for w, chs in by_word.items() if len(chs) >= 3}
        if motifs:
            seen: list[str] = []
            for w in motifs:
                for s in word_sents.get(w, [])[:2]:
                    if s not in seen:
                        seen.append(s)
            out.update(status="FLAG", count=len(motifs), motifs=motifs, sentences=seen[:10])
        return out
    except Exception:  # noqa: BLE001
        return {"status": "PASS", "count": 0, "motifs": {}, "sentences": []}


# ── style-fingerprint saturation scan (own flag, NARASI_STYLE_SATURATION_SCAN,
# default OFF) — a real manuscript flagged 'the way ' 79x, em-dash 301x, 'the
# particular' 24x across a ~37K-word book: the model falling into the SAME repetitive
# crutch phrasing manuscript-WIDE rather than varying its prose. Unlike opening_motif/
# hedge_density/closing_tableau above (which scan per-CHAPTER openings/closings),
# this scans the whole ASSEMBLED manuscript as one text — the saturation signal only
# shows up at book scale. status FLAG-only, NEVER 'OVER' (see docstring below). Own
# flag (not NARASI_REFRAIN_SCAN) so it is independently enableable. Never raises.
_SAT_STOP = _OPENING_STOP | {
    # short EN/ID function words _OPENING_STOP omits (it only holds len>=4 words) —
    # needed so an all-function-word n-gram like 'of the'/'in the' is excluded, while
    # a content-bearing one like 'the way'/'the particular' (only ONE stopword) is kept.
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or", "but", "nor",
    "is", "are", "was", "be", "as", "by", "it", "he", "she", "his", "her", "its", "him",
    "you", "your", "we", "our", "i", "my", "me", "us", "so", "if", "no", "not",
    "do", "did", "can", "may", "had", "has", "who", "how", "why", "all", "any",
    "one", "out", "up", "down", "off", "yet", "am",
    "di", "ke", "dan", "itu", "ini", "ia", "ya", "apa", "kami", "kita",
    "aku", "kau", "nya", "pun", "si", "se", "para",
}


def _style_saturation_scan_on() -> bool:
    """NARASI_STYLE_SATURATION_SCAN=1 arms the manuscript-wide style-fingerprint
    saturation scan below. Default OFF => report key absent, byte-identical."""
    return os.environ.get("NARASI_STYLE_SATURATION_SCAN", "0").strip().lower() in ("1", "true", "yes", "on")


def _style_saturation_scan(text: str) -> dict:
    """Report-only: manuscript-WIDE (whole assembled text, not per-chapter — unlike
    _opening_motif_scan/_hedge_density_scan above) word/phrase crutch-repetition. Two
    sub-signals: (a) top content-bearing n-grams (2-4 words, case/punctuation-normalized
    via the word regex, sentence-scoped so a phrase never straddles a sentence boundary)
    whose count exceeds a length-scaled rate; n-grams made ENTIRELY of function words
    ('of the', 'in the') are excluded so the signal is CONTENT drift, not grammar. (b)
    em-dash ('—') rate per 1000 words, a separate, simpler sub-metric.
    status is FLAG-only, NEVER 'OVER' — same convention as _opening_motif_scan (see its
    docstring): there is no single correct replacement for an overused phrase (or a raw
    dash-rate), so auto-rewriting risks whack-a-mole substitution; a generation-prompt
    steer is the right lever here, not the surgical sentence-diet loop. The em-dash
    sub-metric COULD in principle be a real density budget (OVER-capable, in the
    citations_max/aporia_max style) but this codebase has no calibrated baseline em-dash
    ceiling to anchor one on, so — per the same caution that governs the phrase side —
    it defaults FLAG-only too rather than guessing a number. Never raises."""
    out = {
        "status": "PASS", "count": 0, "n_words": 0, "threshold": 0, "phrases": {},
        "em_dash": {"status": "PASS", "count": 0, "per_1000": 0.0, "threshold": 0.0},
    }
    try:
        n_words = len(_OPENING_WORD_RX.findall(text or ""))
        out["n_words"] = n_words
        _min_words = int(os.environ.get("NARASI_STYLE_SATURATION_MIN_WORDS", "3000"))
        if n_words < _min_words:
            return out  # too short for a manuscript-wide rate signal to mean anything

        # (a) content-bearing 2-4 gram saturation — sentence-scoped (mirrors the R-H3
        # aporia repeated-phrase 4-gram approach further below in this file).
        counts: dict[str, int] = {}
        for s in _sentences(text):
            toks = [w.lower() for w in _OPENING_WORD_RX.findall(s)]
            for n in (2, 3, 4):
                for i in range(max(0, len(toks) - n + 1)):
                    gram = toks[i:i + n]
                    if all(w in _SAT_STOP for w in gram):
                        continue  # function-word-ONLY ('of the') — grammar, not a motif
                    key = " ".join(gram)
                    counts[key] = counts.get(key, 0) + 1

        _rate = float(os.environ.get("NARASI_STYLE_SATURATION_RATE", "0.5"))   # per 1000 words
        _floor = int(os.environ.get("NARASI_STYLE_SATURATION_FLOOR", "8"))     # ignore noise on shortish docs
        thr = max(_floor, math.ceil(_rate * n_words / 1000))
        out["threshold"] = thr
        offenders = {g: c for g, c in counts.items() if c >= thr}
        if offenders:
            top = dict(sorted(offenders.items(), key=lambda kv: -kv[1])[:15])
            out["status"] = "FLAG"
            out["count"] = len(offenders)
            out["phrases"] = top

        # (b) em-dash rate — simple, separate sub-metric (FLAG-only; see docstring).
        _ed_count = (text or "").count("—")
        _ed_rate = (_ed_count / n_words * 1000) if n_words else 0.0
        _ed_thr = float(os.environ.get("NARASI_STYLE_SATURATION_EMDASH_RATE", "4.0"))
        ed = {"status": "PASS", "count": _ed_count, "per_1000": round(_ed_rate, 2), "threshold": _ed_thr}
        if _ed_rate > _ed_thr:
            ed["status"] = "FLAG"
            out["status"] = "FLAG"
        out["em_dash"] = ed
        return out
    except Exception:  # noqa: BLE001 — a broken scan must never break generation
        return {
            "status": "PASS", "count": 0, "n_words": 0, "threshold": 0, "phrases": {},
            "em_dash": {"status": "PASS", "count": 0, "per_1000": 0.0, "threshold": 0.0},
        }


# ── nonfiction prose-integrity scanners (report-only, lens#2 "hedge-as-shield" batch) ──
# Three more report-only scanners under the SAME NARASI_REFRAIN_SCAN flag, targeting the
# nonfiction failure modes lens#2 surfaced on the Notebook/Banda pieces. status FLAG/PASS only
# (never OVER → excluded from over_budget). Each never raises.
_DEFERRED_HOOK_CUES = (
    "later chapter", "later chapters", "the next chapter", "a story for another day",
    "better placed to tell", "will weigh", "chapters will", "for another day",
    "i will have to", "as we will see", "a story a later", "the next chapter is better",
    "bab berikutnya", "bab selanjutnya", "nanti akan", "akan kita lihat", "kelak akan",
)


def _deferred_hook_scan(chapters: list[str]) -> dict:
    """Report-only: DEFERRED-EVIDENCE hooks ('later chapters will… / a story for another day')
    that smuggle drama via a promise — some never paid. Surfaces the promise sentences; FLAG when
    density exceeds threshold. status FLAG/PASS. Never raises. (Payment-tracking is deferred; this
    surfaces the promises for review.)"""
    out = {"status": "PASS", "count": 0, "threshold": 0, "samples": []}
    try:
        hits = []
        for ci, ch in enumerate(chapters):
            for s in _sentences(ch):
                low = s.lower()
                if any(cue in low for cue in _DEFERRED_HOOK_CUES):
                    hits.append((ci + 1, s.strip()[:200]))
        thr = int(os.environ.get("NARASI_DEFERRED_HOOK_MAX", "3"))
        out["count"] = len(hits); out["threshold"] = thr
        if len(hits) > thr:
            out["status"] = "FLAG"
            out["samples"] = ["[Bab %d] %s" % (c, s) for c, s in hits[:10]]
        return out
    except Exception:  # noqa: BLE001
        return {"status": "PASS", "count": 0, "threshold": 0, "samples": []}


_EXPERT_NOUN_RX = re.compile(
    r"(?i)\b(?:specialists?|scholars?|experts?|researchers?|conservators?|historians?|"
    r"philologists?|scientists?|academics?|linguists?|para ahli|ahli|ilmuwan|peneliti|sejarawan)\b")
_EXPERT_VERB_RX = re.compile(
    r"(?i)\b(?:describe[sd]?|suggests?|argues?|says?|notes?|believe[sd]?|contends?|maintains?|"
    r"reports?|claims?|estimates?|observe[sd]?|holds?|insists?|describes it|"
    r"menggambarkan|berpendapat|menyatakan|mencatat|memperkirakan)\b")
_TITLE_TOKEN_RX = re.compile(r"^[A-ZÀ-Þ][a-zà-ÿ]{2,}")


def _unattributed_expert_scan(text: str) -> dict:
    """Report-only: nameless expert consensus — an expert-noun + a claim-verb in the same sentence
    with NO named authority (subtler than a fake citation — nothing to check). Two spare paths:
    (a) no expert-noun at all ('John Bastin described' → _EXPERT_NOUN_RX never matches, sentence
    skipped); (b) an expert-noun co-occurring WITH a name ('the historian John Bastin described')
    → a non-initial Title-case token marks it attributed. Only bare 'specialists/scholars describe…'
    with no name flags. Known FP (report-only, Phase-3 calibration): a sentence-INITIAL named
    authority ('Bastin, a specialist, describes it') is missed, since pos-0 is always capitalised.
    status FLAG/PASS. Never raises."""
    out = {"status": "PASS", "count": 0, "samples": []}
    try:
        hits = []
        for s in _sentences(text or ""):
            s = s.split("\n")[-1].strip()          # drop heading-glue on a chapter's 1st sentence
            if not (_EXPERT_NOUN_RX.search(s) and _EXPERT_VERB_RX.search(s)):
                continue
            toks = s.split()
            named = any(_TITLE_TOKEN_RX.match(t) for t in toks[1:])   # a name-like token (not sentence-initial)
            if not named:
                hits.append(s[:200])
        out["count"] = len(hits)
        if hits:
            out["status"] = "FLAG"; out["samples"] = hits[:10]
        return out
    except Exception:  # noqa: BLE001
        return {"status": "PASS", "count": 0, "samples": []}


_GLOSS_RX = re.compile(r"\b([A-Za-zÀ-ÿ][\wÀ-ÿ'’-]{3,})\s+(?:—|–|--)\s+")


def _reglossing_scan(text: str, *, repeat_cap: Optional[int] = None) -> dict:
    """Report-only by default: the SAME head-term re-glossed multiple times (e.g. 'dluwang — traditional
    Javanese bark paper' 5×) — signature of chapters generated without a 'term already defined'
    state. FLAG a head-term that opens an em-dash gloss >=2×. status FLAG/PASS. Never raises.
    NARASI_REGLOSS_BUDGET promotion: repeat_cap is a RAW per-term budget — deliberately NOT
    _scaled, a term needs defining once per BOOK regardless of length (same insight as the
    thesis read-cap-raw). repeats = glossings - 1; any term with repeats > repeat_cap =>
    status OVER, count = the WORST term's repeats (so narration_api._diet_worthy's tolerance
    band compares repeats against the cap, not the distinct-term tally), budget = repeat_cap,
    terms = only the over-cap terms. repeat_cap=None => historical output byte-identical."""
    out = {"status": "PASS", "count": 0, "terms": {}}
    try:
        counts: dict[str, int] = {}
        for m in _GLOSS_RX.finditer(text or ""):
            term = m.group(1).lower()
            if len(term) >= 4 and term not in _OPENING_STOP:
                # Paired-dash PARENTHETICAL ASIDE ('kerajaan — seperti semua institusi —
                # runtuh') is NOT a gloss: a second dash closing the insert before the
                # sentence ends marks an aside (harari's signature move), while a
                # definition gloss runs to sentence end un-closed. Counting asides made
                # nonfiction registers trip the budget (review finding, reproduced).
                # Skip BOTH dashes of a pair: the opener (a closing dash follows in-sentence)
                # and the closer (an opening dash precedes it in the same sentence).
                _tail = (text or "")[m.end():m.end() + 90]
                _close = re.search(r"\s(?:—|–|--)\s", _tail)
                _stop_p = re.search(r"[.!?\n]", _tail)
                if _close and (not _stop_p or _close.start() < _stop_p.start()):
                    continue
                _head_seg = re.split(r"[.!?\n]", (text or "")[max(0, m.start() - 160):m.start()])[-1]
                if re.search(r"\s(?:—|–|--)\s", _head_seg):
                    continue
                counts[term] = counts.get(term, 0) + 1
        # ── acronym/phrase alias pass (NARASI_REGLOSS_ALIAS, default OFF) ──
        # The single-token head regex is blind to the roll-5 AWS class: 'the AWS — …'
        # (head 3 chars, under the 4-char floor) and 'The Automatic Weather Station —
        # the ring of instruments…' (multi-word head) are the SAME term glossed twice.
        # Normalize phrase heads to their INITIALS and bare acronyms to themselves, so
        # aws == automatic-weather-station; ≥2 ⟹ one alias:* entry in the report.
        if os.environ.get("NARASI_REGLOSS_ALIAS", "0").strip().lower() in ("1", "true", "yes", "on"):
            _ph: dict[str, int] = {}
            for m2 in re.finditer(
                    r"\b((?:[A-Z][\wÀ-ÿ'’-]*\s+){1,3}[A-Z][\wÀ-ÿ'’-]+|[A-Z]{2,6})\s+(?:—|–|--)\s+",
                    text or ""):
                # same paired-dash aside guards as the main loop
                _tail2 = (text or "")[m2.end():m2.end() + 90]
                _close2 = re.search(r"\s(?:—|–|--)\s", _tail2)
                _stop2 = re.search(r"[.!?\n]", _tail2)
                if _close2 and (not _stop2 or _close2.start() < _stop2.start()):
                    continue
                _hs2 = re.split(r"[.!?\n]", (text or "")[max(0, m2.start() - 160):m2.start()])[-1]
                if re.search(r"\s(?:—|–|--)\s", _hs2):
                    continue
                _words = [w for w in re.split(r"\s+", m2.group(1).strip()) if w]
                if len(_words) > 1 and _words[0].lower() in ("the", "a", "an"):
                    _words = _words[1:]
                _key = ("".join(w[0] for w in _words).upper()
                        if len(_words) > 1 else _words[0].upper())
                if len(_key) >= 2:
                    _ph[_key] = _ph.get(_key, 0) + 1
            for _k2, _n2 in _ph.items():
                if _n2 >= 2:
                    counts["alias:" + _k2.lower()] = counts.get("alias:" + _k2.lower(), 0) + _n2
        reglossed = {t: n for t, n in counts.items() if n >= 2}
        if reglossed:
            out["status"] = "FLAG"; out["count"] = len(reglossed)
            out["terms"] = dict(sorted(reglossed.items(), key=lambda kv: -kv[1])[:10])
        if repeat_cap is not None:
            out["budget"] = int(repeat_cap)
            over = {t: n for t, n in reglossed.items() if (n - 1) > int(repeat_cap)}
            if over:
                out["status"] = "OVER"
                out["count"] = max(n - 1 for n in over.values())
                out["terms"] = dict(sorted(over.items(), key=lambda kv: -kv[1])[:10])
        return out
    except Exception:  # noqa: BLE001
        return {"status": "PASS", "count": 0, "terms": {}}


# ── DERIVED-NUMBER scan (report-only, Batch A): machine-checkable arithmetic/word-count errors the
# LLM makes and no craft lens catches (lens#3 on Kaset + Last Row) — (a) a currency "leftover" that
# equals the SUM of the outflows near it (2,95jt = 1,8 + 1,15, labeled "yang tersisa"); (b) an
# "N kata/words" claim that miscounts the quote it refers to ("enam kata" over a 5-word chat).
# Deterministic, status FLAG/PASS (NEVER 'OVER' → excluded from over_budget, cannot drive the diet
# loop / bill a rewrite). Never raises. Report-only ⟹ a residual false positive is a harmless report
# entry, never an edit (Phase-3 calibration). Bounded ID/EN spelled-number parse below is scoped to
# currency + small-int count phrases only.
_DN_UNIT = {
    "nol": 0, "kosong": 0, "se": 1, "satu": 1, "dua": 2, "tiga": 3, "empat": 4, "lima": 5,
    "enam": 6, "tujuh": 7, "delapan": 8, "sembilan": 9, "sepuluh": 10, "sebelas": 11,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
_DN_SCALE = ("juta", "ribu", "miliar", "milyar")


def _dn_parse_spelled(tokens) -> Optional[float]:
    """Multiplicative parse of an ID spelled number with koma-decimal ('dua koma sembilan lima
    juta' → 2.95e6; 'satu juta seratus lima puluh ribu' → 1.15e6). float|None. Never raises."""
    try:
        total = 0.0; cur = 0.0; last = None; frac = ""; in_frac = False; saw = False
        toks = list(tokens); i = 0
        while i < len(toks):
            t = toks[i]; i += 1
            if t == "koma":
                in_frac = True; saw = True; continue
            if in_frac:
                v = _DN_UNIT.get(t)
                if v is not None and v <= 9:
                    frac += str(int(v)); saw = True; continue
                in_frac = False; i -= 1; continue     # non-digit ends the fraction, reprocess token
            if t == "seratus":
                cur += 100.0; last = None; saw = True; continue
            if t == "seribu":
                total += (cur or 1) * 1000.0; cur = 0.0; last = None; saw = True; continue
            v = _DN_UNIT.get(t)
            if v is not None:
                cur += v; last = v; saw = True; continue
            if t == "belas":
                if last is not None:
                    cur = cur - last + (10 + last); last = None
                saw = True; continue
            if t == "puluh":
                if last is not None:
                    cur = cur - last + last * 10; last = None
                saw = True; continue
            if t == "ratus":
                if last is not None:
                    cur = cur - last + last * 100; last = None
                saw = True; continue
            if t in ("ribu", "juta", "miliar", "milyar"):
                scale = {"ribu": 1e3, "juta": 1e6, "miliar": 1e9, "milyar": 1e9}[t]
                base = cur if cur else 1.0
                if frac:
                    base += float("0." + frac); frac = ""; in_frac = False
                total += base * scale; cur = 0.0; last = None; saw = True; continue
            break
        if not saw:
            return None
        val = total + cur
        if frac:
            val += float("0." + frac)
        return val
    except Exception:  # noqa: BLE001
        return None


def _dn_num_digits(s: str) -> Optional[float]:
    """'6.200.000' / '1,8' / 'Rp1.150.000' / '2,95' → float (ID: '.'=thousands, ','=decimal)."""
    try:
        s = re.sub(r"[^\d.,]", "", (s or "").strip())
        if not s:
            return None
        if "," in s:
            s = s.replace(".", "").replace(",", ".")
        elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", s):
            s = s.replace(".", "")
        return float(s)
    except Exception:  # noqa: BLE001
        return None


_DN_ALLNUM = "|".join(sorted(
    list(_DN_UNIT.keys()) + ["koma", "belas", "puluh", "ratus", "ribu", "juta", "miliar",
                             "milyar", "seratus", "seribu"], key=len, reverse=True))
_DN_SPELLED_RX = re.compile(r"(?i)\b(?:%s)(?:[\s-]+(?:%s))*\b" % (_DN_ALLNUM, _DN_ALLNUM))
_DN_DIGIT_CUR_RX = re.compile(r"(?i)\bRp\s?\d[\d.,]*|\b\d[\d.,]*\s*(?:juta|ribu|miliar|milyar)\b")
_DN_SMALLINT = "|".join(sorted([w for w, v in _DN_UNIT.items() if v <= 12] + ["belas", "puluh"],
                               key=len, reverse=True))
_DN_WORDCOUNT_RX = re.compile(
    r"(?i)\b(\d{1,3}|(?:%s)(?:[\s-]+(?:%s)){0,2})[\s-]+(?:kata|words?)\b" % (_DN_SMALLINT, _DN_SMALLINT))
_DN_QUOTE_RX = re.compile(r"[\"“”«»]([^\"“”«»]{1,120})[\"“”«»]|['‘’]([^'‘’\n]{2,120})['‘’]")
_DN_REMAINDER_RX = re.compile(
    r"(?i)(?:yang\s+)?(?:tersisa|sisa|leftover|left\s*over|remaining|remainder|what'?s\s+left)")


def _dn_wordcount(q: str) -> int:
    return len(re.findall(r"[^\s]+", (q or "").strip()))


def _dn_is_yearish(v: float) -> bool:
    """A whole number in [1900,2100] is almost always a YEAR ('dua ribu dua puluh tiga' = 2023),
    not a currency amount — exclude so years can't pollute the arithmetic pool."""
    return float(v).is_integer() and 1900 <= v <= 2100


def _dn_currency_amounts(text: str) -> list:
    """(value, pos) for currency magnitudes ONLY (spelled with a scale word, or Rp/juta digits) —
    excludes bare counts ('delapan bulan') and year-like integers ('dua ribu dua puluh tiga')."""
    out = []
    for m in _DN_SPELLED_RX.finditer(text):
        run = m.group(0)
        if not any(s in run.lower() for s in _DN_SCALE):
            continue
        v = _dn_parse_spelled(re.split(r"[\s-]+", run.lower()))
        if v is not None and v > 0 and not _dn_is_yearish(v):
            out.append((round(v, 2), m.start()))
    for m in _DN_DIGIT_CUR_RX.finditer(text):
        v = _dn_num_digits(m.group(0))
        if v is not None and v < 1000 and re.search(r"(?i)juta", m.group(0)):
            v *= 1e6
        elif v is not None and v < 1000 and re.search(r"(?i)ribu", m.group(0)):
            v *= 1e3
        if v is not None and v > 0 and not _dn_is_yearish(v):
            out.append((round(v, 2), m.start()))
    return out


def _dn_subset_sum(target: float, amounts: list, tol: float = 1.0, min_size: int = 2):
    """A subset (size>=min_size) of `amounts` (bounded to 12) summing to `target` within tol."""
    amts = amounts[:12]; n = len(amts)
    for mask in range(1, 1 << n):
        if bin(mask).count("1") < min_size:
            continue
        s = sum(amts[i] for i in range(n) if mask & (1 << i))
        if abs(s - target) <= tol:
            return [amts[i] for i in range(n) if mask & (1 << i)]
    return None


def _dn_has_consistent_income(rem: float, others: list) -> bool:
    """Is there a plausible income I (> rem) among `others` with I − sum(subset of the rest) == rem?
    If so, an income−outflows==remainder reading EXISTS ⟹ the sub-sum match is a coincidence, not the
    'leftover mislabeled as outflows' error → do NOT flag (kills FPs from unrelated clustered amounts)."""
    for I in sorted(set(others), reverse=True):
        if I <= rem:
            continue
        rest = list(others); rest.remove(I)
        if abs(I - rem) <= 1.0 or _dn_subset_sum(I - rem, rest, min_size=1):
            return True
    return False


def _derived_number_scan(chapters: list) -> dict:
    """Report-only derived-number errors. status FLAG/PASS (never OVER). Never raises."""
    out = {"status": "PASS", "wordcount_mismatches": [], "currency_flags": []}
    try:
        # (a) word-count claim vs its referent quote. Guards: claimed>=2 (drops the 'tak satu kata
        # pun' idiom); pair to the LONGEST 2..15-word quote in a ±260-char window (the substantive
        # referent, not an incidental 1-word quote).
        for ci, ch in enumerate(chapters):
            for m in _DN_WORDCOUNT_RX.finditer(ch):
                raw = m.group(1).strip()
                claimed = (float(raw) if raw.isdigit()
                           else _dn_parse_spelled(re.split(r"[\s-]+", raw.lower())))
                if claimed is None or int(claimed) < 2:
                    continue
                lo, hi = max(0, m.start() - 260), min(len(ch), m.end() + 260)
                inrange = []
                for qm in _DN_QUOTE_RX.finditer(ch[lo:hi]):
                    q = (qm.group(1) or qm.group(2) or "").strip()
                    n = _dn_wordcount(q)
                    if 2 <= n <= 15:
                        inrange.append((n, q))
                if len(inrange) != 1:      # 0 or >1 candidate quotes ⟹ pairing is ambiguous, skip (precision > recall)
                    continue
                n, q = inrange[0]
                if abs(n - int(claimed)) >= 1:
                    out["wordcount_mismatches"].append(
                        "[Bab %d] claim=%d kata vs quote=%d words: \"%s\""
                        % (ci + 1, int(claimed), n, q[:60]))
        # (b) currency leftover == subset-sum of nearby outflows. WINDOW-based (not per-chapter):
        # `_chapters()` returns one chunk for 'Bab N:'-style manuscripts, so pooling the whole book
        # would cross-pair unrelated amounts and let the [:10] subset cap drop the real outflows.
        # Scope each remainder cue to a local ±window so only nearby amounts are considered.
        # Consistency guard: skip when a plausible income (largest windowed amount) MINUS the rest
        # equals the remainder — correct arithmetic that merely coincides with a sub-sum.
        full = "\n\n".join(chapters)
        seen_rem = set()
        for cue in list(_DN_REMAINDER_RX.finditer(full))[:40]:   # bound work on degenerate inputs
            lo, hi = max(0, cue.start() - 700), min(len(full), cue.start() + 300)
            win = full[lo:hi]
            amts = _dn_currency_amounts(win)
            if len(amts) < 3:
                continue
            cue_local = cue.start() - lo
            # the cue must not be a NON-numeric remainder ('sisa: belum ada / nol / habis' = zero)
            after = win[cue.end() - lo: cue.end() - lo + 24].lower()
            if re.search(r"\b(?:belum ada|belum|nol|kosong|habis|tidak ada|nihil|minus|remah)\b", after):
                continue
            rem = min(amts, key=lambda a: abs(a[1] - cue_local))
            if abs(rem[1] - cue_local) > 60:   # remainder amount must be tightly apposed to the cue
                continue
            if rem[0] in seen_rem:
                continue
            others = [v for (v, p) in amts if p != rem[1]]
            if len(others) < 2:
                continue
            combo = _dn_subset_sum(rem[0], others)
            if not combo:
                continue
            if _dn_has_consistent_income(rem[0], others):
                continue   # a plausible income−outflows==remainder reading exists → not the error
            seen_rem.add(rem[0])
            out["currency_flags"].append(
                "'%s' amount %s == sum%s of nearby outflows (leftover mislabeled as sum-of-outflows; income−outflows≠this)"
                % (win[max(0, cue_local - 24):cue_local + 12].strip().replace("\n", " "), rem[0], tuple(combo)))
            if len(out["currency_flags"]) >= 5:
                break
        if out["wordcount_mismatches"] or out["currency_flags"]:
            out["status"] = "FLAG"
        return out
    except Exception:  # noqa: BLE001
        return {"status": "PASS", "wordcount_mismatches": [], "currency_flags": []}


# ── numeric-magnitude + year-fork scanner (NARASI_NUMERIC_MAGNITUDE, default OFF) ──
# round-15, report-only. Per-item money value × victim count that can't reconcile with a
# stated grand total (≈10× off), plus a single money-referent tied to years ≥2 apart.
# Deterministic arithmetic only; reuses _a2_leadnum (forward-ref, resolved at call time).
_MAGN_CUR = r"(?:won|₩|dollars?|USD|rupiah|Rp)"
_MAGN_MONEY_RX = re.compile(
    r"\b(?P<lead>\d[\d.,]*|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred)"
    r"\s*(?P<scale>thousand|million|billion)?\s*" + _MAGN_CUR + r"\b", re.I)
_MAGN_SCALE = {"thousand": 1e3, "million": 1e6, "billion": 1e9}
_MAGN_PERITEM_RX = re.compile(r"\b(?:each|apiece|a\s+piece|per\s+(?:person|victim|family|household|worker|head)|per\s+capita)\b", re.I)
_MAGN_TOTAL_RX = re.compile(r"\b(?:total(?:l?ing|led)?|in\s+total|altogether|combined|in\s+all|aggregate|amounted?\s+to|came\s+to)\b", re.I)
_MAGN_COUNT_RX = re.compile(r"\b(\d[\d,]{1,6}|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:victims?|families|households?|people|persons?|workers?|residents?|survivors?|claimants?|employees?)\b", re.I)
# ROUND-15: only SPECIFIC transactional actions (a single dated event) — NOT broad/
# historical nouns ("the compensation"/"the demolition") that legitimately appear beside
# many backstory years (2005/2009 redevelopment) and would false-fork.
_MAGN_REFERENTS = ("the routing", "the transfer", "the payment", "the disbursement",
                   "the override", "the redirect", "the reroute", "the wire",
                   "the remittance", "the payout")

def _magn_value(lead, scale):
    v = _a2_leadnum(lead)
    return None if v is None else v * _MAGN_SCALE.get((scale or "").lower(), 1.0)

def numeric_magnitude_scan(text: str) -> dict:
    out = {"status": "PASS", "magnitude": [], "year_forks": []}
    if not text:
        return out
    try:
        per_item, totals = [], []
        for m in _MAGN_MONEY_RX.finditer(text):
            val = _magn_value(m.group("lead"), m.group("scale"))
            if val is None or val <= 0:
                continue
            ctx = text[max(0, m.start() - 60):m.end() + 60]
            # A billion-plus figure is structurally an AGGREGATE — no per-victim payout is a
            # billion won — so classify it as a total FIRST, before the per-item cue check.
            # Otherwise a nearby "Each …" from an adjacent sentence (swept into the ±60
            # window) silently reclassifies the grand total as per-item and the gate misses.
            # Below that, an explicit per-item cue ("each family got …") is a stronger, more
            # local signal than a distant "total" word a ±60 window can sweep in.
            if val >= 1e9:
                totals.append(val)
            elif _MAGN_PERITEM_RX.search(ctx):
                per_item.append(val)
            elif _MAGN_TOTAL_RX.search(ctx):
                totals.append(val)
            elif val < 1e8:
                per_item.append(val)
        counts = [c for cm in _MAGN_COUNT_RX.finditer(text) if (c := _a2_leadnum(cm.group(1))) and c > 0]
        n_max = max(counts) if counts else None
        if per_item and totals:
            max_item, max_total = max(per_item), max(totals)
            expected = max_item * n_max if n_max else max_item
            ratio = (max_total / expected) if expected else 0.0
            if ratio >= 10 or (expected and expected / max_total >= 10):
                out["magnitude"].append({"max_per_item": max_item, "stated_total": max_total,
                    "victim_count": n_max, "ratio": round(ratio, 1),
                    "note": (f"per-item max {max_item:,.0f} × count {int(n_max) if n_max else '(none stated)'} "
                             f"≈ {expected:,.0f}, but stated total {max_total:,.0f} (≈{ratio:.0f}× off)")})
        for kw in _MAGN_REFERENTS:
            years = set()
            for km in re.finditer(r"\b" + re.escape(kw) + r"\b", text, re.I):
                for ym in re.finditer(r"\b(?:19|20)\d{2}\b", text[max(0, km.start() - 120):km.end() + 120]):
                    years.add(int(ym.group(0)))
            if len(years) >= 2 and max(years) - min(years) >= 2:
                out["year_forks"].append({"referent": kw, "years": sorted(years),
                    "note": f"'{kw}' tied to years {sorted(years)} ({max(years)-min(years)}y apart)"})
        if out["magnitude"] or out["year_forks"]:
            out["status"] = "FLAG"
        return out
    except Exception:  # noqa: BLE001
        return {"status": "PASS", "magnitude": [], "year_forks": []}


_DENIAL_RX = re.compile(
    r"(?i)\b(?:denial mode|in denial|state of denial|denial was still|"
    r"mode penyangkalan(?:\s+masih(?:\s+aktif)?)?|dalam penyangkalan|penyangkalan masih aktif)\b")


def _denial_fingerprint_scan(text: str) -> dict:
    """Report-only lane-fingerprint: the 'denial mode' concept-slot the pipeline reaches for in
    coming-of-age introspection beats (EN in River, ID 'mode penyangkalan masih aktif' in Kaset).
    status FLAG/PASS. Never raises. Lane-calibration signal, not a quality gate."""
    out = {"status": "PASS", "count": 0, "samples": []}
    try:
        hits = _DENIAL_RX.findall(text or "")
        if hits:
            seen = []
            for h in hits:
                h = h.strip().lower()
                if h not in seen:
                    seen.append(h)
            out["status"] = "FLAG"; out["count"] = len(hits); out["samples"] = seen[:8]
        return out
    except Exception:  # noqa: BLE001
        return {"status": "PASS", "count": 0, "samples": []}


# ── BATCH A.2 (report-only, same NARASI_REFRAIN_SCAN gate): four cheap greppable gates from the Giza
# lens#2 forensic — each status FLAG/PASS (never OVER → excluded from over_budget). Never raise. A
# residual FP is a harmless report entry (Phase-3 calibration), never an edit. `_chapters()` returns
# one chunk for 'Chapter N:'/'Bab N:' manuscripts, so these split chapters locally where needed.
def _a2_chapters(text: str) -> list:
    # `#{0,4}` — the LIVE pipeline text carries markdown headings ("## Chapter 1:"); without it this
    # split matched only bare .txt exports, so _closer_thinning_scan silently returned zeros in prod
    # (found via the Archivist DB dogfood: last_words=0/ratio=null while the raw file measured fine).
    parts = re.split(r"(?im)^\s*#{0,4}\s*(?:chapter|bab)\s+\d+\s*[:.]", text or "")
    return [p for p in parts if p.strip()]


# #1 ANOMALY-IN-A-HEDGED-DOC — the fabricated-precision detector: a BIG (>=1M) numeric value that is
# NEVER source-attributed anywhere in the doc (Giza '31 million tons'), while other big values ARE
# sourced (2.3M/6M via 'scholars'/'estimates vary'). Value-level dedup so a value sourced once doesn't
# flag its rhetorical re-mentions. Once hedge-discipline is internalised, the remaining fabrications
# are OUTLIERS against the doc's own sourcing baseline — this flags the outlier, not "add more hedge".
_A2_NW = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
          "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "twenty": 20, "thirty": 30, "forty": 40,
          "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100}
_A2_BIGNUM_RX = re.compile(
    r"(?i)\b((?:\d[\d.,]*|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twenty|thirty|"
    r"forty|fifty|sixty|seventy|eighty|ninety|hundred))[\s-]+(million|billion)\b|\b(\d{7,})\b")
_A2_SOURCE_RX = re.compile(
    r"(?i)\b(stud(?:y|ies)|research(?:ers)?|scholars?|scientists?|physicists?|experts?|egyptolog\w+|"
    r"university|univ\.|according to|commonly cited|widely cited|estimates?\s+(?:vary|place|put|suggest|reach)|"
    r"scholars still argue|reconstructions?|surveys?|excavat\w+|papyr\w+|census|records?|"
    r"reports?|data|measured|documented|sources?)\b")


def _a2_leadnum(s):
    s = (s or "").strip().lower().rstrip("-")
    if re.match(r"^\d", s):
        try:
            return float(re.sub(r"[^\d.]", "", s.replace(",", "")))
        except Exception:  # noqa: BLE001
            return None
    return _A2_NW.get(s)


def _bignum_unsourced_scan(chapters: list) -> dict:
    """Report-only: a BIG (>=1M) numeric value NEVER source-attributed anywhere (fabricated-precision
    outlier). status FLAG/PASS. Never raises."""
    out = {"status": "PASS", "unsourced_big_claims": [], "distinct_big_values": 0}
    try:
        full = "\n\n".join(chapters)
        vals = {}
        for m in _A2_BIGNUM_RX.finditer(full):
            if m.group(2):
                lead = _a2_leadnum(m.group(1))
                if lead is None:
                    continue
                val = lead * (1e9 if m.group(2).lower() == "billion" else 1e6)
            else:
                val = _a2_leadnum(m.group(3))
                if val is None or val < 1e6:
                    continue
            lo, hi = max(0, m.start() - 150), min(len(full), m.end() + 150)
            sourced = bool(_A2_SOURCE_RX.search(full[lo:hi]))
            snip = full[max(0, m.start() - 30):m.end() + 20].strip().replace("\n", " ")[:90]
            rec = vals.setdefault(round(val / 1e5), {"sourced": False, "snip": snip})
            rec["sourced"] = rec["sourced"] or sourced
            if not sourced:
                rec["snip"] = snip
        out["distinct_big_values"] = len(vals)
        unsourced = [r["snip"] for r in vals.values() if not r["sourced"]]
        if unsourced and len(vals) >= 2:
            out["status"] = "FLAG"; out["unsourced_big_claims"] = unsourced[:8]
        return out
    except Exception:  # noqa: BLE001
        return {"status": "PASS", "unsourced_big_claims": [], "distinct_big_values": 0}


# #2 BRIEF/OUTLINE-LEAK — brief/outline meta-language leaking into prose ('The premise our engineers
# work from', 'the chapter I was promised' [Notebook]). Greppable.
# High-precision: the "our X" noun form REQUIRES a brief-verb ('work from/put/estimate') so common
# prose ('our team walked in') can't trip it; plus the Giza premise-frame + Notebook's promised-chapter
# + explicit brief/outline references + outline-y chapter framing.
_A2_BRIEF_LEAK_RX = re.compile(
    r"(?i)("
    r"the premise (?:our|we|i|the)[\w ,]{0,24}?works? from"
    r"|our (?:engineers|researchers|analysts|writers) (?:work|works|put|puts|estimate|estimates|assume|assumes|suggest|suggests)\b"
    r"|the chapter i was promised"
    r"|the (?:brief|outline) (?:says|calls|specifi\w*|our|we)\b"
    r"|as (?:the|per) (?:brief|outline)\b"
    r"|in this chapter,? (?:we|i) will\b"
    r"|th(?:is|e) chapter (?:will|would) (?:cover|explore|examine|show|discuss|read|open|close)\b"
    r"|(?:the )?epigraph would read\b"       # 'The epigraph would read:' — slot narrated, not rendered (fallen-angel)
    r")")
# A2.3: a narratorial DRAFT-ANNOTATION in square brackets — a full capital-initial sentence inside
# [...] ending in a period ('[He would call it protection, later... That was the trouble.]'). Distinct
# from short markers ([VERIFY], [sic], [2011]) which never match (need >=24 chars + a terminal period).
_A2_BRACKET_ASIDE_RX = re.compile(r"\[[A-Z][^\[\]]{24,}\.\]")
# A2.4: SHORT stage-direction/outline markers ('[BEAT]' surfaced mid-pipeline on the Keyframe roll,
# adjacent to the Kenapa leak; stripped by downstream bracket-handling before delivery, but the
# scanner runs earlier and documents the outline-voice habit). Explicit whitelist of production
# tokens only — [VERIFY]/[sic]/[2011] stay excluded by construction.
_A2_STAGE_MARK_RX = re.compile(r"\[(?:BEAT|PAUSE|CUT TO|CUT|SFX|VO|MUSIC|SILENCE|TRANSITION|MONTAGE)\]")


def _brief_leak_scan(text: str) -> dict:
    """Report-only: brief/outline meta-language + narratorial draft-annotations leaking into prose
    (the 'outline-negotiation voice' — Notebook/Giza/fallen-angel). status FLAG/PASS. Never raises."""
    out = {"status": "PASS", "count": 0, "samples": []}
    try:
        hits = [text[max(0, m.start() - 8):m.end() + 40].strip().replace("\n", " ")[:70]
                for m in _A2_BRIEF_LEAK_RX.finditer(text or "")]
        hits += [m.group(0).replace("\n", " ")[:70] for m in _A2_BRACKET_ASIDE_RX.finditer(text or "")]
        hits += [text[max(0, m.start() - 12):m.end() + 24].strip().replace("\n", " ")[:70]
                 for m in _A2_STAGE_MARK_RX.finditer(text or "")]
        if hits:
            out["status"] = "FLAG"; out["count"] = len(hits); out["samples"] = hits[:8]
        return out
    except Exception:  # noqa: BLE001
        return {"status": "PASS", "count": 0, "samples": []}


# #3 OUTPUT-MODE-SLIP — a VIDEO script that slips into READING-address: a strong video-CTA idiom
# ('keep you watching') co-occurring with an audience reading-address ('you, reading this'). Watch-side
# is video-CTA only (NOT diegetic 'on screen') to avoid fiction-cinema FPs.
_A2_WATCH_RX = re.compile(r"(?i)\b(keep you watching|keep watching|watch(?:ing)? (?:till|until) the end|in this video|by the end of this video|smash that (?:like|subscribe))\b")
_A2_READ_RX = re.compile(r"(?i)\b(you(?:'re| are) reading|as you read|keep reading|read on|reading this|dear reader|turn the page)\b")


def _mode_slip_scan(text: str) -> dict:
    """Report-only: video-CTA address co-occurring with reading-address (video script forgot it's
    video). status FLAG/PASS. Never raises."""
    out = {"status": "PASS", "watch": [], "read": []}
    try:
        w = [m.group(0) for m in _A2_WATCH_RX.finditer(text or "")]
        r = [m.group(0) for m in _A2_READ_RX.finditer(text or "")]
        if w and r:
            out["status"] = "FLAG"; out["watch"] = w[:5]; out["read"] = r[:5]
        return out
    except Exception:  # noqa: BLE001
        return {"status": "PASS", "watch": [], "read": []}


# #4 CLOSER-THINNING — last chapter far shorter than the median chapter (Giza Ch8 529 vs ~1330):
# deliberate outro OR an Assembler token-budget allocation bug. Threshold env-tunable.
def _closer_thinning_scan(text: str) -> dict:
    """Report-only: last chapter < NARASI_CLOSER_MIN_RATIO (default 0.50) of the median chapter.
    status FLAG/PASS. Never raises."""
    out = {"status": "PASS", "last_words": 0, "median_words": 0, "ratio": None}
    try:
        wcs = [len(c.split()) for c in _a2_chapters(text)]
        if len(wcs) < 4:
            return out
        last = wcs[-1]
        body = sorted(wcs[:-1]); med = body[len(body) // 2]
        ratio = (last / med) if med else 1.0
        out.update(last_words=last, median_words=med, ratio=round(ratio, 2))
        try:
            thr = float(os.environ.get("NARASI_CLOSER_MIN_RATIO", "0.50"))
        except Exception:  # noqa: BLE001
            thr = 0.50
        if ratio < thr:
            out["status"] = "FLAG"
        return out
    except Exception:  # noqa: BLE001
        return {"status": "PASS", "last_words": 0, "median_words": 0, "ratio": None}


# #A3 HOME-LANGUAGE BLEED — the pipeline's OWN home tongue (Indonesian) leaking a single conspicuous
# word into a NON-ID manuscript, esp. a character's interiority (fallen-angel: "Kenapa — no. The thought
# came in her own tongue" in a KOREAN character's mind — should be "Wae"). The existing
# narasi_gate.language_consistency_scan catches only a WHOLE sentence in the wrong language (needs 0
# target-lang function-words); a single ID word buried in an otherwise-correct EN sentence slips it. This
# is a distinct, high-visibility bug (both target-lang AND ID viewers catch it). High-precision ID word
# set (near-zero-FP in EN/other-Latin); SKIPPED for ID-family manuscripts where these are native.
# Unambiguous ID words only — dropped 'aku'/'dong'/'kok' (collide with names: 'Dong-hui', 'Aku', etc.,
# and \bdong\b matches the 'Dong' in a Korean/Chinese name). Report-only, so recall < precision here.
_A3_HOME_WORDS = ("kenapa", "mengapa", "kamu", "saya", "banget", "nggak", "sih", "aduh",
                  "sudah", "belum", "adalah", "dengan", "tidak", "kalau", "kalo", "gimana", "sekarang")
_A3_HOME_RX = re.compile(r"(?i)\b(" + "|".join(sorted(_A3_HOME_WORDS, key=len, reverse=True)) + r")\b")
_A3_ID_FAMILY = ("id", "jv", "su", "ms", "min", "ban", "bug", "mad", "ace", "bjn")


def _home_lang_bleed_scan(text: str, lang: str) -> dict:
    """Report-only: a high-signal Indonesian home-word in a NON-ID manuscript (home-language bleed).
    status FLAG/PASS. Never raises. Skipped for ID-family targets (words are native there)."""
    out = {"status": "PASS", "count": 0, "samples": []}
    try:
        base = (lang or "en").split("-")[0].lower()
        if base in _A3_ID_FAMILY:
            return out
        hits = []
        for m in _A3_HOME_RX.finditer(text or ""):
            hits.append(text[max(0, m.start() - 18):m.end() + 26].strip().replace("\n", " ")[:60])
        if hits:
            out["status"] = "FLAG"; out["count"] = len(hits); out["samples"] = hits[:8]
        return out
    except Exception:  # noqa: BLE001
        return {"status": "PASS", "count": 0, "samples": []}


# ── LANGUAGE-CONSISTENCY word scan (report-only; standalone SIBLING of #A3 above) ──
# Two mechanisms already probe "wrong-language text in the manuscript" and both miss a
# short clause built entirely out-of-seed: narasi_gate.language_consistency_scan is
# SENTENCE-level (needs >=3 tokens with ZERO job-language function-word overlap AND >=3
# overlap with some other seeded language — a 3-word ID clause like "Kapan mereka datang"
# never reaches that bar), and #A3's _home_lang_bleed_scan above is WORD-level but its
# curated list is deliberately tight (kenapa/mengapa/kamu/saya/… — see 2719) and doesn't
# contain "kapan" or "mereka" either. This is a THIRD, independent word-level check with a
# wider net of common Indonesian FUNCTION WORDS/particles, kept in its OWN list/regex (NOT
# merged into _A3_HOME_WORDS) so #A3's existing NARASI_REFRAIN_SCAN-gated counter is left
# byte-for-byte untouched by this addition. Gated by its own flag in narration_api.py
# (NARASI_LANGUAGE_CONSISTENCY_SCAN), independent of NARASI_REFRAIN_SCAN and of the
# sentence-level mechanism's NARASI_TERMINAL_GATE.
#
# Same FP discipline as #A3 (whole-word \b...\b only, curated not exhaustive). Deliberately
# EXCLUDED for name/word collision risk with common English proper nouns/words, mirroring
# #A3's own precedent of dropping 'aku'/'dong'/'kok' for the same reason:
#   - "yang"  — common surname ("Yang")
#   - "dan"   — common given name ("Dan")
#   - "dari"  — rare EN fabric noun + high homophone/substring risk
#   - "akan"  — plausible name/place-name fragment; not high-signal enough to risk it
# "kami" carries a residual, lower risk (English loanword for a Shinto spirit in
# Japanese-set fantasy prose) — kept in per investigation sign-off but flagged here for
# any future re-tuning. Audit (round after initial ship) confirmed 3 MORE words belong on
# the excluded list, same collision class as yang/dan/dari/akan above:
#   - "kita"  — common Japanese surname/ward name (Kita Ikki; Kita-ku/Kita Ward)
#   - "saya"  — common Japanese given name + recurring anime/manga character name
#               (Blood+, Elfen Lied) + EN loanword for a katana scabbard
#   - "bisa"  — real given name (artist Bisa Butler) + an Indonesian island name usable
#               in nonfiction/travel copy
_LANGCHK_ID_WORDS = ("dengan", "tidak", "adalah", "sudah", "mereka", "kami",
                     "karena", "jika", "kalau", "kapan", "bagaimana", "mengapa",
                     "kamu")
_LANGCHK_ID_RX = re.compile(r"(?i)\b(" + "|".join(sorted(_LANGCHK_ID_WORDS, key=len, reverse=True)) + r")\b")


def language_consistency_word_scan(text: str, lang: str) -> dict:
    """Report-only: curated Indonesian function-word/particle bleed into a NON-Indonesian
    manuscript (word-level, whole-word match) — a sibling of #A3's _home_lang_bleed_scan
    with a wider function-word net (see module comment above for the excluded-word
    rationale). status FLAG/PASS. Never raises. Skipped entirely for ID-family targets
    (words are native there), reusing #A3's _A3_ID_FAMILY skip-list verbatim."""
    out = {"status": "PASS", "count": 0, "samples": []}
    try:
        base = (lang or "en").split("-")[0].lower()
        if base in _A3_ID_FAMILY:
            return out
        hits = []
        for m in _LANGCHK_ID_RX.finditer(text or ""):
            hits.append({
                "term": m.group(0),
                "snippet": text[max(0, m.start() - 24):m.end() + 30].strip().replace("\n", " ")[:70],
            })
        if hits:
            out["status"] = "FLAG"
            out["count"] = len(hits)
            out["samples"] = hits[:8]
        return out
    except Exception:  # noqa: BLE001
        return {"status": "PASS", "count": 0, "samples": []}


# #HG HOMOGENIZATION-TIC INVENTORY — a cross-roll audit (fallen-angel ↔ Lumi, same lane) found the
# generator reaching for the SAME lane-default tics across rolls: opening timestamps ending :14, the
# 11th floor, cold-coffee-as-opening-beat, tteokbokki-as-comfort-food, the '[X] do not [Y]' villain-
# composure aphorism. A single-manuscript scanner can't SEE cross-roll sameness, but it can INVENTORY
# these markers per roll so the pattern is queryable in the DB (and so NARASI_ANTI_HOMOGENIZATION's
# effect is measurable). Report-only telemetry; FLAG only when a roll reaches for >=2 lane-defaults.
_HG_TS14_RX = re.compile(r"\b\d{1,2}:14\b")
_HG_11FLOOR_RX = re.compile(r"(?i)\beleven(?:th)?[- ]floor\b|\b11th[- ]floor\b")
_HG_XDONOTY_RX = re.compile(r"(?i)\b\w+ do not \w+[.\n]")   # 'Boards do not gasp.' / 'men like him do not.'
_HG_COFFEE_RX = re.compile(r"(?i)coffee[^.]{0,25}\bcold\b|\bcold\b[^.]{0,12}coffee")


def _homogenization_tic_scan(text: str) -> dict:
    """Report-only cross-roll tic INVENTORY (timestamp-:14, 11th-floor, cold-coffee, tteokbokki,
    'X do not Y'). status FLAG when >=2 lane-defaults present. Never raises."""
    out = {"status": "PASS", "tics": {}}
    try:
        t = text or ""
        tics = {
            "timestamp_14": len(_HG_TS14_RX.findall(t)),
            "eleventh_floor": len(_HG_11FLOOR_RX.findall(t)),
            "cold_coffee": len(_HG_COFFEE_RX.findall(t)),
            "tteokbokki": len(re.findall(r"(?i)\btteokbokki\b", t)),
            "x_do_not_y": len(_HG_XDONOTY_RX.findall(t)),
            # Archivist round-3 additions (INVENTORY-ONLY, never affect FLAG): the lane's repertory
            # cast (Hae-rin[Lumi] ≈ Ha-rin[Archivist] loyal record-keeper; Ok Jae-heon/Hye-ran) and the
            # favorite number 41 (Lumi 41GB → 41 sacks + 41 leaves ×7).
            "cast_harin": len(re.findall(r"(?i)\bHae?-rin\b", t)),
            "cast_ok": len(re.findall(r"(?i)\bOk (?:Jae-heon|Hye-ran)\b", t)),
            "forty_one": len(re.findall(r"(?i)\bforty-one\b|\b41\b", t)),
        }
        out["tics"] = {k: v for k, v in tics.items() if v}
        # The individual tics (11th-floor / cold-coffee / a single :14 / tteokbokki) are COMMON in
        # normal prose (any office thriller, any Korean-set story) — so this is an INVENTORY, and
        # cross-roll sameness is detected by QUERYING these counts across jobs, NOT by a per-manuscript
        # verdict. FLAG only on the one unambiguous within-manuscript anomaly: the ':14' favorite-number
        # tic reused >=3x, which has no legitimate narrative reason and is the pipeline's own fingerprint.
        if tics["timestamp_14"] >= 3:
            out["status"] = "FLAG"
        return out
    except Exception:  # noqa: BLE001
        return {"status": "PASS", "tics": {}}


# ── lane-ledger deterministic validator (NARASI_LEDGER_VALIDATOR, default OFF) ─────────
# Round-2 evidence (jobs eky9gcge/cpn9kjg7/ibtgb7zg): PROMPT bans failed 3/3 rolls —
# 'eleven' leaked every roll INCLUDING at bible level (eleven-month drought ⟹ poisons
# every chapter), and cold coffee / barley tea / fluorescent hum / steady-hands-as-data
# recurred 3/3. Only deterministic validation holds. This turns the SAME
# pakem/lane_ledger.json that dynamic.py injects as a negative PROMPT constraint into a
# post-hoc regex INVENTORY over BOTH the pinned bible and the final manuscript, plus a
# static banned-tics table for the telemetry-confirmed repeats. Report-only:
# status FLAG/PASS/OFF, NEVER 'OVER' → excluded from over_budget, can never
# drive the diet loop. name_stems/recycled_beats are prose descriptions for the prompt
# injection, NOT regexable — skipped here by design. Never raises.
_LEDGER_EN_ONES = ("zero one two three four five six seven eight nine ten eleven twelve "
                   "thirteen fourteen fifteen sixteen seventeen eighteen nineteen").split()
_LEDGER_EN_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
                   "eighty", "ninety")
_LEDGER_DUR_WORDS = r"(?:months?|years?|weeks?|days?|hours?|minutes?|bulan|tahun|minggu|hari|jam|menit)"
# (term, pattern, min_count) — min_count>1: a single use is normal furniture, the repeat is the tic.
_LEDGER_STATIC_TICS: tuple = (
    ("eleven (spelled/duration)",
     r"(?i)\beleven\b|\b11[\s-]" + _LEDGER_DUR_WORDS, 1),
    ("cold coffee", _HG_COFFEE_RX.pattern, 1),
    ("fluorescent hum",
     r"(?i)\bfluorescents?\b[^.\n]{0,40}\bhum\w*\b|\bhum\w*\b[^.\n]{0,40}\bfluorescents?\b", 1),
    ("barley tea", r"(?i)\bbarley\s+tea\b", 2),
    ("steady hands as data",
     r"(?i)\bsteady\s+hands?\b[^.\n]{0,80}\b(?:data|numbers?|metrics?|charts?|spreadsheets?)\b"
     r"|\b(?:data|numbers?|metrics?|charts?|spreadsheets?)\b[^.\n]{0,80}\bsteady\s+hands?\b", 1),
    # r16 (S2-Th-3 finding): a hedged approximate duration ("around 4-5 minutes") followed by
    # precise arithmetic language on that same imprecise figure ("The math was not generous") —
    # the tell is HEDGING then COMPUTING on the hedge, not the hedge alone (an ordinary "around
    # 5 minutes" is unremarkable prose; scoping to the math-tell keeps this high-precision).
    # AUDIT FIX: the gap `[^.\n]{0,150}` stopped at the FIRST period, missing the real
    # motivating example ("around 4-5 minutes. ... The math was not generous.") where the
    # hedge and its math-tell are in ADJACENT sentences, not the same one. Widened to permit
    # crossing exactly one sentence boundary while staying line-bounded (never crosses a
    # paragraph break).
    ("hedged duration then math",
     r"(?i)\b(?:around|about|roughly|approximately)\s+\d{1,3}(?:[-–]\d{1,3})?\s+"
     r"(?:minutes?|seconds?|hours?)\b[^\n]{0,180}\b(?:the math|do(?:ing)? the math|"
     r"that (?:left|gave|meant)|which (?:left|meant)|not generous|wasn't generous)\b", 1),
)


def _ledger_validator_on() -> bool:
    return os.environ.get("NARASI_LEDGER_VALIDATOR", "0").strip().lower() in ("1", "true", "yes", "on")


def _timeline_v2_on() -> bool:
    return os.environ.get("NARASI_TIMELINE_ARITH_V2", "0").strip().lower() in ("1", "true", "yes", "on")


def _timeline_arith_on() -> bool:
    return os.environ.get("NARASI_TIMELINE_ARITH", "0").strip().lower() in ("1", "true", "yes", "on")


def _numeric_magnitude_on() -> bool:
    return os.environ.get("NARASI_NUMERIC_MAGNITUDE", "0").strip().lower() in ("1", "true", "yes", "on")


def _age_ledger_on() -> bool:
    return os.environ.get("NARASI_AGE_LEDGER", "0").strip().lower() in ("1", "true", "yes", "on")


def _canon_anchor_on() -> bool:
    return os.environ.get("NARASI_CANON_ANCHOR", "0").strip().lower() in ("1", "true", "yes", "on")


def _brand_scan_on() -> bool:
    return os.environ.get("NARASI_BRAND_SCAN", "0").strip().lower() in ("1", "true", "yes", "on")


# ── real-brand scan (round-5.1; roll-8 S1: 'Doosan Marine Engineering' — a real
# chaebol — cast as the culpable company behind 14 deaths = defamation exposure).
# Fiction may reference the real world, but a REAL conglomerate as wrongdoing's
# named actor is a legal/brand risk the lane must never ship silently. Long names
# match bare; two-letter groups (SK/LG/GS/CJ/KT) only with a corporate tail, so
# prose like "LG?" or Korean romanization noise can't FP.
_REAL_BRANDS_LONG = (
    "Hanshin",   # round-8: Hanshin E&C is real — culpable-builder slot 2 rolls running
    "Samsung", "Hyundai", "Doosan", "Lotte", "Hanwha", "POSCO", "Daewoo", "Kumho",
    "Hanjin", "Ssangyong", "Kolon", "Naver", "Kakao", "Coupang", "Celltrion",
    "Hyosung",
    # banks (round-N: manuscript e75fgr0z used a real bank as bribe-laundering conduit)
    "Nonghyup", "Kookmin", "Shinhan", "Woori", "KEB Hana",
    # press (near-match camouflage spellings added directly, same rationale as the
    # Hanjin≈Hanshin orbit fix at _lev_le1 — the model reliably reuses ONE transliteration)
    "Hankyoreh", "Hangyeore Ilbo", "Chosun Ilbo", "JoongAng Ilbo", "Dong-A Ilbo",
    # Hankuk/Hankook Ilbo: the literal multi-word press names bare-match here, same
    # as Chosun Ilbo etc. above. Bare unmodified "Hankuk"/"Hankook" (no tail) is
    # deliberately NOT listed here (round-N FP fix): both are just the McCune-
    # Reischauer/Revised-Romanization spelling of "Korea" itself, so a bare
    # unconditional match false-flags ordinary prose ("...got back to Hankuk") as
    # a real-company hit. See _REAL_BRANDS_PRESS_ROOT below for the tail-gated form.
    "Hankuk Ilbo", "Hankook Ilbo",
)
# Names that collide with people/places in prose ("Tae-young" unhyphenated, Mount
# Halla) only count WITH a corporate tail — same rule as the two-letter groups.
# "Daesung" collides with a common real Korean given name (also an idol stage
# name) — bare match would false-flag a character, not just Daesung Group.
_REAL_BRANDS_SHORT = ("SK", "LG", "GS", "CJ", "KT", "DL",
                      "Booyoung", "Hoban", "Halla", "HDC", "Taeyoung", "Daesung")
# "Hankuk"/"Hankook" bare are generic Korea-romanization tokens, not brand-unambiguous
# like Samsung/Hyundai — same collision class as _REAL_BRANDS_SHORT above, so they
# only count as a hit WITH a tail. Unlike SHORT, the tail may be a PRESS suffix
# (_PRESS_TAIL: "Hankuk Daily") as well as a corporate one (_BRAND_TAIL), since the
# collision this protects against is specifically the "Hankuk/Hankook Ilbo" family —
# kept as its own list (not folded into SHORT) so real_brand_scan can gate it on
# BOTH tail regexes instead of _BRAND_TAIL alone.
_REAL_BRANDS_PRESS_ROOT = ("Hankuk", "Hankook")
_NUM_WORDS_XL = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "ninety": 90,
    "satu": 1, "dua": 2, "tiga": 3, "empat": 4, "lima": 5, "enam": 6, "tujuh": 7,
    "delapan": 8, "sembilan": 9, "sepuluh": 10, "sebelas": 11,
}


def premise_term_in_topic(term: str, topic: str) -> bool:
    """ROUND-10 (moved here from narration_api so orchestrator.static can use it
    without a circular import): premise exemption with cross-language number
    matching — banned «eleven» is premise-supplied when the Indonesian brief says
    «sebelas». Word-boundary first; numeric terms match by VALUE across en/id
    words and digits."""
    if not term:
        return False
    t = topic or ""
    if re.search(r"(?i)\b" + re.escape(term) + r"\b", t):
        return True
    _val = None
    _tl = term.strip().lower()
    if _tl.isdigit():
        _val = int(_tl)
    elif _tl in _NUM_WORDS_XL:
        _val = _NUM_WORDS_XL[_tl]
    if _val is None:
        return False
    if re.search(r"\b" + str(_val) + r"\b", t):
        return True
    for _w, _v in _NUM_WORDS_XL.items():
        if _v == _val and re.search(r"(?i)\b" + _w + r"\b", t):
            return True
    if 12 <= _val <= 19:
        _ones = {2: "dua", 3: "tiga", 4: "empat", 5: "lima", 6: "enam", 7: "tujuh", 8: "delapan", 9: "sembilan"}
        _w = _ones.get(_val - 10)
        if _w and re.search(r"(?i)\b" + _w + r"\s+belas\b", t):
            return True
    return False


def build_authority_ownership(*, topic: str = "", outline: str = "", bible: str = "",
                              canon=None) -> dict:
    """F5: the deterministic set of terms this story's ACCEPTED authority owns.

    Lives here, not in `narration_api`, for the same reason `premise_term_in_topic`
    does: `orchestrator.static` needs it at Bible-pin time and cannot import that
    module. One implementation, two call sites — duplicating it would be the
    "two mechanisms for one rule" trap this workstream has already paid for four times.

    Sources, all optional, all ACCEPTED-only:
      · `topic`   — the user's own premise/goal/brief;
      · `outline` — the accepted chapter outline;
      · `bible`   — the PINNED story bible (post-generation only, never the candidate);
      · `canon`   — a Canon Lite registry: entity `canonical_name` + every accepted
                    alias, and every anchor `literal` (numbers, durations, dates,
                    addresses).

    🔴 THE CANDIDATE BIBLE IS NOT AN AUTHORITY. At pin time it has not been accepted;
    feeding it in would make every term it just invented self-exempt and switch ledger
    enforcement off entirely. Callers pass `bible=""` there — see the Bible-pin site."""
    texts = tuple(str(t) for t in (topic, outline, bible) if str(t or "").strip())
    terms: set[str] = set()

    def _fields(obj, name):
        value = getattr(obj, name, None)
        if value is None and isinstance(obj, dict):
            value = obj.get(name)
        return value

    by_category: dict = {}

    def _register(category: str, value) -> None:
        value = str(value or "").strip().lower()
        if not value:
            return
        terms.add(value)
        by_category.setdefault(category, set()).add(value)

    for entity in (_fields(canon, "entities") or ()):
        _register("name", _fields(entity, "canonical_name"))
        aliases = _fields(entity, "aliases") or ()
        if isinstance(aliases, str):
            aliases = (aliases,)
        for alias in aliases:
            _register("name", alias)
    for anchor in (_fields(canon, "anchors") or ()):
        # The anchor's own kind IS its category (number/duration/date/address/…), so a
        # `number:` ledger hit can never be exempted by a NAME that happens to read the
        # same, and vice versa.
        _register(str(_fields(anchor, "kind") or "literal").strip().lower(),
                  _fields(anchor, "literal"))
    return {"texts": texts, "terms": frozenset(terms),
            "by_category": {k: frozenset(v) for k, v in by_category.items()}}


#: Ledger categories that may be satisfied by a registry entry of a DIFFERENT category.
#: Deliberately empty: a `number:` hit is not excused by a `name:` that reads the same.
_F5_CATEGORY_ALIASES: dict = {}
#: Categories whose VALUE must itself be a number. The free-text authorities (topic,
#: outline, bible) carry no categories at all, so without this a `number:Soo` hit was
#: owned the moment an outline sentence said "Soo accepts the offer" — the text match
#: cannot tell which category it is answering. A numeric category answered by a
#: non-numeric value is incoherent on its face, so it is refused before the text
#: fallback ever runs.
#: Narrowed deliberately. `date` and `duration` were in this set and produced a
#: FALSE NEGATIVE in the other direction — an outline that legitimately says "Tuesday"
#: stopped owning `date:Tuesday`, which is worse than the leak it was closing. Only
#: categories whose ledger value is ALWAYS a bare numeral stay.
#:
def _f5_value_is_numeric(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return False
    if lowered.isdigit() or lowered in _NUM_WORDS_XL:
        return True
    # A bound literal like "30 days" is numeric when any whole token is.
    return any(part.isdigit() or part in _NUM_WORDS_XL for part in lowered.split())


#: Weekday and month vocabulary, id + en. A `date:` value is a date when it says one.
_F5_DATE_WORDS = frozenset({
    "senin", "selasa", "rabu", "kamis", "jumat", "jum'at", "sabtu", "minggu", "ahad",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "januari", "februari", "maret", "april", "mei", "juni", "juli", "agustus",
    "september", "oktober", "november", "desember",
    "january", "february", "march", "may", "june", "july", "august", "october",
    "december",
})
#: Time units that turn a numeral into a span.
_F5_DURATION_UNITS = frozenset({
    "detik", "menit", "jam", "hari", "minggu", "bulan", "tahun", "windu", "dekade",
    "second", "seconds", "minute", "minutes", "hour", "hours", "day", "days",
    "week", "weeks", "month", "months", "year", "years", "decade", "decades",
})
_F5_DATE_RX = re.compile(r"^\d{1,4}[-/.]\d{1,2}([-/.]\d{1,4})?$")


def _f5_proper_noun_occurrence(value: str, text: str) -> bool:
    """Does `value` occur in `text` as a standalone CAPITALISED token?

    The weakest claim a free-text authority can actually support: this string is used
    as a proper name here. It cannot say WHICH kind of proper name, which is why only
    `name` is satisfied by it and `place`/`food`/`organisation` are not."""
    for match in re.finditer(r"(?<!\w)" + re.escape(str(value or "")) + r"(?!\w)",
                             str(text or ""), re.IGNORECASE):
        if match.group(0)[:1].isupper():
            return True
    return False


def _f5_surname_occurrence(value: str, text: str) -> bool:
    """`value` occurs as a capitalised token ADJACENT to another capitalised token.

    🔴 THIS IS THE ROUND-4 LESSON, KEPT. A surname the user's own premise supplies —
    `Han` out of `Han Seo-jin` — is not a lane tic and must never be re-rolled away.
    What makes it a surname is that it sits inside a multi-token proper name, and that
    is checkable. `Soo` in "Soo accepts the offer" is followed by a lowercase word, so
    the same rule refuses it — which is exactly the `surname:Soo` leak."""
    text = str(text or "")
    for match in re.finditer(r"(?<!\w)" + re.escape(str(value or "")) + r"(?!\w)",
                             text, re.IGNORECASE):
        if not match.group(0)[:1].isupper():
            continue
        after = re.match(r"[ \t]+([^\W\d_]\S*)", text[match.end():])
        before = re.search(r"([^\W\d_]\S*)[ \t]+$", text[:match.start()])
        if (after and after.group(1)[:1].isupper()) or \
                (before and before.group(1)[:1].isupper()):
            return True
    return False


def _f5_date_value(value: str, _text: str) -> bool:
    lowered = str(value or "").strip().lower()
    if not lowered:
        return False
    if _F5_DATE_RX.match(lowered):
        return True
    return any(part.strip(".,") in _F5_DATE_WORDS for part in lowered.split())


def _f5_duration_value(value: str, _text: str) -> bool:
    parts = [p.strip(".,") for p in str(value or "").strip().lower().split()]
    return (any(p in _F5_DURATION_UNITS for p in parts)
            and any(p.isdigit() or p in _NUM_WORDS_XL for p in parts))


def _f5_numeric_category_value(value: str, _text: str) -> bool:
    return _f5_value_is_numeric(value)


#: 🔴 WHICH CATEGORIES FREE TEXT CAN PROVE, AND NOTHING ELSE CAN BE ASSUMED.
#: `ownership["texts"]` — topic, outline, pinned bible — is prose with no categories in
#: it. A bare occurrence of a string proves the VALUE appears; it proves nothing about
#: the SENSE. Treating occurrence as proof of category is what let an outline sentence
#: "Soo accepts the offer" own `food:Soo`, `place:Soo` and `surname:Soo` as readily as
#: `name:Soo` — three real lane-repetition tics that stopped reaching repair.
#:
#: A category is provable only when a deterministic validator can corroborate it from
#: the value, the way it is written, or both. A category with NO validator here is not
#: provable from prose and therefore never exempts: the registry's exact match remains
#: its only route to ownership. Silence is refusal, not permission.
#:
#: 🔴 AND THE FALSE NEGATIVES THIS MUST NOT REINTRODUCE. `date`/`duration` once went
#: into a blanket numeric rule and an outline that legitimately said "Tuesday" stopped
#: owning `date:Tuesday`. Typed validators separate the two cases the blanket rule
#: could not: `date:Tuesday` is a date and is owned; `duration:Tuesday` is not a span
#: and is not. The premise-supplied `surname:Han` from "Han Seo-jin" survives for the
#: same reason — it is checkable, so it is checked rather than guessed at.
_F5_CATEGORY_VALIDATORS = {
    "name": _f5_proper_noun_occurrence,
    "surname": _f5_surname_occurrence,
    "date": _f5_date_value,
    "duration": _f5_duration_value,
    "number": _f5_numeric_category_value,
    "floor": _f5_numeric_category_value,
    "count": _f5_numeric_category_value,
    "year": _f5_numeric_category_value,
    "age": _f5_numeric_category_value,
}


def _f5_free_text_may_prove(value: str, category: str, text: str) -> bool:
    """May this free-text authority be read as answering THIS category?

    An uncategorised ledger hit is unchanged — there is no category to prove, and the
    deterministic matcher decides alone, exactly as before."""
    if not category:
        return True
    validator = _F5_CATEGORY_VALIDATORS.get(category)
    if validator is None:
        return False
    return bool(validator(value, text))


def _f5_category_conflict(lowered: str, category: str, by_category) -> bool:
    """Does the registry place this exact value under a DIFFERENT category?

    ⚠️ PRECONDITION: the value is NOT registered under `category` itself. The single
    door for that is `authority_owns_term`'s exact-match return, which fires long
    before this is reached. This function guarded it a second time ("if it IS this
    category, no conflict") and a mutation run scored that guard SURVIVED — because it
    was unreachable, not because the test was weak. Two mechanisms for one rule is the
    failure this workstream has now paid for seven times; the guard is gone and the
    early return is the only door.

    🔴 THE LEAK THIS CLOSES. `ownership["texts"]` — topic, outline, pinned bible — is
    free prose carrying no categories at all, so the text fallback can only ever prove
    that a VALUE appears, never that the authority used it in the sense the ledger is
    asking about. An outline sentence "Soo accepts the offer" therefore owned
    `food:Soo`, `surname:Soo` and `place:Soo` just as readily as `name:Soo`, and every
    one of those is a real lane-repetition tic that stopped reaching repair.

    The registry is the one authority here that IS categorised, so it is what answers.
    When it records this value under some other category, it has already said what the
    value is, and prose that merely contains the word cannot overrule it.

    🔴 AND WHY THIS IS NOT "REFUSE WHENEVER THE CATEGORY IS UNPROVEN". That was tried in
    the value-shape rule above: putting `date`/`duration` into the numeric set made an
    outline that legitimately says "Tuesday" stop owning `date:Tuesday` — a false
    NEGATIVE, which sends a legitimately-established term to repair. Refusal here is
    driven by positive evidence of a DIFFERENT category, never by absence of evidence,
    so a value the registry has never heard of is left exactly as it was.

    Residual, deliberately: when no registry entry exists for the value at all, free
    text remains the only authority and `food:Soo` is still owned by an outline that
    says "Soo". Nothing structured exists to contradict it, and refusing on silence is
    the `date:Tuesday` failure again."""
    if not category or not by_category:
        return False
    aliases = set(_F5_CATEGORY_ALIASES.get(category, ()))
    return any(lowered in (values or ())
               for other, values in by_category.items()
               if other != category and other not in aliases)


def authority_owns_term(term, ownership, category: str = "") -> bool:
    """Does the accepted authority own `term`? Category-and-value, never free substring.

    🔴 THE FALSE EXEMPTION IS THE DANGEROUS DIRECTION. Every term wrongly called "owned"
    is a real lane-repetition tic that stops reaching repair, so matching is deliberately
    narrow:
      · a registered canon term matches EXACTLY (case-insensitively) — `Jun` is owned
        only when `Jun` is itself a registered alias, never because it sits inside a
        registered `Jun-ho`;
      · a NUMERIC term may additionally match word-bounded INSIDE a registered literal,
        which is what lets the bound literal `30 days` own `30` without letting a name
        own a longer name;
      · the free-text authorities use the existing deterministic matcher, including its
        cross-language numerics (its coverage is the limit — see the suite's pinned
        note on Indonesian tens)."""
    text = str(term or "").strip()
    if not text or not isinstance(ownership, dict):
        return False
    category = str(category or "").strip().lower()
    by_category = ownership.get("by_category") or {}
    if category and by_category:
        # 🔴 CATEGORY IS PART OF THE VALUE. The ledger reports `name:Tae-jun`,
        # `number:11`, `floor:6`. Dropping the category before the check let a value
        # owned in ONE category exempt the same value in another — "match category and
        # value" was the claim, and without this it was only "match value".
        registered = by_category.get(category) or frozenset()
        for alias_of in _F5_CATEGORY_ALIASES.get(category, ()):
            registered = registered | (by_category.get(alias_of) or frozenset())
    else:
        registered = ownership.get("terms") or frozenset()
    if text.lower() in registered:
        return True
    lowered = text.lower()
    is_numeric = lowered.isdigit() or lowered in _NUM_WORDS_XL
    if is_numeric and registered:
        # The literal set as one blob, so the shared matcher's cross-language numeric
        # equivalence applies to bound literals too.
        if premise_term_in_topic(text, "\n".join(sorted(registered))):
            return True
    if _f5_category_conflict(lowered, category, by_category):
        # The registry already places this value in another category, so the free-text
        # fallback below — which cannot tell WHICH category a sentence is answering —
        # must not be allowed to overrule it.
        return False
    for authority_text in ownership.get("texts") or ():
        # 🔴 OCCURRENCE IS NOT PROOF OF CATEGORY. The matcher below only ever answers
        # "does this VALUE appear in this prose". Asking it a categorised question
        # without this gate is what made `food:Soo` owned by a sentence about a person
        # named Soo. The old gate here was a single numeric-shape rule; it could not
        # tell `date:Tuesday` (owned) from `duration:Tuesday` (not), so it is replaced
        # by the typed validator table rather than joined to it.
        if not _f5_free_text_may_prove(text, category, authority_text):
            continue
        if premise_term_in_topic(text, authority_text):
            return True
    return False


def _lev_le1(a: str, b: str) -> bool:
    """Edit distance <= 1 (round-8: Hanshin≈Hanjin — the model orbited the
    BLOCKLIST itself one roll after Hanjin was banned)."""
    a, b = a.lower(), b.lower()
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        return sum(1 for x, y in zip(a, b) if x != y) <= 1
    if la > lb:
        a, b, la, lb = b, a, lb, la
    i = j = diff = 0
    while i < la and j < lb:
        if a[i] != b[j]:
            diff += 1
            if diff > 1:
                return False
            j += 1
        else:
            i += 1
            j += 1
    return True


_BRAND_TAIL = (r"(?:\s+(?:Group|Corporation|Corp|Construction|Engineering|E&C|Heavy|"
               r"Marine|Industries|Industrial|Telecom|Electronics|Chemical|Energy|"
               r"Development|Builders?|Shipbuilding))")


# press-style suffix, parallel to _BRAND_TAIL: lets the round-8 FUZZY pass (below)
# protect newspaper names too — _BRAND_TAIL alone only recognizes corporate
# suffixes, so a camouflage respelling of e.g. Hankuk Ilbo had no tail to anchor on.
_PRESS_TAIL = r"(?:\s+(?:Daily|Ilbo|Times|Post|News|Shinmun))"


_BRAND_CULPABLE_RX = re.compile(
    r"(?i)liab|guilt|falsif|bribe|criminal|defendant|dissolv|negligen|indict|convict|"
    r"fraud|cover-up|withdrew the report|lawsuit|sued|charge")


def _brand_role(text: str, rx) -> str:
    """ROUND-6 (lens-4 CC brief): a real brand as VILLAIN (Doosan R8, Hanjin SBF)
    is a legal blocker; a real brand as PROP (a dented grey Hyundai, a Verbatim
    drive) is normal fiction furniture. A brand is culpable if ANY of its
    occurrences sits within ±150 chars of culpability vocabulary — first-mention-
    only classification inverted Hanjin/Hyundai on the SBF roll (Hanjin's 'liable'
    is its tenth mention; Hyundai's only mention brushed the word 'collapse')."""
    for m in rx.finditer(text):
        lo, hi = max(0, m.start() - 150), min(len(text), m.end() + 150)
        if _BRAND_CULPABLE_RX.search(text[lo:hi]):
            return "culpable"
    return "prop"


# ── real-brand entity-use predicate (NARASI_BRAND_REPORT, default OFF) — round-15,
# report-only. A real brand used as an in-story CORPORATE ENTITY (firm/client/employer/
# HQ/board) within ±120 chars, distinct from culpable use (real_brand_scan/_brand_role
# own that) and nominative prop use ("a dented grey Hyundai"). Consumed only by the gated
# narration_api consumer; defining it here changes nothing until that flag is on.
_BRAND_ENTITY_RX = re.compile(
    r"\b(?:group|corp\w*|company|firm|law\s+firm|conglomerate|chaebol|holdings?|"
    r"subsidiary|affiliate|client|contractor|developer|builder|CEO|chairman|board|"
    r"headquarters|HQ|executives?|shares?|stock|IPO|merger|acquired|"
    r"employ\w+|represent(?:ed|s|ing)?)\b", re.I)

def _brand_entity_use(text: str, rx) -> bool:
    if not text:
        return False
    for m in rx.finditer(text):
        lo, hi = max(0, m.start() - 120), min(len(text), m.end() + 120)
        if _BRAND_ENTITY_RX.search(text[lo:hi]):
            return True
    return False


def real_brand_scan(text: str, *, bible: str = "") -> dict:
    """Report-only FLAG/PASS: real Korean conglomerate names used as in-story
    corporate entities. Never raises; never OVER (excluded from diet)."""
    out: dict = {"status": "OFF", "hits": []}
    if not _brand_scan_on():
        return out
    try:
        pats = [(b, re.compile(r"\b" + re.escape(b) + r"\b")) for b in dict.fromkeys(_REAL_BRANDS_LONG)]
        pats += [(b, re.compile(r"\b" + re.escape(b) + _BRAND_TAIL)) for b in _REAL_BRANDS_SHORT]
        # "Hankuk"/"Hankook" are generic Korea-romanization roots (see comment at
        # _REAL_BRANDS_PRESS_ROOT) — only count with a PRESS or corporate tail, never bare.
        pats += [(b, re.compile(r"\b" + re.escape(b) + r"(?:" + _BRAND_TAIL + "|" + _PRESS_TAIL + r")"))
                 for b in _REAL_BRANDS_PRESS_ROOT]
        for where, txt in (("bible", bible or ""), ("manuscript", text or "")):
            if not txt:
                continue
            for brand, rx in pats:
                m = rx.search(txt)
                if m:
                    out["hits"].append({
                        "brand": brand, "where": where,
                        "count": len(rx.findall(txt)),
                        "role": _brand_role(txt, rx),
                        "snippet": re.sub(r"\s+", " ", txt[max(0, m.start() - 40):m.end() + 60])[:130]})
        # round-8 FUZZY pass: capitalized token + corporate OR press tail, edit-distance
        # <=1 to a blocklisted name but not exact — catches the Hanjin→Hanshin orbit
        # class, now also newspaper camouflage (e.g. Hankuk Ilbo -> Hanguk Ilbo).
        _fz_rx = re.compile(r"\b([A-Z][a-z]{4,})(?:" + _BRAND_TAIL + "|" + _PRESS_TAIL + r")")
        # _REAL_BRANDS_PRESS_ROOT included so "Hankuk"/"Hankook" stay available as FUZZY
        # anchors (catching e.g. "Hanguk Ilbo") even though they were removed from the
        # bare/unconditional exact-match pass above.
        _fz_names = [b.lower() for b in list(_REAL_BRANDS_LONG) + list(_REAL_BRANDS_SHORT)
                     + list(_REAL_BRANDS_PRESS_ROOT) if len(b) >= 5]
        for where, txt in (("bible", bible or ""), ("manuscript", text or "")):
            if not txt:
                continue
            _fz_seen = set()
            for m2 in _fz_rx.finditer(txt):
                cand = m2.group(1)
                if cand.lower() in _fz_names or cand in _fz_seen:
                    continue
                for bn in _fz_names:
                    if _lev_le1(cand, bn):
                        _fz_seen.add(cand)
                        out["hits"].append({
                            "brand": f"{cand}≈{bn}", "where": where, "fuzzy": True,
                            "count": len(re.findall(r"\b" + re.escape(cand) + r"\b", txt)),
                            "role": _brand_role(txt, re.compile(r"\b" + re.escape(cand) + r"\b")),
                            "snippet": re.sub(r"\s+", " ", txt[max(0, m2.start() - 40):m2.end() + 60])[:130]})
                        break
                if len(_fz_seen) >= 4:
                    break
        out["status"] = "FLAG" if out["hits"] else "PASS"
        return out
    except Exception:  # noqa: BLE001
        return {"status": "PASS", "hits": []}


def _levenshtein_le2(a: str, b: str) -> bool:
    """True when edit distance ≤ 2 (early-exit band algorithm, lowercase compare)."""
    a, b = a.lower(), b.lower()
    if a == b:
        return True
    if abs(len(a) - len(b)) > 2:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        row_min = i
        for j, cb in enumerate(b, 1):
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            cur.append(v)
            row_min = min(row_min, v)
        if row_min > 2:
            return False
        prev = cur
    return prev[-1] <= 2


def _abort_seam_on() -> bool:
    return os.environ.get("NARASI_ABORT_SEAM_SCAN", "0").strip().lower() in ("1", "true", "yes", "on")


_ABORT_SEAM_RX = re.compile(r"\b\w[\w-]* \u2014 no\.\s+[A-Z]")


def _meta_ref_on() -> bool:
    return os.environ.get("NARASI_METALEAK_SCAN", "0").strip().lower() in ("1", "true", "yes", "on")


_META_REF_RX = re.compile(
    # trigger-word gap: "before Chapter 6 hung between all of them" fell through the
    # original alternation (no temporal/positional connectors). Added before/after/
    # until/since. Deliberately NOT adding "by"/"through" — idiom-collision risk
    # ("she'd read the whole thing by chapter six" is legitimate in-world narration).
    r"(?i)\b(?:belongs to|that(?:'s| is) for|save(?:d)? for|see|in|comes in|"
    r"covered in|happens in|before|after|until|since)\s+(?:ch|chapter|chap|episode|ep)\.?\s*"
    # ROUND-14: match a DIGIT (Ch9) OR a spelled-out ordinal/cardinal (roll-15 gap:
    # "the map she had drawn in Chapter Three" slipped past the digit-only r13 regex).
    r"(?:\d{1,2}|(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
    r"first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth))\b"
    # ROUND-14 audit: a real out-of-world leak is followed by punctuation ("belongs to
    # Ch9.", "in Chapter Three, and"); "chapter one OF her life" / "chapter twelve of the
    # report" is a metaphor or an in-world document ref — not a generator artifact. Skip it.
    r"(?!\s+of\b)"
    # AUDIT FIX (comma-gap): the "of" carve-out above only looks at the word immediately
    # after the match, so "since chapter one, in the book of his own life" — the SAME
    # metaphor, with a short comma-delimited clause between the number and "of" — still
    # matched as a leak. Bounded (<=4 words between the comma and "of", a literal
    # possessive/article alternation right after "of", one more required trailing word)
    # so a genuine leak that merely happens to contain "of" later in the sentence isn't
    # re-exempted, and so the lookahead can't run away on adversarial/long input.
    r"(?!\s*,\s+(?:\w+\s+){0,4}of\s+(?:her|his|their|its|your|my|our|the|a|an)\s+\w+)"
    r"|\bthe rest\s+[\u2014\-]\s*that belongs to\b"
    # r16 (S2-Th-3 finding): "during Season One's Archive opening" leaked past the ch/chapter/
    # episode-only lexicon (trigger word "during" and noun "Season" were both outside it).
    # Scoped to "Season <Ordinal/Number>" \u2014 a serialized-production self-reference with no
    # plausible diegetic reading in MOST cases (unlike "chapter one OF her life", a common
    # in-world metaphor the sibling branch above already carves out). Deliberately NOT
    # extending to "this season"/"this volume" \u2014 both have real diegetic uses (weather/
    # harvest/sports; a physical book prop) per adversarial review.
    # AUDIT FIX: added the SAME "(?!\s+of\b)" carve-out as the sibling branch \u2014 "Season Two
    # OF the tournament" is a genuine in-world sports/competition reference, not a meta-leak
    # (missed in the original cut). A second exclusion (a character WATCHING an in-world
    # show's "Season One") is a Python-level context check in meta_reference_scan below,
    # since a variable-length preceding-verb lookbehind isn't expressible in one regex.
    r"|\bSeason\s+(?:One|Two|Three|Four|Five|Six|Seven|Eight|Nine|Ten|\d{1,2})\b(?!\s+of\b)")


# r16 AUDIT FIX: a character diegetically WATCHING an in-world show's "Season One" ("she'd
# binge-watched Season Two over the weekend") is a legitimate in-story activity, not a
# production self-reference — excluded via a short pre-context check (a variable-length
# preceding-verb lookbehind isn't expressible in _META_REF_RX's single regex).
_META_WATCH_VERB_RX = re.compile(
    r"(?i)\b(?:watch(?:ed|ing)?|binge[- ]watch(?:ed|ing)?|stream(?:ed|ing)?|"
    r"finish(?:ed|ing)?|start(?:ed|ing)?|rewatch(?:ed|ing)?)\s+(?:the\s+(?:show\s+)?)?$")


def meta_reference_scan(text: str) -> dict:
    """ROUND-13 (roll-15 regression): an outline/generator artifact leaked into a
    character's DIALOGUE -- 'The rest -- that belongs to Ch9.' The r4.1 meta-leak
    guards catch analysis BLOCKS, not an in-clause chapter self-reference. A story's
    own characters never say 'Ch9' / 'chapter 9' / 'episode 3'; deterministic net.
    Report-only."""
    hits = []
    if not text:
        return {"count": 0, "hits": []}
    for m in _META_REF_RX.finditer(text):
        if m.group(0).lower().startswith("season") and _META_WATCH_VERB_RX.search(text[max(0, m.start() - 30):m.start()]):
            continue  # "binge-watched Season Two" — an in-world viewing activity, not a leak
        lo = text.rfind("\n", 0, m.start()) + 1
        hi = text.find("\n", m.end())
        line = text[lo:hi if hi != -1 else len(text)]
        hits.append({"snippet": line.strip()[:160]})
        if len(hits) >= 6:
            break
    return {"count": len(hits), "hits": hits}


# ── provenance-citation leak (NARASI_PROVENANCE_LEAK, default OFF) — round-15,
# report-only. In-world prose that cites the story's own scaffolding (story bible /
# outline / canon / fact-sheet) — a generator artifact a character would never utter.
def _provenance_leak_on() -> bool:
    return os.environ.get("NARASI_PROVENANCE_LEAK", "0").strip().lower() in ("1", "true", "yes", "on")

_PROVENANCE_RX = re.compile(
    # trigger + an UNAMBIGUOUS craft-doc artifact (never an ordinary in-world noun)
    r"\b(?:per|as\s+per|according\s+to|noted?\s+in|listed\s+in|from|see)\s+"
    r"(?:the\s+)?(?:story\s+bible|series\s+bible|show\s+bible|"
    r"fact[-\s]?sheet|character\s+sheet|continuity\s+(?:bible|notes?)|"
    # weak nouns (outline/canon/spec/synopsis/treatment) ONLY when story/series/character-
    # qualified — "the outline of the valley" is in-world; "the story outline" is a leak.
    r"(?:story|series|character|plot|episode|season|show)\s+"
    r"(?:outline|canon|spec(?:\s+sheet)?|synopsis|treatment|brief|arc))\b"
    r"|\b(?:canonical\s+facts?|per\s+canon|story\s+bible|fact[-\s]?sheet)\b", re.I)

def provenance_leak_scan(text: str) -> dict:
    hits = []
    if not text:
        return {"count": 0, "hits": []}
    for m in _PROVENANCE_RX.finditer(text):
        lo = text.rfind("\n", 0, m.start()) + 1
        hi = text.find("\n", m.end())
        line = text[lo:hi if hi != -1 else len(text)]
        hits.append({"snippet": line.strip()[:160], "cite": m.group(0)})
        if len(hits) >= 6:
            break
    return {"count": len(hits), "hits": hits}


def abort_seam_scan(text: str) -> dict:
    """ROUND-6 (SBF roll): the chapter-level ban line can make the model start a
    banned default, cancel itself MID-CLAUSE, and ship the correction into prose
    ("Eo-jin made barley \u2014 no. He boiled water for instant coffee"). The gate
    worked; its skid mark reached the reader. Deterministic net for that seam
    shape. Quoted dialogue is exempt \u2014 characters may legitimately say "\u2014 no.".
    Report-only."""
    hits = []
    if not text:
        return {"count": 0, "hits": []}
    for m in _ABORT_SEAM_RX.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        line = text[line_start:text.find("\n", m.end()) if text.find("\n", m.end()) != -1 else len(text)]
        # inside quoted speech on the same line -> legit self-interruption
        if line.count('"') >= 2 or "\u201c" in line[:m.start() - line_start]:
            continue
        hits.append({"snippet": text[max(0, m.start() - 60):m.end() + 40].replace("\n", " ")})
        if len(hits) >= 6:
            break
    return {"count": len(hits), "hits": hits}


def _coda_dedup_on() -> bool:
    return os.environ.get("NARASI_CODA_DEDUP_SCAN", "0").strip().lower() in ("1", "true", "yes", "on")


def coda_repeat_scan(text: str) -> dict:
    """ROUND-6 (SBF roll): Ch3 and Ch4 shipped the IDENTICAL closing aphorism
    ("Some truths are buried under concrete, others under a signature.") \u2014 a 2x
    verbatim repeat sits below REFRAIN_SCAN's radar. Compare each chapter's final
    non-empty line, normalized; flag exact duplicates. Report-only.

    r16 (S2-Th-3 finding): the normalization used to only collapse whitespace + case, so a
    maxim repeated with only its TRAILING PUNCTUATION changed ("...allowed to knock." vs
    "...allowed to knock;") slipped past as two "different" strings. Now also strips trailing
    punctuation before the equality compare \u2014 the stored `coda` (used for the reported
    snippet) is unaffected, only the dedup KEY changes.

    EVIDENCE-LOCATABILITY (2026-07-17): `coda` used to be lowercased at capture time (below),
    so the "reported snippet" the r16 comment above describes was ALREADY case-destroyed before
    r16 ever touched it \u2014 a real, verbatim chapter-closing line, but with its case wiped, so
    laozhang_api.py's _occ() (a CASE-SENSITIVE \\b-bounded search) could almost never re-locate
    it against the real mixed-case manuscript (sentences are virtually always capitalized).
    `coda` now keeps the manuscript's ORIGINAL case; lowercasing is applied only inline, when
    building the dedup `_key` below \u2014 extending the same store-vs-dedup separation r16
    already established for punctuation-stripping to case as well."""
    if not text:
        return {"count": 0, "pairs": []}
    # AUDIT FIX (r16): the internal pipeline format is "## Chapter N: Title" (per
    # orchestrator/static.py's _chapter_md / _POLISH_CHAPTER_SPLIT_RX) — a bare "Chapter N"
    # split never matched, making this scanner dead code on every real chaptered manuscript.
    # "#{0,3}\s*" accepts 0-3 leading #'s so a bare-text export (no markdown) still matches too.
    blocks = re.split(r"(?m)^(?=#{0,3}\s*Chapter \d+\s*[:.])", text)
    codas: list[tuple[int, str]] = []
    for b in blocks:
        m = re.match(r"#{0,3}\s*Chapter (\d+)", b)
        if not m:
            continue
        lines = [ln.strip() for ln in b.rstrip().split("\n") if ln.strip()]
        if len(lines) >= 2:
            # EVIDENCE-LOCATABILITY: original case preserved here (no .lower()) -- see docstring.
            codas.append((int(m.group(1)), re.sub(r"\s+", " ", lines[-1])))
    seen: dict[str, int] = {}
    pairs = []
    for ch, coda in codas:
        if len(coda) < 25:
            continue
        # AUDIT FIX: keep ?/! in the strip charclass EXCLUDED — a coda ending in "?" is a
        # question, one ending in "." is a statement; stripping both to the same key would
        # merge two codas with the SAME words but different clause type (different meaning)
        # into a false "duplicate". Only strip punctuation that's genuinely craft-equivalent
        # (period/semicolon/comma/ellipsis/quotes) — a real near-verbatim repeat differing
        # ONLY by one of these still collapses to the same key. .lower() applied HERE ONLY --
        # the dedup key must stay case-insensitive; `coda` itself stays original-case.
        _key = re.sub(r"[.;,…\"'“”]+$", "", coda).strip().lower()
        if _key in seen:
            pairs.append({"chapters": [seen[_key], ch], "coda": coda[:120]})
        else:
            seen[_key] = ch
    return {"count": len(pairs), "pairs": pairs}


def _ledger_fuzzy_on() -> bool:
    return os.environ.get("NARASI_LEDGER_FUZZY", "0").strip().lower() in ("1", "true", "yes", "on")


def _alias_ledger_on() -> bool:
    return os.environ.get("NARASI_ALIAS_LEDGER", "0").strip().lower() in ("1", "true", "yes", "on")


_REAL_GEO_WHITELIST = {
    # round-8: real Korean geography that collided with the fictional-place prefix net
    # (Cheonggyecheon/Cheongnyangni ≈ banned "Cheongpa" — the real places are fine)
    "cheonggyecheon", "cheongnyangni", "cheongju", "cheongdam", "gangnam", "jongno",
    "dongdaemun", "yeongdeungpo", "cheonan", "chuncheon", "changsin", "yeongdo",
}


def ledger_fuzzy_names(text: str, *, style_key: Optional[str] = None) -> dict:
    """Round-5.1 near-variant net (Law-4 endgame evidence: Ik-ro reused VERBATIM in
    R8 while Yul-so/Bok-rye/Noh dodged exact bans by 1-2 edits). Hyphenated given-name
    candidates in the text vs ledger given_names at edit distance ≤2 (exact hits are
    the exact scan's job — only true near-misses reported). FLAG/PASS, report-only."""
    out: dict = {"status": "OFF", "hits": []}
    if not _ledger_fuzzy_on():
        return out
    try:
        import json as _j
        _p = os.path.join(os.path.dirname(__file__), "pakem", "lane_ledger.json")
        with open(_p, encoding="utf-8") as f:
            lane = (_j.load(f) or {}).get((style_key or "").strip()) or {}
        banned = [_ledger_term(x) for x in (lane.get("given_names") or []) if _ledger_term(x)]
        if not banned:
            return {"status": "PASS", "hits": []}
        cands = sorted({m.group(1) for m in re.finditer(r"\b([A-Z][a-z]+-[a-z]+)\b", text or "")})
        seen: set = set()
        for c in cands[:80]:
            for b in banned:
                if c.lower() == b.lower():
                    break   # exact — the exact scan owns it
                # same-initial requirement: suffix-driven pairs (Yeon-ok≈Byeong-ok,
                # Jae-sung≈Tae-sun) are coincidence, not dodging; a dodge keeps the head.
                # r5.2: Korean romanization variants share a SOUND, not a letter — 노 is
                # Noh/Roh/No, 이 is Lee/Yi/Rhee, 류 is Ryu/Yoo/Yu — so equivalence
                # CLASSES stand in for raw initials (roll-9: Noh Gi-jun ≈ banned Roh).
                _iq = {"n": "nrl", "r": "nrl", "l": "nrl", "y": "yr", "i": "iy"}
                _ci, _bi = c[:1].lower(), b[:1].lower()
                if _ci != _bi and _bi not in _iq.get(_ci, _ci):
                    continue
                if _levenshtein_le2(c, b) and (c, b) not in seen:
                    seen.add((c, b))
                    out["hits"].append({"name": c, "near": b})
                    break
        # r5.2 PLACE-STEM net: an invented town orbiting a banned one by suffix swap
        # (roll-9: banned Yeongdeok → REAL neighboring Yeonghae) — shared prefix ≥5
        # chars with a banned place = WARN. Report-only; curation arbitrates.
        try:
            banned_places = [_ledger_term(x).split()[0] for x in (lane.get("places") or []) if _ledger_term(x)]
            place_cands = sorted({m.group(1) for m in re.finditer(
                r"\b([A-Z][a-zà-ÿ]{4,}(?:-(?:ro|gil|dong|gu|si|gun|do|myeon|eup|ri))?)\b", text or "")})
            for c in place_cands[:60]:
                if c.lower().split("-")[0] in _REAL_GEO_WHITELIST:
                    continue   # round-8: real geography is not an orbit
                for b in banned_places:
                    if len(b) >= 5 and c.lower() != b.lower() and c[:5].lower() == b[:5].lower()                             and (c, b) not in seen:
                        seen.add((c, b))
                        out["hits"].append({"name": c, "near": b, "class": "place-stem"})
                        break
        except Exception:  # noqa: BLE001
            pass
        out["status"] = "FLAG" if out["hits"] else "PASS"
        return out
    except Exception:  # noqa: BLE001
        return {"status": "PASS", "hits": []}


# ── alias↔legal-name lock (NARASI_ALIAS_LEDGER, default OFF) — round-15, report-only.
# One surname carrying ≥2 given-names is an alias/legal fork; the dominant (most-used)
# given-name is presumed legal. An alias landing in a document/registry context (envelope,
# RRN, court file) — or the legal name labelled a "working name" — is a misfile.
_ALIAS_SURNAME_GIVEN_RX = re.compile(r"\b([A-Z][a-z]+)\s+([A-Z][a-z]+-[a-z]+)\b")
# ROUND-15: sentence-initial / common capitalized words are NOT surnames — kills the
# "But Do-yoon" / "And So-ra" false forks (a capitalized clause-opener + a hyphenated
# Korean given name). Real Korean surnames are outside this closed set.
_ALIAS_STOP_SN = {"But", "And", "The", "When", "Then", "That", "This", "She", "He",
                  "They", "His", "Her", "Their", "It", "As", "At", "In", "On", "For",
                  "With", "From", "So", "Yet", "Now", "Here", "There", "Not", "No", "If",
                  "Or", "Nor", "Because", "Since", "While", "After", "Before", "Later",
                  "Outside", "Inside", "Every", "Each", "Some", "What", "Why", "How",
                  "Where", "Who", "Above", "Below", "Behind", "Beside", "Even", "Only",
                  "Both", "Still", "Once", "Never", "Always", "Perhaps", "Maybe", "Of"}
_ALIAS_DOC_RX = re.compile(
    r"\b(?:envelope|addressed\s+to|registry|registered|resident\s+registration(?:\s+number)?|"
    r"\bRRN\b|court\s+(?:file|record|filing)|official\s+record|birth\s+certificate|ID\s+card|"
    r"identity\s+card|deed|census|registration\s+card|legal\s+name|registered\s+name)\b", re.I)
_ALIAS_LABEL_RX = re.compile(r"\b(?:working\s+name|alias|goes\s+by|known\s+as|assumed\s+name|pen\s+name|street\s+name)\b", re.I)

def alias_legal_lock_scan(text: str) -> dict:
    out = {"status": "PASS", "forks": [], "misfiled": []}
    if not text:
        return out
    try:
        by_surname: dict = {}
        for m in _ALIAS_SURNAME_GIVEN_RX.finditer(text):
            sn, gv = m.group(1), m.group(2)
            if sn in _ALIAS_STOP_SN:
                continue
            by_surname.setdefault(sn, {}).setdefault(gv, []).append(m.start())
        for sn, givens in by_surname.items():
            if len(givens) < 2:
                continue
            ranked = sorted(givens, key=lambda g: len(givens[g]), reverse=True)
            legal, aliases = ranked[0], ranked[1:]
            out["forks"].append({"family": sn, "legal_presumed": f"{sn} {legal}",
                "aliases": [f"{sn} {a}" for a in aliases],
                "note": (f"surname '{sn}' carries {len(givens)} given-names "
                         f"{[f'{sn} {g}' for g in ranked]} — alias vs legal fork; lock system/registry docs to the legal name")})
            for a in aliases:
                for pos in givens[a]:
                    win = text[max(0, pos - 90):pos + 90]
                    if _ALIAS_DOC_RX.search(win):
                        out["misfiled"].append({"name": f"{sn} {a}", "legal": f"{sn} {legal}",
                            "note": f"'{sn} {a}' (alias) appears in a document/registry context where legal name '{sn} {legal}' is required",
                            "context": win.strip().replace(chr(10), " ")[:120]})
                        break
            for pos in givens[legal]:
                win = text[max(0, pos - 60):pos + 60]
                if _ALIAS_LABEL_RX.search(win):
                    out["misfiled"].append({"name": f"{sn} {legal}", "legal": f"{sn} {legal}",
                        "note": f"the dominant/legal name '{sn} {legal}' is labelled a working/assumed name (backwards)",
                        "context": win.strip().replace(chr(10), " ")[:120]})
                    break
        out["forks"], out["misfiled"] = out["forks"][:5], out["misfiled"][:5]
        if out["forks"] or out["misfiled"]:
            out["status"] = "FLAG"
        return out
    except Exception:  # noqa: BLE001
        return {"status": "PASS", "forks": [], "misfiled": []}


# ── r16-S1: unsubstituted template-placeholder scan (NARASI_PLACEHOLDER_SCAN) ────────────
# S2-Th-3 finding: a raw '[date of today]' token survived to the final manuscript (L3373,
# "Reinterred Jeonju, [date of today]. Consent tier: public.") — the date-substitution step
# silently no-op'd, and the phantom-name gate mislabeled the bracket as a name. Curated token
# list (NOT a generic bracket scan — real in-world brackets like stage directions
# "[Cut to black]" or a citation must not fire).
def _placeholder_scan_on() -> bool:
    return os.environ.get("NARASI_PLACEHOLDER_SCAN", "0").strip().lower() in ("1", "true", "yes", "on")


_PLACEHOLDER_RX = re.compile(
    r"\[(?:date of today|today's date|current date|insert[^\]]{0,40}|TODO[^\]]{0,40}|TBD|"
    r"placeholder[^\]]{0,40}|character name[^\]]{0,20}|\bNAME\b[^\]]{0,20}|XXX+|"
    r"fill[- ]in[^\]]{0,40}|to be (?:determined|decided|filled)[^\]]{0,20})\]", re.I)


def placeholder_scan(text: str) -> dict:
    """Unsubstituted template-placeholder tokens shipped in final prose. Report-only."""
    out = {"count": 0, "hits": []}
    if not text:
        return out
    try:
        for m in _PLACEHOLDER_RX.finditer(text):
            lo = text.rfind("\n", 0, m.start()) + 1
            hi = text.find("\n", m.end())
            line = text[lo:hi if hi != -1 else len(text)]
            out["hits"].append({"token": m.group(0), "snippet": line.strip()[:160]})
            if len(out["hits"]) >= 6:
                break
        out["count"] = len(out["hits"])
        return out
    except Exception:  # noqa: BLE001
        return {"count": 0, "hits": []}


# ── r16-S2: entity-quantity consistency gate (NARASI_ENTITY_QTY) ─────────────────────────
# S2-Th-3 finding: "the Shadow Ledger" pinned at 37 names/entries throughout Ch5/Ch8, then
# Ch10 attributes "112 unlisted victims" to the same ledger with zero setup — an unforeshadowed
# resize. Tracks a named quantitative entity (Ledger/Registry/Archive/Database/etc) and flags
# >=2 widely divergent counts attributed to it. Reuses the r15-B style: nearest-entity
# attribution within a proximity window, ratio-based divergence threshold.
def _entity_qty_on() -> bool:
    return os.environ.get("NARASI_ENTITY_QTY", "0").strip().lower() in ("1", "true", "yes", "on")


_ENTITY_ANCHOR_RX = re.compile(
    r"\b(?:the\s+)?((?:[A-Z][a-z]+\s+){0,2}(?:Ledger|Registry|Archive|Database|List|Trust|Fund|Program))\b")
_QTY_UNIT_RX = re.compile(
    r"\b(\d[\d,]{0,5}"                                     # digits: "112"
    r"|[a-z]+\s+hundred(?:\s+and)?\s+[a-z]+(?:-[a-z]+)?"   # "one hundred and twelve" / "one hundred twelve"
    r"|[a-z]+\s+hundred"                                    # "one hundred"
    r"|[a-z]+(?:-[a-z]+)?)"                                 # "thirty-seven" / "twelve"
    # up to 2 filler words ("unlisted", "additional") between the number and the unit noun.
    r"\s+(?:[a-z]+\s+){0,2}(?:names?|entries|entr(?:y|ies)|pages?|letters?|"
    r"envelopes?|victims?|people|persons?|records?)\b", re.I)


def _entity_qty_num(s: str) -> Optional[int]:
    """Word/digit -> int, including 'thirty-seven' and 'one hundred and twelve'-style
    compounds (bare _a2_leadnum only covers simple 1-20/tens/hundred alone)."""
    s = (s or "").strip().lower()
    if re.match(r"^[\d,]+$", s):
        try:
            return int(s.replace(",", ""))
        except Exception:  # noqa: BLE001
            return None
    m = re.match(r"^(\w+)\s+hundred(?:\s+and)?(?:\s+(\w+(?:-\w+)?))?$", s)
    if m:
        h = _A2_NW.get(m.group(1))
        if h is None:
            return None
        rest = 0
        if m.group(2):
            r = m.group(2)
            if "-" in r:
                t, o = r.split("-", 1)
                rest = _A2_NW.get(t, 0) + _A2_NW.get(o, 0)
            else:
                rest = _A2_NW.get(r, 0)
        return h * 100 + rest
    if "-" in s:
        t, o = s.split("-", 1)
        if t in _A2_NW and o in _A2_NW:
            return _A2_NW[t] + _A2_NW[o]
    return _A2_NW.get(s)


def entity_quantity_scan(text: str) -> dict:
    """Same named quantitative entity given >=2 widely divergent counts. Report-only."""
    out = {"status": "PASS", "forks": []}
    if not text:
        return out
    try:
        qty_by_entity: dict[str, set] = {}
        for m in _QTY_UNIT_RX.finditer(text):
            n = _entity_qty_num(m.group(1))
            if n is None or not (2 <= n <= 100000):
                continue
            win = text[max(0, m.start() - 200):m.end() + 200]
            em = None
            for e in _ENTITY_ANCHOR_RX.finditer(win):
                em = e  # last match in window wins — same nearest-mention convention as r15
            if em:
                # Normalize away a leading "The"/"the" so a sentence-initial capitalized mention
                # ("The Shadow Ledger...") and a mid-sentence one ("...the Shadow Ledger had...")
                # key to the SAME entity instead of silently forking on capitalization alone.
                ename = re.sub(r"^the\s+", "", re.sub(r"\s+", " ", em.group(1)).strip(), flags=re.I)
                qty_by_entity.setdefault(ename, set()).add(int(n))
        for ename, qtys in qty_by_entity.items():
            if len(qtys) >= 2:
                lo, hi = min(qtys), max(qtys)
                if lo > 0 and hi / lo >= 2.0 and (hi - lo) >= 10:
                    out["forks"].append({"entity": ename, "counts": sorted(qtys),
                        "note": f"'{ename}' given counts {sorted(qtys)} — unify or explain the discrepancy"})
        out["forks"] = out["forks"][:5]
        if out["forks"]:
            out["status"] = "FLAG"
        return out
    except Exception:  # noqa: BLE001
        return {"status": "PASS", "forks": []}


# ── r16-S2: kinship term/label mismatch (NARASI_KINSHIP_SCAN) ────────────────────────────
# S2-Th-3 finding: Seo-an is correctly called "imo" (Korean: maternal aunt) twice, then the
# SAME chapter's clerk scene labels her "the applicant's paternal aunt" — a direct
# self-contradiction. Lexicon-level cross-check (Korean kinship term vs English maternal/
# paternal label), not a full family graph — much narrower FP surface.
def _kinship_scan_on() -> bool:
    return os.environ.get("NARASI_KINSHIP_SCAN", "0").strip().lower() in ("1", "true", "yes", "on")


_KINSHIP_KR_TERM_RX = re.compile(r"\b(imo|gomo|emo)\b", re.I)
_KINSHIP_EN_LABEL_RX = re.compile(r"\b(maternal|paternal)\s+(aunt|uncle)\b", re.I)
_KINSHIP_NAME_RX = re.compile(r"\b([A-Z][a-z]+(?:-[a-z]+)?)\b")
_KINSHIP_STOP_NAMES = {"The", "He", "She", "They", "It", "His", "Her", "Their", "But", "And",
                        "When", "Then", "That", "This", "Chapter", "Detective", "Doctor",
                        "Prosecutor", "Officer", "Mr", "Mrs", "Ms", "Why", "How", "What",
                        "Where", "Here", "There", "Now", "Because", "After", "Before",
                        # AUDIT FIX: common capitalized dialogue-starter words were being
                        # mistaken for names by _nearest_name ("Thank you, imo" -> "Thank"
                        # outranked the real speaker's name several dialogue-lines earlier).
                        "Thank", "You", "Your", "Yours", "I'm", "You're", "We're", "They're",
                        "Yes", "No", "Well", "So", "Oh", "Ah", "Please", "Sorry", "Come", "Go",
                        "Look", "Wait", "Stop", "Let", "We", "I", "Not", "Just", "Still",
                        "Even", "Only"}


def kinship_term_scan(text: str) -> dict:
    """Korean kinship term (imo/emo=maternal, gomo=paternal) used for a relation elsewhere
    labelled with the CONTRADICTING English side, for the SAME named character. Report-only.

    AUDIT FIX (r16): the original version tracked ONE global term_side for the whole
    manuscript and used "both terms present anywhere" as a full kill-switch — either bug could
    misfire: an unrelated character's correctly-labelled paternal side would read as
    "contradicting" a totally different character's imo, OR detection would be silently
    disabled entirely the moment a manuscript legitimately used both imo and gomo for two
    different people. Now scoped PER-CHARACTER via nearest-preceding-name attribution (the
    same mechanism scan_same_entity_age_fork/scan_tenure_ledger already use) — a mismatch only
    fires when the SAME named character is described with both a Korean term and a
    contradicting English label."""
    out = {"status": "PASS", "mismatches": []}
    if not text:
        return out
    try:
        def _nearest_name(pos: int) -> Optional[str]:
            # 400-char window (wider than the age/tenure scanners' 220) — a kinship term used
            # vocatively in dialogue ("Imo bought me sundaeguk") is often several dialogue
            # lines removed from the scene's last narrated name (real-manuscript case: ~350
            # chars from "Not at Seo-an." to "Imo bought me..." with only pronoun-only dialogue
            # lines in between).
            cand = None
            for nm in _KINSHIP_NAME_RX.finditer(text[max(0, pos - 400):pos]):
                if nm.group(1) not in _KINSHIP_STOP_NAMES:
                    cand = nm.group(1)
            return cand

        side_by_name: dict[str, str] = {}
        for m in _KINSHIP_KR_TERM_RX.finditer(text):
            side = "maternal" if m.group(1).lower() in ("imo", "emo") else "paternal"
            cand = _nearest_name(m.start())
            if cand:
                side_by_name.setdefault(cand, side)  # first-seen side is this character's established term
        for m in _KINSHIP_EN_LABEL_RX.finditer(text):
            side = m.group(1).lower()
            cand = _nearest_name(m.start())
            if not cand or cand not in side_by_name or side_by_name[cand] == side:
                continue
            lo = text.rfind("\n", 0, m.start()) + 1
            hi = text.find("\n", m.end())
            line = text[lo:hi if hi != -1 else len(text)]
            out["mismatches"].append({
                "note": (f"'{cand}' is described with the Korean term for "
                         f"'{side_by_name[cand]} aunt/uncle' elsewhere, but here labelled "
                         f"'{side} {m.group(2).lower()}' — contradiction"),
                "context": line.strip()[:160]})
            if len(out["mismatches"]) >= 4:
                break
        if out["mismatches"]:
            out["status"] = "FLAG"
        return out
    except Exception:  # noqa: BLE001
        return {"status": "PASS", "mismatches": []}


# ── r16-S1: character-location continuity heuristic (NARASI_LOCATION_CONTINUITY) ─────────
# S2-Th-3 finding: Do-yoon explicitly stays behind in Seoul in Ch1 ("I'll stay with the other
# thirty-six... I'll photograph every envelope"), Seo-an drives to Donghae alone — then Do-yoon
# is simply AT the Donghae shelter at the start of Ch2, zero travel/arrival beat. Deliberately
# LOW-CONFIDENCE by design (name-proximity + verb-presence, not real scene/blocking tracking):
# report-only, a miss is silent, a hit still needs human judgment.
def _location_continuity_on() -> bool:
    return os.environ.get("NARASI_LOCATION_CONTINUITY", "0").strip().lower() in ("1", "true", "yes", "on")


_STAY_BEHIND_RX = re.compile(
    r"\b([A-Z][a-z]+(?:-[a-z]+)?)\b[^.\n]{0,60}\b(?:would\s+|will\s+|'ll\s+)?sta(?:y|ying|yed)\b"
    r"[^.\n]{0,30}\b(?:behind|in|at|here)\b"
    r"|\b(?:stayed|remained)\s+behind\b[^.\n]{0,60}\b([A-Z][a-z]+(?:-[a-z]+)?)\b", re.I)
_TRAVEL_ARRIVAL_RX = re.compile(
    r"(?i)\b(?:arrived|drove|driving|took the|reached|made it to|walked in|entered|appeared|"
    r"boarded|caught the|showed up|got (?:to|there)|pulled up|stepped (?:off|out)|traveled|"
    r"followed (?:her|him|them)|caught up)\b")


def location_continuity_scan(text: str) -> list[dict]:
    """A character explicitly stated to stay/remain in ONE chapter, then named at the START of
    the NEXT chapter with no travel/arrival verb nearby — a possible unbridged location jump.
    Heuristic, report-only."""
    if not text:
        return []
    try:
        # AUDIT FIX (r16): same dead-code gap as coda_repeat_scan — the internal pipeline
        # format is "## Chapter N: Title", not bare "Chapter N".
        blocks = re.split(r"(?m)^(?=#{0,3}\s*Chapter \d+\s*[:.])", text)
        chapters = []
        for b in blocks:
            m = re.match(r"#{0,3}\s*Chapter (\d+)", b)
            if m:
                chapters.append((int(m.group(1)), b))
        chapters.sort(key=lambda c: c[0])
        out = []
        for i in range(len(chapters) - 1):
            _, body = chapters[i]
            next_no, next_body = chapters[i + 1]
            for m in _STAY_BEHIND_RX.finditer(body):
                name = m.group(1) or m.group(2)
                if not name:
                    continue
                head = next_body[:400]
                if (re.search(r"\b" + re.escape(name) + r"\b", head)
                        and not _TRAVEL_ARRIVAL_RX.search(head)):
                    out.append({"kind": "location_continuity_gap", "subject": name,
                        "note": (f"'{name}' stated to stay/remain behind, then appears at the "
                                 f"start of Chapter {next_no} with no travel/arrival beat nearby")})
                    break
            if len(out) >= 4:
                break
        return out
    except Exception:  # noqa: BLE001
        return []


def _ledger_en_num(n: int) -> str:
    if 0 <= n < 20:
        return _LEDGER_EN_ONES[n]
    if 20 <= n < 100:
        t, o = divmod(n, 10)
        return _LEDGER_EN_TENS[t] + ("-" + _LEDGER_EN_ONES[o] if o else "")
    return ""


def _ledger_term(x: Any) -> str:
    """Normalize a ledger entry: drop the curation parenthetical + leading ellipsis."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", str(x)).strip().lstrip("…").strip()


def _ledger_lane_patterns(style_key: Optional[str]) -> list:
    """(term, compiled_rx, min_count) triples from pakem/lane_ledger.json for this lane.
    Fail-open: missing file / unknown lane / malformed entry ⟹ entries skipped."""
    pats: list = []
    try:
        import json as _j
        _p = os.path.join(os.path.dirname(__file__), "pakem", "lane_ledger.json")
        with open(_p, encoding="utf-8") as f:
            lane = (_j.load(f) or {}).get((style_key or "").strip()) or {}
        if not isinstance(lane, dict):
            return pats
        for nm in lane.get("given_names") or []:
            t = _ledger_term(nm)
            if t:
                pats.append(("name:" + t, re.compile(r"\b" + re.escape(t) + r"\b"), 1))
        for sn in lane.get("overused_surnames") or []:
            t = _ledger_term(sn)
            if t:   # surname must precede a Capitalized given name — kills 'Ok,'/'Ha!' prose hits
                pats.append(("surname:" + t, re.compile(r"\b" + re.escape(t) + r"\s+[A-Z]"), 1))
        for ts in lane.get("timestamp_minutes") or []:
            m = re.search(r"(\d{1,2})?:(\d{2})", str(ts))
            if m:
                hh = re.escape(m.group(1)) if m.group(1) else r"\d{1,2}"
                pats.append(("timestamp:" + m.group(0),
                             re.compile(r"\b" + hh + ":" + re.escape(m.group(2)) + r"\b"), 1))
        for n in lane.get("small_numbers") or []:
            if isinstance(n, int):
                # scoped (?i:...) — a GLOBAL (?i) mid-alternation is a re.error on py3.11+
                alts = [r"\b" + str(n) + r"\b"]
                w = _ledger_en_num(n)
                if w:
                    alts.append(r"(?i:\b" + w + r"\b)")
                pats.append(("number:" + str(n), re.compile("|".join(alts)), 1))
            else:
                t = _ledger_term(n)
                if t:
                    pats.append(("number:" + t, re.compile(r"(?i)\b" + re.escape(t) + r"\b"), 1))
        for fd in lane.get("foods") or []:
            t = _ledger_term(fd)
            if t:
                pats.append(("food:" + t, re.compile(r"(?i)\b" + re.escape(t) + r"\b"), 1))
        for fl in lane.get("floors") or []:
            if isinstance(fl, int):
                w = _ledger_en_num(fl)
                pats.append(("floor:" + str(fl), re.compile(
                    r"(?i)\b(?:" + str(fl) + r"(?:st|nd|rd|th)?" + (("|" + w + r"(?:th)?") if w else "") +
                    r")[\s-]floor\b|\blantai\s+" + str(fl) + r"\b"), 1))
        for vp in lane.get("verbatim_phrases") or []:
            t = _ledger_term(vp)
            if len(t) >= 12:   # too-short phrases would false-positive everywhere
                pats.append(("phrase:" + t[:40],
                             re.compile(r"(?i)" + re.escape(t).replace(r"\ ", r"\s+")), 1))
        for pl in lane.get("places") or []:
            t = _ledger_term(pl)
            if t:
                pats.append(("place:" + t, re.compile(r"\b" + re.escape(t) + r"\b"), 1))
    except Exception:  # noqa: BLE001 — ledger is an enhancement; never block the scan
        return pats
    return pats


def ledger_hits_scan(manuscript: str, *, bible: str = "", style_key: Optional[str] = None) -> dict:
    """Deterministic lane-ledger + static-tics scan over the bible AND the manuscript.
    status FLAG/PASS/OFF (never OVER). Per-hit {term, where, count, snippet}. Never raises."""
    out: dict = {"status": "OFF", "hits": [], "bible_hits": 0}
    if not _ledger_validator_on():
        return out
    try:
        _lane_pats = _ledger_lane_patterns(style_key)
        # Static tics are kdrama-telemetry-derived: append them ONLY for lanes that
        # exist in the ledger (review finding — 'eleventh century' on a harari book
        # is legit prose, not a tic). Unknown lane ⟹ scan is a no-op.
        pats = _lane_pats + ([
            (t, re.compile(p) if isinstance(p, str) else p, mc)
            for t, p, mc in _LEDGER_STATIC_TICS] if _lane_pats else [])
        for where, txt in (("bible", bible or ""), ("manuscript", manuscript or "")):
            if not txt:
                continue
            _seen_spans: set = set()   # exact-span dedupe: 'eleven' matched by 3 patterns = 1 hit
            for term, rx, min_n in pats:
                ms = []
                for m in rx.finditer(txt):
                    ms.append(m)
                    if len(ms) >= 25:   # bound work + payload
                        break
                if len(ms) < max(1, int(min_n)):
                    continue
                for m in ms[:3]:   # ≤3 snippets per term per text
                    if (m.start(), m.end()) in _seen_spans:
                        continue
                    _seen_spans.add((m.start(), m.end()))
                    out["hits"].append({
                        "term": term, "where": where, "count": len(ms),
                        "snippet": re.sub(r"\s+", " ", txt[max(0, m.start() - 45):m.end() + 45])[:140]})
        out["bible_hits"] = sum(1 for h in out["hits"] if h["where"] == "bible")
        out["status"] = "FLAG" if out["hits"] else "PASS"
        return out
    except Exception:  # noqa: BLE001
        return {"status": "PASS", "hits": [], "bible_hits": 0}


def _names_from(m: re.Match) -> Optional[str]:
    gd = m.groupdict()
    return gd.get("name1") or gd.get("name2") or gd.get("name3")


def scan_manuscript(text: str, *, lang: str = "en", style_entry: Optional[dict] = None,
                    thesis: Optional[str] = None, word_target: Optional[int] = None,
                    embed_fn: Any = None, bible: Optional[str] = None) -> dict:
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

        # ── floating-aphorism budget (NARASI_ANCHOR_BUDGET, default OFF) ──
        # kdrama rolls eky9gcge/cpn9kjg7/ibtgb7zg: UNTAGGED gnomic standalone lines
        # doubled per roll (7→11→20) while `anchors` only sees [ANCHOR] tokens.
        # Deterministic gnomic net: standalone single-sentence paragraph, ≤18 words,
        # no dialogue quotes, no digits, no mid-sentence proper noun. Cap read RAW
        # (regloss_max pattern) — ABSOLUTE, not length-scaled; scarcity is the point.
        # DATA-SCOPED (review finding, lane-safety HIGH): armed ONLY for styles whose
        # style_spec.counters defines aphorisms_max (kdrama today) — no global env
        # fallback, so harari-class nonfiction thesis lines are never counted. The
        # env NARASI_APHORISMS_MAX only overrides the VALUE where the data key exists.
        if _anchor_budget_on() and "aphorisms_max" in budgets:
            _aph_cap = 8
            try:
                _aph_cap = int(os.environ.get("NARASI_APHORISMS_MAX", "") or budgets["aphorisms_max"])
            except Exception:  # noqa: BLE001
                _aph_cap = 8
            _aph_lines: list[str] = []
            for para in (text or "").split("\n\n"):
                p = para.strip()
                # >=4 words: one-line scene-transition beats ('Morning came.') are
                # pacing, not gnomics (review finding).
                if not p or "\n" in p or not (4 <= len(p.split()) <= 18):
                    continue
                if p.startswith(("#", ">", "- ", "* ", "|")):
                    continue
                if not re.search(r"[.!?…]\s*$", p):
                    continue
                if re.search(r"[\"“”‘’']", p) or re.search(r"\d", p):
                    continue
                # a capitalized word mid-sentence = named/scene line, not a gnomic
                if re.search(r"(?<=[a-zà-ÿ] )[A-ZÀ-Þ]", p):
                    continue
                if re.search(r"(?i)\[\s*anchor", p) or p[:200] in _aph_lines:
                    continue
                _aph_lines.append(p[:200])
            _aph_total = len(_aph_lines) + len(anchor_lines)
            report["counters"]["aphorisms"] = {
                "status": "OVER" if _aph_total > _aph_cap else "PASS",
                "count": _aph_total, "budget": _aph_cap,
                "tagged": len(anchor_lines),
                "sentences": _aph_lines[:12],
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

        # Structural-refrain scanners (report-only, FLAG-never-OVER → excluded from over_budget
        # at :2228; a repeated idea/hedge/closing is a generation-prompt concern, not a surgical
        # sentence-edit one). ONE flag gates all three (lens#3: "one density gate catches all
        # three refrains"). Default OFF → block skipped → byte-identical to today.
        if _refrain_scan_on():
            try:
                _tcap = int(budgets.get("thesis_cluster_max",
                                        int(os.environ.get("NARASI_THESIS_CLUSTER_MAX", "6"))))
            except Exception:  # noqa: BLE001
                _tcap = 6
            report["counters"]["thesis_cluster"] = _thesis_cluster_scan(chapters, embed_fn=embed_fn, cap=_tcap)
            report["counters"]["hedge_density"] = _hedge_density_scan(chapters)
            report["counters"]["closing_tableau"] = _closing_tableau_scan(chapters)
            # nonfiction prose-integrity (lens#2 hedge-as-shield batch), same flag, report-only
            report["counters"]["deferred_hook"] = _deferred_hook_scan(chapters)
            report["counters"]["unattributed_expert"] = _unattributed_expert_scan(text)
            if not _regloss_budget_on():
                report["counters"]["reglossing"] = _reglossing_scan(text)
            # Batch A (derived-number arithmetic/word-count + coming-of-age denial fingerprint),
            # same flag, report-only FLAG-never-OVER → excluded from over_budget, can't drive the diet loop.
            report["counters"]["derived_number"] = _derived_number_scan(chapters)
            report["counters"]["denial_fingerprint"] = _denial_fingerprint_scan(text)
            # Batch A.2 (Giza lens#2), same flag, report-only FLAG-never-OVER → excluded from over_budget.
            report["counters"]["bignum_unsourced"] = _bignum_unsourced_scan(chapters)
            report["counters"]["brief_leak"] = _brief_leak_scan(text)
            report["counters"]["mode_slip"] = _mode_slip_scan(text)
            report["counters"]["closer_thinning"] = _closer_thinning_scan(text)
            # Batch A.3 (fallen-angel lens#3): home-language bleed (ID word in a non-ID manuscript).
            report["counters"]["home_lang_bleed"] = _home_lang_bleed_scan(text, lang)
            # Homogenization tic-inventory (cross-roll telemetry; report-only FLAG-never-OVER).
            report["counters"]["homogenization_tics"] = _homogenization_tic_scan(text)

        # ── style-fingerprint saturation (NARASI_STYLE_SATURATION_SCAN, default OFF) —
        # manuscript-wide word/phrase + em-dash crutch-repetition (79x "the way", 301x
        # em-dash in one ~37K-word manuscript). OWN flag, independent of NARASI_REFRAIN_SCAN.
        # status FLAG-never-OVER → excluded from over_budget below, can never drive the diet
        # loop (see _style_saturation_scan docstring for why). OFF ⟹ key absent ⟹ byte-identical.
        if _style_saturation_scan_on():
            report["counters"]["style_saturation"] = _style_saturation_scan(text)

        # ── lane-ledger validator (NARASI_LEDGER_VALIDATOR, default OFF) — report-only.
        # FLAG-never-OVER → excluded from over_budget below, can never drive the diet
        # loop. OWN flag (independent of NARASI_REFRAIN_SCAN) so the ledger gate is
        # enableable alone. Scans the manuscript AND the pinned bible (bible kwarg —
        # a bible-level hit poisons every chapter: the eleven-month-drought class)
        # against pakem/lane_ledger.json for style_entry['key'] plus the static
        # cross-roll tics table. OFF ⟹ key absent ⟹ report byte-identical.
        if _ledger_validator_on():
            report["counters"]["ledger_hits"] = ledger_hits_scan(
                text, bible=bible or "", style_key=(style_entry or {}).get("key"))

        # ── real-brand scan (NARASI_BRAND_SCAN, default OFF) — roll-8 Doosan class.
        if _brand_scan_on():
            report["counters"]["real_brands"] = real_brand_scan(text, bible=bible or "")

        # ── abort-seam scan (NARASI_ABORT_SEAM_SCAN, default OFF) — round-6, report-only.
        if _abort_seam_on():
            report["counters"]["abort_seam"] = abort_seam_scan(text)

        # -- meta-reference scan (NARASI_METALEAK_SCAN, default OFF) -- round-13.
        if _meta_ref_on():
            report["counters"]["meta_reference"] = meta_reference_scan(text)

        # -- provenance-citation leak (NARASI_PROVENANCE_LEAK, default OFF) -- round-15.
        if _provenance_leak_on():
            report["counters"]["provenance_leak"] = provenance_leak_scan(text)

        # ── chapter-coda verbatim repeat (NARASI_CODA_DEDUP_SCAN, default OFF) — round-6.
        if _coda_dedup_on():
            report["counters"]["coda_repeat"] = coda_repeat_scan(text)

        # ── near-variant name net (NARASI_LEDGER_FUZZY, default OFF).
        if _ledger_fuzzy_on():
            report["counters"]["ledger_fuzzy"] = ledger_fuzzy_names(
                text, style_key=(style_entry or {}).get("key"))

        # ── timeline arithmetic (NARASI_TIMELINE_ARITH, default OFF) — report-only.
        # 5/5 rain rolls drifted on DERIVED spans while absolute dates held ("Three
        # months after the flood" beside 13 Aug → 17 Oct; "fourteen years" ×7
        # alternating "fifteen years" ×3). Prompts cannot do arithmetic; the
        # deterministic pass in narasi_arithmetic can. EN-only v1 (the interval/
        # alternation tables are en); other langs ⟹ key absent. FLAG-never-OVER —
        # can never drive the diet loop.
        if _timeline_arith_on() and (lang or "en").split("-")[0].lower() == "en":
            try:
                import narasi_arithmetic as _na
                _tl = _na.scan_arithmetic(text, lang="en", v2=_timeline_v2_on())
                report["counters"]["timeline_arith"] = {
                    "status": "FLAG" if _tl.get("findings") else "PASS",
                    "count": len(_tl.get("findings") or []),
                    "findings": (_tl.get("findings") or [])[:8],
                }
            except Exception:  # noqa: BLE001 — linter is an enhancement, never blocks the scan
                pass

        # -- numeric magnitude + year fork (NARASI_NUMERIC_MAGNITUDE, default OFF) -- round-15, report-only.
        if _numeric_magnitude_on():
            report["counters"]["numeric_magnitude"] = numeric_magnitude_scan(text)

        # -- age/duration fork ledger (NARASI_AGE_LEDGER, default OFF) -- round-15, EN v1, report-only.
        if _age_ledger_on() and (lang or "en").split("-")[0].lower() == "en":
            try:
                import narasi_arithmetic as _na
                report["counters"]["age_ledger"] = _na.scan_age_ledger(text)
            except Exception:  # noqa: BLE001
                pass

        # -- alias↔legal-name lock (NARASI_ALIAS_LEDGER, default OFF) -- round-15, report-only.
        if _alias_ledger_on():
            report["counters"]["alias_ledger"] = alias_legal_lock_scan(text)

        # -- canon-anchor absolute-date fork (NARASI_CANON_ANCHOR, default OFF) -- round-16,
        # EN v1 (reuses narasi_arithmetic's date extraction), report-only.
        if _canon_anchor_on() and (lang or "en").split("-")[0].lower() == "en":
            try:
                import narasi_arithmetic as _na2
                _cf = _na2.scan_canon_anchor_dates(text)
                report["counters"]["canon_anchor"] = {
                    "status": "FLAG" if _cf else "PASS", "count": len(_cf), "findings": _cf}
            except Exception:  # noqa: BLE001
                pass

        # -- unsubstituted template-placeholder scan (NARASI_PLACEHOLDER_SCAN, default OFF) --
        # round-16, report-only.
        if _placeholder_scan_on():
            report["counters"]["placeholder"] = placeholder_scan(text)

        # -- entity-quantity consistency (NARASI_ENTITY_QTY, default OFF) -- round-16, report-only.
        if _entity_qty_on():
            report["counters"]["entity_qty"] = entity_quantity_scan(text)

        # -- kinship term/label mismatch (NARASI_KINSHIP_SCAN, default OFF) -- round-16, report-only.
        if _kinship_scan_on():
            report["counters"]["kinship"] = kinship_term_scan(text)

        # -- character-location continuity heuristic (NARASI_LOCATION_CONTINUITY, default OFF)
        # -- round-16, LOW-confidence by design, report-only (never injected into revise).
        if _location_continuity_on():
            _lc = location_continuity_scan(text)
            report["counters"]["location_continuity"] = {
                "status": "FLAG" if _lc else "PASS", "count": len(_lc), "findings": _lc}

        # ── reglossing budget promotion (NARASI_REGLOSS_BUDGET, default OFF) ──
        # FLAG→BUDGETED: cap read RAW from style_spec.counters.regloss_max (env
        # NARASI_REGLOSS_MAX fallback, default 2 repeats => a term may open an em-dash
        # gloss at most THREE times per book). Deliberately NOT _scaled — a gloss does
        # not earn headroom with book length (thesis read-cap-raw insight). Runs
        # regardless of NARASI_REFRAIN_SCAN so the budget doesn't depend on a second
        # flag. OVER enters over_budget, but a rewrite only happens when
        # NARASI_DIET_MAX_LOOPS>0 (prod currently 0 => report-only in practice).
        # Style cap is read FIRST in its own try so a malformed env value can never
        # clobber an explicit per-style regloss_max (review finding).
        if _regloss_budget_on():
            _rg_cap = 2
            try:
                if "regloss_max" in budgets:
                    _rg_cap = int(budgets["regloss_max"])
                else:
                    _rg_cap = int(os.environ.get("NARASI_REGLOSS_MAX", "2"))
            except Exception:  # noqa: BLE001
                _rg_cap = 2
            report["counters"]["reglossing"] = _reglossing_scan(text, repeat_cap=_rg_cap)

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
    if c.get("aphorisms", {}).get("status") == "OVER":
        aph = c["aphorisms"]
        _tagged = int(aph.get("tagged") or 0)
        parts.append(
            f"FLOATING APHORISMS: {aph['count']} standalone aphoristic/gnomic lines "
            f"(tagged + untagged) exceed the budget of {aph['budget']}. The budget counts "
            f"[ANCHOR]-tagged lines FIRST ({_tagged} already tagged); of the UNTAGGED lines "
            f"listed below keep at most {max(0, int(aph['budget']) - _tagged)}, each as a "
            "chapter-FINAL line; DELETE the rest entirely — do not paraphrase them into "
            "narration and do not fold them into an adjacent paragraph. A cut aphorism "
            "leaves no residue. Lines:\n- "
            + "\n- ".join(aph.get("sentences", [])[:12]))
    if c.get("reglossing", {}).get("status") == "OVER":
        rg = c["reglossing"]
        _terms = "; ".join(f"{t} ×{n}" for t, n in (rg.get("terms") or {}).items())
        parts.append(
            f"RE-GLOSSED TERMS: these head-terms are re-defined with an em-dash gloss more than "
            f"{int(rg.get('budget', 1)) + 1} times across the book ({_terms}). Keep ONLY the FIRST "
            "em-dash gloss of each term; at every later occurrence use the bare term and delete "
            "just the ' — definition' segment, keeping the rest of the sentence intact.")
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
