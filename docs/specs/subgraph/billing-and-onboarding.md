<!--
Product: Subgraph (Go-Live)
Version: 1.0
Last updated: 2026-03-03
-->

# Billing & Onboarding Architecture — Subgraph Go-Live

**Version:** 1.0
**Date:** March 3, 2026
**Scope:** User payment, authentication, chain selection, and paymaster authorization for the decentralized (subgraph) product.

---

## 1. Design Principles

1. **Seed is everything** — The 12-word BIP-39 seed derives the encryption key, on-chain identity (Smart Account), AND the authentication credential. No API keys, no usernames, no passwords.
2. **Free tier first** — Users experience value before being asked to pay. Zero friction onboarding.
3. **Pragmatic hybrid** — Billing is centralized (Stripe). Data layer is decentralized (subgraph on Gnosis Chain). The relay server bridges both.
4. **Agent-driven UX** — The OpenClaw agent orchestrates checkout, activation detection, and error handling. Users never visit a separate dashboard.

---

## 2. Key Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| **Chain** | Gnosis Chain | $0.00076/fact (8-13x cheaper than Base), xDAI stablecoin gas (no volatility), Graph indexing rewards, 640GB archive (easy for indexers), permanent L1 storage |
| **Paymaster** | Pimlico or ZeroDev | Multi-chain support (incl. Gnosis), custom webhook for subscription gating. NOT Coinbase Paymaster (Base-only, no webhooks) |
| **Fiat payments** | Stripe Checkout | Agent-generated checkout URL. Card, Apple Pay, Google Pay |
| **Identity** | Wallet address (ERC-4337 Smart Account) | Derived from BIP-39 seed. Single identity for billing, auth, and on-chain |
| **Auth** | Wallet signature per request | No API keys. Seed-derived private key signs every relay request |
| **Indexing** | Subgraph on The Graph Network | Existing subgraph code, zero changes needed. Indexing rewards incentivize indexers on Gnosis |
| **Custom data service** | Not pursuing | Horizon framework not ready until Q4 2026. Substreams overkill for single-event contract |

---

## 3. Chain Selection: Gnosis Chain

### Why Gnosis

| Factor | Gnosis | Base | Alt-DA (Manta/Mantle) |
|--------|--------|------|----------------------|
| Cost per fact (9KB) | **$0.00076** | $0.006-0.010 | ~$0.002 |
| Gas token | **xDAI (stablecoin, $1.00)** | ETH (volatile) | ETH/MNT (volatile) |
| Graph indexing rewards | **Yes** | Yes | No |
| Archive node size | **640GB** | 2TB+ | Varies |
| Data permanence | **Permanent (L1)** | ~18 days (blobs) + permanent (calldata) | **30-day pruning (dealbreaker)** |
| EVM compatibility | Identical | Identical | Identical |
| Block time | 5s | 2s | 2s |

### Cost Projections (Power Users: 50 facts/day)

| Scenario | Write cost/mo | Query fees/mo | Total cost/mo | Revenue @ $5/mo | Net |
|----------|:---:|:---:|:---:|:---:|:---:|
| 100 power users | $114 | $1 | $115 | $500 | **+$385** |
| 1K power users | $1,140 | $28 | $1,168 | $5,000 | **+$3,832** |
| 10K power users | $11,400 | $298 | $11,698 | $50,000 | **+$38,302** |

**On Base, the same scenarios would lose money at scale. Gnosis is the only chain that makes this economically viable.**

### Indexer Economics

Query fees alone are small ($1-$298/mo). Indexers are primarily incentivized by **GRT indexing rewards** (protocol issuance), which are active on Gnosis. The subgraph needs curation signal (GRT staked on it) to attract indexers. The 640GB archive node is trivially small — any indexer can run it cheaply.

### Gnosis Tradeoffs

| Concern | Severity | Mitigation |
|---------|----------|------------|
| Smaller validator set | LOW | TotalReclaw stores encrypted data; chain compromise doesn't reveal plaintext |
| Less ecosystem visibility | LOW | Not building DeFi; need cheap reliable event emission |
| 5s block times | LOW | Memory writes aren't latency-sensitive |
| xDAI bridge risk | LOW | Running since 2018, battle-tested |

---

## 4. Identity & Authentication

- **Identity:** Wallet address (ERC-4337 Smart Account derived from BIP-39 seed via `m/44'/60'/0'/0/0`).
- **Auth mechanism:** Every relay request is signed by the seed-derived private key. The relay server verifies the signature — no API key needed.
- **Billing mapping:** `wallet_address → Stripe customer_id`.

---

## 5. Tier Structure

| Tier | Cost | Limits | Authorization |
|------|------|--------|---------------|
| **Free** | $0 | 500 memories/month, rate-limited reads | Relay checks quota by wallet address |
| **Pro** | $5/month | Unlimited memories, priority reads | Relay checks active subscription |

Free tier: 500 memories/month on Base Sepolia (testnet, trial). Pro tier: unlimited memories on Gnosis mainnet (permanent on-chain storage).

---

## 6. Payment Infrastructure

### 6.1 Fiat (Credit Card)

- **Provider:** Stripe Checkout
- **Flow:** Agent creates a Stripe Checkout session via TotalReclaw API → user clicks URL → pays (card, Apple Pay, Google Pay) → Stripe webhook confirms → relay activates subscription for wallet address.
- **Stripe customer ID:** Mapped to wallet address. Email collected by Stripe for receipts only (not a TotalReclaw account).

### 6.2 Subscription Status Table

```sql
CREATE TABLE subscriptions (
    wallet_address  TEXT PRIMARY KEY,
    tier            TEXT NOT NULL DEFAULT 'free',  -- 'free' | 'pro'
    source          TEXT,                          -- 'stripe'
    stripe_id       TEXT,
    expires_at      TIMESTAMPTZ,
    free_writes_used INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

On every relay request (write or read), the server checks:
1. Valid wallet signature? → reject if not
2. Active subscription OR free tier quota remaining? → reject if not
3. Forward to paymaster (writes) or Graph Network (reads)

---

## 7. Paymaster & Gas Sponsorship

### 7.1 Provider

**Pimlico or ZeroDev** (final selection TBD based on Gnosis-specific support and developer experience).

Both support:
- **Custom webhook policies** — relay server receives UserOp, checks subscription status, returns approve/reject. Zero UX impact.
- **100+ EVM chains** including Gnosis.
- **ERC-4337 standard** — compatible with existing Smart Account code.

### 7.2 Conditional Sponsorship Flow

```
User signs tx with seed-derived key
    → Relay receives signed UserOp
    → Relay forwards to Paymaster
    → Paymaster calls relay webhook: "Should I sponsor this UserOp?"
    → Relay checks: wallet_address has active subscription or free quota?
        → YES: return { sponsor: true }
        → NO: return { sponsor: false, reason: "upgrade_required" }
    → If sponsored: Bundler submits to Gnosis Chain
    → If rejected: Agent shows upgrade prompt to user
```

---

## 8. Onboarding Flow (End-to-End)

### 8.1 First-Time User (Free Tier)

```
1. Install TotalReclaw skill on OpenClaw
2. Skill generates 12-word BIP-39 seed
3. Agent: "Your recovery phrase is [12 words]. Write it down."
4. User starts using normally — free tier, no payment
5. Relay checks quota per wallet address
6. All writes sponsored by paymaster (within free tier limits)
```

### 8.2 Upgrade to Paid

```
1. User approaches free tier limit
2. Agent: "You've used X/Y free memories. Upgrade for $Z/month."
3. Agent creates Stripe Checkout URL
4. User clicks URL, completes payment in browser
5. Webhook fires → relay activates subscription
6. Agent detects activation: "You're all set."
```

### 8.3 Recovery on New Device

```
1. Install skill on new device/agent
2. Paste 12-word seed
3. Derive wallet address → query subgraph → decrypt all memories
4. Relay recognizes wallet address → existing subscription still active
5. Full memory restored, billing intact
```

---

## 9. Architecture Diagram

```
+------------------------------------------------------------------+
|                    USER (OpenClaw Agent)                          |
|  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────────────┐  |
|  │ BIP-39   │→│ Encrypt  │→│ Protobuf │→│ Sign UserOp    │  |
|  │ Seed     │  │ AES-GCM  │  │ Encode   │  │ (seed-derived) │  |
|  └──────────┘  └──────────┘  └──────────┘  └────────────────┘  |
+------------------------------------------------------------------+
                              │
                              ▼
+------------------------------------------------------------------+
|                  RELAY SERVER (You Host)                          |
|  ┌───────────────┐  ┌───────────────┐  ┌─────────────────────┐  |
|  │ Verify sig    │→│ Check sub    │→│ Forward to Paymaster │  |
|  │ (wallet auth) │  │ (free/paid?) │  │ (Pimlico/ZeroDev)   │  |
|  └───────────────┘  └───────────────┘  └─────────────────────┘  |
|  ┌───────────────┐  ┌───────────────┐                           |
|  │ Stripe        │                      ← Payment webhooks       |
|  │ Checkout      │                                               |
|  └───────────────┘                                               |
+------------------------------------------------------------------+
                              │
                    ┌─────────┴──────────┐
                    ▼                    ▼
+------------------------+  +----------------------------+
|   GNOSIS CHAIN         |  |   THE GRAPH NETWORK        |
|   EventfulDataEdge.sol |  |   Subgraph (GraphQL)       |
|   emit Log(bytes)      |  |   ← Indexes events         |
|   Gas: xDAI stablecoin |  |   ← Indexing rewards (GRT) |
|   ~$0.00076/fact       |  |   ← $2/100K queries        |
+------------------------+  +----------------------------+
```

---

## 10. Open Questions (Remaining)

| # | Question | Impact | Status |
|---|----------|--------|--------|
| 1 | Exact subscription price ($2 vs $5/mo) | Margin sizing | TBD — tune after launch |
| 2 | Free tier threshold (50 vs 100 vs 500 facts) | Conversion vs abuse | TBD — tune after usage data |
| 3 | Pimlico vs ZeroDev for paymaster | Developer billing model | TBD — evaluate Gnosis support |
| 4 | Batch writes to reduce per-fact cost | Gas efficiency | Future optimization |
| 5 | Embedding stored off-chain (IPFS) to reduce calldata | 49% gas reduction | Future optimization |
