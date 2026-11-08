"""The operation dock: pick a registered operation, fill its parameters, read the record.

A manual test tool rather than a framework part.  It reads the specs the registry
already publishes, builds one editor per ``input_schema`` property, and calls the
two engine methods the Python Console would call -- ``run_sync`` for a main-thread
operation, ``submit`` plus polling for a background one.  What it shows is the
``ExecutionRecord`` as JSON, so an operator checks an operation against the very
values a caller receives.

Qt is imported at module level here (``plugin`` and the runners carry the same
permission); QGIS is not imported at all, because the dock only knows the registry,
the engine and Qt, and the first two are QGIS-free.
"""

from __future__ import annotations

import json
from typing import Any

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDockWidget,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..core.execution_store import TERMINAL_STATUSES
from ..core.models import ExecutionRecord, ExecutionStrategy, FrameworkError, OperationSpec

# How often a submitted call is asked for its record (F10: about five times a second).
POLL_MS = 200

# What the two spin boxes offer: wide enough for any count or coordinate, small
# enough for ``QSpinBox`` (which takes at most a 32-bit int).
_INTEGER = 2**31 - 1
_NUMBER = 1e12

# How a schema ``type`` becomes an editor; anything not listed is a single-line text.
_KINDS = {
    "boolean": "bool",
    "integer": "integer",
    "number": "number",
    "array": "json",
    "object": "json",
}


class OperationDock(QDockWidget):
    """One list of operations, one form per selection, one record on screen.

    A ``QDockWidget`` because that is what ``QMainWindow.addDockWidget`` and
    ``QgisInterface.addDockWidget`` take; everything the dock shows lives inside the
    one widget it carries.
    """

    def __init__(self, registry: Any, engine: Any) -> None:
        super().__init__()
        self.setObjectName("QGISAgentOperationDock")
        self.setWindowTitle("QGIS Agent 操作")
        self._registry = registry
        self._engine = engine
        self._specs: dict[str, OperationSpec] = {}
        # Field name to ``(kind, editor)``, rebuilt with every form.
        self.editors: dict[str, tuple[str, Any]] = {}
        # The live background call, or None: what the cancel button acts on.
        self._execution_id: str | None = None
        self._busy = False
        self.timer = QTimer(self)
        self.timer.setInterval(POLL_MS)
        self.timer.timeout.connect(self._poll)
        self._build()
        self._fill_operations()

    # -- the surface ---------------------------------------------------------

    def _build(self) -> None:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.addWidget(QLabel("操作"))
        self.operations = QListWidget()
        self.operations.currentItemChanged.connect(self._selection_changed)
        layout.addWidget(self.operations)
        self.empty = QLabel("没有注册的操作")
        self.empty.setVisible(False)
        layout.addWidget(self.empty)
        self.form = QFormLayout()
        layout.addLayout(self.form)
        self.error = QLabel("")
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #b00020;")
        layout.addWidget(self.error)
        buttons = QHBoxLayout()
        self.run_button = QPushButton("运行")
        self.run_button.clicked.connect(self.run)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self.cancel)
        buttons.addWidget(self.run_button)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.result = QPlainTextEdit()
        self.result.setReadOnly(True)
        layout.addWidget(self.result, 1)
        self.setWidget(body)
        self._refresh_buttons()

    def _fill_operations(self) -> None:
        specs = sorted(self._registry.list_specs(), key=lambda spec: spec.name)
        self._specs = {spec.name: spec for spec in specs}
        for spec in specs:
            item = QListWidgetItem(f"{spec.name}  [{spec.kind.value} · {spec.strategy.value}]")
            item.setData(Qt.UserRole, spec.name)
            self.operations.addItem(item)
        self.empty.setVisible(not specs)
        if specs:
            self.operations.setCurrentRow(0)

    def _selection_changed(self, current: Any, previous: Any) -> None:
        self._build_form(self._selected_spec())

    def _selected_spec(self) -> OperationSpec | None:
        item = self.operations.currentItem()
        return self._specs.get(item.data(Qt.UserRole)) if item is not None else None

    def _build_form(self, spec: OperationSpec | None) -> None:
        """Replace the editors with the selected operation's, one row per property."""

        # removeRow deletes the row's widgets too, so a switch cannot leave either a
        # stale editor or an empty row behind.
        while self.form.rowCount():
            self.form.removeRow(0)
        self.editors = {}
        # A field error belongs to the operation that produced it, not to this one.
        self.error.setText("")
        if spec is None:
            return
        properties = spec.input_schema.get("properties") or {}
        required = set(spec.input_schema.get("required") or ())
        for name, schema in properties.items():
            kind = _kind(schema)
            editor = _editor(kind, schema)
            self.editors[name] = (kind, editor)
            title = schema.get("title") or name
            self.form.addRow(f"{title} *" if name in required else title, editor)
        if not properties:
            self.form.addRow(QLabel("这个操作不接受参数"))

    # -- running -------------------------------------------------------------

    def run(self) -> None:
        """Send the form to the engine the way its strategy requires."""

        spec = self._selected_spec()
        if spec is None:
            return
        arguments = self._arguments()
        if arguments is None:
            return  # the JSON editor reported itself; the engine is not called
        self.error.setText("")
        if spec.strategy is ExecutionStrategy.MAIN_THREAD:
            self._run_sync(spec, arguments)
        else:
            self._submit(spec, arguments)

    def _run_sync(self, spec: OperationSpec, arguments: dict[str, Any]) -> None:
        self._busy = True
        self._refresh_buttons()
        try:
            record = self._engine.run_sync(spec.name, arguments)
        except Exception as error:  # noqa: BLE001 - a Qt slot may not raise
            self._show_failure(error)
            return
        finally:
            self._busy = False
            self._refresh_buttons()
        self._show_record(record)

    def _submit(self, spec: OperationSpec, arguments: dict[str, Any]) -> None:
        try:
            record = self._engine.submit(spec.name, arguments)
        except Exception as error:  # noqa: BLE001 - a Qt slot may not raise
            self._show_failure(error)
            return
        # A submit answers with a terminal record when it was refused outright.
        self._execution_id = None if record.status in TERMINAL_STATUSES else record.execution_id
        self._show_record(record)
        if self._execution_id is not None:
            self.timer.start()
        self._refresh_buttons()

    def _poll(self) -> None:
        """Ask the engine for the live record; stop at the first terminal one."""

        execution_id = self._execution_id
        if execution_id is None:
            self.timer.stop()
            return
        try:
            record = self._engine.get(execution_id)
        except Exception as error:  # noqa: BLE001 - a Qt slot may not raise
            self._end_polling()
            self._show_failure(error)
            return
        self._show_record(record)
        if record.status in TERMINAL_STATUSES:
            self._end_polling()

    def cancel(self) -> None:
        """Ask for a stop and show the receipt the engine answered with."""

        execution_id = self._execution_id
        if execution_id is None:
            return
        try:
            receipt = self._engine.cancel(execution_id)
        except Exception as error:  # noqa: BLE001 - a Qt slot may not raise
            self._show_failure(error)
            return
        self.status.setText(
            f"取消回执：accepted={receipt.accepted}，status={receipt.status.value}，"
            f"reason={receipt.reason or '-'}"
        )

    def shutdown(self) -> None:
        """Stop polling and forget the run, so a removed dock leaves nothing live."""

        self._busy = False
        self._end_polling()

    # -- what is shown --------------------------------------------------------

    def _arguments(self) -> dict[str, Any] | None:
        """The form as arguments, or None when a JSON field does not parse.

        A field left empty is not submitted, so the model's default decides; the
        exception is a checkbox, whose unchecked state is a real answer rather than
        a missing one.
        """

        arguments: dict[str, Any] = {}
        for name, (kind, editor) in self.editors.items():
            if kind == "bool":
                arguments[name] = editor.isChecked()
            elif kind in {"integer", "number"}:
                arguments[name] = editor.value()
            elif kind == "enum":
                arguments[name] = editor.currentText()
            else:
                text = editor.text().strip()
                if not text:
                    continue
                if kind == "json":
                    try:
                        arguments[name] = json.loads(text)
                    except ValueError:
                        self.error.setText(f"{name}：不是合法的 JSON")
                        return None
                else:
                    arguments[name] = text
        return arguments

    def _show_record(self, record: ExecutionRecord) -> None:
        self.result.setPlainText(
            json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2)
        )
        self.status.setText(f"{record.status.value} · {record.progress:.0f}%")
        error = record.error
        self.error.setText(f"{error.code}: {error.message}" if error is not None else "")

    def _show_failure(self, error: Exception) -> None:
        """Show what the engine refused with; never let it reach the Qt slot."""

        if isinstance(error, FrameworkError):
            self.status.setText("调用被拒绝")
            self.error.setText(f"{error.code}: {error.message}")
            return
        self.status.setText("意外错误")
        self.error.setText(f"{type(error).__name__}: {error}")

    def _end_polling(self) -> None:
        self.timer.stop()
        self._execution_id = None
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        # One live background call at a time: while it runs, the cancel button is the
        # only way forward, so a second run cannot silently take over its record.
        self.run_button.setEnabled(not self._busy and self._execution_id is None)
        self.cancel_button.setEnabled(not self._busy and self._execution_id is not None)


def _kind(schema: dict[str, Any]) -> str:
    """The editor kind for one property, looking through a nullable ``anyOf``."""

    for option in (schema, *(schema.get("anyOf") or ())):
        if "enum" in option:
            return "enum"
        kind = _KINDS.get(str(option.get("type")))
        if kind is not None:
            return kind
    return "text"


def _editor(kind: str, schema: dict[str, Any]) -> Any:
    """One editor for a field, starting at the schema's default."""

    default = schema.get("default")
    if kind == "bool":
        editor = QCheckBox()
        editor.setChecked(bool(default))
        return editor
    if kind == "integer":
        editor = QSpinBox()
        editor.setRange(-_INTEGER, _INTEGER)
        editor.setValue(int(default or 0))
        return editor
    if kind == "number":
        editor = QDoubleSpinBox()
        editor.setRange(-_NUMBER, _NUMBER)
        editor.setDecimals(6)
        editor.setValue(float(default or 0))
        return editor
    if kind == "enum":
        editor = QComboBox()
        editor.addItems([str(value) for value in _enum_values(schema)])
        if default is not None:
            editor.setCurrentText(str(default))
        return editor
    editor = QLineEdit()
    if default is not None:
        editor.setText(default if isinstance(default, str) else json.dumps(default))
    return editor


def _enum_values(schema: dict[str, Any]) -> list[Any]:
    for option in (schema, *(schema.get("anyOf") or ())):
        if "enum" in option:
            return list(option["enum"])
    return []
