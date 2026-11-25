"""Strict-CSP browser walk for the local (Hermes) pair page.

Starts the REAL embedded pair HTTP server (so the response headers are the
real ones), loads the page in headless Chromium and walks the whole offline
flow, asserting zero ``securitypolicyviolation`` events and zero CSP/Trusted
Types console errors. The ciphertext POST is intercepted in the browser and
never reaches the server; nothing pairs. Dummy words only.

Skipped when playwright or a Chromium build is not available. Set
``PLAYWRIGHT_CHROMIUM_EXECUTABLE`` to point at a Chromium binary.
"""
from __future__ import annotations

import glob
import os
from pathlib import Path

import pytest

if os.environ.get("CI"):
    # In CI the browser walk must run, never skip.
    import playwright.sync_api as playwright_sync  # noqa: E402
else:
    playwright_sync = pytest.importorskip("playwright.sync_api")

from totalreclaw.pair.crypto import generate_gateway_keypair  # noqa: E402
from totalreclaw.pair.http_server import (  # noqa: E402
    CompletePairingResult,
    PairHttpConfig,
    build_pair_http_server,
)
from totalreclaw.pair.session_store import (  # noqa: E402
    create_pair_session,
    default_pair_sessions_path,
)


def _chromium() -> str | None:
    env = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    if env and Path(env).exists():
        return env
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or str(Path.home() / ".cache" / "ms-playwright")
    for cand in sorted(glob.glob(os.path.join(root, "chromium-*", "chrome-linux64", "chrome")), reverse=True):
        return cand
    return None


CHROMIUM = _chromium()
pytestmark = pytest.mark.skipif(
    CHROMIUM is None and not os.environ.get("CI"),
    reason="no Chromium available for the browser walk",
)

PIN = "482914"
# Obviously fake, valid BIP-39 words (never a real phrase).
WORDS = "zoo zebra youth wrong write worth world work wool wine wing wide".split()

_INIT = """
window.__violations = [];
document.addEventListener('securitypolicyviolation', (e) => {
  window.__violations.push(e.violatedDirective + ' ' + e.blockedURI + ' ' + (e.sample || ''));
});
"""
_CRYPTO_FAIL = """
(() => {
  const boom = () => { throw new Error('stubbed webcrypto failure'); };
  Object.defineProperty(window.crypto.subtle, 'generateKey', { value: boom });
  Object.defineProperty(window.crypto.subtle, 'deriveBits', { value: boom });
})();
"""


@pytest.fixture
def pair_server(tmp_path: Path):
    d = tmp_path / "totalreclaw"
    d.mkdir(mode=0o700)
    path = default_pair_sessions_path(d)
    servers = []

    def make(mode: str):
        kp = generate_gateway_keypair()
        session = create_pair_session(
            path, mode=mode, sk_b64=kp.sk_b64, pk_b64=kp.pk_b64, secondary_code=PIN
        )
        cfg = PairHttpConfig(
            sessions_path=path,
            complete_pairing=lambda phrase, s: CompletePairingResult(state="active", account_id="0x0"),
            bind_host="127.0.0.1",
            bind_port=0,
        )
        server = build_pair_http_server(cfg)
        server.start()
        servers.append(server)
        return server, session, kp

    yield make
    for s in servers:
        s.stop()


@pytest.fixture(scope="module")
def browser():
    with playwright_sync.sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
        yield b
        b.close()


def _open(browser, server, session, kp, crypto_fail=False):
    ctx = browser.new_context()
    ctx.grant_permissions(["clipboard-read", "clipboard-write"])
    page = ctx.new_page()
    errors: list[str] = []
    posts: list[str] = []
    page.add_init_script(_INIT)
    if crypto_fail:
        page.add_init_script(_CRYPTO_FAIL)
    page.on(
        "console",
        lambda m: errors.append(m.text)
        if any(k in m.text.lower() for k in ("content security policy", "trusted type", "refused to"))
        else None,
    )
    page.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))

    def intercept(route):
        if route.request.method == "POST":
            posts.append(route.request.url)
            route.fulfill(status=200, content_type="application/json", body="{}")
        else:
            route.continue_()

    page.route("**/pair/**", intercept)
    page.goto(f"http://{server.host}:{server.port}/pair/{session.sid}#pk={kp.pk_b64}")
    return ctx, page, errors, posts


def _assert_clean(page, errors):
    assert page.evaluate("window.__violations") == []
    assert errors == []


def _enter_pin(page):
    page.locator(".pin-cell").first.click()
    page.keyboard.type(PIN)
    page.wait_for_selector("#pin-continue:not([disabled])")
    page.click("#pin-continue")
    page.wait_for_selector("#screen-phrase.is-active, #screen-phrase:not([hidden])")


def _paste(page, words):
    page.evaluate(
        """(words) => {
          const el = document.querySelector('#phrase-grid-import .word-input');
          el.focus();
          const dt = new DataTransfer();
          dt.setData('text', words.join(' '));
          el.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
        }""",
        words,
    )


def test_import_flow_has_no_csp_violations(browser, pair_server):
    server, session, kp = pair_server("import")
    ctx, page, errors, posts = _open(browser, server, session, kp)
    _enter_pin(page)
    # typing into the grid
    page.locator("#phrase-grid-import .word-input").first.click()
    page.keyboard.type("zoo")
    page.keyboard.press("Space")
    page.keyboard.type("zebra")
    page.click("#paste-all")  # clipboard empty/other: exercises the toast path
    # full paste into the grid
    _paste(page, WORDS)
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('#phrase-grid-import .word-input')).every(i => i.value)"
    )
    # paste-all button reading the clipboard
    page.evaluate("(w) => navigator.clipboard.writeText(w.join(' '))", WORDS)
    page.click("#paste-all")
    page.wait_for_selector("#phrase-pair:not([disabled])")
    page.click("#phrase-pair")
    page.wait_for_selector("#screen-done.is-active, #screen-done:not([hidden])")
    assert len(posts) == 1
    _assert_clean(page, errors)
    ctx.close()


def test_generate_flow_has_no_csp_violations(browser, pair_server):
    server, session, kp = pair_server("generate")
    ctx, page, errors, posts = _open(browser, server, session, kp)
    page.evaluate(f"navigator.clipboard.writeText('{PIN}')")
    page.click("#pin-paste")
    page.wait_for_selector("#screen-phrase.is-active, #screen-phrase:not([hidden])")
    page.wait_for_function(
        "() => document.querySelectorAll('#phrase-grid-generate .word-input').length === 12"
    )
    page.click("#gen-regen")
    page.click("#gen-copy")
    page.wait_for_function("() => document.getElementById('gen-copy-label').textContent === 'Copied'")
    page.check("#gen-ack")
    page.click("#back-to-pin")
    page.wait_for_selector("#pin-continue")
    page.click("#pin-continue")
    page.wait_for_selector("#phrase-grid-generate .word-input")
    page.check("#gen-ack")
    page.wait_for_selector("#phrase-pair:not([disabled])")
    page.click("#phrase-pair")
    page.wait_for_selector("#screen-done.is-active, #screen-done:not([hidden])")
    assert len(posts) == 1
    _assert_clean(page, errors)
    ctx.close()


def test_crypto_probe_failure_path_has_no_csp_violations(browser, pair_server):
    server, session, kp = pair_server("import")
    ctx, page, errors, _ = _open(browser, server, session, kp, crypto_fail=True)
    page.wait_for_function("() => document.getElementById('err-banner') && !document.getElementById('err-banner').hidden")
    assert page.is_disabled("#pin-continue")
    _assert_clean(page, errors)
    ctx.close()


def test_trusted_types_block_dom_html_sinks(browser, pair_server):
    server, session, kp = pair_server("import")
    ctx, page, errors, _ = _open(browser, server, session, kp)
    results = page.evaluate(
        """() => {
          const attempt = (f) => { try { f(); return 'allowed'; } catch (e) { return 'blocked'; } };
          return [
            attempt(() => { document.body.innerHTML += ''; }),
            attempt(() => { document.body.insertAdjacentHTML('beforeend', '<i></i>'); }),
          ];
        }"""
    )
    assert results == ["blocked", "blocked"]
    ctx.close()


def test_session_line_is_styled_by_stylesheet(browser, pair_server):
    server, session, kp = pair_server("import")
    ctx, page, errors, _ = _open(browser, server, session, kp)
    size = page.evaluate("getComputedStyle(document.querySelector('.session-line')).fontSize")
    assert float(size.replace("px", "")) < 13
    _assert_clean(page, errors)
    ctx.close()
