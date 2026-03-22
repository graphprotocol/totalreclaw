-- TotalReclaw Server Database Schema v0.3.1
-- PostgreSQL 16+

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============ Users Table ============
-- Stores authentication credentials for each user
-- auth_key_hash is SHA256(HKDF(recovery_phrase, salt, "totalreclaw-auth-v1"))
-- Server NEVER stores the recovery phrase or auth_key itself

CREATE TABLE users (
  user_id TEXT PRIMARY KEY,           -- UUIDv7 (time-sortable)
  auth_key_hash BYTEA NOT NULL,       -- SHA256(auth_key)
  salt BYTEA NOT NULL,                -- 32 bytes, used for HKDF derivation
  is_deleted BOOLEAN NOT NULL DEFAULT false,
  deleted_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  last_seen_at TIMESTAMPTZ
);

-- Index for fast auth lookups
CREATE INDEX idx_users_auth_hash ON users(auth_key_hash);

-- ============ Raw Events Table ============
-- Immutable log of all incoming events for audit/replay
-- Stores raw Protobuf bytes for future event sourcing

CREATE TABLE raw_events (
  id BIGSERIAL PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  event_bytes BYTEA NOT NULL,         -- raw Protobuf of StoreRequest
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index for efficient user event queries
CREATE INDEX idx_events_user ON raw_events(user_id, created_at DESC);

-- ============ Facts Table ============
-- Mutable view of memory facts
-- encrypted_blob contains AES-256-GCM ciphertext (doc + embedding + metadata)
-- blind_indices is an array of SHA-256 hashes for blind search

CREATE TABLE facts (
  id TEXT PRIMARY KEY,                -- fact UUIDv7
  user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  encrypted_blob BYTEA NOT NULL,
  blind_indices TEXT[] NOT NULL,      -- Array of hex-encoded SHA-256 hashes
  decay_score DOUBLE PRECISION NOT NULL DEFAULT 1.0,
  is_active BOOLEAN NOT NULL DEFAULT true,
  version INT NOT NULL DEFAULT 1,
  source TEXT NOT NULL,               -- conversation | pre_compaction | explicit
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  -- v0.3.1b: Content fingerprint dedup + sync
  sequence_id BIGSERIAL,              -- monotonic per-user, for delta sync
  content_fp TEXT,                    -- HMAC-SHA256 fingerprint for exact dedup
  agent_id TEXT                       -- which agent created this fact
);

-- Standard indexes
CREATE INDEX idx_facts_user ON facts(user_id);
CREATE INDEX idx_facts_active_decay ON facts(user_id, is_active, decay_score DESC);

-- GIN index for blind_indices array - enables fast ANY() queries
-- This is critical for LSH-based search performance
CREATE INDEX idx_facts_blind_gin ON facts USING GIN(blind_indices);

-- Composite index for common search pattern
CREATE INDEX idx_facts_search ON facts(user_id, is_active) WHERE is_active = true;

-- v0.3.1b indexes
-- Unique constraint: same user + same content fingerprint + active = duplicate
CREATE UNIQUE INDEX idx_facts_user_fp ON facts(user_id, content_fp) WHERE is_active = true;
-- Index for delta sync queries (sequence_id ordering)
CREATE INDEX idx_facts_user_seq ON facts(user_id, sequence_id);

-- ============ v0.3.1b Migration (for existing databases) ============
-- Run these ALTER statements AFTER initial schema creation for upgrades:
-- ALTER TABLE facts ADD COLUMN IF NOT EXISTS sequence_id BIGSERIAL;
-- ALTER TABLE facts ADD COLUMN IF NOT EXISTS content_fp TEXT;
-- ALTER TABLE facts ADD COLUMN IF NOT EXISTS agent_id TEXT;
-- CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_user_fp ON facts(user_id, content_fp) WHERE is_active = true;
-- CREATE INDEX IF NOT EXISTS idx_facts_user_seq ON facts(user_id, sequence_id);

-- ============ Tombstones Table ============
-- Soft delete tracking with 30-day retention policy
-- Allows for undo and audit trail

CREATE TABLE tombstones (
  fact_id TEXT PRIMARY KEY REFERENCES facts(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL,
  deleted_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index for cleanup job
CREATE INDEX idx_tombstones_expiry ON tombstones(deleted_at);

-- ============ Row-Level Security (Optional) ============
-- For multi-tenant deployments, enable RLS
-- ALTER TABLE facts ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE raw_events ENABLE ROW LEVEL SECURITY;

-- ============ Functions ============

-- Update timestamp trigger
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Apply trigger to facts table
CREATE TRIGGER trigger_facts_updated_at
  BEFORE UPDATE ON facts
  FOR EACH ROW
  EXECUTE FUNCTION update_updated_at();

-- ============ Subscriptions Table ============
-- NOTE: Subscriptions/billing have been moved to the private relay repo (totalreclaw-relay).
-- The self-hosted server does not need this table. It is retained as a comment for reference.
-- See totalreclaw-relay/src/db/migrations/002-subscriptions.sql for the relay schema.

-- ============ Cleanup Job (Optional) ============
-- For production, set up pg_cron or external job to:
-- 1. DELETE FROM tombstones WHERE deleted_at < NOW() - INTERVAL '30 days'
-- 2. DELETE FROM raw_events WHERE created_at < NOW() - INTERVAL '90 days'
