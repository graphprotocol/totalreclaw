module.exports = {
  testEnvironment: 'node',
  roots: ['<rootDir>/tests'],
  testMatch: ['**/*.test.js', '**/*.test.ts'],
  moduleFileExtensions: ['ts', 'js', 'json'],
  transform: {
    // Transform TypeScript sources
    '^.+\\.ts$': ['ts-jest', {
      tsconfig: {
        module: 'commonjs',
        moduleResolution: 'node',
        esModuleInterop: true,
        allowJs: true,
        strict: false,
        noImplicitAny: false,
      },
    }],
    // Transform ESM-only node_modules (@noble/hashes is type: module)
    'node_modules/@noble/.+\\.js$': ['ts-jest', {
      tsconfig: {
        module: 'commonjs',
        moduleResolution: 'node',
        esModuleInterop: true,
        allowJs: true,
      },
    }],
    'node_modules/@scure/.+\\.js$': ['ts-jest', {
      tsconfig: {
        module: 'commonjs',
        moduleResolution: 'node',
        esModuleInterop: true,
        allowJs: true,
      },
    }],
  },
  // Allow Jest to transform ESM packages in node_modules
  transformIgnorePatterns: [
    'node_modules/(?!(@noble/hashes|@scure/bip39|@scure/base)/)',
  ],
  resolver: '<rootDir>/tests/jest-resolver.js',
  // totalreclaw-relay#79: no test reaches the Smart Account RPC via the pair ack.
  setupFilesAfterEnv: ['<rootDir>/tests/setup-no-smart-account-rpc.ts'],
  collectCoverageFrom: [
    'src/**/*.ts',
    '!src/**/*.d.ts',
  ],
};
