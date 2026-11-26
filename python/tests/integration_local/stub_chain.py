"""HTTP front for the fake ERC-4337 world, with a REAL clock.

Stands in for Pimlico's bundler/paymaster (``/bundler``) and a Gnosis JSON-RPC
node (``/rpc``) for a real relay process and a real Python client. The
in-process ``world`` is the control surface: tests set faults on it directly.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.fakes.fake_erc4337 import HttpFault, TransportFault, World


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class StubChain:
    def __init__(self, **world_kw) -> None:
        world_kw.setdefault("mine_delay", 3.0)
        world_kw.setdefault("bundle_delay", 0.5)
        world_kw.setdefault("block_time", 2.0)
        world_kw.setdefault("strict", False)
        self.world = World(clock=time.monotonic, **world_kw)
        self.lock = threading.Lock()
        self.requests: list[tuple[str, str]] = []
        stub = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("content-length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
                method, params = req.get("method"), req.get("params") or []
                is_bundler = self.path.startswith("/bundler")
                try:
                    with stub.lock:
                        stub.requests.append((method, "bundler" if is_bundler else "rpc"))
                        body = (stub.world.relay_rpc if is_bundler else stub.world.public_rpc)(method, params)
                except HttpFault as f:
                    self.send_response(f.status)
                    self.end_headers()
                    self.wfile.write(f.text.encode())
                    return
                except TransportFault:
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return
                body = {"jsonrpc": "2.0", "id": req.get("id", 1), **body}
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):  # silence
                pass

        self.server = _Server(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        self._stop = threading.Event()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        threading.Thread(target=self._ticker, daemon=True).start()

    def _ticker(self) -> None:
        while not self._stop.wait(0.2):
            with self.lock:
                self.world.tick()

    @property
    def bundler_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/bundler"

    @property
    def rpc_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/rpc"

    def close(self) -> None:
        self._stop.set()
        self.server.shutdown()
