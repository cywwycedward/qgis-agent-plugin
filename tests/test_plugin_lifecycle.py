"""插件对外的那一层：十七个操作、每次加载一个会话，以及 ``unload`` 之后剩下什么。

QGIS 与 Qt 都按 ``test_background`` 的方式伪造，因为这里要验的是插件自己的簿记：
它填的注册表、它连接又断开的那个信号、它只推进一步的工程代数，以及活到最后一项
任务结束的 supervisor。真正 QgsTask 的销毁、真实的应用父对象与真正的重载留给验收测试。

The plugin surface: seventeen operations, one session per load, and what unload leaves.
QGIS and Qt are faked the way ``test_background`` does it, because the plugin's own
bookkeeping is what is under test: the registry it fills, the signal it connects and
disconnects, the epoch it bumps once, and the supervisor that outlives it until the last
task is gone. Real QgsTask teardown, a real application parent and a real reload are the
acceptance run's business.
"""

from __future__ import annotations

import logging
import sys
import types
import weakref
from typing import Any

import pytest

from qgis_agent_plugin import plugin
from qgis_agent_plugin.core.config import Settings
from qgis_agent_plugin.core.models import (
    DUPLICATE_OPERATION,
    ENGINE_CLOSING,
    ExecutionStatus,
    FrameworkError,
)
from qgis_agent_plugin.core.registry import OperationRegistry
from qgis_agent_plugin.core.runners import qgs_task
from qgis_agent_plugin.operations import register_all

# 构建必须恰好注册这个集合，否则就是错的 / the build registers exactly this set, or it is wrong
PRD_NAMES = frozenset(
    {
        "project.create",
        "project.open",
        "project.save",
        "project.get_info",
        "layer.add",
        "layer.list",
        "layer.get_info",
        "layer.set_visibility",
        "layer.sum_numeric_field",
        "layer.list_style_categories",
        "layer.get_qml",
        "layer.list_available_renderers",
        "layer.get_renderer",
        "layer.set_renderer",
        "processing.list_algorithms",
        "processing.get_algorithm_info",
        "processing.run_algorithm",
    }
)


# --- 伪造的 QGIS / the fake QGIS ----------------------------------------------


class FakeSignal:
    """Qt 信号只留插件用到的那三次调用。

    连接不去重（``QObject.connect`` 也不去重），所以上一次加载遗留的槽必须能以陈旧
    接收者的身份暴露出来。

    A Qt signal reduced to the three calls the plugin makes. Connections are not
    de-duplicated, the way ``QObject.connect`` is not: a slot left behind by an earlier
    load has to show up as a stale receiver.
    """

    def __init__(self) -> None:
        self.slots: list[Any] = []

    def connect(self, slot: Any) -> None:
        self.slots.append(slot)

    def disconnect(self, slot: Any) -> None:
        if slot not in self.slots:
            raise TypeError("disconnect(): not connected")
        self.slots.remove(slot)

    def emit(self, *args: Any) -> Any:
        for slot in list(self.slots):
            slot(*args)


class FakeCrs:
    def authid(self) -> str:
        return "EPSG:4326"


class FakeProject:
    """工程单例，按插件的读法：一个信号加四路读取。

    The project singleton as the plugin reads it: one signal and four reads.
    """

    def __init__(self) -> None:
        self.aboutToBeCleared = FakeSignal()

    def fileName(self) -> str:
        return ""

    def crs(self) -> FakeCrs:
        return FakeCrs()

    def isDirty(self) -> bool:
        return False

    def mapLayers(self) -> dict[str, Any]:
        return {}


class FakeTaskManager:
    """插件自己的任务管理器：三个信号，从不真的加任务。

    This plugin's own manager: the three signals, and no task ever added.
    """

    def __init__(self) -> None:
        self.progressChanged = FakeSignal()
        self.taskAdded = FakeSignal()
        self.taskAboutToBeDeleted = FakeSignal()


class FakeQgsTask:
    """``QgsTask`` 只留适配器建子类所需的部分；什么都不会跑。

    Enough of ``QgsTask`` for the adapter to build its subclass; nothing is run.
    """

    CanCancel = 1

    def __init__(self, description: str = "", flags: int = 0) -> None:
        self.description = description
        self.flags = flags


class FakeQObject:
    """supervisor 的 Qt 那一面：谁持有它，以及它在主线程上怎样被删。

    The Qt side of the supervisor: who owns it, and its main-thread deletion.
    """

    def __init__(self, parent: Any = None) -> None:
        self.parent_object = parent
        self.deleted = False

    def deleteLater(self) -> None:
        self.deleted = True


class FakeApplication:
    """顶掉 ``QgsApplication.instance()``，即 supervisor 的持有者。

    Stands in for ``QgsApplication.instance()``, the supervisor's owner.
    """


class FakeSettingsStore:
    """``QgsSettings`` 压缩成一个映射，缺键时答 ``None``。

    ``QgsSettings``, reduced to a mapping that answers ``None`` for a missing key.
    """

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    def value(self, key: str) -> Any:
        return self.values.get(key)


class FakeMessageLog:
    """``QgsMessageLog`` 压成一份列表，日志面板的替身。

    ``QgsMessageLog`` reduced to a list, standing in for the log panel.
    """

    def __init__(self) -> None:
        self.lines: list[tuple[str, str, Any]] = []

    def logMessage(self, message: str, tag: str, level: Any) -> None:
        self.lines.append((message, tag, level))


class FakeQTimer:
    """把下一个事件循环回合记下来而不执行：单元测试里没有事件循环。

    The next event-loop turn, remembered instead of run: a unit test has no loop.
    """

    def __init__(self, pending: list[Any]) -> None:
        self._pending = pending

    def singleShot(self, delay: int, callback: Any) -> None:
        self._pending.append(callback)


def _module(name: str, **attributes: Any) -> types.ModuleType:
    """顶掉一个模块，它的属性就是要它回答的那些伪造对象。

    A stand-in module whose attributes are the fakes it has to answer with.
    """

    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


@pytest.fixture(autouse=True)
def _no_cached_task_class(monkeypatch: pytest.MonkeyPatch) -> None:
    """适配器会缓存它建的那个任务类，伪造的那个不能活过一条用例。

    The adapter caches the task class it built; a fake one may not outlive a test.
    """

    monkeypatch.setattr(qgs_task, "_TASK_CLASS", None)


@pytest.fixture
def fake_qgis(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """装一个顶掉的 ``qgis`` 包，让 ``plugin`` 与上面那些伪造对象对话。

    Install a stand-in ``qgis`` package, so ``plugin`` talks to the fakes above.
    """

    message_log = FakeMessageLog()
    state = types.SimpleNamespace(
        project=FakeProject(),
        application=FakeApplication(),
        managers=[],
        settings=FakeSettingsStore(),
        pending=[],
        message_log=message_log,
    )

    def new_manager() -> FakeTaskManager:
        manager = FakeTaskManager()
        state.managers.append(manager)
        return manager

    core = _module(
        "qgis.core",
        QgsProject=types.SimpleNamespace(instance=lambda: state.project),
        QgsApplication=types.SimpleNamespace(instance=lambda: state.application),
        QgsTaskManager=new_manager,
        QgsSettings=lambda: state.settings,
        QgsTask=FakeQgsTask,
        Qgis=types.SimpleNamespace(
            MessageLevel=types.SimpleNamespace(Info="info", Warning="warning", Critical="critical")
        ),
        QgsMessageLog=message_log,
    )
    qtcore = _module("qgis.PyQt.QtCore", QObject=FakeQObject, QTimer=FakeQTimer(state.pending))
    pyqt = _module("qgis.PyQt", QtCore=qtcore)
    qgis = _module("qgis", core=core, PyQt=pyqt)
    for name, module in (
        ("qgis", qgis),
        ("qgis.core", core),
        ("qgis.PyQt", pyqt),
        ("qgis.PyQt.QtCore", qtcore),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    return state


# --- 构建交付什么 / what the build ships ---------------------------------------


def test_the_build_registers_exactly_the_seventeen_operations_of_the_prd() -> None:
    registry = OperationRegistry()

    register_all(registry)

    assert {spec.name for spec in registry.list_specs()} == PRD_NAMES


def test_a_duplicate_is_still_the_registry_refusal() -> None:
    registry = OperationRegistry()
    register_all(registry)

    with pytest.raises(FrameworkError) as refusal:
        register_all(registry)

    assert refusal.value.code == DUPLICATE_OPERATION


def test_the_settings_keep_their_defaults_for_every_key_nobody_set(fake_qgis) -> None:
    assert plugin.read_settings() == Settings()

    fake_qgis.settings.values["qgis_agent_plugin/max_pending"] = 4
    fake_qgis.settings.values["qgis_agent_plugin/left_over_key"] = "C:/data"
    settings = plugin.read_settings()

    assert settings.max_pending == 4
    # 没人设过的键取默认值，既不是 0 也不是缺失
    # an untouched key keeps its default, which is neither zero nor a missing value
    assert settings.max_active_background == 2
    # 旧配置留下的键不进 Settings，也不让加载失败
    # a key left over from an older config neither reaches Settings nor fails loading
    assert not hasattr(settings, "allowed_data_roots")


# --- supervisor / 托管 -------------------------------------------------------


def test_the_supervisor_holds_work_until_the_last_task_is_gone() -> None:
    disposed: list[bool] = []
    supervisor = plugin.Supervisor(object(), object(), dispose=lambda: disposed.append(True))
    supervisor.attach(7)
    supervisor.attach(9)
    supervisor.close()

    assert supervisor.live == frozenset({7, 9})
    assert disposed == []

    supervisor.release(7)
    assert supervisor.live == frozenset({9})
    assert disposed == []

    supervisor.release(9)
    assert supervisor.live == frozenset()
    assert disposed == [True]


def test_a_supervisor_with_nothing_live_disposes_itself_at_once() -> None:
    disposed: list[bool] = []
    supervisor = plugin.Supervisor(object(), object(), dispose=lambda: disposed.append(True))

    supervisor.close()
    supervisor.close()
    supervisor.release(1)  # 没持有过的 id 不算第二次释放 / an id never held is no second disposal

    assert disposed == [True]


def test_the_supervisor_lets_go_of_the_work_it_held() -> None:
    class Work:
        pass

    engine = Work()
    engine_ref = weakref.ref(engine)
    supervisor = plugin.Supervisor(engine, Work(), dispose=lambda: None)
    del engine
    assert engine_ref() is not None

    supervisor.close()

    assert engine_ref() is None


def test_the_shell_belongs_to_the_application_and_follows_its_manager(fake_qgis) -> None:
    manager = FakeTaskManager()
    supervisor = plugin.application_supervisor(manager, None, None)
    assert isinstance(supervisor, FakeQObject)

    assert supervisor.parent_object is fake_qgis.application

    manager.taskAdded.emit(11)
    manager.taskAdded.emit(12)
    supervisor.close()
    assert supervisor.live == frozenset({11, 12})
    assert supervisor.deleted is False

    manager.taskAboutToBeDeleted.emit(11)
    assert supervisor.deleted is False

    manager.taskAboutToBeDeleted.emit(12)
    assert supervisor.deleted is True
    assert supervisor.live == frozenset()


# --- initGui 与 unload / initGui and unload ------------------------------------


def test_three_loads_and_unloads_leave_no_connection_and_no_session(fake_qgis) -> None:
    iface = types.SimpleNamespace()
    registries = []

    for _ in range(3):
        instance = plugin.Plugin(iface)
        instance.initGui()
        engine = instance.engine
        registry = instance.registry
        supervisor = instance.supervisor
        assert engine is not None and registry is not None
        registries.append(registry)
        assert isinstance(supervisor, FakeQObject)

        assert iface.agent_engine is engine
        assert iface.agent_registry is registry
        assert {spec.name for spec in registry.list_specs()} == PRD_NAMES
        # 工程代数只由这一个信号推进，只连一次 / one connection to the signal that bumps the epoch
        assert len(fake_qgis.project.aboutToBeCleared.slots) == 1
        assert supervisor.parent_object is fake_qgis.application

        # Console 就是这样调用的，底下是伪造的工程 / what the Console does, on a fake project
        record = engine.run_sync("project.get_info", {})
        assert record.status is ExecutionStatus.SUCCEEDED
        assert record.result is not None and record.result["project_epoch"] == 0

        instance.unload()

        assert fake_qgis.project.aboutToBeCleared.slots == []
        assert not hasattr(iface, "agent_engine")
        assert not hasattr(iface, "agent_registry")
        assert instance.engine is None and instance.registry is None
        # 什么都没在跑，所以外壳随会话一起消失
        # nothing was running, so the shell goes with the session
        assert supervisor.deleted is True

        # 新工作被拒，但会话的历史仍可读 / new work is refused, yet the history is readable
        with pytest.raises(FrameworkError) as refusal:
            engine.run_sync("project.get_info", {})
        assert refusal.value.code == ENGINE_CLOSING
        assert engine.get(record.execution_id).status is ExecutionStatus.SUCCEEDED

    # 每次加载都是新注册表、新 manager，所以没有操作被注册两次
    # a fresh registry and a fresh manager per load, so no operation is registered twice
    assert len({id(registry) for registry in registries}) == 3
    assert len(fake_qgis.managers) == 3


def test_session_logs_reach_the_message_log_and_stop_at_unload(fake_qgis) -> None:
    """会话日志进 QgsMessageLog，卸载后不再进。

    坞窗把操作者指向 QGIS 日志面板，所以 initGui 必须把 Python logging 转发进面板；
    unload 拆掉转发，之后的行面板看不见。

    Session logs reach QgsMessageLog, and stop at unload. The dock points the operator at
    the QGIS log panel, so initGui must forward Python logging into the panel; unload
    removes the forwarding, and later lines never reach it.
    """

    iface = types.SimpleNamespace()
    instance = plugin.Plugin(iface)
    instance.initGui()

    plugin._LOGGER.error("a session error")
    assert ("a session error", "QGIS Agent") in [
        (message, tag) for message, tag, _level in fake_qgis.message_log.lines
    ]

    instance.unload()

    before = len(fake_qgis.message_log.lines)
    plugin._LOGGER.error("an error after unload")
    assert len(fake_qgis.message_log.lines) == before


def test_a_count_key_below_one_refuses_the_load_and_publishes_nothing(fake_qgis) -> None:
    iface = types.SimpleNamespace()
    fake_qgis.settings.values["qgis_agent_plugin/max_pending"] = 0

    with pytest.raises(RuntimeError) as refusal:
        plugin.Plugin(iface).initGui()

    # 计数小于一就加载失败并点名该键，而且在往 iface 上挂任何东西之前，
    # 因为半成品会话看起来像一个永远起不来的引擎
    # a count below one stops the load and names the key before anything is published on iface,
    # because a half session there would look like an engine that never starts
    assert "max_pending" in str(refusal.value)
    assert not hasattr(iface, "agent_engine")
    assert not hasattr(iface, "agent_registry")


def test_unloading_keeps_a_task_that_is_still_running(fake_qgis) -> None:
    """活着的任务要活过 ``Plugin`` 实例。

    插件建的会话必须仍可达——从应用而不是从 QGIS 正在丢弃的实例——直到 QGIS 删掉
    最后一项任务；到那时 supervisor 才可以放手。

    A live task outlives the ``Plugin`` instance. The session the plugin built has to stay
    reachable from the application, not from the instance QGIS is dropping, until QGIS
    deletes the last task, and only then may the supervisor let go of it.
    """

    iface = types.SimpleNamespace()
    instance = plugin.Plugin(iface)
    instance.initGui()
    engine = instance.engine
    supervisor = instance.supervisor
    assert engine is not None and isinstance(supervisor, FakeQObject)

    # QGIS 已接下本插件提交的一项任务，它还在跑
    # QGIS has taken one task this plugin submitted, and that task is still running
    fake_qgis.managers[-1].taskAdded.emit(1)

    instance.unload()

    # 新工作立刻被拒 / new work is refused at once
    with pytest.raises(FrameworkError) as refusal:
        engine.run_sync("project.get_info", {})
    assert refusal.value.code == ENGINE_CLOSING

    # 活着的任务被留下，不随实例丢弃，由 supervisor 单独持有，另一条用例验证了这点
    # the live task is kept, not dropped with the plugin instance: the supervisor is the one
    # holder, as ``test_the_supervisor_lets_go_of_the_work_it_held`` shows
    assert supervisor.live == frozenset({1})
    assert supervisor.deleted is False

    # 最后一项任务已结束，外壳自己清理掉 / the last task is gone, so the shell clears itself
    fake_qgis.managers[-1].taskAboutToBeDeleted.emit(1)
    assert supervisor.live == frozenset()
    assert supervisor.deleted is True


def test_a_replaced_project_bumps_the_epoch_once_and_drops_the_old_work(fake_qgis) -> None:
    iface = types.SimpleNamespace()
    instance = plugin.Plugin(iface)
    instance.initGui()
    engine = instance.engine
    lifecycle = instance.lifecycle
    assert engine is not None and lifecycle is not None

    # 还没轮到事件循环的主线程调用 / a main-thread call yet to get its event-loop turn
    queued = engine.submit("project.get_info", {})
    assert queued.status is ExecutionStatus.QUEUED
    assert queued.project_epoch == 0

    fake_qgis.project.aboutToBeCleared.emit()

    assert lifecycle.current_epoch == 1
    assert engine.get(queued.execution_id).status is ExecutionStatus.CANCELLED

    fake_qgis.project.aboutToBeCleared.emit()

    assert lifecycle.current_epoch == 2
    instance.unload()


def test_a_replacement_that_cannot_be_recorded_only_logs(
    fake_qgis: types.SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """工程代数的槽也是 Qt 槽，所以抛出的异常只能进日志。

    引擎被叫到时工程代数已经推进了，于是下面出现的是守卫必须承受的半状态：代数保留，
    其余记日志。

    The epoch slot is a Qt slot too, so a raise may only reach the log. The epoch has
    already moved by the time the engine is asked, so what happens below is the half state
    the guard has to survive: the bump is kept, the rest is logged.
    """

    iface = types.SimpleNamespace()
    instance = plugin.Plugin(iface)
    instance.initGui()
    engine = instance.engine
    lifecycle = instance.lifecycle
    assert engine is not None and lifecycle is not None

    def refuse() -> None:
        raise RuntimeError("the store went away")

    monkeypatch.setattr(engine, "on_project_replaced", refuse)
    with caplog.at_level(logging.ERROR, logger=plugin.__name__):
        fake_qgis.project.aboutToBeCleared.emit()  # 槽不能抛 / a slot may not raise

    assert lifecycle.current_epoch == 1
    assert "the project replacement could not be recorded" in caplog.text
    # 之后会话仍完整：unload 走完六步 / the session stays whole: unload runs all six steps
    instance.unload()
    assert not hasattr(iface, "agent_engine")
    assert fake_qgis.project.aboutToBeCleared.slots == []


def test_unloading_a_plugin_that_never_loaded_does_nothing(fake_qgis) -> None:
    iface = types.SimpleNamespace()

    plugin.Plugin(iface).unload()
    plugin.Plugin(iface).unload()

    assert not hasattr(iface, "agent_engine")
    assert fake_qgis.project.aboutToBeCleared.slots == []
