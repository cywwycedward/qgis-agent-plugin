"""工作通往 Qt 事件队列的唯一一条路。

引擎只能在主线程碰 QGIS，所以每一个延后执行的步骤都走这里，而不是各自临时调 ``QTimer``。投到
这里的回调在稍后的事件循环轮次里跑，而且只在它那次执行还值得跑的时候跑：等待期间被取消的调用会被
跳过，永不执行。抛异常的回调同样在这里记日志并丢掉，因为 PyQt 槽里的异常会中止进程，而不是传到
调用方。Qt 只在默认投递函数内部导入，所以这套排队规则在既没有 QGIS 也没有 Qt 的解释器里仍然可测。

The one way work reaches the Qt event queue.

The engine may only touch QGIS from the main thread, so every deferred step goes
through here instead of through ad-hoc ``QTimer`` calls.  A callback posted here
runs on a later event-loop turn, and only while its execution is still worth
running: a call cancelled while it waited is skipped, never executed.  A callback
that raises is logged and dropped here as well, because an exception inside a
PyQt slot aborts the process instead of reaching a caller.  Qt is imported inside
the default poster, so the queueing rule stays testable in an interpreter that
has no QGIS and no Qt.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

_LOGGER = logging.getLogger(__name__)

Callback = Callable[[], None]
Post = Callable[[Callback], None]


class MainThreadRunner:
    """每个事件循环轮次投递一个回调，跳过已经结束的执行。

    Post one callback per event-loop turn, skipping an execution that ended.
    """

    def __init__(self, is_live: Callable[[str], bool], post: Post | None = None) -> None:
        self._is_live = is_live
        self._post = post or _post_to_event_queue

    def post(self, execution_id: str, callback: Callback) -> None:
        """把 ``callback`` 排进队列，只有 ``execution_id`` 仍然存活时才会跑。

        Queue ``callback``, to run only if ``execution_id`` is still live.
        """

        def run() -> None:
            # 存活检查也放在守卫里面：它是注入进来的代码，
            # 它抛的异常和回调抛的一样会让 QGIS 中止。
            # The liveness check is inside the guard too: it is injected code, and an exception
            # from it would abort QGIS just like one from the callback.
            try:
                if not self._is_live(execution_id):
                    return
                callback()
            except Exception:  # noqa: BLE001 - nothing may escape into a Qt slot
                _LOGGER.exception("main-thread step for %s failed", execution_id)

        self._post(run)


def _post_to_event_queue(callback: Callback) -> None:
    """生产用的投递函数：QGIS 事件循环的下一轮。

    The production poster: the next turn of the QGIS event loop.
    """

    from qgis.PyQt.QtCore import QTimer

    QTimer.singleShot(0, callback)
