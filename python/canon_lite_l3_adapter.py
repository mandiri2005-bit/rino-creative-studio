"""L3-ASSIST Stage 2 — the concrete provider and extractor, as ONE job-scoped session.

The repair engine takes two callables and knows nothing about where they get their
answers. This module is where they come from, and it exists mostly to hold three
lines that are easy to get wrong.

ONE SESSION, NOT TWO CLIENTS
----------------------------
🔴 THERE IS EXACTLY ONE METERED EXTRACTION WAVE PER JOB, AND THIS MODULE MUST NOT
   BE THE SECOND. The `max_inflight` ceiling the meter enforces is
   `replicas x jobs-per-worker x extractor_concurrency`, and that arithmetic only
   describes reality while a job keeps one wave open. `maybe_run_metered_wave`
   claims a one-shot `ExtractionWaveToken` to make a second wave raise — so the
   re-extraction here deliberately does NOT call it, and does not call
   `extract_all` either. It reuses the SAME `MeteredProvider` the wave already
   built, one request at a time, after the wave has fully drained. Same sink, same
   `AttemptContext`, same credential, same accounting.

   Building a fresh adapter here would be the tempting shortcut and would be
   invisible in every test that does not count sessions: the calls would still be
   metered, still succeed, and still silently double the in-flight population the
   ceiling was derived from.

TWO DIFFERENT PROVIDERS, ON PURPOSE
-----------------------------------
Re-extraction is a QC-contract call and rides the QC session above. The repair
itself is prose generation, so it rides the narration job's OWN worker path and
its own usage sink — the same meter as every chapter in the book. Sending a repair
through the QC adapter would mean a second prompt contract, a second pricing entry
and a second ratified template, all to say something the narration meter already
says.

THE BUDGET IS INDEPENDENT OF THE ENGINE'S
-----------------------------------------
The engine already caps attempts at two per chapter. This session caps TOTAL calls
as well, because those are different failures: the engine's bound protects a
chapter from being rewritten forever, and this one protects the job from a target
list that turned out to be longer than anyone expected. A single ceiling would
have to be either too loose for one or too tight for the other.

IDENTITY
--------
A single-chapter re-extraction is inherently index 0 — there is one block in the
request. The session therefore STAMPS the real `chapter_index` and `chapter_id`
back onto the artefact from its own arguments, which is legitimate because the
session is the caller and knows which chapter it asked about. Those two fields are
consequently not evidence on this path, and the engine's `_binding_ok` is told
nothing to the contrary: `content_sha256` and `canon_sha256` remain substantive,
computed by the extractor from the candidate bytes it was actually given, and they
are what would catch an answer about the wrong text or the wrong canon.
"""

from __future__ import annotations

import logging
from dataclasses import replace as _dc_replace
from typing import Any, Optional

import canon_lite as _cl
import canon_lite_l2 as _l2

log = logging.getLogger("narasi")

__all__ = [
    "L3_REPAIR_PHASE",
    "MAX_SESSION_CALLS",
    "SessionBudgetExhausted",
    "L3AssistSession",
]

#: Telemetry phase for the repair generation. Distinct from `canon_lite_l2_extract`
#: so a repair call is never counted as an extraction in any downstream rollup.
L3_REPAIR_PHASE = "canon_lite_l3_repair"

#: Absolute ceiling on provider calls this session may make, of either kind. A
#: backstop far above any real job — the per-chapter bound does the real work.
MAX_SESSION_CALLS = 64


class SessionBudgetExhausted(RuntimeError):
    """Raised by the extractor when the session's call budget is spent."""


def _repair_instruction(canon_text: str, codes: tuple[str, ...], language: str) -> str:
    """The repair turn. Bounded, and explicit about what must NOT change."""
    findings = ", ".join(codes) or "continuity"
    return (
        f"{canon_text}\n\n"
        f"Bab di bawah ini melanggar kanon di atas: {findings}.\n"
        "Perbaiki HANYA pelanggaran itu.\n"
        "- Pertahankan baris judul persis seperti aslinya.\n"
        "- Jangan menambah atau menghapus baris kosong di awal atau akhir.\n"
        "- Ubah sesedikit mungkin: nama, angka, atau kalimat yang bertentangan saja.\n"
        "- Jangan menulis ulang bab, jangan menambah adegan, jangan berkomentar.\n"
        f"- Bahasa keluaran: {language}.\n"
        "Kembalikan HANYA teks bab lengkap yang sudah diperbaiki.\n"
    )


class L3AssistSession:
    """Built ONCE per job. Hands the engine its two callables."""

    __slots__ = ("_metered", "_qc", "_canon_text", "_language", "_worker_model",
                 "_telemetry_sink", "_timeout", "_calls", "_max_calls")

    def __init__(
        self,
        *,
        metered_provider: Any,
        canon_text: str,
        language: str = "id",
        worker_model: str,
        telemetry_sink: Any = None,
        timeout: float = 120.0,
        max_calls: int = MAX_SESSION_CALLS,
    ) -> None:
        if metered_provider is None or not callable(metered_provider):
            # Refused rather than defaulted to a fresh one: a session that mints its
            # own provider is the second wave this module exists to prevent.
            raise ValueError("metered_provider is required and must be the job's own")
        if isinstance(max_calls, bool) or not isinstance(max_calls, int) \
                or not (1 <= max_calls <= MAX_SESSION_CALLS):
            raise ValueError(f"max_calls: expected int in 1..{MAX_SESSION_CALLS}")
        self._metered = metered_provider
        self._qc = None
        self._canon_text = canon_text
        self._language = language
        self._worker_model = worker_model
        self._telemetry_sink = telemetry_sink
        self._timeout = float(timeout)
        self._calls = 0
        self._max_calls = int(max_calls)

    # -- budget ------------------------------------------------------------
    @property
    def calls_made(self) -> int:
        return self._calls

    @property
    def calls_remaining(self) -> int:
        return max(0, self._max_calls - self._calls)

    def _spend(self, kind: str) -> None:
        if self._calls >= self._max_calls:
            raise SessionBudgetExhausted(
                f"l3 session budget exhausted at {self._max_calls} calls ({kind})")
        self._calls += 1

    # -- the two callables the engine wants --------------------------------
    async def repair_provider(
        self, *, chapter_index: int, chapter_id: str, block_bytes: bytes,
        violation_codes: tuple, canon: Any, attempt: int,
    ) -> Optional[bytes]:
        """Generate a repaired chapter. `None` declines; never raises for a fault.

        Declining on an exhausted budget rather than raising: the engine treats a
        raise as a provider FAULT and stops the chapter, while a decline is an
        ordinary rejected candidate. Running out of budget is neither a fault nor a
        reason to hide that the chapter went unrepaired.
        """
        try:
            self._spend("repair")
        except SessionBudgetExhausted:
            log.warning("canon lite l3: session budget exhausted; repair declined")
            return None
        from orchestrator.static import Worker, run_worker

        worker = Worker(
            name=f"l3repair-ch{chapter_index + 1}", role="worker",
            phase=L3_REPAIR_PHASE, model=self._worker_model,
            system=self._canon_text, telemetry_sink=self._telemetry_sink,
        )
        try:
            res = await run_worker(
                worker,
                _repair_instruction(self._canon_text, tuple(violation_codes),
                                    self._language)
                + "\n\n" + block_bytes.decode("utf-8", errors="strict"),
                timeout=self._timeout,
                task_id=f"l3repair-ch{chapter_index + 1}-a{attempt}")
        except Exception:  # noqa: BLE001 - a decline, not a fault
            log.warning("canon lite l3: repair generation failed "
                        "(error_code=l3_repair_generation_error)")
            return None
        text = (res or {}).get("output")
        if not isinstance(text, str) or not text.strip():
            return None
        return text.encode("utf-8")

    async def extract_chapter(
        self, *, chapter_index: int, chapter_id: str, block_bytes: bytes, canon: Any,
    ) -> _l2.ChapterClaimsV1:
        """Re-extract ONE candidate chapter through the job's existing QC session."""
        self._spend("extract")
        import canon_lite_extractor as _ext
        import canon_lite_qc_provider as _qc

        text = block_bytes.decode("utf-8", errors="strict")
        # 🔴 MATERIALIZED WITHOUT THE CANON, DELIBERATELY. A one-block snapshot fed
        #    a canon would have its heading matched against the WHOLE outline and
        #    could be resolved to chapter 1 whatever chapter it really is. Left
        #    unresolved, the block's id is UNKNOWN, the request's id is UNKNOWN,
        #    and the extractor's own identity preflight compares like with like.
        mini = _l2.materialize_final_snapshot({"book": text})
        if mini is None or mini.chapter_count != 1:
            raise ValueError("l3 extract: candidate does not form one chapter block")
        block = mini.blocks[0]
        if block.content_sha256 != _cl.sha256_hex(block_bytes):
            # The materializer must have handed back the exact bytes; if a leading
            # prefix was split off, the hash the artefact carries would describe
            # something other than the candidate.
            raise ValueError("l3 extract: candidate bytes did not survive framing")

        request = _ext.ExtractionRequestV1(
            chapter_index=0, chapter_id=block.chapter_id,
            content_sha256=block.content_sha256, canon_sha256=canon.canon_sha256,
            attempt=1, chapter_bytes=mini.block_bytes(0), canon=canon)
        try:
            _ext._preflight_request(request, snapshot=mini, index=0, canon=canon)
            raw = await self._metered(request)
            payload = _ext._provider_payload(
                raw, snapshot=mini, index=0,
                model_version=_qc.QC_MODEL_UPSTREAM, prompt_sha256=_qc.PROMPT_SHA256,
                canon_sha256=canon.canon_sha256)
            artifact = _l2.parse_chapter_claims(payload, snapshot=mini, canon=canon)
        except Exception as exc:  # noqa: BLE001 - bounded classification, then re-raise
            # 🔴 THIS SITE WAS THE UNNAMED HALF OF `provider_failed`. Live canary
            #    `19444d6`'s L3 record showed `unresolved_reasons: {'provider_failed': 3}`
            #    on ALL THREE chapters after QC extraction itself had already succeeded
            #    on attempt 1 (no timeout, no quote_ambiguous) — meaning the failure was
            #    NOT in the repair generation call (`repair_provider` above logs its own
            #    `l3_repair_generation_error` and did not fire) but in THIS re-verification
            #    pass, and canon_lite_l3_repair.py's bare `except Exception` had nothing
            #    more specific to report than the one word "provider_failed" either way.
            #    Reusing `canon_lite_extractor`'s own classifiers means a repair
            #    re-extraction failure is described in the EXACT SAME bounded vocabulary
            #    as a first-pass extraction failure — `qc_provider_timeout`,
            #    `quote_ambiguous`, `schema_invalid`, etc. — instead of inventing a
            #    second, narrower one for this call site alone.
            _code = _ext._provider_error_code(exc) or _ext._extraction_reason_code(exc)
            log.warning("canon lite l3: repair re-extraction failed "
                        "(error_code=l3_repair_reextraction_error reason=%s)", _code)
            raise

        # 🔴 STAMPED, AND SAID SO. A one-block request is index 0 by construction, so
        #    these two fields carry no information about which chapter was really
        #    asked about — the session supplies them because the session is what
        #    knows. `content_sha256` and `canon_sha256` are NOT stamped: they came
        #    out of the extractor bound to the candidate's own bytes and canon, and
        #    they are what the engine's binding check actually rests on.
        return _dc_replace(artifact, chapter_index=chapter_index,
                           chapter_id=chapter_id)
