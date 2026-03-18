#!/bin/bash
set -e

echo "[entrypoint] Writing OpenClaw config (onboarding test — NO master password)..."

# NOTE: TOTALRECLAW_MASTER_PASSWORD is intentionally OMITTED to test the
# fresh onboarding flow where the agent guides the user through setup.
cat > /home/node/.openclaw/openclaw.json << ENDCONFIG
{
  "env": {
    "ZAI_API_KEY": "${ZAI_API_KEY}",
    "TOTALRECLAW_SERVER_URL": "${TOTALRECLAW_SERVER_URL}",
    "TOTALRECLAW_CHAIN_ID": "${TOTALRECLAW_CHAIN_ID}",
    "TOTALRECLAW_DATA_EDGE_ADDRESS": "${TOTALRECLAW_DATA_EDGE_ADDRESS}",
    "TOTALRECLAW_EXTRACT_EVERY_TURNS": "${TOTALRECLAW_EXTRACT_EVERY_TURNS:-5}"
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
      "token": "${OPENCLAW_GATEWAY_TOKEN}"
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

echo "[entrypoint] Config written (no master password). Starting OpenClaw gateway..."

exec node /app/dist/index.js gateway
