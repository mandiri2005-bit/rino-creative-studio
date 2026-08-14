import hashlib
import json
import asyncio
from types import SimpleNamespace
import os
from pathlib import Path
import subprocess
import sys

import pytest

import narasi_addressed_patch as patch_core
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


def test_move_after_id_is_applied_directly():
    segmented = segment_chapter(CHAPTER)
    result = apply_addressed_patch(
        segmented,
        _payload({"op": "move", "unit_id": "u001", "after_id": "u003"}),
    )
    assert result.text.index("Hakim lalu") < result.text.index("Mira membawa")


def test_multi_move_rejects_final_order_that_breaks_a_declared_relation():
    segmented = segment_chapter(CHAPTER)
    with pytest.raises(PatchValidationError) as exc:
        apply_addressed_patch(
            segmented,
            _payload(
                {"op": "move", "unit_id": "u002", "after_id": "u003"},
                {"op": "move", "unit_id": "u003", "after_id": "u004"},
            ),
        )
    assert exc.value.code == "move_unsatisfiable"


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("Bab lengkap, bukan JSON.", "response_not_json"),
        ("```json\n{}\n```", "response_not_json"),
        (_payload(), "operations_empty"),
        (json.dumps({"schema_version": PATCH_SCHEMA_VERSION, "operations": [], "extra": True}), "schema_shape"),
        (json.dumps({"schema_version": "wrong", "operations": []}), "schema_version"),
        (json.dumps({"schema_version": PATCH_SCHEMA_VERSION, "operations": {}}), "operations_type"),
        (_payload(*(
            {"op": "replace", "unit_id": "u001", "text": f"Valid {index}."}
            for index in range(5)
        )), "operations_cap"),
        (json.dumps({"schema_version": PATCH_SCHEMA_VERSION, "operations": [1]}), "operation_type"),
        (_payload({"op": "replace", "unit_id": "u001", "text": "Valid.", "extra": True}), "operation_shape"),
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
        (_payload({"op": "replace", "unit_id": "u001", "text": "   "}), "prose_empty"),
        (_payload({"op": "replace", "unit_id": "u001", "text": "Here is the revision."}), "prose_wrapper"),
        (_payload({"op": "replace", "unit_id": "u001", "text": "fragmen tanpa tanda akhir"}), "prose_unterminated"),
        (_payload({"op": "replace", "unit_id": "u001", "text": CHAPTER}), "prose_contains_heading"),
        (_payload(
            {"op": "replace", "unit_id": "u002", "text": "Pertama."},
            {"op": "replace", "unit_id": "u002", "text": "Kedua."},
        ), "operation_overlap"),
    ],
)
def test_rejection_matrix_is_atomic(raw, code):
    segmented = segment_chapter(CHAPTER)
    before = segmented.original
    with pytest.raises(PatchValidationError) as exc:
        apply_addressed_patch(segmented, raw)
    assert exc.value.code == code
    assert segmented.original == before


@pytest.mark.parametrize("replacement_first", [False, True])
def test_move_anchor_replace_overlap_is_order_independent(replacement_first):
    move = {"op": "move", "unit_id": "u004", "before_id": "u002"}
    replace = {"op": "replace", "unit_id": "u002", "text": "Anchor baru."}
    operations = (replace, move) if replacement_first else (move, replace)
    with pytest.raises(PatchValidationError) as exc:
        apply_addressed_patch(segment_chapter(CHAPTER), _payload(*operations))
    assert exc.value.code == "operation_overlap"


@pytest.mark.parametrize(
    ("assembled", "code"),
    [
        (CHAPTER.replace("## Bab 3: Atap", "## Bab 9: Rusak"), "heading_changed"),
        (CHAPTER.rstrip() + "\n\n## Bab 9: Rusak\n\nAkhir.", "heading_sequence"),
        (CHAPTER.rstrip() + " fragmen", "output_unterminated"),
    ],
)
def test_post_assembly_guards_are_reachable_and_atomic(monkeypatch, assembled, code):
    segmented = segment_chapter(CHAPTER)
    monkeypatch.setattr(patch_core, "_assemble", lambda *_args: assembled)
    with pytest.raises(PatchValidationError) as exc:
        patch_core.apply_addressed_patch(
            segmented,
            _payload({"op": "replace", "unit_id": "u002", "text": "Valid."}),
        )
    assert exc.value.code == code
    assert segmented.original == CHAPTER


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


def _install_structural_patch_response(monkeypatch, raw_response):
    class _Completions:
        def create(self, **_kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content=raw_response), finish_reason="stop")],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            )

    async def _usage(*_args, **_kwargs):
        return 1

    monkeypatch.setattr(
        lz, "make_narasi_client",
        lambda *_args, **_kwargs: SimpleNamespace(
            chat=SimpleNamespace(completions=_Completions())),
    )
    monkeypatch.setattr(lz, "_log_narasi_usage", _usage)
    monkeypatch.setattr(lz, "_narasi_revise_timeout", lambda *_args: 2.0)


def _run_structural_patch(monkeypatch, raw_response, *, finding=None):
    _install_structural_patch_response(monkeypatch, raw_response)
    finding = finding or {
        "severity": "high", "type": "outline_beat_order", "chapter": 3,
        "evidence": "Restore the order.", "fix": "Apply the smallest repair.",
    }
    return asyncio.run(lz._narasi_structural_patch_revise(
        CHAPTER, [finding], "storytelling", "id", "test-model",
        tenant_id="t", user_id="u", job_uuid=None,
        authority_text="AUTHORITY", outline_packets={"3": "EXACT PACKET"},
    ))


@pytest.mark.parametrize(
    ("raw_response", "reason"),
    [
        (_payload(*(
            {"op": "replace", "unit_id": f"u{index:03d}",
             "text": " ".join(f"panjang{index}_{word}" for word in range(40)) + "."}
            for index in range(1, 5)
        )), "word_band"),
        (_payload(*(
            {"op": "replace", "unit_id": f"u{index:03d}", "text": "Singkat."}
            for index in range(1, 5)
        )), "word_band"),
        (CHAPTER, "response_not_json"),
    ],
)
def test_structural_adapter_refuses_bad_shape_or_size_atomically(
        monkeypatch, caplog, raw_response, reason):
    revised, credits, stats = _run_structural_patch(monkeypatch, raw_response)
    assert revised == CHAPTER
    assert credits == 1
    assert stats["attempted"] == 1
    assert stats["accepted"] == 0
    assert stats["rejected_reason_counts"] == {reason: 1}
    assert f"reason={reason}" in caplog.text


def test_structural_adapter_enforces_legacy_fidelity_floor(monkeypatch):
    wholesale = _payload(*(
        {"op": "replace", "unit_id": f"u{index:03d}",
         "text": " ".join(f"asing{index}_{word}" for word in range(
             len(segment_chapter(CHAPTER).units[index - 1].text.split()))) + "."}
        for index in range(1, 5)
    ))
    revised, _credits, stats = _run_structural_patch(monkeypatch, wholesale)
    assert revised == CHAPTER
    assert stats["accepted"] == 0
    assert stats["rejected_reason_counts"] == {"fidelity_below_minimum": 1}


@pytest.mark.parametrize("declared", [None, "3", 99])
def test_structural_quote_locator_restores_patch_targeting(monkeypatch, declared):
    patch = _payload({
        "op": "replace", "unit_id": "u002",
        "text": "Ia menyerahkan seluruh haknya sebelum rekaman diputar.",
    })
    finding = {
        "severity": "high", "type": "outline_missing_beat",
        "evidence": 'The manuscript says "Ia menyerahkan haknya sebelum rekaman diputar."',
        "fix": "Restore the exact beat.",
    }
    if declared is not None:
        finding["chapter"] = declared
    revised, _credits, stats = _run_structural_patch(
        monkeypatch, patch, finding=finding)
    assert "seluruh haknya" in revised
    assert stats["targeted"] == stats["attempted"] == stats["accepted"] == 1
    assert stats["unresolved_locator_count"] == 0


def test_unresolvable_structural_locator_is_bounded_and_visible(monkeypatch, caplog):
    monkeypatch.setattr(
        lz, "make_narasi_client",
        lambda *_args, **_kwargs: pytest.fail(
            "unresolved structural finding must not call the provider"),
    )
    finding = {
        "severity": "high", "type": "outline_missing_beat",
        "evidence": "No manuscript quote or chapter locator.",
        "fix": "Restore it.",
    }
    revised, credits, stats = asyncio.run(lz._narasi_structural_patch_revise(
        CHAPTER, [finding], "storytelling", "id", "test-model",
        tenant_id="t", user_id="u", job_uuid=None,
        outline_packets={"3": "PACKET"},
    ))
    assert revised == CHAPTER
    assert credits == 0
    assert stats["targeted"] == stats["attempted"] == 0
    assert stats["unresolved_locator_count"] == 1
    assert "unresolved chapter locator type=outline_missing_beat" in caplog.text


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
        {"severity": "high", "type": "timeline", "chapter": 3,
         "evidence": '"Mira membawa hak penyerahan itu ke meja rapat."'},
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


def test_structural_lane_honors_minimum_severity_without_suppressing_high_legacy(
        monkeypatch):
    monkeypatch.setenv("NARASI_REVISE_MIN_SEVERITY", "high")
    captured = {}

    async def forbidden_patch(*_args, **_kwargs):
        pytest.fail("medium structural finding must not enter the patch lane")

    async def legacy(full_text, violations, *_args, **kwargs):
        captured["violations"] = violations
        captured["excluded"] = kwargs["excluded_chapter_numbers"]
        return full_text, 2

    monkeypatch.setattr(lz, "_narasi_structural_patch_revise", forbidden_patch)
    monkeypatch.setattr(lz, "_narasi_revise_chunked", legacy)
    critique = {"violations": [
        {"severity": "medium", "type": "story_clock_progression", "chapter": 3},
        {"severity": "high", "type": "canon_fork",
         "evidence": '"Mira membawa hak penyerahan itu ke meja rapat."'},
    ]}

    revised, credits = asyncio.run(lz._narasi_consistency_revise(
        CHAPTER, critique, "storytelling", "id", model="test-model",
        tenant_id="t", user_id="u", job_uuid=None,
        authority_text="AUTHORITY", outline_packets={"3": "PACKET"},
    ))
    assert revised == CHAPTER
    assert credits == 2
    assert [item["type"] for item in captured["violations"]] == ["canon_fork"]
    assert captured["excluded"] == set()
    assert critique["structural_patch"]["structural_violations"] == 1
    assert critique["structural_patch"]["chapters_targeted"] == 0


def test_nonstructural_declared_chapter_never_overrides_quote_and_deferral_is_counted(
        monkeypatch):
    book = (
        "## Bab 1: Alpha\n\nAlpha owned sentence.\n\n"
        "## Bab 2: Beta\n\nBeta repair sentence.\n"
    )
    captured = {}

    async def patch(*_args, **_kwargs):
        return book, 1, {
            "targeted": 1, "attempted": 1, "accepted": 0,
            "not_attempted_reason_counts": {}, "owned_chapter_numbers": {1},
        }

    async def legacy(full_text, violations, *_args, **_kwargs):
        captured["violations"] = violations
        return full_text, 2

    monkeypatch.setattr(lz, "_narasi_structural_patch_revise", patch)
    monkeypatch.setattr(lz, "_narasi_revise_chunked", legacy)
    critique = {"violations": [
        {"severity": "high", "type": "outline_beat_order", "chapter": 1},
        {"severity": "high", "type": "timeline", "chapter": 1,
         "evidence": '"Beta repair sentence."'},
        {"severity": "high", "type": "register",
         "evidence": '"Alpha owned sentence."'},
    ]}

    _revised, _credits = asyncio.run(lz._narasi_consistency_revise(
        book, critique, "storytelling", "id", model="test-model",
        tenant_id="t", user_id="u", job_uuid=None,
        authority_text="AUTHORITY", outline_packets={"1": "PACKET"},
    ))
    assert [item["type"] for item in captured["violations"]] == ["timeline"]
    assert critique["structural_patch"]["deferred_nonstructural_total"] == 1
    assert critique["structural_patch"]["deferred_nonstructural_by_chapter"] == [
        {"chapter_index": 0, "count": 1}
    ]


def test_mixed_lanes_share_one_max_chapter_attempt_budget(monkeypatch):
    monkeypatch.setenv("NARASI_REVISE_MAX_CHAPTERS", "4")
    captured = {}

    async def patch(*_args, **_kwargs):
        return CHAPTER, 2, {
            "targeted": 2, "attempted": 2, "accepted": 0,
            "not_attempted_reason_counts": {}, "owned_chapter_numbers": {1, 2},
        }

    async def legacy(full_text, _violations, *_args, **kwargs):
        captured["max"] = kwargs["max_chapters_override"]
        return full_text, 1

    monkeypatch.setattr(lz, "_narasi_structural_patch_revise", patch)
    monkeypatch.setattr(lz, "_narasi_revise_chunked", legacy)
    critique = {"violations": [
        {"severity": "high", "type": "outline_beat_order", "chapter": 1},
        {"severity": "high", "type": "timeline", "evidence": '"unmapped quote"'},
    ]}
    asyncio.run(lz._narasi_consistency_revise(
        CHAPTER, critique, "storytelling", "id", model="test-model",
        tenant_id="t", user_id="u", job_uuid=None,
        authority_text="AUTHORITY", outline_packets={},
    ))
    assert captured["max"] == 2


def test_real_legacy_lane_never_calls_provider_for_owned_quoted_chapter(monkeypatch):
    monkeypatch.setattr(
        lz, "make_narasi_client",
        lambda *_args, **_kwargs: pytest.fail(
            "owned chapter must be excluded before provider dispatch"),
    )
    revised, credits = asyncio.run(lz._narasi_revise_chunked(
        CHAPTER,
        [{"severity": "high", "type": "timeline",
          "evidence": '"Mira membawa hak penyerahan itu ke meja rapat."'}],
        "storytelling", "id", "test-model",
        tenant_id="t", user_id="u", job_uuid=None,
        authority_text="AUTHORITY", excluded_chapter_numbers={3},
    ))
    assert revised == CHAPTER
    assert credits == 0


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
                "[{'op':'move','unit_id':'u004','before_id':'u002'}]}\n"
                "try:\n"
                " out=p.apply_addressed_patch(seg,raw).text\n"
                "except p.PatchValidationError as exc:\n"
                " raise AssertionError('valid move was rejected') from exc\n"
                "assert out.index('Deposisinya') < out.index('Ia menyerahkan')\n"
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
            and "AssertionError" in run.stdout
            and "SyntaxError" not in run.stdout
            and "ModuleNotFoundError" not in run.stdout), \
        f"SURVIVED addressed-patch mutation:\n{run.stdout}"
