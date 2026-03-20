# TotalReclaw Production Environment Variables Checklist

> **Purpose**: Track all environment variables needed for deployment.
> **Status**: Collecting — fill in values before deploying.

---

## 1. Pimlico (ERC-4337 Bundler + Paymaster)

| Variable | Where Used | How to Get |
|----------|-----------|------------|
| `PIMLICO_API_KEY` | Relay server + Plugin | Sign up at [pimlico.io](https://dashboard.pimlico.io/) → Create project → Copy API key |
| `PIMLICO_WEBHOOK_SECRET` | Relay server | Pimlico dashboard → Sponsorship Policies → Create policy with webhook → Copy secret |
| `PIMLICO_CHAIN_ID` | Relay server | `100` for Gnosis mainnet (production), `84532` for Base Sepolia (staging) |
| `PIMLICO_BUNDLER_URL` | Relay server | `https://api.pimlico.io/v2/100/rpc` (production) |

**Setup steps**:
1. Create account at [pimlico.io](https://dashboard.pimlico.io/)
2. Create a new project
3. Copy the API key
4. Create a Sponsorship Policy:
   - Type: Webhook
   - Webhook URL: `https://<your-domain>/v1/relay/webhook/pimlico`
   - Copy the webhook secret
5. Note: Pimlico free tier allows ~100 UserOps/day — plenty for beta

---

## 2. The Graph Studio (Subgraph Indexing)

| Variable | Where Used | How to Get |
|----------|-----------|------------|
| `GRAPH_AUTH_TOKEN` | CLI only (deploy time) | [thegraph.com/studio](https://thegraph.com/studio/) → Create subgraph → Copy deploy key |

**Setup steps**:
1. Go to [thegraph.com/studio](https://thegraph.com/studio/)
2. Connect wallet (the deployer wallet: `0x30d37b26257e03942dFCf12251FC25e41ca38cA8`)
3. Create a new subgraph named `totalreclaw`
4. Copy the deploy key
5. Run: `cd subgraph && graph auth --studio <deploy-key>`
6. Run: `graph deploy --studio totalreclaw`

The Studio GraphQL endpoint will be something like:
`https://api.studio.thegraph.com/query/<id>/totalreclaw/version/latest`

---

## 3. Stripe (Fiat Payments)

| Variable | Where Used | How to Get |
|----------|-----------|------------|
| `STRIPE_SECRET_KEY` | Relay server | [dashboard.stripe.com](https://dashboard.stripe.com/) → Developers → API keys |
| `STRIPE_WEBHOOK_SECRET` | Relay server | Stripe dashboard → Developers → Webhooks → Add endpoint → Copy signing secret |
| `STRIPE_PRICE_ID` | Relay server | Stripe dashboard → Products → Create product → Copy price ID |

**Setup steps**:
1. Create Stripe account (or use existing)
2. Create a product: "TotalReclaw Pro" — $3/month recurring
3. Copy the `price_id` (looks like `price_1Abc123...`)
4. Go to Developers → API keys → Copy Secret key (starts with `sk_test_` for testing, `sk_live_` for production)
5. Go to Developers → Webhooks → Add endpoint:
   - URL: `https://<your-domain>/v1/billing/webhook/stripe`
   - Events: `checkout.session.completed`, `customer.subscription.updated`, `customer.subscription.deleted`, `invoice.payment_failed`
   - Copy the Signing secret (starts with `whsec_`)

---

## 4. Coinbase Commerce (Crypto Payments) — Optional for Beta

| Variable | Where Used | How to Get |
|----------|-----------|------------|
| `COINBASE_COMMERCE_API_KEY` | Relay server | [commerce.coinbase.com](https://commerce.coinbase.com/) → Settings → API keys |
| `COINBASE_COMMERCE_WEBHOOK_SECRET` | Relay server | Coinbase Commerce → Settings → Webhook subscriptions |

**Can skip for initial beta** — Stripe alone is sufficient.

---

## 5. Railway (Relay Server Hosting)

| Variable | Where Used | How to Get |
|----------|-----------|------------|
| `DATABASE_URL` | Relay server | Railway provisions this automatically when you add PostgreSQL |
| `DOMAIN` | Caddy / Cloudflare | Your chosen domain (e.g., `api.totalreclaw.xyz`) |

**Setup steps**:
1. Create account at [railway.app](https://railway.app/)
2. Create new project → Deploy from GitHub repo
3. Add PostgreSQL plugin (free tier: 1GB)
4. Railway auto-sets `DATABASE_URL`
5. Set all other env vars in Railway dashboard

---

## 6. Domain + Cloudflare

| Variable | Where Used | How to Get |
|----------|-----------|------------|
| `DOMAIN` | Server config | Register domain or use subdomain |
| `CORS_ORIGINS` | Server config | Your frontend URL(s) |

**Decision needed**: What domain will you use?
- Option A: `api.totalreclaw.xyz` (if you own `totalreclaw.xyz`)
- Option B: `totalreclaw.railway.app` (Railway auto-domain, free, no DNS setup)
- Option C: Something else?

For beta, Railway's auto-domain works fine (no Cloudflare needed). Add Cloudflare later for production.

---

## 7. Plugin Environment Variables (Beta Testers)

These go in each beta tester's OpenClaw `.env` or plugin config:

| Variable | Value | Notes |
|----------|-------|-------|
| `TOTALRECLAW_SELF_HOSTED` | `false` | Set to `true` for self-hosted mode (default: managed service) |
| `TOTALRECLAW_SERVER_URL` | `https://<your-domain>` | Relay server (for registration + billing) |
| `TOTALRECLAW_SUBGRAPH_ENDPOINT` | `https://api.studio.thegraph.com/query/<id>/totalreclaw/version/latest` | Graph Studio endpoint |
| `TOTALRECLAW_RELAY_URL` | `https://<your-domain>` | Same as SERVER_URL for now |
| `TOTALRECLAW_MASTER_PASSWORD` | (user's 12-word seed phrase) | User provides their own |
| `PIMLICO_API_KEY` | (shared beta key) | Same key for all beta testers |

---

## 8. Deployer Wallet

Already configured:
- Address: `0x30d37b26257e03942dFCf12251FC25e41ca38cA8`
- Private key: in your local `.env` (NEVER share)
- Contracts deployed on Base Sepolia (staging) and Gnosis mainnet (production)

For Gnosis mainnet (later):
- Same wallet, just fund with real xDAI (~0.1 xDAI sufficient)

---

## Summary: What You Need to Do

### Before deployment (required):
- [ ] Sign up for Pimlico → get API key
- [ ] Sign up for Graph Studio → get deploy key
- [ ] Sign up for Stripe → create product → get keys
- [ ] Sign up for Railway → create project
- [ ] Decide on domain name

### Optional for beta:
- [ ] Coinbase Commerce setup
- [ ] Cloudflare setup (can use Railway auto-domain for beta)

### Already done:
- [x] Contracts deployed (Base Sepolia staging + Gnosis mainnet production)
- [x] Deployer wallet configured
- [x] Server code written
- [x] Plugin code written
- [x] Subgraph code written
