#!/usr/bin/env python3
"""Memory-type v1 sync check across the 7 canonical locations.

Rc.13 QA surfaced drift symptoms where a PR adds a new memory-type token
to one consumer (e.g. the Rust enum) but forgets the other six. That
desync causes runtime drift: a Python client emits a token the OpenClaw
plugin then rejects at the write path.

This check parses each location, extracts the set of memory-type tokens,
and exits 1 with a per-file diff report if any two locations disagree.

Stopgap until the rc.16 "single-source memory-type spec" epic lands.
See ``docs/specs/totalreclaw/memory-taxonomy-v1.md``.

Run locally::

    python3 scripts/check-memory-type-sync.py

Exit 0 on match, exit 1 on mismatch. stdout = human-readable diff report.

Philosophy: when parsing is ambiguous we err on the side of flagging
(false positive beats silent drift). If the check fails on main for a
reason other than a genuine drift, fix the parser rather than lower the
bar.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Iterable


# Repo root is the parent of this ``scripts/`` directory.
REPO_ROOT = Path(__file__).resolve().parent.parent


# Each entry: (human-readable label, relative path, parser function name).
# The parser returns a ``set[str]`` of memory-type tokens, or raises
# ``ParseError`` if the source shape no longer matches what we expect
# (that's a bug in either the source or this script — fix rather than
# suppress).
LOCATIONS: list[tuple[str, str, str]] = [
    (
        "skill/plugin/extractor.ts (VALID_MEMORY_TYPES)",
        "skill/plugin/extractor.ts",
        "parse_ts_const_array_VALID_MEMORY_TYPES",
    ),
    (
        "mcp/src/v1-types.ts (VALID_MEMORY_TYPES_V1)",
        "mcp/src/v1-types.ts",
        "parse_ts_const_array_VALID_MEMORY_TYPES_V1",
    ),
    (
        "python/src/totalreclaw/agent/extraction.py (VALID_MEMORY_TYPES)",
        "python/src/totalreclaw/agent/extraction.py",
        "parse_py_tuple_VALID_MEMORY_TYPES",
    ),
    (
        "rust/totalreclaw-core/src/claims.rs (MemoryTypeV1 enum)",
        "rust/totalreclaw-core/src/claims.rs",
        "parse_rust_enum_MemoryTypeV1",
    ),
    (
        "skill/plugin/claims-helper.ts (TYPE_TO_CATEGORY_V1)",
        "skill/plugin/claims-helper.ts",
        "parse_ts_record_TYPE_TO_CATEGORY_V1",
    ),
    (
        "python/src/totalreclaw/claims_helper.py (TYPE_TO_CATEGORY_V1)",
        "python/src/totalreclaw/claims_helper.py",
        "parse_py_dict_TYPE_TO_CATEGORY_V1",
    ),
    (
        "rust/totalreclaw-core/src/prompts/extraction.md (TYPE section)",
        "rust/totalreclaw-core/src/prompts/extraction.md",
        "parse_md_type_section",
    ),
]


class ParseError(RuntimeError):
    """Raised when a source file no longer matches the shape we parse.

    This is deliberately fatal — if the Rust enum turns into a macro, or
    the Python tuple becomes a frozenset literal, we'd rather fail loud
    than silently emit an empty set and call everything "in sync".
    """


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _read(path: str) -> str:
    full = REPO_ROOT / path
    if not full.is_file():
        raise ParseError(f"file missing: {path}")
    return full.read_text(encoding="utf-8")


def _strip_line_comments(block: str, markers: Iterable[str]) -> str:
    """Strip trailing ``//`` or ``#`` comments from each line.

    Naive but sufficient: we only ever feed the interior of a literal
    (strings inside array/tuple/dict), and the memory-type tokens never
    contain comment markers.
    """
    out_lines: list[str] = []
    for line in block.splitlines():
        for marker in markers:
            idx = line.find(marker)
            if idx != -1:
                line = line[:idx]
        out_lines.append(line)
    return "\n".join(out_lines)


_STRING_LITERAL_RE = re.compile(r"""['"]([A-Za-z_][A-Za-z0-9_-]*)['"]""")


def _extract_string_literals(block: str) -> set[str]:
    return set(_STRING_LITERAL_RE.findall(block))


# ---------------------------------------------------------------------------
# TypeScript parsers
# ---------------------------------------------------------------------------


def _parse_ts_const_array(source: str, const_name: str) -> set[str]:
    pat = re.compile(
        r"export\s+const\s+" + re.escape(const_name) + r"\s*=\s*\[(?P<body>.*?)\]\s*as\s+const\s*;",
        re.DOTALL,
    )
    m = pat.search(source)
    if not m:
        raise ParseError(f"could not find `export const {const_name} = [...] as const;`")
    body = _strip_line_comments(m.group("body"), ("//",))
    return _extract_string_literals(body)


def parse_ts_const_array_VALID_MEMORY_TYPES(source: str) -> set[str]:
    return _parse_ts_const_array(source, "VALID_MEMORY_TYPES")


def parse_ts_const_array_VALID_MEMORY_TYPES_V1(source: str) -> set[str]:
    return _parse_ts_const_array(source, "VALID_MEMORY_TYPES_V1")


def parse_ts_record_TYPE_TO_CATEGORY_V1(source: str) -> set[str]:
    """Parse ``const TYPE_TO_CATEGORY_V1: Record<MemoryType, string> = { ... };``.

    Returns the set of KEYS (the v1 type tokens), not the values (which
    are display-category short keys and intentionally differ).
    """
    pat = re.compile(
        r"const\s+TYPE_TO_CATEGORY_V1\s*:\s*Record\s*<[^>]+>\s*=\s*\{(?P<body>.*?)\}\s*;",
        re.DOTALL,
    )
    m = pat.search(source)
    if not m:
        raise ParseError("could not find `const TYPE_TO_CATEGORY_V1: Record<...> = { ... };`")
    body = _strip_line_comments(m.group("body"), ("//",))

    # Keys may be bare identifiers or quoted strings: ``claim: 'claim',``
    # or ``'claim': 'claim',``. Match both.
    key_pat = re.compile(
        r"(?m)^\s*(?:['\"]?(?P<key>[A-Za-z_][A-Za-z0-9_]*)['\"]?)\s*:",
    )
    keys = {m.group("key") for m in key_pat.finditer(body)}
    if not keys:
        raise ParseError("TYPE_TO_CATEGORY_V1 body parsed to zero keys — parser broken")
    return keys


# ---------------------------------------------------------------------------
# Python parsers
# ---------------------------------------------------------------------------


def parse_py_tuple_VALID_MEMORY_TYPES(source: str) -> set[str]:
    pat = re.compile(
        r"VALID_MEMORY_TYPES\s*:\s*tuple\[[^\]]*\]\s*=\s*\((?P<body>.*?)\)",
        re.DOTALL,
    )
    m = pat.search(source)
    if not m:
        # Fallback: accept a bare assignment without annotation.
        pat2 = re.compile(
            r"^VALID_MEMORY_TYPES\s*=\s*\((?P<body>.*?)\)",
            re.DOTALL | re.MULTILINE,
        )
        m = pat2.search(source)
    if not m:
        raise ParseError("could not find `VALID_MEMORY_TYPES = (...)` in extraction.py")
    body = _strip_line_comments(m.group("body"), ("#",))
    return _extract_string_literals(body)


def parse_py_dict_TYPE_TO_CATEGORY_V1(source: str) -> set[str]:
    """Parse ``TYPE_TO_CATEGORY_V1: Dict[str, str] = { ... }``.

    Returns KEYS only.
    """
    pat = re.compile(
        r"TYPE_TO_CATEGORY_V1\s*:\s*Dict\s*\[[^\]]+\]\s*=\s*\{(?P<body>.*?)\}",
        re.DOTALL,
    )
    m = pat.search(source)
    if not m:
        raise ParseError("could not find `TYPE_TO_CATEGORY_V1: Dict[...] = { ... }`")
    body = _strip_line_comments(m.group("body"), ("#",))
    # Match `"claim": "claim",` → capture the key side.
    key_pat = re.compile(r"""['"](?P<key>[A-Za-z_][A-Za-z0-9_]*)['"]\s*:""")
    keys = {m.group("key") for m in key_pat.finditer(body)}
    if not keys:
        raise ParseError("TYPE_TO_CATEGORY_V1 body parsed to zero keys — parser broken")
    return keys


# ---------------------------------------------------------------------------
# Rust parser
# ---------------------------------------------------------------------------


_RUST_VARIANT_RE = re.compile(
    # An enum variant line: optional doc-comments / attrs above, then
    # a capitalised identifier followed by a comma. We only match at
    # start-of-line (indentation allowed) and require a trailing comma
    # — this excludes match arms (which use `=>`) and struct fields
    # (which use `:`).
    r"(?m)^\s*(?P<name>[A-Z][A-Za-z0-9]*)\s*,\s*(?://.*)?$"
)


def parse_rust_enum_MemoryTypeV1(source: str) -> set[str]:
    """Parse the ``pub enum MemoryTypeV1 { ... }`` block.

    Variants are emitted lowercase at the wire layer via
    ``#[serde(rename_all = "lowercase")]``, so we lowercase each
    PascalCase variant to match the wire tokens the TS/Python sets hold.
    """
    # Find the enum block. Brace counting keeps us honest about nested
    # constructs (there are none today, but enum variants could gain
    # struct fields).
    enum_start = re.search(r"pub\s+enum\s+MemoryTypeV1\s*\{", source)
    if not enum_start:
        raise ParseError("could not find `pub enum MemoryTypeV1 {`")
    i = enum_start.end()  # position just after the opening `{`
    depth = 1
    body_start = i
    while i < len(source) and depth > 0:
        c = source[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    if depth != 0:
        raise ParseError("unbalanced braces in MemoryTypeV1 enum")
    body = source[body_start:i]

    # Strip `///` doc-comments and `//` line comments before matching.
    stripped = _strip_line_comments(body, ("///", "//"))

    variants: set[str] = set()
    for m in _RUST_VARIANT_RE.finditer(stripped):
        name = m.group("name")
        # Skip common false-positives (attributes already live in
        # `#[...]` which doesn't match the pattern, but defensive):
        if name in {"Self"}:
            continue
        variants.add(name.lower())

    if not variants:
        raise ParseError("MemoryTypeV1 enum body parsed to zero variants — parser broken")
    return variants


# ---------------------------------------------------------------------------
# Markdown (prompt) parser
# ---------------------------------------------------------------------------


def parse_md_type_section(source: str) -> set[str]:
    """Parse the TYPE section of ``prompts/extraction.md``.

    Shape:

        TYPE (6 values)
        ═══...
        - claim: factual assertion ...
        - preference: likes/dislikes ...
        ...

    Ends at the next section header (line containing ``═`` chars or a
    blank line followed by an all-caps header word).
    """
    lines = source.splitlines()
    # Find the TYPE header.
    try:
        start = next(
            i for i, line in enumerate(lines)
            if re.match(r"^TYPE\s*\(", line)
        )
    except StopIteration:
        raise ParseError("could not find `TYPE (N values)` section header in extraction.md")

    # Walk forward from the header, skipping the underline ═... line,
    # until we hit the next section marker.
    types: set[str] = set()
    i = start + 1
    # Skip the ═ underline (and any blank lines right after the header).
    while i < len(lines) and (not lines[i].strip() or re.match(r"^[═=─-]+\s*$", lines[i])):
        i += 1

    # Collect bullet entries.
    bullet_re = re.compile(r"^-\s+(?P<name>[a-z][a-z0-9_-]*)\s*:")
    while i < len(lines):
        line = lines[i]
        # Next section ruler → stop.
        if re.match(r"^[═=─-]+\s*$", line):
            break
        # Next section header (ALL-CAPS word at column 0, maybe with
        # parens) → stop.
        if line and re.match(r"^[A-Z][A-Z _]{2,}(?:\(|$)", line):
            break
        m = bullet_re.match(line)
        if m:
            types.add(m.group("name"))
        i += 1

    if not types:
        raise ParseError("TYPE section parsed to zero tokens — parser broken")
    return types


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


PARSERS = {
    "parse_ts_const_array_VALID_MEMORY_TYPES": parse_ts_const_array_VALID_MEMORY_TYPES,
    "parse_ts_const_array_VALID_MEMORY_TYPES_V1": parse_ts_const_array_VALID_MEMORY_TYPES_V1,
    "parse_ts_record_TYPE_TO_CATEGORY_V1": parse_ts_record_TYPE_TO_CATEGORY_V1,
    "parse_py_tuple_VALID_MEMORY_TYPES": parse_py_tuple_VALID_MEMORY_TYPES,
    "parse_py_dict_TYPE_TO_CATEGORY_V1": parse_py_dict_TYPE_TO_CATEGORY_V1,
    "parse_rust_enum_MemoryTypeV1": parse_rust_enum_MemoryTypeV1,
    "parse_md_type_section": parse_md_type_section,
}


def main() -> int:
    results: list[tuple[str, frozenset[str], str | None]] = []
    parse_errors: list[tuple[str, str]] = []

    for label, path, parser_name in LOCATIONS:
        parser = PARSERS[parser_name]
        try:
            source = _read(path)
            tokens = parser(source)
        except ParseError as exc:
            parse_errors.append((label, str(exc)))
            continue
        results.append((label, frozenset(tokens), path))

    if parse_errors:
        print("PARSE ERRORS (bug in script or source moved):")
        for label, msg in parse_errors:
            print(f"  - {label}: {msg}")
        print()
        print("Not a drift signal — fix the parser or the source before retrying.")
        return 1

    # All locations parsed. Compute union, then per-file diff vs union.
    all_tokens: frozenset[str] = frozenset().union(*(tokens for _, tokens, _ in results))
    sets_identical = len({tokens for _, tokens, _ in results}) == 1

    if sets_identical:
        tokens_sorted = sorted(all_tokens)
        print(
            f"OK: all {len(results)} locations hold the same "
            f"{len(all_tokens)} memory-type tokens: {tokens_sorted}"
        )
        return 0

    # Drift — emit per-file diff vs the union.
    print(
        "MISMATCH: memory-type v1 tokens differ across the canonical locations.\n"
        "Every listed file must hold the SAME set of tokens. See\n"
        "docs/specs/totalreclaw/memory-taxonomy-v1.md for the canonical taxonomy.\n"
    )
    print(f"Union of all tokens found: {sorted(all_tokens)}\n")
    for label, tokens, path in results:
        missing = sorted(all_tokens - tokens)
        extra = sorted(tokens - all_tokens)  # will always be empty given union, kept for symmetry
        if missing or extra:
            print(f"  {label}  ({path})")
            if missing:
                print(f"    MISSING: {missing}")
            if extra:
                print(f"    EXTRA:   {extra}")
    # Also compute pairwise diffs between the first location and each
    # other — the "union" frame hides an "EXTRA" token if it appears in
    # exactly one location. Report EXTRA tokens relative to the most
    # common set.
    from collections import Counter
    counts: Counter[frozenset[str]] = Counter(tokens for _, tokens, _ in results)
    # Most-common set is the reference for the EXTRA diff.
    reference = counts.most_common(1)[0][0]
    extras_any = False
    extras_lines: list[str] = []
    for label, tokens, path in results:
        extra = sorted(tokens - reference)
        if extra:
            if not extras_any:
                extras_lines.append(
                    f"\nEXTRA tokens relative to the most-common set "
                    f"{sorted(reference)}:"
                )
                extras_any = True
            extras_lines.append(f"  {label}  ({path})")
            extras_lines.append(f"    EXTRA: {extra}")
    if extras_any:
        print("\n".join(extras_lines))

    return 1


if __name__ == "__main__":
    sys.exit(main())
