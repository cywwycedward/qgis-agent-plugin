"""The project generation a call is accepted against, and the thread it may run on.

A ``QgsProject`` instance keeps its identity for the whole QGIS session while its
*content* is replaced, so "which project" cannot be answered by comparing object
references.  It is answered by a counter instead: every reset of the current
project bumps the epoch, every execution records the epoch it was accepted under,
and work accepted under an older epoch must not touch the newer project (PRD 9.1,
9.2).  Bumping is bound to ``QgsProject.aboutToBeCleared`` by the plugin
lifecycle; this module only owns the number, which keeps it importable without
QGIS.

The engine is also the only thing that knows which thread QGIS considers main, so
that check lives here too, as a plain ``threading`` comparison: no Qt, no event
loop, and therefore testable in a bare interpreter (PRD 8.3, AT-26).
"""

from __future__ import annotations

import threading

from .models import WRONG_THREAD, FrameworkError


class ProjectLifecycle:
    """The current project's generation: starts at 0 and only counts up."""

    def __init__(self) -> None:
        self._epoch = 0

    @property
    def current_epoch(self) -> int:
        return self._epoch

    def bump(self) -> int:
        """Start a new generation and return it; called on ``aboutToBeCleared``."""

        self._epoch += 1
        return self._epoch


def require_main_thread(owner_thread: int) -> None:
    """Refuse a call that is not on the thread which built the caller.

    ``owner_thread`` is a ``threading.get_ident()`` value captured at construction:
    the main thread for the engine, because that is where it is created.
    """

    if threading.get_ident() != owner_thread:
        raise FrameworkError(
            WRONG_THREAD, "the engine may only be used from the thread that created it"
        )
