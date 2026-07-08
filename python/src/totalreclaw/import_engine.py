"""Back-compat shim: the import engine now lives in ``totalreclaw.imports.engine``.

Importing ``totalreclaw.import_engine`` returns the exact same module object as
``totalreclaw.imports.engine`` (via ``sys.modules`` aliasing), so existing
``patch("totalreclaw.import_engine.<name>")`` targets and
``import totalreclaw.import_engine as ie`` monkeypatches keep working unchanged.
"""
import sys

from totalreclaw.imports import engine as _engine

sys.modules[__name__] = _engine
