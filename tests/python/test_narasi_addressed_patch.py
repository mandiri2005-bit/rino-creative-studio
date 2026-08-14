import hashlib
import json
import asyncio
from types import SimpleNamespace
import os
from pathlib import Path
import subprocess
import sys

import pytest

from narasi_addressed_patch import (
    PATCH_SCHEMA_VERSION,
    PatchValidationError,
    apply_addressed_patch,
    segment_chapter,
)
import laozhang_api as lz


CHAPTER = (
    "## Bab 3: Atap\n\n"
    "Mira membawa hak penyerahan itu ke meja rapat.\n\n"
    "Ia menyerahkan haknya sebelum rekaman diputar.\n\n"
    "Hakim lalu menyatakan rekaman tak dapat diterima.\n\n"
    "Deposisinya menutup sidang.\n"
)


def _payload(*operations):
    return json.dumps({"schema_version": PATCH_SCHEMA_VERSION, "operations": list(operations)})


def test_golden_patch_applies_to_complete_chapter_and_preserves_untouched_units_byte_exact():
    segmented = segment_chapter(CHAPTER)
    result = apply_addressed_patch(
        segmented,
        _payload({
            "op": "replace",
            "unit_id": "u002",
            "text": "Ia menyerahkan seluruh haknya sebelum rekaman diputar.",
        }),
    )

    assert result.text.startswith("## Bab 3: Atap\n\n")
    assert result.text.endswith("Deposisiannya menutup sidang.\n") is False
    assert result.text.endswith("Deposisinya menutup sidang.\n")
    assert [u.text for u in segment_chapter(result.text).units][0] == segmented.units[0].text
    assert [u.text for u in segment_chapter(result.text).units][2:] == [u.text for u in segmented.units][2:]
    assert result.before_sha256 == hashlib.sha256(CHAPTER.encode()).hexdigest()
    assert result.after_sha256 == hashlib.sha256(result.text.encode()).hexdigest()
    assert result.after_sha256 != result.before_sha256


def test_move_uses_original_ids_and_retains_all_original_unit_bodies():
    segmented = segment_chapter(CHAPTER)
    result = apply_addressed_patch(
        segmented,
        _payload({"op": "move", "unit_id": "u004", "before_id": "u003"}),
    )
    assert result.text.index("Deposisinya") < result.text.index("Hakim lalu")
    for unit in segmented.units:
        assert unit.text in result.text
    assert result.text.splitlines()[0] == CHAPTER.splitlines()[0]


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("Bab lengkap, bukan JSON.", "response_not_json"),
        ("```json\n{}\n```", "response_not_json"),
        (_payload(), "operations_empty"),
        (_payload({"op": "replace", "unit_id": "u999", "text": "Valid."}), "unknown_id"),
        (_payload({"op": "delete", "unit_id": "u001"}), "unknown_operation"),
        (_payload({"op": "move", "unit_id": "u001", "before_id": "u001"}), "self_reference"),
        (_payload(
            {"op": "move", "unit_id": "u001", "before_id": "u002"},
            {"op": "move", "unit_id": "u002", "before_id": "u001"},
        ), "move_cycle"),
        (_payload(
            {"op": "insert_before", "anchor_id": "u002", "text": "Pertama."},
            {"op": "insert_after", "anchor_id": "u002", "text": "Kedua."},
        ), "ambiguous_insert_order"),
        (_payload({"op": "replace", "unit_id": "u001", "text": "## Bab 9: Rusak."}), "prose_contains_heading"),
        (_payload({"op": "replace", "unit_id": "u001", "text": "fragmen tanpa tanda akhir"}), "prose_unterminated"),
        (_payload({"op": "replace", "unit_id": "u001", "text": CHAPTER}), "prose_contains_heading"),
    ],
)
def test_rejection_matrix_is_atomic(raw, code):
    segmented = segment_chapter(CHAPTER)
    before = segmented.original
    with pytest.raises(PatchValidationError) as exc:
        apply_addressed_patch(segmented, raw)
    assert exc.value.code == code
    assert segmented.original == before


def test_ineffective_replacement_is_rejected_and_hash_never_claims_change():
    segmented = segment_chapter(CHAPTER)
    with pytest.raises(PatchValidationError, match="ineffective"):
        apply_addressed_patch(
            segmented,
            _payload({"op": "replace", "unit_id": "u001", "text": segmented.units[0].text}),
        )


def test_address_annotation_is_id_only_and_preserves_prose_verbatim():
    segmented = segment_chapter(CHAPTER)
    annotated = segmented.annotated()
    for unit in segmented.units:
        assert f"[{unit.unit_id}]\n{unit.text}" in annotated


def test_classic_structural_adapter_applies_golden_patch_and_records_real_attempt(monkeypatch):
    patch = _payload({
        "op": "replace",
        "unit_id": "u002",
        "text": "Ia menyerahkan seluruh haknya sebelum rekaman diputar.",
    })
    captured = {}

    class _Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content=patch), finish_reason="stop")],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            )

    async def _usage(*args, **kwargs):
        return 7

    monkeypatch.setattr(
        lz, "make_narasi_client",
        lambda *args, **kwargs: SimpleNamespace(chat=SimpleNamespace(completions=_Completions())),
    )
    monkeypatch.setattr(lz, "_log_narasi_usage", _usage)
    monkeypatch.setattr(lz, "_narasi_revise_timeout", lambda *args: 2.0)

    finding = {
        "severity": "high", "type": "outline_beat_order", "chapter": 3,
        "evidence": "Surrender must precede the ruling.", "fix": "Restore the outlined order.",
    }
    revised, credits, stats = asyncio.run(lz._narasi_structural_patch_revise(
        CHAPTER, [finding], "storytelling", "id", "test-model",
        tenant_id="t", user_id="u", job_uuid=None,
        authority_text="AUTHORITY", outline_packets={"3": "EXACT PACKET"},
    ))

    assert "seluruh haknya" in revised
    assert credits == 7
    assert stats["targeted"] == stats["attempted"] == stats["accepted"] == 1
    assert stats["not_attempted_reason_counts"] == {}
    assert captured["response_format"] == {"type": "json_object"}
    assert "EXACT PACKET" in captured["messages"][1]["content"]
    assert CHAPTER not in captured["messages"][1]["content"]


def test_structural_target_without_packet_is_explicitly_not_attempted(monkeypatch):
    monkeypatch.setattr(
        lz, "make_narasi_client",
        lambda *args, **kwargs: pytest.fail("provider must not be called without a packet"),
    )
    finding = {
        "severity": "high", "type": "outline_missing_beat", "chapter": 3,
        "evidence": "Missing beat.", "fix": "Add it.",
    }
    revised, credits, stats = asyncio.run(lz._narasi_structural_patch_revise(
        CHAPTER, [finding], "storytelling", "id", "test-model",
        tenant_id="t", user_id="u", job_uuid=None, outline_packets={},
    ))
    assert revised == CHAPTER
    assert credits == 0
    assert stats["targeted"] == 1
    assert stats["attempted"] == stats["accepted"] == 0
    assert stats["not_attempted_reason_counts"] == {"outline_packet_missing": 1}


def test_mixed_routing_defers_owned_nonstructural_and_excludes_owned_chapter(monkeypatch):
    changed = CHAPTER.replace("seluruh haknya", "semua haknya") if "seluruh haknya" in CHAPTER else CHAPTER.replace("haknya", "seluruh haknya", 1)
    captured = {}

    async def _patch(*args, **kwargs):
        return changed, 3, {
            "targeted": 1, "attempted": 1, "accepted": 1,
            "not_attempted_reason_counts": {}, "owned_chapter_numbers": {3},
        }

    async def _legacy(full_text, violations, *args, **kwargs):
        captured["input"] = full_text
        captured["violations"] = violations
        captured["excluded"] = kwargs.get("excluded_chapter_numbers")
        return full_text, 2

    monkeypatch.setattr(lz, "_narasi_structural_patch_revise", _patch)
    monkeypatch.setattr(lz, "_narasi_revise_chunked", _legacy)
    critique = {"violations": [
        {"severity": "high", "type": "outline_missing_beat", "chapter": 3},
        {"severity": "high", "type": "timeline", "chapter": 3},
        {"severity": "high", "type": "provenance", "chapter": 4},
    ]}
    revised, credits = asyncio.run(lz._narasi_consistency_revise(
        CHAPTER, critique, "storytelling", "id", model="test-model",
        tenant_id="t", user_id="u", job_uuid=None,
        authority_text="AUTHORITY", outline_packets={"3": "PACKET"},
    ))

    assert revised == changed
    assert credits == 5
    assert captured["input"] == changed
    assert captured["excluded"] == {3}
    assert [item["type"] for item in captured["violations"]] == ["provenance"]
    assert critique["structural_patch"] == {
        "schema_version": "structural_patch_summary_v1",
        "status": "accepted",
        "structural_violations": 1,
        "chapters_targeted": 1,
        "chapters_attempted": 1,
        "chapters_accepted": 1,
        "not_attempted_reason_counts": {},
        "deferred_nonstructural_total": 1,
        "deferred_nonstructural_by_chapter": [{"chapter_index": 2, "count": 1}],
    }


@pytest.mark.parametrize(
    ("targeted", "attempted", "accepted", "status"),
    [
        (0, 0, 0, "not_targeted"),
        (2, 0, 0, "targeted_not_attempted"),
        (2, 2, 0, "attempted_no_accept"),
        (2, 2, 1, "accepted"),
    ],
)
def test_structural_summary_four_state_contract(targeted, attempted, accepted, status):
    summary = lz._narasi_structural_patch_summary(
        structural_violations=targeted,
        targeted=targeted,
        attempted=attempted,
        accepted=accepted,
        not_attempted_reason_counts=(
            {"attempt_cap": targeted - attempted} if targeted > attempted else {}),
    )
    assert summary["status"] == status


def test_structural_summary_rejects_accepted_without_manuscript_change():
    with pytest.raises(AssertionError, match="accepted-without-change"):
        lz._narasi_structural_patch_summary(
            structural_violations=1, targeted=1, attempted=1, accepted=1,
            manuscript_changed=False,
        )


@pytest.mark.parametrize(
    ("old", "new", "witness_code"),
    [
        (
            'order.insert(anchor_index if anchor_key == "before_id" else anchor_index + 1, unit_id)',
            'order.insert(anchor_index + 1 if anchor_key == "before_id" else anchor_index, unit_id)',
            (
                "import narasi_addressed_patch as p\n"
                f"chapter={CHAPTER!r}\n"
                "seg=p.segment_chapter(chapter)\n"
                "raw={'schema_version':p.PATCH_SCHEMA_VERSION,'operations':"
                "[{'op':'move','unit_id':'u004','before_id':'u003'}]}\n"
                "out=p.apply_addressed_patch(seg,raw).text\n"
                "assert out.index('Deposisinya') < out.index('Hakim lalu')\n"
            ),
        ),
        (
            "chunks.append(text_by_id[unit.unit_id])",
            'chunks.append(text_by_id[unit.unit_id] + (" MUTATED_UNTOUCHED_UNIT." if index == 0 else ""))',
            (
                "import narasi_addressed_patch as p\n"
                f"chapter={CHAPTER!r}\n"
                "seg=p.segment_chapter(chapter)\n"
                "raw={'schema_version':p.PATCH_SCHEMA_VERSION,'operations':"
                "[{'op':'replace','unit_id':'u002','text':'Ia menyerahkan seluruh haknya sebelum rekaman diputar.'}]}\n"
                "out=p.segment_chapter(p.apply_addressed_patch(seg,raw).text)\n"
                "assert out.units[0].text == seg.units[0].text\n"
            ),
        ),
    ],
)
def test_addressed_patch_mutations_are_killed_without_pattern_miss(tmp_path, old, new, witness_code):
    root = Path(__file__).resolve().parents[2]
    source = (root / "python" / "narasi_addressed_patch.py").read_text(encoding="utf-8")
    assert source.count(old) == 1, "PATTERN-MISS: addressed-patch mutation no longer applies exactly once"
    (tmp_path / "narasi_addressed_patch.py").write_text(source.replace(old, new), encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = os.pathsep.join((str(tmp_path), str(root / "python")))
    run = subprocess.run(
        [sys.executable, "-c", witness_code],
        cwd=root, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=30, check=False,
    )
    assert (run.returncode != 0
            and "SyntaxError" not in run.stdout
            and "ModuleNotFoundError" not in run.stdout), \
        f"SURVIVED addressed-patch mutation:\n{run.stdout}"
