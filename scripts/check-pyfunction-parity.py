#!/usr/bin/env python3
"""Source-level parity + wheel-surface check for the totalreclaw-core PyO3 bindings.

Background
----------
The Python bindings were split out of a former monolithic ``python.rs`` into
``src/python.rs`` + ``src/python/bind_*.rs`` (issue #475). A ``#[pyfunction]``
declared in a ``bind_*.rs`` file is *invisible at runtime* unless it is also
registered with ``m.add_function(wrap_pyfunction!(..))`` in the ``#[pymodule]``
in ``python.rs`` (or in a per-module ``register_python_functions`` for the
external modules). A missed registration silently drops the function from the
built PyPI wheel -- the same failure class as the Hermes rc10 incident (issue
#427: ``totalreclaw_top_up`` shipped with schema + handler but was never
``register()``-ed, so the model never saw it).

This script provides two modes:

1. ``check-pyfunction-parity.py`` (default) -- source-level parity.
   For every *registration unit*, assert the set of ``#[pyfunction]`` Rust fn
   names equals the set of ``wrap_pyfunction!(..)`` registered names. Fails
   (exit 1) and prints the diff on any mismatch. Cheap, no toolchain -- wired
   into the ``rust-tests`` CI job.

   Registration units:
     * ``python.rs`` ``#[pymodule]``  -- declarations in ``python.rs`` +
       ``python/bind_*.rs``; registrations (``wrap_pyfunction!``) in ``python.rs``.
     * each external module that self-registers via
       ``register_python_functions`` (``consolidation.rs``,
       ``import_parsers.rs``, ``memory_types.rs``, ``smart_import.rs``) --
       declarations + registrations both live in that one file.

2. ``check-pyfunction-parity.py --expected-exports`` -- emit the sorted list of
   Python-visible names the built wheel MUST expose (every ``#[pyo3(name = ...)]``
   attribute, falling back to the Rust fn name when absent, plus ``#[pyclass]``
   names). The publish workflow installs the freshly-built wheel into a scratch
   venv, captures ``dir(totalreclaw_core)``, and diffs it against this output;
   publish fails on mismatch. NOT run in PR CI (no toolchain-heavy steps in test
   jobs -- see #489/#491).

Why per-unit scoping: a ``#[pyfunction]`` in ``bind_search.rs`` is registered in
``python.rs`` (not in ``bind_search.rs``), so a flat whole-crate decl-vs-reg
comparison would be meaningless. Grouping by registration site mirrors how the
module is actually assembled and catches the real failure: a declaration whose
registration was forgotten at its home site.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# Rust source comment stripping (so doc-comment mentions of `#[pyfunction]`
# or `fn ` inside `///` docs don't fool the parser -- e.g. the module-level
# doc in python.rs literally writes `#[pyfunction]` in prose).
# --------------------------------------------------------------------------

def strip_comments(text: str) -> str:
    """Remove ``//`` line comments and ``/* */`` block comments outside strings.

    Char/string-aware so a ``//`` or ``/*`` that appears inside a string
    literal is left intact (matters for attribute values, though we don't
    currently read any that contain comment markers).
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        # Line comment
        if c == "/" and nxt == "/":
            j = text.find("\n", i)
            if j == -1:
                break
            i = j
            continue
        # Block comment
        if c == "/" and nxt == "*":
            j = text.find("*/", i + 2)
            if j == -1:
                break
            i = j + 2
            continue
        # String literal
        if c == '"':
            out.append(c)
            i += 1
            while i < n:
                d = text[i]
                out.append(d)
                if d == "\\" and i + 1 < n:
                    out.append(text[i + 1])
                    i += 2
                    continue
                i += 1
                if d == '"':
                    break
            continue
        # Char literal
        if c == "'":
            # Heuristic: only treat as char literal if it looks like one
            # ('x' or '\x' or '\\x'). Avoids eating lifetimes like `'a`.
            m = re.match(r"'(\\.|[^'\\])'", text[i:])
            if m:
                out.append(m.group(0))
                i += m.end()
                continue
        out.append(c)
        i += 1
    return "".join(out)


# --------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------

PYF_ATTR = re.compile(r"#\[(?:pyo3::prelude::)?pyfunction\]")
FN = re.compile(r"\bfn\s+(\w+)")
PYO3_NAME = re.compile(r'#\[pyo3\(\s*name\s*=\s*"([^"]+)"\s*\)\]')
WRAP = re.compile(r"wrap_pyfunction!\(\s*(\w+)")
PYCLASS = re.compile(r"#\[(?:pyo3::prelude::)?pyclass(?:[^\]]*)\]")
STRUCT_OR_ENUM = re.compile(r"\b(?:struct|enum)\s+(\w+)")


class PyFunc:
    """A parsed ``#[pyfunction]`` declaration."""

    __slots__ = ("rust_fn", "py_name", "file")

    def __init__(self, rust_fn: str, py_name: str, file: str) -> None:
        self.rust_fn = rust_fn
        self.py_name = py_name
        self.file = file


def parse_pyfunctions(text: str, file: str) -> list[PyFunc]:
    """Return every ``#[pyfunction]`` in ``text`` with its Rust fn name and
    Python-visible name (``#[pyo3(name=...)]`` if present, else the fn name).

    The attribute block between the ``#[pyfunction]`` line and the ``fn``
    declaration may carry ``#[pyo3(name = "...")]``, ``#[cfg(...)]``, doc
    comments, etc.; we scan that block for the pyo3 name.
    """
    text = strip_comments(text)
    funcs: list[PyFunc] = []
    for m in PYF_ATTR.finditer(text):
        tail = text[m.end(): m.end() + 2000]
        fnm = FN.search(tail)
        if fnm is None:
            # Not a function attribute (shouldn't happen for valid Rust) -- skip.
            continue
        rust_fn = fnm.group(1)
        block = tail[: fnm.start()]
        nm = PYO3_NAME.search(block)
        py_name = nm.group(1) if nm else rust_fn
        funcs.append(PyFunc(rust_fn, py_name, file))
    return funcs


def parse_registrations(text: str) -> list[str]:
    """Return the Rust fn names registered via ``wrap_pyfunction!(..)``."""
    text = strip_comments(text)
    return WRAP.findall(text)


def parse_classes(text: str, file: str) -> list[str]:
    """Return Python-visible class names from ``#[pyclass]`` declarations."""
    text = strip_comments(text)
    names: list[str] = []
    for m in PYCLASS.finditer(text):
        attr = m.group(0)
        tail = text[m.end(): m.end() + 500]
        nm = re.search(r'name\s*=\s*"([^"]+)"', attr)
        if nm:
            names.append(nm.group(1))
            continue
        sm = STRUCT_OR_ENUM.search(tail)
        if sm:
            names.append(sm.group(1))
    return names


# --------------------------------------------------------------------------
# Registration units
# --------------------------------------------------------------------------

PYTHON_DECL_FILES = ["src/python.rs"]  # bind_*.rs appended at runtime (glob)
PYTHON_REG_FILES = ["src/python.rs"]

# External modules that own their own register_python_functions: decls and
# registrations both live in the single named file.
EXTERNAL_MODULES = [
    "src/consolidation.rs",
    "src/import_parsers.rs",
    "src/memory_types.rs",
    "src/smart_import.rs",
]


def core_dir_default() -> Path:
    """Auto-detect the core dir from this script's location.

    Script lives at ``<repo>/scripts/check-pyfunction-parity.py``; core is at
    ``<repo>/rust/totalreclaw-core``. CI invokes from the repo root, so a
    relative ``rust/totalreclaw-core`` also resolves, but anchoring on
    ``__file__`` keeps it correct regardless of CWD.
    """
    here = Path(__file__).resolve().parent
    repo = here.parent
    return repo / "rust" / "totalreclaw-core"


def all_decl_files(core: Path) -> list[str]:
    files = list(PYTHON_DECL_FILES)
    bind = sorted(p.name for p in (core / "src" / "python").glob("bind_*.rs"))
    files += [f"src/python/{p}" for p in bind]
    return files


# --------------------------------------------------------------------------
# Mode 1: parity
# --------------------------------------------------------------------------

def run_parity(core: Path) -> int:
    units: list[tuple[str, list[str], list[str]]] = [
        ("python.rs #[pymodule]", all_decl_files(core), PYTHON_REG_FILES),
    ]
    for mod in EXTERNAL_MODULES:
        units.append((f"{mod} (register_python_functions)", [mod], [mod]))

    failed = False
    print("# PyO3 pyfunction <-> registration parity")
    for name, decl_files, reg_files in units:
        decls: list[PyFunc] = []
        for rel in decl_files:
            decls += parse_pyfunctions((core / rel).read_text(), rel)
        regs: list[str] = []
        for rel in reg_files:
            regs += parse_registrations((core / rel).read_text())

        decl_names = sorted(d.rust_fn for d in decls)
        reg_names = sorted(set(regs))
        decl_set = set(decl_names)
        reg_set = set(reg_names)

        unregistered = sorted(decl_set - reg_set)
        orphan = sorted(reg_set - decl_set)
        ok = not unregistered and not orphan
        status = "OK" if ok else "FAIL"
        print(f"  [{status}] {name}: {len(decl_names)} declared, {len(reg_names)} registered")
        if unregistered:
            failed = True
            print(f"        declared but NOT registered (dropped from wheel):")
            for fn in unregistered:
                src = next(d.file for d in decls if d.rust_fn == fn)
                print(f"          - {fn}  (in {src})")
        if orphan:
            failed = True
            print(f"        registered but NOT declared in scope (stale/dangling):")
            for fn in orphan:
                print(f"          - {fn}")
        # Duplicate declarations would mean two fns with the same name in the
        # unit -- flag it (len(list) != len(set)).
        dupes = sorted({fn for fn in decl_names if decl_names.count(fn) > 1})
        if dupes:
            failed = True
            print(f"        DUPLICATE declarations:")
            for fn in dupes:
                print(f"          - {fn}")

    if failed:
        print("\nFAIL: pyfunction/registration mismatch detected.", file=sys.stderr)
        return 1
    print("\nOK: all #[pyfunction] declarations are registered.")
    return 0


# --------------------------------------------------------------------------
# Mode 2: expected exports (wheel surface)
# --------------------------------------------------------------------------

def run_expected_exports(core: Path) -> int:
    files = all_decl_files(core) + EXTERNAL_MODULES
    exports: set[str] = set()
    for rel in files:
        text = (core / rel).read_text()
        for d in parse_pyfunctions(text, rel):
            exports.add(d.py_name)
        for cls in parse_classes(text, rel):
            exports.add(cls)

    for name in sorted(exports):
        print(name)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--core-dir",
        type=Path,
        default=core_dir_default(),
        help="path to rust/totalreclaw-core (default: auto-detect from script location)",
    )
    p.add_argument(
        "--expected-exports",
        action="store_true",
        help="print the sorted Python-visible names the built wheel must expose, then exit",
    )
    args = p.parse_args(argv)

    core: Path = args.core_dir
    if not core.exists():
        print(f"error: core dir not found: {core}", file=sys.stderr)
        return 2

    if args.expected_exports:
        return run_expected_exports(core)
    return run_parity(core)


if __name__ == "__main__":
    sys.exit(main())
