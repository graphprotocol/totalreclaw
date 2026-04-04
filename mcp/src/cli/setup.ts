#!/usr/bin/env node
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

import { generateMnemonic, validateMnemonic } from '@scure/bip39';
import { wordlist } from '@scure/bip39/wordlists/english.js';
import * as wasm from '@totalreclaw/core';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

export const DEFAULT_SERVER_URL = 'https://api.totalreclaw.xyz';
const CREDENTIALS_DIR = path.join(os.homedir(), '.totalreclaw');
export const CREDENTIALS_PATH = path.join(CREDENTIALS_DIR, 'credentials.json');

// ---------------------------------------------------------------------------
// Key Derivation (delegates to @totalreclaw/core Rust WASM)
// ---------------------------------------------------------------------------

/**
 * Derive the auth key and salt from a BIP-39 mnemonic.
 *
 * Delegates to `@totalreclaw/core` WASM which performs:
 *   - BIP-39 seed via mnemonicToSeedSync (512 bits)
 *   - salt = first 32 bytes of seed
 *   - authKey = HKDF-SHA256(seed, salt, "totalreclaw-auth-key-v1", 32)
 *
 * Returns hex-encoded strings (not raw Uint8Array).
 */
export function deriveAuthKey(mnemonic: string): { authKeyHex: string; saltHex: string } {
  const keys = wasm.deriveKeysFromMnemonic(mnemonic.trim());
  return { authKeyHex: keys.auth_key, saltHex: keys.salt };
}

/**
 * Compute SHA-256(authKey) as a hex string.
 * Delegates to `@totalreclaw/core` WASM.
 *
 * @param authKeyHex - 64-char hex string (32 bytes)
 */
export function computeAuthKeyHash(authKeyHex: string): string {
  return wasm.computeAuthKeyHash(authKeyHex);
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

export async function registerWithServer(
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
  mnemonic?: string;
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

function printConfigSnippet(_serverUrl: string): void {
  const snippet = {
    mcpServers: {
      totalreclaw: {
        command: 'npx',
        args: ['@totalreclaw/mcp-server'],
      },
    },
  };

  console.log('\n========================================');
  console.log('Add this to your MCP config');
  console.log('(Claude Desktop / Cursor / VS Code):');
  console.log('========================================\n');
  console.log(JSON.stringify(snippet, null, 2));
  console.log(`\nCredentials are loaded automatically from ${CREDENTIALS_PATH}.`);
  console.log('');
}

// ---------------------------------------------------------------------------
// Recovery Phrase Warning
// ---------------------------------------------------------------------------

function printRecoveryPhraseWarning(): void {
  console.log('');
  console.log('+------------------------------------------------------------+');
  console.log('|  CRITICAL: Your recovery phrase is your ONLY identity.     |');
  console.log('|  If you lose it, you lose ALL your memories forever.       |');
  console.log('|  There is NO password reset. No recovery. No support.      |');
  console.log('|  Write it down. Store it securely. Never share it.         |');
  console.log('+------------------------------------------------------------+');
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

    const hasExisting = await ask(rl, 'Do you have an existing recovery phrase? (y/n): ');

    if (hasExisting.toLowerCase() === 'y' || hasExisting.toLowerCase() === 'yes') {
      // Import existing mnemonic
      console.log('');
      console.log('Enter your 12-word recovery phrase (space-separated):');
      mnemonic = await ask(rl, '> ');

      if (!validateMnemonic(mnemonic, wordlist)) {
        console.error('\nError: Invalid mnemonic. Please check your words and try again.');
        rl.close();
        process.exit(1);
      }

      console.log('\nRecovery phrase validated successfully.');

      // Show critical warning even for returning users
      printRecoveryPhraseWarning();
    } else {
      // Generate new mnemonic
      mnemonic = generateMnemonic(wordlist, 128); // 128 bits = 12 words

      console.log('');
      console.log('Your new recovery phrase:');
      console.log('');
      console.log(`  ${mnemonic}`);
      console.log('');

      printRecoveryPhraseWarning();

      // Require explicit confirmation
      let confirmed = await ask(rl, 'Have you written down your recovery phrase? (yes/no): ');
      while (confirmed.toLowerCase() !== 'yes') {
        if (confirmed.toLowerCase() === 'no' || confirmed.toLowerCase() === 'n') {
          console.log('');
          console.log('Please write down your recovery phrase before continuing:');
          console.log('');
          console.log(`  ${mnemonic}`);
          console.log('');
        }
        confirmed = await ask(rl, 'Have you written down your recovery phrase? (yes/no): ');
      }
    }

    // ── Step 2: Key Derivation ────────────────────────────────────────────

    const { authKeyHex, saltHex } = deriveAuthKey(mnemonic);
    const authKeyHash = computeAuthKeyHash(authKeyHex);

    console.log('\nKeys derived successfully.');

    // ── Step 3: Server URL ────────────────────────────────────────────────
    // Default to production — only override via TOTALRECLAW_SERVER_URL env var (for self-hosted)

    const serverUrl = process.env.TOTALRECLAW_SERVER_URL || DEFAULT_SERVER_URL;

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
      mnemonic,
    };

    saveCredentials(credentials);
    console.log(`\nCredentials saved to ${CREDENTIALS_PATH}`);
    console.log('(Recovery phrase saved securely in credentials.json with owner-only permissions.)');

    // ── Step 6: Pre-download Embedding Model ────────────────────────────

    console.log('\nDownloading embedding model (one-time, ~164MB)...');
    console.log('This enables local, private semantic search — no API calls needed.\n');
    try {
      const { pipeline } = await import('@huggingface/transformers');
      await pipeline('feature-extraction', 'onnx-community/harrier-oss-v1-270m-ONNX', {
        // @ts-ignore - quantized option exists at runtime but not in type defs
        quantized: true,
      } as any);
      console.log('Embedding model downloaded and cached.');
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      console.warn(`\nWarning: Could not pre-download embedding model: ${message}`);
      console.warn('The model will be downloaded automatically on first use.');
    }

    // ── Step 7: Print Config Snippet ──────────────────────────────────────

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
