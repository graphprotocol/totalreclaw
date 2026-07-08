"""Tool handlers for TotalReclaw Hermes plugin.

Core CRUD + lifecycle tool handlers live here. The memory-import subsystem
(the ``totalreclaw_import_*`` handlers, the privacy-disclosure gate, and the
export-URL download machinery) lives in :mod:`totalreclaw.hermes.import_tools`
and is re-exported at the bottom of this module so ``tools.<name>`` attribute
access — and the test suite's ``monkeypatch.setattr(tools, …)`` sites — keep
working unchanged.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .state import PluginState

logger = logging.getLogger(__name__)


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
    "totalreclaw_top_up",
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
        except Exception as e:
            logger.debug("embed failed for remember %r: %s", text[:40], e)
            # degrade: store without embedding (search falls back to blind indices)

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
    from totalreclaw.claims_helper import compose_provenance_label  # #317

    client = state.get_client()
    if not client:
        return json.dumps({"error": "TotalReclaw not configured. Call totalreclaw_pair to set up — browser-side crypto keeps the phrase out of this chat."})

    query = args.get("query", "").strip()
    if not query:
        return json.dumps({"error": "No query provided"})

    top_k = args.get("top_k", state.get_recall_top_k())

    try:
        query_embedding = None
        try:
            from totalreclaw.embedding import get_embedding
            query_embedding = get_embedding(query)
        except Exception as e:
            logger.debug("embed failed for recall query %r: %s", query[:40], e)
            # degrade: query on blind indices only (no cosine signal)

        results = await client.recall(
            query,
            query_embedding=query_embedding,
            top_k=top_k,
            max_candidates=state.get_max_candidate_pool(),
        )
        memories = []
        for r in results:
            mem = {
                "id": r.id,
                "text": r.text,
                "type": r.category,
                "date": _fmt_date(getattr(r, "created_at", None)),
                "score": round(r.rrf_score, 4),
            }
            # #425 — surface provenance so the agent can answer "where does
            # this memory come from?" (imported memories carry
            # import_source + a per-conversation session_id since #356/#363).
            if getattr(r, "source", None):
                mem["source"] = r.source
            md = getattr(r, "metadata", None) or {}
            if md.get("import_source"):
                mem["import_source"] = md["import_source"]
            if md.get("session_id"):
                mem["session_id"] = md["session_id"]
            # #317 — agent-instance provenance. Surface both the raw name and
            # a composed "John (Hermes)" label so the agent/SPA can render it
            # directly. Only present when the memory carries a name → output
            # for pre-#317 memories is unchanged.
            if md.get("agent_name"):
                mem["agent_name"] = md["agent_name"]
                mem["provenance"] = compose_provenance_label("Hermes", md["agent_name"])
            memories.append(mem)
        return json.dumps({"count": len(results), "memories": memories})
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
        result = await client.forget(fact_id)
        return json.dumps({"deleted": result["success"], "fact_id": fact_id})
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
        # Surface the account identity so the agent can answer "what's my
        # account ID / address?" — previously NO tool exposed it, so the agent
        # had nothing to report. The Smart Account address IS the account ID;
        # it is a public on-chain address (NOT the recovery phrase) and safe to
        # show. Resolve it if status() didn't already.
        sa = client.resolved_wallet_address
        if not sa:
            try:
                sa = await client.get_wallet_address()
            except Exception:
                sa = None
        return json.dumps({
            "tier": billing.tier,
            "free_writes_used": billing.free_writes_used,
            "free_writes_limit": billing.free_writes_limit,
            "expires_at": billing.expires_at,
            "account_id": sa,
            "wallet_address": sa,
            "eoa_address": client.eoa_address,
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
            "eoa_address": client.eoa_address,
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
        try:
            await client.ensure_address()
        except Exception:
            # Non-fatal — the relay may still accept the checkout if
            # the Smart Account address was populated elsewhere.
            pass

        checkout = await client.relay.create_checkout()
        payload = {
            "checkout_url": checkout.checkout_url,
            "message": (
                f"Open this URL in your browser to complete the upgrade "
                f"to Pro: {checkout.checkout_url}"
            ),
        }
        # The relay does not return a Stripe session id; only include the
        # field if some future relay build starts sending one.
        if checkout.session_id:
            payload["session_id"] = checkout.session_id
        return json.dumps(payload)
    except Exception as e:
        logger.error("totalreclaw_upgrade failed: %s", e)
        return json.dumps({"error": f"Failed to create checkout session: {e}"})


async def top_up(args: dict, state: "PluginState", **kwargs) -> str:
    """Buy a one-time pack of extra memories (#392).

    For when the monthly quota + grace are exhausted (e.g. mid-import) and the
    user wants more memories now rather than waiting for the reset. ``pack`` is
    the facts count: ``"1000"``, ``"5000"``, or ``"10000"``. Returns the
    checkout URL the agent should read back verbatim.
    """
    client = state.get_client()
    if not client:
        return json.dumps(
            {"error": "TotalReclaw not configured. Call totalreclaw_pair to set up — browser-side crypto keeps the phrase out of this chat."}
        )
    pack = str(args.get("pack") or "").strip()
    if pack not in ("1000", "5000", "10000"):
        return json.dumps({"error": "Invalid or missing 'pack'. Choose 1000, 5000, or 10000 (memories)."})

    try:
        try:
            await client.ensure_address()
        except Exception:
            pass
        checkout = await client.relay.create_topup(pack)
        return json.dumps({
            "checkout_url": checkout.checkout_url,
            "pack": pack,
            "message": f"Open this URL in your browser to buy +{pack} memories: {checkout.checkout_url}",
        })
    except Exception as e:
        logger.error("totalreclaw_top_up failed: %s", e)
        return json.dumps({"error": f"Failed to create top-up checkout session: {e}"})


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
    # agent rather than an empty debrief. The explicit tool holds a stricter
    # 4-turn floor than the auto path: ``session_debrief``'s content-aware gate
    # will crystallize a short 2-3 turn session when it produced >= 2 stored
    # facts, but the explicit tool has no stored-fact context to judge that, so
    # it keeps the simple length floor and tells the user to keep chatting.
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


# ── Import subsystem (re-exported from import_tools) ─────────────────────────
#
# The import / disclosure / export-URL subsystem was extracted to
# ``totalreclaw.hermes.import_tools`` to keep this module focused on the core
# CRUD + lifecycle handlers. Every public and private symbol is re-exported
# here so ``tools.<name>`` attribute access is unchanged and the test suite's
# ``monkeypatch.setattr(tools, …)`` / ``patch("…hermes.tools.<name>")`` sites
# stay authoritative — import_tools routes its patchable collaborators back
# through this module (see its module docstring). Import last so ``remember``
# and the other handlers above are already defined when import_tools binds.
from .import_tools import (  # noqa: E402
    import_from,
    import_batch,
    import_status,
    import_abort,
    _read_hermes_llm_config,
    _make_extractor,
    _make_llm_completion,
    _get_or_create_import_engine,
    _persist_import_memory,
    _is_allowlisted_export_host,
    _prior_disclosure_consent,
    _mint_disclosure_token,
    _redeem_disclosure_token,
    _disclosure_token_hash,
    _disclosure_consent_ok,
    _disclosure_required_response,
    _provider_name_from_base_url,
    _extraction_provider_label,
    _make_redirect_validator,
    _fetch_export_url,
    _gate_url_input,
    _check_import_tier_gate,
    _estimate_failure_errors,
    _import_failed_response,
    _fail_orphaned_imports_on_exit,
    _register_spawned_import,
    _import_heartbeat,
    _process_batch_with_heartbeat,
    _run_small_import,
    _spawn_background_import,
    _run_import_batches,
    _run_import_background,
    _patch_import_state,
    _DISCLOSURE_TOKEN_TTL_S,
    _HEARTBEAT_INTERVAL_S,
    _SPAWNED_IMPORT_IDS,
    _EXTRACTION_IMPORT_SOURCES,
    _EXPORT_URL_ALLOWLIST,
    _SMALL_IMPORT_THRESHOLD,
    _BG_TASKS,
)
