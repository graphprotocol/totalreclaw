"""Start a real relay checkout (node/tsx) against a throwaway Postgres.

Environment:
  TR_RELAY_DIR      relay checkout (dependencies installed)
  TR_PG_CONTAINER   docker container name of a throwaway postgres:16
  TR_PG_PORT        host port of that container (default 55433)
"""
from __future__ import annotations

import os
import socket
import subprocess
import time
import urllib.request

PG_CONTAINER = os.environ.get("TR_PG_CONTAINER", "d23-relay-pg")
PG_PORT = int(os.environ.get("TR_PG_PORT", "55433"))
DATA_EDGE = "0xC445af1D4EB9fce4e1E61fE96ea7B8feBF03c5ca"


def psql(sql: str) -> str:
    out = subprocess.run(
        ["docker", "exec", PG_CONTAINER, "psql", "-U", "postgres", "-d", "relay", "-tAc", sql],
        capture_output=True, text=True, timeout=30,
    )
    if out.returncode != 0:
        raise RuntimeError(f"psql failed: {out.stderr[:200]}")
    return out.stdout.strip()


def reset_db() -> None:
    psql("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Relay:
    def __init__(self, relay_dir: str, stub_bundler: str, stub_rpc: str, *, extra_env: dict | None = None) -> None:
        self.port = free_port()
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG")}
        env.update({
            "PORT": str(self.port),
            "DATABASE_URL": f"postgres://postgres:pw@127.0.0.1:{PG_PORT}/relay",
            "PIMLICO_API_KEY": "dummy",
            "PIMLICO_BUNDLER_URL": stub_bundler,
            "PRO_PIMLICO_BUNDLER_URL": stub_bundler,
            "PIMLICO_CHAIN_ID": "100",
            "PRO_PIMLICO_CHAIN_ID": "100",
            "GNOSIS_RPC_URL": stub_rpc,
            "DATA_EDGE_ADDRESS": DATA_EDGE,
            "PRO_DATA_EDGE_ADDRESS": DATA_EDGE,
            "QA_REGISTER_BYPASS": "true",
            "NODE_ENV": "development",
            "LOG_LEVEL": "warn",
        })
        env.update(extra_env or {})
        self.log = open(os.path.join(relay_dir, ".d23-relay.log"), "w")
        self.proc = subprocess.Popen(
            ["npx", "tsx", "src/index.ts"], cwd=relay_dir, env=env, stdout=self.log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.url = f"http://127.0.0.1:{self.port}"
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{self.url}/health", timeout=2) as r:
                    if r.status == 200:
                        return
            except Exception:
                time.sleep(1)
        self.close()
        raise RuntimeError("relay did not start")

    def close(self) -> None:
        # npx -> tsx -> node: stop the whole process group, not just npx.
        import signal

        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.log.close()
        try:
            os.unlink(self.log.name)
        except OSError:
            pass
