#!/usr/bin/env python3
"""F4b — targeted NON-DELIVERY probe for structural repair that actually LANDS.

BRIEF-FOR-CODEX-2026-08-14-POST-CANARY-V9.md §F4b (`fecd3dcb…20b5`).

🔴 WHY THE F4a PROBE COULD NOT BE REUSED, AND WHY THIS IS A SEPARATE FILE.
   The F4a probe answers ONE question — "how many billable requests did one attempt
   make?" — and its whole capture path is built for exactly one response: a single
   `captured` slot for the raw text and the usage. Under a corrective retry the second
   response OVERWRITES the first, so the artifact prices one response and reports it as
   the run's cost. Run offline with two identical responses it reports 165 tokens where
   330 were spent. It also has nowhere to put the only facts F4b is about: the retry
   counters, whether a chapter was ACCEPTED, and whether the manuscript actually
   changed. Widening it would silently redefine what an existing `narasi_f4a_*`
   artifact means, so F4b gets its own runner, its own schema and its own filename.
   🔴 The three F4a artifacts are NOT rewritten and are NOT evidence for F4b.

🔴 NON-DELIVERY, AND THE WORD MEANS ALL OF THIS:
   · one SYNTHETIC chapter — never a customer manuscript;
   · `_narasi_structural_patch_revise` is called directly: no job seam, no DB, no
     publication, no delivery, nothing persisted for a reader;
   · `credit_row=False`;
   · a network call is REFUSED unless `--allow-network` is passed explicitly, and the
     upstream cap is TWO — one first call plus at most one corrective retry;
   · the repaired manuscript is examined in-process and then discarded. Only hashes,
     booleans, closed enums, lengths and token counts are written. Neither the prompt,
     nor any response, nor the repaired text ever leaves this process.

🔴 AND IT CANNOT LAUNDER ITSELF INTO EVIDENCE IT IS NOT. `recording_mode` is earned by
   what happened, not granted by a flag, and the `dod` block reports each landing
   requirement SEPARATELY so a partial result cannot be read as a pass.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import hashlib
import json
import os
import pathlib
import sys
from types import SimpleNamespace

_HERE = pathlib.Path(__file__).resolve()
_REPO = _HERE.parents[2]
sys.path.insert(0, str(_REPO / "python"))
sys.path.insert(0, str(_HERE.parent))

# The bounded observation vocabulary is SHARED with the F4a probe on purpose: the same
# closed enums must mean the same thing in both artifacts, and a second copy would be
# free to drift.
from narasi_f4a_probe import (                                        # noqa: E402
    OPERATION_SOURCES,
    REFUSED_NETWORK_MODELS,
    SUBTYPES,
    VALIDATOR_OUTCOMES,
    _classify,
    _head,
    _sha256_file,
)

# 🔴 F4b NEEDS ITS OWN TARGET, AND THIS IS NOT A STYLE PREFERENCE. F4a's synthetic
#    chapter ALREADY satisfies F4a's own finding — its u002 surrenders before the u003
#    ruling — because F4a only ever needed a chapter that would provoke one call, not
#    one that needed repairing. Reusing it here made the probe prove that a COSMETIC
#    patch lands ("haknya" → "seluruh haknya") while calling it a structural repair.
#    Below, u002 actively CONTRADICTS the outline beat: Mira refuses, so the surrender
#    the packet requires before the ruling never happens on the page. The finding and
#    the fix name u002 as the unit to replace, which is what gives
#    `EXPECTED_CHANGED_POSITIONS` a basis in the PROMPT rather than in whatever the
#    scripted offline response happened to touch.
F4B_PROBE_CHAPTER = (
    "## Bab 3: Atap\n\n"
    "Mira membawa map penyerahan hak itu ke meja rapat.\n\n"
    "Ia menolak menyerahkan haknya dan menyimpan map itu kembali.\n\n"
    "Hakim lalu menyatakan rekaman tak dapat diterima.\n\n"
    "Deposisinya menutup sidang.\n"
)
F4B_PROBE_FINDING = {
    "severity": "high", "type": "outline_beat_order", "chapter": 3,
    "evidence": (
        "Unit u002 has Mira REFUSE to hand over her rights and put the file away. The "
        "outline requires the surrender itself to happen on the page, before the "
        "ruling in u003 — as written the beat is never executed."),
    "fix": (
        "Replace u002 so the surrender happens there, before the ruling in u003. Do "
        "not touch any other unit."),
}
F4B_PROBE_PACKET = {"3": (
    "EXACT OUTLINE EXECUTION PACKET (synthetic). Bab 3 beat order:\n"
    "(1) [u001] Mira brings the surrender file to the table.\n"
    "(2) [u002] Mira SURRENDERS her rights, on the page, before anything is played.\n"
    "(3) [u003] ONLY THEN does the judge rule the recording inadmissible.\n"
    "(4) [u004] The deposition closes the session.\n"
    "The manuscript currently has Mira REFUSE at (2), so beat (2) is unexecuted and "
    "the ruling at (3) lands with nothing having been surrendered.")}

# v2 (2026-08-15): the `fault_injection` block was added, so the KEY SET changed. Bumped
# rather than slipped in: two artifacts both claiming v1 while disagreeing on their own
# keys is exactly the ambiguity a schema version exists to prevent. The live v1 artifact
# from 2026-08-15T17:16:24Z stays v1 and must NOT be rewritten.
ARTIFACT_SCHEMA_VERSION = "narasi_f4b_probe_artifact_v2"
PROBE_KINDS = ("targeted_non_delivery_repair_landing", "deterministic_fault_injection")
#: What the injection replaces the first candidate with — a member of the observed
#: unreadable-`op` class, i.e. exactly the defect F4a proved happens naturally live.
_INJECTED_UNREADABLE = json.dumps({
    "schema_version": "narasi_addressed_patch_v1",
    "operations": [{"op": None, "unit_id": "u002", "text": "Kandidat."}],
})
RECORDING_MODES = ("live", "failed_probe", "offline_dry_run")
#: The provider helpers this probe wraps to observe transports independently of the
#: production ledger. Module-level so a test can switch the observer off and prove the
#: unobserved case is refused rather than back-filled.
_TRANSPORT_NAMES = ("_anthropic_messages_create", "_vertex_gemini_create",
                    "_fal_llm_create")
#: One first call plus at most ONE corrective retry. Not a promise of two — a cap.
DEFAULT_UPSTREAM_CAP = 2
#: Per-response completeness of the provider's own usage metadata.
USAGE_STATUSES = ("complete", "partial", "absent")
#: Where the token counts came from, run-wide. `provider_reported` requires EVERY
#: response to have carried COMPLETE usage; anything less is named, never rounded up.
USAGE_SOURCES = ("provider_reported", "incomplete", "absent")


def _usage_fields(response):
    """Provider-REPORTED usage, plus how complete it actually was.

    🔴 A ZERO THAT MEANS "THE PROVIDER TOLD US NOTHING" AND A ZERO THAT MEANS "NO
    TOKENS" ARE DIFFERENT FACTS ABOUT COST. Reading the attributes with a silent `or 0`
    collapses them, and the artifact then publishes a complete-looking cost of nothing.

    🔴 AND "SOME USAGE" IS NOT "USAGE". The first fix used `or` — either field present
    was enough to call the record complete — so a provider returning only
    `prompt_tokens` produced `usage_source=provider_reported` and a met DoD over a cost
    that was missing every completion token. Completeness needs BOTH fields, and the
    half-present case needs a NAME of its own rather than promotion to the full one.

    This probe never substitutes an estimate for missing usage; if one is ever added it
    must be labelled as an estimate rather than reported as provider usage."""
    usage = getattr(response, "usage", None)
    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)

    def _ok(value):
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    has_prompt, has_completion = _ok(prompt), _ok(completion)
    if usage is None or not (has_prompt or has_completion):
        status = "absent"
    elif has_prompt and has_completion:
        status = "complete"
    else:
        status = "partial"
    return (int(prompt) if has_prompt else 0,
            int(completion) if has_completion else 0,
            status)

#: What an offline dry run scripts. Response 1 is a member of the observed
#: unreadable-`op` class (see the F4b suite on why no single member may be called "the"
#: live shape); response 2 obeys the contract, so the LANDING path is exercised.
_OFFLINE_FIRST = json.dumps({
    "schema_version": "narasi_addressed_patch_v1",
    "operations": [{"op": None, "unit_id": "u002", "text": "Kandidat."}],
})
_OFFLINE_SECOND = json.dumps({
    "schema_version": "narasi_addressed_patch_v1",
    "operations": [{"op": "replace", "unit_id": "u002",
                    "text": "Ia menyerahkan haknya di hadapan majelis sebelum "
                            "rekaman diputar."}],
})


#: Which unit position the probe's own scripted repair is allowed to rewrite. The
#: scripted second response replaces `u002`, i.e. index 1. Declared UP FRONT and
#: compared against, never derived from what the candidate happened to do.
EXPECTED_CHANGED_POSITIONS = (1,)


def _structural_identity(before: str, after: str, *,
                         expected_changed_positions=EXPECTED_CHANGED_POSITIONS) -> dict:
    """Compare the original and repaired chapter WITHOUT retaining either.

    Returns booleans, counts, positions and hashes only. This is the F4b claim the F4a
    artifact had no field for: a repair that lands must change the manuscript and must
    leave everything it did not address byte-identical.

    🔴 `untouched_units_identical` IS SCOPED TO THE *EXPECTED* EDIT, AND THAT IS THE
    WHOLE POINT. The first version collected the positions that DIFFERED, called them
    `changed`, and then compared the positions outside that set — which are equal by
    construction. It read `True` for every possible input: a candidate that quietly
    rewrote a second unit still passed, and a mutant replacing the whole computation
    with the literal `True` was indistinguishable from the real thing. "Untouched" must
    mean "not in the edit we asked for", never "not in whatever the model touched",
    otherwise the candidate gets to define the property it is being judged against."""
    import narasi_addressed_patch as ap

    expected = frozenset(int(index) for index in expected_changed_positions)
    out = {
        "before_sha256": hashlib.sha256(before.encode("utf-8", "replace")).hexdigest(),
        "after_sha256": hashlib.sha256(after.encode("utf-8", "replace")).hexdigest(),
        "manuscript_changed": before != after,
        "heading_identical": False,
        "trailing_frame_identical": False,
        "unit_count_before": 0,
        "unit_count_after": 0,
        "unit_counts_match": False,
        "units_changed": -1,
        "changed_unit_positions": [],
        "expected_changed_positions": sorted(expected),
        "only_expected_units_changed": False,
        "untouched_units_identical": False,
        "all_original_bodies_present": False,
    }
    try:
        out["heading_identical"] = (
            before.splitlines()[:1] == after.splitlines()[:1])
        out["trailing_frame_identical"] = (
            before[len(before.rstrip()):] == after[len(after.rstrip()):])
        units_before = [u.text for u in ap.segment_chapter(before).units]
        units_after = [u.text for u in ap.segment_chapter(after).units]
        out["unit_count_before"] = len(units_before)
        out["unit_count_after"] = len(units_after)
        out["unit_counts_match"] = len(units_before) == len(units_after)
        # A topology change (insert/move) makes a positional comparison meaningless,
        # so it is reported as a count mismatch rather than silently compared anyway.
        if out["unit_counts_match"]:
            changed = [index for index, (b, a)
                       in enumerate(zip(units_before, units_after)) if b != a]
            out["units_changed"] = len(changed)
            out["changed_unit_positions"] = changed[:16]     # bounded, positions only
            out["only_expected_units_changed"] = set(changed) == expected
            out["untouched_units_identical"] = all(
                units_before[index] == units_after[index]
                for index in range(len(units_before)) if index not in expected)
        else:
            out["units_changed"] = abs(len(units_after) - len(units_before))
        replaced = {units_before[i] for i in expected if i < len(units_before)}
        out["all_original_bodies_present"] = all(
            body in after for body in units_before if body not in replaced)
    except Exception:
        pass
    return out


def run_probe(*, allow_network: bool, out_dir: pathlib.Path, now_utc: str,
              offline_responses=None,
              model_alias: str = "test-model",
              upstream_cap: int = DEFAULT_UPSTREAM_CAP,
              fault_injection: bool = False) -> pathlib.Path:
    """Run the probe. With `fault_injection`, the FIRST candidate is deterministically
    replaced with the unreadable-`op` class AFTER its response has been received and
    metered, so the production engine must detect it and spend its one corrective retry.

    🔴 THE INJECTION LIVES ENTIRELY IN THIS RUNNER. Production repair code is not
    modified, not flagged, and does not know a probe is running: the substitution
    happens in the probe's own replacement of `_resp_content`, which production calls
    only AFTER metering. That ordering is production's, not the probe's, which is why
    "injected after the response was received and metered" is an observable fact here
    rather than a promise — the event sequence is recorded and compared.

    Why inject at all: the natural defect is not reproducible on demand. F4a proved the
    `unknown_operation`/`unreadable` class happens live; the 17:16:24Z probe proved
    repair can land live; neither could make the RETRY fire, because the model answered
    correctly first time. Re-rolling the dice until it misbehaves is stochastic and was
    refused. Injection makes the retry path deterministic while keeping BOTH provider
    calls real."""
    import laozhang_api as lz

    if allow_network and model_alias in REFUSED_NETWORK_MODELS:
        raise ValueError(
            f"--allow-network requires an explicit production model alias; "
            f"{model_alias!r} is refused")
    # 🔴 THE BOUND BELONGS TO THE FUNCTION THAT INSTALLS THE LEDGER, NOT TO THE CLI.
    # `main()` checking it only protects the command line: any caller reaching
    # `run_probe()` directly — a test, a script, a future runner — could set the cap to
    # five and get a met DoD over five billable calls. The authorisation F4b was given
    # is one call plus at most one corrective retry, and it must be enforced where the
    # permission is actually granted. The CLI check stays as an outer layer that can
    # report a friendlier error before any work starts.
    upstream_cap = int(upstream_cap)
    if not 1 <= upstream_cap <= DEFAULT_UPSTREAM_CAP:
        raise ValueError(
            f"F4b is bounded at {DEFAULT_UPSTREAM_CAP} upstream requests (one call "
            f"plus at most one corrective retry); {upstream_cap} was requested")
    if fault_injection and upstream_cap != DEFAULT_UPSTREAM_CAP:
        # The whole point is to force the retry. A cap of 1 would refuse it above the
        # transport and the run could only ever fail its own DoD — spending a billable
        # call to prove nothing.
        raise ValueError(
            f"fault injection needs exactly {DEFAULT_UPSTREAM_CAP} upstream requests "
            f"(the retry is the thing under test); {upstream_cap} was requested")

    scripted = list(offline_responses if offline_responses is not None
                    else (_OFFLINE_FIRST, _OFFLINE_SECOND))
    observed = {"create_entered": 0, "upstream_requests": 0}
    # 🔴 A LIST, NOT A SLOT. This is the defect that made the F4a probe unusable here:
    # one `captured` dict means response N overwrites response N-1, so a retried run
    # reports the cost of its LAST response as the cost of the run.
    per_response: list[dict] = []
    real_factory = lz.make_narasi_client

    _transport_names = _TRANSPORT_NAMES
    _real_transports = {}

    def _wrap_transport(name):
        real = getattr(lz, name)

        def _counted(*a, **kw):
            # OBSERVE ONLY — the cap is enforced by the production ledger's atomic
            # `reserve()`. A refused invocation is not a request.
            try:
                result = real(*a, **kw)
            except lz._NarasiUpstreamRefused:
                raise
            except Exception:
                observed["upstream_requests"] += 1
                raise
            observed["upstream_requests"] += 1
            return result
        _real_transports[name] = real
        setattr(lz, name, _counted)

    _real_openai = getattr(lz, "OpenAI", None)

    def _counted_openai(*a, **kw):
        client = _real_openai(*a, **kw)
        inner_create = client.chat.completions.create

        def _c(**kw_):
            observed["upstream_requests"] += 1
            return inner_create(**kw_)
        client.chat.completions.create = _c
        return client

    def _offline_client():
        """Stands in for `make_client(model) -> OpenAI`, INCLUDING `with_options`: the
        structural lane refuses to dispatch on a plain client whose SDK retries it
        cannot disable, so a stub without it would make every offline run report
        `provider_error` and zero cost — a green-looking artifact that proved nothing."""
        def _create(**_kw):
            content = scripted.pop(0) if scripted else "{}"
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content=content), finish_reason="stop")],
                usage=SimpleNamespace(prompt_tokens=120, completion_tokens=45))
        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=_create)))
        client.with_options = lambda **_kw: client
        return client

    def _counting_factory(*args, **kwargs):
        client = (real_factory(*args, **kwargs) if allow_network
                  else _offline_client())
        inner = client.chat.completions.create
        _is_failover = isinstance(client, lz._NarasiFailoverClient)

        def _counted(**kwargs_):
            observed["create_entered"] += 1
            try:
                response = inner(**kwargs_)
            except lz._NarasiUpstreamRefused:
                raise
            except Exception:
                if not _is_failover:
                    observed["upstream_requests"] += 1
                raise
            if not _is_failover:
                observed["upstream_requests"] += 1
            try:
                # 🔴 THE REAL ONE, DELIBERATELY. The probe's own record must describe
                # what the PROVIDER returned; routing it through the injection wrapper
                # would make the artifact report the injected payload as the model's
                # answer — falsifying the very evidence the injection is meant to
                # accompany honestly.
                raw = _real_resp_content(response) or ""
            except Exception:
                raw = ""
            _prompt, _completion, _usage_status = _usage_fields(response)
            # EVERY response gets its own record. The artifact's cost is the SUM, and
            # `usage_metadata_observed` says whether that sum is a measurement or an
            # absence dressed as one.
            per_response.append({
                "index": len(per_response) + 1,
                "prompt_tokens": _prompt,
                "completion_tokens": _completion,
                "usage_metadata": _usage_status,
                "served_by": str(getattr(response, "_narasi_served_by", "") or "")[:32],
                "served_model": str(
                    getattr(response, "_narasi_served_model", "") or "")[:64],
                "observation": _classify(raw, F4B_PROBE_CHAPTER),
            })
            return response

        client.chat.completions.create = _counted
        return client

    # 🔴 THE INJECTION SEAM. `_meter` and `_read_candidate` below replace the two
    # production seams the probe already owns. The ORDER they fire in is decided by
    # production's `_exchange` (meter the response, THEN read its candidate), so the
    # recorded sequence is evidence rather than assertion.
    injection = {
        "applied": False,
        "target_response_index": None,
        "sequence": [],          # bounded event trace, at most a handful of entries
        "metered_responses": 0,
    }

    async def _meter(_tenant=None, _user=None, _model=None, response=None, **_kwargs):
        """Stands in for `_log_narasi_usage`: records that THIS response was metered and
        writes no credit row. It reads usage off the response the provider actually
        returned — before any injection touches the candidate."""
        injection["metered_responses"] += 1
        injection["sequence"].append(f"metered:{injection['metered_responses']}")
        return 0

    def _read_candidate(response):
        """Stands in for `_resp_content`, which production calls AFTER metering.

        When injection is armed, the FIRST candidate is replaced here with the
        unreadable-`op` class. The provider's real response object is never mutated —
        only what production reads from it — so nothing about the recorded response,
        its usage or its observation is falsified."""
        # 🔴 ONE DOOR. There is deliberately NO "…and it has already been metered"
        # condition here. Production's `_exchange` meters the response and only then
        # reads its candidate, so the ordering is already guaranteed by the code under
        # test — and a second guard restating it made the two mechanisms hide each
        # other: a mutation run showed that removing either one left every test green,
        # because whichever survived covered the other. The ordering is now VERIFIED
        # instead, from the recorded trace, and the DoD refuses a run whose trace does
        # not show `metered:1` before `injected:1`.
        if (fault_injection and not injection["applied"]
                and len(per_response) == 1):
            injection["applied"] = True
            injection["target_response_index"] = 1
            injection["sequence"].append("injected:1")
            return _INJECTED_UNREADABLE
        return _real_resp_content(response)

    lz.make_narasi_client = _counting_factory
    real_usage = lz._log_narasi_usage
    _real_resp_content = lz._resp_content
    lz._log_narasi_usage = _meter
    lz._resp_content = _read_candidate
    cap_token = lz._NARASI_UPSTREAM_CAP.set(int(upstream_cap))
    for _name in _transport_names:
        _wrap_transport(_name)
    if _real_openai is not None:
        lz.OpenAI = _counted_openai
    try:
        revised, _credits, stats = asyncio.run(lz._narasi_structural_patch_revise(
            F4B_PROBE_CHAPTER, [dict(F4B_PROBE_FINDING)], "storytelling", "id", model_alias,
            tenant_id="probe", user_id="probe", job_uuid=None,
            credit_row=False,                       # never bill a customer ledger row
            authority_text="AUTHORITY", outline_packets=dict(F4B_PROBE_PACKET)))
    finally:
        lz.make_narasi_client = real_factory
        lz._log_narasi_usage = real_usage
        # The injection must not outlive the run. A leaked `_resp_content` wrapper would
        # silently corrupt the next caller in this process.
        lz._resp_content = _real_resp_content
        lz._NARASI_UPSTREAM_CAP.reset(cap_token)
        for _name, _real in _real_transports.items():
            setattr(lz, _name, _real)
        if _real_openai is not None:
            lz.OpenAI = _real_openai

    reported = int(stats["provider_calls"])
    entered = int(observed["create_entered"])
    upstream = int(observed["upstream_requests"])
    responses = len(per_response)

    if not allow_network:
        recording_mode = "offline_dry_run"
    elif upstream >= 1 and responses >= 1 and reported == upstream:
        recording_mode = "live"
    else:
        recording_mode = "failed_probe"

    tok_in = sum(item["prompt_tokens"] for item in per_response)
    tok_out = sum(item["completion_tokens"] for item in per_response)
    statuses = [item["usage_metadata"] for item in per_response]
    usage_complete = sum(1 for status in statuses if status == "complete")
    if per_response and usage_complete == len(per_response):
        usage_source = "provider_reported"
    elif any(status in ("complete", "partial") for status in statuses):
        # SOME usage arrived but not all of it. Named, never promoted: a half-reported
        # cost that calls itself provider-reported is the same lie as a zero one.
        usage_source = "incomplete"
    else:
        usage_source = "absent"
    # Priced on the model that actually SERVED — the phase switchboard is free to
    # override the caller's alias, and pricing by the alias mis-bills exactly when it
    # does. Each response is priced on its own served model, then summed, because a
    # failover retry can be served by a different rung than the first call.
    cost_usd = 0.0
    for item in per_response:
        try:
            cost_usd += float(lz._calc_cost(
                item["served_model"] or model_alias,
                item["prompt_tokens"], item["completion_tokens"]))
        except Exception:
            pass

    identity = _structural_identity(F4B_PROBE_CHAPTER, revised)
    # Derived from the RECORDED sequence, never asserted: the injection is only "after
    # metering" if `metered:1` genuinely precedes `injected:1` in the event trace.
    _seq = injection["sequence"]
    applied_after_metering = (
        "metered:1" in _seq and "injected:1" in _seq
        and _seq.index("metered:1") < _seq.index("injected:1"))
    retry = {
        "schema_retry_chapters": int(stats["schema_retry_chapters"]),
        "schema_retry_accepted": int(stats["schema_retry_accepted"]),
        "schema_retry_exhausted": int(stats["schema_retry_exhausted"]),
    }

    # 🔴 EACH LANDING REQUIREMENT REPORTED SEPARATELY. A single `met` boolean lets a
    # partial result read as a pass; a reader must be able to see WHICH half failed.
    checks = {
        "reported_equals_observed": reported == upstream,
        "two_physical_calls": reported == 2 and upstream == 2,
        "usage_recorded_for_every_response": (
            responses == upstream and responses > 0
            # 🔴 A COUNT OF RESPONSES IS NOT A COUNT OF USAGE. Without this clause the
            # probe could publish "cost coverage complete" over 0/0 tokens.
            and usage_source == "provider_reported"),
        "retry_entered_once": retry["schema_retry_chapters"] == 1,
        "retry_accepted_once": retry["schema_retry_accepted"] == 1,
        "retry_not_exhausted": retry["schema_retry_exhausted"] == 0,
        "one_chapter_accepted": int(stats["accepted"]) == 1,
        "manuscript_changed": bool(identity["manuscript_changed"]),
        "heading_identical": bool(identity["heading_identical"]),
        "trailing_frame_identical": bool(identity["trailing_frame_identical"]),
        # The three that together make "only the scripted edit landed" checkable.
        "unit_counts_match": bool(identity["unit_counts_match"]),
        "only_expected_units_changed": bool(identity["only_expected_units_changed"]),
        "untouched_units_identical": bool(identity["untouched_units_identical"]),
        "no_credit_row": True,
        "not_delivered": True,
        "within_upstream_cap": upstream <= int(upstream_cap),
    }
    # 🔴 BOTH MODES CARRY AN INJECTION CHECK, so neither can stay silent about it. A
    # normal run that somehow injected fails just as loudly as an injection run that
    # did not.
    if fault_injection:
        checks["fault_injection_applied"] = injection["applied"] is True
        checks["fault_injection_after_metering"] = applied_after_metering is True
        checks["fault_injection_target_is_first_response"] = (
            injection["target_response_index"] == 1)
    else:
        checks["no_fault_injection"] = injection["applied"] is False

    artifact = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        # Named for what the run ACTUALLY was. An injected run that called itself a
        # plain repair-landing probe would be the single most misleading artifact this
        # workstream could produce.
        "probe_kind": ("deterministic_fault_injection" if fault_injection
                       else "targeted_non_delivery_repair_landing"),
        "recording_mode": recording_mode,
        "primary_response_observed": recording_mode == "live",
        "executed_at_utc": now_utc,
        "source": {
            "head": _head(),
            "laozhang_api_sha256": _sha256_file(_REPO / "python/laozhang_api.py"),
            "narasi_addressed_patch_sha256": _sha256_file(
                _REPO / "python/narasi_addressed_patch.py"),
            # 🔴 THE CODE THAT PRODUCES THE EVIDENCE IS PART OF THE EVIDENCE. The DoD
            # verdict, the usage sum and the structural comparison are all computed
            # HERE, and the bounded classifier is imported from the F4a runner. Binding
            # only the code under test would let either runner change while an
            # artifact's source binding stayed byte-identical — the reader could not
            # tell that the measurement itself had moved.
            "narasi_f4b_probe_sha256": _sha256_file(_HERE),
            "narasi_f4a_probe_sha256": _sha256_file(
                _HERE.parent / "narasi_f4a_probe.py"),
        },
        "provider": {
            "phase": "canon_diff_revise",
            "model_alias": model_alias,
            "resolved_model": str(lz.MODELS.get(model_alias, model_alias)),
        },
        "delivery": {"persisted": False, "returned_to_user": False},
        "counts": {
            "chapters_targeted": int(stats["targeted"]),
            "chapters_attempted": int(stats["attempted"]),
            "chapters_accepted": int(stats["accepted"]),
            "provider_calls_reported": reported,
            "provider_calls_observed": upstream,
            "adapter_invocations_observed": entered,
            "responses_observed": responses,
            "upstream_request_cap": int(upstream_cap),
            **retry,
        },
        "cost": {
            "prompt_tokens": tok_in,
            "completion_tokens": tok_out,
            "total_tokens": tok_in + tok_out,
            "estimated_usd_micros": int(round(cost_usd * 1_000_000)),
            "estimator": "laozhang_api._calc_cost",
            # Where the TOKEN COUNTS came from — distinct from the price estimator.
            # `provider_reported` requires every response to have carried usage; the
            # probe never substitutes an estimate for missing usage, and if one is ever
            # added it must be named here rather than reported as provider usage.
            "usage_source": usage_source,
            "responses_with_complete_usage": usage_complete,
            "credit_row_written": False,
        },
        # 🔴 ALWAYS PRESENT, IN BOTH MODES. A block that only appears when injection
        # happened would let its ABSENCE be read as "this was a clean run" by a reader
        # looking at an artifact written by an older build.
        "fault_injection": {
            "requested": bool(fault_injection),
            "applied": bool(injection["applied"]),
            "class": "unknown_operation_unreadable" if injection["applied"] else None,
            "target_response_index": injection["target_response_index"],
            "applied_after_metering": bool(applied_after_metering),
            "injected_payload_sha256": (
                hashlib.sha256(_INJECTED_UNREADABLE.encode()).hexdigest()
                if injection["applied"] else None),
            # The raw event trace, so "after metering" is checkable, not just claimed.
            "event_sequence": list(injection["sequence"])[:16],
        },
        # Per response, so the sum above is auditable rather than asserted.
        # NOTE: `responses[*].observation` describes what the PROVIDER returned. Under
        # injection, response #1's observation is the model's real answer — the
        # injected payload is recorded above, by hash, and never as the model's.
        "responses": per_response,
        "manuscript": identity,
        "dod": {"met": all(checks.values()), "checks": checks},
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    # The filename carries the verdict: a failed probe cannot be mistaken for the live
    # artifact by a reader who only looks at a directory listing. The `f4b-` prefix
    # keeps it distinguishable from the three F4a artifacts in the same directory.
    suffix = "" if recording_mode == "live" else f"-{recording_mode.replace('_', '-')}"
    if recording_mode == "live" and not artifact["dod"]["met"]:
        suffix = "-dod-not-met"
    # A DIFFERENT PREFIX, so an injected run can never be mistaken for the naturally
    # occurring one in a directory listing — the two prove different things.
    stem = ("f4b-fault-injection-probe" if fault_injection
            else "f4b-repair-landing-probe")
    path = out_dir / f"{stem}-{now_utc.replace(':', '')}{suffix}.json"
    with open(path, "x", encoding="utf-8") as handle:
        json.dump(artifact, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-network", action="store_true",
                        help="REQUIRED to reach a real provider. Spends AT MOST "
                             "--max-upstream billable requests. Needs Rino's explicit "
                             "permission, granted for F4b specifically.")
    parser.add_argument("--max-upstream", type=int, default=DEFAULT_UPSTREAM_CAP,
                        help="Hard cap on billable upstream requests (default 2: one "
                             "first call plus at most one corrective retry).")
    parser.add_argument("--fault-injection", action="store_true",
                        help="DETERMINISTIC closure probe: after the first response is "
                             "received and metered, the probe replaces its candidate "
                             "with the unreadable-`op` class so the production engine "
                             "must spend its one corrective retry. BOTH calls stay "
                             "live. Requires --max-upstream 2.")
    parser.add_argument("--out-dir", default=str(_REPO / "docs/audit/narasi"))
    parser.add_argument("--model", default="test-model",
                        help="Model alias. REQUIRED and must be a production alias "
                             "when --allow-network is used.")
    args = parser.parse_args(argv)

    if args.allow_network and os.environ.get("NARASI_F4B_PROBE_CONFIRM") != "1":
        print("refusing: --allow-network also requires NARASI_F4B_PROBE_CONFIRM=1",
              file=sys.stderr)
        return 2
    if args.allow_network and args.model in REFUSED_NETWORK_MODELS:
        print(f"refusing: --allow-network needs an explicit production --model; "
              f"{args.model!r} is refused", file=sys.stderr)
        return 2
    # 🔴 ONE DOOR FOR THE CAP, AND IT IS `run_probe()`. This used to check the bound
    # here as well, and `max(1, …)` quietly clamped a 0 or a negative into 1 on the way
    # past. Once `main()` started surfacing the runner's own refusal, the duplicate
    # became unobservable — a mutation run confirmed it survived deletion, because the
    # runner caught every case the CLI check did. The bound belongs where the ledger is
    # installed; the raw value is handed straight down so the runner judges it.
    cap = int(args.max_upstream)

    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        path = run_probe(allow_network=args.allow_network,
                         out_dir=pathlib.Path(args.out_dir), now_utc=now,
                         model_alias=args.model, upstream_cap=cap,
                         fault_injection=args.fault_injection)
    except ValueError as refusal:
        # The runner's own bounds are refusals, not crashes. A traceback here reads as
        # a broken tool; the other gates all print one line and exit 2.
        print(f"refusing: {refusal}", file=sys.stderr)
        return 2
    print(f"artifact written: {path}")

    # 🔴 FAIL-CLOSED. The filename carries the verdict, but an operator or a CI step
    # reads the EXIT CODE — and a run that spent two billable calls without landing a
    # repair reported success. The exit code answers one question: did this run
    # demonstrate what the runner exists to demonstrate?
    doc = json.loads(path.read_text(encoding="utf-8"))
    met = bool(doc["dod"]["met"])
    mode = doc["recording_mode"]
    failed = sorted(key for key, value in doc["dod"]["checks"].items() if not value)
    print(f"recording_mode={mode} probe_kind={doc['probe_kind']} "
          f"fault_injection_applied={doc['fault_injection']['applied']} dod.met={met}"
          + (f" failed_checks={','.join(failed)}" if failed else ""))
    if args.allow_network and mode != "live":
        print(f"FAILED: a network run that is not live evidence (mode={mode})",
              file=sys.stderr)
        return 1
    if not met:
        print("FAILED: the definition of done was not met", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
