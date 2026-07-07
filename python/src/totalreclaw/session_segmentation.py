"""Back-compat shim: session segmentation now lives in ``totalreclaw.crystals.session_segmentation``.

Aliased through ``sys.modules`` so ``totalreclaw.session_segmentation`` and
``totalreclaw.crystals.session_segmentation`` are the same module object —
existing ``import totalreclaw.session_segmentation`` targets and module-level
monkeypatches keep working unchanged.
"""
import sys

from totalreclaw.crystals import session_segmentation as _session_segmentation

sys.modules[__name__] = _session_segmentation
