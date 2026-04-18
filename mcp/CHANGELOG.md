# Changelog

## [3.0.1]

### Fixed
- Outer protobuf wrapper `version` field was hardcoded to `2` in `encodeFactProtobuf`, while all other v1 clients (OpenClaw plugin, Python, Rust `totalreclaw-memory`) write `4` per the Memory Taxonomy v1 contract. MCP now writes `PROTOBUF_VERSION_V4 = 4`. Matches VPS QA Bug #10 in `QA-V1-VPS-20260418.md`.

## [3.0.0]

### Changed
- Compressed tool descriptions to ≤500 chars each to reduce per-turn LLM context cost while preserving auto-invocation triggers.
