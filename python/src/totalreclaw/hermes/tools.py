"""Tool handlers for TotalReclaw Hermes plugin."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .state import PluginState

logger = logging.getLogger(__name__)

# Threshold for "small" imports that run synchronously in import_from
_SMALL_IMPORT_THRESHOLD = 50

# Strong references to background import tasks so the event loop cannot GC them.
_BG_TASKS: set[asyncio.Task] = set()


# First-person agent-voice phrases. LLMs use these to declare their own
# operational decisions. A genuine user-attributed directive about a tool
# reads as imperative to YOU ("always use X") rather than first-person from
# the agent ("I'll always use X").
_AGENT_VOICE_PHRASES = (
    "i'll always",
    "i will always",
    "i should always",
    "i'll use",
    "i will use",
    "i'll prefer",
    "i will prefer",
    "i'll favor",
    "i will favor",
    "i'll favour",
    "i will favour",
    "i'll call",
    "i will call",
    "i'll route",
    "i will route",
    "i'll switch",
    "i will switch",
)

# Internal totalreclaw tool names. End users do not refer to internal tool
# names in natural speech — when the stored text mentions one alongside
# first-person agent voice, the source is almost certainly the LLM's own
# operational reasoning leaking into a remember() call.
_INTERNAL_TOOL_NAMES = (
    "totalreclaw_remember",
    "totalreclaw_recall",
    "totalreclaw_forget",
    "totalreclaw_pin",
    "totalreclaw_unpin",
    "totalreclaw_status",
    "totalreclaw_debrief",
    "totalreclaw_pair",
    "totalreclaw_import_from",
    "totalreclaw_import_batch",
    "totalreclaw_set_scope",
    "totalreclaw_retype",
    "totalreclaw_upgrade",
    "totalreclaw_export",
    "totalreclaw_report_qa_bug",
)


def _is_likely_agent_self_directive(text: str) -> bool:
    """Block writes that read as the agent's own operational decisions.

    Catches the issue #337 / QA F5 pattern: agent calls totalreclaw_remember
    with text like "I'll always use totalreclaw_remember and totalreclaw_recall
    over the built-in memory tool". SKILL.md 2.4.4 instructs not to do this
    but glm-class models ignore the rule, so enforcement moves to the tool
    call site. Match requires BOTH first-person agent voice AND an internal
    tool-name mention — neither alone is sufficient.
    """
    if not text:
        return False
    lower = text.lower()
    has_agent_voice = any(p in lower for p in _AGENT_VOICE_PHRASES)
    has_tool_name = any(t in lower for t in _INTERNAL_TOOL_NAMES)
    return has_agent_voice and has_tool_name


async def remember(args: dict, state: "PluginState", **kwargs) -> str:
    """Store a memory in TotalReclaw (v1 taxonomy).

    Accepts v1 taxonomy fields: ``type`` (claim | preference | directive |
    commitment | episode | summary), optional ``scope``, optional
    ``reasoning`` for decision-style claims, plus the legacy ``importance``
    slider. Legacy v0 tokens (fact, decision, episodic, goal, context,
    rule) are coerced transparently via :func:`normalize_to_v1_type`.
    """
    from totalreclaw.agent.extraction import (
        VALID_MEMORY_TYPES,
        VALID_MEMORY_SCOPES,
        normalize_to_v1_type,
    )

    client = state.get_client()
    if not client:
        return json.dumps({"error": "TotalReclaw not configured. Call totalreclaw_pair to set up — browser-side crypto keeps the phrase out of this chat."})

    text = args.get("text", "").strip()
    if not text:
        return json.dumps({"error": "No text provided"})

    if _is_likely_agent_self_directive(text):
        logger.warning(
            "totalreclaw_remember blocked agent self-directive: %r",
            text[:200],
        )
        return json.dumps({
            "stored": False,
            "blocked": "agent_self_directive",
            "reason": (
                "Text reads as an agent operational decision (first-person "
                "agent voice + internal totalreclaw_* tool-name reference). "
                "The user did not explicitly say this; storing it would "
                "pollute the vault with a fabricated user directive. Skip."
            ),
        })

    # 2.4.4rc2 (F7) — suppress manual remember calls that duplicate
    # content the next auto-extract batch will capture. The agent can
    # force a write via ``force=true`` for verbatim-preserve cases
    # (e.g. exact quotes the extractor would paraphrase). The
    # suppression check uses a cheap substring-containment on
    # normalized strings; embedding dedup at storage time catches the
    # rest. Counter surfaces in `totalreclaw_status` for observability.
    force_write = bool(args.get("force", False))
    if not force_write and state.manual_remember_is_dup_of_pending(text):
        state.increment_suppressed_writes()
        logger.info(
            "totalreclaw_remember suppressed (duplicate of pending auto-extract): %r",
            text[:200],
        )
        return json.dumps({
            "stored": False,
            "suppressed": "duplicate_of_pending_auto_extract",
            "reason": (
                "This content matches a recent user message that the "
                "post_llm_call hook's auto-extraction will capture in the "
                "next batch (every ~3 turns). Manual storage is redundant "
                "and would waste quota. Pass force=true if you must store "
                "verbatim (e.g. an exact quote the extractor would "
                "paraphrase)."
            ),
        })

    # Default importance to 8 for explicit remember (matches plugin).
    raw_importance = args.get("importance", 8)
    try:
        importance_val = float(raw_importance)
    except (TypeError, ValueError):
        importance_val = 8.0
    importance_val = max(1.0, min(10.0, importance_val))

    # Validate + normalize the v1 type (legacy v0 tokens coerced).
    fact_type_raw = args.get("type", "claim")
    fact_type = normalize_to_v1_type(fact_type_raw)

    # v1 scope + reasoning (both optional)
    scope_raw = str(args.get("scope", "unspecified")).lower()
    scope = scope_raw if scope_raw in VALID_MEMORY_SCOPES else "unspecified"
    reasoning = args.get("reasoning")
    if reasoning is not None and not isinstance(reasoning, str):
        reasoning = None

    try:
        embedding = None
        try:
            from totalreclaw.embedding import get_embedding
            embedding = get_embedding(text)
        except Exception:
            pass

        fact_id = await client.remember(
            text,
            embedding=embedding,
            importance=importance_val,
            fact_type=fact_type,
            provenance="user",  # explicit remember → user provenance
            scope=scope,
            reasoning=reasoning,
            confidence=1.0,  # explicit remember = highest confidence
        )
        return json.dumps({
            "stored": True,
            "fact_id": fact_id,
            "type": fact_type,
            "scope": scope,
            "importance": importance_val,
        })
    except Exception as e:
        logger.error("totalreclaw_remember failed: %s", e)
        return json.dumps({"error": str(e)})


async def recall(args: dict, state: "PluginState", **kwargs) -> str:
    """Search memories in TotalReclaw."""
    from totalreclaw.agent.recall import _fmt_date

    client = state.get_client()
    if not client:
        return json.dumps({"error": "TotalReclaw not configured. Call totalreclaw_pair to set up — browser-side crypto keeps the phrase out of this chat."})

    query = args.get("query", "").strip()
    if not query:
        return json.dumps({"error": "No query provided"})

    top_k = args.get("top_k", 8)

    try:
        query_embedding = None
        try:
            from totalreclaw.embedding import get_embedding
            query_embedding = get_embedding(query)
        except Exception:
            pass

        results = await client.recall(query, query_embedding=query_embedding, top_k=top_k)
        return json.dumps({
            "count": len(results),
            "memories": [
                {
                    "id": r.id,
                    "text": r.text,
                    "type": r.category,
                    "date": _fmt_date(getattr(r, "created_at", None)),
                    "score": round(r.rrf_score, 4),
                }
                for r in results
            ],
        })
    except Exception as e:
        logger.error("totalreclaw_recall failed: %s", e)
        return json.dumps({"error": str(e)})


async def forget(args: dict, state: "PluginState", **kwargs) -> str:
    """Delete a memory from TotalReclaw."""
    client = state.get_client()
    if not client:
        return json.dumps({"error": "TotalReclaw not configured. Call totalreclaw_pair to set up — browser-side crypto keeps the phrase out of this chat."})

    fact_id = args.get("fact_id", "").strip()
    if not fact_id:
        return json.dumps({"error": "No fact_id provided"})

    try:
        success = await client.forget(fact_id)
        return json.dumps({"deleted": success, "fact_id": fact_id})
    except Exception as e:
        logger.error("totalreclaw_forget failed: %s", e)
        return json.dumps({"error": str(e)})


async def pin(args: dict, state: "PluginState", **kwargs) -> str:
    """Pin a memory so auto-resolution cannot supersede it.

    Phase 2 knowledge-graph pinning semantics: the claim is rewritten with
    ``status=pinned``, the old fact is tombstoned, and a new fact is
    written that ``supersedes`` the old one. Idempotent on already-pinned
    claims.
    """
    client = state.get_client()
    if not client:
        return json.dumps({"error": "TotalReclaw not configured. Call totalreclaw_pair to set up — browser-side crypto keeps the phrase out of this chat."})

    raw = args.get("fact_id")
    if not isinstance(raw, str):
        return json.dumps({"error": "fact_id must be a string"})
    fact_id = raw.strip()
    if not fact_id:
        return json.dumps({"error": "No fact_id provided"})

    try:
        result = await client.pin_fact(fact_id)
        response = {
            "pinned": True,
            "fact_id": result.get("fact_id"),
            "new_fact_id": result.get("new_fact_id"),
            "previous_status": result.get("previous_status"),
            "new_status": result.get("new_status"),
        }
        if result.get("idempotent"):
            response["idempotent"] = True
            response["message"] = "Claim was already pinned; no on-chain write."
        return json.dumps(response)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error("totalreclaw_pin failed: %s", e)
        return json.dumps({"error": str(e)})


async def unpin(args: dict, state: "PluginState", **kwargs) -> str:
    """Unpin a memory so auto-resolution can supersede it again.

    Inverse of :func:`pin`. The claim is rewritten with ``status=active``
    (the canonical default — the status field is omitted from the new
    blob) and the supersession flow is identical to pinning.
    """
    client = state.get_client()
    if not client:
        return json.dumps({"error": "TotalReclaw not configured. Call totalreclaw_pair to set up — browser-side crypto keeps the phrase out of this chat."})

    raw = args.get("fact_id")
    if not isinstance(raw, str):
        return json.dumps({"error": "fact_id must be a string"})
    fact_id = raw.strip()
    if not fact_id:
        return json.dumps({"error": "No fact_id provided"})

    try:
        result = await client.unpin_fact(fact_id)
        response = {
            "unpinned": True,
            "fact_id": result.get("fact_id"),
            "new_fact_id": result.get("new_fact_id"),
            "previous_status": result.get("previous_status"),
            "new_status": result.get("new_status"),
        }
        if result.get("idempotent"):
            response["idempotent"] = True
            response["message"] = "Claim was already active; no on-chain write."
        return json.dumps(response)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error("totalreclaw_unpin failed: %s", e)
        return json.dumps({"error": str(e)})


async def retype(args: dict, state: "PluginState", **kwargs) -> str:
    """Re-type an existing memory (e.g. claim → preference).

    Mirrors ``skill/plugin/index.ts::handleRetype``. The claim is rewritten
    with the new ``type`` and tombstoned; a new fact carrying
    ``superseded_by`` and the inherited ``pin_status`` is written in the
    same atomic batch. Surfaces ``partial: True`` when the on-chain write
    succeeded but the subgraph indexer hasn't caught up within the timeout.
    """
    from totalreclaw.retype_setscope import validate_retype_args

    client = state.get_client()
    if not client:
        return json.dumps({"error": "TotalReclaw not configured. Call totalreclaw_pair to set up — browser-side crypto keeps the phrase out of this chat."})

    parsed = validate_retype_args(args)
    if not parsed.get("ok"):
        return json.dumps({"error": parsed.get("error")})

    try:
        result = await client.retype(parsed["fact_id"], parsed["new_type"])
        if not result.get("success"):
            return json.dumps({"error": result.get("error", "retype failed")})
        response = {
            "retyped": True,
            "fact_id": result.get("fact_id"),
            "new_fact_id": result.get("new_fact_id"),
            "previous_type": result.get("previous_type"),
            "new_type": result.get("new_type"),
        }
        if result.get("tx_hash"):
            response["tx_hash"] = result["tx_hash"]
        if result.get("partial"):
            response["partial"] = True
        return json.dumps(response)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error("totalreclaw_retype failed: %s", e)
        return json.dumps({"error": str(e)})


async def set_scope(args: dict, state: "PluginState", **kwargs) -> str:
    """Re-scope an existing memory (e.g. unspecified → health).

    Mirrors ``skill/plugin/index.ts::handleSetScope``. Same on-chain shape
    as :func:`retype` — tombstone + new fact in one ``executeBatch`` UserOp.
    """
    from totalreclaw.retype_setscope import validate_set_scope_args

    client = state.get_client()
    if not client:
        return json.dumps({"error": "TotalReclaw not configured. Call totalreclaw_pair to set up — browser-side crypto keeps the phrase out of this chat."})

    parsed = validate_set_scope_args(args)
    if not parsed.get("ok"):
        return json.dumps({"error": parsed.get("error")})

    try:
        result = await client.set_scope(parsed["fact_id"], parsed["new_scope"])
        if not result.get("success"):
            return json.dumps({"error": result.get("error", "set_scope failed")})
        response = {
            "scope_set": True,
            "fact_id": result.get("fact_id"),
            "new_fact_id": result.get("new_fact_id"),
            "previous_scope": result.get("previous_scope"),
            "new_scope": result.get("new_scope"),
        }
        if result.get("tx_hash"):
            response["tx_hash"] = result["tx_hash"]
        if result.get("partial"):
            response["partial"] = True
        return json.dumps(response)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error("totalreclaw_set_scope failed: %s", e)
        return json.dumps({"error": str(e)})


async def export_all(args: dict, state: "PluginState", **kwargs) -> str:
    """Export all memories from TotalReclaw."""
    client = state.get_client()
    if not client:
        return json.dumps({"error": "TotalReclaw not configured. Call totalreclaw_pair to set up — browser-side crypto keeps the phrase out of this chat."})

    try:
        facts = await client.export_all()
        return json.dumps({"count": len(facts), "facts": facts})
    except Exception as e:
        logger.error("totalreclaw_export failed: %s", e)
        return json.dumps({"error": str(e)})


async def status(args: dict, state: "PluginState", **kwargs) -> str:
    """Check TotalReclaw billing status."""
    client = state.get_client()
    if not client:
        return json.dumps({"error": "TotalReclaw not configured. Call totalreclaw_pair to set up — browser-side crypto keeps the phrase out of this chat."})

    try:
        billing = await client.status()
        return json.dumps({
            "tier": billing.tier,
            "free_writes_used": billing.free_writes_used,
            "free_writes_limit": billing.free_writes_limit,
            "expires_at": billing.expires_at,
        })
    except Exception as e:
        logger.error("totalreclaw_status failed: %s", e)
        return json.dumps({"error": str(e)})


def setup(args: dict, state: "PluginState", **kwargs) -> str:
    """Configure TotalReclaw credentials. Generates a new recovery phrase if none provided.

    .. warning::

       In 2.3.1rc4 this function is NO LONGER registered as an agent tool —
       ``project_phrase_safety_rule.md`` requires that recovery phrases
       never cross the LLM context, and both the ``recovery_phrase``
       parameter path (phrase-in) AND the phrase-less generation path
       (phrase-out in the return JSON) violated that rule. The function
       stays in the module for test compatibility and for the CLI
       delegation chain (``totalreclaw setup`` -> ``hermes.cli.run_setup``).
       Calling it from an agent-tool handler is forbidden; route the agent
       to ``totalreclaw_pair`` instead.
    """
    recovery_phrase = args.get("recovery_phrase", "").strip()
    generated = False

    if not recovery_phrase:
        # Generate a new BIP-39 mnemonic using eth_account (already a dependency)
        try:
            from eth_account import Account
            Account.enable_unaudited_hdwallet_features()
            _acct, recovery_phrase = Account.create_with_mnemonic()
            generated = True
        except Exception as e:
            logger.error("Failed to generate recovery phrase: %s", e)
            return json.dumps({"error": f"Failed to generate recovery phrase: {e}"})

    try:
        state.configure(recovery_phrase)
        client = state.get_client()
        # The Smart Account address is resolved lazily on the first
        # remember/recall call (requires async RPC). At setup time we only
        # have the EOA — surface it as ``eoa_address`` and mark the SA as
        # pending so callers don't confuse the two. See DIAG-PYTHON-V2.
        result = {
            "configured": True,
            "eoa_address": client._eoa_address,
            "wallet_address_pending": True,
            "note": (
                "Smart Account address will be resolved on the first "
                "remember/recall call. Until then `wallet_address` is unset."
            ),
        }
        if generated:
            result["recovery_phrase"] = recovery_phrase
            result["generated"] = True
        return json.dumps(result)
    except Exception as e:
        logger.error("totalreclaw_setup failed: %s", e)
        return json.dumps({"error": str(e)})


# ── Billing Upgrade ──────────────────────────────────────────────────────────


async def upgrade(args: dict, state: "PluginState", **kwargs) -> str:
    """Create a Stripe Checkout session for upgrading to Pro tier.

    Thin wrapper around :meth:`RelayClient.create_checkout` — the relay
    already resolves wallet + auth headers from the client state, so the
    tool takes no user-facing arguments. Returns the checkout URL plus a
    user-visible ``message`` the agent should read back verbatim (the URL
    is the whole point of calling this tool).
    """
    client = state.get_client()
    if not client:
        return json.dumps(
            {"error": "TotalReclaw not configured. Call totalreclaw_pair to set up — browser-side crypto keeps the phrase out of this chat."}
        )

    try:
        # Ensure the relay has the resolved Smart Account address before
        # asking for a checkout — otherwise the ``wallet_address`` field
        # in the POST body is null and the relay rejects with 400. The
        # public client methods (remember/recall/status) all hit
        # ``_ensure_address()`` internally; create_checkout is one of the
        # few that doesn't need a fact write so we thread the address
        # resolution explicitly.
        ensure_addr = getattr(client, "_ensure_address", None)
        if callable(ensure_addr):
            try:
                await ensure_addr()
            except Exception:
                # Non-fatal — the relay may still accept the checkout if
                # ``self._wallet_address`` was populated elsewhere.
                pass

        checkout = await client._relay.create_checkout()
        return json.dumps({
            "checkout_url": checkout.checkout_url,
            "session_id": checkout.session_id,
            "message": (
                f"Open this URL in your browser to complete the upgrade "
                f"to Pro: {checkout.checkout_url}"
            ),
        })
    except Exception as e:
        logger.error("totalreclaw_upgrade failed: %s", e)
        return json.dumps({"error": f"Failed to create checkout session: {e}"})


# ── Debrief (explicit invocation) ────────────────────────────────────────────


async def debrief(args: dict, state: "PluginState", **kwargs) -> str:
    """Run the session debrief on demand.

    Reuses :func:`totalreclaw.agent.lifecycle.session_debrief` — the same
    function the automatic ``on_session_end`` hook calls — so the stored
    summary facts are indistinguishable from the auto-flow output
    (``type=summary``, ``provenance=derived``, ``scope=unspecified``).

    Returns a JSON object with ``stored`` (count), ``fact_ids``, and
    ``skipped=true`` when the session is too short (< 4 turns).
    """
    client = state.get_client()
    if not client:
        return json.dumps(
            {"error": "TotalReclaw not configured. Call totalreclaw_pair to set up — browser-side crypto keeps the phrase out of this chat."}
        )

    # Short-circuit guard so the tool returns a clear explanation to the
    # agent rather than an empty debrief. Mirrors the < 8-message gate in
    # ``session_debrief`` itself.
    all_messages = state.get_all_messages() if hasattr(state, "get_all_messages") else []
    if len(all_messages) < 8:
        return json.dumps({
            "stored": 0,
            "count": 0,
            "fact_ids": [],
            "skipped": True,
            "message": (
                "Session is too short for a debrief (need at least 4 turns). "
                "Keep chatting, or re-invoke after a longer session."
            ),
        })

    try:
        # ``session_debrief`` is synchronous (drives its own ``run_sync``
        # under the hood — see ``agent/loop_runner.py``). Call it via an
        # executor so we do not block the Hermes async runtime loop.
        import asyncio

        loop = asyncio.get_running_loop()
        from totalreclaw.agent.lifecycle import session_debrief
        fact_ids: list[str] = await loop.run_in_executor(
            None, session_debrief, state, None
        ) or []
        return json.dumps({
            "stored": len(fact_ids),
            "count": len(fact_ids),
            "fact_ids": fact_ids,
            "message": (
                f"Stored {len(fact_ids)} debrief summary fact(s) on-chain."
                if fact_ids
                else "No debrief items were generated (conversation may not have enough signal)."
            ),
        })
    except Exception as e:
        logger.error("totalreclaw_debrief failed: %s", e)
        return json.dumps({"error": str(e)})


# ── Import Tools ─────────────────────────────────────────────────────────────


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


async def import_from(args: dict, state: "PluginState", **kwargs) -> str:
    """Import memories from other AI tools (Gemini, ChatGPT, Claude, Mem0, etc.)."""
    client = state.get_client()
    if not client:
        return json.dumps({"error": "TotalReclaw not configured. Call totalreclaw_pair to set up — browser-side crypto keeps the phrase out of this chat."})

    source = args.get("source", "")
    file_path = args.get("file_path")
    content = args.get("content")
    dry_run = args.get("dry_run", False)
    resume_id = args.get("resume_id")

    if not source:
        from totalreclaw.import_adapters import list_sources
        return json.dumps({
            "error": "No source specified",
            "available_sources": list_sources(),
        })

    import_id = resume_id or str(uuid.uuid4())

    try:
        from totalreclaw.import_engine import ImportEngine
        from totalreclaw.import_state import (
            ImportState, write_import_state, read_import_state, is_import_stale,
        )

        engine = ImportEngine(
            client=client,
            llm_extract=_make_extractor(state),
            llm_completion=_make_llm_completion(state),
        )

        if dry_run:
            estimate = engine.estimate(source=source, file_path=file_path, content=content)
            estimate["import_id"] = import_id
            return json.dumps(estimate)

        estimate = engine.estimate(source=source, file_path=file_path, content=content)
        total_items = estimate.get("total_chunks") or estimate.get("total_facts") or 0

        # ── Tier gate (PRD-IMP §6: import is a Pro-only feature) ───────────
        # Interim client-side UX gate: a free-tier user hits the upgrade wall
        # BEFORE any LLM extraction or on-chain write, so they aren't charged
        # cost/quota by surprise. The relay still enforces the real write
        # quota server-side. Full "free gets 1 lifetime trial" enforcement is
        # pending the relay `import_count_lifetime` field (imp-3); until then
        # this is strict Pro-only. Fails OPEN if billing is unreachable
        # (self-hosted / offline) so those flows are not blocked.
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

        if total_items <= _SMALL_IMPORT_THRESHOLD:
            # Small import: process synchronously but still write state for tracking.
            now = datetime.now(timezone.utc).isoformat()
            batch_size = total_items or 25
            istate = ImportState(
                import_id=import_id, source=source, status="running",
                started_at=now, last_updated=now,
                total_chunks=total_items, batch_total=1, batch_done=0,
                file_path=file_path,
                estimated_total_facts=estimate.get("estimated_facts", 0),
                estimated_minutes=estimate.get("estimated_minutes", 0),
            )
            write_import_state(istate)
            try:
                result = await engine.process_batch(
                    source=source, file_path=file_path, content=content,
                    offset=0, batch_size=batch_size,
                )
                final = read_import_state(import_id) or istate
                write_import_state(ImportState(
                    **{**asdict(final),
                       "status": "completed",
                       "batch_done": 1,
                       "facts_stored": result.facts_stored,
                       "facts_extracted": result.facts_extracted,
                    }
                ))
                return json.dumps({**asdict(result), "import_id": import_id})
            except Exception as e:
                final = read_import_state(import_id) or istate
                write_import_state(ImportState(**{**asdict(final), "status": "failed", "errors": [str(e)]}))
                raise
        else:
            # Large import: spawn background asyncio.Task, return immediately.
            num_batches = estimate.get("num_batches", 1) or 1
            estimated_minutes = estimate.get("estimated_minutes", 0)
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
            )
            write_import_state(istate)

            async def _run_background() -> None:
                from totalreclaw.import_state import read_import_state, write_import_state, ImportState
                offset = 0
                batch_size = estimate.get("batch_size", 25)
                total_stored = 0
                total_extracted = 0
                batch_done = 0
                try:
                    while offset < total_items:
                        # Check abort flag before each batch.
                        current = read_import_state(import_id)
                        if current and current.status == "aborted":
                            logger.info("Import %s: abort flag detected at offset %d", import_id, offset)
                            return
                        result = await engine.process_batch(
                            source=source, file_path=file_path, content=content,
                            offset=offset, batch_size=batch_size,
                        )
                        total_stored += result.facts_stored
                        total_extracted += result.facts_extracted
                        batch_done += 1
                        offset += batch_size
                        # Checkpoint state.
                        s = read_import_state(import_id)
                        if s:
                            elapsed = (datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(
                                s.started_at.replace("Z", "+00:00")).timestamp())
                            sec_per_batch = elapsed / batch_done if batch_done else 45
                            remaining = num_batches - batch_done
                            eta_ms = remaining * sec_per_batch
                            write_import_state(ImportState(**{
                                **asdict(s),
                                "batch_done": batch_done,
                                "facts_stored": total_stored,
                                "facts_extracted": total_extracted,
                                "estimated_completion_iso": datetime.fromtimestamp(
                                    datetime.now(timezone.utc).timestamp() + eta_ms,
                                    tz=timezone.utc,
                                ).isoformat(),
                            }))
                        if result.is_complete:
                            break
                    # Mark complete.
                    s = read_import_state(import_id)
                    if s and s.status == "running":
                        write_import_state(ImportState(**{**asdict(s), "status": "completed",
                                                          "batch_done": num_batches,
                                                          "facts_stored": total_stored,
                                                          "facts_extracted": total_extracted}))
                    logger.info("Import %s: background complete (%d facts stored)", import_id, total_stored)
                except Exception as e:
                    s = read_import_state(import_id)
                    if s and s.status == "running":
                        write_import_state(ImportState(**{**asdict(s), "status": "failed",
                                                          "errors": s.errors + [str(e)]}))
                    logger.error("Import %s: background task failed: %s", import_id, e)

            _bg_task = asyncio.ensure_future(_run_background())
            _BG_TASKS.add(_bg_task)
            _bg_task.add_done_callback(_BG_TASKS.discard)
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
                    "Ask \"how's the import?\" to check progress with totalreclaw_import_status."
                ),
            })

    except ValueError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error("totalreclaw_import_from failed: %s", e)
        return json.dumps({"error": str(e)})


async def import_batch(args: dict, state: "PluginState", **kwargs) -> str:
    """Process one batch of a large import. Call repeatedly with increasing offset."""
    client = state.get_client()
    if not client:
        return json.dumps({"error": "TotalReclaw not configured. Call totalreclaw_pair to set up — browser-side crypto keeps the phrase out of this chat."})

    source = args.get("source", "")
    file_path = args.get("file_path")
    content = args.get("content")
    offset = args.get("offset", 0)
    batch_size = args.get("batch_size", 25)

    if not source:
        return json.dumps({"error": "No source specified"})

    try:
        from totalreclaw.import_engine import ImportEngine

        engine = ImportEngine(
            client=client,
            llm_extract=_make_extractor(state),
            llm_completion=_make_llm_completion(state),
        )
        result = await engine.process_batch(
            source=source,
            file_path=file_path,
            content=content,
            offset=offset,
            batch_size=batch_size,
        )
        return json.dumps(asdict(result))

    except ValueError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error("totalreclaw_import_batch failed: %s", e)
        return json.dumps({"error": str(e)})


async def import_status(args: dict, state: "PluginState", **kwargs) -> str:
    """Check the progress of a background import."""
    from totalreclaw.import_state import (
        read_import_state, read_most_recent_active_import, is_import_stale, write_import_state, ImportState,
    )
    from dataclasses import asdict

    import_id = args.get("import_id")

    if import_id:
        s = read_import_state(import_id)
        if not s:
            return json.dumps({"error": f"No import found with id: {import_id}"})
    else:
        s = read_most_recent_active_import()
        if not s:
            return json.dumps({"status": "no_active_import", "message": "No active import found. Start one with totalreclaw_import_from."})

    # 1h freshness guard.
    if s.status == "running" and is_import_stale(s):
        write_import_state(ImportState(**{**asdict(s), "status": "failed",
                                          "errors": s.errors + ["Stale: no progress in 1h"]}))
        return json.dumps({
            "import_id": s.import_id, "status": "failed", "stale": True,
            "facts_stored": s.facts_stored,
            "message": "Import appears stale — no progress in 1 hour. Resume with totalreclaw_import_from using the same file and resume_id.",
            "resume_id": s.import_id,
        })

    now_ts = datetime.now(timezone.utc).timestamp()
    try:
        started_ts = datetime.fromisoformat(s.started_at.replace("Z", "+00:00")).timestamp()
        elapsed = now_ts - started_ts
    except Exception:
        elapsed = 0
    sec_per_batch = elapsed / s.batch_done if s.batch_done > 0 else 45
    remaining = max(0, s.batch_total - s.batch_done)
    eta_seconds = int(remaining * sec_per_batch) if s.status == "running" else 0

    return json.dumps({
        "import_id": s.import_id,
        "status": s.status,
        "batch_done": s.batch_done,
        "batch_total": s.batch_total,
        "facts_stored": s.facts_stored,
        "dups_skipped": s.dups_skipped,
        "eta_seconds": eta_seconds,
        "completion_iso": (
            datetime.fromtimestamp(now_ts + eta_seconds, tz=timezone.utc).isoformat()
            if s.status == "running" else s.last_updated
        ),
        "source": s.source,
        "started_at": s.started_at,
        "errors": s.errors,
    })


async def import_abort(args: dict, state: "PluginState", **kwargs) -> str:
    """Cancel a running background import."""
    from totalreclaw.import_state import read_import_state, write_import_state, ImportState
    from dataclasses import asdict

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

    write_import_state(ImportState(**{**asdict(s), "status": "aborted"}))
    logger.info("Import %s: abort requested (%d facts already stored)", import_id, s.facts_stored)

    return json.dumps({
        "aborted": True,
        "import_id": import_id,
        "facts_already_stored": s.facts_stored,
        "message": "Import abort requested. The background task will stop at the next batch boundary. Already-stored facts are kept.",
    })
