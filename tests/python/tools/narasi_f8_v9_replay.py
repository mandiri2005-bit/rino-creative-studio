#!/usr/bin/env python3
"""Offline exact-v9 seam replay through the REAL addressed-patch lane.

    python3 tests/python/tools/narasi_f8_v9_replay.py --input <path> [--out <dir>]

🔴 WHY THIS RUNS THE PRODUCTION LANE. The first version inserted a bridge itself, hardcoded the
   post-census as clean and reported `provider_calls` it had never made — a replay that grades
   its own homework. Everything here now goes through `_narasi_structural_patch_revise`: the
   real prompt, the real addressed-patch validator, the real accept/reject rules, the real
   per-address attribution. The only substitutions are the transport (a deterministic local
   client double) and the post-verifier — and the verifier INSPECTS the repaired bytes rather
   than being told what to say.

🔴 HYGIENE IS PART OF THE CONTRACT.
     · no network, no real provider, no credential — the client is a local double;
     · the manuscript is never printed, never logged, never copied into the repository;
     · every emitted line is a count, a 1-based index, a bounded identity, or a SHA-256;
     · the input hash is verified BEFORE the replay;
     · output is refused if it would land inside the repository.

🔴 THE HONEST LIMIT. `provider_double_calls` counts calls to the LOCAL double; real paid calls
   are zero and are reported separately as such. The census the model would have returned cannot
   be obtained without an authorised provider call, so the pre-repair observation is the two
   documented v9 elisions and the post-repair observation is computed by inspecting the bytes
   for the transition the directive asked for. What this proves is the server-owned loop —
   framing, directive, routing, the addressed-patch lane, address attribution, verification and
   accounting — over the real manuscript. It is NOT evidence of live model compliance.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import pathlib
import sys
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "tests" / "python"))

import narasi_f8 as f8              # noqa: E402
import narasi_gate as ng            # noqa: E402
import laozhang_api as lz           # noqa: E402
from narasi_addressed_patch import (PATCH_SCHEMA_VERSION,  # noqa: E402
                                    segment_chapter)

EXPECTED_INPUT_SHA = "ae7e241c2074a8b28beebc6e774436c7a9e47f00c91d15d280434f4862545714"

#: The two documented v9 elisions. 1→2 summarises the relationship thaw off-page (causal and
#: time elided, the setting does not move); 2→3 jumps rooftop → boardroom with the decision,
#: the evidence gathering, the preparation, the journey and the elapsed time all absent.
V9_ELISIONS = {
    (1, 2): {"causal": "missing", "location": "continuous", "time": "missing"},
    (2, 3): {"causal": "missing", "location": "missing", "time": "missing"},
}

#: What the double writes. Deterministic, and each clause is the evidence the post-verifier
#: looks for — so a repair that omitted one would be caught rather than assumed.
_BRIDGE_CAUSAL = "Ia memutuskan pergi setelah percakapan itu"
_BRIDGE_LOCATION = "menempuh jalan dari atap sampai ke gedung itu"
_BRIDGE_TIME = "tiga minggu kemudian"
_BRIDGE = (f"{_BRIDGE_CAUSAL}, {_BRIDGE_LOCATION}, dan {_BRIDGE_TIME} pintu itu dibuka. ")

_EVIDENCE = {"causal": _BRIDGE_CAUSAL, "location": _BRIDGE_LOCATION, "time": _BRIDGE_TIME}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _census_rows(chapter_count: int, elisions: dict) -> list:
    rows = []
    for first in range(1, chapter_count):
        pair = elisions.get((first, first + 1))
        rows.append({"chapter_a": first, "chapter_b": first + 1,
                     **(pair or {d: "explicit" for d in f8.DIMENSIONS})})
    return rows


def _chapter_blocks(text):
    """(index, block) for every block that carries a heading, 1-based."""
    out, index = [], 0
    for block in ng.split_chapter_blocks(text):
        if not ng.chapter_heading_line(block):
            out.append((None, block))
            continue
        index += 1
        out.append((index, block))
    return out


class _Double:
    """A deterministic local stand-in for the transport. No network, no credential."""

    def __init__(self, calls):
        self.calls = calls

    def create(self, **kwargs):
        self.calls.append(1)
        prompt = kwargs["messages"][-1]["content"]
        # The real prompt carries the ORIGINAL ADDRESS TABLE; the double answers the way a
        # compliant provider would — one replace against the opening unit.
        if "[u001]\n" not in prompt:
            return self._reply(json.dumps({"error": "no address table"}))
        # The table is `[uNNN]\n<unit text>`; the opening unit's text is what a compliant
        # provider would rewrite, so the double reproduces it verbatim behind the bridge.
        opening = prompt.split("[u001]\n", 1)[1].split("\n[u", 1)[0].strip()
        return self._reply(json.dumps({
            "schema_version": PATCH_SCHEMA_VERSION,
            "operations": [{"op": "replace", "unit_id": "u001",
                            "text": _BRIDGE + opening}],
        }))

    @staticmethod
    def _reply(content):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content),
                                     finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))


def _post_census(repaired_blocks, elisions):
    """Read the REPAIRED bytes and report each dimension on the evidence actually present.

    🔴 THIS IS THE HALF THAT MUST NOT BE HARDCODED. A verifier told to say "clean" proves the
    plumbing and nothing else. Each dimension is reported `explicit` only when the clause the
    directive asked for is present in the chapter that opens the seam."""
    by_index = {i: b for i, b in repaired_blocks if i is not None}
    rows = []
    for (first, second), dims in sorted(elisions.items()):
        body = by_index.get(second, "")
        row = {"chapter_a": first, "chapter_b": second}
        for dimension in f8.DIMENSIONS:
            was = dims.get(dimension, "explicit")
            if was != "missing":
                row[dimension] = was
            else:
                row[dimension] = ("explicit" if _EVIDENCE[dimension] in body else "missing")
        rows.append(row)
    return rows


def _replay_packet(block) -> str:
    """A minimal packet reconstructed from the chapter's OWN heading.

    🔴 LABELLED, NOT DISGUISED. The structural lane refuses a chapter with no outline packet
    (`outline_packet_missing`), and the real v9 artifact carries no accepted outline offline.
    This is NOT authority — it exists so the lane's own validation, prompting and accept rules
    actually run. The report says so in `outline_packet_source`."""
    heading = ng.chapter_heading_line(block).strip().lstrip("#").strip()
    return f"CURRENT ORDERED OUTLINE BEATS:\n  1. {heading}\n"


async def _repair(chapter_text, violations, calls):
    async def _usage(*_a, **_k):
        return 0

    lz.make_narasi_client = lambda *_a, **_k: SimpleNamespace(
        chat=SimpleNamespace(completions=_Double(calls)),
        with_options=lambda **_k2: SimpleNamespace(
            chat=SimpleNamespace(completions=_Double(calls))))
    lz._log_narasi_usage = _usage
    lz._narasi_revise_timeout = lambda *_a: 5.0
    return await lz._narasi_structural_patch_revise(
        chapter_text, violations, "storytelling", "id", "replay-model",
        tenant_id="replay", user_id="replay", job_uuid=None,
        authority_text="AUTHORITY",
        outline_packets={str(violations[0]["chapter_b"]): _replay_packet(chapter_text)})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--out", default=None)
    parser.add_argument("--allow-any-input", action="store_true")
    args = parser.parse_args()

    source = pathlib.Path(args.input)
    if not source.is_file():
        print(f"REFUSED input_missing {source}")
        return 2
    raw = source.read_text(encoding="utf-8")
    input_sha = _sha(raw)
    if input_sha != EXPECTED_INPUT_SHA and not args.allow_any_input:
        print(f"REFUSED input_hash_mismatch got={input_sha[:16]}")
        return 2

    counts = ng.chapter_word_counts(raw)
    chapter_count = len(counts)
    blocks = _chapter_blocks(raw)
    openings = {i: " ".join(b[len(ng.chapter_heading_line(b)):].split())[:180]
                for i, b in blocks if i is not None}

    before = f8.seam_census(_census_rows(chapter_count, V9_ELISIONS),
                            chapter_count=chapter_count)
    detected = f8.detect(before, openings=openings)
    by_target = {}
    for violation in detected:
        by_target.setdefault(violation["chapter_b"], []).append(violation)

    # ── the REAL lane, one chapter at a time, against a local double ──────────────
    calls, repaired_blocks, accepted_ids, changed = [], [], set(), set()
    lane = {"targeted": 0, "attempted": 0, "accepted": 0, "f8_calls": 0}
    for index, block in blocks:
        if index is None or index not in by_target:
            repaired_blocks.append((index, block))
            continue
        text, _credits, stats = asyncio.run(_repair(block, by_target[index], calls))
        lane["targeted"] += stats["targeted"]
        lane["attempted"] += len(stats.get("f8_attempted_ids") or ())
        lane["accepted"] += len(stats.get("accepted_violation_ids") or ())
        lane["f8_calls"] += int(stats.get("f8_provider_calls") or 0)
        accepted_ids |= set(stats.get("accepted_violation_ids") or ())
        if text != block:
            changed.add(index)
        repaired_blocks.append((index, text))

    repaired = "".join(b for _i, b in repaired_blocks)
    after = f8.seam_census(_post_census(repaired_blocks, V9_ELISIONS),
                           chapter_count=chapter_count)
    verdict = f8.verify(before=before, after=after, targeted=detected,
                        changed_chapters=changed,
                        allowed_chapters={v["chapter_b"] for v in detected},
                        attribution=accepted_ids)
    accounting = f8.accounting(
        census_before=before, targeted=detected, verdict=verdict,
        attempts=lane["attempted"], provider_calls=lane["f8_calls"],
        accepted_operations=lane["accepted"], byte_changing=len(changed & {
            v["chapter_b"] for v in detected if v["seam"] in accepted_ids}))

    untouched_ok = all(
        b0 == b1 for (i0, b0), (_i1, b1) in zip(blocks, repaired_blocks)
        if i0 is None or i0 not in changed)

    report = {
        "replay": "narasi.f8.v9.offline.v2",
        "outline_packet_source": "replay_reconstructed_from_headings",
        "input_sha256": input_sha,
        "result_sha256": _sha(repaired),
        "manuscript_modified": repaired != raw,
        "real_paid_provider_calls": 0,
        "network_calls": 0,
        "provider_double_calls": len(calls),
        "lane": dict(lane),
        "chapter_count": chapter_count,
        "chapter_count_after": sum(1 for i, _b in _chapter_blocks(repaired) if i is not None),
        "headings_well_formed_after": bool(ng.chapter_headings_well_formed(repaired)),
        "word_counts_before": counts[:f8.MAX_PERSISTED_IDS],
        "changed_chapters": sorted(changed),
        "untouched_chapters_identical": bool(untouched_ok),
        "detected_seams": sorted(before.get("missing") or {}),
        "detected_dimensions": sorted(before.get("missing_dims") or ()),
        "targets": sorted(f"{v['seam']}->ch{v['chapter']}" for v in detected),
        "attributed_seams": sorted(accepted_ids),
        "post_census_reason": after.get("reason"),
        "post_census_missing": sorted(after.get("missing") or {}),
        "accounting": accounting,
    }
    print(json.dumps(report, indent=2, sort_keys=True))

    if args.out:
        out_dir = pathlib.Path(args.out).resolve()
        if out_dir == ROOT or ROOT in out_dir.parents:
            print("REFUSED output_inside_repository")
            return 2
        out_dir.mkdir(parents=True, exist_ok=True)
        blob = json.dumps(report, indent=2, sort_keys=True)
        target = out_dir / f"f8-v9-replay-{input_sha[:12]}.json"
        target.write_text(blob, encoding="utf-8")
        print(f"REPORT {target} sha256={_sha(blob)}")
    return 0 if not accounting["delivery_blocked"] else 1


if __name__ == "__main__":
    sys.exit(main())
