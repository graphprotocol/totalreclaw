"""Back-compat shim: smart-import pipeline now lives in ``totalreclaw.imports.smart``.

Aliased through ``sys.modules`` so ``totalreclaw._smart_import`` and
``totalreclaw.imports.smart`` are the same module object.
"""
import sys

from totalreclaw.imports import smart as _smart

sys.modules[__name__] = _smart
