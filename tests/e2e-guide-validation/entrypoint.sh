#!/bin/bash
set -e

echo "[entrypoint] Writing OpenClaw config..."

cat > /home/node/.openclaw/openclaw.json << ENDCONFIG
{
  "env": {
    "ZAI_API_KEY": "${ZAI_API_KEY}",
    "TOTALRECLAW_SERVER_URL": "${TOTALRECLAW_SERVER_URL}",
    "TOTALRECLAW_RECOVERY_PHRASE": "${TOTALRECLAW_RECOVERY_PHRASE}",
    "TOTALRECLAW_CHAIN_ID": "${TOTALRECLAW_CHAIN_ID}",
    "TOTALRECLAW_DATA_EDGE_ADDRESS": "${TOTALRECLAW_DATA_EDGE_ADDRESS}",
    "TOTALRECLAW_EXTRACT_EVERY_TURNS": "${TOTALRECLAW_EXTRACT_EVERY_TURNS:-1}"
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
      "paths": ["/opt/totalreclaw/plugin"]
    },
    "slots": {
      "memory": "totalreclaw"
    },
    "entries": {}
  }
}
ENDCONFIG

echo "[entrypoint] Config written. Starting OpenClaw gateway..."

exec node /app/dist/index.js gateway
