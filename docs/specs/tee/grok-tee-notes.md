<!--
Product: TEE
Formerly: tech specs/v0.3 (grok)/Grok v0.3 TEE.md
Version: 0.3 (TEE Edition 1.0)
Last updated: 2026-02-24
-->

2. Technical Specification — TEE Edition (v1.0)
Title: TotalReclaw v1.0 TEE — Full-Hybrid Search Inside Confidential Enclave
Target platforms: AWS Nitro Enclaves or Intel TDX (Azure/Google Confidential VMs)
Key change: Server-side index lives entirely inside attested enclave → plaintext-level accuracy and speed while provider still cannot read data.
2.1 Architecture Overview

Data at rest: still XChaCha20-Poly1305 with user master key (never leaves client).
Search: client sends encrypted query + session attestation.
Enclave: decrypts query inside enclave, decrypts only the necessary vectors/docs on-the-fly or keeps hot index in enclave-protected RAM, runs full HNSW + BM25 + RRF inside enclave, returns only encrypted top-8 results.
No LSH needed. Full corpus hybrid search.

2.2 Enclave Setup (one-time)

Base image: Ubuntu 24.04 + Nitro/TDX SDK
Enclave binary: single Go/Rust binary (Qdrant compiled with TEE support or custom FAISS + Tantivy)
Remote attestation: every search request includes enclave quote verification (client library does this automatically).

2.3 Data Flow (TEE version)
Ingestion (client):

Same as crypto version but WITHOUT LSH.
Upload encrypted doc + encrypted embedding + blind indices (still kept for exact keyword fallback).

Search (client → TEE):

Client encrypts query embedding with master key.
Client calls https://tee.totalreclaw.xyz/search with encrypted query + attestation nonce.
Load balancer routes to healthy enclave.
Enclave: decrypts query inside TEE, performs full ANN + BM25 on decrypted-in-memory index, returns only encrypted top results.
Client decrypts final results.

2.4 Changes vs Crypto-Only

Remove LSH entirely.
Server now runs full faiss.IndexHNSW + Tantivy BM25 inside enclave.
Candidate pool = entire corpus (or sharded).
Accuracy = plaintext baseline (F1@5 ≈ 0.242 instead of 0.218).
Latency: 35–70 ms even at 10 M memories.
Cost: ~3–4× higher (enclave vCPU/RAM premium).

2.5 Security Guarantees

Host provider cannot read RAM, disk, or network inside enclave (Nitro/TDX hardware guarantee).
User can verify enclave measurement (PCR values) on every connection.
Forward secrecy: each session uses fresh session key derived from recovery phrase.