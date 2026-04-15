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
    """Store a memory in TotalReclaw.

    Phase 2.2.6: now forwards ``type`` + ``importance`` from the tool call to
    the canonical Claim builder via ``client.remember``. Prior to 2.2.6 the
    tool accepted only ``text`` + ``importance`` and stored everything as
    ``type='fact'``, making the rule category structurally unreachable from
    Hermes.
    """
    from totalreclaw.agent.extraction import VALID_MEMORY_TYPES

    client = state.get_client()
    if not client:
        return json.dumps({"error": "TotalReclaw not configured. Run totalreclaw_setup first."})

    text = args.get("text", "").strip()
    if not text:
        return json.dumps({"error": "No text provided"})

    # Default importance to 8 for explicit remember (same convention as
    # the OpenClaw plugin) — higher than auto-extraction's typical 6-7 so
    # store-time dedup's shouldSupersede prefers the explicit call.
    raw_importance = args.get("importance", 8)
    try:
        importance_val = float(raw_importance)
    except (TypeError, ValueError):
        importance_val = 8.0
    # store_fact handles both 0.0-1.0 and 1-10 input, but we clamp to sensible range
    importance_val = max(1.0, min(10.0, importance_val))

    # Validate + default the type
    fact_type_raw = args.get("type", "fact")
    fact_type = fact_type_raw if fact_type_raw in VALID_MEMORY_TYPES else "fact"

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
            confidence=1.0,  # explicit remember = highest confidence
        )
        return json.dumps({"stored": True, "fact_id": fact_id, "type": fact_type, "importance": importance_val})
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


async def pin(args: dict, state: "PluginState", **kwargs) -> str:
    """Pin a memory so auto-resolution cannot supersede it.

    Phase 2 knowledge-graph pinning semantics: the claim is rewritten with
    ``status=pinned``, the old fact is tombstoned, and a new fact is
    written that ``supersedes`` the old one. Idempotent on already-pinned
    claims.
    """
    client = state.get_client()
    if not client:
        return json.dumps({"error": "TotalReclaw not configured. Run totalreclaw_setup first."})

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
        return json.dumps({"error": "TotalReclaw not configured. Run totalreclaw_setup first."})

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


# ── Import Tools ─────────────────────────────────────────────────────────────


def _read_hermes_llm_config():
    """Read the LLM provider config from Hermes's own config files.

    Hermes stores its config in ~/.hermes/config.yaml (provider + model)
    and ~/.hermes/.env (API keys). This reads both to build an LLM config
    that matches what Hermes itself uses — no separate env vars needed.
    """
    import yaml
    from pathlib import Path
    from totalreclaw.agent.llm_client import LLMConfig

    hermes_dir = Path.home() / ".hermes"
    config_path = hermes_dir / "config.yaml"
    env_path = hermes_dir / ".env"

    if not config_path.exists():
        return None

    # Read config.yaml for provider + model
    with open(config_path) as f:
        cfg = yaml.safe_load(f) or {}

    model_cfg = cfg.get("model", {})
    provider = model_cfg.get("provider", "")
    model = model_cfg.get("model", "")

    if not provider or not model:
        return None

    # Read .env for API keys
    env_vars = {}
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    env_vars[k.strip()] = v.strip()

    # Map Hermes provider names to API keys and base URLs
    provider_key_map = {
        "zai": (["ZAI_API_KEY", "GLM_API_KEY"], "https://api.z.ai/api/coding/paas/v4"),
        "openai": (["OPENAI_API_KEY"], "https://api.openai.com/v1"),
        "anthropic": (["ANTHROPIC_API_KEY"], "https://api.anthropic.com/v1"),
        "openrouter": (["OPENROUTER_API_KEY"], "https://openrouter.ai/api/v1"),
        "groq": (["GROQ_API_KEY"], "https://api.groq.com/openai/v1"),
        "deepseek": (["DEEPSEEK_API_KEY"], "https://api.deepseek.com/v1"),
        "mistral": (["MISTRAL_API_KEY"], "https://api.mistral.ai/v1"),
    }

    key_names, default_base_url = provider_key_map.get(provider.lower(), ([], ""))
    api_key = None
    for kn in key_names:
        api_key = env_vars.get(kn) or os.environ.get(kn)
        if api_key:
            break

    if not api_key:
        return None

    # Respect custom base URL from .env
    base_url = env_vars.get("GLM_BASE_URL") or env_vars.get("OPENAI_BASE_URL") or default_base_url
    api_format = "anthropic" if provider.lower() == "anthropic" else "openai"

    return LLMConfig(api_key=api_key, base_url=base_url, model=model, api_format=api_format)


def _make_extractor(state: "PluginState"):
    """Build an async LLM extraction callable using Hermes's own LLM config.

    Reads from ~/.hermes/config.yaml + ~/.hermes/.env to match the host
    agent's configured provider. Falls back to detect_llm_config() for
    non-Hermes environments.
    """
    from totalreclaw.agent.extraction import (
        EXTRACTION_SYSTEM_PROMPT,
        _truncate_messages,
        _parse_response,
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

        parsed = _parse_response(response)
        return [
            {
                "text": f.text,
                "type": f.type,
                "importance": f.importance,
                "action": f.action,
            }
            for f in parsed
        ]

    return extract


async def import_from(args: dict, state: "PluginState", **kwargs) -> str:
    """Import memories from other AI tools (Gemini, ChatGPT, Claude, Mem0, etc.)."""
    client = state.get_client()
    if not client:
        return json.dumps({"error": "TotalReclaw not configured. Run totalreclaw_setup first."})

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
        return json.dumps({"error": "TotalReclaw not configured. Run totalreclaw_setup first."})

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
