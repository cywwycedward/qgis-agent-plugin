"""The plugin dock: the local server page and the command debug page."""

from __future__ import annotations

from typing import Any

from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtWidgets import (
    QDockWidget,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..dispatch import Handle
from ..socket_bridge import SocketBridge
from .actions_page import ActionsPage
from .theme import outcome_colors


class Dock(QDockWidget):
    """Dock widget for the qgis-agent-plugin"""

    closed = pyqtSignal()

    def __init__(self, handle: Handle) -> None:
        super().__init__("QGIS Agent")
        self._handle = handle
        self._ok_color, _ = outcome_colors()
        self.server: SocketBridge | None = None
        self.setup_ui()

    def setup_ui(self) -> None:
        """Set up the dock widget UI"""
        # Create widget and layout
        widget = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        widget.setLayout(layout)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.build_server_page(), "服务")
        self.tabs.addTab(ActionsPage(self._handle), "命令调试")
        layout.addWidget(self.tabs)

        # Add to dock widget
        self.setWidget(widget)

    def build_server_page(self) -> QWidget:
        """Build the page that binds the port and starts the bridge"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        box = QGroupBox("本地服务")
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(10, 8, 10, 10)
        box_layout.setSpacing(6)

        form = QFormLayout()
        form.setSpacing(6)
        self.port_spin = QSpinBox()
        self.port_spin.setMinimum(1024)
        self.port_spin.setMaximum(65535)
        self.port_spin.setValue(9876)
        self.port_spin.setToolTip("端口范围 1024–65535，默认 9876。")
        # A port is a number, not a sentence: it does not need the whole width.
        self.port_spin.setMaximumWidth(120)
        form.addRow("端口", self.port_spin)
        box_layout.addLayout(form)

        hint = QLabel("MCP 服务器通过回环地址调用 QGIS 命令。")
        hint.setWordWrap(True)
        box_layout.addWidget(hint)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.start_button = QPushButton("启动服务")
        self.start_button.clicked.connect(self.start_server)
        buttons.addWidget(self.start_button)
        self.stop_button = QPushButton("停止服务")
        self.stop_button.clicked.connect(self.stop_server)
        self.stop_button.setEnabled(False)
        buttons.addWidget(self.stop_button)
        box_layout.addLayout(buttons)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        box_layout.addWidget(self.status_label)
        self._show_running(None)

        layout.addWidget(box)
        layout.addStretch(1)
        return page

    def start_server(self) -> None:
        """Start the server"""
        if self.server is not None:
            return
        server = SocketBridge(self._handle, port=self.port_spin.value())
        if server.start():
            self.server = server
            self._show_running(server)
        else:
            server.stop()

    def stop_server(self) -> None:
        """Stop the server"""
        if self.server:
            self.server.stop()
            self.server = None

        self._show_running(None)

    def _show_running(self, server: SocketBridge | None) -> None:
        """Report the server's state on the widgets that carry it"""
        if server is not None:
            self.status_label.setText(f"服务运行中 · {server.host}:{server.port}")
            self.status_label.setStyleSheet(f"color: {self._ok_color}; font-weight: 600;")
        else:
            self.status_label.setText("服务未启动")
            self.status_label.setStyleSheet("")
        self.start_button.setEnabled(server is None)
        self.stop_button.setEnabled(server is not None)
        self.port_spin.setEnabled(server is None)

    def closeEvent(self, event: Any) -> None:
        """Stop server on dock close"""
        self.stop_server()
        self.closed.emit()
        super().closeEvent(event)
