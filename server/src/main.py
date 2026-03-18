"""
TotalReclaw Server - Main FastAPI Application.

Zero-knowledge encrypted memory vault server for Phase 4.
"""
import logging
import re
import signal
import uuid
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from .config import get_settings
from .db import init_db, close_db, get_db
from .handlers import (
    register_router,
    store_router,
    search_router,
    health_router,
    account_router,
    sync_router,
    observability_router,
)
from .metrics import record_request, get_metrics_response, update_db_pool_metrics
from .middleware.rate_limit import RateLimitMiddleware


# ============ Structured JSON Logging ============

class SensitiveDataFilter(logging.Filter):
    """Filter that redacts sensitive data from log records."""

    SENSITIVE_PATTERNS = [
        "auth_key", "authorization", "bearer", "password",
        "encrypted_blob", "salt"
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        """Always return True (don't suppress), but redact sensitive fields."""
        msg = str(record.getMessage()).lower()
        for pattern in self.SENSITIVE_PATTERNS:
            if pattern in msg and "=" in msg:
                # Don't suppress, just flag
                record.msg = "[REDACTED - contains sensitive data pattern]"
                break
        return True


def configure_logging():
    """Configure structured JSON logging."""
    settings = get_settings()

    try:
        from pythonjsonlogger.json import JsonFormatter as _JsonFormatter
        # Create a namespace that looks like the old jsonlogger module
        class jsonlogger:
            JsonFormatter = _JsonFormatter

        # JSON formatter
        formatter = jsonlogger.JsonFormatter(
            fmt="%(asctime)s %(name)s %(levelname)s %(message)s",
            rename_fields={
                "asctime": "timestamp",
                "name": "logger",
                "levelname": "level",
                "message": "message"
            }
        )
    except ImportError:
        # Fallback to standard formatting if python-json-logger not installed
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG if settings.debug else logging.INFO)

    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Add handler with JSON formatter
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    handler.addFilter(SensitiveDataFilter())
    root_logger.addHandler(handler)


configure_logging()
logger = logging.getLogger(__name__)

# Suppress SQLAlchemy echo in production
settings = get_settings()
if settings.debug:
    logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO)
else:
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)



@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    """
    Application lifespan manager.

    Handles startup and shutdown events.
    """
    # Startup
    logger.info("Starting TotalReclaw Server...")
    logger.info(f"API Version: {settings.api_version}")
    logger.info(f"Debug Mode: {settings.debug}")
    logger.info(f"Environment: {settings.environment}")

    try:
        # Initialize database (with retry for transient failures)
        logger.info("Initializing database connection...")
        db_url = settings.database_url
        # Redact password but show scheme + host for debugging
        masked_url = re.sub(r'://[^@]+@', '://***:***@', db_url)
        logger.info(f"Database URL: {masked_url}")
        logger.info(f"Database URL scheme: {db_url.split('://')[0]}")
        await init_db(settings.database_url)
        logger.info("Database connected successfully")
    except Exception as e:
        logger.error(f"Failed to connect to database: {e}")
        raise

    # Register signal handlers for graceful shutdown
    import asyncio
    loop = asyncio.get_running_loop()

    def _signal_handler(sig: signal.Signals) -> None:
        logger.info(f"Received {sig.name}, initiating graceful shutdown...")

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler, sig)

    yield

    # Shutdown
    logger.info("Shutting down TotalReclaw Server...")
    await close_db()
    logger.info("Database connections closed")


# Create FastAPI application
app = FastAPI(
    title="TotalReclaw Server",
    description="""
    Zero-knowledge encrypted memory vault API.

    ## Authentication

    All endpoints (except /register and /health) require authentication using
    a Bearer token in the Authorization header:

    ```
    Authorization: Bearer <hex-encoded-auth-key>
    ```

    The auth_key is derived from the user's master password using HKDF-SHA256:
    ```
    auth_key = HKDF-SHA256(master_password, salt, "totalreclaw-auth-key-v1")
    ```

    ## Zero-Knowledge Design

    - Server NEVER sees the master password
    - Server NEVER sees the encryption key
    - Server NEVER sees plaintext memories
    - Server only stores encrypted blobs and blind indices
    """,
    version=settings.api_version,
    lifespan=lifespan,
    docs_url="/docs" if settings.debug else None,  # Disable docs in production
    redoc_url="/redoc" if settings.debug else None,
)

# CORS middleware (environment-specific)
cors_origins = settings.cors_origin_list
if settings.is_development:
    # In development, allow localhost on any port
    cors_origins = ["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:8080"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

# Per-user rate limiting middleware
app.add_middleware(RateLimitMiddleware)


# ============ Exception Handlers ============

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    """
    Generic exception handler.

    Logs the error internally but returns a generic message to the client.
    This prevents leaking internal details.
    """
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error_code": "INTERNAL_ERROR",
            "error_message": "An internal error occurred"
        }
    )


# ============ Request Logging Middleware with Correlation IDs ============

@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log incoming requests with correlation ID (without logging sensitive data)."""
    # Generate correlation ID
    correlation_id = str(uuid.uuid4())
    request.state.correlation_id = correlation_id

    # Log request (path only, NOT headers or body)
    logger.info(
        "Request received",
        extra={
            "correlation_id": correlation_id,
            "method": request.method,
            "path": request.url.path,
        }
    )

    response = await call_next(request)

    # Add correlation ID to response headers
    response.headers["X-Correlation-ID"] = correlation_id

    # Log response
    logger.info(
        "Response sent",
        extra={
            "correlation_id": correlation_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
        }
    )

    return response


# ============ Metrics Collection Middleware ============

@app.middleware("http")
async def collect_metrics(request: Request, call_next):
    """Collect Prometheus metrics for every request."""
    start_time = time.time()
    response = await call_next(request)
    duration = time.time() - start_time

    record_request(
        method=request.method,
        endpoint=request.url.path,
        status_code=response.status_code,
        duration=duration
    )

    return response


# ============ Security Headers Middleware ============

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Add security headers to all responses."""
    response = await call_next(request)

    # Prevent clickjacking
    response.headers["X-Frame-Options"] = "DENY"

    # Prevent MIME type sniffing
    response.headers["X-Content-Type-Options"] = "nosniff"

    # XSS protection
    response.headers["X-XSS-Protection"] = "1; mode=block"

    # Referrer policy
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

    return response


# ============ Request Size Limit Middleware ============

@app.middleware("http")
async def limit_request_size(request: Request, call_next):
    """Reject request bodies larger than 50MB."""
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            length = int(content_length)
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "error_code": "BAD_REQUEST",
                    "error_message": "Malformed Content-Length header"
                }
            )
        if length > 50 * 1024 * 1024:
            return JSONResponse(
                status_code=413,
                content={
                    "success": False,
                    "error_code": "PAYLOAD_TOO_LARGE",
                    "error_message": "Request body exceeds 50MB limit"
                }
            )
    return await call_next(request)


# ============ Include Routers ============

# Infrastructure endpoints stay at root (no /v1/ prefix)
app.include_router(health_router)

# All API routers are mounted under /v1/
v1_router = APIRouter(prefix="/v1")
v1_router.include_router(register_router)
v1_router.include_router(store_router)
v1_router.include_router(search_router)
v1_router.include_router(account_router)
v1_router.include_router(sync_router)
v1_router.include_router(observability_router)
app.include_router(v1_router)


# ============ Metrics Endpoint ============

@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
    # Update DB pool metrics if available
    try:
        db = get_db()
        update_db_pool_metrics(db.engine.sync_engine)
    except Exception:
        pass
    return get_metrics_response()


# ============ Root Endpoint ============

@app.get("/")
async def root():
    """Root endpoint - returns basic server info."""
    return {
        "message": "TotalReclaw API",
        "version": settings.api_version,
        "docs": "/docs" if settings.debug else "disabled"
    }


# ============ Entry Point ============

def main():
    """Entry point for running the server with uvicorn."""
    import uvicorn

    uvicorn.run(
        "totalreclaw.src.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level="debug" if settings.debug else "info"
    )


if __name__ == "__main__":
    main()
