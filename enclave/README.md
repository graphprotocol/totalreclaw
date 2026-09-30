# TotalReclaw Enclave (service)

The enclave is TotalReclaw's attested remote MCP service: a Python 3.12 ASGI app that will run in an Intel TDX confidential VM (dstack), hold paired vaults' keys sealed under a NEAR-derived root, and serve memory tools to hosts that can only be extended through remote MCP. It is a deployed service, not a library: the package carries the `Private :: Do Not Upload` classifier and is never published to PyPI.

Status: in development; not deployed.

> **Threat-model ceiling.** The enclave holds the derived `encryption_key` of every paired vault in RAM and every fact's ciphertext is public on the Gnosis subgraph. A compromise of the enclave discloses the full contents of every paired vault, past and future, and revocation of its session key only stops future writes. This mode is attested custody, not end-to-end encryption; device-side E2EE remains the default tier.

## Dev quickstart

From the repository root, with Python 3.12:

```bash
python3.12 -m venv enclave/.venv
source enclave/.venv/bin/activate
pip install --upgrade pip
pip install -e ./python            # in-repo TotalReclaw client (pulls totalreclaw-core from PyPI)
pip install -e "./enclave[dev]"

cd enclave
ENCLAVE_ENV=dev python -m totalreclaw_enclave
```

If `python3.12 -m venv` fails because `ensurepip` is missing, create the venv with `python3.12 -m venv --without-pip enclave/.venv && curl -sS https://bootstrap.pypa.io/get-pip.py | enclave/.venv/bin/python` and continue from `source enclave/.venv/bin/activate`.

In another shell:

```bash
curl -s http://127.0.0.1:8080/healthz
curl -s http://127.0.0.1:8080/.well-known/totalreclaw-enclave.json
```

`ENCLAVE_ENV=dev` uses a local SQLite file at `.enclave-dev/enclave.sqlite3` (git-ignored), placeholder measurements and no NEAR network access. To test against an unreleased `totalreclaw-core`, build its wheel from `rust/totalreclaw-core` with `maturin build --release --features python-extension --out dist -i python3.12` and `pip install --force-reinstall rust/totalreclaw-core/dist/totalreclaw_core-*.whl` (this is what CI does).

## Tests and lint

```bash
cd enclave
ruff check src tests
ruff format --check src tests
python -m pytest tests/ -v
```

Tests always run as `ENCLAVE_ENV=dev`; `tests/conftest.py` clears any `ENCLAVE_*` variable inherited from your shell and forces the client's relay default to staging.

## Configuration

All configuration comes from environment variables, parsed once at boot into a frozen `Settings` (`src/totalreclaw_enclave/settings.py`). Invalid configuration stops the process with exit code 2 and a message that names the variable, never its value.

| Variable | dev | staging / prod |
|---|---|---|
| `ENCLAVE_ENV` | required: `dev` | required: `staging` or `prod` (no default, ever) |
| `ENCLAVE_PUBLIC_URL` | default `http://127.0.0.1:8080` | required; `https://` origin, no path |
| `ENCLAVE_BIND_HOST` / `ENCLAVE_PORT` | default `127.0.0.1` / `8080` | same defaults |
| `ENCLAVE_DB_PATH` | default `.enclave-dev/enclave.sqlite3` | required |
| `ENCLAVE_LOG_LEVEL` | default `INFO` | `DEBUG` refused in prod |
| `ENCLAVE_ALLOW_OWNER_EOA` | `0` or `1` | `1` allowed in staging only; refused in prod and on the production derivation root |
| `ENCLAVE_DERIVATION_ROOT` | optional; must equal `totalreclaw-enclave-v1/dev` | optional; must equal the environment's root |
| `ENCLAVE_RELAY_URL` | optional override (a local fake relay); the production relay is refused | not overridable |
| `ENCLAVE_NEAR_CONTRACT` | must be unset | required; `.testnet` account in staging, `.near` in prod |
| `ENCLAVE_MEASUREMENT_ID` | default `dev` | required |
| `ENCLAVE_COMPOSE_HASH` / `ENCLAVE_OS_IMAGE_HASH` | default 64 zeros | required; 64 hex chars |
| `ENCLAVE_INFERENCE_POLICY_VERSION` | default `dev` | required |
| `ENCLAVE_TRANSPARENCY_URL` | default `https://totalreclaw.xyz/enclave` | same default |

Relay and DataEdge are fixed per environment: dev and staging use the staging relay (`https://api-staging.totalreclaw.xyz`) and the staging DataEdge (`0xE7a4D2677B686e13775Ba9092631089e35F0BB91`); only prod uses the production ones.

## Invariants every change must keep

- **No plaintext in logs.** Log through `logging.getLogger(__name__)` with constant messages and values as arguments or `extra=` fields. The single root handler (`logs.configure_logging`) redacts any argument or field that is not a short slug, drops exception messages (except `SafeMessageError`), and scrubs phrase- and key-shaped text. Ruff's `G` rules reject f-strings, `%`, `+` and `.format` inside logging calls.
- **No plaintext in the audit log.** `AuditLogger.record(event, vault_id=..., details=...)` refuses plaintext-shaped fields (`redaction.py` defines the rule) and stores only a 16-hex `vault_hash`, never the vault address.
- **Staging only.** Nothing in dev or staging may talk to the production relay or DataEdge; `load_settings` refuses it.
- **Schema changes are new migrations.** Append `Migration(N + 1, ...)` to `db/migrations.py`; never edit a released one.

## Adding a subsystem

A subsystem is a `Subsystem(name, routes, lifespan=None)` value (`src/totalreclaw_enclave/subsystems.py`). `routes(settings, deps)` returns Starlette `Route`/`Mount` objects; `lifespan(settings, deps)`, if present, returns an async context manager entered at startup in registration order. Register it by appending to `default_subsystems()`. A duplicate name, or a path registered twice with overlapping methods, fails at app construction (GET and POST routes on one path are fine). Dependencies (settings, database, clock, audit logger, and the sealing placeholders) arrive through `Deps` (`src/totalreclaw_enclave/deps.py`).

## Related

- Python client: [`../python/README.md`](../python/README.md)
- Security policy: [`../SECURITY.md`](../SECURITY.md)
