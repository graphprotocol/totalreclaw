"""totalreclaw_report_qa_bug — RC-gated agent tool (Hermes edition).

Only registered when ``totalreclaw.__version__`` carries a pre-release
token (PEP-440 ``rcN`` or SemVer ``-rc.``). Lets the agent file a
structured QA bug issue to ``p-diogo/totalreclaw-internal`` during RC
testing without the maintainer opening a fresh issue manually.

All user-supplied free-text fields run through :func:`redact_secrets`
fail-close before the HTTPS POST: BIP-39 phrases, API keys, Telegram
bot tokens, and bearer-token auth headers are replaced with
``<REDACTED>``. The agent is also instructed (via SKILL.md addendum) to
not pass raw secrets, but redaction is the last line of defence.

The target repo defaults to ``p-diogo/totalreclaw-internal`` and can
only be overridden via the ``TOTALRECLAW_QA_REPO`` environment variable
to a repo slug ending in ``-internal``. Any other slug (including the
public ``p-diogo/totalreclaw`` repo) is rejected with a loud error —
QA bug reports frequently contain RC ship-stopper detail that must
never reach the public tracker. See rc.13 → rc.14 for the fix rationale.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RC-gate detection
# ---------------------------------------------------------------------------


def is_rc_build(version: Optional[str]) -> bool:
    """True when ``version`` is a pre-release RC build.

    Accepts SemVer ``-rc.N`` and PEP-440 ``rcN`` shapes. Rejects stable
    versions and non-RC pre-release tokens (e.g. ``-beta.1``).
    """
    if not version or not isinstance(version, str):
        return False
    v = version.lower()
    # SemVer: `-rc.N`
    if re.search(r"-rc\.\d+", v):
        return True
    # PEP-440: `rcN` (no dash)
    if re.search(r"\d+rc\d+", v):
        return True
    return False


# ---------------------------------------------------------------------------
# Redaction — fail-close
# ---------------------------------------------------------------------------

REDACTED = "<REDACTED>"

# BIP-39 mnemonic: 12 or 24 lowercase alpha-word phrases (accept 15/18/21 too).
# Shape-based — over-redacts legitimate 12-word lowercase sequences.
_BIP39 = re.compile(r"\b(?:[a-z]{3,10}(?:\s+[a-z]{3,10}){11,23})\b")

# OpenAI / Anthropic-style `sk-` keys.
_SK_KEY = re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")

# Google API key: `AIza` prefix + ~35 trailing chars (30–45 tolerant).
_GOOGLE = re.compile(r"\bAIza[0-9A-Za-z\-_]{30,45}\b")

# Telegram bot token: `\d+:[A-Za-z0-9_-]{35,}`.
_TELEGRAM = re.compile(r"\b\d{6,}:[A-Za-z0-9_\-]{35,}\b")

# Bearer token in Authorization header.
_BEARER = re.compile(r"(authorization[:\s]*bearer\s+)[A-Za-z0-9._\-+/=]+", re.IGNORECASE)

# X-Api-Key header.
_XAPIKEY = re.compile(r"(x-api-key[:\s]*)[A-Za-z0-9._\-+/=]{20,}", re.IGNORECASE)

# 64+ char hex blob (typical HKDF auth keys / raw private keys).
_LONG_HEX = re.compile(r"\b[a-fA-F0-9]{64,}\b")

# 0x-prefixed 64-hex (private-key-style).
_ETH_PRIVKEY = re.compile(r"\b0x[a-fA-F0-9]{64}\b")

# token=/secret=/auth_key= qualifiers with value.
_QUALIFIED_SECRET = re.compile(
    r"((?:token|secret|auth_key)\s*[=:]\s*)[A-Za-z0-9\-]{20,}",
    re.IGNORECASE,
)


def redact_secrets(text: Optional[str]) -> str:
    """Run a sequence of secret-shape patterns over ``text`` and replace
    each hit with ``<REDACTED>``.

    Order matters — longer/more-specific patterns run first so that a
    bearer-token regex doesn't eat its own header prefix.

    Over-redaction is acceptable: a random 12-word lowercase sentence is
    redacted, a 64-char hex commit SHA plus random bytes is redacted,
    etc. The alternative (leaking a real secret) is worse.
    """
    if not text or not isinstance(text, str):
        return ""
    out = text
    out = _BIP39.sub(REDACTED, out)
    out = _SK_KEY.sub(REDACTED, out)
    out = _GOOGLE.sub(REDACTED, out)
    out = _TELEGRAM.sub(REDACTED, out)
    out = _BEARER.sub(lambda m: m.group(1) + REDACTED, out)
    out = _XAPIKEY.sub(lambda m: m.group(1) + REDACTED, out)
    out = _LONG_HEX.sub(REDACTED, out)
    out = _ETH_PRIVKEY.sub(REDACTED, out)
    out = _QUALIFIED_SECRET.sub(lambda m: m.group(1) + REDACTED, out)
    return out


# ---------------------------------------------------------------------------
# Target repo guard — fail-loud on any repo that isn't the internal tracker.
# ---------------------------------------------------------------------------

DEFAULT_QA_REPO = "p-diogo/totalreclaw-internal"

# Repo slugs we KNOW are public. The slug must also end in ``-internal``
# (structural rule); this list is a belt-and-braces explicit denylist so
# the rule catches the historical ``p-diogo/totalreclaw`` leak even if a
# future repo rename skips the ``-internal`` suffix.
PUBLIC_REPOS_DENYLIST = frozenset({
    "p-diogo/totalreclaw",
    "p-diogo/totalreclaw-website",
    "p-diogo/totalreclaw-relay",
    "p-diogo/totalreclaw-plugin",
    "p-diogo/totalreclaw-hermes",
})


def resolve_qa_repo(
    override: Optional[str] = None,
    *,
    env: Optional[dict] = None,
) -> str:
    """Resolve the target repo for QA bug filings.

    Precedence (highest first):
      1. ``override`` argument (used by tests).
      2. ``TOTALRECLAW_QA_REPO`` environment variable.
      3. Default: ``p-diogo/totalreclaw-internal``.

    Raises ``RuntimeError`` if the resolved slug is on the public-repo
    denylist or does not end in ``-internal``. rc.13 QA surfaced a bug
    where agent-filed bug reports leaked to the public repo; this guard
    is the last line of defence to prevent that recurring.
    """
    env = os.environ if env is None else env
    candidate = (override or env.get("TOTALRECLAW_QA_REPO") or DEFAULT_QA_REPO).strip()
    if not candidate or "/" not in candidate:
        raise RuntimeError(
            f"invalid QA repo slug {candidate!r}: expected 'owner/name' format"
        )
    if candidate in PUBLIC_REPOS_DENYLIST:
        raise RuntimeError(
            f"refusing to file QA bug to PUBLIC repo {candidate!r}. "
            "QA bug reports contain RC ship-stopper detail that must not "
            "leak to public. Set TOTALRECLAW_QA_REPO to a repo ending in "
            "'-internal' (e.g. p-diogo/totalreclaw-internal)."
        )
    if not candidate.endswith("-internal"):
        raise RuntimeError(
            f"refusing to file QA bug to repo {candidate!r}: slug must end "
            "in '-internal' (structural safety rule). Override via "
            "TOTALRECLAW_QA_REPO only to another internal fork."
        )
    return candidate


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

VALID_INTEGRATIONS = {
    "plugin", "hermes", "nanoclaw", "mcp", "relay", "clawhub", "docs", "other",
}
INTEGRATION_DISPLAY = {
    "plugin": "OpenClaw plugin",
    "hermes": "Hermes Python",
    "nanoclaw": "NanoClaw skill",
    "mcp": "MCP server",
    "relay": "Relay (backend)",
    "clawhub": "ClawHub publishing",
    "docs": "Docs / setup guide",
    "other": "Other",
}
VALID_SEVERITIES = {"blocker", "high", "medium", "low"}
REQUIRED_FIELDS = ("integration", "rc_version", "severity", "title",
                   "symptom", "expected", "repro", "logs", "environment")


def validate_args(args: dict) -> Optional[str]:
    """Return an error string when ``args`` is invalid, ``None`` when valid."""
    if not isinstance(args, dict):
        return "args must be an object"
    missing = [
        f for f in REQUIRED_FIELDS
        if not args.get(f) or not isinstance(args.get(f), str)
    ]
    if missing:
        return f"missing or non-string fields: {', '.join(missing)}"
    if args["integration"] not in VALID_INTEGRATIONS:
        return (
            f"invalid integration {args['integration']!r}; "
            f"expected one of {sorted(VALID_INTEGRATIONS)}"
        )
    if args["severity"] not in VALID_SEVERITIES:
        return (
            f"invalid severity {args['severity']!r}; "
            f"expected one of {sorted(VALID_SEVERITIES)}"
        )
    if len(args["title"]) > 60:
        return "title must be <= 60 chars"
    return None


# ---------------------------------------------------------------------------
# Issue body builder
# ---------------------------------------------------------------------------


def build_issue_body(args: dict) -> str:
    """Render the redacted issue body mirroring ``qa-bug.yml`` layout."""
    integration = INTEGRATION_DISPLAY.get(args["integration"], args["integration"])
    return "\n".join([
        "_Filed automatically by the TotalReclaw RC bug-report tool._",
        "",
        "### Integration",
        integration,
        "",
        "### RC version",
        "`" + redact_secrets(args["rc_version"]) + "`",
        "",
        "### Severity",
        args["severity"],
        "",
        "### What happened",
        redact_secrets(args["symptom"]),
        "",
        "### What was expected",
        redact_secrets(args["expected"]),
        "",
        "### Reproduction steps",
        redact_secrets(args["repro"]),
        "",
        "### Relevant logs / evidence",
        "```",
        redact_secrets(args["logs"]),
        "```",
        "",
        "### Environment",
        redact_secrets(args["environment"]),
        "",
        "---",
        "> Reporter: LLM agent via `totalreclaw_report_qa_bug` (RC-gated tool)",
    ])


# ---------------------------------------------------------------------------
# HTTPS POST
# ---------------------------------------------------------------------------


async def post_qa_bug_issue(
    args: dict,
    *,
    github_token: str,
    repo: Optional[str] = None,
    http_client: Optional[httpx.AsyncClient] = None,
) -> dict:
    """POST the redacted issue to GitHub. Returns
    ``{"issue_url": ..., "issue_number": ...}`` on success; raises
    ``RuntimeError`` on validation or HTTP failure.

    ``repo`` is resolved through :func:`resolve_qa_repo` which reads the
    ``TOTALRECLAW_QA_REPO`` env var and refuses any slug that isn't a
    repo ending in ``-internal``. Pass a slug explicitly (tests only) to
    override env-var lookup.
    """
    err = validate_args(args)
    if err:
        raise RuntimeError(f"invalid args: {err}")
    if not github_token:
        raise RuntimeError("github_token is required")

    target_repo = resolve_qa_repo(repo)
    url = f"https://api.github.com/repos/{target_repo}/issues"
    title = "[qa-bug] " + redact_secrets(args["title"])
    body = build_issue_body(args)
    # Safe label value — strip chars that GH rejects in label names.
    rc_label_val = re.sub(r"[^A-Za-z0-9.\-]", "_", args["rc_version"])[:40]
    labels = [
        "qa-bug",
        "pending-triage",
        f"severity:{args['severity']}",
        f"component:{args['integration']}",
        f"rc:{rc_label_val}",
    ]
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {github_token}",
        "Content-Type": "application/json",
        "User-Agent": "totalreclaw-hermes-qa-bug",
    }
    payload = {"title": title, "body": body, "labels": labels}

    owns_client = http_client is None
    if http_client is None:
        http_client = httpx.AsyncClient(timeout=20.0)
    try:
        resp = await http_client.post(url, headers=headers, json=payload)
    finally:
        if owns_client:
            await http_client.aclose()

    if resp.status_code < 200 or resp.status_code >= 300:
        raise RuntimeError(
            f"GitHub API {resp.status_code}: {resp.text[:200]}"
        )
    data = resp.json()
    html_url = data.get("html_url")
    number = data.get("number")
    if not html_url or not isinstance(number, int):
        raise RuntimeError("GitHub API returned no html_url / number")
    logger.info("Filed QA bug #%s: %s", number, html_url)
    return {"issue_url": html_url, "issue_number": number}


# ---------------------------------------------------------------------------
# Tool handler (wired up in __init__.py when RC build)
# ---------------------------------------------------------------------------


async def report_qa_bug(args: dict, state, **kwargs) -> str:
    """Tool handler — JSON-out, matches the other totalreclaw_* tools.

    Gated at registration time in ``__init__.py`` so this handler is only
    reachable from RC builds. Still guards for missing token + invalid
    args because registration can't prevent runtime-env issues.
    """
    token = os.environ.get("TOTALRECLAW_QA_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        return json.dumps({
            "error": (
                "No GitHub token found. The operator must export "
                "TOTALRECLAW_QA_GITHUB_TOKEN (or GITHUB_TOKEN) with 'repo' "
                "scope to enable agent-filed bug reports during RC testing."
            ),
        })
    try:
        result = await post_qa_bug_issue(args, github_token=token)
        return json.dumps({
            "issue_url": result["issue_url"],
            "issue_number": result["issue_number"],
            "message": f"Filed QA bug #{result['issue_number']}: {result['issue_url']}",
        })
    except Exception as e:
        logger.error("totalreclaw_report_qa_bug failed: %s", e)
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------

SCHEMA = {
    "name": "totalreclaw_report_qa_bug",
    "description": (
        "File a structured QA bug report to the internal tracker. RC-only; "
        "never available in stable builds. Do NOT auto-file — ask the user "
        "first before invoking. The tool redacts recovery phrases, API "
        "keys, and Telegram bot tokens from all free-text fields before "
        "posting, but the agent SHOULD still avoid passing raw secrets."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "integration": {
                "type": "string",
                "enum": sorted(VALID_INTEGRATIONS),
                "description": "Which TotalReclaw surface is affected.",
            },
            "rc_version": {
                "type": "string",
                "description": (
                    "Exact RC version string "
                    '(e.g. "3.3.1-rc.3" or "2.3.1rc3").'
                ),
            },
            "severity": {
                "type": "string",
                "enum": sorted(VALID_SEVERITIES),
                "description": (
                    "blocker=release blocked, high=major UX failure, "
                    "medium=annoying, low=polish."
                ),
            },
            "title": {
                "type": "string",
                "description": (
                    'Short summary, <60 chars. Prefix "[qa-bug]" is added '
                    "automatically."
                ),
                "maxLength": 60,
            },
            "symptom": {
                "type": "string",
                "description": "What happened (redacted automatically).",
            },
            "expected": {
                "type": "string",
                "description": "What should have happened.",
            },
            "repro": {
                "type": "string",
                "description": (
                    "Reproduction steps (redacted automatically)."
                ),
            },
            "logs": {
                "type": "string",
                "description": (
                    "Log excerpts / error messages (redacted automatically)."
                ),
            },
            "environment": {
                "type": "string",
                "description": (
                    "Host, Docker/native, OpenClaw version, LLM provider, etc."
                ),
            },
        },
        "required": list(REQUIRED_FIELDS),
    },
}
