"""动作调试页:选一条命令、填它的参数、运行,并把插件的应答原样显示出来。

页面拼出的是 socket 客户端会发的那封信封,再交给插件自己的处理管道,所以这里
看到的结果就是客户端会拿到的结果——参数校验、错误码、操作快照一应俱全。这里不
认识任何具体命令:参数从模型自己的 JSON schema 起步,执行方式取自声明里列出的
那几种。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QFontDatabase, QPalette
from qgis.PyQt.QtWidgets import (
    QApplication,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .qgis_actions import COMMAND_NAMES, REGISTRY

# The JSON value a field with no default starts as, per schema type.
_EMPTY: dict[str, Any] = {
    "string": "",
    "integer": 0,
    "number": 0,
    "boolean": False,
    "array": [],
    "object": {},
}

# What the wire calls an execution mode, as this page says it.
_MODE_LABELS = {"sync": "同步", "async": "异步"}


def params_template(action: Any) -> dict[str, Any]:
    """The params of ``action`` as a starting point: one key per declared field.

    An optional field keeps the default it declared; a required one gets a
    placeholder of its type, so an empty string or a zero is a value the caller
    has to replace rather than a silent default.
    """

    template = {}
    for name, spec in action.params_model.model_json_schema().get("properties", {}).items():
        template[name] = spec["default"] if "default" in spec else _placeholder(spec)
    return template


def _placeholder(spec: dict[str, Any]) -> Any:
    """One value for a field the schema gives no default for."""

    if "anyOf" in spec:  # a union: the first branch is the one a caller fills in
        return _placeholder(spec["anyOf"][0])
    if "enum" in spec:
        return spec["enum"][0]
    if "const" in spec:
        return spec["const"]
    return _EMPTY.get(spec.get("type", ""))


def outcome_colors() -> tuple[str, str]:
    """Green and red for the outcome line, picked for the theme QGIS is running.

    A fixed pair would be unreadable in the other theme, and these carry meaning,
    so they come from the theme's own lightness rather than from a gray.
    """

    dark = QApplication.palette().color(QPalette.Window).lightness() < 128
    return ("#4ade80", "#f87171") if dark else ("#15803d", "#b91c1c")


class ActionTesterWidget(QWidget):
    """Send any registered command with params typed here, and show its answer."""

    def __init__(self, run_command: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        super().__init__()
        self._run_command = run_command
        self._ok_color, self._fail_color = outcome_colors()
        self.setup_ui()
        self.load_action(self.action_combo.currentText())

    def setup_ui(self) -> None:
        """Set up the page: the command, its params, and its answer."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        layout.addWidget(self._build_command_box())
        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(6)
        self.splitter.addWidget(self._build_params_box())
        self.splitter.addWidget(self._build_result_box())
        self.splitter.setSizes([240, 320])
        layout.addWidget(self.splitter, 1)

    def _build_command_box(self) -> QGroupBox:
        """The command to send and the execution mode its declaration offers."""

        box = QGroupBox("命令")
        form = QFormLayout(box)
        form.setContentsMargins(10, 8, 10, 10)
        form.setSpacing(6)
        self.action_combo = QComboBox()
        self.action_combo.addItems(COMMAND_NAMES)
        self.action_combo.currentTextChanged.connect(self.load_action)
        form.addRow("动作", self.action_combo)
        self.execution_combo = QComboBox()
        # Only a command that offers more than one mode has anything to choose.
        self.execution_combo.setEnabled(False)
        form.addRow("执行方式", self.execution_combo)
        return box

    def _build_params_box(self) -> QGroupBox:
        """The JSON object that becomes the command's params."""

        box = QGroupBox("参数（JSON）")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(6)

        hint = QLabel("可选字段已按默认值填好，必填字段需要替换。")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.params_edit = QPlainTextEdit()
        self.params_edit.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.params_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.params_edit, 1)

        # The action sits at the end of the row it acts on, like every dialog.
        row = QHBoxLayout()
        row.addStretch(1)
        self.run_button = QPushButton("运行命令")
        self.run_button.clicked.connect(self.run_action)
        row.addWidget(self.run_button)
        layout.addLayout(row)
        return box

    def _build_result_box(self) -> QGroupBox:
        """The plugin's answer, and how it ended."""

        box = QGroupBox("应答")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(6)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.result_edit = QPlainTextEdit()
        self.result_edit.setReadOnly(True)
        self.result_edit.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.result_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.result_edit.setPlaceholderText("运行命令后，这里显示插件返回的 JSON 应答。")
        layout.addWidget(self.result_edit, 1)
        return box

    def load_action(self, name: str) -> None:
        """Show the modes and the fresh params of the command just picked."""

        action = REGISTRY.get(name)
        if action is None:  # an empty list, or a name from a surface that is gone
            return
        self.execution_combo.clear()
        for mode in action.modes:
            self.execution_combo.addItem(_MODE_LABELS.get(mode, mode), mode)
        self.execution_combo.setEnabled(len(action.modes) > 1)
        self.params_edit.setPlainText(json.dumps(params_template(action), indent=2))
        # The answer on screen belonged to the command that was picked before.
        self.result_edit.clear()
        self._set_status("尚未运行", "")

    def run_action(self) -> None:
        """Run the command as typed and show the answer the pipeline gives."""

        try:
            params = json.loads(self.params_edit.toPlainText())
        except ValueError as error:
            self.show_answer({"status": "error", "code": "invalid_json", "message": str(error)})
            return
        command = {
            "type": self.action_combo.currentText(),
            "params": params,
            "execution": self.execution_combo.currentData(),
        }
        # shortcut: the command runs on the GUI thread, so a slow one (a whole
        # processing algorithm) freezes QGIS until it returns; move a run that
        # outlives this page into an operation instead of threading it.
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.show_answer(self._run_command(command))
        finally:
            QApplication.restoreOverrideCursor()

    def show_answer(self, answer: Any) -> None:
        """Put the answer on screen as JSON; ``default=str`` keeps a stray value readable."""

        self.result_edit.setPlainText(json.dumps(answer, indent=2, ensure_ascii=False, default=str))
        if isinstance(answer, dict) and answer.get("status") == "success":
            self._set_status("运行成功", self._ok_color)
        elif isinstance(answer, dict) and "code" in answer:
            self._set_status(f"运行失败 · {answer['code']}", self._fail_color)
        else:
            self._set_status("尚未运行", "")

    def _set_status(self, text: str, color: str) -> None:
        """Name the outcome above the answer, where it is read first."""

        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {color}; font-weight: 600;" if color else "")
