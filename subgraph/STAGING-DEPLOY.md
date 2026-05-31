# Staging subgraph deploy runbook (ops-6)

Deploys `total-reclaw-gnosis-staging` — the isolated staging index for the
staging-only EventfulDataEdge on Gnosis. Part of the staging-chain-isolation
epic (`totalreclaw-internal#363`).

Manifest: [`subgraph-gnosis-staging.yaml`](./subgraph-gnosis-staging.yaml)
Indexes: staging DataEdge `0xE7a4D2677B686e13775Ba9092631089e35F0BB91`
(predicted — ops-5; verify against `contracts/deployed-addresses.json` →
`stagingGnosis.eventfulDataEdge` after broadcast).

## Prerequisites (BLOCKED until ops-5 broadcasts)

1. **ops-5 staging DataEdge broadcast on Gnosis.** The manifest `startBlock`
   is a PLACEHOLDER until the contract exists on-chain. Do not deploy the
   subgraph before the contract — it would index an empty address.
2. A `total-reclaw-gnosis-staging` subgraph created in Graph Studio (studio
   slug). Graph Studio deploy key (CLAUDE.md: `Graph Studio deploy key`).

## Steps (Monday, after broadcast)

```bash
cd subgraph

# 1. Set the real startBlock in subgraph-gnosis-staging.yaml to the
#    staging DataEdge deploy block (from the forge broadcast receipt or
#    contracts/deployed-addresses.json → stagingGnosis.blockNumber).

# 2. Authenticate to Graph Studio (one-time per machine):
graph auth <STUDIO_DEPLOY_KEY>

# 3. Build + deploy the staging manifest:
npm run build:staging
npm run deploy:staging          # → graph deploy --studio total-reclaw-gnosis-staging subgraph-gnosis-staging.yaml
#    bump the version label when prompted (e.g. v0.6.0 to match prod schema)

# 4. Wait for indexing to reach chainhead, then grab the query URL:
#    https://api.studio.thegraph.com/query/<id>/total-reclaw-gnosis-staging/<version>
```

## After deploy (ops-7/8 — relay env)

Point the **staging** relay (`totalreclaw` service) at the staging subgraph +
staging DataEdge. Per CLAUDE.md CI/CD rule, set both the free and pro vars to
the staging values (single-chain dry-run — both tiers on the one staging
DataEdge):

```bash
cd ../totalreclaw-relay
# Pro tier → staging Gnosis
railway variables set "PRO_SUBGRAPH_ENDPOINT=https://api.studio.thegraph.com/query/<id>/total-reclaw-gnosis-staging/<version>" -s totalreclaw
railway variables set "PRO_DATA_EDGE_ADDRESS=0xE7a4D2677B686e13775Ba9092631089e35F0BB91" -s totalreclaw
# Free tier → SAME staging Gnosis DataEdge (single-chain dry-run, ops-8)
railway variables set "PIMLICO_CHAIN_ID=100" -s totalreclaw
railway variables set "DATA_EDGE_ADDRESS=0xE7a4D2677B686e13775Ba9092631089e35F0BB91" -s totalreclaw
railway variables set "SUBGRAPH_ENDPOINT=https://api.studio.thegraph.com/query/<id>/total-reclaw-gnosis-staging/<version>" -s totalreclaw
```

**Touch the `totalreclaw` (staging) service ONLY. Never the `totalreclaw-production` service.** Setting vars triggers a Railway redeploy; verify `/health` after.

## Acceptance (ops-9)

Run the store-as-free → upgrade → recall-as-pro round-trip against
`api-staging.totalreclaw.xyz`; assert zero data loss. This is the dry-run
that de-risks the prod single-chain flip (ops-1). The imp-13 batched-cycle
E2E retargets to this staging subgraph and drops its `tier=pro` precondition.
