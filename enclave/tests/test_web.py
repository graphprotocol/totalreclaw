from __future__ import annotations

import io
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Mount, Route
from starlette.testclient import TestClient

from tests.support import MEMORY_TEXT, SENTINELS, VAULT_A, staging_env
from totalreclaw_enclave import __version__
from totalreclaw_enclave.clock import FixedClock
from totalreclaw_enclave.deps import Deps, NotConfiguredError, RootProvider, Sealer, build_deps
from totalreclaw_enclave.logs import configure_logging
from totalreclaw_enclave.settings import Settings, load_settings
from totalreclaw_enclave.subsystems import Subsystem, default_subsystems
from totalreclaw_enclave.web import create_app
from totalreclaw_enclave.wellknown import SUBSYSTEM as WELLKNOWN

SPEC_4_1_KEYS = {
    "version",
    "compose_hash",
    "os_image_hash",
    "near_contract",
    "measurement_id",
    "inference_policy_version",
    "transparency_url",
}


def test_healthz(dev_settings: Settings, deps: Deps) -> None:
    with TestClient(create_app(dev_settings, deps)) as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert response.headers["cache-control"] == "no-store"
        assert client.post("/healthz").status_code == 405


def test_enclave_document_dev(dev_settings: Settings, deps: Deps) -> None:
    with TestClient(create_app(dev_settings, deps)) as client:
        response = client.get("/.well-known/totalreclaw-enclave.json")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == SPEC_4_1_KEYS
    assert body == {
        "version": __version__,
        "compose_hash": "0" * 64,
        "os_image_hash": "0" * 64,
        "near_contract": None,
        "measurement_id": "dev",
        "inference_policy_version": "dev",
        "transparency_url": "https://totalreclaw.xyz/enclave",
    }


def test_enclave_document_reflects_settings(tmp_path: Path) -> None:
    settings = load_settings(staging_env(tmp_path))
    with TestClient(create_app(settings)) as client:
        body = client.get("/.well-known/totalreclaw-enclave.json").json()
    assert body["near_contract"] == "enclave-staging.totalreclaw.testnet"
    assert body["measurement_id"] == "m-2026-09-27"
    assert body["compose_hash"] == "c" * 64
    assert body["os_image_hash"] == "d" * 64
    assert body["inference_policy_version"] == "p-1"


async def test_lifespan_opens_db_writes_boot_audit_and_closes(dev_settings: Settings, deps: Deps) -> None:
    with TestClient(create_app(dev_settings, deps)):
        pass
    await deps.db.open(now=deps.clock.now())  # reopen to inspect; already migrated
    try:
        rows = await deps.db.fetchall("SELECT event, details, vault_hash FROM audit_log")
        assert [(r["event"], json.loads(r["details"]), r["vault_hash"]) for r in rows] == [
            ("enclave.boot", {"env": "dev", "version": __version__, "migrations_applied": 1}, None)
        ]
    finally:
        await deps.db.close()


def test_subsystem_lifespans_nest_in_order(dev_settings: Settings, deps: Deps) -> None:
    events: list[str] = []

    def make(name: str, path: str) -> Subsystem:
        async def endpoint(_request: Request) -> JSONResponse:
            return JSONResponse({"name": name})

        def routes(_s: Settings, _d: Deps) -> Sequence[BaseRoute]:
            return [Route(path, endpoint, methods=["GET"])]

        @asynccontextmanager
        async def lifespan(_s: Settings, _d: Deps) -> AsyncIterator[None]:
            events.append(f"enter {name}")
            yield
            events.append(f"exit {name}")

        return Subsystem(name=name, routes=routes, lifespan=lifespan)

    app = create_app(dev_settings, deps, subsystems=(*default_subsystems(), make("a", "/a"), make("b", "/b")))
    with TestClient(app) as client:
        assert client.get("/a").json() == {"name": "a"}
        assert client.get("/b").json() == {"name": "b"}
        assert client.get("/healthz").status_code == 200
    assert events == ["enter a", "enter b", "exit b", "exit a"]


def test_duplicate_routes_and_names_are_refused(dev_settings: Settings, deps: Deps) -> None:
    shadow = Subsystem(name="shadow", routes=WELLKNOWN.routes)
    with pytest.raises(ValueError, match="/healthz registered by both wellknown and shadow"):
        create_app(dev_settings, deps, subsystems=(WELLKNOWN, shadow))
    with pytest.raises(ValueError, match="duplicate subsystem name"):
        create_app(dev_settings, deps, subsystems=(WELLKNOWN, WELLKNOWN))


def test_get_and_post_on_one_path_are_allowed(dev_settings: Settings, deps: Deps) -> None:
    async def read(_request: Request) -> JSONResponse:
        return JSONResponse({"op": "read"})

    async def write(_request: Request) -> JSONResponse:
        return JSONResponse({"op": "write"})

    getter = Subsystem(name="getter", routes=lambda _s, _d: [Route("/thing", read, methods=["GET"])])
    poster = Subsystem(name="poster", routes=lambda _s, _d: [Route("/thing", write, methods=["POST"])])
    with TestClient(create_app(dev_settings, deps, subsystems=(getter, poster))) as client:
        assert client.get("/thing").json() == {"op": "read"}
        assert client.post("/thing").json() == {"op": "write"}
        assert client.delete("/thing").status_code == 405
    putter = Subsystem(name="putter", routes=lambda _s, _d: [Route("/thing", write, methods=["PUT", "POST"])])
    with pytest.raises(ValueError, match="/thing registered by both poster and putter"):
        create_app(dev_settings, deps, subsystems=(getter, poster, putter))
    mount = Subsystem(name="mount", routes=lambda _s, _d: [Mount("/thing", routes=[])])
    with pytest.raises(ValueError, match="/thing registered by both getter and mount"):
        create_app(dev_settings, deps, subsystems=(getter, mount))


class _DevRoot:
    kind = "dev"

    async def get_root(self) -> bytes:
        return bytes(32)


class _EchoSealer:
    def seal_for_vault(self, vault_id: str, plaintext: bytes, *, aad: bytes) -> bytes:
        return plaintext

    def open_for_vault(self, vault_id: str, sealed: bytes, *, aad: bytes) -> bytes:
        return sealed

    def seal_for_instance(self, plaintext: bytes, *, aad: bytes) -> bytes:
        return plaintext

    def open_for_instance(self, sealed: bytes, *, aad: bytes) -> bytes:
        return sealed


def test_build_deps_wires_the_sealing_hooks(dev_settings: Settings, clock: FixedClock) -> None:
    root, sealer = _DevRoot(), _EchoSealer()
    assert isinstance(root, RootProvider) and isinstance(sealer, Sealer)
    wired = build_deps(
        dev_settings, clock=clock, root_provider=root, sealer=sealer, vault_hasher=lambda _v: "f" * 16
    )
    assert wired.require_root_provider() is root
    assert wired.require_sealer() is sealer
    assert wired.audit.vault_hash(VAULT_A) == "f" * 16


def test_sealing_placeholders_are_unwired(deps: Deps) -> None:
    assert deps.root_provider is None and deps.sealer is None
    with pytest.raises(NotConfiguredError):
        deps.require_sealer()
    with pytest.raises(NotConfiguredError):
        deps.require_root_provider()


@pytest.mark.usefixtures("restore_logging")
def test_request_log_uses_route_templates_and_drops_plaintext(dev_settings: Settings, deps: Deps) -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)

    async def boom(_request: Request) -> JSONResponse:
        raise ValueError(MEMORY_TEXT)

    probe = Subsystem(
        name="probe", routes=lambda _s, _d: [Route("/connect/{id}/boom", boom, methods=["GET"])]
    )
    app = create_app(dev_settings, deps, subsystems=(*default_subsystems(), probe))
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/connect/CapabilitySecret123/boom").status_code == 500
        assert client.get("/connect/CapabilitySecret456/unknown?code=abc").status_code == 404
    output = stream.getvalue()
    for sentinel in (*SENTINELS, "CapabilitySecret"):
        assert sentinel not in output, sentinel
    requests = [json.loads(line) for line in output.splitlines() if '"msg": "http.request"' in line]
    assert [(r["fields"]["route"], r["fields"]["status"]) for r in requests] == [
        ("/connect/:id/boom", 500),
        ("unmatched", 404),
    ]


@pytest.mark.usefixtures("restore_logging")
def test_route_template_comes_from_route_definitions(dev_settings: Settings, deps: Deps) -> None:
    # One-character ids occur inside the literal segments ("c" in "connect",
    # "t" in "attestation"); the template must still come out intact.
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)

    async def ok(_request: Request) -> JSONResponse:
        return JSONResponse({})

    probe = Subsystem(
        name="probe",
        routes=lambda _s, _d: [
            Route("/connect/{id}/attestation", ok, methods=["GET"]),
            Mount("/oauth/{client}", routes=[Route("/token/{kind:int}", ok, methods=["POST"])]),
        ],
    )
    with TestClient(create_app(dev_settings, deps, subsystems=(probe,))) as client:
        assert client.get("/connect/c/attestation").status_code == 200
        assert client.get("/connect/t/attestation").status_code == 200
        assert client.delete("/connect/c/attestation").status_code == 405
        assert client.post("/oauth/o/token/7").status_code == 200
        assert client.post("/oauth/o/other").status_code == 404
    requests = [
        json.loads(line) for line in stream.getvalue().splitlines() if '"msg": "http.request"' in line
    ]
    assert [(r["fields"]["route"], r["fields"]["status"]) for r in requests] == [
        ("/connect/:id/attestation", 200),
        ("/connect/:id/attestation", 200),
        ("/connect/:id/attestation", 405),
        ("/oauth/:client/token/:kind", 200),
        ("/oauth/:client/:path", 404),
    ]


@pytest.mark.usefixtures("restore_logging")
def test_mcp_sdk_streamable_http_mounts_as_a_subsystem(dev_settings: Settings, deps: Deps) -> None:
    """Proves the ENC-4 extension point against the pinned SDK (mcp 2.2):
    a Route whose endpoint is the SDK's ASGI app, plus a lifespan that runs
    the session manager. ENC-2 ships no /mcp route; this probe is test-only."""
    configure_logging("INFO", stream=io.StringIO())
    from mcp.server import MCPServer
    from mcp.server.streamable_http_manager import StreamableHTTPASGIApp

    server = MCPServer(name="probe")
    server.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")
    probe = Subsystem(
        name="mcp-probe",
        routes=lambda _s, _d: [Route("/mcp", StreamableHTTPASGIApp(server.session_manager))],
        lifespan=lambda _s, _d: server.session_manager.run(),
    )
    app = create_app(dev_settings, deps, subsystems=(*default_subsystems(), probe))
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "enc2-test", "version": "0"},
        },
    }
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            json=initialize,
            headers={"Accept": "application/json, text/event-stream"},
        )
    assert response.status_code == 200
    assert response.json()["result"]["serverInfo"]["name"] == "probe"
    assert "mcp-session-id" not in {k.lower() for k in response.headers}


def test_build_deps_uses_settings_db_path(tmp_path: Path) -> None:
    settings = load_settings({"ENCLAVE_ENV": "dev", "ENCLAVE_DB_PATH": str(tmp_path / "x" / "e.sqlite3")})
    assert build_deps(settings).db.path == tmp_path / "x" / "e.sqlite3"
