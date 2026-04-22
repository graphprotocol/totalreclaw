"""pair.pair_page — self-contained HTML + inline JS + CSS for the browser
side of the QR-pair handshake.

Python parity of ``skill/plugin/pair-page.ts``. The browser loads this
page at ``GET /pair/<token>``, reads the gateway's ephemeral pubkey from
the URL fragment (``#pk=<b64url>``), does x25519 ECDH + HKDF +
ChaCha20-Poly1305 encrypt against the user's typed / generated phrase,
and POSTs the ciphertext to ``POST /pair/<token>``.

UI mirrors the TS page's UX:

- Brand-consistent copy ("your TotalReclaw account key", "Use it ONLY
  with TotalReclaw", etc).
- Consequence blocks ("With it you can" / "Without it").
- Two modes: ``generate`` (browser generates BIP-39) and ``import`` (user
  pastes).
- PIN entry (6-digit) + acknowledge-gate BEFORE reveal.

Security properties:

- Entirely self-contained (no CDN, no external fetch). Inline CSS + JS.
- No ``console.log`` of phrase / PIN / key material.
- Memory scrubbed after submit (best-effort — browser GC is not
  guaranteed).
- ``cache-control: no-store`` + strict CSP on the response (set by the
  HTTP server, not this module).

BIP-39 wordlist: kept out of this module to reduce the Python wheel's
size; on ``generate`` mode the page uses the browser's ``crypto.getRandomValues``
+ a compact 2048-word list compiled from the ``mnemonic`` package at
build time. See :func:`_bip39_words` for the lazy loader.

ML-KEM hybrid port: NOT in rc.4. Both TS and Python pages use pure
x25519 + ChaCha20-Poly1305. Hybrid KEM lands in rc.5 in lockstep across
both stacks.
"""
from __future__ import annotations

import html
import json
from functools import lru_cache
from typing import List


@lru_cache(maxsize=1)
def _bip39_words() -> List[str]:
    """Load the BIP-39 English wordlist from the ``mnemonic`` package if
    available; otherwise return an empty list and let the browser skip
    ``generate`` mode (``import`` still works).

    The ``mnemonic`` package is a ``dev`` extra, not a core dep — we
    don't want to pull it into every ``pip install totalreclaw``. If a
    user runs the pair flow without it, they can still use ``import``
    mode to paste an existing phrase; ``generate`` mode falls back to
    a "please install ``mnemonic`` to generate" error page.
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
    """JSON-encode a string for safe ``<script>`` embedding.

    ``json.dumps`` produces a valid JS string literal. We still escape
    ``<``, ``>``, ``&``, and U+2028/U+2029 because ``</script>`` in the
    payload would close the script block.
    """
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
    """Render the self-contained pair HTML page.

    ``api_base`` is the prefix that the pair respond endpoint lives under
    (e.g. ``/pair/<token>``). The browser POSTs the encrypted payload to
    the same URL it was served from, so the page embeds ``api_base``
    verbatim.
    """
    if mode not in ("generate", "import"):
        raise ValueError(f"pair.pair_page: invalid mode '{mode}'")

    sid_js = _escape_js_string(sid)
    mode_js = _escape_js_string(mode)
    api_base_js = _escape_js_string(api_base)
    bip39 = _bip39_words()
    bip39_js = json.dumps(bip39)
    expires_safe = int(expires_at_ms)
    now_safe = int(now_ms)

    sid_html = html.escape(sid)
    mode_html = html.escape(mode)

    # The HTML template is big; we compose it in pieces to keep line
    # length reasonable.
    script = _PAIR_PAGE_SCRIPT.format(
        sid_js=sid_js,
        mode_js=mode_js,
        api_base_js=api_base_js,
        expires_at_ms=expires_safe,
        now_ms=now_safe,
        bip39_js=bip39_js,
    )

    return _PAIR_PAGE_HTML.format(
        sid_html=sid_html,
        mode_html=mode_html,
        style=_PAIR_PAGE_STYLE,
        script=script,
    )


# ---------------------------------------------------------------------------
# HTML / CSS / JS templates
# ---------------------------------------------------------------------------

_PAIR_PAGE_STYLE = """
  :root {
    --bg: #0b0d12;
    --fg: #f4f4f7;
    --muted: #9aa2b4;
    --accent: #7c66ff;
    --warn: #ffb84d;
    --danger: #ff5a6a;
    --card: #14181f;
    --border: #242936;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0;
    background: var(--bg); color: var(--fg);
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    font-size: 16px; line-height: 1.5;
    min-height: 100vh;
  }
  .page {
    max-width: 640px; margin: 0 auto;
    padding: 24px 20px 40px;
  }
  h1 { font-size: 1.6rem; margin: 0 0 8px; letter-spacing: -0.01em; }
  h2 { font-size: 1.15rem; margin: 24px 0 8px; }
  p { margin: 0 0 12px; }
  .muted { color: var(--muted); }
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 20px;
    margin: 16px 0;
  }
  .warn-card { border-color: var(--warn); }
  .danger-card { border-color: var(--danger); }
  button {
    background: var(--accent); color: #fff; border: 0;
    font-size: 1rem; font-weight: 600;
    padding: 14px 20px; border-radius: 10px;
    cursor: pointer; width: 100%;
  }
  button:disabled { opacity: 0.4; cursor: not-allowed; }
  .secondary-button {
    background: transparent; color: var(--accent);
    border: 1px solid var(--accent);
  }
  label { display: block; font-weight: 600; margin: 12px 0 6px; }
  input[type="text"], input[type="password"], textarea {
    width: 100%; padding: 12px 14px;
    background: var(--bg); color: var(--fg);
    border: 1px solid var(--border); border-radius: 8px;
    font-size: 1rem; font-family: inherit;
  }
  textarea { min-height: 120px; resize: vertical; font-family: ui-monospace, Menlo, Consolas, monospace; }
  .pin-input { letter-spacing: 0.3em; text-align: center; font-size: 1.4rem; font-family: ui-monospace, Menlo, monospace; }
  .phrase-box {
    background: #000; color: #a6d0ff;
    border: 1px solid var(--border); border-radius: 10px;
    padding: 16px; font-family: ui-monospace, Menlo, Consolas, monospace;
    font-size: 1.05rem; line-height: 1.7;
    word-spacing: 0.15em;
    user-select: text;
  }
  ol { padding-left: 24px; }
  ol li { margin: 6px 0; }
  .consequence { display: flex; gap: 16px; margin: 12px 0; }
  .consequence > div { flex: 1; padding: 14px; border-radius: 10px; border: 1px solid var(--border); }
  .hidden { display: none; }
  #status { margin: 20px 0; padding: 14px; border-radius: 10px; font-weight: 600; text-align: center; }
  #status.info { background: rgba(124,102,255,0.12); color: var(--accent); }
  #status.ok { background: rgba(69,209,120,0.12); color: #45d178; }
  #status.err { background: rgba(255,90,106,0.12); color: var(--danger); }
  .countdown { font-variant-numeric: tabular-nums; color: var(--muted); font-size: 0.9rem; }
"""


_PAIR_PAGE_SCRIPT = r"""
  "use strict";
  (function() {{
    const SID = {sid_js};
    const MODE = {mode_js};
    const API_BASE = {api_base_js};
    const EXPIRES_AT_MS = {expires_at_ms};
    const NOW_MS = {now_ms};
    const BIP39 = {bip39_js};

    const HKDF_INFO = "totalreclaw-pair-v1";
    const AEAD_KEY_BYTES = 32;
    const AEAD_NONCE_BYTES = 12;

    // ---- Base64url helpers (matching Node / Python side) ----
    function b64url(buf) {{
      const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
      let s = "";
      for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
      return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    }}
    function b64urlDecode(s) {{
      const pad = "=".repeat((4 - (s.length % 4)) % 4);
      const b64 = (s + pad).replace(/-/g, "+").replace(/_/g, "/");
      const raw = atob(b64);
      const out = new Uint8Array(raw.length);
      for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
      return out;
    }}

    // ---- URL fragment → gateway pubkey ----
    function readGatewayPubkeyFromHash() {{
      const h = window.location.hash || "";
      const m = /#pk=([A-Za-z0-9_\-]+)/.exec(h);
      if (!m) return null;
      try {{ return b64urlDecode(m[1]); }} catch (_e) {{ return null; }}
    }}

    // ---- Web Crypto: x25519 ECDH + HKDF-SHA256 + ChaCha20-Poly1305 ----
    // WebCrypto's x25519 + ChaCha20-Poly1305 support lands in Safari 17.2
    // and Chromium 118. Older browsers fall back to a fail-closed error
    // page ("update your browser to pair securely"). No polyfill ship.
    async function deriveKey(gatewayPub, sid) {{
      // Generate an ephemeral x25519 keypair for the device side.
      const kp = await crypto.subtle.generateKey({{ name: "X25519" }}, true, ["deriveBits"]);
      const pkRaw = await crypto.subtle.exportKey("raw", kp.publicKey);

      const pubCrypto = await crypto.subtle.importKey(
        "raw", gatewayPub, {{ name: "X25519" }}, false, []
      );
      const sharedBits = await crypto.subtle.deriveBits(
        {{ name: "X25519", public: pubCrypto }}, kp.privateKey, 256
      );
      const shared = new Uint8Array(sharedBits);

      // HKDF: extract with salt=sid, expand with info="totalreclaw-pair-v1".
      const baseKey = await crypto.subtle.importKey(
        "raw", shared, "HKDF", false, ["deriveBits"]
      );
      const keyBits = await crypto.subtle.deriveBits(
        {{
          name: "HKDF",
          hash: "SHA-256",
          salt: new TextEncoder().encode(sid),
          info: new TextEncoder().encode(HKDF_INFO),
        }},
        baseKey,
        AEAD_KEY_BYTES * 8
      );
      return {{ kEnc: new Uint8Array(keyBits), pkRaw: new Uint8Array(pkRaw) }};
    }}

    async function aeadEncrypt(kEnc, sid, plaintext) {{
      const key = await crypto.subtle.importKey(
        "raw", kEnc, {{ name: "ChaCha20-Poly1305" }}, false, ["encrypt"]
      );
      const nonce = crypto.getRandomValues(new Uint8Array(AEAD_NONCE_BYTES));
      const ct = await crypto.subtle.encrypt(
        {{ name: "ChaCha20-Poly1305", iv: nonce, additionalData: new TextEncoder().encode(sid) }},
        key,
        plaintext
      );
      return {{ nonce, ct: new Uint8Array(ct) }};
    }}

    // ---- BIP-39 generate (browser side) ----
    function bytesToBits(bytes) {{
      const bits = [];
      for (let i = 0; i < bytes.length; i++) {{
        const b = bytes[i];
        for (let j = 7; j >= 0; j--) bits.push((b >> j) & 1);
      }}
      return bits;
    }}
    async function sha256(data) {{
      const d = await crypto.subtle.digest("SHA-256", data);
      return new Uint8Array(d);
    }}
    async function generateMnemonic12() {{
      if (!BIP39 || BIP39.length !== 2048) {{
        throw new Error("BIP-39 wordlist unavailable. Use 'import' mode or install the 'mnemonic' Python package on the gateway.");
      }}
      const entropy = crypto.getRandomValues(new Uint8Array(16));
      const digest = await sha256(entropy);
      const checksumBits = bytesToBits(digest).slice(0, 4);
      const bits = bytesToBits(entropy).concat(checksumBits);
      const words = [];
      for (let i = 0; i < bits.length; i += 11) {{
        let v = 0;
        for (let j = 0; j < 11; j++) v = (v << 1) | bits[i + j];
        words.push(BIP39[v]);
      }}
      return words.join(" ");
    }}

    // ---- DOM helpers ----
    const $ = (id) => document.getElementById(id);
    function setStatus(text, kind) {{
      const el = $("status");
      el.textContent = text;
      el.className = kind || "info";
      el.classList.remove("hidden");
    }}
    function hide(id) {{ $(id).classList.add("hidden"); }}
    function show(id) {{ $(id).classList.remove("hidden"); }}

    // ---- Countdown ----
    function renderCountdown() {{
      const remaining = Math.max(0, EXPIRES_AT_MS - Date.now());
      const m = Math.floor(remaining / 60000);
      const s = Math.floor((remaining % 60000) / 1000);
      $("countdown").textContent = remaining > 0
        ? `Expires in ${{m}}:${{String(s).padStart(2,"0")}}`
        : "Session expired. Ask the agent for a fresh URL.";
      if (remaining <= 0) {{
        document.querySelectorAll("button").forEach(b => b.disabled = true);
      }}
    }}
    setInterval(renderCountdown, 1000); renderCountdown();

    // ---- PIN gate → unlock phrase flow ----
    $("pin-submit").addEventListener("click", () => {{
      const pin = ($("pin").value || "").trim();
      if (!/^\d{{6}}$/.test(pin)) {{
        setStatus("Enter the 6-digit PIN the agent showed you.", "err");
        return;
      }}
      // Store the PIN locally; we'll send it with the encrypted payload.
      window.__PAIR_PIN = pin;
      hide("pin-step");
      if (MODE === "generate") show("gen-step"); else show("import-step");
      setStatus("PIN accepted. Continue below.", "ok");
    }});

    // ---- Generate flow ----
    let generatedPhrase = null;
    $("gen-button").addEventListener("click", async () => {{
      try {{
        generatedPhrase = await generateMnemonic12();
        $("phrase-display").textContent = generatedPhrase;
        show("phrase-display-box");
        hide("gen-button");
        show("gen-ack-step");
      }} catch (err) {{
        setStatus("Could not generate phrase: " + err.message, "err");
      }}
    }});
    $("gen-confirm").addEventListener("click", async () => {{
      if (!$("gen-ack").checked) {{
        setStatus("Check the acknowledgment before continuing.", "err");
        return;
      }}
      if (!generatedPhrase) {{
        setStatus("No phrase to submit. Retry generate.", "err");
        return;
      }}
      await submitPhrase(generatedPhrase);
    }});

    // ---- Import flow ----
    $("import-submit").addEventListener("click", async () => {{
      const raw = ($("import-phrase").value || "").trim().toLowerCase().replace(/\s+/g, " ");
      if (!raw) {{
        setStatus("Paste your 12 or 24-word phrase.", "err");
        return;
      }}
      const n = raw.split(" ").length;
      if (n !== 12 && n !== 24) {{
        setStatus("Phrase must be exactly 12 or 24 words.", "err");
        return;
      }}
      await submitPhrase(raw);
    }});

    // ---- Submit: encrypt + POST ----
    async function submitPhrase(phrase) {{
      try {{
        const gw = readGatewayPubkeyFromHash();
        if (!gw) {{
          setStatus("Gateway key missing from URL. Ask for a fresh pair URL.", "err");
          return;
        }}
        setStatus("Encrypting phrase end-to-end…", "info");
        const {{ kEnc, pkRaw }} = await deriveKey(gw, SID);
        const plaintext = new TextEncoder().encode(phrase);
        const {{ nonce, ct }} = await aeadEncrypt(kEnc, SID, plaintext);

        // Zero the plaintext buffer — best-effort (browser GC is not
        // guaranteed; but we at least drop our reference).
        plaintext.fill(0);

        const body = JSON.stringify({{
          v: 1,
          sid: SID,
          pk_d: b64url(pkRaw),
          pin: window.__PAIR_PIN,
          nonce: b64url(nonce),
          ct: b64url(ct),
        }});
        window.__PAIR_PIN = null; // drop reference
        const res = await fetch(API_BASE, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body,
          cache: "no-store",
        }});
        if (res.status === 204 || res.status === 200) {{
          hide("gen-step"); hide("import-step"); hide("gen-ack-step");
          show("done-step");
          setStatus("Pairing complete. Tell the agent you're done; it will confirm on its side.", "ok");
          return;
        }}
        if (res.status === 403) {{
          setStatus("PIN mismatch or too many failed attempts. Ask the agent for a fresh URL.", "err");
          return;
        }}
        if (res.status === 410) {{
          setStatus("Session expired. Ask the agent for a fresh URL.", "err");
          return;
        }}
        const detail = await res.text();
        setStatus(`Pairing failed (${{res.status}}): ${{detail || "unknown error"}}`, "err");
      }} catch (err) {{
        setStatus("Encryption failed: " + (err && err.message ? err.message : String(err)), "err");
      }}
    }}

    // Capability gate: refuse to run on browsers without x25519 + ChaCha.
    (async () => {{
      try {{
        const ok = crypto && crypto.subtle && typeof crypto.subtle.generateKey === "function";
        if (!ok) throw new Error("no webcrypto");
        // Probe X25519 support without actually using a key.
        await crypto.subtle.generateKey({{ name: "X25519" }}, true, ["deriveBits"]);
      }} catch (err) {{
        setStatus("This browser lacks x25519 + ChaCha20-Poly1305 support. Please use an up-to-date Safari (17.2+) or Chromium (118+) browser to pair securely.", "err");
        document.querySelectorAll("button").forEach(b => b.disabled = true);
      }}
    }})();
  }})();
"""


_PAIR_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
<meta name="referrer" content="no-referrer" />
<meta name="robots" content="noindex, nofollow" />
<title>TotalReclaw Pair</title>
<style>{style}</style>
</head>
<body>
<main class="page">
  <h1>TotalReclaw pairing</h1>
  <p class="muted">Mode: <strong>{mode_html}</strong>. Session: <code>{sid_html}</code>.</p>
  <p class="countdown" id="countdown">Loading…</p>

  <div id="status" class="info hidden"></div>

  <section class="card" id="pin-step">
    <h2>Step 1. Verify PIN</h2>
    <p>The TotalReclaw agent showed you a 6-digit PIN. Type it here so the gateway knows you're the right browser.</p>
    <label for="pin">6-digit PIN</label>
    <input id="pin" class="pin-input" type="text" inputmode="numeric" pattern="\\d{{6}}" maxlength="6" autocomplete="off" />
    <p></p>
    <button id="pin-submit">Verify</button>
  </section>

  <section class="card hidden" id="gen-step">
    <h2>Step 2. Generate your TotalReclaw account key</h2>
    <p>This is your <strong>one and only</strong> key to your encrypted memory vault. Treat it like a master password — it cannot be reset if lost.</p>
    <div class="consequence">
      <div><strong>With it you can:</strong> recover access from any device, export your memories, switch agents.</div>
      <div><strong>Without it:</strong> your memories are permanently unrecoverable. There is no "forgot password".</div>
    </div>
    <p class="muted">Store it in a password manager, encrypted notes, or on paper in a safe. <strong>Never</strong> share it with anyone. <strong>Use it ONLY with TotalReclaw</strong> — never paste it into other apps, not even ones that ask nicely.</p>
    <button id="gen-button">Generate now</button>
    <div id="phrase-display-box" class="hidden">
      <label>Your account key (write this down)</label>
      <div class="phrase-box" id="phrase-display"></div>
    </div>
  </section>

  <section class="card hidden warn-card" id="gen-ack-step">
    <h2>Step 3. Acknowledge</h2>
    <p>Before we seal the key into your TotalReclaw account on this gateway, confirm:</p>
    <label><input type="checkbox" id="gen-ack" /> I have written down or securely stored the 12 words above. I understand losing them means losing access to my memories forever.</label>
    <p></p>
    <button id="gen-confirm">Seal key and finish</button>
  </section>

  <section class="card hidden" id="import-step">
    <h2>Step 2. Paste your existing TotalReclaw account key</h2>
    <p>Paste your 12 or 24-word phrase below. The phrase is encrypted in this browser tab before leaving — it never crosses the LLM context.</p>
    <label for="import-phrase">Your 12 or 24-word phrase</label>
    <textarea id="import-phrase" autocomplete="off" spellcheck="false"></textarea>
    <button id="import-submit">Seal key and finish</button>
  </section>

  <section class="card hidden" id="done-step">
    <h2>Pairing complete</h2>
    <p>Your key is sealed into this TotalReclaw gateway. You can close this tab and go back to the agent. If the agent asks, tell it "pairing complete" — it will verify and restart the memory plugin if needed.</p>
  </section>
</main>
<script>{script}</script>
</body>
</html>
"""
