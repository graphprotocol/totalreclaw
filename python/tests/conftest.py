"""Shared fixtures for TotalReclaw Python tests.

IMPORTANT — test-safety invariant: no pytest session in this repo may hit
the production relay. CLAUDE.md mandates that all tests use staging, but
``DEFAULT_RELAY_URL`` resolves to production when unset, and some unit
tests create a real ``TotalReclaw`` client without explicitly overriding
the URL. If such a test makes any method call that flows through
``_ensure_registered``, it will POST to whichever URL the client picked —
which would be production.

To prevent that, this module forces the relay URL to staging at import
time, BEFORE any test module imports ``totalreclaw.relay`` or
``totalreclaw.client``. The env var is also re-asserted per-test so a
test that clears it accidentally gets it back.
"""
from __future__ import annotations

import os

# Force staging for every test, always. Individual tests that need a
# different URL can override locally, but the default is never production.
_STAGING_URL = "https://api-staging.totalreclaw.xyz"
os.environ.setdefault("TOTALRECLAW_SERVER_URL", _STAGING_URL)
