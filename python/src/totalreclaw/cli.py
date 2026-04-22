"""`totalreclaw` CLI — first-class entry point for setup + diagnostics.

Shipped in 2.3.1rc2 after Pedro's agent Hermes flagged a catch-22 in the
rc.1 UX: users who installed `pip install totalreclaw` standalone (no
Hermes, no OpenClaw) had nowhere to initialise the encrypted vault. The
`hermes setup` binary exists, but `pip install totalreclaw` with a bare
agent framework left users staring at a recovery-phrase-is-required
error with no CLI surface to reach.

Two subcommands:

* ``totalreclaw setup`` — interactive wizard that generates or restores a
  recovery phrase. Reuses the ``hermes setup`` logic via the shared
  :mod:`totalreclaw.hermes.cli` module so the two binaries behave
  identically. After writing credentials, eagerly derives the Smart
  Account address via RPC and persists it to ``credentials.json`` so
  subsequent status / doctor calls show the real address rather than
  ``pending``.

* ``totalreclaw doctor`` — health check. Verifies:
    - credentials.json exists, parses, mnemonic is a valid BIP-39 phrase.
    - Smart Account address is cached OR can be derived.
    - Embedding model is cached (Harrier-OSS-v1-270M ONNX).
    - An LLM provider key is present in env OR a Hermes config file.
    - Hermes plugin is registered (if Hermes is installed).
    - Relay URL is reachable (best-effort probe, 5s timeout).

Exit codes:
    0 — all healthy.
    1 — at least one issue (shown with a yellow ``warn`` or red ``fail``
        marker and a remediation hint).
    2 — setup not started (no credentials file) — a fast-path exit so
        doctor is the one place users see "run `totalreclaw setup`" loudly.

Coloured output is suppressed when stdout is not a TTY (plain text for
piped use).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

from totalreclaw.onboarding import CANONICAL_CREDENTIALS_PATH, detect_first_run


# ---------------------------------------------------------------------------
# Coloured-output helpers (no dependency — ANSI escape codes only, fallback
# to plain text when stdout isn't a TTY)
# ---------------------------------------------------------------------------


def _is_color_stream(stream) -> bool:
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def _c(code: str, text: str, stream=None) -> str:
    stream = stream if stream is not None else sys.stdout
    if _is_color_stream(stream):
        return f"\x1b[{code}m{text}\x1b[0m"
    return text


def _ok(text: str) -> str:
    return _c("32", f"[OK]    {text}")  # green


def _warn(text: str) -> str:
    return _c("33", f"[WARN]  {text}")  # yellow


def _fail(text: str) -> str:
    return _c("31", f"[FAIL]  {text}")  # red


def _info(text: str) -> str:
    return _c("36", f"[INFO]  {text}")  # cyan


# ---------------------------------------------------------------------------
# `setup` — delegates to totalreclaw.hermes.cli so the two binaries share
# the exact same wizard. After a successful setup, eagerly derives the
# Smart Account address.
# ---------------------------------------------------------------------------


def run_setup(
    credentials_path: Optional[Path] = None,
    emit_phrase: bool = False,
) -> int:
    """Run the interactive setup wizard.

    Delegates to the shared hermes wizard (which in 2.3.1rc2 handles
    eager Smart Account resolution itself — see
    ``_try_eager_resolve_scope_address`` in ``hermes/cli.py``). This
    keeps behaviour identical across ``hermes setup`` and
    ``totalreclaw setup``.
    """
    # Delay import so `totalreclaw doctor` on a setup-less box doesn't pay
    # for eth_account's import cost.
    from totalreclaw.hermes.cli import run_setup as _hermes_run_setup

    path = credentials_path if credentials_path is not None else CANONICAL_CREDENTIALS_PATH
    return _hermes_run_setup(credentials_path=path, emit_phrase=emit_phrase)


# ---------------------------------------------------------------------------
# `doctor` — health check. Exits 0 if all green, 1 on warnings, 2 if
# setup hasn't been started.
# ---------------------------------------------------------------------------


def run_doctor(credentials_path: Optional[Path] = None, relay_url: Optional[str] = None) -> int:
    """Walk through a fixed checklist and print pass/fail for each step."""
    path = credentials_path if credentials_path is not None else CANONICAL_CREDENTIALS_PATH
    issues = 0
    setup_started = True

    print("TotalReclaw doctor — health check\n")

    # --------------------------------------------------------------------
    # Check 1 — credentials.json exists + parses
    # --------------------------------------------------------------------
    if not path.exists():
        print(_fail(f"credentials.json not found at {path}"))
        print(f"        Run `totalreclaw setup` to create it.")
        setup_started = False
    else:
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
            if not isinstance(data, dict):
                print(_fail(f"credentials.json is not a JSON object"))
                issues += 1
            else:
                print(_ok(f"credentials.json exists at {path}"))
        except (OSError, json.JSONDecodeError) as err:
            print(_fail(f"credentials.json is corrupt: {err}"))
            issues += 1
            setup_started = False

    if not setup_started:
        # No point running the rest of the checks; user needs to set up first.
        return 2

    # Re-read data for the remaining checks (we know file exists now).
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}

    mnemonic = data.get("mnemonic") or data.get("recovery_phrase")

    # --------------------------------------------------------------------
    # Check 2 — mnemonic is a valid BIP-39 phrase
    # --------------------------------------------------------------------
    if not isinstance(mnemonic, str) or not mnemonic.strip():
        print(_fail("credentials.json has no mnemonic field"))
        issues += 1
    else:
        try:
            from eth_account import Account
            Account.enable_unaudited_hdwallet_features()
            Account.from_mnemonic(mnemonic.strip(), account_path="m/44'/60'/0'/0/0")
            print(_ok("Recovery phrase is a valid BIP-39 12-word mnemonic"))
        except Exception as err:
            print(_fail(f"Recovery phrase validation failed: {err}"))
            issues += 1

    # --------------------------------------------------------------------
    # Check 3 — Smart Account address
    # --------------------------------------------------------------------
    cached_sa = data.get("scope_address") or data.get("wallet_address")
    if cached_sa and isinstance(cached_sa, str):
        print(_ok(f"Smart Account address cached: {cached_sa}"))
    elif isinstance(mnemonic, str) and mnemonic.strip():
        # Derive on-the-fly
        try:
            import asyncio
            from totalreclaw.client import _derive_smart_account_address

            sa = asyncio.run(_derive_smart_account_address(mnemonic.strip()))
            if sa:
                print(_ok(f"Smart Account address resolved on-the-fly: {sa}"))
                print("        (not cached in credentials.json — run `totalreclaw setup` to cache)")
            else:
                print(_warn("Smart Account address could not be derived (RPC returned empty)"))
                issues += 1
        except Exception as err:
            print(_warn(f"Smart Account address derivation failed: {err}"))
            issues += 1

    # --------------------------------------------------------------------
    # Check 4 — embedding model cached
    # --------------------------------------------------------------------
    try:
        # The embedding module lazy-initialises a singleton; we just probe
        # the HF cache path to see if the model has already been downloaded.
        hf_cache_root = Path(
            os.environ.get("HF_HOME", str(Path.home() / ".cache" / "huggingface"))
        )
        hub_cache = hf_cache_root / "hub"
        # Harrier model id follows HF's `models--owner--repo` pattern.
        candidates = (
            list(hub_cache.glob("models--*harrier*"))
            if hub_cache.exists()
            else []
        )
        if candidates:
            print(_ok(f"Embedding model cached under {candidates[0].name}"))
        else:
            print(_warn("Embedding model NOT cached — first recall will download ~216 MB from HuggingFace"))
            # Not counted as an issue — it's a first-run one-time cost.
    except Exception as err:
        print(_warn(f"Could not verify embedding-model cache: {err}"))

    # --------------------------------------------------------------------
    # Check 5 — LLM provider key present
    # --------------------------------------------------------------------
    env_keys = [
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "ZAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "MISTRAL_API_KEY",
        "GROQ_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENROUTER_API_KEY",
        "XAI_API_KEY",
    ]
    present = [k for k in env_keys if os.environ.get(k)]
    if present:
        print(_ok(f"LLM provider key(s) present in env: {', '.join(present)}"))
    else:
        print(_warn("No LLM provider key found in env (auto-extraction may be disabled)"))
        print(
            "        Set one of: " + ", ".join(env_keys[:4]) + ", ..."
        )

    # --------------------------------------------------------------------
    # Check 6 — Hermes plugin registered (if Hermes is installed)
    # --------------------------------------------------------------------
    try:
        import importlib

        importlib.import_module("hermes_agent")
        hermes_installed = True
    except ImportError:
        hermes_installed = False

    if hermes_installed:
        # Check Hermes plugin entry point
        try:
            from importlib.metadata import entry_points

            eps = entry_points()
            if hasattr(eps, "select"):
                hermes_eps = list(eps.select(group="hermes_agent.plugins"))
            else:
                hermes_eps = eps.get("hermes_agent.plugins", [])  # type: ignore
            tr_registered = any(ep.name == "totalreclaw" for ep in hermes_eps)
            if tr_registered:
                print(_ok("Hermes plugin entry-point `totalreclaw` is registered"))
            else:
                print(_warn("Hermes is installed but `totalreclaw` plugin entry is missing"))
                issues += 1
        except Exception as err:
            print(_warn(f"Could not check Hermes plugin registration: {err}"))
    else:
        print(_info("Hermes not installed (skipping plugin-registration check)"))

    # --------------------------------------------------------------------
    # Check 7 — Relay reachable
    # --------------------------------------------------------------------
    resolved_relay = (
        relay_url
        or os.environ.get("TOTALRECLAW_SERVER_URL")
        or "https://api.totalreclaw.xyz"
    )
    try:
        import httpx

        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{resolved_relay.rstrip('/')}/health")
            if 200 <= resp.status_code < 500:
                print(_ok(f"Relay reachable at {resolved_relay} (HTTP {resp.status_code})"))
            else:
                print(_warn(f"Relay at {resolved_relay} returned HTTP {resp.status_code}"))
                issues += 1
    except Exception as err:
        print(_warn(f"Could not reach relay at {resolved_relay}: {err}"))

    # --------------------------------------------------------------------
    # Summary
    # --------------------------------------------------------------------
    print()
    if issues == 0:
        print(_ok(f"All checks passed. TotalReclaw is healthy."))
        return 0
    else:
        print(_warn(f"{issues} issue(s) found. See messages above for remediation."))
        return 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="totalreclaw",
        description=(
            "TotalReclaw — End-to-end encrypted memory for AI agents (Python client). "
            "Run `totalreclaw setup` to initialise your encrypted vault or "
            "`totalreclaw doctor` to diagnose an existing install."
        ),
    )
    sub = parser.add_subparsers(dest="command")

    sp_setup = sub.add_parser(
        "setup",
        help=(
            "Interactive wizard to generate a NEW recovery phrase or restore an existing "
            "one. Writes credentials to ~/.totalreclaw/credentials.json and eagerly resolves "
            "the Smart Account address."
        ),
    )
    sp_setup.add_argument(
        "--credentials-path",
        type=Path,
        default=None,
        help="Override the credentials file location (default: ~/.totalreclaw/credentials.json).",
    )
    sp_setup.add_argument(
        "--emit-phrase",
        action="store_true",
        default=False,
        help=(
            "POWER-USER OPT-IN. Display the generated recovery phrase in the terminal "
            "(stderr). Default: silent-save."
        ),
    )

    sp_doctor = sub.add_parser(
        "doctor",
        help=(
            "Health check — verifies credentials, mnemonic validity, Smart Account address, "
            "embedding-model cache, LLM provider keys, Hermes plugin registration, and relay "
            "reachability. Exits 0 if all pass, 1 on warnings, 2 if setup isn't started."
        ),
    )
    sp_doctor.add_argument(
        "--credentials-path",
        type=Path,
        default=None,
        help="Override the credentials file location.",
    )
    sp_doctor.add_argument(
        "--relay-url",
        type=str,
        default=None,
        help="Override the relay URL for the reachability check.",
    )

    args = parser.parse_args(argv)

    if args.command == "setup":
        return run_setup(
            credentials_path=args.credentials_path,
            emit_phrase=getattr(args, "emit_phrase", False),
        )
    if args.command == "doctor":
        return run_doctor(
            credentials_path=args.credentials_path,
            relay_url=args.relay_url,
        )

    # No subcommand — check whether setup has been done, then nudge accordingly.
    if not CANONICAL_CREDENTIALS_PATH.exists():
        print(
            "TotalReclaw is not set up yet. Run `totalreclaw setup` to create a recovery "
            "phrase and initialise your encrypted vault.\n"
        )
    else:
        print(
            f"TotalReclaw is configured at {CANONICAL_CREDENTIALS_PATH}. "
            "Run `totalreclaw doctor` to verify everything is healthy.\n"
        )
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
