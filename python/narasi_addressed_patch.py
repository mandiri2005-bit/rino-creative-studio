"""Pure, provider-free addressed patching for one complete narrative chapter.

The module deliberately owns only deterministic segmentation, strict patch validation,
and atomic reassembly.  Provider calls, prompts, metering, semantic decisions, and the
Classic word/shape guards remain in the Classic adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping, Sequence


PATCH_SCHEMA_VERSION = "narasi_addressed_patch_v1"
MAX_PATCH_OPERATIONS = 4

_HEADING_RX = re.compile(
    r"^[^\w\n]*(?:##[ \t]|(?:Chapter|Bab|BAB|Chapitre|Cap[íi]tulo)[ \t]+\d+).*$"
)
_HEADING_ANYWHERE_RX = re.compile(
    r"(?m)^[^\w\n]*(?:##[ \t]|(?:Chapter|Bab|BAB|Chapitre|Cap[íi]tulo)[ \t]+\d+).*$"
)
_TERMINAL_RX = re.compile(r'(?:[.!?…]|—|–|--)["\'’”»\)\]]*\s*$')
_WRAPPER_RX = re.compile(
    r"(?i)^\s*(?:sure|here|certainly|okay|note:|of course|looking at|"
    r"i\s+(?:changed|revised|updated|need)|the corrected chapter)\b"
)


class PatchValidationError(ValueError):
    """Bounded validation failure safe to expose as an internal reason code."""

    def __init__(self, code: str, *, received: Any = None):
        super().__init__(code)
        self.code = code
        # Optional raw value behind the error, e.g. an unrecognized "op" string.
        # Never part of the public reason code; callers must bucket it through
        # bounded_operation_subtype() before it touches a log line, and must
        # never widen the accepted vocabulary or the reason code itself.
        self.received = received


_OPERATION_SUBTYPE_BUCKETS = ("insert", "delete", "rewrite", "update")


def bounded_operation_subtype(received: Any) -> str:
    """Classify an ``unknown_operation``'s raw ``op`` value into a closed bucket.

    Observability only (F4a). Never widens the accepted operation vocabulary
    and never changes the public ``unknown_operation`` reason code -- this
    bucket is the safe substitute a caller logs instead of the raw value.
    Matches by case-insensitive substring, so verbose model output such as
    ``"delete_paragraph"`` still buckets sensibly. First match wins in the
    fixed order above; anything with no match -- including non-string or
    empty values -- is ``"other"``.
    """
    if not isinstance(received, str) or not received.strip():
        return "other"
    lowered = received.lower()
    for bucket in _OPERATION_SUBTYPE_BUCKETS:
        if bucket in lowered:
            return bucket
    return "other"


@dataclass(frozen=True)
class AddressedUnit:
    unit_id: str
    text: str


@dataclass(frozen=True)
class SegmentedChapter:
    original: str
    heading: str
    prefix: str
    units: tuple[AddressedUnit, ...]
    separators: tuple[str, ...]
    suffix: str
    joiner: str

    def annotated(self) -> str:
        """Render model-visible IDs without changing or quoting the source prose."""
        return "\n\n".join(f"[{unit.unit_id}]\n{unit.text}" for unit in self.units)


@dataclass(frozen=True)
class PatchResult:
    text: str
    before_sha256: str
    after_sha256: str
    operations_applied: int


def _sha256(text: str) -> str:
    try:
        raw = text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise PatchValidationError("invalid_utf8") from exc
    return hashlib.sha256(raw).hexdigest()


def _paragraph_parts(body: str) -> tuple[list[str], list[str]]:
    """Split on explicit blank lines while retaining every source separator byte."""
    units: list[str] = []
    separators: list[str] = []
    cursor = 0
    for match in re.finditer(r"(?:\r?\n[ \t]*){2,}", body):
        piece = body[cursor:match.start()]
        if piece:
            units.append(piece)
            separators.append(match.group(0))
        elif separators:
            separators[-1] += match.group(0)
        cursor = match.end()
    tail = body[cursor:]
    if tail:
        units.append(tail)
    if len(separators) >= len(units):
        separators = separators[: max(0, len(units) - 1)]
    return units, separators


def segment_chapter(chapter: str) -> SegmentedChapter:
    """Build stable ``uNNN`` IDs from a complete chapter's original paragraphs."""
    if not isinstance(chapter, str) or not chapter:
        raise PatchValidationError("chapter_empty")
    _sha256(chapter)
    first_end = chapter.find("\n")
    first_end = len(chapter) if first_end < 0 else first_end
    heading = chapter[:first_end].rstrip("\r")
    if not _HEADING_RX.fullmatch(heading):
        raise PatchValidationError("heading_missing")

    body_offset = first_end
    while body_offset < len(chapter) and chapter[body_offset] in "\r\n \t":
        body_offset += 1
    prefix = chapter[:body_offset]
    end = len(chapter)
    while end > body_offset and chapter[end - 1] in "\r\n \t":
        end -= 1
    suffix = chapter[end:]
    bodies, separators = _paragraph_parts(chapter[body_offset:end])
    if not bodies or any(not body.strip() for body in bodies):
        raise PatchValidationError("segmentation_empty")
    units = tuple(AddressedUnit(f"u{index:03d}", body) for index, body in enumerate(bodies, 1))
    if separators:
        joiner = max(enumerate(separators), key=lambda item: (len(item[1]), -item[0]))[1]
    else:
        joiner = "\r\n\r\n" if "\r\n" in chapter else "\n\n"
    return SegmentedChapter(
        original=chapter,
        heading=heading,
        prefix=prefix,
        units=units,
        separators=tuple(separators),
        suffix=suffix,
        joiner=joiner,
    )


def _strict_json_payload(raw: str | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(raw, str):
        if raw.lstrip().startswith("```"):
            raise PatchValidationError("response_not_json")
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise PatchValidationError("response_not_json") from exc
    elif isinstance(raw, Mapping):
        parsed = raw
    else:
        raise PatchValidationError("response_not_json")
    if not isinstance(parsed, Mapping) or set(parsed) != {"schema_version", "operations"}:
        raise PatchValidationError("schema_shape")
    if parsed.get("schema_version") != PATCH_SCHEMA_VERSION:
        raise PatchValidationError("schema_version")
    if not isinstance(parsed.get("operations"), list):
        raise PatchValidationError("operations_type")
    return parsed


def _validate_prose(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PatchValidationError("prose_empty")
    text = value.strip()
    _sha256(text)
    if _HEADING_ANYWHERE_RX.search(text):
        raise PatchValidationError("prose_contains_heading")
    if _WRAPPER_RX.search(text):
        raise PatchValidationError("prose_wrapper")
    if not _TERMINAL_RX.search(text):
        raise PatchValidationError("prose_unterminated")
    return text


def _has_cycle(edges: Mapping[str, set[str]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        if any(visit(nxt) for nxt in edges.get(node, ())):
            return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in edges)


_KNOWN_OP_VERBS = frozenset({"insert_before", "insert_after", "replace", "move"})


def _normalize_verb_as_key_operation(raw_op: Any) -> Any:
    """F4b: unwrap a model response that put the verb in the WRAPPING KEY instead of
    the "op" field's value -- e.g. ``{"replace": {"unit_id": "u006", "text": "..."}}``
    instead of the spec'd ``{"op": "replace", "unit_id": "u006", "text": "..."}``.
    Grounded in a live targeted probe (2026-08-14, model=gemini-3.5-flash via vertex,
    phase=canon_diff_revise) that returned exactly this shape; NOT a vocabulary
    alias/typo guess -- see BRIEF-FOR-CODEX-2026-08-14-POST-CANARY-V9.md, F4b, on why
    a speculative alias is refused but this unambiguous structural unwrap is not.

    Fires ONLY when `raw_op` has EXACTLY one key, that key is one of the four closed
    operation verbs, its value is itself a mapping, and that mapping does not already
    carry its own "op" key (which would make the wrapping ambiguous/malformed on its
    face -- refuse rather than guess which "op" wins). Returns the original `raw_op`
    unchanged in every other case, including an unrecognized wrapping key (e.g.
    "delete") -- that stays an unknown_operation for the caller to classify."""
    if not isinstance(raw_op, Mapping) or len(raw_op) != 1:
        return raw_op
    (verb, fields), = raw_op.items()
    if verb not in _KNOWN_OP_VERBS or not isinstance(fields, Mapping) or "op" in fields:
        return raw_op
    return {"op": verb, **fields}


def _validated_operations(
    segmented: SegmentedChapter, raw: str | Mapping[str, Any]
) -> list[dict[str, str]]:
    parsed = _strict_json_payload(raw)
    operations = parsed["operations"]
    if not operations:
        raise PatchValidationError("operations_empty")
    if len(operations) > MAX_PATCH_OPERATIONS:
        raise PatchValidationError("operations_cap")
    known = {unit.unit_id for unit in segmented.units}
    edit_targets: set[str] = set()
    replacement_targets: set[str] = set()
    move_targets: set[str] = set()
    move_anchors: set[str] = set()
    insert_anchors: set[str] = set()
    move_edges: dict[str, set[str]] = {}
    validated: list[dict[str, str]] = []

    for raw_op in operations:
        if not isinstance(raw_op, Mapping):
            raise PatchValidationError("operation_type")
        raw_op = _normalize_verb_as_key_operation(raw_op)
        kind = raw_op.get("op")
        received_for_telemetry = kind
        if kind is None and len(raw_op) == 1:
            (_only_key, _only_val), = raw_op.items()
            if isinstance(_only_val, Mapping):
                # Always surface the attempted wrapping key for unknown_operation's
                # F4a telemetry (e.g. "delete"), even when it can't be dispatched.
                received_for_telemetry = _only_key
                # But only ADOPT it as the dispatch `kind` when it is NOT one of the
                # four closed verbs. A known verb reaching here (adversarial-audit
                # finding, 2026-08-14 night) means the normalization above saw an
                # AMBIGUOUS shape (its inner mapping already carried its own "op")
                # and correctly refused to unwrap it -- dispatching on the wrapping
                # key anyway would route it into that verb's OWN shape-check branch
                # instead of unknown_operation, silently skipping F4a's telemetry
                # (gated strictly on code == "unknown_operation") and reclassifying
                # which rejected_reason_counts bucket the rejection lands in.
                if _only_key not in _KNOWN_OP_VERBS:
                    kind = _only_key
        if kind in ("insert_before", "insert_after"):
            if set(raw_op) != {"op", "anchor_id", "text"}:
                raise PatchValidationError("operation_shape")
            anchor = raw_op.get("anchor_id")
            if anchor not in known:
                raise PatchValidationError("unknown_id")
            if anchor in insert_anchors:
                raise PatchValidationError("ambiguous_insert_order")
            insert_anchors.add(anchor)
            validated.append({"op": kind, "anchor_id": anchor, "text": _validate_prose(raw_op.get("text"))})
            continue

        if kind == "replace":
            if set(raw_op) != {"op", "unit_id", "text"}:
                raise PatchValidationError("operation_shape")
            unit_id = raw_op.get("unit_id")
            if unit_id not in known:
                raise PatchValidationError("unknown_id")
            if unit_id in edit_targets or unit_id in insert_anchors:
                raise PatchValidationError("operation_overlap")
            edit_targets.add(unit_id)
            replacement_targets.add(unit_id)
            validated.append({"op": kind, "unit_id": unit_id, "text": _validate_prose(raw_op.get("text"))})
            continue

        if kind == "move":
            expected_before = {"op", "unit_id", "before_id"}
            expected_after = {"op", "unit_id", "after_id"}
            keys = set(raw_op)
            if keys not in (expected_before, expected_after):
                raise PatchValidationError("operation_shape")
            unit_id = raw_op.get("unit_id")
            anchor_key = "before_id" if "before_id" in raw_op else "after_id"
            anchor = raw_op.get(anchor_key)
            if unit_id not in known or anchor not in known:
                raise PatchValidationError("unknown_id")
            if unit_id == anchor:
                raise PatchValidationError("self_reference")
            if (unit_id in edit_targets or unit_id in insert_anchors
                    or anchor in replacement_targets):
                raise PatchValidationError("operation_overlap")
            edit_targets.add(unit_id)
            move_targets.add(unit_id)
            move_anchors.add(anchor)
            before, after = ((unit_id, anchor) if anchor_key == "before_id" else (anchor, unit_id))
            move_edges.setdefault(before, set()).add(after)
            validated.append({"op": kind, "unit_id": unit_id, anchor_key: anchor})
            continue

        raise PatchValidationError("unknown_operation", received=received_for_telemetry)

    if insert_anchors & edit_targets:
        raise PatchValidationError("operation_overlap")
    # A replace and a move that uses the replaced unit as its anchor describe the
    # same intent regardless of array order.  Check the complete operation set so
    # [move, replace] cannot be accepted while [replace, move] is rejected.
    if replacement_targets & move_anchors:
        raise PatchValidationError("operation_overlap")
    if _has_cycle(move_edges):
        raise PatchValidationError("move_cycle")
    return validated


def _assemble(segmented: SegmentedChapter, operations: Sequence[Mapping[str, str]]) -> str:
    order = [unit.unit_id for unit in segmented.units]
    text_by_id = {unit.unit_id: unit.text for unit in segmented.units}
    inserted_before: dict[str, str] = {}
    inserted_after: dict[str, str] = {}
    topology_changed = False

    for op in operations:
        kind = op["op"]
        if kind == "replace":
            text_by_id[op["unit_id"]] = op["text"]
        elif kind in ("insert_before", "insert_after"):
            topology_changed = True
            target = inserted_before if kind == "insert_before" else inserted_after
            target[op["anchor_id"]] = op["text"]
        else:
            topology_changed = True
            unit_id = op["unit_id"]
            anchor_key = "before_id" if "before_id" in op else "after_id"
            anchor = op[anchor_key]
            order.remove(unit_id)
            anchor_index = order.index(anchor)
            order.insert(anchor_index if anchor_key == "before_id" else anchor_index + 1, unit_id)

    # Validation treats every move as a declarative ordering relation over the
    # original ID table (not as an imperative edit whose meaning changes with its
    # array position).  Sequential assembly is allowed only when its final order
    # satisfies every declared relation jointly; otherwise reject the whole patch.
    final_position = {unit_id: index for index, unit_id in enumerate(order)}
    for op in operations:
        if op["op"] != "move":
            continue
        unit_position = final_position[op["unit_id"]]
        if "before_id" in op:
            relation_holds = unit_position < final_position[op["before_id"]]
        else:
            relation_holds = unit_position > final_position[op["after_id"]]
        if not relation_holds:
            raise PatchValidationError("move_unsatisfiable")

    rendered: list[str] = []
    for unit_id in order:
        if unit_id in inserted_before:
            rendered.append(inserted_before[unit_id])
        rendered.append(text_by_id[unit_id])
        if unit_id in inserted_after:
            rendered.append(inserted_after[unit_id])

    if topology_changed:
        body = segmented.joiner.join(rendered)
    else:
        # Replacements keep every original gap byte as well as untouched unit bodies.
        chunks: list[str] = []
        for index, unit in enumerate(segmented.units):
            chunks.append(text_by_id[unit.unit_id])
            if index < len(segmented.separators):
                chunks.append(segmented.separators[index])
        body = "".join(chunks)
    return segmented.prefix + body + segmented.suffix


def apply_addressed_patch(
    segmented: SegmentedChapter, raw: str | Mapping[str, Any]
) -> PatchResult:
    """Validate the complete operation set, then apply it atomically or raise."""
    operations = _validated_operations(segmented, raw)
    output = _assemble(segmented, operations)
    if output == segmented.original:
        raise PatchValidationError("ineffective")
    if output.splitlines()[:1] != segmented.original.splitlines()[:1]:
        raise PatchValidationError("heading_changed")
    if _HEADING_ANYWHERE_RX.findall(output) != _HEADING_ANYWHERE_RX.findall(segmented.original):
        raise PatchValidationError("heading_sequence")
    _sha256(output)
    if not _TERMINAL_RX.search(output.rstrip()):
        raise PatchValidationError("output_unterminated")
    return PatchResult(
        text=output,
        before_sha256=_sha256(segmented.original),
        after_sha256=_sha256(output),
        operations_applied=len(operations),
    )


__all__ = [
    "AddressedUnit",
    "MAX_PATCH_OPERATIONS",
    "PATCH_SCHEMA_VERSION",
    "PatchResult",
    "PatchValidationError",
    "SegmentedChapter",
    "apply_addressed_patch",
    "bounded_operation_subtype",
    "segment_chapter",
]
