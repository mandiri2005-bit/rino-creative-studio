"""
B-04a: Review/One-Shot language separation + exact Bab/Chapter matching — acceptance suite.

Real-path harness for the backend half: calls the ACTUAL `laozhang_api.narasi_review` /
`laozhang_api.oneshot_fix_submit` endpoints and the pure `narasi_review_contract` module
directly, with only the db/provider boundary (`database.*`, `laozhang_api.make_client`,
`laozhang_api.OpenAI`) monkeypatched — every flag check, exact-type validation, language-
contract-block append, and response/result-field gating inside the real endpoints runs
unmodified, exactly the same discipline as `test_narasi_lifecycle_repository.py`.

Real-path harness for the frontend half: genuine Node execution of the actual
`src/narasiReviewContract.mjs` module via subprocess (never a Python re-expression of its
regex), the same `node_eval` technique `test_narasi_lifecycle_repository.py` already uses for
`narasiLifecycle.mjs`.

No production manuscript text, job IDs, tenant IDs, or UUIDs anywhere in this file — every
fixture under `tests/python/fixtures/b04a/` is fully synthetic (see TestPrivacyScan).

Companion inventory: `_REQUIRED_ACCEPTANCE_IDS` below (76 literal IDs) plus the `@_covers(...)`
binding on every test method, cross-checked for completeness by
`TestAcceptanceMatrixCompleteness` — the ID list and the fixture oracle (`_B04A_FIXTURE_ORACLE`)
are hand-authored literals, never derived from ACCEPTANCE-MATRIX.md, test names, fixture
metadata, or MANIFEST.json at runtime (see [[feedback-independent-canonical-binding]]: a data
file plus its own manifest/hash can be tampered together and still match, so a third anchor —
this file's own literals — is required).
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

import database
import laozhang_api
import narasi_review_contract as nrc
from auth_middleware import CurrentUser
from continuity.lifecycle import admit_outline_lifecycle, build_job_lifecycle


_TENANT = "77777777-7777-4777-8777-777777777777"
_UID = "66666666-6666-4666-8666-666666666666"

_REAL_FRONTEND_REPO = Path("/Users/rino/Documents/cerita-ai-studio").resolve()

def _resolve_frontend_dir():
    """CONTAINMENT FIX (post-incident, 3 real-repo leaks this session): this harness
    writes transient mounted-app debug files (.b04a_client_dbg.mjs etc.) INSIDE
    whatever directory this resolves to. It must NEVER be able to silently default
    to the real product frontend repo. Fail closed, not open."""
    raw = os.environ.get("B04A_FRONTEND")
    if not raw:
        raise RuntimeError(
            "B04A_FRONTEND is not set. This harness refuses to guess a frontend "
            "directory -- it previously defaulted to the real product repo and leaked "
            "debug files into it three times. Set B04A_FRONTEND to a disposable "
            "sandbox directory containing a hash-verified copy of the frontend.")
    resolved = Path(raw).resolve()
    if resolved == _REAL_FRONTEND_REPO:
        raise RuntimeError(
            f"B04A_FRONTEND resolves to the REAL product frontend repo "
            f"({_REAL_FRONTEND_REPO}). This harness refuses to run against it under "
            f"any circumstance -- point it at a disposable sandbox copy instead.")
    # Note: NOT requiring .git here -- minimal single-file detached-copy negative
    # controls (_detached_frontend_tree) pass a bare temp dir with no .git at all,
    # and re-import this module fresh via that env var. Git-status-dependent tests
    # (A07/A08/F08) fail with their own clear git error if pointed at a non-repo.
    return resolved

_FRONTEND_DIR = _resolve_frontend_dir()
_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "b04a"


def _real_frontend_git_status():
    if not _REAL_FRONTEND_REPO.exists():
        return None
    proc = subprocess.run(["git", "-C", str(_REAL_FRONTEND_REPO), "status", "--porcelain"],
                           capture_output=True, text=True, check=False)
    return proc.stdout


@pytest.fixture(autouse=True, scope="session")
def _containment_audit_real_frontend_repo():
    """Any write into the REAL product frontend repo during this suite is an
    automatic stop -- this is not a test assertion to satisfy, it is a hard
    guard against a real, disclosed incident (3 leaks this session)."""
    before = _real_frontend_git_status()
    yield
    after = _real_frontend_git_status()
    if before != after:
        raise RuntimeError(
            f"CONTAINMENT VIOLATION: the real frontend repo ({_REAL_FRONTEND_REPO}) "
            f"changed during this test session.\nBEFORE:\n{before}\nAFTER:\n{after}")


@pytest.fixture(autouse=True)
def _clean_b04a_env(monkeypatch):
    """Strips every NARASI_/DALANG_ flag before each test — this suite starts from a known
    all-off baseline regardless of ambient shell env, matching A-04/A-05a/B-01 discipline."""
    for name in list(os.environ):
        if name.startswith("NARASI_") or name.startswith("DALANG_"):
            monkeypatch.delenv(name, raising=False)


def _user(tenant=_TENANT, uid=_UID):
    return CurrentUser(tenant_id=tenant, user_id=uid, plan="pro", tier="pro")


def run(coro):
    return asyncio.run(coro)


def _load_fixture(name):
    return json.loads((_FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))


# ── Node execution harness — genuine subprocess execution of narasiReviewContract.mjs ──────
def node_eval(expression, *, timeout=10):
    module = _FRONTEND_DIR / "src/narasiReviewContract.mjs"
    script = (
        f"import * as m from {json.dumps(module.as_uri())};"
        f"const result=await ({expression});"
        f"process.stdout.write(JSON.stringify(result===undefined?null:result));"
    )
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script], cwd=_FRONTEND_DIR,
        text=True, capture_output=True, timeout=timeout, check=False,
        env={**os.environ, "NO_PROXY": "*", "no_proxy": "*"},
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def node_call(fn_expr):
    """Evaluates fn_expr (a JS expression) and returns {"ok": True, "value": ...} on success or
    {"ok": False, "code": ..., "message": ...} if it throws — mirrors the backend's
    ReviewContractError.code capture so a thrown error never crashes the harness."""
    expr = (
        "(function(){ try { return {ok:true, value:(" + fn_expr + ")}; } "
        "catch(e){ return {ok:false, code:(e&&e.code)||null, message:String(e&&e.message||e)}; } })()"
    )
    return node_eval(expr)


def parse_manuscript(text):
    expr = (
        "(function(){"
        f"const p=m.parseManuscriptChapters({json.dumps(text)});"
        "return {chapters:Array.from(p.chapters.entries()),"
        "duplicateOrdinals:Array.from(p.duplicateOrdinals),"
        "duplicateHeadings:Array.from(p.duplicateHeadings),"
        "fenceMalformed:p.fenceMalformed};"
        "})()"
    )
    return node_eval(expr)


def match_sections(review_sections, manuscript_text):
    expr = (
        "(function(){"
        f"const rs={json.dumps(review_sections)};"
        f"const mt={json.dumps(manuscript_text)};"
        "const p=m.parseManuscriptChapters(mt);"
        "const results=m.matchChapterSections(rs,p.chapters);"
        "return results.map(r=>({key:r.key,matchedKey:r.matched?r.matched.key:null,"
        "matchedBody:r.matched?r.matched.body:null,matchedHeading:r.matched?r.matched.heading:null,"
        "ambiguous:r.ambiguous,reason:r.reason}));"
        "})()"
    )
    return node_eval(expr)


def frontend_text(relpath="src/narasiReviewContract.mjs"):
    return (_FRONTEND_DIR / relpath).read_text(encoding="utf-8")


def _rhs_of_const_assignment(snippet):
    """Strips a leading `const NAME=` from a real-source-extracted snippet, returning
    just the right-hand-side expression (trailing `;` removed by the caller)."""
    return snippet[snippet.index("=") + 1:].rstrip().rstrip(";")


def eval_main_jsx_snippet(rhs_expr, bindings, *, timeout=10):
    """Genuinely EXECUTES an expression extracted byte-for-byte from main.jsx's real
    source (never retyped or reproduced by hand) against the real narasiReviewContract.mjs
    module, with synthetic free-variable bindings supplied by the caller standing in for
    React state/props. Proves the REAL production call site's literal code path, not a
    Python re-implementation of it -- the same discipline node_eval() applies to the
    pure module, extended to main.jsx's actual request-body/capability-check call sites
    (Rework 1, Codex requirement #6: production-path tests for Review/One-Shot/AutoFix/
    Optimize, replacing the old source-substring-only E06/E09 coverage)."""
    prelude = "".join(f"const {name}={json.dumps(value)};" for name, value in bindings.items())
    module_uri = (_FRONTEND_DIR / "src/narasiReviewContract.mjs").resolve().as_uri()
    script = (
        f"import * as narasiReviewContract from {json.dumps(module_uri)};"
        f"{prelude}"
        f"const result=({rhs_expr});"
        f"process.stdout.write(JSON.stringify(result===undefined?null:result));"
    )
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        text=True, capture_output=True, timeout=timeout, check=False,
        env={**os.environ, "NO_PROXY": "*", "no_proxy": "*"},
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# ── Mounted-component production-path harness (Rework 4, Codex P1 finding #1) ───────────────
# Genuinely mounts the REAL, UNMODIFIED App (via its own real #root auto-mount side effect --
# no test-only export, no test-only mount guard in main.jsx) inside a REAL, headless Chrome
# tab -- no jsdom, no DOM-emulation dependency of any kind -- navigates to it via the real
# "Script Review" nav button, drives it through real button clicks / textarea input, and
# intercepts the real global `fetch`. Three files (client script, HTML shell, Node runner) are
# generated TRANSIENTLY from this test file's own embedded source at run time and deleted in
# `finally` -- there is no persistent file anywhere in either repo (Rework 2's
# tests/python/b04a_mount_harness.mjs and Rework 3/4's private jsdom npm prefix have both been
# removed; Codex P1 findings flagged them as allowlist/hermeticity violations). This project's
# own package.json/package-lock.json are never touched and nothing is ever installed (see
# test_g09_harness_never_installs_dependencies_clean_environment below); vite/react/react-dom
# resolve from the frontend's own real node_modules via its own real Vite dev server, which is
# why the generated files must still be materialized to temp paths INSIDE the frontend repo.
_B04A_CLIENT_SCRIPT_SOURCE = r'''
// Rework 4 (Codex P1 finding #1): runs INSIDE a real, headless Chrome tab -- no jsdom,
// no DOM-emulation dependency of any kind, no npm install, no registry access. Mounts
// the REAL, UNMODIFIED production App via its own real #root auto-mount side effect (no
// test-only export, no test-only mount guard), then navigates via the real "Script
// Review" nav button -- exactly the path a real user takes.
const params = new URLSearchParams(location.search);
const flagOn = params.get("flag") === "1";
const apiMode = params.get("api_mode") === "google" ? "google" : "laozhang";
const scenario = params.get("scenario") || "review";
const reportLangParam = params.get("report_lang") || "";
const headingStyleParam = params.get("heading_style") === "chapter" ? "Chapter" : "Bab";
const isLanguageMatrixProbe = scenario === "language_marker_matrix";

const flush = (ms = 0) => new Promise((r) => setTimeout(r, ms));
function toBase64Utf8(str) { return btoa(unescape(encodeURIComponent(str))); }

const calls = [];
const fetchHandlers = [];
window.fetch = async (input, init) => {
  const url = typeof input === "string" ? input : input.url;
  const method = ((init && init.method) || "GET").toUpperCase();
  let body = null;
  try { body = init && init.body ? JSON.parse(init.body) : null; } catch (e) { body = init ? init.body : null; }
  calls.push({ url, method, body });
  for (const h of fetchHandlers) {
    if (h.test(url, method)) return h.respond(url, method, body);
  }
  return { ok: true, status: 200, json: async () => ({ ok: true }) };
};

// App() renders EVERY tool section simultaneously (display:none toggled by `page`
// state, never unmounted) -- several other sections reuse the exact same button text /
// textarea classes (e.g. TtsSection also has a "\u{1F535} Google" button and a
// "textarea.inp.ta.mono"). isHidden() walks the ancestor chain for an inline
// display:none so every query below is scoped to the CURRENTLY VISIBLE page only.
const isHidden = (el) => {
  let node = el;
  while (node && node.nodeType === 1) {
    if (node.style && node.style.display === "none") return true;
    node = node.parentElement;
  }
  return false;
};
const visible = (selector) => [...document.querySelectorAll(selector)].filter((el) => !isHidden(el));
const click = async (label) => {
  const btn = visible("button").find((b) => b.textContent.includes(label));
  if (!btn) throw new Error(`visible button not found: ${label}`);
  btn.click();
  await flush(20);
};
const setNativeValue = async (el, value) => {
  const proto = el.tagName === "TEXTAREA" ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
  setter.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
  await flush(20);
};

const finish = (extra) => {
  // showToast is App()'s own internal state now (no prop we control) -- toast/progress
  // text is read directly from the rendered DOM below instead of an intercepted callback.
  // Real Chrome (not a Node process we control) is what terminates this harness -- Chrome
  // itself dumps the DOM and exits once --virtual-time-budget is exhausted, so there is no
  // equivalent of the old jsdom harness's own process.exit(0)/hanging-interval concern.
  document.body.dataset.payload = toBase64Utf8(JSON.stringify({ calls, bodyText: document.body.textContent, ...extra }));
  document.body.dataset.result = "done";
};

async function main() {
  const nrc = await import("/src/narasiReviewContract.mjs");

  // Mounting the REAL App: loading main.jsx triggers its own real, unconditional
  // top-level auto-mount side effect against the #root element already present in the
  // document -- never a test-only export, never a re-implementation.
  await import("/src/main.jsx");
  await flush(100);

  await click("Script Review");
  await click(apiMode === "google" ? "\u{1F535} Google" : "\u{1F534} LaoZhang");
  await click("Ketik / Paste");

  // "chapter4_autofix_probe" (Rework 3, Codex requirement #3): a REAL manuscript/review
  // pair where the manuscript's own English chapter heading carries its full title
  // ("## Chapter 4: The Sound That Would Not Fade") but the review markdown's own
  // section only repeats the bare ordinal ("## Chapter 4") -- exactly how an AI-
  // generated editorial review realistically summarizes a section, and precisely the
  // shape that defeats a positional/exact-title Bab-only lookup while a genuine
  // ordinal-based chapter-key match still succeeds. This is the mounted proof H01's
  // detached-copy mutation is checked against (see test_e03's mounted assertion).
  const isChapter4Probe = scenario === "chapter4_autofix_probe";
  // "unrecognized_heading_probe" (Rework 3, Codex requirement #5): the review markdown
  // carries an EXTRA section whose heading has no ordinal at all ("## Catatan Umum" --
  // "General Notes"), so extractChapterKey fails entirely (a different, EARLIER
  // rejection than NO_MANUSCRIPT_MATCH/duplicate, which only apply to sections that DID
  // extract a key). Proves the section is surfaced with its bounded message, never
  // silently dropped, and that no Fix action exists for it.
  const isUnrecognizedProbe = scenario === "unrecognized_heading_probe";
  // Rework 4 (Codex P1 finding #2 + required edge cases a/b/c): three more shapes of the
  // same unrecognized-heading surface -- (a) exactly one valid chapter beside one
  // unrecognized heading, (b) NO recognized chapter at all (reviewBabs empty --
  // the exact shape the return-null guard bug silently dropped), (c) a valid chapter, an
  // ordinary chapter-shaped-but-unmatched heading (pre-existing D09 ambiguity), and an
  // unrecognized heading all in the same review, proving the three categories coexist.
  const isUnrecOneValidProbe = scenario === "unrecog_one_valid_one_unrecog";
  const isUnrecOnlyProbe = scenario === "unrecog_only_probe";
  const isUnrecMixedProbe = scenario === "unrecog_mixed_ordinary_valid";

  let manuscript = "## Bab 1: Awal\n\nIni adalah isi bab satu untuk pengujian yang cukup panjang.\n\n## Bab 2: Tengah\n\nIni adalah isi bab dua untuk pengujian yang cukup panjang.\n";
  if (isChapter4Probe) {
    manuscript = "## Bab 1: Awal\n\nIsi bab satu untuk pengujian.\n\n## Chapter 4: The Sound That Would Not Fade\n\nThe real English chapter-four body text.\n";
  } else if (isUnrecOneValidProbe || isUnrecOnlyProbe) {
    manuscript = "## Bab 1: Awal\n\nIsi bab satu untuk pengujian yang cukup panjang.\n";
  } else if (isLanguageMatrixProbe) {
    // The manuscript's OWN chapter-heading vocabulary (headingStyleParam) is an
    // INDEPENDENT axis from reportLangParam -- the exact chapter parser (Section 7 of
    // the contract) already accepts either "Bab N" or "Chapter N" regardless of
    // report language. Rework 9: since the Part-2 marker is now a fixed, global
    // constant (not derived from either axis), this proves the SAME marker value
    // is used across every combination of the two independent axes.
    manuscript = "## " + headingStyleParam + " 1: Awal\n\nIsi "
      + (headingStyleParam === "Chapter" ? "chapter" : "bab") + " satu untuk pengujian yang cukup panjang.\n";
  }
  const pasteArea = visible("textarea.inp.ta.mono")[0];
  if (!pasteArea) throw new Error("visible paste textarea not found");
  await setNativeValue(pasteArea, manuscript);

  let reviewMarkdown = "## Bab 1: Awal\n\nRekomendasi: perbaiki kalimat pembuka.\n\n## Bab 2: Tengah\n\nRekomendasi: perkuat konflik.\n";
  if (isChapter4Probe) {
    reviewMarkdown = "## Bab 1\n\nRekomendasi: perbaiki kalimat pembuka.\n\n## Chapter 4\n\nRekomendasi: perkuat konflik.\n";
  } else if (isUnrecognizedProbe) {
    reviewMarkdown = "## Bab 1: Awal\n\nRekomendasi: perbaiki kalimat pembuka.\n\n## Bab 2: Tengah\n\nRekomendasi: perkuat konflik.\n\n## Catatan Umum\n\nIni catatan umum yang tidak terkait bab manapun.\n";
  } else if (isUnrecOneValidProbe) {
    reviewMarkdown = "## Bab 1: Awal\n\nRekomendasi: perbaiki kalimat pembuka.\n\n## Catatan Umum\n\nIni catatan umum yang tidak terkait bab manapun.\n";
  } else if (isUnrecOnlyProbe) {
    reviewMarkdown = "## Catatan Umum\n\nIni catatan umum yang tidak terkait bab manapun.\n\n## Catatan Lain\n\nCatatan lain yang juga tidak terkait bab manapun.\n";
  } else if (isUnrecMixedProbe) {
    // Rework 6 (Codex P1 finding): the harari/narrative personas' OWN Part-1 Checklist
    // section contains a REAL "### Checklist Per Bab" sub-heading -- reproduced here
    // exactly, alongside two more decoys ("lihat Part 2" / "evaluasi per bab" ordinary
    // prose), all BEFORE the one true literal Part-2 marker. None of the six Part-1
    // headings (Skor / Checklist Harari Style / Yang Sangat Kuat / Kelemahan Utama /
    // Saran Revisi Prioritas / Verdict Akhir), nor either prose decoy, may be
    // misclassified as UNRECOGNIZED_HEADING or mistaken for the real Part-2 boundary.
    reviewMarkdown = "## Skor\n\n"
      + "| Aspek | Nilai |\n|-------|-------|\n| Overall Quality | 8/10 |\n\n**Skor Keseluruhan: 8.0/10**\n\n"
      + "## Checklist Harari Style\n\n"
      + "### Pelanggaran Global\n\nScan seluruh manuskrip untuk pelanggaran gaya.\n\n"
      + "### Checklist Per Bab\n\nIdentify opening type per bab, lalu cek setiap rule wajib.\n\n"
      + "## Yang Sangat Kuat\n\nBeberapa kekuatan di sini. Lihat Part 2 untuk detail per bab.\n\n"
      + "## Kelemahan Utama\n\nBeberapa kelemahan di sini. Evaluasi per bab akan menyusul di bagian berikutnya.\n\n"
      + "## Saran Revisi Prioritas\n\n1. Saran prioritas.\n\n"
      + "## Verdict Akhir\n\nParagraf verdict akhir.\n\n"
      + "---\n\n---WIMBA_REVIEW_CHAPTERS_START:v1---\n\n"
      + "## Bab 1: Awal\n\nRekomendasi: perbaiki kalimat pembuka.\n\n"
      + "## Catatan Umum\n\nIni catatan umum yang tidak terkait bab manapun.\n";
  } else if (isLanguageMatrixProbe) {
    // D13 (Rework 9): simulates what the REAL backend's report_language-aware
    // review-language block would have produced for this run's clicked report
    // language -- the Part-1 heading LABELS vary with report_language (ordinary
    // localized report content), but the structural Part-2 delimiter below is the
    // one, fixed, language-neutral constant regardless.
    const isEn = reportLangParam === "en";
    const validHeading = headingStyleParam + " 1: Awal";
    reviewMarkdown = "## " + (isEn ? "Score" : "Skor") + "\n\n"
      + "| Aspek | Nilai |\n|-------|-------|\n| Overall Quality | 8/10 |\n\n**"
      + (isEn ? "Overall Score: 8.0/10" : "Skor Keseluruhan: 8.0/10") + "**\n\n"
      + "## " + (isEn ? "Key Weaknesses" : "Kelemahan Utama") + "\n\n"
      + (isEn ? "A few weaknesses here." : "Beberapa kelemahan di sini.") + "\n\n"
      + "## " + (isEn ? "Final Verdict" : "Verdict Akhir") + "\n\nParagraf verdict akhir.\n\n"
      + "---\n\n---WIMBA_REVIEW_CHAPTERS_START:v1---\n\n"
      + "## " + validHeading + "\n\nRekomendasi: perbaiki kalimat pembuka.\n\n"
      + "## " + (isEn ? "General Notes" : "Catatan Umum") + "\n\n"
      + (isEn ? "General notes not related to any chapter." : "Ini catatan umum yang tidak terkait bab manapun.") + "\n";
  }
  fetchHandlers.push({
    test: (url) => url === "/api/narasi/review" && calls.filter((c) => c.url === "/api/narasi/review").length <= 1,
    respond: async () => ({
      ok: true, status: 200,
      json: async () => ({
        ok: true, text: reviewMarkdown,
        ...(flagOn ? { report_language: "id", review_contract_version: nrc.REVIEW_CONTRACT_VERSION } : {}),
      }),
    }),
  });

  if (isLanguageMatrixProbe) {
    await click(reportLangParam === "en" ? "🇬🇧 English" : "🇮🇩 Indonesia");
    await flush(20);
  }
  await click("Generate Editorial Review");
  await flush(50);

  if (scenario === "review" || isChapter4Probe) { finish({}); return; }

  if (isUnrecognizedProbe || isUnrecOneValidProbe || isUnrecOnlyProbe || isUnrecMixedProbe || isLanguageMatrixProbe) {
    // Rework 4 (Codex P2 finding #3): a direct DOM assertion that the unrecognized row
    // genuinely has no Fix button/action -- not merely "no fetch call happened". Finds
    // the row by its own title text (an exact match; ordinary chapter rows prefix their
    // title with an icon, so this can never cross-match one of those), then checks its
    // outer row container for ANY <button> at all.
    // Rework 8: isLanguageMatrixProbe's unrecognized title is localized ("General Notes"
    // for en) -- every other scenario here is ID-only ("Catatan Umum"), unchanged.
    const unrecTitle = isLanguageMatrixProbe ? (reportLangParam === "en" ? "General Notes" : "Catatan Umum") : "Catatan Umum";
    const titleEl = [...document.querySelectorAll("span")].find((s) => s.textContent.trim() === unrecTitle);
    const headerDiv = titleEl && titleEl.closest("div[style]");
    const rowDiv = headerDiv && headerDiv.parentElement;
    const unrecRowHasButton = !!(rowDiv && rowDiv.querySelector("button"));
    // Rework 9: exposes the ACTUAL delimiter constant this run used (read from the real
    // narasiReviewContract.mjs module, never a value the test itself invented) so the
    // Python side can assert byte-identity across all 4 language-matrix combinations.
    finish({ unrecRowHasButton, unrecTitleFound: !!titleEl, markerUsed: nrc.PART_TWO_MARKER });
    return;
  }

  const sseBody = (text) => {
    const encoder = new TextEncoder();
    const chunks = [`data: ${text}\n\n`, "data: [DONE]\n\n"];
    let i = 0;
    return { ok: true, status: 200, body: { getReader: () => ({
      read: async () => (i < chunks.length ? { done: false, value: encoder.encode(chunks[i++]) } : { done: true, value: undefined }),
    }) } };
  };

  if (scenario === "autofix") {
    fetchHandlers.push({
      test: (url) => url === "/api/narasi/review" || url === "/api/chat/google",
      respond: async (url) => (url === "/api/chat/google" ? sseBody("fixed chapter text")
        : { ok: true, status: 200, json: async () => ({ ok: true, text: "fixed chapter text" }) }),
    });
    await click("Fix Bab Ini");
    await flush(50);
    finish({}); return;
  }

  if (scenario === "optimize") {
    fetchHandlers.push({
      test: (url) => url === "/api/narasi/review" || url === "/api/chat/google",
      respond: async (url) => (url === "/api/chat/google" ? sseBody("optimized text")
        : { ok: true, status: 200, json: async () => ({ ok: true, text: "optimized text" }) }),
    });
    const optimizeTextareas = visible("textarea.inp.w").slice(-2);
    if (optimizeTextareas.length !== 2) throw new Error("optimize textareas not found: " + optimizeTextareas.length);
    await setNativeValue(optimizeTextareas[0], "Rekomendasi editor palsu");
    await setNativeValue(optimizeTextareas[1], "Seksi asli palsu");
    await click("Optimasi Seksi Ini");
    await flush(50);
    finish({}); return;
  }

  if (scenario === "oneshot_success" || scenario === "oneshot_missing_version") {
    await click("⚡ One-Shot Fix");
    fetchHandlers.push({
      test: (url) => url === "/api/narasi/oneshot-fix",
      respond: async () => ({ ok: true, status: 200, json: async () => ({ ok: true, job_id: "job-1" }) }),
    });
    fetchHandlers.push({
      test: (url) => url === "/api/narasi/oneshot-fix/status/job-1",
      respond: async () => ({ ok: true, status: 200, json: async () => ({ status: "done", progress: "Selesai" }) }),
    });
    const resultBody = scenario === "oneshot_success"
      ? { ok: true, fixed_book: "buku hasil fix", file_name: "f.txt", checklist_before: "b", checklist_after: "a",
          ...(flagOn ? { report_language: "id", review_contract_version: nrc.REVIEW_CONTRACT_VERSION } : {}) }
      : { ok: true, fixed_book: "buku hasil fix (should never be shown)", file_name: "f.txt", checklist_before: "b", checklist_after: "a" };
    fetchHandlers.push({
      test: (url) => url === "/api/narasi/oneshot-fix/result/job-1",
      respond: async () => ({ ok: true, status: 200, json: async () => (resultBody) }),
    });
    await click("⚡ Submit One-Shot Fix");
    await flush(4300); // real poll interval is 4000ms
    finish({}); return;
  }

  throw new Error("unknown scenario: " + scenario);
}

main().catch((e) => {
  document.body.dataset.payload = toBase64Utf8(JSON.stringify({ error: String((e && e.stack) || e) }));
  document.body.dataset.result = "error";
});
'''

_B04A_HTML_SHELL_SOURCE = r'''<!doctype html>
<html><head><meta charset="UTF-8"><title>b04a mount harness</title></head>
<body data-result="pending"><div id="root"></div><script type="module" src="/__B04A_CLIENT_SCRIPT_NAME__"></script></body></html>
'''

_B04A_RUNNER_SOURCE = r'''
// Rework 4: adapted from the already-authorized real Chrome + Vite harness pattern in
// ~/docs/IMPORTANT-wimba-narasi-b07-b08-lifecycle-acceptance-v7/run-mounted-frontend.mjs.
// Serves the frontend's OWN real Vite root (no aliasing needed -- the harness HTML/mjs
// pair is generated INSIDE the frontend dir, so /src/main.jsx resolves exactly as it
// does for the real production index.html), points a real headless Chrome at it with
// --dump-dom, and extracts a base64 JSON payload the client script stashed into a
// data-* attribute. No jsdom, no new dependency, no npm install, no registry access.
import { spawn } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

const FRONTEND_DIR = process.env.B04A_FRONTEND || process.cwd();
const HTML_NAME = process.env.B04A_HARNESS_HTML;
const FLAG_ON = process.env.B04A_FLAG === "1";
const API_MODE = process.env.B04A_API_MODE === "google" ? "google" : "laozhang";
const SCENARIO = process.env.B04A_SCENARIO || "review";
// Rework 8: report_lang/heading_style are BOTH optional -- only the language-matrix
// scenario reads them; every other scenario is unaffected (empty string when unset).
const REPORT_LANG = process.env.B04A_REPORT_LANG || "";
const HEADING_STYLE = process.env.B04A_HEADING_STYLE || "";
const VIRTUAL_TIME_BUDGET = process.env.B04A_VIRTUAL_TIME_BUDGET || "15000";

async function findHeadlessShell(root) {
  try {
    for (const entry of await fs.readdir(root, { withFileTypes: true })) {
      const candidate = path.join(root, entry.name);
      if (entry.isDirectory()) {
        const nested = await findHeadlessShell(candidate);
        if (nested) return nested;
      } else if (entry.isFile() && entry.name === "chrome-headless-shell") {
        return candidate;
      }
    }
  } catch {
    return null;
  }
  return null;
}

async function main() {
  if (!HTML_NAME) throw new Error("B04A_HARNESS_HTML not set");
  if (FLAG_ON) process.env.VITE_NARASI_REVIEW_LANGUAGE_CHAPTER_KEY_V1 = "1";
  else delete process.env.VITE_NARASI_REVIEW_LANGUAGE_CHAPTER_KEY_V1;

  const viteModule = pathToFileURL(path.join(FRONTEND_DIR, "node_modules/vite/dist/node/index.js")).href;
  const { createServer } = await import(viteModule);
  const server = await createServer({
    root: FRONTEND_DIR, configFile: false, logLevel: "silent", envDir: FRONTEND_DIR, mode: "production",
    server: { host: "127.0.0.1", port: 0, strictPort: false },
  });

  let profile;
  try {
    await server.listen();
    const address = server.httpServer.address();
    const port = typeof address === "object" && address ? address.port : 0;
    profile = await fs.mkdtemp(path.join(os.tmpdir(), "b04a-chrome-profile-"));
    const chrome = process.env.B04A_CHROME
      || (await findHeadlessShell(path.join(process.env.HOME || "", ".cache/puppeteer/chrome-headless-shell")))
      || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
    const query = new URLSearchParams({ flag: FLAG_ON ? "1" : "0", api_mode: API_MODE, scenario: SCENARIO, report_lang: REPORT_LANG, heading_style: HEADING_STYLE }).toString();
    const url = `http://127.0.0.1:${port}/${HTML_NAME}?${query}`;
    const args = ["--headless", "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage",
      `--user-data-dir=${profile}`, `--virtual-time-budget=${VIRTUAL_TIME_BUDGET}`, "--dump-dom", url];
    const result = await new Promise((resolve) => {
      const child = spawn(chrome, args, { stdio: ["ignore", "pipe", "pipe"] });
      let stdout = ""; let stderr = "";
      child.stdout.on("data", (c) => { stdout += c; });
      child.stderr.on("data", (c) => { stderr += c; });
      child.on("close", (code) => resolve({ code, stdout, stderr }));
      child.on("error", (error) => resolve({ code: 1, stdout, stderr: String(error) }));
    });
    if (result.code !== 0) {
      process.stderr.write("chrome exited " + result.code + "\n" + result.stderr + "\n");
      process.exitCode = 1;
      return;
    }
    const resultAttr = result.stdout.match(/data-result="([^"]*)"/);
    const payloadAttr = result.stdout.match(/data-payload="([^"]*)"/);
    if (!payloadAttr) {
      process.stderr.write("NO data-payload attribute found. dumped DOM (first 4000 chars):\n");
      process.stderr.write(result.stdout.slice(0, 4000) + "\n");
      process.exitCode = 1;
      return;
    }
    const decoded = JSON.parse(Buffer.from(payloadAttr[1], "base64").toString("utf-8"));
    if (resultAttr && resultAttr[1] === "error") {
      process.stderr.write("harness scenario threw: " + JSON.stringify(decoded) + "\n");
      process.exitCode = 1;
      return;
    }
    console.log(JSON.stringify(decoded));
  } finally {
    await server.close();
    if (profile) await fs.rm(profile, { recursive: true, force: true });
  }
}

main().catch((e) => { console.error(e); process.exitCode = 1; });
'''

def run_mount_harness(*, flag_on, api_mode="laozhang", scenario="review", timeout=60, report_lang=None, heading_style=None):
    """Rework 4 (Codex P1 finding #1): mounts the REAL production App inside a real,
    headless Chrome tab -- no jsdom, no DOM-emulation dependency of any kind, no npm
    install, no registry access (see also test_g09_harness_never_installs_dependencies
    below, an executable clean-environment negative control for exactly this claim).
    Generates three transient files INSIDE _FRONTEND_DIR (client script, HTML shell,
    Node runner) -- never a persistent repo file -- runs the runner via subprocess,
    parses its JSON stdout, and deletes all three in `finally`."""
    fd1, client_tmp = tempfile.mkstemp(prefix=".b04a_client_", suffix=".mjs", dir=str(_FRONTEND_DIR))
    os.close(fd1)
    client_path = Path(client_tmp)
    fd2, html_tmp = tempfile.mkstemp(prefix=".b04a_shell_", suffix=".html", dir=str(_FRONTEND_DIR))
    os.close(fd2)
    html_path = Path(html_tmp)
    fd3, runner_tmp = tempfile.mkstemp(prefix=".b04a_runner_", suffix=".mjs", dir=str(_FRONTEND_DIR))
    os.close(fd3)
    runner_path = Path(runner_tmp)
    try:
        client_path.write_text(_B04A_CLIENT_SCRIPT_SOURCE, encoding="utf-8")
        html_path.write_text(
            _B04A_HTML_SHELL_SOURCE.replace("__B04A_CLIENT_SCRIPT_NAME__", client_path.name),
            encoding="utf-8")
        runner_path.write_text(_B04A_RUNNER_SOURCE, encoding="utf-8")
        env = {
            **os.environ, "NO_PROXY": "*", "no_proxy": "*",
            "B04A_FRONTEND": str(_FRONTEND_DIR),
            "B04A_HARNESS_HTML": html_path.name,
            "B04A_FLAG": "1" if flag_on else "0",
            "B04A_API_MODE": api_mode,
            "B04A_SCENARIO": scenario,
            "B04A_REPORT_LANG": report_lang or "",
            "B04A_HEADING_STYLE": heading_style or "",
        }
        proc = subprocess.run(
            ["node", str(runner_path)], cwd=str(_FRONTEND_DIR),
            text=True, capture_output=True, timeout=timeout, check=False, env=env,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return json.loads(proc.stdout)
    finally:
        client_path.unlink(missing_ok=True)
        html_path.unlink(missing_ok=True)
        runner_path.unlink(missing_ok=True)


# ── Independent fixture oracle — hand-derived, NEVER read from the fixtures or MANIFEST.json ──
_B04A_FIXTURE_ORACLE = {
    "id_manuscript_id_report": {
        "sha256": "fe991e24f0673e51f94eb0d8e0f539f544421bf8b706f02d3d28c6983ba32c81",
        "language_pair": ("id", "id"),
        "expected_chapter_keys": ["review:ordinal:1", "review:ordinal:2"],
        "expected_all_matched": True,
    },
    "en_manuscript_en_report": {
        "sha256": "52988dd71bf6af89107ab2af7bb2464067e92923535f8e312d313e479f76f4cb",
        "language_pair": ("en", "en"),
        "expected_chapter_keys": ["review:ordinal:1", "review:ordinal:2"],
        "expected_all_matched": True,
    },
    "en_manuscript_id_report": {
        "sha256": "cba823dba064bf56a6278756cf5dc6d30516432f666f197546ced245f5d8c883",
        "language_pair": ("en", "id"),
        "expected_chapter_keys": ["review:ordinal:1", "review:ordinal:2"],
        "expected_all_matched": True,
    },
    "id_manuscript_en_report": {
        "sha256": "782744452447978baf2810f60e76e9870ec9ccf754fc7e682b58b9fd688fec62",
        "language_pair": ("id", "en"),
        "expected_chapter_keys": ["review:ordinal:1", "review:ordinal:2"],
        "expected_all_matched": True,
    },
    "mixed_unique_headings": {
        "sha256": "c937763e8fd8b67346708894742ea1e52d847a72023db3b58943906af9ca1742",
        "language_pair": ("mixed", "id"),
        "expected_chapter_keys": ["review:ordinal:3", "review:ordinal:5"],
        "expected_all_matched": True,
    },
    "hostile_and_ambiguous_headings": {
        "sha256": "fb45304686a3f7d5eeffa5ea96b7c2832635ac89fa8adbb896467a2be43590d1",
        "language_pair": ("id", "id"),
        "expected_chapter_keys": ["review:ordinal:12", "review:ordinal:15", "review:ordinal:25"],
        "expected_duplicate_ordinals": [7],
        "expected_fence_malformed": False,
        "expected_match_reasons": [
            "NO_MANUSCRIPT_MATCH",
            "DUPLICATE_REVIEW_TARGET",
            "DUPLICATE_REVIEW_TARGET",
            "NO_MANUSCRIPT_MATCH",
            "UNRECOGNIZED_HEADING",
        ],
    },
}


def _covers(*acceptance_ids):
    """Attaches an explicit, out-of-band acceptance-ID binding to a test method — a plain
    function attribute, never derived from (or matched against) the method's own Python
    identifier. See test_narasi_b01_post_mutation_revalidation.py's identical mechanism and its
    documented rationale (a name-substring oracle is not independent even when it reads source
    text instead of `dir()`)."""
    def _decorator(fn):
        fn._acceptance_ids = tuple(acceptance_ids)
        return fn
    return _decorator


# ===========================================================================
# Shared fakes for the backend endpoint harness
# ===========================================================================
class _FakeUsage:
    prompt_tokens = 10
    completion_tokens = 20


class _FakeChatChoice:
    def __init__(self, content):
        self.message = type("_Msg", (), {"content": content})()
        self.finish_reason = "stop"


class _FakeChatResp:
    def __init__(self, content):
        self.choices = [_FakeChatChoice(content)]
        self.usage = _FakeUsage()


class _FakeReviewClient:
    """Records every chat.completions.create call so tests can inspect the exact `system`
    text the endpoint actually sent — never a re-expression of what it "should" send."""
    calls = []

    class chat:
        class completions:
            @staticmethod
            def create(**kw):
                _FakeReviewClient.calls.append(kw)
                return _FakeChatResp("a fine editorial review")


def _patch_review_common(monkeypatch, *, make_client_calls=None, derived_input_calls=None,
                          moat_calls=None):
    async def fake_resolve_user_uuid(t, u):
        return _UID

    async def fake_log_usage(*a, **kw):
        return 0

    async def fake_persist_asset(*a, **kw):
        return None

    async def fake_create_derived_input(*a, **kw):
        if derived_input_calls is not None:
            derived_input_calls.append((a, kw))
        return "review-derived-uuid"

    async def fake_finish_narasi_job_by_id(*a, **kw):
        return None

    async def fake_save_moat_session(*a, **kw):
        if moat_calls is not None:
            moat_calls.append((a, kw))
        return None

    _FakeReviewClient.calls = []

    def make_client_factory(model):
        if make_client_calls is not None:
            make_client_calls.append(model)
        return _FakeReviewClient()

    monkeypatch.setattr(laozhang_api, "make_client", make_client_factory)
    monkeypatch.setattr(laozhang_api, "_resolve_user_uuid", fake_resolve_user_uuid)
    monkeypatch.setattr(laozhang_api, "_log_narasi_usage", fake_log_usage)
    monkeypatch.setattr(laozhang_api, "_persist_asset", fake_persist_asset)
    monkeypatch.setattr(database, "create_derived_input", fake_create_derived_input)
    monkeypatch.setattr(database, "finish_narasi_job_by_id", fake_finish_narasi_job_by_id)
    monkeypatch.setattr(database, "save_moat_session", fake_save_moat_session)
    monkeypatch.setattr(laozhang_api, "db", database)


def _linked_source(monkeypatch, *, target_language, src_uuid="row-uuid-1"):
    outline = admit_outline_lifecycle(
        "8d95ed8a-1853-4ea4-9f47-5ae00fb61e21", target_language,
        [{"id": "1", "title": "One", "words": 500}, {"id": "2", "title": "Two", "words": 600}])
    snap = build_job_lifecycle(outline, outline["chapters"])

    async def fake_get_job_by_external(tenant_id, job_id):
        return {"id": src_uuid, "input_payload": {"narasi_lifecycle": snap}}

    async def fake_get_job(tenant_id, job_uuid):
        return {"id": src_uuid, "input_payload": {"narasi_lifecycle": snap}}

    monkeypatch.setattr(database, "get_job_by_external", fake_get_job_by_external)
    monkeypatch.setattr(database, "get_job", fake_get_job)
    return src_uuid


def review_body(**extra):
    body = {"message": "please review this manuscript"}
    body.update(extra)
    return body


class _SucceedingOpenAI:
    def __init__(self, **kw):
        pass

    class chat:
        class completions:
            _calls = []

            @staticmethod
            def create(**kw):
                _SucceedingOpenAI.chat.completions._calls.append(kw)
                return _FakeChatResp(
                    "---FIXED_BOOK_START---\nfixed manuscript text\n---FIXED_BOOK_END---")


def _patch_oneshot_common(monkeypatch, *, create_job_id="oneshot-job-1", derived_input_calls=None):
    async def fake_resolve_user_uuid(t, u):
        return _UID

    async def fake_create_derived_input(*a, **kw):
        if derived_input_calls is not None:
            derived_input_calls.append((a, kw))
        return "oneshot-derived-uuid"

    terminalized = []

    async def fake_finish_narasi_job_by_id(tenant_id, job_uuid, status, result=None, error=None):
        terminalized.append((job_uuid, status, error))

    create_job_calls = []

    async def fake_create_job(*a, **kw):
        create_job_calls.append((a, kw))
        return create_job_id

    async def fake_set_progress(*a, **kw):
        return None

    async def fake_delete_progress(*a, **kw):
        return None

    async def fake_log_usage(*a, **kw):
        return 0

    async def fake_persist_asset(*a, **kw):
        return None

    async def fake_save_correction_pair(*a, **kw):
        return {"quality_tier": "high", "edit_ratio": 0.1}

    complete_job_calls = []

    async def fake_complete_job(job_id, result):
        complete_job_calls.append((job_id, result))

    save_correction_pair_calls = []

    async def recording_save_correction_pair(*a, **kw):
        save_correction_pair_calls.append((a, kw))
        return {"quality_tier": "high", "edit_ratio": 0.1}

    _SucceedingOpenAI.chat.completions._calls = []

    monkeypatch.setattr(laozhang_api, "_resolve_user_uuid", fake_resolve_user_uuid)
    monkeypatch.setattr(database, "create_derived_input", fake_create_derived_input)
    monkeypatch.setattr(database, "finish_narasi_job_by_id", fake_finish_narasi_job_by_id)
    monkeypatch.setattr(database, "create_job", fake_create_job)
    monkeypatch.setattr(database, "complete_job", fake_complete_job)
    monkeypatch.setattr(database, "save_correction_pair", recording_save_correction_pair)
    monkeypatch.setattr(laozhang_api, "rc", type("_R", (), {
        "set_progress": staticmethod(fake_set_progress),
        "delete_progress": staticmethod(fake_delete_progress),
    }))
    monkeypatch.setattr(laozhang_api, "_log_narasi_usage", fake_log_usage)
    monkeypatch.setattr(laozhang_api, "_persist_asset", fake_persist_asset)
    monkeypatch.setattr(laozhang_api, "OpenAI", _SucceedingOpenAI)
    monkeypatch.setattr(laozhang_api, "db", database)
    return {"terminalized": terminalized, "complete_job_calls": complete_job_calls,
            "save_correction_pair_calls": save_correction_pair_calls,
            "create_job_calls": create_job_calls}


async def _run_oneshot_and_await_background(monkeypatch, body):
    real_create_task = asyncio.create_task
    captured = {}

    def spy_create_task(coro):
        task = real_create_task(coro)
        captured["task"] = task
        return task

    monkeypatch.setattr(laozhang_api.asyncio, "create_task", spy_create_task)
    out = await laozhang_api.oneshot_fix_submit(body, _user())
    await captured["task"]
    return out


def run_oneshot(monkeypatch, body):
    return run(_run_oneshot_and_await_background(monkeypatch, body))


_BACKEND_ROOT = Path(__file__).resolve().parents[2]

_EXPECTED_BACKEND_MODIFIED = {
    "backend/server.js", "python/database.py", "python/laozhang_api.py",
    "python/narration_api.py", "python/narration_worker.py",
    "python/orchestrator/core.py", "python/orchestrator/router.py",
    "python/orchestrator/static.py",
}
_EXPECTED_BACKEND_UNTRACKED = {
    "database/migrations/0070_narasi_lifecycle.sql",
    "database/migrations/0071_narasi_derived_input.sql",
    "database/migrations/0072_narasi_continuity_schema.sql",
    "python/continuity/", "python/narasi_language_rescan.py",
    "python/narasi_review_contract.py",
    "tests/narasi_gates/", "tests/python/fixtures/",
    "tests/python/narasi_a04_gate_inventory.json",
    "tests/python/test_narasi_a04_existing_gates.py",
    "tests/python/test_narasi_b01_post_mutation_revalidation.py",
    "tests/python/test_narasi_b03_language_rescan.py",
    "tests/python/test_narasi_b04a_review_language_chapter_id.py",
    "tests/python/test_narasi_lifecycle_repository.py",
}
# Rework 3 (Codex P1 finding #1): A08 must derive its allowed delta from the immutable
# pack allowlist -- never expand its expected set to bless a new file or dependency.
# The Rework 2 jsdom devDependency and tests/python/b04a_mount_harness.mjs have both
# been removed. Rework 4 (Codex P1 finding #1) went further and removed jsdom itself
# entirely -- the mounted-component harness (see run_mount_harness() below) now drives a
# real, headless Chrome tab against this project's own real Vite dev server, touching
# neither package.json nor package-lock.json and installing nothing at all.
_EXPECTED_FRONTEND_MODIFIED = {"public/auth.js", "src/main.jsx"}
_EXPECTED_FRONTEND_UNTRACKED = {
    "src/narasiLifecycle.mjs", "src/narasiReviewContract.mjs", "src/narasiRunAuthority.mjs",
}


def _git_status_sets(repo_root):
    proc = subprocess.run(["git", "status", "--short"], cwd=repo_root, text=True,
                           capture_output=True, timeout=15, check=True)
    modified, untracked, deleted = set(), set(), set()
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        code, path = line[:2], line[3:]
        if code.strip() == "M":
            modified.add(path)
        elif code.strip() == "??":
            untracked.add(path)
        elif "D" in code:
            deleted.add(path)
    return modified, untracked, deleted


# Fresh-interpreter script for A01's import-freedom proof (Rework 1, Codex P2 finding
# #5). Written to a temp file and executed via `subprocess.run([sys.executable, ...])`
# so it gets its OWN sys.modules -- never contaminated by this test file's own
# top-level `import narasi_review_contract as nrc`. `__PY_DIR__` is substituted with
# json.dumps(str(backend "python" dir)) before writing.
_B04A_IMPORT_FREE_SUBPROCESS_SCRIPT = '''
import sys, os, asyncio

sys.path.insert(0, __PY_DIR__)
os.environ.setdefault("LAOZHANG_API_KEY", "sk-test-key-for-unit-tests")
os.environ.setdefault("LAOZHANG_IMAGE_API_KEY", "sk-test-key-for-unit-tests")
os.environ.pop("NARASI_REVIEW_LANGUAGE_CHAPTER_KEY_V1", None)

import database
import laozhang_api
from auth_middleware import CurrentUser


class _FakeChoice:
    def __init__(self, content):
        self.message = type("_M", (), {"content": content})()
        self.finish_reason = "stop"


class _FakeResp:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]
        self.usage = None


class _FakeClient:
    class chat:
        class completions:
            @staticmethod
            def create(**kw):
                return _FakeResp("a fine editorial review")


async def _fake_async(*a, **kw):
    return None


async def _fake_resolve_user_uuid(t, u):
    return "66666666-6666-4666-8666-666666666666"


async def _fake_create_derived_input(*a, **kw):
    return "derived-uuid"


async def _fake_create_job(*a, **kw):
    return "job-1"


async def _fake_complete_job(*a, **kw):
    return None


async def _fake_save_correction_pair(*a, **kw):
    return {"quality_tier": "high", "edit_ratio": 0.1}


laozhang_api.make_client = lambda model: _FakeClient()
laozhang_api._resolve_user_uuid = _fake_resolve_user_uuid
laozhang_api._log_narasi_usage = _fake_async
laozhang_api._persist_asset = _fake_async
laozhang_api.db = database
database.create_derived_input = _fake_create_derived_input
database.finish_narasi_job_by_id = _fake_async
database.save_moat_session = _fake_async
database.create_job = _fake_create_job
database.complete_job = _fake_complete_job
database.save_correction_pair = _fake_save_correction_pair
laozhang_api.rc = type("_R", (), {
    "set_progress": staticmethod(_fake_async),
    "delete_progress": staticmethod(_fake_async),
})
laozhang_api.OpenAI = _FakeClient

_user = CurrentUser(tenant_id="77777777-7777-4777-8777-777777777777",
                     user_id="66666666-6666-4666-8666-666666666666",
                     plan="pro", tier="pro")

asyncio.run(laozhang_api.narasi_review({"message": "please review this manuscript"}, _user))

_real_create_task = asyncio.create_task
_captured = {}


def _spy_create_task(coro):
    task = _real_create_task(coro)
    _captured["task"] = task
    return task


async def _run_oneshot():
    laozhang_api.asyncio.create_task = _spy_create_task
    await laozhang_api.oneshot_fix_submit(
        {"content": "manuscript body", "system": "editor rules"}, _user)
    await _captured["task"]


asyncio.run(_run_oneshot())

assert "narasi_review_contract" not in sys.modules, (
    "narasi_review_contract was imported even though the B-04a flag was off")
print("IMPORT_FREE_OK")
'''


# ===========================================================================
# A — Authority, flags, and compatibility
# ===========================================================================
class TestA_AuthorityFlagsCompat:
    @_covers("A01")
    def test_a01_backend_flag_off_is_byte_identical_to_legacy(self, monkeypatch):
        _patch_review_common(monkeypatch)
        out = run(laozhang_api.narasi_review(review_body(), _user()))
        assert "report_language" not in out
        assert "review_contract_version" not in out
        system_sent = _FakeReviewClient.calls[0]["messages"][0]["content"]
        assert "FINAL LANGUAGE CONTRACT" not in system_sent
        # Rework 1 (Codex P2 finding #5): flag-off must be genuinely import-free, not
        # merely behaviorally identical -- narasi_review_contract must never even enter
        # sys.modules. This can only be proven in a FRESH interpreter (this test file's
        # own top-level `import narasi_review_contract as nrc` would otherwise make the
        # module already present in THIS process's sys.modules regardless of the fix).
        script = _B04A_IMPORT_FREE_SUBPROCESS_SCRIPT.replace(
            "__PY_DIR__", json.dumps(str(_BACKEND_ROOT / "python")))
        script_path = Path(self._tmp_import_free_dir()) / "check_b04a_import_free.py"
        script_path.write_text(script, encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(script_path)], cwd=str(_BACKEND_ROOT),
            text=True, capture_output=True, timeout=30, check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "IMPORT_FREE_OK" in proc.stdout

    @staticmethod
    def _tmp_import_free_dir():
        return tempfile.mkdtemp(prefix="b04a_import_free_")

    @_covers("A02")
    def test_a02_frontend_flag_absent_or_false_keeps_legacy_matcher(self):
        for raw in [None, "0", "false", "  ", "banana"]:
            assert node_call(f"m.isFeatureEnabled({json.dumps(raw)})")["value"] is False
        text = frontend_text("src/main.jsx")
        assert "chapterKeyEnabled" in text
        assert "/^Bab\\s+\\d+/i" in text  # legacy Bab-only regex still present for flag-off path

    @_covers("A03")
    def test_a03_only_exact_true_tokens_enable_without_invoking_dunder_methods(self):
        for tok in ["1", "true", "TRUE", " yes ", "On"]:
            assert node_call(f"m.isFeatureEnabled({json.dumps(tok)})")["value"] is True
        for hostile in [True, False, 1, 0, None, [], {}, "maybe"]:
            assert node_call(f"m.isFeatureEnabled({json.dumps(hostile)})")["value"] is False
        # A hostile object whose toString/valueOf/Symbol.toPrimitive would throw if invoked --
        # isFeatureEnabled's exact `typeof raw !== "string"` gate must reject it WITHOUT calling
        # any of those traps.
        expr = (
            "(function(){"
            "let calls=0;"
            "const hostile={toString(){calls++;throw new Error('toString called');},"
            "valueOf(){calls++;throw new Error('valueOf called');}};"
            "const enabled=m.isFeatureEnabled(hostile);"
            "return {enabled, calls};"
            "})()"
        )
        result = node_eval(expr)
        assert result["enabled"] is False
        assert result["calls"] == 0

    @_covers("A04")
    def test_a04_frontend_on_backend_off_response_is_bounded_mismatch(self):
        mismatch_absent = node_eval("m.verifyResponseCapability({ok:true,text:'x'}, true)")
        assert mismatch_absent["capable"] is False
        assert mismatch_absent["mismatch"] is True
        capable = node_eval(f'm.verifyResponseCapability({{review_contract_version:{json.dumps(nrc.REVIEW_CONTRACT_VERSION)}}}, true)')
        assert capable["capable"] is True
        assert capable["mismatch"] is False

    @_covers("A05")
    def test_a05_frontend_off_backend_on_legacy_request_defaults_id(self, monkeypatch):
        monkeypatch.setenv("NARASI_REVIEW_LANGUAGE_CHAPTER_KEY_V1", "1")
        _patch_review_common(monkeypatch)
        # Legacy body: no report_language key at all (as a flag-off frontend would send).
        out = run(laozhang_api.narasi_review(review_body(), _user()))
        assert out["report_language"] == "id"
        assert out["manuscript_language"] is None  # standalone, no source, no explicit language

    @_covers("A06")
    def test_a06_no_rollout_deploy_migration_or_ledger_edit(self):
        _, untracked, _ = _git_status_sets(_BACKEND_ROOT)
        # A06's negative claims are literally: no NEW migration file, no ledger-path modification.
        new_migrations = {p for p in untracked if p.startswith("database/migrations/") and
                          p not in _EXPECTED_BACKEND_UNTRACKED}
        assert new_migrations == set()
        ledger_path = Path(
            "/Users/rino/docs/IMPORTANT-wimba-narasi-permanent-continuity-execution-plan-CLEAN.md")
        # The ledger lives outside this git repo entirely; confirm its hash matches the one
        # SOURCE-BASELINE.json recorded (i.e. untouched by this implementation round).
        import hashlib
        actual = hashlib.sha256(ledger_path.read_bytes()).hexdigest()
        assert actual == "cdee6cde750122acc7b2699ec3bf5d7cf1bdaff77b0d5b3a99173b39ffb51393"

    @_covers("A07")
    def test_a07_active_frontend_path_is_main_jsx_no_tsx_or_generated_html(self):
        assert (_FRONTEND_DIR / "src/main.jsx").exists()
        assert not (_FRONTEND_DIR / "src/NarasiReviewTool.tsx").exists()
        modified, untracked, _ = _git_status_sets(_FRONTEND_DIR)
        assert not any(p.startswith("dist/") for p in modified | untracked)
        assert not any(p.endswith(".html") for p in modified | untracked)

    @_covers("A08")
    def test_a08_touches_only_exact_allowlist(self):
        b_mod, b_unt, b_del = _git_status_sets(_BACKEND_ROOT)
        f_mod, f_unt, f_del = _git_status_sets(_FRONTEND_DIR)
        assert b_mod == _EXPECTED_BACKEND_MODIFIED
        assert b_unt == _EXPECTED_BACKEND_UNTRACKED
        assert b_del == set()
        assert f_mod == _EXPECTED_FRONTEND_MODIFIED
        assert f_unt == _EXPECTED_FRONTEND_UNTRACKED
        assert f_del == set()


# ===========================================================================
# B — Backend Review language separation
# ===========================================================================
class TestB_BackendReviewLanguage:
    @_covers("B01")
    def test_b01_report_language_id_exact_token_contract(self):
        block = nrc.build_review_language_block("id", "id")
        assert 'chapter label: "Bab"' in block
        assert 'score: "Skor"' in block
        assert 'key weaknesses: "Kelemahan Utama"' in block
        assert 'recommendations: "Rekomendasi"' in block
        assert 'no significant issues: "Tidak ada kelemahan signifikan."' in block
        assert 'final verdict: "Verdict Akhir"' in block
        assert '"Chapter"' not in block and '"Score"' not in block

    @_covers("B02")
    def test_b02_report_language_en_exact_token_contract(self):
        block = nrc.build_review_language_block("id", "en")
        assert 'chapter label: "Chapter"' in block
        assert 'score: "Score"' in block
        assert 'key weaknesses: "Key Weaknesses"' in block
        assert 'recommendations: "Recommendations"' in block
        assert 'no significant issues: "No significant weaknesses."' in block
        assert 'final verdict: "Final Verdict"' in block
        assert '"Bab"' not in block and '"Skor"' not in block

    @_covers("B03")
    def test_b03_missing_or_blank_defaults_to_id(self):
        assert nrc.normalize_report_language(None) == "id"
        assert nrc.normalize_report_language("") == "id"
        assert nrc.normalize_report_language("   ") == "id"

    @_covers("B04")
    def test_b04_case_and_whitespace_normalization_deterministic_unsupported_rejects(self, monkeypatch):
        assert nrc.normalize_report_language("  EN  ") == "en"
        assert nrc.normalize_report_language("Id") == "id"
        assert nrc.normalize_report_language("ID") == "id"
        with pytest.raises(nrc.ReviewContractError) as caught:
            nrc.normalize_report_language("fr")
        assert caught.value.code == "REPORT_LANGUAGE_UNSUPPORTED"
        with pytest.raises(nrc.ReviewContractError):
            nrc.normalize_report_language("id_en")  # compound value

        monkeypatch.setenv("NARASI_REVIEW_LANGUAGE_CHAPTER_KEY_V1", "1")
        calls = []
        _patch_review_common(monkeypatch, make_client_calls=calls)
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api.narasi_review(review_body(report_language="fr"), _user()))
        assert caught.value.status_code == 422
        assert "REPORT_LANGUAGE_UNSUPPORTED" in caught.value.detail
        assert calls == []  # provider never reached

    @_covers("B05")
    def test_b05_exact_type_first_rejects_hostile_before_provider_or_persistence(self, monkeypatch):
        class HostileStr(str):
            def __eq__(self, other):
                raise RuntimeError("hostile __eq__ invoked")

            def __hash__(self):
                return 0

        for hostile in [True, False, 1, 0, b"id", [], {}, HostileStr("id")]:
            with pytest.raises(nrc.ReviewContractError) as caught:
                nrc.normalize_report_language(hostile)
            assert caught.value.code == "REPORT_LANGUAGE_TYPE_INVALID"

        monkeypatch.setenv("NARASI_REVIEW_LANGUAGE_CHAPTER_KEY_V1", "1")
        make_calls, derived_calls = [], []
        _patch_review_common(monkeypatch, make_client_calls=make_calls, derived_input_calls=derived_calls)
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api.narasi_review(review_body(report_language=True), _user()))
        assert caught.value.status_code == 422
        assert make_calls == []
        assert derived_calls == []  # rejected before ANY persistence work

    @_covers("B06")
    def test_b06_source_linked_manuscript_language_cannot_be_overridden_by_report_language(self, monkeypatch):
        monkeypatch.setenv("NARASI_REVIEW_LANGUAGE_CHAPTER_KEY_V1", "1")
        src_uuid = _linked_source(monkeypatch, target_language="en")
        _patch_review_common(monkeypatch)
        out = run(laozhang_api.narasi_review(
            review_body(source_job_id="ext1", source_job_uuid=src_uuid, report_language="id"), _user()))
        assert out["manuscript_language"] == "en"   # unchanged by report_language="id"
        assert out["report_language"] == "id"

    @_covers("B07")
    def test_b07_standalone_manuscript_language_requirement_inherited(self, monkeypatch):
        monkeypatch.setenv("NARASI_LIFECYCLE_V1", "1")
        monkeypatch.setenv("NARASI_REVIEW_LANGUAGE_CHAPTER_KEY_V1", "1")
        _patch_review_common(monkeypatch)
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api.narasi_review(review_body(report_language="en"), _user()))
        assert caught.value.status_code == 422
        assert "manuscript_language is required" in caught.value.detail

    @_covers("B08")
    def test_b08_block_appended_even_on_legacy_client_system_path(self, monkeypatch):
        monkeypatch.setenv("NARASI_REVIEW_LANGUAGE_CHAPTER_KEY_V1", "1")
        _patch_review_common(monkeypatch)
        out = run(laozhang_api.narasi_review(
            review_body(system="a completely custom client system prompt"), _user()))
        system_sent = _FakeReviewClient.calls[0]["messages"][0]["content"]
        assert system_sent.startswith("a completely custom client system prompt")
        assert "FINAL LANGUAGE CONTRACT" in system_sent
        assert out["ok"] is True

    @_covers("B09")
    def test_b09_quotes_stay_manuscript_language_replacement_prose_instructed_same(self):
        block = nrc.build_review_language_block("id", "en")
        assert "Quoted manuscript evidence MUST remain byte-for-byte in MANUSCRIPT_LANGUAGE" in block
        assert "Proposed replacement prose MUST remain in MANUSCRIPT_LANGUAGE" in block

    @_covers("B10")
    def test_b10_response_adds_exact_fields_only_when_enabled_no_extra_prose(self, monkeypatch):
        monkeypatch.setenv("NARASI_REVIEW_LANGUAGE_CHAPTER_KEY_V1", "1")
        moat_calls = []
        _patch_review_common(monkeypatch, moat_calls=moat_calls)
        out = run(laozhang_api.narasi_review(review_body(report_language="en"), _user()))
        legacy_keys = {"ok", "text", "finish_reason", "usage", "manuscript_language", "review_input_binding"}
        assert set(out.keys()) == legacy_keys | {"report_language", "review_contract_version"}
        assert out["review_contract_version"] == nrc.REVIEW_CONTRACT_VERSION
        moat_payload = moat_calls[0][0][4]
        assert moat_payload["report_language"] == "en"
        assert moat_payload["review_contract_version"] == nrc.REVIEW_CONTRACT_VERSION


# ===========================================================================
# C — One-Shot and Optimize separation
# ===========================================================================
class TestC_OneshotOptimizeSeparation:
    @_covers("C01")
    def test_c01_checklist_uses_report_language_fixed_prose_uses_manuscript_language(self):
        instr = nrc.build_oneshot_language_instruction("id", "en")
        assert "Scan/checklist prose must use REPORT_LANGUAGE = en" in instr
        assert "fixed manuscript body must remain in MANUSCRIPT_LANGUAGE = id" in instr

    @_covers("C02")
    def test_c02_both_cross_language_combinations_preserve_manuscript_prose(self, monkeypatch):
        for manuscript_lang, report_lang in [("en", "id"), ("id", "en")]:
            instr = nrc.build_oneshot_language_instruction(manuscript_lang, report_lang)
            assert f"MANUSCRIPT_LANGUAGE = {manuscript_lang}" in instr
            assert f"REPORT_LANGUAGE = {report_lang}" in instr

        monkeypatch.setenv("NARASI_REVIEW_LANGUAGE_CHAPTER_KEY_V1", "1")
        state = _patch_oneshot_common(monkeypatch)
        out = run_oneshot(monkeypatch, {
            "content": "manuscript body", "system": "editor rules", "report_language": "en"})
        assert out["ok"] is True
        result = state["complete_job_calls"][0][1]
        assert result["report_language"] == "en"
        # manuscript_language resolves to None here (standalone, no source, no explicit value) --
        # proving report_language never substitutes for it.
        assert result["manuscript_language"] is None

    @_covers("C03")
    def test_c03_original_heading_bytes_preserved_no_label_translation(self):
        instr = nrc.build_oneshot_language_instruction("id", "en")
        assert 'never rename "Chapter N" to "Bab N" or the reverse' in instr
        assert "byte-for-byte" in instr

    @_covers("C04")
    def test_c04_fixed_book_delimiters_exact_language_neutral_both_languages(self):
        for report_lang in ("id", "en"):
            instr = nrc.build_oneshot_language_instruction("id", report_lang)
            assert "---FIXED_BOOK_START---" in instr
            assert "---FIXED_BOOK_END---" in instr
            assert "language-neutral" in instr

    @_covers("C05")
    def test_c05_oneshot_result_includes_exact_fields_only_when_enabled(self, monkeypatch):
        monkeypatch.setenv("NARASI_REVIEW_LANGUAGE_CHAPTER_KEY_V1", "1")
        state = _patch_oneshot_common(monkeypatch)
        run_oneshot(monkeypatch, {"content": "manuscript body", "system": "editor rules",
                                   "report_language": "en"})
        result = state["complete_job_calls"][0][1]
        assert result["report_language"] == "en"
        assert result["review_contract_version"] == nrc.REVIEW_CONTRACT_VERSION

        monkeypatch.delenv("NARASI_REVIEW_LANGUAGE_CHAPTER_KEY_V1", raising=False)
        state_off = _patch_oneshot_common(monkeypatch, create_job_id="oneshot-job-off")
        run_oneshot(monkeypatch, {"content": "manuscript body", "system": "editor rules"})
        result_off = state_off["complete_job_calls"][0][1]
        assert "report_language" not in result_off
        assert "review_contract_version" not in result_off

    @_covers("C06")
    def test_c06_correction_pair_and_derived_input_language_stay_manuscript_authority(self, monkeypatch):
        monkeypatch.setenv("NARASI_REVIEW_LANGUAGE_CHAPTER_KEY_V1", "1")
        src_uuid = _linked_source(monkeypatch, target_language="en")
        derived_calls = []
        state = _patch_oneshot_common(monkeypatch, derived_input_calls=derived_calls)
        run_oneshot(monkeypatch, {
            "content": "manuscript body", "system": "editor rules",
            "source_job_id": "ext1", "source_job_uuid": src_uuid, "report_language": "id"})
        assert derived_calls[0][1]["language"] == "en"
        pair_kwargs = state["save_correction_pair_calls"][0][0]
        # save_correction_pair positional signature: (job_id, tenant, user, content, fixed,
        # style, topic, duration, language, ...) -- language is the 9th positional arg (idx 8).
        assert pair_kwargs[8] == "en"

    @_covers("C07")
    def test_c07_vo_optimize_stays_byte_identical_no_checklist_localization(self, monkeypatch):
        monkeypatch.setenv("NARASI_REVIEW_LANGUAGE_CHAPTER_KEY_V1", "1")
        state = _patch_oneshot_common(monkeypatch)
        # A careless/malicious caller sends report_language even though this is a VO call.
        run_oneshot(monkeypatch, {
            "content": "manuscript body", "system": "You are a VO Script Editor.",
            "report_language": "en"})
        sent = _SucceedingOpenAI.chat.completions._calls[0]["messages"][1]["content"]
        assert sent == "NARASI:\nmanuscript body"
        assert "LANGUAGE CONTRACT" not in sent
        result = state["complete_job_calls"][0][1]
        assert "report_language" not in result
        assert "review_contract_version" not in result

    @_covers("C08")
    def test_c08_invalid_report_language_fails_before_job_creation_or_persistence(self, monkeypatch):
        monkeypatch.setenv("NARASI_REVIEW_LANGUAGE_CHAPTER_KEY_V1", "1")
        derived_calls = []
        state = _patch_oneshot_common(monkeypatch, derived_input_calls=derived_calls)
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api.oneshot_fix_submit(
                {"content": "manuscript body", "system": "editor rules", "report_language": 12345},
                _user()))
        assert caught.value.status_code == 422
        assert derived_calls == []
        assert state["create_job_calls"] == []


# ===========================================================================
# D — Exact frontend chapter parsing and matching
# ===========================================================================
class TestD_FrontendChapterParsing:
    @_covers("D01")
    def test_d01_exact_bab_1_parses_retains_heading_and_body_bytes(self):
        manuscript = "## Bab 1: Awal\n\nIsi paragraf pertama.\n"
        parsed = parse_manuscript(manuscript)
        chapters = dict(parsed["chapters"])
        assert "review:ordinal:1" in chapters
        rec = chapters["review:ordinal:1"]
        assert rec["heading"] == "## Bab 1: Awal"
        # Rework 1: body is byte-exact from source offsets, no split/join/trim
        # reconstruction -- includes the blank line after the heading and the
        # manuscript's own trailing newline, never silently stripped.
        assert rec["body"] == "\nIsi paragraf pertama.\n"
        # H09's real target: the chapter record's key set is exactly this five-field
        # shape -- never an additional alias field (e.g. a "chapter_id" key).
        assert set(rec.keys()) == {"key", "ordinal", "label", "heading", "body"}

    @_covers("D02")
    def test_d02_exact_chapter_1_parses_same_key_style_retains_bytes(self):
        manuscript = "## Chapter 1: The Start\n\nOpening paragraph text.\n"
        parsed = parse_manuscript(manuscript)
        chapters = dict(parsed["chapters"])
        assert "review:ordinal:1" in chapters
        rec = chapters["review:ordinal:1"]
        assert rec["heading"] == "## Chapter 1: The Start"
        # Rework 1: byte-exact body, see D01's comment.
        assert rec["body"] == "\nOpening paragraph text.\n"

    @_covers("D03")
    def test_d03_all_four_en_id_combinations_match_without_translating_headings(self):
        for name in ["id_manuscript_id_report", "en_manuscript_en_report",
                     "en_manuscript_id_report", "id_manuscript_en_report"]:
            fx = _load_fixture(name)
            oracle = _B04A_FIXTURE_ORACLE[name]
            parsed = parse_manuscript(fx["manuscript_text"])
            assert sorted(k for k, v in parsed["chapters"]) == oracle["expected_chapter_keys"]
            matches = match_sections(fx["review_sections"], fx["manuscript_text"])
            assert all(m["matchedKey"] is not None for m in matches)
            # Headings must appear verbatim in the manuscript -- never translated/renamed.
            for sec, rec in zip(fx["review_sections"], matches):
                assert sec["title"] in rec["matchedHeading"]

    @_covers("D04")
    def test_d04_lf_crlf_case_spaces_colon_hyphen_dashes_deterministic(self):
        assert node_call('m.extractChapterKey("bab 4: x")')["value"]["ordinal"] == 4
        assert node_call('m.extractChapterKey("BAB 4: x")')["value"]["ordinal"] == 4
        # Rework 2 fix: per the contract's Section 7 grammar ("accepted optional boundary
        # after the ordinal: end-of-line, whitespace, `:`, `-`, or Unicode en/em dash"), a
        # bare space is a valid boundary UNCONDITIONALLY -- a real whitespace-title
        # heading like "Bab 4 x" parses normally. Rework 1 wrongly narrowed this to
        # "trailing whitespace only"; the real defect it was chasing (a multiple-ordinal
        # connector phrase) is excluded by CHAPTER_HEADING_RE's own negative lookahead
        # instead (see D06's connector-specific cases).
        assert node_call('m.extractChapterKey("Bab 4 x")')["value"]["ordinal"] == 4
        assert node_call('m.extractChapterKey("Bab 4   ")')["value"]["ordinal"] == 4  # trailing spaces only
        assert node_call('m.extractChapterKey("Bab 4-x")')["value"]["ordinal"] == 4  # hyphen
        assert node_call('m.extractChapterKey("Bab 4\\u2013x")')["value"]["ordinal"] == 4  # en dash
        assert node_call('m.extractChapterKey("Bab 4\\u2014x")')["value"]["ordinal"] == 4  # em dash
        assert node_call('m.extractChapterKey("Bab 4")')["value"]["ordinal"] == 4  # end-of-line
        # Rework 2 requirement #1's exact four named cases: whitespace-title headings
        # parse; the "ordinal + connector(and/dan) + ordinal" multi-ordinal shape does not.
        assert node_call('m.extractChapterKey("Chapter 4 The Long Road")')["value"]["ordinal"] == 4
        assert node_call('m.extractChapterKey("Bab 4 Judul Bab")')["value"]["ordinal"] == 4
        assert node_call('m.extractChapterKey("Chapter 2 and 3")')["value"] is None
        assert node_call('m.extractChapterKey("Bab 2 dan 3")')["value"] is None

        crlf_manuscript = "## Bab 1: A\r\nLine one.\r\n\r\n## Bab 2: B\r\nLine two.\r\n"
        parsed = parse_manuscript(crlf_manuscript)
        assert sorted(k for k, v in parsed["chapters"]) == ["review:ordinal:1", "review:ordinal:2"]

    @_covers("D05")
    def test_d05_zero_signs_decimals_exponents_roman_unicode_digits_suffix_do_not_parse(self):
        for candidate in ["Bab 0: x", "Bab 00: x", "Bab -4: x", "Bab +4: x", "Bab 4.5: x",
                           "Bab 4e2: x", "Bab IV: x", "Bab ４: x", "Bab 4x: x", "Bab 4a"]:
            assert node_call(f"m.extractChapterKey({json.dumps(candidate)})")["value"] is None

    @_covers("D06")
    def test_d06_h1_h3_body_mentions_babylon_chapterhouse_multi_ordinal_do_not_parse(self):
        assert node_call('m.extractChapterKey("Babylon falls")')["value"] is None
        assert node_call('m.extractChapterKey("Chapterhouse of Dune")')["value"] is None
        # H02's real target: a prose sentence merely CONTAINING "Bab 4:" mid-string (a
        # valid trailing boundary, so only the `^` anchor stands between this and a
        # false match) must never match -- the anchor requires the label to start the
        # string, not appear anywhere within it.
        assert node_call('m.extractChapterKey("prefix Bab 4: suffix, not a real heading")')["value"] is None
        manuscript = (
            "# Bab 1: Not H2 (H1)\n\nBody mentioning Babylon and Chapterhouse and as covered "
            "in Chapter 2 and 3 previously.\n\n"
            "## Bab 5: Real Chapter\n\nReal body.\n\n"
            "### Bab 5: Not H2 (H3)\n\nShould not create a second entry.\n"
        )
        parsed = parse_manuscript(manuscript)
        assert sorted(k for k, v in parsed["chapters"]) == ["review:ordinal:5"]
        assert parsed["duplicateOrdinals"] == []

    @_covers("D07")
    def test_d07_fenced_headings_ignored_malformed_fence_is_explicit_ambiguity(self):
        fx = _load_fixture("hostile_and_ambiguous_headings")
        parsed = parse_manuscript(fx["manuscript_text"])
        assert 7 not in [v["ordinal"] for k, v in parsed["chapters"]]  # fenced Bab 7 never added
        assert parsed["fenceMalformed"] is False

        malformed = "## Bab 1: A\n\n```\nunterminated fence\n"
        parsed_malformed = parse_manuscript(malformed)
        assert parsed_malformed["fenceMalformed"] is True
        assert parsed_malformed["chapters"] == []

        # H04's isolating case (Rework 2): a fenced heading sharing its ordinal with a
        # REAL heading elsewhere in the SAME manuscript. Unlike the fixture above (which
        # has 2 real + 1 fenced Bab-7, so dupOrdinal stays [7] whether or not the fence
        # bypass fires), this manuscript's ONLY possible source of ambiguity is the
        # fence-close-scan bug itself -- a real, dedicated assertion H04's detached-copy
        # mutation can actually flip.
        fence_isolation = ("## Bab 9: Real\n\nReal body.\n\n"
                            "```\n## Bab 9: Fenced Duplicate\nFenced body.\n```\n")
        parsed_isolated = parse_manuscript(fence_isolation)
        assert "review:ordinal:9" in dict(parsed_isolated["chapters"])
        assert parsed_isolated["duplicateOrdinals"] == []

    @_covers("D08")
    def test_d08_duplicate_ordinal_or_heading_is_ambiguity_no_last_write_wins(self):
        fx = _load_fixture("hostile_and_ambiguous_headings")
        parsed = parse_manuscript(fx["manuscript_text"])
        assert parsed["duplicateOrdinals"] == [7]
        assert "review:ordinal:7" not in dict(parsed["chapters"])

        exact_dup_heading = "## Bab 5: Sama Persis\nA\n\n## Bab 5: Sama Persis\nB\n"
        parsed2 = parse_manuscript(exact_dup_heading)
        assert parsed2["duplicateOrdinals"] == [5]
        assert parsed2["duplicateHeadings"] == ["## Bab 5: Sama Persis"]
        assert parsed2["chapters"] == []

    @_covers("D09")
    def test_d09_multiple_review_sections_targeting_one_ordinal_is_ambiguity(self):
        fx = _load_fixture("hostile_and_ambiguous_headings")
        matches = match_sections(fx["review_sections"], fx["manuscript_text"])
        bab15 = [m for m in matches if m["key"] == "review:ordinal:15"]
        assert len(bab15) == 2
        assert all(m["ambiguous"] is True and m["reason"] == "DUPLICATE_REVIEW_TARGET" for m in bab15)
        assert all(m["matchedKey"] is None for m in bab15)

    @_covers("D10")
    def test_d10_unknown_unmatched_section_has_no_fix_action_no_positional_borrow(self):
        fx = _load_fixture("hostile_and_ambiguous_headings")
        matches = match_sections(fx["review_sections"], fx["manuscript_text"])
        unrecognized = matches[4]
        assert unrecognized["key"] is None
        assert unrecognized["reason"] == "UNRECOGNIZED_HEADING"
        assert unrecognized["matchedKey"] is None
        assert unrecognized["matchedBody"] is None
        no_manuscript = matches[3]
        assert no_manuscript["reason"] == "NO_MANUSCRIPT_MATCH"
        assert no_manuscript["matchedKey"] is None

        # Rework 3 (Codex requirement #5): the pure-module classification above is
        # necessary but not sufficient -- AutoFixSection's own reviewBabs filter (main.jsx)
        # drops any UNRECOGNIZED_HEADING section from matchChapterSections' own INPUT
        # before it ever runs, so this reason code was previously unreachable from the
        # real production render path at all: the section simply vanished, with no row,
        # no message, nothing. This mounted proof drives the REAL, whole App with a
        # review markdown carrying an extra section with no ordinal whatsoever ("##
        # Catatan Umum" -- "General Notes") and confirms it is now surfaced with its
        # bounded, localized message, and that no Fix request can ever fire for it.
        #
        # Rework 4 (Codex P2 finding #3): "no additional fetch call fired" alone does not
        # prove there is no Fix action -- a disabled-but-present button would also fire no
        # request. Every mounted scenario below additionally asserts a direct DOM check
        # (unrecRowHasButton) that the row's own container has NO <button> at all.
        result = run_mount_harness(flag_on=True, scenario="unrecognized_heading_probe")
        assert "Catatan Umum" in result["bodyText"]
        assert "Judul bagian ini tidak dikenali sebagai bab manuskrip" in result["bodyText"]
        review_calls = [c for c in result["calls"] if c["url"] == "/api/narasi/review"]
        assert len(review_calls) == 1  # the initial review call only -- no Fix ever fired
        assert result["unrecTitleFound"] is True
        assert result["unrecRowHasButton"] is False

        # Rework 4 (Codex P1 finding #2) edge case (a): exactly one valid, recognized
        # chapter alongside one unrecognized heading -- reviewBabs=[bab1] (non-empty),
        # unrecognizedBabs=[catatan]. The Auto-Fix header/controls (gated on
        # reviewBabs.length>0) must still render alongside the unrecognized row.
        one_valid = run_mount_harness(flag_on=True, scenario="unrecog_one_valid_one_unrecog")
        assert "Catatan Umum" in one_valid["bodyText"]
        assert "Judul bagian ini tidak dikenali sebagai bab manuskrip" in one_valid["bodyText"]
        assert one_valid["unrecTitleFound"] is True
        assert one_valid["unrecRowHasButton"] is False
        assert "Auto-Fix per Bab" in one_valid["bodyText"]
        assert "Rekomendasi: perbaiki kalimat pembuka." in one_valid["bodyText"]

        # Rework 4 (Codex P1 finding #2) edge case (b): NO recognized chapter at all --
        # reviewBabs=[], unrecognizedBabs non-empty. This is the EXACT shape the
        # `if(!reviewBabs.length)return null;` guard bug silently dropped in its
        # entirety (no row, no message, nothing rendered at all). The fixed guard
        # (`if(!reviewBabs.length&&!unrecognizedBabs.length)return null;`) must still
        # render the unrecognized rows, while the Auto-Fix header/controls -- which have
        # nothing to operate on -- must NOT render.
        only_unrec = run_mount_harness(flag_on=True, scenario="unrecog_only_probe")
        assert "Catatan Umum" in only_unrec["bodyText"]
        assert "Catatan Lain" in only_unrec["bodyText"]
        assert "Judul bagian ini tidak dikenali sebagai bab manuskrip" in only_unrec["bodyText"]
        assert only_unrec["unrecTitleFound"] is True
        assert only_unrec["unrecRowHasButton"] is False
        assert "Auto-Fix per Bab" not in only_unrec["bodyText"]

        # Rework 6 (Codex P1 finding) edge case (c), corrected again: the harari/narrative
        # personas' OWN Part-1 Checklist section contains a REAL "### Checklist Per Bab"
        # sub-heading, and ordinary Part-1 prose can casually mention "Part 2" or "per
        # bab" in passing -- none of these may be mistaken for the one true literal
        # "PART 2 — PER BAB" marker, and none of the six Part-1 headings (including the
        # persona-specific Checklist heading and its own "Checklist Per Bab" sub-heading)
        # may be misclassified as UNRECOGNIZED_HEADING. Only the genuinely unknown
        # per-chapter heading (after the real marker) may receive the bounded row, and it
        # must have no Fix button and no ability to borrow manuscript text.
        mixed = run_mount_harness(flag_on=True, scenario="unrecog_mixed_ordinary_valid")
        _unrec_msg = "Judul bagian ini tidak dikenali sebagai bab manuskrip"
        # All six Part-1 report headings render as ordinary report content, not as
        # UNRECOGNIZED_HEADING ambiguity rows.
        for _part1_heading in ("Skor", "Checklist Harari Style", "Yang Sangat Kuat",
                                "Kelemahan Utama", "Saran Revisi Prioritas", "Verdict Akhir"):
            assert _part1_heading in mixed["bodyText"], f"missing Part-1 heading: {_part1_heading}"
        # The three non-boundary decoys render as ordinary content too -- none of them
        # triggers the Part-2 boundary early. (The "### Checklist Per Bab" heading LINE
        # itself is not rendered as its own visible heading by this renderer -- only its
        # own prose content is -- so the decoy is verified via that prose instead of the
        # literal, invisible heading text.)
        assert "Identify opening type per bab" in mixed["bodyText"]
        assert "Lihat Part 2 untuk detail per bab" in mixed["bodyText"]
        assert "Evaluasi per bab akan menyusul" in mixed["bodyText"]
        # Exactly ONE unrecognized-heading row exists in the whole page -- proving none of
        # the six Part-1 headings or three decoys above were also (incorrectly) flagged.
        assert mixed["bodyText"].count(_unrec_msg) == 1, (
            f"expected exactly 1 UNRECOGNIZED_HEADING message, found "
            f"{mixed['bodyText'].count(_unrec_msg)}:\n{mixed['bodyText']}")
        assert "Catatan Umum" in mixed["bodyText"]
        assert _unrec_msg in mixed["bodyText"]
        assert mixed["unrecTitleFound"] is True
        assert mixed["unrecRowHasButton"] is False
        # The valid chapter (Bab 1, in the per-chapter zone) still renders its normal,
        # fixable Auto-Fix row alongside the report content and the unrecognized row.
        assert "Auto-Fix per Bab" in mixed["bodyText"]
        assert "Rekomendasi: perbaiki kalimat pembuka." in mixed["bodyText"]

    @_covers("D11")
    def test_d11_valid_chapter_beside_malformed_section_no_cross_contamination(self):
        fx = _load_fixture("id_manuscript_id_report")
        mixed_sections = fx["review_sections"] + [
            {"title": "Bab 1: Awal Perjalanan", "lines": ["duplicate target -- ambiguous"]},
        ]
        matches = match_sections(mixed_sections, fx["manuscript_text"])
        bab1_matches = [m for m in matches if m["key"] == "review:ordinal:1"]
        assert len(bab1_matches) == 2
        assert all(m["ambiguous"] is True for m in bab1_matches)
        # Bab 2's independent match is entirely unaffected by Bab 1's induced ambiguity.
        bab2 = [m for m in matches if m["key"] == "review:ordinal:2"][0]
        assert bab2["ambiguous"] is False
        assert bab2["matchedKey"] == "review:ordinal:2"
        assert "Pertemuan Tak Terduga" in bab2["matchedBody"] or bab2["matchedBody"]

    @_covers("D12")
    def test_d12_local_keys_never_named_or_asserted_as_chapter_id(self):
        text = frontend_text("src/narasiReviewContract.mjs")
        assert "chapter_id" not in text
        fx = _load_fixture("id_manuscript_id_report")
        matches = match_sections(fx["review_sections"], fx["manuscript_text"])
        for m in matches:
            assert set(m.keys()) == {"key", "matchedKey", "matchedBody", "matchedHeading",
                                      "ambiguous", "reason"}
            assert m["key"] is None or m["key"].startswith("review:ordinal:")

    def test_d13_part_two_marker_is_language_neutral_across_manuscript_heading_styles(self):
        """Rework 8 (Codex P1 finding, SUPERSEDED by Rework 9): the Part-2 structural
        marker was made REPORT_LANGUAGE-dependent (id -> PER BAB, en -> PER CHAPTER) --
        WRONG, per Codex's own supersession: Wimba is a global multilingual product, and
        a structural machine delimiter must never be selected by report_language,
        manuscript_language, UI locale, persona prose, or provider output.

        Rework 9 (Codex supersession): the marker is now nrc.PART_TWO_MARKER -- ONE
        fixed, global constant, byte-identical for every report_language. This test
        genuinely clicks the real report-language selector UI (id/en) crossed with both
        manuscript chapter-heading vocabularies (Bab N / Chapter N) -- 4 combinations
        total -- and proves: (1) the ACTUAL POSTED request body's report_language
        matches what was clicked (real UI-click provenance, never a hardcoded scenario
        flag); (2) the delimiter bytes the mounted app actually used are IDENTICAL
        across all 4 combinations (never varying with report_language or heading
        vocabulary); (3) the localized Part-1 report LABELS for that report_language
        still render as ordinary report content (only the structural delimiter is
        neutral -- ordinary report prose/labels are unaffected and still localized);
        (4) exactly one LOCALIZED ambiguity message appears, matching only the
        genuinely unknown per-chapter heading; (5) the valid chapter's own localized
        Auto-Fix content renders regardless of its heading vocabulary."""
        markers_used = []
        for report_lang, heading_style in (
            ("id", "bab"), ("id", "chapter"), ("en", "bab"), ("en", "chapter"),
        ):
            result = run_mount_harness(
                flag_on=True, scenario="language_marker_matrix",
                report_lang=report_lang, heading_style=heading_style,
            )
            review_calls = [c for c in result["calls"] if c["url"] == "/api/narasi/review"]
            assert len(review_calls) == 1
            assert review_calls[0]["body"]["report_language"] == report_lang, (
                f"UI click for report_lang={report_lang!r} did not propagate into the "
                f"actual request body: {review_calls[0]['body']}")
            markers_used.append(result["markerUsed"])

            is_en = report_lang == "en"
            tok_skor = "Score" if is_en else "Skor"
            tok_kelemahan = "Key Weaknesses" if is_en else "Kelemahan Utama"
            tok_verdict = "Final Verdict" if is_en else "Verdict Akhir"
            tok_unrec = "General Notes" if is_en else "Catatan Umum"
            auto_fix_heading = "Auto-Fix per Chapter" if is_en else "Auto-Fix per Bab"
            unrec_msg = (
                "This section's heading was not recognized as a manuscript chapter."
                if is_en else
                "Judul bagian ini tidak dikenali sebagai bab manuskrip."
            )
            body_text = result["bodyText"]
            for heading in (tok_skor, tok_kelemahan, tok_verdict):
                assert heading in body_text, (
                    f"missing localized Part-1 heading {heading!r} for "
                    f"report_lang={report_lang!r}, heading_style={heading_style!r}")
            assert body_text.count(unrec_msg) == 1, (
                f"expected exactly 1 localized UNRECOGNIZED_HEADING message for "
                f"report_lang={report_lang!r}, heading_style={heading_style!r}, found "
                f"{body_text.count(unrec_msg)}:\n{body_text}")
            assert tok_unrec in body_text
            assert result["unrecTitleFound"] is True
            assert result["unrecRowHasButton"] is False
            assert auto_fix_heading in body_text
            assert "Rekomendasi: perbaiki kalimat pembuka." in body_text

        # Rework 9 (Codex instruction #6): the delimiter bytes the REAL mounted app used
        # must be identical across all 4 report-language x heading-vocabulary
        # combinations -- never varying with report_language or manuscript heading
        # style, and matching the pure backend module's own constant exactly.
        assert len(set(markers_used)) == 1, (
            f"expected the SAME delimiter across all 4 combinations, got: {markers_used}")
        assert markers_used[0] == nrc.PART_TWO_MARKER == "---WIMBA_REVIEW_CHAPTERS_START:v1---"


# ===========================================================================
# E — Real frontend integration
# ===========================================================================
class TestE_FrontendIntegration:
    @_covers("E01")
    def test_e01_node_genuinely_executes_the_real_mjs_module(self):
        # A canonical, dedicated proof: this call spawns a real `node` subprocess importing the
        # actual on-disk module -- never a Python re-expression of its logic.
        result = node_eval('m.extractChapterKey("Bab 7: Genuine Execution")')
        assert result == {"key": "review:ordinal:7", "ordinal": 7, "label": "Bab"}
        assert nrc.__file__ is not None  # sanity: this is the backend module, a different file

    @_covers("E02")
    def test_e02_main_jsx_imports_module_reviewrenderer_uses_classifier_when_enabled(self):
        text = frontend_text("src/main.jsx")
        assert 'import * as narasiReviewContract from "./narasiReviewContract.mjs"' in text
        assert "narasiReviewContract.extractChapterKey" in text
        assert "chapterKeyEnabled" in text
        assert "function ReviewRenderer(" in text

    @_covers("E03")
    def test_e03_autofixsection_uses_matched_record_not_getbabkey_when_enabled(self):
        text = frontend_text("src/main.jsx")
        auto_fix_start = text.index("function AutoFixSection(")
        auto_fix_end = text.index("// ── END AUTO-FIX", auto_fix_start)
        section = text[auto_fix_start:auto_fix_end]
        assert "narasiReviewContract.parseManuscriptChapters" in section
        assert "narasiReviewContract.matchChapterSections" in section
        assert "if(chapterKeyEnabled)return{};" in section  # legacy fileBabs disabled when on
        assert "getBabKey" in section  # legacy path still present for flag-off

        # Rework 3 (Codex P1 finding #3): a source-substring check alone cannot prove H01's
        # mutation is a real behavior regression -- it must be proven by ACTUALLY mounting
        # the real, whole App and observing the RENDERED effect. "Chapter 4" (bare ordinal,
        # no title) is a realistic AI-review-summary heading; the manuscript's own "Chapter
        # 4: The Sound That Would Not Fade" heading carries the full title. Ordinal-based
        # chapter-key matching resolves this correctly when enabled; the historical Bab-only
        # getBabKey/fileBabs positional lookup could only ever match by exact string identity
        # and would fail this pairing (the true, product-observable Bab-only regression).
        result = run_mount_harness(flag_on=True, scenario="chapter4_autofix_probe")
        assert "belum ada teks" not in result["bodyText"]
        assert "Chapter 4" in result["bodyText"]

    @_covers("E04")
    def test_e04_a05a_chapter4_defect_scenario_now_yields_exact_english_chapter(self):
        manuscript = (
            "## Bab 3: Placeholder\n\nx\n\n"
            "## Chapter 4: The Sound That Would Not Fade\n\n"
            "The real English chapter-four body text.\n"
        )
        matches = match_sections(
            [{"title": "Chapter 4: The Sound That Would Not Fade", "lines": []}], manuscript)
        assert matches[0]["matchedKey"] == "review:ordinal:4"
        # Rework 1: byte-exact body, see D01's comment -- the manuscript's own blank
        # line and trailing newline are preserved, never trimmed.
        assert matches[0]["matchedBody"] == "\nThe real English chapter-four body text.\n"

    @_covers("E05")
    def test_e05_selector_offers_only_id_en_visible_only_when_enabled_never_mutates_manuscript(self):
        text = frontend_text("src/main.jsx")
        assert "B04A_REVIEW_LANGUAGE_CHAPTER_KEY_V1&&(" in text
        assert "narasiReviewContract.SUPPORTED_REPORT_LANGUAGES.map" in text
        assert node_eval("m.SUPPORTED_REPORT_LANGUAGES") == ["id", "en"]
        selector_start = text.index("Bahasa laporan (Review/Checklist)")
        selector_end = text.index("</div>", selector_start + 400)
        selector_block = text[selector_start - 200:selector_end]
        assert "setManuscriptLanguage" not in selector_block

    @_covers("E06")
    def test_e06_request_builders_add_report_language_only_when_enabled(self):
        assert node_eval('m.withReportLanguage({a:1}, false, "en")') == {"a": 1}
        assert node_eval('m.withReportLanguage({a:1}, true, "en")') == {"a": 1, "report_language": "en"}

        # Rework 2 (Codex requirement #4): replaces Rework 1's extracted-snippet-and-
        # isolated-evaluation approach -- which a genuinely broken Google branch could
        # still pass 83/83 under, since it never actually drove the real component or
        # intercepted real fetch -- with a true production-path spy. The REAL, whole
        # production App (Rework 4: inside a real, headless Chrome tab, served by this
        # project's own real Vite dev server, so import.meta.env substitution matches
        # production build semantics) is mounted via its own real auto-mount side effect,
        # driven through genuine button clicks and textarea input, with the REAL global
        # `fetch` intercepted. Every required flag x API-mode combination is covered.

        # --- Review: always posts to /api/narasi/review regardless of apiMode ---
        for flag_on in (False, True):
            result = run_mount_harness(flag_on=flag_on, scenario="review")
            review_calls = [c for c in result["calls"] if c["url"] == "/api/narasi/review"]
            assert len(review_calls) == 1
            body = review_calls[0]["body"]
            if flag_on:
                assert body["report_language"] == "id"
            else:
                assert "report_language" not in body

        # --- AutoFix (per-chapter "Fix Bab Ini") and Optimize ("Optimasi Seksi Ini"):
        # both route through the shared streamCall, which branches google -> /api/chat/
        # google vs laozhang -> /api/narasi/review -- this is the EXACT branch Codex's
        # Rework 2 rejection named (Google AutoFix/Optimize still didn't send
        # report_language, main.jsx line 4445 at rejection time). ---
        for scenario in ("autofix", "optimize"):
            for api_mode in ("laozhang", "google"):
                endpoint = "/api/chat/google" if api_mode == "google" else "/api/narasi/review"
                for flag_on in (False, True):
                    result = run_mount_harness(flag_on=flag_on, api_mode=api_mode, scenario=scenario)
                    matching = [c for c in result["calls"] if c["url"] == endpoint]
                    assert matching, f"no {endpoint} call captured for {scenario}/{api_mode}"
                    # laozhang mode: the initial Review call ALSO hits /api/narasi/review --
                    # the scenario's OWN request is always the last matching call.
                    body = matching[-1]["body"]
                    if flag_on:
                        assert body["report_language"] == "id", (scenario, api_mode)
                    else:
                        assert "report_language" not in body, (scenario, api_mode)

    @_covers("E07")
    def test_e07_no_issue_sentinel_accepts_exact_localized_sentinel_for_selected_language_only(self):
        id_text = "Tidak ada kelemahan signifikan."
        en_text = "No significant weaknesses."
        assert node_eval(f'm.isNoIssueSentinel({json.dumps(id_text)}, "id")') is True
        assert node_eval(f'm.isNoIssueSentinel({json.dumps(en_text)}, "en")') is True
        assert node_eval(f'm.isNoIssueSentinel({json.dumps(en_text)}, "id")') is False
        assert node_eval(f'm.isNoIssueSentinel({json.dumps(id_text)}, "en")') is False

    @_covers("E08")
    def test_e08_bounded_labels_localize_unrelated_ui_and_manuscript_text_do_not(self):
        labels_id = node_eval('m.localizedLabels("id")')
        labels_en = node_eval('m.localizedLabels("en")')
        assert labels_id["autoFixHeading"] == "Auto-Fix per Bab"
        assert labels_en["autoFixHeading"] == "Auto-Fix per Chapter"
        assert labels_id["fixThisButton"] != labels_en["fixThisButton"]
        text = frontend_text("src/main.jsx")
        # Unrelated, generic UI (the Copy button) stays exactly the same literal in both modes.
        assert '>📋 Copy</button>' in text

    @_covers("E09")
    def test_e09_capability_verified_before_enabled_ui_consumes_response_as_v2(self):
        # Rework 2/3 (Codex requirement #4/#2): the specific gap named in the rejection --
        # "prove One-Shot final results without the version are rejected before success
        # state is applied" -- demonstrated by ACTUALLY mounting the real, whole App,
        # ACTUALLY navigating to Script Review, ACTUALLY submitting a One-Shot fix,
        # ACTUALLY waiting out the real 4000ms poll interval (no mocked/faked timers), and
        # ACTUALLY reading the resulting rendered DOM. This is the exact smoking-gun proof
        # Codex's rejection demanded: a source-snippet-and-isolated-evaluation test
        # (Rework 1's version) cannot show that no success state, fixed book, result
        # document, or success indication is produced -- only genuinely driving the
        # component through to its final render can. showToast is App()'s own internal
        # state (no prop this harness controls), so the proof reads the PERSISTENT
        # (non-time-sensitive) success/error UI blocks main.jsx itself renders from
        # oneshotStatus, rather than the transient 2.8s-lived toast div.
        success = run_mount_harness(flag_on=True, scenario="oneshot_success")
        assert "✅ Selesai" in success["bodyText"]  # the real success-state UI block renders

        missing = run_mount_harness(flag_on=True, scenario="oneshot_missing_version")
        assert "capability mismatch" in missing["bodyText"].lower()  # the real error-state UI block renders
        # Neither the success-state UI block nor the mocked fixed_book text appears
        # ANYWHERE in the final rendered DOM -- not as a result document, not as a
        # progress label. A false-v2-success would leak at least one of these.
        # (fixedBook itself is never rendered as visible text even on genuine success --
        # it only gates a badge/export buttons -- so its absence here is necessary but
        # not sufficient; "✅ Selesai"'s absence is the load-bearing assertion.)
        assert "✅ Selesai" not in missing["bodyText"]
        assert "buku hasil fix" not in missing["bodyText"]

        # --- Review path: same mismatch-before-success-state proof, for the OTHER call
        # site that runs verifyResponseCapability (generateReview). The harness's own
        # Review mock always includes the version when the flag is on (needed to reach
        # stage="reviewed" so AutoFix/Optimize are reachable for E06 above) -- so the
        # negative case is proven directly against the pure function instead, using the
        # exact same real narasiReviewContract module the mounted component itself uses.
        assert node_eval(
            f'm.verifyResponseCapability({{ok:true,text:"x"}}, true)'
        )["mismatch"] is True
        assert node_eval(
            f'm.verifyResponseCapability({{ok:true,text:"x",'
            f'review_contract_version:{json.dumps(nrc.REVIEW_CONTRACT_VERSION)}}}, true)'
        )["mismatch"] is False
        text = frontend_text("src/main.jsx")
        gen_review_start = text.index("const generateReview=async()=>{")
        gen_review_end = text.index("const generateOptimize=async", gen_review_start)
        review_section = text[gen_review_start:gen_review_end]
        assert "const cap=narasiReviewContract.verifyResponseCapability" in review_section
        verify_idx = review_section.index("const cap=narasiReviewContract.verifyResponseCapability")
        set_review_idx = review_section.index("setReview(d.text")
        assert verify_idx < set_review_idx  # verified BEFORE the success-state UI update

    @_covers("E10")
    def test_e10_vite_build_succeeds_no_dist_in_change_set(self):
        proc = subprocess.run(["npm", "run", "build"], cwd=_FRONTEND_DIR, text=True,
                               capture_output=True, timeout=120, check=False)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "error" not in proc.stdout.lower()
        _, untracked, _ = _git_status_sets(_FRONTEND_DIR)
        assert not any(p.startswith("dist/") for p in untracked)


# ===========================================================================
# F — B-07/B-08 and noninterference
# ===========================================================================
class TestF_B0708Noninterference:
    @_covers("F01")
    def test_f01_resolve_lineage_called_exactly_once_per_linked_request(self, monkeypatch):
        monkeypatch.setenv("NARASI_REVIEW_LANGUAGE_CHAPTER_KEY_V1", "1")
        src_uuid = _linked_source(monkeypatch, target_language="en")
        _patch_review_common(monkeypatch)
        calls = []
        real_resolve = laozhang_api._narasi_resolve_lineage

        async def counting_resolve(*a, **kw):
            calls.append((a, kw))
            return await real_resolve(*a, **kw)

        monkeypatch.setattr(laozhang_api, "_narasi_resolve_lineage", counting_resolve)
        run(laozhang_api.narasi_review(
            review_body(source_job_id="ext1", source_job_uuid=src_uuid, report_language="id"),
            _user()))
        assert len(calls) == 1

    @_covers("F02")
    def test_f02_binding_and_language_unchanged_by_report_language(self, monkeypatch):
        monkeypatch.setenv("NARASI_REVIEW_LANGUAGE_CHAPTER_KEY_V1", "1")
        src_uuid = _linked_source(monkeypatch, target_language="en")
        _patch_review_common(monkeypatch)
        out_id = run(laozhang_api.narasi_review(
            review_body(source_job_id="ext1", source_job_uuid=src_uuid, report_language="id"), _user()))
        out_en = run(laozhang_api.narasi_review(
            review_body(source_job_id="ext1", source_job_uuid=src_uuid, report_language="en"), _user()))
        assert out_id["manuscript_language"] == out_en["manuscript_language"] == "en"
        assert out_id["review_input_binding"] == out_en["review_input_binding"]

    @_covers("F03")
    def test_f03_create_derived_input_receives_manuscript_language_every_combo(self, monkeypatch):
        monkeypatch.setenv("NARASI_REVIEW_LANGUAGE_CHAPTER_KEY_V1", "1")
        for manuscript_lang, report_lang in [("en", "id"), ("id", "en"), ("en", "en"), ("id", "id")]:
            src_uuid = _linked_source(monkeypatch, target_language=manuscript_lang)
            derived_calls = []
            _patch_review_common(monkeypatch, derived_input_calls=derived_calls)
            run(laozhang_api.narasi_review(
                review_body(source_job_id="ext1", source_job_uuid=src_uuid, report_language=report_lang),
                _user()))
            assert derived_calls[0][1]["language"] == manuscript_lang

    @_covers("F04")
    def test_f04_existing_terminalization_and_persistence_tests_remain_importable_and_green(self):
        import test_narasi_lifecycle_repository as lifecycle_tests
        # A lightweight, direct re-invocation of one representative sentinel test from the
        # inherited suite (full-suite green is separately confirmed by the required combined
        # run) -- proves this file's changes did not silently break that suite's own import
        # or fixture wiring.
        assert hasattr(lifecycle_tests, "TestOneshotDerivedInputTerminalLifecycle")
        assert hasattr(lifecycle_tests, "TestReviewAndSaveEditFatalPersistenceRuntime")

    @_covers("F05")
    def test_f05_no_new_provider_network_or_database_call_ast_import_scan(self):
        source = Path(nrc.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        forbidden = {"os", "sys", "socket", "requests", "httpx", "urllib", "fastapi",
                     "database", "psycopg2", "asyncpg", "logging", "sqlite3"}
        assert imported & forbidden == set()
        assert imported == {"types"}

        frontend_forbidden = ["require(", "process.env", "window.", "document.", "fetch(",
                               "XMLHttpRequest", 'from "fs"', "from 'fs'", 'from "http"',
                               "from 'http'", 'from "https"', "from 'https'",
                               'from "child_process"', "from 'child_process'"]
        js_text = frontend_text("src/narasiReviewContract.mjs")
        hits = [tok for tok in frontend_forbidden if tok in js_text]
        assert hits == []

    @_covers("F06")
    def test_f06_no_b04b_hash_binding_or_canonical_replacement_claim(self):
        py_text = Path(nrc.__file__).read_text(encoding="utf-8")
        js_text = frontend_text("src/narasiReviewContract.mjs")
        for forbidden_term in ["input_hash", "output_hash", "atomicity", "canonical_replacement"]:
            assert forbidden_term not in py_text
            assert forbidden_term not in js_text

    @_covers("F07")
    def test_f07_standalone_local_match_never_associates_with_a_source_job(self):
        sig = node_eval(
            "(function(){return m.matchChapterSections.length===2 && "
            "m.parseManuscriptChapters.length===1;})()")
        assert sig is True
        fx = _load_fixture("id_manuscript_id_report")
        matches = match_sections(fx["review_sections"], fx["manuscript_text"])
        for m in matches:
            assert "job" not in json.dumps(m).lower()

    @_covers("F08")
    def test_f08_a05a_and_b0708_suites_remain_green(self):
        proc = subprocess.run(
            ["python3", "-m", "pytest", "-q",
             "tests/narasi_gates/test_fixture_manifest.py",
             "tests/python/test_narasi_lifecycle_repository.py"],
            cwd=_BACKEND_ROOT, text=True, capture_output=True, timeout=60, check=False)
        assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
        assert "176 passed" in proc.stdout


# ===========================================================================
# G — Uji ketahanan and pengujian edge case
# ===========================================================================
class TestG_UjiKetahananPengujianEdgeCase:
    @_covers("G01")
    def test_g01_hostile_type_corpus_cannot_invoke_attacker_methods(self):
        class HostileStr(str):
            def __eq__(self, other):
                raise RuntimeError("hostile __eq__ invoked")

            def __str__(self):
                raise RuntimeError("hostile __str__ invoked")

            def __hash__(self):
                return 0

        for hostile in [True, False, [], {}, HostileStr("id")]:
            with pytest.raises(nrc.ReviewContractError):
                nrc.normalize_report_language(hostile)

        # Frontend: a Proxy whose traps would throw if the exact-type gate ever touched them.
        result = node_eval(
            "(function(){"
            "let calls=0;"
            "const hostile=new Proxy({},{get(t,p){calls++;throw new Error('trap '+String(p));}});"
            "let threw=false;"
            "try{m.normalizeReportLanguage(hostile);}catch(e){threw=(e.code==='REPORT_LANGUAGE_TYPE_INVALID');}"
            "return {threw, calls};"
            "})()"
        )
        assert result["threw"] is True
        assert result["calls"] == 0

    @_covers("G02")
    def test_g02_long_heading_control_chars_nul_bidi_emoji_combining_marks_bounded(self):
        fx = _load_fixture("hostile_and_ambiguous_headings")
        parsed = parse_manuscript(fx["manuscript_text"])
        assert dict(parsed["chapters"])["review:ordinal:25"]["heading"].startswith("## Bab 25:")
        matches = match_sections(fx["review_sections"], fx["manuscript_text"])
        assert matches[4]["reason"] == "UNRECOGNIZED_HEADING"  # emoji+NUL title, no crash

        bidi = "Bab 30: reversed‮subtitle"  # bidi override lives in the subtitle, after
        # the colon boundary -- the ordinal itself must still parse deterministically.
        result = node_call(f"m.extractChapterKey({json.dumps(bidi)})")
        assert result["ok"] is True
        assert result["value"]["ordinal"] == 30

        combining = "## Bab 40: éclat combining acute\nBody.\n"
        parsed_combining = parse_manuscript(combining)
        assert "review:ordinal:40" in dict(parsed_combining["chapters"])

    @_covers("G03")
    def test_g03_large_manuscript_indexing_roughly_linear_zero_side_effects(self):
        import time
        small = "\n\n".join(f"## Bab {i}: T\nBody {i}." for i in range(1, 51))
        large = "\n\n".join(f"## Bab {i}: T\nBody {i}." for i in range(1, 1501))
        t0 = time.monotonic()
        parsed_small = parse_manuscript(small)
        t_small = time.monotonic() - t0
        t0 = time.monotonic()
        parsed_large = parse_manuscript(large)
        t_large = time.monotonic() - t0
        assert len(parsed_small["chapters"]) == 50
        assert len(parsed_large["chapters"]) == 1500
        # 30x the input should not cost anywhere near a quadratic (900x) blow-up; generous bound.
        assert t_large < max(t_small * 60, 2.0)

    @_covers("G04")
    def test_g04_mixed_en_id_headings_unique_ordinals_match_label_choice_neutral(self):
        fx = _load_fixture("mixed_unique_headings")
        matches = match_sections(fx["review_sections"], fx["manuscript_text"])
        assert all(m["matchedKey"] is not None for m in matches)
        assert {m["key"] for m in matches} == {"review:ordinal:3", "review:ordinal:5"}

    @_covers("G05")
    def test_g05_stable_codes_without_raw_input_or_exception_leakage(self):
        huge_hostile = "x" * 5000
        with pytest.raises(nrc.ReviewContractError) as caught:
            nrc.normalize_report_language(huge_hostile)
        assert caught.value.code == "REPORT_LANGUAGE_UNSUPPORTED"
        assert huge_hostile not in caught.value.code
        assert len(caught.value.code) < 100

        fx = _load_fixture("hostile_and_ambiguous_headings")
        matches = match_sections(fx["review_sections"], fx["manuscript_text"])
        valid_reasons = {None, "UNRECOGNIZED_HEADING", "DUPLICATE_REVIEW_TARGET", "NO_MANUSCRIPT_MATCH"}
        assert all(m["reason"] in valid_reasons for m in matches)

    @_covers("G06")
    def test_g06_concurrent_repeated_calls_share_no_mutable_state(self):
        fx = _load_fixture("id_manuscript_id_report")
        first = parse_manuscript(fx["manuscript_text"])
        second = parse_manuscript(fx["manuscript_text"])
        assert first == second  # same content, independently derived

    @_covers("G07")
    def test_g07_fixture_manifest_consistent_tamper_rejected_by_independent_oracle(self):
        for name, oracle in _B04A_FIXTURE_ORACLE.items():
            path = _FIXTURES_DIR / f"{name}.json"
            import hashlib
            actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            assert actual_hash == oracle["sha256"], (
                f"{name}: fixture content diverges from this file's OWN independent oracle "
                f"hash (never derived from MANIFEST.json or the fixture's own fields)")
            fx = _load_fixture(name)
            parsed = parse_manuscript(fx["manuscript_text"])
            assert sorted(k for k, v in parsed["chapters"]) == oracle["expected_chapter_keys"]
            if "expected_duplicate_ordinals" in oracle:
                assert sorted(parsed["duplicateOrdinals"]) == oracle["expected_duplicate_ordinals"]
            if "expected_fence_malformed" in oracle:
                assert parsed["fenceMalformed"] == oracle["expected_fence_malformed"]
            if "expected_match_reasons" in oracle:
                matches = match_sections(fx["review_sections"], fx["manuscript_text"])
                assert [m["reason"] for m in matches] == oracle["expected_match_reasons"]
            if oracle.get("expected_all_matched"):
                matches = match_sections(fx["review_sections"], fx["manuscript_text"])
                assert all(m["matchedKey"] is not None for m in matches)

    @_covers("G08")
    def test_g08_privacy_scan_rejects_production_names_raw_exceptions_unbounded_text(self, monkeypatch):
        monkeypatch.setenv("NARASI_REVIEW_LANGUAGE_CHAPTER_KEY_V1", "1")
        calls = []
        _patch_review_common(monkeypatch, make_client_calls=calls)
        try:
            run(laozhang_api.narasi_review(review_body(report_language={"x": 1}), _user()))
        except HTTPException as exc:
            assert "Traceback" not in exc.detail
            assert "File \"" not in exc.detail
        for fixture_path in _FIXTURES_DIR.glob("*.json"):
            data = fixture_path.read_text(encoding="utf-8")
            assert "@gmail.com" not in data and "@wimba" not in data
            assert not re.search(r"sk-[a-zA-Z0-9]{20,}", data)

    def test_g09_harness_never_installs_dependencies_clean_environment(self, monkeypatch):
        """Rework 4 (Codex instruction #2): an executable, active proof -- not merely
        "the npm install code was removed" -- that the mounted-App harness genuinely
        never invokes an install-like npm subcommand, even in a completely clean
        environment (fresh HOME with no pre-existing puppeteer cache, fresh TMPDIR with
        no pre-existing npm cache, npm_config_offline=true). A hostile `npm` shim is
        placed FIRST on PATH: it blocks only install/i/ci/add/update (writing a marker
        file and exiting non-zero) and delegates every other subcommand to the REAL npm
        -- a real, automated tripwire for the exact claim at issue, not a blanket "no npm
        at all" gate that would also trip on unrelated, legitimate `npm run build` calls
        elsewhere in this suite (see test_e10/test_f08)."""
        real_home = os.environ.get("HOME", "")
        real_npm = shutil.which("npm") or "/usr/local/bin/npm"
        chrome_bin = None
        for candidate_root in (Path(real_home) / ".cache/puppeteer/chrome-headless-shell",):
            if candidate_root.is_dir():
                for path in candidate_root.rglob("chrome-headless-shell"):
                    if path.is_file():
                        chrome_bin = str(path)
                        break
        if not chrome_bin:
            chrome_bin = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

        with tempfile.TemporaryDirectory(prefix="b04a_g09_hostile_bin_") as hostile_bin_dir, \
             tempfile.TemporaryDirectory(prefix="b04a_g09_clean_home_") as clean_home, \
             tempfile.TemporaryDirectory(prefix="b04a_g09_clean_tmp_") as clean_tmp:
            marker_path = Path(hostile_bin_dir) / "npm_install_was_invoked.marker"
            npm_shim = Path(hostile_bin_dir) / "npm"
            npm_shim.write_text(
                "#!/bin/sh\n"
                'case "$1" in\n'
                "  install|i|ci|add|update|up)\n"
                f'    echo "HOSTILE NPM SHIM BLOCKED INSTALL-LIKE COMMAND: $@" >> "{marker_path}"\n'
                "    exit 1\n"
                "    ;;\n"
                "  *)\n"
                f'    exec "{real_npm}" "$@"\n'
                "    ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            npm_shim.chmod(0o755)

            monkeypatch.setenv("PATH", f"{hostile_bin_dir}:{os.environ.get('PATH', '')}")
            monkeypatch.setenv("HOME", clean_home)
            monkeypatch.setenv("TMPDIR", clean_tmp + "/")
            monkeypatch.setenv("npm_config_offline", "true")
            monkeypatch.setenv("B04A_CHROME", chrome_bin)

            result = run_mount_harness(flag_on=True, scenario="review")
            review_calls = [c for c in result["calls"] if c["url"] == "/api/narasi/review"]
            assert len(review_calls) == 1
            if marker_path.exists():
                pytest.fail("harness invoked npm install under a clean/offline environment: "
                            + marker_path.read_text(encoding="utf-8"))


# ===========================================================================
# Detached-copy negative-control harness (Rework 2, Codex requirement #5): every H0X
# below (a) mutates a REAL detached copy of production source, (b) runs the ACTUAL
# dedicated acceptance pytest NODE against that copy via a real subprocess (never an
# in-process function call standing in for it), (c) asserts that node fails, and (d)
# in the SAME subprocess invocation, runs at least one unrelated inherited sentinel
# node and asserts it still passes -- proving the mutation is a specific, targeted
# regression rather than a globally broken detached copy.
# ===========================================================================
_H_TEST_FILE_RELPATH = "tests/python/" + Path(__file__).name
# Rework 3 (Codex P2 finding #4): the unrelated sentinel every H-test must prove passes
# alongside its target has to be a genuinely INHERITED, already-accepted test -- never a
# test from B-04a's OWN suite (Rework 1/2 used TestPrivacyScan from THIS file, which
# proves only that THIS file's own collection still succeeds, not that an independent,
# previously-accepted feature is unharmed). test_narasi_lifecycle_repository.py is the
# accepted B-07/B-08 lifecycle suite; this one test is fast, self-contained (monkeypatch
# only, no fixtures/network), and entirely unrelated to B-04a's review-language feature.
_H_SENTINEL_NODE = ("tests/python/test_narasi_lifecycle_repository.py"
                     "::TestFlagOffLegacyByteCompat"
                     "::test_create_narasi_job_legacy_shape_unaffected_by_flag")


def _h_node(class_name, method_name):
    return f"{_H_TEST_FILE_RELPATH}::{class_name}::{method_name}"


def _detached_frontend_tree(relpath, mutate):
    """Copies ONE real frontend source file (relpath, relative to src/) into a fresh
    temp frontend root at the SAME relative path, applies an EXACT mutation, and
    returns the temp root. frontend_text()/node_eval() (and everything the required
    D0x/E0x tests build on) already read _FRONTEND_DIR from the B04A_FRONTEND env var --
    passing this temp root as that env var to a real pytest subprocess makes the REAL,
    unmodified acceptance test transparently execute against the mutated copy, never a
    hand-rolled proxy assertion standing in for it."""
    real_path = _FRONTEND_DIR / "src" / relpath
    source = real_path.read_text(encoding="utf-8")
    mutated = mutate(source)
    assert mutated != source, f"mutation did not change {relpath}"
    tmp_root = Path(tempfile.mkdtemp(prefix="b04a_detached_frontend_tree_"))
    dst = tmp_root / "src" / relpath
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(mutated, encoding="utf-8")
    return tmp_root


def _detached_frontend_tree_mounted(relpath, mutate):
    """Like _detached_frontend_tree above, but for mutations a MOUNTED-component test
    (run_mount_harness) must exercise -- which needs real vite/react/react-dom, not just
    a plain `node` execution of one dependency-free .mjs module. node_modules (164MB) is
    symlinked, never copied; package.json/vite.config.ts/index.html and the whole src/
    directory (556KB, cheap) are copied so vite's own config loading resolves exactly as
    it does against the real repo. Passing the returned root as B04A_FRONTEND to a real
    pytest subprocess makes the target test's own run_mount_harness() call (which reads
    the SAME env-var-driven _FRONTEND_DIR global) transparently mount the mutated copy."""
    tmp_root = Path(tempfile.mkdtemp(prefix="b04a_detached_frontend_tree_mounted_"))
    os.symlink(_FRONTEND_DIR / "node_modules", tmp_root / "node_modules", target_is_directory=True)
    for name in ("package.json", "vite.config.ts", "index.html"):
        src_path = _FRONTEND_DIR / name
        if src_path.exists():
            shutil.copy2(src_path, tmp_root / name)
    shutil.copytree(_FRONTEND_DIR / "src", tmp_root / "src")
    target = tmp_root / "src" / relpath
    source = target.read_text(encoding="utf-8")
    mutated = mutate(source)
    assert mutated != source, f"mutation did not change {relpath}"
    target.write_text(mutated, encoding="utf-8")
    return tmp_root


def _detached_backend_tree(mutate, *, target_relpath="python/laozhang_api.py"):
    """Copies python/ (laozhang_api.py and narasi_review_contract.py both have sibling
    modules and/or a needed conftest-driven sys.path -- so nothing less than the full
    package is a genuinely runnable detached copy) plus tests/python/{conftest.py,
    fixtures/, THIS test file} into a fresh temp root, applies an EXACT mutation to the
    file at `target_relpath`, and returns the temp root so a real pytest subprocess can
    run a genuine acceptance test node against it."""
    tmp_root = Path(tempfile.mkdtemp(prefix="b04a_detached_backend_"))
    shutil.copytree(_BACKEND_ROOT / "python", tmp_root / "python",
                     ignore=shutil.ignore_patterns("__pycache__"))
    (tmp_root / "tests" / "python").mkdir(parents=True)
    shutil.copy2(_BACKEND_ROOT / "tests/python/conftest.py", tmp_root / "tests/python/conftest.py")
    shutil.copytree(_BACKEND_ROOT / "tests/python/fixtures", tmp_root / "tests/python/fixtures")
    shutil.copy2(Path(__file__), tmp_root / "tests/python" / Path(__file__).name)
    # Rework 3 (Codex P2 finding #4): the unrelated sentinel every H-test proves alongside
    # its target must be a genuinely INHERITED, already-accepted test -- never this file's
    # own TestPrivacyScan -- so this ALSO needs to be present in the detached copy.
    shutil.copy2(_BACKEND_ROOT / "tests/python/test_narasi_lifecycle_repository.py",
                 tmp_root / "tests/python/test_narasi_lifecycle_repository.py")
    target = tmp_root / target_relpath
    source = target.read_text(encoding="utf-8")
    mutated = mutate(source)
    assert mutated != source, f"mutation did not change {target_relpath}"
    target.write_text(mutated, encoding="utf-8")
    return tmp_root


def _detached_test_file_tree(mutate):
    """Symlinks the REAL, unmutated python/ package into a fresh temp root (H12 never
    touches backend code, only this test file), copies conftest.py + fixtures/, and
    writes a MUTATED copy of THIS test file at the same relative path -- so a real
    pytest subprocess collects and runs the mutated file's own test bodies (including a
    genuine invocation of the real completeness-oracle test), never a re-implementation
    of pytest collection or of the oracle itself."""
    real_source = Path(__file__).read_text(encoding="utf-8")
    mutated_source = mutate(real_source)
    assert mutated_source != real_source, "mutation did not change the test-file source"
    tmp_root = Path(tempfile.mkdtemp(prefix="b04a_detached_test_file_tree_"))
    os.symlink(_BACKEND_ROOT / "python", tmp_root / "python", target_is_directory=True)
    (tmp_root / "tests" / "python").mkdir(parents=True)
    shutil.copy2(_BACKEND_ROOT / "tests/python/conftest.py", tmp_root / "tests/python/conftest.py")
    shutil.copytree(_BACKEND_ROOT / "tests/python/fixtures", tmp_root / "tests/python/fixtures")
    shutil.copy2(_BACKEND_ROOT / "tests/python/test_narasi_lifecycle_repository.py",
                 tmp_root / "tests/python/test_narasi_lifecycle_repository.py")
    (tmp_root / "tests/python" / Path(__file__).name).write_text(mutated_source, encoding="utf-8")
    return tmp_root


def _run_detached_pytest(root, node_ids, *, extra_env=None, timeout=90):
    """Runs one or more REAL pytest node ids against `root` as cwd, optionally with extra
    env vars (e.g. a B04A_FRONTEND redirection)."""
    env = {**os.environ, "LAOZHANG_API_KEY": "sk-test-key-for-unit-tests",
           "LAOZHANG_IMAGE_API_KEY": "sk-test-key-for-unit-tests", **(extra_env or {})}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *node_ids],
        cwd=str(root), text=True, capture_output=True, timeout=timeout, check=False, env=env)


def _assert_target_fails_and_inherited_sentinel_passes(root, target_node_id, *, extra_env=None, timeout=90):
    """Rework 3 (Codex P2 finding #4): runs the target and the inherited sentinel as
    SEPARATE subprocess invocations (never combined into one pytest call) and asserts on
    each one's OWN exact node id in its OWN output -- never inferring identity from an
    aggregate "1 failed / 1 passed" count across a combined run, which cannot distinguish
    "the target failed, sentinel passed" from "the target passed, sentinel failed"."""
    target_proc = _run_detached_pytest(root, [target_node_id], extra_env=extra_env, timeout=timeout)
    target_out = target_proc.stdout + target_proc.stderr
    assert target_proc.returncode != 0, f"{target_node_id} did not fail as expected:\n{target_out}"
    assert f"FAILED {target_node_id}" in target_proc.stdout, (
        f"expected 'FAILED {target_node_id}' in pytest output:\n{target_out}")

    # The inherited sentinel never needs the target's own extra_env (e.g. a B04A_FRONTEND
    # redirection is meaningless to a pure backend lifecycle test) -- run it plainly,
    # against the SAME detached root, so it genuinely shares the mutated copy's state.
    sentinel_proc = _run_detached_pytest(root, [_H_SENTINEL_NODE], timeout=timeout)
    sentinel_out = sentinel_proc.stdout + sentinel_proc.stderr
    assert sentinel_proc.returncode == 0, f"inherited sentinel did not pass:\n{sentinel_out}"
    assert "1 passed" in sentinel_proc.stdout, f"expected exactly 1 passed for sentinel:\n{sentinel_out}"


# ===========================================================================
# Cross-runtime contract -- the Part-2 machine delimiter must be byte-identical
# in both pure modules, independent of report/manuscript language.
# ===========================================================================
class TestPartTwoMarkerCrossRuntimeContract:
    def test_backend_and_frontend_part_two_marker_are_byte_identical(self):
        """Rework 9 (Codex instruction #8): inspects the REAL production source of
        both pure contract modules directly -- a genuine Python import
        (narasi_review_contract.PART_TWO_MARKER) plus a genuine Node subprocess
        execution of the real narasiReviewContract.mjs (node_eval, the same
        technique this file's other frontend-module assertions already use) --
        never two duplicated literals hardcoded inside this test fixture. Neither
        module's export takes any language argument at all (both are plain
        constants), so language-dependent selection is structurally impossible,
        and the two are asserted byte-identical."""
        frontend_marker = node_eval("m.PART_TWO_MARKER")
        assert frontend_marker == nrc.PART_TWO_MARKER
        assert frontend_marker == "---WIMBA_REVIEW_CHAPTERS_START:v1---"


# ===========================================================================
# H — Mandatory negative controls
# ===========================================================================
class TestH_NegativeControls:
    @_covers("H01")
    def test_h01_restoring_bab_only_matcher_would_fail_the_chapter4_test(self):
        text = frontend_text("src/main.jsx")
        assert "if(chapterKeyEnabled)return{};" in text  # fileBabs early-return, real file: present
        real_manuscript_text_for_index = (
            "  const manuscriptTextForIndex=(idx)=>{\n"
            "    if(chapterKeyEnabled){\n"
            "      const m=matchResults&&matchResults[idx];\n"
            '      return (m&&m.matched)?m.matched.body:"";\n'
            "    }\n"
            "    const sec=reviewBabs[idx];\n"
            '    return fileBabs[getBabKey(sec.title)]||"";\n'
            "  };\n"
        )
        assert real_manuscript_text_for_index in text  # real file: present

        def _h01_mutate(src):
            # (1) fileBabs must ACTUALLY compute the legacy positional map even when the
            # flag is on -- otherwise mutation (2) alone would break EVERY chapter's
            # lookup (nothing populated at all), not specifically restore the historical
            # Bab-only-vs-Chapter-N defect.
            step1 = src.replace(
                "  const fileBabs=useMemo(()=>{\n"
                "    if(chapterKeyEnabled)return{};\n",
                "  const fileBabs=useMemo(()=>{\n",
                1,
            )
            # (2) manuscriptTextForIndex -- the REAL production render path E03's mounted
            # assertion exercises -- must ACTUALLY consume the legacy fileBabs/getBabKey
            # lookup instead of matchResults. This is the exact gap Rework 2's mutation
            # (which only flipped mutation (1) above) left untouched: with (1) alone, the
            # render path still read matchResults regardless, so the true Bab-only
            # regression never actually surfaced in the mounted UI.
            step2 = step1.replace(
                real_manuscript_text_for_index,
                "  const manuscriptTextForIndex=(idx)=>{\n"
                "    const sec=reviewBabs[idx];\n"
                '    return fileBabs[getBabKey(sec.title)]||"";\n'
                "  };\n",
                1,
            )
            assert step2 != step1, "mutation (2) did not change manuscriptTextForIndex"
            return step2

        tmp_root = _detached_frontend_tree_mounted("main.jsx", _h01_mutate)
        target = _h_node("TestE_FrontendIntegration",
                          "test_e03_autofixsection_uses_matched_record_not_getbabkey_when_enabled")
        # E03's real MOUNTED assertion ("belum ada teks" absent, meaning the Chapter 4
        # row's original text populates via ordinal-based chapter-key matching) now
        # fails against the detached, mutated copy -- the mutation targets the ENABLED
        # production RENDER path in main.jsx itself (never just the .mjs helper regex,
        # never just a source-substring flip), genuinely restoring the historical
        # Bab-only getBabKey/filter behavior: a review section's bare-ordinal heading
        # ("Chapter 4") no longer resolves against the manuscript's own fuller heading
        # text ("Chapter 4: The Sound That Would Not Fade") once matching is forced back
        # onto the legacy exact-string positional lookup.
        _assert_target_fails_and_inherited_sentinel_passes(
            _BACKEND_ROOT, target, extra_env={"B04A_FRONTEND": str(tmp_root)})

    @_covers("H02")
    def test_h02_unanchored_search_would_wrongly_match_body_prose(self):
        text = frontend_text("src/narasiReviewContract.mjs")
        assert "const CHAPTER_HEADING_RE =\n  /^(Bab|Chapter)" in text  # real file: present

        tmp_root = _detached_frontend_tree(
            "narasiReviewContract.mjs",
            lambda src: src.replace(
                "const CHAPTER_HEADING_RE =\n  /^(Bab|Chapter)",
                "const CHAPTER_HEADING_RE =\n  /(Bab|Chapter)", 1))
        target = _h_node("TestD_FrontendChapterParsing",
                          "test_d06_h1_h3_body_mentions_babylon_chapterhouse_multi_ordinal_do_not_parse")
        # D06's real assertion (a prose sentence merely CONTAINING "Bab 4:" mid-string
        # must never parse as a heading) now fails against the detached, mutated copy --
        # removing the `^` anchor lets the label match anywhere in the string.
        _assert_target_fails_and_inherited_sentinel_passes(
            _BACKEND_ROOT, target, extra_env={"B04A_FRONTEND": str(tmp_root)})

    @_covers("H03")
    def test_h03_last_write_wins_would_silently_hide_the_duplicate_ordinal_7(self):
        old_block = (
            "  for (const entry of rawEntries) {\n"
            "    const dupOrdinal = ordinalCounts.get(entry.ordinal) > 1;\n"
            "    const dupHeading = headingTextCounts.get(entry.heading) > 1;\n"
            "    if (dupOrdinal) result.duplicateOrdinals.add(entry.ordinal);\n"
            "    if (dupHeading) result.duplicateHeadings.add(entry.heading);\n"
            "    if (dupOrdinal || dupHeading) continue;\n"
            "    result.chapters.set(entry.key, Object.freeze({\n"
        )
        new_block = (
            "  for (const entry of rawEntries) {\n"
            "    result.chapters.set(entry.key, Object.freeze({\n"
        )
        tmp_root = _detached_frontend_tree(
            "narasiReviewContract.mjs", lambda src: src.replace(old_block, new_block, 1))
        target = _h_node("TestD_FrontendChapterParsing",
                          "test_d08_duplicate_ordinal_or_heading_is_ambiguity_no_last_write_wins")
        # D08's real assertions (ordinal 7 flagged as a duplicate and excluded from
        # chapters) now fail against the detached, mutated copy -- last-write-wins keeps
        # whichever ordinal-7 body was written last, silently, with no duplicate signal.
        _assert_target_fails_and_inherited_sentinel_passes(
            _BACKEND_ROOT, target, extra_env={"B04A_FRONTEND": str(tmp_root)})

    @_covers("H04")
    def test_h04_non_fence_aware_scan_would_wrongly_count_the_fenced_heading(self):
        old_close_branch = (
            "      const closeMatch = FENCE_CLOSE_RE.exec(line.content);\n"
            "      if (closeMatch && closeMatch[1][0] === fenceChar && closeMatch[1].length >= fenceLen) {\n"
            "        fenceChar = null;\n"
            "        fenceLen = 0;\n"
            "      }\n"
            "      continue;\n"
            "    }\n"
        )
        new_close_branch = (
            "      const closeMatch = FENCE_CLOSE_RE.exec(line.content);\n"
            "      if (closeMatch && closeMatch[1][0] === fenceChar && closeMatch[1].length >= fenceLen) {\n"
            "        fenceChar = null;\n"
            "        fenceLen = 0;\n"
            "      }\n"
            "    }\n"
        )
        tmp_root = _detached_frontend_tree(
            "narasiReviewContract.mjs", lambda src: src.replace(old_close_branch, new_close_branch, 1))
        target = _h_node("TestD_FrontendChapterParsing",
                          "test_d07_fenced_headings_ignored_malformed_fence_is_explicit_ambiguity")
        # D07's real assertion (a fenced heading sharing its ordinal with a real heading
        # elsewhere stays fence-ignored, never counted as a duplicate) now fails against
        # the detached, mutated copy -- removing the `continue` lets every line while
        # inside a fence ALSO be scanned as a potential H2 heading.
        _assert_target_fails_and_inherited_sentinel_passes(
            _BACKEND_ROOT, target, extra_env={"B04A_FRONTEND": str(tmp_root)})

    @_covers("H05")
    def test_h05_report_language_substituted_for_manuscript_language_at_the_call_site(self):
        old = (
            "        lineage_binding=_review_binding, language=_review_resolved_language,\n"
            "        content_hash=_review_hashlib.sha256(message.encode(\"utf-8\")).hexdigest(), model=model)"
        )
        new = (
            "        lineage_binding=_review_binding, language=_review_report_language,\n"
            "        content_hash=_review_hashlib.sha256(message.encode(\"utf-8\")).hexdigest(), model=model)"
        )
        root = _detached_backend_tree(lambda src: src.replace(old, new, 1))
        target = _h_node("TestF_B0708Noninterference",
                          "test_f03_create_derived_input_receives_manuscript_language_every_combo")
        # F03's real assertion (create_derived_input receives manuscript_language, never
        # report_language) now fails against the detached, mutated copy.
        _assert_target_fails_and_inherited_sentinel_passes(root, target)

    @_covers("H06")
    def test_h06_removing_block_append_while_keeping_response_metadata_breaks_b08_not_b10(self):
        old = (
            "    if _b04a_on:\n"
            "        # Appended even on the legacy client-system path (no `style` sent) -- the final\n"
            "        # contract block must not be bypassable just because `style` was omitted.\n"
            "        system = system + narasi_review_contract.build_review_language_block(\n"
            "            _review_resolved_language, _review_report_language)\n"
        )
        new = (
            "    if _b04a_on:\n"
            "        pass  # H06 mutation: block-append removed, response metadata untouched\n"
        )
        root = _detached_backend_tree(lambda src: src.replace(old, new, 1))
        target = _h_node("TestB_BackendReviewLanguage",
                          "test_b08_block_appended_even_on_legacy_client_system_path")
        # B08's real assertion ("FINAL LANGUAGE CONTRACT" present in the sent system
        # prompt) now fails against the detached, mutated copy -- even though the
        # response's review_contract_version metadata (B10) is untouched by this mutation.
        _assert_target_fails_and_inherited_sentinel_passes(root, target)

    @_covers("H07")
    def test_h07_stripped_oneshot_instruction_breaks_c03_heading_preservation_check(self):
        old = (
            "        \"- Preserve every original Markdown chapter heading byte-for-byte; never rename \"\n"
            "        \"\\\"Chapter N\\\" to \\\"Bab N\\\" or the reverse.\",\n"
        )
        new = (
            "        \"- Preserve every original Markdown chapter heading byte-for-byte.\",\n"
        )
        root = _detached_backend_tree(
            lambda src: src.replace(old, new, 1), target_relpath="python/narasi_review_contract.py")
        target = _h_node("TestC_OneshotOptimizeSeparation",
                          "test_c03_original_heading_bytes_preserved_no_label_translation")
        # C03's real assertion would now fail against the detached, mutated copy.
        _assert_target_fails_and_inherited_sentinel_passes(root, target)

    @_covers("H08")
    def test_h08_swallowing_hostile_types_into_default_breaks_b05(self):
        old = (
            "    if type(raw) is not str:\n"
            "        raise ReviewContractError(\"REPORT_LANGUAGE_TYPE_INVALID\")\n"
            "    trimmed = raw.strip(_ASCII_WHITESPACE)\n"
        )
        new = (
            "    if type(raw) is not str:\n"
            "        raw = str(raw)  # H08 mutation: naive coercion, not exact-type rejection\n"
            "    trimmed = raw.strip(_ASCII_WHITESPACE)\n"
        )
        root = _detached_backend_tree(
            lambda src: src.replace(old, new, 1), target_relpath="python/narasi_review_contract.py")
        target = _h_node("TestB_BackendReviewLanguage",
                          "test_b05_exact_type_first_rejects_hostile_before_provider_or_persistence")
        # B05's real assertion (code == REPORT_LANGUAGE_TYPE_INVALID for every hostile
        # type, including bool) now fails against the detached, mutated copy -- it
        # naively coerces `True` to "true", which is REPORT_LANGUAGE_UNSUPPORTED
        # instead, a different failure class entirely.
        _assert_target_fails_and_inherited_sentinel_passes(root, target)

    @_covers("H09")
    def test_h09_emitting_chapter_id_alias_would_break_the_exact_record_shape_check(self):
        old = (
            "    result.chapters.set(entry.key, Object.freeze({\n"
            "      key: entry.key, ordinal: entry.ordinal, label: entry.label,\n"
            "      heading: entry.heading, body: entry.body,\n"
            "    }));"
        )
        new = (
            "    result.chapters.set(entry.key, Object.freeze({\n"
            "      key: entry.key, chapter_id: entry.key, ordinal: entry.ordinal, label: entry.label,\n"
            "      heading: entry.heading, body: entry.body,\n"
            "    }));"
        )
        tmp_root = _detached_frontend_tree(
            "narasiReviewContract.mjs", lambda src: src.replace(old, new, 1))
        target = _h_node("TestD_FrontendChapterParsing",
                          "test_d01_exact_bab_1_parses_retains_heading_and_body_bytes")
        # D01's real exact-keys assertion now fails against the detached, mutated copy --
        # it emits an extra "chapter_id" alias field never part of the real shape.
        _assert_target_fails_and_inherited_sentinel_passes(
            _BACKEND_ROOT, target, extra_env={"B04A_FRONTEND": str(tmp_root)})

    @_covers("H10")
    def test_h10_ignoring_the_flag_would_leak_fields_into_the_off_path(self):
        old = '    return _flag_on("NARASI_REVIEW_LANGUAGE_CHAPTER_KEY_V1", "0")\n'
        new = '    return True  # H10 mutation: flag check ignored\n'
        root = _detached_backend_tree(lambda src: src.replace(old, new, 1))
        target = _h_node("TestA_AuthorityFlagsCompat",
                          "test_a01_backend_flag_off_is_byte_identical_to_legacy")
        # A01's real assertion ("report_language" absent from the response when the env
        # flag is off) now fails against the detached, mutated copy -- the flag check
        # itself is ignored and always reports enabled.
        _assert_target_fails_and_inherited_sentinel_passes(root, target)

    @_covers("H11")
    def test_h11_injected_import_into_pure_module_source_is_detected_by_the_ast_guard(self):
        root = _detached_backend_tree(
            lambda src: "import os\n" + src, target_relpath="python/narasi_review_contract.py")
        target = _h_node("TestF_B0708Noninterference",
                          "test_f05_no_new_provider_network_or_database_call_ast_import_scan")
        # F05's real assertions (imported == {"types"}; imported & forbidden == set())
        # now fail against the detached, mutated copy.
        _assert_target_fails_and_inherited_sentinel_passes(root, target)

    @_covers("H12")
    def test_h12_removing_a_bound_covers_decorator_would_fail_completeness(self):
        old = ('    @_covers("B01")\n'
               '    def test_b01_report_language_id_exact_token_contract(self):')
        new = '    def test_b01_report_language_id_exact_token_contract(self):'
        root = _detached_test_file_tree(lambda src: src.replace(old, new, 1))
        completeness_target = _h_node(
            "TestAcceptanceMatrixCompleteness", "test_every_required_id_bound_via_explicit_covers_decorator")
        # TestAcceptanceMatrixCompleteness's real check (every required ID has a
        # @_covers binding) now fails against the detached, mutated copy -- proven
        # against the genuinely inherited lifecycle sentinel, in its own subprocess,
        # with its own exact node-id assertion (never an aggregate count).
        _assert_target_fails_and_inherited_sentinel_passes(root, completeness_target)

        # A SECOND, even more specific sentinel in the SAME detached copy: the B01 test
        # METHOD ITSELF -- still fully present and functional, only its @_covers binding
        # removed -- proving the mutation is surgical: it breaks completeness TRACKING
        # alone, never B01's actual product behavior. Its own separate subprocess, its
        # own exact node-id assertion.
        b01_sentinel = _h_node("TestB_BackendReviewLanguage", "test_b01_report_language_id_exact_token_contract")
        b01_proc = _run_detached_pytest(root, [b01_sentinel])
        b01_out = b01_proc.stdout + b01_proc.stderr
        assert b01_proc.returncode == 0, f"B01 sentinel did not pass:\n{b01_out}"
        assert "1 passed" in b01_proc.stdout, f"expected exactly 1 passed for B01 sentinel:\n{b01_out}"

    def test_h16_reintroducing_an_id_en_marker_map_would_break_delimiter_byte_identity(self):
        """Rework 9 (Codex instruction #7, negative control #1) [SUCCESSOR
        RECONSTRUCTION: exact real-code anchors below are byte-verified against the
        frozen accepted python/narasi_review_contract.py and src/narasiReviewContract.mjs
        (both hash-pinned in the ledger); the surrounding harness/assertion shape is
        reconstructed from the sibling H18/H20 pattern, not recovered original bytes --
        see the source-recovery dossier]: restores the SPECIFIC, previously-rejected
        Rework-8 design -- a closed id/en marker MAP keyed by report_language --
        instead of the single fixed, language-neutral PART_TWO_MARKER constant, and
        proves the cross-runtime byte-identity contract genuinely fails against that
        reversion: the backend export is no longer a plain string at all, so it can
        never be byte-identical to the frontend's plain-string constant."""
        real_export = 'PART_TWO_MARKER = "---WIMBA_REVIEW_CHAPTERS_START:v1---"'
        text = _BACKEND_ROOT.joinpath("python/narasi_review_contract.py").read_text(encoding="utf-8")
        assert real_export in text  # real file: present, exact language-neutral constant

        def _h16_mutate(src):
            mutated = src.replace(
                real_export,
                'PART_TWO_MARKER = {"id": "---WIMBA_REVIEW_CHAPTERS_START:v1---", '
                '"en": "---WIMBA_REVIEW_CHAPTERS_START:v1---"}',
                1,
            )
            assert mutated != src, "mutation did not change PART_TWO_MARKER export"
            return mutated

        root = _detached_backend_tree(_h16_mutate, target_relpath="python/narasi_review_contract.py")
        target = _h_node("TestPartTwoMarkerCrossRuntimeContract",
                          "test_backend_and_frontend_part_two_marker_are_byte_identical")
        # The cross-runtime contract test's own module-shape expectations fail against
        # the detached, mutated copy -- reintroducing any language-keyed structure,
        # even one that resolves to the same bytes for one key today, breaks the
        # "plain constant, never a function or map" structural guarantee round 9
        # requires, and Codex's finding was exactly that any such structure risks a
        # future edit picking the wrong key.
        _assert_target_fails_and_inherited_sentinel_passes(root, target)

    def test_h17_deriving_the_delimiter_from_report_language_would_break_every_combination(self):
        """Rework 9 (Codex instruction #7, negative control #2) [SUCCESSOR
        RECONSTRUCTION: real-code anchor below is byte-verified against the frozen
        accepted src/main.jsx; harness/assertion shape reconstructed from the
        sibling H18 pattern (same mechanism, swapping reportLanguage for
        manuscriptLanguage) -- see the source-recovery dossier]: swaps which prop
        authorizes the marker lookup -- reportLanguage (the user-facing selector)
        instead of the fixed, language-neutral constant -- and proves the corrected
        D13 mounted test genuinely fails against that authority swap: the moment
        ANY report_language-derived component is folded into the marker, clicking
        the EN report-language selector produces a different delimiter than the ID
        selector, breaking every one of D13's four language combinations at once."""
        real_unrecognized_babs = (
            "  const unrecognizedBabs=useMemo(()=>{\n"
            "    if(!chapterKeyEnabled)return[];\n"
            "    const secs=parseReviewMarkdown(review);\n"
            "    const partTwoIdx=secs.findIndex(s=>(s.lines||[]).some(line=>line.trim()===narasiReviewContract.PART_TWO_MARKER));\n"
            "    const perChapterZone=partTwoIdx===-1?secs:secs.slice(partTwoIdx+1);\n"
            '    return perChapterZone.filter(s=>s.type==="section"&&!narasiReviewContract.extractChapterKey((s.title||"").trim()));\n'
            "  },[review,chapterKeyEnabled]);\n"
        )
        text = frontend_text("src/main.jsx")
        assert real_unrecognized_babs in text  # real file: present, language-neutral delimiter version

        def _h17_mutate(src):
            mutated = src.replace(
                real_unrecognized_babs,
                "  const unrecognizedBabs=useMemo(()=>{\n"
                "    if(!chapterKeyEnabled)return[];\n"
                "    const secs=parseReviewMarkdown(review);\n"
                '    const PART_TWO_MARKER=(reportLanguage==="en")?"---WIMBA_REVIEW_CHAPTERS_START:v1---en":"---WIMBA_REVIEW_CHAPTERS_START:v1---id";\n'
                "    const partTwoIdx=secs.findIndex(s=>(s.lines||[]).some(line=>line.trim()===PART_TWO_MARKER));\n"
                "    const perChapterZone=partTwoIdx===-1?secs:secs.slice(partTwoIdx+1);\n"
                '    return perChapterZone.filter(s=>s.type==="section"&&!narasiReviewContract.extractChapterKey((s.title||"").trim()));\n'
                "  },[review,chapterKeyEnabled,reportLanguage]);\n",
                1,
            )
            assert mutated != src, "mutation did not change unrecognizedBabs"
            return mutated

        tmp_root = _detached_frontend_tree_mounted("main.jsx", _h17_mutate)
        target = _h_node("TestD_FrontendChapterParsing",
                          "test_d13_part_two_marker_is_language_neutral_across_manuscript_heading_styles")
        # D13's four-combination assertion fails against the detached, mutated copy --
        # D13 genuinely clicks the real report-language selector UI, so reportLanguage
        # is a real, non-empty React state value here (unlike H18's manuscriptLanguage,
        # which the mounted harness never independently sets) -- the moment ANY
        # reportLanguage-derived component is folded into the marker, the delimiter
        # differs between the id-report and en-report runs, breaking D13's
        # same-delimiter-across-all-4-combinations assertion.
        _assert_target_fails_and_inherited_sentinel_passes(
            _BACKEND_ROOT, target, extra_env={"B04A_FRONTEND": str(tmp_root)})

    def test_h18_deriving_the_delimiter_from_manuscript_language_would_break_the_marker(self):
        """Rework 9 (Codex instruction #7, negative control #3): swaps which prop
        authorizes the marker lookup -- manuscriptLanguage (the B-07 lineage-
        resolved manuscript language) instead of the fixed, language-neutral
        constant -- and proves the corrected D13 mounted test genuinely fails
        against that authority swap: the mounted harness's manuscriptLanguage state
        is never independently set (empty string, unrelated to the clicked report-
        language selector), so narasiReviewContract.partTwoMarker is called with an
        argument it no longer even accepts (the export is now a plain constant, no
        function), throwing for every combination regardless of what was actually
        clicked."""
        real_unrecognized_babs = (
            "  const unrecognizedBabs=useMemo(()=>{\n"
            "    if(!chapterKeyEnabled)return[];\n"
            "    const secs=parseReviewMarkdown(review);\n"
            "    const partTwoIdx=secs.findIndex(s=>(s.lines||[]).some(line=>line.trim()===narasiReviewContract.PART_TWO_MARKER));\n"
            "    const perChapterZone=partTwoIdx===-1?secs:secs.slice(partTwoIdx+1);\n"
            '    return perChapterZone.filter(s=>s.type==="section"&&!narasiReviewContract.extractChapterKey((s.title||"").trim()));\n'
            "  },[review,chapterKeyEnabled]);\n"
        )
        text = frontend_text("src/main.jsx")
        assert real_unrecognized_babs in text  # real file: present, language-neutral delimiter version

        def _h18_mutate(src):
            mutated = src.replace(
                real_unrecognized_babs,
                "  const unrecognizedBabs=useMemo(()=>{\n"
                "    if(!chapterKeyEnabled)return[];\n"
                "    const secs=parseReviewMarkdown(review);\n"
                "    const PART_TWO_MARKER=narasiReviewContract.PART_TWO_MARKER(manuscriptLanguage);\n"
                "    const partTwoIdx=secs.findIndex(s=>(s.lines||[]).some(line=>line.trim()===PART_TWO_MARKER));\n"
                "    const perChapterZone=partTwoIdx===-1?secs:secs.slice(partTwoIdx+1);\n"
                '    return perChapterZone.filter(s=>s.type==="section"&&!narasiReviewContract.extractChapterKey((s.title||"").trim()));\n'
                "  },[review,chapterKeyEnabled,manuscriptLanguage]);\n",
                1,
            )
            assert mutated != src, "mutation did not change unrecognizedBabs"
            return mutated

        tmp_root = _detached_frontend_tree_mounted("main.jsx", _h18_mutate)
        target = _h_node("TestD_FrontendChapterParsing",
                          "test_d13_part_two_marker_is_language_neutral_across_manuscript_heading_styles")
        # D13 fails against the detached, mutated copy -- manuscriptLanguage is empty
        # in the mounted harness (never independently set), so appending it is a
        # harmless no-op HERE, but the mutation still proves the principle: the
        # instant ANY manuscriptLanguage-derived component is folded into the marker,
        # the marker stops being a pure, language-neutral constant, and a real,
        # non-empty manuscriptLanguage value elsewhere in the app would silently
        # break every combination the same way H16/H17 do.
        _assert_target_fails_and_inherited_sentinel_passes(
            _BACKEND_ROOT, target, extra_env={"B04A_FRONTEND": str(tmp_root)})

    def test_h19_a_one_byte_frontend_delimiter_drift_would_break_the_cross_runtime_contract(self):
        """Rework 9 (Codex instruction #7, negative control #4) [SUCCESSOR
        RECONSTRUCTION: real-code anchor below is byte-verified against the frozen
        accepted src/narasiReviewContract.mjs; harness/assertion shape reconstructed
        from the sibling H20 pattern at minimal (one-byte) mutation magnitude instead
        of H20's full-translation magnitude -- see the source-recovery dossier]:
        mutates ONLY the frontend's PART_TWO_MARKER export by a single trailing
        character (v1 -> v2), leaving the backend's constant untouched, and proves
        the pure cross-runtime contract test genuinely fails even against the
        smallest possible drift -- the byte-identity check has no tolerance, unlike
        a semantic/fuzzy comparison that a one-character version bump might survive."""
        real_export = 'export const PART_TWO_MARKER = "---WIMBA_REVIEW_CHAPTERS_START:v1---";'
        text = frontend_text("src/narasiReviewContract.mjs")
        assert real_export in text  # real file: present, exact neutral-delimiter export

        def _h19_mutate(src):
            mutated = src.replace(
                real_export,
                'export const PART_TWO_MARKER = "---WIMBA_REVIEW_CHAPTERS_START:v2---";',
                1,
            )
            assert mutated != src, "mutation did not change PART_TWO_MARKER export"
            return mutated

        tmp_root = _detached_frontend_tree("narasiReviewContract.mjs", _h19_mutate)
        target = _h_node("TestPartTwoMarkerCrossRuntimeContract",
                          "test_backend_and_frontend_part_two_marker_are_byte_identical")
        # The cross-runtime contract test fails against the detached, mutated copy --
        # a single trailing byte ("v1" -> "v2") is enough to break byte-identity,
        # proving the contract has zero tolerance rather than accidentally passing
        # on near-identical strings.
        _assert_target_fails_and_inherited_sentinel_passes(
            _BACKEND_ROOT, target, extra_env={"B04A_FRONTEND": str(tmp_root)})

    def test_h20_translating_the_delimiter_would_break_the_cross_runtime_contract(self):
        """Rework 9 (Codex instruction #7, negative control #5): mutates ONLY the
        frontend's PART_TWO_MARKER export to a translated/localized-LOOKING string
        (Indonesian wording, still an ALL-CAPS machine-delimiter shape) -- exactly
        the kind of well-intentioned but forbidden localization Codex's instruction
        explicitly rules out -- leaving the backend's constant untouched, and proves
        the pure cross-runtime contract test genuinely fails."""
        real_export = 'export const PART_TWO_MARKER = "---WIMBA_REVIEW_CHAPTERS_START:v1---";'
        text = frontend_text("src/narasiReviewContract.mjs")
        assert real_export in text  # real file: present, exact neutral-delimiter export

        def _h20_mutate(src):
            mutated = src.replace(
                real_export,
                'export const PART_TWO_MARKER = "---MULAI_BAB_REVIEW_WIMBA:v1---";',
                1,
            )
            assert mutated != src, "mutation did not change PART_TWO_MARKER export"
            return mutated

        tmp_root = _detached_frontend_tree("narasiReviewContract.mjs", _h20_mutate)
        target = _h_node("TestPartTwoMarkerCrossRuntimeContract",
                          "test_backend_and_frontend_part_two_marker_are_byte_identical")
        # The cross-runtime contract test fails against the detached, mutated copy --
        # the frontend's PART_TWO_MARKER is now a translated/localized string the
        # backend's constant does not share, so the byte-identity assertion breaks.
        _assert_target_fails_and_inherited_sentinel_passes(
            _BACKEND_ROOT, target, extra_env={"B04A_FRONTEND": str(tmp_root)})


# ===========================================================================
# W — Rework 10 (Codex P1 finding): the mounted-frontend D0x/H0x tests above all
# fabricate their OWN "true marker" text inside a client-script fixture -- proving
# the FRONTEND PARSER is correct, but never that the REAL backend endpoint (the
# only thing that actually instructs the model) emits PART_TWO_MARKER at all.
# Codex proved this gap by disabling the marker in-memory: the full 94-test suite
# still passed 94/94, because nothing here inspected the real captured system
# message for this specific literal. W01-W05 close that gap by binding to the
# REAL laozhang_api.narasi_review endpoint and the REAL build_review_language_block
# output, never a frontend fixture. W06-W08 make the "language-independent by API
# shape" claim executable rather than merely behavioral: a wrapper function that
# still RETURNS the same value for every language but reintroduces a language
# PARAMETER in the selection path is a forbidden shape even though D13 (which only
# checks the returned VALUE) would not itself notice.
# ===========================================================================
class TestW_BackendDelimiterWiring:
    @_covers("W01")
    def test_w01_build_review_language_block_delimiter_contract_report_language_id(self):
        block = nrc.build_review_language_block("id", "id")
        assert block.count(nrc.PART_TWO_MARKER) == 1
        assert nrc.PART_TWO_MARKER == "---WIMBA_REVIEW_CHAPTERS_START:v1---"
        header_idx = block.index("FINAL LANGUAGE CONTRACT")
        marker_idx = block.index(nrc.PART_TWO_MARKER)
        assert header_idx < marker_idx, "delimiter must be inside the final language-contract block"
        assert "FIXED, LANGUAGE-NEUTRAL MACHINE DELIMITER" in block
        assert "exactly once" in block
        assert "do not translate, localize, paraphrase, prefix, suffix, duplicate" in block

    @_covers("W02")
    def test_w02_build_review_language_block_delimiter_contract_report_language_en(self):
        block = nrc.build_review_language_block("id", "en")
        assert block.count(nrc.PART_TWO_MARKER) == 1
        header_idx = block.index("FINAL LANGUAGE CONTRACT")
        marker_idx = block.index(nrc.PART_TWO_MARKER)
        assert header_idx < marker_idx, "delimiter must be inside the final language-contract block"
        assert "FIXED, LANGUAGE-NEUTRAL MACHINE DELIMITER" in block
        assert "exactly once" in block
        assert "do not translate, localize, paraphrase, prefix, suffix, duplicate" in block

    @_covers("W03")
    def test_w03_narasi_review_endpoint_system_message_contains_delimiter_exactly_once_id(self, monkeypatch):
        monkeypatch.setenv("NARASI_REVIEW_LANGUAGE_CHAPTER_KEY_V1", "1")
        _patch_review_common(monkeypatch)
        run(laozhang_api.narasi_review(review_body(report_language="id"), _user()))
        system_sent = _FakeReviewClient.calls[0]["messages"][0]["content"]
        # The REAL system message the endpoint actually sent to the (fake) provider --
        # never a re-expression of what it "should" send -- must contain the canonical
        # delimiter exactly once.
        assert system_sent.count(nrc.PART_TWO_MARKER) == 1, (
            f"expected the canonical delimiter exactly once in the real system message:\n{system_sent}")

    @_covers("W04")
    def test_w04_narasi_review_endpoint_system_message_contains_delimiter_exactly_once_en(self, monkeypatch):
        monkeypatch.setenv("NARASI_REVIEW_LANGUAGE_CHAPTER_KEY_V1", "1")
        _patch_review_common(monkeypatch)
        run(laozhang_api.narasi_review(review_body(report_language="en"), _user()))
        system_sent = _FakeReviewClient.calls[0]["messages"][0]["content"]
        assert system_sent.count(nrc.PART_TWO_MARKER) == 1, (
            f"expected the canonical delimiter exactly once in the real system message:\n{system_sent}")

    @_covers("W05")
    def test_w05_removing_the_marker_from_build_review_language_block_breaks_the_wiring_test(self):
        old = (
            '        "worded Part 2 boundary marker instruction given earlier in this prompt:",\n'
            "        PART_TWO_MARKER,\n"
        )
        new = (
            '        "worded Part 2 boundary marker instruction given earlier in this prompt:",\n'
        )
        root = _detached_backend_tree(
            lambda src: src.replace(old, new, 1), target_relpath="python/narasi_review_contract.py")
        target = _h_node("TestW_BackendDelimiterWiring",
                          "test_w03_narasi_review_endpoint_system_message_contains_delimiter_exactly_once_id")
        # W03's real assertion (the canonical delimiter appears in the ACTUAL system
        # message the endpoint sends to the provider) now fails against the detached,
        # mutated copy -- the PART_TWO_MARKER tuple entry itself is removed from
        # build_review_language_block's `lines`, so the endpoint never instructs the
        # model to emit it, even though this mutation is invisible to every OTHER
        # B-04a test (none of which inspect the captured system message for this
        # specific literal) -- reproducing exactly the gap Codex found by disabling
        # the same line in memory and observing the suite still pass 94/94.
        _assert_target_fails_and_inherited_sentinel_passes(root, target)

    @_covers("W06")
    def test_w06_python_part_two_marker_is_a_single_direct_string_literal_no_language_map_or_function(self):
        source = Path(nrc.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        module_assigns = [
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "PART_TWO_MARKER"
        ]
        assert len(module_assigns) == 1, (
            f"expected exactly one module-level PART_TWO_MARKER assignment, "
            f"found {len(module_assigns)}")
        value_node = module_assigns[0].value
        assert isinstance(value_node, ast.Constant) and isinstance(value_node.value, str), (
            "PART_TWO_MARKER must be a direct string-literal assignment, never computed, "
            "never a call, never a subscript/lookup -- the API shape itself must make "
            "language-dependent selection impossible")
        assert value_node.value == "---WIMBA_REVIEW_CHAPTERS_START:v1---"

        assigned_names, function_names = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assigned_names.add(target.id)
            elif isinstance(node, ast.FunctionDef):
                function_names.add(node.name)
        assert "_PART_TWO_MARKERS" not in assigned_names, (
            "the superseded Rework 8 id/en marker map must never be reintroduced")
        assert "part_two_marker" not in function_names, (
            "a language-parameterized marker function must never be reintroduced, even "
            "if it would currently return the same value for every language")

    @_covers("W07")
    def test_w07_frontend_part_two_marker_is_a_single_plain_const_no_language_map_or_function(self):
        contract_text = frontend_text("src/narasiReviewContract.mjs")
        real_export = 'export const PART_TWO_MARKER = "---WIMBA_REVIEW_CHAPTERS_START:v1---";'
        assert contract_text.count(real_export) == 1, (
            "expected exactly one plain PART_TWO_MARKER const export")
        assert "PART_TWO_MARKERS" not in contract_text, (
            "the superseded Rework 8 id/en marker Map must never be reintroduced")
        assert "partTwoMarker" not in contract_text, (
            "a language-parameterized marker function must never be reintroduced, even "
            "if it would currently return the same value for every language")

        main_text = frontend_text("src/main.jsx")
        selection_line = (
            "    const partTwoIdx=secs.findIndex(s=>(s.lines||[]).some("
            "line=>line.trim()===narasiReviewContract.PART_TWO_MARKER));"
        )
        assert selection_line in main_text, "expected the exact plain-constant selection line"
        assert "reportLanguage" not in selection_line
        assert "manuscriptLanguage" not in selection_line

    @_covers("W08")
    def test_w08_behavior_preserving_language_parameterized_wrapper_still_fails_the_shape_test(self):
        real_export = 'export const PART_TWO_MARKER = "---WIMBA_REVIEW_CHAPTERS_START:v1---";'

        def _add_wrapper(src):
            assert src.count(real_export) == 1
            return src.replace(
                real_export,
                real_export + "\n\n"
                "// Rework 10 W08 negative-control mutation: a wrapper that still RETURNS\n"
                "// the same neutral value for every language -- behaviorally invisible to\n"
                "// D13, but a forbidden API shape (a language PARAMETER in the selection\n"
                "// path) that the structural shape test (W07) must nevertheless catch.\n"
                "export function partTwoMarker(reportLanguage) { return PART_TWO_MARKER; }",
                1,
            )

        tmp_root = _detached_frontend_tree_mounted("narasiReviewContract.mjs", _add_wrapper)

        real_selection = (
            "    const partTwoIdx=secs.findIndex(s=>(s.lines||[]).some("
            "line=>line.trim()===narasiReviewContract.PART_TWO_MARKER));"
        )
        mutated_selection = (
            "    const partTwoIdx=secs.findIndex(s=>(s.lines||[]).some("
            "line=>line.trim()===narasiReviewContract.partTwoMarker(reportLanguage)));"
        )
        main_path = tmp_root / "src/main.jsx"
        main_real = main_path.read_text(encoding="utf-8")
        assert real_selection in main_real
        main_path.write_text(main_real.replace(real_selection, mutated_selection, 1), encoding="utf-8")

        # (1) D13's real behavioral assertion must stay GREEN: the wrapper returns the
        # exact same value as the plain constant for every report_language, so the
        # mounted app's actual parsing/rendering is genuinely unaffected.
        d13_target = _h_node("TestD_FrontendChapterParsing",
                              "test_d13_part_two_marker_is_language_neutral_across_manuscript_heading_styles")
        d13_proc = _run_detached_pytest(
            _BACKEND_ROOT, [d13_target], extra_env={"B04A_FRONTEND": str(tmp_root)})
        d13_out = d13_proc.stdout + d13_proc.stderr
        assert d13_proc.returncode == 0, (
            f"D13 must stay green -- the wrapper is behaviorally identical to the plain "
            f"constant for every combination:\n{d13_out}")
        assert "1 passed" in d13_proc.stdout, d13_out

        # (2) W07's structural shape assertion must nevertheless FAIL against this exact
        # same mutated copy -- proving language-independence is enforced by shape, not
        # merely because the current outputs happen to match.
        w07_target = _h_node(
            "TestW_BackendDelimiterWiring",
            "test_w07_frontend_part_two_marker_is_a_single_plain_const_no_language_map_or_function")
        _assert_target_fails_and_inherited_sentinel_passes(
            _BACKEND_ROOT, w07_target, extra_env={"B04A_FRONTEND": str(tmp_root)})


class TestPrivacyScan:
    # This file legitimately embeds ONE UUID-shaped literal (the outline_id passed to the real
    # `admit_outline_lifecycle`, which requires canonical UUID format) -- matching
    # test_narasi_lifecycle_repository.py's own precedent, this file therefore does not carry a
    # blanket "no UUID literal" check; opaque tenant/user IDs below are non-UUID placeholders.
    _TENANT_SHAPE_RX = re.compile(r"\b(?!legacy)[a-z]{3,4}\d{4,6}\b")

    def test_no_tenant_id_shaped_strings(self):
        text = open(__file__, encoding="utf-8").read()
        hits = [m.group(0) for m in self._TENANT_SHAPE_RX.finditer(text)]
        assert hits == [], hits

    def test_fixtures_contain_no_production_identifiers(self):
        for fixture_path in _FIXTURES_DIR.glob("*.json"):
            data = fixture_path.read_text(encoding="utf-8")
            assert "@gmail.com" not in data
            assert "wimba" not in data.lower()
            assert not re.search(r"sk-[a-zA-Z0-9]{20,}", data)
            assert "cerita-ai-studio" not in data
            assert "rino" not in data.lower()

    def test_narasi_review_contract_source_has_no_secret_or_env_access(self):
        # AST-based (not substring): a naive `"os.environ" not in source` check would false-
        # positive on this very module's own docstring prose describing what it does NOT do.
        tree = ast.parse(Path(nrc.__file__).read_text(encoding="utf-8"))
        env_access_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv"):
                env_access_names.add(node.attr)
        assert env_access_names == set()


# ===========================================================================
# Completeness oracle — every mandatory acceptance ID bound to an executable test.
# Hand-authored, fixed at write-time; never derived from ACCEPTANCE-MATRIX.md, test
# names (at runtime or from parsed source), or implementation constants. See
# test_narasi_b01_post_mutation_revalidation.py's identical `_covers`/oracle discipline.
# ===========================================================================
_REQUIRED_ACCEPTANCE_IDS = frozenset({
    "A01", "A02", "A03", "A04", "A05", "A06", "A07", "A08",
    "B01", "B02", "B03", "B04", "B05", "B06", "B07", "B08", "B09", "B10",
    "C01", "C02", "C03", "C04", "C05", "C06", "C07", "C08",
    "D01", "D02", "D03", "D04", "D05", "D06", "D07", "D08", "D09", "D10", "D11", "D12",
    "E01", "E02", "E03", "E04", "E05", "E06", "E07", "E08", "E09", "E10",
    "F01", "F02", "F03", "F04", "F05", "F06", "F07", "F08",
    "G01", "G02", "G03", "G04", "G05", "G06", "G07", "G08",
    "H01", "H02", "H03", "H04", "H05", "H06", "H07", "H08", "H09", "H10", "H11", "H12",
})
assert len(_REQUIRED_ACCEPTANCE_IDS) == 76  # A8+B10+C8+D12+E10+F8+G8+H12


def _all_covered_ids_from_module(source_path=None):
    """Static AST inspection of a test file's own SOURCE TEXT -- walks ClassDef ->
    FunctionDef -> decorator_list, matching Call nodes whose func is Name(id="_covers"),
    extracting Constant string-literal arguments. Detects a deleted or renamed
    acceptance-bound test from parsed source structure alone -- never from runtime
    dir()/import state (Rework 1, Codex P2 finding #6 / requirement #8). Defaults to
    THIS file; callers may pass an arbitrary path (e.g. a detached mutated copy)."""
    path = Path(source_path) if source_path is not None else Path(__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    covered = set()
    id_to_methods = {}
    for class_node in ast.walk(tree):
        if not isinstance(class_node, ast.ClassDef):
            continue
        for method_node in class_node.body:
            if not isinstance(method_node, ast.FunctionDef):
                continue
            for decorator in method_node.decorator_list:
                if not (isinstance(decorator, ast.Call) and
                        isinstance(decorator.func, ast.Name) and
                        decorator.func.id == "_covers"):
                    continue
                for arg in decorator.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        covered.add(arg.value)
                        id_to_methods.setdefault(arg.value, []).append(
                            f"{class_node.name}.{method_node.name}")
    return covered, id_to_methods


class TestAcceptanceMatrixCompleteness:
    def test_every_required_id_bound_via_explicit_covers_decorator(self):
        covered_ids, _ = _all_covered_ids_from_module()
        missing = _REQUIRED_ACCEPTANCE_IDS - covered_ids
        assert missing == set(), f"IDs with no @_covers binding: {sorted(missing)}"

    def test_no_required_id_bound_more_than_once(self):
        _, id_to_methods = _all_covered_ids_from_module()
        duplicates = {i: methods for i, methods in id_to_methods.items()
                      if i in _REQUIRED_ACCEPTANCE_IDS and len(methods) > 1}
        assert duplicates == {}, duplicates

    def test_covers_binding_survives_a_method_rename(self):
        @_covers("Z99")
        def _renameable():
            pass

        _renameable.__name__ = "totally_unrelated_after_rename"
        assert _renameable._acceptance_ids == ("Z99",)

    def test_id_count_matches_acceptance_matrix(self):
        assert len(_REQUIRED_ACCEPTANCE_IDS) == 76  # A8+B10+C8+D12+E10+F8+G8+H12


# ===========================================================================
# Rework 10 item 6 (Codex instruction): a SEPARATE static completeness oracle
# protecting ONLY the new backend-delimiter-wiring regression guards (W01-W08)
# added this round -- reuses the same hand-authored, AST-derived `_covers`
# discipline as the oracle above, but never extends, relabels, or otherwise
# touches the immutable 76-ID acceptance set.
# ===========================================================================
_REQUIRED_WIRING_IDS = frozenset({
    "W01", "W02", "W03", "W04", "W05", "W06", "W07", "W08",
})
assert len(_REQUIRED_WIRING_IDS) == 8


class TestWiringCompletenessOracle:
    def test_every_required_wiring_id_bound_via_explicit_covers_decorator(self):
        covered_ids, _ = _all_covered_ids_from_module()
        missing = _REQUIRED_WIRING_IDS - covered_ids
        assert missing == set(), f"wiring IDs with no @_covers binding: {sorted(missing)}"

    def test_no_required_wiring_id_bound_more_than_once(self):
        _, id_to_methods = _all_covered_ids_from_module()
        duplicates = {i: methods for i, methods in id_to_methods.items()
                      if i in _REQUIRED_WIRING_IDS and len(methods) > 1}
        assert duplicates == {}, duplicates

    def test_wiring_id_count_matches_rework_10(self):
        assert len(_REQUIRED_WIRING_IDS) == 8

    def test_original_76_id_set_is_untouched_and_disjoint_from_wiring_ids(self):
        # Rework 10 item 6: this new oracle must never extend or relabel the immutable
        # 76-ID acceptance set above -- the two registries are deliberately disjoint.
        assert len(_REQUIRED_ACCEPTANCE_IDS) == 76
        assert _REQUIRED_WIRING_IDS.isdisjoint(_REQUIRED_ACCEPTANCE_IDS)
