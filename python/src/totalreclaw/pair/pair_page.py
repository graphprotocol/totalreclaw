"""pair.pair_page — self-contained HTML + inline JS + CSS for the browser
side of the local-mode pair flow (``TOTALRECLAW_PAIR_MODE=local``).

rc.13 UX refresh: mirrors the relay's production "wizard" UX (see
``p-diogo/totalreclaw-relay:src/routes/pair-html.ts``). Three-step
typeform-style flow — PIN → phrase → done — with the 4 user-ratified
design decisions baked in:

  1. 6-cell PIN input + ``autocomplete="one-time-code"`` on cell 1.
  2. Returning-user tab label is "Log in".
  3. Step-1 subheading is agent-generic.
  4. Paste into PIN auto-advances + auto-submits.

Shape parity with the relay ensures a user who pairs on one install
sees the exact same UX regardless of whether they're in default
relay-mode or air-gapped local-mode.

Crypto is unchanged from rc.12: x25519 + HKDF-SHA256 + AES-256-GCM
with ``HKDF_INFO = "totalreclaw-pair-v2"``. The browser derives a
shared key against the gateway's ephemeral pubkey in the URL fragment,
encrypts the phrase + POSTs the ciphertext to ``api_base`` on the
gateway's loopback HTTP server.

Security properties:

  - Entirely self-contained (no CDN, no external fetch). Inline CSS + JS.
  - No ``console.log`` of phrase / PIN / key material.
  - Memory scrubbed after submit (best-effort — browser GC is not
    guaranteed).
  - ``cache-control: no-store`` + strict CSP on the response (set by the
    HTTP server, not this module).

BIP-39 wordlist: kept out of this module to reduce the Python wheel's
size; ``generate`` mode uses the ``mnemonic`` package's wordlist if
available. See :func:`_bip39_words`.
"""
from __future__ import annotations

import html as _html
import json
from functools import lru_cache
from typing import List


@lru_cache(maxsize=1)
def _bip39_words() -> List[str]:
    """Load the BIP-39 English wordlist from the ``mnemonic`` package if
    available; otherwise return an empty list and the browser falls back
    to ``import`` mode only (``generate`` mode displays an error).

    The ``mnemonic`` package is a ``dev`` extra, not a core dep — we
    don't want to pull it into every ``pip install totalreclaw``. If a
    user runs the pair flow without it, they can still use ``import``
    mode to paste an existing phrase.
    """
    try:
        from mnemonic import Mnemonic  # type: ignore

        m = Mnemonic("english")
        words = list(m.wordlist)
        if len(words) == 2048:
            return words
        return []
    except Exception:
        return []


def _escape_js_string(s: str) -> str:
    """JSON-encode a string for safe ``<script>`` embedding."""
    encoded = json.dumps(s, ensure_ascii=False)
    return (
        encoded.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def render_pair_page(
    *,
    sid: str,
    mode: str,
    expires_at_ms: int,
    api_base: str,
    now_ms: int,
) -> str:
    """Render the self-contained wizard-style pair HTML page.

    ``api_base`` is the full path the browser POSTs ciphertext to
    (e.g. ``/pair/<token>``). Local-mode uses POST to ``api_base``
    directly (no ``/respond`` suffix — the relay and local-mode differ
    in wire shape here; local-mode's handler accepts the POST body at
    exactly the URL the page was served from).
    """
    if mode not in ("generate", "import"):
        raise ValueError(f"pair.pair_page: invalid mode '{mode}'")

    sid_js = _escape_js_string(sid)
    mode_js = _escape_js_string(mode)
    api_base_js = _escape_js_string(api_base)
    bip39 = _bip39_words()
    bip39_js = json.dumps(bip39)
    expires_safe = int(expires_at_ms)
    sid_html = _html.escape(sid)

    # Panel rendering: local-mode is single-mode (generate XOR import),
    # so no tab switcher. The PIN screen + done screen are always there.
    if mode == "generate":
        panels_html = _GENERATE_PANEL_HTML
        default_mode_js = "generate"
    else:
        panels_html = _IMPORT_PANEL_HTML
        default_mode_js = "import"

    script = (
        _PAIR_SCRIPT
        .replace("__SID_JS__", sid_js)
        .replace("__MODE_JS__", mode_js)
        .replace("__API_BASE_JS__", api_base_js)
        .replace("__EXPIRES_AT_MS__", str(expires_safe))
        .replace("__BIP39_JS__", bip39_js)
        .replace("__DEFAULT_MODE_JS__", _escape_js_string(default_mode_js))
    )

    return (
        _PAIR_PAGE_HTML
        .replace("__SID_HTML__", sid_html)
        .replace("__PANELS__", panels_html)
        .replace("__STYLE__", _PAIR_STYLE)
        .replace("__SCRIPT__", script)
    )


# ---------------------------------------------------------------------------
# CSS — derived from docs/mockups/rc13-pair-wizard/wizard.css. Mirrors the
# relay's pair-html.ts PAGE_STYLE bit-for-bit. Keep them in sync when the
# mockup is iterated so local-mode and relay look identical.
# ---------------------------------------------------------------------------

_PAIR_STYLE = r"""
:root {
  --bg: #0B0B1A; --bg-elev: #141329; --bg-elev-2: #1B1A36;
  --fg: #F0EDF8; --fg-dim: rgba(240, 237, 248, 0.70); --fg-faint: rgba(240, 237, 248, 0.42);
  --border: rgba(255, 255, 255, 0.08); --border-strong: rgba(255, 255, 255, 0.16);
  --purple: #7B5CFF; --purple-soft: rgba(123, 92, 255, 0.14);
  --purple-softer: rgba(123, 92, 255, 0.08); --purple-ring: rgba(123, 92, 255, 0.32);
  --orange: #D4943A; --orange-soft: rgba(212, 148, 58, 0.16);
  --danger: #FF5A6A; --success: #45D178; --success-soft: rgba(69, 209, 120, 0.12);
  --shadow-soft: 0 1px 2px rgba(0, 0, 0, 0.2), 0 8px 32px rgba(0, 0, 0, 0.28);
  --shadow-lift: 0 4px 16px rgba(123, 92, 255, 0.22);
  --radius-sm: 8px; --radius: 12px; --radius-lg: 16px; --radius-xl: 24px;
  --transition-fast: 140ms cubic-bezier(0.2, 0.8, 0.2, 1);
  --transition: 240ms cubic-bezier(0.2, 0.8, 0.2, 1);
}
@media (prefers-color-scheme: light) {
  :root {
    --bg: #F7F6FB; --bg-elev: #FFFFFF; --bg-elev-2: #F0EEF8;
    --fg: #18182B; --fg-dim: rgba(24, 24, 43, 0.70); --fg-faint: rgba(24, 24, 43, 0.48);
    --border: rgba(24, 24, 43, 0.10); --border-strong: rgba(24, 24, 43, 0.18);
    --purple: #6B48FF; --purple-soft: rgba(107, 72, 255, 0.10);
    --purple-softer: rgba(107, 72, 255, 0.05); --purple-ring: rgba(107, 72, 255, 0.28);
    --orange: #B87320; --orange-soft: rgba(184, 115, 32, 0.12);
    --shadow-soft: 0 1px 2px rgba(24, 24, 43, 0.05), 0 8px 32px rgba(24, 24, 43, 0.08);
  }
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html { font-size: 16px; -webkit-text-size-adjust: 100%; }
body {
  min-height: 100vh; min-height: 100dvh; background: var(--bg); color: var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  font-size: 1rem; line-height: 1.5;
  -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale;
  letter-spacing: -0.005em; overflow-x: hidden;
}
body::before {
  content: ""; position: fixed; inset: -20vmax 0 auto 0; height: 60vmax;
  background:
    radial-gradient(60% 60% at 50% 30%, var(--purple-softer), transparent 70%),
    radial-gradient(50% 50% at 80% 10%, var(--orange-soft), transparent 70%);
  pointer-events: none; z-index: 0; opacity: 0.8;
}
a { color: var(--purple); text-decoration: none; }
a:hover { text-decoration: underline; }
button { font: inherit; color: inherit; background: transparent; border: 0; cursor: pointer; }
em { font-style: normal; color: var(--fg); font-weight: 500; background: var(--purple-softer); padding: 0.1em 0.4em; border-radius: 6px; }
.skip-link { position: absolute; left: -9999px; top: 0; padding: 8px 12px; background: var(--purple); color: #fff; border-radius: 6px; }
.skip-link:focus { left: 12px; top: 12px; z-index: 999; }
.wizard {
  position: relative; z-index: 1; max-width: 520px; margin: 0 auto;
  padding: 18px 20px calc(24px + env(safe-area-inset-bottom));
  min-height: 100vh; min-height: 100dvh; display: flex; flex-direction: column;
}
.topbar { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 4px 2px 20px; }
.brand { display: inline-flex; align-items: center; gap: 10px; color: var(--fg); font-weight: 600; text-decoration: none; }
.brand-mark { width: 28px; height: 28px; flex-shrink: 0; }
.brand-text { font-size: 0.98rem; letter-spacing: 0.005em; }
.meta { display: flex; align-items: center; gap: 14px; }
.step-meter { display: flex; align-items: center; gap: 10px; padding: 6px 10px; background: var(--bg-elev); border: 1px solid var(--border); border-radius: 999px; font-size: 0.78rem; color: var(--fg-dim); font-variant-numeric: tabular-nums; }
.step-dots { display: flex; gap: 4px; }
.step-dots .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--border-strong); transition: background var(--transition), transform var(--transition); }
.step-dots .dot.done { background: var(--purple); }
.step-dots .dot.active { background: var(--purple); transform: scale(1.4); box-shadow: 0 0 0 3px var(--purple-ring); }
.countdown { display: inline-flex; align-items: center; gap: 6px; padding: 6px 10px; background: var(--bg-elev); border: 1px solid var(--border); border-radius: 999px; font-size: 0.82rem; font-variant-numeric: tabular-nums; color: var(--fg-dim); transition: color var(--transition-fast), border-color var(--transition-fast), background var(--transition-fast); }
.countdown .clock { width: 14px; height: 14px; }
.countdown.warn { color: var(--orange); border-color: var(--orange); background: var(--orange-soft); }
.countdown.warn .clock { animation: pulse 1s ease-in-out infinite; }
.countdown.expired { color: var(--danger); border-color: var(--danger); }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.45; } }
.stage { position: relative; flex: 1; display: flex; }
.screen { position: absolute; inset: 0; display: flex; flex-direction: column; gap: 18px; padding: 6px 2px 0; opacity: 0; transform: translateX(24px); transition: opacity var(--transition), transform var(--transition); pointer-events: none; }
.screen.active { position: relative; inset: auto; opacity: 1; transform: translateX(0); pointer-events: auto; width: 100%; }
.screen.exit-left { position: absolute; inset: 0; opacity: 0; transform: translateX(-32px); pointer-events: none; }
.screen.enter-right { position: absolute; inset: 0; opacity: 0; transform: translateX(32px); pointer-events: none; }
@media (prefers-reduced-motion: reduce) { .screen { transition: opacity 0.1s linear; transform: none !important; } .screen.exit-left, .screen.enter-right { transform: none !important; } }
.screen-inner { flex: 1; display: flex; flex-direction: column; gap: 14px; }
.screen-inner.center { align-items: center; justify-content: center; text-align: center; padding-top: 24px; }
.eyebrow { font-size: 0.78rem; color: var(--fg-faint); letter-spacing: 0.14em; text-transform: uppercase; font-weight: 500; }
.heading { font-size: clamp(1.6rem, 5.8vw, 2.1rem); font-weight: 600; line-height: 1.15; letter-spacing: -0.02em; color: var(--fg); }
.subheading { font-size: 1rem; color: var(--fg-dim); line-height: 1.55; }
.helper { font-size: 0.92rem; color: var(--fg-dim); margin: 4px 0 8px; }
.screen-cta { position: sticky; bottom: 0; padding: 18px 0 2px; background: linear-gradient(to top, var(--bg) 65%, transparent); display: flex; flex-direction: column; gap: 14px; }
.cta-row { display: flex; gap: 10px; }
.btn-primary, .btn-secondary, .btn-ghost { display: inline-flex; align-items: center; justify-content: center; gap: 8px; min-height: 52px; padding: 14px 20px; border-radius: var(--radius); font-size: 1rem; font-weight: 600; transition: transform var(--transition-fast), background var(--transition-fast), box-shadow var(--transition-fast), border-color var(--transition-fast), opacity var(--transition-fast); -webkit-tap-highlight-color: transparent; user-select: none; }
.btn-primary { flex: 1; background: var(--purple); color: #fff; border: 1px solid var(--purple); box-shadow: var(--shadow-lift); position: relative; }
.btn-primary:hover:not(:disabled) { transform: translateY(-1px); box-shadow: 0 6px 22px rgba(123, 92, 255, 0.36); }
.btn-primary:active:not(:disabled) { transform: translateY(0); }
.btn-primary:disabled { background: var(--bg-elev-2); border-color: var(--border); color: var(--fg-faint); box-shadow: none; cursor: not-allowed; }
.btn-primary:focus-visible { outline: 2px solid var(--purple); outline-offset: 3px; }
.btn-primary.loading .btn-label { opacity: 0; }
.btn-primary.loading .btn-spinner { opacity: 1; }
.btn-spinner { position: absolute; width: 20px; height: 20px; border-radius: 50%; border: 2px solid rgba(255,255,255,0.35); border-top-color: #fff; opacity: 0; animation: spin 0.85s linear infinite; transition: opacity var(--transition-fast); }
@keyframes spin { to { transform: rotate(360deg); } }
.btn-secondary { flex: 0 0 56px; padding: 14px; background: var(--bg-elev); border: 1px solid var(--border); color: var(--fg-dim); }
.btn-secondary:hover { border-color: var(--border-strong); color: var(--fg); }
.btn-secondary svg { width: 20px; height: 20px; }
.btn-ghost { width: 100%; background: var(--bg-elev); border: 1px solid var(--border); color: var(--fg-dim); font-weight: 500; min-height: 44px; padding: 10px 16px; }
.btn-ghost:hover { border-color: var(--border-strong); color: var(--fg); }
.btn-ghost svg { width: 18px; height: 18px; }
.btn-ghost-inline { width: auto; min-height: 36px; padding: 7px 14px; font-size: 0.86rem; font-weight: 500; border-radius: 999px; gap: 6px; }
.btn-ghost-inline svg { width: 15px; height: 15px; }
.security-note { display: flex; gap: 10px; align-items: flex-start; font-size: 0.85rem; color: var(--fg-dim); line-height: 1.5; padding: 12px 14px; background: var(--purple-softer); border: 1px solid var(--border); border-radius: var(--radius); }
.security-note .lock { width: 16px; height: 16px; flex-shrink: 0; margin-top: 2px; color: var(--purple); }
.pin-wrap { margin-top: 10px; }
.pin-cells { display: flex; align-items: center; justify-content: space-between; gap: 6px; max-width: 360px; margin: 4px auto 10px; }
.pin-cell { flex: 1; width: 100%; max-width: 54px; aspect-ratio: 1 / 1.15; text-align: center; font-size: 1.6rem; font-weight: 600; font-variant-numeric: tabular-nums; color: var(--fg); background: var(--bg-elev); border: 1.5px solid var(--border-strong); border-radius: var(--radius); transition: border-color var(--transition-fast), box-shadow var(--transition-fast), background var(--transition-fast), transform var(--transition-fast); caret-color: var(--purple); }
.pin-cell:focus { outline: none; border-color: var(--purple); background: var(--bg-elev-2); box-shadow: 0 0 0 4px var(--purple-ring); transform: translateY(-1px); }
.pin-cell.filled { border-color: var(--purple); background: var(--purple-soft); }
.pin-cell.shake { animation: shake 0.4s ease-in-out; }
@keyframes shake { 10%, 90% { transform: translateX(-1px); } 20%, 80% { transform: translateX(2px); } 30%, 50%, 70% { transform: translateX(-3px); } 40%, 60% { transform: translateX(3px); } }
.pin-sep { display: inline-block; width: 10px; height: 2px; background: var(--border-strong); border-radius: 2px; margin: 0 2px; flex: 0 0 auto; }
.pin-error { min-height: 20px; font-size: 0.85rem; color: var(--danger); text-align: center; opacity: 0; transition: opacity var(--transition-fast); }
.pin-error.show { opacity: 1; }
.pin-actions { display: flex; justify-content: center; margin: 6px 0 2px; }
.phrase-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; padding: 14px 12px; background: var(--bg-elev); border: 1px solid var(--border); border-radius: var(--radius); margin: 4px 0 10px; }
@media (min-width: 420px) { .phrase-grid { grid-template-columns: repeat(3, 1fr); gap: 10px; padding: 16px; } }
.word-cell { display: flex; align-items: center; gap: 8px; padding: 10px 12px 10px 10px; background: var(--bg-elev-2); border: 1px solid var(--border); border-radius: var(--radius-sm); transition: border-color var(--transition-fast), background var(--transition-fast); min-height: 48px; }
.word-cell:focus-within { border-color: var(--purple); background: var(--bg-elev); box-shadow: 0 0 0 3px var(--purple-ring); }
.word-cell.filled { border-color: var(--purple-ring); }
.word-idx { color: var(--fg-faint); font-size: 0.74rem; font-variant-numeric: tabular-nums; font-weight: 600; width: 18px; flex-shrink: 0; text-align: right; }
.word-input { flex: 1; min-width: 0; border: 0; background: transparent; color: var(--fg); font-size: 0.98rem; font-weight: 500; font-family: inherit; outline: none; padding: 0; }
.word-input::placeholder { color: var(--fg-faint); font-weight: 400; }
.phrase-grid.readonly .word-cell { background: var(--bg-elev); border-color: var(--border); }
.phrase-grid.readonly .word-input { color: var(--fg); font-weight: 600; cursor: default; user-select: all; }
.gen-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 4px 0 10px; }
.ack { display: flex; gap: 12px; align-items: flex-start; padding: 14px; background: var(--bg-elev); border: 1px solid var(--border); border-radius: var(--radius); margin: 4px 0 10px; cursor: pointer; transition: border-color var(--transition-fast), background var(--transition-fast); }
.ack:hover { border-color: var(--border-strong); }
.ack:has(input:checked) { border-color: var(--purple); background: var(--purple-softer); }
.ack input { appearance: none; -webkit-appearance: none; width: 22px; height: 22px; flex-shrink: 0; margin-top: 1px; border: 1.5px solid var(--border-strong); border-radius: 6px; background: var(--bg); cursor: pointer; position: relative; transition: background var(--transition-fast), border-color var(--transition-fast); }
.ack input:checked { background: var(--purple); border-color: var(--purple); }
.ack input:checked::after { content: ""; position: absolute; inset: 0; background: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='white' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'><polyline points='5 12 10 17 19 7'/></svg>") center / 14px no-repeat; }
.ack span { font-size: 0.92rem; color: var(--fg-dim); line-height: 1.5; }
.ack:has(input:checked) span { color: var(--fg); }
.check-wrap { width: 112px; height: 112px; margin: 8px auto 10px; position: relative; }
.check-wrap::before { content: ""; position: absolute; inset: -16px; border-radius: 50%; background: radial-gradient(circle, rgba(69, 209, 120, 0.22), transparent 70%); animation: halo 1.6s ease-out forwards; }
@keyframes halo { 0% { transform: scale(0.6); opacity: 0; } 30% { opacity: 1; } 100% { transform: scale(1.3); opacity: 0; } }
.check { width: 100%; height: 100%; display: block; }
.check-ring { stroke: var(--success); stroke-dasharray: 276; stroke-dashoffset: 276; animation: draw-ring 0.55s cubic-bezier(0.2, 0.8, 0.2, 1) forwards; }
.check-tick { stroke: var(--success); stroke-dasharray: 80; stroke-dashoffset: 80; animation: draw-tick 0.5s cubic-bezier(0.2, 0.8, 0.2, 1) 0.45s forwards; }
@keyframes draw-ring { to { stroke-dashoffset: 0; } }
@keyframes draw-tick { to { stroke-dashoffset: 0; } }
.close-link { display: inline-block; margin-top: 22px; padding: 10px 18px; border-radius: 999px; background: var(--bg-elev); border: 1px solid var(--border); color: var(--fg-dim); font-size: 0.9rem; font-weight: 500; }
.err-banner { display: flex; gap: 10px; align-items: flex-start; padding: 12px 14px; background: rgba(255,90,106,0.08); border: 1px solid var(--danger); border-radius: var(--radius); color: var(--danger); font-size: 0.9rem; line-height: 1.5; margin-top: 8px; }
.err-banner[hidden] { display: none; }
.toast { position: fixed; left: 50%; bottom: calc(88px + env(safe-area-inset-bottom)); transform: translateX(-50%) translateY(20px); padding: 10px 16px; background: var(--bg-elev-2); border: 1px solid var(--border-strong); border-radius: 999px; color: var(--fg); font-size: 0.86rem; box-shadow: var(--shadow-soft); opacity: 0; pointer-events: none; z-index: 10; transition: opacity var(--transition), transform var(--transition); max-width: calc(100% - 32px); text-align: center; }
.toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
@media (min-width: 600px) { .wizard { padding: 36px 24px 40px; max-width: 560px; } .heading { font-size: 2.1rem; } .pin-cell { max-width: 60px; font-size: 1.8rem; } }
@media (max-width: 360px) { .wizard { padding: 14px 14px calc(20px + env(safe-area-inset-bottom)); } .pin-cell { font-size: 1.4rem; } .brand-text { display: none; } }
"""


# ---------------------------------------------------------------------------
# Panel HTML — rendered based on the pinned mode. Local-mode is single-mode
# (no tab switcher); the full tab-switcher UX lives in the relay page.
# ---------------------------------------------------------------------------

_GENERATE_PANEL_HTML = r"""
  <div id="panel-generate" class="tab-panel" role="tabpanel">
    <p class="helper">This phrase IS your account — write it down. You can restore your memories on any agent with it.</p>
    <div class="phrase-grid readonly" id="phrase-grid-generate" role="group" aria-label="Generated 12-word recovery phrase"></div>
    <div class="gen-actions">
      <button type="button" class="btn-ghost" id="gen-copy">
        <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M5 15V5a2 2 0 0 1 2-2h10" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>
        <span id="gen-copy-label">Copy</span>
      </button>
      <button type="button" class="btn-ghost" id="gen-regen">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12a8 8 0 0 1 14-5.3M20 12a8 8 0 0 1-14 5.3M18 3v4h-4M6 21v-4h4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
        Regenerate
      </button>
    </div>
    <label class="ack" for="gen-ack">
      <input type="checkbox" id="gen-ack" />
      <span>I've written this down and stored it somewhere safe.</span>
    </label>
  </div>
"""

_IMPORT_PANEL_HTML = r"""
  <div id="panel-import" class="tab-panel" role="tabpanel">
    <p class="helper">Enter your recovery phrase to restore your memories on this device.</p>
    <div class="phrase-grid" id="phrase-grid-import" role="group" aria-label="12-word recovery phrase input"></div>
    <button type="button" class="btn-ghost" id="paste-all">
      <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="8" y="3" width="8" height="4" rx="1" fill="none" stroke="currentColor" stroke-width="1.6"/><rect x="5" y="6" width="14" height="15" rx="2" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>
      Paste all 12 words
    </button>
  </div>
"""

# ---------------------------------------------------------------------------
# JavaScript — wizard logic + real crypto + real POST. Mirrors the relay's
# pair-html.ts script. Local-mode POSTs directly to ``api_base`` (no
# ``/respond`` suffix — the wire shape differs slightly from the relay).
# ---------------------------------------------------------------------------

_PAIR_SCRIPT = r"""
"use strict";
(function(){
  const SID = __SID_JS__;
  const INITIAL_MODE = __MODE_JS__;
  const API_BASE = __API_BASE_JS__;
  const EXPIRES_AT_MS = __EXPIRES_AT_MS__;
  const HKDF_INFO = "totalreclaw-pair-v2";
  const AEAD_KEY_BYTES = 32;
  const AEAD_NONCE_BYTES = 12;
  const AEAD_TAG_BITS = 128;
  const BIP39 = __BIP39_JS__;
  const DEFAULT_MODE = __DEFAULT_MODE_JS__;

  // UX constants (see rc.13 design decisions in pair-html.ts).
  const PASTE_PIN_AUTOSUBMIT = true;
  const STEP1_SUBHEADING = "Enter the 6-digit code from your chat to continue.";

  const STATE = {
    step: 1,
    pin: ['', '', '', '', '', ''],
    mode: DEFAULT_MODE,
    words: new Array(12).fill(''),
    generated: null,
    ackChecked: false,
    timerId: null,
    submitting: false,
  };

  const $ = (id) => document.getElementById(id);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  function pad2(n) { return n < 10 ? '0' + n : String(n); }

  function b64url(buf) {
    const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
    let s = "";
    for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function b64urlDecode(s) {
    const pad = "=".repeat((4 - (s.length % 4)) % 4);
    const b64 = (s + pad).replace(/-/g, "+").replace(/_/g, "/");
    const raw = atob(b64);
    const out = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }
  function readGatewayPubkeyFromHash() {
    const h = window.location.hash || "";
    const m = /#pk=([A-Za-z0-9_\-]+)/.exec(h);
    if (!m) return null;
    try { return b64urlDecode(m[1]); } catch(_e) { return null; }
  }
  async function deriveKey(gatewayPub, sid) {
    const kp = await crypto.subtle.generateKey({ name: "X25519" }, true, ["deriveBits"]);
    const pkRaw = await crypto.subtle.exportKey("raw", kp.publicKey);
    const pubCrypto = await crypto.subtle.importKey("raw", gatewayPub, { name: "X25519" }, false, []);
    const sharedBits = await crypto.subtle.deriveBits({ name: "X25519", public: pubCrypto }, kp.privateKey, 256);
    const shared = new Uint8Array(sharedBits);
    const baseKey = await crypto.subtle.importKey("raw", shared, "HKDF", false, ["deriveBits"]);
    const keyBits = await crypto.subtle.deriveBits(
      { name: "HKDF", hash: "SHA-256", salt: new TextEncoder().encode(sid), info: new TextEncoder().encode(HKDF_INFO) },
      baseKey, AEAD_KEY_BYTES * 8
    );
    return { kEnc: new Uint8Array(keyBits), pkRaw: new Uint8Array(pkRaw) };
  }
  async function aeadEncrypt(kEnc, sid, plaintext) {
    const key = await crypto.subtle.importKey("raw", kEnc, { name: "AES-GCM" }, false, ["encrypt"]);
    const nonce = crypto.getRandomValues(new Uint8Array(AEAD_NONCE_BYTES));
    const ct = await crypto.subtle.encrypt(
      { name: "AES-GCM", iv: nonce, additionalData: new TextEncoder().encode(sid), tagLength: AEAD_TAG_BITS },
      key, plaintext
    );
    return { nonce, ct: new Uint8Array(ct) };
  }

  function bytesToBits(bytes) {
    const bits = [];
    for (let i = 0; i < bytes.length; i++) {
      const b = bytes[i];
      for (let j = 7; j >= 0; j--) bits.push((b >> j) & 1);
    }
    return bits;
  }
  async function sha256(data) {
    const d = await crypto.subtle.digest("SHA-256", data);
    return new Uint8Array(d);
  }
  async function entropyToMnemonic(entropy) {
    if (!BIP39 || BIP39.length !== 2048) {
      throw new Error("BIP-39 wordlist unavailable on gateway — use import mode or install the 'mnemonic' Python package.");
    }
    const digest = await sha256(entropy);
    const checksumBits = bytesToBits(digest).slice(0, 4);
    const bits = bytesToBits(entropy).concat(checksumBits);
    const words = [];
    for (let i = 0; i < bits.length; i += 11) {
      let v = 0;
      for (let j = 0; j < 11; j++) v = (v << 1) | bits[i + j];
      words.push(BIP39[v]);
    }
    return words;
  }
  async function generateMnemonic12() {
    const entropy = crypto.getRandomValues(new Uint8Array(16));
    try { return await entropyToMnemonic(entropy); }
    finally { entropy.fill(0); }
  }

  function setDots() {
    $$('.step-dots .dot').forEach((d) => {
      const n = Number(d.dataset.step);
      d.classList.toggle('done', n < STATE.step);
      d.classList.toggle('active', n === STATE.step);
    });
    const cur = $('step-current');
    if (cur) cur.textContent = STATE.step <= 3 ? String(STATE.step) : '3';
  }
  function transitionTo(nextId, direction) {
    const all = $$('.screen');
    const current = all.find((s) => s.classList.contains('active'));
    const next = $(nextId);
    if (!current || !next || current === next) return;
    const forward = direction !== 'back';
    next.hidden = false;
    next.classList.remove('active', 'exit-left', 'enter-right');
    next.classList.add(forward ? 'enter-right' : 'exit-left');
    next.offsetWidth;
    current.classList.remove('active');
    current.classList.add(forward ? 'exit-left' : 'enter-right');
    next.classList.remove('enter-right', 'exit-left');
    next.classList.add('active');
    const onDone = () => {
      current.hidden = true;
      current.classList.remove('exit-left', 'enter-right');
      next.removeEventListener('transitionend', onDone);
    };
    next.addEventListener('transitionend', onDone);
    setTimeout(onDone, 400);
  }

  function renderCountdown() {
    const remaining = Math.max(0, EXPIRES_AT_MS - Date.now());
    const m = Math.floor(remaining / 60000);
    const s = Math.floor((remaining % 60000) / 1000);
    const el = $('countdown');
    const txt = $('countdown-text');
    if (!el || !txt) return;
    if (remaining <= 0) {
      txt.textContent = 'Expired';
      el.classList.remove('warn');
      el.classList.add('expired');
      $$('.btn-primary').forEach((b) => (b.disabled = true));
      if (STATE.timerId) { clearInterval(STATE.timerId); STATE.timerId = null; }
      return;
    }
    txt.textContent = pad2(m) + ':' + pad2(s);
    el.classList.toggle('warn', remaining < 60 * 1000);
  }
  function startTimer() {
    renderCountdown();
    STATE.timerId = setInterval(renderCountdown, 1000);
  }

  let toastEl = null;
  let toastTimer = null;
  function showToast(msg, ms) {
    if (!toastEl) {
      toastEl = document.createElement('div');
      toastEl.className = 'toast';
      toastEl.setAttribute('role', 'status');
      document.body.appendChild(toastEl);
    }
    toastEl.textContent = msg;
    toastEl.offsetWidth;
    toastEl.classList.add('show');
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { if (toastEl) toastEl.classList.remove('show'); }, ms || 2400);
  }

  function setError(msg) {
    const el = $('err-banner');
    if (!el) return;
    if (!msg) { el.hidden = true; el.textContent = ''; return; }
    el.hidden = false;
    el.textContent = msg;
  }

  function initPinStep() {
    const cells = $$('.pin-cell');
    const continueBtn = $('pin-continue');
    const errEl = $('pin-error');
    const pasteBtn = $('pin-paste');
    function updateFilledClass() { cells.forEach((c, i) => c.classList.toggle('filled', !!STATE.pin[i])); }
    function updateContinueState() {
      const complete = STATE.pin.every((d) => /^\d$/.test(d));
      continueBtn.disabled = !complete;
    }
    function clearError() { errEl.classList.remove('show'); errEl.textContent = ''; }
    function showError(msg) { errEl.textContent = msg; errEl.classList.add('show'); }
    function distributeDigits(digits, startIdx, autoSubmit) {
      const start = Math.max(0, Math.min(startIdx || 0, cells.length - 1));
      const toWrite = digits.slice(0, cells.length - start).split('');
      toWrite.forEach((d, k) => { const t = cells[start + k]; if (t) { t.value = d; STATE.pin[start + k] = d; } });
      updateFilledClass(); updateContinueState();
      const nextIdx = Math.min(start + toWrite.length, cells.length - 1);
      if (cells[nextIdx]) cells[nextIdx].focus();
      if (autoSubmit && PASTE_PIN_AUTOSUBMIT && STATE.pin.every((d) => /^\d$/.test(d))) {
        setTimeout(() => { if (!continueBtn.disabled) continueBtn.click(); }, 120);
      }
    }
    cells.forEach((cell, i) => {
      cell.addEventListener('input', (e) => {
        const raw = e.target.value.replace(/\D/g, '');
        if (!raw) { STATE.pin[i] = ''; updateFilledClass(); updateContinueState(); return; }
        if (raw.length > 1) { e.target.value = ''; distributeDigits(raw, i, true); clearError(); return; }
        STATE.pin[i] = raw; e.target.value = raw;
        updateFilledClass(); updateContinueState(); clearError();
        if (i < cells.length - 1) cells[i + 1].focus(); else cell.blur();
      });
      cell.addEventListener('keydown', (e) => {
        if (e.key === 'Backspace' && !cell.value && i > 0) {
          e.preventDefault(); cells[i - 1].focus(); cells[i - 1].value = '';
          STATE.pin[i - 1] = ''; updateFilledClass(); updateContinueState();
        } else if (e.key === 'Enter') { if (!continueBtn.disabled) continueBtn.click(); }
      });
      cell.addEventListener('focus', () => cell.select());
      cell.addEventListener('paste', (e) => {
        e.preventDefault();
        const text = (e.clipboardData || window.clipboardData).getData('text') || '';
        const digits = text.replace(/\D/g, '');
        if (!digits) return;
        distributeDigits(digits, i, true); clearError();
      });
    });
    if (pasteBtn) {
      pasteBtn.addEventListener('click', async () => {
        let text = '';
        try { if (navigator.clipboard && navigator.clipboard.readText) text = await navigator.clipboard.readText(); else throw new Error(''); }
        catch (_) { showToast('Paste not allowed — type manually'); return; }
        const digits = (text || '').replace(/\D/g, '').slice(0, 6);
        if (!digits) { showToast('Clipboard has no digits'); return; }
        distributeDigits(digits, 0, true); clearError();
      });
    }
    continueBtn.addEventListener('click', () => {
      const complete = STATE.pin.every((d) => /^\d$/.test(d));
      if (!complete) {
        const firstEmpty = cells.find((c, idx) => !STATE.pin[idx]);
        if (firstEmpty) { firstEmpty.classList.add('shake'); setTimeout(() => firstEmpty.classList.remove('shake'), 420); firstEmpty.focus(); }
        showError('Enter all 6 digits to continue.'); return;
      }
      clearError();
      STATE.step = 2; setDots();
      transitionTo('screen-phrase', 'forward');
    });
    setTimeout(() => cells[0] && cells[0].focus(), 150);
  }

  function buildWordGrid(gridEl, options) {
    gridEl.innerHTML = '';
    const readonly = !!options.readonly;
    for (let i = 0; i < 12; i++) {
      const cell = document.createElement('label'); cell.className = 'word-cell';
      const idx = document.createElement('span'); idx.className = 'word-idx'; idx.textContent = (i + 1) + '.';
      const input = document.createElement('input');
      input.className = 'word-input'; input.type = 'text'; input.autocomplete = 'off';
      input.autocapitalize = 'none'; input.spellcheck = false; input.dataset.index = String(i);
      input.setAttribute('aria-label', 'Word ' + (i + 1));
      if (readonly) { input.readOnly = true; input.tabIndex = -1; } else { input.placeholder = 'word'; }
      cell.appendChild(idx); cell.appendChild(input); gridEl.appendChild(cell);
    }
  }

  function wireImportGrid() {
    const grid = $('phrase-grid-import'); if (!grid) return;
    const inputs = $$('.word-input', grid);
    function updatePairState() {
      const complete = STATE.words.every((w) => w.trim().length > 0);
      $('phrase-pair').disabled = !complete || STATE.submitting;
    }
    function setWord(i, val) {
      const trimmed = val.trim().toLowerCase();
      STATE.words[i] = trimmed;
      inputs[i].parentElement.classList.toggle('filled', trimmed.length > 0);
      updatePairState();
    }
    inputs.forEach((input, i) => {
      input.addEventListener('input', (e) => setWord(i, e.target.value));
      input.addEventListener('keydown', (e) => {
        if (e.key === ' ' || e.key === 'Enter' || e.key === 'Tab') {
          if (e.key !== 'Tab') e.preventDefault();
          if (inputs[i + 1]) inputs[i + 1].focus(); else input.blur();
        } else if (e.key === 'Backspace' && !input.value && i > 0) { e.preventDefault(); inputs[i - 1].focus(); }
      });
      input.addEventListener('paste', (e) => {
        const text = (e.clipboardData || window.clipboardData).getData('text') || '';
        const words = text.trim().split(/\s+/).filter(Boolean);
        if (words.length < 2) return;
        e.preventDefault();
        const toInsert = words.slice(0, 12 - i);
        toInsert.forEach((w, k) => { const idx = i + k; inputs[idx].value = w.toLowerCase(); setWord(idx, w); });
        const next = Math.min(i + toInsert.length, inputs.length - 1);
        inputs[next].focus();
      });
    });
    const pasteAll = $('paste-all');
    if (pasteAll) {
      pasteAll.addEventListener('click', async () => {
        let text = '';
        if (navigator.clipboard && navigator.clipboard.readText) {
          try { text = await navigator.clipboard.readText(); } catch (_) {}
        }
        if (!text) { showToast('Clipboard blocked — type manually'); return; }
        const words = text.trim().split(/\s+/).slice(0, 12);
        words.forEach((w, k) => { inputs[k].value = w.toLowerCase(); setWord(k, w); });
        inputs[Math.min(words.length, 11)].focus();
      });
    }
  }

  function wireGenerateGrid() {
    const grid = $('phrase-grid-generate'); if (!grid) return;
    const inputs = $$('.word-input', grid);
    function fill(words) {
      STATE.generated = words.slice();
      words.forEach((w, i) => { inputs[i].value = w; inputs[i].parentElement.classList.add('filled'); });
    }
    async function generateAndFill() {
      try { const words = await generateMnemonic12(); fill(words); }
      catch (err) { setError('Could not generate phrase: ' + (err && err.message ? err.message : String(err))); }
    }
    generateAndFill();
    function updatePairState() {
      const ready = STATE.generated && STATE.generated.length === 12 && STATE.ackChecked;
      $('phrase-pair').disabled = !ready || STATE.submitting;
    }
    $('gen-ack').addEventListener('change', (e) => { STATE.ackChecked = !!e.target.checked; updatePairState(); });
    $('gen-copy').addEventListener('click', async () => {
      const label = $('gen-copy-label');
      if (!STATE.generated) return;
      try { await navigator.clipboard.writeText(STATE.generated.join(' ')); label.textContent = 'Copied'; setTimeout(() => { label.textContent = 'Copy'; }, 1600); }
      catch (_) { label.textContent = 'Copy blocked'; setTimeout(() => { label.textContent = 'Copy'; }, 1600); }
    });
    $('gen-regen').addEventListener('click', async () => {
      if (STATE.generated) { for (let i = 0; i < STATE.generated.length; i++) STATE.generated[i] = ''; STATE.generated = null; }
      STATE.ackChecked = false; $('gen-ack').checked = false;
      updatePairState(); await generateAndFill();
    });
  }

  async function submitPhrase(phrase) {
    STATE.submitting = true;
    const btn = $('phrase-pair'); btn.disabled = true; btn.classList.add('loading');
    setError('');
    let phase = 'init';
    try {
      const gw = readGatewayPubkeyFromHash();
      if (!gw) { setError('Gateway key missing from URL. Ask for a fresh pair URL.'); return; }
      phase = 'derive';
      const { kEnc, pkRaw } = await deriveKey(gw, SID);
      phase = 'encrypt';
      const plaintext = new TextEncoder().encode(phrase);
      const { nonce, ct } = await aeadEncrypt(kEnc, SID, plaintext);
      plaintext.fill(0);
      if (STATE.generated) { for (let i = 0; i < STATE.generated.length; i++) STATE.generated[i] = ''; STATE.generated = null; }
      STATE.words = STATE.words.map(() => '');
      $$('.word-input').forEach((el) => { if (!el.readOnly) el.value = ''; });

      phase = 'post';
      // Local-mode wire shape — matches http_server.py's POST handler:
      // { v:1, sid, pk_d, pin, nonce, ct }
      const body = JSON.stringify({
        v: 1, sid: SID,
        pk_d: b64url(pkRaw),
        pin: STATE.pin.join(''),
        nonce: b64url(nonce),
        ct: b64url(ct),
      });
      STATE.pin = ['', '', '', '', '', ''];
      const res = await fetch(API_BASE, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body, cache: 'no-store',
      });
      if (res.status === 204 || res.status === 200) {
        STATE.step = 3; setDots();
        transitionTo('screen-done', 'forward');
        if (STATE.timerId) { clearInterval(STATE.timerId); STATE.timerId = null; }
        const cd = $('countdown'); if (cd) cd.style.visibility = 'hidden';
        return;
      }
      if (res.status === 403) { setError('PIN mismatch or too many failed attempts. Ask the agent for a fresh URL.'); return; }
      if (res.status === 410) { setError('Session expired. Ask the agent for a fresh URL.'); return; }
      let detail = '';
      try { detail = await res.text(); } catch (_) {}
      setError('Pairing failed (HTTP ' + res.status + '): ' + (detail || 'unknown error'));
    } catch (err) {
      const label = phase === 'derive' ? 'Key derivation failed' :
                    phase === 'encrypt' ? 'Encryption failed' :
                    phase === 'post' ? 'Submit failed' : 'Pair failed';
      setError(label + ': ' + (err && err.message ? err.message : String(err)));
    } finally {
      STATE.submitting = false;
      btn.classList.remove('loading');
      if (STATE.step !== 3) btn.disabled = false;
    }
  }

  function initPhraseStep() {
    if ($('phrase-grid-import')) { buildWordGrid($('phrase-grid-import'), { readonly: false }); wireImportGrid(); }
    if ($('phrase-grid-generate')) { buildWordGrid($('phrase-grid-generate'), { readonly: true }); wireGenerateGrid(); }
    $('back-to-pin').addEventListener('click', () => { STATE.step = 1; setDots(); transitionTo('screen-pin', 'back'); });
    $('phrase-pair').addEventListener('click', async () => {
      const btn = $('phrase-pair');
      if (btn.disabled || STATE.submitting) return;
      if (STATE.mode === 'generate') {
        if (!STATE.generated || STATE.generated.length !== 12) { setError('No phrase to submit. Tap Regenerate.'); return; }
        if (!STATE.ackChecked) { setError('Confirm you have written down the phrase before continuing.'); return; }
        await submitPhrase(STATE.generated.join(' '));
      } else {
        if (!STATE.words.every((w) => w.trim().length > 0)) { setError('Enter all 12 words to continue.'); return; }
        await submitPhrase(STATE.words.join(' '));
      }
    });
  }

  function initDoneStep() {
    const closeLink = $('close-link');
    if (closeLink) closeLink.addEventListener('click', (e) => { e.preventDefault(); try { window.close(); } catch (_) {} });
  }

  async function probeCrypto() {
    const ok = crypto && crypto.subtle && typeof crypto.subtle.generateKey === "function";
    if (!ok) throw new Error("no webcrypto");
    await crypto.subtle.generateKey({ name: "X25519" }, true, ["deriveBits"]);
    const rawKey = new Uint8Array(AEAD_KEY_BYTES);
    const aesKey = await crypto.subtle.importKey("raw", rawKey, { name: "AES-GCM" }, false, ["encrypt"]);
    const probeNonce = new Uint8Array(AEAD_NONCE_BYTES);
    await crypto.subtle.encrypt({ name: "AES-GCM", iv: probeNonce, additionalData: new Uint8Array(0), tagLength: AEAD_TAG_BITS }, aesKey, new Uint8Array(0));
    await crypto.subtle.importKey("raw", rawKey, "HKDF", false, ["deriveBits"]);
  }

  document.addEventListener('DOMContentLoaded', async () => {
    const sub = document.getElementById('pin-subheading');
    if (sub) sub.textContent = STEP1_SUBHEADING;
    setDots(); startTimer();
    try { await probeCrypto(); }
    catch (err) {
      setError('This browser lacks x25519 + AES-GCM + HKDF support required to pair securely. Use an up-to-date Safari 17.2+ or Chromium 133+ browser. (' + (err && err.message ? err.message : String(err)) + ')');
      $$('.btn-primary').forEach((b) => (b.disabled = true));
      return;
    }
    initPinStep(); initPhraseStep(); initDoneStep();
  });
})();
"""


# ---------------------------------------------------------------------------
# HTML template — single-mode (local-mode doesn't show the tab switcher).
# The PANELS placeholder is replaced with either GENERATE or IMPORT panel.
# ---------------------------------------------------------------------------

_PAIR_PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
<meta name="referrer" content="no-referrer" />
<meta name="robots" content="noindex, nofollow" />
<meta name="theme-color" content="#0B0B1A" />
<title>Set up TotalReclaw</title>
<style>__STYLE__</style>
</head>
<body>
<a href="#main" class="skip-link">Skip to content</a>
<main id="main" class="wizard" aria-live="polite">
  <header class="topbar">
    <a class="brand" href="#" aria-label="TotalReclaw">
      <svg class="brand-mark" viewBox="0 0 256 256" aria-hidden="true" focusable="false">
        <defs>
          <linearGradient id="shieldGrad" x1="30%" y1="0%" x2="70%" y2="100%">
            <stop offset="0%" stop-color="#5040B0"/>
            <stop offset="100%" stop-color="#7B5CFF"/>
          </linearGradient>
        </defs>
        <path d="M128 18 L212 58 V162 C212 210 174 236 128 250 C82 236 44 210 44 162 V58 Z"
              fill="rgba(75,55,170,0.05)" stroke="url(#shieldGrad)" stroke-width="5" stroke-linejoin="round"/>
        <path d="M128 40 L196 74 V160 C196 200 164 222 128 234 C92 222 60 200 60 160 V74 Z"
              fill="none" stroke="url(#shieldGrad)" stroke-width="2.5" stroke-linejoin="round" opacity="0.45"/>
        <path d="M 116 186 C 96 162, 60 124, 72 92 C 76 78, 94 70, 108 82 C 118 90, 114 108, 100 116"
              fill="none" stroke="url(#shieldGrad)" stroke-width="9" stroke-linecap="round"/>
        <path d="M 140 186 C 160 162, 196 124, 184 92 C 180 78, 162 70, 148 82 C 138 90, 142 108, 156 116"
              fill="none" stroke="url(#shieldGrad)" stroke-width="9" stroke-linecap="round"/>
      </svg>
      <span class="brand-text">TotalReclaw</span>
    </a>
    <div class="meta">
      <div class="step-meter" aria-label="Progress">
        <span class="step-label"><span id="step-current">1</span> / 3</span>
        <div class="step-dots">
          <span class="dot" data-step="1" aria-hidden="true"></span>
          <span class="dot" data-step="2" aria-hidden="true"></span>
          <span class="dot" data-step="3" aria-hidden="true"></span>
        </div>
      </div>
      <div class="countdown" id="countdown">
        <svg class="clock" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="13" r="8" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M12 9v4l2.5 2" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><path d="M9 3h6M12 3v2" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
        <span id="countdown-text">5:00</span>
      </div>
    </div>
  </header>

  <section class="stage" id="stage">
    <article class="screen active" id="screen-pin" data-step="1">
      <div class="screen-inner">
        <p class="eyebrow">Step 1 of 3</p>
        <h1 class="heading">Enter your PIN</h1>
        <p id="pin-subheading" class="subheading">Enter the 6-digit code from your chat to continue.</p>
        <div class="pin-wrap">
          <div class="pin-cells" role="group" aria-label="6-digit PIN">
            <input class="pin-cell" type="tel" inputmode="numeric" pattern="[0-9]*" maxlength="1" autocomplete="one-time-code" aria-label="Digit 1" data-index="0" />
            <input class="pin-cell" type="tel" inputmode="numeric" pattern="[0-9]*" maxlength="1" autocomplete="off" aria-label="Digit 2" data-index="1" />
            <input class="pin-cell" type="tel" inputmode="numeric" pattern="[0-9]*" maxlength="1" autocomplete="off" aria-label="Digit 3" data-index="2" />
            <span class="pin-sep" aria-hidden="true"></span>
            <input class="pin-cell" type="tel" inputmode="numeric" pattern="[0-9]*" maxlength="1" autocomplete="off" aria-label="Digit 4" data-index="3" />
            <input class="pin-cell" type="tel" inputmode="numeric" pattern="[0-9]*" maxlength="1" autocomplete="off" aria-label="Digit 5" data-index="4" />
            <input class="pin-cell" type="tel" inputmode="numeric" pattern="[0-9]*" maxlength="1" autocomplete="off" aria-label="Digit 6" data-index="5" />
          </div>
          <div class="pin-actions">
            <button type="button" class="btn-ghost btn-ghost-inline" id="pin-paste" aria-label="Paste 6-digit PIN from clipboard">
              <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="8" y="3" width="8" height="4" rx="1" fill="none" stroke="currentColor" stroke-width="1.6"/><rect x="5" y="6" width="14" height="15" rx="2" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>
              Paste
            </button>
          </div>
          <p class="pin-error" id="pin-error" role="alert"></p>
        </div>
      </div>
      <footer class="screen-cta">
        <button type="button" class="btn-primary" id="pin-continue" disabled>Continue</button>
        <p class="security-note">
          <svg class="lock" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 10V7a5 5 0 1 1 10 0v3" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><rect x="5" y="10" width="14" height="10" rx="2" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>
          This flow is the only secure way to set up your recovery phrase without the LLM provider ever seeing it.
        </p>
      </footer>
    </article>

    <article class="screen" id="screen-phrase" data-step="2" hidden>
      <div class="screen-inner">
        <p class="eyebrow">Step 2 of 3</p>
        <h1 class="heading">Your recovery phrase</h1>
__PANELS__
        <p class="security-note">
          <svg class="lock" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 10V7a5 5 0 1 1 10 0v3" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><rect x="5" y="10" width="14" height="10" rx="2" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>
          Never share this phrase. Encrypted in your browser before transmission.
        </p>
        <div class="err-banner" id="err-banner" hidden role="alert"></div>
      </div>
      <footer class="screen-cta">
        <div class="cta-row">
          <button type="button" class="btn-secondary" id="back-to-pin" aria-label="Back to PIN">
            <svg viewBox="0 0 24 24"><path d="M15 6l-6 6 6 6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </button>
          <button type="button" class="btn-primary" id="phrase-pair" disabled>
            <span class="btn-label">Seal key and finish</span>
            <span class="btn-spinner" aria-hidden="true"></span>
          </button>
        </div>
      </footer>
    </article>

    <article class="screen" id="screen-done" data-step="3" hidden>
      <div class="screen-inner center">
        <div class="check-wrap" aria-hidden="true">
          <svg class="check" viewBox="0 0 100 100">
            <circle class="check-ring" cx="50" cy="50" r="44" fill="none" stroke-width="3"/>
            <path class="check-tick" d="M30 52 L45 67 L72 36" fill="none" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </div>
        <h1 class="heading">You're all set</h1>
        <p class="subheading">TotalReclaw account created. Go back to your chat and try: <em>"remember I prefer Python over Go"</em></p>
        <a href="#" class="close-link" id="close-link">Close this page</a>
      </div>
    </article>
  </section>
  <p style="text-align:center;margin-top:28px;font-size:.72rem;color:var(--fg-faint);letter-spacing:.08em;opacity:.6">Session: __SID_HTML__</p>
</main>
<script>__SCRIPT__</script>
</body>
</html>
"""
