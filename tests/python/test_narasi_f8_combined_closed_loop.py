"""F8 — THE MANDATORY COMBINED ACCEPTANCE: nine defects, one job, three real provider lanes.

🔴 WHY THE PREVIOUS VERSION OF THIS FILE WAS NOT AN ACCEPTANCE. It scripted the merged revise
   (`_repair_all` appended one generic bridge and repaired nothing else), it let the critic
   double flip from broken to clean on CALL ORDER rather than on the bytes, it injected the
   structural attribution and the F8 counters by hand, its ceiling reducer was a no-op, and its
   fixture carried none of the four bound markers, no literal unanswered final question, no L3
   semantic violation and no authority-owned term that must survive. Every assertion in it was
   therefore a statement about the test's own scaffolding.

🔴 WHAT THIS FILE DRIVES INSTEAD. One `_run_narration_job_after_parity` call over a
   three-chapter book carrying ALL of:

     1. four ACTIVE canon-bound `[ancN]` markers;
     2. textual tense drift — past / present / past;
     3. a real chapter-2 teleport (an unbridged location jump on the page);
     4. a literal terminal `Stay?` with no answer;
     5. a deposition PROMISED and not executed;
     6. chapter 2 genuinely above the `word_target x 1.1` ceiling THIS SERVER computed;
     7. a structural boundary defect — the 2|3 seam elides causal, location AND time;
     8. an L3 semantic violation — chapter 2 names an entity the canon contradicts;
     9. an authority-owned ledger term, `[buku-besar-7]`, that must survive every lane.

   and repairs them through the REAL production paths:

     structural addressed patch   invalid operation  ->  valid addressed patch   (chapter 3)
     legacy chunked revise        byte-identical no-op ->  real present->past repair (chapter 2)
     bounded ceiling reducer      one attempt, spliced back between server-held blocks
     L3 assist                    armed by the REAL `mode == "assist"` + `_assist_ready`
                                  preflight, never by calling `_canon_lite_l3_assist_repair`

   `_narasi_consistency_revise` is NOT monkeypatched. `_narasi_chapter_reduce` is NOT
   monkeypatched. `_canon_lite_l3_assist_repair` is NOT called directly. The only doubles are
   the OUTBOUND boundaries: one provider client, one metered QC provider, one repair worker,
   and the observer.

🔴 THE OBSERVER IS A FUNCTION OF THE BYTES, NOT OF THE CALL COUNTER. `observe()` below derives
   every census — tense, teleports, beats, seams — from the manuscript it is handed, by a
   lexical rule stated once in this module's own vocabulary. It cannot return `clean` because it
   is the second call; it returns clean only if the delivered bytes are clean. The negative
   controls at the bottom disable ONE repair each and prove the corresponding finding survives
   and blocks delivery — which is only possible because the observer reads the text.

No network, no provider, no database. Every string here is synthetic.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import types
from types import SimpleNamespace

import pytest

REPO_PY = os.path.join(os.path.dirname(__file__), "..", "..", "python")
if REPO_PY not in sys.path:
    sys.path.insert(0, REPO_PY)

import canon_lite as cl                       # noqa: E402
import canon_lite_l2 as l2                    # noqa: E402
import canon_lite_qc_contract as qcc          # noqa: E402
import canon_lite_qc_meter as meter           # noqa: E402
import canon_lite_qc_runner as qcr            # noqa: E402
import narasi_gate as ng                      # noqa: E402
from narasi_addressed_patch import PATCH_SCHEMA_VERSION  # noqa: E402

from test_canon_lite_l3_repair_stage2 import _claims_for  # noqa: E402


def _live(name):
    return sys.modules.get(name) or importlib.import_module(name)


f8 = _live("narasi_f8")

CANARY = "t-canary"

#: Not a credential. Presence is all the activation gate reads; the value is never resolved,
#: compared or transmitted, and this string never leaves the process.
PLACEHOLDER_KEY = "placeholder-not-a-credential"


# ---------------------------------------------------------------------------
# 1. THE FIXTURE'S OWN MACHINE-CHECKABLE VOCABULARY
#
# 🔴 STATED ONCE, READ BY BOTH SIDES. The observer derives its censuses from these tokens and
#    the provider doubles repair by rewriting them. Neither side may hold a private opinion
#    about what "repaired" means, because both are reading the same bytes through the same
#    definitions — which is exactly what makes a negative control possible.
# ---------------------------------------------------------------------------
GOOD_NAME = "Suranto"          # what the canon says the entity is called
BAD_NAME = "Hartono"           # what chapter 2 calls it — the L3 semantic violation

PRESENT = "sekarang"           # a chapter carrying this narrates in the present
PAST = "tadi"                  # ...and this is the tense the rest of the book is in

TELEPORT = "Ia sudah di seberang kota."
TRANSIT = "Ia menempuh jalan panjang itu lalu tiba di seberang kota."

DEPOSITION_PROMISE = "berjanji akan menandatangani deposisi itu"
DEPOSITION_DONE = "membubuhkan tanda tangannya pada deposisi itu"

FINAL_QUESTION = "Stay?"
FINAL_ANSWER = 'Ia menjawab, "Aku tinggal."'

#: The three dimensions a chapter opening must account for. A bridge that supplies one and not
#: the others closes exactly one dimension — which is the whole reason F8 judges them apart.
SEAM_CAUSAL = "Karena keputusan itu"
SEAM_LOCATION = "ia berpindah ke"
SEAM_TIME = "kemudian"

#: The authority-owned ledger reference. It is a bracket token that is NOT a bound id of this
#: canon, so `scrub_bound_markers` must leave it byte for byte — and no gate may target it.
LEDGER = "[buku-besar-7]"

#: The droppable paragraph the bounded reducer removes to bring chapter 2 inside its ceiling.
PADDING = ("Lampu di langit-langit menyala terang dan kursi-kursi kosong berjajar rapi "
           "di sepanjang dinding ruangan dingin yang panjang itu.")

#: Word targets from the REQUEST. `chapter_word_bounds` turns these into (0.9x, 1.1x); chapter
#: 2's body is written deliberately over its ceiling and nothing else is.
WORD_TARGETS = (30, 50, 60)


def _chapter(number, title, body):
    return f"## Bab {number}: {title}\n\n{body}"


#: Chapter 1 — sound, past tense, no marker, no defect, and never targeted by anything. Its
#: bytes are the file's untouched-chapter control.
BODY_1 = (
    f"Malam {PAST} {GOOD_NAME} menutup pintu atap itu pelan sekali.\n\n"
    "Ia menatap kota yang berkedip di bawahnya dan tidak berkata apa pun lagi."
)

#: Chapter 2 — present tense, an unbridged jump, ALL FOUR bound markers, the ledger term, the
#: contradicted name, and a padding paragraph that puts it over its ceiling. Its OPENING already
#: accounts for the 1|2 seam in all three dimensions, so that seam is sound before and after.
#:
#: 🔴 WHY ALL FOUR MARKERS LIVE IN THE CHAPTER L3 REWRITES. The L3-assist candidate is scrubbed
#:    by the session before it is validated (`canon_lite_l3_adapter`), so this chapter's markers
#:    are removed on the HEALTHY path — the one F1's final seam documents it "never sees". That
#:    matters for the negative controls below: a marker parked in a chapter whose only repair a
#:    control disables would survive to the final scrub, change the manuscript AFTER L3 recorded
#:    its binding, and refuse the job as `l3_proof_invalidated_late_mutation` — a real rule,
#:    firing BEFORE F6 and F8, which would mask the finding each control exists to expose.
BODY_2 = (
    f"{SEAM_CAUSAL} {SEAM_LOCATION} ruang rapat, tiga jam {SEAM_TIME}, "
    f"dan ruangan itu penuh {PRESENT}.\n\n"
    f"{BAD_NAME} meletakkan map cokelat [anc1] di atas meja panjang itu {PRESENT}.\n\n"
    f"{TELEPORT}\n\n"
    f"Berkas {LEDGER} itu masih menunggu satu tanda tangan [anc2] dan satu stempel [anc3] "
    "sebelum sidang [anc4].\n\n"
    f"{PADDING}"
)

#: Chapter 3 — past tense, a deposition promised but never executed, a literal terminal
#: question nobody answers, and an OPENING that accounts for none of the three dimensions of
#: the 2|3 seam.
BODY_3 = (
    f"Meja panjang itu tampak kosong dan lampu di langit-langit menyala terang {PAST}.\n\n"
    f"{GOOD_NAME} {DEPOSITION_PROMISE} esok hari.\n\n"
    f"Ia menatap perempuan yang berdiri {PAST} di ambang pintu itu.\n\n"
    "Di luar jendela hujan turun perlahan dan suara langkah di koridor itu berhenti "
    "persis di ambang.\n\n"
    f"{FINAL_QUESTION}"
)

BOOK = "\n\n".join((
    _chapter(1, "Atap", BODY_1),
    _chapter(2, "Ruang Rapat", BODY_2),
    _chapter(3, "Meja Panjang", BODY_3),
)) + "\n"

#: The accepted outline. Chapter 3 carries TWO commitments, so the deposition is a
#: `beat_execution` failure and the final choice is the `final_beat` — two distinct classes
#: rather than one class asserted twice.
OUTLINE = {
    1: ["Suranto menutup pintu atap"],
    2: ["Rapat itu dimulai"],
    3: ["Suranto menandatangani deposisi", "Suranto menjawab pertanyaan terakhir"],
}


def _packet(beats) -> str:
    return ("CURRENT ORDERED OUTLINE BEATS:\n"
            + "".join(f"  {i}. {b}\n" for i, b in enumerate(beats, 1)) + "\n")


AUTHORITY = {
    "text": f"NARRATIVE AUTHORITY: accepted outline. Ledger reference {LEDGER} is authority-owned.",
    "outline_packets_by_chapter": {str(k): _packet(v) for k, v in OUTLINE.items()},
}


# ---------------------------------------------------------------------------
# 2. THE OBSERVER — every census derived from the manuscript it is handed
# ---------------------------------------------------------------------------
def _bodies(text):
    """The prose of each chapter, heading excluded, in chapter order."""
    out = []
    for block in ng.split_chapter_blocks(text or ""):
        heading = ng.chapter_heading_line(block)
        if not heading:
            continue                       # preamble / the gates' `> **Gaya:**` header
        out.append(block[len(heading):])
    return out


def _opening(body):
    """A chapter's first paragraph — the unit a boundary bridge belongs in."""
    return next((p for p in body.split("\n\n") if p.strip()), "")


def observe(text):
    """The critic's answer, computed from `text` and from nothing else.

    🔴 THIS IS THE WHOLE POINT OF THE FILE. A double that returns `broken` on its first call
       and `clean` on its second proves that the job asked twice; it proves nothing about what
       the repair did. Every value below is a lexical fact about the bytes, so removing a
       repair — as the negative controls do — necessarily changes what comes back.
    """
    bodies = _bodies(text)
    n = len(bodies)
    tense = ["present" if PRESENT in b else "past" for b in bodies]
    teleports = [b.count(TELEPORT) for b in bodies]

    beats = [{"chapter": 1, "beat": 1, "state": "executed"},
             {"chapter": 2, "beat": 1, "state": "executed"},
             {"chapter": 3, "beat": 1,
              "state": ("executed" if len(bodies) >= 3 and DEPOSITION_DONE in bodies[2]
                        else "promised")},
             {"chapter": 3, "beat": 2,
              "state": ("executed" if len(bodies) >= 3 and FINAL_ANSWER in bodies[2]
                        else "promised")}]

    seams = []
    for a in range(1, n):
        opening = _opening(bodies[a])          # the LATER chapter's opening
        seams.append({
            "chapter_a": a, "chapter_b": a + 1,
            "causal": "explicit" if SEAM_CAUSAL in opening else "missing",
            "location": "explicit" if SEAM_LOCATION in opening else "missing",
            "time": "explicit" if SEAM_TIME in opening else "missing",
        })
    return {"score": 8, "violations": [], "tense_by_chapter": tense,
            "teleports_by_chapter": teleports, "beat_states": beats,
            "seam_states": seams}


# ---------------------------------------------------------------------------
# 3. THE PROVIDER DOUBLE — one client, three real lanes, the mandated sequences
# ---------------------------------------------------------------------------
class Lanes:
    """One deterministic client serving the structural, legacy and reducer lanes.

    Each lane is recognised by the SHAPE of its own request — strict JSON for the addressed
    patch, the chapter marker for the legacy revise, the exact word range for the reducer — so no
    lane is simulated: all three are the real production code asking in their own voice.
    """

    def __init__(self, *, repair_tense=True, repair_teleport=True, repair_seam=True,
                 repair_beats=True, reduce_chapter=True):
        self.structural = self.legacy = self.reduce = self.foreign = 0
        #: Every prompt each lane actually received. The directives inside them are the only
        #: place the MERGE of F6's and F8's findings is observable without scripting the
        #: revise — which this file may not do.
        self.prompts: dict = {"structural": [], "legacy": [], "reduce": []}
        self.systems: dict = {"legacy": []}
        self.repair_tense = repair_tense
        self.repair_teleport = repair_teleport
        self.repair_seam = repair_seam
        self.repair_beats = repair_beats
        self.reduce_chapter = reduce_chapter

    # -- the three lanes ----------------------------------------------------
    def create(self, **kwargs):
        user = kwargs["messages"][-1]["content"]
        # 🔴 ROUTED ON THE LANE'S OWN MARKER, NOT ON `response_format`. Several report-only
        #    gates issue book-sized `_narasi_cheap_call`s in JSON mode; matching on the JSON
        #    flag alone served one of them an addressed patch and counted it as a structural
        #    exchange, which is exactly the kind of miscount this file exists to refuse.
        if "[ORIGINAL ADDRESS TABLE]\n" in user:
            return self._structural(user)
        if "WORD RANGE: between" in user and "CHAPTER BODY:\n" in user:
            return self._reduce(user)
        if _LEGACY_MARKER in user:
            return self._legacy(user, kwargs["messages"][0]["content"])
        # Any other request belongs to a report-only gate. Answered with an empty object so
        # the gate degrades exactly as it does against a provider that says nothing useful,
        # and RECORDED so it can never be mistaken for one of the three lanes.
        self.foreign += 1
        return _reply("{}")

    def _structural(self, prompt):
        self.structural += 1
        self.prompts["structural"].append(prompt)
        if self.structural == 1:
            # 🔴 SEQUENCE STEP 1 — an INVALID operation. The lane must refuse it, count the
            #    exchange, and spend its ONE bounded corrective retry.
            return _reply(json.dumps({
                "schema_version": PATCH_SCHEMA_VERSION,
                "operations": [{"op": "obliterate", "unit_id": "u001", "text": "x"}]}))
        units = _units(prompt)
        operations = []
        if self.repair_seam:
            # 🔴 THE REWRITTEN PROSE DOES NOT ECHO AN INTERNAL ID. A chapter writer that
            #    returns a bound marker is the leak F1 exists to catch; a chapter writer that
            #    returns repaired prose without one is the healthy path, and it is the path a
            #    combined acceptance has to model — F1's final seam explicitly documents that
            #    it "never sees this" unless both earlier defences already failed.
            operations.append({
                "op": "replace", "unit_id": "u001",
                "text": (f"{SEAM_CAUSAL} {SEAM_LOCATION} ruang sidang, dua hari {SEAM_TIME}. "
                         + _strip_markers(units["u001"]))})
        if self.repair_beats:
            # Addressed by CONTENT, not by a hardcoded ordinal: the unit table is the
            # server's, and a fixture that assumes its numbering silently stops repairing
            # the thing it names the moment a paragraph is added.
            promise = _find_unit(units, DEPOSITION_PROMISE)
            question = _find_unit(units, FINAL_QUESTION)
            operations.append({
                "op": "replace", "unit_id": promise,
                "text": _strip_markers(
                    units[promise].replace(DEPOSITION_PROMISE, DEPOSITION_DONE))})
            operations.append({
                "op": "replace", "unit_id": question,
                "text": f"{FINAL_QUESTION} {FINAL_ANSWER}"})
        return _reply(json.dumps({"schema_version": PATCH_SCHEMA_VERSION,
                                  "operations": operations}))

    def _legacy(self, prompt, system):
        self.legacy += 1
        self.prompts["legacy"].append(prompt)
        self.systems["legacy"].append(system)
        chapter = prompt.split(_LEGACY_MARKER, 1)[1].split("\n\n[CORRECTION]\n", 1)[0]
        if self.legacy == 1:
            # 🔴 SEQUENCE STEP 1 — a byte-identical no-op. F2 requires this be refused as
            #    `ineffective` and retried, never counted as `revised`.
            return _reply(chapter)
        fixed = chapter
        if self.repair_tense:
            fixed = fixed.replace(PRESENT, PAST)
        if self.repair_teleport:
            fixed = fixed.replace(TELEPORT, TRANSIT)
        return _reply(fixed)

    def _reduce(self, prompt):
        self.reduce += 1
        self.prompts["reduce"].append(prompt)
        body = prompt.split("CHAPTER BODY:\n", 1)[1].split(
            "\n\nCORRECTION TO YOUR PREVIOUS ATTEMPT:", 1)[0]
        if not self.reduce_chapter:
            return _reply(body)
        # Drop the one droppable paragraph — every event, marker and the ledger term stay.
        kept = [p for p in body.split("\n\n") if PADDING not in p]
        return _reply("\n\n".join(kept).strip())


_LEGACY_MARKER = "[CHAPTER — return the corrected version, unchanged except for the fixes]\n"


def _strip_markers(text):
    """Prose as a writer that does not echo an internal id would return it.

    Only the four BOUND markers, and never the authority-owned ledger term beside them —
    which is the discrimination `scrub_bound_markers` makes and this fixture must not do
    for it.
    """
    for marker in BOUND_MARKERS:
        text = text.replace(f" {marker}", "").replace(f"{marker} ", "").replace(marker, "")
    return text


def _find_unit(units, needle):
    return next(uid for uid, text in units.items() if needle in text)


def _units(prompt):
    """The `[uNNN]` address table the structural prompt carries, as {id: text}."""
    table = prompt.split("[ORIGINAL ADDRESS TABLE]\n", 1)[1].split("\n\n[RESPONSE SHAPE]", 1)[0]
    out, current, buf = {}, None, []
    for line in table.split("\n"):
        if line.startswith("[u") and line.endswith("]"):
            if current:
                out[current] = "\n".join(buf).strip()
            current, buf = line[1:-1], []
        else:
            buf.append(line)
    if current:
        out[current] = "\n".join(buf).strip()
    return out


def _reply(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content),
                                 finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))


# ---------------------------------------------------------------------------
# 4. THE CANON — three chapters, one entity, four bound anchors
# ---------------------------------------------------------------------------
def build_canon():
    chapters = tuple(
        cl.CanonChapterV1(chapter_id=f"ch{i}", order=i, expected_title=cl.UNKNOWN)
        for i in (1, 2, 3))
    anchors = (
        cl.CanonAnchorV1(anchor_id="anc1", kind="quantity", literal="satu map cokelat"),
        cl.CanonAnchorV1(anchor_id="anc2", kind="quantity", literal="satu tanda tangan"),
        cl.CanonAnchorV1(anchor_id="anc3", kind="time", literal="esok hari"),
        cl.CanonAnchorV1(anchor_id="anc4", kind="time", literal="malam itu"),
    )
    return cl._finalize({
        "schema_version": cl.SCHEMA_VERSION,
        "outline_sha256": "a" * 64, "generation_config_sha256": "b" * 64,
        "target_language": "id", "chapters": chapters,
        "entities": (cl.CanonEntityV1(entity_id="e1", canonical_name=GOOD_NAME,
                                      aliases=(), alias_source="none"),),
        "anchors": anchors, "one_time_events": (), "reveals": (),
        "flashback_exceptions": (),
        "fact_source_policy": "fiction_generated",
        "advisory_bible_sha256": cl.UNKNOWN,
    })


BOUND_MARKERS = ("[anc1]", "[anc2]", "[anc3]", "[anc4]")


# ---------------------------------------------------------------------------
# 5. THE JOB — driven exactly as production drives it
# ---------------------------------------------------------------------------
@pytest.fixture
def metered_host():
    """Claim the metered-host role, as narration-worker's boot code does."""
    meter.reset_host_role_for_tests()
    meter.declare_host_role(meter.HOST_ROLE_NARRATION_WORKER)
    yield
    meter.reset_host_role_for_tests()


@pytest.fixture
def job(monkeypatch, metered_host):
    lz, na = _live("laozhang_api"), _live("narration_api")
    import orchestrator.static as st

    def run(*, lanes=None, repair_semantics=True, word_targets=WORD_TARGETS, drop_env=()):
        lanes = lanes if lanes is not None else Lanes()
        canon = build_canon()
        seen = {"finalize": [], "persisted": None, "critique": [], "refund": 0,
                "payload": None, "waves": 0, "repairs": 0, "extractions": 0,
                "routed": [], "lanes": lanes, "canon": canon}

        async def _anoop(*_a, **_k):
            return None

        async def _finalize(job_id, job_uuid, tenant_id, *, status, result=None, error=None):
            seen["finalize"].append({"status": status, "error": error})
            if result is not None:
                seen["payload"] = dict(result)

        async def _refund(*_a, **_k):
            seen["refund"] += 1

        async def _persist(_tenant, _job_uuid, res, *_a, **_k):
            seen["persisted"] = dict(res or {})

        # ── the observer, installed at the critic seam ────────────────────────
        async def critique(full_text, *_a, authority_text="", **_k):
            seen["critique"].append(full_text)
            out = observe(full_text)
            if not str(authority_text or "").strip():
                out.pop("beat_states", None)
            return (out, 0)

        # ── the ONE outbound client, serving all three real lanes ─────────────
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=lanes),
            with_options=lambda **_k: SimpleNamespace(
                chat=SimpleNamespace(completions=lanes)))

        async def _usage(*_a, **_k):
            return 0

        async def _gen(_req, **_kw):
            bodies = [seg.split("\n", 1)[1].rstrip("\n") for seg in BOOK.split("## ")[1:]]
            # 🔴 0-BASED, as `orchestrator.static` builds them and as
            #    `_l3_sync_chapter_records` reads them (`set(by_index) == set(range(n))`).
            return {"ok": True, "book": BOOK,
                    "chapters": [{"no": i, "ok": True, "content": body}
                                 for i, body in enumerate(bodies)],
                    "n_ok": 3, "n_total": 3,
                    "_canon_lite_canon": canon,
                    "_narrative_authority": dict(AUTHORITY)}

        # ── L3: the metered wave hands its session over; claims come from the
        #    snapshot the seam actually built, so they describe the CURRENT bytes.
        async def _fake_wave(snapshot, _canon, *_a, on_session=None, **_k):
            seen["waves"] += 1
            assert on_session is not None, "the seam stopped passing on_session"
            on_session(_metered)
            claims = {}
            for i in range(len(snapshot.blocks)):
                raw = snapshot.block_bytes(i)
                needle = BAD_NAME if BAD_NAME.encode() in raw else GOOD_NAME
                claims[i] = _claims_for(raw, index=i,
                                        chapter_id=snapshot.blocks[i].chapter_id,
                                        canon=_canon, needle=needle)
            seen["snapshot"] = snapshot
            return claims

        async def _metered(request):
            seen["extractions"] += 1
            body = request.chapter_bytes
            start = body.find(GOOD_NAME.encode("utf-8"))
            assert start >= 0, "the candidate handed to re-extraction was not repaired"
            end = start + len(GOOD_NAME.encode("utf-8"))
            atom_start = next(a.index for a in request.chapter_atoms if a.byte_start == start)
            atom_end = next(a.index for a in request.chapter_atoms if a.byte_end == end)
            return {
                "coverage": {p: (l2.COVERAGE_CHECKED if p == "entity_name_contradiction"
                                 else l2.COVERAGE_NO_CLAIMS_FOUND)
                             for p in l2.SEMANTIC_PREDICATES},
                "claims": [{"claim_type": l2.CLAIM_ENTITY_MENTION, "canon_ref": "e1",
                            "atom_start": atom_start, "atom_end": atom_end}],
            }

        async def _fake_run_worker(_worker, prompt, timeout=None, task_id=None):
            seen["repairs"] += 1
            snapshot = seen["snapshot"]
            index = next(i for i in range(len(snapshot.blocks))
                         if BAD_NAME.encode() in snapshot.block_bytes(i))
            body = snapshot.block_bytes(index).decode("utf-8")
            if not repair_semantics:
                return {"ok": True, "output": body}
            return {"ok": True, "output": body.replace(BAD_NAME, GOOD_NAME)}

        # ── activation: the REAL preflight, cleared by a real environment ─────
        for name, value in ((cl.MODE_ENV_VAR, "assist"),
                            (cl.ASSIST_TENANTS_ENV_VAR, CANARY),
                            (meter.EXTRACTOR_CONCURRENCY_ENV, "4"),
                            ("L2B_MAX_INFLIGHT", "8"),
                            (qcc.QC_API_KEY_ENV, PLACEHOLDER_KEY)):
            if name in drop_env:
                monkeypatch.delenv(name, raising=False)
                continue
            monkeypatch.setenv(name, value)
        monkeypatch.delenv("NARASI_OBSERVABILITY_ENABLED", raising=False)
        # The hooks stay None — production installs neither, so the seam must build its
        # own `L3AssistSession` from the wave's provider.
        monkeypatch.setattr(na, "_L3_REPAIR_PROVIDER", None)
        monkeypatch.setattr(na, "_L3_CHAPTER_EXTRACTOR", None)
        monkeypatch.setattr(qcr, "maybe_run_metered_wave", _fake_wave)
        monkeypatch.setattr(st, "run_worker", _fake_run_worker)

        monkeypatch.setenv("NARASI_DIET_MAX_LOOPS", "0")
        monkeypatch.setenv("NARASI_REGISTER_GATE", "0")
        monkeypatch.setenv("NARASI_THREAD_TRACKER", "0")
        monkeypatch.delenv("NARASI_F6_ENABLED", raising=False)
        monkeypatch.setattr(lz, "make_narasi_client", lambda *a, **k: client)
        monkeypatch.setattr(lz, "_log_narasi_usage", _usage)
        monkeypatch.setattr(lz, "_narasi_revise_timeout", lambda *a: 5.0)
        # F6 owns its census: the legacy critic is OFF, which is the default configuration.
        monkeypatch.setattr(lz, "_narasi_critique_enabled", lambda: False)
        monkeypatch.setattr(lz, "_narasi_critique_revise_enabled", lambda: False)
        monkeypatch.setattr(lz, "NARASI_CRITIQUE_MIN_CHAPTERS", 1)
        monkeypatch.setattr(lz, "_narasi_consistency_critique", critique)
        monkeypatch.setattr(na, "generate_narration", _gen)
        monkeypatch.setattr(na, "_cancel_watcher", lambda *a, **k: asyncio.sleep(3600))
        monkeypatch.setattr(na, "_finalize", _finalize)
        monkeypatch.setattr(na, "_set_status", _anoop)
        monkeypatch.setattr(na, "_safe_progress", _anoop)
        monkeypatch.setattr(na, "_settle", _anoop)
        monkeypatch.setattr(na, "_refund", _refund)
        monkeypatch.setattr(na, "_reconcile_checkboxes", _anoop)
        monkeypatch.setattr(na, "_persist_chapters", _persist)
        monkeypatch.setattr(na, "credits_lib", types.SimpleNamespace(touch_hold=_anoop))
        monkeypatch.setattr(na, "db", types.SimpleNamespace(
            get_known_bad_claims=_anoop, get_known_good_claims=_anoop, log_usage=_anoop,
            checkpoint_narasi_meter=_anoop))

        async def drive():
            before = set(asyncio.all_tasks())
            await na._run_narration_job_after_parity(
                body={"chapters": [{"word_target": w} for w in word_targets],
                      "style": "storytelling", "language": "id"},
                job_id="j-combined", job_uuid=None, tenant_id=CANARY, user_id="u",
                total=3, meter_op="op", model="m", executor="narration_worker")
            survivors = (set(asyncio.all_tasks()) - before) - {asyncio.current_task()}
            if survivors:
                await asyncio.gather(*survivors, return_exceptions=True)

        asyncio.run(drive())
        return seen

    return run


def _payload(seen):
    return seen["payload"] or {}


def _status(seen):
    return seen["finalize"][-1]["status"] if seen["finalize"] else None


def _delivered(seen):
    return _payload(seen).get("markdown") or ""


# ---------------------------------------------------------------------------
# 6. THE COMBINED ACCEPTANCE
# ---------------------------------------------------------------------------
def test_the_fixture_really_carries_all_nine_defects():
    """🔴 A NEGATIVE CONTROL FOR THE FIXTURE ITSELF. Everything below asserts that nine
    defects were repaired; none of it means anything unless they were there to begin with."""
    canon = build_canon()
    bodies = _bodies(BOOK)
    census = observe(BOOK)

    assert len(bodies) == 3
    # 1 — four ACTIVE bound markers
    assert set(cl.detect_bound_markers(BOOK, canon)) == {"anc1", "anc2", "anc3", "anc4"}
    # 2 — past / present / past
    assert census["tense_by_chapter"] == ["past", "present", "past"]
    # 3 — a chapter-2 teleport
    assert census["teleports_by_chapter"] == [0, 1, 0]
    # 4 — a literal terminal question with no answer
    assert BOOK.rstrip().endswith(FINAL_QUESTION)
    assert FINAL_ANSWER not in BOOK
    # 5 — a deposition promised, not executed
    assert DEPOSITION_PROMISE in BOOK and DEPOSITION_DONE not in BOOK
    # 6 — chapter 2 above the ceiling THIS SERVER computes from the request
    import narasi_f6 as nf6
    bounds = nf6.chapter_word_bounds(
        {"chapters": [{"word_target": w} for w in WORD_TARGETS]}, 3)
    counts = ng.chapter_word_counts(BOOK)
    assert counts[1] > bounds[2][1], (counts, bounds)
    assert counts[0] <= bounds[1][1] and counts[2] <= bounds[3][1], (counts, bounds)
    # 7 — the 2|3 seam elides all three dimensions; the 1|2 seam is sound
    assert census["seam_states"][0] == {"chapter_a": 1, "chapter_b": 2, "causal": "explicit",
                                        "location": "explicit", "time": "explicit"}
    assert census["seam_states"][1] == {"chapter_a": 2, "chapter_b": 3, "causal": "missing",
                                        "location": "missing", "time": "missing"}
    # 8 — an L3 semantic violation
    assert BAD_NAME in bodies[1]
    # 9 — an authority-owned ledger term that is NOT a bound id
    assert LEDGER in BOOK
    assert "buku-besar-7" not in cl.canon_bound_marker_ids(canon)


def test_every_defect_is_repaired_through_the_real_lanes_and_the_book_is_delivered(job):
    """🔴 THE MANDATORY COMBINED ACCEPTANCE, asserted over the DELIVERED BYTES."""
    seen = job()
    delivered = _delivered(seen)
    payload = _payload(seen)
    canon = seen["canon"]

    assert _status(seen) == "done", seen["finalize"]
    assert seen["refund"] == 0, "a delivered job must not be refunded"

    # -- the manuscript moved, and it is still the same book --------------------
    assert delivered and delivered != BOOK
    assert ng.chapter_ordinal_sequence(delivered) == [1, 2, 3]

    # -- 1. no active bound marker survives; the ledger term does ---------------
    assert cl.detect_bound_markers(delivered, canon) == ()
    for marker in BOUND_MARKERS:
        assert marker not in delivered, marker
    assert LEDGER in delivered, "the authority-owned ledger term was repaired away"

    # -- 2. tense is consistent -------------------------------------------------
    after = observe(delivered)
    assert after["tense_by_chapter"] == ["past", "past", "past"], after

    # -- 3. the teleport is gone, and it is BRIDGED rather than deleted ---------
    assert after["teleports_by_chapter"] == [0, 0, 0]
    assert TELEPORT not in delivered
    assert TRANSIT in delivered

    # -- 4/5. the deposition is executed and the final choice is explicit -------
    assert DEPOSITION_DONE in delivered and DEPOSITION_PROMISE not in delivered
    assert FINAL_ANSWER in delivered
    assert all(b["state"] == "executed" for b in after["beat_states"]), after["beat_states"]

    # -- 7. both seams are explicit in all three dimensions ---------------------
    for row in after["seam_states"]:
        assert (row["causal"], row["location"], row["time"]) == (
            "explicit", "explicit", "explicit"), row

    # -- 8. the semantic violation is gone from BOTH live copies ----------------
    assert BAD_NAME not in delivered
    rows = [c["content"] for c in (seen["persisted"] or {}).get("chapters", [])]
    assert rows and not any(BAD_NAME in r for r in rows)

    # -- the untouched chapter is byte-identical --------------------------------
    assert _bodies(delivered)[0].strip() == BODY_1

    # -- delivery is allowed, on both gates' own books --------------------------
    f6 = payload.get("f6") or {}
    f8b = payload.get("f8") or {}
    assert f6.get("violations_unresolved") == 0, f6
    assert f6.get("delivery_blocked") is False, f6
    assert f8b.get("seams_unresolved") == 0, f8b
    assert f8b.get("delivery_blocked") is False, f8b


def test_every_initial_hard_finding_is_resolved_and_the_lanes_account_for_it(job):
    """🔴 REAL LANE ACCOUNTING, NOT AN INJECTED ONE. Every counter below is published by the
    production lane itself; nothing in this file writes `_f8_structural_*`."""
    seen = job()
    payload = _payload(seen)
    lanes = seen["lanes"]
    f6 = payload.get("f6") or {}
    f8b = payload.get("f8") or {}

    # -- the mandated provider sequences actually ran ---------------------------
    assert lanes.structural == 2, "the invalid structural operation was never refused+retried"
    assert lanes.legacy == 4, \
        "the mixed and cheap-teleport chapter lanes must each stay bounded to one retry"
    assert lanes.reduce == 1, "the bounded ceiling reduction is exactly one attempt"

    # -- F6: five classes detected, every one resolved --------------------------
    assert f6.get("violations_detected") >= 5, f6
    assert f6.get("violations_resolved") == f6.get("violations_detected"), f6
    assert f6.get("violations_unresolved") == 0, f6
    assert f6.get("collateral_chapters") == [], f6

    # -- F8: one seam, attributed to an accepted OPENING-unit operation ---------
    assert f8b.get("seams_detected") == 1, f8b
    assert f8b.get("seams_resolved") == 1, f8b
    assert f8b.get("byte_changing_repairs") >= 1, f8b
    assert f8b.get("collateral_chapters") == [], f8b
    assert f8b.get("new_defects") == [], f8b

    # -- the lanes' OWN published accounting ------------------------------------
    persisted = seen["persisted"] or {}
    legacy = persisted.get("legacy_revise") or {}
    assert legacy.get("chapters_changed", 0) >= 1, legacy
    assert legacy.get("ineffective_count", 0) >= 1, \
        "the byte-identical no-op was never recorded as ineffective"
    assert legacy.get("provider_calls", 0) >= 2, legacy

    structural = persisted.get("structural_patch") or {}
    assert structural.get("chapters_accepted", 0) >= 1, structural
    assert structural.get("schema_retry_chapters", 0) == 1, structural
    assert structural.get("schema_retry_accepted", 0) == 1, structural
    assert structural.get("schema_retry_exhausted", 0) == 0, structural

    # -- a report-only gate's own cheap call is not one of the three lanes ------
    assert lanes.foreign >= 0
    assert lanes.structural + lanes.legacy + lanes.reduce == 7


# ---------------------------------------------------------------------------
# 7. THE BOUNDED CEILING REDUCTION — a real reduction, measured on the bytes
# ---------------------------------------------------------------------------
def test_the_ceiling_reduction_brings_chapter_two_into_band_without_moving_the_contract(job):
    import narasi_f6 as nf6
    body = {"chapters": [{"word_target": w} for w in WORD_TARGETS]}
    bounds = nf6.chapter_word_bounds(body, 3)
    before = ng.chapter_word_counts(BOOK)
    assert before[1] > bounds[2][1], "the fixture must start over the ceiling"

    seen = job()
    delivered = _delivered(seen)
    after = ng.chapter_word_counts(delivered)

    assert len(after) == 3, "the reduction changed the chapter count"
    assert bounds[2][0] <= after[1] <= bounds[2][1], (after, bounds)
    assert nf6.chapter_word_bounds(body, 3) == bounds, "the requested target moved"
    assert seen["lanes"].reduce == 1


# ---------------------------------------------------------------------------
# 8. NEGATIVE CONTROLS — remove ONE repair, prove its finding survives and blocks
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("knob,gate,error,unresolved", [
    ("repair_tense", "f6", "f6_unresolved_hard_violation", ["tense_drift:2"]),
    ("repair_teleport", "f6", "f6_unresolved_hard_violation",
     ["teleport:2:teleport_instance:2|1"]),
    ("repair_beats", "f6", "f6_unresolved_hard_violation",
     ["beat_execution:3:outline_beat:3|1", "final_beat:3:outline_beat:3|2"]),
    ("reduce_chapter", "f6", "f6_unresolved_hard_violation", ["chapter_ceiling:2"]),
    ("repair_seam", "f8", "f8_unresolved_chapter_seam", ["seam:2|3"]),
])
def test_removing_one_repair_leaves_its_finding_unresolved_and_blocks_delivery(
        job, knob, gate, error, unresolved):
    """🔴 THE OBSERVER READS THE BYTES, AND THIS IS HOW THAT IS PROVED. Each row disables
    exactly one repair. If the double were flipping to clean on its second call, every one of
    these would still deliver — the manuscript would be broken and the census would say
    otherwise. They block instead, each on its own gate, and the accounting names the exact
    finding that was left in the bytes."""
    seen = job(lanes=Lanes(**{knob: False}))
    assert _status(seen) == "failed", (knob, seen["finalize"])
    assert seen["finalize"][-1]["error"] == error, (knob, seen["finalize"])
    assert seen["refund"] == 1, "a refused job must be refunded"
    assert seen["persisted"] is None, "a refused job must never reach persistence"

    accounting = _payload(seen).get(gate) or {}
    still_open = accounting.get("unresolved_ids") or []
    for identity in unresolved:
        assert identity in still_open, (knob, identity, accounting)


def test_removing_the_semantic_repair_leaves_the_contradiction_in_the_delivered_bytes(job):
    """The L3 negative control. The seam still RUNS — the preflight cleared and the metered
    wave executed — but the worker declines to repair, so nothing is accepted, nothing is
    substituted, and the contradicted name is still in the bytes.

    🔴 AND THE JOB DOES NOT SHIP. Declining also means the candidate scrub never runs, so the
    four bound markers survive to F1's final seam, which removes them AFTER L3 recorded its
    binding — a late mutation, refused as `l3_proof_invalidated_late_mutation`. That is F1's
    rule doing exactly what it is for, and it is asserted rather than glossed."""
    seen = job(repair_semantics=False)
    delivered = _delivered(seen)
    assert seen["waves"] == 1, "the real assist preflight did not arm the metered wave"
    assert seen["repairs"] >= 1, "the repair worker was never asked"
    assert BAD_NAME in delivered, "the fixture's semantic violation was never in play"
    assert _status(seen) == "failed"
    assert seen["finalize"][-1]["error"] == "l3_proof_invalidated_late_mutation"


def test_a_deployment_that_cannot_run_assist_never_arms_the_wave(job):
    """🔴 THE PROOF THAT THE REAL PREFLIGHT IS THE GATE. Remove the ONE credential the
    activation gate reads and nothing else changes — same tenant, same mode, same allowlist.
    The wave must never run, no repair candidate may be generated, and the job must record the
    bounded cause instead of discovering it after the money is spent. Without this row,
    "the wave ran" is not evidence that `assist_activation_ready` was ever consulted."""
    seen = job(drop_env=(qcc.QC_API_KEY_ENV,))

    assert seen["waves"] == 0, "the metered wave ran on a deployment that cannot run assist"
    assert seen["repairs"] == 0, "a repair was generated with no activation"
    record = (seen["persisted"] or {}).get("canon_lite_l3") or {}
    assert record.get("requested_mode") == "assist", record
    assert record.get("effective_mode") == "off", record
    assert record.get("outcome") == "unchecked", record
    assert record.get("reason_code"), "the bounded cause was not recorded"
    # The legacy path still delivers: assist being unavailable is not a delivery failure.
    assert _status(seen) == "done", seen["finalize"]


def test_the_l3_arm_is_armed_by_the_real_preflight_and_lands_its_stripped_chapter(job):
    """🔴 ACTIVATION THROUGH THE PRODUCTION PATH. `_canon_lite_l3_assist_repair` is never
    called by this file: the job reaches it only because `_cl_effective_mode` says assist and
    the real `assist_activation_ready` preflight cleared. The hooks stay None, so the seam
    builds its own `L3AssistSession` from the wave's provider — the branch production takes."""
    na = _live("narration_api")
    seen = job()

    assert na._L3_REPAIR_PROVIDER is None and na._L3_CHAPTER_EXTRACTOR is None
    assert seen["waves"] == 1, "the metered wave never ran — assist was not armed"
    assert seen["repairs"] >= 1, "no repair candidate was generated"
    assert seen["extractions"] >= 1, "re-extraction did not ride the job's metered session"

    persisted = seen["persisted"] or {}
    record = persisted.get("canon_lite_l3") or {}
    assert record.get("stage") == "complete", record
    assert record.get("chapters_repaired", 0) >= 1, record
    assert record.get("manuscript_changed") is True, record
    assert record.get("delivery_binding") == l2.BINDING_MATCH, record

    # 🔴 THE CANDIDATE LANDED, AND IT LANDED STRIPPED. The session scrubs the repair
    #    candidate before the validator sees it, so all four bound markers leave with it —
    #    and the book delivered below carries neither them nor the contradiction.
    assert persisted.get("f1_l3_candidate_markers_removed") == len(BOUND_MARKERS)
    delivered = _delivered(seen)
    assert GOOD_NAME in _bodies(delivered)[1] and BAD_NAME not in delivered
    # 🔴 THE HASH IN THE RECORD DESCRIBES THE MANUSCRIPT THAT SHIPPED. Nothing after the L3
    #    seam moved the delivered bytes, so the verdict's own evidence and the book a reader
    #    opens are the same object — which is what `BINDING_MATCH` is claiming.
    assert record.get("manuscript_sha256") == cl.sha256_hex(delivered.encode("utf-8"))
    assert "delivered_manuscript_sha256" not in record, \
        "a late mutation was recorded on a job whose bytes never moved after L3"


def test_the_two_gates_share_one_bounded_post_repair_read(job):
    """🔴 ONE DETECTION PASS, BOUNDED REPAIR LANES, ONE VERIFICATION PASS.

    F6/F8 structural work remains merged. Teleport is deliberately isolated on cheap Claude,
    so an unrelated critic finding cannot promote it to Opus or let a generic prompt ignore the
    server census. The assertion is on the real directives each lane received."""
    seen = job()
    lanes = seen["lanes"]

    assert len(seen["critique"]) == 2, "F6 and F8 must share ONE post-repair read"
    assert lanes.structural == 2 and lanes.legacy == 4, \
        "one mixed repair plus one independently bounded teleport repair"

    structural_prompt = lanes.prompts["structural"][-1]
    mixed_legacy_prompt = lanes.prompts["legacy"][1]
    teleport_prompt = lanes.prompts["legacy"][-1]

    # F8's seam and F6's beats reached the SAME structural request.
    assert "chapter_boundary_break" in structural_prompt, \
        "F8's seam never reached the merged revise"
    assert "outline_missing_beat" in structural_prompt, \
        "F6's beat findings never reached the merged revise"
    # F6 tense remains in the mixed legacy half; teleport gets its own mandatory request.
    assert "tense_drift" in mixed_legacy_prompt and "spatial" not in mixed_legacy_prompt
    assert "spatial" in teleport_prompt and "tense_drift" not in teleport_prompt
    assert "F6 SERVER-MEASURED REPAIR" in lanes.systems["legacy"][-1]


def test_the_verifier_reads_the_repaired_bytes_not_the_call_counter(job):
    """🔴 THE DIRECT STATEMENT OF REQUIREMENT 4. The second critique is handed the REPAIRED
    manuscript, and what it answers is a function of that text: run the same observer over the
    same bytes in the opposite order and it gives the same answers. A double keyed on call
    order cannot satisfy both halves of this."""
    seen = job()
    calls = seen["critique"]
    assert len(calls) == 2, "detection and the post-repair read are one call each"
    assert calls[0] != calls[1], "the second read was handed the same bytes as the first"

    first, second = observe(calls[0]), observe(calls[1])
    assert first["tense_by_chapter"] == ["past", "present", "past"]
    assert second["tense_by_chapter"] == ["past", "past", "past"]
    # Order-independence, stated as an equality rather than implied.
    assert observe(calls[1]) == second and observe(calls[0]) == first

    # And the bytes it read are the bytes that shipped.
    assert calls[1] == _delivered(seen)


# ---------------------------------------------------------------------------
# 9. THE TWO WIRING RULES THIS ACCEPTANCE EXPOSED
# ---------------------------------------------------------------------------
def _scan(text):
    import narasi_f6 as nf6
    na = _live("narration_api")
    return na._f6_scan(
        text=text, chapter_count=3, observation=observe(text),
        outline_sizes={k: len(v) for k, v in OUTLINE.items()},
        bounds=nf6.chapter_word_bounds(
            {"chapters": [{"word_target": w} for w in WORD_TARGETS]}, 3),
        expected_chapters=3)


def test_a_routed_tense_finding_carries_the_locator_the_legacy_lane_reads():
    """🔴 THE DEFECT THIS ACCEPTANCE FOUND. `_narasi_revise_chunked` keeps a violation only
    if it has a quoted manuscript span OR a machine-authored `@chN` locator; F6's evidence is
    a server-authored summary and never a quote. Without the locator the finding was detected,
    merged into the revise request, and then dropped by the lane ("UNMAPPED evidence heads") —
    so no chapter was ever targeted and every book carrying tense drift was refused."""
    found = [v for v in _scan(BOOK)["violations"] if v["f6_class"] == "tense_drift"]
    assert found, "the fixture must drift"
    for violation in found:
        assert f"@ch{violation['chapter']}" in violation["evidence"], violation


def test_a_routed_teleport_finding_carries_the_locator_the_legacy_lane_reads():
    """The same rule, for the other class that rides the legacy lane."""
    found = [v for v in _scan(BOOK)["violations"] if v["f6_class"] == "teleport"]
    assert found, "the fixture must teleport"
    for violation in found:
        assert f"@ch{violation['chapter']}" in violation["evidence"], violation


def test_the_chapter_records_are_re_derived_from_the_repaired_book(job):
    """🔴 ONE BOOK, NOT TWO. `_persist_chapters` stores these rows and a reader can open
    them; the gates repair the assembled book and leave the rows where generation left them.
    Without the re-sync the two disagree — and `_l3_sync_chapter_records` then refuses to
    substitute at all, which is how the L3 repair silently stopped landing on any book whose
    earlier gates had repaired anything."""
    seen = job()
    persisted = seen["persisted"] or {}
    rows = [c["content"] for c in persisted.get("chapters", [])]
    delivered = _delivered(seen)

    assert len(rows) == 3
    for index, (row, body) in enumerate(zip(rows, _bodies(delivered))):
        assert row == body.strip(), f"chapter row {index} is not the text in the book"
        assert row in delivered


def test_the_re_sync_refuses_records_that_are_not_a_census():
    """A duplicate, a gap, a non-int `no`, or a count that disagrees with the delivered
    blocks means the rows cannot be indexed — and prose written into a row nobody can
    address is worse than a stale row."""
    na = _live("narration_api")
    book = "## Bab 1: A\n\nsatu.\n\n## Bab 2: B\n\ndua.\n"
    for records in (
            [{"no": 0, "content": "x"}, {"no": 0, "content": "y"}],       # duplicate
            [{"no": 0, "content": "x"}, {"no": 2, "content": "y"}],       # gap
            [{"no": "0", "content": "x"}, {"no": 1, "content": "y"}],     # not an int
            [{"no": True, "content": "x"}, {"no": 1, "content": "y"}],    # bool is an int
            [{"no": 0, "content": "x"}],                                  # too few rows
            [{"no": i, "content": "x"} for i in range(3)],                # too many rows
    ):
        result = {"book": book, "chapters": [dict(r) for r in records]}
        assert na._resync_chapter_records(result) == 0, records
        assert result["chapters"] == records, "a refused re-sync must change nothing"


def test_the_re_sync_never_writes_an_empty_body():
    """A heading with no prose under it is not a re-derivation; writing `""` into the row
    would delete a chapter from the durable copy while the book still shows one."""
    na = _live("narration_api")
    result = {"book": "## Bab 1: A\n\nsatu.\n\n## Bab 2: B\n\n",
              "chapters": [{"no": 0, "content": "lama"}, {"no": 1, "content": "lama"}]}
    assert na._resync_chapter_records(result) == 0
    assert [c["content"] for c in result["chapters"]] == ["lama", "lama"]


# ---------------------------------------------------------------------------
# 10. THE THINGS THAT MUST NOT HAVE HAPPENED
# ---------------------------------------------------------------------------
def test_no_untargeted_unit_or_chapter_was_touched(job):
    """🔴 UNIT-LEVEL, NOT ONLY CHAPTER-LEVEL. The addressed patch names three unit ids in
    chapter 3; every OTHER unit of that chapter has to come back byte-identical, or "targeted
    repair" means nothing more than "the chapter was rewritten and we hoped"."""
    seen = job()
    delivered = _delivered(seen)
    bodies = _bodies(delivered)

    assert len(bodies) == 3, "the job delivered a different number of chapters"
    assert bodies[0].strip() == BODY_1, "chapter 1 was never routed and must be byte-identical"

    before_units = [p for p in BODY_3.split("\n\n") if p.strip()]
    after_units = [p for p in bodies[2].strip().split("\n\n") if p.strip()]
    assert len(after_units) == len(before_units), (before_units, after_units)
    routed = {0, 1, len(before_units) - 1}          # opening, deposition, final question
    for index, (was, now) in enumerate(zip(before_units, after_units)):
        if index in routed:
            continue
        assert now == was, f"unit {index} of chapter 3 was never routed and changed"


def test_the_authority_owned_ledger_term_never_became_a_repair_target(job):
    """🔴 IT SURVIVES, AND NOTHING EVER AIMED AT IT. `scrub_bound_markers` leaves a bracket
    token that is not a bound id of THIS canon byte for byte — so the term is still there —
    but "was not deleted" is the weaker half. The stronger half is that no gate ever routed a
    directive at it: it appears in the lanes' prose, where it belongs, and in none of their
    DIRECTIVES, which is where a repair target would be."""
    seen = job()
    payload = _payload(seen)
    delivered = _delivered(seen)
    lanes = seen["lanes"]

    assert LEDGER in delivered, "the authority-owned ledger term was repaired away"
    blob = json.dumps({"f6": payload.get("f6"), "f8": payload.get("f8")})
    assert "buku-besar" not in blob, "the ledger term reached the accounting"

    # Both lanes render one directive per line as `- [severity/type] evidence ... FIX: ...`,
    # so the directives are collectable without knowing either prompt's section layout.
    directives = [line
                  for prompts in (lanes.prompts["structural"], lanes.prompts["legacy"])
                  for prompt in prompts
                  for line in prompt.split("\n")
                  if line.startswith("- [")]
    assert directives, "no lane was given any directive at all"
    for directive in directives:
        assert "buku-besar" not in directive, directive[:200]


def test_the_accounting_is_bounded_and_carries_no_prose(job):
    seen = job()
    blob = json.dumps(_payload(seen).get("f8") or {})
    for fragment in (GOOD_NAME, BAD_NAME, "Bab ", "##", "buku-besar", "Karena"):
        assert fragment not in blob, fragment
    assert (_payload(seen).get("f8") or {}).get("reason") in f8.ACCOUNTING_REASONS
