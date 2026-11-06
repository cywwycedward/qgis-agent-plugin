"""Socket bridge exposing QGIS Desktop to the MCP server (QGIS main thread)."""

from __future__ import annotations

import json
import logging
import socket
import sys
from collections.abc import Callable
from typing import Any

from .protocol import failure

logger = logging.getLogger(__name__)

# The bridge imports without QGIS or Qt so its socket lifecycle can be tested
# on a plain interpreter; in QGIS both imports succeed and the module behaves
# exactly as before.
try:
    from qgis.core import Qgis, QgsMessageLog
except ImportError:  # pragma: no cover - exercised by the pure test suite
    Qgis = None
    QgsMessageLog = None

try:
    from qgis.PyQt.QtCore import QTimer
except ImportError:  # pragma: no cover - exercised by the pure test suite
    QTimer = None

LOG_TAG = "QGIS Agent"


def _log(message: str, level: str = "info") -> None:
    """Write to the QGIS message log, or to stdlib logging outside QGIS."""

    if QgsMessageLog is None:
        logger.log(getattr(logging, level.upper()), message)
        return
    QgsMessageLog.logMessage(message, LOG_TAG, getattr(Qgis, level.capitalize()))


class SocketBridge:
    """Server class to handle socket connections and execute QGIS commands

    ``handle`` is the bound pipeline: any callable that takes one decoded command
    and answers with one envelope.  The bridge owns the socket and the frame
    encoding, and never touches QGIS itself.
    Without Qt the caller has to drive :meth:`process_server`; with Qt a 100ms
    timer does it on the main thread.

    One frame is one line: a command and an answer are each a JSON object
    followed by ``\\n``.  The delimiter is what tells a whole frame from half of
    one, so a read carrying two pipelined commands is answered twice instead of
    being kept as one frame that never parses.
    """

    def __init__(
        self, handle: Callable[[Any], dict[str, Any]], host: str = "127.0.0.1", port: int = 9876
    ) -> None:
        super().__init__()
        self.handle = handle
        self.host = host
        self.port = port
        self.running = False
        self.socket: Any = None
        self.client: Any = None
        self.buffer = b""
        self.timer: Any = None

    def start(self) -> bool:
        """Start the server"""

        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if sys.platform != "win32":
                # On POSIX this only reclaims a port left in TIME_WAIT.  On Windows
                # it lets a second process bind the port we are listening on, so a
                # second QGIS instance would answer here too; let bind() fail there.
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            self.socket.bind((self.host, self.port))
            self.socket.listen(1)
            self.socket.setblocking(False)

            if QTimer is not None:
                # Process server operations on the QGIS main thread.
                self.timer = QTimer()
                self.timer.timeout.connect(self.process_server)
                self.timer.start(100)  # 100ms interval

            _log(f"QGIS Agent server started on {self.host}:{self.port}")
            # Running is only ever true for a listener that is actually listening,
            # so a port that could not be bound cannot leave a server that answers.
            self.running = True
            return True
        except Exception as error:  # noqa: BLE001 - a start that fails has to answer False
            _log(f"Failed to start server: {error}", "critical")
            self.stop()
            return False

    def stop(self) -> None:
        """Stop the server"""

        was_running = self.running
        self.running = False

        if self.timer:
            self.timer.stop()
            self.timer = None

        if self.socket:
            self.socket.close()
        if self.client:
            self.client.close()

        self.socket = None
        self.client = None
        # Half a frame belonged to the client that is gone, not to the next one.
        self.buffer = b""
        if was_running:
            _log("QGIS Agent server stopped")

    def process_server(self) -> None:
        """Process server operations (called by timer)"""

        if not self.running:
            return
        if self.client is None:
            self._accept()
            if self.client is None:
                return  # no client arrived, so there is nothing to read
        try:
            self._serve_client()
        except Exception as error:  # noqa: BLE001 - any failure here means one client is gone
            _log(f"Error with client: {error}", "warning")
            self._drop()

    def _accept(self) -> None:
        """Take the one connection this listener allows, if one is waiting."""

        try:
            self.client, address = self.socket.accept()
            self.client.setblocking(False)
            _log(f"Connected to client: {address}")
        except BlockingIOError:
            pass  # No connection waiting
        except Exception as error:  # noqa: BLE001 - a failed accept must not stop the timer
            _log(f"Error accepting connection: {error}", "warning")

    def _serve_client(self) -> None:
        """Answer every whole frame the client has sent."""

        try:
            data = self.client.recv(8192)
        except BlockingIOError:
            return  # No data available
        if not data:
            _log("Client disconnected")
            return self._drop()

        self.buffer += data
        # Everything up to a delimiter is a whole frame; the tail is half a
        # command and waits here for the rest of it.
        frames = self.buffer.split(b"\n")
        self.buffer = frames.pop()
        for frame in frames:
            self.client.sendall(self._answer(frame) + b"\n")

    def _answer(self, frame: bytes) -> bytes:
        """Return the answer frame for one whole command frame."""

        try:
            command = json.loads(frame)
        except ValueError:
            # A whole frame that is not JSON is a caller mistake this side can
            # name, so it is answered instead of being dropped.
            answer: Any = failure("invalid_command", "Frame is not valid JSON")
        else:
            answer = self.handle(command)
        return json.dumps(answer).encode("utf-8")

    def _drop(self) -> None:
        """Close the client and forget everything that belonged to it."""

        if self.client is not None:
            self.client.close()
        self.client = None
        self.buffer = b""
