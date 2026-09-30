"""ASGI application factory (Starlette).

``create_app(settings, deps=None, subsystems=None)`` builds the app from an
ordered tuple of ``Subsystem`` values (``subsystems.default_subsystems()``
when omitted). The lifespan opens the database (running migrations), writes
the ``enclave.boot`` audit row, then enters each subsystem lifespan in order;
shutdown unwinds in reverse and closes the database.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import BaseRoute, Mount, Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from totalreclaw_enclave import __version__
from totalreclaw_enclave.deps import Deps, build_deps
from totalreclaw_enclave.settings import Settings
from totalreclaw_enclave.subsystems import Subsystem, default_subsystems

# Named ``logger`` (not ``_log``): ruff's G/LOG rules only check logging calls
# on loggers they recognise by name, so any other name silently escapes lint.
logger = logging.getLogger("totalreclaw_enclave.web")

UNMATCHED_ROUTE = "unmatched"

# Starlette path parameter, with optional convertor: {name} or {name:int}.
_PATH_PARAM = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)(?::[a-zA-Z_][a-zA-Z0-9_]*)?\}")


def _route_chain(routes: Sequence[BaseRoute], target: BaseRoute) -> list[BaseRoute] | None:
    """The routes from the app root down to ``target``: enclosing Mounts, then ``target``."""
    for route in routes:
        if route is target:
            return [route]
        if isinstance(route, Mount):
            below = _route_chain(route.routes, target)
            if below is not None:
                return [route, *below]
    return None


def route_template(scope: Scope) -> str:
    """The matched route's path template with parameters shown as ``:name``.

    Built from the route definitions Starlette matched (``scope["route"]``,
    found under the top-level ``scope["router"]`` with its enclosing Mounts),
    never from the request path: ``/connect/{id}`` ids are capabilities and
    must not reach a log line.
    """
    router, matched = scope.get("router"), scope.get("route")
    if router is None or matched is None:
        return UNMATCHED_ROUTE
    chain = _route_chain(router.routes, matched)
    if chain is None:
        return UNMATCHED_ROUTE
    prefix = "".join(getattr(mount, "path", "") for mount in chain[:-1])
    return _PATH_PARAM.sub(r":\1", prefix + getattr(matched, "path_format", ""))


class RequestLogMiddleware:
    """Logs ``http.request`` (method, route template, status, duration)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = {"code": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            logger.exception(
                "http.request.error",
                extra={"method": scope["method"], "route": route_template(scope)},
            )
            raise
        finally:
            logger.info(
                "http.request",
                extra={
                    "method": scope["method"],
                    "route": route_template(scope),
                    "status": status["code"],
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )


def _route_key(route: BaseRoute) -> tuple[str, frozenset[str] | None]:
    """``(path, methods)``; ``None`` methods = every method (a Mount, or a Route without ``methods``)."""
    if isinstance(route, Route):
        return route.path, frozenset(route.methods) if route.methods is not None else None
    if isinstance(route, Mount):
        return route.path, None
    raise TypeError("subsystem routes must be starlette Route or Mount instances")


def _methods_overlap(a: frozenset[str] | None, b: frozenset[str] | None) -> bool:
    return a is None or b is None or not a.isdisjoint(b)


def create_app(
    settings: Settings,
    deps: Deps | None = None,
    subsystems: Sequence[Subsystem] | None = None,
) -> Starlette:
    deps = deps if deps is not None else build_deps(settings)
    chosen = tuple(subsystems) if subsystems is not None else default_subsystems()

    names = [s.name for s in chosen]
    if len(set(names)) != len(names):
        raise ValueError("duplicate subsystem name")

    # A path may be registered more than once only with disjoint methods
    # (e.g. GET and POST routes on one path); anything else would shadow.
    routes: list[BaseRoute] = []
    seen: dict[str, list[tuple[frozenset[str] | None, str]]] = {}
    for subsystem in chosen:
        for route in subsystem.routes(settings, deps):
            path, methods = _route_key(route)
            for other_methods, owner in seen.get(path, []):
                if _methods_overlap(methods, other_methods):
                    raise ValueError(f"route {path} registered by both {owner} and {subsystem.name}")
            seen.setdefault(path, []).append((methods, subsystem.name))
            routes.append(route)

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[dict[str, Any]]:
        applied = await deps.db.open(now=deps.clock.now())
        try:
            await deps.audit.record(
                "enclave.boot",
                details={"env": settings.env, "version": __version__, "migrations_applied": len(applied)},
            )
            async with AsyncExitStack() as stack:
                for subsystem in chosen:
                    if subsystem.lifespan is not None:
                        await stack.enter_async_context(subsystem.lifespan(settings, deps))
                logger.info("enclave.started", extra={"env": settings.env, "subsystems": len(chosen)})
                yield {}
        finally:
            await deps.db.close()

    app = Starlette(
        debug=False,
        routes=routes,
        middleware=[Middleware(RequestLogMiddleware)],
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.deps = deps
    return app
