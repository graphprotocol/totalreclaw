"""CI grep gate (D8, internal#573 spec 9): no client-side hashing of words,
stems, entities, digest markers or LSH buckets in ``python/src``.

The gate also matches comments and docstrings, on purpose: a comment that
names a removed helper invites someone to bring it back.
"""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

FORBIDDEN = [
    r"generate_blind_indices",
    r"compute_entity_trapdoor",
    r"DIGEST_TRAPDOOR",
    r"generate_content_fingerprint\(",
    r"generate_search_trapdoors\(",
    r"generate_expansion_trapdoors",
    r"prepare_fact\(",
    r"prepare_tombstone\(",
    r"encode_fact_protobuf\(",
    r"encode_tombstone_protobuf\(",
    r"lsh_hasher\.hash\(",
    r"_inner\.hash\(",
    r"hasher\.hash\(",
    r"""b?["']entity:["']""",
    r"""b?["']stem:["']""",
    r"""b?["']type:digest["']""",
]
PATTERN = re.compile("|".join("(?:%s)" % p for p in FORBIDDEN))
DEF_HASH = re.compile(r"def hash\(")


def scan_text(path: Path, text: str):
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        if PATTERN.search(line):
            hits.append((str(path), n))
        if path.name == "lsh.py" and DEF_HASH.search(line):
            hits.append((str(path), n))
    return hits


def scan_tree(root: Path):
    hits = []
    for p in sorted(root.rglob("*.py")):
        hits.extend(scan_text(p, p.read_text(encoding="utf-8")))
    return hits


def test_no_client_side_hashing_in_src():
    assert scan_tree(SRC) == []


def test_gate_positive_controls(tmp_path):
    """The scan itself must work: each forbidden form is caught."""
    samples = [
        "generate_blind_indices(x)",
        "compute_entity_trapdoor(n)",
        "DIGEST_TRAPDOOR = 1",
        "generate_content_fingerprint(a, b)",
        "generate_search_trapdoors(a)",
        "x = prepare_fact(a)",
        "encode_fact_protobuf(p)",
        "encode_tombstone_protobuf(a, b)",
        "lsh_hasher.hash(e)",
        "self._inner.hash(e)",
        'h = sha(b"entity:" + n)',
        "h = sha('stem:' + n)",
        'x = "type:digest"',
    ]
    f = tmp_path / "probe.py"
    for s in samples:
        f.write_text(s + "\n")
        assert scan_tree(tmp_path), s
    lsh = tmp_path / "lsh.py"
    lsh.write_text("    def hash(self, e):\n        pass\n")
    assert any(h[0].endswith("lsh.py") for h in scan_tree(tmp_path))


def test_allowed_keyed_forms_pass(tmp_path):
    f = tmp_path / "ok.py"
    f.write_text(
        "generate_content_fingerprint_v2(a, b)\n"
        "generate_search_trapdoors_keyed(a)\n"
        "encode_fact_protobuf_keyed(p)\n"
        "encode_tombstone_protobuf_keyed(a, b)\n"
    )
    assert scan_tree(tmp_path) == []
