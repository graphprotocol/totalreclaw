-- TotalReclaw Database Schema
-- PostgreSQL schema for encrypted memory storage with vector embeddings
-- Requires pgvector extension for vector(1024) type

-- Drop existing tables
DROP TABLE IF EXISTS encrypted_vault CASCADE;

-- Main encrypted vault table
CREATE TABLE encrypted_vault (
    id SERIAL PRIMARY KEY,
    vault_id UUID NOT NULL,
    agent_id VARCHAR(255) NOT NULL,
    ciphertext BYTEA NOT NULL,
    nonce BYTEA NOT NULL,
    tag BYTEA NOT NULL,
    -- Vector embedding using pgvector (1024 dimensions)
    embedding vector(1024) NOT NULL,
    -- Blind indices for searchable encryption
    blind_indices TEXT[] NOT NULL DEFAULT '{}',
    -- OpenClaw compatibility fields
    source_file TEXT,
    source_type VARCHAR(50) CHECK (source_type IN ('MEMORY.md', 'memory-daily', 'imported')),
    chunk_index INTEGER DEFAULT 0,
    -- Metadata
    category VARCHAR(100),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Create indexes for efficient querying
CREATE INDEX idx_vault_id ON encrypted_vault(vault_id);
CREATE INDEX idx_agent_id ON encrypted_vault(agent_id);
CREATE INDEX idx_source_type ON encrypted_vault(source_type);
CREATE INDEX idx_source_file ON encrypted_vault(source_file);
CREATE INDEX idx_category ON encrypted_vault(category);
CREATE INDEX idx_created_at ON encrypted_vault(created_at DESC);

-- GIN index for blind indices array search
CREATE INDEX idx_blind_indices ON encrypted_vault USING GIN (blind_indices);

-- HNSW index for vector similarity search (pgvector)
CREATE INDEX idx_embedding_hnsw ON encrypted_vault USING hnsw (embedding vector_cosine_ops);

-- Composite index for routing queries
CREATE INDEX idx_routing ON encrypted_vault(vault_id, source_type, chunk_index);

-- Add comments for documentation
COMMENT ON TABLE encrypted_vault IS 'Encrypted memory chunks with vector embeddings for TotalReclaw';
COMMENT ON COLUMN encrypted_vault.vault_id IS 'User vault identifier';
COMMENT ON COLUMN encrypted_vault.agent_id IS 'Agent that created the memory';
COMMENT ON COLUMN encrypted_vault.ciphertext IS 'Encrypted content (XChaCha20-Poly1305)';
COMMENT ON COLUMN encrypted_vault.nonce IS 'XChaCha20 nonce for encryption';
COMMENT ON COLUMN encrypted_vault.tag IS 'Poly1305 authentication tag';
COMMENT ON COLUMN encrypted_vault.embedding IS '1024-dim vector embedding (pgvector)';
COMMENT ON COLUMN encrypted_vault.blind_indices IS 'Searchable blind indices';
COMMENT ON COLUMN encrypted_vault.source_type IS 'OpenClaw: MEMORY.md, memory-daily, or imported';
COMMENT ON COLUMN encrypted_vault.chunk_index IS 'OpenClaw: chunk sequence number';
