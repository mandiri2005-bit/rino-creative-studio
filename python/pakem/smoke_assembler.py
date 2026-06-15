# -*- coding: utf-8 -*-
"""pakem.smoke_assembler — prove the cache-aware assembler's prefix is stable.

Builds N chapters of ONE style/job through pakem.assembler.compose() and asserts:

  1. The `system` block (the cacheable static prefix) is BYTE-FOR-BYTE identical
     across every chapter — this is the property the whole prefix-cache design
     hinges on. If chapter scope or prev_tail ever leaked into the prefix, this
     fails loudly.
  2. The cache_key is identical across chapters of the same job.
  3. The `user` block (dynamic) DIFFERS between chapters (scope/story-so-far move).
  4. The Anthropic-style cache_control hint is present on the system block.
  5. The budgeter trims a long prev_tail to fit the model's input budget.

Run:
    cd python && python3 -m pakem.smoke_assembler --style storytelling --chapters 3

Exit code 0 on success, non-zero on any failed assertion.
"""
from __future__ import annotations

import argparse
import hashlib
import sys

from .assembler import Chapter, compose
from .resolvers import resolve_style_key


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _fake_outline(n: int) -> list[dict]:
    return [
        {
            "title": f"Chapter {i + 1}: The Turning",
            "summary": f"What changes in part {i + 1}, and who it changes.",
            "word_target": 800,
        }
        for i in range(n)
    ]


def _fake_passages(i: int) -> list[dict]:
    # Per-chapter passages — DIFFERENT each chapter, must live in the user block.
    return [
        {"source": f"Gutenberg #{1000 + i}", "text": f"A retrieved reference snippet for chapter {i + 1}."},
        {"source": f"Gutenberg #{2000 + i}", "text": f"Another passage, only relevant to chapter {i + 1}."},
    ]


def run(style: str, chapters: int, language: str = "id", mode: str = "text") -> int:
    job_id = "smoke-job-0001"
    brief = (
        "A sweeping, intimate history told for the ear: warm but precise, moving "
        "from one human moment to the wide sweep of an age and back."
    )
    outline = _fake_outline(chapters)

    composed = []
    # A growing 'story so far' — long enough to force the budgeter to trim.
    story_so_far = ""
    long_para = ("The river had not always run this way. " * 80).strip()

    for i in range(chapters):
        ch = Chapter(
            id=f"ch{i + 1}",
            title=outline[i]["title"],
            summary=outline[i]["summary"],
            index=i,
            total=chapters,
            word_target=800,
        )
        # For chapter > 0, prev_tail carries everything written so far (oversized).
        prev_tail = story_so_far if i > 0 else ""
        # Pin a small explicit prev_tail budget so the budgeter's trim path is
        # exercised deterministically, independent of the worker model's (large)
        # output ceiling. 300 tokens << the growing story-so-far for chapter 2+.
        c = compose(
            style,
            language=language,
            mode=mode,
            outline=outline,
            brief=brief,
            chapter=ch,
            prev_tail=prev_tail,
            rag_passages=_fake_passages(i),
            job_id=job_id,
            prev_tail_token_budget=300,
        )
        composed.append(c)
        # grow the story so far so chapter 2+ overflows the budget
        story_so_far += f"\n\n[chapter {i + 1} body] {long_para}"

    # --- Assertions ---------------------------------------------------------
    errors: list[str] = []

    # (0) the system block must be messages[0] and user messages[1].
    for idx, c in enumerate(composed):
        if c.messages[0]["role"] != "system":
            errors.append(f"chapter {idx + 1}: messages[0] is not the system block")
        if c.messages[-1]["role"] != "user":
            errors.append(f"chapter {idx + 1}: last message is not the user block")

    # (1) byte-for-byte identical static prefix / system content across chapters.
    prefixes = [c.static_prefix for c in composed]
    sys_contents = [c.messages[0]["content"] for c in composed]
    base = prefixes[0]
    base_sys = sys_contents[0]
    for idx, (p, s) in enumerate(zip(prefixes, sys_contents)):
        if p != base:
            errors.append(
                f"STATIC PREFIX DRIFT at chapter {idx + 1}: "
                f"sha {_sha(p)[:12]} != {_sha(base)[:12]}"
            )
        if s != base_sys:
            errors.append(f"SYSTEM CONTENT DRIFT at chapter {idx + 1}")
        if s != p:
            errors.append(f"chapter {idx + 1}: system content != static_prefix")

    # (2) identical cache_key across chapters of the same job.
    keys = {c.cache_key for c in composed}
    if len(keys) != 1:
        errors.append(f"CACHE KEY NOT STABLE across chapters: {keys}")

    # (3) dynamic blocks DIFFER between chapters.
    dyn = [c.dynamic_block for c in composed]
    if len(set(dyn)) != len(dyn):
        errors.append("dynamic blocks are NOT all distinct (scope/story-so-far did not vary)")

    # (4) cache_control hint present on the system block.
    if composed[0].messages[0].get("cache_control") != {"type": "ephemeral"}:
        errors.append("cache_control ephemeral hint missing on system block")

    # (5) budgeter trimmed the oversized prev_tail on a later chapter.
    if chapters >= 2 and not any(c.prev_tail_trimmed for c in composed[1:]):
        errors.append("budgeter did NOT trim an oversized prev_tail (expected a trim)")

    # --- Report -------------------------------------------------------------
    skey = resolve_style_key(style)
    print(f"style='{style}' -> resolved key '{skey}'  |  chapters={chapters}  lang={language}  mode={mode}")
    print(f"model (budgeter)   : {composed[0].model}  (output ceiling {composed[0].max_tokens} tok)")
    print(f"static prefix sha  : {_sha(base)[:16]}  ({composed[0].prefix_tokens} est tokens, {len(base)} bytes)")
    print(f"cache_key          : {composed[0].cache_key}")
    print("per-chapter dynamic tokens / prev_tail_trimmed:")
    for i, c in enumerate(composed):
        print(f"   ch{i + 1}: dyn={c.dynamic_tokens:>5} tok  input={c.input_tokens:>5} tok  trimmed={c.prev_tail_trimmed}")

    if errors:
        print("\nFAILED:")
        for e in errors:
            print("  - " + e)
        return 1

    print(f"\nOK: system prefix byte-for-byte identical across {chapters} chapters; "
          f"cache_key stable; dynamic blocks distinct; budgeter trims prev_tail.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Smoke-test the cache-aware assembler.")
    ap.add_argument("--style", default="storytelling", help="narration style (raw user string)")
    ap.add_argument("--chapters", type=int, default=3, help="number of chapters to build")
    ap.add_argument("--language", default="id", help="language code/label")
    ap.add_argument("--mode", default="text", help="text | video (VO mode)")
    args = ap.parse_args(argv)
    return run(args.style, max(1, args.chapters), args.language, args.mode)


if __name__ == "__main__":
    sys.exit(main())
