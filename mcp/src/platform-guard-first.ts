/**
 * Side-effect module: import this FIRST in every entry point so the platform
 * check runs before any native module (onnxruntime-node) loads.
 */
import { assertSupportedPlatform } from './platform-guard.js';

assertSupportedPlatform();
