"""``/healthz`` and ``/.well-known/totalreclaw-enclave.json`` (spec §4.1)."""

from __future__ import annotations

from collections.abc import Sequence

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from totalreclaw_enclave import __version__
from totalreclaw_enclave.deps import Deps
from totalreclaw_enclave.settings import Settings
from totalreclaw_enclave.subsystems import Subsystem

HEALTHZ_PATH = "/healthz"
ENCLAVE_DOC_PATH = "/.well-known/totalreclaw-enclave.json"


def enclave_document(settings: Settings) -> dict[str, str | None]:
    """The public transparency document. Keys exactly as spec §4.1."""
    return {
        "version": __version__,
        "compose_hash": settings.compose_hash,
        "os_image_hash": settings.os_image_hash,
        "near_contract": settings.near_contract,
        "measurement_id": settings.measurement_id,
        "inference_policy_version": settings.inference_policy_version,
        "transparency_url": settings.transparency_url,
    }


def routes(settings: Settings, deps: Deps) -> Sequence[BaseRoute]:
    document = enclave_document(settings)

    async def healthz(_request: Request) -> JSONResponse:
        # Liveness only: no DB, no network, no state (spec §4.1).
        return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})

    async def enclave_json(_request: Request) -> JSONResponse:
        return JSONResponse(document, headers={"Cache-Control": "no-cache"})

    return [
        Route(HEALTHZ_PATH, healthz, methods=["GET"]),
        Route(ENCLAVE_DOC_PATH, enclave_json, methods=["GET"]),
    ]


SUBSYSTEM = Subsystem(name="wellknown", routes=routes)
