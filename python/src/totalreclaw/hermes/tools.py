"""Tool handlers for TotalReclaw Hermes plugin."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .state import PluginState

logger = logging.getLogger(__name__)

# Threshold for "small" imports that run synchronously in import_from
_SMALL_IMPORT_THRESHOLD = 50


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
                {"id": r.id, "text": r.text, "type": r.category, "score": round(r.rrf_score, 4)}
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

    async def extract(messages: list[dict], timestamp: str) -> list[dict]:
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

        response = await chat_completion(config, EXTRACTION_SYSTEM_PROMPT, user_prompt)
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


async def import_from(args: dict, state: "PluginState", **kwargs) -> str:
    """Import memories from other AI tools (Gemini, ChatGPT, Claude, Mem0, etc.)."""
    client = state.get_client()
    if not client:
        return json.dumps({"error": "TotalReclaw not configured. Call totalreclaw_pair to set up — browser-side crypto keeps the phrase out of this chat."})

    source = args.get("source", "")
    file_path = args.get("file_path")
    content = args.get("content")
    dry_run = args.get("dry_run", False)

    if not source:
        from totalreclaw.import_adapters import list_sources
        return json.dumps({
            "error": "No source specified",
            "available_sources": list_sources(),
        })

    try:
        from totalreclaw.import_engine import ImportEngine

        engine = ImportEngine(client=client, llm_extract=_make_extractor(state))

        if dry_run:
            estimate = engine.estimate(source=source, file_path=file_path, content=content)
            return json.dumps(estimate)

        # For small imports, run all batches synchronously
        estimate = engine.estimate(source=source, file_path=file_path, content=content)
        total_items = estimate.get("total_chunks") or estimate.get("total_facts") or 0

        if total_items <= _SMALL_IMPORT_THRESHOLD:
            # Process everything in one pass
            result = await engine.process_batch(
                source=source,
                file_path=file_path,
                content=content,
                offset=0,
                batch_size=total_items or 25,
            )
            return json.dumps(asdict(result))
        else:
            # For large imports, return estimate and instruct agent to use import_batch
            estimate["message"] = (
                f"Large import detected ({total_items} chunks). "
                f"Use totalreclaw_import_batch to process in batches of {estimate['batch_size']}. "
                f"Call with offset=0, then offset={estimate['batch_size']}, etc. "
                f"Estimated {estimate['num_batches']} batches, ~{estimate['estimated_minutes']} minutes."
            )
            return json.dumps(estimate)

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

        engine = ImportEngine(client=client, llm_extract=_make_extractor(state))
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
