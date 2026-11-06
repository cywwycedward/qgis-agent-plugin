"""The pipeline's answer contract: the codes and the shape a caller branches on."""

from __future__ import annotations

from typing import Any

import pytest

from qgis_agent_plugin.command import Command, CommandContext, Params, Result
from qgis_agent_plugin.dispatch import Handle, dispatcher
from qgis_agent_plugin.protocol import CommandError


class EchoResult(Result):
    pong: bool = True


class Echo(Command):
    """A command that answers, so a success envelope can be read."""

    name = "echo"

    def run(self, ctx: CommandContext, params: Params) -> EchoResult:
        return EchoResult()


class ModeResult(Result):
    mode: str


class Both(Command):
    """A command offering both modes, so the default of an omitted one is visible."""

    name = "both"
    execution = "both"

    def run(self, ctx: CommandContext, params: Params) -> ModeResult:
        return ModeResult(mode="sync")

    def arun(self, ctx: CommandContext, params: Params) -> ModeResult:
        return ModeResult(mode="async")


class Refuses(Command):
    """A command failing the way a command is expected to fail."""

    name = "refuses"

    def run(self, ctx: CommandContext, params: Params) -> Result:
        raise CommandError("layer_not_found", "Layer not found: x")


class Breaks(Command):
    """A command failing the way a defect fails."""

    name = "breaks"

    def run(self, ctx: CommandContext, params: Params) -> Result:
        raise RuntimeError("boom")


class AnswersADict(Command):
    """A command breaking the result contract, the way a defect does."""

    name = "answers_a_dict"

    def run(self, ctx: CommandContext, params: Params) -> Any:
        return {"pong": True}


COMMANDS = {
    command.name: command for command in (Echo(), Both(), Refuses(), Breaks(), AnswersADict())
}


@pytest.fixture
def handle() -> Handle:
    return dispatcher(CommandContext, COMMANDS)


def test_dispatcher_success_answers_the_result_unchanged(handle: Handle) -> None:
    assert handle({"type": "echo"}) == {"status": "success", "result": {"pong": True}}


def test_dispatcher_unknown_command_answers_unknown_command(handle: Handle) -> None:
    answer = handle({"type": "nope"})

    assert answer["code"] == "unknown_command"
    assert "nope" in answer["message"]


def test_dispatcher_command_that_is_not_an_object_answers_invalid_command(handle: Handle) -> None:
    answer = handle([])

    assert answer == {
        "status": "error",
        "code": "invalid_command",
        "message": "A command must be a JSON object",
    }


def test_dispatcher_unknown_envelope_key_answers_invalid_command(handle: Handle) -> None:
    answer = handle({"type": "echo", "param": {}})

    assert answer["code"] == "invalid_command"
    assert "param" in answer["message"]


def test_dispatcher_async_request_for_a_sync_command_answers_unsupported_execution(
    handle: Handle,
) -> None:
    answer = handle({"type": "echo", "execution": "async"})

    assert answer["code"] == "unsupported_execution"


def test_dispatcher_omitted_execution_takes_the_first_mode(handle: Handle) -> None:
    assert handle({"type": "both"})["result"] == {"mode": "sync"}


def test_dispatcher_requested_execution_takes_the_other_mode(handle: Handle) -> None:
    assert handle({"type": "both", "execution": "async"})["result"] == {"mode": "async"}


def test_dispatcher_unknown_param_answers_invalid_field(handle: Handle) -> None:
    answer = handle({"type": "echo", "params": {"limit": 1}})

    assert answer["code"] == "invalid_field"
    assert "limit" in answer["message"]


def test_dispatcher_expected_failure_keeps_its_own_code(handle: Handle) -> None:
    answer = handle({"type": "refuses"})

    assert answer == {
        "status": "error",
        "code": "layer_not_found",
        "message": "Layer not found: x",
    }


def test_dispatcher_unexpected_failure_answers_internal_error(handle: Handle) -> None:
    answer = handle({"type": "breaks"})

    assert answer["status"] == "error"
    assert answer["code"] == "internal_error"


def test_dispatcher_command_returning_a_bare_dict_answers_internal_error(
    handle: Handle,
) -> None:
    """A result the wire cannot take is a defect, and is answered as one."""

    answer = handle({"type": "answers_a_dict"})

    assert answer["code"] == "internal_error"
