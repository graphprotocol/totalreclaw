# Changelog

All notable changes to the `totalreclaw` Python client and the `totalreclaw.hermes`
Hermes Agent plugin are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.3.1rc6] — 2026-04-22

Ship-stopper fix for the rc.4 regression that shipped to manual QA: Hermes chat agent could not see the `totalreclaw_*` tools in its toolset even though the plugin loaded cleanly (SKILL.md surfaced, module imported). Root cause was a long-latent drift between `plugin.yaml::provides_tools` and the `ctx.register_tool()` calls in `totalreclaw.hermes.register()` — the manifest advertised `totalreclaw_pin` / `totalreclaw_unpin` as agent-facing but `register()` never wired them. The drift had been in the codebase since pin/unpin landed in 2.2.2; rc.4's phrase-safety hardening surfaced it because the user's first contact with the plugin was a fresh install hunting for `totalreclaw_pair` (which IS wired), which made the narrower missing-pin/unpin symptom mis-attributable to pair.

### Fixed (ship-stopper)

- **`python/src/totalreclaw/hermes/__init__.py` — wire `totalreclaw_pin` and `totalreclaw_unpin` into the agent tool list.** Both tool handlers (`tools.pin`, `tools.unpin`) and schemas (`schemas.PIN`, `schemas.UNPIN`) have existed since 2.2.2, and `plugin.yaml` has advertised them since that release — but `register()` never called `ctx.register_tool()` for either. Downstream effect: Hermes chat agents could not pin or unpin memories (the manifest said the tools existed, the register body disagreed, the register body won). Fix is 2 new `register_tool` calls mirroring the existing pattern.

### Added

- **`python/tests/test_hermes_plugin_manifest_parity.py`** — regression shield. Parses `plugin.yaml::provides_tools` and asserts every advertised tool has a matching `ctx.register_tool(name=...)` call during `register()`. Fails on rc.4 / rc.5 (pin + unpin missing from register body); passes on rc.6. Three related assertions: manifest is parseable, pair tool is both advertised + registered, phrase-unsafe tool names are not registered.

### Changed

- **`python/tests/test_hermes_plugin.py`** — updated `TestRegister.test_register` expected tool count (10 stable / 11 RC → 12 stable / 13 RC) and added explicit `totalreclaw_pin` / `totalreclaw_unpin` name assertions.

### Why auto-QA missed this

The rc.4 auto-QA "assertion 5" claimed pin/unpin worked — but that verification happened via a natural-language chat request on the **OpenClaw** plugin (which shares the same `credentials.json` as Hermes, making on-chain writes indistinguishable between clients). Auto-QA never asked the Hermes chat agent to pin/unpin, and its Hermes tool enumeration compared `register()`'s output against a hard-coded list that also omitted pin/unpin — so both sides drifted together away from `plugin.yaml`. The new parity test closes the gap at the choke-point: the manifest is the single source of truth for what the plugin advertises; `register()` must match it.

## [2.3.1rc5] — 2026-04-22 — SKIPPED

rc.5's QR-display work (PR #76, branch `fix/plugin-3.3.1-rc.5-qr-display`) remained unmerged when the rc.4 regression was escalated. rc.6 lands on top of rc.4 main with the narrow tool-registration fix; rc.5's QR display rebases onto rc.6 as a follow-up (see PR #76 coordination comment).

## [2.3.1rc4] — 2026-04-22

Phrase-safety hardening + console-script collision fix. Paired with plugin `3.3.1-rc.4`. This release introduces architectural enforcement of the "recovery phrase MUST NEVER cross the LLM context" rule by porting the OpenClaw plugin's QR-pair flow to Hermes Python and removing every phrase-generating agent tool.

### Fixed (ship-stopper)

- **`pyproject.toml` — removed the `hermes = "totalreclaw.hermes.cli:main"` console script.** rc.3 shipped with this entry, which OVERWROTE the upstream `hermes-agent` CLI on `pip install totalreclaw`. Users hit `hermes gateway → argument command: invalid choice: 'gateway' (choose from setup)`, the Docker container restart-looped, the TR plugin never loaded, and zero memories were extracted. rc.4 drops the colliding binary — only `totalreclaw = "totalreclaw.cli:main"` remains. Users on rc.3 whose `hermes` binary was overwritten can restore it via `pip install --force-reinstall hermes-agent`.

### Added

- **`totalreclaw.pair`** — new module. x25519 ECDH + HKDF-SHA256 + ChaCha20-Poly1305 AEAD. Ports `skill/plugin/pair-crypto.ts` to Python via the `cryptography` library. Includes:
  - `totalreclaw/pair/crypto.py` — `generate_gateway_keypair`, `compute_shared_secret`, `derive_aead_key_from_ecdh`, `aead_decrypt`, `encrypt_pairing_payload`, `decrypt_pairing_payload`, `compare_secondary_codes_ct`. Constants (HKDF info, AEAD key/nonce/tag lengths) match the TS module so a ciphertext produced by either side decrypts on the other.
  - `totalreclaw/pair/session_store.py` — atomic TTL-evicted session store at `~/.totalreclaw/pair-sessions.json` (mode 0600). `.lock` sentinel via `O_CREAT | O_EXCL`. Stale-lock break at 30 s. Schema parity with the TS store: same field names on-disk (camelCase), same status values (`awaiting_scan` / `device_connected` / `consumed` / `completed` / `expired` / `rejected`), same TTL bounds (5-60 min, default 15), same 5-strike secondary-code lockout.
  - `totalreclaw/pair/http_server.py` — stdlib `http.server` pinned to `127.0.0.1`. `GET /pair/<token>` serves the self-contained pair page; `POST /pair/<token>` accepts `{v, sid, pk_d, nonce, ct, pin}`, verifies PIN constant-time, decrypts, calls the caller-supplied completion handler, returns 204. CSP locked down, no-store cache, no LAN exposure.
  - `totalreclaw/pair/pair_page.py` — browser HTML with inline crypto (x25519 + ChaCha via WebCrypto). No CDN, no external scripts. Refuses to run on browsers that lack WebCrypto x25519 (Safari <17.2, Chromium <118).
- **`totalreclaw.hermes.pair_tool`** — `totalreclaw_pair` agent-tool handler. Returns `{url, pin, expires_at, mode, instructions}`. No phrase-adjacent data in the payload. Spawns (or reuses) a module-singleton background HTTP server on the first call.
- **`cryptography>=42.0`** runtime dep. OpenSSL-backed; provides `ChaCha20Poly1305` + `X25519PrivateKey`.

### Removed (phrase-safety enforcement — BREAKING for agent tool callers)

- **`totalreclaw_setup` agent tool — REMOVED.** rc.3 accepted a `recovery_phrase` tool argument (phrase-in via LLM tool-call payload) AND on phrase-less invocations GENERATED a fresh BIP-39 mnemonic and RETURNED it in the JSON response (phrase-out). Either path is a vault-compromise-class violation of `project_phrase_safety_rule.md`. The underlying `tools.setup` function stays in the module for CLI delegation (`totalreclaw setup` -> `hermes.cli.run_setup`) and test compat, but is NO LONGER registered as an agent tool. Agents route through `totalreclaw_pair` instead.
- Error messages in `tools.py` that previously pointed agents at `totalreclaw_setup` now point at `totalreclaw_pair`.

### Changed

- **`hermes/SKILL.md` — RULE 0 rewritten.** Now states the absolute rule ("phrase MUST NEVER cross the LLM context") and enumerates forbidden patterns (running `totalreclaw setup` / `hermes setup` via shell tool, passing phrase as tool-call arg, generating phrase in-chat, echoing pasted phrase, asking user to paste). RULE 1a rewritten around `totalreclaw_pair` as the canonical agent setup surface. RULE 5 (remote setup) collapsed into RULE 1a — QR pair is now the default for ALL users, not just remote/headless.
- **`hermes/plugin.yaml` — tool list updated.** `totalreclaw_setup` and `totalreclaw_onboarding_start` removed; `totalreclaw_pair` added.
- **`docs/guides/hermes-setup.md` — QR pair flow is now the default.** CLI wizard (`totalreclaw setup`) relegated to an "if you prefer local-terminal setup" subsection with a prominent "do NOT run this through an agent shell" warning.

### Tests

- **`tests/test_pair_crypto.py`** — 22 tests. Round-trip encrypt/decrypt, tamper rejection, sid binding, wrong-key rejection, constant-time PIN compare, RFC 5869 HKDF vector parity.
- **`tests/test_pair_http.py`** — 11 tests. Spins up the embedded server; asserts happy path (204 + completion), PIN mismatch (403), expired session (410), unknown token (404), tampered ciphertext (400). Also validates the `GET` pair page (CSP, cache-control, content).
- **`tests/test_agent_tools_phrase_safety.py`** — 5 tests. Enforces the phrase-safety contract: forbidden tool names NEVER registered, `totalreclaw_pair` IS registered, pair schema has no phrase-adjacent params, pair tool return value contains no phrase-adjacent keys.

## [2.3.1rc3] — 2026-04-22

Hermes-side companion to the plugin `3.3.1-rc.3` wave. Paired fixes for the two zai endpoint paths, a bigger retry budget, AA25 nonce serialisation, a new RC-gated bug-report tool, and two SKILL.md addendums. All prior rc.1 + rc.2 fixes preserved.

### Changed

- **`agent/llm_client.py` — configurable `ZAI_BASE_URL` + auto-fallback on "Insufficient balance" 429.** GLM Coding Plan keys hitting STANDARD (and PAYG keys hitting CODING) return HTTP 429 with body `"Insufficient balance or no resource package. Please recharge."`. rc.3: (a) accepts `ZAI_BASE_URL` env override via `get_zai_base_url()`; (b) auto-detects the error body in `chat_completion` and flips CODING ↔ STANDARD once per call (logged at INFO). SKILL.md updated with setup guidance ("GLM Coding Plan → leave unset; PAYG → set `ZAI_BASE_URL=https://api.z.ai/api/paas/v4`").
- **`agent/llm_client.py` — retry budget 3 attempts × 5/10/20s → 5 attempts × 2/4/8/16/32s.** rc.1/rc.2 QA: 5–9 of 10 extraction windows returned 0 facts against multi-minute upstream 429 storms. rc.3: total ~62s budget configurable via `TOTALRECLAW_LLM_RETRY_BUDGET_MS` env. On exhaustion raises new `LLMUpstreamOutageError` (with `attempts` + `last_status`) instead of returning `None` so callers can distinguish transient outages from parseable-empty responses. Non-retryable HTTP errors (401/403/404) re-raise as `httpx.HTTPStatusError` unchanged.
- **`userop.py` — per-account `asyncio.Lock` on UserOp submission.** rc.2 logged 16 AA25 nonce-conflict events from concurrent `build_and_send_userop{,_batch}` calls racing at `get_nonce(sender, 0)`. rc.3 serialises per-`sender` with `_get_sender_lock(sender)` so only one UserOp submission is in flight per Smart Account at a time. Existing AA25 retry with fresh nonce remains unchanged. Symmetric to the plugin-side `withSenderLock`.

### Added

- **`totalreclaw_report_qa_bug`** (RC-gated tool) — lets the Hermes agent file structured QA-bug issues to `p-diogo/totalreclaw-internal` during RC testing. Registered only when `totalreclaw.__version__` is a pre-release RC (PEP-440 `rcN` or SemVer `-rc.`). Handler POSTs to GitHub REST API using `TOTALRECLAW_QA_GITHUB_TOKEN` (or `GITHUB_TOKEN`). All free-text fields run through `redact_secrets()` fail-close: BIP-39 phrases, `sk-*` / Anthropic keys, `AIzaSy*` Google keys, Telegram bot tokens, bearer-auth headers, 64+ char hex blobs, 0x-prefixed private keys, qualified `token=`/`secret=` values. Naked UUIDs (fact ids) and 40-char commit SHAs are preserved. Stable builds never expose the tool.
- **`totalreclaw/hermes/qa_bug_report.py`** — pure-logic module. Exports `is_rc_build`, `redact_secrets`, `validate_args`, `build_issue_body`, `post_qa_bug_issue`, `report_qa_bug`, `SCHEMA`.
- **`tests/test_llm_client_rc3.py`** — 23 tests for zai auto-fallback (CODING→STANDARD, STANDARD→CODING, both-fail surfaces outage, non-zai URLs skip fallback), `LLMUpstreamOutageError` surfacing on 503/timeout exhaustion, retry-budget short-circuit.
- **`tests/test_qa_bug_report_rc3.py`** — 32 tests covering redaction corpus, validation, body builder, POST success + HTTP failure + invalid-args.
- **`tests/test_nonce_serialization_rc3.py`** — 5 tests for per-sender `asyncio.Lock` behaviour.

### SKILL.md

- **RULE 3a — First-person queries ALWAYS trigger recall.** rc.2 debug found the agent skipped `totalreclaw_recall` in 5/5 attempts on "Where do I live?". New hard rule: any first-person factual query ("where do I…", "my [noun]", "do I…") calls recall first. If recall returns 0, say so — don't invent.
- **RULE 10 — Filing QA bugs (RC builds only).** New section with the four triggers (repeated tool failure, user friction, setup errors, docs-vs-reality). Offer to file, never auto-file, never the same bug twice.
- **zai provider configuration** — new section under RULE 9 documenting the two endpoints and when to set `ZAI_BASE_URL`.

## [2.3.1rc2] — 2026-04-22

Follow-up RC for UX gaps flagged by Pedro's agent (Hermes) during
parallel Telegram testing alongside the plugin 3.3.1-rc.1 QA. Ships as
part of the unified rc.2 wave (plugin 3.3.1-rc.2 + Hermes 2.3.1rc2).
All 2.3.1rc1 work is preserved.

### Added

- **`totalreclaw` standalone CLI** (new console script). Users who
  install `pip install totalreclaw` outside of Hermes now have a
  first-class entry point. Two subcommands:
    - `totalreclaw setup` — delegates to the shared `hermes setup`
      wizard so both binaries behave identically. Default silent-save
      mode (see below) + optional `--emit-phrase` opt-in.
    - `totalreclaw doctor` — health check across 7 dimensions
      (credentials exist + parse, mnemonic valid, Smart Account
      resolved/cached, embedding model cached, LLM provider keys
      present, Hermes plugin registered, relay reachable). Coloured
      output when stdout is a TTY; exit 0 = healthy, 1 = warnings,
      2 = setup not started.

- **Post-install onboarding pointer** — when users run `totalreclaw`
  with no subcommand on a setup-less machine, they see a helpful
  "run `totalreclaw setup`" message instead of a bare argparse help.

- **Eager Smart Account resolution in `hermes setup` / `totalreclaw
  setup`** — after writing credentials.json, the wizard derives the
  CREATE2 Smart Account address via a one-shot RPC call and merges it
  back into credentials.json as `scope_address`. Means subsequent
  status / doctor / agent-tool calls see the real address instead of
  "pending". Best-effort: a missing network is non-fatal and prints a
  warning pointing at the "will be derived on first remember/recall"
  fallback.

- **Embedding-model download progress banner** (`embedding.py`) —
  before the first call to Harrier, we print a single-line stderr
  banner: `[TotalReclaw] Downloading embedding model from HuggingFace
  (~216 MB, one-time)…`. We also enable huggingface_hub's built-in
  progress bar so users see bytes moving. Suppressable via
  `TOTALRECLAW_QUIET_EMBEDDING_BANNER=1` for CI.

- **Hermes plugin SKILL.md** (`python/src/totalreclaw/hermes/SKILL.md`)
  — agent-directive document shipped with the plugin. Tells the agent
  exactly when to call each tool + enforces RULE 0 (recovery-phrase
  handling). Replaces the ambiguity in rc.1 where the agent (a) didn't
  know it had setup authority via `totalreclaw_onboarding_start`, and
  (b) occasionally echoed phrases back despite the security rules.

- **`totalreclaw_onboarding_start` entry in plugin.yaml** — was
  implemented in `tools.py` for rc.1 but not listed in the plugin
  manifest. Agents now discover it via the standard plugin surface.

### Changed

- **Silent-save by default in `setup` generate flow.** Pedro's agent
  flagged in the Hermes Telegram QA that rc.1 printed the generated
  BIP-39 phrase to stderr in a 4x3 grid — "for a secrets management
  plugin, this feels ironic". Terminal recordings, screen-shares, and
  shoulder-surfers defeat the E2EE promise. rc.2 default: the phrase
  is written to credentials.json (mode 0600), NEVER displayed. The
  post-setup banner points the user at
  `cat ~/.totalreclaw/credentials.json | jq -r .mnemonic` for
  retrieval when they need it. Behaviour change is gated by a new
  `--emit-phrase` flag for power users who genuinely want the rc.1
  4x3-grid + last-3-words-confirmation flow.

- **`LOCAL_MODE_INSTRUCTIONS` / `REMOTE_MODE_INSTRUCTIONS`** now
  mention both `totalreclaw setup` (standalone) and `hermes setup`
  (Hermes-specific) so users know which binary to run based on how
  they installed.

### Preserved from rc.1

All of the 2.3.1rc1 onboarding work carries forward:
- `detect_first_run`, `maybe_emit_welcome`, canonical copy constants.
- `hermes setup` wizard (restore + generate branches, non-TTY
  tolerance, overwrite confirmation).
- `totalreclaw.onboarding` first-run sentinel.
- All existing tools, hooks, and the Retrieval v2 Tier 1 reranker.

## [2.3.1] - 2026-04-20

First-run onboarding UX parity with plugin 3.3.0. Users switching between
OpenClaw (plugin) and Hermes (Python) now see the same welcome, the same
branch question, and the same canonical "recovery phrase" terminology.

### Added

- `totalreclaw.onboarding` (new module): synchronous, no-network
  first-run detection + welcome copy.
  - `detect_first_run(credentials_path)` — returns ``True`` when the
    credentials file is missing, empty, malformed, or lacks a
    recognised credentials key. Accepts both canonical ``mnemonic``
    and legacy ``recovery_phrase`` keys on read (same back-compat
    pattern as ``agent/state.py``).
  - `build_welcome_message(mode)` — renders the full welcome +
    branch-question copy for either ``local`` or ``remote`` mode.
  - `detect_mode(relay_url)` — classifies the invocation context from
    ``TOTALRECLAW_SERVER_URL`` / loopback hostnames / explicit
    ``TOTALRECLAW_LOCAL_GATEWAY`` env flags.
  - `maybe_emit_welcome(...)` — once-per-process emission guarded by
    a module-level flag AND a best-effort sentinel file at
    ``~/.totalreclaw/.welcome_shown`` so repeat command invocations
    for a first-run user who deferred setup don't re-spam the banner.
  - Verbatim copy constants exported at module level:
    ``WELCOME_MESSAGE``, ``BRANCH_QUESTION``, ``LOCAL_MODE_INSTRUCTIONS``,
    ``REMOTE_MODE_INSTRUCTIONS``, ``STORAGE_GUIDANCE``,
    ``RESTORE_PROMPT``, ``GENERATED_CONFIRMATION``. Tests assert
    byte-identity so future cross-client drift is caught.

- `hermes` CLI (new console script via ``[project.scripts]``) —
  interactive onboarding wizard mirroring plugin 3.3.0's
  ``openclaw totalreclaw onboard``:
  - `hermes setup` subcommand — asks the branch question, then
    branches into a restore flow (12-word paste, BIP-39 checksum
    validation via ``eth_account.Account.from_mnemonic``) or a
    generate flow (fresh 12-word phrase, prints STORAGE_GUIDANCE
    before showing the phrase grid, prints GENERATED_CONFIRMATION
    after a last-3-words confirmation challenge).
  - Overwrite guard: if credentials already exist, the wizard
    prompts ``Account already set up at <path>. Overwrite? [y/N]``
    and defaults to ``N``.
  - Phrase banner goes to stderr so ``hermes setup > log.txt`` does
    NOT capture the phrase into the log.
  - Non-TTY stdin is tolerated (scripted installers work) but the
    restore flow prints a clear warning that the phrase was visible.

### Changed

- `TotalReclaw.__init__` now accepts a ``suppress_welcome`` kwarg and
  emits the 2.3.1 welcome message the first time a client is
  constructed on a machine without credentials AND without an explicit
  ``recovery_phrase`` / ``mnemonic`` argument. The welcome surface
  points the user at ``hermes setup``. Backward-compatible: callers
  who pass a phrase never see the welcome, and ``suppress_welcome=True``
  opts out entirely.

- `totalreclaw.hermes.register(ctx)` calls ``maybe_emit_welcome`` once
  at plugin load time so Hermes-launched sessions on a clean machine
  also surface the welcome.

- `python/README.md`: updated the "End-to-end encrypted" bullet to say
  ``BIP-39 recovery phrase`` instead of ``BIP-39 mnemonic`` —
  matches the 2.3.1 canonical user-facing terminology. Internal
  variable names, docstrings, and plugin-side ``mnemonic`` references
  (e.g. ``derive_keys_from_mnemonic``) are unchanged — the sweep is
  scoped to output strings only.

- `python/pyproject.toml`: added ``[project.scripts]`` entry for the
  ``hermes`` console script, version bumped ``2.3.0`` → ``2.3.1``.

- `python/src/totalreclaw/__init__.py`: updated ``__version__``
  fallback to ``"2.3.1"`` for editable installs where
  ``importlib.metadata`` can't resolve the installed version.

- `python/src/totalreclaw/hermes/plugin.yaml`: bumped
  ``version: 2.3.0`` → ``2.3.1``.

### Tests

- `python/tests/test_onboarding.py` (new, 12 tests): detect_first_run
  across missing / empty / invalid-JSON / valid-credentials / legacy
  ``recovery_phrase``-keyed files; ``build_welcome_message`` local +
  remote contents; ``detect_mode`` env-flag + URL classification;
  module-level copy-constant parity; terminology sweep AST walker
  that asserts no ``mnemonic`` / ``seed phrase`` / ``recovery code`` /
  ``recovery key`` string literals are passed to ``print`` /
  ``click.echo`` / ``logger.warning`` / ``logger.error`` call sites
  under ``python/src/**/*.py``.
- `python/tests/test_hermes_setup_cli.py` (new, 8 tests): happy-path
  restore (mocked stdin 12 words → credentials.json written), happy-path
  generate (mocked last-3-words confirmation → STORAGE_GUIDANCE +
  GENERATED_CONFIRMATION printed + credentials written), overwrite-
  confirmation reject, invalid 12-word input rejected, non-TTY stdin
  tolerated in both flows, phrase-never-leaves-stderr assertion on the
  generate flow.

### Compatibility

- No breaking changes to public Hermes API. All existing tool
  signatures, schema shapes, and client constructor parameters remain
  unchanged. ``suppress_welcome`` is a new keyword-only parameter with
  a back-compat default.
- The welcome message is emitted AT MOST ONCE per process (module-level
  flag) AND at most once per host until the user completes setup
  (sentinel file). No new noise for returning users.

## [2.3.0] - 2026-04-19

### Changed

- **`EXTRACTION_SYSTEM_PROMPT` + `COMPACTION_SYSTEM_PROMPT` now sourced
  from the Rust core** via the new `totalreclaw_core.get_extraction_system_prompt`
  / `get_compaction_system_prompt` accessors (core 2.2.0+). The module-level
  names + exported symbols in `totalreclaw.agent` are unchanged — existing
  importers and the `test_v1_taxonomy.py` assertions keep working — but the
  literal string contents now come from a single canonical source embedded
  in `totalreclaw-core` via `include_str!`. This closes the cross-client
  prompt-drift gap that the 2026-04-18 v1 QA surfaced (NanoClaw
  `BASE_SYSTEM_PROMPT` was missing the Rule 6 meta-filter and mis-listed
  `summary` in the ADD output shape). The TS plugin still keeps a local
  copy for this release wave — the plugin consumer wire lands in a
  follow-up (plugin 3.3.0) to avoid conflicting with the parallel
  pin-atomic-batch (3.2.2) and wave2c (3.2.3) version bumps.

### Compatibility

- `totalreclaw-core>=2.2.0,<3.0.0` is now the hard dependency floor
  (was `>=2.0.0`). Pre-2.2.0 core wheels do NOT export the prompt
  accessors; `agent/extraction.py` imports them at module load so the
  floor bump is load-bearing. `pip install totalreclaw==2.3.0` will
  resolve a core 2.2.0+ wheel automatically.
- Plugin.yaml bumped to 2.3.0 (previously diverged at 2.2.1 — PR #56
  did not bump it; corrected here).
- Prompts are byte-identical to 2.2.2's literal constants. This is
  explicitly tested via the existing `test_extraction_prompt_mentions_v1_types`
  / `test_extraction_system_prompt_is_merged_topic` /
  `test_compaction_prompt_admits_floor_5` suite — assertions continue
  to pass unchanged.
## [2.2.4] - 2026-04-19

Wave 2c cleanup: expose `totalreclaw.__version__` at the package top-level
so `import totalreclaw; print(totalreclaw.__version__)` works. Sourced from
`importlib.metadata` when the package is installed, falls back to the
hardcoded `"2.2.4"` string in editable / source-tree installs where
metadata may not be available.

### Added

- `python/src/totalreclaw/__init__.py` — `__version__` exported via
  `importlib.metadata.version("totalreclaw")` with a `"2.2.4"` fallback;
  added to `__all__`.

### Tests

- `python/tests/test_version.py`: 4 assertions — non-empty string, semver
  shape, presence in `__all__`, importable via `from totalreclaw import
  __version__`.
## [2.2.3] - 2026-04-20

Pin/unpin made atomic — patch. Fixes the Hermes 2.2.2 staging QA
finding where pin operations occasionally stalled in Pimlico's
mempool mid-operation, leaving the user's fact tombstoned on-chain
with no pinned replacement ever surfacing.

### Fixed

- **Pin/unpin atomic on-chain write.** `_change_claim_status`
  (which backs both `pin_fact` and `unpin_fact`) pre-2.2.3 issued
  two sequential `build_and_send_userop` calls at nonces N and N+1:
  one for the tombstone, one for the new pinned blob. Pimlico's
  bundler occasionally accepted the nonce-N+1 UserOp (returning a
  hash) but then never propagated it past its mempool, leaving the
  user with a tombstoned old fact but no pinned replacement. This
  is observed on staging during the Hermes 2.2.2 QA pass
  (internal repo, issue #17).

  2.2.3 refactors the helper to emit a single batched UserOp via
  `build_and_send_userop_batch` (which wraps both protobuf payloads
  in one `SimpleAccount.executeBatch(...)` call). The on-chain
  shape is identical — the DataEdge contract emits one `Log(bytes)`
  event per call, and the subgraph indexes each by `(txHash,
  logIndex)` the same way as the pre-2.2.3 two-UserOp flow. What
  changes:
  - **Atomicity** — either both the tombstone AND the new v1 pinned
    blob land in the same block, or neither does. No more half-pin
    races.
  - **Nonce safety** — one nonce, one submission, one retry path.
    The AA25-retry behavior that previously applied per-UserOp now
    applies to the whole pin operation.
  - **Gas** — paymaster counts the pin as 1 UserOp rather than 2,
    and the base transaction cost is amortized across both calls.
  - **Latency** — one round-trip to Pimlico for gas + sponsorship
    + submission rather than two.

  The ordering within the batch is preserved: tombstone at index
  0, new fact at index 1 — matches `skill/plugin/pin.ts::executePinOperation`
  byte-for-byte, and plugin 3.2.2's parity test locks this in
  cross-client.

  **No API change.** `client.pin_fact()` / `client.unpin_fact()`
  signatures and return shapes are unchanged. A caller observes
  a single on-chain transaction hash instead of two, but the
  existing return contract (`{success, fact_id, new_fact_id, ...}`)
  carries no per-UserOp metadata so this is transparent.

### Added

- `python/tests/test_pin_batch_cross_impl_parity.py`: locks in
  byte-identical pin batch calldata between Python (PyO3) and
  plugin 3.2.2 (WASM) for identical pin inputs. Both paths delegate
  to the same shared-Rust `userop::encode_batch_call`, so byte
  parity is guaranteed at the ABI-encoding step — what the test
  actually guards is the pin-path payload construction (protobuf
  versions, field ordering, tombstone-vs-new-fact ordering in the
  batch).

### Changed

- `operations.py::_change_claim_status`: step 6 now calls
  `build_and_send_userop_batch(protobuf_payloads=[tombstone, new])`
  instead of two sequential `build_and_send_userop` calls. The
  docstring gains a "New in 2.2.3" block explaining the Pimlico
  mempool race.
- `pyproject.toml`: version bumped 2.2.2 → 2.2.3.

### Tests

- `python/tests/test_pin_unpin.py`: 26/26 pass. Existing assertions
  updated from "two sequential writes" (`mock_send.await_count == 2`)
  to "one batched write with two payloads"
  (`mock_send.await_count == 1` +
  `len(kwargs["protobuf_payloads"]) == 2`). Every other assertion
  is unchanged.
- `python/tests/test_wave2a_hermes_fixes.py`: 19/19 pass. The Bug
  #8 regression tests (v=4 new-fact payload, `pin_status=pinned`,
  v=3 tombstone) are preserved — they now inspect payloads inside
  the batch rather than across two separate submissions.
- `python/tests/test_pin_batch_cross_impl_parity.py`: 6/6 new
  tests pass.
- Full suite: 680 passed, 10 skipped, 1 xfailed — all pre-existing
  green.

### Related

- Plugin 3.2.2 (`skill/plugin/CHANGELOG.md`): matching parity test
  + cross-client byte-identity lock-in. No plugin code changes
  required (the plugin's pin path has been batched since 3.0.0).

## [2.2.2] - 2026-04-20

Wave 2a Hermes fix-up. Three bugs from the 2.2.1 VPS QA
([internal#14](https://github.com/p-diogo/totalreclaw-internal/pull/14),
`docs/notes/QA-hermes-RC-2.2.1-20260420.md`) — each would have been a
ship-stopper if left in a public release:

### Fixed

- **Bug #4 (HIGH) — `auto_extract` reads Hermes `config.yaml`.**
  Pre-2.2.2's `auto_extract` + post-extraction pipeline required
  `OPENAI_MODEL` to be set as an env var even when
  `~/.hermes/config.yaml` already carried `provider: zai` +
  `model: glm-5-turbo`. The 2.0.2 "fix" only wired the Hermes
  reader into the hooks layer; the generic `detect_llm_config` still
  read env vars exclusively, and the YAML reader expected a NESTED
  `model: {provider, model}` shape while Hermes actually writes
  top-level `provider:` + `model:` keys. 2.2.2:
  - `agent/llm_client.py::read_hermes_llm_config` handles BOTH YAML
    shapes and scans `$HERMES_CONFIG` → XDG → `~/.config/hermes/` →
    legacy `~/.hermes/`. Emits a WARN-level log line identifying the
    config path the model came from.
  - `detect_llm_config()` falls through to the Hermes reader when no
    env vars resolve. This is the path `extract_facts_llm` hits when
    no explicit `llm_config` is passed.
- **Bug #7 (SHIP-STOPPER) — `credentials.json` key parity with plugin 3.2.0.**
  Python pre-2.2.2 wrote `{"recovery_phrase": ...}` at
  `~/.totalreclaw/credentials.json`; plugin 3.2.0 writes
  `{"mnemonic": ...}` on the same canonical path. Cross-agent
  portability — a user switching from Hermes to OpenClaw without
  re-onboarding — was silently broken. 2.2.2:
  - `agent/state.py::_extract_mnemonic_from_creds` helper accepts
    BOTH keys on read, prefers canonical `mnemonic` when both present.
  - `configure()` write path now emits canonical `mnemonic` for
    fresh writes. Preserves legacy `recovery_phrase` shape when an
    existing file carries ONLY that key for the same mnemonic — no
    silent migration on touch.
  - Canonical decision documented in
    `docs/specs/totalreclaw/flows/01-identity-setup.md`.
- **Bug #8 (MEDIUM) — `pin_fact` emits v=4 `MemoryClaimV1` with `pin_status`.**
  Pre-2.2.2's `pin_fact()` wrote a v=3 tombstone but no companion v=4
  pinned claim — a pinned fact was invisible on the subgraph, so
  cross-client pin awareness was broken (other clients couldn't see
  the pin and the Tier-1 reranker's pin-aware ranking never fired).
  2.2.2 ports `skill/plugin/pin.ts::executePinOperation`:
  - `claims_helper.py::build_canonical_claim_v1` gains a
    `pin_status` parameter (validated against
    `VALID_PIN_STATUSES = ("pinned", "unpinned")`).
  - `operations.py::_change_claim_status` now always emits a fresh
    v1.1 blob (long-form `text`/`type`/`pin_status`/`superseded_by`)
    regardless of whether the source fact was v0 short-key or v1.
    New `FactPayload.version` is set to `PROTOBUF_VERSION_V4` so the
    outer protobuf tags the write as v1 taxonomy. Tombstone stays at
    v=3 (matches plugin behavior).
  - New `_project_source_to_v1` helper mirrors the plugin's
    `projectToV1` function-for-function — v0 sources upgrade on the
    fly (short-key `c` → v1 `type`, `sa` heuristics → v1 `source`).

### Tests

- `tests/test_wave2a_hermes_fixes.py`: 19 new tests — 7 Bug #4, 7 Bug #7
  (5 parity + 2 cross-client), 3 Bug #8, 2 cross-client portability.
- `tests/test_pin_unpin.py`: 2 tests updated to assert on the new v1.1
  long-form shape (prior assertions encoded the buggy pre-2.2.2
  short-key contract).
- Full Python suite: 678 passing, 10 skipped, 1 xfailed — no regressions.

### Known limitations

- The installed `totalreclaw-core==2.1.0` PyPI wheel doesn't round-trip
  the v1.1 `pin_status` field through `validate_memory_claim_v1` (the
  Rust struct has it; the serde emit drops it). 2.2.2 reattaches
  `pin_status` after validation — same pattern as `schema_version` and
  `volatility` — so the fix ships independently of core. A future
  `totalreclaw-core` release (2.1.1 on npm already; PyPI pending) will
  round-trip the field natively and the reattach becomes a no-op.

## [2.2.1] - 2026-04-19

Wire `auto_extract` to `remember_batch`; realizes the ~8x extraction latency win
from 2.2.0. No new public API — internal call-site change only.

### Changed

- `agent/lifecycle.py::auto_extract` now submits ADD/UPDATE facts via
  `client.remember_batch()` in chunks of 15 instead of looping
  `client.remember()` per fact. DELETE and NOOP actions are unaffected.
  For a 15-fact extraction cycle this drops relay round-trips from 15
  separate UserOperations to 1, matching the ~60s → ~8s latency projection
  from the Gap 3 notes in 2.2.0.
- Per-fact error granularity is preserved: if `remember_batch` returns
  fewer IDs than facts, the missing ones are logged at WARNING level so
  the caller can diagnose. If the whole batch fails, each fact is logged
  individually.
- UPDATE tombstones (`client.forget(existing_fact_id)`) are still issued
  individually after the batch that stored the replacement, preserving the
  same ordering guarantee as the old loop.

### Tests

- +3 new tests (`tests/test_auto_extract_uses_batch.py`):
  `test_auto_extract_5_facts_calls_remember_batch_once`,
  `test_auto_extract_20_facts_calls_remember_batch_twice`,
  `test_auto_extract_partial_failure_logs_failed_facts`.
- Updated `tests/test_v1_hooks_integration.py` and
  `tests/test_hermes_plugin.py` to assert on `remember_batch` instead of
  `remember` for the auto-extraction path.
- Full suite: 640 passing, 9 skipped, 1 xfailed.

## [2.2.0] - 2026-04-19

Hermes parity Gap 3: client-side batching. Drops 15-fact extraction
latency from ~60s to ~8s by submitting one ERC-4337 UserOperation
per extraction cycle instead of N sequential ones.

### Added

- `TotalReclaw.remember_batch(facts)` — public async API that stores up
  to 15 facts in a single UserOperation via
  `SimpleAccount.executeBatch(...)`. Paymaster / bundler / inclusion
  costs paid once.
- `operations.store_fact_batch(facts, ...)` — internal batch path that
  mirrors `store_fact` per-fact (same encryption, trapdoor generation,
  v1 canonical claim, protobuf v4 wrapper), then wraps all N payloads
  into one UserOp.
- `userop.build_and_send_userop_batch(...)` — batched UserOp submitter
  mirroring `build_and_send_userop` with the same AA25/AA10 retry loop.
- `userop.encode_execute_batch_calldata_for_data_edge(payloads)` +
  `userop.MAX_BATCH_SIZE` (= 15) — thin wrappers around the Rust core's
  `totalreclaw_core.encode_batch_call`, byte-identical to the TS
  plugin's `encodeBatchCalls`.
- `tests/test_userop_batch.py` — byte-match parity fixtures for N = 1 /
  3 / 5 / 10 / 15, empty-batch + oversize-batch validation, mocked
  relay retry tests, and an optional staging-integration test (runs
  only when `TOTALRECLAW_STAGING_INTEGRATION=1`).
- `tests/fixtures/batch_calldata_vectors.{py,json}` — fixture generator
  + baked expected-calldata vectors from the shared Rust core.

### Notes

- Part of the Hermes parity roadmap
  ([`docs/plans/2026-04-18-hermes-parity-roadmap.md`][hermes-parity],
  Gap 3). Closes the UX cliff where auto-extraction after a long
  conversation appeared to freeze the agent for 45–75s.
- The `agent/lifecycle.py::auto_extract` store loop is still per-fact
  on disk in this release — wiring it to `remember_batch` lives in a
  separate follow-up so Phase A (Hermes plugin / adapters) and Gap 3
  (batching) could merge independently. The new public API is fully
  shipped and importable today.
- `encode_batch_call` in the Rust core folds a batch of 1 back to
  `execute(...)` rather than `executeBatch(...)`, so a 1-element batch
  is byte-identical to the single-fact path. No correctness penalty
  for callers that unconditionally batch.

## [2.1.0] - 2026-04-19

Phase A of the Hermes parity roadmap
([docs/plans/2026-04-18-hermes-parity-roadmap.md][hermes-parity]). Closes the
three lowest-effort / highest-visibility gaps between the Python client's
Hermes plugin and the OpenClaw + MCP reference implementations. Feature
release per semver (new public tool surface; no breaking changes).

### Added

- **`totalreclaw_upgrade` tool** (Hermes) — creates a Stripe Checkout
  session via `RelayClient.create_checkout()` and returns the URL for the
  user to complete payment for the Pro tier. Mirrors
  `mcp/src/tools/upgrade.ts`, except the Python client already knows its
  own wallet address so the tool schema has no required arguments. The
  description follows the Phase 2 (v2.0.2) style with explicit
  user-utterance hints ("upgrade to Pro", "I hit the free limit",
  "unlimited") to help the agent invoke it correctly.

- **`totalreclaw_debrief` tool** (Hermes) — explicit-invocation form of
  the session-end debrief. Reuses
  `totalreclaw.agent.lifecycle.session_debrief` (the same function the
  auto `on_session_end` hook calls), so the stored summary facts are
  indistinguishable from the auto-flow output (`type=summary`,
  `provenance=derived`, `scope=unspecified`). The tool returns the stored
  count + `fact_ids` so the agent can confirm. Short sessions
  (< 4 turns) short-circuit with a clear `skipped=true` response.

- **Mem0 import adapter** (`totalreclaw.import_adapters.mem0_adapter`) —
  structural port of `skill/plugin/import-adapters/mem0-adapter.ts`.
  Parses the three canonical Mem0 JSON shapes (dashboard export
  `{memories: [...]}`, API response `{results: [...]}`, bare array) and
  emits pre-structured `NormalizedFact`s that flow through the existing
  `ImportEngine` without LLM re-extraction. Category mapping is
  byte-identical to the TS adapter. `get_adapter('mem0')` + `list_sources()`
  now include the new source.

### Changed

- `totalreclaw.agent.lifecycle.session_debrief(state, stored_fact_texts=None)`
  now returns `list[str]` of stored debrief fact ids instead of `None` so
  the new `totalreclaw_debrief` tool can surface them back to the user.
  The auto `on_session_end` hook ignores the return value — this is a
  behaviour-compatible widening.

- `totalreclaw.hermes.plugin.yaml` version bumped `2.0.2` → `2.1.0` and
  the two new tools added to `provides_tools`.

- `hermes/__init__.py::register()` now registers 10 tools (was 8): the
  existing 8 plus `totalreclaw_upgrade` + `totalreclaw_debrief`.

### Tests

- +33 new tests (`test_upgrade_tool.py` 7, `test_debrief_tool.py` 8,
  `test_mem0_adapter.py` 18). Full suite now 637 passing, 4 skipped,
  1 xfailed.

### Notes

- Gap 3 (`remember_batch` + Python batcher) is tracked in a parallel
  agent worktree; those files (`userop.py`, `operations.py`, `client.py`,
  `agent/extraction.py`) are deliberately untouched in this release.
- The Mem0 adapter's optional live-API fetch path is intentionally
  skipped for Phase A — users export JSON from the Mem0 dashboard and
  paste or point-to it. Live-API ingestion is a potential Phase B
  follow-up.

[hermes-parity]: https://github.com/p-diogo/totalreclaw-internal/blob/main/docs/plans/2026-04-18-hermes-parity-roadmap.md

## [2.0.2] - 2026-04-18

Phase 2 of the v1.0.x stabilization wave. Plugin-layer fixes flagged by
the v1.0.0 QA run (`docs/notes/QA-V1CLEAN-VPS-20260418.md`).

### Fixed

- **Event-loop lifecycle** — `RelayClient` now caches `httpx.AsyncClient`
  per event loop so the Python client works both from Hermes's async
  runtime AND its sync-hook sidecars (`pre_llm_call`). Previous behavior
  raised "Event loop is closed" on the second loop.

- **LLM auto-detect surfaces visible errors** — when no LLM config
  resolves, `extract_facts_llm` / `extract_facts_compaction` now warn at
  WARNING level with actionable guidance, and `post_llm_call` surfaces a
  one-time quota-channel warning so the user sees an explanation in
  their next assistant turn.

- **Auto-setup detection** — rewrote `REMEMBER` / `RECALL` tool
  descriptions so the LLM prefers TotalReclaw over Hermes's built-in
  `memory` tool, and added a one-time setup-nudge when a memory-related
  message arrives before `totalreclaw_setup` has run.

- **In-batch cosine dedup** — `deduplicate_facts_by_embedding` now
  collapses near-identical facts both against `existing_memories` AND
  against earlier facts in the same extraction batch.

- **Spurious extraction of setup meta-content** — `is_product_meta_request`
  + `_filter_product_meta_facts` filter "set up TotalReclaw" / "install
  the memory plugin" utterances before they reach the vault as "user
  preferences". Genuine preferences still pass through.

- **Export / session-id / chain-id auto-detect** — export path, session
  header forwarding, and Pro-tier chain-100 auto-detect all stabilized.

## [2.0.1] and earlier

See git history — pre-v1 stabilization patches.
