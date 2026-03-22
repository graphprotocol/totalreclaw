"""
SQLAlchemy models for TotalReclaw Server.
"""
from datetime import datetime
from typing import List, Optional
from sqlalchemy import (
    Column, String, LargeBinary, Float, Boolean, Integer,
    DateTime, BigInteger, ForeignKey, Text, Index, Sequence
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func, text


class Base(DeclarativeBase):
    """Base class for all models."""
    pass


class User(Base):
    """
    User model for authentication.

    Stores:
    - user_id: UUIDv7 identifier
    - auth_key_hash: SHA256 of the derived auth key (NOT the recovery phrase)
    - salt: Random 32 bytes used for HKDF derivation
    """
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    auth_key_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    salt: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now()
    )
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True
    )

    def __repr__(self) -> str:
        return f"<User(user_id={self.user_id})>"


# Sequence for facts.sequence_id — must be defined before the Fact model
# so that Base.metadata.create_all() creates it before the table.
facts_sequence_id_seq = Sequence("facts_sequence_id_seq", metadata=Base.metadata)


class Fact(Base):
    """
    Fact model for encrypted memory storage.

    Stores:
    - id: UUIDv7 fact identifier
    - encrypted_blob: AES-256-GCM ciphertext
    - blind_indices: Array of SHA-256 hashes for blind search
    - decay_score: Importance score (decreases over time)
    - version: For optimistic locking
    - content_fp: HMAC-SHA256 fingerprint for exact dedup (v0.3.1b)
    - sequence_id: Monotonic per-user ID for delta sync (v0.3.1b)
    - agent_id: Identifier of the agent that created this fact (v0.3.1b)
    """
    __tablename__ = "facts"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False
    )
    encrypted_blob: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    blind_indices: Mapped[List[str]] = mapped_column(
        ARRAY(Text),
        nullable=False
    )
    decay_score: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now()
    )
    # --- Added in v0.3.1b ---
    sequence_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        facts_sequence_id_seq,
        server_default=facts_sequence_id_seq.next_value(),
        nullable=True  # nullable for backward compat with existing rows
    )
    content_fp: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    agent_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # --- Added in PoC v2 (LSH + reranking) ---
    encrypted_embedding: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )  # AES-256-GCM encrypted embedding, hex-encoded. Server never decrypts.

    # Indexes
    __table_args__ = (
        Index("idx_facts_user", "user_id"),
        Index("idx_facts_active_decay", "user_id", "is_active", decay_score.desc()),
        Index("idx_facts_blind_gin", "blind_indices", postgresql_using="gin"),
        Index("idx_facts_search", "user_id", "is_active", postgresql_where=(is_active == True)),
        # v0.3.1b indexes
        Index("idx_facts_user_fp", "user_id", "content_fp", unique=True,
              postgresql_where=(is_active == True)),
        Index("idx_facts_user_seq", "user_id", "sequence_id"),
    )

    def __repr__(self) -> str:
        return f"<Fact(id={self.id}, user_id={self.user_id}, active={self.is_active})>"


class RawEvent(Base):
    """
    Raw event model for immutable audit log.

    Stores raw Protobuf bytes of incoming requests for:
    - Event sourcing / replay
    - Audit trail
    - Debugging
    """
    __tablename__ = "raw_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False
    )
    event_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<RawEvent(id={self.id}, user_id={self.user_id})>"


class Tombstone(Base):
    """
    Tombstone model for soft delete tracking.

    Records deleted facts for:
    - Undo capability
    - Audit trail
    - 30-day retention policy
    """
    __tablename__ = "tombstones"

    fact_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("facts.id", ondelete="CASCADE"),
        primary_key=True
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<Tombstone(fact_id={self.fact_id})>"
