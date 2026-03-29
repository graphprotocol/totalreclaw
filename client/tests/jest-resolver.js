/**
 * Custom Jest resolver that handles ESM-style .js extension imports
 * in TypeScript source files compiled via ts-jest with CJS output.
 *
 * For npm packages like @noble/hashes that use .js subpath exports
 * (e.g., `@noble/hashes/hkdf.js`), the file literally exists as hkdf.js
 * so no mapping is needed. For local .ts files imported as .js, we remap.
 */
module.exports = (path, options) => {
  try {
    return options.defaultResolver(path, options);
  } catch (err) {
    if (path.endsWith('.js')) {
      try {
        return options.defaultResolver(path.replace(/\.js$/, '.ts'), options);
      } catch (_) {
        try {
          return options.defaultResolver(path.replace(/\.js$/, ''), options);
        } catch (__) {
          // Fall through to original error
        }
      }
    }
    throw err;
  }
};
