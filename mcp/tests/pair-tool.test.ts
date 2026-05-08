/**
 * Tests for the totalreclaw_pair tool — definition shape, env-var resolution,
 * and URL/PIN format the tool emits to the agent.
 *
 * The full WS round-trip is exercised by the plugin-side
 * `pair-remote-client.test.ts` and the relay's own conformance suite — both
 * are byte-compatible with this module since `pair-remote-client.ts` and
 * `pair-crypto.ts` are verbatim ports.
 */

import {
  pairToolDefinition,
  resolvePairRelayUrl,
} from '../src/tools/pair';

describe('pairToolDefinition', () => {
  it('has the canonical tool name', () => {
    expect(pairToolDefinition.name).toBe('totalreclaw_pair');
  });

  it('has a non-empty description', () => {
    expect(typeof pairToolDefinition.description).toBe('string');
    expect(pairToolDefinition.description.length).toBeGreaterThan(0);
  });

  it('description references browser-side phrase generation', () => {
    // Phrase-safety hard rail signal — the description MUST NOT imply the
    // agent generates the phrase. It MUST tell the agent the browser does it.
    expect(pairToolDefinition.description).toMatch(/browser/i);
    expect(pairToolDefinition.description).toMatch(/never/i);
  });

  it('has an inputSchema with optional `mode` enum', () => {
    expect(pairToolDefinition.inputSchema.type).toBe('object');
    const props = pairToolDefinition.inputSchema.properties as Record<string, unknown>;
    expect(props.mode).toBeDefined();
    const mode = props.mode as { type?: string; enum?: string[]; default?: string };
    expect(mode.type).toBe('string');
    expect(mode.enum).toEqual(['generate', 'import']);
    expect(mode.default).toBe('generate');
  });

  it('has the standard tool annotations', () => {
    expect(pairToolDefinition.annotations).toBeDefined();
    expect(typeof pairToolDefinition.annotations.readOnlyHint).toBe('boolean');
    expect(typeof pairToolDefinition.annotations.destructiveHint).toBe('boolean');
    expect(typeof pairToolDefinition.annotations.idempotentHint).toBe('boolean');
    // Pair is NOT idempotent (each call opens a fresh relay session) and is
    // NOT destructive — but it DOES write credentials.json so it's not
    // read-only either.
    expect(pairToolDefinition.annotations.readOnlyHint).toBe(false);
    expect(pairToolDefinition.annotations.destructiveHint).toBe(false);
    expect(pairToolDefinition.annotations.idempotentHint).toBe(false);
  });
});

describe('resolvePairRelayUrl', () => {
  it('prefers TOTALRECLAW_PAIR_RELAY_URL when set', () => {
    const result = resolvePairRelayUrl({
      TOTALRECLAW_PAIR_RELAY_URL: 'wss://relay-explicit.example.com',
      TOTALRECLAW_SERVER_URL: 'https://api-staging.totalreclaw.xyz',
    });
    expect(result).toBe('wss://relay-explicit.example.com');
  });

  it('strips trailing slashes from the explicit override', () => {
    const result = resolvePairRelayUrl({
      TOTALRECLAW_PAIR_RELAY_URL: 'wss://relay.example.com/',
    });
    expect(result).toBe('wss://relay.example.com');
  });

  it('rewrites https TOTALRECLAW_SERVER_URL to wss', () => {
    const result = resolvePairRelayUrl({
      TOTALRECLAW_SERVER_URL: 'https://api-staging.totalreclaw.xyz',
    });
    expect(result).toBe('wss://api-staging.totalreclaw.xyz');
  });

  it('rewrites http TOTALRECLAW_SERVER_URL to ws (self-hosted plain HTTP)', () => {
    const result = resolvePairRelayUrl({
      TOTALRECLAW_SERVER_URL: 'http://localhost:8080',
    });
    expect(result).toBe('ws://localhost:8080');
  });

  it('falls back to wss://api.totalreclaw.xyz when neither env var is set', () => {
    const result = resolvePairRelayUrl({});
    expect(result).toBe('wss://api.totalreclaw.xyz');
  });

  it('treats an empty TOTALRECLAW_PAIR_RELAY_URL as unset', () => {
    const result = resolvePairRelayUrl({
      TOTALRECLAW_PAIR_RELAY_URL: '   ',
      TOTALRECLAW_SERVER_URL: 'https://api-staging.totalreclaw.xyz',
    });
    expect(result).toBe('wss://api-staging.totalreclaw.xyz');
  });
});

describe('phrase-safety contract', () => {
  it('tool description does NOT instruct the agent to generate or paste a phrase', () => {
    const desc = pairToolDefinition.description.toLowerCase();
    // Negative guards — these strings would signal an LLM-context phrase
    // path, which is the rule we explicitly forbid.
    expect(desc).not.toMatch(/paste your phrase in chat/);
    expect(desc).not.toMatch(/generate the phrase here/);
    expect(desc).not.toMatch(/return the phrase/);
  });
});
