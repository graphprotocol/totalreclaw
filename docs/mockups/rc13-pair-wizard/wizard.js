/**
 * TotalReclaw rc.13 pair-wizard mockup — vanilla JS.
 *
 * No dependencies. No storage. No network. No crypto. Pure UX.
 *
 * Step machine: 1 = PIN, 2 = phrase (import | generate), 3 = done.
 * A "fake submit" resolves after 800ms and advances to the done screen.
 *
 * NOTE: this is a mockup. Never log or transmit any word entered here.
 *       All input buffers are scrubbed on screen leave.
 */
(function () {
  'use strict';

  // ---------- State ----------
  const STATE = {
    step: 1,
    pin: ['', '', '', '', '', ''],
    mode: 'import', // 'import' | 'generate'
    words: new Array(12).fill(''),
    generated: null,
    ackChecked: false,
    expiresAtMs: Date.now() + 10 * 60 * 1000, // 10 minutes
    timerId: null,
  };

  // Deterministic demo phrase (NOT cryptographically generated — this is a
  // mockup). Real pair page uses WebCrypto entropy → BIP-39.
  const DEMO_PHRASE = [
    'anchor', 'velvet', 'ridge', 'harbor',
    'meadow', 'pilot', 'echo', 'falcon',
    'quiet', 'amber', 'cobalt', 'summit',
  ];

  // ---------- Helpers ----------
  const $ = (id) => document.getElementById(id);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  function pad2(n) { return n < 10 ? '0' + n : String(n); }

  function setDots() {
    const dots = $$('.step-dots .dot');
    dots.forEach((d) => {
      const n = Number(d.dataset.step);
      d.classList.toggle('done', n < STATE.step);
      d.classList.toggle('active', n === STATE.step);
    });
    const cur = $('step-current');
    if (cur) cur.textContent = STATE.step <= 3 ? String(STATE.step) : '3';
  }

  function transitionTo(nextId, direction) {
    // direction: 'forward' | 'back'
    const all = $$('.screen');
    const current = all.find((s) => s.classList.contains('active'));
    const next = $(nextId);
    if (!current || !next || current === next) return;

    const forward = direction !== 'back';
    const exitClass = forward ? 'exit-left' : 'enter-right';
    const enterClass = forward ? 'enter-right' : 'exit-left';

    // Prep next off-screen to the correct side
    next.hidden = false;
    next.classList.remove('active');
    next.classList.remove('exit-left', 'enter-right');
    next.classList.add(forward ? 'enter-right' : 'exit-left');

    // Force reflow so the browser registers the starting transform
    // eslint-disable-next-line no-unused-expressions
    next.offsetWidth;

    current.classList.remove('active');
    current.classList.add(exitClass);

    next.classList.remove('enter-right', 'exit-left');
    next.classList.add('active');

    const onDone = () => {
      current.hidden = true;
      current.classList.remove('exit-left', 'enter-right');
      next.removeEventListener('transitionend', onDone);
    };
    next.addEventListener('transitionend', onDone);

    // Fallback in case transitionend doesn't fire (reduced-motion, etc.)
    setTimeout(onDone, 400);
  }

  // ---------- Countdown ----------
  function renderCountdown() {
    const remaining = Math.max(0, STATE.expiresAtMs - Date.now());
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

  // ---------- Step 1: PIN ----------
  function initPinStep() {
    const cells = $$('.pin-cell');
    const continueBtn = $('pin-continue');
    const errEl = $('pin-error');

    function updateFilledClass() {
      cells.forEach((c, i) => {
        c.classList.toggle('filled', !!STATE.pin[i]);
      });
    }

    function updateContinueState() {
      const complete = STATE.pin.every((d) => /^\d$/.test(d));
      continueBtn.disabled = !complete;
    }

    function clearError() {
      errEl.classList.remove('show');
      errEl.textContent = '';
    }

    function showError(msg) {
      errEl.textContent = msg;
      errEl.classList.add('show');
    }

    cells.forEach((cell, i) => {
      cell.addEventListener('input', (e) => {
        const raw = e.target.value.replace(/\D/g, '');
        if (!raw) {
          STATE.pin[i] = '';
          updateFilledClass();
          updateContinueState();
          return;
        }
        // Take only the first digit; handle paste of multi-char in keydown/paste instead
        const digit = raw[raw.length - 1];
        STATE.pin[i] = digit;
        e.target.value = digit;
        updateFilledClass();
        updateContinueState();
        clearError();
        // Auto-advance
        if (i < cells.length - 1) {
          cells[i + 1].focus();
        } else {
          cell.blur();
        }
      });

      cell.addEventListener('keydown', (e) => {
        if (e.key === 'Backspace' && !cell.value && i > 0) {
          e.preventDefault();
          cells[i - 1].focus();
          cells[i - 1].value = '';
          STATE.pin[i - 1] = '';
          updateFilledClass();
          updateContinueState();
        } else if (e.key === 'ArrowLeft' && i > 0) {
          e.preventDefault();
          cells[i - 1].focus();
        } else if (e.key === 'ArrowRight' && i < cells.length - 1) {
          e.preventDefault();
          cells[i + 1].focus();
        } else if (e.key === 'Enter') {
          if (!continueBtn.disabled) continueBtn.click();
        }
      });

      cell.addEventListener('focus', () => cell.select());

      cell.addEventListener('paste', (e) => {
        e.preventDefault();
        const text = (e.clipboardData || window.clipboardData).getData('text') || '';
        const digits = text.replace(/\D/g, '').slice(0, 6 - i).split('');
        digits.forEach((d, k) => {
          const target = cells[i + k];
          if (target) {
            target.value = d;
            STATE.pin[i + k] = d;
          }
        });
        updateFilledClass();
        updateContinueState();
        const next = Math.min(i + digits.length, cells.length - 1);
        cells[next].focus();
      });
    });

    continueBtn.addEventListener('click', () => {
      const complete = STATE.pin.every((d) => /^\d$/.test(d));
      if (!complete) {
        // Subtle shake for the first empty cell
        const firstEmpty = cells.find((c, idx) => !STATE.pin[idx]);
        if (firstEmpty) {
          firstEmpty.classList.add('shake');
          setTimeout(() => firstEmpty.classList.remove('shake'), 420);
          firstEmpty.focus();
        }
        showError('Enter all 6 digits to continue.');
        return;
      }
      clearError();
      // Advance to step 2
      STATE.step = 2;
      setDots();
      transitionTo('screen-phrase', 'forward');
      // Focus the first word input of the active panel
      setTimeout(focusFirstWordOfActivePanel, 300);
    });

    // Autofocus first cell on load
    setTimeout(() => cells[0] && cells[0].focus(), 150);
  }

  // ---------- Step 2: Phrase ----------
  function buildWordGrid(gridEl, options) {
    gridEl.innerHTML = '';
    const readonly = !!options.readonly;
    for (let i = 0; i < 12; i++) {
      const cell = document.createElement('label');
      cell.className = 'word-cell';
      cell.setAttribute('for', `word-${options.prefix}-${i}`);

      const idx = document.createElement('span');
      idx.className = 'word-idx';
      idx.textContent = String(i + 1) + '.';
      idx.setAttribute('aria-hidden', 'true');

      const input = document.createElement('input');
      input.className = 'word-input';
      input.id = `word-${options.prefix}-${i}`;
      input.type = 'text';
      input.autocomplete = 'off';
      input.autocapitalize = 'none';
      input.spellcheck = false;
      input.dataset.index = String(i);
      input.setAttribute('aria-label', `Word ${i + 1}`);
      input.placeholder = '';
      if (readonly) {
        input.readOnly = true;
        input.tabIndex = -1;
      } else {
        input.placeholder = 'word';
      }

      cell.appendChild(idx);
      cell.appendChild(input);
      gridEl.appendChild(cell);
    }
  }

  function wireImportGrid() {
    const grid = $('phrase-grid-import');
    const inputs = $$('.word-input', grid);

    function updatePairState() {
      const complete = STATE.words.every((w) => w.trim().length > 0);
      $('phrase-pair').disabled = !complete;
    }

    function setWord(i, val) {
      const trimmed = val.trim().toLowerCase();
      STATE.words[i] = trimmed;
      inputs[i].parentElement.classList.toggle('filled', trimmed.length > 0);
      updatePairState();
    }

    inputs.forEach((input, i) => {
      input.addEventListener('input', (e) => {
        setWord(i, e.target.value);
      });

      input.addEventListener('keydown', (e) => {
        if (e.key === ' ' || e.key === 'Enter' || e.key === 'Tab') {
          if (e.key !== 'Tab') e.preventDefault();
          // Auto-advance on space/enter
          if (inputs[i + 1]) inputs[i + 1].focus();
          else input.blur();
        } else if (e.key === 'Backspace' && !input.value && i > 0) {
          e.preventDefault();
          inputs[i - 1].focus();
        }
      });

      input.addEventListener('paste', (e) => {
        const text = (e.clipboardData || window.clipboardData).getData('text') || '';
        const words = text.trim().split(/\s+/).filter(Boolean);
        if (words.length < 2) return; // single-word paste — let browser handle
        e.preventDefault();
        const remaining = 12 - i;
        const toInsert = words.slice(0, remaining);
        toInsert.forEach((w, k) => {
          const idx = i + k;
          inputs[idx].value = w.toLowerCase();
          setWord(idx, w);
        });
        const next = Math.min(i + toInsert.length, inputs.length - 1);
        inputs[next].focus();
      });
    });

    $('paste-all').addEventListener('click', async () => {
      // Try clipboard API first
      let text = '';
      if (navigator.clipboard && navigator.clipboard.readText) {
        try { text = await navigator.clipboard.readText(); } catch (_) { /* ignore */ }
      }
      if (!text) {
        // Fallback: demo-prefill with the demo phrase to let reviewer click through
        text = DEMO_PHRASE.join(' ');
      }
      const words = text.trim().split(/\s+/).slice(0, 12);
      words.forEach((w, k) => {
        inputs[k].value = w.toLowerCase();
        setWord(k, w);
      });
      inputs[Math.min(words.length, 11)].focus();
    });
  }

  function wireGenerateGrid() {
    const grid = $('phrase-grid-generate');
    const inputs = $$('.word-input', grid);

    function fill(phrase) {
      STATE.generated = phrase.slice();
      phrase.forEach((w, i) => {
        inputs[i].value = w;
        inputs[i].parentElement.classList.add('filled');
      });
    }

    fill(DEMO_PHRASE);

    function updatePairState() {
      const ready = STATE.generated && STATE.generated.length === 12 && STATE.ackChecked;
      $('phrase-pair').disabled = !ready;
    }

    $('gen-ack').addEventListener('change', (e) => {
      STATE.ackChecked = !!e.target.checked;
      updatePairState();
    });

    $('gen-copy').addEventListener('click', async () => {
      const label = $('gen-copy-label');
      if (!STATE.generated) return;
      try {
        await navigator.clipboard.writeText(STATE.generated.join(' '));
        label.textContent = 'Copied';
        setTimeout(() => { label.textContent = 'Copy'; }, 1600);
      } catch (_) {
        label.textContent = 'Copy blocked';
        setTimeout(() => { label.textContent = 'Copy'; }, 1600);
      }
    });

    $('gen-regen').addEventListener('click', () => {
      // Mockup — rotate demo phrase so user sees it "regenerate"
      const shifted = DEMO_PHRASE.slice();
      // Cheap deterministic rotate so reviewer sees a change
      shifted.push(shifted.shift());
      fill(shifted);
      // Ack resets on regenerate
      STATE.ackChecked = false;
      $('gen-ack').checked = false;
      updatePairState();
    });
  }

  function focusFirstWordOfActivePanel() {
    const panelId = STATE.mode === 'import' ? 'panel-import' : 'panel-generate';
    const input = $(panelId).querySelector('.word-input:not([readonly])');
    if (input) input.focus();
  }

  function wireTabs() {
    const tabs = $$('.tab');
    const tabsRow = document.querySelector('.tabs');

    function setMode(mode) {
      STATE.mode = mode;
      tabs.forEach((t) => {
        const active = t.dataset.mode === mode;
        t.classList.toggle('active', active);
        t.setAttribute('aria-selected', active ? 'true' : 'false');
      });
      tabsRow.classList.toggle('mode-generate', mode === 'generate');
      $('panel-import').hidden = mode !== 'import';
      $('panel-generate').hidden = mode !== 'generate';

      // Re-evaluate Pair button
      if (mode === 'import') {
        $('phrase-pair').disabled = !STATE.words.every((w) => w.trim().length > 0);
      } else {
        $('phrase-pair').disabled = !(STATE.generated && STATE.ackChecked);
      }
    }

    tabs.forEach((t) => {
      t.addEventListener('click', () => setMode(t.dataset.mode));
      t.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
          e.preventDefault();
          const next = tabs.find((x) => x !== t);
          if (next) { next.focus(); setMode(next.dataset.mode); }
        }
      });
    });
  }

  function initPhraseStep() {
    buildWordGrid($('phrase-grid-import'), { prefix: 'i', readonly: false });
    buildWordGrid($('phrase-grid-generate'), { prefix: 'g', readonly: true });
    wireImportGrid();
    wireGenerateGrid();
    wireTabs();

    $('back-to-pin').addEventListener('click', () => {
      STATE.step = 1;
      setDots();
      transitionTo('screen-pin', 'back');
    });

    $('phrase-pair').addEventListener('click', async () => {
      const btn = $('phrase-pair');
      if (btn.disabled) return;
      btn.disabled = true;
      btn.classList.add('loading');
      // Fake submit — 800ms
      await new Promise((r) => setTimeout(r, 800));

      // Scrub inputs in-memory (mockup — no real phrase, but keep the habit)
      STATE.words = STATE.words.map(() => '');
      STATE.generated = null;
      $$('.word-input').forEach((el) => { el.value = ''; });
      // Also clear PIN buffer
      STATE.pin = ['', '', '', '', '', ''];
      $$('.pin-cell').forEach((el) => { el.value = ''; });

      btn.classList.remove('loading');
      btn.disabled = false;
      STATE.step = 3;
      setDots();
      transitionTo('screen-done', 'forward');
      // Stop timer — session consumed
      if (STATE.timerId) { clearInterval(STATE.timerId); STATE.timerId = null; }
      const cd = $('countdown');
      if (cd) cd.style.visibility = 'hidden';
    });
  }

  // ---------- Step 3: Done ----------
  function initDoneStep() {
    $('close-link').addEventListener('click', (e) => {
      e.preventDefault();
      // Demo-only: reset wizard so reviewer can click through again
      resetWizard();
    });
  }

  function resetWizard() {
    STATE.step = 1;
    STATE.pin = ['', '', '', '', '', ''];
    STATE.words = new Array(12).fill('');
    STATE.generated = null;
    STATE.ackChecked = false;
    STATE.expiresAtMs = Date.now() + 10 * 60 * 1000;
    setDots();

    $$('.pin-cell').forEach((el) => {
      el.value = '';
      el.classList.remove('filled', 'shake');
    });
    $$('.word-input').forEach((el) => {
      if (!el.readOnly) el.value = '';
      el.parentElement.classList.remove('filled');
    });
    $('gen-ack').checked = false;
    $('pin-continue').disabled = true;
    $('phrase-pair').disabled = true;
    const cd = $('countdown');
    if (cd) {
      cd.style.visibility = '';
      cd.classList.remove('warn', 'expired');
    }

    // Rebuild generated panel so it re-fills with demo phrase cleanly
    buildWordGrid($('phrase-grid-generate'), { prefix: 'g', readonly: true });
    wireGenerateGrid();

    // Back to step 1
    const currentActive = document.querySelector('.screen.active');
    if (currentActive && currentActive.id !== 'screen-pin') {
      transitionTo('screen-pin', 'back');
    }

    // Restart timer
    if (STATE.timerId) clearInterval(STATE.timerId);
    startTimer();

    setTimeout(() => $$('.pin-cell')[0] && $$('.pin-cell')[0].focus(), 320);
  }

  // ---------- Boot ----------
  document.addEventListener('DOMContentLoaded', () => {
    setDots();
    startTimer();
    initPinStep();
    initPhraseStep();
    initDoneStep();
  });
})();
