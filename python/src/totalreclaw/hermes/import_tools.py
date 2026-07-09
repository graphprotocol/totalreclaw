"""Import / disclosure / export-URL subsystem for the Hermes plugin.

Extracted from :mod:`totalreclaw.hermes.tools` — that module kept the whole
memory-import capability (source adapters wiring, the privacy-disclosure gate,
export-URL download + redirect validation, and the four ``totalreclaw_import_*``
tool handlers) inline alongside the core CRUD handlers, pushing it past 1,600
lines. The import surface now lives here; ``tools`` re-exports every public and
private symbol below so ``tools.<name>`` attribute access is unchanged.

**Patchability contract.** The test suite monkeypatches import collaborators on
the ``tools`` module (e.g. ``monkeypatch.setattr(tools, "_make_extractor", …)``
and ``patch("totalreclaw.hermes.tools.remember")``). Because ``tools``
re-exports the names defined here, those patches rebind the ``tools``-module
binding only — not the local binding a caller in *this* module would resolve.
So the cross-function calls that tests patch (``_make_extractor``,
``_make_llm_completion``, ``_disclosure_consent_ok``, ``_fetch_export_url``,
``_extraction_provider_label``, and ``remember`` — which stays in ``tools``) are
routed back through the ``tools`` module (resolved lazily to sidestep the
``tools`` <-> ``import_tools`` import cycle). This keeps every existing
``setattr(tools, …)`` site authoritative after the extraction, with no test
churn.
"""
from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import uuid

from . import consent_tokens
from dataclasses import asdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .state import PluginState

logger = logging.getLogger(__name__)

# Threshold for "small" imports that run synchronously in import_from.
# rc13 (#426): was 50 — a 34-chunk ChatGPT import blocked the agent turn for
# 7+ minutes and any client timeout aborted it mid-run. ~5 chunks ≈ a couple
# of minutes worst-case; everything larger backgrounds with an import_id ack.
_SMALL_IMPORT_THRESHOLD = 5

# Strong references to background import tasks so the event loop cannot GC them.
_BG_TASKS: set[asyncio.Task] = set()

# imp-2 (#244): sources whose import sends raw conversation text to an LLM
# for fact extraction. These REQUIRE the privacy disclosure (PRD-IMP G-4).
# mem0 / mcp-memory ship pre-structured facts — no conversation text ever
# reaches an LLM, so no disclosure applies.
_EXTRACTION_IMPORT_SOURCES = frozenset({"chatgpt", "claude", "gemini"})

# imp-2 (#244): hosts a memory export legitimately comes from. URLs on these
# hosts (or their subdomains) fetch without friction; anything else needs an
# explicit user confirmation first (phishing / malicious-download guard).
# Deliberately NOT here: bare `googleapis.com` — storage.googleapis.com is
# multi-tenant (anyone can host a bucket), so trusting it would let an
# attacker-controlled "export" fetch with no confirmation (review of PR #431).
_EXPORT_URL_ALLOWLIST = (
    "chatgpt.com",
    "openai.com",
    "takeout.google.com",
    "claude.ai",
    "anthropic.com",
)

_NOT_CONFIGURED = json.dumps(
    {"error": "TotalReclaw not configured. Call totalreclaw_pair to set up — browser-side crypto keeps the phrase out of this chat."}
)


def _dispatch_module():
    """The ``tools`` dispatch module — the authoritative monkeypatch surface.

    Resolved lazily (call-time import) so the ``tools`` <-> ``import_tools``
    cycle never bites regardless of which module is imported first. See the
    module docstring's patchability contract.
    """
    from totalreclaw.hermes import tools

    return tools


# ── LLM config helpers (import extraction) ───────────────────────────────────


def _read_hermes_llm_config():
    """Back-compat thin wrapper — delegates to the agent-package helper.

    Historically lived on the Hermes tools layer; moved to
    :func:`totalreclaw.agent.llm_client.read_hermes_llm_config` in
    ``totalreclaw`` 2.2.2 so the generic env-var fallback in
    :func:`detect_llm_config` can reuse the Hermes YAML + .env resolution
    without a circular plugin-layer import (Bug #4).
    """
    from totalreclaw.agent.llm_client import read_hermes_llm_config

    return read_hermes_llm_config()


def _make_extractor(state: "PluginState"):
    """Build an async LLM extraction callable using Hermes's own LLM config.

    Reads from ~/.hermes/config.yaml + ~/.hermes/.env to match the host
    agent's configured provider. Falls back to detect_llm_config() for
    non-Hermes environments.
    """
    from totalreclaw.agent.extraction import (
        EXTRACTION_SYSTEM_PROMPT,
        _truncate_messages,
        parse_facts_response,
    )
    from totalreclaw.agent.llm_client import detect_llm_config, chat_completion

    async def extract(
        messages: list[dict],
        timestamp: str,
        *,
        enriched_system_prompt: Optional[str] = None,
    ) -> list[dict]:
        """Hermes extraction callable.

        ``enriched_system_prompt`` (imp-4): when the ``ImportEngine``'s
        smart-import pipeline produced a profile-enriched system prompt,
        it's forwarded here so this LLM call sees the same profile
        context the plugin injects via ``runSmartImportPipeline``. Falls
        back to ``EXTRACTION_SYSTEM_PROMPT`` when unset.
        """
        # Try Hermes config first, fall back to env var detection
        config = _read_hermes_llm_config() or detect_llm_config()
        if not config:
            logger.warning("No LLM config found for extraction (checked Hermes config + env vars)")
            return []

        conversation_text = _truncate_messages(messages)
        if len(conversation_text) < 20:
            return []

        context_note = ""
        if timestamp:
            context_note = f"\n\n(Conversation timestamp: {timestamp})"

        user_prompt = (
            f"Extract ALL valuable long-term memories from this conversation:{context_note}\n\n"
            f"{conversation_text}"
        )

        system_prompt = enriched_system_prompt or EXTRACTION_SYSTEM_PROMPT
        response = await chat_completion(config, system_prompt, user_prompt)
        if not response:
            return []

        # v1 parser returns facts with source/scope/reasoning populated.
        parsed = parse_facts_response(response)
        return [
            {
                "text": f.text,
                "type": f.type,
                "importance": f.importance,
                "action": f.action,
                "source": f.source,
                "scope": f.scope,
                "reasoning": f.reasoning,
            }
            for f in parsed
        ]

    return extract


def _make_llm_completion(state: "PluginState"):
    """Build an async prompt-only LLM completion callable for smart-import.

    Mirrors ``_make_extractor`` but exposes a ``(prompt: str) -> str | None``
    surface for the smart-import profile + triage passes. The pipeline's
    prompts (from ``totalreclaw_core``) are self-contained and don't need
    a separate system instruction, so we pass them through as the user
    message with an empty system slot.

    Returns ``None`` (the *function*, not the *callable*) when no LLM
    config can be resolved at construction time so the caller wires
    ``llm_completion=None`` and ``ImportEngine`` falls back to blind
    extraction. The config lookup is repeated *inside* the closure so
    transient env changes (e.g. Hermes restart) are picked up.
    """
    from totalreclaw.agent.llm_client import detect_llm_config, chat_completion

    async def complete(prompt: str) -> Optional[str]:
        config = _read_hermes_llm_config() or detect_llm_config()
        if not config:
            logger.warning(
                "smart_import: no LLM config (checked Hermes config + env vars); "
                "profile/triage passes will fall back to blind extraction",
            )
            return None
        # Empty system prompt — the smart-import prompts built by
        # totalreclaw_core are self-contained (see smart_import.rs:178+).
        return await chat_completion(config, "", prompt)

    return complete


def _get_or_create_import_engine(state: "PluginState", source: str, file_path: Optional[str]):
    """Return a cached ``ImportEngine`` for ``(source, file_path)``, creating one if needed.

    The agent's manual paging flow drives multi-batch imports through repeated
    ``totalreclaw_import_batch`` tool calls (one per offset). Each call lands as
    a fresh tool invocation, so without this cache the engine — and its
    smart-import profile + session-assignment state — is rebuilt every call,
    paying the 17-25 min profiling pass per batch on large imports (#389).

    Only ``file_path`` imports are cached. ``content``-based imports have no
    stable cache key, so they keep the per-call engine (uncached) behaviour.
    """
    from totalreclaw.import_engine import ImportEngine

    _tools = _dispatch_module()

    cache = getattr(state, "_import_engines", None)
    if cache is None:
        cache = {}
        state._import_engines = cache

    cache_key = (source, file_path) if file_path else None
    if cache_key is not None:
        engine = cache.get(cache_key)
        if engine is not None:
            return engine

    engine = ImportEngine(
        client=state.get_client(),
        llm_extract=_tools._make_extractor(state),
        llm_completion=_tools._make_llm_completion(state),
    )
    if cache_key is not None:
        cache[cache_key] = engine
    return engine


async def _persist_import_memory(state: "PluginState", text: str, importance: float = 0.7) -> None:
    """Best-effort persist an import-tracking note to the TotalReclaw vault.

    #401: long-running imports outlive the conversation window. After Hermes
    context compaction the agent forgets an import is running / finished and
    stops monitoring. Writing the import id + status into the vault (via the
    standard remember path) means ``totalreclaw_recall("active import")`` or
    the ``pre_llm_call`` auto-recall surfaces it again — the import context
    flows through the same memory system the user already uses.

    Graceful no-op when TotalReclaw is not configured or the write fails:
    this is an observability aid, never load-bearing for the import itself.

    The issue spec proposed ``provider.remember_sync(...)``; that method does
    not exist on the real ``TotalReclawMemoryProvider`` surface (there is no
    ``get_provider(state)`` + ``remember_sync`` API). The closest equivalent
    is the shared ``tools.remember`` handler. Must be ``await``ed (not driven
    via ``run_sync``) because the call sites run inside the Hermes event loop
    — blocking the loop thread on the persistent sync runner would stall the
    background import task. ``force=True`` bypasses the dup-of-pending
    auto-extract suppression (this is an internal bookkeeping write, not a
    duplicate user fact).
    """
    try:
        if not state.is_configured():
            return
        # Routed through the tools module so patch("…tools.remember") applies.
        await _dispatch_module().remember(
            {"text": text, "importance": importance, "force": True},
            state,
        )
    except Exception as e:  # never block the import over a bookkeeping write
        logger.info("import-memory persist skipped: %s", e)


# ── Export-URL fetch + privacy disclosure ────────────────────────────────────


def _is_allowlisted_export_host(url: str) -> bool:
    """True when *url* is https on an allowlisted export host (or subdomain)."""
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    return any(
        host == allowed or host.endswith("." + allowed)
        for allowed in _EXPORT_URL_ALLOWLIST
    )


def _prior_disclosure_consent(source: str, resume_id: Optional[str] = None) -> bool:
    """True when a persisted import state already records disclosure consent.

    Checked by resume paths and by ``totalreclaw_import_batch`` (which the
    agent calls repeatedly after ``import_from`` recorded consent) so the
    user is never re-prompted mid-import.

    internal#418: a persisted consent is honored ONLY when the provider it was
    disclosed for (``ImportState.disclosure_provider``) still equals the
    CURRENT resolved provider label. The disclosure exists so the user knows
    which LLM reads their conversations in cleartext — a silent provider swap
    must re-fire it. An absent/None ``disclosure_provider`` (a pre-#418 record)
    is a mismatch → re-prompt once (safe direction: never silently authorize).
    """
    try:
        # Resolve through the tools module so the test suite's
        # ``setattr(tools, "_extraction_provider_label", …)`` is authoritative
        # (see the module docstring's patchability contract).
        current_provider = _dispatch_module()._extraction_provider_label()

        def _valid(st) -> bool:
            return bool(
                st.disclosure_confirmed
                and st.disclosure_provider is not None
                and st.disclosure_provider == current_provider
            )

        from totalreclaw import import_state as ist
        if resume_id:
            prior = ist.read_import_state(resume_id)
            if prior is not None:
                return _valid(prior)
        # #460: scan genuine state records ONLY. The previous inline glob of
        # ``*.json`` + ``data.get(...)`` crashed on the #436 conversation
        # registry ledger (``imported-conversations-<source>.json``, a JSON
        # LIST) — ``list.get`` raised AttributeError, which the narrow
        # ``except (OSError, ValueError)`` missed, bricking import batch 2 and
        # every re-import. ``iter_import_state_records`` excludes the ledgers
        # and yields only dict records (belt: exclude by name; braces: skip
        # non-dict payloads).
        for st in ist.iter_import_state_records():
            if st.source == source and _valid(st):
                return True
    except Exception:
        pass
    return False


def _current_disclosure_provider() -> str:
    """The LLM provider label the user would be (or was) shown right now.

    Routed through the ``tools`` module (the monkeypatch surface — see the
    module docstring) so the test suite's ``setattr(tools,
    "_extraction_provider_label", …)`` is authoritative. Used both by the
    disclosure message and to stamp ``ImportState.disclosure_provider`` when
    consent is recorded (internal#418).
    """
    return _dispatch_module()._extraction_provider_label()


# Disclosure tokens are one-time, hash-at-rest consent tokens (#437). The
# generic mint/redeem now live in ``hermes.consent_tokens`` (shared with the
# pair-replace guard, #466); these thin wrappers pin the ``"disclosure"`` kind
# and the token subject = the import ``source``.
_DISCLOSURE_TOKEN_TTL_S = consent_tokens.TOKEN_TTL_S


def _disclosure_token_hash(token: str) -> str:
    """SHA-256 (first 16 hex chars) of a disclosure token — the at-rest key."""
    return consent_tokens.token_hash(token)


def _mint_disclosure_token(source: str) -> str:
    """Mint a one-time token proving the disclosure response was received.

    rc13 (#421): rc12 QA showed the agent composing its OWN consent prompt and
    self-setting disclosure_confirmed=true — the tool's provider-naming
    disclosure never reached the user. The token forces at least one round-trip
    through the disclosure_required response before consent can be asserted.
    #437 (rc5): stored HASHED at rest (see :mod:`hermes.consent_tokens`).
    """
    return consent_tokens.mint("disclosure", source)


def _redeem_disclosure_token(source: str, token) -> bool:
    """Consume a pending disclosure token for *source*. One-time use; hashed
    lookup, 1h TTL (see :mod:`hermes.consent_tokens`)."""
    return consent_tokens.redeem("disclosure", source, token)


def _disclosure_consent_ok(source: str, args: dict, resume_id: Optional[str] = None) -> bool:
    """True when the user's disclosure consent is established for *source*.

    Consent = a persisted ImportState with disclosure_confirmed (resume /
    the import_from -> import_batch loop), OR disclosure_confirmed=true
    accompanied by a valid one-time disclosure_token (proving the tool's
    disclosure response was received this flow).
    """
    if source not in _EXTRACTION_IMPORT_SOURCES:
        return True
    if _prior_disclosure_consent(source, resume_id):
        return True
    return bool(args.get("disclosure_confirmed", False)) and _redeem_disclosure_token(
        source, args.get("disclosure_token")
    )


def _disclosure_required_response(source: str, import_id: str, estimate: Optional[dict] = None) -> str:
    """The disclosure_required payload (PRD-IMP G-4) — provider named explicitly."""
    # Routed through the tools module so setattr(tools, "_extraction_provider_label") applies.
    provider_label = _dispatch_module()._extraction_provider_label()
    token = _mint_disclosure_token(source)
    payload = {
        "disclosure_required": True,
        "import_id": import_id,
        "llm_provider": provider_label,
        "disclosure_token": token,
        "message": (
            "PRIVACY DISCLOSURE — relay the next two sentences to the user "
            "VERBATIM; do not paraphrase or substitute your own wording. "
            f"Importing this {source} export sends your past conversations "
            f"in cleartext to {provider_label} for fact extraction. "
            "TotalReclaw itself never sees plaintext — but that LLM "
            "provider does. Ask for explicit consent. Only after the user "
            "explicitly agrees, call again with disclosure_confirmed=true "
            f"AND disclosure_token=\"{token}\". If they decline, stop — "
            "nothing has been processed."
        ),
    }
    if estimate:
        payload["estimated_facts"] = estimate.get("estimated_facts")
        payload["estimated_minutes"] = estimate.get("estimated_minutes")
    return json.dumps(payload)


def _provider_name_from_base_url(base_url: str) -> str:
    """Map an LLM endpoint host to a human-readable provider name.

    ``LLMConfig`` carries no ``provider`` field (only base_url/model/…), so
    the disclosure name is derived from the endpoint host. Unknown hosts
    fall back to the bare hostname so the disclosure still names *something*
    concrete rather than a generic placeholder.
    """
    if not base_url:
        return "your configured LLM provider"
    lower = base_url.lower()
    if "z.ai" in lower or "bigmodel" in lower:
        return "z.ai (GLM)"
    if "openai.com" in lower:
        return "OpenAI"
    if "anthropic.com" in lower:
        return "Anthropic"
    if "groq" in lower:
        return "Groq"
    from urllib.parse import urlparse
    host = urlparse(base_url).hostname or ""
    return host or "your configured LLM provider"


def _extraction_provider_label() -> str:
    """Human-readable name of the LLM that will read conversation text.

    The privacy disclosure must name the provider explicitly (PRD-IMP G-4;
    internal#437). ``LLMConfig`` has no ``provider`` field, so we derive the
    provider name from ``base_url`` and pair it with the model. Resolves the
    same config chain ``_make_extractor`` uses.
    """
    try:
        from totalreclaw.agent.llm_client import read_hermes_llm_config, detect_llm_config
        config = read_hermes_llm_config() or detect_llm_config()
        if config:
            base_url = getattr(config, "base_url", "") or ""
            model = getattr(config, "model", "") or ""
            provider_name = _provider_name_from_base_url(base_url)
            if model:
                return f"{provider_name} — model {model}"
            return provider_name
    except Exception:
        pass
    return "your configured LLM provider"


def _make_redirect_validator(require_allowlist: bool):
    """Redirect handler that re-validates every hop (review of PR #431).

    urllib's default handler follows redirects to ANY http/https URL, so an
    open redirect on a trusted host could bypass the allowlist entirely.
    - require_allowlist=True (initial URL was allowlisted): every hop must
      also be allowlisted.
    - require_allowlist=False (user explicitly confirmed a non-allowlisted
      host): hops may go anywhere https, but a cleartext http downgrade is
      always blocked.
    """
    import urllib.error
    import urllib.request

    class _ValidatedRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            from urllib.parse import urlparse
            if require_allowlist:
                if not _is_allowlisted_export_host(newurl):
                    raise urllib.error.URLError(
                        f"Redirect to non-allowlisted URL blocked: {newurl}"
                    )
            elif urlparse(newurl).scheme != "https":
                raise urllib.error.URLError(
                    f"Redirect off https blocked: {newurl}"
                )
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    return _ValidatedRedirectHandler


def _fetch_export_url(url: str, *, require_allowlist: bool = True) -> str:
    """Download an export archive to a temp file and return its path.

    Enforces the 500MB import cap during streaming so a hostile or
    mis-linked URL can't fill the disk, and re-validates redirects (see
    ``_make_redirect_validator``). The temp file is kept for the lifetime
    of the import — resume (US-7) re-reads it by path.
    """
    import tempfile
    import urllib.request
    from urllib.parse import urlparse

    cap = 500 * 1024 * 1024
    suffix = os.path.splitext(urlparse(url).path)[1] or ".download"
    req = urllib.request.Request(url, headers={"User-Agent": "totalreclaw-import"})
    opener = urllib.request.build_opener(_make_redirect_validator(require_allowlist)())
    with opener.open(req, timeout=120) as resp:
        length = resp.headers.get("Content-Length")
        if length and int(length) > cap:
            raise ValueError(f"Download is {int(length) / (1024*1024):.0f}MB — exceeds the 500MB import cap.")
        fd, tmp_path = tempfile.mkstemp(prefix="totalreclaw-import-", suffix=suffix)
        written = 0
        try:
            with os.fdopen(fd, "wb") as out:
                while True:
                    block = resp.read(1 << 20)
                    if not block:
                        break
                    written += len(block)
                    if written > cap:
                        raise ValueError("Download exceeds the 500MB import cap.")
                    out.write(block)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    return tmp_path


# ── #457a: fail-loud import entry point ────────────────────────────────────


def _estimate_failure_errors(estimate: dict) -> Optional[list]:
    """Return a loud-fail error list when an import estimate is UNUSABLE.

    rc4 QA (#457a): a real ``import_from`` with a typo'd path produced
    ``status:completed, total_chunks:0, facts_stored:0, errors:[]`` — a silent
    failure presented as success, which then leaked a false "import finished,
    0 memories" note into agent context. An import is unusable when the
    adapter reported parse errors (e.g. file-not-found) OR the parse yielded
    nothing to import (0 conversations AND 0 facts). Returns None when the
    estimate is importable.
    """
    errs = [str(e) for e in (estimate.get("errors") or [])]
    if errs:
        return errs
    total_chunks = estimate.get("total_chunks") or 0
    total_facts = estimate.get("total_facts") or 0
    if total_chunks == 0 and total_facts == 0:
        return [
            "No importable content found (0 conversations and 0 facts). "
            "Check the source path/format is correct and try again."
        ]
    return None


def _import_failed_response(source: str, import_id: str, errors: list) -> str:
    """A loud error payload for an unusable import — NEVER a completed-empty
    success. The adapter errors are surfaced verbatim so the agent can relay
    the real cause (file-not-found, unparseable export, …)."""
    return json.dumps({
        "error": "import_failed",
        "status": "failed",
        "source": source,
        "import_id": import_id,
        "errors": errors,
        "message": (
            "Import failed — nothing was extracted or stored. "
            + " ".join(errors)
        ),
    })


# ── #457b: orphaned background-import reaping ──────────────────────────────
#
# In a one-shot ``hermes chat -q`` invocation the spawned asyncio task dies
# with the process, leaving the state file stuck at ``running`` 0/N. We record
# every import id this process backgrounded and, at interpreter exit, mark any
# still-``running`` record ``failed`` with a resume hint. Bounded + best-effort.
_SPAWNED_IMPORT_IDS: set[str] = set()
_ORPHAN_ATEXIT_REGISTERED = False


def _fail_orphaned_imports_on_exit() -> None:
    """atexit hook: fail this process's still-running background imports."""
    try:
        from totalreclaw.import_state import (
            read_import_state, write_import_state, ImportState,
        )
    except Exception:
        return
    # Read via the tools dispatch module so a test's ``setattr(tools,
    # "_SPAWNED_IMPORT_IDS", …)`` is authoritative (patchability contract).
    for iid in list(_dispatch_module()._SPAWNED_IMPORT_IDS):
        try:
            s = read_import_state(iid)
            if s is not None and s.status == "running":
                write_import_state(ImportState(**{
                    **asdict(s),
                    "status": "failed",
                    "errors": s.errors + [
                        "process exited before the import completed — resume "
                        f"with resume_id {iid}"
                    ],
                }))
        except Exception:
            pass


def _register_spawned_import(import_id: str) -> None:
    """Track a backgrounded import id + register the atexit reaper once."""
    global _ORPHAN_ATEXIT_REGISTERED
    _SPAWNED_IMPORT_IDS.add(import_id)
    if not _ORPHAN_ATEXIT_REGISTERED:
        try:
            atexit.register(_fail_orphaned_imports_on_exit)
        except Exception:
            pass
        _ORPHAN_ATEXIT_REGISTERED = True


# ── #457b review Finding 1: heartbeat so a live-but-slow batch isn't reaped ─
#
# ``is_import_early_stale`` keys on ``last_updated``, which the background loop
# only writes at BATCH BOUNDARIES — so it stays frozen at spawn time for the
# whole of batch 0. A healthy first batch (25 chunks × receipt-confirmed
# byte-capped groups, each store waiting up to 240s) can exceed the 10-min
# early-stale window, which would false-reap a LIVE import as an orphan — and
# then (because the loop only stopped on "aborted" and checkpoints re-inherit
# the old status) the still-running task would keep writing on-chain while the
# record reports failed forever. The heartbeat refreshes ``last_updated`` every
# ~60s WHILE a batch runs: a live process keeps its record fresh; a dead one
# doesn't; early-stale then reaps only true orphans.
_HEARTBEAT_INTERVAL_S = 60


async def _import_heartbeat(import_id: str, interval: Optional[float] = None) -> None:
    """Refresh a ``running`` import's ``last_updated`` on a fixed interval.

    Stops itself the moment the record is reaped (status flipped off
    ``running``) or removed — it never resurrects a reaped import.
    """
    from totalreclaw.import_state import read_import_state, write_import_state
    # Resolve through the tools dispatch module so ``setattr(tools,
    # "_HEARTBEAT_INTERVAL_S", …)`` in tests is authoritative (patchability
    # contract — see module docstring).
    iv = _dispatch_module()._HEARTBEAT_INTERVAL_S if interval is None else interval
    try:
        while True:
            await asyncio.sleep(iv)
            s = read_import_state(import_id)
            if s is None or s.status != "running":
                return
            write_import_state(s)  # write_import_state re-stamps last_updated=now
    except asyncio.CancelledError:
        return


async def _process_batch_with_heartbeat(engine, import_id: str, **batch_kwargs):
    """Run one ``engine.process_batch`` with a concurrent heartbeat that keeps
    the import record fresh for the (possibly multi-minute) duration of the
    batch. The heartbeat is always cancelled when the batch returns."""
    beat = asyncio.ensure_future(_import_heartbeat(import_id))
    try:
        return await engine.process_batch(**batch_kwargs)
    finally:
        beat.cancel()
        try:
            await beat
        except BaseException:
            pass


# ── import_from stages ───────────────────────────────────────────────────────


def _patch_import_state(prior, **changes) -> None:
    """Rewrite ``prior`` (an ``ImportState``) with field overrides and persist.

    Factors the repeated ``write_import_state(ImportState(**{**asdict(s), …}))``
    boilerplate that the import checkpointing sprinkled across the small-import,
    background-batch, and completion paths.
    """
    from totalreclaw.import_state import ImportState, write_import_state

    write_import_state(ImportState(**{**asdict(prior), **changes}))


def _gate_url_input(
    url: str,
    url_confirmed: bool,
    source: str,
    import_id: str,
    dry_run: bool,
    consent_ok: bool,
):
    """URL-input gate (imp-2): allowlist check, disclosure-before-download, fetch.

    Returns ``(file_path, early_response)`` where exactly one is non-None:
    a resolved local ``file_path`` to continue with, or an early-return JSON
    string (url-confirmation prompt, disclosure prompt, or download error).
    """
    _tools = _dispatch_module()
    url_allowlisted = _is_allowlisted_export_host(url)
    if not url_allowlisted and not url_confirmed:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or url) if url else url
        return None, json.dumps({
            "url_confirmation_required": True,
            "host": host,
            "import_id": import_id,
            "message": (
                f"⚠️ This URL is from `{host}`, which I don't recognize as a "
                "trusted memory-export host. Fetching it could expose you to "
                "phishing or download a malicious file. Ask the user to "
                "confirm they trust this source, then call again with "
                "url_confirmed=true. Nothing was downloaded."
            ),
        })
    # Disclosure comes BEFORE the download for gated sources (review of
    # PR #431 finding 4): don't spend a multi-hundred-MB fetch on a call
    # that is about to return disclosure_required anyway. No estimate is
    # available at this point — the agent's dry_run already surfaced it.
    if not dry_run and not consent_ok:
        return None, _disclosure_required_response(source, import_id)
    try:
        file_path = _tools._fetch_export_url(url, require_allowlist=url_allowlisted)
    except Exception as e:
        return None, json.dumps({"error": f"Failed to download export from URL: {e}"})
    return file_path, None


async def _check_import_tier_gate(client, import_id: str, estimate: dict) -> Optional[str]:
    """Tier gate (PRD-IMP §6: import is a Pro-only feature).

    Interim client-side UX gate: a free-tier user hits the upgrade wall
    BEFORE any LLM extraction or on-chain write, so they aren't charged
    cost/quota by surprise. The relay still enforces the real write quota
    server-side. Fails OPEN if billing is unreachable (self-hosted / offline)
    so those flows are not blocked. Returns the blocked JSON, or None to
    proceed.
    """
    try:
        billing = await client.status()
        tier = (getattr(billing, "tier", "") or "").strip().lower()
    except Exception:
        tier = ""
    if tier and tier != "pro":
        return json.dumps({
            "blocked": True,
            "reason": "import_is_pro_only",
            "tier": tier,
            "import_id": import_id,
            "estimated_facts": estimate.get("estimated_facts"),
            "estimated_minutes": estimate.get("estimated_minutes"),
            "message": (
                "Importing your AI memory history is a Pro feature. Your "
                f"archive looks like ~{estimate.get('estimated_facts', '?')} "
                "memories (~"
                f"{estimate.get('estimated_minutes', '?')} min to import). "
                "Upgrade to Pro ($3.99/mo) to run it — call "
                "totalreclaw_upgrade for a checkout link. Nothing was "
                "extracted or stored."
            ),
        })
    return None


async def _run_small_import(engine, source, file_path, content, import_id, estimate) -> str:
    """Small import: process synchronously but still write state for tracking."""
    from totalreclaw.import_state import ImportState, write_import_state, read_import_state

    total_items = estimate.get("total_chunks") or estimate.get("total_facts") or 0
    now = datetime.now(timezone.utc).isoformat()
    batch_size = total_items or 25
    istate = ImportState(
        import_id=import_id, source=source, status="running",
        started_at=now, last_updated=now,
        total_chunks=total_items, batch_total=1, batch_done=0,
        file_path=file_path,
        estimated_total_facts=estimate.get("estimated_facts", 0),
        estimated_minutes=estimate.get("estimated_minutes", 0),
        disclosure_confirmed=True,
        disclosure_provider=_current_disclosure_provider(),
    )
    write_import_state(istate)
    try:
        result = await engine.process_batch(
            source=source, file_path=file_path, content=content,
            offset=0, batch_size=batch_size,
        )
        final = read_import_state(import_id) or istate
        _patch_import_state(
            final, status="completed", batch_done=1,
            facts_stored=result.facts_stored,
            facts_extracted=result.facts_extracted,
            derived_facts=getattr(result, "derived_facts", 0),
            dups_skipped=getattr(result, "dups_skipped", 0),
        )
        return json.dumps({**asdict(result), "import_id": import_id})
    except Exception as e:
        final = read_import_state(import_id) or istate
        _patch_import_state(final, status="failed", errors=[str(e)])
        raise


async def _run_import_batches(
    state, engine, source, file_path, content, import_id,
    total_items, num_batches, batch_size, start_dt,
) -> None:
    """Background driver: page through an import, checkpointing after each batch.

    Extracted from the ``import_from`` nested closure. All ``ImportState``
    rewrites go through :func:`_patch_import_state`.
    """
    from totalreclaw.import_state import read_import_state

    offset = 0
    total_stored = 0
    total_extracted = 0
    total_derived = 0
    batch_done = 0
    eta_ms = 0
    s = None
    try:
        # #401: persist the import id to memory at start so the agent
        # recovers it after context compaction and can poll
        # totalreclaw_import_status without the user re-supplying the
        # id. Best-effort; never blocks the import. Done inside the
        # background task (not the caller) so import_from returns
        # immediately with the "started" ack.
        await _dispatch_module()._persist_import_memory(
            state,
            text=(
                f"Active background import: id={import_id}, source={source}, "
                f"chunks={total_items}, batches={num_batches}, "
                f"started={start_dt.isoformat()}. Check status with "
                f"totalreclaw_import_status."
            ),
            importance=0.7,
        )
        while offset < total_items:
            # #457b (b): any non-running status is a stop signal (aborted /
            # early-stale-failed / 2h-stale-failed / external), not just
            # "aborted" — a reaped record must halt the on-chain writes.
            current = read_import_state(import_id)
            if current is None or current.status != "running":
                logger.info(
                    "Import %s: no longer running (%s) at offset %d — stopping",
                    import_id, current.status if current else "missing", offset,
                )
                return
            # #457b review Finding 1: heartbeat keeps last_updated fresh for
            # the (possibly multi-minute) duration of the batch so a LIVE
            # import isn't false-reaped by the early-stale guard.
            result = await _process_batch_with_heartbeat(
                engine, import_id,
                source=source, file_path=file_path, content=content,
                offset=offset, batch_size=batch_size,
            )
            total_stored += result.facts_stored
            total_extracted += result.facts_extracted
            total_derived += getattr(result, "derived_facts", 0)
            batch_done += 1
            offset += batch_size
            # #457b (c): re-read before checkpointing — a reap DURING the batch
            # must not be resurrected by an old-status checkpoint write.
            s = read_import_state(import_id)
            if s is None or s.status != "running":
                logger.info(
                    "Import %s: reaped during batch (%s) — stopping without "
                    "overwrite", import_id, s.status if s else "missing",
                )
                return
            elapsed = (datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(
                s.started_at.replace("Z", "+00:00")).timestamp())
            sec_per_batch = elapsed / batch_done if batch_done else 45
            remaining = num_batches - batch_done
            eta_ms = remaining * sec_per_batch
            _patch_import_state(
                s,
                batch_done=batch_done,
                facts_stored=total_stored,
                facts_extracted=total_extracted,
                derived_facts=total_derived,
                estimated_completion_iso=datetime.fromtimestamp(
                    datetime.now(timezone.utc).timestamp() + eta_ms,
                    tz=timezone.utc,
                ).isoformat(),
            )
            # #401: INFO-level batch progress so
            # ``journalctl -u hermes-gateway | grep "Import "``
            # reconstructs the timeline without polling the state
            # file. Was checkpoint-only (silent) pre-#401.
            eta_min = f"{eta_ms / 60:.0f}min" if eta_ms else "?"
            logger.info(
                "Import %s: batch %d/%d complete (%d facts stored, %d extracted, ETA ~%s)",
                import_id, batch_done, num_batches, total_stored, total_extracted, eta_min,
            )
            if result.is_complete:
                break
        # Mark complete — only if still running (a reap wins).
        s = read_import_state(import_id)
        if s and s.status == "running":
            _patch_import_state(
                s, status="completed", batch_done=num_batches,
                facts_stored=total_stored, facts_extracted=total_extracted,
                derived_facts=total_derived,
            )
        end_dt = datetime.now(timezone.utc)
        duration_min = (end_dt - start_dt).total_seconds() / 60
        logger.info(
            "Import %s: COMPLETED in %.0f minutes — %d facts stored, %d extracted, %d batches",
            import_id, duration_min, total_stored, total_extracted, batch_done,
        )
        # #401: persist completion to the vault so the agent
        # recovers the result after context compaction. Best-effort.
        await _dispatch_module()._persist_import_memory(
            state,
            text=(
                f"Import {import_id} completed: {total_stored} memories stored, "
                f"{total_extracted} extracted from {total_items} chunks "
                f"({batch_done} batches) in {duration_min:.0f} minutes. "
                f"Source: {source}."
            ),
            importance=0.8,
        )
    except Exception as e:
        s = read_import_state(import_id)
        if s and s.status == "running":
            _patch_import_state(s, status="failed", errors=s.errors + [str(e)])
        logger.error("Import %s: background task failed: %s", import_id, e)


async def _run_import_background(
    *, engine, state, import_id, source, file_path, content,
    estimate, total_items, num_batches, start_dt,
) -> None:
    """Keyword-only background-import entry point (#457b).

    The desloppify split factored the background driver into
    :func:`_run_import_batches` (positional, ``batch_size`` passed in). This
    thin wrapper preserves the pre-split call surface the RC5 tests + any
    external caller use — ``estimate``-driven, keyword-only — by deriving the
    batch size from the estimate and delegating to the extracted loop. All the
    reap-safe / heartbeat / derived-facts invariants live in
    ``_run_import_batches``.
    """
    batch_size = estimate.get("batch_size", 25)
    await _run_import_batches(
        state, engine, source, file_path, content, import_id,
        total_items, num_batches, batch_size, start_dt,
    )


def _spawn_background_import(state, engine, source, file_path, content, import_id, estimate) -> str:
    """Large import: write initial state, spawn the background task, ack immediately."""
    from totalreclaw.import_state import ImportState, write_import_state

    total_items = estimate.get("total_chunks") or estimate.get("total_facts") or 0
    num_batches = estimate.get("num_batches", 1) or 1
    estimated_minutes = estimate.get("estimated_minutes", 0)
    batch_size = estimate.get("batch_size", 25)
    now_dt = datetime.now(timezone.utc)
    eta_iso = datetime.fromtimestamp(
        now_dt.timestamp() + num_batches * 45, tz=timezone.utc
    ).isoformat()
    istate = ImportState(
        import_id=import_id, source=source, status="running",
        started_at=now_dt.isoformat(), last_updated=now_dt.isoformat(),
        total_chunks=total_items, batch_total=num_batches, batch_done=0,
        file_path=file_path,
        estimated_total_facts=estimate.get("estimated_facts", 0),
        estimated_minutes=estimated_minutes,
        estimated_completion_iso=eta_iso,
        disclosure_confirmed=True,
        disclosure_provider=_current_disclosure_provider(),
    )
    write_import_state(istate)

    task = asyncio.ensure_future(_run_import_batches(
        state, engine, source, file_path, content, import_id,
        total_items, num_batches, batch_size, now_dt,
    ))
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    # #457b: if this process exits before the task finishes (one-shot
    # `hermes chat -q`), the atexit reaper marks this record failed.
    _register_spawned_import(import_id)
    return json.dumps({
        "import_id": import_id,
        "status": "running",
        "source": source,
        "total_chunks": total_items,
        "estimated_batches": num_batches,
        "estimated_minutes": estimated_minutes,
        "estimated_completion_iso": eta_iso,
        "message": (
            f"Import started in background. ~{estimated_minutes} min for {total_items} chunks. "
            "Ask \"how's the import?\" to check progress with totalreclaw_import_status. "
            "NOTE: this runs in a background task that needs a long-lived Hermes process "
            "(the gateway/daemon) to finish — a one-shot `hermes chat -q` invocation exits "
            "before it completes, so keep the session running until the import reports done."
        ),
    })


# ── Import tool handlers ─────────────────────────────────────────────────────


async def import_from(args: dict, state: "PluginState", **kwargs) -> str:
    """Import memories from other AI tools (Gemini, ChatGPT, Claude, Mem0, etc.)."""
    _tools = _dispatch_module()
    client = state.get_client()
    if not client:
        return _NOT_CONFIGURED

    source = args.get("source", "")
    file_path = args.get("file_path")
    content = args.get("content")
    dry_run = args.get("dry_run", False)
    resume_id = args.get("resume_id")
    url = args.get("url")
    url_confirmed = bool(args.get("url_confirmed", False))

    if not source:
        from totalreclaw.import_adapters import list_sources
        return json.dumps({
            "error": "No source specified",
            "available_sources": list_sources(),
        })

    import_id = resume_id or str(uuid.uuid4())

    # Consent is evaluated ONCE per call (the disclosure_token is one-time —
    # a second redemption attempt within the same call would fail).
    consent_ok = _tools._disclosure_consent_ok(source, args, resume_id)

    # ── imp-2: URL input — allowlist gate, then server-side fetch ──────────
    if url and not file_path and not content:
        file_path, early = _gate_url_input(
            url, url_confirmed, source, import_id, dry_run, consent_ok,
        )
        if early is not None:
            return early

    try:
        from totalreclaw.import_engine import ImportEngine

        engine = ImportEngine(
            client=client,
            llm_extract=_tools._make_extractor(state),
            llm_completion=_tools._make_llm_completion(state),
        )

        if dry_run:
            estimate = engine.estimate(source=source, file_path=file_path, content=content)
            estimate["import_id"] = import_id
            # #457a: fail loud on a dry-run of an unusable source too, so a
            # typo'd path / empty export surfaces as an error rather than a
            # "0 conversations" preview the agent reports as benign.
            failure = _estimate_failure_errors(estimate)
            if failure:
                return _import_failed_response(source, import_id, failure)
            return json.dumps(estimate)

        estimate = engine.estimate(source=source, file_path=file_path, content=content)
        total_items = estimate.get("total_chunks") or estimate.get("total_facts") or 0

        # #457a: FAIL LOUD BEFORE any state write / background ack. An
        # unusable estimate (adapter errors, or a valid-but-empty 0/0 parse)
        # must NEVER become a completed-empty record — that silent "success"
        # leaked a phantom "import finished, 0 memories" note in rc4 QA. Write
        # a FAILED record (so import_status can report it) and return the
        # adapter errors verbatim.
        failure = _estimate_failure_errors(estimate)
        if failure:
            try:
                from totalreclaw.import_state import ImportState, write_import_state
                now = datetime.now(timezone.utc).isoformat()
                write_import_state(ImportState(
                    import_id=import_id, source=source, status="failed",
                    started_at=now, last_updated=now,
                    total_chunks=estimate.get("total_chunks", 0),
                    file_path=file_path, errors=failure,
                ))
            except Exception as _e:
                logger.warning("import_from: failed to persist failed-state: %s", _e)
            return _import_failed_response(source, import_id, failure)

        # ── Tier gate (PRD-IMP §6: import is a Pro-only feature) ───────────
        blocked = await _check_import_tier_gate(client, import_id, estimate)
        if blocked is not None:
            return blocked

        # ── imp-2: mandatory privacy disclosure (PRD-IMP G-4) ──────────────
        # Extraction-based imports send the user's raw past conversations to
        # an LLM in cleartext. The user must consent AFTER being told which
        # provider will read them — before any extraction begins. Consent is
        # persisted in the import state file so a resume never re-prompts;
        # a fresh consent requires the one-time disclosure_token (#421).
        if not consent_ok:
            return _disclosure_required_response(source, import_id, estimate)

        if total_items <= _SMALL_IMPORT_THRESHOLD:
            return await _run_small_import(engine, source, file_path, content, import_id, estimate)
        return _spawn_background_import(state, engine, source, file_path, content, import_id, estimate)

    except ValueError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error("totalreclaw_import_from failed: %s", e)
        return json.dumps({"error": str(e)})


async def import_batch(args: dict, state: "PluginState", **kwargs) -> str:
    """Process one batch of a large import. Call repeatedly with increasing offset."""
    _tools = _dispatch_module()
    client = state.get_client()
    if not client:
        return _NOT_CONFIGURED

    source = args.get("source", "")
    file_path = args.get("file_path")
    content = args.get("content")
    offset = args.get("offset", 0)
    batch_size = args.get("batch_size", 25)

    if not source:
        return json.dumps({"error": "No source specified"})

    # ── imp-2: disclosure gate (review of PR #431 finding 1; #421 token) ───
    # import_batch runs the same LLM extraction as import_from; without this
    # check it would be a consent bypass. The normal flow satisfies it via
    # the state file import_from wrote (consent persists per source), so the
    # documented import_from -> import_batch loop never re-prompts.
    if not _tools._disclosure_consent_ok(source, args):
        return _disclosure_required_response(source, args.get("import_id") or "")

    # ── Tier gate (PRD-IMP §6) — same gate as import_from (rc12 QA showed
    # the batch path running a full import on a Free account with no wall).
    # Fails OPEN when billing is unreachable (self-hosted / offline).
    if client is not None:
        try:
            billing = await client.status()
            tier = (getattr(billing, "tier", "") or "").strip().lower()
        except Exception:
            tier = ""
        if tier and tier != "pro":
            return json.dumps({
                "blocked": True,
                "reason": "import_is_pro_only",
                "tier": tier,
                "message": (
                    "Importing your AI memory history is a Pro feature. "
                    "Upgrade to Pro ($3.99/mo) to run it — call "
                    "totalreclaw_upgrade for a checkout link. Nothing was "
                    "extracted or stored."
                ),
            })

    # ── F4 (#424): batch-driven imports are tracked in ImportState so
    # totalreclaw_import_status can answer for them — rc12 QA got
    # "no_import_found" right after a completed batch import.
    from totalreclaw.import_state import (
        ImportState, read_import_state, write_import_state,
    )
    key_material = f"tr-import-batch:{source}:{file_path or content or ''}"
    import_id = args.get("import_id") or str(
        uuid.uuid5(uuid.NAMESPACE_URL, key_material)
    )

    try:
        engine = _get_or_create_import_engine(state, source, file_path)
        result = await engine.process_batch(
            source=source,
            file_path=file_path,
            content=content,
            offset=offset,
            batch_size=batch_size,
        )

        # Persist/refresh the tracking state (best-effort — a bookkeeping
        # failure must never fail the batch that already stored facts).
        try:
            now = datetime.now(timezone.utc).isoformat()
            prior = read_import_state(import_id)
            total_batches = max(
                1, -(-result.total_chunks // batch_size) if batch_size else 1,
            )
            write_import_state(ImportState(
                import_id=import_id,
                source=source,
                status="completed" if result.is_complete else "running",
                started_at=prior.started_at if prior else now,
                last_updated=now,
                total_chunks=result.total_chunks,
                batch_total=total_batches,
                batch_done=(prior.batch_done if prior else 0) + 1,
                facts_stored=(prior.facts_stored if prior else 0) + result.facts_stored,
                facts_extracted=(prior.facts_extracted if prior else 0) + result.facts_extracted,
                derived_facts=(prior.derived_facts if prior else 0) + getattr(result, "derived_facts", 0),
                dups_skipped=(prior.dups_skipped if prior else 0) + getattr(result, "dups_skipped", 0),
                file_path=file_path,
                disclosure_confirmed=True,
                disclosure_provider=_current_disclosure_provider(),
            ))
        except Exception as state_err:
            logger.warning("import_batch state bookkeeping failed: %s", state_err)

        return json.dumps({**asdict(result), "import_id": import_id})

    except ValueError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error("totalreclaw_import_batch failed: %s", e)
        return json.dumps({"error": str(e)})


async def import_status(args: dict, state: "PluginState", **kwargs) -> str:
    """Check the progress of a background import."""
    from totalreclaw.import_state import (
        read_import_state, read_most_recent_active_import, read_most_recent_import,
        is_import_stale, is_import_early_stale, write_import_state, ImportState,
        mark_import_announced,
    )

    import_id = args.get("import_id")

    if import_id:
        s = read_import_state(import_id)
        if not s:
            return json.dumps({"error": f"No import found with id: {import_id}"})
    else:
        s = read_most_recent_active_import()
        if not s:
            # #401: fall back to the most recent completed/failed import within
            # the last 48h so the agent can report final state after a long
            # import finishes. Without this, a post-completion status call
            # returned a blind "no_active_import" and the agent could not tell
            # "completed successfully" from "never existed".
            s = read_most_recent_import(max_age_hours=48)
            if not s:
                return json.dumps({"status": "no_import_found", "message": "No active or recent import found. Start one with totalreclaw_import_from."})

    # #457b: orphaned-background-import guard. A running import stuck at 0/N
    # for >10 min almost certainly had its spawning process exit (one-shot
    # `hermes chat -q`). Surface it promptly as failed with a resume hint,
    # instead of waiting out the 2h stale threshold.
    if s.status == "running" and is_import_early_stale(s):
        _patch_import_state(
            s, status="failed",
            errors=s.errors + [
                "Import process appears to have exited before making progress "
                "(no batches completed in 10+ min) — likely a one-shot session "
                "that ended before the background import ran. Resume with "
                "totalreclaw_import_from using the same file and resume_id."
            ],
        )
        return json.dumps({
            "import_id": s.import_id, "status": "failed", "stale": True,
            "orphaned": True, "facts_stored": s.facts_stored,
            "message": (
                "The import didn't run — its session likely exited before the "
                "background task started (no progress in 10+ min). Resume it "
                "with totalreclaw_import_from using the same file and resume_id, "
                "or drive totalreclaw_import_batch synchronously."
            ),
            "resume_id": s.import_id,
        })

    # 2h freshness guard (#401: was 1h — see STALE_THRESHOLD_SECONDS).
    if s.status == "running" and is_import_stale(s):
        _patch_import_state(s, status="failed", errors=s.errors + ["Stale: no progress in 2h"])
        return json.dumps({
            "import_id": s.import_id, "status": "failed", "stale": True,
            "facts_stored": s.facts_stored,
            "message": "Import appears stale — no progress in 2 hours. Resume with totalreclaw_import_from using the same file and resume_id.",
            "resume_id": s.import_id,
        })

    # #401: the agent explicitly queried a completed import (by id or via the
    # 48h fallback) — treat that as the acknowledgment signal for the
    # proactive completion notification and latch ``announced`` so
    # ``pre_llm_call`` stops re-injecting it. This is the "no new tool" path
    # to retiring the notification: the agent demonstrably knows about the
    # import (it just asked), so the nudge has done its job.
    if s.status == "completed" and not s.announced:
        mark_import_announced(s.import_id)

    now_ts = datetime.now(timezone.utc).timestamp()
    try:
        started_ts = datetime.fromisoformat(s.started_at.replace("Z", "+00:00")).timestamp()
        elapsed = now_ts - started_ts
    except Exception:
        elapsed = 0
    sec_per_batch = elapsed / s.batch_done if s.batch_done > 0 else 45
    remaining = max(0, s.batch_total - s.batch_done)
    eta_seconds = int(remaining * sec_per_batch) if s.status == "running" else 0

    # #401: surface elapsed + completion time so the agent can report "the
    # import finished X min ago, took Y minutes" without extra arithmetic.
    # ``completed_at`` is derived from ``last_updated`` when the import has
    # reached a terminal state (the background task writes last_updated on
    # the final checkpoint).
    terminal = s.status in ("completed", "failed", "aborted")
    completed_at = s.last_updated if terminal else None

    return json.dumps({
        "import_id": s.import_id,
        "status": s.status,
        "batch_done": s.batch_done,
        "batch_total": s.batch_total,
        "facts_stored": s.facts_stored,
        "facts_extracted": s.facts_extracted,
        "derived_facts": s.derived_facts,
        "dups_skipped": s.dups_skipped,
        "elapsed_seconds": int(elapsed),
        "eta_seconds": eta_seconds,
        "completion_iso": (
            datetime.fromtimestamp(now_ts + eta_seconds, tz=timezone.utc).isoformat()
            if s.status == "running" else s.last_updated
        ),
        "completed_at": completed_at,
        "source": s.source,
        "started_at": s.started_at,
        "errors": s.errors,
    })


async def import_abort(args: dict, state: "PluginState", **kwargs) -> str:
    """Cancel a running background import."""
    from totalreclaw.import_state import read_import_state

    import_id = args.get("import_id")
    if not import_id:
        return json.dumps({"error": "import_id is required"})

    s = read_import_state(import_id)
    if not s:
        return json.dumps({"error": f"No import found with id: {import_id}"})

    if s.status == "aborted":
        return json.dumps({"aborted": True, "idempotent": True, "import_id": import_id, "facts_already_stored": s.facts_stored})
    if s.status == "completed":
        return json.dumps({"error": "Import already completed — nothing to abort", "import_id": import_id, "facts_stored": s.facts_stored})

    _patch_import_state(s, status="aborted")
    logger.info("Import %s: abort requested (%d facts already stored)", import_id, s.facts_stored)

    return json.dumps({
        "aborted": True,
        "import_id": import_id,
        "facts_already_stored": s.facts_stored,
        "message": "Import abort requested. The background task will stop at the next batch boundary. Already-stored facts are kept.",
    })
