/**
 * Early exit on platforms the embedding runtime does not support.
 *
 * `onnxruntime-node` (pulled in by `@huggingface/transformers`) ships no
 * darwin-x64 binary, so on an Intel Mac the module graph fails to load with an
 * obscure native-module error before the server answers `initialize`, and the
 * host app just shows no tools. This module is imported FIRST by the entry
 * points so the check runs before any native module loads.
 */

export const INTEL_MAC_MESSAGE =
  'TotalReclaw MCP server: Intel Macs (darwin-x64) are not supported. ' +
  'The embedding runtime (onnxruntime-node) has no build for this platform. ' +
  'Use an Apple silicon Mac, Linux or Windows. ' +
  'See https://github.com/graphprotocol/totalreclaw/tree/main/mcp (see Requirements)';

export function isUnsupportedPlatform(platform: string = process.platform, arch: string = process.arch): boolean {
  return platform === 'darwin' && arch === 'x64';
}

/** Print the message to stderr and exit(1) on an unsupported platform. */
export function assertSupportedPlatform(
  platform: string = process.platform,
  arch: string = process.arch,
  exit: (code: number) => void = (code) => process.exit(code),
): void {
  if (!isUnsupportedPlatform(platform, arch)) return;
  console.error(INTEL_MAC_MESSAGE);
  exit(1);
}

assertSupportedPlatform();
