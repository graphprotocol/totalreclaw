/**
 * Early exit on platforms the embedding runtime does not support.
 *
 * `onnxruntime-node` (pulled in by `@huggingface/transformers`) ships binaries
 * only for darwin-arm64, linux-x64/arm64 and win32-x64/arm64. Anywhere else
 * (an Intel Mac, for one) the module graph fails to load with an obscure
 * native-module error before the server answers `initialize`, and the host app
 * just shows no tools.
 *
 * This file holds the pure check, so tests can import it without exiting. The
 * entry points import `./platform-guard-first.js` first, which runs the check
 * before any native module loads.
 */
import * as fs from 'node:fs';

/** platform -> architectures onnxruntime-node 1.30.0 ships a binary for. */
export const SUPPORTED_PLATFORMS: Readonly<Record<string, readonly string[]>> = {
  darwin: ['arm64'],
  linux: ['x64', 'arm64'],
  win32: ['x64', 'arm64'],
};

const DOCS = 'See https://github.com/graphprotocol/totalreclaw/tree/main/mcp (see Requirements).';

export const INTEL_MAC_MESSAGE =
  'TotalReclaw MCP server: Intel Macs (darwin-x64) are not supported. ' +
  'The embedding runtime (onnxruntime-node) has no build for this platform. ' +
  'Use an Apple silicon Mac, Linux or Windows. ' +
  'If this is an Apple silicon Mac running an x64 Node.js (for example under Rosetta), ' +
  'reinstall Node.js as an arm64 (Apple silicon) build. ' +
  DOCS;

export function unsupportedPlatformMessage(platform: string, arch: string): string {
  if (platform === 'darwin' && arch === 'x64') return INTEL_MAC_MESSAGE;
  return (
    `TotalReclaw MCP server: ${platform}-${arch} is not supported. ` +
    'The embedding runtime (onnxruntime-node) has no build for this platform. ' +
    'Supported: macOS on Apple silicon, Linux (x64, arm64) and Windows (x64, arm64). ' +
    DOCS
  );
}

export function isUnsupportedPlatform(platform: string = process.platform, arch: string = process.arch): boolean {
  return !(SUPPORTED_PLATFORMS[platform] ?? []).includes(arch);
}

/** Write the message synchronously to stderr and exit(1) on an unsupported platform. */
export function assertSupportedPlatform(
  platform: string = process.platform,
  arch: string = process.arch,
  exit: (code: number) => void = (code) => process.exit(code),
  write: (text: string) => void = (text) => fs.writeSync(2, text),
): void {
  if (!isUnsupportedPlatform(platform, arch)) return;
  try {
    // Synchronous: console.error to a pipe is async on macOS and can be lost on exit.
    write(unsupportedPlatformMessage(platform, arch) + '\n');
  } catch {
    // stderr closed: still exit.
  }
  exit(1);
}
