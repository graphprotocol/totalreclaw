#!/usr/bin/env tsx
/**
 * run-all.ts -- Main orchestrator for the E2E functional test suite.
 *
 * Execution flow (section 7.3):
 *   1. Start in-memory mock TotalReclaw server
 *   2. Parse CLI arguments (--instances, --scenarios)
 *   3. For each instance:
 *      a. Set environment variables (including mock server URL)
 *      b. Install GraphQL interceptor (if subgraph mode)
 *      c. For each applicable scenario:
 *         - Reset mock server state + plugin state
 *         - Create ConversationDriver
 *         - driver.initialize() -> driver.runScenario()
 *         - Collect TestMetrics, run assertions
 *   4. Run cross-instance comparison assertions
 *   5. Generate JSON report + stdout summary
 *   6. Stop mock server
 *
 * Usage:
 *   tsx run-all.ts
 *   tsx run-all.ts --instances=server-improved,server-baseline
 *   tsx run-all.ts --scenarios=C --instances=server-improved
 *   tsx run-all.ts --scenarios=A,B,C --instances=server-improved,server-baseline
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { ConversationDriver } from './conversation-driver.js';
import { generateReport, printSummary, writeReportToFile } from './report.js';
import {
  installGraphQLInterceptor,
  getGraphQLCaptures,
  resetCaptures,
} from './interceptors/graphql-interceptor.js';
import { analyzeCacheEvents } from './interceptors/cache-monitor.js';
import { analyzeExtractionEvents } from './interceptors/extraction-tracker.js';
import * as scenarioAssertions from './assertions/scenario-assertions.js';
import { startMockServer, type MockServer } from './mock-server.js';
import { startMockSubgraph, type MockSubgraph } from './mock-subgraph.js';
import {
  installLLMInterceptor,
  resetExtractionCallCount,
  resetAnthropicTurnCounter,
} from './interceptors/llm-interceptor.js';
import type {
  InstanceConfig,
  ConversationScenario,
  TestMetrics,
  AssertionResult,
  InstanceReport,
  Turn,
  TurnResult,
} from './types.js';
import { SCENARIO_APPLICABILITY } from './types.js';

// ---------------------------------------------------------------------------
// Instance definitions (section 2.2)
// ---------------------------------------------------------------------------

const PLUGIN_PATH = '../../skill/plugin/index.ts';

/** These env vars are injected into ALL server-mode instances at runtime. */
function serverBaseEnv(mockServerUrl: string, credentialsDir: string): Record<string, string> {
  return {
    TOTALRECLAW_RECOVERY_PHRASE: 'e2e-test-master-password-2026',
    TOTALRECLAW_SERVER_URL: mockServerUrl,
    TOTALRECLAW_CREDENTIALS_PATH: path.join(credentialsDir, 'credentials.json'),
    // Mock LLM: set a fake OpenAI key so the plugin's LLM client detects a provider.
    // The actual API calls are intercepted by llm-interceptor.ts.
    OPENAI_API_KEY: 'mock-openai-key-for-e2e-testing',
    // Mock Anthropic key for Scenario H (LLM-driven freeform).
    // The orchestrator checks isApiKeyAvailable() which reads this env var.
    ANTHROPIC_API_KEY: 'mock-anthropic-key-for-e2e',
    // Redirect Anthropic SDK to the mock server (SDK bypasses globalThis.fetch).
    // The mock server serves /v1/messages with pre-scripted Alex Chen responses.
    ANTHROPIC_BASE_URL: mockServerUrl,
  };
}

const ALL_INSTANCES: InstanceConfig[] = [
  {
    id: 'server-improved',
    mode: 'server',
    improvements: true,
    env: {
      TOTALRECLAW_RELEVANCE_THRESHOLD: '0.3',
      TOTALRECLAW_EXTRACT_EVERY_TURNS: '5',
      TOTALRECLAW_RANKING_WEIGHTS: '0.25,0.25,0.25,0.25',
      TOTALRECLAW_TWO_TIER_SEARCH: 'true',
      TOTALRECLAW_CACHE_TTL_MS: '300000',
    },
    pluginPath: PLUGIN_PATH,
  },
  {
    id: 'server-baseline',
    mode: 'server',
    improvements: false,
    env: {
      TOTALRECLAW_RELEVANCE_THRESHOLD: '0.0',
      TOTALRECLAW_EXTRACT_EVERY_TURNS: '1',
      TOTALRECLAW_RANKING_WEIGHTS: '0.25,0.25,0.25,0.25',
      TOTALRECLAW_TWO_TIER_SEARCH: 'false',
      TOTALRECLAW_CACHE_TTL_MS: '0',
    },
    pluginPath: PLUGIN_PATH,
  },
  {
    id: 'subgraph-improved',
    mode: 'subgraph',
    improvements: true,
    env: {
      TOTALRECLAW_RELEVANCE_THRESHOLD: '0.3',
      TOTALRECLAW_EXTRACT_EVERY_TURNS: '5',
      TOTALRECLAW_RANKING_WEIGHTS: '0.25,0.25,0.25,0.25',
      // TWO_TIER_SEARCH disabled for mock: LSH-only hook search needs ~100+ facts
      // to reliably match. With <20 mock facts, word trapdoors are needed.
      TOTALRECLAW_TWO_TIER_SEARCH: 'false',
      TOTALRECLAW_CACHE_TTL_MS: '300000',
      TOTALRECLAW_SUBGRAPH_ENDPOINT: 'http://localhost:28000/subgraphs/name/totalreclaw',
    },
    pluginPath: PLUGIN_PATH,
  },
  {
    id: 'subgraph-baseline',
    mode: 'subgraph',
    improvements: false,
    env: {
      TOTALRECLAW_RELEVANCE_THRESHOLD: '0.0',
      TOTALRECLAW_EXTRACT_EVERY_TURNS: '1',
      TOTALRECLAW_TWO_TIER_SEARCH: 'false',
      TOTALRECLAW_CACHE_TTL_MS: '0',
      TOTALRECLAW_SUBGRAPH_ENDPOINT: 'http://localhost:28000/subgraphs/name/totalreclaw',
    },
    pluginPath: PLUGIN_PATH,
  },
  {
    id: 'server-recency',
    mode: 'server',
    improvements: true,
    env: {
      TOTALRECLAW_RELEVANCE_THRESHOLD: '0.3',
      TOTALRECLAW_EXTRACT_EVERY_TURNS: '5',
      TOTALRECLAW_RANKING_WEIGHTS: '0.2,0.2,0.2,0.4',
      TOTALRECLAW_TWO_TIER_SEARCH: 'true',
      TOTALRECLAW_CACHE_TTL_MS: '300000',
    },
    pluginPath: PLUGIN_PATH,
  },
];

// ---------------------------------------------------------------------------
// Scenario loading
// ---------------------------------------------------------------------------

const SCENARIO_MODULES: Record<string, string> = {
  A: './scenarios/scenario-a-preferences.js',
  B: './scenarios/scenario-b-technical.js',
  C: './scenarios/scenario-c-noise.js',
  D: './scenarios/scenario-d-topics.js',
  E: './scenarios/scenario-e-long.js',
  F: './scenarios/scenario-f-subgraph.js',
  G: './scenarios/scenario-g-pagination.js',
  H: './scenarios/scenario-h-freeform.js',
};

async function loadScenario(
  scenarioId: string,
  pluginPath: string,
): Promise<ConversationScenario> {
  const modulePath = SCENARIO_MODULES[scenarioId];
  if (!modulePath) {
    throw new Error(
      `Unknown scenario ID: ${scenarioId}. Available: ${Object.keys(SCENARIO_MODULES).join(', ')}`,
    );
  }

  const mod = await import(modulePath);
  const scenario: ConversationScenario =
    mod.default ??
    mod[`scenario${scenarioId}`] ??
    Object.values(mod).find(
      (v: any) => v && typeof v === 'object' && 'turns' in v && 'id' in v,
    );

  if (!scenario) {
    throw new Error(
      `Scenario module ${modulePath} does not export a ConversationScenario`,
    );
  }

  return { ...scenario, pluginPath };
}

// ---------------------------------------------------------------------------
// Assertion loading
// ---------------------------------------------------------------------------

function getAssertionsForScenario(
  scenarioId: string,
): Record<string, (metrics: TestMetrics) => void> {
  const prefix = `scenario${scenarioId}_`;
  const result: Record<string, (metrics: TestMetrics) => void> = {};

  for (const [name, fn] of Object.entries(scenarioAssertions)) {
    if (name.startsWith(prefix) && typeof fn === 'function') {
      result[name] = fn as (metrics: TestMetrics) => void;
    }
  }

  return result;
}

// ---------------------------------------------------------------------------
// CLI argument parsing
// ---------------------------------------------------------------------------

interface CliArgs {
  instances: string[];
  scenarios: string[];
}

function parseArgs(): CliArgs {
  const args = process.argv.slice(2);
  let instances: string[] | null = null;
  let scenarios: string[] | null = null;

  for (const arg of args) {
    if (arg.startsWith('--instances=')) {
      instances = arg.split('=')[1].split(',').map((s) => s.trim());
    } else if (arg.startsWith('--scenarios=')) {
      scenarios = arg.split('=')[1].split(',').map((s) => s.trim().toUpperCase());
    }
  }

  return {
    instances: instances ?? ALL_INSTANCES.map((i) => i.id),
    scenarios: scenarios ?? Object.keys(SCENARIO_APPLICABILITY),
  };
}

// ---------------------------------------------------------------------------
// Assertion runner
// ---------------------------------------------------------------------------

function runAssertions(
  assertions: Record<string, (metrics: TestMetrics) => void>,
  metrics: TestMetrics,
): Record<string, AssertionResult> {
  const results: Record<string, AssertionResult> = {};

  for (const [name, fn] of Object.entries(assertions)) {
    try {
      fn(metrics);
      results[name] = { passed: true, message: 'PASS' };
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      results[name] = { passed: false, message };
    }
  }

  return results;
}

// ---------------------------------------------------------------------------
// Environment variable management
// ---------------------------------------------------------------------------

function setInstanceEnv(config: InstanceConfig): Record<string, string | undefined> {
  const previous: Record<string, string | undefined> = {};
  for (const [key, value] of Object.entries(config.env)) {
    previous[key] = process.env[key];
    process.env[key] = value;
  }
  return previous;
}

function restoreEnv(previous: Record<string, string | undefined>): void {
  for (const [key, value] of Object.entries(previous)) {
    if (value === undefined) {
      delete process.env[key];
    } else {
      process.env[key] = value;
    }
  }
}

// ---------------------------------------------------------------------------
// Plugin state reset
// ---------------------------------------------------------------------------

/**
 * Reset the plugin's module-level state between scenarios.
 * Dynamically imports the plugin to call __resetForTesting().
 */
async function resetPluginState(pluginPath: string): Promise<void> {
  try {
    const mod = await import(pluginPath);
    if (typeof mod.__resetForTesting === 'function') {
      mod.__resetForTesting();
    }
  } catch {
    // Plugin not loaded yet — nothing to reset
  }
}

// ---------------------------------------------------------------------------
// Main execution
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  const startTime = performance.now();
  const cliArgs = parseArgs();

  // Create temp directory for credentials
  const credentialsDir = fs.mkdtempSync(path.join(os.tmpdir(), 'totalreclaw-e2e-'));

  // Install LLM mock interceptor BEFORE any plugin loading.
  // This intercepts fetch calls to LLM APIs and returns mock extraction results.
  installLLMInterceptor();

  // Start mock server (needed for all instances: server-mode uses it for API,
  // subgraph-mode uses it for user registration + Anthropic mock)
  const mockServer = await startMockServer();
  console.log(`Mock server started at ${mockServer.url}`);

  // Start mock subgraph (for subgraph-mode instances)
  let mockSubgraph: MockSubgraph | null = null;
  const hasSubgraphInstances = cliArgs.instances.some((id) => {
    const inst = ALL_INSTANCES.find((i) => i.id === id);
    return inst && inst.mode === 'subgraph';
  });

  if (hasSubgraphInstances) {
    mockSubgraph = await startMockSubgraph();
    console.log(`Mock subgraph started: relay=${mockSubgraph.relayUrl}, graphql=${mockSubgraph.graphqlUrl}`);
  }

  console.log('='.repeat(70));
  console.log('  TotalReclaw E2E Functional Test Suite');
  console.log('='.repeat(70));
  console.log(`  Instances: ${cliArgs.instances.join(', ')}`);
  console.log(`  Scenarios: ${cliArgs.scenarios.join(', ')}`);
  if (mockServer) {
    console.log(`  Mock Server: ${mockServer.url}`);
  }
  if (mockSubgraph) {
    console.log(`  Mock Relay:    ${mockSubgraph.relayUrl}`);
    console.log(`  Mock GraphQL:  ${mockSubgraph.graphqlUrl}`);
  }
  console.log('='.repeat(70));
  console.log();

  // Filter instances to those requested
  const instances = ALL_INSTANCES.filter((i) => cliArgs.instances.includes(i.id));
  if (instances.length === 0) {
    console.error(
      `No matching instances found for: ${cliArgs.instances.join(', ')}`,
    );
    console.error(
      `Available: ${ALL_INSTANCES.map((i) => i.id).join(', ')}`,
    );
    process.exit(1);
  }

  // Inject server base env vars into server-mode instances
  const baseEnv = serverBaseEnv(mockServer.url, credentialsDir);
  for (const inst of instances) {
    if (inst.mode === 'server') {
      Object.assign(inst.env, baseEnv);
    }
  }

  // Inject mock subgraph URLs into subgraph-mode instances
  if (mockSubgraph) {
    for (const inst of instances) {
      if (inst.mode === 'subgraph') {
        inst.env.TOTALRECLAW_RELAY_URL = mockSubgraph.relayUrl;
        // The GraphQL endpoint path must match what the plugin constructs:
        // it uses the full env var value as-is, so include the subgraph path.
        inst.env.TOTALRECLAW_SUBGRAPH_ENDPOINT = `${mockSubgraph.graphqlUrl}/subgraphs/name/totalreclaw`;
        // Subgraph instances also need the mock server for user registration,
        // plus recovery phrase, credentials, and mock LLM keys.
        inst.env.TOTALRECLAW_SERVER_URL = mockServer.url;
        inst.env.TOTALRECLAW_RECOVERY_PHRASE = 'e2e-test-master-password-2026';
        inst.env.TOTALRECLAW_CREDENTIALS_PATH = path.join(credentialsDir, 'credentials.json');
        inst.env.OPENAI_API_KEY = 'mock-openai-key-for-e2e-testing';
        inst.env.ANTHROPIC_API_KEY = 'mock-anthropic-key-for-e2e';
        inst.env.ANTHROPIC_BASE_URL = mockServer.url;
      }
    }
  }

  // Collect all results keyed by instanceId -> scenarioId
  const allResults: Record<string, InstanceReport> = {};
  let totalAssertions = 0;
  let totalPassed = 0;
  let totalFailed = 0;
  let scenariosRun = 0;

  for (const instance of instances) {
    console.log(
      `\n--- Instance: ${instance.id} (${instance.mode}, improvements=${instance.improvements}) ---\n`,
    );
    allResults[instance.id] = { scenarios: {} };

    // Install GraphQL interceptor for subgraph instances
    if (instance.mode === 'subgraph') {
      installGraphQLInterceptor();
    }

    for (const scenarioId of cliArgs.scenarios) {
      // Check scenario applicability (section 7.4)
      const applicableInstances = SCENARIO_APPLICABILITY[scenarioId];
      if (!applicableInstances || !applicableInstances.includes(instance.id)) {
        console.log(`  [SKIP] Scenario ${scenarioId} -- not applicable for ${instance.id}`);
        continue;
      }

      console.log(`  [RUN]  Scenario ${scenarioId} on ${instance.id}...`);
      const scenarioStart = performance.now();

      try {
        // Reset state for isolation
        if (mockServer) mockServer.reset();
        if (mockSubgraph) mockSubgraph.reset();
        resetCaptures();
        resetExtractionCallCount();
        resetAnthropicTurnCounter();
        await resetPluginState(instance.pluginPath);

        // Delete credentials file so plugin re-registers
        const credFile = path.join(credentialsDir, 'credentials.json');
        if (fs.existsSync(credFile)) fs.unlinkSync(credFile);

        // Load scenario definition with instance-specific pluginPath
        const scenario = await loadScenario(scenarioId, instance.pluginPath);

        // Collect assertions for this scenario
        const assertions = getAssertionsForScenario(scenarioId);

        // Save and set environment
        const previousEnv = setInstanceEnv(instance);

        // Create driver and run scenario
        const driver = new ConversationDriver(instance, scenario);
        let metrics: TestMetrics;
        let turnsExecuted: number;

        // Scenario H (LLM-driven freeform): delegate to the orchestrator
        if ((scenario as ConversationScenario & { useLlmOrchestrator?: boolean }).useLlmOrchestrator) {
          // Ensure mock Anthropic key is set (serverBaseEnv sets it for server
          // instances, but subgraph instances may not have it)
          if (!process.env.ANTHROPIC_API_KEY) {
            process.env.ANTHROPIC_API_KEY = 'mock-anthropic-key-for-e2e';
          }

          const { runLlmDrivenScenario } = await import('./llm-orchestrator.js');

          // Initialize the plugin (register hooks + tools)
          await driver.initialize();

          // Build conversation history tracker and adapter for the orchestrator.
          // The orchestrator expects a `processTurn(turn)` interface while our
          // ConversationDriver has `runTurn(turn, messageHistory)`.
          let messageHistory: unknown[] = [];
          const adapter = {
            async processTurn(turn: Turn): Promise<TurnResult> {
              const result = await driver.runTurn(turn, messageHistory);
              messageHistory = result.messageHistory;
              return result;
            },
          };

          // Run the orchestrator-driven scenario
          const generatedTurns = await runLlmDrivenScenario(adapter);
          turnsExecuted = generatedTurns.length;

          // Trigger compaction if the scenario requests it
          if (scenario.triggerCompaction) {
            // Access private fireHook via cast -- acceptable in test infrastructure
            await (driver as any).fireHook('before_compaction', {
              messages: messageHistory,
              messageCount: messageHistory.length,
            });
          }

          metrics = driver.getMetrics();
        } else {
          // Standard scenario: driver iterates the static turns array
          metrics = await driver.runScenario();
          turnsExecuted = scenario.turns.length;
        }

        // Enrich metrics with interceptor data
        if (instance.mode === 'subgraph') {
          metrics.graphqlQueries = getGraphQLCaptures();
        }
        metrics.cacheEvents = analyzeCacheEvents(driver.getLogs(), metrics.injectionEvents);
        metrics.extractionEvents = analyzeExtractionEvents(
          driver.getLogs(),
          turnsExecuted,
        );

        // Run assertions
        const assertionResults = runAssertions(assertions, metrics);

        // Tally results
        for (const result of Object.values(assertionResults)) {
          totalAssertions++;
          if (result.passed) totalPassed++;
          else totalFailed++;
        }
        scenariosRun++;

        // Store report
        allResults[instance.id].scenarios[scenarioId] = {
          metrics,
          assertions: assertionResults,
        };

        // Restore environment
        restoreEnv(previousEnv);

        const elapsed = ((performance.now() - scenarioStart) / 1000).toFixed(1);
        const passCount = Object.values(assertionResults).filter((r) => r.passed).length;
        const failCount = Object.values(assertionResults).filter((r) => !r.passed).length;
        const status = failCount === 0 ? 'PASS' : 'FAIL';
        console.log(
          `  [${status}] Scenario ${scenarioId} -- ${passCount} passed, ${failCount} failed (${elapsed}s)`,
        );

        // Print individual failures
        for (const [name, result] of Object.entries(assertionResults)) {
          if (!result.passed) {
            console.log(`         FAIL: ${name} -- ${result.message}`);
          }
        }
      } catch (err: unknown) {
        const message = err instanceof Error ? err.message : String(err);
        console.error(`  [ERROR] Scenario ${scenarioId} -- ${message}`);
        allResults[instance.id].scenarios[scenarioId] = {
          metrics: {
            instanceId: instance.id,
            scenarioId,
            hookInvocations: [],
            cacheEvents: [],
            extractionEvents: [],
            injectionEvents: [],
            graphqlQueries: [],
            turnMetrics: [],
          },
          assertions: {
            _scenario_error: { passed: false, message },
          },
        };
        totalAssertions++;
        totalFailed++;
        scenariosRun++;
      }
    }
  }

  // -------------------------------------------------------------------------
  // Cleanup
  // -------------------------------------------------------------------------

  if (mockServer) {
    await mockServer.stop();
    console.log('\nMock server stopped.');
  }

  if (mockSubgraph) {
    await mockSubgraph.stop();
    console.log('Mock subgraph stopped.');
  }

  // Clean up temp credentials dir
  try {
    fs.rmSync(credentialsDir, { recursive: true, force: true });
  } catch { /* ignore */ }

  // -------------------------------------------------------------------------
  // Generate report
  // -------------------------------------------------------------------------

  const durationSeconds = (performance.now() - startTime) / 1000;

  const report = generateReport({
    allResults,
    durationSeconds,
    totalAssertions,
    totalPassed,
    totalFailed,
    scenariosRun,
    instancesUsed: instances.length,
  });

  // Write JSON report to results/
  await writeReportToFile(report);

  // Print summary
  console.log();
  printSummary(report);

  // Exit with appropriate code
  process.exit(totalFailed > 0 ? 1 : 0);
}

main().catch((err) => {
  console.error('Fatal error:', err);
  process.exit(2);
});
