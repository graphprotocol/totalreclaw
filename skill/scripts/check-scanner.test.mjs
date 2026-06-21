import assert from 'node:assert';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const SCRIPT = join(HERE, 'check-scanner.mjs');

function runOn(files) {
  const dir = mkdtempSync(join(tmpdir(), 'scan-'));
  for (const [name, body] of Object.entries(files)) writeFileSync(join(dir, name), body);
  try {
    execFileSync('node', [SCRIPT, dir], { cwd: HERE });
    return { ok: true };
  } catch (e) { return { ok: false, stderr: e.stderr?.toString() ?? '' }; }
  finally { rmSync(dir, { recursive: true, force: true }); }
}

// CLEAN: env-only file + network-only file, separated
assert.ok(runOn({ 'a.ts': 'const U = process.env.X;', 'b.ts': 'await fetch(U);' }).ok, 'split files are clean');
// TRIP: one file co-contains env + fetch
assert.ok(!runOn({ 'bad.ts': 'const U = process.env.X;\nawait fetch(U);' }).ok, 'env+fetch co-occurrence trips');
// TRIP: env + fetch tokens in a COMMENT
assert.ok(!runOn({ 'cmt.ts': '// TODO: do not read process.env then fetch() here\nexport const x=1;' }).ok, 'comment tokens trip');
// TRIP: child_process
assert.ok(!runOn({ 'cp.ts': 'import {exec} from "child_process";' }).ok, 'child_process trips');
console.log('check-scanner.test OK');
