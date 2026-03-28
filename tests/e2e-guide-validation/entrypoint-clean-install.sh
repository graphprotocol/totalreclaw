#!/bin/bash
set -e

echo "[entrypoint] Writing OpenClaw config (clean install — NO TotalReclaw env vars)..."

# NOTE: No TOTALRECLAW_SERVER_URL, no TOTALRECLAW_RECOVERY_PHRASE,
# no TOTALRECLAW_CHAIN_ID, no TOTALRECLAW_DATA_EDGE_ADDRESS.
# This simulates a completely fresh install from ClawHub.
cat > /home/node/.openclaw/openclaw.json << ENDCONFIG
{
  "env": {
    "ZAI_API_KEY": "${ZAI_API_KEY}"
  },
  "agents": {
    "defaults": {
      "model": {
        "primary": "zai/glm-5"
      }
    }
  },
  "gateway": {
    "mode": "local",
    "port": 18789,
    "bind": "lan",
    "auth": {
      "mode": "token",
      "token": "${OPENCLAW_GATEWAY_TOKEN}",
      "scopes": ["operator.read", "operator.write"]
    },
    "http": {
      "endpoints": {
        "chatCompletions": {
          "enabled": true
        }
      }
    },
    "controlUi": {
      "dangerouslyDisableDeviceAuth": true
    }
  },
  "tools": {
    "allow": ["totalreclaw", "group:plugins"]
  },
  "plugins": {
    "allow": ["totalreclaw"],
    "load": {
      "paths": ["/opt/totalreclaw-npm/plugin"]
    },
    "slots": {
      "memory": "totalreclaw"
    },
    "entries": {}
  }
}
ENDCONFIG

echo "[entrypoint] Config written (no TotalReclaw env vars). Starting OpenClaw gateway..."

exec node /app/dist/index.js gateway
