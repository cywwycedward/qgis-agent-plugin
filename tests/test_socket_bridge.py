"""Frame handling: one line is one command, whatever the read happens to carry."""

from __future__ import annotations

import json

from qgis_agent_plugin.qgis_socket_bridge import QGISSocketBridge


class FakeClient:
    """A connected client whose reads are the chunks it was built with.

    An empty queue raises ``BlockingIOError``, which is what the bridge reads as
    "nothing has arrived yet" on a real non-blocking socket.
    """

    def __init__(self, *chunks: bytes) -> None:
        self._chunks = list(chunks)
        self.sent = b""

    def recv(self, size: int) -> bytes:
        if not self._chunks:
            raise BlockingIOError
        return self._chunks.pop(0)

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def close(self) -> None:
        pass


class Echo:
    """A gateway that echoes the command it was handed, so framing is visible."""

    def handle(self, command: object) -> dict[str, object]:
        return {"status": "success", "result": {"seen": command}}


def answers(*chunks: bytes) -> list[dict]:
    """Run the poll loop once per chunk and return the answers it wrote, parsed."""

    bridge = QGISSocketBridge(Echo())
    bridge.running = True
    bridge.client = FakeClient(*chunks)
    for _ in chunks:
        bridge.process_server()
    frames = bridge.client.sent.split(b"\n")
    assert frames.pop() == b""  # every answer ends with the delimiter
    return [json.loads(frame) for frame in frames]


def test_bridge_two_commands_in_one_read_are_answered_twice() -> None:
    assert answers(b'{"type":"a"}\n{"type":"b"}\n') == [
        {"status": "success", "result": {"seen": {"type": "a"}}},
        {"status": "success", "result": {"seen": {"type": "b"}}},
    ]


def test_bridge_half_a_command_waits_for_the_rest() -> None:
    assert answers(b'{"type":"a"', b"}\n") == [
        {"status": "success", "result": {"seen": {"type": "a"}}}
    ]


def test_bridge_frame_that_is_not_json_is_answered_as_invalid_command() -> None:
    assert answers(b"not json\n") == [
        {"status": "error", "code": "invalid_command", "message": "Frame is not valid JSON"}
    ]


def test_bridge_nothing_to_read_answers_nothing() -> None:
    assert answers() == []
