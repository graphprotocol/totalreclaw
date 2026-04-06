"""Tool handlers for TotalReclaw Hermes plugin."""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .state import PluginState

logger = logging.getLogger(__name__)


async def remember(args: dict, state: "PluginState", **kwargs) -> str:
    """Store a memory in TotalReclaw."""
    client = state.get_client()
    if not client:
        return json.dumps({"error": "TotalReclaw not configured. Run totalreclaw_setup first."})

    text = args.get("text", "").strip()
    if not text:
        return json.dumps({"error": "No text provided"})

    importance = args.get("importance", 0.5)

    try:
        embedding = None
        try:
            from totalreclaw.embedding import get_embedding
            embedding = get_embedding(text)
        except Exception:
            pass

        fact_id = await client.remember(text, embedding=embedding, importance=importance)
        return json.dumps({"stored": True, "fact_id": fact_id})
    except Exception as e:
        logger.error("totalreclaw_remember failed: %s", e)
        return json.dumps({"error": str(e)})


async def recall(args: dict, state: "PluginState", **kwargs) -> str:
    """Search memories in TotalReclaw."""
    client = state.get_client()
    if not client:
        return json.dumps({"error": "TotalReclaw not configured. Run totalreclaw_setup first."})

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
                {"id": r.id, "text": r.text, "score": round(r.rrf_score, 4)}
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
        return json.dumps({"error": "TotalReclaw not configured. Run totalreclaw_setup first."})

    fact_id = args.get("fact_id", "").strip()
    if not fact_id:
        return json.dumps({"error": "No fact_id provided"})

    try:
        success = await client.forget(fact_id)
        return json.dumps({"deleted": success, "fact_id": fact_id})
    except Exception as e:
        logger.error("totalreclaw_forget failed: %s", e)
        return json.dumps({"error": str(e)})


async def export_all(args: dict, state: "PluginState", **kwargs) -> str:
    """Export all memories from TotalReclaw."""
    client = state.get_client()
    if not client:
        return json.dumps({"error": "TotalReclaw not configured. Run totalreclaw_setup first."})

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
        return json.dumps({"error": "TotalReclaw not configured. Run totalreclaw_setup first."})

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
    """Configure TotalReclaw credentials. Generates a new recovery phrase if none provided."""
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
        result = {
            "configured": True,
            "wallet_address": state.get_client().wallet_address,
        }
        if generated:
            result["recovery_phrase"] = recovery_phrase
            result["generated"] = True
        return json.dumps(result)
    except Exception as e:
        logger.error("totalreclaw_setup failed: %s", e)
        return json.dumps({"error": str(e)})
