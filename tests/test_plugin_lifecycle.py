"""The plugin surface: nine operations, one session per load, and what unload leaves.

PRD 1.3 (the Console reaches the engine), 9.1 (the epoch), 9.3 (the six steps of
``unload``) and AT-25 (three loads and unloads) are decided here.  QGIS and Qt are
faked the way ``test_background`` fakes them: the plugin's own bookkeeping is what
is under test -- the registry it fills, the signal it connects and disconnects, the
epoch it bumps once, and the supervisor that outlives it until the last task is
gone.  Real QgsTask teardown, a real application parent and a real plugin reload are
the acceptance run's business (F7).
"""

from __future__ import annotations

import sys
import types
import weakref
from pathlib import Path
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

# The PRD 7.1 table, spelled out: the build registers this set or it is wrong.
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
    }
)


# --- the fake QGIS -----------------------------------------------------------


class FakeSignal:
    """A Qt signal reduced to the three calls the plugin makes.

    Connections are not de-duplicated, the way ``QObject.connect`` is not: a slot
    left behind by an earlier load has to show up as a stale receiver.
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
    """The project singleton as the plugin reads it: one signal and four reads."""

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
    """This plugin's own manager: the three signals, and no task ever added."""

    def __init__(self) -> None:
        self.progressChanged = FakeSignal()
        self.taskAdded = FakeSignal()
        self.taskAboutToBeDeleted = FakeSignal()


class FakeQgsTask:
    """Enough of ``QgsTask`` for the adapter to build its subclass; nothing is run."""

    CanCancel = 1

    def __init__(self, description: str = "", flags: int = 0) -> None:
        self.description = description
        self.flags = flags


class FakeQObject:
    """The Qt side of the supervisor: who owns it, and its main-thread deletion."""

    def __init__(self, parent: Any = None) -> None:
        self.parent_object = parent
        self.deleted = False

    def deleteLater(self) -> None:
        self.deleted = True


class FakeApplication:
    """Stands in for ``QgsApplication.instance()``, the supervisor's owner."""


class FakeSettingsStore:
    """``QgsSettings``, reduced to a mapping that answers ``None`` for a missing key."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    def value(self, key: str) -> Any:
        return self.values.get(key)


class FakeQTimer:
    """The next event-loop turn, remembered instead of run: a unit test has no loop."""

    def __init__(self, pending: list[Any]) -> None:
        self._pending = pending

    def singleShot(self, delay: int, callback: Any) -> None:
        self._pending.append(callback)


def _module(name: str, **attributes: Any) -> types.ModuleType:
    """A stand-in module whose attributes are the fakes it has to answer with."""

    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


@pytest.fixture(autouse=True)
def _no_cached_task_class(monkeypatch: pytest.MonkeyPatch) -> None:
    """The adapter caches the task class it built; a fake one may not outlive a test."""

    monkeypatch.setattr(qgs_task, "_TASK_CLASS", None)


@pytest.fixture
def fake_qgis(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """Install a stand-in ``qgis`` package, so ``plugin`` talks to the fakes above."""

    state = types.SimpleNamespace(
        project=FakeProject(),
        application=FakeApplication(),
        managers=[],
        settings=FakeSettingsStore(),
        pending=[],
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


# --- what the build ships ----------------------------------------------------


def test_the_build_registers_exactly_the_nine_operations_of_the_prd() -> None:
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
    fake_qgis.settings.values["qgis_agent_plugin/allowed_data_roots"] = "C:/data"
    settings = plugin.read_settings()

    assert settings.max_pending == 4
    assert settings.allowed_data_roots == (Path("C:/data"),)
    # An untouched key is the PRD 10.1 default, not a zero and not a missing value.
    assert settings.max_active_background == 2
    assert settings.allowed_project_roots == ()


# --- the supervisor ----------------------------------------------------------


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
    supervisor.release(1)  # a task id it never held is not a second disposal

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


# --- initGui / unload --------------------------------------------------------


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
        # PRD 9.1: one connection to the one signal that bumps the epoch.
        assert len(fake_qgis.project.aboutToBeCleared.slots) == 1
        assert supervisor.parent_object is fake_qgis.application

        # PRD 1.3: what the Console does, with the fake project underneath.
        record = engine.run_sync("project.get_info", {})
        assert record.status is ExecutionStatus.SUCCEEDED
        assert record.result is not None and record.result["project_epoch"] == 0

        instance.unload()

        assert fake_qgis.project.aboutToBeCleared.slots == []
        assert not hasattr(iface, "agent_engine")
        assert not hasattr(iface, "agent_registry")
        assert instance.engine is None and instance.registry is None
        # PRD 9.3.5: nothing was running, so the shell is gone with the session.
        assert supervisor.deleted is True

        # PRD 9.3.1: new work is refused, the session's history is still readable.
        with pytest.raises(FrameworkError) as refusal:
            engine.run_sync("project.get_info", {})
        assert refusal.value.code == ENGINE_CLOSING
        assert engine.get(record.execution_id).status is ExecutionStatus.SUCCEEDED

    # A fresh registry and a fresh manager per load: no operation registered twice.
    assert len({id(registry) for registry in registries}) == 3
    assert len(fake_qgis.managers) == 3


def test_unloading_keeps_a_task_that_is_still_running(fake_qgis) -> None:
    """AT-13 and PRD 9.3.4/9.3.5: a live task outlives the ``Plugin`` instance.

    The session the plugin built has to stay reachable -- from the application, not
    from the instance QGIS is dropping -- until QGIS deletes the last task, and only
    then may the supervisor let go of it.
    """

    iface = types.SimpleNamespace()
    instance = plugin.Plugin(iface)
    instance.initGui()
    engine = instance.engine
    supervisor = instance.supervisor
    assert engine is not None and isinstance(supervisor, FakeQObject)

    # QGIS has taken one task this plugin submitted, and it is still running.
    fake_qgis.managers[-1].taskAdded.emit(1)

    instance.unload()

    # PRD 9.3.1: new work is refused at once.
    with pytest.raises(FrameworkError) as refusal:
        engine.run_sync("project.get_info", {})
    assert refusal.value.code == ENGINE_CLOSING

    # PRD 9.3.4: the live task is kept, not dropped with the plugin instance.  What
    # the supervisor holds is exactly that -- see
    # ``test_the_supervisor_lets_go_of_the_work_it_held``, where it is the only holder.
    assert supervisor.live == frozenset({1})
    assert supervisor.deleted is False

    # PRD 9.3.5: the last task has terminated, so the shell clears itself.
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

    # A main-thread call that has not had its event-loop turn yet (PRD 8.5).
    queued = engine.submit("project.get_info", {})
    assert queued.status is ExecutionStatus.QUEUED
    assert queued.project_epoch == 0

    fake_qgis.project.aboutToBeCleared.emit()

    assert lifecycle.current_epoch == 1
    assert engine.get(queued.execution_id).status is ExecutionStatus.CANCELLED

    fake_qgis.project.aboutToBeCleared.emit()

    assert lifecycle.current_epoch == 2
    instance.unload()


def test_unloading_a_plugin_that_never_loaded_does_nothing(fake_qgis) -> None:
    iface = types.SimpleNamespace()

    plugin.Plugin(iface).unload()
    plugin.Plugin(iface).unload()

    assert not hasattr(iface, "agent_engine")
    assert fake_qgis.project.aboutToBeCleared.slots == []
