"""
Test script to verify TotalReclaw infrastructure setup.

Tests PostgreSQL connection, schema, and vector operations.
"""

import asyncio
import sys
from uuid import uuid4

import numpy as np

# Add src to path for imports
sys.path.insert(0, "/Users/pdiogo/Documents/code/totalreclaw/src")

from totalreclaw_infrastructure import Database, SchemaManager, get_database


async def test_database_connection():
    """Test basic database connection."""
    print("Testing database connection...")
    db = get_database()

    try:
        is_healthy = await db.health_check()
        print(f"  Health check: {'PASS' if is_healthy else 'FAIL'}")
        return is_healthy
    except Exception as e:
        print(f"  Connection failed: {e}")
        return False


async def test_schema():
    """Test schema setup."""
    print("\nTesting schema setup...")
    db = get_database()
    manager = SchemaManager(db)

    try:
        verification = await manager.verify_schema()
        print("  Schema verification:")
        for item, status in verification.items():
            print(f"    {item}: {'PASS' if status else 'FAIL'}")
        return all(verification.values())
    except Exception as e:
        print(f"  Schema test failed: {e}")
        return False


async def test_vector_operations():
    """Test vector insertion and search."""
    print("\nTesting vector operations...")
    db = get_database()

    try:
        # Create a test vault ID
        vault_id = uuid4()

        # Create a test embedding (640 dimensions, all zeros for simplicity)
        test_embedding = np.zeros(640, dtype=np.float32)
        test_embedding[0] = 1.0  # Set first dimension to 1

        # Insert test record
        record_id = await db.insert_encrypted_memory(
            vault_id=vault_id,
            agent_id="test-agent",
            ciphertext=b"encrypted_content_here",
            nonce=b"12_byte_nonce",
            tag=b"16_byte_auth_tag",
            embedding=test_embedding,
            blind_indices=["hashed_term_1", "hashed_term_2"],
            source_file="MEMORY.md",
            source_type="MEMORY.md",
            chunk_index=0,
            category="test",
        )
        print(f"  Inserted record ID: {record_id}")

        # Retrieve the record
        record = await db.get_by_id(record_id)
        if record:
            print(f"  Retrieved vault_id: {record.vault_id}")
            print(f"  Retrieved source_type: {record.source_type}")
        else:
            print("  Failed to retrieve record")
            return False

        # Test vector search
        query_embedding = np.zeros(640, dtype=np.float32)
        query_embedding[0] = 1.0

        results = await db.vector_search(
            query_embedding=query_embedding,
            vault_id=vault_id,
            limit=5,
        )
        print(f"  Vector search found {len(results)} results")
        if results:
            print(f"    Top result similarity: {results[0].similarity:.4f}")
            print(f"    Top result chunk_index: {results[0].chunk_index}")

        # Cleanup test data
        async with db.connection() as conn:
            await conn.execute("DELETE FROM encrypted_vault WHERE vault_id = %s", (vault_id,))
            await conn.commit()
            print("  Cleaned up test data")

        return True

    except Exception as e:
        print(f"  Vector operations test failed: {e}")
        import traceback

        traceback.print_exc()
        return False


async def test_table_info():
    """Display table structure."""
    print("\nTable structure:")
    db = get_database()
    manager = SchemaManager(db)

    try:
        info = await manager.get_table_info("encrypted_vault")
        for col in info["columns"]:
            print(f"  {col['name']}: {col['type']} (nullable: {col['nullable']})")
    except Exception as e:
        print(f"  Failed to get table info: {e}")


async def test_pgvector_extension():
    """Verify pgvector extension is loaded and working."""
    print("\nTesting pgvector extension...")
    db = get_database()

    try:
        async with db.connection() as conn:
            async with conn.cursor() as cur:
                # Check if pgvector is installed
                await cur.execute(
                    "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
                )
                result = await cur.fetchone()
                if result:
                    print(f"  pgvector extension version: {result[0]}")
                else:
                    print("  pgvector extension NOT found")
                    return False

                # Test vector column type (pgvector appears as USER-DEFINED in information_schema)
                # Use pg_attribute to check for actual type
                await cur.execute(
                    """
                    SELECT a.atttypid::regtype as type_name
                    FROM pg_attribute a
                    JOIN pg_class c ON a.attrelid = c.oid
                    JOIN pg_namespace n ON c.relnamespace = n.oid
                    WHERE c.relname = 'encrypted_vault'
                    AND a.attname = 'embedding'
                    AND n.nspname = 'public'
                    """
                )
                result = await cur.fetchone()
                if result and "vector" in result[0]:
                    print(f"  embedding column type: {result[0]} (640 dimensions)")
                else:
                    print(f"  embedding column type: {result[0] if result else 'NOT FOUND'}")
                    return False

                # Test HNSW index exists
                await cur.execute(
                    """
                    SELECT indexname FROM pg_indexes
                    WHERE tablename = 'encrypted_vault' AND indexname = 'idx_embedding_hnsw'
                    """
                )
                result = await cur.fetchone()
                if result:
                    print(f"  HNSW index: {result[0]}")
                else:
                    print("  HNSW index NOT found")
                    return False

                return True
    except Exception as e:
        print(f"  pgvector test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    """Run all infrastructure tests."""
    print("=" * 60)
    print("TotalReclaw Infrastructure Test Suite")
    print("=" * 60)

    # Initialize database
    db = get_database()
    await db.initialize()

    try:
        # Run tests
        results = {
            "connection": await test_database_connection(),
            "pgvector": await test_pgvector_extension(),
            "schema": await test_schema(),
            "vector_ops": await test_vector_operations(),
        }

        # Display table info
        await test_table_info()

        # Summary
        print("\n" + "=" * 60)
        print("Test Summary:")
        for test, passed in results.items():
            status = "PASS" if passed else "FAIL"
            print(f"  {test}: {status}")
        print("=" * 60)

        # Return exit code
        return 0 if all(results.values()) else 1

    finally:
        await db.close()


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
