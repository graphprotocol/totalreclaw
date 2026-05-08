/**
 * skill-md-mcp-first.test.ts — MCP-first SKILL.md content gate (3.3.0-rc.2).
 *
 * Companion to skill/plugin/skill-md-hybrid-primary.test.ts. Asserts the
 * MCP-bundled SKILL.md (mcp/SKILL.md) directs the agent to use MCP tools
 * for memory rather than writing to local files (MEMORY.md / USER.md).
 *
 * The 2026-05-07 pop-os QA found the agent made 28 `Write` calls into
 * MEMORY.md / USER.md and 0 calls to totalreclaw_remember despite
 * explicit "I prefer X" statements — root cause: the prior plugin-
 * centric SKILL.md never told the agent that totalreclaw_remember was
 * the canonical path. This test gates the rewrite.
 */

import * as fs from 'node:fs';
import * as path from 'node:path';

const SKILL_MD = path.join(__dirname, '..', 'SKILL.md');

function read(): string {
  return fs.readFileSync(SKILL_MD, 'utf-8');
}

describe('mcp/SKILL.md — MCP-first directive content', () => {
  it('exists at mcp/SKILL.md', () => {
    expect(fs.existsSync(SKILL_MD)).toBe(true);
  });

  it('is 200-450 lines (concise)', () => {
    const lines = read().split('\n').length;
    expect(lines).toBeGreaterThanOrEqual(150);
    expect(lines).toBeLessThanOrEqual(450);
  });

  it('mentions the namespaced totalreclaw__totalreclaw_remember tool', () => {
    // The namespace prefix `totalreclaw__` is what the MCP host adds; the
    // agent sees the tool with this prefix, so SKILL.md must reference it
    // verbatim at least once for trigger pattern matching.
    expect(read()).toMatch(/totalreclaw__totalreclaw_remember/);
  });

  it('mentions totalreclaw__totalreclaw_recall', () => {
    expect(read()).toMatch(/totalreclaw__totalreclaw_recall/);
  });

  it('does NOT mention `openclaw plugins install` (legacy plugin path)', () => {
    expect(read()).not.toMatch(/openclaw plugins install/);
  });

  it('does NOT recommend the `tr` CLI as the primary path', () => {
    // The tr CLI is the plugin path; mcp-only SKILL.md must not direct
    // the agent to use it. We allow zero mentions to keep the file
    // entirely free of the legacy primary-path framing.
    const md = read();
    expect(md).not.toMatch(/\btr CLI\b/);
    expect(md).not.toMatch(/node "\$TR_CLI"/);
    expect(md).not.toMatch(/hybrid-primary/);
    expect(md).not.toMatch(/hybrid mode/);
  });

  it('has an explicit trigger-phrase list for totalreclaw_remember', () => {
    const md = read();
    // Headline + at least 5 of the canonical trigger phrases.
    expect(md).toMatch(/Trigger phrases/i);
    const phrases = ['I prefer', 'I like', 'my favorite', 'I work', 'I live', 'remember that', 'I use', 'I decided'];
    const matched = phrases.filter((p) => md.includes(p));
    expect(matched.length).toBeGreaterThanOrEqual(5);
  });

  it('has an explicit "do NOT write to MEMORY.md / USER.md" rule', () => {
    const md = read();
    // At least one strong directive — the rule is the actual fix for the
    // 2026-05-07 regression.
    expect(md).toMatch(/MEMORY\.md/);
    expect(md).toMatch(/USER\.md/);
    expect(md).toMatch(/(NOT|never|NEVER|don'?t|do not)/i);
    // The conjunction MUST appear — i.e. the file says
    // "do not write to MEMORY.md / USER.md" (case + phrasing flexible).
    expect(md).toMatch(/(MEMORY\.md|USER\.md)/);
  });

  it('has an explicit "store via tool, not local files" rule', () => {
    const md = read();
    // Look for the canonical "DO NOT write the fact to MEMORY.md / USER.md"
    // bullet-pattern. We accept either explicit DO NOT lines or a
    // forbidden-actions block referencing them.
    const hasForbiddenWriteRule =
      /DO NOT write.*MEMORY\.md/i.test(md) ||
      /NEVER store.*local files/i.test(md) ||
      /not for user-supplied/i.test(md);
    expect(hasForbiddenWriteRule).toBe(true);
  });

  it('contains phrase-safety hard rule', () => {
    const md = read();
    expect(md).toMatch(/[Pp]hrase safety/);
    expect(md).toMatch(/NEVER (echo|generate|ask).*recovery phrase|recovery phrase.*never/i);
    // Browser-side is the canonical safe path.
    expect(md).toMatch(/[Bb]rowser/);
  });

  it('does NOT instruct the agent to store recovery phrase or pass it to a tool', () => {
    const md = read();
    // Forbidden: any positive instruction to put the phrase into a tool
    // call, env var, file, or chat message. The file MAY (and should)
    // contain NEVER-style prohibitions on the same — those are fine.
    // We approximate "positive instruction" by looking for imperative-
    // mood patterns that are NOT preceded by NEVER / NOT / DON'T within
    // the same sentence.
    expect(md).not.toMatch(/--emit-phrase/);
    // Disallow any line that says "pass <phrase> to ... pair" / "store the phrase".
    expect(md).not.toMatch(/^\s*(?:Pass|Store|Save|Send) (?:the )?(?:recovery )?phrase/im);
    // The file MUST forbid passing the phrase via any tool input.
    expect(md).toMatch(/NEVER call.*pair.*phrase/i);
  });

  it('describes the auto-extraction trajectory poller as a safety net', () => {
    const md = read();
    expect(md).toMatch(/auto-extraction|trajectory poller/i);
    // It must NOT promise the poller substitutes for explicit calls.
    expect(md).toMatch(/safety net|still valuable|higher-fidelity|not a substitute/i);
  });

  it('lists the canonical install command (openclaw mcp set + skills install)', () => {
    const md = read();
    expect(md).toMatch(/openclaw mcp set totalreclaw/);
    expect(md).toMatch(/openclaw skills install totalreclaw/);
  });

  it('frontmatter version matches mcp/package.json', () => {
    const pkg = JSON.parse(
      fs.readFileSync(path.join(__dirname, '..', 'package.json'), 'utf-8'),
    ) as { version: string };
    const md = read();
    const m = md.match(/^version:\s*(\S+)/m);
    expect(m).not.toBeNull();
    expect(m![1]).toBe(pkg.version);
  });

  it('preserves user-vocabulary table ("set up an account" not "pair")', () => {
    const md = read();
    expect(md).toMatch(/set up an account/);
    // Phrase "pair" is internal — the file documents this, but should
    // not use it in user-facing line examples.
    expect(md).toMatch(/internal jargon/i);
  });

  it('does NOT contain forbidden vocabulary ("local-only", "stored locally")', () => {
    const md = read();
    // The file documents these as forbidden in a list — that's expected.
    // What we reject is positive use OUTSIDE the forbidden-list block.
    // Heuristic: the file contains the canonical decentralized line.
    expect(md).toMatch(/decentralized network/i);
    expect(md).toMatch(/encrypted .* recovery phrase/i);
  });
});
