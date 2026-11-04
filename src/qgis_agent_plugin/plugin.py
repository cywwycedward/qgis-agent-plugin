from __future__ import annotations

from collections.abc import Callable
from typing import Any

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QAction,
    QDockWidget,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

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


class QGISAgentDockWidget(QDockWidget):
    """Dock widget for the qgis-agent-plugin"""

    closed = pyqtSignal()

    def __init__(self, build_server: Callable[[int], Any]) -> None:
        super().__init__("QGIS Agent")
        self._build_server = build_server
        self.server: Any = None
        self.setup_ui()

    def setup_ui(self) -> None:
        """Set up the dock widget UI"""
        # Create widget and layout
        widget = QWidget()
        layout = QVBoxLayout()
        widget.setLayout(layout)

        # Add port selection
        layout.addWidget(QLabel("Server Port:"))
        self.port_spin = QSpinBox()
        self.port_spin.setMinimum(1024)
        self.port_spin.setMaximum(65535)
        self.port_spin.setValue(9876)
        layout.addWidget(self.port_spin)

        # Add server control buttons
        self.start_button = QPushButton("Start Server")
        self.start_button.clicked.connect(self.start_server)
        layout.addWidget(self.start_button)

        self.stop_button = QPushButton("Stop Server")
        self.stop_button.clicked.connect(self.stop_server)
        self.stop_button.setEnabled(False)
        layout.addWidget(self.stop_button)

        # Add status label
        self.status_label = QLabel("Server: Stopped")
        layout.addWidget(self.status_label)

        # Add to dock widget
        self.setWidget(widget)

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
            self.status_label.setText(f"Server: Running on port {self.server.port}")
        else:
            self.status_label.setText("Server: Stopped")
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
        self.dock_widget: QGISAgentDockWidget | None = None
        self.action: QAction | None = None

    def initGui(self) -> None:
        """Initialize GUI"""
        # Create action
        self.action = QAction(
            "QGIS Agent",
            self.iface.mainWindow()
        )
        self.action.setCheckable(True)
        self.action.triggered.connect(self.toggle_dock)

        # Add to plugins menu and toolbar
        self.iface.addPluginToMenu("QGIS Agent", self.action)
        self.iface.addToolBarIcon(self.action)

    def toggle_dock(self, checked: bool) -> None:
        """Toggle the dock widget"""
        if checked:
            # Create dock widget if it doesn't exist
            if not self.dock_widget:
                self.dock_widget = QGISAgentDockWidget(self._build_server)
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
        self.action.setChecked(False)

    def unload(self) -> None:
        """Unload plugin"""
        # initGui() and toggle_dock() made these connections; unload undoes them.
        self.action.triggered.disconnect(self.toggle_dock)

        # Stop server if running
        if self.dock_widget:
            self.dock_widget.closed.disconnect(self.dock_closed)
            self.dock_widget.stop_server()
            self.iface.removeDockWidget(self.dock_widget)
            self.dock_widget = None

        # Remove plugin menu item and toolbar icon
        self.iface.removePluginMenu("QGIS Agent", self.action)
        self.iface.removeToolBarIcon(self.action)
        self.action = None


# Plugin entry point
def classFactory(iface: Any) -> QGISAgentPlugin:
    return QGISAgentPlugin(iface)
