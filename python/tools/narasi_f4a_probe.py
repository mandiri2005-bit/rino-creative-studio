#!/usr/bin/env python3
"""F4a — targeted NON-DELIVERY probe for the structural patch call path.

BRIEF-FOR-CODEX-2026-08-14-POST-CANARY-V9.md §F4a (`fecd3dcb…20b5`).

What this is for: canary v9 rejected every structural patch as `unknown_operation`, and
the only record of WHY was a comment. This runner reproduces exactly one structural
patch attempt against the SAME production prompt and call path, observes what came back,
and writes a bounded, sanitized artifact that can be audited later.

🔴 NON-DELIVERY, AND THE WORD MEANS ALL OF THIS:
   · one SYNTHETIC chapter — never a customer manuscript;
   · `_narasi_structural_patch_revise` is called directly: no job seam, no DB, no
     publication, no delivery, nothing persisted for a reader;
   · `credit_row=False`;
   · a network call is REFUSED unless `--allow-network` is passed explicitly;
   · the raw prompt and the raw response are never printed and never written. Only a
     closed subtype, a length and a SHA-256 leave this process.

🔴 AND IT CANNOT LAUNDER ITSELF INTO EVIDENCE IT IS NOT. `recording_mode` is written
   into the artifact: an offline dry run says so, and only a run that actually reached a
   provider may say `live`. A record transcribed from an older comment must say
   `retrospective_transcription` with `primary_raw_evidence_available=false`, and such a
   record does NOT close the live-evidence requirement.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import hashlib
import json
import os
import pathlib
import subprocess
import sys
from types import SimpleNamespace

_HERE = pathlib.Path(__file__).resolve()
_REPO = _HERE.parents[2]
sys.path.insert(0, str(_REPO / "python"))

# v2 (2026-08-15): `primary_raw_evidence_available` renamed to
# `primary_response_observed`, and `served_by` / `served_model` /
# `adapter_invocations_observed` / `upstream_request_cap` added. Bumped because the
# SHAPE changed — two artifacts both claiming v1 while disagreeing on their own key set
# is exactly the ambiguity a schema version exists to prevent. The live artifact from
# 2026-08-15T11:29:26Z stays v1 and must not be rewritten.
ARTIFACT_SCHEMA_VERSION = "narasi_f4a_probe_artifact_v2"
RECORDING_MODES = ("live", "failed_probe", "offline_dry_run",
                   "retrospective_transcription")
#: Aliases that must never be used for a network run — a probe pointed at a test alias
#: proves nothing about production routing and would still be filed as evidence.
REFUSED_NETWORK_MODELS = frozenset({"test-model", "", "dummy", "fake"})
#: The provider helpers this probe wraps to observe transports independently of the
#: production ledger. Module-level so a test can switch the observer off and prove the
#: unobserved case is refused rather than back-filled.
_TRANSPORT_NAMES = ("_anthropic_messages_create", "_vertex_gemini_create",
                    "_fal_llm_create")
VALIDATOR_OUTCOMES = ("accepted", "unknown_operation", "other_rejection")
OPERATION_SOURCES = ("op_field", "single_wrapper_key", "unreadable")
SUBTYPES = ("insert", "delete", "rewrite", "update", "other")

#: Synthetic. Four addressable units, no customer text, no real names.
PROBE_CHAPTER = (
    "## Bab 3: Atap\n\n"
    "Mira membawa hak penyerahan itu ke meja rapat.\n\n"
    "Ia menyerahkan haknya sebelum rekaman diputar.\n\n"
    "Hakim lalu menyatakan rekaman tak dapat diterima.\n\n"
    "Deposisinya menutup sidang.\n"
)
PROBE_FINDING = {
    "severity": "high", "type": "outline_beat_order", "chapter": 3,
    "evidence": "Surrender must precede the ruling.",
    "fix": "Restore the outlined order.",
}
PROBE_PACKET = {"3": "EXACT OUTLINE EXECUTION PACKET (synthetic)"}

#: What an offline dry run pretends the provider said — the v9 shape, so the wiring is
#: exercised on the branch the probe exists to observe.
_OFFLINE_RESPONSE = json.dumps({
    "schema_version": "narasi_addressed_patch_v1",
    "operations": [{"op": "delete_paragraph", "unit_id": "u001"}],
})


def _sha256_file(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _head() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=_REPO,
                              capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return "unknown"


def _classify(raw: str, chapter: str) -> dict:
    """Derive the BOUNDED observation from the raw response, in-process.

    The raw string never leaves this function: what comes back is a closed enum, a
    length and a hash."""
    import narasi_addressed_patch as ap

    outcome, source, subtype = "other_rejection", "unreadable", "other"
    token = ""
    try:
        segmented = ap.segment_chapter(chapter)
    except Exception:
        segmented = None

    try:
        parsed = json.loads(raw)
        operations = parsed.get("operations")
        first = operations[0] if isinstance(operations, list) and operations else None
        if isinstance(first, dict):
            if isinstance(first.get("op"), str):
                source, token = "op_field", first["op"]
            elif len(first) == 1:
                only_key = next(iter(first))
                if isinstance(only_key, str) and isinstance(first[only_key], dict):
                    source, token = "single_wrapper_key", only_key
    except Exception:
        pass

    if segmented is not None:
        try:
            ap.apply_addressed_patch(segmented, raw)
            outcome = "accepted"
        except ap.PatchValidationError as err:
            outcome = ("unknown_operation" if err.code == "unknown_operation"
                       else "other_rejection")
            if isinstance(getattr(err, "received", None), str) and err.received:
                token = err.received
        except Exception:
            outcome = "other_rejection"

    subtype = ap.bounded_operation_subtype(token if token else None)
    return {
        "validator_outcome": outcome,
        "operation_source": source,
        "subtype": subtype,
        "token_length": len(token),
        "token_sha256": hashlib.sha256(token.encode("utf-8", errors="replace")).hexdigest(),
    }


def run_probe(*, allow_network: bool, out_dir: pathlib.Path, now_utc: str,
              offline_response: str = _OFFLINE_RESPONSE,
              model_alias: str = "test-model",
              upstream_cap: int = 1) -> pathlib.Path:
    import laozhang_api as lz

    if allow_network and model_alias in REFUSED_NETWORK_MODELS:
        raise ValueError(
            f"--allow-network requires an explicit production model alias; "
            f"{model_alias!r} is refused")

    observed = {"create_entered": 0, "upstream_requests": 0, "response_received": False}
    captured = {"raw": "", "usage": None, "served_by": "", "served_model": ""}
    real_factory = lz.make_narasi_client

    # 🔴 THE INDEPENDENT COUNT MUST SIT AT A DIFFERENT BOUNDARY. Wrapping the outer
    #    `.create()` and then comparing it to a production counter that also wraps the
    #    outer `.create()` compares a number with itself: both read 1 while the
    #    failover chain quietly issued two upstream requests. These wrappers sit on the
    #    TRANSPORTS the rung loop actually calls, so a retry or a second rung shows up
    #    here and a disagreement with the reported number becomes visible.
    _transport_names = _TRANSPORT_NAMES
    _real_transports = {}

    def _wrap_transport(name):
        real = getattr(lz, name)

        def _counted(*a, **kw):
            # OBSERVE ONLY — the cap is enforced by the production ledger's atomic
            # `reserve()`. A wrapper that refused HERE would refuse only after
            # production had already counted the request (reported 4, spent 1).
            # A REFUSED invocation is not a request: the reservation now lives inside
            # the helper, so "helper called" and "transport attempted" are different
            # events and only the second one costs money.
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
        # The chain's default rung builds a client inline; count at ITS create.
        client = _real_openai(*a, **kw)
        inner_create = client.chat.completions.create

        def _c(**kw_):
            observed["upstream_requests"] += 1
            return inner_create(**kw_)
        client.chat.completions.create = _c
        return client

    def _offline_client():
        """The dry-run stand-in for `make_client(model) -> OpenAI`.

        It must answer `with_options`: since F4b Phase 0 the structural lane refuses to
        dispatch on a plain client whose SDK retries it cannot switch off, so a stub
        without the method would make every offline probe report `provider_error` and
        zero cost — a green-looking artifact that never exercised the lane at all."""
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **_kw: SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content=offline_response),
                    finish_reason="stop")],
                usage=SimpleNamespace(prompt_tokens=120, completion_tokens=45)))))
        client.with_options = lambda **_kw: client
        return client

    def _counting_factory(*args, **kwargs):
        client = (real_factory(*args, **kwargs) if allow_network
                  else _offline_client())
        inner = client.chat.completions.create

        _is_failover = isinstance(client, lz._NarasiFailoverClient)

        def _counted(**kwargs_):
            # Independent of the production counter ON PURPOSE: the artifact reports
            # both, and a mismatch is the finding.
            #
            # For a PLAIN client this `.create` IS the transport, so it counts as an
            # upstream request here. For the FAILOVER client it is not — the chain may
            # issue several — and those are counted by the transport wrappers instead.
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
            observed["response_received"] = True
            try:
                captured["raw"] = lz._resp_content(response) or ""
            except Exception:
                captured["raw"] = ""
            captured["usage"] = getattr(response, "usage", None)
            captured["served_by"] = str(
                getattr(response, "_narasi_served_by", "") or "")[:32]
            captured["served_model"] = str(
                getattr(response, "_narasi_served_model", "") or "")[:64]
            return response

        client.chat.completions.create = _counted
        return client

    async def _no_credit(*_a, **_k):
        return 0

    lz.make_narasi_client = _counting_factory
    real_usage = lz._log_narasi_usage
    lz._log_narasi_usage = _no_credit
    # The cap belongs to the object that also does the counting.
    cap_token = lz._NARASI_UPSTREAM_CAP.set(int(upstream_cap))
    for _name in _transport_names:
        _wrap_transport(_name)
    if _real_openai is not None:
        lz.OpenAI = _counted_openai
    try:
        _revised, _credits, stats = asyncio.run(lz._narasi_structural_patch_revise(
            PROBE_CHAPTER, [dict(PROBE_FINDING)], "storytelling", "id", model_alias,
            tenant_id="probe", user_id="probe", job_uuid=None,
            credit_row=False,                       # never bill a customer ledger row
            authority_text="AUTHORITY", outline_packets=dict(PROBE_PACKET)))
    finally:
        lz.make_narasi_client = real_factory
        lz._log_narasi_usage = real_usage
        lz._NARASI_UPSTREAM_CAP.reset(cap_token)
        for _name, _real in _real_transports.items():
            setattr(lz, _name, _real)
        if _real_openai is not None:
            lz.OpenAI = _real_openai

    reported = int(stats["provider_calls"])
    entered = int(observed["create_entered"])
    # The comparison that means something: BOTH sides measure upstream requests. NO
    # fallback to the adapter count — substituting `entered` when the transport
    # observer never fired manufactured an "independent" observation out of the very
    # number it was supposed to check, and a plain-client stub then published
    # `recording_mode="live"` on zero observed transports.
    upstream = int(observed["upstream_requests"])
    # 🔴 `live` IS EARNED BY EVIDENCE, NOT GRANTED BY A FLAG. The first version set it
    #    from `--allow-network` alone, so a paid run whose client factory raised before
    #    `.create` — no request, no response, nothing observed — was still filed as
    #    live evidence closing the DoD. A run may call itself live only if a request
    #    actually went out, a response actually came back, and the production counter
    #    agrees with this probe's independent observation.
    if not allow_network:
        recording_mode = "offline_dry_run"
    elif upstream >= 1 and observed["response_received"] and reported == upstream:
        recording_mode = "live"
    else:
        recording_mode = "failed_probe"

    tok_in = int(getattr(captured["usage"], "prompt_tokens", 0) or 0)
    tok_out = int(getattr(captured["usage"], "completion_tokens", 0) or 0)
    # Priced on the model that was actually SERVED, not the alias requested: the phase
    # switchboard is free to override the caller's model, and pricing the request by
    # the alias mis-bills exactly when it does (the production code carries the same
    # warning where it stamps `_narasi_served_model`).
    priced_model = captured["served_model"] or model_alias
    try:
        # PURE estimator — the same one production prices with, and it writes nothing.
        cost_usd = float(lz._calc_cost(priced_model, tok_in, tok_out))
    except Exception:
        cost_usd = 0.0

    artifact = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "probe_kind": "targeted_non_delivery",
        "recording_mode": recording_mode,
        # Named for what it actually asserts. The raw response IS observed in-process
        # and then deliberately discarded, so "raw evidence available" overstated it:
        # nobody can go back and re-read it.
        "primary_response_observed": recording_mode == "live",
        "executed_at_utc": now_utc,
        "source": {
            "head": _head(),
            "laozhang_api_sha256": _sha256_file(_REPO / "python/laozhang_api.py"),
            "narasi_addressed_patch_sha256": _sha256_file(
                _REPO / "python/narasi_addressed_patch.py"),
        },
        "provider": {
            "phase": "canon_diff_revise",
            "model_alias": model_alias,
            "resolved_model": str(lz.MODELS.get(model_alias, model_alias)),
            # Which rung actually served it, and with which model — the claim
            # "SERVED via vertex" belongs IN the record, not only in a log line a
            # reader of the artifact never sees.
            "served_by": captured["served_by"],
            "served_model": captured["served_model"],
        },
        "delivery": {"persisted": False, "returned_to_user": False},
        "counts": {
            "chapters_targeted": int(stats["targeted"]),
            "chapters_attempted": int(stats["attempted"]),
            "provider_calls_reported": reported,
            "provider_calls_observed": upstream,
            "adapter_invocations_observed": entered,
            "upstream_request_cap": int(upstream_cap),
        },
        "cost": {
            "prompt_tokens": tok_in,
            "completion_tokens": tok_out,
            "total_tokens": tok_in + tok_out,
            # Micros, so the record stays an integer and cannot drift on float repr.
            "estimated_usd_micros": int(round(cost_usd * 1_000_000)),
            "estimator": "laozhang_api._calc_cost",
            "credit_row_written": False,
        },
        "observation": _classify(captured["raw"], PROBE_CHAPTER),
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    # The filename carries the verdict too: a failed probe cannot be mistaken for the
    # live artifact by a reader who only ever looks at a directory listing.
    suffix = "" if recording_mode == "live" else f"-{recording_mode.replace('_', '-')}"
    path = (out_dir /
            f"f4a-targeted-nondelivery-probe-{now_utc.replace(':', '')}{suffix}.json")
    # Exclusive create: an artifact is a record, and a record that can be silently
    # overwritten is not one.
    with open(path, "x", encoding="utf-8") as handle:
        json.dump(artifact, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-network", action="store_true",
                        help="REQUIRED to reach a real provider. Spends AT MOST "
                             "--max-upstream billable requests (the failover chain can "
                             "retry, so this is a cap, not a promise of one). Needs "
                             "Rino's explicit permission.")
    parser.add_argument("--max-upstream", type=int, default=1,
                        help="Hard cap on billable upstream requests (default 1).")
    parser.add_argument("--out-dir", default=str(_REPO / "docs/audit/narasi"))
    parser.add_argument("--model", default="test-model",
                        help="Model alias. REQUIRED and must be a production alias "
                             "when --allow-network is used.")
    args = parser.parse_args(argv)

    if args.allow_network and os.environ.get("NARASI_F4A_PROBE_CONFIRM") != "1":
        print("refusing: --allow-network also requires NARASI_F4A_PROBE_CONFIRM=1",
              file=sys.stderr)
        return 2
    if args.allow_network and args.model in REFUSED_NETWORK_MODELS:
        print(f"refusing: --allow-network needs an explicit production --model; "
              f"{args.model!r} is refused", file=sys.stderr)
        return 2

    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path = run_probe(allow_network=args.allow_network,
                     out_dir=pathlib.Path(args.out_dir), now_utc=now,
                     model_alias=args.model, upstream_cap=max(1, int(args.max_upstream)))
    print(f"artifact written: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
