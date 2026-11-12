/**
 * Tests for scripts/check-url-binding.cjs (internal#706).
 *
 * Run: node --test scripts/check-url-binding.test.cjs
 *
 * Synthetic artifact trees pin each check per profile; the source-tree cases
 * at the bottom run the real profiles against the files that ship, so a
 * renamed default-URL site fails CI instead of the next publish. No network.
 */

'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const { checkUrlBinding, parseArgs, stripComments, PROFILES } = require('./check-url-binding.cjs');

const REPO = path.resolve(__dirname, '..');
const SCRIPT = path.join(__dirname, 'check-url-binding.cjs');
const PROD = 'https://api.totalreclaw.xyz';
const STAGING = 'https://api-staging.totalreclaw.xyz';

function tree(files) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'url-binding-'));
  for (const [rel, content] of Object.entries(files)) {
    const f = path.join(root, rel);
    fs.mkdirSync(path.dirname(f), { recursive: true });
    fs.writeFileSync(f, content);
  }
  return root;
}

// Minimal "good" artifact per profile.
const GOOD = {
  nanoclaw: () => ({
    'dist/billing.js': `const serverUrl = process.env.TOTALRECLAW_SERVER_URL || '${PROD}';\n`,
    'dist/index.js': 'module.exports = {};\n',
    'mcp/nanoclaw-agent-runner.ts':
      '// Discover directories mounted at /workspace/extra/*\n' +
      `TOTALRECLAW_SERVER_URL: process.env.TOTALRECLAW_SERVER_URL || '${PROD}',\n`,
  }),
  client: () => ({
    'dist/index.js': 'class C { constructor(c) { this.serverUrl = c.serverUrl; } }\n',
  }),
  'core-npm': () => ({
    'nodejs/totalreclaw_core.js': 'module.exports.encrypt = () => {};\n',
    'nodejs/totalreclaw_core_bg.wasm': Buffer.from([0, 97, 115, 109, 1, 0, 0, 0]),
    'web/totalreclaw_core.js': 'export function encrypt() {}\n',
  }),
  'core-crate': () => ({
    'src/lib.rs': '//! Pure computation; no relay URL.\npub fn f() {}\n',
  }),
  'core-wheel': () => ({
    'totalreclaw_core/__init__.py': 'from .totalreclaw_core import *\n',
    'totalreclaw_core/totalreclaw_core.abi3.so': Buffer.from('\x7fELF fake binary'),
  }),
  'memory-crate': () => ({
    'src/backend.rs': `/// Default relay URL.\nconst DEFAULT_RELAY_URL: &str = "${PROD}";\n`,
    'src/relay.rs': '//! All communication with the relay at `api.totalreclaw.xyz`.\n',
  }),
  'python-client': () => ({
    'totalreclaw/relay.py': `_HARDCODED_DEFAULT_URL = "${PROD}"\n`,
    'totalreclaw/pair/remote_client.py': 'DEFAULT_RELAY_URL = "wss://api.totalreclaw.xyz"\n',
  }),
};

test('every profile has a good-tree fixture', () => {
  assert.deepEqual(Object.keys(GOOD).sort(), Object.keys(PROFILES).sort());
});

for (const name of Object.keys(PROFILES)) {
  const p = PROFILES[name];
  const codeFile = {
    js: Object.keys(GOOD[name]()).find((f) => f.endsWith('.js')),
    rust: 'src/extra.rs',
    python: `${p.include[0]}/extra.py`,
  }[p.lang];
  const quote = p.lang === 'rust' ? '"' : "'";

  test(`${name}: good artifact passes`, () => {
    const r = checkUrlBinding({ profile: name, root: tree(GOOD[name]()) });
    assert.equal(r.ok, true, r.errors.join('\n'));
  });

  test(`${name}: quoted staging URL in shipped code fails`, () => {
    const files = GOOD[name]();
    files[codeFile] = `${files[codeFile] || ''}\nconst u = ${quote}${STAGING}${quote};\n`;
    const r = checkUrlBinding({ profile: name, root: tree(files) });
    assert.equal(r.ok, false);
    assert.match(r.errors.join('\n'), /staging URL bound/);
  });

  test(`${name}: staging mentioned in a comment passes`, () => {
    const files = GOOD[name]();
    const comment = p.lang === 'python' ? '#' : '//';
    files[codeFile] = `${files[codeFile] || ''}\n${comment} QA pins ${quote}${STAGING}${quote} via the env var\n`;
    const r = checkUrlBinding({ profile: name, root: tree(files) });
    assert.equal(r.ok, true, r.errors.join('\n'));
  });

  test(`${name}: scheme-less staging host (banner detection) passes`, () => {
    const files = GOOD[name]();
    files[codeFile] = `${files[codeFile] || ''}\nconst isStaging = ${quote}api-staging.totalreclaw.xyz${quote};\n`;
    const r = checkUrlBinding({ profile: name, root: tree(files) });
    assert.equal(r.ok, true, r.errors.join('\n'));
  });

  test(`${name}: wrong layout fails`, () => {
    const r = checkUrlBinding({ profile: name, root: tree({ 'README.md': 'x' }) });
    assert.equal(r.ok, false);
    assert.match(r.errors.join('\n'), /not the expected artifact layout/);
  });

  if (p.noRelayUrls) {
    test(`${name}: any production relay URL fails (no default expected)`, () => {
      const files = GOOD[name]();
      files[codeFile] = `${files[codeFile] || ''}\nconst u = ${quote}${PROD}${quote};\n`;
      const r = checkUrlBinding({ profile: name, root: tree(files) });
      assert.equal(r.ok, false);
      assert.match(r.errors.join('\n'), /add it to the profile as a canonical site/);
    });
  }

  for (const site of p.sites) {
    test(`${name}: site ${site.file} missing fails`, () => {
      const files = GOOD[name]();
      delete files[site.file];
      const r = checkUrlBinding({ profile: name, root: tree(files) });
      assert.equal(r.ok, false);
      assert.match(r.errors.join('\n'), /file missing/);
    });

    test(`${name}: site ${site.file} rebound off production fails`, () => {
      const files = GOOD[name]();
      files[site.file] = String(files[site.file])
        .replace('https://api.totalreclaw.xyz', 'http://localhost:8080')
        .replace('wss://api.totalreclaw.xyz', 'ws://localhost:8080');
      const r = checkUrlBinding({ profile: name, root: tree(files) });
      assert.equal(r.ok, false);
      assert.match(r.errors.join('\n'), /lost the production default/);
    });
  }
}

test('binary containing the staging host fails', () => {
  const files = GOOD['core-wheel']();
  files['totalreclaw_core/totalreclaw_core.abi3.so'] = Buffer.from(`\x00\x01${STAGING}\x00`);
  const r = checkUrlBinding({ profile: 'core-wheel', root: tree(files) });
  assert.equal(r.ok, false);
  assert.match(r.errors.join('\n'), /binary contains api-staging/);
});

test('python-client: production commented out above a staging default fails', () => {
  const files = GOOD['python-client']();
  files['totalreclaw/relay.py'] = `# _HARDCODED_DEFAULT_URL = "${PROD}"\n_HARDCODED_DEFAULT_URL = "${STAGING}"\n`;
  const r = checkUrlBinding({ profile: 'python-client', root: tree(files) });
  assert.equal(r.ok, false);
  const msg = r.errors.join('\n');
  assert.match(msg, /staging URL bound/);
  assert.match(msg, /relay\.py \(no production default/);
});

test('nanoclaw: .d.ts declarations are not scanned', () => {
  const files = GOOD.nanoclaw();
  files['dist/types.d.ts'] = `export declare const x: '${STAGING}';\n`;
  const r = checkUrlBinding({ profile: 'nanoclaw', root: tree(files) });
  assert.equal(r.ok, true, r.errors.join('\n'));
});

// Comment stripping is a small tokenizer; these pin the cases a regex gets wrong.
test('stripComments: js line comment containing /* does not swallow code', () => {
  // The shape of skill-nanoclaw/mcp/nanoclaw-agent-runner.ts, which broke a
  // regex-based stripper (the site read as missing).
  const src = `// mounted at /workspace/extra/*\nconst u = env.X || '${PROD}';\n/* real block */\n`;
  const out = stripComments(src, 'js');
  assert.ok(out.includes(`'${PROD}'`));
  assert.ok(!out.includes('mounted at'));
  assert.ok(!out.includes('real block'));
  assert.equal(out.split('\n').length, src.split('\n').length, 'line numbers kept');
});

test('stripComments: js URL in a string is not a // comment; regex literal with quotes', () => {
  const src = `const a = '${STAGING}'; const re = /['"]/g; const b = '${PROD}'; // tail\n`;
  const out = stripComments(src, 'js');
  assert.ok(out.includes(`'${STAGING}'`));
  assert.ok(out.includes(`'${PROD}'`));
  assert.ok(!out.includes('tail'));
});

test('stripComments: rust char literal, lifetime, raw string, nested block comment', () => {
  const src = [
    "fn f<'a>(x: &'a str) -> char { '\"' }",
    `const R: &str = r#"${STAGING}"#;`,
    '/* outer /* inner */ still comment */',
    `const D: &str = "${PROD}";`,
  ].join('\n');
  const out = stripComments(src, 'rust');
  assert.ok(out.includes(`r#"${STAGING}"#`));
  assert.ok(out.includes(`"${PROD}"`));
  assert.ok(!out.includes('still comment'));
});

test('stripComments: python # inside strings and docstrings is not a comment', () => {
  const src = `"""Doc # not a comment."""\nx = "a # b"\ny = "${STAGING}"  # real comment\n`;
  const out = stripComments(src, 'python');
  assert.ok(out.includes('Doc # not a comment.'));
  assert.ok(out.includes(`"${STAGING}"`));
  assert.ok(!out.includes('real comment'));
});

test('a staging literal after a comment holding /* is still caught', () => {
  const files = GOOD.nanoclaw();
  files['dist/index.js'] = `// see /workspace/extra/*\nconst u = '${STAGING}';\n`;
  const r = checkUrlBinding({ profile: 'nanoclaw', root: tree(files) });
  assert.equal(r.ok, false);
  assert.match(r.errors.join('\n'), /dist\/index\.js:2/);
});

test('parseArgs: flags, defaults and errors', () => {
  const a = parseArgs(['--profile=client', '--root', '/tmp/x', '--release-type=rc'], {});
  assert.deepEqual(a, { profile: 'client', root: path.resolve('/tmp/x'), releaseType: 'rc' });
  assert.equal(parseArgs(['--profile', 'client', '--root=/x'], {}).releaseType, 'stable');
  assert.equal(
    parseArgs(['--profile=client', '--root=/x'], { TOTALRECLAW_RELEASE_TYPE: 'rc' }).releaseType,
    'rc',
  );
  assert.throws(() => parseArgs(['--root=/x'], {}), /--profile is required/);
  assert.throws(() => parseArgs(['--profile=client'], {}), /--root is required/);
  assert.throws(() => parseArgs(['--profile=nope', '--root=/x'], {}), /unknown --profile/);
  assert.throws(() => parseArgs(['--profile=client', '--root=/x', '--release-type=beta'], {}), /invalid/);
  assert.throws(() => parseArgs(['--bogus'], {}), /unknown argument/);
});

test('CLI exit codes: 0 ok, 1 violation, 2 usage', () => {
  const run = (...args) => spawnSync(process.execPath, [SCRIPT, ...args], { encoding: 'utf8' });
  const good = tree(GOOD.client());
  assert.equal(run('--profile=client', `--root=${good}`).status, 0);
  const bad = tree({ 'dist/index.js': `const u = '${STAGING}';\n` });
  const r = run('--profile=client', `--root=${bad}`, '--release-type=rc');
  assert.equal(r.status, 1);
  assert.match(r.stderr, /FAIL \(rc/);
  assert.equal(run('--profile=client').status, 2);
});

// Source trees whose layout matches the shipped artifact. These guard the
// real files: a renamed or rebound default-URL site fails here first.
const SOURCE_TREES = [
  ['python-client', 'python/src'],
  ['memory-crate', 'rust/totalreclaw-memory'],
  ['core-crate', 'rust/totalreclaw-core'],
];
for (const [name, dir] of SOURCE_TREES) {
  test(`source tree ${dir} passes the ${name} profile`, () => {
    const r = checkUrlBinding({ profile: name, root: path.join(REPO, dir) });
    assert.equal(r.ok, true, r.errors.join('\n'));
  });
}

test('NanoClaw default-URL sites exist in source', () => {
  // dist/billing.js is compiled from src/billing.ts; mcp/ ships as-is.
  for (const f of ['skill-nanoclaw/src/billing.ts', 'skill-nanoclaw/mcp/nanoclaw-agent-runner.ts']) {
    const src = fs.readFileSync(path.join(REPO, f), 'utf8');
    assert.ok(src.includes(`'${PROD}'`), `${f} lost its quoted production default`);
  }
});
