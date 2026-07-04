# Deployment Runbook (canonical)

**This is the version-controlled source of truth for deploying TotalReclaw.** The
machine-local `deploy-totalreclaw` skill is a convenience wrapper that should
mirror this file — if they ever disagree, **this file wins** (it's the one that
travels with the repo + gets PR-reviewed).

> Why this exists: the deploy procedure used to live only in gitignored,
> machine-local skill files. It drifted (a dangerous staging↔production service
> mapping error sat there uncaught) and couldn't be PR-reviewed. Keeping the
> runbook here fixes that.

---

## 1. Service ↔ environment ↔ domain map

**Verified 2026-05-31 via `railway domain -s <service>`.** Re-verify if anything
feels off — trust the `api-staging.` / `api.` custom domains, NOT the
`.up.railway.app` names.

| Railway service | Environment | Custom domain | relay `TOTALRECLAW_ENV` |
|---|---|---|---|
| `totalreclaw` | **STAGING** | `api-staging.totalreclaw.xyz` | `staging` |
| `totalreclaw-production` | **PRODUCTION** | `api.totalreclaw.xyz` | `production` |

- `railway up -s totalreclaw` → **STAGING**.
- `railway up -s totalreclaw-production -d` → **PRODUCTION**.

### ⚠️ Naming trap
The **staging** service `totalreclaw` also carries a Railway-internal domain
literally named `totalreclaw-production.up.railway.app`. That subdomain is on the
*staging* service and does NOT make it production. A prior skill version got this
backwards ("`railway up -s totalreclaw` targets PRODUCTION" — **false**). When in
doubt: `railway domain -s totalreclaw` shows `api-staging.` → it's staging.

---

## 2. Chain routing (current — dual-chain)

Verified 2026-05-31 via `railway variables`:

| Tier | Chain | `/health`-adjacent env |
|---|---|---|
| Free | Base Sepolia (84532) | `PIMLICO_CHAIN_ID=84532` |
| Pro | Gnosis (100) | `PRO_PIMLICO_CHAIN_ID=100` |

Single-chain Gnosis (both tiers) is the **target**, tracked as ops-1
(`totalreclaw-internal#283`) — NOT yet shipped. Clients should read `chain_id`
from `GET /v1/billing/status` (authoritative as of `totalreclaw-relay` PR #21),
not hardcode the tier→chain map.

---

## 3. `/health` contract

The TypeScript relay returns:

```json
{ "status": "ok", "service": "totalreclaw-relay", "version": "<7-char-sha>" }
```

`version` = `process.env.RAILWAY_GIT_COMMIT_SHA.slice(0,7) || "dev"`
(`src/routes/health.ts`). So:
- A **GitHub-integration** deploy stamps the 7-char commit SHA.
- A **local `railway up`** does NOT auto-set the SHA → `version: "dev"` unless you
  inject it (see §5 Step 2).

> This is NOT the old FastAPI shape (`{"status":"healthy","version":"0.3.1","database":"connected"}`).
> The relay is TypeScript; that shape is retired.

**⚠️ A stale/`dev` SHA does NOT prove "not deployed"** (2026-07-02 incident: prod ran
relay-main code while `/health` still reported an old SHA — the deployer had skipped the
Step 2/4 `RAILWAY_GIT_COMMIT_SHA` injection). To verify what code is actually live,
**probe a route that only exists in the new build** (e.g. `POST /v1/billing/topup` →
401 auth-gate = route exists; 404 = old build). Conversely, never claim "deployed" from
the SHA alone after a CLI deploy.

---

## 4. Deploy-SHA sentinel (catch silent skips)

`totalreclaw-relay/.github/workflows/deploy-sha-sentinel.yml` runs every 15 min +
on push to main. It asserts:
- staging `/health.version` == `origin/main` short SHA (alerts on `dev` / stale),
- prod `/health.version` is a real SHA, never `dev`.

**Staging GitHub auto-deploy is unreliable — it has silently SKIPPED merges**
(relay PR #21, 2026-05-31). If the sentinel alerts (or staging shows `dev`/stale),
force a clean deploy per §5 Step 2. Root-cause of the auto-deploy skip lives in
the Railway dashboard GitHub-trigger config (check: "wait for CI" toggle, watch
paths, GitHub app connection).

---

## 5. Procedure

### Step 0 — CI is a hard gate
```bash
cd ~/Documents/code/totalreclaw-relay
gh run list --branch main --limit 1 --json conclusion --jq '.[0].conclusion'  # must be "success"
```
**Never deploy on red CI.**

### Step 1 — Staging (normal path = GitHub auto-deploy on merge to relay `main`)
After merge, the sentinel (or a manual check) confirms staging caught up:
```bash
curl -s https://api-staging.totalreclaw.xyz/health | jq -r .version   # want origin/main short SHA
```

### Step 2 — Staging (forced, when auto-deploy skipped or you need it now)
```bash
cd ~/Documents/code/totalreclaw-relay && git checkout main && git pull --ff-only
SHA=$(git rev-parse --short HEAD)
railway variables --set "RAILWAY_GIT_COMMIT_SHA=$SHA" -s totalreclaw --skip-deploys
railway up -s totalreclaw --detach
# verify
[ "$(curl -s https://api-staging.totalreclaw.xyz/health | jq -r .version)" = "$SHA" ] \
  && echo "staging OK on $SHA" || echo "FAIL: staging not on $SHA"
```

### Step 3 — E2E against staging (gate before prod)
```bash
cd ~/Documents/code/totalreclaw/tests/e2e-batch
RELAY_URL=https://api-staging.totalreclaw.xyz npx tsx batch-e2e.ts --test A --test C --test E
```

### Step 4 — Promote to production (manual; never auto)
```bash
cd ~/Documents/code/totalreclaw-relay && git checkout main && git pull --ff-only
SHA=$(git rev-parse --short HEAD)
railway variables --set "RAILWAY_GIT_COMMIT_SHA=$SHA" -s totalreclaw-production --skip-deploys
railway up -s totalreclaw-production -d
# HARD GATE — prod must report the SHA we shipped, never "dev":
PROD_VER=$(curl -s https://api.totalreclaw.xyz/health | jq -r .version)
[ "$PROD_VER" = "$SHA" ] || { echo "FAIL: prod version=$PROD_VER expected=$SHA"; exit 1; }
```

---

## 6. Subgraph (post-ops-1 — single-chain Gnosis, isolated staging)

Two deployments, two manifests. The default `subgraph.yaml` is a STALE base-sepolia
manifest — never deploy from it.

```bash
cd ~/Documents/code/totalreclaw/subgraph
# STAGING (isolated DataEdge 0xE7a4…):
npm run deploy:staging   # = graph deploy --studio total-reclaw-gnosis-staging subgraph-gnosis-staging.yaml
# PRODUCTION (DataEdge 0xC445…):
npm run deploy:prod      # = graph deploy --studio total-reclaw-gnosis subgraph-gnosis-mainnet.yaml
```
Pass `--version-label v<X.Y.Z>` (bump from the currently-served version).

**After ANY subgraph deploy, repoint BOTH endpoint env vars on the matching relay
service** — each service runs `SUBGRAPH_ENDPOINT` AND `PRO_SUBGRAPH_ENDPOINT` (post-ops-1
both point at the same subgraph per env):
```bash
# staging:
railway variables --set "SUBGRAPH_ENDPOINT=https://api.studio.thegraph.com/query/41768/total-reclaw-gnosis-staging/<v>" \
                  --set "PRO_SUBGRAPH_ENDPOINT=https://api.studio.thegraph.com/query/41768/total-reclaw-gnosis-staging/<v>" -s totalreclaw
# production:
railway variables --set "SUBGRAPH_ENDPOINT=https://api.studio.thegraph.com/query/41768/total-reclaw-gnosis/<v>" \
                  --set "PRO_SUBGRAPH_ENDPOINT=https://api.studio.thegraph.com/query/41768/total-reclaw-gnosis/<v>" -s totalreclaw-production
```

### Dead-shard failure mode (Graph Studio, seen 2026-07-02)
A Studio deployment can die **server-side**: queries return
`Store error: shard not found: shard_bb` — including the `_meta` query itself. Diagnostic
tell: a healthy-but-stalled subgraph still answers `_meta` (with an old block number);
a dead shard **errors outright**. Our staging v0.6.1 first stalled (~June 6) then died
this way while prod ran identical mapping code healthily — it is a Studio infra failure,
NOT a handler bug. **Fix (~10 min): redeploy with a bumped version label + repoint the
env vars above.** All events re-index from chain; nothing is lost. Do not spend time
debugging mappings first.

## 6.5 Stripe env pairing (mode invariant)

A Stripe object (price/product/customer) exists in exactly ONE mode. The pairing
invariant per relay service:

| Service | Key mode | Object IDs must be |
|---|---|---|
| `totalreclaw` (staging) | `sk_test_…` | created in **test mode** |
| `totalreclaw-production` | `rk_live_…` / `sk_live_…` | created in **live mode** |

Copying prod price IDs into staging env vars (or vice versa) yields opaque
`CHECKOUT_FAILED` responses (2026-07-04 incident: `TOTALRECLAW_TOPUP_PRICE_*` on staging
held live-mode IDs → every top-up checkout failed until test-mode twins were created).
When adding any new Stripe-backed feature: create the objects TWICE (test + live) and
set the mode-matching ID in each service.

---

## 7. Rollback
```bash
cd ~/Documents/code/totalreclaw-relay
railway deployment list -s totalreclaw-production | head
railway redeploy -s totalreclaw-production    # redeploys current; for an older one, redeploy by id
```

---

## 8. Package publishing
All publishing goes through GitHub Actions (`gh workflow run ...`). **Never
publish manually.** CI must be green first. Order: Core → MCP → Plugin (ClawHub)
→ Python. Verify each with `npm view` / `pip install --dry-run`. See the
`deploy-totalreclaw` skill for the per-workflow commands.

---

## Key rules
- CI green is a hard gate (staging AND prod).
- `railway up -s totalreclaw` = STAGING; `-s totalreclaw-production` = PRODUCTION. Trust custom domains, not `.up.railway.app` names.
- Production NEVER auto-deploys.
- Always assert `/health.version` == the SHA you shipped. `dev` = SHA not stamped.
- E2E gates prod promotion.
- After a subgraph deploy, update `SUBGRAPH_ENDPOINT` on both relay services.
