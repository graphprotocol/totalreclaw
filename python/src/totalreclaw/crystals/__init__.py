"""Session-Crystal subsystem for TotalReclaw.

Groups the two internal modules that build and re-group session "Crystals"
(session-summary memories):

  - ``totalreclaw.crystals.session_segmentation`` -- ``segment_sessions``: the
    (core-hoisted) heuristic that splits a flat conversation into topic
    sessions. Dependency-free leaf (stdlib only).
  - ``totalreclaw.crystals.recrystallize``        -- the recrystallize planner
    + executor that re-groups already-stored facts into coherent Crystals.

Both were previously flat modules at the package root. The pre-consolidation
paths (``totalreclaw.session_segmentation``, ``totalreclaw.recrystallize``)
remain importable as ``sys.modules`` aliases (see the shim modules of the same
name at the package root), so existing
``import totalreclaw.recrystallize as rec`` / ``rec.<attr>`` monkeypatches and
``from totalreclaw.session_segmentation import segment_sessions`` targets keep
working unchanged. Neither module is part of the public API
(``totalreclaw.__all__`` exports only ``TotalReclaw``).
"""
from __future__ import annotations

from .session_segmentation import segment_sessions

__all__ = ["segment_sessions"]
