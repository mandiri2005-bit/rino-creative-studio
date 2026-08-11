"""Canon Lite L1 acceptance suite.

Covers the L1 exit contract (CANON-LITE-ARCHITECTURE-FINAL.md §13) and the L1 plan's
acceptance gate: strict-schema/adversarial tests, canon/hash determinism, real-job
flag-off equivalence, zero surviving child task after cancel, bounded parallelism, at
most one steady-state canon call (L1 spends zero), and no synthetic or paid job.

Every test here is offline. No provider is called, no socket is opened, no credit is
spent — the chapter worker is replaced by a local stub in the integration tests.
"""

import asyncio
import importlib
import json
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

import canon_lite as cl
from orchestrator import static as st
from orchestrator.context_builder import SharedContext


# ===========================================================================
# Helpers
# ===========================================================================

def _outline(n=3, titled=True):
    return [{"id": i + 1, "title": (f"Bab {i + 1}" if titled else ""),
             "summary": f"ringkasan {i + 1}"} for i in range(n)]


def _cfg(outline=None, **kw):
    outline = _outline() if outline is None else outline
    kw.setdefault("target_language", "id")
    kw.setdefault("narration_style", "kdrama_serial")
    return cl.build_job_config_snapshot(outline_chapters=outline, **kw)


def _canon(outline=None, **kw):
    outline = _outline() if outline is None else outline
    cfg = kw.pop("job_config", None) or _cfg(outline)
    return cl.build_canon_lite_v1(outline_chapters=outline, job_config=cfg, **kw)


def _ctx(n=3):
    return SharedContext(topic="topik", chapters=_outline(n), style="kdrama_serial")


# ===========================================================================
# A. Strict schema / adversarial
# ===========================================================================

def test_unknown_top_level_field_is_rejected():
    obj = _canon().to_canonical_obj()
    obj["injected"] = "x"
    with pytest.raises(cl.CanonSchemaError, match="unknown field"):
        cl.parse_canon_lite_v1(obj)


def test_non_string_unknown_field_is_rejected_as_schema_error():
    obj = _canon().to_canonical_obj()
    obj[1] = "x"
    with pytest.raises(cl.CanonSchemaError, match="unknown field"):
        cl.parse_canon_lite_v1(obj)


@pytest.mark.parametrize("section", list(cl._COMPONENT_SPECS))
def test_unknown_nested_field_is_rejected_in_every_section(section):
    """Not just the top level — every component has its own closed field set."""
    canon = cl.build_canon_lite_v1(
        outline_chapters=_outline(), job_config=_cfg(),
        entities=[cl.CanonEntityV1("e1", "Sari", ("Neng Sari",), "job_input")],
        anchors=[cl.CanonAnchorV1("a1", "time", "12 Maret 1998")],
        one_time_events=[cl.CanonEventV1("ev1", 2)],
        reveals=[cl.CanonRevealV1("r1", 3)],
        flashback_exceptions=[cl.CanonFlashbackExceptionV1("f1", 2, "declared_flashback")],
    )
    obj = canon.to_canonical_obj()
    assert obj[section], f"{section} fixture must be non-empty to be a real probe"
    obj[section][0]["injected"] = "x"
    with pytest.raises(cl.CanonSchemaError, match="unknown field"):
        cl.parse_canon_lite_v1(obj)


def test_missing_field_is_rejected():
    obj = _canon().to_canonical_obj()
    del obj["target_language"]
    with pytest.raises(cl.CanonSchemaError, match="missing field"):
        cl.parse_canon_lite_v1(obj)


def test_bool_is_not_an_int_for_order_fields():
    """True == 1 in Python. A bool reaching an order field is a bug, not chapter one."""
    with pytest.raises(cl.CanonSchemaError, match="expected int"):
        cl._req_order(True, "probe", count=3)


def test_chapter_order_must_be_exact_not_merely_sorted():
    rows = [cl.CanonChapterV1("ch1", 1, "A"), cl.CanonChapterV1("ch2", 3, "B")]
    with pytest.raises(cl.CanonSchemaError, match="expected exactly 2"):
        cl._validate_chapters(rows)


def test_swapped_chapter_order_is_rejected():
    rows = [cl.CanonChapterV1("ch1", 2, "A"), cl.CanonChapterV1("ch2", 1, "B")]
    with pytest.raises(cl.CanonSchemaError):
        cl._validate_chapters(rows)


def test_duplicate_chapter_id_is_rejected():
    rows = [cl.CanonChapterV1("ch1", 1, "A"), cl.CanonChapterV1("ch1", 2, "B")]
    with pytest.raises(cl.CanonSchemaError, match="duplicate"):
        cl._validate_chapters(rows)


def test_duplicate_entity_id_is_rejected():
    rows = [cl.CanonEntityV1("e1", "Sari", (), "none"),
            cl.CanonEntityV1("e1", "Budi", (), "none")]
    with pytest.raises(cl.CanonSchemaError, match="duplicate"):
        cl._validate_entities(rows)


def test_alias_without_an_authoritative_source_is_rejected():
    """§7.1 alias-authority rule, enforced structurally rather than by convention."""
    rows = [cl.CanonEntityV1("e1", "Sari", ("Neng",), "none")]
    with pytest.raises(cl.CanonSchemaError, match="alias_source='none'"):
        cl._validate_entities(rows)


def test_declared_alias_source_without_aliases_is_rejected():
    rows = [cl.CanonEntityV1("e1", "Sari", (), "job_input")]
    with pytest.raises(cl.CanonSchemaError, match="no aliases"):
        cl._validate_entities(rows)


@pytest.mark.parametrize("policy", ["", "fiction", "FICTION_GENERATED", "made_up", None, 1])
def test_fact_source_policy_is_a_closed_enum(policy):
    with pytest.raises(cl.CanonSchemaError):
        cl.build_canon_lite_v1(outline_chapters=_outline(), job_config=_cfg(),
                               fact_source_policy=policy)


def test_anchor_kind_and_flashback_reason_are_closed_enums():
    with pytest.raises(cl.CanonSchemaError):
        _canon(anchors=[cl.CanonAnchorV1("a1", "colour", "biru")])
    with pytest.raises(cl.CanonSchemaError):
        _canon(flashback_exceptions=[cl.CanonFlashbackExceptionV1("f1", 1, "because")])


def test_collection_bounds_are_enforced_not_truncated():
    big = _outline(cl.MAX_CHAPTERS + 1)
    with pytest.raises(cl.CanonBoundsError, match="exceeds"):
        cl.build_job_config_snapshot(outline_chapters=big, target_language="id",
                                     narration_style="x")


def test_alias_count_bound_is_enforced():
    aliases = tuple(f"nama{i}" for i in range(cl.MAX_ALIASES_PER_ENTITY + 1))
    with pytest.raises(cl.CanonBoundsError, match="exceeds"):
        cl._validate_entities([cl.CanonEntityV1("e1", "Sari", aliases, "job_input")])


def test_over_long_name_is_rejected_not_truncated():
    with pytest.raises(cl.CanonBoundsError):
        cl._validate_entities([cl.CanonEntityV1("e1", "x" * (cl.MAX_NAME_LEN + 1),
                                                (), "none")])


def test_over_long_outline_title_is_rejected_not_silently_truncated():
    outline = [{"id": 1, "title": "x" * (cl.MAX_TITLE_LEN + 1)}]
    with pytest.raises(cl.CanonBoundsError, match="exceeds"):
        cl.build_job_config_snapshot(
            outline_chapters=outline, target_language="id", narration_style="x")


@pytest.mark.parametrize("bad", [None, "chapter", 1, True])
def test_non_mapping_outline_chapter_is_rejected(bad):
    with pytest.raises(cl.CanonSchemaError, match="expected a mapping"):
        cl.build_job_config_snapshot(
            outline_chapters=[bad], target_language="id", narration_style="x")


@pytest.mark.parametrize("bad", [True, [], {}, object()])
def test_outline_id_rejects_unsupported_types(bad):
    with pytest.raises(cl.CanonSchemaError, match="expected int or str"):
        cl.build_job_config_snapshot(
            outline_chapters=[{"id": bad, "title": "A"}],
            target_language="id", narration_style="x")


def test_non_string_narration_style_is_a_schema_error():
    with pytest.raises(cl.CanonSchemaError, match="expected str"):
        cl.build_job_config_snapshot(
            outline_chapters=_outline(), target_language="id", narration_style=7)


def test_empty_string_must_be_stated_as_unknown():
    """An empty string is the silent-unknown §6 forbids."""
    with pytest.raises(cl.CanonSchemaError, match="use UNKNOWN"):
        cl._req_str("   ", "probe", max_len=50, allow_unknown=True)


def test_unknown_is_refused_where_a_real_value_is_required():
    with pytest.raises(cl.CanonSchemaError, match="not permitted"):
        cl._validate_entities([cl.CanonEntityV1("e1", cl.UNKNOWN, (), "none")])


def test_tampered_canon_hash_is_rejected_by_the_parser():
    obj = _canon().to_canonical_obj()
    obj["canon_sha256"] = "0" * 64
    with pytest.raises(cl.CanonSchemaError, match="does not bind"):
        cl.parse_canon_lite_v1(obj)


def test_content_tamper_without_hash_update_is_rejected():
    """The realistic forgery: edit a value, leave the advertised hash alone."""
    obj = _canon().to_canonical_obj()
    obj["chapters"][0]["expected_title"] = "Judul Palsu"
    with pytest.raises(cl.CanonSchemaError, match="does not bind"):
        cl.parse_canon_lite_v1(obj)


@pytest.mark.parametrize("bad", ["", "xyz", "A" * 64, "0" * 63, "0" * 65, 12345, None])
def test_non_canonical_sha256_is_rejected(bad):
    with pytest.raises(cl.CanonSchemaError):
        cl._req_sha256(bad, "probe")


def test_order_out_of_range_is_rejected_but_unknown_order_is_accepted():
    with pytest.raises(cl.CanonBoundsError):
        _canon(reveals=[cl.CanonRevealV1("r1", 99)])
    ok = _canon(reveals=[cl.CanonRevealV1("r1", cl.UNKNOWN_ORDER)])
    assert ok.reveals[0].planned_chapter_order == cl.UNKNOWN_ORDER


def test_canon_requires_an_accepted_outline():
    with pytest.raises(cl.CanonSchemaError, match="empty"):
        cl.build_canon_lite_v1(outline_chapters=[], job_config=_cfg())


def test_outline_drift_from_the_bound_snapshot_is_rejected():
    """The snapshot is the authority on what was accepted (§6.1)."""
    cfg = _cfg(_outline(3))
    with pytest.raises(cl.CanonSchemaError, match="does not match the bound job-config"):
        cl.build_canon_lite_v1(outline_chapters=_outline(4), job_config=cfg)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda rows: rows[1].__setitem__("title", "JUDUL DISELUNDUPKAN"),
            id="same-count-title-drift",
        ),
        pytest.param(
            lambda rows: (
                rows[0].__setitem__("id", 2),
                rows[1].__setitem__("id", 1),
            ),
            id="same-count-id-swap",
        ),
    ],
)
def test_outline_hash_guard_rejects_same_count_identity_drift(mutate):
    """Bind the hash guard itself: chapter_count cannot catch same-size drift."""
    accepted = _outline(3)
    cfg = _cfg(accepted)
    drifted = [dict(chapter) for chapter in accepted]
    mutate(drifted)
    assert len(drifted) == cfg.chapter_count
    with pytest.raises(
        cl.CanonSchemaError,
        match=r"^outline_sha256: outline does not match the bound job-config snapshot$",
    ):
        cl.build_canon_lite_v1(outline_chapters=drifted, job_config=cfg)


@pytest.mark.parametrize("declared", [4, 2], ids=["declared-too-high", "declared-too-low"])
def test_chapter_count_guard_rejects_a_self_bound_but_inconsistent_snapshot(declared):
    """Bind the SECOND drift layer: the hash guard cannot catch this one.

    A `JobConfigSnapshotV1` can be internally inconsistent while still verifying against
    itself — re-bind `config_sha256` over the forged fields and the snapshot is valid on
    its own terms, carrying an `outline_sha256` that genuinely matches the real outline.
    The hash guard therefore PASSES, and only the `chapter_count` cross-check stands
    between a forged snapshot and a canon built under the wrong declared size.

    A payload plus its own manifest can be re-bound together and still verify; this
    cross-field check is the independent anchor. It is not redundant with the hash guard
    and must not be simplified away — it is the only reachable defence for this class.
    """
    accepted = _outline(3)
    good = _cfg(accepted)

    payload = good.to_canonical_obj()
    payload["chapter_count"] = declared
    payload["config_sha256"] = cl._digest(
        "canon_lite.job_config.v1",
        {k: payload[k] for k in cl._JOB_CONFIG_HASHED_FIELDS})
    forged = cl.JobConfigSnapshotV1(**payload)

    # Preconditions — without them this test could pass for the wrong reason.
    assert forged.outline_sha256 == cl.outline_digest(accepted), \
        "the hash guard must PASS, or this does not isolate the count guard"
    assert forged.verify_sha256(), "the forged snapshot must be self-bound"
    assert forged.chapter_count != len(accepted)

    with pytest.raises(
        cl.CanonSchemaError,
        match=r"^chapter_count: outline does not match the bound job-config snapshot$",
    ):
        cl.build_canon_lite_v1(outline_chapters=accepted, job_config=forged)


def test_artifacts_are_immutable():
    canon = _canon()
    with pytest.raises(Exception):
        canon.canon_sha256 = "0" * 64
    with pytest.raises(Exception):
        canon.chapters[0].order = 9


def test_job_config_public_constructor_rejects_an_unbound_hash():
    obj = _cfg().to_canonical_obj()
    obj["config_sha256"] = "0" * 64
    with pytest.raises(cl.CanonSchemaError, match="does not bind"):
        cl.JobConfigSnapshotV1(**obj)


def test_job_config_public_constructor_validates_schema_before_hash():
    obj = _cfg().to_canonical_obj()
    obj["schema_version"] = "job_config_snapshot_v999"
    with pytest.raises(cl.CanonSchemaError, match="schema_version"):
        cl.JobConfigSnapshotV1(**obj)


def test_canon_builder_rejects_a_snapshot_mutated_after_binding():
    cfg = _cfg()
    object.__setattr__(cfg, "chapter_count", cfg.chapter_count + 1)
    with pytest.raises(cl.CanonSchemaError, match="mutated after binding"):
        cl.build_canon_lite_v1(outline_chapters=_outline(), job_config=cfg)


def test_canon_public_constructor_rejects_an_unbound_hash():
    canon = _canon()
    obj = {name: getattr(canon, name) for name in cl._CANON_FIELDS}
    obj["canon_sha256"] = "0" * 64
    with pytest.raises(cl.CanonSchemaError, match="does not bind"):
        cl.CanonLiteV1(**obj)


def test_parser_rejects_an_empty_chapter_set_even_with_a_recomputed_hash():
    obj = _canon().to_canonical_obj()
    obj["chapters"] = []
    without_hash = {k: v for k, v in obj.items() if k != "canon_sha256"}
    obj["canon_sha256"] = cl._digest("canon_lite.canon.v1", without_hash)
    with pytest.raises(cl.CanonSchemaError, match="chapters: empty"):
        cl.parse_canon_lite_v1(obj)


def test_advisory_bible_must_be_text_or_none():
    with pytest.raises(cl.CanonSchemaError, match="expected str or None"):
        _canon(advisory_bible_text={"prose": "not text"})


# ===========================================================================
# B. Determinism and hashing
# ===========================================================================

def test_canon_hash_is_deterministic_across_independent_builds():
    a = _canon(advisory_bible_text="Sari, 27 tahun.")
    b = _canon(advisory_bible_text="Sari, 27 tahun.")
    assert a.canon_sha256 == b.canon_sha256
    assert a.verify_sha256() and b.verify_sha256()


def test_canon_hash_is_stable_in_a_separate_interpreter():
    """Guards against a hash that depends on PYTHONHASHSEED or dict ordering."""
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "import canon_lite as cl\n"
        "o=[{'id':i+1,'title':'Bab %%d'%%(i+1),'summary':'s'} for i in range(3)]\n"
        "c=cl.build_canon_lite_v1(outline_chapters=o,"
        " job_config=cl.build_job_config_snapshot(outline_chapters=o,"
        " target_language='id', narration_style='kdrama_serial'))\n"
        "print(c.canon_sha256)\n"
    ) % str(Path(cl.__file__).parent)
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                         env={"PYTHONHASHSEED": "12345", "PATH": "/usr/bin:/bin"})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == _canon().canon_sha256


@pytest.mark.parametrize("mutate", [
    pytest.param(lambda o: o.__setitem__("target_language", "en"), id="language"),
    pytest.param(lambda o: o.__setitem__("fact_source_policy", "user_supplied"), id="policy"),
    pytest.param(lambda o: o["chapters"][0].__setitem__("expected_title", "Lain"), id="title"),
    pytest.param(lambda o: o["chapters"].pop(), id="chapter_count"),
    pytest.param(lambda o: o.__setitem__("advisory_bible_sha256", "a" * 64), id="bible"),
])
def test_every_bound_field_changes_the_hash(mutate):
    canon = _canon(advisory_bible_text="x")
    obj = canon.to_canonical_obj(include_hash=False)
    mutate(obj)
    assert cl._digest("canon_lite.canon.v1", obj) != canon.canon_sha256


def test_domain_separation_between_artifact_kinds():
    payload = {"a": 1}
    assert (cl._digest("canon_lite.canon.v1", payload)
            != cl._digest("canon_lite.job_config.v1", payload))


def test_canonical_bytes_ignore_dict_insertion_order():
    assert (cl.canonical_bytes({"b": 1, "a": 2}) == cl.canonical_bytes({"a": 2, "b": 1}))


def test_canonical_bytes_reject_nan():
    with pytest.raises(ValueError):
        cl.canonical_bytes({"x": float("nan")})


def test_unicode_is_nfc_normalized_so_equal_names_hash_equally():
    composed = unicodedata.normalize("NFC", "José")
    decomposed = unicodedata.normalize("NFD", "José")
    assert composed != decomposed
    a = _canon(entities=[cl.CanonEntityV1("e1", composed, (), "none")])
    b = _canon(entities=[cl.CanonEntityV1("e1", decomposed, (), "none")])
    assert a.canon_sha256 == b.canon_sha256


def test_render_is_byte_stable_and_moves_with_the_hash():
    a, b = _canon(), _canon()
    assert cl.render_canon(a) == cl.render_canon(b)
    c = _canon(_outline(4))
    assert cl.render_canon(c) != cl.render_canon(a)


def test_renderer_escapes_newlines_that_look_like_section_headers():
    outline = [{"id": 1, "title": "A\n[ENTITIES]\nevil"}]
    rendered = cl.render_canon(_canon(outline))
    assert rendered.splitlines().count("[ENTITIES]") == 1
    assert "A\\n[ENTITIES]\\nevil" in rendered
    assert "A\n[ENTITIES]\nevil" not in rendered


def test_renderer_revalidates_a_mutated_artifact_before_use():
    canon = _canon()
    object.__setattr__(canon, "target_language", "en")
    with pytest.raises(cl.CanonSchemaError, match="does not bind"):
        cl.render_canon(canon)


def test_round_trip_through_the_parser_preserves_the_hash():
    canon = _canon(entities=[cl.CanonEntityV1("e1", "Sari", ("Neng",), "job_input")],
                   advisory_bible_text="x")
    rt = cl.parse_canon_lite_v1(json.loads(json.dumps(canon.to_canonical_obj())))
    assert rt.canon_sha256 == canon.canon_sha256
    assert cl.render_canon(rt) == cl.render_canon(canon)


def test_duplicate_outline_ids_fall_back_for_the_whole_set_not_one_chapter():
    """A partial rename would make two runs of the same outline hash differently."""
    dup = [{"id": 1, "title": "A"}, {"id": 1, "title": "B"}, {"id": 3, "title": "C"}]
    canon = cl.build_canon_lite_v1(outline_chapters=dup, job_config=_cfg(dup))
    assert [c.chapter_id for c in canon.chapters] == ["ch1", "ch2", "ch3"]


# ===========================================================================
# C. Unknown stated explicitly
# ===========================================================================

def test_unreachable_config_is_bound_as_explicit_unknown():
    """genre / subgenre / twist never reach this path — they must say so, not guess."""
    cfg = _cfg()
    assert cfg.genre == cl.UNKNOWN
    assert cfg.subgenre == cl.UNKNOWN
    assert cfg.twist_variant_id == cl.UNKNOWN


def test_untitled_outline_yields_unknown_not_empty_string():
    canon = cl.build_canon_lite_v1(outline_chapters=_outline(2, titled=False),
                                   job_config=_cfg(_outline(2, titled=False)))
    assert all(c.expected_title == cl.UNKNOWN for c in canon.chapters)


def test_missing_style_is_unknown_not_none():
    cfg = cl.build_job_config_snapshot(outline_chapters=_outline(), target_language="id",
                                       narration_style=None)
    assert cfg.narration_style == cl.UNKNOWN


def test_absent_advisory_bible_is_unknown():
    assert _canon().advisory_bible_sha256 == cl.UNKNOWN
    assert _canon(advisory_bible_text="   ").advisory_bible_sha256 == cl.UNKNOWN
    assert cl._SHA256_RE.match(_canon(advisory_bible_text="x").advisory_bible_sha256)


def test_default_fact_source_policy_is_unknown_not_a_guess():
    assert _canon().fact_source_policy == "unknown"


# ===========================================================================
# D. Privacy (C12 / §10)
# ===========================================================================

def test_telemetry_digest_leaks_no_prose():
    secret_name, secret_title, secret_alias, secret_literal = (
        "Suranto Wijaya", "Rahasia Bab Tiga", "Si Kumis", "12 Maret 1998")
    outline = [{"id": 1, "title": secret_title}, {"id": 2, "title": "B"}]
    canon = cl.build_canon_lite_v1(
        outline_chapters=outline, job_config=_cfg(outline),
        entities=[cl.CanonEntityV1("e1", secret_name, (secret_alias,), "job_input")],
        anchors=[cl.CanonAnchorV1("a1", "time", secret_literal)],
        advisory_bible_text="prosa rahasia yang panjang")
    blob = json.dumps(cl.telemetry_digest(canon, canon_status="present"),
                      ensure_ascii=False)
    for leak in (secret_name, secret_title, secret_alias, secret_literal,
                 "prosa rahasia"):
        assert leak not in blob, f"telemetry leaked {leak!r}"


def test_telemetry_digest_values_are_only_hashes_counts_and_labels():
    d = cl.telemetry_digest(_canon(advisory_bible_text="x"), canon_status="present")
    allowed_labels = set(cl.FACT_SOURCE_POLICIES) | {
        cl.SCHEMA_VERSION, cl.UNKNOWN, "present", "absent", "invalid", "skipped"}
    for key, value in d.items():
        if isinstance(value, (int, bool)):
            continue
        assert isinstance(value, str)
        assert cl._SHA256_RE.match(value) or value in allowed_labels, (key, value)


def test_telemetry_digest_for_a_missing_canon_never_looks_clean():
    d = cl.telemetry_digest(None, canon_status="absent")
    assert d["canon_status"] == "absent"
    assert d["canon_sha256"] == cl.UNKNOWN
    assert d["chapter_count"] == 0


def test_telemetry_status_is_a_closed_enum():
    with pytest.raises(cl.CanonSchemaError):
        cl.telemetry_digest(None, canon_status="clean")


def test_telemetry_refuses_contradictory_status_and_artifact():
    with pytest.raises(cl.CanonSchemaError, match="present requires"):
        cl.telemetry_digest(None, canon_status="present")
    with pytest.raises(cl.CanonSchemaError, match="requires canon=None"):
        cl.telemetry_digest(_canon(), canon_status="invalid")


# ===========================================================================
# E. Mode gate and C11 (flag-off imports nothing)
# ===========================================================================

_MODE_MATRIX = [
    ("", "off"), ("off", "off"), ("OFF", "off"), ("  off  ", "off"),
    ("shadow", "shadow"), ("SHADOW", "shadow"), (" shadow ", "shadow"),
    ("assist", "assist"), ("enforce", "enforce"),
    ("shadwo", "off"), ("on", "off"), ("1", "off"), ("true", "off"),
    ("shadow;enforce", "off"), ("enforce ", "enforce"),
]


#: The allowlisted tenant every `assist` row below is resolved for. The matrix keeps
#: `assist -> assist` only because this tenant is named; see `_TENANT_MATRIX`.
_CANARY = "t-canary"


def _env(raw, allow=_CANARY):
    e = {cl.MODE_ENV_VAR: raw}
    if allow is not None:
        e[cl.ASSIST_TENANTS_ENV_VAR] = allow
    return e


@pytest.mark.parametrize("raw,expected", _MODE_MATRIX)
def test_resolve_mode_fails_safe(raw, expected):
    """`resolve_mode` is the GLOBAL configuration and takes no tenant."""
    assert cl.resolve_mode(_env(raw)) == expected
    assert cl.resolve_mode({cl.MODE_ENV_VAR: raw}) == expected


#: (mode, allowlist, tenant) -> effective mode. The rows that matter are the ones
#: where `assist` collapses to `off`: those are the difference between activating a
#: cohort and activating the fleet.
_TENANT_MATRIX = [
    ("assist", _CANARY, _CANARY, "assist"),          # the canary itself
    ("assist", _CANARY, "t-other", "off"),           # a different tenant
    ("assist", _CANARY, None, "off"),                # no tenant at all
    ("assist", _CANARY, "", "off"),
    ("assist", None, _CANARY, "off"),                # allowlist unset
    ("assist", "", _CANARY, "off"),                  # allowlist empty
    ("assist", " , ,, ", _CANARY, "off"),            # allowlist of separators
    ("assist", "t-a,t-canary,t-b", _CANARY, "assist"),   # multi-entry
    ("assist", " t-canary , t-b ", _CANARY, "assist"),   # padded entries
    ("assist", "T-CANARY", _CANARY, "off"),          # case-sensitive: ids are opaque
    ("assist", _CANARY, " t-canary ", "assist"),     # padded tenant
    # Every other mode is untouched by the allowlist.
    ("shadow", None, "t-other", "shadow"),
    ("shadow", _CANARY, "t-other", "shadow"),
    ("enforce", None, "t-other", "enforce"),
    ("off", _CANARY, _CANARY, "off"),
    ("", _CANARY, _CANARY, "off"),
]


def _inline_static(raw, allow, tenant):
    """static.py's copy, transcribed. C11 forbids it importing canon_lite."""
    mode = str(raw or "").strip().lower()
    if mode not in ("shadow", "assist", "enforce"):
        mode = "off"
    if mode == "assist":
        tid = str(tenant or "").strip()
        allowed = {x.strip() for x in str(allow or "").split(",") if x.strip()}
        if not tid or tid not in allowed:
            mode = "off"
    return mode


@pytest.mark.parametrize("raw,allow,tenant,expected", _TENANT_MATRIX)
def test_all_three_mode_gates_agree_including_the_tenant_allowlist(
        raw, allow, tenant, expected, monkeypatch):
    """🔴 THREE COPIES OF ONE GATE, BOUND OVER ONE MATRIX.

    C11 forbids the flag-off path from importing canon_lite, so `narrate_chapters`
    and `narration_api` each carry their own literal copy of this decision. Three
    copies is three chances to drift, and a drift that only shows up on `assist`
    would mean the dispatcher, the worker and the terminal seam disagree about
    whether a job is a canary — the worst possible place for them to disagree,
    because each would be individually self-consistent.
    """
    import narration_api as na
    env = {cl.MODE_ENV_VAR: raw}
    if allow is not None:
        env[cl.ASSIST_TENANTS_ENV_VAR] = allow

    canonical = cl.resolve_effective_mode(env, tenant_id=tenant)

    monkeypatch.setenv(cl.MODE_ENV_VAR, raw)
    if allow is None:
        monkeypatch.delenv(cl.ASSIST_TENANTS_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(cl.ASSIST_TENANTS_ENV_VAR, allow)
    api_side = na._cl_effective_mode(tenant)

    assert canonical == api_side == _inline_static(raw, allow, tenant) == expected


def test_an_empty_allowlist_makes_the_mode_flag_alone_a_no_op(monkeypatch):
    """🔴 THE SAFE DIRECTION FOR THE ONE MISTAKE SOMEONE WILL MAKE.

    Flipping `NARASI_CANON_LITE_MODE=assist` and forgetting the allowlist is the
    obvious operator error, and it is the one that would otherwise put the whole
    fleet — including jobs already queued — onto the repair path in one step. It
    resolves to `off` for everybody instead.
    """
    monkeypatch.setenv(cl.MODE_ENV_VAR, "assist")
    monkeypatch.delenv(cl.ASSIST_TENANTS_ENV_VAR, raising=False)
    for tid in (None, "", "t-canary", "t-anything", "00000000-0000-0000-0000-000000000000"):
        assert cl.resolve_effective_mode(tenant_id=tid) == "off", tid
    # ...while the GLOBAL configuration still reads `assist`. The two answers are
    # different questions, and collapsing them is what broke the metered wave.
    assert cl.resolve_mode() == "assist"
    assert cl.assist_tenants() == frozenset()


def test_resolve_mode_reads_the_process_environment(monkeypatch):
    monkeypatch.setenv(cl.MODE_ENV_VAR, "shadow")
    assert cl.resolve_mode() == "shadow"
    monkeypatch.delenv(cl.MODE_ENV_VAR, raising=False)
    assert cl.resolve_mode() == "off"


def test_flag_off_imports_no_canon_lite_module():
    """C11: flag-off performs no Canon Lite import. Proven in a clean interpreter,
    because this test process has already imported canon_lite at module scope."""
    root = str(Path(cl.__file__).parent)
    script = (
        "import sys, asyncio, os\n"
        "sys.path.insert(0, %r)\n"
        "os.environ.pop('NARASI_CANON_LITE_MODE', None)\n"
        "os.environ['NARASI_STORY_BIBLE'] = '0'\n"
        "from orchestrator import static as st\n"
        "from orchestrator.context_builder import SharedContext\n"
        "async def fake(**kw):\n"
        "    return {'ok': True, 'output': 'teks', 'no': kw['no'], 'model': 'm'}\n"
        "st._write_chapter = fake\n"
        "ch = [{'id': i+1, 'title': 'B%%d'%%i} for i in range(3)]\n"
        "ctx = SharedContext(topic='t', chapters=ch)\n"
        "asyncio.run(st.narrate_chapters('t', ch, shared_context=ctx, polish='none'))\n"
        "print('canon_lite' in sys.modules)\n"
    ) % root
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr[-3000:]
    assert out.stdout.strip().splitlines()[-1] == "False", out.stdout


def test_enforce_fails_closed_before_any_physical_work(monkeypatch, caplog):
    """A mode whose guarantees do not exist yet is refused, loudly, before any spend.

    🔴 THIS USED TO COVER `assist` TOO, AND NARROWING IT IS A SCOPE CHANGE, NOT A
       WEAKENING. L3-ASSIST implements `assist`, so refusing it would now be the
       bug; its wiring has its own controls in
       `test_canon_lite_l3_assist_stage1.py`. `enforce` is a SEPARATE project,
       deferred until L1/L2/L3 are live, so it keeps the refusal — including the
       part that matters most here: nothing physical runs first. Half-enabling a
       mode is worse than refusing it, because the job still costs money and the
       guarantees it implies are absent.
    """
    for mode in ("enforce",):
        monkeypatch.setenv("NARASI_CANON_LITE_MODE", mode)
        caplog.clear()
        stub = _Stub()

        def _route_must_not_run(*args, **kwargs):
            raise AssertionError("model routing ran before the L1 mode refusal")

        monkeypatch.setattr(st, "route_model", _route_must_not_run)
        with caplog.at_level("ERROR"):
            res = asyncio.run(_run_map(3, stub=stub))
        assert not res["ok"]
        assert res["error"] == "canon_lite_mode_unavailable_enforce"
        assert stub.started == 0
        assert any("refused before work" in r.getMessage() for r in caplog.records)
        assert any(mode in r.getMessage() for r in caplog.records)


# ===========================================================================
# F. C10 — child-task drain
# ===========================================================================

class _Stub:
    """A chapter worker stub that records exactly when each child starts and ends."""

    def __init__(self, hang=False):
        self.hang = hang
        self.started = 0
        self.finished = 0
        self.live = 0
        self.peak = 0
        self.cancelled = 0
        self.first_started = asyncio.Event()

    async def __call__(self, **kw):
        self.started += 1
        self.live += 1
        self.peak = max(self.peak, self.live)
        self.first_started.set()
        try:
            await asyncio.sleep(3600 if self.hang else 0)
            return {"ok": True, "output": f"teks {kw['no']}", "no": kw["no"], "model": "m"}
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.live -= 1
            self.finished += 1


async def _run_map(n=3, *, stub=None, max_parallel=4, ctx=None):
    st._write_chapter = stub or _Stub()
    chapters = _outline(n)
    return await st.narrate_chapters(
        "topik", chapters, shared_context=(ctx or SharedContext(topic="topik",
                                                                chapters=chapters)),
        polish="none", max_parallel=max_parallel)


@pytest.fixture(autouse=True)
def _restore_write_chapter(monkeypatch):
    original = st._write_chapter
    yield
    st._write_chapter = original


@pytest.fixture(autouse=True)
def _no_story_bible(monkeypatch):
    monkeypatch.setenv("NARASI_STORY_BIBLE", "0")
    monkeypatch.delenv("NARASI_CANON_LITE_MODE", raising=False)


def test_outer_cancellation_leaves_zero_surviving_child_tasks():
    """The C10 defect P0 measured: two children outlived an outer cancel."""
    async def scenario():
        stub = _Stub(hang=True)
        task = asyncio.ensure_future(_run_map(4, stub=stub))
        await stub.first_started.wait()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Give any orphan a chance to be observed before we assert its absence.
        await asyncio.sleep(0.05)
        return stub, [t for t in asyncio.all_tasks()
                      if "_bounded" in repr(t.get_coro()) and not t.done()]

    stub, orphans = asyncio.run(scenario())
    assert stub.started == stub.finished, (
        f"{stub.started - stub.finished} chapter task(s) survived the cancel")
    assert stub.cancelled == stub.started
    assert orphans == []


def test_without_the_drain_children_do_survive_a_cancel():
    """Control. The pre-fix loop shape, reproduced verbatim, still leaks — so the
    passing test above measures the drain and not merely a benign scheduler.
    """
    async def scenario():
        live = {"n": 0}
        started = asyncio.Event()

        async def child():
            live["n"] += 1
            started.set()
            try:
                await asyncio.sleep(3600)
            finally:
                live["n"] -= 1

        async def pre_fix_map():
            tasks = [asyncio.ensure_future(child()) for _ in range(4)]
            for fut in asyncio.as_completed(tasks):
                try:
                    await fut
                except asyncio.CancelledError:
                    raise            # <-- the defect: no cancel, no join
            return tasks

        outer = asyncio.ensure_future(pre_fix_map())
        await started.wait()
        await asyncio.sleep(0)
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer
        await asyncio.sleep(0.05)
        return live["n"]

    survivors = asyncio.run(scenario())
    assert survivors > 0, "control failed to reproduce the leak — the guard test is vacuous"


def test_drain_survives_being_cancelled_again_mid_join():
    """A second cancel delivered during the drain must not abandon the children."""
    async def scenario():
        finished = {"n": 0}

        async def child():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await asyncio.sleep(0)      # a child that unwinds across a suspension
                raise
            finally:
                finished["n"] += 1

        tasks = [asyncio.ensure_future(child()) for _ in range(3)]
        await asyncio.sleep(0)

        async def drain_then_report():
            return await st._drain_chapter_tasks(tasks, why="test")

        d = asyncio.ensure_future(drain_then_report())
        await asyncio.sleep(0)
        d.cancel()                            # cancel the drain itself
        try:
            await d
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0.05)
        return finished["n"], [t for t in tasks if not t.done()]

    finished, alive = asyncio.run(scenario())
    assert finished == 3
    assert alive == []


def test_drain_is_a_noop_when_every_task_is_done():
    async def scenario():
        async def done_child():
            return 1
        tasks = [asyncio.ensure_future(done_child()) for _ in range(3)]
        await asyncio.gather(*tasks)
        return await st._drain_chapter_tasks(tasks, why="test")

    assert asyncio.run(scenario()) == 0


def test_drain_never_returns_while_a_child_is_alive(monkeypatch, caplog):
    monkeypatch.setattr(st, "_MAP_DRAIN_TIMEOUT_S", 0.01)

    async def scenario():
        cancels = {"n": 0}

        async def stubborn():
            while True:
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    cancels["n"] += 1
                    if cancels["n"] == 1:
                        continue               # swallows exactly the first cancel
                    raise
        tasks = [asyncio.ensure_future(stubborn())]
        await asyncio.sleep(0)
        n = await st._drain_chapter_tasks(tasks, why="test")
        return n, cancels["n"], [t for t in tasks if not t.done()]

    with caplog.at_level("ERROR"):
        drained, cancels, alive = asyncio.run(scenario())
    assert drained == 1
    assert cancels >= 2
    assert alive == []
    assert any("continuing mandatory drain" in r.getMessage()
               for r in caplog.records)


def test_cancelling_during_the_story_bible_leaves_no_physical_work_running(monkeypatch):
    """I09: timeout/cancellation must wait for the physical work to actually finish.

    The pre-MAP slot is the other place a job can be cancelled. Structural review says
    the slot only ever `await`s (gather awaits its children; wait_for awaits the
    cancellation it delivers) — this binds that reading to the real call path instead
    of trusting it.
    """
    monkeypatch.setenv("NARASI_STORY_BIBLE", "1")
    monkeypatch.delenv("NARASI_CANON_LITE_MODE", raising=False)

    async def scenario():
        live = {"n": 0, "finished": 0}
        entered = asyncio.Event()
        from orchestrator import dynamic as dyn

        async def hanging_bible(*a, **kw):
            live["n"] += 1
            entered.set()
            try:
                await asyncio.sleep(3600)
                return "bible"
            finally:
                live["n"] -= 1
                live["finished"] += 1

        monkeypatch.setattr(dyn, "build_story_bible", hanging_bible)
        st._write_chapter = _Stub()
        chapters = _outline(3)
        task = asyncio.ensure_future(st.narrate_chapters(
            "topik", chapters, shared_context=SharedContext(topic="t", chapters=chapters),
            polish="none"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.05)
        return live

    live = asyncio.run(scenario())
    assert live["n"] == 0, "story-bible work outlived the cancellation"
    assert live["finished"] == 1


# ===========================================================================
# G. Parallelism and ordering are unchanged (C1)
# ===========================================================================

def test_parallelism_stays_bounded_by_max_parallel():
    stub = _Stub()
    asyncio.run(_run_map(12, stub=stub, max_parallel=3))
    assert stub.peak <= 3, f"concurrency reached {stub.peak}, bound was 3"
    assert stub.started == 12


def test_map_still_runs_in_parallel_not_serially():
    stub = _Stub()
    asyncio.run(_run_map(8, stub=stub, max_parallel=8))
    assert stub.peak > 1, "the MAP collapsed to serial execution"


def test_output_order_is_book_order_regardless_of_completion_order():
    class _Reversed(_Stub):
        async def __call__(self, **kw):
            await asyncio.sleep((10 - kw["no"]) / 1000)
            return {"ok": True, "output": f"teks {kw['no']}", "no": kw["no"], "model": "m"}

    res = asyncio.run(_run_map(6, stub=_Reversed(), max_parallel=6))
    assert [c["no"] for c in res["chapters"]] == list(range(6))


# ===========================================================================
# H. Shared-context freeze (I08 / C2)
# ===========================================================================

def test_soft_freeze_detects_attribute_rebinding():
    ctx = _ctx()
    fz = cl.SharedContextFreeze(ctx, hard=False)
    assert fz.verify() == (True, ())
    ctx.canonical_facts = "diubah setelah fan-out"
    ok, codes = fz.verify()
    assert not ok and "shared_context_mutated_after_freeze" in codes


def test_soft_freeze_detects_deep_in_place_mutation():
    """No __setattr__ guard can see this — only the deep digest can."""
    ctx = _ctx()
    fz = cl.SharedContextFreeze(ctx, hard=False)
    ctx.chapters[0]["title"] = "Judul Diselundupkan"
    ok, codes = fz.verify()
    assert not ok and "shared_context_mutated_after_freeze" in codes


def test_soft_freeze_detects_append_to_a_nested_list():
    ctx = _ctx()
    fz = cl.SharedContextFreeze(ctx, hard=False)
    ctx.passages.append({"text": "baru"})
    assert not fz.verify()[0]


def test_hard_freeze_actually_prevents_mutation():
    """Proves the prevention layer is load-bearing rather than dead code awaiting L3."""
    ctx = _ctx()
    fz = cl.SharedContextFreeze(ctx, hard=True)
    try:
        with pytest.raises(cl.CanonFrozenError):
            ctx.canonical_facts = "x"
        with pytest.raises(cl.CanonFrozenError):
            del ctx.style
        assert ctx.canonical_facts == ""
    finally:
        fz.release()


def test_hard_freeze_release_restores_the_original_class():
    ctx = _ctx()
    original = type(ctx)
    fz = cl.SharedContextFreeze(ctx, hard=True)
    assert type(ctx) is not original
    fz.release()
    assert type(ctx) is original
    ctx.canonical_facts = "ok sekarang"
    assert ctx.canonical_facts == "ok sekarang"


def test_hard_freeze_release_is_idempotent():
    ctx = _ctx()
    fz = cl.SharedContextFreeze(ctx, hard=True)
    fz.release()
    fz.release()
    assert type(ctx) is SharedContext


def test_freeze_context_manager_releases_on_exception():
    ctx = _ctx()
    original = type(ctx)
    with pytest.raises(RuntimeError):
        with cl.SharedContextFreeze(ctx, hard=True):
            raise RuntimeError("boom")
    assert type(ctx) is original


def test_freeze_reports_an_attribute_it_does_not_cover():
    """If SharedContext grows a field, the freeze says so instead of drifting."""
    ctx = _ctx()
    fz = cl.SharedContextFreeze(ctx, hard=False)
    assert fz.unknown_attrs() == ()
    object.__setattr__(fz, "_original_class", type("Grown", (), {}))
    fz2 = cl.SharedContextFreeze(_ctx(), hard=False)
    object.__setattr__(fz2, "_baseline", fz2.digest())
    # Simulate a new dataclass field by checking the real coverage set directly.
    covered = set(cl._FROZEN_CONTEXT_ATTRS)
    actual = {f.name for f in __import__("dataclasses").fields(SharedContext)}
    assert actual - covered == set(), (
        f"SharedContext fields outside the freeze: {sorted(actual - covered)}")


def test_freeze_digest_is_deterministic_for_equal_contexts():
    assert (cl.SharedContextFreeze(_ctx(), hard=False).baseline_digest
            == cl.SharedContextFreeze(_ctx(), hard=False).baseline_digest)


def test_freeze_projection_accepts_non_string_mapping_keys():
    ctx = _ctx()
    ctx.chapters[0] = {1: "numeric", "1": "text"}
    fz = cl.SharedContextFreeze(ctx, hard=False)
    assert fz.verify() == (True, ())


# ===========================================================================
# I. Real-job integration — shadow equivalence and the call budget
# ===========================================================================

def test_shadow_output_is_identical_to_flag_off(monkeypatch):
    """L1 exit criterion: real-job flag-off equivalence, and shadow changes nothing
    a user can see."""
    monkeypatch.delenv("NARASI_CANON_LITE_MODE", raising=False)
    off = asyncio.run(_run_map(5))
    monkeypatch.setenv("NARASI_CANON_LITE_MODE", "shadow")
    shadow = asyncio.run(_run_map(5))

    def comparable(r):
        d = dict(r)
        d.pop("context", None)          # carries timing/telemetry, not user output
        d.pop("_canon_lite_canon", None)        # L2a in-process transit only
        d.pop("_canon_lite_canon_status", None)
        return d

    assert comparable(off) == comparable(shadow)
    assert "_canon_lite_canon" not in off
    assert "_canon_lite_canon_status" not in off
    assert isinstance(shadow["_canon_lite_canon"], cl.CanonLiteV1)
    assert shadow["_canon_lite_canon_status"] == "present"
    assert off["book"] == shadow["book"]
    assert [c["content"] for c in off["chapters"]] == \
           [c["content"] for c in shadow["chapters"]]


def test_shadow_spends_zero_provider_calls(monkeypatch):
    """§11 caps Canon Lite at one steady-state construction call. L1 spends ZERO, so
    the ceiling cannot be breached and no second serial planner is stacked (I09)."""
    calls = []
    import laozhang_api

    for name in ("_narasi_cheap_call",):
        if hasattr(laozhang_api, name):
            async def _boom(*a, _n=name, **k):
                calls.append(_n)
                raise AssertionError(f"canon lite called a provider via {_n}")
            monkeypatch.setattr(laozhang_api, name, _boom)

    monkeypatch.setenv("NARASI_CANON_LITE_MODE", "shadow")
    stub = _Stub()
    res = asyncio.run(_run_map(4, stub=stub))
    assert res["ok"]
    assert calls == []
    assert stub.started == 4, "only the chapter workers may call out, once each"


def test_shadow_records_the_freeze_verdict(monkeypatch, caplog):
    monkeypatch.setenv("NARASI_CANON_LITE_MODE", "shadow")
    with caplog.at_level("INFO"):
        asyncio.run(_run_map(3))
    messages = [r.getMessage() for r in caplog.records]
    assert any("shared-context freeze intact across MAP" in m for m in messages)
    assert any("canon lite: {" in m and "canon_sha256" in m for m in messages)


def test_shadow_releases_the_context_freeze_on_map_cancellation(monkeypatch):
    monkeypatch.setenv("NARASI_CANON_LITE_MODE", "shadow")
    made = []
    original = cl.SharedContextFreeze

    class _TrackingFreeze(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.append(self)

    monkeypatch.setattr(cl, "SharedContextFreeze", _TrackingFreeze)

    async def scenario():
        stub = _Stub(hang=True)
        task = asyncio.ensure_future(_run_map(3, stub=stub))
        await stub.first_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return stub.live

    assert asyncio.run(scenario()) == 0
    assert len(made) == 1
    assert made[0]._released is True


def test_shadow_reports_a_context_mutated_during_the_map(monkeypatch, caplog):
    """The positive half of the freeze wiring: an intact-context log is not evidence
    that a violation would ever be reported."""
    monkeypatch.setenv("NARASI_CANON_LITE_MODE", "shadow")

    class _Mutating(_Stub):
        def __init__(self, ctx):
            super().__init__()
            self.ctx = ctx

        async def __call__(self, **kw):
            # A worker reaching back into the shared context — exactly what C2 forbids.
            self.ctx.chapters[0]["title"] = f"diubah oleh bab {kw['no']}"
            return await super().__call__(**kw)

    chapters = _outline(3)
    ctx = SharedContext(topic="topik", chapters=chapters)
    with caplog.at_level("WARNING"):
        asyncio.run(_run_map(3, stub=_Mutating(ctx), ctx=ctx))
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "shared-context freeze violation" in joined
    assert "shared_context_mutated_after_freeze" in joined
    assert "diubah oleh bab" not in joined, "the violation report leaked context content"


def test_shadow_never_reports_a_missing_canon_as_clean(monkeypatch, caplog):
    monkeypatch.setenv("NARASI_CANON_LITE_MODE", "shadow")

    def _explode(**kw):
        raise ValueError("outline rusak dengan prosa rahasia di dalamnya")
    monkeypatch.setattr(cl, "build_canon_lite_v1", _explode)

    with caplog.at_level("WARNING"):
        res = asyncio.run(_run_map(3))
    assert res["ok"], "a shadow canon failure must never break the job"
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "recorded as invalid, never as clean" in joined
    assert "prosa rahasia" not in joined, "the provider/validation message leaked"


def test_shadow_error_log_does_not_leak_a_dynamic_exception_type(monkeypatch, caplog):
    monkeypatch.setenv("NARASI_CANON_LITE_MODE", "shadow")
    SecretError = type("RahasiaPenggunaDalamNamaClass", (Exception,), {})

    def _explode(**kw):
        raise SecretError("pesan rahasia")

    monkeypatch.setattr(cl, "build_canon_lite_v1", _explode)
    with caplog.at_level("WARNING"):
        res = asyncio.run(_run_map(2))
    assert res["ok"]
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "RahasiaPenggunaDalamNamaClass" not in joined
    assert "pesan rahasia" not in joined
    assert "error_code=canon_construction_error" in joined


def test_shadow_survives_an_empty_context(monkeypatch):
    """The shadow block must read nothing that `narrate_chapters` does not already
    require. A context with no outline, no facts and no RAG exercises every fallback
    in it: `ctx.chapters or chapters`, absent bible, and the unknown fact policy.
    """
    monkeypatch.setenv("NARASI_CANON_LITE_MODE", "shadow")
    res = asyncio.run(_run_map(3, ctx=SharedContext(topic="")))
    assert res["ok"]
    assert len(res["chapters"]) == 3


def test_canon_lite_module_has_no_import_time_side_effects():
    """It must be safe for `narrate_chapters` to import this lazily inside a job."""
    mod = importlib.reload(cl)
    assert mod.resolve_mode({}) == "off"
