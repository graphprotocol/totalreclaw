"""
Database connection and session management for TotalReclaw Server.
"""
import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    create_async_engine,
    async_sessionmaker
)
from sqlalchemy import text

from .models import Base, User, Fact, RawEvent, Tombstone
from ..config import get_settings


# Valid trapdoor: exactly 64 hex characters (SHA-256 output)
VALID_TRAPDOOR_RE = re.compile(r'^[0-9a-fA-F]{64}$')


def validate_trapdoors(trapdoors: list) -> list:
    """
    Validate and filter trapdoors to prevent SQL injection.

    Only allows valid hex SHA-256 hashes (exactly 64 hex characters).
    Invalid trapdoors are silently dropped.

    Args:
        trapdoors: List of candidate trapdoor strings

    Returns:
        List of validated trapdoor strings

    Raises:
        ValueError: If no valid trapdoors remain after filtering
    """
    valid = [t for t in trapdoors if VALID_TRAPDOOR_RE.match(t)]
    if trapdoors and not valid:
        raise ValueError(
            f"All {len(trapdoors)} trapdoors failed validation. "
            "Trapdoors must be 64-character hex strings (SHA-256)."
        )
    return valid


class Database:
    """
    Database manager for TotalReclaw Server.

    Handles connection pooling, session management, and common queries.
    """

    def __init__(self, database_url: Optional[str] = None):
        """
        Initialize database manager.

        Args:
            database_url: Database connection URL. If None, uses settings.
        """
        settings = get_settings()
        self.database_url = database_url or settings.database_url

        # Create async engine with connection pooling
        self.engine = create_async_engine(
            self.database_url,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            pool_recycle=settings.database_pool_recycle,
            pool_pre_ping=settings.database_pool_pre_ping,
            pool_timeout=settings.database_pool_timeout,
            echo=settings.debug  # Log SQL in debug mode
        )

        # Create session factory
        self.async_session = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False
        )

    async def init(self):
        """Create all tables if they don't exist."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self):
        """Close all connections."""
        await self.engine.dispose()

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        """
        Get a database session.

        Usage:
            async with db.session() as session:
                user = await session.execute(...)
        """
        async with self.async_session() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    # ============ User Operations ============

    async def create_user(self, user_id: str, auth_key_hash: bytes, salt: bytes) -> User:
        """
        Create a new user.

        Args:
            user_id: UUIDv7 user identifier
            auth_key_hash: SHA256 of auth key
            salt: Random 32 bytes for HKDF

        Returns:
            Created User object
        """
        async with self.session() as session:
            user = User(
                user_id=user_id,
                auth_key_hash=auth_key_hash,
                salt=salt
            )
            session.add(user)
            await session.flush()
            return user

    async def get_user_by_auth_hash(self, auth_key_hash: bytes) -> Optional[User]:
        """
        Look up user by auth key hash.

        Args:
            auth_key_hash: SHA256 of auth key

        Returns:
            User if found, None otherwise
        """
        async with self.session() as session:
            result = await session.execute(
                text("SELECT * FROM users WHERE auth_key_hash = :hash AND is_deleted = false"),
                {"hash": auth_key_hash}
            )
            row = result.fetchone()
            if row:
                return User(
                    user_id=row.user_id,
                    auth_key_hash=row.auth_key_hash,
                    salt=row.salt,
                    created_at=row.created_at,
                    last_seen_at=row.last_seen_at
                )
            return None

    async def get_user_by_id(self, user_id: str) -> Optional[User]:
        """
        Get user by ID.

        Args:
            user_id: User's UUID

        Returns:
            User if found, None otherwise
        """
        async with self.session() as session:
            result = await session.execute(
                text("SELECT * FROM users WHERE user_id = :user_id AND is_deleted = false"),
                {"user_id": user_id}
            )
            row = result.fetchone()
            if row:
                return User(
                    user_id=row.user_id,
                    auth_key_hash=row.auth_key_hash,
                    salt=row.salt,
                    created_at=row.created_at,
                    last_seen_at=row.last_seen_at
                )
            return None

    async def update_last_seen(self, user_id: str):
        """Update user's last_seen_at timestamp."""
        async with self.session() as session:
            await session.execute(
                text("UPDATE users SET last_seen_at = NOW() WHERE user_id = :user_id"),
                {"user_id": user_id}
            )

    async def user_exists(self, user_id: str) -> bool:
        """Check if active (non-deleted) user exists."""
        async with self.session() as session:
            result = await session.execute(
                text("SELECT 1 FROM users WHERE user_id = :user_id AND is_deleted = false"),
                {"user_id": user_id}
            )
            return result.fetchone() is not None

    # ============ Fact Operations ============

    async def store_fact(
        self,
        fact_id: str,
        user_id: str,
        encrypted_blob: bytes,
        blind_indices: list,
        decay_score: float,
        source: str,
        content_fp: str = None,
        agent_id: str = None,
        encrypted_embedding: str = None
    ) -> Fact:
        """
        Store a new fact.

        Args:
            fact_id: UUIDv7 fact identifier
            user_id: Owner's user ID
            encrypted_blob: XChaCha20-Poly1305 ciphertext
            blind_indices: List of SHA-256 hashes
            decay_score: Initial importance score
            source: Origin of fact (conversation, explicit, etc.)
            content_fp: HMAC-SHA256 content fingerprint for dedup (v0.3.1b)
            agent_id: Identifier of the creating agent (v0.3.1b)
            encrypted_embedding: XChaCha20-Poly1305 encrypted embedding, hex-encoded (PoC v2)

        Returns:
            Created Fact object
        """
        async with self.session() as session:
            # Use raw SQL INSERT to omit sequence_id, letting PostgreSQL's
            # nextval('facts_sequence_id_seq') default fire properly.
            # ORM session.add() would set sequence_id=None explicitly,
            # which bypasses the server default.
            result = await session.execute(
                text("""
                    INSERT INTO facts (id, user_id, encrypted_blob, blind_indices,
                                       decay_score, source, content_fp, agent_id,
                                       encrypted_embedding, is_active, version)
                    VALUES (:id, :user_id, :blob, :indices,
                            :decay, :source, :content_fp, :agent_id,
                            :encrypted_embedding, true, 1)
                    RETURNING id, user_id, encrypted_blob, blind_indices,
                              decay_score, is_active, version, source,
                              created_at, updated_at, sequence_id,
                              content_fp, agent_id, encrypted_embedding
                """),
                {
                    "id": fact_id,
                    "user_id": user_id,
                    "blob": encrypted_blob,
                    "indices": blind_indices,
                    "decay": decay_score,
                    "source": source,
                    "content_fp": content_fp,
                    "agent_id": agent_id,
                    "encrypted_embedding": encrypted_embedding,
                }
            )
            row = result.fetchone()
            fact = Fact(
                id=row.id,
                user_id=row.user_id,
                encrypted_blob=row.encrypted_blob,
                blind_indices=row.blind_indices,
                decay_score=row.decay_score,
                is_active=row.is_active,
                version=row.version,
                source=row.source,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            fact.sequence_id = row.sequence_id
            fact.content_fp = row.content_fp
            fact.agent_id = row.agent_id
            fact.encrypted_embedding = row.encrypted_embedding
            return fact

    async def find_fact_by_fingerprint(
        self,
        user_id: str,
        content_fp: str
    ) -> Optional[str]:
        """
        Check if an active fact with the given content fingerprint exists.

        Used for v0.3.1b content fingerprint dedup.

        Args:
            user_id: User's ID
            content_fp: HMAC-SHA256 content fingerprint

        Returns:
            Existing fact ID if duplicate found, None otherwise
        """
        async with self.session() as session:
            result = await session.execute(
                text("""
                    SELECT id FROM facts
                    WHERE user_id = :user_id
                      AND content_fp = :content_fp
                      AND is_active = true
                    LIMIT 1
                """),
                {"user_id": user_id, "content_fp": content_fp}
            )
            row = result.fetchone()
            return row.id if row else None

    async def get_facts_since_sequence(
        self,
        user_id: str,
        since_sequence: int,
        limit: int = 1000
    ) -> tuple:
        """
        Get facts for a user since a given sequence_id (v0.3.1b delta sync).

        Args:
            user_id: User's ID
            since_sequence: Return facts with sequence_id > this value
            limit: Maximum facts to return

        Returns:
            Tuple of (facts_list, latest_sequence_id, has_more)
        """
        async with self.session() as session:
            # Fetch limit+1 to check has_more
            result = await session.execute(
                text("""
                    SELECT id, user_id, encrypted_blob, blind_indices,
                           decay_score, is_active, version, source,
                           created_at, updated_at, sequence_id,
                           content_fp, agent_id, encrypted_embedding
                    FROM facts
                    WHERE user_id = :user_id
                      AND (sequence_id > :since_seq OR :since_seq = 0)
                    ORDER BY sequence_id ASC
                    LIMIT :fetch_limit
                """),
                {
                    "user_id": user_id,
                    "since_seq": since_sequence,
                    "fetch_limit": limit + 1
                }
            )
            rows = result.fetchall()

            has_more = len(rows) > limit
            if has_more:
                rows = rows[:limit]

            facts = []
            for row in rows:
                fact = Fact(
                    id=row.id,
                    user_id=row.user_id,
                    encrypted_blob=row.encrypted_blob,
                    blind_indices=row.blind_indices,
                    decay_score=row.decay_score,
                    is_active=row.is_active,
                    version=row.version,
                    source=row.source,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                )
                fact.sequence_id = row.sequence_id
                fact.content_fp = row.content_fp
                fact.agent_id = row.agent_id
                fact.encrypted_embedding = row.encrypted_embedding
                facts.append(fact)

            # Get latest sequence for this user
            seq_result = await session.execute(
                text("""
                    SELECT COALESCE(MAX(sequence_id), 0) as max_seq
                    FROM facts
                    WHERE user_id = :user_id
                """),
                {"user_id": user_id}
            )
            latest_seq = seq_result.fetchone().max_seq or 0

            return facts, latest_seq, has_more

    async def search_facts_by_blind_indices(
        self,
        user_id: str,
        trapdoors: list,
        max_candidates: int = 3000,
        min_decay_score: float = 0.0
    ) -> tuple:
        """
        Search facts using blind index GIN query.

        Uses PostgreSQL's array overlap operator (&&) with GIN index
        for fast retrieval of candidate facts.

        Args:
            user_id: User's ID
            trapdoors: List of blind trapdoors to match
            max_candidates: Maximum number of results
            min_decay_score: Minimum decay score filter

        Returns:
            Tuple of (list of matching Fact objects, total_candidates_matched int)
            total_candidates_matched is the count of ALL matching facts before
            the LIMIT is applied, useful for observability.

        Raises:
            ValueError: If all trapdoors are invalid
        """
        # Validate trapdoors to prevent SQL injection
        validated_trapdoors = validate_trapdoors(trapdoors) if trapdoors else []

        async with self.session() as session:
            # First, count total matching candidates (before LIMIT)
            count_query = text("""
                SELECT COUNT(*) as total
                FROM facts
                WHERE user_id = :user_id
                  AND is_active = true
                  AND decay_score >= :min_decay
                  AND blind_indices && CAST(:trapdoors AS text[])
            """)
            count_result = await session.execute(
                count_query,
                {
                    "user_id": user_id,
                    "min_decay": min_decay_score,
                    "trapdoors": validated_trapdoors
                }
            )
            total_matched = count_result.fetchone().total

            # Use parameterized array with CAST() function — no f-string interpolation.
            # NOTE: We use CAST(:trapdoors AS text[]) instead of :trapdoors::text[]
            # because the :: cast syntax conflicts with SQLAlchemy's named-parameter
            # parsing when using asyncpg (which uses positional $N parameters).
            query = text("""
                SELECT id, encrypted_blob, decay_score, created_at, version,
                       encrypted_embedding
                FROM facts
                WHERE user_id = :user_id
                  AND is_active = true
                  AND decay_score >= :min_decay
                  AND blind_indices && CAST(:trapdoors AS text[])
                ORDER BY decay_score DESC
                LIMIT :limit
            """)
            result = await session.execute(
                query,
                {
                    "user_id": user_id,
                    "min_decay": min_decay_score,
                    "limit": max_candidates,
                    "trapdoors": validated_trapdoors
                }
            )
            rows = result.fetchall()
            facts = []
            for row in rows:
                fact = Fact(
                    id=row.id,
                    encrypted_blob=row.encrypted_blob,
                    decay_score=row.decay_score,
                    created_at=row.created_at,
                    version=row.version
                )
                fact.encrypted_embedding = row.encrypted_embedding
                facts.append(fact)
            return facts, total_matched

    async def count_active_facts(self, user_id: str) -> int:
        """
        Count active (non-deleted) facts for a user.

        Args:
            user_id: User's ID

        Returns:
            Count of active facts
        """
        async with self.session() as session:
            result = await session.execute(
                text("""
                    SELECT COUNT(*) as cnt FROM facts
                    WHERE user_id = :user_id AND is_active = true
                """),
                {"user_id": user_id}
            )
            return result.fetchone().cnt

    async def get_fact_by_id(self, fact_id: str, user_id: str) -> Optional[Fact]:
        """Get a specific fact by ID for a user."""
        async with self.session() as session:
            result = await session.execute(
                text("SELECT * FROM facts WHERE id = :id AND user_id = :user_id"),
                {"id": fact_id, "user_id": user_id}
            )
            row = result.fetchone()
            if row:
                return Fact(
                    id=row.id,
                    user_id=row.user_id,
                    encrypted_blob=row.encrypted_blob,
                    blind_indices=row.blind_indices,
                    decay_score=row.decay_score,
                    is_active=row.is_active,
                    version=row.version,
                    source=row.source,
                    created_at=row.created_at,
                    updated_at=row.updated_at
                )
            return None

    async def update_fact(
        self,
        fact_id: str,
        user_id: str,
        encrypted_blob: bytes,
        blind_indices: list,
        decay_score: float,
        version: int
    ) -> tuple[bool, int]:
        """
        Update a fact with optimistic locking.

        Args:
            fact_id: Fact ID
            user_id: Owner's user ID
            encrypted_blob: New encrypted content
            blind_indices: New blind indices
            decay_score: New decay score
            version: Expected current version

        Returns:
            Tuple of (success, new_version)
            If version conflict, returns (False, current_version)
        """
        async with self.session() as session:
            result = await session.execute(
                text("""
                    UPDATE facts
                    SET encrypted_blob = :blob,
                        blind_indices = :indices,
                        decay_score = :decay,
                        version = version + 1,
                        updated_at = NOW()
                    WHERE id = :id
                      AND user_id = :user_id
                      AND version = :version
                    RETURNING version
                """),
                {
                    "id": fact_id,
                    "user_id": user_id,
                    "blob": encrypted_blob,
                    "indices": blind_indices,
                    "decay": decay_score,
                    "version": version
                }
            )
            row = result.fetchone()
            if row:
                return True, row.version
            # Version conflict - get current version
            current = await session.execute(
                text("SELECT version FROM facts WHERE id = :id"),
                {"id": fact_id}
            )
            current_row = current.fetchone()
            return False, current_row.version if current_row else version

    async def soft_delete_fact(self, fact_id: str, user_id: str) -> bool:
        """
        Soft delete a fact (create tombstone).

        Returns:
            True if deleted, False if not found
        """
        async with self.session() as session:
            # Create tombstone
            await session.execute(
                text("""
                    INSERT INTO tombstones (fact_id, user_id)
                    VALUES (:fact_id, :user_id)
                """),
                {"fact_id": fact_id, "user_id": user_id}
            )
            # Mark fact as inactive
            result = await session.execute(
                text("""
                    UPDATE facts
                    SET is_active = false, updated_at = NOW()
                    WHERE id = :id AND user_id = :user_id
                """),
                {"id": fact_id, "user_id": user_id}
            )
            return result.rowcount > 0

    async def get_all_facts(self, user_id: str) -> list:
        """Get all active facts for a user (export)."""
        async with self.session() as session:
            result = await session.execute(
                text("""
                    SELECT * FROM facts
                    WHERE user_id = :user_id AND is_active = true
                    ORDER BY created_at DESC
                """),
                {"user_id": user_id}
            )
            rows = result.fetchall()
            facts = []
            for row in rows:
                fact = Fact(
                    id=row.id,
                    user_id=row.user_id,
                    encrypted_blob=row.encrypted_blob,
                    blind_indices=row.blind_indices,
                    decay_score=row.decay_score,
                    is_active=row.is_active,
                    version=row.version,
                    source=row.source,
                    created_at=row.created_at,
                    updated_at=row.updated_at
                )
                fact.encrypted_embedding = getattr(row, 'encrypted_embedding', None)
                facts.append(fact)
            return facts

    async def get_facts_paginated(
        self,
        user_id: str,
        limit: int = 1000,
        cursor: Optional[str] = None
    ) -> tuple:
        """
        Get active facts for a user with cursor-based pagination (export).

        Uses fact ID as cursor for stable pagination. Facts are ordered
        by created_at DESC, id DESC for deterministic ordering.

        Args:
            user_id: User's UUID
            limit: Maximum number of facts to return
            cursor: Last fact_id from previous page (or None for first page)

        Returns:
            Tuple of (facts_list, next_cursor_or_none, has_more, total_count)
        """
        async with self.session() as session:
            # Get total count of active facts
            count_result = await session.execute(
                text("""
                    SELECT COUNT(*) as cnt FROM facts
                    WHERE user_id = :user_id AND is_active = true
                """),
                {"user_id": user_id}
            )
            total_count = count_result.fetchone().cnt

            # Build query with optional cursor
            if cursor:
                # Fetch the cursor fact's created_at for stable pagination
                cursor_result = await session.execute(
                    text("""
                        SELECT created_at FROM facts
                        WHERE id = :cursor_id AND user_id = :user_id AND is_active = true
                    """),
                    {"cursor_id": cursor, "user_id": user_id}
                )
                cursor_row = cursor_result.fetchone()
                if cursor_row is None:
                    # Invalid cursor — return empty page
                    return [], None, False, total_count

                result = await session.execute(
                    text("""
                        SELECT * FROM facts
                        WHERE user_id = :user_id
                          AND is_active = true
                          AND (created_at, id) < (:cursor_ts, :cursor_id)
                        ORDER BY created_at DESC, id DESC
                        LIMIT :fetch_limit
                    """),
                    {
                        "user_id": user_id,
                        "cursor_ts": cursor_row.created_at,
                        "cursor_id": cursor,
                        "fetch_limit": limit + 1  # Fetch one extra to check has_more
                    }
                )
            else:
                result = await session.execute(
                    text("""
                        SELECT * FROM facts
                        WHERE user_id = :user_id AND is_active = true
                        ORDER BY created_at DESC, id DESC
                        LIMIT :fetch_limit
                    """),
                    {
                        "user_id": user_id,
                        "fetch_limit": limit + 1
                    }
                )

            rows = result.fetchall()

            has_more = len(rows) > limit
            if has_more:
                rows = rows[:limit]

            facts = []
            for row in rows:
                fact = Fact(
                    id=row.id,
                    user_id=row.user_id,
                    encrypted_blob=row.encrypted_blob,
                    blind_indices=row.blind_indices,
                    decay_score=row.decay_score,
                    is_active=row.is_active,
                    version=row.version,
                    source=row.source,
                    created_at=row.created_at,
                    updated_at=row.updated_at
                )
                fact.encrypted_embedding = getattr(row, 'encrypted_embedding', None)
                facts.append(fact)

            next_cursor = facts[-1].id if facts and has_more else None

            return facts, next_cursor, has_more, total_count

    # ============ Raw Event Operations ============

    async def log_raw_event(self, user_id: str, event_bytes: bytes):
        """
        Log a raw event for audit trail.

        Creates an immutable record in raw_events table.
        The session context manager handles commit/rollback.
        """
        async with self.session() as session:
            event = RawEvent(
                user_id=user_id,
                event_bytes=event_bytes
            )
            session.add(event)
            await session.flush()  # Ensure the INSERT is sent before commit

    # ============ Account Operations (GDPR) ============

    async def soft_delete_user(self, user_id: str) -> bool:
        """
        Soft delete a user account (GDPR).

        1. Marks user as deleted
        2. Deactivates all user facts
        3. Logs deletion event

        Data purge happens after 30 days via cleanup job.

        Args:
            user_id: User's UUID

        Returns:
            True if user was deleted, False if not found
        """
        import json
        async with self.session() as session:
            # Mark user as deleted
            result = await session.execute(
                text("""
                    UPDATE users
                    SET is_deleted = true, deleted_at = NOW()
                    WHERE user_id = :user_id AND is_deleted = false
                """),
                {"user_id": user_id}
            )
            if result.rowcount == 0:
                return False

            # Deactivate all user facts
            await session.execute(
                text("""
                    UPDATE facts
                    SET is_active = false, updated_at = NOW()
                    WHERE user_id = :user_id AND is_active = true
                """),
                {"user_id": user_id}
            )

            # Log deletion event
            audit_record = json.dumps({
                "action": "account_deletion",
                "user_id": user_id,
            }).encode("utf-8")
            event = RawEvent(
                user_id=user_id,
                event_bytes=audit_record
            )
            session.add(event)
            await session.flush()

            return True

    # ============ Health Check ============

    async def health_check(self) -> dict:
        """Check database connectivity."""
        try:
            async with self.session() as session:
                result = await session.execute(text("SELECT 1"))
                result.fetchone()
                return {"status": "connected"}
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Database health check failed: {e}")
            return {"status": "error", "message": "Database connection failed"}


# Global database instance
_db: Optional[Database] = None


async def init_db(database_url: Optional[str] = None, max_retries: int = 5, base_delay: float = 1.0) -> Database:
    """Initialize the global database instance with retry logic.

    Retries the initial connection with exponential backoff to handle
    transient failures during deployment (e.g., database not yet ready).

    Args:
        database_url: Database connection URL. If None, uses settings.
        max_retries: Maximum number of connection attempts.
        base_delay: Base delay in seconds (doubles each retry).

    Returns:
        Initialized Database instance.
    """
    global _db
    logger = logging.getLogger(__name__)
    _db = Database(database_url)

    for attempt in range(max_retries):
        try:
            await _db.init()
            return _db
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            delay = base_delay * (2 ** attempt)
            logger.warning(
                f"DB connection attempt {attempt + 1}/{max_retries} failed: {e}. "
                f"Retrying in {delay:.1f}s..."
            )
            await asyncio.sleep(delay)

    # Should not reach here, but satisfy type checker
    return _db


async def close_db():
    """Close the global database instance."""
    global _db
    if _db:
        await _db.close()
        _db = None


def get_db() -> Database:
    """Get the global database instance."""
    global _db
    if _db is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _db
