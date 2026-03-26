# Monitoring & Alerting Setup

Production monitoring for TotalReclaw using a Telegram bot, GitHub Actions health checks, and relay deep health endpoints.

## Architecture

```
                    Proactive Alerting
                    ==================
Relay (real-time)
  Registration handler → "New user registered"
  Stripe webhook       → "New Pro subscriber!"
  /health/deep         → status transition alerts (ok ↔ degraded ↔ down)
        │
        ▼
  Telegram Bot API → alerts chat

GitHub Actions (every 5 min)
  ├── GET /health           → shallow (relay alive?)
  ├── GET /health/deep      → DB + Pimlico + Subgraph
  └── GET totalreclaw.xyz   → website alive?
        │
        ▼ (on failure)
  Telegram Bot API → alerts chat

daily-backup.yml (03:00 UTC)
        │ (on failure)
        ▼
  Telegram Bot API → alerts chat

                    On-Demand Commands
                    ==================
Telegram Bot (long-polling, runs locally)
  /status      → pings production + staging /health/deep
  /backup      → checks latest GitHub Actions backup run
  /users       → total users, new this week, tier breakdown
  /mrr         → monthly recurring revenue + unit economics
  /conversions → free-to-pro conversion rate
  /overview    → combined summary of all the above
```

**Four layers of monitoring:**

1. **Real-time: Relay alerts** -- new user registrations, new Pro subscribers, and health status transitions push to Telegram instantly
2. **Proactive: GitHub Actions health check** -- runs every 5 minutes, pings production endpoints, sends Telegram alert on failure
3. **Proactive: Backup failure alerts** -- daily backup workflow sends Telegram notification if the backup fails
4. **On-demand: Telegram bot** -- send commands from your phone to check infrastructure and business metrics

## Endpoints

| Endpoint | Auth | Rate-Limited | Purpose |
|----------|:----:|:------------:|---------|
| `GET /health` | No | No | Shallow -- confirms the relay process is alive |
| `GET /health/deep` | No | No | Deep -- checks DB, Pimlico, Subgraph connectivity |

### Deep health response

```json
{
  "status": "ok",
  "service": "totalreclaw-relay",
  "version": "abc1234",
  "uptime": 86400,
  "checks": {
    "database": { "name": "database", "status": "ok", "latencyMs": 3 },
    "pimlico": { "name": "pimlico", "status": "ok", "latencyMs": 120 },
    "subgraph": { "name": "subgraph", "status": "ok", "latencyMs": 85 }
  }
}
```

Status values: `ok`, `degraded`, `down`

- HTTP 200 -- `ok` or `degraded`
- HTTP 503 -- `down` (DB is unreachable)

The deep health endpoint sends Telegram alerts on status transitions (ok -> degraded/down, or recovery) if `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set as Railway env vars.

## Telegram Bot Setup

### 1. Create the bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot`
3. Name: `TotalReclaw Alerts`
4. Username: `totalreclaw_alerts_bot` (must be unique -- pick your own)
5. Copy the **bot token** (looks like `7123456789:AAH...`)

### 2. Get your chat ID

Option A -- private chat:
1. Send `/start` to your new bot
2. Run:
   ```bash
   curl "https://api.telegram.org/bot<BOT_TOKEN>/getUpdates" | python3 -m json.tool
   ```
3. Look for `"chat": { "id": 123456789 }` -- that positive number is your chat ID

Option B -- group chat:
1. Create a Telegram group (e.g., "TotalReclaw Alerts")
2. Add the bot to the group
3. Send any message to the group
4. Run the getUpdates curl above
5. Look for `"chat": { "id": -100XXXXXXXXXX }` -- that negative number is the group chat ID

### 3. Test the bot

```bash
curl -X POST "https://api.telegram.org/bot<BOT_TOKEN>/sendMessage" \
  -H "Content-Type: application/json" \
  -d '{"chat_id": "<CHAT_ID>", "text": "Test alert from TotalReclaw monitoring"}'
```

### 4. Configure secrets

**GitHub Actions** (for proactive alerting):

Add to `totalreclaw-relay` repo secrets (`Settings > Secrets and variables > Actions`):

| Secret | Value |
|--------|-------|
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather |
| `TELEGRAM_CHAT_ID` | Chat/group ID |

**Railway** (for relay real-time alerts + deep health transitions):

Add the same `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` as Railway environment variables. This enables:
- New user registration alerts
- New Pro subscriber alerts
- Health status transition alerts on `/health/deep`

**Local bot** (for on-demand commands):

```bash
cd totalreclaw-internal/monitoring
cp .env.example .env
# Edit .env with your token, chat ID, and optionally ADMIN_API_KEY
npm install
npm start
```

## Telegram Bot Commands

The bot lives at `totalreclaw-internal/monitoring/telegram-bot.ts`. It uses long-polling (no webhook server needed).

### Infrastructure Commands

#### /status

Pings all endpoints in parallel and reports back:

```
All systems operational

Production
  Relay: OK (45ms)
  Status: ok | Uptime: 3d 14h
    database: ok (2ms)
    pimlico: ok (120ms)
    subgraph: ok (85ms)

Staging
  Relay: OK (52ms)
  Status: ok | Uptime: 1d 6h
    database: ok (3ms)
    pimlico: ok (130ms)
    subgraph: ok (90ms)

Website
  totalreclaw.xyz: OK (180ms)
```

#### /backup

Checks the latest daily-backup workflow run via the GitHub API. Requires `GITHUB_TOKEN`.

### Business Metrics Commands

All business metric commands require `ADMIN_API_KEY` to be set. The bot authenticates with the admin API using the `X-Admin-API-Key` header (no OTP flow needed).

#### /users

Total users, new this week/month, tier breakdown, quota alerts.

#### /mrr

Monthly recurring revenue, gas costs, operating margin, ARPU.

#### /conversions

Free-to-pro conversion rate.

#### /overview

Combined summary of users, revenue, conversions, and usage in one message.

## Admin API Key Auth

The admin API now supports an alternative authentication method for internal services: the `X-Admin-API-Key` header. If a request includes this header with the correct `ADMIN_API_KEY` value, JWT verification is skipped.

This is used by:
- The Telegram monitoring bot (for `/users`, `/mrr`, `/conversions`, `/overview` commands)
- Any future internal tooling that needs admin API access without the OTP flow

The API key is the same `ADMIN_API_KEY` already used for the OTP login flow. Timing-safe comparison is used.

## Real-time Relay Alerts

The relay sends Telegram notifications (fire-and-forget) on:

| Event | Message |
|-------|---------|
| New user registration | "New user registered (free tier)" |
| Checkout completed (Stripe) | "New Pro subscriber! (wallet: 0xABCD...1234)" |
| Health status transition | "TotalReclaw Relay DEGRADED" / "TotalReclaw Relay recovered" |

These require `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` as Railway env vars. If not set, alerts are silently skipped (no errors).

## GitHub Actions Workflows

### Health check (`health-check.yml`)

Location: `totalreclaw-relay/.github/workflows/health-check.yml`

- **Schedule**: Every 5 minutes
- **Checks**: Relay `/health`, relay `/health/deep`, website `totalreclaw.xyz`
- **Alert**: Sends Telegram message on any failure
- **Workflow fails**: If relay shallow health or deep health is unreachable/down

### Backup alerts (`daily-backup.yml`)

Location: `totalreclaw-relay/.github/workflows/daily-backup.yml`

- **Schedule**: Daily at 03:00 UTC
- **Alert**: Sends Telegram message if backup step fails

## Environment Variables Reference

### Relay (Railway)

| Variable | Required | Description |
|----------|:--------:|-------------|
| `TELEGRAM_BOT_TOKEN` | No | Bot token from BotFather. All Telegram alerts disabled if missing. |
| `TELEGRAM_CHAT_ID` | No | Telegram chat/group ID. All Telegram alerts disabled if missing. |

### Monitoring Bot (local)

| Variable | Required | Description |
|----------|:--------:|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from BotFather |
| `TELEGRAM_CHAT_ID` | No | Restrict bot to this chat only (security) |
| `GITHUB_TOKEN` | No | GitHub PAT for `/backup` command (Actions read scope) |
| `GITHUB_REPO` | No | GitHub repo override (default: `p-diogo/totalreclaw-relay`) |
| `ADMIN_API_KEY` | No | Admin API key for business metrics (`/users`, `/mrr`, etc.) |
| `ADMIN_API_URL` | No | Admin API URL override (default: `https://api.totalreclaw.xyz/admin/api`) |

### GitHub Actions Secrets

| Secret | Used By | Description |
|--------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | health-check.yml, daily-backup.yml | Bot token |
| `TELEGRAM_CHAT_ID` | health-check.yml, daily-backup.yml | Chat/group ID |
