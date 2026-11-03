# Test fixtures

`chatgpt-conversations.fixture.json` and `chatgpt-fixture-manifest.json` are **fully synthetic** (2026-10-25).
The seven conversations mirror the tree shape of a real ChatGPT export (mapping, branches, `current_node`,
roles, content types, string vs. dict parts, relative timestamps) so the session-fidelity tests keep their
expected counts, but every id, title and piece of text was generated. No real conversation text is present.
Never commit real export data here; see `totalreclaw-internal/e2e/chatgpt-import/README.md`.
