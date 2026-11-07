"""The one way work reaches the Qt event queue.

The engine may only touch QGIS from the main thread, so every deferred step goes
through here instead of through ad-hoc ``QTimer`` calls.  A callback posted here
runs on a later event-loop turn, and only while its execution is still worth
running: a call cancelled while it waited is skipped, never executed (PRD 8.5,
AT-09).  Qt is imported inside the default poster, so the queueing rule stays
testable in an interpreter that has no QGIS and no Qt.
"""

from __future__ import annotations

from collections.abc import Callable

Callback = Callable[[], None]
Post = Callable[[Callback], None]


class MainThreadRunner:
    """Post one callback per event-loop turn, skipping an execution that ended."""

    def __init__(self, is_live: Callable[[str], bool], post: Post | None = None) -> None:
        self._is_live = is_live
        self._post = post or _post_to_event_queue

    def post(self, execution_id: str, callback: Callback) -> None:
        """Queue ``callback``, to run only if ``execution_id`` is still live."""

        def run() -> None:
            if self._is_live(execution_id):
                callback()

        self._post(run)


def _post_to_event_queue(callback: Callback) -> None:
    """The production poster: the next turn of the QGIS event loop."""

    from qgis.PyQt.QtCore import QTimer

    QTimer.singleShot(0, callback)
