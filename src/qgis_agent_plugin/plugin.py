"""QGIS 加载的插件：一次加载一个会话，以及 ``unload`` 的六步。

``initGui`` 装好一个会话——四项设置、工程代数、这个插件自己的
``QgsTaskManager``、建在它之上的适配器、装满本构建全部操作的注册表，以及引擎——
把操作坞窗挂到主窗口上，并把引擎与注册表发布到 ``iface`` 上，
Python Console 就在那里找它们。

``unload`` 只做卸载该做的事：引擎停止接受工作并取消从未开始的调用，
这个插件启动的每个任务都被请求停下，仍在运行的交给托管在 QGIS 应用上的
supervisor，因为卸载时不能让任务的 Python 回调继续挂在 QGIS 正在丢弃的
``Plugin`` 实例上。supervisor 在最后一个任务真正终止后于主线程上删掉自己；
这里不等任何任务结束。

pydantic 的大版本在这里检查，早于 DTO 层被导入：DTO 是按 pydantic 2 写的，
别的大版本必须报成依赖错误，而不是模型模块内部的导入失败。

The plugin QGIS loads: one session per load, and the six steps of ``unload``.

``initGui`` assembles a session -- the four settings, the project generation,
this plugin's own ``QgsTaskManager``, the adapter over it, the registry filled with
this build's operations, and the engine -- puts the operation dock on the main
window, and publishes the engine and the registry on ``iface``, which is where the
Python Console finds them.

``unload`` is the unload step and nothing else: the engine stops accepting work and
cancels what never started, every task this plugin started is asked to stop, and
whatever is still running is handed to a supervisor the QGIS application hosts,
because keeping the tasks' Python callbacks on the ``Plugin`` instance QGIS is
dropping is forbidden.  The supervisor removes itself on the main thread once the
last task has really terminated; nothing here waits for a task to end.

The pydantic major is checked here, before the DTO layer can be imported: the DTOs
are written against pydantic 2, so any other major has to read as a dependency
error rather than as an import failure inside a model module.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import fields
from typing import TYPE_CHECKING, Any

import pydantic

if TYPE_CHECKING:
    from .core.config import Settings
    from .core.context import ProjectLifecycle
    from .core.engine import OperationEngine
    from .core.registry import OperationRegistry

__all__ = [
    "Plugin",
    "Supervisor",
    "application_supervisor",
    "read_settings",
]

_LOGGER = logging.getLogger(__name__)

# 四项设置共用一个组：这个插件读到的名字不会和别的插件撞上，默认安装什么也读不到。
# The four settings live under one group: nothing this plugin reads can be confused with
# another plugin's setting, and a default install reads nothing at all.
SETTINGS_GROUP = "qgis_agent_plugin"

# 会话发布在 ``iface`` 上的位置；Python Console 就是在这里读它们的。
# Where the session is published on ``iface``; the Python Console reads these.
ENGINE_ATTRIBUTE = "agent_engine"
REGISTRY_ATTRIBUTE = "agent_registry"


def require_supported_pydantic(version: str) -> None:
    """拒绝一个 DTO 层无法从中导入的 pydantic 大版本。

    Refuse a pydantic major the DTO layer cannot import from.
    """

    if int(version.split(".", 1)[0]) != 2:
        raise RuntimeError(f"qgis-agent-plugin requires pydantic 2.x, but {version} is installed")


require_supported_pydantic(pydantic.VERSION)


def read_settings(store: Any = None) -> Settings:
    """从 ``QgsSettings`` 里读出这几项设置，没写过的用默认值。

    没人写过的键返回 ``None``，``Settings.from_mapping`` 把它当「没设过」，
    于是一个没配置过的 profile 就拿全部出厂默认值。
    store 只是参数，为了这段映射能在不写进用户 profile 的前提下被测试。

    The settings as ``QgsSettings`` holds them, defaults for the rest.

    A key nobody wrote comes back as ``None``, which ``Settings.from_mapping`` reads
    as "not set", so an untouched profile keeps every product default.  The store is a
    parameter only so this mapping can be tested without writing into the user's
    profile.
    """

    from .core.config import Settings

    if store is None:
        from qgis.core import QgsSettings

        store = QgsSettings()
    return Settings.from_mapping(
        {field.name: store.value(f"{SETTINGS_GROUP}/{field.name}") for field in fields(Settings)}
    )


def _message_log_handler() -> logging.Handler:
    """把 WARNING 及以上的每条 logging 记录转给 ``QgsMessageLog`` 的 handler。

    坞窗把操作者指向 QGIS 日志面板，所以这些行必须真的进那个面板：挂在包 logger 上，
    模块 logger 的行顺着传播到达。QGIS 在这里面导入，本模块在裸解释器里仍然可加载。

    A handler that hands every record at WARNING or above to ``QgsMessageLog``.

    The dock points the operator at the QGIS log panel, so those lines have to really
    reach it: attached to the package logger, the module loggers' records propagate in.
    QGIS is imported inside, so this module still loads in a bare interpreter.
    """

    from qgis.core import Qgis, QgsMessageLog

    class _MessageLogHandler(logging.Handler):
        """一条 logging 记录变成 QgsMessageLog 的一行。

        One logging record becomes one QgsMessageLog line.
        """

        def emit(self, record: logging.LogRecord) -> None:
            # ERROR 及以上读成 Critical，WARNING 读成 Warning，其余读成 Info；
            # 异常记录自带 traceback（logging 的默认 formatter 把它附在消息后）。
            # ERROR and above read as Critical, WARNING as Warning, the rest as Info; an
            # exception record carries its traceback (the default formatter appends it).
            if record.levelno >= logging.ERROR:
                level = Qgis.MessageLevel.Critical
            elif record.levelno >= logging.WARNING:
                level = Qgis.MessageLevel.Warning
            else:
                level = Qgis.MessageLevel.Info
            QgsMessageLog.logMessage(self.format(record), "QGIS Agent", level)

    return _MessageLogHandler()


class Supervisor:
    """让一次会话里还活着的工作在插件实例消失后仍然可达。

    卸载时不能让任务的 Python 回调继续挂在 QGIS 正在丢弃的 ``Plugin`` 实例上，
    所以引擎与适配器改由这里持有，而它是 :func:`application_supervisor` 挂在
    QGIS 应用上的对象。这套记账是纯 Python 的，这是有意的：``attach`` 与 ``release``
    跟着这个插件自己的管理器增加和删除的任务走，``close`` 是 ``unload`` 的一次调用，
    最后一个 ``release`` 才结束等待。这里不阻塞，而 ``release`` 之所以可信，
    只因为 QGIS 是在任务真正终止之后才删除它。

    Keeps a session's live work reachable after the plugin instance is gone.

    Keeping the tasks' Python callbacks on the ``Plugin`` instance QGIS is unloading
    is forbidden, so the engine and the adapter are held here instead, in an object
    :func:`application_supervisor` parents on the QGIS application.  The accounting
    is plain Python and that is deliberate: ``attach`` and ``release`` follow the
    tasks this plugin's own manager adds and deletes, ``close`` is one call from
    ``unload``, and the last ``release`` is what ends the wait.  Nothing blocks, and
    ``release`` may only be trusted because QGIS deletes a task after it has really
    terminated.
    """

    def __init__(self, engine: Any, runner: Any, *, dispose: Callable[[], None]) -> None:
        self._engine = engine
        self._runner = runner
        self._dispose = dispose
        self._live: set[int] = set()
        self._closed = False
        self._disposed = False

    def attach(self, task_id: int) -> None:
        """这个插件的一个任务已经交给 QGIS，现在被记账。

        A task of this plugin has reached QGIS and is now held.
        """

        self._live.add(task_id)

    def release(self, task_id: int) -> None:
        """任务结束了、QGIS 正在删除它；最后一个结束等待。

        A task has ended and QGIS is deleting it; the last one ends the wait.
        """

        self._live.discard(task_id)
        if self._closed and not self._live:
            self._finish()

    def close(self) -> None:
        """停止记账，没有活任务时就立即释放。

        Stop holding work, and dispose now if no task is live.
        """

        self._closed = True
        if not self._live:
            self._finish()

    @property
    def live(self) -> frozenset[int]:
        """仍然活着的任务 id；supervisor 做完之后为空。

        The task ids still alive; empty once this supervisor is done.
        """

        return frozenset(self._live)

    def _finish(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        # 放手这两个对象正是这个类存在的意义：从这里开始，引擎、适配器与任务的 Python 回调都不可达。
        # Letting go of these two is the point of the whole class: from here the engine, the adapter
        # and the tasks' Python callbacks are unreachable.
        self._engine = None
        self._runner = None
        self._dispose()


def application_supervisor(manager: Any, engine: Any, runner: Any) -> Supervisor:
    """作为 QGIS 应用子对象的 supervisor。

    Qt 在这个函数内部导入，所以 ``plugin`` 在裸解释器里仍然可导入。
    这个壳挂在应用上而不是插件上，这正是卸载要的；``deleteLater``
    是最后一个任务结束后删除它的主线程机制。

    The supervisor as a child of the QGIS application.

    Qt is imported inside this function, so ``plugin`` still imports in a bare
    interpreter.  The shell is parented to the application rather than to the plugin,
    which is what unload asks for, and ``deleteLater`` is the main-thread mechanism
    that removes it once the last task has ended.
    """

    from qgis.core import QgsApplication
    from qgis.PyQt.QtCore import QObject

    class ApplicationSupervisor(Supervisor, QObject):
        """跟着应用而不是插件被删除的 supervisor。

        A supervisor QGIS deletes with the application, not with the plugin.
        """

        def __init__(self) -> None:
            QObject.__init__(self, QgsApplication.instance())
            Supervisor.__init__(self, engine, runner, dispose=self.deleteLater)
            # 这个管理器只收本插件的任务，而 QGIS 在任务终止后才删除它：
            # 这两个信号就是全部记账，都不是轮询。
            # This manager takes only this plugin's tasks, and QGIS deletes each task after it has
            # terminated: those two signals are the whole bookkeeping, neither is a poll or a wait.
            manager.taskAdded.connect(self.attach)
            manager.taskAboutToBeDeleted.connect(self.release)

    return ApplicationSupervisor()


class Plugin:
    """QGIS 在 ``initGui`` 与 ``unload`` 之间保持活着的实例。

    The instance QGIS keeps alive between ``initGui`` and ``unload``.
    """

    def __init__(self, iface: Any) -> None:
        self.iface = iface
        self.registry: OperationRegistry | None = None
        self.engine: OperationEngine | None = None
        self.lifecycle: ProjectLifecycle | None = None
        self.supervisor: Supervisor | None = None
        # 手工测试用的坞窗；界面承载不了坞窗时为空。
        # The manual test surface; None when the interface cannot host a dock.
        self.dock: Any = None
        # 承载坞窗的对象：``iface`` 自己，或者回退时用的那个窗口。
        # Whoever hosts the dock: ``iface`` itself, or the window the fallback used.
        self._dock_host: Any = None
        # 把 logging 转给 QgsMessageLog 的 handler；unload 时移除。
        # The handler that forwards logging to QgsMessageLog; removed on unload.
        self._log_handler: Any = None

    def initGui(self) -> None:
        from qgis.core import QgsProject, QgsTaskManager

        from .core.context import ProjectLifecycle
        from .core.engine import OperationEngine
        from .core.registry import OperationRegistry
        from .core.runners.qgs_task import OperationTaskRunner
        from .operations import register_all

        settings = read_settings()
        lifecycle = ProjectLifecycle()
        # 这个插件和它的任务共用一个管理器：全局 cancelAll() 因此不可达，
        # 而 supervisor 盯的就是这同一个对象。
        # One manager for this plugin and its tasks: a session-wide cancelAll() is out of reach
        # when nothing else submits to it, and the supervisor watches this same object.
        manager = QgsTaskManager()
        runner = OperationTaskRunner(task_manager=manager)
        registry = OperationRegistry()
        register_all(registry)
        project = QgsProject.instance()
        self.registry = registry
        self.lifecycle = lifecycle
        self.engine = OperationEngine(
            registry,
            settings,
            lifecycle,
            iface=self.iface,
            project=project,
            background_runner=runner,
            background_cancel=runner.cancel,
        )
        self.supervisor = application_supervisor(manager, self.engine, runner)
        # 代数只在这里加：aboutToBeCleared 只算一次，第二个计数器只会变成第二个真相。
        # The only place the epoch is bumped: aboutToBeCleared counts once, and a second counter
        # would be a second truth.
        project.aboutToBeCleared.connect(self._project_about_to_be_cleared)
        self._create_dock(registry, self.engine)
        self._publish()
        # 日志转发装在最后：这之后没有会抛的步骤，装配中途死掉的会话不会把 handler 漏给进程；
        # unload 把它拆掉。
        # Forwarding goes in last: nothing after this can throw, so a session that dies
        # half-assembled leaks no handler into the process; unload removes it.
        self._log_handler = _message_log_handler()
        logging.getLogger(__package__).addHandler(self._log_handler)

    def unload(self) -> None:
        from qgis.core import QgsProject

        # 什么都没连上：重复卸载，或者根本没加载。
        # Nothing of ours connected: a repeated unload, or no load at all.
        with suppress(TypeError, RuntimeError):
            QgsProject.instance().aboutToBeCleared.disconnect(self._project_about_to_be_cleared)
        # 坞窗先撤：它的轮询否则会碰上一个正在关闭的引擎。
        # The dock goes first: its poll would otherwise touch an engine that is closing.
        self._remove_dock()
        engine, supervisor = self.engine, self.supervisor
        self._withdraw()
        self.engine = self.registry = self.lifecycle = self.supervisor = None
        if engine is not None:
            # 先拒绝新工作，再取消从未开始的调用，
            # 并请这个插件的每个任务停下，从不全局取消。
            # Refuse new work first, then cancel what never started and ask every task of this
            # plugin to stop, never a global cancel.
            engine.shutdown()
        if supervisor is not None:
            # 仍在运行的保持可达，直到最后一个任务终止，然后壳在主线程上清掉自己。
            # What is still running stays reachable until the last task has terminated, then the
            # shell clears itself on the main thread.
            supervisor.close()
        # 会话结束，转发随它拆掉；重复卸载时 handler 已经是 None。
        # The session is over and its forwarding goes with it; on a repeated unload
        # the handler is already None.
        handler, self._log_handler = self._log_handler, None
        if handler is not None:
            logging.getLogger(__package__).removeHandler(handler)

    def _project_about_to_be_cleared(self) -> None:
        """先换一代工程，然后丢掉旧一代的工作。

        One new project generation, then the old one's work is dropped.
        """

        lifecycle, engine = self.lifecycle, self.engine
        if lifecycle is None or engine is None:
            # 排队中的信号可能在卸载之后到达；槽不能抛异常。
            # A queued signal may arrive after unload; a slot may not raise.
            return
        lifecycle.bump()
        try:
            # 代数在这次调用之前已经加过，所以替换的取消那一半仍要发生
            # （引擎在 ``finally`` 里补完）；抛异常只记日志。
            # The epoch has already moved, so the cancel half of the replacement still has to
            # happen (the engine finishes it in a ``finally``); a raise only logs.
            engine.on_project_replaced()
        except Exception:  # noqa: BLE001 - a Qt slot may not raise
            _LOGGER.exception("the project replacement could not be recorded")

    def _create_dock(self, registry: OperationRegistry, engine: OperationEngine) -> None:
        """创建操作坞窗并把它挂到右边，每个会话一次。

        承载不了坞窗的界面（比如无头验收用的假 iface）就不建：
        没有它会话也是完整的，而且那种情况下根本不会导入 Qt。

        Create the operation dock and dock it on the right, once per session.

        An interface that cannot host a dock -- a faked one, as the headless
        acceptance harness has -- gets none: the session is whole without it, and Qt
        is not imported at all in that case.
        """

        self._remove_dock()
        host = self.iface
        add = getattr(host, "addDockWidget", None)
        if add is None:
            window = getattr(host, "mainWindow", None)
            host = window() if window is not None else None
            if host is None:
                return
            add = host.addDockWidget
        try:
            from qgis.PyQt.QtCore import Qt

            from .ui.dock import OperationDock
        except ImportError:
            return
        dock = OperationDock(registry, engine)
        add(Qt.RightDockWidgetArea, dock)
        self.dock = dock
        # 同一个宿主挂上去，也由它摘下来，所以 mainWindow() 回退不是半条路。
        # The same host that docked it is the one that undocks it, so the mainWindow() fallback
        # is a real one.
        self._dock_host = host

    def _remove_dock(self) -> None:
        """从当初挂它的宿主上摘下来，然后交给 Qt 删除。

        Take the dock off the host it was added to, then let Qt delete it.
        """

        dock, self.dock = self.dock, None
        host, self._dock_host = self._dock_host, None
        if dock is None:
            return
        dock.shutdown()
        remove = getattr(host, "removeDockWidget", None)
        if remove is not None:
            remove(dock)
        dock.deleteLater()

    def _publish(self) -> None:
        """把会话放到 Python Console 找它的地方。

        Put the session where the Python Console looks for it.
        """

        setattr(self.iface, ENGINE_ATTRIBUTE, self.engine)
        setattr(self.iface, REGISTRY_ATTRIBUTE, self.registry)

    def _withdraw(self) -> None:
        """把这些句柄收回，禁用的插件不会留下一个会话。

        Take those handles back, so a disabled plugin leaves no session behind.
        """

        for name in (ENGINE_ATTRIBUTE, REGISTRY_ATTRIBUTE):
            if hasattr(self.iface, name):
                delattr(self.iface, name)
