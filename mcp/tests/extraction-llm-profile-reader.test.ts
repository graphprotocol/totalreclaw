/**
 * Tests for `src/extraction/llm-profile-reader.ts` — port of the plugin's
 * tap-style test into a jest-shaped subset. Covers the high-value paths:
 *   - parseAuthProfilesFile shape acceptance + malformed-input safety
 *   - provider-namespace aliasing (google → gemini, z.ai → zai)
 *   - findAuthProfilesFiles walks agent subdirectories
 *   - parseModelsJsonFile (legacy fallback) accepts apiKey / api_key / key
 *   - readAllProfileKeys merges auth-profiles + models.json without overlap
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import {
  parseAuthProfilesFile,
  findAuthProfilesFiles,
  readAllProfileKeys,
  parseModelsJsonFile,
  findModelsJsonFiles,
  defaultAuthProfilesRoot,
} from '../src/extraction/llm-profile-reader.js';

function mkTmp(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'tr-mcp-authprof-'));
}

describe('extraction/llm-profile-reader', () => {
  it('parseAuthProfilesFile maps :default profile keys to canonical providers', () => {
    const tmp = mkTmp();
    const file = path.join(tmp, 'auth-profiles.json');
    fs.writeFileSync(file, JSON.stringify({
      profiles: {
        'openai:default': { key: 'sk-openai-1' },
        'anthropic:default': { key: 'sk-ant-1' },
        'z.ai:default': { key: 'zai-key-1' },
        'google:default': { key: 'goog-key-1' },
        'openai:work': { key: 'sk-openai-2' }, // non-default, must be skipped
        'unknown-ns:default': { key: 'irrelevant' },
        'openai:default-empty': { key: '' },
      },
    }));
    const keys = parseAuthProfilesFile(file);
    const byProvider = Object.fromEntries(keys.map((k) => [k.provider, k.apiKey]));
    expect(byProvider.openai).toBe('sk-openai-1');
    expect(byProvider.anthropic).toBe('sk-ant-1');
    expect(byProvider.zai).toBe('zai-key-1');
    expect(byProvider.gemini).toBe('goog-key-1');
    // Non-default + unknown-ns + empty are dropped.
    expect(Object.keys(byProvider).sort()).toEqual(['anthropic', 'gemini', 'openai', 'zai']);
  });

  it('parseAuthProfilesFile returns [] on malformed JSON / missing profiles', () => {
    const tmp = mkTmp();
    const f1 = path.join(tmp, 'broken.json');
    fs.writeFileSync(f1, '{ not json');
    expect(parseAuthProfilesFile(f1)).toEqual([]);

    const f2 = path.join(tmp, 'no-profiles.json');
    fs.writeFileSync(f2, JSON.stringify({ stuff: 1 }));
    expect(parseAuthProfilesFile(f2)).toEqual([]);
  });

  it('findAuthProfilesFiles walks agent subdirectories', () => {
    const tmp = mkTmp();
    const root = path.join(tmp, 'agents');
    fs.mkdirSync(path.join(root, 'agent-a', 'agent'), { recursive: true });
    fs.mkdirSync(path.join(root, 'agent-b', 'agent'), { recursive: true });
    fs.mkdirSync(path.join(root, '.hidden', 'agent'), { recursive: true });
    fs.writeFileSync(path.join(root, 'agent-a', 'agent', 'auth-profiles.json'), '{}');
    fs.writeFileSync(path.join(root, 'agent-b', 'agent', 'auth-profiles.json'), '{}');
    fs.writeFileSync(path.join(root, '.hidden', 'agent', 'auth-profiles.json'), '{}');

    const files = findAuthProfilesFiles(root);
    expect(files).toHaveLength(2);
    expect(files.every((f) => f.includes('agent-a') || f.includes('agent-b'))).toBe(true);
  });

  it('parseModelsJsonFile accepts apiKey / api_key / key field variants', () => {
    const tmp = mkTmp();
    const file = path.join(tmp, 'models.json');
    fs.writeFileSync(file, JSON.stringify({
      providers: {
        zai: { apiKey: 'zai-1' },
        openai: { api_key: 'openai-1' },
        anthropic: { key: 'ant-1' },
        unknown: { apiKey: 'should-skip' },
      },
    }));
    const keys = parseModelsJsonFile(file);
    const map = Object.fromEntries(keys.map((k) => [k.provider, k.apiKey]));
    expect(map.zai).toBe('zai-1');
    expect(map.openai).toBe('openai-1');
    expect(map.anthropic).toBe('ant-1');
    expect(Object.keys(map).sort()).toEqual(['anthropic', 'openai', 'zai']);
  });

  it('findModelsJsonFiles walks agent subdirectories', () => {
    const tmp = mkTmp();
    const root = path.join(tmp, 'agents');
    fs.mkdirSync(path.join(root, 'a', 'agent'), { recursive: true });
    fs.writeFileSync(path.join(root, 'a', 'agent', 'models.json'), '{}');
    expect(findModelsJsonFiles(root)).toHaveLength(1);
  });

  it('readAllProfileKeys: auth-profiles takes precedence; models.json fills gaps', () => {
    const tmp = mkTmp();
    const root = path.join(tmp, 'agents');
    fs.mkdirSync(path.join(root, 'a', 'agent'), { recursive: true });
    fs.writeFileSync(
      path.join(root, 'a', 'agent', 'auth-profiles.json'),
      JSON.stringify({ profiles: { 'openai:default': { key: 'authprof-openai' } } }),
    );
    fs.writeFileSync(
      path.join(root, 'a', 'agent', 'models.json'),
      JSON.stringify({
        providers: {
          openai: { apiKey: 'modelsjson-openai-LOSES' },
          anthropic: { apiKey: 'modelsjson-ant-WINS' },
        },
      }),
    );
    const keys = readAllProfileKeys({ root });
    const byProvider = Object.fromEntries(keys.map((k) => [k.provider, k.apiKey]));
    expect(byProvider.openai).toBe('authprof-openai');
    expect(byProvider.anthropic).toBe('modelsjson-ant-WINS');
  });

  it('defaultAuthProfilesRoot handles undefined HOME safely', () => {
    expect(defaultAuthProfilesRoot(undefined)).toBe('');
    expect(defaultAuthProfilesRoot('/tmp/example')).toBe('/tmp/example/.openclaw/agents');
  });
});
