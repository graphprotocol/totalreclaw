"""Back-compat shim: import checkpoint state now lives in ``totalreclaw.imports.state``.

Aliased through ``sys.modules`` so ``totalreclaw.import_state`` and
``totalreclaw.imports.state`` are the same module object.
"""
import sys

from totalreclaw.imports import state as _state

sys.modules[__name__] = _state
