# rc.13 pair wizard — clickable mockup

Open `index.html` in any browser. Clickable rc.13 UX mockup. No backend needed.

Three screens: PIN → phrase (Set up / Log in) → done. Pure HTML/CSS/JS, no
build step, no dependencies. Purpose: gather taste-test feedback before we
implement for real in rc.13 against `src/routes/pair-html.ts` in the relay.

## Live preview

`https://api-staging.totalreclaw.xyz/pair-preview/` (served by the relay).

## Preview mode

When the page is loaded under `/pair-preview/` or with `?preview=1`, the
mockup:

- shows a small orange **PREVIEW** badge at the top,
- leaves both primary CTAs (**Continue**, **Set up TotalReclaw** / **Log in**)
  always clickable so a reviewer can click through the whole flow without
  typing anything.

The detection is path + query based (see `isPreviewMode()` in `wizard.js`). The
production pair page — served from a different route entirely — never matches
this check and keeps strict validation.

## Copy decisions (2026-04-23)

- "Setup" replaces "Pair" in user-facing copy. The tool name
  `totalreclaw_pair` and internal routes stay as-is.
- Default tab on step 2 is **Set up** (generate). **Log in** is the returning
  user branch (restore from existing phrase).
- Step 3 heading stays "You're all set"; subheading now reads "TotalReclaw
  account created. Your memories are encrypted to your recovery phrase."

## PIN input UX

- Cell 1 carries `autocomplete="one-time-code"` so iOS can surface
  SMS-autofill suggestions. The input handler detects multi-digit writes
  and distributes them across the 6 cells.
- A **Paste** button under the cells reads the clipboard via
  `navigator.clipboard.readText()` and drops the first six digits into the
  cells. If the browser blocks the API, a toast appears ("Paste not allowed —
  type manually") and the user types in each cell manually.

## What this is not

- Not production code. Does no crypto. Fake submit = `await 800ms`.
- Never log or transmit a phrase from this file — it's a mockup.
- Does not replace the production pair HTML at
  `totalreclaw-relay/src/routes/pair-html.ts`. Runs alongside for review only.
