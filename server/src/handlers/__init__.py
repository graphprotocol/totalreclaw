"""
API handlers for TotalReclaw Server.

Note: Relay, billing, and proxy handlers have been moved to the private
totalreclaw-relay repo. This server only handles self-hosted functionality.
"""
from .register import router as register_router
from .store import router as store_router
from .search import router as search_router
from .health import router as health_router
from .account import router as account_router
from .sync import router as sync_router
from .observability import router as observability_router

__all__ = [
    "register_router",
    "store_router",
    "search_router",
    "health_router",
    "account_router",
    "sync_router",
    "observability_router",
]
