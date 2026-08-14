# -*- coding: utf-8 -*-
"""Deterministic, provider-free outline execution packets for Narasi.

The accepted outline remains the authority.  This module only makes the adjacent
planned states and explicitly-labelled beat order salient; it never asks a model to
interpret, paraphrase, or amend an outline.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Sequence


OUTLINE_PACKET_CONTRACT_VERSION = "outline_packet_v2"

_LABEL_RX = re.compile(
    r"(?<!\S)(?P<label>[A-Z][A-Z0-9 &/\-'’]{1,64}:)(?=\s|$)"
)


def _summary(chapter: Mapping[str, Any]) -> str:
    return str(chapter.get("summary") or chapter.get("description") or "").strip()


def _title(chapter: Mapping[str, Any], index: int) -> str:
    return str(chapter.get("title", "") or "").strip() or f"Chapter {index + 1}"


def split_ordered_outline_beats(summary: str) -> tuple[str, ...]:
    """Return conservative verbatim spans in source order.

    Explicit lines are boundaries.  Within each line, uppercase outline labels are
    boundaries.  Unlabelled sentences stay with the preceding label; an unstructured
    paragraph remains one beat rather than being split by semantic guesswork.
    """
    text = str(summary or "").strip()
    if not text:
        return ()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    chunks = lines if len(lines) > 1 else [text]
    beats: list[str] = []
    for chunk in chunks:
        matches = list(_LABEL_RX.finditer(chunk))
        if not matches:
            beats.append(chunk)
            continue
        prefix = chunk[:matches[0].start()].strip()
        if prefix:
            beats.append(prefix)
        for pos, match in enumerate(matches):
            end = matches[pos + 1].start() if pos + 1 < len(matches) else len(chunk)
            beat = chunk[match.start():end].strip()
            if beat:
                beats.append(beat)
    return tuple(beats) or (text,)


def render_outline_execution_packet(
    chapters: Sequence[Mapping[str, Any]], chapter_index: int
) -> str:
    """Render the exact dynamic outline packet for one 0-based chapter index."""
    if not chapters or chapter_index < 0 or chapter_index >= len(chapters):
        return ""
    current = chapters[chapter_index]
    beats = split_ordered_outline_beats(_summary(current))
    lines = [
        f"OUTLINE PACKET CONTRACT: {OUTLINE_PACKET_CONTRACT_VERSION}",
        "The states below are accepted-outline commitments, not observations of "
        "another parallel worker's prose.",
        "The numbered beats are internal planning references. Render them as flowing "
        "prose; never copy their numbering or Scene N / Chapter N planning labels.",
        "",
        "PREVIOUS OUTLINE COMMITMENT:",
    ]
    if chapter_index == 0:
        lines.append("NONE — this chapter opens the book.")
    else:
        previous = chapters[chapter_index - 1]
        lines.extend([
            f"Chapter {chapter_index}: \"{_title(previous, chapter_index - 1)}\"",
            _summary(previous) or "(No synopsis text supplied.)",
        ])

    lines.extend([
        "",
        "CURRENT ORDERED OUTLINE BEATS:",
        f"Chapter {chapter_index + 1}: \"{_title(current, chapter_index)}\"",
    ])
    if beats:
        lines.extend(f"  {number}. {beat}" for number, beat in enumerate(beats, 1))
    else:
        lines.append("  1. (No synopsis text supplied.)")

    lines.extend(["", "NEXT RESERVED OUTLINE STATE:"])
    if chapter_index == len(chapters) - 1:
        lines.append("NONE — this chapter closes the book.")
    else:
        following = chapters[chapter_index + 1]
        lines.extend([
            f"Chapter {chapter_index + 2}: \"{_title(following, chapter_index + 1)}\"",
            _summary(following) or "(No synopsis text supplied.)",
        ])

    lines.append("")
    if chapter_index == 0:
        lines.append(
            "STORY CLOCK START: establish the opening position of every bounded "
            "duration or deadline that the outline makes important."
        )
    else:
        lines.append(
            "ON-PAGE HANDOFF: before the first new set-piece, show the elapsed interval, "
            "the causal change or decision, and the opening location. A time label alone "
            "is insufficient. Do not execute the NEXT RESERVED state."
        )
    lines.extend([
        "STORY CLOCK: along the forward timeline, advance any bounded duration "
        "monotonically. An explicitly labelled flashback or parallel track may narrate "
        "an earlier point without moving the forward clock backward. Do not invent an "
        "absolute date absent from the outline or declare a deadline complete before "
        "the intervening time is accounted for on-page.",
        "FINAL EXECUTION CHECKLIST: satisfy every current outline event in source "
        "order unless the outline explicitly labels a flashback, parallel action, or "
        "another chronology; do not complete a future event early; keep person and "
        "tense stable. Callbacks may recur, but an outlined event must not be re-staged "
        "as a second contradictory occurrence.",
        "Priority: accepted outline > Bible/canonical facts > critique > prose.",
    ])
    return "\n".join(lines).strip()


def outline_packet_record(
    chapters: Sequence[Mapping[str, Any]], chapter_index: int
) -> dict[str, str | int]:
    text = render_outline_execution_packet(chapters, chapter_index)
    return {
        "chapter_index": chapter_index,
        "contract_version": OUTLINE_PACKET_CONTRACT_VERSION,
        "packet_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "text": text,
    }


def outline_packet_bundle(chapters: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    records = [outline_packet_record(chapters, index) for index in range(len(chapters))]
    identity = [
        [record["chapter_index"], record["packet_sha256"]] for record in records
    ]
    canonical = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
    return {
        "contract_version": OUTLINE_PACKET_CONTRACT_VERSION,
        "bundle_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "packet_sha256_by_chapter": {
            str(int(record["chapter_index"]) + 1): record["packet_sha256"]
            for record in records
        },
        "packets_by_chapter": {
            str(int(record["chapter_index"]) + 1): record["text"] for record in records
        },
    }


__all__ = [
    "OUTLINE_PACKET_CONTRACT_VERSION",
    "outline_packet_bundle",
    "outline_packet_record",
    "render_outline_execution_packet",
    "split_ordered_outline_beats",
]
