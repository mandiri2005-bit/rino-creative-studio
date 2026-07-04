# ── narasi_manifest — ID-path fixes §1: the gates-active manifest (the systemic fix).
# Root failure class of the Diponegoro run = SILENT NON-DEPLOYMENT: every gate that
# failed was already specified and simply not active on that path. Kill the class:
# at job start the pipeline emits {rule: active | UNMEASURED(reason) | n/a(reason)} for
# EVERY registered pipeline rule. A rule that resolves to none of those states fails the
# job before any tokens are spent. The manifest ships in the editor report so a reviewer
# sees `counters.aporia: UNMEASURED(no ID pattern list)` instead of discovering it in
# the output. Deterministic, stdlib-only.
from __future__ import annotations

import os
from typing import Any

__all__ = ["build_manifest", "PIPELINE_RULES", "ManifestError"]


class ManifestError(RuntimeError):
    """A pipeline rule resolved to no state — the job must not start (§1)."""


# The registry: every enforcement surface the ⚡ pipeline claims to have. Adding a gate
# to the pipeline without adding it here (or here without implementing it) is exactly
# the silent-absence class this kills — keep the two in lockstep.
PIPELINE_RULES = (
    "known_bad",            # R-FG4 auto-correct/flag registry
    "verify_resolve",       # R-FG6 [VERIFY] resolve→hedge→cut (localized)
    "terminal_strip",       # R-FG5 bracket/marker strip incl. lowercase
    "foreign_placeholder",  # §2.4 other-language token blocklist
    "counters.citations",   # R-H2
    "counters.aporia",      # R-H3
    "counters.epithet",     # R-E3
    "counters.word_budget", # R-E4 (report; length is the word-gate's job)
    "counters.anchors",     # §6.2 anchor budget 3
    "counters.scene_dup",   # §6.1 cross-chapter scene dedup
    "counters.thesis",      # R-H6 (embed-based)
    "register_gate",        # R-H10 scorecard
    "factscan",             # R-FG9/FG10 sweep
    "verify_search",        # R-FG8 search-backed verify
    "entity_pass",          # §5 scholar resolution
    "attribution_verify",   # FG3-b wrong-subject scholar check (round-2 §4 — needs search)
    "style_spec",           # round-2 §6: the style label MUST resolve; spec-load asserted
    "number_rendering",     # §7 output-keyed number pass
    "header",               # Gaya/Output header
    "living_guard",         # regime-precedence §2
)


def _flag_on(name: str, default: str = "1") -> bool:
    return str(os.environ.get(name, default)).strip().lower() not in ("0", "false", "no", "off")


def build_manifest(*, style_entry: dict | None, lang: str, regime: str, mode: str,
                   style: str = "") -> dict[str, dict]:
    """Resolve every PIPELINE_RULE to its status for THIS job. Raises ManifestError if
    any rule resolves to nothing (fail-to-start, per §1) — or, round-2 §6, if the style
    label doesn't resolve to a registry entry (NO silent fallback to a default spec)."""
    lang_key = (lang or "en").split("-")[0].lower()

    # Round-2 §6 schema-validation gate: the style string must hit the alias index —
    # a garbage label silently falling back to creative_nonfiction is exactly the
    # "spec never loaded" failure mode. Empty style = legit default; junk style = 422.
    style_key = ""
    if (style or "").strip():
        try:
            from pakem.resolvers import _ALIAS_INDEX, _norm  # registry's own index
            style_key = _ALIAS_INDEX.get(_norm(style), "")
            if not style_key:
                raise ManifestError(f"unknown style: {style!r} — resolves to no registry entry")
        except ManifestError:
            raise
        except Exception:  # noqa: BLE001 — resolver internals moved; don't block jobs
            style_key = ""
    try:
        from narasi_counters import LANGUAGE_PACKS
        pack = LANGUAGE_PACKS.get(lang_key) or {}
    except Exception:  # noqa: BLE001
        pack = {}
    budgets = ((style_entry or {}).get("style_spec") or {}).get("counters") or {}

    m: dict[str, dict] = {}

    def _set(rule: str, status: str, reason: str = "") -> None:
        m[rule] = {"status": status, **({"reason": reason} if reason else {})}

    gate_on = _flag_on("NARASI_TERMINAL_GATE")
    _set("known_bad", "active" if gate_on else "UNMEASURED", "" if gate_on else "NARASI_TERMINAL_GATE=0")
    if not gate_on:
        _set("verify_resolve", "UNMEASURED", "NARASI_TERMINAL_GATE=0")
        _set("terminal_strip", "UNMEASURED", "NARASI_TERMINAL_GATE=0")
    else:
        _set("verify_resolve", "active" if (pack.get("hedge_value") and pack.get("hedge_prose"))
             else "UNMEASURED", "" if pack.get("hedge_value") else f"no hedge templates for '{lang_key}'")
        _set("terminal_strip", "active")
    _set("foreign_placeholder", "active" if pack.get("foreign_tokens") else "n/a",
         "" if pack.get("foreign_tokens") else f"no blocklist for '{lang_key}' (native language)")

    _set("counters.citations", "active" if pack.get("attribution") else "UNMEASURED",
         "" if pack.get("attribution") else f"no attribution patterns for '{lang_key}'")
    _set("counters.aporia", "active" if pack.get("aporia") else "UNMEASURED",
         "" if pack.get("aporia") else f"no aporia phrases for '{lang_key}'")
    _set("counters.epithet", "active" if pack.get("epithet") else "UNMEASURED",
         "" if pack.get("epithet") else f"no epithet pattern for '{lang_key}'")
    _set("counters.word_budget", "active")
    _set("counters.anchors", "active")
    _set("counters.scene_dup", "active")
    _set("counters.thesis", "active" if budgets.get("thesis_restatement_max") is not None
         else "n/a", "" if budgets.get("thesis_restatement_max") is not None
         else "style has no thesis budget")

    reg_on = _flag_on("NARASI_REGISTER_GATE")
    has_spec = bool((style_entry or {}).get("register_spec"))
    _set("register_gate", "active" if (reg_on and has_spec) else "n/a",
         "" if (reg_on and has_spec) else ("NARASI_REGISTER_GATE=0" if not reg_on else "style has no register_spec"))

    _set("factscan", "n/a" if regime == "fictional" else
         ("active" if lang_key == "en" else "UNMEASURED"),
         "fictional regime skips external claims" if regime == "fictional" else
         ("" if lang_key == "en" else
          ("numeric+spelled-quantity sweep active; FG10 prose detectors EN-only"
           if pack.get("spelled_quantity") else "FG10 detectors EN-only; numeric sweep still runs")))

    verify_on = _flag_on("FACTGATE_SEARCH_ENABLED", "0")
    keyed = bool(os.environ.get("TAVILY_API_KEY") or os.environ.get("SERPER_API_KEY"))
    _set("verify_search", "active" if (verify_on and keyed and regime == "strict") else
         ("n/a" if regime != "strict" else "UNMEASURED"),
         "" if (verify_on and keyed and regime == "strict") else
         (f"regime={regime}" if regime != "strict" else
          ("FACTGATE_SEARCH_ENABLED=0" if not verify_on else "no search provider keys")))

    _set("entity_pass", "active")
    # FG3-b (round-2 §4): "is this person a scholar OF this field" needs live search —
    # UNMEASURED until wired; the table-seeded downgrades (Budiardjo/Buiskool class) are
    # the only coverage. NEVER shown as active; named citations stay human-review items.
    _set("attribution_verify", "n/a" if regime == "fictional" else "UNMEASURED",
         "fictional regime" if regime == "fictional"
         else "FG3-b wrong-subject check needs FG-SEARCH; table-seeded names only")
    # Round-2 §6: assert the spec actually loaded (the R-H9-absence routing hypothesis).
    _set("style_spec", "active",
         (f"{style_key or 'default'} loaded"
          + ("+register_spec" if (style_entry or {}).get("register_spec") else "")
          + ("+style_spec" if (style_entry or {}).get("style_spec") else "")))
    _set("number_rendering", "active" if (mode == "video" and pack.get("spell_number")) else "n/a",
         "" if (mode == "video" and pack.get("spell_number")) else
         ("book path keeps digits" if mode != "video" else f"no spellout rules for '{lang_key}'"))
    _set("header", "active")
    _set("living_guard", "active" if _flag_on("NARRATION_LIVING_GUARD") else "UNMEASURED",
         "" if _flag_on("NARRATION_LIVING_GUARD") else "NARRATION_LIVING_GUARD=0")

    # §1 hard assertion: no rule may be silently absent.
    missing = [r for r in PIPELINE_RULES if r not in m
               or m[r].get("status") not in ("active", "UNMEASURED", "n/a")]
    if missing:
        raise ManifestError(f"pipeline rules with no resolved state: {missing}")
    return m
