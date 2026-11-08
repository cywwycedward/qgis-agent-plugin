# ruff: noqa: E402 - the platform and the Qt import have to precede the dock import.

"""The operation dock: schema to form, form to arguments, and one record on screen.

The dock is the manual test entry (F10), so what it shows is asserted here: the
list order and labels, the editor each property becomes, which fields become
arguments, the two engine paths (``run_sync`` for a main-thread call, ``submit`` plus
polling for a background one), the JSON in the result box, and the two ways a
failure must become text instead of reaching the Qt slot.

Qt is real and offscreen; QGIS is only faked where ``initGui`` needs it.  The dock
is the one module that imports Qt at module level, so this whole module skips
rather than fails in the bare interpreter (``tests/test_package.py`` keeps that
rule intact).
"""

import json
import os
import sys
import time
import types
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("qgis.PyQt.QtWidgets", reason="the operation dock needs Qt")

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QDoubleSpinBox,
    QFormLayout,
    QLineEdit,
    QMainWindow,
    QSpinBox,
)

from qgis_agent_plugin import plugin
from qgis_agent_plugin.core.models import (
    ENGINE_CLOSING,
    INTERNAL_ERROR,
    CancelReceipt,
    ErrorDetail,
    ExecutionRecord,
    ExecutionStatus,
    ExecutionStrategy,
    FrameworkError,
    OperationKind,
    OperationSpec,
)
from qgis_agent_plugin.core.registry import OperationRegistry
from qgis_agent_plugin.operations import register_all
from qgis_agent_plugin.ui.dock import OperationDock

# Every editor kind in one schema: text, bool, integer, number, enum, a nullable
# string and a nullable array, with two required fields and defaults on the rest.
FORM = {
    "type": "object",
    "properties": {
        "name": {"title": "Name", "type": "string", "minLength": 1},
        "flag": {"default": True, "title": "Flag", "type": "boolean"},
        "count": {"default": 3, "title": "Count", "type": "integer"},
        "ratio": {"default": 0.5, "title": "Ratio", "type": "number"},
        "kind": {
            "default": "raster",
            "enum": ["vector", "raster"],
            "title": "Kind",
            "type": "string",
        },
        "note": {"title": "Note", "anyOf": [{"type": "string"}, {"type": "null"}]},
        "items": {
            "default": [1, 2],
            "title": "Items",
            "anyOf": [{"items": {"type": "integer"}, "type": "array"}, {"type": "null"}],
        },
    },
    "required": ["name", "kind"],
}


@pytest.fixture(scope="session")
def qapp() -> Any:
    """One Qt application for this module; offscreen, with no QGIS involved."""

    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _qt(qapp: Any) -> Any:
    """Every test here builds widgets, so the application has to exist first."""

    return qapp


class FakeRegistry:
    """``list_specs`` only: the dock never asks the registry anything else."""

    def __init__(self, *specs: OperationSpec) -> None:
        self._specs = list(specs)

    def list_specs(self) -> list[OperationSpec]:
        return list(self._specs)


class FakeEngine:
    """The four engine methods the dock may call, answering from a script.

    A list answer is one reply per call with the last one repeating, so a poll
    sequence is spelled out and an exhausted script fails the test instead of
    spinning the event loop forever.
    """

    def __init__(self, **answers: Any) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self._answers = answers

    def run_sync(self, name: str, arguments: dict[str, Any]) -> ExecutionRecord:
        self.calls.append(("run_sync", name, arguments))
        return self._answer("run_sync")

    def submit(self, name: str, arguments: dict[str, Any]) -> ExecutionRecord:
        self.calls.append(("submit", name, arguments))
        return self._answer("submit")

    def get(self, execution_id: str) -> ExecutionRecord:
        self.calls.append(("get", execution_id))
        return self._answer("get")

    def cancel(self, execution_id: str) -> CancelReceipt:
        self.calls.append(("cancel", execution_id))
        return self._answer("cancel")

    def _answer(self, name: str) -> Any:
        answer = self._answers[name]
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _spec(
    name: str = "demo.op",
    strategy: ExecutionStrategy = ExecutionStrategy.MAIN_THREAD,
    schema: dict[str, Any] | None = None,
) -> OperationSpec:
    return OperationSpec(
        name=name,
        description="a demo operation",
        kind=OperationKind.QUERY,
        strategy=strategy,
        destructive=False,
        input_schema=schema if schema is not None else {"type": "object", "properties": {}},
        output_schema={"type": "object"},
        pydantic_major=2,
    )


def _record(
    status: ExecutionStatus = ExecutionStatus.SUCCEEDED,
    strategy: ExecutionStrategy = ExecutionStrategy.MAIN_THREAD,
    *,
    execution_id: str = "e1",
    progress: float = 0,
    result: dict[str, Any] | None = None,
    error: ErrorDetail | None = None,
) -> ExecutionRecord:
    """A record in one of the states the dock has to show."""

    fields: dict[str, Any] = {
        "execution_id": execution_id,
        "operation": "demo.op",
        "kind": OperationKind.QUERY,
        "strategy": strategy,
        "status": status,
        "progress": progress,
        "created_at": "2026-01-01T00:00:00+00:00",
        "project_epoch": 0,
    }
    finished = "2026-01-01T00:00:01+00:00"
    if status is ExecutionStatus.SUCCEEDED:
        fields.update(
            progress=100,
            result=result if result is not None else {"answer": 42},
            finished_at=finished,
        )
    elif status is ExecutionStatus.FAILED:
        fields.update(
            error=error or ErrorDetail(code=INTERNAL_ERROR, message="it broke"),
            finished_at=finished,
        )
    elif status is ExecutionStatus.CANCELLED:
        fields.update(finished_at=finished)
    return ExecutionRecord.model_validate(fields)


def _dock(engine: FakeEngine, *specs: OperationSpec) -> OperationDock:
    return OperationDock(FakeRegistry(*specs or (_spec(),)), engine)


def _wait_until(predicate: Any, timeout_ms: int = 2000) -> bool:
    """Pump the event loop until the predicate holds, so the poll timer can fire."""

    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if predicate():
            return True
        QTest.qWait(25)
    return predicate()


# --- the operation list ------------------------------------------------------


def test_the_list_is_sorted_and_shows_kind_and_strategy(qapp: Any) -> None:
    registry = FakeRegistry(_spec("layer.sum", ExecutionStrategy.BACKGROUND), _spec("layer.list"))

    dock = OperationDock(registry, FakeEngine())

    labels = [dock.operations.item(row).text() for row in range(dock.operations.count())]
    assert labels == [
        "layer.list  [query · main_thread]",
        "layer.sum  [query · background]",
    ]
    assert dock.empty.isHidden()


def test_an_empty_registry_says_so_instead_of_showing_nothing(qapp: Any) -> None:
    dock = OperationDock(FakeRegistry(), FakeEngine())

    assert dock.operations.count() == 0
    assert not dock.empty.isHidden()
    assert dock.empty.text() == "没有注册的操作"


def test_every_registered_operation_gets_a_form_from_its_own_schema(qapp: Any) -> None:
    """The nine PRD operations, whose schemas carry enums, bools and nullable unions."""

    registry = OperationRegistry()
    register_all(registry)
    dock = OperationDock(registry, FakeEngine())

    for row, spec in enumerate(registry.list_specs()):
        dock.operations.setCurrentRow(row)
        properties = spec.input_schema["properties"]
        assert set(dock.editors) == set(properties)
        # An operation without parameters still gets its one "takes no arguments" row.
        assert dock.form.rowCount() == max(len(properties), 1)


def test_the_dock_is_something_a_real_main_window_accepts(qapp: Any) -> None:
    """The r1 finding: a plain ``QWidget`` is refused by ``addDockWidget``."""

    window = QMainWindow()
    dock = _dock(FakeEngine())

    window.addDockWidget(Qt.RightDockWidgetArea, dock)  # the real sip type check
    assert window.dockWidgetArea(dock) == Qt.RightDockWidgetArea

    window.removeDockWidget(dock)
    assert window.dockWidgetArea(dock) == Qt.NoDockWidgetArea


# --- the form ----------------------------------------------------------------


def test_a_property_becomes_the_editor_its_type_asks_for(qapp: Any) -> None:
    dock = _dock(FakeEngine(), _spec(schema=FORM))
    editors = {name: widget for name, (_, widget) in dock.editors.items()}

    assert set(editors) == {"name", "flag", "count", "ratio", "kind", "note", "items"}
    assert isinstance(editors["flag"], QCheckBox)
    assert editors["flag"].isChecked()
    assert isinstance(editors["count"], QSpinBox)
    assert editors["count"].value() == 3
    assert isinstance(editors["ratio"], QDoubleSpinBox)
    assert editors["ratio"].value() == 0.5
    assert isinstance(editors["kind"], QComboBox)
    assert editors["kind"].currentText() == "raster"
    assert [editors["kind"].itemText(row) for row in range(editors["kind"].count())] == [
        "vector",
        "raster",
    ]
    assert isinstance(editors["name"], QLineEdit)
    assert isinstance(editors["items"], QLineEdit)
    assert editors["items"].text() == "[1, 2]"
    # The required ones carry the marker; the optional ones do not.
    assert dock.form.labelForField(editors["name"]).text() == "Name *"
    assert dock.form.labelForField(editors["kind"]).text() == "Kind *"
    assert dock.form.labelForField(editors["note"]).text() == "Note"


def test_an_empty_field_is_left_to_the_model_default(qapp: Any) -> None:
    engine = FakeEngine(run_sync=_record())
    dock = _dock(engine, _spec(schema=FORM))

    dock.editors["name"][1].setText("")
    dock.editors["note"][1].setText("  ")
    dock.run()

    assert engine.calls == [
        (
            "run_sync",
            "demo.op",
            {"flag": True, "count": 3, "ratio": 0.5, "kind": "raster", "items": [1, 2]},
        )
    ]


def test_a_field_that_is_not_json_stops_before_the_engine(qapp: Any) -> None:
    engine = FakeEngine(run_sync=_record())
    dock = _dock(engine, _spec(schema=FORM))

    dock.editors["items"][1].setText("{not json")
    dock.run()

    assert engine.calls == []
    assert "items" in dock.error.text()


def _fields(form: QFormLayout) -> list[Any]:
    """The editor widgets the form shows right now, in row order."""

    return [form.itemAt(row, QFormLayout.FieldRole).widget() for row in range(form.rowCount())]


def test_switching_operation_rebuilds_the_whole_form(qapp: Any) -> None:
    """A switch replaces the rows: no editor of the old operation may stay behind."""

    two = {
        "type": "object",
        "properties": {
            "alpha": {"title": "Alpha", "type": "string"},
            "beta": {"title": "Beta", "type": "string"},
        },
    }
    one = {"type": "object", "properties": {"gamma": {"title": "Gamma", "type": "string"}}}
    registry = FakeRegistry(_spec("first.op", schema=two), _spec("second.op", schema=one))

    dock = OperationDock(registry, FakeEngine())

    assert _fields(dock.form) == [dock.editors["alpha"][1], dock.editors["beta"][1]]

    dock.operations.setCurrentRow(1)

    assert set(dock.editors) == {"gamma"}
    assert _fields(dock.form) == [dock.editors["gamma"][1]]

    dock.operations.setCurrentRow(0)

    assert _fields(dock.form) == [dock.editors["alpha"][1], dock.editors["beta"][1]]


# --- running -----------------------------------------------------------------


def test_a_main_thread_operation_is_run_synchronously(qapp: Any) -> None:
    engine = FakeEngine(run_sync=_record(result={"layer_count": 0}))
    dock = _dock(engine)

    dock.run()

    assert engine.calls == [("run_sync", "demo.op", {})]
    assert "SUCCEEDED" in dock.status.text()
    assert dock.error.text() == ""
    assert not dock.timer.isActive()
    shown = json.loads(dock.result.toPlainText())
    assert shown["status"] == "SUCCEEDED"
    assert shown["result"] == {"layer_count": 0}


def test_a_background_operation_is_submitted_and_polled_until_it_ends(qapp: Any) -> None:
    engine = FakeEngine(
        submit=_record(ExecutionStatus.QUEUED, ExecutionStrategy.BACKGROUND),
        get=[
            _record(ExecutionStatus.RUNNING, ExecutionStrategy.BACKGROUND, progress=40),
            _record(ExecutionStatus.SUCCEEDED, ExecutionStrategy.BACKGROUND, result={"total": 3}),
        ],
    )
    dock = _dock(engine, _spec(strategy=ExecutionStrategy.BACKGROUND))

    dock.run()

    assert engine.calls[0][0] == "submit"
    assert dock.timer.isActive()
    assert dock.cancel_button.isEnabled()
    assert _wait_until(lambda: not dock.timer.isActive())
    assert [call[0] for call in engine.calls] == ["submit", "get", "get"]
    assert json.loads(dock.result.toPlainText())["status"] == "SUCCEEDED"
    assert not dock.cancel_button.isEnabled()


def test_a_live_background_call_owns_the_run_button(qapp: Any) -> None:
    """While one call polls, a second run would steal its record and its cancel button."""

    engine = FakeEngine(
        submit=_record(ExecutionStatus.QUEUED, ExecutionStrategy.BACKGROUND),
        get=_record(ExecutionStatus.SUCCEEDED, ExecutionStrategy.BACKGROUND),
    )
    dock = _dock(engine, _spec(strategy=ExecutionStrategy.BACKGROUND))

    dock.run()

    assert not dock.run_button.isEnabled()
    assert dock.cancel_button.isEnabled()

    assert _wait_until(lambda: not dock.timer.isActive())

    assert dock.run_button.isEnabled()
    assert not dock.cancel_button.isEnabled()


def test_a_submit_that_is_refused_at_once_is_not_polled(qapp: Any) -> None:
    refused = _record(
        ExecutionStatus.FAILED,
        ExecutionStrategy.BACKGROUND,
        error=ErrorDetail(code=INTERNAL_ERROR, message="no runner"),
    )
    engine = FakeEngine(submit=refused)
    dock = _dock(engine, _spec(strategy=ExecutionStrategy.BACKGROUND))

    dock.run()

    assert not dock.timer.isActive()
    assert "no runner" in dock.error.text()


def test_cancel_shows_the_receipt_then_the_cancelled_record(qapp: Any) -> None:
    engine = FakeEngine(
        submit=_record(ExecutionStatus.QUEUED, ExecutionStrategy.BACKGROUND),
        get=[
            _record(ExecutionStatus.CANCEL_REQUESTED, ExecutionStrategy.BACKGROUND),
            _record(ExecutionStatus.CANCELLED, ExecutionStrategy.BACKGROUND),
        ],
        cancel=CancelReceipt(
            execution_id="e1",
            accepted=True,
            status=ExecutionStatus.CANCEL_REQUESTED,
        ),
    )
    dock = _dock(engine, _spec(strategy=ExecutionStrategy.BACKGROUND))

    dock.run()
    dock.cancel()

    assert engine.calls[-1] == ("cancel", "e1")
    assert "accepted=True" in dock.status.text()
    assert _wait_until(lambda: not dock.timer.isActive())
    assert json.loads(dock.result.toPlainText())["status"] == "CANCELLED"


# --- failures never reach the slot -------------------------------------------


def test_a_refused_call_is_shown_in_the_dock_not_raised(qapp: Any) -> None:
    engine = FakeEngine(run_sync=FrameworkError(ENGINE_CLOSING, "the engine is shutting down"))
    dock = _dock(engine)

    dock.run()

    assert "ENGINE_CLOSING" in dock.error.text()
    assert "shutting down" in dock.error.text()
    assert dock.result.toPlainText() == ""
    assert dock.run_button.isEnabled()


def test_an_unexpected_failure_is_shown_not_raised(qapp: Any) -> None:
    engine = FakeEngine(run_sync=RuntimeError("boom"))
    dock = _dock(engine)

    dock.run()

    assert "RuntimeError" in dock.error.text()
    assert "boom" in dock.error.text()
    assert dock.run_button.isEnabled()


def test_a_failure_while_polling_ends_the_poll_and_is_shown(qapp: Any) -> None:
    engine = FakeEngine(
        submit=_record(ExecutionStatus.QUEUED, ExecutionStrategy.BACKGROUND),
        get=FrameworkError(ENGINE_CLOSING, "the engine is shutting down"),
    )
    dock = _dock(engine, _spec(strategy=ExecutionStrategy.BACKGROUND))

    dock.run()

    assert _wait_until(lambda: "ENGINE_CLOSING" in dock.error.text())
    assert not dock.timer.isActive()


# --- the plugin's half of the lifecycle --------------------------------------

# ``initGui`` also builds the engine and its QgsTask adapter, so this half of the
# module fakes ``qgis.core``: the real Qt is what the dock is built with, and it is
# the one thing the dock needs from QGIS.


class _Signal:
    """A Qt signal as the plugin uses it: connect, disconnect, emit."""

    def __init__(self) -> None:
        self.slots: list[Any] = []

    def connect(self, slot: Any) -> None:
        self.slots.append(slot)

    def disconnect(self, slot: Any) -> None:
        if slot not in self.slots:
            raise TypeError("disconnect(): not connected")
        self.slots.remove(slot)

    def emit(self, *args: Any) -> None:
        for slot in list(self.slots):
            slot(*args)


class _FakeTask:
    """Enough of ``QgsTask`` for the adapter to build its subclass; nothing runs."""

    CanCancel = 1

    def __init__(self, description: str = "", flags: int = 0) -> None:
        self.description = description
        self.flags = flags


class FakeIface:
    """The interface calls the dock lifecycle makes, backed by a real main window.

    ``addDockWidget`` is the window's own, so a dock of the wrong Qt type is refused
    here exactly as ``QgisInterface`` refuses it.  A fake that accepted any object is
    what let a plain ``QWidget`` pass review r1.
    """

    def __init__(self, window: QMainWindow) -> None:
        self.window = window

    def addDockWidget(self, area: Any, dock: Any) -> None:
        self.window.addDockWidget(area, dock)

    def removeDockWidget(self, dock: Any) -> None:
        self.window.removeDockWidget(dock)

    def mainWindow(self) -> QMainWindow:
        return self.window

    @property
    def docks(self) -> list[Any]:
        """What is docked right now: a removed dock keeps its parent but leaves the layout."""

        return [
            dock
            for dock in self.window.findChildren(QDockWidget)
            if self.window.dockWidgetArea(dock) != Qt.NoDockWidgetArea
        ]


class WindowOnlyIface:
    """An interface that can dock only through ``mainWindow()``: the F10 fallback."""

    def __init__(self, window: QMainWindow) -> None:
        self.window = window

    def mainWindow(self) -> QMainWindow:
        return self.window


@pytest.fixture
def fake_qgis(qapp: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """``qgis.core`` faked, ``qgis.PyQt`` the real one, so ``initGui`` can run here."""

    project = types.SimpleNamespace(
        aboutToBeCleared=_Signal(),
        fileName=lambda: "",
        crs=lambda: types.SimpleNamespace(authid=lambda: "EPSG:4326"),
        isDirty=lambda: False,
        mapLayers=dict,
    )
    manager = types.SimpleNamespace(
        progressChanged=_Signal(),
        taskAdded=_Signal(),
        taskAboutToBeDeleted=_Signal(),
    )
    core = types.ModuleType("qgis.core")
    core.__dict__.update(
        QgsProject=types.SimpleNamespace(instance=lambda: project),
        QgsApplication=types.SimpleNamespace(instance=lambda: qapp),
        QgsTaskManager=lambda: manager,
        QgsSettings=lambda: types.SimpleNamespace(value=lambda key: None),
        QgsTask=_FakeTask,
    )
    qgis = types.ModuleType("qgis")
    qgis.core = core
    # Kept as it is: the dock is Qt-only, and that is the point of this half.
    qgis.PyQt = sys.modules["qgis.PyQt"]
    monkeypatch.setitem(sys.modules, "qgis", qgis)
    monkeypatch.setitem(sys.modules, "qgis.core", core)
    return types.SimpleNamespace(project=project, manager=manager)


def test_a_second_load_leaves_one_dock_and_unload_stops_its_timer(
    fake_qgis: Any, qapp: Any
) -> None:
    iface = FakeIface(QMainWindow())
    instance = plugin.Plugin(iface)

    instance.initGui()
    first = instance.dock
    assert isinstance(first, OperationDock)
    assert iface.docks == [first]
    assert iface.window.dockWidgetArea(first) == Qt.RightDockWidgetArea

    instance.initGui()
    second = instance.dock
    assert isinstance(second, OperationDock)
    assert second is not first
    assert iface.docks == [second]

    # A live poll is what a running background call leaves behind.
    second.timer.start()
    instance.unload()

    assert iface.docks == []
    assert instance.dock is None
    assert not second.timer.isActive()


def test_two_loads_and_unloads_leave_nothing_docked(fake_qgis: Any, qapp: Any) -> None:
    iface = FakeIface(QMainWindow())

    for _ in range(2):
        instance = plugin.Plugin(iface)
        instance.initGui()
        dock = instance.dock
        assert isinstance(dock, OperationDock)
        assert dock.operations.count() == 9
        dock.timer.start()

        instance.unload()

        assert iface.docks == []
        assert instance.dock is None
        assert not dock.timer.isActive()

    # ``unload`` twice is what QGIS does when a plugin is disabled while unloaded.
    plugin.Plugin(iface).unload()
    assert iface.docks == []


def test_the_main_window_fallback_docks_and_undocks_through_that_window(
    fake_qgis: Any, qapp: Any
) -> None:
    """r1 finding 5: the ``mainWindow()`` fallback has to be symmetric."""

    window = QMainWindow()
    iface = WindowOnlyIface(window)
    assert not hasattr(iface, "addDockWidget")
    instance = plugin.Plugin(iface)

    instance.initGui()
    dock = instance.dock
    assert isinstance(dock, OperationDock)
    assert window.dockWidgetArea(dock) == Qt.RightDockWidgetArea

    instance.unload()

    assert instance.dock is None
    assert window.dockWidgetArea(dock) == Qt.NoDockWidgetArea
