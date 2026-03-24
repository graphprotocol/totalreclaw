#!/usr/bin/env node
/**
 * Stub NanoClaw MCP server for E2E testing.
 * The real server lives in the NanoClaw container.
 * This stub provides a minimal MCP server that responds to initialize/list
 * and then idles (no tools registered).
 */
'use strict';

const readline = require('readline');

const rl = readline.createInterface({ input: process.stdin });

function send(msg) {
  const json = JSON.stringify(msg);
  process.stdout.write(`Content-Length: ${Buffer.byteLength(json)}\r\n\r\n${json}`);
}

rl.on('line', (line) => {
  // MCP uses Content-Length framing; for simplicity, try to parse JSON from each line
  try {
    const msg = JSON.parse(line);
    if (msg.method === 'initialize') {
      send({
        jsonrpc: '2.0',
        id: msg.id,
        result: {
          protocolVersion: '2024-11-05',
          capabilities: { tools: {} },
          serverInfo: { name: 'nanoclaw-stub', version: '0.0.1' },
        },
      });
    } else if (msg.method === 'notifications/initialized') {
      // No response needed for notifications
    } else if (msg.method === 'tools/list') {
      send({ jsonrpc: '2.0', id: msg.id, result: { tools: [] } });
    } else if (msg.id !== undefined) {
      // Unknown method with id — return empty result
      send({ jsonrpc: '2.0', id: msg.id, result: {} });
    }
  } catch {
    // Ignore non-JSON lines (Content-Length headers)
  }
});

// Keep process alive
process.stdin.resume();
