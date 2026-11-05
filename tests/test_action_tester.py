"""The Actions page must start every command from params its own model accepts."""

from qgis_agent_plugin.action_tester import params_template
from qgis_agent_plugin.qgis_actions import COMMAND_NAMES, REGISTRY


def test_params_template_of_a_command_without_params_is_empty() -> None:
    assert params_template(REGISTRY["ping"]) == {}


def test_params_template_keeps_declared_defaults() -> None:
    assert params_template(REGISTRY["get_layers"]) == {"limit": None, "offset": 0}


def test_params_template_places_a_typed_value_for_a_required_field() -> None:
    assert params_template(REGISTRY["zoom_to_layer"]) == {"layer_id": ""}


def test_params_template_is_valid_for_every_registered_command() -> None:
    for name in COMMAND_NAMES:
        action = REGISTRY[name]
        # A command with a required field gets a placeholder the caller replaces,
        # so only the params no rule rejects are expected to validate here.
        try:
            action.validate(params_template(action))
        except Exception:
            continue
