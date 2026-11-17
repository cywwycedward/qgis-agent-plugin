"""一次调用被接受时所依据的工程代数，以及它被允许跑在哪个线程。

``QgsProject`` 实例在整个 QGIS 会话里保持同一个身份，被换掉的是它的内容，所以「哪个工程」
没法用对象引用比较来回答。这里改用计数器：当前工程每次重置就把代数加一，每条执行记录下自己被
接受时的代数，在旧代数下接受的工作不允许碰新工程。加代数的那一步由插件生命周期挂在
``QgsProject.aboutToBeCleared`` 上；本模块只管这个数字，所以不依赖 QGIS 也能导入。

引擎也是唯一知道 QGIS 认为哪个线程是主线程的地方，所以这项检查也放在这里，写成普通的
``threading`` 比较：不用 Qt、不碰事件循环，因此在裸解释器里就能测。

The project generation a call is accepted against, and the thread it may run on.

A ``QgsProject`` instance keeps its identity for the whole QGIS session while its
*content* is replaced, so "which project" cannot be answered by comparing object
references.  It is answered by a counter instead: every reset of the current
project bumps the epoch, every execution records the epoch it was accepted under,
and work accepted under an older epoch must not touch the newer project.  Bumping
is bound to ``QgsProject.aboutToBeCleared`` by the plugin lifecycle; this module
only owns the number, which keeps it importable without QGIS.

The engine is also the only thing that knows which thread QGIS considers main, so
that check lives here too, as a plain ``threading`` comparison: no Qt, no event
loop, and therefore testable in a bare interpreter.
"""

from __future__ import annotations

import threading

from .models import WRONG_THREAD, FrameworkError


class ProjectLifecycle:
    """当前工程的代数：从 0 开始，只增不减。

    The current project's generation: starts at 0 and only counts up.
    """

    def __init__(self) -> None:
        self._epoch = 0

    @property
    def current_epoch(self) -> int:
        return self._epoch

    def bump(self) -> int:
        """开启新一代并返回它；``aboutToBeCleared`` 的信号处理函数调用的就是它。

        Start a new generation and return it; called on ``aboutToBeCleared``.
        """

        self._epoch += 1
        return self._epoch


def require_main_thread(owner_thread: int) -> None:
    """拒绝一个不是来自构建调用方那个线程的调用。

    ``owner_thread`` 是构造时捕获的 ``threading.get_ident()`` 值：对引擎来说就是主线程，因为
    引擎是在主线程上创建的。

    Refuse a call that is not on the thread which built the caller.

    ``owner_thread`` is a ``threading.get_ident()`` value captured at construction:
    the main thread for the engine, because that is where it is created.
    """

    if threading.get_ident() != owner_thread:
        raise FrameworkError(
            WRONG_THREAD, "the engine may only be used from the thread that created it"
        )
