#!/usr/bin/env tsx
/**
 * run-scenario.ts -- Run a single scenario on a single instance.
 *
 * Simpler entry point for quick iteration during development.
 *
 * Usage:
 *   tsx run-scenario.ts --scenario=A --instance=server-improved
 *   tsx run-scenario.ts --scenario=C --instance=server-baseline
 */

import fs from 'node:fs';
import { ConversationDriver } from './conversation-driver.js';
import {
  installGraphQLInterceptor,
  getGraphQLCaptures,
  resetCaptures,
} from './interceptors/graphql-interceptor.js';
import { analyzeCacheEvents } from './interceptors/cache-monitor.js';
import { analyzeExtractionEvents } from './interceptors/extraction-tracker.js';
import * as scenarioAssertions from './assertions/scenario-assertions.js';
import type { InstanceConfig, ConversationScenario, TestMetrics } from './types.js';
import { SCENARIO_APPLICABILITY } from './types.js';

// ---------------------------------------------------------------------------
// Instance definitions (mirrored from run-all.ts for standalone use)
// ---------------------------------------------------------------------------

const PLUGIN_PATH = '../../skill/plugin/index.ts';

const ALL_INSTANCES: Record<string, InstanceConfig> = {
  'server-improved': {
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
  'server-baseline': {
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
  'subgraph-improved': {
    id: 'subgraph-improved',
    mode: 'subgraph',
    improvements: true,
    env: {
      TOTALRECLAW_RELEVANCE_THRESHOLD: '0.3',
      TOTALRECLAW_EXTRACT_EVERY_TURNS: '5',
      TOTALRECLAW_RANKING_WEIGHTS: '0.25,0.25,0.25,0.25',
      TOTALRECLAW_TWO_TIER_SEARCH: 'true',
      TOTALRECLAW_CACHE_TTL_MS: '300000',
      TOTALRECLAW_SUBGRAPH_ENDPOINT: 'http://localhost:28000/subgraphs/name/totalreclaw',
    },
    pluginPath: PLUGIN_PATH,
  },
  'subgraph-baseline': {
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
  'server-recency': {
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
};

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

function parseArgs(): { scenario: string; instance: string } {
  const args = process.argv.slice(2);
  let scenario: string | null = null;
  let instance: string | null = null;

  for (const arg of args) {
    if (arg.startsWith('--scenario=')) {
      scenario = arg.split('=')[1].trim().toUpperCase();
    } else if (arg.startsWith('--instance=')) {
      instance = arg.split('=')[1].trim();
    }
  }

  if (!scenario || !instance) {
    console.error('Usage: tsx run-scenario.ts --scenario=<A-H> --instance=<instance-id>');
    console.error();
    console.error('Scenarios: A, B, C, D, E, F, G, H');
    console.error(`Instances: ${Object.keys(ALL_INSTANCES).join(', ')}`);
    process.exit(1);
  }

  return { scenario, instance };
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  const { scenario: scenarioId, instance: instanceId } = parseArgs();

  const instanceConfig = ALL_INSTANCES[instanceId];
  if (!instanceConfig) {
    console.error(`Unknown instance: ${instanceId}`);
    console.error(`Available: ${Object.keys(ALL_INSTANCES).join(', ')}`);
    process.exit(1);
  }

  // Check applicability
  const applicableInstances = SCENARIO_APPLICABILITY[scenarioId];
  if (!applicableInstances?.includes(instanceId)) {
    console.error(
      `Scenario ${scenarioId} is not applicable for instance ${instanceId}.`,
    );
    console.error(
      `Applicable instances: ${applicableInstances?.join(', ') ?? 'none'}`,
    );
    process.exit(1);
  }

  console.log(`Running Scenario ${scenarioId} on ${instanceId}...`);
  console.log(`  Mode: ${instanceConfig.mode}`);
  console.log(`  Improvements: ${instanceConfig.improvements}`);
  console.log();

  // Set environment
  for (const [key, value] of Object.entries(instanceConfig.env)) {
    process.env[key] = value;
  }

  // Install GraphQL interceptor for subgraph mode
  if (instanceConfig.mode === 'subgraph') {
    installGraphQLInterceptor();
  }
  resetCaptures();

  // Load scenario with instance-specific pluginPath
  const scenario = await loadScenario(scenarioId, instanceConfig.pluginPath);

  // Collect assertions for this scenario
  const assertions = getAssertionsForScenario(scenarioId);

  // Run scenario
  const startTime = performance.now();
  const driver = new ConversationDriver(instanceConfig, scenario);
  const metrics = await driver.runScenario();
  const durationMs = performance.now() - startTime;

  // Enrich metrics
  if (instanceConfig.mode === 'subgraph') {
    metrics.graphqlQueries = getGraphQLCaptures();
  }
  metrics.cacheEvents = analyzeCacheEvents(driver.getLogs(), metrics.injectionEvents);
  metrics.extractionEvents = analyzeExtractionEvents(
    driver.getLogs(),
    scenario.turns.length,
  );

  // Print summary
  console.log(`Completed in ${(durationMs / 1000).toFixed(1)}s`);
  console.log(`Turns: ${scenario.turns.length}`);
  console.log(`Hook invocations: ${metrics.hookInvocations.length}`);
  console.log(`Injection events: ${metrics.injectionEvents.length}`);
  console.log(`GraphQL queries: ${metrics.graphqlQueries.length}`);
  console.log();

  // Run assertions
  let passed = 0;
  let failed = 0;

  if (Object.keys(assertions).length === 0) {
    console.log('No assertions defined for this scenario.');
  } else {
    console.log('Assertions:');
    for (const [name, fn] of Object.entries(assertions)) {
      try {
        fn(metrics);
        console.log(`  PASS  ${name}`);
        passed++;
      } catch (err: unknown) {
        const message = err instanceof Error ? err.message : String(err);
        console.log(`  FAIL  ${name}`);
        console.log(`        ${message}`);
        failed++;
      }
    }
  }

  console.log();
  console.log(`Results: ${passed} passed, ${failed} failed`);

  // Write metrics JSON for inspection
  const dir = './results';
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
  const outputPath = `${dir}/${instanceId}-${scenarioId}-${Date.now()}.json`;
  fs.writeFileSync(outputPath, JSON.stringify(metrics, null, 2));
  console.log(`Metrics written to: ${outputPath}`);

  process.exit(failed > 0 ? 1 : 0);
}

main().catch((err) => {
  console.error('Fatal error:', err);
  process.exit(2);
});
