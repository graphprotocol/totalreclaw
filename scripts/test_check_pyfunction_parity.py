#!/usr/bin/env python3
"""Self-test for check-pyfunction-parity.py. Run: python3 scripts/test_check_pyfunction_parity.py"""
import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "cpp", Path(__file__).resolve().parent / "check-pyfunction-parity.py"
)
cpp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cpp)


def names(src):
    return [(f.rust_fn, f.py_name) for f in cpp.parse_pyfunctions(src, "t.rs")]


CASES = {
    "sole name": (
        '#[pyfunction]\n#[pyo3(name = "foo")]\nfn py_foo() {}',
        [("py_foo", "foo")],
    ),
    "name then multi-line signature": (
        '#[pyfunction]\n#[pyo3(\n    name = "foo",\n    signature = (a, b, cap = crate::X::DEFAULT)\n)]\npub(crate) fn py_foo(\n',
        [("py_foo", "foo")],
    ),
    "signature then name": (
        '#[pyfunction]\n#[pyo3(signature = (a, b = 1), name = "foo")]\nfn py_foo() {}',
        [("py_foo", "foo")],
    ),
    "no name falls back to fn": (
        '#[pyfunction]\n#[pyo3(signature = (a, b = 1))]\nfn py_foo() {}\n#[pyfunction]\nfn bar() {}',
        [("py_foo", "py_foo"), ("bar", "bar")],
    ),
    "doc comment name ignored": (
        '/// uses #[pyo3(name = "x")] in prose\n#[pyfunction]\n/// name = "x"\nfn py_foo() {}',
        [("py_foo", "py_foo")],
    ),
    "name inside signature string ignored": (
        '#[pyfunction]\n#[pyo3(signature = (a = "name = \\"y\\""))]\nfn py_foo() {}',
        [("py_foo", "py_foo")],
    ),
}

failed = 0
for label, (src, want) in CASES.items():
    got = names(src)
    ok = got == want
    failed += not ok
    print(f"[{'OK' if ok else 'FAIL'}] {label}" + ("" if ok else f": got {got}, want {want}"))
sys.exit(1 if failed else 0)
