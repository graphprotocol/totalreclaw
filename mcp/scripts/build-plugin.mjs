#!/usr/bin/env node
/**
 * build-plugin.mjs: assemble an installable TotalReclaw Claude plugin directory.
 *
 * Usage:
 *   node mcp/scripts/build-plugin.mjs --bundle <path.mcpb> --out <dir> [--entry mcpb|npx]
 *
 * Copies `plugins/totalreclaw/` to `<out>/totalreclaw/`, sets the plugin version
 * from the bundle's manifest, and wires the server entry:
 *   mcpb (default): puts the bundle at `totalreclaw.mcpb` and points
 *                   `mcpServers` in plugin.json at it (drops the npx .mcp.json)
 *   npx:            keeps the source `.mcp.json` (`npx -y @totalreclaw/mcp-server`)
 *
 * Node built-ins only; works from a plain `git archive` export.
 *
 * Last line on stdout (JSON):
 *   {"plugin_dir":"<abs path>","server_entry":"mcpb|node|npx","server_name":"totalreclaw"}
 *
 * On failure: one line `build-plugin: <reason>` on stderr and a non-zero exit.
 */
import {
  cpSync, existsSync, lstatSync, mkdirSync, readdirSync, readFileSync, realpathSync, rmSync, writeFileSync,
} from 'node:fs';
import { basename, dirname, join, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { inflateRawSync } from 'node:zlib';

const HERE = dirname(fileURLToPath(import.meta.url));
const PLUGIN_SRC = resolve(HERE, '..', '..', 'plugins', 'totalreclaw');

const MARKER = '.totalreclaw-build';

function fail(msg) { throw new Error(msg); }

/**
 * Real path of `p`, which may not exist yet: realpath of the nearest existing
 * ancestor plus the remaining segments. Defeats symlinked or /var vs
 * /private/var spellings so the overlap check compares like with like.
 */
function realish(p) {
  let cur = resolve(p);
  const rest = [];
  for (;;) {
    try {
      return rest.length ? join(realpathSync(cur), ...rest.reverse()) : realpathSync(cur);
    } catch {
      const parent = dirname(cur);
      if (parent === cur) return resolve(p);
      rest.push(basename(cur));
      cur = parent;
    }
  }
}

function contains(a, b) { return b === a || b.startsWith(a.endsWith(sep) ? a : a + sep); }

/**
 * Validate the output dir before any recursive delete. Refuses when it
 * overlaps the plugin source, or when it exists and is neither empty nor
 * carries the marker this script wrote on a previous build.
 */
function checkDest(dest) {
  const realSrc = realish(PLUGIN_SRC);
  const realDest = realish(dest);
  if (contains(realSrc, realDest) || contains(realDest, realSrc)) {
    fail(`refusing: output ${dest} (real path ${realDest}) overlaps the plugin source ${realSrc}`);
  }
  if (!existsSync(dest)) return;
  const st = lstatSync(dest);
  if (!st.isDirectory()) fail(`refusing: ${dest} exists and is not a directory`);
  if (readdirSync(dest).length === 0) return;
  if (!existsSync(join(dest, MARKER))) {
    fail(`refusing: ${dest} exists, is not empty and was not created by build-plugin (no ${MARKER} marker)`);
  }
}

function parseArgs(argv) {
  const o = { entry: 'mcpb' };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const val = () => {
      if (i + 1 >= argv.length) fail(`${a} needs a value`);
      return argv[++i];
    };
    if (a === '--bundle') o.bundle = val();
    else if (a === '--out') o.out = val();
    else if (a === '--entry') o.entry = val();
    else fail(`unknown argument ${a}`);
  }
  if (!o.out) fail('--out <dir> is required');
  if (!['mcpb', 'npx'].includes(o.entry)) fail('--entry must be mcpb or npx');
  if (o.entry === 'mcpb' && !o.bundle) fail('--bundle <path.mcpb> is required');
  if (o.entry === 'npx' && !o.bundle) fail('--bundle is required (its manifest supplies the version to pin)');
  return o;
}

/** Read one file out of a zip (central directory + raw deflate), no dependencies. */
function readZipEntry(buf, name) {
  let eocd = -1;
  for (let i = buf.length - 22; i >= Math.max(0, buf.length - 65557); i--) {
    if (buf.readUInt32LE(i) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd < 0) fail('not a zip file (no end-of-central-directory record)');
  const count = buf.readUInt16LE(eocd + 10);
  let p = buf.readUInt32LE(eocd + 16);
  for (let n = 0; n < count; n++) {
    if (buf.readUInt32LE(p) !== 0x02014b50) fail('corrupt zip central directory');
    const method = buf.readUInt16LE(p + 10);
    const csize = buf.readUInt32LE(p + 20);
    const nlen = buf.readUInt16LE(p + 28);
    const xlen = buf.readUInt16LE(p + 30);
    const clen = buf.readUInt16LE(p + 32);
    const lho = buf.readUInt32LE(p + 42);
    const fname = buf.toString('utf8', p + 46, p + 46 + nlen);
    if (fname === name) {
      const lnlen = buf.readUInt16LE(lho + 26);
      const lxlen = buf.readUInt16LE(lho + 28);
      const start = lho + 30 + lnlen + lxlen;
      const data = buf.subarray(start, start + csize);
      return method === 0 ? data : inflateRawSync(data);
    }
    p += 46 + nlen + xlen + clen;
  }
  return null;
}

function main() {
  const opts = parseArgs(process.argv.slice(2));
  if (!existsSync(join(PLUGIN_SRC, '.claude-plugin', 'plugin.json'))) {
    fail(`plugin source not found at ${PLUGIN_SRC}`);
  }
  let version = null;
  let bundleBuf = null;
  if (opts.bundle) {
    const bundlePath = resolve(opts.bundle);
    if (!existsSync(bundlePath)) fail(`bundle not found: ${bundlePath}`);
    bundleBuf = readFileSync(bundlePath);
    const mf = readZipEntry(bundleBuf, 'manifest.json');
    if (!mf) fail('bundle has no manifest.json');
    const manifest = JSON.parse(mf.toString('utf8'));
    if (manifest.name !== 'totalreclaw') fail(`bundle manifest name is "${manifest.name}", expected "totalreclaw"`);
    version = manifest.version;
  }

  const dest = join(resolve(opts.out), 'totalreclaw');
  checkDest(dest);
  // Safe: dest is outside the plugin source and is empty or marked as our own previous output.
  if (existsSync(dest)) rmSync(dest, { recursive: true, force: true });
  mkdirSync(dirname(dest), { recursive: true });
  cpSync(PLUGIN_SRC, dest, { recursive: true });
  writeFileSync(join(dest, MARKER), 'written by mcp/scripts/build-plugin.mjs; safe to replace\n');

  const pjPath = join(dest, '.claude-plugin', 'plugin.json');
  const pj = JSON.parse(readFileSync(pjPath, 'utf8'));
  if (version) pj.version = version;
  if (opts.entry === 'mcpb') {
    writeFileSync(join(dest, 'totalreclaw.mcpb'), bundleBuf);
    pj.mcpServers = './totalreclaw.mcpb';
    rmSync(join(dest, '.mcp.json'), { force: true }); // single file we just copied
  } else if (version) {
    // npx entry: pin to the bundle's version
    const mcpPath = join(dest, '.mcp.json');
    const mcp = JSON.parse(readFileSync(mcpPath, 'utf8'));
    mcp.mcpServers.totalreclaw.args = ['-y', `@totalreclaw/mcp-server@${version}`];
    writeFileSync(mcpPath, JSON.stringify(mcp, null, 2) + '\n');
  }
  writeFileSync(pjPath, JSON.stringify(pj, null, 2) + '\n');

  process.stdout.write(JSON.stringify({
    plugin_dir: dest, server_entry: opts.entry, server_name: 'totalreclaw',
  }) + '\n');
}

try {
  main();
} catch (e) {
  process.stderr.write(`build-plugin: ${String(e.message ?? e).split('\n')[0]}\n`);
  process.exit(1);
}
