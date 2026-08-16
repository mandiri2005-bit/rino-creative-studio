#!/usr/bin/env python3
"""Shared isolation contract for every mutation harness in this directory.

🔴 WHY THIS EXISTS. A harness restores its source byte-identically and reports the tree
   clean — and the interpreter still runs the MUTANT. CPython keys its bytecode cache on
   (source size, source mtime); a replacement of the SAME LENGTH written inside one
   mtime tick leaves both unchanged, so the `.pyc` compiled from the mutated source
   survives. Two consequences, both observed for real here: a mutant scored SURVIVED
   while never executing, and a later run importing a poisoned module (`v1` from cache,
   `v2` in source) with `git diff` clean and `shasum` matching.

🔴 A SHARED PREFIX RE-CREATES THE BUG IT WAS ADDED TO FIX. The first version of this
   module made ONE cache root for the whole process. `PYTHONDONTWRITEBYTECODE` can be
   defeated by a `sys.dont_write_bytecode = False` inside a conftest — which is exactly
   the case worth defending against — and then the BASELINE run populates the shared
   prefix and the same-length mutant reads the baseline's bytecode. Every subprocess
   therefore gets its OWN empty prefix, torn down afterwards: two runs can never share
   a cache even when the flag is defeated.

🔴 ONE MECHANISM. `_drop_bytecode()`, `rmtree(__pycache__)` and mtime bumping were three
   partial defences that each hid the others' gaps — this workstream's recurring "two
   mechanisms for one rule" failure. They are replaced by this module, not joined by it.

Contract:

    from _harness import isolated_run, restore_guard, bytecode_snapshot, assert_bytecode_unchanged

    with isolated_run() as env:                 # fresh prefix, removed on exit
        proc = subprocess.run([...], cwd=WT, env=env, ...)

    with restore_guard(path) as (original,):    # bytes + mode + mtime_ns
        path.write_bytes(mutated)               # restore + SHA verify on exit

Failures to isolate or restore RAISE. A harness that cannot guarantee its own isolation
must fail loudly rather than report a score it cannot stand behind.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import pathlib
import shutil
import tempfile

__all__ = ["isolated_run", "restore_guard", "bytecode_snapshot",
           "assert_bytecode_unchanged", "purge_repo_bytecode", "IsolationError"]


class IsolationError(RuntimeError):
    """Isolation or restoration could not be guaranteed. Never caught-and-ignored."""


@contextlib.contextmanager
def isolated_run(base: dict | None = None):
    """Yield an environment for ONE subprocess, with its own empty bytecode cache.

    A fresh prefix per call is the whole point: a prefix reused across the baseline and
    the mutated run lets the second read what the first compiled, and a same-length
    mutation is invisible to CPython's (size, mtime) cache key. The directory is removed
    on exit, so nothing accumulates and nothing is shared.

    `PYTHONDONTWRITEBYTECODE` is set as well. The two fail differently and neither is
    redundant: the flag can be overridden from inside the interpreter, and the prefix
    cannot stop an ALREADY-EXISTING `.pyc` from being read — only a fresh one can."""
    try:
        root = pathlib.Path(tempfile.mkdtemp(prefix="prove-pycache-"))
    except Exception as exc:  # noqa: BLE001
        raise IsolationError(f"could not create a bytecode cache root: {exc}") from exc
    try:
        if any(root.iterdir()):
            raise IsolationError(f"bytecode cache root is not empty: {root}")
        env = dict(os.environ if base is None else base)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPYCACHEPREFIX"] = str(root)
        yield env
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _meta(path: pathlib.Path) -> tuple:
    info = path.stat()
    return (hashlib.sha256(path.read_bytes()).hexdigest(), info.st_size,
            info.st_mtime_ns, info.st_mode)


@contextlib.contextmanager
def restore_guard(*paths: pathlib.Path):
    """Snapshot source bytes, MODE and nanosecond mtime; restore and verify on exit.

    The first version claimed "bytes + metadata" but restored only float atime/mtime, so
    a mutation that widened a `0600` file to `0644` left it `0644`. What is restored is
    exactly what is verified here — bytes, mode and `st_mtime_ns` — and nothing more is
    claimed."""
    paths = tuple(pathlib.Path(p) for p in paths)
    snapshots: dict = {}
    for path in paths:
        if not path.exists():
            raise IsolationError(f"source missing before mutation: {path}")
        snapshots[path] = (path.read_bytes(), _meta(path))
    try:
        yield tuple(snapshots[p][0] for p in paths)
    finally:
        problems = []
        for path, (raw, (digest, size, mtime_ns, mode)) in snapshots.items():
            try:
                path.write_bytes(raw)
                os.chmod(path, mode & 0o7777)
                os.utime(path, ns=(mtime_ns, mtime_ns))
                now_digest, now_size, now_mtime, now_mode = _meta(path)
                if now_digest != digest:
                    problems.append(f"{path}: sha {now_digest[:12]} != {digest[:12]}")
                if now_size != size:
                    problems.append(f"{path}: size {now_size} != {size}")
                if now_mtime != mtime_ns:
                    problems.append(f"{path}: mtime_ns {now_mtime} != {mtime_ns}")
                if (now_mode & 0o7777) != (mode & 0o7777):
                    problems.append(f"{path}: mode {now_mode & 0o7777:o} != {mode & 0o7777:o}")
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{path}: {exc}")
        if problems:
            raise IsolationError("RESTORE FAILED — tree is now dirty: "
                                 + "; ".join(problems))


def bytecode_snapshot(root: pathlib.Path) -> dict:
    """Map every repo `.pyc` to (sha256, size, mtime_ns).

    A set of PATHS was not enough: a `.pyc` overwritten in place with different bytes
    keeps its path, so the set compares equal while the executable state has changed
    underneath. Content is what matters, so content is what is recorded."""
    out: dict = {}
    for pyc in pathlib.Path(root).rglob("*.pyc"):
        try:
            info = pyc.stat()
            out[str(pyc)] = (hashlib.sha256(pyc.read_bytes()).hexdigest(),
                             info.st_size, info.st_mtime_ns)
        except OSError:
            continue
    return out


def assert_bytecode_unchanged(root: pathlib.Path, before: dict) -> None:
    """Raise unless the repo's bytecode is byte-for-byte what it was."""
    after = bytecode_snapshot(root)
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in set(after) & set(before) if after[k][0] != before[k][0])
    if added or removed or changed:
        raise IsolationError(
            "repo bytecode changed despite isolation — "
            f"added={added[:3]} removed={removed[:3]} changed={changed[:3]}")


def purge_repo_bytecode(root: pathlib.Path, *, expect_marker: str = "python") -> int:
    """One-time cleanup of a checkout an OLDER harness already poisoned.

    🔴 A RECURSIVE DELETE NEEDS A PROVEN ROOT. This takes a path and removes trees under
    it, so it validates first and refuses otherwise: the root must resolve to a real
    directory, must not be `/` or the home directory, must look like this checkout
    (`expect_marker` present), and every target must still resolve INSIDE it after
    symlinks are followed — a `__pycache__` symlink pointing outside must not be
    followed into someone else's tree.

    Not part of the isolation contract: isolation prevents new poisoning, this removes
    what earlier runs left behind."""
    root = pathlib.Path(root).resolve(strict=True)
    if not root.is_dir():
        raise IsolationError(f"purge root is not a directory: {root}")
    if root == pathlib.Path(root.anchor) or root == pathlib.Path.home().resolve():
        raise IsolationError(f"refusing to purge a filesystem or home root: {root}")
    if len(root.parts) < 3:
        raise IsolationError(f"refusing to purge a top-level directory: {root}")
    if not (root / expect_marker).is_dir():
        raise IsolationError(
            f"{root} does not look like the expected checkout (no {expect_marker}/)")
    removed = 0
    for cache in sorted(root.rglob("__pycache__"), reverse=True):
        if cache.is_symlink():
            raise IsolationError(f"refusing to follow a symlinked cache: {cache}")
        resolved = cache.resolve()
        if root not in resolved.parents and resolved != root:
            raise IsolationError(f"cache escapes the checkout: {cache} -> {resolved}")
        try:
            shutil.rmtree(resolved)
            removed += 1
        except Exception as exc:  # noqa: BLE001
            raise IsolationError(f"could not purge {cache}: {exc}") from exc
    return removed
