"""The Actions page must start every command from params its own model accepts."""

from qgis_agent_plugin.params_template import params_template
from qgis_agent_plugin.qgis_actions import COMMAND_NAMES, REGISTRY


def test_params_template_of_a_command_without_params_is_empty() -> None:
    assert params_template(REGISTRY["ping"]) == {}


def test_params_template_keeps_declared_defaults() -> None:
    assert params_template(REGISTRY["get_layers"]) == {"limit": None, "offset": 0}


def test_params_template_places_a_typed_value_for_a_required_field() -> None:
    assert params_template(REGISTRY["zoom_to_layer"]) == {"layer_id": ""}


def test_params_template_is_accepted_by_every_command_whose_fields_are_optional() -> None:
    """A template is only valid where no placeholder has to be replaced first.

    A required field starts as an empty string or a zero the caller has to change,
    so only a command whose every field declares a default has a template its model
    accepts.  The set is read off the schemas, so it follows the command surface
    instead of being a list to keep in step, and it must not be empty.
    """

    optional_only = [
        name
        for name in COMMAND_NAMES
        if not REGISTRY[name].params_model.model_json_schema().get("required")
    ]
    assert optional_only, "no command declares every field optional: nothing is proved here"
    for name in optional_only:
        REGISTRY[name].validate(params_template(REGISTRY[name]))
