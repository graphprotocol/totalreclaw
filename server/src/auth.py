"""
Authentication module for TotalReclaw Server.

Implements HKDF-SHA256 based authentication:
- Registration: auth_key = HKDF(master_password, salt, "totalreclaw-auth-key-v1")
- Server stores SHA256(auth_key) and salt
- Every request: Client sends auth_key in Authorization header
- Server computes SHA256(auth_key) and looks up user

The server NEVER sees the recovery phrase or encryption key.
"""
import hashlib
import hmac
import secrets
from typing import Optional, Tuple
from dataclasses import dataclass

from hkdf import Hkdf as HKDF


# Constants from config
AUTH_KEY_INFO = b"totalreclaw-auth-key-v1"
HKDF_LENGTH = 32  # 256 bits
SALT_LENGTH = 32  # 256 bits


@dataclass
class AuthCredentials:
    """Authentication credentials for a user."""
    user_id: str
    auth_key_hash: bytes
    salt: bytes


def generate_salt() -> bytes:
    """Generate a cryptographically secure random salt."""
    return secrets.token_bytes(SALT_LENGTH)


def derive_auth_key(master_password: str, salt: bytes) -> bytes:
    """
    Derive the auth key from recovery phrase using HKDF-SHA256.

    This is the CLIENT-SIDE function. The server should never need to call this.
    It's included here for reference and testing.

    Args:
        master_password: User's recovery phrase
        salt: 32-byte salt (stored on server)

    Returns:
        32-byte auth key to be sent in Authorization header
    """
    # Convert password to bytes
    password_bytes = master_password.encode('utf-8')

    # Derive key using HKDF-SHA256
    # hkdf library API: Hkdf(salt, input_key_material, hash) for extract,
    # then .expand(info, length) for expand step.
    hkdf = HKDF(salt, password_bytes, hashlib.sha256)
    return hkdf.expand(AUTH_KEY_INFO, HKDF_LENGTH)


def hash_auth_key(auth_key: bytes) -> bytes:
    """
    Hash the auth key for storage.

    The server stores SHA256(auth_key), not the auth_key itself.
    This prevents replay attacks if database is compromised.

    Args:
        auth_key: The derived auth key (32 bytes)

    Returns:
        SHA256 hash of auth key (32 bytes)
    """
    return hashlib.sha256(auth_key).digest()


def verify_auth_key(auth_key: bytes, stored_hash: bytes) -> bool:
    """
    Verify an auth key against stored hash.

    Args:
        auth_key: The auth key provided by client
        stored_hash: The stored SHA256 hash

    Returns:
        True if auth_key hashes to stored_hash
    """
    return hmac.compare_digest(hashlib.sha256(auth_key).digest(), stored_hash)


def generate_user_id() -> str:
    """
    Generate a UUIDv7-like user ID.

    For simplicity in PoC, we use UUID4. In production, use UUIDv7
    for time-sortable IDs.

    Returns:
        UUID string
    """
    import uuid
    return str(uuid.uuid4())


class AuthError(Exception):
    """Authentication error."""
    def __init__(self, message: str, code: str = "AUTH_FAILED"):
        self.message = message
        self.code = code
        super().__init__(message)


class AuthMiddleware:
    """
    Authentication middleware for FastAPI.

    Validates Authorization header and extracts user_id for request handlers.
    """

    def __init__(self, db_session):
        """
        Initialize auth middleware.

        Args:
            db_session: Database session for user lookup
        """
        self.db = db_session

    async def authenticate(self, authorization: Optional[str]) -> AuthCredentials:
        """
        Authenticate a request using Authorization header.

        Args:
            authorization: The Authorization header value (e.g., "Bearer <auth_key>")

        Returns:
            AuthCredentials if authentication succeeds

        Raises:
            AuthError: If authentication fails
        """
        if not authorization:
            raise AuthError("Missing Authorization header", "UNAUTHORIZED")

        # Parse Bearer token
        parts = authorization.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise AuthError("Invalid Authorization header format", "UNAUTHORIZED")

        try:
            # Decode hex-encoded auth key
            auth_key = bytes.fromhex(parts[1])
        except ValueError:
            raise AuthError("Invalid auth key format", "UNAUTHORIZED")

        # Hash the auth key and look up user
        auth_key_hash = hash_auth_key(auth_key)

        # Look up user by auth_key_hash
        user = await self.db.get_user_by_auth_hash(auth_key_hash)
        if not user:
            raise AuthError("Invalid credentials", "UNAUTHORIZED")

        return AuthCredentials(
            user_id=user.user_id,
            auth_key_hash=user.auth_key_hash,
            salt=user.salt
        )
