"""The mutation harnesses' own isolation contract, pinned as tests.

🔴 A HARNESS THAT CANNOT PROVE ITS OWN ISOLATION PROVES NOTHING ELSE EITHER. Every
   mutation score in this repo rests on one assumption: the subprocess executed the
   source the harness had just written. CPython's bytecode cache keys on (size, mtime),
   so a SAME-LENGTH replacement written inside one mtime tick is invisible to it — the
   mutant is scored SURVIVED while never having run, and a later import can read the
   poisoned cache with `git diff` clean and `shasum` matching.

   Each test below is a defect that was found in the helper itself, in the order it was
   found. They are regression pins, not illustrations.
"""
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

_MUT = Path(__file__).resolve().parents[1] / "mutation"
sys.path.insert(0, str(_MUT))

from _harness import (  # noqa: E402
    IsolationError,
    assert_bytecode_unchanged,
    bytecode_snapshot,
    isolated_run,
    purge_repo_bytecode,
    restore_guard,
)

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def module(tmp_path):
    """A tiny importable module whose value a subprocess can print back."""
    path = tmp_path / "isolation_probe.py"
    path.write_text('VALUE = "original"\n', encoding="utf-8")
    return path


def _read_value(module_dir, env):
    proc = subprocess.run(
        [sys.executable, "-c",
         "import isolation_probe as m; print(m.VALUE)"],
        cwd=module_dir, env=env, capture_output=True, text=True)
    return proc.stdout.strip()


def _defeat_flag(module_dir):
    """A sitecustomize that turns `PYTHONDONTWRITEBYTECODE` back off — the exact case
    the isolation exists to survive, and the one the first helper failed."""
    (module_dir / "sitecustomize.py").write_text(
        "import sys\nsys.dont_write_bytecode = False\n", encoding="utf-8")


# ── P1: a prefix reused across runs re-creates the bug it was added to fix ──

def test_a_same_length_mutation_is_executed_not_read_from_a_baseline_cache(module):
    """🔴 THE FALSE SURVIVAL. With ONE shared cache prefix, the baseline run compiles and
    caches, and the same-length mutant then reads the baseline's bytecode: the harness
    reports SURVIVED for a mutant that never ran. A fresh prefix per subprocess is what
    makes the two runs incapable of sharing a cache — even when the no-bytecode flag has
    been switched back off from inside the interpreter."""
    directory = module.parent
    _defeat_flag(directory)

    with isolated_run() as env:
        assert _read_value(directory, env) == "original"

    original = module.read_bytes()
    mutated = original.replace(b'"original"', b'"mutated!"')
    assert len(mutated) == len(original), "the mutant must be the same length"

    with restore_guard(module):
        module.write_bytes(mutated)
        os.utime(module, ns=(os.stat(module).st_mtime_ns,) * 2)
        with isolated_run() as env:
            observed = _read_value(directory, env)

    assert observed == "mutated!", (
        "the subprocess read a cached baseline instead of the mutated source — every "
        "same-length mutant would be scored SURVIVED without ever executing")


def test_each_isolated_run_gets_its_own_prefix_and_removes_it():
    with isolated_run() as first:
        first_root = Path(first["PYTHONPYCACHEPREFIX"])
        assert first_root.is_dir() and not any(first_root.iterdir())
        with isolated_run() as second:
            assert second["PYTHONPYCACHEPREFIX"] != first["PYTHONPYCACHEPREFIX"]
        assert not Path(second["PYTHONPYCACHEPREFIX"]).exists()
    assert not first_root.exists(), "the cache root outlived its subprocess"
    assert first["PYTHONDONTWRITEBYTECODE"] == "1"


# ── P2: a set of paths cannot see a `.pyc` rewritten in place ──────────────

def test_bytecode_comparison_notices_a_pyc_overwritten_in_place(tmp_path):
    """🔴 SAME PATH, DIFFERENT BYTES. Comparing the SET of `.pyc` paths reports "no
    change" when a cache entry is overwritten with different content — which is exactly
    what a poisoned cache looks like."""
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    entry = cache / "m.cpython-99.pyc"
    entry.write_bytes(b"AAAA")

    before = bytecode_snapshot(tmp_path)
    assert_bytecode_unchanged(tmp_path, before)

    entry.write_bytes(b"BBBB")                      # same path, same size
    with pytest.raises(IsolationError, match="changed"):
        assert_bytecode_unchanged(tmp_path, before)


def test_bytecode_comparison_notices_an_added_entry(tmp_path):
    (tmp_path / "__pycache__").mkdir()
    before = bytecode_snapshot(tmp_path)
    (tmp_path / "__pycache__" / "new.cpython-99.pyc").write_bytes(b"x")
    with pytest.raises(IsolationError, match="added"):
        assert_bytecode_unchanged(tmp_path, before)


# ── P2: "bytes + metadata" must mean what it says ──────────────────────────

def test_the_restore_guard_puts_back_the_file_mode(module):
    """🔴 THE CLAIM WAS WIDER THAN THE CODE. It said "bytes + metadata" and restored
    only float atime/mtime, so a mutation that widened `0600` to `0644` left it `0644`."""
    os.chmod(module, 0o600)
    before_mode = os.stat(module).st_mode & 0o7777

    with restore_guard(module):
        module.write_bytes(b'VALUE = "mutated!"\n')
        os.chmod(module, 0o644)

    assert os.stat(module).st_mode & 0o7777 == before_mode == 0o600


def test_the_restore_guard_puts_back_bytes_and_nanosecond_mtime(module):
    before = module.read_bytes()
    before_ns = os.stat(module).st_mtime_ns

    with restore_guard(module):
        module.write_bytes(b'VALUE = "mutated!"\n')

    assert module.read_bytes() == before
    assert os.stat(module).st_mtime_ns == before_ns
    assert hashlib.sha256(module.read_bytes()).hexdigest() == \
        hashlib.sha256(before).hexdigest()


def test_a_missing_source_raises_instead_of_passing_quietly(tmp_path):
    with pytest.raises(IsolationError, match="missing"):
        with restore_guard(tmp_path / "nope.py"):
            pass


def test_a_restore_that_cannot_reproduce_the_source_raises(module, monkeypatch):
    """A silent restore failure would let the run report a clean tree it does not have."""
    real_write = Path.write_bytes

    def _sabotage(self, data):
        return real_write(self, b"not the original\n")

    with pytest.raises(IsolationError, match="RESTORE FAILED"):
        with restore_guard(module):
            module.write_bytes(b'VALUE = "mutated!"\n')
            monkeypatch.setattr(Path, "write_bytes", _sabotage)


# ── P2: a recursive delete needs a proven root ─────────────────────────────

@pytest.mark.parametrize("marker", ["python", "nope"])
def test_purge_refuses_anything_that_is_not_the_expected_checkout(tmp_path, marker):
    (tmp_path / "python").mkdir()
    if marker == "nope":
        with pytest.raises(IsolationError, match="does not look like"):
            purge_repo_bytecode(tmp_path, expect_marker=marker)
    else:
        assert purge_repo_bytecode(tmp_path, expect_marker=marker) == 0


def test_purge_refuses_a_filesystem_or_home_root():
    for root in (Path(Path.cwd().anchor), Path.home()):
        with pytest.raises(IsolationError):
            purge_repo_bytecode(root)


def test_purge_refuses_a_cache_that_escapes_the_checkout(tmp_path):
    """A `__pycache__` symlink pointing outside must not be followed into another tree."""
    checkout = tmp_path / "checkout"
    (checkout / "python").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("keep me", encoding="utf-8")
    (checkout / "__pycache__").symlink_to(outside, target_is_directory=True)

    with pytest.raises(IsolationError, match="symlink"):
        purge_repo_bytecode(checkout)
    assert (outside / "precious.txt").exists(), "the purge escaped the checkout"


def test_purge_removes_only_caches_inside_the_checkout(tmp_path):
    checkout = tmp_path / "checkout"
    (checkout / "python" / "__pycache__").mkdir(parents=True)
    (checkout / "python" / "__pycache__" / "a.pyc").write_bytes(b"x")
    (checkout / "keep.py").write_text("x = 1\n", encoding="utf-8")

    assert purge_repo_bytecode(checkout) == 1
    assert not (checkout / "python" / "__pycache__").exists()
    assert (checkout / "keep.py").exists()


# ── the repo itself must be untouched by an isolated run ───────────────────

def test_an_isolated_subprocess_writes_no_bytecode_into_the_repo():
    before = bytecode_snapshot(REPO)
    with isolated_run() as env:
        subprocess.run(
            [sys.executable, "-c", "import sys; sys.path.insert(0, 'python'); "
                                   "import narasi_counters"],
            cwd=REPO, env=env, capture_output=True, text=True)
    assert_bytecode_unchanged(REPO, before)
