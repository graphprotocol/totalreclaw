/**
 * @jest-environment node
 *
 * phrase-safety-dist.test.ts
 *
 * Regression guard for the 3.2.1 SECURITY hotfix that removed the
 * `totalreclaw_setup` MCP tool. The deleted tool used to return the
 * 12-word BIP-39 recovery phrase via a `recovery_phrase` field in its
 * tool response — which crossed the LLM context every time an agent
 * invoked it. That violated the phrase-safety invariant
 * ("the recovery phrase MUST NEVER cross the LLM context").
 *
 * This test scans the COMPILED `dist/` JavaScript (not the TypeScript
 * source) for any string literal that looks like an MCP-tool response
 * emitting `recovery_phrase` or `mnemonic` as an OBJECT KEY. Source
 * lines that merely reference the literal (e.g. "Set
 * TOTALRECLAW_RECOVERY_PHRASE in your config" instructional copy) are
 * ignored — we only care about response-payload-shaped emissions
 * (`recovery_phrase:` / `"recovery_phrase":` / `result.recovery_phrase = ...`
 * / `mnemonic:` as a key inside a tool-handler return).
 *
 * The test FAILS if any such emission is found in `mcp/dist/`. Run via
 * `npm test` after `npm run build`. The build step is what the
 * `mcp-tests` CI job in `.github/workflows/ci.yml` already executes
 * (`npm run build && npm test`), so this catches regressions at PR
 * time.
 *
 * Important: this test does NOT scan reads of the file at
 * `~/.totalreclaw/credentials.json` (where the `mnemonic` key is the
 * persisted credential format and reading it on disk is not a phrase-
 * safety violation). It only flags lines that emit `mnemonic` /
 * `recovery_phrase` as a property of an object built into a tool
 * response or other LLM-visible payload.
 */

import * as fs from 'node:fs';
import * as path from 'node:path';

// ---------------------------------------------------------------------------
// Patterns: an emission is a line that builds a JSON-shaped tool response
// with `recovery_phrase` or `mnemonic` as a key. The dist/ output of TS is
// JavaScript, so we look for both object-literal shape (`key: value`,
// `"key": value`) and assignment shape (`obj.key = ...`) that target
// fields likely to be returned in a tool result payload.
// ---------------------------------------------------------------------------

interface PhrasePattern {
  name: string;
  // Match the pattern in compiled JS. Must be JS-shape, not TS-shape.
  regex: RegExp;
  // A regex of ALLOWED contexts on the same line — if the matched line
  // contains an allow pattern, the match is skipped. Used to whitelist
  // legitimate persistence reads / writes (e.g. credentials.json) and
  // non-payload utility code paths.
  allowedContext?: RegExp;
}

const FORBIDDEN_PATTERNS: PhrasePattern[] = [
  // Plain JS object key: `recovery_phrase: <expr>` (used in JSON.stringify
  // of a tool response). The deleted handler had two such emissions.
  {
    name: 'recovery_phrase as JS object key (unquoted)',
    regex: /\brecovery_phrase\s*:/,
    // The setup-CLI's saved-credentials migration helper persists to
    // disk only — it never returns the phrase to a tool caller. Allow
    // lines that ALSO mention writeFileSync / credentials.json / disk-
    // facing context AND don't build a tool-response object.
    allowedContext:
      /credentials\.json|writeFileSync|readFileSync|CREDENTIALS_PATH|description|inputSchema|process\.env|TOTALRECLAW_RECOVERY_PHRASE/i,
  },
  // Quoted JS object key: `"recovery_phrase": <expr>`.
  {
    name: 'recovery_phrase as JS object key (quoted)',
    regex: /"recovery_phrase"\s*:/,
    allowedContext:
      /credentials\.json|writeFileSync|readFileSync|CREDENTIALS_PATH|description|inputSchema|process\.env|TOTALRECLAW_RECOVERY_PHRASE/i,
  },
  // Property assignment: `<obj>.recovery_phrase = ...`. The deleted
  // handler had `result.recovery_phrase = mnemonic;` exactly here.
  {
    name: 'recovery_phrase property assignment',
    regex: /\.recovery_phrase\s*=/,
    allowedContext: /\.recovery_phrase\s*=\s*undefined/, // explicit nulling allowed
  },
  // mnemonic as a JS object key. The persisted SavedCredentials shape
  // legitimately uses this key, so we allow lines that ALSO mention
  // `credentials.json` / `CREDENTIALS_PATH` / disk persistence on the
  // same line. We're catching mnemonic keys built into tool responses.
  {
    name: 'mnemonic as JS object key (unquoted)',
    regex: /\bmnemonic\s*:/,
    allowedContext:
      /credentials\.json|writeFileSync|readFileSync|CREDENTIALS_PATH|SavedCredentials|generateMnemonic|validateMnemonic|mnemonicToAccount|mnemonicToSeedSync|description|inputSchema|process\.env|state\.mnemonic|subgraphState\.mnemonic|trimmed\.mnemonic|parsed\.mnemonic|input\?\.\s*mnemonic|input\.mnemonic|cli\/setup|TOTALRECLAW_RECOVERY_PHRASE/i,
  },
  // Quoted JS object key: `"mnemonic": <expr>`.
  {
    name: 'mnemonic as JS object key (quoted)',
    regex: /"mnemonic"\s*:/,
    allowedContext:
      /credentials\.json|writeFileSync|readFileSync|CREDENTIALS_PATH|SavedCredentials|generateMnemonic|validateMnemonic|description|inputSchema|process\.env|cli\/setup|TOTALRECLAW_RECOVERY_PHRASE/i,
  },
  // Property assignment: `<obj>.mnemonic = ...`. Persisted-credentials
  // shape legitimately writes this; tool-response shape must not.
  {
    name: 'mnemonic property assignment',
    regex: /\.mnemonic\s*=(?!=)/,
    allowedContext:
      /credentials\.mnemonic|SavedCredentials|cli\/setup|writeFileSync|readFileSync|state\.mnemonic|subgraphState\.mnemonic|\.mnemonic\s*=\s*undefined/i,
  },
];

// ---------------------------------------------------------------------------
// File walk: every .js file under mcp/dist/ in scope. We deliberately do not
// scan source-map (.map) files or .d.ts type declarations.
// ---------------------------------------------------------------------------

const DIST_DIR = path.resolve(__dirname, '..', 'dist');

function walkJs(dir: string): string[] {
  if (!fs.existsSync(dir)) return [];
  const out: string[] = [];
  const stack: string[] = [dir];
  while (stack.length) {
    const cur = stack.pop()!;
    const entries = fs.readdirSync(cur, { withFileTypes: true });
    for (const e of entries) {
      const full = path.join(cur, e.name);
      if (e.isDirectory()) {
        stack.push(full);
      } else if (e.isFile() && full.endsWith('.js')) {
        out.push(full);
      }
    }
  }
  return out.sort();
}

interface Violation {
  file: string;
  line: number;
  pattern: string;
  text: string;
}

function scanFile(file: string): Violation[] {
  const content = fs.readFileSync(file, 'utf-8');
  const lines = content.split('\n');
  const violations: Violation[] = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    // Skip pure comment lines — they're not emitted in payloads.
    const trimmed = line.trim();
    if (
      trimmed.startsWith('//') ||
      trimmed.startsWith('*') ||
      trimmed.startsWith('/*')
    ) {
      continue;
    }
    for (const pat of FORBIDDEN_PATTERNS) {
      if (pat.regex.test(line)) {
        if (pat.allowedContext && pat.allowedContext.test(line)) {
          continue;
        }
        violations.push({
          file,
          line: i + 1,
          pattern: pat.name,
          text: line.trim().slice(0, 160),
        });
      }
    }
  }
  return violations;
}

// ---------------------------------------------------------------------------
// Test
// ---------------------------------------------------------------------------

describe('phrase-safety: compiled dist/ does not emit phrase strings in response payloads', () => {
  it('mcp/dist/ exists (build must run before this test)', () => {
    expect(fs.existsSync(DIST_DIR)).toBe(true);
  });

  it('no recovery_phrase / mnemonic emission in any compiled JS file', () => {
    const files = walkJs(DIST_DIR);
    expect(files.length).toBeGreaterThan(0);

    const allViolations: Violation[] = [];
    for (const f of files) {
      allViolations.push(...scanFile(f));
    }

    if (allViolations.length > 0) {
      const msg = allViolations
        .map(
          (v) =>
            `  ${path.relative(DIST_DIR, v.file)}:${v.line} [${v.pattern}]\n    ${v.text}`,
        )
        .join('\n');
      throw new Error(
        `\nPHRASE-SAFETY VIOLATION: ${allViolations.length} occurrence(s) in compiled dist/.\n\n` +
          `These shapes emit the user's recovery phrase or mnemonic into a JSON-shaped\n` +
          `tool response — every agent calling such a tool gets the phrase in LLM context.\n` +
          `Phrase-safety rule: the recovery phrase MUST NEVER cross the LLM context.\n` +
          `Setup must follow the URL-driven flow at docs/guides/claude-code-setup.md.\n\n` +
          msg +
          '\n',
      );
    }
  });
});
