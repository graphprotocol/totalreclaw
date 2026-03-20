<!--
Product: TotalReclaw
Formerly: tech specs/TotalReclaw-PRD.md
Version: 1.0.0
Last updated: 2026-02-24
-->

# Product Requirements Document: TotalReclaw

**Version:** 1.0.0
**Status:** Draft
**Last Updated:** February 18, 2026

---

## 1. Executive Summary

### 1.1 Product Vision

TotalReclaw is an end-to-end encrypted memory vault for AI agents that provides true cross-device sync and data portability. It is the "password manager for AI memory" — enabling users to maintain a single, portable memory that works across all their AI agents (OpenClaw, Claude Desktop, and any MCP-compatible tool).

### 1.2 The Problem

AI agents create powerful, personalized memories that improve over time. However, the current landscape creates data silos:

- **Hosted OpenClaw instances** (Railway, Vercel, Render) each have isolated local memory
- **Platform-specific memory** — ChatGPT memories don't sync with Claude; Claude Desktop doesn't talk to OpenClaw
- **Vendor-controlled storage** — your memories are held hostage by whoever hosts your agent
- **No portable export** — switching agents means starting from zero

The result: a fragmented digital brain scattered across different companies' servers.

### 1.3 Our Solution

TotalReclaw is a universal memory layer for AI agents with three core promises:

1. **Encrypted** — End-to-end encrypted. We can never read your memories.
2. **Portable** — One-click plain-text export. Leave anytime, no lock-in.
3. **Universal** — Works across OpenClaw, Claude Desktop, and any MCP-compatible agent.

---

## 2. Market Analysis

### 2.1 Problem Validation

The AI memory lock-in problem is real and already emerging:

**Evidence from Research:**
- **Reddit r/SaaS (Feb 2026):** "Are we seeing the early formation of an OpenClaw ecosystem? More deployments happening on Vercel, Railway, Render..."
- **Guild.ai:** "Your AI memory is vendor-locked. Each platform independently solved the same problem — creating accidental lock-in by design."
- **The Dr. Center:** "What's needed is a neutral, portable memory layer that travels with the person whose memory it is."
- **Zep:** Published "The Portable Memory Wallet Fallacy" arguing portable memory faces "insurmountable" challenges — proving they see this as a competitive threat.

**Current Fragmentation:**
| Platform | Memory Approach | Lock-in |
|---------|----------------|---------|
| Railway OpenClaw | Local Markdown files | Trapped on Railway |
| Vercel OpenClaw | Local Markdown files | Trapped on Vercel |
| Mem0 | Cloud storage | Vendor holds data |
| openclaw-engram | Local-first | No cross-device sync |
| QMD | Local-first | No cross-device sync |

### 2.2 Competitive Landscape

| Solution | Strengths | Weaknesses |
|----------|----------|------------|
| **Local-First** (engram, QMD, memsearch) | Privacy, accuracy, no vendor | No cross-device sync, requires technical setup, single-device only |
| **Hosted** (Mem0, platform-hosted) | Easy to use, instant value | Vendor lock-in, they can read your data, switching = starting over |
| **TotalReclaw** | Encrypted + portable + cross-device sync | New to market, requires local client install |

**The Gap:** No one offers encrypted cross-device sync with guaranteed data portability.

### 2.3 Target Market

**Primary Segments:**

1. **Non-technical hosted OpenClaw users** (Segment A)
   - Deployed OpenClaw on Railway/Vercel because it was easy
   - Their memory is locked to that instance
   - Want: Portable memories without losing the convenience of hosting

2. **Power users with multiple agents** (Segment B)
   - Run OpenClaw locally + Claude Desktop + other agents
   - Memory is fragmented across tools
   - Want: One memory that works across all their agents

**Secondary Segment (Long-term):**
- **Teams/Enterprises** — Shared memory, admin controls, SSO, audit logs
- **IoT/constrained devices** — Can't run local crypto, need lightweight client

### 2.4 Market Size Indicators

- **189K+ GitHub stars** for OpenClaw (and growing rapidly)
- **Dozens of hosting platforms** already offering one-click OpenClaw deployment
- **8+ major articles** published on AI memory portability and vendor lock-in
- **Multiple competing solutions** (Mem0, Zep, engram) proving market demand

---

## 3. Product Requirements

### 3.1 Core Features (MVP)

#### FR-1: End-to-End Encryption
- All memories encrypted client-side before leaving the device
- AES-GCM for data encryption
- HKDF (HMAC-based Key Derivation) for key derivation from master password
- Server stores only ciphertext + embeddings
- Server can never decrypt user data

#### FR-2: Cross-Device Sync
- Automatic sync across all user devices
- Conflict resolution for concurrent writes
- Incremental sync (only transmit changes)

#### FR-3: Universal Agent Compatibility
- **OpenClaw Skill (npm package)** — Overrides default local memory commands
- **MCP Server** — Local stdio process for Claude Desktop, ChatGPT Desktop
- **REST API** — For custom agents and enterprise integrations

#### FR-4: Data Export (Anti-Vendor Lock-in)
- One-click export to plain-text Markdown
- Option for encrypted export (for backup/transfer)
- All memories included, no data left behind

#### FR-5: Hybrid Search
- Two-pass retrieval: Remote vector search → Local BM25 reranking → RRF fusion
- Blind indices for exact-match queries (emails, API keys, IDs)
- Competitive accuracy with end-to-end encrypted privacy

### 3.2 Technical Architecture

**Client-Side (Local Node):**
- Argon2id KDF → Data Key + Blind Key
- ONNX all-MiniLM-L6-v2 (INT8) for local vectorization
- AES-GCM encryption
- HMAC-SHA256 blind indices
- Local BM25 reranking
- OS Keychain integration for master password storage

**Server-Side (TotalReclaw SaaS):**
- PostgreSQL + pgvector for storage
- Stores: ciphertext, embeddings, blind indices
- mTLS for secure communication
- End-to-end encrypted — server never sees plaintext

**Search Flow:**
```
Pass 1 (Server, ~100ms): Query vector → HNSW KNN search → Top 250 matches (ciphertext)
Pass 2 (Client, ~500ms): Decrypt → BM25 on plaintext → RRF fusion → Top 3-5 results
```

### 3.3 Non-Functional Requirements

| Requirement | Specification |
|-------------|---------------|
| **Security** | End-to-end encryption, mTLS, no plaintext on server disk |
| **Privacy** | GDPR compliant, right to deletion, data minimization |
| **Performance** | Search latency < 1 second, sync latency < 5 seconds |
| **Availability** | 99.9% uptime SLA for SaaS |
| **Scalability** | Support 10K concurrent users per region |
| **Portability** | Export must complete in < 60 seconds for 100K memories |

---

## 4. User Experience

### 4.1 Onboarding Flow

1. **Install:** `npm install -g @totalreclaw/cli` or download macOS/Windows app
2. **Create account:** Generate API token (or skip for local-only mode)
3. **Set master password:** Used to derive encryption keys (stored in OS Keychain)
4. **Install agent integration:** OpenClaw skill OR MCP server
5. **Verify:** Agent writes test memory, confirms sync works

### 4.2 Key User Journeys

**Journey 1: Cross-Device Sync**
1. User has OpenClaw on desktop and laptop
2. Both devices configured with same TotalReclaw account
3. User adds memory on desktop → automatically syncs to laptop
4. User switches to laptop → memory immediately available

**Journey 2: Agent Switching**
1. User uses OpenClaw for work, Claude Desktop for coding
2. Both agents connected to same TotalReclaw vault
3. Memory added in OpenClaw → available in Claude Desktop
4. Unified context across all agents

**Journey 3: Data Export**
1. User decides to leave TotalReclaw
2. Runs `totalreclaw export` or clicks Export in UI
3. Receives plain-text Markdown files + folder structure
4. Imports into alternative system or keeps for backup

---

## 5. Long-Term Vision

### 5.1 Phase 1: Centralized SaaS (Current)

**Focus:** Fast go-to-market, prove the model

- Single-operator SaaS
- End-to-end encryption
- OpenClaw skill + MCP server
- REST API for custom integrations

### 5.2 Phase 2: The Graph Network

**Focus:** Decentralization, censorship resistance

- Migrate storage to The Graph Horizon Data Service
- Graph Indexers run TotalReclaw nodes
- No central point of failure
- GRT token for payments/incentives

**Benefits:**
- Censorship-resistant
- No central operator can be pressured to expose data
- Composable with other Web3 data
- Community-operated infrastructure

### 5.3 Phase 3: TDX Integration

**Focus:** Hardware-enforced security

- Intel Trust Domain Extensions (TDX) for isolated execution
- Enclave-based memory processing
- Hardware guarantees that even cloud providers can't access data

**Benefits:**
- Even stronger privacy guarantees
- Regulatory compliance advantages
- Enterprise trust through hardware security

---

## 6. Success Metrics

### 6.1 Key Performance Indicators (KPIs)

| Metric | Target (6 months) | Target (12 months) |
|--------|-------------------|--------------------|
| **Active Users** | 1,000 | 10,000 |
| **Weekly Active Rate** | 40% | 45% |
| **Memories Stored** | 1M | 100M |
| **Cross-Device Users** | 30% of users | 50% of users |
| **Export Rate** | <5% monthly (churn check) | <3% monthly |
| **Search Latency (p50)** | <800ms | <600ms |
| **Search Latency (p99)** | <1.5s | <1s |

### 6.2 Success Criteria

- **Technical:** E2EE property maintained (no plaintext on server, ever)
- **Product:** Export functionality works for 100% of memories
- **Business:** $10K MRR within 12 months
- **Ecosystem:** At least 3 different AI agent integrations live

---

## 7. Risks & Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| **AI companies add portable memory** | High | Move fast to establish brand, focus on E2EE differentiation |
| **Users don't care about privacy** | Medium | Emphasize portability benefit, make privacy invisible/automatic |
| **Technical complexity of E2EE** | Medium | Extensive testing, clear documentation, graceful fallbacks |
| **Competition from well-funded players** | High | Open-source client code, community trust, first-mover advantage |
| **Key management UX issues** | Medium | OS keychain integration, recovery flows, biometric options |

---

## 8. Open Questions

1. **Pricing model:** Freemium vs paid-only? What are the tier limits?
2. **Enterprise features:** What specific admin/SSO/compliance features do enterprises need?
3. **Mobile support:** Timeline for iOS/Android apps?
4. **API design:** GraphQL vs REST for Phase 2 (The Graph)?
5. **Regulatory compliance:** What certifications needed for enterprise (SOC2, HIPAA, etc.)?

---

## 9. Appendix: References

### 9.1 Competitive Research Sources

- **Railway:** "Deploy openclaw - Railway" — One-click OpenClaw hosting
- **Vercel:** "Running OpenClaw in Vercel Sandbox" — Vercel integration guide
- **Zep:** "The Portable Memory Wallet Fallacy" — Arguments against portable memory
- **Guild.ai:** "AI Agent Portability" — Accidental lock-in by design
- **The Dr. Center:** "When One Company Owns Your Memory" — Need for neutral memory layer

### 9.2 Technical Specifications

- **v0.2 E2EE Technical Specification** — Original encrypted memory design
- **v0.3 PRD (TDX & Horizon)** — Decentralized network architecture
- **v0.4 Technical Spec (TDX SaaS)** — TEE + LLM auto-enrichment

### 9.3 Related Work

- **openclaw-engram:** Local-first memory plugin with 10 categories, confidence tiers
- **Milvus memsearch:** OpenClaw memory extraction into standalone library
- **QMD:** Hybrid BM25 + vector + reranking search backend

---

**Document Control:**

| Version | Date | Changes | Author |
|---------|------|---------|--------|
| 1.0.0 | 2026-02-18 | Initial PRD based on stakeholder discussions | TotalReclaw Team |
