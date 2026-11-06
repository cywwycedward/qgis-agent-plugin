"""The wire shapes: an answer is dumped once, a failure carries a code."""

from __future__ import annotations

from datetime import UTC, datetime

from qgis_agent_plugin.command import Result
from qgis_agent_plugin.protocol import CommandError, failure, success


class Stamped(Result):
    when: datetime


def test_success_dumps_the_result_as_json() -> None:
    answer = success(Stamped(when=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)))

    assert answer == {"status": "success", "result": {"when": "2026-01-02T03:04:05Z"}}


def test_failure_carries_the_code_and_the_message() -> None:
    assert failure("unknown_command", "Unknown command type: x") == {
        "status": "error",
        "code": "unknown_command",
        "message": "Unknown command type: x",
    }


def test_command_error_is_an_exception_carrying_its_code() -> None:
    error = CommandError("layer_not_found", "Layer not found: x")

    assert str(error) == "Layer not found: x"
    assert error.code == "layer_not_found"
