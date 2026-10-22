"""Process entry point: ``python -m totalreclaw_enclave`` / ``totalreclaw-enclave``."""

from __future__ import annotations

import sys

import uvicorn

from totalreclaw_enclave.logs import configure_logging
from totalreclaw_enclave.settings import SettingsError, load_settings
from totalreclaw_enclave.web import create_app


def main() -> int:
    try:
        settings = load_settings()
    except SettingsError as exc:
        # Before logging is configured; the message never contains a value.
        print(f"totalreclaw-enclave: configuration error: {exc}", file=sys.stderr)
        return 2
    configure_logging(settings.log_level)
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.bind_host,
        port=settings.bind_port,
        log_config=None,  # keep configure_logging's single redacting handler
        access_log=False,  # raw paths carry capability ids; RequestLogMiddleware logs templates
        proxy_headers=False,  # no trusted proxy: TLS terminates in the CVM (ENC-13)
        server_header=False,
        lifespan="on",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
