"""The gateway's answer contract: the codes and the shape a caller branches on."""

from __future__ import annotations

import pytest

from qgis_agent_plugin.qgis_actions.base import Action, ActionContext, ActionError
from qgis_agent_plugin.qgis_gateway import QGISGateway


class Echo(Action):
    """A command that answers, so a success envelope can be read."""

    name = "echo"

    def run(self, ctx: ActionContext, params: object) -> dict[str, bool]:
        return {"pong": True}


class Both(Action):
    """A command offering both modes, so the default of an omitted one is visible."""

    name = "both"
    execution = "both"

    def run(self, ctx: ActionContext, params: object) -> dict[str, str]:
        return {"mode": "sync"}

    def arun(self, ctx: ActionContext, params: object) -> dict[str, str]:
        return {"mode": "async"}


class Refuses(Action):
    """A command failing the way an action is expected to fail."""

    name = "refuses"

    def run(self, ctx: ActionContext, params: object) -> None:
        raise ActionError("layer_not_found", "Layer not found: x")


class Breaks(Action):
    """A command failing the way a defect fails."""

    name = "breaks"

    def run(self, ctx: ActionContext, params: object) -> None:
        raise RuntimeError("boom")


REGISTRY = {action.name: action for action in (Echo(), Both(), Refuses(), Breaks())}


@pytest.fixture
def gateway() -> QGISGateway:
    return QGISGateway(ActionContext, REGISTRY)


def test_gateway_success_answers_the_result_unchanged(gateway: QGISGateway) -> None:
    assert gateway.handle({"type": "echo"}) == {"status": "success", "result": {"pong": True}}


def test_gateway_unknown_command_answers_unknown_command(gateway: QGISGateway) -> None:
    answer = gateway.handle({"type": "nope"})

    assert answer["code"] == "unknown_command"
    assert "nope" in answer["message"]


def test_gateway_command_that_is_not_an_object_answers_invalid_command(
    gateway: QGISGateway,
) -> None:
    answer = gateway.handle([])

    assert answer == {
        "status": "error",
        "code": "invalid_command",
        "message": "A command must be a JSON object",
    }


def test_gateway_unknown_envelope_key_answers_invalid_command(gateway: QGISGateway) -> None:
    answer = gateway.handle({"type": "echo", "param": {}})

    assert answer["code"] == "invalid_command"
    assert "param" in answer["message"]


def test_gateway_async_request_for_a_sync_command_answers_unsupported_execution(
    gateway: QGISGateway,
) -> None:
    answer = gateway.handle({"type": "echo", "execution": "async"})

    assert answer["code"] == "unsupported_execution"


def test_gateway_omitted_execution_takes_the_first_mode(gateway: QGISGateway) -> None:
    assert gateway.handle({"type": "both"})["result"] == {"mode": "sync"}


def test_gateway_requested_execution_takes_the_other_mode(gateway: QGISGateway) -> None:
    assert gateway.handle({"type": "both", "execution": "async"})["result"] == {"mode": "async"}


def test_gateway_unknown_param_answers_invalid_field(gateway: QGISGateway) -> None:
    answer = gateway.handle({"type": "echo", "params": {"limit": 1}})

    assert answer["code"] == "invalid_field"
    assert "limit" in answer["message"]


def test_gateway_expected_failure_keeps_its_own_code(gateway: QGISGateway) -> None:
    answer = gateway.handle({"type": "refuses"})

    assert answer == {
        "status": "error",
        "code": "layer_not_found",
        "message": "Layer not found: x",
    }


def test_gateway_unexpected_failure_answers_internal_error(gateway: QGISGateway) -> None:
    answer = gateway.handle({"type": "breaks"})

    assert answer["status"] == "error"
    assert answer["code"] == "internal_error"
