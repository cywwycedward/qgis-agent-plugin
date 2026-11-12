# ruff: noqa: E402 - the platform and the Qt import have to precede the dock import.

"""The operation dock: the selector, the form, the six states, and one record on screen.

The dock is the manual test entry (F10) and an Operate surface (F11), so what is
asserted here is what an operator sees and can do: the operation each schema
becomes, which fields become arguments, the capsule and the controls in every
state (idle / running / ok / failed / cancelled / empty), the keyboard path, the
colour pairs the theme derives from a light and a dark palette, and the two
engine paths (``run_sync`` for a main-thread call, ``submit`` plus polling for a
background one).  Failures must become text, never reach the Qt slot.

Qt is real and offscreen; QGIS is only faked where ``initGui`` needs it.  The dock
is the one module that imports Qt at module level, so this whole module skips
rather than fails in the bare interpreter (``tests/test_package.py`` guards that).
"""

import json
import os
import re
import sys
import time
import types
from collections import Counter
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# The offscreen platform has no font database of its own: without one no glyph is
# painted, and the pixel probes below would be measuring blank widgets.
for _fonts in (r"C:\Windows\Fonts", "/usr/share/fonts"):
    if os.path.isdir(_fonts):
        os.environ.setdefault("QT_QPA_FONTDIR", _fonts)
        break

pytest.importorskip("qgis.PyQt.QtWidgets", reason="the operation dock needs Qt")

from qgis.PyQt.QtCore import QPoint, Qt
from qgis.PyQt.QtGui import QColor, QPalette
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFormLayout,
    QLineEdit,
    QMainWindow,
)

from qgis_agent_plugin import plugin
from qgis_agent_plugin.core.models import (
    ENGINE_CLOSING,
    INTERNAL_ERROR,
    INVALID_INPUT,
    PATH_NOT_ALLOWED,
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
from qgis_agent_plugin.ui import theme
from qgis_agent_plugin.ui.dock import _DESTRUCTIVE_ROLE, _META_ROLE, OperationDock, _Field

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


# What pydantic emits for a named enum and for a discriminated union: the property is
# a ``$ref``, and the enum or the branches sit behind the definition table.
REFERRED = {
    "type": "object",
    "properties": {
        "renderer": {"$ref": "#/$defs/RendererConfig"},
        "mode": {"$ref": "#/$defs/Mode"},
    },
    "required": ["renderer"],
    "$defs": {
        "Mode": {"enum": ["fast", "safe"], "title": "Mode", "type": "string"},
        "RendererConfig": {
            "discriminator": {"propertyName": "type"},
            "oneOf": [{"$ref": "#/$defs/PointRenderer"}, {"$ref": "#/$defs/LineRenderer"}],
        },
        "PointRenderer": {
            "properties": {"type": {"const": "point"}},
            "required": ["type"],
            "title": "PointRenderer",
            "type": "object",
        },
        "LineRenderer": {
            "properties": {"type": {"const": "line"}},
            "required": ["type"],
            "title": "LineRenderer",
            "type": "object",
        },
    },
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
    description: str = "a demo operation",
    destructive: bool = False,
) -> OperationSpec:
    return OperationSpec(
        name=name,
        description=description,
        kind=OperationKind.QUERY,
        strategy=strategy,
        destructive=destructive,
        input_schema=schema if schema is not None else {"type": "object", "properties": {}},
        output_schema={"type": "object"},
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


def _fields(form: QFormLayout) -> list[Any]:
    """The editor widgets the form shows right now, in row order."""

    return [form.itemAt(row, QFormLayout.FieldRole).widget() for row in range(form.rowCount())]


def _painted_contrast(widget: Any) -> float:
    """WCAG ratio between the glyphs a widget actually painted and its own background.

    The pixels, not the theme: Qt draws a styled ``QLineEdit`` placeholder at half
    strength no matter what the palette says, and a table of intended colours is
    exactly what let that pass review.
    """

    image = widget.grab().toImage()
    assert image.width() > 40 and image.height() > 12, (widget.size(), image.size())
    counts: Counter[str] = Counter()
    for y in range(4, max(5, image.height() - 4)):
        for x in range(6, max(7, image.width() - 6)):
            counts[image.pixelColor(x, y).name()] += 1
    background = QColor(counts.most_common(1)[0][0])
    base = theme.relative_luminance(background)
    strongest = background
    for name in counts:
        color = QColor(name)
        if abs(theme.relative_luminance(color) - base) > abs(
            theme.relative_luminance(strongest) - base
        ):
            strongest = color
    return theme.contrast(strongest, background)


def _light_palette() -> QPalette:
    palette = QPalette()
    for role, color in {
        QPalette.Window: "#efefef",
        QPalette.Base: "#ffffff",
        QPalette.WindowText: "#1a1a1a",
        QPalette.Text: "#1a1a1a",
        QPalette.Button: "#efefef",
        QPalette.ButtonText: "#1a1a1a",
        QPalette.Highlight: "#308cc6",
        QPalette.HighlightedText: "#ffffff",
    }.items():
        palette.setColor(role, QColor(color))
    return palette


def _dark_palette() -> QPalette:
    palette = QPalette()
    for role, color in {
        QPalette.Window: "#2b2b2b",
        QPalette.Base: "#1e1e1e",
        QPalette.WindowText: "#e8e8e8",
        QPalette.Text: "#e8e8e8",
        QPalette.Button: "#2b2b2b",
        QPalette.ButtonText: "#e8e8e8",
        QPalette.Highlight: "#4a9eff",
        QPalette.HighlightedText: "#101010",
    }.items():
        palette.setColor(role, QColor(color))
    return palette


# --- the selector ------------------------------------------------------------


def test_the_selector_is_sorted_and_carries_kind_and_strategy(qapp: Any) -> None:
    registry = FakeRegistry(_spec("layer.sum", ExecutionStrategy.BACKGROUND), _spec("layer.list"))

    dock = OperationDock(registry, FakeEngine())

    names = [dock.operations.itemText(row) for row in range(dock.operations.count())]
    assert names == ["layer.list", "layer.sum"]
    assert dock.operations.itemData(1, _META_ROLE) == "query · background"
    assert dock.operations.currentText() == "layer.list"


def test_a_destructive_operation_is_marked_in_the_selector(qapp: Any) -> None:
    dock = _dock(FakeEngine(), _spec("project.create", destructive=True))

    assert dock.operations.itemData(0, _DESTRUCTIVE_ROLE) is True
    assert "可破坏" in dock.meta.text()
    assert dock.meta.styleSheet() != ""


def test_the_summary_shows_the_description_of_the_selection(qapp: Any) -> None:
    dock = _dock(FakeEngine(), _spec(description="Read one layer's identity"))

    assert dock.meta.text() == "query · main_thread"
    assert dock.summary.text() == "Read one layer's identity"
    assert not dock.details.isHidden()


def test_every_registered_operation_gets_a_form_from_its_own_schema(qapp: Any) -> None:
    """The schemas actually registered: enums, bools and nullable unions among them."""

    registry = OperationRegistry()
    register_all(registry)
    dock = OperationDock(registry, FakeEngine())

    for row, spec in enumerate(registry.list_specs()):
        dock.operations.setCurrentIndex(row)
        properties = spec.input_schema["properties"]
        assert set(dock.editors) == set(properties)
        # No parameters means no parameter block, not a one-line apology row.
        assert dock.form.rowCount() == len(properties)
        assert dock.params.isHidden() is not bool(properties)
        # The shapes the fixtures only hand-copy: an inline enum, a free object, and a
        # union behind a ``$ref``.
        for name, expected in {"kind": "enum", "parameters": "json", "renderer": "json"}.items():
            if name in dock.editors:
                assert dock.editors[name][0] == expected, f"{spec.name}.{name}"


def test_the_dock_is_something_a_real_main_window_accepts(qapp: Any) -> None:
    """The F10 r1 finding: a plain ``QWidget`` is refused by ``addDockWidget``."""

    window = QMainWindow()
    dock = _dock(FakeEngine())

    window.addDockWidget(Qt.RightDockWidgetArea, dock)  # the real sip type check
    assert window.dockWidgetArea(dock) == Qt.RightDockWidgetArea

    window.removeDockWidget(dock)
    assert window.dockWidgetArea(dock) == Qt.NoDockWidgetArea


# --- the form ----------------------------------------------------------------


def test_a_property_becomes_the_editor_its_type_asks_for(qapp: Any) -> None:
    """Bools keep their state; every field that can be empty shows its default."""

    dock = _dock(FakeEngine(), _spec(schema=FORM))
    editors = {name: widget for name, (_, widget) in dock.editors.items()}

    assert set(editors) == {"name", "flag", "count", "ratio", "kind", "note", "items"}
    assert isinstance(editors["flag"], QCheckBox)
    assert editors["flag"].isChecked()
    assert isinstance(editors["count"], QLineEdit)
    assert editors["count"].hint == "3"
    assert isinstance(editors["ratio"], QLineEdit)
    assert editors["ratio"].hint == "0.5"
    assert isinstance(editors["kind"], QComboBox)
    assert [editors["kind"].itemText(row) for row in range(editors["kind"].count())] == [
        "默认（raster）",
        "vector",
        "raster",
    ]
    assert editors["kind"].currentIndex() == 0
    assert isinstance(editors["name"], QLineEdit)
    # A required field with no default says so once, in its label -- not twice.
    assert editors["name"].hint == ""
    assert isinstance(editors["items"], QLineEdit)
    assert editors["items"].hint == "[1, 2]"
    # The field labels are the dock's words where it has them, and are marked when required.
    assert "必填" in dock.form.labelForField(editors["name"]).text()
    assert "必填" in dock.form.labelForField(editors["kind"]).text()
    assert "必填" not in dock.form.labelForField(editors["note"]).text()


def test_a_checkbox_has_no_blank_state_to_promise(qapp: Any) -> None:
    """A checkbox's default is its initial state, not a value left empty for it."""

    registry = OperationRegistry()
    register_all(registry)
    real = OperationDock(registry, FakeEngine())
    assert real.operations.currentText() == "layer.add"
    fixture = _dock(FakeEngine(), _spec(schema=FORM))

    assert "留空" not in real.editors["add_to_legend"][1].toolTip()
    assert "留空" not in fixture.editors["flag"][1].toolTip()


def test_a_schema_behind_a_ref_keeps_its_union_and_its_enum(qapp: Any) -> None:
    """``layer.set_renderer`` publishes its renderer as a ``$ref``, not as a ``oneOf``."""

    engine = FakeEngine(run_sync=_record())
    dock = _dock(engine, _spec(schema=REFERRED))

    # The union is one definition away, so the property is a JSON field, not text.
    assert dock.editors["renderer"][0] == "json"
    mode = dock.editors["mode"][1]
    assert isinstance(mode, QComboBox)
    assert [mode.itemText(row) for row in range(mode.count())] == ["fast", "safe"]

    dock.editors["renderer"][1].setText('{"type": "line"}')
    mode.setCurrentIndex(1)
    dock.run()

    assert engine.calls == [("run_sync", "demo.op", {"renderer": {"type": "line"}, "mode": "safe"})]


def test_an_empty_field_is_left_to_the_model_default(qapp: Any) -> None:
    engine = FakeEngine(run_sync=_record())
    dock = _dock(engine, _spec(schema=FORM))

    dock.run()

    # Only the checkbox submits without being asked: it has no empty state.
    assert engine.calls == [("run_sync", "demo.op", {"flag": True})]


def test_a_filled_field_is_submitted_in_its_own_type(qapp: Any) -> None:
    engine = FakeEngine(run_sync=_record())
    dock = _dock(engine, _spec(schema=FORM))

    dock.editors["name"][1].setText("roads")
    dock.editors["count"][1].setText("7")
    dock.editors["ratio"][1].setText("0.25")
    dock.editors["kind"][1].setCurrentIndex(2)
    dock.run()

    assert engine.calls == [
        (
            "run_sync",
            "demo.op",
            {"name": "roads", "flag": True, "count": 7, "ratio": 0.25, "kind": "raster"},
        )
    ]


def test_a_field_that_is_not_json_stops_before_the_engine(qapp: Any) -> None:
    engine = FakeEngine(run_sync=_record())
    dock = _dock(engine, _spec(schema=FORM))

    dock.editors["items"][1].setText("{not json")
    dock.run()

    assert engine.calls == []
    assert dock.error_code.text() == INVALID_INPUT
    assert "items" in dock.error_message.text()
    assert dock.capsule.text() == theme.STATE_LABELS["failed"]


def test_switching_operation_rebuilds_the_whole_form(qapp: Any) -> None:
    """A switch replaces the rows and wipes what belonged to the previous operation."""

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
    dock.result.setPlainText("{}")
    dock._show_problem(INTERNAL_ERROR, "boom")

    dock.operations.setCurrentIndex(1)

    assert set(dock.editors) == {"gamma"}
    assert _fields(dock.form) == [dock.editors["gamma"][1]]
    assert dock.result.toPlainText() == ""
    assert dock.result_summary.isHidden()
    assert dock.feedback.isHidden()
    assert dock.state == "idle"

    dock.operations.setCurrentIndex(0)

    assert _fields(dock.form) == [dock.editors["alpha"][1], dock.editors["beta"][1]]


def test_switching_to_a_smaller_form_shrinks_the_parameter_block(qapp: Any) -> None:
    """The block is as tall as the current operation's rows, not the last one's.

    The path matters: open on ``layer.add`` (five rows), let the dock lay out, then
    switch to a two-row operation.  Picking the small operation before the first
    layout hides the bug, because the first size hint is already the right one.
    """

    registry = OperationRegistry()
    register_all(registry)
    dock = OperationDock(registry, FakeEngine())
    # Big enough for the five rows, so the first layout is not squeezed by the window.
    dock.resize(420, 760)
    dock.show()
    QTest.qWait(80)

    assert dock.operations.currentText() == "layer.add"
    tall = dock.form_host.sizeHint().height()
    assert dock.params.height() == tall, "the first layout fits the five rows"
    assert dock.params.minimumHeight() < tall, "the block shrinks and scrolls at the floor"
    run_y = dock.run_button.mapTo(dock.widget(), QPoint(0, 0)).y()

    row = [dock.operations.itemText(i) for i in range(dock.operations.count())].index(
        "layer.sum_numeric_field"
    )
    dock.operations.setCurrentIndex(row)
    QTest.qWait(80)

    short = dock.form_host.sizeHint().height()
    assert short < tall
    assert dock.params.height() == short, "the block kept the previous operation's height"
    assert dock.run_button.mapTo(dock.widget(), QPoint(0, 0)).y() < run_y

    dock.close()


# --- the six states ----------------------------------------------------------


def test_the_idle_state_offers_a_run_and_nothing_to_cancel(qapp: Any) -> None:
    dock = _dock(FakeEngine(), _spec(schema=FORM))

    assert dock.capsule.text() == "就绪"
    assert dock.run_button.isEnabled()
    assert not dock.cancel_button.isEnabled()
    assert not dock.copy_button.isEnabled()
    assert dock.progress.isHidden()
    assert dock.feedback.isHidden()
    # No record, nothing to measure: the capsule alone names the state.
    assert dock.result_summary.isHidden()
    # Every control says what it is, and the state says what it means.
    for control in (dock.operations, dock.run_button, dock.cancel_button, dock.copy_button):
        assert control.accessibleName().strip()
        assert control.toolTip().strip()
    assert "就绪" in dock.capsule.accessibleDescription()


def test_a_running_background_call_locks_the_form_and_offers_cancel(qapp: Any) -> None:
    engine = FakeEngine(
        submit=_record(ExecutionStatus.QUEUED, ExecutionStrategy.BACKGROUND),
        get=_record(ExecutionStatus.RUNNING, ExecutionStrategy.BACKGROUND, progress=0),
    )
    dock = _dock(engine, _spec(strategy=ExecutionStrategy.BACKGROUND, schema=FORM))

    dock.run()

    assert dock.capsule.text() == "运行中"
    assert not dock.operations.isEnabled()
    assert not dock.editors["name"][1].isEnabled()
    assert not dock.run_button.isEnabled()
    assert dock.cancel_button.isEnabled()
    assert not dock.progress.isHidden()
    # No number yet, so the bar is indeterminate rather than frozen at zero.
    assert (dock.progress.minimum(), dock.progress.maximum()) == (0, 0)
    assert dock.result_summary.isHidden()

    dock.shutdown()


def test_the_bar_becomes_real_progress_when_the_runner_reports_it(qapp: Any) -> None:
    engine = FakeEngine(
        submit=_record(ExecutionStatus.QUEUED, ExecutionStrategy.BACKGROUND),
        get=_record(ExecutionStatus.RUNNING, ExecutionStrategy.BACKGROUND, progress=40),
    )
    dock = _dock(engine, _spec(strategy=ExecutionStrategy.BACKGROUND))

    dock.run()

    assert _wait_until(lambda: dock.progress.maximum() == 100)
    assert dock.progress.value() == 40
    assert dock.result_summary.text() == "进度 40%"
    assert "运行中" not in dock.result_summary.text()

    dock.shutdown()


def test_a_successful_call_ends_ok_with_copyable_json(qapp: Any) -> None:
    engine = FakeEngine(run_sync=_record(result={"layer_count": 0}))
    dock = _dock(engine)

    dock.run()

    assert dock.capsule.text() == "完成"
    assert dock.progress.isHidden()
    assert dock.feedback.isHidden()
    assert dock.copy_button.isEnabled()
    assert dock.run_button.isEnabled()
    # The summary carries what the capsule cannot: how long it took.
    assert dock.result_summary.text() == "耗时 1.0 s"
    assert "完成" not in dock.result_summary.text()
    shown = json.loads(dock.result.toPlainText())
    assert shown["status"] == "SUCCEEDED"
    assert shown["result"] == {"layer_count": 0}


def test_a_failed_record_shows_its_code_a_reason_and_what_to_do(qapp: Any) -> None:
    engine = FakeEngine(
        run_sync=_record(
            ExecutionStatus.FAILED,
            error=ErrorDetail(code=PATH_NOT_ALLOWED, message="outside the allowed roots"),
        )
    )
    dock = _dock(engine)

    dock.run()

    assert dock.capsule.text() == "失败"
    assert not dock.feedback.isHidden()
    assert dock.error_code.text() == PATH_NOT_ALLOWED
    assert dock.error_message.text() == "这个路径不在允许的目录里。"
    # The engine's own message is kept, but only as the tooltip.
    assert "outside the allowed roots" in dock.error_message.toolTip()
    assert "allowed_data_roots" in dock.error_hint.text()
    assert PATH_NOT_ALLOWED in dock.feedback.accessibleDescription()
    assert dock.result_summary.text() == "耗时 1.0 s"
    assert "失败" not in dock.result_summary.text()
    # The record itself is still there to copy.
    assert json.loads(dock.result.toPlainText())["status"] == "FAILED"


def test_a_cancelled_call_ends_cancelled(qapp: Any) -> None:
    engine = FakeEngine(
        submit=_record(ExecutionStatus.QUEUED, ExecutionStrategy.BACKGROUND),
        get=_record(ExecutionStatus.CANCELLED, ExecutionStrategy.BACKGROUND),
    )
    dock = _dock(engine, _spec(strategy=ExecutionStrategy.BACKGROUND))

    dock.run()

    assert _wait_until(lambda: dock.capsule.text() == "已取消")
    assert dock.progress.isHidden()
    assert dock.run_button.isEnabled()
    assert not dock.cancel_button.isEnabled()


def test_an_empty_registry_teaches_instead_of_showing_nothing(qapp: Any) -> None:
    dock = OperationDock(FakeRegistry(), FakeEngine())

    assert dock.capsule.text() == "无操作"
    assert dock.operations.count() == 0
    assert dock.operations.placeholderText() == "没有可用的操作"
    assert not dock.operations.isEnabled()
    assert not dock.empty_state.isHidden()
    assert dock.empty_state.findChild(type(dock.summary), "emptyTitle").text() == "还没有注册的操作"
    assert dock.result.isHidden()
    assert dock.result_summary.isHidden()
    assert not dock.run_button.isEnabled()
    assert not dock.cancel_button.isEnabled()
    assert not dock.copy_button.isEnabled()


# --- the keyboard ------------------------------------------------------------


def test_ctrl_return_runs_the_selected_operation(qapp: Any) -> None:
    engine = FakeEngine(run_sync=_record())
    dock = _dock(engine)

    dock.show()
    dock.activateWindow()
    dock.run_button.setFocus()
    QTest.qWait(30)

    QTest.keyClick(dock, Qt.Key_Return, Qt.ControlModifier)

    assert engine.calls == [("run_sync", "demo.op", {})]
    dock.close()


def test_the_failed_state_survives_the_dock_minimum(qapp: Any) -> None:
    """At its own minimum the dock keeps every banner line and the record inside the body.

    A layout's minimum counts a wrapped label as one line; the banner is asked for
    its height at the current width, so the floor is the real one.
    """

    engine = FakeEngine(
        run_sync=_record(
            ExecutionStatus.FAILED,
            error=ErrorDetail(code=PATH_NOT_ALLOWED, message="outside the allowed roots"),
        )
    )
    registry = OperationRegistry()
    register_all(registry)
    dock = OperationDock(registry, engine)
    window = QMainWindow()
    window.addDockWidget(Qt.RightDockWidgetArea, dock)
    window.resize(351, 900)
    window.show()
    window.resizeDocks([dock], [351], Qt.Horizontal)
    QTest.qWait(120)

    dock.editors["source_path"][1].setText("D:/temp/roads.gpkg")
    dock.editors["name"][1].setText("roads")
    dock.run()
    QTest.qWait(150)

    floor = dock.minimumSizeHint().height()
    window.resize(351, floor)
    QTest.qWait(250)

    body = dock.widget()
    hint = dock.error_hint
    bottom = dock.result.mapTo(body, QPoint(0, 0)).y() + dock.result.height()
    assert dock.feedback.height() >= dock.feedback.heightForWidth(dock.feedback.width())
    assert hint.height() >= hint.heightForWidth(hint.width()), "the hint lost its last line"
    assert dock.result.height() >= dock.result.minimumHeight()
    assert bottom + 16 <= body.height(), "the record was pushed out of the body"
    assert dock.params.height() < dock.form_host.sizeHint().height()
    window.close()


def test_a_failure_without_a_record_does_not_show_the_idle_hint(qapp: Any) -> None:
    """The record area says what happened: a failed submit produced no record."""

    engine = FakeEngine(run_sync=FrameworkError(ENGINE_CLOSING, "the engine is shutting down"))
    dock = OperationDock(FakeRegistry(_spec("a.op"), _spec("b.op")), engine)

    assert dock.result.hint.text().startswith("运行一次后")

    dock.run()

    assert dock.result.toPlainText() == ""
    assert "没有产生执行记录" in dock.result.hint.text()
    assert not dock.result.hint.isHidden()

    dock.operations.setCurrentIndex(1)

    assert dock.result.hint.text().startswith("运行一次后")


def test_a_run_hands_the_keyboard_back(qapp: Any) -> None:
    """Disabling the form during a call must not leave focus parked on the record view."""

    dock = _dock(FakeEngine(run_sync=_record()))
    dock.show()
    QTest.qWait(40)
    assert QApplication.focusWidget() is dock.operations

    dock.run()
    QTest.qWait(40)

    assert QApplication.focusWidget() is dock.operations
    dock.close()


def test_the_tab_order_goes_selector_then_fields_then_run(qapp: Any) -> None:
    dock = _dock(FakeEngine(), _spec(schema=FORM))

    chain: list[Any] = []
    widget = dock
    for _ in range(200):
        widget = widget.nextInFocusChain()
        if widget is dock:
            break
        chain.append(widget)

    steps = [chain.index(dock.operations)]
    steps += [chain.index(editor) for _, editor in dock.editors.values()]
    steps.append(chain.index(dock.run_button))
    assert steps == sorted(steps)
    assert len(set(steps)) == len(steps)


# --- the theme ---------------------------------------------------------------


def test_every_text_pair_clears_its_contrast_floor(qapp: Any) -> None:
    """Light and dark both: body and secondary text 4.5:1, capsules 3:1."""

    for factory in (_light_palette, _dark_palette):
        look = theme.build(factory())
        for name, (foreground, background, floor) in look.pairs().items():
            ratio = theme.contrast(foreground, background)
            assert ratio >= floor, f"{name}: {ratio:.2f} < {floor}"


def test_the_dock_follows_the_application_palette(qapp: Any) -> None:
    original = QApplication.palette()
    try:
        QApplication.setPalette(_light_palette())
        light = OperationDock(FakeRegistry(_spec()), FakeEngine())
        QApplication.setPalette(_dark_palette())
        dark = OperationDock(FakeRegistry(_spec()), FakeEngine())
    finally:
        QApplication.setPalette(original)

    assert light.look.surface.name() != dark.look.surface.name()
    assert light.look.text.name() != dark.look.text.name()
    assert light.look.capsule("ok") != dark.look.capsule("ok")
    # Each dock painted itself with its own palette.
    assert light.look.surface.name() in light.styleSheet()
    assert dark.look.surface.name() in dark.styleSheet()


def test_no_module_but_the_theme_names_a_colour(qapp: Any) -> None:
    """The dock's colour comes from the palette: only ``theme.py`` may hold a literal."""

    ui_dir = os.path.dirname(theme.__file__)
    for name in sorted(os.listdir(ui_dir)):
        if not name.endswith(".py") or name == "theme.py":
            continue
        with open(os.path.join(ui_dir, name), encoding="utf-8") as handle:
            assert re.search(r"#[0-9a-fA-F]{3,8}", handle.read()) is None, name


def test_the_painted_hint_clears_the_contrast_floor(qapp: Any) -> None:
    """The default value in an empty field is text, so it must read as text.

    Measured on the pixels the widget paints, in both palettes: the previous
    guard asserted a palette role Qt never used, so it stayed green while the
    hint rendered at 3.3:1.
    """

    original = QApplication.palette()
    try:
        for palette in (_light_palette(), _dark_palette()):
            QApplication.setPalette(palette)
            dock = OperationDock(FakeRegistry(_spec(schema=FORM)), FakeEngine())
            dock.show()
            QTest.qWait(60)
            assert _painted_contrast(dock.editors["count"][1]) >= theme.BODY
            assert _painted_contrast(dock.result) >= theme.BODY
            dock.close()
    finally:
        QApplication.setPalette(original)


def test_the_hint_probe_notices_a_weak_hint(qapp: Any) -> None:
    """The guard can fail: a hint at a third strength measures below the floor."""

    palette = QApplication.palette()
    field = _Field()
    field.resize(180, 30)
    weak = theme.mix(palette.color(QPalette.Base), palette.color(QPalette.Text), 0.35)
    field.set_hint("EPSG:4326", weak)
    field.show()
    QTest.qWait(40)

    assert _painted_contrast(field) < theme.BODY
    field.close()


def test_a_submitted_value_stays_readable_while_it_runs(qapp: Any) -> None:
    """Disabling the form stops edits, not reading: the value being run stays legible."""

    engine = FakeEngine(
        submit=_record(ExecutionStatus.QUEUED, ExecutionStrategy.BACKGROUND),
        get=_record(ExecutionStatus.RUNNING, ExecutionStrategy.BACKGROUND, progress=0),
    )
    dock = OperationDock(
        FakeRegistry(_spec(strategy=ExecutionStrategy.BACKGROUND, schema=FORM)), engine
    )
    dock.show()
    dock.editors["name"][1].setText("roads")
    dock.run()
    QTest.qWait(60)

    field = dock.editors["name"][1]
    assert not field.isEnabled()
    assert _painted_contrast(field) >= theme.BODY

    dock.shutdown()
    dock.close()


# --- running, polling and cancel ---------------------------------------------


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
    assert dock.error_code.text() == INTERNAL_ERROR
    assert "no runner" in dock.error_message.toolTip()


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
    assert "accepted=True" in dock.result_summary.text()
    assert _wait_until(lambda: not dock.timer.isActive())
    assert json.loads(dock.result.toPlainText())["status"] == "CANCELLED"


def test_copy_json_puts_the_record_on_the_clipboard(qapp: Any) -> None:
    dock = _dock(FakeEngine(run_sync=_record(result={"layer_count": 0})))
    dock.run()

    dock.copy_button.click()

    assert QApplication.clipboard().text() == dock.result.toPlainText()
    assert dock.copy_button.text() == "已复制"


# --- failures never reach the slot -------------------------------------------


def test_a_refused_call_is_shown_in_the_dock_not_raised(qapp: Any) -> None:
    engine = FakeEngine(run_sync=FrameworkError(ENGINE_CLOSING, "the engine is shutting down"))
    dock = _dock(engine)

    dock.run()

    assert dock.capsule.text() == "失败"
    assert dock.error_code.text() == ENGINE_CLOSING
    assert dock.error_message.text() == "插件正在关闭，调用没有执行。"
    assert "shutting down" in dock.error_message.toolTip()
    assert "重新启用" in dock.error_hint.text()
    assert dock.result.toPlainText() == ""
    assert dock.result_summary.isHidden()
    assert dock.run_button.isEnabled()
    assert not dock.copy_button.isEnabled()


def test_an_unexpected_failure_is_shown_not_raised(qapp: Any) -> None:
    engine = FakeEngine(run_sync=RuntimeError("boom"))
    dock = _dock(engine)

    dock.run()

    assert dock.error_code.text() == INTERNAL_ERROR
    assert dock.error_message.text() == "界面遇到意外错误，调用没有完成。"
    assert "RuntimeError" in dock.error_message.toolTip()
    assert "boom" in dock.error_message.toolTip()
    assert dock.run_button.isEnabled()


def test_an_invalid_input_names_the_field_where_it_can_be_read(qapp: Any) -> None:
    """The schema cannot carry the models' own validators, so the engine's message is
    the only thing that names the field that failed; it has to reach the panel."""

    engine = FakeEngine(
        run_sync=FrameworkError(INVALID_INPUT, "demo.op: invalid input at source_path, name")
    )
    dock = _dock(engine)

    dock.run()

    assert "source_path, name" in dock.error_message.text() + dock.error_hint.text()
    assert "source_path, name" in dock.feedback.accessibleDescription()


def test_an_error_without_a_sentence_falls_back_to_the_engine_text(qapp: Any) -> None:
    """A code this build has no words for shows what the engine said, marked as such."""

    dock = _dock(FakeEngine())
    dock._show_engine_error("FUTURE_CODE", "something new happened")

    assert dock.error_code.text() == "FUTURE_CODE"
    assert "引擎返回" in dock.error_message.text()
    assert "something new happened" in dock.error_message.text()


def test_a_failure_while_polling_ends_the_poll_and_is_shown(qapp: Any) -> None:
    engine = FakeEngine(
        submit=_record(ExecutionStatus.QUEUED, ExecutionStrategy.BACKGROUND),
        get=FrameworkError(ENGINE_CLOSING, "the engine is shutting down"),
    )
    dock = _dock(engine, _spec(strategy=ExecutionStrategy.BACKGROUND))

    dock.run()

    assert _wait_until(lambda: dock.error_code.text() == ENGINE_CLOSING)
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
    # The faked package's surface: the real Qt face and the faked core.
    qgis.__dict__.update(core=core, PyQt=sys.modules["qgis.PyQt"])
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
        assert dock.operations.count() == 17
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
