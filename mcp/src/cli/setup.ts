/**
 * TotalReclaw MCP Setup CLI
 *
 * Interactive setup wizard for new users. Guides through:
 *   1. Mnemonic creation or import (BIP-39 12-word)
 *   2. Key derivation (HKDF-SHA256 -- identical to plugin/crypto.ts)
 *   3. Server registration
 *   4. Credential persistence (~/.totalreclaw/credentials.json)
 *   5. MCP config snippet for Claude Desktop / Cursor / VS Code
 *
 * Key derivation chain (BIP-39 path, must match plugin/crypto.ts exactly):
 *   mnemonic -> mnemonicToSeedSync() -> 512-bit seed
 *   salt = seed[0..32]
 *   authKey = HKDF-SHA256(seed, salt, "totalreclaw-auth-key-v1", 32)
 *   authKeyHash = SHA-256(authKey).hex()
 */

import * as readline from 'node:readline';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';

import { generateMnemonic, validateMnemonic, mnemonicToSeedSync } from '@scure/bip39';
import { wordlist } from '@scure/bip39/wordlists/english.js';
import { hkdf } from '@noble/hashes/hkdf.js';
import { sha256 } from '@noble/hashes/sha2.js';

// ---------------------------------------------------------------------------
// Constants -- must match plugin/crypto.ts
// ---------------------------------------------------------------------------

const AUTH_KEY_INFO = 'totalreclaw-auth-key-v1';

const DEFAULT_SERVER_URL = 'https://api.totalreclaw.xyz';
const CREDENTIALS_DIR = path.join(os.homedir(), '.totalreclaw');
const CREDENTIALS_PATH = path.join(CREDENTIALS_DIR, 'credentials.json');

// ---------------------------------------------------------------------------
// Key Derivation (BIP-39 path only -- matches plugin/crypto.ts)
// ---------------------------------------------------------------------------

/**
 * Derive the auth key and salt from a BIP-39 mnemonic.
 *
 * This is the exact same derivation as `deriveKeysFromMnemonic` in
 * `skill/plugin/crypto.ts`:
 *   - BIP-39 seed via mnemonicToSeedSync (512 bits)
 *   - salt = first 32 bytes of seed
 *   - authKey = HKDF-SHA256(seed, salt, "totalreclaw-auth-key-v1", 32)
 */
export function deriveAuthKey(mnemonic: string): { authKey: Uint8Array; salt: Uint8Array } {
  const seed = mnemonicToSeedSync(mnemonic.trim());

  // Deterministic salt: first 32 bytes of BIP-39 seed
  const salt = seed.slice(0, 32);

  // HKDF-SHA256 with the full 512-bit seed as IKM
  const infoBytes = new TextEncoder().encode(AUTH_KEY_INFO);
  const authKey = hkdf(sha256, seed, salt, infoBytes, 32);

  return { authKey, salt };
}

/**
 * Compute SHA-256(authKey) as a hex string.
 * Matches `computeAuthKeyHash` in plugin/crypto.ts.
 */
export function computeAuthKeyHash(authKey: Uint8Array): string {
  const hash = sha256(authKey);
  return Buffer.from(hash).toString('hex');
}

// ---------------------------------------------------------------------------
// Readline helpers
// ---------------------------------------------------------------------------

function createInterface(): readline.Interface {
  return readline.createInterface({
    input: process.stdin,
    output: process.stdout,
  });
}

function ask(rl: readline.Interface, question: string): Promise<string> {
  return new Promise((resolve) => {
    rl.question(question, (answer) => {
      resolve(answer.trim());
    });
  });
}

// ---------------------------------------------------------------------------
// Server Registration
// ---------------------------------------------------------------------------

interface RegisterResponse {
  user_id: string;
}

async function registerWithServer(
  serverUrl: string,
  authKeyHash: string,
  saltHex: string,
): Promise<string> {
  const url = `${serverUrl.replace(/\/+$/, '')}/v1/register`;

  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      auth_key_hash: authKeyHash,
      salt: saltHex,
    }),
  });

  if (!response.ok) {
    const body = await response.text().catch(() => '');
    throw new Error(
      `Registration failed (HTTP ${response.status}): ${body || response.statusText}`,
    );
  }

  const data = (await response.json()) as RegisterResponse;
  if (!data.user_id) {
    throw new Error('Registration response missing user_id');
  }

  return data.user_id;
}

// ---------------------------------------------------------------------------
// Credential Persistence
// ---------------------------------------------------------------------------

export interface SavedCredentials {
  userId: string;
  salt: string; // hex
  serverUrl: string;
}

export function saveCredentials(credentials: SavedCredentials, filePath?: string): void {
  const targetPath = filePath || CREDENTIALS_PATH;
  const dir = path.dirname(targetPath);

  if (!fs.existsSync(dir)) {
    fs.mkdirSync(dir, { recursive: true });
  }

  fs.writeFileSync(targetPath, JSON.stringify(credentials, null, 2) + '\n', {
    encoding: 'utf-8',
    mode: 0o600, // Owner read/write only
  });
}

export function loadCredentials(filePath?: string): SavedCredentials {
  const targetPath = filePath || CREDENTIALS_PATH;
  const data = fs.readFileSync(targetPath, 'utf-8');
  return JSON.parse(data) as SavedCredentials;
}

// ---------------------------------------------------------------------------
// Config Snippet
// ---------------------------------------------------------------------------

function printConfigSnippet(serverUrl: string): void {
  const snippet = {
    mcpServers: {
      totalreclaw: {
        command: 'npx',
        args: ['@totalreclaw/mcp-server'],
        env: {
          TOTALRECLAW_MASTER_PASSWORD: '<your-12-word-seed-phrase>',
          TOTALRECLAW_SERVER_URL: serverUrl,
        },
      },
    },
  };

  console.log('\n========================================');
  console.log('Add this to your MCP config');
  console.log('(Claude Desktop / Cursor / VS Code):');
  console.log('========================================\n');
  console.log(JSON.stringify(snippet, null, 2));
  console.log('\nReplace <your-12-word-seed-phrase> with your actual seed phrase.');
  console.log('');
}

// ---------------------------------------------------------------------------
// Main Setup Flow
// ---------------------------------------------------------------------------

export async function runSetup(): Promise<void> {
  const rl = createInterface();

  console.log('');
  console.log('=== TotalReclaw Setup ===');
  console.log('End-to-end encrypted memory for AI agents.');
  console.log('');

  try {
    // ── Step 1: Mnemonic ──────────────────────────────────────────────────

    let mnemonic: string;

    const hasExisting = await ask(rl, 'Do you have an existing seed phrase? (y/n): ');

    if (hasExisting.toLowerCase() === 'y' || hasExisting.toLowerCase() === 'yes') {
      // Import existing mnemonic
      console.log('');
      console.log('Enter your 12-word seed phrase (space-separated):');
      mnemonic = await ask(rl, '> ');

      if (!validateMnemonic(mnemonic, wordlist)) {
        console.error('\nError: Invalid mnemonic. Please check your words and try again.');
        rl.close();
        process.exit(1);
      }

      console.log('\nSeed phrase validated successfully.');
    } else {
      // Generate new mnemonic
      mnemonic = generateMnemonic(wordlist, 128); // 128 bits = 12 words

      console.log('');
      console.log('Your new seed phrase (WRITE THIS DOWN AND KEEP IT SAFE):');
      console.log('');
      console.log(`  ${mnemonic}`);
      console.log('');
      console.log('WARNING: This is the ONLY way to recover your encrypted memories.');
      console.log('         Anyone with this phrase can access your data.');
      console.log('         Store it in a password manager or write it down securely.');
      console.log('');

      const confirmed = await ask(rl, 'Have you saved your seed phrase? (yes to continue): ');
      if (confirmed.toLowerCase() !== 'yes' && confirmed.toLowerCase() !== 'y') {
        console.log('\nSetup cancelled. Please save your seed phrase and try again.');
        rl.close();
        process.exit(1);
      }
    }

    // ── Step 2: Key Derivation ────────────────────────────────────────────

    const { authKey, salt } = deriveAuthKey(mnemonic);
    const authKeyHash = computeAuthKeyHash(authKey);
    const saltHex = Buffer.from(salt).toString('hex');

    console.log('\nKeys derived successfully.');

    // ── Step 3: Server URL ────────────────────────────────────────────────

    const serverUrlInput = await ask(
      rl,
      `Server URL (press Enter for ${DEFAULT_SERVER_URL}): `,
    );
    const serverUrl = serverUrlInput || DEFAULT_SERVER_URL;

    // ── Step 4: Server Registration ───────────────────────────────────────

    console.log(`\nRegistering with ${serverUrl}...`);

    let userId: string;
    try {
      userId = await registerWithServer(serverUrl, authKeyHash, saltHex);
      console.log(`Registered successfully. User ID: ${userId}`);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      console.error(`\nRegistration failed: ${message}`);
      console.error('You can retry setup later or register manually.');
      rl.close();
      process.exit(1);
    }

    // ── Step 5: Save Credentials ──────────────────────────────────────────

    const credentials: SavedCredentials = {
      userId,
      salt: saltHex,
      serverUrl,
    };

    saveCredentials(credentials);
    console.log(`\nCredentials saved to ${CREDENTIALS_PATH}`);
    console.log('(Mnemonic and keys are NOT stored -- only userId, salt, and serverUrl.)');

    // ── Step 6: Print Config Snippet ──────────────────────────────────────

    printConfigSnippet(serverUrl);

    console.log('Setup complete! You can now use TotalReclaw with any MCP-compatible AI agent.');
    console.log('');
  } finally {
    rl.close();
  }
}

// ---------------------------------------------------------------------------
// CLI Entry Point
// ---------------------------------------------------------------------------

// When run directly (not imported), execute the setup wizard
const isDirectRun =
  typeof process !== 'undefined' &&
  process.argv[1] &&
  (process.argv[1].endsWith('/setup.js') ||
   process.argv[1].endsWith('/setup.ts') ||
   process.argv[1].endsWith('cli/setup'));

if (isDirectRun) {
  runSetup().catch((error) => {
    console.error('Setup failed:', error);
    process.exit(1);
  });
}
