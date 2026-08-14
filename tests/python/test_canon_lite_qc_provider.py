"""DG-4 dark path — acceptance suite for the ratified provider/model amendment.

Ratified record: L2B-METER-DG4-PROVIDER-MODEL-AMENDMENT-001,
sha256 e5412fd9f0e32127f01568d3bd5665aac691cb6ea1635498a2f4c0fb6695ca08.
Rows §8.1 - §8.43c. Test names carry their row number so a failure names its clause.

D-METER-23: ZERO network egress. The adapter is constructed only with an injected fake
transport and a dummy credential; no socket, DNS lookup or provider route is reachable
from this file. An earlier draft claimed the adapter is never constructed in tests, which
contradicted 8.6-8.9 — those rows must construct it to assert max_retries=0, the request
count, the retry-mode schedule, and the absence of failover.
"""
import ast
import asyncio
import hashlib
import json
import socket
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))

import canon_lite as cl                    # noqa: E402
import canon_lite_extractor as ext         # noqa: E402
import canon_lite_l2 as l2                 # noqa: E402
import canon_lite_qc_meter as meter        # noqa: E402
import canon_lite_qc_contract as qcc       # noqa: E402
import canon_lite_qc_provider as qc        # noqa: E402

# v6 replaces quote/context transcription with a deterministic lexical atom table and
# address-only claims. Moving this pin is deliberate: prompt_sha256 is cache identity, so
# the first post-deploy extraction is repaid under the new wire rather than reusing v5
# evidence whose address table did not exist.
RATIFIED_PROMPT_SHA256 = \
    "c9e9bf792d4923e1d400dc8599f692bf069d8d0f37e8f9541e9b3ce1dc8ba5dc"
# Moved with the prompt pin above and for the same reason: the system template embeds the
# claim contract, so changing what a claim carries necessarily changes these bytes. The
# round-trip property this constant guards — template -> JSON -> template, byte-exact — is
# unchanged and still asserted below.
RATIFIED_TEMPLATE_SHA256 = \
    "1ac7791f19151c84381cf03c4a1bcfe8ea8923ede377c77f5b1d24b53b9521f2"

BOOK = "## Bab 1\nRatna pergi pagi\n## Bab 2\nRatna pulang malam"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _canon(*, entities=(), anchors=(), events=(), n_chapters=2):
    """Built through canon_lite._finalize, the ONLY constructor path — it is what binds
    canon_sha256 to the content. A hand-computed hash would be a second, divergent
    definition of the canon hash and verify_sha256() would reject it."""
    chapters = tuple(
        cl.CanonChapterV1(chapter_id=f"ch{i+1}", order=i + 1, expected_title=cl.UNKNOWN)
        for i in range(n_chapters))
    return cl._finalize({
        "schema_version": cl.SCHEMA_VERSION,
        "outline_sha256": "a" * 64, "generation_config_sha256": "b" * 64,
        "target_language": "id", "chapters": chapters,
        "entities": tuple(entities), "anchors": tuple(anchors),
        "one_time_events": tuple(events), "reveals": (), "flashback_exceptions": (),
        "fact_source_policy": "fiction_generated", "advisory_bible_sha256": cl.UNKNOWN,
    })


def _authoritative_canon():
    return _canon(entities=(cl.CanonEntityV1(
        entity_id="e1", canonical_name="Ratna", aliases=(), alias_source="none"),))


def _snapshot():
    return l2.materialize_final_snapshot({"book": BOOK})


def _atom_request_fields(snapshot, index):
    atoms, atom_sha = l2.build_chapter_atom_table(snapshot.block_bytes(index))
    return {"atom_table_sha256": atom_sha, "chapter_atoms": atoms}


def _request(*, attempt=1, canon=None, snapshot=None, index=0):
    snapshot = snapshot or _snapshot()
    canon = canon if canon is not None else _authoritative_canon()
    block = snapshot.blocks[index]
    chapter_bytes = snapshot.block_bytes(index)
    atoms, atom_sha = l2.build_chapter_atom_table(chapter_bytes)
    return ext.ExtractionRequestV1(
        chapter_index=index, chapter_id=block.chapter_id,
        content_sha256=block.content_sha256, canon_sha256=canon.canon_sha256,
        atom_table_sha256=atom_sha, attempt=attempt, chapter_bytes=chapter_bytes,
        chapter_atoms=atoms, canon=canon)


# ---- fake transport: records every call, opens no socket ------------------

class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeUsage:
    def __init__(self, tin, tout):
        self.prompt_tokens = tin
        self.completion_tokens = tout


class _FakeResponse:
    def __init__(self, *, content, model, usage=(11, 22)):
        self.model = model
        self.choices = [_FakeChoice(content)] if content is not None else []
        self.usage = _FakeUsage(*usage) if usage is not None else None


class _FakeCompletions:
    def __init__(self, owner):
        self._owner = owner

    async def create(self, **kwargs):
        self._owner.calls.append(kwargs)
        if self._owner.raises is not None:
            raise self._owner.raises
        return self._owner.responses.pop(0) if self._owner.responses \
            else _FakeResponse(content=self._owner.content, model=self._owner.model,
                               usage=self._owner.usage)


class FakeClient:
    """Stands in for AsyncOpenAI. Never touches a network."""

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.calls = []
        self.responses = []
        self.raises = None
        self.model = qc.QC_MODEL_UPSTREAM
        self.usage = (11, 22)
        self.content = json.dumps({
            "coverage": {p: "NO_CLAIMS_FOUND" for p in l2.SEMANTIC_PREDICATES},
            "claims": []})
        self.chat = type("Chat", (), {"completions": _FakeCompletions(self)})()


def _adapter(**kw):
    holder = {}

    def factory(**kwargs):
        client = FakeClient(**kwargs)
        holder["client"] = client
        return client

    adapter = qc.QcProviderAdapter(api_key="dummy-not-a-real-credential",
                                   client_factory=factory, **kw)
    return adapter, holder["client"]


def _run(coro):
    return asyncio.run(coro)


# ===========================================================================
# §8.1 - §8.5  QcProviderResult and usage
# ===========================================================================

def test_8_1_mapping_key_isolation():
    usage = meter.ProviderUsage(tokens_in=1, tokens_out=2,
                                provider_reported_cost_usd=None)
    r = qc.QcProviderResult(coverage={"a": 1}, claims=[], usage=usage)
    assert tuple(r) == ("coverage", "claims")
    assert len(r) == 2
    with pytest.raises(KeyError):
        r["usage"]
    # _provider_payload must accept it: its allow-list is exactly these two keys.
    snap = _snapshot()
    payload = ext._provider_payload(
        r, snapshot=snap, index=0, model_version=qc.QC_MODEL_UPSTREAM,
        prompt_sha256=qc.PROMPT_SHA256, canon_sha256="c" * 64)
    assert payload["coverage"] == {"a": 1}


def test_8_2_usage_extraction_returns_the_exact_object():
    usage = meter.ProviderUsage(tokens_in=7, tokens_out=9,
                                provider_reported_cost_usd=None)
    r = qc.QcProviderResult(coverage={}, claims=[], usage=usage)
    assert qc.usage_reader(r) is usage


def test_8_3_usage_reader_type_isolation_rejects_duck_typing():
    usage = meter.ProviderUsage(tokens_in=1, tokens_out=1,
                                provider_reported_cost_usd=None)

    class DuckTyped:
        pass
    duck = DuckTyped()
    duck.usage = usage                     # looks right, is not the ratified type
    for bad in ({"coverage": {}, "claims": []}, duck, None, object()):
        with pytest.raises(qc.QcProviderError):
            qc.usage_reader(bad)


def test_8_4_missing_usage_raises_and_never_zero_fills():
    for usage in (None, ("x", 2), (1, None)):
        adapter, client = _adapter()
        client.usage = usage
        with pytest.raises(qc.QcProviderError) as e:
            _run(adapter(_request()))
        assert str(e.value) == "qc_provider_usage_missing"


def test_8_5_model_mismatch_raises_and_never_relabels():
    adapter, client = _adapter()
    client.model = "gemini-3.0-pro"
    with pytest.raises(qc.QcProviderError) as e:
        _run(adapter(_request()))
    assert str(e.value) == "qc_provider_model_mismatch"


# ===========================================================================
# §8.6 - §8.13  One request per row, schedule, no failover, timeouts
# ===========================================================================

def test_8_6_max_retries_is_zero_on_the_constructed_client():
    _, client = _adapter()
    assert client.init_kwargs["max_retries"] == 0


def test_8_7_exactly_one_http_call_per_invocation_across_outcomes():
    # success
    adapter, client = _adapter()
    _run(adapter(_request()))
    assert len(client.calls) == 1

    # empty 200 — a failure that RAISES; retrying here would hide a second request
    adapter, client = _adapter()
    client.content = None
    with pytest.raises(qc.QcProviderError):
        _run(adapter(_request()))
    assert len(client.calls) == 1

    # unparseable
    adapter, client = _adapter()
    client.content = "not json at all"
    with pytest.raises(qc.QcProviderError):
        _run(adapter(_request()))
    assert len(client.calls) == 1

    # timeout
    adapter, client = _adapter()
    client.raises = TimeoutError()
    with pytest.raises(qc.QcProviderError) as e:
        _run(adapter(_request()))
    assert str(e.value) == "qc_provider_timeout"
    assert len(client.calls) == 1


def test_8_8_every_attempt_sends_the_same_closed_json_schema():
    sent = []
    for attempt in (1, 2, 3):
        adapter, client = _adapter()
        _run(adapter(_request(attempt=attempt)))
        rf = client.calls[0]["response_format"]
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["schema"] == qc.QC_RESPONSE_SCHEMA
        assert rf["json_schema"]["strict"] is True
        sent.append(rf)
    assert sent[1] == sent[0] == sent[2]


def test_8_8b_claim_wire_is_address_only():
    item = qc.QC_RESPONSE_SCHEMA["properties"]["claims"]["items"]
    assert item["additionalProperties"] is False
    assert item["required"] == ["claim_type", "canon_ref", "atom_start", "atom_end"]
    assert set(item["properties"]) == set(item["required"])
    assert "quote" not in json.dumps(item)
    assert "context" not in json.dumps(item)


def test_8_8a_response_format_out_of_range_refuses_without_index_error():
    for bad in (0, 4, -1, True, "1", None):
        with pytest.raises(qc.QcProviderError) as exc:
            qc.response_format_for(bad)
        assert str(exc.value) == "qc_provider_schema_violation"


def test_8_9_no_failover_client_is_ever_constructed():
    source = Path(qc.__file__).read_text(encoding="utf-8")
    assert "make_narasi_client" not in source
    assert "_NarasiFailoverClient" not in source
    adapter, client = _adapter()
    client.raises = RuntimeError("route down")
    with pytest.raises(qc.QcProviderError) as e:
        _run(adapter(_request()))
    assert str(e.value) == "qc_provider_http_error"
    assert len(client.calls) == 1          # a code, never a second route


def test_8_10_zero_network_egress(monkeypatch):
    def _forbidden(*a, **k):               # noqa: ANN001
        raise AssertionError("the QC suite opened a socket")
    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    adapter, _ = _adapter()
    _run(adapter(_request()))


def test_8_11_no_provider_text_body_or_headers_leaks():
    secret = "PROVIDER-INTERNAL-TRACE-abc123"

    class Boom(Exception):
        def __init__(self):
            super().__init__(secret)
            self.headers = {"x-request-id": secret}
            self.body = secret
    adapter, client = _adapter()
    client.raises = Boom()
    with pytest.raises(qc.QcProviderError) as e:
        _run(adapter(_request()))
    assert secret not in str(e.value)
    assert str(e.value) in qc.QC_PROVIDER_CODES


def test_8_12_timeout_ordering_asserted_on_constants():
    assert qc.QC_HTTP_TIMEOUT_S < float(qc.QC_ATTEMPT_TIMEOUT_S)
    assert float(qc.QC_ATTEMPT_TIMEOUT_S) == ext.DEFAULT_TIMEOUT_S


def test_8_12b_thinking_is_pinned_off_and_reaches_the_wire():
    """🔴 THE MODEL RAISE SILENTLY TURNED THINKING ON. Gemini 2.5 Flash thinks by
       default; Flash-Lite — the model these pins were tuned for — is the one 2.5 model
       that does not. Canary `2peg3q6i` paid for it three times on the longest chapter
       (`qc_provider_timeout` x3), with thought tokens billed as OUTPUT at $2.50/M for
       nothing: extraction is read-and-point, not reasoning. The vertex path has had
       this guard for months; this is the OpenAI-compat path finally getting it.

       Falsified before being trusted: with the kwarg line removed from the adapter,
       the wire assertion below fails; with the contract entry removed, the digest
       assertion fails.
    """
    assert qc.QC_REASONING_EFFORT == "none"
    adapter, client = _adapter()
    _run(adapter(_request()))
    # the WIRE, not the constant: what the provider call actually carries
    assert client.calls[0]["reasoning_effort"] == "none"
    # and the contract DECLARES what the wire carries, so the pin is hash-bound:
    # a diff dropping it moves PROMPT_SHA256 and trips the ratified pin above
    assert qc._QC_REQUEST_CONTRACT_OBJ["reasoning_effort"] is qc.QC_REASONING_EFFORT


def test_8_13_each_attempt_gets_a_fresh_budget():
    """A slow attempt 1 must not shorten attempt 2 — proves timeout_s is per attempt,
    not one shared wave deadline."""
    snap = _snapshot()
    canon = _authoritative_canon()
    seen = []

    async def provider(request):
        seen.append(request.attempt)
        if request.attempt == 1:
            await asyncio.sleep(0.05)      # eats most of attempt 1's budget
            raise asyncio.TimeoutError()
        return {"coverage": {p: "NO_CLAIMS_FOUND" for p in l2.SEMANTIC_PREDICATES},
                "claims": []}

    run = _run(ext.extract_all(
        snap, canon, provider=provider, model_version=qc.QC_MODEL_UPSTREAM,
        prompt_sha256=qc.PROMPT_SHA256, max_concurrency=1, timeout_s=0.08))
    assert 2 in seen                       # attempt 2 ran with its own budget
    assert run.claims[0].measured


# ===========================================================================
# §8.14 - §8.17b  Contract hashing, route, model and pricing parity
# ===========================================================================

def test_8_14_exact_contract_hashing():
    assert qc.PROMPT_SHA256 == hashlib.sha256(
        qc.QC_REQUEST_CONTRACT_BYTES).hexdigest()
    assert qc.PROMPT_SHA256 == RATIFIED_PROMPT_SHA256


def test_8_15_one_byte_contract_mutation_changes_the_digest():
    original = qc.QC_REQUEST_CONTRACT_BYTES
    mutated = bytearray(original)
    mutated[10] ^= 0x01                    # flip one bit in a LOCAL copy
    assert hashlib.sha256(bytes(mutated)).hexdigest() != qc.PROMPT_SHA256
    assert qc.QC_REQUEST_CONTRACT_BYTES == original   # module constant untouched


def test_8_16_route_immutability(monkeypatch):
    assert qc.resolve_base_url({}) == qc.QC_PROVIDER_BASE_URL
    assert qc.resolve_base_url(
        {qc.QC_BASE_URL_ENV: qc.QC_PROVIDER_BASE_URL}) == qc.QC_PROVIDER_BASE_URL
    with pytest.raises(qc.QcProviderError) as e:
        qc.resolve_base_url({qc.QC_BASE_URL_ENV: "https://evil.example/v1/"})
    assert str(e.value) == "qc_provider_route_mismatch"
    _, client = _adapter()
    assert client.init_kwargs["base_url"] == qc.QC_PROVIDER_BASE_URL


# ── Where the provider IDENTITY lives — RATIFIED AMENDMENT 2026-08-12 ───────────────
#
# §7.2's invariant is ONE definition of the model and the route. It is NOT "one definition
# inside canon_lite_qc_provider.py" — that was the implementation location, and these two
# rows pinned it by accident of how they were written.
#
# The activation gate has to read the ratified route and the credential's NAME without
# importing the provider, because refusing to build the provider is the entire point of that
# gate. So the canonical owner is now the inert contract module, and the provider re-exports
# from it. The alternative — the contract importing the provider — would have put the
# provider import back inside the gate, i.e. defeated the thing being guarded.
#
# 🔴 COUNTING OVER THE UNION IS WHY THIS IS A STRENGTHENING, NOT A WEAKENING. A count inside
#    one file passes the moment a second literal moves to a neighbouring module; the union
#    covers every module that could plausibly hold a copy, so a duplicate still fails — and
#    duplication the single-file check could never see now fails too. The canonical owner is
#    asserted explicitly, so "exactly one" cannot be satisfied by the wrong file, and the
#    narration seam is asserted at ZERO separately.
QC_IDENTITY_MODULES = (
    "canon_lite_qc_contract.py",       # canonical owner
    "canon_lite_qc_provider.py",       # re-exports the owner's objects
    "canon_lite_qc_meter.py",          # reads the names through the contract
    "canon_lite_qc_runner.py",         # reads the names through the contract
)
# The QUOTED form matters: `"gemini-2.5-flash"` cannot match inside
# `"gemini-2.5-flash-lite"` (the closing quote differs), so prose mentioning the old
# model in a comment cannot be miscounted as a second definition.
QC_MODEL_LITERAL = '"gemini-2.5-flash"'
QC_ROUTE_LITERAL = '"https://generativelanguage.googleapis.com/v1beta/openai/"'
QC_SEAM_MODULE = "narration_api.py"

_PYTHON_DIR = Path(qc.__file__).resolve().parent


def _module_source(name):
    return (_PYTHON_DIR / name).read_text(encoding="utf-8")


def _literal_counts(literal):
    return {name: _module_source(name).count(literal) for name in QC_IDENTITY_MODULES}


def test_8_17_model_parity_one_constant_four_uses():
    adapter, client = _adapter()
    _run(adapter(_request()))
    fields = qc.attempt_context_fields()
    assert client.calls[0]["model"] == qc.QC_MODEL_UPSTREAM       # outgoing request
    assert fields["model_upstream"] == qc.QC_MODEL_UPSTREAM       # AttemptContext
    client.model = qc.QC_MODEL_UPSTREAM                           # checked response

    counts = _literal_counts(QC_MODEL_LITERAL)
    assert sum(counts.values()) == 1, counts                      # no second literal
    assert counts["canon_lite_qc_contract.py"] == 1, counts       # ...and it lives here

    # The provider's constant is the contract's OBJECT, not a copy that happens to match.
    # `is` is the assertion that matters: `==` would still hold if someone re-typed the
    # string, which is exactly the drift the single-definition rule exists to prevent.
    assert qc.QC_MODEL_UPSTREAM is qcc.QC_MODEL_UPSTREAM
    # RAISED 2026-08-13, flash-lite -> flash, as a CONTROLLED EXPERIMENT — see
    # QC_MODEL_UPSTREAM's own note, which records what the canaries did and did NOT prove.
    # A diff moving this line moves QC_PRICING with it, or 8_17b fails.
    assert qc.QC_MODEL_UPSTREAM == "gemini-2.5-flash"


def test_8_17c_the_l3_seam_has_no_qc_model_identity():
    """🔴 THE INVARIANT IS SEMANTIC, AND A SUBSTRING COUNT ONLY EVER APPROXIMATED IT.

       The rule is: the seam that reports L3 outcomes does not hold, read, or publish the
       QC model's identity. That was checked by counting the model literal in
       `narration_api.py` and requiring zero — which worked only while the QC model was a
       string nothing else used. The moment QC moved to `gemini-2.5-flash`, the seam's own
       unrelated narration default became an indistinguishable "hit", and the test failed
       for a reason that had nothing to do with the invariant.

       Letting that failure pick the production model would be backwards: the test would be
       choosing the model to keep its own implementation working. So the check now asks the
       real question, structurally — no reference to the constant, no import of the modules
       that own it, and no model-shaped field in what the reporter emits.
    """
    tree = ast.parse(_module_source(QC_SEAM_MODULE))
    reporters = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name.startswith("_l3_")]
    assert reporters, "no L3 reporter functions found — the probe lost its subject"

    # 1. no reporter reads the model constant, by either spelling
    for fn in reporters:
        for node in ast.walk(fn):
            if isinstance(node, ast.Name):
                assert node.id != "QC_MODEL_UPSTREAM", fn.name
            if isinstance(node, ast.Attribute):
                assert node.attr != "QC_MODEL_UPSTREAM", fn.name

    # 2. no reporter reaches the QC provider or its contract — importing the provider on
    #    this host is the very thing the activation gate exists to prevent (see 8.42)
    qc_modules = {"canon_lite_qc_provider", "canon_lite_qc_contract"}
    for fn in reporters:
        for node in ast.walk(fn):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name not in qc_modules, f"{fn.name} imports {alias.name}"
            if isinstance(node, ast.ImportFrom):
                assert node.module not in qc_modules, f"{fn.name} imports {node.module}"
            # ...and by the dynamic spellings, which are not Import nodes at all. This
            # gap was found by falsifying this very test: the first version passed against
            # a reporter doing `__import__("canon_lite_qc_provider")`, because it only
            # looked for import STATEMENTS.
            if isinstance(node, ast.Call):
                target = node.func
                dynamic = (isinstance(target, ast.Name) and target.id == "__import__") or (
                    isinstance(target, ast.Attribute) and target.attr == "import_module")
                if dynamic:
                    for arg in node.args:
                        if isinstance(arg, ast.Constant) and type(arg.value) is str:
                            assert arg.value not in qc_modules, (
                                f"{fn.name} dynamically imports {arg.value}")

    # 3. nothing model-shaped in the record the reporter publishes. Read from the dict
    #    literal it builds, so this holds without importing the seam module at all.
    record = next(fn for fn in reporters if fn.name == "_l3_record_outcome")
    keys = [k.value for node in ast.walk(record) if isinstance(node, ast.Dict)
            for k in node.keys if isinstance(k, ast.Constant) and type(k.value) is str]
    assert keys, "the outcome record has no literal keys — the probe found nothing"
    assert not [k for k in keys if "model" in k], keys


def test_8_17a_route_and_provider_parity_no_second_literal():
    counts = _literal_counts(QC_ROUTE_LITERAL)
    assert sum(counts.values()) == 1, counts
    assert counts["canon_lite_qc_contract.py"] == 1, counts

    assert qc.QC_PROVIDER_BASE_URL is qcc.QC_PROVIDER_BASE_URL
    assert qc.QC_PROVIDER_BASE_URL == \
        "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert _module_source(QC_SEAM_MODULE).count(QC_ROUTE_LITERAL) == 0

    # The provider NAME is not contract material: it never leaves this module and the
    # activation gate has no use for it, so it stays exactly where it was — once, here.
    assert _module_source("canon_lite_qc_provider.py").count('"gemini_direct"') == 1
    assert qc.attempt_context_fields()["provider"] == qc.QC_PROVIDER_NAME


def test_8_17b_pricing_parity_one_immutable_record():
    fields = qc.attempt_context_fields()
    assert fields["pricing_version"] == qc.QC_PRICING.pricing_version
    # Gemini 2.5 Flash Standard paid tier, read from the record's own E5 source on
    # 2026-08-13 ($0.30 in / $2.50 out per million). Flash-Lite was $0.10/$0.40 and the
    # old record was correct for the model it described — it moved because the MODEL did.
    assert fields["rate_in_usd_per_m"] == Decimal("0.30")
    assert fields["rate_out_usd_per_m"] == Decimal("2.50")
    assert qc.QC_PRICING.pricing_version == "qc-rates-2026-08-13"
    assert "Flash Standard" in qc.QC_PRICING.source
    assert "Flash-Lite" not in qc.QC_PRICING.source
    assert isinstance(fields["rate_in_usd_per_m"], Decimal)
    with pytest.raises(Exception):         # frozen: a rate cannot move alone
        qc.QC_PRICING.rate_in_usd_per_m = Decimal("0.20")


# ===========================================================================
# §8.18 - §8.21  Failure discipline and placeholders
# ===========================================================================

def test_8_18_failure_always_raises_never_returns():
    for setup in (
        lambda c: setattr(c, "content", None),
        lambda c: setattr(c, "content", "{{{"),
        lambda c: setattr(c, "model", "other"),
        lambda c: setattr(c, "usage", None),
        lambda c: setattr(c, "raises", RuntimeError("x")),
        lambda c: setattr(c, "content", json.dumps({"coverage": {}})),
    ):
        adapter, client = _adapter()
        setup(client)
        with pytest.raises(qc.QcProviderError) as e:
            _run(adapter(_request()))
        assert str(e.value) in qc.QC_PROVIDER_CODES


def test_8_19_no_syntactic_placeholders():
    for name in dir(qc):
        if name.startswith("QC_"):
            value = getattr(qc, name)
            if isinstance(value, str):
                assert "____" not in value and value != "…", name


def test_8_20_chapter_atom_round_trip():
    req = _request()
    content = qc.build_user_content(req)
    parsed = json.loads(content)
    assert parsed["atom_table_version"] == l2.ATOM_TABLE_VERSION
    assert parsed["atom_table_sha256"] == req.atom_table_sha256
    assert b"".join(text.encode("utf-8") for _index, text in parsed["chapter_atoms"]) \
        == req.chapter_bytes
    assert [index for index, _text in parsed["chapter_atoms"]] \
        == list(range(len(req.chapter_atoms)))


def test_8_20b_request_refuses_an_atom_table_from_different_bytes():
    req = _request()
    wrong_atoms, _wrong_digest = l2.build_chapter_atom_table(
        req.chapter_bytes + b" altered")
    with pytest.raises(cl.CanonSchemaError, match="do not bind chapter_bytes"):
        replace(req, chapter_atoms=wrong_atoms)


def test_8_21_no_semantic_placeholder():
    from datetime import date
    version = qc.QC_PRICING.pricing_version
    assert version != "qc-rates-YYYY-MM-DD"
    date.fromisoformat(version[len("qc-rates-"):])         # a real calendar date
    for attestation in (qc.QC_CAP_SOURCE, qc.QC_CAP_REVISION,
                        qc.QC_PRICING.source, qc.QC_PRICING.revision):
        assert attestation.strip() and "YYYY" not in attestation


# ===========================================================================
# §8.22 - §8.28  Canon binding, cache identity, schema v2
# ===========================================================================

def test_8_22_chapter_claims_carries_canon_and_atom_table_sha256():
    assert "canon_sha256" in l2._CLAIMS_PAYLOAD_FIELDS
    assert "canon_sha256" in {f for f in l2.ChapterClaimsV1.__dataclass_fields__}
    assert "canon_sha256" in l2.CLAIM_CACHE_KEY_FIELDS
    assert "atom_table_sha256" in l2._CLAIMS_PAYLOAD_FIELDS
    assert "atom_table_sha256" in l2.ChapterClaimsV1.__dataclass_fields__
    assert "atom_table_sha256" in l2.CLAIM_CACHE_KEY_FIELDS
    canon = _authoritative_canon()
    snap = _snapshot()
    art = _run(ext.extract_all(
        snap, canon, provider=_ok_provider(), model_version=qc.QC_MODEL_UPSTREAM,
        prompt_sha256=qc.PROMPT_SHA256, max_concurrency=1)).claims[0]
    assert art.canon_sha256 == canon.canon_sha256
    assert art.to_canonical_obj()["canon_sha256"] == canon.canon_sha256


def test_8_23_serializer_contract_mutation_changes_the_digest():
    base = dict(qc._QC_REQUEST_CONTRACT_OBJ)
    for key, mutation in (
        ("json_ensure_ascii", True), ("json_sort_keys", False),
        # The probe value must never equal the LIVE version, or the mutation is a no-op
        # and this key silently stops being tested — which is what happened when the
        # contract moved to v4 while the probe still said v4.
        ("json_separators", [", ", ": "]),
        ("contract_version", "qc_request_contract_vPROBE"),
        ("dynamic_field_order", ["canon", "chapter_atoms"]),
        ("canon_projection_rule", "something_else"),
    ):
        mutated = dict(base)
        mutated[key] = mutation
        text = json.dumps(mutated, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
        assert hashlib.sha256(text.encode()).hexdigest() != qc.PROMPT_SHA256, key


def test_8_24_changed_canon_invalidates_reuse():
    snap = _snapshot()
    canon_a = _authoritative_canon()
    canon_b = _canon(entities=(cl.CanonEntityV1(
        entity_id="e2", canonical_name="Bima", aliases=(), alias_source="none"),))
    assert canon_a.canon_sha256 != canon_b.canon_sha256
    keys = set()
    for canon in (canon_a, canon_b):
        run = _run(ext.extract_all(
            snap, canon, provider=_ok_provider(), model_version=qc.QC_MODEL_UPSTREAM,
            prompt_sha256=qc.PROMPT_SHA256, max_concurrency=1))
        keys.add(l2.claim_cache_key(run.claims[0]))
    assert len(keys) == 2                  # same bytes, different canon -> no reuse


def test_8_25_canon_projection_parity():
    req = _request()
    projected = json.loads(qc.build_user_content(req))["canon"]
    assert projected == req.canon.to_canonical_obj(include_hash=True)
    assert projected["canon_sha256"] == req.canon_sha256


def test_8_26_cache_key_tuple_and_order():
    assert l2.CLAIM_CACHE_KEY_FIELDS == (
        "schema_version", "chapter_index", "chapter_id", "content_sha256",
        "canon_sha256", "atom_table_sha256", "extractor_version", "model_version",
        "prompt_sha256", "predicate_set_version")
    assert len(l2.CLAIM_CACHE_KEY_FIELDS) == 10
    canon = _authoritative_canon()
    run = _run(ext.extract_all(
        _snapshot(), canon, provider=_ok_provider(),
        model_version=qc.QC_MODEL_UPSTREAM, prompt_sha256=qc.PROMPT_SHA256,
        max_concurrency=1))
    key = l2.claim_cache_key(run.claims[0])
    assert len(key) == 10
    assert key[4] == canon.canon_sha256
    assert key[5] == run.claims[0].atom_table_sha256


def test_8_27_pre_atom_payload_rejected_after_the_bump():
    assert l2.CLAIMS_SCHEMA_VERSION == "chapter_claims_v3"
    snap, canon = _snapshot(), _authoritative_canon()
    payload = _valid_payload(snap, canon)
    payload["schema_version"] = "chapter_claims_v2"
    with pytest.raises(cl.CanonSchemaError):
        l2.parse_chapter_claims(payload, snapshot=snap, canon=canon)


def test_8_28_failure_artefacts_carry_canon_sha256():
    canon = _authoritative_canon()

    async def failing(request):
        raise RuntimeError("provider down")

    run = _run(ext.extract_all(
        _snapshot(), canon, provider=failing, model_version=qc.QC_MODEL_UPSTREAM,
        prompt_sha256=qc.PROMPT_SHA256, max_concurrency=1))
    for art in run.claims:
        assert art.canon_sha256 == canon.canon_sha256
        assert art.coverage_state == l2.COVERAGE_PROVIDER_FAILURE


# ===========================================================================
# §8.29 - §8.33a  Preflight: zero HTTP, zero sink.begin()
# ===========================================================================

class _CountingProvider:
    """Stands in for MeteredProvider: counts sink.begin() and HTTP separately."""

    def __init__(self):
        self.begins = 0
        self.http = 0

    async def __call__(self, request):
        self.begins += 1                   # MeteredProvider calls begin() BEFORE the
        self.http += 1                     # adapter, so both would be non-zero
        return {"coverage": {p: "NO_CLAIMS_FOUND" for p in l2.SEMANTIC_PREDICATES},
                "claims": []}


def _replace_hash(canon, value="f" * 64):
    """A canon whose stored hash no longer binds its content.

    dataclasses.replace() cannot build one: __post_init__ re-validates the binding and
    refuses — which is itself the constructor invariant working. To exercise the
    downstream guards we have to forge the object past its own constructor, exactly the
    corruption those guards exist to catch.
    """
    import copy
    tampered = copy.copy(canon)
    object.__setattr__(tampered, "canon_sha256", value)
    return tampered


def test_8_29_invalid_canon_blocks_at_step_2():
    bad = _replace_hash(_authoritative_canon())
    assert not bad.verify_sha256()
    counter = _CountingProvider()
    with pytest.raises(cl.CanonSchemaError) as e:
        _run(ext.extract_all(
            _snapshot(), bad, provider=counter, model_version=qc.QC_MODEL_UPSTREAM,
            prompt_sha256=qc.PROMPT_SHA256, max_concurrency=1))
    assert "extractor_canon_hash_invalid" in str(e.value)
    assert counter.begins == 0 and counter.http == 0


def test_8_29a_adapter_defence_in_depth_is_metered():
    """Reached INSIDE the adapter, a projection failure costs zero HTTP but exactly one
    failed physical-attempt row. Asserting zero rows here would be wrong."""
    adapter, client = _adapter()
    bad_request = _request(canon=_replace_hash(_authoritative_canon()))
    with pytest.raises(qc.QcProviderError):
        _run(adapter(bad_request))
    assert len(client.calls) == 0          # zero HTTP...
    # ...but MeteredProvider would already have opened a row: that is why the preflight,
    # not this layer, is the only thing that can promise zero rows.


def test_8_30_generation_policy_bound():
    base = dict(qc._QC_REQUEST_CONTRACT_OBJ)
    for key, mutation in (("temperature", "0.7"), ("max_tokens", 4096),
                          ("stream", True), ("n", 2),
                          ("response_format_schedule", ["none", "none", "none"])):
        mutated = dict(base)
        mutated[key] = mutation
        text = json.dumps(mutated, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
        assert hashlib.sha256(text.encode()).hexdigest() != qc.PROMPT_SHA256, key
    # any field of the response schema
    mutated = dict(base)
    schema = json.loads(json.dumps(qc.QC_RESPONSE_SCHEMA))
    schema["additionalProperties"] = True
    mutated["response_schema"] = schema
    text = json.dumps(mutated, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(text.encode()).hexdigest() != qc.PROMPT_SHA256


def test_8_31_identity_is_part_of_cache_identity():
    """Identical bytes at a different identity must not share an entry.

    Built directly rather than through the materializer: real blocks carry their heading,
    so two chapters never have byte-identical content. The defect this row guards is a
    DUPLICATED PASSAGE serving one chapter's claims for another, which needs the content
    hash held equal while identity varies — exactly what the materializer will not produce.
    """
    import dataclasses
    canon, snap = _authoritative_canon(), _snapshot()
    run = _run(ext.extract_all(
        snap, canon, provider=_ok_provider(), model_version=qc.QC_MODEL_UPSTREAM,
        prompt_sha256=qc.PROMPT_SHA256, max_concurrency=1))
    base = run.claims[0]
    other_index = dataclasses.replace(base, chapter_index=1)
    other_id = dataclasses.replace(base, chapter_id="ch2")
    assert base.content_sha256 == other_index.content_sha256 == other_id.content_sha256
    keys = {l2.claim_cache_key(a) for a in (base, other_index, other_id)}
    assert len(keys) == 3          # same bytes, three identities, three keys


def test_8_32_content_hash_mismatch_blocks_before_http_and_metering():
    snap, canon = _snapshot(), _authoritative_canon()
    block = snap.blocks[0]
    bad = ext.ExtractionRequestV1(
        chapter_index=0, chapter_id=block.chapter_id,
        content_sha256="c" * 64,                        # does not bind the bytes
        canon_sha256=canon.canon_sha256, attempt=1,
        chapter_bytes=snap.block_bytes(0), canon=canon,
        **_atom_request_fields(snap, 0))
    with pytest.raises(cl.CanonSchemaError) as e:
        ext._preflight_request(bad, snapshot=snap, index=0, canon=canon)
    assert "extractor_request_content_hash_mismatch" in str(e.value)


def test_8_33_identity_mismatch_blocks_before_http_and_metering():
    snap, canon = _snapshot(), _authoritative_canon()
    block = snap.blocks[1]
    bad = ext.ExtractionRequestV1(
        chapter_index=1, chapter_id=block.chapter_id,
        content_sha256=block.content_sha256, canon_sha256=canon.canon_sha256,
        attempt=1, chapter_bytes=snap.block_bytes(1), canon=canon,
        **_atom_request_fields(snap, 1))
    with pytest.raises(cl.CanonSchemaError) as e:
        ext._preflight_request(bad, snapshot=snap, index=0, canon=canon)  # wrong index
    assert "extractor_request_identity_mismatch" in str(e.value)


def test_8_33a_request_canon_parity_three_conjuncts_independently():
    """Three SEPARATE cases — one combined case would pass while two conjuncts went
    unimplemented."""
    import dataclasses
    snap, canon = _snapshot(), _authoritative_canon()
    block = snap.blocks[0]
    other = _canon(entities=(cl.CanonEntityV1(
        entity_id="e9", canonical_name="Lain", aliases=(), alias_source="none"),))

    def _mk(**over):
        base = dict(chapter_index=0, chapter_id=block.chapter_id,
                    content_sha256=block.content_sha256,
                    canon_sha256=canon.canon_sha256, attempt=1,
                    chapter_bytes=snap.block_bytes(0), canon=canon,
                    **_atom_request_fields(snap, 0))
        base.update(over)
        return ext.ExtractionRequestV1(**base)

    # (a) request names a different canon than the argument
    a = _mk(canon_sha256=other.canon_sha256, canon=other)
    with pytest.raises(cl.CanonSchemaError) as e:
        ext._preflight_request(a, snapshot=snap, index=0, canon=canon)
    assert "extractor_request_canon_mismatch" in str(e.value)

    # (b) request.canon fails verify_sha256() while the ARGUMENT passes — proves the
    #     check reads request.canon, not the argument. The argument stays the GOOD canon
    #     and the two hashes agree, so conjunct 1 passes and only conjunct 2 can catch it.
    tampered = _replace_hash(canon, canon.canon_sha256)
    object.__setattr__(tampered, "target_language", "xx")   # content now differs
    assert not tampered.verify_sha256()
    b = _mk()
    object.__setattr__(b, "canon", tampered)
    with pytest.raises(cl.CanonSchemaError) as e:
        ext._preflight_request(b, snapshot=snap, index=0, canon=canon)
    assert "extractor_request_canon_mismatch" in str(e.value)

    # (c) projection's embedded hash differs from request.canon_sha256
    class _SkewedCanon:
        def __init__(self, real):
            self._real = real
            self.canon_sha256 = real.canon_sha256

        def verify_sha256(self):
            return True

        def to_canonical_obj(self, *, include_hash=True):
            obj = self._real.to_canonical_obj(include_hash=include_hash)
            obj["canon_sha256"] = "e" * 64          # skewed projection
            return obj
    c = _mk()
    object.__setattr__(c, "canon", _SkewedCanon(canon))
    with pytest.raises(cl.CanonSchemaError) as e:
        ext._preflight_request(c, snapshot=snap, index=0, canon=canon)
    assert "extractor_request_canon_mismatch" in str(e.value)


# ===========================================================================
# §8.34 - §8.39  Contract summary, UNKNOWN invariant, extractor codes
# ===========================================================================

def test_8_34_contract_summary_parity():
    """The component set named in the record is exactly what is hashed."""
    assert set(qc._QC_REQUEST_CONTRACT_OBJ) == {
        "contract_version", "system_template", "dynamic_field_order",
        "canon_projection_rule", "reasoning_effort", "json_ensure_ascii",
        "json_sort_keys", "json_separators", "temperature", "max_tokens", "stream",
        "n", "response_format_schedule", "response_schema"}


def test_8_35_canon_none_yields_unknown_with_zero_calls():
    counter = _CountingProvider()
    run = _run(ext.extract_all(
        _snapshot(), None, provider=counter, model_version=qc.QC_MODEL_UPSTREAM,
        prompt_sha256=qc.PROMPT_SHA256, max_concurrency=1))
    assert counter.begins == 0 and counter.http == 0
    for art in run.claims:
        assert art.canon_sha256 == cl.UNKNOWN
        assert all(c.state == l2.COVERAGE_NO_CANON_AUTHORITY for c in art.coverage)
        assert art.claims == ()


def test_8_35a_valid_canon_without_authority_carries_its_real_hash():
    structural = _canon()                  # no entities/anchors/events -> no authority
    assert not l2.has_semantic_authority(structural)
    counter = _CountingProvider()
    run = _run(ext.extract_all(
        _snapshot(), structural, provider=counter,
        model_version=qc.QC_MODEL_UPSTREAM, prompt_sha256=qc.PROMPT_SHA256,
        max_concurrency=1))
    assert counter.begins == 0 and counter.http == 0
    for art in run.claims:
        assert art.canon_sha256 == structural.canon_sha256   # never UNKNOWN
        assert art.canon_sha256 != cl.UNKNOWN


def test_8_35b_invalid_canon_fails_before_the_short_circuit():
    """It is never converted into a NO_CANON_AUTHORITY artefact."""
    bad = _replace_hash(_canon())          # invalid AND without authority
    with pytest.raises(cl.CanonSchemaError) as e:
        _run(ext.extract_all(
            _snapshot(), bad, provider=_ok_provider(),
            model_version=qc.QC_MODEL_UPSTREAM, prompt_sha256=qc.PROMPT_SHA256,
            max_concurrency=1))
    assert "extractor_canon_hash_invalid" in str(e.value)


def test_8_36_unknown_is_conditional():
    snap, canon = _snapshot(), _authoritative_canon()
    # structural: UNKNOWN with a non-NO_CANON_AUTHORITY state is rejected
    with pytest.raises(cl.CanonSchemaError):
        l2.ChapterClaimsV1(
            schema_version=l2.CLAIMS_SCHEMA_VERSION, chapter_index=0,
            chapter_id=snap.blocks[0].chapter_id,
            content_sha256=snap.blocks[0].content_sha256, canon_sha256=cl.UNKNOWN,
            atom_table_sha256=l2.build_chapter_atom_table(snap.block_bytes(0))[1],
            extractor_version=ext.EXTRACTOR_VERSION,
            model_version=qc.QC_MODEL_UPSTREAM, prompt_sha256=qc.PROMPT_SHA256,
            predicate_set_version=l2.PREDICATE_SET_VERSION,
            coverage=tuple(l2.PredicateCoverageV1(predicate=p,
                                                  state=l2.COVERAGE_NO_CLAIMS_FOUND)
                           for p in l2.SEMANTIC_PREDICATES),
            claims=())
    # contextual (stronger): UNKNOWN with a canon supplied is rejected by the parser
    payload = _valid_payload(snap, canon)
    payload["canon_sha256"] = cl.UNKNOWN
    with pytest.raises(cl.CanonSchemaError):
        l2.parse_chapter_claims(payload, snapshot=snap, canon=canon)


def test_8_37_failure_artefacts_name_the_real_canon():
    canon = _authoritative_canon()

    async def timing_out(request):
        raise asyncio.TimeoutError()

    run = _run(ext.extract_all(
        _snapshot(), canon, provider=timing_out, model_version=qc.QC_MODEL_UPSTREAM,
        prompt_sha256=qc.PROMPT_SHA256, max_concurrency=1))
    for art in run.claims:
        assert art.canon_sha256 == canon.canon_sha256
        assert art.canon_sha256 != cl.UNKNOWN


def test_8_38_unknown_is_not_cacheable():
    run = _run(ext.extract_all(
        _snapshot(), None, provider=_ok_provider(),
        model_version=qc.QC_MODEL_UPSTREAM, prompt_sha256=qc.PROMPT_SHA256,
        max_concurrency=1))
    with pytest.raises(cl.CanonSchemaError) as e:
        l2.claim_cache_key(run.claims[0])
    assert "not cacheable" in str(e.value)


def test_8_39_extractor_codes_are_extractor_owned():
    """Asserted on IMPORT statements, not on any mention: the module documents why it
    does not import the adapter, and a substring match would forbid saying so."""
    source = Path(ext.__file__).read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        assert not stripped.startswith(
            ("import canon_lite_qc_provider", "from canon_lite_qc_provider")), line
        assert not stripped.startswith(
            ("import canon_lite_qc_meter", "from canon_lite_qc_meter")), line
    assert "from canon_lite_qc_contract import (" in source
    assert "raise QcProviderError" not in source
    snap, canon = _snapshot(), _authoritative_canon()
    bad = ext.ExtractionRequestV1(
        chapter_index=0, chapter_id=snap.blocks[0].chapter_id,
        content_sha256="c" * 64, canon_sha256=canon.canon_sha256, attempt=1,
        chapter_bytes=snap.block_bytes(0), canon=canon,
        **_atom_request_fields(snap, 0))
    with pytest.raises(cl.CanonSchemaError):
        ext._preflight_request(bad, snapshot=snap, index=0, canon=canon)


# ===========================================================================
# §8.40 - §8.41a  Single source of truth, derivation, parser binding
# ===========================================================================

def test_8_40_contract_has_one_source_of_truth():
    source = Path(qc.__file__).read_text(encoding="utf-8")
    assert source.count('"qc_request_contract_v6"') == 1
    rebuilt = dict(qc._QC_REQUEST_CONTRACT_OBJ)
    rebuilt["temperature"] = "0.9"
    text = json.dumps(rebuilt, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    assert hashlib.sha256(text.encode()).hexdigest() != qc.PROMPT_SHA256


def test_8_40a_contract_derivation_is_exact():
    obj = qc._QC_REQUEST_CONTRACT_OBJ
    assert len(obj) == 14
    # declared parameters ARE the ones used
    assert obj["json_ensure_ascii"] is qc.QC_JSON_ENSURE_ASCII
    assert obj["json_sort_keys"] is qc.QC_JSON_SORT_KEYS
    assert obj["json_separators"] == list(qc.QC_JSON_SEPARATORS)
    text = json.dumps(obj, ensure_ascii=obj["json_ensure_ascii"],
                      sort_keys=obj["json_sort_keys"],
                      separators=tuple(obj["json_separators"]))
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == qc.PROMPT_SHA256
    # no JSON number in the bytes originates from float formatting
    assert isinstance(obj["temperature"], str)
    assert obj["temperature"] == qc.QC_TEMPERATURE_TEXT
    source = Path(qc.__file__).read_text(encoding="utf-8")
    assert "str(QC_TEMPERATURE)" not in source


def test_8_40b_template_round_trip_and_non_containment():
    assert hashlib.sha256(qc.QC_SYSTEM_TEMPLATE_BYTES).hexdigest() == \
        RATIFIED_TEMPLATE_SHA256
    parsed = json.loads(qc._QC_REQUEST_CONTRACT_TEXT)["system_template"]
    assert parsed.encode("utf-8") == qc.QC_SYSTEM_TEMPLATE_BYTES
    # The property is REVERSIBLE ENCODING, not containment: the template contains `"`,
    # which JSON escapes, so its bytes are not a contiguous run in the contract bytes.
    assert b'"' in qc.QC_SYSTEM_TEMPLATE_BYTES
    assert qc.QC_SYSTEM_TEMPLATE_BYTES not in qc.QC_REQUEST_CONTRACT_BYTES
    # a template with a control char, backslash and non-BMP codepoint still round-trips
    tricky = 'a"b\\c\x01d\U0001F600e'
    blob = json.dumps({"system_template": tricky}, ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"))
    assert json.loads(blob)["system_template"].encode("utf-8") == tricky.encode("utf-8")


def test_8_40c_temperature_is_one_to_one_with_what_is_sent():
    assert qc.QC_TEMPERATURE_TEXT == repr(qc.QC_TEMPERATURE)
    assert qc.QC_TEMPERATURE_TEXT == "0.0"
    # every non-canonical spelling that collapses to the same float is REFUSED, not
    # normalized: otherwise two PROMPT_SHA256 values attest to one runtime temperature
    for spelling, canonical in (("0.10", "0.1"), ("0.100", "0.1"), ("1e-1", "0.1"),
                                ("0", "0.0"), ("0.00", "0.0"), ("2.0", "2.0")):
        collapses = repr(float(Decimal(spelling))) == canonical
        is_canonical = spelling == repr(float(Decimal(spelling)))
        if collapses and not is_canonical:
            assert spelling != repr(float(Decimal(spelling)))   # gate would refuse it
    for bad in ("NaN", "Infinity", "-Infinity"):
        d = Decimal(bad)
        assert not d.is_finite()
    assert repr(float(Decimal("1e400"))) == "inf"               # refused by the gate
    adapter, client = _adapter()
    _run(adapter(_request()))
    assert client.calls[0]["temperature"] == qc.QC_TEMPERATURE   # the derived float


def test_8_41_parser_binding_both_directions():
    snap, canon = _snapshot(), _authoritative_canon()
    other = _canon(entities=(cl.CanonEntityV1(
        entity_id="e3", canonical_name="Sari", aliases=(), alias_source="none"),))

    # canon=None with a real hash
    p = _valid_payload(snap, canon)
    with pytest.raises(cl.CanonSchemaError):
        l2.parse_chapter_claims(p, snapshot=snap, canon=None)
    # supplied canon with UNKNOWN
    p = _valid_payload(snap, canon)
    p["canon_sha256"] = cl.UNKNOWN
    with pytest.raises(cl.CanonSchemaError):
        l2.parse_chapter_claims(p, snapshot=snap, canon=canon)
    # supplied canon with a DIFFERENT real hash
    p = _valid_payload(snap, canon)
    p["canon_sha256"] = other.canon_sha256
    with pytest.raises(cl.CanonSchemaError):
        l2.parse_chapter_claims(p, snapshot=snap, canon=canon)


def test_8_41a_self_inconsistent_canon_rejected_even_when_payload_agrees():
    """The payload and canon AGREE — all three 8.41 cases pass it. Only the first
    conjunct of the parser rule catches it: consistency with a corrupted canon is not
    provenance, it is two copies of the same wrong fact."""
    snap = _snapshot()
    tampered = _replace_hash(_authoritative_canon())
    assert not tampered.verify_sha256()
    payload = _valid_payload(snap, tampered)
    payload["canon_sha256"] = tampered.canon_sha256      # agrees with the tampered hash
    with pytest.raises(cl.CanonSchemaError) as e:
        l2.parse_chapter_claims(payload, snapshot=snap, canon=tampered)
    assert "self-consistent" in str(e.value)


# ===========================================================================
# §8.42 - §8.43c  Module placement, capability gates
# ===========================================================================

EXCLUDED_PATHS = ("api_direct", "api_fallback", "worker_mode_off")


@pytest.mark.parametrize("path", EXCLUDED_PATHS)
def test_8_42_module_unimported_on_every_excluded_path(path):
    """Three SEPARATE assertions — a single combined one would pass while two paths
    leaked the import."""
    code = (
        "import sys, os\n"
        "sys.path.insert(0, %r)\n"
        "os.environ.pop('NARASI_CANON_LITE_MODE', None)\n"
        "import canon_lite as cl\n"
        "import canon_lite_qc_meter as m\n"
        "assert cl.resolve_mode() == cl.MODE_OFF\n"
        "assert not m.metered_host_ok()\n"
        "assert 'canon_lite_qc_provider' not in sys.modules, %r\n"
        "print('clean')\n"
    ) % (str(Path(qc.__file__).parent), path)
    import subprocess
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "clean" in out.stdout


def test_8_42a_placement_and_gate_locality():
    assert Path(qc.__file__).name == "canon_lite_qc_provider.py"
    # no module-level import of the provider anywhere in python/
    pkg = Path(qc.__file__).parent
    for path in pkg.glob("*.py"):
        if path.name == "canon_lite_qc_provider.py":
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(("import canon_lite_qc_provider",
                                "from canon_lite_qc_provider")):
                pytest.fail(f"module-level provider import in {path.name}")
    # every constant lives in the provider module
    for name in ("QC_SYSTEM_TEMPLATE", "QC_TEMPERATURE_TEXT", "QC_MAX_TOKENS",
                 "QC_RESPONSE_SCHEMA", "PROMPT_SHA256", "QC_REQUEST_CONTRACT_BYTES"):
        assert hasattr(qc, name)
        assert not hasattr(meter, name)


def test_8_43_temperature_range_gate():
    lo = Decimal(qc.QC_CAP_TEMPERATURE_MIN_TEXT)
    hi = Decimal(qc.QC_CAP_TEMPERATURE_MAX_TEXT)
    assert lo <= qc.QC_TEMPERATURE_DECIMAL <= hi
    for out_of_range in ("-3.0", "999.0", "2.5"):
        d = Decimal(out_of_range)
        assert d.is_finite()
        assert out_of_range == repr(float(d))          # canonical but INVALID
        assert not (lo <= d <= hi)                     # the gate refuses it
    # comparison runs in Decimal, never float
    src = Path(qc.__file__).read_text(encoding="utf-8")
    assert "Decimal(cap_min_text) <= temperature_decimal <= Decimal(cap_max_text)" in src
    # and the gate genuinely refuses each of them
    for out_of_range in ("-3.0", "999.0", "2.5"):
        with pytest.raises(meter.MeterConfigurationError) as e:
            qc.validate_constants(temperature_text=out_of_range)
        assert str(e.value) == "qc_temperature_out_of_range"


def test_8_43a_max_tokens_type_and_bounds():
    assert type(qc.QC_MAX_TOKENS) is int
    assert 1 <= qc.QC_MAX_TOKENS <= qc.QC_CAP_MAX_OUTPUT_TOKENS
    # the case an isinstance implementation silently passes
    assert isinstance(True, int)                       # the trap
    assert type(True) is not int                       # what the gate actually checks
    for bad in (True, False, 0, -1, 1.0, "16384", qc.QC_CAP_MAX_OUTPUT_TOKENS + 1):
        ok = type(bad) is int and 1 <= bad <= qc.QC_CAP_MAX_OUTPUT_TOKENS
        assert not ok, bad
    for good in (1, qc.QC_CAP_MAX_OUTPUT_TOKENS):
        assert type(good) is int and 1 <= good <= qc.QC_CAP_MAX_OUTPUT_TOKENS


def test_8_43b_structured_output_support_is_required():
    assert qc.QC_CAP_STRUCTURED_OUTPUT_SUPPORTED is True
    assert qc.QC_RESPONSE_FORMAT_SCHEDULE == ("json_schema",) * 3
    # never falls back to plain mode or json_object on any bounded attempt
    src = Path(qc.__file__).read_text(encoding="utf-8")
    assert '"type": "json_object"' not in src
    for attempt in (1, 2, 3):
        assert qc.response_format_for(attempt)["type"] == "json_schema"


def _gate(**over):
    """Run the real import-time gate with one constant replaced, and return its code."""
    with pytest.raises(meter.MeterConfigurationError) as e:
        qc.validate_constants(**over)
    return str(e.value)


def test_gates_actually_reject_bad_constants():
    """Mutation control found every import-time gate UNFALSIFIABLE: with the ratified
    constants a gate never fires, so deleting one changed nothing and no test noticed.
    These exercise each gate with a value it must refuse."""
    qc.validate_constants()                                   # ratified values pass

    # temperature canonicality — non-canonical spellings that collapse to the same float
    for bad in ("0.10", "0.100", "1e-1", "0", "0.00", "00.0"):
        assert _gate(temperature_text=bad) == "qc_temperature_not_canonical"
    for bad in ("NaN", "Infinity", "-Infinity", "1e400", "not-a-number"):
        assert _gate(temperature_text=bad) == "qc_temperature_not_canonical"

    # temperature range — canonical but outside the attested capability
    for bad in ("-3.0", "999.0", "2.5"):
        assert _gate(temperature_text=bad) == "qc_temperature_out_of_range"

    # max_tokens type and bounds; True is the case isinstance() would wave through.
    # None is INCLUDED: validate_constants uses a private _UNSET sentinel rather than
    # None precisely so that None — a plausible unset-constant defect — stays testable.
    for bad in (True, False, 0, -1, 1.0, "16384", None,
                qc.QC_CAP_MAX_OUTPUT_TOKENS + 1):
        assert _gate(max_tokens=bad) == "qc_max_tokens_invalid"
    for bad in (0, -1, True, "65536"):
        assert _gate(cap_max_output_tokens=bad) == "qc_max_tokens_invalid"

    # structured output must be exactly True — not truthy
    for bad in (False, None, 1, "yes"):
        assert _gate(structured_output_supported=bad) == "qc_structured_output_unsupported"

    # pricing_version: syntactic AND semantic
    for bad in ("qc-rates-YYYY-MM-DD", "qc-rates-2026-13-45", "2026-07-30", "", None):
        assert _gate(pricing_version=bad) == "qc_pricing_version_invalid"

    # attestation strings must be real
    for bad in (("", "x", "y", "z"), ("   ", "x", "y", "z"),
                ("has ____ sentinel", "x", "y", "z"), (None, "x", "y", "z")):
        assert _gate(attestations=bad) == "qc_attestation_source_invalid"


def test_template_round_trip_gate_actually_rejects():
    """The gate must fire when the contract text and the template bytes disagree."""
    qc.validate_template_round_trip(qc._QC_REQUEST_CONTRACT_TEXT,
                                    qc.QC_SYSTEM_TEMPLATE_BYTES)
    skewed = json.dumps({"system_template": "something else entirely"},
                        ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with pytest.raises(meter.MeterConfigurationError) as e:
        qc.validate_template_round_trip(skewed, qc.QC_SYSTEM_TEMPLATE_BYTES)
    assert str(e.value) == "qc_system_template_not_round_trippable"
    # and it passes for a template full of the characters JSON must escape
    tricky = 'a"b\\c\x01d\U0001F600e'
    ok = json.dumps({"system_template": tricky}, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"))
    qc.validate_template_round_trip(ok, tricky.encode("utf-8"))


def test_empty_string_content_is_a_failure_not_a_success():
    """An empty 200 RAISES. Distinct from a missing choices list, which an earlier
    version of this suite was the only case it actually covered."""
    for blank in ("", "   ", "\n\t "):
        adapter, client = _adapter()
        client.content = blank                 # choices present, content blank
        with pytest.raises(qc.QcProviderError) as e:
            _run(adapter(_request()))
        assert str(e.value) == "qc_provider_empty_response"
        assert len(client.calls) == 1          # and no retry was hidden inside the row


def test_8_43c_capability_constants_are_outside_the_contract():
    assert len(qc._QC_REQUEST_CONTRACT_OBJ) == 14
    for key in qc._QC_REQUEST_CONTRACT_OBJ:
        assert not key.startswith("cap_")
    assert "QC_CAP" not in json.dumps(qc._QC_REQUEST_CONTRACT_OBJ)
    # a cap re-attestation cannot masquerade as a changed request
    assert qc.PROMPT_SHA256 == RATIFIED_PROMPT_SHA256


# ===========================================================================
# Host sentinel and concurrency loader (Topology Amendment 001 §1.1)
# ===========================================================================

def test_host_sentinel_is_server_set_and_never_payload_derived():
    meter.reset_host_role_for_tests()
    try:
        assert not meter.metered_host_ok()
        with pytest.raises(meter.MeterConfigurationError):
            meter.declare_host_role("python_api")       # not a permitted role
        meter.declare_host_role("narration_worker")
        assert meter.metered_host_ok()
        meter.declare_host_role("narration_worker")     # idempotent
    finally:
        meter.reset_host_role_for_tests()


def test_extractor_concurrency_loader_requires_host_and_has_no_default():
    meter.reset_host_role_for_tests()
    try:
        with pytest.raises(meter.MeterConfigurationError) as e:
            meter.load_extractor_concurrency({"CANON_LITE_EXTRACTOR_CONCURRENCY": "4"})
        assert "host_not_permitted" in str(e.value)
        meter.declare_host_role("narration_worker")
        with pytest.raises(meter.MeterConfigurationError) as e:
            meter.load_extractor_concurrency({})
        assert str(e.value) == "extractor_concurrency_missing"
        for bad, code in (("0", "extractor_concurrency_invalid"),
                          ("abc", "extractor_concurrency_invalid"),
                          ("9", "extractor_concurrency_out_of_range"),
                          ("1000", "extractor_concurrency_out_of_range")):
            with pytest.raises(meter.MeterConfigurationError) as e:
                meter.load_extractor_concurrency(
                    {"CANON_LITE_EXTRACTOR_CONCURRENCY": bad})
            assert str(e.value) == code, bad
        assert meter.load_extractor_concurrency(
            {"CANON_LITE_EXTRACTOR_CONCURRENCY": "1"}) == 1
        assert meter.load_extractor_concurrency(
            {"CANON_LITE_EXTRACTOR_CONCURRENCY": "8"}) == 8
    finally:
        meter.reset_host_role_for_tests()


def test_one_wave_per_job_invariant():
    token = ext.ExtractionWaveToken()
    snap, canon = _snapshot(), _authoritative_canon()
    _run(ext.extract_all(snap, canon, provider=_ok_provider(),
                         model_version=qc.QC_MODEL_UPSTREAM,
                         prompt_sha256=qc.PROMPT_SHA256, max_concurrency=1,
                         wave_token=token))
    assert token.claimed
    with pytest.raises(cl.CanonSchemaError) as e:
        _run(ext.extract_all(snap, canon, provider=_ok_provider(),
                             model_version=qc.QC_MODEL_UPSTREAM,
                             prompt_sha256=qc.PROMPT_SHA256, max_concurrency=1,
                             wave_token=token))
    assert "wave_already_claimed" in str(e.value)


def test_prompt_sha256_is_required_and_has_no_default():
    """A default is exactly how the wrong hash would ship silently into provenance."""
    import inspect
    sig = inspect.signature(ext.extract_all)
    assert sig.parameters["prompt_sha256"].default is inspect.Parameter.empty
    assert not hasattr(ext, "PROMPT_SHA256")           # the descriptor digest is gone


def test_approved_pins_are_what_the_suite_actually_ran_against():
    """Owner decision 2: compatibility must be PROVEN by tests, not assumed."""
    import httpx
    import openai
    assert openai.__version__ == "2.41.0"
    assert httpx.__version__ == "0.28.1"
    reqs = (Path(qc.__file__).parent / "requirements.txt").read_text(encoding="utf-8")
    assert "openai==2.41.0" in reqs
    assert "httpx==0.28.1" in reqs


# ===========================================================================
# DG-4 caller wiring — the runner's frozen gate order
# ===========================================================================

import canon_lite_qc_runner as runner       # noqa: E402  (imports no adapter)

ENV_ON = {"NARASI_CANON_LITE_MODE": "shadow",
          "CANON_LITE_EXTRACTOR_CONCURRENCY": "2",
          "L2B_MAX_INFLIGHT": "8",
          qc.QC_API_KEY_ENV: "dummy-not-a-real-credential"}


def test_runner_gate1_mode_off_refuses_before_anything():
    meter.reset_host_role_for_tests()
    try:
        assert runner.metered_wave_permitted({"NARASI_CANON_LITE_MODE": "off"}) is False
        assert runner.metered_wave_permitted({}) is False
        # even with the host declared, mode off still refuses — gate 1 precedes gate 2
        meter.declare_host_role("narration_worker")
        assert runner.metered_wave_permitted({"NARASI_CANON_LITE_MODE": "off"}) is False
    finally:
        meter.reset_host_role_for_tests()


def test_runner_gate2_refuses_off_host_even_with_mode_on():
    """The `python` service runs the same job code via api_direct/api_fallback and has no
    concurrency bound, so its term in the max_inflight formula is undefined."""
    meter.reset_host_role_for_tests()
    try:
        assert runner.metered_wave_permitted(ENV_ON) is False
        meter.declare_host_role("narration_worker")
        assert runner.metered_wave_permitted(ENV_ON) is True
    finally:
        meter.reset_host_role_for_tests()


def test_runner_refusal_reads_no_config_and_imports_no_adapter():
    """Gate order is mode -> host -> config, and the order is load-bearing: the module
    ships to `python` where CANON_LITE_EXTRACTOR_CONCURRENCY is intentionally absent, so
    reading config before the host check would crash a host that never meters."""
    code = (
        "import sys, asyncio\n"
        "sys.path.insert(0, %r)\n"
        "import canon_lite_qc_runner as r\n"
        # mode ON but no host sentinel, and NO concurrency var present at all
        "env = {'NARASI_CANON_LITE_MODE': 'shadow'}\n"
        "assert r.metered_wave_permitted(env) is False\n"
        "out = asyncio.run(r.maybe_run_metered_wave(None, None, run_id='r',"
        " job_uuid=None, job_external_id=None, environ=env, wave_token=None))\n"
        "assert out is None, out\n"
        "assert 'canon_lite_qc_provider' not in sys.modules\n"
        "print('clean')\n"
    ) % str(Path(qc.__file__).parent)
    import subprocess
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert "clean" in res.stdout


class _FakeRedisClient:
    """In-memory stand-in for the in-flight counter. Opens no socket.

    Without one, MeteredProvider fails SAFE — no redis means it cannot bound in-flight
    calls, so it arms the kill and blocks every attempt. That is correct production
    behaviour and is why this fake is required to exercise the success path at all.
    """

    def __init__(self):
        self.store = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, **kw):
        self.store[key] = value
        return True

    async def incr(self, key):
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]

    async def decr(self, key):
        self.store[key] = int(self.store.get(key, 0)) - 1
        return self.store[key]

    async def delete(self, key):
        self.store.pop(key, None)
        return 1


_FAKE_REDIS = _FakeRedisClient()


def _FakeRedis():
    return _FAKE_REDIS


def test_runner_runs_exactly_one_reconciled_wave():
    """Both gates pass: one wave, metered, with the ratified provenance."""
    meter.reset_host_role_for_tests()
    _FAKE_REDIS.store.clear()
    begins = []

    class FakeSink:
        async def begin(self, context, *, unit_index, attempt_ordinal):
            begins.append((context.phase, context.provider, context.model_upstream,
                           str(context.pricing_version), unit_index, attempt_ordinal))
            # `outcome` is REQUIRED: without it MeteredProvider treats the row as
            # begin_write_failed and arms the kill, which then blocks every later
            # attempt process-wide.
            return {"attempt_id": f"a{len(begins)}", "outcome": "inserted"}

        async def finish(self, attempt_id, state):
            return {"attempt_id": attempt_id, "state": state}

        async def resolve(self, attempt_id, usage):
            # `outcome` must be applied/already_same; anything else arms the kill and
            # every subsequent attempt in the process is blocked.
            return {"attempt_id": attempt_id, "outcome": "applied"}

        async def arm(self, reason_code):
            return {}

        async def is_armed(self):
            return False

    # _process_killed is a MODULE GLOBAL. One armed kill in an earlier test would block
    # every attempt here and the failure would look like a wiring bug.
    meter._process_killed = False
    try:
        meter.declare_host_role("narration_worker")
        adapter, client = _adapter()
        claims = _run(runner.maybe_run_metered_wave(
            _snapshot(), _authoritative_canon(),
            run_id="run-1", job_uuid="00000000-0000-4000-8000-000000000001",
            job_external_id="job-1", environ=ENV_ON,
            wave_token=ext.ExtractionWaveToken(),
            sink=FakeSink(), adapter_factory=lambda: adapter,
            redis_getter=_FakeRedis))
    finally:
        meter.reset_host_role_for_tests()

    assert claims is not None and len(claims) == 2       # one artefact per chapter
    for artifact in claims.values():
        assert artifact.prompt_sha256 == RATIFIED_PROMPT_SHA256
        assert artifact.model_version == qc.QC_MODEL_UPSTREAM
        assert artifact.canon_sha256 != cl.UNKNOWN
    # exactly one metered row per physical attempt, on the only catalogued phase
    assert len(begins) == len(client.calls) == 2
    for phase, provider, model, pricing, _idx, ordinal in begins:
        assert phase == "canon_lite_l2_extract"
        assert provider == qc.QC_PROVIDER_NAME
        assert model == qc.QC_MODEL_UPSTREAM
        assert pricing == qc.QC_PRICING.pricing_version
        assert ordinal == 1                              # no hidden retries


def _wave(**over):
    """Run one metered wave with the standard fakes. Returns claims_by_index."""
    kw = dict(run_id="r", job_uuid="00000000-0000-4000-8000-000000000001",
              job_external_id="j", environ=ENV_ON, redis_getter=_FakeRedis,
              # mandatory now: the runner mints nothing
              wave_token=ext.ExtractionWaveToken())
    kw.update(over)
    if "sink" not in kw:
        class S:
            def __init__(self):
                self.n = 0

            async def begin(self, c, *, unit_index, attempt_ordinal):
                self.n += 1
                return {"attempt_id": f"a{self.n}", "outcome": "inserted"}

            async def finish(self, a, s):
                return {"attempt_id": a}

            async def resolve(self, a, u):
                return {"attempt_id": a, "outcome": "applied"}

            async def arm(self, r):
                return {}

            async def is_armed(self):
                return False
        kw["sink"] = S()
    if "adapter_factory" not in kw:
        adapter, _ = _adapter()
        kw["adapter_factory"] = lambda: adapter
    return _run(runner.maybe_run_metered_wave(
        _snapshot(), _authoritative_canon(), **kw))


def test_runner_wave_token_is_job_scoped_not_minted_internally():
    """A token minted INSIDE the runner would guard nothing — every call would get its
    own, so two waves for one job would both claim successfully and the one-wave
    invariant would be decorative. It must be threadable by the caller."""
    import inspect
    assert "wave_token" in inspect.signature(runner.maybe_run_metered_wave).parameters
    meter.reset_host_role_for_tests()
    meter._process_killed = False
    _FAKE_REDIS.store.clear()
    try:
        meter.declare_host_role("narration_worker")
        token = ext.ExtractionWaveToken()
        assert _wave(wave_token=token) is not None
        assert token.claimed
        # the SAME job token cannot open a second wave
        with pytest.raises(cl.CanonSchemaError) as e:
            _wave(wave_token=token)
        assert "wave_already_claimed" in str(e.value)
    finally:
        meter.reset_host_role_for_tests()


def test_runner_refuses_more_rows_than_attempts():
    """A row can never exist without an attempt that produced it. True reconciliation
    against DURABLE rows is the reaper's job (DG-5); this is the local invariant."""
    meter.reset_host_role_for_tests()
    meter._process_killed = False
    _FAKE_REDIS.store.clear()

    class InflatedProvider:
        """Reports more emitted rows than the extractor ever attempted."""
        emitted_attempts = 10_000

        def __init__(self, *a, **k):
            pass

        async def __call__(self, request):
            return {"coverage": {p: "NO_CLAIMS_FOUND"
                                 for p in l2.SEMANTIC_PREDICATES}, "claims": []}

        def assert_reconciled(self, n):
            pass

    import canon_lite_qc_meter as m
    real = m.MeteredProvider
    try:
        meter.declare_host_role("narration_worker")
        m.MeteredProvider = InflatedProvider
        with pytest.raises(RuntimeError) as e:
            _wave()
        assert "exceed_logical" in str(e.value)
    finally:
        m.MeteredProvider = real
        meter.reset_host_role_for_tests()


def test_production_path_refuses_a_duplicate_wave_for_one_job():
    """THE production path, end to end: job-scoped token -> seam -> runner.

    The earlier revision passed this test's shape only because the runner minted its own
    token. It did not, and could not, catch the real defect: the production caller omitted
    the token entirely, so every wave got a fresh one and a duplicate wave was impossible
    to detect. This drives `narration_api`'s own helpers, not the runner directly, so a
    regression in the wiring — not just in the runner — fails here.
    """
    import narration_api as na
    meter.reset_host_role_for_tests()
    meter._process_killed = False
    _FAKE_REDIS.store.clear()

    class S:
        def __init__(self):
            self.n = 0

        async def begin(self, c, *, unit_index, attempt_ordinal):
            self.n += 1
            return {"attempt_id": f"a{self.n}", "outcome": "inserted"}

        async def finish(self, a, s):
            return {"attempt_id": a}

        async def resolve(self, a, u):
            return {"attempt_id": a, "outcome": "applied"}

        async def arm(self, r):
            return {}

        async def is_armed(self):
            return False

    real_runner = runner.maybe_run_metered_wave
    adapter, client = _adapter()

    async def metered(snapshot, canon, **kw):
        kw.setdefault("environ", ENV_ON)
        kw["sink"] = S()
        kw["adapter_factory"] = lambda: adapter
        kw["redis_getter"] = _FakeRedis
        return await real_runner(snapshot, canon, **kw)

    result = {"book": BOOK, "chapters": [{"content": "x"}]}
    try:
        meter.declare_host_role("narration_worker")
        runner.maybe_run_metered_wave = metered

        # the job mints ONE token, exactly as _run_narration_job_after_parity does
        import canon_lite_extractor as _ext
        token = _ext.ExtractionWaveToken()

        first = _run(na._canon_lite_l2_shadow_projection(
            result, mode="shadow", canon=_authoritative_canon(), wave_token=token,
            run_id="job-abc", job_uuid="00000000-0000-4000-8000-000000000001",
            job_external_id="job-abc"))
        assert first["l2_status"] == "present"
        assert token.claimed
        calls_after_first = len(client.calls)
        assert calls_after_first > 0          # the first wave really ran

        # A SECOND wave under the SAME job token must not reach the provider. The seam
        # contains the failure and still delivers a claims-free report, because a
        # metering fault may never change legacy narration delivery.
        second = _run(na._canon_lite_l2_shadow_projection(
            result, mode="shadow", canon=_authoritative_canon(), wave_token=token,
            run_id="job-abc", job_uuid="00000000-0000-4000-8000-000000000001",
            job_external_id="job-abc"))
        assert second["l2_status"] == "present"
        assert len(client.calls) == calls_after_first    # zero extra provider calls
    finally:
        runner.maybe_run_metered_wave = real_runner
        meter.reset_host_role_for_tests()


def test_wave_token_is_minted_once_at_job_scope_and_never_below_it():
    """Mandatory all the way down: no default at the seam, no mint in the runner."""
    import inspect

    import narration_api as na
    assert inspect.signature(
        runner.maybe_run_metered_wave).parameters["wave_token"].default \
        is inspect.Parameter.empty
    assert inspect.signature(
        na._canon_lite_l2_shadow_projection).parameters["wave_token"].default \
        is inspect.Parameter.empty

    runner_src = Path(runner.__file__).read_text(encoding="utf-8")
    assert "ExtractionWaveToken()" not in runner_src        # never minted below job scope

    api_src = Path(na.__file__).read_text(encoding="utf-8")
    assert api_src.count("ExtractionWaveToken()") == 1      # exactly one mint site
    # and it is inside the job-scope helper, called from the job body. The helper
    # now takes the job's tenant — assist is per-tenant, so a token minted without
    # one would arm a wave for a job whose effective mode is `off`. The property
    # this control is about is unchanged: ONE mint site, at job scope.
    assert "_cl_l2_wave_token = _canon_lite_wave_token(tenant_id)" in api_src
    assert "wave_token=_cl_l2_wave_token" in api_src

    # the runner refuses a missing or wrong-typed token rather than defaulting
    meter.reset_host_role_for_tests()
    try:
        meter.declare_host_role("narration_worker")
        for bad in (None, object(), "token"):
            with pytest.raises(RuntimeError) as e:
                _run(runner.maybe_run_metered_wave(
                    _snapshot(), _authoritative_canon(), run_id="r", job_uuid=None,
                    job_external_id=None, environ=ENV_ON, wave_token=bad))
            assert "wave_token_required" in str(e.value)
        # an empty run_id must name itself, not fail deep inside AttemptContext where
        # the seam's blanket handler would turn it into "metering silently never ran"
        for bad_run in ("", "   ", None):
            with pytest.raises(RuntimeError) as e:
                _run(runner.maybe_run_metered_wave(
                    _snapshot(), _authoritative_canon(), run_id=bad_run, job_uuid=None,
                    job_external_id=None, environ=ENV_ON,
                    wave_token=ext.ExtractionWaveToken()))
            assert "run_id_required" in str(e.value)
    finally:
        meter.reset_host_role_for_tests()


def test_flag_off_job_scope_mints_no_token_and_imports_nothing():
    """C11: flag off must import no Canon Lite module, so the mint is mode-gated."""
    code = (
        "import sys, os\n"
        "sys.path.insert(0, %r)\n"
        "os.environ.pop('NARASI_CANON_LITE_MODE', None)\n"
        "import narration_api as na\n"
        "assert na._canon_lite_wave_token() is None\n"
        "assert 'canon_lite_extractor' not in sys.modules\n"
        "print('clean')\n"
    ) % str(Path(qc.__file__).parent)
    import subprocess
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert "clean" in res.stdout


def test_runner_missing_credential_raises_rather_than_skipping():
    """Skipping would produce a report that looks measured but called nothing."""
    meter.reset_host_role_for_tests()
    env = dict(ENV_ON)
    env.pop(qc.QC_API_KEY_ENV)
    try:
        meter.declare_host_role("narration_worker")
        with pytest.raises(RuntimeError) as e:
            _run(runner.maybe_run_metered_wave(
                _snapshot(), _authoritative_canon(), run_id="r", job_uuid=None,
                job_external_id=None, environ=env,
                wave_token=ext.ExtractionWaveToken()))
        assert "api_key_missing" in str(e.value)
    finally:
        meter.reset_host_role_for_tests()


def test_narration_seam_contains_the_metered_wave_and_survives_its_failure():
    """The seam must call the runner, and a metering fault must not change delivery."""
    seam = (Path(qc.__file__).parent / "narration_api.py").read_text(encoding="utf-8")
    assert "canon_lite_qc_runner" in seam
    assert "maybe_run_metered_wave" in seam
    # the import is function-local, never module-level, on this always-loaded module
    for line in seam.splitlines():
        assert not line.startswith(("import canon_lite_qc_runner",
                                    "from canon_lite_qc_runner")), line
    # and a failure inside the wave falls back to claims-free, not partial claims
    assert "claims_by_index = None" in seam
    assert "l2_metered_wave_error" in seam


# ---------------------------------------------------------------------------
# helpers used above
# ---------------------------------------------------------------------------

def _ok_provider():
    async def provider(request):
        return {"coverage": {p: "NO_CLAIMS_FOUND" for p in l2.SEMANTIC_PREDICATES},
                "claims": []}
    return provider


def _valid_payload(snapshot, canon, index=0):
    block = snapshot.blocks[index]
    _atoms, atom_sha = l2.build_chapter_atom_table(snapshot.block_bytes(index))
    return {
        "schema_version": l2.CLAIMS_SCHEMA_VERSION,
        "chapter_index": index,
        "chapter_id": block.chapter_id,
        "content_sha256": block.content_sha256,
        "canon_sha256": canon.canon_sha256,
        "atom_table_sha256": atom_sha,
        "extractor_version": ext.EXTRACTOR_VERSION,
        "model_version": qc.QC_MODEL_UPSTREAM,
        "prompt_sha256": qc.PROMPT_SHA256,
        "predicate_set_version": l2.PREDICATE_SET_VERSION,
        "coverage": {p: l2.COVERAGE_NO_CLAIMS_FOUND for p in l2.SEMANTIC_PREDICATES},
        "claims": [],
    }


# ===========================================================================
# §8.18  THE DEFAULT ADAPTER FACTORY — the branch production actually takes
# ===========================================================================
#
# 🔴 EVERY ROW ABOVE PASSES `adapter_factory=`, AND THAT IS WHY THEY ALL PASSED WHILE
#    PRODUCTION CRASHED. `narration_api` injects no factory, so the real call takes the
#    DEFAULT branch — which referenced a local `api_key` that no statement in
#    `maybe_run_metered_wave` ever assigned. `adapter_factory()` therefore raised
#    `NameError` on a correctly configured deployment; the terminal seam does not
#    recognise that as one of its bounded faults, so it flattened to
#    `l3_metered_wave_error` / `stage=no_session` — which reads as "there was no session
#    to be had" rather than "the wave crashed on its first line". Live job `yp8f04rr` died
#    exactly there, after a clean assist census, with the credential present.
#
#    A dependency only production constructs is a dependency no test was holding. These
#    rows hold it: they run the default branch and nothing else.

class _SpySink:
    """The wave builds a QcUsageSink when none is passed, and that one talks to the DB."""

    async def begin(self, context, *, unit_index, attempt_ordinal):
        return {"attempt_id": "a1", "outcome": "inserted"}

    async def settle(self, *a, **k):
        return {}

    async def arm(self, reason_code):
        return {}

    async def is_armed(self):
        return False


def _default_factory_spy(monkeypatch):
    """Make the DEFAULT factory runnable without a provider or a socket.

    Patches the adapter CLASS (not the factory) so the branch under test is the real one,
    and stubs `extract_all` so the wave stops as soon as the adapter exists — this file
    asserts on construction, not on extraction.
    """
    seen = []

    class _SpyAdapter:
        def __init__(self, *, api_key, environ=None):
            seen.append({"api_key": api_key, "environ": environ})

        async def __call__(self, request):                      # pragma: no cover
            raise AssertionError("no request may leave this test")

    async def _no_extract(snapshot, canon, **kw):
        class _Run:
            claims_by_index: dict = {}
            logical_attempts = 0
            logical_extractions = 0
        return _Run()

    monkeypatch.setattr(qc, "QcProviderAdapter", _SpyAdapter)
    monkeypatch.setattr(ext, "extract_all", _no_extract)
    return seen


def _run_default_wave(env):
    meter._process_killed = False
    meter.reset_host_role_for_tests()
    try:
        meter.declare_host_role("narration_worker")
        return _run(runner.maybe_run_metered_wave(
            _snapshot(), _authoritative_canon(),
            run_id="r", job_uuid="00000000-0000-4000-8000-000000000001",
            job_external_id="j", environ=env,
            wave_token=ext.ExtractionWaveToken(),
            sink=_SpySink(), redis_getter=_FakeRedis,
            adapter_factory=None))          # ← the production branch, explicitly
    finally:
        meter.reset_host_role_for_tests()


def test_8_18_the_default_factory_passes_the_env_credential_to_the_adapter(monkeypatch):
    """The credential reaches the adapter INTACT — asserted by value, not by presence.

    `api_key is not None` would pass against a factory that forwarded the env NAME, an
    empty string, or a truthy placeholder. The whole defect was a name that resolved to
    nothing, so only an equality check against the value the environment actually holds
    can distinguish a working binding from a lucky one."""
    seen = _default_factory_spy(monkeypatch)
    secret = "CREDENTIAL-FROM-ENV-9f3a"
    _run_default_wave(dict(ENV_ON, **{qc.QC_API_KEY_ENV: secret}))

    assert len(seen) == 1, "the default factory built no adapter"
    assert seen[0]["api_key"] == secret


def test_8_18b_a_blank_credential_still_refuses_before_any_adapter_exists(monkeypatch):
    """Whitespace-only is absence. The bounded code must survive the fix — binding the
    value must not turn a configuration fault into a provider built with an empty key."""
    seen = _default_factory_spy(monkeypatch)
    with pytest.raises(RuntimeError) as exc:
        _run_default_wave(dict(ENV_ON, **{qc.QC_API_KEY_ENV: "   "}))

    assert str(exc.value) == "qc_provider_api_key_missing"
    assert seen == [], "an adapter was constructed for a deployment with no credential"


# ===========================================================================
# §8.19  BOUNDED TELEMETRY ON A FAILING EXTRACTION — ratified scope, 2026-08-13
# ===========================================================================
#
# 🔴 THREE CANARIES WERE SPENT ESTABLISHING A FACT ONE LOG LINE CARRIES. A failing
#    extraction used to leave no trace: the provider exception is discarded by design and
#    replaced with a coverage state, and nothing was logged. The only evidence that nine
#    attempts had failed was the arithmetic `logical_attempts=9` against `units=3`, which
#    a reader has to compute before they can even suspect a problem. Silence read as calm.
#
#    The ratified scope is narrow and these rows hold it there: `unit_index`,
#    `attempt_ordinal`, and an `error_code`. Never the prompt, the response, a quote,
#    narration text, a tenant id, a credential, or a raw exception. The exception's
#    identity stays discarded — the line names WHERE and a CLOSED CLASS, never WHY in the
#    provider's own words.
#
#    `error_code` is a COVERAGE_* constant for TIMEOUT/PROVIDER_FAILURE — those were
#    already specific — and, for a parser-side rejection, one of `_l2.EXTRACT_REASON_*`
#    instead of the single coarse `INVALID_EXTRACTOR_OUTPUT` every cause used to share
#    regardless of which rule actually rejected it (2026-08-13, same-day audit finding —
#    rows 8.19c/8.19d below). Still exactly three fields, still a closed vocabulary either
#    way: narrower, not wider.

def test_8_19_a_failing_attempt_logs_exactly_one_bounded_line(caplog):
    import logging

    secret = "PROVIDER-INTERNAL-TRACE-zzz999"

    async def _raising_provider(request):
        raise RuntimeError(secret)

    snap = _snapshot()
    canon = _authoritative_canon()
    with caplog.at_level(logging.WARNING, logger="canon-lite-extractor"):
        run = _run(ext.extract_all(
            snap, canon, provider=_raising_provider,
            model_version=qc.QC_MODEL_UPSTREAM, prompt_sha256=qc.PROMPT_SHA256,
            max_concurrency=1))

    lines = [r for r in caplog.records if r.name == "canon-lite-extractor"]
    assert lines, "a failing extraction still leaves no trace — the whole point of this"
    # one per failed attempt, not one per wave and not one per unit
    assert len(lines) == run.logical_attempts

    for rec in lines:
        msg = rec.getMessage()
        assert "unit_index=" in msg and "attempt_ordinal=" in msg
        assert "error_code=" in msg
        code = msg.split("error_code=")[1].rstrip(")")
        assert code in l2.COVERAGE_STATES, f"error_code {code!r} is not a closed constant"
        # nothing the provider, the model or the book said may appear
        assert secret not in msg
        assert "RuntimeError" not in msg
        for leak in ("Ratna", "Rina", "Bab", "pergi", "pulang"):
            assert leak not in msg


def test_8_19c_an_invalid_atom_address_names_the_specific_rule(caplog):
    """The gap §8.19 left open: this parser-side rejection used to log the same coarse
    `INVALID_EXTRACTOR_OUTPUT` as every other one. It now names WHICH rule — still without
    the address itself, or the exception, ever reaching the line."""
    import logging

    async def _invalid_address(request):
        return {
            "coverage": {**{p: "NO_CLAIMS_FOUND" for p in l2.SEMANTIC_PREDICATES},
                        l2.PREDICATE_ENTITY_NAME: "CHECKED"},
            "claims": [{"claim_type": l2.CLAIM_ENTITY_MENTION,
                       "canon_ref": "e1", "atom_start": 9999, "atom_end": 9999}],
        }

    snap = l2.materialize_final_snapshot({"book": "## Bab 1\nRatna pergi pagi"})
    canon = _authoritative_canon()
    with caplog.at_level(logging.WARNING, logger="canon-lite-extractor"):
        _run(ext.extract_all(
            snap, canon, provider=_invalid_address, model_version=qc.QC_MODEL_UPSTREAM,
            prompt_sha256=qc.PROMPT_SHA256, max_concurrency=1, max_attempts=1))

    lines = [r for r in caplog.records if r.name == "canon-lite-extractor"]
    assert len(lines) == 1
    msg = lines[0].getMessage()
    code = msg.split("error_code=")[1].rstrip(")")
    assert code == l2.EXTRACT_REASON_ATOM_INDEX_OUT_OF_RANGE
    assert code != l2.COVERAGE_INVALID_EXTRACTOR_OUTPUT, (
        "narrowed code must not just repeat the coarse coverage constant")
    assert "9999" not in msg


def test_8_19d_a_canon_ref_rejection_differs_from_an_address_rejection(caplog):
    """🔴 THE ACTUAL PROOF OF NON-COLLAPSE. Same closed vocabulary, a genuinely different
    cause — §8.19c alone could pass even if every rejection mapped to one hardcoded
    constant; this row is what makes that a lie."""
    import logging

    async def _unknown_canon_ref(request):
        ratna = next(a.index for a in request.chapter_atoms if a.text == "Ratna")
        return {
            "coverage": {**{p: "NO_CLAIMS_FOUND" for p in l2.SEMANTIC_PREDICATES},
                        l2.PREDICATE_ENTITY_NAME: "CHECKED"},
            "claims": [{"claim_type": l2.CLAIM_ENTITY_MENTION,
                       "canon_ref": "not_a_real_canon_id",
                       "atom_start": ratna, "atom_end": ratna}],
        }

    snap = l2.materialize_final_snapshot({"book": "## Bab 1\nRatna pergi pagi"})
    canon = _authoritative_canon()
    with caplog.at_level(logging.WARNING, logger="canon-lite-extractor"):
        _run(ext.extract_all(
            snap, canon, provider=_unknown_canon_ref, model_version=qc.QC_MODEL_UPSTREAM,
            prompt_sha256=qc.PROMPT_SHA256, max_concurrency=1, max_attempts=1))

    lines = [r for r in caplog.records if r.name == "canon-lite-extractor"]
    assert len(lines) == 1
    msg = lines[0].getMessage()
    code = msg.split("error_code=")[1].rstrip(")")
    assert code == l2.EXTRACT_REASON_CANON_REF_INVALID
    assert code != l2.EXTRACT_REASON_ATOM_INDEX_OUT_OF_RANGE, (
        "two distinct parser-side rejections collapsed into the same error_code")
    assert "not_a_real_canon_id" not in msg


def test_8_19e_provider_code_survives_without_provider_text(caplog):
    """The adapter already minted a closed code; the extractor must not flatten it."""
    import logging

    secret = "RAW-PROVIDER-BODY-MUST-NOT-APPEAR"

    async def _unparseable(request):
        exc = qc.QcProviderError("qc_provider_unparseable")
        exc.provider_body = secret
        raise exc

    with caplog.at_level(logging.WARNING, logger="canon-lite-extractor"):
        run = _run(ext.extract_all(
            _snapshot(), _authoritative_canon(), provider=_unparseable,
            model_version=qc.QC_MODEL_UPSTREAM, prompt_sha256=qc.PROMPT_SHA256,
            max_concurrency=1, max_attempts=1))

    assert all(a.coverage_state == l2.COVERAGE_PROVIDER_FAILURE for a in run.claims)
    lines = [r.getMessage() for r in caplog.records
             if r.name == "canon-lite-extractor"]
    assert lines and all("error_code=qc_provider_unparseable" in line for line in lines)
    assert all(secret not in line for line in lines)


def test_8_19f_address_retry_changes_request_bytes_and_becomes_measured():
    """Attempt 2 receives bounded feedback and can recover a rejected first response."""
    canon = _canon(
        n_chapters=1,
        entities=(cl.CanonEntityV1(
            entity_id="e1", canonical_name="Ratna", aliases=(), alias_source="none"),),
    )
    snapshot = l2.materialize_final_snapshot(
        {"book": "## Bab 1\nRatna pergi pagi"}, canon=canon)
    seen = []

    async def _retrying_provider(request):
        seen.append(request)
        if request.attempt == 1:
            return {
                "coverage": {
                    **{p: "NO_CLAIMS_FOUND" for p in l2.SEMANTIC_PREDICATES},
                    l2.PREDICATE_ENTITY_NAME: "CHECKED",
                },
                "claims": [{"claim_type": l2.CLAIM_ENTITY_MENTION,
                            "canon_ref": "e1", "atom_start": 9999,
                            "atom_end": 9999}],
            }
        return {"coverage": {p: "NO_CLAIMS_FOUND"
                             for p in l2.SEMANTIC_PREDICATES}, "claims": []}

    run = _run(ext.extract_all(
        snapshot, canon, provider=_retrying_provider,
        model_version=qc.QC_MODEL_UPSTREAM, prompt_sha256=qc.PROMPT_SHA256,
        max_concurrency=1))

    assert run.logical_attempts == 2
    assert run.claims[0].measured is True
    assert [r.retry_reason for r in seen] == [
        qcc.QC_RETRY_REASON_NONE,
        qcc.QC_RETRY_REASON_ADDRESS_INVALID,
    ]
    request_bytes = [qc.build_user_content(r).encode("utf-8") for r in seen]
    assert request_bytes[0] != request_bytes[1]
    assert json.loads(request_bytes[1])["retry_reason"] == "address_invalid"


def test_8_19b_a_successful_extraction_logs_nothing(caplog):
    """The line marks failure. A clean wave that narrated its own success would train
    every reader to skim past it, which is how the next silent fault gets missed."""
    import logging

    snap = _snapshot()
    canon = _authoritative_canon()
    with caplog.at_level(logging.WARNING, logger="canon-lite-extractor"):
        _run(ext.extract_all(
            snap, canon, provider=_ok_provider(), model_version=qc.QC_MODEL_UPSTREAM,
            prompt_sha256=qc.PROMPT_SHA256, max_concurrency=1))

    assert [r for r in caplog.records if r.name == "canon-lite-extractor"] == []
