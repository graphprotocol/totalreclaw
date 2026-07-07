"""Back-compat shim: the recrystallize planner/executor now lives in ``totalreclaw.crystals.recrystallize``.

Aliased through ``sys.modules`` so ``totalreclaw.recrystallize`` and
``totalreclaw.crystals.recrystallize`` are the same module object — existing
``import totalreclaw.recrystallize as rec`` targets and ``rec.<attr>``
monkeypatches (e.g. ``RECRYSTALLIZE_STATE_DIR``) keep working unchanged.
"""
import sys

from totalreclaw.crystals import recrystallize as _recrystallize

sys.modules[__name__] = _recrystallize
