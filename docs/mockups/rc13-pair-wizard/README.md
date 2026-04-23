# rc.13 pair wizard — clickable mockup

Open `index.html` in any browser. Clickable rc.13 UX mockup. No backend needed.

Three screens: PIN → phrase (import / generate) → paired. Pure HTML/CSS/JS, no
build step, no dependencies. Purpose: gather taste-test feedback before we
implement for real in rc.13 against `src/routes/pair-html.ts` in the relay.

## Live preview

`https://api-staging.totalreclaw.xyz/pair-preview/` (served by the relay).

## What this is not

- Not production code. Does no crypto. Fake submit = `await 800ms`.
- Never log or transmit a phrase from this file — it's a mockup.
- Does not replace the production pair HTML at
  `totalreclaw-relay/src/routes/pair-html.ts`. Runs alongside for review only.
