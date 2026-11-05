from __future__ import annotations

from collections.abc import Callable
from typing import Any

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QAction,
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

from .action_tester import ActionTesterWidget, outcome_colors
from .qgis_actions import REGISTRY
from .qgis_gateway import QGISGateway
from .qgis_runtime import PluginRuntime
from .qgis_socket_bridge import QGISSocketBridge


def server_builder(runtime: PluginRuntime) -> Callable[[int], QGISSocketBridge]:
    """Return the builder the Dock is handed: the bridge for the port it names.

    It closes over the runtime and not over the plugin on purpose.  A bound method
    would make the Dock hold the plugin while the plugin holds the Dock, and a
    widget freed by the collector rather than by unload() outlives the Qt
    application it was created under.
    """

    def build(port: int) -> QGISSocketBridge:
        gateway = QGISGateway(runtime.context, REGISTRY)
        return QGISSocketBridge(gateway=gateway, port=port)

    return build


def command_runner(runtime: PluginRuntime) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Return the answer the socket would give for one command, without a socket.

    The Actions page sends the envelope a client would send and shows what comes
    back, so a command that works here works for a caller too.
    """

    return QGISGateway(runtime.context, REGISTRY).handle


class QGISAgentDockWidget(QDockWidget):
    """Dock widget for the qgis-agent-plugin"""

    closed = pyqtSignal()

    def __init__(
        self,
        build_server: Callable[[int], Any],
        run_command: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> None:
        super().__init__("QGIS Agent")
        self._build_server = build_server
        self._run_command = run_command
        self._ok_color, _ = outcome_colors()
        self.server: Any = None
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
        self.tabs.addTab(ActionTesterWidget(self._run_command), "动作调试")
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

        hint = QLabel("MCP 服务器通过回环地址调用 QGIS 动作。")
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
        self._show_running(False)

        layout.addWidget(box)
        layout.addStretch(1)
        return page

    def start_server(self) -> None:
        """Start the server"""
        if self.server is not None:
            return
        server = self._build_server(self.port_spin.value())
        if server.start():
            self.server = server
            self._show_running(True)
        else:
            server.stop()

    def stop_server(self) -> None:
        """Stop the server"""
        if self.server:
            self.server.stop()
            self.server = None

        self._show_running(False)

    def _show_running(self, running: bool) -> None:
        """Report the server's state on the widgets that carry it"""
        if running:
            self.status_label.setText(f"服务运行中 · {self.server.host}:{self.server.port}")
            self.status_label.setStyleSheet(f"color: {self._ok_color}; font-weight: 600;")
        else:
            self.status_label.setText("服务未启动")
            self.status_label.setStyleSheet("")
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.port_spin.setEnabled(not running)

    def closeEvent(self, event: Any) -> None:
        """Stop server on dock close"""
        self.stop_server()
        self.closed.emit()
        super().closeEvent(event)


class QGISAgentPlugin:
    """Main plugin class for qgis-agent-plugin"""

    def __init__(self, iface: Any) -> None:
        self.iface = iface
        # One runtime per plugin run: an operation registered before a Stop must
        # still be readable through get_operation after the next Start.
        self.runtime = PluginRuntime(iface)
        self._build_server = server_builder(self.runtime)
        self._run_command = command_runner(self.runtime)
        self.dock_widget: QGISAgentDockWidget | None = None
        self.action: QAction | None = None

    def initGui(self) -> None:
        """Initialize GUI"""
        # Create action
        action = QAction(
            "QGIS Agent",
            self.iface.mainWindow()
        )
        action.setCheckable(True)
        action.triggered.connect(self.toggle_dock)
        self.action = action

        # Add to plugins menu and toolbar
        self.iface.addPluginToMenu("QGIS Agent", action)
        self.iface.addToolBarIcon(action)

    def toggle_dock(self, checked: bool) -> None:
        """Toggle the dock widget"""
        if checked:
            # Create dock widget if it doesn't exist
            if not self.dock_widget:
                self.dock_widget = QGISAgentDockWidget(self._build_server, self._run_command)
                self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dock_widget)
                # Connect close event
                self.dock_widget.closed.connect(self.dock_closed)
            else:
                # Show existing dock widget
                self.dock_widget.show()
        else:
            # Hide dock widget
            if self.dock_widget:
                self.dock_widget.hide()

    def dock_closed(self) -> None:
        """Handle dock widget closed"""
        if self.action is not None:
            self.action.setChecked(False)

    def unload(self) -> None:
        """Unload plugin"""
        # Stop server if running
        if self.dock_widget:
            self.dock_widget.closed.disconnect(self.dock_closed)
            self.dock_widget.stop_server()
            self.iface.removeDockWidget(self.dock_widget)
            self.dock_widget = None

        action = self.action
        if action is not None:
            # initGui() made these connections; unload undoes them.
            action.triggered.disconnect(self.toggle_dock)
            # Remove plugin menu item and toolbar icon
            self.iface.removePluginMenu("QGIS Agent", action)
            self.iface.removeToolBarIcon(action)
        self.action = None


# Plugin entry point
def classFactory(iface: Any) -> QGISAgentPlugin:
    return QGISAgentPlugin(iface)
