"""The plugin QGIS loads: its lifecycle, and the one place the parts are wired.

A plugin run builds the store once and binds it to one pipeline, so a command
sent over the socket and a command run on the debug page go through the same
code.  Nothing here imports this module: the wiring is one-way.
"""

from __future__ import annotations

from typing import Any

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QAction

from .command import CommandContext
from .commands import COMMANDS
from .dispatch import dispatcher
from .operations import Operations
from .ui.dock import Dock


class Plugin:
    """Main plugin class for qgis-agent-plugin"""

    def __init__(self, iface: Any) -> None:
        self.iface = iface
        # One store per plugin run: an operation registered before the dock is
        # closed must still be readable through get_operation after the next start.
        self.operations = Operations()
        self.handle = dispatcher(self.context, COMMANDS)
        self.dock: Dock | None = None
        self.action: QAction | None = None

    def context(self) -> CommandContext:
        """The handles one command gets; the only place QGIS globals are read."""

        from qgis.core import QgsProject

        return CommandContext(
            iface=self.iface, project=QgsProject.instance(), operations=self.operations
        )

    def initGui(self) -> None:
        """Initialize GUI"""
        # Create action
        action = QAction("QGIS Agent", self.iface.mainWindow())
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
            if not self.dock:
                self.dock = Dock(self.handle)
                self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dock)
                # Connect close event
                self.dock.closed.connect(self.dock_closed)
            else:
                # Show existing dock widget
                self.dock.show()
        else:
            # Hide dock widget
            if self.dock:
                self.dock.hide()

    def dock_closed(self) -> None:
        """Handle dock widget closed"""
        if self.action is not None:
            self.action.setChecked(False)

    def unload(self) -> None:
        """Unload plugin"""
        # Stop server if running
        if self.dock:
            self.dock.closed.disconnect(self.dock_closed)
            self.dock.stop_server()
            self.iface.removeDockWidget(self.dock)
            self.dock = None

        action = self.action
        if action is not None:
            # initGui() made these connections; unload undoes them.
            action.triggered.disconnect(self.toggle_dock)
            # Remove plugin menu item and toolbar icon
            self.iface.removePluginMenu("QGIS Agent", action)
            self.iface.removeToolBarIcon(action)
        self.action = None


# Plugin entry point
def classFactory(iface: Any) -> Plugin:
    return Plugin(iface)
