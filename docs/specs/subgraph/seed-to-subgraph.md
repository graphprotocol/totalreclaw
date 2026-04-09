<!--
Product: Subgraph
Formerly: tech specs/v0.3 (grok)/TS v0.3: Subgraphs and Account Abstraction (addition).md
Version: 1.0
Last updated: 2026-02-24
-->

# Full Technical Specification (Ready for Coding Agent)
# Technical Specification: Decentralized TotalReclaw v1.0  
**"Seed-to-Subgraph" — Fully E2EE, Account-Abstraction, Gasless, Decentralized Memory for OpenClaw**

**Version:** 1.0 (Complete for Coding Agent)  
**Date:** February 21, 2026  
**Target:** OpenClaw skill + self-hosted server + Base L2 (or cheapest L2)  
**Core Principle:** The user’s 12-word master seed is the **only** secret. It derives both the encryption key **and** the on-chain identity. All gas is sponsored by your paymaster. Users never see wallets, gas, or crypto UX.

---

## 1. Architecture Overview
```
OpenClaw Skill (client)
↓
Seed → BIP-39 → EOA → ERC-4337 Smart Account (counterfactual)
↓
Encrypt + Protobuf + blind indices → UserOperation
↓
POST to your Paymaster/Bundler endpoint (simple JSON)
↓
Your server sponsors gas → Bundler submits to Base
↓
EventfulDataEdge.fallback() → emit Log(encryptedProtobuf)
↓
Subgraph indexes event by Smart Account address
↓
Any agent queries subgraph by address → decrypts with seed

````
**User experience:**
- Install skill → auto-generates 12-word seed (or import existing).
- User only ever sees: “Backup this 12-word phrase”.
- All writes are gasless.
- Recovery on any device/agent: paste seed → full memory restored.


---

## 2. Seed & Account Abstraction (ERC-4337)

- Use `bip39` + `viem`/`ethers` in the skill.
- Derivation path: `m/44'/60'/0'/0/0` (standard Ethereum).
- Convert EOA to ERC-4337 Smart Account (use ZeroDev, Pimlico, or Stackup SDK — counterfactual address, zero deployment cost).
- The Smart Account address = the permanent user identifier used in subgraph queries.

---

## 3. Data Models & Serialization

**Protobuf schema** (`totalreclaw.proto`):
```proto
message TotalReclawFact {
  string id = 1;                    // UUIDv7
  string timestamp = 2;
  string fact_text = 3;
  string type = 4;
  int32 importance = 5;
  float decay_score = 6;
  repeated Entity entities = 7;
  repeated Relation relations = 8;
  // ... all fields from your existing spec
}

message Entity { string id = 1; string name = 2; string type = 3; }
message Relation { string subject_id = 1; string predicate = 2; string object_id = 3; float confidence = 4; }
```
Client serializes to Uint8Array → sends as calldata in UserOp.


## 4. Client-Side Flow (OpenClaw Skill)
On every store / update / decay / eviction:

1. Encrypt payload (XChaCha20-Poly1305 with seed-derived key).
2. Serialize to Protobuf.
3. Build ERC-4337 UserOperation (target = EventfulDataEdge address, calldata = encrypted bytes).
4. POST to your server /relay endpoint (simple JSON, no wallet UI).
5. On success → optimistic UI update + cache locally.

**Recovery flow:**

- Paste seed → regenerate Smart Account address.
- Query subgraph: facts(where: {owner: $address, isActive: true}) { ... }
- Download all events → decrypt → rebuild local graph + facts.


## 5. Server-Side Components (You Host)
### 5.1 Paymaster + Bundler

- Use Pimlico or Stackup (both have excellent 2026 self-hosted + managed options).
- Deploy a simple Paymaster contract (verifies UserOp signature from your Smart Account).
- Sponsor 100% of gas (you pay ~$0.0002–0.0005 per write).
- Rate-limit per Smart Account to prevent abuse.

### 5.2 EventfulDataEdge Deployment

- Deploy the EventfulDataEdge.sol contract once on Base (or chosen L2).
- Address becomes constant in the skill.

### 5.3 Subgraph (You Host Your Own Graph Node / Indexer)

- Schema with rich entities/relations (same as your earlier spec).
- Mapping (AssemblyScript):
```TypeScriptexport function handleLog(event: EventfulDataEdge.Log): void {
  const fact = decodeProtobuf<TotalReclawFact>(event.params.data);
  let entity = FactEntity.load(fact.id) || new FactEntity(fact.id);
  entity.owner = event.transaction.from;   // Smart Account address
  entity.factText = fact.factText;
  entity.decayScore = fact.decayScore;
  entity.isActive = fact.decayScore >= 0.3;
  // ... map relations, blind indices for search
  entity.save();
}
```
Hosted on your own Graph Node (or use The Graph’s decentralized network).


## 6. Cost & Billing Reality

- Per typical OpenClaw user: 5–15 writes/day → $0.0002 – $0.0007/day.
- 10,000 active users: ~$2-7/day (easily covered by the $5/month Pro tier or absorbed as marketing).
- You can add an optional “pay-your-own-gas” mode for ultra-heavy users.


## 7. Implementation Order (for Coding Agent)

1. Client seed → Smart Account derivation + UserOp builder.
2. Protobuf schema + encode/decode (client + subgraph).
3. EventfulDataEdge deployment script.
4. Server paymaster/bundler endpoint (/relay).
5. Subgraph schema + mapping (with Protobuf decoder).
6. Full recovery flow + tests.
7. Optional: rate-limiting, analytics, premium tier.