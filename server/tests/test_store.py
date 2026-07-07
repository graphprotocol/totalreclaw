"""
Tests for store endpoint.
"""
import pytest
import os
import sys
from datetime import datetime
import uuid

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestStoreEndpoint:
    """Tests for the /store endpoint."""

    @pytest.fixture
    def sample_fact(self):
        """Create a sample fact for testing."""
        return {
            "id": str(uuid.uuid4()),
            "timestamp": datetime.utcnow().isoformat(),
            "encrypted_blob": "a1b2c3d4e5f6" * 100,  # Simulated encrypted data
            "blind_indices": [
                "abc123def456",  # SHA-256 hash of keyword
                "789ghi012jkl",  # SHA-256 hash of LSH bucket
            ],
            "decay_score": 1.0,
            "is_active": True,
            "version": 1,
            "source": "conversation"
        }

    @pytest.fixture
    def auth_headers(self, test_user):
        """Create auth headers for requests."""
        return {"Authorization": f"Bearer {test_user['auth_key'].hex()}"}

    def test_store_single_fact(self, client, auth_headers, sample_fact):
        """Test storing a single fact."""
        response = client.post(
            "/v1/store",
            json={
                "user_id": "test_user_123",
                "facts": [sample_fact]
            },
            headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert len(data["ids"]) == 1
        assert data["ids"][0] == sample_fact["id"]

    def test_store_multiple_facts(self, client, auth_headers):
        """Test storing multiple facts."""
        facts = []
        for i in range(5):
            facts.append({
                "id": str(uuid.uuid4()),
                "timestamp": datetime.utcnow().isoformat(),
                "encrypted_blob": f"blob_{i}" + "00" * 50,
                "blind_indices": [f"index_{i}"],
                "decay_score": 1.0 - (i * 0.1),
                "is_active": True,
                "version": 1,
                "source": "test"
            })

        response = client.post(
            "/v1/store",
            json={"user_id": "test_user_123", "facts": facts},
            headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert len(data["ids"]) == 5

    def test_store_empty_facts_list(self, client, auth_headers):
        """Test storing empty facts list."""
        response = client.post(
            "/v1/store",
            json={"user_id": "test_user_123", "facts": []},
            headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert data["error_code"] == "INVALID_REQUEST"

    def test_store_without_auth(self, client, sample_fact):
        """Test storing without authentication."""
        response = client.post(
            "/v1/store",
            json={"user_id": "test_user_123", "facts": [sample_fact]}
        )
        assert response.status_code == 401

    def test_store_invalid_auth(self, client, sample_fact):
        """Test storing with invalid authentication."""
        response = client.post(
            "/v1/store",
            json={"user_id": "test_user_123", "facts": [sample_fact]},
            headers={"Authorization": "Bearer invalid_key"}
        )
        assert response.status_code == 401

    def test_store_user_id_mismatch(self, client, auth_headers, sample_fact):
        """Test storing with mismatched user ID."""
        response = client.post(
            "/v1/store",
            json={"user_id": "different_user", "facts": [sample_fact]},
            headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert data["error_code"] == "AUTH_FAILED"

    def test_store_without_user_id(self, client, auth_headers, sample_fact):
        """Omitting the legacy body user_id succeeds — it is now optional,
        with ownership scoped to the authenticated identity."""
        response = client.post(
            "/v1/store",
            json={"facts": [sample_fact]},
            headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["ids"][0] == sample_fact["id"]

    def test_store_invalid_hex_blob(self, client, auth_headers, sample_fact):
        """Test storing with invalid hex blob."""
        sample_fact["encrypted_blob"] = "not_valid_hex!"
        response = client.post(
            "/v1/store",
            json={"user_id": "test_user_123", "facts": [sample_fact]},
            headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert data["error_code"] == "INVALID_REQUEST"


class TestDeleteEndpoint:
    """Tests for the DELETE /v1/facts/{fact_id} endpoint."""

    @pytest.fixture
    def auth_headers(self, test_user):
        """Create auth headers for requests."""
        return {"Authorization": f"Bearer {test_user['auth_key'].hex()}"}

    def test_delete_fact(self, client, auth_headers, stored_fact):
        """Test deleting a fact."""
        response = client.delete(
            f"/v1/facts/{stored_fact['id']}",
            headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    def test_delete_nonexistent_fact(self, client, auth_headers):
        """Test deleting a fact that doesn't exist."""
        response = client.delete(
            f"/v1/facts/{str(uuid.uuid4())}",
            headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert data["error_code"] == "NOT_FOUND"

    def test_delete_without_auth(self, client, stored_fact):
        """Test deleting without authentication."""
        response = client.delete(f"/v1/facts/{stored_fact['id']}")
        assert response.status_code == 401


class TestExportEndpoint:
    """Tests for the GET /export endpoint."""

    @pytest.fixture
    def auth_headers(self, test_user):
        """Create auth headers for requests."""
        return {"Authorization": f"Bearer {test_user['auth_key'].hex()}"}

    def test_export_facts(self, client, auth_headers, multiple_stored_facts):
        """Test exporting all facts."""
        response = client.get("/v1/export", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert len(data["facts"]) == len(multiple_stored_facts)

    def test_export_empty(self, client, auth_headers):
        """Test exporting when no facts exist."""
        response = client.get("/v1/export", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert len(data["facts"]) == 0

    def test_export_without_auth(self, client):
        """Test exporting without authentication."""
        response = client.get("/v1/export")
        assert response.status_code == 401


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
