"""How subsystems plug into the ASGI app.

A subsystem is a ``Subsystem`` value: a name, a ``routes`` factory and an
optional ``lifespan`` factory. ``create_app`` calls ``routes(settings, deps)``
for each subsystem in order and enters each ``lifespan(settings, deps)`` in
order at startup (exits in reverse at shutdown).

Registration is an explicit, ordered tuple — ``default_subsystems()`` — not
entry points or import side effects, so the route table is reviewable in one
place. Later leaves append theirs:

* ENC-5  ``totalreclaw_enclave.oauth.SUBSYSTEM``   (/.well-known/oauth-*, /oauth/*)
* ENC-4  ``totalreclaw_enclave.mcp.SUBSYSTEM``     (/mcp; its lifespan enters the
  MCP SDK's ``StreamableHTTPSessionManager.run()``)
* ENC-12 ``totalreclaw_enclave.connect.SUBSYSTEM`` (/connect/*, /attestation)
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from starlette.routing import BaseRoute

if TYPE_CHECKING:
    from totalreclaw_enclave.deps import Deps
    from totalreclaw_enclave.settings import Settings

RoutesFactory = Callable[["Settings", "Deps"], Sequence[BaseRoute]]
LifespanFactory = Callable[["Settings", "Deps"], AbstractAsyncContextManager[None]]


@dataclass(frozen=True, slots=True)
class Subsystem:
    name: str
    routes: RoutesFactory
    lifespan: LifespanFactory | None = None


def default_subsystems() -> tuple[Subsystem, ...]:
    from totalreclaw_enclave.wellknown import SUBSYSTEM as WELLKNOWN

    return (WELLKNOWN,)
