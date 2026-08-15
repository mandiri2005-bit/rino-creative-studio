"""F4a — the targeted non-delivery probe artifact must be bounded and honest.

The artifact is the durable audit record that replaces "there is a comment saying we
once probed this". Two things therefore have to be true of it, and both are pinned here:
it must be SANITIZED (no raw token, no prose, no prompt, no response, no external id),
and it must be unable to CLAIM more than it is — an offline dry run says
`offline_dry_run`, and only a run that actually reached a provider may say `live`.
"""
import importlib
import json
import os
import pathlib
import sys

import pytest
from types import SimpleNamespace

_REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "python"))
sys.path.insert(0, str(_REPO / "python" / "tools"))

probe = importlib.import_module("narasi_f4a_probe")


_HEX64 = 64
_TOP_KEYS = {
    "schema_version", "probe_kind", "recording_mode",
    "primary_response_observed", "executed_at_utc", "source", "provider",
    "delivery", "counts", "cost", "observation",
}


@pytest.fixture
def artifact(tmp_path):
    path = probe.run_probe(allow_network=False, out_dir=tmp_path,
                           now_utc="2026-08-15T00:00:00Z")
    return json.loads(path.read_text(encoding="utf-8")), path


def test_the_artifact_has_exactly_the_declared_keys(artifact):
    doc, _path = artifact
    assert set(doc) == _TOP_KEYS
    assert doc["schema_version"] == probe.ARTIFACT_SCHEMA_VERSION
    assert doc["probe_kind"] == "targeted_non_delivery"
    assert set(doc["source"]) == {
        "head", "laozhang_api_sha256", "narasi_addressed_patch_sha256"}
    assert set(doc["provider"]) == {"phase", "model_alias", "resolved_model",
                                   "served_by", "served_model"}
    assert set(doc["delivery"]) == {"persisted", "returned_to_user"}
    assert set(doc["counts"]) == {
        "chapters_targeted", "chapters_attempted",
        "provider_calls_reported", "provider_calls_observed",
        "adapter_invocations_observed", "upstream_request_cap"}
    assert set(doc["cost"]) == {
        "prompt_tokens", "completion_tokens", "total_tokens",
        "estimated_usd_micros", "estimator", "credit_row_written"}
    assert set(doc["observation"]) == {
        "validator_outcome", "operation_source", "subtype",
        "token_length", "token_sha256"}


def test_every_enum_is_closed(artifact):
    doc, _path = artifact
    assert doc["recording_mode"] in probe.RECORDING_MODES
    assert doc["observation"]["validator_outcome"] in probe.VALIDATOR_OUTCOMES
    assert doc["observation"]["operation_source"] in probe.OPERATION_SOURCES
    assert doc["observation"]["subtype"] in probe.SUBTYPES
    assert doc["provider"]["phase"] == "canon_diff_revise"


def test_hashes_are_lowercase_hex_of_the_right_width(artifact):
    doc, _path = artifact
    for value in (doc["source"]["laozhang_api_sha256"],
                  doc["source"]["narasi_addressed_patch_sha256"],
                  doc["observation"]["token_sha256"]):
        assert isinstance(value, str) and len(value) == _HEX64
        assert value == value.lower()
        assert all(c in "0123456789abcdef" for c in value)


def test_counts_are_bounded_integers_and_the_two_counters_agree(artifact):
    doc, _path = artifact
    counts = doc["counts"]
    for key, value in counts.items():
        assert type(value) is int and value >= 0, key
    assert counts["chapters_attempted"] <= counts["chapters_targeted"]
    assert counts["provider_calls_reported"] == counts["provider_calls_observed"], (
        "the production counter and the probe's own independent observation disagree — "
        "that mismatch IS the finding, never something to paper over")
    assert type(doc["observation"]["token_length"]) is int
    assert doc["observation"]["token_length"] >= 0


def test_the_probe_records_non_delivery(artifact):
    doc, _path = artifact
    assert doc["delivery"] == {"persisted": False, "returned_to_user": False}


def test_an_offline_run_may_not_claim_to_be_live_evidence(artifact):
    doc, _path = artifact
    assert doc["recording_mode"] == "offline_dry_run"
    assert doc["primary_response_observed"] is False


def _walk(node, path="$"):
    if isinstance(node, dict):
        for key, value in node.items():
            yield path, key, None
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _walk(value, f"{path}[{i}]")
    else:
        yield path, None, node


def test_no_forbidden_key_or_raw_content_anywhere_in_the_artifact(artifact):
    """Recursive, because a leak added later will not be added at the top level."""
    doc, path = artifact
    forbidden_keys = {
        "prompt", "system", "user", "messages", "response", "raw", "raw_response",
        "content", "text", "chapter", "manuscript", "operation", "operations",
        "received", "token", "api_key", "authorization", "request_id", "id",
        "exception", "traceback", "prose",
    }
    for _p, key, _value in _walk(doc):
        if key is not None:
            assert key not in forbidden_keys, f"forbidden key in artifact: {key}"

    blob = path.read_text(encoding="utf-8")
    # The synthetic chapter's own prose, the raw verb, and the JSON envelope must all
    # be absent — the probe may report ABOUT them, never reproduce them.
    for leak in ("Mira membawa", "Deposisinya", "delete_paragraph",
                 "narasi_addressed_patch_v1", "unit_id", "EXACT OUTLINE"):
        assert leak not in blob, f"raw content leaked into the artifact: {leak!r}"


def test_the_artifact_is_never_silently_overwritten(tmp_path):
    kwargs = dict(allow_network=False, out_dir=tmp_path, now_utc="2026-08-15T00:00:00Z")
    first = probe.run_probe(**kwargs)
    assert first.exists()
    with pytest.raises(FileExistsError):
        probe.run_probe(**kwargs)


def test_network_is_refused_without_the_explicit_confirmation(monkeypatch, tmp_path):
    """Two independent gates, because one flag is too easy to pass by accident."""
    monkeypatch.delenv("NARASI_F4A_PROBE_CONFIRM", raising=False)
    rc = probe.main(["--allow-network", "--out-dir", str(tmp_path)])
    assert rc == 2
    assert list(tmp_path.iterdir()) == [], "a refused probe must write nothing"


def test_cost_is_recorded_from_the_response_without_a_ledger_write(artifact):
    """§4 of the reject: 'no DB write' was satisfied, 'the cost was counted' was not.
    Usage comes off the response the probe proxied; the price comes from the same PURE
    estimator production uses (`_calc_cost`), which writes nothing."""
    doc, _path = artifact
    cost = doc["cost"]
    for key in ("prompt_tokens", "completion_tokens", "total_tokens",
                "estimated_usd_micros"):
        assert type(cost[key]) is int and cost[key] >= 0, key
    assert cost["total_tokens"] == cost["prompt_tokens"] + cost["completion_tokens"]
    # Exact, so a mutant that stops reading usage off the response cannot hide behind
    # a fixture that was zero anyway.
    assert (cost["prompt_tokens"], cost["completion_tokens"]) == (120, 45)
    assert cost["total_tokens"] == 165
    assert cost["estimator"] == "laozhang_api._calc_cost"
    assert cost["credit_row_written"] is False


def _failed_network_run(monkeypatch, tmp_path):
    """A PAID run that never reached the provider: the client factory raises."""
    import laozhang_api as lz

    def _dead_factory(*_a, **_k):
        raise RuntimeError("no credential")

    monkeypatch.setattr(lz, "make_narasi_client", _dead_factory)
    path = probe.run_probe(allow_network=True, out_dir=tmp_path,
                           now_utc="2026-08-15T00:00:00Z", model_alias="opus-4-6")
    return json.loads(path.read_text(encoding="utf-8")), path


def test_a_paid_run_that_never_reached_the_provider_is_not_live_evidence(
        monkeypatch, tmp_path):
    """🔴 THE EVIDENCE-LAUNDERING BLOCKER. `live` used to follow from `--allow-network`
    alone, so this artifact — zero calls entered, zero observed, no response — was
    filed as live evidence closing the DoD."""
    doc, path = _failed_network_run(monkeypatch, tmp_path)

    assert doc["counts"]["provider_calls_observed"] == 0
    assert doc["recording_mode"] == "failed_probe"
    assert doc["primary_response_observed"] is False
    assert "failed-probe" in path.name, (
        "a failed probe must be distinguishable from the live artifact by filename too")


def test_a_network_run_refuses_a_test_model_alias(tmp_path):
    with pytest.raises(ValueError, match="explicit production model"):
        probe.run_probe(allow_network=True, out_dir=tmp_path,
                        now_utc="2026-08-15T00:00:00Z", model_alias="test-model")
    assert list(tmp_path.iterdir()) == []


def test_the_cli_refuses_a_test_model_alias_for_network_runs(monkeypatch, tmp_path):
    monkeypatch.setenv("NARASI_F4A_PROBE_CONFIRM", "1")
    rc = probe.main(["--allow-network", "--out-dir", str(tmp_path)])
    assert rc == 2, "the default alias must not be usable for a paid run"
    assert list(tmp_path.iterdir()) == []


def test_live_requires_the_two_counters_to_agree(monkeypatch, tmp_path):
    """Even with a real response, a disagreement between the production counter and the
    probe's independent observation must NOT be filed as live evidence — that mismatch
    is the finding, not a detail to round away."""
    import laozhang_api as lz

    real = lz._narasi_structural_patch_revise

    async def _miscounting(*a, **k):
        revised, credits, stats = await real(*a, **k)
        stats = dict(stats)
        stats["provider_calls"] = stats["provider_calls"] + 1   # disagree on purpose
        return revised, credits, stats

    monkeypatch.setattr(lz, "_narasi_structural_patch_revise", _miscounting)
    path = probe.run_probe(allow_network=False, out_dir=tmp_path,
                           now_utc="2026-08-15T00:00:00Z")
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["counts"]["provider_calls_reported"] != doc["counts"]["provider_calls_observed"]
    assert doc["recording_mode"] != "live"


def _failover_probe(monkeypatch, tmp_path, *, upstream_failures=1, cap=2):
    """A network-mode probe whose TRANSPORTS are stubbed — the real failover client
    runs, no packet leaves the process."""
    import laozhang_api as lz

    seen = {"n": 0}
    patch = json.dumps({"schema_version": "narasi_addressed_patch_v1",
                        "operations": [{"op": "replace", "unit_id": "u002",
                                        "text": "Ia menyerahkan seluruh haknya sebelum "
                                                "rekaman diputar."}]})

    def _transport(_endpoint, _key, model_id, _messages, _max_tokens, _timeout,
                   temperature=None, _reserve=None):
        if _reserve is not None and not _reserve():
            raise lz._NarasiUpstreamRefused("upstream refused")
        seen["n"] += 1
        if seen["n"] <= upstream_failures:
            raise RuntimeError("rung down")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=patch),
                                     finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))

    monkeypatch.setattr(lz, "_anthropic_messages_create", _transport)
    monkeypatch.setattr(lz, "_narasi_failover_chain", lambda *_a, **_k: [
        (name, "anthropic", f"https://{name}.invalid/v1/messages", "k", "served-model-x")
        for name in ("rung_a", "rung_b")])
    monkeypatch.setattr(lz, "make_narasi_client", lambda *_a, **_k:
                        lz._NarasiFailoverClient(model="opus-4-6", role="",
                                                 phase="canon_diff_revise"))
    path = probe.run_probe(allow_network=True, out_dir=tmp_path,
                           now_utc="2026-08-15T00:00:00Z", model_alias="opus-4-6",
                           upstream_cap=cap)
    return json.loads(path.read_text(encoding="utf-8")), seen


def test_the_probe_observes_the_upstream_boundary_not_the_adapter(monkeypatch, tmp_path):
    """🔴 TWO COUNTERS ON THE SAME BOUNDARY COMPARE A NUMBER WITH ITSELF. The probe used
    to wrap the outer `.create()` — exactly where the production counter sat — so both
    read 1 while the failover chain had issued two upstream requests. The independent
    observation now sits on the transports the rung loop calls."""
    doc, seen = _failover_probe(monkeypatch, tmp_path)

    assert seen["n"] == 2, "the fixture must exercise a second rung"
    assert doc["counts"]["provider_calls_observed"] == 2
    assert doc["counts"]["adapter_invocations_observed"] == 1, (
        "one adapter invocation, two upstream requests — the whole point")
    assert doc["counts"]["provider_calls_reported"] == 2
    assert doc["recording_mode"] == "live"


def test_the_artifact_records_who_actually_served_it(monkeypatch, tmp_path):
    """"SERVED via vertex" belonged in a log line nobody auditing the artifact can see.
    The rung and the model it really sent are part of the record now — and the price is
    computed from the SERVED model, not the alias the caller asked for."""
    doc, _seen = _failover_probe(monkeypatch, tmp_path)

    # `rung_a`, not `rung_b`: NARASI_RUNG_ATTEMPTS is 2, so the first rung is retried
    # and serves on its second try — two upstream requests inside one rung, which is
    # precisely the shape an adapter-level counter cannot see.
    assert doc["provider"]["served_by"] == "rung_a"
    assert doc["provider"]["served_model"] == "served-model-x"
    assert doc["provider"]["model_alias"] == "opus-4-6"
    assert doc["cost"]["prompt_tokens"] == 10 and doc["cost"]["completion_tokens"] == 5


def test_the_offline_run_reproduces_the_v9_unknown_operation_shape(artifact):
    """The branch the probe exists to observe is actually the one it walked."""
    doc, _path = artifact
    assert doc["observation"]["validator_outcome"] == "unknown_operation"
    assert doc["observation"]["operation_source"] == "op_field"
    assert doc["observation"]["subtype"] == "delete"
    assert doc["counts"]["provider_calls_reported"] == 1


def test_the_schema_version_moved_with_the_shape(tmp_path):
    """🔴 A SHAPE CHANGE UNDER AN UNCHANGED VERSION. The v1 artifact from the live run
    and the artifact this runner writes today disagree on their own key set — one
    carries `primary_raw_evidence_available`, the other `primary_response_observed` —
    yet both claimed v1. A consumer had no way to tell them apart."""
    assert probe.ARTIFACT_SCHEMA_VERSION == "narasi_f4a_probe_artifact_v2"
    doc = json.loads(probe.run_probe(
        allow_network=False, out_dir=tmp_path,
        now_utc="2026-08-15T00:00:00Z").read_text(encoding="utf-8"))
    assert doc["schema_version"] == "narasi_f4a_probe_artifact_v2"
    assert "primary_raw_evidence_available" not in doc
    assert "primary_response_observed" in doc


def test_the_live_v1_artifact_is_left_untouched():
    """The 2026-08-15 live record predates the rework. It stays v1 — rewriting it would
    be back-dating evidence to a shape it was never produced under."""
    live = _REPO / "docs/audit/narasi/f4a-targeted-nondelivery-probe-2026-08-15T112926Z.json"
    assert live.exists(), (
        "the live probe artifact is REQUIRED packaging evidence — skipping when it is "
        "missing let a commit that dropped it stay green")
    doc = json.loads(live.read_text(encoding="utf-8"))
    assert doc["schema_version"] == "narasi_f4a_probe_artifact_v1"
    assert "primary_raw_evidence_available" in doc


def test_the_upstream_cap_is_enforced_not_promised(monkeypatch, tmp_path):
    """"One paid call" was help text while one adapter invocation could retry into
    several billable requests. The cap makes the sentence true."""
    doc, seen = _failover_probe(monkeypatch, tmp_path, upstream_failures=5, cap=1)
    assert seen["n"] == 1, "the cap must stop the failover chain spending a second one"
    assert doc["counts"]["upstream_request_cap"] == 1
    # 🔴 THE INVARIANT THE FIRST CAP BROKE: actual == observed == reported. The cap used
    #    to live in the probe's wrapper, refusing requests 2-4 only AFTER the production
    #    counter had already counted them — actual 1, reported 4.
    assert doc["counts"]["provider_calls_observed"] == 1
    assert doc["counts"]["provider_calls_reported"] == 1
    assert doc["recording_mode"] == "failed_probe"


def test_a_probe_with_no_transport_observer_is_not_live(monkeypatch, tmp_path):
    """🔴 NO FALLBACK. `upstream = observed or entered` substituted the ADAPTER count
    whenever the transport observer never fired — manufacturing the "independent"
    observation out of the very number it exists to check. Here the observer is
    inactive (a rung whose transport this probe does not wrap) while the failover
    adapter is entered once: observed must stay 0 and the run must NOT be live."""
    import laozhang_api as lz

    def _transport(_endpoint, _key, model_id, _messages, _max_tokens, _timeout,
                   temperature=None, _reserve=None):
        if _reserve is not None and not _reserve():
            raise lz._NarasiUpstreamRefused("upstream refused")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="{}"),
                                     finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    monkeypatch.setattr(lz, "_anthropic_messages_create", _transport)
    monkeypatch.setattr(lz, "_narasi_failover_chain", lambda *_a, **_k: [
        ("rung_a", "anthropic", "https://rung_a.invalid/v1/messages", "k", "m1")])
    monkeypatch.setattr(lz, "make_narasi_client", lambda *_a, **_k:
                        lz._NarasiFailoverClient(model="opus-4-6", role="",
                                                 phase="canon_diff_revise"))
    # The observer is switched off: this probe build wraps no transport at all.
    monkeypatch.setattr(probe, "_TRANSPORT_NAMES", (), raising=False)

    path = probe.run_probe(allow_network=True, out_dir=tmp_path,
                           now_utc="2026-08-15T00:00:00Z", model_alias="opus-4-6")
    doc = json.loads(path.read_text(encoding="utf-8"))

    assert doc["counts"]["adapter_invocations_observed"] == 1
    assert doc["counts"]["provider_calls_observed"] == 0, (
        "nothing was observed at a transport boundary")
    assert doc["recording_mode"] == "failed_probe", (
        "an unobserved run may not be filed as live evidence")
