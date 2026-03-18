"""
Configuration management for TotalReclaw Server.
"""
import os
from typing import Optional
from pydantic import ConfigDict
from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = ConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # Ignore unknown env vars (e.g. ZAI_API_KEY, MEM0_API_KEY)
    )

    # Server configuration
    host: str = "127.0.0.1"
    port: int = 8080
    debug: bool = False
    environment: str = "development"  # development | staging | production

    # Database configuration
    database_url: str = "postgresql+asyncpg://totalreclaw:dev@localhost:5432/totalreclaw"
    database_pool_size: int = 20
    database_max_overflow: int = 30
    database_pool_recycle: int = 3600     # Recycle connections after 1 hour
    database_pool_pre_ping: bool = True   # Detect stale connections before use
    database_pool_timeout: int = 30       # Fail fast if pool exhausted (seconds)

    # Security
    # Note: No JWT_SECRET needed - we use HKDF-derived auth keys

    # Trusted proxy IPs (only trust X-Forwarded-For from these)
    trusted_proxies: str = ""  # Comma-separated IPs, e.g. "172.18.0.1,10.0.0.1"

    # CORS
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    # Rate limiting (per hour)
    rate_limit_register_per_hour: int = 5
    rate_limit_store_per_hour: int = 1000
    rate_limit_search_per_hour: int = 1000
    rate_limit_sync_per_hour: int = 1000
    rate_limit_account_per_hour: int = 10

    # API version
    api_version: str = "0.3.1"

    # NOTE: Pimlico, Stripe, Coinbase, subgraph, and proxy/billing config
    # have been moved to the private relay repo (totalreclaw-relay).
    # This self-hosted server only needs core storage, search, and auth config.

    @property
    def is_production(self) -> bool:
        """Check if running in production."""
        return self.environment == "production"

    @property
    def is_development(self) -> bool:
        """Check if running in development."""
        return self.environment == "development"

    @property
    def trusted_proxy_list(self) -> list:
        """Parse trusted proxy IPs from comma-separated string."""
        if not self.trusted_proxies:
            return []
        return [ip.strip() for ip in self.trusted_proxies.split(",") if ip.strip()]

    @property
    def cors_origin_list(self) -> list:
        """Parse CORS origins from comma-separated string."""
        if not self.cors_origins:
            return []
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


# Auth configuration constants
AUTH_KEY_INFO = b"totalreclaw-auth-key-v1"
ENCRYPTION_KEY_INFO = b"totalreclaw-encryption-key-v1"
HKDF_LENGTH = 32  # 256 bits
SALT_LENGTH = 32  # 256 bits


