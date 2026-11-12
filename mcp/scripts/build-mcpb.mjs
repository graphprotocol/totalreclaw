#!/usr/bin/env node
/**
 * build-mcpb.mjs: build the TotalReclaw `.mcpb` bundle (Claude Desktop extension).
 *
 * Node built-ins plus `npm`/`npx` only. Works from a plain `git archive` export
 * with no devDependencies installed.
 *
 * Usage:
 *   node mcp/scripts/build-mcpb.mjs (--from-npm <spec> | --from-dir <path>) \
 *        [--platform host|darwin-arm64|darwin-x64|win32-x64|universal] --out <dir>
 *
 * Last line on stdout (JSON):
 *   {"bundle":"<abs path>","version":"...","platform":"...","bytes":N,"sha256":"..."}
 *
 * All progress goes to stderr. On failure: one line `build-mcpb: <reason>` on
 * stderr and a non-zero exit.
 */
import { spawnSync, spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import {
  cpSync, createReadStream, existsSync, mkdirSync, mkdtempSync, readdirSync,
  readFileSync, rmSync, statSync, writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const TEMPLATE_DIR = resolve(HERE, '..', 'mcpb');
const MCPB_CLI = '@anthropic-ai/mcpb@2.1.2'; // pinned
const STAGING_URL = 'https://api-staging.totalreclaw.xyz';
const PLATFORMS = ['host', 'darwin-arm64', 'darwin-x64', 'win32-x64', 'universal'];
const IS_WIN = process.platform === 'win32';

function log(msg) { process.stderr.write(`[build-mcpb] ${msg}\n`); }
function fail(msg) { throw new Error(msg); }

/** Recursive delete, only for a path strictly inside `root` (a dir this script created). */
function rmInside(root, target) {
  const r = resolve(root);
  const t = resolve(target);
  if (t === r || !t.startsWith(r + sep)) fail(`refusing to delete ${t}: not inside ${r}`);
  rmSync(t, { recursive: true, force: true });
}

function parseArgs(argv) {
  const o = { platform: 'host' };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const val = () => {
      if (i + 1 >= argv.length) fail(`${a} needs a value`);
      return argv[++i];
    };
    if (a === '--from-npm') o.fromNpm = val();
    else if (a === '--from-dir') o.fromDir = val();
    else if (a === '--platform') o.platform = val();
    else if (a === '--out') o.out = val();
    else fail(`unknown argument ${a}`);
  }
  if (!!o.fromNpm === !!o.fromDir) fail('give exactly one of --from-npm <spec> or --from-dir <path>');
  if (!o.out) fail('--out <dir> is required');
  if (!PLATFORMS.includes(o.platform)) fail(`--platform must be one of ${PLATFORMS.join('|')}`);
  return o;
}

/** Run a command, forwarding its output to stderr. Throws on non-zero exit. */
function run(cmd, args, opts = {}) {
  const r = spawnSync(cmd, args, {
    stdio: ['ignore', 2, 2],
    shell: IS_WIN,
    ...opts,
  });
  if (r.error) fail(`${cmd} failed to start: ${r.error.message}`);
  if (r.status !== 0) fail(`${cmd} ${args[0] ?? ''} exited with status ${r.status}`);
}

function resolvePlatform(p) {
  if (p === 'host') {
    const os = process.platform;
    const cpu = process.arch;
    return { label: `${os}-${cpu}`, os, cpu, universal: false };
  }
  if (p === 'universal') return { label: 'universal', os: null, cpu: null, universal: true };
  const [os, cpu] = p.split('-');
  return { label: p, os, cpu, universal: false };
}

function stageServer(opts, work) {
  const server = join(work, 'stage', 'server');
  mkdirSync(server, { recursive: true });
  let srcRoot;
  if (opts.fromNpm) {
    log(`npm pack ${opts.fromNpm}`);
    const packDir = join(work, 'pack');
    mkdirSync(packDir);
    run('npm', ['pack', opts.fromNpm, '--pack-destination', packDir, '--silent', '--no-audit']);
    const tgz = readdirSync(packDir).find((f) => f.endsWith('.tgz'));
    if (!tgz) fail(`npm pack produced no tarball for ${opts.fromNpm}`);
    const ex = join(work, 'extract');
    mkdirSync(ex);
    run('tar', ['-xzf', join(packDir, tgz), '-C', ex]);
    srcRoot = join(ex, 'package');
  } else {
    srcRoot = resolve(opts.fromDir);
  }
  if (!existsSync(join(srcRoot, 'package.json'))) fail(`no package.json in ${srcRoot}`);
  if (!existsSync(join(srcRoot, 'dist', 'index.js'))) {
    fail(`no dist/index.js in ${srcRoot} (run \`npm run build\` first)`);
  }
  cpSync(join(srcRoot, 'dist'), join(server, 'dist'), { recursive: true });
  const pkg = JSON.parse(readFileSync(join(srcRoot, 'package.json'), 'utf8'));
  // Minimal package.json: runtime fields only, so `npm install` runs no root lifecycle scripts.
  const slim = {
    name: pkg.name,
    version: pkg.version,
    description: pkg.description,
    main: pkg.main,
    license: pkg.license,
    dependencies: pkg.dependencies,
    overrides: pkg.overrides,
  };
  if (pkg.type) slim.type = pkg.type;
  writeFileSync(join(server, 'package.json'), JSON.stringify(slim, null, 2) + '\n');
  return { server, version: pkg.version };
}

function installDeps(server, plat) {
  const args = [
    'install', '--omit=dev', '--ignore-scripts', '--no-audit', '--no-fund',
    '--legacy-peer-deps', '--no-package-lock',
  ];
  if (!plat.universal) args.push(`--os=${plat.os}`, `--cpu=${plat.cpu}`);
  log(`npm ${args.join(' ')}`);
  run('npm', args, { cwd: server, env: { ...process.env, ONNXRUNTIME_NODE_INSTALL_CUDA: 'skip' } });
}

function dirBytes(p) {
  let n = 0;
  for (const e of readdirSync(p, { withFileTypes: true })) {
    const f = join(p, e.name);
    n += e.isDirectory() ? dirBytes(f) : statSync(f).size;
  }
  return n;
}

function pruneNative(server, plat) {
  const nm = join(server, 'node_modules');
  // onnxruntime-web is bundled inline into transformers' node build and never
  // required at runtime in Node; it is ~140 MB of wasm.
  rmInside(server, join(nm, 'onnxruntime-web'));
  const bin = join(nm, 'onnxruntime-node', 'bin');
  if (!existsSync(bin)) {
    log('WARNING: onnxruntime-node/bin not found; skipping native prune');
    return;
  }
  if (plat.universal) return;
  for (const napi of readdirSync(bin)) {
    const napiDir = join(bin, napi);
    for (const os of readdirSync(napiDir)) {
      const osDir = join(napiDir, os);
      if (os !== plat.os) { rmInside(server, osDir); continue; }
      for (const cpu of readdirSync(osDir)) {
        if (cpu !== plat.cpu) rmInside(server, join(osDir, cpu));
      }
    }
  }
  const kept = join(bin, 'napi-v6', plat.os, plat.cpu);
  if (!existsSync(kept)) {
    log(`WARNING: onnxruntime-node ships no ${plat.os}-${plat.cpu} binary; local embeddings will not load in this bundle`);
  }
}

/** Start the staged server over stdio and read tools/list. Unconfigured, so no relay call. */
function listTools(server) {
  return new Promise((resolveP, rejectP) => {
    const fakeHome = mkdtempSync(join(tmpdir(), 'tr-mcpb-home-'));
    const child = spawn(process.execPath, [join(server, 'dist', 'index.js')], {
      cwd: server,
      env: {
        PATH: process.env.PATH,
        HOME: fakeHome,
        USERPROFILE: fakeHome,
        TOTALRECLAW_SERVER_URL: STAGING_URL,
      },
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    let buf = '';
    let stderr = '';
    let done = false;
    const finish = (err, tools) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      child.kill();
      rmInside(tmpdir(), fakeHome); // mkdtemp dir created above
      err ? rejectP(err) : resolveP(tools);
    };
    const timer = setTimeout(() => finish(new Error(`tools/list timed out. stderr: ${stderr.slice(-300)}`)), 60000);
    child.stderr.on('data', (d) => { stderr += d; });
    child.on('error', (e) => finish(e));
    child.on('exit', (code) => finish(new Error(`server exited early (code ${code}). stderr: ${stderr.slice(-300)}`)));
    child.stdout.on('data', (d) => {
      buf += d;
      let nl;
      while ((nl = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line) continue;
        let msg;
        try { msg = JSON.parse(line); } catch { continue; }
        if (msg.id === 1) {
          child.stdin.write(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) + '\n');
          child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'tools/list', params: {} }) + '\n');
        } else if (msg.id === 2) {
          finish(null, (msg.result?.tools ?? []).map((t) => ({ name: t.name, description: t.description ?? '' })));
        }
      }
    });
    child.stdin.write(JSON.stringify({
      jsonrpc: '2.0', id: 1, method: 'initialize',
      params: { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'build-mcpb', version: '1' } },
    }) + '\n');
  });
}

function sha256File(p) {
  return new Promise((res, rej) => {
    const h = createHash('sha256');
    createReadStream(p).on('data', (d) => h.update(d)).on('end', () => res(h.digest('hex'))).on('error', rej);
  });
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  const plat = resolvePlatform(opts.platform);
  const out = resolve(opts.out);
  mkdirSync(out, { recursive: true });
  const work = mkdtempSync(join(tmpdir(), 'tr-mcpb-build-'));
  try {
    const { server, version } = stageServer(opts, work);
    const stage = join(work, 'stage');
    // The server loads onnxruntime-node at startup, so tools/list needs a tree that
    // matches the machine running this script. For a cross-platform target, build
    // a throwaway host-platform copy just to read the tool list.
    const cross = !plat.universal && (plat.os !== process.platform || plat.cpu !== process.arch);
    let toolsServer = server;
    if (cross) {
      toolsServer = join(work, 'tools-host', 'server');
      cpSync(server, toolsServer, { recursive: true });
      log(`cross-platform target ${plat.label}: installing a host copy to read tools/list`);
      installDeps(toolsServer, { universal: true });
    }
    installDeps(server, plat);
    pruneNative(server, plat);
    log(`server tree: ${(dirBytes(server) / 1048576).toFixed(0)} MB after prune`);

    const manifest = JSON.parse(readFileSync(join(TEMPLATE_DIR, 'manifest.json'), 'utf8'));
    manifest.version = version;
    log('generating tools list from the staged server (staging URL, temp HOME)');
    const tools = await listTools(toolsServer);
    if (tools.length === 0) fail('staged server returned an empty tools/list');
    manifest.tools = tools;
    if (!manifest.privacy_policies) {
      log('TODO: manifest has no privacy_policies URL; add a real one before submitting to any directory (not invented here)');
    }
    cpSync(join(TEMPLATE_DIR, 'icon.png'), join(stage, 'icon.png'));
    writeFileSync(join(stage, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n');

    log('mcpb validate');
    run('npx', ['-y', MCPB_CLI, 'validate', join(stage, 'manifest.json')]);

    const bundle = join(out, `totalreclaw-${version}-${plat.label}.mcpb`);
    rmSync(bundle, { force: true });
    log('mcpb pack');
    run('npx', ['-y', MCPB_CLI, 'pack', stage, bundle]);
    if (!existsSync(bundle)) fail('mcpb pack produced no file');

    const bytes = statSync(bundle).size;
    const sha256 = await sha256File(bundle);
    process.stdout.write(JSON.stringify({ bundle, version, platform: plat.label, bytes, sha256 }) + '\n');
  } finally {
    rmInside(tmpdir(), work); // mkdtemp dir created above
  }
}

main().catch((e) => {
  process.stderr.write(`build-mcpb: ${String(e.message ?? e).split('\n')[0]}\n`);
  process.exit(1);
});
