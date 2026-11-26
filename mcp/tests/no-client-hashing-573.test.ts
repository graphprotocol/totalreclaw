/**
 * internal#573 (M6): CI grep gate. Nothing in `mcp/src` may hash a word, stem,
 * entity or digest marker, or call a legacy unkeyed core function. No
 * allowlist inside `src`. Comments and docstrings are scanned on purpose: a
 * comment that names a removed helper invites someone to bring it back.
 *
 * Not covered, by design: MCP self-hosted (HTTP) mode. Its hashing lives in
 * the npm dependency `@totalreclaw/client`, outside `mcp/src`.
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const SRC = path.join(__dirname, '..', 'src');

const FORBIDDEN: Array<[string, RegExp]> = [
  ['generateBlindIndices', /generateBlindIndices/],
  ['computeEntityTrapdoor(s)', /computeEntityTrapdoor/],
  ['DIGEST_TRAPDOOR', /DIGEST_TRAPDOOR/],
  ['generateContentFingerprint( (V2 is fine)', /generateContentFingerprint\(/],
  ['generateSearchTrapdoors( (Keyed is fine)', /generateSearchTrapdoors\(/],
  ['generateExpansionTrapdoors', /generateExpansionTrapdoors/],
  ['prepareFact', /prepareFact/],
  ['encodeFactProtobuf( (Keyed( is fine)', /encodeFactProtobuf\(/],
  ['inner.hash(', /inner\.hash\(/],
  ['.hash(new Float64Array', /\.hash\(new Float64Array/],
  ["createHash('sha256')", /createHash\(\s*['"]sha256['"]\s*\)/],
  ["'entity:' / \"entity:\"", /['"]entity:['"]/],
  ["'stem:'", /['"]stem:['"]/],
  ['type:digest', /type:digest/],
];

function listTs(dir: string): string[] {
  const out: string[] = [];
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, e.name);
    if (e.isDirectory()) out.push(...listTs(full));
    else if (e.name.endsWith('.ts') && !e.name.endsWith('.d.ts')) out.push(full);
  }
  return out;
}

function scanFiles(files: string[]): string[] {
  const hits: string[] = [];
  for (const f of files) {
    const lines = fs.readFileSync(f, 'utf8').split('\n');
    lines.forEach((line, i) => {
      for (const [label, re] of FORBIDDEN) {
        if (re.test(line)) hits.push(`${path.relative(SRC, f)}:${i + 1} [${label}]`);
      }
    });
  }
  return hits;
}

describe('no client-side hashing in mcp/src (grep gate, D8)', () => {
  test('scans a real tree', () => {
    expect(listTs(SRC).length).toBeGreaterThan(40);
  });

  test('no forbidden call, constant or literal anywhere in mcp/src, comments included', () => {
    expect(scanFiles(listTs(SRC))).toEqual([]);
  });

  test('positive control: the same scan hits every forbidden pattern in a temp file', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'tr-gate-'));
    try {
      const samples = [
        'generateBlindIndices(x)', 'computeEntityTrapdoors(x)', 'const DIGEST_TRAPDOOR = 1', 'generateContentFingerprint(a, b)',
        'generateSearchTrapdoors(q)', 'generateExpansionTrapdoors(q)', 'prepareFact(x)', 'encodeFactProtobuf(x)',
        'this.inner.hash(e)', 'h.hash(new Float64Array(e))', "createHash('sha256')", 'createHash("sha256")',
        "'entity:' + n", '"entity:" + n', "'stem:' + s", '// type:digest',
      ];
      const file = path.join(dir, 'probe.ts');
      fs.writeFileSync(file, samples.join('\n') + '\n');
      const hits = scanFiles([file]);
      for (const [label, re] of FORBIDDEN) {
        expect([label, samples.some((s) => re.test(s))]).toEqual([label, true]);
      }
      expect(hits.length).toBeGreaterThanOrEqual(samples.length);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  test('the allowed keyed / V2 forms do not trip the gate', () => {
    const ok = ['generateContentFingerprintV2(a, b)', 'generateSearchTrapdoorsKeyed(q)', 'encodeFactProtobufKeyed(x)', "hkdfSync('sha256', a, b, c, 32)", 'createHmac("sha256", k)'];
    for (const line of ok) {
      for (const [, re] of FORBIDDEN) expect(re.test(line)).toBe(false);
    }
  });
});
